"""Dashboard read-only HTTP API (stdlib only — no extra runtime deps; matches the repo's minimalism).

M1 scope: auth + all READ endpoints. Writes (command channel, param PATCH) land in M2/M4. The API
opens a fresh read-only SQLite connection per request (WAL → never blocks the Observer's writes) and
NEVER mutates business state. Response envelope: {"data": ..., "serverTime": ISO}. All amounts USD;
ratios are percent numbers (28.45 == 28.45%) unless suffixed Pct.

Run via hl_dashboard.py. Endpoints:
  POST /api/auth/login            {password} -> {token, expiresAt}
  GET  /api/overview
  GET  /api/equity?range=1d|7d|all
  GET  /api/insights
  GET  /api/positions?status=open|closed&coin=&wallet=&type=&side=
  GET  /api/wallets
  GET  /api/wallets/{address}
  GET  /api/discovery
  GET  /api/scan-runs?limit=20
  GET  /api/params
"""
import calendar
import json
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config
from . import params as params_mod
from . import procman
from .util import now_iso

# ─────────────────────────────────────────────────────────────────────────── auth
TOKEN_TTL_S = 24 * 3600

# Score display scale. v5 native score is already [0,1], so display is a plain ×100 (engine/DB stay
# native). MIN_FOLLOW_SCORE=0.50 -> 50. Applied to wallet scores, the follow line, AND the
# MIN_FOLLOW_SCORE setting so the operator reads ONE 0–100 ruler everywhere.
def score100(raw):
    """Native v5 score [0,1] -> 0–100 display."""
    if raw is None:
        return None
    return round(min(max(raw, 0.0), 1.0) * 100, 1)


def recent_roi_pct(week_roi, mon_roi):
    """Dashboard ROI column = the SAME recent return-on-capital the SCORE's ROI pillar uses: a weighted
    blend of HL week/month roi (net/本金, deposit-adjusted; all-time excluded — copy only cares about
    recent form). Shown raw (unclipped) — the score clips each window to +100% only to stop a single
    window flying away; the display shows the true recent return so the ROI column explains the ranking."""
    parts = [(config.ROI_W_WEEK, week_roi), (config.ROI_W_MON, mon_roi)]
    w = sum(wt for wt, v in parts if v is not None)
    return (sum(wt * v for wt, v in parts if v is not None) / w * 100.0) if w else 0.0


def score_from100(disp):
    """Inverse of score100 — UI 0–100 -> native [0,1] before writing MIN_FOLLOW_SCORE."""
    if disp is None:
        return None
    return disp / 100.0


class Auth:
    """Single-user opaque-token auth. Username from $DASH_USER / secret/dash_user (default 'admin');
    password from $DASH_PASSWORD / secret/dash_password."""

    def __init__(self):
        self.username = os.environ.get("DASH_USER") or self._read("secret/dash_user") or "admin"
        self.password = self._load_password()
        self._tokens = {}            # token -> expiry_epoch
        self._lock = threading.Lock()
        self._fail_until = 0.0       # crude global login throttle after a failure

    @staticmethod
    def _read(path):
        try:
            with open(path) as fh:
                return fh.read().strip() or None
        except OSError:
            return None

    @classmethod
    def _load_password(cls):
        pw = os.environ.get("DASH_PASSWORD")
        if pw:
            return pw
        for p in ("secret/dash_password", "secret/dashboard.txt"):
            s = cls._read(p)
            if s:
                return s
        print("WARN: no DASH_PASSWORD / secret/dash_password — using insecure default 'changeme'")
        return "changeme"

    def login(self, username, password):
        now = time.time()
        if now < self._fail_until:
            return None, "rate_limited"
        ok = (password and secrets.compare_digest(str(username or ""), self.username)
              and secrets.compare_digest(str(password), self.password))
        if not ok:
            self._fail_until = now + 1.5      # throttle brute force
            return None, "invalid_credentials"
        token = secrets.token_urlsafe(32)
        exp = now + TOKEN_TTL_S
        with self._lock:
            self._tokens[token] = exp
            self._prune(now)
        return token, None

    def valid(self, token):
        if not token:
            return False
        with self._lock:
            exp = self._tokens.get(token)
            if exp is None:
                return False
            if exp < time.time():
                self._tokens.pop(token, None)
                return False
            return True

    def _prune(self, now):
        for t, e in list(self._tokens.items()):
            if e < now:
                self._tokens.pop(t, None)


# ─────────────────────────────────────────────────────────────────────── db helpers
def ro_connect(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        pass
    return db


def rw_connect(path):
    """Read-WRITE connection — used ONLY to write the command channel / params (never business tables).
    The DB is already WAL (storage.connect); busy_timeout lets a brief insert wait out the engine's commit."""
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    return db


# commands the dashboard may enqueue. observer owns the first five; scanner owns rescan; patch_params
# is reserved (M4 uses PATCH /api/params directly, but the type is accepted for completeness).
# observer owns pause/resume/close/toggle (soft, in-process); the scan-trigger SUPERVISOR owns
# observer_start/observer_stop (process lifecycle via systemctl — the observer can't start itself);
# scanner owns rescan; patch_params is reserved (M4 uses PATCH /api/params directly).
ALLOWED_COMMANDS = {"pause", "resume", "close_position", "close_all", "wallet_toggle",
                    "observer_start", "observer_stop", "rescan", "patch_params", "reload_params"}
PROC_STALE_SEC = 90       # heartbeat older than this -> the process is likely dead (UI shows stale)


def insert_command(db_path, ctype, payload, idem):
    db = rw_connect(db_path)
    try:
        if idem:
            row = db.execute("SELECT id,status FROM commands WHERE idempotency_key=?", (idem,)).fetchone()
            if row:
                return row["id"], row["status"]            # idempotent replay -> same command
        cur = db.execute(
            "INSERT INTO commands (type,payload_json,idempotency_key,owner,status,created_at) "
            "VALUES (?,?,?,?,'pending',?)",
            (ctype, json.dumps(payload or {}), idem, "dashboard", now_iso()))
        db.commit()
        return cur.lastrowid, "pending"
    finally:
        db.close()


# Process-lifecycle commands the dashboard EXECUTES DIRECTLY (self-contained control plane — no separate
# supervisor daemon). observer_start/stop spawn/kill the observer child; rescan spawns a one-shot scan.
# Everything is still recorded in `commands` so the frontend's poll-by-id contract is unchanged.
PROCESS_COMMANDS = {"observer_start", "observer_stop", "rescan"}


def _resolve_command(db_path, cmd_id, status, result):
    try:
        db = rw_connect(db_path)
        db.execute("UPDATE commands SET status=?,done_at=?,result_json=? WHERE id=?",
                   (status, now_iso(), json.dumps(result or {}), cmd_id))
        db.commit()
        db.close()
    except sqlite3.Error:
        pass


def exec_process_command(db_path, ctype):
    """Run a process-lifecycle command inline via procman; record it in `commands` for the frontend.
    observer_start/stop resolve immediately; rescan stays 'pending' (the row IS the manual-scan marker
    scanner.scan reads) and is driven to 'done' by the scan process it spawns, so the UI tracks the real scan."""
    cmd_id, _ = insert_command(db_path, ctype, None, None)
    try:
        if ctype == "observer_start":
            res = procman.start_observer(db_path)
        elif ctype == "observer_stop":
            res = procman.stop_observer(db_path)
        else:                                   # rescan: leave the row pending; the spawned scan acks/resolves it
            procman.start_scan(db_path)
            return cmd_id, "pending"
        _resolve_command(db_path, cmd_id, "done", res)
        return cmd_id, "done"
    except Exception as e:  # noqa: BLE001
        _resolve_command(db_path, cmd_id, "error", {"error": str(e)})
        return cmd_id, "error"


def q1(db, sql, args=(), default=None):
    """First row (or default). Tolerates a missing table (un-migrated db) -> default."""
    try:
        return db.execute(sql, args).fetchone()
    except sqlite3.OperationalError:
        return default


def qall(db, sql, args=()):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def _iso_epoch(s):
    if not s:
        return None
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))   # ISO is UTC -> timegm (not mktime/local)
    except (ValueError, TypeError):
        return None


