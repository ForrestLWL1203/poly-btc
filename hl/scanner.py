"""Discovery domain: the rolling scanner that maintains the live watchlist.

harvest leaderboard -> coarse candidates -> profile work-set (actives + new + top rechecks)
over a short window -> perp episodes/metrics -> upsert active/rejected/retired.
Composes rest + fills + metrics + storage; holds no infra of its own.
"""
import concurrent.futures
import dataclasses
import hashlib
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from . import auto_tune, config, follow_score, generation, metrics, params, pipeline_audit, rest, selection, storage
from .fills import build_episodes, is_spot
from .copy_data import load_copyable_fills, normalize_copyable_fills
from .copy_policy import load_copy_policy
from .copy_evidence import summarize_copy_evidence
from .sector import classify_coin, compact_sector_results
from .fill_transition import classify_fill_transition
from .scanner_copy_bt import (
    apply_copy_bt_gate as _apply_copy_bt_gate,
    apply_sector_copy_bt_gate as _apply_sector_copy_bt_gate,
    copy_bt_market_ctx as _copy_bt_market_ctx,
    copy_bt_overrides as _copy_bt_overrides,
    copy_bt_results as _copy_bt_results,
    copy_bt_sigmas as _copy_bt_sigmas,
    sector_copy_bt_results as _sector_copy_bt_results,
)
from .scanner_lifecycle import (
    prune_discovery_cache as _prune_discovery_cache,
    ScanTimeBudget,
    schedule_profile_workset,
    upsert_wallet_registry,
)
from .util import f, now_iso

_db_lock = threading.Lock()   # serializes sqlite writes across scanner worker threads


def _episode_rows(addr: str, eps: list) -> list:
    """Rows for episode storage; seq preserves same-ms flip/reopen episodes instead of replacing them."""
    seen = {}
    rows = []
    for e in eps:
        key = (e["coin"], e["open_ms"])
        seq = seen.get(key, 0)
        seen[key] = seq + 1
        rows.append((addr, e["coin"], e["side"], e["open_ms"], seq, e["close_ms"], e["hold_s"],
                     e["net_pnl"], e["fee"], e["max_notl"], e["n_fills"], e["open_px"], e["close_px"]))
    return rows


def _load_cached_fills(db, addr, since):
    """Cached raw fills for addr in the [since, now] window (ASC). Empty for a never-scanned candidate."""
    with _db_lock:
        rows = db.execute("SELECT fill_json FROM candidate_fills WHERE addr=? AND time>=? ORDER BY time",
                          (addr, since)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r[0]))
        except (ValueError, TypeError):
            pass
    return out


def _store_cached_fills(db, addr, fills, window_start, *, coverage_complete=False, coverage_end=None):
    """Upsert fills (dedup by tid) + prune anything older than the window. CALLER HOLDS _db_lock."""
    rows = [(addr, x.get("tid"), x["time"], json.dumps(x)) for x in fills if x.get("tid") is not None]
    if rows:
        db.executemany("INSERT OR IGNORE INTO candidate_fills (addr,tid,time,fill_json) VALUES (?,?,?,?)", rows)
    db.execute("DELETE FROM candidate_fills WHERE addr=? AND time<?", (addr, window_start))
    if coverage_complete:
        db.execute(
            "INSERT INTO fill_cache_state(addr,coverage_start_ms,coverage_end_ms,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(addr) DO UPDATE SET coverage_start_ms=MIN(fill_cache_state.coverage_start_ms,excluded.coverage_start_ms),"
            "coverage_end_ms=MAX(COALESCE(fill_cache_state.coverage_end_ms,0),excluded.coverage_end_ms),"
            "updated_at=excluded.updated_at",
            (addr, int(window_start), int(coverage_end or window_start), now_iso()),
        )


def _copy_warmup_backfill_addrs(db, desired_start_ms):
    """Wallets with real Copy evidence whose cache has never been confirmed to cover the warm-up prefix."""
    return [r[0] for r in db.execute(
        "SELECT p.addr FROM profile p LEFT JOIN fill_cache_state s ON s.addr=p.addr "
        "WHERE (COALESCE(p.copy_bt_closed_n,0)>0 OR p.copy_bt_net_pnl IS NOT NULL) "
        "AND (s.coverage_start_ms IS NULL OR s.coverage_start_ms>?) ORDER BY p.addr",
        (int(desired_start_ms),),
    ).fetchall()]


def _replace_episode_rows(db, addr: str, eps: list) -> None:
    erows = _episode_rows(addr, eps)
    db.execute("DELETE FROM episode WHERE addr=?", (addr,))
    if erows:
        db.executemany(
            "INSERT OR REPLACE INTO episode "
            "(addr,coin,side,open_ms,seq,close_ms,hold_s,net_pnl,fee,max_notl,n_fills,open_px,close_px)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            erows)
    stored = db.execute("SELECT COUNT(*) FROM episode WHERE addr=?", (addr,)).fetchone()[0]
    if stored != len(eps):
        raise RuntimeError(f"episode consistency failed for {addr}: stored {stored}, built {len(eps)}")


def repair_missing_episode_rows(db, addrs) -> int:
    """Rebuild missing episode rows from cached fills.

    Older scans could update profile/copy backtest evidence while leaving no episode detail rows.
    Regate and the wallet UI depend on episode detail for activity and risk signals, so repair only
    wallets that have cached fills but no stored episodes.
    """
    repaired = 0
    for addr in dict.fromkeys(a for a in addrs if a):
        has_episode = db.execute("SELECT 1 FROM episode WHERE addr=? LIMIT 1", (addr,)).fetchone()
        if has_episode:
            continue
        fills = _load_cached_fills(db, addr, 0)
        if not fills:
            continue
        perp = [x for x in fills if not is_spot(x.get("coin") or "")]
        eps, _open_eps = build_episodes(perp)
        if not eps:
            continue
        with _db_lock:
            _replace_episode_rows(db, addr, eps)
        repaired += 1
    if repaired:
        db.commit()
    return repaired


def _due_for_full_resync(db):
    """True if no FULL re-sync in the last FULL_RESYNC_DAYS (fresh db / missing col → True). A full re-sync
    re-fetches everyone's window to heal any incremental gap (append-only fills → gap can only be missing)."""
    try:
        r = db.execute(
            "SELECT MAX(finished_at) FROM scan_runs WHERE full=1 AND COALESCE(complete,1)=1"
        ).fetchone()
    except Exception:  # noqa: BLE001 — `full` column not yet added (old db)
        return True
    if not r or not r[0]:
        return True
    try:
        from datetime import datetime, timezone
        last = datetime.fromisoformat(str(r[0]).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).total_seconds() / 86400 >= config.FULL_RESYNC_DAYS
    except Exception:  # noqa: BLE001
        return True


def _copy_bt_cached_fills(db, addr, now_ms, p):
    """Cached copyable fills for regate's no-network copy replay."""
    days = int(getattr(p, "copy_bt_days", config.COPY_BT_DAYS) or config.COPY_BT_DAYS)
    days += int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
    start_ms = now_ms - days * 86400_000
    return normalize_copyable_fills(_load_cached_fills(db, addr, start_ms), addr=addr)


def _fetch_profile_fills(db, addr, window_start, p, full):
    """(raw_full ASC, hit_cap, new_fills_to_persist). Incremental unless `full`: load the cached window,
    fetch ONLY the delta since our cursor (max cached time − overlap), merge (tid-dedup). A never-cached
    candidate, or a delta that blows past the page cap (can't be trusted), falls back to a full re-fetch."""
    if not full:
        stored = _load_cached_fills(db, addr, window_start)
        cursor = max((x["time"] for x in stored), default=None)
        if cursor is not None:
            delta, hit_cap = rest.fetch_window(addr, max(window_start, cursor - config.POLL_OVERLAP_MS), p.max_pages)
            if not hit_cap:
                merged = {x.get("tid"): x for x in stored}
                merged.update({x.get("tid"): x for x in delta})
                raw_full = sorted((x for x in merged.values() if x["time"] >= window_start), key=lambda x: x["time"])
                return raw_full, False, delta
            # delta hit the cap → too many new fills to trust incrementally → full re-fetch (self-heal)
    raw_full, hit_cap = rest.fetch_window(addr, window_start, p.max_pages)
    return raw_full, hit_cap, raw_full


# -- dashboard status (best-effort; a status write must never break a real scan) ----------
def _set_scanner_proc(db, state, detail=None):
    try:
        with _db_lock:
            db.execute("INSERT INTO process_status (name,state,pid,heartbeat_at,detail_json) VALUES "
                       "('scanner',?,?,?,?) ON CONFLICT(name) DO UPDATE SET state=excluded.state,"
                       "pid=excluded.pid,heartbeat_at=excluded.heartbeat_at,detail_json=excluded.detail_json",
                       (state, os.getpid(), now_iso(), json.dumps(detail or {})))
            db.commit()
    except Exception:  # noqa: BLE001
        pass


def _set_scan_progress(db, **kw):
    try:
        with _db_lock:
            cur = db.execute("SELECT id FROM scan_progress WHERE id=1").fetchone()
            if cur is None:
                db.execute("INSERT INTO scan_progress (id,state,updated_at) VALUES (1,'idle',?)", (now_iso(),))
            sets = ",".join(f"{k}=?" for k in kw) + ",updated_at=?"
            db.execute(f"UPDATE scan_progress SET {sets} WHERE id=1", tuple(kw.values()) + (now_iso(),))
            db.commit()
    except Exception:  # noqa: BLE001
        pass


def _payload_requests_full(payload_json) -> bool:
    try:
        return bool(payload_json and json.loads(payload_json).get("full"))
    except (ValueError, TypeError, AttributeError):
        return False


def _resolve_rescan_commands(db, initial_ids, *, run_full, complete, failed, active):
    """Finish only rescan commands this run actually satisfied.

    Requests arriving during the run can be absorbed when they are no stronger than the work just
    completed. A full request arriving during an incremental run is explicitly failed as retryable.
    """
    pending_after = db.execute(
        "SELECT id,payload_json FROM commands WHERE type='rescan' AND status='pending'"
    ).fetchall()
    if complete:
        satisfied = set(initial_ids)
        stronger = []
        for cid, payload_json in pending_after:
            if run_full or not _payload_requests_full(payload_json):
                satisfied.add(cid)
            else:
                stronger.append(cid)
        if satisfied:
            marks = ",".join("?" for _ in satisfied)
            db.execute(
                f"UPDATE commands SET status='done',done_at=?,result_json=? WHERE id IN ({marks})",
                (now_iso(), json.dumps({"active": active, "full": run_full}), *sorted(satisfied)),
            )
        if stronger:
            marks = ",".join("?" for _ in stronger)
            db.execute(
                f"UPDATE commands SET status='failed',done_at=?,error=?,result_json=? WHERE id IN ({marks})",
                (now_iso(), "full_rescan_not_satisfied_by_incremental_run",
                 json.dumps({"retry": True, "full": False}), *stronger),
            )
        return
    failed_ids = sorted(set(initial_ids) | {r[0] for r in pending_after})
    if failed_ids:
        marks = ",".join("?" for _ in failed_ids)
        db.execute(
            f"UPDATE commands SET status='failed',done_at=?,error=?,result_json=? WHERE id IN ({marks})",
            (now_iso(), f"scan_incomplete:{failed}_wallets_failed",
             json.dumps({"retry": True, "failed": failed}), *failed_ids),
        )


# -------------------------------------------------------------------------- harvest
def _prepare_leaderboard_rows(rows, p, fetched_at):
    """Attach the cheap harvest decision without mutating the live leaderboard."""
    min_acct = getattr(p, "min_acct", config.HARVEST_MIN_ACCT)
    vlm_min = getattr(p, "week_vlm_min", config.HARVEST_WEEK_VLM_MIN)
    vlm_max = getattr(p, "week_vlm_max", config.HARVEST_WEEK_VLM_MAX)
    pv_min = getattr(p, "pnl_vol_min", config.HARVEST_PNL_VOL_MIN)
    pv_max = getattr(p, "pnl_vol_max", config.HARVEST_PNL_VOL_MAX)
    prepared = []
    for original in rows or []:
        r = dict(original or {})
        w = {name: perf for name, perf in r.get("windowPerformances", [])}
        wk, mo, al = w.get("week", {}), w.get("month", {}), w.get("allTime", {})
        acct = f(r.get("accountValue"))
        wk_vlm, wk_pnl = f(wk.get("vlm")), f(wk.get("pnl"))
        ratio = wk_pnl / wk_vlm if wk_vlm > 0 else 0.0
        r["is_candidate"] = int(
            acct >= min_acct
            and vlm_min <= wk_vlm <= vlm_max
            and wk_pnl > 0 and f(mo.get("pnl")) > 0 and f(al.get("pnl")) > 0
            and pv_min <= ratio <= pv_max
        )
        r["fetched_at"] = fetched_at
        mon_vlm = f(mo.get("vlm"))
        r["daily_turnover"] = (mon_vlm / acct / 30.0) if acct > 0 else 0.0
        prepared.append(r)
    return prepared


def harvest(db, p, *, generation_id=None) -> int:
    """STAGE-1 leaderboard BOX (v5) — leaderboard windows only, ZERO per-wallet API. Gate ONLY on what
    the leaderboard can HONESTLY say; defer ALL profit JUDGMENT to the profile (real fills). Predicate:
      • acct ≥ floor                         → real capital (we copy by %, not $).
      • vlm_min ≤ 7d VOLUME ≤ vlm_max        → genuinely trading this week, but NOT a market-maker
                                               (billion-$/wk bots sit above the ceiling).
      • 7d & 30d & all-time PnL all > 0      → MULTI-WINDOW consistency: profitable across three
                                               horizons, not a one-window fluke (cheap robustness).
      • pv_min ≤ 7d pnl/volume ≤ pv_max      → profit is a PLAUSIBLE fraction of traded volume: below =
                                               razor-thin MM, above = profit too big for the volume =
                                               NOT trading (deposit/spot/airdrop ghost; real = 0.2-4%).
    Leaderboard ROI/PnL MAGNITUDE is deliberately NOT a gate — it's contaminated (top-ROI wallets are
    $0-volume HODLers/ghosts) and return magnitude belongs in the SCORE, not eligibility. Bots/grids are
    INVISIBLE to leaderboard aggregates (proven), so the profile's grid/worst_loss gates handle them."""
    rows = rest.get_leaderboard()
    now = now_iso()
    prepared = _prepare_leaderboard_rows(rows, p, now)
    n_cand = sum(int(r.get("is_candidate") or 0) for r in prepared)

    if generation_id:
        previous_count = generation.previous_published_row_count(db)
        validation = generation.validate_leaderboard_rows(
            prepared,
            previous_count=previous_count,
            min_row_ratio=float(getattr(config, "LEADERBOARD_MIN_ROW_RATIO", 0.85)),
            min_completeness=float(getattr(config, "LEADERBOARD_MIN_COMPLETE_RATIO", 0.99)),
        )
        generation.stage_leaderboard_rows(db, generation_id, prepared, fetched_at=now)
        generation.record_leaderboard_validation(db, generation_id, validation, fetched_at=now)
        db.commit()
        if not validation.valid:
            raise RuntimeError("leaderboard_invalid:" + ",".join(validation.reasons))
        return n_cand

    # Standalone ``harvest`` remains a leaderboard-only maintenance command.  Full scans use the staging
    # path above and promote this table only with their complete selection generation.
    db.execute("UPDATE leaderboard SET is_candidate=0")
    for r in prepared:
        w = {name: perf for name, perf in r.get("windowPerformances", [])}
        d, wk, mo, al = w.get("day", {}), w.get("week", {}), w.get("month", {}), w.get("allTime", {})
        acct = f(r.get("accountValue"))
        db.execute(
            "INSERT OR REPLACE INTO leaderboard (addr,display_name,account_value,"
            "day_pnl,day_roi,day_vlm,week_pnl,week_roi,week_vlm,mon_pnl,mon_roi,mon_vlm,"
            "all_pnl,all_roi,all_vlm,daily_turnover,is_candidate,fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["ethAddress"].lower(), r.get("displayName"), acct,
             f(d.get("pnl")), f(d.get("roi")), f(d.get("vlm")),
             f(wk.get("pnl")), f(wk.get("roi")), f(wk.get("vlm")),
             f(mo.get("pnl")), f(mo.get("roi")), f(mo.get("vlm")),
             f(al.get("pnl")), f(al.get("roi")), f(al.get("vlm")),
             r.get("daily_turnover"), int(r.get("is_candidate") or 0), now))
    db.commit()
    return n_cand


