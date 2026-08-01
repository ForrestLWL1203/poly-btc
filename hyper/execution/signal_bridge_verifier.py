"""Mainnet public target-fill to Testnet signed-execution API verifier."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, Optional

from .coordinator import SerializedExecutionCoordinator
from .hyperliquid_broker import HyperliquidBroker
from .liquidity import assess_order_book
from .orders import OrderIntent, OrderOutcome, deterministic_cloid
from .venue import ExecutionNetwork


def _open_signal(fill: Any) -> bool:
    if not isinstance(fill, dict):
        return False
    direction = str(fill.get("dir") or "")
    side = str(fill.get("side") or "")
    coin = str(fill.get("coin") or "")
    return (
        direction in {"Open Long", "Open Short"}
        and side in {"A", "B"}
        and bool(coin)
        and ":" not in coin
        and not coin.startswith(("@", "#"))
    )


class MainnetSignalBridgeVerifier:
    def __init__(
        self,
        mainnet_broker: HyperliquidBroker,
        testnet_broker: HyperliquidBroker,
        *,
        leaderboard_rows: Iterable[dict],
        fill_fetcher: Callable[[str, int], Any],
        notional: float = 15.0,
        lookback_days: int = 7,
        max_targets: int = 8,
        slippage_bps: int = 100,
    ):
        if mainnet_broker.venue.network is not ExecutionNetwork.MAINNET:
            raise ValueError("signal_bridge_requires_mainnet_signal_broker")
        if testnet_broker.venue.network is not ExecutionNetwork.TESTNET:
            raise ValueError("signal_bridge_requires_testnet_execution_broker")
        self.mainnet = mainnet_broker
        self.testnet = testnet_broker
        self.leaderboard_rows = list(leaderboard_rows)
        self.fill_fetcher = fill_fetcher
        self.notional = float(notional)
        self.lookback_days = int(lookback_days)
        self.max_targets = int(max_targets)
        self.slippage_bps = int(slippage_bps)
        self.session = str(time.time_ns())

    def _position(self, coin: str) -> float:
        state = self.testnet.info.user_state(self.testnet.account_address)
        for row in state.get("assetPositions", []) if isinstance(state, dict) else []:
            position = row.get("position") if isinstance(row, dict) else None
            if isinstance(position, dict) and position.get("coin") == coin:
                return float(position.get("szi") or 0)
        return 0.0

    def _wait_position(self, coin: str, predicate, timeout: float = 10.0) -> float:
        deadline = time.monotonic() + timeout
        value = self._position(coin)
        while not predicate(value) and time.monotonic() < deadline:
            time.sleep(0.25)
            value = self._position(coin)
        return value

    def _discover(self) -> Optional[Dict[str, Any]]:
        main_specs = self.mainnet.load_market_specs()
        test_specs = self.testnet.load_market_specs()
        since = int(time.time() * 1000) - self.lookback_days * 86_400_000
        checked = 0
        for row in self.leaderboard_rows:
            if checked >= self.max_targets:
                break
            address = row.get("ethAddress") if isinstance(row, dict) else None
            if not address:
                continue
            checked += 1
            try:
                fills = self.fill_fetcher(str(address), since)
            except Exception:  # noqa: BLE001 - skip unavailable public target
                continue
            candidates = sorted(
                (fill for fill in (fills or []) if _open_signal(fill)),
                key=lambda fill: int(fill.get("time") or 0),
                reverse=True,
            )
            for fill in candidates:
                coin = str(fill["coin"])
                if coin not in main_specs or coin not in test_specs:
                    continue
                is_buy = fill.get("side") == "B"
                try:
                    book = self.testnet.l2_book(coin)
                except Exception:  # noqa: BLE001 - unavailable Testnet mapping
                    continue
                liquidity = assess_order_book(
                    book,
                    is_buy=is_buy,
                    planned_notional=self.notional,
                    max_spread_bps=20,
                    max_impact_bps=35,
                )
                if not liquidity.get("available") or liquidity.get("reason"):
                    continue
                return {
                    "target": str(address).lower(),
                    "fill": fill,
                    "coin": coin,
                    "isBuy": is_buy,
                    "liquidity": liquidity,
                    "checkedTargets": checked,
                    "mainSpec": main_specs[coin],
                    "testSpec": test_specs[coin],
                }
        return None

    def _close(self, coin: str, position: float, *, emergency: bool = False):
        levels = self.testnet.l2_book(coin)["levels"]
        bid, ask = float(levels[0][0]["px"]), float(levels[1][0]["px"])
        is_buy = position < 0
        bps = max(self.slippage_bps, 250) if emergency else self.slippage_bps
        reference = ask if is_buy else bid
        limit = reference * (1 + bps / 10_000 if is_buy else 1 - bps / 10_000)
        intent = OrderIntent(
            coin,
            is_buy,
            abs(position),
            limit,
            True,
            deterministic_cloid("mainnet-signal-testnet-execution", self.session, coin, "close"),
        )
        result = SerializedExecutionCoordinator(self.testnet).submit_once(intent)
        if result.result is None or result.result.outcome not in (OrderOutcome.FILLED, OrderOutcome.PARTIAL):
            raise RuntimeError("signal_bridge_close_not_filled")
        return result.result

    def run(self) -> Dict[str, Any]:
        failure = None
        cleanup_error = None
        report: Dict[str, Any] = {}
        selected = None
        try:
            snapshot = self.testnet.account_snapshot()
            if snapshot.abstraction != "unifiedAccount" or any(snapshot.open_orders.values()):
                raise RuntimeError("signal_bridge_dirty_or_unsupported_baseline")
            selected = self._discover()
            if selected is None:
                raise RuntimeError("signal_bridge_no_mappable_recent_open")
            coin = selected["coin"]
            if abs(self._position(coin)) >= 1e-12:
                raise RuntimeError("signal_bridge_existing_position")
            leverage = min(2, selected["testSpec"].max_leverage)
            action = self.testnet.set_isolated_leverage(coin, leverage)
            if not action.ok:
                raise RuntimeError("signal_bridge_leverage_failed")
            liquidity = selected["liquidity"]
            reference = float(liquidity["best_ask"] if selected["isBuy"] else liquidity["best_bid"])
            limit = reference * (
                1 + self.slippage_bps / 10_000 if selected["isBuy"] else 1 - self.slippage_bps / 10_000
            )
            source = selected["fill"]
            intent = OrderIntent(
                coin,
                selected["isBuy"],
                self.notional / limit,
                limit,
                False,
                deterministic_cloid(
                    "mainnet-signal-testnet-execution",
                    self.session,
                    selected["target"],
                    source.get("tid") or source.get("oid"),
                    coin,
                    source.get("side"),
                ),
            )
            opened = SerializedExecutionCoordinator(self.testnet).submit_once(intent)
            if opened.result is None or opened.result.outcome not in (OrderOutcome.FILLED, OrderOutcome.PARTIAL):
                raise RuntimeError("signal_bridge_open_not_filled")
            position = self._wait_position(
                coin,
                (lambda value: value > 0) if selected["isBuy"] else (lambda value: value < 0),
            )
            if abs(position) < 1e-12:
                raise RuntimeError("signal_bridge_position_not_observed")
            closed = self._close(coin, position)
            final_position = self._wait_position(coin, lambda value: abs(value) < 1e-12)
            fills = self.testnet.recent_fills()
            fill_oids = {str(fill.get("oid")) for fill in fills if isinstance(fill, dict)}
            if str(opened.result.oid) not in fill_oids or str(closed.oid) not in fill_oids:
                raise RuntimeError("signal_bridge_testnet_fills_missing")
            report = {
                "source": "mainnetLeaderboardUserFillsByTime",
                "sourceCoin": coin,
                "sourceDirection": source.get("dir"),
                "sourceAgeSeconds": max(0, round((int(time.time() * 1000) - int(source.get("time") or 0)) / 1000, 1)),
                "targetsChecked": selected["checkedTargets"],
                "mainnetAndTestnetMarket": True,
                "testnetSpreadBps": round(float(selected["liquidity"]["spread_bps"]), 3),
                "testnetImpactBps": round(float(selected["liquidity"]["impact_bps"]), 3),
                "openOid": opened.result.oid,
                "closeOid": closed.oid,
                "fillsRecovered": True,
                "finalPosition": final_position,
            }
        except Exception as exc:  # noqa: BLE001 - sanitized verifier failure
            text = str(exc)
            failure = text if text.startswith("signal_bridge_") else f"unexpected:{type(exc).__name__}"
        finally:
            try:
                coin = selected["coin"] if selected else None
                if coin:
                    remaining = self._position(coin)
                    if abs(remaining) >= 1e-12:
                        self._close(coin, remaining, emergency=True)
                    final_position = self._wait_position(coin, lambda value: abs(value) < 1e-12)
                else:
                    final_position = 0.0
                final_orders = len(self.testnet.info.open_orders(self.testnet.account_address))
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
