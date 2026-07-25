"""Official Portfolio precheck for high-quality Perp discovery candidates."""

from __future__ import annotations

from dataclasses import dataclass


DAY_MS = 86_400_000
WINDOWS = (
    ("week", "perpWeek", "week"),
    ("month", "perpMonth", "month"),
    ("allTime", "perpAllTime", "all"),
)


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pnl_delta(window: dict | None) -> float | None:
    """Return the official series' terminal minus initial PnL, or None when incomplete."""
    history = (window or {}).get("pnlHistory")
    if not isinstance(history, list) or len(history) < 2:
        return None
    first = history[0]
    last = history[-1]
    first_value = _number(first[-1] if isinstance(first, (list, tuple)) and first else None)
    last_value = _number(last[-1] if isinstance(last, (list, tuple)) and last else None)
    if first_value is None or last_value is None:
        return None
    return last_value - first_value


def _portfolio_map(payload) -> dict:
    if not isinstance(payload, list):
        return {}
    return {
        str(item[0]): item[1]
        for item in payload
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict)
    }


def _history(window: dict | None, key: str) -> list[tuple[int, float]]:
    """Return one deduplicated, time-ordered official Portfolio series."""
    values = {}
    for item in (window or {}).get(key) or ():
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        stamp = _number(item[0])
        value = _number(item[-1])
        if stamp is None or value is None:
            continue
        values[int(stamp)] = float(value)
    return sorted(values.items())


def _at_boundary(
    series: list[tuple[int, float]],
    stamp: int,
    *,
    max_gap_ms: int,
    positive: bool = False,
) -> tuple[float | None, str]:
    """Interpolate one official mark without turning a normal daily sample gap into missing evidence."""
    left = right = None
    for sample in series:
        if sample[0] <= stamp:
            left = sample
        if sample[0] >= stamp:
            right = sample
            break
    if left is None or right is None:
        return None, "boundary_sample_gap"
    if left[0] == stamp:
        value = float(left[1])
        return (
            (None, "zero_start_equity")
            if positive and value <= 0.0 else (value, "exact")
        )
    if right[0] == stamp:
        value = float(right[1])
        return (
            (None, "zero_start_equity")
            if positive and value <= 0.0 else (value, "exact")
        )
    if (
        stamp - left[0] > max_gap_ms
        or right[0] - stamp > max_gap_ms
        or right[0] - left[0] > max_gap_ms
    ):
        return None, "boundary_sample_gap"
    if positive and (float(left[1]) <= 0.0 or float(right[1]) <= 0.0):
        return None, "zero_start_equity"
    ratio = (stamp - left[0]) / max(1, right[0] - left[0])
    value = float(left[1]) + (float(right[1]) - float(left[1])) * ratio
    if positive and value <= 0.0:
        return None, "zero_start_equity"
    return value, "interpolated"


