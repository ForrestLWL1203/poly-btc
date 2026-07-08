import sqlite3
import tempfile
import unittest
import asyncio
from pathlib import Path

from hl import storage
from hl.observer import Observer


class ObserverMarkRefreshTests(unittest.TestCase):
    def _db(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = storage.connect(str(Path(td.name) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        db.row_factory = sqlite3.Row
        db.execute(
            "INSERT INTO copy_position "
            "(addr,coin,side,status,entry_px,leverage,margin,notional,size,rem_size,opened_at) "
            "VALUES ('0xaaa','BTC','long','open',100,5,50,200,2,2,'2026-01-01T00:00:00Z')"
        )
        db.execute(
            "INSERT INTO copy_position "
            "(addr,coin,side,status,entry_px,leverage,margin,notional,size,rem_size,opened_at) "
            "VALUES ('0xbbb','ETH','long','open',200,5,50,200,1,1,'2026-01-01T00:00:00Z')"
        )
        db.commit()
        return db

    def test_bbo_tick_immediately_persists_that_coin_marks(self):
        db = self._db()
        obs = Observer(db, [], {})

        obs.on_bbo({"coin": "BTC", "bbo": [{"px": "101"}, {"px": "103"}]})

        btc = db.execute("SELECT mark_px,unrealized_pnl FROM copy_position WHERE coin='BTC'").fetchone()
        eth = db.execute("SELECT mark_px,unrealized_pnl FROM copy_position WHERE coin='ETH'").fetchone()
        self.assertEqual(btc["mark_px"], 102)
        self.assertEqual(btc["unrealized_pnl"], 4)
        self.assertIsNone(eth["mark_px"])
        self.assertIsNone(eth["unrealized_pnl"])

    def test_builder_all_mids_mark_overrides_book_mid_for_dashboard_marks(self):
        db = self._db()
        db.execute(
            "INSERT INTO copy_position "
            "(addr,coin,side,status,entry_px,leverage,margin,notional,size,rem_size,opened_at) "
            "VALUES ('0xccc','xyz:MU','long','open',900,5,50,900,1,1,'2026-01-01T00:00:00Z')"
        )
        db.commit()
        obs = Observer(db, [], {})
        obs.bbo["xyz:MU"] = (941, 943)
        obs.mark_mid["xyz:MU"] = 937

        obs._refresh_coin_marks("xyz:MU")

        mu = db.execute("SELECT mark_px,unrealized_pnl FROM copy_position WHERE coin='xyz:MU'").fetchone()
        self.assertEqual(mu["mark_px"], 937)
        self.assertEqual(mu["unrealized_pnl"], 37)

    def test_normal_close_does_not_persist_stale_liquidation_flag(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "INSERT INTO copy_position "
                "(addr,coin,side,status,entry_px,leverage,margin,notional,size,rem_size,opened_at) "
                "VALUES ('0xliq','DOGE','long','open',100,4,100,400,4,4,'2026-01-01T00:00:00Z')"
            ).lastrowid
            db.commit()
            obs = Observer(db, [], {})
            ready = asyncio.Event()
            ready.set()
            ep = {
                "pos_id": pos_id,
                "side": "long",
                "sign": 1,
                "entry_px": 100,
                "leverage": 4,
                "margin": 100,
                "notional": 400,
                "size": 4,
                "rem_size": 4,
                "realized_pnl": 0.0,
                "mae": 0.0,
                "num_actions": 0,
                "master_peak": 4,
                "entries_ready": ready,
                "lock": asyncio.Lock(),
                "was_liq": 1,
            }
            obs.taker.open_ep[("0xliq", "DOGE")] = ep

            await obs._apply_reduce(
                "0xliq", "DOGE", ep, 1_000, 99, -4, 0,
                closing=True, liq=False, maker=False, forced_px=99,
            )

            row = db.execute("SELECT status,was_liq FROM copy_position WHERE pos_id=?", (pos_id,)).fetchone()
            self.assertEqual(row["status"], "closed")
            self.assertEqual(row["was_liq"], 0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
