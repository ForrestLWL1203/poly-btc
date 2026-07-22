"""Discovery domain: the rolling scanner that maintains the live watchlist.

harvest leaderboard -> coarse candidates -> profile work-set (actives + new + top rechecks)
over a short window -> perp episodes/metrics -> upsert active/rejected/retired.
Composes rest + fills + metrics + storage; holds no infra of its own.
"""
import calendar
import concurrent.futures
from dataclasses import replace
import hashlib
import json
import math
import os
import threading
import time
from types import SimpleNamespace

from hyper import config, params, storage
from hyper.copy.copy_backtest import ADD_METRICS_VERSION, run_backtest, slice_backtest_result
from hyper.copy.fills import build_episodes
from hyper.copy.copy_data import (
    is_copyable_coin,
    load_copyable_fills,
    normalize_copyable_fills,
    out_of_scope_fills,
)
from hyper.copy.copy_policy import COPY_POLICY_PARAM_KEYS, load_copy_policy
from hyper.copy.copy_evidence import summarize_copy_evidence
from hyper.copy.sector import (
    SECTORS,
    apply_allowed_sector_copy_metrics,
    classify_coin,
    compact_sector_results,
)
from hyper.copy.fill_transition import classify_fill_transition
from hyper.market import generation_market, price_path, rest, volatility
from hyper.selection import (
    auto_tune,
    core_formation,
    follow_score,
    offline_core_optimizer,
    state as selection,
    strategy_revision,
)
from . import generation, metrics, perp_prefilter, pipeline_audit
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
    schedule_profile_workset,
    upsert_wallet_registry,
)
from hyper.util import f, now_iso

_db_lock = threading.Lock()   # serializes sqlite writes across scanner worker threads

_SECTOR_RECOVERABLE_STRUCTURE_REASONS = {
    "bot_frequency", "hft_uncopyable", "grid_dca", "heavy_dca",
    "too_many_concurrent",
}
_SECTOR_RECOVERABLE_STATE_REASONS = set()


def _current_sector_structure_policy(perp_fills, now_ms, p, *, source="current_generation"):
    """Build this generation's sector specialization without consulting prior profile state.

    Whole-wallet structure can be contaminated by a disabled specialty (for example stock DCA beside
    clean Crypto trading).  Every scan therefore evaluates each sector from the current fills first.  A
    single complete Heavy-DCA outlier is the only soft structure: it must pass the same capped Copy pressure
    replay used by execution before it can participate in Core formation.
    """
    out = {"source": source}
    for sector in SECTORS:
        fills = [x for x in (perp_fills or []) if classify_coin(x.get("coin")) == sector]
        if not fills:
            out[sector] = {
                "allow": False, "status": "no_sector_evidence", "reason": "本轮无该板块可复制成交",
            }
            continue
        episodes, _open = build_episodes(fills)
        current = metrics.compute_metrics(
            fills, episodes, now_ms, int(getattr(p, "days", 14) or 14),
        )
        if not current:
            out[sector] = {
                "allow": False, "status": "no_sector_evidence", "reason": "本轮该板块结构证据不足",
            }
            continue
        current["perp_frac"] = 1.0
        ok, reason = metrics.gates_structural(current, p)
        raw_payoff = float(current.get("payoff_ratio") or 0.0)
        raw_closed = int(current.get("n_trades") or 0)
        if (
            ok
            and raw_closed >= load_copy_policy().min_closed_30d
            and raw_payoff < load_copy_policy().min_raw_payoff_ratio
        ):
            ok, reason = False, "weak_payoff_structure"
        complete = [episode for episode in episodes if episode.get("open_complete", True)]
        heavy_limit = int(getattr(p, "max_single_adds", config.MAX_SINGLE_ADDS_PER_EP))
        heavy_count = sum(1 for episode in complete if int(episode.get("n_adds") or 0) > heavy_limit)
        one_off_heavy = bool(
            reason == "heavy_dca"
            and heavy_count == 1
            and float(current.get("median_adds_per_ep") or 0) <= float(p.grid_max_adds)
        )
        if one_off_heavy:
            out[sector] = {
                "allow": True,
                "watch": True,
                "coreBlocked": False,
                "status": "heavy_dca_watch",
                "reason": "本轮仅一个完整回合超过Heavy-DCA阈值，进入受限回放压力验证",
                "heavyEpisodeCount": heavy_count,
                "maxAdds": int(current.get("max_adds_per_ep") or 0),
                "medianAdds": int(current.get("median_adds_per_ep") or 0),
                "rawPayoffRatio": raw_payoff,
                "rawClosed": raw_closed,
            }
        else:
            out[sector] = {
                "allow": bool(ok),
                "status": "structural_ok" if ok else str(reason or "structural_unqualified"),
                "reason": "本轮板块结构可复制" if ok else f"本轮板块结构不合格：{reason}",
                "heavyEpisodeCount": heavy_count,
                "maxAdds": int(current.get("max_adds_per_ep") or 0),
                "medianAdds": int(current.get("median_adds_per_ep") or 0),
                "maxConcurrent": int(current.get("max_concurrent") or 0),
                "rawPayoffRatio": raw_payoff,
                "rawClosed": raw_closed,
            }
    out["allowed"] = [sector for sector in SECTORS if (out.get(sector) or {}).get("allow")]
    return out


def _structural_specialization_snapshot(structure):
    """Serializable preliminary policy for profiles stopped before economic Copy replay."""
    structure = structure or {}
    out = {
        sector: dict(structure.get(sector) or {})
        for sector in SECTORS
    }
    out["allowed"] = list(structure.get("allowed") or ())
    out["specializationSource"] = structure.get("source") or "current_generation"
    out["specializationPhase"] = "structural"
    return out


def _episode_rows(addr: str, eps: list) -> list:
    """Rows for episode storage; seq preserves same-ms flip/reopen episodes instead of replacing them."""
    seen = {}
    rows = []
    for e in eps:
        key = (e["coin"], e["open_ms"])
        seq = seen.get(key, 0)
        seen[key] = seq + 1
        rows.append((addr, e["coin"], e["side"], e["open_ms"], seq, e["close_ms"], e["hold_s"],
                     e["net_pnl"], e["fee"], e["max_notl"], e["n_fills"], e["open_px"], e["close_px"],
                     1 if e.get("open_complete", True) else 0))
    return rows


def _load_cached_fills(db, addr, since):
    """Cached in-scope contract fills for addr in the window, defensively normalized."""
    with _db_lock:
        rows = db.execute("SELECT fill_json FROM candidate_fills WHERE addr=? AND time>=? ORDER BY time",
                          (addr, since)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r[0]))
        except (ValueError, TypeError):
            pass
    return normalize_copyable_fills(out, addr=addr)


def _store_cached_fills(db, addr, fills, window_start, *, coverage_complete=False, coverage_end=None,
                        universe=None):
    """Persist only executable Crypto/stock contracts; caller holds ``_db_lock``.

    This is a second fail-closed boundary behind the response-time filter.  A
    future caller cannot accidentally put spot, outcome or private-dex history
    back into the canonical replay cache.
    """
    # Heal rows written by an older release, and rows for a plain perp that has since been delisted.
    # Without this cleanup the publication audit would correctly fail, but could never self-recover on
    # a delta scan because an immutable stale row would remain in the cache forever.
    cached = db.execute(
        "SELECT tid,fill_json FROM candidate_fills WHERE addr=?", (addr,),
    ).fetchall()
    invalid_tids = []
    for tid, payload in cached:
        try:
            row = json.loads(payload)
        except (TypeError, ValueError):
            invalid_tids.append(tid)
            continue
        if not is_copyable_coin(row.get("coin"), universe=universe):
            invalid_tids.append(tid)
    if invalid_tids:
        db.executemany(
            "DELETE FROM candidate_fills WHERE addr=? AND tid=?",
            [(addr, tid) for tid in invalid_tids],
        )

    scoped = normalize_copyable_fills(fills, addr=addr, universe=universe)
    rows = [(addr, x.get("tid"), x["time"], json.dumps(x)) for x in scoped if x.get("tid") is not None]
    if rows:
        db.executemany("INSERT OR IGNORE INTO candidate_fills (addr,tid,time,fill_json) VALUES (?,?,?,?)", rows)
    db.execute("DELETE FROM candidate_fills WHERE addr=? AND time<?", (addr, window_start))
    if coverage_complete:
        db.execute(
            "INSERT INTO fill_cache_state(addr,coverage_start_ms,coverage_end_ms,backfill_start_ms,"
            "backfill_cursor_ms,updated_at) VALUES (?,?,?,NULL,NULL,?) "
            "ON CONFLICT(addr) DO UPDATE SET coverage_start_ms=MIN(fill_cache_state.coverage_start_ms,excluded.coverage_start_ms),"
            "coverage_end_ms=MAX(COALESCE(fill_cache_state.coverage_end_ms,0),excluded.coverage_end_ms),"
            "backfill_start_ms=NULL,backfill_cursor_ms=NULL,"
            "updated_at=excluded.updated_at",
            (addr, int(window_start), int(coverage_end or window_start), now_iso()),
        )


def _assert_scoped_fill_cache(db, addrs, universe) -> dict:
    """Fail publication if a profiled wallet cache contains an out-of-scope row."""
    owners = sorted({str(addr or "").lower() for addr in addrs or [] if addr})
    audited = invalid = 0
    for offset in range(0, len(owners), 400):
        batch = owners[offset:offset + 400]
        marks = ",".join("?" for _ in batch)
        rows = db.execute(
            f"SELECT fill_json FROM candidate_fills WHERE lower(addr) IN ({marks})",
            batch,
        ).fetchall()
        payloads = []
        for (payload,) in rows:
            audited += 1
            try:
                row = json.loads(payload)
            except (TypeError, ValueError):
                invalid += 1
                continue
            payloads.append(row)
        invalid += len(out_of_scope_fills(payloads, universe=universe))
    if invalid:
        raise RuntimeError(f"market_scope_cache_violation:{invalid}:{audited}")
    return {"audited": audited, "invalid": 0, "scope": ["crypto", "stock"]}


def _copy_warmup_backfill_addrs(db, desired_start_ms):
    """Wallets with real Copy evidence whose cache has never been confirmed to cover the warm-up prefix."""
    return [r[0] for r in db.execute(
        "SELECT p.addr FROM profile p LEFT JOIN fill_cache_state s ON s.addr=p.addr "
        "WHERE (COALESCE(p.copy_bt_closed_n,0)>0 OR p.copy_bt_net_pnl IS NOT NULL) "
        "AND (s.coverage_start_ms IS NULL OR s.coverage_start_ms>?) ORDER BY p.addr",
        (int(desired_start_ms),),
    ).fetchall()]


def _incomplete_fill_cache_addrs(db, addrs, desired_start_ms):
    """Return wallets without a confirmed complete rolling-window source snapshot."""
    owners = sorted({str(addr or "").lower() for addr in addrs if addr})
    if not owners:
        return []
    complete = set()
    for offset in range(0, len(owners), 400):
        batch = owners[offset:offset + 400]
        marks = ",".join("?" for _ in batch)
        complete.update(
            (addr or "").lower() for (addr,) in db.execute(
                f"SELECT addr FROM fill_cache_state WHERE lower(addr) IN ({marks}) "
                "AND coverage_start_ms<=?",
                (*batch, int(desired_start_ms)),
            ).fetchall()
        )
    return [addr for addr in owners if addr not in complete]


def _replace_episode_rows(db, addr: str, eps: list) -> None:
    erows = _episode_rows(addr, eps)
    db.execute("DELETE FROM episode WHERE addr=?", (addr,))
    if erows:
        db.executemany(
            "INSERT OR REPLACE INTO episode "
            "(addr,coin,side,open_ms,seq,close_ms,hold_s,net_pnl,fee,max_notl,n_fills,open_px,close_px,open_complete)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        eps, _open_eps = build_episodes(normalize_copyable_fills(fills, addr=addr))
        if not eps:
            continue
        with _db_lock:
            _replace_episode_rows(db, addr, eps)
        repaired += 1
    if repaired:
        db.commit()
    return repaired


def _copy_bt_cached_fills(db, addr, now_ms, p):
    """Cached copyable fills for regate's no-network copy replay."""
    days = int(getattr(p, "copy_bt_days", config.COPY_BT_DAYS) or config.COPY_BT_DAYS)
    days += int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
    start_ms = now_ms - days * 86400_000
    return normalize_copyable_fills(_load_cached_fills(db, addr, start_ms), addr=addr)


def _fetch_profile_fills(db, addr, window_start, p, full, *, universe=None):
    """Fetch history, then cross the market-scope boundary immediately.

    Hyperliquid's ``userFillsByTime`` has no coin/dex filter.  The returned
    response therefore has to be filtered locally, before persistence and
    before *any* metric sees it.  ``coverage_end_ms`` tracks the source cursor
    independently from the last retained fill, so wallets trading only an
    excluded market do not cause the same payload to be downloaded forever.
    """
    coverage = db.execute(
        "SELECT coverage_start_ms,coverage_end_ms,backfill_start_ms,backfill_cursor_ms "
        "FROM fill_cache_state WHERE addr=?",
        (addr,),
    ).fetchone()
    coverage_complete = bool(coverage and int(coverage[0] or 0) <= int(window_start))
    if not full and coverage_complete:
        stored = normalize_copyable_fills(
            _load_cached_fills(db, addr, window_start), addr=addr, universe=universe,
        )
        cursor = max(
            int(coverage[1] or 0),
            max((int(x["time"]) for x in stored), default=0),
        )
        if cursor is not None:
            delta, hit_cap = rest.fetch_window(addr, max(window_start, cursor - config.POLL_OVERLAP_MS), p.max_pages)
            if not hit_cap:
                scoped_delta = normalize_copyable_fills(delta, addr=addr, universe=universe)
                merged = {x.get("tid"): x for x in stored}
                merged.update({x.get("tid"): x for x in scoped_delta})
                scoped_full = sorted(
                    (x for x in merged.values() if x["time"] >= window_start),
                    key=lambda x: x["time"],
                )
                return scoped_full, False, scoped_delta, False
            # An unexpectedly capped delta becomes a resumable heal instead of repeatedly restarting.
    cached = normalize_copyable_fills(
        _load_cached_fills(db, addr, window_start), addr=addr, universe=universe,
    )
    resume_cursor = int(coverage[3] or 0) if coverage else 0
    resume_start = int(coverage[2] or 0) if coverage else 0
    if not resume_cursor or (resume_start and resume_start > window_start):
        resume_cursor = int(window_start)
        resume_start = int(window_start)
    raw_delta, hit_cap, next_cursor = rest.fetch_window_progress(addr, resume_cursor, p.max_pages)
    scoped_delta = normalize_copyable_fills(raw_delta, addr=addr, universe=universe)
    merged = {x.get("tid"): x for x in cached}
    merged.update({x.get("tid"): x for x in scoped_delta})
    scoped_full = sorted(
        (x for x in merged.values() if x["time"] >= window_start), key=lambda x: x["time"],
    )
    if hit_cap:
        with _db_lock:
            db.execute(
                "INSERT INTO fill_cache_state(addr,backfill_start_ms,backfill_cursor_ms,updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(addr) DO UPDATE SET "
                "backfill_start_ms=excluded.backfill_start_ms,"
                "backfill_cursor_ms=MAX(COALESCE(fill_cache_state.backfill_cursor_ms,0),excluded.backfill_cursor_ms),"
                "updated_at=excluded.updated_at",
                (addr, resume_start, int(next_cursor), now_iso()),
            )
            # Do not carry a write transaction into the caller's potentially expensive metric/replay work.
            # Scanner and Observer intentionally share this WAL database; even a resumable cursor write must
            # release the single SQLite writer slot immediately.
            db.commit()
    return scoped_full, hit_cap, scoped_delta, True


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
    """Attach the cheap discovery decision without mutating the live leaderboard.

    Official ROI remains a ranking/audit input, never a magnitude gate.  The bulk endpoint is used only to
    prove useful account size, leveraged activity and positive PnL in both recent windows before the
    authoritative scoped Copy replay.
    """
    min_acct = getattr(p, "min_acct", config.HARVEST_MIN_ACCT)
    vlm_min = getattr(p, "week_vlm_min", config.HARVEST_WEEK_VLM_MIN)
    roi_min = {
        "week": getattr(p, "week_roi_min", config.HARVEST_WEEK_ROI_MIN),
        "month": getattr(p, "month_roi_min", config.HARVEST_MONTH_ROI_MIN),
        "all": getattr(p, "all_roi_min", config.HARVEST_ALL_ROI_MIN),
    }
    pnl_min = {
        "week": getattr(p, "week_pnl_min", config.HARVEST_WEEK_PNL_MIN),
        "month": getattr(p, "month_pnl_min", config.HARVEST_MONTH_PNL_MIN),
        "all": getattr(p, "all_pnl_min", config.HARVEST_ALL_PNL_MIN),
    }
    prepared = []
    for original in rows or []:
        r = dict(original or {})
        w = {name: perf for name, perf in r.get("windowPerformances", [])}
        wk, mo, al = w.get("week", {}), w.get("month", {}), w.get("allTime", {})
        acct = f(r.get("accountValue"))
        wk_vlm, wk_pnl = f(wk.get("vlm")), f(wk.get("pnl"))
        month_pnl, all_pnl = f(mo.get("pnl")), f(al.get("pnl"))
        week_roi, month_roi, all_roi = f(wk.get("roi")), f(mo.get("roi")), f(al.get("roi"))
        # Retain the old diagnostics so shadow reports can compare the removed ROI policy with the new
        # recall surface.  They intentionally do not participate in ``is_candidate``.
        r["roi_windows_passed"] = sum((
            week_roi >= roi_min["week"], month_roi >= roi_min["month"], all_roi >= roi_min["all"],
        ))
        week_positive = wk_pnl >= pnl_min["week"] if pnl_min["week"] > 0 else wk_pnl > 0
        month_positive = month_pnl >= pnl_min["month"] if pnl_min["month"] > 0 else month_pnl > 0
        r["is_candidate"] = int(
            acct >= min_acct
            and wk_vlm >= vlm_min
            and week_positive
            and month_positive
        )
        r["fetched_at"] = fetched_at
        mon_vlm = f(mo.get("vlm"))
        r["daily_turnover"] = (mon_vlm / acct / 30.0) if acct > 0 else 0.0
        prepared.append(r)
    return prepared


def harvest(db, p, *, generation_id=None) -> int:
    """Leaderboard official ROI + absolute PnL screen; no leveraged-volume efficiency ratio."""
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


def _official_roi_audit(db, generation_id, stamp, p):
    """Record the complete leaderboard decision surface for the generation."""
    pipeline_audit._delete_stage(db, stamp, "scan", "official_roi")
    rows = db.execute(
        "SELECT addr,is_candidate,account_value,week_vlm,week_pnl,week_roi,mon_pnl,mon_roi,all_pnl,all_roi "
        "FROM leaderboard_staging WHERE generation=? ORDER BY mon_roi DESC,addr",
        (generation_id,),
    ).fetchall()
    names = ("addr", "is_candidate", "accountValue", "weekVlm", "weekPnl", "weekRoi",
             "monthPnl", "monthRoi", "allPnl", "allRoi")
    rejected_counts = {}
    for rank, row in enumerate(rows, 1):
        item = dict(zip(names, row))
        passed = bool(item.pop("is_candidate"))
        diagnostics = {
            "week_roi_below_reference": f(item["weekRoi"]) < getattr(p, "week_roi_min", config.HARVEST_WEEK_ROI_MIN),
            "month_roi_below_reference": f(item["monthRoi"]) < getattr(p, "month_roi_min", config.HARVEST_MONTH_ROI_MIN),
            "all_roi_below_reference": f(item["allRoi"]) < getattr(p, "all_roi_min", config.HARVEST_ALL_ROI_MIN),
        }
        week_floor = getattr(p, "week_pnl_min", config.HARVEST_WEEK_PNL_MIN)
        month_floor = getattr(p, "month_pnl_min", config.HARVEST_MONTH_PNL_MIN)
        week_positive = f(item["weekPnl"]) >= week_floor if week_floor > 0 else f(item["weekPnl"]) > 0
        month_positive = f(item["monthPnl"]) >= month_floor if month_floor > 0 else f(item["monthPnl"]) > 0
        checks = {
            "account_value_below_floor": f(item["accountValue"]) < getattr(p, "min_acct", config.HARVEST_MIN_ACCT),
            "week_volume_below_floor": f(item["weekVlm"]) < getattr(p, "week_vlm_min", config.HARVEST_WEEK_VLM_MIN),
            "week_pnl_below_floor": not week_positive,
            "month_pnl_below_floor": not month_positive,
        }
        failed_checks = [reason for reason, failed in checks.items() if failed]
        roi_windows_passed = 3 - sum(bool(value) for value in diagnostics.values())
        item["roiWindowsPassed"] = roi_windows_passed
        item["roiMagnitudeGateEnabled"] = False
        item["roiDiagnostics"] = [reason for reason, failed in diagnostics.items() if failed]
        item["failedChecks"] = failed_checks
        addr = item.pop("addr")
        reason = failed_checks[0] if failed_checks else "discovery_recall_below_floor"
        if passed:
            pipeline_audit._insert_event(
                db, stamp=stamp, source="scan", stage="official_roi", addr=addr, rank=rank,
                status="passed", reason="discovery_recall_passed", payload=item,
            )
        else:
            rejected_counts[reason] = rejected_counts.get(reason, 0) + 1
    for reason, count in sorted(rejected_counts.items()):
        pipeline_audit._insert_event(
            db, stamp=stamp, source="scan", stage="official_roi", status="rejected",
            reason=reason, payload={"count": count},
        )
    db.commit()


def _run_perp_prefilter(db, addrs, p, stamp):
    """Run the authoritative Portfolio precheck for ROI survivors before history collection."""
    pipeline_audit._delete_stage(db, stamp, "scan", "perp_prefilter")
    # The delete starts a SQLite write transaction.  Release it before the first network request: holding the
    # single writer slot across a batch of rate-paced Portfolio calls freezes Observer marks and commands.
    db.commit()
    minima = {
        "week": getattr(p, "week_pnl_min", config.HARVEST_WEEK_PNL_MIN),
        "month": getattr(p, "month_pnl_min", config.HARVEST_MONTH_PNL_MIN),
        "all": getattr(p, "all_pnl_min", config.HARVEST_ALL_PNL_MIN),
    }
    share_min = getattr(p, "perp_pnl_share_min", config.HARVEST_PERP_PNL_SHARE_MIN)
    cache_policy = {
        "version": "three_window_perp_v1",
        "pnlMinima": {key: float(value) for key, value in minima.items()},
        "shareMin": float(share_min),
    }
    addr_set = {str(addr).lower() for addr in addrs}
    cached_results = {}
    # A deployment/restart starts a new generation but does not make Portfolio evidence fetched minutes ago
    # stale. Reuse only exact-policy business decisions inside a short TTL; deferred transport failures are
    # deliberately retried. Every cache hit is copied into the new stamp's audit surface below.
    for addr, status, reason, payload_json in db.execute(
        "SELECT addr,status,reason,payload_json FROM pipeline_audit "
        "WHERE source='scan' AND stage='perp_prefilter' AND addr IS NOT NULL "
        "AND strftime('%s',created_at)>=strftime('%s','now')-? ORDER BY id DESC",
        (int(config.PERP_PREFILTER_CACHE_TTL_S),),
    ).fetchall():
        addr = str(addr or "").lower()
        if addr not in addr_set or addr in cached_results or status not in {"passed", "rejected"}:
            continue
        try:
            cached_payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError):
            continue
        if cached_payload.get("policy") != cache_policy:
            continue
        cached_results[addr] = perp_prefilter.Result(
            str(status), str(reason or "perp_prefilter_cached"), dict(cached_payload.get("windows") or {}),
        )
    results = {}
    pending_audit = []

    def flush_audit():
        for event in pending_audit:
            pipeline_audit._insert_event(db, **event)
        db.commit()
        pending_audit.clear()

    for rank, addr in enumerate(addrs, 1):
        result = cached_results.get(str(addr).lower())
        cache_hit = result is not None
        if result is None:
            try:
                payload = rest.portfolio(addr)
            except Exception as exc:  # noqa: BLE001
                result = perp_prefilter.Result(
                    "deferred_data_error", f"portfolio_error:{type(exc).__name__}", {},
                )
            else:
                result = perp_prefilter.evaluate(payload, pnl_minima=minima, share_min=share_min)
        results[addr] = result
        # Buffer audit values in memory so no write transaction remains open during the next REST call.
        pending_audit.append({
            "stamp": stamp, "source": "scan", "stage": "perp_prefilter", "addr": addr,
            "rank": rank, "status": result.status, "reason": result.reason,
            "payload": {**result.payload(), "policy": cache_policy, "cacheHit": cache_hit},
        })
        if rank % 10 == 0:
            flush_audit()
            _set_scan_progress(db, stage="perp_prefilter", candidates_scanned=rank,
                               candidates_total=len(addrs))
    if pending_audit:
        flush_audit()
    return results


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


def _current_margin_equity_pct(db) -> float:
    return float(params.load_follow(db).get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT))


