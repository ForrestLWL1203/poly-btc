"""Conservative profitability evidence shared by source and Copy qualification.

Closed Episodes prove profitability.  A positive terminal mark is retained for
audit only, while a negative terminal mark is charged in full because it is
already real account risk.
"""
from __future__ import annotations

import json
import math
from typing import Mapping


OPEN_LOSS_RATIO_LIMIT = 0.50
PROFITABILITY_BASIS = "closed_episode_minus_open_loss_v1"
STABILITY_7D_BASIS = "four_non_overlapping_closed_episode_7d_v1"
PROFIT_PRIORITY_30_WEIGHT = 0.60
PROFIT_PRIORITY_SEGMENT_AVG_WEIGHT = 0.25
PROFIT_PRIORITY_SEGMENT_WORST_WEIGHT = 0.15
LEGACY_PROFIT_PRIORITY_30_WEIGHT = 0.70
LEGACY_PROFIT_PRIORITY_7_WEIGHT = 0.30


def finite_number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return float(default) if math.isnan(number) or math.isinf(number) else number


def copy_stability_7d(metrics: Mapping) -> dict:
    """Normalize the generation-frozen four-by-seven-day stability evidence.

    The field is intentionally compact and may arrive either directly from an in-process replay or through
    the durable sector replay JSON.  A present but incomplete payload is different from a legacy generation
    that predates this evidence: the former must not silently receive a zero or the old 70/30 score.
    """
    raw = metrics.get("copy_bt_stability_7d")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
    if not isinstance(raw, Mapping) or not raw:
        return {
            "present": False,
            "evidenceComplete": False,
            "segments": [],
            "availableSegments": 0,
            "positiveSegments": 0,
            "averageReturn": None,
            "worstReturn": None,
            "latestReturn": None,
        }
    segments = []
    for index, item in enumerate(raw.get("segments") or (), 1):
        if not isinstance(item, Mapping):
            continue
        closed_n = max(0, int(finite_number(item.get("closedN"))))
        value = item.get("return")
        return_value = None
        if value is not None:
            number = finite_number(value, float("nan"))
            if not math.isnan(number) and not math.isinf(number):
                return_value = number
        segments.append({
            "index": int(finite_number(item.get("index"), index)),
            "startMs": int(finite_number(item.get("startMs"))),
            "endMs": int(finite_number(item.get("endMs"))),
            "closedN": closed_n,
            "closedPnl": finite_number(item.get("closedPnl")),
            "return": return_value,
            "profitFactor": finite_number(item.get("profitFactor")),
            "liquidations": max(0, int(finite_number(item.get("liquidations")))),
        })
    usable = [
        item for item in segments
        if item["closedN"] > 0 and item["return"] is not None
    ]
    complete = bool(
        len(segments) == 4
        and len(usable) == 4
        and raw.get("basis", STABILITY_7D_BASIS) == STABILITY_7D_BASIS
    )
    values = [float(item["return"]) for item in usable]
    return {
        "present": True,
        "basis": str(raw.get("basis") or STABILITY_7D_BASIS),
        "rangeDays": int(finite_number(raw.get("rangeDays"), 28)),
        "segmentDays": int(finite_number(raw.get("segmentDays"), 7)),
        "evidenceComplete": complete,
        "segments": segments,
        "availableSegments": len(usable),
        "positiveSegments": sum(value > 0.0 for value in values),
        "averageReturn": sum(values) / len(values) if complete else None,
        "worstReturn": min(values) if complete else None,
        "latestReturn": (
            segments[-1]["return"]
            if complete and segments else None
        ),
    }


def stable_profit_priority(return30, return7, stability: Mapping) -> tuple[float | None, dict]:
    """Return the stable-profit priority while keeping old generations readable."""
    long_return = finite_number(return30)
    stability = dict(stability or {})
    if stability.get("present"):
        if not stability.get("evidenceComplete"):
            return None, {
                "available": False,
                "legacy": False,
                "mode": "stable_profit_60_25_15_incomplete",
                "weights": {
                    "30d": PROFIT_PRIORITY_30_WEIGHT,
                    "segment7dAverage": PROFIT_PRIORITY_SEGMENT_AVG_WEIGHT,
                    "segment7dWorst": PROFIT_PRIORITY_SEGMENT_WORST_WEIGHT,
                },
            }
        average = finite_number(stability.get("averageReturn"))
        worst = finite_number(stability.get("worstReturn"))
        value = (
            PROFIT_PRIORITY_30_WEIGHT * long_return
            + PROFIT_PRIORITY_SEGMENT_AVG_WEIGHT * average
            + PROFIT_PRIORITY_SEGMENT_WORST_WEIGHT * worst
        )
        return value, {
            "available": True,
            "legacy": False,
            "mode": "stable_profit_60_25_15_four_segments_v1",
            "weights": {
                "30d": PROFIT_PRIORITY_30_WEIGHT,
                "segment7dAverage": PROFIT_PRIORITY_SEGMENT_AVG_WEIGHT,
                "segment7dWorst": PROFIT_PRIORITY_SEGMENT_WORST_WEIGHT,
            },
        }
    # Immutable generations published before the stability evidence remain displayable and executable. New
    # scans always persist the four segments, so this branch cannot admit a new wallet under the old model.
    recent_return = finite_number(return7)
    return (
        LEGACY_PROFIT_PRIORITY_30_WEIGHT * long_return
        + LEGACY_PROFIT_PRIORITY_7_WEIGHT * recent_return
    ), {
        "available": True,
        "legacy": True,
        "mode": "legacy_conservative_profit_70_30",
        "weights": {
            "30d": LEGACY_PROFIT_PRIORITY_30_WEIGHT,
            "7d": LEGACY_PROFIT_PRIORITY_7_WEIGHT,
        },
    }


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
