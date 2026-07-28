import json
import unittest
from types import SimpleNamespace

from hyper.discovery import scanner


NOW = 2_000_000_000_000


def qualified(**overrides):
    row = {
        "official_perp_status": "passed",
        "official_perp_reason": "perp_prefilter_passed",
        "official_perp_return_30d": .40,
        "source_episode_n_30d": 20,
        "source_win_rate_30d": .80,
        "source_top3_profit_share": .50,
        "source_body_after_top3_n": 17,
        "source_body_after_top3_win_rate": .75,
        "source_body_after_top3_net_pnl": 500,
        "source_episode_n_7d": 6,
        "source_net_pnl_30d": 2_400,
        "source_net_pnl_7d": 700,
        "open_unrealized": 0,
        "data_status": "valid",
        "evidence_status": "qualified",
        "copy_bt_closed_n": 16,
        "copy_bt_win_rate": .75,
        "copy_bt_net_pnl": 1_800,
        "copy_bt_closed_net_pnl": 1_800,
        "copy_bt_7d_net_pnl": 600,
        "copy_bt_7d_closed_net_pnl": 600,
        "copy_bt_window_start_equity": 10_000,
        "copy_bt_7d_window_start_equity": 10_000,
        "copy_bt_open_fill_rate": .90,
        "actionable_open_rate": .90,
        "copy_bt_profit_factor": 1.8,
        "copy_bt_payoff_ratio": 1.5,
        "copy_bt_top3_profit_share": .40,
        "copy_bt_body_after_top3_n": 13,
        "copy_bt_body_after_top3_win_rate": .54,
        "copy_bt_body_after_top3_net_pnl": 500,
        "copy_bt_liquidations": 0,
        "copy_bt_max_liquidation_loss_pct": 0,
        "copy_bt_valuation_status": "complete",
        "copy_path_risk_status": "complete",
        "pre_strict_activity": {
            "operational": True,
            "reason": "operational_activity",
            "latest7dActive": True,
            "activeWeeks4": 4,
            "maxOpenGapDays28d": 7,
        },
        "last_copyable_open_ms": NOW - 3_600_000,
        "open_events_30d": 20,
        "sector_policy_json": json.dumps({"allowed": ["crypto"]}),
    }
    row.update(overrides)
    return row


class ProfileQualificationTests(unittest.TestCase):
    def setUp(self):
        self.params = SimpleNamespace(
            copy_bt_gate_enable=True,
            evidence_min_trades=7,
            evidence_min_days=0,
            margin_equity_pct=1.0,
        )

    def test_rough_copy_qualified_profile_remains_active(self):
        self.assertEqual(
            scanner._profile_copy_qualification(qualified(), NOW, self.params),
            (True, "rough_copy_qualified"),
        )

    def test_evidence_failures_remain_challengers_but_economic_loss_rejects(self):
        cases = (
            ({"copy_bt_closed_n": 6}, True, "copy_episode_evidence_insufficient"),
            (
                {"copy_bt_net_pnl": -1, "copy_bt_closed_net_pnl": -1},
                False,
                "copy_30d_closed_pnl_not_positive",
            ),
            (
                {"pre_strict_activity": {
                    "operational": False,
                    "reason": "no_actionable_open_7d",
                }},
                True,
                "no_actionable_open_7d",
            ),
            ({
                "copy_bt_win_rate": .41,
                "copy_bt_body_after_top3_net_pnl": -1,
            }, True, "copy_lottery_profile_rejected"),
        )
        for overrides, expected_ok, expected in cases:
            with self.subTest(expected=expected):
                ok, reason = scanner._profile_copy_qualification(
                    qualified(**overrides), NOW, self.params,
                )
                self.assertEqual(ok, expected_ok)
                self.assertEqual(reason, expected)

    def test_historical_max_drawdown_is_audit_only(self):
        ok, reason = scanner._profile_copy_qualification(
            qualified(copy_intratrade_max_drawdown=.90), NOW, self.params,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "rough_copy_qualified")

    def test_source_prescore_is_ranking_only(self):
        row = qualified()
        ok, reason, score = scanner._finalize_profile_qualification(row, True, "ok")
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertGreater(score, 0)

    def test_copy_gate_switch_keeps_structurally_valid_profile(self):
        params = SimpleNamespace(copy_bt_gate_enable=False, inactive_days=1)
        self.assertEqual(
            scanner._profile_copy_qualification(
                qualified(copy_bt_net_pnl=-1), NOW, params,
            ),
            (True, "copy_gate_disabled"),
        )


if __name__ == "__main__":
    unittest.main()
