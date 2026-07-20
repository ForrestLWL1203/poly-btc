import json
import unittest

from hyper import sector


def bt(net, closed, wins=None, target_open=None, opened=None):
    return {
        "copy_net_pnl": net,
        "closed_n": closed,
        "wins": closed if wins is None else wins,
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
                30: bt(1200, 10),
                14: bt(600, 6),
                7: bt(220, 5),
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
        self.assertEqual(policy["stock"]["status"], "recent_loss")
        self.assertEqual(policy["allowed"], ["crypto"])

    def test_current_generation_structure_limits_profitable_sector_without_prior_policy(self):
        sector_results = {
            "crypto": {30: bt(1800, 10), 14: bt(900, 6), 7: bt(600, 5)},
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
            "crypto": {30: bt(2200, 12), 14: bt(1000, 7), 7: bt(600, 5)},
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
        self.assertEqual(policy["crypto"]["status"], "thin_evidence")

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
            "crypto": {30: bt(2200, 12), 14: bt(1000, 7), 7: bt(600, 5)},
            "stock": {30: bt(1800, 10), 14: bt(900, 6), 7: bt(500, 5)},
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

    def test_six_recent_losses_are_insufficient_for_hard_rejection(self):
        sector_results = {
            "crypto": {
                30: bt(1671, 40),
                14: bt(1773, 25),
                7: bt(-154, 6, wins=2),
            },
        }

        policy = sector.evaluate_sector_policy(sector_results)

        self.assertTrue(policy["crypto"]["allow"])
        self.assertEqual(policy["crypto"]["status"], "recent_soft_loss")
        self.assertEqual(policy["crypto"]["recent"]["classification"], "insufficient_recent")

    def test_recent_loss_uses_margin_normalized_wallet_distribution(self):
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
        self.assertEqual(policy["crypto"]["status"], "recent_soft_loss")
        self.assertEqual(policy["crypto"]["recent"]["classification"], "shallow_loss")
        self.assertFalse(policy["crypto"]["recent"]["hard"])

    def test_significant_recent_loss_gets_one_new_evidence_grace_for_existing_wallet(self):
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
        changed_results = json.loads(json.dumps(sector_results))
        changed_results["crypto"]["7"]["copy_net_pnl"] = -81
        second = sector.evaluate_sector_policy(changed_results, previous_policy=first)

        self.assertTrue(first["crypto"]["allow"])
        self.assertEqual(first["crypto"]["status"], "recent_degradation_watch")
        self.assertEqual(first["crypto"]["recent"]["streak"], 1)
        self.assertTrue(repeated["crypto"]["allow"])
        self.assertEqual(repeated["crypto"]["recent"]["streak"], 1)
        self.assertFalse(second["crypto"]["allow"])
        self.assertEqual(second["crypto"]["status"], "recent_loss")
        self.assertEqual(second["crypto"]["recent"]["streak"], 2)

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
                    "30": bt(1500, 10),
                    "14": bt(700, 6),
                    "7": bt(300, 5),
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
