import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hyper.discovery import scanner


NOW = 2_000_000_000_000


def qualified(**overrides):
    row = {
        "data_status": "valid", "evidence_status": "qualified",
        "copy_bt_closed_n": 16, "copy_bt_campaign_closed_n": 12,
        "copy_bt_net_pnl": 1800, "copy_bt_14d_net_pnl": 900, "copy_bt_7d_net_pnl": 600,
        "copy_expected_return": 0.045, "copy_return_lcb": 0.01,
        "copy_positive_probability": 0.82, "copy_evidence_days": 10,
        "actionable_open_rate": 0.90, "capacity_fit": 0.90,
        "copy_bt_liquidations": 0, "copy_bt_campaign_net_after_top1": 400,
        "copy_bt_cost_stress_net_pnl": 1200,
        "copy_path_risk_status": "complete", "copy_intratrade_max_drawdown": .08,
        "copy_deep_bag_recovery_rate": 1.0, "initial_margin_equity": 10_000,
        "last_copyable_open_ms": NOW - 3_600_000,
        "sector_policy_json": json.dumps({
            "allowed": ["crypto"],
            "copyWeeklyProfitability": {
                "evidenceSufficient": True, "passed": True,
                "evaluableFolds": 4, "profitableFolds": 4, "qualifiedFolds": 4,
            },
        }),
    }
    row.update(overrides)
    return row


class ProfileQualificationTests(unittest.TestCase):
    def setUp(self):
        self.params = SimpleNamespace(copy_min_expected_margin_return=0.02)

    def test_core_quality_profile_remains_active(self):
        self.assertEqual(scanner._profile_copy_qualification(qualified(), NOW, self.params), (True, "ok"))

    def test_positive_thin_sample_remains_active_challenger(self):
        ok, reason = scanner._profile_copy_qualification(
            qualified(copy_evidence_days=2, copy_bt_campaign_closed_n=3), NOW, self.params,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_stale_activity_does_not_delete_profile(self):
        ok, reason = scanner._profile_copy_qualification(
            qualified(last_copyable_open_ms=NOW - 90 * 3_600_000), NOW, self.params,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_losing_strict_copy_is_rejected(self):
        ok, reason = scanner._profile_copy_qualification(
            qualified(copy_bt_net_pnl=-1), NOW, self.params,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "copy_not_profitable")

    def test_historical_max_drawdown_does_not_reject_profile(self):
        ok, reason = scanner._profile_copy_qualification(
            qualified(copy_intratrade_max_drawdown=.16), NOW, self.params,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_raw_quality_score_is_ranking_only(self):
        row = qualified()
        with patch.object(scanner.metrics, "score", return_value=0.581):
            ok, reason, score = scanner._finalize_profile_qualification(row, True, "ok")
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(score, 0.581)

    def test_thin_normalized_edge_remains_challenger_profile(self):
        ok, reason = scanner._profile_copy_qualification(
            qualified(copy_expected_return=0.005), NOW, self.params,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_copy_gate_switch_keeps_structurally_valid_profile(self):
        params = SimpleNamespace(copy_bt_gate_enable=False, inactive_days=1)
        self.assertEqual(
            scanner._profile_copy_qualification(
                qualified(copy_expected_return=-1, last_copyable_open_ms=NOW - 10 * 86_400_000), NOW, params,
            ),
            (True, "copy_gate_disabled"),
        )


if __name__ == "__main__":
    unittest.main()
