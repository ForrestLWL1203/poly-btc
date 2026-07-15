import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hl import procman, storage


class ProcmanTests(unittest.TestCase):
    def test_never_scanned_db_does_not_look_overdue(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "hl.db")
            db = storage.connect(db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.close()

            self.assertIsNone(procman.hours_since_last_scan(db_path))

    def test_dashboard_startup_status_ticker_does_not_start_scan(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "hl.db")
            db = storage.connect(db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.close()
            stop = threading.Event()

            with patch.object(procman, "reconcile") as reconcile, \
                    patch.object(procman, "start_scan") as start_scan:
                thread = procman.start_auto_scan_ticker(db_path, interval=0.01, stop_event=stop)
                time.sleep(0.05)
                stop.set()
                thread.join(timeout=1)

            self.assertGreaterEqual(reconcile.call_count, 1)
            start_scan.assert_not_called()
            self.assertFalse(thread.is_alive())

    def test_stop_scan_cancels_old_rescan_commands_before_retry(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "hl.db")
            db = storage.connect(db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.execute(
                "INSERT INTO commands(type,payload_json,owner,status,created_at) VALUES "
                "('rescan','{\"full\":true}','dashboard','acked','now'),"
                "('rescan','{\"full\":false}','dashboard','pending','now')"
            )
            db.execute("INSERT OR REPLACE INTO scan_progress(id,state,manual,updated_at) VALUES(1,'scanning',1,'now')")
            db.commit()
            db.close()

            with patch.object(procman, "_use_systemd", return_value=False), \
                    patch.object(procman, "_stop", return_value=True) as stop, \
                    patch.object(procman, "_repair_scan_state", return_value=True) as repair:
                result = procman.stop_scan(db_path)

            db = storage.connect(db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            rows = db.execute(
                "SELECT status,error FROM commands WHERE type='rescan' ORDER BY id"
            ).fetchall()
            db.close()
            stop.assert_called_once_with(db_path, procman.SCAN)
            repair.assert_called_once_with(db_path)
            self.assertFalse(result["scanning"])
            self.assertEqual(result["cancelledCommands"], 2)
            self.assertEqual([tuple(row) for row in rows], [
                ("failed", "cancelled_by_operator"),
                ("failed", "cancelled_by_operator"),
            ])


if __name__ == "__main__":
    unittest.main()
