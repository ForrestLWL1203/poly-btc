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

    # A leading zero is normally an account's initial funding boundary, not evidence that its later
    # Perp trading is invalid. Start after the latest non-positive sample so a later reset can never be
    # crossed. Every observed equity sample in the accepted coverage is therefore strictly positive.
    last_nonpositive_ms = max(
        (stamp for stamp, value in equity if raw_start_ms <= stamp <= end_ms and value <= 0.0),
        default=None,
    )
    if last_nonpositive_ms is None:
        positive_start_ms = raw_start_ms
    else:
        positive_start_ms = next(
            (
                stamp for stamp, value in equity
                if last_nonpositive_ms < stamp <= end_ms and value > 0.0
            ),
            None,
        )
        if positive_start_ms is None:
            return {
                "version": "official-perp-observed-return-v2",
                "evidenceSufficient": False, "reason": "history_under_7d",
                "rawStartMs": raw_start_ms, "endMs": end_ms,
                "positiveCoverageDays": 0.0,
            }

    positive_coverage_days = (end_ms - positive_start_ms) / DAY_MS
    long_days = max(1, int(long_history_days))
    short_days = max(1, min(int(short_history_days), long_days))
    if positive_coverage_days >= long_days:
        start_ms = positive_start_ms
        history_tier = "full_history"
        minimum_return = float(min_return_30d)
    elif positive_coverage_days >= short_days:
        start_ms = end_ms - short_days * DAY_MS
        history_tier = "short_history_7d"
        minimum_return = float(min_return_7d)
    else:
        return {
            "version": "official-perp-observed-return-v2",
            "evidenceSufficient": False, "reason": "history_under_7d",
            "rawStartMs": raw_start_ms, "endMs": end_ms,
            "positiveCoverageStartMs": positive_start_ms,
            "positiveCoverageDays": positive_coverage_days,
            "minimumHistoryDays": short_days,
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
        "version": "official-perp-observed-return-v2",
        "maxBoundaryGapHours": float(max_boundary_gap_hours),
        "evidenceSufficient": sufficient,
        "reason": reason or ("passed" if sufficient else "boundary_sample_gap"),
        "historyTier": history_tier,
        "rawStartMs": raw_start_ms,
        "positiveCoverageStartMs": positive_start_ms,
        "positiveCoverageDays": positive_coverage_days,
        "startMs": start_ms, "endMs": end_ms,
        "windowDays": (end_ms - start_ms) / DAY_MS,
        "minimumReturn": minimum_return,
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
    min_return_7d: float = 0.05,
    long_history_days: int = 28,
    short_history_days: int = 7,
    max_boundary_gap_hours: float = 36.0,
) -> Result:
    """Require profitable, Perp-led activity and a qualified long/short official Perp ROI."""
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
        long_history_days=long_history_days,
        short_history_days=short_history_days,
        min_return_30d=min_return_30d,
        min_return_7d=min_return_7d,
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
    required_return = float(official_return.get("minimumReturn") or min_return_30d)
    if float(official_return.get("return") or 0.0) + 1e-12 < required_return:
        suffix = (
            "short_7d"
            if official_return.get("historyTier") == "short_history_7d"
            else "month"
        )
        return Result("rejected", f"official_perp_return_below_floor:{suffix}", metrics)
    reason = (
        "perp_prefilter_passed_short_history"
        if official_return.get("historyTier") == "short_history_7d"
        else "perp_prefilter_passed"
    )
    return Result("passed", reason, metrics)
