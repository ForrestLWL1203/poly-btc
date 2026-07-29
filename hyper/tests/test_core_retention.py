import unittest

from hyper.selection import core_retention


class CoreRetentionTest(unittest.TestCase):
    def test_soft_failure_needs_two_complete_generations(self):
        first = core_retention.advance(
            generation="g1", scan_kind="complete", scan_successful=True,
            reason="strict_copy_7d_conservative_return_below_floor",
        )
        self.assertEqual(core_retention.PROBATION, first.status)
        self.assertTrue(first.retain_enabled)
        self.assertEqual(1, first.failure_streak)

        second = core_retention.advance(
            previous_status=first.status,
            previous_streak=first.failure_streak,
            previous_reason=first.failure_reason,
            previous_started_generation=first.started_generation,
            generation="g2", scan_kind="complete", scan_successful=True,
            reason="copy_profit_factor_below_1_25",
        )
        self.assertEqual(core_retention.EXIT_ONLY, second.status)
        self.assertFalse(second.retain_enabled)
        self.assertEqual(2, second.failure_streak)

    def test_daily_and_incomplete_evidence_do_not_advance(self):
        daily = core_retention.advance(
            generation="d1", scan_kind="challenger_refresh", scan_successful=True,
            reason="strict_copy_7d_conservative_return_below_floor",
        )
        self.assertEqual(0, daily.failure_streak)
        incomplete = core_retention.advance(
            previous_status=core_retention.PROBATION, previous_streak=1,
            previous_reason="copy_profit_factor_below_1_25",
            previous_started_generation="g0",
            generation="g1", scan_kind="complete", scan_successful=True,
            reason="copy_path_incomplete", deferred=True,
        )
        self.assertEqual(core_retention.PROBATION, incomplete.status)
        self.assertEqual(1, incomplete.failure_streak)
        self.assertIsNone(incomplete.last_generation)

    def test_recovery_clears_probation_and_hard_failure_is_immediate(self):
        recovered = core_retention.advance(
            previous_status=core_retention.PROBATION, previous_streak=1,
            previous_reason="copy_profit_factor_below_1_25",
            previous_started_generation="g0",
            generation="g1", scan_kind="complete", scan_successful=True,
            reason=None,
        )
        self.assertEqual(core_retention.HEALTHY, recovered.status)
        self.assertEqual(0, recovered.failure_streak)
        hard = core_retention.advance(
            generation="g2", scan_kind="complete", scan_successful=True,
            reason="copy_30d_closed_pnl_not_positive",
        )
        self.assertEqual(core_retention.EXIT_ONLY, hard.status)
        self.assertFalse(hard.retain_enabled)
        catastrophic = core_retention.advance(
            generation="g3", scan_kind="complete", scan_successful=True,
            reason="copy_single_liquidation_loss_over_8pct",
        )
        self.assertEqual(core_retention.SAFETY_FROZEN, catastrophic.status)

    def test_replacement_requires_both_accounts_and_recent_non_decline(self):
        baseline = {
            "standardizedAccount": {"netPnl30d": 1000, "dynamicReturn7d": .08},
            "paperAccount": {"netPnl30d": 800, "dynamicReturn7d": .06},
        }
        proposal = {
            "standardizedAccount": {"netPnl30d": 1100, "dynamicReturn7d": .08},
            "paperAccount": {"netPnl30d": 880, "dynamicReturn7d": .061},
        }
        self.assertTrue(
            core_retention.replacement_gain(baseline, proposal)["eligible"]
        )
        proposal["paperAccount"]["dynamicReturn7d"] = .059
        self.assertFalse(
            core_retention.replacement_gain(baseline, proposal)["eligible"]
        )

    def test_hard_check_is_not_hidden_by_earlier_soft_first_failure(self):
        classification, reason = core_retention.qualification_failure({
            "firstFailure": "copy_episode_evidence_insufficient",
            "checks": {
                "copyClosedSample": False,
                "copyClosedProfit30d": False,
            },
        })
        self.assertEqual("hard", classification)
        self.assertEqual("copy_30d_closed_pnl_not_positive", reason)

    def test_unsafe_shared_baseline_has_no_replacement_protection(self):
        self.assertFalse(core_retention.baseline_protectable({
            "standardizedAccount": {
                "netPnl30d": -1, "openLossRatio30d": 0.0,
            },
            "paperAccount": {
                "netPnl30d": 100, "openLossRatio30d": 0.0,
            },
        }))


if __name__ == "__main__":
    unittest.main()