def _assert_margin_equity_snapshot(db, expected: float) -> None:
    """Fail closed before publication if an operator changed the manual sizing base mid-generation."""
    current = _current_margin_equity_pct(db)
    if abs(current - float(expected)) > 1e-12:
        raise RuntimeError(
            f"margin_equity_pct_changed_during_generation:{float(expected):.6f}:{current:.6f}"
        )


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

    c7, _ = window(7)
    c30, d30 = window(30)
    rate_day = c30 / 30.0
    return {
        "last_copyable_open_ms": opens[-1] if opens else 0,
        "open_events_7d": c7, "open_events_30d": c30,
        # Refined later by the canonical replay once policy/liquidity/capacity skips are known.
        "actionable_open_events_7d": c7, "actionable_open_events_30d": c30,
        "open_days_30d": d30,
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
            margin_equity_pct=getattr(p, "margin_equity_pct", config.MARGIN_EQUITY_PCT),
            policy_values=getattr(p, "copy_bt_overrides", None),
        )
        if not result.get("eligible"):
            if result.get("deferred"):
                return True, "copy_backtest_deferred_data_error"
            return False, result.get("status") or "copy_unqualified"
    last_open = int(m.get("last_copyable_open_ms") or 0)
    max_age_ms = int(getattr(p, "inactive_days", config.INACTIVE_DAYS) * 86_400_000)
    if not last_open or now_ms - last_open > max_age_ms:
        # A mirrored swing episode that is still open is not an inactive wallet.  The target has no
        # reason to emit another flat->open event while it is deliberately carrying the position, and
        # demoting it here would make long-hold winners churn out of Core for precisely following their
        # strategy.  This flag is attached only when the fresh target snapshot still has a material open
        # position, the target's open book is net-profitable, AND our forward-only copy book is also
        # net-profitable for the same wallet. A carried loser must never earn an activity exemption.
        # Economics, recent-loss, structure, valuation and data-integrity gates above remain authoritative.
        if m.get("open_copy_activity_bypass"):
            return True, "ok" if copy_gate_enabled else "copy_gate_disabled"
        return False, "inactive_copyable_open"
    return True, "ok" if copy_gate_enabled else "copy_gate_disabled"


def _attach_open_copy_activity_context(m, addr: str, open_copy_pnl_by_addr) -> bool:
    """Attach the narrow inactivity bypass for a target/copy episode that remains net-profitable."""
    addr = str(addr or "").lower()
    copy_pnl = {
        str(key or "").lower(): f(value)
        for key, value in dict(open_copy_pnl_by_addr or {}).items()
    }.get(addr)
    active = bool(
        int(m.get("material_open_count") or 0) > 0
        and f(m.get("open_unrealized")) > 0.0
        and copy_pnl is not None
        and copy_pnl > 0.0
    )
    m["open_copy_activity_bypass"] = active
    m["open_copy_activity_pnl"] = copy_pnl
    return active


def _finalize_profile_qualification(m, ok: bool, reason: str) -> tuple[bool, str, float]:
    """Attach an allowed-sector score without turning it into another qualification gate."""
    scoped = apply_allowed_sector_copy_metrics(m)
    score = metrics.score(scoped) if ok else 0.0
    m["raw_quality_score"] = score
    return ok, reason, score


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


def _reject_prefilter_profile(db, addr, prior, stamp, generation_id, reason):
    """Publish a hard front-funnel failure so incumbents and pins cannot bypass it."""
    row = dict(prior or {})
    row.update(
        addr=addr,
        status="retired" if prior and prior.get("status") in {"active", "qualified"} else "rejected",
        reason=str(reason or "prefilter_rejected")[:120],
        score=0.0,
        raw_quality_score=0.0,
        data_status="valid",
        evidence_status="ineligible",
        profile_generation=generation_id,
        evaluated_at=stamp,
        times_seen=int((prior or {}).get("times_seen") or 0) + 1,
    )
    cols = storage.PROFILE_COLS.split(",")
    with _db_lock:
        db.execute(
            f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' * len(cols))})",
            [row.get(column) for column in cols],
        )
        db.commit()
    return row["status"], row["reason"], row, False


def _open_snapshot(addr, dexes, open_eps, now_ms, acct, *, universe):
    """Current OPEN-POSITION character inside the executable market scope.

    Clearinghouse snapshots are returned per dex but contain every market on
    that dex.  Each position is therefore checked against the same immutable
    universe used for history collection; outcome/private markets must not
    contaminate open PnL, leverage, risk or terminal replay marks.

    The data un-blinds the
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
    mark_prices = {}
    for dex in dexes:
        cs = rest.clearinghouse_state(addr, dex=dex)             # dex None -> standard perp dex
        if not isinstance(cs, dict):
            continue
        answered = True
        ms = cs.get("marginSummary", {})
        acct_val = max(acct_val, f(ms.get("accountValue")))      # standard dex carries the real equity
        for pp in cs.get("assetPositions", []) or []:
            p_ = pp.get("position", {})
            coin = p_.get("coin")
            szi, entry, pv = f(p_.get("szi")), f(p_.get("entryPx")), f(p_.get("positionValue"))
            if not is_copyable_coin(coin, universe=universe) or abs(szi) < config.FLAT:
                continue
            has_pos = True
            types.add((p_.get("leverage") or {}).get("type"))
            tot_ntl += abs(pv)
            open_position_count += 1
            upnl = f(p_.get("unrealizedPnl"))                    # HL's authoritative current unrealized
            if szi and pv:
                mark_prices[coin] = abs(pv / szi)
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
            "account_value": acct_val,
            "worst_underwater": worst_uw, "open_unrealized": up_loss + up_win,
            "open_loss_frac": up_loss / a, "open_win_frac": up_win / a,
            "bag_count": bag_n, "max_bag_days": max_bag_d, "max_win_days": max_win_d,
            "open_position_count": open_position_count, "material_open_count": material_open_count,
            "hedge_ratio": hedge_ratio, "mark_prices": mark_prices}


def _current_copy_valuation_marks():
    """Load one shared terminal-mark snapshot for cache-only replay paths.

    Fresh profile scans use each wallet's already-fetched clearinghouse snapshot. Regate and post-tune
    replay do not fetch one snapshot per wallet, so two bounded allMids calls cover the standard and
    transparent builder universes without adding per-wallet REST pressure.
    """
    out = {}
    for dex in (None, *rest.BUILDER_DEXES):
        for coin, px in (rest.all_mids(dex=dex) or {}).items():
            value = f(px)
            if value > 0:
                out[str(coin)] = value
    return out


def _missing_copy_valuation_coins(*result_groups) -> set[str]:
    """Collect terminal marks that canonical replay could not value, including nested sector windows."""
    missing = set()

    def visit(value):
        if not isinstance(value, dict):
            return
        missing.update(str(coin) for coin in value.get("valuation_missing_coins") or () if coin)
        for child in value.values():
            if isinstance(child, dict):
                visit(child)

    for group in result_groups:
        visit(group)
    return missing


def _retry_missing_copy_valuation_marks(current_marks, *result_groups, attempts: int = 2) -> dict:
    """Retry missing terminal prices through the independent bulk ``allMids`` source.

    Fresh profiling first uses the generation's immutable scan-start marks.  This retry exists for the
    narrow case where that context or a target position snapshot was transiently incomplete; an unresolved
    market still fails closed instead of trusting a stale last fill.
    """
    marks = {str(coin): f(px) for coin, px in dict(current_marks or {}).items() if f(px) > 0}
    missing = _missing_copy_valuation_coins(*result_groups) - set(marks)
    by_dex = {}
    for coin in missing:
        dex = coin.split(":", 1)[0] if ":" in coin else None
        by_dex.setdefault(dex, set()).add(coin)
    for dex, coins in by_dex.items():
        unresolved = set(coins)
        for _ in range(max(1, int(attempts or 1))):
            mids = rest.all_mids(dex=dex) or {}
            for coin in tuple(unresolved):
                px = f(mids.get(coin))
                if px > 0:
                    marks[coin] = px
                    unresolved.remove(coin)
            if not unresolved:
                break
    return marks


def _profile_one(db, addr, start_ms, now_ms, p, prior, lb, stamp, universe, force_full=False):
    # ONE aggregated fetch per wallet (aggregateByTime -> ~1 page, trade-level). No separate
    # pre-screen call: the response crosses the executable-market boundary before cache/metrics,
    # and gates reject dormant/no-copyable-contract evidence on that same scoped data.
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
        raw_full, hit_cap, new_fills, fetched_full_window = _fetch_profile_fills(
            db, addr, window_start, p, full, universe=universe,
        )
    except Exception as exc:  # noqa: BLE001 - network failures are a first-class deferred outcome
        return _defer_profile(db, addr, prior, stamp, f"fills_error:{type(exc).__name__}")
    for x in raw_full:
        x["user"] = addr
    # `_fetch_profile_fills` already crossed the collection boundary: only current standard Crypto
    # perps and transparent xyz contracts can reach this point or the cache. Normalize again as a
    # defensive invariant, then compute every metric from this exact scoped set.
    perp_full = normalize_copyable_fills(raw_full, addr=addr, universe=universe)
    perp = [x for x in perp_full if x["time"] >= start_ms]
    perp_frac = 1.0 if perp else 0.0
    eps, open_eps = build_episodes(perp)
    m = metrics.compute_metrics(perp, eps, now_ms, p.days)
    if m is None:
        m = {"n_fills": len(perp), "n_trades": 0, "window_days": 0, "trades_per_day": 0,
             "taker_frac_notl": 0, "median_hold_s": 0, "win_rate": 0, "net_pnl": 0,
             "total_notl": 0, "top_coin": None, "max_drawdown": 0, "avg_notional": 0, "hold_skew": 0,
             "last_fill_ms": perp[-1]["time"] if perp else 0, "active_days": 0, "activity_ratio": 0,
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
    m["liq_count"], m["liq_worst_pct"] = _self_liquidations(perp, addr, acct_value)
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
    sector_structure = _current_sector_structure_policy(perp, now_ms, p)
    # Every completed profile evaluation, including cold-start structural rejects, records which sectors
    # were independently evaluated this generation.  Strict Copy replay below replaces this preliminary
    # snapshot with the final net-of-cost economic policy for survivors.
    m["sector_policy_json"] = json.dumps(
        _structural_specialization_snapshot(sector_structure), sort_keys=True,
    )
    if not perp:
        ok, reason = False, "no_copyable_perp_fills"
    elif hit_cap:
        # A capped history is a real data-integrity failure, never a business rejection. Persist the
        # partial cache without marking coverage complete so the next scan is forced to heal it, then
        # quarantine/defer the profile while preserving any previously published usable snapshot.
        with _db_lock:
            _store_cached_fills(
                db, addr, new_fills, window_start,
                coverage_complete=False, coverage_end=now_ms, universe=universe,
            )
            db.commit()
        status, deferred_reason, deferred, _ = _defer_profile(db, addr, prior, stamp, "hit_page_cap")
        return status, deferred_reason, deferred, True
    else:
        ok, reason = metrics.gates_structural(m, p)
        # Specialization is derived from this generation's fills.  It must work identically on a fresh
        # database and may not require a previously sealed sector_policy_json to escape a whole-wallet
        # structural false positive.
        if (
            not ok
            and reason in _SECTOR_RECOVERABLE_STRUCTURE_REASONS
            and sector_structure.get("allowed")
        ):
            ok, reason = True, "ok"

    # STAGE B — fetch the LIVE open-position snapshot (un-blinds the funnel to held positions), fold in
    # realized+unrealized roi, then re-judge: held position = ACTIVE, 扛单 bags drag roi_total negative,
    # trend holders kept. Only structural survivors pay the extra clearinghouse call.
    if ok:
        dexes = {(c.split(":")[0] if ":" in c else None) for c in {x["coin"] for x in perp}}
        snap = _open_snapshot(addr, dexes, open_eps, now_ms, acct_value, universe=universe)
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
        # The portfolio endpoint is account-wide (spot + every perp dex) and has no market filter.  Its
        # PnL/volume/drawdown must never enter this product's quality path.  Keep only current account
        # equity as a denominator; profitability and execution edge come exclusively from scoped fills
        # and our fee-paid canonical Copy replay below.
        m["pf_equity"] = acct_value or snap.get("account_value")
        m["pf_week_pnl"] = m["pf_week_vlm"] = None
        m["pf_mon_pnl"] = m["pf_mon_vlm"] = None
        m["pf_turnover"] = None
        ok, reason = metrics.gates_state(m, now_ms, p)
        if (
            not ok
            and reason in _SECTOR_RECOVERABLE_STATE_REASONS
            and sector_structure.get("allowed")
        ):
            ok, reason = True, "ok"
    if ok:
        try:
            resolver = getattr(p, "generation_market_resolver", None)
            if resolver is None:
                if getattr(p, "scan_generation", None):
                    raise generation_market.MarketSnapshotError(
                        "generation_market_resolver_missing"
                    )
                # Compatibility for explicitly offline/unit replay callers that do not publish a generation.
                replay_sigmas = getattr(p, "copy_bt_sigmas", None) or {}
                replay_market_ctx = getattr(p, "copy_bt_market_ctx", None) or {}
                replay_fills = perp_full
            else:
                replay_sigmas, replay_market_ctx, replay_fills = {}, {}, []
                sector_market_errors = {}
                for sector in SECTORS:
                    if not (sector_structure.get(sector) or {}).get("allow"):
                        continue
                    sector_fills = [
                        x for x in perp_full if classify_coin(x.get("coin")) == sector
                    ]
                    try:
                        sector_sigmas, sector_ctx = resolver.ensure(
                            {x.get("coin") for x in sector_fills if x.get("coin")}
                        )
                    except generation_market.MarketSnapshotError as exc:
                        sector_market_errors[sector] = str(exc)
                        continue
                    replay_sigmas.update(sector_sigmas)
                    replay_market_ctx.update(sector_ctx)
                    replay_fills.extend(sector_fills)
                if sector_market_errors:
                    # Market transport/integrity failures are sector-local.  They cannot silently default,
                    # but an independent healthy specialty may still qualify under the product's isolation
                    # invariant. If every structurally viable sector failed, defer the wallet as a true error.
                    for sector, error in sector_market_errors.items():
                        sector_structure[sector] = {
                            **(sector_structure.get(sector) or {}),
                            "allow": False, "status": "market_data_error",
                            "reason": f"本轮板块市场数据失败：{error}", "dataError": error,
                        }
                    sector_structure["allowed"] = [
                        sector for sector in SECTORS
                        if (sector_structure.get(sector) or {}).get("allow")
                    ]
                    if not replay_fills:
                        raise generation_market.MarketSnapshotError(
                            next(iter(sector_market_errors.values()))
                        )
        except generation_market.MarketSnapshotError as exc:
            return _defer_profile(db, addr, prior, stamp, str(exc))
        # Qualification is anchored to the generation's scan-start context, not whichever target snapshot
        # happens to finish first.  A target can close between its history fetch and clearinghouse snapshot;
        # the replay then still has an as-of open position while the later account snapshot no longer lists
        # that coin.  The immutable market context supplies the correct independent terminal mark.
        generation_marks = {
            coin: f((replay_market_ctx.get(coin) or {}).get("mark_px"))
            for coin in replay_market_ctx
            if f((replay_market_ctx.get(coin) or {}).get("mark_px")) > 0
        }
        valuation_marks = {**(snap.get("mark_prices") or {}), **generation_marks}
        copy_results = _copy_bt_results(
            addr, replay_fills, now_ms, p, valuation_marks=valuation_marks,
            sigmas=replay_sigmas, market_ctx=replay_market_ctx,
        )
        sector_results = _sector_copy_bt_results(
            addr, replay_fills, now_ms, p, valuation_marks=valuation_marks,
            sigmas=replay_sigmas, market_ctx=replay_market_ctx,
        )
        retried_marks = _retry_missing_copy_valuation_marks(
            valuation_marks, copy_results, sector_results,
        )
        if retried_marks != valuation_marks:
            valuation_marks = retried_marks
            copy_results = _copy_bt_results(
                addr, replay_fills, now_ms, p, valuation_marks=valuation_marks,
                sigmas=replay_sigmas, market_ctx=replay_market_ctx,
            )
            sector_results = _sector_copy_bt_results(
                addr, replay_fills, now_ms, p, valuation_marks=valuation_marks,
                sigmas=replay_sigmas, market_ctx=replay_market_ctx,
            )
        ok, reason = _apply_sector_copy_bt_gate(
            m, copy_results, sector_results, p,
            previous_policy=(
                None
                if getattr(p, "rebuild_sector_policy", False)
                else (prior or {}).get("sector_policy_json")
            ),
            structural_policy=sector_structure,
        )
        try:
            sector_policy = json.loads(m.get("sector_policy_json") or "{}")
        except (TypeError, ValueError):
            sector_policy = {}
        allowed_sectors = set(sector_policy.get("allowed") or [])
        evidence_sectors = allowed_sectors or set(sector_policy.get("watch") or [])
        evidence_results = copy_results
        evidence_fills = replay_fills
        if evidence_sectors and evidence_sectors != {"crypto", "stock"}:
            allowed_fills = [x for x in replay_fills if classify_coin(x.get("coin")) in evidence_sectors]
            evidence_fills = allowed_fills
            evidence_results = _copy_bt_results(
                addr, allowed_fills, now_ms, p, valuation_marks=valuation_marks,
                sigmas=replay_sigmas, market_ctx=replay_market_ctx,
            )
        m.update(_open_flow_metrics(evidence_fills, now_ms))
        _copy_profile_evidence(m, evidence_results, p, addr=addr, now_ms=now_ms)
        if (
            not sector_policy.get("allowed")
            and not sector_policy.get("watch")
            and m.get("evidence_status") not in {"missing", "invalid"}
        ):
            m["evidence_status"] = "economically_disqualified"
        if m.get("data_status") == "deferred_data_error":
            return _defer_profile(db, addr, prior, stamp, "copy_replay_unavailable")
        if ok:
            _attach_open_copy_activity_context(
                m, addr, getattr(p, "open_copy_pnl_by_addr", {}),
            )
            ok, reason = _profile_copy_qualification(m, now_ms, p)
    m["times_active"] += 1 if ok else 0

    # age is NOT fetched (a full-history call just for account age = wasteful, and would penalise a
    # new wallet with strong recent performance). Survival now leans on times_active (our own observed
    # cross-scan persistence), not age. Keep any age a prior run already had; never fetch a new one.
    m["age_days"] = (prior or {}).get("age_days")

    prev_status = (prior or {}).get("status")
    ok, reason, m["score"] = _finalize_profile_qualification(m, ok, reason)
    status = "active" if ok else ("retired" if prev_status == "active" else "rejected")
    row = dict(m)                                    # keys match column names -> robust positional build
    row.update(addr=addr, status=status, reason=reason, last_refreshed=stamp,
               profile_generation=getattr(p, "scan_generation", None), evaluated_at=stamp,
               # Business qualification is not data health. Structural/economic rejects still had a
               # complete profile and must never be relabelled as copy_data_error by selection/UI code.
               # True fetch/cache/replay failures return through _defer_profile before reaching this write.
               data_status=m.get("data_status") or "valid",
               evidence_status=m.get("evidence_status") or ("qualified" if ok else "rejected"),
               first_added=(prior or {}).get("first_added") or (stamp if ok else None),
               times_seen=(prior or {}).get("times_seen", 0) + 1)
    cols = storage.PROFILE_COLS.split(",")
    with _db_lock:
        _store_cached_fills(
            db, addr, new_fills, window_start,
            # A delta fetch is only attempted from an already-complete cache. A successful response
            # therefore preserves that proof and advances its source cursor even when it contains no
            # in-scope fills. This avoids repeatedly downloading the same quiet/excluded-market interval.
            coverage_complete=not hit_cap, coverage_end=now_ms,
            universe=universe,
        )   # persist the delta + prune the window
        _replace_episode_rows(db, addr, eps)
        db.execute(f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                   f"VALUES ({','.join('?' * len(cols))})", [row.get(c) for c in cols])
        db.commit()
    return status, reason, m, hit_cap


# ------------------------------------------------------------------ curated outputs
def refresh_watchlist(db, stamp, *, leaderboard_generation=None, commit=True) -> int:
    """Rebuild OUR tiny leaderboard (watchlist) from active profiles. Derived view —
    profile stays the source of truth; operator settings in target_controls survive.
    """
    if commit:
        params.seed_params(db)
    margin_equity_pct = params.load_follow(db).get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
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
        "p.copy_bt_liquidations,p.copy_bt_fee_drag,p.copy_bt_unrealized_pnl,p.copy_bt_valuation_status,"
        "p.copy_bt_14d_net_pnl,p.copy_bt_14d_unrealized_pnl,p.copy_bt_14d_closed_n,"
        "p.copy_bt_7d_net_pnl,p.copy_bt_7d_unrealized_pnl,p.copy_bt_7d_closed_n,p.sector_copy_json,p.sector_policy_json,"
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
    policy_values = {
        **params.load_follow(db), **params.load_category(db, "scanner"),
    }
    ranked = []
    for r in rows:
        r["margin_equity_pct"] = margin_equity_pct
        score, detail = follow_score.compute_follow_score(r)
        detail = dict(detail or {})
        eligibility = follow_score.evaluate_follow_eligibility(
            r, margin_equity_pct=margin_equity_pct, policy_values=policy_values,
        )
        if not eligibility.get("eligible"):
            detail.setdefault("reasons", []).extend(eligibility.get("reasons") or [])
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


def _quality_first_core_transition(
    profiles,
    *,
    generation_id,
    previous_roles,
    controls,
    desired_order,
    strict_evaluate,
    robust_allowed_memberships=None,
    pinned_order=(),
):
    """Publish the current strict result without entry/exit hysteresis.

    Profile/data errors and wallets that no longer clear the individual Core gate cannot originate new
    positions.  Open copies are handled by the caller as Exit-only; they never justify stale Core authority.
    """
    rows = {(row.get("addr") or "").lower(): row for row in profiles}
    previous_core = {
        (addr or "").lower() for addr, role in previous_roles.items() if role == selection.CORE
    }
    desired = tuple(dict.fromkeys((addr or "").lower() for addr in desired_order if addr))
    pinned = tuple(dict.fromkeys((addr or "").lower() for addr in pinned_order if addr))
    pinned_set = set(pinned)
    selected = []
    reasons = {}
    hard_removed = set()
    for addr, row in rows.items():
        refreshed = row.get("profile_generation") == generation_id
        data_valid = refreshed and (row.get("data_status") or "valid") == "valid"
        enabled = controls.get(addr, True)
        qualification = row.get("follow_qualification") or {}
        core_ok = (
            row.get("status") in {"active", "qualified"}
            and bool(qualification.get("coreEligible"))
            and data_valid and enabled
        )
        nominated = data_valid and enabled and (addr in pinned_set or (core_ok and addr in desired))
        if nominated:
            selected.append(addr)
            reasons[addr] = (
                "operator_starred_core" if addr in pinned_set
                else "core_strong_evidence" if qualification.get("strongEntry")
                else "core_quality_selected"
            )
        elif addr in previous_core:
            hard_removed.add(addr)
            reasons[addr] = (
                "portfolio_not_selected" if core_ok
                else qualification.get("status") or row.get("reason") or "core_not_selected"
            )
        elif row.get("status") in {"active", "qualified"}:
            reasons[addr] = qualification.get("status") or "portfolio_not_selected"

    # Remove only a wallet whose *actual conditional presence* lowers the funded account's net result.
    # Coin overlap is deliberately irrelevant here: profitable consensus remains because taking any owner
    # out would reduce net PnL; redundant fee/drawdown drag can be removed regardless of quality rank.
    published = set(selected)
    max_removals = max(0, int(getattr(config, "CORE_LOO_MAX_REMOVALS", 2) or 0))
    min_net_gain = float(getattr(config, "CORE_LOO_MIN_NET_GAIN", 1.0) or 0.0)
    removed_by_loo = []
    robust_allowed = {
        tuple(sorted((addr or "").lower() for addr in membership if addr))
        for membership in (robust_allowed_memberships or ())
    }
    while len(published) > 1 and len(removed_by_loo) < max_removals:
        base = strict_evaluate(tuple(sorted(published)))
        trials = []
        for addr in published:
            if addr in pinned_set:
                continue
            without = strict_evaluate(tuple(sorted(published - {addr})))
            net_gain = f(without.net_pnl) - f(base.net_pnl)
            stress_gain = f(without.stress_net_pnl) - f(base.stress_net_pnl)
            feasible = (
                f(without.net_pnl) > 0.0
                and f(without.stress_net_pnl) > 0.0
                and f(without.actionable_open_rate) >= load_copy_policy().min_actionable_open_rate
                and f(without.capacity_fit) >= load_copy_policy().min_capacity_fit
                and (
                    not robust_allowed
                    or tuple(sorted(published - {addr})) in robust_allowed
                )
            )
            if feasible and net_gain >= min_net_gain:
                utility_gain = f(without.risk_adjusted_utility) - f(base.risk_adjusted_utility)
                trials.append((net_gain, utility_gain, stress_gain, -desired.index(addr), addr))
        if not trials:
            break
        _net_gain, _utility_gain, _stress_gain, _rank, outgoing = max(trials)
        published.remove(outgoing)
        removed_by_loo.append(outgoing)
        reasons[outgoing] = "portfolio_negative_incremental_net"
        if outgoing in previous_core:
            hard_removed.add(outgoing)

    # Conditional contribution under the final set remains the operator-facing Core rank.
    final_metrics = strict_evaluate(tuple(sorted(published)))
    base_utility = f(
        final_metrics.risk_adjusted_utility
        if final_metrics.risk_adjusted_utility is not None else final_metrics.net_lcb
    )
    contribution_rows = []
    desired_rank = {addr: rank for rank, addr in enumerate(desired)}
    for addr in published:
        without = strict_evaluate(tuple(sorted(published - {addr})))
        without_utility = f(
            without.risk_adjusted_utility
            if without.risk_adjusted_utility is not None else without.net_lcb
        )
        contribution_rows.append((base_utility - without_utility, -desired_rank.get(addr, 999999), addr))
    contribution_rows.sort(reverse=True)
    contribution_order = tuple(row[-1] for row in contribution_rows)
    final_order = tuple(addr for addr in pinned if addr in published) + tuple(
        addr for addr in contribution_order if addr not in pinned_set
    )
    return {
        "selected": final_order,
        "reasons": reasons,
        "utilities": {row[-1]: row[0] for row in contribution_rows},
        "hardRemoved": tuple(sorted(hard_removed)),
        "desired": desired,
        "metrics": final_metrics,
        "looRemoved": tuple(removed_by_loo),
    }


def _portfolio_selection_metrics(windows, baseline_n=0, selected_n=0):
    """Compact shared-account replay into actual-dollar selection economics.

    Isolated liquidations already lose their full allocated margin in ``copy_net_pnl`` and equity drawdown.
    Risk-adjusted utility subtracts max drawdown dollars once more, so a wallet passes only when its added
    net profit more than compensates for any added drawdown. Individual final replay already limits Core to
    five 30-day liquidations; the shared portfolio layer does not charge the same event a third time.
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


