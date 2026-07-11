import unittest

from hl.copy_policy import load_copy_policy


class CopyPolicyTests(unittest.TestCase):
    def test_all_windows_and_selection_thresholds_share_one_versioned_policy(self):
        policy = load_copy_policy()
        self.assertEqual(policy.min_closed(30), policy.min_closed_30d)
        self.assertEqual(policy.min_closed(14), policy.min_closed_14d)
        self.assertEqual(policy.min_closed(7), policy.min_closed_7d)
        self.assertGreaterEqual(policy.min_actionable_open_rate, 0.7)
        self.assertGreaterEqual(policy.min_capacity_fit, 0.85)
        self.assertTrue(policy.version.startswith("copy-policy-"))

    def test_policy_version_changes_with_overrides(self):
        baseline = load_copy_policy()
        changed = load_copy_policy({"COPY_BT_MIN_CLOSED_7D": baseline.min_closed_7d + 1})
        self.assertNotEqual(baseline.version, changed.version)


if __name__ == "__main__":
    unittest.main()
