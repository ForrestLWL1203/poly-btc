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
from .util import now_iso

# ─────────────────────────────────────────────────────────────────────────── auth
TOKEN_TTL_S = 24 * 3600

# Score display scale. The v3 quality score is bounded [0, SCORE_RAR_CAP] (quality≤RAR_CAP, and
# survival·health≤1), so we present it on a 0–100 ruler for the UI (engine/DB stay native — no
# re-scoring). MIN_FOLLOW_SCORE=1.2 -> 40. Applied to wallet scores, the follow line, the histogram
# axis, AND the MIN_FOLLOW_SCORE setting so the operator reads ONE ruler everywhere.
RAW_SCORE_MAX = config.SCORE_RAR_CAP


def score100(raw):
    """Native v3 score -> 0–100 display."""
    if raw is None:
        return None
    return round(min(max(raw, 0.0) / RAW_SCORE_MAX, 1.0) * 100, 1)


def score_from100(disp):
    """Inverse of score100 — for the M4 PATCH of MIN_FOLLOW_SCORE (UI 0–100 -> native before write)."""
    if disp is None:
        return None
    return disp / 100.0 * RAW_SCORE_MAX


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
ALLOWED_COMMANDS = {"pause", "resume", "close_position", "close_all", "wallet_toggle", "rescan", "patch_params"}
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
    _line = params_mod.get(db, "MIN_FOLLOW_SCORE", 0.9) or 0.9   # "被跟" = wallets above the follow line
    wl = q1(db, "SELECT COUNT(*) c FROM watchlist WHERE score>=?", (_line,))

    def _stale(row):
        if not row or not row["heartbeat_at"]:
            return False
        hb = _iso_epoch(row["heartbeat_at"])
        return bool(hb and (time.time() - hb) > PROC_STALE_SEC)

    base["system"] = {
        "observer": (obs["state"] if obs else "running"),
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
                        "cp.addr,w.rank AS wrank FROM copy_position cp "
                        "LEFT JOIN watchlist w ON w.addr=cp.addr WHERE " + " AND ".join(where) +
                        " ORDER BY cp.closed_at DESC LIMIT 100", tuple(args))   # most recent 100 (UI paginates 25/page)
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
                        "walletRank": r["wrank"],   # wrank None = 已脱榜
                        "entry": r["entry_px"], "closePx": close_px,
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
    out, float_total = [], 0.0
    for r in rows:
        entry = r["entry_px"] or 0.0
        mark = r["mark_px"] if r["mark_px"] else entry           # null until Observer persists (M2)
        margin = r["margin"] or 0.0
        upnl = r["unrealized_pnl"] if r["unrealized_pnl"] is not None else 0.0
        float_total += upnl
        liq = r["liq_px"]
        # Distance-to-liquidation as a consistent NEGATIVE buffer regardless of side: the % adverse
        # move from mark to liq_px (0 = at liquidation). -1.5 = 1.5% away (danger), -30 = comfortable.
        liq_dist = (-abs(liq / mark - 1) * 100) if (liq and mark) else None
        out.append({
            "id": f"pos_{r['pos_id']}", "coin": r["coin"], "marketType": r["mtype"] or "crypto",
            "side": r["side"], "entry": entry, "leverage": r["leverage"],
            "notional": r["notional"] or 0.0, "mark": mark,
            "unrealizedPnl": upnl,
            "unrealizedPctOfMargin": (upnl / margin * 100) if margin else 0.0,
            "wallet": r["addr"], "walletRank": r["wrank"],
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
    line_native = params_mod.get(db, "MIN_FOLLOW_SCORE", 0.9) or 0.9   # native ~0–3 scale
    grid_max = params_mod.get(db, "grid_max_adds", 5) or 5
    page = max(0, int((qs.get("page", ["0"]))[0]))
    size = min(100, max(1, int((qs.get("size", ["30"]))[0])))
    # DROPPED tab: wallets that WERE on the follow line (follow_history stamped) but are now below it or
    # no longer active — "recently demoted for poor performance". Recovers automatically if it climbs back.
    if (qs.get("tab", ["followed"]))[0] == "dropped":
        rows = qall(db,
            "SELECT fh.addr,fh.last_followed_at,fh.last_followed_score,p.score,p.status,p.reason,"
            "p.market_type,p.win_rate,p.roi_equity,p.roi_total,p.top_coin,w.rank AS rank "
            "FROM follow_history fh JOIN profile p ON p.addr=fh.addr "
            "LEFT JOIN watchlist w ON w.addr=fh.addr "
            "WHERE NOT (p.status='active' AND p.score >= ?) ORDER BY fh.last_followed_at DESC", (line_native,))
        out = [{
            "address": r["addr"], "rank": r["rank"], "marketType": r["market_type"] or "crypto",
            "score": score100(r["score"] or 0.0), "lastFollowedScore": score100(r["last_followed_score"] or 0.0),
            "lastFollowedAt": _iso_epoch(r["last_followed_at"]),
            "dropReason": ("掉出评分线" if r["status"] == "active" else {"inactive": "失活", "blowup_loss": "扛单爆亏",
                "spot_hedge": "对冲盘", "not_profitable": "转亏", "irregular": "低频", "grid_dca": "网格",
                "bot_frequency": "高频", "hft_uncopyable": "高频", "spot_dominant": "现货为主"}.get(r["reason"], r["reason"] or "淘汰")),
            "winRatePct": (r["win_rate"] or 0.0) * 100, "roiEqPct": (r["roi_equity"] or 0.0) * 100,
            "mainCoin": r["top_coin"],
        } for r in rows]
        return {"followLine": score100(line_native), "total": len(out), "tab": "dropped", "wallets": out}
    # Only the wallets we ACTUALLY follow: score above the follow line (the watchlist also holds many
    # lower-score actives we observe but don't copy). enabled+disabled both shown so the toggle works.
    cutoff7d = int((time.time() - 7 * 86400) * 1000)   # target's own round-trips closed in the last 7d
    rows = qall(db,
        "SELECT w.addr,w.rank,w.market_type,w.score,w.roi_equity,w.win_rate,w.top_coin,"
        "w.worst_single_loss_pct,w.grid,COALESCE(c.enabled,1) AS enabled,"
        "pr.worst_loss_pct,pr.median_adds_per_ep,"
        "(SELECT COUNT(*) FROM episode e WHERE e.addr=w.addr AND e.close_ms >= ?) AS closed_7d,"
        "(SELECT COUNT(*) FROM copy_position cp WHERE cp.addr=w.addr) AS follow_count,"
        "(SELECT COUNT(*) FROM copy_position cp WHERE cp.addr=w.addr AND cp.status!='open') AS closed_n,"
        "(SELECT COALESCE(SUM(realized_pnl),0) FROM copy_position cp WHERE cp.addr=w.addr AND cp.status!='open') AS realized,"
        "(SELECT COALESCE(SUM(unrealized_pnl),0) FROM copy_position cp WHERE cp.addr=w.addr AND cp.status='open') AS unreal "
        "FROM watchlist w "
        "LEFT JOIN target_controls c ON c.addr=w.addr "
        "LEFT JOIN profile pr ON pr.addr=w.addr "
        "WHERE w.score >= ? ORDER BY w.rank", (cutoff7d, line_native))
    total = len(rows)
    out = []
    for r in rows[page * size:page * size + size]:
        grid = r["grid"]
        if grid is None:                       # COALESCE: derive from profile until scanner backfills
            grid = min((r["median_adds_per_ep"] or 0) / grid_max, 1.0)
        worst = r["worst_single_loss_pct"]
        if worst is None:
            worst = (r["worst_loss_pct"] or 0.0) * 100
        out.append({
            "address": r["addr"], "rank": r["rank"], "marketType": r["market_type"] or "crypto",
            "score": score100(r["score"] or 0.0), "roiEqPct": (r["roi_equity"] or 0.0) * 100,
            "winRatePct": (r["win_rate"] or 0.0) * 100, "grid": round(grid, 3),
            "worstSingleLossPct": worst, "mainCoin": r["top_coin"],
            "followCount": r["follow_count"], "enabled": bool(r["enabled"]),
            "closed7d": r["closed_7d"],                            # target's OWN round-trips closed in 7d (活跃度)
            "closedN": r["closed_n"],                              # our forward (real copy) results
            "forwardNetPnl": (r["realized"] or 0) + (r["unreal"] or 0),   # PnL is the real verdict, not win%
            "trend": _wallet_trend(db, r["addr"]),
        })
    return {"followLine": score100(line_native), "total": total, "page": page, "size": size, "wallets": out}


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
    line_native = params_mod.get(db, "MIN_FOLLOW_SCORE", 0.9) or 0.9
    watchlist = (q1(db, "SELECT COUNT(*) c FROM watchlist WHERE score>=?",
                    (line_native,)) or {"c": 0})["c"]
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
    # score histogram over scored profiles. X-axis anchored to the score ceiling (RAW_SCORE_MAX) so
    # the bins map onto the same 0–100 ruler as the wallet scores, with a stable follow-line position.
    scores = [r["score"] for r in qall(db,
              "SELECT score FROM profile WHERE score IS NOT NULL AND score>0")]
    follow_line = params_mod.get(db, "MIN_FOLLOW_SCORE", 0.9) or 0.9
    nbins = 16
    hi = RAW_SCORE_MAX or 1.0
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
    r = q1(db, "SELECT state,started_at,stage,candidates_scanned,candidates_total,eta_sec FROM scan_progress WHERE id=1")
    if not r or (r["state"] or "idle") != "scanning":
        return {"state": "idle"}
    started = _iso_epoch(r["started_at"])
    elapsed = int(time.time() - started) if started else 0
    total, scanned, eta = r["candidates_total"] or 0, r["candidates_scanned"] or 0, r["eta_sec"] or 1200
    pct = round(scanned / total * 100) if total else min(99, round(elapsed / eta * 100))
    return {"state": "scanning", "startedAt": r["started_at"], "elapsedSec": elapsed, "etaSec": eta,
            "progressPct": pct, "candidatesScanned": scanned, "candidatesTotal": total, "stage": r["stage"]}


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
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(db_path, host="127.0.0.1", port=8787, static_dir=None):
    auth = Auth()
    handler = make_handler(db_path, auth, static_dir)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"dashboard API on http://{host}:{port}  (db={db_path}, static={static_dir or '-'})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
