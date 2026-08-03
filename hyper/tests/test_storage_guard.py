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

    def test_prunes_only_old_heavy_detail_and_redundant_staging(self):
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
        base = self._generation(1, source="scan")
        building = self._generation(2, status="ready")
        for n in range(3, 36):
            self._generation(n, current=int(n == 35))
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
        self.assertEqual(
            audit,
            [("selection_summary", "durable"), ("profile", "recent-heavy")],
        )
        kept = {row[0] for row in self.db.execute(
            "SELECT DISTINCT generation FROM leaderboard_staging"
        )}
        self.assertIn(base, kept)
        self.assertIn(building, kept)
        self.assertTrue({f"g{n:02d}" for n in range(6, 36)}.issubset(kept))
        self.assertEqual(result["retention"]["deletedPipelineRows"], 3)
        self.assertEqual(result["retention"]["deletedStagingRows"], 3)
        self.assertEqual(result["retention"]["deletedStagingGenerations"], 3)

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
            {"disk_used_warning", "db_growth_24h_warning", "wal_size_warning"},
        )
        state = self.db.execute(
            "SELECT state,detail_json FROM process_status WHERE name='storage_guard'"
        ).fetchone()
        self.assertEqual(state[0], "warning")
        self.assertEqual(json.loads(state[1])["database"]["growth24hBytes"],
                         config.STORAGE_GUARD_DB_GROWTH_WARN_BYTES_24H + 1)

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
