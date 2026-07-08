"""Day-2 operations against an already-deployed target: status, start/stop/restart, logs, code
update, restore-default-params. Each opens a fresh connection (key auth on a VPS), acts, closes —
stateless and safe to call ad-hoc from the ops console. Code update is `git reset --hard` + restart;
no build needed on the target because web/app.js is committed pre-compiled.
"""
from . import services
from .pipeline import connect


def _conn(cfg):
    ex = connect(cfg)
    return ex, services.for_mode(ex, cfg)


def status(cfg):
    ex, svc = _conn(cfg)
    try:
        st = svc.status()
        commit = ex.run(f"cd {cfg.app_dir} && git log -1 --format='%h %s' 2>/dev/null").out.strip()
        code = ex.run(f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{cfg.port}/ "
                      f"--max-time 6 2>/dev/null || true").out.strip()[-3:]
        return {"services": st, "commit": commit,
                "dashboardHttp": code, "url": (f"https://{cfg.domain}" if cfg.domain
                                               else f"http://127.0.0.1:{cfg.port}")}
    finally:
        ex.close()


def action(cfg, op, unit):
    """op ∈ start|stop|restart, unit ∈ dashboard|observe|scan|timer."""
    ex, svc = _conn(cfg)
    try:
        fn = {"start": svc.start, "stop": svc.stop, "restart": svc.restart}[op]
        r = fn(unit)
        return {"ok": getattr(r, "ok", True), "out": getattr(r, "out", "")}
    finally:
        ex.close()


def logs(cfg, unit, lines=120):
    ex, svc = _conn(cfg)
    try:
        return {"unit": unit, "log": svc.logs(unit, lines)}
    finally:
        ex.close()


def update(cfg):
    """Pull latest code + restart. On the VPS: hard-reset to origin (deploy is the source of truth);
    restart the dashboard, and the observer too if it was live (so copying picks up new code)."""
    ex, svc = _conn(cfg)
    try:
        out = []
        if cfg.mode == "vps":
            r = ex.run(f"cd {cfg.app_dir} && git fetch -q origin && git reset --hard origin/{cfg.branch} "
                       f"&& git log -1 --format='%h %s'")
        else:
            r = ex.run(f"cd {cfg.app_dir} && git pull -q --ff-only && git log -1 --format='%h %s'")
        out.append(r.out.strip())
        observing = svc.status().get("observe") in ("active", "running")
        svc.restart("dashboard")
        out.append("dashboard 已重启")
        if observing:
            svc.restart("observe")
            out.append("observer 已重启(跟单中,已载入新代码)")
        return {"ok": r.ok, "detail": "\n".join(out)}
    finally:
        ex.close()


def reset_params(cfg, category=None):
    """恢复默认参数 on the target — force-overwrite the params table to config defaults, then enqueue
    a follow-param reload command when relevant. category None = all; 'follow'/'scanner' = one tab."""
    ex, _ = _conn(cfg)
    try:
        cat = "None" if not category else repr(category)
        py = ("import json; "
              "from hl import storage, params; "
              "from hl.util import now_iso; "
              "db=storage.connect('data/hl.db', storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA); "
              f"cat={cat}; "
              "n=params.reset_defaults(db, cat); "
              "reload_needed = cat in (None, 'follow'); "
              "rescan_needed = cat in (None, 'scanner'); "
              "if reload_needed: "
              " db.execute('INSERT INTO commands (type,payload_json,owner,status,created_at) "
              "VALUES (?,?,?,\\'pending\\',?)', "
              "('reload_params', json.dumps({'by':'launcher_reset_params','category':cat or 'all'}), "
              "'launcher', now_iso())); "
              " db.commit(); "
              "print('reset', n, 'reload', int(reload_needed), 'rescan', int(rescan_needed))")
        r = ex.run(f'cd {cfg.app_dir} && .venv/bin/python -c "{py}"')
        return {"ok": r.ok, "out": r.out.strip()}
    finally:
        ex.close()
