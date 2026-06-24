"""Tiny pure helpers used everywhere."""
import time


def f(v) -> float:
    """Parse to float, tolerating None / bad strings -> 0.0 (HL sends numbers as strings)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def now_ms() -> int:
    return int(time.time() * 1000)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
