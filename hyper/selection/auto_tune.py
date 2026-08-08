"""Post-scan portfolio auto-tuning for copy-trading sizing.

The tuner adjusts the operator-approved sizing surface: first-open margins,
tier leverage caps, and the smart-add core knobs. The aggregate new-open
deployment line, per-coin caps and exit rules remain operator-owned limits.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import sqlite3
import time
from datetime import datetime
from typing import Callable, Iterable

from hyper import config, params
from hyper.copy.copy_backtest import (
    prepare_price_path,
    prepare_replay_fills,
    run_backtest,
    slice_prepared_replay_fills,
    slice_backtest_result,
    subset_price_path,
)
from hyper.copy.copy_data import load_copyable_fills
from hyper.copy.copy_policy import load_copy_policy
from hyper.copy import replay_parallel
from hyper.copy.economics import (
    open_loss_ratio_within_limit,
    replay_result_profitability,
)
from hyper.copy.sector import parse_json_obj
from hyper.market import generation_market, price_path
from hyper.execution.mode import selected_book
from hyper.ops import resource_guard
from hyper.util import f, now_iso
from . import state as selection, strategy_revision

MARGIN_KEYS = ("STABLE_MARGIN_PCT", "MID_MARGIN_PCT", "HIGH_MARGIN_PCT")
MARGIN_GRID_STEP = 0.005
COIN_CAP_KEYS = ("STABLE_COIN_CAP_PCT", "MID_COIN_CAP_PCT", "HIGH_COIN_CAP_PCT")
LEV_KEYS = ("STABLE_LEV_CAP", "MID_LEV_CAP", "HIGH_LEV_CAP")
DEPLOY_KEYS = ()
CONGESTION_PCT_KEYS = ()
CONGESTION_INT_KEYS = ()
TUNE_KEYS = MARGIN_KEYS + LEV_KEYS + DEPLOY_KEYS + CONGESTION_PCT_KEYS + CONGESTION_INT_KEYS
ADD_TUNE_KEYS = ("ADD_GAP_K", "POS_ADD_GAP_K", "ADD_GAP_SHRINK_G", "ADD_MAX_HARD")
CAPACITY_SKIP_KEYS = (
    "skip_coin_full", "skip_no_cash", "skip_deploy_cap", "skip_margin_too_small",
    "skip_wallet_full", "skip_wallet_sector_side_full", "skip_wallet_position_cap",
    "skip_wallet_stock_side_position_cap",
)
_PROCESS_REPLAY_CONTEXT = {}


def _init_process_replay_context(context: dict) -> None:
    global _PROCESS_REPLAY_CONTEXT
    _PROCESS_REPLAY_CONTEXT = context


def _evaluate_candidate_process(task):
    kind, follow, candidate, primary_only = task
    context = _PROCESS_REPLAY_CONTEXT
    common = {
        "sigmas": context["sigmas"],
        "now_ms": context["now_ms"],
        "window_fills": context["window_fills"],
        "path_rows": context["path_rows"],
        "path_meta": context["path_meta"],
        "market_ctx": context["market_ctx"],
    }
    if kind == "add":
        return evaluate_add_candidate(
            None, context["addrs"], follow, candidate,
            primary_only=bool(primary_only), **common,
        )
    return evaluate_tune_candidate(
        None, context["addrs"], follow, candidate,
        primary_only=bool(primary_only), **common,
    )


def _evaluate_walk_forward_surface_process(overrides):
    context = _PROCESS_REPLAY_CONTEXT
    return _walk_forward_surface(
        overrides,
        sigmas=context["sigmas"],
        market_ctx=context["market_ctx"],
        path_meta=context["path_meta"],
        prepared=context["walk_forward"],
    )


def margin_add_capacity_ceilings(follow: dict) -> dict[str, float]:
    """Per-tier margin ceilings that preserve the configured smart-add slots."""
    margin_equity_pct = max(1e-9, float(
        follow.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
    ))
    add_capacity = max(1, int(getattr(config, "SMART_ADD_MIN_CAPACITY", 2) or 2))
    return {
        margin_key: max(0.0, float(
            follow.get(cap_key, getattr(config, cap_key))
        ) / ((add_capacity + 1) * margin_equity_pct))
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
            "portfolio", fills, sigmas=sigmas,
            overrides={**overrides, "_PATH_REFINEMENT_PROBE": True},
            market_ctx=market_ctx, price_path=prepare_price_path(rows), price_path_meta=meta,
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
    return float(replay_result_profitability(result).get("qualificationPnl") or 0.0)


def _candidate_score(candidate: dict) -> float:
    """Primary tuning objective: rolling-equity 30d net profit.

    The shorter windows remain mandatory validity/strict-replay checks, but adding them again here would
    triple-count recent trades and could select a lower 30d-profit surface merely because the same final
    week appears in the 30d, 14d and 7d windows.
    """
    windows = candidate.get("windows") or {}
    primary = windows.get(30) or (windows.get(max(windows)) if windows else {})
    return _result_pnl(primary)


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
        if _enough_sample(result, int(days)):
            economics = replay_result_profitability(result)
            if _result_pnl(result) <= 0:
                return False
            if int(days) == 30 and not open_loss_ratio_within_limit(economics):
                return False
    return True


def _candidate_distance(candidate: dict, baseline: dict) -> float:
    params_ = candidate.get("params") or {}
    base_params = baseline.get("params") or {}
    if not params_ or not base_params:
        return abs(float(candidate.get("mult") or 1.0) - float(baseline.get("mult") or 1.0))
    keys = tuple(candidate.get("distance_keys") or baseline.get("distance_keys") or TUNE_KEYS)
    return sum(abs(float(params_.get(k, 0.0)) - float(base_params.get(k, 0.0))) for k in keys)


def _candidate_liquidations(candidate: dict) -> int:
    windows = candidate.get("windows") or {}
    primary = windows.get(30) or (windows.get(max(windows)) if windows else {})
    return int(primary.get("liquidations") or 0)


def _candidate_execution_priority(candidate: dict) -> tuple:
    """Prefer fundable opens and faithful adds after profit/liquidation risk are comparable."""
    windows = candidate.get("windows") or {}
    usable = list(windows.values())
    if not usable:
        return (0.0, 0.0, 0.0, -1.0)
    capacity = min((_capacity_fit(result) for result in usable), default=0.0)
    open_rate = min(
        (
            float(
                result.get("actionable_open_rate")
                if result.get("actionable_open_rate") is not None
                else result.get("open_fill_rate") or 0.0
            )
            for result in usable
        ),
        default=0.0,
    )
    add_capture_values = [
        float(result.get("actionable_add_capture_rate"))
        for result in usable if result.get("actionable_add_capture_rate") is not None
    ]
    blocked_values = [
        float(result.get("true_blocked_add_rate"))
        for result in usable if result.get("true_blocked_add_rate") is not None
    ]
    # Missing add evidence is neutral. It must not outrank a measured candidate, but it also must not make
    # sizing-only candidates invalid before an add surface has been evaluated.
    add_capture = min(add_capture_values) if add_capture_values else 0.0
    blocked = max(blocked_values) if blocked_values else 0.0
    return (capacity, open_rate, add_capture, -blocked)


def _candidate_rank_key(candidate: dict, baseline: dict) -> tuple:
    windows = candidate.get("windows") or {}
    weighted_net = _candidate_score(candidate)
    liquidations = _candidate_liquidations(candidate)
    primary = windows.get(30) or (windows.get(max(windows)) if windows else {})
    primary_net = _result_pnl(primary)
    primary_equity = float(
        primary.get("window_start_equity")
        or primary.get("initial_margin_equity")
        or config.INITIAL_BALANCE
    )
    liquidation_penalty = (
        liquidations
        * primary_equity
        * float(getattr(config, "AUTO_TUNE_LIQUIDATION_PENALTY_PCT", 0.05))
    )
    risk_ordered_net = weighted_net - liquidation_penalty
    return (
        risk_ordered_net,
        weighted_net,
        -liquidations,
        *_candidate_execution_priority(candidate),
        primary_net,
        _result_pnl(windows.get(14, {})),
        _result_pnl(windows.get(7, {})),
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
    """Choose profit first, then liquidations, congestion and add quality inside a near-best band."""
    valid = [c for c in candidates if _candidate_valid(c, baseline)]
    if not valid:
        return baseline
    if not any(candidate is baseline for candidate in valid) and _candidate_valid(baseline, baseline):
        valid.append(baseline)
    best_profit = max(_candidate_score(candidate) for candidate in valid)
    tolerance = abs(best_profit) * float(
        getattr(config, "AUTO_TUNE_NEAR_BEST_PROFIT_REL", 0.08)
    )
    near_best = [
        candidate for candidate in valid
        if _candidate_score(candidate) + tolerance + 1e-9 >= best_profit
    ]
    preferred_liquidations = int(
        getattr(config, "AUTO_TUNE_PREFERRED_LIQUIDATIONS_30D", 3)
    )
    preferred = [
        candidate for candidate in near_best
        if _candidate_liquidations(candidate) <= preferred_liquidations
    ]
    pool = preferred or near_best
    return max(pool, key=lambda candidate: (
        -_candidate_liquidations(candidate),
        *_candidate_execution_priority(candidate),
        _candidate_score(candidate),
        _result_pnl((candidate.get("windows") or {}).get(30, {})),
        -_candidate_distance(candidate, baseline),
    ))


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


def _efficient_pareto_sizing_candidates(
    candidates: list[dict], baseline: dict, limit: int,
) -> list[dict]:
    """Reserve final validation slots for profit, liquidation, capacity and the active baseline.

    Risk/capacity champions must remain inside the configured near-best-profit band.  A globally safest
    surface that gives away materially more profit cannot consume one of the few expensive path-validation
    slots merely by under-deploying the account.
    """
    rows = [candidate for candidate in candidates if _candidate_valid(candidate, baseline)]
    if not rows:
        rows = [baseline]
    best_profit = max(_candidate_score(candidate) for candidate in rows)
    tolerance = abs(best_profit) * float(
        getattr(config, "AUTO_TUNE_NEAR_BEST_PROFIT_REL", 0.08)
    )
    near_best = [
        candidate for candidate in rows
        if _candidate_score(candidate) + tolerance + 1e-9 >= best_profit
    ]
    champions = [
        max(rows, key=lambda candidate: (
            _candidate_score(candidate),
            -_candidate_liquidations(candidate),
            *_candidate_execution_priority(candidate),
        )),
        min(near_best, key=lambda candidate: (
            _candidate_liquidations(candidate),
            -_candidate_score(candidate),
        )),
        max(near_best, key=lambda candidate: (
            *_candidate_execution_priority(candidate),
            _candidate_score(candidate),
            -_candidate_liquidations(candidate),
        )),
        baseline,
    ]
    champions.extend(_diverse_sizing_candidates(near_best, baseline, limit))
    selected = []
    seen = set()
    for candidate in champions:
        params_ = candidate.get("params") or {}
        key = tuple(round(float(params_.get(name, 0.0)), 12) for name in TUNE_KEYS)
        if key in seen:
            continue
        seen.add(key)
        selected.append(candidate)
        if len(selected) >= max(1, int(limit)):
            break
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


def _load_followed_wallets(db) -> list[str]:
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
    raw_fill_bytes = _portfolio_fill_json_bytes(db, addrs, start_ms)
    if max_bytes > 0 and raw_fill_bytes > max_bytes:
        return None
    resource_guard.require_replay_budget(raw_fill_bytes)
    fills = prepare_replay_fills(
        _load_portfolio_fills(db, addrs, start_ms, include_watch=include_watch)
    )
    # Keep one immutable longest prepared sequence.  Every 30/14/7 result is a view sliced from the one
    # continuous capital path in ``_candidate_windows``; materializing three fill lists only tripled RSS.
    return {max_days: fills}


def _filter_window_fills_by_addr(window_fills: dict[int, list[dict]], addrs: Iterable[str]) -> dict[int, list[dict]]:
    allowed = {(a or "").lower() for a in addrs if a}
    return {
        int(days): slice_prepared_replay_fills(
            prepare_replay_fills(fills), allowed_addrs=allowed,
        )
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
    params_ = {
        key: float(params_.get(key, getattr(config, key)))
        for key in TUNE_KEYS
    }
    return {
        "mult": None,
        "axis": axis,
        "margins": {key: params_[key] for key in MARGIN_KEYS},
        "lev_caps": {key: params_[key] for key in LEV_KEYS},
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


def _pair_margins_for_leverage(base: dict, leverage_values: dict, follow: dict | None) -> dict:
    """Keep tier notional approximately constant while moving leverage.

    ``margin × leverage`` owns exposure.  A lower leverage candidate is only a genuine safety alternative
    when its margin rises reciprocally; otherwise it merely shrinks the trade and wins by under-deploying.
    """
    proposal = {**base, **leverage_values}
    if not follow:
        return proposal
    ceilings = margin_add_capacity_ceilings(follow)
    for margin_key, leverage_key in zip(MARGIN_KEYS, LEV_KEYS):
        old_leverage = max(1e-9, float(base[leverage_key]))
        new_leverage = max(1e-9, float(proposal[leverage_key]))
        floor_key = margin_key.replace("_MARGIN_PCT", "_MARGIN_MIN_PCT")
        floor = min(float(follow.get(floor_key) or 0.0), ceilings[margin_key])
        notional_fraction = float(base[margin_key]) * old_leverage
        proposal[margin_key] = min(
            ceilings[margin_key],
            max(floor, notional_fraction / new_leverage),
        )
    return proposal


def independent_leverage_candidates(base: dict, follow: dict | None = None) -> list[dict]:
    """Coordinate-polish leverage while preserving each tier's approximate notional."""
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
                _pair_margins_for_leverage(base, {key: value}, follow),
                axis=f"notional_paired_leverage_{key.lower()}",
            ))
    return candidates


