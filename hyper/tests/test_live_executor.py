import os
import time
import unittest
from types import SimpleNamespace

from hyper import storage
from hyper.execution.hyperliquid_broker import AccountSnapshot, BrokerError
from hyper.execution.live_executor import LiveExecutor
from hyper.execution.orders import (
    ActionResult, MarketSpec, OrderOutcome, SubmitResult, deterministic_cloid, prepare_ioc_order,
)
from hyper.execution.venue import ExecutionNetwork
from hyper.util import now_iso, now_ms


ACCOUNT = "0x" + "a" * 40
AGENT = "0x" + "b" * 40


class FakeLiveBroker:
    def __init__(self, outcomes=None):
        self.venue = SimpleNamespace(network=ExecutionNetwork.MAINNET)
        self.outcomes = list(outcomes or [(OrderOutcome.FILLED, 1.0, None)])
        self.positions = {}
        self.unrealized_pnl = {}
        self.total_equity = 8000.0
        self.orders = []
        self.fills = []
        self.submit_calls = 0
        self.account_snapshot_calls = 0
        self.leverage_calls = []
        self.statuses = {}
        self.book_failures = 0
        self.max_leverage = 50

    def market_spec(self, coin):
        return MarketSpec(coin, "", 0 if coin == "BTC" else 1, 5, self.max_leverage)

    def prepare_order(self, intent):
        return prepare_ioc_order(intent, self.market_spec(intent.coin))

    def market_contexts(self, _dex=""):
        return [
            {"universe": [
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
                {"name": "ETH", "szDecimals": 5, "maxLeverage": 50},
            ]},
            [{"markPx": "100"}, {"markPx": "100"}],
        ]

    def l2_book(self, _coin):
        if self.book_failures:
            self.book_failures -= 1
            raise BrokerError("l2_transport_error")
        return {
            "time": now_ms(),
            "levels": [
                [{"px": "99.99", "sz": "10000"}],
                [{"px": "100.01", "sz": "10000"}],
            ],
        }

    def all_mids(self, _dex=""):
        return {"BTC": "100", "ETH": "100"}

    def set_isolated_leverage(self, coin, leverage):
        self.leverage_calls.append((coin, leverage))
        return ActionResult(True)

    def order_status(self, cloid):
        return self.statuses.get(str(cloid).lower(), {"status": "unknownOid"})

    def submit_ioc(self, intent):
        self.submit_calls += 1
        outcome, fraction, error = self.outcomes.pop(0)
        filled = intent.size * fraction if outcome in {OrderOutcome.FILLED, OrderOutcome.PARTIAL} else 0.0
        if intent.reduce_only:
            current = self.positions.get(intent.coin, 0.0)
            filled = min(filled, abs(current))
            if filled + 1e-12 < intent.size and filled > 0.0:
                outcome = OrderOutcome.PARTIAL
        oid = 1000 + self.submit_calls
        if filled:
            signed = filled if intent.is_buy else -filled
            current = self.positions.get(intent.coin, 0.0)
            next_size = current + signed
            if intent.reduce_only:
                if current > 0:
                    next_size = max(0.0, next_size)
                elif current < 0:
                    next_size = min(0.0, next_size)
            if abs(next_size) <= 1e-12:
                self.positions.pop(intent.coin, None)
            else:
                self.positions[intent.coin] = next_size
            self.fills.append({
                "tid": str(oid), "cloid": intent.cloid, "oid": oid, "coin": intent.coin,
                "side": "B" if intent.is_buy else "A", "sz": str(filled), "px": str(intent.limit_px),
                "fee": str(filled * intent.limit_px * 0.00045), "closedPnl": "0", "time": now_ms(),
            })
        return SubmitResult(
            outcome, oid=oid, filled_size=filled,
            average_px=intent.limit_px if filled else None, error_code=error,
        )

    def recent_fills(self):
        return list(self.fills)

    def fills_by_time(self, start_time_ms, _end_time_ms=None):
        return [row for row in self.fills if int(row.get("time") or 0) >= int(start_time_ms)]

    def cancel_by_cloid(self, coin, cloid):
        before = len(self.orders)
        self.orders = [
            row for row in self.orders
            if not (row.get("coin") == coin and str(row.get("cloid") or "").lower() == str(cloid).lower())
        ]
        return ActionResult(len(self.orders) < before)

    def account_snapshot(self):
        self.account_snapshot_calls += 1
        rows = []
        for coin, size in self.positions.items():
            rows.append({"position": {
                "coin": coin, "szi": str(size), "entryPx": "100",
                "positionValue": str(abs(size) * 100), "marginUsed": str(abs(size) * 10),
                "leverage": {"type": "isolated", "value": 10},
                "unrealizedPnl": str(self.unrealized_pnl.get(coin, 0.0)), "liquidationPx": "50",
            }})
        return AccountSnapshot(
            network=ExecutionNetwork.MAINNET, account_address=ACCOUNT,
            abstraction="unifiedAccount",
            collateral_state={
                "balances": [{"coin": "USDC", "total": str(self.total_equity), "hold": "0"}],
            },
            perp_states={"": {"assetPositions": rows}, "xyz": {"assetPositions": []}},
            open_orders={"": list(self.orders), "xyz": []},
            frontend_open_orders={"": list(self.orders), "xyz": []},
        )