def _stage_existing_leaderboard(db, generation_id):
    """Use the last published snapshot for a no-harvest scan without changing live membership."""
    cur = db.execute(
        "SELECT addr,display_name,account_value,day_pnl,day_roi,day_vlm,week_pnl,week_roi,week_vlm,"
        "mon_pnl,mon_roi,mon_vlm,all_pnl,all_roi,all_vlm,daily_turnover,is_candidate,fetched_at "
        "FROM leaderboard ORDER BY addr"
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    validation = generation.validate_leaderboard_rows(
        rows,
        previous_count=generation.previous_published_row_count(db),
        min_row_ratio=float(getattr(config, "LEADERBOARD_MIN_ROW_RATIO", 0.85)),
        min_completeness=float(getattr(config, "LEADERBOARD_MIN_COMPLETE_RATIO", 0.99)),
    )
    generation.stage_leaderboard_rows(db, generation_id, rows, fetched_at=now_iso())
    generation.record_leaderboard_validation(db, generation_id, validation)
    db.commit()
    if not validation.valid:
        raise RuntimeError("leaderboard_invalid:" + ",".join(validation.reasons))
    return sum(int(row.get("is_candidate") or 0) for row in rows)


# -------------------------------------------------------------------------- profile
def _self_liquidations(fills, addr, acct):
    """Self-liquidation events (liquidation.liquidatedUser == this wallet, NOT where it was the
    liquidator). Returns (count_by_coin, worst_single_loss_pct_of_equity<=0). Account blow-up
    doesn't transfer to our isolated per-trade copy, so this is a mild high-variance flag."""
    bycoin = {}
    for x in fills:
        liq = x.get("liquidation") or {}
        if (liq.get("liquidatedUser") or "").lower() == addr:
            bycoin[x["coin"]] = bycoin.get(x["coin"], 0.0) + f(x.get("closedPnl"))
    if not bycoin:
        return 0, 0.0
    worst = min(bycoin.values())
    return len(bycoin), (worst / acct * 100 if acct else 0.0)


_DAY_MS = 86400_000.0


def _open_flow_metrics(fills: list, now_ms: int) -> dict:
    """Measure copyable *new-position* supply rather than treating every fill as activity."""
    opens = []
    seen = set()
    for x in sorted(fills or [], key=lambda row: (int(row.get("time") or 0), str(row.get("tid") or ""))):
        try:
            pos0 = f(x.get("startPosition"))
            size = f(x.get("sz"))
            pos1 = pos0 + (size if x.get("side") == "B" else -size)
        except (TypeError, ValueError):
            continue
        if classify_fill_transition(pos0, pos1) not in {"open", "flip"} or abs(pos1) < config.FLAT:
            continue
        key = (x.get("coin"), int(x.get("time") or 0), x.get("oid"), x.get("tid"))
        if key in seen:
            continue
        seen.add(key)
        opens.append(int(x.get("time") or 0))

    def window(days):
        cutoff = now_ms - int(days) * int(_DAY_MS)
        vals = [ts for ts in opens if ts >= cutoff]
        return len(vals), len({ts // int(_DAY_MS) for ts in vals})

    c7, d7 = window(7)
    c14, d14 = window(14)
    c30, d30 = window(30)
    intervals_h = [(b - a) / 3_600_000 for a, b in zip(opens, opens[1:]) if b > a]
    rate_day = c30 / 30.0
    return {
        "last_copyable_open_ms": opens[-1] if opens else 0,
        "open_events_7d": c7, "open_events_14d": c14, "open_events_30d": c30,
        # Refined later by the canonical replay once policy/liquidity/capacity skips are known.
        "actionable_open_events_7d": c7, "actionable_open_events_14d": c14,
        "actionable_open_events_30d": c30,
        "open_days_7d": d7, "open_days_14d": d14, "open_days_30d": d30,
        "avg_open_interval_h": (sum(intervals_h) / len(intervals_h)) if intervals_h else None,
        "median_open_interval_h": statistics.median(intervals_h) if intervals_h else None,
        "open_probability_24h": 1.0 - math.exp(-rate_day),
        "open_probability_48h": 1.0 - math.exp(-2.0 * rate_day),
    }


def _copy_profile_evidence(m, results, p, *, addr="", now_ms=None):
    """Derive non-overlapping, normalized OOS evidence from canonical replay positions."""
    if not isinstance(results, dict):
        results = {}
    by_days = {}
    for key, value in results.items():
        try:
            by_days[int(key)] = value or {}
        except (TypeError, ValueError):
            continue
    primary_days = int(getattr(p, "copy_bt_days", config.COPY_BT_DAYS) or config.COPY_BT_DAYS)
    primary = by_days.get(primary_days) or (by_days.get(max(by_days)) if by_days else {})
    oos = by_days.get(7) or by_days.get(14) or primary
    statuses = {str(result.get("data_status") or "valid") for result in by_days.values()}
    evidence = {str(result.get("evidence_status") or "") for result in by_days.values()}
    if any(status not in {"valid", "ok"} for status in statuses):
        m.update(data_status="deferred_data_error", evidence_status="invalid")
        return

    positions = list(primary.get("positions") or [])
    evidence_summary = summarize_copy_evidence(
        positions,
        seed=f"{addr}:{getattr(p, 'scan_generation', '')}:{primary_days}",
        now_ms=now_ms,
    )
    dd = max(0.0, f(primary.get("max_drawdown")))
    worst_return = min(
        (f(pos.get("net_pnl")) / f(pos.get("margin")) for pos in positions if f(pos.get("margin")) > 0),
        default=0.0,
    )
    actionable_rate = primary.get("open_fill_rate")
    capacity_fit = primary.get("capacity_open_fit")
    master_coverage = primary.get("master_leverage_coverage")
    price_coverage = primary.get("price_path_coverage")
    coverage_parts = [x for x in (master_coverage, price_coverage) if x is not None]
    model_coverage = min(coverage_parts) if coverage_parts else 0.0
    closed_n = int(primary.get("closed_n") or 0)
    if not by_days or evidence.issubset({"", "no_fills", "no_open_events"}):
        evidence_status = "missing"
    elif closed_n < load_copy_policy().min_closed_7d:
        evidence_status = "thin"
    else:
        evidence_status = "qualified"
    m.update(
        data_status="valid",
        evidence_status=evidence_status,
        copy_expected_return=evidence_summary.expected_return,
        copy_return_lcb=evidence_summary.return_lcb,
        copy_return_volatility=evidence_summary.return_volatility,
        copy_positive_probability=evidence_summary.positive_probability,
        copy_evidence_days=evidence_summary.evidence_days,
        copy_recent_return_14d=evidence_summary.recent_return_14d,
        copy_recent_return_7d=evidence_summary.recent_return_7d,
        copy_risk_score=max(0.0, min(1.0, 1.0 - max(dd, abs(min(0.0, worst_return))))),
        execution_score=(
            (f(actionable_rate) + f(capacity_fit)) / 2.0
            if actionable_rate is not None and capacity_fit is not None else None
        ),
        model_coverage=model_coverage,
        oos_net_pnl=oos.get("copy_net_pnl"),
        oos_max_drawdown=oos.get("max_drawdown"),
        oos_cvar95=oos.get("cvar95"),
        actionable_open_rate=actionable_rate,
        capacity_fit=capacity_fit,
    )
    for days in (7, 14, 30):
        result = by_days.get(days) or {}
        if result:
            m[f"actionable_open_events_{days}d"] = int(result.get("opened_n") or 0)


def _profile_copy_qualification(m, now_ms: int, p) -> tuple[bool, str]:
    """One authoritative Profile qualification for evidence, activity and economics."""
    copy_gate_enabled = getattr(p, "copy_bt_gate_enable", config.COPY_BT_GATE_ENABLE)
    if copy_gate_enabled:
        enriched = dict(m)
        enriched["copy_bt_data_status"] = m.get("data_status")
        enriched["copy_bt_evidence_status"] = m.get("evidence_status")
        result = follow_score.evaluate_follow_eligibility(
            enriched,
            min_closed30=getattr(p, "evidence_min_trades", config.EVIDENCE_MIN_TRADES),
            min_evidence_days=getattr(p, "evidence_min_days", config.EVIDENCE_MIN_DAYS),
            min_expected_return=getattr(
                p, "copy_min_expected_margin_return", config.COPY_MIN_EXPECTED_MARGIN_RETURN
            ),
        )
        if not result.get("eligible"):
            if result.get("deferred"):
                return True, "copy_backtest_deferred_data_error"
            return False, result.get("status") or "copy_unqualified"
    last_open = int(m.get("last_copyable_open_ms") or 0)
    max_age_ms = int(getattr(p, "inactive_days", 1.0) * 86_400_000)
    if not last_open or now_ms - last_open > max_age_ms:
        return False, "inactive_copyable_open"
    return True, "ok" if copy_gate_enabled else "copy_gate_disabled"


def _defer_profile(db, addr, prior, stamp, reason):
    """Persist a tri-state data error while preserving the last usable market snapshot."""
    reason = str(reason or "data_error")[:120]
    if prior:
        with _db_lock:
            db.execute(
                "UPDATE profile SET data_status='deferred_data_error',evidence_status='invalid',"
                "evaluated_at=?,reason=? WHERE addr=?",
                (stamp, reason, addr),
            )
            db.commit()
        m = dict(prior)
        m.update(data_status="deferred_data_error", evidence_status="invalid")
        return (prior.get("status") or "quarantine"), reason, m, False
    row = {
        "addr": addr,
        "status": "quarantine",
        "reason": reason,
        "score": 0.0,
        "raw_quality_score": 0.0,
        "data_status": "deferred_data_error",
        "evidence_status": "invalid",
        "evaluated_at": stamp,
        "times_seen": 1,
        "times_active": 0,
    }
    cols = storage.PROFILE_COLS.split(",")
    with _db_lock:
        db.execute(
            f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' * len(cols))})",
            [row.get(c) for c in cols],
        )
        db.commit()
    return "quarantine", reason, row, False


def _open_snapshot(addr, dexes, open_eps, now_ms, acct):
    """Current OPEN-POSITION character across EVERY dex the wallet traded — the data that un-blinds the
    funnel to live positions (a trend trader's winning holds AND a 扛单's losing holds). clearinghouse-
    State is PER-DEX (standard call omits builder/stock xyz:* positions), so we query each dex and
    combine. Returns a dict (None if no dex answered):
      margin_type, cur_leverage, worst_underwater (<=0, most-negative adverse among material positions),
      open_unrealized (total signed $), open_loss_frac / open_win_frac (underwater / winning unrealized
      ÷ acct), bag_count (# material underwater positions), max_bag_days / max_win_days (longest hold,
      from the in-window open episodes' open_ms). Durations are a LOWER bound for positions opened
      pre-window. Tiny dust positions still count toward total unrealized, but do not drive the deep-bag
      score guard."""
    open_ms = {e["coin"]: e["open_ms"] for e in (open_eps or [])}    # coin -> when the live run started
    types, worst_uw = set(), 0.0
    tot_ntl, acct_val, answered, has_pos = 0.0, 0.0, False, False
    up_loss, up_win, bag_n, max_bag_d, max_win_d = 0.0, 0.0, 0, 0.0, 0.0
    open_position_count = material_open_count = 0
    perp_short, perp_notl = {}, 0.0                              # for spot-hedge detection
    for dex in dexes:
        cs = rest.clearinghouse_state(addr, dex=dex)             # dex None -> standard perp dex
        if not isinstance(cs, dict):
            continue
        answered = True
        ms = cs.get("marginSummary", {})
        acct_val = max(acct_val, f(ms.get("accountValue")))      # standard dex carries the real equity
        tot_ntl += f(ms.get("totalNtlPos"))
        for pp in cs.get("assetPositions", []) or []:
            has_pos = True
            p_ = pp.get("position", {})
            coin = p_.get("coin")
            types.add((p_.get("leverage") or {}).get("type"))
            szi, entry, pv = f(p_.get("szi")), f(p_.get("entryPx")), f(p_.get("positionValue"))
            if abs(szi) < config.FLAT:
                continue
            open_position_count += 1
            upnl = f(p_.get("unrealizedPnl"))                    # HL's authoritative current unrealized
            days = (now_ms - open_ms[coin]) / _DAY_MS if coin in open_ms else 0.0
            perp_notl += abs(pv)
            if szi < 0:                                          # a SHORT — candidate hedge of a spot long
                perp_short[(coin or "").upper()] = perp_short.get((coin or "").upper(), 0.0) + abs(pv)
            risk_acct = acct or acct_val or 0.0
            material = True
            if risk_acct > 0:
                material = abs(pv) / risk_acct >= config.OPEN_RISK_MIN_POSITION_EQUITY_FRAC
            if material:
                material_open_count += 1
            if entry and szi and material:
                mark = pv / abs(szi)
                worst_uw = min(worst_uw, (mark - entry) / entry * (1 if szi > 0 else -1))
            if upnl < 0:
                up_loss += upnl
                if material:
                    bag_n += 1
                    max_bag_d = max(max_bag_d, days)   # a material carried LOSS = a bag
            elif upnl > 0:
                up_win += upnl;   max_win_d = max(max_win_d, days)               # a carried WIN = trend value
    if not answered:
        return None
    # SPOT-HEDGE ratio: a perp SHORT offset by a spot LONG of the same token is a hedge (its perp PnL is
    # cancelled by spot → the naked perp leg we'd copy is a loss). Only fetch spot when there ARE shorts.
    hedge_ratio = 0.0
    if perp_short and perp_notl:
        ss = rest.spot_clearinghouse_state(addr)
        spot_val = {}
        for b in (ss.get("balances") if isinstance(ss, dict) else []) or []:
            tok, v = (b.get("coin") or "").upper(), f(b.get("entryNtl"))
            if v <= 0:
                continue
            spot_val[tok] = spot_val.get(tok, 0.0) + v
            if tok.startswith("U") and len(tok) > 1:            # Unit-wrapped major: UBTC->BTC, UETH->ETH
                spot_val[tok[1:]] = spot_val.get(tok[1:], 0.0) + v
        hedged = sum(min(notl, spot_val.get(c, 0.0)) for c, notl in perp_short.items())
        hedge_ratio = hedged / perp_notl
    types.discard(None)
    mt = next(iter(types)) if len(types) == 1 else ("mixed" if types else "flat")
    a = acct or acct_val or 1.0
    return {"margin_type": mt if has_pos else "flat",
            "cur_leverage": (tot_ntl / acct_val if acct_val else 0.0),
            "worst_underwater": worst_uw, "open_unrealized": up_loss + up_win,
            "open_loss_frac": up_loss / a, "open_win_frac": up_win / a,
            "bag_count": bag_n, "max_bag_days": max_bag_d, "max_win_days": max_win_d,
            "open_position_count": open_position_count, "material_open_count": material_open_count,
            "hedge_ratio": hedge_ratio}


def _profile_one(db, addr, start_ms, now_ms, p, prior, lb, stamp, universe, force_full=False):
    # ONE aggregated fetch per wallet (aggregateByTime -> ~1 page, trade-level). No separate
    # pre-screen call: gates() already rejects dormant ("inactive"), spot/opaque-dominant
    # ("spot_dominant") and no-trades ("no_perp_trades") on this same data — the old two-stage
    # split only existed to avoid a heavy raw fetch, which aggregation made cheap.
    # Fetch a LONG window (PROFILE_FETCH_DAYS) via the paginated fetch_window — it sorts ASCENDING and
    # caps at max_pages*2000 fills (NOT a single 2000-row page: user_fills_latest truncated active wallets
    # at 2000 AND returned newest-first unsorted, which broke window_days/trades_per_day/last_fill_ms and
    # over-rejected as hit_page_cap). We slice the 14d window for the existing scoring metrics (behaviour
    # unchanged) and use the full fetch for the multi-window / lifetime nets — still ONE fetch per wallet.
    if not universe:
        return _defer_profile(db, addr, prior, stamp, "universe_unavailable")
    window_start = now_ms - config.PROFILE_FETCH_DAYS * 86400_000
    # Workset scope and fill-fetch mode are independent.  A UI "full scan" may evaluate every candidate
    # while only the scheduler-selected migration/repair wallets perform a complete historical refetch.
    full = bool(force_full or not config.INCREMENTAL_SCAN)
    try:
        raw_full, hit_cap, new_fills = _fetch_profile_fills(db, addr, window_start, p, full)
    except Exception as exc:  # noqa: BLE001 - network failures are a first-class deferred outcome
        return _defer_profile(db, addr, prior, stamp, f"fills_error:{type(exc).__name__}")
    for x in raw_full:
        x["user"] = addr
    # only COPYABLE activity counts: crypto perps + transparent builder perps (stocks/commodities,
    # e.g. xyz:AAPL — in `universe`). Spot is excluded (is_spot); opaque/private builder dexes are
    # excluded by not being in `universe`. perp_frac = copyable-perp share of fills.
    perp_full = normalize_copyable_fills(raw_full, addr=addr, universe=universe)
    raw = [x for x in raw_full if x["time"] >= start_ms]          # 14d window slice (scoring metrics)
    perp = [x for x in perp_full if x["time"] >= start_ms]
    perp_frac = (len(perp) / len(raw)) if raw else 0.0
    eps, open_eps = build_episodes(perp)
    m = metrics.compute_metrics(perp, eps, now_ms, p.days)
    if m is None:
        m = {"n_fills": len(perp), "n_trades": 0, "window_days": 0, "trades_per_day": 0,
             "taker_frac_notl": 0, "median_hold_s": 0, "win_rate": 0, "net_pnl": 0, "gross_pnl": 0,
             "roi_notional": 0, "total_notl": 0, "total_fee": 0, "n_coins": 0, "top_coin": None,
             "long_frac": 0, "max_drawdown": 0, "avg_notional": 0, "hold_skew": 0,
             "last_fill_ms": raw[-1]["time"] if raw else 0, "active_days": 0, "activity_ratio": 0,
             "median_eps": 0, "pos_day_ratio": 0, "profit_conc": 0,
             "max_adds_per_ep": 0, "median_adds_per_ep": 0, "worst_loss": 0.0,
             "tp_move_pct": 0.0, "market_type": None, "crypto_frac": None}
    # multi-window / lifetime realized nets from the FULL history (in-memory, no extra fetch) — the
    # long-term stability cross-check + the net_life datum the 14d window can't see. Computed even when
    # the 14d window is empty (dormant-but-historically-profitable wallets still get a true net_life).
    eps_full, _ = build_episodes(perp_full)
    m.update(metrics.window_nets(eps_full, now_ms))
    m.update(_open_flow_metrics(perp_full, now_ms))

    acct_value = f((lb or {}).get("account_value"))
    m["perp_frac"] = perp_frac
    m["acct_value"] = acct_value
    # HL 官方 return-on-capital(净利/本金)三窗口 → score() 的 ROI 支柱(取代 net/名义)。None 保留以便加权归一。
    _lbroi = lambda k: (f(lb[k]) if lb and lb.get(k) is not None else None)
    m["week_roi"], m["mon_roi"], m["all_roi"] = _lbroi("week_roi"), _lbroi("mon_roi"), _lbroi("all_roi")
    m["roi_equity"] = (m["net_pnl"] / acct_value) if acct_value else 0.0
    m["worst_loss_pct"] = (m["worst_loss"] / acct_value) if acct_value else 0.0  # loss discipline (realized)
    m["times_active"] = (prior or {}).get("times_active", 0)
    m["lev_proxy"] = (m["avg_notional"] / acct_value) if acct_value else 0.0  # hist. eff. leverage
    m["liq_count"], m["liq_worst_pct"] = _self_liquidations(raw, addr, acct_value)
    # open-position character defaults (filled by the live snapshot in stage B). roi_total starts as the
    # realized-only roi and is upgraded to realized+unrealized once we read the wallet's live positions.
    m.update(open_underwater=0.0, open_unrealized=0.0, open_loss_frac=0.0, open_win_frac=0.0,
             bag_count=0, open_position_count=0, material_open_count=0,
             max_bag_days=0.0, max_win_days=0.0, hedge_ratio=0.0, roi_total=m["roi_equity"])
    m["margin_type"] = (prior or {}).get("margin_type")
    m["cur_leverage"] = (prior or {}).get("cur_leverage") or 0.0

    # STAGE A — cheap structural copyability (NO api). Front-of-funnel rejects (MM/HFT/grid/spot) that do
    # NOT kill a genuine trend trader. n_trades==0 (pure-hold) skips the episode-based checks → judged on
    # live positions in stage B. (Old behaviour auto-rejected n_trades==0 as 'no_closed_episode'.)
    if not perp:
        ok, reason = False, "no_copyable_perp_fills"
    elif hit_cap:
        ok, reason = False, "hit_page_cap"
    else:
        ok, reason = metrics.gates_structural(m, p)

    # STAGE B — fetch the LIVE open-position snapshot (un-blinds the funnel to held positions), fold in
    # realized+unrealized roi, then re-judge: held position = ACTIVE, 扛单 bags drag roi_total negative,
    # trend holders kept. Only structural survivors pay the extra clearinghouse call.
    if ok:
        dexes = {(c.split(":")[0] if ":" in c else None) for c in {x["coin"] for x in perp}}
        snap = _open_snapshot(addr, dexes, open_eps, now_ms, acct_value)
        if snap is None:
            return _defer_profile(db, addr, prior, stamp, "clearinghouse_unavailable")
        m["margin_type"] = snap["margin_type"]
        m["cur_leverage"] = snap["cur_leverage"]
        m["open_underwater"] = snap["worst_underwater"]
        for k in ("open_unrealized", "open_loss_frac", "open_win_frac", "bag_count",
                  "open_position_count", "material_open_count",
                  "max_bag_days", "max_win_days", "hedge_ratio"):
            m[k] = snap[k]
        m["roi_total"] = ((m["net_pnl"] + snap["open_unrealized"]) / acct_value) if acct_value else 0.0
        # v7 PORTFOLIO — authoritative NET-of-fees, deposit-adjusted account perf (one call, all windows).
        # Fed to the ROI pillar (net, replacing leaderboard gross) + the turnover/edge-bps copyability filters.
        _pf = rest.portfolio(addr)
        _pw = rest.parse_portfolio(_pf, "week")
        _pm = rest.parse_portfolio(_pf, "month")
        if not _pw or not _pm:
            return _defer_profile(db, addr, prior, stamp, "portfolio_unavailable")
        m["pf_week_pnl"], m["pf_week_vlm"] = _pw.get("pnl"), _pw.get("vlm")
        m["pf_mon_pnl"], m["pf_mon_vlm"] = _pm.get("pnl"), _pm.get("vlm")
        m["pf_equity"] = _pw.get("equity") or _pm.get("equity")
        m["pf_max_dd"] = _pm.get("max_drawdown") or _pw.get("max_drawdown")   # 30d curve = fuller DD picture
        m["pf_turnover"], m["pf_edge_bps"] = _pw.get("turnover"), _pw.get("edge_bps")
        ok, reason = metrics.gates_state(m, now_ms, p)
    if ok:
        copy_results = _copy_bt_results(addr, perp_full, now_ms, p)
        sector_results = _sector_copy_bt_results(addr, perp_full, now_ms, p)
        ok, reason = _apply_sector_copy_bt_gate(
            m, copy_results, sector_results, p,
            previous_policy=(prior or {}).get("sector_policy_json"),
        )
        try:
            sector_policy = json.loads(m.get("sector_policy_json") or "{}")
        except (TypeError, ValueError):
            sector_policy = {}
        allowed_sectors = set(sector_policy.get("allowed") or [])
        evidence_results = copy_results
        evidence_fills = perp_full
        if allowed_sectors and allowed_sectors != {"crypto", "stock"}:
            allowed_fills = [x for x in perp_full if classify_coin(x.get("coin")) in allowed_sectors]
            evidence_fills = allowed_fills
            evidence_results = _copy_bt_results(addr, allowed_fills, now_ms, p)
        m.update(_open_flow_metrics(evidence_fills, now_ms))
        _copy_profile_evidence(m, evidence_results, p, addr=addr, now_ms=now_ms)
        if not sector_policy.get("allowed") and m.get("evidence_status") not in {"missing", "invalid"}:
            m["evidence_status"] = "economically_disqualified"
        if m.get("data_status") == "deferred_data_error":
            return _defer_profile(db, addr, prior, stamp, "copy_replay_unavailable")
        if ok:
            ok, reason = _profile_copy_qualification(m, now_ms, p)
    m["times_active"] += 1 if ok else 0

    # age is NOT fetched (a full-history call just for account age = wasteful, and would penalise a
    # new wallet with strong recent performance). Survival now leans on times_active (our own observed
    # cross-scan persistence), not age. Keep any age a prior run already had; never fetch a new one.
    m["age_days"] = (prior or {}).get("age_days")

    prev_status = (prior or {}).get("status")
    m["score"] = metrics.score(m) if ok else 0.0
    m["raw_quality_score"] = m["score"]
    if ok and m["score"] < getattr(p, "min_active_score", config.MIN_ACTIVE_SCORE):
        ok, reason, m["score"] = False, "low_quality", 0.0
    status = "active" if ok else ("retired" if prev_status == "active" else "rejected")
    row = dict(m)                                    # keys match column names -> robust positional build
    row.update(addr=addr, status=status, reason=reason, last_refreshed=stamp,
               profile_generation=getattr(p, "scan_generation", None), evaluated_at=stamp,
               data_status="valid" if ok else "rejected",
               evidence_status=m.get("evidence_status") or ("qualified" if ok else "rejected"),
               first_added=(prior or {}).get("first_added") or (stamp if ok else None),
               times_seen=(prior or {}).get("times_seen", 0) + 1)
    cols = storage.PROFILE_COLS.split(",")
    with _db_lock:
        _store_cached_fills(
            db, addr, new_fills, window_start,
            coverage_complete=bool(full and not hit_cap), coverage_end=now_ms,
        )   # persist the delta + prune the window
        _replace_episode_rows(db, addr, eps)
        db.execute(f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                   f"VALUES ({','.join('?' * len(cols))})", [row.get(c) for c in cols])
        db.commit()
    return status, reason, m, hit_cap


# ------------------------------------------------------------------ curated outputs
def refresh_watchlist(db, stamp, source: str = "watchlist", *, update_follow_line=True,
                      update_follow_history=True, leaderboard_generation=None, commit=True) -> int:
    """Rebuild OUR tiny leaderboard (watchlist) from active profiles. Derived view —
    profile stays the source of truth; operator settings in target_controls survive.

    ``update_follow_line`` and ``update_follow_history`` are retained for call compatibility only.  Explicit
    published Core owns production membership and its history is written after atomic Selection publication.
    """
    if commit:
        params.seed_params(db)
    prev_followed = set(selection.published_core_addrs(db) or [])
    db.execute("DELETE FROM watchlist")
    leaderboard_join = (
        "LEFT JOIN leaderboard_staging l ON l.addr=p.addr AND l.generation=?"
        if leaderboard_generation else
        "LEFT JOIN leaderboard l ON l.addr=p.addr"
    )
    cur = db.execute(
        "SELECT p.addr, l.display_name, p.score, p.roi_equity, l.mon_roi, p.net_pnl, p.acct_value, "
        "p.n_trades, p.trades_per_day, p.taker_frac_notl, p.median_hold_s, p.win_rate, p.max_drawdown, "
        "p.age_days, p.top_coin, p.market_type, p.tp_move_pct, p.roi_total, p.open_loss_frac, p.open_win_frac, "
        "p.perp_frac, p.lev_proxy, p.margin_type, p.cur_leverage, p.liq_worst_pct, "
        "p.times_active, p.first_added, p.last_fill_ms, "
        "p.copy_bt_net_pnl,p.copy_bt_win_rate,p.copy_bt_closed_n,p.copy_bt_open_fill_rate,"
        "p.copy_bt_liquidations,p.copy_bt_fee_drag,p.copy_bt_14d_net_pnl,p.copy_bt_14d_closed_n,"
        "p.copy_bt_7d_net_pnl,p.copy_bt_7d_closed_n,p.sector_copy_json,p.sector_policy_json,"
        "p.profile_generation,p.evaluated_at,p.data_status,p.evidence_status,"
        "p.copy_expected_return,p.copy_return_lcb,p.copy_return_volatility,p.copy_positive_probability,"
        "p.copy_evidence_days,p.copy_recent_return_14d,p.copy_recent_return_7d,p.copy_risk_score,"
        "p.execution_score,p.actionable_open_rate,p.capacity_fit,p.open_probability_48h "
        f"FROM profile p {leaderboard_join} "
        "WHERE p.status='active' ORDER BY p.score DESC, p.addr",
        (leaderboard_generation,) if leaderboard_generation else (),
    )
    row_cols = [d[0] for d in cur.description]
    rows = [dict(zip(row_cols, r)) for r in cur.fetchall()]
    ranked = []
    for r in rows:
        score, detail = follow_score.compute_follow_score(r)
        detail = dict(detail or {})
        eligibility = follow_score.evaluate_follow_eligibility(r)
        base_score = float(score or 0.0)
        stability = {
            "previouslyFollowed": (r["addr"] or "").lower() in prev_followed,
            "baseFollowScore": base_score,
            "bonus": 0.0,
            "status": "new_or_unfollowed",
        }
        if not eligibility.get("eligible"):
            floor = float(getattr(config, "AUTO_FOLLOW_MIN_SCORE", 0.60))
            score = min(score, max(0.0, floor - 1e-9))
            detail.setdefault("reasons", []).extend(eligibility.get("reasons") or [])
            stability["status"] = "ineligible" if stability["previouslyFollowed"] else "new_or_unfollowed"
        elif stability["previouslyFollowed"]:
            # Membership stability is expressed by lifecycle entry/keep confirmation, never by silently
            # inflating the displayed score.
            stability["status"] = "previously_followed"
        detail["stability"] = stability
        r["follow_detail"] = detail
        r["follow_eligibility"] = eligibility
        r["follow_score"] = score
        ranked.append(r)
    ranked.sort(key=lambda r: (-(r["follow_score"] or 0.0), r["addr"]))
    for rank, r in enumerate(ranked, 1):
        db.execute(
            "INSERT INTO watchlist (rank,addr,display_name,score,roi_equity,mon_roi,net_pnl,acct_value,"
            "n_trades,trades_per_day,taker_frac,median_hold_s,win_rate,max_drawdown,age_days,top_coin,"
            "market_type,tp_move_pct,roi_total,open_loss_frac,open_win_frac,"
            "perp_frac,lev_proxy,margin_type,cur_leverage,liq_worst_pct,sector_copy_json,sector_policy_json,"
            "generation,profile_generation,evaluated_at,data_status,evidence_status,"
            "times_active,first_added,last_fill_ms,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rank, r["addr"], r["display_name"], r["follow_score"], r["roi_equity"], r["mon_roi"],
                r["net_pnl"], r["acct_value"], r["n_trades"], r["trades_per_day"], r["taker_frac_notl"],
                r["median_hold_s"], r["win_rate"], r["max_drawdown"], r["age_days"], r["top_coin"],
                r["market_type"], r["tp_move_pct"], r["roi_total"], r["open_loss_frac"], r["open_win_frac"],
                r["perp_frac"], r["lev_proxy"], r["margin_type"], r["cur_leverage"], r["liq_worst_pct"],
                r["sector_copy_json"], r["sector_policy_json"], r["profile_generation"],
                r["profile_generation"], r["evaluated_at"], r["data_status"], r["evidence_status"],
                r["times_active"], r["first_added"], r["last_fill_ms"], stamp,
            ))
        db.execute("INSERT OR IGNORE INTO target_controls (addr,enabled,updated_at) VALUES (?,1,?)",
                   (r["addr"], stamp))
    if commit:
        db.commit()
    return len(rows)


_HARD_EXIT_REASONS = {
    "spot_dominant", "bot_frequency", "hft_uncopyable", "grid_dca", "too_many_concurrent",
    "hit_page_cap", "no_copyable_perp_fills", "spot_hedge", "blowup_loss",
}


def _iso_ms(value):
    if not value:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _portfolio_selection_metrics(windows, baseline_n=0, selected_n=0):
    """Compact shared-account replay into actual-dollar selection economics.

    Isolated liquidations already lose their full allocated margin in ``copy_net_pnl`` and equity drawdown.
    Risk-adjusted utility subtracts max drawdown dollars once more, so a wallet passes only when its added
    net profit more than compensates for any added drawdown; liquidation count itself is not a veto.
    """
    usable = []
    for days, result in (windows or {}).items():
        if not result:
            continue
        closed = int(result.get("closed_n") or 0)
        if closed < max(1, load_copy_policy().min_closed_7d):
            continue
        usable.append((int(days), result))
    if not usable:
        # Empty baseline is a valid starting portfolio; any candidate still needs real evidence.
        if selected_n == 0:
            return selection.PortfolioMetrics(
                0.0, 0.0, 0, 1.0, 1.0, 0.0, 0.0, 0.0,
                net_pnl=0.0, stress_net_pnl=0.0, drawdown_dollars=0.0,
                risk_adjusted_utility=0.0,
            )
        return selection.PortfolioMetrics(
            -1e12, -1e12, 0, 0.0, 0.0, 1.0, 1.0, 1.0,
            net_pnl=-1e12, stress_net_pnl=-1e12,
            drawdown_dollars=float(config.INITIAL_BALANCE), risk_adjusted_utility=-1e12,
        )
    primary = next((row for row in usable if row[0] == 30), max(usable, key=lambda row: row[0]))
    net_pnl = f(primary[1].get("copy_net_pnl"))
    stress_net = min(f(row[1].get("copy_net_pnl")) for row in usable)
    liquidations = max(int(row[1].get("liquidations") or 0) for row in usable)
    def num(value, default=0.0):
        return default if value is None else f(value)

    actionable = min(num(row[1].get("open_fill_rate"), 0.0) for row in usable)
    capacity = min(num(row[1].get("capacity_open_fit"), actionable) for row in usable)
    max_dd = max(num(row[1].get("max_drawdown"), 0.0) for row in usable)
    peak_deploy = max(num(row[1].get("peak_deploy_pct"), 0.0) for row in usable)
    cost_drag = max(
        num(row[1].get("fee_slippage_drag"), f(row[1].get("fee_drag")))
        / max(1.0, abs(f(row[1].get("copy_gross_pnl"))))
        for row in usable
    )
    drawdown_dollars = max_dd * float(config.INITIAL_BALANCE)
    risk_adjusted_utility = net_pnl - drawdown_dollars
    return selection.PortfolioMetrics(
        net_pnl, stress_net, liquidations, actionable, capacity, max_dd, peak_deploy,
        cost_drag, net_pnl=net_pnl, stress_net_pnl=stress_net,
        drawdown_dollars=drawdown_dollars, risk_adjusted_utility=risk_adjusted_utility,
    )


def _build_explicit_selection(db, generation_id, stamp, now_ms, *, force_cold_bootstrap=False,
                              validate_price_path=True):
    """Build Core/Challenger roles and optimize shared-account membership to a stable set."""
    copy_policy = load_copy_policy()
    previous_generation = None if force_cold_bootstrap else selection.latest_published_generation(db)
    previous_roles = {}
    if previous_generation:
        previous_roles = {
            (addr or "").lower(): role
            for addr, role in db.execute(
                "SELECT addr,role FROM follow_selection WHERE generation=?", (previous_generation,)
            ).fetchall()
        }

    cold_bootstrap = bool(
        (force_cold_bootstrap or previous_generation is None)
        and not previous_roles
        and params.get(
            db, "FOLLOW_SELECTION_BOOTSTRAP_ENABLE", config.FOLLOW_SELECTION_BOOTSTRAP_ENABLE
        )
    )

    held = {
        (addr or "").lower()
        for table in ("copy_position", "shadow_position")
        for (addr,) in db.execute(f"SELECT DISTINCT addr FROM {table} WHERE status='open'").fetchall()
    }
    controls = {
        (addr or "").lower(): bool(enabled)
        for addr, enabled in db.execute("SELECT addr,enabled FROM target_controls").fetchall()
    }
    registry = {}
    for row in db.execute(
        "SELECT addr,state,current_role,first_qualified_at,consecutive_qualified,consecutive_bad "
        "FROM wallet_registry"
    ).fetchall():
        registry[(row[0] or "").lower()] = {
            "state": row[1], "role": row[2], "first_qualified_at": row[3],
            "good": int(row[4] or 0), "bad": int(row[5] or 0),
        }

    cur = db.execute(
        "SELECT p.addr,p.status,p.reason,p.score,p.profile_generation,p.data_status,p.evidence_status,p.last_copyable_open_ms,"
        "p.copy_bt_closed_n,p.copy_bt_14d_closed_n,p.copy_bt_7d_closed_n,"
        "p.copy_positive_probability,p.copy_expected_return,p.copy_return_lcb,p.copy_return_volatility,"
        "p.copy_evidence_days,p.copy_recent_return_14d,p.copy_recent_return_7d,p.copy_risk_score,"
        "p.execution_score,p.open_probability_48h,"
        "p.actionable_open_rate,p.capacity_fit,p.copy_bt_net_pnl,p.copy_bt_14d_net_pnl,p.copy_bt_7d_net_pnl,"
        "p.copy_bt_open_fill_rate,p.copy_bt_liquidations,p.copy_bt_fee_drag,p.sector_policy_json "
        "FROM profile p"
    )
    names = [desc[0] for desc in cur.description]
    profiles = [dict(zip(names, row)) for row in cur.fetchall()]
    # watchlist.score is the published final Copy-follow score.  Selection must consume that exact value
    # rather than recomputing from a narrower row projection and creating an invisible second score line.
    watch_scores = {
        (addr or "").lower(): score
        for addr, score in db.execute("SELECT addr,score FROM watchlist").fetchall()
    }
    for row in profiles:
        addr = (row.get("addr") or "").lower()
        row["follow_score"] = (
            f(watch_scores[addr]) if addr in watch_scores
            else follow_score.compute_follow_score(row)[0]
        )
    profiles.sort(key=lambda row: (-(row.get("follow_score") or 0.0), row.get("addr") or ""))
    for rank, row in enumerate(profiles, 1):
        row["rank"] = rank
    if True:  # sole production selection path; there is no observation-period mode
        # Active means the wallet itself is structurally/economically copyable.  Score orders the bounded
        # candidate pool; the shared-account replay then keeps candidates whose added net profit exceeds
        # their added max-drawdown dollars.  There is no LCB, observation-period or liquidation-count veto.
        portfolio_candidates = [
            (row.get("addr") or "").lower() for row in profiles
            if row.get("status") in {"active", "qualified"}
            and controls.get((row.get("addr") or "").lower(), True)
        ][:int(config.MAX_TARGETS)]
        # If canonical portfolio replay is unavailable, preserve only a still-qualified published Core.
        # Never fabricate production targets from a retired score threshold.
        selected_set = {
            addr for addr in portfolio_candidates if previous_roles.get(addr) == selection.CORE
        }
        portfolio_rejections = {}
        portfolio_utilities = {}
        marginal = None
        evaluate = None
        if portfolio_candidates:
            window_fills = auto_tune._portfolio_window_fills(db, portfolio_candidates, now_ms)
            if window_fills is not None and any(window_fills.values()):
                follow = params.load_follow(db)
                if "SMART_ADD" in follow:
                    follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
                neutral_tune, _ = auto_tune.resolve_tune_baseline(
                    db, {key: f(follow[key]) for key in auto_tune.TUNE_KEYS},
                )
                neutral_add, _ = auto_tune.resolve_add_baseline(
                    db, {key: f(follow[key]) for key in auto_tune.ADD_TUNE_KEYS},
                )
                neutral_follow = {**follow, **neutral_tune, **neutral_add}
                if "SMART_ADD" in neutral_follow:
                    neutral_follow["ADD_STRATEGY"] = (
                        "smart" if neutral_follow["SMART_ADD"] else "hardcap"
                    )
                sigmas = auto_tune._load_sigmas(db)
                market_ctx = auto_tune._load_market_ctx(db)
                eval_cache = {}
                validation_cache = {}

                def evaluate(addrs):
                    key = tuple(sorted(addrs))
                    if key in eval_cache:
                        return eval_cache[key]
                    allowed = set(key)
                    # Core search ranks on the primary 30-day window only.  Avoid rebuilding unused 14/7
                    # lists for every beam candidate; finalists receive the normal multi-window tuner later.
                    filtered = {
                        30: [
                            fill for fill in (window_fills.get(30) or [])
                            if (fill.get("user") or "").lower() in allowed
                        ]
                    }
                    primary = auto_tune.evaluate_portfolio_window(
                        db,
                        list(key),
                        sigmas,
                        neutral_follow,
                        now_ms,
                        window_fills=filtered,
                        days=30,
                        market_ctx=market_ctx,
                    )
                    eval_cache[key] = _portfolio_selection_metrics(
                        {30: primary}, baseline_n=0, selected_n=len(key),
                    )
                    return eval_cache[key]

                def validate(addrs):
                    key = tuple(sorted(addrs))
                    if key in validation_cache:
                        return validation_cache[key]
                    filtered = auto_tune._filter_window_fills_by_addr(window_fills, key)
                    windows = auto_tune._candidate_windows(
                        db,
                        list(key),
                        sigmas,
                        follow,
                        now_ms,
                        window_fills=filtered,
                        market_ctx=market_ctx,
                    )
                    validation_cache[key] = _portfolio_selection_metrics(
                        windows, baseline_n=0, selected_n=len(key),
                    )
                    return validation_cache[key]

                constraints = selection.SelectionConstraints(
                    min_relative_lcb_improvement=0.0,
                    min_actionable_open_rate=copy_policy.min_actionable_open_rate,
                    min_capacity_fit=copy_policy.min_capacity_fit,
                    max_drawdown_worsening=1.0,
                    max_deploy_pct=float(params.get(db, "MAX_DEPLOY_PCT", config.MAX_DEPLOY_PCT)),
                    max_cost_drag_ratio=1.0,
                    max_targets=int(config.MAX_TARGETS),
                )
                marginal = selection.search_smart_core(
                    portfolio_candidates,
                    evaluate,
                    constraints,
                    seed_target=int(getattr(config, "CORE_SEARCH_SEED_TARGET", 10)),
                    beam_width=int(getattr(config, "CORE_SEARCH_BEAM_WIDTH", 3)),
                    swap_passes=int(getattr(config, "CORE_SEARCH_SWAP_PASSES", 1)),
                    max_replace_out=int(getattr(config, "CORE_SEARCH_MAX_REPLACE_OUT", 2)),
                    min_marginal_gain_ratio=float(getattr(
                        config, "CORE_SEARCH_MIN_MARGINAL_GAIN_RATIO", 0.01,
                    )),
                    time_budget_s=float(getattr(config, "CORE_SEARCH_TIME_BUDGET_SEC", 600)),
                    validation_evaluator=validate,
                )
                selected_set = set(marginal.selected)
                # The beam search deliberately stays fills-only for bounded runtime. Its finalist must then
                # survive the shared 15m market path before it can change production membership. Missing
                # path data retains the still-qualified published Core; it never turns an optimistic replay
                # into a newly published target set.
                if selected_set and validate_price_path:
                    selected_fills = [
                        fill for fill in (window_fills.get(30) or [])
                        if (fill.get("user") or "").lower() in selected_set
                    ]
                    from . import price_path
                if selected_set and validate_price_path and price_path.coins_for_fills(selected_fills):
                    path_start = now_ms - (
                        30 + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7))
                    ) * 86_400_000
                    path_rows = price_path.load_refined(db, selected_fills, path_start, now_ms)
                    path_meta = price_path.coverage(db, selected_fills, path_start, now_ms)
                    path_rejected = set()
                    for candidate_addr in sorted(selected_set):
                        wallet_fills = [
                            fill for fill in selected_fills
                            if (fill.get("user") or "").lower() == candidate_addr
                        ]
                        wallet_result = auto_tune.evaluate_portfolio_window(
                            db, [candidate_addr], sigmas,
                            {**neutral_follow, "AMBIGUOUS_PATH_MODE": "liquidate"}, now_ms,
                            window_fills={30: wallet_fills}, days=30, market_ctx=market_ctx,
                            path_rows=path_rows, path_meta=path_meta,
                        )
                        wallet_liqs = int(wallet_result.get("liquidations") or 0)
                        wallet_closed = int(wallet_result.get("closed_n") or 0)
                        wallet_liq_rate = wallet_liqs / wallet_closed if wallet_closed else 0.0
                        if f(wallet_result.get("copy_net_pnl")) <= 0:
                            path_rejected.add(candidate_addr)
                            portfolio_rejections[candidate_addr] = "path_copy_net_nonpositive"
                        elif (
                            wallet_liqs >= int(getattr(config, "CORE_PATH_WALLET_MAX_LIQUIDATIONS", 3))
                            and wallet_liq_rate >= float(getattr(
                                config, "CORE_PATH_WALLET_MAX_LIQUIDATION_RATE", .05,
                            ))
                        ):
                            path_rejected.add(candidate_addr)
                            portfolio_rejections[candidate_addr] = "path_liquidation_excess"
                    selected_set -= path_rejected
                    if path_rejected:
                        marginal = dataclasses.replace(
                            marginal,
                            selected=tuple(sorted(selected_set)),
                            action="path_regate",
                            removed=tuple(sorted(set(marginal.removed) | path_rejected)),
                            search_meta={**dict(marginal.search_meta or {}),
                                         "pathRejected": len(path_rejected)},
                        )
                    selected_fills = [
                        fill for fill in selected_fills
                        if (fill.get("user") or "").lower() in selected_set
                    ]
                    path_primary = auto_tune.evaluate_portfolio_window(
                        db, sorted(selected_set), sigmas,
                        {**neutral_follow, "AMBIGUOUS_PATH_MODE": "liquidate"}, now_ms,
                        window_fills={30: selected_fills}, days=30, market_ctx=market_ctx,
                        path_rows=path_rows, path_meta=path_meta,
                    )
                    path_ok = not selected_set or (
                        float(path_meta.get("coverage") or 0.0)
                        >= float(getattr(config, "CORE_PRICE_PATH_MIN_COVERAGE", .95))
                        and f(path_primary.get("maintenance_margin_coverage"))
                        >= float(getattr(config, "CORE_MAINTENANCE_META_MIN_COVERAGE", .95))
                        and f(path_primary.get("copy_net_pnl")) > 0
                    )
                    if not path_ok:
                        selected_set = {
                            addr for addr in portfolio_candidates
                            if previous_roles.get(addr) == selection.CORE
                        }
                        marginal = None
                final_metrics = evaluate(tuple(sorted(selected_set)))
                final_utility = f(final_metrics.risk_adjusted_utility)
                for addr in selected_set:
                    without = tuple(sorted(selected_set - {addr}))
                    portfolio_utilities[addr] = final_utility - f(
                        evaluate(without).risk_adjusted_utility
                    )
                for addr in portfolio_candidates:
                    if addr in selected_set:
                        continue
                    if str(portfolio_rejections.get(addr) or "").startswith("path_"):
                        continue
                    trial = evaluate(tuple(sorted(selected_set | {addr})))
                    final_net = f(final_metrics.net_pnl)
                    marginal_net = f(trial.net_pnl) - final_net
                    min_gain_ratio = float(getattr(
                        config, "CORE_SEARCH_MIN_MARGINAL_GAIN_RATIO", 0.01,
                    ))
                    if (
                        marginal_net > 0
                        and final_net > 0
                        and marginal_net / final_net < min_gain_ratio
                    ):
                        portfolio_rejections[addr] = "portfolio_marginal_gain_below_floor"
                    else:
                        portfolio_rejections[addr] = selection.portfolio_economic_rejection_reason(
                            final_metrics, trial, constraints,
                        )

        rows = []
        for row in profiles:
            addr = (row["addr"] or "").lower()
            enabled = controls.get(addr, True)
            refreshed_now = row.get("profile_generation") == generation_id
            data_status = row.get("data_status") or "valid"
            selection_data_status = data_status if refreshed_now or data_status == "deferred_data_error" else "stale"
            active = row.get("status") in {"active", "qualified"}
            previous_role = previous_roles.get(addr) or registry.get(addr, {}).get("role")
            if data_status == "deferred_data_error" and previous_role == selection.CORE and enabled:
                role, reason = selection.CORE, "deferred_data_error"
            elif active and enabled and addr in selected_set:
                role = selection.CORE
                reason = (
                    "portfolio_positive_net_contribution" if marginal is not None
                    else "portfolio_replay_unavailable_keep_core"
                )
            elif addr in held:
                role, reason = selection.EXIT_ONLY, "exit_only_open_position"
            elif active:
                role = selection.CHALLENGER
                if not enabled:
                    reason = "operator_disabled"
                elif marginal is not None and addr in portfolio_candidates:
                    reason = portfolio_rejections.get(addr, "portfolio_no_profit_improvement")
                else:
                    reason = "portfolio_replay_unavailable"
            else:
                continue
            rows.append(selection.SelectionRow(
                addr=addr,
                role=role,
                enabled=enabled,
                reason=reason,
                utility=portfolio_utilities.get(addr, f(row.get("follow_score"))),
                data_status=selection_data_status,
                evidence_status=row.get("evidence_status") or "",
                model_version=(
                    "selection-smart-expansion-v1" if marginal is not None
                    else "selection-replay-unavailable-v1"
                ),
                policy_version=copy_policy.version,
            ))
            lifecycle_state = role if role in {
                selection.CORE, selection.CHALLENGER, selection.EXIT_ONLY
            } else "qualified"
            upsert_wallet_registry(
                db,
                addr,
                generation=generation_id,
                seen_at=stamp,
                state=lifecycle_state,
                role=role,
                data_status=selection_data_status,
                reason=reason,
                last_actionable_open_ms=row.get("last_copyable_open_ms"),
            )
            db.execute(
                "UPDATE profile SET selection_marginal_utility=? WHERE addr=?",
                (portfolio_utilities.get(addr), addr),
            )
        return rows, marginal
    evidences = []
    row_by_addr = {}
    for row in profiles:
        addr = (row["addr"] or "").lower()
        row_by_addr[addr] = row
        prior_reg = {} if force_cold_bootstrap else registry.get(addr, {})
        previous_role = previous_roles.get(addr) or prior_reg.get("role") or selection.CHALLENGER
        data_status = row.get("data_status") or "valid"
        refreshed_now = row.get("profile_generation") == generation_id
        effective_data_status = data_status if refreshed_now or data_status == "deferred_data_error" else "stale"
        lifecycle_data_status = (
            "valid" if effective_data_status in {"valid", "rejected"} else effective_data_status
        )
        soft_bad = (
            row.get("status") not in {"active", "qualified"}
            or row.get("evidence_status") in {"economically_disqualified", "invalid"}
        )
        hard_exit = row.get("reason") in _HARD_EXIT_REASONS
        good_now = lifecycle_data_status == "valid" and row.get("status") in {"active", "qualified"} and not soft_bad
        bad_now = lifecycle_data_status == "valid" and soft_bad
        evidences.append(selection.LifecycleEvidence(
            addr=addr,
            now_ms=now_ms,
            current_role=previous_role,
            data_status=lifecycle_data_status,
            consecutive_complete_good=int(prior_reg.get("good") or 0) + (1 if good_now else 0),
            consecutive_soft_bad=int(prior_reg.get("bad") or 0) + (1 if bad_now else 0),
            last_actionable_open_ms=row.get("last_copyable_open_ms"),
            oos_closed_n=int(row.get("copy_bt_7d_closed_n") or row.get("copy_bt_closed_n") or 0),
            positive_probability=f(row.get("copy_positive_probability")),
            challenger_since_ms=_iso_ms(prior_reg.get("first_qualified_at")),
            soft_bad=soft_bad,
            soft_bad_reason=row.get("reason") or "soft_bad",
            hard_exit=hard_exit,
            hard_exit_reason=row.get("reason") or "hard_exit",
            has_open_copy=addr in held,
        ))

    decisions = {d.addr: d for d in selection.decide_lifecycles(
        evidences,
        selection.LifecyclePolicy(
            entry_complete_generations=(
                1 if cold_bootstrap else int(getattr(config, "CORE_ENTRY_CONFIRM_GENERATIONS", 2))
            ),
            entry_actionable_age_ms=int(copy_policy.entry_max_open_age_h * 3_600_000),
            entry_oos_closes=int(copy_policy.min_closed_7d),
            entry_positive_probability=copy_policy.entry_positive_probability,
            challenger_observation_ms=(
                0 if cold_bootstrap
                else int(getattr(config, "CORE_ENTRY_MIN_CHALLENGER_H", 24) * 3_600_000)
            ),
            keep_actionable_grace_ms=int(copy_policy.keep_max_open_age_h * 3_600_000),
            soft_bad_generations=int(getattr(config, "CORE_SOFT_CONFIRM_GENERATIONS", 2)),
            max_soft_membership_changes=len(evidences),
        ),
    )}
    baseline_core = sorted(
        addr for addr, decision in decisions.items()
        if decision.previous_role == selection.CORE and decision.role == selection.CORE
        and controls.get(addr, True)
    )[:int(config.MAX_TARGETS)]
    entry_candidates = [
        addr for addr, decision in decisions.items()
        if decision.previous_role != selection.CORE and decision.role == selection.CORE
        and controls.get(addr, True)
    ]
    entry_candidates.sort(key=lambda addr: (
        -(row_by_addr[addr].get("copy_expected_return") or -1e9),
        row_by_addr[addr].get("rank") or 999999,
        addr,
    ))
    entry_candidates = entry_candidates[:int(config.MAX_TARGETS)]

    selected_core = tuple(baseline_core)
    marginal = None
    if entry_candidates:
        all_addrs = sorted(set(baseline_core) | set(entry_candidates))
        window_fills = auto_tune._portfolio_window_fills(db, all_addrs, now_ms)
        if window_fills is not None and any(window_fills.values()):
            follow = params.load_follow(db)
            if "SMART_ADD" in follow:
                follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
            sigmas = auto_tune._load_sigmas(db)
            baseline_n = len(baseline_core)

            def evaluate(addrs):
                filtered = auto_tune._filter_window_fills_by_addr(window_fills, addrs)
                windows = auto_tune._candidate_windows(
                    db, list(addrs), sigmas, follow, now_ms, window_fills=filtered,
                )
                return _portfolio_selection_metrics(
                    windows, baseline_n=baseline_n, selected_n=len(addrs),
                )

            try:
                constraints = selection.SelectionConstraints(
                    min_relative_lcb_improvement=copy_policy.min_marginal_gain,
                    min_actionable_open_rate=copy_policy.min_actionable_open_rate,
                    min_capacity_fit=copy_policy.min_capacity_fit,
                    max_drawdown_worsening=copy_policy.max_drawdown_worsening,
                    max_deploy_pct=float(params.get(db, "MAX_DEPLOY_PCT", config.MAX_DEPLOY_PCT)),
                    max_targets=int(config.MAX_TARGETS),
                )
                marginal = selection.select_core_until_stable(
                    baseline_core,
                    entry_candidates,
                    evaluate,
                    constraints,
                    action="bootstrap" if cold_bootstrap else "rebalance",
                )
                selected_core = marginal.selected
            except Exception as exc:  # noqa: BLE001 - never publish a generation with fabricated empty Core
                raise RuntimeError(f"selection_marginal_replay_failed:{exc}") from exc

    selected_set = set(selected_core)
    portfolio_rejections = {}
    portfolio_utilities = {}
    if marginal is not None:
        final_metrics = evaluate(selected_core)
        for addr in selected_core:
            without = tuple(item for item in selected_core if item != addr)
            portfolio_utilities[addr] = final_metrics.net_lcb - evaluate(without).net_lcb
        for addr in entry_candidates:
            if addr in selected_set:
                continue
            trial = evaluate(tuple(sorted(selected_set | {addr})))
            portfolio_rejections[addr] = selection.portfolio_rejection_reason(
                final_metrics, trial, constraints,
            )
    rows = []
    for addr, decision in sorted(decisions.items()):
        profile = row_by_addr[addr]
        enabled = controls.get(addr, True)
        if addr in selected_set:
            role, reason = selection.CORE, decision.reason
        elif addr in held and decision.role != selection.CORE:
            role, reason = selection.EXIT_ONLY, decision.reason
        else:
            role = selection.CHALLENGER
            reason = portfolio_rejections.get(addr, decision.reason)
        utility = portfolio_utilities.get(addr)
        include_selection = (
            role in {selection.CORE, selection.EXIT_ONLY}
            or profile.get("status") in {"active", "qualified"}
            or profile.get("data_status") == "deferred_data_error" and decision.previous_role == selection.CORE
        )
        if include_selection:
            selection_data_status = profile.get("data_status") or "valid"
            if selection_data_status != "deferred_data_error" and profile.get("profile_generation") != generation_id:
                selection_data_status = "stale"
            rows.append(selection.SelectionRow(
                addr=addr,
                role=role,
                enabled=enabled,
                reason=reason,
                utility=utility,
                data_status=selection_data_status,
                evidence_status=profile.get("evidence_status") or "",
                model_version="selection-vnext-1",
                policy_version=copy_policy.version,
            ))
        lifecycle_state = role if role in {selection.CORE, selection.CHALLENGER, selection.EXIT_ONLY} else "qualified"
        if profile.get("data_status") == "deferred_data_error":
            lifecycle_state = "quarantine" if role != selection.CORE else "core"
        elif profile.get("status") not in {"active", "qualified"}:
            lifecycle_state = "rejected"
        elif profile.get("evidence_status") in {"economically_disqualified", "invalid"}:
            lifecycle_state = "cooldown"
        registry_data_status = profile.get("data_status") or "valid"
        if registry_data_status != "deferred_data_error" and profile.get("profile_generation") != generation_id:
            registry_data_status = "unobserved"
        upsert_wallet_registry(
            db,
            addr,
            generation=generation_id,
            seen_at=stamp,
            state=lifecycle_state,
            role=role,
            data_status=registry_data_status,
            reason=profile.get("reason"),
            last_actionable_open_ms=profile.get("last_copyable_open_ms"),
        )
        db.execute("UPDATE profile SET selection_marginal_utility=? WHERE addr=?", (utility, addr))
    return rows, marginal


