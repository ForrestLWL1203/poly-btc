"""Pure live order-book capacity checks for planned copy orders.

Historical Copy replay deliberately does not use this module: one current L2 snapshot cannot describe
whether a fill from days ago was executable.  The Observer calls it only after sizing the actual order we
would place now, so a large source-wallet position does not make a small Paper copy look unfillable.
"""

from __future__ import annotations

from hyper.util import f


def assess_order_book(
    book,
    *,
    is_buy: bool,
    planned_notional: float,
    max_spread_bps: float,
    max_impact_bps: float,
) -> dict:
    """Return a size-aware L2 execution assessment.

    ``impact_bps`` is the all-in average execution displacement from the current mid, so it includes half
    the spread plus depth walked by this specific order.  Missing or malformed books are explicitly
    unavailable and let the caller use its market-context fallback.
    """
    wanted = max(0.0, f(planned_notional))
    levels = (book or {}).get("levels") if isinstance(book, dict) else None
    if wanted <= 0.0 or not levels or len(levels) != 2 or not levels[0] or not levels[1]:
        return {"available": False, "reason": "book_unavailable"}

    bids = sorted(
        (
            (f(level.get("px")), f(level.get("sz")))
            for level in levels[0]
            if f(level.get("px")) > 0.0 and f(level.get("sz")) > 0.0
        ),
        reverse=True,
    )
    asks = sorted(
        (
            (f(level.get("px")), f(level.get("sz")))
            for level in levels[1]
            if f(level.get("px")) > 0.0 and f(level.get("sz")) > 0.0
        ),
    )
    if not bids or not asks or asks[0][0] < bids[0][0]:
        return {"available": False, "reason": "book_unavailable"}

    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    spread_bps = (best_ask - best_bid) / mid * 10_000.0 if mid > 0.0 else float("inf")
    side = asks if is_buy else bids
    remaining = wanted
    filled_notional = 0.0
    filled_size = 0.0
    for px, size in side:
        level_notional = px * size
        take = min(remaining, level_notional)
        if take <= 0.0:
            continue
        filled_notional += take
        filled_size += take / px
        remaining -= take
        if remaining <= max(1e-9, wanted * 1e-12):
            break

    fill_ratio = min(1.0, filled_notional / wanted) if wanted > 0.0 else 1.0
    average_px = filled_notional / filled_size if filled_size > 0.0 else None
    impact_bps = float("inf")
    if average_px is not None and mid > 0.0:
        impact_bps = (
            (average_px - mid) / mid if is_buy else (mid - average_px) / mid
        ) * 10_000.0

    reason = None
    if fill_ratio < 1.0 - 1e-9:
        reason = "book_depth"
    elif spread_bps > max(0.0, f(max_spread_bps)):
        reason = "book_spread"
    elif impact_bps > max(0.0, f(max_impact_bps)):
        reason = "book_impact"
    return {
        "available": True,
        "reason": reason,
        "planned_notional": wanted,
        "filled_notional": filled_notional,
        "fill_ratio": fill_ratio,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "average_px": average_px,
        "spread_bps": spread_bps,
        "impact_bps": impact_bps,
    }