def _iso_ago(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


# ─────────────────────────────────────────────────────────────────────── endpoints
def _scanner_status(db):
    """Live status of the CONTINUOUS rolling scanner (distinct from a stop-the-world full rescan).
    mode: rolling (always-on trickle) | scanning (full rescan) | stopped | unknown. detail carries the
    rolling sweep position / pace / last-touched wallet so the UI can show it's actively working."""
    r = q1(db, "SELECT state,heartbeat_at,detail_json FROM process_status WHERE name='scanner'")
    if not r:
        # no status row yet (scanner runs as a 6h batch — hasn't written one this code-version). If scans
        # have ever run, treat as idle-between-cycles (healthy), not a scary 'unknown'.
        ran = q1(db, "SELECT COUNT(*) c FROM scan_runs")
        return {"mode": "idle" if (ran and ran["c"]) else "unknown", "stale": False,
                "heartbeatAt": None, "detail": {}}
    try:
        detail = json.loads(r["detail_json"]) if r["detail_json"] else {}
    except (ValueError, TypeError):
        detail = {}
    hb = _iso_epoch(r["heartbeat_at"])
    return {"mode": r["state"] or "unknown",
            "stale": bool(hb and (time.time() - hb) > PROC_STALE_SEC),
            "heartbeatAt": r["heartbeat_at"], "detail": detail}


def _followed_count(db, line):
    """Count of wallets we ACTUALLY copy — mirrors observer.load_targets: score ≥ line AND the evidence
    floor (n_trades ≥ FOLLOW_MIN_TRADES, active_days ≥ FOLLOW_MIN_ACTIVE_DAYS), enabled. (NOT just
    score ≥ line — that overstates by the thin-sample wallets the floor holds back.)"""
    r = q1(db, "SELECT COUNT(*) cnt FROM watchlist w "
               "LEFT JOIN target_controls tc ON tc.addr=w.addr "
               "LEFT JOIN profile p ON p.addr=w.addr "
               "WHERE COALESCE(tc.enabled,1)=1 AND w.score>=? "
               "AND COALESCE(w.n_trades,0)>=? AND COALESCE(p.active_days,0)>=?",
               (line, config.FOLLOW_MIN_TRADES, config.FOLLOW_MIN_ACTIVE_DAYS))
    return (r["cnt"] if r else 0)


def _follow_positions(db):
    """{addr: 1-based position in the copy set} — same filter+order as observer.load_targets. Lets position/
    history badges show the follow-序号 (1..N) instead of the confusing global watchlist rank (#29 > 25)."""
    line = params_mod.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE
    rows = qall(db, "SELECT w.addr FROM watchlist w LEFT JOIN target_controls tc ON tc.addr=w.addr "
                    "LEFT JOIN profile p ON p.addr=w.addr WHERE COALESCE(tc.enabled,1)=1 AND w.score>=? "
                    "AND COALESCE(w.n_trades,0)>=? AND COALESCE(p.active_days,0)>=? ORDER BY w.rank",
                    (line, config.FOLLOW_MIN_TRADES, config.FOLLOW_MIN_ACTIVE_DAYS))
    return {r["addr"]: i + 1 for i, r in enumerate(rows)}


def ep_overview(db):
    # LIVE-DERIVE from copy_position + copy_account (fresh as of the observer's 25s mark refresh) rather
    # than the 5-min account_stats snapshot row — so the cards aren't up to 5 minutes stale. account_stats
    # is used only for the today baseline + the equity curve (ep_equity).
    acct = q1(db, "SELECT initial_balance, balance FROM copy_account WHERE id=1")
    if acct is None:
        base = {"equity": 0, "roiPct": 0, "todayPct": 0, "realizedPnl": 0, "unrealizedPnl": 0,
                "winRatePct": 0, "openCount": 0, "availableBalance": 0, "availablePctOfEquity": 0,
                "risk": {"gross": 0, "net": 0, "netGrossRatioPct": 0, "longPct": 0, "shortPct": 0},
                "fees": {"cumulative": 0, "netPerGrossBp": 0}, "lastUpdate": None}
    else:
        init = acct["initial_balance"] or 1.0
        balance = acct["balance"] or 0.0
        upnl = locked = gross = net = 0.0
        for r in qall(db, "SELECT side,rem_size,size,entry_px,mark_px,unrealized_pnl,margin,notional "
                          "FROM copy_position WHERE status='open' AND size>0"):
            sgn = 1 if r["side"] == "long" else -1
            mark = r["mark_px"] if r["mark_px"] else (r["entry_px"] or 0)
            u = r["unrealized_pnl"] if r["unrealized_pnl"] is not None else \
                (r["rem_size"] or 0) * (mark - (r["entry_px"] or 0)) * sgn
            upnl += u
            frac = (r["rem_size"] / r["size"]) if r["size"] else 0
            locked += (r["margin"] or 0) * frac
            cur_notl = (r["notional"] or 0) * frac
            gross += cur_notl
            net += cur_notl * sgn
        open_n = (q1(db, "SELECT COUNT(*) c FROM copy_position WHERE status='open'") or {"c": 0})["c"]
        closed = [row["realized_pnl"] for row in qall(db,
                  "SELECT realized_pnl FROM copy_position WHERE status!='open'")]
        win_rate = (sum(1 for r in closed if (r or 0) > 0) / len(closed)) if closed else 0.0
        gross_traded = (q1(db, "SELECT COALESCE(SUM(ABS(our_qty_delta*our_px)),0) g FROM copy_action")
                        or {"g": 0})["g"] or 0.0
        equity = balance + upnl
        realized = balance - init
        available = balance - locked
        long_n = (gross + net) / 2 if gross else 0.0
        short_n = (gross - net) / 2 if gross else 0.0
        eq24 = q1(db, "SELECT equity FROM account_stats WHERE ts<=? ORDER BY ts DESC LIMIT 1",
                  (_iso_ago(24 * 3600),))
        today = ((equity / eq24["equity"] - 1) * 100) if (eq24 and eq24["equity"]) else 0.0
        bp = (realized / gross_traded * 1e4) if gross_traded else 0.0
        base = {
            "equity": equity, "roiPct": (equity / init - 1) * 100, "todayPct": today,
            "realizedPnl": realized, "unrealizedPnl": upnl,
            "winRatePct": win_rate * 100, "openCount": open_n,
            "availableBalance": available,
            "availablePctOfEquity": (available / equity * 100) if equity else 0.0,
            "risk": {"gross": gross, "net": net,
                     "netGrossRatioPct": (net / gross * 100) if gross else 0.0,
                     "longPct": (long_n / gross * 100) if gross else 0.0,
                     "shortPct": (short_n / gross * 100) if gross else 0.0},
            "fees": {"cumulative": gross_traded * config.TAKER_FEE, "netPerGrossBp": bp},
            "lastUpdate": (q1(db, "SELECT MAX(ts) m FROM account_stats") or {"m": None})["m"],
        }
    # system block. process_status may be absent (pre-M2 db) -> sensible defaults; a stale heartbeat
    # (dead process) is flagged so the UI doesn't show a dead observer as "running".
    obs = q1(db, "SELECT state,heartbeat_at FROM process_status WHERE name='observer'")
    ss = _scanner_status(db)
    last_scan = q1(db, "SELECT MAX(finished_at) m FROM scan_runs")
    _line = params_mod.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE   # "被跟" = wallets we actually copy
    wl = {"c": _followed_count(db, _line)}                        # score≥line AND evidence floor (real set)

    def _stale(row):
        if not row or not row["heartbeat_at"]:
            return False
        hb = _iso_epoch(row["heartbeat_at"])
        return bool(hb and (time.time() - hb) > PROC_STALE_SEC)

    # observer is a 3-state PROCESS now: stopped (down — no row / supervisor wrote 'stopped' / heartbeat
    # gone dead) vs running vs paused (soft). The dashboard's on/off control drives the process via the
    # supervisor; the pause/resume control only applies while it's up.
    obs_state = ("stopped" if (not obs or obs["state"] == "stopped" or _stale(obs))
                 else (obs["state"] or "running"))

    base["system"] = {
        "observer": obs_state,
        "observerStale": _stale(obs),
        "observerHeartbeatAt": (obs["heartbeat_at"] if obs else None),
        "scanner": ss["mode"],                  # rolling | scanning | stopped | unknown
        "scannerStale": ss["stale"],
        "scannerHeartbeatAt": ss["heartbeatAt"],
        "scannerDetail": ss["detail"],          # rolling sweep position / pace / last wallet
        "lastScanAt": (last_scan["m"] if last_scan else None),
        "watchlistCount": (wl["c"] if wl else 0),
        "mode": "paper",
    }
    return base


def ep_equity(db, rng):
    cutoff = {"1d": _iso_ago(86400), "7d": _iso_ago(7 * 86400)}.get(rng)
    if cutoff:
        rows = qall(db, "SELECT ts,equity FROM account_stats WHERE ts>=? ORDER BY ts", (cutoff,))
    else:
        rng = "all"
        rows = qall(db, "SELECT ts,equity FROM account_stats ORDER BY ts")
    pts = [{"t": r["ts"], "equity": r["equity"]} for r in rows]
    max_pts = 300                                  # downsample by stride, always keep the last point
    if len(pts) > max_pts:
        stride = len(pts) // max_pts + 1
        pts = pts[::stride] + ([pts[-1]] if (len(pts) - 1) % stride else [])
    return {"range": rng, "points": pts}


def _top_bottom(rows, key, top=5, bottom=3):
    """Sorted-by-`key`-desc winners + losers, no overlap. <=top+bottom rows -> return all."""
    s = sorted(rows, key=lambda r: r[key], reverse=True)
    if len(s) <= top + bottom:
        return s
    return s[:top] + s[-bottom:]


def ep_insights(db):
    """Forward-performance breakdowns for the Overview home page: which followed WALLETS and which
    COINS actually make/lose us money (net = realized on closed + unrealized on open copies)."""
    NET = "COALESCE(SUM(CASE WHEN cp.status!='open' THEN cp.realized_pnl ELSE cp.unrealized_pnl END),0)"
    wallets = [{
        "address": r["addr"], "rank": r["rank"], "netPnl": r["net"] or 0.0, "closedN": r["cn"] or 0,
        "winRatePct": (r["wn"] / r["cn"] * 100) if r["cn"] else None,
    } for r in qall(db,
        f"SELECT cp.addr, {NET} net, w.rank, "
        "SUM(CASE WHEN cp.status!='open' THEN 1 ELSE 0 END) cn, "
        "SUM(CASE WHEN cp.status!='open' AND cp.realized_pnl>0 THEN 1 ELSE 0 END) wn "
        "FROM copy_position cp LEFT JOIN watchlist w ON w.addr=cp.addr GROUP BY cp.addr")]
    coins = [{"coin": r["coin"], "netPnl": r["net"] or 0.0, "n": r["n"]} for r in qall(db,
        f"SELECT cp.coin, {NET} net, COUNT(*) n FROM copy_position cp GROUP BY cp.coin")]
    return {"walletContrib": _top_bottom(wallets, "netPnl"), "coinPnl": _top_bottom(coins, "netPnl")}


def ep_positions(db, qs):
    status = (qs.get("status", ["open"])[0])
    if status == "closed":
        where, args = ["cp.status!='open'"], []
        for col, key in (("cp.coin", "coin"), ("cp.addr", "wallet"), ("cp.side", "side")):
            if qs.get(key):
                where.append(f"{col}=?"); args.append(qs[key][0])
        rows = qall(db, "SELECT cp.pos_id,cp.coin,cp.side,cp.realized_pnl,cp.opened_at,cp.closed_at,"
                        "cp.entry_px,cp.leverage,cp.notional,cp.master_open_px,cp.master_leverage,cp.master_margin,"
                        "cp.was_stopped,cp.was_liq,cp.add_count,cp.addr,w.rank AS wrank FROM copy_position cp "
                        "LEFT JOIN watchlist w ON w.addr=cp.addr WHERE " + " AND ".join(where) +
                        " ORDER BY cp.closed_at DESC LIMIT 100", tuple(args))   # most recent 100 (UI paginates 25/page)
        fpos = _follow_positions(db)
        out = []
        for r in rows:
            o, c = _iso_epoch(r["opened_at"]), _iso_epoch(r["closed_at"])
            pnl = r["realized_pnl"] or 0.0
            # avg exit price, derived from realized PnL (exact in the fee-less paper model):
            # pnl = size·(exit−entry)·sign  →  exit = entry + sign·pnl/size,  size = notional/entry.
            entry = r["entry_px"]; notl = r["notional"] or 0.0
            size = (notl / entry) if entry else 0.0
            close_px = (entry + (1 if r["side"] == "long" else -1) * pnl / size) if size else None
            out.append({"id": f"cls_{r['pos_id']}", "coin": r["coin"], "side": r["side"],
                        "realizedPnl": pnl, "durationSec": int(c - o) if (o and c) else None,
                        "closedAt": c,   # epoch sec (UTC); frontend renders in UTC+8
                        "result": "win" if pnl > 0 else "loss", "wallet": r["addr"],
                        # 结算类型: liq=爆仓 / stop=我们主动σ止损 / mirror=镜像跟随目标平仓
                        "closeType": "liq" if r["was_liq"] else ("stop" if r["was_stopped"] else "mirror"),
                        "walletRank": r["wrank"],   # wrank None = 已脱榜
                        "followPos": fpos.get(r["addr"]),   # 1..N in the copy set (None = 现在不在跟单集)
                        "entry": r["entry_px"], "closePx": close_px, "addCount": r["add_count"] or 0,
                        "leverage": r["leverage"], "notional": r["notional"] or 0.0,
                        "masterEntry": r["master_open_px"], "masterLeverage": r["master_leverage"],
                        "masterNotional": (r["master_margin"] or 0.0) * (r["master_leverage"] or 0.0)})
        # all-time stats over the FULL closed set (not just the recent-100 list above), honoring any filter
        sw = "cp.status!='open'" + ("".join(f" AND {c}=?" for c, k in
             (("cp.coin", "coin"), ("cp.addr", "wallet"), ("cp.side", "side")) if qs.get(k)))
        s = q1(db,
            "SELECT COUNT(*) n, "
            "SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins, "
            "COALESCE(SUM(realized_pnl),0) total, AVG(realized_pnl) avg_pnl, "
            "MAX(realized_pnl) best, MIN(realized_pnl) worst, "
            "COALESCE(SUM(CASE WHEN realized_pnl>0 THEN realized_pnl ELSE 0 END),0) gwin, "
            "COALESCE(SUM(CASE WHEN realized_pnl<0 THEN realized_pnl ELSE 0 END),0) gloss, "
            "AVG(CASE WHEN realized_pnl>0 THEN realized_pnl END) avg_win, "
            "AVG(CASE WHEN realized_pnl<0 THEN realized_pnl END) avg_loss, "
            "AVG((julianday(closed_at)-julianday(opened_at))*86400.0) avg_hold "
            "FROM copy_position cp WHERE " + sw, tuple(args))
        n = (s["n"] if s else 0) or 0
        wins = (s["wins"] if s else 0) or 0
        gloss = (s["gloss"] if s else 0.0) or 0.0
        stats = {
            "total": n, "wins": wins, "losses": n - wins,
            "winRatePct": (wins / n * 100) if n else None,
            "totalPnl": (s["total"] if s else 0.0) or 0.0,
            "avgPnl": s["avg_pnl"] if s else None,
            "bestPnl": s["best"] if s else None, "worstPnl": s["worst"] if s else None,
            "avgWin": s["avg_win"] if s else None, "avgLoss": s["avg_loss"] if s else None,
            "profitFactor": ((s["gwin"] or 0.0) / abs(gloss)) if gloss else None,
            "avgHoldSec": s["avg_hold"] if s else None,
        }
        return {"positions": out, "stats": stats}

    # status=open. Only RESOLVED positions (entry_px/size set) — a just-opened row sits unresolved for a
    # few seconds while its price/size are fetched; showing it would flash a 0.0 entry/mark.
    where, args = ["cp.status='open'", "cp.size>0", "cp.entry_px IS NOT NULL", "cp.entry_px>0"], []
    for col, key in (("cp.coin", "coin"), ("cp.addr", "wallet"), ("cp.side", "side"),
                     ("COALESCE(w.market_type,pr.market_type)", "type")):
        if qs.get(key):
            where.append(f"{col}=?"); args.append(qs[key][0])
    rows = qall(db,
        "SELECT cp.pos_id,cp.coin,cp.side,cp.entry_px,cp.leverage,cp.margin,cp.notional,cp.size,"
        "cp.rem_size,cp.liq_px,cp.mark_px,cp.unrealized_pnl,cp.open_lag_sec,cp.addr,cp.add_count,"
        "cp.master_open_px,cp.master_leverage,cp.master_margin,"
        "w.rank AS wrank,COALESCE(w.market_type,pr.market_type) AS mtype "
        "FROM copy_position cp "
        "LEFT JOIN watchlist w ON w.addr=cp.addr "
        "LEFT JOIN profile pr ON pr.addr=cp.addr "
        "WHERE " + " AND ".join(where) + " ORDER BY cp.opened_at DESC", tuple(args))
    fpos = _follow_positions(db)
    out, float_total = [], 0.0
    for r in rows:
        entry = r["entry_px"] or 0.0
        mark = r["mark_px"] if r["mark_px"] else entry           # null until Observer persists (M2)
        held = (r["rem_size"] / r["size"]) if r["size"] else 1.0   # remaining fraction (< 1 after a partial close)
        margin = (r["margin"] or 0.0) * held                     # EFFECTIVE locked margin on the remaining size
        upnl = r["unrealized_pnl"] if r["unrealized_pnl"] is not None else 0.0
        float_total += upnl
        liq = r["liq_px"]
        # Distance-to-liquidation as a consistent NEGATIVE buffer regardless of side: the % adverse
        # move from mark to liq_px (0 = at liquidation). -1.5 = 1.5% away (danger), -30 = comfortable.
        liq_dist = (-abs(liq / mark - 1) * 100) if (liq and mark) else None
        out.append({
            "id": f"pos_{r['pos_id']}", "coin": r["coin"], "marketType": r["mtype"] or "crypto",
            "side": r["side"], "entry": entry, "leverage": r["leverage"],
            # scale by remaining/total so a PARTIAL close (rem_size < size) shows the current notional
            "notional": (r["notional"] or 0.0) * held, "mark": mark,
            "unrealizedPnl": upnl,
            "unrealizedPctOfMargin": (upnl / margin * 100) if margin else 0.0,   # vs EFFECTIVE margin (scaled)
            "wallet": r["addr"], "walletRank": r["wrank"], "followPos": fpos.get(r["addr"]),
            "lagSec": r["open_lag_sec"], "liqPx": liq, "liqDistancePct": liq_dist,
            "masterEntry": r["master_open_px"], "masterLeverage": r["master_leverage"],
            "masterNotional": (r["master_margin"] or 0.0) * (r["master_leverage"] or 0.0),
            "addCount": r["add_count"] or 0,
        })
    return {"summary": {"floatingPnl": float_total, "openCount": len(out)}, "positions": out}


def _wallet_trend(db, addr, n=8):
    rows = qall(db, "SELECT realized_pnl FROM copy_position WHERE addr=? AND status!='open' "
                    "ORDER BY closed_at LIMIT ?", (addr, n))
    trend, cum = [], 0.0
    for r in rows:
        cum += r["realized_pnl"] or 0.0
        trend.append(round(cum, 2))
    return trend


def ep_wallets(db, qs=None):
    qs = qs or {}
    line_native = params_mod.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE   # native [0,1] scale
    grid_max = params_mod.get(db, "grid_max_adds", 5) or 5
    page = max(0, int((qs.get("page", ["0"]))[0]))
    size = min(100, max(1, int((qs.get("size", ["30"]))[0])))
    # DROPPED tab: wallets that WERE on the follow line (follow_history stamped) but are now below it or
    # no longer active — "recently demoted for poor performance". Recovers automatically if it climbs back.
    if (qs.get("tab", ["followed"]))[0] == "dropped":
        rows = qall(db,
            "SELECT fh.addr,fh.last_followed_at,fh.last_followed_score,p.score,p.status,p.reason,"
            "p.market_type,p.win_rate,p.roi_equity,p.net_pnl,p.avg_notional,p.roi_total,p.top_coin,w.rank AS rank,"
            "l.week_roi,l.mon_roi "
            "FROM follow_history fh JOIN profile p ON p.addr=fh.addr "
            "LEFT JOIN watchlist w ON w.addr=fh.addr "
            "LEFT JOIN leaderboard l ON l.addr=fh.addr "
            "WHERE NOT (p.status='active' AND p.score >= ?) ORDER BY fh.last_followed_at DESC", (line_native,))
        out = [{
            "address": r["addr"], "rank": r["rank"], "marketType": r["market_type"] or "crypto",
            "score": score100(r["score"] or 0.0), "lastFollowedScore": score100(r["last_followed_score"] or 0.0),
            "lastFollowedAt": _iso_epoch(r["last_followed_at"]),
            "dropReason": ("掉出评分线" if r["status"] == "active" else {"inactive": "失活", "blowup_loss": "扛单爆亏",
                "spot_hedge": "对冲盘", "not_profitable": "转亏", "irregular": "低频", "grid_dca": "网格",
                "bot_frequency": "高频", "hft_uncopyable": "高频", "spot_dominant": "现货为主"}.get(r["reason"], r["reason"] or "淘汰")),
            "winRatePct": (r["win_rate"] or 0.0) * 100,
            "roiEqPct": recent_roi_pct(r["week_roi"], r["mon_roi"]),   # recent HL ROI (matches score)
            "mainCoin": r["top_coin"],
        } for r in rows]
        return {"followLine": score100(line_native), "total": len(out), "tab": "dropped", "wallets": out}
    # Only the wallets we ACTUALLY follow: score above the follow line (the watchlist also holds many
    # lower-score actives we observe but don't copy). enabled+disabled both shown so the toggle works.
    cutoff7d = int((time.time() - 7 * 86400) * 1000)   # target's own round-trips closed in the last 7d
    rows = qall(db,
        "SELECT w.addr,w.rank,w.market_type,w.score,w.roi_equity,w.net_pnl,w.win_rate,w.top_coin,"
        "w.worst_single_loss_pct,w.grid,COALESCE(c.enabled,1) AS enabled,w.n_trades,"
        "pr.worst_loss_pct,pr.median_adds_per_ep,pr.active_days,pr.avg_notional,l.week_roi,l.mon_roi,"
        "(SELECT COUNT(*) FROM episode e WHERE e.addr=w.addr AND e.close_ms >= ?) AS closed_7d,"
        "(SELECT COUNT(*) FROM copy_position cp WHERE cp.addr=w.addr) AS follow_count,"
        "(SELECT COUNT(*) FROM copy_position cp WHERE cp.addr=w.addr AND cp.status!='open') AS closed_n,"
        "(SELECT COALESCE(SUM(realized_pnl),0) FROM copy_position cp WHERE cp.addr=w.addr AND cp.status!='open') AS realized,"
        "(SELECT COALESCE(SUM(unrealized_pnl),0) FROM copy_position cp WHERE cp.addr=w.addr AND cp.status='open') AS unreal "
        "FROM watchlist w "
        "LEFT JOIN target_controls c ON c.addr=w.addr "
        "LEFT JOIN profile pr ON pr.addr=w.addr "
        "LEFT JOIN leaderboard l ON l.addr=w.addr "
        "WHERE w.score >= ? ORDER BY w.rank", (cutoff7d, line_native))
    # Partition by the evidence floor (mirrors observer.load_targets) so the list == what we ACTUALLY copy:
    #   FOLLOWED (tab 'followed') = score≥line AND clears the floor → the real copy set, numbered 1..N by
    #                               follow position (NOT global watchlist rank — that confused users).
    #   OBSERVING (tab 'observing') = score≥line but thin-sample → tracked, not yet copied.
    def _held(r):
        return (r["n_trades"] or 0) < config.FOLLOW_MIN_TRADES or (r["active_days"] or 0) < config.FOLLOW_MIN_ACTIVE_DAYS
    foll = [r for r in rows if not _held(r)]
    obs = [r for r in rows if _held(r)]
    tab = (qs.get("tab", ["followed"]))[0]
    sel = obs if tab == "observing" else foll
    out = []
    for i, r in enumerate(sel[page * size:page * size + size]):
        grid = r["grid"]
        if grid is None:                       # COALESCE: derive from profile until scanner backfills
            grid = min((r["median_adds_per_ep"] or 0) / grid_max, 1.0)
        worst = r["worst_single_loss_pct"]
        if worst is None:
            worst = (r["worst_loss_pct"] or 0.0) * 100
        out.append({
            "evidenceHeld": tab == "observing",  # this tab's rows are the held-back set
            "followPos": (page * size + i + 1) if tab != "observing" else None,   # 1..N position in the copy set
            "address": r["addr"], "rank": r["rank"], "marketType": r["market_type"] or "crypto",
            "score": score100(r["score"] or 0.0),   # ROI shown = recent HL 收益/本金 (周+月),与评分 ROI 支柱同口径
            "roiEqPct": recent_roi_pct(r["week_roi"], r["mon_roi"]),
            "winRatePct": (r["win_rate"] or 0.0) * 100, "grid": round(grid, 3),
            "worstSingleLossPct": worst, "mainCoin": r["top_coin"],
            "followCount": r["follow_count"], "enabled": bool(r["enabled"]),
            "closed7d": r["closed_7d"],                            # target's OWN round-trips closed in 7d (活跃度)
            "closedN": r["closed_n"],                              # our forward (real copy) results
            "forwardNetPnl": (r["realized"] or 0) + (r["unreal"] or 0),   # PnL is the real verdict, not win%
            "trend": _wallet_trend(db, r["addr"]),
        })
    return {"followLine": score100(line_native), "tab": tab, "total": len(sel),
            "followed": len(foll), "observing": len(obs),
            "page": page, "size": size, "wallets": out}


def ep_wallet_detail(db, addr, qs=None):
    w = q1(db, "SELECT rank FROM watchlist WHERE addr=?", (addr,))
    # SCORED (historical 14d, the basis of the score) — from profile
    pr = q1(db, "SELECT score,win_rate,n_trades,market_type FROM profile WHERE addr=?", (addr,))
    # FORWARD (our real copy results)
    agg = q1(db, "SELECT COALESCE(SUM(realized_pnl),0) pnl, COUNT(*) n, "
                 "SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins "
                 "FROM copy_position WHERE addr=? AND status!='open'", (addr,))
    op = q1(db, "SELECT COUNT(*) n, COALESCE(SUM(unrealized_pnl),0) u "
                "FROM copy_position WHERE addr=? AND status='open'", (addr,))
    n = (agg["n"] if agg else 0) or 0
    win_n = (agg["wins"] if agg else 0) or 0
    realized = (agg["pnl"] if agg else 0.0) or 0.0
    open_u = (op["u"] if op else 0.0) or 0.0
    total_recs = (q1(db, "SELECT COUNT(*) c FROM copy_position WHERE addr=?", (addr,)) or {"c": 0})["c"]
    rp = max(0, int((qs.get("recPage", ["0"]))[0])) if qs else 0
    rs = min(50, max(1, int((qs.get("recSize", ["20"]))[0]))) if qs else 20
    recs = qall(db,
        "SELECT cp.pos_id,cp.coin,cp.side,cp.status,cp.realized_pnl,cp.unrealized_pnl,cp.opened_at,cp.closed_at,"
        "cp.entry_px,cp.mark_px,cp.leverage,cp.margin,cp.notional,cp.master_open_px,cp.add_count,"
        "(SELECT our_px FROM copy_action ca WHERE ca.pos_id=cp.pos_id AND ca.action='close' ORDER BY ca.act_id DESC LIMIT 1) AS exit_px "
        "FROM copy_position cp WHERE cp.addr=? ORDER BY cp.opened_at DESC LIMIT ? OFFSET ?",
        (addr, rs, rp * rs))
    return {
        "address": addr, "rank": (w["rank"] if w else None),
        "marketType": (pr["market_type"] if pr else None),
        "score": score100(pr["score"]) if pr else None,
        # 历史(评分依据)
        "scoredWinRatePct": (pr["win_rate"] * 100) if (pr and pr["win_rate"] is not None) else None,
        "scoredTrades": (pr["n_trades"] if pr else None),
        # 实盘(我们跟出来)
        "forwardWinRatePct": (win_n / n * 100) if n else None,
        "closedN": n, "winN": win_n, "lossN": n - win_n,
        "realizedPnl": realized, "openN": (op["n"] if op else 0), "openUnrealized": open_u,
        "netPnl": realized + open_u,
        "recordsTotal": total_recs, "recPage": rp, "recSize": rs,
        "records": [{
            "id": r["pos_id"], "coin": r["coin"], "side": r["side"], "status": r["status"],
            "pnl": (r["realized_pnl"] or 0.0) if r["status"] != "open" else (r["unrealized_pnl"] or 0.0),
            "openedAt": r["opened_at"], "closedAt": r["closed_at"],
            "entry": r["entry_px"], "exit": (r["exit_px"] if r["status"] != "open" else r["mark_px"]),
            "masterEntry": r["master_open_px"], "leverage": r["leverage"], "margin": r["margin"],
            "notional": r["notional"], "addCount": r["add_count"],
        } for r in recs],
    }


def ep_position_detail(db, pos_id):
    """Per-position fill-by-fill breakdown: every observed MASTER action (open/add/reduce/close) with its
    price/size, aligned with OUR response — followed (our px×qty) or NOT (a capped add we skipped shows as
    被限制). copy_action records the master's move even when we didn't follow (our_qty_delta=0)."""
    p = q1(db, "SELECT pos_id,addr,coin,side,status,entry_px,leverage,notional,size,rem_size,margin,"
               "master_open_px,master_leverage,master_margin,realized_pnl,unrealized_pnl,was_liq,was_stopped,"
               "add_count,opened_at,closed_at FROM copy_position WHERE pos_id=?", (pos_id,))
    if not p:
        return {"error": "not_found"}
    acts = qall(db, "SELECT ts,action,maker,master_px,master_sz_delta,master_pos_after,our_qty_delta,our_px,"
                    "realized_pnl,slippage_bps FROM copy_action WHERE pos_id=? ORDER BY ts,act_id", (pos_id,))
    ACT = {"open": "开仓", "add": "加仓", "reduce": "减仓", "close": "平仓"}
    fills, m_adds, skipped = [], 0, 0
    for a in acts:
        our_qty = abs(a["our_qty_delta"] or 0.0)
        followed = our_qty > 1e-9
        is_entry = a["action"] in ("open", "add")
        if a["action"] == "add":
            m_adds += 1
            if not followed:
                skipped += 1
        fills.append({
            "atSec": (a["ts"] or 0) / 1000.0, "action": a["action"], "actionLabel": ACT.get(a["action"], a["action"]),
            "maker": bool(a["maker"]),
            "masterPx": a["master_px"], "masterSz": abs(a["master_sz_delta"] or 0.0), "masterPosAfter": a["master_pos_after"],
            "followed": followed, "ourPx": a["our_px"] if followed else None, "ourSz": our_qty if followed else None,
            "skipped": is_entry and not followed,                 # a master entry/add we did NOT mirror (cap)
            "pnl": a["realized_pnl"] if a["action"] in ("reduce", "close") else None,
            "slippageBps": a["slippage_bps"],
        })
    close_type = "liq" if p["was_liq"] else ("stop" if p["was_stopped"] else "mirror")
    return {
        "id": p["pos_id"], "coin": p["coin"], "side": p["side"], "status": p["status"], "closeType": close_type,
        "ourEntry": p["entry_px"], "ourLeverage": p["leverage"], "ourNotional": p["notional"],
        "ourSize": p["size"], "ourRemSize": p["rem_size"], "ourMargin": p["margin"],
        "masterEntry": p["master_open_px"], "masterLeverage": p["master_leverage"],
        "masterNotional": (p["master_margin"] or 0.0) * (p["master_leverage"] or 0.0),
        "masterFinalPos": (acts[-1]["master_pos_after"] if acts else None),
        "realizedPnl": p["realized_pnl"], "unrealizedPnl": p["unrealized_pnl"],
        "ourAdds": p["add_count"], "masterAdds": m_adds, "skippedAdds": skipped,   # 我们跟了 (adds-skipped)/adds
        "openedAt": _iso_epoch(p["opened_at"]), "closedAt": _iso_epoch(p["closed_at"]),
        "fills": fills,
    }


# gate reason -> the 4 UI buckets (kept here so it's tweakable in one place)
_REJECT_BUCKETS = [
    ("不活跃 / 成交不足", {"inactive", "spot_dominant", "bot_frequency", "irregular"}),
    ("网格度过高", {"grid_dca"}),
    ("扛单 / 单笔大亏", {"blowup_loss", "not_profitable"}),
]


def ep_discovery(db):
    candidates = (q1(db, "SELECT COUNT(*) c FROM leaderboard WHERE is_candidate=1") or {"c": 0})["c"]
    active = (q1(db, "SELECT COUNT(*) c FROM profile WHERE status='active'") or {"c": 0})["c"]
    # funnel's final stage = wallets ABOVE the follow line (the ones we actually copy), NOT the whole
    # watchlist (which also holds many lower-score actives we only observe).
    line_native = params_mod.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE
    watchlist = _followed_count(db, line_native)   # funnel's final stage = wallets we actually copy
    # reject reasons -> buckets
    reason_rows = qall(db, "SELECT reason,COUNT(*) n FROM profile WHERE status='rejected' GROUP BY reason")
    counts = {row["reason"]: row["n"] for row in reason_rows}
    total_rej = sum(counts.values()) or 0
    buckets, used = [], set()
    for label, keys in _REJECT_BUCKETS:
        n = sum(counts.get(k, 0) for k in keys)
        used |= keys
        buckets.append([label, n])
    other = sum(v for k, v in counts.items() if k not in used)
    buckets.append(["其他", other])
    reject_reasons = [{"label": lbl, "pct": round(n / total_rej * 100) if total_rej else 0}
                      for lbl, n in buckets]
    # score histogram over scored profiles. X-axis anchored to the native score ceiling (v5 score is
    # native [0,1]; display = ×100) so the bins map onto the same 0–100 ruler as the wallet scores.
    scores = [r["score"] for r in qall(db,
              "SELECT score FROM profile WHERE score IS NOT NULL AND score>0")]
    follow_line = params_mod.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE
    nbins = 16
    hi = 1.0
    bins = [0] * nbins
    for sc in scores:
        idx = min(int(max(sc, 0.0) / hi * nbins), nbins - 1)
        bins[idx] += 1
    follow_idx = min(int(follow_line / hi * nbins), nbins - 1)
    last_scan = q1(db, "SELECT MAX(finished_at) m FROM scan_runs")
    return {"funnel": {"candidates": candidates, "active": active, "watchlist": watchlist},
            "rejectReasons": reject_reasons,
            "scoreHistogram": {"bins": bins, "followLineBinIndex": follow_idx},
            "scanner": _scanner_status(db),               # live rolling-scanner status for the page card
            "lastScanAt": (last_scan["m"] if last_scan else None)}


def ep_scan_runs(db, limit):
    rows = qall(db, "SELECT started_at,finished_at,candidates,added,retired,kept,rejected,n_active "
                    "FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,))
    return {"runs": [{"at": r["started_at"], "finishedAt": r["finished_at"],
                      "candidates": r["candidates"], "added": r["added"], "retired": r["retired"],
                      "kept": r["kept"], "rejected": r["rejected"], "active": r["n_active"]}
                     for r in rows]}


def ep_command(db, cmd_id):
    r = q1(db, "SELECT id,type,status,result_json,error,created_at,acked_at,done_at "
               "FROM commands WHERE id=?", (cmd_id,))
    if not r:
        return {"commandId": cmd_id, "status": "not_found"}
    return {"commandId": r["id"], "type": r["type"], "status": r["status"],
            "result": json.loads(r["result_json"]) if r["result_json"] else None,
            "error": r["error"], "createdAt": r["created_at"],
            "ackedAt": r["acked_at"], "doneAt": r["done_at"]}


def ep_scan_status(db):
    r = q1(db, "SELECT * FROM scan_progress WHERE id=1")
    if not r or (r["state"] or "idle") != "scanning":
        return {"state": "idle"}
    started = _iso_epoch(r["started_at"])
    elapsed = int(time.time() - started) if started else 0
    total, scanned, eta = r["candidates_total"] or 0, r["candidates_scanned"] or 0, r["eta_sec"] or 1200
    pct = round(scanned / total * 100) if total else min(99, round(elapsed / eta * 100))
    manual = bool(r["manual"]) if "manual" in r.keys() else True   # missing col (old db) → treat as manual (safe)
    return {"state": "scanning", "manual": manual, "startedAt": r["started_at"], "elapsedSec": elapsed, "etaSec": eta,
            "progressPct": pct, "candidatesScanned": scanned, "candidatesTotal": total, "stage": r["stage"]}


def ep_score_dist(db):
    """All watchlist (active) wallets' DISPLAY scores (0–100), sorted desc — lets the Settings UI show,
    live, how many wallets a given MIN_FOLLOW_SCORE would actually follow (the number is the real guide,
    not an abstract range). Tiny payload (~dozens of floats)."""
    scores = [round(score100(r["score"] or 0.0), 1)
              for r in qall(db, "SELECT score FROM watchlist ORDER BY score DESC")]
    return {"scores": scores, "total": len(scores)}


WRITABLE_LEVELS = {"green", "yellow", "blue"}     # black / display are read-only

# ── SSE live stream (replaces polling for the fast-changing bundle) ──
STREAM_MAX = 8                # cap concurrent stream connections (single-user; guards a reconnect storm)
STREAM_TICK = 1.0            # server-side read cadence; we push only on CHANGE (+ heartbeat)
STREAM_HEARTBEAT = 15.0
_stream_lock = threading.Lock()
_stream_clients = 0


def _fast_bundle(db):
    """The fast-changing slice pushed over SSE: overview (cards/ticker/system) + open positions.
    Slow data (wallets/discovery/params/scan-runs) stays on-demand GET."""
    return {"overview": ep_overview(db), "positions": ep_positions(db, {"status": ["open"]}),
            "serverTime": now_iso()}


def patch_params(db_path, category, updates):
    """Write UI param edits to the params table (the only place the dashboard writes besides commands).
    Rejects read-only levels. MIN_FOLLOW_SCORE arrives on the 0–100 ruler -> inverted to native."""
    db = rw_connect(db_path)
    try:
        out = {}
        for key, val in (updates or {}).items():
            row = db.execute("SELECT category,level,type FROM params WHERE key=?", (key,)).fetchone()
            if not row:
                continue
            if row["category"] != category:
                continue
            if row["level"] not in WRITABLE_LEVELS or row["type"] == "display":
                raise ValueError(f"{key} is read-only")
            stored = val
            if key == "MIN_FOLLOW_SCORE" and val is not None:
                stored = score_from100(val)                       # UI 0–100 -> native ~0–3
            sval = (None if stored is None else "true" if stored is True
                    else "false" if stored is False else str(stored))
            db.execute("UPDATE params SET value=?,updated_at=? WHERE key=?", (sval, now_iso(), key))
            out[key] = val
        db.commit()
        return out
    finally:
        db.close()


def ep_params(db):
    data = params_mod.get_all(db)
    # MIN_FOLLOW_SCORE is on the score ruler -> present it on the same 0–100 scale as wallet scores
    # (engine stores native ~0–3). The M4 PATCH must invert with score_from100() before writing.
    for pr in data.get("follow", []):
        if pr["key"] == "MIN_FOLLOW_SCORE":
            pr["value"] = score100(pr["value"])
            pr["default"] = score100(pr["default"])
            pr["scaled"] = True            # hint to the frontend: 0–100 display ruler
    return data


# ─────────────────────────────────────────────────────────────────────── http handler
def make_handler(db_path, auth, static_dir=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "hl-dashboard/0.1"

        def log_message(self, fmt, *a):            # quieter logs
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, default=float).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _envelope(self, data):
            self._send(200, {"data": data, "serverTime": now_iso()})

        def _authed(self):
            h = self.headers.get("Authorization", "")
            token = h[7:] if h.startswith("Bearer ") else None
            return auth.valid(token)

        def do_OPTIONS(self):
            self._send(204, {})

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/auth/login":
                body = self._read_json() or {}
                token, err = auth.login(body.get("username"), body.get("password"))
                if err:
                    code = 429 if err == "rate_limited" else 401
                    return self._send(code, {"error": err})
                return self._send(200, {"token": token,
                                        "expiresAt": _iso_ago(-TOKEN_TTL_S)})
            if path == "/api/commands":
                if not self._authed():
                    return self._send(401, {"error": "unauthorized"})
                body = self._read_json() or {}
                ctype = body.get("type")
                if ctype not in ALLOWED_COMMANDS:
                    return self._send(400, {"error": "bad_command_type", "detail": ctype})
                try:
                    if ctype in PROCESS_COMMANDS:            # dashboard executes these directly (procman)
                        cmd_id, status = exec_process_command(db_path, ctype)
                    else:                                    # soft commands: queued for the observer to consume
                        cmd_id, status = insert_command(db_path, ctype, body.get("payload"),
                                                        body.get("idempotencyKey"))
                    return self._send(202, {"commandId": cmd_id, "status": status})
                except Exception as e:  # noqa: BLE001
                    return self._send(500, {"error": "server_error", "detail": str(e)})
            return self._send(404, {"error": "not_found"})

        def do_PATCH(self):
            path = urlparse(self.path).path
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            if path.startswith("/api/params/"):
                cat = path.rsplit("/", 1)[1]
                if cat not in ("follow", "scanner"):
                    return self._send(400, {"error": "bad_category"})
                try:
                    updated = patch_params(db_path, cat, self._read_json() or {})
                    resp = {"updated": updated}
                    if cat == "scanner":
                        resp["pendingRescan"] = True            # changes need a rescan to take effect
                    return self._send(200, resp)
                except ValueError as e:
                    return self._send(422, {"error": str(e)})
                except Exception as e:  # noqa: BLE001
                    return self._send(500, {"error": "server_error", "detail": str(e)})
            return self._send(404, {"error": "not_found"})

        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return {}

        def do_GET(self):
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            if path in ("/", "/index.html") and static_dir:
                return self._serve_static("index.html")
            if not path.startswith("/api/"):
                if static_dir:
                    return self._serve_static(path.lstrip("/"))
                return self._send(404, {"error": "not_found"})
            if path == "/api/stream":
                # SSE: EventSource can't send an Authorization header -> token via query param.
                return self._serve_stream(qs.get("token", [None])[0])
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            db = ro_connect(db_path)
            try:
                if path == "/api/overview":
                    return self._envelope(ep_overview(db))
                if path == "/api/equity":
                    return self._envelope(ep_equity(db, qs.get("range", ["all"])[0]))
                if path == "/api/insights":
                    return self._envelope(ep_insights(db))
                if path == "/api/positions":
                    return self._envelope(ep_positions(db, qs))
                if path.startswith("/api/positions/"):
                    pid = path.rsplit("/", 1)[1]
                    if pid.isdigit():
                        return self._envelope(ep_position_detail(db, int(pid)))
                if path == "/api/wallets":
                    return self._envelope(ep_wallets(db, qs))
                if path.startswith("/api/wallets/"):
                    return self._envelope(ep_wallet_detail(db, path.rsplit("/", 1)[1], qs))
                if path == "/api/discovery":
                    return self._envelope(ep_discovery(db))
                if path == "/api/scan-runs":
                    return self._envelope(ep_scan_runs(db, int(qs.get("limit", [20])[0])))
                if path == "/api/params":
                    return self._envelope(ep_params(db))
                if path == "/api/scan-status":
                    return self._envelope(ep_scan_status(db))
                if path == "/api/score-dist":
                    return self._envelope(ep_score_dist(db))
                if path.startswith("/api/commands/"):
                    return self._envelope(ep_command(db, int(path.rsplit("/", 1)[1])))
                return self._send(404, {"error": "not_found"})
            except Exception as e:                          # noqa: BLE001 — never 500 the dashboard
                return self._send(500, {"error": "server_error", "detail": str(e)})
            finally:
                db.close()

        def _serve_stream(self, token):
            global _stream_clients
            if not auth.valid(token):
                return self._send(401, {"error": "unauthorized"})
            with _stream_lock:
                if _stream_clients >= STREAM_MAX:
                    return self._send(503, {"error": "too_many_streams"})
                _stream_clients += 1
            db = None
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")          # don't let a proxy buffer the stream
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                db = ro_connect(db_path)
                prev, last_hb = None, 0.0
                while True:
                    try:
                        body = json.dumps(_fast_bundle(db), default=float)
                    except Exception:  # noqa: BLE001 — a transient query error shouldn't drop the stream
                        body = None
                    now = time.time()
                    if body is not None and body != prev:
                        self.wfile.write(b"data: " + body.encode() + b"\n\n")
                        self.wfile.flush()
                        prev, last_hb = body, now
                    elif now - last_hb >= STREAM_HEARTBEAT:
                        self.wfile.write(b": ping\n\n")              # keep-alive comment
                        self.wfile.flush()
                        last_hb = now
                    time.sleep(STREAM_TICK)
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                return                                               # client went away
            finally:
                if db is not None:
                    db.close()
                with _stream_lock:
                    _stream_clients -= 1

        def _serve_static(self, rel):
            import mimetypes
            from pathlib import Path
            base = Path(static_dir).resolve()
            target = (base / rel).resolve()
            if not str(target).startswith(str(base)) or not target.is_file():
                target = base / "index.html"                # SPA fallback
                if not target.is_file():
                    return self._send(404, {"error": "not_found"})
            if target.name == "index.html":
                # Inject a cache-busting ?v=<asset mtime> onto the app.jsx/app.css refs. babel fetches
                # app.jsx via a JS-initiated XHR, which a browser hard-refresh does NOT bypass — so a plain
                # no-store isn't enough to shake a stale copy. A fresh URL per deploy forces the new build.
                html = target.read_text()
                try:
                    ver = int(max((base / f).stat().st_mtime for f in ("app.jsx", "app.css") if (base / f).is_file()))
                except ValueError:
                    ver = 0
                html = html.replace("/app.jsx", f"/app.jsx?v={ver}").replace("/app.css", f"/app.css?v={ver}")
                data = html.encode()
            else:
                data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            # Only index.html is uncached (it's tiny and carries the ?v=<mtime> version stamp). app.jsx/app.css
            # are busted by that stamp on deploy, and /vendor/ is immutable → cache them ALL hard, so a normal
            # refresh re-fetches nothing but index.html (no re-downloading assets every time).
            if target.name == "index.html":
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
            else:
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(db_path, host="127.0.0.1", port=8787, static_dir=None):
    auth = Auth()
    procman.reconcile(db_path)                    # drop stale pidfiles; re-attach to a still-live observer
    procman.start_auto_scan_ticker(db_path)       # 24h auto-scan now lives here (no separate supervisor daemon)
    handler = make_handler(db_path, auth, static_dir)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"dashboard API on http://{host}:{port}  (db={db_path}, static={static_dir or '-'})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
