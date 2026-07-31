import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hyper import config, storage
from hyper.execution.observer import Observer
from hyper.market import volatility
from hyper.util import now_iso, now_ms


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

    @staticmethod
    def _set_bbo(obs, coin, bid, ask):
        obs.bbo[coin] = (bid, ask)
        obs.bbo_ms[coin] = now_ms()

    def test_observer_restart_preserves_operator_pause(self):
        db = self._db()
        db.execute(
            "INSERT INTO process_status (name,state) VALUES ('observer','paused') "
            "ON CONFLICT(name) DO UPDATE SET state=excluded.state"
        )
        db.commit()

        restarted = Observer(db, [], {})

        self.assertTrue(restarted.paused)
        self.assertEqual(restarted._proc_state, "paused")

    def test_portfolio_drawdown_stop_pauses_flattens_and_survives_restart(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs._load_account()
            obs.taker.open_ep[("0xrisk", "BTC")] = self._live_ep(1, "long", 100, 100)
            obs.mark_mid["BTC"] = 84.9

            with patch.object(obs, "_cmd_close_all", new=AsyncMock(return_value={"count": 1})) as close_all:
                result = await obs._check_portfolio_drawdown_stop()

            self.assertTrue(result["active"])
            self.assertGreater(result["drawdown"], 0.15)
            self.assertTrue(obs.paused)
            close_all.assert_awaited_once()
            state = db.execute(
                "SELECT equity_high_water,drawdown_stop_active,drawdown_stopped_at "
                "FROM copy_account WHERE id=1"
            ).fetchone()
            self.assertEqual(state["equity_high_water"], 10_000)
            self.assertEqual(state["drawdown_stop_active"], 1)
            self.assertTrue(state["drawdown_stopped_at"])

            restarted = Observer(db, [], {})
            restarted._load_account()
            self.assertTrue(restarted.paused)
            self.assertTrue(restarted.taker.drawdown_stop_active)

        asyncio.run(run())

    def test_manual_resume_rebases_portfolio_drawdown_high_water(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs._load_account()
            obs.taker.balance = 8_000
            obs.taker.equity_high_water = 10_000
            obs.taker.drawdown_stop_active = True
            obs.paused = True
            obs._save_portfolio_risk_state(force=True)

            result = await obs._dispatch_command("resume", {})

            self.assertFalse(result["paused"])
            self.assertFalse(obs.taker.drawdown_stop_active)
            self.assertEqual(obs.taker.equity_high_water, 8_000)

            restarted = Observer(db, [], {})
            restarted._load_account()
            self.assertEqual(restarted.taker.equity_high_water, 8_000)

        asyncio.run(run())

    def test_normal_pause_resume_keeps_existing_high_water(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs._load_account()
            obs.taker.balance = 8_000
            obs.taker.equity_high_water = 12_000
            obs.paused = True
            obs._save_portfolio_risk_state(force=True)

            await obs._dispatch_command("resume", {})

            self.assertEqual(obs.taker.equity_high_water, 12_000)

        asyncio.run(run())

    def test_null_sigma_placeholder_is_not_loaded_as_warm_cache(self):
        db = self._db()
        db.execute(
            "INSERT INTO coin_vol (coin,sigma,updated_at) VALUES ('xyz:SP500',NULL,'now')"
        )
        db.commit()

        self.assertNotIn("xyz:SP500", volatility.load_all(db))

    def test_ensure_vol_refreshes_existing_null_placeholder(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs.vol["xyz:SP500"] = None

            with patch("hyper.execution.observer.volatility.refresh", return_value=0.0095) as refresh:
                await obs._ensure_vol("xyz:SP500")

            refresh.assert_called_once_with(db, "xyz:SP500")
            self.assertAlmostEqual(obs.vol["xyz:SP500"], 0.0095)

        asyncio.run(run())

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

        obs._reload_params({
            "MARGIN_EQUITY_PCT": 0.5,
            "WALLET_SECTOR_SIDE_CAP_PCT": 0.45,
        })

        after = db.execute(
            "SELECT margin,notional,size FROM copy_position WHERE addr='0xaaa'"
        ).fetchone()
        self.assertEqual(obs.margin_equity_pct, 0.5)
        self.assertEqual(obs.wallet_sector_side_cap_pct, 1.0)
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

    def test_exchange_mark_overrides_book_mid_and_drives_liquidation_check(self):
        db = self._db()
        db.execute(
            "INSERT INTO copy_position "
            "(addr,coin,side,status,entry_px,leverage,margin,notional,size,rem_size,opened_at) "
            "VALUES ('0xccc','xyz:MU','long','open',900,5,50,900,1,1,'2026-01-01T00:00:00Z')"
        )
        db.commit()
        obs = Observer(db, [], {})
        self._set_bbo(obs, "xyz:MU", 941, 943)

        with patch.object(obs, "_maybe_liquidate") as liquidate:
            applied = obs._apply_authoritative_marks(
                {"xyz:MU": {"markPx": "937", "midPx": "942"}},
                {"xyz:MU"},
            )

        mu = db.execute("SELECT mark_px,unrealized_pnl FROM copy_position WHERE coin='xyz:MU'").fetchone()
        self.assertEqual(applied, 1)
        self.assertEqual(mu["mark_px"], 937)
        self.assertEqual(mu["unrealized_pnl"], 37)
        liquidate.assert_called_once_with("xyz:MU", 937, obs.taker)

    def test_mid_without_exchange_mark_cannot_drive_liquidation(self):
        db = self._db()
        obs = Observer(db, [], {})

        with patch.object(obs, "_maybe_liquidate") as liquidate:
            applied = obs._apply_authoritative_marks(
                {"BTC": {"midPx": "150", "oraclePx": "151"}},
                {"BTC"},
            )

        self.assertEqual(applied, 0)
        self.assertNotIn("BTC", obs.mark_mid)
        liquidate.assert_not_called()

    def test_bbo_tick_never_drives_liquidation(self):
        db = self._db()
        obs = Observer(db, [], {})

        with patch.object(obs, "_maybe_liquidate") as liquidate:
            obs.on_bbo({"coin": "BTC", "bbo": [{"px": "149"}, {"px": "151"}]})

        liquidate.assert_not_called()

    def test_execution_price_uses_only_fresh_cached_crypto_quote(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            self._set_bbo(obs, "BTC", 99.0, 101.0)
            self.assertEqual(await obs._execution_px("BTC", True, 105.0), 101.0)

            obs.bbo_ms["BTC"] = now_ms() - config.EXECUTION_QUOTE_MAX_AGE_MS - 1
            self.assertEqual(await obs._execution_px("BTC", True, 105.0), 105.0)

        asyncio.run(run())

    def test_stale_builder_quote_is_refetched_before_execution(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs.bbo["xyz:IBM"] = (220.0, 221.0)
            obs.bbo_ms["xyz:IBM"] = now_ms() - config.EXECUTION_QUOTE_MAX_AGE_MS - 1

            with patch("hyper.execution.observer.rest.realtime_book_top", return_value=(222.0, 223.0)) as fetch:
                px = await obs._execution_px("xyz:IBM", True, 224.0)

            self.assertEqual(px, 223.0)
            self.assertEqual(obs.bbo["xyz:IBM"], (222.0, 223.0))
            fetch.assert_called_once_with("xyz:IBM")

        asyncio.run(run())

    def test_smart_add_gap_compares_target_prices_not_our_execution_price(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            obs.low_liquidity_filter_enable = False
            obs.add_strategy = "smart"
            obs.follow_pos_add = True
            obs.add_gap_k = 0.12
            obs.pos_add_gap_k = 0.08
            obs.add_shrink_g = 1.0
            obs.min_open_margin_pct = 0.001
            obs.vol["BTC"] = 0.10
            # Our opening execution was 5% above the target.  That execution slippage must not make a
            # target add only 0.2% away from its own open look like a 4.6% adverse move.
            ep = self._live_ep(pos_id, "long", 105.0, 2.0)
            ep.update(
                margin=100.0,
                notional=200.0,
                master_open_px=100.0,
                master_peak=2.0,
                first_margin=100.0,
                master_first_notl=200.0,
                last_target_add_px=100.0,
                add_count=0,
                seen_oids={1},
                add_orders={},
            )
            obs.taker.open_ep[("0xaaa", "BTC")] = ep
            self._set_bbo(obs, "BTC", 104.9, 105.0)

            copied = await obs._apply_add(
                "0xaaa", "BTC", ep, now_ms(), 100.2, 1.0, 3.0, 2, obs.taker,
            )

            self.assertFalse(copied)
            self.assertEqual(ep["add_count"], 0)
            action = db.execute(
                "SELECT master_px,our_qty_delta FROM copy_action WHERE pos_id=? AND action='add'",
                (pos_id,),
            ).fetchone()
            self.assertEqual(action["master_px"], 100.2)
            self.assertEqual(action["our_qty_delta"], 0)

        asyncio.run(run())

    def test_manual_close_uses_taker_bid_for_long(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            obs.taker.open_ep[("0xaaa", "BTC")] = self._live_ep(pos_id, "long", 100, 2)
            self._set_bbo(obs, "BTC", 99.0, 101.0)

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
            self._set_bbo(obs, "ETH", 198.0, 202.0)

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

    def test_reload_repairs_liquidation_basis_after_reduce_then_add(self):
        db = self._db()
        pos_id = db.execute(
            "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
        ).fetchone()["pos_id"]
        db.execute(
            "INSERT OR REPLACE INTO coin_vol (coin,max_leverage) VALUES ('BTC',40)"
        )
        db.execute(
            "UPDATE copy_position SET side='short',entry_px=?,leverage=30,margin=?,notional=?,"
            "size=?,rem_size=?,liq_px=?,add_count=5 WHERE pos_id=?",
            (
                64_019.93288094258,
                2_180.857254838225,
                65_425.717645146746,
                1.0237671431979045,
                0.5756738995010446,
                65_333.49205532649,
                pos_id,
            ),
        )
        db.commit()

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {})
            obs._reload_open()

            row = db.execute(
                "SELECT size,rem_size,margin,notional,liq_px FROM copy_position WHERE pos_id=?",
                (pos_id,),
            ).fetchone()
            self.assertAlmostEqual(row["size"], row["rem_size"])
            self.assertAlmostEqual(row["notional"], 36_854.60440736736)
            self.assertAlmostEqual(row["margin"], 1_228.4868135789122)
            self.assertAlmostEqual(row["liq_px"], 65_337.2154505093)
            ep = obs.taker.open_ep[("0xaaa", "BTC")]
            self.assertAlmostEqual(ep["liq_px"], row["liq_px"])
            self.assertEqual(ep["maintenance_leverage"], 40)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_partial_reduce_persists_current_liquidation_basis(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            ep = self._live_ep(pos_id, "long", 100.0, 2.0)
            ep.update(
                leverage=5.0,
                margin=40.0,
                notional=200.0,
                liq_px=80.0,
                maintenance_leverage=None,
            )
            obs.taker.open_ep[("0xaaa", "BTC")] = ep

            await obs._apply_reduce(
                "0xaaa",
                "BTC",
                ep,
                now_ms(),
                110.0,
                0.0,
                2.0,
                closing=False,
                liq=False,
                forced_px=110.0,
                forced_frac=0.5,
                book=obs.taker,
            )

            row = db.execute(
                "SELECT size,rem_size,margin,notional,liq_px FROM copy_position WHERE pos_id=?",
                (pos_id,),
            ).fetchone()
            self.assertAlmostEqual(row["size"], 1.0)
            self.assertAlmostEqual(row["rem_size"], 1.0)
            self.assertAlmostEqual(row["margin"], 20.0)
            self.assertAlmostEqual(row["notional"], 100.0)
            self.assertAlmostEqual(row["liq_px"], 80.0)

        asyncio.run(run())

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
            self.assertEqual(ep["last_target_add_px"], 115.0)
            self.assertEqual(ep["master_first_notl"], 100.0)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_smart_take_profit_cut_persists_high_water_stage_across_restart(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            db.execute(
                "UPDATE copy_position SET master_open_px=100,master_peak_sz=2,master_current_sz=2,peak_size=2 "
                "WHERE pos_id=?",
                (pos_id,),
            )
            db.commit()
            obs = Observer(db, [], {})
            obs.smart_tp_enable = True
            obs.vol["BTC"] = 0.10
            ep = self._live_ep(pos_id, "long", 100, 2)
            ep.update(
                peak_size=2,
                liq_px=80,
                master_open_px=100,
                master_current=2,
                smart_tp_armed=False,
                smart_tp_stage=0,
                smart_tp_peak_pnl=0.0,
                smart_tp_base_size=0.0,
                smart_tp_master_anchor=0.0,
                smart_tp_inflight=False,
            )
            obs.taker.open_ep[("0xaaa", "BTC")] = ep

            self._set_bbo(obs, "BTC", 105.9, 106.1)
            obs._queue_smart_take_profit("BTC", 106.0)
            self.assertTrue(ep["smart_tp_armed"])
            self.assertEqual(ep["smart_tp_stage"], 0)

            self._set_bbo(obs, "BTC", 104.4, 104.6)
            obs._queue_smart_take_profit("BTC", 104.5)
            await asyncio.sleep(0.05)

            row = db.execute(
                "SELECT rem_size,smart_tp_armed,smart_tp_stage,smart_tp_peak_pnl,smart_tp_base_size,"
                "smart_tp_master_anchor FROM copy_position WHERE pos_id=?",
                (pos_id,),
            ).fetchone()
            self.assertAlmostEqual(row["rem_size"], 1.6)
            self.assertEqual(row["smart_tp_armed"], 1)
            self.assertEqual(row["smart_tp_stage"], 1)
            self.assertEqual(row["smart_tp_base_size"], 2)
            self.assertEqual(row["smart_tp_master_anchor"], 2)

            restarted = Observer(db, [], {})
            restarted._reload_open()
            restored = restarted.taker.open_ep[("0xaaa", "BTC")]
            self.assertTrue(restored["smart_tp_armed"])
            self.assertEqual(restored["smart_tp_stage"], 1)
            self.assertEqual(restored["smart_tp_base_size"], 2)
            self.assertGreater(restored["smart_tp_peak_pnl"], 0)

        asyncio.run(run())

    def test_smart_take_profit_tail_ignores_small_trim_then_closes_all_at_thirty_pct(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            obs.smart_tp_enable = True
            ep = self._live_ep(pos_id, "long", 100, 0.6)
            ep.update(
                size=2,
                peak_size=2,
                rem_size=0.6,
                liq_px=80,
                master_open_px=100,
                master_current=100,
                smart_tp_armed=True,
                smart_tp_stage=3,
                smart_tp_peak_pnl=6,
                smart_tp_base_size=2,
                smart_tp_master_anchor=100,
                smart_tp_inflight=False,
            )
            obs.taker.open_ep[("0xaaa", "BTC")] = ep

            await obs._apply_reduce(
                "0xaaa", "BTC", ep, now_ms(), 110, -29, 71,
                closing=False, liq=False, forced_px=110,
            )
            self.assertAlmostEqual(ep["rem_size"], 0.6)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM copy_action WHERE pos_id=?", (pos_id,)
            ).fetchone()[0], 0)

            await obs._apply_reduce(
                "0xaaa", "BTC", ep, now_ms(), 110, -1, 70,
                closing=False, liq=False, forced_px=110,
            )
            row = db.execute(
                "SELECT status,rem_size FROM copy_position WHERE pos_id=?", (pos_id,)
            ).fetchone()
            self.assertEqual(row["status"], "tail_closed")
            self.assertEqual(row["rem_size"], 0)
            self.assertNotIn(("0xaaa", "BTC"), obs.taker.open_ep)

        asyncio.run(run())

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
            self._set_bbo(obs, "BTC", 99.0, 101.0)

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

    def test_prior_liquidation_does_not_create_hidden_wallet_freeze(self):
        db = self._db()
        pos_id = db.execute(
            "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
        ).fetchone()["pos_id"]
        obs = Observer(db, [], {})
        obs.target_sector_policy = {
            "0xaaa": {"allowed": ["crypto"], "crypto": {"allow": True}}
        }
        db.execute(
            "UPDATE copy_position SET was_liq=1,status='liquidated',closed_at=? WHERE pos_id=?",
            (now_iso(), pos_id),
        )
        db.commit()

        with patch.object(obs, "_open_position") as open_position:
            obs._dispatch_fill(
                "0xaaa", "ETH", ("0xaaa", "ETH"), now_ms(),
                10.0, 0.0, 10.0, 200.0, False, 9001,
            )

        open_position.assert_called_once()
        self.assertEqual(
            db.execute(
                "SELECT COUNT(*) FROM manual_close_cooldown "
                "WHERE reason LIKE 'liquidation_%'"
            ).fetchone()[0],
            0,
        )

    def test_manual_full_profit_does_not_add_cooldown(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            obs.taker.open_ep[("0xaaa", "BTC")] = self._live_ep(pos_id, "long", 100, 2)
            self._set_bbo(obs, "BTC", 110.0, 111.0)

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
                    50.0, 0.0, 50.0, 109.0, False, 125,
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
            self._set_bbo(obs, "BTC", 99.0, 101.0)

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
            self._set_bbo(obs, "BTC", 99.0, 101.0)
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
            self._set_bbo(obs, "BTC", 110.0, 111.0)
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
                    30,
                    0,
                    30,
                    100,
                    False,
                    1,
                )

            open_position.assert_not_called()
            self.assertEqual(obs.hb.get("skip_manual_cooldown"), 1)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_source_open_waits_for_floor_then_carries_open_oid_slices(self):
        db = self._db()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {})
            obs.vol["BTC"] = 0.04
            obs.target_sector_policy = {
                "0xaaa": {"allowed": ["crypto"], "crypto": {"allow": True}}
            }
            key = ("0xaaa", "BTC")

            with patch.object(obs, "_open_position") as open_position:
                obs._dispatch_fill(
                    "0xaaa", "BTC", key, 1_000,
                    1.0, 0.0, 1.0, 1_000.0, False, 77,
                )
                open_position.assert_not_called()
                self.assertEqual(obs.taker.pending_source_opens[key]["source_notional"], 1_000.0)

                obs._dispatch_fill(
                    "0xaaa", "BTC", key, 1_050,
                    5.0, 1.0, 6.0, 1_000.0, False, 77,
                )

            open_position.assert_called_once()
            self.assertEqual(open_position.call_args.kwargs["source_open_oids"], {77})
            self.assertNotIn(key, obs.taker.pending_source_opens)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_same_source_open_oid_extension_is_not_dispatched_as_add(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            ep = self._live_ep(pos_id, "long", 1_000.0, 3.0)
            ep.update(
                addr="0xaaa",
                coin="BTC",
                master_open_px=1_000.0,
                master_peak=3.0,
                master_current=3.0,
                master_first_notl=3_000.0,
                source_open_oids={77},
                seen_oids={77},
                add_orders={},
            )
            obs.taker.open_ep[("0xaaa", "BTC")] = ep

            with patch.object(obs, "_apply_add", new_callable=AsyncMock) as apply_add:
                obs._dispatch_fill(
                    "0xaaa", "BTC", ("0xaaa", "BTC"), 1_100,
                    1.0, 3.0, 4.0, 1_000.0, False, 77,
                )
                await asyncio.sleep(0)

            apply_add.assert_not_awaited()
            self.assertEqual(ep["master_first_notl"], 4_000.0)
            persisted = db.execute(
                "SELECT master_open_notional FROM copy_position WHERE pos_id=?",
                (pos_id,),
            ).fetchone()[0]
            self.assertEqual(persisted, 4_000.0)

        asyncio.run(run())

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
                    60,
                    0,
                    60,
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
            shallow_book = {
                "levels": [
                    [{"px": "0.0097", "sz": "1000"}],
                    [{"px": "0.0099", "sz": "1000"}],
                ],
            }

            with (
                patch.object(obs, "_target_snapshot", return_value=(4, None, None, None)) as target_snapshot,
                patch("hyper.execution.observer.rest.realtime_book_snapshot", return_value=shallow_book),
            ):
                obs._open_position("0xaaa", "VINE", now_ms(), 0.0098, -100000, 1, obs.taker)
                await asyncio.sleep(0.05)

            target_snapshot.assert_called_once()
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM copy_position WHERE coin='VINE'").fetchone()[0],
                0,
            )
            self.assertEqual(obs.hb.get("skip_low_liquidity"), 1)
            audit = db.execute(
                "SELECT action,reason,count FROM live_policy_skip "
                "WHERE addr='0xaaa' AND coin='VINE'"
            ).fetchone()
            self.assertEqual(tuple(audit), ("open", "book_depth", 1))

        asyncio.run(run())

    def test_low_day_volume_with_deep_l2_allows_our_small_tao_order(self):
        async def run():
            db = self._db()
            db.execute(
                "INSERT INTO coin_vol "
                "(coin,sigma,sigma_fast,sigma_slow,n,day_ntl_vlm,open_interest,mark_px,oi_notional,"
                "max_leverage,updated_at,market_ctx_updated_at) "
                "VALUES ('TAO',0.05,0.04,0.05,30,2500000,260000,100,26000000,5,"
                "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
            )
            db.commit()
            obs = Observer(db, [], {})
            obs.vol["TAO"] = 0.05
            self._set_bbo(obs, "TAO", 99.99, 100.01)
            deep_book = {
                "levels": [
                    [{"px": "99.99", "sz": "1000"}],
                    [{"px": "100.01", "sz": "1000"}],
                ],
            }

            with (
                patch.object(obs, "_target_snapshot", return_value=(5, 5, 50000, 100)),
                patch("hyper.execution.observer.rest.realtime_book_snapshot", return_value=deep_book),
            ):
                obs._open_position("0xaaa", "TAO", now_ms(), 100, 2500, 1, obs.taker)
                await asyncio.sleep(0.05)

            position = db.execute(
                "SELECT status,notional,entry_px,opening_account_equity "
                "FROM copy_position WHERE addr='0xaaa' AND coin='TAO'"
            ).fetchone()
            self.assertIsNotNone(position)
            self.assertEqual(position["status"], "open")
            self.assertGreaterEqual(position["notional"], 1000)
            self.assertAlmostEqual(position["entry_px"], 100.01, places=6)
            self.assertIsNotNone(position["opening_account_equity"])
            self.assertGreater(position["opening_account_equity"], 0)
            self.assertIsNone(
                db.execute(
                    "SELECT reason FROM live_policy_skip WHERE addr='0xaaa' AND coin='TAO'"
                ).fetchone()
            )

        asyncio.run(run())

    def test_missing_l2_only_blocks_when_volume_and_oi_are_both_weak(self):
        db = self._db()
        db.executemany(
            "INSERT INTO coin_vol "
            "(coin,sigma,day_ntl_vlm,oi_notional,updated_at,market_ctx_updated_at) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("TAO", 0.05, 2_500_000, 26_000_000, now_iso(), now_iso()),
                ("VINE", 0.12, 1_600_000, 588_000, now_iso(), now_iso()),
            ],
        )
        db.commit()
        obs = Observer(db, [], {})

        tao = obs._coin_liquidity_decision(
            "TAO", book_snapshot=None, is_buy=True, planned_notional=2_700,
        )
        vine = obs._coin_liquidity_decision(
            "VINE", book_snapshot=None, is_buy=True, planned_notional=800,
        )

        self.assertIsNone(tao["reason"])
        self.assertEqual(vine["reason"], "fallback_volume_and_open_interest")

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
            ep.update(master_open_px=100, first_margin=100, master_first_notl=200,
                      last_target_add_px=100,
                      add_count=0, seen_oids={1})
            obs.taker.open_ep[("0xaaa", "BTC")] = ep

            shallow_book = {
                "levels": [
                    [{"px": "100.9", "sz": "0.01"}],
                    [{"px": "101.1", "sz": "0.01"}],
                ],
            }
            with patch(
                "hyper.execution.observer.rest.realtime_book_snapshot", return_value=shallow_book,
            ):
                await obs._apply_add(
                    "0xaaa", "BTC", ep, now_ms(), 101, 1, 3, 2, obs.taker,
                )

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
            audit = db.execute(
                "SELECT action,reason,count FROM live_policy_skip "
                "WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()
            self.assertEqual(tuple(audit), ("add", "book_depth", 1))

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
                last_target_add_px=64335.0,
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

                # The OID is final after its first successful Copy add. A later exchange fill updates
                # source exposure but cannot submit another Copy order.
                obs._dispatch_fill("0xaaa", "BTC", ("0xaaa", "BTC"), t + 2, -1.0,
                                   -4.0, -5.0, 65008.0, False, 99)
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
            self.assertAlmostEqual(row["master_open_px"], 64613.2, places=4)
            self.assertEqual(row["master_peak_sz"], 5.0)
            self.assertEqual(len(actions), 2)
            self.assertEqual(actions[0]["our_qty_delta"], 0)
            self.assertLess(actions[1]["our_qty_delta"], 0)
            self.assertIn(99, ep["seen_oids"])
            self.assertNotIn(99, ep["add_orders"])
            self.assertEqual(ep["master_current"], 5.0)

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
            obs = Observer(db, [], {}, top_n=10)

            obs._reload_targets(init=True)

            self.assertIn("0xsector", obs.addrs)
            self.assertFalse(obs.target_sector_policy["0xsector"]["stock"]["allow"])
            self.assertTrue(obs.target_sector_policy["0xsector"]["crypto"]["allow"])
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_low_risk_target_stays_tracked_and_keeps_entry_permission(self):
        db = self._db()
        db.execute(
            "INSERT INTO watchlist (rank,addr,score,acct_value,sector_policy_json,updated_at) "
            "VALUES (1,'0xprobation',0.9,10000,?,'now')",
            ('{"crypto":{"allow":true},"allowed":["crypto"]}',),
        )
        db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,publishable,is_current,started_at,published_at) "
            "VALUES ('g1','published',1,1,1,'2026-01-01T00:00:00Z','2026-01-02T00:00:00Z')"
        )
        db.execute(
            "INSERT INTO follow_selection "
            "(generation,addr,role,enabled,entry_eligible,retention_status,"
            "retention_failure_streak,selected_at) "
            "VALUES ('g1','0xprobation','core',1,1,'probation',1,'now')"
        )
        db.commit()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {}, top_n=10)
            obs.selection_generation = "g1"
            obs._reload_targets(init=True)

            self.assertIn("0xprobation", obs.addrs)
            self.assertNotIn("0xprobation", obs.entry_frozen)
            self.assertIsNone(
                obs._new_exposure_block_reason("0xprobation", "BTC"),
            )
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def test_live_actual_copy_refresh_labels_repeated_losses_medium(self):
        db = self._db()
        db.execute(
            "INSERT INTO wallet_registry "
            "(addr,state,first_seen_at,last_seen_at,updated_at) "
            "VALUES ('0xcore3','qualified','now','now','now')"
        )
        db.executemany(
            "INSERT INTO copy_position "
            "(addr,coin,status,realized_pnl,unrealized_pnl,was_liq,"
            "opening_account_equity,opened_at,closed_at) "
            "VALUES ('0xcore3',?,?,?,?,?,?,?,?)",
            [
                ("xyz:MU", "liquidated", -257.0, 0.0, 1, 9752.0, "2026-07-30", now_iso()),
                ("xyz:AMD", "liquidated", -252.0, 0.0, 1, 9750.0, "2026-07-30", now_iso()),
                ("xyz:AMD", "closed", -25.0, 0.0, 0, 11690.0, "2026-07-30", now_iso()),
                ("xyz:AMD", "closed", 39.0, 0.0, 0, 11524.0, "2026-07-30", now_iso()),
                ("xyz:BRENTOIL", "open", 0.0, -23.0, 0, 11588.0, "2026-07-30", None),
            ],
        )
        db.commit()
        obs = Observer(db, [], {})

        result = obs._refresh_live_wallet_risks({"0xcore3"})

        self.assertEqual("medium", result["0xcore3"].level)
        registry = db.execute(
            "SELECT risk_level,risk_reasons_json,risk_assessed_at "
            "FROM wallet_registry WHERE addr='0xcore3'"
        ).fetchone()
        self.assertEqual("medium", registry["risk_level"])
        self.assertIn(
            "actual_copy_30d_conservative_pnl_not_positive",
            registry["risk_reasons_json"],
        )
        self.assertTrue(registry["risk_assessed_at"])
        self.assertEqual(
            "observer_live",
            db.execute(
                "SELECT source FROM wallet_risk_assessment WHERE addr='0xcore3'"
            ).fetchone()[0],
        )

    def test_high_wallet_risk_blocks_new_exposure(self):
        db = self._db()
        db.execute(
            "INSERT INTO wallet_registry "
            "(addr,state,first_seen_at,last_seen_at,risk_level,updated_at) "
            "VALUES ('0xhigh','qualified','now','now','high','now')"
        )
        db.commit()
        obs = Observer(db, [], {})

        self.assertEqual(
            "wallet_risk_blocked",
            obs._new_exposure_block_reason("0xhigh", "BTC"),
        )

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

    def test_pending_wallet_safety_blocks_open_and_add_exposure(self):
        db = self._db()
        db.execute(
            "INSERT INTO execution_wallet_safety "
            "(addr,state,event_key,reason,first_seen_at,updated_at) "
            "VALUES ('0xfrozen','pending','tid-1','source_liquidation_pending','now','now')"
        )
        db.commit()
        obs = Observer(db, [], {})
        self.assertEqual(
            "wallet_safety_frozen",
            obs._new_exposure_block_reason("0xfrozen", "BTC"),
        )
        self.assertTrue(obs._target_self_liquidation(
            "0xfrozen",
            {"liquidation": {"liquidatedUser": "0xFROZEN"}},
        ))
        self.assertFalse(obs._target_self_liquidation(
            "0xfrozen",
            {"liquidation": {"liquidatedUser": "0xother"}},
        ))

    def test_source_liquidation_confirmation_requires_zero_equity_and_no_positions(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs._set_wallet_safety(
                "0xliq", "pending", event_key="tid-2", occurred_at=123,
                reason="source_liquidation_pending", evidence={"coin": "BTC"},
            )
            zero = {
                "marginSummary": {"accountValue": "0"},
                "assetPositions": [],
            }
            with patch(
                "hyper.execution.observer.rest.clearinghouse_state",
                return_value=zero,
            ), patch.object(obs, "_reconcile_open", new=AsyncMock()) as reconcile:
                self.assertTrue(await obs._confirm_wallet_safety("0xliq", "BTC"))
            state = db.execute(
                "SELECT state,reason FROM execution_wallet_safety WHERE addr='0xliq'"
            ).fetchone()
            self.assertEqual((state["state"], state["reason"]),
                             ("confirmed", "source_account_liquidated_zero"))
            self.assertEqual(
                1,
                db.execute(
                    "SELECT COUNT(*) FROM wallet_risk_event "
                    "WHERE addr='0xliq' AND event_type='source_account_liquidated_zero'"
                ).fetchone()[0],
            )
            reconcile.assert_awaited_once()

            obs._set_wallet_safety(
                "0xfunded", "pending", event_key="tid-3", occurred_at=124,
                reason="source_liquidation_pending", evidence={"coin": "BTC"},
            )
            funded = {
                "marginSummary": {"accountValue": "1"},
                "assetPositions": [],
            }
            with patch(
                "hyper.execution.observer.rest.clearinghouse_state",
                return_value=funded,
            ):
                self.assertTrue(
                    await obs._confirm_wallet_safety("0xfunded", "BTC")
                )
            self.assertEqual(
                "cleared",
                db.execute(
                    "SELECT state FROM execution_wallet_safety WHERE addr='0xfunded'"
                ).fetchone()[0],
            )
            self.assertNotIn("0xfunded", obs.safety_frozen)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