def coarse_leverage_candidates(base: dict, follow: dict | None = None) -> list[dict]:
    """Baseline plus only each tier's low/high endpoint for prefix-count exploration."""
    candidates = independent_leverage_candidates(base, follow)
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
    for key in CONGESTION_PCT_KEYS:
        out[key] = float(params_[key])
    for key in CONGESTION_INT_KEYS:
        out[key] = int(round(float(params_[key])))
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
                       path_meta: dict | None = None,
                       initial_balance: float | None = None,
                       compact: bool = False) -> dict:
    """Return 30/14/7 views sliced from one continuously compounding account.

    Replaying every window independently reset the recent views to ``INITIAL_BALANCE``.  A strategy that had
    already grown to $30k could therefore be rejected as congested by a synthetic $10k 14-day account.  Run
    the longest warm surface once; ``slice_backtest_result`` now slices both economics and timestamped
    open/capacity outcomes without changing historical position sizes.
    """
    market_ctx = _load_market_ctx(db) if market_ctx is None else market_ctx
    days_values = _tune_days()
    max_days = max(days_values)
    fills = prepare_replay_fills((window_fills or {}).get(max_days) or [])
    if window_fills is None:
        warmup_days = int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
        fills = prepare_replay_fills(_load_portfolio_fills(
            db, addrs, now_ms - (max_days + warmup_days) * 86_400_000,
        ))
    replay_path = path_rows
    if path_rows and fills:
        first_fill = min(int(row.get("time") or 0) for row in fills)
        last_fill = max(int(row.get("time") or 0) for row in fills)
        replay_path = subset_price_path(
            path_rows, fills, start_ms=first_fill, end_ms=last_fill,
        )
    warm_result = run_backtest(
        "portfolio", fills, sigmas=sigmas, overrides=overrides, market_ctx=market_ctx or {},
        price_path=replay_path, price_path_meta=path_meta,
        initial_balance=initial_balance,
    )
    windows = {}
    for days in days_values:
        result = slice_backtest_result(
            warm_result,
            now_ms - int(days) * 86_400_000,
            window_days=int(days),
        )
        result["fills"] = sum(
            int(row.get("time") or 0) >= now_ms - int(days) * 86_400_000
            for row in fills
        )
        result["continuous_replay_days"] = int(max_days)
        windows[int(days)] = _compact_backtest(result) if compact else result
    return windows


def evaluate_portfolio_window(db, sigmas: dict, overrides: dict, now_ms: int,
                              *, window_fills: dict[int, list[dict]], days: int = 30,
                              market_ctx: dict | None = None, path_rows: list[dict] | None = None,
                              path_meta: dict | None = None) -> dict:
    """Replay one portfolio/window and immediately discard heavy position/equity details."""
    days = int(days)
    fills = prepare_replay_fills((window_fills or {}).get(days) or [])
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


