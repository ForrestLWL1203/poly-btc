"""The deployment pipeline as an ordered list of idempotent, re-runnable steps.

Each step is (id, title, fn, modes). fn(ctx) emits progress lines and raises StepError on failure.
Steps are filtered by cfg.mode ("vps" vs "local"), so one linear definition serves both paths — a
local deploy simply skips ssh_key / apt / caddy. Every step is safe to re-run (the pipeline can
resume after a fix): clones become pulls, venvs are reused, unit files are overwritten in place.
"""
from . import services
from .ssh import _q


class StepError(Exception):
    pass


class Ctx:
    def __init__(self, ex, cfg, emit):
        self.ex, self.cfg, self.emit = ex, cfg, emit

    def sh(self, cmd, check=True, timeout=None):
        r = self.ex.run(cmd, on_line=self.emit, timeout=timeout)
        if check and not r.ok:
            raise StepError(f"命令失败(exit {r.code}): {cmd.splitlines()[0][:80]}")
        return r


# ─────────────────────────────────────────────────────────────────── steps
def probe(ctx):
    if ctx.cfg.mode == "vps":
        ctx.sh("uname -a; (cat /etc/os-release | grep -E '^PRETTY_NAME=') || true")
    r = ctx.sh("python3 --version && git --version", check=False)
    if not r.ok:
        raise StepError("目标机缺少 python3 / git" + ("(VPS 会在下一步安装)" if ctx.cfg.mode == "vps" else ""))


def ssh_key(ctx):
    if not ctx.cfg.pubkey:
        ctx.emit("无公钥可安装,跳过(将继续用密码)"); return
    key = ctx.cfg.pubkey.strip()
    if "\n" in key or not key.startswith(("ssh-ed25519 ", "ecdsa-sha2-", "ssh-rsa ")):
        raise StepError("SSH 公钥格式无效")
    qkey = _q(key)
    ctx.sh(f'umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; '
           f'grep -qF {qkey} ~/.ssh/authorized_keys || printf "%s\\n" {qkey} >> ~/.ssh/authorized_keys; '
           f'chmod 600 ~/.ssh/authorized_keys')
    ctx.emit("✓ 公钥已安装,之后可免密登录")


def base_pkgs(ctx):
    ctx.emit("apt 安装 python/git/curl …(首次可能较慢)")
    ctx.sh("export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && "
           "apt-get install -y -qq python3 python3-venv python3-pip git curl gnupg", timeout=600)
    if ctx.cfg.domain:
        ctx.emit("安装 caddy(域名 HTTPS 反代)…")
        ctx.sh("command -v caddy >/dev/null 2>&1 || { "
               "apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https; "
               "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | "
               "  gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg; "
               "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | "
               "  tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null; "
               "apt-get update -qq && apt-get install -y -qq caddy; }", timeout=600)


def repo(ctx):
    c = ctx.cfg
    app_dir, branch, repo_url = _q(c.app_dir), _q(c.branch), _q(c.repo_url)
    if c.mode == "vps":
        ctx.sh(f"if [ -d {app_dir}/.git ]; then cd {app_dir} && git fetch -q origin && "
               f"git reset --hard {_q('origin/' + c.branch)}; else "
               f"git clone -q --branch {branch} {repo_url} {app_dir}; fi", timeout=300)
    else:                                          # local: never hard-reset (may hold uncommitted work)
        r = ctx.sh(f"if [ -d {app_dir}/.git ]; then cd {app_dir} && "
                   f"git rev-parse --short HEAD; else git clone -q --branch {branch} "
                   f"{repo_url} {app_dir} && echo cloned; fi", timeout=300)
        ctx.emit(f"代码就绪 @ {r.out.strip().splitlines()[-1] if r.out.strip() else c.app_dir}")


def venv(ctx):
    c = ctx.cfg
    ctx.emit("创建/复用 venv 并安装依赖…")
    ctx.sh(f"cd {_q(c.app_dir)} && [ -d .venv ] || python3 -m venv .venv && "
           f".venv/bin/pip install -q --upgrade pip && "
           f".venv/bin/pip install -q -r requirements.txt", timeout=300)


