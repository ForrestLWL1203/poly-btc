"""Official Portfolio Perp-volume confirmation for newly recalled wallets."""

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


def perp_week_volume(payload) -> float | None:
    """Return the official Perp-only seven-day notional volume."""
    window = _portfolio_map(payload).get("perpWeek")
    if not isinstance(window, dict):
        return None
    return _number(window.get("vlm"))


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


def _funded_segments(
    equity: list[tuple[int, float]],
    start_ms: int,
    end_ms: int,
) -> list[tuple[int, int]]:
    """Return positive-equity operating segments, closing each one at its first zero sample."""
    segments = []
    segment_start = None
    for stamp, value in equity:
        if stamp < start_ms or stamp > end_ms:
            continue
        if value > 0.0 and segment_start is None:
            segment_start = stamp
        elif value <= 0.0 and segment_start is not None:
            if stamp > segment_start:
                segments.append((segment_start, stamp))
            segment_start = None
    if segment_start is not None and end_ms > segment_start:
        segments.append((segment_start, end_ms))
    return segments


def official_perp_month_return(
    window: dict | None,
    *,
    max_boundary_gap_hours: float = 36.0,
    long_history_days: int = 28,
    short_history_days: int = 7,
    min_return_30d: float = 0.20,
    min_return_7d: float = 0.05,
) -> dict:
    """Return one trustworthy official ``perpMonth`` qualification ROI.

    A wallet with at least 28 days of continuously positive Perp equity uses its full observable window
    and the normal 20% return floor. A younger wallet with at least seven days uses the latest complete
    seven-day window and a 5% floor. This admits genuinely strong new accounts without annualising one
    lucky week or dividing across a zero-equity funding boundary.
    """
    pnl = _history(window, "pnlHistory")
    equity = _history(window, "accountValueHistory")
    if len(pnl) < 2 or len(equity) < 2:
        return {
            "version": "official-perp-observed-return-v2",
            "evidenceSufficient": False, "reason": "history_under_7d",
        }
    raw_start_ms = max(pnl[0][0], equity[0][0])
    end_ms = min(pnl[-1][0], equity[-1][0])
    if end_ms <= raw_start_ms:
        return {
            "version": "official-perp-observed-return-v2",
            "evidenceSufficient": False, "reason": "history_under_7d",
            "rawStartMs": raw_start_ms, "endMs": end_ms,
        }

    # Portfolio PnL is deposit-adjusted. A temporary full withdrawal therefore closes one funded segment
    # without creating a loss, and a later deposit starts another segment on its own capital base. Compound
    # those segment returns instead of either (a) discarding all pre-withdrawal evidence or (b) dividing
    # post-deposit profit by an obsolete pre-withdrawal balance. A genuine liquidation remains visible as
    # the PnL loss at the zero-equity endpoint and cannot be repaired by a later deposit.
    funded_segments = _funded_segments(equity, raw_start_ms, end_ms)
    funded_coverage_ms = sum(end - start for start, end in funded_segments)
    funded_coverage_days = funded_coverage_ms / DAY_MS
    long_days = max(1, int(long_history_days))
    short_days = max(1, min(int(short_history_days), long_days))
    if funded_coverage_days >= long_days:
        selected_segments = funded_segments
        history_tier = "full_history"
        minimum_return = float(min_return_30d)
    elif funded_coverage_days >= short_days:
        # Use all observed funded segments for a young/re-funded wallet. Selecting an artificial trailing
        # seven funded days can cut through a sparse transfer boundary and invent a zero-equity denominator.
        selected_segments = funded_segments
        history_tier = "short_history_7d"
        minimum_return = float(min_return_7d)
    else:
        return {
            "version": "official-perp-funded-segments-v3",
            "evidenceSufficient": False, "reason": "history_under_7d",
            "rawStartMs": raw_start_ms, "endMs": end_ms,
            "positiveCoverageStartMs": funded_segments[0][0] if funded_segments else None,
            "positiveCoverageDays": funded_coverage_days,
            "fundedCoverageDays": funded_coverage_days,
            "fundedSegmentCount": len(funded_segments),
            "fundingResetCount": max(0, len(funded_segments) - 1),
            "minimumHistoryDays": short_days,
        }

    max_gap_ms = max(1, int(float(max_boundary_gap_hours) * 3_600_000))
    segment_results = []
    boundary_reason = None
    compounded_factor = 1.0
    net_pnl = 0.0
    for segment_start, segment_end in selected_segments:
        pnl_start, pnl_start_source = _at_boundary(
            pnl, segment_start, max_gap_ms=max_gap_ms,
        )
        pnl_end, pnl_end_source = _at_boundary(
            pnl, segment_end, max_gap_ms=max_gap_ms,
        )
        start_equity, equity_source = _at_boundary(
            equity, segment_start, max_gap_ms=max_gap_ms, positive=True,
        )
        reason = next(
            (
                value for value in (pnl_start_source, pnl_end_source, equity_source)
                if value in {"boundary_sample_gap", "zero_start_equity"}
            ),
            None,
        )
        if reason or pnl_start is None or pnl_end is None or not start_equity:
            boundary_reason = reason or "boundary_sample_gap"
            break
        segment_pnl = pnl_end - pnl_start
        segment_return = segment_pnl / start_equity
        compounded_factor *= max(0.0, 1.0 + segment_return)
        net_pnl += segment_pnl
        segment_results.append({
            "startMs": segment_start,
            "endMs": segment_end,
            "days": (segment_end - segment_start) / DAY_MS,
            "startEquity": start_equity,
            "netPnl": segment_pnl,
            "return": segment_return,
            "boundarySource": {
                "pnlStart": pnl_start_source,
                "pnlEnd": pnl_end_source,
                "startEquity": equity_source,
            },
        })
    sufficient = not boundary_reason and len(segment_results) == len(selected_segments)
    start_ms = selected_segments[0][0] if selected_segments else None
    start_equity = segment_results[0]["startEquity"] if segment_results else None
    return {
        "version": "official-perp-funded-segments-v3",
        "maxBoundaryGapHours": float(max_boundary_gap_hours),
        "evidenceSufficient": sufficient,
        "reason": boundary_reason or ("passed" if sufficient else "boundary_sample_gap"),
        "historyTier": history_tier,
        "rawStartMs": raw_start_ms,
        "positiveCoverageStartMs": funded_segments[0][0] if funded_segments else None,
        "positiveCoverageDays": funded_coverage_days,
        "fundedCoverageDays": funded_coverage_days,
        "fundedSegmentCount": len(funded_segments),
        "fundingResetCount": max(0, len(funded_segments) - 1),
        "startMs": start_ms, "endMs": end_ms,
        "windowDays": sum(row["days"] for row in segment_results),
        "minimumReturn": minimum_return,
        "startEquity": start_equity,
        "netPnl": net_pnl if sufficient else None,
        "return": (compounded_factor - 1.0) if sufficient else None,
        "segments": segment_results,
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
    min_week_perp_volume: float = 0.0,
) -> Result:
    """Confirm only that official seven-day Perp volume reaches the cheap-recall floor.

    Profit direction is already checked on Leaderboard; Portfolio ROI, account
    history, profit share and absolute PnL are audit telemetry and never qualify
    a wallet.
    """
    windows = _portfolio_map(payload)
    if not windows:
        return Result("deferred_data_error", "portfolio_unavailable", {})
    metrics = {}
    for total_key, perp_key, label in WINDOWS:
        if total_key not in windows or perp_key not in windows:
            if label == "week" and perp_key not in windows:
                return Result("deferred_data_error", "portfolio_window_missing:week", metrics)
            metrics[label] = {"auditStatus": "missing", "hardGate": False}
            continue
        total_pnl = pnl_delta(windows[total_key])
        perp_pnl = pnl_delta(windows[perp_key])
        if total_pnl is None or perp_pnl is None:
            metrics[label] = {"auditStatus": "incomplete", "hardGate": False}
        else:
            share = (perp_pnl / total_pnl) if total_pnl > 0 else None
            metrics[label] = {
                "totalPnl": total_pnl, "perpPnl": perp_pnl, "perpShare": share,
                "totalVlm": _number(windows[total_key].get("vlm")),
                "perpVlm": _number(windows[perp_key].get("vlm")),
                "hardGate": label == "week", "auditStatus": "complete",
            }
        if label == "week":
            metrics[label]["perpVlm"] = _number(windows[perp_key].get("vlm"))
    week = metrics.get("week") or {}
    if float(min_week_perp_volume) > 0.0:
        if week.get("perpVlm") is None:
            return Result("deferred_data_error", "portfolio_volume_incomplete:week", metrics)
        if float(week["perpVlm"]) < float(min_week_perp_volume):
            return Result("rejected", "perp_week_volume_below_floor", metrics)
    return Result("passed", "perp_week_volume_confirmed", metrics)
