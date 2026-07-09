"""Post-scan portfolio auto-tuning for copy-trading sizing.

The tuner adjusts the operator-approved sizing surface: first-open margin upper
bounds, tier leverage caps, the deployment level where new opens begin to
shrink, and the smart-add core knobs. Lower margin bounds, per-coin caps, max
deployment cap, and stop rules remain operator-owned risk limits.
"""
from __future__ import annotations

import itertools
import json
import math
import sqlite3
import time
from typing import Iterable

from . import config, params
from .copy_backtest import run_backtest
from .fills import is_spot
from .sector import parse_json_obj, policy_allows_coin
from .util import now_iso

MARGIN_KEYS = ("STABLE_MARGIN_PCT", "MID_MARGIN_PCT", "HIGH_MARGIN_PCT")
LEV_KEYS = ("STABLE_LEV_CAP", "MID_LEV_CAP", "HIGH_LEV_CAP")
DEPLOY_KEYS = ("DEPLOY_FULL_PCT",)
TUNE_KEYS = MARGIN_KEYS + LEV_KEYS + DEPLOY_KEYS
ADD_TUNE_KEYS = ("ADD_GAP_K", "ADD_GAP_SHRINK_G", "ADD_MAX_HARD")
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


def _same_values(a: dict, b: dict, keys: tuple[str, ...], eps: float = 1e-9) -> bool:
    try:
        return all(abs(float(a[k]) - float(b[k])) <= eps for k in keys)
    except (KeyError, TypeError, ValueError):
        return False


def _same_margins(a: dict, b: dict, eps: float = 1e-9) -> bool:
    return _same_values(a, b, MARGIN_KEYS, eps)


def _same_tune_values(a: dict, b: dict, eps: float = 1e-9) -> bool:
    return _same_values(a, b, TUNE_KEYS, eps)


def _same_add_values(a: dict, b: dict, eps: float = 1e-9) -> bool:
    return _same_values(a, b, ADD_TUNE_KEYS, eps)


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


def store_tune_state(db, base: dict, last_auto: dict) -> None:
    """Persist manual baseline and last auto-applied sizing surface in engine units."""
    _state_set(db, "tune_base", {k: float(base[k]) for k in TUNE_KEYS})
    _state_set(db, "tune_last_auto", {k: float(last_auto[k]) for k in TUNE_KEYS})
    db.commit()


def resolve_tune_baseline(db, current: dict) -> tuple[dict, bool]:
    current = {k: float(current[k]) for k in TUNE_KEYS}
    base = _json_load(_state_get(db, "tune_base"), None)
    last = _json_load(_state_get(db, "tune_last_auto"), None)
    if not base or not last or not _same_tune_values(current, last):
        return current, True
    return {k: float(base[k]) for k in TUNE_KEYS}, False


def store_add_state(db, base: dict, last_auto: dict) -> None:
    _state_set(db, "add_base", {k: float(base[k]) for k in ADD_TUNE_KEYS})
    _state_set(db, "add_last_auto", {k: float(last_auto[k]) for k in ADD_TUNE_KEYS})
    db.commit()


def resolve_add_baseline(db, current: dict) -> tuple[dict, bool]:
    current = {k: float(current[k]) for k in ADD_TUNE_KEYS}
    base = _json_load(_state_get(db, "add_base"), None)
    last = _json_load(_state_get(db, "add_last_auto"), None)
    if not base or not last or not _same_add_values(current, last):
        return current, True
    return {k: float(base[k]) for k in ADD_TUNE_KEYS}, False


def _capacity_skips(result: dict) -> int:
    skips = result.get("skip_reasons") or {}
    return int(sum(skips.get(k, 0) or 0 for k in CAPACITY_SKIP_KEYS))


def _min_closed_for_days(days: int) -> int:
    base_days = int(getattr(config, "COPY_BT_DAYS", 30) or 30)
    base_min = int(getattr(config, "COPY_BT_MIN_CLOSED", 0) or 0)
    if int(days) <= 7 and hasattr(config, "COPY_BT_MIN_CLOSED_7D"):
        return int(getattr(config, "COPY_BT_MIN_CLOSED_7D") or base_min)
    if int(days) <= 14 and hasattr(config, "COPY_BT_MIN_CLOSED_14D"):
        return int(getattr(config, "COPY_BT_MIN_CLOSED_14D") or base_min)
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
        _result_pnl(windows.get(14, {}))
        + 0.50 * _result_pnl(windows.get(7, {}))
        + 0.25 * _result_pnl(windows.get(30, {}))
    )


