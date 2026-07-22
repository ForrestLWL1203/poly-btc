"""Post-scan portfolio auto-tuning for copy-trading sizing.

The tuner adjusts the operator-approved sizing surface: first-open margin upper
bounds, tier leverage caps, the deployment level where new opens begin to
shrink, and the smart-add core knobs. Lower margin bounds, per-coin caps, max
deployment cap, and stop rules remain operator-owned risk limits.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import time
from datetime import datetime
from typing import Iterable

from hyper import config, params
from hyper.copy.copy_backtest import run_backtest, slice_backtest_result
from hyper.copy.copy_data import load_copyable_fills
from hyper.copy.copy_policy import load_copy_policy
from hyper.copy.sector import parse_json_obj
from hyper.market import generation_market, price_path
from hyper.util import f, now_iso
from . import state as selection, strategy_revision

MARGIN_KEYS = ("STABLE_MARGIN_PCT", "MID_MARGIN_PCT", "HIGH_MARGIN_PCT")
COIN_CAP_KEYS = ("STABLE_COIN_CAP_PCT", "MID_COIN_CAP_PCT", "HIGH_COIN_CAP_PCT")
LEV_KEYS = ("STABLE_LEV_CAP", "MID_LEV_CAP", "HIGH_LEV_CAP")
DEPLOY_KEYS = ("DEPLOY_FULL_PCT",)
TUNE_KEYS = MARGIN_KEYS + LEV_KEYS + DEPLOY_KEYS
ADD_TUNE_KEYS = ("ADD_GAP_K", "POS_ADD_GAP_K", "ADD_GAP_SHRINK_G", "ADD_MAX_HARD")
CAPACITY_SKIP_KEYS = ("skip_coin_full", "skip_no_cash", "skip_deploy_cap", "skip_margin_too_small")


def margin_add_capacity_ceilings(follow: dict) -> dict[str, float]:
    """Per-tier margin ceilings that preserve four executable smart-add slots."""
    margin_equity_pct = max(1e-9, float(
        follow.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
    ))
    min_add_pct = max(0.0, float(
        follow.get("MIN_OPEN_MARGIN_PCT", config.MIN_OPEN_MARGIN_PCT)
    ))
    add_capacity = max(1, int(getattr(config, "SMART_ADD_MIN_CAPACITY", 4) or 4))
    return {
        margin_key: max(0.0, (
            float(follow.get(cap_key, getattr(config, cap_key)))
            - min_add_pct * margin_equity_pct
        ) / (add_capacity * margin_equity_pct))
        for margin_key, cap_key in zip(MARGIN_KEYS, COIN_CAP_KEYS)
    }


def enforce_margin_add_capacity(values: dict, follow: dict) -> dict:
    """Clamp every tier to the operator's coin cap instead of compounding upward forever."""
    out = dict(values)
    ceilings = margin_add_capacity_ceilings(follow)
    for key in MARGIN_KEYS:
        out[key] = min(float(out[key]), ceilings[key])
    return out


def prepare_refined_price_path(db, fills: list[dict], start_ms: int, end_ms: int,
                               *, sigmas: dict, overrides: dict, market_ctx: dict,
                               immutable_market_ctx: bool = False) -> tuple[list[dict], dict]:
    """Fetch the 15m baseline, then refine only liquidation-ambiguous markets and recent ranges."""
    # A generation snapshot already carries immutable maintenance metadata.  Refresh only genuinely missing
    # rows; otherwise a current exchange response would silently mutate the replay surface mid-generation.
    missing_meta_coins = {
        row.get("coin") for row in fills
        if row.get("coin") and not (market_ctx.get(row.get("coin")) or {}).get("max_leverage")
    }
    if immutable_market_ctx and missing_meta_coins:
        missing = ",".join(sorted(missing_meta_coins)[:12])
        raise RuntimeError(f"generation_market_max_leverage_missing:{missing}")
    margin_meta = price_path.refresh_margin_metadata(
        db, [row for row in fills if row.get("coin") in missing_meta_coins],
    ) if missing_meta_coins else {}
    for coin, max_leverage in margin_meta.items():
        market_ctx.setdefault(coin, {})["max_leverage"] = max_leverage
    price_path.ensure(db, fills, start_ms, end_ms)
    meta = price_path.coverage(db, fills, start_ms, end_ms)
    rows = price_path.load_refined(db, fills, start_ms, end_ms)
    for interval in price_path.REFINEMENT_INTERVALS:
        probe = run_backtest(
            "portfolio", fills, sigmas=sigmas, overrides=overrides, market_ctx=market_ctx,
            price_path=rows, price_path_meta=meta,
        )
        refinement = price_path.refinement_fills(
            probe.get("ambiguous_path_ranges") or [], end_ms, interval,
        )
        if not refinement:
            continue
        refine_start = min(int(row["time"]) for row in refinement)
        price_path.ensure(db, refinement, refine_start, end_ms, interval=interval)
        fine = price_path.load(db, refinement, refine_start, end_ms, interval=interval)
        rows = price_path.merge_finer_path(rows, fine)
    return rows, meta


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


def _same_tune_values(a: dict, b: dict, eps: float = 1e-9) -> bool:
    return _same_values(a, b, TUNE_KEYS, eps)


def _same_add_values(a: dict, b: dict, eps: float = 1e-9) -> bool:
    return _same_values(a, b, ADD_TUNE_KEYS, eps)


def _capacity_skips(result: dict) -> int:
    skips = result.get("skip_reasons") or {}
    return int(sum(skips.get(k, 0) or 0 for k in CAPACITY_SKIP_KEYS))


def _min_closed_for_days(days: int) -> int:
    return load_copy_policy().min_closed(int(days))


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

    max_fit_drop = float(getattr(config, "AUTO_TUNE_MARGIN_MAX_OPEN_FIT_DROP", 0.03))
    absolute_fit_floor = float(getattr(config, "AUTO_TUNE_MARGIN_MIN_OPEN_FIT", 0.75))
    base_fit = _capacity_fit(base_primary)
    # A parameter search must not become permanently disabled merely because the currently published
    # baseline is already below a live-money absolute floor.  In that situation require the proposal to
    # preserve (or improve) the baseline; once the baseline clears the floor, the absolute floor applies.
    min_open_fit = max(
        absolute_fit_floor if base_fit >= absolute_fit_floor else 0.0,
        base_fit - max_fit_drop,
    )
    if _capacity_fit(result_primary) < min_open_fit:
        return False
    candidate_open_rate = result_primary.get("open_fill_rate")
    if candidate_open_rate is not None:
        base_open_rate = base_primary.get("open_fill_rate")
        base_open_rate = float(base_open_rate or 0.0)
        min_open_rate = max(
            0.70 if base_open_rate >= 0.70 else 0.0,
            base_open_rate - max_fit_drop,
        )
        if float(candidate_open_rate or 0.0) < min_open_rate:
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
    base_windows = baseline.get("windows") or {}
    weighted_net = _candidate_score(candidate)
    max_drawdown = max(
        (float(result.get("max_drawdown") or 0.0) for result in windows.values()),
        default=0.0,
    )
    # Compare profit and drawdown in the same dollars. A profitable candidate
    # is not rejected merely because drawdown rises by an arbitrary percentage
    # point; the extra downside must be paid for by extra net profit.
    risk_adjusted_utility = weighted_net - max_drawdown * float(config.INITIAL_BALANCE)
    liquidations = max(
        (int(result.get("liquidations") or 0) for result in windows.values()),
        default=10**9,
    )
    primary = windows.get(30) or (windows.get(max(windows)) if windows else {})
    base_primary = base_windows.get(30) or (base_windows.get(max(base_windows)) if base_windows else {})
    primary_net = _result_pnl(primary)
    base_net = _result_pnl(base_primary)
    # Isolated-liquidation losses are already debited from every window's net PnL and represented in
    # equity drawdown.  Profit is therefore the primary objective; counting liquidations ahead of PnL
    # would double-charge the loss and systematically select near-zero-return leverage surfaces.  The raw
    # count remains a final tie-break/audit signal when economic results are otherwise equal.
    return (
        weighted_net,
        primary_net,
        risk_adjusted_utility,
        _result_pnl(windows.get(14, {})),
        _result_pnl(windows.get(7, {})),
        -liquidations,
        -_candidate_distance(candidate, baseline),
    )


def _candidate_admission_rank_key(candidate: dict, baseline: dict) -> tuple:
    """Rank a sizing surface by whether it can fund the whole proposed Core prefix.

    Normal tuning is profit-led.  Formation additionally needs one capacity-led finalist; otherwise every
    lower-margin candidate can be pruned before walk-forward validation simply because it earns a little less
    than an already-congested baseline.
    """
    windows = candidate.get("windows") or {}
    usable = [result for days, result in windows.items() if _enough_sample(result, int(days))]
    usable = usable or list(windows.values())
    min_capacity = min((_capacity_fit(result) for result in usable), default=0.0)
    min_open = min((float(result.get("open_fill_rate") or 0.0) for result in usable), default=0.0)
    capacity_floor = float(load_copy_policy().min_capacity_fit)
    profitable = bool(usable) and all(_result_pnl(result) > 0.0 for result in usable)
    return (
        int(profitable and min_capacity >= capacity_floor and min_open >= 0.70),
        int(profitable),
        min(min_capacity, capacity_floor) + min(min_open, 0.70),
        min_capacity,
        min_open,
        *_candidate_rank_key(candidate, baseline),
    )


def choose_margin_candidate(candidates: list[dict], baseline: dict) -> dict:
    """Pick the best 14d-led PnL candidate that preserves copyability."""
    valid = [c for c in candidates if _candidate_valid(c, baseline)]
    if not valid:
        return baseline
    best = max(valid, key=lambda c: _candidate_rank_key(c, baseline))
    return best if _candidate_rank_key(best, baseline) >= _candidate_rank_key(baseline, baseline) else baseline


def _diverse_sizing_candidates(candidates: list[dict], baseline: dict, limit: int) -> list[dict]:
    """Keep risk leaders without allowing one leverage tuple to consume every validation slot."""
    ranked = sorted(candidates, key=lambda item: _candidate_rank_key(item, baseline), reverse=True)
    groups = {}
    for candidate in ranked:
        params_ = candidate.get("params") or {}
        key = tuple(round(float(params_.get(name, 0.0)), 8) for name in LEV_KEYS)
        groups.setdefault(key, []).append(candidate)
    ordered_groups = sorted(
        groups.values(), key=lambda rows: _candidate_rank_key(rows[0], baseline), reverse=True,
    )
    selected = []
    depth = 0
    while len(selected) < max(1, int(limit)):
        added = False
        for rows in ordered_groups:
            if depth >= len(rows):
                continue
            selected.append(rows[depth])
            added = True
            if len(selected) >= max(1, int(limit)):
                break
        if not added:
            break
        depth += 1
    return selected


