"""Non-publishing profitability research over a broad Perp wallet sample.

Leaderboard and official Portfolio are recall surfaces only.  The collector deliberately bypasses every
historical profitability, win-rate, sample-depth, activity and score gate.  It retains only executable-market
structure, catastrophic source risk and data-integrity checks, then replays every surviving wallet under the
currently active Copy parameters.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace

from hyper import config, params, storage
from hyper.copy.copy_backtest import prepare_price_path
from hyper.copy.economics import replay_result_profitability
from hyper.copy.fills import build_episodes
from hyper.copy.copy_data import normalize_copyable_fills
from hyper.copy.sector import SECTORS, classify_coin
from hyper.market import price_path, rest
from hyper.util import f

from . import metrics, perp_prefilter, scanner
from .scanner_copy_bt import copy_bt_results


DAY_MS = 86_400_000
MODEL_VERSION = "profit-distribution-structural-only-v1"
STRUCTURAL_GATES = (
    "perp_week_volume_250k",
    "second_scale_hft",
    "oid_bot_frequency",
    "systematic_grid_or_heavy_dca",
    "spot_hedge",
    "opaque_or_unexecutable_market",
    "extreme_concurrency",
    "source_zero_or_major_liquidation",
    "complete_data_and_path",
)
RETURN_30_CUTS = (0.10, 0.20, 0.30, 0.40, 0.50, 0.75, 1.00)
RETURN_7_CUTS = (0.00, 0.03, 0.05, 0.075, 0.10, 0.15, 0.20, 0.30)


def wallet_id(addr: str) -> str:
    return "wallet_" + hashlib.sha256(str(addr or "").lower().encode()).hexdigest()[:12]


def _atomic_json(path: str, payload: dict) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".profit-distribution-", suffix=".json", dir=target.parent,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _windows(payload) -> dict:
    if not isinstance(payload, list):
        return {}
    return {
        str(item[0]): item[1]
        for item in payload
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict)
    }


def _leaderboard_candidates(rows, minimum_week_volume: float) -> list[dict]:
    """Use volume only; PnL, ROI, account size and activity quality are audit fields."""
    out = []
    for row in rows or ():
        performances = {
            str(name): dict(values or {})
            for name, values in row.get("windowPerformances") or ()
        }
        week = performances.get("week") or {}
        month = performances.get("month") or {}
        addr = str(row.get("ethAddress") or "").lower()
        if not addr or f(week.get("vlm")) < float(minimum_week_volume):
            continue
        out.append({
            "addr": addr,
            "wallet": wallet_id(addr),
            "accountValue": f(row.get("accountValue")),
            "leaderboardWeekVolume": f(week.get("vlm")),
            "leaderboardWeekPnl": f(week.get("pnl")),
            "leaderboardWeekRoi": f(week.get("roi")),
            "leaderboardMonthPnl": f(month.get("pnl")),
            "leaderboardMonthRoi": f(month.get("roi")),
        })
    return sorted(out, key=lambda row: (-row["leaderboardWeekVolume"], row["addr"]))


def _stratified_sample(
    candidates: list[dict],
    limit: int,
    *,
    must_include: set[str] | None = None,
) -> list[dict]:
    """Deterministically cover the full volume rank while retaining current operational evidence."""
    if limit <= 0 or limit >= len(candidates):
        return list(candidates)
    required = {
        str(addr or "").lower() for addr in (must_include or ()) if addr
    }
    forced = [row for row in candidates if row["addr"] in required]
    forced = forced[:limit]
    remaining_slots = max(0, int(limit) - len(forced))
    pool = [row for row in candidates if row["addr"] not in required]
    if remaining_slots >= len(pool):
        sampled = pool
    elif remaining_slots <= 0:
        sampled = []
    elif remaining_slots == 1:
        sampled = [pool[len(pool) // 2]]
    else:
        indexes = {
            int(round(index * (len(pool) - 1) / (remaining_slots - 1)))
            for index in range(remaining_slots)
        }
        sampled = [pool[index] for index in sorted(indexes)]
    selected = {row["addr"]: row for row in (*forced, *sampled)}
    return [
        row for row in candidates if row["addr"] in selected
    ][:limit]


def _active_surface(db: sqlite3.Connection) -> dict:
    try:
        row = db.execute(
            "SELECT sr.params_json FROM strategy_revision sr "
            "JOIN active_strategy_revision ar ON ar.revision=sr.revision WHERE ar.id=1"
        ).fetchone()
        follow = json.loads(row[0]) if row and row[0] else params.load_follow(db)
    except (sqlite3.Error, TypeError, ValueError):
        follow = params.load_follow(db)
    follow.update(params.load_category(db, "scanner"))
    if "SMART_ADD" in follow:
        follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
    return follow


def _market_evidence(db: sqlite3.Connection) -> tuple[dict, dict]:
    try:
        available = {
            str(row[1]) for row in db.execute("PRAGMA table_info(coin_vol)").fetchall()
        }
    except sqlite3.Error:
        return {}, {}
    if "coin" not in available:
        return {}, {}
    requested = ("coin", "sigma", "day_ntl_vlm", "oi_notional", "max_leverage", "mark_px")
    columns = ",".join(
        name if name in available else f"NULL AS {name}" for name in requested
    )
    sigmas, context = {}, {}
    for row in db.execute(f"SELECT {columns} FROM coin_vol").fetchall():
        coin = str(row[0])
        if row[1] is not None:
            sigmas[coin] = f(row[1])
        context[coin] = {
            "day_ntl_vlm": row[2],
            "oi_notional": row[3],
            "max_leverage": row[4],
        }
        if f(row[5]) > 0.0:
            context[coin]["mark_px"] = f(row[5])
    return sigmas, context


def _known_major_risk(db: sqlite3.Connection) -> set[str]:
    try:
        rows = db.execute(
            "SELECT DISTINCT lower(addr) FROM wallet_risk_event "
            "WHERE event_type IN ('copy_single_liquidation_loss_over_5pct',"
            "'source_account_liquidated_zero')"
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[0]).lower() for row in rows if row and row[0]}


def _current_selection_addrs(db: sqlite3.Connection) -> set[str]:
    try:
        rows = db.execute(
            "SELECT DISTINCT lower(fs.addr) FROM follow_selection fs "
            "JOIN scan_generation sg ON sg.generation=fs.generation "
            "WHERE sg.is_current=1 AND sg.status='published' "
            "AND fs.role IN ('core','challenger')"
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[0]).lower() for row in rows if row and row[0]}


def _research_namespace(surface: dict, universe: set[str]) -> SimpleNamespace:
    return SimpleNamespace(
        days=30,
        min_perp=0.60,
        max_daily_eps=float(surface.get("max_daily_eps", 30.0)),
        exclude_hft=bool(surface.get("EXCLUDE_HFT", True)),
        hft_min_hold_min=float(surface.get("HFT_MIN_HOLD_MIN", 3.0)),
        max_single_adds=int(surface.get(
            "max_single_adds", config.MAX_SINGLE_ADDS_PER_EP,
        )),
        max_fills_per_ep=int(surface.get("max_fills_per_ep", 50)),
        max_orders_per_ep=int(surface.get("max_fills_per_ep", 50)),
        max_concurrent_pos=int(surface.get(
            "MAX_CONCURRENT_POS", config.MAX_CONCURRENT_POS,
        )),
        copy_bt_days=int(config.COPY_BT_DAYS),
        copy_bt_min_closed=0,
        copy_bt_overrides=dict(surface),
        copyable_universe=frozenset(universe),
        copy_bt_price_path=None,
        copy_bt_price_path_meta={},
        scan_generation=None,
    )


def _first_structure_failure(structure: dict) -> str:
    reasons = [
        str((structure.get(sector) or {}).get("status") or "")
        for sector in SECTORS
        if (structure.get(sector) or {}).get("status")
        not in {"structural_ok", "no_sector_evidence"}
    ]
    return reasons[0] if reasons else "no_copyable_sector"


def _copy_windows(results: dict) -> dict:
    out = {}
    for days in (30, 14, 7):
        result = dict(results.get(days) or {})
        economics = replay_result_profitability(result)
        out[str(days)] = {
            "valid": result.get("valid") is not False,
            "dataStatus": result.get("data_status"),
            "evidenceStatus": result.get("evidence_status"),
            "valuationStatus": result.get("valuation_status"),
            "closedPnl": economics.get("closedPnl"),
            "openProfitReference": economics.get("openProfitReference"),
            "openLoss": economics.get("openLoss"),
            "qualificationPnl": economics.get("qualificationPnl"),
            "qualificationReturn": economics.get("qualificationReturn"),
            "openLossRatio": economics.get("openLossRatio"),
            "windowStartEquity": economics.get("windowStartEquity"),
            "closedEpisodes": int(result.get("closed_n") or 0),
            "wins": int(result.get("wins") or 0),
            "liquidations": int(result.get("liquidations") or 0),
            "maxLiquidationLossPct": result.get("max_liquidation_loss_pct"),
            "actionableOpenRate": result.get("actionable_open_rate"),
            "pathCompletionRate": result.get("path_completion_rate"),
            "feeDrag": result.get("fee_drag"),
        }
    return out


def _rough_wallet(
    candidate: dict,
    *,
    now_ms: int,
    minimum_week_volume: float,
    universe: set[str],
    namespace: SimpleNamespace,
    sigmas: dict,
    market_context: dict,
    terminal_marks: dict,
    known_risk: set[str],
    max_pages: int,
) -> tuple[dict, dict | None]:
    addr = candidate["addr"]
    base = {
        key: value for key, value in candidate.items() if key != "addr"
    }
    payload = rest.portfolio(addr)
    portfolio = _windows(payload)
    perp_week = portfolio.get("perpWeek")
    if not isinstance(perp_week, dict) or perp_week.get("vlm") is None:
        return {**base, "status": "deferred", "reason": "portfolio_perp_week_incomplete"}, None
    perp_week_volume = f(perp_week.get("vlm"))
    base["officialPerpWeekVolume"] = perp_week_volume
    if perp_week_volume < float(minimum_week_volume):
        return {**base, "status": "rejected", "reason": "perp_week_volume_below_floor"}, None
    official_return = perp_prefilter.official_perp_month_return(
        portfolio.get("perpMonth"),
        min_return_30d=-math.inf,
        min_return_7d=-math.inf,
    )
    base["officialPerpReturnAudit"] = {
        key: official_return.get(key)
        for key in (
            "evidenceSufficient", "reason", "historyTier", "windowDays",
            "fundingResetCount", "netPnl", "return",
        )
    }
    if addr in known_risk:
        return {**base, "status": "rejected", "reason": "historical_major_liquidation"}, None

    raw_fills, hit_cap = rest.fetch_window(
        addr, int(now_ms) - int(config.PROFILE_FETCH_DAYS) * DAY_MS, max_pages,
    )
    if not isinstance(raw_fills, list):
        return {**base, "status": "deferred", "reason": "fill_history_unavailable"}, None
    scoped = normalize_copyable_fills(raw_fills, addr=addr, universe=universe)
    base["rawFillCount"] = len(raw_fills)
    base["copyableFillCount"] = len(scoped)
    if not scoped:
        return {**base, "status": "rejected", "reason": "opaque_or_unexecutable_market"}, None

    structure = scanner._current_sector_structure_policy(
        scoped, now_ms, namespace, source=MODEL_VERSION,
    )
    base["structure"] = {
        "allowed": list(structure.get("allowed") or ()),
        **{
            sector: {
                key: (structure.get(sector) or {}).get(key)
                for key in (
                    "status", "maxAdds", "medianAdds", "maxConcurrent",
                    "rawClosed", "rawPayoffRatio",
                )
            }
            for sector in SECTORS
        },
    }
    if not structure.get("allowed"):
        sector_statuses = {
            str((structure.get(sector) or {}).get("status") or "")
            for sector in SECTORS
        }
        if sector_statuses <= {"", "no_sector_evidence"}:
            return {
                **base, "status": "deferred",
                "reason": "complete_sector_structure_unavailable",
            }, None
        return {
            **base, "status": "rejected",
            "reason": _first_structure_failure(structure),
        }, None
    if hit_cap:
        # A partial page may prove structural uncopyability, but it can never prove profitability. Reaching
        # this branch means at least one sector looked copyable, so retain it as incomplete instead of
        # calculating a flattering return from the truncated path.
        return {**base, "status": "deferred", "reason": "fill_history_page_cap"}, None

    allowed = set(structure["allowed"])
    replay_fills = [
        fill for fill in scoped if classify_coin(fill.get("coin")) in allowed
    ]
    episodes, open_episodes = build_episodes(replay_fills)
    source = metrics.source_episode_quality(episodes, now_ms)
    computed = metrics.compute_metrics(replay_fills, episodes, now_ms, 30) or {}
    dexes = {
        fill["coin"].split(":", 1)[0] if ":" in fill["coin"] else None
        for fill in replay_fills
    } or {None}
    snapshot = scanner._open_snapshot(
        addr, dexes, open_episodes, now_ms, candidate.get("accountValue"), universe=universe,
    )
    if snapshot is None:
        return {**base, "status": "deferred", "reason": "clearinghouse_unavailable"}, None
    if f(snapshot.get("hedge_ratio")) > float(config.HEDGE_MAX_FRAC):
        return {**base, "status": "rejected", "reason": "spot_hedge"}, None

    liquidation_count, worst_liquidation_pct = scanner._self_liquidations(
        scoped, addr, candidate.get("accountValue"),
    )
    if liquidation_count and f(snapshot.get("account_value")) <= max(config.FLAT, 1e-6):
        return {**base, "status": "rejected", "reason": "source_account_liquidated_zero"}, None
    major_loss_limit = float(config.CORE_COPY_MAX_SINGLE_LIQUIDATION_LOSS_PCT)
    if liquidation_count and abs(f(worst_liquidation_pct)) / 100.0 > major_loss_limit:
        return {**base, "status": "rejected", "reason": "source_major_liquidation"}, None

    marks = {**terminal_marks, **dict(snapshot.get("mark_prices") or {})}
    results = copy_bt_results(
        addr, replay_fills, now_ms, namespace,
        valuation_marks=marks, sigmas=sigmas, market_ctx=market_context,
    )
    windows = _copy_windows(results)
    invalid = any(not windows[str(days)]["valid"] for days in (30, 14, 7))
    valuation_incomplete = any(
        windows[str(days)]["valuationStatus"] not in {None, "complete"}
        for days in (30, 14, 7)
    )
    status = "deferred" if invalid or valuation_incomplete else "rough_complete"
    reason = (
        "copy_replay_invalid" if invalid
        else "copy_valuation_incomplete" if valuation_incomplete
        else "structural_sample_collected"
    )
    record = {
        **base,
        "status": status,
        "reason": reason,
        "source": {
            **{key: source.get(key) for key in (
                "source_episode_n_30d", "source_episode_n_7d",
                "source_win_rate_30d", "source_win_rate_7d",
                "source_net_pnl_30d", "source_net_pnl_7d",
                "source_top3_profit_share", "source_body_after_top3_net_pnl",
            )},
            "medianHoldSeconds": computed.get("median_hold_s"),
            "medianEpisodesPerActiveDay": computed.get("median_eps"),
            "takerNotionalFraction": computed.get("taker_frac_notl"),
            "selfLiquidations": liquidation_count,
            "worstSelfLiquidationPct": worst_liquidation_pct,
        },
        "current": {
            "accountValue": snapshot.get("account_value"),
            "openUnrealizedPnl": snapshot.get("open_unrealized"),
            "openLossFraction": snapshot.get("open_loss_frac"),
            "openWinFraction": snapshot.get("open_win_frac"),
            "openPositions": snapshot.get("open_position_count"),
            "spotHedgeRatio": snapshot.get("hedge_ratio"),
        },
        "rough": {"mode": "fills_only_current_surface", "windows": windows},
    }
    return record, {
        "addr": addr,
        "wallet": candidate["wallet"],
        "fills": replay_fills,
        "marks": marks,
    }


def _quantile(values: list[float], q: float) -> float | None:
    rows = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    index = (len(rows) - 1) * float(q)
    lo, hi = int(math.floor(index)), int(math.ceil(index))
    if lo == hi:
        return rows[lo]
    return rows[lo] + (rows[hi] - rows[lo]) * (index - lo)


def summarize(wallets: list[dict]) -> dict:
    strict = [
        row for row in wallets
        if row.get("status") == "strict_complete" and isinstance(row.get("strict"), dict)
    ]
    returns30 = [
        f(row["strict"]["windows"]["30"].get("qualificationReturn")) for row in strict
    ]
    returns7 = [
        f(row["strict"]["windows"]["7"].get("qualificationReturn")) for row in strict
    ]
    matrix = []
    for floor30 in RETURN_30_CUTS:
        for floor7 in RETURN_7_CUTS:
            passed = sum(
                r30 >= floor30 and r7 >= floor7
                for r30, r7 in zip(returns30, returns7)
            )
            matrix.append({
                "return30Floor": floor30,
                "return7Floor": floor7,
                "passed": passed,
                "passRate": passed / len(strict) if strict else None,
            })
    expected_recent = []
    for r30, r7 in zip(returns30, returns7):
        if r30 <= -1.0:
            continue
        expected7 = (1.0 + r30) ** (7.0 / 30.0) - 1.0
        if expected7 > 0.0:
            expected_recent.append(r7 / expected7)
    statuses = Counter(str(row.get("status") or "unknown") for row in wallets)
    reasons = Counter(str(row.get("reason") or "unknown") for row in wallets)
    return {
        "statusCounts": dict(sorted(statuses.items())),
        "reasonCounts": dict(reasons.most_common()),
        "strictSampleCount": len(strict),
        "strictReturnQuantiles": {
            "30d": {
                str(q): _quantile(returns30, q)
                for q in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
            },
            "7d": {
                str(q): _quantile(returns7, q)
                for q in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
            },
            "recentPaceVs30d": {
                str(q): _quantile(expected_recent, q)
                for q in (0.10, 0.25, 0.50, 0.75, 0.90)
            },
        },
        "thresholdMatrix": matrix,
    }


def run(
    db_path: str,
    report_path: str,
    cache_db_path: str,
    *,
    minimum_week_volume: float = 250_000.0,
    max_pages: int = 5,
    limit: int = 0,
    scan_interval: float = 1.1,
    progress=None,
) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    now_ms = int(time.time() * 1000)
    source = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    source.execute("PRAGMA query_only=ON")
    try:
        surface = _active_surface(source)
        sigmas, market_context = _market_evidence(source)
        known_risk = _known_major_risk(source)
        current_selection = _current_selection_addrs(source)
    finally:
        source.close()

    config.MIN_POST_INTERVAL = max(0.1, float(scan_interval))
    rest.reset_request_stats()
    leaderboard = rest.get_leaderboard()
    recalled = _leaderboard_candidates(leaderboard, minimum_week_volume)
    candidates = _stratified_sample(
        recalled, int(limit), must_include=current_selection,
    )
    universe = rest.copyable_universe(force=True)
    namespace = _research_namespace(surface, universe)
    terminal_marks = scanner._current_copy_valuation_marks()
    wallets, replay_inputs = [], []
    total = len(candidates)
    for index, candidate in enumerate(candidates, 1):
        try:
            record, replay_input = _rough_wallet(
                candidate,
                now_ms=now_ms,
                minimum_week_volume=minimum_week_volume,
                universe=universe,
                namespace=namespace,
                sigmas=sigmas,
                market_context=market_context,
                terminal_marks=terminal_marks,
                known_risk=known_risk,
                max_pages=max_pages,
            )
        except Exception as exc:  # one wallet must not erase the broad research sample
            record, replay_input = ({
                **{key: value for key, value in candidate.items() if key != "addr"},
                "status": "deferred",
                "reason": f"collector_error:{type(exc).__name__}",
            }, None)
        wallets.append(record)
        if replay_input is not None:
            replay_inputs.append(replay_input)
        if progress:
            progress("rough", index, total)
        if index % 10 == 0 or index == total:
            _atomic_json(report_path, {
                "status": "collecting",
                "modelVersion": MODEL_VERSION,
                "startedAt": started_at,
                "minimumPerpWeekVolume": minimum_week_volume,
                "leaderboardRows": len(leaderboard),
                "leaderboardVolumeRecall": len(recalled),
                "sampledCandidates": len(candidates),
                "processed": index,
                "wallets": wallets,
                "requestStats": rest.request_stats(),
            })

    Path(cache_db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
    cache = storage.connect(
        cache_db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
    )
    os.chmod(Path(cache_db_path).resolve(), 0o600)
    path_start = now_ms - int(config.PROFILE_FETCH_DAYS) * DAY_MS
    all_fills = [
        fill for item in replay_inputs for fill in item["fills"]
    ]
    path_audit = price_path.ensure(
        cache, all_fills, path_start, now_ms, interval=price_path.BASE_INTERVAL,
    )
    leverage = price_path.refresh_margin_metadata(cache, all_fills)
    for coin, maximum in leverage.items():
        market_context.setdefault(coin, {})["max_leverage"] = maximum

    by_wallet = {str(row.get("wallet")): row for row in wallets}
    for index, item in enumerate(replay_inputs, 1):
        row = by_wallet[item["wallet"]]
        coverage = price_path.coverage(
            cache, item["fills"], path_start, now_ms,
            interval=price_path.BASE_INTERVAL,
        )
        if f(coverage.get("coverage")) < float(config.CORE_PRICE_PATH_MIN_COVERAGE):
            row["status"] = "deferred"
            row["reason"] = "strict_price_path_incomplete"
            row["strictPath"] = coverage
        else:
            namespace.copy_bt_price_path = prepare_price_path(
                price_path.load_refined(cache, item["fills"], path_start, now_ms),
            )
            namespace.copy_bt_price_path_meta = coverage
            results = copy_bt_results(
                item["addr"], item["fills"], now_ms, namespace,
                valuation_marks=item["marks"], sigmas=sigmas,
                market_ctx=market_context,
            )
            windows = _copy_windows(results)
            invalid = any(not windows[str(days)]["valid"] for days in (30, 14, 7))
            valuation_incomplete = any(
                windows[str(days)]["valuationStatus"] not in {None, "complete"}
                for days in (30, 14, 7)
            )
            row["strict"] = {
                "mode": "current_surface_15m_path",
                "pathCoverage": coverage,
                "windows": windows,
            }
            if invalid or valuation_incomplete:
                row["status"] = "deferred"
                row["reason"] = (
                    "strict_replay_invalid" if invalid
                    else "strict_valuation_incomplete"
                )
            else:
                row["status"] = "strict_complete"
                row["reason"] = "structural_sample_collected"
        if progress:
            progress("strict", index, len(replay_inputs))

    cache.close()
    summary = summarize(wallets)
    report = {
        "status": "complete",
        "readOnlySource": True,
        "publishesGeneration": False,
        "changesStrategyRevision": False,
        "modelVersion": MODEL_VERSION,
        "startedAt": started_at,
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "minimumPerpWeekVolume": minimum_week_volume,
        "qualityGatesApplied": False,
        "structuralGates": list(STRUCTURAL_GATES),
        "leaderboardRows": len(leaderboard),
        "leaderboardVolumeRecall": len(recalled),
        "sampledCandidates": len(candidates),
        "pathAudit": path_audit,
        "requestStats": rest.request_stats(),
        "summary": summary,
        "wallets": wallets,
    }
    _atomic_json(report_path, report)
    return report
