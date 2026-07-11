import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import hl_discover


class ScanCadenceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE scan_runs (finished_at TEXT,complete INTEGER,full INTEGER)"
        )

    def args(self):
        return SimpleNamespace(full_scan=False, no_harvest=False)

    @patch.object(hl_discover.time, "time", return_value=1_800_000_000)
    def test_first_automatic_run_is_weekly_full(self, _):
        args = self.args()
        cadence = hl_discover._configure_scan_cadence(self.db, args, manual=False)
        self.assertEqual(cadence, "weekly_full")
        self.assertTrue(args.full_scan)
        self.assertFalse(args.no_harvest)

    @patch.object(hl_discover.time, "time", return_value=1_800_000_000)
    def test_recent_full_makes_daily_run_incremental_without_harvest(self, _):
        import time
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1_800_000_000 - 2 * 86400))
        self.db.execute("INSERT INTO scan_runs VALUES (?,1,1)", (stamp,))
        args = self.args()
        cadence = hl_discover._configure_scan_cadence(self.db, args, manual=False)
        self.assertEqual(cadence, "daily_incremental")
        self.assertFalse(args.full_scan)
        self.assertTrue(args.no_harvest)

    def test_manual_scan_keeps_user_requested_mode(self):
        args = self.args()
        cadence = hl_discover._configure_scan_cadence(self.db, args, manual=True)
        self.assertEqual(cadence, "manual")
        self.assertFalse(args.full_scan)
        self.assertFalse(args.no_harvest)


if __name__ == "__main__":
    unittest.main()
