"""Rendered config artifacts pushed to the VPS during deploy — systemd units + Caddyfile.

These mirror the units that ran on the reference host verbatim (only paths/port/domain are
parameterized). Keeping them here as templates — not shell heredocs buried in steps — makes the
deployed system auditable and lets `ops.update` diff/re-push a unit without touching the pipeline.
"""

# The three long-lived services + the daily scan (oneshot) and its timer. `observe` is the copy
# engine — enabled but NOT started at deploy (the operator starts copy-trading from the dashboard).
UNITS = ("hl-dashboard", "hl-observe", "hl-scan.service", "hl-scan.timer")


def dashboard_unit(app_dir, py, db, port, host="127.0.0.1"):
    return f"""[Unit]
Description=HL copy-trade dashboard (read-only API + static UI)
After=network.target

[Service]
Type=simple
WorkingDirectory={app_dir}
ExecStart={py} hl_dashboard.py --db {db} --static web --host {host} --port {port}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


def observe_unit(app_dir, py, db):
    return f"""[Unit]
Description=Hyperliquid copy-trade observer + paper sim
After=network-online.target
Wants=network-online.target

[Service]
Environment=PYTHONUNBUFFERED=1
WorkingDirectory={app_dir}
ExecStart={py} {app_dir}/hl_observe.py --db {db} observe
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


def scan_service(app_dir, py, db, days=14, scan_interval=8):
    return f"""[Unit]
Description=Hyperliquid copy-trade daily incremental / weekly full scanner
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=PYTHONUNBUFFERED=1
WorkingDirectory={app_dir}
ExecStart={py} {app_dir}/hl_discover.py --db {db} scan --days {days} --scan-interval {scan_interval}
TimeoutStartSec=14h
ExecStopPost={py} {app_dir}/hl_discover.py --db {db} repair-watchlist
"""


def scan_timer(on_calendar="*-*-* 04:00:00"):
    return f"""[Unit]
Description=Run HL scanner daily (incremental; weekly full refresh selected by scanner)

[Timer]
OnCalendar={on_calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""


def caddyfile(domain, port):
    """Reverse-proxy the domain to the local dashboard; Caddy auto-provisions + renews TLS.
    Requires the domain's DNS A-record to already point at this host (checked in the verify step)."""
    return f"""{domain} {{
    reverse_proxy 127.0.0.1:{port}
}}
"""


def render_all(cfg):
    """cfg: a DeployConfig-like object. Returns {remote_path: file_text} for every unit + caddyfile.
    caddyfile is omitted when no domain is set (dashboard is then reached via IP:port / SSH tunnel)."""
    out = {
        "/etc/systemd/system/hl-dashboard.service": dashboard_unit(cfg.app_dir, cfg.py, cfg.db, cfg.port),
        "/etc/systemd/system/hl-observe.service": observe_unit(cfg.app_dir, cfg.py, cfg.db),
        "/etc/systemd/system/hl-scan.service": scan_service(cfg.app_dir, cfg.py, cfg.db,
                                                            cfg.scan_days, cfg.scan_interval),
        "/etc/systemd/system/hl-scan.timer": scan_timer(cfg.scan_calendar),
    }
    if cfg.domain:
        out["/etc/caddy/Caddyfile"] = caddyfile(cfg.domain, cfg.port)
    return out
