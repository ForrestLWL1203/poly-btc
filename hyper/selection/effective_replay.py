"""Authoritative replay of the active immutable strategy revision.

Formation may inspect watch-only sectors while deciding whether a wallet can qualify.  Operator-facing
portfolio estimates must instead describe what Paper/Live can actually open: the active revision's Core,
parameters and frozen ``allowed`` sector policies.
"""
from __future__ import annotations

from hyper import config
from hyper.copy.copy_backtest import prepare_price_path, prepare_replay_fills
from hyper.copy.copy_data import load_copyable_fills
from hyper.copy.copy_policy import load_copy_policy
from hyper.copy.economics import (
    PROFITABILITY_BASIS,
    open_loss_ratio_within_limit,
    replay_result_profitability,
)
from hyper.market import price_path
from hyper.ops import resource_guard
from hyper.util import f

from . import auto_tune, state as selection, strategy_revision


DAY_MS = 86_400_000


def active_execution_context(db, generation: str) -> tuple[dict, list[str], dict]:
    bundle = strategy_revision.load_active(db)
    if not bundle or bundle.get("selectionGeneration") != generation:
        raise RuntimeError("effective_replay_active_revision_generation_mismatch")
    published = {
        str(addr or "").lower()
        for addr in (selection.published_core_addrs(db) or ()) if addr
    }
    targets = [
        row for row in (bundle.get("targets") or ())
        if str(row.get("addr") or "").lower() in published
    ]
    addrs = [str(row.get("addr") or "").lower() for row in targets]
    if set(addrs) != published or len(addrs) != len(published):
        raise RuntimeError("effective_replay_revision_targets_mismatch")
    policies = {
        str(row.get("addr") or "").lower(): dict(row.get("sectorPolicy") or {})
        for row in targets
    }
    if any(not (policies.get(addr) or {}).get("allowed") for addr in addrs):
        raise RuntimeError("effective_replay_allowed_policy_missing")
    follow = dict(bundle.get("params") or {})
    if "SMART_ADD" in follow:
        follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
    return follow, addrs, policies


def _generation_asof_ms(db, generation: str) -> int:
    row = db.execute(
        "SELECT COALESCE(gmm.asof_ms,CAST(strftime('%s',sg.published_at) AS INTEGER)*1000,"
        "CAST(strftime('%s',sg.started_at) AS INTEGER)*1000) "
        "FROM scan_generation sg LEFT JOIN generation_market_manifest gmm "
        "ON gmm.generation=sg.generation WHERE sg.generation=? "
        "AND sg.status='published' AND sg.complete=1 AND sg.is_current=1",
        (generation,),
    ).fetchone()
    now_ms = int((row[0] if row else 0) or 0)
    if now_ms <= 0:
        raise RuntimeError("effective_replay_generation_asof_missing")
    return now_ms


