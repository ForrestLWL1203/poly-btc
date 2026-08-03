from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from hyper import storage
from hyper.discovery import collection_blacklist, scanner


class CollectionBlacklistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "hl.db")
        self.db = storage.connect(
            self.db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
        )
        self.addr = "0x" + "a" * 40

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_only_high_confidence_automation_reasons_are_permanent(self):
        self.assertTrue(collection_blacklist.should_block({
            "reason": "bot_frequency", "n_trades": 20, "data_status": "valid",
        }))
        self.assertFalse(collection_blacklist.should_block({
            "reason": "bot_frequency", "n_trades": 19, "data_status": "valid",
        }))
        self.assertTrue(collection_blacklist.should_block({
            "reason": "hft_uncopyable", "n_trades": 10, "data_status": "valid",
        }))
        self.assertTrue(collection_blacklist.should_block({
            "reason": "grid_dca", "n_trades": 5, "data_status": "valid",
        }))
        for reason in ("heavy_dca", "too_many_concurrent", "copy_net_non_positive"):
            self.assertFalse(collection_blacklist.should_block({
                "reason": reason, "n_trades": 1000, "data_status": "valid",
            }))
        self.assertFalse(collection_blacklist.should_block({
            "reason": "hft_uncopyable", "n_trades": 100, "data_status": "deferred_data_error",
        }))

    def test_record_and_purge_remove_only_discovery_history(self):
        self.db.execute(
            "INSERT INTO candidate_fills(addr,tid,time,fill_json) VALUES (?,?,?,?)",
            (self.addr, 1, 1, "{}"),
        )
        self.db.execute(
            "INSERT INTO fill_cache_state(addr,coverage_start_ms) VALUES (?,?)",
            (self.addr, 1),
        )
        self.db.execute(
            "INSERT INTO episode(addr,coin,side,open_ms,seq) VALUES (?,?,?,?,?)",
            (self.addr, "BTC", "long", 1, 0),
        )
        profile = {
            "addr": self.addr.upper(),
            "reason": "grid_dca",
            "n_trades": 8,
            "data_status": "valid",
            "profile_generation": "g1",
        }
        self.assertTrue(collection_blacklist.record(self.db, profile))
        deleted = collection_blacklist.purge_address(self.db, self.addr)
        self.db.commit()

        self.assertEqual(collection_blacklist.reason_for(self.db, self.addr), "grid_dca")
        self.assertEqual(deleted["candidate_fills"], 1)
        self.assertEqual(deleted["fill_cache_state"], 1)
        self.assertEqual(deleted["episode"], 1)

    def test_perp_prefilter_never_calls_api_for_blacklisted_address(self):
        collection_blacklist.record(self.db, {
            "addr": self.addr,
            "reason": "hft_uncopyable",
            "n_trades": 30,
            "data_status": "valid",
        })
        self.db.commit()
        p = SimpleNamespace(week_vlm_min=250_000)
        with mock.patch("hyper.discovery.scanner.rest.portfolio") as portfolio:
            result = scanner._run_perp_prefilter(
                self.db, [self.addr], p, "2026-08-03T00:00:00Z", allow_cache=False,
            )[self.addr]

        portfolio.assert_not_called()
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "hft_uncopyable")
        self.assertEqual(result.windows["scanResolution"]["source"], "collection_blacklist")

    def test_explicit_remove_requires_a_valid_address_and_preserves_cached_history(self):
        collection_blacklist.record(self.db, {
            "addr": self.addr,
            "reason": "grid_dca",
            "n_trades": 8,
            "data_status": "valid",
        })
        self.db.execute(
            "INSERT INTO candidate_fills(addr,tid,time,fill_json) VALUES (?,?,?,?)",
            (self.addr, 1, 1, "{}"),
        )
        self.assertTrue(collection_blacklist.remove(self.db, self.addr.upper()))
        self.assertIsNone(collection_blacklist.reason_for(self.db, self.addr))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM candidate_fills WHERE addr=?", (self.addr,),
        ).fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "invalid_wallet_address"):
            collection_blacklist.remove(self.db, "0x123")


if __name__ == "__main__":
    unittest.main()