def _selection_prefetch_candidates(db, limit=None) -> list[str]:
    """Return the bounded qualified universe needed for path prefetch without running selection."""
    limit = max(0, int(config.MAX_TARGETS if limit is None else limit))
    if not limit:
        return []
    return [
        (row[0] or "").lower() for row in db.execute(
            "SELECT p.addr FROM profile p "
            "LEFT JOIN watchlist w ON w.addr=p.addr "
            "LEFT JOIN target_controls tc ON tc.addr=p.addr "
            "WHERE p.status IN ('active','qualified') "
            "AND COALESCE(tc.enabled,1)=1 "
            "ORDER BY COALESCE(w.score,-1) DESC,p.addr LIMIT ?",
            (limit,),
        ).fetchall()
        if row[0]
    ]


def _is_parameter_return_probe(row, margin_equity_pct: float) -> bool:
    """Whether a below-floor wallet is close enough to inform cold-start sizing without being published.

    The public Challenger line is 5%.  This internal set exists only to break the circular dependency
    where a low seeded margin rejects a strong wallet before the tuner can test the larger safe margin that
    would make it qualify.  Final replay must still clear the real line before the wallet can be published.
    """
    if str(row.get("reason") or "") not in {
        "copy_value_below_challenger_floor", "research_copy_positive", "research_insufficient_evidence",
    }:
        return False
    policy = load_copy_policy()
    scoped = apply_allowed_sector_copy_metrics(row)
    qualification_equity = max(1.0, float(config.INITIAL_BALANCE))
    pnl30 = f(scoped.get("copy_bt_net_pnl"))
    pnl7 = f(scoped.get("copy_bt_7d_net_pnl"))
    try:
        sector_policy = json.loads(row.get("sector_policy_json") or "{}")
    except (TypeError, ValueError):
        sector_policy = {}
    return bool(
        sector_policy.get("allowed")
        and str(scoped.get("copy_bt_valuation_status") or "complete") == "complete"
        and int(scoped.get("copy_bt_closed_n") or 0) >= policy.min_closed_30d
        and int(scoped.get("copy_bt_7d_closed_n") or 0) >= policy.min_closed_7d
        and int(row.get("copy_evidence_days") or 0) >= min(5, policy.min_closed_30d)
        and pnl30 >= qualification_equity * float(
            getattr(config, "CORE_TUNE_PROBE_MIN_RETURN_30D", 0.05)
        )
        and pnl7 >= qualification_equity * policy.challenger_min_return_7d
        and f(row.get("copy_expected_return")) >= policy.min_expected_margin_return
        and f(row.get("actionable_open_rate")) >= policy.min_actionable_open_rate
        and f(row.get("capacity_fit")) >= policy.min_capacity_fit
    )


def _quality_core_profiles(db, generation_id, *, core_only=True) -> list[dict]:
    """Current-generation follow-quality profiles in immutable quality order.

    ``core_only=False`` returns the bounded Core+Challenger workset needed for final-parameter
    requalification; the default preserves the original Core-ready contract for callers/tests.
    """
    cur = db.execute(
        "SELECT p.addr,p.status,p.reason,p.score,p.profile_generation,p.data_status,p.evidence_status,p.last_copyable_open_ms,"
        "p.copy_bt_closed_n,p.copy_bt_14d_closed_n,p.copy_bt_7d_closed_n,"
        "p.copy_positive_probability,p.copy_expected_return,p.copy_return_lcb,p.copy_return_volatility,"
        "p.copy_evidence_days,p.copy_recent_return_14d,p.copy_recent_return_7d,p.copy_risk_score,"
        "p.execution_score,p.open_probability_48h,"
        "p.actionable_open_rate,p.capacity_fit,p.copy_bt_net_pnl,p.copy_bt_unrealized_pnl,p.copy_bt_valuation_status,"
        "p.copy_bt_14d_net_pnl,p.copy_bt_14d_unrealized_pnl,p.copy_bt_7d_net_pnl,p.copy_bt_7d_unrealized_pnl,"
        "p.copy_bt_open_fill_rate,p.copy_bt_liquidations,p.copy_bt_fee_drag,p.sector_copy_json,p.sector_policy_json,p.acct_value "
        "FROM profile p WHERE p.profile_generation=?",
        (generation_id,),
    )
    names = [desc[0] for desc in cur.description]
    controls = {
        (addr or "").lower(): bool(enabled)
        for addr, enabled in db.execute("SELECT addr,enabled FROM target_controls").fetchall()
    }
    pinned_order = tuple(
        item["addr"] for item in selection.pinned_core_controls(db, enabled_only=True)
    )
    pinned = set(pinned_order)
    previous_selection = {
        row.addr: row for row in selection.current_selection_rows(db)
    }
    forward_risk = {
        (addr or "").lower(): {
            "forward_net_pnl": f(net_pnl),
            "forward_liquidations": int(liquidations or 0),
            "forward_closed_n": int(closed_n or 0),
        }
        for addr, net_pnl, liquidations, closed_n in db.execute(
            "SELECT addr,COALESCE(SUM(COALESCE(realized_pnl,0)+CASE WHEN status='open' "
            "THEN COALESCE(unrealized_pnl,0) ELSE 0 END),0),"
            "SUM(CASE WHEN COALESCE(was_liq,0)=1 AND julianday(closed_at)>=julianday('now','-30 days') "
            "THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END) FROM copy_position GROUP BY lower(addr)"
        ).fetchall()
    }
    live_wallet_risk = {
        (addr or "").lower(): {
            "wallet_breaker_stage": int(stage or 0),
            "wallet_cooldown_until_ms": cooldown,
            "wallet_drawdown_frac": f(drawdown),
            "wallet_risk_active_member": bool(active_member),
        }
        for addr, stage, cooldown, drawdown, active_member in db.execute(
            "SELECT addr,breaker_stage,cooldown_until_ms,drawdown_frac,active_member FROM wallet_risk_state "
            "WHERE execution_book='paper'"
        ).fetchall()
    }
    rows = []
    follow_values = params.load_follow(db)
    margin_equity_pct = follow_values.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
    policy_values = {**follow_values, **params.load_category(db, "scanner")}
    for raw in cur.fetchall():
        row = dict(zip(names, raw))
        addr = (row.get("addr") or "").lower()
        row["addr"] = addr
        row.update(forward_risk.get(addr) or {})
        row.update(live_wallet_risk.get(addr) or {})
        if addr in pinned:
            try:
                current_policy = json.loads(row.get("sector_policy_json") or "{}")
            except (TypeError, ValueError):
                current_policy = {}
            prior = previous_selection.get(addr)
            if not current_policy.get("allowed") and prior and prior.sector_policy_json:
                row["sector_policy_json"] = prior.sector_policy_json
        row["margin_equity_pct"] = margin_equity_pct
        row["follow_score"] = follow_score.compute_follow_score(row)[0]
        row["follow_qualification"] = follow_score.evaluate_follow_eligibility({
            **row,
            "copy_bt_data_status": row.get("data_status"),
            "copy_bt_evidence_status": row.get("evidence_status"),
        }, margin_equity_pct=margin_equity_pct, policy_values=policy_values)
        qualified = (
            row.get("status") in {"active", "qualified"}
            and (row.get("follow_qualification") or {}).get(
                "coreEligible" if core_only else "eligible"
            )
        )
        probe = bool(not core_only and _is_parameter_return_probe(row, margin_equity_pct))
        if (
            (qualified or probe or addr in pinned)
            and (row.get("data_status") or "valid") == "valid"
            and controls.get(addr, True)
        ):
            row["formation_probe"] = probe
            rows.append(row)
    present = {row["addr"] for row in rows}
    missing_pinned = [addr for addr in pinned_order if addr not in present]
    if missing_pinned:
        raise RuntimeError(f"pinned_core_profile_unavailable:{len(missing_pinned)}")
    pin_rank = {addr: rank for rank, addr in enumerate(pinned_order)}
    rows.sort(key=lambda row: (
        0 if row["addr"] in pinned else 1,
        pin_rank.get(row["addr"], 999999),
        -(row.get("follow_score") or 0.0), row.get("addr") or "",
    ))
    return rows


def _effective_follow_replay(db, row, now_ms, *, generation_id, follow, valuation_marks,
                             sigmas=None, market_ctx=None, retention=False) -> dict:
    """Replay one wallet under the final parameter surface without mutating its scan-time profile.

    Formation first tunes the shared account.  This second, cache-only pass is the authoritative individual
    profitability check for that tuned surface.  One shared mark snapshot is supplied by the caller, so the
    check adds CPU work but no per-wallet network request.
    """
    addr = (row.get("addr") or "").lower()
    replay_ctx = SimpleNamespace(
        copy_bt_days=int(config.COPY_BT_DAYS),
        copy_bt_sigmas=dict(sigmas if sigmas is not None else _copy_bt_sigmas(db)),
        copy_bt_market_ctx=dict(market_ctx if market_ctx is not None else _copy_bt_market_ctx(db)),
        copy_bt_overrides={**dict(follow), "AMBIGUOUS_PATH_MODE": "liquidate"},
        copy_bt_valuation_marks=dict(valuation_marks or {}),
        scan_generation=generation_id,
        margin_equity_pct=follow.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT),
    )
    fills = _copy_bt_cached_fills(db, addr, int(now_ms), replay_ctx)
    try:
        sector_policy = json.loads(row.get("sector_policy_json") or "{}")
    except (TypeError, ValueError):
        sector_policy = {}
    allowed = set(sector_policy.get("allowed") or ())
    evidence_sectors = allowed or set(sector_policy.get("watch") or ())
    if not evidence_sectors:
        return {
            "metrics": {}, "score": 0.0,
            "qualification": {
                "eligible": False, "coreEligible": False,
                "status": "effective_sector_policy_missing", "role": "quarantine",
                "deferred": True, "reasons": ["最终参数回放缺少板块策略"],
            },
        }
    evidence_fills = [
        fill for fill in fills if classify_coin(fill.get("coin")) in evidence_sectors
    ]
    # Formation has already prefetched the bounded candidate path cache.  Individual qualification must
    # consume that same canonical intratrade path as the shared account; otherwise a wallet can display
    # +$4k/70% in the profile while the exact portfolio replay sees four liquidations and a net loss.
    path_start = int(now_ms) - (
        int(config.COPY_BT_DAYS) + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
    ) * 86_400_000
    replay_ctx.copy_bt_price_path = price_path.load_refined(
        db, evidence_fills, path_start, int(now_ms),
    )
    replay_ctx.copy_bt_price_path_meta = price_path.coverage(
        db, evidence_fills, path_start, int(now_ms),
    )
    results = _copy_bt_results(
        addr, evidence_fills, int(now_ms), replay_ctx,
        valuation_marks=replay_ctx.copy_bt_valuation_marks,
    )
    replay_versions = {
        str(result.get("add_metrics_version") or "")
        for result in results.values() if result and result.get("has_evidence")
    }
    if replay_versions and replay_versions != {ADD_METRICS_VERSION}:
        return {
            "metrics": {}, "score": 0.0,
            "qualification": {
                "eligible": False, "coreEligible": False,
                "status": "add_metrics_version_mismatch", "role": "quarantine",
                "deferred": True, "reasons": ["最终参数回放混用了不同版加仓指标"],
            },
        }
    effective = {"sector_policy_json": row.get("sector_policy_json")}
    _apply_copy_bt_gate(effective, results, replay_ctx)
    effective.update(_open_flow_metrics(evidence_fills, int(now_ms)))
    _copy_profile_evidence(effective, results, replay_ctx, addr=addr, now_ms=int(now_ms))
    for key in (
        "forward_net_pnl", "forward_liquidations", "forward_closed_n",
        "wallet_breaker_stage", "wallet_cooldown_until_ms", "wallet_drawdown_frac",
        "wallet_risk_active_member",
    ):
        if row.get(key) is not None:
            effective[key] = row[key]
    qualification = follow_score.evaluate_follow_eligibility(
        {
            **effective,
            "copy_bt_data_status": effective.get("data_status", effective.get("copy_bt_data_status")),
            "copy_bt_evidence_status": effective.get(
                "evidence_status", effective.get("copy_bt_evidence_status")
            ),
        },
        margin_equity_pct=replay_ctx.margin_equity_pct,
        policy_values=follow,
        retention=bool(retention),
    )
    # The replay already contains only sectors allowed by the sealed policy.  Do not let the scan-time
    # sector aggregate overwrite these final-parameter metrics while recomputing rank.
    score, _detail = follow_score.compute_follow_score({
        **row, **effective, "sector_copy_json": None,
        "margin_equity_pct": replay_ctx.margin_equity_pct,
    })
    return {"metrics": effective, "qualification": qualification, "score": score}


def _apply_core_soft_failure_grace(db, addr, generation_id, qualification, policy_values=None):
    """Persist the two-complete-scan Core retention counter and apply one soft-failure grace round."""
    result = dict(qualification or {})
    row = db.execute(
        "SELECT core_soft_fail_count,core_soft_fail_generation FROM wallet_registry WHERE addr=?",
        (str(addr or "").lower(),),
    ).fetchone()
    old_count = int(row[0] or 0) if row else 0
    old_generation = row[1] if row else None
    if result.get("coreEligible"):
        count = 0
        reason = None
    elif result.get("hardRisk") or result.get("role") in {"exit_only", "quarantine"}:
        count = load_copy_policy(policy_values).soft_fail_confirmations
        reason = result.get("status") or "hard_risk"
    else:
        count = old_count if old_generation == generation_id else old_count + 1
        reason = result.get("status") or "soft_failure"
        required = load_copy_policy(policy_values).soft_fail_confirmations
        if count < required:
            original = dict(result)
            result.update({
                "eligible": True,
                "coreEligible": True,
                "strongEntry": False,
                "status": "core_retained_soft_grace",
                "role": "core_eligible",
                "softFailureCount": count,
                "softFailureOriginal": original,
                "reasons": [
                    f"Core软条件第{count}轮失败，达到连续{required}轮前保留；硬风险仍即时退出"
                ],
            })
    if row:
        db.execute(
            "UPDATE wallet_registry SET core_soft_fail_count=?,core_soft_fail_generation=?,"
            "core_soft_fail_reason=?,updated_at=? WHERE addr=?",
            (count, generation_id, reason, now_iso(), str(addr or "").lower()),
        )
    return result


def _prefix_eval_from_tune(count, tune_result, *, initial_balance):
    validation = dict(tune_result.get("validation") or {})
    folds = list(validation.get("folds") or ())

    def side(prefix, stress_key, stress_liq_key, proposal):
        net = sum(f(row.get(f"{prefix}Net")) for row in folds)
        max_dd = max((f(row.get(f"{prefix}MaxDD")) for row in folds), default=1.0)
        open_rate = min((f(row.get(f"{prefix}OpenRate")) for row in folds), default=0.0)
        capacity = min((f(row.get(f"{prefix}CapacityFit")) for row in folds), default=0.0)
        liquidations = max(
            [int(row.get(f"{prefix}Liquidations") or 0) for row in folds]
            + [int(validation.get(stress_liq_key) or 0)]
        )
        return core_formation.PrefixEvaluation(
            count=int(count), net_pnl=net,
            stress_net_pnl=f(validation.get(stress_key)), max_drawdown=max_dd,
            actionable_open_rate=open_rate, capacity_fit=capacity,
            liquidations=liquidations, params=dict(proposal or {}),
            payload={"initialBalance": float(initial_balance)},
        )

    challenger = side(
        "challenger", "stressNet", "stressLiquidations", tune_result.get("proposal") or {},
    )
    baseline = side(
        "baseline", "baselineStressNet", "baselineStressLiquidations",
        tune_result.get("baseline_proposal") or {},
    )
    # A rejected proposal must never win formation merely because its in-sample utility is attractive.
    # The tuner already performed fold/holdout/stress validation; explicit failure is authoritative and
    # means this wallet-count node is evaluated on its active baseline surface.
    if tune_result.get("eligible_to_apply") is False:
        return baseline
    feasible = [value for value in (challenger, baseline) if value.feasible]
    return max(
        feasible or [challenger, baseline],
        key=lambda value: (value.utility, value.stress_net_pnl),
    )


def _formation_param_surface(base_follow, tune_result=None, *, retune=True):
    """Return the only parameter surface Core formation is allowed to seal."""
    tuned = {
        key: f(base_follow.get(key))
        for key in (*auto_tune.TUNE_KEYS, *auto_tune.ADD_TUNE_KEYS)
    }
    if not retune:
        return tuned, None, "retune_disabled"
    # Rolling-deploy test doubles may omit this field. Production's explicit false is authoritative.
    eligible = (tune_result or {}).get("eligible_to_apply") is not False
    reason = (
        "validated_proposal" if eligible
        else ",".join(((tune_result or {}).get("validation") or {}).get("reasons") or ())
        or "no_validated_finalist"
    )
    if eligible:
        proposal = dict((tune_result or {}).get("params") or {})
        proposal.update((tune_result or {}).get("add_params") or {})
        proposal.update((tune_result or {}).get("proposal") or {})
        tuned.update({key: f(proposal.get(key, tuned[key])) for key in tuned})
    return tuned, eligible, reason


_PARAMETER_TUNABLE_CHALLENGER_STATUSES = {
    "challenger_return_watch",
    "challenger_weekly_return_watch",
    "challenger_confidence_watch",
    "challenger_thin_edge_watch",
    "challenger_recent_decline",
}


def _formation_tune_candidate(row) -> bool:
    """Whether a quality-qualified wallet should influence the joint sizing search.

    Return/weekly/confidence misses can change when margin, leverage and add behaviour change, so excluding
    them creates a circular tuner that can only optimize the incumbent Core.  Missing samples, unresolved
    open valuation and structural-watch wallets cannot be repaired by sizing and remain observation-only.
    """
    qualification = dict((row or {}).get("follow_qualification") or {})
    if (row or {}).get("formation_probe"):
        return qualification.get("status") in {
            "copy_value_below_challenger_floor", "research_copy_positive", "research_insufficient_evidence",
        }
    if not qualification.get("eligible"):
        return False
    return bool(
        qualification.get("coreEligible")
        or qualification.get("status") in _PARAMETER_TUNABLE_CHALLENGER_STATUSES
    )


def _rank_formation_candidates_for_surface(db, rows, now_ms, *, generation_id, follow,
                                           valuation_marks, sigmas, market_ctx,
                                           required_order=()) -> list[dict]:
    """Re-rank the bounded quality pool under the exact active parameter surface.

    Profile Copy columns are immutable scan-time evidence and may have been produced before the latest
    generation-bound tuner revision.  Using them to order a second formation pass makes the UI's current
    replay and the optimizer disagree.  Recompute from cached fills once here; this is CPU-only and uses the
    same marks, market metadata and follow snapshot for every wallet.
    """
    ranked = []
    required_order = tuple(dict.fromkeys((addr or "").lower() for addr in required_order if addr))
    required = set(required_order)
    current_core = set(selection.published_core_addrs(db) or ()) if db is not None else set()
    for row in rows:
        effective = _effective_follow_replay(
            db, row, now_ms, generation_id=generation_id, follow=follow,
            valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
            retention=(row.get("addr") or "").lower() in current_core,
        )
        qualification = dict(effective.get("qualification") or {})
        addr = (row.get("addr") or "").lower()
        if addr in current_core:
            qualification = _apply_core_soft_failure_grace(
                db, addr, generation_id, qualification, policy_values=follow,
            )
        if qualification.get("deferred") or qualification.get("role") == "quarantine":
            if addr in required:
                raise RuntimeError(f"pinned_core_replay_invalid:{addr}")
            continue
        parameter_probe = bool(
            row.get("formation_probe")
            and qualification.get("status") == "copy_value_below_challenger_floor"
        )
        if not qualification.get("eligible") and not parameter_probe and addr not in required:
            continue
        ranked.append({
            **row,
            "follow_score": f(effective.get("score")),
            "follow_qualification": qualification,
        })
    required_rank = {addr: rank for rank, addr in enumerate(required_order)}
    ranked.sort(key=lambda row: (
        0 if row.get("addr") in required else 1,
        required_rank.get(row.get("addr"), 999999),
        -(row.get("follow_score") or 0.0), row.get("addr") or "",
    ))
    return ranked


def _core_prefix_retention() -> dict:
    return {
        "utility_retention": float(config.CORE_PREFIX_UTILITY_RETENTION),
        "net_retention": float(config.CORE_PREFIX_NET_RETENTION),
        "stress_retention": float(config.CORE_PREFIX_STRESS_RETENTION),
        "utility_slack": float(config.CORE_PREFIX_ABS_UTILITY_SLACK),
        "net_slack": float(config.CORE_PREFIX_ABS_NET_SLACK),
        "stress_slack": float(config.CORE_PREFIX_ABS_STRESS_SLACK),
        "max_dd_worsen": float(config.CORE_PREFIX_MAX_DD_WORSEN),
    }


def _core_rebalance_due(db, current_core, *, now_ms: int, interval_days: int) -> tuple:
    """Age the current membership, not the daily evidence snapshot.

    Daily scans publish a fresh generation even when the Core set is unchanged.  Walking back through the
    consecutive generations with the same membership keeps those evidence refreshes from resetting the weekly
    rebalance clock.  Hard qualification failures still bypass this normal-cycle decision in formation.
    """
    rows = db.execute(
        "SELECT sg.generation,sg.published_at,lower(fs.addr),lower(fs.role),COALESCE(fs.enabled,1) "
        "FROM scan_generation sg LEFT JOIN follow_selection fs ON fs.generation=sg.generation "
        "WHERE sg.status='published' AND sg.complete=1 "
        "ORDER BY sg.published_at DESC,sg.id DESC,lower(fs.addr),fs.addr"
    ).fetchall()
    if not rows:
        return True, None
    snapshots = []
    for generation_id, published_at, addr, role, enabled in rows:
        if not snapshots or snapshots[-1][0] != generation_id:
            snapshots.append([generation_id, published_at, set()])
        if role == selection.CORE and enabled and addr:
            snapshots[-1][2].add(addr)
    wanted = {(addr or "").lower() for addr in (current_core or ()) if addr}
    if not wanted or not snapshots or snapshots[0][2] != wanted:
        return True, None
    anchor = snapshots[0][1]
    for _generation_id, published_at, members in snapshots[1:]:
        if members != wanted:
            break
        anchor = published_at or anchor
    if not anchor:
        return True, None
    try:
        published_s = calendar.timegm(time.strptime(str(anchor), "%Y-%m-%dT%H:%M:%SZ"))
        age_days = max(0.0, (float(now_ms) / 1000.0 - published_s) / 86400.0)
    except (TypeError, ValueError, OverflowError):
        return True, None
    return age_days >= max(0, int(interval_days)), age_days


def _explicit_empty_core_formation(ranked_rows, *, reason: str, **search_meta) -> dict:
    """Seal a zero-Core result when strict portfolio evidence is unavailable.

    Individual profile evidence may still be useful for Research/Challenger classification, but it is not
    sufficient to fund a shared Core without a replayable portfolio fill surface.  Returning an explicit
    empty formation lets the normal atomic publication path turn every old Core into Exit-only instead of
    failing the whole generation and silently retaining stale risk.
    """
    rows = list(ranked_rows or ())
    qualifications = {
        (row.get("addr") or "").lower(): dict(row.get("follow_qualification") or {})
        for row in rows if row.get("addr")
    }
    scores = {
        (row.get("addr") or "").lower(): f(row.get("follow_score"))
        for row in rows if row.get("addr")
    }
    admission = [{
        "addr": (row.get("addr") or "").lower(),
        "passed": bool((row.get("follow_qualification") or {}).get("coreEligible")),
        "status": (row.get("follow_qualification") or {}).get("status") or "unknown",
    } for row in rows if row.get("addr")]
    return {
        "selected": (),
        "ranked": tuple((row.get("addr") or "").lower() for row in rows if row.get("addr")),
        "params": {},
        "evaluations": (),
        "qualifications": qualifications,
        "scores": scores,
        "search": {
            "algorithm": "quality_prefix_joint_binary_v4",
            "initialCount": len(rows),
            "selectedCount": 0,
            "explicitEmptyCore": True,
            "formationTuneEligible": False,
            "formationTuneReason": str(reason or "strict_portfolio_evidence_unavailable"),
            "qualificationRejected": [],
            "admission": admission,
            **search_meta,
        },
    }