def _capacity_fit(result: dict) -> float:
    val = result.get("capacity_open_fit")
    if val is not None:
        return float(val)
    return float(result.get("open_fill_rate") or 0.0)


def _candidate_valid(candidate: dict, baseline: dict) -> bool:
    windows = candidate.get("windows") or {}
    base_windows = baseline.get("windows") or {}
    primary_days = 14 if 14 in windows and 14 in base_windows else max(windows) if windows else 0
    base_primary = base_windows.get(primary_days, {})
    result_primary = windows.get(primary_days, {})
    if not result_primary:
        return False

    min_open_fit = max(
        float(getattr(config, "AUTO_TUNE_MARGIN_MIN_OPEN_FIT", 0.75)),
        _capacity_fit(base_primary) - float(getattr(config, "AUTO_TUNE_MARGIN_MAX_OPEN_FIT_DROP", 0.03)),
    )
    if _capacity_fit(result_primary) < min_open_fit:
        return False
    if int(result_primary.get("liquidations") or 0) > int(base_primary.get("liquidations") or 0):
        return False

    base_skips = _capacity_skips(base_primary)
    skip_allow = max(2, int((base_primary.get("target_open_events") or 0) * float(getattr(config, "AUTO_TUNE_MARGIN_CAP_SKIP_FRAC", 0.05))))
    if _capacity_skips(result_primary) > base_skips + skip_allow:
        return False

    for days, result in windows.items():
        if _enough_sample(result, int(days)) and _result_pnl(result) <= 0:
            return False
    return True


def _candidate_distance(candidate: dict, baseline: dict) -> float:
    params_ = candidate.get("params") or {}
    base_params = baseline.get("params") or {}
    if not params_ or not base_params:
        return abs(float(candidate.get("mult") or 1.0) - float(baseline.get("mult") or 1.0))
    keys = tuple(candidate.get("distance_keys") or baseline.get("distance_keys") or TUNE_KEYS)
    return sum(abs(float(params_.get(k, 0.0)) - float(base_params.get(k, 0.0))) for k in keys)


def _candidate_rank_key(candidate: dict, baseline: dict) -> tuple:
    windows = candidate.get("windows") or {}
    return (
        _result_pnl(windows.get(14, {})),
        _result_pnl(windows.get(7, {})),
        _result_pnl(windows.get(30, {})),
        -_candidate_distance(candidate, baseline),
    )


def _portfolio_line_score(candidate: dict) -> float:
    windows = candidate.get("windows") or {}
    return (
        _result_pnl(windows.get(14, {}))
        + 0.50 * _result_pnl(windows.get(7, {}))
        + 0.25 * _result_pnl(windows.get(30, {}))
    )


def _inclusive_follow_line(score: float, min_score: float) -> float:
    score = float(score or 0.0)
    return max(float(min_score), score - 1e-9 if score > min_score else score)


def _compact_follow_line_candidate(candidate: dict) -> dict:
    return {
        "n": candidate.get("n"),
        "line": candidate.get("line"),
        "score_floor": candidate.get("score_floor"),
        "addrs": candidate.get("addrs") or [],
        "score": _portfolio_line_score(candidate),
        "windows": {
            str(days): _compact_backtest(result)
            for days, result in (candidate.get("windows") or {}).items()
        },
    }


def choose_margin_candidate(candidates: list[dict], baseline: dict) -> dict:
    """Pick the best 14d-led PnL candidate that preserves copyability."""
    valid = [c for c in candidates if _candidate_valid(c, baseline)]
    if not valid:
        return baseline
    best = max(valid, key=lambda c: _candidate_rank_key(c, baseline))
    return best if _candidate_rank_key(best, baseline) >= _candidate_rank_key(baseline, baseline) else baseline


