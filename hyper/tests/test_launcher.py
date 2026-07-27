import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hyper.launcher.core import services, targets, templates
from hyper.launcher.core.model import DeployConfig
from hyper.launcher.core.ssh import SSHExecutor


ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_scan_timers_use_explicit_shanghai_schedules(self):
        cfg = DeployConfig()
        rendered = templates.render_all(cfg)

        self.assertIn(
            "OnCalendar=Mon,Thu *-*-* 04:00:00 Asia/Shanghai",
            rendered["/etc/systemd/system/hl-scan.timer"],
        )
        self.assertIn(
            "OnCalendar=Tue,Wed,Fri,Sat,Sun *-*-* 04:00:00 Asia/Shanghai",
            rendered["/etc/systemd/system/hl-challenger-refresh.timer"],
        )
        self.assertIn(
            "challenger-refresh",
            rendered["/etc/systemd/system/hl-challenger-refresh.service"],
        )

    def test_timer_install_touches_persistent_stamps_before_enable(self):
        commands = []

        class Executor:
            def run(self, command, on_line=None):
                commands.append(command)
                return type("Result", (), {"ok": True, "out": ""})()

            def put_text(self, _path, _text):
                pass

        svc = services.SystemdServices(Executor(), DeployConfig())
        svc.install(lambda _line: None)
        joined = "\n".join(commands)

        self.assertIn(
            "touch /var/lib/systemd/timers/stamp-hl-scan.timer && "
            "systemctl enable --now hl-scan.timer",
            joined,
        )
        self.assertIn(
            "touch /var/lib/systemd/timers/stamp-hl-challenger-refresh.timer && "
            "systemctl enable --now hl-challenger-refresh.timer",
            joined,
        )

    def test_timer_status_includes_each_next_trigger(self):
        class Executor:
            def run(self, command, on_line=None):
                if "NextElapseUSecRealtime" in command:
                    out = "Tue 2026-07-28 04:00:00 CST"
                elif "LastTriggerUSec" in command:
                    out = "n/a"
                else:
                    out = "active"
                return type("Result", (), {"ok": True, "out": out})()

        schedule = services.SystemdServices(Executor(), DeployConfig()).timer_schedule()

        self.assertEqual(set(schedule), {"timer", "challenger_timer"})
        self.assertIn("2026-07-28 04:00:00", schedule["challenger_timer"]["next"])

    def test_quiet_ssh_command_waits_instead_of_busy_spinning(self):
        class Channel:
            polls = 0

            def exec_command(self, _cmd):
                pass

            def recv_ready(self):
                return False

            def recv_stderr_ready(self):
                return False

            def exit_status_ready(self):
                self.polls += 1
                return self.polls >= 3

            def recv_exit_status(self):
                return 0

        channel = Channel()

        class Transport:
            def open_session(self):
                return channel

        class Client:
            def get_transport(self):
                return Transport()

        executor = SSHExecutor.__new__(SSHExecutor)
        executor._client = Client()
        with patch("hyper.launcher.core.ssh.time.sleep") as wait:
            result = executor.run("true")

        self.assertTrue(result.ok)
        self.assertEqual(wait.call_count, 2)

    def test_root_launcher_shortcuts_exist(self):
        mac = ROOT / "launcher" / "launcher.command"
        win = ROOT / "launcher" / "launcher.cmd"

        self.assertTrue(mac.exists(), "macOS launcher shortcut should live with the launcher")
        self.assertTrue(os.access(mac, os.X_OK), "launcher.command should be directly executable")
        self.assertTrue(win.exists(), "Windows launcher shortcut should live with the launcher")
        self.assertIn("-m hyper.launcher.launcher", win.read_text(encoding="utf-8"))

    def test_launcher_build_script_is_executable(self):
        script = ROOT / "launcher" / "web" / "build.sh"
        self.assertTrue(script.exists())
        self.assertTrue(os.access(script, os.X_OK), "launcher/web/build.sh should be directly executable")

    def test_custom_ssh_key_path_reuses_matching_pubkey(self):
        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "id_ed25519"
            key.write_text("not a real private key\n", encoding="utf-8")
            key.chmod(0o600)
            (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAATEST custom\n", encoding="utf-8")

            path, pub = targets.keypair(str(key))

        self.assertEqual(str(key), path)
        self.assertEqual("ssh-ed25519 AAAATEST custom", pub)

    def test_custom_ssh_key_path_must_exist(self):
        with self.assertRaises(FileNotFoundError):
            targets.keypair("/definitely/missing/poly-btc-launcher-key")


if __name__ == "__main__":
    unittest.main()
