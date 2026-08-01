"""Pure Hyperliquid order preparation and response normalization.

There are no network calls or credentials in this module.  Keeping precision, client-order ids and exchange
response parsing pure makes the dangerous part of the future Live path deterministic and fully testable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR
from enum import Enum
from typing import Optional


MIN_PERP_NOTIONAL_USD = Decimal("10")


class OrderValidationError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


class OrderOutcome(str, Enum):
    FILLED = "filled"
    PARTIAL = "partial"
    RESTING = "resting"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MarketSpec:
    coin: str
    dex: str
    asset_id: int
    sz_decimals: int
    max_leverage: int


@dataclass(frozen=True)
class OrderIntent:
    coin: str
    is_buy: bool
    size: float
    limit_px: float
    reduce_only: bool
    cloid: str


@dataclass(frozen=True)
class PreparedOrder:
    coin: str
    is_buy: bool
    size: float
    limit_px: float
    notional: float
    reduce_only: bool
    cloid: str


@dataclass(frozen=True)
class SubmitResult:
    outcome: OrderOutcome
    oid: Optional[int] = None
    filled_size: float = 0.0
    average_px: Optional[float] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class ClientOrderId:
    """SDK-compatible 16-byte client order id without importing the SDK in pure tests."""

    def __init__(self, raw: str):
        value = str(raw)
        if len(value) != 34 or not value.startswith("0x"):
            raise OrderValidationError("invalid_cloid", "expected_0x_plus_32_hex_chars")
        try:
            int(value[2:], 16)
        except ValueError as exc:
            raise OrderValidationError("invalid_cloid", "not_hex") from exc
        self._raw = value.lower()

    def to_raw(self) -> str:
        return self._raw

    def __str__(self) -> str:
        return self._raw


def deterministic_cloid(*parts) -> str:
    """Return a stable, domain-separated 16-byte CLOID for one logical copy action."""
    payload = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"), default=str)
    digest = hashlib.sha256(b"poly-btc/hyperliquid-order/v1\0" + payload.encode("utf-8")).hexdigest()
    return "0x" + digest[:32]


def dex_for_coin(coin: str) -> str:
    value = str(coin or "")
    return value.split(":", 1)[0] if ":" in value else ""


def _decimal(value, code: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OrderValidationError(code, "not_numeric") from exc
    if not number.is_finite():
        raise OrderValidationError(code, "not_finite")
    return number


def quantize_size(size, sz_decimals: int) -> Decimal:
    if not 0 <= int(sz_decimals) <= 6:
        raise OrderValidationError("invalid_sz_decimals", str(sz_decimals))
    value = _decimal(size, "invalid_size")
    if value <= 0:
        raise OrderValidationError("invalid_size", "must_be_positive")
    quantum = Decimal(1).scaleb(-int(sz_decimals))
    rounded = value.quantize(quantum, rounding=ROUND_DOWN)
    if rounded <= 0:
        raise OrderValidationError("size_rounds_to_zero")
    return rounded


def quantize_perp_price(price, sz_decimals: int, *, is_buy: bool) -> Decimal:
    """Round toward the trader-safe side while satisfying Hyperliquid perp price rules.

    Buy limits round down and sell limits round up, so precision normalization can reduce fill probability but
    never silently makes an order more aggressive than the approved book price.
    """
    if not 0 <= int(sz_decimals) <= 6:
        raise OrderValidationError("invalid_sz_decimals", str(sz_decimals))
    value = _decimal(price, "invalid_price")
    if value <= 0:
        raise OrderValidationError("invalid_price", "must_be_positive")
    if value == value.to_integral_value():
        return value

    decimal_places = max(0, 6 - int(sz_decimals))
    significant_exponent = value.adjusted() - 4
    decimal_exponent = -decimal_places
    quantum = Decimal(1).scaleb(max(significant_exponent, decimal_exponent))
    rounding = ROUND_FLOOR if is_buy else ROUND_CEILING
    rounded = value.quantize(quantum, rounding=rounding)
    if rounded <= 0:
        raise OrderValidationError("price_rounds_to_zero")
    return rounded


def prepare_ioc_order(intent: OrderIntent, market: MarketSpec) -> PreparedOrder:
    if not intent.coin or intent.coin != market.coin:
        raise OrderValidationError("market_mismatch", f"{intent.coin}!={market.coin}")
    cloid = ClientOrderId(intent.cloid).to_raw()
    size = quantize_size(intent.size, market.sz_decimals)
    price = quantize_perp_price(intent.limit_px, market.sz_decimals, is_buy=bool(intent.is_buy))
    notional = size * price
    if notional < MIN_PERP_NOTIONAL_USD:
        raise OrderValidationError("min_trade_notional", str(notional))
    return PreparedOrder(
        coin=intent.coin,
        is_buy=bool(intent.is_buy),
        size=float(size),
        limit_px=float(price),
        notional=float(notional),
        reduce_only=bool(intent.reduce_only),
        cloid=cloid,
    )


_ERROR_PATTERNS = (
    ("minimum value", "min_trade_notional"),
    ("mintraden", "min_trade_notional"),
    ("insufficient margin", "insufficient_margin"),
    ("perpmargin", "insufficient_margin"),
    ("reduce only", "reduce_only"),
    ("reduceonly", "reduce_only"),
    ("price must be divisible", "invalid_tick"),
    ("tick", "invalid_tick"),
    ("could not immediately match", "ioc_cancel"),
    ("ioc", "ioc_cancel"),
    ("no liquidity", "no_liquidity"),
    ("open interest", "open_interest_cap"),
    ("oracle", "oracle_reject"),
    ("max position", "max_position"),
    ("order was never placed", "missing_order"),
)


def normalize_error(message) -> str:
    value = str(message or "unknown_exchange_error")
    lowered = value.lower().replace("_", "")
    for needle, code in _ERROR_PATTERNS:
        if needle.replace("_", "") in lowered:
            return code
    return "exchange_reject"


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_order_response(response, *, requested_size: float) -> SubmitResult:
    if not isinstance(response, dict):
        return SubmitResult(OrderOutcome.UNKNOWN, error_code="invalid_response")
    if response.get("status") != "ok":
        nested = response.get("response")
        message = (
            response.get("error")
            or response.get("message")
            or (nested if isinstance(nested, str) else None)
            or "exchange_status_not_ok"
        )
        return SubmitResult(
            OrderOutcome.REJECTED,
            error_code=normalize_error(message),
            error_message=str(message),
        )
    envelope = response.get("response")
    if not isinstance(envelope, dict) or envelope.get("type") != "order":
        return SubmitResult(OrderOutcome.UNKNOWN, error_code="unexpected_response_type")
    data = envelope.get("data")
    statuses = data.get("statuses") if isinstance(data, dict) else None
    if not isinstance(statuses, list) or len(statuses) != 1:
        return SubmitResult(OrderOutcome.UNKNOWN, error_code="unexpected_order_status_count")
    status = statuses[0]
    if isinstance(status, str):
        message = status
        code = normalize_error(message)
        outcome = OrderOutcome.CANCELED if code == "ioc_cancel" else OrderOutcome.REJECTED
        return SubmitResult(outcome, error_code=code, error_message=message)
    if not isinstance(status, dict):
        return SubmitResult(OrderOutcome.UNKNOWN, error_code="invalid_order_status")
    if "error" in status:
        message = str(status.get("error") or "unknown_exchange_error")
        code = normalize_error(message)
        outcome = OrderOutcome.CANCELED if code == "ioc_cancel" else OrderOutcome.REJECTED
        return SubmitResult(outcome, error_code=code, error_message=message)
    if "resting" in status and isinstance(status["resting"], dict):
        resting = status["resting"]
        return SubmitResult(OrderOutcome.RESTING, oid=resting.get("oid"))
    if "filled" in status and isinstance(status["filled"], dict):
        filled = status["filled"]
        filled_size = _float(filled.get("totalSz"), default=None)
        requested = _float(requested_size, default=None)
        if filled_size is None or requested is None or filled_size < 0 or requested <= 0:
            return SubmitResult(OrderOutcome.UNKNOWN, error_code="invalid_fill_status")
        outcome = OrderOutcome.PARTIAL if filled_size + 1e-12 < requested else OrderOutcome.FILLED
        return SubmitResult(
            outcome,
            oid=filled.get("oid"),
            filled_size=filled_size,
            average_px=_float(filled.get("avgPx"), default=None),
        )
    return SubmitResult(OrderOutcome.UNKNOWN, error_code="unknown_order_status")


def normalize_action_response(response) -> ActionResult:
    if not isinstance(response, dict):
        return ActionResult(ok=False, error_code="exchange_reject", error_message="invalid_response")
    if response.get("status") != "ok":
        nested = response.get("response")
        message = (
            response.get("error")
            or response.get("message")
            or (nested if isinstance(nested, str) else None)
            or "exchange_status_not_ok"
        )
        return ActionResult(ok=False, error_code=normalize_error(message), error_message=str(message))

    envelope = response.get("response")
    if not isinstance(envelope, dict):
        return ActionResult(ok=False, error_code="exchange_reject", error_message="invalid_response")
    if envelope.get("type") == "default":
        return ActionResult(ok=True)

    data = envelope.get("data")
    statuses = data.get("statuses") if isinstance(data, dict) else None
    if not isinstance(statuses, list) or len(statuses) != 1:
        return ActionResult(ok=False, error_code="exchange_reject", error_message="invalid_action_status_count")
    status = statuses[0]
    if status == "success":
        return ActionResult(ok=True)
    if isinstance(status, dict) and "error" in status:
        message = str(status.get("error") or "unknown_exchange_error")
    else:
        message = str(status or "unknown_exchange_error")
    return ActionResult(ok=False, error_code=normalize_error(message), error_message=message)
