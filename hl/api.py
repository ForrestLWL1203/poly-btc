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
from .api_common import iso_epoch as _iso_epoch
from .api_common import q1, qall, score100, score_from100
from .api_discovery import ep_discovery, ep_scan_runs, ep_scan_status, ep_score_dist
from .api_discovery import followed_count as _followed_count
from .api_discovery import scanner_status as _scanner_status
from .api_positions import ep_position_detail, ep_positions
from .api_wallets import ep_wallet_detail, ep_wallets
from .util import now_iso

# ─────────────────────────────────────────────────────────────────────────── auth
TOKEN_TTL_S = 24 * 3600

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


def _iso_ago(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


# ─────────────────────────────────────────────────────────────────────── endpoints
def ep_shadow(db):
    """Taker vs maker-shadow A/B — two isolated paper books, SAME strategy, only execution differs
    (taker fills on every target fill at the taker fee; maker fills only on the target's maker fills at
    the maker fee). equity = balance + Σ open unrealized."""
    def book(acct, pos):
        br = db.execute(f"SELECT balance FROM {acct} WHERE id=1").fetchone()
        bal = float(br["balance"]) if br else 0.0
        o = db.execute(f"SELECT COALESCE(SUM(unrealized_pnl),0) u, COUNT(*) n FROM {pos} WHERE status='open'").fetchone()
        c = db.execute(f"SELECT COALESCE(SUM(realized_pnl),0) r, COUNT(*) n, "
                       f"SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) w FROM {pos} WHERE status!='open'").fetchone()
        upnl = float(o["u"] or 0.0)
        return {"balance": bal, "unrealized": upnl, "equity": bal + upnl, "realized": float(c["r"] or 0.0),
                "openN": o["n"], "closedN": c["n"],
                "winRatePct": ((c["w"] or 0) / c["n"] * 100.0) if c["n"] else 0.0}
    taker = book("copy_account", "copy_position")
    maker = book("shadow_account", "shadow_position")
    mpos = [{"addr": r["addr"], "coin": r["coin"], "side": r["side"], "entry": r["entry_px"],
             "lev": r["leverage"], "margin": r["margin"], "mark": r["mark_px"],
             "upnl": r["unrealized_pnl"], "addN": r["add_count"], "openedAt": r["opened_at"]}
            for r in db.execute("SELECT addr,coin,side,entry_px,leverage,margin,mark_px,unrealized_pnl,"
                                "add_count,opened_at FROM shadow_position WHERE status='open' ORDER BY opened_at DESC").fetchall()]
    return {"enabled": bool(config.SHADOW_MAKER_ENABLED), "taker": taker, "maker": maker, "makerPositions": mpos}


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
        closed = q1(db, "SELECT COUNT(*) n, SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins "
                        "FROM copy_position WHERE status!='open'") or {"n": 0, "wins": 0}
        closed_n = closed["n"] or 0
        win_rate = ((closed["wins"] or 0) / closed_n) if closed_n else 0.0
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


def ep_command(db, cmd_id):
    r = q1(db, "SELECT id,type,status,result_json,error,created_at,acked_at,done_at "
               "FROM commands WHERE id=?", (cmd_id,))
    if not r:
        return {"commandId": cmd_id, "status": "not_found"}
    return {"commandId": r["id"], "type": r["type"], "status": r["status"],
            "result": json.loads(r["result_json"]) if r["result_json"] else None,
            "error": r["error"], "createdAt": r["created_at"],
            "ackedAt": r["acked_at"], "doneAt": r["done_at"]}


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


def reset_params(db_path, category):
    """恢复默认配置: force-overwrite params back to config defaults. category 'all' = both tabs.
    Same single-writer contract as patch_params (writes only the params table)."""
    db = rw_connect(db_path)
    try:
        cat = None if category == "all" else category
        n = params_mod.reset_defaults(db, cat)
        return n
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
            if path.startswith("/api/params/") and path.endswith("/reset"):
                if not self._authed():
                    return self._send(401, {"error": "unauthorized"})
                cat = path.split("/")[3]                      # /api/params/{cat}/reset
                if cat not in ("follow", "scanner", "all"):
                    return self._send(400, {"error": "bad_category"})
                try:
                    n = reset_params(db_path, cat)
                    resp = {"reset": n}
                    if cat in ("scanner", "all"):
                        resp["pendingRescan"] = True           # scanner defaults need a rescan to bite
                    return self._send(200, resp)
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
                if path == "/api/shadow":
                    return self._envelope(ep_shadow(db))
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
                # Inject a cache-busting ?v=<asset mtime> onto compiled assets. They are served immutable, so
                # a fresh URL per deploy is what forces phones and desktop browsers to load the new UI.
                import re
                html = target.read_text()
                assets = ("app.js", "app.css", "app.jsx")
                try:
                    ver = int(max((base / f).stat().st_mtime for f in assets if (base / f).is_file()))
                except ValueError:
                    ver = 0
                for asset in assets:
                    html = re.sub(rf"/{re.escape(asset)}(?:\?v=[^\"']*)?", f"/{asset}?v={ver}", html)
                data = html.encode()
            else:
                data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            # Only index.html is uncached (it's tiny and carries the ?v=<mtime> version stamp). app.js/app.css
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