def _load_sigmas(db) -> dict:
    try:
        return {coin: sigma for coin, sigma in db.execute("SELECT coin,sigma FROM coin_vol WHERE sigma IS NOT NULL")}
    except sqlite3.Error:
        return {}


def _load_market_ctx(db) -> dict:
    try:
        rows = db.execute(
            "SELECT coin,day_ntl_vlm,oi_notional FROM coin_vol "
            "WHERE day_ntl_vlm IS NOT NULL OR oi_notional IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {r[0]: {"day_ntl_vlm": r[1], "oi_notional": r[2]} for r in rows}


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
    policies = {
        (r[0] or "").lower(): parse_json_obj(r[1])
        for r in db.execute(f"SELECT addr,sector_policy_json FROM watchlist WHERE addr IN ({qs})", addrs).fetchall()
    }
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
        if not policy_allows_coin(policies.get((addr or "").lower()), coin, default=True):
            continue
        fill["user"] = (addr or "").lower()
        out.append(fill)
    out.sort(key=lambda x: int(x.get("time") or 0))
    return out


def _portfolio_fill_json_bytes(db, addrs: Iterable[str], start_ms: int) -> int:
    addrs = [(a or "").lower() for a in addrs if a]
    if not addrs:
        return 0
    qs = ",".join("?" for _ in addrs)
    try:
        row = db.execute(
            f"SELECT COALESCE(SUM(LENGTH(fill_json)),0) FROM candidate_fills WHERE addr IN ({qs}) AND time>=?",
            (*addrs, int(start_ms or 0)),
        ).fetchone()
        return int((row[0] if row else 0) or 0)
    except sqlite3.Error:
        return 0


def _tune_days() -> list[int]:
    out = []
    for days in getattr(config, "AUTO_TUNE_MARGIN_DAYS", (30, 14, 7)):
        try:
            val = int(days)
        except (TypeError, ValueError):
            continue
        if val > 0 and val not in out:
            out.append(val)
    return out or [30]


def _portfolio_window_fills(db, addrs: list[str], now_ms: int) -> dict[int, list[dict]] | None:
    days = _tune_days()
    max_days = max(days)
    start_ms = now_ms - max_days * 86400_000
    max_bytes = int(getattr(config, "AUTO_TUNE_FILL_CACHE_MAX_BYTES", 64 * 1024 * 1024) or 0)
    if max_bytes > 0 and _portfolio_fill_json_bytes(db, addrs, start_ms) > max_bytes:
        return None
    fills = _load_portfolio_fills(db, addrs, start_ms)
    windows = {}
    for day in days:
        start_ms = now_ms - day * 86400_000
        windows[day] = [x for x in fills if int(x.get("time") or 0) >= start_ms]
    return windows


def _filter_window_fills_by_addr(window_fills: dict[int, list[dict]], addrs: Iterable[str]) -> dict[int, list[dict]]:
    allowed = {(a or "").lower() for a in addrs if a}
    return {
        int(days): [x for x in fills if (x.get("user") or "").lower() in allowed]
        for days, fills in (window_fills or {}).items()
    }


def _follow_line_candidate_valid(candidate: dict) -> bool:
    windows = candidate.get("windows") or {}
    primary = windows.get(14) or (windows.get(max(windows)) if windows else None)
    if not primary:
        return False
    min_open_fit = float(getattr(config, "AUTO_FOLLOW_PORTFOLIO_MIN_OPEN_FIT", 0.70))
    if _capacity_fit(primary) < min_open_fit:
        return False
    for days, result in windows.items():
        if not _enough_sample(result, int(days)):
            return False
        if _result_pnl(result) <= 0:
            return False
    return True


def _recent_pnl_cliff(prev: dict, cur: dict) -> bool:
    prev_windows = prev.get("windows") or {}
    cur_windows = cur.get("windows") or {}
    min_abs = float(getattr(config, "AUTO_FOLLOW_PORTFOLIO_MAX_RECENT_DROP_ABS", 250.0))
    min_rel = float(getattr(config, "AUTO_FOLLOW_PORTFOLIO_MAX_RECENT_DROP_REL", 0.25))
    for days in (14, 7):
        prev_pnl = _result_pnl(prev_windows.get(days, {}))
        cur_pnl = _result_pnl(cur_windows.get(days, {}))
        drop = prev_pnl - cur_pnl
        if drop <= 0:
            continue
        hurdle = max(min_abs, abs(prev_pnl) * min_rel)
        if drop >= hurdle:
            return True
    return False


def _cap_before_recent_cliff(candidates: list[dict]) -> tuple[list[dict], dict | None]:
    ordered = sorted(candidates, key=lambda c: int(c.get("n") or 0))
    kept = []
    for c in ordered:
        if kept and _recent_pnl_cliff(kept[-1], c):
            return kept, c
        kept.append(c)
    return kept, None


def _follow_line_candidate_key(candidate: dict, target_n: int) -> tuple:
    windows = candidate.get("windows") or {}
    primary = windows.get(14) or {}
    return (
        _portfolio_line_score(candidate),
        _result_pnl(windows.get(14, {})),
        _result_pnl(windows.get(7, {})),
        _result_pnl(windows.get(30, {})),
        -int(primary.get("liquidations") or 0),
        _capacity_fit(primary),
        -abs(int(candidate.get("n") or 0) - int(target_n)),
    )


def _meaningfully_better(candidate: dict, reference: dict) -> bool:
    gain = _portfolio_line_score(candidate) - _portfolio_line_score(reference)
    min_abs = float(getattr(config, "AUTO_FOLLOW_PORTFOLIO_MIN_ABS_GAIN", 250.0))
    min_rel = float(getattr(config, "AUTO_FOLLOW_PORTFOLIO_MIN_REL_GAIN", 0.08))
    hurdle = max(min_abs, abs(_portfolio_line_score(reference)) * min_rel)
    return gain >= hurdle


def choose_follow_line_by_portfolio(db, ranked: list[dict], follow: dict | None = None,
                                    stamp: str | None = None) -> dict:
    """Choose MIN_FOLLOW_SCORE by replaying ranked top-N prefixes as one shared copy account.

    This is intentionally a narrow selector: it tunes only the wallet-count boundary using
    current follow params. Sizing/add grids still run afterwards on the selected final set.
    If cached fills are too large/missing, or no prefix has enough positive evidence, caller
    should fall back to the score-cliff/capacity heuristic.
    """
    if not getattr(config, "AUTO_FOLLOW_PORTFOLIO_ENABLE", True):
        return {"status": "disabled", "reason": "portfolio_selector_disabled"}

    min_score = float(getattr(config, "AUTO_FOLLOW_MIN_SCORE", 0.60))
    rows = [
        r for r in ranked
        if float(r.get("follow_score", r.get("score")) or 0.0) >= min_score
        and (r.get("follow_eligibility") or {}).get("eligible", True)
    ]
    if not rows:
        return {"status": "fallback", "reason": "no_wallet_above_floor"}

    available = len(rows)
    min_n = max(1, min(int(getattr(config, "AUTO_FOLLOW_MIN_N", 7)), available))
    max_n = max(min_n, min(int(getattr(config, "AUTO_FOLLOW_MAX_N", 20)), int(config.MAX_TARGETS), available))
    target_n = max(min_n, min(int(getattr(config, "AUTO_FOLLOW_TARGET_N", 16)), max_n))
    max_addrs = [(r.get("addr") or "").lower() for r in rows[:max_n] if r.get("addr")]
    if not max_addrs:
        return {"status": "fallback", "reason": "no_candidate_addrs"}

    now_ms = int(time.time() * 1000)
    window_fills = _portfolio_window_fills(db, max_addrs, now_ms)
    if window_fills is None:
        return {"status": "fallback", "reason": "fill_cache_guard"}
    if not any(window_fills.values()):
        return {"status": "fallback", "reason": "no_cached_fills"}

    follow = dict(follow or params.load_follow(db))
    if "SMART_ADD" in follow:
        follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
    sigmas = _load_sigmas(db)
    candidates = []
    for n in range(min_n, max_n + 1):
        addrs = max_addrs[:n]
        fills_by_window = _filter_window_fills_by_addr(window_fills, addrs)
        windows = _candidate_windows(db, addrs, sigmas, follow, now_ms, window_fills=fills_by_window)
        candidates.append({
            "n": n,
            "line": _inclusive_follow_line(rows[n - 1].get("follow_score", rows[n - 1].get("score")), min_score),
            "addrs": addrs,
            "score_floor": float(rows[n - 1].get("follow_score", rows[n - 1].get("score")) or min_score),
            "windows": windows,
        })

    valid = [c for c in candidates if _follow_line_candidate_valid(c)]
    if not valid:
        return {
            "status": "fallback",
            "reason": "no_valid_portfolio_prefix",
            "candidates": [_compact_follow_line_candidate(c) for c in candidates],
        }
    uncapped_valid = valid
    uncapped_reference = next((c for c in uncapped_valid if int(c["n"]) == target_n), None)
    if uncapped_reference is None:
        uncapped_reference = min(uncapped_valid, key=lambda c: abs(int(c["n"]) - target_n))
    uncapped_best = max(uncapped_valid, key=lambda c: _follow_line_candidate_key(c, target_n))

    valid, cliff_candidate = _cap_before_recent_cliff(uncapped_valid)
    if not valid:
        return {
            "status": "fallback",
            "reason": "no_valid_portfolio_prefix",
            "candidates": [_compact_follow_line_candidate(c) for c in candidates],
        }
    if cliff_candidate is not None and uncapped_best in valid and _meaningfully_better(uncapped_best, uncapped_reference):
        selected = uncapped_best
        reference = uncapped_reference
        best = uncapped_best
        reason = "portfolio_topn"
    else:
        reference = next((c for c in valid if int(c["n"]) == target_n), None)
        if reference is None:
            reference = min(valid, key=lambda c: abs(int(c["n"]) - target_n))
        best = max(valid, key=lambda c: _follow_line_candidate_key(c, target_n))
        use_best = best is not reference and _meaningfully_better(best, reference)
        selected = best if use_best else reference
        reason = "portfolio_topn" if use_best else "portfolio_flat_capacity"
        if cliff_candidate is not None and not use_best and int(selected["n"]) == int(valid[-1]["n"]):
            reason = "portfolio_recent_cliff"
    result = {
        "status": "ok",
        "reason": reason,
        "line": float(selected["line"]),
        "count": int(selected["n"]),
        "target_n": int(target_n),
        "min_n": int(min_n),
        "max_n": int(max_n),
        "selected": _compact_follow_line_candidate(selected),
        "reference": _compact_follow_line_candidate(reference),
        "best": _compact_follow_line_candidate(best),
        "recent_cliff_blocked": _compact_follow_line_candidate(cliff_candidate) if cliff_candidate else None,
        "candidates": [_compact_follow_line_candidate(c) for c in candidates],
    }
    _state_set(db, "follow_line_last_choice", {**result, "stamp": stamp or now_iso()})
    return result


def build_tune_candidate(base: dict, margin_mult: float, lev_caps: tuple[float, float, float],
                         deploy_full_pct: float) -> dict:
    margins = {k: float(base[k]) * float(margin_mult) for k in MARGIN_KEYS}
    lev_caps_map = {k: float(v) for k, v in zip(LEV_KEYS, lev_caps)}
    params_ = {
        **margins,
        **lev_caps_map,
        "DEPLOY_FULL_PCT": float(deploy_full_pct),
    }
    return {
        "mult": float(margin_mult),
        "margins": margins,
        "lev_caps": lev_caps_map,
        "deploy_full_pct": float(deploy_full_pct),
        "params": params_,
        "windows": {},
        "score": None,
    }


def build_add_candidate(base: dict, gap_k: float, shrink_g: float, max_hard: int) -> dict:
    params_ = {
        "ADD_GAP_K": float(gap_k),
        "ADD_GAP_SHRINK_G": float(shrink_g),
        "ADD_MAX_HARD": int(max_hard),
    }
    return {
        "gap_k": params_["ADD_GAP_K"],
        "shrink_g": params_["ADD_GAP_SHRINK_G"],
        "max_hard": params_["ADD_MAX_HARD"],
        "add_params": params_,
        "params": params_,
        "distance_keys": ADD_TUNE_KEYS,
        "windows": {},
        "score": None,
    }


def _unique_values(values, current=None):
    out = []
    for val in list(values or []) + ([] if current is None else [current]):
        try:
            fval = float(val)
        except (TypeError, ValueError):
            continue
        if all(abs(fval - x) > 1e-9 for x in out):
            out.append(fval)
    return out


def _unique_lev_sets(values, current=None):
    out = []
    raw = list(values or [])
    if current is not None:
        raw.append(current)
    for item in raw:
        try:
            vals = tuple(float(x) for x in item)
        except (TypeError, ValueError):
            continue
        if len(vals) != 3:
            continue
        if vals not in out:
            out.append(vals)
    return out


def tune_candidates_from_axes(base: dict) -> list[dict]:
    margin_mults = _unique_values(getattr(config, "AUTO_TUNE_MARGIN_MULTS", (0.8, 1.0, 1.2, 1.4, 1.6)), 1.0)
    lev_sets = _unique_lev_sets(
        getattr(config, "AUTO_TUNE_LEV_CAP_SETS", ((20, 8, 4), (25, 10, 4), (30, 12, 4), (35, 12, 5))),
        tuple(float(base[k]) for k in LEV_KEYS),
    )
    deploy_fulls = _unique_values(
        getattr(config, "AUTO_TUNE_DEPLOY_FULL_PCTS", (0.30, 0.40, 0.50)),
        float(base["DEPLOY_FULL_PCT"]),
    )
    return [
        build_tune_candidate(base, mult, levs, deploy)
        for mult, levs, deploy in itertools.product(margin_mults, lev_sets, deploy_fulls)
    ]


def add_candidates_from_axes(base: dict) -> list[dict]:
    gap_ks = _unique_values(getattr(config, "AUTO_TUNE_ADD_GAP_KS", (0.04, 0.06, 0.08, 0.10, 0.12)),
                            float(base["ADD_GAP_K"]))
    shrink_gs = _unique_values(getattr(config, "AUTO_TUNE_ADD_SHRINK_GS", (1.1, 1.2, 1.3, 1.5)),
                               float(base["ADD_GAP_SHRINK_G"]))
    max_hards = _unique_values(getattr(config, "AUTO_TUNE_ADD_MAX_HARDS", (4, 6, 8, 10)),
                               float(base["ADD_MAX_HARD"]))
    return [
        build_add_candidate(base, gap_k, shrink_g, int(max_hard))
        for gap_k, shrink_g, max_hard in itertools.product(gap_ks, shrink_gs, max_hards)
    ]


def follow_overrides_for_tune_candidate(follow: dict, candidate: dict) -> dict:
    out = dict(follow)
    params_ = candidate.get("params") or {}
    for key in MARGIN_KEYS:
        min_key = key.replace("_MARGIN_PCT", "_MARGIN_MIN_PCT")
        floor = float(follow.get(min_key) or 0.0)
        out[key] = max(floor, float(params_[key]))
    for key in LEV_KEYS:
        out[key] = float(params_[key])
    out["DEPLOY_FULL_PCT"] = float(params_["DEPLOY_FULL_PCT"])
    if "SMART_ADD" in out:
        out["ADD_STRATEGY"] = "smart" if out["SMART_ADD"] else "hardcap"
    return out


def follow_overrides_for_add_candidate(follow: dict, candidate: dict) -> dict:
    out = dict(follow)
    params_ = candidate.get("params") or {}
    out["ADD_STRATEGY"] = "smart"
    out["SMART_ADD"] = True
    out["ADD_GAP_K"] = float(params_["ADD_GAP_K"])
    out["ADD_GAP_SHRINK_G"] = float(params_["ADD_GAP_SHRINK_G"])
    out["ADD_MAX_HARD"] = int(params_["ADD_MAX_HARD"])
    return out


def follow_overrides_for_margin_candidate(follow: dict, margins: dict) -> dict:
    candidate = {"params": {**{k: margins[k] for k in MARGIN_KEYS},
                            **{k: follow.get(k, getattr(config, k)) for k in LEV_KEYS},
                            "DEPLOY_FULL_PCT": follow.get("DEPLOY_FULL_PCT", config.DEPLOY_FULL_PCT)}}
    return follow_overrides_for_tune_candidate(follow, candidate)


def _candidate_windows(db, addrs: list[str], sigmas: dict, overrides: dict, now_ms: int,
                       window_fills: dict[int, list[dict]] | None = None,
                       market_ctx: dict | None = None) -> dict:
    market_ctx = _load_market_ctx(db) if market_ctx is None else market_ctx
    windows = {}
    for days in _tune_days():
        fills = list((window_fills or {}).get(days) or [])
        if window_fills is None:
            start_ms = now_ms - int(days) * 86400_000
            fills = _load_portfolio_fills(db, addrs, start_ms)
        result = run_backtest("portfolio", fills, sigmas=sigmas, overrides=overrides, market_ctx=market_ctx or {})
        result["fills"] = len(fills)
        windows[int(days)] = result
    return windows


def evaluate_tune_candidate(db, addrs: list[str], follow: dict, candidate: dict,
                            sigmas: dict | None = None, now_ms: int | None = None,
                            window_fills: dict[int, list[dict]] | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    overrides = follow_overrides_for_tune_candidate(follow, candidate)
    params_ = {k: overrides[k] for k in TUNE_KEYS}
    sigmas = sigmas if sigmas is not None else _load_sigmas(db)
    out = dict(candidate)
    out["params"] = params_
    out["margins"] = {k: params_[k] for k in MARGIN_KEYS}
    out["lev_caps"] = {k: params_[k] for k in LEV_KEYS}
    out["deploy_full_pct"] = params_["DEPLOY_FULL_PCT"]
    out["windows"] = _candidate_windows(db, addrs, sigmas, overrides, now_ms, window_fills=window_fills)
    return out


def evaluate_add_candidate(db, addrs: list[str], follow: dict, candidate: dict,
                           sigmas: dict | None = None, now_ms: int | None = None,
                           window_fills: dict[int, list[dict]] | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    overrides = follow_overrides_for_add_candidate(follow, candidate)
    params_ = {k: overrides[k] for k in ADD_TUNE_KEYS}
    sigmas = sigmas if sigmas is not None else _load_sigmas(db)
    out = dict(candidate)
    out["params"] = params_
    out["add_params"] = params_
    out["windows"] = _candidate_windows(db, addrs, sigmas, overrides, now_ms, window_fills=window_fills)
    return out


def evaluate_margin_candidate(db, addrs: list[str], follow: dict, base: dict, mult: float,
                              sigmas: dict | None = None, now_ms: int | None = None,
                              window_fills: dict[int, list[dict]] | None = None) -> dict:
    tune_base = {**{k: float(base[k]) for k in MARGIN_KEYS},
                 **{k: float(follow[k]) for k in LEV_KEYS},
                 "DEPLOY_FULL_PCT": float(follow["DEPLOY_FULL_PCT"])}
    candidate = build_tune_candidate(tune_base, mult, tuple(tune_base[k] for k in LEV_KEYS),
                                     tune_base["DEPLOY_FULL_PCT"])
    return evaluate_tune_candidate(db, addrs, follow, candidate, sigmas=sigmas, now_ms=now_ms,
                                   window_fills=window_fills)


def _write_margin_params(db, margins: dict) -> None:
    stamp = now_iso()
    for key in MARGIN_KEYS:
        db.execute("UPDATE params SET value=?,updated_at=? WHERE key=?", (str(float(margins[key]) * 100.0), stamp, key))


def _write_tune_params(db, vals: dict) -> None:
    stamp = now_iso()
    for key in TUNE_KEYS:
        val = float(vals[key])
        stored = val * 100.0 if key in MARGIN_KEYS or key in DEPLOY_KEYS else val
        db.execute("UPDATE params SET value=?,updated_at=? WHERE key=?", (str(stored), stamp, key))


def _write_add_params(db, vals: dict) -> None:
    stamp = now_iso()
    for key in ADD_TUNE_KEYS:
        val = int(vals[key]) if key == "ADD_MAX_HARD" else float(vals[key])
        db.execute("UPDATE params SET value=?,updated_at=? WHERE key=?", (str(val), stamp, key))


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
        "capacity_open_fit", "master_leverage_coverage", "master_leverage_known",
        "master_leverage_missing", "fills",
    )
    out = {k: result.get(k) for k in keys if k in result}
    out["skip_reasons"] = result.get("skip_reasons") or {}
    return out


def _compact_candidate(candidate: dict) -> dict:
    return {
        "mult": candidate.get("mult"),
        "gap_k": candidate.get("gap_k"),
        "shrink_g": candidate.get("shrink_g"),
        "max_hard": candidate.get("max_hard"),
        "margins": candidate.get("margins"),
        "lev_caps": candidate.get("lev_caps"),
        "deploy_full_pct": candidate.get("deploy_full_pct"),
        "add_params": candidate.get("add_params"),
        "params": candidate.get("params"),
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

    current = {k: float(follow[k]) for k in TUNE_KEYS}
    base, baseline_reset = resolve_tune_baseline(db, current)
    sigmas = _load_sigmas(db)
    now_ms = int(time.time() * 1000)
    window_fills = _portfolio_window_fills(db, addrs, now_ms)
    candidates = [
        evaluate_tune_candidate(db, addrs, follow, candidate, sigmas=sigmas, now_ms=now_ms,
                                window_fills=window_fills)
        for candidate in tune_candidates_from_axes(base)
    ]
    baseline = next((c for c in candidates if _same_tune_values(c.get("params") or {}, base)), candidates[0])
    selected = choose_margin_candidate(candidates, baseline)
    selected_params = selected.get("params") or base
    selected_margins = {k: selected_params[k] for k in MARGIN_KEYS}

    follow_for_add = follow_overrides_for_tune_candidate(follow, selected)
    current_add = {k: float(follow[k]) for k in ADD_TUNE_KEYS}
    add_base, add_baseline_reset = resolve_add_baseline(db, current_add)
    add_candidates = []
    add_baseline = None
    selected_add = None
    selected_add_params = add_base
    if follow_for_add.get("SMART_ADD", True):
        add_candidates = [
            evaluate_add_candidate(db, addrs, follow_for_add, candidate, sigmas=sigmas, now_ms=now_ms,
                                   window_fills=window_fills)
            for candidate in add_candidates_from_axes(add_base)
        ]
        add_baseline = next((c for c in add_candidates if _same_add_values(c.get("params") or {}, add_base)),
                            add_candidates[0] if add_candidates else None)
        selected_add = choose_margin_candidate(add_candidates, add_baseline) if add_baseline else None
        if selected_add:
            selected_add_params = selected_add.get("params") or add_base

    applied_sizing = False
    applied_add = False
    if not dry_run and not _same_tune_values(current, selected_params):
        _write_tune_params(db, selected_params)
        applied_sizing = True
    if not dry_run and follow_for_add.get("SMART_ADD", True) and not _same_add_values(current_add, selected_add_params):
        _write_add_params(db, selected_add_params)
        applied_add = True
    applied = applied_sizing or applied_add
    if not dry_run and applied:
        _enqueue_reload(db, source)
    if not dry_run:
        store_tune_state(db, base, selected_params)
        if follow_for_add.get("SMART_ADD", True):
            store_add_state(db, add_base, selected_add_params)

    result = {
        "status": "ok",
        "applied": applied,
        "applied_sizing": applied_sizing,
        "applied_add": applied_add,
        "baseline_reset": baseline_reset,
        "add_baseline_reset": add_baseline_reset,
        "followed_n": len(addrs),
        "selected_mult": selected.get("mult"),
        "margins": selected_margins,
        "lev_caps": selected.get("lev_caps"),
        "deploy_full_pct": selected.get("deploy_full_pct"),
        "params": selected_params,
        "add_params": selected_add_params,
        "candidates": [_compact_candidate(c) for c in candidates],
        "add_candidates": [_compact_candidate(c) for c in add_candidates],
    }
    _record_run(db, source, stamp, selected, applied, len(addrs), base, result)
    db.commit()
    return result
