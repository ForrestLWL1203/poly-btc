import json
import unittest

from hyper.selection.follow_score import compute_follow_score, evaluate_follow_eligibility


NOW = 2_000_000_000_000


def stability(*, passed=True, sufficient=True, profitable=4):
    return {
        "version": "nonoverlap-weekly-return-v2", "evidenceSufficient": sufficient,
        "passed": passed, "evaluableFolds": 4 if sufficient else 1,
        "profitableFolds": profitable, "qualifiedFolds": profitable,
        "minReturn": 0.04, "minNetPerClosedReturn": 0.005,
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
        self.assertEqual(result["status"], "challenger_score_watch")

    def test_insufficient_campaign_evidence_is_challenger(self):
        result = judge(copy_bt_closed_n=4, copy_bt_campaign_closed_n=3, copy_evidence_days=2)
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_campaign_evidence_building")

    def test_core_uses_ten_campaigns_copy_weekly_profit_activity_and_stress(self):
        result = judge()
        self.assertTrue(result["coreEligible"])
        self.assertTrue(result["checks"]["tenIndependentCampaigns"])
        self.assertTrue(result["checks"]["strictCopyWeeklyPositive"])
        self.assertTrue(result["checks"]["activityWithin72h"])
        self.assertTrue(result["checks"]["campaignWinRate"])
        self.assertTrue(result["checks"]["repeatableBodyWinRate"])
        self.assertTrue(result["checks"]["coreFollowScore"])

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
            copy_bt_body_after_top3_net_pnl=800.0,
            copy_bt_payoff_ratio=2.47,
            copy_bt_profit_factor=2.52,
            copy_bt_net_pnl=2100.0,
            copy_bt_14d_net_pnl=1480.0,
            copy_bt_7d_net_pnl=1020.0,
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

    def test_rolling_7d_and_14d_results_do_not_repeat_core_gate(self):
        result = judge(
            copy_bt_14d_closed_n=0, copy_bt_7d_closed_n=0,
            copy_bt_14d_net_pnl=-10.0, copy_bt_7d_net_pnl=-10.0,
            copy_bt_campaign_wins=1, copy_bt_profit_factor=0.2,
        )
        self.assertTrue(result["coreEligible"])

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
        self.assertEqual(thin["status"], "challenger_copy_weekly_evidence_building")
        self.assertEqual(failed["status"], "challenger_copy_weekly_loss")

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

    def test_cost_stress_is_separate_core_gate(self):
        result = judge(copy_bt_cost_stress_net_pnl=-1.0)
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_cost_stress_watch")

    def test_one_isolated_liquidation_allowed_repeat_rejected(self):
        self.assertTrue(judge(copy_bt_liquidations=1)["coreEligible"])
        repeat = judge(copy_bt_liquidations=2)
        self.assertFalse(repeat["eligible"])
        self.assertTrue(repeat["hardRisk"])

    def test_path_risk_downgrades_then_rejects(self):
        challenger = judge(copy_intratrade_max_drawdown=0.13)
        rejected = judge(copy_intratrade_max_drawdown=0.151)
        self.assertEqual(challenger["status"], "challenger_intratrade_drawdown")
        self.assertEqual(rejected["role"], "rejected")
        self.assertTrue(rejected["hardRisk"])

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

    def test_copy_score_ignores_legacy_raw_and_overlapping_window_pnl(self):
        low, _ = compute_follow_score(evidence(
            score=.05,
            copy_bt_net_pnl=100,
            copy_bt_14d_net_pnl=-500,
            copy_bt_7d_net_pnl=-900,
        ))
        high, _ = compute_follow_score(evidence(
            score=.99,
            copy_bt_net_pnl=50_000,
            copy_bt_14d_net_pnl=40_000,
            copy_bt_7d_net_pnl=30_000,
        ))
        self.assertAlmostEqual(low, high)

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
