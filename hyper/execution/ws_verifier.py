"""Bounded Testnet WebSocket verification with real order/fill events and guaranteed flattening."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Iterable, Optional, Set

import websockets

from .hyperliquid_broker import HyperliquidBroker
from .orders import OrderIntent, OrderOutcome, deterministic_cloid
from .venue import ExecutionNetwork


def _contains_oid(value: Any, oid: int) -> bool:
    if isinstance(value, dict):
        if str(value.get("oid")) == str(oid):
            return True
        return any(_contains_oid(item, oid) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_oid(item, oid) for item in value)
    return False


class TestnetWebsocketVerifier:
    def __init__(
        self,
        broker: HyperliquidBroker,
        *,
        coin: str = "BTC",
        notional: float = 15.0,
        slippage_bps: int = 100,
        timeout: float = 20.0,
    ):
        if broker.venue.network is not ExecutionNetwork.TESTNET:
            raise ValueError("websocket_verifier_requires_testnet")
        if notional < 15:
            raise ValueError("websocket_verifier_notional_below_safety_floor")
        self.broker = broker
        self.coin = str(coin)
        self.notional = float(notional)
        self.slippage_bps = int(slippage_bps)
        self.timeout = float(timeout)
        self.session = str(time.time_ns())

    def _position(self) -> float:
        dex = self.coin.split(":", 1)[0] if ":" in self.coin else ""
        state = self.broker.info.user_state(self.broker.account_address, dex=dex)
        for row in state.get("assetPositions", []) if isinstance(state, dict) else []:
            position = row.get("position") if isinstance(row, dict) else None
            if isinstance(position, dict) and position.get("coin") == self.coin:
                return float(position.get("szi") or 0)
        return 0.0

    def _top(self) -> tuple[float, float]:
        levels = self.broker.l2_book(self.coin).get("levels")
        try:
            return float(levels[0][0]["px"]), float(levels[1][0]["px"])
        except (IndexError, KeyError, TypeError, ValueError):
            raise RuntimeError("websocket_verifier_missing_l2") from None

    def _open(self):
        _, ask = self._top()
        limit = ask * (1 + self.slippage_bps / 10_000)
        intent = OrderIntent(
            self.coin,
            True,
            self.notional / limit,
            limit,
            False,
            deterministic_cloid("testnet-websocket", self.session, self.coin, "open"),
        )
        result = self.broker.submit_ioc(intent)
        if result.outcome not in (OrderOutcome.FILLED, OrderOutcome.PARTIAL) or not result.oid:
            raise RuntimeError(f"websocket_open_not_filled:{result.outcome.value}:{result.error_code}")
        return result

    def _close(self, position: Optional[float] = None, *, emergency: bool = False):
        size = self._position() if position is None else float(position)
        if abs(size) < 1e-12:
            return None
        bid, ask = self._top()
        is_buy = size < 0
        bps = max(self.slippage_bps, 250) if emergency else self.slippage_bps
        reference = ask if is_buy else bid
        limit = reference * (1 + bps / 10_000 if is_buy else 1 - bps / 10_000)
        intent = OrderIntent(
            self.coin,
            is_buy,
            abs(size),
            limit,
            True,
            deterministic_cloid(
                "testnet-websocket", self.session, self.coin, "emergency-close" if emergency else "close"
            ),
        )
        result = self.broker.submit_ioc(intent)
        if result.outcome not in (OrderOutcome.FILLED, OrderOutcome.PARTIAL) or not result.oid:
            raise RuntimeError(f"websocket_close_not_filled:{result.outcome.value}:{result.error_code}")
        return result

    async def _collect(
        self,
        conn,
        *,
        required_channels: Iterable[str] = (),
        required_acks: Iterable[str] = (),
        required_oid: Optional[int] = None,
    ) -> Dict[str, Any]:
        channels: Set[str] = set()
        acks: Set[str] = set()
        oid_channels: Set[str] = set()
        deadline = asyncio.get_running_loop().time() + self.timeout
        while True:
            if (
                set(required_channels).issubset(channels)
                and set(required_acks).issubset(acks)
                and (required_oid is None or {"userFills", "orderUpdates"}.issubset(oid_channels))
            ):
                return {"channels": sorted(channels), "acks": sorted(acks), "oidChannels": sorted(oid_channels)}
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError("websocket_event_timeout")
            raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            message = json.loads(raw)
            if not isinstance(message, dict):
                continue
            channel = str(message.get("channel") or "")
            if channel == "subscriptionResponse":
                data = message.get("data")
                subscription = data.get("subscription") if isinstance(data, dict) else None
                sub_type = subscription.get("type") if isinstance(subscription, dict) else None
                if sub_type:
                    acks.add(str(sub_type))
                continue
            if channel:
                channels.add(channel)
            if required_oid is not None and channel in {"userFills", "orderUpdates"}:
                if _contains_oid(message.get("data"), required_oid):
                    oid_channels.add(channel)

    async def run(self) -> Dict[str, Any]:
        failure = None
        cleanup_error = None
        report: Dict[str, Any] = {}
        subscriptions = [
            {"type": "allMids", "dex": ""},
            {"type": "l2Book", "coin": self.coin},
            {"type": "bbo", "coin": self.coin},
            {"type": "userFills", "user": self.broker.account_address, "aggregateByTime": False},
            {"type": "orderUpdates", "user": self.broker.account_address},
        ]
        try:
            snapshot = self.broker.account_snapshot()
            if snapshot.abstraction != "unifiedAccount":
                raise RuntimeError(f"unsupported_account_abstraction:{snapshot.abstraction}")
            if any(snapshot.open_orders.values()) or abs(self._position()) >= 1e-12:
                raise RuntimeError("websocket_verifier_dirty_baseline")
            async with websockets.connect(
                self.broker.venue.ws_url,
                ping_interval=None,
                max_size=None,
                open_timeout=self.timeout,
            ) as conn:
                for subscription in subscriptions:
                    await conn.send(json.dumps({"method": "subscribe", "subscription": subscription}))
                initial = await self._collect(
                    conn,
                    required_channels={"allMids", "l2Book", "bbo", "userFills"},
                    required_acks={"allMids", "l2Book", "bbo", "userFills", "orderUpdates"},
                )
                opened = await asyncio.to_thread(self._open)
                open_events = await self._collect(conn, required_oid=opened.oid)
                closed = await asyncio.to_thread(self._close)
                if closed is None:
                    raise RuntimeError("websocket_close_position_missing")
                close_events = await self._collect(conn, required_oid=closed.oid)
                for subscription in subscriptions:
                    await conn.send(json.dumps({"method": "unsubscribe", "subscription": subscription}))
                report = {
                    "initial": initial,
                    "openOid": opened.oid,
                    "openEvents": open_events,
                    "closeOid": closed.oid,
                    "closeEvents": close_events,
                }
        except Exception as exc:  # noqa: BLE001 - only safe code/type crosses verification boundary
            text = str(exc)
            failure = text if text.startswith(("websocket_", "unsupported_")) else f"unexpected:{type(exc).__name__}"
        finally:
            try:
                remaining = await asyncio.to_thread(self._position)
                if abs(remaining) >= 1e-12:
                    await asyncio.to_thread(self._close, remaining, emergency=True)
                final_position = await asyncio.to_thread(self._position)
                final_orders = sum(
                    len(self.broker.info.open_orders(self.broker.account_address, dex=dex))
                    for dex in self.broker.supported_dexes
                )
            except Exception as exc:  # noqa: BLE001 - sanitized cleanup audit
                final_position = None
                final_orders = None
                cleanup_error = type(exc).__name__
        clean = final_position is not None and abs(final_position) < 1e-12 and final_orders == 0
        return {
            "ok": failure is None and cleanup_error is None and clean,
            **report,
            "failure": failure,
            "cleanupError": cleanup_error,
            "finalPosition": final_position,
            "finalOpenOrders": final_orders,
        }
