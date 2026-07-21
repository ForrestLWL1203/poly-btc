import unittest

from hyper.copy.copy_backtest import campaign_structure_metrics, profit_structure_metrics, run_backtest


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
    def test_overlapping_same_direction_basket_is_one_independent_campaign(self):
        positions = [
            {
                "addr": "0xaaa", "coin": f"xyz:C{i}", "side": "short", "status": "closed",
                "opened_at": 1_000 + i, "closed_at": 5_000 + i, "net_pnl": 10.0,
            }
            for i in range(10)
        ]

        metrics = campaign_structure_metrics(positions)

        self.assertEqual(metrics["campaign_closed_n"], 1)
        self.assertEqual(metrics["campaign_wins"], 1)
        self.assertEqual(metrics["campaign_max_positions"], 10)

    def test_liquidation_blocks_immediate_reentry_in_replay(self):
        fills = [
            fill(1_000, "BTC", "B", 100, 0, 100.0, 1),
            fill(2_000, "BTC", "B", 1, 100, 90.0, 2),
            fill(3_000, "BTC", "A", 101, 101, 90.0, 3),
            fill(4_000, "BTC", "B", 100, 0, 90.0, 4),
        ]

        result = run_backtest("0xabc", fills, sigmas={"BTC": 0.04})

        self.assertEqual(result["liquidations"], 1)
        self.assertEqual(result["liquidation_reentry_blocks"], 1)
        self.assertEqual(result["opened_n"], 1)

    def test_wallet_position_cap_preserves_slots_for_other_sources(self):
        fills = [
            fill(i + 1, f"C{i}", "B", 10_000, 0, 100.0, i + 1)
            for i in range(4)
        ]
        result = run_backtest("0xabc", fills, sigmas={f"C{i}": 0.06 for i in range(4)}, overrides={
            "MID_MIN_NOTIONAL": 0.0,
            "WALLET_MARGIN_CAP_PCT": 1.0,
            "WALLET_SECTOR_SIDE_CAP_PCT": 1.0,
        })

        self.assertEqual(result["opened_n"], 3)
        self.assertEqual(result["skip_reasons"].get("skip_wallet_position_cap"), 1)

    def test_profit_body_after_top3_distinguishes_repeatable_from_lottery_wallet(self):
        wallet_a = [1000, 800, 600, 40, 35, 30, 25, 20, 15, -10]
        wallet_b = [1000, 800, 600, 10, -10, -15, -20, -25, -30, -35]

        def metrics(values):
            positions = [{"status": "closed", "net_pnl": value} for value in values]
            return profit_structure_metrics(positions, total_net=sum(values), fee_drag=0)

        good = metrics(wallet_a)
        bad = metrics(wallet_b)

        self.assertAlmostEqual(good["body_after_top3_win_rate"], 6 / 7)
        self.assertGreater(good["body_after_top3_net_pnl"], 0)
        self.assertGreater(good["body_after_top3_median_pnl"], 0)
        self.assertAlmostEqual(bad["body_after_top3_win_rate"], 1 / 7)
        self.assertLess(bad["body_after_top3_net_pnl"], 0)
        self.assertLess(bad["body_after_top3_median_pnl"], 0)

    def test_manual_margin_equity_budget_scales_replay_open_and_pnl(self):
        fills = [
            fill(1, "BTC", "B", 100, 0, 100.0, 1),
            fill(2, "BTC", "A", 100, 100, 101.0, 2),
        ]
        full = run_backtest("0xabc", fills, sigmas={"BTC": 0.04}, overrides={
            "MARGIN_EQUITY_PCT": 1.0,
        })
        half = run_backtest("0xabc", fills, sigmas={"BTC": 0.04}, overrides={
            "MARGIN_EQUITY_PCT": 0.5,
        })

        self.assertEqual(full["margin_equity_pct"], 1.0)
        self.assertEqual(half["margin_equity_pct"], 0.5)
        self.assertEqual(half["initial_margin_equity"], 5_000.0)
        self.assertAlmostEqual(half["positions"][0]["margin"], full["positions"][0]["margin"] * 0.5)
        self.assertAlmostEqual(half["copy_net_pnl"], full["copy_net_pnl"] * 0.5)

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

    def test_korean_stock_preset_skips_new_open(self):
        fills = [
            fill(1_000, "xyz:EWY", "B", 100, 0, 100.0, 1),
            fill(2_000, "xyz:EWY", "A", 100, 100, 101.0, 2),
        ]

        result = run_backtest("0xabc", fills, sigmas={"xyz:EWY": 0.12}, overrides={
            "BLOCK_KOREAN_STOCKS": True,
        })

        self.assertEqual(result["target_open_events"], 1)
        self.assertEqual(result["opened_n"], 0)
        self.assertEqual(result["closed_n"], 0)
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
        self.assertEqual(result["add_outcome_counts"]["noise_merged"], 1)
        self.assertEqual(result["blocked_adds"], 0)
        self.assertEqual(result["actionable_add_capture_rate"], 1.0)
        self.assertEqual(result["behavior_replication_rate"], 1.0)
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

    def test_btc_sigma_changes_add_spacing_but_never_changes_stable_open_tier(self):
        fills = [
            fill(1, "BTC", "B", 10, 0, 100.0, 1),
            fill(2, "BTC", "B", 10, 10, 99.0, 2),
            fill(3, "BTC", "A", 20, 20, 101.0, 3),
        ]
        overrides = {
            "STABLE_MARGIN_PCT": .04, "STABLE_MARGIN_MIN_PCT": .04,
            "STABLE_LEV_CAP": 21.0, "STABLE_MIN_NOTIONAL": 0.0,
            "MID_LEV_CAP": 8.0, "HIGH_LEV_CAP": 4.0,
            "ADD_GAP_K": .10, "ADD_GAP_SHRINK_G": 1.0,
            "MIN_OPEN_MARGIN_PCT": .0001,
        }
        calm = run_backtest("0xabc", fills, sigmas={"BTC": .05}, overrides=overrides)
        volatile = run_backtest("0xabc", fills, sigmas={"BTC": .20}, overrides=overrides)

        self.assertEqual(calm["positions"][0]["leverage"], 21.0)
        self.assertEqual(volatile["positions"][0]["leverage"], 21.0)
        self.assertEqual(calm["followed_adds"], 1)
        self.assertEqual(volatile["followed_adds"], 0)
        self.assertEqual(volatile["add_outcome_counts"]["noise_merged"], 1)

    def test_large_target_add_is_capped_to_one_first_margin_in_replay(self):
        fills = [
            fill(1, "BTC", "B", 100, 0, 100.0, 20),
            fill(2, "BTC", "B", 200, 100, 98.0, 21),  # target adds 2x its first order
            fill(3, "BTC", "A", 300, 300, 101.0, 22),
        ]
        result = run_backtest("0xabc", fills, sigmas={"BTC": 0.04}, overrides={
            "STABLE_MARGIN_PCT": 0.085,
            "STABLE_MARGIN_MIN_PCT": 0.085,
            "STABLE_COIN_CAP_PCT": 0.40,
            "STABLE_LEV_CAP": 1.0,
            "STABLE_MIN_NOTIONAL": 0.0,
            "ADD_GAP_K": 0.01,
            "ADD_GAP_SHRINK_G": 1.0,
            "WALLET_MARGIN_CAP_PCT": 1.0,
            "WALLET_SECTOR_SIDE_CAP_PCT": 1.0,
            "MAX_TOTAL_MARGIN_PCT": 1.0,
        })

        self.assertEqual(result["followed_adds"], 1)
        self.assertAlmostEqual(result["positions"][0]["margin"], 1700.0)

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
        self.assertEqual(result["add_outcome_counts"]["followed"], 1)
        self.assertEqual(result["add_outcome_counts"]["noise_merged"], 0)
        self.assertGreater(result["copy_net_pnl"], 0)

    def test_true_add_cap_block_is_not_classified_as_noise(self):
        fills = [
            fill(1, "ZEC", "B", 10, 0, 100.0, 1),
            fill(2, "ZEC", "B", 10, 10, 98.0, 2),
            fill(3, "ZEC", "A", 20, 20, 101.0, 3),
        ]

        result = run_backtest("0xabc", fills, sigmas={"ZEC": 0.10}, overrides={
            "ADD_MAX_HARD": 0,
        })

        self.assertEqual(result["add_outcome_counts"]["hard_cap_blocked"], 1)
        self.assertEqual(result["add_outcome_counts"]["noise_merged"], 0)
        self.assertEqual(result["actionable_add_capture_rate"], 0.0)

    def test_fewer_than_five_add_episodes_keeps_fidelity_audit_only(self):
        fills = [
            fill(1, "ZEC", "B", 10, 0, 100.0, 1),
            fill(2, "ZEC", "B", 10, 10, 98.0, 2),
            fill(3, "ZEC", "A", 20, 20, 101.0, 3),
        ]
        result = run_backtest("0xabc", fills, sigmas={"ZEC": 0.10})

        self.assertFalse(result["add_fidelity_applied"])
        self.assertEqual(result["effective_add_fidelity"], 1.0)
        self.assertGreaterEqual(result["entry_gap_pct_weighted"], 0.0)

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
            "MID_MARGIN_MIN_PCT": 0.02,
            "MID_MARGIN_PCT": 0.04,
            "MID_LEV_CAP": 10.0,
            "MID_MIN_NOTIONAL": 0.0,
            "MID_COIN_CAP_PCT": 1.0,
            "DEPLOY_FULL_PCT": 0.08,
            "MAX_DEPLOY_PCT": 0.50,
            "WALLET_MARGIN_CAP_PCT": 1.0,
            "WALLET_SECTOR_SIDE_CAP_PCT": 1.0,
            "WALLET_MAX_OPEN_POSITIONS": 20,
        })

        margins = [p["margin"] for p in sorted(result["open_positions"], key=lambda p: p["opened_at"])]

        self.assertGreaterEqual(len(margins), 6)
        self.assertGreater(margins[0], 390)
        self.assertGreater(margins[1], 390)
        self.assertLess(margins[3], 390)
        self.assertGreater(margins[3], 300)
        self.assertGreater(margins[-1], 190)
        self.assertLess(margins[-1], margins[3])

    def test_single_wallet_replay_caps_same_stock_direction_across_coins(self):
        fills = [
            fill(1, "xyz:AAA", "A", 1_000, 0, 100.0, 1),
            fill(2, "xyz:BBB", "A", 1_000, 0, 100.0, 2),
            fill(3, "xyz:CCC", "A", 1_000, 0, 100.0, 3),
        ]
        result = run_backtest("0xaaa", fills, sigmas={
            "xyz:AAA": 0.06, "xyz:BBB": 0.06, "xyz:CCC": 0.06,
        }, overrides={
            "WALLET_SECTOR_SIDE_CAP_PCT": 0.05,
            "MID_MARGIN_PCT": 0.03,
            "MID_MARGIN_MIN_PCT": 0.02,
            "MID_COIN_CAP_PCT": 1.0,
            "MID_MIN_NOTIONAL": 0.0,
        })

        used = sum(position["margin"] for position in result["open_positions"])
        self.assertEqual(result["wallet_sector_side_cap_pct"], 0.05)
        self.assertGreater(used, 499.0)
        self.assertLessEqual(used, 500.0)
        self.assertEqual(result["opened_n"], 2)
        self.assertEqual(result["skip_reasons"].get("skip_wallet_sector_side_full"), 1)

    def test_portfolio_replay_gives_each_wallet_an_independent_group_cap(self):
        fills = [
            user_fill("0xaaa", 1, "xyz:AAA", "A", 1_000, 0, 100.0, 1),
            user_fill("0xbbb", 2, "xyz:BBB", "A", 1_000, 0, 100.0, 2),
            user_fill("0xaaa", 3, "xyz:CCC", "A", 1_000, 0, 100.0, 3),
            user_fill("0xbbb", 4, "xyz:DDD", "A", 1_000, 0, 100.0, 4),
        ]
        result = run_backtest("portfolio", fills, sigmas={
            coin: 0.06 for coin in ("xyz:AAA", "xyz:BBB", "xyz:CCC", "xyz:DDD")
        }, overrides={
            "WALLET_SECTOR_SIDE_CAP_PCT": 0.05,
            "MID_MARGIN_PCT": 0.03,
            "MID_MARGIN_MIN_PCT": 0.02,
            "MID_COIN_CAP_PCT": 1.0,
            "MID_MIN_NOTIONAL": 0.0,
        })

        by_wallet = {}
        for position in result["open_positions"]:
            by_wallet[position["addr"]] = by_wallet.get(position["addr"], 0.0) + position["margin"]
        self.assertEqual(result["opened_n"], 4)
        self.assertGreater(by_wallet["0xaaa"], 499.0)
        self.assertLessEqual(by_wallet["0xaaa"], 500.0)
        self.assertGreater(by_wallet["0xbbb"], 499.0)
        self.assertLessEqual(by_wallet["0xbbb"], 500.0)

    def test_price_path_can_liquidate_between_target_fills(self):
        fills = [
            fill(1_000, "BTC", "B", 100, 0, 100.0, 40),
            fill(3_000, "BTC", "A", 100, 100, 101.0, 41),
        ]

        fills_only = run_backtest("0xabc", fills, sigmas={"BTC": 0.04})
        with_path = run_backtest(
            "0xabc",
            fills,
            sigmas={"BTC": 0.04},
            price_path={"BTC": [{"time": 2_000, "low": 95.0, "high": 100.0}]},
        )

        self.assertEqual(fills_only["positions"][0]["status"], "closed")
        self.assertEqual(fills_only["positions"][0]["opened_at"], 1_000)
        self.assertEqual(fills_only["positions"][0]["closed_at"], 3_000)
        self.assertGreater(fills_only["copy_net_pnl"], 0)
        self.assertEqual(with_path["positions"][0]["status"], "liquidated")
        self.assertEqual(with_path["liquidations"], 1)
        self.assertEqual(with_path["path_completion_rate"], 0.0)
        self.assertEqual(with_path["behavior_replication_rate"], 0.0)
        self.assertLess(with_path["copy_net_pnl"], 0)

    def test_price_path_adverse_move_without_liquidation_does_not_force_close(self):
        fills = [
            fill(1_000, "BTC", "B", 100, 0, 100.0, 50),
            fill(3_000, "BTC", "A", 100, 100, 101.0, 51),
        ]

        result = run_backtest(
            "0xabc",
            fills,
            sigmas={"BTC": 0.04},
            price_path={"BTC": [{"time": 2_000, "low": 97.0, "high": 100.0}]},
        )

        self.assertEqual(result["positions"][0]["status"], "closed")
        self.assertEqual(result["positions"][0]["closed_at"], 3_000)
        self.assertNotIn("stops", result)
        self.assertEqual(result["liquidations"], 0)

    def test_fill_candle_crossing_is_ambiguous_not_confirmed(self):
        fills = [
            fill(1_000, "BTC", "B", 100, 0, 100.0, 60),
            fill(3_000, "BTC", "A", 100, 100, 101.0, 61),
        ]
        path = [{"coin": "BTC", "time": 2_000, "open_time": 1, "close_time": 2_000,
                 "low": 95.0, "high": 101.0, "close": 100.0}]
        best = run_backtest("0xabc", fills, sigmas={"BTC": 0.04}, price_path=path)
        worst = run_backtest("0xabc", fills, sigmas={"BTC": 0.04},
                             overrides={"AMBIGUOUS_PATH_MODE": "liquidate"},
                             price_path=path)
        self.assertEqual(0, best["liquidations"])
        self.assertEqual(1, best["ambiguous_liquidations"])
        self.assertEqual(1, worst["liquidations"])
        self.assertLess(worst["copy_net_pnl"], best["copy_net_pnl"])

    def test_long_to_short_flip_closes_old_position_and_opens_new_one(self):
        fills = [
            fill(1_000, "BTC", "B", 100, 0, 100.0, 70),
            fill(2_000, "BTC", "A", 200, 100, 99.0, 71),
            fill(3_000, "BTC", "B", 100, -100, 98.0, 72),
        ]

        result = run_backtest("0xabc", fills, sigmas={"BTC": 0.04}, overrides={
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
            "MID_MIN_NOTIONAL": 0.0,
        })

        self.assertEqual(result["closed_n"], 1)
        self.assertEqual(len(result["open_positions"]), 0)
        self.assertEqual(result["positions"][0]["status"], "closed")
        self.assertEqual(result["positions"][0]["closed_at"], 2_000)

    def test_profitable_risky_tail_closes_on_target_reduce(self):
        fills = [
            fill(1_000, "ETH", "B", 100, 0, 100.0, 100),
            fill(2_000, "ETH", "A", 65, 100, 110.0, 101),
            fill(3_000, "ETH", "A", 35, 35, 80.0, 102),
        ]

        result = run_backtest("0xabc", fills, sigmas={"ETH": 0.08}, overrides={
            "MID_MIN_NOTIONAL": 0.0,
            "TAIL_CLOSE_ENABLE": True,
        })

        self.assertEqual(result["closed_n"], 1)
        self.assertEqual(result["tail_profit_closes"], 1)
        self.assertEqual(result["positions"][0]["status"], "tail_closed")
        self.assertEqual(result["positions"][0]["closed_at"], 2_000)
        self.assertGreater(result["copy_net_pnl"], 0)

    def test_losing_small_tail_is_not_force_closed(self):
        fills = [
            fill(1_000, "ETH", "B", 100, 0, 100.0, 110),
            fill(2_000, "ETH", "A", 80, 100, 95.0, 111),
        ]

        result = run_backtest("0xabc", fills, sigmas={"ETH": 0.08}, overrides={
            "MID_MIN_NOTIONAL": 0.0,
            "TAIL_CLOSE_ENABLE": True,
        })

        self.assertEqual(result["closed_n"], 0)
        self.assertEqual(result["tail_profit_closes"], 0)
        self.assertEqual(len(result["open_positions"]), 1)

    def test_smart_take_profit_replays_three_cuts_and_exits_tail_after_target_reduces_thirty_pct(self):
        fills = [
            fill(1_000, "ZEC", "B", 100, 0, 100.0, 120),
            fill(5_000, "ZEC", "A", 29, 100, 109.0, 121),
            fill(6_000, "ZEC", "A", 1, 71, 109.0, 122),
        ]
        path = {"ZEC": [
            {"time": 2_000, "low": 103.0, "high": 104.0, "close": 103.1},
            {"time": 3_000, "low": 106.0, "high": 110.0, "close": 106.4},
            {"time": 4_000, "low": 108.0, "high": 120.0, "close": 109.0},
        ]}

        result = run_backtest(
            "0xabc",
            fills,
            sigmas={"ZEC": 0.10},
            price_path=path,
            overrides={"SMART_TP_ENABLE": True, "HIGH_MIN_NOTIONAL": 0.0},
        )

        self.assertEqual(result["closed_n"], 1)
        self.assertEqual(result["tail_profit_closes"], 1)
        self.assertEqual(result["positions"][0]["status"], "tail_closed")
        self.assertEqual(result["positions"][0]["closed_at"], 6_000)
        self.assertEqual(result["skip_reasons"].get("smart_tp_cut"), 3)
        self.assertAlmostEqual(result["positions"][0]["remaining_size"], 0.0, places=8)

    def test_smart_take_profit_is_disabled_by_default(self):
        fills = [fill(1_000, "ZEC", "B", 100, 0, 100.0, 130)]
        path = {"ZEC": [
            {"time": 2_000, "low": 103.0, "high": 104.0, "close": 103.1},
            {"time": 3_000, "low": 106.0, "high": 110.0, "close": 106.4},
            {"time": 4_000, "low": 108.0, "high": 120.0, "close": 109.0},
        ]}

        result = run_backtest(
            "0xabc", fills, sigmas={"ZEC": 0.10}, price_path=path,
            overrides={"HIGH_MIN_NOTIONAL": 0.0},
        )

        self.assertEqual(result["skip_reasons"].get("smart_tp_cut"), None)
        self.assertEqual(result["open_n"], 1)
        self.assertGreater(result["open_positions"][0]["remaining_size"], 0)


if __name__ == "__main__":
    unittest.main()