def store_certified_portfolio_replay(db, generation_id: str, strict: dict | None) -> dict:
    """Persist the already-computed final strict 30-day Core replay without replaying it again."""
    strict = dict(strict or {})
    core_count = int(strict.get("selectedCount") or 0)
    if not core_count:
        summary = {
            "generation": generation_id, "coreCount": 0, "replayedAt": now_iso(),
            "status": "empty", "validationSource": "final_strict_copy",
        }
        _state_set(db, "effective_portfolio_replay", summary)
        db.commit()
        return summary
    validation_status = str(strict.get("status") or "")
    if validation_status not in {"passed", "operator_review_degraded", "probation"}:
        return {
            "generation": generation_id, "coreCount": core_count,
            "status": "unavailable", "reason": "final_strict_copy_missing",
        }
    follow = params.load_follow(db)
    if "SMART_ADD" in follow:
        follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
    params_hash = hashlib.sha256(
        json.dumps(follow, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    effective_params = {
        "leverageCaps": {key: f(follow.get(key)) for key in LEV_KEYS},
        "marginPct": {key: f(follow.get(key)) for key in MARGIN_KEYS},
        "marginEquityPct": f(follow.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)),
        "initialMarginEquity": f(config.INITIAL_BALANCE) * f(
            follow.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
        ),
        "add": {key: f(follow.get(key)) for key in ADD_TUNE_KEYS},
        "smartAddCapacity": {
            "reservedAdds": int(getattr(config, "SMART_ADD_MIN_CAPACITY", 2) or 2),
            "marginCeilings": margin_add_capacity_ceilings(follow),
        },
    }
    summary = {
        "generation": generation_id,
        "status": "ok",
        "validationStatus": validation_status,
        "validationFailures": list(strict.get("failures") or ()),
        "coreCount": core_count,
        "paramsHash": params_hash,
        "replayedAt": now_iso(),
        "netPnl30": f(strict.get("netPnl30d")),
        # The certification replay already uses the conservative liquidate-on-ambiguity mode.
        "netPnl30Worst": f(strict.get("netPnl30d")),
        "netPnl30AmbiguousLiquidate": f(strict.get("netPnl30d")),
        "startEquity30": f(strict.get("startEquity30d")),
        "endEquity30": f(strict.get("endEquity30d")),
        "dynamicReturn30d": strict.get("dynamicReturn30d"),
        "netPnl7": f(strict.get("netPnl7d")),
        "startEquity7": f(strict.get("startEquity7d")),
        "endEquity7": f(strict.get("endEquity7d")),
        "dynamicReturn7d": strict.get("dynamicReturn7d"),
        "standardizedAccount": dict(strict.get("standardizedAccount") or {}),
        "paperAccount": dict(strict.get("paperAccount") or {}),
        "maxDrawdown30": f(strict.get("maxDrawdown30d")),
        "openRate30": f(strict.get("actionableOpenRate30d")),
        "capacityFit30": f(strict.get("capacityFit30d")),
        "liquidations30": int(strict.get("liquidations30d") or 0),
        "liquidations30Worst": int(strict.get("liquidations30d") or 0),
        "liquidations30AmbiguousLiquidate": int(strict.get("liquidations30d") or 0),
        "pricePathCoverage30": f(strict.get("pricePathCoverage30d")),
        "pricePathStatus": (
            "covered" if f(strict.get("pricePathCoverage30d"))
            >= float(getattr(config, "CORE_PRICE_PATH_MIN_COVERAGE", .95)) else "unverified"
        ),
        "estimateKind": "trade_ohlc_conservative_proxy",
        "validationSource": strict.get("validationSource") or "final_strict_copy",
        "effectiveParams": effective_params,
        "maintenanceMarginCoverage30": f(strict.get("maintenanceMarginCoverage30d")),
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
    # Grid search can evaluate hundreds of candidates.  Retaining every position, open-position snapshot,
    # and equity-curve point for every candidate exhausts a 512MB VPS even though ranking only consumes the
    # compact summary below.
    if primary_only:
        result = evaluate_portfolio_window(
            db, sigmas, overrides, now_ms, window_fills={30: list((window_fills or {}).get(30) or [])},
            days=30, market_ctx=market_ctx, path_rows=path_rows, path_meta=path_meta,
        )
        out["windows"] = {30: _compact_backtest(result)}
    else:
        out["windows"] = {
            days: _compact_backtest(result)
            for days, result in _candidate_windows(
                db, addrs, sigmas, overrides, now_ms, window_fills=window_fills,
                market_ctx=market_ctx, path_rows=path_rows, path_meta=path_meta,
                compact=True,
            ).items()
        }
    return out


def evaluate_add_candidate(db, addrs: list[str], follow: dict, candidate: dict,
                           sigmas: dict | None = None, now_ms: int | None = None,
                           window_fills: dict[int, list[dict]] | None = None,
                           path_rows: list[dict] | None = None, path_meta: dict | None = None,
                           market_ctx: dict | None = None,
                           primary_only: bool = False) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    overrides = {**follow_overrides_for_add_candidate(follow, candidate),
                 "AMBIGUOUS_PATH_MODE": "liquidate"}
    params_ = {k: overrides[k] for k in ADD_TUNE_KEYS}
    sigmas = sigmas if sigmas is not None else _load_sigmas(db)
    out = dict(candidate)
    out["params"] = params_
    out["add_params"] = params_
    if primary_only:
        result = evaluate_portfolio_window(
            db, sigmas, overrides, now_ms,
            window_fills={30: list((window_fills or {}).get(30) or [])},
            days=30, market_ctx=market_ctx, path_rows=path_rows, path_meta=path_meta,
        )
        out["windows"] = {30: _compact_backtest(result)}
    else:
        out["windows"] = {
            days: _compact_backtest(result)
            for days, result in _candidate_windows(
                db, addrs, sigmas, overrides, now_ms, window_fills=window_fills,
                market_ctx=market_ctx, path_rows=path_rows, path_meta=path_meta,
                compact=True,
            ).items()
        }
    return out


def _evaluate_candidates_parallel(
    candidates: Iterable[dict],
    *,
    kind: str,
    addrs: list[str],
    follow: dict,
    sigmas: dict,
    now_ms: int,
    window_fills: dict[int, list[dict]],
    path_rows,
    path_meta: dict,
    market_ctx: dict,
    primary_only: bool = False,
    result_cache: dict | None = None,
    worker_pool: replay_parallel.ReusableOrderedPool | None = None,
) -> list[dict]:
    """Evaluate one independent candidate batch using CPU-count-aware pure workers."""
    rows = list(candidates)
    evaluator = evaluate_add_candidate if kind == "add" else evaluate_tune_candidate
    if result_cache is not None:
        def marker(candidate):
            proposal = (
                follow_overrides_for_add_candidate(follow, candidate)
                if kind == "add" else
                follow_overrides_for_tune_candidate(follow, candidate)
            )
            return (
                str(kind), bool(primary_only),
                tuple(
                    round(float(proposal.get(key, 0.0)), 12)
                    for key in (*TUNE_KEYS, *ADD_TUNE_KEYS)
                ),
            )

        def hydrate(cached, candidate):
            out = dict(cached)
            # Preserve the current search-axis provenance while reusing the immutable economics.
            for key in (
                "axis", "mult", "gap_k", "pos_gap_k", "shrink_g", "max_hard",
                "distance_keys",
            ):
                if key in candidate:
                    out[key] = candidate[key]
            return out

        markers = [marker(candidate) for candidate in rows]
        missing = []
        missing_markers = []
        seen_missing = set()
        for candidate, key in zip(rows, markers):
            if key in result_cache or key in seen_missing:
                continue
            seen_missing.add(key)
            missing.append(candidate)
            missing_markers.append(key)
        if missing:
            evaluated = _evaluate_candidates_parallel(
                missing,
                kind=kind, addrs=addrs, follow=follow, sigmas=sigmas,
                now_ms=now_ms, window_fills=window_fills,
                path_rows=path_rows, path_meta=path_meta, market_ctx=market_ctx,
                primary_only=primary_only, result_cache=None, worker_pool=worker_pool,
            )
            for key, value in zip(missing_markers, evaluated):
                result_cache[key] = value
        return [
            hydrate(result_cache[key], candidate)
            for key, candidate in zip(markers, rows)
        ]
    if getattr(evaluator, "__module__", None) != __name__:
        # Unit tests and operator diagnostics may temporarily inject an evaluator.  Such callables are not
        # importable in spawned workers; preserve the injected contract in-process.
        common = {
            "sigmas": sigmas, "now_ms": now_ms, "window_fills": window_fills,
            "path_rows": path_rows, "path_meta": path_meta, "market_ctx": market_ctx,
        }
        if kind == "add":
            return [
                evaluator(
                    None, addrs, follow, candidate,
                    primary_only=primary_only, **common,
                )
                for candidate in rows
            ]
        return [
            evaluator(
                None, addrs, follow, candidate,
                primary_only=primary_only, **common,
            )
            for candidate in rows
        ]
    context = {
        "addrs": list(addrs),
        "sigmas": sigmas,
        "now_ms": int(now_ms),
        "window_fills": window_fills,
        "path_rows": path_rows,
        "path_meta": path_meta,
        "market_ctx": market_ctx,
    }
    tasks = [
        (kind, dict(follow), candidate, bool(primary_only))
        for candidate in rows
    ]
    if worker_pool is not None:
        return worker_pool.map_ordered(_evaluate_candidate_process, tasks)
    return replay_parallel.map_ordered(
        _evaluate_candidate_process, tasks,
        initializer=_init_process_replay_context, initargs=(context,),
    )


def _write_tune_params(db, vals: dict) -> None:
    stamp = now_iso()
    for key in TUNE_KEYS:
        val = int(round(float(vals[key]))) if key in CONGESTION_INT_KEYS else float(vals[key])
        stored = (
            val * 100.0
            if key in (*MARGIN_KEYS, *DEPLOY_KEYS, *CONGESTION_PCT_KEYS)
            else val
        )
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
        "closed_n", "open_n", "wins", "liquidations",
        "max_liquidation_loss_pct", "max_liquidation_loss",
        "max_liquidation_loss_coin", "max_liquidation_loss_closed_at",
        "copy_win_rate",
        "copy_net_pnl", "copy_gross_pnl", "closed_net_pnl", "unrealized_pnl", "fee_drag",
        "margin_equity_pct", "initial_margin_equity", "window_start_equity",
        "window_end_equity",
        "target_open_events", "raw_target_open_events", "small_open_excluded_n",
        "effective_target_open_events", "opened_n", "raw_open_capture_rate",
        "effective_open_follow_rate", "open_execution_audit",
        "open_fill_rate", "target_adds",
        "followed_adds", "missed_adds", "missed_add_rate", "add_dependency",
        "add_metrics_version", "add_outcome_counts", "raw_add_order_follow_rate",
        "noise_merged_adds", "blocked_adds", "actionable_add_orders",
        "actionable_add_capture_rate", "true_blocked_add_rate", "add_episode_count",
        "entry_gap_sigma_weighted", "entry_gap_sigma_p90", "entry_gap_pct_weighted",
        "entry_gap_pct_p90", "entry_alignment", "add_execution", "add_fidelity",
        "add_fidelity_applied", "behavior_replication_v2", "behavior_replication_rate",
        "profit_factor", "payoff_ratio", "net_after_top1", "net_after_top2",
        "top1_profit_share", "top3_profit_share",
        "body_after_top3_n", "body_after_top3_wins", "body_after_top3_losses",
        "body_after_top3_win_rate", "body_after_top3_net_pnl",
        "body_after_top3_profit_factor", "body_after_top3_payoff_ratio",
        "body_after_top3_median_pnl",
        "target_peak_concurrent", "copy_peak_concurrent", "max_concurrent_fit",
        "capacity_open_fit", "execution_capacity_fit", "cash_congestion_fit",
        "open_constraint_counts", "open_constraint_fit",
        "price_path_coverage", "model_coverage", "max_drawdown",
        "maintenance_margin_coverage", "maintenance_margin_known", "maintenance_margin_missing",
        "worst_day", "cvar95", "peak_deploy_pct", "avg_deploy_pct", "deployment_distribution",
        "actionable_open_rate",
        "execution_fill_rate", "fee_slippage_drag", "pnl_concentration", "fallback_reasons", "fills",
        "ambiguous_liquidations", "price_path_boundary_skips", "continuous_replay_days",
        "tier_economics",
    )
    out = {k: result.get(k) for k in keys if k in result}
    out["skip_reasons"] = result.get("skip_reasons") or {}
    episode_index = {}
    for position in [
        *(result.get("positions") or ()), *(result.get("open_positions") or ()),
    ]:
        marker = hashlib.sha256("|".join((
            str(position.get("addr") or "").lower(),
            str(position.get("coin") or ""), str(position.get("side") or ""),
            str(int(position.get("opened_at") or 0)),
        )).encode("utf-8")).hexdigest()[:24]
        episode_index[marker] = float(position.get("net_pnl") or 0.0) + float(
            position.get("unrealized_pnl") or 0.0
        )
    out["episode_pnl_index"] = episode_index
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


def _prepare_walk_forward_context(window_fills, now_ms, path_rows, *,
                                  fold_days=10, fold_count=3) -> dict:
    max_days = max(window_fills) if window_fills else 30
    fills = prepare_replay_fills((window_fills or {}).get(max_days) or [])
    warmup_ms = int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0) * 86400_000
    fold_days = max(1, int(fold_days))
    fold_count = max(1, int(fold_count))
    total_days = fold_days * fold_count
    start_ms = int(now_ms) - total_days * 86_400_000
    continuous_fills = slice_prepared_replay_fills(
        fills, start_ms=start_ms - warmup_ms,
    )
    continuous_path = subset_price_path(
        path_rows, continuous_fills,
        start_ms=start_ms - warmup_ms, end_ms=int(now_ms),
    )
    fold_fill_counts = []
    for index in range(fold_count):
        lo = start_ms + index * fold_days * 86_400_000
        hi = lo + fold_days * 86_400_000
        fold_fill_counts.append(sum(
            lo <= int(row.get("time") or 0) < hi for row in continuous_fills
        ))
    return {
        "fills": continuous_fills,
        "path": continuous_path,
        "startMs": start_ms,
        "totalDays": total_days,
        "foldDays": fold_days,
        "foldCount": fold_count,
        "foldFillCounts": fold_fill_counts,
    }


def _walk_forward_surface(overrides, *, sigmas, market_ctx, path_meta, prepared) -> dict:
    warm = run_backtest(
        "portfolio", prepared["fills"], sigmas=sigmas, overrides=overrides,
        market_ctx=market_ctx or {}, price_path=prepared["path"], price_path_meta=path_meta,
    )
    result = slice_backtest_result(
        warm, prepared["startMs"], window_days=prepared["totalDays"],
    )
    positions = list(result.get("positions") or ())
    path_samples = sorted(
        (
            (int(row.get("time") or 0), float(row.get("equity") or 0.0))
            for row in (result.get("path_equity_samples") or ())
            if int(row.get("time") or 0) > 0 and float(row.get("equity") or 0.0) > 0
        ),
        key=lambda row: row[0],
    )
    initial_equity = float(
        result.get("window_start_equity")
        or result.get("initial_margin_equity")
        or config.INITIAL_BALANCE
    )

    def equity_at(stamp, *, fallback):
        value = fallback
        for sample_stamp, equity in path_samples:
            if sample_stamp > stamp:
                break
            value = equity
        return value

    rows = []
    carried_equity = initial_equity
    fold_days = int(prepared["foldDays"])
    for index in range(int(prepared["foldCount"])):
        lo = int(prepared["startMs"]) + index * fold_days * 86_400_000
        hi = lo + fold_days * 86_400_000
        start_equity = max(1.0, equity_at(lo, fallback=carried_equity))
        end_equity = max(1.0, equity_at(hi, fallback=start_equity))
        fold_positions = sorted(
            (
                position for position in positions
                if lo <= int(position.get("closed_at") or 0) < hi
            ),
            key=lambda position: int(position.get("closed_at") or 0),
        )
        points = [start_equity]
        points.extend(equity for stamp, equity in path_samples if lo < stamp <= hi)
        if len(points) == 1:
            running = start_equity
            for position in fold_positions:
                running += float(position.get("net_pnl") or 0.0)
                points.append(running)
            end_equity = running
        peak = points[0]
        max_drawdown = 0.0
        for equity in points:
            peak = max(peak, equity)
            max_drawdown = max(
                max_drawdown, (peak - equity) / peak if peak > 0 else 0.0,
            )
        carried_equity = end_equity
        rows.append({
            "startMs": lo,
            "endMs": hi,
            "startEquity": start_equity,
            "endEquity": end_equity,
            "netPnl": end_equity - start_equity,
            "maxDrawdown": max_drawdown,
            "liquidations": sum(
                1 for position in fold_positions
                if position.get("status") == "liquidated"
            ),
        })
    return {
        "folds": rows,
        "openRate": float(
            result.get("actionable_open_rate", result.get("open_fill_rate")) or 0.0
        ),
        "capacityFit": float(result.get("capacity_open_fit") or 0.0),
        "maintenanceMarginCoverage": float(
            result.get("maintenance_margin_coverage") or 0.0
        ),
        "pricePathCoverage": float(result.get("price_path_coverage") or 0.0),
    }