def form_quality_prefix(db, generation_id, stamp, now_ms=None, *, retune=True) -> dict:
    """Jointly tune binary quality-prefix counts, then seal one final internally consistent surface."""
    now_ms = int(now_ms or time.time() * 1000)
    ranked_candidates = _quality_core_profiles(db, generation_id, core_only=False)
    base_follow = params.load_follow(db)
    scanner_values = params.load_category(db, "scanner")
    base_follow.update({
        key: scanner_values[key] for key in COPY_POLICY_PARAM_KEYS if key in scanner_values
    })
    if "SMART_ADD" in base_follow:
        base_follow["ADD_STRATEGY"] = "smart" if base_follow["SMART_ADD"] else "hardcap"
    sigmas = auto_tune._load_sigmas(db, generation_id)
    market_ctx = auto_tune._load_market_ctx(db, generation_id)
    valuation_marks = _current_copy_valuation_marks()
    pinned_order = tuple(
        item["addr"] for item in selection.pinned_core_controls(db, enabled_only=True)
    )
    pinned = set(pinned_order)
    current_core = tuple(selection.published_core_addrs(db) or ())
    target_min = max(1, int(params.get(db, "CORE_TARGET_MIN_N", config.CORE_TARGET_MIN_N) or 1))
    rebalance_interval = max(1, int(params.get(
        db, "CORE_REBALANCE_INTERVAL_DAYS", config.CORE_REBALANCE_INTERVAL_DAYS,
    ) or 1))
    rebalance_due, core_age_days = _core_rebalance_due(
        db, current_core, now_ms=now_ms, interval_days=rebalance_interval,
    )
    # Daily scans still refresh evidence and can immediately remove a hard-risk failure. Parameter/membership
    # optimization only runs weekly, except while the Core is below its minimum target and needs safe additions.
    retune = bool(retune and (rebalance_due or len(current_core) < target_min))
    surface_ranked = _rank_formation_candidates_for_surface(
        db, ranked_candidates, now_ms, generation_id=generation_id, follow=base_follow,
        valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
        required_order=pinned_order,
    )
    upper = max(1, len(pinned_order), min(
        int(config.MAX_TARGETS),
        int(params.get(db, "CORE_INITIAL_MAX_N", config.CORE_INITIAL_MAX_N) or config.CORE_INITIAL_MAX_N),
    ))
    # Tune against every quality wallet whose economics can improve under a different sizing surface.
    # Individual qualification is recomputed once under the winning surface; portfolio folds/holdout/stress
    # are the only stability checks and no second time-based admission gate may override them.
    tune_ranked = [
        row for row in surface_ranked
        if row.get("addr") in pinned or _formation_tune_candidate(row)
    ][:upper]
    if not tune_ranked:
        return _explicit_empty_core_formation(
            surface_ranked, reason="no_core_qualified_wallets", tunePoolCount=0,
        )
    tune_ordered = tuple(row["addr"] for row in tune_ranked)

    # A generation with individually promising profiles but no copyable portfolio fills is a valid zero-Core
    # outcome, not a reason to roll back to stale members.  Preflight the exact bounded tune pool before the
    # binary search so ``maybe_tune_margins`` cannot turn ``no_cached_fills`` into a publication failure.
    tune_window_fills = auto_tune._portfolio_window_fills(db, list(tune_ordered), now_ms)
    if tune_window_fills is None or not any(tune_window_fills.values()):
        return _explicit_empty_core_formation(
            surface_ranked,
            reason=("fill_cache_guard" if tune_window_fills is None else "no_cached_fills"),
            tunePoolCount=len(tune_ordered),
        )

    tune_eligible = None
    tune_reason = "retune_disabled"
    tune_search = None
    tune_runs = {}
    chosen_run = {}
    retention = _core_prefix_retention()
    if retune:
        def tune_evaluate(count):
            count = int(count)
            _set_scan_progress(
                db, stage="portfolio_tune", candidates_scanned=count,
                candidates_total=len(tune_ordered),
            )
            result = auto_tune.maybe_tune_margins(
                db, source="core_formation", stamp=f"{stamp}:k{count}",
                dry_run=True, mode="apply", follow_values=base_follow, data_complete=True,
                addrs_override=list(tune_ordered[:count]), record_run=False,
                formation_admission=True, market_generation=generation_id,
            )
            if result.get("status") != "ok":
                raise RuntimeError(
                    "core_prefix_tune_failed:" + str(result.get("reason") or result.get("status"))
                )
            tune_runs[count] = result
            db.commit()  # reusable path-cache writes only; membership/params remain untouched.
            return _prefix_eval_from_tune(
                count, result,
                initial_balance=f(base_follow.get("INITIAL_BALANCE") or config.INITIAL_BALANCE),
            )

        tune_search = core_formation.search_quality_prefix(
            len(tune_ordered), tune_evaluate, retention_kwargs=retention,
            tie_tolerance=float(config.CORE_PREFIX_TIE_TOLERANCE),
            exhaustive_below=int(getattr(config, "CORE_PREFIX_EXHAUSTIVE_MAX_N", 8) or 0),
            min_count=max(1, len(pinned_order)),
        )
        tuned_params = dict(tune_search.selected.params or {})
        chosen_run = tune_runs.get(int(tune_search.selected.count)) or {}
        _surface, tune_eligible, tune_reason = _formation_param_surface(
            base_follow, chosen_run, retune=True,
        )
        if not tuned_params:
            tuned_params = _surface
    else:
        tuned_params, tune_eligible, tune_reason = _formation_param_surface(
            base_follow, None, retune=False,
        )
    fixed_follow = {**base_follow, **tuned_params, "AMBIGUOUS_PATH_MODE": "liquidate"}

    # Parameter tuning changes leverage, initial margin and add behaviour.  Replaying only the portfolio
    # after that change is insufficient: every potential owner must still clear the percentage-based
    # individual profit floors under the exact surface that will be sealed for Observer.
    base_core_count = sum(
        1 for row in surface_ranked
        if (row.get("follow_qualification") or {}).get("coreEligible")
    )
    active_tune_surface = {
        key: f(base_follow.get(key)) for key in (*auto_tune.TUNE_KEYS, *auto_tune.ADD_TUNE_KEYS)
    }
    tune_surface_changed = any(
        abs(f(tuned_params.get(key)) - value) > 1e-12
        for key, value in active_tune_surface.items()
    )

    def replay_effective_surface(follow_surface):
        qualifications = {}
        scores = {}
        qualified_rows = []
        audit = []
        rejected = []
        for row in ranked_candidates:
            effective = _effective_follow_replay(
                db, row, now_ms, generation_id=generation_id, follow=follow_surface,
                valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
                retention=row["addr"] in set(current_core),
            )
            qualification = dict(effective.get("qualification") or {})
            addr = row["addr"]
            if addr in set(current_core):
                qualification = _apply_core_soft_failure_grace(
                    db, addr, generation_id, qualification, policy_values=follow_surface,
                )
            if qualification.get("deferred") or qualification.get("role") == "quarantine":
                raise RuntimeError(f"effective_copy_replay_invalid:{addr}")
            qualifications[addr] = qualification
            scores[addr] = f(effective.get("score"))
            passed = bool(qualification.get("coreEligible"))
            audit.append({
                "addr": addr,
                "passed": passed,
                "status": qualification.get("status") or "unknown",
                "operatorStarred": addr in pinned,
            })
            if passed:
                qualified_rows.append(row)
            else:
                rejected.append(addr)
        return qualifications, scores, qualified_rows, audit, rejected

    (effective_qualifications, effective_scores, effective_ranked,
     admission_audit, qualification_rejected) = replay_effective_surface(fixed_follow)
    tune_coverage_fallback = False
    # Parameters serve the qualified pool. They may improve shared-account dollars, but may not win by
    # shrinking today's individually Core-qualified coverage. Membership search owns capital contention.
    if tune_surface_changed and len(effective_ranked) < base_core_count:
        tuned_params = dict(active_tune_surface)
        fixed_follow = {**base_follow, **tuned_params, "AMBIGUOUS_PATH_MODE": "liquidate"}
        (effective_qualifications, effective_scores, effective_ranked,
         admission_audit, qualification_rejected) = replay_effective_surface(fixed_follow)
        tune_coverage_fallback = True
        tune_eligible = False
        tune_reason = "qualified_wallet_coverage_regressed"
    effective_pinned_order = tuple(
        addr for addr in pinned_order
        if (effective_qualifications.get(addr) or {}).get("coreEligible")
    )
    effective_pinned = set(effective_pinned_order)
    pin_rank = {addr: rank for rank, addr in enumerate(pinned_order)}
    current_rank = {addr: rank for rank, addr in enumerate(current_core)}
    effective_ranked.sort(key=lambda row: (
        0 if row["addr"] in pinned else 1,
        0 if row["addr"] in current_rank else 1,
        pin_rank.get(row["addr"], 999999),
        current_rank.get(row["addr"], 999999),
        -effective_scores.get(row["addr"], 0.0), row["addr"],
    ))
    ordered = tuple(row["addr"] for row in effective_ranked[:upper])
    if not ordered:
        return {
            "selected": (), "ranked": (), "params": {}, "evaluations": (),
            "qualifications": effective_qualifications,
            "scores": effective_scores,
            "search": {
                "algorithm": "quality_prefix_joint_binary_v4", "initialCount": 0,
                "selectedCount": 0,
                "explicitEmptyCore": True,
                "tunePoolCount": len(tune_ordered),
                "tunedInputCount": (
                    int(tune_search.selected.count) if tune_search is not None else len(tune_ordered)
                ),
                "fullTuneRuns": len(tune_runs),
                "tuneEvaluatedCounts": (
                    [value.count for value in tune_search.evaluated] if tune_search is not None else []
                ),
                "effectiveRejected": qualification_rejected,
                "formationTuneEligible": tune_eligible,
                "formationTuneReason": tune_reason,
                "tuneCoverageFallback": tune_coverage_fallback,
                "formationTuneFinalists": list(chosen_run.get("finalists") or ()),
                "formationMarginRounds": list(chosen_run.get("margin_rounds") or ()),
                "qualificationRejected": qualification_rejected,
                "admission": admission_audit,
            },
        }
    window_fills = auto_tune._portfolio_window_fills(db, list(ordered), now_ms)
    if window_fills is None or not any(window_fills.values()):
        raise RuntimeError("core_prefix_replay_unavailable")
    all_fills = list(window_fills.get(max(window_fills)) or [])
    path_start = now_ms - (
        max(window_fills) + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7))
    ) * 86_400_000
    shared_path = price_path.load_refined(db, all_fills, path_start, now_ms)
    shared_meta = price_path.coverage(db, all_fills, path_start, now_ms)

    membership_eval_cache = {}
    membership_replay_cache = {}

    def evaluate_members(addrs):
        key = tuple(sorted(dict.fromkeys(addrs)))
        if key in membership_eval_cache:
            return membership_eval_cache[key]
        _set_scan_progress(
            db, stage="portfolio_tune", candidates_scanned=len(key), candidates_total=len(ordered),
        )
        filtered = auto_tune._filter_window_fills_by_addr(window_fills, key)
        windows = auto_tune._candidate_windows(
            db, list(key), sigmas, fixed_follow, now_ms,
            window_fills=filtered, market_ctx=market_ctx,
            path_rows=shared_path, path_meta=shared_meta,
        )
        stressed = auto_tune._candidate_windows(
            db, list(key), sigmas, {**fixed_follow, "REPLAY_COST_MULT": 1.5}, now_ms,
            window_fills=filtered, market_ctx=market_ctx,
            path_rows=shared_path, path_meta=shared_meta,
        )
        metrics_ = _portfolio_selection_metrics(windows, selected_n=len(key))
        stress_net = min(
            (f(result.get("copy_net_pnl")) for result in stressed.values()), default=-1e12,
        )
        stress_liquidations = max(
            (int(result.get("liquidations") or 0) for result in stressed.values()), default=0,
        )
        value = core_formation.PrefixEvaluation(
            count=len(key), net_pnl=f(metrics_.net_pnl), stress_net_pnl=stress_net,
            max_drawdown=f(metrics_.max_drawdown),
            actionable_open_rate=f(metrics_.actionable_open_rate),
            capacity_fit=f(metrics_.capacity_fit),
            liquidations=max(int(metrics_.liquidations), stress_liquidations),
            params=tuned_params,
            payload={"initialBalance": f(base_follow.get("INITIAL_BALANCE") or config.INITIAL_BALANCE)},
        )
        membership_eval_cache[key] = value
        membership_replay_cache[key] = (windows, stressed)
        return value

    def evaluate(count):
        return evaluate_members(ordered[:int(count)])

    prefix_search = core_formation.search_quality_prefix(
        len(ordered), evaluate, retention_kwargs=retention,
        tie_tolerance=float(config.CORE_PREFIX_TIE_TOLERANCE),
        exhaustive_below=int(getattr(config, "CORE_PREFIX_EXHAUSTIVE_MAX_N", 8) or 0),
        min_count=max(1, len(effective_pinned_order)),
    )
    membership_search = core_formation.search_quality_membership(
        ordered, evaluate_members,
        initial=ordered[:prefix_search.selected.count],
        required=effective_pinned_order,
        exhaustive_below=int(getattr(config, "CORE_PREFIX_EXHAUSTIVE_MAX_N", 8) or 0),
    )
    chosen = membership_search.metrics
    chosen_addrs = tuple(membership_search.selected)

    # Membership selection now receives its own independent-regime validation. Parameter candidates have
    # already passed the tuner's walk-forward check, but that says nothing about swapping wallet owners on
    # the winning surface. Only a bounded set of strict finalists pays this additional CPU cost.
    fold_cache = {}

    def fold_replays(addrs):
        key = tuple(sorted(dict.fromkeys(addrs)))
        if key in fold_cache:
            return fold_cache[key]
        if not key:
            zero = core_formation.PrefixEvaluation(
                count=0, net_pnl=0.0, stress_net_pnl=0.0, max_drawdown=0.0,
                actionable_open_rate=1.0, capacity_fit=1.0, liquidations=0,
                params=tuned_params,
                payload={"initialBalance": f(base_follow.get("INITIAL_BALANCE") or config.INITIAL_BALANCE)},
            )
            fold_cache[key] = ([zero, zero, zero], 0.0)
            return fold_cache[key]
        filtered = auto_tune._filter_window_fills_by_addr(window_fills, key)
        all_rows = list(filtered.get(max(filtered)) or [])
        warmup_ms = int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0) * 86_400_000
        folds = []
        for older, newer in ((30, 20), (20, 10), (10, 0)):
            lo = now_ms - older * 86_400_000
            hi = now_ms - newer * 86_400_000 if newer else now_ms + 1
            rows = [
                row for row in all_rows
                if lo - warmup_ms <= int(row.get("time") or 0) < hi
            ]
            replay_path = [
                row for row in shared_path
                if int(row.get("close_time") or row.get("time") or 0) >= lo - warmup_ms
                and int(row.get("open_time") or row.get("time") or 0) < hi
            ]
            warm = run_backtest(
                "portfolio", rows, sigmas=sigmas,
                overrides={**fixed_follow, "AMBIGUOUS_PATH_MODE": "liquidate"},
                market_ctx=market_ctx, price_path=replay_path, price_path_meta=shared_meta,
            )
            result = slice_backtest_result(warm, lo, window_days=10)
            open_rate = result.get("actionable_open_rate", result.get("open_fill_rate"))
            capacity = result.get("capacity_open_fit")
            folds.append(core_formation.PrefixEvaluation(
                count=len(key), net_pnl=f(result.get("copy_net_pnl")),
                stress_net_pnl=f(result.get("copy_net_pnl")),
                max_drawdown=f(result.get("max_drawdown")),
                actionable_open_rate=1.0 if open_rate is None else f(open_rate),
                capacity_fit=1.0 if capacity is None else f(capacity),
                liquidations=int(result.get("liquidations") or 0), params=tuned_params,
                payload={"initialBalance": f(base_follow.get("INITIAL_BALANCE") or config.INITIAL_BALANCE)},
            ))
        holdout_start = now_ms - 10 * 86_400_000
        holdout_rows = [
            row for row in all_rows
            if int(row.get("time") or 0) >= holdout_start - warmup_ms
        ]
        holdout_path = [
            row for row in shared_path
            if int(row.get("close_time") or row.get("time") or 0) >= holdout_start - warmup_ms
        ]
        stress_warm = run_backtest(
            "portfolio", holdout_rows, sigmas=sigmas,
            overrides={
                **fixed_follow, "AMBIGUOUS_PATH_MODE": "liquidate", "REPLAY_COST_MULT": 1.5,
            },
            market_ctx=market_ctx, price_path=holdout_path, price_path_meta=shared_meta,
        )
        stress = slice_backtest_result(stress_warm, holdout_start, window_days=10)
        fold_cache[key] = (folds, f(stress.get("copy_net_pnl")))
        return fold_cache[key]

    previous_qualified = tuple(
        addr for addr in current_core
        if addr in set(ordered) and (
            (effective_qualifications.get(addr) or {}).get("coreEligible")
        )
    )
    baseline_eval = evaluate_members(previous_qualified) if previous_qualified else None
    baseline_folds, _baseline_cost_stress = fold_replays(previous_qualified)
    initial_margin_equity = float(config.INITIAL_BALANCE)
    robust_cache = {}

    def validate_members(addrs):
        key = tuple(sorted(dict.fromkeys(addrs)))
        if key in robust_cache:
            return robust_cache[key]
        value = evaluate_members(key)
        folds, cost_stress = fold_replays(key)
        windows, stressed = membership_replay_cache[key]
        primary = windows.get(30) or windows.get(max(windows)) or {}
        contributions = {}
        for position in list(primary.get("positions") or ()) + list(primary.get("open_positions") or ()):
            addr = (position.get("addr") or "").lower()
            pnl = f(position.get("net_pnl"))
            if position.get("status") == "open":
                pnl += f(position.get("unrealized_pnl"))
            contributions[addr] = contributions.get(addr, 0.0) + pnl
        top_wallet = max(contributions, key=contributions.get) if contributions else None
        without_top = tuple(addr for addr in key if addr != top_wallet)
        if without_top:
            without_value = evaluate_members(without_top)
            top_normal = without_value.net_pnl
            top_stress = without_value.stress_net_pnl
        else:
            top_normal = top_stress = 0.0
        all_strong = bool(key) and all(
            (effective_qualifications.get(addr) or {}).get("strongEntry") for addr in key
        )
        membership_changed = bool(previous_qualified and set(key) != set(previous_qualified))
        # Only removing/replacing an otherwise qualified incumbent pays the anti-churn replacement hurdle.
        # A pure addition is judged by positive marginal net plus the same fold/holdout/stress safeguards.
        replacing = bool(
            previous_qualified and not set(previous_qualified).issubset(set(key))
        )
        check = core_formation.validate_final_membership(
            value, folds, cost_stress_net=cost_stress,
            baseline=baseline_eval, baseline_folds=baseline_folds,
            membership_changed=membership_changed,
            replacing_qualified_core=replacing,
            initial_margin_equity=initial_margin_equity,
            min_relative_utility_gain=float(config.SELECTION_MIN_RELATIVE_GAIN),
            min_net_return_gain=float(getattr(config, "CORE_REPLACEMENT_MIN_NET_RETURN", 0.02)),
            tail_after_top1=primary.get("net_after_top1"),
            tail_after_top2=primary.get("net_after_top2"),
            min_tail_return=float(load_copy_policy().min_tail_return_30d),
            top_wallet_normal_net=top_normal,
            top_wallet_stress_net=top_stress,
            all_members_strong=all_strong,
        )
        check.update({
            "addrs": list(key), "netPnl": value.net_pnl, "utility": value.utility,
            "costStressNetPnl": cost_stress, "topWallet": top_wallet,
            "topWalletRemovalNetPnl": top_normal,
            "topWalletRemovalStressNetPnl": top_stress,
            "tailAfterTop1": primary.get("net_after_top1"),
            "tailAfterTop2": primary.get("net_after_top2"),
            "profitConcentration": primary.get("pnl_concentration") or {},
        })
        robust_cache[key] = check
        return check

    finalist_limit = max(1, int(getattr(config, "CORE_SEARCH_ROBUST_FINALISTS", 12) or 12))
    finalist_pool = {
        key: value for key, value in membership_eval_cache.items()
        if key and value.feasible and effective_pinned.issubset(key)
    }
    # A bounded add/swap search can return a winner which was not retained in the generic replay cache's
    # highest-utility slice.  Include it in the same ordering, but never give it artificial priority over a
    # better robust finalist merely because it was the search algorithm's terminal node.
    chosen_key = tuple(sorted(chosen_addrs))
    finalist_pool[chosen_key] = chosen
    finalist_states = sorted(
        finalist_pool.items(),
        key=lambda item: (
            item[1].utility, item[1].net_pnl, item[1].stress_net_pnl, -len(item[0]), item[0],
        ),
        reverse=True,
    )[:finalist_limit]
    if chosen_key not in {key for key, _value in finalist_states}:
        finalist_states.append((chosen_key, chosen))
    robust_audit = []
    robust_winner = None
    for key, value in finalist_states:
        check = validate_members(key)
        robust_audit.append(check)
        if check.get("eligible"):
            robust_winner = (key, value, check)
            break
    if robust_winner is None:
        # An explicit zero-Core generation is safer than silently keeping wallets which failed the latest
        # strict evidence. Existing positions are materialized as Exit-only by the selection builder.
        chosen_addrs = ()
        chosen = core_formation.PrefixEvaluation(
            count=0, net_pnl=0.0, stress_net_pnl=0.0, max_drawdown=0.0,
            actionable_open_rate=1.0, capacity_fit=1.0, liquidations=0,
            params=tuned_params,
            payload={"initialBalance": f(base_follow.get("INITIAL_BALANCE") or config.INITIAL_BALANCE)},
        )
        robust_check = {
            "eligible": False, "reason": "no_robust_quality_membership",
            "explicitEmptyCore": True,
        }
        previous_qualified = ()
    else:
        chosen_addrs, chosen, robust_check = robust_winner
    stability_applied = False
    stable_additions = []
    if not rebalance_due and previous_qualified:
        # Between weekly rebalances, preserve every incumbent which still clears today's individual hard
        # gates.  A liquidation/forward-loss/campaign failure is removed immediately, but it must not give
        # the optimizer permission to churn the other sound incumbents during the same daily refresh.
        stable = [addr for addr in current_core if addr in set(previous_qualified)]
        hard_removed = [addr for addr in current_core if addr not in set(previous_qualified)]
        if len(stable) < target_min:
            for candidate in tuple(chosen_addrs) + tuple(ordered):
                if candidate in stable:
                    continue
                trial = tuple(stable + [candidate])
                if validate_members(trial).get("eligible"):
                    stable.append(candidate)
                    stable_additions.append(candidate)
                if len(stable) >= min(target_min, upper):
                    break
        chosen_addrs = tuple(stable)
        chosen = evaluate_members(chosen_addrs)
        robust_check = {
            "eligible": True,
            "stableRetention": True,
            "reason": (
                "daily_hard_failures_removed" if hard_removed
                else "weekly_rebalance_not_due"
            ),
            "hardRemoved": hard_removed,
        }
        stability_applied = True
    # Pre-validate any strict LOO result. Publication may remove a negative incremental member only when
    # the resulting set has passed these same membership stress rules.
    robust_allowed = {tuple(sorted(chosen_addrs))}
    for outgoing in chosen_addrs:
        if outgoing in effective_pinned:
            continue
        smaller = tuple(addr for addr in chosen_addrs if addr != outgoing)
        if smaller:
            check = validate_members(smaller)
            robust_audit.append(check)
            if check.get("eligible"):
                robust_allowed.add(tuple(sorted(smaller)))
    evaluations = tuple({
        "count": value.count, "netPnl": value.net_pnl,
        "stressNetPnl": value.stress_net_pnl, "maxDrawdown": value.max_drawdown,
        "openRate": value.actionable_open_rate, "capacityFit": value.capacity_fit,
        "liquidations": value.liquidations, "utility": value.utility,
        "feasible": bool(value.feasible),
        "retainsReference": (
            core_formation.retains_reference(prefix_search.reference, value, **retention)
            if prefix_search.reference.feasible else value.feasible
        ),
    } for value in prefix_search.evaluated)
    tune_evaluations = tuple({
        "count": value.count,
        "netPnl": value.net_pnl,
        "stressNetPnl": value.stress_net_pnl,
        "maxDrawdown": value.max_drawdown,
        "openRate": value.actionable_open_rate,
        "capacityFit": value.capacity_fit,
        "liquidations": value.liquidations,
        "utility": value.utility,
        "feasible": bool(value.feasible),
    } for value in (tune_search.evaluated if tune_search is not None else ()))
    return {
        "selected": chosen_addrs, "ranked": ordered,
        "params": dict(chosen.params), "evaluations": evaluations,
        "qualifications": effective_qualifications, "scores": effective_scores,
        "search": {
            "algorithm": "quality_membership_joint_tune_v5", "initialCount": len(ordered),
            "selectedCount": len(chosen_addrs), "boundary": prefix_search.boundary,
            "evaluatedCounts": [value.count for value in prefix_search.evaluated],
            "evaluations": evaluations,
            "membershipAlgorithm": membership_search.algorithm,
            "membershipEvaluated": membership_search.evaluated,
            "membershipSelected": list(chosen_addrs),
            "membershipRobustAudit": robust_audit,
            "explicitEmptyCore": bool(robust_check.get("explicitEmptyCore")),
            "robustAllowedMemberships": [list(key) for key in sorted(robust_allowed)],
            "singleWalletDependencyWarning": bool(
                robust_check.get("singleWalletDependencyWarning")
            ),
            "rebalanceDue": rebalance_due,
            "coreAgeDays": core_age_days,
            "rebalanceIntervalDays": rebalance_interval,
            "targetMinCount": target_min,
            "stableRetentionApplied": stability_applied,
            "stableAdditions": stable_additions,
            "operatorStarred": list(pinned_order),
            "effectiveStarred": list(effective_pinned_order),
            "tunePoolCount": len(tune_ordered),
            "tunedInputCount": (
                int(tune_search.selected.count) if tune_search is not None else len(tune_ordered)
            ),
            "fullTuneRuns": len(tune_runs),
            "tuneBoundary": tune_search.boundary if tune_search is not None else None,
            "tuneEvaluatedCounts": (
                [value.count for value in tune_search.evaluated] if tune_search is not None else []
            ),
            "tuneEvaluations": tune_evaluations,
            "effectiveRejected": qualification_rejected,
            "formationTuneEligible": tune_eligible,
            "formationTuneReason": tune_reason,
            "tuneCoverageFallback": tune_coverage_fallback,
            "formationTuneFinalists": list(chosen_run.get("finalists") or ()),
            "formationMarginRounds": list(chosen_run.get("margin_rounds") or ()),
            "qualificationRejected": qualification_rejected,
            "admission": admission_audit,
        },
    }


