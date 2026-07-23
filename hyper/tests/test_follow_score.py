import json
import unittest

from hyper.selection.follow_score import compute_follow_score, evaluate_follow_eligibility


NOW = 2_000_000_000_000


def stability(*, passed=True, sufficient=True, profitable=3):
    return {
        "version": "nonoverlap-campaign-v1", "evidenceSufficient": sufficient,
        "passed": passed, "evaluableFolds": 3 if sufficient else 1,
        "profitableFolds": profitable,
    }


def evidence(**overrides):
    policy = {"allowed": ["crypto"], "stability": stability()}
    row = {
        "score": 0.70,
        "copy_bt_closed_n": 16,
        "copy_bt_campaign_closed_n": 12,
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
        result = judge(copy_bt_net_pnl=100.0)
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["role"], "challenger")
        self.assertEqual(result["status"], "challenger_return_watch")

    def test_insufficient_campaign_evidence_is_challenger(self):
        result = judge(copy_bt_closed_n=4, copy_bt_campaign_closed_n=3, copy_evidence_days=2)
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_campaign_evidence_building")

    def test_core_uses_ten_campaigns_nonoverlap_activity_and_stress(self):
        result = judge()
        self.assertTrue(result["coreEligible"])
        self.assertTrue(result["checks"]["tenIndependentCampaigns"])
        self.assertTrue(result["checks"]["nonoverlapStability"])
        self.assertTrue(result["checks"]["activityWithin72h"])

    def test_rolling_7d_and_14d_results_do_not_repeat_core_gate(self):
        result = judge(
            copy_bt_14d_closed_n=0, copy_bt_7d_closed_n=0,
            copy_bt_14d_net_pnl=-999.0, copy_bt_7d_net_pnl=-999.0,
            copy_bt_campaign_wins=1, copy_bt_profit_factor=0.2,
        )
        self.assertTrue(result["coreEligible"])

    def test_nonoverlap_insufficient_is_unknown_and_failed_stability_is_watch(self):
        thin_policy = {"allowed": ["crypto"], "stability": stability(passed=False, sufficient=False)}
        failed_policy = {"allowed": ["crypto"], "stability": stability(passed=False, profitable=1)}
        thin = judge(sector_policy_json=json.dumps(thin_policy))
        failed = judge(sector_policy_json=json.dumps(failed_policy))
        self.assertEqual(thin["status"], "challenger_stability_evidence_building")
        self.assertEqual(failed["status"], "challenger_stability_watch")

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
            "allowed": [], "watch": ["crypto"], "stability": stability(),
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


if __name__ == "__main__":
    unittest.main()
