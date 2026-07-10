"""Launcher HTTP server — serves the React UI (web/) and a small JSON+SSE API that drives deploys
and day-2 ops. It is loopback-only and protects its API with a per-process SameSite session cookie,
strict Host/Origin checks, and JSON-only writes. Deploys stream progress over SSE; ops authenticate
to the VPS with the launcher keypair, so no VPS password is sent after the first deploy.
"""
import json
import ipaddress
import os
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    from .core import ops, targets
    from .core.model import DeployConfig
    from .core.pipeline import DeployRunner
except ImportError:  # direct `python launcher/launcher.py`
    from core import ops, targets
    from core.model import DeployConfig
    from core.pipeline import DeployRunner

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_RUNNERS = {}          # deployId -> DeployRunner
_MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json"}
_SESSION_TOKEN = secrets.token_urlsafe(32)
_MAX_BODY = 1024 * 1024
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
_SAFE_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_SAFE_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")


def _loopback_name(value):
    host = (value or "").strip().lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_cfg(d, require_dashboard_password=False):
    mode = d.get("mode", "vps")
    if mode not in {"vps", "local"}:
        raise ValueError("mode must be vps or local")
    app_dir = d.get("app_dir") or "/root/poly-btc"
    branch = d.get("branch") or "main"
    user = d.get("user") or "root"
    if not _SAFE_PATH.fullmatch(app_dir) or ".." in app_dir.split("/"):
        raise ValueError("invalid app_dir")
    if not _SAFE_BRANCH.fullmatch(branch) or branch.startswith("-") or ".." in branch.split("/"):
        raise ValueError("invalid branch")
    if not _SAFE_NAME.fullmatch(user):
        raise ValueError("invalid SSH user")
    for key in ("ssh_port", "port"):
        val = int(d.get(key, 22 if key == "ssh_port" else 8810) or (22 if key == "ssh_port" else 8810))
        if not 1 <= val <= 65535:
            raise ValueError(f"invalid {key}")
    domain = (d.get("domain") or "").strip()
    if domain and not _SAFE_DOMAIN.fullmatch(domain):
        raise ValueError("invalid domain")
    fp = (d.get("host_fingerprint") or "").strip()
    if fp and not _SAFE_FINGERPRINT.fullmatch(fp):
        raise ValueError("invalid SSH host fingerprint")
    if mode == "vps" and not (d.get("host") or "").strip():
        raise ValueError("VPS host is required")
    dash_user = d.get("dash_user") or "admin"
    if not _SAFE_NAME.fullmatch(dash_user):
        raise ValueError("invalid dashboard user")
    if require_dashboard_password and not str(d.get("dash_password") or ""):
        raise ValueError("dashboard password is required")


