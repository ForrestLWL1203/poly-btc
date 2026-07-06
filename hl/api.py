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

from . import procman
from .api_commands import ALLOWED_COMMANDS, PROCESS_COMMANDS, ep_command, exec_process_command, insert_command
from .api_discovery import ep_discovery, ep_scan_runs, ep_scan_status, ep_score_dist
from .api_discovery import scanner_status as _scanner_status
from .api_overview import ep_equity, ep_insights, ep_overview, ep_shadow
from .api_params import ep_params, patch_params, reset_params
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


def _iso_ago(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


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
