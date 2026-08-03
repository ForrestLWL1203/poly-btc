import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hyper import params, storage
from hyper.discovery import perp_prefilter, scanner
from hyper.ops import scan_lock
from hyper.selection import pre_strict
from hyper.selection import state as selection


class ChallengerRefreshTests(unittest.TestCase):
    def test_scan_lock_rejects_overlapping_runs(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "hl.db")
            with scan_lock.acquire(db_path):
                with self.assertRaises(scan_lock.ScanBusyError):
                    with scan_lock.acquire(db_path):
                        pass

    def open_db(self, td):
        db = storage.connect(
            str(Path(td) / "hl.db"),
            storage.DISCOVERY_SCHEMA,
            storage.OBSERVE_SCHEMA,
        )
        params.seed_params(db)
        db.execute(
            "INSERT INTO scan_generation "
            "(generation,source,status,started_at,leaderboard_rows,leaderboard_unique_rows,"
            "leaderboard_complete_rows,leaderboard_completeness,leaderboard_valid,profile_complete,"
            "publishable,complete,is_current,published_at) "
            "VALUES ('g-full','scan','published','start',1,1,1,1,1,1,1,1,1,'start')"
        )
        db.execute(
            "INSERT INTO leaderboard "
            "(addr,account_value,day_pnl,day_roi,day_vlm,week_pnl,week_roi,week_vlm,"
            "mon_pnl,mon_roi,mon_vlm,all_pnl,all_roi,all_vlm,daily_turnover,is_candidate,"
            "fetched_at,generation) VALUES "
            "('0xcore',50000,1,.01,1,1,.01,200000,1,.1,1,1,.1,1,1,1,'start','g-full')"
        )
        db.executemany(
            "INSERT INTO profile(addr,status,reason,score,profile_generation,data_status) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("0xcore", "active", "rough_copy_qualified", .8, "g-full", "valid"),
                ("0xchallenge", "active", "source_activity_stale", .7, "g-full", "valid"),
            ],
        )
        selection.replace_selection_rows(
            db, "g-full",
            [
                selection.SelectionRow(
                    "0xcore", "core", follow_score=.8,
                    model_version=pre_strict.SELECTION_MODEL_VERSION,
                    policy_version=pre_strict.POLICY_VERSION,
                ),
                selection.SelectionRow(
                    "0xchallenge", "challenger", follow_score=.7,
                    model_version=pre_strict.SELECTION_MODEL_VERSION,
                    policy_version=pre_strict.POLICY_VERSION,
                ),
            ],
            selected_at="start",
        )
        db.commit()
        return db

    @staticmethod
    def ns():
        return SimpleNamespace(
            days=14, max_pages=5, workers=1, scan_interval=0,
            full_scan=False, no_harvest=True, rebuild_sector_policy=True,
            min_acct=20_000, week_vlm_min=150_000, week_pnl_min=0,
            month_pnl_min=0, all_pnl_min=0, perp_pnl_share_min=.6,
            min_perp=.6, inactive_days=3, exclude_hft=True,
            hft_min_hold_min=3, max_single_adds=30,
        )

    def test_pool_stays_anchored_to_latest_full_generation(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute("UPDATE scan_generation SET is_current=0 WHERE generation='g-full'")
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,source,status,started_at,complete,is_current,published_at) "
                "VALUES ('g-daily','challenger_daily','published','later',1,1,'later')"
            )
            selection.replace_selection_rows(
                db, "g-daily",
                [selection.SelectionRow("0xchallenge", "core")],
                selected_at="later",
            )
            db.commit()

            base, pool = scanner.challenger_refresh_pool(db)

        self.assertEqual(base, "g-full")
        self.assertEqual(pool, ["0xchallenge", "0xcore"])

    def test_live_refresh_pool_ignores_inactive_paper_positions(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO copy_position (addr,coin,side,status,opened_at) "
                "VALUES ('0xpaper-held','BTC','long','open','now')"
            )
            db.execute(
                "INSERT INTO live_copy_position (addr,coin,side,status,opened_at) "
                "VALUES ('0xlive-held','ETH','short','open','now')"
            )
            db.execute(
                "INSERT INTO execution_control (id,selected_mode,state,updated_at) "
                "VALUES (1,'live','live_ready','now') ON CONFLICT(id) DO UPDATE SET "
                "selected_mode='live',state='live_ready',updated_at='now'"
            )
            db.commit()

            base, pool = scanner.challenger_refresh_pool(db)

        self.assertEqual(base, "g-full")
        self.assertIn("0xlive-held", pool)
        self.assertNotIn("0xpaper-held", pool)

    def test_daily_skips_a_legacy_full_generation(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "UPDATE follow_selection SET model_version='legacy',policy_version='legacy' "
                "WHERE generation='g-full'"
            )
            db.commit()

            result = scanner.refresh_challengers(db, self.ns())

        self.assertEqual(result, {
            "status": "skipped", "reason": "legacy_generation_policy_mismatch",
        })

    def test_daily_queue_cannot_admit_wallet_outside_full_strict_roles(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.executemany(
                "INSERT INTO pre_strict_evidence "
                "(generation,addr,policy_version,model_version,status,tier,"
                "rough_profit_priority,rough_return_30d,rough_return_7d,"
                "copy_profit_factor_30d,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "g-daily", "0xchallenge", pre_strict.POLICY_VERSION,
                        pre_strict.SELECTION_MODEL_VERSION, "passed", "primary",
                        .50, .50, .50, 2.0, "now",
                    ),
                    (
                        "g-daily", "0xheld-only", pre_strict.POLICY_VERSION,
                        pre_strict.SELECTION_MODEL_VERSION, "passed", "primary",
                        .90, .90, .90, 5.0, "now",
                    ),
                ],
            )
            queued = scanner._finalize_pre_strict_queue(
                db, "g-daily", allowed_addrs={"0xcore", "0xchallenge"},
            )
            ranks = dict(db.execute(
                "SELECT addr,queue_rank FROM pre_strict_evidence "
                "WHERE generation='g-daily'"
            ).fetchall())

        self.assertEqual(queued, ["0xchallenge"])
        self.assertEqual(ranks["0xchallenge"], 1)
        self.assertIsNone(ranks["0xheld-only"])

    def test_refresh_promotes_challenger_without_harvest_or_bootstrap_and_retunes(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            passed = perp_prefilter.Result("passed", "passed", {})

            def profile(_db, addr, *_args, **kwargs):
                self.assertFalse(kwargs["force_full"])
                return "active", "source_quality_passed", {
                    "data_status": "valid", "evidence_status": "source_qualified",
                }, False

            formation = {
                "selected": ("0xchallenge", "0xcore"),
                "params": {},
                "qualifications": {}, "scores": {}, "policies": {},
                "walletMetrics": {}, "scoreDetails": {},
                "replayParamsHash": "fixed",
                "search": {
                    "retuned": True,
                    "robustAllowedMemberships": [["0xchallenge", "0xcore"]],
                    "tunePoolCount": 2,
                    "formationTuneEligible": True,
                    "formationTuneReason": "proposal_selected",
                },
            }
            rows = [
                selection.SelectionRow("0xchallenge", "core", follow_score=.9),
                selection.SelectionRow("0xcore", "core", follow_score=.8),
            ]
            marginal = SimpleNamespace(search_meta={})
            resolver = SimpleNamespace()
            with ExitStack() as stack:
                stack.enter_context(patch.object(
                    scanner.rest, "get_leaderboard",
                    side_effect=AssertionError("leaderboard forbidden"),
                ))
                stack.enter_context(patch.object(
                    scanner.rest, "copyable_universe", return_value={"BTC"},
                ))
                stack.enter_context(patch.object(
                    scanner.generation_market, "fetch_context_snapshot", return_value={},
                ))
                stack.enter_context(patch.object(
                    scanner.generation_market, "Resolver", return_value=resolver,
                ))
                stack.enter_context(patch.object(
                    scanner, "_run_perp_prefilter",
                    return_value={"0xcore": passed, "0xchallenge": passed},
                ))
                stack.enter_context(patch.object(
                    scanner, "_incomplete_fill_cache_addrs", return_value=[],
                ))
                profile_one = stack.enter_context(patch.object(
                    scanner, "_profile_one", side_effect=profile,
                ))
                stack.enter_context(patch.object(
                    scanner, "_source_quality_pool",
                    return_value=(["0xcore", "0xchallenge"], []),
                ))
                stack.enter_context(patch.object(
                    scanner, "_rough_replay_source_pool",
                    return_value={"qualified": ["0xchallenge"], "failed": []},
                ))
                stack.enter_context(patch.object(
                    scanner.generation_market, "seal", return_value={"sealed": True},
                ))
                stack.enter_context(patch.object(
                    scanner, "_assert_scoped_fill_cache",
                    return_value={"audited": 2, "invalid": 0},
                ))
                stack.enter_context(patch.object(
                    scanner.pipeline_audit, "record_profile_snapshot",
                ))
                stack.enter_context(patch.object(
                    scanner, "refresh_watchlist", return_value=2,
                ))
                stack.enter_context(patch.object(
                    scanner, "_prefetch_selection_paths", return_value={"status": "ok"},
                ))
                stack.enter_context(patch.object(
                    scanner, "_assert_daily_promotion_parity",
                    return_value={"checked": 1, "passed": 1},
                ))
                form = stack.enter_context(patch.object(
                    scanner, "form_quality_prefix", return_value=formation,
                ))
                stack.enter_context(patch.object(
                    scanner, "_apply_formation_params", return_value=False,
                ))
                build_selection = stack.enter_context(patch.object(
                    scanner, "_build_explicit_selection",
                    return_value=(rows, marginal),
                ))
                stack.enter_context(patch.object(
                    scanner, "_selection_market_snapshot_validation",
                    return_value={"status": "ok"},
                ))
                stack.enter_context(patch.object(
                    scanner, "_record_explicit_follow_history",
                    return_value={"0xchallenge", "0xcore"},
                ))
                stack.enter_context(patch.object(
                    scanner.strategy_revision, "create_revision",
                    return_value={"revision": 2, "source": "challenger_daily"},
                ))
                stack.enter_context(patch.object(
                    scanner, "_store_final_copy_summary",
                    return_value=({"status": "ok"}, {"status": "ok"}),
                ))
                stack.enter_context(patch.object(
                    scanner.auto_tune, "bind_active_tune_rollback_core",
                ))
                result = scanner.refresh_challengers(db, self.ns())

            current = db.execute(
                "SELECT generation,source FROM scan_generation WHERE is_current=1"
            ).fetchone()
            selected = db.execute(
                "SELECT addr,role FROM follow_selection WHERE generation=? ORDER BY role,addr",
                (current[0],),
            ).fetchall()
            run = db.execute(
                "SELECT kind,complete,full FROM scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()

        self.assertEqual(result["status"], "published")
        self.assertEqual(result["coreAdded"], 1)
        self.assertEqual(result["coreRemoved"], 0)
        self.assertEqual(current[1], "challenger_daily")
        self.assertEqual(selected, [
            ("0xchallenge", "core"), ("0xcore", "core")
        ])
        self.assertEqual(run, ("challenger_refresh", 1, 0))
        self.assertEqual(profile_one.call_count, 2)
        self.assertEqual(form.call_count, 2)
        self.assertFalse(form.call_args_list[0].kwargs["retune"])
        self.assertTrue(form.call_args_list[1].kwargs["retune"])
        self.assertTrue(form.call_args_list[1].kwargs["force_retune"])
        self.assertTrue(
            build_selection.call_args.kwargs[
                "formation_meta"
            ]["retentionHysteresis"]
        )

    def test_daily_replacement_proposal_fills_an_open_seat_without_removing_incumbent(self):
        previous = [
            selection.SelectionRow(
                "0xcore", "core", follow_score=.8, selection_rank=1,
                sector_policy_json='{"allowed":["crypto"]}',
            ),
            selection.SelectionRow("0xoldchallenge", "challenger", follow_score=.7),
        ]
        proposed = [
            selection.SelectionRow(
                "0xchallenge", "core", follow_score=.9, selection_rank=1,
                sector_policy_json='{"allowed":["crypto"]}',
            ),
            selection.SelectionRow("0xcore", "challenger", follow_score=.8),
        ]

        decision = scanner._challenger_daily_membership_decision(
            ("0xcore",), ("0xchallenge",),
        )
        carried = scanner._carry_challenger_daily_core_rows(
            previous, proposed, ("0xcore",),
        )
        roles = {row.addr: row.role for row in carried}
        reasons = {row.addr: row.reason for row in carried}

        self.assertEqual(decision["mode"], "promote")
        self.assertEqual(decision["selected"], ("0xcore", "0xchallenge"))
        self.assertEqual(decision["removed"], ("0xcore",))
        self.assertEqual(decision["added"], ("0xchallenge",))
        self.assertEqual(roles["0xcore"], "core")
        self.assertEqual(roles["0xchallenge"], "challenger")
        self.assertEqual(
            reasons["0xcore"], "challenger_daily_core_carried",
        )
        self.assertEqual(
            reasons["0xchallenge"], "challenger_daily_promotion_not_published",
        )

    def test_daily_same_membership_preserves_incumbent_order(self):
        decision = scanner._challenger_daily_membership_decision(
            ("0xcoreb", "0xcorea"),
            ("0xcorea", "0xcoreb"),
        )

        self.assertEqual(decision["mode"], "refresh")
        self.assertEqual(decision["selected"], ("0xcoreb", "0xcorea"))

    def test_daily_full_core_never_auto_replaces_incumbent(self):
        previous = tuple(f"0xcore{i:02d}" for i in range(16))
        decision = scanner._challenger_daily_membership_decision(
            previous,
            ("0xnew",) + previous[1:],
        )
        self.assertEqual("carry", decision["mode"])
        self.assertEqual(previous, decision["selected"])
        self.assertEqual((), decision["added"])

    def test_recent_self_liquidation_and_zero_equity_is_hard_safety_exit(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            now_ms = 1_000_000
            db.execute(
                "INSERT INTO candidate_fills(addr,tid,time,fill_json) VALUES (?,?,?,?)",
                (
                    "0xcore", 1, now_ms - 1_000,
                    json.dumps({
                        "coin": "xyz:SKHX",
                        "liquidation": {"liquidatedUser": "0xcore"},
                    }),
                ),
            )
            zero = {
                "marginSummary": {"accountValue": "0"},
                "assetPositions": [],
            }
            with patch.object(
                scanner.rest, "clearinghouse_state", return_value=zero,
            ) as clearinghouse:
                result = scanner._verified_zero_equity_source_liquidations(
                    db, ["0xcore"], now_ms,
                )

        self.assertIn("0xcore", result)
        self.assertEqual(result["0xcore"]["coins"], ["xyz:SKHX"])
        self.assertEqual(result["0xcore"]["liquidationFills"], 1)
        self.assertEqual(clearinghouse.call_count, 2)

    def test_liquidation_with_remaining_equity_is_not_zero_equity_exit(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            now_ms = 1_000_000
            db.execute(
                "INSERT INTO candidate_fills(addr,tid,time,fill_json) VALUES (?,?,?,?)",
                (
                    "0xcore", 1, now_ms - 1_000,
                    json.dumps({
                        "coin": "BTC",
                        "liquidation": {"liquidatedUser": "0xcore"},
                    }),
                ),
            )
            funded = {
                "marginSummary": {"accountValue": "2500"},
                "assetPositions": [],
            }
            with patch.object(
                scanner.rest, "clearinghouse_state", return_value=funded,
            ):
                result = scanner._verified_zero_equity_source_liquidations(
                    db, ["0xcore"], now_ms,
                )

        self.assertEqual(result, {})

    def test_severe_copy_liquidation_wallet_is_not_carried_with_promotion_floor(self):
        previous = [
            selection.SelectionRow("0xkeep", "core", selection_rank=1),
            selection.SelectionRow("0xblown", "core", selection_rank=2),
        ]
        proposed = [
            selection.SelectionRow("0xkeep", "challenger", selection_rank=3),
            selection.SelectionRow(
                "0xblown", "exit_only",
                reason="copy_single_liquidation_loss_over_8pct:exit_pending",
            ),
            selection.SelectionRow("0xnew", "core", selection_rank=1),
        ]

        carried = scanner._carry_challenger_daily_core_rows(
            previous, proposed, ("0xkeep",),
        )
        roles = {row.addr: row.role for row in carried}

        self.assertEqual(roles["0xkeep"], "core")
        self.assertEqual(roles["0xblown"], "exit_only")
        self.assertEqual(roles["0xnew"], "challenger")

    def test_current_core_delta_error_after_zero_equity_gate_aborts_and_retains_generation(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            passed = perp_prefilter.Result("passed", "passed", {})
            zero_equity_gate = perp_prefilter.Result(
                "deferred_data_error", "zero_start_equity", {},
            )

            def profile(_db, addr, *_args, **_kwargs):
                if addr == "0xcore":
                    return "active", "fills_error", {
                        "data_status": "deferred_data_error",
                        "evidence_status": "invalid",
                    }, False
                return "active", "source_quality_passed", {
                    "data_status": "valid",
                }, False

            with patch.object(scanner.rest, "copyable_universe", return_value={"BTC"}), \
                    patch.object(scanner.generation_market, "fetch_context_snapshot", return_value={}), \
                    patch.object(scanner.generation_market, "Resolver", return_value=SimpleNamespace()), \
                    patch.object(scanner, "_run_perp_prefilter",
                                 return_value={
                                     "0xcore": zero_equity_gate,
                                     "0xchallenge": passed,
                                 }), \
                    patch.object(scanner, "_incomplete_fill_cache_addrs", return_value=[]), \
                    patch.object(scanner, "_profile_one", side_effect=profile) as profile_one:
                with self.assertRaisesRegex(RuntimeError, "core_data_incomplete"):
                    scanner.refresh_challengers(db, self.ns())
                self.assertEqual(profile_one.call_count, 1)

            current = db.execute(
                "SELECT generation FROM scan_generation WHERE is_current=1"
            ).fetchone()[0]
            failed = db.execute(
                "SELECT status FROM scan_generation WHERE source='challenger_daily' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            run = db.execute(
                "SELECT complete,kind FROM scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()

        self.assertEqual(current, "g-full")
        self.assertEqual(failed, "failed")
        self.assertEqual(run, (0, "challenger_refresh"))

    def test_challenger_profile_error_is_deferred_and_remains_in_next_pool(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            passed = perp_prefilter.Result("passed", "passed", {})

            def profile(_db, addr, *_args, **_kwargs):
                if addr == "0xchallenge":
                    raise TimeoutError("temporary")
                return "active", "source_quality_passed", {
                    "data_status": "valid", "evidence_status": "source_qualified",
                }, False

            formation = {
                "selected": ("0xcore",), "params": {},
                "qualifications": {}, "scores": {}, "policies": {},
                "walletMetrics": {}, "scoreDetails": {},
                "replayParamsHash": "fixed",
                "search": {"retuned": False, "robustAllowedMemberships": [["0xcore"]]},
            }
            rows = [selection.SelectionRow("0xcore", "core", follow_score=.8)]
            marginal = SimpleNamespace(search_meta={})
            with ExitStack() as stack:
                stack.enter_context(patch.object(
                    scanner.rest, "copyable_universe", return_value={"BTC"},
                ))
                stack.enter_context(patch.object(
                    scanner.generation_market, "fetch_context_snapshot", return_value={},
                ))
                stack.enter_context(patch.object(
                    scanner.generation_market, "Resolver", return_value=SimpleNamespace(),
                ))
                stack.enter_context(patch.object(
                    scanner, "_run_perp_prefilter",
                    return_value={"0xcore": passed, "0xchallenge": passed},
                ))
                stack.enter_context(patch.object(
                    scanner, "_incomplete_fill_cache_addrs", return_value=[],
                ))
                stack.enter_context(patch.object(
                    scanner, "_profile_one", side_effect=profile,
                ))
                stack.enter_context(patch.object(
                    scanner, "_source_quality_pool", return_value=(["0xcore"], []),
                ))
                stack.enter_context(patch.object(
                    scanner, "_rough_replay_source_pool",
                    return_value={"qualified": ["0xcore"], "failed": []},
                ))
                stack.enter_context(patch.object(
                    scanner.generation_market, "seal", return_value={"sealed": True},
                ))
                stack.enter_context(patch.object(
                    scanner, "_assert_scoped_fill_cache",
                    return_value={"audited": 1, "invalid": 0},
                ))
                stack.enter_context(patch.object(
                    scanner.pipeline_audit, "record_profile_snapshot",
                ))
                stack.enter_context(patch.object(
                    scanner, "refresh_watchlist", return_value=1,
                ))
                stack.enter_context(patch.object(
                    scanner, "_prefetch_selection_paths", return_value={"status": "ok"},
                ))
                form = stack.enter_context(patch.object(
                    scanner, "form_quality_prefix", return_value=formation,
                ))
                stack.enter_context(patch.object(
                    scanner, "_apply_formation_params", return_value=False,
                ))
                stack.enter_context(patch.object(
                    scanner, "_build_explicit_selection",
                    return_value=(rows, marginal),
                ))
                stack.enter_context(patch.object(
                    scanner, "_selection_market_snapshot_validation",
                    return_value={"status": "ok"},
                ))
                stack.enter_context(patch.object(
                    scanner, "_record_explicit_follow_history",
                    return_value={"0xcore"},
                ))
                stack.enter_context(patch.object(
                    scanner.strategy_revision, "create_revision",
                    return_value={"revision": 2, "source": "challenger_daily"},
                ))
                stack.enter_context(patch.object(
                    scanner, "_store_final_copy_summary",
                    return_value=({"status": "ok"}, {"status": "ok"}),
                ))
                stack.enter_context(patch.object(
                    scanner.auto_tune, "bind_active_tune_rollback_core",
                ))
                result = scanner.refresh_challengers(db, self.ns())

            deferred = db.execute(
                "SELECT data_status FROM profile WHERE addr='0xchallenge'"
            ).fetchone()[0]
            _base, next_pool = scanner.challenger_refresh_pool(db)

        self.assertEqual(result["status"], "published")
        self.assertEqual(deferred, "deferred_data_error")
        self.assertIn("0xchallenge", next_pool)
        self.assertEqual(form.call_count, 1)
        self.assertFalse(form.call_args.kwargs["retune"])


if __name__ == "__main__":
    unittest.main()
