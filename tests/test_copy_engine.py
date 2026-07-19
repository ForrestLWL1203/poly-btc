import unittest
from dataclasses import replace

from hl.copy_engine import (OpenSizingParams, plan_open_sizing, profit_tail_close_decision,
                            smart_add_order_margin)
from hl.sizing import sizing_equity_for_drawdown


class CopyEngineTests(unittest.TestCase):
    def test_all_tiers_reserve_four_adds_and_last_add_fills_remaining_cap(self):
        params = OpenSizingParams(
            stable_sigma_max=0.05,
            high_sigma_min=0.09,
            tier_margin={"stable": 0.085, "mid": 0.05274, "high": 0.03625},
            tier_margin_min={"stable": 0.02, "mid": 0.02, "high": 0.012},
            tier_lev_cap={"stable": 20.0, "mid": 9.0, "high": 6.0},
            tier_min_notional={"stable": 0.0, "mid": 0.0, "high": 0.0},
            tier_coin_cap={"stable": 0.40, "mid": 0.22, "high": 0.15},
            min_lev=1.0,
            stock_max_lev=10.0,
            deploy_full_pct=0.40,
            max_deploy_pct=0.80,
            min_open_margin_pct=0.005,
        )
        cases = (("BTC", 0.04, 850.0), ("ETH", 0.06, 527.4), ("ZEC", 0.12, 362.5))
        for coin, sigma, expected_margin in cases:
            with self.subTest(coin=coin):
                plan = plan_open_sizing(
                    coin=coin, side="long", entry_px=100.0, sigma=sigma,
                    balance=10_000.0, available=10_000.0, existing_coin_margin=0.0,
                    master_notional=1_000_000.0, master_leverage=20.0, params=params,
                )
                self.assertTrue(plan.ok)
                self.assertAlmostEqual(plan.margin, expected_margin)

        # BTC: open + three full adds = 34%; the fourth add fills the remaining 6%.
        self.assertEqual(smart_add_order_margin(
            first_margin=850.0, target_ratio=2.0, followed_margin=0.0,
            coin_room=600.0, risk_available=10_000.0,
        ), 600.0)
        self.assertEqual(smart_add_order_margin(
            first_margin=850.0, target_ratio=2.0, followed_margin=0.0,
            coin_room=0.0, risk_available=10_000.0,
        ), 0.0)

    def test_one_large_target_add_cannot_consume_multiple_slots(self):
        self.assertEqual(smart_add_order_margin(
            first_margin=850.0, target_ratio=2.0, followed_margin=0.0,
            coin_room=4_000.0, risk_available=10_000.0,
        ), 850.0)
        # Later slices of the same target order can only top that order up to one first margin.
        self.assertEqual(smart_add_order_margin(
            first_margin=850.0, target_ratio=2.0, followed_margin=200.0,
            coin_room=3_800.0, risk_available=10_000.0,
        ), 650.0)

    def test_drawdown_sizing_compounds_gains_and_smooths_losses(self):
        self.assertEqual(sizing_equity_for_drawdown(15_000, 10_000), 15_000)
        self.assertEqual(sizing_equity_for_drawdown(10_000, 10_000), 10_000)
        self.assertAlmostEqual(sizing_equity_for_drawdown(5_000, 10_000), 7071.0678, places=3)
        self.assertEqual(sizing_equity_for_drawdown(2_500, 10_000), 3750.0)
        self.assertEqual(sizing_equity_for_drawdown(1_000, 10_000), 1500.0)

    def test_open_sizing_uses_smoothed_base_but_real_equity_caps(self):
        params = OpenSizingParams(
            stable_sigma_max=0.05,
            high_sigma_min=0.10,
            tier_margin={"stable": 0.01, "mid": 0.01, "high": 0.01},
            tier_margin_min={"stable": 0.01, "mid": 0.01, "high": 0.01},
            tier_lev_cap={"stable": 25.0, "mid": 10.0, "high": 4.0},
            tier_min_notional={"stable": 0.0, "mid": 0.0, "high": 0.0},
            tier_coin_cap={"stable": 0.30, "mid": 0.22, "high": 0.15},
            min_lev=1.0,
            stock_max_lev=10.0,
            deploy_full_pct=0.40,
            max_deploy_pct=0.80,
            min_open_margin_pct=0.001,
            capital_anchor=10_000.0,
        )

        loss_plan = plan_open_sizing(
            coin="BTC", side="long", entry_px=100.0, sigma=0.04,
            balance=5_000.0, available=5_000.0, existing_coin_margin=0.0,
            master_notional=100_000.0, master_leverage=25.0, params=params,
        )
        gain_plan = plan_open_sizing(
            coin="BTC", side="long", entry_px=100.0, sigma=0.04,
            balance=15_000.0, available=15_000.0, existing_coin_margin=0.0,
            master_notional=100_000.0, master_leverage=25.0, params=params,
        )
        capped_plan = plan_open_sizing(
            coin="BTC", side="long", entry_px=100.0, sigma=0.04,
            balance=5_000.0, available=5_000.0, existing_coin_margin=1490.0,
            master_notional=100_000.0, master_leverage=25.0, params=params,
        )

        self.assertTrue(loss_plan.ok)
        self.assertAlmostEqual(loss_plan.sizing_equity, 7071.0678, places=3)
        self.assertAlmostEqual(loss_plan.margin, 70.7107, places=3)
        self.assertEqual(loss_plan.risk_equity, 5_000.0)
        self.assertAlmostEqual(gain_plan.margin, 150.0)
        self.assertAlmostEqual(capped_plan.room, 10.0)
        self.assertFalse(capped_plan.ok)
        self.assertEqual(capped_plan.reason, "coin_full")

    def test_margin_equity_pct_scales_open_only_and_keeps_full_account_caps(self):
        params = OpenSizingParams(
            stable_sigma_max=0.05,
            high_sigma_min=0.10,
            tier_margin={"stable": 0.01, "mid": 0.01, "high": 0.01},
            tier_margin_min={"stable": 0.01, "mid": 0.01, "high": 0.01},
            tier_lev_cap={"stable": 10.0, "mid": 10.0, "high": 4.0},
            tier_min_notional={"stable": 0.0, "mid": 0.0, "high": 0.0},
            tier_coin_cap={"stable": 0.30, "mid": 0.22, "high": 0.15},
            min_lev=1.0,
            stock_max_lev=10.0,
            deploy_full_pct=0.40,
            max_deploy_pct=0.80,
            min_open_margin_pct=0.005,
            margin_equity_pct=0.50,
        )
        plan = plan_open_sizing(
            coin="BTC", side="long", entry_px=100.0, sigma=0.04,
            balance=10_000.0, available=10_000.0, existing_coin_margin=0.0,
            master_notional=100_000.0, master_leverage=10.0, params=params,
        )

        self.assertTrue(plan.ok)
        self.assertEqual(plan.risk_equity, 10_000.0)
        self.assertEqual(plan.sizing_equity, 10_000.0)
        self.assertEqual(plan.margin_equity, 5_000.0)
        self.assertEqual(plan.margin, 50.0)
        self.assertEqual(plan.room, 3_000.0)
        self.assertEqual(plan.deploy_room, 8_000.0)

        # The proportional dust floor follows the $5k sizing base, while the fixed notional floor remains real.
        dust_ok = replace(params, tier_margin={"stable": 0.006, "mid": 0.006, "high": 0.006})
        self.assertTrue(plan_open_sizing(
            coin="BTC", side="long", entry_px=100.0, sigma=0.04,
            balance=10_000.0, available=10_000.0, existing_coin_margin=0.0,
            master_notional=100_000.0, master_leverage=10.0, params=dust_ok,
        ).ok)
        fixed_floor = replace(params, tier_min_notional={"stable": 600.0, "mid": 600.0, "high": 600.0})
        self.assertEqual(plan_open_sizing(
            coin="BTC", side="long", entry_px=100.0, sigma=0.04,
            balance=10_000.0, available=10_000.0, existing_coin_margin=0.0,
            master_notional=100_000.0, master_leverage=10.0, params=fixed_floor,
        ).reason, "small_notl")

    def test_open_sizing_caps_leverage_to_master_and_master_notional(self):
        params = OpenSizingParams(
            stable_sigma_max=0.05,
            high_sigma_min=0.10,
            tier_margin={"stable": 0.015, "mid": 0.02, "high": 0.01},
            tier_margin_min={"stable": 0.015, "mid": 0.02, "high": 0.01},
            tier_lev_cap={"stable": 25.0, "mid": 10.0, "high": 4.0},
            tier_min_notional={"stable": 0.0, "mid": 0.0, "high": 0.0},
            tier_coin_cap={"stable": 0.30, "mid": 0.22, "high": 0.15},
            min_lev=1.0,
            stock_max_lev=10.0,
            deploy_full_pct=0.40,
            max_deploy_pct=0.80,
            min_open_margin_pct=0.001,
        )

        plan = plan_open_sizing(
            coin="BTC",
            side="long",
            entry_px=100.0,
            sigma=0.04,
            balance=10_000.0,
            available=10_000.0,
            existing_coin_margin=0.0,
            master_notional=500.0,
            master_leverage=5.0,
            params=params,
        )

        self.assertTrue(plan.ok)
        self.assertEqual(plan.tier, "stable")
        self.assertEqual(plan.leverage, 5.0)
        self.assertEqual(plan.notional, 500.0)
        self.assertEqual(plan.margin, 100.0)
        self.assertEqual(plan.size, 5.0)
        self.assertAlmostEqual(plan.liq_px, 80.0)

    def test_open_sizing_never_exceeds_market_max_leverage(self):
        params = OpenSizingParams(
            stable_sigma_max=0.05,
            high_sigma_min=0.10,
            tier_margin={"stable": 0.04, "mid": 0.03, "high": 0.02},
            tier_margin_min={"stable": 0.02, "mid": 0.02, "high": 0.012},
            tier_lev_cap={"stable": 35.0, "mid": 12.0, "high": 4.0},
            tier_min_notional={"stable": 0.0, "mid": 0.0, "high": 0.0},
            tier_coin_cap={"stable": 0.30, "mid": 0.22, "high": 0.15},
            min_lev=1.0,
            stock_max_lev=10.0,
            deploy_full_pct=0.40,
            max_deploy_pct=0.80,
            min_open_margin_pct=0.001,
        )

        plan = plan_open_sizing(
            coin="ETH", side="long", entry_px=2_000.0, sigma=0.046,
            balance=10_000.0, available=10_000.0, existing_coin_margin=0.0,
            master_notional=100_000.0, master_leverage=None, params=params,
            maintenance_leverage=25.0,
        )

        self.assertTrue(plan.ok)
        self.assertEqual(plan.tier, "mid")
        self.assertEqual(plan.leverage, 12.0)

    def test_only_btc_is_eligible_for_stable_tier(self):
        from hl.copy_engine import tier_for_sigma

        self.assertEqual(tier_for_sigma(0.04, 0.05, 0.10, "BTC"), "stable")
        self.assertEqual(tier_for_sigma(0.04, 0.05, 0.10, "ETH"), "mid")
        self.assertEqual(tier_for_sigma(0.04, 0.05, 0.10, "XRP"), "mid")
        self.assertEqual(tier_for_sigma(0.04, 0.05, 0.10, "xyz:GOLD"), "mid")
        self.assertEqual(tier_for_sigma(0.07, 0.05, 0.10, "BTC"), "stable")
        self.assertEqual(tier_for_sigma(0.20, 0.05, 0.10, "BTC"), "stable")
        self.assertEqual(tier_for_sigma(0.07, 0.05, 0.09, "ETH"), "mid")
        self.assertEqual(tier_for_sigma(0.10, 0.05, 0.09, "ETH"), "high")

    def test_profit_tail_uses_percentages_and_asset_liquidation_risk(self):
        decision = profit_tail_close_decision(
            rem_size=1578.73231,
            peak_size=4007.55125,
            reduce_frac=242.88189 / 1578.73231,
            execution_px=0.14868,
            risk_px=0.14868,
            entry_px=0.14898,
            side="long",
            realized_pnl=59.30,
            liq_px=0.119184,
            fee_rate=0.00045,
        )

        self.assertTrue(decision.close)
        self.assertEqual(decision.reason, "liq_risk_profit_tail")
        self.assertAlmostEqual(decision.remaining_fraction, 1 / 3, places=3)
        self.assertGreater(decision.giveback_fraction, 0.6)

    def test_profit_tail_never_turns_into_a_loss_stop(self):
        decision = profit_tail_close_decision(
            rem_size=30,
            peak_size=100,
            reduce_frac=0.5,
            execution_px=90,
            risk_px=90,
            entry_px=100,
            side="long",
            realized_pnl=0,
            liq_px=80,
            fee_rate=0.00045,
        )

        self.assertFalse(decision.close)
        self.assertLess(decision.close_now_profit, 0)

    def test_hard_profit_tail_is_direction_symmetric(self):
        decision = profit_tail_close_decision(
            rem_size=25,
            peak_size=100,
            reduce_frac=0.4,
            execution_px=90,
            risk_px=90,
            entry_px=100,
            side="short",
            realized_pnl=10,
            liq_px=120,
            fee_rate=0.00045,
        )

        self.assertTrue(decision.close)
        self.assertEqual(decision.reason, "hard_profit_tail")
        self.assertAlmostEqual(decision.remaining_fraction, 0.15)


if __name__ == "__main__":
    unittest.main()