def _record_explicit_follow_history(db, selection_rows, stamp, previous_core, generation_id):
    current_core = {row.addr for row in selection_rows if row.role == selection.CORE and row.enabled}
    scores = {
        addr: score for addr, score in db.execute(
            "SELECT addr,score FROM watchlist WHERE addr IN (%s)" % (
                ",".join("?" for _ in current_core) or "NULL"
            ),
            tuple(current_core),
        ).fetchall()
    } if current_core else {}
    db.executemany(
        "INSERT INTO follow_history (addr,first_followed_at,last_followed_at,last_followed_score,"
        "first_followed_generation,last_followed_generation) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(addr) DO UPDATE SET "
        "first_followed_at=COALESCE(follow_history.first_followed_at,excluded.first_followed_at),"
        "last_followed_at=excluded.last_followed_at,last_followed_score=excluded.last_followed_score,"
        "first_followed_generation=COALESCE(follow_history.first_followed_generation,"
        "excluded.first_followed_generation),last_followed_generation=excluded.last_followed_generation",
        [(
            addr, stamp if addr not in previous_core else None, stamp, scores.get(addr),
            generation_id if addr not in previous_core else None, generation_id,
        ) for addr in sorted(current_core)],
    )
    if current_core != set(previous_core):
        db.execute(
            "INSERT INTO commands (type,payload_json,owner,created_at) VALUES (?,?,?,?)",
            ("reload_params", json.dumps({
                "by": "explicit_selection", "previous": len(previous_core), "current": len(current_core),
            }), "scanner", stamp),
        )
    return current_core


