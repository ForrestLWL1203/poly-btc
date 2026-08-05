import asyncio
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from hyper import storage
from hyper.execution.account_monitor import LiveAccountMonitor
from hyper.execution.live_executor import LiveExecutor
from hyper.execution.orders import OrderOutcome
from hyper.tests.test_live_executor import ACCOUNT, AGENT, FakeLiveBroker
from hyper.util import now_iso, now_ms


class _HealthyMonitor:
    def is_healthy(self):
        return True

    def recover_order(self, _cloid, _start_ms):
        return None


class _WsPublishingBroker(FakeLiveBroker):
    def __init__(self):
        super().__init__([(OrderOutcome.FILLED, 1.0, None)])
        self.venue.ws_url = "wss://example.invalid/ws"
        self.account_address = ACCOUNT
        self.supported_dexes = ("", "xyz")
        self.account_snapshot_calls = 0
        self.executor = None

    def account_snapshot(self):
        self.account_snapshot_calls += 1
        return super().account_snapshot()

    def submit_ioc(self, intent):
        result = super().submit_ioc(intent)
        # Deliver official facts before the synchronous HTTP call returns. The
        # fill intentionally has no CLOID, matching the documented WsFill.
        fill = dict(self.fills[-1])
        fill.pop("cloid", None)
        self.executor.apply_ws_message({
            "channel": "userFills",
            "data": {"isSnapshot": False, "user": ACCOUNT, "fills": [fill]},
        })
        self.executor.apply_ws_message({
            "channel": "orderUpdates",
            "data": [{
                "order": {
                    "coin": intent.coin, "oid": result.oid, "cloid": intent.cloid,
                    "origSz": str(intent.size), "sz": "0",
                },
                "status": "filled", "statusTimestamp": now_ms(),
            }],
        })
        return result


