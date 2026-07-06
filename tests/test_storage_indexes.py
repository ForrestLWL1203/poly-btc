import tempfile
import unittest
from pathlib import Path

from hl import storage


class StorageIndexTests(unittest.TestCase):
    def test_dashboard_query_indexes_are_created(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            rows = db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
            ).fetchall()
            names = {r[0] for r in rows}

        expected = {
            "idx_watchlist_score_rank",
            "idx_watchlist_rank",
            "idx_ep_addr_close",
            "idx_cp_status_opened",
            "idx_cp_closed_closed_at",
            "idx_cp_addr_status_opened",
            "idx_cp_coin_status_opened",
            "idx_cp_side_status_opened",
            "idx_ca_pos_action_ts",
        }
        self.assertTrue(expected.issubset(names), expected - names)


if __name__ == "__main__":
    unittest.main()
