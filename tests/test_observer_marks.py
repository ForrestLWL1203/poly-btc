import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hl import storage
from hl.observer import Observer
from hl.util import now_ms


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

    def test_reload_targets_loads_sector_policy(self):
        db = self._db()
        db.execute(
            "INSERT INTO watchlist (rank,addr,score,acct_value,sector_policy_json,updated_at) "
            "VALUES (1,'0xsector',0.9,10000,?,'now')",
            ('{"crypto":{"allow":true},"stock":{"allow":false},"allowed":["crypto"]}',),
        )
        db.commit()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {}, top_n=10, min_score=0.5)

            obs._reload_targets(init=True)

            self.assertIn("0xsector", obs.addrs)
            self.assertFalse(obs.target_sector_policy["0xsector"]["stock"]["allow"])
            self.assertTrue(obs.target_sector_policy["0xsector"]["crypto"]["allow"])
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_disallowed_sector_open_is_skipped(self):
        db = self._db()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {})
            obs.target_sector_policy = {
                "0xsector": {"crypto": {"allow": True}, "stock": {"allow": False}, "allowed": ["crypto"]},
            }

            with patch.object(obs, "_open_position") as open_position:
                obs._dispatch_fill(
                    obs.taker,
                    "0xsector",
                    "xyz:MU",
                    ("0xsector", "xyz:MU"),
                    1_000,
                    10,
                    0,
                    10,
                    900,
                    False,
                    False,
                    1,
                )

            open_position.assert_not_called()
            self.assertEqual(obs.hb.get("skip_sector_disabled"), 1)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_pending_maker_open_fills_when_price_later_trades_through(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs._load_account(obs.maker)
            obs.vol["BTC"] = 0.03
            for tier in obs.tier_min_notional:
                obs.tier_min_notional[tier] = 0

            async def ensure_vol(_coin):
                return None

            obs._ensure_vol = ensure_vol
            t = now_ms()
            obs.px_ext["BTC"] = [100.0, 101.0, t]

            with patch.object(obs, "_target_snapshot", return_value=(5, 5, 1000, 100)):
                obs._dispatch_fill(
                    obs.maker,
                    "0xmaker",
                    "BTC",
                    ("0xmaker", "BTC"),
                    t,
                    1,
                    0,
                    1,
                    100,
                    False,
                    True,
                    123,
                )

                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM shadow_position").fetchone()[0],
                    0,
                )

                obs.on_bbo({"coin": "BTC", "bbo": [{"px": "99"}, {"px": "101"}]})
                await asyncio.sleep(0.1)

            row = db.execute(
                "SELECT status,entry_px,master_open_px FROM shadow_position WHERE addr='0xmaker'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "open")
            self.assertEqual(row["entry_px"], 100)
            self.assertEqual(row["master_open_px"], 100)
            act = db.execute("SELECT action,maker,our_px FROM shadow_action").fetchone()
            self.assertEqual(act["action"], "open")
            self.assertEqual(act["maker"], 1)
            self.assertEqual(act["our_px"], 100)

        asyncio.run(run())

    def test_pending_maker_open_is_cancelled_when_target_leaves_side(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs._load_account(obs.maker)
            obs.vol["BTC"] = 0.03
            for tier in obs.tier_min_notional:
                obs.tier_min_notional[tier] = 0

            async def ensure_vol(_coin):
                return None

            obs._ensure_vol = ensure_vol
            t = now_ms()
            obs.px_ext["BTC"] = [100.0, 101.0, t]
            with patch.object(obs, "_target_snapshot", return_value=(5, 5, 1000, 100)):
                obs._dispatch_fill(
                    obs.maker, "0xmaker", "BTC", ("0xmaker", "BTC"),
                    t, 1, 0, 1, 100, False, True, 123,
                )
                self.assertIn(("0xmaker", "BTC"), obs.pending_maker_opens)

                obs._dispatch_fill(
                    obs.maker, "0xmaker", "BTC", ("0xmaker", "BTC"),
                    t + 1000, -1, 1, 0, 99, False, False, 124,
                )
                self.assertNotIn(("0xmaker", "BTC"), obs.pending_maker_opens)

                obs.on_bbo({"coin": "BTC", "bbo": [{"px": "99"}, {"px": "101"}]})
                await asyncio.sleep(0.1)

            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM shadow_position").fetchone()[0],
                0,
            )

        asyncio.run(run())

    def _seed_shadow_ep(self, db, obs, *, addr="0xmaker", coin="BTC", side="long"):
        pos_id = db.execute(
            "INSERT INTO shadow_position "
            "(addr,coin,side,status,master_open_ms,master_open_px,master_peak_sz,leverage,margin,notional,"
            "entry_px,size,rem_size,liq_px,stop_px,realized_pnl,add_count,opened_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (addr, coin, side, "open", now_ms(), 100, 1, 5, 100, 500, 100, 5, 5, 80, 0, 0, 0,
             "2026-01-01T00:00:00Z"),
        ).lastrowid
        db.commit()
        ready = asyncio.Event()
        ready.set()
        ep = {
            "pos_id": pos_id,
            "side": side,
            "sign": 1 if side == "long" else -1,
            "master_open_ms": now_ms(),
            "master_open_px": 100,
            "master_peak": 1,
            "open_maker": True,
            "open_oid": 1,
            "leverage": 5,
            "margin": 100,
            "notional": 500,
            "entry_px": 100,
            "size": 5,
            "rem_size": 5,
            "liq_px": 80,
            "stop_px": 0,
            "realized_pnl": 0,
            "add_count": 0,
            "entries_ready": ready,
            "lock": asyncio.Lock(),
            "mae": 0,
            "num_actions": 0,
            "gap": False,
            "seen_oids": {1},
            "first_margin": 100,
            "master_first_notl": 500,
            "last_add_px": 100,
        }
        obs.maker.open_ep[(addr, coin)] = ep
        return ep

    def test_pending_maker_add_fills_when_price_later_trades_through(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs._load_account(obs.maker)
            obs.vol["BTC"] = 0.03
            obs.add_strategy = "hardcap"
            obs.tier_min_notional = {k: 0 for k in obs.tier_min_notional}
            ep = self._seed_shadow_ep(db, obs)
            t = now_ms()
            obs.px_ext["BTC"] = [96.0, 101.0, t]

            obs._dispatch_fill(
                obs.maker,
                "0xmaker",
                "BTC",
                ("0xmaker", "BTC"),
                t,
                1,
                1,
                2,
                95,
                False,
                True,
                222,
            )
            await asyncio.sleep(0.05)
            self.assertEqual(ep["add_count"], 0)

            obs.on_bbo({"coin": "BTC", "bbo": [{"px": "94"}, {"px": "96"}]})
            await asyncio.sleep(0.1)

            row = db.execute("SELECT add_count,entry_px,margin FROM shadow_position WHERE pos_id=?",
                             (ep["pos_id"],)).fetchone()
            self.assertEqual(row["add_count"], 1)
            self.assertLess(row["entry_px"], 100)
            self.assertGreater(row["margin"], 100)
            act = db.execute(
                "SELECT action,maker,our_px FROM shadow_action WHERE pos_id=? ORDER BY act_id DESC LIMIT 1",
                (ep["pos_id"],),
            ).fetchone()
            self.assertEqual(act["action"], "add")
            self.assertEqual(act["maker"], 1)
            self.assertEqual(act["our_px"], 95)

        asyncio.run(run())

    def test_pending_maker_close_fills_when_price_later_trades_through(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs._load_account(obs.maker)
            obs.vol["BTC"] = 0.03
            ep = self._seed_shadow_ep(db, obs)
            t = now_ms()
            obs.px_ext["BTC"] = [98.0, 99.0, t]

            obs._dispatch_fill(
                obs.maker,
                "0xmaker",
                "BTC",
                ("0xmaker", "BTC"),
                t,
                -1,
                1,
                0,
                110,
                False,
                True,
                333,
            )
            await asyncio.sleep(0.05)
            self.assertEqual(
                db.execute("SELECT status FROM shadow_position WHERE pos_id=?", (ep["pos_id"],)).fetchone()["status"],
                "open",
            )

            obs.on_bbo({"coin": "BTC", "bbo": [{"px": "109"}, {"px": "111"}]})
            await asyncio.sleep(0.1)

            row = db.execute(
                "SELECT status,realized_pnl,rem_size FROM shadow_position WHERE pos_id=?",
                (ep["pos_id"],),
            ).fetchone()
            self.assertEqual(row["status"], "closed")
            self.assertEqual(row["rem_size"], 0)
            self.assertGreater(row["realized_pnl"], 0)
            act = db.execute(
                "SELECT action,maker,our_px FROM shadow_action WHERE pos_id=? ORDER BY act_id DESC LIMIT 1",
                (ep["pos_id"],),
            ).fetchone()
            self.assertEqual(act["action"], "close")
            self.assertEqual(act["maker"], 1)
            self.assertEqual(act["our_px"], 110)

        asyncio.run(run())

    def test_pending_maker_reduce_fills_when_price_later_trades_through(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs._load_account(obs.maker)
            obs.vol["BTC"] = 0.03
            ep = self._seed_shadow_ep(db, obs)
            t = now_ms()
            obs.px_ext["BTC"] = [98.0, 99.0, t]

            obs._dispatch_fill(
                obs.maker,
                "0xmaker",
                "BTC",
                ("0xmaker", "BTC"),
                t,
                -0.5,
                1,
                0.5,
                110,
                False,
                True,
                444,
            )
            await asyncio.sleep(0.05)
            self.assertEqual(
                db.execute("SELECT rem_size FROM shadow_position WHERE pos_id=?", (ep["pos_id"],)).fetchone()["rem_size"],
                5,
            )

            obs.on_bbo({"coin": "BTC", "bbo": [{"px": "109"}, {"px": "111"}]})
            await asyncio.sleep(0.1)

            row = db.execute(
                "SELECT status,realized_pnl,rem_size FROM shadow_position WHERE pos_id=?",
                (ep["pos_id"],),
            ).fetchone()
            self.assertEqual(row["status"], "open")
            self.assertAlmostEqual(row["rem_size"], 2.5)
            self.assertGreater(row["realized_pnl"], 0)
            act = db.execute(
                "SELECT action,maker,our_px FROM shadow_action WHERE pos_id=? ORDER BY act_id DESC LIMIT 1",
                (ep["pos_id"],),
            ).fetchone()
            self.assertEqual(act["action"], "reduce")
            self.assertEqual(act["maker"], 1)
            self.assertEqual(act["our_px"], 110)

        asyncio.run(run())

    def test_pending_maker_flip_closes_old_side_then_opens_new_side(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs._load_account(obs.maker)
            obs.vol["BTC"] = 0.03
            for tier in obs.tier_min_notional:
                obs.tier_min_notional[tier] = 0

            async def ensure_vol(_coin):
                return None

            obs._ensure_vol = ensure_vol
            ep = self._seed_shadow_ep(db, obs)
            t = now_ms()
            obs.px_ext["BTC"] = [98.0, 99.0, t]

            with patch.object(obs, "_target_snapshot", return_value=(5, 5, 1000, 110)):
                obs._dispatch_fill(
                    obs.maker,
                    "0xmaker",
                    "BTC",
                    ("0xmaker", "BTC"),
                    t,
                    -2,
                    1,
                    -1,
                    110,
                    False,
                    True,
                    555,
                )
                await asyncio.sleep(0.05)
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM shadow_position WHERE status='open'").fetchone()[0],
                    1,
                )

                obs.on_bbo({"coin": "BTC", "bbo": [{"px": "109"}, {"px": "111"}]})
                await asyncio.sleep(0.2)

            rows = db.execute(
                "SELECT side,status,entry_px,realized_pnl FROM shadow_position ORDER BY pos_id"
            ).fetchall()
            self.assertEqual([(r["side"], r["status"]) for r in rows], [("long", "closed"), ("short", "open")])
            self.assertGreater(rows[0]["realized_pnl"], 0)
            self.assertEqual(rows[1]["entry_px"], 110)

            actions = db.execute(
                "SELECT action,maker,our_px FROM shadow_action ORDER BY act_id"
            ).fetchall()
            self.assertEqual([a["action"] for a in actions[-2:]], ["close", "open"])
            self.assertEqual([a["maker"] for a in actions[-2:]], [1, 1])
            self.assertEqual([a["our_px"] for a in actions[-2:]], [110, 110])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
