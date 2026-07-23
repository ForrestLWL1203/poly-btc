"""Service supervision — one interface, two backends.

VPS  (SystemdServices): dashboard常开 / scan.timer周一周四 / observe安装但禁用开机自启.
Local (LocalServices):  the launcher directly runs only the DASHBOARD (detached, pidfile). The
observer + scan are driven from inside the dashboard by its own procman, which — since the earlier
procman change — spawns them locally when systemd is absent. So locally the launcher owns the
dashboard; the dashboard owns copy-trading. Status/logs still surface all three via procman pidfiles.
"""
from . import templates
from .ssh import _q

SYSTEMD_UNITS = {"dashboard": "hl-dashboard.service", "observe": "hl-observe.service",
                 "scan": "hl-scan.service", "timer": "hl-scan.timer"}
# procman pidfile basenames (data/run/<name>.pid|.log) — used by the local backend
PID_NAMES = {"dashboard": "dashboard", "observe": "observer", "scan": "scan"}


def for_mode(ex, cfg):
    return SystemdServices(ex, cfg) if cfg.mode == "vps" else LocalServices(ex, cfg)


class SystemdServices:
    def __init__(self, ex, cfg):
        self.ex, self.cfg = ex, cfg

    def sync_units(self, emit=None):
        """Refresh unit definitions without changing which optional workers are running."""
        emit = emit or (lambda _message: None)
        for path, text in templates.render_all(self.cfg).items():
            if "/systemd/" in path:
                self.ex.put_text(path, text)
                emit(f"写入 {path}")
        self.ex.run("systemctl daemon-reload", on_line=emit)

    def install(self, emit):
        self.sync_units(emit)
        emit("启用 + 启动 dashboard(常开)…")
        self.ex.run("systemctl enable --now hl-dashboard.service", on_line=emit)
        emit("启用 scan 定时器(每周一/四 04:00，间隔3/4天)…")
        self.ex.run("systemctl enable --now hl-scan.timer", on_line=emit)
        emit("禁用 observe 开机自启(仍可由 dashboard 手动启动)…")
        self.ex.run("systemctl disable hl-observe.service", on_line=emit)

    def start(self, unit):   return self.ex.run(f"systemctl start {SYSTEMD_UNITS[unit]}")
    def stop(self, unit):    return self.ex.run(f"systemctl stop {SYSTEMD_UNITS[unit]}")
    def restart(self, unit): return self.ex.run(f"systemctl restart {SYSTEMD_UNITS[unit]}")

    def status(self):
        out = {}
        for k, u in SYSTEMD_UNITS.items():
            out[k] = (self.ex.run(f"systemctl is-active {u}").out.strip() or "unknown")
        return out

    def logs(self, unit, lines=80):
        return self.ex.run(f"journalctl -u {SYSTEMD_UNITS[unit]} -n {lines} -o cat --no-pager").out


class LocalServices:
    def __init__(self, ex, cfg):
        self.ex, self.cfg = ex, cfg
        self.rd = f"{cfg.app_dir}/data/run"

    def install(self, emit):
        emit("本地无 systemd — 直接后台启动 dashboard 进程…")
        self.start("dashboard", emit)

    def sync_units(self, emit=None):
        """Local processes have no persisted service definitions to refresh."""
        return None

    def start(self, unit, emit=None):
        if unit != "dashboard":                    # observe/scan 走 dashboard 内的 procman(本地 spawn)
            return self.ex.run("true")             # no-op: use the dashboard's 启动跟单/扫描 buttons
        pidfile = _q(f"{self.rd}/dashboard.pid")
        logfile = _q(f"{self.rd}/dashboard.log")
        cmd = (f"cd {_q(self.cfg.app_dir)} && mkdir -p data/run && "
               f"if [ -f {pidfile} ] && kill -0 $(cat {pidfile}) 2>/dev/null; "
               f"then echo 'dashboard 已在运行'; else "
               f"nohup {_q(self.cfg.py)} -m dashboard.server --db data/hl.db --static dashboard/web "
               f"--host 127.0.0.1 --port {int(self.cfg.port)} >> {logfile} 2>&1 & "
               f"echo $! > {pidfile}; echo 'dashboard 已启动 pid='$(cat {pidfile}); fi")
        return self.ex.run(cmd, on_line=emit)

    def stop(self, unit):
        name = PID_NAMES.get(unit, unit)
        pidfile = _q(f"{self.rd}/{name}.pid")
        return self.ex.run(f"[ -f {pidfile} ] && kill $(cat {pidfile}) 2>/dev/null; "
                           f"rm -f {pidfile}; true")

    def restart(self, unit):
        self.stop(unit)
        return self.start(unit)

    def status(self):
        out = {}
        for k, name in PID_NAMES.items():
            r = self.ex.run(f"cat {_q(f'{self.rd}/{name}.pid')} 2>/dev/null")
            pid = r.out.strip()
            out[k] = "active" if (pid and self.ex.run(f"kill -0 {pid} 2>/dev/null").ok) else "inactive"
        out["timer"] = "n/a"                        # no daily timer locally (manual scans)
        return out

    def logs(self, unit, lines=80):
        name = PID_NAMES.get(unit, unit)
        return self.ex.run(f"tail -n {int(lines)} {_q(f'{self.rd}/{name}.log')} 2>/dev/null").out
