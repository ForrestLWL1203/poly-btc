import unittest

from hyper.execution.hyperliquid_broker import (
    BrokerError,
    BrokerProtocolError,
    HyperliquidBroker,
    MainnetSigningDisabled,
)
from hyper.execution.orders import OrderIntent, OrderOutcome, deterministic_cloid
from hyper.execution.testnet_verifier import TestnetScenarioRunner
from hyper.execution.venue import ExecutionNetwork, venue_config


ACCOUNT = "0x" + "a" * 40
AGENT = "0x" + "b" * 40


class FakeInfo:
    def __init__(self):
        self.calls = []

    def perp_dexs(self):
        return [None, {"name": "xyz"}]

    def meta(self, dex=""):
        self.calls.append(("meta", dex))
        if dex == "":
            return {"universe": [
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
                {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
            ]}
        if dex == "xyz":
            return {"universe": [
                {"name": "xyz:XYZ100", "szDecimals": 2, "maxLeverage": 10},
            ]}
        return {"universe": []}

    def l2_snapshot(self, coin):
        return {"coin": coin, "levels": [[{"px": "99", "sz": "2"}], [{"px": "101", "sz": "2"}]]}

    def all_mids(self, dex=""):
        return {"BTC": "100"} if not dex else {"xyz:XYZ100": "50"}

    def meta_and_asset_ctxs(self):
        return [self.meta(), [{"markPx": "100"}, {"markPx": "10"}]]

    def post(self, path, payload):
        if path == "/info" and payload == {"type": "metaAndAssetCtxs", "dex": "xyz"}:
            return [self.meta("xyz"), [{"markPx": "50"}]]
        raise AssertionError((path, payload))

    def user_role(self, address):
        if address == AGENT:
            return {"role": "agent", "data": {"user": ACCOUNT}}
        return {"role": "user"}

    def extra_agents(self, address):
        self.calls.append(("extra_agents", address))
        return [{"name": "copy-agent", "address": AGENT, "validUntil": 4_102_444_800_000}]

    def query_user_abstraction_state(self, address):
        self.calls.append(("abstraction", address))
        return "unifiedAccount"

    def spot_user_state(self, address):
        return {"balances": [{"coin": "USDC", "total": "1000", "hold": "0"}]}

    def user_state(self, address, dex=""):
        return {"dex": dex, "assetPositions": []}

    def open_orders(self, address, dex=""):
        return []

    def frontend_open_orders(self, address, dex=""):
        return []

    def user_fills(self, address):
        return [{"coin": "BTC", "time": 10}]

    def user_fills_by_time(self, address, start_time, end_time=None, aggregate_by_time=False):
        return [{"coin": "BTC", "time": start_time, "end": end_time}]

    def historical_orders(self, address):
        return [{"order": {"coin": "BTC"}, "status": "filled"}]

    def query_order_by_cloid(self, address, cloid):
        return {"address": address, "cloid": cloid.to_raw()}

    def query_order_by_oid(self, address, oid):
        return {"address": address, "oid": oid}


class FakeExchange:
    def __init__(self):
        self.orders = []
        self.leverage = []
        self.cancels = []

    def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False, cloid=None):
        self.orders.append({
            "coin": coin, "is_buy": is_buy, "size": size, "limit_px": limit_px,
            "order_type": order_type, "reduce_only": reduce_only, "cloid": cloid.to_raw(),
        })
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"filled": {"totalSz": str(size), "avgPx": str(limit_px), "oid": 17}},
        ]}}}

    def update_leverage(self, leverage, coin, is_cross=True):
        self.leverage.append((leverage, coin, is_cross))
        return {"status": "ok", "response": {"type": "default"}}

    def cancel_by_cloid(self, coin, cloid):
        self.cancels.append((coin, cloid.to_raw()))
        return {"status": "ok", "response": {
            "type": "cancel", "data": {"statuses": ["success"]},
        }}

    def cancel(self, coin, oid):
        self.cancels.append((coin, oid))
        return {"status": "ok", "response": {
            "type": "cancel", "data": {"statuses": ["success"]},
        }}


