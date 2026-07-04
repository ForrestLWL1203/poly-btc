"""Deploy runner — executes the step list on a background thread and exposes an event stream.

Steps run synchronously but emit progress through a thread-safe queue, so the HTTP layer can drain
`events()` and forward each as an SSE line. One connection is opened up front (password on a first
deploy, key thereafter) and reused for every step, then closed.
"""
import queue
import threading

from .ssh import SSHExecutor, LocalExecutor, ensure_paramiko
from .steps import Ctx, StepError, steps_for


def connect(cfg):
    if cfg.mode == "local":
        return LocalExecutor()
    # Prefer key if we have one that works; else password (first deploy). key_filename+password can
    # both be passed to paramiko — it tries key then password — but we keep it explicit per what's set.
    return SSHExecutor(cfg.host, cfg.user, password=cfg.password,
                       key_filename=cfg.key_path, port=cfg.ssh_port)


class DeployRunner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.q = queue.Queue()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()
        return self

    def _put(self, ev):
        self.q.put(ev)

    def _run(self):
        steps = steps_for(self.cfg.mode)
        # VPS mode shows a leading "准备依赖" step that auto-installs paramiko (runs locally, before we
        # can construct the SSH executor) so the operator never pre-installs anything.
        display = ([{"id": "deps", "title": "准备依赖"}] if self.cfg.mode == "vps" else []) \
            + [{"id": s[0], "title": s[1]} for s in steps]
        self._put({"type": "begin", "mode": self.cfg.mode, "steps": display})
        if self.cfg.mode == "vps":
            self._put({"type": "step", "id": "deps", "status": "running"})
            if not ensure_paramiko(lambda l: self._put({"type": "log", "step": "deps", "line": l})):
                self._put({"type": "step", "id": "deps", "status": "error", "error": "paramiko 安装失败"})
                self._put({"type": "end", "ok": False, "failed": "deps"})
                return
            self._put({"type": "step", "id": "deps", "status": "done"})
        try:
            ex = connect(self.cfg)
        except Exception as e:  # noqa: BLE001
            self._put({"type": "log", "step": "_connect", "line": f"连接失败: {e}"})
            self._put({"type": "end", "ok": False, "failed": "_connect"})
            return
        try:
            for sid, title, fn, _modes in steps:
                self._put({"type": "step", "id": sid, "title": title, "status": "running"})
                try:
                    ctx = Ctx(ex, self.cfg,
                              lambda line, sid=sid: self._put({"type": "log", "step": sid, "line": line}))
                    fn(ctx)
                    self._put({"type": "step", "id": sid, "status": "done"})
                except StepError as e:
                    self._put({"type": "step", "id": sid, "status": "error", "error": str(e)})
                    self._put({"type": "end", "ok": False, "failed": sid})
                    return
                except Exception as e:  # noqa: BLE001 — unexpected; surface but don't crash the server
                    self._put({"type": "step", "id": sid, "status": "error", "error": repr(e)})
                    self._put({"type": "end", "ok": False, "failed": sid})
                    return
            self._put({"type": "end", "ok": True,
                       "url": (f"https://{self.cfg.domain}" if self.cfg.domain
                               else f"http://127.0.0.1:{self.cfg.port}")})
        finally:
            try:
                ex.close()
            except Exception:  # noqa: BLE001
                pass

    def events(self):
        """Blocking generator of event dicts; ends after the terminal 'end' event."""
        while True:
            ev = self.q.get()
            yield ev
            if ev.get("type") == "end":
                break
