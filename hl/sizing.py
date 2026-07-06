"""Shared copy-sizing helpers for live observer and offline replay."""
from __future__ import annotations


def margin_pct_for_deploy(max_pct: float, min_pct: float, deploy_full_pct: float,
                          max_deploy_pct: float, locked_margin: float, equity: float) -> float:
    """Return the first-open margin% for the current portfolio deployment.

    <= deploy_full_pct uses max_pct. Between deploy_full_pct and max_deploy_pct
    it linearly shrinks to min_pct. At/above max_deploy_pct the caller's deploy
    room check will block new opens; returning min_pct keeps sizing monotonic.
    """
    upper = max(0.0, float(max_pct or 0.0))
    lower = max(0.0, float(min_pct or 0.0))
    if lower > upper:
        lower = upper
    if equity <= 0:
        return lower

    full = max(0.0, float(deploy_full_pct or 0.0))
    stop = max(0.0, float(max_deploy_pct or 0.0))
    deploy = max(0.0, float(locked_margin or 0.0) / float(equity))

    if deploy <= full:
        return upper
    if deploy >= stop or stop <= full:
        return lower

    weight = (stop - deploy) / (stop - full)
    return lower + (upper - lower) * max(0.0, min(1.0, weight))
