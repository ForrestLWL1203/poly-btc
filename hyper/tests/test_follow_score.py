import json
import unittest

from hyper.selection import pre_strict
from hyper.selection.follow_score import (
    compute_follow_score,
    compute_profit_priority,
    compute_source_quality_score,
    evaluate_follow_eligibility,
    follow_score_sort_key,
    profit_priority_sort_key,
)


NOW = 2_000_000_000_000


def operational_activity(**overrides):
    value = {
        "operational": True,
        "reason": "operational_activity",
        "latest7dActive": True,
        "activeWeeks4": 4,
        "maxOpenGapDays28d": 7.0,
        "weeklyOpenCountsOldestFirst": [2, 2, 2, 2],
    }
    value.update(overrides)
    return value


def evidence(**overrides):
    row = {
        "official_perp_status": "passed",
        "official_perp_return_30d": 0.01,
        "source_episode_n_30d": 20,
        "source_episode_n_7d": 5,
        "source_win_rate_30d": 0.55,
        "source_win_rate_7d": 0.55,
        "source_net_pnl_30d": 8_000.0,
        "source_net_pnl_7d": 1_500.0,
        "source_top3_profit_share": 0.40,
        "source_body_after_top3_n": 17,
        "source_body_after_top3_win_rate": 0.53,
        "source_body_after_top3_net_pnl": 1_000.0,
        "source_profit_factor_30d": 1.8,
        "source_payoff_ratio_30d": 1.5,
        "open_unrealized": 0.0,
        "copy_bt_closed_n": 12,
        "copy_bt_7d_closed_n": 1,
        "copy_bt_win_rate": 0.55,
        "copy_bt_net_pnl": 3_000.0,
        "copy_bt_closed_net_pnl": 3_000.0,
        "copy_bt_unrealized_pnl": 0.0,
        "copy_bt_7d_net_pnl": 1_000.0,
        "copy_bt_7d_closed_net_pnl": 1_000.0,
        "copy_bt_7d_unrealized_pnl": 0.0,
        "copy_bt_window_start_equity": 10_000.0,
        "copy_bt_7d_window_start_equity": 12_000.0,
        "copy_bt_open_fill_rate": 0.90,
        "actionable_open_rate": 0.90,
        "copy_bt_behavior_replication_rate": 0.85,
        "copy_bt_profit_factor": 1.8,
        "copy_bt_payoff_ratio": 1.5,
        "copy_bt_top3_profit_share": 0.40,
        "copy_bt_body_after_top3_n": 9,
        "copy_bt_body_after_top3_win_rate": 0.53,
        "copy_bt_body_after_top3_net_pnl": 800.0,
        "copy_bt_liquidations": 0,
        "copy_bt_max_liquidation_loss_pct": 0.0,
        "copy_bt_data_status": "valid",
        "copy_bt_evidence_status": "qualified",
        "copy_bt_valuation_status": "complete",
        "copy_path_risk_status": "complete",
        "pre_strict_activity": operational_activity(),
        "last_copyable_open_ms": NOW - 12 * 3_600_000,
        "open_events_30d": 20,
        "sector_policy_json": json.dumps({"allowed": ["crypto"]}),
    }
    row.update(overrides)
    if "copy_bt_net_pnl" in overrides and "copy_bt_closed_net_pnl" not in overrides:
        row["copy_bt_closed_net_pnl"] = (
            float(row["copy_bt_net_pnl"]) - float(row.get("copy_bt_unrealized_pnl") or 0.0)
        )
    if "copy_bt_7d_net_pnl" in overrides and "copy_bt_7d_closed_net_pnl" not in overrides:
        row["copy_bt_7d_closed_net_pnl"] = (
            float(row["copy_bt_7d_net_pnl"])
            - float(row.get("copy_bt_7d_unrealized_pnl") or 0.0)
        )
    return row


def judge(stage="strict", **overrides):
    return evaluate_follow_eligibility(evidence(**overrides), stage=stage, as_of_ms=NOW)


