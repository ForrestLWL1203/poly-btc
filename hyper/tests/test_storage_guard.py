import calendar
import json
from pathlib import Path
import tempfile
import time
import unittest

from hyper import config, storage
from hyper.ops import storage_guard


class StorageGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "hl.db")
        self.db = storage.connect(
            self.db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
        )
        self.now = calendar.timegm(
            time.strptime("2026-08-01T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ")
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_connection_caps_reusable_wal_file(self):
        self.assertEqual(
            int(self.db.execute("PRAGMA journal_size_limit").fetchone()[0]),
            int(config.SQLITE_JOURNAL_SIZE_LIMIT_BYTES),
        )

    def _generation(self, n, *, source="challenger_daily", status="published", current=0):
        generation = f"g{n:02d}"
        self.db.execute(
            "INSERT INTO scan_generation "
            "(generation,source,status,complete,publishable,is_current,started_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                generation, source, status, int(status == "published"),
                int(status == "published"), int(current), f"2026-06-{(n % 28) + 1:02d}T00:00:00Z",
            ),
        )
        self.db.execute(
            "INSERT INTO leaderboard_staging(generation,addr) VALUES (?,?)",
            (generation, f"0x{n:040x}"),
        )
        return generation

    def test_prunes_completed_pipeline_and_keeps_only_required_generations(self):
        old = "2026-04-01T00:00:00Z"
        recent = "2026-07-31T00:00:00Z"
        rows = [
            (old, "scan", "profile", "old-heavy", old),
            (old, "scan", "official_roi", "old-heavy", old),
            (old, "scan", "workset_member", "old-heavy", old),
            (old, "scan", "selection_summary", "durable", old),
            (recent, "scan", "profile", "recent-heavy", recent),
        ]
        self.db.executemany(
            "INSERT INTO pipeline_audit(stamp,source,stage,reason,created_at) VALUES (?,?,?,?,?)",
            rows,
        )
        stale = self._generation(0, source="scan", status="leaderboard_validated")
        base = self._generation(1, source="scan")
        for n in range(3, 36):
            self._generation(n, current=int(n == 35))
        building = self._generation(36, status="ready")
        self.db.commit()

        result = storage_guard.run(
            self.db,
            self.db_path,
            now_epoch=self.now,
            disk_usage=(10_000, 2_000, 8_000),
            db_main_bytes=100,
            db_wal_bytes=10,
        )

        audit = self.db.execute(
            "SELECT stage,reason FROM pipeline_audit ORDER BY id"
        ).fetchall()
        self.assertEqual(audit, [])
        kept = {row[0] for row in self.db.execute(
            "SELECT DISTINCT generation FROM leaderboard_staging"
        )}
        self.assertIn(base, kept)
        self.assertIn(building, kept)
        self.assertEqual(kept, {base, building, "g35"})
        self.assertEqual(
            self.db.execute(
                "SELECT status FROM scan_generation WHERE generation=?", (stale,),
            ).fetchone()[0],
            "failed",
        )
        self.assertEqual(result["retention"]["supersededGenerations"], 1)
        self.assertEqual(result["retention"]["deletedPipelineRows"], 5)
        self.assertEqual(
            result["retention"]["generationCleanup"]["leaderboard_staging"], 33,
        )

    def test_warns_on_daily_growth_disk_and_wal_thresholds(self):
        storage_guard.run(
            self.db,
            self.db_path,
            now_epoch=self.now - 86400,
            disk_usage=(10_000, 2_000, 8_000),
            db_main_bytes=2_000_000_000,
            db_wal_bytes=1,
        )
        result = storage_guard.run(
            self.db,
            self.db_path,
            now_epoch=self.now,
            disk_usage=(10_000, 7_100, 2_900),
            db_main_bytes=2_000_000_000 + config.STORAGE_GUARD_DB_GROWTH_WARN_BYTES_24H + 1,
            db_wal_bytes=config.STORAGE_GUARD_WAL_WARN_BYTES + 1,
        )

        self.assertEqual(result["status"], "warning")
        self.assertEqual(
            set(result["reasons"]),
            {"disk_used_warning", "wal_size_warning"},
        )
        state = self.db.execute(
            "SELECT state,detail_json FROM process_status WHERE name='storage_guard'"
        ).fetchone()
        self.assertEqual(state[0], "warning")
        # Physical preallocation does not count as active-data growth.
        self.assertEqual(json.loads(state[1])["database"]["growth24hBytes"], 0)

    def test_dry_run_is_read_only_and_reports_protected_generations(self):
        current = self._generation(1, source="scan", current=1)
        old = self._generation(2)
        self.db.execute(
            "INSERT INTO pipeline_audit(generation,stamp,source,stage,created_at) "
            "VALUES (?,?,?,?,?)",
            (old, "old", "scan", "profile", "2026-07-01T00:00:00Z"),
        )
        self.db.commit()

        result = storage_guard.run(
            self.db, self.db_path, now_epoch=self.now, dry_run=True,
            disk_usage=(10_000, 2_000, 8_000), db_main_bytes=100, db_wal_bytes=10,
        )

        self.assertTrue(result["dryRun"])
        self.assertEqual(result["retention"]["deletedPipelineRows"], 1)
        self.assertGreater(result["retention"]["estimatedReclaimedPages"], 0)
        self.assertGreater(result["retention"]["estimatedReclaimedBytes"], 0)
        self.assertEqual(result["database"]["reclaimedFreelistBytes"], 0)
        self.assertEqual(result["retention"]["protectedGenerations"]["current"], current)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pipeline_audit").fetchone()[0], 1)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM leaderboard_staging").fetchone()[0], 2,
        )

    def test_dry_run_counts_legacy_audit_owned_by_superseded_generation(self):
        stale = self._generation(1, source="scan", status="leaderboard_validated")
        started_at = self.db.execute(
            "SELECT started_at FROM scan_generation WHERE generation=?", (stale,),
        ).fetchone()[0]
        current = self._generation(2, source="scan", current=1)
        self.db.execute(
            "INSERT INTO pipeline_audit(stamp,source,stage,created_at) VALUES (?,?,?,?)",
            (started_at, "scan", "profile", started_at),
        )
        self.db.commit()

        result = storage_guard.run(
            self.db, self.db_path, now_epoch=self.now, dry_run=True,
            disk_usage=(10_000, 2_000, 8_000), db_main_bytes=100, db_wal_bytes=10,
        )

        self.assertEqual(result["retention"]["deletedPipelineRows"], 1)
        self.assertEqual(result["retention"]["protectedGenerations"]["current"], current)
        self.assertEqual(result["retention"]["supersededGenerations"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pipeline_audit").fetchone()[0], 1)

    def test_post_publish_cleanup_preserves_latest_full_cache_and_trade_ledgers(self):
        base = self._generation(1, source="scan")
        current = self._generation(2, current=1)
        self.db.execute(
            "INSERT INTO pre_strict_evidence "
            "(generation,addr,policy_version,model_version,status,created_at) VALUES (?,?,?,?,?,?)",
            (base, "0xaaa", "p", "m", "qualified", "2026-07-01T00:00:00Z"),
        )
        self.db.execute(
            "INSERT INTO formation_prefix_evidence "
            "(generation,policy_version,params_hash,membership_hash,member_count,evaluation_json,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (current, "p", "h", "m", 1, "{}", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z"),
        )
        self.db.execute(
            "INSERT INTO pipeline_audit(generation,stamp,source,stage,created_at) VALUES (?,?,?,?,?)",
            (current, "s", "challenger_daily", "profile", "2026-07-01T00:00:00Z"),
        )
        self.db.execute(
            "INSERT INTO copy_position(addr,coin,side,status,opened_at) VALUES (?,?,?,?,?)",
            ("0xaaa", "BTC", "long", "closed", "2026-07-01T00:00:00Z"),
        )
        ledger_before = self.db.execute("SELECT COUNT(*) FROM copy_position").fetchone()[0]
        self.db.commit()

        result = storage_guard.post_publish_cleanup(self.db, current, now_epoch=self.now)

        self.assertEqual(result["pipelineAudit"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM formation_prefix_evidence").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pre_strict_evidence").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_position").fetchone()[0], ledger_before)

    def test_execution_diagnostics_use_normal_and_anomaly_windows(self):
        old = "2026-07-01T00:00:00Z"
        recent = "2026-07-31T00:00:00Z"
        self.db.executemany(
            "INSERT INTO execution_account_snapshot "
            "(session_id,equity,available,observed_at) VALUES (?,?,?,?)",
            [("s", 1, 1, old), ("s", 1, 1, recent)],
        )
        self.db.executemany(
            "INSERT INTO execution_reconcile_checkpoint(session_id,status,created_at) VALUES (?,?,?)",
            [("s", "ok", old), ("s", "reconcile_required", old)],
        )
        self.db.executemany(
            "INSERT INTO execution_signal "
            "(mode,session_id,addr,coin,tid,source_time_ms,payload_json,state,received_at,updated_at,completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("live", "s", "0xa", "BTC", 1, 1, "{}", "completed", old, old, old),
                ("live", "s", "0xa", "BTC", 2, 2, "{}", "retryable", old, old, None),
            ],
        )
        self.db.commit()

        removed = storage_guard.prune_execution_transients(self.db, now_epoch=self.now)

        self.assertEqual(removed["execution_account_snapshot"], 1)
        self.assertEqual(removed["execution_reconcile_ok"], 1)
        self.assertEqual(removed["execution_reconcile_anomaly"], 0)
        self.assertEqual(removed["execution_signal"], 1)
        self.assertEqual(
            self.db.execute("SELECT state FROM execution_signal").fetchone()[0], "retryable",
        )

    def test_storage_guard_trims_legacy_scan_history_to_latest_five(self):
        self.db.executemany(
            "INSERT INTO scan_runs (started_at,finished_at) VALUES (?,?)",
            [(f"run-{index}", f"done-{index}") for index in range(8)],
        )
        self.db.commit()

        deleted = storage_guard.trim_scan_history(self.db)
        self.db.commit()

        self.assertEqual(deleted, 3)
        self.assertEqual(
            [row[0] for row in self.db.execute(
                "SELECT started_at FROM scan_runs ORDER BY id DESC"
            )],
            [f"run-{index}" for index in range(7, 2, -1)],
        )

    def test_critical_disk_state_takes_precedence(self):
        result = storage_guard.run(
            self.db,
            self.db_path,
            now_epoch=self.now,
            disk_usage=(10_000, 8_500, 1_500),
            db_main_bytes=100,
            db_wal_bytes=10,
        )

        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["reasons"], ["disk_used_critical"])

    def test_expires_fill_window_and_purges_bootstrapped_automation_cache(self):
        old_ms = int((self.now - (config.PROFILE_FETCH_DAYS + 2) * 86400) * 1000)
        fresh_ms = int((self.now - 86400) * 1000)
        stale_addr = "0x" + "1" * 40
        bot_addr = "0x" + "2" * 40
        self.db.executemany(
            "INSERT INTO candidate_fills(addr,tid,time,fill_json) VALUES (?,?,?,?)",
            [
                (stale_addr, 1, old_ms, "{}"),
                (stale_addr, 2, fresh_ms, "{}"),
                (bot_addr, 3, fresh_ms, "{}"),
            ],
        )
        self.db.executemany(
            "INSERT INTO fill_cache_state(addr,coverage_start_ms) VALUES (?,?)",
            [(stale_addr, old_ms), (bot_addr, old_ms)],
        )
        self.db.execute(
            "INSERT INTO profile(addr,status,reason,n_trades,data_status) VALUES (?,?,?,?,?)",
            (bot_addr, "rejected", "bot_frequency", 25, "valid"),
        )
        self.db.commit()

        result = storage_guard.run(
            self.db,
            self.db_path,
            now_epoch=self.now,
            disk_usage=(10_000, 2_000, 8_000),
            db_main_bytes=100,
            db_wal_bytes=10,
        )

        remaining = self.db.execute(
            "SELECT addr,tid FROM candidate_fills ORDER BY addr,tid"
        ).fetchall()
        self.assertEqual(remaining, [(stale_addr, 2)])
        self.assertEqual(
            self.db.execute(
                "SELECT reason FROM wallet_scan_blacklist WHERE addr=?", (bot_addr,)
            ).fetchone()[0],
            "bot_frequency",
        )
        self.assertEqual(result["retention"]["expiredCandidateFills"], 1)
        self.assertEqual(result["retention"]["blacklistCleanup"]["candidate_fills"], 1)
        self.assertIn("walCheckpoint", result["database"])


if __name__ == "__main__":
    unittest.main()
