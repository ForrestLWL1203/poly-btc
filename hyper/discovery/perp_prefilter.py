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


def _at_or_before(series: list[tuple[int, float]], stamp: int, *, max_gap_ms: int) -> float | None:
    """Use the last official mark at a boundary, rejecting stale/gappy evidence."""
    selected = None
    selected_stamp = None
    for sample_stamp, value in series:
        if sample_stamp > stamp:
            break
        selected = value
        selected_stamp = sample_stamp
    if selected_stamp is None or stamp - selected_stamp > max_gap_ms:
        return None
    return selected


def official_weekly_stability(
    window: dict | None,
    *,
    fold_days: int = 7,
    fold_count: int = 4,
    min_return: float = 0.05,
) -> dict:
    """Evaluate adjacent official Perp-return folds before downloading fills.

    Leaderboard exposes only one rolling week and month. The Portfolio month response already fetched by
    this precheck contains deposit-adjusted net-PnL and account-value time series, which is the earliest
    honest source for independent weekly returns. Campaign independence and 1.5x cost stress still require
    fills and are confirmed by canonical strict Copy later.
    """
    fold_days = max(1, int(fold_days))
    fold_count = max(1, int(fold_count))
    min_return = max(0.0, float(min_return))
    pnl = _history(window, "pnlHistory")
    equity = _history(window, "accountValueHistory")
    if len(pnl) < 2 or len(equity) < 2:
        return {"evidenceSufficient": False, "passed": False, "folds": []}

    width = fold_days * DAY_MS
    end_ms = min(pnl[-1][0], equity[-1][0])
    start_ms = end_ms - fold_count * width
    folds = []
    for index in range(fold_count):
        lo = start_ms + index * width
        hi = lo + width
        pnl_start = _at_or_before(pnl, lo, max_gap_ms=DAY_MS)
        pnl_end = _at_or_before(pnl, hi, max_gap_ms=DAY_MS)
        start_equity = _at_or_before(equity, lo, max_gap_ms=DAY_MS)
        evaluable = bool(
            pnl_start is not None and pnl_end is not None
            and start_equity is not None and start_equity > 0.0
        )
        net = (pnl_end - pnl_start) if evaluable else None
        fold_return = (net / start_equity) if evaluable else None
        folds.append({
            "index": index + 1, "startMs": lo, "endMs": hi,
            "netPnl": net, "startEquity": start_equity, "return": fold_return,
            "returnFloor": min_return, "evaluable": evaluable,
            "qualified": bool(evaluable and fold_return >= min_return),
        })
    sufficient = len(folds) == fold_count and all(fold["evaluable"] for fold in folds)
    return {
        "version": "official-nonoverlap-weekly-return-v1",
        "foldDays": fold_days, "foldCount": fold_count, "returnFloor": min_return,
        "evidenceSufficient": sufficient,
        "qualifiedFolds": sum(bool(fold["qualified"]) for fold in folds),
        "passed": bool(sufficient and all(fold["qualified"] for fold in folds)),
        "folds": folds,
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
    stability_fold_days: int = 7,
    stability_fold_count: int = 4,
    stability_min_return: float = 0.05,
) -> Result:
    """Require profitable, Perp-led activity and early official weekly stability.

    The raw Leaderboard gate owns only cheap account/activity and positive 7/30-day PnL recall. Portfolio
    week/all-time aggregate windows remain audit-only. The dense ``perpMonth`` series owns the earliest
    independent four-week profitability gate; later strict Copy confirms Campaign count and execution-cost
    robustness using our own capital.
    """
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
    stability = official_weekly_stability(
        windows.get("perpMonth"),
        fold_days=stability_fold_days,
        fold_count=stability_fold_count,
        min_return=stability_min_return,
    )
    metrics["officialStability"] = stability
    if not stability["evidenceSufficient"]:
        return Result("deferred_data_error", "portfolio_weekly_stability_incomplete", metrics)
    if not stability["passed"]:
        return Result("rejected", "portfolio_weekly_return_below_floor", metrics)
    return Result("passed", "perp_prefilter_passed", metrics)
