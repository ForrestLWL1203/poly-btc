"""Per-coin realized volatility (daily σ) for risk-targeted copy sizing — REGIME-AWARE.

A single fixed-window σ lags a regime change: a coin calm for 30d then erupting keeps a low σ and
would get over-levered right as it turns dangerous. So we compute TWO horizons and take the MAX —
asymmetric on purpose: de-risk FAST when recent vol rises above the baseline, re-risk SLOWLY (only
once calm is sustained into the long window). σ lives in the coin_vol TABLE (one row/coin), refreshed
periodically off the signal hot path; the sizing code just reads the latest value.
"""
import math

from . import config, rest
from .util import f, now_iso


def _realized_sigma(closes: list):
    """Std-dev of daily log returns over `closes` (chronological). None if too few points."""
    closes = [c for c in closes if c > 0]
    if len(closes) < 3:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def compute(coin: str):
    """Fetch daily candles and return (sigma_used, sigma_fast, sigma_slow, n) or None.
    sigma_used = max(fast, slow): catches a fresh volatility regime fast, holds the baseline slowly."""
    cs = rest.candle_snapshot(coin, "1d", config.VOL_SLOW_DAYS)
    if not isinstance(cs, list) or len(cs) < config.VOL_MIN_SAMPLES + 1:
        return None
    closes = [f(c.get("c")) for c in sorted(cs, key=lambda c: c.get("t", 0))]
    slow = _realized_sigma(closes)
    if slow is None:
        return None
    fast = _realized_sigma(closes[-(config.VOL_FAST_DAYS + 1):]) or slow
    return max(fast, slow), fast, slow, len(closes)


def refresh(db, coin: str):
    """Recompute coin's σ and upsert its coin_vol row. Returns sigma_used, or the FALLBACK (also
    persisted, briefly) when candles are unavailable so we don't refetch a dead coin every signal."""
    res = compute(coin)
    if res is None:
        sigma = config.VOL_FALLBACK_SIGMA
        db.execute("INSERT OR REPLACE INTO coin_vol (coin,sigma,sigma_fast,sigma_slow,n,updated_at) "
                   "VALUES (?,?,?,?,?,?)", (coin, sigma, None, None, 0, now_iso()))
    else:
        sigma, fast, slow, n = res
        db.execute("INSERT OR REPLACE INTO coin_vol (coin,sigma,sigma_fast,sigma_slow,n,updated_at) "
                   "VALUES (?,?,?,?,?,?)", (coin, sigma, fast, slow, n, now_iso()))
    db.commit()
    return sigma


def load_all(db) -> dict:
    """Read the whole coin_vol table into {coin: sigma} for an in-memory read-cache at startup."""
    return {r[0]: r[1] for r in db.execute("SELECT coin, sigma FROM coin_vol").fetchall()}
