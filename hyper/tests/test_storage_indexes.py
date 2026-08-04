import sqlite3
import tempfile
import unittest
from pathlib import Path

from hyper import storage


class StorageIndexTests(unittest.TestCase):
    def test_legacy_pipeline_table_gains_generation_before_generation_index(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "hl.db")
            legacy = sqlite3.connect(path)
            legacy.execute(
                "CREATE TABLE pipeline_audit ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,stamp TEXT,source TEXT,stage TEXT,addr TEXT,"
                "rank INTEGER,status TEXT,reason TEXT,raw_score REAL,follow_score REAL,"
                "payload_json TEXT,created_at TEXT)"
            )
            legacy.commit()
            legacy.close()

            db = storage.connect(path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(pipeline_audit)")}
            indexes = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )}
            self.assertIn("generation", columns)
            self.assertIn("idx_pipeline_audit_generation_stage_id", indexes)
            db.close()

    def test_existing_maker_shadow_tables_and_param_are_retired(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "hl.db")
            legacy = sqlite3.connect(path)
            for table in ("shadow_account", "shadow_position", "shadow_action", "shadow_order", "target_orders"):
                legacy.execute(f"CREATE TABLE {table} (id INTEGER)")
            legacy.execute("CREATE TABLE params (key TEXT PRIMARY KEY, value TEXT)")
            legacy.execute("INSERT INTO params (key,value) VALUES ('EXEC_MAKER_MIRROR','true')")
            legacy.commit()
            legacy.close()

            db = storage.connect(path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            self.assertTrue({
                "shadow_account", "shadow_position", "shadow_action", "shadow_order", "target_orders"
            }.isdisjoint(tables))
            self.assertIsNone(db.execute(
                "SELECT value FROM params WHERE key='EXEC_MAKER_MIRROR'"
            ).fetchone())
            db.execute("ALTER TABLE copy_action ADD COLUMN maker INTEGER")
            db.commit()
            db.close()

            db = storage.connect(path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            self.assertNotIn("maker", {
                row[1] for row in db.execute("PRAGMA table_info(copy_action)").fetchall()
            })
            db.close()

    def test_hot_query_indexes_are_created(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            rows = db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
            ).fetchall()
            names = {r[0] for r in rows}

        expected = {
            "idx_leaderboard_candidate_mon_roi",
            "idx_leaderboard_candidate_week_roi",
            "idx_leaderboard_candidate_mon_pnl",
            "idx_prof_status_score_addr",
            "idx_prof_status_reason",
            "idx_watchlist_score_rank",
            "idx_watchlist_rank",
            "idx_follow_history_last_followed",
            "idx_ep_addr_close",
            "idx_scan_runs_finished",
            "idx_pipeline_audit_stamp_stage_id",
            "idx_pipeline_audit_stamp_source_stage_id",
            "idx_pipeline_audit_stage_id",
            "idx_pipeline_audit_addr_id",
            "idx_pipeline_audit_generation_stage_id",
            "idx_wallet_risk_event_addr_type",
            "idx_cp_status_opened",
            "idx_cp_closed_closed_at",
            "idx_cp_addr_status_opened",
            "idx_cp_coin_status_opened",
            "idx_cp_side_status_opened",
            "idx_ca_pos_action_ts",
            "idx_cmd_status_type_id",
            "idx_commands_status_created",
            "idx_execution_signal_status_completed",
            "idx_execution_account_observed_at",
            "idx_execution_reconcile_status_created",
        }
        self.assertTrue(expected.issubset(names), expected - names)


if __name__ == "__main__":
    unittest.main()