def config_step(ctx):
    c = ctx.cfg
    if not c.dash_password:
        raise StepError("必须设置 dashboard 密码")
    ctx.sh("mkdir -p " + " ".join(_q(f"{c.app_dir}/{p}") for p in ("data", "secret", "data/run")))
    ctx.ex.put_text(f"{c.app_dir}/secret/dash_user", (c.dash_user or "admin") + "\n", 0o600)
    ctx.ex.put_text(f"{c.app_dir}/secret/dash_password", (c.dash_password or "") + "\n", 0o600)
    ctx.emit(f"✓ dashboard 账号写入 secret/(用户 {c.dash_user or 'admin'})")


def services_step(ctx):
    svc = services.for_mode(ctx.ex, ctx.cfg)
    svc.install(ctx.emit)


def caddy_step(ctx):
    c = ctx.cfg
    if not c.domain:
        ctx.emit("未配置域名,跳过 HTTPS(用 IP:端口 或 SSH 隧道访问)"); return
    # ACME (Let's Encrypt) validates by connecting to THIS host on 80/443 from outside. If ufw is active
    # but only permits 22 (common VPS default), the challenge times out and NO cert is issued (dashboard
    # then only answers on 127.0.0.1). Open 80/443 first — keep 22 so we never lock ourselves out.
    ctx.sh("if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q 'Status: active'; then "
           "ufw allow 22/tcp >/dev/null 2>&1; ufw allow 80/tcp >/dev/null 2>&1; "
           "ufw allow 443/tcp >/dev/null 2>&1; echo '✓ ufw 放行 22/80/443(ACME 签证书需要)'; "
           "else echo 'ufw 未启用,无需放行'; fi", check=False)
    from . import templates
    ctx.ex.put_text("/etc/caddy/Caddyfile", templates.caddyfile(c.domain, c.port))
    ctx.emit(f"写入 Caddyfile({c.domain} → 127.0.0.1:{c.port})")
    ctx.sh("systemctl reload caddy 2>/dev/null || systemctl restart caddy", check=False)
    ctx.emit("⚠ 确认域名 DNS A 记录已指向本机 IP,Caddy 才能签发证书")


def verify(ctx):
    c = ctx.cfg
    svc = services.for_mode(ctx.ex, ctx.cfg)
    st = svc.status()
    ctx.emit("服务状态: " + " · ".join(f"{k}={v}" for k, v in st.items()))
    ctx.emit("探测 dashboard 响应(启动需几秒,最多等 15s)…")
    # POLL, don't single-shot: a freshly-spawned dashboard takes ~0.5-3s to bind the port (systemd
    # start / nohup both return before it's listening), so an immediate curl races and false-fails.
    r = ctx.sh(
        f"for i in $(seq 1 30); do "
        f"c=$(curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{c.port}/ --max-time 3 2>/dev/null); "
        f'case "$c" in 200|401|301|302) echo "OK $c"; exit 0;; esac; sleep 0.5; done; '
        f'echo "FAIL ${{c:-000}}"; exit 1', check=False)
    if "OK" in r.out:
        ctx.emit(f"✓ dashboard 响应正常(HTTP {r.out.split()[-1]})")
    else:
        raise StepError("dashboard 未在超时内响应 — 查看 dashboard 日志(运维台→日志)")
    if c.mode == "vps":
        ctx.emit("实测到 Hyperliquid API 的延迟…")
        ctx.sh("curl -s -o /dev/null -w 'HL API: %{time_connect}s connect\\n' "
               "https://api.hyperliquid.xyz/info --max-time 8 || true", check=False)


STEPS = [
    ("probe",    "连接与环境探测",   probe,        ("vps", "local")),
    ("ssh_key",  "安装 SSH 公钥(免密)", ssh_key,   ("vps",)),
    ("base_pkgs","安装系统依赖",     base_pkgs,     ("vps",)),
    ("repo",     "拉取代码",         repo,          ("vps", "local")),
    ("venv",     "Python 环境与依赖", venv,         ("vps", "local")),
    ("config",   "写入配置与密码",   config_step,   ("vps", "local")),
    ("services", "安装并启动服务",   services_step, ("vps", "local")),
    ("caddy",    "域名与 HTTPS",     caddy_step,    ("vps",)),
    ("verify",   "验证部署",         verify,        ("vps", "local")),
]


def steps_for(mode):
    return [s for s in STEPS if mode in s[3]]