class LiveExecutorTests(unittest.TestCase):
    def setUp(self):
        self.db = storage.connect(":memory:", storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        stamp = now_iso()
        self.session = {
            "session_id": "live-test", "state": "live_canary", "account_address": ACCOUNT,
            "agent_address": AGENT, "strategy_revision": "revision-one", "sizing_anchor": 8000.0,
            "margin_equity_pct": 0.8, "sizing_equity": 6400.0, "canary": True,
            "canary_margin_cap": 80.0, "network": "mainnet", "started_at": stamp,
        }
        self.db.execute(
            "INSERT INTO execution_session "
            "(session_id,mode,network,state,account_address,agent_address,strategy_revision,sizing_anchor,"
            "margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at) "
            "VALUES (?,'live','mainnet','live_canary',?,?,?,?,?,?,1,?,?,?)",
            ("live-test", ACCOUNT, AGENT, "revision-one", 8000.0, 0.8, 6400.0, 80.0, stamp, stamp),
        )
        self.db.execute(
            "INSERT INTO execution_control (id,selected_mode,state,active_session_id,updated_at) "
            "VALUES (1,'live','live_canary','live-test',?)", (stamp,),
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def execute(self, executor, **changes):
        values = {
            "coin": "BTC", "is_buy": True, "size": 0.2, "leverage": 10,
            "reduce_only": False, "action": "open", "source_address": "0xsource",
            "source_fill_id": "source-fill-1", "source_order_id": "7",
            "source_time_ms": 123456789, "action_seq": 1,
        }
        values.update(changes)
        return executor.execute(**values)

    def _insert_filled_intent(self, *, coin="BTC", side="sell", size=0.2, cloid=None, oid=77):
        cloid = cloid or ("0x" + "7" * 32)
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO execution_order_intent "
            "(cloid,session_id,strategy_revision,source_address,source_fill_id,source_order_id,"
            "source_time_ms,action_seq,action,coin,side,reduce_only,leverage,requested_size,"
            "requested_limit_px,state,oid,filled_size,average_px,created_at,updated_at) "
            "VALUES (?,'live-test','revision-one','0xsource','prior-fill','6',123456000,1,"
            "'open',?,?,0,10,?,100,'filled',?,?,100,?,?)",
            (cloid, coin, side, size, oid, size, stamp, stamp),
        )
        self.db.commit()
        return cloid

    def test_full_fill_is_durable_and_same_logical_action_does_not_resubmit(self):
        broker = FakeLiveBroker()
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        first = self.execute(executor)
        second = self.execute(executor)

        self.assertEqual(first.outcome, "filled")
        self.assertAlmostEqual(first.filled_size, 0.2)
        self.assertEqual(second.cloids, first.cloids)
        self.assertEqual(broker.submit_calls, 1)
        self.assertEqual(
            self.db.execute("SELECT state FROM execution_order_intent").fetchone()[0], "filled",
        )
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM execution_fill").fetchone()[0], 1)

    def test_paused_session_may_recover_existing_fill_without_new_submission(self):
        broker = FakeLiveBroker()
        executor = LiveExecutor(self.db, self.session.copy(), broker)
        first = self.execute(executor)
        self.db.execute(
            "UPDATE execution_session SET state='paused' WHERE session_id='live-test'"
        )
        self.db.execute("UPDATE execution_control SET state='paused' WHERE id=1")
        self.db.commit()

        recovered = self.execute(executor)

        self.assertEqual(recovered.cloids, first.cloids)
        self.assertAlmostEqual(recovered.filled_size, first.filled_size)
        self.assertEqual(broker.submit_calls, 1)

    def test_verified_canceled_ambiguous_order_retries_only_remaining_size(self):
        broker = FakeLiveBroker()
        cloid = deterministic_cloid(
            "live-test", "revision-one", "0xsource", "source-fill-1", "7",
            "BTC", "open", 1, 0,
        )
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO execution_order_intent "
            "(cloid,session_id,strategy_revision,source_address,source_fill_id,source_order_id,"
            "source_time_ms,action_seq,action,coin,side,reduce_only,leverage,requested_size,"
            "requested_limit_px,state,error_code,created_at,updated_at) "
            "VALUES (?,'live-test','revision-one','0xsource','source-fill-1','7',123456789,1,"
            "'open','BTC','buy',0,10,.2,100,'ambiguous','transport_ambiguous',?,?)",
            (cloid, stamp, stamp),
        )
        self.db.commit()
        broker.statuses[cloid] = {
            "status": "order",
            "order": {"status": "canceled", "order": {"oid": 999, "status": "canceled"}},
        }
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = self.execute(executor)

        self.assertAlmostEqual(result.filled_size, 0.2)
        self.assertEqual(broker.submit_calls, 1)
        self.assertEqual(len(result.cloids), 2)
        self.assertEqual(
            self.db.execute(
                "SELECT state FROM execution_order_intent WHERE cloid=?", (cloid,),
            ).fetchone()[0],
            "canceled",
        )

    def test_lease_reclaims_immediately_when_previous_worker_pid_is_dead(self):
        self.db.execute(
            "INSERT INTO execution_lease (id,owner,acquired_at,heartbeat_at,expires_at_ms) "
            "VALUES (1,'live-observer:999999999:old',?,?,?)",
            (now_iso(), now_iso(), now_ms() + 60_000),
        )
        self.db.commit()

        executor = LiveExecutor(self.db, self.session.copy(), FakeLiveBroker())

        owner = self.db.execute("SELECT owner FROM execution_lease WHERE id=1").fetchone()[0]
        self.assertEqual(owner, executor.owner)

    def test_lease_still_rejects_a_different_live_worker(self):
        self.db.execute(
            "INSERT INTO execution_lease (id,owner,acquired_at,heartbeat_at,expires_at_ms) "
            "VALUES (1,?,?,?,?)",
            (f"live-observer:{os.getpid()}:old", now_iso(), now_iso(), now_ms() + 60_000),
        )
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "live_execution_lease_held"):
            LiveExecutor(self.db, self.session.copy(), FakeLiveBroker())

    def test_canary_clamps_margin_instead_of_rejecting_whole_signal(self):
        broker = FakeLiveBroker()
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = self.execute(executor, size=20.0)

        self.assertAlmostEqual(result.filled_size, 8.0, places=5)
        self.assertEqual(broker.submit_calls, 1)

    def test_increase_clips_tier_leverage_to_official_market_max_and_preserves_margin(self):
        broker = FakeLiveBroker()
        broker.max_leverage = 10
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = self.execute(executor, size=0.24, leverage=12)

        self.assertEqual(result.leverage, 10)
        self.assertEqual(broker.leverage_calls[-1], ("BTC", 10))
        self.assertAlmostEqual(result.filled_size, 0.20, places=8)
        intent = self.db.execute(
            "SELECT leverage,requested_size FROM execution_order_intent"
        ).fetchone()
        self.assertEqual(intent[0], 10)
        self.assertAlmostEqual(intent[1], 0.20, places=8)

    def test_partial_ioc_requotes_once_and_partial_is_terminal(self):
        broker = FakeLiveBroker([
            (OrderOutcome.PARTIAL, 0.4, "ioc_cancel"),
            (OrderOutcome.PARTIAL, 0.5, "ioc_cancel"),
        ])
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = self.execute(executor)

        self.assertEqual(broker.submit_calls, 2)
        self.assertEqual(len(result.cloids), 2)
        self.assertGreater(result.filled_size, 0)
        self.assertLess(result.filled_size, 0.2)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM execution_order_intent WHERE state='partial'").fetchone()[0], 2,
        )

    def test_transient_quote_read_is_retried_before_any_signed_order(self):
        broker = FakeLiveBroker()
        broker.book_failures = 1
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = self.execute(executor)

        self.assertEqual(result.outcome, "filled")
        self.assertEqual(broker.submit_calls, 1)

    def test_existing_nonterminal_intent_freezes_without_resubmit(self):
        broker = FakeLiveBroker()
        executor = LiveExecutor(self.db, self.session.copy(), broker)
        cloid = deterministic_cloid(
            "live-test", "revision-one", "0xsource", "source-fill-1", "7", "BTC", "open", 1, 0,
        )
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO execution_order_intent "
            "(cloid,session_id,strategy_revision,source_address,source_fill_id,source_order_id,source_time_ms,"
            "action_seq,action,coin,side,reduce_only,leverage,requested_size,requested_limit_px,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cloid, "live-test", "revision-one", "0xsource", "source-fill-1", "7", 123456789,
             1, "open", "BTC", "buy", 0, 10, 0.2, 100, "submitting", stamp, stamp),
        )
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "live_reconcile_required"):
            self.execute(executor)

        self.assertEqual(broker.submit_calls, 0)
        self.assertEqual(
            self.db.execute("SELECT state FROM execution_control WHERE id=1").fetchone()[0],
            "reconcile_required",
        )
        self.assertEqual(
            self.db.execute("SELECT state FROM execution_session WHERE session_id='live-test'").fetchone()[0],
            "reconcile_required",
        )

    def test_ambiguous_intent_recovers_from_authoritative_fill_then_stays_paused(self):
        broker = FakeLiveBroker()
        cloid = deterministic_cloid(
            "live-test", "revision-one", "0xsource", "source-fill-1", "7", "BTC", "open", 1, 0,
        )
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO execution_order_intent "
            "(cloid,session_id,strategy_revision,source_address,source_fill_id,source_order_id,source_time_ms,"
            "action_seq,action,coin,side,reduce_only,leverage,requested_size,requested_limit_px,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cloid, "live-test", "revision-one", "0xsource", "source-fill-1", "7", 123456789,
             1, "open", "BTC", "buy", 0, 10, 0.2, 100, "ambiguous", stamp, stamp),
        )
        self.db.execute("UPDATE execution_session SET state='reconcile_required' WHERE session_id='live-test'")
        self.db.execute("UPDATE execution_control SET state='reconcile_required' WHERE id=1")
        self.db.commit()
        broker.positions["BTC"] = 0.2
        broker.fills.append({
            "tid": "recovered-1", "cloid": cloid, "oid": 77, "coin": "BTC", "side": "B",
            "sz": "0.2", "px": "100", "fee": "0.009", "closedPnl": "0", "time": now_ms(),
        })
        broker.statuses[cloid] = {
            "status": "order", "order": {"order": {"oid": 77}, "status": "filled"},
        }
        session = self.session.copy()
        session["state"] = "reconcile_required"
        executor = LiveExecutor(self.db, session, broker)

        result = executor.reconcile()

        self.assertTrue(result["ok"])
        self.assertEqual(result["ambiguousIntents"], 0)
        self.assertEqual(
            self.db.execute("SELECT state,filled_size FROM execution_order_intent WHERE cloid=?", (cloid,)).fetchone(),
            ("filled", 0.2),
        )
        self.assertEqual(self.db.execute("SELECT state FROM execution_control WHERE id=1").fetchone()[0], "paused")

    def test_unknown_exchange_position_freezes_increases(self):
        broker = FakeLiveBroker()
        broker.positions["ETH"] = 1.0
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = executor.reconcile()

        self.assertFalse(result["ok"])
        self.assertEqual(result["unknownPositions"], 1)
        self.assertEqual(
            self.db.execute("SELECT state FROM execution_control WHERE id=1").fetchone()[0],
            "reconcile_required",
        )

    def test_explicit_unlinked_liquidation_reconciles_managed_position_to_flat(self):
        broker = FakeLiveBroker()
        self._insert_filled_intent(side="sell", size=0.2)
        broker.fills.append({
            "tid": "self-liq-1", "oid": 9001, "coin": "BTC", "side": "B",
            "sz": "0.2", "px": "90", "fee": "0.01", "closedPnl": "-2",
            "dir": "Liquidated Isolated Short", "time": now_ms(),
        })
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = executor.reconcile()

        self.assertTrue(result["ok"])
        self.assertEqual(result["unknownPositions"], 0)
        self.assertEqual(result["positions"], [])
        self.assertEqual(executor.unmatched_account_fill_count(), 0)

    def test_unlinked_manual_fill_remains_unknown(self):
        broker = FakeLiveBroker()
        self._insert_filled_intent(side="sell", size=0.2)
        broker.fills.append({
            "tid": "manual-1", "oid": 9002, "coin": "BTC", "side": "B",
            "sz": "0.2", "px": "90", "fee": "0.01", "closedPnl": "-2",
            "dir": "Close Short", "time": now_ms(),
        })
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = executor.reconcile()

        self.assertFalse(result["ok"])
        self.assertEqual(executor.unmatched_account_fill_count(), 1)

    def test_reduce_only_uses_coin_exchange_position_during_unrelated_drift(self):
        broker = FakeLiveBroker()
        self._insert_filled_intent(side="sell", size=0.2)
        broker.positions.update({"BTC": -0.2, "ETH": 1.0})
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = self.execute(
            executor, is_buy=True, size=0.2, reduce_only=True, action="close",
            source_fill_id="close-btc", source_order_id="8", action_seq=2,
        )

        self.assertAlmostEqual(result.filled_size, 0.2)
        self.assertEqual(broker.submit_calls, 1)
        self.assertNotIn("BTC", broker.positions)
        self.assertEqual(
            self.db.execute("SELECT state FROM execution_control WHERE id=1").fetchone()[0],
            "reconcile_required",
        )

    def test_reduce_only_reuses_fresh_successful_rest_projection(self):
        broker = FakeLiveBroker()
        self._insert_filled_intent(side="sell", size=0.2)
        broker.positions["BTC"] = -0.2
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO execution_position_projection "
            "(session_id,dex,coin,signed_size,observed_at) "
            "VALUES ('live-test','','BTC',-.2,?)",
            (stamp,),
        )
        self.db.execute(
            "INSERT INTO execution_reconcile_checkpoint(session_id,status,created_at) "
            "VALUES ('live-test','ok',?)",
            (stamp,),
        )
        self.db.commit()
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = self.execute(
            executor, is_buy=True, size=0.2, reduce_only=True, action="close",
            source_fill_id="cached-close", source_order_id="8", action_seq=2,
        )

        self.assertAlmostEqual(result.filled_size, 0.2)
        self.assertEqual(broker.account_snapshot_calls, 0)
        self.assertNotIn("BTC", broker.positions)

    def test_reduce_only_dust_close_uses_venue_minimum_wire_size_without_flipping(self):
        broker = FakeLiveBroker()
        self._insert_filled_intent(side="sell", size=0.0001)
        broker.positions["BTC"] = -0.0001
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = self.execute(
            executor, is_buy=True, size=0.000099999999999995,
            reduce_only=True, action="close", source_fill_id="dust-close",
            source_order_id="8", action_seq=2,
        )

        self.assertEqual(result.outcome, "filled")
        self.assertAlmostEqual(result.filled_size, 0.0001)
        self.assertEqual(broker.submit_calls, 1)
        self.assertNotIn("BTC", broker.positions)
        intent = self.db.execute(
            "SELECT requested_size,filled_size,state FROM execution_order_intent "
            "WHERE source_fill_id='dust-close'"
        ).fetchone()
        self.assertGreater(intent[0] * 100.0, 10.0)
        self.assertAlmostEqual(intent[1], 0.0001)
        self.assertEqual(intent[2], "partial")

    def test_reduce_only_refreshes_projection_older_than_thirty_seconds(self):
        broker = FakeLiveBroker()
        self._insert_filled_intent(side="sell", size=0.2)
        broker.positions["BTC"] = -0.2
        stale_stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 31))
        self.db.execute(
            "INSERT INTO execution_position_projection "
            "(session_id,dex,coin,signed_size,observed_at) "
            "VALUES ('live-test','','BTC',-.2,?)",
            (stale_stamp,),
        )
        self.db.execute(
            "INSERT INTO execution_reconcile_checkpoint(session_id,status,created_at) "
            "VALUES ('live-test','ok',?)",
            (stale_stamp,),
        )
        self.db.commit()
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = self.execute(
            executor, is_buy=True, size=0.2, reduce_only=True, action="close",
            source_fill_id="stale-cached-close", source_order_id="8", action_seq=2,
        )

        self.assertAlmostEqual(result.filled_size, 0.2)
        self.assertEqual(broker.account_snapshot_calls, 1)
        self.assertNotIn("BTC", broker.positions)

    def test_increase_stays_blocked_during_unrelated_drift(self):
        broker = FakeLiveBroker()
        broker.positions["ETH"] = 1.0
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        with self.assertRaisesRegex(RuntimeError, "live_reconcile_required"):
            self.execute(executor)

        self.assertEqual(broker.submit_calls, 0)

    def test_reconcile_does_not_add_unrealized_pnl_to_unified_total_equity(self):
        broker = FakeLiveBroker()
        broker.positions["BTC"] = 1.0
        broker.unrealized_pnl["BTC"] = 125.0
        broker.total_equity = 8125.0
        executor = LiveExecutor(self.db, self.session.copy(), broker)

        result = executor.reconcile()

        self.assertEqual(result["equity"], 8125.0)
        snapshot = self.db.execute(
            "SELECT equity,unrealized_pnl,equity_projection_version "
            "FROM execution_account_snapshot ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()
        account = self.db.execute(
            "SELECT balance,equity_projection_version FROM live_copy_account WHERE id=1"
        ).fetchone()
        self.assertEqual(snapshot, (8125.0, 125.0, 2))
        self.assertEqual(account, (8125.0, 2))


if __name__ == "__main__":
    unittest.main()
