import json
import unittest

from hyper.copy import sector


def bt(net, closed, wins=None, target_open=None, opened=None):
    wins = closed if wins is None else wins
    return {
        "copy_net_pnl": net,
        "closed_n": closed,
        "wins": wins,
        "campaign_closed_n": closed,
        "campaign_wins": wins,
        "campaign_net_after_top2": net * 0.25,
        "cost_stress_net_pnl": net * 0.75,
        "profit_factor": 2.0 if net > 0 else 0.5,
        "evidence_days": min(5, closed),
        "target_open_events": closed if target_open is None else target_open,
        "opened_n": closed if opened is None else opened,
        "liquidations": 0,
        "fee_drag": 10,
    }


DAY_MS = 86400_000


def replay_window(net, returns, *, now_ms, liquidations=0):
    positions = []
    for index, value in enumerate(returns):
        positions.append({
            "net_pnl": value * 100.0,
            "margin": 100.0,
            "closed_at": now_ms - (len(returns) - index) * DAY_MS,
        })
    return {
        **bt(net, len(returns)),
        "liquidations": liquidations,
        "positions": positions,
        "open_positions": [],
        "unrealized_pnl": 0.0,
        "_window_end_ms": now_ms,
    }


class SectorPolicyTests(unittest.TestCase):
    def test_classifies_crypto_and_xyz_stock_sectors(self):
        self.assertEqual(sector.classify_coin("BTC"), "crypto")
        self.assertEqual(sector.classify_coin("HYPE"), "crypto")
        self.assertEqual(sector.classify_coin("xyz:SP500"), "stock")
        self.assertEqual(sector.classify_coin("XYZ:MU"), "stock")
        self.assertIsNone(sector.classify_coin("#4830"))
        self.assertIsNone(sector.classify_coin("BTC/USDC"))
        self.assertIsNone(sector.classify_coin("vntl:OPENAI"))

    def test_policy_allows_profitable_sector_and_denies_losing_sector(self):
        sector_results = {
            "crypto": {
                30: bt(1200, 15),
                14: bt(600, 7),
                7: bt(600, 5),
            },
            "stock": {
                30: bt(-900, 10, wins=2),
                14: bt(-500, 6, wins=1),
                7: bt(-200, 5, wins=1),
            },
        }

        policy = sector.evaluate_sector_policy(sector_results)

        self.assertTrue(policy["crypto"]["allow"])
        self.assertEqual(policy["crypto"]["status"], "allowed")
        self.assertFalse(policy["stock"]["allow"])
        self.assertEqual(policy["stock"]["status"], "sector_not_profitable")
        self.assertEqual(policy["allowed"], ["crypto"])

    def test_current_generation_structure_limits_profitable_sector_without_prior_policy(self):
        sector_results = {
            "crypto": {30: bt(1800, 15), 14: bt(900, 7), 7: bt(600, 5)},
            "stock": {30: bt(2200, 12), 14: bt(1100, 7), 7: bt(700, 5)},
        }
        structure = {
            "source": "current_generation",
            "crypto": {"allow": True, "status": "structural_ok"},
            "stock": {"allow": False, "status": "grid_dca", "reason": "本轮股票板块网格"},
        }

        policy = sector.evaluate_sector_policy(
            sector_results, previous_policy=None, structural_policy=structure,
        )

        self.assertEqual(policy["allowed"], ["crypto"])
        self.assertTrue(policy["crypto"]["allow"])
        self.assertFalse(policy["stock"]["allow"])
        self.assertEqual(policy["stock"]["status"], "grid_dca")
        self.assertEqual(policy["specializationSource"], "current_generation")

    def test_profitable_copyable_mix_wallet_keeps_both_sectors(self):
        sector_results = {
            "crypto": {30: bt(2400, 15), 14: bt(1400, 9), 7: bt(800, 5)},
            "stock": {30: bt(3100, 20), 14: bt(1700, 11), 7: bt(900, 6)},
        }
        structure = {
            "source": "current_generation",
            "crypto": {"allow": True, "status": "structural_ok"},
            "stock": {"allow": True, "status": "structural_ok"},
        }

        policy = sector.evaluate_sector_policy(sector_results, structural_policy=structure)

        self.assertEqual(policy["allowed"], ["crypto", "stock"])
        self.assertTrue(policy["crypto"]["allow"])
        self.assertTrue(policy["stock"]["allow"])
        self.assertFalse(policy["coreBlocked"])

    def test_single_heavy_dca_pressure_pass_can_enter_core(self):
        sector_results = {
            "crypto": {30: bt(2200, 15), 14: bt(1000, 7), 7: bt(600, 5)},
        }
        structure = {
            "source": "current_generation",
            "crypto": {"allow": True, "watch": True, "status": "heavy_dca_watch"},
        }

        policy = sector.evaluate_sector_policy(sector_results, structural_policy=structure)

        self.assertEqual(policy["allowed"], ["crypto"])
        self.assertEqual(policy["structuralWatch"], ["crypto"])
        self.assertFalse(policy["coreBlocked"])
        self.assertTrue(policy["crypto"]["allow"])
        self.assertFalse(policy["crypto"]["coreBlocked"])
        self.assertEqual(policy["crypto"]["status"], "heavy_dca_pressure_passed")

    def test_profitable_thin_sector_is_sample_watch_not_live_allowed(self):
        sector_results = {
            "crypto": {30: bt(1900, 2), 14: bt(1900, 2), 7: bt(1900, 2)},
        }
        structure = {
            "source": "current_generation",
            "crypto": {"allow": True, "status": "structural_ok"},
        }

        policy = sector.evaluate_sector_policy(sector_results, structural_policy=structure)

        self.assertEqual(policy["allowed"], [])
        self.assertEqual(policy["watch"], ["crypto"])
        self.assertFalse(policy["crypto"]["allow"])
        self.assertTrue(policy["crypto"]["watch"])
        self.assertEqual(policy["crypto"]["status"], "sector_sample_watch")

    def test_single_heavy_dca_with_recent_loss_fails_pressure_validation(self):
        sector_results = {
            "crypto": {30: bt(2200, 12), 14: bt(1000, 7), 7: bt(-100, 5)},
        }
        structure = {
            "source": "current_generation",
            "crypto": {"allow": True, "watch": True, "status": "heavy_dca_watch"},
        }

        policy = sector.evaluate_sector_policy(sector_results, structural_policy=structure)

        self.assertEqual(policy["allowed"], [])
        self.assertFalse(policy["coreBlocked"])
        self.assertFalse(policy["crypto"]["allow"])
        self.assertEqual(policy["crypto"]["status"], "heavy_dca_pressure_failed")

    def test_mix_wallet_keeps_clean_and_pressure_tested_heavy_dca_sectors(self):
        sector_results = {
            "crypto": {30: bt(2200, 15), 14: bt(1000, 7), 7: bt(600, 5)},
            "stock": {30: bt(1800, 15), 14: bt(900, 7), 7: bt(500, 5)},
        }
        structure = {
            "source": "current_generation",
            "crypto": {"allow": True, "status": "structural_ok"},
            "stock": {"allow": True, "watch": True, "status": "heavy_dca_watch"},
        }

        policy = sector.evaluate_sector_policy(sector_results, structural_policy=structure)

        self.assertEqual(policy["allowed"], ["crypto", "stock"])
        self.assertFalse(policy["coreBlocked"])
        self.assertTrue(policy["crypto"]["allow"])
        self.assertTrue(policy["stock"]["allow"])
        self.assertEqual(policy["stock"]["status"], "heavy_dca_pressure_passed")

    def test_recent_loss_is_diagnostic_when_thirty_day_sector_copy_remains_profitable(self):
        sector_results = {
            "crypto": {
                30: bt(1671, 40),
                14: bt(1773, 25),
                7: bt(-154, 6, wins=2),
            },
        }

        policy = sector.evaluate_sector_policy(sector_results)

        self.assertTrue(policy["crypto"]["allow"])
        self.assertEqual(policy["crypto"]["status"], "allowed")
        self.assertEqual(policy["allowed"], ["crypto"])
        self.assertEqual(policy["crypto"]["recent"]["classification"], "insufficient_recent")

    def test_shallow_recent_loss_does_not_duplicate_wallet_core_gate(self):
        now_ms = 100 * DAY_MS
        baseline_returns = [-0.20, -0.10, 0.00, 0.05, 0.10, 0.20, 0.30, 0.40]
        recent_returns = [-0.01] * 8
        primary = replay_window(1200, baseline_returns, now_ms=now_ms - 8 * DAY_MS)
        primary["_window_end_ms"] = now_ms
        recent = replay_window(-8, recent_returns, now_ms=now_ms)
        sector_results = {
            "crypto": {
                30: primary,
                14: bt(500, 12),
                7: recent,
            },
        }

        policy = sector.evaluate_sector_policy(sector_results)

        self.assertTrue(policy["crypto"]["allow"])
        self.assertEqual(policy["crypto"]["status"], "allowed")
        self.assertEqual(policy["watch"], [])
        self.assertEqual(policy["crypto"]["recent"]["classification"], "shallow_loss")
        self.assertFalse(policy["crypto"]["recent"]["hard"])

    def test_significant_recent_loss_has_no_previous_policy_grace(self):
        now_ms = 100 * DAY_MS
        baseline_returns = [0.08, 0.09, 0.10, 0.11, 0.12, 0.09, 0.10, 0.11]
        recent_returns = [-0.10] * 8
        primary = replay_window(1200, baseline_returns, now_ms=now_ms - 8 * DAY_MS)
        primary["_window_end_ms"] = now_ms
        recent = replay_window(-80, recent_returns, now_ms=now_ms)
        sector_results = {
            "crypto": {
                30: primary,
                14: bt(500, 12),
                7: recent,
            },
        }
        previous = {"crypto": {"allow": True, "status": "allowed"}, "allowed": ["crypto"]}

        first = sector.evaluate_sector_policy(sector_results, previous_policy=previous)
        repeated = sector.evaluate_sector_policy(sector_results, previous_policy=first)

        self.assertTrue(first["crypto"]["allow"])
        self.assertEqual(first["crypto"]["status"], "allowed")
        self.assertEqual(first["crypto"]["recent"]["streak"], 0)
        self.assertTrue(repeated["crypto"]["allow"])
        self.assertEqual(first["crypto"]["recent"]["classification"], "significant_loss")
        self.assertEqual(repeated["crypto"]["recent"]["streak"], 0)

    def test_mix_wallet_does_not_apply_a_fixed_seven_day_return_gate(self):
        sector_results = {
            "crypto": {
                30: bt(1227, 30),
                14: bt(900, 18),
                7: bt(1415, 26),
            },
            "stock": {
                30: bt(2371, 32, wins=23),
                14: bt(1012, 13, wins=9),
                7: bt(-21, 5, wins=3),
            },
        }

        policy = sector.evaluate_sector_policy(sector_results)

        self.assertEqual(policy["allowed"], ["crypto", "stock"])
        self.assertEqual(policy["watch"], [])
        self.assertTrue(policy["crypto"]["allow"])
        self.assertTrue(policy["stock"]["allow"])
        self.assertEqual(policy["stock"]["status"], "allowed")

    def test_positive_but_sub_five_percent_recent_sector_can_stay_live(self):
        sector_results = {
            "crypto": {30: bt(2400, 20), 14: bt(1100, 10), 7: bt(499, 5)},
        }

        policy = sector.evaluate_sector_policy(sector_results)

        self.assertEqual(policy["allowed"], ["crypto"])
        self.assertEqual(policy["watch"], [])
        self.assertEqual(policy["crypto"]["status"], "allowed")

    def test_sector_gate_does_not_repeat_wallet_level_win_rate_gate(self):
        sector_results = {
            "crypto": {
                30: bt(2400, 20, wins=15),
                14: bt(1200, 10, wins=7),
                7: bt(700, 5, wins=4),
            },
            "stock": {
                30: bt(2600, 20, wins=10),
                14: bt(1300, 10, wins=5),
                7: bt(800, 5, wins=2),
            },
        }

        policy = sector.evaluate_sector_policy(sector_results)

        self.assertEqual(policy["allowed"], ["crypto", "stock"])
        self.assertEqual(policy["watch"], [])
        self.assertEqual(policy["stock"]["status"], "allowed")

    def test_sector_liquidation_is_hard_but_recent_top3_body_is_diagnostic(self):
        liquidated = {
            30: {**bt(2400, 20, wins=15), "liquidations": 6},
            14: bt(1200, 10, wins=7),
            7: bt(700, 5, wins=4),
        }
        body = {
            30: bt(2400, 20, wins=15),
            14: {
                **bt(1200, 13, wins=9),
                "body_after_top3_n": 10, "body_after_top3_net_pnl": -20,
            },
            7: {
                **bt(700, 13, wins=9),
                "body_after_top3_n": 10, "body_after_top3_net_pnl": -5,
            },
        }

        liq_policy = sector.evaluate_sector_policy({"crypto": liquidated})
        body_policy = sector.evaluate_sector_policy({"crypto": body})

        self.assertEqual(liq_policy["crypto"]["status"], "sector_liquidation_limit")
        self.assertEqual(liq_policy["watch"], [])
        self.assertEqual(body_policy["crypto"]["status"], "allowed")
        self.assertEqual(body_policy["allowed"], ["crypto"])

    def test_recent_liquidation_has_no_grace(self):
        now_ms = 100 * DAY_MS
        sector_results = {
            "crypto": {
                30: replay_window(1200, [0.1] * 8, now_ms=now_ms - 8 * DAY_MS),
                14: bt(500, 12),
                7: replay_window(-100, [-0.1] * 8, now_ms=now_ms, liquidations=1),
            },
        }
        sector_results["crypto"][30]["_window_end_ms"] = now_ms
        previous = {"crypto": {"allow": True, "status": "allowed"}, "allowed": ["crypto"]}

        policy = sector.evaluate_sector_policy(sector_results, previous_policy=previous)

        self.assertFalse(policy["crypto"]["allow"])
        self.assertEqual(policy["crypto"]["recent"]["classification"], "liquidation")

    def test_allowed_sector_metrics_ignore_disallowed_losing_sector(self):
        metrics = {
            "score": 0.70,
            "initial_margin_equity": 25_000,
            "copy_bt_net_pnl": -1000,
            "copy_bt_14d_net_pnl": -600,
            "copy_bt_7d_net_pnl": -250,
            "copy_bt_closed_n": 20,
            "copy_bt_14d_closed_n": 12,
            "copy_bt_7d_closed_n": 8,
            "sector_policy_json": json.dumps({
                "crypto": {"allow": True},
                "stock": {"allow": False},
            }),
            "sector_copy_json": json.dumps({
                "crypto": {
                    "30": {
                        **bt(1500, 10), "initial_margin_equity": 10_000,
                        "window_start_equity": 12_000,
                    },
                    "14": {**bt(700, 6), "window_start_equity": 13_000},
                    "7": {**bt(300, 5), "window_start_equity": 14_000},
                },
                "stock": {
                    "30": bt(-2500, 10),
                    "14": bt(-1300, 6),
                    "7": bt(-550, 5),
                },
            }),
        }

        adjusted = sector.apply_allowed_sector_copy_metrics(metrics)

        self.assertEqual(adjusted["copy_bt_net_pnl"], 1500)
        self.assertEqual(adjusted["copy_bt_14d_net_pnl"], 700)
        self.assertEqual(adjusted["copy_bt_7d_net_pnl"], 300)
        self.assertEqual(adjusted["copy_bt_closed_n"], 10)
        self.assertEqual(adjusted["copy_bt_14d_closed_n"], 6)
        self.assertEqual(adjusted["copy_bt_7d_closed_n"], 5)
        self.assertEqual(adjusted["initial_margin_equity"], 10_000)
        self.assertEqual(adjusted["copy_bt_initial_margin_equity"], 10_000)
        self.assertEqual(adjusted["copy_bt_window_start_equity"], 12_000)
        self.assertEqual(adjusted["copy_bt_14d_window_start_equity"], 13_000)
        self.assertEqual(adjusted["copy_bt_7d_window_start_equity"], 14_000)

    def test_mix_wallet_uses_joint_account_replay_instead_of_summing_two_accounts(self):
        metrics = {
            "sector_policy_json": json.dumps({
                "crypto": {"allow": True}, "stock": {"allow": True},
            }),
            "sector_copy_json": json.dumps({
                "crypto": {"30": bt(3000, 10)},
                "stock": {"30": bt(4000, 12)},
                "joint": {"30": bt(5100, 18, wins=14)},
            }),
        }

        adjusted = sector.apply_allowed_sector_copy_metrics(metrics)

        self.assertEqual(adjusted["copy_bt_net_pnl"], 5100)
        self.assertEqual(adjusted["copy_bt_closed_n"], 18)
        self.assertAlmostEqual(adjusted["copy_bt_win_rate"], 14 / 18)


if __name__ == "__main__":
    unittest.main()
