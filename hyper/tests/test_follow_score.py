import json
import unittest

from hyper.selection.follow_score import compute_follow_score, evaluate_follow_eligibility


def evidence(**overrides):
    row = {
        "score": 0.70,
        "copy_bt_closed_n": 16,
        "copy_bt_14d_closed_n": 9,
        "copy_bt_7d_closed_n": 5,
        "copy_bt_campaign_closed_n": 12,
        "copy_bt_campaign_wins": 9,
        "copy_bt_14d_campaign_closed_n": 6,
        "copy_bt_14d_campaign_wins": 4,
        "copy_bt_7d_campaign_closed_n": 5,
        "copy_bt_7d_campaign_wins": 4,
        "copy_bt_win_rate": 0.75,
        "copy_bt_14d_win_rate": 2 / 3,
        "copy_bt_7d_win_rate": 0.80,
        "copy_bt_net_pnl": 1800.0,
        "copy_bt_14d_net_pnl": 900.0,
        "copy_bt_7d_net_pnl": 600.0,
        "copy_bt_profit_factor": 1.60,
        "copy_bt_campaign_net_after_top2": 400.0,
        "copy_bt_cost_stress_net_pnl": 1200.0,
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
        "copy_bt_liquidations": 0,
        "copy_bt_data_status": "valid",
        "copy_bt_evidence_status": "qualified",
        "copy_bt_valuation_status": "complete",
        "copy_path_risk_status": "complete",
        "copy_intratrade_max_drawdown": 0.08,
        "copy_deep_bag_event_n": 0,
        "copy_failed_deep_bag_n": 0,
        "copy_deep_bag_recovery_rate": 1.0,
        "copy_max_deep_bag_hours": 0.0,
        "initial_margin_equity": 10_000.0,
    }
    row.update(overrides)
    return row


