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
    def test_low_liquidity_crypto_open_is_skipped(self):
        fills = [
            fill(1_000, "VINE", "A", 100_000, 0, 0.0098, 1),
            fill(2_000, "VINE", "B", 100_000, -100_000, 0.0100, 2),
        ]

        result = run_backtest(
            "0xabc",
            fills,
            sigmas={"VINE": 0.12},
            market_ctx={"VINE": {"day_ntl_vlm": 1_600_000, "oi_notional": 588_000}},
        )

        self.assertEqual(result["target_open_events"], 1)
        self.assertEqual(result["opened_n"], 0)
        self.assertEqual(result["closed_n"], 0)
        self.assertEqual(result["skip_reasons"].get("skip_low_liquidity"), 1)

    def test_coin_blacklist_skips_new_open(self):
        fills = [
            fill(1_000, "xyz:SHKX", "B", 100, 0, 100.0, 1),
            fill(2_000, "xyz:SHKX", "A", 100, 100, 101.0, 2),
        ]

        result = run_backtest("0xabc", fills, sigmas={"xyz:SHKX": 0.12}, overrides={
            "COIN_BLACKLIST": "XYZ:SHKX",
        })

        self.assertEqual(result["target_open_events"], 1)
        self.assertEqual(result["opened_n"], 0)
        self.assertEqual(result["closed_n"], 0)
        self.assertEqual(len(result["open_positions"]), 0)
        self.assertEqual(result["skip_reasons"].get("skip_coin_blacklist"), 1)

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

    def test_rejected_first_slice_does_not_consume_same_oid_add(self):
        fills = [
            fill(1, "ZEC", "B", 10, 0, 100.0, 1),
            fill(2, "ZEC", "B", 1, 10, 99.9, 2),
            fill(3, "ZEC", "B", 9, 11, 98.0, 2),
            fill(4, "ZEC", "A", 20, 20, 101.0, 3),
        ]

        result = run_backtest("0xabc", fills, sigmas={"ZEC": 0.10})

        self.assertEqual(result["target_adds"], 1)
        self.assertEqual(result["followed_adds"], 1)
        self.assertEqual(result["missed_adds"], 0)
        self.assertGreater(result["copy_net_pnl"], 0)

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

    def test_tier_sizing_overrides_match_live_follow_params(self):
        fills = [
            fill(1, "BTC", "B", 10_000, 0, 100.0, 60),
            fill(2, "BTC", "A", 10_000, 10_000, 101.0, 61),
        ]

        result = run_backtest("0xabc", fills, sigmas={"BTC": 0.04}, overrides={
            "STABLE_MARGIN_PCT": 0.015,
            "STABLE_LEV_CAP": 25.0,
            "STABLE_MIN_NOTIONAL": 2500.0,
        })

        self.assertEqual(result["closed_n"], 1)
        self.assertAlmostEqual(result["positions"][0]["margin"], 150.0)
        self.assertEqual(result["positions"][0]["leverage"], 25.0)

    def test_master_leverage_on_fill_caps_backtest_leverage_like_live_observer(self):
        fills = [
            fill(1, "BTC", "B", 10_000, 0, 100.0, 62),
            fill(2, "BTC", "A", 10_000, 10_000, 101.0, 63),
        ]
        fills[0]["masterLeverage"] = 5

        result = run_backtest("0xabc", fills, sigmas={"BTC": 0.04}, overrides={
            "STABLE_MARGIN_PCT": 0.015,
            "STABLE_LEV_CAP": 25.0,
            "STABLE_MIN_NOTIONAL": 0.0,
        })

        self.assertEqual(result["closed_n"], 1)
        self.assertEqual(result["positions"][0]["leverage"], 5.0)
        self.assertEqual(result["master_leverage_known"], 1)
        self.assertEqual(result["master_leverage_missing"], 0)

    def test_dynamic_margin_range_shrinks_only_as_deploy_fills(self):
        fills = []
        for i in range(8):
            coin = f"C{i}"
            fills.append(fill(i + 1, coin, "B", 10_000, 0, 100.0, 100 + i))
        sigmas = {f"C{i}": 0.04 for i in range(8)}

        result = run_backtest("0xabc", fills, sigmas=sigmas, overrides={
            "STABLE_MARGIN_MIN_PCT": 0.02,
            "STABLE_MARGIN_PCT": 0.04,
            "STABLE_LEV_CAP": 10.0,
            "STABLE_MIN_NOTIONAL": 0.0,
            "STABLE_COIN_CAP_PCT": 1.0,
            "DEPLOY_FULL_PCT": 0.08,
            "MAX_DEPLOY_PCT": 0.50,
            "COPY_STOP_ENABLE": False,
        })

        margins = [p["margin"] for p in sorted(result["open_positions"], key=lambda p: p["opened_at"])]

        self.assertGreaterEqual(len(margins), 6)
        self.assertGreater(margins[0], 390)
        self.assertGreater(margins[1], 390)
        self.assertLess(margins[3], 390)
        self.assertGreater(margins[3], 300)
        self.assertGreater(margins[-1], 190)
        self.assertLess(margins[-1], margins[3])

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

    def test_long_to_short_flip_closes_old_position_and_opens_new_one(self):
        fills = [
            fill(1_000, "BTC", "B", 100, 0, 100.0, 70),
            fill(2_000, "BTC", "A", 200, 100, 99.0, 71),
            fill(3_000, "BTC", "B", 100, -100, 98.0, 72),
        ]

        result = run_backtest("0xabc", fills, sigmas={"BTC": 0.04}, overrides={
            "COPY_STOP_ENABLE": False,
            "STABLE_MIN_NOTIONAL": 0.0,
        })

        self.assertEqual(result["closed_n"], 2)
        self.assertEqual(result["target_open_events"], 2)
        self.assertEqual(len(result["open_positions"]), 0)
        self.assertEqual([p["side"] for p in result["positions"]], ["long", "short"])
        self.assertEqual([p["closed_at"] for p in result["positions"]], [2_000, 3_000])

    def test_short_to_long_flip_closes_old_position_and_opens_new_one(self):
        fills = [
            fill(1_000, "ETH", "A", 100, 0, 100.0, 80),
            fill(2_000, "ETH", "B", 200, -100, 101.0, 81),
            fill(3_000, "ETH", "A", 100, 100, 102.0, 82),
        ]

        result = run_backtest("0xabc", fills, sigmas={"ETH": 0.08}, overrides={
            "COPY_STOP_ENABLE": False,
            "MID_MIN_NOTIONAL": 0.0,
        })

        self.assertEqual(result["closed_n"], 2)
        self.assertEqual(result["target_open_events"], 2)
        self.assertEqual(len(result["open_positions"]), 0)
        self.assertEqual([p["side"] for p in result["positions"]], ["short", "long"])
        self.assertEqual([p["closed_at"] for p in result["positions"]], [2_000, 3_000])

    def test_near_full_reduce_closes_remaining_dust(self):
        fills = [
            fill(1_000, "ETH", "B", 100, 0, 100.0, 90),
            fill(2_000, "ETH", "A", 99.9999, 100, 101.0, 91),
        ]

        result = run_backtest("0xabc", fills, sigmas={"ETH": 0.08}, overrides={
            "COPY_STOP_ENABLE": False,
            "MID_MIN_NOTIONAL": 0.0,
        })

        self.assertEqual(result["closed_n"], 1)
        self.assertEqual(len(result["open_positions"]), 0)
        self.assertEqual(result["positions"][0]["status"], "closed")
        self.assertEqual(result["positions"][0]["closed_at"], 2_000)


if __name__ == "__main__":
    unittest.main()
