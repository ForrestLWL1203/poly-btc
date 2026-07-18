import json
import unittest

from hl.follow_score import compute_follow_score, evaluate_follow_eligibility


def evidence(**overrides):
    row = {
        "score": 0.70,
        "copy_bt_closed_n": 16,
        "copy_bt_14d_closed_n": 9,
        "copy_bt_7d_closed_n": 5,
        "copy_bt_net_pnl": 1800.0,
        "copy_bt_14d_net_pnl": 900.0,
        "copy_bt_7d_net_pnl": 600.0,
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

    def test_high_return_thin_sector_remains_sample_challenger(self):
        replay = {
            "copy_net_pnl": 1900.0,
            "unrealized_pnl": 0.0,
            "closed_n": 2,
            "wins": 2,
            "target_open_events": 2,
            "opened_n": 2,
            "liquidations": 0,
            "fee_drag": 5.0,
            "valuation_status": "complete",
        }
        result = evaluate_follow_eligibility(evidence(
            copy_bt_evidence_status="thin",
            sector_policy_json=json.dumps({
                "allowed": [],
                "watch": ["crypto"],
                "crypto": {"allow": False, "watch": True, "status": "thin_evidence"},
            }),
            sector_copy_json=json.dumps({
                "crypto": {"30": replay, "14": replay, "7": replay},
            }),
        ))

        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_sample_watch")

    def test_sample_complete_wallet_has_no_hidden_probability_gate(self):
        result = evaluate_follow_eligibility(evidence(copy_positive_probability=0.64))
        self.assertTrue(result["eligible"])
        self.assertTrue(result["coreEligible"])

    def test_near_core_thin_edge_with_strong_dollar_economics_is_challenger(self):
        result = evaluate_follow_eligibility(evidence(
            copy_expected_return=0.01886,
            copy_bt_net_pnl=1687,
            copy_bt_14d_net_pnl=853,
            copy_bt_7d_net_pnl=571,
            copy_bt_closed_n=50,
            copy_bt_14d_closed_n=25,
            copy_bt_7d_closed_n=10,
            copy_evidence_days=13,
            copy_positive_probability=0.8275,
            copy_return_lcb=-0.0132,
        ))

        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["role"], "challenger")
        self.assertEqual(result["status"], "challenger_thin_edge_watch")

    def test_materially_thin_edge_stays_rejected_despite_strict_copy_dollars(self):
        result = evaluate_follow_eligibility(evidence(
            copy_expected_return=0.005,
            copy_bt_net_pnl=1600,
            copy_bt_7d_net_pnl=400,
        ))

        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "thin_copy_edge")

    def test_near_boundary_thin_edge_is_challenger_not_core(self):
        result = evaluate_follow_eligibility(evidence(copy_expected_return=0.018))

        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_thin_edge_watch")

    def test_no_allowed_specialty_sector_is_rejected_even_with_global_profit(self):
        result = evaluate_follow_eligibility(evidence(
            sector_policy_json=json.dumps({
                "allowed": [],
                "crypto": {"allow": False, "status": "recent_loss"},
                "stock": {"allow": False, "status": "grid_dca"},
            }),
        ))

        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "no_allowed_sector")

    def test_execution_and_capacity_are_real_gates(self):
        low_fill = evaluate_follow_eligibility(evidence(actionable_open_rate=0.55))
        low_capacity = evaluate_follow_eligibility(evidence(capacity_fit=0.70))
        self.assertEqual(low_fill["status"], "low_fill_rate")
        self.assertEqual(low_capacity["status"], "capacity_fit_low")

    def test_recent_7d_loss_with_enough_sample_is_rejected(self):
        one = evaluate_follow_eligibility(evidence(copy_bt_14d_net_pnl=-50, copy_bt_7d_net_pnl=20))
        both = evaluate_follow_eligibility(evidence(copy_bt_14d_net_pnl=-50, copy_bt_7d_net_pnl=-220))
        self.assertFalse(one["eligible"])
        self.assertFalse(one["coreEligible"])
        self.assertEqual(one["status"], "copy_recent_value_below_challenger_floor")
        self.assertEqual(both["status"], "recent_copy_collapse")

    def test_strong_30d_evidence_does_not_override_sampled_14d_decline(self):
        result = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=2500, copy_bt_closed_n=40, copy_evidence_days=15,
            copy_bt_14d_closed_n=12, copy_bt_14d_net_pnl=-100,
            copy_bt_7d_closed_n=4, copy_bt_7d_net_pnl=80,
            copy_return_lcb=-0.02,
        ))

        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_recent_decline")

    def test_quality_tiers_match_operator_examples(self):
        strong = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=2365, copy_bt_closed_n=43, copy_bt_7d_closed_n=3,
            copy_bt_7d_net_pnl=772, copy_return_lcb=-0.009, copy_evidence_days=13,
        ))
        sample_watch = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=1604, copy_bt_closed_n=9, copy_bt_7d_closed_n=3,
            copy_bt_7d_net_pnl=517, copy_return_lcb=-0.067, copy_evidence_days=7,
        ))
        collapse = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=274, copy_bt_14d_net_pnl=-305, copy_bt_7d_net_pnl=-730,
            copy_bt_closed_n=24, copy_bt_14d_closed_n=18, copy_bt_7d_closed_n=10,
        ))
        recent_below_weekly_floor = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=3429, copy_bt_14d_net_pnl=1000, copy_bt_7d_net_pnl=-247,
            copy_bt_closed_n=33, copy_bt_14d_closed_n=15, copy_bt_7d_closed_n=10,
            copy_evidence_days=12,
        ))
        self.assertFalse(strong["coreEligible"])
        self.assertEqual(strong["status"], "challenger_sample_watch")
        self.assertEqual(sample_watch["status"], "challenger_sample_watch")
        self.assertEqual(collapse["status"], "recent_copy_collapse")
        self.assertEqual(
            recent_below_weekly_floor["status"], "copy_recent_value_below_challenger_floor"
        )

    def test_profit_floors_use_manual_margin_equity_budget(self):
        core = evaluate_follow_eligibility(
            evidence(copy_bt_net_pnl=800), margin_equity_pct=0.50,
        )
        strong = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=1000, copy_bt_closed_n=20, copy_evidence_days=10,
            copy_bt_7d_closed_n=5, copy_bt_7d_net_pnl=300, copy_return_lcb=-0.05,
        ), margin_equity_pct=0.50)
        rejected = evaluate_follow_eligibility(
            evidence(copy_bt_net_pnl=499), margin_equity_pct=0.50,
        )

        self.assertTrue(core["coreEligible"])
        self.assertTrue(strong["coreEligible"])
        self.assertTrue(strong["strongEntry"])
        self.assertEqual(rejected["status"], "copy_value_below_challenger_floor")

    def test_weekly_economic_floor_applies_to_standard_and_strong_core(self):
        standard = evaluate_follow_eligibility(evidence(copy_bt_7d_net_pnl=499))
        strong = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=2500, copy_bt_closed_n=25, copy_evidence_days=12,
            copy_bt_7d_closed_n=2, copy_bt_7d_net_pnl=499, copy_return_lcb=-0.05,
        ))

        self.assertEqual(standard["status"], "challenger_weekly_return_watch")
        self.assertEqual(strong["status"], "challenger_sample_watch")
        self.assertFalse(standard["coreEligible"])
        self.assertFalse(strong["coreEligible"])

    def test_profit_floors_use_total_realized_plus_open_pnl(self):
        result = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=1200, copy_bt_unrealized_pnl=-400,
            copy_bt_7d_net_pnl=600, copy_bt_7d_unrealized_pnl=-150,
        ))

        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "copy_value_below_challenger_floor")

    def test_profit_percentages_scale_with_canonical_replay_capital(self):
        result = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=1900, copy_bt_7d_net_pnl=900,
            initial_margin_equity=20_000,
        ))

        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "copy_value_below_challenger_floor")

    def test_missing_open_valuation_is_challenger_not_data_error(self):
        result = evaluate_follow_eligibility(evidence(copy_bt_valuation_status="missing_marks"))

        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_open_valuation_pending")

    def test_liquidation_is_risk_evidence_not_an_automatic_rejection(self):
        result = evaluate_follow_eligibility(evidence(copy_bt_liquidations=1))
        self.assertTrue(result["eligible"])

    def test_heavy_dca_pressure_pass_remains_challenger_even_with_core_economics(self):
        result = evaluate_follow_eligibility(evidence(
            sector_policy_json=json.dumps({
                "allowed": ["crypto"],
                "coreBlocked": True,
                "structuralWatch": ["crypto"],
                "crypto": {"allow": True, "status": "heavy_dca_watch"},
            }),
        ))

        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_structural_watch")

    def test_liquidation_frequency_is_bounded_ranking_evidence_not_a_veto(self):
        baseline, _ = compute_follow_score(evidence(copy_bt_liquidations=0, copy_risk_score=0.0))
        liquidated, detail = compute_follow_score(evidence(copy_bt_liquidations=3, copy_risk_score=0.0))
        self.assertLess(liquidated, baseline)
        self.assertGreater(liquidated, baseline - 0.10)
        self.assertAlmostEqual(detail["liquidationRate"], 3 / 16)
        self.assertTrue(any("损失已计收益" in reason for reason in detail["reasons"]))

    def test_strict_copy_economic_power_outranks_thin_profit_with_prettier_episode_stats(self):
        thin, _ = compute_follow_score(evidence(
            score=.48, copy_bt_net_pnl=1674, copy_bt_14d_net_pnl=-199,
            copy_bt_7d_net_pnl=580, copy_expected_return=.09, copy_return_lcb=.05,
            copy_positive_probability=1.0, copy_bt_closed_n=52, copy_evidence_days=18,
        ))
        strong, detail = compute_follow_score(evidence(
            score=.82, copy_bt_net_pnl=6813, copy_bt_14d_net_pnl=5193,
            copy_bt_7d_net_pnl=4228, copy_expected_return=.032, copy_return_lcb=.009,
            copy_positive_probability=.985, copy_bt_closed_n=88, copy_evidence_days=18,
        ))

        self.assertGreater(strong, thin)
        self.assertAlmostEqual(detail["economicReturns"]["30d"], .6813)

    def test_large_core_economics_do_not_saturate_at_the_minimum_admission_line(self):
        ordinary, ordinary_detail = compute_follow_score(evidence(
            score=.75, copy_bt_net_pnl=2442, copy_bt_14d_net_pnl=2031,
            copy_bt_7d_net_pnl=605, copy_expected_return=.076, copy_return_lcb=.021,
            copy_positive_probability=.983, copy_bt_closed_n=74, copy_evidence_days=21,
        ))
        exceptional, exceptional_detail = compute_follow_score(evidence(
            score=.82, copy_bt_net_pnl=7173, copy_bt_14d_net_pnl=5553,
            copy_bt_7d_net_pnl=4588, copy_expected_return=.032, copy_return_lcb=.009,
            copy_positive_probability=.985, copy_bt_closed_n=88, copy_evidence_days=18,
        ))

        self.assertGreater(exceptional, ordinary)
        self.assertGreater(
            exceptional_detail["economicScore"], ordinary_detail["economicScore"] + .40,
        )

    def test_economic_score_scales_with_actual_replay_equity_not_fixed_dollars(self):
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

    def test_negative_bootstrap_lcb_is_scored_but_not_an_automatic_rejection(self):
        result = evaluate_follow_eligibility(evidence(copy_return_lcb=-0.05))
        self.assertTrue(result["eligible"])
        self.assertTrue(result["coreEligible"])

    def test_five_recent_closes_are_sufficient_core_evidence(self):
        result = evaluate_follow_eligibility(evidence(
            copy_bt_closed_n=9,
            copy_bt_14d_closed_n=5,
            copy_bt_7d_closed_n=5,
            copy_evidence_days=5,
            copy_bt_net_pnl=3100,
            copy_bt_14d_net_pnl=3070,
            copy_bt_7d_net_pnl=3070,
            copy_return_lcb=-0.02,
            copy_positive_probability=0.55,
        ))

        self.assertTrue(result["eligible"])
        self.assertTrue(result["coreEligible"])
        self.assertEqual(result["status"], "core_eligible")

    def test_score_confidence_saturates_at_qualification_sample_floors(self):
        _score, detail = compute_follow_score(evidence(
            copy_bt_closed_n=7, copy_evidence_days=5,
        ))

        self.assertEqual(detail["confidence"], 1.0)

    def test_more_strict_copy_profit_at_the_same_equity_increases_wallet_score(self):
        low, _ = compute_follow_score(evidence(
            copy_bt_net_pnl=80, copy_bt_14d_net_pnl=35, copy_bt_7d_net_pnl=12,
        ))
        high, _ = compute_follow_score(evidence(
            copy_bt_net_pnl=80000, copy_bt_14d_net_pnl=35000, copy_bt_7d_net_pnl=12000,
        ))
        self.assertGreater(high, low)

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

if __name__ == "__main__":
    unittest.main()