class FollowScoreTests(unittest.TestCase):
    def test_official_roi_and_balance_are_not_admission_inputs(self):
        low = judge("strict", official_perp_status="rejected", official_perp_return_30d=-9)
        self.assertTrue(low["coreEligible"])
        self.assertTrue(low["checks"]["officialPerpPassed"])

    def test_profit_priority_uses_dynamic_equity_and_70_30(self):
        priority, detail = compute_profit_priority(evidence(
            copy_bt_net_pnl=4_000,
            copy_bt_closed_net_pnl=4_000,
            copy_bt_window_start_equity=20_000,
            copy_bt_7d_net_pnl=1_000,
            copy_bt_7d_closed_net_pnl=1_000,
            copy_bt_7d_window_start_equity=10_000,
        ))
        self.assertAlmostEqual(priority, .70 * .20 + .30 * .10)
        self.assertEqual(detail["weights"], {"30d": .70, "7d": .30})

    def test_follow_score_is_primary_then_profit_tie_breaks_are_stable(self):
        rows = [
            (evidence(copy_bt_profit_factor=1.3), .9, "0xb"),
            (evidence(copy_bt_profit_factor=2.0), .2, "0xc"),
            (evidence(copy_bt_profit_factor=2.0), .2, "0xa"),
        ]
        ordered = sorted(rows, key=lambda item: profit_priority_sort_key(
            item[0], follow_score_value=item[1], addr=item[2],
        ))
        self.assertEqual([item[2] for item in ordered], ["0xb", "0xa", "0xc"])

    def test_profit_aligned_score_cannot_reward_low_return_with_quality_points(self):
        weak = evidence(
            copy_bt_net_pnl=1_500, copy_bt_closed_net_pnl=1_500,
            copy_bt_7d_net_pnl=600, copy_bt_7d_closed_net_pnl=600,
            copy_bt_win_rate=.99, copy_bt_profit_factor=9.0,
            copy_bt_closed_n=80, actionable_open_rate=1.0,
            copy_bt_open_fill_rate=1.0,
        )
        strong = evidence(
            copy_bt_net_pnl=5_000, copy_bt_closed_net_pnl=5_000,
            copy_bt_7d_net_pnl=1_500, copy_bt_7d_closed_net_pnl=1_500,
            copy_bt_win_rate=.51, copy_bt_profit_factor=1.30,
            copy_bt_closed_n=8, actionable_open_rate=.71,
            copy_bt_open_fill_rate=.71,
        )
        weak_score, weak_detail = compute_follow_score(weak, stage="strict")
        strong_score, strong_detail = compute_follow_score(strong, stage="strict")

        self.assertGreater(strong_score, weak_score)
        for score, detail in (
            (weak_score, weak_detail), (strong_score, strong_detail),
        ):
            self.assertLessEqual(score, detail["profitComponent"] + 1e-12)
            self.assertGreaterEqual(
                score, detail["profitComponent"] * .85 - 1e-12,
            )

    def test_final_sort_is_monotonic_with_displayed_score(self):
        rows = []
        for index, metrics in enumerate((
            evidence(copy_bt_net_pnl=2_000, copy_bt_7d_net_pnl=500),
            evidence(copy_bt_net_pnl=4_000, copy_bt_7d_net_pnl=1_000),
            evidence(copy_bt_net_pnl=3_000, copy_bt_7d_net_pnl=800),
        )):
            score, _detail = compute_follow_score(metrics, stage="strict")
            rows.append((metrics, score, f"0x{index}"))
        ordered = sorted(rows, key=lambda item: follow_score_sort_key(
            item[0], follow_score_value=item[1], addr=item[2],
        ))

        self.assertEqual(
            [item[1] for item in ordered],
            sorted((item[1] for item in rows), reverse=True),
        )

    def test_rough_requires_closed_profit_pf_execution_and_activity(self):
        self.assertTrue(judge("rough")["coreEligible"])
        self.assertEqual(
            judge("rough", copy_bt_closed_n=6)["firstFailure"],
            "copy_episode_evidence_insufficient",
        )
        self.assertEqual(
            judge("rough", copy_bt_profit_factor=1.249)["firstFailure"],
            "copy_profit_factor_below_1_25",
        )
        self.assertEqual(
            judge("rough", actionable_open_rate=.699, copy_bt_open_fill_rate=.699)["firstFailure"],
            "rough_copy_open_rate_below_floor",
        )
        self.assertEqual(
            judge("rough", pre_strict_activity=operational_activity(
                operational=False, reason="active_weeks_below_3_of_4",
            ))["firstFailure"],
            "active_weeks_below_3_of_4",
        )

    def test_no_fixed_seven_day_episode_count_gate(self):
        self.assertTrue(judge("strict", copy_bt_7d_closed_n=0)["coreEligible"])

    def test_conditional_lottery_allows_low_win_with_profitable_body(self):
        passed = judge(
            "rough", copy_bt_win_rate=.41,
            copy_bt_top3_profit_share=.21,
            copy_bt_body_after_top3_win_rate=.38,
            copy_bt_body_after_top3_net_pnl=500,
        )
        failed = judge(
            "rough", copy_bt_win_rate=.41,
            copy_bt_top3_profit_share=.21,
            copy_bt_body_after_top3_win_rate=.38,
            copy_bt_body_after_top3_net_pnl=-1,
        )
        self.assertTrue(passed["coreEligible"])
        self.assertEqual(failed["firstFailure"], "copy_lottery_profile_rejected")

    def test_concentrated_weak_source_is_rejected(self):
        result = judge(
            "rough", source_top3_profit_share=.70,
            source_body_after_top3_win_rate=.49,
            source_body_after_top3_net_pnl=100,
        )
        self.assertEqual(result["firstFailure"], "source_lottery_profile_rejected")

    def test_strict_dynamic_return_boundaries_and_path(self):
        boundary = judge(
            "strict",
            copy_bt_net_pnl=1_000,
            copy_bt_closed_net_pnl=1_000,
            copy_bt_window_start_equity=10_000,
            copy_bt_7d_net_pnl=300,
            copy_bt_7d_closed_net_pnl=300,
            copy_bt_7d_window_start_equity=10_000,
        )
        self.assertTrue(boundary["coreEligible"])
        self.assertEqual(
            judge("strict", copy_bt_net_pnl=999)["firstFailure"],
            "strict_copy_30d_conservative_return_below_floor",
        )
        self.assertEqual(
            judge("strict", copy_path_risk_status="missing")["firstFailure"],
            "copy_path_incomplete",
        )
        self.assertTrue(judge("rough", copy_path_risk_status="missing")["coreEligible"])

    def test_positive_float_never_qualifies_and_open_loss_is_fully_deducted(self):
        positive_float = judge(
            "rough", copy_bt_closed_net_pnl=-100,
            copy_bt_unrealized_pnl=5_000, copy_bt_net_pnl=4_900,
        )
        self.assertEqual(positive_float["firstFailure"], "copy_30d_closed_pnl_not_positive")
        loss = judge(
            "strict", copy_bt_closed_net_pnl=2_000,
            copy_bt_unrealized_pnl=-1_001, copy_bt_net_pnl=999,
        )
        self.assertEqual(loss["firstFailure"], "copy_open_loss_over_50pct")
        self.assertEqual(loss["economics"]["30"]["qualificationPnl"], 999)

    def test_liquidation_contract(self):
        self.assertTrue(judge("strict", copy_bt_liquidations=3)["coreEligible"])
        self.assertEqual(
            judge("strict", copy_bt_liquidations=4)["firstFailure"],
            "strict_copy_liquidations_over_3",
        )
        self.assertEqual(
            judge("rough", copy_bt_max_liquidation_loss_pct=.05)["firstFailure"],
            "copy_single_liquidation_loss_over_5pct",
        )

    def test_score_remains_ranking_only(self):
        low_score, _ = compute_follow_score(evidence(
            copy_bt_net_pnl=1_500, copy_bt_7d_net_pnl=600,
        ), stage="rough")
        high_score, _ = compute_follow_score(evidence(
            copy_bt_net_pnl=5_000, copy_bt_7d_net_pnl=2_000,
        ), stage="rough")
        self.assertGreater(high_score, low_score)
        self.assertTrue(evaluate_follow_eligibility(
            evidence(), stage="strict", follow_score_value=.01,
        )["coreEligible"])

    def test_official_return_is_not_source_score_input(self):
        low, detail = compute_source_quality_score(evidence(
            official_perp_return_30d=-10,
        ), as_of_ms=NOW)
        high, _ = compute_source_quality_score(evidence(
            official_perp_return_30d=10,
        ), as_of_ms=NOW)
        self.assertEqual(detail["officialPerpContribution"], 0.0)
        self.assertAlmostEqual(low, high)


