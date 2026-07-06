import tempfile
import unittest
from pathlib import Path

from hl import storage


class StorageIndexTests(unittest.TestCase):
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
            "idx_cp_status_opened",
            "idx_cp_closed_closed_at",
            "idx_cp_addr_status_opened",
            "idx_cp_coin_status_opened",
            "idx_cp_side_status_opened",
            "idx_ca_pos_action_ts",
            "idx_cmd_status_type_id",
        }
        self.assertTrue(expected.issubset(names), expected - names)


if __name__ == "__main__":
    unittest.main()
