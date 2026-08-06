import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from hyper import config, storage
from hyper.execution.live_executor import LiveExecutionResult
from hyper.execution.observer import Observer, RetryableSignalError, TerminalSignalError
from hyper.market import ws
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

    def _activate_live(self, db, session_id="live-signal-test"):
        stamp = now_iso()
        db.execute(
            "INSERT INTO execution_session "
            "(session_id,mode,network,state,account_address,agent_address,strategy_revision,"
            "sizing_anchor,margin_equity_pct,sizing_equity,canary,started_at,updated_at) "
            "VALUES (?,'live','mainnet','live_running',?,?, 'revision-one',200,1,200,0,?,?)",
            (session_id, "0x" + "a" * 40, "0x" + "b" * 40, stamp, stamp),
        )
        db.execute(
            "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
            "VALUES (1,'live','live_running',?,?) ON CONFLICT(id) DO UPDATE SET "
            "selected_mode='live',state='live_running',active_session_id=excluded.active_session_id,"
            "updated_at=excluded.updated_at",
            (session_id, stamp),
        )
        db.execute(
            "INSERT INTO live_copy_account (id,initial_balance,balance,available,updated_at) "
            "VALUES (1,200,200,200,?) ON CONFLICT(id) DO UPDATE SET balance=200,available=200,"
            "updated_at=excluded.updated_at",
            (stamp,),
        )
        db.commit()
        return session_id

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

    def test_xyz_uses_same_ws_bbo_and_official_mark_subscriptions_as_crypto(self):
        async def run():
            obs = Observer(self._db(), [], {})
            obs.valid_coins = {"BTC", "xyz:SNDK"}
            obs.crypto_coins = {"BTC"}
            obs.ws = object()

            def discard(coro, *_args, **_kwargs):
                coro.close()
                return None

            with patch.object(obs, "_spawn_background", side_effect=discard), \
                    patch.object(obs, "_sub", new_callable=AsyncMock) as subscribe:
                await obs.ensure_coin("BTC")
                await obs.ensure_coin("xyz:SNDK")

            self.assertEqual(
                [call.args[0] for call in subscribe.await_args_list],
                [
                    ws.bbo("BTC"), ws.active_asset_ctx("BTC"),
                    ws.bbo("xyz:SNDK"), ws.active_asset_ctx("xyz:SNDK"),
                ],
            )
            self.assertEqual(obs.sub_coins, {"BTC", "xyz:SNDK"})

        asyncio.run(run())

    def test_active_asset_ctx_updates_official_mark_and_dashboard_pnl(self):
        db = self._db()
        obs = Observer(db, [], {})
        obs.on_message(json.dumps({
            "channel": "activeAssetCtx",
            "data": {"coin": "BTC", "ctx": {"markPx": "110.0"}},
        }))

        row = db.execute(
            "SELECT mark_px,unrealized_pnl FROM copy_position WHERE coin='BTC'"
        ).fetchone()
        self.assertEqual(obs.mark_mid["BTC"], 110.0)
        self.assertGreater(obs.official_mark_ms["BTC"], 0)
        self.assertEqual((row["mark_px"], row["unrealized_pnl"]), (110.0, 20.0))

    def test_fresh_ws_marks_skip_rest_fallback(self):
        async def run():
            obs = Observer(self._db(), [], {})
            obs.taker.open_ep = {
                ("0xaaa", "BTC"): {"master_open_px": None},
                ("0xbbb", "xyz:SNDK"): {"master_open_px": None},
            }
            obs.crypto_coins = {"BTC"}
            obs.official_mark_ms = {"BTC": now_ms(), "xyz:SNDK": now_ms()}
            with patch("hyper.execution.observer.rest.asset_contexts") as fetch:
                self.assertEqual(await obs._refresh_stale_authoritative_marks_once(), 0)
            fetch.assert_not_called()

        asyncio.run(run())

    def test_target_snapshot_keeps_source_leverage_as_audit_only_metadata(self):
        obs = Observer(self._db(), [], {})
        state = {"assetPositions": [{"position": {
            "coin": "BTC", "szi": "2", "entryPx": "101", "marginUsed": "40",
            "leverage": {"type": "cross", "value": 5},
        }}]}

        with patch("hyper.execution.observer.rest.clearinghouse_state", return_value=state):
            self.assertEqual(obs._target_snapshot("0xaaa", "BTC"), (40.0, 101.0, 5.0))

    def test_reconcile_backfills_source_leverage_without_changing_ours(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            obs.taker.open_ep = {
                ("0xaaa", "BTC"): {"pos_id": pos_id, "side": "long", "entry_px": 100.0,
                                     "realized_pnl": 0.0},
            }
            state = {"assetPositions": [{"position": {
                "coin": "BTC", "szi": "2", "entryPx": "101", "marginUsed": "40",
                "leverage": {"type": "cross", "value": 3},
            }}]}

            with patch("hyper.execution.observer.rest.clearinghouse_state", return_value=state):
                await obs._reconcile_open()

            row = db.execute(
                "SELECT leverage,master_leverage,master_margin,master_open_px "
                "FROM copy_position WHERE pos_id=?", (pos_id,),
            ).fetchone()
            self.assertEqual(row["leverage"], 5)
            self.assertEqual(row["master_leverage"], 3)
            self.assertEqual(row["master_margin"], 40)
            self.assertEqual(row["master_open_px"], 101)

        asyncio.run(run())

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

    def test_live_fill_is_journalled_before_dispatch_and_cursor_is_persisted(self):
        async def run():
            db = self._db()
            session_id = self._activate_live(db)
            obs = Observer(db, ["0xsource"], {})
            fill = {
                "tid": 123, "time": 1_900_000_000_000, "coin": "BTC", "side": "B",
                "dir": "Open Long", "px": "100", "sz": "1", "startPosition": "0",
                "oid": 77, "closedPnl": "0", "crossed": True,
            }
            with patch("hyper.execution.observer.rest.post_soft", return_value=[fill]), \
                    patch.object(obs, "_dispatch_fill") as dispatch:
                await obs._poll_fills("0xsource", fill["time"] - 1000)

            signal = db.execute(
                "SELECT state,tid,payload_json FROM execution_signal WHERE session_id=?",
                (session_id,),
            ).fetchone()
            self.assertEqual((signal["state"], signal["tid"]), ("pending", 123))
            self.assertEqual(json.loads(signal["payload_json"])["oid"], 77)
            cursor = db.execute(
                "SELECT last_fill_ms FROM observer_target_cursor WHERE session_id=? AND addr='0xsource'",
                (session_id,),
            ).fetchone()[0]
            self.assertGreaterEqual(cursor, fill["time"])
            dispatch.assert_not_called()

        asyncio.run(run())

    def test_failed_target_fill_read_does_not_advance_live_cursor(self):
        async def run():
            db = self._db()
            session_id = self._activate_live(db)
            obs = Observer(db, ["0xsource"], {})
            obs.last_fill_ms["0xsource"] = 1234

            with patch("hyper.execution.observer.rest.post_soft", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "target_fills_unavailable"):
                    await obs._poll_fills("0xsource", 1000)

            self.assertEqual(obs.last_fill_ms["0xsource"], 1234)
            self.assertIsNone(db.execute(
                "SELECT last_fill_ms FROM observer_target_cursor "
                "WHERE session_id=? AND addr='0xsource'",
                (session_id,),
            ).fetchone())

        asyncio.run(run())

    def test_live_signal_consumer_dispatches_journal_in_order(self):
        async def run():
            db = self._db()
            session_id = self._activate_live(db)
            obs = Observer(db, [], {})
            stamp = now_iso()
            payload = {
                "tid": 124, "time": 1_900_000_000_001, "coin": "BTC", "side": "S",
                "dir": "Close Long", "px": "100", "sz": "1", "startPosition": "1",
                "oid": 78,
            }
            signal_id = db.execute(
                "INSERT INTO execution_signal "
                "(mode,session_id,addr,coin,tid,source_time_ms,source_order_id,payload_json,state,"
                "received_at,updated_at) VALUES ('live',?,'0xsource','BTC',124,?,78,?,'pending',?,?)",
                (session_id, payload["time"], json.dumps(payload), stamp, stamp),
            ).lastrowid
            db.commit()

            task = asyncio.create_task(obs.signal_retry_loop())
            for _ in range(20):
                state = db.execute(
                    "SELECT state FROM execution_signal WHERE signal_id=?", (signal_id,),
                ).fetchone()[0]
                if state == "completed":
                    break
                await asyncio.sleep(0.02)
            obs.stop = True
            await task

            row = db.execute(
                "SELECT state,decision_code FROM execution_signal WHERE signal_id=?", (signal_id,),
            ).fetchone()
            self.assertEqual((row["state"], row["decision_code"]),
                             ("completed", "NO_MANAGED_POSITION"))

        asyncio.run(run())

    def test_signal_retry_survives_temporary_sqlite_writer_lock(self):
        async def run():
            db = self._db()
            self._activate_live(db)
            db.execute("PRAGMA busy_timeout=1")
            obs = Observer(db, [], {})
            db_path = db.execute("PRAGMA database_list").fetchone()[2]
            blocker = sqlite3.connect(db_path, timeout=0.01)
            blocker.execute("PRAGMA busy_timeout=1")
            blocker.execute("BEGIN IMMEDIATE")
            try:
                task = asyncio.create_task(obs.signal_retry_loop())
                await asyncio.sleep(0.08)
                self.assertFalse(task.done())
                self.assertFalse(obs.stop)
            finally:
                blocker.rollback()
                blocker.close()
            await asyncio.sleep(0.08)
            obs.stop = True
            await task

        asyncio.run(run())

    def test_critical_background_failure_is_propagated_for_systemd_restart(self):
        async def run():
            obs = Observer(self._db(), [], {})

            async def fail():
                raise RuntimeError("test critical failure")

            obs._spawn_background(fail(), "test_loop", critical=True)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertTrue(obs.stop)
            with self.assertRaisesRegex(
                RuntimeError, "critical_background_task_failed:RuntimeError:test critical failure",
            ):
                obs._raise_critical_background_failure()

        asyncio.run(run())

    def test_flip_signal_with_only_close_action_resumes_reverse_open(self):
        async def run():
            db = self._db()
            session_id = self._activate_live(db)
            obs = Observer(db, [], {})
            stamp = now_iso()
            payload = {
                "tid": 125, "time": 1_900_000_000_002, "coin": "BTC", "side": "S",
                "dir": "Long > Short", "px": "100", "sz": "2", "startPosition": "1",
                "oid": 79,
            }
            signal_id = db.execute(
                "INSERT INTO execution_signal "
                "(mode,session_id,addr,coin,tid,source_time_ms,source_order_id,payload_json,state,"
                "received_at,updated_at) VALUES ('live',?,'0xsource','BTC',125,?,79,?,'pending',?,?)",
                (session_id, payload["time"], json.dumps(payload), stamp, stamp),
            ).lastrowid
            db.execute(
                "INSERT INTO live_copy_action "
                "(addr,coin,ts,action,master_oid,our_qty_delta) VALUES "
                "('0xsource','BTC',?,'close',79,-1)",
                (payload["time"],),
            )
            db.commit()

            def finish(*_args, **kwargs):
                obs._mark_signal(kwargs["signal_id"], "completed", code="TEST")
                obs.stop = True

            with patch.object(obs, "_dispatch_fill", side_effect=finish) as dispatch:
                await obs.signal_retry_loop()

            dispatch.assert_called_once()
            self.assertEqual(
                db.execute(
                    "SELECT state FROM execution_signal WHERE signal_id=?", (signal_id,),
                ).fetchone()[0],
                "completed",
            )

        asyncio.run(run())

    def test_later_same_market_signal_waits_for_retryable_predecessor(self):
        async def run():
            db = self._db()
            session_id = self._activate_live(db)
            obs = Observer(db, [], {})
            stamp = now_iso()
            rows = [
                (
                    126, 100, "retryable", now_ms() + 60_000,
                    {"tid": 126, "time": 100, "coin": "BTC", "side": "B",
                     "px": "100", "sz": "1", "startPosition": "0", "oid": 80},
                ),
                (
                    127, 101, "pending", 0,
                    {"tid": 127, "time": 101, "coin": "BTC", "side": "B",
                     "px": "99", "sz": "1", "startPosition": "1", "oid": 81},
                ),
            ]
            db.executemany(
                "INSERT INTO execution_signal "
                "(mode,session_id,addr,coin,tid,source_time_ms,source_order_id,payload_json,state,"
                "next_attempt_ms,received_at,updated_at) "
                "VALUES ('live',?,'0xsource','BTC',?,?,?,?,?,?,?,?)",
                [
                    (
                        session_id, tid, source_ms, payload["oid"], json.dumps(payload), state,
                        next_ms, stamp, stamp,
                    )
                    for tid, source_ms, state, next_ms, payload in rows
                ],
            )
            db.commit()

            with patch.object(obs, "_dispatch_fill") as dispatch:
                task = asyncio.create_task(obs.signal_retry_loop())
                await asyncio.sleep(0.1)
                obs.stop = True
                await task

            dispatch.assert_not_called()
            self.assertEqual(
                db.execute(
                    "SELECT state FROM execution_signal WHERE tid=127"
                ).fetchone()[0],
                "pending",
            )

        asyncio.run(run())

    def test_retryable_live_signal_is_not_marked_completed(self):
        async def run():
            db = self._db()
            session_id = self._activate_live(db)
            obs = Observer(db, [], {})
            stamp = now_iso()
            signal_id = db.execute(
                "INSERT INTO execution_signal "
                "(mode,session_id,addr,coin,tid,source_time_ms,payload_json,state,received_at,updated_at) "
                "VALUES ('live',?,'0xsource','BTC',1,1,'{}','pending',?,?)",
                (session_id, stamp, stamp),
            ).lastrowid
            db.commit()

            async def fail_once():
                raise RetryableSignalError("temporary")

            task = obs._schedule_signal_task(signal_id, fail_once(), name="test")
            await task

            row = db.execute(
                "SELECT state,attempt_count,last_error,next_attempt_ms FROM execution_signal "
                "WHERE signal_id=?", (signal_id,),
            ).fetchone()
            self.assertEqual(row["state"], "retryable")
            self.assertEqual(row["attempt_count"], 1)
            self.assertIn("temporary", row["last_error"])
            self.assertGreater(row["next_attempt_ms"], now_ms())

        asyncio.run(run())

    def test_verified_terminal_signal_failure_is_not_retried(self):
        async def run():
            db = self._db()
            session_id = self._activate_live(db)
            obs = Observer(db, [], {})
            stamp = now_iso()
            signal_id = db.execute(
                "INSERT INTO execution_signal "
                "(mode,session_id,addr,coin,tid,source_time_ms,payload_json,state,received_at,updated_at) "
                "VALUES ('live',?,'0xsource','BTC',2,2,'{}','pending',?,?)",
                (session_id, stamp, stamp),
            ).lastrowid
            db.commit()

            async def fail_terminal():
                raise TerminalSignalError("verified_rejected")

            task = obs._schedule_signal_task(signal_id, fail_terminal(), name="test-terminal")
            await task

            row = db.execute(
                "SELECT state,attempt_count,last_error,next_attempt_ms FROM execution_signal "
                "WHERE signal_id=?", (signal_id,),
            ).fetchone()
            self.assertEqual(row["state"], "failed_terminal")
            self.assertEqual(row["attempt_count"], 1)
            self.assertIn("verified_rejected", row["last_error"])
            self.assertEqual(row["next_attempt_ms"], 0)

        asyncio.run(run())

    def test_manual_cooldown_is_scoped_by_execution_mode(self):
        db = self._db()
        self._activate_live(db)
        db.execute(
            "INSERT INTO execution_manual_close_cooldown "
            "(mode,addr,coin,pos_id,reason,created_at,expires_at) "
            "VALUES ('paper','0xsource','BTC',1,'manual_stop_loss',?,?)",
            (now_iso(), "2999-01-01T00:00:00Z"),
        )
        db.commit()

        live = Observer(db, [], {})

        self.assertIsNone(live._manual_close_cooldown_until("0xsource", "BTC"))
        self.assertEqual(
            db.execute(
                "SELECT COUNT(*) FROM execution_manual_close_cooldown WHERE mode='paper'"
            ).fetchone()[0],
            1,
        )

    def test_live_ledger_projection_drift_fails_closed(self):
        db = self._db()
        session_id = self._activate_live(db)
        db.execute(
            "INSERT INTO live_copy_position "
            "(addr,coin,side,status,entry_px,size,rem_size,opened_at) "
            "VALUES ('0xsource','BTC','long','open',100,1,1,?)",
            (now_iso(),),
        )
        db.commit()
        obs = Observer(db, [], {})

        self.assertFalse(obs._verify_live_ledger_projection())
        self.assertEqual(
            db.execute("SELECT state FROM execution_control WHERE id=1").fetchone()[0],
            "reconcile_required",
        )

    def test_running_live_observer_fails_closed_on_mode_change(self):
        db = self._db()
        self._activate_live(db)
        obs = Observer(db, [], {})
        db.execute(
            "UPDATE execution_control SET selected_mode='paper',state='paper',active_session_id=NULL "
            "WHERE id=1"
        )
        db.commit()

        with self.assertRaisesRegex(RuntimeError, "execution_mode_changed"):
            obs._assert_mode_binding()

    def test_explicit_exchange_liquidation_settles_live_ledger_once(self):
        async def settle():
            db = self._db()
            session_id = self._activate_live(db)
            fill_time = now_ms()
            db.execute(
                "INSERT INTO live_copy_position "
                "(addr,coin,side,status,master_open_ms,master_open_px,master_peak_sz,leverage,margin,"
                "notional,entry_px,size,rem_size,peak_size,liq_px,realized_pnl,num_actions,opened_at,"
                "strategy_revision_id) VALUES "
                "('0xsource','CASHCAT','short','open',1,.15,2707,6,67.68,406.05,.15,2707,2707,"
                "2707,.14164,0,0,?,'revision-one')",
                (now_iso(),),
            )
            db.execute(
                "INSERT INTO execution_fill "
                "(network,tid,session_id,oid,coin,side,size,px,fee,closed_pnl,fill_time_ms,raw_json,created_at) "
                "VALUES ('mainnet','cashcat-liq',?,9001,'CASHCAT','B',2707,.14164,.17,-87,?,?,?)",
                (
                    session_id, fill_time,
                    json.dumps({"dir": "Liquidated Isolated Short"}), now_iso(),
                ),
            )
            db.commit()
            obs = Observer(db, [], {})
            obs.live_executor = Mock(
                equity=112.83, available=112.83, session={"sizing_anchor": 200.0},
            )
            obs._load_account(obs.taker)
            obs._reload_open(obs.taker)
            first = obs._settle_forced_liquidations()
            second = obs._settle_forced_liquidations()
            return db, obs, first, second

        db, obs, first, second = asyncio.run(settle())

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertNotIn(("0xsource", "CASHCAT"), obs.open_ep)
        position = db.execute(
            "SELECT status,rem_size,realized_pnl,was_liq,num_actions "
            "FROM live_copy_position WHERE coin='CASHCAT'"
        ).fetchone()
        self.assertEqual(position["status"], "liquidated")
        self.assertAlmostEqual(position["rem_size"], 0.0)
        self.assertAlmostEqual(position["realized_pnl"], -87.17)
        self.assertEqual(position["was_liq"], 1)
        self.assertEqual(position["num_actions"], 1)
        action = db.execute(
            "SELECT action,our_qty_delta,our_px,realized_pnl FROM live_copy_action "
            "WHERE coin='CASHCAT'"
        ).fetchone()
        self.assertEqual(action["action"], "close")
        self.assertAlmostEqual(action["our_qty_delta"], 2707)
        self.assertAlmostEqual(action["our_px"], 0.14164)
        self.assertAlmostEqual(action["realized_pnl"], -87.17)

    def test_close_all_never_reports_success_with_remaining_positions(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs.taker.open_ep = {
                ("0xaaa", "BTC"): {"pos_id": 1},
                ("0xbbb", "ETH"): {"pos_id": 2},
            }
            with patch.object(
                obs, "_cmd_close", new=AsyncMock(side_effect=RuntimeError("unfilled")),
            ):
                with self.assertRaisesRegex(RuntimeError, "close_all_incomplete"):
                    await obs._cmd_close_all()

        asyncio.run(run())

    def test_cancelling_live_order_await_still_returns_confirmed_worker_result(self):
        async def run():
            db = self._db()
            self._activate_live(db)
            obs = Observer(db, [], {})
            started = threading.Event()
            release = threading.Event()
            expected = LiveExecutionResult(1.0, 100.0, 0.05, 0.0, ("cloid",), (1,), "filled")

            def execute(**_kwargs):
                started.set()
                release.wait(2)
                return expected

            obs.live_executor = Mock(execute=execute)
            task = asyncio.create_task(obs._execute_live_order(
                ep={"num_actions": 0}, addr="0xsource", coin="BTC", action="open",
                is_buy=True, size=1, leverage=5, reduce_only=False,
                source_time_ms=1, source_order_id=2,
            ))
            for _ in range(50):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(started.is_set())
            task.cancel()
            release.set()

            self.assertIs(await task, expected)
            self.assertEqual(obs._live_order_inflight, 0)

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

            refresh.assert_called_once()
            refresh_db, coin, asset_ctx = refresh.call_args.args
            self.assertIsNot(refresh_db, db)
            self.assertEqual(coin, "xyz:SP500")
            self.assertIsNone(asset_ctx)
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

    def test_live_sizing_uses_current_session_anchor_and_fresh_exchange_balance(self):
        async def run():
            db = self._db()
            stamp = now_iso()
            db.execute(
                "INSERT INTO execution_session "
                "(session_id,mode,network,state,account_address,agent_address,strategy_revision,"
                "sizing_anchor,margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at) "
                "VALUES ('live-current','live','mainnet','live_running',?,?, 'revision-one',200,1,200,0,NULL,?,?)",
                ("0x" + "a" * 40, "0x" + "b" * 40, stamp, stamp),
            )
            db.execute(
                "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
                "VALUES (1,'live','live_running','live-current',?) "
                "ON CONFLICT(id) DO UPDATE SET selected_mode='live',state='live_running',"
                "active_session_id='live-current',updated_at=excluded.updated_at",
                (stamp,),
            )
            # Lifetime ROI may legitimately retain an older $8k starting point. It must not become this
            # newly downsized session's sizing anchor.
            db.execute(
                "INSERT INTO live_copy_account (id,initial_balance,balance,available,updated_at) "
                "VALUES (1,8000,205,190,?)",
                (stamp,),
            )
            db.commit()
            obs = Observer(db, [], {})

            class FreshExecutor:
                session = {"sizing_anchor": 200.0}
                equity = 205.0
                available = 190.0

                def reconcile(self):
                    self.equity = 204.0
                    self.available = 187.0
                    return {"ok": True}

            obs.live_executor = FreshExecutor()
            obs._load_account()
            self.assertEqual(obs.taker.initial_balance, 8000.0)
            self.assertEqual(obs._open_sizing_params().capital_anchor, 200.0)

            await obs._refresh_live_sizing_state()
            self.assertEqual(obs.taker.balance, 204.0)
            self.assertEqual(obs._risk_available(), 187.0)
            account = db.execute(
                "SELECT initial_balance,balance,available FROM live_copy_account WHERE id=1"
            ).fetchone()
            self.assertEqual(tuple(account), (8000.0, 204.0, 187.0))

        asyncio.run(run())

    def test_live_executor_database_connection_is_not_shared_with_observer(self):
        db = self._db()
        obs = Observer(db, [], {})

        execution_db = obs._open_live_executor_db()
        self.addCleanup(execution_db.close)

        self.assertIsNot(execution_db, db)
        observer_path = db.execute("PRAGMA database_list").fetchone()[2]
        execution_path = execution_db.execute("PRAGMA database_list").fetchone()[2]
        self.assertEqual(execution_path, observer_path)
        self.assertEqual(
            execution_db.execute("PRAGMA journal_mode").fetchone()[0].lower(),
            "wal",
        )
        self.assertEqual(
            execution_db.execute("PRAGMA busy_timeout").fetchone()[0],
            config.LIVE_EXECUTOR_DB_BUSY_TIMEOUT_MS,
        )

    def test_live_poll_round_batches_durable_cursors(self):
        async def run():
            db = self._db()
            session_id = self._activate_live(db)
            obs = Observer(db, ["0xsource", "0xsecond"], {})
            with patch("hyper.execution.observer.rest.post_soft", return_value=[]):
                first = await obs._poll_fills("0xsource", 1000, persist_cursor=False)
                second = await obs._poll_fills("0xsecond", 1000, persist_cursor=False)

            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM observer_target_cursor WHERE session_id=?",
                (session_id,),
            ).fetchone()[0], 0)
            obs._persist_live_cursors([("0xsource", first), ("0xsecond", second)])
            rows = db.execute(
                "SELECT addr,last_fill_ms FROM observer_target_cursor WHERE session_id=? ORDER BY addr",
                (session_id,),
            ).fetchall()
            self.assertEqual([row["addr"] for row in rows], ["0xsecond", "0xsource"])
            self.assertTrue(all(int(row["last_fill_ms"]) > 1000 for row in rows))

        asyncio.run(run())

    def test_threaded_volatility_refresh_does_not_share_observer_connection(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            seen = []

            def refresh(refresh_db, coin, asset_ctx=None):
                seen.append((refresh_db, coin, asset_ctx))
                return 0.08

            with patch("hyper.execution.observer.volatility.refresh", side_effect=refresh):
                await obs._ensure_vol("ZEC")

            self.assertEqual(obs.vol["ZEC"], 0.08)
            self.assertEqual(seen[0][1], "ZEC")
            self.assertIsNot(seen[0][0], db)

        asyncio.run(run())

    def test_live_sizing_retries_transient_reconcile_without_pausing(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs.execution_mode = "live"
            obs.execution_state = "live_running"

            executor = Mock()
            executor.equity = 205.0
            executor.available = 190.0
            executor.reconcile.side_effect = [
                sqlite3.OperationalError("database is locked"),
                sqlite3.OperationalError("database is locked"),
                {"ok": True},
            ]
            obs.live_executor = executor

            with patch("hyper.execution.observer.asyncio.sleep", new=AsyncMock()) as sleep:
                await obs._refresh_live_sizing_state()

            self.assertEqual(executor.reconcile.call_count, 3)
            self.assertEqual(executor.rollback_after_error.call_count, 2)
            self.assertEqual(sleep.await_count, 2)
            self.assertFalse(obs.paused)
            self.assertIsNone(obs.live_reconcile_error)

        asyncio.run(run())

    def test_background_reconcile_transient_error_is_visible_and_auto_recovers(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs.execution_mode = "live"
            obs.execution_state = "live_running"
            obs._proc_state = "running"

            executor = Mock()
            executor.equity = 205.0
            executor.available = 190.0
            executor.reconcile.side_effect = [
                sqlite3.OperationalError("database is locked"),
                sqlite3.OperationalError("database is locked"),
                sqlite3.OperationalError("database is locked"),
            ]
            obs.live_executor = executor

            with patch("hyper.execution.observer.asyncio.sleep", new=AsyncMock()) as sleep:
                result = await obs._reconcile_live_once()

            self.assertIsNone(result)
            self.assertEqual(executor.reconcile.call_count, 3)
            self.assertEqual(sleep.await_count, 2)
            self.assertFalse(obs.paused)
            self.assertEqual(obs.execution_state, "live_running")
            self.assertIn("database is locked", obs.live_reconcile_error)
            detail = json.loads(db.execute(
                "SELECT detail_json FROM process_status WHERE name='observer'"
            ).fetchone()[0])
            self.assertFalse(detail["reconcileHealthy"])

            executor.reconcile.side_effect = None
            executor.reconcile.return_value = {"ok": True}
            result = await obs._reconcile_live_once()

            self.assertTrue(result["ok"])
            self.assertFalse(obs.paused)
            self.assertIsNone(obs.live_reconcile_error)

        asyncio.run(run())

    def test_background_reconcile_hides_recovered_writer_collision(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs.execution_mode = "live"
            obs.execution_state = "live_running"
            obs._proc_state = "running"

            executor = Mock()
            executor.equity = 205.0
            executor.available = 190.0
            executor.reconcile.side_effect = [
                sqlite3.OperationalError("database is locked"),
                {"ok": True},
            ]
            obs.live_executor = executor

            with patch("hyper.execution.observer.asyncio.sleep", new=AsyncMock()) as sleep:
                result = await obs._reconcile_live_once()

            self.assertTrue(result["ok"])
            self.assertEqual(executor.reconcile.call_count, 2)
            self.assertEqual(executor.rollback_after_error.call_count, 1)
            self.assertEqual(sleep.await_count, 1)
            self.assertIsNone(obs.live_reconcile_error)

        asyncio.run(run())

    def test_background_reconcile_retries_observer_account_sync_collision(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs.execution_mode = "live"
            obs.execution_state = "live_running"
            obs._proc_state = "running"

            executor = Mock()
            executor.equity = 205.0
            executor.available = 190.0
            executor.reconcile.return_value = {"ok": True}
            obs.live_executor = executor

            with patch.object(
                obs, "_sync_live_account",
                side_effect=[sqlite3.OperationalError("database is locked"), None],
            ), patch("hyper.execution.observer.asyncio.sleep", new=AsyncMock()) as sleep:
                result = await obs._reconcile_live_once()

            self.assertTrue(result["ok"])
            self.assertEqual(executor.reconcile.call_count, 2)
            self.assertEqual(executor.rollback_after_error.call_count, 1)
            self.assertEqual(sleep.await_count, 1)
            self.assertIsNone(obs.live_reconcile_error)

        asyncio.run(run())

    def test_background_reconcile_confirmed_drift_still_pauses_live(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs.execution_mode = "live"
            obs.execution_state = "live_running"
            obs._proc_state = "running"

            executor = Mock()
            executor.equity = 205.0
            executor.available = 190.0
            executor.reconcile.return_value = {"ok": False}
            obs.live_executor = executor

            result = await obs._reconcile_live_once()

            self.assertFalse(result["ok"])
            self.assertTrue(obs.paused)
            self.assertEqual(obs.execution_state, "reconcile_required")
            self.assertEqual(
                db.execute("SELECT state FROM process_status WHERE name='observer'").fetchone()[0],
                "paused",
            )

        asyncio.run(run())

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

    def test_busy_mark_persistence_keeps_fresh_bbo_in_memory(self):
        db = self._db()
        obs = Observer(db, [], {})

        with patch.object(
            obs, "_refresh_coin_marks",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            obs.on_bbo({"coin": "BTC", "bbo": [{"px": "101"}, {"px": "103"}]})

        self.assertEqual(obs.bbo["BTC"], (101.0, 103.0))
        self.assertNotIn("BTC", obs.mark_write_ms)

    def test_failed_signal_batch_restores_wallet_cursor_for_full_retry(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            addr = "0xsignal"
            obs.last_fill_ms[addr] = 100
            fill = {"tid": 1, "time": 500, "coin": "BTC"}

            def fail_after_cursor_advance(_addr, _fill):
                obs.last_fill_ms[addr] = 500
                raise sqlite3.OperationalError("database is locked")

            with patch("hyper.execution.observer.rest.post_soft", return_value=[fill]), \
                    patch.object(obs, "process_fill", side_effect=fail_after_cursor_advance):
                with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                    await obs._poll_fills(addr, 88)

            self.assertEqual(obs.last_fill_ms[addr], 100)

        asyncio.run(run())

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

    def test_stale_builder_quote_does_not_poll_rest_from_observer(self):
        async def run():
            db = self._db()
            obs = Observer(db, [], {})
            obs.bbo["xyz:IBM"] = (220.0, 221.0)
            obs.bbo_ms["xyz:IBM"] = now_ms() - config.EXECUTION_QUOTE_MAX_AGE_MS - 1

            with patch("hyper.execution.observer.rest.realtime_book_top", return_value=(222.0, 223.0)) as fetch:
                px = await obs._execution_px("xyz:IBM", True, 224.0)

            self.assertEqual(px, 224.0)
            self.assertEqual(obs.bbo["xyz:IBM"], (220.0, 221.0))
            fetch.assert_not_called()

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

    def test_opposite_coin_direction_is_blocked_until_existing_side_is_flat(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            ep = self._live_ep(pos_id, "long", 100, 2)
            ep.update(coin="BTC", addr="0xaaa")
            obs.taker.open_ep[("0xaaa", "BTC")] = ep

            self.assertEqual(
                obs._new_exposure_block_reason("0xbbb", "BTC", side="short"),
                "coin_direction_conflict",
            )
            self.assertIsNone(
                obs._new_exposure_block_reason("0xbbb", "BTC", side="long"),
            )

            ep["rem_size"] = 0.0
            self.assertIsNone(
                obs._new_exposure_block_reason("0xbbb", "BTC", side="short"),
            )

        asyncio.run(run())

    def test_live_manual_close_does_not_repeat_failed_executor_cycle(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            obs.execution_mode = "live"
            obs.taker.open_ep[("0xaaa", "BTC")] = self._live_ep(pos_id, "long", 100, 2)
            self._set_bbo(obs, "BTC", 99.0, 101.0)

            with patch.object(
                obs, "_apply_reduce", new_callable=AsyncMock,
                side_effect=RetryableSignalError("live_reduce_execution_failed"),
            ) as reduce:
                with self.assertRaisesRegex(RuntimeError, "manual_close_incomplete"):
                    await obs._cmd_close(
                        pos_id, source_event_id="command:test:position:1",
                    )

            reduce.assert_awaited_once()

        asyncio.run(run())

    def test_target_poll_loop_starts_wallets_at_configured_interval(self):
        async def run():
            obs = Observer(self._db(), [], {})
            obs.addrs = ["0xone", "0xtwo", "0xthree"]
            starts = []

            async def poll(addr, _since, *, persist_cursor=True):
                starts.append((addr, time.monotonic()))
                if len(starts) == 3:
                    obs.stop = True
                return now_ms()

            with patch.object(obs, "_poll_fills", side_effect=poll), \
                    patch.object(config, "TARGET_POLL_START_INTERVAL_S", 0.03):
                await obs.poll_loop()
            return starts

        starts = asyncio.run(run())

        self.assertEqual([item[0] for item in starts], ["0xone", "0xtwo", "0xthree"])
        self.assertGreaterEqual(starts[1][1] - starts[0][1], 0.02)
        self.assertGreaterEqual(starts[2][1] - starts[1][1], 0.02)

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

    def test_reduce_that_would_leave_sub_minimum_notional_closes_all(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            ep = self._live_ep(pos_id, "long", 100, 2)
            obs.taker.open_ep[("0xaaa", "BTC")] = ep

            # A proportional mirror would leave $3.  The shared engine must
            # upgrade it to a full close because Hyperliquid's minimum is $10.
            await obs._apply_reduce(
                "0xaaa", "BTC", ep, now_ms(), 100.0, -1.97, 0.03,
                closing=False, liq=False, forced_px=100.0,
            )

            row = db.execute(
                "SELECT status,rem_size FROM copy_position WHERE pos_id=?", (pos_id,),
            ).fetchone()
            self.assertEqual(row["status"], "closed")
            self.assertEqual(row["rem_size"], 0)

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

    def test_reload_never_fake_closes_live_exchange_dust(self):
        db = self._db()
        self._activate_live(db)
        pos_id = db.execute(
            "INSERT INTO live_copy_position "
            "(addr,coin,side,status,entry_px,leverage,margin,notional,size,rem_size,opened_at) "
            "VALUES ('0xaaa','BTC','short','open',30000,10,.3,3,.0001,.0001,?)",
            (now_iso(),),
        ).lastrowid
        db.commit()

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            obs = Observer(db, [], {})
            obs._reload_open()

            row = db.execute(
                "SELECT status,rem_size FROM live_copy_position WHERE pos_id=?", (pos_id,),
            ).fetchone()
            self.assertEqual(row["status"], "open")
            self.assertEqual(row["rem_size"], 0.0001)
            self.assertIn(("0xaaa", "BTC"), obs.taker.open_ep)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM live_copy_action WHERE pos_id=?", (pos_id,)).fetchone()[0],
                0,
            )
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
                "SELECT addr,coin,pos_id,reason,created_at,expires_at "
                "FROM execution_manual_close_cooldown "
                "WHERE mode='paper' AND addr='0xaaa' AND coin='BTC'"
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
                "SELECT COUNT(*) FROM execution_manual_close_cooldown "
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
                "SELECT COUNT(*) FROM execution_manual_close_cooldown"
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

            n = db.execute("SELECT COUNT(*) FROM execution_manual_close_cooldown").fetchone()[0]
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
            "INSERT INTO execution_manual_close_cooldown "
            "(mode,addr,coin,pos_id,reason,created_at,expires_at) "
            "VALUES ('paper','0xaaa','BTC',123,'manual_close','2026-01-01T00:00:00Z',"
            "'2999-01-01T00:00:00Z')"
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

    def test_source_open_follows_first_fill_and_carries_open_oid(self):
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

            open_position.assert_called_once()
            self.assertEqual(open_position.call_args.kwargs["source_open_oids"], {77})
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
                "INSERT INTO execution_manual_close_cooldown "
                "(mode,addr,coin,pos_id,reason,created_at,expires_at) VALUES ('paper',?,?,?,?,?,?)",
                [
                    ("0xprofit", "SOL", profit_pos, "manual_close", "old", "2999-01-01T00:00:00Z"),
                    ("0xloss", "HYPE", loss_pos, "manual_close", "old", "2999-01-01T00:00:00Z"),
                ],
            )
            db.commit()

            Observer(db, [], {})._reload_open()

            rows = db.execute(
                "SELECT addr,coin FROM execution_manual_close_cooldown "
                "WHERE mode='paper' ORDER BY addr"
            ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [("0xloss", "HYPE")])

        asyncio.run(run())

    def test_expired_manual_cooldown_allows_new_open(self):
        db = self._db()
        db.execute(
            "INSERT INTO execution_manual_close_cooldown "
            "(mode,addr,coin,pos_id,reason,created_at,expires_at) "
            "VALUES ('paper','0xaaa','BTC',123,'manual_close','2026-01-01T00:00:00Z',"
            "'2026-01-02T00:00:00Z')"
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
                    "SELECT expires_at FROM execution_manual_close_cooldown "
                    "WHERE mode='paper' AND addr='0xaaa' AND coin='BTC'"
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
                patch.object(obs, "_target_snapshot", return_value=(4, None, 2)) as target_snapshot,
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
                patch.object(obs, "_target_snapshot", return_value=(5, 100, 2)),
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

    def test_live_full_close_clear_unfilled_schedules_bounded_continuation(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            db.execute(
                "INSERT INTO execution_control (id,selected_mode,state,updated_at) "
                "VALUES (1,'live','live_running',?) ON CONFLICT(id) DO UPDATE SET "
                "selected_mode='live',state='live_running',updated_at=excluded.updated_at", (now_iso(),),
            )
            db.commit()
            obs.execution_mode = "live"
            obs.live_executor = object()
            ep = self._live_ep(pos_id, "long", 100, 2)
            obs.taker.open_ep[("0xaaa", "BTC")] = ep
            unfilled = LiveExecutionResult(0, None, 0, 0, (), (), "unfilled", "ioc_cancel")

            with patch.object(obs, "_execute_live_order", new=AsyncMock(return_value=unfilled)), \
                    patch.object(obs, "_retry_live_full_close", new=AsyncMock()) as retry:
                await obs._apply_reduce(
                    "0xaaa", "BTC", ep, now_ms(), 100, -2, 0,
                    closing=True, liq=False, forced_px=100,
                )
                await asyncio.sleep(0)

            retry.assert_awaited_once_with("0xaaa", "BTC", ep, obs.taker)

        asyncio.run(run())

    def test_live_full_close_ambiguous_failure_never_blind_retries(self):
        async def run():
            db = self._db()
            pos_id = db.execute(
                "SELECT pos_id FROM copy_position WHERE addr='0xaaa' AND coin='BTC'"
            ).fetchone()["pos_id"]
            obs = Observer(db, [], {})
            db.execute(
                "INSERT INTO execution_control (id,selected_mode,state,updated_at) "
                "VALUES (1,'live','reconcile_required',?) ON CONFLICT(id) DO UPDATE SET "
                "selected_mode='live',state='reconcile_required',updated_at=excluded.updated_at", (now_iso(),),
            )
            db.commit()
            obs.execution_mode = "live"
            obs.live_executor = object()
            ep = self._live_ep(pos_id, "long", 100, 2)
            obs.taker.open_ep[("0xaaa", "BTC")] = ep

            with patch.object(
                obs, "_execute_live_order", new=AsyncMock(side_effect=RuntimeError("live_order_status_ambiguous")),
            ), patch.object(obs, "_retry_live_full_close", new=AsyncMock()) as retry:
                await obs._apply_reduce(
                    "0xaaa", "BTC", ep, now_ms(), 100, -2, 0,
                    closing=True, liq=False, forced_px=100,
                )
                await asyncio.sleep(0)

            retry.assert_not_awaited()

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
            "actual_copy_cumulative_loss_over_2pct",
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
