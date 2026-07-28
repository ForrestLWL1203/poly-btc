"""Conservative profitability evidence shared by source and Copy qualification.

Closed Episodes prove profitability.  A positive terminal mark is retained for
audit only, while a negative terminal mark is charged in full because it is
already real account risk.
"""
from __future__ import annotations

import math
from typing import Mapping


OPEN_LOSS_RATIO_LIMIT = 0.50
PROFITABILITY_BASIS = "closed_episode_minus_open_loss_v1"


def finite_number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return float(default) if math.isnan(number) or math.isinf(number) else number


def conservative_profitability(
    closed_pnl,
    unrealized_pnl,
    *,
    start_equity=None,
) -> dict:
    """Return the one-sided realized-profit contract for one replay window."""
    closed = finite_number(closed_pnl)
    unrealized = finite_number(unrealized_pnl)
    open_profit = max(0.0, unrealized)
    open_loss = max(0.0, -unrealized)
    qualification = closed - open_loss
    ratio = open_loss / closed if closed > 0.0 else None
    equity = finite_number(start_equity)
    return {
        "basis": PROFITABILITY_BASIS,
        "closedPnl": closed,
        "openProfitReference": open_profit,
        "openLoss": open_loss,
        "qualificationPnl": qualification,
        "openLossRatio": ratio,
        "openLossRatioLimit": OPEN_LOSS_RATIO_LIMIT,
        "windowStartEquity": equity if equity > 0.0 else None,
        "qualificationReturn": qualification / equity if equity > 0.0 else None,
    }


def replay_result_profitability(result: Mapping | None, *, start_equity=None) -> dict:
    """Read exact closed PnL from a canonical replay result.

    The marked-minus-unrealized fallback keeps old immutable generations
    readable. New qualification paths always receive ``closed_net_pnl`` from
    the canonical replay.
    """
    result = result or {}
    marked = finite_number(result.get("copy_net_pnl"))
    unrealized = finite_number(result.get("unrealized_pnl"))
    exact = result.get("closed_net_pnl") is not None
    closed = (
        finite_number(result.get("closed_net_pnl"))
        if exact else marked - unrealized
    )
    if start_equity is None:
        start_equity = (
            result.get("window_start_equity")
            if result.get("window_start_equity") is not None
            else result.get("initial_margin_equity")
        )
    return {
        **conservative_profitability(
            closed, unrealized, start_equity=start_equity,
        ),
        "markedPnl": marked,
        "exactClosedPnl": exact,
    }


def open_loss_ratio_within_limit(economics: Mapping) -> bool:
    ratio = economics.get("openLossRatio")
    return (
        finite_number(economics.get("closedPnl")) > 0.0
        and (ratio is None or finite_number(ratio) <= OPEN_LOSS_RATIO_LIMIT)
    )
