import sqlite3
import tempfile
import unittest
from pathlib import Path

from hyper import storage
from hyper.discovery import generation, scanner_lifecycle


def leaderboard_row(addr, *, complete=True, candidate=True):
    windows = []
    for name in generation.WINDOW_NAMES:
        payload = {"pnl": 1.0, "roi": 0.01, "vlm": 100.0}
        if not complete and name == "allTime":
            payload["vlm"] = None
        windows.append((name, payload))
    return {
        "ethAddress": addr,
        "displayName": None,
        "accountValue": 10_000.0,
        "windowPerformances": windows,
        "is_candidate": int(candidate),
    }


class GenerationFoundationTests(unittest.TestCase):
    def open_db(self, path):
        return storage.connect(str(path), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)

    def test_existing_tables_receive_backward_compatible_vnext_columns(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.db"
            old = sqlite3.connect(path)
            old.executescript(
                """
                CREATE TABLE leaderboard (
                    addr TEXT PRIMARY KEY, display_name TEXT, account_value REAL,
                    day_pnl REAL,day_roi REAL,day_vlm REAL,week_pnl REAL,week_roi REAL,week_vlm REAL,
                    mon_pnl REAL,mon_roi REAL,mon_vlm REAL,all_pnl REAL,all_roi REAL,all_vlm REAL,
                    daily_turnover REAL,is_candidate INTEGER,fetched_at TEXT
                );
                CREATE TABLE profile (addr TEXT PRIMARY KEY,status TEXT,reason TEXT,score REAL);
                CREATE TABLE watchlist (addr TEXT PRIMARY KEY,rank INTEGER,score REAL);
                CREATE TABLE auto_tune_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT,stamp TEXT,selected_mult REAL,
                    applied INTEGER,followed_n INTEGER,baseline_json TEXT,result_json TEXT,created_at TEXT
                );
                CREATE TABLE wallet_registry (
                    addr TEXT PRIMARY KEY,state TEXT NOT NULL DEFAULT 'qualified',current_role TEXT,
                    first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,first_qualified_at TEXT,
                    last_qualified_at TEXT,first_core_at TEXT,last_core_at TEXT,last_rejected_at TEXT,
                    last_reject_reason TEXT,cooldown_until TEXT,data_error_count INTEGER NOT NULL DEFAULT 0,
                    consecutive_qualified INTEGER NOT NULL DEFAULT 0,consecutive_bad INTEGER NOT NULL DEFAULT 0,
                    core_entries INTEGER NOT NULL DEFAULT 0,core_exits INTEGER NOT NULL DEFAULT 0,
                    recovery_count INTEGER NOT NULL DEFAULT 0,last_valid_generation TEXT,
                    last_evaluated_generation TEXT,last_actionable_open_ms INTEGER,updated_at TEXT NOT NULL
                );
                """
            )
            old.commit()
            old.close()

            db = self.open_db(path)
            profile_cols = {row[1] for row in db.execute("PRAGMA table_info(profile)")}
            watchlist_cols = {row[1] for row in db.execute("PRAGMA table_info(watchlist)")}
            tune_cols = {row[1] for row in db.execute("PRAGMA table_info(auto_tune_runs)")}
            registry_cols = {row[1] for row in db.execute("PRAGMA table_info(wallet_registry)")}
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

            self.assertTrue({
                "profile_generation", "data_status", "evidence_status", "last_copyable_open_ms",
                "open_events_7d", "actionable_open_events_30d", "open_days_30d",
                "open_position_count", "material_open_count", "raw_quality_score",
                "copy_positive_probability", "selection_marginal_utility", "model_coverage",
                "oos_cvar95", "capacity_fit",
            }.issubset(profile_cols))
            self.assertFalse({
                "avg_win", "avg_loss", "roi_notional", "gross_pnl", "total_fee", "n_coins",
                "long_frac", "life_trades", "pf_max_dd", "pf_edge_bps", "open_events_14d",
                "actionable_open_events_14d", "open_days_7d", "open_days_14d",
                "avg_open_interval_h", "median_open_interval_h", "open_probability_24h",
            } & profile_cols)
            self.assertTrue({
                "generation", "profile_generation", "evaluated_at", "data_status", "evidence_status",
            }.issubset(watchlist_cols))
            self.assertTrue({
                "generation", "mode", "status", "proposal_json", "validation_json", "rollback_reason",
            }.issubset(tune_cols))
            self.assertFalse({
                "core_nomination_streak", "core_omission_streak", "core_nomination_started_at",
                "core_omission_started_at", "last_core_signal_generation",
            } & registry_cols)
            self.assertTrue({
                "scan_generation", "leaderboard_staging", "wallet_registry", "follow_selection",
            }.issubset(tables))

    def test_leaderboard_count_thresholds_are_inclusive_and_reject_duplicates(self):
        valid = generation.validate_leaderboard_counts(85, 85, 85, previous_count=100)
        too_short = generation.validate_leaderboard_counts(84, 84, 84, previous_count=100)
        complete_99 = generation.validate_leaderboard_counts(100, 100, 99, previous_count=100)
        duplicate = generation.validate_leaderboard_counts(100, 99, 100, previous_count=100)

        self.assertTrue(valid.valid)
        self.assertFalse(too_short.valid)
        self.assertTrue(complete_99.valid)
        self.assertFalse(duplicate.valid)
        self.assertIn("duplicate_or_missing_address", duplicate.reasons)

    def test_only_complete_generation_atomically_replaces_current_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(Path(td) / "hl.db")
            first_rows = [leaderboard_row(f"0x{i:040x}") for i in range(100)]
            with db:
                first = generation.begin_generation(
                    db, generation="g1", started_at="2026-07-11T00:00:00Z"
                )
                validation = generation.validate_leaderboard_rows(first_rows, previous_count=0)
                generation.stage_leaderboard_rows(
                    db, first, first_rows, fetched_at="2026-07-11T00:01:00Z"
                )
                generation.record_leaderboard_validation(
                    db, first, validation, fetched_at="2026-07-11T00:01:00Z"
                )
                self.assertTrue(validation.valid)
                generation.mark_generation_ready(
                    db, first, profile_total=100, profile_valid=100, profile_complete=True,
                    ready_at="2026-07-11T00:02:00Z",
                )
                generation.publish_generation(db, first, published_at="2026-07-11T00:03:00Z")

            self.assertEqual(db.execute("SELECT COUNT(*) FROM leaderboard").fetchone()[0], 100)
            self.assertEqual(generation.current_published_generation(db)["generation"], "g1")
            self.assertEqual({row[0] for row in db.execute("SELECT DISTINCT generation FROM leaderboard")}, {"g1"})

            short_rows = [leaderboard_row(f"0x{i:040x}") for i in range(84)]
            with db:
                second = generation.begin_generation(
                    db, generation="g2", started_at="2026-07-12T00:00:00Z"
                )
                validation = generation.validate_leaderboard_rows(short_rows, previous_count=100)
                generation.stage_leaderboard_rows(db, second, short_rows)
                generation.record_leaderboard_validation(db, second, validation)
                self.assertFalse(validation.valid)
                with self.assertRaises(ValueError):
                    generation.mark_generation_ready(
                        db, second, profile_total=84, profile_valid=84, profile_complete=True
                    )

            self.assertEqual(generation.current_published_generation(db)["generation"], "g1")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leaderboard").fetchone()[0], 100)
            self.assertEqual(db.execute(
                "SELECT status,leaderboard_valid FROM scan_generation WHERE generation='g2'"
            ).fetchone(), ("failed", 0))

    def test_window_completeness_is_measured_from_all_four_windows(self):
        rows = [leaderboard_row(f"0x{i:040x}") for i in range(99)]
        rows.append(leaderboard_row("0x" + "f" * 40, complete=False))
        exactly_99 = generation.validate_leaderboard_rows(rows, previous_count=100)
        rows[-2] = leaderboard_row("0x" + "e" * 40, complete=False)
        below_99 = generation.validate_leaderboard_rows(rows, previous_count=100)

        self.assertTrue(exactly_99.valid)
        self.assertEqual(exactly_99.complete_count, 99)
        self.assertFalse(below_99.valid)
        self.assertIn("window_completeness_below_floor", below_99.reasons)

    def test_registry_counts_one_transition_per_generation_and_keeps_deferred_state(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(Path(td) / "hl.db")
            with db:
                scanner_lifecycle.upsert_wallet_registry(
                    db, "0xABC", generation="g1", seen_at="2026-07-11T00:00:00Z",
                    state="core", last_actionable_open_ms=100,
                )
                scanner_lifecycle.upsert_wallet_registry(
                    db, "0xabc", generation="g1", seen_at="2026-07-11T00:01:00Z", state="core",
                )
                scanner_lifecycle.upsert_wallet_registry(
                    db, "0xabc", generation="g2", seen_at="2026-07-12T00:00:00Z", state="challenger",
                )
                scanner_lifecycle.upsert_wallet_registry(
                    db, "0xabc", generation="g3", seen_at="2026-07-13T00:00:00Z",
                    data_status="deferred_data_error",
                )
                scanner_lifecycle.upsert_wallet_registry(
                    db, "0xabc", generation="g4", seen_at="2026-07-14T00:00:00Z",
                    state="core", last_actionable_open_ms=90,
                )

            row = db.execute(
                "SELECT state,current_role,consecutive_qualified,data_error_count,core_entries,core_exits,"
                "recovery_count,last_valid_generation,last_actionable_open_ms "
                "FROM wallet_registry WHERE addr='0xabc'"
            ).fetchone()
            self.assertEqual(row, ("core", "core", 3, 1, 2, 1, 1, "g4", 100))

    def test_connect_retires_obsolete_selection_columns_and_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "hl.db"
            db = self.open_db(path)
            for column, definition in (
                ("core_nomination_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("core_omission_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("core_nomination_started_at", "TEXT"),
                ("core_omission_started_at", "TEXT"),
                ("last_core_signal_generation", "TEXT"),
            ):
                db.execute(f"ALTER TABLE wallet_registry ADD COLUMN {column} {definition}")
            for column, definition in (
                ("avg_win", "REAL"), ("avg_loss", "REAL"), ("roi_notional", "REAL"),
                ("gross_pnl", "REAL"), ("total_fee", "REAL"), ("n_coins", "INTEGER"),
                ("long_frac", "REAL"), ("life_trades", "INTEGER"), ("pf_max_dd", "REAL"),
                ("pf_edge_bps", "REAL"), ("open_events_14d", "INTEGER"),
                ("actionable_open_events_14d", "INTEGER"), ("open_days_7d", "INTEGER"),
                ("open_days_14d", "INTEGER"), ("avg_open_interval_h", "REAL"),
                ("median_open_interval_h", "REAL"), ("open_probability_24h", "REAL"),
            ):
                db.execute(f"ALTER TABLE profile ADD COLUMN {column} {definition}")
            db.executemany(
                "INSERT OR REPLACE INTO params "
                "(key,value,category,level,type,effect,default_value) VALUES (?,?,'follow','green','float','immediate',?)",
                [(key, "1", "1") for key in ("MIN_FOLLOW_SCORE", "COPY_STOP_ENABLE", "STOP_MARGIN_PCT")],
            )
            db.executemany(
                "INSERT OR REPLACE INTO auto_tune_state (key,value,updated_at) VALUES (?, '{}', 'old')",
                [(key,) for key in (
                    "margin_base", "margin_last_auto", "tune_base", "tune_last_auto",
                    "add_base", "add_last_auto", "follow_line_last_choice",
                )],
            )
            db.commit()
            db.close()

            migrated = self.open_db(path)
            registry_cols = {row[1] for row in migrated.execute("PRAGMA table_info(wallet_registry)")}
            profile_cols = {row[1] for row in migrated.execute("PRAGMA table_info(profile)")}
            self.assertFalse({
                "core_nomination_streak", "core_omission_streak", "core_nomination_started_at",
                "core_omission_started_at", "last_core_signal_generation",
            } & registry_cols)
            self.assertFalse({
                "avg_win", "avg_loss", "roi_notional", "gross_pnl", "total_fee", "n_coins",
                "long_frac", "life_trades", "pf_max_dd", "pf_edge_bps", "open_events_14d",
                "actionable_open_events_14d", "open_days_7d", "open_days_14d",
                "avg_open_interval_h", "median_open_interval_h", "open_probability_24h",
            } & profile_cols)
            self.assertEqual(migrated.execute(
                "SELECT COUNT(*) FROM params WHERE key IN ('MIN_FOLLOW_SCORE','COPY_STOP_ENABLE','STOP_MARGIN_PCT')"
            ).fetchone()[0], 0)
            self.assertEqual(migrated.execute(
                "SELECT COUNT(*) FROM auto_tune_state WHERE key IN "
                "('margin_base','margin_last_auto','tune_base','tune_last_auto',"
                "'add_base','add_last_auto','follow_line_last_choice')"
            ).fetchone()[0], 0)

    def test_scheduler_preserves_priority_and_applies_time_capacity(self):
        candidates = [f"0xc{i}" for i in range(30)]
        budget = scanner_lifecycle.ScanTimeBudget(100.0, total_s=60.0, finalize_reserve_s=10.0)
        result = scanner_lifecycle.schedule_profile_workset(
            candidates,
            position_addrs=["0xposition"],
            core_addrs=["0xcore"],
            qualified_addrs=["0xcore", "0xqualified"],
            challenger_addrs=["0xchallenger"],
            off_list_qualified_addrs=["0xoff"],
            profiled_addrs=candidates,
            limit=300,
            budget=budget,
            estimated_profile_s=5.0,
            now_monotonic=125.0,
            refresh_shard=0,
        )

        self.assertEqual(result["workset"][:5], [
            "0xposition", "0xcore", "0xqualified", "0xchallenger", "0xoff",
        ])
        self.assertEqual(result["counts"]["priority"], 5)
        self.assertEqual(result["time_capacity"], 5)
        self.assertEqual(len(result["workset"]), 10)

    def test_scheduler_counts_warmup_as_ordinary_budget_not_challenger_priority(self):
        candidates = ["0xwarm1", "0xwarm2", "0xother1", "0xother2"]
        result = scanner_lifecycle.schedule_profile_workset(
            candidates,
            core_addrs=["0xcore"],
            qualified_addrs=["0xcore", "0xchallenger"],
            challenger_addrs=["0xchallenger"],
            warmup_backfill_addrs=["0xwarm1", "0xwarm2"],
            profiled_addrs=candidates,
            limit=4,
            refresh_shard=0,
        )

        self.assertEqual(result["counts"]["priority"], 2)
        self.assertEqual(result["counts"]["qualified"], 2)
        self.assertEqual(result["counts"]["challenger"], 1)
        self.assertEqual(result["counts"]["warmup_backfill"], 2)
        self.assertEqual(result["workset"], ["0xcore", "0xchallenger", "0xwarm1", "0xwarm2"])

    def test_scheduler_uses_40_40_20_after_stable_refresh_lane(self):
        new = [f"0xnew{i}" for i in range(30)]
        recovery = [f"0xrecover{i}" for i in range(30)]
        explore = [f"0xexplore{i}" for i in range(30)]
        candidates = new + recovery + explore
        profiled = recovery + explore
        refresh_shard = 3
        rotation_n = sum(
            scanner_lifecycle.stable_refresh_shard(addr, 7) == refresh_shard for addr in candidates
        )
        result = scanner_lifecycle.schedule_profile_workset(
            candidates,
            profiled_addrs=profiled,
            near_threshold_addrs=recovery,
            exploration_addrs=explore,
            full_refetch_addrs=[new[0]],
            limit=rotation_n + 10,
            shard_count=7,
            refresh_shard=refresh_shard,
            exploration_seed="g1",
        )

        self.assertEqual(result["counts"]["rotation"], rotation_n)
        self.assertEqual(result["counts"]["new"], 4)
        self.assertEqual(result["counts"]["recovery"], 4)
        self.assertEqual(result["counts"]["exploration"], 2)
        self.assertEqual(
            result["refresh"]["full_refetch"],
            [new[0]] if new[0] in result["workset"] else [],
        )
        self.assertTrue(all(
            addr not in result["refresh"]["full_refetch"]
            for addr in result["workset"] if addr != new[0]
        ))
        self.assertEqual(
            scanner_lifecycle.stable_refresh_shard("0xabc", 7),
            scanner_lifecycle.stable_refresh_shard("0xABC", 7),
        )

    def test_complete_cache_evaluation_shard_stays_delta_only(self):
        candidates = [f"0xwallet{i}" for i in range(40)]
        refresh_shard = 2
        result = scanner_lifecycle.schedule_profile_workset(
            candidates,
            profiled_addrs=candidates,
            limit=len(candidates),
            shard_count=7,
            refresh_shard=refresh_shard,
            full_refetch_addrs=[],
        )

        self.assertTrue(any(
            scanner_lifecycle.stable_refresh_shard(addr, 7) == refresh_shard
            for addr in result["workset"]
        ))
        self.assertEqual(result["refresh"]["full_refetch"], [])
        self.assertEqual(result["refresh"]["delta"], result["workset"])

    def test_weekly_full_evaluation_does_not_refetch_complete_wallets(self):
        candidates = ["0xknown1", "0xknown2", "0xnew"]
        result = scanner_lifecycle.schedule_profile_workset(
            candidates,
            profiled_addrs=candidates[:2],
            full_refetch_addrs=["0xnew"],
            limit=len(candidates),
            full_scan=True,
        )

        self.assertEqual(result["workset"], candidates)
        self.assertEqual(result["refresh"]["full_refetch"], ["0xnew"])
        self.assertEqual(result["refresh"]["delta"], candidates[:2])
        self.assertEqual(result["fill_mode"], "mixed")

    def test_subsequent_refresh_shard_batch_excludes_used_and_preserves_order(self):
        candidates = [f"0xwallet{i}" for i in range(100)]
        current = 4
        next_shard = (current + 1) % 7
        already_used = next(
            addr for addr in candidates
            if scanner_lifecycle.stable_refresh_shard(addr, 7) == next_shard
        )

        batches = scanner_lifecycle.subsequent_refresh_shard_batches(
            candidates,
            [already_used],
            current_shard=current,
            shard_count=7,
            max_shards=1,
        )

        expected = [
            addr for addr in candidates
            if addr != already_used
            and scanner_lifecycle.stable_refresh_shard(addr, 7) == next_shard
        ]
        self.assertEqual(batches, [{"shard": next_shard, "workset": expected}])


if __name__ == "__main__":
    unittest.main()
