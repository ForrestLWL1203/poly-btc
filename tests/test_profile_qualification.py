import unittest
from types import SimpleNamespace

from hl import scanner


NOW = 2_000_000_000_000


def qualified(**overrides):
    row = {
        "data_status": "valid",
        "evidence_status": "qualified",
        "copy_bt_closed_n": 16,
        "copy_bt_14d_closed_n": 9,
        "copy_bt_7d_closed_n": 5,
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
        self.assertFalse(ok)
        self.assertEqual(reason, "thin_independent_evidence")

    def test_recent_loss_is_excluded(self):
        ok, reason = scanner._profile_copy_qualification(qualified(copy_recent_return_7d=-0.001), NOW, self.params)
        self.assertFalse(ok)
        self.assertEqual(reason, "recent_copy_loss")

    def test_inactive_copyable_open_flow_is_excluded(self):
        ok, reason = scanner._profile_copy_qualification(
            qualified(last_copyable_open_ms=NOW - 25 * 3_600_000), NOW, self.params,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "inactive_copyable_open")

    def test_thin_normalized_copy_edge_is_excluded(self):
        ok, reason = scanner._profile_copy_qualification(qualified(copy_expected_return=0.019), NOW, self.params)
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