def _cfg_from_target(t, extra=None, require_dashboard_password=False):
    """Build a DeployConfig from a saved target (+ optional per-request extras like passwords)."""
    d = dict(t or {})
    d.update(extra or {})
    _validate_cfg(d, require_dashboard_password=require_dashboard_password)
    key_path_input = d.get("key_path") if d.get("mode", "vps") == "vps" else None
    kp, pub = targets.keypair(key_path_input)
    return DeployConfig(
        mode=d.get("mode", "vps"), host=d.get("host", ""), user=d.get("user", "root"),
        password=d.get("password"), ssh_port=int(d.get("ssh_port", 22) or 22),
        key_path=kp if d.get("mode", "vps") == "vps" else None, pubkey=pub,
        host_fingerprint=(d.get("host_fingerprint") or None),
        app_dir=d.get("app_dir") or "/root/poly-btc", branch=d.get("branch") or "main",
        port=int(d.get("port", 8810) or 8810), domain=(d.get("domain") or None),
        dash_user=d.get("dash_user") or "admin", dash_password=d.get("dash_password") or "",
    )


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _valid_host(self):
        raw = self.headers.get("Host", "")
        try:
            return _loopback_name(urlparse("//" + raw).hostname)
        except ValueError:
            return False

    def _valid_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlparse(origin)
            request_host = self.headers.get("Host", "").lower()
            return (parsed.scheme == "http" and _loopback_name(parsed.hostname)
                    and parsed.netloc.lower() == request_host)
        except ValueError:
            return False

    def _authed(self):
        cookies = self.headers.get("Cookie", "")
        token = None
        for item in cookies.split(";"):
            k, sep, v = item.strip().partition("=")
            if sep and k == "launcher_session":
                token = v
                break
        return bool(token) and secrets.compare_digest(token, _SESSION_TOKEN)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, code, obj):
        self.close_connection = True
        return self._json(code, obj)

    def _read(self):
        n = int(self.headers.get("Content-Length", 0))
        if n < 0 or n > _MAX_BODY:
            raise ValueError("request body too large")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON body") from exc

    # ── static ──
    def _serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        fp = os.path.normpath(os.path.join(WEB, rel))
        if not fp.startswith(WEB) or not os.path.isfile(fp):
            return self._json(404, {"error": "not_found"})
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(os.path.splitext(fp)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        if os.path.basename(fp) == "index.html":
            self.send_header("Set-Cookie", f"launcher_session={_SESSION_TOKEN}; Path=/; HttpOnly; SameSite=Strict")
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._valid_host():
            return self._json(403, {"error": "invalid_host"})
        path = urlparse(self.path).path
        if path == "/api/targets":
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            kp, pub = targets.keypair()
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            return self._json(200, {"targets": targets.load(), "pubkey": pub,
                                    "keyPath": kp, "repoRoot": repo_root})
        if path.startswith("/api/deploy/") and path.endswith("/events"):
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            return self._stream_deploy(path.split("/")[3])
        return self._serve_static(path)

    def do_POST(self):
        if not self._valid_host() or not self._valid_origin():
            return self._reject(403, {"error": "forbidden_origin"})
        if not self._authed():
            return self._reject(401, {"error": "unauthorized"})
        if self.headers.get_content_type() != "application/json":
            return self._reject(415, {"error": "application_json_required"})
        path = urlparse(self.path).path
        try:
            body = self._read()
        except ValueError as exc:
            return self._reject(400, {"error": str(exc)})
        try:
            if path == "/api/targets/save":
                fp = (body.get("host_fingerprint") or "").strip()
                if fp and not _SAFE_FINGERPRINT.fullmatch(fp):
                    raise ValueError("invalid SSH host fingerprint")
                return self._json(200, {"target": targets.save(body)})
            if path == "/api/targets/delete":
                targets.remove(body.get("id"))
                return self._json(200, {"ok": True})
            if path == "/api/deploy":
                cfg = _cfg_from_target(targets.get(body.get("id")) or {}, body,
                                       require_dashboard_password=True)
                did = f"d{len(_RUNNERS)+1}"
                _RUNNERS[did] = DeployRunner(cfg).start()
                # persist connection metadata (never secrets) so the ops console can find it later
                if cfg.mode == "vps" or cfg.mode == "local":
                    targets.save({**body, "mode": cfg.mode})
                return self._json(200, {"deployId": did})
            # ── day-2 ops (authenticate with the keypair; no password needed) ──
            if path.startswith("/api/ops/"):
                op = path.split("/")[3]
                cfg = _cfg_from_target(targets.get(body.get("id")) or body)
                if op == "status":
                    return self._json(200, ops.status(cfg))
                if op == "action":
                    return self._json(200, ops.action(cfg, body.get("op"), body.get("unit")))
                if op == "logs":
                    return self._json(200, ops.logs(cfg, body.get("unit"), int(body.get("lines", 120))))
                if op == "update":
                    return self._json(200, ops.update(cfg))
                if op == "reset-params":
                    return self._json(200, ops.reset_params(cfg, body.get("category")))
            return self._json(404, {"error": "not_found"})
        except Exception as e:  # noqa: BLE001 — surface any op failure as JSON, never 500-crash the UI
            return self._json(200, {"ok": False, "error": str(e)})

    # ── SSE deploy stream ──
    def _stream_deploy(self, did):
        runner = _RUNNERS.get(did)
        if not runner:
            return self._json(404, {"error": "unknown_deploy"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for ev in runner.events():
                self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                # On a successful VPS deploy the ssh_key step installed our pubkey → mark the target
                # passwordless so the UI can show it (and ops authenticate with the key from now on).
                if ev.get("type") == "end" and ev.get("ok") and runner.cfg.mode == "vps":
                    targets.save({"id": f"vps:{runner.cfg.host}", "keyInstalled": True})
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _RUNNERS.pop(did, None)


def serve(port=8799, host="127.0.0.1"):
    if not _loopback_name(host):
        raise ValueError("launcher may only bind to a loopback host")
    targets.keypair()                     # ensure the keypair exists at startup
    httpd = ThreadingHTTPServer((host, port), H)
    print(f"  launcher → http://{host}:{port}")
    httpd.serve_forever()
