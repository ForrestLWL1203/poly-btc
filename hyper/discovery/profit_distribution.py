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
import zlib

from hyper import config, params, storage
from hyper.copy.copy_backtest import prepare_price_path, profit_structure_metrics
from hyper.copy.economics import replay_result_profitability
from hyper.copy.fills import build_episodes
from hyper.copy.copy_data import normalize_copyable_fills
from hyper.copy.sector import SECTORS, classify_coin
from hyper.market import price_path, rest
from hyper.util import f

from . import metrics, perp_prefilter, scanner
from .scanner_copy_bt import copy_bt_results


DAY_MS = 86_400_000
MODEL_VERSION = "profit-distribution-structural-activity-v2"
ACTIVITY_LOOKBACK_DAYS = 28
ACTIVITY_BUCKET_DAYS = 7
ACTIVITY_MIN_ACTIVE_WEEKS = 3
ACTIVITY_MAX_OPEN_GAP_DAYS = 10.0
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
RESEARCH_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS profit_research_run_cache (
    run_key TEXT PRIMARY KEY,
    model_version TEXT NOT NULL,
    report_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    context_blob BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profit_research_wallet_cache (
    run_key TEXT NOT NULL,
    wallet TEXT NOT NULL,
    addr TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    leaderboard_week_volume REAL,
    official_perp_week_volume REAL,
    raw_fill_count INTEGER,
    copyable_fill_count INTEGER,
    rough_return_30d REAL,
    rough_return_14d REAL,
    rough_return_7d REAL,
    source_episode_30d INTEGER,
    source_episode_7d INTEGER,
    source_win_rate_30d REAL,
    source_win_rate_7d REAL,
    active_weeks_4 INTEGER,
    actionable_open_28d INTEGER,
    actionable_open_7d INTEGER,
    max_open_gap_days REAL,
    operational_activity INTEGER,
    record_json TEXT NOT NULL,
    artifact_blob BLOB,
    replay_blob BLOB,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_key,wallet)
);
"""


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


def _research_cache(path: str) -> sqlite3.Connection:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    cache = sqlite3.connect(target, timeout=30)
    cache.execute("PRAGMA journal_mode=WAL")
    cache.execute("PRAGMA busy_timeout=30000")
    cache.executescript(RESEARCH_CACHE_SCHEMA)
    columns = {
        str(row[1])
        for row in cache.execute(
            "PRAGMA table_info(profit_research_wallet_cache)"
        ).fetchall()
    }
    additions = {
        "leaderboard_week_volume": "REAL",
        "official_perp_week_volume": "REAL",
        "raw_fill_count": "INTEGER",
        "copyable_fill_count": "INTEGER",
        "rough_return_30d": "REAL",
        "rough_return_14d": "REAL",
        "rough_return_7d": "REAL",
        "source_episode_30d": "INTEGER",
        "source_episode_7d": "INTEGER",
        "source_win_rate_30d": "REAL",
        "source_win_rate_7d": "REAL",
        "active_weeks_4": "INTEGER",
        "actionable_open_28d": "INTEGER",
        "actionable_open_7d": "INTEGER",
        "max_open_gap_days": "REAL",
        "operational_activity": "INTEGER",
        "artifact_blob": "BLOB",
    }
    for name, kind in additions.items():
        if name not in columns:
            cache.execute(
                f"ALTER TABLE profit_research_wallet_cache ADD COLUMN {name} {kind}"
            )
    cache.commit()
    os.chmod(target, 0o600)
    return cache


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=str)
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _compressed_json(value) -> bytes:
    return zlib.compress(
        json.dumps(
            _json_safe(value), sort_keys=True, separators=(",", ":"),
        ).encode(),
        level=6,
    )


def _research_run_key(report_path: str) -> str:
    return hashlib.sha256(str(Path(report_path).resolve()).encode()).hexdigest()[:16]


def _cache_rough_record(
    cache: sqlite3.Connection,
    run_key: str,
    candidate: dict,
    record: dict,
    replay_input: dict | None,
    artifact: dict | None,
) -> None:
    private_replay = None
    if replay_input is not None:
        private_replay = _compressed_json({
            "addr": replay_input.get("addr"),
            "wallet": replay_input.get("wallet"),
            "fills": replay_input.get("fills") or [],
            "marks": replay_input.get("marks") or {},
        })
    windows = ((record.get("rough") or {}).get("windows") or {})
    source = record.get("source") or {}
    activity = record.get("activity") or {}
    private_artifact = dict(artifact or {})
    if "allowedReplayFills" in private_artifact:
        private_artifact["allowedReplayFillsStoredIn"] = "replay_blob"
        private_artifact.pop("allowedReplayFills", None)
    cache.execute(
        "INSERT INTO profit_research_wallet_cache("
        "run_key,wallet,addr,status,reason,"
        "leaderboard_week_volume,official_perp_week_volume,raw_fill_count,copyable_fill_count,"
        "rough_return_30d,rough_return_14d,rough_return_7d,"
        "source_episode_30d,source_episode_7d,source_win_rate_30d,source_win_rate_7d,"
        "active_weeks_4,actionable_open_28d,actionable_open_7d,max_open_gap_days,"
        "operational_activity,record_json,artifact_blob,replay_blob,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(run_key,wallet) DO UPDATE SET "
        "addr=excluded.addr,status=excluded.status,reason=excluded.reason,"
        "leaderboard_week_volume=excluded.leaderboard_week_volume,"
        "official_perp_week_volume=excluded.official_perp_week_volume,"
        "raw_fill_count=excluded.raw_fill_count,copyable_fill_count=excluded.copyable_fill_count,"
        "rough_return_30d=excluded.rough_return_30d,"
        "rough_return_14d=excluded.rough_return_14d,"
        "rough_return_7d=excluded.rough_return_7d,"
        "source_episode_30d=excluded.source_episode_30d,"
        "source_episode_7d=excluded.source_episode_7d,"
        "source_win_rate_30d=excluded.source_win_rate_30d,"
        "source_win_rate_7d=excluded.source_win_rate_7d,"
        "active_weeks_4=excluded.active_weeks_4,"
        "actionable_open_28d=excluded.actionable_open_28d,"
        "actionable_open_7d=excluded.actionable_open_7d,"
        "max_open_gap_days=excluded.max_open_gap_days,"
        "operational_activity=excluded.operational_activity,"
        "record_json=excluded.record_json,artifact_blob=excluded.artifact_blob,"
        "replay_blob=excluded.replay_blob,"
        "updated_at=excluded.updated_at",
        (
            run_key,
            str(record.get("wallet") or candidate.get("wallet") or ""),
            str(candidate.get("addr") or "").lower(),
            str(record.get("status") or "unknown"),
            record.get("reason"),
            record.get("leaderboardWeekVolume"),
            record.get("officialPerpWeekVolume"),
            record.get("rawFillCount"),
            record.get("copyableFillCount"),
            (windows.get("30") or {}).get("qualificationReturn"),
            (windows.get("14") or {}).get("qualificationReturn"),
            (windows.get("7") or {}).get("qualificationReturn"),
            source.get("source_episode_n_30d"),
            source.get("source_episode_n_7d"),
            source.get("source_win_rate_30d"),
            source.get("source_win_rate_7d"),
            activity.get("activeWeeks4"),
            activity.get("actionableOpenEvents28d"),
            activity.get("actionableOpenEvents7d"),
            activity.get("maxOpenGapDays28d"),
            int(bool(activity.get("operational"))) if activity else None,
            json.dumps(
                _json_safe(record), sort_keys=True, separators=(",", ":"),
            ),
            _compressed_json(private_artifact) if artifact is not None else None,
            private_replay,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _load_cached_replay(
    cache: sqlite3.Connection,
    run_key: str,
    wallet: str,
) -> dict:
    row = cache.execute(
        "SELECT replay_blob FROM profit_research_wallet_cache "
        "WHERE run_key=? AND wallet=?",
        (run_key, wallet),
    ).fetchone()
    if not row or row[0] is None:
        raise ValueError("profit_research_replay_cache_missing")
    return json.loads(zlib.decompress(row[0]))


def _cache_run_context(
    cache: sqlite3.Connection,
    run_key: str,
    report_path: str,
    started_at: str,
    context: dict,
) -> None:
    cache.execute(
        "INSERT INTO profit_research_run_cache("
        "run_key,model_version,report_path,started_at,context_blob,updated_at"
        ") VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(run_key) DO UPDATE SET context_blob=excluded.context_blob,"
        "updated_at=excluded.updated_at",
        (
            run_key,
            MODEL_VERSION,
            str(Path(report_path).resolve()),
            started_at,
            _compressed_json(context),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


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
        closed_positions = list(result.get("positions") or ())
        closed_structure = profit_structure_metrics(
            closed_positions,
            total_net=sum(f(position.get("net_pnl")) for position in closed_positions),
        )
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
            "closedTop3ProfitShare": closed_structure.get("top3_profit_share"),
            "closedBodyAfterTop3N": closed_structure.get("body_after_top3_n"),
            "closedBodyAfterTop3WinRate": closed_structure.get("body_after_top3_win_rate"),
            "closedBodyAfterTop3Pnl": closed_structure.get("body_after_top3_net_pnl"),
            "closedPayoffRatio": closed_structure.get("payoff_ratio"),
            "closedProfitFactor": closed_structure.get("profit_factor"),
        }
    return out


def _copy_activity(results: dict, now_ms: int) -> dict:
    """Describe recurring source opportunities using the Copy open gate itself.

    ``Backtest.open_events`` already owns exactly one event per source flat->open/flip lifecycle.  A tiny
    first slice is updated in place when the same source position grows above the tier notional floor, so
    OID fragments never become fake extra opportunities.  Capacity misses still count as source activity;
    economically sub-floor and blacklisted opens do not.
    """
    primary = dict(results.get(30) or {})
    events = []
    raw_events = list(primary.get("open_events") or ())
    for event in raw_events:
        minimum = f(event.get("minimum_notional"))
        master = f(event.get("master_notional"))
        if minimum > 0.0 and master + 1e-9 < minimum:
            continue
        if str(event.get("outcome") or "") == "skip_coin_blacklist":
            continue
        stamp = int(f(event.get("time")))
        if stamp > 0:
            events.append(stamp)
    events.sort()

    bucket_ms = ACTIVITY_BUCKET_DAYS * DAY_MS
    lookback_ms = ACTIVITY_LOOKBACK_DAYS * DAY_MS
    start_ms = int(now_ms) - lookback_ms
    recent = [stamp for stamp in events if start_ms <= stamp <= int(now_ms)]
    buckets = [0, 0, 0, 0]
    for stamp in recent:
        index = min(3, max(0, int((stamp - start_ms) // bucket_ms)))
        buckets[index] += 1

    active_weeks = sum(count > 0 for count in buckets)
    latest_active = buckets[-1] > 0
    points = [start_ms, *recent, int(now_ms)]
    max_gap_days = max(
        (right - left) / DAY_MS for left, right in zip(points, points[1:])
    )
    gaps = [
        (right - left) / DAY_MS
        for left, right in zip(recent, recent[1:])
    ]
    median_gap_days = None
    if gaps:
        ordered = sorted(gaps)
        middle = len(ordered) // 2
        median_gap_days = (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2.0
        )

    continuous = active_weeks == len(buckets)
    operational = bool(
        latest_active
        and active_weeks >= ACTIVITY_MIN_ACTIVE_WEEKS
        and max_gap_days <= ACTIVITY_MAX_OPEN_GAP_DAYS
    )
    if not recent:
        reason = "no_actionable_open_28d"
    elif not latest_active:
        reason = "no_actionable_open_7d"
    elif active_weeks < ACTIVITY_MIN_ACTIVE_WEEKS:
        reason = "active_weeks_below_3_of_4"
    elif max_gap_days > ACTIVITY_MAX_OPEN_GAP_DAYS:
        reason = "actionable_open_gap_over_10d"
    else:
        reason = "operational_activity"

    return {
        "definition": "oid_deduped_copy_threshold_open_or_flip",
        "actionableOpenEvents30d": len(events),
        "actionableOpenEvents28d": len(recent),
        "actionableOpenEvents14d": sum(stamp >= int(now_ms) - 14 * DAY_MS for stamp in events),
        "actionableOpenEvents7d": sum(stamp >= int(now_ms) - 7 * DAY_MS for stamp in events),
        "activeOpenDays28d": len({stamp // DAY_MS for stamp in recent}),
        "weeklyOpenCountsOldestFirst": buckets,
        "activeWeeks4": active_weeks,
        "latest7dActive": latest_active,
        "continuous4of4": continuous,
        "maxOpenGapDays28d": max_gap_days,
        "medianOpenGapDays28d": median_gap_days,
        "operational": operational,
        "reason": reason,
    }


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
) -> tuple[dict, dict | None, dict]:
    addr = candidate["addr"]
    base = {
        key: value for key, value in candidate.items() if key != "addr"
    }
    artifact = {
        "candidate": dict(candidate),
        "portfolioPayload": None,
        "portfolioWindows": None,
        "rawFills": None,
        "copyableFills": None,
        "hitPageCap": None,
        "structurePolicy": None,
        "allowedReplayFills": None,
        "sourceEpisodeQuality": None,
        "computedMetrics": None,
        "openSnapshot": None,
        "selfLiquidations": None,
        "valuationMarks": None,
        "roughCopyResults": None,
    }

    def done(record: dict, replay_input: dict | None = None):
        artifact["record"] = record
        return record, replay_input, artifact

    payload = rest.portfolio(addr)
    artifact["portfolioPayload"] = payload
    portfolio = _windows(payload)
    artifact["portfolioWindows"] = portfolio
    perp_week = portfolio.get("perpWeek")
    if not isinstance(perp_week, dict) or perp_week.get("vlm") is None:
        return done({
            **base, "status": "deferred",
            "reason": "portfolio_perp_week_incomplete",
        })
    perp_week_volume = f(perp_week.get("vlm"))
    base["officialPerpWeekVolume"] = perp_week_volume
    if perp_week_volume < float(minimum_week_volume):
        return done({
            **base, "status": "rejected",
            "reason": "perp_week_volume_below_floor",
        })
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
        return done({
            **base, "status": "rejected",
            "reason": "historical_major_liquidation",
        })

    raw_fills, hit_cap = rest.fetch_window(
        addr, int(now_ms) - int(config.PROFILE_FETCH_DAYS) * DAY_MS, max_pages,
    )
    artifact["rawFills"] = raw_fills
    artifact["hitPageCap"] = bool(hit_cap)
    if not isinstance(raw_fills, list):
        return done({
            **base, "status": "deferred",
            "reason": "fill_history_unavailable",
        })
    scoped = normalize_copyable_fills(raw_fills, addr=addr, universe=universe)
    artifact["copyableFills"] = scoped
    base["rawFillCount"] = len(raw_fills)
    base["copyableFillCount"] = len(scoped)
    if not scoped:
        return done({
            **base, "status": "rejected",
            "reason": "opaque_or_unexecutable_market",
        })

    structure = scanner._current_sector_structure_policy(
        scoped, now_ms, namespace, source=MODEL_VERSION,
    )
    artifact["structurePolicy"] = structure
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
            return done({
                **base, "status": "deferred",
                "reason": "complete_sector_structure_unavailable",
            })
        return done({
            **base, "status": "rejected",
            "reason": _first_structure_failure(structure),
        })
    if hit_cap:
        # A partial page may prove structural uncopyability, but it can never prove profitability. Reaching
        # this branch means at least one sector looked copyable, so retain it as incomplete instead of
        # calculating a flattering return from the truncated path.
        return done({
            **base, "status": "deferred",
            "reason": "fill_history_page_cap",
        })

    allowed = set(structure["allowed"])
    replay_fills = [
        fill for fill in scoped if classify_coin(fill.get("coin")) in allowed
    ]
    artifact["allowedReplayFills"] = replay_fills
    episodes, open_episodes = build_episodes(replay_fills)
    source = metrics.source_episode_quality(episodes, now_ms)
    computed = metrics.compute_metrics(replay_fills, episodes, now_ms, 30) or {}
    artifact["sourceEpisodeQuality"] = source
    artifact["computedMetrics"] = computed
    dexes = {
        fill["coin"].split(":", 1)[0] if ":" in fill["coin"] else None
        for fill in replay_fills
    } or {None}
    snapshot = scanner._open_snapshot(
        addr, dexes, open_episodes, now_ms, candidate.get("accountValue"), universe=universe,
    )
    artifact["openSnapshot"] = snapshot
    if snapshot is None:
        return done({
            **base, "status": "deferred",
            "reason": "clearinghouse_unavailable",
        })
    if f(snapshot.get("hedge_ratio")) > float(config.HEDGE_MAX_FRAC):
        return done({
            **base, "status": "rejected", "reason": "spot_hedge",
        })

    liquidation_count, worst_liquidation_pct = scanner._self_liquidations(
        scoped, addr, candidate.get("accountValue"),
    )
    artifact["selfLiquidations"] = {
        "count": liquidation_count,
        "worstPct": worst_liquidation_pct,
    }
    if liquidation_count and f(snapshot.get("account_value")) <= max(config.FLAT, 1e-6):
        return done({
            **base, "status": "rejected",
            "reason": "source_account_liquidated_zero",
        })
    major_loss_limit = float(config.CORE_COPY_MAX_SINGLE_LIQUIDATION_LOSS_PCT)
    if liquidation_count and abs(f(worst_liquidation_pct)) / 100.0 > major_loss_limit:
        return done({
            **base, "status": "rejected",
            "reason": "source_major_liquidation",
        })

    marks = {**terminal_marks, **dict(snapshot.get("mark_prices") or {})}
    artifact["valuationMarks"] = marks
    results = copy_bt_results(
        addr, replay_fills, now_ms, namespace,
        valuation_marks=marks, sigmas=sigmas, market_ctx=market_context,
    )
    artifact["roughCopyResults"] = results
    windows = _copy_windows(results)
    activity = _copy_activity(results, now_ms)
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
                "source_top3_profit_share", "source_body_after_top3_n",
                "source_body_after_top3_win_rate", "source_body_after_top3_net_pnl",
            )},
            "medianHoldSeconds": computed.get("median_hold_s"),
            "medianEpisodesPerActiveDay": computed.get("median_eps"),
            "takerNotionalFraction": computed.get("taker_frac_notl"),
            "payoffRatio": computed.get("payoff_ratio"),
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
        "activity": activity,
        "strictEligibility": {
            "eligible": bool(activity["operational"]),
            "reason": (
                "operational_activity"
                if activity["operational"] else activity["reason"]
            ),
        },
    }
    replay_input = {
        "addr": addr,
        "wallet": candidate["wallet"],
        "fills": replay_fills,
        "marks": marks,
    }
    return done(record, replay_input)


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


def _rough_profit_sort_key(row: dict) -> tuple:
    windows = ((row.get("rough") or {}).get("windows") or {})
    return30 = f((windows.get("30") or {}).get("qualificationReturn"))
    return7 = f((windows.get("7") or {}).get("qualificationReturn"))
    priority = 0.70 * return30 + 0.30 * return7
    return (-priority, -return30, -return7, str(row.get("wallet") or ""))


def summarize(wallets: list[dict]) -> dict:
    rough = [
        row for row in wallets
        if isinstance(row.get("rough"), dict)
        and isinstance((row.get("rough") or {}).get("windows"), dict)
    ]
    operational = [
        row for row in rough
        if bool((row.get("activity") or {}).get("operational"))
    ]
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

    def rough_quantiles(rows):
        return {
            f"{days}d": {
                str(q): _quantile([
                    f(row["rough"]["windows"][str(days)].get("qualificationReturn"))
                    for row in rows
                ], q)
                for q in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)
            }
            for days in (30, 14, 7)
        }

    return {
        "statusCounts": dict(sorted(statuses.items())),
        "reasonCounts": dict(reasons.most_common()),
        "roughSampleCount": len(rough),
        "roughOperationalCount": len(operational),
        "roughContinuous4WeekCount": sum(
            bool((row.get("activity") or {}).get("continuous4of4"))
            for row in rough
        ),
        "roughReturnQuantiles": rough_quantiles(rough),
        "roughOperationalReturnQuantiles": rough_quantiles(operational),
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


def resume_rough(
    db_path: str,
    report_path: str,
    cache_db_path: str,
    resume_report_path: str,
    *,
    minimum_week_volume: float = 250_000.0,
    recovery_pages: int = 20,
    activity_audit_limit: int = 256,
    scan_interval: float = 1.1,
    progress=None,
) -> dict:
    """Finish an old rough checkpoint without repeating the entire Leaderboard universe.

    The prior report already contains complete returns for every wallet that fit inside its first history
    pass.  Re-fetch only the page-capped wallets plus a broad rough-profit prefix for the new activity audit,
    then merge those refreshed anonymous records into the original population.  This mode is deliberately
    rough-only: it cannot reconstruct strict replay inputs for untouched wallets and must never silently
    advance into strict.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    now_ms = int(time.time() * 1000)
    with open(Path(resume_report_path).resolve(), encoding="utf-8") as handle:
        prior = json.load(handle)
    wallets = [dict(row) for row in (prior.get("wallets") or ())]
    if not wallets or int(prior.get("processed") or 0) != len(wallets):
        raise ValueError("rough_resume_checkpoint_incomplete")

    source = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    source.execute("PRAGMA query_only=ON")
    try:
        surface = _active_surface(source)
        sigmas, market_context = _market_evidence(source)
        known_risk = _known_major_risk(source)
    finally:
        source.close()

    config.MIN_POST_INTERVAL = max(0.1, float(scan_interval))
    rest.reset_request_stats()
    leaderboard = rest.get_leaderboard()
    candidate_by_wallet = {
        row["wallet"]: row
        for row in _leaderboard_candidates(leaderboard, 0.0)
    }
    universe = rest.copyable_universe(force=True)
    namespace = _research_namespace(surface, universe)
    terminal_marks = scanner._current_copy_valuation_marks()
    research_cache = _research_cache(cache_db_path)
    run_key = _research_run_key(report_path)
    _cache_run_context(research_cache, run_key, report_path, started_at, {
        "mode": "resume_rough",
        "resumeReportPath": str(Path(resume_report_path).resolve()),
        "surface": surface,
        "sigmas": sigmas,
        "marketContext": market_context,
        "knownRiskAddresses": sorted(known_risk),
        "leaderboard": leaderboard,
        "copyableUniverse": sorted(universe),
        "minimumPerpWeekVolume": minimum_week_volume,
        "recoveryPages": recovery_pages,
        "activityAuditLimit": activity_audit_limit,
    })
    existing_wallets = {
        str(row[0]) for row in research_cache.execute(
            "SELECT wallet FROM profit_research_wallet_cache WHERE run_key=?",
            (run_key,),
        ).fetchall()
    }
    # The old anonymous checkpoint still contains a complete wallet-level population even though it did not
    # persist raw evidence. Seed those derived records into the new research table so distribution analysis
    # remains full-universe; refreshed audit/repair rows replace them with non-null artifact/replay blobs.
    for row in wallets:
        wallet = str(row.get("wallet") or "")
        if not wallet or wallet in existing_wallets:
            continue
        candidate = candidate_by_wallet.get(wallet) or {
            "wallet": wallet,
            "addr": "",
        }
        _cache_rough_record(
            research_cache, run_key, candidate, row, None, None,
        )
    research_cache.commit()

    capped_ids = {
        str(row.get("wallet")) for row in wallets
        if row.get("reason") == "fill_history_page_cap"
    }
    prior_rough = [
        row for row in wallets
        if row.get("status") == "rough_complete"
        and isinstance((row.get("rough") or {}).get("windows"), dict)
    ]
    prior_rough.sort(key=_rough_profit_sort_key)
    ranked_ids = {
        str(row.get("wallet"))
        for row in prior_rough[:max(0, int(activity_audit_limit))]
    }
    audit_ids = capped_ids | ranked_ids
    indexes = {
        str(row.get("wallet")): index for index, row in enumerate(wallets)
    }
    # Produce useful high-profit activity evidence first. Page-capped histories can take many API pages each;
    # putting them first made operators wait over an hour before learning whether the already-known return
    # leaders were recurring, actionable sources.
    ranked_order = [
        str(row.get("wallet"))
        for row in prior_rough[:max(0, int(activity_audit_limit))]
    ]
    capped_order = [
        str(row.get("wallet")) for row in wallets
        if str(row.get("wallet")) in capped_ids
        and str(row.get("wallet")) not in ranked_ids
    ]
    ordered_ids = ranked_order + capped_order
    cached_records = {
        str(wallet): json.loads(record_json)
        for wallet, record_json in research_cache.execute(
            "SELECT wallet,record_json FROM profit_research_wallet_cache "
            "WHERE run_key=? AND artifact_blob IS NOT NULL",
            (run_key,),
        ).fetchall()
        if str(wallet) in audit_ids
    }
    for wallet, cached_record in cached_records.items():
        wallets[indexes[wallet]] = cached_record
    pending_ids = [
        wallet for wallet in ordered_ids if wallet not in cached_records
    ]

    completed = len(cached_records)
    for index, wallet in enumerate(pending_ids, completed + 1):
        candidate = candidate_by_wallet.get(wallet)
        if candidate is None:
            if wallet in capped_ids:
                wallets[indexes[wallet]] = {
                    **wallets[indexes[wallet]],
                    "status": "deferred",
                    "reason": "rough_resume_wallet_unavailable",
                }
        else:
            try:
                record, _replay_input, artifact = _rough_wallet(
                    candidate,
                    now_ms=now_ms,
                    minimum_week_volume=minimum_week_volume,
                    universe=universe,
                    namespace=namespace,
                    sigmas=sigmas,
                    market_context=market_context,
                    terminal_marks=terminal_marks,
                    known_risk=known_risk,
                    max_pages=max(1, int(recovery_pages)),
                )
            except Exception as exc:
                record = {
                    **{
                        key: value for key, value in candidate.items()
                        if key != "addr"
                    },
                    "status": "deferred",
                    "reason": f"rough_resume_error:{type(exc).__name__}",
                }
                _replay_input = None
                artifact = {
                    "candidate": dict(candidate),
                    "collectorException": type(exc).__name__,
                }
            wallets[indexes[wallet]] = record
            _cache_rough_record(
                research_cache, run_key, candidate, record, _replay_input, artifact,
            )
        completed = index
        if progress:
            progress("rough_resume", index, len(ordered_ids))
        if index % 10 == 0 or index == len(ordered_ids):
            research_cache.commit()
            _atomic_json(report_path, {
                "status": "rough_resuming",
                "readOnlySource": True,
                "publishesGeneration": False,
                "changesStrategyRevision": False,
                "modelVersion": MODEL_VERSION,
                "startedAt": started_at,
                "resumeSourceModelVersion": prior.get("modelVersion"),
                "sampledCandidates": len(wallets),
                "historyRepairCandidates": len(capped_ids),
                "activityAuditRequested": len(ordered_ids),
                "activityAuditCompleted": completed,
                "wallets": wallets,
                "requestStats": rest.request_stats(),
            })

    final_rough = [
        row for row in wallets
        if row.get("status") == "rough_complete"
        and isinstance((row.get("rough") or {}).get("windows"), dict)
    ]
    final_rough.sort(key=_rough_profit_sort_key)
    final_ranked = final_rough[:max(0, int(activity_audit_limit))]
    operational = [
        row for row in final_ranked
        if bool((row.get("activity") or {}).get("operational"))
    ]
    report = {
        "status": "rough_complete",
        "readOnlySource": True,
        "publishesGeneration": False,
        "changesStrategyRevision": False,
        "modelVersion": MODEL_VERSION,
        "startedAt": started_at,
        "roughFinishedAt": datetime.now(timezone.utc).isoformat(),
        "resumeSourceModelVersion": prior.get("modelVersion"),
        "minimumPerpWeekVolume": minimum_week_volume,
        "qualityGatesApplied": False,
        "structuralGates": list(STRUCTURAL_GATES),
        "preStrictActivityPolicy": {
            "definition": "oid_deduped_copy_threshold_open_or_flip",
            "lookbackDays": ACTIVITY_LOOKBACK_DAYS,
            "bucketDays": ACTIVITY_BUCKET_DAYS,
            "minimumActiveWeeks": ACTIVITY_MIN_ACTIVE_WEEKS,
            "latest7dRequired": True,
            "maximumOpenGapDays": ACTIVITY_MAX_OPEN_GAP_DAYS,
        },
        "leaderboardRows": len(leaderboard),
        "sampledCandidates": len(wallets),
        "historyRepairCandidates": len(capped_ids),
        "activityAuditMode": "page_capped_plus_prior_rough_profit_prefix",
        "activityAuditLimit": max(0, int(activity_audit_limit)),
        "activityAuditCandidates": len(ordered_ids),
        "activityAuditCompleted": completed,
        "preStrictEligibleCandidates": len(operational),
        "strictReplayCandidates": 0,
        "strictRankingMode": "operational_activity_then_rough_conservative_profit_70_30",
        "requestStats": rest.request_stats(),
        "summary": summarize(wallets),
        "wallets": wallets,
    }
    research_cache.commit()
    research_cache.close()
    _atomic_json(report_path, report)
    return report


