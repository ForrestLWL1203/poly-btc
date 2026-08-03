"""Rendered config artifacts pushed to the VPS during deploy — systemd units + Caddyfile.

These mirror the units that ran on the reference host verbatim (only paths/port/domain are
parameterized). Keeping them here as templates — not shell heredocs buried in steps — makes the
deployed system auditable and lets `ops.update` diff/re-push a unit without touching the pipeline.
"""

# Long-lived services plus the two mutually-exclusive scanner jobs and their timers. `observe` is the copy
# engine — installed but NOT started at deploy (the operator starts copy-trading from the dashboard).
UNITS = (
    "hl-dashboard", "hl-observe", "hl-execution-control.service", "hl-scan.service", "hl-scan.timer",
    "hl-challenger-refresh.service", "hl-challenger-refresh.timer",
    "hl-finalize-resume.service", "hl-finalize-resume.timer",
)


def dashboard_unit(app_dir, py, db, port, host="127.0.0.1"):
    return f"""[Unit]
Description=HL copy-trade dashboard (read-only API + static UI)
After=network.target

[Service]
Type=simple
Environment=HL_CREDENTIAL_PUBLIC_KEY_FILE={app_dir}/secret/credential-wrap-public.pem
InaccessiblePaths={app_dir}/secret/credential-wrap-private.pem
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=full
UMask=0077
WorkingDirectory={app_dir}
ExecStart={py} -m dashboard.server --db {db} --static dashboard/web --host {host} --port {port}
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
LoadCredential=hl-credential-wrap-private.pem:{app_dir}/secret/credential-wrap-private.pem
Environment=HL_CREDENTIAL_PRIVATE_KEY_FILE=%d/hl-credential-wrap-private.pem
WorkingDirectory={app_dir}
ExecStart={py} -m hyper.cli.observe --db {db} observe
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=multi-user.target
"""


def execution_control_unit(app_dir, py, db):
    return f"""[Unit]
Description=Hyperliquid protected execution control worker
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=PYTHONUNBUFFERED=1
LoadCredential=hl-credential-wrap-private.pem:{app_dir}/secret/credential-wrap-private.pem
Environment=HL_CREDENTIAL_PRIVATE_KEY_FILE=%d/hl-credential-wrap-private.pem
WorkingDirectory={app_dir}
ExecStart={py} -m hyper.cli.execution_control --db {db} process-pending
TimeoutStartSec=90
UMask=0077
"""


def scan_service(app_dir, py, db, days=14, scan_interval=8):
    return f"""[Unit]
Description=Hyperliquid copy-trade incremental scanner / weekly candidate refresh
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=PYTHONUNBUFFERED=1
WorkingDirectory={app_dir}
ExecStart={py} -m hyper.cli.discover --db {db} scan --days {days} --scan-interval {scan_interval}
TimeoutStartSec=infinity
MemoryHigh=1100M
MemoryMax=1400M
MemorySwapMax=512M
ExecStopPost={py} -m hyper.cli.discover --db {db} repair-watchlist
ExecStopPost={py} -m hyper.cli.discover --db {db} storage-maintenance
"""


def scan_timer(on_calendar="Mon,Thu *-*-* 04:00:00 Asia/Shanghai"):
    return f"""[Unit]
Description=Run HL scanner twice weekly (alternating 3/4-day evidence refresh)

[Timer]
OnCalendar={on_calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""


def challenger_refresh_service(app_dir, py, db, scan_interval=8):
    return f"""[Unit]
Description=HL frozen Challenger evidence refresh
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=PYTHONUNBUFFERED=1
WorkingDirectory={app_dir}
ExecStart={py} -m hyper.cli.discover --db {db} challenger-refresh --scan-interval {scan_interval}
TimeoutStartSec=infinity
MemoryHigh=1100M
MemoryMax=1400M
MemorySwapMax=512M
ExecStopPost={py} -m hyper.cli.discover --db {db} storage-maintenance
"""


def challenger_refresh_timer(
    on_calendar="Tue,Wed,Fri,Sat,Sun *-*-* 04:00:00 Asia/Shanghai",
):
    return f"""[Unit]
Description=Refresh frozen HL Challengers on non-full-scan days

[Timer]
OnCalendar={on_calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""


def finalize_resume_service(app_dir, py, db):
    return f"""[Unit]
Description=Resume resource-deferred HL Core formation
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=PYTHONUNBUFFERED=1
WorkingDirectory={app_dir}
ExecStart={py} -m hyper.cli.discover --db {db} finalize-profiled --if-ready
TimeoutStartSec=infinity
MemoryHigh=1100M
MemoryMax=1400M
MemorySwapMax=512M
"""


def finalize_resume_timer():
    return """[Unit]
Description=Retry deferred HL Core formation until atomic publication

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
RandomizedDelaySec=30
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
        "/etc/systemd/system/hl-execution-control.service": execution_control_unit(
            cfg.app_dir, cfg.py, cfg.db,
        ),
        "/etc/systemd/system/hl-scan.service": scan_service(cfg.app_dir, cfg.py, cfg.db,
                                                            cfg.scan_days, cfg.scan_interval),
        "/etc/systemd/system/hl-scan.timer": scan_timer(cfg.scan_calendar),
        "/etc/systemd/system/hl-challenger-refresh.service": challenger_refresh_service(
            cfg.app_dir, cfg.py, cfg.db, cfg.scan_interval,
        ),
        "/etc/systemd/system/hl-challenger-refresh.timer": challenger_refresh_timer(
            cfg.challenger_calendar,
        ),
        "/etc/systemd/system/hl-finalize-resume.service": finalize_resume_service(
            cfg.app_dir, cfg.py, cfg.db,
        ),
        "/etc/systemd/system/hl-finalize-resume.timer": finalize_resume_timer(),
    }
    if cfg.domain:
        out["/etc/caddy/Caddyfile"] = caddyfile(cfg.domain, cfg.port)
    return out
