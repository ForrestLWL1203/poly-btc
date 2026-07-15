import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hl import config, storage
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

    def _live_ep(self, pos_id, side, entry_px, size):
        ready = asyncio.Event()
        ready.set()
        return {
            "pos_id": pos_id,
            "side": side,
            "sign": 1 if side == "long" else -1,
            "entry_px": entry_px,
            "leverage": 5,
            "margin": 100,
            "notional": entry_px * size,
            "size": size,
            "rem_size": size,
            "realized_pnl": 0.0,
            "mae": 0.0,
            "num_actions": 0,
            "master_peak": size,
            "entries_ready": ready,
            "lock": asyncio.Lock(),
        }

    def test_stats_snapshot_reuses_startup_lifetime_counters(self):
        db = self._db()
        db.execute(
            "INSERT INTO copy_account (id,initial_balance,balance,updated_at) VALUES (1,10000,10010,'now')"
        )
        db.execute(
            "INSERT INTO copy_position (addr,coin,side,status,realized_pnl,opened_at,closed_at) "
            "VALUES ('0xclosed','SOL','long','closed',10,'2026-01-01','2026-01-02')"
        )
        db.execute(
            "INSERT INTO copy_action (pos_id,addr,coin,ts,action,our_qty_delta,our_px) "
            "VALUES (99,'0xclosed','SOL',1,'close',-2,100)"
        )
        db.commit()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {})
            obs._load_account()
            statements = []
            db.set_trace_callback(statements.append)

            obs._write_stats()
            db.set_trace_callback(None)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        snap = db.execute("SELECT closed_n,win_rate,fees_cum FROM account_stats ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(snap["closed_n"], 1)
        self.assertEqual(snap["win_rate"], 1.0)
        self.assertAlmostEqual(snap["fees_cum"], 200 * config.TAKER_FEE)
        sql = " ".join(statements)
        self.assertNotIn("FROM copy_position WHERE status!='open'", sql)
        self.assertNotIn("FROM copy_action", sql)

    def test_margin_equity_reload_changes_future_sizing_not_existing_positions(self):
        db = self._db()
        before = db.execute(
            "SELECT margin,notional,size FROM copy_position WHERE addr='0xaaa'"
        ).fetchone()
        obs = Observer(db, [], {})

        obs._reload_params({"MARGIN_EQUITY_PCT": 0.5})

        after = db.execute(
            "SELECT margin,notional,size FROM copy_position WHERE addr='0xaaa'"
        ).fetchone()
        self.assertEqual(obs.margin_equity_pct, 0.5)
        self.assertEqual(obs._open_sizing_params().margin_equity_pct, 0.5)
        self.assertEqual(tuple(after), tuple(before))

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

    def test_manual_close_uses_taker_bid_for_long(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            obs.taker.open_ep[("0xaaa", "BTC")] = self._live_ep(pos_id, "long", 100, 2)
            obs.bbo["BTC"] = (99.0, 101.0)

            res = await obs._cmd_close(pos_id)

            self.assertEqual(res["exit"], 99.0)
            action = db.execute("SELECT our_px FROM copy_action WHERE pos_id=?", (pos_id,)).fetchone()
            self.assertEqual(action["our_px"], 99.0)

        asyncio.run(run())

    def test_manual_close_uses_taker_ask_for_short(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "INSERT INTO copy_position "
                "(addr,coin,side,status,entry_px,leverage,margin,notional,size,rem_size,opened_at) "
                "VALUES ('0xshort','ETH','short','open',200,5,100,200,1,1,'2026-01-01T00:00:00Z')"
            ).lastrowid
            db.commit()
            obs = Observer(db, [], {})
            obs.taker.open_ep[("0xshort", "ETH")] = self._live_ep(pos_id, "short", 200, 1)
            obs.bbo["ETH"] = (198.0, 202.0)

            res = await obs._cmd_close(pos_id)

            self.assertEqual(res["exit"], 202.0)
            action = db.execute("SELECT our_px FROM copy_action WHERE pos_id=?", (pos_id,)).fetchone()
            self.assertEqual(action["our_px"], 202.0)

        asyncio.run(run())

    def test_near_full_target_reduce_closes_our_dust_position(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            ep = self._live_ep(pos_id, "long", 100, 2)
            obs.taker.open_ep[("0xaaa", "BTC")] = ep

            await obs._apply_reduce(
                "0xaaa",
                "BTC",
                ep,
                now_ms(),
                101.0,
                -99.9999,
                0.0001,
                closing=False,
                liq=False,
                forced_px=101.0,
            )

            row = db.execute(
                "SELECT status,rem_size,realized_pnl FROM copy_position WHERE pos_id=?",
                (pos_id,),
            ).fetchone()
            self.assertEqual(row["status"], "closed")
            self.assertEqual(row["rem_size"], 0)
            self.assertGreater(row["realized_pnl"], 0)
            self.assertNotIn(("0xaaa", "BTC"), obs.taker.open_ep)
            action = db.execute(
                "SELECT action,our_qty_delta FROM copy_action WHERE pos_id=?",
                (pos_id,),
            ).fetchone()
            self.assertEqual(action["action"], "close")
            self.assertAlmostEqual(action["our_qty_delta"], -2)

        asyncio.run(run())

    def test_reload_closes_existing_open_dust_position(self):
        db = self._db()
        pos_id = db.execute(
            "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
        ).fetchone()["pos_id"]
        db.execute("UPDATE copy_position SET rem_size=? WHERE pos_id=?", (0.0001, pos_id))
        db.commit()

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {})
            obs._reload_open()

            row = db.execute(
                "SELECT status,rem_size FROM copy_position WHERE pos_id=?",
                (pos_id,),
            ).fetchone()
            self.assertEqual(row["status"], "closed")
            self.assertEqual(row["rem_size"], 0)
            self.assertNotIn(("0xaaa", "BTC"), obs.taker.open_ep)
            action = db.execute(
                "SELECT action,our_qty_delta FROM copy_action WHERE pos_id=?",
                (pos_id,),
            ).fetchone()
            self.assertEqual(action["action"], "close")
            self.assertAlmostEqual(action["our_qty_delta"], -0.0001)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_reload_reconstructs_peak_size_from_actions_for_existing_position(self):
        db = self._db()
        pos_id = db.execute(
            "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
        ).fetchone()["pos_id"]
        db.execute(
            "UPDATE copy_position SET size=6,rem_size=3,peak_size=NULL WHERE pos_id=?",
            (pos_id,),
        )
        db.executemany(
            "INSERT INTO copy_action (pos_id,addr,coin,ts,action,our_qty_delta,our_px) "
            "VALUES (?,'0xaaa','BTC',?,?,?,100)",
            [
                (pos_id, 1, "open", 4.0),
                (pos_id, 2, "reduce", -3.0),
                (pos_id, 3, "add", 2.0),
            ],
        )
        db.commit()

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {})
            obs._reload_open()

            self.assertEqual(obs.taker.open_ep[("0xaaa", "BTC")]["peak_size"], 4.0)
            stored = db.execute(
                "SELECT peak_size FROM copy_position WHERE pos_id=?", (pos_id,),
            ).fetchone()["peak_size"]
            self.assertEqual(stored, 4.0)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_reload_reconstructs_exact_smart_add_anchors_from_actions(self):
        db = self._db()
        pos_id = db.execute(
            "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
        ).fetchone()["pos_id"]
        db.execute(
            "UPDATE copy_position SET leverage=5,margin=260,add_count=2,master_margin=20,"
            "master_leverage=5,entry_px=110 WHERE pos_id=?",
            (pos_id,),
        )
        db.executemany(
            "INSERT INTO copy_action "
            "(pos_id,addr,coin,ts,action,master_oid,master_px,our_qty_delta,our_px) "
            "VALUES (?,'0xaaa','BTC',?,?,?,?,?,?)",
            [
                (pos_id, 1, "open", 10, 100, 5.0, 100),
                (pos_id, 2, "add", 11, 108, 2.0, 108),
                (pos_id, 3, "add", 12, 115, 1.0, 115),
            ],
        )
        db.commit()

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {})
            obs._reload_open()

            ep = obs.taker.open_ep[("0xaaa", "BTC")]
            self.assertEqual(ep["first_margin"], 100.0)
            self.assertEqual(ep["last_add_px"], 115.0)
            self.assertEqual(ep["master_first_notl"], 100.0)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_target_reduce_closes_profitable_risky_tail(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            ep = self._live_ep(pos_id, "long", 100, 2)
            ep.update(peak_size=2, liq_px=80, realized_pnl=1)
            obs.taker.open_ep[("0xaaa", "BTC")] = ep

            await obs._apply_reduce(
                "0xaaa", "BTC", ep, now_ms(), 110.0, -1.3, 0.7,
                closing=False, liq=False, forced_px=110.0,
            )

            row = db.execute(
                "SELECT status,rem_size,realized_pnl FROM copy_position WHERE pos_id=?", (pos_id,),
            ).fetchone()
            self.assertEqual(row["status"], "tail_closed")
            self.assertEqual(row["rem_size"], 0)
            self.assertGreater(row["realized_pnl"], 0)
            self.assertNotIn(("0xaaa", "BTC"), obs.taker.open_ep)
        asyncio.run(run())

    def test_manual_full_loss_adds_wallet_coin_cooldown(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            obs.taker.open_ep[("0xaaa", "BTC")] = self._live_ep(pos_id, "long", 100, 2)
            obs.bbo["BTC"] = (99.0, 101.0)

            res = await obs._cmd_close(pos_id)

            row = db.execute(
                "SELECT addr,coin,pos_id,reason,created_at,expires_at FROM manual_close_cooldown "
                "WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["pos_id"], pos_id)
            self.assertEqual(row["reason"], "manual_stop_loss")
            self.assertGreater(row["expires_at"], row["created_at"])
            self.assertEqual(res["cooldownUntil"], row["expires_at"])

        asyncio.run(run())

    def test_manual_full_profit_does_not_add_cooldown(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            obs.taker.open_ep[("0xaaa", "BTC")] = self._live_ep(pos_id, "long", 100, 2)
            obs.bbo["BTC"] = (110.0, 111.0)

            res = await obs._cmd_close(pos_id)

            self.assertGreater(res["realizedPnl"], 0)
            self.assertIsNone(res["cooldownUntil"])
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM manual_close_cooldown"
            ).fetchone()[0], 0)
            obs.target_sector_policy = {
                "0xaaa": {"allowed": ["crypto"], "crypto": {"allow": True}}
            }
            with patch.object(obs, "_open_position") as open_position:
                obs._dispatch_fill(
                    "0xaaa", "BTC", ("0xaaa", "BTC"), now_ms(),
                    1.0, 0.0, 1.0, 109.0, False, 125,
                )
            open_position.assert_called_once()

        asyncio.run(run())

    def test_manual_partial_close_does_not_add_cooldown(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            obs.taker.open_ep[("0xaaa", "BTC")] = self._live_ep(pos_id, "long", 100, 2)
            obs.bbo["BTC"] = (99.0, 101.0)

            res = await obs._cmd_close(pos_id, frac=0.5)

            n = db.execute("SELECT COUNT(*) FROM manual_close_cooldown").fetchone()[0]
            self.assertEqual(n, 0)
            self.assertFalse(res["closed"])
            self.assertIsNone(res.get("cooldownUntil"))

        asyncio.run(run())

    def test_manual_partial_loss_keeps_following_target_adds(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            ep = self._live_ep(pos_id, "long", 100, 2)
            obs.taker.open_ep[("0xaaa", "BTC")] = ep
            obs.target_sector_policy = {
                "0xaaa": {"allowed": ["crypto"], "crypto": {"allow": True}}
            }
            obs.bbo["BTC"] = (99.0, 101.0)
            await obs._cmd_close(pos_id, frac=0.5)

            with patch.object(obs, "_apply_add", new_callable=AsyncMock) as apply_add:
                obs._dispatch_fill(
                    "0xaaa", "BTC", ("0xaaa", "BTC"), now_ms(),
                    1.0, 2.0, 3.0, 98.0, False, 123,
                )
                await asyncio.sleep(0)

            apply_add.assert_awaited_once()
            self.assertIn(("0xaaa", "BTC"), obs.taker.open_ep)

        asyncio.run(run())

    def test_manual_partial_profit_keeps_following_target_reduces(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            ep = self._live_ep(pos_id, "long", 100, 2)
            obs.taker.open_ep[("0xaaa", "BTC")] = ep
            obs.bbo["BTC"] = (110.0, 111.0)
            await obs._cmd_close(pos_id, frac=0.5)

            with patch.object(obs, "_apply_reduce", new_callable=AsyncMock) as apply_reduce:
                obs._dispatch_fill(
                    "0xaaa", "BTC", ("0xaaa", "BTC"), now_ms(),
                    -2.0, 2.0, 0.0, 109.0, False, 124,
                )
                await asyncio.sleep(0)

            apply_reduce.assert_awaited_once()
            self.assertTrue(apply_reduce.await_args.kwargs["closing"])
            self.assertIn(("0xaaa", "BTC"), obs.taker.open_ep)

        asyncio.run(run())

    def test_manual_cooldown_blocks_new_open_same_wallet_coin(self):
        db = self._db()
        db.execute(
            "INSERT INTO manual_close_cooldown (addr,coin,pos_id,reason,created_at,expires_at) "
            "VALUES ('0xaaa','BTC',123,'manual_close','2026-01-01T00:00:00Z','2999-01-01T00:00:00Z')"
        )
        db.commit()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {})
            obs.target_sector_policy = {
                "0xaaa": {"allowed": ["crypto"], "crypto": {"allow": True}}
            }

            with patch.object(obs, "_open_position") as open_position:
                obs._dispatch_fill(
                    "0xaaa",
                    "BTC",
                    ("0xaaa", "BTC"),
                    1_000,
                    1,
                    0,
                    1,
                    100,
                    False,
                    1,
                )

            open_position.assert_not_called()
            self.assertEqual(obs.hb.get("skip_manual_cooldown"), 1)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_restart_prunes_legacy_profitable_but_keeps_losing_cooldown(self):
        async def run():
            db = self._db()
            profit_pos = db.execute(
                "INSERT INTO copy_position "
                "(addr,coin,side,status,entry_px,realized_pnl,opened_at,closed_at) "
                "VALUES ('0xprofit','SOL','long','closed',100,12,'old','old')"
            ).lastrowid
            loss_pos = db.execute(
                "INSERT INTO copy_position "
                "(addr,coin,side,status,entry_px,realized_pnl,opened_at,closed_at) "
                "VALUES ('0xloss','HYPE','long','closed',100,-12,'old','old')"
            ).lastrowid
            db.executemany(
                "INSERT INTO manual_close_cooldown "
                "(addr,coin,pos_id,reason,created_at,expires_at) VALUES (?,?,?,?,?,?)",
                [
                    ("0xprofit", "SOL", profit_pos, "manual_close", "old", "2999-01-01T00:00:00Z"),
                    ("0xloss", "HYPE", loss_pos, "manual_close", "old", "2999-01-01T00:00:00Z"),
                ],
            )
            db.commit()

            Observer(db, [], {})._reload_open()

            rows = db.execute(
                "SELECT addr,coin FROM manual_close_cooldown ORDER BY addr"
            ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [("0xloss", "HYPE")])

        asyncio.run(run())

    def test_expired_manual_cooldown_allows_new_open(self):
        db = self._db()
        db.execute(
            "INSERT INTO manual_close_cooldown (addr,coin,pos_id,reason,created_at,expires_at) "
            "VALUES ('0xaaa','BTC',123,'manual_close','2026-01-01T00:00:00Z','2026-01-02T00:00:00Z')"
        )
        db.commit()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {})
            obs.target_sector_policy = {
                "0xaaa": {"allowed": ["crypto"], "crypto": {"allow": True}}
            }

            with patch.object(obs, "_open_position") as open_position:
                obs._dispatch_fill(
                    "0xaaa",
                    "BTC",
                    ("0xaaa", "BTC"),
                    1_000,
                    1,
                    0,
                    1,
                    100,
                    False,
                    1,
                )

            open_position.assert_called_once()
            self.assertIsNone(
                db.execute(
                    "SELECT expires_at FROM manual_close_cooldown WHERE addr='0xaaa' AND coin='BTC'"
                ).fetchone()
            )
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_low_liquidity_crypto_open_is_skipped(self):
        async def run():
            db = self._db()
            db.execute(
                "INSERT INTO coin_vol "
                "(coin,sigma,sigma_fast,sigma_slow,n,day_ntl_vlm,open_interest,mark_px,oi_notional,updated_at,market_ctx_updated_at) "
                "VALUES ('VINE',0.12,0.12,0.10,30,1600000,60000000,0.0098,588000,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
            )
            db.commit()
            obs = Observer(db, [], {})
            obs.vol["VINE"] = 0.12

            with patch.object(obs, "_target_snapshot") as target_snapshot:
                obs._open_position("0xaaa", "VINE", now_ms(), 0.0098, -1000, 1, obs.taker)
                await asyncio.sleep(0.05)

            target_snapshot.assert_not_called()
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM copy_position WHERE coin='VINE'").fetchone()[0],
                0,
            )
            self.assertEqual(obs.hb.get("skip_low_liquidity"), 1)

        asyncio.run(run())

    def test_low_liquidity_crypto_add_is_observe_only(self):
        async def run():
            db = self._db()
            db.execute(
                "INSERT OR REPLACE INTO coin_vol "
                "(coin,sigma,sigma_fast,sigma_slow,n,day_ntl_vlm,open_interest,mark_px,oi_notional,updated_at,market_ctx_updated_at) "
                "VALUES ('BTC',0.04,0.04,0.04,30,1000000,10,100,1000,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
            )
            db.commit()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            ep = self._live_ep(pos_id, "long", 100, 2)
            ep.update(master_open_px=100, first_margin=100, master_first_notl=200, last_add_px=100,
                      add_count=0, seen_oids={1})
            obs.taker.open_ep[("0xaaa", "BTC")] = ep

            await obs._apply_add("0xaaa", "BTC", ep, now_ms(), 101, 1, 3, 2, obs.taker)

            row = db.execute(
                "SELECT add_count,margin,master_open_px FROM copy_position WHERE pos_id=?",
                (pos_id,),
            ).fetchone()
            act = db.execute(
                "SELECT our_qty_delta FROM copy_action WHERE pos_id=? AND action='add'",
                (pos_id,),
            ).fetchone()
            self.assertEqual(row["add_count"], 0)
            self.assertEqual(row["margin"], 50)
            self.assertGreater(row["master_open_px"], 100)
            self.assertEqual(act["our_qty_delta"], 0)
            self.assertEqual(obs.hb.get("skip_low_liquidity_add"), 1)

        asyncio.run(run())

    def test_same_oid_dust_slice_accumulates_and_follows_full_add_once(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            initial_size = 939.0 * 20 / 64075.0
            db.execute(
                "UPDATE copy_position SET side='short',entry_px=64075,leverage=20,margin=939,"
                "notional=18780,size=?,rem_size=?,peak_size=?,master_open_px=64021,"
                "master_peak_sz=2,master_margin=3190.85,master_leverage=20,add_count=1 "
                "WHERE pos_id=?",
                (initial_size, initial_size, initial_size, pos_id),
            )
            db.commit()

            obs = Observer(db, [], {})
            obs.low_liquidity_filter_enable = False
            obs.add_strategy = "smart"
            obs.add_gap_k = 0.04
            obs.add_shrink_g = 1.3
            obs.add_max_hard = 10
            obs.min_open_margin_pct = 0.005
            obs.tier_coin_cap["stable"] = 0.30
            obs.vol["BTC"] = 0.034
            ep = self._live_ep(pos_id, "short", 64075, initial_size)
            ep.update(
                sign=-1,
                leverage=20,
                margin=939.0,
                notional=18780.0,
                peak_size=initial_size,
                master_open_px=64021.0,
                master_peak=2.0,
                first_margin=939.0,
                master_first_notl=63817.0,
                last_add_px=64335.0,
                add_count=1,
                seen_oids={1},
                add_orders={},
            )
            obs.taker.open_ep[("0xaaa", "BTC")] = ep
            t = now_ms()

            with patch.object(obs, "_sector_allowed", return_value=True):
                obs._dispatch_fill("0xaaa", "BTC", ("0xaaa", "BTC"), t, -0.00028,
                                   -2.0, -2.00028, 65008.0, False, 99)
                await asyncio.sleep(0.05)

                first = db.execute(
                    "SELECT our_qty_delta FROM copy_action WHERE pos_id=? AND master_oid=99 ORDER BY act_id",
                    (pos_id,),
                ).fetchall()
                self.assertEqual(len(first), 1)
                self.assertEqual(first[0]["our_qty_delta"], 0)
                self.assertNotIn(99, ep["seen_oids"])

                obs._dispatch_fill("0xaaa", "BTC", ("0xaaa", "BTC"), t + 1, -1.99972,
                                   -2.00028, -4.0, 65008.0, False, 99)
                await asyncio.sleep(0.05)

            row = db.execute(
                "SELECT add_count,margin,master_open_px,master_peak_sz FROM copy_position WHERE pos_id=?",
                (pos_id,),
            ).fetchone()
            actions = db.execute(
                "SELECT our_qty_delta FROM copy_action WHERE pos_id=? AND master_oid=99 ORDER BY act_id",
                (pos_id,),
            ).fetchall()
            self.assertEqual(row["add_count"], 2)
            self.assertAlmostEqual(row["margin"], 1878.0, places=4)
            self.assertAlmostEqual(row["master_open_px"], 64514.5, places=4)
            self.assertEqual(row["master_peak_sz"], 4.0)
            self.assertEqual(len(actions), 2)
            self.assertEqual(actions[0]["our_qty_delta"], 0)
            self.assertLess(actions[1]["our_qty_delta"], 0)
            self.assertIn(99, ep["seen_oids"])

        asyncio.run(run())

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
                closing=True, liq=False, forced_px=99,
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
        db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,publishable,is_current,started_at,published_at) "
            "VALUES ('g1','published',1,1,1,'2026-01-01','2026-01-02')"
        )
        db.execute(
            "INSERT INTO follow_selection (generation,addr,role,enabled,reason,utility,selected_at) "
            "VALUES ('g1','0xsector','core',1,'portfolio_positive_net_contribution',1,'now')"
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
                    "0xsector",
                    "xyz:MU",
                    ("0xsector", "xyz:MU"),
                    1_000,
                    10,
                    0,
                    10,
                    900,
                    False,
                    1,
                )

            open_position.assert_not_called()
            self.assertEqual(obs.hb.get("skip_sector_disabled"), 1)
        finally:
            asyncio.set_event_loop(None)
            loop.close()


if __name__ == "__main__":
    unittest.main()
