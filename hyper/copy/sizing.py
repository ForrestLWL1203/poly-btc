"""Shared copy-sizing helpers for live observer and offline replay."""
from __future__ import annotations


def sizing_equity_for_drawdown(
    risk_equity: float,
    capital_anchor: float,
    *,
    exponent: float = 0.50,
    max_multiplier: float = 1.50,
) -> float:
    """Return the equity base used for one-position margin sizing.

    Above the strategy allocation anchor, profits compound one-for-one. Below
    it, a concave curve slows position shrinkage while `max_multiplier` bounds
    the effective risk uplift versus real strategy equity. Portfolio and coin
    caps still use `risk_equity`, never this smoothed base.
    """
    equity = max(0.0, float(risk_equity or 0.0))
    anchor = max(0.0, float(capital_anchor or 0.0))
    if equity <= 0:
        return 0.0
    if anchor <= 0 or equity >= anchor:
        return equity
    power = max(0.0, min(1.0, float(exponent)))
    smoothed = anchor * ((equity / anchor) ** power)
    multiplier = max(1.0, float(max_multiplier or 1.0))
    return min(smoothed, equity * multiplier)


def margin_pct_for_deploy(max_pct: float, min_pct: float, deploy_full_pct: float,
                          max_deploy_pct: float, locked_margin: float, equity: float) -> float:
    """Return the tuned first-open margin until the aggregate deploy cap.

    ``min_pct`` and ``deploy_full_pct`` are retained in the call signature only
    so older immutable strategy snapshots remain replayable.  The former
    firepower line duplicated the aggregate deploy cap and made otherwise valid
    opens too small before any real account contention occurred.
    """
    upper = max(0.0, float(max_pct or 0.0))
    if equity <= 0:
        return 0.0
    return upper