def _apply_formation_params(db, formation, stamp) -> bool:
    """Stage the chosen tuning surface in the caller's publication transaction."""
    proposal = dict((formation or {}).get("params") or {})
    if not proposal:
        return False
    keys = (*auto_tune.TUNE_KEYS, *auto_tune.ADD_TUNE_KEYS)
    missing = [key for key in keys if key not in proposal]
    if missing:
        raise RuntimeError(f"core_formation_params_incomplete:{len(missing)}")
    previous_follow = params.load_follow(db)
    old = {key: f(previous_follow.get(key)) for key in keys}
    changed = any(abs(old[key] - f(proposal[key])) > 1e-12 for key in keys)
    auto_tune._write_tune_params(db, proposal)
    auto_tune._write_add_params(db, proposal)
    if changed:
        auto_tune._state_set(db, "active_tune_rollback", {
            "appliedAt": stamp,
            "addrs": list((formation or {}).get("selected") or ()),
            "oldParams": old,
            "newParams": proposal,
            "resolved": False,
        })
    return changed


def _portfolio_replay_input_diagnostics(db, addrs, now_ms, window_fills=None) -> dict:
    """Compact, address-free evidence for explaining why portfolio inputs were unavailable."""
    owners = sorted({(addr or "").lower() for addr in addrs if addr})
    warmup_days = int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
    tune_days = auto_tune._tune_days()
    start_ms = int(now_ms) - (max(tune_days) + warmup_days) * 86_400_000
    if not owners:
        return {"candidates": 0, "rawRows": 0, "rawBytes": 0, "policies": 0, "usable": {}}
    marks = ",".join("?" for _ in owners)
    raw = db.execute(
        f"SELECT COUNT(*),COALESCE(SUM(LENGTH(fill_json)),0) FROM candidate_fills "
        f"WHERE lower(addr) IN ({marks}) AND time>=?",
        (*owners, start_ms),
    ).fetchone()
    policy_rows = db.execute(
        f"SELECT sector_policy_json FROM watchlist WHERE lower(addr) IN ({marks})",
        tuple(owners),
    ).fetchall()
    valid_policies = 0
    for (raw_policy,) in policy_rows:
        try:
            policy = json.loads(raw_policy or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(policy, dict) and policy.get("allowed"):
            valid_policies += 1
    return {
        "candidates": len(owners),
        "rawRows": int((raw[0] if raw else 0) or 0),
        "rawBytes": int((raw[1] if raw else 0) or 0),
        "maxBytes": int(getattr(
            config, "AUTO_TUNE_FILL_CACHE_MAX_BYTES", 64 * 1024 * 1024,
        ) or 0),
        "policies": len(policy_rows),
        "validPolicies": valid_policies,
        "usable": {
            int(days): len(rows) for days, rows in (window_fills or {}).items()
        },
    }


def _prefetch_selection_paths(db, candidates, now_ms, generation_id) -> dict:
    """Incrementally prepare the bounded candidate path cache without profile/fill refetch."""
    candidates = list(dict.fromkeys((addr or "").lower() for addr in candidates if addr))
    if not candidates:
        return {"candidates": 0, "fills": 0, "pathRows": 0, "coverage": 1.0}
    path_start = int(now_ms) - (
        30 + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7))
    ) * 86_400_000
    fills = load_copyable_fills(db, candidates, path_start)
    follow = params.load_follow(db)
    if "SMART_ADD" in follow:
        follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
    rows, meta = auto_tune.prepare_refined_price_path(
        db, fills, path_start, int(now_ms),
        sigmas=auto_tune._load_sigmas(db, generation_id), overrides=follow,
        market_ctx=auto_tune._load_market_ctx(db, generation_id),
        immutable_market_ctx=True,
    )
    return {
        "candidates": len(candidates),
        "fills": len(fills),
        "pathRows": len(rows),
        "coverage": float(meta.get("coverage") or 0.0),
        "missingCoins": len(meta.get("missingCoins") or ()),
    }


def _build_forced_prefix_selection(db, generation_id, stamp, now_ms, *, profiles,
                                   previous_roles, controls, held,
                                   desired_order, formation_meta,
                                   effective_qualifications=None, effective_scores=None):
    """Materialize the jointly tuned, skip-aware quality membership selected during formation."""
    policy_values = {**params.load_follow(db), **params.load_category(db, "scanner")}
    copy_policy = load_copy_policy(policy_values)
    by_addr = {(row.get("addr") or "").lower(): row for row in profiles}
    pinned_controls = selection.pinned_core_controls(db)
    pinned_enabled_order = tuple(item["addr"] for item in pinned_controls if item["enabled"])
    for addr, qualification in dict(effective_qualifications or {}).items():
        addr = (addr or "").lower()
        if addr in by_addr:
            by_addr[addr]["follow_qualification"] = dict(qualification or {})
            if (qualification or {}).get("eligible"):
                # A cold-start parameter probe was intentionally not active on the seeded surface.  Once
                # the sealed surface clears the real public qualification line, treat that exact replay as
                # authoritative for this publication transaction.
                by_addr[addr]["status"] = "active"
    for addr, score in dict(effective_scores or {}).items():
        addr = (addr or "").lower()
        if addr in by_addr:
            by_addr[addr]["follow_score"] = f(score)
    profiles.sort(key=lambda row: (-(row.get("follow_score") or 0.0), row.get("addr") or ""))
    for rank, row in enumerate(profiles, 1):
        row["rank"] = rank
    desired = tuple(dict.fromkeys((addr or "").lower() for addr in desired_order if addr))
    declared_effective_pins = tuple(
        (addr or "").lower()
        for addr in (formation_meta or {}).get("effectiveStarred") or ()
        if addr
    )
    effective_pinned_order = tuple(
        addr for addr in pinned_enabled_order
        if addr in desired
        and (by_addr.get(addr, {}).get("follow_qualification") or {}).get("coreEligible")
    )
    missing_required = [addr for addr in declared_effective_pins if addr not in effective_pinned_order]
    if missing_required:
        raise RuntimeError(f"quality_prefix_missing_pinned_wallets:{len(missing_required)}")
    invalid = [
        addr for addr in desired
        if addr not in by_addr
        or by_addr[addr].get("profile_generation") != generation_id
        or by_addr[addr].get("status") not in {"active", "qualified"}
        or not (by_addr[addr].get("follow_qualification") or {}).get("coreEligible")
        or (by_addr[addr].get("data_status") or "valid") != "valid"
        or not controls.get(addr, True)
    ]
    if invalid:
        raise RuntimeError(f"quality_prefix_contains_ineligible_wallets:{len(invalid)}")

    eval_cache = {}
    if desired:
        window_fills = auto_tune._portfolio_window_fills(db, list(desired), int(now_ms))
        if window_fills is None or not any(window_fills.values()):
            raise RuntimeError("quality_prefix_replay_unavailable")
        follow = params.load_follow(db)
        if "SMART_ADD" in follow:
            follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
        sigmas = auto_tune._load_sigmas(db, generation_id)
        market_ctx = auto_tune._load_market_ctx(db, generation_id)
        all_fills = list(window_fills.get(max(window_fills)) or [])
        path_start = int(now_ms) - (
            max(window_fills) + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7))
        ) * 86_400_000
        shared_path = price_path.load_refined(db, all_fills, path_start, int(now_ms))
        shared_meta = price_path.coverage(db, all_fills, path_start, int(now_ms))

        def strict_evaluate(addrs):
            key = tuple(sorted(addrs))
            if key in eval_cache:
                return eval_cache[key]
            if not key:
                value = _portfolio_selection_metrics({}, selected_n=0)
            else:
                filtered = auto_tune._filter_window_fills_by_addr(window_fills, key)
                windows = auto_tune._candidate_windows(
                    db, list(key), sigmas,
                    {**follow, "AMBIGUOUS_PATH_MODE": "liquidate"}, int(now_ms),
                    window_fills=filtered, market_ctx=market_ctx,
                    path_rows=shared_path, path_meta=shared_meta,
                )
                value = _portfolio_selection_metrics(windows, selected_n=len(key))
            eval_cache[key] = value
            return value
    else:
        def strict_evaluate(addrs):
            return _portfolio_selection_metrics({}, selected_n=0)

    transition = _quality_first_core_transition(
        profiles,
        generation_id=generation_id,
        previous_roles=previous_roles,
        controls=controls,
        desired_order=desired,
        strict_evaluate=strict_evaluate,
        robust_allowed_memberships=(formation_meta or {}).get("robustAllowedMemberships") or (),
        pinned_order=effective_pinned_order,
    )
    selected_enabled_set = set(transition["selected"])
    core_order = effective_pinned_order + tuple(
        addr for addr in transition["selected"] if addr not in set(effective_pinned_order)
    )
    selected_set = set(core_order)
    core_rank = {addr: rank for rank, addr in enumerate(core_order, 1)}
    previous_core = {addr for addr, role in previous_roles.items() if role == selection.CORE}
    explicit_empty_core = bool((formation_meta or {}).get("explicitEmptyCore"))
    marginal = selection.MarginalSelectionResult(
        selected=transition["selected"],
        baseline=strict_evaluate(tuple(sorted(previous_core & set(by_addr)))),
        metrics=transition["metrics"],
        action="quality_prefix_rebuild",
        added=tuple(sorted(selected_enabled_set - previous_core)),
        removed=tuple(sorted(previous_core - selected_set)),
        evaluated=len(eval_cache),
        search_meta={
            **dict(formation_meta or {}),
            "membershipPolicy": "quality-prefix-current-evidence-loo-v3",
            "desiredOrder": desired,
            "contributionOrder": transition["selected"],
            "looRemoved": list(transition.get("looRemoved") or ()),
        },
    )
    transition_reasons = transition.get("reasons") or {}
    rows = []
    for rank, row in enumerate(profiles, 1):
        addr = (row.get("addr") or "").lower()
        enabled = controls.get(addr, True)
        refreshed = row.get("profile_generation") == generation_id
        data_status = row.get("data_status") or "valid"
        selection_data_status = data_status if refreshed or data_status == "deferred_data_error" else "stale"
        active = row.get("status") in {"active", "qualified"}
        qualification = row.get("follow_qualification") or {}
        candidate_ok = active and bool(qualification.get("eligible"))
        include = True
        if addr in selected_set and enabled:
            role = selection.CORE
            reason = (
                "operator_starred_core" if addr in set(effective_pinned_order)
                else transition_reasons.get(addr, "core_quality_selected")
            )
        elif explicit_empty_core and addr in previous_core:
            role, reason = selection.EXIT_ONLY, "no_robust_core_latest_evidence:exit_only"
            enabled = False
        elif addr in held and data_status != "valid":
            role, reason = selection.EXIT_ONLY, transition_reasons.get(addr, "exit_only_open_position")
        elif data_status != "valid":
            role = selection.QUARANTINE
            reason = "deferred_data_error" if data_status == "deferred_data_error" else "copy_data_error"
            include = False
        elif candidate_ok:
            role = selection.CHALLENGER
            if not enabled:
                reason = "operator_disabled"
            elif qualification.get("coreEligible"):
                reason = transition_reasons.get(addr, "portfolio_not_selected")
            else:
                reason = qualification.get("status") or "sample_observation"
            if addr in held:
                reason = f"{reason}:exit_pending"
        elif addr in held:
            role, reason = selection.EXIT_ONLY, transition_reasons.get(addr, "exit_only_open_position")
        else:
            role = selection.REJECTED
            reason = qualification.get("status") or row.get("reason") or "not_qualified"
            include = False
        if include:
            rows.append(selection.SelectionRow(
                addr=addr, role=role, enabled=enabled, reason=reason,
                utility=transition.get("utilities", {}).get(addr, f(row.get("follow_score"))),
                follow_score=f(row.get("follow_score")),
                selection_rank=core_rank.get(addr) if role == selection.CORE else rank,
                data_status=selection_data_status,
                evidence_status=row.get("evidence_status") or "",
                model_version="selection-quality-profit-add-v2-robust-v1",
                policy_version=copy_policy.version,
                acct_value=row.get("acct_value"),
                sector_policy_json=row.get("sector_policy_json"),
            ))
        lifecycle_state = role if role in {
            selection.CORE, selection.CHALLENGER, selection.EXIT_ONLY
        } else "quarantine" if role == selection.QUARANTINE else "rejected"
        upsert_wallet_registry(
            db, addr, generation=generation_id, seen_at=stamp,
            state=lifecycle_state,
            role=role if role in {selection.CORE, selection.CHALLENGER, selection.EXIT_ONLY} else None,
            data_status=selection_data_status, reason=reason,
            last_actionable_open_ms=row.get("last_copyable_open_ms"),
        )
        db.execute(
            "UPDATE profile SET selection_marginal_utility=? WHERE addr=?",
            (transition.get("utilities", {}).get(addr), addr),
        )
    missing_policy = []
    for item in rows:
        if item.role != selection.CORE or not item.enabled:
            continue
        try:
            policy = json.loads(item.sector_policy_json or "{}")
        except (TypeError, ValueError):
            policy = {}
        if not policy.get("allowed"):
            missing_policy.append(item.addr)
    if missing_policy:
        raise RuntimeError(f"selection_core_policy_missing:{len(missing_policy)}")
    return rows, marginal