def official_perp_month_return(
    window: dict | None,
    *,
    max_boundary_gap_hours: float = 36.0,
) -> dict:
    """Return the official ``perpMonth`` ROI from its own window endpoints.

    Hyperliquid's month series can span 28–30 days depending on the sampling boundary.  Treat the
    official window as authoritative instead of inventing a synthetic 30-day boundary outside it.
    Interpolation is only allowed when an endpoint differs between the PnL and equity series and the
    adjacent samples are at most 36 hours apart.
    """
    pnl = _history(window, "pnlHistory")
    equity = _history(window, "accountValueHistory")
    if len(pnl) < 2 or len(equity) < 2:
        return {
            "version": "official-perp-30d-return-v1",
            "evidenceSufficient": False, "reason": "history_under_28d",
        }
    start_ms = max(pnl[0][0], equity[0][0])
    end_ms = min(pnl[-1][0], equity[-1][0])
    if end_ms - start_ms < 28 * DAY_MS:
        return {
            "version": "official-perp-30d-return-v1",
            "evidenceSufficient": False, "reason": "history_under_28d",
            "startMs": start_ms, "endMs": end_ms,
        }
    max_gap_ms = max(1, int(float(max_boundary_gap_hours) * 3_600_000))
    pnl_start, pnl_start_source = _at_boundary(pnl, start_ms, max_gap_ms=max_gap_ms)
    pnl_end, pnl_end_source = _at_boundary(pnl, end_ms, max_gap_ms=max_gap_ms)
    start_equity, equity_source = _at_boundary(
        equity, start_ms, max_gap_ms=max_gap_ms, positive=True,
    )
    reason = next(
        (
            value for value in (pnl_start_source, pnl_end_source, equity_source)
            if value in {"boundary_sample_gap", "zero_start_equity"}
        ),
        None,
    )
    sufficient = bool(
        pnl_start is not None and pnl_end is not None
        and start_equity is not None and start_equity > 0.0
    )
    net_pnl = (pnl_end - pnl_start) if sufficient else None
    return {
        "version": "official-perp-30d-return-v1",
        "maxBoundaryGapHours": float(max_boundary_gap_hours),
        "evidenceSufficient": sufficient,
        "reason": reason or ("passed" if sufficient else "boundary_sample_gap"),
        "startMs": start_ms, "endMs": end_ms,
        "startEquity": start_equity,
        "netPnl": net_pnl,
        "return": (net_pnl / start_equity) if sufficient else None,
        "boundarySource": {
            "pnlStart": pnl_start_source,
            "pnlEnd": pnl_end_source,
            "startEquity": equity_source,
        },
    }


@dataclass(frozen=True)
class Result:
    status: str
    reason: str
    windows: dict

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def deferred(self) -> bool:
        return self.status == "deferred_data_error"

    def payload(self) -> dict:
        return {"status": self.status, "reason": self.reason, "windows": self.windows}


def evaluate(
    payload,
    *,
    pnl_minima: dict[str, float],
    share_min: float,
    min_return_30d: float = 0.20,
    max_boundary_gap_hours: float = 36.0,
) -> Result:
    """Require profitable, Perp-led activity and at least 20% official Perp 30-day ROI."""
    del pnl_minima
    windows = _portfolio_map(payload)
    if not windows:
        return Result("deferred_data_error", "portfolio_unavailable", {})
    metrics = {}
    for total_key, perp_key, label in WINDOWS:
        if total_key not in windows or perp_key not in windows:
            if label == "month":
                return Result("deferred_data_error", f"portfolio_window_missing:{label}", metrics)
            metrics[label] = {"auditStatus": "missing", "hardGate": False}
            continue
        total_pnl = pnl_delta(windows[total_key])
        perp_pnl = pnl_delta(windows[perp_key])
        if total_pnl is None or perp_pnl is None:
            if label == "month":
                return Result("deferred_data_error", f"portfolio_history_incomplete:{label}", metrics)
            metrics[label] = {"auditStatus": "incomplete", "hardGate": False}
            continue
        share = (perp_pnl / total_pnl) if total_pnl > 0 else None
        metrics[label] = {
            "totalPnl": total_pnl, "perpPnl": perp_pnl, "perpShare": share,
            "hardGate": label == "month", "auditStatus": "complete",
        }
    month = metrics.get("month") or {}
    if float(month.get("perpPnl") or 0.0) <= 0.0:
        return Result("rejected", "perp_pnl_not_profitable:month", metrics)
    if month.get("perpShare") is None or float(month["perpShare"]) < float(share_min):
        return Result("rejected", "perp_share_below_floor:month", metrics)
    official_return = official_perp_month_return(
        windows.get("perpMonth"),
        max_boundary_gap_hours=max_boundary_gap_hours,
    )
    metrics["officialPerp30d"] = official_return
    if not official_return["evidenceSufficient"]:
        return Result(
            "deferred_data_error",
            str(official_return.get("reason") or "official_perp_return_evidence_incomplete"),
            metrics,
        )
    month["perpReturn"] = official_return.get("return")
    month["perpStartEquity"] = official_return.get("startEquity")
    if float(official_return.get("return") or 0.0) < float(min_return_30d):
        return Result("rejected", "official_perp_return_below_floor:month", metrics)
    return Result("passed", "perp_prefilter_passed", metrics)
