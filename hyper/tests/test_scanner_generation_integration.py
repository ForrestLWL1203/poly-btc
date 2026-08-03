import tempfile
import inspect
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hyper import params, storage
from hyper.discovery import scanner


def leaderboard_row(addr="0xaaa"):
    return {
        "ethAddress": addr,
        "accountValue": "100000",
        "windowPerformances": [
            ("day", {"pnl": "100", "roi": "0.001", "vlm": "1000000"}),
            ("week", {"pnl": "300000", "roi": "0.30", "vlm": "30000000"}),
            ("month", {"pnl": "500000", "roi": "0.60", "vlm": "90000000"}),
            ("allTime", {"pnl": "900000", "roi": "0.90", "vlm": "180000000"}),
        ],
}


def portfolio_rows():
    def window(pnl):
        return {
            "vlm": "30000000",
            "pnlHistory": [
                [index * 7 * 86400_000, str(pnl * index / 4)]
                for index in range(5)
            ],
            "accountValueHistory": [
                [index * 7 * 86400_000, "1"]
                for index in range(5)
            ],
        }
    return [
        ["week", window(300000)], ["month", window(500000)], ["allTime", window(900000)],
        ["perpWeek", window(280000)], ["perpMonth", window(450000)], ["perpAllTime", window(800000)],
    ]


def strict_sector_json(net30=1800, n30=20, net14=900, n14=10, net7=600, n7=6):
    def window(net, closed, rate):
        wins = int(round(closed * rate))
        losses = max(0, closed - wins)
        gross_loss = 200.0 if losses else 0.0
        gross_profit = max(1.0, net + gross_loss)
        return {
            "copy_net_pnl": net, "closed_net_pnl": net,
            "unrealized_pnl": 0.0,
            "closed_n": closed, "wins": wins,
            "gross_profit": gross_profit, "gross_loss": gross_loss,
            "profit_factor": gross_profit / gross_loss if gross_loss else 999.0,
            "payoff_ratio": (
                (gross_profit / wins) / (gross_loss / losses)
                if wins and losses and gross_loss else 999.0
            ),
            "opened_n": closed, "target_open_events": closed,
            "open_fill_rate": 1.0, "capacity_open_fit": 1.0,
            "liquidations": 0, "valuation_status": "complete",
            "top1_profit_share": .20, "top3_profit_share": .45,
            "body_after_top3_n": max(1, closed - 3),
            "body_after_top3_wins": max(1, int(round(max(1, closed - 3) * rate))),
            "body_after_top3_win_rate": rate,
            "body_after_top3_net_pnl": net * .35,
            "path_risk_status": "complete", "intratrade_max_drawdown": .05,
            "deep_bag_event_n": 0, "failed_deep_bag_n": 0,
            "deep_bag_recovery_rate": 1.0, "initial_margin_equity": 10_000,
            "window_start_equity": 10_000,
        }
    return json.dumps({
        "crypto": {
            "30": window(net30, n30, .75),
            "14": window(net14, n14, .70),
            "7": window(net7, n7, .80),
        },
    })


def strict_policy_json():
    return json.dumps({
        "allowed": ["crypto"], "crypto": {"allow": True},
    })


def qualifying_source_fields(now_ms=None):
    return {
        "official_perp_status": "passed",
        "official_perp_reason": "perp_prefilter_passed",
        "official_perp_return_30d": .40,
        "official_perp_pnl_30d": 4000,
        "official_perp_pnl_share": .80,
        "source_episode_n_30d": 20,
        "source_episode_n_7d": 6,
        "source_win_rate_30d": .80,
        "source_win_rate_7d": .80,
        "source_net_pnl_30d": 4000,
        "source_net_pnl_7d": 800,
        "source_active_days_30d": 16,
        "source_active_days_7d": 5,
        "source_top3_profit_share": .50,
        "source_body_after_top3_n": 17,
        "source_body_after_top3_win_rate": .76,
        "source_body_after_top3_net_pnl": 900,
        "source_profit_factor_30d": 2.0,
        "source_payoff_ratio_30d": 1.5,
        "pre_strict_activity": {
            "operational": True,
            "reason": "operational_activity",
            "latest7dActive": True,
            "activeWeeks4": 4,
            "maxOpenGapDays28d": 7,
            "weeklyOpenCountsOldestFirst": [2, 2, 2, 2],
        },
        "last_copyable_open_ms": int(now_ms or time.time() * 1000),
    }


def scan_args():
    return SimpleNamespace(
        days=14,
        no_harvest=False,
        full_scan=False,
        order="mon_roi",
        limit=300,
        workers=1,
        max_pages=2,
    )