def _build_explicit_selection(db, generation_id, stamp, now_ms, *, force_cold_bootstrap=False,
                              validate_price_path=True, audit_stamp=None,
                              forced_core_order=None, formation_meta=None,
                              effective_qualifications=None, effective_scores=None):
    """Build Core/Challenger roles and optimize shared-account membership to a stable set."""
    policy_values = {**params.load_follow(db), **params.load_category(db, "scanner")}
    copy_policy = load_copy_policy(policy_values)
    previous_generation = None if force_cold_bootstrap else selection.latest_published_generation(db)
    previous_roles = {}
    previous_selection = {}
    if previous_generation:
        previous_selection = {
            row.addr: row for row in selection.current_selection_rows(db)
        }
        previous_roles = {addr: row.role for addr, row in previous_selection.items()}

    held = {(addr or "").lower() for (addr,) in db.execute(
        "SELECT DISTINCT addr FROM copy_position WHERE status='open'"
    ).fetchall()}
    controls = {
        (addr or "").lower(): bool(enabled)
        for addr, enabled in db.execute("SELECT addr,enabled FROM target_controls").fetchall()
    }
    cur = db.execute(
        "SELECT p.addr,p.status,p.reason,p.score,p.profile_generation,p.data_status,p.evidence_status,p.last_copyable_open_ms,"
        "p.copy_bt_closed_n,p.copy_bt_14d_closed_n,p.copy_bt_7d_closed_n,"
        "p.copy_positive_probability,p.copy_expected_return,p.copy_return_lcb,p.copy_return_volatility,"
        "p.copy_evidence_days,p.copy_recent_return_14d,p.copy_recent_return_7d,p.copy_risk_score,"
        "p.execution_score,p.open_probability_48h,"
        "p.actionable_open_rate,p.capacity_fit,p.copy_bt_net_pnl,p.copy_bt_unrealized_pnl,p.copy_bt_valuation_status,"
        "p.copy_bt_14d_net_pnl,p.copy_bt_14d_unrealized_pnl,p.copy_bt_7d_net_pnl,p.copy_bt_7d_unrealized_pnl,"
        "p.copy_bt_open_fill_rate,p.copy_bt_liquidations,p.copy_bt_fee_drag,p.sector_copy_json,p.sector_policy_json,p.acct_value "
        "FROM profile p"
    )
    names = [desc[0] for desc in cur.description]
    profiles = [dict(zip(names, row)) for row in cur.fetchall()]
    forward_risk = {
        (addr or "").lower(): {
            "forward_net_pnl": f(net_pnl),
            "forward_liquidations": int(liquidations or 0),
            "forward_closed_n": int(closed_n or 0),
        }
        for addr, net_pnl, liquidations, closed_n in db.execute(
            "SELECT addr,COALESCE(SUM(COALESCE(realized_pnl,0)+CASE WHEN status='open' "
            "THEN COALESCE(unrealized_pnl,0) ELSE 0 END),0),"
            "SUM(CASE WHEN COALESCE(was_liq,0)=1 AND julianday(closed_at)>=julianday('now','-30 days') "
            "THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END) FROM copy_position GROUP BY lower(addr)"
        ).fetchall()
    }
    live_wallet_risk = {
        (addr or "").lower(): {
            "wallet_breaker_stage": int(stage or 0),
            "wallet_cooldown_until_ms": cooldown,
            "wallet_drawdown_frac": f(drawdown),
            "wallet_risk_active_member": bool(active_member),
        }
        for addr, stage, cooldown, drawdown, active_member in db.execute(
            "SELECT addr,breaker_stage,cooldown_until_ms,drawdown_frac,active_member "
            "FROM wallet_risk_state WHERE execution_book='paper'"
        ).fetchall()
    }
    # watchlist.score is the published final Copy-follow score.  Selection must consume that exact value
    # rather than recomputing from a narrower row projection and creating an invisible second score line.
    watch_scores = {
        (addr or "").lower(): score
        for addr, score in db.execute("SELECT addr,score FROM watchlist").fetchall()
    }
    margin_equity_pct = params.load_follow(db).get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
    pinned_addrs = {
        item["addr"] for item in selection.pinned_core_controls(db)
    }
    for row in profiles:
        addr = (row.get("addr") or "").lower()
        row.update(forward_risk.get(addr) or {})
        row.update(live_wallet_risk.get(addr) or {})
        if addr in pinned_addrs:
            try:
                current_policy = json.loads(row.get("sector_policy_json") or "{}")
            except (TypeError, ValueError):
                current_policy = {}
            prior = previous_selection.get(addr)
            if not current_policy.get("allowed") and prior and prior.sector_policy_json:
                row["sector_policy_json"] = prior.sector_policy_json
        row["follow_score"] = (
            f(watch_scores[addr]) if addr in watch_scores
            else follow_score.compute_follow_score(row)[0]
        )
        row["follow_qualification"] = follow_score.evaluate_follow_eligibility({
            **row,
            "copy_bt_data_status": row.get("data_status"),
            "copy_bt_evidence_status": row.get("evidence_status"),
        }, margin_equity_pct=margin_equity_pct, policy_values=policy_values)
    profiles.sort(key=lambda row: (-(row.get("follow_score") or 0.0), row.get("addr") or ""))
    for rank, row in enumerate(profiles, 1):
        row["rank"] = rank
    selection_mode = str(
        params.get(db, "FOLLOW_SELECTION_MODE", config.FOLLOW_SELECTION_MODE) or "auto"
    ).lower()
    if forced_core_order is not None:
        if selection_mode != "auto":
            raise RuntimeError("quality-prefix formation requires FOLLOW_SELECTION_MODE=auto")
        return _build_forced_prefix_selection(
            db, generation_id, stamp, now_ms,
            profiles=profiles, previous_roles=previous_roles, controls=controls,
            held=held, desired_order=tuple(forced_core_order),
            formation_meta=dict(formation_meta or {}),
            effective_qualifications=effective_qualifications,
            effective_scores=effective_scores,
        )
    if selection_mode == "auto":
        # Active means the wallet itself is structurally/economically copyable.  Score orders the bounded
        # candidate pool; the shared-account replay then keeps candidates whose added net profit exceeds
        # their added max-drawdown dollars. Individual sample/win/Wilson/recent-body/liquidation gates have
        # already run before this shared-account membership stage.
        ranked_candidates = [
            (row.get("addr") or "").lower() for row in profiles
            if row.get("status") in {"active", "qualified"}
            and (row.get("follow_qualification") or {}).get("coreEligible")
            and controls.get((row.get("addr") or "").lower(), True)
        ]
        # Active incumbents remain inside the bounded optimizer even when a one-day score move pushes them
        # below the Challenger cutoff.  Otherwise rank truncation itself becomes an immediate silent exit.
        incumbent_candidates = [
            addr for addr in ranked_candidates if previous_roles.get(addr) == selection.CORE
        ]
        portfolio_candidates = list(dict.fromkeys(
            incumbent_candidates + ranked_candidates
        ))[:int(config.MAX_TARGETS)]
        # If canonical portfolio replay is unavailable, preserve only a still-qualified published Core.
        # Never fabricate production targets from a retired score threshold.
        selected_set = {
            addr for addr in portfolio_candidates if previous_roles.get(addr) == selection.CORE
        }
        portfolio_rejections = {}
        portfolio_utilities = {}
        core_rank = {}
        marginal = None
        evaluate = None
        path_fallback = False
        if portfolio_candidates:
            replay_addrs = list(dict.fromkeys(
                portfolio_candidates + [
                    addr for addr, role in previous_roles.items()
                    if role == selection.CORE and controls.get(addr, True)
                ]
            ))
            window_fills = auto_tune._portfolio_window_fills(db, replay_addrs, now_ms)
            if (window_fills is None or not any(window_fills.values())) and previous_generation:
                # Selection did not run. Publishing the still-qualified intersection of the old Core and
                # the new profile set silently shrinks membership without portfolio evidence. Fail the
                # generation so the complete previous published selection remains authoritative.
                diagnostic = _portfolio_replay_input_diagnostics(
                    db, portfolio_candidates, now_ms, window_fills,
                )
                raise RuntimeError(
                    "selection_portfolio_replay_unavailable:"
                    + json.dumps(diagnostic, sort_keys=True, separators=(",", ":"))
                )
            if window_fills is not None and any(window_fills.values()):
                follow = params.load_follow(db)
                if "SMART_ADD" in follow:
                    follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
                # Selection must evaluate candidates under the parameters Observer is actually running.
                # Historical tune/add baselines are rollback metadata only; using them here can change Core
                # membership for a strategy surface that is no longer active.
                neutral_tune = {key: f(follow[key]) for key in auto_tune.TUNE_KEYS}
                neutral_add = {key: f(follow[key]) for key in auto_tune.ADD_TUNE_KEYS}
                neutral_follow = {**follow, **neutral_tune, **neutral_add}
                if "SMART_ADD" in neutral_follow:
                    neutral_follow["ADD_STRATEGY"] = (
                        "smart" if neutral_follow["SMART_ADD"] else "hardcap"
                    )
                sigmas = auto_tune._load_sigmas(db, generation_id)
                market_ctx = auto_tune._load_market_ctx(db, generation_id)
                eval_cache = {}
                validation_cache = {}
                fold_validation_cache = {}
                path_validation_details = {}
                path_start = now_ms - (
                    30 + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7))
                ) * 86_400_000
                all_selection_fills = list(window_fills.get(30) or [])
                shared_path_rows = (
                    price_path.load_refined(
                        db, all_selection_fills, path_start, now_ms,
                    ) if validate_price_path else []
                )

                def evaluate(addrs):
                    key = tuple(sorted(addrs))
                    if key in eval_cache:
                        return eval_cache[key]
                    allowed = set(key)
                    # Fast discovery ranks on the primary 30-day window only. Avoid rebuilding unused 14/7
                    # lists for every candidate; strict finalists receive full multi-window replay below.
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
                    if not key:
                        validation_cache[key] = _portfolio_selection_metrics(
                            {}, baseline_n=0, selected_n=0,
                        )
                        return validation_cache[key]
                    filtered = auto_tune._filter_window_fills_by_addr(window_fills, key)
                    path_reasons = []
                    path_meta = {}
                    path_primary = None
                    normal_primary = None
                    selected_fills = []
                    if validate_price_path:
                        selected_fills = list(filtered.get(30) or [])
                        path_meta = price_path.coverage(
                            db, selected_fills, path_start, now_ms,
                        )
                        strict_follow = {**follow, "AMBIGUOUS_PATH_MODE": "liquidate"}
                        normal_windows = auto_tune._candidate_windows(
                            db, list(key), sigmas, follow, now_ms,
                            window_fills=filtered, market_ctx=market_ctx,
                            path_rows=shared_path_rows, path_meta=path_meta,
                        )
                        strict_windows = auto_tune._candidate_windows(
                            db, list(key), sigmas, strict_follow, now_ms,
                            window_fills=filtered, market_ctx=market_ctx,
                            path_rows=shared_path_rows, path_meta=path_meta,
                        )
                        normal_primary = normal_windows.get(30) or normal_windows.get(
                            max(normal_windows, default=30)
                        )
                        path_primary = strict_windows.get(30) or strict_windows.get(
                            max(strict_windows, default=30)
                        )
                        coverage = float(path_meta.get("coverage") or 0.0)
                        maintenance = min(
                            f((normal_primary or {}).get("maintenance_margin_coverage")),
                            f((path_primary or {}).get("maintenance_margin_coverage")),
                        )
                        if coverage < float(getattr(
                                config, "CORE_PRICE_PATH_MIN_COVERAGE", .95)):
                            path_reasons.append("price_path_coverage_low")
                        if maintenance < float(getattr(
                                config, "CORE_MAINTENANCE_META_MIN_COVERAGE", .95)):
                            path_reasons.append("maintenance_margin_coverage_low")
                        if min(
                            f((normal_primary or {}).get("copy_net_pnl")),
                            f((path_primary or {}).get("copy_net_pnl")),
                        ) <= 0:
                            path_reasons.append("path_net_nonpositive")
                        normal_metrics = _portfolio_selection_metrics(
                            normal_windows, baseline_n=0, selected_n=len(key),
                        )
                        strict_metrics = _portfolio_selection_metrics(
                            strict_windows, baseline_n=0, selected_n=len(key),
                        )
                        value = offline_core_optimizer.conservative_metrics(
                            normal_metrics, strict_metrics,
                        )
                    else:
                        windows = auto_tune._candidate_windows(
                            db, list(key), sigmas, follow, now_ms,
                            window_fills=filtered, market_ctx=market_ctx,
                        )
                        value = _portfolio_selection_metrics(
                            windows, baseline_n=0, selected_n=len(key),
                        )
                    path_validation_details[key] = {
                        "reasons": tuple(path_reasons),
                        "coins": tuple(price_path.coins_for_fills(selected_fills))
                        if validate_price_path else (),
                        "fills": len(selected_fills) if validate_price_path else 0,
                        "coverage": float(path_meta.get("coverage") or 0.0),
                        "expectedCandles": int(path_meta.get("expected") or 0),
                        "observedCandles": int(path_meta.get("observed") or 0),
                        "missingCoins": tuple(path_meta.get("missingCoins") or ()),
                        "pathNetPnl": min(
                            f((normal_primary or {}).get("copy_net_pnl")),
                            f((path_primary or {}).get("copy_net_pnl")),
                        ),
                        "pathLiquidations": max(
                            int((normal_primary or {}).get("liquidations") or 0),
                            int((path_primary or {}).get("liquidations") or 0),
                        ),
                        "ambiguousLiquidations": max(
                            int((normal_primary or {}).get("ambiguous_liquidations") or 0),
                            int((path_primary or {}).get("ambiguous_liquidations") or 0),
                        ),
                        "pathBoundarySkips": int(
                            (path_primary or {}).get("price_path_boundary_skips") or 0
                        ),
                        "maintenanceCoverage": f(
                            (path_primary or {}).get("maintenance_margin_coverage")
                        ),
                    }
                    if path_reasons:
                        value = _portfolio_selection_metrics(
                            {}, baseline_n=0, selected_n=len(key),
                        )
                    validation_cache[key] = value
                    return validation_cache[key]

                def validate_fold(addrs, older, newer, cost_mult=1.0):
                    key = (tuple(sorted(addrs)), int(older), int(newer), float(cost_mult))
                    if key in fold_validation_cache:
                        return fold_validation_cache[key]
                    selected = key[0]
                    if not selected:
                        value = _portfolio_selection_metrics({}, selected_n=0)
                        fold_validation_cache[key] = value
                        return value
                    lo = now_ms - int(older) * 86_400_000
                    hi = now_ms - int(newer) * 86_400_000 if newer else now_ms + 1
                    warmup = int(getattr(config, "COPY_BT_WARMUP_DAYS", 7)) * 86_400_000
                    allowed = set(selected)
                    source_fills = list(window_fills.get(max(window_fills)) or [])
                    fold_fills = [
                        row for row in source_fills
                        if (row.get("user") or "").lower() in allowed
                        and lo - warmup <= int(row.get("time") or 0) < hi
                    ]
                    result = run_backtest(
                        "portfolio", fold_fills, sigmas=sigmas,
                        overrides={
                            **follow, "AMBIGUOUS_PATH_MODE": "liquidate",
                            "REPLAY_COST_MULT": float(cost_mult),
                        },
                        market_ctx=market_ctx, price_path=shared_path_rows,
                        price_path_meta=price_path.coverage(
                            db, fold_fills, path_start, now_ms,
                        ) if validate_price_path else {},
                    )
                    sliced = slice_backtest_result(
                        result, lo,
                        window_days=max(1, int(older) - int(newer)),
                    )
                    value = _portfolio_selection_metrics(
                        {max(1, int(older) - int(newer)): sliced},
                        baseline_n=0, selected_n=len(selected),
                    )
                    fold_validation_cache[key] = value
                    return value

                constraints = selection.SelectionConstraints(
                    min_relative_lcb_improvement=float(config.SELECTION_MIN_RELATIVE_GAIN),
                    min_actionable_open_rate=copy_policy.min_actionable_open_rate,
                    min_capacity_fit=copy_policy.min_capacity_fit,
                    max_drawdown_worsening=1.0,
                    max_deploy_pct=float(params.get(db, "MAX_DEPLOY_PCT", config.MAX_DEPLOY_PCT)),
                    max_cost_drag_ratio=1.0,
                    max_targets=int(config.MAX_TARGETS),
                    max_actionable_open_rate_drop=float(getattr(
                        config, "CORE_SEARCH_MAX_OPEN_RATE_DROP", .05,
                    )),
                    max_capacity_fit_drop=float(getattr(
                        config, "CORE_SEARCH_MAX_CAPACITY_FIT_DROP", .05,
                    )),
                    min_absolute_net_gain=(
                        float(config.INITIAL_BALANCE)
                        * f(follow.get("MARGIN_EQUITY_PCT") or config.MARGIN_EQUITY_PCT)
                        * float(getattr(config, "CORE_REPLACEMENT_MIN_NET_RETURN", .02))
                    ),
                )
                search_budget_s = float(getattr(
                    config, "CORE_SEARCH_TIME_BUDGET_SEC", 600,
                ))
                search_started = time.monotonic()
                search_config = offline_core_optimizer.OfflineSearchConfig(
                    finalist_limit=int(getattr(
                        config, "CORE_SEARCH_VALIDATION_FINALISTS", 12,
                    )),
                    strict_move_shortlist=int(getattr(
                        config, "CORE_SEARCH_STRICT_MOVE_SHORTLIST", 8,
                    )),
                    max_strict_moves=int(getattr(
                        config, "CORE_SEARCH_MAX_STRICT_MOVES", config.MAX_TARGETS,
                    )),
                    pair_add_limit=int(getattr(
                        config, "CORE_SEARCH_PAIR_ADD_LIMIT", 4,
                    )),
                    time_budget_s=search_budget_s,
                )
                initial_core = tuple(sorted(selected_set))
                staged_search = offline_core_optimizer.optimize_membership(
                    portfolio_candidates, initial_core, evaluate, validate,
                    constraints, search_config,
                )
                if staged_search.timed_out:
                    raise selection.SmartCoreSearchTimeout(
                        "robust_core_search_time_budget"
                    )
                robust_selected = initial_core
                robust_rounds = []
                robust_seen = {robust_selected}
                robust_eval_total = 0
                # A positive override is shared by all phases.  Production uses
                # zero (unbounded wall clock); the finite candidate/move graph,
                # visited-state guard and MAX_TARGETS still guarantee closure.
                robust_deadline = (
                    float("inf") if search_budget_s <= 0
                    else search_started + search_budget_s
                )
                for _ in range(max(1, len(portfolio_candidates))):
                    if time.monotonic() >= robust_deadline:
                        raise selection.SmartCoreSearchTimeout(
                            "robust_core_closure_time_budget"
                        )
                    robust = offline_core_optimizer.choose_robust_candidate(
                        robust_selected, portfolio_candidates,
                        (*staged_search.finalists, staged_search.selected),
                        validate, validate_fold, constraints,
                        finalist_limit=int(getattr(
                            config, "CORE_SEARCH_ROBUST_FINALISTS", 12,
                        )),
                    )
                    robust_eval_total += robust.evaluated
                    robust_rounds.append(robust)
                    next_selected = tuple(sorted(robust.selected))
                    if next_selected == robust_selected:
                        break
                    if next_selected in robust_seen:
                        raise RuntimeError("robust_core_search_cycle")
                    robust_seen.add(next_selected)
                    robust_selected = next_selected
                robust = robust_rounds[-1]
                robust_metrics = validate(robust_selected)
                # Published 1..N order is leave-one-out strict contribution, not
                # raw score order or the arbitrary tuple order of a subset.
                contributions = []
                for addr in robust_selected:
                    without = tuple(a for a in robust_selected if a != addr)
                    without_metrics = validate(without)
                    contributions.append((
                        f(robust_metrics.risk_adjusted_utility)
                        - f(without_metrics.risk_adjusted_utility),
                        f(robust_metrics.net_pnl) - f(without_metrics.net_pnl),
                        -portfolio_candidates.index(addr), addr,
                    ))
                contributions.sort(reverse=True)
                contribution_order = tuple(row[-1] for row in contributions)
                marginal = selection.MarginalSelectionResult(
                    selected=contribution_order,
                    baseline=validate(initial_core),
                    metrics=robust_metrics,
                    action="robust_multi_start",
                    added=tuple(sorted(set(robust_selected) - set(initial_core))),
                    removed=tuple(sorted(set(initial_core) - set(robust_selected))),
                    evaluated=(
                        staged_search.fast_evaluated + staged_search.strict_evaluated
                        + robust_eval_total + len(fold_validation_cache)
                    ),
                    search_meta={
                        "algorithm": "multi_start_robust_v1",
                        "selectedCount": len(robust_selected),
                        "inSampleSelectedCount": len(staged_search.selected),
                        "fastEvaluations": staged_search.fast_evaluated,
                        "strictEvaluations": staged_search.strict_evaluated,
                        "robustEvaluations": robust_eval_total,
                        "foldEvaluations": len(fold_validation_cache),
                        "finalists": len(staged_search.finalists),
                        "robustRounds": len(robust_rounds),
                        "robustFinalists": sum(
                            len(round_.audit) for round_ in robust_rounds
                        ),
                        "robustPassed": sum(
                            1 for round_ in robust_rounds for row in round_.audit
                            if row.get("eligible")
                        ),
                        "contributionOrder": contribution_order,
                        "timedOut": staged_search.timed_out,
                    },
                )
                selected_set = set(marginal.selected)
                core_rank = {
                    addr: rank for rank, addr in enumerate(marginal.selected, 1)
                }
                # Every retained finalist has already competed under the strict shared K-line path. A
                # coverage/maintenance-data failure is operational and retains the previous Core; a complete
                # strict replay that finds no profitable combination may intentionally publish an empty Core.
                nonempty_path_checks = {
                    key: detail for key, detail in path_validation_details.items() if key
                }
                path_data_unavailable = bool(
                    validate_price_path and not selected_set and nonempty_path_checks
                    and all(any(reason in {
                        "price_path_coverage_low", "maintenance_margin_coverage_low",
                    } for reason in detail.get("reasons") or ())
                            for detail in nonempty_path_checks.values())
                )
                if path_data_unavailable:
                    raise RuntimeError("selection_price_path_unavailable")
                if selected_set and validate_price_path and marginal is not None:
                    candidate_core = sorted(selected_set)
                    detail = path_validation_details.get(tuple(candidate_core))
                    path_reasons = list((detail or {}).get("reasons") or ())
                    if detail is None:
                        path_reasons = ["path_validation_missing"]
                    if path_reasons:
                        raise RuntimeError(
                            "selection_price_path_invalid:" + ",".join(path_reasons)
                        )
                    coverage_floor = float(getattr(config, "CORE_PRICE_PATH_MIN_COVERAGE", .95))
                    maintenance_floor = float(
                        getattr(config, "CORE_MAINTENANCE_META_MIN_COVERAGE", .95)
                    )
                    pipeline_audit._insert_event(
                        db,
                        stamp=audit_stamp or stamp,
                        source="scan",
                        stage="selection_path_validation",
                        status="fallback" if path_reasons else "ok",
                        reason=",".join(path_reasons) if path_reasons else "path_validated",
                        payload={
                            "generation": generation_id,
                            "candidateCore": candidate_core,
                            "effectiveCore": sorted(selected_set),
                            "coins": list((detail or {}).get("coins") or ()),
                            "fills": int((detail or {}).get("fills") or 0),
                            "fillsOnlyNetPnl": f(evaluate(tuple(candidate_core)).net_pnl),
                            "pathNetPnl": f((detail or {}).get("pathNetPnl")),
                            "pathLiquidations": int(
                                (detail or {}).get("pathLiquidations") or 0
                            ),
                            "ambiguousLiquidations": int(
                                (detail or {}).get("ambiguousLiquidations") or 0
                            ),
                            "pathBoundarySkips": int(
                                (detail or {}).get("pathBoundarySkips") or 0
                            ),
                            "coverage": float((detail or {}).get("coverage") or 0.0),
                            "coverageFloor": coverage_floor,
                            "maintenanceCoverage": f(
                                (detail or {}).get("maintenanceCoverage")
                            ),
                            "maintenanceFloor": maintenance_floor,
                            "expectedCandles": int(
                                (detail or {}).get("expectedCandles") or 0
                            ),
                            "observedCandles": int(
                                (detail or {}).get("observedCandles") or 0
                            ),
                            "missingCoins": list(
                                (detail or {}).get("missingCoins") or ()
                            ),
                            "fallback": bool(path_reasons),
                            "reasons": path_reasons,
                            "reusedStrictReplay": True,
                        },
                    )
                effective_evaluate = (
                    validate if marginal is not None and validate_price_path else evaluate
                )
                final_metrics = effective_evaluate(tuple(sorted(selected_set)))
                final_fold_metrics = None
                final_cost_stress = None
                prefix = []
                prefix_metrics = effective_evaluate(())
                for addr in marginal.selected if marginal is not None else sorted(selected_set):
                    prefix.append(addr)
                    current_metrics = effective_evaluate(tuple(sorted(prefix)))
                    portfolio_utilities[addr] = (
                        f(current_metrics.risk_adjusted_utility)
                        - f(prefix_metrics.risk_adjusted_utility)
                    )
                    prefix_metrics = current_metrics
                for addr in portfolio_candidates:
                    if addr in selected_set:
                        continue
                    if str(portfolio_rejections.get(addr) or "").startswith("path_"):
                        continue
                    trial = effective_evaluate(tuple(sorted(selected_set | {addr})))
                    reason = selection.portfolio_economic_rejection_reason(
                        final_metrics, trial, constraints,
                    )
                    if reason == "portfolio_not_selected":
                        if final_fold_metrics is None:
                            final_fold_metrics = [
                                validate_fold(
                                    tuple(sorted(selected_set)), older, newer, 1.0,
                                )
                                for older, newer in ((30, 20), (20, 10), (10, 0))
                            ]
                            final_cost_stress = validate_fold(
                                tuple(sorted(selected_set)), 10, 0, 1.5,
                            )
                        trial_addrs = tuple(sorted(selected_set | {addr}))
                        comparison = offline_core_optimizer.robust_improvement(
                            final_metrics, trial, final_fold_metrics,
                            [
                                validate_fold(trial_addrs, older, newer, 1.0)
                                for older, newer in ((30, 20), (20, 10), (10, 0))
                            ],
                            final_cost_stress,
                            validate_fold(trial_addrs, 10, 0, 1.5),
                            constraints,
                        )
                        reason_map = {
                            "fewer_than_required_fold_wins": "portfolio_fold_stability_low",
                            "fold_total_gain_below_floor": "portfolio_fold_gain_below_floor",
                            "holdout_not_better": "portfolio_holdout_not_better",
                            "cost_stress_not_better": "portfolio_cost_stress_no_gain",
                            "cost_stress_new_liquidation": "portfolio_cost_stress_liquidation",
                            "fold_infeasible": "portfolio_fold_constraints_failed",
                        }
                        reason = (
                            "portfolio_not_selected" if comparison.eligible
                            else reason_map.get(
                                next(iter(comparison.reasons), ""),
                                "portfolio_robustness_not_improved",
                            )
                        )
                    portfolio_rejections[addr] = reason

        transition = None
        if marginal is not None and evaluate is not None:
            desired_marginal = marginal
            effective_evaluate = validate if validate_price_path else evaluate
            pinned_order = tuple(
                item["addr"] for item in selection.pinned_core_controls(db, enabled_only=True)
            )
            transition = _quality_first_core_transition(
                profiles,
                generation_id=generation_id,
                previous_roles=previous_roles,
                controls=controls,
                desired_order=(*pinned_order, *desired_marginal.selected),
                strict_evaluate=effective_evaluate,
                pinned_order=pinned_order,
            )
            selected_set = set(transition["selected"])
            core_rank = {
                addr: rank for rank, addr in enumerate(transition["selected"], 1)
            }
            portfolio_utilities.update(transition["utilities"])
            previous_core_set = {
                addr for addr, role in previous_roles.items() if role == selection.CORE
            }
            search_meta = dict(desired_marginal.search_meta or {})
            search_meta.update({
                "desiredSelectedCount": len(transition["desired"]),
                "publishedSelectedCount": len(transition["selected"]),
                "desiredOrder": transition["desired"],
                "hardExitCount": len(transition["hardRemoved"]),
                "membershipPolicy": "quality-first-v1",
            })
            marginal = selection.MarginalSelectionResult(
                selected=transition["selected"],
                baseline=effective_evaluate(tuple(sorted(previous_core_set))),
                metrics=transition["metrics"],
                action=(
                    "membership_change" if previous_core_set != selected_set else "keep"
                ),
                added=tuple(sorted(selected_set - previous_core_set)),
                removed=tuple(sorted(previous_core_set - selected_set)),
                evaluated=desired_marginal.evaluated,
                search_meta=search_meta,
            )
            pipeline_audit._insert_event(
                db,
                stamp=audit_stamp or stamp,
                source="scan",
                stage="selection_membership_transition",
                status="ok",
                reason=(
                    "quality_first_change"
                    if transition["hardRemoved"]
                    else "quality_first_publish"
                ),
                payload={
                    "generation": generation_id,
                    "policy": "quality-first-v1",
                    "previousCount": len(previous_core_set),
                    "desiredCount": len(transition["desired"]),
                    "publishedCount": len(transition["selected"]),
                    "addedCount": len(selected_set - previous_core_set),
                    "removedCount": len(previous_core_set - selected_set),
                    "hardExitCount": len(transition["hardRemoved"]),
                },
            )

        transition_reasons = (transition or {}).get("reasons", {})
        rows = []
        for row in profiles:
            addr = (row["addr"] or "").lower()
            enabled = controls.get(addr, True)
            include_selection = True
            refreshed_now = row.get("profile_generation") == generation_id
            data_status = row.get("data_status") or "valid"
            selection_data_status = data_status if refreshed_now or data_status == "deferred_data_error" else "stale"
            active = row.get("status") in {"active", "qualified"}
            qualification = row.get("follow_qualification") or {}
            candidate_ok = active and bool(qualification.get("eligible"))
            if addr in selected_set and enabled:
                role = selection.CORE
                reason = transition_reasons.get(addr) or (
                    "portfolio_positive_net_contribution" if marginal is not None
                    else "portfolio_replay_unavailable_keep_core"
                )
            elif addr in held and data_status != "valid":
                role = selection.EXIT_ONLY
                reason = transition_reasons.get(addr, "exit_only_open_position")
            elif data_status != "valid":
                role = selection.QUARANTINE
                reason = "deferred_data_error" if data_status == "deferred_data_error" else "copy_data_error"
                include_selection = False
            elif candidate_ok:
                role = selection.CHALLENGER
                if transition_reasons.get(addr):
                    reason = transition_reasons[addr]
                elif not enabled:
                    reason = "operator_disabled"
                elif not qualification.get("coreEligible"):
                    reason = qualification.get("status") or "challenger_confidence_watch"
                elif marginal is not None and addr in portfolio_candidates:
                    reason = portfolio_rejections.get(addr, "portfolio_no_profit_improvement")
                elif path_fallback:
                    reason = "path_validation_failed"
                else:
                    reason = "portfolio_replay_unavailable"
                if addr in held:
                    reason = f"{reason}:exit_pending"
            elif addr in held:
                role = selection.EXIT_ONLY
                reason = transition_reasons.get(addr, "exit_only_open_position")
            else:
                role = selection.REJECTED
                reason = qualification.get("status") or transition_reasons.get(
                    addr, row.get("reason") or "not_qualified"
                )
                include_selection = False
            if include_selection:
                rows.append(selection.SelectionRow(
                    addr=addr,
                    role=role,
                    enabled=enabled,
                    reason=reason,
                    utility=portfolio_utilities.get(addr, f(row.get("follow_score"))),
                    follow_score=f(row.get("follow_score")),
                    selection_rank=core_rank.get(addr) if role == selection.CORE else row.get("rank"),
                    data_status=selection_data_status,
                    evidence_status=row.get("evidence_status") or "",
                    model_version=(
                        "selection-quality-first-v1" if transition is not None
                        else "selection-smart-expansion-v1" if marginal is not None
                        else "selection-path-fallback-v1" if path_fallback
                        else "selection-replay-unavailable-v1"
                    ),
                    policy_version=copy_policy.version,
                    acct_value=row.get("acct_value"),
                    sector_policy_json=row.get("sector_policy_json"),
                ))
            lifecycle_state = role if role in {
                selection.CORE, selection.CHALLENGER, selection.EXIT_ONLY
            } else "qualified"
            if role == selection.QUARANTINE:
                lifecycle_state = "quarantine"
            elif role == selection.REJECTED:
                lifecycle_state = "rejected"
            if role not in {selection.CORE, selection.EXIT_ONLY} and not active:
                lifecycle_state = (
                    "cooldown" if row.get("evidence_status") == "economically_disqualified"
                    else "rejected"
                )
            upsert_wallet_registry(
                db,
                addr,
                generation=generation_id,
                seen_at=stamp,
                state=lifecycle_state,
                role=role if role in {selection.CORE, selection.CHALLENGER, selection.EXIT_ONLY} else None,
                data_status=selection_data_status,
                reason=reason,
                last_actionable_open_ms=row.get("last_copyable_open_ms"),
            )
            db.execute(
                "UPDATE profile SET selection_marginal_utility=? WHERE addr=?",
                (portfolio_utilities.get(addr), addr),
            )
        row_addrs = {row.addr for row in rows}
        for addr in sorted(selected_set - row_addrs):
            prior = previous_selection.get(addr)
            if prior is None:
                continue
            rows.append(selection.SelectionRow(
                addr=addr,
                role=selection.CORE,
                enabled=prior.enabled,
                reason=transition_reasons.get(addr, "core_profile_missing_keep"),
                utility=portfolio_utilities.get(addr, prior.utility),
                follow_score=prior.follow_score,
                selection_rank=core_rank.get(addr),
                data_status="stale",
                evidence_status=prior.evidence_status,
                model_version="selection-quality-first-v1",
                policy_version=copy_policy.version,
                acct_value=prior.acct_value,
                sector_policy_json=prior.sector_policy_json,
            ))
            upsert_wallet_registry(
                db,
                addr,
                generation=generation_id,
                seen_at=stamp,
                state="core",
                role=selection.CORE,
                data_status="unobserved",
                reason="core_profile_missing_keep",
            )
        missing_core_policy = []
        for selected_row in rows:
            if selected_row.role != selection.CORE or not selected_row.enabled:
                continue
            try:
                sealed_policy = json.loads(selected_row.sector_policy_json or "{}")
            except (TypeError, ValueError):
                sealed_policy = {}
            if not sealed_policy.get("allowed"):
                missing_core_policy.append(selected_row.addr)
        if missing_core_policy:
            raise RuntimeError(f"selection_core_policy_missing:{len(missing_core_policy)}")
        return rows, marginal
    raise RuntimeError("explicit selection builder requires FOLLOW_SELECTION_MODE=auto")


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
    return current_core


def _selection_market_snapshot_validation(db, generation_id, rows, now_ms) -> dict:
    core_rows = [row for row in rows if row.role == selection.CORE and row.enabled]
    core_addrs = [row.addr for row in core_rows]
    coins = set()
    if core_addrs:
        start_ms = int(now_ms) - int(config.PROFILE_FETCH_DAYS) * 86_400_000
        allowed = {}
        for row in core_rows:
            try:
                policy = json.loads(row.sector_policy_json or "{}")
            except (TypeError, ValueError):
                policy = {}
            allowed[(row.addr or "").lower()] = set(policy.get("allowed") or ())
        coins = {
            fill.get("coin")
            for fill in load_copyable_fills(db, core_addrs, start_ms)
            if fill.get("coin")
            and classify_coin(fill.get("coin")) in allowed.get(fill.get("user"), set())
        }
    return generation_market.validate_coins(db, generation_id, coins)


def refresh_selection_copy_replay(db, generation_id: str, *, replayed_at=None) -> dict:
    """Refresh dashboard Copy PnL with the currently effective follow parameters.

    Scan-time profile evidence remains the immutable qualification snapshot.  Auto-tune runs after list
    publication, so its per-wallet replay belongs on the generation selection: the UI can match the live
    sizing/add/leverage rules without a post-tune regate changing Core membership.
    """
    current = selection.latest_published_generation(db)
    if not generation_id or current != generation_id:
        return {"status": "skipped", "reason": "generation_not_current", "generation": generation_id}
    if not generation_market.has_snapshot(db, generation_id):
        return {
            "status": "skipped", "reason": "market_snapshot_missing_rescan_required",
            "generation": generation_id,
        }
    rows = db.execute(
        "SELECT addr,sector_policy_json FROM follow_selection WHERE generation=? "
        "AND role IN ('core','challenger') ORDER BY addr",
        (generation_id,),
    ).fetchall()
    if not rows:
        return {"status": "ok", "generation": generation_id, "refreshed": 0}

    now_ms = int(time.time() * 1000)
    stamp = replayed_at or now_iso()
    overrides = {**_copy_bt_overrides(db), "AMBIGUOUS_PATH_MODE": "liquidate"}
    replay_hash = hashlib.sha256(
        json.dumps(overrides, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    replay_sigmas, replay_market_ctx = generation_market.load(db, generation_id)
    replay_ctx = SimpleNamespace(
        copy_bt_days=int(config.COPY_BT_DAYS),
        copy_bt_sigmas=replay_sigmas,
        copy_bt_market_ctx=replay_market_ctx,
        copy_bt_overrides=overrides,
        copy_bt_valuation_marks=_current_copy_valuation_marks(),
        scan_generation=generation_id,
    )
    updates = []
    for addr, raw_policy in rows:
        fills = _copy_bt_cached_fills(db, addr, now_ms, replay_ctx)
        try:
            sector_policy = json.loads(raw_policy or "{}")
        except (TypeError, ValueError):
            sector_policy = {}
        evidence_sectors = set(sector_policy.get("allowed") or ()) or set(
            sector_policy.get("watch") or ()
        )
        evidence_fills = (
            [fill for fill in fills if classify_coin(fill.get("coin")) in evidence_sectors]
            if evidence_sectors else fills
        )
        path_start = now_ms - (
            int(config.COPY_BT_DAYS) + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
        ) * 86_400_000
        replay_ctx.copy_bt_price_path = price_path.load_refined(
            db, evidence_fills, path_start, now_ms,
        )
        replay_ctx.copy_bt_price_path_meta = price_path.coverage(
            db, evidence_fills, path_start, now_ms,
        )
        windows = _copy_bt_results(addr, evidence_fills, now_ms, replay_ctx)
        sectors = _sector_copy_bt_results(addr, evidence_fills, now_ms, replay_ctx)
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
            primary.get("unrealized_pnl"), primary.get("valuation_status"),
            recent14.get("copy_net_pnl"), recent14.get("unrealized_pnl"),
            int(recent14.get("closed_n") or 0),
            recent7.get("copy_net_pnl"), recent7.get("unrealized_pnl"),
            int(recent7.get("closed_n") or 0),
            json.dumps(compact_sector_results(sectors, joint_results=windows), sort_keys=True),
            replay_hash, stamp, generation_id, addr,
        ))
    db.executemany(
        "UPDATE follow_selection SET replay_copy_bt_net_pnl=?,replay_copy_bt_win_rate=?,"
        "replay_copy_bt_closed_n=?,replay_copy_bt_open_fill_rate=?,replay_copy_bt_liquidations=?,"
        "replay_copy_bt_fee_drag=?,replay_copy_bt_unrealized_pnl=?,replay_copy_bt_valuation_status=?,"
        "replay_copy_bt_14d_net_pnl=?,replay_copy_bt_14d_unrealized_pnl=?,replay_copy_bt_14d_closed_n=?,"
        "replay_copy_bt_7d_net_pnl=?,replay_copy_bt_7d_unrealized_pnl=?,replay_copy_bt_7d_closed_n=?,replay_sector_copy_json=?,"
        "replay_params_hash=?,replayed_at=? WHERE generation=? AND addr=?",
        updates,
    )
    db.commit()
    return {
        "status": "ok", "generation": generation_id, "refreshed": len(updates),
        "paramsHash": replay_hash, "replayedAt": stamp,
    }


