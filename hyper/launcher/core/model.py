"""Deploy configuration — the single object every step + template reads from."""
from __future__ import annotations

import dataclasses

REPO_URL = "https://github.com/ForrestLWL1203/poly-btc.git"   # public → HTTPS clone, no credentials
ACCOUNT_MONITOR_MODES = frozenset({"rest_only", "ws_primary"})


def account_monitor_mode(value):
    """Return a validated account monitor mode, defaulting old configs to REST."""
    mode = str(value or "rest_only").strip()
    if mode not in ACCOUNT_MONITOR_MODES:
        raise ValueError("account_monitor_mode must be rest_only or ws_primary")
    return mode


@dataclasses.dataclass
class DeployConfig:
    # mode: "vps" (remote Linux, systemd + caddy) | "local" (this machine, detached processes)
    mode: str = "vps"
    # connection (vps only)
    host: str = ""
    user: str = "root"
    password: str | None = None          # first-login password (dropped after key install)
    ssh_port: int = 22
    key_path: str | None = None          # local private key (launcher-managed by default, or operator-supplied)
    pubkey: str | None = None            # local public key text (installed into authorized_keys)
    host_fingerprint: str | None = None   # expected OpenSSH SHA256 host-key fingerprint on first connect

    # target layout
    app_dir: str = "/root/poly-btc"
    repo_url: str = REPO_URL
    branch: str = "main"

    # dashboard
    port: int = 8810                     # local dashboard port (behind caddy)
    domain: str | None = None            # e.g. dashboard.example.com — None => IP:port / tunnel only
    dash_user: str = "admin"
    dash_password: str = ""

    # scanner cadence
    scan_days: int = 14
    scan_interval: int = 8
    # Stable membership matters more than a daily beauty contest. Monday/Thursday gives a predictable
    # alternating 3/4-day evidence refresh while live Observer risk controls remain continuous.
    scan_calendar: str = "Mon,Thu *-*-* 04:00:00 Asia/Shanghai"
    challenger_calendar: str = "Tue,Wed,Fri,Sat,Sun *-*-* 04:00:00 Asia/Shanghai"

    # Live-account monitoring is deliberately opt-in.  Old saved targets omit this field and must
    # therefore continue rendering the established REST-only observer service after an update.
    account_monitor_mode: str = "rest_only"

    @property
    def py(self):
        return f"{self.app_dir}/.venv/bin/python"

    @property
    def db(self):
        return f"{self.app_dir}/data/hl.db"

    def redacted(self):
        """Dict for logging/UI — secrets masked."""
        d = dataclasses.asdict(self)
        for k in ("password", "dash_password"):
            if d.get(k):
                d[k] = "••••••"
        d["pubkey"] = bool(d.get("pubkey"))
        return d
