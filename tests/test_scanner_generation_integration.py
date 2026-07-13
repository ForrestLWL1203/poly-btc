import tempfile
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hl import params, scanner, storage


def leaderboard_row(addr="0xaaa"):
    return {
        "ethAddress": addr,
        "accountValue": "100000",
        "windowPerformances": [
            ("day", {"pnl": "100", "roi": "0.001", "vlm": "1000000"}),
            ("week", {"pnl": "300000", "roi": "0.10", "vlm": "30000000"}),
            ("month", {"pnl": "500000", "roi": "0.20", "vlm": "90000000"}),
            ("allTime", {"pnl": "900000", "roi": "0.30", "vlm": "180000000"}),
        ],
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
    def test_selection_uses_effective_params_not_historical_tune_baseline(self):
        source = inspect.getsource(scanner._build_explicit_selection)

        self.assertNotIn("resolve_tune_baseline", source)
        self.assertNotIn("resolve_add_baseline", source)

    def test_path_validation_is_portfolio_fail_closed_not_wallet_regate(self):
        source = inspect.getsource(scanner._build_explicit_selection)

        self.assertIn("if path_net <= 0", source)
        self.assertNotIn("path_rejected", source)

    def open_db(self, td):
        return storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)

    def test_selection_replay_uses_current_params_without_mutating_profile(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            db.execute(
                "INSERT INTO scan_generation(generation,status,complete,publishable,is_current,started_at) "
                "VALUES('g1','published',1,1,1,'now')"
            )
            db.execute(
                "INSERT INTO follow_selection(generation,addr,role,enabled,selected_at) "
                "VALUES('g1','0xaaa','core',1,'now')"
            )
            db.execute(
                "INSERT INTO profile(addr,status,copy_bt_net_pnl,copy_bt_closed_n) "
                "VALUES('0xaaa','active',100,7)"
            )
            db.commit()
            windows = {
                30: {"copy_net_pnl": 250, "copy_win_rate": 0.75, "closed_n": 8,
                     "opened_n": 9, "target_open_events": 10, "liquidations": 0, "fee_drag": 12},
                14: {"copy_net_pnl": 180, "closed_n": 6},
                7: {"copy_net_pnl": 90, "closed_n": 3},
            }
            sectors = {"crypto": windows, "stock": {}}
            with patch.object(scanner, "_copy_bt_cached_fills", return_value=[{"time": 1}]), \
                    patch.object(scanner, "_copy_bt_results", return_value=windows), \
                    patch.object(scanner, "_sector_copy_bt_results", return_value=sectors), \
                    patch.object(scanner, "_copy_bt_overrides", return_value={"MID_MARGIN_PCT": 0.05}), \
                    patch.object(scanner, "_copy_bt_sigmas", return_value={}), \
                    patch.object(scanner, "_copy_bt_market_ctx", return_value={}):
                result = scanner.refresh_selection_copy_replay(db, "g1", replayed_at="later")

            replay = db.execute(
                "SELECT replay_copy_bt_net_pnl,replay_copy_bt_closed_n,replay_copy_bt_7d_net_pnl,"
                "replay_params_hash,replayed_at FROM follow_selection WHERE generation='g1' AND addr='0xaaa'"
            ).fetchone()
            profile = db.execute(
                "SELECT copy_bt_net_pnl,copy_bt_closed_n FROM profile WHERE addr='0xaaa'"
            ).fetchone()
            self.assertEqual(result["refreshed"], 1)
            self.assertEqual(replay[:3], (250, 8, 90))
            self.assertTrue(replay[3])
            self.assertEqual(replay[4], "later")
            self.assertEqual(profile, (100, 7))

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
                    patch.object(scanner.rest, "get_leaderboard", return_value=[]), \
                    patch.object(scanner, "_launch_async_tuner") as launch, \
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
            launch.assert_not_called()
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
                    patch.object(scanner.rest, "get_leaderboard", return_value=[leaderboard_row()]), \
                    patch.object(scanner, "_profile_one", side_effect=fake_profile), \
                    patch.object(scanner, "now_iso", side_effect=scan_time), \
                    patch.object(scanner.generation, "now_iso", return_value="2026-01-01T00:01:00Z"), \
                    patch.object(scanner, "_launch_async_tuner", return_value={"status": "launched"}) as launch, \
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
            self.assertEqual(selection_row, (current[0], "0xaaa", "challenger", "valid", "missing"))
            self.assertEqual(db.execute("SELECT DISTINCT generation FROM leaderboard").fetchone()[0], current[0])
            launch.assert_called_once()

    def test_cold_paper_bootstrap_can_publish_first_qualified_core(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)

            def fake_profile(db_, addr, start_ms, now_ms, p, prior, lb, stamp, universe, force_full=False):
                row = {
                    "addr": addr, "status": "active", "reason": "ok", "score": 0.9,
                    "raw_quality_score": 0.9, "profile_generation": p.scan_generation,
                    "evaluated_at": stamp, "last_refreshed": stamp, "data_status": "valid",
                    "evidence_status": "qualified", "last_copyable_open_ms": now_ms,
                    "copy_bt_closed_n": 12, "copy_bt_14d_closed_n": 10, "copy_bt_7d_closed_n": 8,
                    "copy_positive_probability": 0.85, "copy_expected_return": 0.05,
                    "copy_return_lcb": 0.015, "copy_return_volatility": 0.08,
                    "copy_evidence_days": 10, "copy_recent_return_14d": 0.04,
                    "copy_recent_return_7d": 0.03, "copy_risk_score": 0.85,
                    "execution_score": 0.95, "open_probability_48h": 0.8,
                    "copy_bt_open_fill_rate": 0.95, "actionable_open_rate": 0.95,
                    "capacity_fit": 0.95, "copy_bt_net_pnl": 800,
                    "copy_bt_14d_net_pnl": 400, "copy_bt_7d_net_pnl": 200,
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
            marginal = scanner.selection.MarginalSelectionResult(
                selected=("0xaaa",), baseline=scanner.selection.PortfolioMetrics(
                    0.0, 0.0, 0, 1.0, 1.0, 0.0, 0.0, 0.0,
                ), metrics=metrics, action="add", added=("0xaaa",), evaluated=1,
            )

            def select_after_watchlist(*args, **kwargs):
                self.assertIsNotNone(db.execute(
                    "SELECT 1 FROM watchlist WHERE addr='0xaaa'"
                ).fetchone())
                return marginal

            with patch.object(scanner.rest, "copyable_universe", return_value={"BTC"}), \
                    patch.object(scanner.rest, "get_leaderboard", return_value=[leaderboard_row()]), \
                    patch.object(scanner, "_profile_one", side_effect=fake_profile), \
                    patch.object(scanner.auto_tune, "_portfolio_window_fills",
                                 return_value={30: [{}], 14: [{}], 7: [{}]}), \
                    patch.object(scanner.selection, "search_smart_core", side_effect=select_after_watchlist), \
                    patch.object(scanner, "_launch_async_tuner", return_value={"status": "launched"}), \
                    patch.object(scanner, "_prune_discovery_cache", return_value={}):
                scanner.scan(db, scan_args())

            current = db.execute(
                "SELECT generation FROM scan_generation WHERE is_current=1 AND status='published'"
            ).fetchone()[0]
            row = db.execute(
                "SELECT addr,role FROM follow_selection WHERE generation=?", (current,)
            ).fetchone()
            self.assertEqual(row, ("0xaaa", "core"))

    def test_systemd_scan_launches_tuner_in_independent_transient_unit(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
            completed = SimpleNamespace(returncode=0, stderr="")

            with patch.dict(scanner.os.environ, {"INVOCATION_ID": "scan-service"}), \
                    patch.object(scanner.shutil, "which", return_value="/usr/bin/systemd-run"), \
                    patch.object(scanner.subprocess, "run", return_value=completed) as run:
                result = scanner._launch_async_tuner(db, "generation-1", "2026-01-02T00:00:00Z")

            command = run.call_args.args[0]
            self.assertEqual(result["status"], "launched")
            self.assertTrue(result["unit"].startswith("hl-tune-"))
            self.assertIn("--property=MemoryMax=512M", command)
            self.assertIn("--generation", command)
            self.assertIn("generation-1", command)

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
            core_row = scanner.selection.SelectionRow(
                "0xaaa", "core", reason="core_entry", data_status="valid", evidence_status="qualified",
            )
            marginal = scanner.selection.MarginalSelectionResult(
                selected=("0xaaa",),
                baseline=scanner.selection.PortfolioMetrics(0, 0, 0, 1, 1, 0, 0, 0),
                metrics=scanner.selection.PortfolioMetrics(10, 5, 0, 1, 1, .005, .1, .1),
                action="bootstrap", added=("0xaaa",),
            )

            with patch.object(scanner, "_build_explicit_selection", return_value=([core_row], marginal)) as build, \
                    patch.object(scanner, "_launch_async_tuner", return_value={"status": "launched"}) as launch:
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
            launch.assert_called_once()

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
                "sector_policy_json": '{"allowed":["crypto"],"crypto":{"allow":true}}',
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
            core_row = scanner.selection.SelectionRow("0xaaa", "core", reason="core_keep")

            def build(db_arg, generation, stamp, now_ms, **kwargs):
                self.assertIsNotNone(db_arg.execute(
                    "SELECT 1 FROM watchlist WHERE addr='0xaaa'"
                ).fetchone())
                self.assertFalse(kwargs["force_cold_bootstrap"])
                return [core_row], None

            with patch.object(scanner, "_build_explicit_selection", side_effect=build), \
                    patch.object(scanner, "_launch_async_tuner", return_value={"status": "launched"}):
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

            def decide(evidences, _policy):
                evidence = list(evidences)
                self.assertEqual(evidence[0].current_role, "challenger")
                self.assertEqual(evidence[0].consecutive_complete_good, 0)
                return [scanner.selection.LifecycleDecision(
                    "0xaaa", "challenger", "challenger", "challenger_evidence",
                )]

            with patch.object(scanner.selection, "decide_lifecycles", side_effect=decide):
                rows, marginal = scanner._build_explicit_selection(
                    db, "g1", "2026-01-03", 1000, force_cold_bootstrap=True,
                )

            self.assertIsNone(marginal)
            self.assertEqual([(row.addr, row.role) for row in rows], [("0xaaa", "challenger")])

    def test_active_wallet_without_portfolio_replay_stays_challenger(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            params.seed_params(db)
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
            }
            db.execute(
                f"INSERT INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' for _ in cols)})",
                [profile.get(col) for col in cols],
            )
            db.execute(
                "INSERT INTO watchlist(rank,addr,score,updated_at) VALUES(1,'0xaaa',0.71,'now')"
            )
            db.commit()

            rows, marginal = scanner._build_explicit_selection(db, "g1", "2026-01-03", 1000)

            self.assertIsNone(marginal)
            self.assertEqual([(row.addr, row.role, row.reason) for row in rows], [
                ("0xaaa", "challenger", "portfolio_replay_unavailable"),
            ])
            self.assertEqual(rows[0].utility, 0.71)

    def test_path_validation_failure_records_metrics_and_explicit_fallback_reason(self):
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
                }
                db.execute(
                    f"INSERT INTO profile ({storage.PROFILE_COLS}) "
                    f"VALUES ({','.join('?' for _ in cols)})",
                    [profile.get(col) for col in cols],
                )
                db.execute(
                    "INSERT INTO watchlist(rank,addr,score,updated_at) VALUES(?,?,?,'now')",
                    (1 if addr == "0xnew" else 2, addr, score),
                )
            db.commit()
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

            with patch.object(scanner.auto_tune, "_portfolio_window_fills", return_value={30: fills}), \
                    patch.object(scanner.auto_tune, "evaluate_portfolio_window", side_effect=evaluate), \
                    patch.object(scanner.selection, "search_smart_core", return_value=marginal), \
                    patch("hl.price_path.coins_for_fills", return_value=["BTC"]), \
                    patch("hl.price_path.load_refined", return_value=[{"coin": "BTC", "time": 1000}]), \
                    patch("hl.price_path.coverage", return_value={
                        "coverage": .90, "expected": 100, "observed": 90,
                        "missingCoins": ["BTC"],
                    }):
                rows, result = scanner._build_explicit_selection(
                    db, "g1", "published-at", 10_000, audit_stamp="scan-start",
                )

            self.assertIsNone(result)
            roles = {row.addr: (row.role, row.reason) for row in rows}
            self.assertEqual(roles["0xold"], ("core", "path_validation_failed_keep_core"))
            self.assertEqual(roles["0xnew"], ("challenger", "path_validation_failed"))
            audit = db.execute(
                "SELECT stamp,status,reason,payload_json FROM pipeline_audit "
                "WHERE stage='selection_path_validation'"
            ).fetchone()
            self.assertEqual(audit[:3], (
                "scan-start", "fallback", "price_path_coverage_low,maintenance_margin_coverage_low,path_net_nonpositive",
            ))
            self.assertIn('"candidateCore": ["0xnew"]', audit[3])
            self.assertIn('"effectiveCore": ["0xold"]', audit[3])
            self.assertIn('"pathNetPnl": -25.0', audit[3])

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
            baseline = scanner.selection.PortfolioMetrics(
                0, 0, 0, 1, 1, 0, 0, 0, net_pnl=0, drawdown_dollars=0,
                risk_adjusted_utility=0,
            )
            profitable = scanner.selection.PortfolioMetrics(
                3000, 3000, 2, .9, .9, .1, .5, .1,
                net_pnl=3000, drawdown_dollars=1000, risk_adjusted_utility=2000,
            )
            marginal = scanner.selection.MarginalSelectionResult(
                selected=("0xaaa",), baseline=baseline, metrics=profitable,
                action="add", added=("0xaaa",), evaluated=2,
            )
            with patch.object(scanner.auto_tune, "_portfolio_window_fills", return_value={30: [{}]}), \
                    patch.object(scanner.selection, "search_smart_core", return_value=marginal), \
                    patch.object(scanner, "_portfolio_selection_metrics", side_effect=[profitable, baseline]):
                rows, result = scanner._build_explicit_selection(db, "g1", "now", 1000)

            self.assertEqual(result, marginal)
            self.assertEqual([(row.addr, row.role, row.reason) for row in rows], [
                ("0xaaa", "core", "portfolio_positive_net_contribution"),
            ])

    def test_manual_selection_mode_carries_operator_membership_into_new_generation(self):
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
                "(generation,addr,role,enabled,reason,utility,data_status,evidence_status,selected_at) "
                "VALUES ('manual-old','0xoperator','core',1,'operator_pick',9.0,'valid','qualified','2026-01-02')"
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
                    patch.object(scanner.rest, "get_leaderboard", return_value=[leaderboard_row("0xauto")]), \
                    patch.object(scanner, "_profile_one", side_effect=fake_profile), \
                    patch.object(scanner, "_launch_async_tuner", return_value={"status": "launched"}), \
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
            self.assertEqual(rows, [("0xoperator", "core", "operator_pick")])
            self.assertEqual(summary[0], "manual_selection_preserved")
            self.assertIn('"mode": "manual"', summary[1])


if __name__ == "__main__":
    unittest.main()
