"""Bounded shared OHLC cache for copy-replay path validation."""
from __future__ import annotations

import time

from . import rest
from .copy_data import normalize_copyable_fills

INTERVAL_MS = {"1m": 60_000, "15m": 15 * 60_000}
RETENTION_DAYS = {"1m": 4, "15m": 39}
BASE_INTERVAL = "15m"


def coins_for_fills(fills) -> list[str]:
    return sorted({row["coin"] for row in normalize_copyable_fills(fills) if row.get("coin")})


def prune(db, now_ms: int | None = None) -> int:
    now_ms = int(now_ms or time.time() * 1000)
    deleted = 0
    for interval, days in RETENTION_DAYS.items():
        cur = db.execute(
            "DELETE FROM coin_price_candle WHERE interval=? AND close_time<?",
            (interval, now_ms - days * 86_400_000),
        )
        deleted += max(0, int(cur.rowcount or 0))
    return deleted


def _upsert(db, coin: str, interval: str, rows, fetched_at: int) -> int:
    values = []
    step = INTERVAL_MS[interval]
    for row in rows or []:
        try:
            start = int(row.get("t"))
            end = int(row.get("T") or (start + step - 1))
            o, h, lo, c = (float(row[k]) for k in ("o", "h", "l", "c"))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if start <= 0 or min(o, h, lo, c) <= 0:
            continue
        values.append((coin, interval, start, end, o, h, lo, c, fetched_at))
    if values:
        db.executemany(
            "INSERT INTO coin_price_candle "
            "(coin,interval,open_time,close_time,open_px,high_px,low_px,close_px,fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(coin,interval,open_time) DO UPDATE SET "
            "close_time=excluded.close_time,open_px=excluded.open_px,high_px=excluded.high_px,"
            "low_px=excluded.low_px,close_px=excluded.close_px,fetched_at=excluded.fetched_at",
            values,
        )
    return len(values)


def ensure(db, fills, start_ms: int, end_ms: int, *, interval: str = BASE_INTERVAL) -> dict:
    """Incrementally ensure a shared path for the markets present in fills."""
    step = INTERVAL_MS[interval]
    now_ms = int(time.time() * 1000)
    end_ms = min(int(end_ms), now_ms)
    start_ms = max(int(start_ms), now_ms - RETENTION_DAYS[interval] * 86_400_000)
    coins = coins_for_fills(fills)
    fetched, failed, deferred = 0, [], 0
    for coin in coins:
        state = db.execute(
            "SELECT status,error_count,retry_after FROM coin_price_path_state "
            "WHERE coin=? AND interval=?", (coin, interval),
        ).fetchone()
        if state and state[0] == "failed" and int(state[2] or 0) > now_ms:
            deferred += 1
            continue
        row = db.execute(
            "SELECT MAX(close_time),MAX(fetched_at) FROM coin_price_candle WHERE coin=? AND interval=?",
            (coin, interval),
        ).fetchone()
        latest_close = int((row[0] if row else 0) or 0)
        latest_fetch = int((row[1] if row else 0) or 0)
        # A scanner finalization and its generation-bound tuner commonly run back-to-back. Reuse the
        # just-fetched forming candle instead of issuing one request per market twice.
        if latest_close >= end_ms - 2 * step and latest_fetch >= now_ms - step // 2:
            continue
        cursor = max(start_ms, latest_close + 1)
        # Refresh the forming candle and bridge a possible boundary gap.
        cursor = max(start_ms, cursor - step)
        if cursor >= end_ms:
            continue
        candles = rest.candle_snapshot_range(coin, interval, cursor, end_ms)
        if not isinstance(candles, list):
            failed.append(coin)
            error_count = int((state[1] if state else 0) or 0) + 1
            retry_ms = min(24 * 3_600_000, 3_600_000 * (2 ** min(4, error_count - 1)))
            db.execute(
                "INSERT INTO coin_price_path_state "
                "(coin,interval,status,error_count,last_attempt,retry_after) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(coin,interval) DO UPDATE SET status=excluded.status,"
                "error_count=excluded.error_count,last_attempt=excluded.last_attempt,"
                "retry_after=excluded.retry_after",
                (coin, interval, "failed", error_count, now_ms, now_ms + retry_ms),
            )
            continue
        fetched += _upsert(db, coin, interval, candles, now_ms)
        db.execute(
            "INSERT INTO coin_price_path_state "
            "(coin,interval,status,error_count,last_attempt,retry_after) VALUES (?,?,?,?,?,0) "
            "ON CONFLICT(coin,interval) DO UPDATE SET status='ok',error_count=0,"
            "last_attempt=excluded.last_attempt,retry_after=0",
            (coin, interval, "ok", 0, now_ms),
        )
    deleted = prune(db, now_ms)
    db.commit()
    return {"coins": len(coins), "fetched": fetched, "failed": failed,
            "deferred": deferred, "deleted": deleted}


def load(db, fills, start_ms: int, end_ms: int, *, interval: str = BASE_INTERVAL) -> list[dict]:
    coins = coins_for_fills(fills)
    if not coins:
        return []
    marks = ",".join("?" for _ in coins)
    rows = db.execute(
        f"SELECT coin,open_time,close_time,low_px,high_px,close_px FROM coin_price_candle "
        f"WHERE interval=? AND coin IN ({marks}) AND close_time>=? AND open_time<=? "
        "ORDER BY open_time,coin",
        (interval, *coins, int(start_ms), int(end_ms)),
    ).fetchall()
    return [{"coin": r[0], "time": r[2], "open_time": r[1], "close_time": r[2],
             "low": r[3], "high": r[4], "close": r[5], "interval": interval} for r in rows]


def coverage(db, fills, start_ms: int, end_ms: int, *, interval: str = BASE_INTERVAL) -> dict:
    """Coin/time coverage for the requested replay window (strict, gap-aware baseline)."""
    normalized = normalize_copyable_fills(fills)
    coins = sorted({row["coin"] for row in normalized})
    step = INTERVAL_MS[interval]
    ranges = {}
    for coin in coins:
        times = [int(row.get("time") or 0) for row in normalized if row.get("coin") == coin]
        # Include one boundary candle around the observed fill span. Completely silent periods outside a
        # market's first/last replay observation are irrelevant and must not dilute portfolio coverage.
        lo = max(int(start_ms), min(times) - step)
        hi = min(int(end_ms), max(times) + step)
        ranges[coin] = (lo, hi, max(1, (hi - lo + step - 1) // step))
    expected = sum(item[2] for item in ranges.values())
    if not expected:
        return {"coverage": 1.0, "expected": 0, "observed": 0, "missingCoins": []}
    observed, missing = 0, []
    for coin in coins:
        lo, hi, expected_per_coin = ranges[coin]
        row = db.execute(
            "SELECT COUNT(DISTINCT open_time),MIN(open_time),MAX(close_time) FROM coin_price_candle "
            "WHERE coin=? AND interval=? AND close_time>=? AND open_time<=?",
            (coin, interval, lo, hi),
        ).fetchone()
        count = min(expected_per_coin, int((row[0] if row else 0) or 0))
        observed += count
        if count < expected_per_coin * .95:
            missing.append(coin)
    return {"coverage": min(1.0, observed / expected), "expected": expected,
            "observed": observed, "missingCoins": missing}
