import unittest

from hl.copy_engine import OpenSizingParams, plan_open_sizing


class CopyEngineTests(unittest.TestCase):
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