class AccountWsMonitorTests(unittest.TestCase):
    def setUp(self):
        self.db = storage.connect(":memory:", storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        stamp = now_iso()
        self.session = {
            "session_id": "live-ws-test", "state": "live_running", "account_address": ACCOUNT,
            "agent_address": AGENT, "strategy_revision": "revision-ws", "sizing_anchor": 8000.0,
            "margin_equity_pct": 0.8, "sizing_equity": 6400.0, "canary": False,
            "canary_margin_cap": 0.0, "network": "mainnet", "started_at": stamp,
        }
        self.db.execute(
            "INSERT INTO execution_session "
            "(session_id,mode,network,state,account_address,agent_address,strategy_revision,sizing_anchor,"
            "margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at) "
            "VALUES (?,'live','mainnet','live_running',?,?,?,?,?,?,0,?,?,?)",
            ("live-ws-test", ACCOUNT, AGENT, "revision-ws", 8000.0, 0.8, 6400.0, 0.0, stamp, stamp),
        )
        self.db.execute(
            "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
            "VALUES (1,'live','live_running','live-ws-test',?)", (stamp,),
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _executor(self, broker=None):
        broker = broker or _WsPublishingBroker()
        executor = LiveExecutor(self.db, self.session.copy(), broker)
        executor.available = executor.equity = 8000.0
        return executor

    def _insert_intent(self, *, cloid="0x" + "1" * 32, oid=None, requested=0.2):
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO execution_order_intent "
            "(cloid,session_id,strategy_revision,action_seq,action,coin,side,reduce_only,leverage,"
            "requested_size,requested_limit_px,state,oid,created_at,updated_at) "
            "VALUES (?,'live-ws-test','revision-ws',1,'open','BTC','buy',0,10,?,100,'submitting',?,?,?)",
            (cloid, requested, oid, stamp, stamp),
        )
        self.db.commit()
        return cloid

    def test_subscription_set_matches_official_account_streams(self):
        broker = _WsPublishingBroker()
        executor = self._executor(broker)
        async def build_monitor():
            return LiveAccountMonitor(executor, mode="ws_primary")

        monitor = asyncio.run(build_monitor())
        subscriptions = monitor._build_subscriptions()

        self.assertEqual(
            {row["type"] for row in subscriptions},
            {
                "orderUpdates", "userFills", "allDexsClearinghouseState",
                "openOrders", "spotState",
            },
        )
        self.assertEqual(
            {row.get("dex") for row in subscriptions if row["type"] == "openOrders"},
            {"", "xyz"},
        )
        self.assertIn("openOrders:xyz", monitor._required_snapshots)

    def test_new_live_session_wallet_rebuilds_every_user_subscription(self):
        new_account = "0x" + "c" * 40
        broker = _WsPublishingBroker()
        broker.account_address = new_account
        new_session = self.session.copy()
        new_session["session_id"] = "live-ws-new-wallet"
        new_session["account_address"] = new_account
        executor = LiveExecutor(self.db, new_session, broker)

        async def build_monitor():
            return LiveAccountMonitor(executor, mode="ws_primary")

        monitor = asyncio.run(build_monitor())

        self.assertEqual(monitor.address, new_account)
        for subscription in monitor._build_subscriptions():
            self.assertEqual(subscription["user"], new_account)

    def test_fill_before_order_update_is_backfilled_and_deduplicated(self):
        executor = self._executor()
        cloid = self._insert_intent()
        fill = {
            "tid": "ws-tid-1", "oid": 77, "coin": "BTC", "side": "B", "sz": "0.2",
            "px": "100", "fee": "0.01", "closedPnl": "0", "time": now_ms(),
        }
        fill_message = {"channel": "userFills", "data": {"fills": [fill], "isSnapshot": False}}

        executor.apply_ws_message(fill_message)
        self.assertIsNone(self.db.execute(
            "SELECT cloid FROM execution_fill WHERE tid='ws-tid-1'"
        ).fetchone()[0])
        executor.apply_ws_message({
            "channel": "orderUpdates",
            "data": [{
                "order": {"coin": "BTC", "oid": 77, "cloid": cloid, "origSz": "0.2"},
                "status": "filled", "statusTimestamp": 123,
            }],
        })
        executor.apply_ws_message(fill_message)
        executor.apply_ws_message({
            "channel": "orderUpdates",
            "data": [{
                "order": {"coin": "BTC", "oid": 77, "cloid": cloid, "origSz": "0.2"},
                "status": "filled", "statusTimestamp": 123,
            }],
        })

        intent = self.db.execute(
            "SELECT state,oid,filled_size,exchange_status FROM execution_order_intent WHERE cloid=?",
            (cloid,),
        ).fetchone()
        self.assertEqual(intent, ("filled", 77, 0.2, "filled"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM execution_fill").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM execution_order_event").fetchone()[0], 1)
        self.assertEqual(self.db.execute(
            "SELECT cloid FROM execution_fill WHERE tid='ws-tid-1'"
        ).fetchone()[0], cloid)

    def test_filled_order_status_without_complete_fill_rows_is_not_business_filled(self):
        executor = self._executor()
        cloid = self._insert_intent(requested=0.2)

        executor.apply_ws_message({
            "channel": "orderUpdates",
            "data": [{
                "order": {"coin": "BTC", "oid": 88, "cloid": cloid, "origSz": "0.2"},
                "status": "filled", "statusTimestamp": 456,
            }],
        })

        self.assertEqual(self.db.execute(
            "SELECT state FROM execution_order_intent WHERE cloid=?", (cloid,),
        ).fetchone()[0], "submitting")

    def test_ws_confirmed_order_avoids_account_rest_reconcile(self):
        broker = _WsPublishingBroker()
        executor = self._executor(broker)
        broker.executor = executor
        executor.attach_account_monitor(_HealthyMonitor())

        result = executor.execute(
            coin="BTC", is_buy=True, size=0.2, leverage=10, reduce_only=False,
            action="open", source_address="0xsource", source_fill_id="source-ws-1",
            source_order_id="7", source_time_ms=123456789, action_seq=1,
        )

        self.assertEqual(result.outcome, "filled")
        self.assertAlmostEqual(result.filled_size, 0.2)
        self.assertEqual(broker.account_snapshot_calls, 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM execution_fill").fetchone()[0], 1)

    def test_account_snapshots_update_projection_without_touching_copy_ledger(self):
        executor = self._executor()
        executor.apply_ws_message({
            "channel": "allDexsClearinghouseState",
            "data": {"user": ACCOUNT, "clearinghouseStates": {
                "": {"assetPositions": []}, "xyz": {"assetPositions": []},
            }},
        })
        executor.apply_ws_message({
            "channel": "spotState",
            "data": {"user": ACCOUNT, "spotState": {
                "balances": [{"coin": "USDC", "total": "1234", "hold": "34"}],
            }},
        })

        self.assertEqual((executor.equity, executor.available), (1234.0, 1200.0))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM live_copy_position").fetchone()[0], 0)

    def test_isolated_slow_event_does_not_leave_monitor_degraded(self):
        async def run():
            monitor = LiveAccountMonitor(self._executor(), mode="ws_primary")
            monitor._ready.set()
            monitor._set_state("healthy")
            await monitor._enqueue_message(
                time.monotonic() - 3.0,
                {"channel": "userFills", "data": {"fills": []}},
            )
            consumer = asyncio.create_task(monitor._consume())
            await monitor._drained.wait()
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            self.assertEqual(monitor.state, "healthy")
            self.assertEqual(monitor.queue_lag_ms, 0)

        asyncio.run(run())

    def test_sustained_slow_backlog_degrades_monitor(self):
        async def run():
            monitor = LiveAccountMonitor(self._executor(), mode="ws_primary")
            monitor._ready.set()
            monitor._set_state("healthy")
            message = {"channel": "userFills", "data": {"fills": []}}
            await monitor._enqueue_message(time.monotonic() - 3.0, message)
            await monitor._enqueue_message(time.monotonic() - 3.0, message)
            consumer = asyncio.create_task(monitor._consume())
            await monitor._drained.wait()
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            self.assertEqual(monitor.state, "degraded")
            self.assertEqual(monitor.last_error, "account_ws_queue_lag")

        asyncio.run(run())

    def test_rest_recovery_replay_does_not_retrigger_queue_lag(self):
        async def run():
            monitor = LiveAccountMonitor(self._executor(), mode="ws_primary")
            monitor._ready.set()
            monitor._conn = object()
            monitor.last_message_at = time.time()
            monitor.mark_degraded("account_ws_queue_lag")
            message = {"channel": "userFills", "data": {"fills": []}}
            await monitor._enqueue_message(time.monotonic() - 3.0, message)
            await monitor._enqueue_message(time.monotonic() - 3.0, message)

            monitor.restore_after_rest({"ok": True})
            consumer = asyncio.create_task(monitor._consume())
            await monitor._drained.wait()
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

            self.assertEqual(monitor.state, "healthy")
            self.assertIsNone(monitor.last_error)
            self.assertFalse(monitor._lag_recovery_until_drained)
            self.assertEqual(monitor.queue_lag_ms, 0)

        asyncio.run(run())

    def test_replaceable_account_snapshots_are_coalesced_and_batched(self):
        async def run():
            executor = self._executor()
            applied = []
            monitor = LiveAccountMonitor(executor, mode="ws_primary")
            monitor._ready.set()
            monitor._set_state("healthy")
            base = time.monotonic() - 5.0
            messages = [
                {"channel": "spotState", "data": {"spotState": {"version": 1}}},
                {"channel": "spotState", "data": {"spotState": {"version": 2}}},
                {"channel": "allDexsClearinghouseState", "data": {
                    "clearinghouseStates": {"": {"version": 1}},
                }},
                {"channel": "allDexsClearinghouseState", "data": {
                    "clearinghouseStates": {"": {"version": 2}},
                }},
                {"channel": "openOrders", "data": {"dex": "", "orders": [{"oid": 1}]}},
                {"channel": "openOrders", "data": {"dex": "", "orders": []}},
                {"channel": "openOrders", "data": {"dex": "xyz", "orders": []}},
            ]
            for offset, message in enumerate(messages):
                await monitor._enqueue_message(base + offset / 1000.0, message)

            before = monitor.snapshot()
            self.assertEqual(before["eventQueueDepth"], 0)
            self.assertEqual(before["snapshotSlotDepth"], 4)
            self.assertEqual(before["snapshotCoalescedCount"], 3)

            with patch.object(executor, "apply_ws_messages", side_effect=lambda rows: applied.append(rows)):
                consumer = asyncio.create_task(monitor._consume())
                await monitor._drained.wait()
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)

            self.assertEqual(len(applied), 1)
            self.assertEqual(len(applied[0]), 4)
            by_channel = {row["channel"]: row for row in applied[0] if row["channel"] != "openOrders"}
            self.assertEqual(by_channel["spotState"]["data"]["spotState"]["version"], 2)
            self.assertEqual(
                by_channel["allDexsClearinghouseState"]["data"]["clearinghouseStates"][""]["version"],
                2,
            )
            standard = [
                row for row in applied[0]
                if row["channel"] == "openOrders" and row["data"]["dex"] == ""
            ]
            self.assertEqual(standard[0]["data"]["orders"], [])
            self.assertEqual(monitor.state, "healthy")
            self.assertEqual(monitor.snapshot()["queueDepth"], 0)

        asyncio.run(run())

    def test_critical_order_events_preserve_fifo_ahead_of_snapshots(self):
        async def run():
            executor = self._executor()
            applied = []
            monitor = LiveAccountMonitor(executor, mode="ws_primary")
            monitor._ready.set()
            monitor._set_state("healthy")
            await monitor._enqueue_message(
                time.monotonic(), {"channel": "spotState", "data": {"spotState": {}}},
            )
            for sequence in (1, 2):
                await monitor._enqueue_message(
                    time.monotonic(),
                    {"channel": "orderUpdates", "data": {"sequence": sequence}},
                )

            with patch.object(
                executor, "apply_ws_messages",
                side_effect=lambda rows: applied.append([row["channel"] for row in rows]
                                                        + [rows[0].get("data", {}).get("sequence")]),
            ):
                consumer = asyncio.create_task(monitor._consume())
                await monitor._drained.wait()
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)

            self.assertEqual(applied[0], ["orderUpdates", 1])
            self.assertEqual(applied[1], ["orderUpdates", 2])
            self.assertEqual(applied[2][0], "spotState")

        asyncio.run(run())

    def test_database_busy_degrades_without_freezing_exchange_state(self):
        async def run():
            executor = self._executor()
            monitor = LiveAccountMonitor(executor, mode="ws_primary")
            monitor._ready.set()
            monitor._set_state("healthy")
            await monitor._enqueue_message(
                time.monotonic(), {"channel": "userFills", "data": {"fills": []}},
            )

            with patch.object(
                executor, "apply_ws_messages", side_effect=sqlite3.OperationalError("database is locked"),
            ), patch.object(executor, "rollback_ws_transaction") as rollback, patch.object(
                executor, "freeze_from_monitor",
            ) as freeze:
                consumer = asyncio.create_task(monitor._consume())
                await monitor._drained.wait()
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)

            self.assertEqual(monitor.state, "degraded")
            self.assertEqual(monitor.last_error, "account_ws_database_busy")
            rollback.assert_called_once_with()
            freeze.assert_not_called()

        asyncio.run(run())

    def test_monitor_cancellation_waits_for_sqlite_worker_to_finish(self):
        async def run():
            started = threading.Event()
            release = threading.Event()
            finished = threading.Event()

            def writer():
                started.set()
                release.wait(timeout=2)
                finished.set()

            task = asyncio.create_task(LiveAccountMonitor._complete_thread(writer))
            while not started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(finished.is_set())

        asyncio.run(run())

    def test_successful_rest_recovery_refreshes_stream_state_clocks(self):
        async def run():
            monitor = LiveAccountMonitor(self._executor(), mode="ws_primary")
            monitor._ready.set()
            monitor._conn = object()
            monitor.last_message_at = time.time()
            monitor.last_position_at = time.time() - 500
            monitor._last_spot_at = time.time() - 500
            monitor.mark_degraded("account_state_snapshot_stale")

            before = time.time()
            monitor.restore_after_rest({"ok": True})

            self.assertEqual(monitor.state, "healthy")
            self.assertGreaterEqual(monitor.last_position_at, before)
            self.assertGreaterEqual(monitor._last_spot_at, before)

        asyncio.run(run())

    def test_account_state_batch_limits_history_rows_but_keeps_projection_fresh(self):
        executor = self._executor()
        perp = {
            "channel": "allDexsClearinghouseState",
            "data": {"clearinghouseStates": {
                "": {"assetPositions": []}, "xyz": {"assetPositions": []},
            }},
        }
        spot = {
            "channel": "spotState",
            "data": {"spotState": {
                "balances": [{"coin": "USDC", "total": "1234", "hold": "34"}],
            }},
        }
        executor.apply_ws_messages([perp, spot])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM execution_account_snapshot").fetchone()[0], 1,
        )

        newer_spot = {
            "channel": "spotState",
            "data": {"spotState": {
                "balances": [{"coin": "USDC", "total": "1300", "hold": "20"}],
            }},
        }
        executor.apply_ws_messages([newer_spot])
        self.assertEqual((executor.equity, executor.available), (1300.0, 1280.0))
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM execution_account_snapshot").fetchone()[0], 1,
        )
        with patch("hyper.execution.live_executor.config.ACCOUNT_WS_HISTORY_INTERVAL_S", 0):
            executor.apply_ws_messages([newer_spot])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM execution_account_snapshot").fetchone()[0], 2,
        )

    def test_failed_ws_batch_rolls_back_partial_audit_writes(self):
        executor = self._executor()

        def partially_write_then_fail(_event):
            self.db.execute(
                "INSERT INTO execution_order_event "
                "(event_hash,session_id,exchange_status,raw_json,received_at) "
                "VALUES ('partial','live-ws-test','open','{}',?)",
                (now_iso(),),
            )
            raise ValueError("invalid_order_event")

        with patch.object(executor, "_apply_ws_order_update", side_effect=partially_write_then_fail):
            with self.assertRaisesRegex(ValueError, "invalid_order_event"):
                executor.apply_ws_messages([{"channel": "orderUpdates", "data": [{}]}])

        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM execution_order_event").fetchone()[0], 0,
        )


if __name__ == "__main__":
    unittest.main()
