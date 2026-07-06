import sqlite3
import unittest
from importlib import import_module, util

from hl import api_discovery


class ApiScannerStatusTests(unittest.TestCase):
    def _db_with_status(self, state):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE process_status (name TEXT PRIMARY KEY,state TEXT,pid INTEGER,heartbeat_at TEXT,detail_json TEXT)"
        )
        db.execute(
            "INSERT INTO process_status (name,state,heartbeat_at,detail_json) VALUES (?,?,?,?)",
            ("scanner", state, "2000-01-01T00:00:00Z", "{}"),
        )
        return db

    def test_idle_batch_scanner_is_not_stale_between_timer_runs(self):
        st = api_discovery.scanner_status(self._db_with_status("idle"))

        self.assertFalse(st["stale"])

    def test_scanning_scanner_is_stale_when_heartbeat_is_old(self):
        st = api_discovery.scanner_status(self._db_with_status("scanning"))

        self.assertTrue(st["stale"])

    def test_discovery_endpoints_are_split_from_api_module(self):
        self.assertIsNotNone(util.find_spec("hl.api_discovery"))
        api_discovery = import_module("hl.api_discovery")

        self.assertTrue(callable(api_discovery.scanner_status))
        self.assertTrue(callable(api_discovery.ep_discovery))
        self.assertTrue(callable(api_discovery.ep_scan_runs))
        self.assertTrue(callable(api_discovery.ep_scan_status))
        self.assertTrue(callable(api_discovery.ep_score_dist))


if __name__ == "__main__":
    unittest.main()
