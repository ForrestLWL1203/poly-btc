import tempfile
import inspect
import json
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
        campaigns = min(closed, 12)
        campaign_wins = int(round(campaigns * rate))
        return {
            "copy_net_pnl": net, "closed_n": closed, "wins": wins,
            "opened_n": closed, "target_open_events": closed,
            "liquidations": 0, "valuation_status": "complete",
            "profit_factor": 2.5, "payoff_ratio": 1.5,
            "top1_profit_share": .20, "top3_profit_share": .45,
            "body_after_top3_n": max(1, closed - 3),
            "body_after_top3_wins": max(1, int(round(max(1, closed - 3) * rate))),
            "body_after_top3_win_rate": rate,
            "body_after_top3_net_pnl": net * .35,
            "cost_stress_net_pnl": net * .7,
            "campaign_closed_n": campaigns, "campaign_wins": campaign_wins,
            "campaign_net_after_top1": net * .45,
            "campaign_net_after_top2": net * .3,
            "path_risk_status": "complete", "intratrade_max_drawdown": .05,
            "deep_bag_event_n": 0, "failed_deep_bag_n": 0,
            "deep_bag_recovery_rate": 1.0, "initial_margin_equity": 10_000,
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
        "copyWeeklyProfitability": {
            "version": "nonoverlap-weekly-return-v2", "evidenceSufficient": True,
            "passed": True, "evaluableFolds": 4, "profitableFolds": 4,
            "qualifiedFolds": 4,
        },
    })


