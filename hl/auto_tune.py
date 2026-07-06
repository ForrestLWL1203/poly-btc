"""Post-scan portfolio auto-tuning for copy-trading sizing.

The tuner deliberately has a narrow blast radius: it only adjusts the three
first-open margin percentages. Leverage caps, per-coin caps, deploy cap and stop
rules remain operator-owned risk limits.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from typing import Iterable

from . import config, params
from .copy_backtest import run_backtest
from .fills import is_spot
from .util import now_iso

MARGIN_KEYS = ("STABLE_MARGIN_PCT", "MID_MARGIN_PCT", "HIGH_MARGIN_PCT")
CAPACITY_SKIP_KEYS = ("skip_coin_full", "skip_no_cash", "skip_deploy_cap", "skip_margin_too_small")


def _json_load(raw, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _state_get(db, key, fallback=None):
    row = db.execute("SELECT value FROM auto_tune_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return fallback
    return row[0] if not isinstance(row, sqlite3.Row) else row["value"]


def _state_set(db, key, value):
    stamp = now_iso()
    db.execute(
        "INSERT INTO auto_tune_state (key,value,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (key, json.dumps(value, sort_keys=True), stamp),
    )


def _same_margins(a: dict, b: dict, eps: float = 1e-9) -> bool:
    try:
        return all(abs(float(a[k]) - float(b[k])) <= eps for k in MARGIN_KEYS)
    except (KeyError, TypeError, ValueError):
        return False


def store_margin_state(db, base: dict, last_auto: dict) -> None:
    """Persist manual baseline and last auto-applied margins in engine units."""
    _state_set(db, "margin_base", {k: float(base[k]) for k in MARGIN_KEYS})
    _state_set(db, "margin_last_auto", {k: float(last_auto[k]) for k in MARGIN_KEYS})
    db.commit()


def resolve_margin_baseline(db, current: dict) -> tuple[dict, bool]:
    """Return the manual baseline for tuning and whether current params reset it.

    If current values still equal the last auto-applied values, keep tuning around
    the stored manual baseline. If an operator changed any margin manually, treat
    the new current values as the new baseline to avoid compounding.
    """
    current = {k: float(current[k]) for k in MARGIN_KEYS}
    base = _json_load(_state_get(db, "margin_base"), None)
    last = _json_load(_state_get(db, "margin_last_auto"), None)
    if not base or not last or not _same_margins(current, last):
        return current, True
    return {k: float(base[k]) for k in MARGIN_KEYS}, False


def _capacity_skips(result: dict) -> int:
    skips = result.get("skip_reasons") or {}
    return int(sum(skips.get(k, 0) or 0 for k in CAPACITY_SKIP_KEYS))


def _min_closed_for_days(days: int) -> int:
    base_days = int(getattr(config, "COPY_BT_DAYS", 30) or 30)
    base_min = int(getattr(config, "COPY_BT_MIN_CLOSED", 0) or 0)
    if days >= base_days or base_days <= 0:
        return base_min
    return max(1, int(math.ceil(base_min * days / base_days)))


def _enough_sample(result: dict, days: int) -> bool:
    return int(result.get("closed_n") or 0) >= _min_closed_for_days(days)


def _result_pnl(result: dict) -> float:
    return float(result.get("copy_net_pnl") or 0.0)


def _candidate_score(candidate: dict) -> float:
    windows = candidate.get("windows") or {}
    return (
        _result_pnl(windows.get(30, {}))
        + 0.50 * _result_pnl(windows.get(14, {}))
        + 0.25 * _result_pnl(windows.get(7, {}))
    )


def _candidate_valid(candidate: dict, baseline: dict) -> bool:
    windows = candidate.get("windows") or {}
    base_windows = baseline.get("windows") or {}
    base30 = base_windows.get(30, {})
    result30 = windows.get(30, {})
    if not result30:
        return False

    min_open_fit = max(
        float(getattr(config, "AUTO_TUNE_MARGIN_MIN_OPEN_FIT", 0.75)),
        float(base30.get("open_fill_rate") or 1.0) - float(getattr(config, "AUTO_TUNE_MARGIN_MAX_OPEN_FIT_DROP", 0.03)),
    )
    if float(result30.get("open_fill_rate") or 0.0) < min_open_fit:
        return False
    if int(result30.get("liquidations") or 0) > int(base30.get("liquidations") or 0):
        return False

    base_skips = _capacity_skips(base30)
    skip_allow = max(2, int((base30.get("target_open_events") or 0) * float(getattr(config, "AUTO_TUNE_MARGIN_CAP_SKIP_FRAC", 0.05))))
    if _capacity_skips(result30) > base_skips + skip_allow:
        return False

    for days, result in windows.items():
        if _enough_sample(result, int(days)) and _result_pnl(result) <= 0:
            return False
    return True


def choose_margin_candidate(candidates: list[dict], baseline: dict) -> dict:
    """Pick the highest recency-weighted PnL candidate that preserves copyability."""
    valid = [c for c in candidates if _candidate_valid(c, baseline)]
    if not valid:
        return baseline
    best = max(valid, key=lambda c: (_candidate_score(c), -abs(float(c.get("mult") or 1.0) - 1.0)))
    return best if _candidate_score(best) >= _candidate_score(baseline) else baseline


def _load_sigmas(db) -> dict:
    try:
        return {coin: sigma for coin, sigma in db.execute("SELECT coin,sigma FROM coin_vol WHERE sigma IS NOT NULL")}
    except sqlite3.Error:
        return {}


def _load_followed_wallets(db, follow: dict) -> list[str]:
    line = float(follow.get("MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE)
    rows = db.execute(
        "SELECT w.addr FROM watchlist w LEFT JOIN target_controls c ON c.addr=w.addr "
        "WHERE COALESCE(c.enabled,1)=1 AND w.score>=? ORDER BY w.rank LIMIT ?",
        (line, int(config.MAX_TARGETS)),
    ).fetchall()
    return [(r[0] if not isinstance(r, sqlite3.Row) else r["addr"]).lower() for r in rows]


def _load_portfolio_fills(db, addrs: Iterable[str], start_ms: int) -> list[dict]:
    addrs = [(a or "").lower() for a in addrs if a]
    if not addrs:
        return []
    qs = ",".join("?" for _ in addrs)
    rows = db.execute(
        f"SELECT addr,fill_json FROM candidate_fills WHERE addr IN ({qs}) AND time>=? ORDER BY time",
        (*addrs, int(start_ms or 0)),
    ).fetchall()
    out = []
    for row in rows:
        addr = row[0] if not isinstance(row, sqlite3.Row) else row["addr"]
        raw = row[1] if not isinstance(row, sqlite3.Row) else row["fill_json"]
        try:
            fill = json.loads(raw)
        except (TypeError, ValueError):
            continue
        coin = fill.get("coin") or ""
        if not coin or is_spot(coin):
            continue
        fill["user"] = (addr or "").lower()
        out.append(fill)
    out.sort(key=lambda x: int(x.get("time") or 0))
    return out


def follow_overrides_for_margin_candidate(follow: dict, margins: dict) -> dict:
    out = dict(follow)
    for key in MARGIN_KEYS:
        min_key = key.replace("_MARGIN_PCT", "_MARGIN_MIN_PCT")
        floor = float(follow.get(min_key) or 0.0)
        out[key] = max(floor, float(margins[key]))
    if "SMART_ADD" in out:
        out["ADD_STRATEGY"] = "smart" if out["SMART_ADD"] else "hardcap"
    return out


def evaluate_margin_candidate(db, addrs: list[str], follow: dict, base: dict, mult: float,
                              sigmas: dict | None = None, now_ms: int | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    margins = {k: float(base[k]) * float(mult) for k in MARGIN_KEYS}
    overrides = follow_overrides_for_margin_candidate(follow, margins)
    margins = {k: overrides[k] for k in MARGIN_KEYS}
    sigmas = sigmas if sigmas is not None else _load_sigmas(db)
    windows = {}
    for days in getattr(config, "AUTO_TUNE_MARGIN_DAYS", (30, 14, 7)):
        start_ms = now_ms - int(days) * 86400_000
        fills = _load_portfolio_fills(db, addrs, start_ms)
        result = run_backtest("portfolio", fills, sigmas=sigmas, overrides=overrides)
        result["fills"] = len(fills)
        windows[int(days)] = result
    return {"mult": float(mult), "margins": margins, "windows": windows, "score": None}


def _write_margin_params(db, margins: dict) -> None:
    stamp = now_iso()
    for key in MARGIN_KEYS:
        db.execute("UPDATE params SET value=?,updated_at=? WHERE key=?", (str(float(margins[key]) * 100.0), stamp, key))


def _record_run(db, source: str, stamp: str, selected: dict | None, applied: bool, followed_n: int,
                baseline: dict, result: dict) -> None:
    db.execute(
        "INSERT INTO auto_tune_runs (source,stamp,selected_mult,applied,followed_n,baseline_json,result_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            source,
            stamp,
            float(selected.get("mult")) if selected else None,
            1 if applied else 0,
            followed_n,
            json.dumps(baseline, sort_keys=True),
            json.dumps(result, sort_keys=True, default=float),
            now_iso(),
        ),
    )


def _enqueue_reload(db, source: str) -> None:
    db.execute(
        "INSERT INTO commands (type,payload_json,owner,status,created_at) VALUES (?,?,?,'pending',?)",
        ("reload_params", json.dumps({"by": "auto_tune_margin", "source": source}), "auto_tune", now_iso()),
    )


def _compact_backtest(result: dict) -> dict:
    keys = (
        "closed_n", "open_n", "wins", "stops", "liquidations", "copy_win_rate",
        "copy_net_pnl", "closed_net_pnl", "unrealized_pnl", "fee_drag",
        "target_open_events", "opened_n", "open_fill_rate", "target_adds",
        "followed_adds", "missed_adds", "missed_add_rate", "add_dependency",
        "target_peak_concurrent", "copy_peak_concurrent", "max_concurrent_fit",
        "capacity_open_fit", "fills",
    )
    out = {k: result.get(k) for k in keys if k in result}
    out["skip_reasons"] = result.get("skip_reasons") or {}
    return out


def _compact_candidate(candidate: dict) -> dict:
    return {
        "mult": candidate.get("mult"),
        "margins": candidate.get("margins"),
        "score": _candidate_score(candidate),
        "windows": {str(days): _compact_backtest(result) for days, result in (candidate.get("windows") or {}).items()},
    }


def maybe_tune_margins(db, source: str = "scan", stamp: str | None = None, dry_run: bool = False) -> dict:
    """Run the post-scan margin tuner. Returns a compact audit dict."""
    stamp = stamp or now_iso()
    params.seed_params(db)
    follow = params.load_follow(db)
    if not follow.get("AUTO_TUNE_MARGIN_ENABLE", getattr(config, "AUTO_TUNE_MARGIN_ENABLE", True)):
        result = {"status": "disabled", "applied": False}
        _record_run(db, source, stamp, None, False, 0, {}, result)
        db.commit()
        return result

    addrs = _load_followed_wallets(db, follow)
    if len(addrs) < int(getattr(config, "AUTO_TUNE_MARGIN_MIN_FOLLOWED", 1)):
        result = {"status": "no_followed_wallets", "applied": False, "followed_n": len(addrs)}
        _record_run(db, source, stamp, None, False, len(addrs), {}, result)
        db.commit()
        return result

    current = {k: float(follow[k]) for k in MARGIN_KEYS}
    base, baseline_reset = resolve_margin_baseline(db, current)
    sigmas = _load_sigmas(db)
    now_ms = int(time.time() * 1000)
    candidates = [
        evaluate_margin_candidate(db, addrs, follow, base, float(mult), sigmas=sigmas, now_ms=now_ms)
        for mult in getattr(config, "AUTO_TUNE_MARGIN_MULTS", (0.75, 1.0, 1.25, 1.5))
    ]
    baseline = next((c for c in candidates if abs(float(c.get("mult") or 0) - 1.0) <= 1e-9), candidates[0])
    selected = choose_margin_candidate(candidates, baseline)
    selected_margins = selected.get("margins") or base
    applied = False
    if not dry_run and not _same_margins(current, selected_margins):
        _write_margin_params(db, selected_margins)
        _enqueue_reload(db, source)
        applied = True
    if not dry_run:
        store_margin_state(db, base, selected_margins)

    result = {
        "status": "ok",
        "applied": applied,
        "baseline_reset": baseline_reset,
        "followed_n": len(addrs),
        "selected_mult": selected.get("mult"),
        "margins": selected_margins,
        "candidates": [_compact_candidate(c) for c in candidates],
    }
    _record_run(db, source, stamp, selected, applied, len(addrs), base, result)
    db.commit()
    return result
