import tempfile
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

from hyper import storage
from hyper.discovery import scanner
from hyper.market import generation_market


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
            self.assertEqual(first[1]["BTC"]["mark_px"], 50_000.0)
            sealed = generation_market.seal(db, "g1")
            db.execute("UPDATE coin_vol SET sigma=.99 WHERE coin='BTC'")
            db.commit()
            self.assertEqual(generation_market.load(db, "g1")[0]["BTC"], .12)
            self.assertEqual(generation_market.load(db, "g1")[1]["BTC"]["mark_px"], 50_000.0)
            self.assertEqual(generation_market.summary(db, "g1")["hash"], sealed["hash"])

    def test_warm_sigma_cache_avoids_repeated_candle_requests(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            day = 86_400_000
            asof = (4_000_000_000_000 // day) * day
            requests = []

            def candles(_coin, interval, start_ms, end_ms):
                self.assertEqual(interval, "1d")
                requests.append((int(start_ms), int(end_ms)))
                cursor = (int(start_ms) // day) * day
                rows = []
                while cursor < int(end_ms):
                    rows.append({
                        "t": cursor, "T": cursor + day - 1,
                        "o": "100", "h": "102", "l": "100", "c": "101",
                    })
                    cursor += day
                return rows

            with patch.object(
                generation_market.volatility.price_path.time, "time",
                return_value=(asof + day) / 1000,
            ), patch.object(
                generation_market.volatility.price_path.rest,
                "candle_snapshot_range", side_effect=candles,
            ):
                first = generation_market.Resolver(
                    db, "daily-g1", asof, {"BTC"}, {"BTC": context()},
                )
                first.ensure({"BTC"})
                second = generation_market.Resolver(
                    db, "daily-g2", asof + day, {"BTC"}, {"BTC": context()},
                )
                second.ensure({"BTC"})
                same_day = generation_market.Resolver(
                    db, "daily-g3", asof + day, {"BTC"}, {"BTC": context()},
                )
                same_day.ensure({"BTC"})

            self.assertEqual(len(requests), 1)
            self.assertEqual(
                db.execute(
                    "SELECT sigma_source FROM generation_market_snapshot "
                    "WHERE generation='daily-g2' AND coin='BTC'"
                ).fetchone()[0],
                "coin_vol_cache",
            )

    def test_warm_sigma_is_frozen_when_resolver_starts(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO coin_vol(coin,sigma,sigma_fast,sigma_slow,n,updated_at) "
                "VALUES('BTC',.12,.12,.08,30,'before')"
            )
            db.commit()
            resolver = generation_market.Resolver(
                db, "warm-g1", 1_700_000_000_000, {"BTC"}, {"BTC": context()},
            )
            db.execute("UPDATE coin_vol SET sigma=.99,sigma_fast=.99 WHERE coin='BTC'")
            db.commit()
            with patch.object(
                generation_market.volatility, "compute_at",
                side_effect=AssertionError("warm sigma must not refetch"),
            ):
                sigmas, _ = resolver.ensure({"BTC"})
            self.assertEqual(sigmas["BTC"], .12)
            self.assertEqual(db.execute(
                "SELECT sigma,sigma_source FROM generation_market_snapshot "
                "WHERE generation='warm-g1' AND coin='BTC'"
            ).fetchone(), (.12, "coin_vol_cache"))

    def test_transient_seven_percent_cache_is_not_reused_as_accurate_sigma(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO coin_vol(coin,sigma,sigma_fast,sigma_slow,n,updated_at) "
                "VALUES('ETH',.07,NULL,NULL,0,'before')"
            )
            db.commit()
            resolver = generation_market.Resolver(
                db, "cold-g1", 1_700_000_000_000, {"ETH"}, {"ETH": context()},
            )
            sample = {"status": "real", "sigma": .11, "fast": .11, "slow": .08, "n": 30}
            with patch.object(
                generation_market.volatility, "compute_at", return_value=sample,
            ) as compute:
                sigmas, _ = resolver.ensure({"ETH"})
            compute.assert_called_once()
            self.assertEqual(sigmas["ETH"], .11)

    def test_missing_terminal_mark_retries_independent_bulk_price_source(self):
        replay = {30: {
            "valuation_status": "missing_marks",
            "valuation_missing_coins": ["BTC"],
        }}
        with patch.object(scanner.rest, "all_mids", side_effect=[{}, {"BTC": "65000"}]) as mids:
            marks = scanner._retry_missing_copy_valuation_marks({}, replay)
        self.assertEqual(marks["BTC"], 65_000.0)
        self.assertEqual(mids.call_count, 2)

    def test_existing_generation_mark_avoids_price_retry(self):
        replay = {30: {
            "valuation_status": "missing_marks",
            "valuation_missing_coins": ["BTC"],
        }}
        with patch.object(scanner.rest, "all_mids") as mids:
            marks = scanner._retry_missing_copy_valuation_marks({"BTC": 64_000}, replay)
        self.assertEqual(marks["BTC"], 64_000.0)
        mids.assert_not_called()

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

    def test_transport_failure_is_retryable_and_not_defaulted(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            resolver = generation_market.Resolver(
                db, "g3", 1_700_000_000_000, {"ETH"}, {"ETH": context()},
            )
            failed = {
                "status": "request_failed", "sigma": None,
                "fast": None, "slow": None, "n": 0,
            }
            recovered = {
                "status": "real", "sigma": .08,
                "fast": .08, "slow": .06, "n": 30,
            }
            with patch.object(
                generation_market.volatility, "compute_at",
                side_effect=[failed, recovered],
            ) as compute:
                with self.assertRaisesRegex(generation_market.MarketSnapshotError, "sigma_request_failed:ETH"):
                    resolver.ensure({"ETH"})
                sigmas, _ = resolver.ensure({"ETH"})
            self.assertEqual(compute.call_count, 2)
            self.assertEqual(sigmas["ETH"], .08)
            self.assertEqual(db.execute(
                "SELECT sigma FROM generation_market_snapshot WHERE generation='g3' AND coin='ETH'"
            ).fetchone()[0], .08)

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

    def test_sealed_resolver_is_read_only_and_rejects_missing_coin(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            resolver = generation_market.Resolver(
                db, "g7", 1_700_000_000_000, {"BTC"}, {"BTC": context()},
            )
            sample = {"status": "real", "sigma": .05, "fast": .05, "slow": .04, "n": 30}
            with patch.object(generation_market.volatility, "compute_at", return_value=sample):
                resolver.ensure({"BTC"})
            generation_market.seal(db, "g7")

            frozen = generation_market.SealedResolver(db, "g7")
            sigmas, market_ctx = frozen.ensure({"BTC"})

            self.assertEqual(sigmas, {"BTC": .05})
            self.assertEqual(market_ctx["BTC"]["mark_px"], 50_000.0)
            with self.assertRaisesRegex(
                    generation_market.MarketSnapshotError, "ETH:missing"):
                frozen.ensure({"ETH"})


if __name__ == "__main__":
    unittest.main()
