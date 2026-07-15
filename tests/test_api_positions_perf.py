import sqlite3
import tempfile
import unittest
from importlib import import_module, util
from pathlib import Path

from hl import api_positions, params, storage


class GuardedDb:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT w.addr FROM watchlist w LEFT JOIN target_controls tc ON tc.addr=w.addr"):
            raise AssertionError("positions endpoint should not run a separate follow-position query")
        if "cp.master_margin" in normalized:
            raise AssertionError("positions endpoint should not select unused master_margin")
        if "FROM copy_action ca JOIN closed_base cb" in normalized:
            raise AssertionError("closed positions should start from the 100 closed positions, not scan copy_action")
        return self.db.execute(sql, args)


class DetailGuardedDb:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, args=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT ts,action,our_px,our_qty_delta,realized_pnl,master_oid FROM copy_action"):
            raise AssertionError("position detail should aggregate action fills in SQL, not fetch every slice")
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
            "(1,'0xaaa',0.9,'crypto','now'),(2,'0xbbb',0.8,'crypto','now'),"
            "(3,'0xccc',0.7,'crypto','now')"
        )
        db.execute("INSERT INTO target_controls (addr,enabled,updated_at) VALUES ('0xbbb',0,'now')")
        db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,publishable,is_current,started_at,published_at) "
            "VALUES ('g1','published',1,1,1,'2026-01-01','2026-01-02')"
        )
        db.executemany(
            "INSERT INTO follow_selection "
            "(generation,addr,role,enabled,utility,selection_rank,selected_at) "
            "VALUES ('g1',?,'core',1,?,?,'2026-01-02')",
            [("0xaaa", 1.0, 1), ("0xbbb", .5, 3), ("0xccc", 2.0, 2)],
        )
        db.execute(
            "INSERT INTO copy_position "
            "(addr,coin,side,status,entry_px,leverage,margin,notional,size,rem_size,liq_px,"
            "mark_px,unrealized_pnl,opened_at,add_count) "
            "VALUES ('0xaaa','BTC','long','open',100,5,100,500,5,5,80,101,5,'2026-01-01T00:00:00Z',0)"
        )
        db.execute(
            "INSERT INTO copy_position "
            "(addr,coin,side,status,realized_pnl,entry_px,leverage,notional,master_peak_sz,"
            "master_open_px,was_liq,opened_at,closed_at,add_count) "
            "VALUES ('0xaaa','ETH','short','closed',10,200,4,800,4,200,0,"
            "'2026-01-01T00:00:00Z','2026-01-01T01:00:00Z',1)"
        )
        db.commit()
        return db

    def test_open_positions_embed_follow_positions_without_extra_query(self):
        res = api_positions.ep_positions(GuardedDb(self._db()), {"status": ["open"]})

        self.assertEqual(res["positions"][0]["followPos"], 1)

    def test_closed_positions_embed_follow_positions_without_extra_query(self):
        res = api_positions.ep_positions(GuardedDb(self._db()), {"status": ["closed"]})

        self.assertEqual(res["positions"][0]["followPos"], 1)

    def test_closed_position_close_type_follows_terminal_status_not_stale_flag(self):
        db = self._db()
        db.execute(
            "INSERT INTO copy_position "
            "(addr,coin,side,status,realized_pnl,entry_px,leverage,notional,master_peak_sz,"
            "master_open_px,was_liq,opened_at,closed_at,add_count) "
            "VALUES ('0xaaa','DOGE','long','closed',-10,100,4,400,4,100,1,"
            "'2026-01-01T00:00:00Z','2026-01-01T02:00:00Z',0)"
        )
        db.commit()

        res = api_positions.ep_positions(db, {"status": ["closed"], "coin": ["DOGE"]})

        self.assertEqual(res["positions"][0]["closeType"], "mirror")

    def test_closed_position_uses_actual_exit_action_average_for_close_px(self):
        db = self._db()
        pos_id = db.execute(
            "INSERT INTO copy_position "
            "(addr,coin,side,status,realized_pnl,entry_px,leverage,notional,master_peak_sz,"
            "master_open_px,was_liq,opened_at,closed_at,add_count) "
            "VALUES ('0xaaa','SOL','long','closed',-10,100,4,1000,10,100,0,"
            "'2026-01-01T00:00:00Z','2026-01-01T02:00:00Z',0)"
        ).lastrowid
        db.executemany(
            "INSERT INTO copy_action "
            "(pos_id,addr,coin,ts,action,our_px,our_qty_delta,realized_pnl) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (pos_id, "0xaaa", "SOL", 1000, "reduce", 90, -3, -30),
                (pos_id, "0xaaa", "SOL", 2000, "close", 96, -1, 20),
            ],
        )
        db.commit()

        res = api_positions.ep_positions(db, {"status": ["closed"], "coin": ["SOL"]})

        self.assertAlmostEqual(res["positions"][0]["closePx"], 91.5)

    def test_positions_endpoints_are_split_from_api_module(self):
        self.assertIsNotNone(util.find_spec("hl.api_positions"))
        api_positions = import_module("hl.api_positions")

        self.assertTrue(callable(api_positions.ep_positions))
        self.assertTrue(callable(api_positions.ep_position_detail))

    def test_position_detail_aggregates_action_fills_in_sql(self):
        db = self._db()
        pos_id = db.execute(
            "INSERT INTO copy_position "
            "(addr,coin,side,status,entry_px,leverage,margin,size,rem_size,master_open_px,opened_at) "
            "VALUES ('0xaaa','BTC','long','open',100,5,100,5,5,99,'2026-01-01T00:00:00Z')"
        ).lastrowid
        rows = [
            (pos_id, "0xaaa", "BTC", 1000, "add", 7, 100, 1, 0),
            (pos_id, "0xaaa", "BTC", 1001, "add", 7, 110, 3, 0),
            (pos_id, "0xaaa", "BTC", 2000, "reduce", 8, 120, -1, 5),
            (pos_id, "0xaaa", "BTC", 2001, "close", 8, 130, -2, 15),
        ]
        db.executemany(
            "INSERT INTO copy_action "
            "(pos_id,addr,coin,ts,action,master_oid,our_px,our_qty_delta,realized_pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        db.commit()

        detail = api_positions.ep_position_detail(DetailGuardedDb(db), pos_id)

        self.assertEqual(detail["masterAdds"], 1)
        self.assertEqual(detail["ourAdds"], 1)
        self.assertEqual(len(detail["fills"]), 2)
        add_fill, close_fill = detail["fills"]
        self.assertEqual(add_fill["action"], "add")
        self.assertEqual(add_fill["fillCount"], 2)
        self.assertEqual(add_fill["qty"], 4)
        self.assertEqual(add_fill["px"], 107.5)
        self.assertEqual(add_fill["margin"], 86.0)
        self.assertIsNone(add_fill["pnl"])
        self.assertEqual(close_fill["action"], "close")
        self.assertEqual(close_fill["fillCount"], 2)
        self.assertEqual(close_fill["qty"], 3)
        self.assertAlmostEqual(close_fill["px"], 126.6666666667)
        self.assertEqual(close_fill["pnl"], 20)


if __name__ == "__main__":
    unittest.main()
