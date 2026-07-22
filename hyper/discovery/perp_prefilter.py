"""Official Portfolio precheck for high-quality Perp discovery candidates."""

from __future__ import annotations

from dataclasses import dataclass


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


def evaluate(payload, *, pnl_minima: dict[str, float], share_min: float) -> Result:
    """Require profitable, Perp-led activity in every official window.

    Deep history collection is deliberately reserved for wallets whose cheap Portfolio surface already
    clears the configured 7d/30d/lifetime absolute PnL floors and Perp-share floor. Missing transport in any
    window is quarantined; non-positive total PnL cannot establish a meaningful positive Perp share.
    """
    windows = _portfolio_map(payload)
    if not windows:
        return Result("deferred_data_error", "portfolio_unavailable", {})
    metrics = {}
    for total_key, perp_key, label in WINDOWS:
        if total_key not in windows or perp_key not in windows:
            return Result("deferred_data_error", f"portfolio_window_missing:{label}", metrics)
        total_pnl = pnl_delta(windows[total_key])
        perp_pnl = pnl_delta(windows[perp_key])
        if total_pnl is None or perp_pnl is None:
            return Result("deferred_data_error", f"portfolio_history_incomplete:{label}", metrics)
        share = (perp_pnl / total_pnl) if total_pnl > 0 else None
        metrics[label] = {"totalPnl": total_pnl, "perpPnl": perp_pnl, "perpShare": share}
        if perp_pnl < float(pnl_minima[label]):
            return Result("rejected", f"perp_pnl_below_floor:{label}", metrics)
        if share is None or share < float(share_min):
            return Result("rejected", f"perp_share_below_floor:{label}", metrics)
    return Result("passed", "perp_prefilter_passed", metrics)
