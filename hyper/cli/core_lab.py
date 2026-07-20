#!/usr/bin/env python3
"""Read-only Core-selection laboratory.

Runs the experimental multi-start/multi-move optimizer against an existing DB.
The connection is opened with ``mode=ro`` and ``PRAGMA query_only``; this command
cannot publish a generation, alter params, enqueue commands, or reload Observer.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone

from hyper import config, params
from hyper.copy.copy_backtest import run_backtest, slice_backtest_result
from hyper.copy.copy_policy import load_copy_policy
from hyper.discovery import scanner
from hyper.market import price_path
from hyper.selection import auto_tune, offline_core_optimizer, state as selection
from hyper.util import f


def _iso_ms(value: str | None) -> int:
    if not value:
        return int(time.time() * 1000)
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _mask(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


def _metric_net(metrics: selection.PortfolioMetrics) -> float:
    return f(metrics.net_pnl if metrics.net_pnl is not None else metrics.net_lcb)


def _metric_utility(metrics: selection.PortfolioMetrics) -> float:
    return f(
        metrics.risk_adjusted_utility
        if metrics.risk_adjusted_utility is not None else _metric_net(metrics)
    )


def _combine_conservative(normal: selection.PortfolioMetrics,
                          liquidate: selection.PortfolioMetrics) -> selection.PortfolioMetrics:
    net = min(_metric_net(normal), _metric_net(liquidate))
    stress = min(f(normal.stress_net_pnl), f(liquidate.stress_net_pnl))
    drawdown = max(f(normal.drawdown_dollars), f(liquidate.drawdown_dollars))
    utility = min(_metric_utility(normal), _metric_utility(liquidate), net - drawdown)
    return selection.PortfolioMetrics(
        net_lcb=net,
        stress_net_lcb=stress,
        liquidations=max(int(normal.liquidations), int(liquidate.liquidations)),
        actionable_open_rate=min(
            f(normal.actionable_open_rate), f(liquidate.actionable_open_rate)
        ),
        capacity_fit=min(f(normal.capacity_fit), f(liquidate.capacity_fit)),
        max_drawdown=max(f(normal.max_drawdown), f(liquidate.max_drawdown)),
        peak_deploy_pct=max(f(normal.peak_deploy_pct), f(liquidate.peak_deploy_pct)),
        cost_drag_ratio=max(f(normal.cost_drag_ratio), f(liquidate.cost_drag_ratio)),
        net_pnl=net,
        stress_net_pnl=stress,
        drawdown_dollars=drawdown,
        risk_adjusted_utility=utility,
    )


def _param_hash(follow: dict) -> str:
    raw = json.dumps(follow, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _compact_metrics(metrics: selection.PortfolioMetrics, details: dict | None = None) -> dict:
    out = {
        "net30": round(_metric_net(metrics), 2),
        "riskAdjustedUtility": round(_metric_utility(metrics), 2),
        "stressNet": round(f(metrics.stress_net_pnl), 2),
        "drawdownDollars": round(f(metrics.drawdown_dollars), 2),
        "liquidations": int(metrics.liquidations),
        "openRate": round(f(metrics.actionable_open_rate), 4),
        "capacityFit": round(f(metrics.capacity_fit), 4),
        "peakDeploy": round(f(metrics.peak_deploy_pct), 4),
    }
    if details:
        out.update({
            "net14": round(f(details.get("net14")), 2),
            "net7": round(f(details.get("net7")), 2),
            "closed30": int(details.get("closed30") or 0),
            "pricePathCoverage": round(f(details.get("coverage")), 5),
        })
    return out


class ReplayLab:
    def __init__(self, db, candidates: list[str], now_ms: int, base_follow: dict):
        self.db = db
        self.candidates = candidates
        self.now_ms = now_ms
        self.base_follow = dict(base_follow)
        self.window_fills = auto_tune._portfolio_window_fills(db, candidates, now_ms)
        if not self.window_fills or not any(self.window_fills.values()):
            raise RuntimeError("offline_fill_cache_unavailable")
        self.sigmas = auto_tune._load_sigmas(db)
        self.market_ctx = auto_tune._load_market_ctx(db)
        self.path_start = now_ms - (
            max(self.window_fills) + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7))
        ) * 86_400_000
        all_fills = list(self.window_fills.get(max(self.window_fills)) or [])
        self.path_rows = price_path.load_refined(db, all_fills, self.path_start, now_ms)
        self.path_meta = price_path.coverage(db, all_fills, self.path_start, now_ms)
        self.fast_cache = {}
        self.strict_cache = {}
        self.fold_cache = {}
        self.details = {}

    def _surface(self, follow: dict | None) -> dict:
        out = dict(follow or self.base_follow)
        if "SMART_ADD" in out:
            out["ADD_STRATEGY"] = "smart" if out["SMART_ADD"] else "hardcap"
        return out

    def fast(self, follow: dict | None = None):
        surface = self._surface(follow)
        surface_hash = _param_hash(surface)

        def evaluate(addrs: tuple[str, ...]) -> selection.PortfolioMetrics:
            key = (surface_hash, tuple(sorted(addrs)))
            if key not in self.fast_cache:
                allowed = set(addrs)
                filtered = {
                    30: [
                        row for row in (self.window_fills.get(30) or [])
                        if (row.get("user") or "").lower() in allowed
                    ]
                }
                primary = auto_tune.evaluate_portfolio_window(
                    self.db, list(addrs), self.sigmas, surface, self.now_ms,
                    window_fills=filtered, days=30, market_ctx=self.market_ctx,
                )
                self.fast_cache[key] = scanner._portfolio_selection_metrics(
                    {30: primary}, baseline_n=0, selected_n=len(addrs),
                )
            return self.fast_cache[key]

        return evaluate

    def strict(self, follow: dict | None = None):
        surface = self._surface(follow)
        surface_hash = _param_hash(surface)

        def evaluate(addrs: tuple[str, ...]) -> selection.PortfolioMetrics:
            addrs = tuple(sorted(addrs))
            key = (surface_hash, addrs)
            if key in self.strict_cache:
                return self.strict_cache[key]
            if not addrs:
                value = scanner._portfolio_selection_metrics({}, selected_n=0)
                self.strict_cache[key] = value
                return value
            filtered = auto_tune._filter_window_fills_by_addr(self.window_fills, addrs)
            fills30 = list(filtered.get(30) or [])
            meta = price_path.coverage(self.db, fills30, self.path_start, self.now_ms)
            normal_windows = auto_tune._candidate_windows(
                self.db, list(addrs), self.sigmas, surface, self.now_ms,
                window_fills=filtered, market_ctx=self.market_ctx,
                path_rows=self.path_rows, path_meta=meta,
            )
            worst_windows = auto_tune._candidate_windows(
                self.db, list(addrs), self.sigmas,
                {**surface, "AMBIGUOUS_PATH_MODE": "liquidate"}, self.now_ms,
                window_fills=filtered, market_ctx=self.market_ctx,
                path_rows=self.path_rows, path_meta=meta,
            )
            normal = scanner._portfolio_selection_metrics(
                normal_windows, baseline_n=0, selected_n=len(addrs),
            )
            worst = scanner._portfolio_selection_metrics(
                worst_windows, baseline_n=0, selected_n=len(addrs),
            )
            value = _combine_conservative(normal, worst)
            primary_normal = normal_windows.get(30) or normal_windows.get(max(normal_windows))
            primary_worst = worst_windows.get(30) or worst_windows.get(max(worst_windows))
            self.details[key] = {
                "net14": min(
                    f((normal_windows.get(14) or {}).get("copy_net_pnl")),
                    f((worst_windows.get(14) or {}).get("copy_net_pnl")),
                ),
                "net7": min(
                    f((normal_windows.get(7) or {}).get("copy_net_pnl")),
                    f((worst_windows.get(7) or {}).get("copy_net_pnl")),
                ),
                "closed30": min(
                    int(primary_normal.get("closed_n") or 0),
                    int(primary_worst.get("closed_n") or 0),
                ),
                "coverage": f(meta.get("coverage")),
            }
            self.strict_cache[key] = value
            return value

        return evaluate

    def detail(self, follow: dict, addrs: tuple[str, ...]) -> dict:
        return self.details.get((_param_hash(self._surface(follow)), tuple(sorted(addrs))), {})

    def fold(self, addrs: tuple[str, ...], follow: dict, older: int, newer: int,
             *, cost_mult: float = 1.0) -> selection.PortfolioMetrics:
        surface = self._surface(follow)
        addrs = tuple(sorted(addrs))
        key = (_param_hash(surface), addrs, int(older), int(newer), float(cost_mult))
        if key in self.fold_cache:
            return self.fold_cache[key]
        lo = self.now_ms - int(older) * 86_400_000
        hi = self.now_ms - int(newer) * 86_400_000 if newer else self.now_ms + 1
        warmup = int(getattr(config, "COPY_BT_WARMUP_DAYS", 7)) * 86_400_000
        allowed = set(addrs)
        source = list(self.window_fills.get(max(self.window_fills)) or [])
        fills = [
            row for row in source
            if (row.get("user") or "").lower() in allowed
            and lo - warmup <= int(row.get("time") or 0) < hi
        ]
        result = run_backtest(
            "portfolio", fills, sigmas=self.sigmas,
            overrides={
                **surface, "AMBIGUOUS_PATH_MODE": "liquidate",
                "REPLAY_COST_MULT": float(cost_mult),
            },
            market_ctx=self.market_ctx, price_path=self.path_rows,
            price_path_meta=self.path_meta,
        )
        sliced = slice_backtest_result(
            result, lo, window_days=max(1, int((hi - lo) / 86_400_000)),
        )
        value = scanner._portfolio_selection_metrics(
            {max(1, int(older) - int(newer)): sliced}, selected_n=len(addrs),
        )
        self.fold_cache[key] = value
        return value

    def robust_compare(self, base_addrs: tuple[str, ...], trial_addrs: tuple[str, ...],
                       follow: dict, constraints: selection.SelectionConstraints):
        base = self.strict(follow)(base_addrs)
        trial = self.strict(follow)(trial_addrs)
        windows = ((30, 20), (20, 10), (10, 0))
        base_folds = [self.fold(base_addrs, follow, *window) for window in windows]
        trial_folds = [self.fold(trial_addrs, follow, *window) for window in windows]
        base_stress = self.fold(base_addrs, follow, 10, 0, cost_mult=1.5)
        trial_stress = self.fold(trial_addrs, follow, 10, 0, cost_mult=1.5)
        return offline_core_optimizer.robust_improvement(
            base, trial, base_folds, trial_folds, base_stress, trial_stress, constraints,
        )

    def parameter_validation(self, addrs: tuple[str, ...], base_follow: dict,
                             proposal_follow: dict) -> dict:
        filtered = auto_tune._filter_window_fills_by_addr(self.window_fills, addrs)
        proposal = {
            key: proposal_follow[key]
            for key in (*auto_tune.TUNE_KEYS, *auto_tune.ADD_TUNE_KEYS)
        }
        validation = auto_tune._walk_forward_validation(
            list(addrs), base_follow, proposal, self.sigmas, filtered, self.now_ms,
            path_rows=self.path_rows, path_meta=self.path_meta,
            market_ctx=self.market_ctx,
        )
        model = auto_tune._model_validation(validation, load_copy_policy(base_follow))
        return {**validation, **model}


def _nearest_axis_candidates(base_follow: dict) -> list[tuple[str, dict]]:
    tune_base = {key: f(base_follow[key]) for key in auto_tune.TUNE_KEYS}
    add_base = {key: f(base_follow[key]) for key in auto_tune.ADD_TUNE_KEYS}
    rows: list[tuple[str, dict]] = [("current", dict(base_follow))]

    def keep_nearest(candidates, keys, kind):
        by_axis = {}
        for candidate in candidates:
            values = candidate.get("params") or {}
            changed = [key for key in keys if abs(f(values.get(key)) - f(base_follow.get(key))) > 1e-12]
            if len(changed) != 1:
                continue
            key = changed[0]
            by_axis.setdefault(key, []).append(candidate)
        for key, items in by_axis.items():
            current = f(base_follow[key])
            below = [row for row in items if f((row.get("params") or {}).get(key)) < current]
            above = [row for row in items if f((row.get("params") or {}).get(key)) > current]
            picks = []
            if below:
                picks.append(max(below, key=lambda row: f((row.get("params") or {}).get(key))))
            if above:
                picks.append(min(above, key=lambda row: f((row.get("params") or {}).get(key))))
            for candidate in picks:
                values = candidate.get("params") or {}
                rows.append((f"{kind}:{key}={values[key]}", {**base_follow, **values}))

    keep_nearest(auto_tune.independent_leverage_candidates(tune_base), auto_tune.LEV_KEYS, "lev")
    keep_nearest(auto_tune.independent_margin_candidates(tune_base, base_follow), auto_tune.MARGIN_KEYS, "margin")
    keep_nearest(auto_tune.deploy_candidates(tune_base), auto_tune.DEPLOY_KEYS, "deploy")
    keep_nearest(auto_tune.add_candidates_from_axes(add_base), auto_tune.ADD_TUNE_KEYS, "add")
    dedup, out = set(), []
    for label, values in rows:
        marker = _param_hash(values)
        if marker not in dedup:
            dedup.add(marker)
            out.append((label, values))
    return out


def _better(item):
    _label, _surface, metrics = item
    return (
        _metric_utility(metrics), _metric_net(metrics), f(metrics.stress_net_pnl),
        -f(metrics.drawdown_dollars),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only offline Core optimizer")
    ap.add_argument("--db", required=True)
    ap.add_argument(
        "--time-budget", type=float, default=0,
        help="optional wall-clock cutoff in seconds; 0 means no cutoff",
    )
    ap.add_argument("--finalists", type=int, default=12)
    ap.add_argument("--strict-moves", type=int, default=16)
    ap.add_argument("--skip-param-polish", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    generation = selection.latest_published_generation(db)
    if not generation:
        raise RuntimeError("no_published_generation")
    rows = db.execute(
        "SELECT lower(addr) addr,lower(role) role,COALESCE(follow_score,utility,0) score "
        "FROM follow_selection WHERE generation=? AND role IN ('core','challenger') "
        "AND COALESCE(enabled,1)=1 ORDER BY score DESC,addr",
        (generation,),
    ).fetchall()
    candidates = [row["addr"] for row in rows]
    current_core = [row["addr"] for row in rows if row["role"] == "core"]
    state = db.execute(
        "SELECT value FROM auto_tune_state WHERE key='effective_portfolio_replay'"
    ).fetchone()
    replay = json.loads(state[0]) if state and state[0] else {}
    now_ms = _iso_ms(replay.get("replayedAt"))
    revision = db.execute(
        "SELECT sr.params_json FROM strategy_revision sr JOIN active_strategy_revision ar "
        "ON ar.revision=sr.revision WHERE ar.id=1"
    ).fetchone()
    follow = json.loads(revision[0]) if revision and revision[0] else params.load_follow(db)
    if "SMART_ADD" in follow:
        follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"

    print(
        f"offline lab: generation={generation} candidates={len(candidates)} core={len(current_core)}",
        file=sys.stderr, flush=True,
    )
    lab = ReplayLab(db, candidates, now_ms, follow)
    constraints = selection.SelectionConstraints(
        min_relative_lcb_improvement=0,
        min_actionable_open_rate=float(getattr(config, "MIN_ACTIONABLE_OPEN_RATE", .70)),
        min_capacity_fit=float(getattr(config, "MIN_CAPACITY_FIT", .85)),
        max_drawdown_worsening=1,
        max_deploy_pct=f(params.get(db, "MAX_DEPLOY_PCT", config.MAX_DEPLOY_PCT)),
        max_cost_drag_ratio=1,
        max_targets=len(candidates),
        max_actionable_open_rate_drop=float(getattr(config, "CORE_SEARCH_MAX_OPEN_RATE_DROP", .05)),
        max_capacity_fit_drop=float(getattr(config, "CORE_SEARCH_MAX_CAPACITY_FIT_DROP", .05)),
    )
    search_cfg = offline_core_optimizer.OfflineSearchConfig(
        finalist_limit=max(1, args.finalists),
        strict_move_shortlist=8,
        max_strict_moves=max(1, args.strict_moves),
        pair_add_limit=4,
        time_budget_s=(0 if args.time_budget <= 0 else max(30, args.time_budget)),
    )
    result = offline_core_optimizer.optimize_membership(
        candidates, current_core, lab.fast(follow), lab.strict(follow), constraints, search_cfg,
    )
    current_tuple = tuple(sorted(current_core))
    # The 30-day winner is only a proposal.  Promote a state over the published
    # Core only when it also survives independent folds and cost stress.
    robust_states = set(result.finalists) | {result.selected, current_tuple}
    outside = [addr for addr in candidates if addr not in set(current_tuple)]
    robust_states.update(tuple(sorted((*current_tuple, addr))) for addr in outside)
    robust_states.update(
        tuple(sorted((*current_tuple, first, second)))
        for first, second in itertools.combinations(outside, 2)
    )
    current_metrics = lab.strict(follow)(current_tuple)
    ranked_robust = []
    for state in robust_states:
        metrics = lab.strict(follow)(state)
        if _metric_net(metrics) > _metric_net(current_metrics) and _metric_utility(metrics) > _metric_utility(current_metrics):
            ranked_robust.append((state, metrics))
    ranked_robust.sort(
        key=lambda item: (_metric_utility(item[1]), _metric_net(item[1])), reverse=True,
    )
    robust_limit = max(12, int(args.finalists))
    robust_passed = []
    robust_audit = []
    for state, metrics in ranked_robust[:robust_limit]:
        comparison = lab.robust_compare(current_tuple, state, follow, constraints)
        robust_audit.append({
            "walletCount": len(state),
            "added": [_mask(addr) for addr in sorted(set(state) - set(current_tuple))],
            "removed": [_mask(addr) for addr in sorted(set(current_tuple) - set(state))],
            "eligible": comparison.eligible,
            "reasons": list(comparison.reasons),
            "foldWins": comparison.fold_wins,
            "foldDeltas": [round(value, 2) for value in comparison.fold_deltas],
            "costStressGain": round(comparison.cost_stress_gain, 2),
            "net30": round(_metric_net(metrics), 2),
            "utility": round(_metric_utility(metrics), 2),
        })
        if comparison.eligible:
            robust_passed.append((state, metrics, comparison))
    if robust_passed:
        best_selected, best_metrics, robust_choice = max(
            robust_passed,
            key=lambda item: (_metric_utility(item[1]), _metric_net(item[1])),
        )
    else:
        best_selected, best_metrics, robust_choice = current_tuple, current_metrics, None
    best_surface, best_label = follow, "current"
    polish_rows = []
    polish_audit = []
    if not args.skip_param_polish and not result.timed_out:
        print("offline lab: local parameter polish", file=sys.stderr, flush=True)
        for label, surface in _nearest_axis_candidates(follow):
            metrics = lab.strict(surface)(best_selected)
            polish_rows.append((label, surface, metrics))
        feasible = [("current", follow, best_metrics)]
        for label, surface, metrics in polish_rows:
            if label == "current":
                continue
            validation = lab.parameter_validation(best_selected, follow, surface)
            continuous_better = (
                _metric_net(metrics) > _metric_net(best_metrics)
                and _metric_utility(metrics) > _metric_utility(best_metrics)
            )
            eligible = bool(validation.get("eligible") and continuous_better)
            polish_audit.append({
                "label": label,
                "eligible": eligible,
                "continuousBetter": continuous_better,
                "reasons": list(validation.get("reasons") or ()),
                "foldWins": int(validation.get("foldWins") or 0),
                "relativeGain": round(f(validation.get("relativeGain")), 5),
                "net30": round(_metric_net(metrics), 2),
                "utility": round(_metric_utility(metrics), 2),
            })
            if eligible:
                feasible.append((label, surface, metrics))
        if feasible:
            best_label, best_surface, best_metrics = max(feasible, key=_better)
            print(f"offline lab: best local surface={best_label}", file=sys.stderr, flush=True)
            if _param_hash(best_surface) != _param_hash(follow):
                final_selected, final_metrics, final_timeout = offline_core_optimizer.strict_local_closure(
                    best_selected, candidates, lab.fast(best_surface), lab.strict(best_surface),
                    constraints, search_cfg, phase="post_polish_closure",
                )
                if final_selected != best_selected:
                    final_comparison = lab.robust_compare(
                        best_selected, final_selected, best_surface, constraints,
                    )
                    if final_comparison.eligible:
                        best_selected, best_metrics = final_selected, final_metrics
                result.timed_out = result.timed_out or final_timeout

    final_detail = lab.detail(best_surface, best_selected)
    current_detail = lab.detail(follow, current_tuple)
    report = {
        "status": "complete" if not result.timed_out else "timed_out_no_publish",
        "readOnly": True,
        "generation": generation,
        "replayAt": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
        "candidateCount": len(candidates),
        "currentCore": [_mask(addr) for addr in current_tuple],
        "optimizedCore": [_mask(addr) for addr in best_selected],
        "added": [_mask(addr) for addr in sorted(set(best_selected) - set(current_tuple))],
        "removed": [_mask(addr) for addr in sorted(set(current_tuple) - set(best_selected))],
        "current": _compact_metrics(current_metrics, current_detail),
        "optimized": _compact_metrics(best_metrics, final_detail),
        "delta": {
            "net30": round(_metric_net(best_metrics) - _metric_net(current_metrics), 2),
            "riskAdjustedUtility": round(
                _metric_utility(best_metrics) - _metric_utility(current_metrics), 2
            ),
        },
        "parameterSurface": {
            "label": best_label,
            "changed": _param_hash(best_surface) != _param_hash(follow),
            "effective": {
                key: best_surface.get(key)
                for key in (*auto_tune.TUNE_KEYS, *auto_tune.ADD_TUNE_KEYS)
            },
        },
        "search": {
            "fastEvaluated": result.fast_evaluated,
            "strictEvaluatedBeforePolish": result.strict_evaluated,
            "strictEvaluatedTotal": len(lab.strict_cache),
            "pathRows": len(lab.path_rows),
            "finalists": len(result.finalists),
            "moves": [
                {
                    "phase": step.phase,
                    "action": step.action,
                    "beforeCount": len(step.before),
                    "afterCount": len(step.after),
                    "net": round(step.net, 2),
                    "utility": round(step.utility, 2),
                }
                for step in result.steps
            ],
            "inSampleWinner": {
                "wallets": [_mask(addr) for addr in result.selected],
                "net30": round(_metric_net(result.metrics), 2),
                "utility": round(_metric_utility(result.metrics), 2),
            },
            "robustChoice": ({
                "foldWins": robust_choice.fold_wins,
                "foldDeltas": [round(value, 2) for value in robust_choice.fold_deltas],
                "costStressGain": round(robust_choice.cost_stress_gain, 2),
            } if robust_choice else None),
            "robustAudit": robust_audit,
            "parameterAudit": polish_audit,
        },
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
