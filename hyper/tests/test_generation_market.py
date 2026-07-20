import tempfile
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

from hyper import generation_market, scanner, storage


def context(max_leverage=20, *, volume="1000000", oi="100", mark="50000"):
    return {
        "universe_maxLeverage": max_leverage,
        "dayNtlVlm": volume,
        "openInterest": oi,
        "markPx": mark,
    }


class GenerationMarketSnapshotTests(unittest.TestCase):
    def open_db(self, td):
        return storage.connect(
            str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
        )

    def test_same_coin_is_fetched_once_and_snapshot_ignores_live_cache_changes(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            resolver = generation_market.Resolver(
                db, "g1", 1_700_000_000_000, {"BTC"}, {"BTC": context()},
            )
            sample = {"status": "real", "sigma": .12, "fast": .12, "slow": .08, "n": 30}
            with patch.object(generation_market.volatility, "compute_at", return_value=sample) as compute:
                first = resolver.ensure({"BTC"})
                second = resolver.ensure({"BTC"})
            self.assertEqual(compute.call_count, 1)
            self.assertEqual(first, second)
            self.assertEqual(first[0]["BTC"], .12)
            sealed = generation_market.seal(db, "g1")
            db.execute("UPDATE coin_vol SET sigma=.99 WHERE coin='BTC'")
            db.commit()
            self.assertEqual(generation_market.load(db, "g1")[0]["BTC"], .12)
            self.assertEqual(generation_market.summary(db, "g1")["hash"], sealed["hash"])

    def test_profile_resolves_market_before_first_strict_copy_replay(self):
        source = inspect.getsource(scanner._profile_one)
        self.assertLess(
            source.index("resolver.ensure("),
            source.index("copy_results = _copy_bt_results("),
        )

    def test_bulk_context_failure_rejects_generation_snapshot(self):
        with patch.object(generation_market.rest, "asset_contexts", return_value={}):
            with self.assertRaisesRegex(
                    generation_market.MarketSnapshotError, "crypto_market_context_unavailable"):
                generation_market.fetch_context_snapshot({"BTC"})

    def test_insufficient_closed_history_uses_neutral_seven_percent(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            resolver = generation_market.Resolver(
                db, "g2", 1_700_000_000_000, {"ETH"}, {"ETH": context()},
            )
            sample = {
                "status": "insufficient_history", "sigma": None,
                "fast": None, "slow": None, "n": 4,
            }
            with patch.object(generation_market.volatility, "compute_at", return_value=sample):
                sigmas, _ = resolver.ensure({"ETH"})
            self.assertEqual(sigmas["ETH"], .07)
            source, n = db.execute(
                "SELECT sigma_source,sigma_n FROM generation_market_snapshot "
                "WHERE generation='g2' AND coin='ETH'"
            ).fetchone()
            self.assertEqual((source, n), ("insufficient_history_default", 4))

    def test_transport_failure_is_cached_as_error_not_defaulted(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            resolver = generation_market.Resolver(
                db, "g3", 1_700_000_000_000, {"ETH"}, {"ETH": context()},
            )
            sample = {
                "status": "request_failed", "sigma": None,
                "fast": None, "slow": None, "n": 0,
            }
            with patch.object(generation_market.volatility, "compute_at", return_value=sample) as compute:
                with self.assertRaisesRegex(generation_market.MarketSnapshotError, "sigma_request_failed:ETH"):
                    resolver.ensure({"ETH"})
                with self.assertRaisesRegex(generation_market.MarketSnapshotError, "sigma_request_failed:ETH"):
                    resolver.ensure({"ETH"})
            self.assertEqual(compute.call_count, 1)
            self.assertIsNone(db.execute(
                "SELECT 1 FROM generation_market_snapshot WHERE generation='g3' AND coin='ETH'"
            ).fetchone())

    def test_missing_crypto_liquidity_and_max_leverage_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            no_liquidity = {"universe_maxLeverage": 20}
            resolver = generation_market.Resolver(
                db, "g4", 1_700_000_000_000, {"ETH"}, {"ETH": no_liquidity},
            )
            with self.assertRaisesRegex(generation_market.MarketSnapshotError, "crypto_liquidity"):
                resolver.ensure({"ETH"})

            stock = generation_market.Resolver(
                db, "g5", 1_700_000_000_000, {"xyz:AAPL"}, {"xyz:AAPL": {}},
            )
            with self.assertRaisesRegex(generation_market.MarketSnapshotError, "max_leverage"):
                stock.ensure({"xyz:AAPL"})

    def test_sealed_hash_detects_external_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            resolver = generation_market.Resolver(
                db, "g6", 1_700_000_000_000, {"BTC"}, {"BTC": context()},
            )
            sample = {"status": "real", "sigma": .05, "fast": .05, "slow": .04, "n": 30}
            with patch.object(generation_market.volatility, "compute_at", return_value=sample):
                resolver.ensure({"BTC"})
            generation_market.seal(db, "g6")
            db.execute(
                "UPDATE generation_market_snapshot SET sigma=.09 WHERE generation='g6' AND coin='BTC'"
            )
            db.commit()
            with self.assertRaisesRegex(generation_market.MarketSnapshotError, "hash_mismatch"):
                generation_market.load(db, "g6")


if __name__ == "__main__":
    unittest.main()
