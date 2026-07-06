import sqlite3
import tempfile
import unittest
from pathlib import Path

from hl import api, params, storage


class GuardedDb:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT w.addr FROM watchlist w LEFT JOIN target_controls tc ON tc.addr=w.addr"):
            raise AssertionError("positions endpoint should not run a separate follow-position query")
        return self.db.execute(sql, args)


class ApiPositionsPerfTests(unittest.TestCase):
    def _db(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = storage.connect(str(Path(td.name) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        db.row_factory = sqlite3.Row
        params.seed_params(db)
        db.execute(
            "INSERT INTO watchlist (rank,addr,score,market_type,updated_at) VALUES "
            "(1,'0xaaa',0.9,'crypto','now'),(2,'0xbbb',0.8,'crypto','now')"
        )
        db.execute("INSERT INTO target_controls (addr,enabled,updated_at) VALUES ('0xbbb',0,'now')")
        db.execute(
            "INSERT INTO copy_position "
            "(addr,coin,side,status,entry_px,leverage,margin,notional,size,rem_size,liq_px,"
            "mark_px,unrealized_pnl,opened_at,add_count) "
            "VALUES ('0xaaa','BTC','long','open',100,5,100,500,5,5,80,101,5,'2026-01-01T00:00:00Z',0)"
        )
        db.execute(
            "INSERT INTO copy_position "
            "(addr,coin,side,status,realized_pnl,entry_px,leverage,notional,master_peak_sz,"
            "master_open_px,was_stopped,was_liq,opened_at,closed_at,add_count) "
            "VALUES ('0xaaa','ETH','short','closed',10,200,4,800,4,200,0,0,"
            "'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z',1)"
        )
        db.commit()
        return db

    def test_open_positions_embed_follow_positions_without_extra_query(self):
        res = api.ep_positions(GuardedDb(self._db()), {"status": ["open"]})

        self.assertEqual(res["positions"][0]["followPos"], 1)

    def test_closed_positions_embed_follow_positions_without_extra_query(self):
        res = api.ep_positions(GuardedDb(self._db()), {"status": ["closed"]})

        self.assertEqual(res["positions"][0]["followPos"], 1)


if __name__ == "__main__":
    unittest.main()
