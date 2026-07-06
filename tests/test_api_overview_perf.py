import sqlite3
import tempfile
import unittest
from pathlib import Path

from hl import api, storage


class GuardedDb:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        if normalized == "SELECT realized_pnl FROM copy_position WHERE status!='open'":
            raise AssertionError("overview must aggregate closed PnL in SQL, not fetch every row")
        return self.db.execute(sql, args)


class ApiOverviewPerfTests(unittest.TestCase):
    def test_overview_aggregates_closed_win_rate_in_sql(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.execute(
                "INSERT INTO copy_account (id,initial_balance,balance,updated_at) VALUES (1,10000,10020,'now')"
            )
            db.execute(
                "INSERT INTO copy_position (addr,coin,side,status,realized_pnl,opened_at,closed_at) "
                "VALUES ('0x1','BTC','long','closed',50,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')"
            )
            db.execute(
                "INSERT INTO copy_position (addr,coin,side,status,realized_pnl,opened_at,closed_at) "
                "VALUES ('0x2','ETH','short','closed',-30,'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z')"
            )
            db.commit()

            overview = api.ep_overview(GuardedDb(db))

        self.assertEqual(overview["winRatePct"], 50.0)


if __name__ == "__main__":
    unittest.main()
