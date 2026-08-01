"""Explicit Hyperliquid execution-venue verification commands.

``public-metadata`` and ``testnet-preflight`` are read-only.  ``testnet-roundtrip`` is intentionally Testnet-
only and opens then immediately closes one minimum-sized position, with a best-effort emergency flatten path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from hyper.execution.hyperliquid_broker import HyperliquidBroker
from hyper.execution.coordinator import SerializedExecutionCoordinator
from hyper.execution.orders import (
    ClientOrderId,
    OrderIntent,
    OrderOutcome,
    deterministic_cloid,
    normalize_order_response,
)
from hyper.execution.preflight import evaluate_account_preflight
from hyper.execution.sdk_clients import create_public_info_client, create_signed_testnet_clients
from hyper.execution.signal_bridge_verifier import MainnetSignalBridgeVerifier
from hyper.execution.testnet_verifier import TestnetScenarioRunner
from hyper.execution.venue import ExecutionNetwork
from hyper.execution.ws_verifier import TestnetWebsocketVerifier


_ZERO_ADDRESS = "0x" + "0" * 40


def _public_metadata(args) -> int:
    dexes = tuple(args.dex)
    info = create_public_info_client(args.network, supported_dexes=dexes, timeout=args.timeout)
    broker = HyperliquidBroker(
        args.network,
        _ZERO_ADDRESS,
        info_client=info,
        supported_dexes=dexes,
    )
    specs = broker.load_market_specs()
    missing = [coin for coin in args.require_coin if coin not in specs]
    payload = {
        "ok": not missing,
        "network": broker.venue.network.value,
        "apiUrl": broker.venue.api_url,
        "dexes": list(broker.supported_dexes),
        "marketCount": len(specs),
        "requiredCoins": list(args.require_coin),
        "missingCoins": missing,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if not missing else 2


def _agent_owner(agent_role):
    if not isinstance(agent_role, dict) or agent_role.get("role") != "agent":
        return None
    data = agent_role.get("data")
    return str(data.get("user") or "").lower() if isinstance(data, dict) else None


def _position_size(state, coin: str) -> float:
    rows = state.get("assetPositions") if isinstance(state, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("invalid_asset_positions")
    for row in rows:
        position = row.get("position") if isinstance(row, dict) else None
        if isinstance(position, dict) and position.get("coin") == coin:
            return float(position.get("szi") or 0)
    return 0.0


def _best_prices(book) -> tuple[float, float]:
    levels = book.get("levels") if isinstance(book, dict) else None
    if not isinstance(levels, list) or len(levels) != 2:
        raise RuntimeError("invalid_l2_levels")
    try:
        bid = float(levels[0][0]["px"])
        ask = float(levels[1][0]["px"])
    except (IndexError, KeyError, TypeError, ValueError):
        raise RuntimeError("missing_l2_top") from None
    if bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError("invalid_l2_top")
    return bid, ask


def _wait_for_position(info, account: str, coin: str, predicate, *, timeout: float = 10.0) -> float:
    deadline = time.monotonic() + timeout
    latest = 0.0
    dex = coin.split(":", 1)[0] if ":" in coin else ""
    while True:
        latest = _position_size(info.user_state(account, dex=dex), coin)
        if predicate(latest):
            return latest
        if time.monotonic() >= deadline:
            return latest
        time.sleep(0.25)


def _snapshot_payload(broker: HyperliquidBroker, agent_address: str) -> dict:
    identity = broker.identity_snapshot(agent_address)
    account = broker.account_snapshot()
    state = account.perp_states[""]
    margin = state.get("marginSummary") if isinstance(state, dict) else None
    abstraction = account.abstraction
    unified = abstraction == "unifiedAccount"
    balances = account.collateral_state.get("balances") if isinstance(account.collateral_state, dict) else None
    usdc = next(
        (row for row in (balances or []) if isinstance(row, dict) and row.get("coin") == "USDC"),
        None,
    )
    if unified and isinstance(usdc, dict):
        account_value = usdc.get("total")
        try:
            available = str(max(0.0, float(usdc.get("total") or 0) - float(usdc.get("hold") or 0)))
        except (TypeError, ValueError):
            available = None
    else:
        account_value = margin.get("accountValue") if isinstance(margin, dict) else None
        available = state.get("withdrawable") if isinstance(state, dict) else None
    position_rows = []
    position_counts = {}
    for dex, dex_state in account.perp_states.items():
        rows = dex_state.get("assetPositions") if isinstance(dex_state, dict) else None
        if not isinstance(rows, list):
            rows = []
        position_rows.extend(rows)
        position_counts[dex or "standard"] = len(rows)
    owner = _agent_owner(identity.agent_role)
    return {
        "network": account.network.value,
        "accountAddress": account.account_address,
        "agentAddress": identity.agent_address,
        "agentRole": identity.agent_role.get("role") if isinstance(identity.agent_role, dict) else None,
        "agentOwnerMatches": owner == account.account_address,
        "abstraction": abstraction,
        "unifiedAccount": unified,
        "accountValue": account_value,
        "withdrawable": available,
        "positionCount": len(position_rows),
        "positionCountByDex": position_counts,
        "openOrderCount": sum(len(rows) for rows in account.open_orders.values()),
        "frontendOpenOrderCount": sum(len(rows) for rows in account.frontend_open_orders.values()),
    }


def _signed_broker(args, *, dexes=("",)) -> tuple[HyperliquidBroker, str]:
    clients = create_signed_testnet_clients(
        args.account_address,
        args.agent_address,
        args.private_key_file,
        supported_dexes=dexes,
        timeout=args.timeout,
    )
    broker = HyperliquidBroker(
        ExecutionNetwork.TESTNET,
        args.account_address,
        info_client=clients.info,
        exchange_client=clients.exchange,
        supported_dexes=dexes,
    )
    return broker, clients.agent_address


def _testnet_preflight(args) -> int:
    broker, agent = _signed_broker(args, dexes=("", "xyz"))
    payload = _snapshot_payload(broker, agent)
    identity = broker.identity_snapshot(agent)
    snapshot = broker.account_snapshot()
    decision = evaluate_account_preflight(identity, snapshot)
    payload["ok"] = decision.ok
    payload["preflightCode"] = decision.code.value
    payload["availableCollateral"] = decision.available_collateral
    payload["cleanBaseline"] = decision.position_count == 0 and decision.open_order_count == 0
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["ok"] else 2


def _close_position(broker: HyperliquidBroker, coin: str, size: float, session: str, slippage_bps: int):
    bid, ask = _best_prices(broker.l2_book(coin))
    is_buy = size < 0
    reference = ask if is_buy else bid
    multiplier = 1 + slippage_bps / 10_000 if is_buy else 1 - slippage_bps / 10_000
    intent = OrderIntent(
        coin=coin,
        is_buy=is_buy,
        size=abs(size),
        limit_px=reference * multiplier,
        reduce_only=True,
        cloid=deterministic_cloid("testnet-roundtrip", session, coin, "close"),
    )
    return intent, broker.submit_ioc(intent)


def _testnet_roundtrip(args) -> int:
    dexes = ("", args.coin.split(":", 1)[0]) if ":" in args.coin else ("",)
    broker, agent = _signed_broker(args, dexes=dexes)
    before = _snapshot_payload(broker, agent)
    if not before["agentOwnerMatches"]:
        raise RuntimeError("agent_owner_mismatch")
    if before["positionCount"] != 0 or before["openOrderCount"] != 0:
        raise RuntimeError("testnet_account_not_clean")
    if float(before["withdrawable"] or 0) < float(args.min_withdrawable):
        raise RuntimeError("insufficient_testnet_withdrawable")

    session = str(time.time_ns())
    coin = str(args.coin)
    leverage = (
        broker.set_cross_leverage(coin, args.leverage)
        if args.margin_mode == "cross"
        else broker.set_isolated_leverage(coin, args.leverage)
    )
    if not leverage.ok:
        raise RuntimeError(f"set_leverage_failed:{leverage.error_code}")

    bid, ask = _best_prices(broker.l2_book(coin))
    open_limit = ask * (1 + args.slippage_bps / 10_000)
    open_intent = OrderIntent(
        coin=coin,
        is_buy=True,
        size=float(args.notional) / open_limit,
        limit_px=open_limit,
        reduce_only=False,
        cloid=deterministic_cloid("testnet-roundtrip", session, coin, "open"),
    )
    opened = None
    closed = None
    close_intent = None
    flatten_error = None
    try:
        opened = broker.submit_ioc(open_intent)
        if opened.outcome not in (OrderOutcome.FILLED, OrderOutcome.PARTIAL):
            raise RuntimeError(f"open_not_filled:{opened.outcome.value}:{opened.error_code}")
        position = _wait_for_position(
            broker.info,
            broker.account_address,
            coin,
            lambda value: abs(value) > 0,
        )
        if position <= 0:
            raise RuntimeError("long_position_not_observed")
        close_intent, closed = _close_position(broker, coin, position, session, args.slippage_bps)
        if closed.outcome not in (OrderOutcome.FILLED, OrderOutcome.PARTIAL):
            raise RuntimeError(f"close_not_filled:{closed.outcome.value}:{closed.error_code}")
    finally:
        remaining = _wait_for_position(
            broker.info,
            broker.account_address,
            coin,
            lambda value: abs(value) < 1e-12,
            timeout=3.0,
        )
        if abs(remaining) >= 1e-12:
            try:
                emergency_session = session + "-emergency"
                _, emergency = _close_position(
                    broker,
                    coin,
                    remaining,
                    emergency_session,
                    max(args.slippage_bps, 200),
                )
                if emergency.outcome not in (OrderOutcome.FILLED, OrderOutcome.PARTIAL):
                    flatten_error = f"emergency_close_not_filled:{emergency.outcome.value}:{emergency.error_code}"
            except Exception as exc:  # noqa: BLE001 - report only sanitized exception type
                flatten_error = f"emergency_close_transport:{type(exc).__name__}"

    final_position = _wait_for_position(
        broker.info,
        broker.account_address,
        coin,
        lambda value: abs(value) < 1e-12,
    )
    after = _snapshot_payload(broker, agent)
    open_status = broker.order_status(open_intent.cloid)
    close_status = broker.order_status(close_intent.cloid) if close_intent is not None else None
    clean = abs(final_position) < 1e-12 and after["openOrderCount"] == 0
    payload = {
        "ok": bool(clean and flatten_error is None),
        "network": "testnet",
        "coin": coin,
        "leverage": args.leverage,
        "marginMode": args.margin_mode,
        "requestedNotional": float(args.notional),
        "open": {
            "outcome": opened.outcome.value if opened else None,
            "oid": opened.oid if opened else None,
            "filledSize": opened.filled_size if opened else 0,
            "averagePx": opened.average_px if opened else None,
            "statusFound": isinstance(open_status, dict),
        },
        "close": {
            "outcome": closed.outcome.value if closed else None,
            "oid": closed.oid if closed else None,
            "filledSize": closed.filled_size if closed else 0,
            "averagePx": closed.average_px if closed else None,
            "statusFound": isinstance(close_status, dict),
        },
        "finalPositionSize": final_position,
        "finalOpenOrderCount": after["openOrderCount"],
        "flattenError": flatten_error,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["ok"] else 3


def _testnet_scenarios(args) -> int:
    broker, agent = _signed_broker(args, dexes=("", "xyz"))
    runner = TestnetScenarioRunner(
        broker,
        agent,
        notional=args.notional,
        leverage=args.leverage,
        slippage_bps=args.slippage_bps,
    )
    payload = runner.run()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["ok"] else 3


def _testnet_websocket(args) -> int:
    broker, _ = _signed_broker(args, dexes=("",))
    verifier = TestnetWebsocketVerifier(
        broker,
        coin=args.coin,
        notional=args.notional,
        slippage_bps=args.slippage_bps,
        timeout=args.event_timeout,
    )
    payload = asyncio.run(verifier.run())
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["ok"] else 3


def _testnet_idempotency(args) -> int:
    broker, _ = _signed_broker(args, dexes=("",))
    failure = None
    cleanup_error = None
    payload = {}
    session = str(time.time_ns())
    intent = None
    try:
        snapshot = _snapshot_payload(broker, args.agent_address)
        if not snapshot["unifiedAccount"] or snapshot["positionCount"] or snapshot["openOrderCount"]:
            raise RuntimeError("idempotency_dirty_or_unsupported_baseline")
        bid, ask = _best_prices(broker.l2_book(args.coin))
        limit = ask * (1 + args.slippage_bps / 10_000)
        intent = OrderIntent(
            args.coin,
            True,
            args.notional / limit,
            limit,
            False,
            deterministic_cloid("testnet-idempotency", session, args.coin, "open"),
        )
        first_coordinator = SerializedExecutionCoordinator(broker)
        first = first_coordinator.submit_once(intent)
        same_process = first_coordinator.submit_once(intent)
        restarted = SerializedExecutionCoordinator(broker).submit_once(intent)
        if not first.submitted or first.result is None:
            raise RuntimeError("idempotency_first_submit_missing")
        if same_process is not first or restarted.submitted or restarted.recovered_status is None:
            raise RuntimeError("idempotency_duplicate_not_suppressed")
        position = _wait_for_position(
            broker.info, broker.account_address, args.coin, lambda value: value > 0,
        )
        if position <= 0 or position > first.result.filled_size + 1e-12:
            raise RuntimeError("idempotency_position_mismatch")
        close_intent, closed = _close_position(broker, args.coin, position, session, args.slippage_bps)
        final_position = _wait_for_position(
            broker.info, broker.account_address, args.coin, lambda value: abs(value) < 1e-12,
        )
        payload = {
            "firstSubmitted": first.submitted,
            "sameProcessCacheHit": same_process is first,
            "restartRecovered": not restarted.submitted and restarted.recovered_status is not None,
            "openOid": first.result.oid,
            "closeOid": closed.oid,
            "closeCloid": close_intent.cloid,
            "finalPosition": final_position,
        }
    except Exception as exc:  # noqa: BLE001 - sanitized verifier failure
        text = str(exc)
        failure = text if text.startswith("idempotency_") else f"unexpected:{type(exc).__name__}"
    finally:
        try:
            remaining = _wait_for_position(
                broker.info, broker.account_address, args.coin, lambda value: abs(value) < 1e-12, timeout=1,
            )
            if abs(remaining) >= 1e-12:
                _close_position(broker, args.coin, remaining, session + "-emergency", max(args.slippage_bps, 250))
            final_position = _wait_for_position(
                broker.info, broker.account_address, args.coin, lambda value: abs(value) < 1e-12,
            )
            final_orders = len(broker.info.open_orders(broker.account_address))
        except Exception as exc:  # noqa: BLE001 - sanitized cleanup audit
            final_position = None
            final_orders = None
            cleanup_error = type(exc).__name__
    clean = final_position is not None and abs(final_position) < 1e-12 and final_orders == 0
    payload.update({
        "ok": failure is None and cleanup_error is None and clean,
        "failure": failure,
        "cleanupError": cleanup_error,
        "finalPosition": final_position,
        "finalOpenOrders": final_orders,
    })
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["ok"] else 3


def _testnet_reconcile(args) -> int:
    broker, _ = _signed_broker(args, dexes=("",))
    active_broker = broker
    session = str(time.time_ns())
    failure = None
    cleanup_error = None
    payload = {}
    try:
        before = _snapshot_payload(broker, args.agent_address)
        if not before["unifiedAccount"] or before["positionCount"] or before["openOrderCount"]:
            raise RuntimeError("reconcile_dirty_or_unsupported_baseline")
        bid, ask = _best_prices(broker.l2_book(args.coin))
        open_limit = ask * (1 + args.slippage_bps / 10_000)
        open_intent = OrderIntent(
            args.coin,
            True,
            args.notional / open_limit,
            open_limit,
            False,
            deterministic_cloid("testnet-reconcile", session, args.coin, "position"),
        )
        opened = SerializedExecutionCoordinator(broker).submit_once(open_intent)
        if opened.result is None or opened.result.outcome not in (OrderOutcome.FILLED, OrderOutcome.PARTIAL):
            raise RuntimeError("reconcile_open_not_filled")
        position = _wait_for_position(broker.info, broker.account_address, args.coin, lambda value: value > 0)
        rest_intent = OrderIntent(
            args.coin,
            True,
            args.notional / (bid * 0.98),
            bid * 0.98,
            False,
            deterministic_cloid("testnet-reconcile", session, args.coin, "resting"),
        )
        rest_order = broker.prepare_order(rest_intent)
        resting = normalize_order_response(
            broker.exchange.order(
                rest_order.coin,
                rest_order.is_buy,
                rest_order.size,
                rest_order.limit_px,
                order_type={"limit": {"tif": "Gtc"}},
                reduce_only=False,
                cloid=ClientOrderId(rest_order.cloid),
            ),
            requested_size=rest_order.size,
        )
        if resting.outcome is not OrderOutcome.RESTING or not resting.oid:
            raise RuntimeError("reconcile_resting_order_missing")

        # Reconstruct every client and broker object, as a restarted execution process would.
        restarted, _ = _signed_broker(args, dexes=("",))
        active_broker = restarted
        recovered_position = _wait_for_position(
            restarted.info, restarted.account_address, args.coin, lambda value: value > 0,
        )
        recovered_orders = restarted.info.open_orders(restarted.account_address)
        recovered_oids = {int(row.get("oid")) for row in recovered_orders if row.get("oid")}
        fills = restarted.recent_fills()
        fill_oids = {int(row.get("oid")) for row in fills if isinstance(row, dict) and row.get("oid")}
        position_status = restarted.order_status(open_intent.cloid)
        resting_status = restarted.order_status_by_oid(resting.oid)
        if recovered_position <= 0 or resting.oid not in recovered_oids:
            raise RuntimeError("reconcile_exchange_state_not_recovered")
        if opened.result.oid not in fill_oids:
            raise RuntimeError("reconcile_fill_not_recovered")
        if not isinstance(position_status, dict) or not isinstance(resting_status, dict):
            raise RuntimeError("reconcile_order_status_not_recovered")

        canceled = restarted.cancel_by_oid(args.coin, resting.oid)
        if not canceled.ok:
            raise RuntimeError("reconcile_cancel_failed")
        _, closed = _close_position(restarted, args.coin, recovered_position, session, args.slippage_bps)
        final_position = _wait_for_position(
            restarted.info, restarted.account_address, args.coin, lambda value: abs(value) < 1e-12,
        )
        payload = {
            "recreatedClients": True,
            "recoveredPosition": recovered_position,
            "recoveredRestingOrder": resting.oid in recovered_oids,
            "recoveredFill": opened.result.oid in fill_oids,
            "cloidStatusFound": isinstance(position_status, dict),
            "oidStatusFound": isinstance(resting_status, dict),
            "cancelOk": canceled.ok,
            "closeOid": closed.oid,
            "finalPosition": final_position,
        }
    except Exception as exc:  # noqa: BLE001 - sanitized verifier failure
        text = str(exc)
        failure = text if text.startswith("reconcile_") else f"unexpected:{type(exc).__name__}"
    finally:
        try:
            for row in active_broker.info.open_orders(active_broker.account_address):
                if row.get("oid"):
                    active_broker.cancel_by_oid(row.get("coin"), int(row["oid"]))
            remaining = _wait_for_position(
                active_broker.info,
                active_broker.account_address,
                args.coin,
                lambda value: abs(value) < 1e-12,
                timeout=1,
            )
            if abs(remaining) >= 1e-12:
                _close_position(
                    active_broker,
                    args.coin,
                    remaining,
                    session + "-emergency",
                    max(args.slippage_bps, 250),
                )
            final_position = _wait_for_position(
                active_broker.info,
                active_broker.account_address,
                args.coin,
                lambda value: abs(value) < 1e-12,
            )
            final_orders = len(active_broker.info.open_orders(active_broker.account_address))
        except Exception as exc:  # noqa: BLE001 - sanitized cleanup audit
            final_position = None
            final_orders = None
            cleanup_error = type(exc).__name__
    clean = final_position is not None and abs(final_position) < 1e-12 and final_orders == 0
    payload.update({
        "ok": failure is None and cleanup_error is None and clean,
        "failure": failure,
        "cleanupError": cleanup_error,
        "finalPosition": final_position,
        "finalOpenOrders": final_orders,
    })
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["ok"] else 3


def _testnet_signal_bridge(args) -> int:
    from hyper.market import rest

    testnet, _ = _signed_broker(args, dexes=("",))
    mainnet_info = create_public_info_client(ExecutionNetwork.MAINNET, supported_dexes=("",), timeout=args.timeout)
    mainnet = HyperliquidBroker(
        ExecutionNetwork.MAINNET,
        _ZERO_ADDRESS,
        info_client=mainnet_info,
        supported_dexes=("",),
    )
    verifier = MainnetSignalBridgeVerifier(
        mainnet,
        testnet,
        leaderboard_rows=rest.get_leaderboard(),
        fill_fetcher=lambda address, since: rest.user_fills_by_time(address, since, aggregate=True),
        notional=args.notional,
        lookback_days=args.lookback_days,
        max_targets=args.max_targets,
        slippage_bps=args.slippage_bps,
    )
    payload = verifier.run()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["ok"] else 3


def _testnet_all(args) -> int:
    results = []
    checks = (
        ("preflight_before", _testnet_preflight),
        ("rest_and_signed_scenarios", _testnet_scenarios),
        ("websocket", _testnet_websocket),
        ("idempotency", _testnet_idempotency),
        ("restart_reconcile", _testnet_reconcile),
        ("mainnet_signal_bridge", _testnet_signal_bridge),
        ("preflight_after", _testnet_preflight),
    )
    for name, run in checks:
        try:
            code = int(run(args))
            results.append({"check": name, "ok": code == 0, "exitCode": code})
        except Exception as exc:  # noqa: BLE001 - continue through final cleanup preflight
            results.append({"check": name, "ok": False, "errorType": type(exc).__name__})
    payload = {"ok": all(row["ok"] for row in results), "checks": results}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["ok"] else 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hyperliquid execution API verification")
    subparsers = parser.add_subparsers(dest="command", required=True)
    public = subparsers.add_parser("public-metadata", help="verify public perp metadata without a wallet")
    public.add_argument(
        "--network",
        choices=[network.value for network in ExecutionNetwork],
        default=ExecutionNetwork.TESTNET.value,
    )
    public.add_argument("--dex", action="append", default=None, help="perp DEX; repeat for multiple DEXes")
    public.add_argument("--require-coin", action="append", default=[])
    public.add_argument("--timeout", type=float, default=10.0)
    public.set_defaults(run=_public_metadata)

    def add_signed_arguments(command):
        command.add_argument("--account-address", required=True)
        command.add_argument("--agent-address", required=True)
        command.add_argument("--private-key-file", required=True)
        command.add_argument("--timeout", type=float, default=10.0)

    preflight = subparsers.add_parser("testnet-preflight", help="read-only Agent/account validation")
    add_signed_arguments(preflight)
    preflight.set_defaults(run=_testnet_preflight)

    roundtrip = subparsers.add_parser(
        "testnet-roundtrip",
        help="open and immediately reduce-only close one small Testnet position",
    )
    add_signed_arguments(roundtrip)
    roundtrip.add_argument("--coin", default="BTC")
    roundtrip.add_argument("--notional", type=float, default=15.0)
    roundtrip.add_argument("--leverage", type=int, default=2)
    roundtrip.add_argument("--margin-mode", choices=["isolated", "cross"], default="isolated")
    roundtrip.add_argument("--slippage-bps", type=int, default=100)
    roundtrip.add_argument("--min-withdrawable", type=float, default=20.0)
    roundtrip.set_defaults(run=_testnet_roundtrip)

    scenarios = subparsers.add_parser(
        "testnet-scenarios",
        help="run the bounded long/add/reduce/short/flip/cancel Testnet suite",
    )
    add_signed_arguments(scenarios)
    scenarios.add_argument("--notional", type=float, default=16.0)
    scenarios.add_argument("--leverage", type=int, default=2)
    scenarios.add_argument("--slippage-bps", type=int, default=100)
    scenarios.set_defaults(run=_testnet_scenarios)

    websocket = subparsers.add_parser(
        "testnet-websocket",
        help="verify public and user Testnet streams around a real roundtrip",
    )
    add_signed_arguments(websocket)
    websocket.add_argument("--coin", default="BTC")
    websocket.add_argument("--notional", type=float, default=15.0)
    websocket.add_argument("--slippage-bps", type=int, default=100)
    websocket.add_argument("--event-timeout", type=float, default=20.0)
    websocket.set_defaults(run=_testnet_websocket)

    idempotency = subparsers.add_parser(
        "testnet-idempotency",
        help="prove local duplicate suppression and restart CLOID recovery",
    )
    add_signed_arguments(idempotency)
    idempotency.add_argument("--coin", default="BTC")
    idempotency.add_argument("--notional", type=float, default=15.0)
    idempotency.add_argument("--slippage-bps", type=int, default=100)
    idempotency.set_defaults(run=_testnet_idempotency)

    reconcile = subparsers.add_parser(
        "testnet-reconcile",
        help="recreate clients and recover exchange position/order/fill state before cleanup",
    )
    add_signed_arguments(reconcile)
    reconcile.add_argument("--coin", default="BTC")
    reconcile.add_argument("--notional", type=float, default=15.0)
    reconcile.add_argument("--slippage-bps", type=int, default=100)
    reconcile.set_defaults(run=_testnet_reconcile)

    bridge = subparsers.add_parser(
        "testnet-signal-bridge",
        help="map a real Mainnet leaderboard open fill to one bounded Testnet roundtrip",
    )
    add_signed_arguments(bridge)
    bridge.add_argument("--notional", type=float, default=15.0)
    bridge.add_argument("--lookback-days", type=int, default=7)
    bridge.add_argument("--max-targets", type=int, default=8)
    bridge.add_argument("--slippage-bps", type=int, default=100)
    bridge.set_defaults(run=_testnet_signal_bridge)

    all_checks = subparsers.add_parser(
        "testnet-all",
        help="run every bounded REST, signed, WebSocket, idempotency, recovery, and signal bridge verifier",
    )
    add_signed_arguments(all_checks)
    all_checks.add_argument("--coin", default="BTC")
    all_checks.add_argument("--notional", type=float, default=15.0)
    all_checks.add_argument("--leverage", type=int, default=2)
    all_checks.add_argument("--slippage-bps", type=int, default=100)
    all_checks.add_argument("--event-timeout", type=float, default=25.0)
    all_checks.add_argument("--lookback-days", type=int, default=7)
    all_checks.add_argument("--max-targets", type=int, default=8)
    all_checks.set_defaults(run=_testnet_all)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "dex", None) is None and args.command == "public-metadata":
        args.dex = ["", "xyz"]
    if args.command == "public-metadata" and not args.require_coin:
        args.require_coin = ["BTC"]
    return int(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
