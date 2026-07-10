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


if __name__ == "__main__":
    unittest.main()