def _load_sigmas(db, generation_id: str | None = None) -> dict:
    if generation_id:
        if not generation_market.has_snapshot(db, generation_id):
            raise RuntimeError(f"market_snapshot_missing_rescan_required:{generation_id}")
        sigmas, _ = generation_market.load(db, generation_id)
        return sigmas
    try:
        return {coin: sigma for coin, sigma in db.execute("SELECT coin,sigma FROM coin_vol WHERE sigma IS NOT NULL")}
    except sqlite3.Error:
        return {}


def _load_market_ctx(db, generation_id: str | None = None) -> dict:
    if generation_id:
        if not generation_market.has_snapshot(db, generation_id):
            raise RuntimeError(f"market_snapshot_missing_rescan_required:{generation_id}")
        sigmas, market_ctx = generation_market.load(db, generation_id)
        return market_ctx
    try:
        rows = db.execute(
            "SELECT coin,day_ntl_vlm,oi_notional,max_leverage FROM coin_vol "
            "WHERE day_ntl_vlm IS NOT NULL OR oi_notional IS NOT NULL OR max_leverage IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {r[0]: {"day_ntl_vlm": r[1], "oi_notional": r[2], "max_leverage": r[3]} for r in rows}


def _load_followed_wallets(db, follow: dict) -> list[str]:
    explicit = selection.published_core_addrs(db, int(config.MAX_TARGETS))
    return explicit or []


def _load_portfolio_fills(db, addrs: Iterable[str], start_ms: int, *, include_watch=False) -> list[dict]:
    addrs = [(a or "").lower() for a in addrs if a]
    if not addrs:
        return []
    qs = ",".join("?" for _ in addrs)
    policies = {
        (r[0] or "").lower(): parse_json_obj(r[1])
        for r in db.execute(
            f"SELECT addr,sector_policy_json FROM profile WHERE lower(addr) IN ({qs})", addrs,
        ).fetchall()
    }
    missing = [addr for addr in addrs if not (policies.get(addr) or {}).get("allowed")]
    if missing:
        mqs = ",".join("?" for _ in missing)
        for addr, raw in db.execute(
            f"SELECT addr,sector_policy_json FROM watchlist WHERE lower(addr) IN ({mqs})", missing,
        ).fetchall():
            policy = parse_json_obj(raw)
            if policy.get("allowed"):
                policies[(addr or "").lower()] = policy
    missing = [addr for addr in addrs if not (policies.get(addr) or {}).get("allowed")]
    generation = selection.latest_published_generation(db)
    if missing and generation:
        mqs = ",".join("?" for _ in missing)
        for addr, raw in db.execute(
            f"SELECT addr,sector_policy_json FROM follow_selection "
            f"WHERE generation=? AND lower(addr) IN ({mqs})",
            (generation, *missing),
        ).fetchall():
            policy = parse_json_obj(raw)
            if policy.get("allowed"):
                policies[(addr or "").lower()] = policy
    if include_watch:
        # Formation deliberately admits parameter-sensitive return-watch wallets so a safe sizing surface
        # can prove whether they cross the public Core return line.  Their live ``allowed`` list is empty by
        # definition; use ``watch`` only inside that sealed, non-executing replay.  Observer and ordinary
        # auto-tune callers keep the fail-closed allowed-only policy.
        for addr, policy in list(policies.items()):
            if policy.get("watch"):
                watched = list(policy.get("watch") or ())
                promoted_sectors = list(dict.fromkeys([
                    *(policy.get("allowed") or ()), *watched,
                ]))
                promoted = {**policy, "allowed": promoted_sectors}
                for sector in watched:
                    item = policy.get(sector)
                    if isinstance(item, dict):
                        promoted[sector] = {**item, "allow": True}
                policies[addr] = promoted
    return load_copyable_fills(
        db,
        addrs,
        start_ms,
        policies=policies,
        # A missing/corrupt policy is not sufficient evidence for a portfolio
        # tuner to trade every sector.  The scanner can keep the wallet in its
        # challenger path while the tuner fails closed here.
        policy_default=False,
    )


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


def _portfolio_window_fills(db, addrs: list[str], now_ms: int, *, include_watch=False) -> dict[int, list[dict]] | None:
    days = _tune_days()
    max_days = max(days)
    warmup_days = int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
    start_ms = now_ms - (max_days + warmup_days) * 86400_000
    max_bytes = int(getattr(config, "AUTO_TUNE_FILL_CACHE_MAX_BYTES", 64 * 1024 * 1024) or 0)
    if max_bytes > 0 and _portfolio_fill_json_bytes(db, addrs, start_ms) > max_bytes:
        return None
    fills = _load_portfolio_fills(db, addrs, start_ms, include_watch=include_watch)
    windows = {}
    for day in days:
        start_ms = now_ms - (day + warmup_days) * 86400_000
        windows[day] = [x for x in fills if int(x.get("time") or 0) >= start_ms]
    return windows


def _filter_window_fills_by_addr(window_fills: dict[int, list[dict]], addrs: Iterable[str]) -> dict[int, list[dict]]:
    allowed = {(a or "").lower() for a in addrs if a}
    return {
        int(days): [x for x in fills if (x.get("user") or "").lower() in allowed]
        for days, fills in (window_fills or {}).items()
    }


def build_add_candidate(base: dict, gap_k: float, shrink_g: float, max_hard: int,
                        pos_gap_k: float | None = None) -> dict:
    params_ = {
        "ADD_GAP_K": float(gap_k),
        "POS_ADD_GAP_K": float(base.get("POS_ADD_GAP_K", gap_k) if pos_gap_k is None else pos_gap_k),
        "ADD_GAP_SHRINK_G": float(shrink_g),
        "ADD_MAX_HARD": int(max_hard),
    }
    return {
        "gap_k": params_["ADD_GAP_K"],
        "pos_gap_k": params_["POS_ADD_GAP_K"],
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


def _candidate_from_params(params_: dict, *, axis: str) -> dict:
    params_ = {key: float(params_[key]) for key in TUNE_KEYS}
    return {
        "mult": None,
        "axis": axis,
        "margins": {key: params_[key] for key in MARGIN_KEYS},
        "lev_caps": {key: params_[key] for key in LEV_KEYS},
        "deploy_full_pct": params_["DEPLOY_FULL_PCT"],
        "params": params_,
        "windows": {},
        "score": None,
    }


def independent_margin_candidates(base: dict, follow: dict) -> list[dict]:
    """Coordinate-polish each tier around a jointly selected sizing surface."""
    base = enforce_margin_add_capacity(base, follow)
    ceilings = margin_add_capacity_ceilings(follow)
    factors = _unique_values(
        getattr(
            config, "AUTO_TUNE_MARGIN_FACTORS",
            getattr(config, "AUTO_TUNE_MARGIN_MULTS", (0.8, 1.0, 1.2, 1.4, 1.6)),
        ), 1.0
    )
    candidates = [_candidate_from_params(dict(base), axis="independent_margins")]
    for key in MARGIN_KEYS:
        floor_key = key.replace("_MARGIN_PCT", "_MARGIN_MIN_PCT")
        floor = float(follow.get(floor_key) or 0.0)
        safe_floor = min(floor, ceilings[key])
        for value in sorted({min(ceilings[key], max(safe_floor, float(base[key]) * factor)) for factor in factors}):
            if abs(value - float(base[key])) <= 1e-12:
                continue
            candidates.append(_candidate_from_params(
                {**base, key: value}, axis=f"independent_margin_{key.lower()}",
            ))
    return candidates


def global_margin_candidates(base: dict, follow: dict) -> list[dict]:
    """Shrink/grow all volatility tiers together so formation can relieve account-wide contention."""
    base = enforce_margin_add_capacity(base, follow)
    ceilings = margin_add_capacity_ceilings(follow)
    factors = _unique_values(getattr(config, "AUTO_TUNE_MARGIN_FACTORS", (0.85, 1.0, 1.15)), 1.0)
    candidates = []
    for factor in factors:
        proposal = dict(base)
        for key in MARGIN_KEYS:
            floor_key = key.replace("_MARGIN_PCT", "_MARGIN_MIN_PCT")
            floor = min(float(follow.get(floor_key) or 0.0), ceilings[key])
            proposal[key] = min(ceilings[key], max(floor, float(base[key]) * float(factor)))
        candidates.append(_candidate_from_params(proposal, axis="global_margins"))
    return candidates


def capacity_margin_candidates(base: dict, follow: dict) -> list[dict]:
    """Probe a tiny absolute cold-start grid anchored to each tier's executable add capacity.

    Percentage perturbations around a freshly seeded 3.5% stable margin cannot rediscover a previously
    useful 6-7% surface in a bounded number of rounds.  Three shared ceiling fractions span that missing
    range while adding only three portfolio replays, not a stable/mid/high Cartesian product.
    """
    base = enforce_margin_add_capacity(base, follow)
    ceilings = margin_add_capacity_ceilings(follow)
    fractions = _unique_values(
        getattr(config, "AUTO_TUNE_MARGIN_CEILING_FRACTIONS", (0.50, 0.75, 1.00)),
    )
    proposals = [dict(base)]
    for fraction in fractions:
        proposal = dict(base)
        for key in MARGIN_KEYS:
            floor_key = key.replace("_MARGIN_PCT", "_MARGIN_MIN_PCT")
            floor = min(float(follow.get(floor_key) or 0.0), ceilings[key])
            proposal[key] = min(ceilings[key], max(floor, ceilings[key] * float(fraction)))
        proposals.append(proposal)
    out = []
    for proposal in proposals:
        if any(_same_margin_values(proposal, item.get("params") or {}) for item in out):
            continue
        out.append(_candidate_from_params(proposal, axis="capacity_margin_grid"))
    return out


def _same_margin_values(a: dict, b: dict, eps: float = 1e-9) -> bool:
    return all(abs(float(a.get(key, 0.0)) - float(b.get(key, 0.0))) <= eps for key in MARGIN_KEYS)


def independent_leverage_candidates(base: dict) -> list[dict]:
    """Coordinate-polish one leverage tier at a time around the selected joint surface."""
    configured = {
        "STABLE_LEV_CAP": getattr(config, "AUTO_TUNE_COORD_STABLE_LEV_CAPS", (35, 32, 30, 28, 25)),
        "MID_LEV_CAP": getattr(config, "AUTO_TUNE_COORD_MID_LEV_CAPS", (12, 11, 10, 9)),
        "HIGH_LEV_CAP": getattr(config, "AUTO_TUNE_COORD_HIGH_LEV_CAPS", (4, 5, 6)),
    }
    axes = [sorted({float(value) for value in configured[key]} | {float(base[key])}) for key in LEV_KEYS]
    candidates = [_candidate_from_params(dict(base), axis="independent_leverage_baseline")]
    for index, key in enumerate(LEV_KEYS):
        for value in axes[index]:
            if abs(value - float(base[key])) <= 1e-12:
                continue
            candidates.append(_candidate_from_params(
                {**base, key: value}, axis=f"independent_leverage_{key.lower()}",
            ))
    return candidates


def coarse_leverage_candidates(base: dict) -> list[dict]:
    """Baseline plus only each tier's low/high endpoint for prefix-count exploration."""
    candidates = independent_leverage_candidates(base)
    out = [candidates[0]]
    for key in LEV_KEYS:
        rows = [
            candidate for candidate in candidates[1:]
            if sum(
                abs(float((candidate.get("params") or {}).get(name, base[name])) - float(base[name])) > 1e-12
                for name in LEV_KEYS
            ) == 1
            and abs(float((candidate.get("params") or {})[key]) - float(base[key])) > 1e-12
        ]
        rows.sort(key=lambda candidate: float((candidate.get("params") or {})[key]))
        for candidate in (rows[:1] + rows[-1:]):
            marker = tuple(float((candidate.get("params") or {})[name]) for name in TUNE_KEYS)
            if not any(
                tuple(float((existing.get("params") or {})[name]) for name in TUNE_KEYS) == marker
                for existing in out
            ):
                out.append(candidate)
    return out


def _tier_leverage_shortlist(candidates: list[dict], baseline: dict, key: str,
                             limit: int = 3) -> list[float]:
    """Keep current, best-profit and fewest-liquidation values for one independently tested tier."""
    base_params = baseline.get("params") or {}
    rows = []
    for candidate in candidates:
        params_ = candidate.get("params") or {}
        if all(
            name == key or abs(float(params_.get(name, 0.0)) - float(base_params.get(name, 0.0))) <= 1e-9
            for name in LEV_KEYS
        ):
            rows.append(candidate)
    if not rows:
        return [float(base_params[key])]
    primary = lambda row: (row.get("windows") or {}).get(30) or {}
    picks = [baseline]
    picks.append(max(rows, key=lambda row: _result_pnl(primary(row))))
    picks.append(min(rows, key=lambda row: (
        int(primary(row).get("liquidations") or 0), -_result_pnl(primary(row)),
    )))
    picks.extend(sorted(rows, key=lambda row: _candidate_rank_key(row, baseline), reverse=True))
    values = []
    for candidate in picks:
        value = float((candidate.get("params") or base_params)[key])
        if value not in values:
            values.append(value)
        if len(values) >= max(1, int(limit)):
            break
    return values


def deploy_candidates(base: dict) -> list[dict]:
    values = _unique_values(
        getattr(config, "AUTO_TUNE_DEPLOY_FULL_PCTS", (0.30, 0.40, 0.50)),
        float(base["DEPLOY_FULL_PCT"]),
    )
    return [
        _candidate_from_params({**base, "DEPLOY_FULL_PCT": value}, axis="deploy_full")
        for value in values
    ]


def add_candidates_from_axes(base: dict) -> list[dict]:
    gap_ks = _unique_values(getattr(config, "AUTO_TUNE_ADD_GAP_KS", (0.04, 0.06, 0.08, 0.10, 0.12)),
                            float(base["ADD_GAP_K"]))
    pos_gap_ks = _unique_values(getattr(config, "AUTO_TUNE_POS_ADD_GAP_KS", (0.06, 0.08, 0.10, 0.12)),
                                float(base["POS_ADD_GAP_K"]))
    shrink_gs = _unique_values(getattr(config, "AUTO_TUNE_ADD_SHRINK_GS", (1.1, 1.2, 1.3, 1.5)),
                               float(base["ADD_GAP_SHRINK_G"]))
    max_hards = _unique_values(getattr(config, "AUTO_TUNE_ADD_MAX_HARDS", (4, 6, 8, 10)),
                               float(base["ADD_MAX_HARD"]))
    baseline = build_add_candidate(
        base, float(base["ADD_GAP_K"]), float(base["ADD_GAP_SHRINK_G"]),
        int(base["ADD_MAX_HARD"]), pos_gap_k=float(base["POS_ADD_GAP_K"]),
    )
    out = [baseline]
    axes = (
        ("ADD_GAP_K", gap_ks),
        ("POS_ADD_GAP_K", pos_gap_ks),
        ("ADD_GAP_SHRINK_G", shrink_gs),
        ("ADD_MAX_HARD", max_hards),
    )
    seen = {tuple(float(baseline["params"][key]) for key in ADD_TUNE_KEYS)}
    for key, values in axes:
        for value in values:
            params_ = {name: baseline["params"][name] for name in ADD_TUNE_KEYS}
            params_[key] = int(value) if key == "ADD_MAX_HARD" else float(value)
            marker = tuple(float(params_[name]) for name in ADD_TUNE_KEYS)
            if marker in seen:
                continue
            seen.add(marker)
            out.append(build_add_candidate(
                params_, params_["ADD_GAP_K"], params_["ADD_GAP_SHRINK_G"],
                int(params_["ADD_MAX_HARD"]), pos_gap_k=params_["POS_ADD_GAP_K"],
            ))
    return out


def follow_overrides_for_tune_candidate(follow: dict, candidate: dict) -> dict:
    out = dict(follow)
    params_ = candidate.get("params") or {}
    ceilings = margin_add_capacity_ceilings(follow)
    for key in MARGIN_KEYS:
        min_key = key.replace("_MARGIN_PCT", "_MARGIN_MIN_PCT")
        floor = min(float(follow.get(min_key) or 0.0), ceilings[key])
        out[key] = min(ceilings[key], max(floor, float(params_[key])))
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
    out["POS_ADD_GAP_K"] = float(params_["POS_ADD_GAP_K"])
    out["ADD_GAP_SHRINK_G"] = float(params_["ADD_GAP_SHRINK_G"])
    out["ADD_MAX_HARD"] = int(params_["ADD_MAX_HARD"])
    return out


def _candidate_windows(db, addrs: list[str], sigmas: dict, overrides: dict, now_ms: int,
                       window_fills: dict[int, list[dict]] | None = None,
                       market_ctx: dict | None = None, path_rows: list[dict] | None = None,
                       path_meta: dict | None = None) -> dict:
    market_ctx = _load_market_ctx(db) if market_ctx is None else market_ctx
    windows = {}
    for days in _tune_days():
        fills = list((window_fills or {}).get(days) or [])
        if window_fills is None:
            start_ms = now_ms - int(days) * 86400_000
            fills = _load_portfolio_fills(db, addrs, start_ms)
        replay_path = path_rows
        if path_rows and fills:
            first_fill = min(int(row.get("time") or 0) for row in fills)
            last_fill = max(int(row.get("time") or 0) for row in fills)
            replay_path = [
                row for row in path_rows
                if int(row.get("close_time") or row.get("time") or 0) >= first_fill
                and int(row.get("open_time") or row.get("time") or 0) <= last_fill
            ]
        warm_result = run_backtest(
            "portfolio", fills, sigmas=sigmas, overrides=overrides, market_ctx=market_ctx or {},
            price_path=replay_path, price_path_meta=path_meta,
        )
        result = slice_backtest_result(
            warm_result,
            now_ms - int(days) * 86_400_000,
            window_days=int(days),
        )
        result["fills"] = len(fills)
        windows[int(days)] = result
    return windows


def evaluate_portfolio_window(db, addrs: list[str], sigmas: dict, overrides: dict, now_ms: int,
                              *, window_fills: dict[int, list[dict]], days: int = 30,
                              market_ctx: dict | None = None, path_rows: list[dict] | None = None,
                              path_meta: dict | None = None) -> dict:
    """Replay one portfolio/window and immediately discard heavy position/equity details."""
    days = int(days)
    fills = list((window_fills or {}).get(days) or [])
    warm_result = run_backtest(
        "portfolio",
        fills,
        sigmas=sigmas,
        overrides=overrides,
        market_ctx=_load_market_ctx(db) if market_ctx is None else market_ctx,
        price_path=path_rows,
        price_path_meta=path_meta,
    )
    result = slice_backtest_result(
        warm_result,
        now_ms - days * 86_400_000,
        window_days=days,
    )
    result["fills"] = len(fills)
    return _compact_backtest(result)


def store_effective_portfolio_replay(db, generation_id: str, *, now_ms: int | None = None) -> dict:
    """Persist the final Core's shared-account replay under currently effective parameters."""
    addrs = _load_followed_wallets(db, {})
    now_ms = int(now_ms or time.time() * 1000)
    if not addrs:
        summary = {
            "generation": generation_id, "coreCount": 0, "replayedAt": now_iso(),
            "status": "empty",
        }
        _state_set(db, "effective_portfolio_replay", summary)
        db.commit()
        return summary
    window_fills = _portfolio_window_fills(db, addrs, now_ms)
    if window_fills is None or not any(window_fills.values()):
        return {"generation": generation_id, "status": "unavailable", "coreCount": len(addrs)}
    follow = params.load_follow(db)
    if "SMART_ADD" in follow:
        follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
    all_fills = list(window_fills.get(max(window_fills)) or [])
    path_start = now_ms - (max(window_fills) + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7))) * 86_400_000
    sigmas = _load_sigmas(db, generation_id)
    market_ctx = _load_market_ctx(db, generation_id)
    path_rows, path_meta = prepare_refined_price_path(
        db, all_fills, path_start, now_ms, sigmas=sigmas, overrides=follow,
        market_ctx=market_ctx, immutable_market_ctx=True,
    )
    windows = _candidate_windows(
        db, addrs, sigmas, follow, now_ms, window_fills=window_fills,
        market_ctx=market_ctx, path_rows=path_rows, path_meta=path_meta,
    )
    worst_windows = _candidate_windows(
        db, addrs, sigmas, {**follow, "AMBIGUOUS_PATH_MODE": "liquidate"}, now_ms,
        window_fills=window_fills, market_ctx=market_ctx,
        path_rows=path_rows, path_meta=path_meta,
    )
    params_hash = hashlib.sha256(
        json.dumps(follow, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    primary = windows.get(30) or windows.get(max(windows))
    worst_primary = worst_windows.get(30) or worst_windows.get(max(worst_windows))
    conservative_net30 = min(
        f(primary.get("copy_net_pnl")), f(worst_primary.get("copy_net_pnl")),
    )
    conservative_liquidations30 = max(
        int(primary.get("liquidations") or 0), int(worst_primary.get("liquidations") or 0),
    )
    fills_only_primary = evaluate_portfolio_window(
        db, addrs, sigmas, follow, now_ms,
        window_fills={30: list(window_fills.get(30) or [])}, days=30,
        market_ctx=market_ctx,
    )
    effective_params = {
        "leverageCaps": {key: f(follow.get(key)) for key in LEV_KEYS},
        "marginPct": {key: f(follow.get(key)) for key in MARGIN_KEYS},
        "marginEquityPct": f(follow.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)),
        "initialMarginEquity": f(config.INITIAL_BALANCE) * f(
            follow.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
        ),
        "deployFullPct": f(follow.get("DEPLOY_FULL_PCT")),
        "add": {key: f(follow.get(key)) for key in ADD_TUNE_KEYS},
        "smartAddCapacity": {
            "reservedAdds": int(getattr(config, "SMART_ADD_MIN_CAPACITY", 4) or 4),
            "marginCeilings": margin_add_capacity_ceilings(follow),
        },
    }
    summary = {
        "generation": generation_id,
        "status": "ok",
        "coreCount": len(addrs),
        "paramsHash": params_hash,
        "replayedAt": now_iso(),
        "netPnl30": f(primary.get("copy_net_pnl")),
        "netPnl30Worst": conservative_net30,
        "netPnl30AmbiguousLiquidate": f(worst_primary.get("copy_net_pnl")),
        "fillsOnlyNetPnl30": f(fills_only_primary.get("copy_net_pnl")),
        "closed30": int(primary.get("closed_n") or 0),
        "maxDrawdown30": f(primary.get("max_drawdown")),
        "openRate30": f(primary.get("open_fill_rate")),
        "capacityFit30": f(primary.get("capacity_open_fit")),
        "liquidations30": int(primary.get("liquidations") or 0),
        "liquidations30Worst": conservative_liquidations30,
        "liquidations30AmbiguousLiquidate": int(worst_primary.get("liquidations") or 0),
        "fillsOnlyLiquidations30": int(fills_only_primary.get("liquidations") or 0),
        "ambiguousLiquidations30": int(primary.get("ambiguous_liquidations") or 0),
        "pricePathCoverage30": f(primary.get("price_path_coverage")),
        "pricePathBoundarySkips30": int(primary.get("price_path_boundary_skips") or 0),
        "pricePathStatus": (
            "covered" if f(primary.get("price_path_coverage"))
            >= float(getattr(config, "CORE_PRICE_PATH_MIN_COVERAGE", .95)) else "unverified"
        ),
        "estimateKind": "trade_ohlc_conservative_proxy",
        "effectiveParams": effective_params,
        "maintenanceMarginCoverage30": f(primary.get("maintenance_margin_coverage")),
        "addMetricsVersion": primary.get("add_metrics_version"),
        "addOutcomeCounts30": primary.get("add_outcome_counts") or {},
        "rawAddOrderFollowRate30": primary.get("raw_add_order_follow_rate"),
        "actionableAddCaptureRate30": primary.get("actionable_add_capture_rate"),
        "entryGapPctWeighted30": primary.get("entry_gap_pct_weighted"),
        "addFidelity30": primary.get("add_fidelity"),
        "behaviorReplication30": primary.get("behavior_replication_v2"),
        "behaviorReplication30Worst": worst_primary.get("behavior_replication_v2"),
        "profitFactor30": primary.get("profit_factor"),
        "netAfterTop1": primary.get("net_after_top1"),
        "netAfterTop2": primary.get("net_after_top2"),
        "profitConcentration30": primary.get("pnl_concentration") or {},
        "netPnl14": f((windows.get(14) or {}).get("copy_net_pnl")),
        "netPnl7": f((windows.get(7) or {}).get("copy_net_pnl")),
    }
    _state_set(db, "effective_portfolio_replay", summary)
    db.commit()
    return summary


def evaluate_tune_candidate(db, addrs: list[str], follow: dict, candidate: dict,
                            sigmas: dict | None = None, now_ms: int | None = None,
                            window_fills: dict[int, list[dict]] | None = None,
                            path_rows: list[dict] | None = None, path_meta: dict | None = None,
                            primary_only: bool = False, market_ctx: dict | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    overrides = {**follow_overrides_for_tune_candidate(follow, candidate),
                 "AMBIGUOUS_PATH_MODE": "liquidate"}
    params_ = {k: overrides[k] for k in TUNE_KEYS}
    sigmas = sigmas if sigmas is not None else _load_sigmas(db)
    out = dict(candidate)
    out["params"] = params_
    out["margins"] = {k: params_[k] for k in MARGIN_KEYS}
    out["lev_caps"] = {k: params_[k] for k in LEV_KEYS}
    out["deploy_full_pct"] = params_["DEPLOY_FULL_PCT"]
    # Grid search can evaluate hundreds of candidates.  Retaining every position, open-position snapshot,
    # and equity-curve point for every candidate exhausts a 512MB VPS even though ranking only consumes the
    # compact summary below.
    if primary_only:
        result = evaluate_portfolio_window(
            db, addrs, sigmas, overrides, now_ms, window_fills={30: list((window_fills or {}).get(30) or [])},
            days=30, market_ctx=market_ctx, path_rows=path_rows, path_meta=path_meta,
        )
        out["windows"] = {30: _compact_backtest(result)}
    else:
        out["windows"] = {
            days: _compact_backtest(result)
            for days, result in _candidate_windows(
                db, addrs, sigmas, overrides, now_ms, window_fills=window_fills,
                market_ctx=market_ctx, path_rows=path_rows, path_meta=path_meta,
            ).items()
        }
    return out


def evaluate_add_candidate(db, addrs: list[str], follow: dict, candidate: dict,
                           sigmas: dict | None = None, now_ms: int | None = None,
                           window_fills: dict[int, list[dict]] | None = None,
                           path_rows: list[dict] | None = None, path_meta: dict | None = None,
                           market_ctx: dict | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    overrides = {**follow_overrides_for_add_candidate(follow, candidate),
                 "AMBIGUOUS_PATH_MODE": "liquidate"}
    params_ = {k: overrides[k] for k in ADD_TUNE_KEYS}
    sigmas = sigmas if sigmas is not None else _load_sigmas(db)
    out = dict(candidate)
    out["params"] = params_
    out["add_params"] = params_
    out["windows"] = {
        days: _compact_backtest(result)
        for days, result in _candidate_windows(
            db, addrs, sigmas, overrides, now_ms, window_fills=window_fills,
            market_ctx=market_ctx, path_rows=path_rows, path_meta=path_meta,
        ).items()
    }
    return out


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
                baseline: dict, result: dict, *, generation_id: str | None = None) -> None:
    generation_id = generation_id or selection.latest_published_generation(db)
    mode = str(result.get("mode") or "shadow")
    proposal = result.get("proposal") or (_compact_candidate(selected) if selected else {})
    validation = result.get("validation") or {}
    created_at = now_iso()
    db.execute(
        "INSERT INTO auto_tune_runs "
        "(source,stamp,generation,mode,status,selected_mult,applied,eligible_to_apply,followed_n,"
        "baseline_json,proposal_json,validation_json,result_json,applied_at,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            source,
            stamp,
            generation_id,
            mode,
            result.get("status"),
            float(selected.get("mult")) if selected and selected.get("mult") is not None else None,
            1 if applied else 0,
            1 if result.get("eligible_to_apply") else 0,
            followed_n,
            json.dumps(baseline, sort_keys=True),
            json.dumps(proposal, sort_keys=True, default=float),
            json.dumps(validation, sort_keys=True, default=float),
            json.dumps(result, sort_keys=True, default=float),
            created_at if applied else None,
            created_at,
        ),
    )


def _enqueue_reload(db, source: str) -> None:
    db.execute(
        "INSERT INTO commands (type,payload_json,owner,status,created_at) VALUES (?,?,?,'pending',?)",
        ("reload_params", json.dumps({"by": "auto_tune_margin", "source": source}), "auto_tune", now_iso()),
    )


def _compact_backtest(result: dict) -> dict:
    keys = (
        "closed_n", "open_n", "wins", "liquidations", "copy_win_rate",
        "copy_net_pnl", "closed_net_pnl", "unrealized_pnl", "fee_drag",
        "margin_equity_pct", "initial_margin_equity",
        "target_open_events", "opened_n", "open_fill_rate", "target_adds",
        "followed_adds", "missed_adds", "missed_add_rate", "add_dependency",
        "add_metrics_version", "add_outcome_counts", "raw_add_order_follow_rate",
        "noise_merged_adds", "blocked_adds", "actionable_add_orders",
        "actionable_add_capture_rate", "true_blocked_add_rate", "add_episode_count",
        "entry_gap_sigma_weighted", "entry_gap_sigma_p90", "entry_gap_pct_weighted",
        "entry_gap_pct_p90", "entry_alignment", "add_execution", "add_fidelity",
        "add_fidelity_applied", "behavior_replication_v2", "behavior_replication_rate",
        "profit_factor", "payoff_ratio", "net_after_top1", "net_after_top2",
        "top1_profit_share", "top3_profit_share", "cost_stress_net_pnl",
        "body_after_top3_n", "body_after_top3_wins", "body_after_top3_losses",
        "body_after_top3_win_rate", "body_after_top3_net_pnl",
        "body_after_top3_profit_factor", "body_after_top3_payoff_ratio",
        "body_after_top3_median_pnl",
        "target_peak_concurrent", "copy_peak_concurrent", "max_concurrent_fit",
        "capacity_open_fit", "master_leverage_coverage", "master_leverage_known",
        "master_leverage_missing", "price_path_coverage", "model_coverage", "max_drawdown",
        "maintenance_margin_coverage", "maintenance_margin_known", "maintenance_margin_missing",
        "worst_day", "cvar95", "peak_deploy_pct", "avg_deploy_pct", "actionable_open_rate",
        "execution_fill_rate", "fee_slippage_drag", "pnl_concentration", "fallback_reasons", "fills",
        "ambiguous_liquidations", "price_path_boundary_skips",
    )
    out = {k: result.get(k) for k in keys if k in result}
    out["skip_reasons"] = result.get("skip_reasons") or {}
    return out


def _compact_candidate(candidate: dict) -> dict:
    return {
        "mult": candidate.get("mult"),
        "gap_k": candidate.get("gap_k"),
        "pos_gap_k": candidate.get("pos_gap_k"),
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


def _iso_epoch(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _proposal_direction(current: dict, proposed: dict) -> tuple[int, ...]:
    keys = TUNE_KEYS + ADD_TUNE_KEYS
    out = []
    for key in keys:
        before, after = float(current.get(key, 0.0)), float(proposed.get(key, current.get(key, 0.0)))
        out.append(1 if after > before + 1e-9 else -1 if after < before - 1e-9 else 0)
    return tuple(out)


def _walk_forward_validation(addrs, follow, proposal, sigmas, window_fills, now_ms,
                             path_rows=None, path_meta=None, market_ctx=None) -> dict:
    """Three non-overlapping ten-day folds plus a conservative cost-stress holdout."""
    max_days = max(window_fills) if window_fills else 30
    fills = list((window_fills or {}).get(max_days) or [])
    # Current market metadata is required for max-leverage maintenance tiers. Historical liquidity snapshots
    # remain unavailable, but dropping metadata entirely makes maintenance coverage zero for every proposal.
    market_ctx = market_ctx or {}
    base_overrides = {**follow, "AMBIGUOUS_PATH_MODE": "liquidate"}
    proposal_overrides = {**follow, **proposal, "AMBIGUOUS_PATH_MODE": "liquidate"}
    folds = []
    warmup_ms = int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0) * 86400_000
    for older, newer in ((30, 20), (20, 10), (10, 0)):
        lo = now_ms - older * 86400_000
        hi = now_ms - newer * 86400_000 if newer else now_ms + 1
        fold_fills = [row for row in fills if lo - warmup_ms <= int(row.get("time") or 0) < hi]
        baseline_warm = run_backtest("portfolio", fold_fills, sigmas=sigmas, overrides=base_overrides,
                                     market_ctx=market_ctx, price_path=path_rows,
                                     price_path_meta=path_meta)
        challenger_warm = run_backtest("portfolio", fold_fills, sigmas=sigmas, overrides=proposal_overrides,
                                       market_ctx=market_ctx, price_path=path_rows,
                                       price_path_meta=path_meta)
        baseline = slice_backtest_result(baseline_warm, lo, window_days=10)
        challenger = slice_backtest_result(challenger_warm, lo, window_days=10)
        in_window_n = sum(lo <= int(row.get("time") or 0) < hi for row in fold_fills)
        folds.append({"baseline": baseline, "challenger": challenger, "fills": in_window_n})
    holdout_start = now_ms - 10 * 86400_000
    holdout_fills = [row for row in fills if int(row.get("time") or 0) >= holdout_start - warmup_ms]
    stress_warm = run_backtest(
        "portfolio",
        holdout_fills,
        sigmas=sigmas,
        overrides={**proposal_overrides, "REPLAY_COST_MULT": 1.5},
        market_ctx=market_ctx,
        price_path=path_rows,
        price_path_meta=path_meta,
    )
    stress = slice_backtest_result(stress_warm, holdout_start, window_days=10)
    baseline_stress_warm = run_backtest(
        "portfolio",
        holdout_fills,
        sigmas=sigmas,
        overrides={**base_overrides, "REPLAY_COST_MULT": 1.5},
        market_ctx=market_ctx,
        price_path=path_rows,
        price_path_meta=path_meta,
    )
    baseline_stress = slice_backtest_result(baseline_stress_warm, holdout_start, window_days=10)
    compact_folds = []
    wins = 0
    for index, fold in enumerate(folds):
        base, challenger = fold["baseline"], fold["challenger"]
        base_net = float(base.get("copy_net_pnl") or 0.0)
        challenger_net = float(challenger.get("copy_net_pnl") or 0.0)
        win = challenger_net > base_net
        wins += int(win)
        compact_folds.append({
            "fold": index + 1,
            "fills": fold["fills"],
            "baselineNet": base_net,
            "challengerNet": challenger_net,
            "baselineMaxDD": float(base.get("max_drawdown") or 0.0),
            "challengerMaxDD": float(challenger.get("max_drawdown") or 0.0),
            "baselineOpenRate": float(base.get("open_fill_rate") or 0.0),
            "challengerOpenRate": float(challenger.get("open_fill_rate") or 0.0),
            "baselineCapacityFit": float(base.get("capacity_open_fit") or 0.0),
            "challengerCapacityFit": float(challenger.get("capacity_open_fit") or 0.0),
            "baselineLiquidations": int(base.get("liquidations") or 0),
            "challengerLiquidations": int(challenger.get("liquidations") or 0),
            "win": win,
        })
    return {
        "folds": compact_folds,
        "foldWins": wins,
        "holdout": compact_folds[-1] if compact_folds else {},
        "baselineStressNet": float(baseline_stress.get("copy_net_pnl") or 0.0),
        "baselineStressLiquidations": int(baseline_stress.get("liquidations") or 0),
        "stressNet": float(stress.get("copy_net_pnl") or 0.0),
        "stressLiquidations": int(stress.get("liquidations") or 0),
        "masterLeverageCoverage": float(stress.get("master_leverage_coverage") or 0.0),
        "maintenanceMarginCoverage": float(stress.get("maintenance_margin_coverage") or 0.0),
        "pricePathCoverage": float(stress.get("price_path_coverage") or 0.0),
    }


def _proposal_apply_eligibility(db, addrs, follow, current, proposal, validation, stamp) -> dict:
    policy = load_copy_policy(follow)
    fingerprint = ",".join(sorted(addrs))
    direction = _proposal_direction(current, proposal)
    state = _json_load(_state_get(db, "proposal_validation_state"), {}) or {}
    same_core = state.get("fingerprint") == fingerprint
    same_direction = tuple(state.get("direction") or ()) == direction
    started_at = state.get("startedAt") if same_core else stamp
    direction_streak = int(state.get("directionStreak") or 0) + 1 if same_core and same_direction else 1
    _state_set(db, "proposal_validation_state", {
        "fingerprint": fingerprint,
        "direction": list(direction),
        "directionStreak": direction_streak,
        "startedAt": started_at,
        "lastAt": stamp,
    })
    now_ts = _iso_epoch(stamp) or time.time()
    shadow_days = max(0.0, (now_ts - (_iso_epoch(started_at) or now_ts)) / 86400.0)
    if addrs:
        marks = ",".join("?" for _ in addrs)
        row = db.execute(
            f"SELECT COUNT(*) FROM copy_position WHERE status!='open' AND lower(addr) IN ({marks})",
            tuple(sorted(addrs)),
        ).fetchone()
        forward_closed = int((row[0] if row else 0) or 0)
    else:
        forward_closed = 0
    last_apply = db.execute(
        "SELECT applied_at FROM auto_tune_runs WHERE applied=1 AND applied_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    cooldown_days = float(getattr(config, "AUTO_TUNE_APPLY_COOLDOWN_DAYS", 7))
    cooldown_ok = not last_apply or now_ts - (_iso_epoch(last_apply[0]) or 0) >= cooldown_days * 86400
    model = _model_validation(validation, policy)
    reasons = list(model["reasons"])
    relative_gain = model["relativeGain"]
    if shadow_days < policy.tune_min_shadow_days:
        reasons.append("shadow_days_insufficient")
    if forward_closed < policy.tune_min_forward_closed:
        reasons.append("forward_closed_insufficient")
    min_direction_streak = int(
        follow.get("AUTO_TUNE_MIN_DIRECTION_STREAK", config.AUTO_TUNE_MIN_DIRECTION_STREAK)
        if follow.get("AUTO_TUNE_MIN_DIRECTION_STREAK") is not None
        else config.AUTO_TUNE_MIN_DIRECTION_STREAK
    )
    if direction_streak < max(1, min_direction_streak):
        reasons.append("proposal_direction_unconfirmed")
    if not cooldown_ok:
        reasons.append("apply_cooldown")
    leverage_changed = any(abs(float(current.get(key, 0.0)) - float(proposal.get(key, current.get(key, 0.0)))) > 1e-9
                           for key in LEV_KEYS)
    master_coverage_floor = float(
        follow.get("AUTO_TUNE_MASTER_LEVERAGE_MIN_COVERAGE")
        if follow.get("AUTO_TUNE_MASTER_LEVERAGE_MIN_COVERAGE") is not None
        else getattr(config, "AUTO_TUNE_MASTER_LEVERAGE_MIN_COVERAGE", 0.80)
    )
    price_path_floor = float(
        follow.get("AUTO_TUNE_PRICE_PATH_MIN_COVERAGE")
        if follow.get("AUTO_TUNE_PRICE_PATH_MIN_COVERAGE") is not None
        else getattr(config, "AUTO_TUNE_PRICE_PATH_MIN_COVERAGE", 0.95)
    )
    price_path_floor = max(
        price_path_floor, float(getattr(config, "AUTO_TUNE_PRICE_PATH_MIN_COVERAGE", .95)),
    )
    if leverage_changed and validation.get("masterLeverageCoverage", 0.0) < master_coverage_floor:
        reasons.append("master_leverage_coverage_low")
    if leverage_changed and validation.get("pricePathCoverage", 0.0) < price_path_floor:
        reasons.append("price_path_coverage_low")
    if validation.get("maintenanceMarginCoverage", 0.0) < float(
        getattr(config, "CORE_MAINTENANCE_META_MIN_COVERAGE", .95)
    ):
        reasons.append("maintenance_margin_coverage_low")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "relativeGain": relative_gain,
        "shadowDays": shadow_days,
        "forwardClosed": forward_closed,
        "directionStreak": direction_streak,
        "cooldownOk": cooldown_ok,
        **validation,
    }


def _model_validation(validation: dict, policy) -> dict:
    """Pure historical replay validation used to compare every finalist."""
    folds = validation.get("folds") or []
    holdout = validation.get("holdout") or {}
    baseline_total = sum(float(fold.get("baselineNet") or 0.0) for fold in folds)
    challenger_total = sum(float(fold.get("challengerNet") or 0.0) for fold in folds)
    relative_gain = (challenger_total - baseline_total) / max(1.0, abs(baseline_total))
    reasons = []
    if validation.get("foldWins", 0) < 2:
        reasons.append("fewer_than_two_fold_wins")
    # The holdout is already fold three and therefore already participates in the two-of-three win rule.
    # Requiring it to beat baseline a second time made that one slice a hidden double veto.  It still must
    # be independently profitable; a proposal cannot buy older-window gains with a currently losing surface.
    if float(holdout.get("challengerNet") or 0.0) <= 0.0:
        reasons.append("holdout_not_profitable")
    if validation.get("stressNet", 0.0) <= 0:
        reasons.append("stress_not_profitable")
    if relative_gain < policy.tune_min_relative_gain:
        reasons.append("relative_gain_below_floor")
    max_fit_drop = float(getattr(config, "AUTO_TUNE_MARGIN_MAX_OPEN_FIT_DROP", 0.03))
    for fold in folds:
        base_open = float(fold.get("baselineOpenRate") or 0.0)
        candidate_open = float(fold.get("challengerOpenRate") or 0.0)
        required_open = max(
            0.70 if base_open >= 0.70 else 0.0,
            base_open - max_fit_drop,
        )
        if candidate_open < required_open:
            reasons.append("open_rate_below_floor")
            break
    for fold in folds:
        base_capacity = float(fold.get("baselineCapacityFit") or 0.0)
        candidate_capacity = float(fold.get("challengerCapacityFit") or 0.0)
        required_capacity = max(
            policy.min_capacity_fit if base_capacity >= policy.min_capacity_fit else 0.0,
            base_capacity - max_fit_drop,
        )
        if candidate_capacity < required_capacity:
            reasons.append("capacity_fit_below_floor")
            break
    return {"eligible": not reasons, "reasons": reasons, "relativeGain": relative_gain}


def _formation_model_validation(validation: dict, policy) -> dict:
    """Allow a lower-risk proposal to repair an otherwise unfundable wallet-count node.

    This is deliberately narrower than normal auto-tune validation: it applies only during dry-run Core
    formation.  If the active surface already funds the prefix, the ordinary profit-improvement rules remain
    authoritative.  If it does not, the proposal may win only by restoring every hard admission invariant.
    """
    folds = list(validation.get("folds") or ())

    def feasible(prefix, stress_key):
        if not folds:
            return False
        return (
            sum(float(row.get(f"{prefix}Net") or 0.0) for row in folds) > 0.0
            and float(validation.get(stress_key) or 0.0) > 0.0
            and max(float(row.get(f"{prefix}MaxDD") or 0.0) for row in folds) < 1.0
            and min(float(row.get(f"{prefix}OpenRate") or 0.0) for row in folds) >= 0.70
            and min(float(row.get(f"{prefix}CapacityFit") or 0.0) for row in folds) >= policy.min_capacity_fit
        )

    baseline_feasible = feasible("baseline", "baselineStressNet")
    challenger_feasible = feasible("challenger", "stressNet")
    normal = _model_validation(validation, policy)
    if baseline_feasible:
        return {**normal, "baselineFeasible": True, "challengerFeasible": challenger_feasible}
    if challenger_feasible:
        return {
            "eligible": True,
            "reasons": [],
            "relativeGain": normal["relativeGain"],
            "baselineFeasible": False,
            "challengerFeasible": True,
            "admissionRepair": True,
        }
    reasons = list(normal.get("reasons") or ())
    reasons.append("formation_admission_still_infeasible")
    return {
        **normal,
        "eligible": False,
        "reasons": list(dict.fromkeys(reasons)),
        "baselineFeasible": False,
        "challengerFeasible": False,
    }


def _maybe_rollback_applied(db, follow: dict, now_ms: int,
                            expected_generation: str | None = None,
                            expected_strategy_revision: str | None = None) -> dict | None:
    state = _json_load(_state_get(db, "active_tune_rollback"), {}) or {}
    if not state or state.get("resolved"):
        return None
    applied_ts = _iso_epoch(state.get("appliedAt"))
    if not applied_ts or now_ms / 1000.0 - applied_ts < 7 * 86400:
        return {"status": "pending", "reason": "rollback_observation_window"}
    addrs = list(state.get("addrs") or [])
    if not addrs:
        return {"status": "skipped", "reason": "rollback_no_core_snapshot"}
    fills = _load_portfolio_fills(db, addrs, int(applied_ts * 1000))
    if not fills:
        return {"status": "pending", "reason": "rollback_no_forward_fills"}
    old_params = dict(state.get("oldParams") or {})
    current_params = {key: follow.get(key) for key in TUNE_KEYS + ADD_TUNE_KEYS if follow.get(key) is not None}
    sigmas = _load_sigmas(db, expected_generation)
    market_ctx = _load_market_ctx(db, expected_generation)
    champion = run_backtest(
        "portfolio", fills, sigmas=sigmas, market_ctx=market_ctx,
        overrides={**follow, **old_params},
    )
    applied = run_backtest(
        "portfolio", fills, sigmas=sigmas, market_ctx=market_ctx,
        overrides={**follow, **current_params},
    )
    old_net = float(champion.get("copy_net_pnl") or 0.0)
    new_net = float(applied.get("copy_net_pnl") or 0.0)
    utility_drop = old_net - new_net
    hurdle = max(1.0, abs(old_net) * float(getattr(config, "AUTO_TUNE_ROLLBACK_RELATIVE_DROP", 0.10)))
    should_rollback = utility_drop > hurdle
    if should_rollback:
        if expected_generation:
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            current_generation = selection.latest_published_generation(db)
            current_revision = strategy_revision.active_revision_id(db)
            if (current_generation != expected_generation
                    or (expected_strategy_revision is not None
                        and current_revision != expected_strategy_revision)):
                db.rollback()
                return {
                    "status": "skipped",
                    "reason": ("generation_changed_before_rollback"
                               if current_generation != expected_generation
                               else "strategy_revision_changed_before_rollback"),
                    "expectedGeneration": expected_generation,
                    "currentGeneration": current_generation,
                    "expectedStrategyRevision": expected_strategy_revision,
                    "currentStrategyRevision": current_revision,
                }
        _write_tune_params(db, old_params)
        _write_add_params(db, old_params)
        reason = "forward_utility_drop"
        rollback_revision = None
        if expected_generation:
            parent = strategy_revision.load_active(db)
            market_validation = generation_market.summary(db, expected_generation)
            rollback_revision = strategy_revision.create_revision(
                db,
                expected_generation,
                source="auto_tune_rollback",
                parent_revision=expected_strategy_revision,
                targets=(parent or {}).get("targets"),
                validation={
                    "rollbackReason": reason, "oldNet": old_net, "newNet": new_net,
                    "marketSnapshot": market_validation,
                },
                reason=reason,
                expected_active_revision=expected_strategy_revision,
            )
        else:
            _enqueue_reload(db, "auto_tune_rollback")
        db.execute(
            "UPDATE auto_tune_runs SET rollback_at=?,rollback_reason=? "
            "WHERE id=(SELECT id FROM auto_tune_runs WHERE applied=1 ORDER BY id DESC LIMIT 1)",
            (now_iso(), reason),
        )
        state.update(resolved=True, rolledBack=True, rollbackReason=reason, rollbackAt=now_iso())
        _state_set(db, "active_tune_rollback", state)
        if expected_generation:
            db.commit()
        return {"status": "rolled_back", "reason": reason, "oldNet": old_net, "newNet": new_net,
                "strategyRevision": (rollback_revision or {}).get("revision")}
    state.update(resolved=True, rolledBack=False, checkedAt=now_iso())
    _state_set(db, "active_tune_rollback", state)
    return {"status": "kept", "oldNet": old_net, "newNet": new_net}


def bind_active_tune_rollback_core(db, addrs) -> bool:
    """Move the forward champion/challenger check onto the sealed Core set."""
    state = _json_load(_state_get(db, "active_tune_rollback"), {}) or {}
    if not state or state.get("resolved"):
        return False
    state["addrs"] = sorted({(addr or "").lower() for addr in addrs if addr})
    _state_set(db, "active_tune_rollback", state)
    return True


def maybe_tune_margins(db, source: str = "scan", stamp: str | None = None, dry_run: bool = False,
                       mode: str | None = None, follow_values: dict | None = None,
                       data_complete: bool = True, expected_generation: str | None = None,
                       addrs_override: list[str] | tuple[str, ...] | None = None,
                       record_run: bool = True, formation_admission: bool = False,
                       market_generation: str | None = None, search_profile: str = "full",
                       time_budget_s: float | None = None) -> dict:
    """Run the post-scan margin tuner. Returns a compact audit dict."""
    ephemeral = addrs_override is not None
    if ephemeral and expected_generation:
        raise ValueError("addrs_override cannot target a published generation")
    search_profile = str(search_profile or "full").strip().lower()
    if search_profile not in {"coarse", "full"}:
        raise ValueError("search_profile must be coarse or full")
    coarse_search = search_profile == "coarse"
    tune_started = time.monotonic()
    time_budget_s = float(
        getattr(config, "AUTO_TUNE_TIME_BUDGET_SEC", 1800)
        if time_budget_s is None else time_budget_s
    )
    deadline = (
        float("inf") if time_budget_s <= 0 else tune_started + time_budget_s
    )

    def check_budget(stage):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"auto_tune_time_budget:{stage}")

    stamp = stamp or now_iso()
    params.seed_params(db)
    expected_strategy_revision = None
    if expected_generation:
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        current_generation = selection.latest_published_generation(db)
        if current_generation != expected_generation:
            db.rollback()
            return {
                "status": "skipped",
                "reason": "generation_changed_before_tune",
                "expectedGeneration": expected_generation,
                "currentGeneration": current_generation,
                "applied": False,
            }
        active_bundle = strategy_revision.load_active(db)
        if not active_bundle or active_bundle.get("selectionGeneration") != expected_generation:
            strategy_revision.materialize_current(
                db,
                source="tuner_generation_bridge",
                reason="rolling_deploy_generation_bridge",
                enqueue_reload=False,
            )
            active_bundle = strategy_revision.load_active(db)
        expected_strategy_revision = (active_bundle or {}).get("revision")
        active_follow = dict((active_bundle or {}).get("params") or {})
        db.commit()
    else:
        active_follow = {}
    follow = dict(follow_values or active_follow or params.load_follow(db))
    mode = str(mode or follow.get("AUTO_TUNE_MODE") or getattr(config, "AUTO_TUNE_MODE", "shadow")).lower()
    if mode not in {"off", "shadow", "apply"}:
        mode = "shadow"
    effective_shadow = bool(dry_run or mode != "apply")
    rollback_result = None if ephemeral else _maybe_rollback_applied(
        db, follow, int(time.time() * 1000), expected_generation=expected_generation,
        expected_strategy_revision=expected_strategy_revision,
    )
    if rollback_result and rollback_result.get("reason") in {
        "generation_changed_before_rollback", "strategy_revision_changed_before_rollback",
    }:
        result = {
            **rollback_result,
            "mode": mode,
            "shadow": True,
            "applied": False,
        }
        if record_run:
            _record_run(
                db, source, stamp, None, False, 0, {}, result,
                generation_id=expected_generation,
            )
        db.commit()
        return result
    if rollback_result and rollback_result.get("status") == "rolled_back":
        follow = dict(params.load_follow(db))
        expected_strategy_revision = (
            rollback_result.get("strategyRevision") or strategy_revision.active_revision_id(db)
        )
    if mode == "off":
        result = {"status": "disabled", "reason": "auto_tune_mode_off", "mode": mode, "applied": False}
        if record_run:
            _record_run(db, source, stamp, None, False, 0, {}, result,
                        generation_id=expected_generation)
        db.commit()
        return result
    if not follow.get("AUTO_TUNE_MARGIN_ENABLE", getattr(config, "AUTO_TUNE_MARGIN_ENABLE", True)):
        result = {"status": "disabled", "mode": mode, "applied": False}
        if record_run:
            _record_run(db, source, stamp, None, False, 0, {}, result,
                        generation_id=expected_generation)
        db.commit()
        return result

    addrs = (
        list(dict.fromkeys((addr or "").lower() for addr in addrs_override if addr))
        if ephemeral else _load_followed_wallets(db, follow)
    )
    if len(addrs) < int(getattr(config, "AUTO_TUNE_MARGIN_MIN_FOLLOWED", 1)):
        result = {"status": "no_followed_wallets", "applied": False, "followed_n": len(addrs)}
        if record_run:
            _record_run(db, source, stamp, None, False, len(addrs), {}, result,
                        generation_id=expected_generation)
        db.commit()
        return result

    current = {k: float(follow[k]) for k in TUNE_KEYS}
    # Every generation optimizes against the parameters that are actually active in Observer and displayed
    # by the dashboard. A historical manual baseline can be useful for rollback bookkeeping, but using it as
    # the candidate comparator silently discards the entire neighbourhood above that old leverage surface.
    base = enforce_margin_add_capacity(current, follow)
    market_generation = market_generation or expected_generation
    sigmas = _load_sigmas(db, market_generation)
    now_ms = int(time.time() * 1000)
    window_fills = _portfolio_window_fills(
        db, addrs, now_ms, include_watch=bool(formation_admission),
    )
    if window_fills is None:
        result = {
            "status": "skipped",
            "reason": "fill_cache_guard",
            "mode": mode,
            "applied": False,
            "followed_n": len(addrs),
        }
        if record_run:
            _record_run(db, source, stamp, None, False, len(addrs), base, result,
                        generation_id=expected_generation)
        db.commit()
        return result
    if not data_complete or not any(window_fills.values()):
        result = {
            "status": "skipped",
            "reason": "incomplete_data" if not data_complete else "no_cached_fills",
            "mode": mode,
            "applied": False,
            "followed_n": len(addrs),
        }
        if record_run:
            _record_run(db, source, stamp, None, False, len(addrs), base, result,
                        generation_id=expected_generation)
        db.commit()
        return result
    path_fills = list(window_fills.get(max(window_fills)) or [])
    path_start = now_ms - (max(window_fills) + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7))) * 86_400_000
    market_ctx = _load_market_ctx(db, market_generation)
    path_rows, path_meta = prepare_refined_price_path(
        db, path_fills, path_start, now_ms, sigmas=sigmas, overrides=follow,
        market_ctx=market_ctx, immutable_market_ctx=bool(market_generation),
    )
    # First tune stable/mid/high independently, including upward high-tier probes, then combine only each
    # tier's current/best-profit/fewest-liquidation values. This preserves tier attribution without paying
    # for a full leverage Cartesian grid.
    axis_quick = []
    leverage_axis_candidates = (
        coarse_leverage_candidates(base) if coarse_search else independent_leverage_candidates(base)
    )
    for candidate in leverage_axis_candidates:
        check_budget("leverage_axes")
        axis_quick.append(evaluate_tune_candidate(
            db, addrs, follow, candidate, sigmas=sigmas, now_ms=now_ms,
            window_fills=window_fills, path_rows=path_rows, path_meta=path_meta,
            primary_only=True, market_ctx=market_ctx,
        ))
    quick_baseline = next(
        (candidate for candidate in axis_quick if _same_tune_values(candidate.get("params") or {}, base)),
        axis_quick[0],
    )
    shortlist_limit = 1 if coarse_search else max(
        1, int(getattr(config, "AUTO_TUNE_LEVERAGE_SHORTLIST", 2) or 2)
    )
    tier_values = {
        key: _tier_leverage_shortlist(axis_quick, quick_baseline, key, limit=shortlist_limit)
        for key in LEV_KEYS
    }
    combo_quick = []
    for values in itertools.product(*(tier_values[key] for key in LEV_KEYS)):
        check_budget("leverage_combinations")
        candidate = _candidate_from_params(
            {**base, **dict(zip(LEV_KEYS, values))}, axis="leverage_combination",
        )
        combo_quick.append(evaluate_tune_candidate(
            db, addrs, follow, candidate, sigmas=sigmas, now_ms=now_ms,
            window_fills=window_fills, path_rows=path_rows, path_meta=path_meta,
            primary_only=True, market_ctx=market_ctx,
        ))
    joint_quick = axis_quick + combo_quick
    quick_valid = [candidate for candidate in joint_quick if _candidate_valid(candidate, quick_baseline)]
    sizing_limit = 2 if coarse_search else max(
        2, int(getattr(config, "AUTO_TUNE_SIZING_FINALISTS", 12) or 12)
    )
    quick_finalists = sorted(
        quick_valid or [quick_baseline],
        key=lambda candidate: _candidate_rank_key(candidate, quick_baseline), reverse=True,
    )[:sizing_limit]
    if not any(_same_tune_values(candidate.get("params") or {}, base) for candidate in quick_finalists):
        quick_finalists.append(quick_baseline)
    joint_candidates = []
    for candidate in quick_finalists:
        check_budget("joint_finalists")
        joint_candidates.append(evaluate_tune_candidate(
            db, addrs, follow,
            _candidate_from_params(candidate.get("params") or base, axis="joint_finalist"),
            sigmas=sigmas, now_ms=now_ms, window_fills=window_fills,
            path_rows=path_rows, path_meta=path_meta, market_ctx=market_ctx,
        ))
    baseline = next(
        (candidate for candidate in joint_candidates if _same_tune_values(candidate.get("params") or {}, base)),
        joint_candidates[-1],
    )
    selected_joint = choose_margin_candidate(joint_candidates, baseline)
    joint_params = selected_joint.get("params") or base
    # Bounded coordinate closure: one independent sweep can raise only one volatility tier.  Rebuild the
    # same small neighbourhood around its winner so combinations such as high+stable are actually tested,
    # without restoring the expensive three-tier Cartesian grid.  Two rounds means at most two accepted
    # tier moves and remains finite even when every move improves in-sample profit.
    margin_candidates = []
    margin_rounds = []
    for candidate in capacity_margin_candidates(joint_params, follow):
        check_budget("capacity_margin_grid")
        margin_candidates.append(evaluate_tune_candidate(
            db, addrs, follow, candidate, sigmas=sigmas, now_ms=now_ms,
            window_fills=window_fills, path_rows=path_rows, path_meta=path_meta,
            market_ctx=market_ctx,
        ))
    if formation_admission and not coarse_search:
        for candidate in global_margin_candidates(joint_params, follow):
            check_budget("global_margin_polish")
            margin_candidates.append(evaluate_tune_candidate(
                db, addrs, follow, candidate, sigmas=sigmas, now_ms=now_ms,
                window_fills=window_fills, path_rows=path_rows, path_meta=path_meta,
                market_ctx=market_ctx,
            ))
    selected_margin_seed = choose_margin_candidate(
        [selected_joint, *margin_candidates], baseline,
    )
    margin_params = dict(selected_margin_seed.get("params") or joint_params)
    margin_round_limit = 0 if coarse_search else max(
        1, int(getattr(config, "AUTO_TUNE_MARGIN_COORD_ROUNDS", 2) or 2)
    )
    for round_index in range(margin_round_limit):
        round_candidates = []
        for candidate in independent_margin_candidates(margin_params, follow):
            check_budget("margin_polish")
            round_candidates.append(evaluate_tune_candidate(
                db, addrs, follow, candidate, sigmas=sigmas, now_ms=now_ms,
                window_fills=window_fills, path_rows=path_rows, path_meta=path_meta,
                market_ctx=market_ctx,
            ))
        if not round_candidates:
            break
        margin_candidates.extend(round_candidates)
        margin_baseline = next(
            (candidate for candidate in round_candidates
             if _same_tune_values(candidate.get("params") or {}, margin_params)),
            round_candidates[0],
        )
        selected_margin = choose_margin_candidate(round_candidates, margin_baseline)
        next_params = dict(selected_margin.get("params") or margin_params)
        changed = not _same_margin_values(next_params, margin_params)
        margin_rounds.append({
            "round": round_index + 1,
            "candidates": len(round_candidates),
            "changed": changed,
            "params": {key: float(next_params[key]) for key in MARGIN_KEYS},
        })
        margin_params = next_params
        if not changed:
            break
    deploy_polish = []
    for candidate in deploy_candidates(margin_params):
        check_budget("deploy_polish")
        deploy_polish.append(evaluate_tune_candidate(
            db, addrs, follow, candidate, sigmas=sigmas, now_ms=now_ms,
            window_fills=window_fills, path_rows=path_rows, path_meta=path_meta,
            market_ctx=market_ctx,
        ))
    selected = choose_margin_candidate(
        joint_candidates + margin_candidates + deploy_polish + [baseline], baseline,
    )
    candidates = joint_candidates + margin_candidates + deploy_polish
    selected_params = selected.get("params") or base
    selected_margins = {k: selected_params[k] for k in MARGIN_KEYS}

    follow_for_add = follow_overrides_for_tune_candidate(follow, selected)
    current_add = {k: float(follow[k]) for k in ADD_TUNE_KEYS}
    add_base = dict(current_add)
    add_candidates = []
    add_baseline = None
    selected_add = None
    selected_add_params = add_base
    if follow_for_add.get("SMART_ADD", True) and not coarse_search:
        for candidate in add_candidates_from_axes(add_base):
            check_budget("add_polish")
            add_candidates.append(evaluate_add_candidate(
                db, addrs, follow_for_add, candidate, sigmas=sigmas, now_ms=now_ms,
                window_fills=window_fills, path_rows=path_rows, path_meta=path_meta,
                market_ctx=market_ctx,
            ))
        add_baseline = next((c for c in add_candidates if _same_add_values(c.get("params") or {}, add_base)),
                            add_candidates[0] if add_candidates else None)
        selected_add = choose_margin_candidate(add_candidates, add_baseline) if add_baseline else None
        if selected_add:
            selected_add_params = selected_add.get("params") or add_base

    current_combined = {**current, **current_add}
    # Validate ranked sizing/add combinations, not only the most profitable in-sample pair. If the first
    # proposal fails, continue through alternative independent parameter combinations.
    unique_finalists = {}
    for candidate in sorted(candidates, key=lambda item: _candidate_rank_key(item, baseline), reverse=True):
        key = tuple(round(float((candidate.get("params") or {})[name]), 12) for name in TUNE_KEYS)
        unique_finalists.setdefault(key, candidate)
    finalist_limit = 2 if coarse_search else int(
        getattr(config, "AUTO_TUNE_FINALIST_LIMIT", 16) or 16
    )
    sizing_options = _diverse_sizing_candidates(
        list(unique_finalists.values()), baseline, max(1, finalist_limit),
    )
    if formation_admission and unique_finalists:
        # Reserve validation space for the best capacity-restoring surfaces.  Keep ordering stable and
        # deduplicate by the exact parameter tuple before building sizing/add combinations.
        admission_leaders = sorted(
            unique_finalists.values(),
            key=lambda item: _candidate_admission_rank_key(item, baseline),
            reverse=True,
        )[:(1 if coarse_search else 2)]
        combined_sizing = []
        seen_sizing = set()
        for candidate in [*admission_leaders, *sizing_options]:
            key = tuple(round(float((candidate.get("params") or {})[name]), 12) for name in TUNE_KEYS)
            if key in seen_sizing:
                continue
            seen_sizing.add(key)
            combined_sizing.append(candidate)
        sizing_options = combined_sizing[:max(1, finalist_limit)]
    if add_candidates and add_baseline:
        ranked_add = sorted(
            add_candidates,
            key=lambda item: _candidate_rank_key(item, add_baseline),
            reverse=True,
        )
        add_options = []
        seen_add = set()
        for candidate in ([selected_add, add_baseline] + ranked_add):
            if not candidate:
                continue
            params_ = candidate.get("params") or add_base
            key = tuple(round(float(params_[name]), 12) for name in ADD_TUNE_KEYS)
            if key not in seen_add:
                seen_add.add(key)
                add_options.append(params_)
            add_limit = 1 if coarse_search else max(
                1, int(getattr(config, "AUTO_TUNE_ADD_FINALISTS", 3) or 3)
            )
            if len(add_options) >= add_limit:
                break
    else:
        add_options = [selected_add_params]
    combined_options = sorted(
        (
            (sizing_rank + add_rank, sizing_rank, add_rank, sizing_candidate, add_params)
            for sizing_rank, sizing_candidate in enumerate(sizing_options)
            for add_rank, add_params in enumerate(add_options)
        ),
        key=lambda row: (row[0], row[1], row[2]),
    )[:max(1, finalist_limit)]
    finalist_results = []
    chosen = None
    for _rank, _sizing_rank, _add_rank, sizing_candidate, finalist_add_params in combined_options:
        check_budget("walk_forward")
        sizing_params = sizing_candidate.get("params") or base
        combined = {**sizing_params, **finalist_add_params}
        validation = _walk_forward_validation(
            addrs, follow, combined, sigmas, window_fills, now_ms,
            path_rows=path_rows, path_meta=path_meta, market_ctx=market_ctx,
        )
        model = (
            _formation_model_validation(validation, load_copy_policy(follow))
            if formation_admission else _model_validation(validation, load_copy_policy(follow))
        )
        finalist_results.append({
            "params": combined,
            "eligible": model["eligible"],
            "reasons": model["reasons"],
            "relativeGain": model["relativeGain"],
        })
        if model["eligible"]:
            chosen = (sizing_candidate, sizing_params, finalist_add_params, combined, validation)
            break
    no_validated_finalist = chosen is None
    if no_validated_finalist:
        # No proposal passed folds/holdout/stress. Return the exact active baseline for audit, never an
        # attractive but invalid in-sample fallback. Callers may safely retain it while publishing a Core
        # formed under current parameters.
        selected = baseline
        selected_params = {key: float(current[key]) for key in TUNE_KEYS}
        selected_add_params = {key: float(current_add[key]) for key in ADD_TUNE_KEYS}
        combined = {**selected_params, **selected_add_params}
        validation = _walk_forward_validation(
            addrs, follow, combined, sigmas, window_fills, now_ms,
            path_rows=path_rows, path_meta=path_meta, market_ctx=market_ctx,
        )
        chosen = (selected, selected_params, selected_add_params, combined, validation)
    selected, selected_params, selected_add_params, proposal_combined, walk_forward = chosen
    selected_margins = {key: selected_params[key] for key in MARGIN_KEYS}
    follow_for_add = follow_overrides_for_tune_candidate(follow, selected)
    if ephemeral:
        model = (
            _formation_model_validation(walk_forward, load_copy_policy(follow))
            if formation_admission else _model_validation(walk_forward, load_copy_policy(follow))
        )
        validation_reasons = list(model.get("reasons") or ())
        if no_validated_finalist:
            # The fallback compares the active baseline with itself, so its zero-gain fold diagnostics are
            # mathematically inevitable and say nothing about why the actual proposals failed.  Preserve a
            # truthful aggregate of finalist failures instead of publishing that misleading self-comparison.
            validation_reasons = ["no_validated_tune_finalist"]
            for item in finalist_results:
                validation_reasons.extend(item.get("reasons") or ())
            validation_reasons = list(dict.fromkeys(validation_reasons))
        apply_validation = {
            "eligible": bool(model.get("eligible")) and not no_validated_finalist,
            "reasons": validation_reasons,
            "relativeGain": float(model.get("relativeGain") or 0.0),
            **walk_forward,
        }
    elif no_validated_finalist:
        validation_reasons = ["no_validated_tune_finalist"]
        for item in finalist_results:
            validation_reasons.extend(item.get("reasons") or ())
        apply_validation = {
            "eligible": False,
            "reasons": list(dict.fromkeys(validation_reasons)),
            "relativeGain": 0.0,
            **walk_forward,
        }
    else:
        apply_validation = _proposal_apply_eligibility(
            db, addrs, follow, current_combined, proposal_combined, walk_forward, stamp,
        )
    effective_shadow = bool(effective_shadow or not apply_validation.get("eligible"))

    # Tuning is expensive and runs outside the scanner process.  The generation can change while the
    # proposal is being evaluated, so the startup check is insufficient.  Commit harmless validation
    # bookkeeping, then take SQLite's writer lock and re-check immediately before touching live params.
    # A scanner publication now either happens before this check (and makes us stale) or after our complete
    # params/reload transaction; an old Core can never leak its tuning surface into a newer generation.
    if expected_generation:
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        current_generation = selection.latest_published_generation(db)
        current_revision = strategy_revision.active_revision_id(db)
        if (current_generation != expected_generation
                or current_revision != expected_strategy_revision):
            result = {
                "status": "skipped",
                "reason": ("generation_changed_before_apply"
                           if current_generation != expected_generation
                           else "strategy_revision_changed_before_apply"),
                "mode": mode,
                "shadow": True,
                "applied": False,
                "expectedGeneration": expected_generation,
                "currentGeneration": current_generation,
                "expectedStrategyRevision": expected_strategy_revision,
                "currentStrategyRevision": current_revision,
                "followed_n": len(addrs),
                "proposal": proposal_combined,
                "validation": apply_validation,
            }
            _record_run(
                db, source, stamp, selected, False, len(addrs), base, result,
                generation_id=expected_generation,
            )
            db.commit()
            return result

    applied_sizing = False
    applied_add = False
    will_apply = (
        not effective_shadow
        and (
            not _same_tune_values(current, selected_params)
            or (follow_for_add.get("SMART_ADD", True) and not _same_add_values(current_add, selected_add_params))
        )
    )
    if will_apply:
        _state_set(db, "active_tune_rollback", {
            "appliedAt": stamp,
            "addrs": sorted(addrs),
            "oldParams": current_combined,
            "newParams": proposal_combined,
            "resolved": False,
        })
    if not effective_shadow and not _same_tune_values(current, selected_params):
        _write_tune_params(db, selected_params)
        applied_sizing = True
    if not effective_shadow and follow_for_add.get("SMART_ADD", True) and not _same_add_values(current_add, selected_add_params):
        _write_add_params(db, selected_add_params)
        applied_add = True
    applied = applied_sizing or applied_add
    applied_revision = None
    if not effective_shadow and applied:
        if expected_generation:
            parent_bundle = strategy_revision.load_active(db)
            market_validation = generation_market.summary(db, expected_generation)
            applied_revision = strategy_revision.create_revision(
                db,
                expected_generation,
                source="auto_tune",
                parent_revision=expected_strategy_revision,
                targets=(parent_bundle or {}).get("targets"),
                validation={**apply_validation, "marketSnapshot": market_validation},
                reason="validated_portfolio_tune",
                expected_active_revision=expected_strategy_revision,
                # The generation-bound caller seals the new parameters together
                # with a membership consistency pass.  Keep this audit bundle
                # staged: Observer must never see params/new + targets/old, even
                # if it restarts while the strict membership pass is running.
                activate=False,
                enqueue_reload=False,
            )
        else:
            _enqueue_reload(db, source)
    result = {
        "status": "ok",
        "mode": mode,
        "shadow": effective_shadow,
        "applied": applied,
        "applied_sizing": applied_sizing,
        "applied_add": applied_add,
        "followed_n": len(addrs),
        "selected_mult": None,
        "margins": selected_margins,
        "smart_add_capacity": {
            "reserved_adds": int(getattr(config, "SMART_ADD_MIN_CAPACITY", 4) or 4),
            "margin_ceilings": margin_add_capacity_ceilings(follow),
        },
        "lev_caps": selected.get("lev_caps"),
        "deploy_full_pct": selected.get("deploy_full_pct"),
        "params": selected_params,
        "add_params": selected_add_params,
        "eligible_to_apply": bool(apply_validation.get("eligible")),
        "validation": apply_validation,
        "proposal": proposal_combined,
        "baseline_proposal": current_combined,
        "strategyRevision": (applied_revision or {}).get("revision"),
        "parentStrategyRevision": expected_strategy_revision,
        "reloadDeferredForSelection": bool(applied_revision),
        "finalists": finalist_results,
        "rollback": rollback_result,
        "candidates": [_compact_candidate(c) for c in candidates],
        "add_candidates": [_compact_candidate(c) for c in add_candidates],
        "margin_rounds": margin_rounds,
        "formation_admission": bool(formation_admission),
        "search_profile": search_profile,
        "elapsed_s": round(time.monotonic() - tune_started, 3),
    }
    if record_run:
        _record_run(db, source, stamp, selected, applied, len(addrs), base, result,
                    generation_id=expected_generation)
    db.commit()
    return result
