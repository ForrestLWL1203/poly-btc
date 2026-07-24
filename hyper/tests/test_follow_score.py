import json
import unittest

from hyper.selection.follow_score import compute_follow_score, evaluate_follow_eligibility


NOW = 2_000_000_000_000


def stability(*, passed=True, sufficient=True, profitable=4):
    return {
        "version": "nonoverlap-weekly-return-v3", "evidenceSufficient": sufficient,
        "passed": passed, "evaluableFolds": 4 if sufficient else 1,
        "profitableFolds": profitable, "qualifiedFolds": profitable,
        "lossBoundPassed": passed, "worstLossToTotalProfit": 0.0 if passed else 0.5,
        "minReturn": 0.0, "minNetPerClosedReturn": 0.005,
        "folds": [
            {
                "evaluable": sufficient or index == 0,
                "return": 0.08,
                "averageClosedNetReturn": 0.01,
                "qualified": index < profitable,
            }
            for index in range(4)
        ],
    }


def evidence(**overrides):
    policy = {"allowed": ["crypto"], "copyWeeklyProfitability": stability()}
    row = {
        "score": 0.70,
        "copy_bt_closed_n": 16,
        "copy_bt_campaign_closed_n": 12,
        "copy_bt_campaign_win_rate": 0.75,
        "copy_bt_profit_factor": 3.0,
        "copy_bt_payoff_ratio": 1.5,
        "copy_bt_top3_profit_share": 0.45,
        "copy_bt_body_after_top3_n": 13,
        "copy_bt_body_after_top3_win_rate": 0.69,
        "copy_bt_body_after_top3_net_pnl": 650.0,
        "copy_bt_net_pnl": 1800.0,
        "copy_bt_14d_net_pnl": 900.0,
        "copy_bt_7d_net_pnl": 600.0,
        "copy_bt_campaign_net_after_top1": 500.0,
        "copy_bt_cost_stress_net_pnl": 1200.0,
        "copy_expected_return": 0.045,
        "copy_return_lcb": 0.012,
        "copy_return_volatility": 0.08,
        "copy_positive_probability": 0.82,
        "copy_evidence_days": 10,
        "copy_risk_score": 0.80,
        "execution_score": 0.92,
        "actionable_open_rate": 0.90,
        "capacity_fit": 0.90,
        "copy_bt_liquidations": 0,
        "copy_bt_data_status": "valid",
        "copy_bt_evidence_status": "qualified",
        "copy_bt_valuation_status": "complete",
        "copy_path_risk_status": "complete",
        "copy_intratrade_max_drawdown": 0.08,
        "copy_deep_bag_event_n": 0,
        "copy_failed_deep_bag_n": 0,
        "copy_deep_bag_recovery_rate": 1.0,
        "initial_margin_equity": 10_000.0,
        "last_copyable_open_ms": NOW - 12 * 3_600_000,
        "sector_policy_json": json.dumps(policy),
    }
    row.update(overrides)
    return row


def judge(row=None, **overrides):
    return evaluate_follow_eligibility(
        evidence(**overrides) if row is None else row, as_of_ms=NOW,
    )


