"""Execution backends. One interface — run a command, push a file — over two transports:
SSHExecutor (paramiko, a remote VPS) and LocalExecutor (subprocess, this machine).

The pipeline/ops layers are written against `Executor` and never import paramiko, so a local deploy
is the same steps with a different executor. `run()` streams stdout+stderr line-by-line to an optional
callback so the UI can show live progress, and returns (exit_code, full_text).
"""
from __future__ import annotations

import io
import subprocess


class ExecResult:
    def __init__(self, code, out):
        self.code = code
        self.out = out
    @property
    def ok(self):
        return self.code == 0


class Executor:
    def run(self, cmd, on_line=None, timeout=None):        # -> ExecResult
        raise NotImplementedError
    def put_text(self, remote_path, text, mode=0o644):
        raise NotImplementedError
    def exists(self, path):
        return self.run(f"test -e {_q(path)}").ok
    def close(self):
        pass


def _q(s):
    """Single-quote a path/arg for the remote shell."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def ensure_paramiko(emit=None):
    """Make paramiko importable, AUTO-INSTALLING it on first VPS use so a non-technical operator never
    has to. Returns True if available. Local deploys never call this (no SSH dep). Tries a normal then a
    --user pip install into the launcher's own interpreter; guides a manual install if both fail."""
    import importlib
    try:
        importlib.import_module("paramiko")
        return True
    except ImportError:
        pass
    import subprocess
    import sys
    if emit:
        emit("首次远程部署:自动安装 SSH 依赖 paramiko(约十几秒,只此一次)…")
    for extra in ([], ["--user"]):
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "paramiko", *extra],
                           check=True, timeout=240)
        except Exception:  # noqa: BLE001
            continue
        importlib.invalidate_caches()
        try:
            importlib.import_module("paramiko")
            if emit:
                emit("✓ paramiko 安装完成")
            return True
        except ImportError:
            continue
    if emit:
        emit("✗ paramiko 自动安装失败 — 请在终端运行:pip3 install paramiko,再重试")
    return False


# ───────────────────────────────────────────────────────────── remote (paramiko)
class SSHExecutor(Executor):
    def __init__(self, host, user="root", password=None, key_filename=None, port=22, timeout=25):
        import paramiko                                    # lazy — LocalExecutor needs no dep
        self.host, self.user = host, user
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(host, port=port, username=user, password=password,
                             key_filename=key_filename, timeout=timeout,
                             allow_agent=False, look_for_keys=False)
        self._sftp = None

    def run(self, cmd, on_line=None, timeout=None):
        chan = self._client.get_transport().open_session()
        if timeout:
            chan.settimeout(timeout)
        chan.exec_command(cmd)
        buf, pending = io.StringIO(), ""
        while True:
            got = False
            while chan.recv_ready():
                pending += chan.recv(4096).decode("utf-8", "replace"); got = True
            while chan.recv_stderr_ready():
                pending += chan.recv_stderr(4096).decode("utf-8", "replace"); got = True
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                buf.write(line + "\n")
                if on_line:
                    on_line(line)
            if chan.exit_status_ready() and not got and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
        if pending:                                        # trailing line w/o newline
            buf.write(pending)
            if on_line:
                on_line(pending)
        return ExecResult(chan.recv_exit_status(), buf.getvalue())

    def _sftp_conn(self):
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        return self._sftp

    def put_text(self, remote_path, text, mode=0o644):
        sftp = self._sftp_conn()
        with sftp.file(remote_path, "w") as f:
            f.write(text)
        sftp.chmod(remote_path, mode)

    def close(self):
        try:
            if self._sftp:
                self._sftp.close()
        finally:
            self._client.close()


# ───────────────────────────────────────────────────────────── local (subprocess)
class LocalExecutor(Executor):
    """Runs on this machine — for a Linux box the operator controls locally, or dev testing.
    (systemd/caddy steps still require Linux; on macOS this is only useful for the run-processes path.)"""
    host, user = "localhost", None

    def run(self, cmd, on_line=None, timeout=None):
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
        buf = io.StringIO()
        for line in p.stdout:
            buf.write(line)
            if on_line:
                on_line(line.rstrip("\n"))
        p.wait(timeout=timeout)
        return ExecResult(p.returncode, buf.getvalue())

    def put_text(self, remote_path, text, mode=0o644):
        import os
        os.makedirs(os.path.dirname(remote_path) or ".", exist_ok=True)
        with open(remote_path, "w") as f:
            f.write(text)
        os.chmod(remote_path, mode)
