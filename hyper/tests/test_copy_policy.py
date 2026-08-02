import unittest

from hyper.copy.copy_policy import load_copy_policy


class CopyPolicyTests(unittest.TestCase):
    def test_extreme_quality_contract_is_one_versioned_policy(self):
        policy = load_copy_policy()
        self.assertEqual(policy.source_quality_max_n, 40)
        self.assertEqual(policy.source_min_episodes_30d, 7)
        self.assertEqual(policy.source_min_episode_win_rate, 0.70)
        self.assertEqual(policy.source_low_freq_min_episodes_30d, 7)
        self.assertEqual(policy.source_low_freq_max_episodes_30d, 9)
        self.assertEqual(policy.source_low_freq_min_episode_win_rate, 0.85)
        self.assertEqual(policy.source_low_freq_min_official_return, 0.30)
        self.assertEqual(policy.source_top3_concentration_trigger, 0.60)
        self.assertEqual(policy.source_body_min_retained_net, 0.20)
        self.assertEqual(policy.source_body_min_win_rate, 0.70)
        self.assertEqual(policy.official_perp_min_return_30d, 0.20)
        self.assertEqual(policy.official_perp_min_return_7d, 0.05)
        self.assertEqual(policy.official_perp_long_history_days, 28)
        self.assertEqual(policy.official_perp_short_history_days, 7)
        self.assertEqual(policy.official_perp_boundary_max_gap_hours, 36)
        self.assertEqual(policy.rough_min_closed_30d, 7)
        self.assertEqual(policy.min_closed(7), 0)
        self.assertEqual(policy.rough_min_win_rate, 0.60)
        self.assertEqual(policy.core_min_dynamic_copy_return_30d, 0.10)
        self.assertEqual(policy.core_min_dynamic_copy_return_7d, 0.03)
        self.assertEqual(policy.core_min_copy_win_rate, 0.60)
        self.assertEqual(policy.portfolio_min_return_30d, 0.10)
        self.assertEqual(policy.portfolio_min_return_7d, 0.03)
        self.assertEqual(policy.core_max_liquidations_30d, 3)
        self.assertGreaterEqual(policy.min_actionable_open_rate, 0.70)
        self.assertTrue(policy.version.startswith("copy-policy-"))

    def test_policy_version_changes_with_overrides(self):
        baseline = load_copy_policy()
        changed = load_copy_policy({
            "ROUGH_COPY_MIN_WIN_RATE": baseline.rough_min_win_rate + .01,
        })
        self.assertNotEqual(baseline.version, changed.version)

    def test_legacy_five_percent_liquidation_setting_cannot_restore_old_cutoff(self):
        policy = load_copy_policy({
            "CORE_COPY_MAX_SINGLE_LIQUIDATION_LOSS_PCT": 0.05,
        })
        self.assertEqual(policy.catastrophic_liquidation_loss_pct, 0.08)


if __name__ == "__main__":
    unittest.main()
