import sqlite3
import tempfile
import time
import unittest
from importlib import import_module, util
from pathlib import Path
from unittest.mock import patch

from dashboard.api import discovery as api_discovery
from hyper import params, storage


class CompactDiscoveryDb:
    """Reject the retired full-table aggregations from the polling endpoint."""

    def __init__(self, db):
        self.db = db

    def execute(self, sql, args=()):
        normalized = " ".join(sql.lower().split())
        forbidden = ("pipeline_audit", "status='rejected'", "score*?")
        if any(token in normalized for token in forbidden):
            raise AssertionError(f"retired discovery aggregation: {normalized}")
        return self.db.execute(sql, args)


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

    def test_scanning_scanner_allows_expected_multi_minute_batch_heartbeat(self):
        now = 2_000_000_000.0
        db = self._db_with_status("scanning")
        heartbeat = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(now - 6 * 60),
        )
        db.execute(
            "UPDATE process_status SET heartbeat_at=? WHERE name='scanner'",
            (heartbeat,),
        )

        with patch.object(api_discovery.time, "time", return_value=now):
            st = api_discovery.scanner_status(db)

        self.assertFalse(st["stale"])

    def test_scanning_scanner_is_stale_after_scanner_specific_timeout(self):
        now = 2_000_000_000.0
        db = self._db_with_status("scanning")
        heartbeat = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(now - api_discovery.SCANNER_STALE_SEC - 1),
        )
        db.execute(
            "UPDATE process_status SET heartbeat_at=? WHERE name='scanner'",
            (heartbeat,),
        )

        with patch.object(api_discovery.time, "time", return_value=now):
            st = api_discovery.scanner_status(db)

        self.assertTrue(st["stale"])

    def test_discovery_endpoints_are_split_from_api_module(self):
        self.assertIsNotNone(util.find_spec("dashboard.api.discovery"))
        api_discovery = import_module("dashboard.api.discovery")

        self.assertTrue(callable(api_discovery.scanner_status))
        self.assertTrue(callable(api_discovery.ep_discovery))
        self.assertTrue(callable(api_discovery.ep_scan_runs))
        self.assertTrue(callable(api_discovery.ep_scan_status))
        self.assertTrue(callable(api_discovery.ep_score_dist))
        self.assertTrue(callable(api_discovery.ep_pipeline_summary))

    def test_discovery_returns_only_the_compact_funnel_contract(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)
            db.execute("INSERT INTO leaderboard (addr,is_candidate) VALUES ('0xaaa',1)")
            db.execute("INSERT INTO profile (addr,status,score) VALUES ('0x1','active',0.01)")
            db.execute("INSERT INTO profile (addr,status,score) VALUES ('0x2','active',0.50)")
            db.execute("INSERT INTO profile (addr,status,score) VALUES ('0x3','active',1.20)")
            db.execute("INSERT INTO profile (addr,status,score) VALUES ('0x4','rejected',0.00)")
            db.execute("INSERT INTO watchlist (rank,addr,score,updated_at) VALUES (1,'0x2',0.50,'now')")
            db.commit()

            res = api_discovery.ep_discovery(CompactDiscoveryDb(db))

        self.assertEqual(
            set(res["funnel"]),
            {"leaderboard", "candidates", "perpPrefilter", "challenger",
             "core", "finalCore", "watchlist"},
        )
        self.assertEqual(res["funnel"]["leaderboard"], 1)
        self.assertEqual(res["funnel"]["candidates"], 1)
        self.assertNotIn("funnelStages", res)
        self.assertNotIn("failureCategories", res)
        self.assertNotIn("rejectReasons", res)
        self.assertNotIn("scoreHistogram", res)

    def test_scan_runs_exposes_profiled_count_not_legacy_probed_new_name(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.execute(
                "INSERT INTO scan_runs "
                "(started_at,finished_at,duration_s,candidates,probed_new,profiled,added,retired,kept,rejected,n_active) "
                "VALUES ('t0','t1',1.0,100,88,42,3,1,5,33,8)"
            )
            db.commit()

            res = api_discovery.ep_scan_runs(db, 1)

        self.assertEqual(res["runs"][0]["profiled"], 42)
        self.assertNotIn("probedNew", res["runs"][0])


if __name__ == "__main__":
    unittest.main()
