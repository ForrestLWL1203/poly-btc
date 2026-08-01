"""Destructive-but-bounded Testnet execution scenario verifier.

This module never constructs credentials and cannot target Mainnet.  The caller injects a Testnet broker after
the secure credential boundary has validated the Agent.  Every run starts from a clean account and finishes
with cancel-all plus position flattening, including failure paths.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .hyperliquid_broker import HyperliquidBroker
from .orders import (
    ClientOrderId,
    OrderIntent,
    OrderOutcome,
    deterministic_cloid,
    normalize_order_response,
)
from .venue import ExecutionNetwork


class ScenarioError(RuntimeError):
    pass


class TestnetScenarioRunner:
    def __init__(
        self,
        broker: HyperliquidBroker,
        agent_address: str,
        *,
        notional: float = 16.0,
        leverage: int = 2,
        slippage_bps: int = 100,
    ):
        if broker.venue.network is not ExecutionNetwork.TESTNET:
            raise ValueError("testnet_scenarios_require_testnet_broker")
        if notional < 15:
            raise ValueError("testnet_scenario_notional_below_safety_floor")
        if leverage < 1:
            raise ValueError("invalid_testnet_scenario_leverage")
        if not 1 <= slippage_bps <= 500:
            raise ValueError("invalid_testnet_scenario_slippage")
        self.broker = broker
        self.agent_address = str(agent_address).lower()
        self.notional = float(notional)
        self.leverage = int(leverage)
        self.slippage_bps = int(slippage_bps)
        self.session = str(time.time_ns())
        self.started_ms = int(time.time() * 1000)
        self.results: List[Dict[str, Any]] = []

    @staticmethod
    def _dex(coin: str) -> str:
        return coin.split(":", 1)[0] if ":" in coin else ""

    def _position(self, coin: str) -> float:
        state = self.broker.info.user_state(self.broker.account_address, dex=self._dex(coin))
        rows = state.get("assetPositions") if isinstance(state, dict) else None
        if not isinstance(rows, list):
            raise ScenarioError("invalid_asset_positions")
        for row in rows:
            position = row.get("position") if isinstance(row, dict) else None
            if isinstance(position, dict) and position.get("coin") == coin:
                return float(position.get("szi") or 0)
        return 0.0

    def _wait_position(self, coin: str, predicate, *, timeout: float = 10.0) -> float:
        deadline = time.monotonic() + timeout
        value = self._position(coin)
        while not predicate(value) and time.monotonic() < deadline:
            time.sleep(0.25)
            value = self._position(coin)
        return value

    def _top(self, coin: str) -> tuple[float, float]:
        levels = self.broker.l2_book(coin).get("levels")
        try:
            bid = float(levels[0][0]["px"])
            ask = float(levels[1][0]["px"])
        except (IndexError, KeyError, TypeError, ValueError):
            raise ScenarioError(f"missing_l2_top:{coin}") from None
        if bid <= 0 or ask <= 0 or ask < bid:
            raise ScenarioError(f"invalid_l2_top:{coin}")
        return bid, ask

    def _send(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        *,
        reduce_only: bool,
        label: str,
        slippage_bps: Optional[int] = None,
    ):
        bid, ask = self._top(coin)
        bps = self.slippage_bps if slippage_bps is None else int(slippage_bps)
        reference = ask if is_buy else bid
        multiplier = 1 + bps / 10_000 if is_buy else 1 - bps / 10_000
        intent = OrderIntent(
            coin=coin,
            is_buy=is_buy,
            size=abs(size),
            limit_px=reference * multiplier,
            reduce_only=reduce_only,
            cloid=deterministic_cloid("testnet-scenarios", self.session, coin, label),
        )
        result = self.broker.submit_ioc(intent)
        if result.outcome not in (OrderOutcome.FILLED, OrderOutcome.PARTIAL):
            raise ScenarioError(f"{label}_not_filled:{result.outcome.value}:{result.error_code}")
        self.results.append({
            "case": label,
            "coin": coin,
            "outcome": result.outcome.value,
            "filledSize": result.filled_size,
            "averagePx": result.average_px,
            "oid": result.oid,
            "cloid": intent.cloid,
        })
        return result

    def _open_notional(self, coin: str, is_buy: bool, notional: float, label: str):
        bid, ask = self._top(coin)
        reference = ask if is_buy else bid
        multiplier = 1 + self.slippage_bps / 10_000 if is_buy else 1 - self.slippage_bps / 10_000
        return self._send(
            coin,
            is_buy,
            notional / (reference * multiplier),
            reduce_only=False,
            label=label,
        )

    def _flatten(self, coin: str) -> float:
        remaining = self._position(coin)
        for attempt in range(1, 4):
            if abs(remaining) < 1e-12:
                break
            self._send(
                coin,
                remaining < 0,
                abs(remaining),
                reduce_only=True,
                label=f"emergency_flatten_{coin}_{attempt}",
                slippage_bps=max(self.slippage_bps, 250),
            )
            remaining = self._wait_position(coin, lambda value: abs(value) < 1e-12, timeout=3)
        return remaining

    def _cancel_all(self):
        for dex in ("", "xyz"):
            orders = self.broker.info.open_orders(self.broker.account_address, dex=dex)
            if not isinstance(orders, list):
                raise ScenarioError(f"invalid_open_orders:{dex or 'standard'}")
            for row in orders:
                coin = row.get("coin") if isinstance(row, dict) else None
                oid = row.get("oid") if isinstance(row, dict) else None
                if coin and oid:
                    result = self.broker.cancel_by_oid(coin, int(oid))
                    if not result.ok:
                        raise ScenarioError(f"cleanup_cancel_failed:{result.error_code}")

    def _place_resting(self, coin: str, price_multiplier: float, label: str):
        bid, _ = self._top(coin)
        limit = bid * float(price_multiplier)
        market = self.broker.market_spec(coin)
        minimum_size = 10 ** (-market.sz_decimals)
        intent = OrderIntent(
            coin=coin,
            is_buy=True,
            size=max(minimum_size, self.notional / limit),
            limit_px=limit,
            reduce_only=False,
            cloid=deterministic_cloid("testnet-scenarios", self.session, coin, label),
        )
        order = self.broker.prepare_order(intent)
        response = self.broker.exchange.order(
            order.coin,
            order.is_buy,
            order.size,
            order.limit_px,
            order_type={"limit": {"tif": "Gtc"}},
            reduce_only=False,
            cloid=ClientOrderId(order.cloid),
        )
        result = normalize_order_response(response, requested_size=order.size)
        if result.outcome is not OrderOutcome.RESTING or not result.oid:
            raise ScenarioError(f"{label}_not_resting:{result.outcome.value}:{result.error_code}")
        return order, result

    def _verify_rest_apis(self):
        standard_mids = self.broker.all_mids("")
        xyz_mids = self.broker.all_mids("xyz")
        standard_contexts = self.broker.market_contexts("")
        xyz_contexts = self.broker.market_contexts("xyz")
        recent = self.broker.recent_fills()
        by_time = self.broker.fills_by_time(max(0, self.started_ms - 1_000))
        historical = self.broker.historical_orders()
        fill_results = [row for row in self.results if row.get("oid") and row.get("cloid")]
        if "BTC" not in standard_mids or "xyz:XYZ100" not in xyz_mids:
            raise ScenarioError("all_mids_market_missing")
        if not all(isinstance(rows, list) and len(rows) == 2 for rows in (standard_contexts, xyz_contexts)):
            raise ScenarioError("invalid_market_contexts")
        if not fill_results or not recent or not by_time or not historical:
            raise ScenarioError("missing_order_or_fill_history")
        expected_oids = {str(row["oid"]) for row in fill_results}
        recent_oids = {str(row.get("oid")) for row in recent if isinstance(row, dict)}
        timed_oids = {str(row.get("oid")) for row in by_time if isinstance(row, dict)}
        if not expected_oids.issubset(recent_oids) or not expected_oids.issubset(timed_oids):
            raise ScenarioError("scenario_fills_missing_from_info_api")
        last = fill_results[-1]
        if not isinstance(self.broker.order_status(last["cloid"]), dict):
            raise ScenarioError("cloid_order_status_missing")
        if not isinstance(self.broker.order_status_by_oid(last["oid"]), dict):
            raise ScenarioError("oid_order_status_missing")
        snapshot = self.broker.account_snapshot()
        if snapshot.abstraction != "unifiedAccount":
            raise ScenarioError("rest_snapshot_not_unified")
        self.results.append({
            "case": "rest_api_matrix",
            "recentFillCount": len(recent),
            "sessionFillCount": len(by_time),
            "historicalOrderCount": len(historical),
            "standardMidCount": len(standard_mids),
            "xyzMidCount": len(xyz_mids),
            "oidStatus": True,
            "cloidStatus": True,
        })

    def _verify_identity_and_baseline(self):
        identity = self.broker.identity_snapshot(self.agent_address)
        role = identity.agent_role if isinstance(identity.agent_role, dict) else {}
        data = role.get("data")
        owner = str(data.get("user") or "").lower() if isinstance(data, dict) else ""
        if role.get("role") != "agent" or owner != self.broker.account_address:
            raise ScenarioError("agent_owner_mismatch")
        snapshot = self.broker.account_snapshot()
        if snapshot.abstraction != "unifiedAccount":
            raise ScenarioError(f"unsupported_account_abstraction:{snapshot.abstraction}")
        if any(snapshot.open_orders.values()):
            raise ScenarioError("dirty_order_baseline")
        if any(abs(self._position(coin)) > 1e-12 for coin in ("BTC", "ETH")):
            raise ScenarioError("dirty_position_baseline")

    def _run_cases(self):
        self._verify_identity_and_baseline()
        xyz_bid, xyz_ask = self._top("xyz:XYZ100")
        self.results.append({"case": "xyz_l2_read", "coin": "xyz:XYZ100", "bid": xyz_bid, "ask": xyz_ask})

        for coin in ("BTC", "ETH"):
            result = self.broker.set_isolated_leverage(coin, self.leverage)
            if not result.ok:
                raise ScenarioError(f"set_leverage_failed:{coin}:{result.error_code}")
        self.results.append({"case": "isolated_leverage", "coins": ["BTC", "ETH"], "leverage": self.leverage})

        bid, _ = self._top("BTC")
        passive_limit = bid * 0.99
        canceled_ioc = self.broker.submit_ioc(OrderIntent(
            "BTC",
            True,
            self.notional / passive_limit,
            passive_limit,
            False,
            deterministic_cloid("testnet-scenarios", self.session, "BTC", "actual-ioc-cancel"),
        ))
        if canceled_ioc.outcome is not OrderOutcome.CANCELED or canceled_ioc.error_code != "ioc_cancel":
            raise ScenarioError(f"actual_ioc_cancel_missing:{canceled_ioc.outcome.value}:{canceled_ioc.error_code}")
        self.results.append({"case": "actual_ioc_cancel", "coin": "BTC", "errorCode": canceled_ioc.error_code})

        self._open_notional("BTC", True, self.notional, "long_open")
        first_long = self._wait_position("BTC", lambda value: value > 0)
        self._open_notional("BTC", True, self.notional, "long_add")
        added_long = self._wait_position("BTC", lambda value: value > first_long)
        if not added_long > first_long > 0:
            raise ScenarioError("long_add_position_not_observed")
        self._send(
            "BTC", False, min(first_long, added_long / 2), reduce_only=True, label="long_partial_reduce"
        )
        reduced_long = self._wait_position("BTC", lambda value: 0 < value < added_long)
        if not 0 < reduced_long < added_long:
            raise ScenarioError("long_partial_reduce_not_observed")
        self._send("BTC", False, reduced_long, reduce_only=True, label="long_full_close")
        if abs(self._wait_position("BTC", lambda value: abs(value) < 1e-12)) >= 1e-12:
            raise ScenarioError("long_close_not_flat")

        self._open_notional("BTC", False, self.notional * 2, "short_open")
        first_short = self._wait_position("BTC", lambda value: value < 0)
        if first_short >= 0:
            raise ScenarioError("short_not_observed")
        self._send("BTC", True, abs(first_short) / 2, reduce_only=True, label="short_partial_reduce")
        reduced_short = self._wait_position("BTC", lambda value: first_short < value < 0)
        if not first_short < reduced_short < 0:
            raise ScenarioError("short_partial_reduce_not_observed")
        self._send("BTC", True, abs(reduced_short), reduce_only=True, label="short_full_close")
        if abs(self._wait_position("BTC", lambda value: abs(value) < 1e-12)) >= 1e-12:
            raise ScenarioError("short_close_not_flat")

        self._open_notional("BTC", True, self.notional, "flip_old_long_open")
        flip_long = self._wait_position("BTC", lambda value: value > 0)
        self._send("BTC", False, flip_long, reduce_only=True, label="flip_old_long_close")
        if abs(self._wait_position("BTC", lambda value: abs(value) < 1e-12)) >= 1e-12:
            raise ScenarioError("flip_boundary_not_flat")
        self._open_notional("BTC", False, self.notional, "flip_new_short_open")
        flip_short = self._wait_position("BTC", lambda value: value < 0)
        self._send("BTC", True, abs(flip_short), reduce_only=True, label="flip_new_short_close")
        if abs(self._wait_position("BTC", lambda value: abs(value) < 1e-12)) >= 1e-12:
            raise ScenarioError("flip_final_not_flat")

        self._open_notional("ETH", True, self.notional, "eth_long_open")
        eth_position = self._wait_position("ETH", lambda value: value > 0)
        self._send("ETH", False, eth_position, reduce_only=True, label="eth_long_close")
        if abs(self._wait_position("ETH", lambda value: abs(value) < 1e-12)) >= 1e-12:
            raise ScenarioError("eth_close_not_flat")

        order, resting = self._place_resting("BTC", 0.99, "rest_cancel_cloid")
        status = self.broker.order_status(order.cloid)
        frontend = self.broker.info.frontend_open_orders(self.broker.account_address)
        frontend_oids = {int(row.get("oid")) for row in frontend if isinstance(row, dict) and row.get("oid")}
        if resting.oid not in frontend_oids:
            raise ScenarioError("frontend_open_orders_missing_resting_order")
        canceled = self.broker.cancel_by_cloid("BTC", order.cloid)
        if not canceled.ok:
            raise ScenarioError(f"cancel_by_cloid_failed:{canceled.error_code}")
        self.results.append({
            "case": "resting_cancel_by_cloid",
            "coin": "BTC",
            "oid": resting.oid,
            "statusFound": isinstance(status, dict),
            "frontendOrderFound": True,
        })

        _, resting = self._place_resting("BTC", 0.985, "rest_cancel_oid")
        status = self.broker.order_status_by_oid(resting.oid)
        canceled = self.broker.cancel_by_oid("BTC", resting.oid)
        if not canceled.ok:
            raise ScenarioError(f"cancel_by_oid_failed:{canceled.error_code}")
        self.results.append({
            "case": "resting_cancel_by_oid",
            "coin": "BTC",
            "oid": resting.oid,
            "statusFound": isinstance(status, dict),
        })

        leverage = self.broker.set_cross_leverage("xyz:XYZ100", self.leverage)
        if not leverage.ok:
            raise ScenarioError(f"hip3_cross_leverage_failed:{leverage.error_code}")
        _, hip3_ask = self._top("xyz:XYZ100")
        hip3_market = self.broker.market_spec("xyz:XYZ100")
        oracle_reject = self.broker.submit_ioc(OrderIntent(
            "xyz:XYZ100",
            True,
            max(10 ** (-hip3_market.sz_decimals), self.notional / (hip3_ask * 1.01)),
            hip3_ask * 1.01,
            False,
            deterministic_cloid("testnet-scenarios", self.session, "xyz:XYZ100", "oracle-reject"),
        ))
        if oracle_reject.outcome is not OrderOutcome.REJECTED or oracle_reject.error_code != "oracle_reject":
            raise ScenarioError(f"hip3_oracle_reject_missing:{oracle_reject.outcome.value}:{oracle_reject.error_code}")
        self.results.append({
            "case": "actual_hip3_oracle_reject",
            "coin": "xyz:XYZ100",
            "errorCode": oracle_reject.error_code,
        })
        order, resting = self._place_resting("xyz:XYZ100", 0.99, "hip3_rest_cancel")
        status = self.broker.order_status(order.cloid)
        canceled = self.broker.cancel_by_oid("xyz:XYZ100", resting.oid)
        if not canceled.ok:
            raise ScenarioError(f"hip3_cancel_failed:{canceled.error_code}")
        self.results.append({
            "case": "hip3_signed_resting_cancel",
            "coin": "xyz:XYZ100",
            "oid": resting.oid,
            "statusFound": isinstance(status, dict),
        })

        self._verify_rest_apis()

    def run(self) -> Dict[str, Any]:
        failure = None
        cleanup_errors = []
        try:
            self._run_cases()
        except ScenarioError as exc:
            failure = str(exc)
        except Exception as exc:  # noqa: BLE001 - never expose arbitrary SDK/secret-derived messages
            failure = f"unexpected:{type(exc).__name__}"
        finally:
            try:
                self._cancel_all()
            except Exception as exc:  # noqa: BLE001 - sanitized cleanup audit
                cleanup_errors.append(f"cancel:{type(exc).__name__}")
            final_positions = {}
            for coin in ("BTC", "ETH"):
                try:
                    final_positions[coin] = self._flatten(coin)
                except Exception as exc:  # noqa: BLE001 - sanitized cleanup audit
                    final_positions[coin] = None
                    cleanup_errors.append(f"flatten_{coin}:{type(exc).__name__}")
            try:
                time.sleep(0.5)
                final_orders = sum(
                    len(self.broker.info.open_orders(self.broker.account_address, dex=dex))
                    for dex in ("", "xyz")
                )
            except Exception as exc:  # noqa: BLE001 - sanitized cleanup audit
                final_orders = None
                cleanup_errors.append(f"orders:{type(exc).__name__}")

        clean = (
            all(value is not None and abs(value) < 1e-12 for value in final_positions.values())
            and final_orders == 0
        )
        return {
            "ok": failure is None and not cleanup_errors and clean,
            "scenarioCount": len(self.results),
            "results": self.results,
            "failure": failure,
            "cleanupErrors": cleanup_errors,
            "final": {**final_positions, "openOrders": final_orders},
        }
