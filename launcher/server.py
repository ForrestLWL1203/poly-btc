"""Launcher HTTP server — serves the React UI (web/) and a small JSON+SSE API that drives deploys
and day-2 ops. Binds 127.0.0.1 only (a local single-operator tool holding VPS access); no auth layer
by design. Deploys stream progress over SSE; ops are request/response and authenticate to the VPS
with the launcher keypair, so no VPS password is ever sent after the first deploy.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from core import ops, targets
from core.model import DeployConfig
from core.pipeline import DeployRunner

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_RUNNERS = {}          # deployId -> DeployRunner
_MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json"}


def _cfg_from_target(t, extra=None):
    """Build a DeployConfig from a saved target (+ optional per-request extras like passwords)."""
    d = dict(t or {})
    d.update(extra or {})
    key_path_input = d.get("key_path") if d.get("mode", "vps") == "vps" else None
    kp, pub = targets.keypair(key_path_input)
    return DeployConfig(
        mode=d.get("mode", "vps"), host=d.get("host", ""), user=d.get("user", "root"),
        password=d.get("password"), ssh_port=int(d.get("ssh_port", 22) or 22),
        key_path=kp if d.get("mode", "vps") == "vps" else None, pubkey=pub,
        app_dir=d.get("app_dir") or "/root/poly-btc", branch=d.get("branch") or "main",
        port=int(d.get("port", 8810) or 8810), domain=(d.get("domain") or None),
        dash_user=d.get("dash_user") or "admin", dash_password=d.get("dash_password") or "",
    )


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/targets":
            kp, pub = targets.keypair()
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            return self._json(200, {"targets": targets.load(), "pubkey": pub,
                                    "keyPath": kp, "repoRoot": repo_root})
        if path.startswith("/api/deploy/") and path.endswith("/events"):
            return self._stream_deploy(path.split("/")[3])
        return self._serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read()
        try:
            if path == "/api/targets/save":
                return self._json(200, {"target": targets.save(body)})
            if path == "/api/targets/delete":
                targets.remove(body.get("id"))
                return self._json(200, {"ok": True})
            if path == "/api/deploy":
                cfg = _cfg_from_target(targets.get(body.get("id")) or {}, body)
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
    targets.keypair()                     # ensure the keypair exists at startup
    httpd = ThreadingHTTPServer((host, port), H)
    print(f"  launcher → http://{host}:{port}")
    httpd.serve_forever()
