import unittest

from hyper.copy.copy_policy import load_copy_policy


class CopyPolicyTests(unittest.TestCase):
    def test_all_windows_and_selection_thresholds_share_one_versioned_policy(self):
        policy = load_copy_policy()
        self.assertEqual(policy.min_closed(30), policy.min_closed_30d)
        self.assertEqual(policy.min_closed(14), policy.min_closed_14d)
        self.assertEqual(policy.min_closed(7), policy.min_closed_7d)
        self.assertEqual(policy.core_min_campaigns_30d, 10)
        self.assertEqual(policy.stability_fold_count, 4)
        self.assertEqual(policy.stability_fold_days, 7)
        self.assertEqual(policy.stability_min_evaluable_folds, 4)
        self.assertEqual(policy.stability_min_profitable_folds, 3)
        self.assertEqual(policy.stability_min_return, 0.05)
        self.assertEqual(policy.stability_max_loss_to_30d_profit, 0.25)
        self.assertEqual(policy.core_min_copy_return_30d, 0.10)
        self.assertEqual(policy.core_min_copy_return_7d, 0.05)
        self.assertEqual(policy.copy_weekly_min_campaigns_per_fold, 1)
        self.assertEqual(policy.copy_weekly_min_return, 0.0)
        self.assertEqual(policy.copy_weekly_score_return_target, 0.04)
        self.assertEqual(policy.copy_weekly_min_net_per_closed_return, 0.005)
        self.assertEqual(policy.core_max_liquidations_30d, 1)
        self.assertGreaterEqual(policy.min_actionable_open_rate, 0.7)
        self.assertGreaterEqual(policy.min_capacity_fit, 0.75)
        self.assertTrue(policy.version.startswith("copy-policy-"))

    def test_policy_version_changes_with_overrides(self):
        baseline = load_copy_policy()
        changed = load_copy_policy({"COPY_BT_MIN_CLOSED_7D": baseline.min_closed_7d + 1})
        self.assertNotEqual(baseline.version, changed.version)

if __name__ == "__main__":
    unittest.main()
