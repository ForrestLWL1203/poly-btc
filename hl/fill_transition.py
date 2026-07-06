"""Classify target position changes from one Hyperliquid fill."""

from . import config


def _flat(pos: float, flat: float) -> bool:
    return abs(pos) < flat


def classify_fill_transition(pos0: float, pos1: float, flat: float = config.FLAT) -> str:
    """Return open/add/reduce/close/flip for a target position transition."""
    start_flat = _flat(pos0, flat)
    end_flat = _flat(pos1, flat)
    if start_flat and end_flat:
        return "close"
    if start_flat:
        return "open"
    if end_flat:
        return "close"
    if (pos0 > 0 > pos1) or (pos0 < 0 < pos1):
        return "flip"
    return "add" if abs(pos1) >= abs(pos0) - flat else "reduce"
