import json
import unittest

from hyper.selection.follow_score import (
    compute_follow_score,
    compute_profit_priority,
    compute_source_quality_score,
    evaluate_follow_eligibility,
    evaluate_source_quality,
    profit_priority_sort_key,
)


NOW = 2_000_000_000_000


def evidence(**overrides):
    row = {
        "official_perp_status": "passed",
        "official_perp_reason": "perp_prefilter_passed",
        "official_perp_return_30d": 0.40,
        "source_episode_n_30d": 20,
        "source_episode_n_7d": 5,
        "source_win_rate_30d": 0.80,
        "source_win_rate_7d": 0.80,
        "source_net_pnl_30d": 8_000.0,
        "source_net_pnl_7d": 1_500.0,
        "source_top3_profit_share": 0.50,
        "source_body_after_top3_n": 17,
        "source_body_after_top3_win_rate": 0.76,
        "source_body_after_top3_net_pnl": 1_000.0,
        "copy_bt_closed_n": 12,
        "copy_bt_7d_closed_n": 4,
        "copy_bt_win_rate": 0.75,
        "copy_bt_net_pnl": 3_000.0,
        "copy_bt_7d_net_pnl": 1_000.0,
        "copy_bt_window_start_equity": 10_000.0,
        "copy_bt_7d_window_start_equity": 12_000.0,
        "copy_bt_open_fill_rate": 0.90,
        "actionable_open_rate": 0.90,
        "copy_bt_behavior_replication_rate": 0.85,
        "copy_bt_liquidations": 0,
        "copy_bt_data_status": "valid",
        "copy_bt_evidence_status": "qualified",
        "copy_bt_valuation_status": "complete",
        "copy_path_risk_status": "complete",
        "last_copyable_open_ms": NOW - 12 * 3_600_000,
        "open_events_30d": 20,
        "sector_policy_json": json.dumps({"allowed": ["crypto"]}),
    }
    row.update(overrides)
    return row


def judge(stage="strict", **overrides):
    return evaluate_follow_eligibility(
        evidence(**overrides), stage=stage, as_of_ms=NOW,
    )


