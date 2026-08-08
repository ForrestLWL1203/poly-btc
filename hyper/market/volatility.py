"""Per-coin realized volatility (daily σ) for risk-targeted copy sizing — REGIME-AWARE.

A single fixed-window σ lags a regime change: a coin calm for 30d then erupting keeps a low σ and
would get over-levered right as it turns dangerous. So we compute TWO horizons and take the MAX —
asymmetric on purpose: de-risk FAST when recent vol rises above the baseline, re-risk SLOWLY (only
once calm is sustained into the long window). σ lives in the coin_vol TABLE (one row/coin), refreshed
periodically off the signal hot path; the sizing code just reads the latest value.
"""
from __future__ import annotations

import time

from hyper import config
from hyper.util import f, now_iso
from . import price_path, rest


def _daily_range(candles: list):
    """Mean daily HIGH→LOW range = avg of (high-low)/low over `candles` — the typical INTRADAY swing a
    position actually rides (not close-to-close std, which understates the real bumpiness). None if too few."""
    rs = []
    for c in candles:
        h, l = f(c.get("h")), f(c.get("l"))
        if h > 0 and l > 0 and h >= l:
            rs.append((h - l) / l)
    if len(rs) < 3:
        return None
    return sum(rs) / len(rs)


def _cached_daily_candles(db, coin: str, asof_ms: int) -> tuple[list[dict] | None, dict]:
    """Incrementally fill and read the bounded shared daily-candle cache."""
    start_ms = int(asof_ms) - (int(config.VOL_SLOW_DAYS) + 2) * 86_400_000
    result = price_path.ensure_coins(
        db, [coin], start_ms, int(asof_ms), interval="1d", force_retry=True,
    )
    if result.get("failed") or result.get("deferred"):
        return None, result
    rows = db.execute(
        "SELECT open_time,close_time,open_px,high_px,low_px,close_px "
        "FROM coin_price_candle WHERE coin=? AND interval='1d' "
        "AND close_time>=? AND open_time<=? ORDER BY open_time",
        (coin, start_ms, int(asof_ms)),
    ).fetchall()
    return [
        {"t": row[0], "T": row[1], "o": row[2], "h": row[3], "l": row[4], "c": row[5]}
        for row in rows
    ], result


def compute_at(coin: str, asof_ms: int, *, db=None) -> dict:
    """Fetch one as-of volatility sample while distinguishing transport failure from a young market.

    Scanner qualification treats those outcomes differently: an API failure is a real data error, while a
    successful response with too little closed history may use the neutral 7% product default.  ``asof_ms``
    is fixed at generation start so a scan crossing UTC midnight cannot mix candle regimes.
    """
    start_ms = int(asof_ms) - (int(config.VOL_SLOW_DAYS) + 2) * 86_400_000
    if db is None:
        cs = rest.candle_snapshot_range(coin, "1d", start_ms, int(asof_ms))
    else:
        cs, _cache_result = _cached_daily_candles(db, coin, int(asof_ms))
    if cs is None:
        return {"status": "request_failed", "sigma": None, "fast": None, "slow": None, "n": 0}
    if not isinstance(cs, list):
        return {"status": "request_failed", "sigma": None, "fast": None, "slow": None, "n": 0}
    cs = sorted(cs, key=lambda c: c.get("t", 0))
    with_close_time = [c for c in cs if c.get("T") is not None]
    if with_close_time:
        cs = [c for c in cs if int(c.get("T") or 0) <= int(asof_ms)]
    elif cs:
        # Compatibility with fixtures/older API payloads lacking ``T``: the last daily candle is forming.
        cs = cs[:-1]
    if len(cs) < int(config.VOL_MIN_SAMPLES):
        return {"status": "insufficient_history", "sigma": None, "fast": None, "slow": None, "n": len(cs)}
    slow = _daily_range(cs)
    if slow is None:
        return {"status": "insufficient_history", "sigma": None, "fast": None, "slow": None, "n": len(cs)}
    fast = _daily_range(cs[-config.VOL_FAST_DAYS:]) or slow
    return {"status": "real", "sigma": max(fast, slow), "fast": fast, "slow": slow, "n": len(cs)}


def compute(coin: str):
    """Compatibility wrapper returning the historical tuple/None API."""
    sample = compute_at(coin, int(time.time() * 1000))
    if sample["status"] != "real":
        return None
    return sample["sigma"], sample["fast"], sample["slow"], sample["n"]