def _maybe_auto_tune_margins(db, source: str, stamp: str, *, allow_apply: bool = True,
                             data_complete: bool = True) -> dict:
    try:
        mode = str(params.get(db, "AUTO_TUNE_MODE", getattr(config, "AUTO_TUNE_MODE", "shadow")) or "shadow").lower()
        dry_run = (not allow_apply) or mode != "apply"
        if mode == "off":
            res = {"status": "disabled", "reason": "auto_tune_mode_off", "applied": False, "mode": mode}
        else:
            try:
                res = auto_tune.maybe_tune_margins(
                    db, source=source, stamp=stamp, dry_run=dry_run, mode=mode,
                    data_complete=data_complete,
                )
            except TypeError as exc:
                # Test doubles and rolling-deploy workers may still expose the legacy signature.
                if "unexpected keyword" not in str(exc):
                    raise
                res = auto_tune.maybe_tune_margins(db, source=source, stamp=stamp)
            res.setdefault("mode", mode)
    except Exception as exc:  # noqa: BLE001 — auto tuning must never abort discovery
        res = {
            "status": "error",
            "reason": "auto_tune_exception",
            "error": str(exc),
            "applied": False,
        }
        pipeline_audit.record_auto_tune_result(db, stamp, source, res)
        db.commit()
        print(f"auto-tune margin: skipped after {source}: {exc}", flush=True)
        return res
    if res.get("status") != "ok":
        pipeline_audit.record_auto_tune_result(db, stamp, source, res)
        db.commit()
        print(f"auto-tune margin: {res.get('status')}", flush=True)
        return res
    pipeline_audit.record_auto_tune_result(db, stamp, source, res)
    db.commit()
    margins = res.get("margins") or {}
    lev_caps = res.get("lev_caps") or {}
    add_params = res.get("add_params") or {}
    print(
        "auto-tune margin: "
        f"mult={res.get('selected_mult')} applied={bool(res.get('applied'))} "
        f"followed={res.get('followed_n')} "
        f"stable={margins.get('STABLE_MARGIN_PCT', 0) * 100:.2f}% "
        f"mid={margins.get('MID_MARGIN_PCT', 0) * 100:.2f}% "
        f"high={margins.get('HIGH_MARGIN_PCT', 0) * 100:.2f}% "
        f"lev={tuple(lev_caps.get(k) for k in ('STABLE_LEV_CAP', 'MID_LEV_CAP', 'HIGH_LEV_CAP'))} "
        f"full={(res.get('deploy_full_pct') or 0) * 100:.0f}% "
        f"add=k{add_params.get('ADD_GAP_K')} g{add_params.get('ADD_GAP_SHRINK_G')} "
        f"hard{add_params.get('ADD_MAX_HARD')}",
        flush=True,
    )
    return res


