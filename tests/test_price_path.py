import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hl import price_path, storage
from hl.copy_backtest import run_backtest


class PricePathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = storage.connect(
            str(Path(self.tmp.name) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_shared_cache_upserts_and_prunes_by_interval(self):
        now = 4_000_000_000_000
        old = now - 40 * 86_400_000
        self.db.execute(
            "INSERT INTO coin_price_candle VALUES (?,?,?,?,?,?,?,?,?)",
            ("BTC", "15m", old, old + 899_999, 100, 101, 99, 100, old),
        )
        fills = [{"coin": "BTC", "time": now - 60_000, "side": "B", "sz": "1",
                  "startPosition": "0", "px": "100"}]
        candles = [{"t": now - 900_000, "T": now - 1, "o": "100", "h": "102",
                    "l": "98", "c": "101"}]
        with mock.patch("hl.price_path.time.time", return_value=now / 1000), mock.patch(
            "hl.price_path.rest.candle_snapshot_range", return_value=candles,
        ):
            result = price_path.ensure(self.db, fills, now - 86_400_000, now)
        self.assertEqual(1, result["fetched"])
        self.assertEqual(1, result["deleted"])
        self.assertEqual(1, self.db.execute("SELECT COUNT(*) FROM coin_price_candle").fetchone()[0])

    def test_boundary_candle_does_not_false_liquidate_new_position(self):
        fills = [
            {"coin": "BTC", "time": 5_000, "side": "B", "sz": "100", "startPosition": "0", "px": "100"},
            {"coin": "BTC", "time": 20_000, "side": "A", "sz": "100", "startPosition": "100", "px": "110"},
        ]
        path = [{"coin": "BTC", "time": 10_000, "open_time": 1_000, "close_time": 10_000,
                 "low": 1, "high": 110, "close": 100}]
        result = run_backtest("x", fills, overrides={"STABLE_LEV_CAP": 10, "MID_LEV_CAP": 10,
                              "HIGH_LEV_CAP": 10}, price_path=path,
                              price_path_meta={"coverage": 1})
        self.assertEqual(0, result["liquidations"])
        self.assertGreater(result["price_path_boundary_skips"], 0)

    def test_failed_market_uses_retry_backoff(self):
        now = 4_000_000_000_000
        fills = [{"coin": "OLD", "time": now - 60_000, "side": "B", "sz": "10",
                  "startPosition": "0", "px": "10"}]
        with mock.patch("hl.price_path.time.time", return_value=now / 1000), mock.patch(
            "hl.price_path.rest.candle_snapshot_range", return_value=None,
        ) as fetch:
            first = price_path.ensure(self.db, fills, now - 86_400_000, now)
            second = price_path.ensure(self.db, fills, now - 86_400_000, now)
        self.assertEqual(1, len(first["failed"]))
        self.assertEqual(1, second["deferred"])
        self.assertEqual(1, fetch.call_count)


if __name__ == "__main__":
    unittest.main()