def _market_fields(asset_ctx):
    if not isinstance(asset_ctx, dict):
        return None, None, None, None, None, None, None
    day_ntl_vlm = f(asset_ctx.get("dayNtlVlm"))
    open_interest = f(asset_ctx.get("openInterest"))
    mark_px = f(asset_ctx.get("markPx")) or f(asset_ctx.get("oraclePx")) or f(asset_ctx.get("midPx"))
    oi_notional = open_interest * mark_px if open_interest > 0 and mark_px > 0 else None
    max_leverage = f(asset_ctx.get("universe_maxLeverage")) or None
    stamp = now_iso()
    return (
        day_ntl_vlm, open_interest, mark_px or None, oi_notional, stamp,
        max_leverage, stamp if max_leverage is not None else None,
    )


def _upsert_sample(
    db, coin: str, sigma: float, fast, slow, n: int,
    day_ntl_vlm, open_interest, mark_px, oi_notional, market_ctx_updated_at,
    max_leverage, margin_meta_updated_at,
):
    """Persist volatility and refresh market metadata without erasing good evidence.

    Market context is a separate REST surface from candles. A transient/partial
    context response must not turn a previously proven venue leverage cap back
    into NULL, especially for HIP-3 markets whose configured tier cap may be
    higher than the exchange permits.
    """
    db.execute(
        "INSERT INTO coin_vol "
        "(coin,sigma,sigma_fast,sigma_slow,n,day_ntl_vlm,open_interest,mark_px,oi_notional,"
        "market_ctx_updated_at,max_leverage,margin_meta_updated_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(coin) DO UPDATE SET "
        "sigma=excluded.sigma,sigma_fast=excluded.sigma_fast,sigma_slow=excluded.sigma_slow,"
        "n=excluded.n,day_ntl_vlm=COALESCE(excluded.day_ntl_vlm,coin_vol.day_ntl_vlm),"
        "open_interest=COALESCE(excluded.open_interest,coin_vol.open_interest),"
        "mark_px=COALESCE(excluded.mark_px,coin_vol.mark_px),"
        "oi_notional=COALESCE(excluded.oi_notional,coin_vol.oi_notional),"
        "market_ctx_updated_at=COALESCE(excluded.market_ctx_updated_at,coin_vol.market_ctx_updated_at),"
        "max_leverage=COALESCE(excluded.max_leverage,coin_vol.max_leverage),"
        "margin_meta_updated_at=COALESCE(excluded.margin_meta_updated_at,coin_vol.margin_meta_updated_at),"
        "updated_at=excluded.updated_at",
        (
            coin, sigma, fast, slow, n, day_ntl_vlm, open_interest, mark_px, oi_notional,
            market_ctx_updated_at, max_leverage, margin_meta_updated_at, now_iso(),
        ),
    )


def refresh(db, coin: str, asset_ctx=None):
    """Recompute coin's σ and upsert its coin_vol row. Returns sigma_used, or the FALLBACK (also
    persisted, briefly) when candles are unavailable so we don't refetch a dead coin every signal."""
    res = compute(coin)
    if asset_ctx is None and coin:
        asset_ctx = rest.asset_context(coin)
    (day_ntl_vlm, open_interest, mark_px, oi_notional, market_ctx_updated_at,
     max_leverage, margin_meta_updated_at) = _market_fields(asset_ctx)
    if res is None:
        sigma = config.VOL_FALLBACK_SIGMA
        _upsert_sample(
            db, coin, sigma, None, None, 0,
            day_ntl_vlm, open_interest, mark_px, oi_notional, market_ctx_updated_at,
            max_leverage, margin_meta_updated_at,
        )
    else:
        sigma, fast, slow, n = res
        _upsert_sample(
            db, coin, sigma, fast, slow, n,
            day_ntl_vlm, open_interest, mark_px, oi_notional, market_ctx_updated_at,
            max_leverage, margin_meta_updated_at,
        )
    db.commit()
    return sigma


def load_all(db) -> dict:
    """Read the whole coin_vol table into {coin: sigma} for an in-memory read-cache at startup."""
    # Scanner/market-context refreshes may legitimately create a row before any candle-derived sigma exists.
    # Such a placeholder is not a warm volatility value: keeping ``coin: None`` in the cache makes Observer's
    # lazy loader believe the market was already fetched and silently routes it through the 7% fallback tier.
    return {
        r[0]: r[1]
        for r in db.execute(
            "SELECT coin,sigma FROM coin_vol WHERE sigma IS NOT NULL AND sigma>0"
        ).fetchall()
    }
