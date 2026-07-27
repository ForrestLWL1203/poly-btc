import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hyper import params, storage
from hyper.discovery import perp_prefilter, scanner
from hyper.ops import scan_lock
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
                selection.SelectionRow("0xcore", "core", follow_score=.8),
                selection.SelectionRow("0xchallenge", "challenger", follow_score=.7),
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

    def test_refresh_promotes_challenger_without_harvest_bootstrap_or_retune(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            passed = perp_prefilter.Result("passed", "passed", {})

            def profile(_db, addr, *_args, **kwargs):
                self.assertFalse(kwargs["force_full"])
                return "active", "source_quality_passed", {
                    "data_status": "valid", "evidence_status": "source_qualified",
                }, False

            formation = {
                "selected": ("0xchallenge",),
                "params": {},
                "qualifications": {}, "scores": {}, "policies": {},
                "walletMetrics": {}, "scoreDetails": {},
                "replayParamsHash": "fixed",
                "search": {"retuned": False, "robustAllowedMemberships": [["0xchallenge"]]},
            }
            rows = [
                selection.SelectionRow("0xchallenge", "core", follow_score=.9),
                selection.SelectionRow("0xcore", "challenger", follow_score=.8),
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
                    scanner, "_selection_prefetch_candidates", return_value=[],
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
                    return_value={"0xchallenge"},
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
        self.assertEqual(result["coreRemoved"], 1)
        self.assertEqual(current[1], "challenger_daily")
        self.assertEqual(selected, [
            ("0xcore", "challenger"), ("0xchallenge", "core")
        ])
        self.assertEqual(run, ("challenger_refresh", 1, 0))
        self.assertEqual(profile_one.call_count, 2)
        self.assertFalse(form.call_args.kwargs["retune"])

    def test_current_core_data_error_aborts_and_retains_generation(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            passed = perp_prefilter.Result("passed", "passed", {})

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
                                 return_value={"0xcore": passed, "0xchallenge": passed}), \
                    patch.object(scanner, "_incomplete_fill_cache_addrs", return_value=[]), \
                    patch.object(scanner, "_profile_one", side_effect=profile):
                with self.assertRaisesRegex(RuntimeError, "core_data_incomplete"):
                    scanner.refresh_challengers(db, self.ns())

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
                    scanner, "_selection_prefetch_candidates", return_value=[],
                ))
                stack.enter_context(patch.object(
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


if __name__ == "__main__":
    unittest.main()