class ActivityTests(unittest.TestCase):
    def test_activity_uses_canonical_actionable_open_events(self):
        day = pre_strict.DAY_MS
        events = [
            {"time": NOW - 26 * day, "master_notional": 3_000, "minimum_notional": 2_500},
            {"time": NOW - 18 * day, "master_notional": 3_000, "minimum_notional": 2_500},
            {"time": NOW - 11 * day, "master_notional": 3_000, "minimum_notional": 2_500},
            {"time": NOW - 4 * day, "master_notional": 3_000, "minimum_notional": 2_500},
            {"time": NOW - 2 * day, "master_notional": 50, "minimum_notional": 2_500},
        ]
        activity = pre_strict.copy_activity({30: {"open_events": events}}, NOW)
        self.assertTrue(activity["operational"])
        self.assertEqual(activity["weeklyOpenCountsOldestFirst"], [1, 1, 1, 1])
        self.assertEqual(activity["actionableOpenEvents28d"], 4)

    def test_72_hours_has_no_veto(self):
        day = pre_strict.DAY_MS
        events = [
            {"time": NOW - 25 * day}, {"time": NOW - 17 * day},
            {"time": NOW - 9 * day}, {"time": NOW - 5 * day},
        ]
        activity = pre_strict.copy_activity({30: {"open_events": events}}, NOW)
        self.assertTrue(activity["operational"])
        self.assertGreater(NOW - max(event["time"] for event in events), 72 * 3_600_000)


if __name__ == "__main__":
    unittest.main()
