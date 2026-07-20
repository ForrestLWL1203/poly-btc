import json
import http.client
import base64
import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import api
from dashboard.api import commands as api_commands
from hyper.discovery import scanner
from hyper.launcher import server as launcher_server
from hyper.launcher.core import services
from hyper.launcher.core.model import DeployConfig
from hyper.launcher.core.ssh import SSHExecutor


class ReviewFixTests(unittest.TestCase):
    def _commands_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TABLE commands ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,payload_json TEXT,idempotency_key TEXT UNIQUE,"
            "owner TEXT,status TEXT,created_at TEXT,acked_at TEXT,done_at TEXT,result_json TEXT,error TEXT)"
        )
        db.commit()
        db.close()
        return path

    def test_process_rescan_preserves_full_payload(self):
        path = self._commands_db()
        try:
            with patch.object(api_commands.procman, "start_scan", return_value={"started": True}) as start:
                cmd_id, status = api_commands.exec_process_command(path, "rescan", {"full": True})
            db = sqlite3.connect(path)
            payload = db.execute("SELECT payload_json FROM commands WHERE id=?", (cmd_id,)).fetchone()[0]
            db.close()
            self.assertEqual("pending", status)
            self.assertEqual({"full": True}, json.loads(payload))
            start.assert_called_once_with(path, full=True)
        finally:
            os.remove(path)

    def test_process_scan_stop_is_resolved_inline(self):
        path = self._commands_db()
        try:
            with patch.object(api_commands.procman, "stop_scan", return_value={"stopped": True}) as stop:
                cmd_id, status = api_commands.exec_process_command(path, "scan_stop", {})
            db = sqlite3.connect(path)
            row = db.execute("SELECT type,status FROM commands WHERE id=?", (cmd_id,)).fetchone()
            db.close()
            self.assertEqual("done", status)
            self.assertEqual(("scan_stop", "done"), row)
            stop.assert_called_once_with(path)
        finally:
            os.remove(path)

    def test_incremental_run_does_not_claim_midrun_full_request(self):
        path = self._commands_db()
        try:
            db = sqlite3.connect(path)
            cur = db.execute(
                "INSERT INTO commands(type,payload_json,owner,status,created_at) VALUES(?,?,?,?,?)",
                ("rescan", json.dumps({"full": True}), "dashboard", "pending", "now"),
            )
            scanner._resolve_rescan_commands(
                db, [], run_full=False, complete=True, failed=0, active=3
            )
            db.commit()
            row = db.execute("SELECT status,error FROM commands WHERE id=?", (cur.lastrowid,)).fetchone()
            db.close()
            self.assertEqual("failed", row[0])
            self.assertEqual("full_rescan_not_satisfied_by_incremental_run", row[1])
        finally:
            os.remove(path)


    def test_dashboard_refuses_missing_password(self):
        with patch.dict(os.environ, {"DASH_PASSWORD": ""}, clear=False), \
                patch.object(api.Auth, "_read", return_value=None):
            with self.assertRaises(RuntimeError):
                api.Auth()

    def test_launcher_rejects_shell_metacharacters_and_missing_dashboard_password(self):
        safe = {
            "mode": "local", "app_dir": "/tmp/poly-btc", "branch": "main", "user": "root",
            "port": 8810, "ssh_port": 22, "dash_user": "admin", "dash_password": "strong-password",
        }
        launcher_server._validate_cfg(safe, require_dashboard_password=True)
        with self.assertRaises(ValueError):
            launcher_server._validate_cfg({**safe, "branch": "$(touch /tmp/pwn)"}, True)
        with self.assertRaises(ValueError):
            launcher_server._validate_cfg({**safe, "dash_password": ""}, True)

    def test_launcher_api_requires_loopback_session_and_json(self):
        httpd = launcher_server.ThreadingHTTPServer(("127.0.0.1", 0), launcher_server.H)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
        try:
            conn.request("GET", "/", headers={"Host": f"127.0.0.1:{httpd.server_port}"})
            response = conn.getresponse()
            response.read()
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            self.assertEqual(200, response.status)

            conn.request("POST", "/api/targets/delete", body="{}", headers={
                "Host": f"127.0.0.1:{httpd.server_port}", "Cookie": cookie,
            })
            response = conn.getresponse()
            response.read()
            self.assertEqual(415, response.status)

            conn.request("POST", "/api/targets/delete", body="{}", headers={
                "Host": f"127.0.0.1:{httpd.server_port}", "Content-Type": "application/json",
            })
            response = conn.getresponse()
            response.read()
            self.assertEqual(401, response.status)

            conn.request("GET", "/", headers={"Host": "attacker.example"})
            response = conn.getresponse()
            response.read()
            self.assertEqual(403, response.status)
        finally:
            conn.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_systemd_install_disables_observer_boot_start(self):
        class FakeExecutor:
            def __init__(self):
                self.commands = []
            def put_text(self, path, text):
                pass
            def run(self, command, on_line=None):
                self.commands.append(command)
                return type("R", (), {"ok": True, "out": ""})()

        ex = FakeExecutor()
        services.SystemdServices(ex, DeployConfig(dash_password="x")).install(lambda _line: None)
        self.assertIn("systemctl disable hl-observe.service", ex.commands)
        self.assertNotIn("systemctl enable hl-observe.service", ex.commands)

    def test_ssh_unknown_host_requires_matching_fingerprint_before_pinning(self):
        class SSHError(Exception):
            pass

        class Key:
            def asbytes(self):
                return b"server-key"
            def get_name(self):
                return "ssh-ed25519"

        class HostKeys:
            def __init__(self):
                self.added = []
            def add(self, *args):
                self.added.append(args)

        clients = []

        class Client:
            def __init__(self):
                self.policy = None
                self.host_keys = HostKeys()
                clients.append(self)
            def load_system_host_keys(self):
                pass
            def load_host_keys(self, _path):
                pass
            def set_missing_host_key_policy(self, policy):
                self.policy = policy
            def connect(self, host, **_kwargs):
                self.policy.missing_host_key(self, host, Key())
            def get_host_keys(self):
                return self.host_keys
            def save_host_keys(self, _path):
                pass

        fake = types.SimpleNamespace(
            SSHClient=Client, MissingHostKeyPolicy=object, SSHException=SSHError,
        )
        expected = "SHA256:" + base64.b64encode(hashlib.sha256(b"server-key").digest()).decode().rstrip("=")
        with patch.dict(sys.modules, {"paramiko": fake}):
            with self.assertRaisesRegex(SSHError, "未知 SSH 主机密钥"):
                SSHExecutor("example.test")
            SSHExecutor("example.test", host_fingerprint=expected, known_hosts_path="/tmp/test-known-hosts")
        self.assertEqual(1, len(clients[-1].host_keys.added))

    def test_scanner_settings_never_start_scan_implicitly(self):
        source = (Path(__file__).resolve().parents[2] / "dashboard" / "web" / "components" / "Settings.jsx").read_text()
        self.assertNotIn("startRescan", source)
        self.assertIn("不会立即启动采集", source)


if __name__ == "__main__":
    unittest.main()
