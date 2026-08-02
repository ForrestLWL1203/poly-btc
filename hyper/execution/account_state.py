"""Pure Hyperliquid account-state projection shared by verification and Live reconciliation."""

from __future__ import annotations

from typing import Any

from hyper.util import now_iso

from .hyperliquid_broker import AccountSnapshot


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def snapshot_positions(snapshot: AccountSnapshot) -> list[dict]:
    positions = []
    for dex, state in snapshot.perp_states.items():
        rows = state.get("assetPositions") if isinstance(state, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("invalid_exchange_position_state")
        for item in rows:
            position = item.get("position") if isinstance(item, dict) else None
            if not isinstance(position, dict):
                continue
            size = _float(position.get("szi"))
            if abs(size) <= 1e-12:
                continue
            leverage = position.get("leverage") if isinstance(position.get("leverage"), dict) else {}
            positions.append({
                "dex": str(dex or ""), "coin": str(position.get("coin") or ""),
                "signed_size": size, "entry_px": _float(position.get("entryPx"), None),
                "position_value": _float(position.get("positionValue"), None),
                "margin_used": _float(position.get("marginUsed"), None),
                "leverage_type": leverage.get("type"), "leverage_value": _float(leverage.get("value"), None),
                "unrealized_pnl": _float(position.get("unrealizedPnl"), None),
                "liquidation_px": _float(position.get("liquidationPx"), None),
            })
    return positions


def snapshot_orders(snapshot: AccountSnapshot) -> list[dict]:
    return [
        item for rows in snapshot.open_orders.values()
        for item in (rows or []) if isinstance(item, dict)
    ]


def snapshot_account_values(snapshot: AccountSnapshot, positions: list[dict]) -> tuple[float, float]:
    """Return Unified USDC collateral and conservative available-to-trade collateral."""
    balances = snapshot.collateral_state.get("balances") if isinstance(snapshot.collateral_state, dict) else None
    for item in balances or []:
        if isinstance(item, dict) and item.get("coin") == "USDC":
            total = max(0.0, _float(item.get("total")))
            isolated_margin = sum(
                max(0.0, _float(position.get("margin_used"))) for position in positions
                if str(position.get("leverage_type") or "").lower() == "isolated"
            )
            return total, max(0.0, total - _float(item.get("hold")) - isolated_margin)
    return 0.0, 0.0


def persist_account_preview(db, snapshot: AccountSnapshot) -> dict:
    """Persist a replaceable read-only snapshot without creating a Live session or ledger row."""
    positions = snapshot_positions(snapshot)
    orders = snapshot_orders(snapshot)
    collateral, available = snapshot_account_values(snapshot, positions)
    unrealized = sum(_float(position.get("unrealized_pnl")) for position in positions)
    margin_used = sum(max(0.0, _float(position.get("margin_used"))) for position in positions)
    equity = max(0.0, collateral + unrealized)
    observed_at = now_iso()
    db.execute(
        "INSERT INTO execution_account_preview "
        "(network,account_address,equity,available,margin_used,unrealized_pnl,position_count,"
        "open_order_count,observed_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(network) DO UPDATE SET account_address=excluded.account_address,"
        "equity=excluded.equity,available=excluded.available,margin_used=excluded.margin_used,"
        "unrealized_pnl=excluded.unrealized_pnl,position_count=excluded.position_count,"
        "open_order_count=excluded.open_order_count,observed_at=excluded.observed_at",
        (
            snapshot.network.value, snapshot.account_address, equity, available, margin_used,
            unrealized, len(positions), len(orders), observed_at,
        ),
    )
    return {
        "network": snapshot.network.value,
        "accountAddress": snapshot.account_address,
        "equity": equity,
        "available": available,
        "marginUsed": margin_used,
        "unrealizedPnl": unrealized,
        "positionCount": len(positions),
        "openOrderCount": len(orders),
        "observedAt": observed_at,
    }