def run(
    db_path: str,
    report_path: str,
    cache_db_path: str,
    *,
    minimum_week_volume: float = 250_000.0,
    max_pages: int = 5,
    recovery_pages: int = 20,
    limit: int = 0,
    strict_limit: int = 0,
    rough_only: bool = False,
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
    research_cache = _research_cache(cache_db_path)
    run_key = _research_run_key(report_path)
    _cache_run_context(research_cache, run_key, report_path, started_at, {
        "mode": "full_rough_then_optional_strict",
        "surface": surface,
        "sigmas": sigmas,
        "marketContext": market_context,
        "knownRiskAddresses": sorted(known_risk),
        "currentSelectionAddresses": sorted(current_selection),
        "leaderboard": leaderboard,
        "copyableUniverse": sorted(universe),
        "minimumPerpWeekVolume": minimum_week_volume,
        "maxPages": max_pages,
        "recoveryPages": recovery_pages,
        "limit": limit,
        "strictLimit": strict_limit,
        "roughOnly": rough_only,
    })
    wallets, replay_wallets = [], set()
    total = len(candidates)
    for index, candidate in enumerate(candidates, 1):
        try:
            record, replay_input, artifact = _rough_wallet(
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
            record, replay_input, artifact = ({
                **{key: value for key, value in candidate.items() if key != "addr"},
                "status": "deferred",
                "reason": f"collector_error:{type(exc).__name__}",
            }, None, {
                "candidate": dict(candidate),
                "collectorException": type(exc).__name__,
            })
        wallets.append(record)
        if replay_input is not None:
            replay_wallets.add(str(record.get("wallet") or ""))
        _cache_rough_record(
            research_cache, run_key, candidate, record, replay_input, artifact,
        )
        if progress:
            progress("rough", index, total)
        if index % 10 == 0 or index == total:
            research_cache.commit()
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

    repair_wallets = {
        str(row.get("wallet")) for row in wallets
        if row.get("reason") == "fill_history_page_cap"
    }
    repair_candidates = (
        [candidate for candidate in candidates if candidate["wallet"] in repair_wallets]
        if int(recovery_pages) > int(max_pages) else []
    )
    wallet_indexes = {
        str(row.get("wallet")): index for index, row in enumerate(wallets)
    }
    for index, candidate in enumerate(repair_candidates, 1):
        try:
            record, replay_input, artifact = _rough_wallet(
                candidate,
                now_ms=now_ms,
                minimum_week_volume=minimum_week_volume,
                universe=universe,
                namespace=namespace,
                sigmas=sigmas,
                market_context=market_context,
                terminal_marks=terminal_marks,
                known_risk=known_risk,
                max_pages=max(1, int(recovery_pages)),
            )
        except Exception as exc:
            record, replay_input, artifact = ({
                **{key: value for key, value in candidate.items() if key != "addr"},
                "status": "deferred",
                "reason": f"history_repair_error:{type(exc).__name__}",
            }, None, {
                "candidate": dict(candidate),
                "collectorException": type(exc).__name__,
            })
        wallets[wallet_indexes[candidate["wallet"]]] = record
        if replay_input is not None:
            replay_wallets.add(str(record.get("wallet") or ""))
        _cache_rough_record(
            research_cache, run_key, candidate, record, replay_input, artifact,
        )
        if progress:
            progress("history_repair", index, len(repair_candidates))
        if index % 10 == 0 or index == len(repair_candidates):
            research_cache.commit()
            _atomic_json(report_path, {
                "status": "repairing_history",
                "modelVersion": MODEL_VERSION,
                "startedAt": started_at,
                "minimumPerpWeekVolume": minimum_week_volume,
                "leaderboardRows": len(leaderboard),
                "leaderboardVolumeRecall": len(recalled),
                "sampledCandidates": len(candidates),
                "processed": len(candidates),
                "historyRepairCandidates": len(repair_candidates),
                "historyRepairProcessed": index,
                "wallets": wallets,
                "requestStats": rest.request_stats(),
            })

    by_wallet = {str(row.get("wallet")): row for row in wallets}
    operational_wallets = [
        wallet for wallet in replay_wallets
        if bool((by_wallet[wallet].get("activity") or {}).get("operational"))
    ]
    operational_wallets.sort(
        key=lambda wallet: _rough_profit_sort_key(by_wallet[wallet])
    )
    strict_wallets = (
        operational_wallets[:max(0, int(strict_limit))]
        if int(strict_limit) > 0 else operational_wallets
    )
    pre_strict_report = {
        "status": "rough_complete",
        "readOnlySource": True,
        "publishesGeneration": False,
        "changesStrategyRevision": False,
        "modelVersion": MODEL_VERSION,
        "startedAt": started_at,
        "roughFinishedAt": datetime.now(timezone.utc).isoformat(),
        "minimumPerpWeekVolume": minimum_week_volume,
        "qualityGatesApplied": False,
        "structuralGates": list(STRUCTURAL_GATES),
        "preStrictActivityPolicy": {
            "definition": "oid_deduped_copy_threshold_open_or_flip",
            "lookbackDays": ACTIVITY_LOOKBACK_DAYS,
            "bucketDays": ACTIVITY_BUCKET_DAYS,
            "minimumActiveWeeks": ACTIVITY_MIN_ACTIVE_WEEKS,
            "latest7dRequired": True,
            "maximumOpenGapDays": ACTIVITY_MAX_OPEN_GAP_DAYS,
        },
        "leaderboardRows": len(leaderboard),
        "leaderboardVolumeRecall": len(recalled),
        "sampledCandidates": len(candidates),
        "historyRepairCandidates": len(repair_candidates),
        "strictReplayLimit": max(0, int(strict_limit)),
        "preStrictEligibleCandidates": len(operational_wallets),
        "preStrictActivityExcluded": len(replay_wallets) - len(operational_wallets),
        "strictReplayCandidates": 0 if rough_only else len(strict_wallets),
        "strictRankingMode": "operational_activity_then_rough_conservative_profit_70_30",
        "requestStats": rest.request_stats(),
        "summary": summarize(wallets),
        "wallets": wallets,
    }
    _atomic_json(report_path, pre_strict_report)
    if rough_only:
        research_cache.commit()
        research_cache.close()
        return pre_strict_report

    strict_replay_inputs = [
        _load_cached_replay(research_cache, run_key, wallet)
        for wallet in strict_wallets
    ]
    research_cache.commit()
    research_cache.close()
    Path(cache_db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
    cache = storage.connect(
        cache_db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
    )
    os.chmod(Path(cache_db_path).resolve(), 0o600)
    path_start = now_ms - int(config.PROFILE_FETCH_DAYS) * DAY_MS
    all_fills = [
        fill for item in strict_replay_inputs for fill in item["fills"]
    ]
    path_audit = price_path.ensure(
        cache, all_fills, path_start, now_ms, interval=price_path.BASE_INTERVAL,
    )
    leverage = price_path.refresh_margin_metadata(cache, all_fills)
    for coin, maximum in leverage.items():
        market_context.setdefault(coin, {})["max_leverage"] = maximum

    for index, item in enumerate(strict_replay_inputs, 1):
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
            progress("strict", index, len(strict_replay_inputs))

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
        "preStrictActivityPolicy": pre_strict_report["preStrictActivityPolicy"],
        "leaderboardRows": len(leaderboard),
        "leaderboardVolumeRecall": len(recalled),
        "sampledCandidates": len(candidates),
        "historyRepairCandidates": len(repair_candidates),
        "strictReplayLimit": max(0, int(strict_limit)),
        "preStrictEligibleCandidates": len(operational_wallets),
        "preStrictActivityExcluded": len(replay_wallets) - len(operational_wallets),
        "strictReplayCandidates": len(strict_replay_inputs),
        "strictRankingMode": "operational_activity_then_rough_conservative_profit_70_30",
        "pathAudit": path_audit,
        "requestStats": rest.request_stats(),
        "summary": summary,
        "wallets": wallets,
    }
    _atomic_json(report_path, report)
    return report