def repair_published_selection(db, generation_id=None, stamp=None, *, replace_existing=False,
                               retune_formation=True):
    """Rebuild selection from the current complete generation without re-fetching wallet profiles/fills.

    This is intentionally narrow: it may incrementally complete the bounded shared K-line cache, but never
    rewrites wallet profiles or fetches wallet fills. Replacing a non-empty Core requires the explicit
    ``replace_existing`` flag. It repairs both an empty bootstrap and a published selection produced from a
    stale derived watchlist without repeating an expensive full scan.
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
    expected_strategy_revision = strategy_revision.active_revision_id(db)
    if existing_core and not replace_existing:
        return {"status": "skipped", "reason": "core_already_present", "core": len(existing_core)}
    stale_active = db.execute(
        "SELECT COUNT(*) FROM profile WHERE status='active' AND COALESCE(profile_generation,'')<>?",
        (generation_id,),
    ).fetchone()[0]
    if stale_active:
        raise RuntimeError("selection_repair_has_stale_active_profiles")

    stamp = stamp or now_iso()
    repair_now_ms = int(time.time() * 1000)
    db.commit()
    refresh_watchlist(
        db,
        stamp,
        leaderboard_generation=generation_id,
        commit=False,
    )
    prefetch_candidates = _selection_prefetch_candidates(db)
    db.rollback()
    _prefetch_selection_paths(db, prefetch_candidates, repair_now_ms, generation_id)
    formation = form_quality_prefix(
        db, generation_id, stamp, repair_now_ms, retune=retune_formation,
    )
    refresh_watchlist(
        db,
        stamp,
        leaderboard_generation=generation_id,
        commit=False,
    )
    _apply_formation_params(db, formation, stamp)
    rows, marginal = _build_explicit_selection(
        db, generation_id, stamp, repair_now_ms,
        force_cold_bootstrap=not bool(existing_core),
        forced_core_order=formation.get("selected") or (),
        formation_meta=formation.get("search") or {},
        effective_qualifications=formation.get("qualifications") or {},
        effective_scores=formation.get("scores") or {},
    )
    previous_core = set(existing_core)
    selection.replace_selection_rows(db, generation_id, rows, selected_at=stamp)
    market_validation = _selection_market_snapshot_validation(
        db, generation_id, rows, repair_now_ms,
    )
    current_core = _record_explicit_follow_history(db, rows, stamp, previous_core, generation_id)
    active_strategy = strategy_revision.create_revision(
        db,
        generation_id,
        source="selection_repair",
        reason="repaired_selection" if previous_core else "repaired_cold_bootstrap",
        parent_revision=expected_strategy_revision,
        expected_active_revision=expected_strategy_revision,
        validation={"marketSnapshot": market_validation}, stamp=stamp,
    )
    for row in rows:
        pipeline_audit._insert_event(
            db,
            stamp=stamp,
            source="selection_repair",
            stage="selection",
            addr=row.addr,
            status=row.role,
            reason=row.reason,
            follow_score=row.follow_score,
            payload={
                "generation": generation_id,
                "selectionRank": row.selection_rank,
                "marginalUtility": row.utility,
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
            "strategyRevision": active_strategy["revision"],
        },
    )
    db.commit()
    try:
        portfolio_replay = auto_tune.store_effective_portfolio_replay(db, generation_id)
    except Exception as exc:  # noqa: BLE001
        portfolio_replay = {"status": "error", "error": str(exc)[:300]}
    try:
        selection_replay = refresh_selection_copy_replay(db, generation_id, replayed_at=now_iso())
    except Exception as exc:  # noqa: BLE001
        selection_replay = {"status": "error", "error": str(exc)[:300]}
    tune_summary = {
        "status": "complete", "reason": "synchronous_quality_prefix_formation",
        "portfolioReplay": portfolio_replay, "selectionReplay": selection_replay,
    }
    pipeline_audit._insert_event(
        db,
        stamp=stamp,
        source="selection_repair",
        stage="tuner_finalize",
        status=tune_summary.get("status"),
        reason=tune_summary.get("reason"),
        payload=tune_summary,
    )
    db.commit()
    return {
        "status": "repaired",
        "generation": generation_id,
        "core": len(current_core),
        "challenger": sum(1 for row in rows if row.role == selection.CHALLENGER),
        "selectionAction": marginal.action if marginal else "keep",
        "tuner": tune_summary,
    }


def optimize_published_generation(db, generation_id=None, stamp=None) -> dict:
    """Re-form one published generation with the synchronous quality-prefix tuner."""
    generation_id = generation_id or selection.latest_published_generation(db)
    stamp = stamp or now_iso()
    selection_result = repair_published_selection(
        db, generation_id, stamp=stamp, replace_existing=True,
    )
    return {
        "status": "ok" if selection_result.get("status") == "repaired" else selection_result.get("status"),
        "generation": generation_id,
        "selection": selection_result,
        "tune": selection_result.get("tuner"),
    }


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
    return refresh_watchlist(db, stamp or now_iso())


def _record_run(db, started, t0, candidates, profiled, added, retired, kept, rejected, n_active,
                full=0, failed=0, complete=True):
    db.execute(
        "INSERT INTO scan_runs (started_at,finished_at,duration_s,candidates,profiled,probed_new,added,"
        "retired,kept,rejected,n_active,full,failed,complete) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (started, now_iso(), round(time.time() - t0, 1), candidates, profiled, profiled, added, retired,
         kept, rejected, n_active, 1 if full else 0, failed, 1 if complete else 0))
    db.commit()


def _regate_profile_status(old_status, old_reason, ok, *, complete_cached_snapshot=False):
    """Resolve cache-only qualification without reviving profiles that never got a full market snapshot."""
    if old_status == "active" or (ok and complete_cached_snapshot):
        return "active" if ok else "retired"
    return old_status


def regate(db, p, *, stamp=None, source: str = "regate", quiet: bool = False) -> int:
    """Re-apply gates() + score() on ALREADY-STORED profile metrics (no network, no re-fetch) and
    rebuild the watchlist. Thresholds (win/roiEq/dd/tpd/hold/...) can be tuned in seconds without a
    full re-sweep — the expensive part (fetching fills, building episodes) is already done."""
    now = int(time.time() * 1000)
    stamp = stamp or now_iso()
    published_generation = selection.latest_published_generation(db)
    if published_generation and not generation_market.has_snapshot(db, published_generation):
        raise RuntimeError(f"market_snapshot_missing_rescan_required:{published_generation}")
    snapshot_sigmas, snapshot_ctx = (
        generation_market.load(db, published_generation) if published_generation else ({}, {})
    )
    if published_generation:
        # Even an empty sealed map is authoritative. Falling back to mutable ``coin_vol`` here would let a
        # later Observer refresh silently change a published generation's qualification result.
        p.copy_bt_sigmas = snapshot_sigmas
        p.copy_bt_market_ctx = snapshot_ctx
    else:
        p.copy_bt_sigmas = getattr(p, "copy_bt_sigmas", None) or _copy_bt_sigmas(db)
        p.copy_bt_market_ctx = getattr(p, "copy_bt_market_ctx", None) or _copy_bt_market_ctx(db)
    p.copy_bt_overrides = getattr(p, "copy_bt_overrides", None) or _copy_bt_overrides(db)
    p.copy_bt_valuation_marks = (
        getattr(p, "copy_bt_valuation_marks", None) or _current_copy_valuation_marks()
    )
    p.margin_equity_pct = p.copy_bt_overrides.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
    open_copy_pnl_by_addr = {
        str(row[0] or "").lower(): f(row[1])
        for row in db.execute(
            "SELECT addr,SUM(COALESCE(unrealized_pnl,0)) FROM copy_position "
            "WHERE status='open' GROUP BY addr"
        ).fetchall()
    }
    row_scope = ""
    row_args = ()
    if published_generation:
        # A published generation is the qualification evidence boundary. Old-generation profiles may be
        # retained for history/rotation, but must not be reactivated into the current derived watchlist.
        db.execute(
            "UPDATE profile SET status='retired',reason='stale_generation',score=0 "
            "WHERE status='active' AND COALESCE(profile_generation,'')<>?",
            (published_generation,),
        )
        row_scope = " WHERE p.profile_generation=?"
        row_args = (published_generation,)
    rows = db.execute(
        "SELECT p.addr,status,n_trades,n_fills,perp_frac,last_fill_ms,net_pnl,roi_equity,max_drawdown,"
        "acct_value,age_days,times_active,liq_worst_pct,active_days,activity_ratio,median_eps,avg_notional,"
        "pos_day_ratio,profit_conc,hold_skew,open_underwater,max_adds_per_ep,median_adds_per_ep,worst_loss_pct,median_hold_s,win_rate,"
        "roi_total,open_loss_frac,open_win_frac,bag_count,max_bag_days,liq_count,hedge_ratio,net_30d,net_life,reason,"
        "l.week_roi,l.mon_roi,l.all_roi,"                      # HL return-on-capital windows for the ROI pillar
        "p.pf_turnover,p.pf_mon_pnl,p.pf_mon_vlm,p.pf_week_pnl,p.pf_equity,"   # v7 portfolio net metrics (gates + ROI)
        "p.payoff_ratio,p.pf_week_vlm,"   # v9: needed so regate applies the SAME payoff + edge-decay gates as a scan
        "p.copy_bt_net_pnl,p.copy_bt_win_rate,p.copy_bt_closed_n,p.copy_bt_open_fill_rate,"
        "p.copy_bt_liquidations,p.copy_bt_fee_drag,p.copy_bt_unrealized_pnl,p.copy_bt_valuation_status,"
        "p.copy_bt_14d_net_pnl,p.copy_bt_14d_unrealized_pnl,p.copy_bt_14d_closed_n,"
        "p.copy_bt_7d_net_pnl,p.copy_bt_7d_unrealized_pnl,p.copy_bt_7d_closed_n,p.sector_copy_json,p.sector_policy_json,"
        "p.open_position_count,p.material_open_count "
        "FROM profile p LEFT JOIN leaderboard l ON p.addr=l.addr" + row_scope,
        row_args,
    ).fetchall()
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
         copy_unreal, copy_valuation, copy14_net, copy14_unreal, copy14_closed,
         copy7_net, copy7_unreal, copy7_closed, sector_copy_json, sector_policy_json,
         open_position_count, material_open_count) = r
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
             "copy_bt_unrealized_pnl": copy_unreal, "copy_bt_valuation_status": copy_valuation,
             "copy_bt_14d_net_pnl": copy14_net, "copy_bt_14d_unrealized_pnl": copy14_unreal,
             "copy_bt_14d_closed_n": copy14_closed,
             "copy_bt_7d_net_pnl": copy7_net, "copy_bt_7d_unrealized_pnl": copy7_unreal,
             "copy_bt_7d_closed_n": copy7_closed,
             "sector_copy_json": sector_copy_json, "sector_policy_json": sector_policy_json,
             "open_position_count": open_position_count or 0,
             "material_open_count": material_open_count or 0}
        # realized loss-asymmetry from the STORED episodes (no network) — works even for profiles scanned
        # before loss_pain existed, so a regate alone re-ranks 小赚大亏 wallets without a full re-scan.
        m["loss_pain"] = metrics.loss_pain(_pnl.get(addr, ()))
        replay_fills = _copy_bt_cached_fills(db, addr, now, p)
        structural_start = now - int(getattr(p, "days", 14)) * 86_400_000
        structural_fills = [fill for fill in replay_fills if int(fill.get("time") or 0) >= structural_start]
        sector_structure = _current_sector_structure_policy(
            structural_fills, now, p, source="current_generation_regate",
        )
        m["sector_policy_json"] = json.dumps(
            _structural_specialization_snapshot(sector_structure), sort_keys=True,
        )
        ok, reason = metrics.gates_structural(m, p)
        if (
            not ok
            and reason in _SECTOR_RECOVERABLE_STRUCTURE_REASONS
            and sector_structure.get("allowed")
        ):
            ok, reason = True, "ok"
        if ok:
            ok, reason = metrics.gates_state(m, now, p)        # uses the stored open-position metrics
            if (
                not ok
                and reason in _SECTOR_RECOVERABLE_STATE_REASONS
                and sector_structure.get("allowed")
            ):
                ok, reason = True, "ok"
        if not ok and reason == "account_equity_unavailable":
            m["data_status"] = "deferred_data_error"
            m["evidence_status"] = "invalid"
        if ok:
            copy_results = _copy_bt_results(addr, replay_fills, now, p)
            sector_results = _sector_copy_bt_results(addr, replay_fills, now, p)
            ok, reason = _apply_sector_copy_bt_gate(
                m, copy_results, sector_results, p,
                # Regate is an explicit deterministic rebuild of the current cached generation.  It must
                # be able to repair a cold-start generation whose old policy was formed before sector
                # specialization existed, so historical policy never participates in this decision.
                previous_policy=None,
                structural_policy=sector_structure,
            )
            try:
                current_policy = json.loads(m.get("sector_policy_json") or "{}")
            except (TypeError, ValueError):
                current_policy = {}
            allowed_sectors = set(current_policy.get("allowed") or [])
            evidence_sectors = allowed_sectors or set(current_policy.get("watch") or [])
            evidence_results = copy_results
            evidence_fills = replay_fills
            if evidence_sectors and evidence_sectors != {"crypto", "stock"}:
                allowed_fills = [
                    x for x in replay_fills if classify_coin(x.get("coin")) in evidence_sectors
                ]
                evidence_fills = allowed_fills
                evidence_results = _copy_bt_results(addr, allowed_fills, now, p)
            m.update(_open_flow_metrics(evidence_fills, now))
            _copy_profile_evidence(m, evidence_results, p, addr=addr, now_ms=now)
            if (
                not current_policy.get("allowed")
                and not current_policy.get("watch")
                and m.get("evidence_status") not in {"missing", "invalid"}
            ):
                m["evidence_status"] = "economically_disqualified"
            _attach_open_copy_activity_context(m, addr, open_copy_pnl_by_addr)
            ok, reason = _profile_copy_qualification(m, now, p)
        ok, reason, score = _finalize_profile_qualification(m, ok, reason)
        # Only policy-only outcomes removed by this release may be safely reactivated from the current
        # cached replay. Structural/data failures still require a fresh network generation.
        complete_cached_snapshot = bool(
            float(acct or 0.0) > 0.0
            and str(m.get("data_status") or "valid").lower() == "valid"
            and str(m.get("evidence_status") or "").lower() not in {"invalid", "missing"}
        )
        status = _regate_profile_status(
            old, old_reason, ok, complete_cached_snapshot=complete_cached_snapshot,
        )
        db.execute(
            "UPDATE profile SET status=?,reason=?,score=?,raw_quality_score=?,loss_pain=?,max_concurrent=?,win_pt=?,"
            "copy_bt_net_pnl=?,copy_bt_win_rate=?,copy_bt_closed_n=?,copy_bt_open_fill_rate=?,"
            "copy_bt_liquidations=?,copy_bt_fee_drag=?,copy_bt_unrealized_pnl=?,copy_bt_valuation_status=?,"
            "copy_bt_14d_net_pnl=?,copy_bt_14d_unrealized_pnl=?,copy_bt_14d_closed_n=?,"
            "copy_bt_7d_net_pnl=?,copy_bt_7d_unrealized_pnl=?,copy_bt_7d_closed_n=?,sector_copy_json=?,sector_policy_json=?,"
            "copy_expected_return=?,copy_return_lcb=?,copy_return_volatility=?,copy_positive_probability=?,"
            "copy_evidence_days=?,copy_recent_return_14d=?,copy_recent_return_7d=?,copy_risk_score=?,"
            "execution_score=?,model_coverage=?,oos_net_pnl=?,oos_max_drawdown=?,oos_cvar95=?,"
            "actionable_open_rate=?,capacity_fit=?,"
            "copy_path_risk_status=?,copy_intratrade_max_drawdown=?,copy_max_underwater_hours=?,"
            "copy_loss_over_5_time_ratio=?,copy_deep_bag_event_n=?,copy_failed_deep_bag_n=?,"
            "copy_deep_bag_recovery_rate=?,copy_max_deep_bag_hours=?,copy_current_open_loss_frac=?,"
            "copy_current_bag_hours=?,copy_campaign_max_drawdown=?,copy_campaign_peak_positions=?,"
            "copy_campaign_peak_margin_pct=?,data_status=?,evidence_status=? WHERE addr=?",
            (status, reason, score, m.get("raw_quality_score"), m["loss_pain"], concw.get(addr, 0), winptw.get(addr, 0.0),
             m.get("copy_bt_net_pnl"), m.get("copy_bt_win_rate"), m.get("copy_bt_closed_n"),
             m.get("copy_bt_open_fill_rate"), m.get("copy_bt_liquidations"), m.get("copy_bt_fee_drag"),
             m.get("copy_bt_unrealized_pnl"), m.get("copy_bt_valuation_status"),
             m.get("copy_bt_14d_net_pnl"), m.get("copy_bt_14d_unrealized_pnl"),
             m.get("copy_bt_14d_closed_n"),
             m.get("copy_bt_7d_net_pnl"), m.get("copy_bt_7d_unrealized_pnl"),
             m.get("copy_bt_7d_closed_n"),
             m.get("sector_copy_json"), m.get("sector_policy_json"),
             m.get("copy_expected_return"), m.get("copy_return_lcb"), m.get("copy_return_volatility"),
             m.get("copy_positive_probability"), m.get("copy_evidence_days"),
             m.get("copy_recent_return_14d"), m.get("copy_recent_return_7d"),
             m.get("copy_risk_score"), m.get("execution_score"), m.get("model_coverage"),
             m.get("oos_net_pnl"), m.get("oos_max_drawdown"), m.get("oos_cvar95"),
             m.get("actionable_open_rate"), m.get("capacity_fit"),
             m.get("copy_path_risk_status"), m.get("copy_intratrade_max_drawdown"),
             m.get("copy_max_underwater_hours"), m.get("copy_loss_over_5_time_ratio"),
             m.get("copy_deep_bag_event_n"), m.get("copy_failed_deep_bag_n"),
             m.get("copy_deep_bag_recovery_rate"), m.get("copy_max_deep_bag_hours"),
             m.get("copy_current_open_loss_frac"), m.get("copy_current_bag_hours"),
             m.get("copy_campaign_max_drawdown"), m.get("copy_campaign_peak_positions"),
             m.get("copy_campaign_peak_margin_pct"),
             m.get("data_status") or "valid", m.get("evidence_status"),
             addr),
        )
        # ``ok`` can be true for a cache-only recomputation that is intentionally not allowed to revive a
        # profile lacking a complete prior market snapshot.  Report the durable state, not the transient
        # calculation, so the operator count always matches the rebuilt watchlist source of truth.
        n_active += 1 if status == "active" else 0
    db.commit()
    pipeline_audit.record_profile_snapshot(db, stamp, source)
    n = refresh_watchlist(db, stamp)
    if not quiet:
        print(f"regate: {n_active} active / {len(rows)} profiles  ->  watchlist {n}")
    return n


# ----------------------------------------------------------------------------- staged-generation finalization
def _profiled_generation_coverage(db, generation_id: str, scan_stamp=None) -> dict:
    """Count durable profile outcomes, including deferred rows which intentionally retain old evidence."""
    current = db.execute(
        "SELECT COUNT(*),"
        "SUM(CASE WHEN COALESCE(data_status,'valid') NOT IN "
        "('deferred_data_error','rejected') THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN data_status='deferred_data_error' THEN 1 ELSE 0 END) "
        "FROM profile WHERE profile_generation=?",
        (generation_id,),
    ).fetchone()
    current_total = int((current[0] if current else 0) or 0)
    current_valid = int((current[1] if current else 0) or 0)
    current_deferred = int((current[2] if current else 0) or 0)
    if not scan_stamp:
        return {
            "complete": current_total,
            "valid": current_valid,
            "deferred": current_deferred,
            "rejected": max(0, current_total - current_valid - current_deferred),
            "source": "profile_generation",
        }
    audited = db.execute(
        "SELECT COUNT(DISTINCT lower(a.addr)),"
        "COUNT(DISTINCT CASE WHEN p.data_status='deferred_data_error' THEN lower(a.addr) END) "
        "FROM pipeline_audit a LEFT JOIN profile p ON lower(p.addr)=lower(a.addr) "
        "WHERE a.source='scan' AND a.stamp=? AND a.stage='profile' AND a.addr IS NOT NULL",
        (scan_stamp,),
    ).fetchone()
    audited_total = int((audited[0] if audited else 0) or 0)
    if not audited_total:
        return {
            "complete": current_total,
            "valid": current_valid,
            "deferred": current_deferred,
            "rejected": max(0, current_total - current_valid - current_deferred),
            "source": "profile_generation",
        }
    audited_deferred = int((audited[1] if audited else 0) or 0)
    return {
        "complete": audited_total,
        "valid": current_valid,
        "deferred": audited_deferred,
        "rejected": max(0, audited_total - current_valid - audited_deferred),
        "source": "profile_audit",
    }


def finalize_profiled_generation(db, generation_id=None, stamp=None) -> dict:
    """Finish selection/tuning from an already-profiled generation without fetching wallet history."""
    stamp = stamp or now_iso()
    if generation_id is None:
        row = db.execute(
            "SELECT generation FROM scan_generation "
            "WHERE status NOT IN ('published','failed') AND leaderboard_valid=1 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        generation_id = row[0] if row else None
    if not generation_id:
        raise RuntimeError("no_profiled_generation_to_finalize")
    meta = db.execute(
        "SELECT status,leaderboard_valid,workset_n,leaderboard_rows,metrics_json,started_at "
        "FROM scan_generation WHERE generation=?",
        (generation_id,),
    ).fetchone()
    if not meta or meta[0] in {"published", "failed"} or not int(meta[1] or 0):
        raise RuntimeError("generation_not_resumable")
    workset_n = int(meta[2] or 0)
    profile_coverage = _profiled_generation_coverage(db, generation_id, meta[5])
    profile_total = int(profile_coverage["complete"])
    if workset_n <= 0 or profile_total < workset_n:
        raise RuntimeError(f"profile_generation_incomplete:{profile_total}:{workset_n}")
    staged_n = int(db.execute(
        "SELECT COUNT(*) FROM leaderboard_staging WHERE generation=?", (generation_id,),
    ).fetchone()[0] or 0)
    if staged_n != int(meta[3] or 0):
        raise RuntimeError("staged_leaderboard_count_mismatch")
    try:
        generation_metrics = json.loads(meta[4] or "{}")
    except (TypeError, ValueError):
        generation_metrics = {}
    expected_margin_equity_pct = float(
        generation_metrics.get("marginEquityPct", _current_margin_equity_pct(db))
    )
    _assert_margin_equity_snapshot(db, expected_margin_equity_pct)

    now_ms = int(time.time() * 1000)
    previous_core = selection.published_core_addrs(db) or []
    _set_scan_progress(
        db, state="scanning", stage="prepare_selection_candidates",
        candidates_scanned=profile_total, candidates_total=profile_total,
    )
    refresh_watchlist(
        db, stamp, leaderboard_generation=generation_id, commit=False,
    )
    preview = _selection_prefetch_candidates(
        db, limit=int(params.get(db, "CORE_INITIAL_MAX_N", config.CORE_INITIAL_MAX_N)),
    )
    db.rollback()
    if preview:
        _set_scan_progress(db, stage="prefetch_selection_paths")
        _prefetch_selection_paths(db, preview, now_ms, generation_id)
    formation = form_quality_prefix(db, generation_id, stamp, now_ms)
    _assert_margin_equity_snapshot(db, expected_margin_equity_pct)
    publication_stamp = now_iso()
    try:
        refresh_watchlist(
            db, publication_stamp,
            leaderboard_generation=generation_id, commit=False,
        )
        _apply_formation_params(db, formation, publication_stamp)
        rows, marginal = _build_explicit_selection(
            db, generation_id, publication_stamp, now_ms,
            forced_core_order=formation.get("selected") or (),
            formation_meta=formation.get("search") or {},
            effective_qualifications=formation.get("qualifications") or {},
            effective_scores=formation.get("scores") or {},
            audit_stamp=stamp,
        )
        _assert_margin_equity_snapshot(db, expected_margin_equity_pct)
        valid = int(profile_coverage["valid"])
        deferred = int(profile_coverage["deferred"])
        rejected = int(profile_coverage["rejected"])
        generation.mark_generation_ready(
            db, generation_id, profile_total=profile_total, profile_valid=valid,
            profile_deferred=deferred, profile_rejected=rejected,
            profile_complete=True, ready_at=publication_stamp,
        )
        selection.replace_selection_rows(db, generation_id, rows, selected_at=publication_stamp)
        market_validation = _selection_market_snapshot_validation(
            db, generation_id, rows, now_ms,
        )
        generation.publish_generation(db, generation_id, published_at=publication_stamp)
        current_core = _record_explicit_follow_history(
            db, rows, publication_stamp, previous_core, generation_id,
        )
        active_strategy = strategy_revision.create_revision(
            db, generation_id, source="resume_finalize", reason="quality_prefix_formation",
            validation={
                **(formation.get("search") or {}), "marketSnapshot": market_validation,
            }, stamp=publication_stamp,
        )
        for item in rows:
            pipeline_audit._insert_event(
                db, stamp=stamp, source="resume_finalize", stage="selection",
                addr=item.addr, status=item.role, reason=item.reason,
                follow_score=item.follow_score,
                payload={
                    "generation": generation_id, "selectionRank": item.selection_rank,
                    "marginalUtility": item.utility, "dataStatus": item.data_status,
                    "evidenceStatus": item.evidence_status,
                },
            )
        pipeline_audit._insert_event(
            db, stamp=stamp, source="resume_finalize", stage="selection_summary",
            status="ok", reason="quality_prefix_formation",
            payload={
                "generation": generation_id, "core": len(current_core),
                "challenger": sum(1 for item in rows if item.role == selection.CHALLENGER),
                "search": formation.get("search") or {},
                "strategyRevision": active_strategy["revision"],
            },
        )
        db.execute(
            "UPDATE commands SET status='done',done_at=?,result_json=? "
            "WHERE type='rescan' AND status='acked'",
            (publication_stamp, json.dumps({
                "resumed": True, "generation": generation_id, "active": len(current_core),
            }, sort_keys=True)),
        )
        db.commit()
    except Exception:
        db.rollback()
        _set_scan_progress(db, state="idle", stage="error")
        raise

    try:
        portfolio_replay = auto_tune.store_effective_portfolio_replay(db, generation_id)
    except Exception as exc:  # noqa: BLE001
        portfolio_replay = {"status": "error", "error": str(exc)[:300]}
    try:
        selection_replay = refresh_selection_copy_replay(db, generation_id, replayed_at=now_iso())
    except Exception as exc:  # noqa: BLE001
        selection_replay = {"status": "error", "error": str(exc)[:300]}
    auto_tune.bind_active_tune_rollback_core(db, current_core)
    _set_scan_progress(
        db, state="idle", stage="persist", candidates_scanned=profile_total,
        candidates_total=profile_total,
    )
    _set_scanner_proc(db, "idle", {"last_scan_at": now_iso(), "active": len(current_core)})
    db.commit()
    return {
        "status": "published", "generation": generation_id,
        "core": len(current_core),
        "challenger": sum(1 for item in rows if item.role == selection.CHALLENGER),
        "search": formation.get("search") or {},
        "portfolioReplay": portfolio_replay, "selectionReplay": selection_replay,
        "strategyRevision": active_strategy["revision"],
    }


# ----------------------------------------------------------------------------- scan
def scan(db, p) -> None:
    now_ms = int(time.time() * 1000)
    started, t0 = now_iso(), time.time()
    stamp = now_iso()
    start_ms = now_ms - p.days * 86400_000
    cold_start = selection.latest_published_generation(db) is None
    if cold_start:
        # Empty databases have no trustworthy prior leaderboard/profile boundary.  A dashboard command whose
        # checkbox says "incremental" is therefore upgraded to the one valid first-generation operation.
        p.full_scan = True
        p.no_harvest = False
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
    # Every generation reevaluates the complete current candidate set. ``full`` describes workset breadth,
    # not history transport: complete caches still fetch deltas and only new/incomplete caches backfill 37d.
    run_full = True
    # A complete/full operator sweep is a fresh specialization decision.  Prior sector policy may help an
    # incremental scan confirm repeated deterioration, but must never decide whether a cold/full scan gets
    # to evaluate the current generation's Crypto/stock evidence.
    p.rebuild_sector_policy = run_full
    generation_id = generation.begin_generation(
        db,
        source="scan",
        started_at=started,
        workset_mode="cold_full" if cold_start else ("all" if run_full else "priority"),
        fill_mode="full_refetch" if run_full else "mixed",
    )
    p.scan_generation = generation_id
    db.commit()
    _set_scanner_proc(db, "scanning", {"phase": "harvest"})
    _set_scan_progress(db, state="scanning", started_at=started, stage="scan_leaderboard",
                       candidates_scanned=0, candidates_total=0, manual=1 if manual else 0)
    # Production profile replay resolves its generation snapshot explicitly after executable fills and sector
    # structure are known.  Do not seed it from Observer's mutable live cache.
    p.copy_bt_sigmas = {}
    p.copy_bt_market_ctx = {}
    p.copy_bt_overrides = _copy_bt_overrides(db)
    p.margin_equity_pct = p.copy_bt_overrides.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
    rest.reset_request_stats()
    harvest_started_at = time.time()

    try:
        # A full/cold sweep rebuilds specialization from the exchange's current executable market set.
        # Keep this immutable snapshot on ``p`` so every wallet replay in the generation uses the same
        # boundary even if a listing changes while the scan is running.
        universe = rest.copyable_universe(force=run_full)
        if not universe:
            raise RuntimeError("copyable_universe_unavailable")
        p.copyable_universe = frozenset(universe)
        p.generation_market_resolver = generation_market.Resolver(
            db, generation_id, now_ms, p.copyable_universe,
            generation_market.fetch_context_snapshot(p.copyable_universe),
            db_lock=_db_lock,
        )
        if not p.no_harvest:
            print("harvest leaderboard -> staging ...", flush=True)
            n_cand = harvest(db, p, generation_id=generation_id)
        else:
            n_cand = _stage_existing_leaderboard(db, generation_id)
        print(f"  generation {generation_id} · {n_cand} staged candidates", flush=True)
        harvest_done_at = time.time()
        harvest_api_stats = rest.request_stats()
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
    roi_cand = [r[0] for r in db.execute(
        f"SELECT addr FROM leaderboard_staging WHERE generation=? AND is_candidate=1 "
        f"ORDER BY {order} DESC",
        (generation_id,),
    ).fetchall()]
    _official_roi_audit(db, generation_id, stamp, p)
    prefilter_started_at = time.time()
    _set_scan_progress(db, stage="perp_prefilter", candidates_scanned=0,
                       candidates_total=len(roi_cand))
    perp_results = _run_perp_prefilter(db, roi_cand, p, stamp)
    prefilter_done_at = time.time()
    prefilter_api_stats = rest.request_stats()
    cand = [addr for addr in roi_cand if perp_results[addr].passed]
    print(
        f"  official ROI {len(roi_cand)} · Perp precheck passed {len(cand)} · "
        f"deferred {sum(result.deferred for result in perp_results.values())}", flush=True,
    )
    # ``profile.status='active'`` is the storage-compatible spelling for a wallet that passed the
    # per-wallet quality/Copy gates.  It is a qualified pre-selection candidate, not a production role.
    qualified_addrs = [
        r[0] for r in db.execute("SELECT addr FROM profile WHERE status='active'").fetchall()
    ]
    profiled = {r[0] for r in db.execute("SELECT addr FROM profile").fetchall()}
    current_selection_generation = selection.latest_published_generation(db)
    core_addrs = selection.published_core_addrs(db) or []
    challenger_addrs = []
    if current_selection_generation:
        challenger_addrs = [r[0] for r in db.execute(
            "SELECT addr FROM follow_selection WHERE generation=? AND role='challenger' AND enabled=1",
            (current_selection_generation,),
        ).fetchall()]
    # Copy replay adds seven warm-up days. Only wallets that already produced Copy evidence
    # need the one-time 37-day backfill; front-funnel structural rejects remain incremental.
    warmup_backfill_addrs = _copy_warmup_backfill_addrs(
        db, now_ms - config.PROFILE_FETCH_DAYS * 86400_000,
    )
    open_copy_pnl_by_addr = {
        str(addr or "").lower(): f(unrealized)
        for addr, unrealized in db.execute(
            "SELECT addr,SUM(COALESCE(unrealized_pnl,0)) FROM copy_position "
            "WHERE status='open' GROUP BY addr"
        ).fetchall()
    }
    position_addrs = sorted(open_copy_pnl_by_addr)
    # Freeze the open-copy PnL surface for the generation. Worker threads use it only to distinguish a
    # profitable carried mirrored episode from a dormant/losing wallet; it never bypasses economic/risk gates.
    p.open_copy_pnl_by_addr = dict(open_copy_pnl_by_addr)
    cand_set = set(cand)
    off_list_qualified = [addr for addr in qualified_addrs if addr not in cand_set]
    priority_n = len(set(position_addrs) | set(core_addrs) | set(qualified_addrs)
                     | set(challenger_addrs) | set(off_list_qualified))
    recent = db.execute(
        "SELECT duration_s,COALESCE(profiled,probed_new) FROM scan_runs "
        "WHERE COALESCE(profiled,probed_new)>0 AND complete=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    estimated_profile_s = max(1.0, min(120.0, (f(recent[0]) / int(recent[1])))) if recent else 12.0
    scheduler_limit = len(set(cand) | set(position_addrs) | set(core_addrs)
                          | set(qualified_addrs) | set(challenger_addrs) | set(off_list_qualified))
    time_budget = None
    desired_cache_start_ms = now_ms - config.PROFILE_FETCH_DAYS * 86400_000
    full_refetch_due = set(_incomplete_fill_cache_addrs(
        db,
        set(cand) | set(position_addrs) | set(core_addrs) | set(qualified_addrs)
        | set(challenger_addrs) | set(off_list_qualified),
        desired_cache_start_ms,
    )) | set(warmup_backfill_addrs)
    workset_info = schedule_profile_workset(
        cand,
        qualified_addrs=qualified_addrs,
        core_addrs=core_addrs,
        challenger_addrs=challenger_addrs,
        warmup_backfill_addrs=warmup_backfill_addrs,
        off_list_qualified_addrs=off_list_qualified,
        position_addrs=position_addrs,
        profiled_addrs=profiled,
        full_refetch_addrs=full_refetch_due,
        limit=scheduler_limit,
        budget=time_budget,
        estimated_profile_s=estimated_profile_s,
        exploration_seed=generation_id,
        full_scan=True,
    )
    if cold_start:
        workset_info["mode"] = "cold_full"
        workset_info["workset_mode"] = "cold_full"
        workset_info["full_scan"] = True
    migration_backfill = set(warmup_backfill_addrs) & set(workset_info["workset"])
    refresh = workset_info["refresh"]
    workset_info["fill_mode"] = (
        "full_refetch" if refresh["full_refetch"] and not refresh["delta"]
        else ("mixed" if refresh["full_refetch"] else "delta")
    )
    pipeline_audit.record_workset_summary(db, stamp, "scan", workset_info)
    workset_metrics = {
        "estimatedProfileSec": estimated_profile_s,
        "warmupBackfillDue": len(warmup_backfill_addrs),
        "warmupBackfillScheduled": len(migration_backfill),
        "marginEquityPct": float(p.margin_equity_pct),
        "initialMarginEquity": float(config.INITIAL_BALANCE),
    }
    generation.record_workset(
        db,
        generation_id,
        workset_mode=workset_info["workset_mode"],
        fill_mode=workset_info["fill_mode"],
        full_refresh_shard=workset_info["refresh"]["shard_index"],
        workset_n=len(workset_info["workset"]),
        deferred_n=workset_info["counts"]["deferred"],
        metrics=workset_metrics,
    )
    db.commit()
    workset, mode = workset_info["workset"], workset_info["mode"]
    off_qualified_n = len([a for a in qualified_addrs if a not in cand_set])
    full_refetch = set(workset_info["refresh"]["full_refetch"])
    priority_addrs = set(workset[:workset_info["counts"]["priority"]])
    _set_scan_progress(db, stage="fetch_history", candidates_total=len(workset))
    _pace = config.MIN_POST_INTERVAL   # live adaptive pace (fast when no copy-trading, slow trickle when observer up)
    print(f"scan: {mode} · {len(workset)} wallets (incl {off_qualified_n} off-list qualified), "
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
        gate = perp_results.get(addr)
        if gate is None:
            return addr, prior, _reject_prefilter_profile(
                db, addr, prior, stamp, generation_id, "official_roi_below_floor",
            )
        if gate.deferred:
            return addr, prior, _defer_profile(db, addr, prior, stamp, gate.reason)
        if not gate.passed:
            return addr, prior, _reject_prefilter_profile(
                db, addr, prior, stamp, generation_id, gate.reason,
            )
        return addr, prior, _profile_one(
            db, addr, start_ms, now_ms, p, prior, lbs.get(addr, {}), stamp, universe,
            force_full=addr in full_refetch,
        )

    done = 0
    priority_done_at = time.time() if not priority_addrs else None
    def _profile_batch(batch):
        nonlocal done, priority_done_at, added, retired, rejected, kept, failed
        nonlocal profiled_ok, deferred_profiles, valid_profiles
        if not batch:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            pending = {}
            next_index = 0

            def submit_available():
                nonlocal next_index
                while next_index < len(batch) and len(pending) < workers:
                    addr = batch[next_index]
                    next_index += 1
                    pending[ex.submit(_work, addr)] = addr

            submit_available()
            while pending:
                completed, _ = concurrent.futures.wait(
                    tuple(pending), return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in completed:
                    expected_addr = pending.pop(fut)
                    done += 1
                    priority_addrs.discard(expected_addr)
                    if not priority_addrs and priority_done_at is None:
                        priority_done_at = time.time()
                    try:
                        addr, prior, (status, reason, m, hit_cap) = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        print(f"  [{done}/{len(workset)}] FAIL: {exc}")
                        continue
                    profiled_ok += 1
                    profiled_addrs.append(addr)
                    data_status = m.get("data_status")
                    if data_status == "deferred_data_error":
                        deferred_profiles += 1
                    elif data_status == "rejected":
                        rejected += 1
                    else:
                        valid_profiles += 1
                    if data_status == "deferred_data_error":
                        pass
                    elif status == "active":
                        if (prior or {}).get("status") == "active":
                            kept += 1
                        else:
                            added += 1
                    elif status == "retired":
                        retired += 1
                    elif data_status != "rejected":
                        rejected += 1
                    _set_scan_progress(
                        db, stage="score_filter", candidates_scanned=done,
                        candidates_total=len(workset),
                    )
                    if done % 10 == 0:
                        _set_scanner_proc(
                            db, "scanning",
                            {"stage": "score_filter", "scanned": done, "total": len(workset)},
                        )
                submit_available()

    _profile_batch(list(workset))

    profile_done_at = time.time()
    profile_api_stats = rest.request_stats()
    complete = failed == 0
    market_snapshot_audit = {}
    if complete:
        try:
            market_snapshot_audit = generation_market.seal(db, generation_id)
        except Exception as exc:  # fail closed before any replay/formation can read a mutable surface
            complete = False
            failed += 1
            print(f"generation market snapshot seal failed: {exc}", flush=True)
    scope_audit = {"audited": 0, "invalid": 0, "scope": ["crypto", "stock"]}
    if complete:
        try:
            # Qualified profiles are mandatory in the workset.  Audit them all rather than only successful
            # task returns, so no stale candidate can enter shared-account selection through an old cache.
            active_addrs = [
                row[0] for row in db.execute(
                    "SELECT addr FROM profile WHERE status='active'"
                ).fetchall()
            ]
            scope_audit = _assert_scoped_fill_cache(
                db, set(profiled_addrs) | set(active_addrs), universe,
            )
        except Exception as exc:  # fail closed before watchlist/selection publication
            complete = False
            failed += 1
            print(f"generation market-scope audit failed: {exc}", flush=True)
    published = False
    publication_stamp = None
    previous_core = selection.published_core_addrs(db) or []
    n_active = len(previous_core)
    pipeline_audit.record_profile_snapshot(db, stamp, "scan", profiled_addrs)
    if complete:
        _set_scan_progress(db, stage="rebuild_watchlist", candidates_scanned=len(workset))
        selection_mode = str(
            params.get(db, "FOLLOW_SELECTION_MODE", config.FOLLOW_SELECTION_MODE) or "auto"
        ).lower()
        # Build only the bounded candidate universe in a rolled-back staging pass, then fetch its shared
        # market path before the atomic publication transaction.  The old flow ran a complete fills-only
        # selection here and repeated it during final publication merely to discover which paths to fetch.
        # Querying the same quality-qualified top-40 universe removes that duplicate search while keeping
        # network I/O outside the Dashboard/Observer SQLite writer lock.
        if selection_mode == "auto":
            try:
                _set_scan_progress(
                    db, stage="prepare_selection_candidates", candidates_scanned=len(workset),
                )
                db.commit()
                refresh_watchlist(
                    db, stamp,
                    leaderboard_generation=generation_id, commit=False,
                )
                preview_candidates = _selection_prefetch_candidates(db)
                db.rollback()
                if preview_candidates:
                    _set_scan_progress(
                        db, stage="prefetch_selection_paths",
                        candidates_scanned=len(workset), candidates_total=len(workset),
                    )
                    _prefetch_selection_paths(db, preview_candidates, now_ms, generation_id)
            except Exception as exc:  # noqa: BLE001 - final pass safely retains prior Core without coverage
                db.rollback()
                print(f"selection price-path prefetch unavailable: {exc}", flush=True)
        try:
            _assert_margin_equity_snapshot(db, p.margin_equity_pct)
            formation = None
            if selection_mode == "auto":
                formation = form_quality_prefix(db, generation_id, stamp, now_ms)
            _set_scan_progress(
                db, stage="selection_search", candidates_scanned=len(workset),
                candidates_total=len(workset),
            )
            selection_stamp = now_iso()
            # Selection reads final scores and per-wallet sector policies from watchlist.  Rebuild that
            # derived view first; otherwise newly-qualified wallets have no policy row and the canonical
            # portfolio loader correctly filters all of their fills, fabricating zero marginal profit.
            refresh_watchlist(
                db,
                selection_stamp,
                leaderboard_generation=generation_id,
                commit=False,
            )
            if selection_mode == "manual":
                held = {(addr or "").lower() for (addr,) in db.execute(
                    "SELECT DISTINCT addr FROM copy_position WHERE status='open'"
                ).fetchall()}
                profile_gate = {
                    (addr or "").lower(): (status, profile_generation, data_status)
                    for addr, status, profile_generation, data_status in db.execute(
                        "SELECT addr,status,profile_generation,COALESCE(data_status,'valid') FROM profile"
                    ).fetchall()
                }
                selection_rows = []
                for item in selection.current_selection_rows(db):
                    status, profile_generation, data_status = profile_gate.get(
                        item.addr.lower(), (None, None, None)
                    )
                    if status in {"active", "qualified"} and profile_generation == generation_id \
                            and data_status == "valid":
                        selection_rows.append(item)
                    elif item.addr.lower() in held:
                        selection_rows.append(replace(
                            item, role=selection.EXIT_ONLY, enabled=False,
                            reason="manual_target_failed_current_hard_gate:exit_only",
                            data_status=data_status or "invalid",
                        ))
                marginal = None
            else:
                _apply_formation_params(db, formation, selection_stamp)
                selection_rows, marginal = _build_explicit_selection(
                    db, generation_id, selection_stamp, now_ms, audit_stamp=stamp,
                    forced_core_order=(formation or {}).get("selected") or (),
                    formation_meta=(formation or {}).get("search") or {},
                    effective_qualifications=(formation or {}).get("qualifications") or {},
                    effective_scores=(formation or {}).get("scores") or {},
                )
            _assert_margin_equity_snapshot(db, p.margin_equity_pct)
            # Publication timestamps describe when the complete decision became visible, not when the
            # hours-long scan started.  Use one actual completion stamp for ready/selection/publish/history
            # so operational ordering remains monotonic and Observer reload commands have honest times.
            publication_stamp = now_iso()
            generation.mark_generation_ready(
                db,
                generation_id,
                profile_total=profiled_ok,
                profile_valid=valid_profiles,
                profile_deferred=deferred_profiles,
                profile_rejected=rejected,
                profile_complete=True,
                ready_at=publication_stamp,
            )
            selection.replace_selection_rows(
                db, generation_id, selection_rows, selected_at=publication_stamp,
            )
            market_validation = _selection_market_snapshot_validation(
                db, generation_id, selection_rows, now_ms,
            )
            for row in selection_rows:
                pipeline_audit._insert_event(
                    db,
                    stamp=stamp,
                    source="scan",
                    stage="selection",
                    addr=row.addr,
                    status=row.role,
                    reason=row.reason,
                    follow_score=row.follow_score,
                    payload={
                        "generation": generation_id,
                        "selectionRank": row.selection_rank,
                        "marginalUtility": row.utility,
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
            generation.publish_generation(db, generation_id, published_at=publication_stamp)
            current_core = _record_explicit_follow_history(
                db, selection_rows, publication_stamp, previous_core, generation_id,
            )
            active_strategy = strategy_revision.create_revision(
                db,
                generation_id,
                source="scanner",
                reason=("manual_selection_preserved" if selection_mode == "manual"
                        else "quality_prefix_formation"),
                validation={
                    **((formation or {}).get("search") or {}),
                    "marketSnapshot": market_validation,
                },
                stamp=publication_stamp,
            )
            pipeline_audit._insert_event(
                db,
                stamp=publication_stamp,
                source="scan",
                stage="strategy_revision",
                status="active",
                reason=active_strategy["source"],
                payload=active_strategy,
            )
            n_active = len(current_core)
            duration_s = time.time() - t0
            stage_metrics = {
                "durationSec": round(duration_s, 3),
                "leaderboardAndUniverseSec": round(harvest_done_at - harvest_started_at, 3),
                "perpPrefilterSec": round(prefilter_done_at - prefilter_started_at, 3),
                "dailySloSec": None,
                "dailySloMet": None,
                "profileDurationSec": round(profile_done_at - prefilter_done_at, 3),
                "profileSloSec": None,
                "profileSloMet": None,
                "officialRoiPassed": len(roi_cand),
                "perpPrefilterPassed": len(cand),
                "perpPrefilterDeferred": sum(result.deferred for result in perp_results.values()),
                "apiByStage": {
                    "leaderboard": harvest_api_stats,
                    "perpPrefilter": {
                        key: int(prefilter_api_stats.get(key, 0)) - int(harvest_api_stats.get(key, 0))
                        for key in prefilter_api_stats
                    },
                    "profile": {
                        key: int(profile_api_stats.get(key, 0)) - int(prefilter_api_stats.get(key, 0))
                        for key in profile_api_stats
                    },
                    "formation": {
                        key: int(rest.request_stats().get(key, 0)) - int(profile_api_stats.get(key, 0))
                        for key in profile_api_stats
                    },
                },
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
                "marginEquityPct": float(p.margin_equity_pct),
                "initialMarginEquity": float(config.INITIAL_BALANCE),
                "marketScopeAudit": scope_audit,
                "marketScopeCount": len(universe),
                "marketScopeHash": hashlib.sha256(
                    "\n".join(sorted(universe)).encode("utf-8")
                ).hexdigest(),
                "marketSnapshot": market_validation,
                "marketSnapshotProfiled": market_snapshot_audit,
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
            # Profiles/fill cache are already complete and durable.  A transient portfolio/path/tuner
            # failure must retain them for ``finalize-profiled`` instead of forcing another 825-wallet
            # network sweep merely because atomic publication did not complete.
            db.execute(
                "UPDATE scan_generation SET status='leaderboard_validated',complete=0,publishable=0,"
                "is_current=0,error=? WHERE generation=?",
                (f"finalize_error:{str(exc)[:500]}", generation_id),
            )
            pipeline_audit._insert_event(
                db,
                stamp=stamp,
                source="scan",
                stage="selection_summary",
                status="failed",
                reason=str(exc)[:300],
                payload={
                    "generation": generation_id,
                    "mode": selection_mode,
                    "retainedGeneration": selection.latest_published_generation(db),
                },
            )
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
        _set_scan_progress(db, stage="materialize_replay", candidates_scanned=len(workset))
        try:
            portfolio_replay = auto_tune.store_effective_portfolio_replay(db, generation_id)
        except Exception as exc:  # noqa: BLE001 - published strategy remains authoritative
            portfolio_replay = {"status": "error", "error": str(exc)[:300]}
        try:
            selection_replay = refresh_selection_copy_replay(
                db, generation_id, replayed_at=now_iso(),
            )
        except Exception as exc:  # noqa: BLE001
            selection_replay = {"status": "error", "error": str(exc)[:300]}
        tune_summary = {
            "status": "complete", "reason": "synchronous_quality_prefix_formation",
            "portfolioReplay": portfolio_replay, "selectionReplay": selection_replay,
        }
        pipeline_audit._insert_event(
            db,
            stamp=stamp,
            source="scan",
            stage="tuner_finalize",
            status=tune_summary.get("status"),
            reason=tune_summary.get("reason"),
            payload=tune_summary,
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