def refresh_selection_copy_replay(db, generation_id: str, *, replayed_at=None) -> dict:
    """Refresh dashboard Copy PnL with the currently effective follow parameters.

    Scan-time profile evidence remains the immutable qualification snapshot.  Auto-tune runs after list
    publication, so its per-wallet replay belongs on the generation selection: the UI can match the live
    sizing/add/leverage rules without a post-tune regate changing Core membership.
    """
    current = selection.latest_published_generation(db)
    if not generation_id or current != generation_id:
        return {"status": "skipped", "reason": "generation_not_current", "generation": generation_id}
    rows = db.execute(
        "SELECT addr FROM follow_selection WHERE generation=? "
        "AND role IN ('core','challenger') ORDER BY addr",
        (generation_id,),
    ).fetchall()
    if not rows:
        return {"status": "ok", "generation": generation_id, "refreshed": 0}

    now_ms = int(time.time() * 1000)
    stamp = replayed_at or now_iso()
    overrides = _copy_bt_overrides(db)
    replay_hash = hashlib.sha256(
        json.dumps(overrides, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    replay_ctx = SimpleNamespace(
        copy_bt_days=int(config.COPY_BT_DAYS),
        copy_bt_sigmas=_copy_bt_sigmas(db),
        copy_bt_market_ctx=_copy_bt_market_ctx(db),
        copy_bt_overrides=overrides,
        scan_generation=generation_id,
    )
    updates = []
    for (addr,) in rows:
        fills = _copy_bt_cached_fills(db, addr, now_ms, replay_ctx)
        windows = _copy_bt_results(addr, fills, now_ms, replay_ctx)
        sectors = _sector_copy_bt_results(addr, fills, now_ms, replay_ctx)
        primary = (windows.get(30) or windows.get(max(windows))) if windows else {}
        recent14 = windows.get(14) or {}
        recent7 = windows.get(7) or {}
        opened = int(primary.get("opened_n") or 0)
        target_open = int(primary.get("target_open_events") or 0)
        updates.append((
            primary.get("copy_net_pnl"), primary.get("copy_win_rate"),
            int(primary.get("closed_n") or 0),
            (opened / target_open) if target_open else None,
            int(primary.get("liquidations") or 0), primary.get("fee_drag"),
            recent14.get("copy_net_pnl"), int(recent14.get("closed_n") or 0),
            recent7.get("copy_net_pnl"), int(recent7.get("closed_n") or 0),
            json.dumps(compact_sector_results(sectors), sort_keys=True),
            replay_hash, stamp, generation_id, addr,
        ))
    db.executemany(
        "UPDATE follow_selection SET replay_copy_bt_net_pnl=?,replay_copy_bt_win_rate=?,"
        "replay_copy_bt_closed_n=?,replay_copy_bt_open_fill_rate=?,replay_copy_bt_liquidations=?,"
        "replay_copy_bt_fee_drag=?,replay_copy_bt_14d_net_pnl=?,replay_copy_bt_14d_closed_n=?,"
        "replay_copy_bt_7d_net_pnl=?,replay_copy_bt_7d_closed_n=?,replay_sector_copy_json=?,"
        "replay_params_hash=?,replayed_at=? WHERE generation=? AND addr=?",
        updates,
    )
    db.commit()
    return {
        "status": "ok", "generation": generation_id, "refreshed": len(updates),
        "paramsHash": replay_hash, "replayedAt": stamp,
    }


def tune_published_generation(db, generation_id, stamp=None, source="scan"):
    """Run one generation-bound tuner proposal with a DB lease.

    This entrypoint is intentionally separate from ``scan`` so tuning cannot delay atomic list publication.
    """
    current = selection.latest_published_generation(db)
    if current != generation_id:
        return {"status": "skipped", "reason": "generation_not_current", "applied": False}
    stamp = stamp or now_iso()
    now_s = time.time()
    row = db.execute("SELECT value FROM auto_tune_state WHERE key='async_tuner_lease'").fetchone()
    try:
        lease = json.loads(row[0]) if row and row[0] else {}
    except (TypeError, ValueError):
        lease = {}
    if f(lease.get("expiresAt")) > now_s and int(lease.get("pid") or 0) != os.getpid():
        return {"status": "skipped", "reason": "tuner_already_running", "applied": False}
    db.execute(
        "INSERT INTO auto_tune_state (key,value,updated_at) VALUES ('async_tuner_lease',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (json.dumps({"pid": os.getpid(), "generation": generation_id, "expiresAt": now_s + 7200}), stamp),
    )
    db.commit()
    try:
        result = _maybe_auto_tune_margins(db, source, stamp, allow_apply=True, data_complete=True)
        try:
            result["portfolioReplay"] = auto_tune.store_effective_portfolio_replay(
                db, generation_id,
            )
        except Exception as exc:  # noqa: BLE001 - dashboard summary must not invalidate tuning
            result["portfolioReplay"] = {"status": "error", "error": str(exc)[:300]}
        try:
            result["selectionReplay"] = refresh_selection_copy_replay(
                db, generation_id, replayed_at=now_iso()
            )
        except Exception as exc:  # noqa: BLE001 - display replay must never invalidate a tune run
            result["selectionReplay"] = {"status": "error", "error": str(exc)[:300]}
        return result
    finally:
        db.execute(
            "UPDATE auto_tune_state SET value=?,updated_at=? WHERE key='async_tuner_lease'",
            (json.dumps({"pid": os.getpid(), "generation": generation_id, "expiresAt": 0}), now_iso()),
        )
        db.commit()


def _launch_async_tuner(db, generation_id, stamp):
    mode = str(params.get(db, "AUTO_TUNE_MODE", getattr(config, "AUTO_TUNE_MODE", "shadow")) or "shadow").lower()
    if mode == "off":
        return {"status": "disabled", "reason": "auto_tune_mode_off"}
    db_row = db.execute("PRAGMA database_list").fetchone()
    db_path = db_row[2] if db_row and len(db_row) > 2 else None
    if not db_path or db_path == ":memory:":
        return {"status": "skipped", "reason": "async_tuner_requires_file_db"}
    script = str(Path(__file__).resolve().parent.parent / "hl_discover.py")
    limit_mb = int(getattr(config, "TUNER_MEMORY_LIMIT_MB", 512))

    def _limit_memory():
        try:
            import resource
            limit = max(128, limit_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except Exception:  # noqa: BLE001 - platform may not expose RLIMIT_AS
            pass

    try:
        systemd_run = shutil.which("systemd-run")
        if os.environ.get("INVOCATION_ID") and systemd_run:
            unit = "hl-tune-" + hashlib.sha256(str(generation_id).encode()).hexdigest()[:12]
            completed = subprocess.run(
                [
                    systemd_run,
                    "--quiet",
                    "--no-block",
                    "--collect",
                    f"--unit={unit}",
                    f"--property=MemoryMax={limit_mb}M",
                    f"--working-directory={Path(script).resolve().parent}",
                    sys.executable,
                    script,
                    "--db",
                    str(Path(db_path).resolve()),
                    "tune",
                    "--generation",
                    generation_id,
                    "--stamp",
                    stamp,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
            )
            if completed.returncode:
                raise RuntimeError((completed.stderr or "systemd-run failed").strip()[:200])
            return {
                "status": "launched", "unit": unit, "generation": generation_id,
                "memoryLimitMb": limit_mb,
            }
        proc = subprocess.Popen(
            [sys.executable, script, "--db", db_path, "tune", "--generation", generation_id,
             "--stamp", stamp],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            preexec_fn=_limit_memory if os.name == "posix" else None,
        )
        return {"status": "launched", "pid": proc.pid, "generation": generation_id, "memoryLimitMb": limit_mb}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "reason": "tuner_launch_failed", "error": str(exc)[:200]}


def repair_published_selection(db, generation_id=None, stamp=None, *, replace_existing=False,
                               launch_tuner=True):
    """Rebuild selection from the current complete generation without network access.

    This is intentionally narrow: it never fetches network data or rewrites profiles.  Replacing a non-empty
    Core requires the explicit ``replace_existing`` flag.  It repairs both an empty bootstrap and a published
    selection produced from a stale derived watchlist without repeating an expensive full scan.
    """
    current = selection.latest_published_generation(db)
    generation_id = generation_id or current
    if not current or generation_id != current:
        raise RuntimeError("selection_repair_requires_current_generation")
    meta = db.execute(
        "SELECT complete,profile_complete FROM scan_generation WHERE generation=? AND status='published'",
        (generation_id,),
    ).fetchone()
    if not meta or not int(meta[0] or 0) or not int(meta[1] or 0):
        raise RuntimeError("selection_repair_requires_complete_generation")
    existing_core = selection.published_core_addrs(db) or []
    if existing_core and not replace_existing:
        return {"status": "skipped", "reason": "core_already_present", "core": len(existing_core)}
    stale_active = db.execute(
        "SELECT COUNT(*) FROM profile WHERE status='active' AND COALESCE(profile_generation,'')<>?",
        (generation_id,),
    ).fetchone()[0]
    if stale_active:
        raise RuntimeError("selection_repair_has_stale_active_profiles")

    stamp = stamp or now_iso()
    refresh_watchlist(
        db,
        stamp,
        source="selection_repair",
        update_follow_line=False,
        update_follow_history=False,
        leaderboard_generation=generation_id,
        commit=False,
    )
    rows, marginal = _build_explicit_selection(
        db, generation_id, stamp, int(time.time() * 1000),
        force_cold_bootstrap=not bool(existing_core),
    )
    previous_core = set(existing_core)
    selection.replace_selection_rows(db, generation_id, rows, selected_at=stamp)
    current_core = _record_explicit_follow_history(db, rows, stamp, previous_core, generation_id)
    for row in rows:
        pipeline_audit._insert_event(
            db,
            stamp=stamp,
            source="selection_repair",
            stage="selection",
            addr=row.addr,
            status=row.role,
            reason=row.reason,
            follow_score=row.utility,
            payload={
                "generation": generation_id,
                "dataStatus": row.data_status,
                "evidenceStatus": row.evidence_status,
            },
        )
    pipeline_audit._insert_event(
        db,
        stamp=stamp,
        source="selection_repair",
        stage="selection_summary",
        status="ok",
        reason="repaired_selection" if previous_core else "repaired_cold_bootstrap",
        payload={
            "generation": generation_id,
            "action": marginal.action if marginal else "keep",
            "search": marginal.search_meta if marginal else None,
            "evaluated": marginal.evaluated if marginal else 0,
            "core": len(current_core),
            "challenger": sum(1 for row in rows if row.role == selection.CHALLENGER),
        },
    )
    db.commit()
    launch = _launch_async_tuner(db, generation_id, stamp) if launch_tuner else {
        "status": "skipped", "reason": "synchronous_optimizer",
    }
    pipeline_audit._insert_event(
        db,
        stamp=stamp,
        source="selection_repair",
        stage="tuner_launch",
        status=launch.get("status"),
        reason=launch.get("reason") or "generation_bound_async",
        payload=launch,
    )
    db.commit()
    return {
        "status": "repaired",
        "generation": generation_id,
        "core": len(current_core),
        "challenger": sum(1 for row in rows if row.role == selection.CHALLENGER),
        "selectionAction": marginal.action if marginal else "keep",
        "tuner": launch,
    }


def optimize_published_generation(db, generation_id=None, stamp=None) -> dict:
    """One synchronous operator entrypoint: path-regate Core, jointly tune all execution params, persist."""
    generation_id = generation_id or selection.latest_published_generation(db)
    stamp = stamp or now_iso()
    selection_result = repair_published_selection(
        db, generation_id, stamp=stamp, replace_existing=True, launch_tuner=False,
    )
    tune_result = tune_published_generation(db, generation_id, stamp=stamp)
    return {
        "status": "ok" if tune_result.get("status") == "ok" else tune_result.get("status"),
        "generation": generation_id,
        "selection": selection_result,
        "tune": tune_result,
    }


def refresh_watchlist_and_auto_tune(db, stamp: str, source: str = "scan", before_auto_tune=None,
                                    *, auto_tune_enabled: bool = True, allow_tune_apply: bool = True) -> int:
    """Rebuild the derived watchlist, then evaluate an execution-parameter proposal.

    vNext deliberately does *not* regate every stored profile after an applied proposal: most daily
    profiles were not network-refreshed, so replaying them with fresh execution params could promote or
    retire wallets from stale portfolio/open-position state.  A later complete generation publishes the
    new evidence atomically; shadow proposals never mutate live parameters.
    """
    n_active = refresh_watchlist(db, stamp, source=source)
    if before_auto_tune:
        before_auto_tune()
        db.commit()
    if auto_tune_enabled:
        _maybe_auto_tune_margins(db, source, stamp, allow_apply=allow_tune_apply)
    else:
        skipped = {"status": "skipped", "reason": "scan_incomplete", "applied": False, "mode": "shadow"}
        pipeline_audit.record_auto_tune_result(db, stamp, source, skipped)
        db.commit()
    return n_active


def _active_profile_addrs(db):
    return [r[0] for r in db.execute(
        "SELECT addr FROM profile WHERE status='active' ORDER BY score DESC, addr").fetchall()]


def _watchlist_addrs(db):
    return [r[0] for r in db.execute("SELECT addr FROM watchlist ORDER BY rank").fetchall()]


def ensure_watchlist_current(db, stamp=None) -> int:
    """Repair the derived watchlist if a previous scan died after profile updates but before rebuild."""
    active = _active_profile_addrs(db)
    current = _watchlist_addrs(db)
    if set(current) == set(active):
        return len(current)
    # Repair is a pure derived-view rebuild.  Re-running gates against stale live-position/portfolio
    # snapshots could reactivate or retire wallets without a fresh network generation.
    return refresh_watchlist(
        db,
        stamp or now_iso(),
        source="repair",
        update_follow_line=selection.latest_published_generation(db) is None,
        update_follow_history=selection.latest_published_generation(db) is None,
    )


def _record_run(db, started, t0, candidates, profiled, added, retired, kept, rejected, n_active,
                full=0, failed=0, complete=True):
    db.execute(
        "INSERT INTO scan_runs (started_at,finished_at,duration_s,candidates,profiled,probed_new,added,"
        "retired,kept,rejected,n_active,full,failed,complete) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (started, now_iso(), round(time.time() - t0, 1), candidates, profiled, profiled, added, retired,
         kept, rejected, n_active, 1 if full else 0, failed, 1 if complete else 0))
    db.commit()


def regate(db, p, *, stamp=None, source: str = "regate",
           auto_tune_enabled: bool = False, quiet: bool = False) -> int:
    """Re-apply gates() + score() on ALREADY-STORED profile metrics (no network, no re-fetch) and
    rebuild the watchlist. Thresholds (win/roiEq/dd/tpd/hold/...) can be tuned in seconds without a
    full re-sweep — the expensive part (fetching fills, building episodes) is already done."""
    now = int(time.time() * 1000)
    stamp = stamp or now_iso()
    p.copy_bt_sigmas = getattr(p, "copy_bt_sigmas", None) or _copy_bt_sigmas(db)
    p.copy_bt_market_ctx = getattr(p, "copy_bt_market_ctx", None) or _copy_bt_market_ctx(db)
    p.copy_bt_overrides = getattr(p, "copy_bt_overrides", None) or _copy_bt_overrides(db)
    rows = db.execute(
        "SELECT p.addr,status,n_trades,n_fills,perp_frac,last_fill_ms,net_pnl,roi_equity,max_drawdown,"
        "acct_value,age_days,times_active,liq_worst_pct,active_days,activity_ratio,median_eps,avg_notional,"
        "pos_day_ratio,profit_conc,hold_skew,open_underwater,max_adds_per_ep,median_adds_per_ep,worst_loss_pct,median_hold_s,win_rate,"
        "roi_total,open_loss_frac,open_win_frac,bag_count,max_bag_days,liq_count,hedge_ratio,net_30d,net_life,reason,"
        "l.week_roi,l.mon_roi,l.all_roi,"                      # HL return-on-capital windows for the ROI pillar
        "p.pf_turnover,p.pf_mon_pnl,p.pf_mon_vlm,p.pf_week_pnl,p.pf_equity,"   # v7 portfolio net metrics (gates + ROI)
        "p.payoff_ratio,p.pf_week_vlm,"   # v9: needed so regate applies the SAME payoff + edge-decay gates as a scan
        "p.copy_bt_net_pnl,p.copy_bt_win_rate,p.copy_bt_closed_n,p.copy_bt_open_fill_rate,"
        "p.copy_bt_liquidations,p.copy_bt_fee_drag,p.copy_bt_14d_net_pnl,p.copy_bt_14d_closed_n,"
        "p.copy_bt_7d_net_pnl,p.copy_bt_7d_closed_n,p.sector_copy_json,p.sector_policy_json "
        "FROM profile p LEFT JOIN leaderboard l ON p.addr=l.addr").fetchall()
    repaired_eps = repair_missing_episode_rows(db, [r[0] for r in rows])
    if repaired_eps:
        print(f"regate: repaired {repaired_eps} missing episode caches from candidate_fills")
    # p90 per-episode fill count per wallet, from the stored episode table. Missing episode rows are repaired
    # above from cached fills before this gate runs. p90 (not max) so a swing trader who sliced ONE illiquid-stock fill
    # isn't killed for a single outlier; only SYSTEMATIC slicing (≥10% heavy round-trips) trips it.
    # Load episode-derived regate inputs in one pass. Previously loss_pain issued one extra SELECT per
    # profile after the three table sweeps below, making a no-network regate progressively query-bound.
    _epw, _iv, _wpt, _pnl = {}, {}, {}, {}
    for a, nf, om, cm, npnl, mnotl in db.execute(
            "SELECT addr,n_fills,open_ms,close_ms,net_pnl,max_notl FROM episode"):
        if nf is not None:
            _epw.setdefault(a, []).append(nf)
        if om is not None and cm is not None:
            _iv.setdefault(a, []).append((om, cm))
        if npnl is not None:
            _pnl.setdefault(a, []).append(npnl)
            if npnl > 0 and mnotl is not None and mnotl > 0:
                _wpt.setdefault(a, []).append(npnl / mnotl * 100)
    p90fe = {a: sorted(xs)[min(len(xs) - 1, int(len(xs) * 0.9))] for a, xs in _epw.items() if xs}
    # peak concurrent positions per wallet (sweep line over each episode's [open,close]) — the too_many_concurrent
    # gate. Computed HERE from the episode table (not a stored col) so regate applies the SAME gate as a scan.
    def _peakc(ivs):
        evts = sorted([(o, 1) for o, _c in ivs] + [(_c, -1) for _o, _c in ivs], key=lambda x: (x[0], x[1]))
        cur = pk = 0
        for _, d in evts:
            cur += d; pk = max(pk, cur)
        return pk
    concw = {a: _peakc(v) for a, v in _iv.items()}
    # win_pt (median winning per-trade % on notional) from the episode table → audit metric (same as scan)
    winptw = {a: sorted(v)[len(v) // 2] for a, v in _wpt.items() if v}
    n_active = 0
    for r in rows:
        (addr, old, n_tr, n_fills, perp_frac, last_fill, net, roi_eq, mdd, acct, age, ta, liqw,
         ad, ar, meps, avgnotl, pdr, conc, skew, uw, mxadds, mdadds, wloss, mhold, wr,
         roi_tot, oloss, owin, bagn, bagd, liqc, hedge, net30, netlife, old_reason,
         wkroi, moroi, alroi, pf_turn, pf_mpnl, pf_mvlm, pf_wpnl, pf_eq, pay, pf_wvlm,
         copy_net, copy_wr, copy_closed, copy_open_fill_rate, copy_liqs, copy_fee,
         copy14_net, copy14_closed, copy7_net, copy7_closed, sector_copy_json, sector_policy_json) = r
        m = {"n_trades": n_tr or 0, "n_fills": n_fills or 0, "perp_frac": perp_frac or 0.0, "last_fill_ms": last_fill or 0,
             "net_pnl": net or 0.0, "roi_equity": roi_eq or 0.0, "max_drawdown": mdd or 0.0,
             "acct_value": acct or 0.0, "age_days": age, "times_active": ta or 0,
             "liq_worst_pct": liqw or 0.0, "active_days": ad or 0, "activity_ratio": ar or 0.0,
             "median_eps": meps or 0.0, "avg_notional": avgnotl or 0.0, "pos_day_ratio": pdr or 0.0, "profit_conc": conc or 0.0,
             "hold_skew": skew or 0.0, "open_underwater": uw or 0.0, "median_hold_s": mhold,
             "win_rate": wr or 0.0, "max_adds_per_ep": mxadds or 0, "median_adds_per_ep": mdadds or 0,
             "p90_fills_ep": p90fe.get(addr, 0),   # p90 single-episode fills → algo-slicer gate (from episode table)
             "max_concurrent": concw.get(addr, 0), # peak simultaneous positions → too_many_concurrent gate
             "win_pt": winptw.get(addr, 0.0),       # median winning per-trade % → audit metric
             "worst_loss_pct": wloss or 0.0,
             # v4 open-position character (stored from the last scan; regate doesn't re-fetch live state)
             "roi_total": roi_tot if roi_tot is not None else (roi_eq or 0.0),
             "open_loss_frac": oloss or 0.0, "open_win_frac": owin or 0.0,
             "bag_count": bagn or 0, "max_bag_days": bagd or 0.0, "liq_count": liqc or 0,
             "hedge_ratio": hedge or 0.0,
             # v6 nets: None when scanned before this datum existed → net gates skip (safe pre-rescan)
             "net_30d": net30, "net_life": netlife,
             # HL return-on-capital windows (from leaderboard join) → score() ROI pillar. None → weight-renormalized.
             "week_roi": wkroi, "mon_roi": moroi, "all_roi": alroi,
             # v7 portfolio net metrics → turnover/edge gates + net-ROI pillar (None on profiles scanned pre-v7 → skip)
             "pf_turnover": pf_turn, "pf_mon_pnl": pf_mpnl, "pf_mon_vlm": pf_mvlm,
             "pf_week_pnl": pf_wpnl, "pf_equity": pf_eq,
             # v9: payoff (大亏小赚 gate) + week vlm (edge-decay gate) — MUST be here or regate skips both
             # gates the scan applies, silently re-activating wallets the scan rejected (the 128 vs 165 bug).
             "payoff_ratio": pay, "pf_week_vlm": pf_wvlm,
             "copy_bt_net_pnl": copy_net, "copy_bt_win_rate": copy_wr,
             "copy_bt_closed_n": copy_closed, "copy_bt_open_fill_rate": copy_open_fill_rate,
             "copy_bt_liquidations": copy_liqs, "copy_bt_fee_drag": copy_fee,
             "copy_bt_14d_net_pnl": copy14_net, "copy_bt_14d_closed_n": copy14_closed,
             "copy_bt_7d_net_pnl": copy7_net, "copy_bt_7d_closed_n": copy7_closed,
             "sector_copy_json": sector_copy_json, "sector_policy_json": sector_policy_json}
        # realized loss-asymmetry from the STORED episodes (no network) — works even for profiles scanned
        # before loss_pain existed, so a regate alone re-ranks 小赚大亏 wallets without a full re-scan.
        m["loss_pain"] = metrics.loss_pain(_pnl.get(addr, ()))
        ok, reason = metrics.gates_structural(m, p)
        if ok:
            ok, reason = metrics.gates_state(m, now, p)        # uses the stored open-position metrics
        if ok:
            replay_fills = _copy_bt_cached_fills(db, addr, now, p)
            copy_results = _copy_bt_results(addr, replay_fills, now, p)
            sector_results = _sector_copy_bt_results(addr, replay_fills, now, p)
            ok, reason = _apply_sector_copy_bt_gate(
                m, copy_results, sector_results, p,
                previous_policy=sector_policy_json,
            )
            try:
                current_policy = json.loads(m.get("sector_policy_json") or "{}")
            except (TypeError, ValueError):
                current_policy = {}
            allowed_sectors = set(current_policy.get("allowed") or [])
            evidence_results = copy_results
            if allowed_sectors and allowed_sectors != {"crypto", "stock"}:
                allowed_fills = [
                    x for x in replay_fills if classify_coin(x.get("coin")) in allowed_sectors
                ]
                evidence_results = _copy_bt_results(addr, allowed_fills, now, p)
            _copy_profile_evidence(m, evidence_results, p, addr=addr, now_ms=now)
            qualification = follow_score.evaluate_follow_eligibility(
                {
                    **m,
                    "copy_bt_data_status": m.get("data_status"),
                    "copy_bt_evidence_status": m.get("evidence_status"),
                },
                min_closed30=getattr(p, "evidence_min_trades", config.EVIDENCE_MIN_TRADES),
                min_evidence_days=getattr(p, "evidence_min_days", config.EVIDENCE_MIN_DAYS),
                min_expected_return=getattr(
                    p, "copy_min_expected_margin_return", config.COPY_MIN_EXPECTED_MARGIN_RETURN
                ),
            )
            if not qualification.get("eligible") and not qualification.get("deferred"):
                ok, reason = False, qualification.get("status") or "copy_unqualified"
        score = metrics.score(m) if ok else 0.0
        if ok and score < getattr(p, "min_active_score", config.MIN_ACTIVE_SCORE):
            ok, reason, score = False, "low_quality", 0.0
        # A cached/manual regate may retire an old Active, but cannot reactivate
        # a rejected wallet without a fresh network generation.
        status = ("active" if ok else "retired") if old == "active" else old
        db.execute(
            "UPDATE profile SET status=?,reason=?,score=?,loss_pain=?,max_concurrent=?,win_pt=?,"
            "copy_bt_net_pnl=?,copy_bt_win_rate=?,copy_bt_closed_n=?,copy_bt_open_fill_rate=?,"
            "copy_bt_liquidations=?,copy_bt_fee_drag=?,copy_bt_14d_net_pnl=?,copy_bt_14d_closed_n=?,"
            "copy_bt_7d_net_pnl=?,copy_bt_7d_closed_n=?,sector_copy_json=?,sector_policy_json=?,"
            "copy_expected_return=?,copy_return_lcb=?,copy_return_volatility=?,copy_positive_probability=?,"
            "copy_evidence_days=?,copy_recent_return_14d=?,copy_recent_return_7d=?,copy_risk_score=?,"
            "execution_score=?,model_coverage=?,oos_net_pnl=?,oos_max_drawdown=?,oos_cvar95=?,"
            "actionable_open_rate=?,capacity_fit=?,evidence_status=? WHERE addr=?",
            (status, reason, score, m["loss_pain"], concw.get(addr, 0), winptw.get(addr, 0.0),
             m.get("copy_bt_net_pnl"), m.get("copy_bt_win_rate"), m.get("copy_bt_closed_n"),
             m.get("copy_bt_open_fill_rate"), m.get("copy_bt_liquidations"), m.get("copy_bt_fee_drag"),
             m.get("copy_bt_14d_net_pnl"), m.get("copy_bt_14d_closed_n"),
             m.get("copy_bt_7d_net_pnl"), m.get("copy_bt_7d_closed_n"),
             m.get("sector_copy_json"), m.get("sector_policy_json"),
             m.get("copy_expected_return"), m.get("copy_return_lcb"), m.get("copy_return_volatility"),
             m.get("copy_positive_probability"), m.get("copy_evidence_days"),
             m.get("copy_recent_return_14d"), m.get("copy_recent_return_7d"),
             m.get("copy_risk_score"), m.get("execution_score"), m.get("model_coverage"),
             m.get("oos_net_pnl"), m.get("oos_max_drawdown"), m.get("oos_cvar95"),
             m.get("actionable_open_rate"), m.get("capacity_fit"), m.get("evidence_status"),
             addr),
        )
        n_active += 1 if ok else 0
    db.commit()
    def _record_regate_profile_audit():
        pipeline_audit.record_profile_snapshot(db, stamp, source)

    if auto_tune_enabled:
        n = refresh_watchlist_and_auto_tune(
            db,
            stamp,
            source=source,
            before_auto_tune=_record_regate_profile_audit,
        )
    else:
        _record_regate_profile_audit()
        n = refresh_watchlist(db, stamp, source=source)
    if not quiet:
        print(f"regate: {n_active} active / {len(rows)} profiles  ->  watchlist {n}")
    return n


# ----------------------------------------------------------------------------- scan
def scan(db, p) -> None:
    now_ms = int(time.time() * 1000)
    started, t0 = now_iso(), time.time()
    stamp = now_iso()
    start_ms = now_ms - p.days * 86400_000
    if selection.latest_published_generation(db) is None:
        ensure_watchlist_current(db, stamp)

    # dashboard: advertise we're scanning + consume any operator-queued rescan command
    rescan_rows = db.execute(
        "SELECT id, payload_json FROM commands WHERE status='pending' AND type='rescan'").fetchall()
    rescan_ids = [r[0] for r in rescan_rows]
    for cid in rescan_ids:
        db.execute("UPDATE commands SET status='acked',acked_at=? WHERE id=?", (now_iso(), cid))
    db.commit()
    # a rescan command may request a FULL sweep (dashboard 全量 checkbox) via its payload → re-profile
    # EVERYONE (not just the daily active+new tier); picked up by p.full_scan at the workset split below.
    for _, pj in rescan_rows:
        if _payload_requests_full(pj):
            p.full_scan = True
    # MANUAL (dashboard button → pending rescan command) vs AUTO (24h schedule, no command). The frontend
    # locks the page ONLY for manual scans; the auto scan runs SILENTLY in the background (it must be slow
    # since the observer owns the rate budget, so locking the UI for its full duration is unacceptable).
    manual = bool(rescan_ids)
    for tbl, col, default in (("scan_progress", "manual", 0), ("scan_runs", "full", 0),
                              ("scan_runs", "profiled", 0), ("scan_runs", "failed", 0),
                              ("scan_runs", "complete", 1)):
        try:
            db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} INTEGER DEFAULT {default}"); db.commit()
        except Exception:  # noqa: BLE001 — column already exists
            pass
    run_full = bool(getattr(p, "full_scan", False) or not config.INCREMENTAL_SCAN)
    generation_id = generation.begin_generation(
        db,
        source="scan",
        started_at=started,
        workset_mode="all" if run_full else "priority",
        fill_mode="full_refetch" if run_full else "mixed",
    )
    p.scan_generation = generation_id
    db.commit()
    _set_scanner_proc(db, "scanning", {"phase": "harvest"})
    _set_scan_progress(db, state="scanning", started_at=started, stage="scan_leaderboard",
                       candidates_scanned=0, candidates_total=0, manual=1 if manual else 0)
    p.copy_bt_sigmas = _copy_bt_sigmas(db)
    p.copy_bt_market_ctx = _copy_bt_market_ctx(db)
    p.copy_bt_overrides = _copy_bt_overrides(db)
    rest.reset_request_stats()

    try:
        universe = rest.copyable_universe()       # crypto perps + transparent builder (stocks/commodities)
        if not universe:
            raise RuntimeError("copyable_universe_unavailable")
        if not p.no_harvest:
            print("harvest leaderboard -> staging ...", flush=True)
            n_cand = harvest(db, p, generation_id=generation_id)
        else:
            n_cand = _stage_existing_leaderboard(db, generation_id)
        print(f"  generation {generation_id} · {n_cand} staged candidates", flush=True)
    except Exception as exc:  # noqa: BLE001 - old published selection remains authoritative
        db.rollback()
        generation.fail_generation(db, generation_id, str(exc))
        old_core = selection.published_core_addrs(db) or []
        _record_run(db, started, t0, 0, 0, 0, 0, 0, 0, len(old_core),
                    full=run_full, failed=1, complete=False)
        _set_scan_progress(db, state="idle", stage="error", candidates_scanned=0, candidates_total=0)
        _set_scanner_proc(db, "idle", {"last_error": str(exc)[:300], "active": len(old_core)})
        _resolve_rescan_commands(
            db, rescan_ids, run_full=run_full, complete=False, failed=1, active=len(old_core)
        )
        db.commit()
        print(f"scan generation rejected before profiling: {exc}", flush=True)
        return

    order = {"mon_roi": "mon_roi", "week_roi": "week_roi", "mon_pnl": "mon_pnl"}.get(
        getattr(p, "order", "mon_roi"), "mon_roi"
    )
    cand = [r[0] for r in db.execute(
        f"SELECT addr FROM leaderboard_staging WHERE generation=? AND is_candidate=1 "
        f"ORDER BY {order} DESC",
        (generation_id,),
    ).fetchall()]
    active_addrs = [r[0] for r in db.execute("SELECT addr FROM profile WHERE status='active'").fetchall()]
    profiled = {r[0] for r in db.execute("SELECT addr FROM profile").fetchall()}
    current_selection_generation = selection.latest_published_generation(db)
    core_addrs = selection.published_core_addrs(db) or []
    challenger_addrs = []
    if current_selection_generation:
        challenger_addrs = [r[0] for r in db.execute(
            "SELECT addr FROM follow_selection WHERE generation=? AND role='challenger' AND enabled=1",
            (current_selection_generation,),
        ).fetchall()]
    # vNext adds seven warm-up days to Copy replay.  Only wallets that already produced Copy evidence
    # need the one-time 37-day backfill; front-funnel structural rejects remain incremental.
    warmup_backfill_addrs = _copy_warmup_backfill_addrs(
        db, now_ms - config.PROFILE_FETCH_DAYS * 86400_000,
    )
    challenger_addrs = list(dict.fromkeys(challenger_addrs + warmup_backfill_addrs))
    position_addrs = sorted({
        (addr or "").lower()
        for table in ("copy_position", "shadow_position")
        for (addr,) in db.execute(f"SELECT DISTINCT addr FROM {table} WHERE status='open'").fetchall()
    })
    cand_set = set(cand)
    off_list_active = [addr for addr in active_addrs if addr not in cand_set]
    near_threshold = [r[0] for r in db.execute(
        "SELECT addr FROM profile WHERE status!='active' ORDER BY score DESC,addr LIMIT 1000"
    ).fetchall()]
    priority_n = len(set(position_addrs) | set(core_addrs) | set(active_addrs)
                     | set(challenger_addrs) | set(off_list_active))
    recent = db.execute(
        "SELECT duration_s,COALESCE(profiled,probed_new) FROM scan_runs "
        "WHERE COALESCE(profiled,probed_new)>0 AND complete=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    estimated_profile_s = max(1.0, min(120.0, (f(recent[0]) / int(recent[1])))) if recent else 12.0
    if run_full:
        scheduler_limit = max(priority_n, int(getattr(p, "limit", 0) or len(cand) + priority_n))
        time_budget = None
    else:
        daily_cap = int(getattr(p, "daily_profile_budget", config.DAILY_PROFILE_BUDGET) or config.DAILY_PROFILE_BUDGET)
        cli_cap = int(getattr(p, "limit", daily_cap) or daily_cap)
        scheduler_limit = priority_n + min(daily_cap, cli_cap)
        total_min = float(getattr(p, "daily_scan_time_budget_min", config.DAILY_SCAN_TIME_BUDGET_MIN))
        reserve_min = float(getattr(p, "scan_finalize_reserve_min", config.SCAN_FINALIZE_RESERVE_MIN))
        time_budget = ScanTimeBudget(t0, total_s=total_min * 60.0, finalize_reserve_s=reserve_min * 60.0)
    workset_info = schedule_profile_workset(
        cand,
        active_addrs=active_addrs,
        core_addrs=core_addrs,
        challenger_addrs=challenger_addrs,
        off_list_active_addrs=off_list_active,
        position_addrs=position_addrs,
        profiled_addrs=profiled,
        near_threshold_addrs=near_threshold,
        exploration_addrs=cand,
        limit=scheduler_limit,
        budget=time_budget,
        estimated_profile_s=estimated_profile_s,
        shard_count=int(getattr(p, "full_refresh_shards", config.FULL_REFRESH_SHARDS)),
        exploration_seed=generation_id,
        full_scan=run_full,
    )
    migration_backfill = set(warmup_backfill_addrs) & set(workset_info["workset"])
    if migration_backfill:
        refresh = workset_info["refresh"]
        # On the migration scan, "all" still means all profiles are reevaluated; it no longer means
        # wasting a 37-day network refetch on structural rejects that never reached Copy replay.
        if run_full:
            refresh["full_refetch"] = sorted(migration_backfill)
        else:
            refresh["full_refetch"] = sorted(set(refresh["full_refetch"]) | migration_backfill)
        workset_info["fill_mode"] = "mixed"
    pipeline_audit.record_workset_summary(db, stamp, "scan", workset_info)
    generation.record_workset(
        db,
        generation_id,
        workset_mode=workset_info["workset_mode"],
        fill_mode=workset_info["fill_mode"],
        full_refresh_shard=workset_info["refresh"]["shard_index"],
        workset_n=len(workset_info["workset"]),
        deferred_n=workset_info["counts"]["deferred"],
        metrics={"estimatedProfileSec": estimated_profile_s,
                 "warmupBackfillDue": len(warmup_backfill_addrs),
                 "warmupBackfillScheduled": len(migration_backfill)},
    )
    db.commit()
    workset, mode = workset_info["workset"], workset_info["mode"]
    off_active_n = len([a for a in active_addrs if a not in cand_set])
    full_refetch = set(workset_info["refresh"]["full_refetch"])
    priority_addrs = set(workset[:workset_info["counts"]["priority"]])
    _set_scan_progress(db, stage="fetch_history", candidates_total=len(workset))
    _pace = config.MIN_POST_INTERVAL   # live adaptive pace (fast when no copy-trading, slow trickle when observer up)
    print(f"scan: {mode} · {len(workset)} wallets (incl {off_active_n} off-list actives), "
          f"{p.days}d window, pace {_pace:g}s/req ({'FULL-SPEED 无跟单' if _pace <= config.SCAN_IDLE_INTERVAL else '慢采·跟单进行中'})\n")

    # bulk pre-fetch prior profiles + lb account values once, so the worker threads never read the DB
    cols = storage.PROFILE_COLS.split(",")
    priors = {r[0]: dict(zip(cols, r)) for r in
              db.execute(f"SELECT {storage.PROFILE_COLS} FROM profile").fetchall()}
    lbs = {
        a: {"account_value": av, "week_roi": wr, "mon_roi": mr, "all_roi": ar}
        for a, av, wr, mr, ar in db.execute(
            "SELECT addr,account_value,week_roi,mon_roi,all_roi "
            "FROM leaderboard_staging WHERE generation=?",
            (generation_id,),
        ).fetchall()
    }

    added = retired = rejected = kept = failed = profiled_ok = deferred_profiles = valid_profiles = 0
    profiled_addrs = []
    workers = max(1, getattr(p, "workers", 8))      # I/O-bound; the REST pacer still caps total rate

    def _work(addr):
        prior = priors.get(addr)
        return addr, prior, _profile_one(
            db, addr, start_ms, now_ms, p, prior, lbs.get(addr, {}), stamp, universe,
            force_full=addr in full_refetch,
        )

    done = 0
    priority_done_at = time.time() if not priority_addrs else None
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in concurrent.futures.as_completed([ex.submit(_work, a) for a in workset]):
            done += 1
            try:
                addr, prior, (status, reason, m, hit_cap) = fut.result()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  [{done}/{len(workset)}] FAIL: {exc}")
                continue
            profiled_ok += 1
            profiled_addrs.append(addr)
            priority_addrs.discard(addr)
            if not priority_addrs and priority_done_at is None:
                priority_done_at = time.time()
            data_status = m.get("data_status")
            if data_status == "deferred_data_error":
                deferred_profiles += 1
            elif data_status == "rejected":
                rejected += 1
            else:
                valid_profiles += 1
            if data_status == "deferred_data_error":
                pass
            elif status == "active":              # per-wallet detail is in the profile table, not the log
                if (prior or {}).get("status") == "active":
                    kept += 1
                else:
                    added += 1
            elif status == "retired":
                retired += 1
            elif data_status != "rejected":
                rejected += 1
            # progress on EVERY completion (single cheap 1-row UPDATE) so the mask's xxx/yyy moves
            # smoothly (~1 wallet/sec) instead of jumping every 10 (~14s frozen gaps → looked stuck).
            _set_scan_progress(db, stage="score_filter", candidates_scanned=done)
            if done % 10 == 0:
                _set_scanner_proc(db, "scanning", {"stage": "score_filter",   # refresh heartbeat so the
                                  "scanned": done, "total": len(workset)})    # dashboard isn't "心跳超时"

    complete = failed == 0
    published = False
    previous_core = selection.published_core_addrs(db) or []
    n_active = len(previous_core)
    pipeline_audit.record_profile_snapshot(db, stamp, "scan", profiled_addrs)
    if complete:
        _set_scan_progress(db, stage="rebuild_watchlist", candidates_scanned=len(workset))
        selection_mode = str(
            params.get(db, "FOLLOW_SELECTION_MODE", config.FOLLOW_SELECTION_MODE) or "auto"
        ).lower()
        # Discover the fills-only finalist in a rolled-back staging pass, then fetch its shared market path
        # before the atomic publication transaction. Network I/O must never hold the Dashboard/Observer
        # SQLite writer lock, and a partial watchlist must never become visible.
        if selection_mode == "auto":
            try:
                db.commit()
                refresh_watchlist(
                    db, stamp, source="scan", update_follow_line=False, update_follow_history=False,
                    leaderboard_generation=generation_id, commit=False,
                )
                preview_rows, _ = _build_explicit_selection(
                    db, generation_id, stamp, now_ms, validate_price_path=False,
                )
                preview_core = sorted({
                    row.addr for row in preview_rows if row.role == selection.CORE and row.enabled
                })
                db.rollback()
                if preview_core:
                    from . import price_path
                    path_start = now_ms - (
                        30 + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7))
                    ) * 86_400_000
                    preview_fills = load_copyable_fills(db, preview_core, path_start)
                    preview_follow = params.load_follow(db)
                    if "SMART_ADD" in preview_follow:
                        preview_follow["ADD_STRATEGY"] = (
                            "smart" if preview_follow["SMART_ADD"] else "hardcap"
                        )
                    auto_tune.prepare_refined_price_path(
                        db, preview_fills, path_start, now_ms,
                        sigmas=auto_tune._load_sigmas(db), overrides=preview_follow,
                        market_ctx=auto_tune._load_market_ctx(db),
                    )
            except Exception as exc:  # noqa: BLE001 - final pass safely retains prior Core without coverage
                db.rollback()
                print(f"selection price-path prefetch unavailable: {exc}", flush=True)
        try:
            # Selection reads final scores and per-wallet sector policies from watchlist.  Rebuild that
            # derived view first; otherwise newly-qualified wallets have no policy row and the canonical
            # portfolio loader correctly filters all of their fills, fabricating zero marginal profit.
            refresh_watchlist(
                db,
                stamp,
                source="scan",
                update_follow_line=False,
                update_follow_history=False,
                leaderboard_generation=generation_id,
                commit=False,
            )
            if selection_mode == "manual":
                selection_rows = selection.current_selection_rows(db)
                marginal = None
            else:
                selection_rows, marginal = _build_explicit_selection(db, generation_id, stamp, now_ms)
            generation.mark_generation_ready(
                db,
                generation_id,
                profile_total=profiled_ok,
                profile_valid=valid_profiles,
                profile_deferred=deferred_profiles,
                profile_rejected=rejected,
                profile_complete=True,
            )
            selection.replace_selection_rows(db, generation_id, selection_rows, selected_at=stamp)
            for row in selection_rows:
                pipeline_audit._insert_event(
                    db,
                    stamp=stamp,
                    source="scan",
                    stage="selection",
                    addr=row.addr,
                    status=row.role,
                    reason=row.reason,
                    follow_score=row.utility,
                    payload={
                        "generation": generation_id,
                        "dataStatus": row.data_status,
                        "evidenceStatus": row.evidence_status,
                    },
                )
            pipeline_audit._insert_event(
                db,
                stamp=stamp,
                source="scan",
                stage="selection_summary",
                status="ok",
                reason=("manual_selection_preserved" if selection_mode == "manual"
                        else "explicit_core_selection"),
                payload={
                    "generation": generation_id,
                    "mode": selection_mode,
                    "action": marginal.action if marginal else "keep",
                    "search": marginal.search_meta if marginal else None,
                    "evaluated": marginal.evaluated if marginal else 0,
                    "core": sum(1 for row in selection_rows if row.role == selection.CORE and row.enabled),
                    "challenger": sum(1 for row in selection_rows if row.role == selection.CHALLENGER),
                    "exitOnly": sum(1 for row in selection_rows if row.role == selection.EXIT_ONLY),
                },
            )
            generation.publish_generation(db, generation_id, published_at=stamp)
            current_core = _record_explicit_follow_history(
                db, selection_rows, stamp, previous_core, generation_id,
            )
            n_active = len(current_core)
            stage_metrics = {
                "durationSec": round(time.time() - t0, 3),
                "coreRefreshSec": round((priority_done_at or time.time()) - t0, 3),
                "coreDeadlineMet": ((priority_done_at or time.time()) - t0)
                <= float(getattr(p, "core_refresh_deadline_min", config.CORE_REFRESH_DEADLINE_MIN)) * 60.0,
                "profileValid": valid_profiles,
                "profileDeferred": deferred_profiles,
                "profileFailed": failed,
                "deltaRefetch": len(workset) - len(full_refetch),
                "fullRefetch": len(full_refetch),
                "selectionCore": n_active,
                "selectionChallenger": sum(1 for row in selection_rows if row.role == selection.CHALLENGER),
                "selectionAction": marginal.action if marginal else "keep",
                "selectionEvaluated": marginal.evaluated if marginal else 0,
                "selectionSearch": marginal.search_meta if marginal else None,
                **rest.request_stats(),
            }
            db.execute(
                "UPDATE scan_generation SET metrics_json=? WHERE generation=?",
                (json.dumps(stage_metrics, sort_keys=True), generation_id),
            )
            db.commit()
            published = True
        except Exception as exc:  # noqa: BLE001 - rollback restores old watchlist/selection atomically
            db.rollback()
            generation.fail_generation(db, generation_id, f"finalize_error:{exc}")
            db.commit()
            complete = False
            failed += 1
            print(f"generation finalize failed; old selection retained: {exc}", flush=True)
    else:
        generation.mark_generation_ready(
            db,
            generation_id,
            profile_total=profiled_ok,
            profile_valid=valid_profiles,
            profile_deferred=deferred_profiles,
            profile_rejected=rejected,
            profile_complete=False,
        )
        db.commit()

    if published:
        _set_scan_progress(db, stage="auto_tune", candidates_scanned=len(workset))
        launch = _launch_async_tuner(db, generation_id, stamp)
        pipeline_audit._insert_event(
            db,
            stamp=stamp,
            source="scan",
            stage="tuner_launch",
            status=launch.get("status"),
            reason=launch.get("reason") or "generation_bound_async",
            payload=launch,
        )
        db.commit()
    _set_scan_progress(db, stage="persist")
    _record_run(db, started, t0, n_cand, profiled_ok, added, retired, kept, rejected, n_active,
                full=run_full, failed=failed, complete=complete)
    try:
        if not published:
            raise RuntimeError("generation_not_published")
        pruned = _prune_discovery_cache(db)
        pipeline_audit.record_prune_summary(db, stamp, "scan", pruned)
        db.commit()
        if any(pruned.values()):
            print(f"pruned discovery cache: {pruned}", flush=True)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"prune discovery cache skipped: {exc}", flush=True)
    print(f"\nscan done in {time.time()-t0:.0f}s: +{added} new, -{retired} retired, {kept} kept, "
          f"{rejected} rejected, {deferred_profiles} deferred, {failed} failed. Core now: {n_active}.", flush=True)
    # Dashboard: resolve only requests this completed run actually satisfied. A full request arriving
    # during an incremental run is stronger than the current work and must be reported as retryable failure.
    _set_scan_progress(db, state="idle", candidates_scanned=len(workset))
    _set_scanner_proc(db, "idle", {"last_scan_at": now_iso(), "active": n_active})
    _resolve_rescan_commands(
        db, rescan_ids, run_full=run_full, complete=published, failed=failed, active=n_active
    )
    db.commit()


# ------------------------------------------------------------------------ watchlist
def watchlist(db, top: int) -> None:
    """Show OUR curated tiny leaderboard (the watchlist table)."""
    rows = db.execute(
        "SELECT w.rank,w.addr,w.score,w.roi_equity,w.mon_roi,w.win_rate,w.max_drawdown,w.acct_value,"
        "w.lev_proxy,w.margin_type,w.cur_leverage,w.liq_worst_pct,w.taker_frac,w.median_hold_s,"
        "w.age_days,w.times_active,w.top_coin,w.display_name,COALESCE(c.enabled,1),"
        "COALESCE(p.max_adds_per_ep,0),COALESCE(p.worst_loss_pct,0) "
        "FROM watchlist w LEFT JOIN target_controls c ON c.addr=w.addr "
        "LEFT JOIN profile p ON p.addr=w.addr ORDER BY w.rank LIMIT ?",
        (top,)).fetchall()
    print(f"\nWATCHLIST — {len(rows)} crypto-perp targets (core=consistent profit+survival; "
          f"lev/margin/liq are OBSERVED context, we copy isolated per-trade w/ our own cap)\n"
          f"  grid = most scale-ins in one round-trip (gated); wLoss = worst single round-trip loss "
          f"(deep = 扛单到爆, shallow = 及时止损)\n")
    hdr = (f"{'#':>2} {'addr':42} {'on':>2} {'score':>6} {'roiEq':>7} {'monRoi':>7} {'win':>4} {'maxDD%':>6} "
           f"{'lev':>5} {'taker':>5} {'hold':>6} {'age':>5} {'seen':>4} {'grid':>5} {'wLoss':>6} {'coin':>6}")
    print(hdr); print("-" * len(hdr))
    for (rank, addr, sc, roi_eq, mon_roi, win, dd, acct, lev, mtype, curlev, liqw, taker, hold,
         age, ta, coin, name, on, grid, wloss) in rows:
        ddp = (dd / acct * 100) if acct else 0
        levshow = curlev if curlev else (lev or 0)
        flag = f"{grid:>4}!" if grid >= 10 else f"{grid:>5}"   # ! marks a likely grid/DCA wallet
        print(f"{rank:>2} {addr:42} {'Y' if on else 'n':>2} {sc:>6.1f} {roi_eq*100:>+6.1f}% "
              f"{(mon_roi or 0)*100:>+6.1f}% {win*100:>3.0f}% {ddp:>5.1f}% {levshow:>4.1f}x "
              f"{taker*100:>4.0f}% {hold/3600:>5.1f}h "
              f"{age or 0:>4.0f}d {ta:>4} {flag:>5} {(wloss or 0)*100:>+5.1f}% {coin or '':>6}  {name or ''}")
