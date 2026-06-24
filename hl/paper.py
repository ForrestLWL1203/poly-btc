"""Paper-copy simulation math — pure functions over book snapshots.

Given a master's round-trip (episode) and our copy latencies, compute what WE would
have made copying it: taker fills off the live book at signal_time+latency, fixed
notional, our fees, and the slippage vs the master's price. PnL is leverage-
independent (notional x price move); leverage only affects ROI/liquidation, handled
separately via MAE in the observer.
"""
from . import config


def book_at(hist, ts_ms: int):
    """First (bid, ask) at-or-after ts_ms in a bbo history deque; else the latest."""
    for t, bid, ask in hist:
        if t >= ts_ms:
            return (bid, ask)
    if hist:
        return (hist[-1][1], hist[-1][2])
    return None


def compute_legs(side: str, their_open_px: float, their_close_px: float,
                 entry_by_lat: dict, exit_by_lat: dict,
                 notional: float = config.NOTIONAL, taker_fee: float = config.TAKER_FEE) -> list:
    """One leg per latency. entry_by_lat/exit_by_lat map latency -> executable price
    (already taker-adjusted: buy at ask, sell at bid). Returns list of dicts."""
    side_is_buy = side == "long"
    legs = []
    for lat, en in entry_by_lat.items():
        ex = exit_by_lat.get(lat)
        if not en or not ex:
            continue
        qty = notional / en
        gross = qty * (ex - en) * (1 if side_is_buy else -1)
        fees = notional * taker_fee * 2
        pnl = gross - fees
        slip_in = (en - their_open_px) / their_open_px * 1e4 * (1 if side_is_buy else -1)
        slip_out = (their_close_px - ex) / their_close_px * 1e4 * (1 if side_is_buy else -1)
        legs.append({
            "latency_s": lat, "our_entry_px": en, "our_exit_px": ex,
            "slip_entry_bps": slip_in, "slip_exit_bps": slip_out,
            "pnl_usd": pnl, "pnl_pct": pnl / notional, "fees_usd": fees,
        })
    return legs
