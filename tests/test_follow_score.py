import unittest

from hl.follow_score import choose_follow_line, compute_follow_score, evaluate_follow_eligibility


def evidence(**overrides):
    row = {
        "score": 0.70,
        "copy_bt_closed_n": 16,
        "copy_bt_14d_closed_n": 9,
        "copy_bt_7d_closed_n": 5,
        "copy_bt_net_pnl": 800.0,
        "copy_bt_14d_net_pnl": 350.0,
        "copy_bt_7d_net_pnl": 120.0,
        "copy_expected_return": 0.045,
        "copy_return_lcb": 0.012,
        "copy_return_volatility": 0.08,
        "copy_positive_probability": 0.82,
        "copy_evidence_days": 10,
        "copy_recent_return_14d": 0.04,
        "copy_recent_return_7d": 0.03,
        "copy_risk_score": 0.80,
        "execution_score": 0.92,
        "actionable_open_rate": 0.90,
        "capacity_fit": 0.90,
        "open_probability_48h": 0.75,
        "copy_bt_liquidations": 0,
        "copy_bt_data_status": "valid",
        "copy_bt_evidence_status": "qualified",
    }
    row.update(overrides)
    return row


class FollowScoreTests(unittest.TestCase):
    def test_raw_without_copy_is_only_a_prior_and_not_core_eligible(self):
        score, detail = compute_follow_score({"score": 0.73})
        eligibility = evaluate_follow_eligibility({"score": 0.73})
        self.assertAlmostEqual(score, 0.73 * 0.35)
        self.assertIsNone(detail["copyScore"])
        self.assertFalse(eligibility["eligible"])
        self.assertEqual(eligibility["role"], "rejected")

    def test_explicit_no_evidence_is_rejected_at_profile_qualification(self):
        result = evaluate_follow_eligibility({
            "score": 0.95,
            "copy_bt_data_status": "valid",
            "copy_bt_evidence_status": "no_evidence",
        })
        self.assertFalse(result["eligible"])
        self.assertEqual(result["role"], "rejected")

    def test_replay_error_is_quarantined(self):
        result = evaluate_follow_eligibility({
            "score": 0.95,
            "copy_bt_data_status": "replay_error",
            "copy_bt_evidence_status": "invalid",
        })
        self.assertFalse(result["eligible"])
        self.assertTrue(result["deferred"])
        self.assertEqual(result["role"], "quarantine")

    def test_normalized_positive_evidence_is_eligible(self):
        result = evaluate_follow_eligibility(evidence())
        self.assertTrue(result["eligible"])

    def test_independent_days_not_overlapping_window_counts_control_confidence(self):
        thin = evidence(copy_evidence_days=2, copy_bt_14d_closed_n=16, copy_bt_7d_closed_n=16)
        result = evaluate_follow_eligibility(thin)
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_sample_watch")

    def test_low_positive_probability_is_challenger(self):
        result = evaluate_follow_eligibility(evidence(copy_positive_probability=0.64))
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_confidence_watch")

    def test_execution_and_capacity_are_real_gates(self):
        low_fill = evaluate_follow_eligibility(evidence(actionable_open_rate=0.55))
        low_capacity = evaluate_follow_eligibility(evidence(capacity_fit=0.70))
        self.assertEqual(low_fill["status"], "low_fill_rate")
        self.assertEqual(low_capacity["status"], "capacity_fit_low")

    def test_recent_7d_loss_with_enough_sample_is_rejected(self):
        one = evaluate_follow_eligibility(evidence(copy_bt_14d_net_pnl=-50, copy_bt_7d_net_pnl=20))
        both = evaluate_follow_eligibility(evidence(copy_bt_14d_net_pnl=-50, copy_bt_7d_net_pnl=-220))
        self.assertTrue(one["eligible"])
        self.assertEqual(both["status"], "recent_copy_collapse")

    def test_quality_tiers_match_operator_examples(self):
        strong = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=1365, copy_bt_closed_n=43, copy_bt_7d_closed_n=3,
            copy_bt_7d_net_pnl=772, copy_return_lcb=-0.009, copy_evidence_days=13,
        ))
        sample_watch = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=504, copy_bt_closed_n=9, copy_bt_7d_closed_n=3,
            copy_bt_7d_net_pnl=217, copy_return_lcb=-0.067, copy_evidence_days=7,
        ))
        collapse = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=274, copy_bt_14d_net_pnl=-305, copy_bt_7d_net_pnl=-730,
            copy_bt_closed_n=24, copy_bt_14d_closed_n=18, copy_bt_7d_closed_n=10,
        ))
        protected = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=3429, copy_bt_14d_net_pnl=1000, copy_bt_7d_net_pnl=-247,
            copy_bt_closed_n=33, copy_bt_14d_closed_n=15, copy_bt_7d_closed_n=10,
            copy_evidence_days=12,
        ))
        self.assertTrue(strong["coreEligible"])
        self.assertEqual(sample_watch["status"], "challenger_sample_watch")
        self.assertEqual(collapse["status"], "recent_copy_collapse")
        self.assertTrue(protected["coreEligible"])

    def test_liquidation_is_risk_evidence_not_an_automatic_rejection(self):
        result = evaluate_follow_eligibility(evidence(copy_bt_liquidations=1))
        self.assertTrue(result["eligible"])

    def test_liquidation_count_is_not_charged_twice_after_risk_and_pnl(self):
        baseline, _ = compute_follow_score(evidence(copy_bt_liquidations=0, copy_risk_score=0.0))
        liquidated, detail = compute_follow_score(evidence(copy_bt_liquidations=3, copy_risk_score=0.0))
        self.assertAlmostEqual(liquidated, baseline)
        self.assertTrue(any("已计入收益/回撤" in reason for reason in detail["reasons"]))

    def test_negative_bootstrap_lcb_is_scored_but_not_an_automatic_rejection(self):
        result = evaluate_follow_eligibility(evidence(copy_return_lcb=-0.05))
        self.assertTrue(result["eligible"])

    def test_absolute_dollar_scale_does_not_change_wallet_score(self):
        low, _ = compute_follow_score(evidence(
            copy_bt_net_pnl=80, copy_bt_14d_net_pnl=35, copy_bt_7d_net_pnl=12,
        ))
        high, _ = compute_follow_score(evidence(
            copy_bt_net_pnl=80000, copy_bt_14d_net_pnl=35000, copy_bt_7d_net_pnl=12000,
        ))
        self.assertAlmostEqual(low, high)

    def test_overlapping_window_counts_do_not_create_confidence(self):
        low, low_detail = compute_follow_score(evidence(copy_bt_14d_closed_n=1, copy_bt_7d_closed_n=1))
        high, high_detail = compute_follow_score(evidence(copy_bt_14d_closed_n=100, copy_bt_7d_closed_n=100))
        self.assertAlmostEqual(low, high)
        self.assertAlmostEqual(low_detail["confidence"], high_detail["confidence"])

    def test_copy_evidence_dominates_small_raw_score_difference(self):
        weak, _ = compute_follow_score(evidence(
            score=0.76, copy_expected_return=0.01, copy_return_lcb=-0.01,
            copy_positive_probability=0.71, copy_risk_score=0.55,
        ))
        strong, detail = compute_follow_score(evidence(
            score=0.70, copy_expected_return=0.07, copy_return_lcb=0.025,
            copy_positive_probability=0.90, copy_risk_score=0.85,
        ))
        self.assertGreater(strong, weak)
        self.assertEqual(detail["evidenceDays"], 10)

    def test_recent_normalized_loss_demotes_continuously(self):
        baseline, _ = compute_follow_score(evidence())
        degraded, detail = compute_follow_score(evidence(
            copy_recent_return_14d=-0.03, copy_recent_return_7d=-0.04,
        ))
        self.assertLess(degraded, baseline)
        self.assertTrue(any("归一化收益为负" in reason for reason in detail["reasons"]))

    def test_choose_follow_line_remains_migration_fallback(self):
        ranked = [{"follow_score": s} for s in (0.90, 0.86, 0.83, 0.80, 0.70, 0.68)]
        choice = choose_follow_line(ranked, min_score=0.50, min_n=3, target_n=5, max_n=6, cliff_gap=0.08)
        self.assertEqual(choice["reason"], "quality_cliff")
        self.assertEqual(choice["count"], 4)


if __name__ == "__main__":
    unittest.main()
