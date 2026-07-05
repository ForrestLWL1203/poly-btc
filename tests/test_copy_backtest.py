import unittest

from hl.copy_backtest import run_backtest


def fill(t, coin, side, sz, start, px, oid, crossed=True):
    return {
        "time": t,
        "tid": t,
        "coin": coin,
        "side": side,
        "sz": str(sz),
        "startPosition": str(start),
        "px": str(px),
        "oid": oid,
        "crossed": crossed,
    }


def user_fill(user, t, coin, side, sz, start, px, oid, crossed=True):
    x = fill(t, coin, side, sz, start, px, oid, crossed)
    x["user"] = user
    return x


class CopyBacktestTests(unittest.TestCase):
    def test_smart_add_skips_small_adverse_add_and_reports_dependency(self):
        fills = [
            fill(1, "ZEC", "A", 100, 0, 100.0, 10),
            fill(2, "ZEC", "A", 100, -100, 100.5, 11),
            fill(3, "ZEC", "B", 200, -200, 101.0, 12),
        ]

        result = run_backtest("0xabc", fills, sigmas={"ZEC": 0.10})

        self.assertEqual(result["closed_n"], 1)
        self.assertEqual(result["wins"], 0)
        self.assertEqual(result["missed_adds"], 1)
        self.assertEqual(result["followed_adds"], 0)
        self.assertGreater(result["add_dependency"], 0.9)
        self.assertGreater(result["fee_drag"], 0)
        self.assertLess(result["copy_net_pnl"], 0)

    def test_smart_add_follows_large_adverse_add(self):
        fills = [
            fill(1, "ZEC", "B", 100, 0, 100.0, 20),
            fill(2, "ZEC", "B", 100, 100, 98.0, 21),
            fill(3, "ZEC", "A", 200, 200, 101.0, 22),
        ]

        result = run_backtest("0xabc", fills, sigmas={"ZEC": 0.10})

        self.assertEqual(result["closed_n"], 1)
        self.assertEqual(result["missed_adds"], 0)
        self.assertEqual(result["followed_adds"], 1)
        self.assertGreater(result["copy_net_pnl"], 0)

    def test_positive_add_waits_for_gap_when_enabled(self):
        fills = [
            fill(1, "ZEC", "B", 10, 0, 100.0, 30),
            fill(2, "ZEC", "B", 10, 10, 100.5, 31),
            fill(3, "ZEC", "B", 10, 20, 101.2, 32),
            fill(4, "ZEC", "A", 30, 30, 102.0, 33),
        ]

        result = run_backtest("0xabc", fills, sigmas={"ZEC": 0.10}, overrides={
            "FOLLOW_POS_ADD": True,
            "POS_ADD_GAP_K": 0.10,
            "ADD_GAP_SHRINK_G": 1.0,
        })

        self.assertEqual(result["closed_n"], 1)
        self.assertEqual(result["missed_adds"], 1)
        self.assertEqual(result["followed_adds"], 1)

    def test_portfolio_replay_keeps_same_coin_wallet_positions_separate(self):
        fills = [
            user_fill("0xa", 1, "BTC", "B", 100, 0, 100.0, 1),
            user_fill("0xb", 2, "BTC", "B", 100, 0, 100.0, 2),
            user_fill("0xa", 3, "BTC", "A", 100, 100, 101.0, 3),
            user_fill("0xb", 4, "BTC", "A", 100, 100, 102.0, 4),
        ]

        result = run_backtest("portfolio", fills, sigmas={"BTC": 0.04})

        self.assertEqual(result["closed_n"], 2)
        self.assertEqual(result["target_open_events"], 2)
        self.assertEqual(result["copy_peak_concurrent"], 2)
        self.assertEqual({p["addr"] for p in result["positions"]}, {"0xa", "0xb"})

    def test_price_path_can_liquidate_between_target_fills(self):
        fills = [
            fill(1_000, "BTC", "B", 100, 0, 100.0, 40),
            fill(3_000, "BTC", "A", 100, 100, 101.0, 41),
        ]

        fills_only = run_backtest("0xabc", fills, sigmas={"BTC": 0.04}, overrides={"COPY_STOP_ENABLE": False})
        with_path = run_backtest(
            "0xabc",
            fills,
            sigmas={"BTC": 0.04},
            overrides={"COPY_STOP_ENABLE": False},
            price_path={"BTC": [{"time": 2_000, "low": 95.0, "high": 100.0}]},
        )

        self.assertEqual(fills_only["positions"][0]["status"], "closed")
        self.assertEqual(fills_only["positions"][0]["opened_at"], 1_000)
        self.assertEqual(fills_only["positions"][0]["closed_at"], 3_000)
        self.assertGreater(fills_only["copy_net_pnl"], 0)
        self.assertEqual(with_path["positions"][0]["status"], "liquidated")
        self.assertEqual(with_path["liquidations"], 1)
        self.assertLess(with_path["copy_net_pnl"], 0)

    def test_price_path_can_stop_between_target_fills_before_liquidation(self):
        fills = [
            fill(1_000, "BTC", "B", 100, 0, 100.0, 50),
            fill(3_000, "BTC", "A", 100, 100, 101.0, 51),
        ]

        result = run_backtest(
            "0xabc",
            fills,
            sigmas={"BTC": 0.04},
            overrides={"COPY_STOP_ENABLE": True, "STOP_MARGIN_PCT": 0.70},
            price_path={"BTC": [{"time": 2_000, "low": 97.0, "high": 100.0}]},
        )

        self.assertEqual(result["positions"][0]["status"], "stopped")
        self.assertEqual(result["stops"], 1)
        self.assertEqual(result["liquidations"], 0)


if __name__ == "__main__":
    unittest.main()