class FollowScoreTests(unittest.TestCase):
    def test_raw_without_copy_is_prior_only(self):
        score, detail = compute_follow_score({"score": 0.73})
        result = evaluate_follow_eligibility({"score": 0.73}, as_of_ms=NOW)
        self.assertAlmostEqual(score, 0.73 * 0.35)
        self.assertIsNone(detail["copyScore"])
        self.assertFalse(result["eligible"])

    def test_data_error_quarantines_and_zero_copy_rejects(self):
        broken = judge(copy_bt_data_status="replay_error")
        loss = judge(copy_bt_net_pnl=0.0)
        self.assertEqual(broken["role"], "quarantine")
        self.assertTrue(broken["deferred"])
        self.assertEqual(loss["status"], "copy_not_profitable")

    def test_any_positive_strict_copy_stays_challenger(self):
        floor_weekly = stability()
        for fold in floor_weekly["folds"]:
            fold["return"] = .041
            fold["averageClosedNetReturn"] = .0051
        result = judge(
            copy_bt_net_pnl=100.0,
            copy_expected_return=.021,
            copy_return_lcb=0.0,
            copy_positive_probability=.55,
            copy_risk_score=.60,
            execution_score=.75,
            actionable_open_rate=.70,
            capacity_fit=.75,
            open_probability_48h=.50,
            sector_policy_json=json.dumps({
                "allowed": ["crypto"], "copyWeeklyProfitability": floor_weekly,
            }),
        )
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["role"], "challenger")
        self.assertEqual(result["status"], "challenger_return_watch")

    def test_open_unrealized_profit_cannot_fake_realized_per_close_density(self):
        result = judge(
            copy_bt_net_pnl=1800.0,
            copy_bt_unrealized_pnl=1700.0,
            copy_bt_closed_n=16,
        )

        self.assertFalse(result["checks"]["averageNetPerClose"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_thin_profit_watch")

    def test_insufficient_campaign_evidence_is_challenger(self):
        result = judge(copy_bt_closed_n=4, copy_bt_campaign_closed_n=3, copy_evidence_days=2)
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_campaign_evidence_building")

    def test_core_uses_campaigns_copy_weekly_profit_activity_and_score(self):
        result = judge()
        self.assertTrue(result["coreEligible"])
        self.assertTrue(result["checks"]["independentCampaignEvidence"])
        self.assertTrue(result["checks"]["strictCopyWeeklyPositive"])
        self.assertTrue(result["checks"]["activityWithin72h"])
        self.assertTrue(result["checks"]["campaignWinRate"])
        self.assertTrue(result["checks"]["repeatableBodyWinRate"])
        self.assertTrue(result["checks"]["coreFollowScore"])

    def test_body_cost_and_individual_capacity_are_diagnostics_not_duplicate_hard_gates(self):
        row = evidence(
            copy_bt_campaign_closed_n=8,
            copy_bt_body_after_top3_win_rate=0.0,
            copy_bt_body_after_top3_net_pnl=-500.0,
            copy_bt_cost_stress_net_pnl=-1.0,
            actionable_open_rate=0.1,
            capacity_fit=0.1,
        )
        result = evaluate_follow_eligibility(
            row, as_of_ms=NOW, follow_score_value=0.80,
        )
        self.assertTrue(result["coreEligible"])
        self.assertFalse(result["checks"]["repeatableBodyWinRate"])
        self.assertFalse(result["checks"]["repeatableBodyPositive"])
        self.assertFalse(result["checks"]["costStressPositive"])
        self.assertFalse(result["checks"]["openExecution"])
        self.assertFalse(result["checks"]["capacity"])

    def test_low_win_timing_sensitive_wallet_stays_challenger(self):
        result = judge(
            copy_bt_campaign_win_rate=0.41,
            copy_bt_body_after_top3_win_rate=0.37,
            copy_bt_payoff_ratio=3.0,
            copy_bt_profit_factor=2.1,
        )
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_repeatability_watch")

    def test_asymmetric_wallet_can_pass_with_repeatable_body(self):
        result = judge(
            copy_bt_campaign_closed_n=50,
            copy_bt_closed_n=100,
            copy_bt_campaign_win_rate=0.48,
            copy_bt_body_after_top3_n=97,
            copy_bt_body_after_top3_win_rate=0.49,
            copy_bt_body_after_top3_net_pnl=3600.0,
            copy_bt_payoff_ratio=2.47,
            copy_bt_profit_factor=2.52,
            copy_bt_net_pnl=6000.0,
            copy_bt_14d_net_pnl=3000.0,
            copy_bt_7d_net_pnl=1500.0,
            copy_expected_return=0.04,
            copy_return_lcb=0.008,
            copy_positive_probability=0.975,
            copy_evidence_days=20,
            copy_risk_score=0.69,
            open_probability_48h=0.999,
        )
        self.assertTrue(result["coreEligible"])
        self.assertGreaterEqual(result["followScore"], 0.75)

    def test_perfect_small_sample_is_shrunk_below_core_score(self):
        row = evidence(
            copy_bt_closed_n=7,
            copy_bt_campaign_closed_n=6,
            copy_bt_campaign_win_rate=1.0,
            copy_bt_body_after_top3_n=4,
            copy_bt_body_after_top3_win_rate=1.0,
            copy_bt_body_after_top3_net_pnl=189.0,
            copy_bt_top3_profit_share=0.82,
            copy_bt_profit_factor=999.0,
            copy_bt_payoff_ratio=999.0,
            copy_bt_net_pnl=1041.0,
            copy_bt_14d_net_pnl=958.0,
            copy_bt_7d_net_pnl=958.0,
            copy_expected_return=0.147,
            copy_return_lcb=0.067,
            copy_positive_probability=0.999,
            copy_evidence_days=6,
            copy_risk_score=1.0,
            execution_score=1.0,
            actionable_open_rate=1.0,
            capacity_fit=1.0,
            open_probability_48h=0.37,
            sector_policy_json=json.dumps({
                "allowed": ["crypto"],
                "copyWeeklyProfitability": stability(passed=False, sufficient=False),
            }),
        )
        score, _ = compute_follow_score(row)
        result = evaluate_follow_eligibility(row, as_of_ms=NOW)
        self.assertLess(score, 0.75)
        self.assertFalse(result["coreEligible"])

    def test_rolling_7d_is_a_core_profit_gate_but_14d_is_not(self):
        result = judge(
            copy_bt_14d_closed_n=0, copy_bt_7d_closed_n=0,
            copy_bt_14d_net_pnl=-10.0, copy_bt_7d_net_pnl=-10.0,
            copy_bt_campaign_wins=1, copy_bt_profit_factor=0.2,
        )
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_recent_return_watch")

    def test_copy_weekly_insufficient_is_unknown_and_one_losing_fold_is_watch(self):
        thin_policy = {
            "allowed": ["crypto"],
            "copyWeeklyProfitability": stability(passed=False, sufficient=False),
        }
        failed_policy = {
            "allowed": ["crypto"],
            "copyWeeklyProfitability": stability(passed=False, profitable=3),
        }
        thin = judge(sector_policy_json=json.dumps(thin_policy))
        failed = judge(sector_policy_json=json.dumps(failed_policy))
        self.assertEqual(thin["status"], "challenger_copy_fold_evidence_building")
        self.assertEqual(failed["status"], "challenger_copy_timing_instability")

    def test_activity_is_72_hour_core_permission_not_rejection(self):
        edge = judge(last_copyable_open_ms=NOW - 72 * 3_600_000)
        stale = judge(last_copyable_open_ms=NOW - 73 * 3_600_000)
        self.assertTrue(edge["coreEligible"])
        self.assertTrue(stale["eligible"])
        self.assertFalse(stale["coreEligible"])
        self.assertEqual(stale["status"], "challenger_activity_watch")

    def test_only_one_campaign_is_removed_for_outlier_stress(self):
        top1 = judge(copy_bt_campaign_net_after_top1=-1.0)
        legacy_top2 = judge(copy_bt_campaign_net_after_top2=-999.0)
        self.assertEqual(top1["status"], "challenger_outlier_watch")
        self.assertTrue(legacy_top2["coreEligible"])

    def test_cost_stress_is_diagnostic_at_individual_layer(self):
        result = judge(copy_bt_cost_stress_net_pnl=-1.0)
        self.assertTrue(result["eligible"])
        self.assertTrue(result["coreEligible"])
        self.assertFalse(result["checks"]["costStressPositive"])

    def test_up_to_three_proxy_liquidations_allowed_fourth_waits_for_retune(self):
        self.assertTrue(judge(copy_bt_liquidations=3)["coreEligible"])
        repeat = judge(copy_bt_liquidations=4)
        self.assertTrue(repeat["eligible"])
        self.assertFalse(repeat["coreEligible"])
        self.assertEqual(repeat["status"], "challenger_liquidation_tuning")
        self.assertNotIn("hardRisk", repeat)

    def test_historical_max_drawdown_is_diagnostic_not_admission(self):
        result = judge(copy_intratrade_max_drawdown=0.80)
        self.assertTrue(result["coreEligible"])
        self.assertEqual(
            result["simulatedPathRisk"]["intratradeMaxDrawdown"], 0.80,
        )

    def test_no_copyable_sector_rejects_but_watch_sector_stays_challenger(self):
        none = judge(sector_policy_json=json.dumps({"allowed": [], "watch": []}))
        watch = judge(sector_policy_json=json.dumps({
            "allowed": [], "watch": ["crypto"], "copyWeeklyProfitability": stability(),
        }))
        self.assertEqual(none["status"], "no_copyable_sector")
        self.assertTrue(watch["eligible"])
        self.assertFalse(watch["coreEligible"])

    def test_score_scales_with_replay_equity(self):
        small, small_detail = compute_follow_score(evidence(
            copy_bt_net_pnl=3000, copy_bt_14d_net_pnl=1500, copy_bt_7d_net_pnl=800,
            initial_margin_equity=10_000,
        ))
        large, large_detail = compute_follow_score(evidence(
            copy_bt_net_pnl=6000, copy_bt_14d_net_pnl=3000, copy_bt_7d_net_pnl=1600,
            initial_margin_equity=20_000,
        ))
        self.assertAlmostEqual(small, large)
        self.assertEqual(small_detail["economicReturns"], large_detail["economicReturns"])

    def test_recent_roi_uses_rolling_window_start_equity(self):
        row = evidence(
            copy_bt_net_pnl=3000,
            copy_bt_7d_net_pnl=600,
            copy_bt_window_start_equity=20_000,
            copy_bt_7d_window_start_equity=30_000,
        )

        score, detail = compute_follow_score(row)
        result = evaluate_follow_eligibility(row, as_of_ms=NOW, follow_score_value=score)

        self.assertAlmostEqual(detail["economicReturns"]["30d"], 0.15)
        self.assertAlmostEqual(detail["economicReturns"]["7d"], 0.02)
        self.assertEqual(
            detail["economicEquities"],
            {"30d": 20_000, "14d": 10_000, "7d": 30_000},
        )
        self.assertAlmostEqual(result["returns"]["30"], 0.15)
        self.assertAlmostEqual(result["returns"]["7"], 0.02)
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_recent_return_watch")

    def test_copy_score_ignores_legacy_raw_and_14d_overlap(self):
        low, _ = compute_follow_score(evidence(
            score=.05,
            copy_bt_net_pnl=1800,
            copy_bt_14d_net_pnl=-500,
            copy_bt_7d_net_pnl=600,
        ))
        high, _ = compute_follow_score(evidence(
            score=.99,
            copy_bt_net_pnl=1800,
            copy_bt_14d_net_pnl=40_000,
            copy_bt_7d_net_pnl=600,
        ))
        self.assertAlmostEqual(low, high)

    def test_stronger_30d_and_7d_follower_returns_raise_score(self):
        low, low_detail = compute_follow_score(evidence(
            copy_bt_net_pnl=1000, copy_bt_7d_net_pnl=500,
        ))
        high, high_detail = compute_follow_score(evidence(
            copy_bt_net_pnl=3000, copy_bt_7d_net_pnl=1500,
        ))
        self.assertGreater(high, low)
        self.assertGreater(
            high_detail["weeklyEconomics"]["return30Score"],
            low_detail["weeklyEconomics"]["return30Score"],
        )
        self.assertGreater(
            high_detail["weeklyEconomics"]["return7Score"],
            low_detail["weeklyEconomics"]["return7Score"],
        )

    def test_material_account_returns_are_not_failed_by_thin_single_week_density(self):
        weekly = stability()
        returns = (.052, .029, .047, .080)
        densities = (.0052, .0005, .0014, .0026)
        for fold, fold_return, density in zip(weekly["folds"], returns, densities):
            fold["return"] = fold_return
            fold["averageClosedNetReturn"] = density
        row = evidence(
            copy_bt_net_pnl=2250,
            copy_bt_7d_net_pnl=1120,
            sector_policy_json=json.dumps({
                "allowed": ["crypto"], "copyWeeklyProfitability": weekly,
            }),
        )

        score, detail = compute_follow_score(row)
        result = evaluate_follow_eligibility(row, as_of_ms=NOW)

        self.assertGreaterEqual(score, .75)
        self.assertTrue(result["coreEligible"])
        self.assertGreater(detail["economicScore"], .75)

    def test_stronger_independent_weekly_economics_raise_score(self):
        baseline = stability()
        stronger = stability()
        for fold in stronger["folds"]:
            fold["return"] = .16
            fold["averageClosedNetReturn"] = .03
        low, low_detail = compute_follow_score(evidence(sector_policy_json=json.dumps({
            "allowed": ["crypto"], "copyWeeklyProfitability": baseline,
        })))
        high, high_detail = compute_follow_score(evidence(sector_policy_json=json.dumps({
            "allowed": ["crypto"], "copyWeeklyProfitability": stronger,
        })))
        self.assertGreater(high, low)
        self.assertGreater(high_detail["economicScore"], low_detail["economicScore"])


if __name__ == "__main__":
    unittest.main()