class HyperliquidBrokerTests(unittest.TestCase):
    def broker(self, network=ExecutionNetwork.TESTNET, *, dexes=("", "xyz")):
        self.info = FakeInfo()
        self.exchange = FakeExchange()
        return HyperliquidBroker(
            network, ACCOUNT, info_client=self.info, exchange_client=self.exchange,
            supported_dexes=dexes,
        )

    @staticmethod
    def intent(coin="BTC", **changes):
        values = {
            "coin": coin,
            "is_buy": True,
            "size": 0.001,
            "limit_px": 60_000,
            "reduce_only": False,
            "cloid": deterministic_cloid("session", coin, "open"),
        }
        values.update(changes)
        return OrderIntent(**values)

    def test_official_venue_urls_are_separate(self):
        self.assertIn("testnet", venue_config("testnet").api_url)
        self.assertNotIn("testnet", venue_config("mainnet").api_url)

    def test_catalog_uses_official_builder_asset_offset(self):
        specs = self.broker().load_market_specs()

        self.assertEqual(specs["BTC"].asset_id, 0)
        self.assertEqual(specs["ETH"].asset_id, 1)
        self.assertEqual(specs["xyz:XYZ100"].asset_id, 110_000)
        self.assertEqual(specs["xyz:XYZ100"].dex, "xyz")

    def test_standard_dex_is_canonicalized_first(self):
        broker = self.broker(dexes=("xyz", ""))

        self.assertEqual(broker.supported_dexes, ("", "xyz"))
        self.assertIn("xyz:XYZ100", broker.load_market_specs())

    def test_missing_supported_builder_dex_fails_closed(self):
        broker = self.broker(dexes=("", "missing"))

        with self.assertRaisesRegex(BrokerProtocolError, "supported_dex_missing"):
            broker.load_market_specs()

    def test_testnet_submit_is_ioc_and_preserves_reduce_only_and_cloid(self):
        broker = self.broker()
        intent = self.intent(reduce_only=True)

        result = broker.submit_ioc(intent)

        self.assertEqual(result.outcome, OrderOutcome.FILLED)
        sent = self.exchange.orders[0]
        self.assertEqual(sent["order_type"], {"limit": {"tif": "Ioc"}})
        self.assertTrue(sent["reduce_only"])
        self.assertEqual(sent["cloid"], intent.cloid)

    def test_transport_error_does_not_expose_exception_message_or_cause(self):
        broker = self.broker()

        def fail(*args, **kwargs):
            raise RuntimeError("agent-private-key-must-not-leak")

        self.exchange.order = fail
        with self.assertRaises(BrokerError) as caught:
            broker.submit_ioc(self.intent())

        self.assertEqual(str(caught.exception), "order_transport_error:RuntimeError")
        self.assertIsNone(caught.exception.__cause__)

    def test_mainnet_signed_order_is_hard_disabled_before_transport(self):
        broker = self.broker(ExecutionNetwork.MAINNET)

        with self.assertRaisesRegex(MainnetSigningDisabled, "mainnet_signed_trading_not_enabled"):
            broker.submit_ioc(self.intent())
        self.assertEqual(self.exchange.orders, [])

    def test_mainnet_leverage_and_cancel_are_also_hard_disabled(self):
        broker = self.broker(ExecutionNetwork.MAINNET)
        cloid = deterministic_cloid("session", "BTC", "close")

        with self.assertRaises(MainnetSigningDisabled):
            broker.set_isolated_leverage("BTC", 5)
        with self.assertRaises(MainnetSigningDisabled):
            broker.cancel_by_cloid("BTC", cloid)
        with self.assertRaises(MainnetSigningDisabled):
            broker.cancel_by_oid("BTC", 17)
        self.assertEqual(self.exchange.leverage, [])
        self.assertEqual(self.exchange.cancels, [])

    def test_testnet_leverage_is_isolated_and_clipped_to_market_max(self):
        broker = self.broker()

        result = broker.set_isolated_leverage("BTC", 20)

        self.assertTrue(result.ok)
        self.assertEqual(self.exchange.leverage, [(20, "BTC", False)])
        self.assertTrue(broker.set_cross_leverage("BTC", 5).ok)
        self.assertEqual(self.exchange.leverage[-1], (5, "BTC", True))
        with self.assertRaisesRegex(ValueError, "invalid_leverage"):
            broker.set_isolated_leverage("xyz:XYZ100", 11)

    def test_account_and_identity_reads_use_main_account_not_agent(self):
        broker = self.broker()

        identity = broker.identity_snapshot(AGENT)
        account = broker.account_snapshot()

        self.assertEqual(identity.agent_role["data"]["user"], ACCOUNT)
        self.assertEqual(account.account_address, ACCOUNT)
        self.assertEqual(set(account.perp_states), {"", "xyz"})
        self.assertEqual(set(account.open_orders), {"", "xyz"})

    def test_lightweight_collateral_read_uses_spot_state_only(self):
        broker = self.broker()

        state = broker.collateral_state()

        self.assertEqual(state["balances"][0]["total"], "1000")
        self.assertNotIn(("abstraction", ACCOUNT), self.info.calls)

    def test_agent_authorization_reads_official_expiry_for_main_account(self):
        broker = self.broker()

        authorization = broker.agent_authorization(AGENT)

        self.assertEqual(authorization["validUntil"], 4_102_444_800_000)
        self.assertIn(("extra_agents", ACCOUNT), self.info.calls)

    def test_unknown_agent_has_no_authorization(self):
        broker = self.broker()

        self.assertIsNone(broker.agent_authorization("0x" + "c" * 40))

    def test_order_status_queries_by_cloid_for_main_account(self):
        broker = self.broker()
        cloid = deterministic_cloid("session", "BTC", "open")

        status = broker.order_status(cloid)

        self.assertEqual(status, {"address": ACCOUNT, "cloid": cloid})

    def test_oid_query_and_cancel_use_main_account_and_validated_id(self):
        broker = self.broker()

        self.assertEqual(broker.order_status_by_oid(17), {"address": ACCOUNT, "oid": 17})
        self.assertTrue(broker.cancel_by_oid("BTC", 17).ok)
        self.assertEqual(self.exchange.cancels, [("BTC", 17)])
        with self.assertRaisesRegex(ValueError, "invalid_order_id"):
            broker.order_status_by_oid(0)

    def test_read_api_surface_routes_dex_and_uses_main_account(self):
        broker = self.broker()

        self.assertEqual(broker.all_mids("xyz"), {"xyz:XYZ100": "50"})
        self.assertEqual(broker.market_contexts("xyz")[1][0]["markPx"], "50")
        self.assertEqual(broker.recent_fills()[0]["coin"], "BTC")
        self.assertEqual(broker.fills_by_time(10, 20)[0]["end"], 20)
        self.assertEqual(broker.historical_orders()[0]["status"], "filled")

        with self.assertRaisesRegex(ValueError, "invalid_fill_time_range"):
            broker.fills_by_time(20, 10)

    def test_comprehensive_scenario_runner_is_testnet_only_and_bounded(self):
        runner = TestnetScenarioRunner(self.broker(), AGENT)

        self.assertEqual(runner.notional, 16.0)
        with self.assertRaisesRegex(ValueError, "notional_below_safety_floor"):
            TestnetScenarioRunner(self.broker(), AGENT, notional=14.99)
        with self.assertRaisesRegex(ValueError, "require_testnet"):
            TestnetScenarioRunner(self.broker(ExecutionNetwork.MAINNET), AGENT)

    def test_scenario_runner_fails_closed_on_agent_owner_mismatch(self):
        report = TestnetScenarioRunner(self.broker(), "0x" + "c" * 40).run()

        self.assertFalse(report["ok"])
        self.assertEqual(report["failure"], "agent_owner_mismatch")
        self.assertEqual(report["final"]["openOrders"], 0)


if __name__ == "__main__":
    unittest.main()
