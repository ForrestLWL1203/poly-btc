import sqlite3
import unittest

from hyper.discovery import scanner
from hyper.selection import core_retention


class CoreRetentionTest(unittest.TestCase):
    def test_soft_failure_becomes_medium_without_auto_exit(self):
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
        self.assertEqual(core_retention.MEDIUM_RISK, second.status)
        self.assertTrue(second.retain_enabled)
        self.assertEqual(2, second.failure_streak)

    def test_daily_advances_and_incomplete_evidence_does_not(self):
        daily = core_retention.advance(
            generation="d1", scan_kind="challenger_refresh", scan_successful=True,
            reason="strict_copy_7d_conservative_return_below_floor",
        )
        self.assertEqual(1, daily.failure_streak)
        self.assertEqual(core_retention.PROBATION, daily.status)
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

    def test_back_to_back_complete_scan_does_not_confirm_probation(self):
        pending = core_retention.advance(
            previous_status=core_retention.PROBATION,
            previous_streak=1,
            previous_reason="strict_copy_7d_conservative_return_below_floor",
            previous_started_generation="g0",
            generation="g1",
            scan_kind="complete",
            scan_successful=True,
            reason="strict_copy_7d_conservative_return_below_floor",
            confirmation_eligible=False,
        )
        self.assertEqual(core_retention.PROBATION, pending.status)
        self.assertEqual(1, pending.failure_streak)
        self.assertTrue(pending.retain_enabled)
        self.assertEqual("confirmation_interval_pending", pending.action)

    def test_confirmation_interval_uses_generation_start_times(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE scan_generation (generation TEXT, started_at TEXT)")
        db.executemany(
            "INSERT INTO scan_generation VALUES (?,?)",
            [
                ("g0", "2026-01-01T04:00:00Z"),
                ("g-soon", "2026-01-02T04:00:00Z"),
                ("g-ready", "2026-01-04T04:00:00Z"),
            ],
        )
        self.assertFalse(scanner._retention_confirmation_eligible(
            db, "g0", "g-soon", previous_streak=1,
        ))
        self.assertTrue(scanner._retention_confirmation_eligible(
            db, "g0", "g-ready", previous_streak=1,
        ))

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
        medium = core_retention.advance(
            generation="g2", scan_kind="complete", scan_successful=True,
            reason="copy_30d_closed_pnl_not_positive",
        )
        self.assertEqual(core_retention.MEDIUM_RISK, medium.status)
        self.assertTrue(medium.retain_enabled)
        structural = core_retention.advance(
            generation="g2b", scan_kind="complete", scan_successful=True,
            reason="source_heavy_dca",
        )
        self.assertEqual(core_retention.EXIT_ONLY, structural.status)
        self.assertFalse(structural.retain_enabled)
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
        self.assertEqual("medium", classification)
        self.assertEqual("copy_30d_closed_pnl_not_positive", reason)

    def test_qualified_status_is_healthy_for_core_retention(self):
        classification, reason = core_retention.qualification_failure({
            "eligible": True,
            "status": "strict_copy_qualified",
            "firstFailure": None,
            "checks": {
                "copyClosedProfit30d": True,
                "copyConservativeProfit30d": True,
                "singleLiquidationLossWithinLimit": True,
            },
        })
        self.assertEqual(core_retention.HEALTHY, classification)
        self.assertIsNone(reason)

    def test_unsafe_shared_baseline_has_no_replacement_protection(self):
        self.assertFalse(core_retention.baseline_protectable({
            "standardizedAccount": {
                "netPnl30d": -1, "openLossRatio30d": 0.0,
            },
            "paperAccount": {
                "netPnl30d": 100, "openLossRatio30d": 0.0,
            },
        }))

    def test_missing_shared_baseline_fails_closed_into_retention_replay(self):
        gate = core_retention.replacement_gate({}, {
            "standardizedAccount": {"netPnl30d": 200, "dynamicReturn7d": .08},
            "paperAccount": {"netPnl30d": 200, "dynamicReturn7d": .08},
        })
        self.assertFalse(gate["eligible"])
        self.assertEqual("baseline_shared_validation_missing", gate["reason"])


if __name__ == "__main__":
    unittest.main()