def _strict_payload(windows: dict, selected_count: int, follow: dict | None = None) -> dict:
    primary = windows.get(30) or windows.get(max(windows)) or {}
    recent = windows.get(7) or {}
    primary_economics = replay_result_profitability(primary)
    recent_economics = replay_result_profitability(recent)
    start_30 = f(primary.get("window_start_equity") or primary.get("initial_margin_equity"))
    start_7 = f(recent.get("window_start_equity") or recent.get("initial_margin_equity"))
    return_30 = f(primary_economics.get("qualificationPnl")) / start_30 if start_30 else float("-inf")
    return_7 = f(recent_economics.get("qualificationPnl")) / start_7 if start_7 else float("-inf")
    policy = load_copy_policy(follow)
    open_rate = f(
        primary.get("actionable_open_rate")
        if primary.get("actionable_open_rate") is not None
        else primary.get("effective_open_follow_rate")
    )
    capacity = f(primary.get("execution_capacity_fit"))
    failures = []
    if f(primary_economics.get("qualificationPnl")) <= 0.0:
        failures.append("net_not_positive")
    if f(recent_economics.get("qualificationPnl")) <= 0.0:
        failures.append("recent_net_not_positive")
    if return_30 < policy.portfolio_min_return_30d:
        failures.append("dynamic_return_30d")
    if return_7 < policy.portfolio_min_return_7d:
        failures.append("dynamic_return_7d")
    if not open_loss_ratio_within_limit(primary_economics):
        failures.append("open_loss_over_50pct")
    if open_rate < policy.min_actionable_open_rate:
        failures.append("open_follow_rate")
    if f(primary.get("price_path_coverage")) < float(config.CORE_PRICE_PATH_MIN_COVERAGE):
        failures.append("path_coverage")
    if f(primary.get("maintenance_margin_coverage")) < float(
        config.CORE_MAINTENANCE_META_MIN_COVERAGE
    ):
        failures.append("maintenance_coverage")

    account = {
        "initialEquity": float(config.INITIAL_BALANCE),
        "netPnl30d": f(primary_economics.get("qualificationPnl")),
        "markedNetPnl30d": f(primary.get("copy_net_pnl")),
        "closedNetPnl30d": f(primary_economics.get("closedPnl")),
        "openProfitReference30d": f(primary_economics.get("openProfitReference")),
        "openLoss30d": f(primary_economics.get("openLoss")),
        "openLossRatio30d": primary_economics.get("openLossRatio"),
        "startEquity30d": start_30,
        "endEquity30d": f(primary.get("window_end_equity")),
        "dynamicReturn30d": return_30,
        "netPnl7d": f(recent_economics.get("qualificationPnl")),
        "markedNetPnl7d": f(recent.get("copy_net_pnl")),
        "closedNetPnl7d": f(recent_economics.get("closedPnl")),
        "openLoss7d": f(recent_economics.get("openLoss")),
        "startEquity7d": start_7,
        "endEquity7d": f(recent.get("window_end_equity")),
        "dynamicReturn7d": return_7,
    }
    return {
        "status": "failed" if failures else "passed",
        "selectedCount": int(selected_count),
        "profitabilityBasis": PROFITABILITY_BASIS,
        "validationSource": "active_revision_allowed_strict",
        "netPnl30d": account["netPnl30d"],
        "markedNetPnl30d": account["markedNetPnl30d"],
        "closedNetPnl30d": account["closedNetPnl30d"],
        "openProfitReference30d": account["openProfitReference30d"],
        "openLoss30d": account["openLoss30d"],
        "openLossRatio30d": account["openLossRatio30d"],
        "startEquity30d": start_30,
        "endEquity30d": account["endEquity30d"],
        "dynamicReturn30d": return_30,
        "netPnl7d": account["netPnl7d"],
        "markedNetPnl7d": account["markedNetPnl7d"],
        "closedNetPnl7d": account["closedNetPnl7d"],
        "openLoss7d": account["openLoss7d"],
        "startEquity7d": start_7,
        "endEquity7d": account["endEquity7d"],
        "dynamicReturn7d": return_7,
        "standardizedAccount": dict(account),
        "paperAccount": {"basis": "standardized_projection", **account},
        "maxDrawdown30d": f(primary.get("max_drawdown")),
        "liquidations30d": int(primary.get("liquidations") or 0),
        "actionableOpenRate30d": open_rate,
        "paperActionableOpenRate30d": open_rate,
        "capacityFit30d": capacity,
        "executionCapacityFit30d": capacity,
        "cashCongestionFit30d": f(
            primary.get("cash_congestion_fit")
            if primary.get("cash_congestion_fit") is not None else 1.0
        ),
        "openConstraintCounts30d": dict(primary.get("open_constraint_counts") or {}),
        "tierEconomics": dict(primary.get("tier_economics") or {}),
        "deploymentUtilization": auto_tune.deployment_utilization_summary(primary),
        "pricePathCoverage30d": f(primary.get("price_path_coverage")),
        "maintenanceMarginCoverage30d": f(primary.get("maintenance_margin_coverage")),
        "failures": failures,
    }


def certify(db, generation: str | None = None) -> dict:
    generation = generation or selection.latest_published_generation(db)
    if not generation:
        raise RuntimeError("effective_replay_requires_published_generation")
    follow, addrs, policies = active_execution_context(db, generation)
    if not addrs:
        return {"status": "passed", "selectedCount": 0, "validationSource": "active_revision_allowed_strict"}
    now_ms = _generation_asof_ms(db, generation)
    days = auto_tune._tune_days()
    max_days = max(days)
    warmup_days = int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
    start_ms = now_ms - (max_days + warmup_days) * DAY_MS
    raw_fill_bytes = auto_tune._portfolio_fill_json_bytes(db, addrs, start_ms)
    max_bytes = int(getattr(config, "AUTO_TUNE_FILL_CACHE_MAX_BYTES", 64 * 1024 * 1024) or 0)
    if max_bytes > 0 and raw_fill_bytes > max_bytes:
        raise resource_guard.ResourceDeferred({
            "reasons": ["effective_replay_fills_over_budget"],
            "rawFillBytes": raw_fill_bytes,
            "maxFillBytes": max_bytes,
        })
    resource_guard.require_replay_budget(raw_fill_bytes)
    fills = prepare_replay_fills(load_copyable_fills(
        db, addrs, start_ms, policies=policies, policy_default=False,
    ))
    sigmas = auto_tune._load_sigmas(db, generation)
    market_ctx = auto_tune._load_market_ctx(db, generation)
    path_rows = prepare_price_path(price_path.load_refined(db, fills, start_ms, now_ms))
    path_meta = price_path.coverage(db, fills, start_ms, now_ms)
    windows = auto_tune._candidate_windows(
        db, addrs, sigmas, {**follow, "AMBIGUOUS_PATH_MODE": "liquidate"}, now_ms,
        window_fills={max_days: fills}, market_ctx=market_ctx,
        path_rows=path_rows, path_meta=path_meta,
        initial_balance=float(config.INITIAL_BALANCE), compact=True,
    )
    return _strict_payload(windows, len(addrs), follow)


def certify_and_store(db, generation: str | None = None) -> dict:
    generation = generation or selection.latest_published_generation(db)
    strict = certify(db, generation)
    if strict.get("status") != "passed":
        raise RuntimeError(
            "effective_replay_strict_failed:" + ",".join(strict.get("failures") or ("unknown",))
        )
    summary = auto_tune.store_certified_portfolio_replay(db, generation, strict)
    db.commit()
    return summary
