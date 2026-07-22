import unittest

from hyper.copy.copy_policy import load_copy_policy, one_sided_wilson_lower_bound


class CopyPolicyTests(unittest.TestCase):
    def test_all_windows_and_selection_thresholds_share_one_versioned_policy(self):
        policy = load_copy_policy()
        self.assertEqual(policy.min_closed(30), policy.min_closed_30d)
        self.assertEqual(policy.min_closed(14), policy.min_closed_14d)
        self.assertEqual(policy.min_closed(7), policy.min_closed_7d)
        self.assertEqual(
            (policy.core_min_closed_30d, policy.core_min_closed_14d,
             policy.core_min_closed_7d),
            (12, 5, 5),
        )
        self.assertEqual(
            (policy.core_min_win_rate_30d, policy.core_min_win_rate_14d,
             policy.core_min_win_rate_7d),
            (0.60, 0.55, 0.40),
        )
        self.assertEqual(
            (policy.core_min_campaigns_30d, policy.core_min_campaigns_14d,
             policy.core_min_campaigns_7d),
            (10, 5, 5),
        )
        self.assertEqual(policy.core_max_liquidations_30d, 1)
        self.assertGreaterEqual(policy.min_actionable_open_rate, 0.7)
        self.assertGreaterEqual(policy.min_capacity_fit, 0.75)
        self.assertTrue(policy.version.startswith("copy-policy-"))

    def test_policy_version_changes_with_overrides(self):
        baseline = load_copy_policy()
        changed = load_copy_policy({"COPY_BT_MIN_CLOSED_7D": baseline.min_closed_7d + 1})
        self.assertNotEqual(baseline.version, changed.version)

    def test_wilson_boundary_supports_independent_campaign_floor(self):
        self.assertGreaterEqual(one_sided_wilson_lower_bound(7, 10, 0.80), 0.50)
        self.assertLess(one_sided_wilson_lower_bound(6, 10, 0.80), 0.50)


if __name__ == "__main__":
    unittest.main()
