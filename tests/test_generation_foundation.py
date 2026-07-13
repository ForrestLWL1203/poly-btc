import sqlite3
import tempfile
import unittest
from pathlib import Path

from hl import generation, scanner_lifecycle, storage


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
                "open_events_7d", "actionable_open_events_30d", "open_days_14d",
                "open_position_count", "material_open_count", "raw_quality_score",
                "copy_positive_probability", "selection_marginal_utility", "model_coverage",
                "oos_cvar95", "capacity_fit",
            }.issubset(profile_cols))
            self.assertTrue({
                "generation", "profile_generation", "evaluated_at", "data_status", "evidence_status",
            }.issubset(watchlist_cols))
            self.assertTrue({
                "generation", "mode", "status", "proposal_json", "validation_json", "rollback_reason",
            }.issubset(tune_cols))
            self.assertTrue({
                "core_nomination_streak", "core_omission_streak", "core_nomination_started_at",
                "core_omission_started_at", "last_core_signal_generation",
            }.issubset(registry_cols))
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
                validation = generation.stage_and_validate_leaderboard(
                    db, first, first_rows, previous_count=0, fetched_at="2026-07-11T00:01:00Z"
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
                validation = generation.stage_and_validate_leaderboard(db, second, short_rows)
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
                    state="core", last_actionable_open_ms=100, core_nominated=True,
                )
                scanner_lifecycle.upsert_wallet_registry(
                    db, "0xabc", generation="g1", seen_at="2026-07-11T00:01:00Z", state="core",
                    core_nominated=False,
                )
                scanner_lifecycle.upsert_wallet_registry(
                    db, "0xabc", generation="g2", seen_at="2026-07-12T00:00:00Z", state="challenger",
                    core_nominated=False,
                )
                scanner_lifecycle.upsert_wallet_registry(
                    db, "0xabc", generation="g3", seen_at="2026-07-13T00:00:00Z",
                    data_status="deferred_data_error",
                )
                scanner_lifecycle.upsert_wallet_registry(
                    db, "0xabc", generation="g4", seen_at="2026-07-14T00:00:00Z",
                    state="core", last_actionable_open_ms=90, core_nominated=True,
                )

            row = db.execute(
                "SELECT state,current_role,consecutive_qualified,data_error_count,core_entries,core_exits,"
                "recovery_count,last_valid_generation,last_actionable_open_ms,core_nomination_streak,"
                "core_omission_streak,last_core_signal_generation "
                "FROM wallet_registry WHERE addr='0xabc'"
            ).fetchone()
            self.assertEqual(row, ("core", "core", 3, 1, 2, 1, 1, "g4", 100, 1, 0, "g4"))

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
            limit=rotation_n + 10,
            shard_count=7,
            refresh_shard=refresh_shard,
            exploration_seed="g1",
        )

        self.assertEqual(result["counts"]["rotation"], rotation_n)
        self.assertEqual(result["counts"]["new"], 4)
        self.assertEqual(result["counts"]["recovery"], 4)
        self.assertEqual(result["counts"]["exploration"], 2)
        self.assertTrue(all(
            scanner_lifecycle.stable_refresh_shard(addr, 7) == refresh_shard
            for addr in result["refresh"]["full_refetch"]
        ))
        self.assertEqual(
            scanner_lifecycle.stable_refresh_shard("0xabc", 7),
            scanner_lifecycle.stable_refresh_shard("0xABC", 7),
        )


if __name__ == "__main__":
    unittest.main()