def _compose_walk_forward_validation(baseline, challenger, prepared) -> dict:
    baseline_folds = list(baseline.get("folds") or ())
    challenger_folds = list(challenger.get("folds") or ())
    compact_folds = []
    wins = 0
    for index, (base_fold, challenger_fold) in enumerate(zip(
        baseline_folds, challenger_folds,
    )):
        base_net = float(base_fold.get("netPnl") or 0.0)
        challenger_net = float(challenger_fold.get("netPnl") or 0.0)
        win = challenger_net > base_net
        wins += int(win)
        compact_folds.append({
            "fold": index + 1,
            "fills": int((prepared.get("foldFillCounts") or [])[index]),
            "baselineNet": base_net,
            "challengerNet": challenger_net,
            "baselineStartEquity": float(base_fold.get("startEquity") or 0.0),
            "challengerStartEquity": float(challenger_fold.get("startEquity") or 0.0),
            "baselineMaxDD": float(base_fold.get("maxDrawdown") or 0.0),
            "challengerMaxDD": float(challenger_fold.get("maxDrawdown") or 0.0),
            "baselineOpenRate": float(baseline.get("openRate") or 0.0),
            "challengerOpenRate": float(challenger.get("openRate") or 0.0),
            "baselineCapacityFit": float(baseline.get("capacityFit") or 0.0),
            "challengerCapacityFit": float(challenger.get("capacityFit") or 0.0),
            "baselineLiquidations": int(base_fold.get("liquidations") or 0),
            "challengerLiquidations": int(challenger_fold.get("liquidations") or 0),
            "win": win,
        })
    return {
        "folds": compact_folds,
        "foldWins": wins,
        "holdout": compact_folds[-1] if compact_folds else {},
        "maintenanceMarginCoverage": float(
            challenger.get("maintenanceMarginCoverage") or 0.0
        ),
        "pricePathCoverage": float(challenger.get("pricePathCoverage") or 0.0),
        "foldDays": int(prepared["foldDays"]),
        "foldCount": int(prepared["foldCount"]),
    }


def _walk_forward_validation(addrs, follow, proposal, sigmas, window_fills, now_ms,
                             path_rows=None, path_meta=None, market_ctx=None, *,
                             fold_days=10, fold_count=3,
                             prepared_context=None, baseline_surface=None) -> dict:
    """Reject overfit proposals on disjoint folds of one continuous capital path.

    Each parameter surface is replayed exactly once.  Callers evaluating several finalists may reuse the
    prepared fills/path and one active-baseline surface; shorter folds are views of those continuous paths,
    never independent reset-to-$10k accounts.
    """
    prepared = prepared_context or _prepare_walk_forward_context(
        window_fills, now_ms, path_rows,
        fold_days=fold_days, fold_count=fold_count,
    )
    market_ctx = market_ctx or {}
    baseline = baseline_surface or _walk_forward_surface(
        {**follow, "AMBIGUOUS_PATH_MODE": "liquidate"},
        sigmas=sigmas, market_ctx=market_ctx, path_meta=path_meta, prepared=prepared,
    )
    challenger = _walk_forward_surface(
        {**follow, **proposal, "AMBIGUOUS_PATH_MODE": "liquidate"},
        sigmas=sigmas, market_ctx=market_ctx, path_meta=path_meta, prepared=prepared,
    )
    return _compose_walk_forward_validation(baseline, challenger, prepared)


def _walk_forward_validation_batch(addrs, follow, proposals, sigmas, window_fills, now_ms,
                                   path_rows=None, path_meta=None, market_ctx=None, *,
                                   fold_days=10, fold_count=3) -> list[dict]:
    """Replay the active baseline once and all unique finalist surfaces in one CPU-bounded batch."""
    proposals = [dict(proposal or {}) for proposal in proposals]
    if not proposals:
        return []
    if getattr(_walk_forward_validation, "__module__", None) != __name__:
        # Preserve monkey-patched diagnostics/tests without spawning an unimportable callable.
        return [
            _walk_forward_validation(
                addrs, follow, proposal, sigmas, window_fills, now_ms,
                path_rows=path_rows, path_meta=path_meta, market_ctx=market_ctx,
                fold_days=fold_days, fold_count=fold_count,
            )
            for proposal in proposals
        ]
    prepared = _prepare_walk_forward_context(
        window_fills, now_ms, path_rows,
        fold_days=fold_days, fold_count=fold_count,
    )
    def surface_marker(overrides):
        return tuple(
            round(float(overrides.get(key, follow.get(key, 0.0))), 12)
            for key in (*TUNE_KEYS, *ADD_TUNE_KEYS)
        )

    baseline_overrides = {**follow, "AMBIGUOUS_PATH_MODE": "liquidate"}
    all_overrides = [baseline_overrides]
    baseline_marker = surface_marker(baseline_overrides)
    unique_index = {baseline_marker: 0}
    proposal_indices = []
    for proposal in proposals:
        overrides = {**follow, **proposal, "AMBIGUOUS_PATH_MODE": "liquidate"}
        marker = surface_marker(overrides)
        if marker not in unique_index:
            unique_index[marker] = len(all_overrides)
            all_overrides.append(overrides)
        proposal_indices.append(unique_index[marker])
    context = {
        "sigmas": sigmas,
        "market_ctx": market_ctx or {},
        "path_meta": path_meta,
        "walk_forward": prepared,
    }
    surfaces = replay_parallel.map_ordered(
        _evaluate_walk_forward_surface_process,
        all_overrides,
        initializer=_init_process_replay_context,
        initargs=(context,),
    )
    baseline = surfaces[0]
    return [
        _compose_walk_forward_validation(baseline, surfaces[index], prepared)
        for index in proposal_indices
    ]


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
        position_table = selected_book(db).position
        row = db.execute(
            f"SELECT COUNT(*) FROM {position_table} WHERE status!='open' "
            f"AND lower(addr) IN ({marks})",
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
    price_path_floor = float(
        follow.get("AUTO_TUNE_PRICE_PATH_MIN_COVERAGE")
        if follow.get("AUTO_TUNE_PRICE_PATH_MIN_COVERAGE") is not None
        else getattr(config, "AUTO_TUNE_PRICE_PATH_MIN_COVERAGE", 0.95)
    )
    price_path_floor = max(
        price_path_floor, float(getattr(config, "AUTO_TUNE_PRICE_PATH_MIN_COVERAGE", .95)),
    )
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
        "safetyRepair": bool(model.get("safetyRepair")),
        "baselineLiquidations": int(model.get("baselineLiquidations") or 0),
        "challengerLiquidations": int(model.get("challengerLiquidations") or 0),
        "profitRetention": model.get("profitRetention"),
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
    if relative_gain < policy.tune_min_relative_gain:
        reasons.append("relative_gain_below_floor")
    max_fit_drop = float(getattr(config, "AUTO_TUNE_MARGIN_MAX_OPEN_FIT_DROP", 0.03))
    execution_reasons = []
    for fold in folds:
        base_open = float(fold.get("baselineOpenRate") or 0.0)
        candidate_open = float(fold.get("challengerOpenRate") or 0.0)
        required_open = max(
            0.70 if base_open >= 0.70 else 0.0,
            base_open - max_fit_drop,
        )
        if candidate_open < required_open:
            execution_reasons.append("open_rate_below_floor")
            break
    for fold in folds:
        base_capacity = float(fold.get("baselineCapacityFit") or 0.0)
        candidate_capacity = float(fold.get("challengerCapacityFit") or 0.0)
        required_capacity = max(
            policy.min_capacity_fit if base_capacity >= policy.min_capacity_fit else 0.0,
            base_capacity - max_fit_drop,
        )
        if candidate_capacity < required_capacity:
            execution_reasons.append("capacity_fit_below_floor")
            break
    reasons.extend(execution_reasons)

    baseline_liquidations = sum(
        int(fold.get("baselineLiquidations") or 0) for fold in folds
    )
    challenger_liquidations = sum(
        int(fold.get("challengerLiquidations") or 0) for fold in folds
    )
    profit_retention = float(
        getattr(config, "AUTO_TUNE_SAFETY_PROFIT_RETENTION", 0.90)
    )
    safety_repair = bool(
        baseline_total > 0.0
        and challenger_total >= baseline_total * profit_retention
        and challenger_liquidations < baseline_liquidations
        and sum(float(fold.get("challengerNet") or 0.0) > 0.0 for fold in folds) >= 2
        and float(holdout.get("challengerNet") or 0.0) > 0.0
        and not execution_reasons
    )
    if safety_repair:
        # A safer surface should not need to pretend it made 5% more money than the already aggressive
        # baseline. It still must retain most profit and pass every current execution invariant.
        reasons = [
            reason for reason in reasons
            if reason not in {"fewer_than_two_fold_wins", "relative_gain_below_floor"}
        ]
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "relativeGain": relative_gain,
        "safetyRepair": safety_repair,
        "baselineLiquidations": baseline_liquidations,
        "challengerLiquidations": challenger_liquidations,
        "profitRetention": (
            challenger_total / baseline_total if baseline_total > 0.0 else None
        ),
    }


def _formation_model_validation(validation: dict) -> dict:
    """Validate one count-specific tuning proposal before the final strict replay.

    Formation tuning ranks aggregate continuously compounded profit. Wallet admission and final publication
    own quality and exact 30d/7d candle-path safety, so tuning folds are diagnostics rather than another
    wallet-admission gate.
    """
    folds = list(validation.get("folds") or ())

    def feasible(prefix):
        if not folds:
            return False
        nets = [float(row.get(f"{prefix}Net") or 0.0) for row in folds]
        return sum(nets) > 0.0

    baseline_feasible = feasible("baseline")
    challenger_feasible = feasible("challenger")
    baseline_total = sum(float(row.get("baselineNet") or 0.0) for row in folds)
    challenger_total = sum(float(row.get("challengerNet") or 0.0) for row in folds)
    relative_gain = (
        (challenger_total - baseline_total) / max(1.0, abs(baseline_total))
    )
    normal = {
        "eligible": challenger_feasible,
        "reasons": [] if challenger_feasible else ["formation_profit_not_positive"],
        "relativeGain": relative_gain,
        "safetyRepair": False,
        "baselineLiquidations": sum(
            int(row.get("baselineLiquidations") or 0) for row in folds
        ),
        "challengerLiquidations": sum(
            int(row.get("challengerLiquidations") or 0) for row in folds
        ),
        "profitRetention": (
            challenger_total / baseline_total if baseline_total > 0.0 else None
        ),
    }
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


def _local_surface_marker(surface: dict) -> tuple:
    return tuple(
        round(float(surface.get(key, 0.0)), 12)
        for key in (*TUNE_KEYS, *ADD_TUNE_KEYS)
    )


def _local_complete_surface(follow: dict, surface: dict | None = None) -> dict:
    values = dict(surface or {})
    return {
        key: float(values.get(key, follow.get(key, getattr(config, key))))
        for key in (*TUNE_KEYS, *ADD_TUNE_KEYS)
    }


def _margin_grid_floor(value: float) -> float:
    return round(math.floor((float(value) + 1e-12) / MARGIN_GRID_STEP) * MARGIN_GRID_STEP, 10)


def _margin_grid_ceil(value: float) -> float:
    return round(math.ceil((float(value) - 1e-12) / MARGIN_GRID_STEP) * MARGIN_GRID_STEP, 10)


def _margin_grid_bounds(follow: dict) -> tuple[dict[str, float], dict[str, float]]:
    ceilings = margin_add_capacity_ceilings(follow)
    floors = {}
    grid_ceilings = {}
    for key in MARGIN_KEYS:
        floor_key = key.replace("_MARGIN_PCT", "_MARGIN_MIN_PCT")
        floors[key] = _margin_grid_ceil(float(follow.get(floor_key) or 0.0))
        grid_ceilings[key] = max(floors[key], _margin_grid_floor(ceilings[key]))
    return floors, grid_ceilings


def _margin_grid_surface(follow: dict, base: dict, values: dict[str, float]) -> dict:
    floors, ceilings = _margin_grid_bounds(follow)
    out = dict(base)
    for key in MARGIN_KEYS:
        value = _margin_grid_floor(values.get(key, base[key]))
        out[key] = min(ceilings[key], max(floors[key], value))
    return out


