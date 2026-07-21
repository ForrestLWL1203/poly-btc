import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hyper.discovery import scanner


NOW = 2_000_000_000_000


def qualified(**overrides):
    row = {
        "data_status": "valid",
        "evidence_status": "qualified",
        "copy_bt_closed_n": 16,
        "copy_bt_14d_closed_n": 9,
        "copy_bt_7d_closed_n": 5,
        "copy_bt_win_rate": 0.75,
        "copy_bt_14d_win_rate": 2 / 3,
        "copy_bt_7d_win_rate": 0.80,
        "copy_bt_net_pnl": 1800,
        "copy_bt_14d_net_pnl": 900,
        "copy_bt_7d_net_pnl": 600,
        "copy_expected_return": 0.045,
        "copy_return_lcb": 0.01,
        "copy_positive_probability": 0.82,
        "copy_evidence_days": 10,
        "copy_recent_return_14d": 0.03,
        "copy_recent_return_7d": 0.02,
        "actionable_open_rate": 0.90,
        "capacity_fit": 0.90,
        "copy_bt_liquidations": 0,
        "last_copyable_open_ms": NOW - 3_600_000,
    }
    row.update(overrides)
    return row


class ProfileQualificationTests(unittest.TestCase):
    def setUp(self):
        self.params = SimpleNamespace(copy_min_expected_margin_return=0.02)

    def test_quality_passes_once_before_ranking(self):
        self.assertEqual(scanner._profile_copy_qualification(qualified(), NOW, self.params), (True, "ok"))

    def test_thin_sample_is_excluded(self):
        ok, reason = scanner._profile_copy_qualification(qualified(copy_evidence_days=2), NOW, self.params)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_recent_loss_is_excluded(self):
        ok, reason = scanner._profile_copy_qualification(qualified(
            copy_bt_14d_net_pnl=-50, copy_bt_7d_net_pnl=-220,
        ), NOW, self.params)
        self.assertFalse(ok)
        self.assertEqual(reason, "recent_copy_collapse")

    def test_inactive_copyable_open_flow_is_excluded(self):
        ok, reason = scanner._profile_copy_qualification(
            qualified(last_copyable_open_ms=NOW - 49 * 3_600_000), NOW, self.params,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "inactive_copyable_open")

    def test_copyable_open_within_48_hours_remains_qualified(self):
        self.assertEqual(
            scanner._profile_copy_qualification(
                qualified(last_copyable_open_ms=NOW - 47 * 3_600_000), NOW, self.params,
            ),
            (True, "ok"),
        )

    def test_open_mirrored_swing_position_bypasses_only_inactivity(self):
        row = qualified(
            last_copyable_open_ms=NOW - 72 * 3_600_000,
            material_open_count=2,
            open_unrealized=500,
        )
        scanner._attach_open_copy_activity_context(row, "0xaaa", {"0xaaa": 80})
        self.assertEqual(
            scanner._profile_copy_qualification(row, NOW, self.params),
            (True, "ok"),
        )

    def test_open_target_without_our_mirrored_position_does_not_bypass_inactivity(self):
        row = qualified(
            last_copyable_open_ms=NOW - 72 * 3_600_000,
            material_open_count=2,
            open_unrealized=500,
        )
        scanner._attach_open_copy_activity_context(row, "0xaaa", {"0xbbb": 80})
        self.assertEqual(
            scanner._profile_copy_qualification(row, NOW, self.params),
            (False, "inactive_copyable_open"),
        )

    def test_open_copy_bypass_cannot_override_recent_collapse(self):
        row = qualified(
            last_copyable_open_ms=NOW - 72 * 3_600_000,
            material_open_count=1,
            open_unrealized=500,
            copy_bt_14d_net_pnl=-50,
            copy_bt_7d_net_pnl=-220,
        )
        scanner._attach_open_copy_activity_context(row, "0xaaa", {"0xaaa": 80})
        self.assertEqual(
            scanner._profile_copy_qualification(row, NOW, self.params),
            (False, "recent_copy_collapse"),
        )

    def test_carried_target_loss_does_not_bypass_inactivity(self):
        row = qualified(
            last_copyable_open_ms=NOW - 72 * 3_600_000,
            material_open_count=1,
            open_unrealized=-500,
        )
        scanner._attach_open_copy_activity_context(row, "0xaaa", {"0xaaa": 80})
        self.assertEqual(
            scanner._profile_copy_qualification(row, NOW, self.params),
            (False, "inactive_copyable_open"),
        )

    def test_carried_copy_loss_does_not_bypass_inactivity(self):
        row = qualified(
            last_copyable_open_ms=NOW - 72 * 3_600_000,
            material_open_count=1,
            open_unrealized=500,
        )
        scanner._attach_open_copy_activity_context(row, "0xaaa", {"0xaaa": -80})
        self.assertEqual(
            scanner._profile_copy_qualification(row, NOW, self.params),
            (False, "inactive_copyable_open"),
        )

    def test_raw_quality_score_is_ranking_only_not_a_qualification_veto(self):
        row = qualified()
        with patch.object(scanner.metrics, "score", return_value=0.581):
            ok, reason, score = scanner._finalize_profile_qualification(row, True, "ok")
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(score, 0.581)
        self.assertEqual(row["raw_quality_score"], 0.581)

    def test_near_core_thin_edge_with_strong_dollar_economics_remains_observable(self):
        ok, reason = scanner._profile_copy_qualification(qualified(
            copy_expected_return=0.019, copy_bt_7d_net_pnl=600,
        ), NOW, self.params)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_truly_thin_normalized_copy_edge_is_excluded(self):
        ok, reason = scanner._profile_copy_qualification(qualified(
            copy_expected_return=0.005, copy_bt_net_pnl=1600, copy_bt_7d_net_pnl=400,
        ), NOW, self.params)
        self.assertFalse(ok)
        self.assertEqual(reason, "thin_copy_edge")

    def test_copy_gate_switch_bypasses_copy_evidence_but_not_activity(self):
        params = SimpleNamespace(copy_bt_gate_enable=False, inactive_days=1)
        self.assertEqual(
            scanner._profile_copy_qualification(qualified(copy_expected_return=-1), NOW, params),
            (True, "copy_gate_disabled"),
        )
        ok, reason = scanner._profile_copy_qualification(
            qualified(last_copyable_open_ms=NOW - 2 * 86_400_000), NOW, params,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "inactive_copyable_open")


if __name__ == "__main__":
    unittest.main()