def strict_weekly_stability():
    return {
        "version": "nonoverlap-weekly-return-v2",
        "evidenceSufficient": True,
        "passed": True,
        "evaluableFolds": 4,
        "profitableFolds": 4,
        "qualifiedFolds": 4,
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
            scanner._assert_margin_equity_snapshot(db, 1.0)
            db.execute("UPDATE params SET value='50' WHERE key='MARGIN_EQUITY_PCT'")
            db.commit()
            with self.assertRaisesRegex(RuntimeError, "margin_equity_pct_changed_during_generation"):
                scanner._assert_margin_equity_snapshot(db, 1.0)

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
            source.index("chosen_addrs, chosen, robust_check = robust_winner")
        ]

        self.assertNotIn('raise RuntimeError("no_robust_quality_membership")', failure_branch)
        self.assertIn("chosen_addrs = ()", failure_branch)
        self.assertIn('"explicitEmptyCore": True', failure_branch)

    def test_core_formation_uses_coarse_prefixes_and_one_full_winner_tune(self):
        source = inspect.getsource(scanner.form_quality_prefix)

        self.assertEqual(source.count("auto_tune.maybe_tune_margins("), 2)
        self.assertIn('search_profile="coarse"', source)
        self.assertIn('search_profile="full"', source)
        self.assertIn("addrs_override=list(tune_ordered[:count])", source)
        self.assertIn("addrs_override=list(tune_ordered[:winning_count])", source)
        self.assertIn("except TimeoutError as exc", source)
        self.assertIn("full_tune_timeout_using_coarse", source)

    def test_normal_scan_does_not_block_publication_on_parameter_grid(self):
        scan_source = inspect.getsource(scanner.scan)
        optimize_source = inspect.getsource(scanner.optimize_published_generation)
        formation_source = inspect.getsource(scanner.form_quality_prefix)
        publication_source = inspect.getsource(scanner._build_forced_prefix_selection)

        self.assertIn(
            "form_quality_prefix(\n                    db, generation_id, stamp, now_ms, retune=False,",
            scan_source,
        )
        self.assertIn("retune_formation=True", optimize_source)
        self.assertIn("path_rows=None, path_meta=None", formation_source)
        self.assertNotIn("shared_path = price_path.load_refined", formation_source)
        self.assertEqual(publication_source.count("evaluate_portfolio_window("), 1)
        self.assertIn("final_strict_copy_failed:", publication_source)

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
                },
            }
            with patch.object(scanner, "_quality_core_profiles", return_value=[candidate]), \
                    patch.object(scanner.auto_tune, "_load_sigmas", return_value={}), \
                    patch.object(scanner.auto_tune, "_load_market_ctx", return_value={}), \
                    patch.object(scanner, "_current_copy_valuation_marks", return_value={}), \
                    patch.object(scanner.selection, "pinned_core_controls", return_value=[]), \
                    patch.object(scanner.selection, "published_core_addrs", return_value=[]), \
                    patch.object(scanner, "_core_rebalance_due", return_value=(True, None)), \
                    patch.object(scanner, "_rank_formation_candidates_for_surface",
                                 return_value=[candidate]), \
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

    def test_explicit_empty_core_turns_old_core_exit_only(self):
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
            self.assertEqual(rows[0].role, scanner.selection.EXIT_ONLY)
            self.assertFalse(rows[0].enabled)
            self.assertIn("no_robust_core", rows[0].reason)

    def test_core_soft_failure_needs_two_distinct_complete_generations(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            db.execute(
                "INSERT INTO wallet_registry "
                "(addr,state,current_role,first_seen_at,last_seen_at,updated_at) "
                "VALUES ('0xold','core','core','now','now','now')"
            )
            soft = {
                "eligible": True, "coreEligible": False, "role": "challenger",
                "status": "challenger_return_watch", "reasons": ["soft"],
            }

            first = scanner._apply_core_soft_failure_grace(db, "0xold", "g1", soft)
            duplicate = scanner._apply_core_soft_failure_grace(db, "0xold", "g1", soft)
            second = scanner._apply_core_soft_failure_grace(db, "0xold", "g2", soft)

            self.assertTrue(first["coreEligible"])
            self.assertTrue(duplicate["coreEligible"])
            self.assertFalse(second["coreEligible"])
            count = db.execute(
                "SELECT core_soft_fail_count FROM wallet_registry WHERE addr='0xold'"
            ).fetchone()[0]
            self.assertEqual(count, 2)

    def test_core_hard_risk_bypasses_soft_failure_grace(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            db.execute(
                "INSERT INTO wallet_registry "
                "(addr,state,current_role,first_seen_at,last_seen_at,updated_at) "
                "VALUES ('0xold','core','core','now','now','now')"
            )
            hard = {
                "eligible": False, "coreEligible": False, "role": "exit_only",
                "status": "current_deep_loss_freeze", "hardRisk": True,
            }

            result = scanner._apply_core_soft_failure_grace(db, "0xold", "g1", hard)

            self.assertFalse(result["coreEligible"])
            self.assertEqual(result["role"], "exit_only")

    def test_new_core_promotion_needs_prior_complete_generation_at_least_24h_old(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,started_at,published_at) "
                "VALUES('old-good','published',1,'1970-01-01T00:00:00Z','1970-01-01T01:00:00Z')"
            )
            db.execute(
                "INSERT INTO pipeline_audit "
                "(stamp,source,stage,addr,status,payload_json,created_at) "
                "VALUES('s','scan','profile','0xaaa','active',?, '1970-01-01T01:00:00Z')",
                (json.dumps({
                    "qualification": {"profileGeneration": "old-good"},
                    "followEligibility": {"coreEligible": True},
                }),),
            )
            db.commit()
            profiles = [{"addr": "0xaaa", "follow_qualification": {"coreEligible": True}}]

            early, _ = scanner._core_membership_hysteresis(
                db, profiles, {}, now_ms=24 * 3_600_000,
            )
            ready, _ = scanner._core_membership_hysteresis(
                db, profiles, {}, now_ms=26 * 3_600_000,
            )

            self.assertNotIn("0xaaa", early)
            self.assertIn("0xaaa", ready)

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

    def test_core_formation_tune_pool_contains_only_individually_core_eligible_wallets(self):
        self.assertTrue(scanner._formation_tune_candidate({
            "follow_qualification": {"eligible": True, "coreEligible": True},
        }))
        self.assertFalse(scanner._formation_tune_candidate({
            "follow_qualification": {
                "eligible": True, "coreEligible": False,
                "status": "challenger_weekly_return_watch",
            },
        }))

    def test_manual_optimize_requalifies_incumbents_as_new_entries(self):
        source = inspect.getsource(scanner.optimize_published_generation)

        self.assertIn("force_entry_requalification=True", source)
        self.assertFalse(scanner._formation_tune_candidate({
            "follow_qualification": {
                "eligible": True, "coreEligible": False,
                "status": "challenger_open_valuation_pending",
            },
        }))
        self.assertFalse(scanner._formation_tune_candidate({
            "follow_qualification": {
                "eligible": True, "coreEligible": False,
                "status": "challenger_sample_watch",
            },
        }))
        self.assertFalse(scanner._formation_tune_candidate({
            "follow_qualification": {
                "eligible": False, "coreEligible": False,
                "status": "copy_value_below_challenger_floor",
            },
        }))

    def test_formation_ranking_uses_effective_surface_replay_not_scan_time_score(self):
        rows = [
            {"addr": "0xold", "follow_score": .95},
            {"addr": "0xstrong", "follow_score": .50},
        ]

        def replay(_db, row, _now_ms, **_kwargs):
            return {
                "score": .90 if row["addr"] == "0xstrong" else .40,
                "qualification": {
                    "eligible": True, "coreEligible": True, "status": "core_eligible",
                },
            }

        with patch.object(scanner, "_effective_follow_replay", side_effect=replay):
            ranked = scanner._rank_formation_candidates_for_surface(
                None, rows, 1000, generation_id="g1", follow={}, valuation_marks={},
                sigmas={}, market_ctx={},
            )

        self.assertEqual([row["addr"] for row in ranked], ["0xstrong", "0xold"])
        self.assertEqual(ranked[0]["follow_score"], .90)

    def test_formation_ranking_never_reloads_incumbents_during_forced_entry_replay(self):
        retained = []

        def replay(_db, row, _now_ms, **kwargs):
            retained.append((row["addr"], kwargs["retention"]))
            return {
                "score": .80,
                "qualification": {
                    "eligible": True, "coreEligible": True, "status": "core_eligible",
                },
            }

        with (
            patch.object(scanner.selection, "published_core_addrs", side_effect=AssertionError),
            patch.object(scanner, "_effective_follow_replay", side_effect=replay),
        ):
            ranked = scanner._rank_formation_candidates_for_surface(
                object(), [{"addr": "0xold"}], 1000, generation_id="g1", follow={},
                valuation_marks={}, sigmas={}, market_ctx={}, retention_addrs=(),
            )

        self.assertEqual([row["addr"] for row in ranked], ["0xold"])
        self.assertEqual(retained, [("0xold", False)])

    def test_final_surface_quarantines_one_bad_candidate_without_aborting_generation(self):
        source = inspect.getsource(scanner.form_quality_prefix)

        self.assertNotIn('raise RuntimeError(f"effective_copy_replay_invalid:', source)
        self.assertIn('raise RuntimeError(f"pinned_core_replay_invalid:', source)
        self.assertIn("if replay_invalid:", source)
        self.assertIn("rejected.append(addr)", source)


    def test_path_validation_is_portfolio_fail_closed_not_wallet_regate(self):
        source = inspect.getsource(scanner._build_explicit_selection)

        self.assertIn('path_reasons.append("path_net_nonpositive")', source)
        self.assertIn('"reusedStrictReplay": True', source)
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

    def test_bounded_path_pool_prioritizes_incumbent_and_prepath_core_quality(self):
        checks = {
            key: True for key in (
                "strictCopy30dPositive", "strictCopyWeeklyPositive", "tenIndependentCampaigns",
                "campaignWinRate", "repeatableBodyWinRate", "repeatableBodyPositive",
                "coreFollowScore", "activityWithin72h", "oneWinnerRemovalPositive",
                "costStressPositive", "openExecution", "capacity", "valuationComplete",
                "sectorExecutable", "expectedEdge", "noRepeatedLiquidation",
                "noForwardLiquidation",
            )
        }
        rows = [
            {
                "addr": "0xweak", "follow_score": .99,
                "follow_qualification": {
                    "eligible": True, "evidenceDays": 10,
                    "checks": {**checks, "strictCopyWeeklyPositive": False},
                },
                "sector_policy_json": "{}",
            },
            {
                "addr": "0xready", "follow_score": .80,
                "follow_qualification": {
                    "eligible": True, "evidenceDays": 10, "checks": checks,
                },
                "sector_policy_json": "{}",
            },
            {
                "addr": "0xincumbent", "follow_score": .10,
                "follow_qualification": {"eligible": False},
                "sector_policy_json": "{}",
            },
        ]

        selected = scanner._bounded_formation_candidates(
            rows, ("0xincumbent",), 40,
        )

        self.assertEqual(
            [row["addr"] for row in selected],
            ["0xincumbent", "0xready"],
        )

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
                "copy_bt_7d_net_pnl,copy_bt_7d_closed_n,copy_expected_return,copy_return_lcb,"
                "copy_positive_probability,copy_evidence_days,actionable_open_rate,capacity_fit,"
                "sector_policy_json,sector_copy_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "0xsector", "active", "ok", .9, "g-sector", "valid", "qualified",
                    2_000, 30, 1_000, 15, 500, 8, .05, .02, .9, 12, .9, .9,
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

    def test_final_copy_summary_reuses_publication_certification(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            marginal = SimpleNamespace(search_meta={"finalStrictCopy": {
                "status": "passed", "selectedCount": 10, "netPnl30d": 2500,
                "expectedReturn30d": .25, "maxDrawdown30d": .08,
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
            self.assertEqual(persisted["expectedReturn30d"], .25)
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
                "copy_bt_net_pnl": 3000, "copy_bt_14d_net_pnl": 1200,
                "copy_bt_7d_net_pnl": 900, "copy_bt_closed_n": 20,
                "copy_bt_14d_closed_n": 10, "copy_bt_7d_closed_n": 8,
                "copy_expected_return": .06, "copy_return_lcb": .02,
                "copy_positive_probability": .9, "copy_evidence_days": 12,
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
                "status": "challenger_weekly_return_watch",
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
                ("0xaaa", "challenger", "challenger_weekly_return_watch"),
            ])
            self.assertEqual(db.execute(
                "SELECT state,current_role FROM wallet_registry WHERE addr='0xbbb'"
            ).fetchone(), ("qualified", None))

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
                    scanner.auto_tune, "evaluate_portfolio_window",
                    return_value={
                        "copy_net_pnl": 100, "closed_n": 10, "open_fill_rate": .95,
                        "capacity_open_fit": .95, "max_drawdown": .01,
                        "liquidations": 0, "price_path_coverage": 1.0,
                        "maintenance_margin_coverage": 1.0,
                        "initial_margin_equity": 1000,
                        "weekly_stability": strict_weekly_stability(),
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
                )

            self.assertEqual([(row.addr, row.role) for row in rows], [("0xaaa", "core")])
            self.assertEqual(json.loads(rows[0].sector_policy_json)["allowed"], ["crypto"])
            self.assertTrue(window_fills.call_args.kwargs["include_watch"])
            final_replay.assert_called_once()
            self.assertEqual(final_replay.call_args.kwargs["days"], 30)
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

            def fake_profile(db_, addr, start_ms, now_ms, p, prior, lb, stamp, universe, force_full=False):
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

            def fake_profile(db_, addr, start_ms, now_ms, p, prior, lb, stamp, universe,
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

            def fake_profile(db_, addr, start_ms, now_ms, p, prior, lb, stamp, universe, force_full=False):
                row = {
                    "addr": addr, "status": "active", "reason": "ok", "score": 0.9,
                    "raw_quality_score": 0.9, "profile_generation": p.scan_generation,
                    "evaluated_at": stamp, "last_refreshed": stamp, "data_status": "valid",
                    "evidence_status": "qualified", "last_copyable_open_ms": now_ms,
                    "copy_bt_closed_n": 20, "copy_bt_14d_closed_n": 10, "copy_bt_7d_closed_n": 8,
                    "copy_positive_probability": 0.85, "copy_expected_return": 0.05,
                    "copy_return_lcb": 0.015, "copy_return_volatility": 0.08,
                    "copy_evidence_days": 10, "copy_recent_return_14d": 0.04,
                    "copy_recent_return_7d": 0.03, "copy_risk_score": 0.85,
                    "execution_score": 0.95, "open_probability_48h": 0.8,
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

            metrics = scanner.selection.PortfolioMetrics(
                100.0, 80.0, 0, 0.95, 0.95, 0.05, 0.20, 0.05,
                net_pnl=100.0, stress_net_pnl=80.0, drawdown_dollars=50.0,
                risk_adjusted_utility=50.0,
            )
            staged = scanner.offline_core_optimizer.OfflineSearchResult(
                selected=("0xaaa",), metrics=metrics, initial=(),
                initial_metrics=scanner.selection.PortfolioMetrics(
                    0.0, 0.0, 0, 1.0, 1.0, 0.0, 0.0, 0.0,
                ), fast_evaluated=1, strict_evaluated=1,
                finalists=(("0xaaa",),),
            )
            robust = scanner.offline_core_optimizer.RobustSelectionResult(
                selected=("0xaaa",), metrics=metrics, comparison=None,
                evaluated=1,
            )

            def select_after_watchlist(*args):
                self.assertIsNotNone(db.execute(
                    "SELECT 1 FROM watchlist WHERE addr='0xaaa'"
                ).fetchone())
                args[3](("0xaaa",))
                return staged

            strict_windows = {
                30: {
                    "copy_net_pnl": 100, "closed_n": 10, "open_fill_rate": .95,
                    "capacity_open_fit": .95, "max_drawdown": .01,
                    "maintenance_margin_coverage": 1.0,
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
                    patch.object(scanner, "form_quality_prefix", return_value=formation), \
                    patch.object(scanner.auto_tune, "_portfolio_window_fills",
                                 return_value={30: [{}], 14: [{}], 7: [{}]}), \
                    patch.object(scanner.auto_tune, "_candidate_windows", return_value=strict_windows), \
                    patch.object(scanner.auto_tune, "evaluate_portfolio_window",
                                 return_value={
                                     **strict_windows[30], "liquidations": 0,
                                     "price_path_coverage": 1.0,
                                     "initial_margin_equity": 1000,
                                     "weekly_stability": strict_weekly_stability(),
                                 }), \
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
                "copy_bt_net_pnl": 1800, "copy_bt_14d_net_pnl": 900, "copy_bt_7d_net_pnl": 600,
                "copy_bt_closed_n": 12, "copy_bt_14d_closed_n": 8, "copy_bt_7d_closed_n": 5,
                "copy_expected_return": .05, "copy_return_lcb": .01,
                "copy_positive_probability": .8, "copy_evidence_days": 8,
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
            )

            self.assertIsNone(marginal)
            self.assertEqual([(row.addr, row.role) for row in rows], [("0xaaa", "challenger")])

    def test_active_wallet_without_portfolio_replay_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at) "
                "VALUES ('g0','published',1,1,1,'old','old')"
            )
            db.execute(
                "INSERT INTO follow_selection(generation,addr,role,enabled,selected_at) "
                "VALUES('g0','0xold','core',1,'old')"
            )
            cols = storage.PROFILE_COLS.split(",")
            profile = {
                "addr": "0xaaa", "status": "active", "reason": "ok", "score": 0.95,
                "profile_generation": "g1", "data_status": "valid", "evidence_status": "qualified",
                "copy_bt_closed_n": 20, "copy_bt_14d_closed_n": 10, "copy_bt_7d_closed_n": 6,
                "copy_expected_return": 0.08, "copy_return_lcb": 0.02,
                "copy_positive_probability": 0.85, "copy_evidence_days": 10,
                "copy_recent_return_14d": 0.05, "copy_recent_return_7d": 0.04,
                "copy_risk_score": 0.9, "execution_score": 0.9,
                "actionable_open_rate": 0.9, "capacity_fit": 0.9,
                "copy_bt_net_pnl": 1800, "copy_bt_14d_net_pnl": 900,
                "copy_bt_7d_net_pnl": 600,
                "last_copyable_open_ms": 1000,
                "sector_policy_json": strict_policy_json(),
                "sector_copy_json": strict_sector_json(1800, 20, 900, 10, 600, 6),
            }
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [profile.get(col) for col in cols],
            )
            db.execute(
                "INSERT INTO watchlist(rank,addr,score,sector_policy_json,updated_at) "
                "VALUES(1,'0xaaa',0.71,'{\"allowed\":[\"crypto\"],\"crypto\":{\"allow\":true}}','now')"
            )
            db.commit()

            with self.assertRaisesRegex(
                    RuntimeError, "selection_portfolio_replay_unavailable"):
                scanner._build_explicit_selection(db, "g1", "2026-01-03", 1000)

    def test_path_validation_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at) "
                "VALUES ('g0','published',1,1,1,'old','old')"
            )
            db.execute(
                "INSERT INTO follow_selection(generation,addr,role,enabled,selected_at) "
                "VALUES('g0','0xold','core',1,'old')"
            )
            cols = storage.PROFILE_COLS.split(",")
            for addr, score in (("0xold", .8), ("0xnew", .9)):
                profile = {
                    "addr": addr, "status": "active", "score": score,
                    "profile_generation": "g1", "data_status": "valid",
                    "evidence_status": "qualified",
                    "copy_bt_closed_n": 20, "copy_bt_14d_closed_n": 10,
                    "copy_bt_7d_closed_n": 6, "copy_bt_net_pnl": 1800,
                    "copy_bt_14d_net_pnl": 900, "copy_bt_7d_net_pnl": 600,
                    "copy_expected_return": .05, "copy_return_lcb": .01,
                    "copy_positive_probability": .85, "copy_evidence_days": 10,
                    "actionable_open_rate": .9, "capacity_fit": .9,
                    "last_copyable_open_ms": 10_000,
                    "sector_policy_json": strict_policy_json(),
                    "sector_copy_json": strict_sector_json(1800, 20, 900, 10, 600, 6),
                }
                db.execute(
                    f"INSERT INTO profile ({storage.PROFILE_COLS}) "
                    f"VALUES ({','.join('?' for _ in cols)})",
                    [profile.get(col) for col in cols],
                )
                db.execute(
                    "INSERT INTO watchlist(rank,addr,score,sector_policy_json,updated_at) VALUES(?,?,?,?, 'now')",
                    (1 if addr == "0xnew" else 2, addr, score,
                     '{"allowed":["crypto"],"crypto":{"allow":true}}'),
                )
            db.commit()
            self.seal_market(db, "g1")
            marginal = scanner.selection.MarginalSelectionResult(
                selected=("0xnew",),
                baseline=scanner.selection.PortfolioMetrics(0, 0, 0, 1, 1, 0, 0, 0),
                metrics=scanner.selection.PortfolioMetrics(
                    100, 100, 0, 1, 1, .01, .1, .01,
                    net_pnl=100, drawdown_dollars=10, risk_adjusted_utility=90,
                ),
                action="replace", added=("0xnew",), removed=("0xold",),
            )
            fills = [
                {"user": addr, "coin": "BTC", "time": 1000, "tid": index,
                 "side": "B", "sz": "1", "startPosition": "0", "px": "100"}
                for index, addr in enumerate(("0xold", "0xnew"), 1)
            ]

            def evaluate(_db, _addrs, _sigmas, _follow, _now_ms, **kwargs):
                if kwargs.get("path_rows") is not None:
                    return {
                        "copy_net_pnl": -25, "maintenance_margin_coverage": .91,
                        "liquidations": 2, "ambiguous_liquidations": 1,
                        "price_path_boundary_skips": 3,
                    }
                return {
                    "copy_net_pnl": 100, "closed_n": 10, "open_fill_rate": .95,
                    "capacity_open_fit": .95, "max_drawdown": .01,
                }

            strict_window = {
                30: {
                    "copy_net_pnl": -25, "maintenance_margin_coverage": .91,
                    "liquidations": 2, "ambiguous_liquidations": 1,
                    "price_path_boundary_skips": 3, "closed_n": 10,
                    "open_fill_rate": .95, "capacity_open_fit": .95,
                    "max_drawdown": .01,
                },
            }

            def search(*_args, **kwargs):
                kwargs["validation_evaluator"](("0xnew",))
                return marginal

            with patch.object(scanner.auto_tune, "_portfolio_window_fills", return_value={30: fills}), \
                    patch.object(scanner.auto_tune, "evaluate_portfolio_window", side_effect=evaluate), \
                    patch.object(scanner.auto_tune, "_candidate_windows", return_value=strict_window), \
                    patch("hyper.market.price_path.coins_for_fills", return_value=["BTC"]), \
                    patch("hyper.market.price_path.load_refined", return_value=[{"coin": "BTC", "time": 1000}]), \
                    patch("hyper.market.price_path.coverage", return_value={
                        "coverage": .90, "expected": 100, "observed": 90,
                        "missingCoins": ["BTC"],
                    }):
                with self.assertRaisesRegex(
                        RuntimeError, "selection_price_path_invalid"):
                    scanner._build_explicit_selection(
                        db, "g1", "published-at", 10_000, audit_stamp="scan-start",
                    )

            self.assertEqual(scanner.selection.published_core_addrs(db), ["0xold"])

    def test_positive_portfolio_contribution_can_rescue_active_wallet_below_score_line(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            cols = storage.PROFILE_COLS.split(",")
            profile = {
                "addr": "0xaaa", "status": "active", "reason": "ok", "score": 0.8,
                "profile_generation": "g1", "data_status": "valid", "evidence_status": "qualified",
                "copy_bt_closed_n": 20, "copy_bt_14d_closed_n": 10, "copy_bt_7d_closed_n": 6,
                "copy_expected_return": 0.08, "copy_return_lcb": -0.04,
                "copy_positive_probability": 0.85, "copy_evidence_days": 10,
                "copy_recent_return_14d": 0.05, "copy_recent_return_7d": 0.04,
                "copy_risk_score": 0.5, "execution_score": 0.9,
                "actionable_open_rate": 0.9, "capacity_fit": 0.9,
                "copy_bt_net_pnl": 2500, "copy_bt_14d_net_pnl": 900,
                "copy_bt_7d_net_pnl": 600,
                "last_copyable_open_ms": 1000,
                "sector_policy_json": strict_policy_json(),
                "sector_copy_json": strict_sector_json(2500, 20, 900, 10, 600, 6),
            }
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [profile.get(col) for col in cols],
            )
            db.execute(
                "INSERT INTO watchlist(rank,addr,score,sector_policy_json,updated_at) "
                "VALUES(1,'0xaaa',0.65,'{\"allowed\":[\"crypto\"],\"crypto\":{\"allow\":true}}','now')"
            )
            db.commit()
            self.seal_market(db, "g1")
            baseline = scanner.selection.PortfolioMetrics(
                0, 0, 0, 1, 1, 0, 0, 0, net_pnl=0, drawdown_dollars=0,
                risk_adjusted_utility=0,
            )
            profitable = scanner.selection.PortfolioMetrics(
                3000, 3000, 2, .9, .9, .1, .5, .1,
                net_pnl=3000, drawdown_dollars=1000, risk_adjusted_utility=2000,
            )
            staged = scanner.offline_core_optimizer.OfflineSearchResult(
                selected=("0xaaa",), metrics=profitable, initial=(),
                initial_metrics=baseline, fast_evaluated=1, strict_evaluated=1,
                finalists=(("0xaaa",),),
            )
            robust = scanner.offline_core_optimizer.RobustSelectionResult(
                selected=("0xaaa",), metrics=profitable, comparison=None,
                evaluated=1,
            )
            with patch.object(scanner.auto_tune, "_portfolio_window_fills", return_value={30: [{}]}), \
                    patch.object(scanner.offline_core_optimizer, "optimize_membership",
                                 return_value=staged), \
                    patch.object(scanner.offline_core_optimizer, "choose_robust_candidate",
                                 return_value=robust), \
                    patch.object(scanner, "_portfolio_selection_metrics",
                                 side_effect=lambda _windows, baseline_n=0, selected_n=0:
                                 profitable if selected_n else baseline):
                rows, result = scanner._build_explicit_selection(
                    db, "g1", "now", 1000, validate_price_path=False,
                )

            self.assertEqual(result.selected, ())
            self.assertEqual(result.search_meta["desiredSelectedCount"], 1)
            self.assertEqual([(row.addr, row.role, row.reason) for row in rows], [
                ("0xaaa", "challenger", "core_promotion_confirmation_pending"),
            ])

    def test_daily_desired_portfolio_does_not_replace_existing_core_in_one_generation(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at) "
                "VALUES ('g0','published',1,1,1,'old','old')"
            )
            db.execute(
                "INSERT INTO follow_selection(generation,addr,role,enabled,selected_at) "
                "VALUES('g0','0xold','core',1,'old')"
            )
            db.execute(
                "INSERT INTO wallet_registry "
                "(addr,state,current_role,first_seen_at,last_seen_at,updated_at) "
                "VALUES('0xold','core','core','old','old','old')"
            )
            db.execute(
                "INSERT INTO follow_history "
                "(addr,first_followed_at,last_followed_at) "
                "VALUES('0xold','1970-01-01T00:00:00Z','1970-01-01T00:00:00Z')"
            )
            cols = storage.PROFILE_COLS.split(",")
            for rank, (addr, score) in enumerate((("0xnew", .9), ("0xold", .8)), 1):
                profile = {
                    "addr": addr, "status": "active", "reason": "ok", "score": score,
                    "profile_generation": "g1", "data_status": "valid",
                    "evidence_status": "qualified", "last_copyable_open_ms": 1000,
                    "copy_bt_closed_n": 20, "copy_bt_7d_closed_n": 9,
                    "copy_bt_14d_closed_n": 10, "copy_positive_probability": .8,
                    "copy_expected_return": .05, "copy_return_lcb": .01,
                    "copy_evidence_days": 10, "actionable_open_rate": .9, "capacity_fit": .9,
                    "copy_bt_net_pnl": 2500, "copy_bt_14d_net_pnl": 900,
                    "copy_bt_7d_net_pnl": 600,
                    "sector_policy_json": strict_policy_json(),
                    "sector_copy_json": strict_sector_json(2500, 20, 900, 10, 600, 9),
                }
                db.execute(
                    f"INSERT INTO profile ({storage.PROFILE_COLS}) "
                    f"VALUES ({','.join('?' for _ in cols)})",
                    [profile.get(col) for col in cols],
                )
                db.execute(
                    "INSERT INTO watchlist(rank,addr,score,sector_policy_json,updated_at) VALUES(?,?,?,?,'now')",
                    (rank, addr, score, '{"allowed":["crypto"],"crypto":{"allow":true}}'),
                )
            db.execute(
                "INSERT INTO copy_position(addr,coin,side,status,opened_at) "
                "VALUES('0xold','BTC','long','open','now')"
            )
            db.commit()
            self.seal_market(db, "g1")
            baseline = scanner.selection.PortfolioMetrics(
                100, 100, 0, .95, .95, .01, .5, .05,
                net_pnl=100, stress_net_pnl=100, drawdown_dollars=10,
                risk_adjusted_utility=100,
            )
            staged = scanner.offline_core_optimizer.OfflineSearchResult(
                selected=("0xnew",), metrics=baseline, initial=("0xold",),
                initial_metrics=baseline, fast_evaluated=2, strict_evaluated=2,
                finalists=(("0xnew",),),
            )
            robust = scanner.offline_core_optimizer.RobustSelectionResult(
                selected=("0xnew",), metrics=baseline, comparison=None, evaluated=1,
            )
            with patch.object(scanner.auto_tune, "_portfolio_window_fills", return_value={30: [{}]}), \
                    patch.object(scanner.offline_core_optimizer, "optimize_membership", return_value=staged), \
                    patch.object(scanner.offline_core_optimizer, "choose_robust_candidate", return_value=robust), \
                    patch.object(scanner, "_portfolio_selection_metrics", return_value=baseline):
                rows, result = scanner._build_explicit_selection(
                    db, "g1", "1970-01-01T00:00:01Z", 1000, validate_price_path=False,
                )

            by_addr = {row.addr: (row.role, row.reason) for row in rows}
            self.assertEqual(result.selected, ("0xold",))
            self.assertEqual(
                by_addr["0xold"],
                ("core", "core_quality_selected"),
            )
            self.assertEqual(
                by_addr["0xnew"],
                ("challenger", "core_promotion_confirmation_pending"),
            )

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

            def fake_profile(db_, addr, start_ms, now_ms, p, prior, lb, stamp, universe, force_full=False):
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