def local_shared_margin_surfaces(
    follow: dict, base_surface: dict, *, tier_economics: dict | None = None,
) -> list[dict]:
    """Exact control plus a bounded 0.5 percentage-point grid shared by all three tiers."""
    base = _local_complete_surface(follow, base_surface)
    surfaces = [dict(base)]
    anchor = _margin_grid_surface(follow, base, {
        key: _margin_grid_floor(base[key]) for key in MARGIN_KEYS
    })
    surfaces.append(anchor)
    for key in MARGIN_KEYS:
        for delta in (-MARGIN_GRID_STEP, MARGIN_GRID_STEP):
            proposal = dict(anchor)
            proposal[key] = anchor[key] + delta
            surfaces.append(_margin_grid_surface(follow, base, proposal))
    for delta in (-MARGIN_GRID_STEP, MARGIN_GRID_STEP):
        surfaces.append(_margin_grid_surface(follow, base, {
            key: anchor[key] + delta for key in MARGIN_KEYS
        }))
    unique = {}
    for surface in surfaces:
        unique.setdefault(_local_surface_marker(surface), surface)
    return list(unique.values())[:10]


def deployment_utilization_summary(result: dict | None) -> dict:
    """Return compact, correctly-labelled deployment telemetry from one replay.

    ``avg_deploy_pct`` is sampled at portfolio events and remains useful as a
    compatibility metric.  Tier economics are time weighted; because every
    deployment sample records all three tiers at the same timestamp, summing
    their averages yields the account's time-weighted margin deployment.
    """
    result = dict(result or {})
    economics = dict(result.get("tier_economics") or {})
    tier_average = {
        tier: float((economics.get(tier) or {}).get("avgDeployPct") or 0.0)
        for tier in ("stable", "mid", "high")
    }
    distribution = dict(result.get("deployment_distribution") or {})
    constraints = dict(result.get("open_constraint_counts") or {})
    return {
        "timeWeightedAvgDeployPct": float(
            distribution.get("timeWeightedAvgDeployPct", sum(tier_average.values())) or 0.0
        ),
        "activeTimeWeightedAvgDeployPct": float(
            distribution.get("activeTimeWeightedAvgDeployPct") or 0.0
        ),
        "activeTimeShare": float(distribution.get("activeTimeShare") or 0.0),
        "percentiles": dict(distribution.get("percentiles") or {}),
        "timeAbove": dict(distribution.get("timeAbove") or {}),
        "eventSampleAvgDeployPct": float(result.get("avg_deploy_pct") or 0.0),
        "peakDeployPct": float(result.get("peak_deploy_pct") or 0.0),
        "tierTimeWeightedAvgDeployPct": tier_average,
        "constraintAttribution": {
            "noCash": int(constraints.get("cash") or 0),
            "newEntryBudget": int(constraints.get("aggregateDeploy") or 0),
            "coinCap": int(constraints.get("coinCap") or 0),
            "minimumSizing": int(constraints.get("minimumSizing") or 0),
            "walletConcentration": int(constraints.get("concentration") or 0),
        },
    }


def crowding_tradeoff(candidate: dict, baseline: dict) -> dict:
    """Explain a candidate's net change without running an unconstrained shadow replay."""
    candidate_index = dict(candidate.get("episodePnlIndex") or {})
    baseline_index = dict(baseline.get("episodePnlIndex") or {})
    common = set(candidate_index) & set(baseline_index)
    baseline_only = set(baseline_index) - set(candidate_index)
    candidate_only = set(candidate_index) - set(baseline_index)
    return {
        "commonEpisodeCount": len(common),
        "commonEpisodePnlDelta": sum(
            float(candidate_index[key]) - float(baseline_index[key]) for key in common
        ),
        "foregoneBaselineEpisodeCount": len(baseline_only),
        "foregoneBaselinePnl": sum(float(baseline_index[key]) for key in baseline_only),
        "candidateOnlyEpisodeCount": len(candidate_only),
        "candidateOnlyPnl": sum(float(candidate_index[key]) for key in candidate_only),
        "totalNetDelta": float(candidate.get("netPnl") or 0.0) - float(
            baseline.get("netPnl") or 0.0
        ),
        "capacityFitDelta": float(candidate.get("capacityFit") or 0.0) - float(
            baseline.get("capacityFit") or 0.0
        ),
        "openRateDelta": float(candidate.get("openRate") or 0.0) - float(
            baseline.get("openRate") or 0.0
        ),
    }


def _profitable_crowding_challenger(rows: Iterable[dict], standard: dict, base: dict) -> dict | None:
    """Admit one 70-75% capacity surface when its post-skip net profit is materially better."""
    best = None
    standard_net = float(standard.get("netPnl") or 0.0)
    standard_stress = float(standard.get("stressNetPnl", standard_net) or 0.0)
    tier_by_key = dict(zip(MARGIN_KEYS, ("stable", "mid", "high")))
    for row in rows:
        capacity = float(row.get("capacityFit") or 0.0)
        if not (0.70 <= capacity < float(config.SELECTION_MIN_CAPACITY_FIT)):
            continue
        if float(row.get("openRate") or 0.0) < float(config.SELECTION_MIN_ACTIONABLE_RATE):
            continue
        if float(row.get("netPnl") or 0.0) < standard_net * 1.05:
            continue
        if float(row.get("stressNetPnl", row.get("netPnl")) or 0.0) < standard_stress:
            continue
        economics = dict(row.get("tierEconomics") or {})
        expanded = [
            tier_by_key[key] for key in MARGIN_KEYS
            if float((row.get("surface") or {}).get(key) or 0.0) > float(base.get(key) or 0.0) + 1e-12
        ]
        if expanded and any(float((economics.get(tier) or {}).get("netPnl") or 0.0) <= 0.0 for tier in expanded):
            continue
        if best is None or float(row.get("netPnl") or 0.0) > float(best.get("netPnl") or 0.0):
            best = row
    return best


def _select_strict_winner(rows: list[dict]) -> dict:
    """Maximize liquidation-stressed net; inside 8% prefer fewer/smaller liquidations."""
    def strict_net(row):
        return float((row.get("validation") or {}).get("netPnl30d", row.get("netPnl")) or 0.0)

    def stress_net(row):
        validation = row.get("validation") or {}
        return float(validation.get(
            "liquidationStressNetPnl30d", row.get("stressNetPnl", strict_net(row)),
        ) or 0.0)

    best_net = max(stress_net(row) for row in rows)
    tolerance = max(1.0, abs(best_net) * 0.08)
    near = [row for row in rows if stress_net(row) >= best_net - tolerance]
    return max(near, key=lambda row: (
        -int((row.get("validation") or {}).get("liquidations", row.get("liquidations")) or 0),
        -float((row.get("validation") or {}).get("maxLiquidationLossPct", row.get("maxLiquidationLossPct")) or 0.0),
        float((row.get("validation") or {}).get("capacityFit", row.get("capacityFit")) or 0.0),
        float((row.get("validation") or {}).get("openRate", row.get("openRate")) or 0.0),
        stress_net(row),
        strict_net(row),
    ))


def final_membership_margin_surfaces(
    follow: dict, base_surface: dict,
) -> list[dict]:
    """Reuse the same fair three-tier grid for the exact final membership."""
    return local_shared_margin_surfaces(follow, base_surface)


def grid_upward_extensions(
    follow: dict, base_surface: dict, rows: Iterable[dict],
) -> list[dict]:
    """Continue each profitable/non-degrading +0.5 tier probe by one more grid step."""
    base = _local_complete_surface(follow, base_surface)
    anchor = _margin_grid_surface(follow, base, {
        key: _margin_grid_floor(base[key]) for key in MARGIN_KEYS
    })
    by_marker = {
        _local_surface_marker(row.get("surface") or {}): row for row in rows
    }
    anchor_row = by_marker.get(_local_surface_marker(anchor))
    if anchor_row is None:
        return []
    out = []
    for key in MARGIN_KEYS:
        first = dict(anchor)
        first[key] += MARGIN_GRID_STEP
        first = _margin_grid_surface(follow, base, first)
        first_row = by_marker.get(_local_surface_marker(first))
        if first_row is None:
            continue
        if float(first_row.get("netPnl") or 0.0) <= float(anchor_row.get("netPnl") or 0.0):
            continue
        if float(first_row.get("stressNetPnl", first_row.get("netPnl")) or 0.0) < float(
            anchor_row.get("stressNetPnl", anchor_row.get("netPnl")) or 0.0
        ):
            continue
        if float(first_row.get("openRate") or 0.0) < float(config.SELECTION_MIN_ACTIONABLE_RATE):
            continue
        if float(first_row.get("capacityFit") or 0.0) < 0.70:
            continue
        second = dict(first)
        second[key] += MARGIN_GRID_STEP
        second = _margin_grid_surface(follow, base, second)
        if _local_surface_marker(second) != _local_surface_marker(first):
            out.append(second)
    return out[:3]


