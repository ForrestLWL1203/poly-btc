import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hyper import storage
from hyper.copy.copy_backtest import PreparedPricePath, prepare_price_path, run_backtest
from hyper.market import price_path


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
        with mock.patch("hyper.market.price_path.time.time", return_value=now / 1000), mock.patch(
            "hyper.market.price_path.rest.candle_snapshot_range", return_value=candles,
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
        with mock.patch("hyper.market.price_path.time.time", return_value=now / 1000), mock.patch(
            "hyper.market.price_path.rest.candle_snapshot_range", return_value=None,
        ) as fetch:
            first = price_path.ensure(self.db, fills, now - 86_400_000, now)
            second = price_path.ensure(self.db, fills, now - 86_400_000, now)
        self.assertEqual(1, len(first["failed"]))
        self.assertEqual(1, second["deferred"])
        self.assertEqual(1, fetch.call_count)

    def test_forced_retry_bypasses_background_backoff(self):
        now = 4_000_000_000_000
        fills = [{"coin": "OLD", "time": now - 60_000, "side": "B", "sz": "10",
                  "startPosition": "0", "px": "10"}]
        candles = [{"t": now - 900_000, "T": now - 1, "o": "10", "h": "11",
                    "l": "9", "c": "10"}]
        with mock.patch("hyper.market.price_path.time.time", return_value=now / 1000), mock.patch(
            "hyper.market.price_path.rest.candle_snapshot_range",
            side_effect=[None, candles],
        ) as fetch:
            first = price_path.ensure(self.db, fills, now - 86_400_000, now)
            retry = price_path.ensure(
                self.db, fills, now - 86_400_000, now, force_retry=True,
            )
        self.assertEqual(["OLD"], first["failed"])
        self.assertEqual(1, retry["fetched"])
        self.assertEqual(2, fetch.call_count)

    def test_multi_coin_fetch_releases_writer_lock_before_each_rest_call(self):
        now = 4_000_000_000_000
        fills = [
            {"coin": coin, "time": now - 60_000, "side": "B", "sz": "1",
             "startPosition": "0", "px": "100"}
            for coin in ("BTC", "ETH")
        ]
        transaction_state = []

        def fetch(_coin, _interval, _start, _end):
            transaction_state.append(self.db.in_transaction)
            return [{"t": now - 900_000, "T": now - 1, "o": "100", "h": "102",
                     "l": "98", "c": "101"}]

        with mock.patch("hyper.market.price_path.time.time", return_value=now / 1000), mock.patch(
            "hyper.market.price_path.rest.candle_snapshot_range", side_effect=fetch,
        ):
            result = price_path.ensure(self.db, fills, now - 86_400_000, now)

        self.assertEqual([False, False], transaction_state)
        self.assertEqual(2, result["fetched"])

    def test_daily_cache_ignores_forming_candle_and_refreshes_after_utc_close(self):
        day = 86_400_000
        midnight = (4_000_000_000_000 // day) * day
        now = midnight + 12 * 3_600_000
        next_day = midnight + day + 3_600_000
        first_rows = [
            {"t": midnight - day, "T": midnight - 1,
             "o": "100", "h": "102", "l": "99", "c": "101"},
            {"t": midnight, "T": midnight + day - 1,
             "o": "101", "h": "103", "l": "100", "c": "102"},
        ]
        second_rows = [
            {"t": midnight, "T": midnight + day - 1,
             "o": "101", "h": "104", "l": "100", "c": "103"},
            {"t": midnight + day, "T": midnight + 2 * day - 1,
             "o": "103", "h": "105", "l": "102", "c": "104"},
        ]
        with mock.patch(
            "hyper.market.price_path.time.time",
            side_effect=[now / 1000, now / 1000, next_day / 1000],
        ), mock.patch(
            "hyper.market.price_path.rest.candle_snapshot_range",
            side_effect=[first_rows, second_rows],
        ) as fetch:
            price_path.ensure_coins(
                self.db, ["BTC"], midnight - 32 * day, now, interval="1d",
            )
            price_path.ensure_coins(
                self.db, ["BTC"], midnight - 32 * day, now, interval="1d",
            )
            price_path.ensure_coins(
                self.db, ["BTC"], midnight - 31 * day, next_day, interval="1d",
            )

        self.assertEqual(fetch.call_count, 2)
        rows = self.db.execute(
            "SELECT open_time,close_time,close_px FROM coin_price_candle "
            "WHERE coin='BTC' AND interval='1d' ORDER BY open_time"
        ).fetchall()
        self.assertEqual(rows, [
            (midnight - day, midnight - 1, 101.0),
            (midnight, midnight + day - 1, 103.0),
        ])

    def test_finer_path_only_replaces_fully_covered_candle(self):
        coarse = [{"coin": "BTC", "time": 900, "open_time": 1, "close_time": 900,
                   "low": 90, "high": 110, "close": 100, "interval": "15m"}]
        complete = [
            {"coin": "BTC", "time": end, "open_time": start, "close_time": end,
             "low": 95, "high": 105, "close": 100, "interval": "5m"}
            for start, end in ((1, 300), (301, 600), (601, 900))
        ]
        merged = price_path.merge_finer_path(coarse, complete)
        self.assertEqual(3, len(merged))
        gapped = price_path.merge_finer_path(coarse, complete[:2])
        self.assertEqual(coarse, gapped)

    def test_refined_load_is_already_prepared_for_replay(self):
        now = 4_000_000_000_000
        start = now - 900_000
        self.db.execute(
            "INSERT INTO coin_price_candle VALUES (?,?,?,?,?,?,?,?,?)",
            ("BTC", "15m", start, now - 1, 100, 102, 98, 101, now),
        )
        fills = [{
            "coin": "BTC", "time": now - 60_000, "side": "B", "sz": "1",
            "startPosition": "0", "px": "100",
        }]

        refined = price_path.load_refined(self.db, fills, start, now)

        self.assertIsInstance(refined, PreparedPricePath)
        self.assertIs(prepare_price_path(refined), refined)
        self.assertEqual(1, len(refined))


if __name__ == "__main__":
    unittest.main()
