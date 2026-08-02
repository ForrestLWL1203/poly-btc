"""Pure account/Agent execution preflight checks shared by local verification and future Live startup."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .hyperliquid_broker import AccountSnapshot, IdentitySnapshot


class AccountPreflightCode(str, Enum):
    OK = "OK"
    AGENT_MISMATCH = "AGENT_MISMATCH"
    UNSUPPORTED_ACCOUNT_MODE = "UNSUPPORTED_ACCOUNT_MODE"
    ACCOUNT_STATE_INVALID = "ACCOUNT_STATE_INVALID"
    ACCOUNT_NOT_CLEAN = "ACCOUNT_NOT_CLEAN"
    NO_AVAILABLE_COLLATERAL = "NO_AVAILABLE_COLLATERAL"
    NO_EXECUTABLE_CAPACITY = "NO_EXECUTABLE_CAPACITY"


@dataclass(frozen=True)
class AccountPreflight:
    ok: bool
    code: AccountPreflightCode
    available_collateral: float
    position_count: int
    open_order_count: int


def _agent_owner(role: Any) -> str:
    if not isinstance(role, dict) or role.get("role") != "agent":
        return ""
    data = role.get("data")
    return str(data.get("user") or "").lower() if isinstance(data, dict) else ""


def _unified_usdc_available(snapshot: AccountSnapshot) -> float:
    balances = snapshot.collateral_state.get("balances") if isinstance(snapshot.collateral_state, dict) else None
    for row in balances or []:
        if isinstance(row, dict) and row.get("coin") == "USDC":
            try:
                return max(0.0, float(row.get("total") or 0) - float(row.get("hold") or 0))
            except (TypeError, ValueError):
                return -1.0
    return 0.0


def evaluate_account_preflight(
    identity: IdentitySnapshot,
    snapshot: AccountSnapshot,
) -> AccountPreflight:
    positions = []
    for state in snapshot.perp_states.values():
        rows = state.get("assetPositions") if isinstance(state, dict) else None
        if not isinstance(rows, list):
            return AccountPreflight(False, AccountPreflightCode.ACCOUNT_STATE_INVALID, 0.0, 0, 0)
        for row in rows:
            position = row.get("position") if isinstance(row, dict) else None
            try:
                size = abs(float(position.get("szi") or 0)) if isinstance(position, dict) else 0.0
            except (TypeError, ValueError):
                return AccountPreflight(False, AccountPreflightCode.ACCOUNT_STATE_INVALID, 0.0, 0, 0)
            if size > 1e-12:
                positions.append(position)
    order_count = sum(len(rows) for rows in snapshot.open_orders.values() if isinstance(rows, list))
    available = _unified_usdc_available(snapshot)
    base = dict(
        available_collateral=max(0.0, available),
        position_count=len(positions),
        open_order_count=order_count,
    )
    if _agent_owner(identity.agent_role) != snapshot.account_address:
        return AccountPreflight(False, AccountPreflightCode.AGENT_MISMATCH, **base)
    if snapshot.abstraction != "unifiedAccount":
        return AccountPreflight(False, AccountPreflightCode.UNSUPPORTED_ACCOUNT_MODE, **base)
    if available < 0:
        return AccountPreflight(False, AccountPreflightCode.ACCOUNT_STATE_INVALID, **base)
    if positions or order_count:
        return AccountPreflight(False, AccountPreflightCode.ACCOUNT_NOT_CLEAN, **base)
    if available <= 0:
        return AccountPreflight(False, AccountPreflightCode.NO_AVAILABLE_COLLATERAL, **base)
    return AccountPreflight(True, AccountPreflightCode.OK, **base)