def tune_final_membership_margin_surface(
    *,
    follow: dict,
    base_surface: dict,
    evaluate: Callable[[dict, str], dict],
    validate: Callable[[dict], dict] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict:
    """Calibrate first-open margins once for the exact final qualified set.

    This is deliberately not another general tuner: it does not search wallet
    counts, leverage, add parameters or arbitrary subsets.  At most ten base-grid
    surfaces, three one-step extensions and three strict finalists are evaluated, then the caller
    performs the one bounded individual recheck required by the new surface.
    """
    base = _local_complete_surface(follow, base_surface)
    cache = {}

    def run(surface: dict, stage: str) -> dict:
        surface = _local_complete_surface(follow, surface)
        marker = _local_surface_marker(surface)
        if marker not in cache:
            row = dict(evaluate(surface, stage) or {})
            row.update(surface=surface, stage=stage)
            cache[marker] = row
            if progress:
                progress(stage, len(cache), 1)
        return cache[marker]

    def rank(row: dict) -> tuple:
        deployment = dict(row.get("deploymentUtilization") or {})
        return (
            int(bool(row.get("feasible"))),
            float(row.get("stressNetPnl", row.get("netPnl")) or 0.0),
            float(row.get("netPnl") or 0.0),
            -int(row.get("liquidations") or 0),
            float(row.get("capacityFit") or 0.0),
            float(row.get("openRate") or 0.0),
            float(row.get("addCaptureRate") or 0.0),
            float(deployment.get("timeWeightedAvgDeployPct") or 0.0),
            -sum(
                abs(float(row["surface"][key]) - float(base[key]))
                for key in (*TUNE_KEYS, *ADD_TUNE_KEYS)
            ),
        )

    rows = [
        run(surface, "final_membership_margin_calibration")
        for surface in final_membership_margin_surfaces(follow, base)
    ]
    rows.extend(
        run(surface, "final_membership_margin_extension")
        for surface in grid_upward_extensions(follow, base, rows)
    )
    baseline_row = run(base, "final_membership_baseline_control")
    for row in rows:
        row["crowdingTradeoff"] = crowding_tradeoff(row, baseline_row)
    standards = sorted(
        (row for row in rows if row.get("feasible")), key=rank, reverse=True,
    )
    standard = standards[0] if standards else baseline_row
    crowded = _profitable_crowding_challenger(rows, standard, base)
    strict_candidates = []
    for row in [*standards[:2], crowded, baseline_row]:
        if row is None:
            continue
        marker = _local_surface_marker(row["surface"])
        if any(_local_surface_marker(item["surface"]) == marker for item in strict_candidates):
            continue
        strict_candidates.append(row)
    if crowded is not None:
        strict_candidates = [standard, crowded, baseline_row]
    deduped = []
    seen = set()
    for row in strict_candidates:
        marker = _local_surface_marker(row["surface"])
        if marker not in seen:
            seen.add(marker)
            deduped.append(row)
    strict_candidates = deduped[:3]
    strict_rows = []
    for row in strict_candidates:
        validation = dict(validate(row["surface"]) or {}) if validate else {
            "eligible": bool(row.get("feasible")),
        }
        strict_rows.append({**row, "validation": validation})
    finalists = [{
        "params": dict(row["surface"]),
        "netPnl": float(row.get("netPnl") or 0.0),
        "stressNetPnl": float(row.get("stressNetPnl", row.get("netPnl")) or 0.0),
        "feasible": bool(row.get("feasible")),
        "liquidations": int(row.get("liquidations") or 0),
        "openRate": float(row.get("openRate") or 0.0),
        "capacityFit": float(row.get("capacityFit") or 0.0),
        "tierEconomics": dict(row.get("tierEconomics") or {}),
        "deploymentUtilization": dict(row.get("deploymentUtilization") or {}),
        "crowdingTradeoff": dict(row.get("crowdingTradeoff") or {}),
        "validation": dict(row.get("validation") or {}),
        "eligible": bool((row.get("validation") or {}).get("eligible")),
    } for row in strict_rows]
    eligible = [
        row for row in strict_rows
        if (row.get("validation") or {}).get("eligible")
    ]
    if not eligible:
        return {
            "status": "failed", "eligible_to_apply": False,
            "reason": "no_validated_final_membership_margin",
            "proposal": base, "baseline_proposal": base,
            "search_profile": "final_membership_margin",
            "algorithm": "final_membership_margin_calibration_v1",
            "quick_replay_count": len(cache), "finalists": finalists,
        }
    winner = _select_strict_winner(eligible)
    return {
        "status": "ok", "eligible_to_apply": True,
        "proposal": dict(winner["surface"]),
        "params": {key: winner["surface"][key] for key in TUNE_KEYS},
        "add_params": {key: winner["surface"][key] for key in ADD_TUNE_KEYS},
        "baseline_proposal": base,
        "validation": dict(winner.get("validation") or {}),
        "search_profile": "final_membership_margin",
        "algorithm": "final_membership_margin_calibration_v1",
        "quick_replay_count": len(cache),
        "tier_economics": dict(
            (winner.get("validation") or {}).get("tierEconomics")
            or winner.get("tierEconomics") or {}
        ),
        "deployment_utilization": dict(
            (winner.get("validation") or {}).get("deploymentUtilization")
            or winner.get("deploymentUtilization") or {}
        ),
        "crowding_tradeoff": dict(winner.get("crowdingTradeoff") or {}),
        "finalists": finalists,
    }


def _nearest_axis_neighbours(values, current: float) -> tuple[float | None, float | None]:
    axis = sorted({float(value) for value in values} | {float(current)})
    index = axis.index(float(current))
    return (
        axis[index - 1] if index > 0 else None,
        axis[index + 1] if index + 1 < len(axis) else None,
    )


def local_leverage_surfaces(follow: dict, seed_surface: dict) -> list[dict]:
    """At most two adjacent, notional-paired probes for each volatility tier."""
    seed = _local_complete_surface(follow, seed_surface)
    configured = {
        "STABLE_LEV_CAP": getattr(config, "AUTO_TUNE_COORD_STABLE_LEV_CAPS", (35, 32, 30, 28, 25)),
        "MID_LEV_CAP": getattr(config, "AUTO_TUNE_COORD_MID_LEV_CAPS", (12, 11, 10, 9)),
        "HIGH_LEV_CAP": getattr(config, "AUTO_TUNE_COORD_HIGH_LEV_CAPS", (4, 5, 6)),
    }
    surfaces = []
    for key in LEV_KEYS:
        for value in _nearest_axis_neighbours(configured[key], seed[key]):
            if value is None:
                continue
            surfaces.append(_local_complete_surface(
                follow,
                _pair_margins_for_leverage(seed, {key: value}, follow),
            ))
    return surfaces[:6]


def local_add_surfaces(follow: dict, seed_surface: dict) -> list[dict]:
    """One more conservative and one more aggressive smart-add neighbour."""
    seed = _local_complete_surface(follow, seed_surface)
    axes = {
        "ADD_GAP_K": getattr(config, "AUTO_TUNE_ADD_GAP_KS", (0.04, 0.06, 0.08, 0.10, 0.12)),
        "POS_ADD_GAP_K": getattr(config, "AUTO_TUNE_POS_ADD_GAP_KS", (0.06, 0.08, 0.10, 0.12)),
        "ADD_GAP_SHRINK_G": getattr(config, "AUTO_TUNE_ADD_SHRINK_GS", (1.1, 1.2, 1.3, 1.5)),
        "ADD_MAX_HARD": getattr(config, "AUTO_TUNE_ADD_MAX_HARDS", (4, 6, 8, 10)),
    }
    lower = {}
    upper = {}
    for key, values in axes.items():
        lo, hi = _nearest_axis_neighbours(values, seed[key])
        lower[key] = seed[key] if lo is None else lo
        upper[key] = seed[key] if hi is None else hi
    # Wider gaps/fewer adds are conservative; narrower gaps/more adds are aggressive.
    conservative = {
        **seed,
        "ADD_GAP_K": upper["ADD_GAP_K"],
        "POS_ADD_GAP_K": upper["POS_ADD_GAP_K"],
        "ADD_GAP_SHRINK_G": upper["ADD_GAP_SHRINK_G"],
        "ADD_MAX_HARD": lower["ADD_MAX_HARD"],
    }
    aggressive = {
        **seed,
        "ADD_GAP_K": lower["ADD_GAP_K"],
        "POS_ADD_GAP_K": lower["POS_ADD_GAP_K"],
        "ADD_GAP_SHRINK_G": lower["ADD_GAP_SHRINK_G"],
        "ADD_MAX_HARD": upper["ADD_MAX_HARD"],
    }
    return [conservative, aggressive]


def tune_local_prefix_surfaces(
    *,
    candidate_count: int,
    center_count: int,
    follow: dict,
    evaluate: Callable[[int, dict, str], dict],
    validate: Callable[[int, dict], dict] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict:
    """Count-first bounded local tuning with ±2 guards and at most three strict finalists.

    ``evaluate`` owns replay/cache I/O and returns a compact dict containing at least ``netPnl`` and
    ``feasible``.  The orchestration is pure and bounded, so the scanner can resume cached candidates
    without recreating an old per-count tuner or exact-membership closure.
    """
    candidate_count = max(1, int(candidate_count))
    center_count = max(1, min(candidate_count, int(center_count)))
    base = _local_complete_surface(follow)
    primary_counts = sorted({
        count for count in (center_count - 1, center_count, center_count + 1)
        if 1 <= count <= candidate_count
    })
    guard_counts = sorted({
        count for count in (center_count - 2, center_count + 2)
        if 1 <= count <= candidate_count and count not in primary_counts
    })
    cache = {}
    stage_counts = {}
    stage_durations = {}
    cache_hit_count = 0

    def run(count: int, surface: dict, stage: str) -> dict:
        nonlocal cache_hit_count
        surface = _local_complete_surface(follow, surface)
        key = (int(count), _local_surface_marker(surface))
        if key not in cache:
            started = time.monotonic()
            # ``evaluate`` owns persistent evidence lookup as well as the
            # actual replay.  Checking the memory guard here happened before
            # that lookup, so a resume could be deferred merely while reading
            # an already-computed compact SQLite row.  Cache-miss replay paths
            # perform the guard immediately before allocating their surface.
            value = dict(evaluate(int(count), surface, stage) or {})
            value.update(count=int(count), surface=surface, stage=stage)
            cache[key] = value
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            stage_durations[stage] = stage_durations.get(stage, 0.0) + (
                time.monotonic() - started
            )
            if progress:
                progress(stage, len(cache), 1)
        else:
            cache_hit_count += 1
        return cache[key]

    def rank(row: dict) -> tuple:
        return (
            int(bool(row.get("feasible"))),
            float(row.get("stressNetPnl", row.get("netPnl")) or 0.0),
            float(row.get("netPnl") or 0.0),
            -int(row.get("liquidations") or 0),
            float(row.get("capacityFit") or 0.0),
            float(row.get("openRate") or 0.0),
            float(row.get("addCaptureRate") or 0.0),
            -sum(
                abs(float(row["surface"][key]) - float(base[key]))
                for key in (*TUNE_KEYS, *ADD_TUNE_KEYS)
            ),
        )

    center_baseline = run(center_count, base, "tier_baseline")
    shared_surfaces = local_shared_margin_surfaces(
        follow, base,
        tier_economics=center_baseline.get("tierEconomics") or {},
    )
    breakout_tier = None
    shared_rows = [
        run(count, surface, "tier_shared_candidates")
        for count in primary_counts for surface in shared_surfaces
    ]
    extension_surfaces = grid_upward_extensions(
        follow, base,
        [row for row in shared_rows if row["count"] == center_count],
    )
    shared_rows.extend(
        run(count, surface, "tier_margin_extension")
        for count in primary_counts for surface in extension_surfaces
    )
    for row in shared_rows:
        row["crowdingTradeoff"] = crowding_tradeoff(
            row, run(row["count"], base, "tier_baseline_control"),
        )
    seeds = []
    for count in primary_counts:
        rows = [row for row in shared_rows if row["count"] == count]
        seeds.append(max(rows, key=rank))
    seeds.sort(key=rank, reverse=True)
    best_seed_profit = float(seeds[0].get("netPnl") or 0.0) if seeds else 0.0
    leverage_seeds = list(seeds[:2])
    if len(seeds) > 2 and float(seeds[2].get("netPnl") or 0.0) >= (
        best_seed_profit - max(1.0, abs(best_seed_profit) * 0.02)
    ):
        leverage_seeds.append(seeds[2])
    refined_rows = list(shared_rows)
    for seed in leverage_seeds[:3]:
        refined_rows.extend(
            run(seed["count"], surface, "local_leverage_refine")
            for surface in local_leverage_surfaces(follow, seed["surface"])
        )
    ranked_refined = sorted(refined_rows, key=rank, reverse=True)
    add_bases = []
    seen_pairs = set()
    for row in ranked_refined:
        pair = (row["count"], _local_surface_marker(row["surface"]))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        add_bases.append(row)
        if len(add_bases) >= 2:
            break
    for seed in add_bases:
        refined_rows.extend(
            run(seed["count"], surface, "local_add_refine")
            for surface in local_add_surfaces(follow, seed["surface"])
        )

    # Keep at most four distinct parameter surfaces and cross them over the three primary counts.
    finalist_surfaces = []
    seen_surfaces = set()
    for row in sorted(refined_rows, key=rank, reverse=True):
        marker = _local_surface_marker(row["surface"])
        if marker in seen_surfaces:
            continue
        seen_surfaces.add(marker)
        finalist_surfaces.append(row["surface"])
        if len(finalist_surfaces) >= 4:
            break
    cross_rows = [
        run(count, surface, "primary_finalist_cross")
        for count in primary_counts for surface in finalist_surfaces
    ]
    for row in cross_rows:
        row["crowdingTradeoff"] = crowding_tradeoff(
            row, run(row["count"], base, "primary_baseline_control"),
        )
    primary_best = max(cross_rows, key=rank)

    guard_rows = []
    for count in guard_counts:
        for surface in [base, *finalist_surfaces]:
            guard_rows.append(run(count, surface, "guard_validation"))
    promotable = []
    primary_profit = float(primary_best.get("netPnl") or 0.0)
    for row in guard_rows:
        if not row.get("feasible"):
            continue
        profit = float(row.get("netPnl") or 0.0)
        if row["count"] < center_count and profit >= primary_profit * 0.98:
            promotable.append(row)
        elif row["count"] > center_count and profit >= primary_profit * 1.02:
            promotable.append(row)
    promoted = max(promotable, key=rank) if promotable else None
    if promoted is not None:
        promotion_rows = [
            run(promoted["count"], surface, "guard_promotion_refine")
            for surface in local_shared_margin_surfaces(follow, promoted["surface"])
        ]
        promoted = max([promoted, *promotion_rows], key=rank)

    strict_pool = [*cross_rows, *([promoted] if promoted is not None else [])]
    strict_candidates = []
    seen_pairs = set()
    for row in sorted(strict_pool, key=rank, reverse=True):
        pair = (row["count"], _local_surface_marker(row["surface"]))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        strict_candidates.append(row)
        if len(strict_candidates) >= 3:
            break
    standard = next(
        (row for row in sorted(strict_pool, key=rank, reverse=True) if row.get("feasible")),
        None,
    )
    if standard is not None:
        crowded = _profitable_crowding_challenger(
            [*refined_rows, *cross_rows, *guard_rows], standard, base,
        )
        if crowded is not None:
            strict_candidates = [standard, crowded, *strict_candidates]
    baseline_pair = (center_count, _local_surface_marker(base))
    if baseline_pair not in {
        (row["count"], _local_surface_marker(row["surface"]))
        for row in strict_candidates
    }:
        baseline_row = run(center_count, base, "strict_baseline_control")
        strict_candidates = [*strict_candidates[:2], baseline_row]
    deduped = []
    seen_pairs = set()
    for row in strict_candidates:
        pair = (row["count"], _local_surface_marker(row["surface"]))
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            deduped.append(row)
    strict_candidates = deduped[:3]
    strict_rows = []
    for row in strict_candidates:
        validation = dict(validate(row["count"], row["surface"]) or {}) if validate else {
            "eligible": bool(row.get("feasible")),
        }
        strict_rows.append({**row, "validation": validation})
    audit_finalists = [{
        "count": int(row["count"]),
        "params": dict(row["surface"]),
        "netPnl": float(row.get("netPnl") or 0.0),
        "stressNetPnl": float(row.get("stressNetPnl", row.get("netPnl")) or 0.0),
        "feasible": bool(row.get("feasible")),
        "liquidations": int(row.get("liquidations") or 0),
        "openRate": float(row.get("openRate") or 0.0),
        "capacityFit": float(row.get("capacityFit") or 0.0),
        "tierEconomics": dict(row.get("tierEconomics") or {}),
        "deploymentUtilization": dict(row.get("deploymentUtilization") or {}),
        "crowdingTradeoff": dict(row.get("crowdingTradeoff") or {}),
        "validation": dict(row.get("validation") or {}),
        "eligible": bool((row.get("validation") or {}).get("eligible")),
    } for row in strict_rows]
    eligible = [row for row in strict_rows if (row.get("validation") or {}).get("eligible")]
    if not eligible:
        return {
            "status": "failed", "eligible_to_apply": False,
            "reason": "no_validated_local_finalist", "proposal": base,
            "validation": {"eligible": False, "reasons": ["no_validated_local_finalist"]},
            "search_profile": "local", "algorithm": "count_first_local_surface_v1",
            "count_center": center_count, "primary_counts": primary_counts,
            "guard_counts": guard_counts, "guard_promoted": None,
            "shared_surface_count": len(shared_surfaces),
            "breakout_tier": breakout_tier,
            "quick_replay_count": len(cache), "stage_counts": stage_counts,
            "cache_hit_count": cache_hit_count,
            "stage_durations": {key: round(value, 3) for key, value in stage_durations.items()},
            "finalists": audit_finalists,
        }
    winner = _select_strict_winner(eligible)
    validation = dict(winner.get("validation") or {})
    return {
        "status": "ok", "eligible_to_apply": True,
        "proposal": dict(winner["surface"]), "params": {
            key: winner["surface"][key] for key in TUNE_KEYS
        }, "add_params": {
            key: winner["surface"][key] for key in ADD_TUNE_KEYS
        },
        "baseline_proposal": base,
        "validation": validation,
        "search_profile": "local", "algorithm": "count_first_local_surface_v1",
        "count_center": center_count, "selected_count": int(winner["count"]),
        "primary_counts": primary_counts, "guard_counts": guard_counts,
        "guard_promoted": int(promoted["count"]) if promoted is not None else None,
        "shared_surface_count": len(shared_surfaces),
        "breakout_tier": breakout_tier,
        "quick_replay_count": len(cache), "stage_counts": stage_counts,
        "cache_hit_count": cache_hit_count,
        "stage_durations": {key: round(value, 3) for key, value in stage_durations.items()},
        "tier_economics": (
            validation.get("tierEconomics") or winner.get("tierEconomics") or {}
        ),
        "deployment_utilization": (
            validation.get("deploymentUtilization")
            or winner.get("deploymentUtilization") or {}
        ),
        "crowding_tradeoff": winner.get("crowdingTradeoff") or {},
        "finalists": audit_finalists,
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
                       time_budget_s: float | None = None,
                       window_fills_override: dict[int, list[dict]] | None = None) -> dict:
    """Run the post-scan margin tuner. Returns a compact audit dict."""
    ephemeral = addrs_override is not None
    if ephemeral and expected_generation:
        raise ValueError("addrs_override cannot target a published generation")
    search_profile = str(search_profile or "full").strip().lower()
    if search_profile not in {"coarse", "efficient", "full"}:
        raise ValueError("search_profile must be coarse, efficient or full")
    coarse_search = search_profile == "coarse"
    efficient_search = search_profile == "efficient"
    tune_started = time.monotonic()
    def check_budget(stage):
        # Candidate counts bound the work.  Wall-clock time never invalidates a generation;
        # only the resource guard may defer it for a smaller resumable batch.
        resource_guard.require_replay_budget()

    stamp = stamp or now_iso()
    params.seed_params(db)
    # ``seed_params`` uses INSERT OR IGNORE and therefore opens a SQLite writer transaction even when every
    # row already exists. Parameter evaluation can run for tens of minutes; never carry that harmless seed
    # write across the grid or block Observer/full/daily runtime writes for the entire search.
    db.commit()
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
        if ephemeral else _load_followed_wallets(db)
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
    if window_fills_override is None:
        raw_fill_bytes = _portfolio_fill_json_bytes(
            db,
            addrs,
            now_ms - (max(_tune_days()) + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7))) * 86_400_000,
        )
        resource_guard.require_replay_budget(raw_fill_bytes)
        window_fills = _portfolio_window_fills(
            db, addrs, now_ms, include_watch=bool(formation_admission),
        )
    else:
        window_fills = window_fills_override
    resource_guard.require_replay_budget()
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
    market_ctx = _load_market_ctx(db, market_generation)
    if formation_admission:
        # Parameter search is fills-only.  Loading/refining K-lines for every grid finalist made formation
        # slower than the scan itself. The full run nevertheless validates its small finalist set against
        # the already-prefetched immutable path cache, so lowering leverage/moving margin can actually repair
        # proxy liquidations before final wallet admission. Coarse count probes remain fills-only.
        path_rows, path_meta = None, {}
        validation_path_rows, validation_path_meta = None, {}
        if not coarse_search:
            path_fills = list(window_fills.get(max(window_fills)) or [])
            path_start = now_ms - (
                max(window_fills) + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7))
            ) * 86_400_000
            validation_path_rows = prepare_price_path(price_path.load_refined(
                db, path_fills, path_start, now_ms,
            ))
            validation_path_meta = price_path.coverage(
                db, path_fills, path_start, now_ms,
            )
    else:
        path_fills = list(window_fills.get(max(window_fills)) or [])
        path_start = now_ms - (
            max(window_fills) + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7))
        ) * 86_400_000
        path_rows, path_meta = prepare_refined_price_path(
            db, path_fills, path_start, now_ms, sigmas=sigmas, overrides=follow,
            market_ctx=market_ctx, immutable_market_ctx=bool(market_generation),
        )
        path_rows = prepare_price_path(path_rows)
        validation_path_rows, validation_path_meta = path_rows, path_meta
    candidate_result_cache = {}
    # These adaptive batches all consume the same immutable fills and market context. Keep it resident in
    # one worker set instead of respawning processes and copying the 30-day context for every search axis.
    candidate_worker_pool = replay_parallel.ReusableOrderedPool(
        initializer=_init_process_replay_context,
        initargs=({
            "addrs": list(addrs),
            "sigmas": sigmas,
            "now_ms": int(now_ms),
            "window_fills": window_fills,
            "path_rows": path_rows,
            "path_meta": path_meta,
            "market_ctx": market_ctx,
        },),
    )
    # First tune stable/mid/high independently, including upward high-tier probes, then combine only each
    # tier's current/best-profit/fewest-liquidation values. This preserves tier attribution without paying
    # for a full leverage Cartesian grid.
    leverage_axis_candidates = (
        coarse_leverage_candidates(base, follow)
        if coarse_search else independent_leverage_candidates(base, follow)
    )
    for _candidate in leverage_axis_candidates:
        check_budget("leverage_axes")
    axis_quick = _evaluate_candidates_parallel(
        leverage_axis_candidates,
        kind="tune", addrs=addrs, follow=follow, sigmas=sigmas, now_ms=now_ms,
        window_fills=window_fills, path_rows=path_rows, path_meta=path_meta,
        primary_only=True, market_ctx=market_ctx,
        result_cache=candidate_result_cache, worker_pool=candidate_worker_pool,
    )
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
    combo_candidates = []
    if efficient_search:
        # Keep the active, best-profit and lowest-liquidation coordinated directions without expanding
        # their tier choices into a 3^3 Cartesian grid. Independent tier moves remain in ``axis_quick``.
        combination_values = [
            tuple(
                tier_values[key][min(level, len(tier_values[key]) - 1)]
                for key in LEV_KEYS
            )
            for level in range(max(len(tier_values[key]) for key in LEV_KEYS))
        ]
    else:
        combination_values = itertools.product(*(tier_values[key] for key in LEV_KEYS))
    for values in combination_values:
        check_budget("leverage_combinations")
        combo_candidates.append(_candidate_from_params(
            _pair_margins_for_leverage(base, dict(zip(LEV_KEYS, values)), follow),
            axis="notional_paired_leverage_combination",
        ))
    combo_quick = _evaluate_candidates_parallel(
        combo_candidates,
        kind="tune", addrs=addrs, follow=follow, sigmas=sigmas, now_ms=now_ms,
        window_fills=window_fills, path_rows=path_rows, path_meta=path_meta,
        primary_only=True, market_ctx=market_ctx,
        result_cache=candidate_result_cache, worker_pool=candidate_worker_pool,
    )
    joint_quick = axis_quick + combo_quick
    quick_valid = [candidate for candidate in joint_quick if _candidate_valid(candidate, quick_baseline)]
    sizing_limit = (
        2 if coarse_search else
        max(2, int(getattr(config, "AUTO_TUNE_EFFICIENT_SIZING_FINALISTS", 6) or 6))
        if efficient_search else
        max(2, int(getattr(config, "AUTO_TUNE_SIZING_FINALISTS", 12) or 12))
    )
    quick_finalists = sorted(
        quick_valid or [quick_baseline],
        key=lambda candidate: _candidate_rank_key(candidate, quick_baseline), reverse=True,
    )[:sizing_limit]
    if not any(_same_tune_values(candidate.get("params") or {}, base) for candidate in quick_finalists):
        quick_finalists.append(quick_baseline)
    joint_inputs = []
    for candidate in quick_finalists:
        check_budget("joint_finalists")
        joint_inputs.append(
            _candidate_from_params(candidate.get("params") or base, axis="joint_finalist")
        )
    joint_candidates = _evaluate_candidates_parallel(
        joint_inputs,
        kind="tune", addrs=addrs, follow=follow, sigmas=sigmas, now_ms=now_ms,
        window_fills=window_fills, path_rows=path_rows, path_meta=path_meta,
        primary_only=(coarse_search or efficient_search), market_ctx=market_ctx,
        result_cache=candidate_result_cache, worker_pool=candidate_worker_pool,
    )
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
    margin_seed_inputs = list(capacity_margin_candidates(joint_params, follow))
    for _candidate in margin_seed_inputs:
        check_budget("capacity_margin_grid")
    if formation_admission and not coarse_search:
        global_inputs = list(global_margin_candidates(joint_params, follow))
        for _candidate in global_inputs:
            check_budget("global_margin_polish")
        margin_seed_inputs.extend(global_inputs)
    margin_candidates.extend(_evaluate_candidates_parallel(
        margin_seed_inputs,
        kind="tune", addrs=addrs, follow=follow, sigmas=sigmas, now_ms=now_ms,
        window_fills=window_fills, path_rows=path_rows, path_meta=path_meta,
        primary_only=(coarse_search or efficient_search), market_ctx=market_ctx,
        result_cache=candidate_result_cache, worker_pool=candidate_worker_pool,
    ))
    selected_margin_seed = choose_margin_candidate(
        [selected_joint, *margin_candidates], baseline,
    )
    margin_params = dict(selected_margin_seed.get("params") or joint_params)
    margin_round_limit = (
        0 if coarse_search else
        max(
            1,
            int(getattr(config, "AUTO_TUNE_EFFICIENT_MARGIN_COORD_ROUNDS", 1) or 1),
        )
        if efficient_search else
        max(1, int(getattr(config, "AUTO_TUNE_MARGIN_COORD_ROUNDS", 2) or 2))
    )
    for round_index in range(margin_round_limit):
        round_inputs = list(independent_margin_candidates(margin_params, follow))
        for _candidate in round_inputs:
            check_budget("margin_polish")
        round_candidates = _evaluate_candidates_parallel(
            round_inputs,
            kind="tune", addrs=addrs, follow=follow, sigmas=sigmas, now_ms=now_ms,
            window_fills=window_fills, path_rows=path_rows, path_meta=path_meta,
            primary_only=efficient_search, market_ctx=market_ctx,
            result_cache=candidate_result_cache, worker_pool=candidate_worker_pool,
        )
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
    selected = choose_margin_candidate(
        [*joint_candidates, *margin_candidates, baseline],
        baseline,
    )
    candidates = joint_candidates + margin_candidates
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
        add_inputs = list(add_candidates_from_axes(add_base))
        for _candidate in add_inputs:
            check_budget("add_polish")
        add_candidates = _evaluate_candidates_parallel(
            add_inputs,
            kind="add", addrs=addrs, follow=follow_for_add,
            sigmas=sigmas, now_ms=now_ms, window_fills=window_fills,
            path_rows=path_rows, path_meta=path_meta,
            primary_only=efficient_search, market_ctx=market_ctx,
            result_cache=candidate_result_cache, worker_pool=candidate_worker_pool,
        )
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
    finalist_limit = (
        2 if coarse_search else
        max(2, int(getattr(config, "AUTO_TUNE_EFFICIENT_FINALIST_LIMIT", 4) or 4))
        if efficient_search else
        int(getattr(config, "AUTO_TUNE_FINALIST_LIMIT", 16) or 16)
    )
    sizing_options = (
        _efficient_pareto_sizing_candidates(
            list(unique_finalists.values()), baseline, max(1, finalist_limit),
        )
        if efficient_search else
        _diverse_sizing_candidates(
            list(unique_finalists.values()), baseline, max(1, finalist_limit),
        )
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
    if not any(
        _same_tune_values(candidate.get("params") or {}, base)
        for candidate in sizing_options
    ):
        # The active baseline is a required Pareto control. Admission leaders may reorder the shortlist,
        # but they may not evict the only exact baseline before path validation.
        sizing_options = [
            *sizing_options[:max(0, finalist_limit - 1)],
            baseline,
        ]
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
            add_limit = (
                1 if coarse_search else
                max(1, int(getattr(config, "AUTO_TUNE_EFFICIENT_ADD_FINALISTS", 2) or 2))
                if efficient_search else
                max(1, int(getattr(config, "AUTO_TUNE_ADD_FINALISTS", 3) or 3))
            )
            if len(add_options) >= add_limit:
                break
    else:
        add_options = [selected_add_params]
    # Walk-forward uses a different compact context and therefore starts its own one-shot batch below.
    candidate_worker_pool.close()
    all_combined_options = sorted(
        (
            (sizing_rank + add_rank, sizing_rank, add_rank, sizing_candidate, add_params)
            for sizing_rank, sizing_candidate in enumerate(sizing_options)
            for add_rank, add_params in enumerate(add_options)
        ),
        key=lambda row: (row[0], row[1], row[2]),
    )
    combined_options = all_combined_options[:max(1, finalist_limit)]
    baseline_option = next((
        row for row in all_combined_options
        if _same_tune_values(row[3].get("params") or {}, base)
        and _same_add_values(row[4], current_add)
    ), None)
    if baseline_option is not None and baseline_option not in combined_options:
        combined_options = [
            *combined_options[:max(0, finalist_limit - 1)],
            baseline_option,
        ]
    prepared_options = []
    for row in combined_options:
        check_budget("walk_forward")
        sizing_candidate, finalist_add_params = row[3], row[4]
        sizing_params = sizing_candidate.get("params") or base
        prepared_options.append((
            row, sizing_candidate, sizing_params, finalist_add_params,
            {**sizing_params, **finalist_add_params},
        ))
    batched_validations = (
        _walk_forward_validation_batch(
            addrs, follow, [item[4] for item in prepared_options],
            sigmas, window_fills, now_ms,
            path_rows=validation_path_rows, path_meta=validation_path_meta,
            market_ctx=market_ctx, fold_days=10, fold_count=3,
        )
        if formation_admission else
        [None] * len(prepared_options)
    )
    finalist_results = []
    chosen = None
    eligible_choices = []
    for option, validation in zip(prepared_options, batched_validations):
        _row, sizing_candidate, sizing_params, finalist_add_params, combined = option
        if validation is None:
            validation = _walk_forward_validation(
                addrs, follow, combined, sigmas, window_fills, now_ms,
                path_rows=validation_path_rows, path_meta=validation_path_meta,
                market_ctx=market_ctx,
                fold_days=10,
                fold_count=3,
            )
        model = (
            _formation_model_validation(validation)
            if formation_admission else _model_validation(validation, load_copy_policy(follow))
        )
        finalist_results.append({
            "params": combined,
            "eligible": model["eligible"],
            "reasons": model["reasons"],
            "relativeGain": model["relativeGain"],
            "safetyRepair": bool(model.get("safetyRepair")),
            "baselineLiquidations": int(model.get("baselineLiquidations") or 0),
            "challengerLiquidations": int(model.get("challengerLiquidations") or 0),
            "profitRetention": model.get("profitRetention"),
        })
        if model["eligible"]:
            choice = (sizing_candidate, sizing_params, finalist_add_params, combined, validation)
            if not formation_admission:
                chosen = choice
                break
            eligible_choices.append(choice)
    if formation_admission and eligible_choices:
        def path_profit(choice):
            validation = choice[4]
            return sum(
                float(row.get("challengerNet") or 0.0)
                for row in (validation.get("folds") or ())
            )

        best_profit = max(path_profit(choice) for choice in eligible_choices)
        tolerance = abs(best_profit) * float(
            getattr(config, "AUTO_TUNE_NEAR_BEST_PROFIT_REL", 0.08)
        )
        near_best = [
            choice for choice in eligible_choices
            if path_profit(choice) + tolerance + 1e-9 >= best_profit
        ]

        def path_rank(choice):
            sizing_candidate, _sizing, _adds, _combined, validation = choice
            liquidations = sum(
                int(row.get("challengerLiquidations") or 0)
                for row in (validation.get("folds") or ())
            )
            return (
                -liquidations,
                *_candidate_execution_priority(sizing_candidate),
                path_profit(choice),
            )

        # Profit defines the near-best band; inside it, prefer the fewest path liquidations, then execution
        # fit and residual profit. Zero is not required—the final wallet contract explicitly allows <=3.
        chosen = max(near_best, key=path_rank)
    no_validated_finalist = chosen is None
    if no_validated_finalist:
        # No proposal passed the tuning diagnostics. Return the exact active baseline for audit, never an
        # attractive but invalid in-sample fallback. Callers may safely retain it while publishing a Core
        # formed under current parameters.
        selected = baseline
        selected_params = {key: float(current[key]) for key in TUNE_KEYS}
        selected_add_params = {key: float(current_add[key]) for key in ADD_TUNE_KEYS}
        combined = {**selected_params, **selected_add_params}
        validation = next((
            value for item, value in zip(prepared_options, batched_validations)
            if _same_tune_values(item[4], combined)
            and _same_add_values(item[4], combined)
        ), None)
        if validation is None:
            validation = _walk_forward_validation_batch(
                addrs, follow, [combined], sigmas, window_fills, now_ms,
                path_rows=validation_path_rows, path_meta=validation_path_meta,
                market_ctx=market_ctx, fold_days=10, fold_count=3,
            )[0] if formation_admission else _walk_forward_validation(
                addrs, follow, combined, sigmas, window_fills, now_ms,
                path_rows=validation_path_rows, path_meta=validation_path_meta,
                market_ctx=market_ctx, fold_days=10, fold_count=3,
            )
        chosen = (selected, selected_params, selected_add_params, combined, validation)
    selected, selected_params, selected_add_params, proposal_combined, walk_forward = chosen
    selected_margins = {key: selected_params[key] for key in MARGIN_KEYS}
    follow_for_add = follow_overrides_for_tune_candidate(follow, selected)
    if ephemeral:
        model = (
            _formation_model_validation(walk_forward)
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
            "safetyRepair": bool(model.get("safetyRepair")),
            "baselineLiquidations": int(model.get("baselineLiquidations") or 0),
            "challengerLiquidations": int(model.get("challengerLiquidations") or 0),
            "profitRetention": model.get("profitRetention"),
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
            "safetyRepair": False,
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
            "reserved_adds": int(getattr(config, "SMART_ADD_MIN_CAPACITY", 2) or 2),
            "margin_ceilings": margin_add_capacity_ceilings(follow),
        },
        "lev_caps": selected.get("lev_caps"),
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
        "congestion_rounds": [],
        "selection_priority": {
            "order": ["profit", "liquidations", "capacity", "open_rate", "add_fidelity"],
            "near_best_profit_rel": float(
                getattr(config, "AUTO_TUNE_NEAR_BEST_PROFIT_REL", 0.08)
            ),
            "preferred_liquidations_30d": int(
                getattr(config, "AUTO_TUNE_PREFERRED_LIQUIDATIONS_30D", 3)
            ),
            "safety_profit_retention": float(
                getattr(config, "AUTO_TUNE_SAFETY_PROFIT_RETENTION", 0.90)
            ),
            "notional_paired_leverage": True,
        },
        "formation_admission": bool(formation_admission),
        "search_profile": search_profile,
        "elapsed_s": round(time.monotonic() - tune_started, 3),
    }
    if record_run:
        _record_run(db, source, stamp, selected, applied, len(addrs), base, result,
                    generation_id=expected_generation)
    db.commit()
    return result