class FollowScoreTests(unittest.TestCase):
    def test_raw_without_copy_is_prior_only(self):
        score, detail = compute_follow_score({"score": 0.73})
        result = evaluate_follow_eligibility({"score": 0.73})
        self.assertAlmostEqual(score, 0.73 * 0.35)
        self.assertIsNone(detail["copyScore"])
        self.assertFalse(result["eligible"])

    def test_data_errors_quarantine_but_economic_disqualification_keeps_its_label(self):
        broken = evaluate_follow_eligibility(evidence(copy_bt_data_status="replay_error"))
        economic = evaluate_follow_eligibility(evidence(
            copy_bt_evidence_status="economically_disqualified",
        ))
        self.assertTrue(broken["deferred"])
        self.assertEqual(broken["role"], "quarantine")
        self.assertEqual(economic["status"], "economically_disqualified")
        self.assertEqual(economic["role"], "rejected")

    def test_research_is_positive_but_non_executable(self):
        result = evaluate_follow_eligibility(evidence(copy_bt_net_pnl=250.0))
        loss = evaluate_follow_eligibility(evidence(copy_bt_net_pnl=0.0))
        self.assertFalse(result["eligible"])
        self.assertTrue(result["researchEligible"])
        self.assertEqual(result["role"], "research")
        self.assertEqual(loss["status"], "copy_not_profitable")

    def test_challenger_requires_five_percent_and_independent_evidence(self):
        challenger = evaluate_follow_eligibility(evidence(copy_bt_net_pnl=600.0))
        thin = evaluate_follow_eligibility(evidence(
            copy_bt_net_pnl=600.0, copy_bt_closed_n=6,
            copy_bt_campaign_closed_n=4, copy_evidence_days=4,
        ))
        self.assertTrue(challenger["eligible"])
        self.assertFalse(challenger["coreEligible"])
        self.assertEqual(challenger["role"], "challenger")
        self.assertEqual(thin["status"], "research_insufficient_evidence")

    def test_challenger_requires_campaign_tail_and_cost_stress_evidence(self):
        missing = evaluate_follow_eligibility(evidence(copy_bt_cost_stress_net_pnl=None))
        tail_loss = evaluate_follow_eligibility(evidence(copy_bt_campaign_net_after_top2=-1.0))
        cost_loss = evaluate_follow_eligibility(evidence(copy_bt_cost_stress_net_pnl=-1.0))
        self.assertEqual(missing["status"], "research_stress_evidence_missing")
        self.assertEqual(tail_loss["status"], "copy_campaign_tail_weak")
        self.assertEqual(cost_loss["status"], "copy_cost_stress_weak")
        self.assertTrue(tail_loss["hardRisk"])

    def test_core_entry_and_strong_core_return_lines(self):
        core = evaluate_follow_eligibility(evidence(copy_bt_net_pnl=1000.0))
        strong = evaluate_follow_eligibility(evidence(copy_bt_net_pnl=2000.0))
        self.assertTrue(core["coreEligible"])
        self.assertFalse(core["strongEntry"])
        self.assertTrue(strong["coreEligible"])
        self.assertTrue(strong["strongEntry"])

    def test_return_denominator_is_full_risk_capital_not_margin_budget(self):
        result = evaluate_follow_eligibility(
            evidence(copy_bt_net_pnl=800.0), margin_equity_pct=0.50,
        )
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_return_watch")

    def test_core_sample_and_campaign_floors_are_12_5_3_and_ten(self):
        for changes in (
            {"copy_bt_closed_n": 11},
            {"copy_bt_14d_closed_n": 4},
            {"copy_bt_7d_closed_n": 2},
            {"copy_bt_campaign_closed_n": 9},
        ):
            with self.subTest(changes=changes):
                result = evaluate_follow_eligibility(evidence(**changes))
                self.assertTrue(result["eligible"])
                self.assertFalse(result["coreEligible"])
                self.assertEqual(result["status"], "challenger_sample_watch")

    def test_strong_sparse_route_requires_return_win_confidence_and_recent_wins(self):
        strong = evaluate_follow_eligibility(evidence(
            copy_bt_closed_n=10,
            copy_bt_14d_closed_n=4,
            copy_bt_7d_closed_n=5,
            copy_bt_campaign_closed_n=10,
            copy_bt_campaign_wins=9,
            copy_bt_7d_campaign_closed_n=5,
            copy_bt_7d_campaign_wins=4,
            copy_bt_net_pnl=2200.0,
            copy_evidence_days=8,
        ))
        weak_win = evaluate_follow_eligibility(evidence(
            copy_bt_closed_n=10,
            copy_bt_14d_closed_n=4,
            copy_bt_7d_closed_n=5,
            copy_bt_campaign_closed_n=10,
            copy_bt_campaign_wins=7,
            copy_bt_7d_campaign_closed_n=5,
            copy_bt_7d_campaign_wins=4,
            copy_bt_net_pnl=2200.0,
            copy_evidence_days=8,
        ))
        weak_recent = evaluate_follow_eligibility(evidence(
            copy_bt_closed_n=10,
            copy_bt_14d_closed_n=4,
            copy_bt_7d_closed_n=5,
            copy_bt_campaign_closed_n=10,
            copy_bt_campaign_wins=9,
            copy_bt_7d_campaign_closed_n=5,
            copy_bt_7d_campaign_wins=3,
            copy_bt_net_pnl=2200.0,
            copy_evidence_days=8,
        ))

        self.assertTrue(strong["coreEligible"])
        self.assertTrue(strong["strongSparseEntry"])
        self.assertFalse(weak_win["coreEligible"])
        self.assertFalse(weak_recent["coreEligible"])

    def test_core_requires_three_percent_recent_return(self):
        result = evaluate_follow_eligibility(evidence(copy_bt_7d_net_pnl=100.0))

        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_weekly_return_watch")

    def test_campaign_win_rate_and_wilson_control_core_not_challenger(self):
        result = evaluate_follow_eligibility(evidence(
            copy_bt_campaign_closed_n=10, copy_bt_campaign_wins=6,
        ))
        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["status"], "challenger_win_rate_watch")

    def test_retention_uses_seven_percent_and_softer_win_surface(self):
        metrics = evidence(
            copy_bt_net_pnl=800.0,
            copy_bt_campaign_closed_n=10, copy_bt_campaign_wins=6,
        )
        entry = evaluate_follow_eligibility(metrics)
        retained = evaluate_follow_eligibility(metrics, retention=True)
        self.assertFalse(entry["coreEligible"])
        self.assertTrue(retained["coreEligible"])
        self.assertEqual(retained["status"], "core_retention_eligible")

    def test_14d_win_gate_only_activates_at_five_campaigns(self):
        sparse = evaluate_follow_eligibility(evidence(
            copy_bt_14d_campaign_closed_n=4, copy_bt_14d_campaign_wins=0,
            copy_bt_14d_net_pnl=-100.0,
        ))
        sampled = evaluate_follow_eligibility(evidence(
            copy_bt_14d_campaign_closed_n=5, copy_bt_14d_campaign_wins=2,
            copy_bt_14d_net_pnl=-100.0,
        ))
        self.assertTrue(sparse["coreEligible"])
        self.assertFalse(sampled["coreEligible"])
        self.assertEqual(sampled["status"], "challenger_recent_decline")

    def test_7d_has_no_fixed_win_line_above_return_floor_but_detects_hard_collapse(self):
        sparse_loss = evaluate_follow_eligibility(evidence(
            copy_bt_7d_campaign_closed_n=4, copy_bt_7d_campaign_wins=0,
            copy_bt_7d_net_pnl=400.0,
        ))
        collapse = evaluate_follow_eligibility(evidence(
            copy_bt_7d_campaign_closed_n=5, copy_bt_7d_campaign_wins=1,
            copy_bt_7d_net_pnl=-100.0,
        ))
        self.assertTrue(sparse_loss["coreEligible"])
        self.assertFalse(collapse["eligible"])
        self.assertTrue(collapse["hardRisk"])
        self.assertEqual(collapse["status"], "recent_copy_collapse")

    def test_profit_factor_tail_execution_and_capacity_are_core_soft_gates(self):
        cases = (
            ({"copy_bt_profit_factor": 1.20}, "challenger_profit_structure_watch"),
            ({"copy_bt_campaign_net_after_top2": 299.0}, "challenger_tail_profit_watch"),
            ({"actionable_open_rate": 0.69}, "challenger_execution_watch"),
            ({"capacity_fit": 0.74}, "challenger_capacity_watch"),
        )
        for changes, status in cases:
            with self.subTest(status=status):
                result = evaluate_follow_eligibility(evidence(**changes))
                self.assertTrue(result["eligible"])
                self.assertFalse(result["coreEligible"])
                self.assertEqual(result["status"], status)

    def test_one_isolated_liquidation_is_allowed_but_repeat_is_rejected(self):
        one = evaluate_follow_eligibility(evidence(copy_bt_liquidations=1))
        repeat = evaluate_follow_eligibility(evidence(copy_bt_liquidations=2))
        self.assertTrue(one["coreEligible"])
        self.assertFalse(repeat["eligible"])
        self.assertTrue(repeat["hardRisk"])
        self.assertEqual(repeat["status"], "repeated_copy_liquidation")

    def test_path_risk_12_percent_downgrades_and_15_percent_rejects(self):
        challenger = evaluate_follow_eligibility(evidence(copy_intratrade_max_drawdown=0.13))
        rejected = evaluate_follow_eligibility(evidence(copy_intratrade_max_drawdown=0.151))
        pending = evaluate_follow_eligibility(evidence(copy_path_risk_status="pending"))
        self.assertEqual(challenger["status"], "challenger_intratrade_drawdown")
        self.assertEqual(rejected["status"], "historical_deep_loss_reject")
        self.assertTrue(rejected["hardRisk"])
        self.assertEqual(pending["status"], "challenger_path_risk_pending")

    def test_failed_deep_events_and_low_recovery_are_hard_rejections(self):
        failed = evaluate_follow_eligibility(evidence(copy_failed_deep_bag_n=2))
        low_recovery = evaluate_follow_eligibility(evidence(
            copy_deep_bag_event_n=2, copy_deep_bag_recovery_rate=0.49,
        ))
        self.assertEqual(failed["status"], "historical_deep_loss_reject")
        self.assertEqual(low_recovery["status"], "historical_deep_loss_reject")

    def test_long_recovered_bag_is_challenger_and_live_deep_loss_is_exit_only(self):
        historical = evaluate_follow_eligibility(evidence(
            copy_deep_bag_event_n=1, copy_deep_bag_recovery_rate=1.0,
            copy_max_deep_bag_hours=24.0,
        ))
        current = evaluate_follow_eligibility(evidence(copy_current_open_loss_frac=-0.08))
        slow = evaluate_follow_eligibility(evidence(
            copy_current_open_loss_frac=-0.05, copy_current_bag_hours=24.0,
        ))
        self.assertEqual(historical["status"], "challenger_long_deep_bag")
        self.assertEqual(current["role"], "exit_only")
        self.assertEqual(slow["status"], "current_deep_loss_freeze")

    def test_retired_high_water_fields_do_not_change_qualification(self):
        stage = evaluate_follow_eligibility(evidence(wallet_breaker_stage=2))
        cooldown = evaluate_follow_eligibility(evidence(wallet_cooldown_until_ms=9_999_999_999_999))
        self.assertTrue(stage["coreEligible"])
        self.assertTrue(cooldown["coreEligible"])

    def test_no_allowed_specialty_sector_is_rejected(self):
        result = evaluate_follow_eligibility(evidence(sector_policy_json=json.dumps({
            "allowed": [],
            "crypto": {"allow": False, "status": "recent_loss"},
            "stock": {"allow": False, "status": "grid_dca"},
        })))
        self.assertEqual(result["status"], "no_allowed_sector")

    def test_watch_only_sector_can_be_challenger_but_never_core(self):
        result = evaluate_follow_eligibility(evidence(sector_policy_json=json.dumps({
            "allowed": [],
            "watch": ["crypto"],
            "crypto": {"allow": False, "watch": True, "status": "sector_return_weak"},
        })))

        self.assertTrue(result["eligible"])
        self.assertFalse(result["coreEligible"])
        self.assertEqual(result["role"], "challenger")

    def test_score_scales_with_replay_equity_and_rewards_more_copy_profit(self):
        small, small_detail = compute_follow_score(evidence(
            copy_bt_net_pnl=3000, copy_bt_14d_net_pnl=1500, copy_bt_7d_net_pnl=800,
            initial_margin_equity=10_000,
        ))
        large, large_detail = compute_follow_score(evidence(
            copy_bt_net_pnl=6000, copy_bt_14d_net_pnl=3000, copy_bt_7d_net_pnl=1600,
            initial_margin_equity=20_000,
        ))
        richer, detail = compute_follow_score(evidence(copy_bt_net_pnl=8000))
        self.assertAlmostEqual(small, large)
        self.assertEqual(small_detail["economicReturns"], large_detail["economicReturns"])
        self.assertGreater(richer, small)
        self.assertEqual(detail["evidenceDays"], 10)

    def test_recent_normalized_loss_only_demotes_score_continuously(self):
        baseline, _ = compute_follow_score(evidence())
        degraded, detail = compute_follow_score(evidence(
            copy_recent_return_14d=-0.03, copy_recent_return_7d=-0.04,
        ))
        self.assertLess(degraded, baseline)
        self.assertTrue(any("归一化收益为负" in reason for reason in detail["reasons"]))


if __name__ == "__main__":
    unittest.main()
