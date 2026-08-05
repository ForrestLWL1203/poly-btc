import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hyper.launcher.core import services, targets, templates
from hyper.launcher.core.model import DeployConfig
from hyper.launcher.server import _cfg_from_target, _validate_cfg
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
        self.assertIn(
            " -m hyper.cli.discover --db /root/poly-btc/data/hl.db storage-maintenance",
            rendered["/etc/systemd/system/hl-scan.service"],
        )
        self.assertIn(
            " -m hyper.cli.discover --db /root/poly-btc/data/hl.db storage-maintenance",
            rendered["/etc/systemd/system/hl-challenger-refresh.service"],
        )
        self.assertIn(
            "Environment=HL_LIVE_ACCOUNT_MONITOR_MODE=rest_only",
            rendered["/etc/systemd/system/hl-observe.service"],
        )

    def test_account_monitor_mode_is_rest_only_by_default_and_ws_is_explicit(self):
        with patch.object(targets, "keypair", return_value=("/tmp/key", "ssh-ed25519 test")):
            default_cfg = _cfg_from_target({"mode": "local", "app_dir": str(ROOT)})
        self.assertEqual("rest_only", default_cfg.account_monitor_mode)

        ws_cfg = DeployConfig(account_monitor_mode="ws_primary")
        rendered = templates.render_all(ws_cfg)
        self.assertIn(
            "Environment=HL_LIVE_ACCOUNT_MONITOR_MODE=ws_primary",
            rendered["/etc/systemd/system/hl-observe.service"],
        )

    def test_invalid_account_monitor_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "account_monitor_mode"):
            _validate_cfg({"mode": "local", "app_dir": str(ROOT),
                           "account_monitor_mode": "automatic"})
        with self.assertRaisesRegex(ValueError, "account_monitor_mode"):
            templates.observe_unit("/srv/app", "/srv/app/.venv/bin/python", "/srv/app/data/hl.db",
                                   "automatic")

    def test_saved_target_persists_account_monitor_selection(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.object(targets, "TARGETS_JSON", str(Path(td) / "targets.json")), \
                patch.object(targets, "DATA", td):
            targets.save({"id": "vps:test", "mode": "vps", "host": "test",
                          "account_monitor_mode": "ws_primary"})
            saved = targets.get("vps:test")

        self.assertEqual("ws_primary", saved["account_monitor_mode"])

    def test_account_monitor_config_sync_does_not_restart_observer(self):
        events = []

        class Executor:
            def run(self, command):
                events.append(command)
                return type("R", (), {"ok": True, "out": "active"})()

            def close(self):
                events.append("close")

        class Services:
            def sync_observer_unit(self):
                events.append("sync_observer_unit")
                return type("R", (), {"ok": True, "out": ""})()

        from hyper.launcher.core import ops
        with patch.object(ops, "_conn", return_value=(Executor(), Services())):
            result = ops.configure_account_monitor(
                DeployConfig(mode="vps", account_monitor_mode="ws_primary"),
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["restartRequired"])
        self.assertIn("sync_observer_unit", events)
        self.assertFalse(any("restart" in event for event in events))

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

        self.assertEqual(set(schedule), {
            "timer", "challenger_timer", "finalize_timer",
        })
        self.assertIn("2026-07-28 04:00:00", schedule["challenger_timer"]["next"])
        self.assertIn("2026-07-28 04:00:00", schedule["finalize_timer"]["next"])

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