class SourceQualityTests(unittest.TestCase):
    def test_ten_episodes_seven_wins_passes_but_six_wins_fails(self):
        passed = evaluate_source_quality(evidence(
            source_episode_n_30d=10, source_win_rate_30d=0.70,
        ), as_of_ms=NOW)
        failed = evaluate_source_quality(evidence(
            source_episode_n_30d=10, source_win_rate_30d=0.60,
        ), as_of_ms=NOW)
        self.assertTrue(passed["eligible"])
        self.assertEqual(failed["firstFailure"], "source_win_rate_below_floor")

    def test_strong_low_frequency_lane_requires_all_four_proofs(self):
        passed = evaluate_source_quality(evidence(
            source_episode_n_30d=8,
            source_win_rate_30d=0.875,
            official_perp_return_30d=0.35,
            source_net_pnl_7d=100,
        ), as_of_ms=NOW)
        self.assertTrue(passed["eligible"])
        self.assertEqual(passed["qualityLane"], "strong_low_frequency")

        cases = (
            ({"source_episode_n_30d": 6}, "source_episode_evidence_insufficient"),
            (
                {"source_episode_n_30d": 8, "source_win_rate_30d": 0.84},
                "source_low_frequency_win_rate_below_floor",
            ),
            (
                {"source_episode_n_30d": 8, "source_win_rate_30d": 0.875,
                 "official_perp_return_30d": 0.29},
                "source_low_frequency_official_return_below_floor",
            ),
            (
                {"source_episode_n_30d": 8, "source_win_rate_30d": 0.875,
                 "official_perp_return_30d": 0.35, "source_net_pnl_7d": 0},
                "source_low_frequency_recent_not_profitable",
            ),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                result = evaluate_source_quality(evidence(**overrides), as_of_ms=NOW)
                self.assertEqual(result["firstFailure"], reason)

    def test_top3_concentration_is_conditional(self):
        unconcentrated = evaluate_source_quality(evidence(
            source_top3_profit_share=0.699,
            source_body_after_top3_win_rate=0.10,
            source_body_after_top3_net_pnl=-1_000,
        ), as_of_ms=NOW)
        concentrated_good = evaluate_source_quality(evidence(
            source_top3_profit_share=0.70,
            source_body_after_top3_win_rate=0.70,
            source_body_after_top3_net_pnl=0,
        ), as_of_ms=NOW)
        concentrated_bad = evaluate_source_quality(evidence(
            source_top3_profit_share=0.90,
            source_body_after_top3_win_rate=0.69,
            source_body_after_top3_net_pnl=500,
        ), as_of_ms=NOW)
        concentrated_losing = evaluate_source_quality(evidence(
            source_top3_profit_share=0.90,
            source_body_after_top3_win_rate=0.80,
            source_body_after_top3_net_pnl=-1,
        ), as_of_ms=NOW)
        self.assertTrue(unconcentrated["eligible"])
        self.assertTrue(concentrated_good["eligible"])
        self.assertEqual(
            concentrated_bad["firstFailure"], "source_concentrated_body_win_rate_low",
        )
        self.assertEqual(
            concentrated_losing["firstFailure"], "source_concentrated_body_unprofitable",
        )

    def test_source_prescore_is_monotonic(self):
        low = compute_source_quality_score(evidence(
            official_perp_return_30d=.20, source_win_rate_30d=.70,
            source_episode_n_30d=10,
        ), as_of_ms=NOW)[0]
        high = compute_source_quality_score(evidence(
            official_perp_return_30d=.80, source_win_rate_30d=.90,
            source_episode_n_30d=30,
        ), as_of_ms=NOW)[0]
        self.assertGreater(high, low)

    def test_short_history_uses_its_five_percent_official_floor_for_scoring(self):
        short_evidence = json.dumps({
            "windows": {
                "officialPerp30d": {
                    "historyTier": "short_history_7d",
                    "windowDays": 7,
                    "positiveCoverageDays": 15,
                    "minimumReturn": .05,
                },
            },
        })
        _score, detail = compute_source_quality_score(evidence(
            official_perp_return_30d=.05,
            official_perp_evidence_json=short_evidence,
        ), as_of_ms=NOW)

        self.assertAlmostEqual(detail["officialPerp30dScore"], .60)
        self.assertEqual(detail["officialPerpHistoryTier"], "short_history_7d")
        self.assertEqual(detail["officialPerpWindowDays"], 7)


class FollowScoreTests(unittest.TestCase):
    def test_profit_priority_uses_dynamic_window_equities_and_fixed_70_30_weights(self):
        priority, detail = compute_profit_priority(evidence(
            copy_bt_net_pnl=4_000,
            copy_bt_window_start_equity=20_000,
            copy_bt_7d_net_pnl=1_000,
            copy_bt_7d_window_start_equity=10_000,
        ))

        self.assertAlmostEqual(priority, .70 * .20 + .30 * .10)
        self.assertEqual(detail["mode"], "profit_70_30")
        self.assertEqual(detail["weights"], {"30d": .70, "7d": .30})
        self.assertEqual(detail["returns"], {"30d": .20, "7d": .10})

    def test_profit_priority_tie_breaks_by_30d_7d_score_then_address(self):
        rows = [
            (evidence(copy_bt_net_pnl=1_000, copy_bt_7d_net_pnl=1_000), .99, "0xz"),
            (evidence(copy_bt_net_pnl=1_300, copy_bt_7d_net_pnl=300), .80, "0xb"),
            (evidence(copy_bt_net_pnl=1_300, copy_bt_7d_net_pnl=300), .90, "0xc"),
            (evidence(copy_bt_net_pnl=1_300, copy_bt_7d_net_pnl=300), .90, "0xa"),
        ]
        # Give every row a $10k start in both windows. The first two rows both have 10% priority,
        # so the larger 30d return wins before the lower-priority score/address tie-breaks.
        for metrics, _score, _addr in rows:
            metrics["copy_bt_7d_window_start_equity"] = 10_000
        ordered = sorted(rows, key=lambda item: profit_priority_sort_key(
            item[0], follow_score_value=item[1], addr=item[2],
        ))

        self.assertEqual([item[2] for item in ordered], ["0xa", "0xc", "0xb", "0xz"])

    def test_rough_copy_contract_requires_both_windows_to_be_profitable(self):
        profitable = judge(
            "rough",
            copy_bt_net_pnl=1,
            copy_bt_window_start_equity=10_000,
            copy_bt_7d_net_pnl=1,
            copy_bt_7d_window_start_equity=12_000,
        )
        self.assertTrue(profitable["coreEligible"])
        self.assertEqual(
            judge("rough", copy_bt_net_pnl=0)["firstFailure"],
            "rough_copy_30d_not_profitable",
        )
        self.assertEqual(
            judge("rough", copy_bt_7d_net_pnl=0, copy_bt_7d_window_start_equity=12_000)[
                "firstFailure"
            ],
            "rough_copy_7d_not_profitable",
        )

    def test_strict_copy_uses_dynamic_window_equities(self):
        boundary = judge(
            "strict",
            copy_bt_net_pnl=1_000,
            copy_bt_window_start_equity=10_000,
            copy_bt_7d_net_pnl=300,
            copy_bt_7d_window_start_equity=10_000,
        )
        self.assertTrue(boundary["coreEligible"])
        self.assertAlmostEqual(boundary["returns"]["30"], .10)
        self.assertAlmostEqual(boundary["returns"]["7"], .03)
        result = judge(
            "strict", copy_bt_net_pnl=999, copy_bt_window_start_equity=10_000,
        )
        self.assertEqual(result["firstFailure"], "strict_copy_30d_return_below_floor")
        recent = judge(
            "strict", copy_bt_7d_net_pnl=599, copy_bt_7d_window_start_equity=20_000,
        )
        self.assertEqual(recent["firstFailure"], "strict_copy_7d_return_below_floor")
        self.assertAlmostEqual(recent["returns"]["7"], 599 / 20_000)

    def test_seven_day_closed_count_is_not_an_admission_gate(self):
        result = judge(
            "strict",
            copy_bt_7d_closed_n=0,
            copy_bt_7d_net_pnl=300,
            copy_bt_7d_window_start_equity=10_000,
        )
        self.assertTrue(result["coreEligible"])

    def test_copy_win_rate_open_rate_and_sample_are_independent(self):
        self.assertEqual(
            judge("rough", copy_bt_closed_n=6)["firstFailure"],
            "copy_episode_evidence_insufficient",
        )
        self.assertEqual(
            judge("rough", copy_bt_win_rate=.599)["firstFailure"],
            "rough_copy_win_rate_below_floor",
        )
        self.assertEqual(
            judge("strict", copy_bt_open_fill_rate=.699, actionable_open_rate=.699)[
                "firstFailure"
            ],
            "strict_copy_open_rate_below_floor",
        )

    def test_official_evidence_shortage_remains_challenger(self):
        result = judge(
            "strict",
            official_perp_status="deferred_data_error",
            official_perp_reason="history_under_28d",
        )
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertTrue(result["deferred"])
        self.assertEqual(result["firstFailure"], "history_under_28d")

    def test_activity_and_final_path_are_strict_permissions(self):
        stale = judge(
            "strict", last_copyable_open_ms=NOW - 73 * 3_600_000,
        )
        missing_path = judge("strict", copy_path_risk_status="missing")
        self.assertEqual(stale["firstFailure"], "source_activity_stale")
        self.assertEqual(missing_path["firstFailure"], "copy_path_incomplete")
        self.assertTrue(judge("rough", copy_path_risk_status="missing")["coreEligible"])

    def test_three_liquidations_pass_and_four_fail_only_strict(self):
        self.assertTrue(judge("strict", copy_bt_liquidations=3)["coreEligible"])
        self.assertEqual(
            judge("strict", copy_bt_liquidations=4)["firstFailure"],
            "strict_copy_liquidations_over_3",
        )
        self.assertTrue(judge("rough", copy_bt_liquidations=99)["coreEligible"])

    def test_score_is_ranking_only_with_exact_weight_components(self):
        low_score, low = compute_follow_score(evidence(
            copy_bt_net_pnl=1_500, copy_bt_7d_net_pnl=600,
        ), stage="rough")
        high_score, high = compute_follow_score(evidence(
            copy_bt_net_pnl=5_000, copy_bt_7d_net_pnl=2_000,
        ), stage="rough")
        self.assertGreater(high_score, low_score)
        self.assertGreater(high["components"]["copy30d"], low["components"]["copy30d"])
        # An arbitrary score below 75 is never a qualification input.
        result = evaluate_follow_eligibility(
            evidence(), stage="strict", as_of_ms=NOW, follow_score_value=.10,
        )
        self.assertTrue(result["coreEligible"])


if __name__ == "__main__":
    unittest.main()