class ScannerGenerationIntegrationTests(unittest.TestCase):
    def test_margin_equity_snapshot_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            scanner._assert_margin_equity_snapshot(db, 0.9)
            db.execute("UPDATE params SET value='50' WHERE key='MARGIN_EQUITY_PCT'")
            db.commit()
            with self.assertRaisesRegex(RuntimeError, "margin_equity_pct_changed_during_generation"):
                scanner._assert_margin_equity_snapshot(db, 0.9)

    def test_selection_uses_effective_params_not_historical_tune_baseline(self):
        source = inspect.getsource(scanner._build_explicit_selection)

        self.assertNotIn("resolve_tune_baseline", source)
        self.assertNotIn("resolve_add_baseline", source)

    def test_core_formation_never_seals_an_explicitly_invalid_tune_proposal(self):
        base = {
            key: 1.0 for key in (
                *scanner.auto_tune.TUNE_KEYS, *scanner.auto_tune.ADD_TUNE_KEYS,
            )
        }
        invalid = {
            "eligible_to_apply": False,
            "proposal": {**base, "STABLE_MARGIN_PCT": 9.0},
            "validation": {"reasons": ["holdout_not_better"]},
        }

        surface, eligible, reason = scanner._formation_param_surface(base, invalid)

        self.assertFalse(eligible)
        self.assertEqual(surface["STABLE_MARGIN_PCT"], 1.0)
        self.assertEqual(reason, "holdout_not_better")

    def test_no_robust_membership_returns_an_explicit_legal_empty_core(self):
        source = inspect.getsource(scanner.form_quality_prefix)
        failure_branch = source[
            source.index("if robust_winner is None:"):
            source.index("robust_key, chosen, robust_check = robust_winner")
        ]

        self.assertNotIn('raise RuntimeError("no_robust_quality_membership")', failure_branch)
        self.assertIn("chosen_addrs = ()", failure_branch)
        self.assertIn('"explicitEmptyCore": True', failure_branch)

    def test_core_formation_searches_count_coarsely_then_efficient_tunes_the_winner(self):
        source = inspect.getsource(scanner.form_quality_prefix)

        self.assertEqual(source.count("auto_tune.maybe_tune_margins("), 2)
        self.assertIn('search_profile="coarse"', source)
        self.assertIn('search_profile="efficient"', source)
        self.assertIn("addrs_override=list(tune_ordered[:winning_count])", source)
        self.assertIn("search_quality_prefix(", source)
        self.assertIn("except TimeoutError as exc", source)
        self.assertIn("full_tune_timeout_using_coarse", source)
        self.assertIn("full_tune_timeout_using_active", source)
        self.assertIn(
            "_select_formation_finalist_surface(\n                    db, full_run, tune_ranked,",
            source,
        )
        self.assertIn("tuned_candidate_rows = list(prepath_rows)", source)
        self.assertNotIn("tuned_candidate_rows = list(ranked_candidates)", source)
        self.assertNotIn("tuned_candidate_addrs", source)
        self.assertIn("_retune_exact_membership_surface(", source)
        self.assertIn("core_formation_membership_parameter_not_converged", source)
        self.assertEqual(scanner.config.CORE_PREFIX_EXHAUSTIVE_MAX_N, 0)
        self.assertFalse(scanner.config.CORE_FORMATION_ENABLE_LOO)
        self.assertIn(
            'getattr(config, "CORE_FORMATION_ENABLE_LOO", False)',
            source,
        )
        self.assertIn("_load_formation_prefix_evidence(", source)
        self.assertIn("_store_formation_prefix_evidence(", source)

    def test_compact_prefix_evidence_round_trips_without_member_addresses(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            value = scanner.core_formation.PrefixEvaluation(
                count=2, net_pnl=1200.0, stress_net_pnl=300.0,
                max_drawdown=.12, actionable_open_rate=.91, capacity_fit=.94,
                liquidations=1, params={"MID_LEV_CAP": 10.0},
                payload={"return30d": .12, "return7d": .04},
            )
            scanner._store_formation_prefix_evidence(
                db, "g1", "surface1", ["0xaaa", "0xbbb"], value,
                {"return30d": .12, "return7d": .04},
            )
            loaded = scanner._load_formation_prefix_evidence(
                db, "g1", "surface1", ["0xbbb", "0xaaa"],
            )
            raw = db.execute(
                "SELECT evaluation_json,replay_json FROM formation_prefix_evidence"
            ).fetchone()

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[0].count, 2)
        self.assertEqual(loaded[0].net_pnl, 1200.0)
        self.assertEqual(loaded[1]["return7d"], .04)
        self.assertNotIn("0xaaa", raw[0] + raw[1])
        self.assertNotIn("0xbbb", raw[0] + raw[1])

    def test_normal_scan_honors_auto_tune_switch_before_publication(self):
        scan_source = inspect.getsource(scanner.scan)
        optimize_source = inspect.getsource(scanner.optimize_published_generation)
        formation_source = inspect.getsource(scanner.form_quality_prefix)
        publication_source = inspect.getsource(scanner._build_forced_prefix_selection)

        self.assertIn("automatic_retune = _automatic_formation_retune_enabled(db)", scan_source)
        self.assertIn("retune=automatic_retune, force_retune=automatic_retune", scan_source)
        self.assertIn("_assert_automatic_formation_tuned(", scan_source)
        self.assertIn("required=bool(automatic_retune)", scan_source)
        self.assertNotIn('stage="core_membership_retune"', scan_source)
        self.assertIn(
            "automatic_retune and membership_changed and desired_retained",
            scan_source,
        )
        self.assertNotIn("generation_id, stamp, now_ms, retune=False", scan_source)
        self.assertIn("retune_formation=True", optimize_source)
        self.assertIn(
            "_assert_automatic_formation_tuned(",
            inspect.getsource(scanner.repair_published_selection),
        )
        self.assertIn("path_rows=None, path_meta=None", formation_source)
        self.assertNotIn("shared_path = price_path.load_refined", formation_source)
        self.assertEqual(publication_source.count("auto_tune._candidate_windows("), 3)
        self.assertIn("dynamicReturn30d", publication_source)
        self.assertIn("dynamicReturn7d", publication_source)
        self.assertIn("final_strict_copy_failed:", publication_source)

    def test_auto_tune_switch_and_publication_guard(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            self.assertTrue(scanner._automatic_formation_retune_enabled(db))
            db.execute(
                "UPDATE params SET value='false' WHERE key='AUTO_TUNE_MARGIN_ENABLE'"
            )
            db.commit()
            self.assertFalse(scanner._automatic_formation_retune_enabled(db))

        scanner._assert_automatic_formation_tuned(
            {"search": {"tunePoolCount": 0, "formationTuneEligible": False}},
            required=True,
        )
        scanner._assert_automatic_formation_tuned(
            {"search": {
                "tunePoolCount": 7,
                "formationTuneEligible": True,
                "formationTuneReason": "validated_proposal",
            }},
            required=True,
        )
        with self.assertRaisesRegex(RuntimeError, "automatic_core_tune_not_eligible"):
            scanner._assert_automatic_formation_tuned(
                {"search": {
                    "tunePoolCount": 7,
                    "formationTuneEligible": False,
                    "formationTuneReason": "no_validated_finalist",
                }},
                required=True,
            )
        with self.assertRaisesRegex(RuntimeError, "automatic_core_tune_not_executed"):
            scanner._assert_automatic_formation_tuned(
                {"search": {
                    "tunePoolCount": 7,
                    "formationTuneEligible": True,
                    "formationTuneReason": "full_tune_timeout_using_active:timeout",
                }},
                required=True,
            )

    def test_scheduled_formation_never_overwrites_verified_membership_with_old_core(self):
        source = inspect.getsource(scanner.form_quality_prefix)
        finalize_source = inspect.getsource(scanner.finalize_profiled_generation)

        self.assertNotIn("weekly_rebalance_not_due", source)
        self.assertNotIn("chosen_addrs = tuple(stable)", source)
        self.assertIn("retune = bool(retune and (force_retune or rebalance_due))", source)
        self.assertIn("retune=bool(retune), force_retune=bool(retune)", finalize_source)

    def test_core_order_change_is_detected_even_when_fixed_surface_can_publish_it(self):
        formation = {"selected": ("0xbbb", "0xaaa")}

        self.assertTrue(scanner._formation_membership_changed(
            formation, ("0xaaa", "0xbbb"),
        ))
        self.assertFalse(scanner._formation_membership_changed(
            formation, ("0xbbb", "0xaaa"),
        ))

    def test_no_retune_finalize_is_a_hard_fixed_surface_contract(self):
        source = inspect.getsource(scanner.finalize_profiled_generation)

        self.assertNotIn("membership_retune_triggered", source)
        self.assertNotIn("_formation_membership_changed", source)
        self.assertIn("formation, required=bool(retune)", source)
        self.assertIn(
            "retune=bool(retune and membership_changed and desired_retained)",
            source,
        )

    def test_daily_auto_tune_switch_can_publish_a_fixed_surface_promotion(self):
        source = inspect.getsource(scanner.refresh_challengers)

        self.assertIn("automatic_retune = _automatic_formation_retune_enabled(db)", source)
        self.assertIn('elif fixed_decision["mode"] == "promote":', source)
        self.assertIn("if automatic_retune:", source)
        self.assertIn("fixed_surface_promotion = True", source)
        self.assertIn("challenger_daily_promotion_fixed_surface", source)

    def test_missing_portfolio_fill_evidence_publishes_an_explicit_empty_core(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            candidate = {
                "addr": "0xaaa",
                "follow_score": 0.9,
                "follow_qualification": {
                    "eligible": True,
                    "coreEligible": True,
                    "role": "core_eligible",
                    "status": "core_entry_qualified",
                    "checks": {
                        "pathRiskComplete": True, "valuationComplete": True,
                        "sectorExecutable": True, "noRepeatedLiquidation": True,
                        "noForwardLiquidation": True,
                    },
                },
            }
            with patch.object(scanner, "_quality_core_profiles", return_value=[candidate]), \
                    patch.object(scanner, "_effective_follow_replay", return_value={
                        "qualification": {
                            "eligible": True, "coreEligible": True,
                            "role": "core_eligible", "status": "strict_copy_qualified",
                            "deferred": False,
                        },
                        "metrics": {
                            "copy_bt_net_pnl": 1_000,
                            "copy_bt_closed_net_pnl": 1_000,
                            "copy_bt_window_start_equity": 10_000,
                            "copy_bt_7d_net_pnl": 300,
                            "copy_bt_7d_closed_net_pnl": 300,
                            "copy_bt_7d_window_start_equity": 10_000,
                        },
                        "score": .9,
                    }), \
                    patch.object(scanner.auto_tune, "_load_sigmas", return_value={}), \
                    patch.object(scanner.auto_tune, "_load_market_ctx", return_value={}), \
                    patch.object(scanner, "_current_copy_valuation_marks", return_value={}), \
                    patch.object(scanner.selection, "pinned_core_controls", return_value=[]), \
                    patch.object(scanner.selection, "published_core_addrs", return_value=[]), \
                    patch.object(scanner, "_core_rebalance_due", return_value=(True, None)), \
                    patch.object(scanner.auto_tune, "_portfolio_window_fills",
                                 return_value={30: [], 14: [], 7: []}), \
                    patch.object(scanner.auto_tune, "maybe_tune_margins") as tune:
                formation = scanner.form_quality_prefix(
                    db, "g1", "2026-07-22T00:00:00Z", now_ms=1_800_000_000_000,
                )

            self.assertEqual(formation["selected"], ())
            self.assertTrue(formation["search"]["explicitEmptyCore"])
            self.assertEqual(formation["search"]["formationTuneReason"], "no_cached_fills")
            self.assertEqual(formation["search"]["tunePoolCount"], 1)
            self.assertTrue(formation["qualifications"]["0xaaa"]["coreEligible"])
            tune.assert_not_called()

    def test_explicit_empty_core_keeps_profitable_old_core_as_challenger(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            profile = {key: None for key in cols}
            profile.update(
                addr="0xold", status="active", reason="ok", score=.8,
                profile_generation="g2", data_status="valid", evidence_status="qualified",
            )
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [profile.get(key) for key in cols],
            )
            db.commit()
            profiles = [{
                **profile,
                "follow_score": .8,
                "follow_qualification": {
                    "eligible": True, "coreEligible": False,
                    "role": "challenger", "status": "challenger_return_watch",
                },
            }]

            rows, _marginal = scanner._build_forced_prefix_selection(
                db, "g2", "2026-07-07T00:00:00Z", 1,
                profiles=profiles,
                previous_roles={"0xold": scanner.selection.CORE},
                controls={"0xold": True}, held=set(), desired_order=(),
                formation_meta={"explicitEmptyCore": True},
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].role, scanner.selection.CHALLENGER)
            self.assertTrue(rows[0].enabled)
            self.assertEqual(rows[0].reason, "challenger_return_watch")

    def test_recent_former_core_remains_on_recheck_surface_after_empty_generation(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO follow_selection "
                "(generation,addr,role,enabled,selected_at) "
                "VALUES ('gold','0xrecent','core',1,'2026-07-20T00:00:00Z')"
            )
            db.execute(
                "INSERT INTO follow_selection "
                "(generation,addr,role,enabled,selected_at) "
                "VALUES ('gexpired','0xexpired','core',1,'2026-06-15T00:00:00Z')"
            )
            db.commit()

            addrs = scanner._recent_former_core_addrs(
                db, as_of="2026-07-24T00:00:00Z", recheck_days=14,
            )

            self.assertEqual(addrs, ["0xrecent"])

    def test_pre_strict_only_wallet_is_not_exposed_as_challenger(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            profile = {
                "addr": "0xnear", "status": "active", "reason": "ok",
                "profile_generation": "g2", "data_status": "valid",
                "evidence_status": "qualified", "follow_score": .72,
                "follow_qualification": {
                    "eligible": True, "coreEligible": False,
                    "status": "challenger_recent_return_watch",
                    "checks": {
                        "strictCopy30dReturn": True,
                        "strictCopyRolling7dReturn": False,
                    },
                },
            }

            rows, _marginal = scanner._build_forced_prefix_selection(
                db, "g2", "now", 1,
                profiles=[profile], previous_roles={}, controls={"0xnear": True},
                held=set(), desired_order=(),
                formation_meta={"explicitEmptyCore": True},
            )

            self.assertEqual(rows, [])

    def test_retired_core_soft_failure_grace_is_absent(self):
        self.assertFalse(hasattr(scanner, "_apply_core_soft_failure_grace"))
        self.assertNotIn(
            "core_soft_fail_count", storage.DISCOVERY_SCHEMA,
        )

    def test_new_core_has_no_promotion_delay_or_tenure_bypass(self):
        self.assertFalse(hasattr(scanner, "_core_membership_hysteresis"))
        source = inspect.getsource(scanner._quality_first_core_transition)
        self.assertNotIn("promotion_ready", source)
        self.assertNotIn("tenure_protected", source)

    def test_perp_prefilter_never_holds_writer_transaction_during_network_calls(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            transaction_states = []

            def portfolio(_addr):
                transaction_states.append(db.in_transaction)
                return portfolio_rows()

            with patch.object(scanner.rest, "portfolio", side_effect=portfolio):
                results = scanner._run_perp_prefilter(
                    db, ["0xaaa", "0xbbb", "0xccc"], scan_args(), "scan-lock-test",
                )

            self.assertEqual(transaction_states, [False, False, False])
            self.assertTrue(all(result.passed for result in results.values()))
            self.assertFalse(db.in_transaction)

    def test_formation_entry_requires_recent_return_before_shared_replay(self):
        effective = {
            **qualifying_source_fields(),
            "actionable_open_rate": .9,
            "capacity_fit": 1.0,
            "data_status": "valid",
            "evidence_status": "qualified",
            "sector_policy_json": strict_policy_json(),
            # Both dynamic-return gates fail (9%/1%) while source quality,
            # execution, valuation and path evidence remain sound.
            "sector_copy_json": strict_sector_json(900, 7, 400, 6, 100, 5),
            "forward_liquidations": 0,
        }

        result = scanner._formation_entry_eligibility(effective, .80)

        self.assertFalse(result["eligible"])
        self.assertFalse(result["individualCoreEligible"])
        self.assertFalse(result["checks"]["strictCopy30dReturn"])
        self.assertFalse(result["checks"]["strictCopyRolling7dReturn"])

    def test_formation_entry_uses_recent_return_but_not_score_as_a_hard_gate(self):
        effective = {
            **qualifying_source_fields(),
            "actionable_open_rate": .9,
            "capacity_fit": 1.0,
            "data_status": "valid",
            "evidence_status": "qualified",
            "sector_policy_json": strict_policy_json(),
            "sector_copy_json": strict_sector_json(1800, 8, 600, 6, 300, 5),
            "forward_liquidations": 0,
        }

        low_score = scanner._formation_entry_eligibility(effective, .10)
        weak_recent = scanner._formation_entry_eligibility({
            **effective,
            "sector_copy_json": strict_sector_json(1800, 8, 600, 6, -1, 5),
        }, .80)

        self.assertTrue(low_score["eligible"])
        self.assertNotIn("scoreAtLeastCoreFloor", low_score["checks"])
        self.assertFalse(weak_recent["eligible"])
        self.assertFalse(weak_recent["checks"]["strictCopyRolling7dReturn"])

    def test_manual_optimize_requalifies_incumbents_as_new_entries(self):
        source = inspect.getsource(scanner.optimize_published_generation)
        forced_source = inspect.getsource(scanner._build_forced_prefix_selection)

        self.assertIn("force_entry_requalification=True", source)
        self.assertIn("_rerank_cached_pre_strict_queue(", source)
        self.assertIn("generation_asof_ms", source)
        self.assertNotIn("force_promotion", forced_source)
        self.assertNotIn("core_retention_eligible", forced_source)

    def test_cached_pre_strict_rerank_upgrades_score_and_queue_without_replay(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            cols = storage.PROFILE_COLS.split(",")
            activity = qualifying_source_fields()["pre_strict_activity"]
            profiles = [
                {
                    "addr": "0xprofit",
                    "status": "active",
                    "score": .10,
                    "rough_copy_score": .10,
                    "profile_generation": "g-rerank",
                    "copy_bt_closed_n": 12,
                    "copy_bt_net_pnl": 5000,
                    "copy_bt_closed_net_pnl": 5000,
                    "copy_bt_window_start_equity": 10_000,
                    "copy_bt_7d_net_pnl": 1000,
                    "copy_bt_7d_closed_net_pnl": 1000,
                    "copy_bt_7d_window_start_equity": 15_000,
                    "copy_bt_profit_factor": 2.0,
                    "copy_bt_open_fill_rate": .90,
                    "copy_bt_top3_profit_share": .30,
                    "copy_bt_body_after_top3_n": 9,
                    "copy_bt_body_after_top3_net_pnl": 1200,
                },
                {
                    "addr": "0xquality",
                    "status": "active",
                    "score": .90,
                    "rough_copy_score": .90,
                    "profile_generation": "g-rerank",
                    "copy_bt_closed_n": 20,
                    "copy_bt_net_pnl": 1000,
                    "copy_bt_closed_net_pnl": 1000,
                    "copy_bt_window_start_equity": 10_000,
                    "copy_bt_7d_net_pnl": 400,
                    "copy_bt_7d_closed_net_pnl": 400,
                    "copy_bt_7d_window_start_equity": 11_000,
                    "copy_bt_profit_factor": 3.0,
                    "copy_bt_open_fill_rate": 1.0,
                    "copy_bt_top3_profit_share": .20,
                    "copy_bt_body_after_top3_n": 17,
                    "copy_bt_body_after_top3_net_pnl": 600,
                },
            ]
            for rank, profile in enumerate(profiles, 1):
                db.execute(
                    f"INSERT INTO profile ({storage.PROFILE_COLS}) "
                    f"VALUES ({','.join('?' for _ in cols)})",
                    [profile.get(column) for column in cols],
                )
                db.execute(
                    "INSERT INTO pre_strict_evidence "
                    "(generation,addr,policy_version,model_version,status,activity_json,"
                    "tier,queue_rank,rough_profit_priority,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        "g-rerank", profile["addr"],
                        scanner.pre_strict.POLICY_VERSION, "legacy-score-v4",
                        "passed", json.dumps(activity), "primary", rank, .01, "now",
                    ),
                )
            db.commit()

            result = scanner._rerank_cached_pre_strict_queue(
                db, "g-rerank", now_ms=2_000_000_000_000,
            )
            ranked = db.execute(
                "SELECT addr,queue_rank FROM pre_strict_evidence "
                "WHERE generation=? ORDER BY queue_rank",
                ("g-rerank",),
            ).fetchall()
            model_versions = {
                row[0] for row in db.execute(
                    "SELECT DISTINCT model_version FROM pre_strict_evidence "
                    "WHERE generation=?",
                    ("g-rerank",),
                ).fetchall()
            }

        self.assertEqual(result["scored"], 2)
        self.assertEqual(result["queued"], 2)
        self.assertEqual(ranked[0][0], "0xprofit")
        self.assertEqual(
            model_versions, {scanner.pre_strict.SELECTION_MODEL_VERSION},
        )

    def test_formation_runs_one_path_bridge_and_one_final_surface_replay(self):
        source = inspect.getsource(scanner.form_quality_prefix)

        self.assertNotIn("_rank_formation_candidates_for_surface", source)
        self.assertEqual(source.count("_parallel_effective_follow_replays("), 2)
        self.assertIn("tune_ranked = ranked_candidates[:core_upper]", source)
        self.assertIn("tuned_candidate_rows = list(prepath_rows)", source)
        self.assertIn('"finalSurfaceUniverseCount": len(tuned_candidate_rows)', source)
        self.assertEqual(source.count("follow_score.follow_score_sort_key("), 2)

    def test_cached_strict_formation_releases_writer_lock_between_wallets(self):
        source = inspect.getsource(scanner.form_quality_prefix)
        update_sql = (
            '"UPDATE pre_strict_evidence SET strict_status=?,strict_first_failure=? "'
        )
        updates = [
            index for index in range(len(source))
            if source.startswith(update_sql, index)
        ]

        self.assertEqual(len(updates), 2)
        first_tail = source[updates[0]:source.index("if hard_invalid:", updates[0])]
        second_tail = source[updates[1]:source.index("        return (", updates[1])]
        self.assertIn("db.commit()", first_tail)
        self.assertIn("db.commit()", second_tail)

    def test_auto_tune_releases_seed_write_before_expensive_grid(self):
        source = inspect.getsource(scanner.auto_tune.maybe_tune_margins)
        seed = source.index("params.seed_params(db)")
        fill_load = source.index("window_fills = _portfolio_window_fills(")
        between = source[seed:fill_load]

        self.assertIn("db.commit()", between)
        self.assertLess(
            between.index("db.commit()"),
            between.index("window_fills") if "window_fills" in between else len(between),
        )

    def test_pre_strict_queue_uses_the_same_score_before_legacy_tiers(self):
        source = inspect.getsource(scanner._finalize_pre_strict_queue)

        score_order = source.index("COALESCE(p.rough_copy_score,p.score,0) DESC")
        tier_order = source.index("CASE pse.tier WHEN 'primary'")
        profit_order = source.index("pse.rough_profit_priority DESC")
        self.assertLess(score_order, tier_order)
        self.assertLess(tier_order, profit_order)

    def test_final_membership_parameter_closure_is_bounded_and_fail_closed(self):
        source = inspect.getsource(scanner.form_quality_prefix)
        helper = inspect.getsource(scanner._retune_exact_membership_surface)

        self.assertEqual(scanner.config.AUTO_TUNE_LEVERAGE_SHORTLIST, 3)
        self.assertGreaterEqual(scanner.config.AUTO_TUNE_SIZING_FINALISTS, 12)
        self.assertIn("CORE_FORMATION_CLOSURE_MAX_ROUNDS", source)
        self.assertIn("for round_index in range(1, max_rounds + 1)", source)
        self.assertIn("actual == closure_expected", source)
        self.assertIn(
            "core_formation_membership_parameter_not_converged", source,
        )
        self.assertIn('addrs_override=list(ordered_addrs)', helper)
        self.assertIn('search_profile="efficient"', helper)
        self.assertIn("_select_formation_finalist_surface(", helper)

    def test_exact_membership_closure_full_tunes_only_the_actual_core(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            base = {
                key: 1.0 for key in (
                    *scanner.auto_tune.TUNE_KEYS,
                    *scanner.auto_tune.ADD_TUNE_KEYS,
                )
            }
            proposal = {**base, "STABLE_MARGIN_PCT": 0.031}
            tune_result = {
                "status": "ok",
                "eligible_to_apply": True,
                "proposal": proposal,
            }
            with patch.object(
                scanner.auto_tune, "_portfolio_window_fills",
                return_value={30: [{"addr": "0xaaa"}], 14: [], 7: []},
            ), patch.object(
                scanner.auto_tune, "maybe_tune_margins",
                return_value=tune_result,
            ) as tune, patch.object(
                scanner, "_select_formation_finalist_surface",
                return_value=(proposal, [{"feasible": True}]),
            ):
                result = scanner._retune_exact_membership_surface(
                    db, ("0xAAA", "0xBBB"),
                    [{"addr": "0xaaa"}, {"addr": "0xbbb"}, {"addr": "0xccc"}],
                    generation_id="g1", stamp="s1", round_index=1,
                    now_ms=1_800_000_000_000, base_follow=base,
                    valuation_marks={}, sigmas={}, market_ctx={},
                )

            self.assertEqual(result["addrs"], ("0xaaa", "0xbbb"))
            self.assertEqual(result["params"]["STABLE_MARGIN_PCT"], 0.031)
            self.assertTrue(result["eligible"])
            kwargs = tune.call_args.kwargs
            self.assertEqual(kwargs["addrs_override"], ["0xaaa", "0xbbb"])
            self.assertEqual(kwargs["search_profile"], "efficient")
            self.assertTrue(kwargs["formation_admission"])

    def test_effective_replay_keeps_one_sector_scoped_surface(self):
        source = inspect.getsource(scanner._effective_follow_replay)

        self.assertIn(
            "apply_allowed_sector_copy_metrics({**row, **effective})",
            source,
        )
        self.assertNotIn('"sector_copy_json": None', source)
        self.assertIn("**scoring_metrics", source)

    def test_finalist_surface_optimizes_the_individually_qualified_portfolio(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            base = params.load_follow(db)
            winner = {
                key: base[key]
                for key in (*scanner.auto_tune.TUNE_KEYS, *scanner.auto_tune.ADD_TUNE_KEYS)
            }
            finalist = {
                **winner,
                "STABLE_MARGIN_PCT": float(winner["STABLE_MARGIN_PCT"]) + 0.01,
            }
            rows = [{"addr": "0xaaa"}, {"addr": "0xbbb"}]

            def replay(_db, row, _now, **kwargs):
                is_finalist = (
                    float(kwargs["follow"]["STABLE_MARGIN_PCT"])
                    == float(finalist["STABLE_MARGIN_PCT"])
                )
                checks = {key: True for key in scanner._FORMATION_PREPATH_CHECKS}
                if row["addr"] == "0xbbb" and not is_finalist:
                    checks["copy30dReturn"] = False
                passed = all(checks.values())
                return {
                    "qualification": {
                        "checks": checks,
                        "status": "strict_copy_qualified" if passed else
                                  "strict_copy_30d_return_below_floor",
                        "firstFailure": None if passed else
                                        "strict_copy_30d_return_below_floor",
                    },
                    "metrics": {"copy_bt_net_pnl": 1_000 if passed else 100},
                }

            def windows(_db, addrs, _sigmas, overrides, _now, **_kwargs):
                better = (
                    float(overrides["STABLE_MARGIN_PCT"])
                    == float(finalist["STABLE_MARGIN_PCT"])
                )
                return {
                    30: {
                        "copy_net_pnl": 3_000 if better else 1_500,
                        "window_start_equity": 10_000,
                        "price_path_coverage": 1.0,
                        "maintenance_margin_coverage": 1.0,
                    },
                    7: {
                        "copy_net_pnl": 700 if better else 500,
                        "window_start_equity": 10_000,
                    },
                }

            tune = {
                "params": winner,
                "proposal": winner,
                "finalists": [
                    {"eligible": True, "params": winner, "challengerLiquidations": 0},
                    {"eligible": True, "params": finalist, "challengerLiquidations": 0},
                ],
            }
            metrics = SimpleNamespace(actionable_open_rate=.9, capacity_fit=1.0)
            with patch.object(scanner, "_effective_follow_replay", side_effect=replay), \
                    patch.object(
                        scanner.price_path, "load_refined",
                        return_value=[{"coin": "BTC", "time": 1}],
                    ), \
                    patch.object(
                        scanner.price_path, "coverage",
                        return_value={"coverage": 1.0},
                    ), \
                    patch.object(
                        scanner, "prepare_price_path", return_value=["strict-path"],
                    ), \
                    patch.object(
                        scanner.auto_tune, "_filter_window_fills_by_addr",
                        return_value={30: [{"coin": "BTC", "time": 1}], 7: []},
                    ), \
                    patch.object(
                        scanner.auto_tune, "_candidate_windows", side_effect=windows,
                    ) as window_mock, \
                    patch.object(
                        scanner, "_portfolio_selection_metrics", return_value=metrics,
                    ):
                chosen, audit = scanner._select_formation_finalist_surface(
                    db, tune, rows, base_follow=base, generation_id="g1",
                    now_ms=1, valuation_marks={}, sigmas={}, market_ctx={},
                    window_fills={30: [{"coin": "BTC", "time": 1}], 7: []},
                )

            self.assertEqual(
                chosen["STABLE_MARGIN_PCT"], finalist["STABLE_MARGIN_PCT"],
            )
            self.assertEqual(
                sorted(item["qualifiedCount"] for item in audit), [1, 2],
            )
            self.assertTrue(all(
                call.kwargs["path_rows"] == ["strict-path"]
                for call in window_mock.call_args_list
            ))

    def test_finalist_surface_rejects_fills_only_winner_with_negative_strict_recent_path(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            base = params.load_follow(db)
            baseline = {
                key: base[key]
                for key in (*scanner.auto_tune.TUNE_KEYS, *scanner.auto_tune.ADD_TUNE_KEYS)
            }
            aggressive = {**baseline, "STABLE_MARGIN_PCT": baseline["STABLE_MARGIN_PCT"] + .01}
            rows = [{"addr": "0xaaa"}]

            def replay(_db, _row, _now, **_kwargs):
                return {
                    "qualification": {
                        "checks": {key: True for key in scanner._FORMATION_PREPATH_CHECKS},
                        "status": "strict_copy_qualified",
                    },
                    "metrics": {"copy_bt_net_pnl": 2_000},
                }

            def windows(_db, _addrs, _sigmas, overrides, _now, **kwargs):
                self.assertEqual(kwargs["path_rows"], ["strict-path"])
                is_aggressive = (
                    float(overrides["STABLE_MARGIN_PCT"])
                    == float(aggressive["STABLE_MARGIN_PCT"])
                )
                return {
                    30: {
                        "copy_net_pnl": 4_000 if is_aggressive else 2_000,
                        "window_start_equity": 10_000,
                        "price_path_coverage": 1.0,
                        "maintenance_margin_coverage": 1.0,
                    },
                    7: {
                        "copy_net_pnl": -100 if is_aggressive else 500,
                        "window_start_equity": 10_000,
                    },
                }

            tune = {
                "params": aggressive,
                "proposal": aggressive,
                "baseline_proposal": baseline,
                "validation": {
                    "challengerLiquidations": 0,
                    "baselineLiquidations": 0,
                },
                "finalists": [
                    {"eligible": True, "params": aggressive, "challengerLiquidations": 0},
                ],
            }
            metrics = SimpleNamespace(actionable_open_rate=.9, capacity_fit=1.0)
            with patch.object(scanner, "_effective_follow_replay", side_effect=replay), \
                    patch.object(
                        scanner.price_path, "load_refined",
                        return_value=[{"coin": "BTC", "time": 1}],
                    ), \
                    patch.object(
                        scanner.price_path, "coverage",
                        return_value={"coverage": 1.0},
                    ), \
                    patch.object(scanner, "prepare_price_path", return_value=["strict-path"]), \
                    patch.object(
                        scanner.auto_tune, "_filter_window_fills_by_addr",
                        return_value={30: [{"coin": "BTC", "time": 1}], 7: []},
                    ), \
                    patch.object(scanner.auto_tune, "_candidate_windows", side_effect=windows), \
                    patch.object(
                        scanner, "_portfolio_selection_metrics", return_value=metrics,
                    ):
                chosen, audit = scanner._select_formation_finalist_surface(
                    db, tune, rows, base_follow=base, generation_id="g1",
                    now_ms=1, valuation_marks={}, sigmas={}, market_ctx={},
                    window_fills={30: [{"coin": "BTC", "time": 1}], 7: []},
                )

            self.assertEqual(chosen, baseline)
            self.assertEqual(
                {item["source"]: item["feasible"] for item in audit},
                {"tuner_winner": False, "active_baseline": True},
            )

    def test_final_surface_quarantines_one_bad_candidate_without_aborting_generation(self):
        source = inspect.getsource(scanner.form_quality_prefix)

        self.assertNotIn('raise RuntimeError(f"effective_copy_replay_invalid:', source)
        self.assertNotIn("pinned_core_replay_invalid", source)
        self.assertIn("if replay_invalid:", source)
        self.assertIn("rejected.append(addr)", source)


    def test_path_validation_is_portfolio_fail_closed_not_wallet_regate(self):
        source = inspect.getsource(scanner._build_forced_prefix_selection)

        self.assertIn('failures.append("path_coverage")', source)
        self.assertIn('"finalStrictCopy": final_strict_validation', source)
        self.assertNotIn("path_rejected", source)

    def test_scan_runs_explicit_selection_only_once(self):
        source = inspect.getsource(scanner.scan)

        self.assertEqual(source.count("_build_explicit_selection("), 1)
        self.assertIn("_selection_prefetch_candidates(", source)
        self.assertNotIn("preview_rows", source)

    def test_selection_prefetch_candidates_is_bounded_ranked_and_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.executemany(
                "INSERT INTO profile(addr,status,score) VALUES (?,?,?)",
                [
                    ("0xlow", "active", .7),
                    ("0xhigh", "qualified", .9),
                    ("0xdisabled", "active", .95),
                    ("0xrejected", "rejected", .99),
                ],
            )
            db.executemany(
                "INSERT INTO watchlist(rank,addr,score,updated_at) VALUES (?,?,?,'now')",
                [
                    (1, "0xdisabled", .95), (2, "0xhigh", .9),
                    (3, "0xlow", .7), (4, "0xrejected", .99),
                ],
            )
            db.execute(
                "INSERT INTO target_controls(addr,enabled) VALUES('0xdisabled',0)"
            )
            db.commit()

            candidates = scanner._selection_prefetch_candidates(db, limit=2)

        self.assertEqual(candidates, ["0xhigh", "0xlow"])

    def test_bounded_path_pool_is_profit_ordered_without_incumbent_bypass(self):
        rows = [
            {
                "addr": "0xweak", "follow_score": .99,
                "follow_qualification": {
                    "eligible": True, "coreEligible": False, "role": "challenger",
                },
                "sector_policy_json": "{}",
            },
            {
                "addr": "0xready", "follow_score": .80,
                "follow_qualification": {
                    "eligible": True, "coreEligible": True, "role": "core_eligible",
                },
                "sector_policy_json": "{}",
            },
            {
                "addr": "0xincumbent", "follow_score": .10,
                "follow_qualification": {
                    "eligible": False, "coreEligible": False, "role": "rejected",
                },
                "sector_policy_json": "{}",
            },
        ]

        selected = scanner._bounded_formation_candidates(rows, 16)

        self.assertEqual([row["addr"] for row in selected], ["0xready"])

    def test_bounded_path_pool_places_higher_profit_ahead_of_higher_score(self):
        rows = [
            {
                "addr": "0xquality", "follow_score": .99,
                "copy_bt_net_pnl": 1_200, "copy_bt_window_start_equity": 10_000,
                "copy_bt_7d_net_pnl": 300,
                "copy_bt_7d_window_start_equity": 10_000,
                "follow_qualification": {
                    "eligible": True, "coreEligible": True,
                    "role": "core_eligible", "deferred": False,
                },
            },
            {
                "addr": "0xprofit", "follow_score": .70,
                "copy_bt_net_pnl": 3_000, "copy_bt_window_start_equity": 10_000,
                "copy_bt_7d_net_pnl": 800,
                "copy_bt_7d_window_start_equity": 10_000,
                "follow_qualification": {
                    "eligible": True, "coreEligible": True,
                    "role": "core_eligible", "deferred": False,
                },
            },
        ]

        selected = scanner._bounded_formation_candidates(rows, 16)

        self.assertEqual([row["addr"] for row in selected], ["0xprofit", "0xquality"])

    def test_bounded_path_pool_caps_at_sixteen_without_rank_seventeen_backfill(self):
        rows = [
            {
                "addr": f"0x{index:02x}",
                "follow_score": 1.0 - index / 100,
                "follow_qualification": {
                    "eligible": True,
                    "coreEligible": True,
                    "role": "core_eligible",
                    "deferred": False,
                },
            }
            for index in range(20)
        ]

        selected = scanner._bounded_formation_candidates(rows, 16)

        self.assertEqual([row["addr"] for row in selected], [
            f"0x{index:02x}" for index in range(16)
        ])
        self.assertNotIn("0x10", [row["addr"] for row in selected])

    def test_source_quality_pool_keeps_every_structural_survivor_without_top40(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.executemany(
                "INSERT INTO profile "
                "(addr,status,reason,source_quality_score,profile_generation,data_status,"
                "official_perp_status) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        f"0x{index:02x}", "active", "source_structure_passed",
                        100.0 - index, "g-source", "valid", "passed",
                    )
                    for index in range(42)
                ],
            )
            db.commit()

            kept, tail = scanner._source_quality_pool(db, "g-source")

        self.assertEqual(kept, [f"0x{index:02x}" for index in range(42)])
        self.assertEqual(tail, [])

    def test_legacy_five_to_eight_pct_event_is_audit_only(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.executemany(
                "INSERT INTO profile "
                "(addr,status,reason,source_quality_score,profile_generation,data_status,"
                "official_perp_status) VALUES(?,?,?,?,?,?,?)",
                [
                    ("0xsafe", "active", "source_structure_passed", 90, "g-new", "valid", "passed"),
                    ("0xblown", "active", "source_structure_passed", 99, "g-new", "valid", "passed"),
                ],
            )
            scanner._record_wallet_risk_event(
                db, "0xblown", "copy_single_liquidation_loss_over_5pct",
                "SKHX:123", occurred_at=123, coin="SKHX",
                loss_usd=527.92, loss_pct=.052,
            )
            db.commit()

            kept, tail = scanner._source_quality_pool(db, "g-new")
            blocked = db.execute(
                "SELECT status,reason FROM profile WHERE addr='0xblown'"
            ).fetchone()

        self.assertEqual(kept, ["0xblown", "0xsafe"])
        self.assertEqual(tail, [])
        self.assertEqual(blocked, ("active", "source_structure_passed"))

    def test_eight_pct_event_remains_permanent_major_risk(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO profile "
                "(addr,status,reason,source_quality_score,profile_generation,data_status,"
                "official_perp_status) VALUES(?,?,?,?,?,?,?)",
                ("0xblown", "active", "source_structure_passed", 99, "g-new",
                 "valid", "passed"),
            )
            scanner._record_wallet_risk_event(
                db, "0xblown", "copy_single_liquidation_loss_over_8pct",
                "SKHX:123", occurred_at=123, coin="SKHX",
                loss_usd=801, loss_pct=.08,
            )
            db.commit()
            kept, _tail = scanner._source_quality_pool(db, "g-new")
            blocked = db.execute(
                "SELECT status,reason FROM profile WHERE addr='0xblown'"
            ).fetchone()
        self.assertEqual(kept, [])
        self.assertEqual(blocked, ("rejected", "historical_major_liquidation"))

    def test_prepath_candidate_uses_frozen_rough_copy_contract_only(self):
        row = {
            "follow_qualification": {
                "eligible": True, "coreEligible": True, "role": "core_eligible",
                "deferred": False,
            },
            "sector_policy_json": "{}",
        }

        self.assertTrue(scanner._formation_prepath_candidate(row))

    def test_selection_path_prefetch_excludes_disabled_and_watch_only_sectors(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO profile(addr,status,sector_policy_json) VALUES (?,?,?)",
                (
                    "0xsector", "active",
                    json.dumps({
                        "allowed": ["crypto"], "watch": ["stock"],
                        "crypto": {"allow": True},
                        "stock": {"allow": False, "watch": True},
                    }),
                ),
            )
            db.commit()
            fills = [
                {"user": "0xsector", "coin": "BTC", "time": 1},
                {"user": "0xsector", "coin": "xyz:ZM", "time": 2},
            ]
            with patch.object(scanner, "load_copyable_fills", return_value=fills), \
                    patch.object(scanner.params, "load_follow", return_value={}), \
                    patch.object(scanner.auto_tune, "_load_sigmas", return_value={"BTC": .05}), \
                    patch.object(scanner.auto_tune, "_load_market_ctx", return_value={
                        "BTC": {"max_leverage": 20},
                    }), patch.object(
                        scanner.auto_tune, "prepare_refined_price_path",
                        return_value=([{"coin": "BTC"}], {"coverage": 1.0}),
                    ) as prepare:
                result = scanner._prefetch_selection_paths(
                    db, ["0xsector"], 40 * 86_400_000, "g1",
                )

        self.assertEqual(
            [row["coin"] for row in prepare.call_args.args[1]],
            ["BTC"],
        )
        self.assertEqual(result["fills"], 1)
        self.assertEqual(result["coverage"], 1.0)

    def test_selection_path_prefetch_retries_only_missing_markets_before_strict(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO profile(addr,status,sector_policy_json) VALUES (?,?,?)",
                (
                    "0xretry", "active",
                    json.dumps({"allowed": ["crypto"], "crypto": {"allow": True}}),
                ),
            )
            db.commit()
            fills = [
                {"user": "0xretry", "coin": "BTC", "time": 1},
                {"user": "0xretry", "coin": "ZRO", "time": 2},
            ]
            prepared = [
                ([{"coin": "BTC"}], {
                    "coverage": .5, "missingCoins": ["ZRO"],
                }),
                ([{"coin": "BTC"}, {"coin": "ZRO"}], {
                    "coverage": 1.0, "missingCoins": [],
                }),
            ]
            with patch.object(scanner, "load_copyable_fills", return_value=fills), \
                    patch.object(scanner.params, "load_follow", return_value={}), \
                    patch.object(scanner.auto_tune, "_load_sigmas", return_value={
                        "BTC": .05, "ZRO": .08,
                    }), patch.object(scanner.auto_tune, "_load_market_ctx", return_value={
                        "BTC": {"max_leverage": 20}, "ZRO": {"max_leverage": 10},
                    }), patch.object(
                        scanner.auto_tune, "prepare_refined_price_path",
                        side_effect=prepared,
                    ), patch.object(scanner.price_path, "ensure") as ensure, \
                    patch.object(
                        scanner.price_path, "coverage",
                        return_value={"coverage": 1.0, "missingCoins": []},
                    ), patch.object(scanner.time, "sleep") as sleep:
                result = scanner._prefetch_selection_paths(
                    db, ["0xretry"], 40 * 86_400_000, "g1",
                )

        self.assertEqual(result["pathRetryAttempts"], 1)
        self.assertEqual(result["missingCoins"], 0)
        self.assertEqual(
            [row["coin"] for row in ensure.call_args.args[1]], ["ZRO"],
        )
        self.assertTrue(ensure.call_args.kwargs["force_retry"])
        sleep.assert_called_once_with(10.0)

    def test_selection_path_prefetch_exhausts_five_low_frequency_retries(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO profile(addr,status,sector_policy_json) VALUES (?,?,?)",
                (
                    "0xmissing", "active",
                    json.dumps({"allowed": ["crypto"], "crypto": {"allow": True}}),
                ),
            )
            db.commit()
            fills = [{"user": "0xmissing", "coin": "ZRO", "time": 1}]
            incomplete = {
                "coverage": 0.0, "missingCoins": ["ZRO"],
            }
            with patch.object(scanner, "load_copyable_fills", return_value=fills), \
                    patch.object(scanner.params, "load_follow", return_value={}), \
                    patch.object(scanner.auto_tune, "_load_sigmas", return_value={
                        "ZRO": .08,
                    }), patch.object(scanner.auto_tune, "_load_market_ctx", return_value={
                        "ZRO": {"max_leverage": 10},
                    }), patch.object(
                        scanner.auto_tune, "prepare_refined_price_path",
                        side_effect=[
                            ([], incomplete), ([], incomplete),
                        ],
                    ), patch.object(scanner.price_path, "ensure") as ensure, \
                    patch.object(
                        scanner.price_path, "coverage", return_value=incomplete,
                    ), patch.object(scanner.time, "sleep") as sleep:
                result = scanner._prefetch_selection_paths(
                    db, ["0xmissing"], 40 * 86_400_000, "g1",
                )

        self.assertEqual(result["pathRetryAttempts"], 5)
        self.assertEqual(result["missingCoins"], 1)
        self.assertEqual(ensure.call_count, 5)
        self.assertEqual(sleep.call_count, 5)
        self.assertTrue(all(
            call.args == (10.0,) for call in sleep.call_args_list
        ))

    def test_path_prefetch_and_formation_share_bounded_candidate_pool(self):
        formation_source = inspect.getsource(scanner.form_quality_prefix)
        prefetch_source = inspect.getsource(scanner._selection_prefetch_candidates)

        self.assertIn("_bounded_formation_candidates(", formation_source)
        self.assertIn("_bounded_formation_candidates(", prefetch_source)

    def test_scan_does_not_publish_after_selection_path_prefetch_failure(self):
        source = inspect.getsource(scanner.scan)

        self.assertIn("selection_price_path_prefetch_failed:", source)
        self.assertIn("if path_prefetch_error is not None:", source)

    def test_quality_prefix_uses_allowed_sector_copy_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO profile "
                "(addr,status,reason,score,profile_generation,data_status,evidence_status,"
                "copy_bt_net_pnl,copy_bt_closed_n,copy_bt_14d_net_pnl,copy_bt_14d_closed_n,"
                "copy_bt_7d_net_pnl,copy_bt_7d_closed_n,copy_evidence_days,"
                "actionable_open_rate,capacity_fit,sector_policy_json,sector_copy_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "0xsector", "active", "ok", .9, "g-sector", "valid", "qualified",
                    2_000, 30, 1_000, 15, 500, 8, 12, .9, .9,
                    json.dumps({"crypto": {"allow": True}, "stock": {"allow": False},
                                "allowed": ["crypto"]}),
                    json.dumps({
                        "crypto": {
                            "30": {"copy_net_pnl": 100, "closed_n": 10},
                            "14": {"copy_net_pnl": 50, "closed_n": 5},
                            "7": {"copy_net_pnl": 20, "closed_n": 5},
                        },
                        "stock": {
                            "30": {"copy_net_pnl": 1_900, "closed_n": 20},
                            "14": {"copy_net_pnl": 950, "closed_n": 10},
                            "7": {"copy_net_pnl": 480, "closed_n": 5},
                        },
                    }),
                ),
            )
            db.commit()

            ranked = scanner._quality_core_profiles(db, "g-sector")

        self.assertEqual(ranked, [])

    def test_quality_prefix_consumes_frozen_rough_pass_and_score(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            profile = {
                "addr": "0xfrozen",
                "status": "active",
                "reason": "rough_copy_qualified",
                "score": .20,
                "rough_copy_score": .91,
                "profile_generation": "g-frozen",
                "data_status": "valid",
                "evidence_status": "qualified",
                "official_perp_status": "passed",
                "official_perp_reason": "perp_prefilter_passed",
                "official_perp_return_30d": .40,
                "source_episode_n_30d": 12,
                "source_win_rate_30d": .80,
                "source_top3_profit_share": .50,
                "last_copyable_open_ms": 2_000_000_000_000 - 3_600_000,
                "copy_bt_net_pnl": 500,
                "copy_bt_window_start_equity": 10_000,
                "copy_bt_7d_net_pnl": 100,
                "copy_bt_7d_window_start_equity": 10_500,
                "copy_bt_closed_n": 12,
                "copy_bt_win_rate": .75,
                "copy_bt_open_fill_rate": .90,
                "actionable_open_rate": .90,
                "copy_bt_valuation_status": "complete",
                "sector_policy_json": json.dumps({
                    "allowed": ["crypto"],
                    "crypto": {"allow": True},
                }),
            }
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) "
                f"VALUES ({','.join('?' for _ in cols)})",
                [profile.get(column) for column in cols],
            )
            db.execute(
                "INSERT INTO pre_strict_evidence "
                "(generation,addr,policy_version,model_version,status,activity_json,"
                "tier,queue_rank,rough_profit_priority,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "g-frozen", "0xfrozen",
                    scanner.pre_strict.POLICY_VERSION,
                    scanner.pre_strict.SELECTION_MODEL_VERSION,
                    "passed",
                    json.dumps(qualifying_source_fields()[
                        "pre_strict_activity"
                    ]),
                    "reserve", 1, .038, "now",
                ),
            )
            db.commit()

            ranked = scanner._quality_core_profiles(
                db, "g-frozen", core_only=False, now_ms=2_000_000_000_000,
            )

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["addr"], "0xfrozen")
        self.assertEqual(ranked[0]["follow_score"], .91)
        self.assertTrue(ranked[0]["follow_qualification"]["coreEligible"])
        self.assertEqual(
            ranked[0]["follow_qualification"]["checks"],
            {"frozenRoughCopyPassed": True},
        )

    def open_db(self, td):
        return storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)

    def seal_market(self, db, generation):
        scanner.generation_market.Resolver(db, generation, 1, set(), {})
        return scanner.generation_market.seal(db, generation)

    def test_profiled_generation_coverage_counts_audited_deferred_outcomes(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.executemany(
                "INSERT INTO profile(addr,status,profile_generation,data_status) VALUES(?,?,?,?)",
                [
                    ("0xvalid", "rejected", "g1", "valid"),
                    ("0xdeferred", "quarantine", "old-g", "deferred_data_error"),
                ],
            )
            for addr, status, reason in (
                ("0xvalid", "rejected", "economically_disqualified"),
                ("0xdeferred", "quarantine", "hit_page_cap"),
            ):
                scanner.pipeline_audit._insert_event(
                    db, stamp="scan-start", source="scan", stage="profile",
                    addr=addr, status=status, reason=reason,
                )
            db.commit()

            coverage = scanner._profiled_generation_coverage(db, "g1", "scan-start")

            self.assertEqual(coverage, {
                "complete": 2,
                "valid": 1,
                "deferred": 1,
                "rejected": 0,
                "source": "profile_audit",
            })

    def test_resume_adopts_only_same_scan_deferred_profile_outcomes(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.executemany(
                "INSERT INTO profile(addr,status,reason,profile_generation,data_status,evaluated_at) "
                "VALUES(?,?,?,?,?,?)",
                [
                    ("0xcurrent", "quarantine", "hit_page_cap", "old-g",
                     "deferred_data_error", "scan-start"),
                    ("0xstale", "quarantine", "hit_page_cap", "old-g",
                     "deferred_data_error", "older-scan"),
                ],
            )
            scanner.pipeline_audit._insert_event(
                db, stamp="scan-start", source="scan", stage="perp_prefilter",
                addr="0xcurrent", status="passed", reason="perp_week_volume",
            )
            scanner.pipeline_audit._insert_event(
                db, stamp="older-scan", source="scan", stage="perp_prefilter",
                addr="0xstale", status="passed", reason="perp_week_volume",
            )
            db.commit()

            adopted = scanner._adopt_resumable_deferred_profiles(
                db, "new-g", "scan-start",
            )

            self.assertEqual(adopted, 1)
            self.assertEqual(
                db.execute(
                    "SELECT profile_generation FROM profile WHERE addr='0xcurrent'"
                ).fetchone()[0],
                "new-g",
            )
            self.assertEqual(
                db.execute(
                    "SELECT profile_generation FROM profile WHERE addr='0xstale'"
                ).fetchone()[0],
                "old-g",
            )
            db.close()

    def test_finalize_profiled_generation_reuses_cache_without_wallet_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,source,status,started_at,leaderboard_rows,leaderboard_unique_rows,"
                "leaderboard_complete_rows,leaderboard_completeness,leaderboard_valid,workset_n) "
                "VALUES ('cached-g','scan','leaderboard_validated','start',1,1,1,1,1,1)"
            )
            db.execute(
                "INSERT INTO leaderboard_staging(generation,addr,is_candidate,fetched_at) "
                "VALUES('cached-g','0xaaa',1,'start')"
            )
            db.execute(
                "INSERT INTO profile(addr,status,reason,score,profile_generation,data_status,evidence_status) "
                "VALUES('0xaaa','rejected','thin_edge',0.1,'cached-g','rejected','economically_disqualified')"
            )
            db.execute(
                "INSERT INTO commands(type,status,created_at,acked_at) "
                "VALUES('rescan','acked','start','start')"
            )
            db.commit()
            self.seal_market(db, "cached-g")

            with patch.object(scanner.rest, "post", side_effect=AssertionError("wallet fetch forbidden")), \
                    patch.object(scanner, "form_quality_prefix",
                                 wraps=scanner.form_quality_prefix) as formation:
                result = scanner.finalize_profiled_generation(
                    db, "cached-g", stamp="finish", retune=False,
                )

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["core"], 0)
            self.assertFalse(formation.call_args.kwargs["retune"])
            self.assertEqual(db.execute(
                "SELECT status,complete,is_current FROM scan_generation WHERE generation='cached-g'"
            ).fetchone(), ("published", 1, 1))
            self.assertEqual(db.execute(
                "SELECT status FROM commands WHERE type='rescan'"
            ).fetchone(), ("done",))
            self.assertEqual(
                db.execute(
                    "SELECT kind,complete,candidates,profiled,n_active,outcome_reason "
                    "FROM scan_runs WHERE generation='cached-g'"
                ).fetchone(),
                ("complete", 1, 1, 1, 0, "resumed_profiled_generation"),
            )
            metrics = json.loads(db.execute(
                "SELECT metrics_json FROM scan_generation WHERE generation='cached-g'"
            ).fetchone()[0])
            self.assertEqual(metrics["coarseRecallPassed"], 1)
            self.assertEqual(metrics["perpPrefilterPassed"], 1)
            self.assertEqual(metrics["selectionCore"], 0)
            self.assertTrue(metrics["resumedFinalize"])

    def test_final_copy_summary_reuses_publication_certification(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            marginal = SimpleNamespace(search_meta={"finalStrictCopy": {
                "status": "passed", "selectedCount": 10, "netPnl30d": 2500,
                "dynamicReturn30d": .25, "dynamicReturn7d": .08,
                "startEquity30d": 10000, "endEquity30d": 12500,
                "netPnl7d": 800, "startEquity7d": 10000, "endEquity7d": 10800,
                "maxDrawdown30d": .08,
                "liquidations30d": 0, "actionableOpenRate30d": .91,
                "capacityFit30d": .88, "pricePathCoverage30d": .99,
                "maintenanceMarginCoverage30d": 1.0,
            }})

            portfolio, per_wallet = scanner._store_final_copy_summary(db, "g1", marginal)

            persisted = json.loads(db.execute(
                "SELECT value FROM auto_tune_state WHERE key='effective_portfolio_replay'"
            ).fetchone()[0])
            self.assertEqual(portfolio["netPnl30"], 2500)
            self.assertEqual(portfolio["validationSource"], "final_strict_copy")
            self.assertEqual(persisted["dynamicReturn30d"], .25)
            self.assertEqual(per_wallet, {
                "status": "skipped", "reason": "portfolio_strict_only", "refreshed": 0,
            })

    def test_final_parameter_qualification_overrides_scan_time_core_signal(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            profile = {
                "addr": "0xaaa", "status": "active", "reason": "ok", "score": .9,
                "profile_generation": "g1", "data_status": "valid", "evidence_status": "qualified",
                **qualifying_source_fields(),
                "copy_bt_net_pnl": 3000, "copy_bt_14d_net_pnl": 1200,
                "copy_bt_7d_net_pnl": 900, "copy_bt_closed_n": 20,
                "copy_bt_14d_closed_n": 10, "copy_bt_7d_closed_n": 8,
                "copy_bt_win_rate": .75,
                "copy_bt_window_start_equity": 10_000,
                "copy_bt_7d_window_start_equity": 10_000,
                "actionable_open_rate": .9, "capacity_fit": .9,
                "sector_policy_json": '{"allowed":["crypto"],"crypto":{"allow":true}}',
                "sector_copy_json": strict_sector_json(3000, 20, 1200, 10, 900, 8),
            }
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [profile.get(col) for col in cols],
            )
            research_profile = {**profile, "addr": "0xbbb"}
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [research_profile.get(col) for col in cols],
            )
            db.execute(
                "INSERT INTO watchlist(rank,addr,score,sector_policy_json,updated_at) VALUES(1,?,?,?,'now')",
                ("0xaaa", .9, profile["sector_policy_json"]),
            )
            db.commit()
            final_qualification = {
                "eligible": True, "coreEligible": False, "role": "challenger",
                "status": "challenger_recent_return_watch",
                "reasons": ["最终参数7日收益低于Core百分比线"],
            }

            rows, marginal = scanner._build_explicit_selection(
                db, "g1", "now", 1000,
                forced_core_order=(), formation_meta={},
                effective_qualifications={"0xaaa": final_qualification},
                effective_scores={"0xaaa": .8},
            )

            self.assertEqual(marginal.selected, ())
            self.assertEqual([(row.addr, row.role, row.reason) for row in rows], [
                ("0xaaa", "challenger", "challenger_recent_return_watch"),
            ])
            self.assertEqual(db.execute(
                "SELECT state,current_role FROM wallet_registry WHERE addr='0xbbb'"
            ).fetchone(), ("rejected", None))

    def test_final_parameter_policy_promotes_watch_sector_for_selected_core(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            profile = {
                "addr": "0xaaa", "status": "active", "reason": "ok", "score": .9,
                "profile_generation": "g1", "data_status": "valid",
                "evidence_status": "qualified", "follow_score": .9,
                "sector_policy_json": json.dumps({
                    "allowed": [], "watch": ["crypto"],
                    "crypto": {"allow": False, "watch": True},
                }),
                "follow_qualification": {
                    "eligible": True, "coreEligible": False,
                    "status": "challenger_return_watch", "role": "challenger",
                },
            }
            final_policy = json.dumps({
                "allowed": ["crypto"], "watch": [],
                "crypto": {"allow": True, "watch": False},
            })
            final_qualification = {
                "eligible": True, "coreEligible": True,
                "status": "core_eligible", "role": "core_eligible",
            }
            metrics = scanner.selection.PortfolioMetrics(
                100, 80, 0, .9, .9, .01, .1, .01,
                net_pnl=100, stress_net_pnl=80, drawdown_dollars=10,
                risk_adjusted_utility=90,
            )
            transition = {
                "selected": ("0xaaa",), "metrics": metrics,
                "utilities": {"0xaaa": 90}, "reasons": {}, "looRemoved": (),
            }
            self.seal_market(db, "g1")
            with patch.object(
                    scanner.auto_tune, "_portfolio_window_fills",
                    return_value={30: [{"user": "0xaaa", "coin": "BTC", "time": 1}]},
            ) as window_fills, patch.object(
                    scanner.price_path, "load_refined", return_value=[],
            ), patch.object(
                    scanner.price_path, "coverage", return_value={"coverage": 1.0},
            ), patch.object(
                    scanner, "_quality_first_core_transition", return_value=transition,
            ), patch.object(
                    scanner.auto_tune, "_candidate_windows",
                    return_value={
                        30: {
                            "copy_net_pnl": 200, "closed_n": 10,
                            "open_fill_rate": .95, "capacity_open_fit": .95,
                            "max_drawdown": .01, "liquidations": 0,
                            "price_path_coverage": 1.0,
                            "maintenance_margin_coverage": 1.0,
                            "window_start_equity": 1000,
                        },
                        7: {
                            "copy_net_pnl": 50, "closed_n": 5,
                            "open_fill_rate": .95, "capacity_open_fit": .95,
                            "window_start_equity": 1000,
                        },
                    },
            ) as final_replay:
                rows, _marginal = scanner._build_forced_prefix_selection(
                    db, "g1", "now", 1,
                    profiles=[profile], previous_roles={}, controls={"0xaaa": True}, held=set(),
                    desired_order=("0xaaa",), formation_meta={
                        "robustAllowedMemberships": [["0xaaa"]],
                    },
                    effective_qualifications={"0xaaa": final_qualification},
                    effective_scores={"0xaaa": .95},
                    effective_policies={"0xaaa": final_policy},
                    effective_metrics={"0xaaa": {
                        "copy_bt_net_pnl": 4321.0,
                        "copy_bt_win_rate": .80,
                        "copy_bt_closed_n": 10,
                        "copy_bt_7d_net_pnl": 777.0,
                        "copy_bt_7d_closed_n": 3,
                        "sector_copy_json": strict_sector_json(),
                    }},
                    effective_score_details={"0xaaa": {
                        "economicScore": .88,
                        "reasons": ["最终参数评分证据"],
                    }},
                    effective_replay_params_hash="sealed-hash",
                )

            self.assertEqual([(row.addr, row.role) for row in rows], [("0xaaa", "core")])
            self.assertEqual(json.loads(rows[0].sector_policy_json)["allowed"], ["crypto"])
            self.assertEqual(rows[0].replay_copy_bt_net_pnl, 4321.0)
            self.assertEqual(rows[0].replay_copy_bt_7d_net_pnl, 777.0)
            self.assertEqual(rows[0].replay_params_hash, "sealed-hash")
            self.assertEqual(
                json.loads(rows[0].replay_score_detail_json)["economicScore"], .88,
            )
            self.assertEqual(rows[0].replayed_at, "now")
            self.assertTrue(window_fills.call_args.kwargs["include_watch"])
            final_replay.assert_called_once()
            self.assertIsNotNone(final_replay.call_args.kwargs["path_rows"])

    def test_star_cannot_bypass_final_win_gate_and_held_position_becomes_exit_only(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            profile = {
                "addr": "0xstar", "status": "active", "reason": "ok", "score": .9,
                "profile_generation": "g1", "data_status": "valid", "evidence_status": "qualified",
                "sector_policy_json": '{"allowed":["crypto"],"crypto":{"allow":true}}',
            }
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [profile.get(col) for col in cols],
            )
            db.execute(
                "INSERT INTO target_controls(addr,enabled,pinned,pinned_at,updated_at) "
                "VALUES('0xstar',1,1,'old','old')"
            )
            db.execute(
                "INSERT INTO copy_position(addr,coin,side,status,opened_at) "
                "VALUES('0xstar','BTC','long','open','old')"
            )
            db.commit()

            rows, marginal = scanner._build_explicit_selection(
                db, "g1", "now", 1000,
                forced_core_order=(), formation_meta={"effectiveStarred": []},
                effective_qualifications={
                    "0xstar": {
                        "eligible": False, "coreEligible": False, "role": "rejected",
                        "status": "copy_win_rate_below_floor",
                    },
                },
            )

            self.assertEqual(marginal.selected, ())
            self.assertEqual([(row.addr, row.role) for row in rows], [("0xstar", "exit_only")])


    def test_warmup_backfill_targets_only_wallets_with_copy_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            cols = storage.PROFILE_COLS.split(",")
            rows = []
            for addr, closed, pnl in (("0xcopy", 8, 100.0), ("0xstructural", 0, None)):
                row = {"addr": addr, "status": "active", "copy_bt_closed_n": closed,
                       "copy_bt_net_pnl": pnl}
                rows.append([row.get(col) for col in cols])
            db.executemany(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                rows,
            )
            desired_start = 1_000
            self.assertEqual(scanner._copy_warmup_backfill_addrs(db, desired_start), ["0xcopy"])

            with scanner._db_lock:
                scanner._store_cached_fills(
                    db, "0xcopy", [], desired_start,
                    coverage_complete=True, coverage_end=10_000,
                )
                db.commit()
            self.assertEqual(scanner._copy_warmup_backfill_addrs(db, desired_start), [])

    def test_invalid_leaderboard_retains_old_published_selection_and_skips_finalize(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO leaderboard (addr,is_candidate,fetched_at,generation) "
                "VALUES ('0xold',1,'2026-01-01T00:00:00Z','old')"
            )
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at,leaderboard_valid,profile_complete) "
                "VALUES ('old','published',1,1,1,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z',1,1)"
            )
            db.execute(
                "INSERT INTO follow_selection (generation,addr,role,enabled,selected_at) "
                "VALUES ('old','0xold','core',1,'2026-01-01T01:00:00Z')"
            )
            db.commit()

            with patch.object(scanner.rest, "copyable_universe", return_value={"BTC"}), \
                    patch.object(scanner.generation_market, "fetch_context_snapshot", return_value={}), \
                    patch.object(scanner.rest, "get_leaderboard", return_value=[]), \
                    patch.object(scanner, "_prune_discovery_cache") as prune:
                scanner.scan(db, scan_args())

            current = db.execute(
                "SELECT generation FROM scan_generation WHERE is_current=1 AND status='published'"
            ).fetchone()[0]
            failed = db.execute(
                "SELECT status,complete FROM scan_generation WHERE generation!='old' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(current, "old")
            self.assertEqual(failed, ("failed", 0))
            self.assertEqual(db.execute("SELECT addr FROM leaderboard").fetchone()[0], "0xold")
            prune.assert_not_called()

    def test_complete_scan_publishes_generation_and_explicit_challenger(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            now_calls = 0

            def scan_time():
                nonlocal now_calls
                now_calls += 1
                return "2026-01-01T00:00:00Z" if now_calls <= 2 else "2026-01-01T00:01:00Z"

            def fake_profile(db_, addr, now_ms, p, prior, lb, stamp, universe, force_full=False):
                row = {
                    "addr": addr,
                    "status": "active",
                    "reason": "ok",
                    "score": 0.8,
                    "raw_quality_score": 0.8,
                    "profile_generation": p.scan_generation,
                    "evaluated_at": stamp,
                    "last_refreshed": stamp,
                    "data_status": "valid",
                    "evidence_status": "missing",
                    "last_copyable_open_ms": now_ms,
                    "times_seen": 1,
                    "times_active": 1,
                }
                cols = storage.PROFILE_COLS.split(",")
                with scanner._db_lock:
                    db_.execute(
                        f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                        f"VALUES ({','.join('?' for _ in cols)})",
                        [row.get(col) for col in cols],
                    )
                    db_.commit()
                return "active", "ok", row, False

            with patch.object(scanner.rest, "copyable_universe", return_value={"BTC"}), \
                    patch.object(scanner.generation_market, "fetch_context_snapshot", return_value={}), \
                    patch.object(scanner.rest, "get_leaderboard", return_value=[leaderboard_row()]), \
                    patch.object(scanner.rest, "portfolio", return_value=portfolio_rows()), \
                    patch.object(scanner, "_profile_one", side_effect=fake_profile), \
                    patch.object(scanner, "now_iso", side_effect=scan_time), \
                    patch.object(scanner.generation, "now_iso", return_value="2026-01-01T00:01:00Z"), \
                    patch.object(scanner, "_prune_discovery_cache", return_value={}):
                scanner.scan(db, scan_args())

            current = db.execute(
                "SELECT generation,profile_complete,ready_at,published_at,started_at FROM scan_generation "
                "WHERE is_current=1 AND status='published'"
            ).fetchone()
            selection_row = db.execute(
                "SELECT generation,addr,role,data_status,evidence_status FROM follow_selection"
            ).fetchone()
            self.assertEqual(current[1], 1)
            self.assertEqual(current[3], current[2])
            self.assertGreater(current[3], current[4])
            self.assertIsNone(selection_row)
            self.assertEqual(db.execute("SELECT DISTINCT generation FROM leaderboard").fetchone()[0], current[0])

    def test_complete_profiles_remain_resumable_when_portfolio_formation_fails(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)

            def fake_profile(db_, addr, now_ms, p, prior, lb, stamp, universe,
                             force_full=False):
                row = {
                    "addr": addr, "status": "active", "reason": "ok", "score": .8,
                    "profile_generation": p.scan_generation, "data_status": "valid",
                    "evidence_status": "qualified", "last_copyable_open_ms": now_ms,
                }
                cols = storage.PROFILE_COLS.split(",")
                with scanner._db_lock:
                    db_.execute(
                        f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                        f"VALUES ({','.join('?' for _ in cols)})",
                        [row.get(col) for col in cols],
                    )
                    db_.commit()
                return "active", "ok", row, False

            with patch.object(scanner.rest, "copyable_universe", return_value={"BTC"}), \
                    patch.object(scanner.generation_market, "fetch_context_snapshot", return_value={}), \
                    patch.object(scanner.rest, "get_leaderboard", return_value=[leaderboard_row()]), \
                    patch.object(scanner.rest, "portfolio", return_value=portfolio_rows()), \
                    patch.object(scanner, "_profile_one", side_effect=fake_profile), \
                    patch.object(scanner, "form_quality_prefix", side_effect=RuntimeError("tune failed")), \
                    patch.object(scanner, "_prune_discovery_cache", return_value={}):
                scanner.scan(db, scan_args())

            row = db.execute(
                "SELECT status,complete,workset_n FROM scan_generation ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(row, ("leaderboard_validated", 0, 1))
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM profile WHERE profile_generation=("
                "SELECT generation FROM scan_generation ORDER BY id DESC LIMIT 1)"
            ).fetchone()[0], 1)

    def test_cold_paper_bootstrap_can_seed_first_strict_core(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)

            def fake_profile(db_, addr, now_ms, p, prior, lb, stamp, universe, force_full=False):
                row = {
                    "addr": addr, "status": "active", "reason": "ok", "score": 0.9,
                    "raw_quality_score": 0.9, "profile_generation": p.scan_generation,
                    "evaluated_at": stamp, "last_refreshed": stamp, "data_status": "valid",
                    "evidence_status": "qualified",
                    **qualifying_source_fields(now_ms),
                    "copy_bt_closed_n": 20, "copy_bt_14d_closed_n": 10, "copy_bt_7d_closed_n": 8,
                    "copy_bt_win_rate": .75,
                    "copy_bt_window_start_equity": 10_000,
                    "copy_bt_7d_window_start_equity": 10_000,
                    "copy_bt_open_fill_rate": 0.95, "actionable_open_rate": 0.95,
                    "capacity_fit": 0.95, "copy_bt_net_pnl": 1800,
                    "copy_bt_14d_net_pnl": 900, "copy_bt_7d_net_pnl": 600,
                    "sector_policy_json": strict_policy_json(),
                    "sector_copy_json": strict_sector_json(1800, 20, 900, 10, 600, 8),
                    "times_seen": 1, "times_active": 1,
                }
                cols = storage.PROFILE_COLS.split(",")
                with scanner._db_lock:
                    db_.execute(
                        f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                        f"VALUES ({','.join('?' for _ in cols)})",
                        [row.get(col) for col in cols],
                    )
                    db_.commit()
                return "active", "ok", row, False

            def fake_rough(db_, addrs, generation_id, now_ms, p, stamp, **kwargs):
                db_.execute(
                    "UPDATE profile SET rough_copy_score=.9,status='active',reason='pre_strict_qualified' "
                    "WHERE addr='0xaaa' AND profile_generation=?",
                    (generation_id,),
                )
                db_.execute(
                    "INSERT INTO pre_strict_evidence "
                    "(generation,addr,policy_version,model_version,status,activity_json,"
                    "tier,queue_rank,rough_profit_priority,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        generation_id, "0xaaa",
                        scanner.pre_strict.POLICY_VERSION,
                        scanner.pre_strict.SELECTION_MODEL_VERSION,
                        "passed",
                        json.dumps(qualifying_source_fields(now_ms)["pre_strict_activity"]),
                        "primary", 1, .144, stamp,
                    ),
                )
                db_.commit()
                return {
                    "attempted": 1, "qualified": ["0xaaa"],
                    "failed": [], "queued": ["0xaaa"],
                }

            strict_windows = {
                30: {
                    "copy_net_pnl": 100, "closed_n": 10, "open_fill_rate": .95,
                    "capacity_open_fit": .95, "max_drawdown": .01,
                    "maintenance_margin_coverage": 1.0,
                    "price_path_coverage": 1.0, "window_start_equity": 1000,
                },
                14: {
                    "copy_net_pnl": 80, "closed_n": 8, "open_fill_rate": .95,
                    "capacity_open_fit": .95, "max_drawdown": .01,
                    "maintenance_margin_coverage": 1.0,
                },
                7: {
                    "copy_net_pnl": 60, "closed_n": 7, "open_fill_rate": .95,
                    "capacity_open_fit": .95, "max_drawdown": .01,
                    "maintenance_margin_coverage": 1.0,
                    "window_start_equity": 1000,
                },
            }

            follow = params.load_follow(db)
            proposal = {
                key: follow[key]
                for key in (*scanner.auto_tune.TUNE_KEYS, *scanner.auto_tune.ADD_TUNE_KEYS)
            }
            formation = {
                "selected": ("0xaaa",), "ranked": ("0xaaa",), "params": proposal,
                "search": {"algorithm": "quality_prefix_binary_v1", "initialCount": 1,
                           "selectedCount": 1},
            }
            with patch.object(scanner.rest, "copyable_universe", return_value={"BTC"}), \
                    patch.object(scanner.generation_market, "fetch_context_snapshot", return_value={}), \
                    patch.object(scanner.rest, "get_leaderboard", return_value=[leaderboard_row()]), \
                    patch.object(scanner.rest, "portfolio", return_value=portfolio_rows()), \
                    patch.object(scanner, "_profile_one", side_effect=fake_profile), \
                    patch.object(scanner, "_rough_replay_source_pool", side_effect=fake_rough), \
                    patch.object(scanner, "form_quality_prefix", return_value=formation), \
                    patch.object(scanner.auto_tune, "_portfolio_window_fills",
                                 return_value={30: [{}], 14: [{}], 7: [{}]}), \
                    patch.object(scanner.auto_tune, "_candidate_windows", return_value=strict_windows), \
                    patch.object(scanner, "_prune_discovery_cache", return_value={}):
                scanner.scan(db, scan_args())

            current = db.execute(
                "SELECT generation FROM scan_generation WHERE is_current=1 AND status='published'"
            ).fetchone()[0]
            row = db.execute(
                "SELECT addr,role FROM follow_selection WHERE generation=?", (current,)
            ).fetchone()
            self.assertEqual(row, ("0xaaa", "core"))
            registry = db.execute(
                "SELECT state,current_role FROM wallet_registry WHERE addr='0xaaa'"
            ).fetchone()
            self.assertEqual(registry, ("core", "core"))


    def test_repair_empty_published_selection_uses_cached_generation_and_launches_tuner(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at,"
                "leaderboard_valid,profile_complete) "
                "VALUES ('g1','published',1,1,1,'2026-01-01','2026-01-02',1,1)"
            )
            cols = storage.PROFILE_COLS.split(",")
            profile = {
                "addr": "0xaaa", "status": "active", "reason": "ok", "score": 0.9,
                "profile_generation": "g1", "data_status": "valid", "evidence_status": "qualified",
            }
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [profile.get(col) for col in cols],
            )
            db.execute(
                "INSERT INTO follow_selection (generation,addr,role,enabled,selected_at) "
                "VALUES ('g1','0xaaa','challenger',1,'2026-01-02')"
            )
            db.commit()
            self.seal_market(db, "g1")
            core_row = scanner.selection.SelectionRow(
                "0xaaa", "core", reason="core_entry", data_status="valid", evidence_status="qualified",
                acct_value=10000,
                sector_policy_json='{"allowed":["crypto"],"crypto":{"allow":true}}',
            )
            marginal = scanner.selection.MarginalSelectionResult(
                selected=("0xaaa",),
                baseline=scanner.selection.PortfolioMetrics(0, 0, 0, 1, 1, 0, 0, 0),
                metrics=scanner.selection.PortfolioMetrics(10, 5, 0, 1, 1, .005, .1, .1),
                action="bootstrap", added=("0xaaa",),
            )

            with patch.object(scanner, "_build_explicit_selection", return_value=([core_row], marginal)) as build:
                result = scanner.repair_published_selection(db, "g1", "2026-01-03")

            self.assertEqual(result["status"], "repaired")
            self.assertEqual(result["core"], 1)
            self.assertEqual(db.execute(
                "SELECT role FROM follow_selection WHERE generation='g1' AND addr='0xaaa'"
            ).fetchone()[0], "core")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM commands WHERE type='reload_params' AND status='pending'"
            ).fetchone()[0], 1)
            build.assert_called_once()
            self.assertTrue(build.call_args.kwargs["force_cold_bootstrap"])
            self.assertEqual(result["tuner"]["status"], "complete")

    def test_repair_existing_selection_refreshes_watchlist_before_rebuild(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at,"
                "leaderboard_valid,profile_complete) "
                "VALUES ('g1','published',1,1,1,'2026-01-01','2026-01-02',1,1)"
            )
            cols = storage.PROFILE_COLS.split(",")
            profile = {
                "addr": "0xaaa", "status": "active", "reason": "ok", "score": 0.9,
                "profile_generation": "g1", "data_status": "valid", "evidence_status": "qualified",
                "last_copyable_open_ms": 1000,
                "sector_policy_json": strict_policy_json(),
            }
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [profile.get(col) for col in cols],
            )
            db.execute(
                "INSERT INTO follow_selection(generation,addr,role,enabled,selected_at) "
                "VALUES('g1','0xaaa','core',1,'2026-01-02')"
            )
            db.commit()
            self.seal_market(db, "g1")
            core_row = scanner.selection.SelectionRow(
                "0xaaa", "core", reason="core_keep", acct_value=10000,
                sector_policy_json='{"allowed":["crypto"],"crypto":{"allow":true}}',
            )

            def build(db_arg, generation, stamp, now_ms, **kwargs):
                self.assertIsNotNone(db_arg.execute(
                    "SELECT 1 FROM watchlist WHERE addr='0xaaa'"
                ).fetchone())
                self.assertFalse(kwargs["force_cold_bootstrap"])
                return [core_row], None

            with patch.object(scanner, "_build_explicit_selection", side_effect=build):
                result = scanner.repair_published_selection(
                    db, "g1", "2026-01-03", replace_existing=True,
                )

            self.assertEqual(result["status"], "repaired")
            self.assertEqual(result["core"], 1)

    def test_forced_cold_bootstrap_ignores_registry_core_role(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            profile = {
                "addr": "0xaaa", "status": "active", "reason": "ok", "score": 0.9,
                "profile_generation": "g1", "data_status": "valid", "evidence_status": "qualified",
                **qualifying_source_fields(1000),
                "copy_bt_net_pnl": 1800, "copy_bt_14d_net_pnl": 900, "copy_bt_7d_net_pnl": 600,
                "copy_bt_closed_n": 12, "copy_bt_14d_closed_n": 8, "copy_bt_7d_closed_n": 5,
                "copy_bt_win_rate": .75, "copy_bt_window_start_equity": 10_000,
                "copy_bt_7d_window_start_equity": 10_000, "copy_evidence_days": 8,
                "actionable_open_rate": .9, "capacity_fit": .9,
                "sector_policy_json": '{"allowed":["crypto"],"crypto":{"allow":true}}',
                "sector_copy_json": strict_sector_json(1800, 12, 900, 8, 600, 5),
            }
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [profile.get(col) for col in cols],
            )
            db.execute(
                "INSERT INTO wallet_registry "
                "(addr,state,current_role,first_seen_at,last_seen_at,updated_at,consecutive_qualified) "
                "VALUES ('0xaaa','core','core','old','old','old',9)"
            )
            db.commit()

            rows, marginal = scanner._build_explicit_selection(
                db, "g1", "2026-01-03", 1000, force_cold_bootstrap=True,
                forced_core_order=(),
            )

            self.assertEqual(marginal.selected, ())
            self.assertEqual(
                marginal.search_meta["membershipPolicy"],
                scanner.pre_strict.SELECTION_MODEL_VERSION,
            )
            self.assertEqual(rows, [])

    def test_manual_selection_mode_cannot_bypass_current_hard_gate(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            db.execute("UPDATE params SET value='manual' WHERE key='FOLLOW_SELECTION_MODE'")
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at,leaderboard_valid,profile_complete) "
                "VALUES ('manual-old','published',1,1,1,'2026-01-01','2026-01-02',1,1)"
            )
            db.execute(
                "INSERT INTO follow_selection "
                "(generation,addr,role,enabled,reason,utility,data_status,evidence_status,acct_value,"
                "sector_policy_json,selected_at) "
                "VALUES ('manual-old','0xoperator','core',1,'operator_pick',9.0,'valid','qualified',10000,"
                "'{\"allowed\":[\"crypto\"],\"crypto\":{\"allow\":true}}','2026-01-02')"
            )
            db.commit()

            def fake_profile(db_, addr, now_ms, p, prior, lb, stamp, universe, force_full=False):
                row = {
                    "addr": addr, "status": "active", "reason": "ok", "score": 0.99,
                    "raw_quality_score": 0.99, "profile_generation": p.scan_generation,
                    "evaluated_at": stamp, "last_refreshed": stamp, "data_status": "valid",
                    "evidence_status": "qualified", "last_copyable_open_ms": now_ms,
                    "times_seen": 1, "times_active": 1,
                }
                cols = storage.PROFILE_COLS.split(",")
                with scanner._db_lock:
                    db_.execute(
                        f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                        f"VALUES ({','.join('?' for _ in cols)})",
                        [row.get(col) for col in cols],
                    )
                    db_.commit()
                return "active", "ok", row, False

            with patch.object(scanner.rest, "copyable_universe", return_value={"BTC"}), \
                    patch.object(scanner.generation_market, "fetch_context_snapshot", return_value={}), \
                    patch.object(scanner.rest, "get_leaderboard", return_value=[leaderboard_row("0xauto")]), \
                    patch.object(scanner.rest, "portfolio", return_value=portfolio_rows()), \
                    patch.object(scanner, "_profile_one", side_effect=fake_profile), \
                    patch.object(scanner, "_prune_discovery_cache", return_value={}):
                scanner.scan(db, scan_args())

            current = db.execute(
                "SELECT generation FROM scan_generation WHERE is_current=1 AND status='published'"
            ).fetchone()[0]
            rows = db.execute(
                "SELECT addr,role,reason FROM follow_selection WHERE generation=? ORDER BY addr", (current,)
            ).fetchall()
            summary = db.execute(
                "SELECT reason,payload_json FROM pipeline_audit "
                "WHERE stage='selection_summary' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(rows, [])
            self.assertEqual(summary[0], "manual_selection_preserved")
            self.assertIn('"mode": "manual"', summary[1])


if __name__ == "__main__":
    unittest.main()
