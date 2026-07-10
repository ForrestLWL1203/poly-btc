import unittest

from hl.copy_engine import OpenSizingParams, plan_open_sizing
from hl.sizing import sizing_equity_for_drawdown


class CopyEngineTests(unittest.TestCase):
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
            copy_stop_enable=False,
            stop_margin_pct=0.70,
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
        self.assertAlmostEqual(capped_plan.margin, 10.0)

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
            copy_stop_enable=True,
            stop_margin_pct=0.70,
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
        self.assertAlmostEqual(plan.stop_px, 86.0)


if __name__ == "__main__":
    unittest.main()
