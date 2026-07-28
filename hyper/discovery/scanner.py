"""Discovery domain: the rolling scanner that maintains the live watchlist.

harvest leaderboard -> coarse candidates -> profile work-set (actives + new + top rechecks)
over a short window -> perp episodes/metrics -> upsert active/rejected/retired.
Composes rest + fills + metrics + storage; holds no infra of its own.
"""
import calendar
import concurrent.futures
from dataclasses import replace
import gc
import hashlib
import json
import math
import os
import threading
import time
from types import SimpleNamespace

from hyper import config, params, storage
from hyper.copy.copy_backtest import (
    ADD_METRICS_VERSION,
    prepare_price_path,
    run_backtest,
    slice_backtest_result,
    subset_price_path,
)
from hyper.copy.fills import build_episodes
from hyper.copy.copy_data import (
    is_copyable_coin,
    load_copyable_fills,
    normalize_copyable_fills,
    out_of_scope_fills,
)
from hyper.copy.copy_policy import COPY_POLICY_PARAM_KEYS, load_copy_policy
from hyper.copy.economics import (
    OPEN_LOSS_RATIO_LIMIT,
    PROFITABILITY_BASIS,
    conservative_profitability,
    open_loss_ratio_within_limit,
    replay_result_profitability,
)
from hyper.copy.sector import (
    SECTORS,
    apply_allowed_sector_copy_metrics,
    classify_coin,
)
from hyper.copy.fill_transition import classify_fill_transition
from hyper.market import generation_market, price_path, rest, volatility
from hyper.selection import (
    auto_tune,
    core_formation,
    follow_score,
    pre_strict,
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
    clean Crypto trading). Every scan therefore evaluates each sector from the current fills first.
    HFT, grid, systematic slicing, excessive concurrency and Heavy-DCA are structural exclusions; none can
    be resurrected by later profitability.
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
        complete = [episode for episode in episodes if episode.get("open_complete", True)]
        heavy_limit = int(getattr(p, "max_single_adds", config.MAX_SINGLE_ADDS_PER_EP))
        heavy_count = sum(1 for episode in complete if int(episode.get("n_adds") or 0) > heavy_limit)
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
                     e["net_pnl"], e["fee"], e["max_notl"], e["n_fills"], e.get("n_oids", 0),
                     e["open_px"], e["close_px"], 1 if e.get("open_complete", True) else 0))
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


def _recent_former_core_addrs(db, *, as_of, recheck_days=None):
    """Keep recently removed Core wallets on the evidence-refresh surface.

    A legal empty-Core publication stops Observer from opening new copies. It must not also erase the
    research lane which can prove that a still-profitable former Core has recovered. Looking only at the
    latest empty selection would otherwise make every former member disappear before its next replay.
    Immutable selection rows are the authority here; older registry rows may predate Core lifecycle counters.
    """
    days = float(
        config.FORMER_CORE_EVIDENCE_RECHECK_DAYS if recheck_days is None else recheck_days
    )
    if days <= 0:
        return []
    current_core = {
        str(addr or "").lower()
        for addr in (selection.published_core_addrs(db) or ())
        if addr
    }
    return [
        str(addr or "").lower()
        for addr, _last_selected in db.execute(
            "SELECT lower(addr),MAX(selected_at) last_selected "
            "FROM follow_selection WHERE role='core' "
            "AND julianday(selected_at)>=julianday(?)-? "
            "GROUP BY lower(addr) ORDER BY last_selected DESC,lower(addr)",
            (as_of, days),
        ).fetchall()
        if addr and str(addr).lower() not in current_core
    ]


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
            "(addr,coin,side,open_ms,seq,close_ms,hold_s,net_pnl,fee,max_notl,n_fills,n_oids,"
            "open_px,close_px,open_complete) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            erows)
    stored = db.execute("SELECT COUNT(*) FROM episode WHERE addr=?", (addr,)).fetchone()[0]
    if stored != len(eps):
        raise RuntimeError(f"episode consistency failed for {addr}: stored {stored}, built {len(eps)}")


def repair_missing_episode_rows(db, addrs) -> int:
    """Rebuild missing episode rows from cached fills.

    Older scans could update profile/copy backtest evidence while leaving no episode detail rows. Rows created
    before order-aware structure evidence also have ``n_oids IS NULL`` and must be rebuilt once; treating their
    exchange fill count as an order count would repeat the large-wallet HFT false positive.
    """
    repaired = 0
    for addr in dict.fromkeys(a for a in addrs if a):
        episode_state = db.execute(
            "SELECT COUNT(*),SUM(CASE WHEN n_oids IS NULL THEN 1 ELSE 0 END) "
            "FROM episode WHERE addr=?",
            (addr,),
        ).fetchone()
        if episode_state and int(episode_state[0] or 0) > 0 and int(episode_state[1] or 0) == 0:
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

    New-wallet recall uses only seven-day volume and non-negative seven-/thirty-day PnL direction. Official
    Portfolio then confirms that the seven-day volume belongs to Perp.
    Current roles/open-position owners bypass this discovery-only decision and receive retention replay.
    """
    vlm_min = getattr(p, "week_vlm_min", config.HARVEST_WEEK_VLM_MIN)
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
        week_positive = wk_pnl >= pnl_min["week"]
        month_positive = month_pnl >= pnl_min["month"]
        r["is_candidate"] = int(
            wk_vlm >= vlm_min
            and week_positive
            and month_positive
        )
        r["fetched_at"] = fetched_at
        mon_vlm = f(mo.get("vlm"))
        r["daily_turnover"] = (mon_vlm / acct / 30.0) if acct > 0 else 0.0
        prepared.append(r)
    return prepared


def harvest(db, p, *, generation_id=None) -> int:
    """Leaderboard account/activity + positive PnL recall before official observed-history Perp ROI."""
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


def _leaderboard_recall_audit(db, generation_id, stamp, p):
    """Record the complete cheap Leaderboard recall surface for the generation."""
    # Keep this persisted stage name readable for older frozen-audit tooling and historical generations.
    # The stage no longer contains an ROI magnitude gate.
    pipeline_audit._delete_stage(db, stamp, "scan", "official_roi")
    rows = db.execute(
        "SELECT addr,is_candidate,account_value,week_vlm,week_pnl,week_roi,mon_pnl,mon_roi,all_pnl,all_roi "
        "FROM leaderboard_staging WHERE generation=? ORDER BY mon_roi DESC,addr",
        (generation_id,),
    ).fetchall()
    names = ("addr", "is_candidate", "accountValue", "weekVlm", "weekPnl", "weekRoi",
             "monthPnl", "monthRoi", "allPnl", "allRoi")
    for rank, row in enumerate(rows, 1):
        item = dict(zip(names, row))
        passed = bool(item.pop("is_candidate"))
        week_floor = getattr(p, "week_pnl_min", config.HARVEST_WEEK_PNL_MIN)
        month_floor = getattr(p, "month_pnl_min", config.HARVEST_MONTH_PNL_MIN)
        week_positive = f(item["weekPnl"]) >= week_floor
        month_positive = f(item["monthPnl"]) >= month_floor
        checks = {
            "week_volume_below_floor": f(item["weekVlm"]) < getattr(p, "week_vlm_min", config.HARVEST_WEEK_VLM_MIN),
            "week_pnl_below_floor": not week_positive,
            "month_pnl_below_floor": not month_positive,
        }
        failed_checks = [reason for reason, failed in checks.items() if failed]
        item["roiMagnitudeGateEnabled"] = False
        item["failedChecks"] = failed_checks
        addr = item.pop("addr")
        reason = failed_checks[0] if failed_checks else "discovery_recall_below_floor"
        pipeline_audit._insert_event(
            db, stamp=stamp, source="scan", stage="official_roi", addr=addr, rank=rank,
            status="passed" if passed else "rejected",
            reason="discovery_recall_passed" if passed else reason, payload=item,
        )
    db.commit()


def _run_perp_prefilter(db, addrs, p, stamp, *, allow_cache=True, source="scan"):
    """Confirm official seven-day Perp volume before downloading a new wallet's fills."""
    pipeline_audit._delete_stage(db, stamp, source, "perp_prefilter")
    # The delete starts a SQLite write transaction.  Release it before the first network request: holding the
    # single writer slot across a batch of rate-paced Portfolio calls freezes Observer marks and commands.
    db.commit()
    minima = {
        "week": getattr(p, "week_pnl_min", config.HARVEST_WEEK_PNL_MIN),
        "month": getattr(p, "month_pnl_min", config.HARVEST_MONTH_PNL_MIN),
        "all": getattr(p, "all_pnl_min", config.HARVEST_ALL_PNL_MIN),
    }
    share_min = getattr(p, "perp_pnl_share_min", config.HARVEST_PERP_PNL_SHARE_MIN)
    week_perp_volume_min = getattr(
        p, "week_vlm_min", config.HARVEST_WEEK_VLM_MIN,
    )
    copy_policy = load_copy_policy(getattr(p, "copy_bt_overrides", None))
    cache_policy = {
        "version": "official_perp_week_volume_v9",
        "weekPerpVolumeMin": float(week_perp_volume_min),
        "auditPnlMinima": {key: float(value) for key, value in minima.items()},
        "roiMagnitudeGateEnabled": False,
        "accountValueGateEnabled": False,
        "perpProfitShareGateEnabled": False,
    }
    addr_set = {str(addr).lower() for addr in addrs}
    cached_results = {}
    # A deployment/restart starts a new generation but does not make Portfolio evidence fetched minutes ago
    # stale. Reuse only exact-policy business decisions inside a short TTL; deferred transport failures are
    # deliberately retried. Every cache hit is copied into the new stamp's audit surface below.
    if allow_cache:
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
                str(status), str(reason or "perp_prefilter_cached"),
                dict(cached_payload.get("windows") or {}),
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
                result = perp_prefilter.evaluate(
                    payload,
                    pnl_minima=minima,
                    share_min=share_min,
                    min_return_30d=copy_policy.official_perp_min_return_30d,
                    min_return_7d=copy_policy.official_perp_min_return_7d,
                    long_history_days=copy_policy.official_perp_long_history_days,
                    short_history_days=copy_policy.official_perp_short_history_days,
                    max_boundary_gap_hours=(
                        copy_policy.official_perp_boundary_max_gap_hours
                    ),
                    min_week_perp_volume=week_perp_volume_min,
                )
        results[addr] = result
        # Buffer audit values in memory so no write transaction remains open during the next REST call.
        pending_audit.append({
            "stamp": stamp, "source": source, "stage": "perp_prefilter", "addr": addr,
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


def _source_quality_surface(fills: list, episodes: list, structure: dict, now_ms: int) -> dict:
    """Restrict source quality and recency to structurally executable specialties."""
    allowed = set((structure or {}).get("allowed") or ())
    scoped_fills = [
        fill for fill in (fills or ())
        if classify_coin(fill.get("coin")) in allowed
    ]
    scoped_episodes = [
        episode for episode in (episodes or ())
        if classify_coin(episode.get("coin")) in allowed
    ]
    return {
        **metrics.source_episode_quality(scoped_episodes, int(now_ms)),
        **_open_flow_metrics(scoped_fills, int(now_ms)),
    }


def _copy_profile_evidence(m, results, p, *, addr="", now_ms=None):
    """Derive normalized OOS evidence from canonical replay positions."""
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
    actionable_rate = primary.get("open_fill_rate")
    capacity_fit = primary.get("capacity_open_fit")
    master_coverage = primary.get("master_leverage_coverage")
    price_coverage = primary.get("price_path_coverage")
    coverage_parts = [x for x in (master_coverage, price_coverage) if x is not None]
    model_coverage = min(coverage_parts) if coverage_parts else 0.0
    closed_n = int(primary.get("closed_n") or 0)
    if not by_days or evidence.issubset({"", "no_fills", "no_open_events"}):
        evidence_status = "missing"
    else:
        evidence_status = "qualified"
    evidence_days = len({
        int(position.get("closed_at") or 0) // 86_400_000
        for position in positions if int(position.get("closed_at") or 0) > 0
    })
    m.update(
        data_status="valid",
        evidence_status=evidence_status,
        copy_evidence_days=evidence_days,
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
            stage="rough",
            policy_values=getattr(p, "copy_bt_overrides", None),
            as_of_ms=now_ms,
        )
        if not result.get("coreEligible"):
            reason = result.get("status") or "rough_copy_unqualified"
            # Evidence gaps remain observable Challenger material. A wallet whose
            # closed/conservative economics fail is outside the candidate pool;
            # keeping it active would let legacy watchlist projections present
            # an economically rejected wallet as a Challenger.
            return not follow_score.is_economic_rejection(reason), reason
    # Activity is a Core permission, not a Profile deletion rule.  The eligibility evaluator records stale
    # or missing flat->open activity as a Challenger reason, so its evidence remains visible and a later
    # actionable signal can restore Core candidacy without rebuilding the wallet from scratch.
    return True, "rough_copy_qualified" if copy_gate_enabled else "copy_gate_disabled"


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
    """Attach the source-quality prescore without turning it into another gate."""
    score = (
        follow_score.compute_source_quality_score(m)[0] if ok else 0.0
    )
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
    """Publish a current-generation front-funnel failure without an old-Core or star bypass."""
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


def _defer_official_evidence_profile(db, addr, prior, stamp, generation_id, gate):
    """Keep a normal but incomplete official month history visible as Challenger.

    This is not a transport/data corruption path: young accounts and trustworthy boundary gaps are useful
    candidates whose evidence still needs time.  They must not enter source Top40 or Core, but they also
    must not be disguised as an economic rejection or a quarantined fetch failure.
    """
    row = dict(prior or {})
    official_month = dict((gate.windows or {}).get("month") or {})
    official_return = dict((gate.windows or {}).get("officialPerp30d") or {})
    row.update(
        addr=addr,
        status="active",
        reason=str(gate.reason or "official_perp_evidence_incomplete")[:120],
        score=0.0,
        rough_copy_score=None,
        official_perp_status=gate.status,
        official_perp_reason=gate.reason,
        official_perp_evidence_json=json.dumps(
            gate.payload(), sort_keys=True, separators=(",", ":")
        ),
        official_perp_return_30d=official_return.get("return"),
        official_perp_pnl_30d=official_month.get("perpPnl"),
        official_perp_pnl_share=official_month.get("perpShare"),
        data_status="valid",
        evidence_status="official_evidence_building",
        profile_generation=generation_id,
        evaluated_at=stamp,
        times_seen=int((prior or {}).get("times_seen") or 0) + 1,
    )
    cols = storage.PROFILE_COLS.split(",")
    with _db_lock:
        db.execute(
            f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [row.get(column) for column in cols],
        )
        db.commit()
    return "active", row["reason"], row, False


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
    source_start_ms = now_ms - 30 * 86_400_000
    perp = [x for x in perp_full if x["time"] >= source_start_ms]
    perp_frac = 1.0 if perp else 0.0
    eps_full, open_eps = build_episodes(perp_full)
    eps = [
        episode for episode in eps_full
        if episode.get("open_complete", True)
        and int(episode.get("close_ms") or 0) >= source_start_ms
    ]
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
    m.update(metrics.window_nets(eps_full, now_ms))
    m.update(metrics.source_episode_quality(eps_full, now_ms))
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
    official = dict(getattr(p, "official_perp_results", {}) or {}).get(addr)
    m["official_perp_status"] = official.status if official is not None else "missing"
    m["official_perp_reason"] = (
        official.reason if official is not None else "official_perp_evidence_missing"
    )
    m["official_perp_evidence_json"] = (
        json.dumps(official.payload(), sort_keys=True, separators=(",", ":"))
        if official is not None else None
    )
    official_month = dict((official.windows if official is not None else {}).get("month") or {})
    official_return = dict(
        (official.windows if official is not None else {}).get("officialPerp30d") or {}
    )
    m["official_perp_return_30d"] = official_return.get("return")
    m["official_perp_pnl_30d"] = official_month.get("perpPnl")
    m["official_perp_pnl_share"] = official_month.get("perpShare")

    # STAGE A — cheap structural copyability (NO api). Front-of-funnel rejects (MM/HFT/grid/spot) that do
    # NOT kill a genuine trend trader. n_trades==0 (pure-hold) skips the episode-based checks → judged on
    # live positions in stage B. (Old behaviour auto-rejected n_trades==0 as 'no_closed_episode'.)
    sector_structure = _current_sector_structure_policy(perp_full, now_ms, p)
    # Source win-rate, Top3 concentration and 72-hour activity must describe markets we can actually follow.
    # A recent open or winning Episode in a structurally rejected specialty may not qualify the wallet.
    m.update(_source_quality_surface(perp_full, eps_full, sector_structure, now_ms))
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
        # Structural survivors all reach fills-only Copy. Source sample/PnL/PF/lottery/activity are evaluated
        # together with Copy evidence after the replay; no legacy win-rate, official ROI or 72h pre-gate may
        # truncate the dataset before the new funnel has the evidence it needs.
        m["source_quality_score"] = follow_score.compute_source_quality_score(
            m,
            policy_values=getattr(p, "copy_bt_overrides", None),
            as_of_ms=now_ms,
        )[0]

    if ok and getattr(p, "source_only_profile", False):
        # The global source-quality rank is not known until every deep-fill profile completes. Clear any
        # previous-generation Copy result now so it cannot bypass the new Top40 rough-replay boundary.
        m.update(
            copy_bt_net_pnl=None,
            copy_bt_closed_net_pnl=None,
            copy_bt_win_rate=None,
            copy_bt_closed_n=0,
            copy_bt_gross_profit=None,
            copy_bt_gross_loss=None,
            copy_bt_profit_factor=None,
            copy_bt_payoff_ratio=None,
            copy_bt_top3_profit_share=None,
            copy_bt_body_after_top3_n=0,
            copy_bt_body_after_top3_win_rate=None,
            copy_bt_body_after_top3_net_pnl=None,
            copy_bt_open_fill_rate=None,
            copy_bt_liquidations=0,
            copy_bt_max_liquidation_loss_pct=0.0,
            copy_bt_max_liquidation_loss=0.0,
            copy_bt_max_liquidation_loss_coin=None,
            copy_bt_max_liquidation_loss_closed_at=None,
            copy_bt_fee_drag=0.0,
            copy_bt_unrealized_pnl=0.0,
            copy_bt_valuation_status="pending",
            copy_bt_initial_margin_equity=None,
            copy_bt_window_start_equity=None,
            copy_bt_14d_net_pnl=None,
            copy_bt_14d_closed_net_pnl=None,
            copy_bt_14d_unrealized_pnl=0.0,
            copy_bt_14d_closed_n=0,
            copy_bt_14d_window_start_equity=None,
            copy_bt_7d_net_pnl=None,
            copy_bt_7d_closed_net_pnl=None,
            copy_bt_7d_unrealized_pnl=0.0,
            copy_bt_7d_closed_n=0,
            copy_bt_7d_window_start_equity=None,
            sector_copy_json=None,
            rough_copy_score=None,
            data_status="valid",
            evidence_status="source_qualified",
            copy_path_risk_status="pending",
        )
        reason = "source_structure_passed"
    elif ok:
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
    profile_scope = " AND p.profile_generation=?" if leaderboard_generation else ""
    cur = db.execute(
        "SELECT p.addr, l.display_name, p.score, p.roi_equity, l.mon_roi, p.net_pnl, p.acct_value, "
        "p.n_trades, p.trades_per_day, p.taker_frac_notl, p.median_hold_s, p.win_rate, p.max_drawdown, "
        "p.age_days, p.top_coin, p.market_type, p.tp_move_pct, p.roi_total, p.open_loss_frac, p.open_win_frac, "
        "p.perp_frac, p.lev_proxy, p.margin_type, p.cur_leverage, p.liq_worst_pct, "
        "p.times_active, p.first_added, p.last_fill_ms, "
        "p.copy_bt_net_pnl,p.copy_bt_win_rate,p.copy_bt_closed_n,p.copy_bt_open_fill_rate,"
        "p.copy_bt_liquidations,p.copy_bt_fee_drag,p.copy_bt_unrealized_pnl,p.copy_bt_valuation_status,"
        "p.copy_bt_initial_margin_equity,p.copy_bt_window_start_equity,"
        "p.copy_bt_14d_net_pnl,p.copy_bt_14d_unrealized_pnl,p.copy_bt_14d_closed_n,p.copy_bt_14d_window_start_equity,"
        "p.copy_bt_7d_net_pnl,p.copy_bt_7d_unrealized_pnl,p.copy_bt_7d_closed_n,p.copy_bt_7d_window_start_equity,"
        "p.sector_copy_json,p.sector_policy_json,"
        "p.profile_generation,p.evaluated_at,p.data_status,p.evidence_status,"
        "p.official_perp_status,p.official_perp_reason,p.official_perp_evidence_json,"
        "p.official_perp_return_30d,p.official_perp_pnl_30d,p.official_perp_pnl_share,"
        "p.source_episode_n_30d,p.source_episode_n_7d,p.source_win_rate_30d,p.source_win_rate_7d,"
        "p.source_net_pnl_30d,p.source_net_pnl_7d,p.source_active_days_30d,p.source_active_days_7d,"
        "p.open_unrealized,"
        "p.source_top3_profit_share,p.source_body_after_top3_n,p.source_body_after_top3_win_rate,"
        "p.source_body_after_top3_net_pnl,p.source_quality_score,p.rough_copy_score,"
        "p.copy_evidence_days,p.execution_score,p.actionable_open_rate,p.capacity_fit,p.open_probability_48h "
        f"FROM profile p {leaderboard_join} "
        f"WHERE p.status='active'{profile_scope} ORDER BY p.score DESC, p.addr",
        (leaderboard_generation, leaderboard_generation) if leaderboard_generation else (),
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
            follow_score_value=score,
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
    allow_loo=True,
):
    """Publish exactly the currently qualified profit-ordered Core prefix.

    There is no minimum count, promotion delay, incumbent tenure, or stale-Core retention path. Open copies
    are handled by the caller as Exit-only and never justify Core authority.
    """
    rows = {(row.get("addr") or "").lower(): row for row in profiles}
    previous_core = {
        (addr or "").lower() for addr, role in previous_roles.items() if role == selection.CORE
    }
    desired = tuple(dict.fromkeys((addr or "").lower() for addr in desired_order if addr))
    profit_order = desired
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
            and _formation_core_permission(qualification)
            and data_valid and enabled
        )
        nominated = data_valid and enabled and core_ok and addr in desired
        if nominated:
            selected.append(addr)
            reasons[addr] = "core_quality_selected"
        elif addr in previous_core:
            hard_removed.add(addr)
            reasons[addr] = (
                "portfolio_not_selected" if core_ok
                else qualification.get("status") or row.get("reason") or "core_not_selected"
            )
        elif row.get("status") in {"active", "qualified"}:
            reasons[addr] = qualification.get("status") or "portfolio_not_selected"

    # A final conditional check may shorten only the low-profit suffix. Testing every member and deleting
    # an arbitrary middle row would convert a deterministic profit prefix into an overfit subset.
    published = set(selected)
    max_removals = max(0, int(getattr(config, "CORE_LOO_MAX_REMOVALS", 2) or 0))
    min_net_gain = float(getattr(config, "CORE_LOO_MIN_NET_GAIN", 1.0) or 0.0)
    removed_by_loo = []
    robust_allowed = {
        tuple(sorted((addr or "").lower() for addr in membership if addr))
        for membership in (robust_allowed_memberships or ())
    }
    while allow_loo and len(published) > 1 and len(removed_by_loo) < max_removals:
        base = strict_evaluate(tuple(sorted(published)))
        ranked_order = tuple(addr for addr in profit_order if addr in published)
        removable = next(reversed(ranked_order), None)
        if removable is None:
            break
        without = strict_evaluate(tuple(sorted(published - {removable})))
        net_gain = f(without.net_pnl) - f(base.net_pnl)
        feasible = (
            f(without.net_pnl) > 0.0
            and f(without.stress_net_pnl) > 0.0
            and f(without.actionable_open_rate) >= load_copy_policy().min_actionable_open_rate
            and f(without.capacity_fit) >= load_copy_policy().min_capacity_fit
            and (
                not robust_allowed
                or tuple(sorted(published - {removable})) in robust_allowed
            )
        )
        if not feasible or net_gain < min_net_gain:
            break
        published.remove(removable)
        removed_by_loo.append(removable)
        reasons[removable] = "portfolio_negative_incremental_net"
        if removable in previous_core:
            hard_removed.add(removable)

    # Conditional contribution remains telemetry; the operator-facing Core rank is the immutable profit order.
    final_metrics = strict_evaluate(tuple(sorted(published)))
    base_utility = f(
        final_metrics.net_pnl if final_metrics.net_pnl is not None else final_metrics.net_lcb
    )
    contribution_rows = []
    desired_rank = {addr: rank for rank, addr in enumerate(desired)}
    for addr in published:
        without = strict_evaluate(tuple(sorted(published - {addr})))
        without_utility = f(
            without.net_pnl if without.net_pnl is not None else without.net_lcb
        )
        contribution_rows.append((base_utility - without_utility, -desired_rank.get(addr, 999999), addr))
    final_order = tuple(addr for addr in profit_order if addr in published)
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

    Isolated liquidations already lose their full allocated margin in ``copy_net_pnl``. Historical maximum
    drawdown remains telemetry only and does not reduce utility or veto membership. Individual final replay
    limits Core to three 30-day proxy liquidations.
    """
    usable = []
    for days, result in (windows or {}).items():
        if not result:
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
    net_pnl = f(replay_result_profitability(primary[1]).get("qualificationPnl"))
    stress_net = min(
        f(replay_result_profitability(row[1]).get("qualificationPnl"))
        for row in usable
    )
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
    drawdown_dollars = max(
        (
            num(result.get("max_drawdown"), 0.0)
            * num(
                result.get("window_start_equity"),
                num(result.get("initial_margin_equity"), float(config.INITIAL_BALANCE)),
            )
            for _days, result in usable
        ),
        default=0.0,
    )
    # Keep the legacy field populated for schema/API compatibility, but it now aliases net profit.
    risk_adjusted_utility = net_pnl
    return selection.PortfolioMetrics(
        net_pnl, stress_net, liquidations, actionable, capacity, max_dd, peak_deploy,
        cost_drag, net_pnl=net_pnl, stress_net_pnl=stress_net,
        drawdown_dollars=drawdown_dollars, risk_adjusted_utility=risk_adjusted_utility,
    )


def _paper_account_equity(db) -> float:
    """Current Paper equity used for the second publication-scale replay."""
    row = db.execute(
        "SELECT ca.balance + COALESCE(("
        "SELECT SUM(COALESCE(cp.unrealized_pnl,0)) FROM copy_position cp "
        "WHERE cp.status='open'"
        "),0) FROM copy_account ca WHERE ca.id=1"
    ).fetchone()
    value = f(row[0]) if row else 0.0
    return value if value > 0.0 else float(config.INITIAL_BALANCE)


def _selection_prefetch_candidates(db, generation_id=None, now_ms=None, limit=None) -> list[str]:
    """Return the bounded qualified universe needed for path prefetch without running selection."""
    limit = max(0, int(
        config.PRE_STRICT_QUEUE_MAX_N if limit is None else limit
    ))
    if not limit:
        return []
    if generation_id:
        rows = _quality_core_profiles(
            db, generation_id, core_only=False, now_ms=now_ms,
        )
        return [
            row["addr"] for row in _bounded_formation_candidates(
                rows, limit,
            )
        ]
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


def _quality_core_profiles(db, generation_id, *, core_only=True, now_ms=None) -> list[dict]:
    """Current-generation follow-quality profiles in immutable quality order.

    ``core_only=False`` returns the bounded Core+Challenger workset needed for final-parameter
    requalification; the default preserves the original Core-ready contract for callers/tests.
    """
    cur = db.execute(
        "SELECT p.addr,p.status,p.reason,p.score,p.profile_generation,p.data_status,p.evidence_status,p.last_copyable_open_ms,"
        "p.official_perp_status,p.official_perp_reason,p.official_perp_evidence_json,"
        "p.official_perp_return_30d,p.official_perp_pnl_30d,p.official_perp_pnl_share,"
        "p.source_episode_n_30d,p.source_episode_n_7d,p.source_win_rate_30d,p.source_win_rate_7d,"
        "p.source_net_pnl_30d,p.source_net_pnl_7d,p.source_active_days_30d,p.source_active_days_7d,"
        "p.open_unrealized,"
        "p.source_top3_profit_share,p.source_body_after_top3_n,p.source_body_after_top3_win_rate,"
        "p.source_body_after_top3_net_pnl,p.source_gross_profit_30d,p.source_gross_loss_30d,"
        "p.source_profit_factor_30d,p.source_payoff_ratio_30d,p.source_quality_score,p.rough_copy_score,"
        "p.copy_bt_closed_n,p.copy_bt_14d_closed_n,p.copy_bt_7d_closed_n,"
        "p.copy_evidence_days,p.execution_score,p.open_probability_48h,"
        "p.actionable_open_rate,p.capacity_fit,p.copy_bt_net_pnl,p.copy_bt_closed_net_pnl,p.copy_bt_win_rate,"
        "p.copy_bt_unrealized_pnl,p.copy_bt_valuation_status,"
        "p.copy_bt_initial_margin_equity,p.copy_bt_window_start_equity,"
        "p.copy_bt_14d_net_pnl,p.copy_bt_14d_closed_net_pnl,p.copy_bt_14d_unrealized_pnl,p.copy_bt_14d_window_start_equity,"
        "p.copy_bt_7d_net_pnl,p.copy_bt_7d_closed_net_pnl,p.copy_bt_7d_unrealized_pnl,p.copy_bt_7d_window_start_equity,"
        "p.copy_bt_open_fill_rate,p.copy_bt_liquidations,p.copy_bt_max_liquidation_loss_pct,"
        "p.copy_bt_max_liquidation_loss,p.copy_bt_max_liquidation_loss_coin,"
        "p.copy_bt_max_liquidation_loss_closed_at,p.copy_bt_fee_drag,"
        "p.copy_bt_gross_profit,p.copy_bt_gross_loss,p.copy_bt_profit_factor,p.copy_bt_payoff_ratio,"
        "p.copy_bt_top3_profit_share,p.copy_bt_body_after_top3_n,"
        "p.copy_bt_body_after_top3_win_rate,p.copy_bt_body_after_top3_net_pnl,"
        "p.sector_copy_json,p.sector_policy_json,p.acct_value,"
        "pse.activity_json AS pre_strict_activity_json,pse.policy_version AS pre_strict_policy_version,"
        "pse.status AS pre_strict_status,pse.first_failure AS pre_strict_first_failure,"
        "pse.tier AS pre_strict_tier,pse.queue_rank AS pre_strict_queue_rank,"
        "pse.rough_profit_priority AS pre_strict_profit_priority "
        "FROM profile p JOIN pre_strict_evidence pse ON pse.generation=? "
        "AND lower(pse.addr)=lower(p.addr) "
        "WHERE p.profile_generation=? AND pse.queue_rank IS NOT NULL",
        (generation_id, generation_id),
    )
    names = [desc[0] for desc in cur.description]
    controls = {
        (addr or "").lower(): bool(enabled)
        for addr, enabled in db.execute("SELECT addr,enabled FROM target_controls").fetchall()
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
    rows = []
    follow_values = params.load_follow(db)
    margin_equity_pct = follow_values.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
    policy_values = {**follow_values, **params.load_category(db, "scanner")}
    for raw in cur.fetchall():
        row = dict(zip(names, raw))
        addr = (row.get("addr") or "").lower()
        row["addr"] = addr
        row.update(forward_risk.get(addr) or {})
        row["margin_equity_pct"] = margin_equity_pct
        try:
            row["pre_strict_activity"] = json.loads(
                row.get("pre_strict_activity_json") or "{}"
            )
        except (TypeError, ValueError):
            row["pre_strict_activity"] = {}
        # Rough replay already evaluated this exact generation and persisted its score/reason. Formation must
        # consume that frozen hand-off instead of silently rebuilding the gate from a partial SELECT or a
        # later parameter/time surface. The tuned path-complete replay below is the next authoritative gate.
        row["score_as_of_ms"] = now_ms
        row["follow_score"] = (
            f(row.get("rough_copy_score"))
            if row.get("rough_copy_score") is not None
            else follow_score.compute_follow_score(
                row, policy_values=policy_values, stage="rough",
            )[0]
        )
        rough_passed = bool(
            row.get("pre_strict_status") == "passed"
            and row.get("pre_strict_queue_rank") is not None
            and row.get("rough_copy_score") is not None
        )
        row["follow_qualification"] = {
            "eligible": rough_passed,
            "coreEligible": rough_passed,
            "stageEligible": rough_passed,
            "stage": "rough",
            "status": (
                "pre_strict_qualified" if rough_passed
                else row.get("pre_strict_first_failure") or "pre_strict_not_qualified"
            ),
            "firstFailure": None if rough_passed else row.get("pre_strict_first_failure"),
            "role": "core_eligible" if rough_passed else "challenger",
            "deferred": False,
            "checks": {"frozenRoughCopyPassed": rough_passed},
            "reasons": [] if rough_passed else [
                str(row.get("pre_strict_first_failure") or "pre_strict_not_qualified")
            ],
        }
        qualified = (
            row.get("status") in {"active", "qualified"}
            and (row.get("follow_qualification") or {}).get("coreEligible")
        )
        if (
            qualified
            and (row.get("data_status") or "valid") == "valid"
            and controls.get(addr, True)
        ):
            rows.append(row)
    rows.sort(key=lambda row: (
        int(row.get("pre_strict_queue_rank") or 999999), row.get("addr") or "",
    ))
    return rows


def _effective_follow_replay(db, row, now_ms, *, generation_id, follow, valuation_marks,
                             sigmas=None, market_ctx=None, strict_path=True,
                             qualification_stage="strict") -> dict:
    """Replay one wallet under an explicit parameter surface without mutating its scan-time profile.

    This path-complete, cache-only pass is the authoritative individual liquidation/profitability check before
    a wallet may enter formation. One shared mark snapshot is supplied by the caller, so it adds CPU work but
    no per-wallet network request.
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
    if strict_path:
        # Only the at-most-16 final candidates consume refined K-line paths.
        path_start = int(now_ms) - (
            int(config.COPY_BT_DAYS) + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
        ) * 86_400_000
        replay_ctx.copy_bt_price_path = prepare_price_path(price_path.load_refined(
            db, evidence_fills, path_start, int(now_ms),
        ))
        replay_ctx.copy_bt_price_path_meta = price_path.coverage(
            db, evidence_fills, path_start, int(now_ms),
        )
    else:
        replay_ctx.copy_bt_price_path = None
        replay_ctx.copy_bt_price_path_meta = {}
    results = _copy_bt_results(
        addr, evidence_fills, int(now_ms), replay_ctx,
        valuation_marks=replay_ctx.copy_bt_valuation_marks,
    )
    sector_results = _sector_copy_bt_results(
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
    structural_policy = {
        "source": sector_policy.get("specializationSource") or "effective_parameter_replay",
    }
    for sector in SECTORS:
        item = sector_policy.get(sector)
        if isinstance(item, dict) and isinstance(item.get("structural"), dict):
            structural_policy[sector] = dict(item["structural"])
    effective = {}
    _apply_sector_copy_bt_gate(
        effective, results, sector_results, replay_ctx,
        structural_policy=structural_policy,
    )
    effective.update(_open_flow_metrics(evidence_fills, int(now_ms)))
    _copy_profile_evidence(effective, results, replay_ctx, addr=addr, now_ms=int(now_ms))
    for key in (
        "official_perp_status", "official_perp_reason", "official_perp_evidence_json",
    ):
        if row.get(key) is not None:
            effective[key] = row[key]
    for key in ("forward_net_pnl", "forward_liquidations", "forward_closed_n"):
        if row.get(key) is not None:
            effective[key] = row[key]
    # Freeze one executable-sector surface for score, qualification, formation and persistence.  The
    # historical path nulled ``sector_copy_json`` for scoring after qualification had already applied the
    # allowed-sector policy.  That made the final formation re-read the all-sector aggregate and made
    # persisted replay counts/returns disagree with the wallet's first failure reason.
    scoring_metrics = {
        **apply_allowed_sector_copy_metrics({**row, **effective}),
        "margin_equity_pct": replay_ctx.margin_equity_pct,
        "copy_replay_stage": qualification_stage,
        "score_as_of_ms": now_ms,
    }
    score, _detail = follow_score.compute_follow_score(
        scoring_metrics, policy_values=follow, stage=qualification_stage,
    )
    qualification = follow_score.evaluate_follow_eligibility(
        {
            **scoring_metrics,
            "copy_bt_data_status": scoring_metrics.get(
                "data_status", scoring_metrics.get("copy_bt_data_status")
            ),
            "copy_bt_evidence_status": scoring_metrics.get(
                "evidence_status", scoring_metrics.get("copy_bt_evidence_status")
            ),
        },
        stage=qualification_stage,
        margin_equity_pct=replay_ctx.margin_equity_pct,
        policy_values=follow,
        as_of_ms=now_ms,
        follow_score_value=score,
    )
    return {
        "metrics": scoring_metrics,
        "qualification": qualification,
        "score": score,
        "scoreDetail": _detail,
        "sectorPolicyJson": effective.get("sector_policy_json"),
        "results": results,
    }


def _source_quality_pool(db, generation_id: str, *, limit=None) -> tuple[list[str], list[str]]:
    """Return every structurally valid deep-fill profile for fills-only pre-strict evaluation."""
    del limit
    _apply_historical_major_liquidation_gate(db, generation_id)
    rows = db.execute(
        "SELECT lower(addr),COALESCE(source_quality_score,0) FROM profile "
        "WHERE profile_generation=? AND status='active' AND data_status='valid' "
        "AND reason='source_structure_passed' "
        "ORDER BY COALESCE(source_quality_score,0) DESC,lower(addr)",
        (generation_id,),
    ).fetchall()
    return [row[0] for row in rows], []


_MAJOR_LIQUIDATION_EVENT_TYPES = (
    "copy_single_liquidation_loss_over_5pct",
    "source_account_liquidated_zero",
)


def _record_wallet_risk_event(
    db, addr: str, event_type: str, event_key: str, *,
    occurred_at=None, coin=None, loss_usd=None, loss_pct=None, evidence=None,
) -> None:
    """Persist a confirmed admission veto independently from rolling discovery caches."""
    stamp = now_iso()
    db.execute(
        "INSERT INTO wallet_risk_event "
        "(addr,event_type,event_key,occurred_at,coin,loss_usd,loss_pct,evidence_json,"
        "first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(addr,event_type,event_key) DO UPDATE SET "
        "occurred_at=COALESCE(excluded.occurred_at,wallet_risk_event.occurred_at),"
        "coin=COALESCE(excluded.coin,wallet_risk_event.coin),"
        "loss_usd=COALESCE(excluded.loss_usd,wallet_risk_event.loss_usd),"
        "loss_pct=COALESCE(excluded.loss_pct,wallet_risk_event.loss_pct),"
        "evidence_json=COALESCE(excluded.evidence_json,wallet_risk_event.evidence_json),"
        "last_seen_at=excluded.last_seen_at",
        (
            str(addr or "").lower(), str(event_type), str(event_key),
            int(occurred_at) if occurred_at is not None else None,
            str(coin) if coin else None,
            f(loss_usd) if loss_usd is not None else None,
            f(loss_pct) if loss_pct is not None else None,
            json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True, default=float),
            stamp, stamp,
        ),
    )


def _historical_major_liquidation_addrs(db, addrs=None) -> set[str]:
    clauses = [
        f"event_type IN ({','.join('?' for _ in _MAJOR_LIQUIDATION_EVENT_TYPES)})",
    ]
    args = list(_MAJOR_LIQUIDATION_EVENT_TYPES)
    normalized = sorted({
        str(addr or "").lower() for addr in (addrs or ()) if addr
    })
    if addrs is not None:
        if not normalized:
            return set()
        clauses.append(f"lower(addr) IN ({','.join('?' for _ in normalized)})")
        args.extend(normalized)
    return {
        str(addr or "").lower()
        for (addr,) in db.execute(
            f"SELECT DISTINCT lower(addr) FROM wallet_risk_event WHERE {' AND '.join(clauses)}",
            args,
        ).fetchall()
        if addr
    }


def _apply_historical_major_liquidation_gate(db, generation_id: str) -> set[str]:
    generation_addrs = [
        addr for (addr,) in db.execute(
            "SELECT lower(addr) FROM profile WHERE profile_generation=?",
            (generation_id,),
        ).fetchall()
        if addr
    ]
    blocked = _historical_major_liquidation_addrs(db, generation_addrs)
    if blocked:
        marks = ",".join("?" for _ in blocked)
        db.execute(
            "UPDATE profile SET status='rejected',reason='historical_major_liquidation' "
            f"WHERE profile_generation=? AND lower(addr) IN ({marks})",
            (generation_id, *sorted(blocked)),
        )
    return blocked


def _store_pre_strict_evidence(
    db, generation_id: str, addr: str, qualification: dict,
    metrics_: dict, activity: dict, stamp: str,
) -> None:
    """Persist the immutable fills-only hand-off before queue ranking."""
    source = qualification.get("sourceEconomics") or {}
    copy = qualification.get("copyEconomics") or {}
    copy30 = copy.get("30") or {}
    copy7 = copy.get("7") or {}
    copy14 = conservative_profitability(
        metrics_.get("copy_bt_14d_closed_net_pnl"),
        metrics_.get("copy_bt_14d_unrealized_pnl"),
        start_equity=metrics_.get("copy_bt_14d_window_start_equity"),
    )
    payload = {
        **qualification,
        "activity": activity,
        "copyProfitFactor": f(metrics_.get("copy_bt_profit_factor")),
        "copyPayoffRatio": f(metrics_.get("copy_bt_payoff_ratio")),
        "sourceProfitFactor": f(metrics_.get("source_profit_factor_30d")),
        "sourcePayoffRatio": f(metrics_.get("source_payoff_ratio_30d")),
    }
    db.execute(
        "INSERT OR REPLACE INTO pre_strict_evidence ("
        "generation,addr,policy_version,model_version,status,first_failure,"
        "activity_json,latest_7d_active,active_weeks_4,weekly_open_counts_json,"
        "max_open_gap_days_28d,actionable_open_events_28d,actionable_open_events_7d,"
        "source_closed_n_30d,source_win_rate_30d,source_gross_profit_30d,"
        "source_gross_loss_30d,source_profit_factor_30d,source_payoff_ratio_30d,"
        "source_top3_profit_share,source_body_net_pnl,source_body_win_rate,"
        "copy_closed_n_30d,copy_win_rate_30d,copy_gross_profit_30d,copy_gross_loss_30d,"
        "copy_profit_factor_30d,copy_payoff_ratio_30d,copy_top3_profit_share,"
        "copy_body_net_pnl,copy_body_win_rate,rough_return_30d,rough_return_14d,"
        "rough_return_7d,rough_closed_pnl_30d,rough_closed_pnl_14d,"
        "rough_closed_pnl_7d,rough_open_loss_ratio_30d,rough_profit_priority,tier,"
        "queue_rank,strict_status,strict_first_failure,evidence_json,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            generation_id, str(addr).lower(), pre_strict.POLICY_VERSION,
            pre_strict.SELECTION_MODEL_VERSION,
            "passed" if qualification.get("eligible") else
                "deferred" if qualification.get("deferred") else "rejected",
            qualification.get("firstFailure"),
            json.dumps(activity, sort_keys=True, separators=(",", ":")),
            int(bool(activity.get("latest7dActive"))),
            int(activity.get("activeWeeks4") or 0),
            json.dumps(activity.get("weeklyOpenCountsOldestFirst") or []),
            activity.get("maxOpenGapDays28d"),
            int(activity.get("actionableOpenEvents28d") or 0),
            int(activity.get("actionableOpenEvents7d") or 0),
            int(metrics_.get("source_episode_n_30d") or 0),
            metrics_.get("source_win_rate_30d"),
            metrics_.get("source_gross_profit_30d"),
            metrics_.get("source_gross_loss_30d"),
            metrics_.get("source_profit_factor_30d"),
            metrics_.get("source_payoff_ratio_30d"),
            metrics_.get("source_top3_profit_share"),
            metrics_.get("source_body_after_top3_net_pnl"),
            metrics_.get("source_body_after_top3_win_rate"),
            int(metrics_.get("copy_bt_closed_n") or 0),
            metrics_.get("copy_bt_win_rate"),
            metrics_.get("copy_bt_gross_profit"),
            metrics_.get("copy_bt_gross_loss"),
            metrics_.get("copy_bt_profit_factor"),
            metrics_.get("copy_bt_payoff_ratio"),
            metrics_.get("copy_bt_top3_profit_share"),
            metrics_.get("copy_bt_body_after_top3_net_pnl"),
            metrics_.get("copy_bt_body_after_top3_win_rate"),
            copy30.get("qualificationReturn"),
            copy14.get("qualificationReturn"),
            copy7.get("qualificationReturn"),
            copy30.get("closedPnl"),
            copy14.get("closedPnl"),
            copy7.get("closedPnl"),
            copy30.get("openLossRatio"),
            qualification.get("profitPriority"),
            qualification.get("tier"),
            None, None, None,
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=float),
            stamp,
        ),
    )


def _finalize_pre_strict_queue(
    db, generation_id: str, *, allowed_addrs=None,
) -> list[str]:
    """Freeze the primary-first, reserve-fill queue independently from the Core cap."""
    db.execute(
        "UPDATE pre_strict_evidence SET queue_rank=NULL WHERE generation=?",
        (generation_id,),
    )
    allowed = None
    if allowed_addrs is not None:
        allowed = {
            str(addr or "").lower() for addr in allowed_addrs if addr
        }
    rows = db.execute(
        "SELECT pse.addr FROM pre_strict_evidence pse "
        "LEFT JOIN profile p ON lower(p.addr)=lower(pse.addr) "
        "WHERE pse.generation=? AND pse.status='passed' "
        "ORDER BY CASE pse.tier WHEN 'primary' THEN 0 WHEN 'reserve' THEN 1 ELSE 2 END,"
        "pse.rough_profit_priority DESC,pse.rough_return_30d DESC,pse.rough_return_7d DESC,"
        "pse.copy_profit_factor_30d DESC,COALESCE(p.rough_copy_score,p.score,0) DESC,"
        "lower(pse.addr)",
        (generation_id,),
    ).fetchall()
    queued = [
        str(row[0]).lower()
        for row in rows
        if allowed is None or str(row[0] or "").lower() in allowed
    ][:int(config.PRE_STRICT_QUEUE_MAX_N)]
    for rank, addr in enumerate(queued, 1):
        db.execute(
            "UPDATE pre_strict_evidence SET queue_rank=? "
            "WHERE generation=? AND lower(addr)=?",
            (rank, generation_id, addr),
        )
    return queued


def _pre_strict_counts(db, generation_id: str) -> dict:
    row = db.execute(
        "SELECT COUNT(*),"
        "SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN latest_7d_active=1 "
        "AND active_weeks_4>=? AND max_open_gap_days_28d<=? THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN status='passed' AND tier='primary' THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN status='passed' AND tier='reserve' THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN queue_rank IS NOT NULL THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN strict_status='qualified' THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN strict_status='deferred' THEN 1 ELSE 0 END) "
        "FROM pre_strict_evidence WHERE generation=?",
        (
            int(config.PRE_STRICT_ACTIVITY_MIN_ACTIVE_WEEKS),
            float(config.PRE_STRICT_ACTIVITY_MAX_OPEN_GAP_DAYS),
            generation_id,
        ),
    ).fetchone()
    values = list(row or ()) + [0] * 8
    return {
        "roughCopyCompleted": int(values[0] or 0),
        "preStrictPassed": int(values[1] or 0),
        "persistentActivityPassed": int(values[2] or 0),
        "preStrictPrimary": int(values[3] or 0),
        "preStrictReserve": int(values[4] or 0),
        "preStrictTop32": int(values[5] or 0),
        "strictQualified": int(values[6] or 0),
        "strictDeferred": int(values[7] or 0),
    }


def _rough_replay_source_pool(
    db, addrs, generation_id, now_ms, p, stamp, *, source="scan",
    queue_allowed_addrs=None,
) -> dict:
    """Run one cache-only, K-line-free Copy replay for every structural survivor.

    ``queue_allowed_addrs`` is set by Challenger daily refresh to the exact Core/strict-Challenger
    universe published by the latest complete generation. Current Core and open-position wallets still
    receive safety evidence, but cannot use the daily job as an alternative first-time promotion path.
    """
    addrs = list(dict.fromkeys((addr or "").lower() for addr in addrs if addr))
    if not addrs:
        return {"attempted": 0, "qualified": [], "failed": []}
    follow = {**params.load_follow(db), **params.load_category(db, "scanner")}
    valuation_marks = _current_copy_valuation_marks()
    qualified, failed = [], []
    cols = storage.PROFILE_COLS.split(",")
    resolver = getattr(p, "generation_market_resolver", None)
    for rank, addr in enumerate(addrs, 1):
        raw = db.execute(
            f"SELECT {storage.PROFILE_COLS} FROM profile WHERE lower(addr)=?",
            (addr,),
        ).fetchone()
        if raw is None:
            failed.append(addr)
            continue
        row = dict(zip(cols, raw))
        fills = _copy_bt_cached_fills(db, addr, int(now_ms), p)
        sigmas, market_ctx = {}, {}
        if resolver is not None:
            try:
                sigmas, market_ctx = resolver.ensure({
                    fill.get("coin") for fill in fills if fill.get("coin")
                })
            except generation_market.MarketSnapshotError as exc:
                row.update(
                    reason=f"rough_copy_market_data_error:{exc}",
                    data_status="deferred_data_error",
                    evidence_status="invalid",
                )
                db.execute(
                    f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [row.get(column) for column in cols],
                )
                failed.append(addr)
                continue
        marks = {
            coin: f((market_ctx.get(coin) or {}).get("mark_px"))
            for coin in market_ctx
            if f((market_ctx.get(coin) or {}).get("mark_px")) > 0.0
        }
        result = _effective_follow_replay(
            db, row, now_ms, generation_id=generation_id, follow=follow,
            valuation_marks={**valuation_marks, **marks},
            sigmas=sigmas, market_ctx=market_ctx,
            strict_path=False, qualification_stage="rough",
        )
        effective = dict(result.get("metrics") or {})
        activity = pre_strict.copy_activity(result.get("results") or {}, int(now_ms))
        qualification = pre_strict.evaluate(effective, activity, stage="rough")
        qualification["copyProfitFactor"] = f(effective.get("copy_bt_profit_factor"))
        row.update(effective)
        row.update(
            status="active" if qualification.get("eligible") or qualification.get("deferred") else "rejected",
            reason=qualification.get("status") or "pre_strict_unqualified",
            score=f(result.get("score")),
            rough_copy_score=f(result.get("score")),
            sector_policy_json=result.get("sectorPolicyJson") or row.get("sector_policy_json"),
            evaluated_at=stamp,
            profile_generation=generation_id,
            data_status=effective.get("data_status") or row.get("data_status") or "valid",
            evidence_status=effective.get("evidence_status") or row.get("evidence_status"),
        )
        db.execute(
            f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [row.get(column) for column in cols],
        )
        _store_pre_strict_evidence(
            db, generation_id, addr, qualification, effective, activity, stamp,
        )
        pipeline_audit._insert_event(
            db, stamp=stamp, source=source, stage="rough_copy", addr=addr, rank=rank,
            status="passed" if qualification.get("eligible") else
                "deferred" if qualification.get("deferred") else "rejected",
            reason=qualification.get("firstFailure") or "pre_strict_qualified",
            payload={
                "score": result.get("score"),
                "returns": {
                    days: (qualification.get("copyEconomics") or {}).get(days, {}).get(
                        "qualificationReturn"
                    )
                    for days in ("30", "7")
                },
                "profitFactor": effective.get("copy_bt_profit_factor"),
                "payoffRatio": effective.get("copy_bt_payoff_ratio"),
                "activity": activity,
                "rawTargetOpenN": effective.get("copy_bt_raw_target_open_n"),
                "smallOpenExcludedN": effective.get("copy_bt_small_open_excluded_n"),
                "effectiveTargetOpenN": effective.get("copy_bt_effective_target_open_n"),
                "openedN": effective.get("copy_bt_opened_n"),
                "closedN": qualification.get("closedN"),
            },
        )
        if qualification.get("firstFailure") == "copy_single_liquidation_loss_over_5pct":
            loss_pct = f(effective.get("copy_bt_max_liquidation_loss_pct"))
            loss_usd = f(effective.get("copy_bt_max_liquidation_loss"))
            coin = str(effective.get("copy_bt_max_liquidation_loss_coin") or "")
            closed_at = int(f(effective.get("copy_bt_max_liquidation_loss_closed_at")))
            _record_wallet_risk_event(
                db, addr, "copy_single_liquidation_loss_over_5pct",
                f"{coin or 'unknown'}:{closed_at or 'unknown'}",
                occurred_at=closed_at or None,
                coin=coin or None,
                loss_usd=loss_usd,
                loss_pct=loss_pct,
                evidence={
                    "generation": generation_id,
                    "stage": "rough",
                    "thresholdPct": (
                        load_copy_policy(follow).core_max_single_liquidation_loss_pct
                    ),
                },
            )
        if qualification.get("eligible"):
            qualified.append(addr)
        else:
            failed.append(addr)
        _set_scan_progress(
            db, stage="rough_copy", candidates_scanned=rank, candidates_total=len(addrs),
        )
    queued = _finalize_pre_strict_queue(
        db, generation_id, allowed_addrs=queue_allowed_addrs,
    )
    db.commit()
    return {
        "attempted": len(addrs), "qualified": qualified, "failed": failed,
        "queued": queued,
    }


def _prefix_eval_from_tune(count, tune_result, *, initial_balance):
    """Project one count-specific tune onto the adaptive Core-size search."""
    validation = dict((tune_result or {}).get("validation") or {})
    folds = list(validation.get("folds") or ())

    def side(prefix, proposal):
        net = sum(f(row.get(f"{prefix}Net")) for row in folds)
        max_dd = max((f(row.get(f"{prefix}MaxDD")) for row in folds), default=1.0)
        open_rate = min((f(row.get(f"{prefix}OpenRate")) for row in folds), default=0.0)
        capacity = min((f(row.get(f"{prefix}CapacityFit")) for row in folds), default=0.0)
        liquidations = sum(
            int(row.get(f"{prefix}Liquidations") or 0) for row in folds
        )
        return core_formation.PrefixEvaluation(
            count=int(count), net_pnl=net,
            stress_net_pnl=net, max_drawdown=max_dd,
            actionable_open_rate=open_rate, capacity_fit=capacity,
            liquidations=liquidations, params=dict(proposal or {}),
            payload={
                "initialBalance": float(
                    (folds[0].get(f"{prefix}StartEquity") if folds else None)
                    or initial_balance
                ),
                # Congestion selects wallet count here only. Individual/final admission prices missed opens
                # into PnL and must not reject the same execution friction a second time.
                "requireCongestionFit": True,
            },
        )

    challenger = side("challenger", (tune_result or {}).get("proposal") or {})
    baseline = side("baseline", (tune_result or {}).get("baseline_proposal") or {})
    if (tune_result or {}).get("eligible_to_apply") is False:
        return baseline
    feasible = [value for value in (challenger, baseline) if value.feasible]
    return max(
        feasible or [challenger, baseline],
        key=lambda value: value.utility,
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


def _automatic_formation_retune_enabled(db) -> bool:
    """Make the visible auto-tune switch authoritative for complete generation publication."""
    return bool(params.get(
        db, "AUTO_TUNE_MARGIN_ENABLE", config.AUTO_TUNE_MARGIN_ENABLE,
    ))


def _assert_automatic_formation_tuned(formation, *, required: bool) -> None:
    """Refuse to publish a non-empty tune pool on an untuned/invalid execution surface."""
    if not required:
        return
    search = dict((formation or {}).get("search") or {})
    tune_pool_n = int(search.get("tunePoolCount") or 0)
    if tune_pool_n <= 0:
        return
    reason = str(search.get("formationTuneReason") or "missing_tune_result")
    if search.get("formationTuneEligible") is not True:
        raise RuntimeError(f"automatic_core_tune_not_eligible:{reason}")
    if reason == "retune_disabled" or "using_active" in reason:
        raise RuntimeError(f"automatic_core_tune_not_executed:{reason}")


def _formation_membership_changed(formation, current_core) -> bool:
    """Membership includes deterministic order because order owns capital priority."""
    selected = tuple(
        str(addr or "").lower()
        for addr in ((formation or {}).get("selected") or ())
        if addr
    )
    previous = tuple(str(addr or "").lower() for addr in (current_core or ()) if addr)
    return selected != previous


_FORMATION_PREPATH_CHECKS = (
    "dataComplete",
    "sourceQualityPassed",
    "minimumClosedEvidence",
    "copyClosedProfit30d",
    "copyClosedProfit7d",
    "copyOpenLossRatio",
    "copyConservativeProfit30d",
    "copyConservativeProfit7d",
    "copy30dReturn",
    "copy7dReturn",
    "copyProfitFactor",
    "copyLottery",
    "openExecution",
    "activityOperational",
    "valuationComplete",
    "sectorExecutable",
    "liquidationsWithinLimit",
    "singleLiquidationLossWithinLimit",
)


def _select_formation_finalist_surface(
    db, tune_result, candidate_rows, *, base_follow, generation_id, now_ms,
    valuation_marks, sigmas, market_ctx, window_fills,
) -> tuple[dict, list[dict]]:
    """Choose parameters by the portfolio that remains after individual admission.

    The tuner ranks fills-only portfolio surfaces before the final per-wallet contract.  Selecting its raw
    all-wallet winner can therefore maximize profit from wallets which the next step immediately removes.
    Re-score the bounded finalists using the same individual 10%/3%, win-rate, execution and evidence
    checks, then compare the surviving shared portfolios on the already-prefetched K-line path.  This is a
    small finalist set, not the exploratory grid: it prevents a fills-only recent winner from reaching the
    publication transaction and turning negative only in the final strict replay.
    """
    tune_result = dict(tune_result or {})
    candidates = []
    selected = {}
    selected.update(tune_result.get("params") or {})
    selected.update(tune_result.get("add_params") or {})
    selected.update(tune_result.get("proposal") or {})
    if selected:
        candidates.append({
            "source": "tuner_winner",
            "params": selected,
            "liquidations": int(
                ((tune_result.get("validation") or {}).get("challengerLiquidations") or 0)
            ),
        })
    baseline = dict(tune_result.get("baseline_proposal") or {})
    if baseline:
        candidates.append({
            "source": "active_baseline",
            "params": baseline,
            "liquidations": int(
                ((tune_result.get("validation") or {}).get("baselineLiquidations") or 0)
            ),
        })
    for index, item in enumerate(tune_result.get("finalists") or (), 1):
        if not isinstance(item, dict) or item.get("eligible") is False:
            continue
        proposal = dict(item.get("params") or {})
        if not proposal:
            continue
        candidates.append({
            "source": f"finalist_{index}",
            "params": proposal,
            "liquidations": int(item.get("challengerLiquidations") or 0),
        })
    unique = []
    seen = set()
    for item in candidates:
        surface = {
            key: f(item["params"].get(key, base_follow.get(key)))
            for key in (*auto_tune.TUNE_KEYS, *auto_tune.ADD_TUNE_KEYS)
        }
        key = json.dumps(surface, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        unique.append({**item, "params": surface})
    if len(unique) <= 1 or not candidate_rows:
        return (unique[0]["params"] if unique else selected), []

    policy = load_copy_policy(base_follow)
    finalist_fills = list((window_fills or {}).get(30) or [])
    finalist_path = None
    finalist_path_meta = None
    if finalist_fills:
        path_start = int(now_ms) - (
            30 + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
        ) * 86_400_000
        finalist_path = prepare_price_path(
            price_path.load_refined(db, finalist_fills, path_start, int(now_ms))
        )
        finalist_path_meta = price_path.coverage(
            db, finalist_fills, path_start, int(now_ms),
        )
    paper_start_equity = _paper_account_equity(db)
    audits = []
    for item in unique:
        follow = {
            **base_follow, **item["params"], "AMBIGUOUS_PATH_MODE": "liquidate",
        }
        qualified = []
        individual_net = 0.0
        failure_counts = {}
        for row in candidate_rows:
            replay = _effective_follow_replay(
                db, row, now_ms, generation_id=generation_id, follow=follow,
                valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
                strict_path=False, qualification_stage="strict",
            )
            qualification = dict(replay.get("qualification") or {})
            checks = dict(qualification.get("checks") or {})
            passed = all(bool(checks.get(key)) for key in _FORMATION_PREPATH_CHECKS)
            if passed:
                qualified.append(row["addr"])
                replay_metrics = replay.get("metrics") or {}
                individual_net += f(
                    follow_score.compute_profit_priority(replay_metrics)[1]
                    .get("netPnl", {}).get("30d")
                )
            else:
                reason = str(
                    qualification.get("firstFailure")
                    or qualification.get("status")
                    or "prepath_not_qualified"
                )
                failure_counts[reason] = failure_counts.get(reason, 0) + 1

        portfolio_net = 0.0
        return_30d = return_7d = float("-inf")
        open_rate = capacity_fit = 0.0
        feasible = False
        if qualified:
            filtered = auto_tune._filter_window_fills_by_addr(
                window_fills, qualified,
            )
            windows = auto_tune._candidate_windows(
                db, qualified, sigmas, follow, now_ms,
                window_fills=filtered, market_ctx=market_ctx,
                path_rows=finalist_path, path_meta=finalist_path_meta,
                initial_balance=float(config.INITIAL_BALANCE),
            )
            primary = windows.get(30) or windows.get(max(windows)) or {}
            recent = windows.get(7) or {}
            portfolio_metrics = _portfolio_selection_metrics(
                windows, selected_n=len(qualified),
            )
            start_30d = f(
                primary.get("window_start_equity")
                or primary.get("initial_margin_equity")
                or base_follow.get("INITIAL_BALANCE")
                or config.INITIAL_BALANCE
            )
            start_7d = f(
                recent.get("window_start_equity")
                or recent.get("initial_margin_equity")
            )
            primary_economics = replay_result_profitability(primary)
            recent_economics = replay_result_profitability(recent)
            portfolio_net = f(primary_economics.get("qualificationPnl"))
            recent_net = f(recent_economics.get("qualificationPnl"))
            return_30d = (
                portfolio_net / start_30d if start_30d > 0.0 else float("-inf")
            )
            return_7d = (
                recent_net / start_7d if start_7d > 0.0 else float("-inf")
            )
            open_rate = f(portfolio_metrics.actionable_open_rate)
            capacity_fit = f(portfolio_metrics.capacity_fit)
            if abs(paper_start_equity - float(config.INITIAL_BALANCE)) <= 1e-9:
                paper_primary = primary
                paper_recent = recent
                paper_return_30d = return_30d
                paper_return_7d = return_7d
            else:
                paper_windows = auto_tune._candidate_windows(
                    db, qualified, sigmas, follow, now_ms,
                    window_fills=filtered, market_ctx=market_ctx,
                    path_rows=finalist_path, path_meta=finalist_path_meta,
                    initial_balance=paper_start_equity,
                )
                paper_primary = (
                    paper_windows.get(30)
                    or paper_windows.get(max(paper_windows))
                    or {}
                )
                paper_recent = paper_windows.get(7) or {}
                paper_equity_30d = f(
                    paper_primary.get("window_start_equity")
                    or paper_primary.get("initial_margin_equity")
                    or paper_start_equity
                )
                paper_equity_7d = f(
                    paper_recent.get("window_start_equity")
                    or paper_recent.get("initial_margin_equity")
                )
                paper_primary_economics = replay_result_profitability(paper_primary)
                paper_recent_economics = replay_result_profitability(paper_recent)
                paper_return_30d = (
                    f(paper_primary_economics.get("qualificationPnl")) / paper_equity_30d
                    if paper_equity_30d > 0.0 else float("-inf")
                )
                paper_return_7d = (
                    f(paper_recent_economics.get("qualificationPnl")) / paper_equity_7d
                    if paper_equity_7d > 0.0 else float("-inf")
                )
                del paper_windows
            path_complete = bool(
                finalist_path is None
                or (
                    f(primary.get("price_path_coverage"))
                    >= float(config.CORE_PRICE_PATH_MIN_COVERAGE)
                    and f(primary.get("maintenance_margin_coverage"))
                    >= float(config.CORE_MAINTENANCE_META_MIN_COVERAGE)
                )
            )
            feasible = bool(
                portfolio_net > 0.0
                and recent_net > 0.0
                and return_30d >= policy.portfolio_min_return_30d
                and return_7d >= policy.portfolio_min_return_7d
                and open_rate >= policy.min_actionable_open_rate
                and f(replay_result_profitability(paper_primary).get("qualificationPnl")) > 0.0
                and f(replay_result_profitability(paper_recent).get("qualificationPnl")) > 0.0
                and open_loss_ratio_within_limit(primary_economics)
                and open_loss_ratio_within_limit(replay_result_profitability(paper_primary))
                and paper_return_30d >= policy.portfolio_min_return_30d
                and paper_return_7d >= policy.portfolio_min_return_7d
                and path_complete
            )
            del windows
        audits.append({
            "source": item["source"],
            "params": item["params"],
            "qualifiedCount": len(qualified),
            "individualNetPnl": individual_net,
            "portfolioNetPnl": portfolio_net,
            "return30d": return_30d,
            "return7d": return_7d,
            "openRate": open_rate,
            "capacityFit": capacity_fit,
            "paperReturn30d": paper_return_30d if qualified else float("-inf"),
            "paperReturn7d": paper_return_7d if qualified else float("-inf"),
            "strictPath": bool(finalist_path is not None),
            "liquidations": int(item["liquidations"]),
            "feasible": feasible,
            "failureCounts": failure_counts,
        })

    feasible = [item for item in audits if item["feasible"]]
    if not feasible:
        return unique[0]["params"], audits
    best_net = max(item["portfolioNetPnl"] for item in feasible)
    near_best = [
        item for item in feasible
        if item["portfolioNetPnl"] >= best_net - max(
            1.0, abs(best_net) * float(config.AUTO_TUNE_NEAR_BEST_PROFIT_REL),
        )
    ]
    winner = min(
        near_best,
        key=lambda item: (
            item["liquidations"],
            -item["portfolioNetPnl"],
            -item["qualifiedCount"],
            item["source"],
        ),
    )
    return dict(winner["params"]), audits


def _formation_entry_eligibility(effective, score, *, policy_values=None) -> dict:
    """Apply the final path-complete individual contract before shared formation."""
    metrics_ = apply_allowed_sector_copy_metrics(dict(effective or {}))
    qualification = follow_score.evaluate_follow_eligibility(
        {
            **metrics_,
            "copy_bt_data_status": metrics_.get(
                "data_status", metrics_.get("copy_bt_data_status")
            ),
            "copy_bt_evidence_status": metrics_.get(
                "evidence_status", metrics_.get("copy_bt_evidence_status")
            ),
        },
        stage="strict",
        policy_values=policy_values,
        follow_score_value=f(score),
    )
    checks = dict(qualification.get("checks") or {})
    closed_n = int(f(metrics_.get("copy_bt_closed_n")))
    formation_checks = {
        "dataValid": not bool(qualification.get("deferred"))
            and qualification.get("role") != "quarantine",
        "sourceQualityPassed": bool(checks.get("sourceQualityPassed")),
        "strictCopy30dReturn": bool(checks.get("copy30dReturn")),
        "strictCopyRolling7dReturn": bool(checks.get("copy7dReturn")),
        "minimumClosedEvidence": bool(checks.get("minimumClosedEvidence")),
        "copyProfitFactor": bool(checks.get("copyProfitFactor")),
        "copyLotteryProtection": bool(checks.get("copyLottery")),
        "openExecution": bool(checks.get("openExecution")),
        "crossWeekActivity": bool(checks.get("activityOperational")),
        "valuationComplete": bool(checks.get("valuationComplete")),
        "pathRiskComplete": bool(checks.get("pathComplete")),
        "sectorExecutable": bool(checks.get("sectorExecutable")),
        "liquidationsWithinLimit": bool(checks.get("liquidationsWithinLimit")),
        "singleLiquidationLossWithinLimit": bool(
            checks.get("singleLiquidationLossWithinLimit")
        ),
    }
    required = (
        "dataValid",
        "sourceQualityPassed",
        "strictCopy30dReturn",
        "strictCopyRolling7dReturn",
        "minimumClosedEvidence",
        "copyProfitFactor",
        "copyLotteryProtection",
        "openExecution",
        "crossWeekActivity",
        "valuationComplete",
        "pathRiskComplete",
        "sectorExecutable",
        "liquidationsWithinLimit",
        "singleLiquidationLossWithinLimit",
    )
    return {
        "eligible": bool(qualification.get("coreEligible"))
            and all(bool(formation_checks.get(key)) for key in required),
        "requiredChecks": list(required),
        "checks": formation_checks,
        "closedN": closed_n,
        "score": f(score),
        "individualCoreEligible": bool(qualification.get("coreEligible")),
        "individualStatus": qualification.get("status"),
        "qualification": qualification,
    }


def _formation_core_permission(qualification) -> bool:
    """Read the formation contract, falling back for legacy/test qualification payloads."""
    qualification = dict(qualification or {})
    if "formationEligible" in qualification:
        return bool(qualification.get("formationEligible"))
    return bool(qualification.get("coreEligible"))


def _formation_prepath_candidate(row) -> bool:
    """Admit only wallets which passed the frozen-surface rough Copy contract."""
    qualification = dict((row or {}).get("follow_qualification") or {})
    return bool(
        qualification.get("coreEligible")
        and not qualification.get("deferred")
        and qualification.get("role") != "quarantine"
    )


def _bounded_formation_candidates(rows, limit) -> list[dict]:
    """Return the immutable primary-first pre-strict queue."""
    candidates = [
        row for row in rows if _formation_prepath_candidate(row)
    ]
    candidates.sort(key=lambda row: (
        int(row.get("pre_strict_queue_rank") or 999999),
        row.get("addr") or "",
    ))
    return candidates[:max(0, int(limit))]


def _core_prefix_retention() -> dict:
    return {
        "utility_retention": float(config.CORE_PREFIX_UTILITY_RETENTION),
        "net_retention": float(config.CORE_PREFIX_NET_RETENTION),
        "stress_retention": float(config.CORE_PREFIX_STRESS_RETENTION),
        "utility_slack": float(config.CORE_PREFIX_ABS_UTILITY_SLACK),
        "net_slack": float(config.CORE_PREFIX_ABS_NET_SLACK),
        "stress_slack": float(config.CORE_PREFIX_ABS_STRESS_SLACK),
    }


def _core_rebalance_due(db, current_core, *, now_ms: int, interval_days: int) -> tuple:
    """Age the current membership, not the daily evidence snapshot.

    Scheduled evidence refreshes publish a fresh generation even when the Core set is unchanged. Walking back through the
    consecutive generations with the same membership keeps those evidence refreshes from resetting the weekly
    rebalance clock.  Hard qualification failures still bypass this normal-cycle decision in formation.
    """
    rows = db.execute(
        "SELECT sg.generation,sg.published_at,lower(fs.addr),lower(fs.role),COALESCE(fs.enabled,1),"
        "COALESCE(fs.selection_rank,999999) "
        "FROM scan_generation sg LEFT JOIN follow_selection fs ON fs.generation=sg.generation "
        "WHERE sg.status='published' AND sg.complete=1 "
        "ORDER BY sg.published_at DESC,sg.id DESC,COALESCE(fs.selection_rank,999999),"
        "lower(fs.addr),fs.addr"
    ).fetchall()
    if not rows:
        return True, None
    snapshots = []
    for generation_id, published_at, addr, role, enabled, _rank in rows:
        if not snapshots or snapshots[-1][0] != generation_id:
            snapshots.append([generation_id, published_at, []])
        if role == selection.CORE and enabled and addr and addr not in snapshots[-1][2]:
            snapshots[-1][2].append(addr)
    wanted = tuple((addr or "").lower() for addr in (current_core or ()) if addr)
    snapshots = [
        (generation_id, published_at, tuple(members))
        for generation_id, published_at, members in snapshots
    ]
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

    Individual profile evidence may still be useful for Challenger classification, but it is not
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
    policies = {
        (row.get("addr") or "").lower(): row.get("sector_policy_json")
        for row in rows if row.get("addr") and row.get("sector_policy_json")
    }
    admission = [{
        "addr": (row.get("addr") or "").lower(),
        "passed": _formation_core_permission(row.get("follow_qualification")),
        "status": (row.get("follow_qualification") or {}).get("status") or "unknown",
    } for row in rows if row.get("addr")]
    return {
        "selected": (),
        "ranked": tuple((row.get("addr") or "").lower() for row in rows if row.get("addr")),
        "params": {},
        "evaluations": (),
        "qualifications": qualifications,
        "scores": scores,
        "policies": policies,
        "search": {
            "algorithm": "profit_priority_then_unified_tune_v1",
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


def form_quality_prefix(db, generation_id, stamp, now_ms=None, *, retune=True,
                        force_entry_requalification=False, force_retune=False) -> dict:
    """Certify wallets once, search fills quickly, then seal one final strict surface."""
    now_ms = int(now_ms or time.time() * 1000)
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
    current_core = (
        () if force_entry_requalification else tuple(selection.published_core_addrs(db) or ())
    )
    core_upper = max(1, min(
        int(config.MAX_TARGETS),
        int(getattr(config, "CORE_TARGET_MAX_N", 16)),
        int(params.get(
            db, "CORE_INITIAL_MAX_N", config.CORE_INITIAL_MAX_N,
        ) or config.CORE_INITIAL_MAX_N),
    ))
    queue_upper = max(core_upper, int(config.PRE_STRICT_QUEUE_MAX_N))
    all_ranked_candidates = _quality_core_profiles(
        db, generation_id, core_only=False, now_ms=now_ms,
    )
    pre_strict_candidates = _bounded_formation_candidates(
        all_ranked_candidates,
        queue_upper,
    )
    # First strict pass owns path/data/market validity and the strict profit rerank, but intentionally does
    # not pre-reject return magnitude, normal liquidation count, capacity or open-rate misses which unified
    # tuning may repair. It is the only bridge from the generation-frozen Top32 into the bounded Top16.
    prepath_rows = []
    prepath_rejected = []
    for row in pre_strict_candidates:
        effective = _effective_follow_replay(
            db, row, now_ms, generation_id=generation_id, follow=base_follow,
            valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
            strict_path=True, qualification_stage="strict",
        )
        qualification = dict(effective.get("qualification") or {})
        status = str(qualification.get("status") or "strict_current_surface_unknown")
        hard_invalid = bool(
            qualification.get("deferred")
            or qualification.get("role") == "quarantine"
            or status in {
                "copy_single_liquidation_loss_over_5pct",
                "historical_major_liquidation",
                "source_account_liquidated_zero",
                "sector_not_executable",
                "effective_sector_policy_missing",
                "add_metrics_version_mismatch",
            }
        )
        addr = row["addr"]
        db.execute(
            "UPDATE pre_strict_evidence SET strict_status=?,strict_first_failure=? "
            "WHERE generation=? AND lower(addr)=?",
            (
                "deferred" if qualification.get("deferred") else
                    "current_surface_hard_rejected" if hard_invalid else "current_surface_path_valid",
                status if hard_invalid else None,
                generation_id, addr,
            ),
        )
        if hard_invalid:
            prepath_rejected.append(addr)
            continue
        metrics_ = dict(effective.get("metrics") or {})
        prepath_rows.append({
            **row,
            **metrics_,
            "follow_score": f(effective.get("score")),
            "_current_surface_qualification": qualification,
        })
    prepath_rows.sort(key=lambda row: follow_score.profit_priority_sort_key(
        row,
        follow_score_value=f(row.get("follow_score")),
        addr=row.get("addr") or "",
    ))
    ranked_candidates = prepath_rows[:core_upper]
    rebalance_interval = max(1, int(params.get(
        db, "CORE_REBALANCE_INTERVAL_DAYS", config.CORE_REBALANCE_INTERVAL_DAYS,
    ) or 1))
    rebalance_due, core_age_days = _core_rebalance_due(
        db, current_core, now_ms=now_ms, interval_days=rebalance_interval,
    )
    # Every complete generation may publish the membership proven by its current strict replay. The expensive
    # parameter grid remains periodic; it must not also freeze wallet membership or overwrite a newly proven
    # set with the previous Core merely because the parameter-retune interval has not elapsed.
    retune = bool(retune and (force_retune or rebalance_due))
    # Profile construction already performed the cheap profitability, evidence and valuation checks that
    # establish this rough profit order. Do not run a path-complete individual replay on the active/default surface:
    # that duplicated the expensive work and could reject a wallet for parameters the following tuner exists
    # to repair. The winning surface below receives the one authoritative per-wallet strict replay.
    tune_ranked = ranked_candidates[:core_upper]
    if not tune_ranked:
        return _explicit_empty_core_formation(
            ranked_candidates, reason="no_core_qualified_wallets", tunePoolCount=0,
        )
    tune_ordered = tuple(row["addr"] for row in tune_ranked)

    # A generation with individually promising profiles but no copyable portfolio fills is a valid zero-Core
    # outcome, not a reason to roll back to stale members. Preflight the exact bounded tune pool before the
    # tuner so ``maybe_tune_margins`` cannot turn ``no_cached_fills`` into a publication failure.
    tune_window_fills = auto_tune._portfolio_window_fills(
        db, list(tune_ordered), now_ms, include_watch=True,
    )
    if tune_window_fills is None or not any(tune_window_fills.values()):
        return _explicit_empty_core_formation(
            ranked_candidates,
            reason=("fill_cache_guard" if tune_window_fills is None else "no_cached_fills"),
            tunePoolCount=len(tune_ordered),
        )

    tune_eligible = None
    tune_reason = "retune_disabled"
    tune_search = None
    tune_runs = {}
    chosen_run = {}
    winning_count = len(tune_ordered)
    finalist_admission_audit = []
    retention = _core_prefix_retention()
    if retune:
        # Wallet count and sizing are coupled. Search 16 -> 8 -> 12 (plus the bounded neighbours) with a
        # sparse count-specific tuner, then pay for the full grid only on the winning count.  The later
        # quality/membership pass may still publish fewer than eight wallets; eight is only the lower bound
        # of this congestion-search bracket, never a Core quota.
        timed_out_counts = []

        def coarse_tune_evaluate(count):
            count = int(count)
            _set_scan_progress(
                db, stage="portfolio_tune_coarse", candidates_scanned=count,
                candidates_total=len(tune_ordered),
            )
            try:
                result = auto_tune.maybe_tune_margins(
                    db, source="core_formation_coarse",
                    stamp=f"{stamp}:coarse:k{count}",
                    dry_run=True, mode="apply", follow_values=base_follow,
                    data_complete=True, addrs_override=list(tune_ordered[:count]),
                    record_run=False, formation_admission=True,
                    market_generation=generation_id, search_profile="coarse",
                    time_budget_s=float(config.AUTO_TUNE_COARSE_TIME_BUDGET_SEC),
                )
            except TimeoutError:
                db.rollback()
                timed_out_counts.append(count)
                return core_formation.PrefixEvaluation(
                    count=count, net_pnl=-1e12, stress_net_pnl=-1e12,
                    max_drawdown=1.0, actionable_open_rate=0.0, capacity_fit=0.0,
                    liquidations=1, params={},
                    payload={"initialBalance": f(
                        base_follow.get("INITIAL_BALANCE") or config.INITIAL_BALANCE
                    ), "requireCongestionFit": True, "coarseTuneTimeout": True},
                )
            if result.get("status") != "ok":
                raise RuntimeError(
                    "core_coarse_tune_failed:"
                    + str(result.get("reason") or result.get("status"))
                )
            tune_runs[count] = result
            db.commit()
            return _prefix_eval_from_tune(
                count, result,
                initial_balance=f(
                    base_follow.get("INITIAL_BALANCE") or config.INITIAL_BALANCE
                ),
            )

        tune_search_floor = 1 if len(tune_ordered) <= 8 else 8
        try:
            tune_search = core_formation.search_quality_prefix(
                len(tune_ordered), coarse_tune_evaluate,
                retention_kwargs=retention,
                tie_tolerance=float(config.CORE_PREFIX_TIE_TOLERANCE),
                exhaustive_below=int(
                    getattr(config, "CORE_PREFIX_EXHAUSTIVE_MAX_N", 8) or 0
                ),
                required_count=tune_search_floor,
            )
            winning_count = int(tune_search.selected.count)
        except RuntimeError as exc:
            if str(exc) != "no_feasible_quality_prefix":
                raise
            # Even if the 8-wallet bracket is congested, do not manufacture zero Core. Full-tune the
            # smallest bracket once; fixed-surface membership remains free to reduce below eight.
            winning_count = max(1, tune_search_floor)
            tune_reason = (
                "coarse_prefix_timeout_fallback"
                if timed_out_counts else "coarse_prefix_congestion_fallback"
            )

        coarse_winner = tune_runs.get(winning_count) or {}
        _set_scan_progress(
            db, stage="portfolio_tune_full", candidates_scanned=winning_count,
            candidates_total=len(tune_ordered),
        )
        try:
            full_run = auto_tune.maybe_tune_margins(
                db, source="core_formation_full", stamp=f"{stamp}:full:k{winning_count}",
                dry_run=True, mode="apply", follow_values=base_follow, data_complete=True,
                addrs_override=list(tune_ordered[:winning_count]), record_run=False,
                formation_admission=True, market_generation=generation_id,
                search_profile="full",
                time_budget_s=float(config.AUTO_TUNE_TIME_BUDGET_SEC),
            )
            if full_run.get("status") != "ok":
                raise RuntimeError(
                    "core_full_tune_failed:"
                    + str(full_run.get("reason") or full_run.get("status"))
                )
            db.commit()
            finalist_surface, finalist_admission_audit = (
                _select_formation_finalist_surface(
                    db, full_run, tune_ranked,
                    base_follow=base_follow, generation_id=generation_id,
                    now_ms=now_ms, valuation_marks=valuation_marks,
                    sigmas=sigmas, market_ctx=market_ctx,
                    window_fills=tune_window_fills,
                )
            )
            chosen_run = {**full_run, "proposal": finalist_surface}
            tuned_params, tune_eligible, tune_reason = _formation_param_surface(
                base_follow, chosen_run, retune=True,
            )
        except TimeoutError as exc:
            db.rollback()
            chosen_run = coarse_winner
            if chosen_run:
                tuned_params, tune_eligible, coarse_reason = _formation_param_surface(
                    base_follow, chosen_run, retune=True,
                )
                tune_reason = f"full_tune_timeout_using_coarse:{exc}:{coarse_reason}"
            else:
                tuned_params, tune_eligible, _unused = _formation_param_surface(
                    base_follow, None, retune=False,
                )
                tune_eligible = False
                tune_reason = f"full_tune_timeout_using_active:{exc}"
    else:
        tuned_params, tune_eligible, tune_reason = _formation_param_surface(
            base_follow, None, retune=False,
        )
    fixed_follow = {**base_follow, **tuned_params, "AMBIGUOUS_PATH_MODE": "liquidate"}
    # ``winning_count`` says which score prefix fitted this parameter surface; it must not permanently
    # delete the rest of the bounded Top16 before strict replay.  Every Top16 wallet receives the winning
    # surface, individual failures are removed, and only then may the shared-account prefix search choose
    # its final count.  Otherwise fitting k=9 silently made ranks 10–16 ineligible without evaluating them.
    tuned_candidate_rows = list(ranked_candidates)

    def replay_effective_surface(follow_surface):
        qualifications = {}
        scores = {}
        score_details = {}
        profit_priorities = {}
        policies = {}
        metrics = {}
        qualified_rows = []
        audit = []
        rejected = []
        surface_key = hashlib.sha256(json.dumps(
            {**follow_surface, "AMBIGUOUS_PATH_MODE": "liquidate"},
            sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()
        for row in tuned_candidate_rows:
            effective = _effective_follow_replay(
                db, row, now_ms, generation_id=generation_id, follow=follow_surface,
                valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
            )
            qualification = dict(effective.get("qualification") or {})
            addr = row["addr"]
            formation = _formation_entry_eligibility(
                effective.get("metrics") or {}, effective.get("score"),
                policy_values=follow_surface,
            )
            qualification["individualCoreEligible"] = bool(
                formation.get("individualCoreEligible")
            )
            qualification["formationEligible"] = bool(formation.get("eligible"))
            qualification["formationStatus"] = (
                "formation_eligible"
                if formation.get("eligible") else "formation_entry_rejected"
            )
            qualification["formationChecks"] = dict(formation.get("checks") or {})
            qualification["formationEvidence"] = {
                key: formation.get(key)
                for key in ("closedN", "score")
            }
            db.execute(
                "UPDATE pre_strict_evidence SET strict_status=?,strict_first_failure=? "
                "WHERE generation=? AND lower(addr)=?",
                (
                    "qualified" if formation.get("eligible") else
                        "deferred" if qualification.get("deferred") else "rejected",
                    None if formation.get("eligible") else (
                        qualification.get("firstFailure")
                        or qualification.get("formationStatus")
                    ),
                    generation_id, addr,
                ),
            )
            if not bool(
                (qualification.get("checks") or {}).get(
                    "singleLiquidationLossWithinLimit"
                )
            ):
                severe_metrics = dict(effective.get("metrics") or {})
                loss_pct = f(severe_metrics.get("copy_bt_max_liquidation_loss_pct"))
                loss_usd = f(severe_metrics.get("copy_bt_max_liquidation_loss"))
                coin = str(
                    severe_metrics.get("copy_bt_max_liquidation_loss_coin") or ""
                )
                closed_at = int(f(
                    severe_metrics.get("copy_bt_max_liquidation_loss_closed_at")
                ))
                _record_wallet_risk_event(
                    db, addr, "copy_single_liquidation_loss_over_5pct",
                    f"{coin or 'unknown'}:{closed_at or 'unknown'}",
                    occurred_at=closed_at or None,
                    coin=coin or None,
                    loss_usd=loss_usd,
                    loss_pct=loss_pct,
                    evidence={
                        "generation": generation_id,
                        "stage": "strict",
                        "paramsHash": surface_key,
                    },
                )
            qualifications[addr] = qualification
            scores[addr] = f(effective.get("score"))
            metrics[addr] = dict(effective.get("metrics") or {})
            priority, priority_detail = follow_score.compute_profit_priority(metrics[addr])
            profit_priorities[addr] = priority
            score_details[addr] = {
                **dict(effective.get("scoreDetail") or {}),
                "profitPriority": priority_detail,
                "strictQuality": {
                    "profitFactor": metrics[addr].get("copy_bt_profit_factor"),
                    "payoffRatio": metrics[addr].get("copy_bt_payoff_ratio"),
                    "top3ProfitShare": metrics[addr].get("copy_bt_top3_profit_share"),
                    "bodyAfterTop3N": metrics[addr].get("copy_bt_body_after_top3_n"),
                    "bodyAfterTop3WinRate": metrics[addr].get(
                        "copy_bt_body_after_top3_win_rate"
                    ),
                    "bodyAfterTop3NetPnl": metrics[addr].get(
                        "copy_bt_body_after_top3_net_pnl"
                    ),
                },
                "preStrict": {
                    "policyVersion": row.get("pre_strict_policy_version"),
                    "tier": row.get("pre_strict_tier"),
                    "queueRank": row.get("pre_strict_queue_rank"),
                    "activity": row.get("pre_strict_activity"),
                },
            }
            if effective.get("sectorPolicyJson"):
                policies[addr] = effective["sectorPolicyJson"]
            replay_invalid = bool(
                qualification.get("deferred") or qualification.get("role") == "quarantine"
            )
            # A data/path failure belongs to this wallet, not to the whole generation. Publish the exact
            # quarantine label and keep searching the remaining candidates.
            passed = bool(qualification.get("formationEligible"))
            audit.append({
                "addr": addr,
                "passed": passed,
                "status": qualification.get("formationStatus")
                or qualification.get("status") or "unknown",
                "individualStatus": qualification.get("status") or "unknown",
            })
            if replay_invalid:
                rejected.append(addr)
                continue
            if passed:
                qualified_rows.append(row)
            else:
                rejected.append(addr)
        return (
            qualifications, scores, score_details, profit_priorities,
            policies, metrics, qualified_rows,
            audit, rejected, surface_key,
        )

    # The active/default surface is only a discovery baseline.  Core eligibility must be measured on the
    # count-specific winning surface; otherwise a wallet is rejected for needing the very tuning step that
    # just succeeded.
    qualification_follow = fixed_follow
    (effective_qualifications, effective_scores, effective_score_details,
     effective_profit_priorities,
     effective_policies, effective_metrics, effective_ranked,
     admission_audit, qualification_rejected,
     effective_surface_hash) = replay_effective_surface(
        qualification_follow
    )
    tune_coverage_fallback = False
    effective_ranked.sort(key=lambda row: follow_score.profit_priority_sort_key(
        effective_metrics.get(row["addr"]) or {},
        follow_score_value=effective_scores.get(row["addr"], 0.0),
        addr=row["addr"],
    ))
    ordered = tuple(row["addr"] for row in effective_ranked[:core_upper])
    if not ordered:
        return {
            "selected": (), "ranked": (), "params": {}, "evaluations": (),
            "qualifications": effective_qualifications,
            "scores": effective_scores,
            "scoreDetails": effective_score_details,
            "profitPriorities": effective_profit_priorities,
            "policies": effective_policies,
            "walletMetrics": effective_metrics,
            "replayParamsHash": effective_surface_hash,
            "search": {
                "algorithm": "profit_priority_then_unified_tune_v1", "initialCount": 0,
                "selectedCount": 0,
                "explicitEmptyCore": True,
                "tunePoolCount": len(tune_ordered),
                "tunedInputCount": winning_count,
                "fullTuneRuns": 1 if chosen_run.get("search_profile") == "full" else 0,
                "effectiveRejected": qualification_rejected,
                "formationTuneEligible": tune_eligible,
                "formationTuneReason": tune_reason,
                "tuneCoverageFallback": tune_coverage_fallback,
                "formationTuneFinalists": list(chosen_run.get("finalists") or ()),
                "formationMarginRounds": list(chosen_run.get("margin_rounds") or ()),
                "formationFinalistAdmission": finalist_admission_audit,
                "qualificationRejected": qualification_rejected,
                "admission": admission_audit,
            },
        }
    window_fills = auto_tune._portfolio_window_fills(
        db, list(ordered), now_ms, include_watch=True,
    )
    if window_fills is None or not any(window_fills.values()):
        raise RuntimeError("core_prefix_replay_unavailable")
    membership_eval_cache = {}
    membership_replay_cache = {}

    def evaluate_members(addrs):
        key = tuple(sorted(dict.fromkeys(addrs)))
        if key in membership_eval_cache:
            return membership_eval_cache[key]
        if not key:
            value = core_formation.PrefixEvaluation(
                count=0, net_pnl=0.0, stress_net_pnl=0.0, max_drawdown=0.0,
                actionable_open_rate=1.0, capacity_fit=1.0, liquidations=0,
                params=tuned_params,
                payload={
                    "initialBalance": f(
                        base_follow.get("INITIAL_BALANCE") or config.INITIAL_BALANCE
                    ),
                    "return30d": 0.0,
                    "return7d": 0.0,
                },
            )
            membership_eval_cache[key] = value
            membership_replay_cache[key] = {
                "pnlConcentration": {},
                "return30d": 0.0,
                "return7d": 0.0,
            }
            return value
        _set_scan_progress(
            db, stage="portfolio_tune", candidates_scanned=len(key), candidates_total=len(ordered),
        )
        filtered = auto_tune._filter_window_fills_by_addr(window_fills, key)
        windows = auto_tune._candidate_windows(
            db, list(key), sigmas, fixed_follow, now_ms,
            window_fills=filtered, market_ctx=market_ctx,
            path_rows=None, path_meta=None,
        )
        metrics_ = _portfolio_selection_metrics(windows, selected_n=len(key))
        primary = windows.get(30) or windows.get(max(windows)) or {}
        recent = windows.get(7) or {}
        start_equity = f(
            primary.get("window_start_equity")
            or primary.get("initial_margin_equity")
            or base_follow.get("INITIAL_BALANCE")
            or config.INITIAL_BALANCE
        )
        recent_start_equity = f(
            recent.get("window_start_equity")
            or recent.get("initial_margin_equity")
        )
        primary_economics = replay_result_profitability(primary)
        recent_economics = replay_result_profitability(recent)
        return_30d = (
            f(primary_economics.get("qualificationPnl")) / start_equity
            if start_equity > 0.0 else float("-inf")
        )
        return_7d = (
            f(recent_economics.get("qualificationPnl")) / recent_start_equity
            if recent_start_equity > 0.0 else float("-inf")
        )
        replay_summary = {
            "pnlConcentration": primary.get("pnl_concentration") or {},
            "profitabilityBasis": PROFITABILITY_BASIS,
            "return30d": return_30d,
            "return7d": return_7d,
            "closedNetPnl30d": primary_economics.get("closedPnl"),
            "openProfitReference30d": primary_economics.get("openProfitReference"),
            "openLoss30d": primary_economics.get("openLoss"),
            "openLossRatio30d": primary_economics.get("openLossRatio"),
        }
        del windows
        value = core_formation.PrefixEvaluation(
            count=len(key), net_pnl=f(metrics_.net_pnl),
            stress_net_pnl=f(metrics_.net_pnl),
            max_drawdown=f(metrics_.max_drawdown),
            actionable_open_rate=f(metrics_.actionable_open_rate),
            capacity_fit=f(metrics_.capacity_fit),
            liquidations=int(metrics_.liquidations),
            params=tuned_params,
            payload={
                "initialBalance": start_equity,
                "recentStartEquity": recent_start_equity,
                "return30d": return_30d,
                "return7d": return_7d,
                "openLossRatio30d": primary_economics.get("openLossRatio"),
                "requireReturnFit": True,
            },
        )
        membership_eval_cache[key] = value
        # The optimizer may inspect dozens of sets. Retaining six complete replay results per set (positions,
        # open positions and equity curves) exhausted the 1GB production host. Robust validation needs only
        # these contribution/outlier summaries; all ranking metrics already live in ``value``.
        membership_replay_cache[key] = replay_summary
        return value

    def evaluate(count):
        return evaluate_members(ordered[:int(count)])

    prefix_search = core_formation.search_quality_prefix(
        len(ordered), evaluate, retention_kwargs=retention,
        tie_tolerance=float(config.CORE_PREFIX_TIE_TOLERANCE),
        exhaustive_below=int(getattr(config, "CORE_PREFIX_EXHAUSTIVE_MAX_N", 8) or 0),
        required_count=0,
    )
    # Core membership is a strict prefix of final-surface 70/30 profit order. An arbitrary add/swap search
    # would turn the deterministic ranking contract into an overfit subset search.
    chosen = prefix_search.selected
    chosen_addrs = tuple(ordered[:chosen.count])
    membership_algorithm = "strict_profit_prefix"

    robust_cache = {}

    def validate_members(addrs):
        key = tuple(sorted(dict.fromkeys(addrs)))
        if key in robust_cache:
            return robust_cache[key]
        value = evaluate_members(key)
        replay_summary = membership_replay_cache[key]
        check = core_formation.validate_final_membership(value)
        loo_marginals = {}
        for addr in key:
            without = tuple(item for item in key if item != addr)
            without_value = evaluate_members(without)
            loo_marginals[addr] = value.net_pnl - without_value.net_pnl
        nonpositive_loo = [
            addr for addr, marginal in loo_marginals.items() if marginal <= 0.0
        ]
        check.update({
            "addrs": list(key), "netPnl": value.net_pnl, "utility": value.utility,
            "return30d": replay_summary.get("return30d"),
            "return7d": replay_summary.get("return7d"),
            "looMarginalNetPnl": loo_marginals,
            "nonpositiveLoo": nonpositive_loo,
            "profitConcentration": replay_summary.get("pnlConcentration") or {},
        })
        robust_cache[key] = check
        return check

    finalist_limit = max(1, int(getattr(config, "CORE_SEARCH_ROBUST_FINALISTS", 12) or 12))
    prefix_keys = {
        tuple(sorted(ordered[:value.count])) for value in prefix_search.evaluated
    }
    finalist_pool = {
        key: value for key, value in membership_eval_cache.items()
        if key in prefix_keys and value.feasible
    }
    # Always include the count search's chosen profit prefix in the bounded robust finalist set.
    chosen_key = tuple(sorted(chosen_addrs))
    finalist_pool[chosen_key] = chosen
    finalist_states = sorted(
        finalist_pool.items(),
        key=lambda item: (
            item[1].utility, item[1].net_pnl, -len(item[0]), item[0],
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
    else:
        robust_key, chosen, robust_check = robust_winner
        robust_members = set(robust_key)
        chosen_addrs = tuple(addr for addr in ordered if addr in robust_members)
    # Pre-validate any strict LOO result. Publication may remove a negative incremental member only when
    # the resulting set has passed these same membership stress rules. Only the profit-ranked suffix is removable.
    robust_allowed = {tuple(sorted(chosen_addrs))}
    outgoing = next(
        reversed(chosen_addrs), None,
    )
    if outgoing is not None:
        smaller = tuple(addr for addr in chosen_addrs if addr != outgoing)
        if smaller:
            check = validate_members(smaller)
            robust_audit.append(check)
            if check.get("eligible"):
                robust_allowed.add(tuple(sorted(smaller)))
    evaluations = tuple({
        "count": value.count, "netPnl": value.net_pnl,
        "return30d": value.payload.get("return30d"),
        "return7d": value.payload.get("return7d"),
        "maxDrawdown": value.max_drawdown,
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
        "scoreDetails": effective_score_details,
        "profitPriorities": effective_profit_priorities,
        "policies": effective_policies, "walletMetrics": effective_metrics,
        "replayParamsHash": effective_surface_hash,
        "search": {
            "algorithm": "adaptive_count_continuous_equity_v7", "initialCount": len(ordered),
            "selectedCount": len(chosen_addrs), "boundary": prefix_search.boundary,
            "evaluatedCounts": [value.count for value in prefix_search.evaluated],
            "evaluations": evaluations,
            "membershipAlgorithm": membership_algorithm,
            "rankingMode": follow_score.PROFIT_PRIORITY_MODE,
            "rankingWeights": {
                "30d": follow_score.PROFIT_PRIORITY_30_WEIGHT,
                "7d": follow_score.PROFIT_PRIORITY_7_WEIGHT,
            },
            "membershipChanged": tuple(chosen_addrs) != tuple(current_core),
            "retuneApplied": bool(retune),
            "membershipEvaluated": len(membership_eval_cache),
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
            "tunePoolCount": len(tune_ordered),
            "tunedInputCount": winning_count,
            "coarseTuneRuns": len(tune_runs),
            "fullTuneRuns": 1 if chosen_run.get("search_profile") == "full" else 0,
            "tuneBoundary": tune_search.boundary if tune_search is not None else None,
            "tuneEvaluatedCounts": (
                [value.count for value in tune_search.evaluated]
                if tune_search is not None else []
            ),
            "tuneEvaluations": tune_evaluations,
            "effectiveRejected": qualification_rejected,
            "formationTuneEligible": tune_eligible,
            "formationTuneReason": tune_reason,
            "tuneCoverageFallback": tune_coverage_fallback,
            "formationTuneFinalists": list(chosen_run.get("finalists") or ()),
            "formationMarginRounds": list(chosen_run.get("margin_rounds") or ()),
            "formationFinalistAdmission": finalist_admission_audit,
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
    # Match the exact sector boundary consumed by ``_effective_follow_replay``. Candidate fill caches retain
    # every executable Crypto/stock contract for audit, including a wallet's currently disabled specialty.
    # Passing those disabled rows into the immutable generation validator can make one irrelevant market
    # (which was correctly never resolved into the generation snapshot) abort the whole bounded path batch.
    marks = ",".join("?" for _ in candidates)
    policy_by_addr = {}
    if marks:
        for addr, raw_policy in db.execute(
            f"SELECT lower(addr),sector_policy_json FROM profile WHERE lower(addr) IN ({marks})",
            tuple(candidates),
        ).fetchall():
            try:
                policy = json.loads(raw_policy or "{}")
            except (TypeError, ValueError):
                policy = {}
            allowed = set(policy.get("allowed") or ()) if isinstance(policy, dict) else set()
            watched = set(policy.get("watch") or ()) if isinstance(policy, dict) else set()
            policy_by_addr[(addr or "").lower()] = allowed or watched
    fills = [
        row for row in fills
        if classify_coin(row.get("coin"))
        in policy_by_addr.get((row.get("user") or "").lower(), set())
    ]
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
                                   effective_qualifications=None, effective_scores=None,
                                   effective_policies=None, effective_metrics=None,
                                   effective_score_details=None,
                                   effective_replay_params_hash=None,
                                   allow_loo=True):
    """Materialize the fill-searched membership after one final strict 30-day portfolio replay."""
    policy_values = {**params.load_follow(db), **params.load_category(db, "scanner")}
    copy_policy = load_copy_policy(policy_values)
    by_addr = {(row.get("addr") or "").lower(): row for row in profiles}
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
    for addr, policy_json in dict(effective_policies or {}).items():
        addr = (addr or "").lower()
        if addr in by_addr and policy_json:
            by_addr[addr]["sector_policy_json"] = policy_json
    replay_by_addr = {
        (addr or "").lower(): dict(metrics or {})
        for addr, metrics in dict(effective_metrics or {}).items()
        if addr and metrics
    }
    score_detail_by_addr = {
        (addr or "").lower(): dict(detail or {})
        for addr, detail in dict(effective_score_details or {}).items()
        if addr and detail
    }
    for row in profiles:
        addr = (row.get("addr") or "").lower()
        ranking_metrics = replay_by_addr.get(addr) or row
        priority, priority_detail = follow_score.compute_profit_priority(ranking_metrics)
        row["replay_profit_priority"] = priority
        score_detail_by_addr.setdefault(addr, {})["profitPriority"] = priority_detail
    profiles.sort(key=lambda row: follow_score.profit_priority_sort_key(
        replay_by_addr.get((row.get("addr") or "").lower()) or row,
        follow_score_value=f(row.get("follow_score")),
        addr=row.get("addr") or "",
    ))
    for rank, row in enumerate(profiles, 1):
        row["rank"] = rank
    desired = tuple(dict.fromkeys((addr or "").lower() for addr in desired_order if addr))
    # Challenger is a strict-output role, never a synonym for the wider pre-strict reserve. Only final
    # individual strict passes and path/data deferrals from this generation's frozen Top32 are visible.
    operational_candidate_set = set(desired)
    operational_candidate_set.update(
        (addr or "").lower() for addr in dict(effective_qualifications or {}) if addr
    )
    operational_candidate_set.update(
        (item.get("addr") or "").lower()
        for item in (formation_meta or {}).get("admission") or ()
        if isinstance(item, dict) and item.get("addr")
    )
    operational_candidate_set.update(
        (addr or "").lower()
        for (addr,) in db.execute(
            "SELECT addr FROM pre_strict_evidence WHERE generation=? "
            "AND queue_rank IS NOT NULL AND strict_status IN ('qualified','deferred')",
            (generation_id,),
        ).fetchall()
    )
    invalid = [
        addr for addr in desired
        if addr not in by_addr
        or by_addr[addr].get("profile_generation") != generation_id
        or by_addr[addr].get("status") not in {"active", "qualified"}
        or not _formation_core_permission(
            by_addr[addr].get("follow_qualification")
        )
        or (by_addr[addr].get("data_status") or "valid") != "valid"
        or not controls.get(addr, True)
    ]
    if invalid:
        raise RuntimeError(f"quality_prefix_contains_ineligible_wallets:{len(invalid)}")

    eval_cache = {}
    final_strict_validation = {
        "status": "empty", "selectedCount": 0,
        "dynamicReturn30d": 0.0, "dynamicReturn7d": 0.0,
    }
    if desired:
        window_fills = auto_tune._portfolio_window_fills(
            db, list(desired), int(now_ms), include_watch=True,
        )
        if window_fills is None or not any(window_fills.values()):
            raise RuntimeError("quality_prefix_replay_unavailable")
        follow = params.load_follow(db)
        if "SMART_ADD" in follow:
            follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
        sigmas = auto_tune._load_sigmas(db, generation_id)
        market_ctx = auto_tune._load_market_ctx(db, generation_id)

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
                    path_rows=None, path_meta=None,
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
        allow_loo=allow_loo,
    )
    final_addrs = tuple(transition["selected"])
    if final_addrs:
        final_fills_by_window = auto_tune._filter_window_fills_by_addr(
            window_fills, final_addrs,
        )
        final_fills = list(final_fills_by_window.get(30) or [])
        path_start = int(now_ms) - (
            30 + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
        ) * 86_400_000
        final_path = prepare_price_path(
            price_path.load_refined(db, final_fills, path_start, int(now_ms))
        )
        final_path_meta = price_path.coverage(
            db, final_fills, path_start, int(now_ms),
        )
        final_windows = auto_tune._candidate_windows(
            db, list(final_addrs), sigmas,
            {**follow, "AMBIGUOUS_PATH_MODE": "liquidate"}, int(now_ms),
            window_fills=final_fills_by_window, market_ctx=market_ctx,
            path_rows=final_path, path_meta=final_path_meta,
            initial_balance=float(config.INITIAL_BALANCE),
        )
        final_result = final_windows.get(30) or final_windows.get(max(final_windows)) or {}
        final_recent = final_windows.get(7) or {}
        final_metrics = _portfolio_selection_metrics(
            final_windows, selected_n=len(final_addrs),
        )
        initial_equity = f(
            final_result.get("window_start_equity")
            or final_result.get("initial_margin_equity")
            or follow.get("INITIAL_BALANCE")
            or config.INITIAL_BALANCE
        )
        recent_start_equity = f(
            final_recent.get("window_start_equity")
            or final_recent.get("initial_margin_equity")
        )
        final_economics = replay_result_profitability(final_result)
        final_recent_economics = replay_result_profitability(final_recent)
        return_30d = (
            f(final_economics.get("qualificationPnl")) / initial_equity
            if initial_equity > 0.0 else float("-inf")
        )
        return_7d = (
            f(final_recent_economics.get("qualificationPnl")) / recent_start_equity
            if recent_start_equity > 0.0 else float("-inf")
        )
        paper_start_equity = _paper_account_equity(db)
        if abs(paper_start_equity - float(config.INITIAL_BALANCE)) <= 1e-9:
            paper_windows = final_windows
        else:
            paper_windows = auto_tune._candidate_windows(
                db, list(final_addrs), sigmas,
                {**follow, "AMBIGUOUS_PATH_MODE": "liquidate"}, int(now_ms),
                window_fills=final_fills_by_window, market_ctx=market_ctx,
                path_rows=final_path, path_meta=final_path_meta,
                initial_balance=paper_start_equity,
            )
        paper_result = paper_windows.get(30) or paper_windows.get(max(paper_windows)) or {}
        paper_recent = paper_windows.get(7) or {}
        paper_metrics = _portfolio_selection_metrics(
            paper_windows, selected_n=len(final_addrs),
        )
        paper_start_30d = f(
            paper_result.get("window_start_equity")
            or paper_result.get("initial_margin_equity")
            or paper_start_equity
        )
        paper_start_7d = f(
            paper_recent.get("window_start_equity")
            or paper_recent.get("initial_margin_equity")
        )
        paper_economics = replay_result_profitability(paper_result)
        paper_recent_economics = replay_result_profitability(paper_recent)
        paper_return_30d = (
            f(paper_economics.get("qualificationPnl")) / paper_start_30d
            if paper_start_30d > 0.0 else float("-inf")
        )
        paper_return_7d = (
            f(paper_recent_economics.get("qualificationPnl")) / paper_start_7d
            if paper_start_7d > 0.0 else float("-inf")
        )
        failures = []
        if return_30d < float(config.CORE_PORTFOLIO_MIN_RETURN_30D):
            failures.append("dynamic_return_30d")
        if return_7d < float(config.CORE_PORTFOLIO_MIN_RETURN_7D):
            failures.append("dynamic_return_7d")
        if f(final_recent_economics.get("qualificationPnl")) <= 0.0:
            failures.append("recent_net_not_positive")
        if not open_loss_ratio_within_limit(final_economics):
            failures.append("open_loss_over_50pct")
        if f(final_metrics.actionable_open_rate) < load_copy_policy().min_actionable_open_rate:
            failures.append("open_follow_rate")
        if paper_return_30d < float(config.CORE_PORTFOLIO_MIN_RETURN_30D):
            failures.append("paper_dynamic_return_30d")
        if paper_return_7d < float(config.CORE_PORTFOLIO_MIN_RETURN_7D):
            failures.append("paper_dynamic_return_7d")
        if f(paper_recent_economics.get("qualificationPnl")) <= 0.0:
            failures.append("paper_recent_net_not_positive")
        if not open_loss_ratio_within_limit(paper_economics):
            failures.append("paper_open_loss_over_50pct")
        if f(paper_metrics.actionable_open_rate) < load_copy_policy().min_actionable_open_rate:
            failures.append("paper_open_follow_rate")
        if f(final_result.get("price_path_coverage")) < float(config.CORE_PRICE_PATH_MIN_COVERAGE):
            failures.append("path_coverage")
        if f(final_result.get("maintenance_margin_coverage")) < float(
            config.CORE_MAINTENANCE_META_MIN_COVERAGE
        ):
            failures.append("maintenance_coverage")
        final_strict_validation = {
            "status": "failed" if failures else "passed",
            "selectedCount": len(final_addrs),
            "profitabilityBasis": PROFITABILITY_BASIS,
            "netPnl30d": f(final_metrics.net_pnl),
            "markedNetPnl30d": f(final_result.get("copy_net_pnl")),
            "closedNetPnl30d": f(final_economics.get("closedPnl")),
            "openProfitReference30d": f(final_economics.get("openProfitReference")),
            "openLoss30d": f(final_economics.get("openLoss")),
            "openLossRatio30d": final_economics.get("openLossRatio"),
            "startEquity30d": initial_equity,
            "endEquity30d": f(final_result.get("window_end_equity")),
            "dynamicReturn30d": return_30d,
            "netPnl7d": f(final_recent_economics.get("qualificationPnl")),
            "markedNetPnl7d": f(final_recent.get("copy_net_pnl")),
            "closedNetPnl7d": f(final_recent_economics.get("closedPnl")),
            "openLoss7d": f(final_recent_economics.get("openLoss")),
            "startEquity7d": recent_start_equity,
            "endEquity7d": f(final_recent.get("window_end_equity")),
            "dynamicReturn7d": return_7d,
            "returnFloors": {
                "30d": float(config.CORE_PORTFOLIO_MIN_RETURN_30D),
                "7d": float(config.CORE_PORTFOLIO_MIN_RETURN_7D),
            },
            "standardizedAccount": {
                "initialEquity": float(config.INITIAL_BALANCE),
                "netPnl30d": f(final_economics.get("qualificationPnl")),
                "markedNetPnl30d": f(final_result.get("copy_net_pnl")),
                "closedNetPnl30d": f(final_economics.get("closedPnl")),
                "openProfitReference30d": f(final_economics.get("openProfitReference")),
                "openLoss30d": f(final_economics.get("openLoss")),
                "openLossRatio30d": final_economics.get("openLossRatio"),
                "startEquity30d": initial_equity,
                "endEquity30d": f(final_result.get("window_end_equity")),
                "dynamicReturn30d": return_30d,
                "netPnl7d": f(final_recent_economics.get("qualificationPnl")),
                "markedNetPnl7d": f(final_recent.get("copy_net_pnl")),
                "closedNetPnl7d": f(final_recent_economics.get("closedPnl")),
                "openLoss7d": f(final_recent_economics.get("openLoss")),
                "startEquity7d": recent_start_equity,
                "endEquity7d": f(final_recent.get("window_end_equity")),
                "dynamicReturn7d": return_7d,
            },
            "paperAccount": {
                "initialEquity": paper_start_equity,
                "netPnl30d": f(paper_economics.get("qualificationPnl")),
                "markedNetPnl30d": f(paper_result.get("copy_net_pnl")),
                "closedNetPnl30d": f(paper_economics.get("closedPnl")),
                "openProfitReference30d": f(paper_economics.get("openProfitReference")),
                "openLoss30d": f(paper_economics.get("openLoss")),
                "openLossRatio30d": paper_economics.get("openLossRatio"),
                "startEquity30d": paper_start_30d,
                "endEquity30d": f(paper_result.get("window_end_equity")),
                "dynamicReturn30d": paper_return_30d,
                "netPnl7d": f(paper_recent_economics.get("qualificationPnl")),
                "markedNetPnl7d": f(paper_recent.get("copy_net_pnl")),
                "closedNetPnl7d": f(paper_recent_economics.get("closedPnl")),
                "openLoss7d": f(paper_recent_economics.get("openLoss")),
                "startEquity7d": paper_start_7d,
                "endEquity7d": f(paper_recent.get("window_end_equity")),
                "dynamicReturn7d": paper_return_7d,
            },
            "maxDrawdown30d": f(final_metrics.max_drawdown),
            "liquidations30d": int(final_metrics.liquidations),
            "actionableOpenRate30d": f(final_metrics.actionable_open_rate),
            "paperActionableOpenRate30d": f(paper_metrics.actionable_open_rate),
            "capacityFit30d": f(final_metrics.capacity_fit),
            "pricePathCoverage30d": f(final_result.get("price_path_coverage")),
            "maintenanceMarginCoverage30d": f(
                final_result.get("maintenance_margin_coverage")
            ),
            "failures": failures,
        }
        if paper_windows is not final_windows:
            del paper_windows
        del final_windows
        if failures:
            raise RuntimeError(
                "final_strict_copy_failed:" + ",".join(failures)
            )
        transition["metrics"] = final_metrics
    selected_enabled_set = set(transition["selected"])
    core_order = tuple(transition["selected"])
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
            "finalStrictCopy": final_strict_validation,
            "membershipPolicy": pre_strict.SELECTION_MODEL_VERSION,
            "desiredOrder": desired,
            "scoreOrder": transition["selected"],
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
        candidate_ok = refreshed and active and bool(qualification.get("eligible"))
        include = True
        research_only = False
        if addr in selected_set and enabled:
            role = selection.CORE
            reason = transition_reasons.get(addr, "core_quality_selected")
        elif explicit_empty_core and addr in previous_core and candidate_ok:
            # Empty Core is an execution decision, not evidence deletion. A still-profitable former Core
            # opens nothing as Challenger but remains visible and receives the next retention replay.
            role = selection.CHALLENGER
            reason = qualification.get("status") or "no_robust_core_latest_evidence"
        elif addr in held and data_status != "valid":
            role, reason = selection.EXIT_ONLY, transition_reasons.get(addr, "exit_only_open_position")
        elif data_status != "valid":
            role = selection.QUARANTINE
            reason = "deferred_data_error" if data_status == "deferred_data_error" else "copy_data_error"
            include = False
        elif candidate_ok and addr in operational_candidate_set:
            role = selection.CHALLENGER
            if not enabled:
                reason = "operator_disabled"
            elif _formation_core_permission(qualification):
                reason = transition_reasons.get(addr, "portfolio_not_selected")
            else:
                reason = qualification.get("status") or "sample_observation"
            if addr in held:
                reason = f"{reason}:exit_pending"
        elif addr in held:
            role, reason = selection.EXIT_ONLY, transition_reasons.get(addr, "exit_only_open_position")
        elif candidate_ok:
            role, reason = selection.REJECTED, "research_pool_not_bounded"
            include = False
            research_only = True
        else:
            role = selection.REJECTED
            reason = qualification.get("status") or row.get("reason") or "not_qualified"
            include = False
        if include:
            replay = replay_by_addr.get(addr) or {}
            rows.append(selection.SelectionRow(
                addr=addr, role=role, enabled=enabled, reason=reason,
                utility=transition.get("utilities", {}).get(addr, f(row.get("follow_score"))),
                follow_score=f(row.get("follow_score")),
                replay_profit_priority=row.get("replay_profit_priority"),
                selection_rank=core_rank.get(addr) if role == selection.CORE else rank,
                data_status=selection_data_status,
                evidence_status=row.get("evidence_status") or "",
                model_version=pre_strict.SELECTION_MODEL_VERSION,
                policy_version=pre_strict.POLICY_VERSION,
                acct_value=row.get("acct_value"),
                sector_policy_json=row.get("sector_policy_json"),
                replay_copy_bt_net_pnl=replay.get("copy_bt_net_pnl"),
                replay_copy_bt_closed_net_pnl=replay.get("copy_bt_closed_net_pnl"),
                replay_copy_bt_window_start_equity=replay.get(
                    "copy_bt_window_start_equity"
                ),
                replay_copy_bt_win_rate=replay.get("copy_bt_win_rate"),
                replay_copy_bt_closed_n=replay.get("copy_bt_closed_n"),
                replay_copy_bt_open_fill_rate=replay.get(
                    "copy_bt_open_fill_rate", replay.get("actionable_open_rate")
                ),
                replay_copy_bt_raw_target_open_n=replay.get("copy_bt_raw_target_open_n"),
                replay_copy_bt_small_open_excluded_n=replay.get(
                    "copy_bt_small_open_excluded_n"
                ),
                replay_copy_bt_effective_target_open_n=replay.get(
                    "copy_bt_effective_target_open_n"
                ),
                replay_copy_bt_opened_n=replay.get("copy_bt_opened_n"),
                replay_copy_bt_raw_open_capture_rate=replay.get(
                    "copy_bt_raw_open_capture_rate"
                ),
                replay_copy_bt_open_audit_json=replay.get("copy_bt_open_audit_json"),
                replay_copy_bt_liquidations=replay.get("copy_bt_liquidations"),
                replay_copy_bt_fee_drag=replay.get("copy_bt_fee_drag"),
                replay_copy_bt_unrealized_pnl=replay.get("copy_bt_unrealized_pnl"),
                replay_copy_bt_valuation_status=replay.get("copy_bt_valuation_status"),
                replay_copy_bt_14d_net_pnl=replay.get("copy_bt_14d_net_pnl"),
                replay_copy_bt_14d_closed_net_pnl=replay.get(
                    "copy_bt_14d_closed_net_pnl"
                ),
                replay_copy_bt_14d_unrealized_pnl=replay.get(
                    "copy_bt_14d_unrealized_pnl"
                ),
                replay_copy_bt_14d_closed_n=replay.get("copy_bt_14d_closed_n"),
                replay_copy_bt_7d_net_pnl=replay.get("copy_bt_7d_net_pnl"),
                replay_copy_bt_7d_closed_net_pnl=replay.get(
                    "copy_bt_7d_closed_net_pnl"
                ),
                replay_copy_bt_7d_window_start_equity=replay.get(
                    "copy_bt_7d_window_start_equity"
                ),
                replay_copy_bt_7d_unrealized_pnl=replay.get(
                    "copy_bt_7d_unrealized_pnl"
                ),
                replay_copy_bt_7d_closed_n=replay.get("copy_bt_7d_closed_n"),
                replay_sector_copy_json=replay.get("sector_copy_json"),
                replay_params_hash=(
                    effective_replay_params_hash if replay else None
                ),
                replay_score_detail_json=(
                    json.dumps(
                        score_detail_by_addr[addr],
                        sort_keys=True, separators=(",", ":"), default=str,
                    )
                    if addr in score_detail_by_addr else None
                ),
                replayed_at=stamp if replay else None,
            ))
        lifecycle_state = (
            "qualified" if research_only
            else role if role in {selection.CORE, selection.CHALLENGER, selection.EXIT_ONLY}
            else "quarantine" if role == selection.QUARANTINE else "rejected"
        )
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
                              effective_qualifications=None, effective_scores=None,
                              effective_policies=None, effective_metrics=None,
                              effective_score_details=None,
                              effective_replay_params_hash=None,
                              allow_loo=True):
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
        "p.official_perp_status,p.official_perp_reason,p.official_perp_evidence_json,"
        "p.official_perp_return_30d,p.official_perp_pnl_30d,p.official_perp_pnl_share,"
        "p.source_episode_n_30d,p.source_episode_n_7d,p.source_win_rate_30d,p.source_win_rate_7d,"
        "p.source_net_pnl_30d,p.source_net_pnl_7d,p.source_active_days_30d,p.source_active_days_7d,"
        "p.open_unrealized,"
        "p.source_top3_profit_share,p.source_body_after_top3_n,p.source_body_after_top3_win_rate,"
        "p.source_body_after_top3_net_pnl,p.source_quality_score,p.rough_copy_score,"
        "p.copy_bt_closed_n,p.copy_bt_14d_closed_n,p.copy_bt_7d_closed_n,"
        "p.copy_evidence_days,p.execution_score,p.open_probability_48h,"
        "p.actionable_open_rate,p.capacity_fit,p.copy_bt_net_pnl,p.copy_bt_win_rate,"
        "p.copy_bt_unrealized_pnl,p.copy_bt_valuation_status,"
        "p.copy_bt_initial_margin_equity,p.copy_bt_window_start_equity,"
        "p.copy_bt_14d_net_pnl,p.copy_bt_14d_unrealized_pnl,p.copy_bt_14d_window_start_equity,"
        "p.copy_bt_7d_net_pnl,p.copy_bt_7d_unrealized_pnl,p.copy_bt_7d_window_start_equity,"
        "p.copy_bt_open_fill_rate,p.copy_bt_liquidations,p.copy_bt_fee_drag,p.sector_copy_json,p.sector_policy_json,p.acct_value "
        "FROM profile p"
    )
    names = [desc[0] for desc in cur.description]
    profiles = [dict(zip(names, row)) for row in cur.fetchall()]
    pre_strict_rows = {
        (addr or "").lower(): {
            "status": status,
            "firstFailure": first_failure,
            "strictStatus": strict_status,
            "strictFirstFailure": strict_failure,
            "activityJson": activity_json,
            "tier": tier,
            "queueRank": queue_rank,
            "policyVersion": policy_version,
        }
        for (
            addr, status, first_failure, strict_status, strict_failure,
            activity_json, tier, queue_rank, policy_version,
        ) in db.execute(
            "SELECT addr,status,first_failure,strict_status,strict_first_failure,"
            "activity_json,tier,queue_rank,policy_version "
            "FROM pre_strict_evidence WHERE generation=?",
            (generation_id,),
        ).fetchall()
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
    # watchlist.score is the published final Copy-follow score.  Selection must consume that exact value
    # rather than recomputing from a narrower row projection and creating an invisible second score line.
    watch_scores = {
        (addr or "").lower(): score
        for addr, score in db.execute("SELECT addr,score FROM watchlist").fetchall()
    }
    margin_equity_pct = params.load_follow(db).get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
    for row in profiles:
        addr = (row.get("addr") or "").lower()
        row.update(forward_risk.get(addr) or {})
        frozen = pre_strict_rows.get(addr) or {}
        row["pre_strict_activity_json"] = frozen.get("activityJson")
        row["pre_strict_policy_version"] = frozen.get("policyVersion")
        row["pre_strict_tier"] = frozen.get("tier")
        row["pre_strict_queue_rank"] = frozen.get("queueRank")
        row["follow_score"] = (
            f(watch_scores[addr]) if addr in watch_scores
            else follow_score.compute_follow_score(row)[0]
        )
        row["follow_qualification"] = follow_score.evaluate_follow_eligibility({
            **row,
            "copy_bt_data_status": row.get("data_status"),
            "copy_bt_evidence_status": row.get("evidence_status"),
        }, margin_equity_pct=margin_equity_pct, policy_values=policy_values, as_of_ms=now_ms,
            follow_score_value=row["follow_score"])
        if frozen.get("strictStatus") == "deferred" and frozen.get("queueRank") is not None:
            row["follow_qualification"] = {
                "eligible": True,
                "coreEligible": False,
                "stageEligible": False,
                "stage": "strict",
                "status": frozen.get("strictFirstFailure") or "strict_evidence_deferred",
                "firstFailure": frozen.get("strictFirstFailure") or "strict_evidence_deferred",
                "role": "challenger",
                "deferred": True,
                "checks": {"frozenPreStrictDeferred": True},
                "reasons": [frozen.get("strictFirstFailure") or "strict_evidence_deferred"],
            }
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
            effective_policies=effective_policies,
            effective_metrics=effective_metrics,
            effective_score_details=effective_score_details,
            effective_replay_params_hash=effective_replay_params_hash,
            allow_loo=allow_loo,
        )
    raise RuntimeError("explicit selection builder requires forced score-prefix formation")


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


def _store_final_copy_summary(db, generation_id: str, marginal) -> tuple[dict, dict]:
    """Reuse publication certification; never replay Core or Challenger after publication."""
    if marginal is None:
        return (
            {"status": "skipped", "reason": "no_automatic_final_certification"},
            {"status": "skipped", "reason": "portfolio_strict_only", "refreshed": 0},
        )
    strict = ((marginal.search_meta or {}).get("finalStrictCopy") if marginal else None)
    portfolio = auto_tune.store_certified_portfolio_replay(db, generation_id, strict)
    per_wallet = {
        "status": "skipped", "reason": "portfolio_strict_only", "refreshed": 0,
    }
    return portfolio, per_wallet


def repair_published_selection(db, generation_id=None, stamp=None, *, replace_existing=False,
                               retune_formation=False, force_entry_requalification=False):
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
    stale_active = int(db.execute(
        "SELECT COUNT(*) FROM profile WHERE status='active' AND COALESCE(profile_generation,'')<>?",
        (generation_id,),
    ).fetchone()[0] or 0)
    if stale_active:
        stale_protected = int(db.execute(
            "SELECT COUNT(*) FROM profile p WHERE p.status='active' "
            "AND COALESCE(p.profile_generation,'')<>? AND ("
            "EXISTS (SELECT 1 FROM follow_selection fs WHERE fs.generation=? "
            "AND lower(fs.addr)=lower(p.addr) AND fs.role IN ('core','challenger')) OR "
            "EXISTS (SELECT 1 FROM copy_position cp WHERE lower(cp.addr)=lower(p.addr) "
            "AND cp.status='open'))",
            (generation_id, generation_id),
        ).fetchone()[0] or 0)
        if stale_protected:
            raise RuntimeError("selection_repair_has_protected_stale_active_profiles")
        # A complete current generation is authoritative.  Old unselected/non-owning active rows otherwise
        # pollute the derived watchlist and can block a safe cached selection repair forever.
        db.execute(
            "UPDATE profile SET status='retired',reason='stale_generation_not_profiled' "
            "WHERE status='active' AND COALESCE(profile_generation,'')<>? "
            "AND NOT EXISTS (SELECT 1 FROM copy_position cp WHERE lower(cp.addr)=lower(profile.addr) "
            "AND cp.status='open')",
            (generation_id,),
        )
        db.commit()

    stamp = stamp or now_iso()
    repair_now_ms = int(time.time() * 1000)
    db.commit()
    refresh_watchlist(
        db,
        stamp,
        leaderboard_generation=generation_id,
        commit=False,
    )
    prefetch_candidates = _selection_prefetch_candidates(
        db, generation_id, repair_now_ms,
    )
    db.rollback()
    _prefetch_selection_paths(db, prefetch_candidates, repair_now_ms, generation_id)
    formation = form_quality_prefix(
        db, generation_id, stamp, repair_now_ms, retune=retune_formation,
        force_entry_requalification=force_entry_requalification,
        force_retune=retune_formation,
    )
    membership_retune_triggered = (
        not bool(retune_formation)
        and _formation_membership_changed(formation, existing_core)
    )
    if membership_retune_triggered:
        formation = form_quality_prefix(
            db, generation_id, stamp, repair_now_ms, retune=True,
            force_entry_requalification=force_entry_requalification,
            force_retune=True,
        )
    _assert_automatic_formation_tuned(
        formation, required=bool(retune_formation or membership_retune_triggered),
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
        effective_policies=formation.get("policies") or {},
        effective_metrics=formation.get("walletMetrics") or {},
        effective_score_details=formation.get("scoreDetails") or {},
        effective_replay_params_hash=formation.get("replayParamsHash"),
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
        validation={
            **(
                (marginal.search_meta or {}) if marginal
                else (formation.get("search") or {})
            ),
            "marketSnapshot": market_validation,
        },
        stamp=stamp,
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
                "profitPriority": row.replay_profit_priority,
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
    portfolio_replay, selection_replay = _store_final_copy_summary(
        db, generation_id, marginal,
    )
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
        retune_formation=True, force_entry_requalification=True,
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
                full=0, failed=0, complete=True, kind="complete", generation_id=None,
                reason=None, api_stats=None, commit=True):
    api_stats = dict(api_stats or {})
    db.execute(
        "INSERT INTO scan_runs (started_at,finished_at,duration_s,candidates,profiled,probed_new,added,"
        "retired,kept,rejected,n_active,full,failed,complete,kind,generation,api_requests,api_weight,"
        "outcome_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (started, now_iso(), round(time.time() - t0, 1), candidates, profiled, profiled, added, retired,
         kept, rejected, n_active, 1 if full else 0, failed, 1 if complete else 0,
         str(kind or "complete"), generation_id, int(api_stats.get("requests") or 0),
         int(api_stats.get("estimated_weight") or 0), str(reason)[:300] if reason else None))
    if commit:
        db.commit()


def record_challenger_refresh_skip(db, reason="skipped_scan_busy"):
    """Record a scheduled refresh which intentionally did no work."""
    started = now_iso()
    _record_run(
        db, started, time.time(), 0, 0, 0, 0, 0, 0,
        len(selection.published_core_addrs(db) or ()),
        full=False, failed=0, complete=True, kind="challenger_refresh",
        reason=reason,
    )
    pipeline_audit._insert_event(
        db, stamp=started, source="challenger_daily", stage="selection_summary",
        status="skipped", reason=str(reason or "skipped"),
        payload={"retainedGeneration": selection.latest_published_generation(db)},
    )
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
        "roi_total,open_unrealized,open_loss_frac,open_win_frac,bag_count,max_bag_days,liq_count,hedge_ratio,net_30d,net_life,reason,"
        "p.official_perp_status,p.official_perp_reason,p.official_perp_return_30d,"
        "p.official_perp_pnl_30d,p.official_perp_pnl_share,"
        "p.source_episode_n_30d,p.source_episode_n_7d,p.source_win_rate_30d,p.source_win_rate_7d,"
        "p.source_net_pnl_30d,p.source_net_pnl_7d,p.source_active_days_30d,p.source_active_days_7d,"
        "p.source_top3_profit_share,p.source_body_after_top3_n,p.source_body_after_top3_win_rate,"
        "p.source_body_after_top3_net_pnl,p.last_copyable_open_ms,"
        "l.week_roi,l.mon_roi,l.all_roi,"                      # HL return-on-capital windows for the ROI pillar
        "p.pf_turnover,p.pf_mon_pnl,p.pf_mon_vlm,p.pf_week_pnl,p.pf_equity,"   # account-wide audit metrics
        "p.payoff_ratio,p.pf_week_vlm,"
        "p.copy_bt_net_pnl,p.copy_bt_win_rate,p.copy_bt_closed_n,p.copy_bt_open_fill_rate,"
        "p.copy_bt_liquidations,p.copy_bt_fee_drag,p.copy_bt_unrealized_pnl,p.copy_bt_valuation_status,"
        "p.copy_bt_initial_margin_equity,p.copy_bt_window_start_equity,"
        "p.copy_bt_14d_net_pnl,p.copy_bt_14d_unrealized_pnl,p.copy_bt_14d_closed_n,p.copy_bt_14d_window_start_equity,"
        "p.copy_bt_7d_net_pnl,p.copy_bt_7d_unrealized_pnl,p.copy_bt_7d_closed_n,p.copy_bt_7d_window_start_equity,"
        "p.sector_copy_json,p.sector_policy_json,"
        "p.open_position_count,p.material_open_count "
        "FROM profile p LEFT JOIN leaderboard l ON p.addr=l.addr" + row_scope,
        row_args,
    ).fetchall()
    repaired_eps = repair_missing_episode_rows(db, [r[0] for r in rows])
    if repaired_eps:
        print(f"regate: repaired {repaired_eps} missing episode caches from candidate_fills")
    # Distinct OIDs, not exchange fill fragments, own the execution-structure gate. Missing/legacy episode rows
    # are rebuilt above from cached fills before this pass.
    # Load episode-derived regate inputs in one pass. Previously loss_pain issued one extra SELECT per
    # profile after the three table sweeps below, making a no-network regate progressively query-bound.
    _epw, _epo, _iv, _wpt, _pnl = {}, {}, {}, {}, {}
    for a, nf, noid, om, cm, npnl, mnotl in db.execute(
            "SELECT addr,n_fills,n_oids,open_ms,close_ms,net_pnl,max_notl FROM episode"):
        if nf is not None:
            _epw.setdefault(a, []).append(nf)
        if noid is not None:
            _epo.setdefault(a, []).append(noid)
        if om is not None and cm is not None:
            _iv.setdefault(a, []).append((om, cm))
        if npnl is not None:
            _pnl.setdefault(a, []).append(npnl)
            if npnl > 0 and mnotl is not None and mnotl > 0:
                _wpt.setdefault(a, []).append(npnl / mnotl * 100)
    p90fe = {a: sorted(xs)[min(len(xs) - 1, int(len(xs) * 0.9))] for a, xs in _epw.items() if xs}
    p90oe = {a: sorted(xs)[min(len(xs) - 1, int(len(xs) * 0.9))] for a, xs in _epo.items() if xs}
    heavyoe = {a: sum(value > 50 for value in xs) for a, xs in _epo.items()}
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
         roi_tot, open_unreal, oloss, owin, bagn, bagd, liqc, hedge, net30, netlife, old_reason,
         official_status, official_reason, official_return, official_pnl, official_share,
         source_n30, source_n7, source_win30, source_win7,
         source_net30, source_net7, source_days30, source_days7,
         source_top3, source_body_n, source_body_win, source_body_net, last_copyable_open,
         wkroi, moroi, alroi, pf_turn, pf_mpnl, pf_mvlm, pf_wpnl, pf_eq, pay, pf_wvlm,
         copy_net, copy_wr, copy_closed, copy_open_fill_rate, copy_liqs, copy_fee,
         copy_unreal, copy_valuation, copy_initial_equity, copy30_start_equity,
         copy14_net, copy14_unreal, copy14_closed, copy14_start_equity,
         copy7_net, copy7_unreal, copy7_closed, copy7_start_equity,
         sector_copy_json, sector_policy_json,
         open_position_count, material_open_count) = r
        m = {"n_trades": n_tr or 0, "n_fills": n_fills or 0, "perp_frac": perp_frac or 0.0, "last_fill_ms": last_fill or 0,
             "net_pnl": net or 0.0, "roi_equity": roi_eq or 0.0, "max_drawdown": mdd or 0.0,
             "acct_value": acct or 0.0, "age_days": age, "times_active": ta or 0,
             "liq_worst_pct": liqw or 0.0, "active_days": ad or 0, "activity_ratio": ar or 0.0,
             "median_eps": meps or 0.0, "avg_notional": avgnotl or 0.0, "pos_day_ratio": pdr or 0.0, "profit_conc": conc or 0.0,
             "hold_skew": skew or 0.0, "open_underwater": uw or 0.0, "median_hold_s": mhold,
             "win_rate": wr or 0.0, "max_adds_per_ep": mxadds or 0, "median_adds_per_ep": mdadds or 0,
             "p90_fills_ep": p90fe.get(addr, 0),   # raw fragmentation remains audit-only
             "p90_orders_ep": p90oe.get(addr, 0),
             "heavy_orders_episode_n": heavyoe.get(addr, 0),
             "max_concurrent": concw.get(addr, 0), # peak simultaneous positions → too_many_concurrent gate
             "win_pt": winptw.get(addr, 0.0),       # median winning per-trade % → audit metric
             "worst_loss_pct": wloss or 0.0,
             # v4 open-position character (stored from the last scan; regate doesn't re-fetch live state)
             "roi_total": roi_tot if roi_tot is not None else (roi_eq or 0.0),
             "open_unrealized": open_unreal or 0.0,
             "open_loss_frac": oloss or 0.0, "open_win_frac": owin or 0.0,
             "bag_count": bagn or 0, "max_bag_days": bagd or 0.0, "liq_count": liqc or 0,
             "hedge_ratio": hedge or 0.0,
             # v6 nets: None when scanned before this datum existed → net gates skip (safe pre-rescan)
             "net_30d": net30, "net_life": netlife,
             "official_perp_status": official_status,
             "official_perp_reason": official_reason,
             "official_perp_return_30d": official_return,
             "official_perp_pnl_30d": official_pnl,
             "official_perp_pnl_share": official_share,
             "source_episode_n_30d": source_n30,
             "source_episode_n_7d": source_n7,
             "source_win_rate_30d": source_win30,
             "source_win_rate_7d": source_win7,
             "source_net_pnl_30d": source_net30,
             "source_net_pnl_7d": source_net7,
             "source_active_days_30d": source_days30,
             "source_active_days_7d": source_days7,
             "source_top3_profit_share": source_top3,
             "source_body_after_top3_n": source_body_n,
             "source_body_after_top3_win_rate": source_body_win,
             "source_body_after_top3_net_pnl": source_body_net,
             "last_copyable_open_ms": last_copyable_open,
             # HL return-on-capital windows (from leaderboard join) → score() ROI pillar. None → weight-renormalized.
             "week_roi": wkroi, "mon_roi": moroi, "all_roi": alroi,
             # Account-wide portfolio metrics are audit context only; executable-scope strict Copy owns admission.
             "pf_turnover": pf_turn, "pf_mon_pnl": pf_mpnl, "pf_mon_vlm": pf_mvlm,
             "pf_week_pnl": pf_wpnl, "pf_equity": pf_eq,
             # Payoff remains a smooth score factor. Weekly volume is retained for audit/backward compatibility.
             "payoff_ratio": pay, "pf_week_vlm": pf_wvlm,
             "copy_bt_net_pnl": copy_net, "copy_bt_win_rate": copy_wr,
             "copy_bt_closed_n": copy_closed, "copy_bt_open_fill_rate": copy_open_fill_rate,
             "copy_bt_liquidations": copy_liqs, "copy_bt_fee_drag": copy_fee,
             "copy_bt_unrealized_pnl": copy_unreal, "copy_bt_valuation_status": copy_valuation,
             "copy_bt_initial_margin_equity": copy_initial_equity,
             "copy_bt_window_start_equity": copy30_start_equity,
             "copy_bt_14d_net_pnl": copy14_net, "copy_bt_14d_unrealized_pnl": copy14_unreal,
             "copy_bt_14d_closed_n": copy14_closed,
             "copy_bt_14d_window_start_equity": copy14_start_equity,
             "copy_bt_7d_net_pnl": copy7_net, "copy_bt_7d_unrealized_pnl": copy7_unreal,
             "copy_bt_7d_closed_n": copy7_closed,
             "copy_bt_7d_window_start_equity": copy7_start_equity,
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
            "copy_bt_net_pnl=?,copy_bt_closed_net_pnl=?,copy_bt_win_rate=?,copy_bt_closed_n=?,copy_bt_open_fill_rate=?,"
            "copy_bt_raw_target_open_n=?,copy_bt_small_open_excluded_n=?,"
            "copy_bt_effective_target_open_n=?,copy_bt_opened_n=?,"
            "copy_bt_raw_open_capture_rate=?,copy_bt_open_audit_json=?,"
            "copy_bt_liquidations=?,copy_bt_fee_drag=?,copy_bt_unrealized_pnl=?,copy_bt_valuation_status=?,"
            "copy_bt_initial_margin_equity=?,copy_bt_window_start_equity=?,"
            "copy_bt_14d_net_pnl=?,copy_bt_14d_closed_net_pnl=?,copy_bt_14d_unrealized_pnl=?,copy_bt_14d_closed_n=?,copy_bt_14d_window_start_equity=?,"
            "copy_bt_7d_net_pnl=?,copy_bt_7d_closed_net_pnl=?,copy_bt_7d_unrealized_pnl=?,copy_bt_7d_closed_n=?,copy_bt_7d_window_start_equity=?,"
            "sector_copy_json=?,sector_policy_json=?,"
            "copy_evidence_days=?,execution_score=?,model_coverage=?,oos_net_pnl=?,oos_max_drawdown=?,oos_cvar95=?,"
            "actionable_open_rate=?,capacity_fit=?,"
            "copy_path_risk_status=?,copy_intratrade_max_drawdown=?,copy_max_underwater_hours=?,"
            "copy_loss_over_5_time_ratio=?,copy_deep_bag_event_n=?,copy_failed_deep_bag_n=?,"
            "copy_deep_bag_recovery_rate=?,copy_max_deep_bag_hours=?,copy_current_open_loss_frac=?,"
            "copy_current_bag_hours=?,data_status=?,evidence_status=? WHERE addr=?",
            (status, reason, score, m.get("raw_quality_score"), m["loss_pain"], concw.get(addr, 0), winptw.get(addr, 0.0),
             m.get("copy_bt_net_pnl"), m.get("copy_bt_closed_net_pnl"),
             m.get("copy_bt_win_rate"), m.get("copy_bt_closed_n"),
             m.get("copy_bt_open_fill_rate"), m.get("copy_bt_raw_target_open_n"),
             m.get("copy_bt_small_open_excluded_n"), m.get("copy_bt_effective_target_open_n"),
             m.get("copy_bt_opened_n"), m.get("copy_bt_raw_open_capture_rate"),
             m.get("copy_bt_open_audit_json"), m.get("copy_bt_liquidations"), m.get("copy_bt_fee_drag"),
             m.get("copy_bt_unrealized_pnl"), m.get("copy_bt_valuation_status"),
             m.get("copy_bt_initial_margin_equity"), m.get("copy_bt_window_start_equity"),
             m.get("copy_bt_14d_net_pnl"), m.get("copy_bt_14d_closed_net_pnl"),
             m.get("copy_bt_14d_unrealized_pnl"),
             m.get("copy_bt_14d_closed_n"), m.get("copy_bt_14d_window_start_equity"),
             m.get("copy_bt_7d_net_pnl"), m.get("copy_bt_7d_closed_net_pnl"),
             m.get("copy_bt_7d_unrealized_pnl"),
             m.get("copy_bt_7d_closed_n"), m.get("copy_bt_7d_window_start_equity"),
             m.get("sector_copy_json"), m.get("sector_policy_json"),
             m.get("copy_evidence_days"), m.get("execution_score"), m.get("model_coverage"),
             m.get("oos_net_pnl"), m.get("oos_max_drawdown"), m.get("oos_cvar95"),
             m.get("actionable_open_rate"), m.get("capacity_fit"),
             m.get("copy_path_risk_status"), m.get("copy_intratrade_max_drawdown"),
             m.get("copy_max_underwater_hours"), m.get("copy_loss_over_5_time_ratio"),
             m.get("copy_deep_bag_event_n"), m.get("copy_failed_deep_bag_n"),
             m.get("copy_deep_bag_recovery_rate"), m.get("copy_max_deep_bag_hours"),
             m.get("copy_current_open_loss_frac"), m.get("copy_current_bag_hours"),
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


def finalize_profiled_generation(db, generation_id=None, stamp=None, *, retune=True) -> dict:
    """Finish selection from an already-profiled generation without fetching wallet history.

    ``retune=False`` is an operational escape hatch for a generation whose bounded parameter search is too
    expensive on the production host. It preserves every strict individual/path/portfolio membership gate
    and seals the currently active parameter surface; only the multi-node parameter search is skipped.
    """
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
        db, generation_id, now_ms,
        limit=int(config.PRE_STRICT_QUEUE_MAX_N),
    )
    db.rollback()
    if preview:
        _set_scan_progress(db, stage="prefetch_selection_paths")
        _prefetch_selection_paths(db, preview, now_ms, generation_id)
    formation = form_quality_prefix(
        db, generation_id, stamp, now_ms,
        retune=bool(retune), force_retune=bool(retune),
    )
    membership_retune_triggered = (
        not bool(retune)
        and _formation_membership_changed(formation, previous_core)
    )
    if membership_retune_triggered:
        formation = form_quality_prefix(
            db, generation_id, stamp, now_ms,
            retune=True, force_retune=True,
        )
    _assert_automatic_formation_tuned(
        formation, required=bool(retune or membership_retune_triggered),
    )
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
            effective_policies=formation.get("policies") or {},
            effective_metrics=formation.get("walletMetrics") or {},
            effective_score_details=formation.get("scoreDetails") or {},
            effective_replay_params_hash=formation.get("replayParamsHash"),
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
                **(marginal.search_meta or {}), "marketSnapshot": market_validation,
            }, stamp=publication_stamp,
        )
        for item in rows:
            pipeline_audit._insert_event(
                db, stamp=stamp, source="resume_finalize", stage="selection",
                addr=item.addr, status=item.role, reason=item.reason,
                follow_score=item.follow_score,
                payload={
                    "generation": generation_id, "selectionRank": item.selection_rank,
                    "profitPriority": item.replay_profit_priority,
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

    portfolio_replay, selection_replay = _store_final_copy_summary(
        db, generation_id, marginal,
    )
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


# ---------------------------------------------------------------- Challenger daily refresh
def _latest_complete_scan_generation(db):
    row = db.execute(
        "SELECT generation FROM scan_generation "
        "WHERE source='scan' AND status='published' AND complete=1 "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def challenger_refresh_pool(db, base_generation=None):
    """Freeze the daily workset to the latest complete discovery generation."""
    base_generation = base_generation or _latest_complete_scan_generation(db)
    if not base_generation:
        return None, []
    frozen = {
        str(addr or "").lower()
        for (addr,) in db.execute(
            "SELECT addr FROM follow_selection WHERE generation=? "
            "AND role IN ('core','challenger')",
            (base_generation,),
        ).fetchall()
        if addr
    }
    current = {
        str(addr or "").lower()
        for addr in (selection.published_core_addrs(db) or ())
        if addr
    }
    held = {
        str(addr or "").lower()
        for (addr,) in db.execute(
            "SELECT DISTINCT addr FROM copy_position WHERE status='open'"
        ).fetchall()
        if addr
    }
    return base_generation, sorted(frozen | current | held)


def _verified_zero_equity_source_liquidations(
    db, addrs, now_ms, *, lookback_ms=3 * 86_400_000,
) -> dict:
    """Confirm a recent exchange-labelled self-liquidation followed by an empty Perp account.

    A normal losing close, low rolling return or one Paper loss is deliberately insufficient.  The safety
    exception requires both the source fill's ``liquidatedUser`` identity and a fresh clearinghouse snapshot
    with zero equity and no remaining positions across the standard and affected executable dexes.
    """
    addrs = sorted({
        str(addr or "").lower() for addr in (addrs or ()) if addr
    })
    if not addrs:
        return {}
    marks = ",".join("?" for _ in addrs)
    recent = {}
    for addr, stamp_ms, fill_json in db.execute(
        f"SELECT lower(addr),time,fill_json FROM candidate_fills "
        f"WHERE lower(addr) IN ({marks}) AND time>=? ORDER BY time",
        (*addrs, int(now_ms) - max(1, int(lookback_ms))),
    ).fetchall():
        try:
            fill = json.loads(fill_json or "{}")
        except (TypeError, ValueError):
            continue
        liquidation = fill.get("liquidation")
        if not isinstance(liquidation, dict):
            continue
        if str(liquidation.get("liquidatedUser") or "").lower() != addr:
            continue
        item = recent.setdefault(addr, {
            "latestLiquidationMs": 0,
            "liquidationFills": 0,
            "coins": set(),
            "dexes": {None},
        })
        item["latestLiquidationMs"] = max(
            int(item["latestLiquidationMs"]), int(stamp_ms or 0),
        )
        item["liquidationFills"] += 1
        coin = str(fill.get("coin") or "")
        if coin:
            item["coins"].add(coin)
            if ":" in coin:
                item["dexes"].add(coin.split(":", 1)[0])

    verified = {}
    for addr, item in recent.items():
        account_values = []
        open_positions = 0
        for dex in sorted(item["dexes"], key=lambda value: value or ""):
            state = rest.clearinghouse_state(addr, dex=dex)
            if not isinstance(state, dict):
                raise RuntimeError("verified_source_blowup_state_unavailable")
            account_values.append(f((state.get("marginSummary") or {}).get("accountValue")))
            for wrapped in state.get("assetPositions") or ():
                position = wrapped.get("position") or wrapped
                if abs(f(position.get("szi"))) >= config.FLAT:
                    open_positions += 1
        max_equity = max(account_values, default=0.0)
        if max_equity <= max(float(config.FLAT), 1e-6) and open_positions == 0:
            verified[addr] = {
                "latestLiquidationMs": int(item["latestLiquidationMs"]),
                "liquidationFills": int(item["liquidationFills"]),
                "coins": sorted(item["coins"]),
                "maxAccountValue": max_equity,
                "openPositions": open_positions,
            }
    return verified


def _challenger_daily_membership_decision(previous_order, proposed_order) -> dict:
    """Allow daily publication to add Core wallets, but never remove or reshuffle incumbents alone."""
    previous = tuple(dict.fromkeys(
        str(addr or "").lower() for addr in (previous_order or ()) if addr
    ))
    proposed = tuple(dict.fromkeys(
        str(addr or "").lower() for addr in (proposed_order or ()) if addr
    ))
    previous_set = set(previous)
    proposed_set = set(proposed)
    removed = tuple(addr for addr in previous if addr not in proposed_set)
    added = tuple(addr for addr in proposed if addr not in previous_set)
    if removed:
        mode = "carry"
        selected = previous
        reason = "daily_proposal_would_remove_core"
    elif added:
        mode = "promote"
        selected = proposed
        reason = "daily_strict_superset_promotion"
    else:
        mode = "refresh"
        selected = previous
        reason = "daily_evidence_refresh_membership_unchanged"
    return {
        "mode": mode,
        "reason": reason,
        "selected": selected,
        "previous": previous,
        "proposed": proposed,
        "added": added,
        "removed": removed,
    }


def _carry_challenger_daily_core_rows(previous_rows, proposed_rows, previous_core_order):
    """Publish fresh Challenger evidence while carrying the exact incumbent Core snapshot."""
    previous_by_addr = {
        str(row.addr or "").lower(): row for row in (previous_rows or ()) if row.addr
    }
    previous_order = tuple(
        str(addr or "").lower() for addr in (previous_core_order or ()) if addr
    )
    missing = [addr for addr in previous_order if addr not in previous_by_addr]
    if missing:
        raise RuntimeError(f"challenger_daily_previous_core_snapshot_missing:{len(missing)}")
    previous_core = set(previous_order)
    rows = []
    for row in proposed_rows or ():
        addr = str(row.addr or "").lower()
        if addr in previous_core:
            continue
        if row.role == selection.CORE:
            row = replace(
                row,
                role=selection.CHALLENGER,
                reason="challenger_daily_promotion_not_published",
            )
        rows.append(row)
    rows.extend(
        replace(
            previous_by_addr[addr],
            role=selection.CORE,
            reason="challenger_daily_core_carried",
            selection_rank=rank,
        )
        for rank, addr in enumerate(previous_order, 1)
    )
    return rows


def refresh_challengers(db, p) -> dict:
    """Refresh frozen evidence and publish only strict-superset Core promotions."""
    now_ms = int(time.time() * 1000)
    started, t0, stamp = now_iso(), time.time(), now_iso()
    previous_generation = selection.latest_published_generation(db)
    previous_selection_rows = selection.current_selection_rows(db)
    previous_core_order = tuple(
        str(addr or "").lower()
        for addr in (selection.published_core_addrs(db) or ())
        if addr
    )
    previous_core = set(previous_core_order)
    base_generation, workset = challenger_refresh_pool(db)
    if not base_generation or not workset:
        record_challenger_refresh_skip(db, "no_complete_challenger_pool")
        return {"status": "skipped", "reason": "no_complete_challenger_pool"}
    base_policy = db.execute(
        "SELECT COUNT(*),"
        "SUM(CASE WHEN model_version=? AND policy_version=? THEN 1 ELSE 0 END) "
        "FROM follow_selection WHERE generation=?",
        (
            pre_strict.SELECTION_MODEL_VERSION,
            pre_strict.POLICY_VERSION,
            base_generation,
        ),
    ).fetchone()
    if (
        not base_policy
        or int(base_policy[0] or 0) == 0
        or int(base_policy[0] or 0) != int(base_policy[1] or 0)
    ):
        record_challenger_refresh_skip(db, "legacy_generation_policy_mismatch")
        return {"status": "skipped", "reason": "legacy_generation_policy_mismatch"}
    base_promotion_universe = {
        str(addr or "").lower()
        for (addr,) in db.execute(
            "SELECT addr FROM follow_selection WHERE generation=? "
            "AND role IN ('core','challenger') AND model_version=? AND policy_version=?",
            (
                base_generation,
                pre_strict.SELECTION_MODEL_VERSION,
                pre_strict.POLICY_VERSION,
            ),
        ).fetchall()
        if addr
    }

    selection_mode = str(
        params.get(db, "FOLLOW_SELECTION_MODE", config.FOLLOW_SELECTION_MODE) or "auto"
    ).lower()
    if selection_mode != "auto":
        record_challenger_refresh_skip(db, "automatic_selection_disabled")
        return {"status": "skipped", "reason": "automatic_selection_disabled"}

    generation_id = generation.begin_generation(
        db, source="challenger_daily", started_at=started,
        workset_mode="frozen_challenger_pool", fill_mode="delta",
    )
    p.scan_generation = generation_id
    p.full_scan = False
    p.no_harvest = True
    p.rebuild_sector_policy = True
    p.source_only_profile = True
    p.copy_bt_sigmas = {}
    p.copy_bt_market_ctx = {}
    p.copy_bt_overrides = _copy_bt_overrides(db)
    p.margin_equity_pct = p.copy_bt_overrides.get(
        "MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT,
    )
    db.commit()
    _set_scanner_proc(db, "scanning", {"phase": "challenger_refresh"})
    _set_scan_progress(
        db, state="scanning", started_at=started, stage="challenger_prepare",
        candidates_scanned=0, candidates_total=len(workset), manual=0,
    )
    rest.reset_request_stats()
    profiled = failed = valid_profiles = deferred_profiles = rejected = 0
    outcomes = {}
    verified_source_blowups = {}
    hard_safety_core = set()
    severe_copy_liquidations = set()
    published = False

    try:
        _stage_existing_leaderboard(db, generation_id)
        universe = rest.copyable_universe(force=True)
        if not universe:
            raise RuntimeError("copyable_universe_unavailable")
        p.copyable_universe = frozenset(universe)
        p.generation_market_resolver = generation_market.Resolver(
            db, generation_id, now_ms, p.copyable_universe,
            generation_market.fetch_context_snapshot(p.copyable_universe),
            db_lock=_db_lock,
        )
        generation.record_workset(
            db, generation_id,
            workset_mode="frozen_challenger_pool", fill_mode="delta",
            full_refresh_shard=None, workset_n=len(workset), deferred_n=0,
            metrics={
                "baseFullGeneration": base_generation,
                "marginEquityPct": float(p.margin_equity_pct),
                "initialMarginEquity": float(config.INITIAL_BALANCE),
            },
        )
        db.commit()

        _set_scan_progress(
            db, stage="perp_prefilter", candidates_scanned=0,
            candidates_total=len(workset),
        )
        perp_results = _run_perp_prefilter(
            db, workset, p, stamp, allow_cache=False, source="challenger_daily",
        )
        p.official_perp_results = dict(perp_results)
        desired_cache_start_ms = now_ms - config.PROFILE_FETCH_DAYS * 86_400_000
        incomplete_cache = set(_incomplete_fill_cache_addrs(
            db, workset, desired_cache_start_ms,
        ))
        open_copy_pnl_by_addr = {
            str(addr or "").lower(): f(unrealized)
            for addr, unrealized in db.execute(
                "SELECT addr,SUM(COALESCE(unrealized_pnl,0)) FROM copy_position "
                "WHERE status='open' GROUP BY addr"
            ).fetchall()
        }
        p.open_copy_pnl_by_addr = dict(open_copy_pnl_by_addr)
        cols = storage.PROFILE_COLS.split(",")
        priors = {
            str(row[0] or "").lower(): dict(zip(cols, row))
            for row in db.execute(f"SELECT {storage.PROFILE_COLS} FROM profile").fetchall()
        }
        lbs = {
            str(addr or "").lower(): {
                "account_value": account_value,
                "week_roi": week_roi, "mon_roi": mon_roi, "all_roi": all_roi,
            }
            for addr, account_value, week_roi, mon_roi, all_roi in db.execute(
                "SELECT addr,account_value,week_roi,mon_roi,all_roi "
                "FROM leaderboard_staging WHERE generation=?",
                (generation_id,),
            ).fetchall()
        }

        def work(addr):
            prior = priors.get(addr)
            if addr in incomplete_cache:
                return addr, prior, _defer_profile(
                    db, addr, prior, stamp, "daily_cache_incomplete_full_scan_required",
                )
            gate = perp_results.get(addr)
            if gate is None:
                return addr, prior, _defer_profile(
                    db, addr, prior, stamp, "official_perp_evidence_missing",
                )
            if gate.deferred:
                return addr, prior, _defer_profile(
                    db, addr, prior, stamp, gate.reason,
                )
            # The frozen daily pool already owns a complete 37-day cache, so even an official business-gate
            # failure or normal evidence-building state must consume its cheap delta. A zeroed wallet commonly
            # fails Portfolio first; skipping here would hide the liquidation fill needed by hard safety below.
            result = _profile_one(
                db, addr, now_ms - int(p.days) * 86_400_000, now_ms,
                p, prior, lbs.get(addr, {}), stamp, universe, force_full=False,
            )
            # Portfolio week volume is a new-wallet download decision only. Every frozen strict
            # Core/Challenger already owns a complete cache and must be requalified from fills even when its
            # current cheap-recall telemetry falls below the new-wallet floor.
            return addr, prior, result

        workers = max(1, int(getattr(p, "workers", 4) or 4))
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(work, addr): addr for addr in workset}
            for future in concurrent.futures.as_completed(futures):
                addr = futures[future]
                done += 1
                try:
                    _addr, prior, (status, reason, profile, _hit_cap) = future.result()
                except Exception as exc:  # noqa: BLE001
                    reason = f"profile_error:{type(exc).__name__}"
                    if addr in previous_core:
                        failed += 1
                        outcomes[addr] = {
                            "status": "error", "data_status": "deferred_data_error",
                            "reason": reason,
                        }
                        print(
                            f"  [{done}/{len(workset)}] Core refresh FAIL: {exc}",
                            flush=True,
                        )
                    else:
                        status, reason, profile, _hit_cap = _defer_profile(
                            db, addr, priors.get(addr), stamp, reason,
                        )
                        profiled += 1
                        deferred_profiles += 1
                        outcomes[addr] = {
                            "status": status,
                            "data_status": "deferred_data_error",
                            "reason": reason,
                        }
                        print(
                            f"  [{done}/{len(workset)}] Challenger refresh deferred: {exc}",
                            flush=True,
                        )
                else:
                    profiled += 1
                    data_status = profile.get("data_status") or "valid"
                    outcomes[addr] = {
                        "status": status, "data_status": data_status, "reason": reason,
                    }
                    if data_status == "deferred_data_error":
                        deferred_profiles += 1
                    else:
                        valid_profiles += 1
                    if status in {"rejected", "retired"}:
                        rejected += 1
                _set_scan_progress(
                    db, stage="challenger_score", candidates_scanned=done,
                    candidates_total=len(workset),
                )
                if done % 10 == 0:
                    _set_scanner_proc(
                        db, "scanning",
                        {"stage": "challenger_score", "scanned": done, "total": len(workset)},
                    )
        if failed:
            raise RuntimeError(f"challenger_profile_failures:{failed}")
        invalid_core = [
            addr for addr in previous_core
            if (outcomes.get(addr) or {}).get("data_status") != "valid"
        ]
        if invalid_core:
            raise RuntimeError(f"challenger_refresh_core_data_incomplete:{len(invalid_core)}")

        verified_source_blowups = _verified_zero_equity_source_liquidations(
            db, workset, now_ms,
        )
        historical_major_liquidations = _apply_historical_major_liquidation_gate(
            db, generation_id,
        )
        hard_safety_core = previous_core & historical_major_liquidations
        for addr, evidence in verified_source_blowups.items():
            _record_wallet_risk_event(
                db, addr, "source_account_liquidated_zero",
                str(int(evidence.get("latestLiquidationMs") or now_ms)),
                occurred_at=int(evidence.get("latestLiquidationMs") or now_ms),
                coin=",".join(evidence.get("coins") or ()) or None,
                evidence=evidence,
            )
            if (outcomes.get(addr) or {}).get("status") not in {"rejected", "retired"}:
                rejected += 1
            outcomes.setdefault(addr, {}).update({
                "status": "rejected",
                "reason": "source_account_liquidated_zero",
                "data_status": "valid",
            })
            db.execute(
                "UPDATE profile SET status='rejected',reason='source_account_liquidated_zero' "
                "WHERE addr=? AND profile_generation=?",
                (addr, generation_id),
            )
            pipeline_audit._insert_event(
                db, stamp=stamp, source="challenger_daily",
                stage="hard_safety", addr=addr, status="rejected",
                reason="source_account_liquidated_zero",
                payload={
                    **evidence,
                    "wasCore": addr in previous_core,
                    "action": (
                        "exit_only" if addr in previous_core else "exclude_from_promotion"
                    ),
                },
            )
        historical_major_liquidations |= set(verified_source_blowups)
        hard_safety_core |= previous_core & set(verified_source_blowups)
        if verified_source_blowups:
            db.commit()

        source_pool, source_tail = _source_quality_pool(
            db, generation_id,
        )
        _set_scan_progress(
            db, stage="rough_copy", candidates_scanned=0,
            candidates_total=len(source_pool),
        )
        rough_summary = _rough_replay_source_pool(
            db, source_pool, generation_id, now_ms, p, stamp,
            source="challenger_daily",
            queue_allowed_addrs=base_promotion_universe,
        )
        severe_copy_liquidations = {
            str(addr or "").lower()
            for (addr,) in db.execute(
                "SELECT addr FROM profile WHERE profile_generation=? "
                "AND reason='copy_single_liquidation_loss_over_5pct'",
                (generation_id,),
            ).fetchall()
        }
        severe_copy_core = previous_core & severe_copy_liquidations
        hard_safety_core |= severe_copy_core
        for addr in sorted(severe_copy_liquidations):
            pipeline_audit._insert_event(
                db, stamp=stamp, source="challenger_daily",
                stage="hard_safety", addr=addr, status="rejected",
                reason="copy_single_liquidation_loss_over_5pct",
                payload={
                    "wasCore": addr in previous_core,
                    "action": (
                        "exit_only" if addr in previous_core
                        else "exclude_from_candidate_pool"
                    ),
                    "thresholdPct": config.CORE_COPY_MAX_SINGLE_LIQUIDATION_LOSS_PCT,
                },
            )
        p.source_only_profile = False
        pipeline_audit._insert_event(
            db, stamp=stamp, source="challenger_daily",
            stage="source_quality_pool", status="ok",
            reason="frozen_challenger_pre_strict",
            payload={
                "baseFullGeneration": base_generation,
                "structuralPassed": len(source_pool),
                "preStrictPassed": len(rough_summary.get("qualified") or ()),
                "top32": len(rough_summary.get("queued") or ()),
                "promotionUniverse": len(base_promotion_universe),
            },
        )
        db.commit()
        market_snapshot = generation_market.seal(db, generation_id)
        scope_audit = _assert_scoped_fill_cache(db, workset, universe)
        pipeline_audit.record_profile_snapshot(
            db, stamp, "challenger_daily", workset,
        )
        db.commit()

        _set_scan_progress(
            db, stage="prepare_selection_candidates",
            candidates_scanned=len(workset), candidates_total=len(workset),
        )
        refresh_watchlist(
            db, stamp, leaderboard_generation=generation_id, commit=False,
        )
        preview = _selection_prefetch_candidates(
            db, generation_id, now_ms,
            limit=int(config.PRE_STRICT_QUEUE_MAX_N),
        )
        db.rollback()
        if preview:
            _set_scan_progress(
                db, stage="prefetch_selection_paths",
                candidates_scanned=len(workset), candidates_total=len(workset),
            )
            _prefetch_selection_paths(db, preview, now_ms, generation_id)

        fixed_formation = form_quality_prefix(
            db, generation_id, stamp, now_ms,
            retune=False, force_retune=False,
        )
        fixed_core_order = tuple(
            str(addr or "").lower()
            for addr in (fixed_formation.get("selected") or ())
            if addr
        )
        fixed_core = set(fixed_core_order)
        daily_floor_order = tuple(
            addr for addr in previous_core_order if addr not in hard_safety_core
        )
        fixed_decision = _challenger_daily_membership_decision(
            daily_floor_order, fixed_core_order,
        )
        membership_retune_triggered = False
        retune_attempted = False
        promotion_blocked_reason = None
        formation = fixed_formation
        publish_core_order = fixed_decision["selected"]
        if hard_safety_core:
            promotion_blocked_reason = (
                "challenger_daily_hard_safety_removal"
            )
            publish_core_order = fixed_core_order
            pipeline_audit._insert_event(
                db, stamp=stamp, source="challenger_daily",
                stage="promotion_review", status="blocked",
                reason=promotion_blocked_reason,
                payload={
                    "previousCore": len(previous_core),
                    "hardSafetyRemoved": len(hard_safety_core),
                    "protectedCore": len(daily_floor_order),
                    "fixedSurfaceCore": len(fixed_core),
                    "promotionSuppressed": len(fixed_core - set(daily_floor_order)),
                },
            )
            db.commit()
        elif fixed_decision["mode"] == "promote":
            retune_attempted = True
            _set_scan_progress(
                db, stage="challenger_membership_retune",
                candidates_scanned=len(workset), candidates_total=len(workset),
            )
            tuned_formation = form_quality_prefix(
                db, generation_id, stamp, now_ms,
                retune=True, force_retune=True,
            )
            tuned_core_order = tuple(
                str(addr or "").lower()
                for addr in (tuned_formation.get("selected") or ())
                if addr
            )
            tuned_decision = _challenger_daily_membership_decision(
                daily_floor_order, tuned_core_order,
            )
            if tuned_decision["mode"] == "promote":
                formation = tuned_formation
                publish_core_order = tuned_decision["selected"]
                membership_retune_triggered = True
            else:
                promotion_blocked_reason = (
                    "retuned_proposal_not_strict_superset"
                )
                formation = fixed_formation
                publish_core_order = fixed_core_order
            pipeline_audit._insert_event(
                db, stamp=stamp, source="challenger_daily",
                stage="membership_retune",
                status="ok" if membership_retune_triggered else "blocked",
                reason=(
                    "challenger_promotion_retuned"
                    if membership_retune_triggered
                    else promotion_blocked_reason
                ),
                payload={
                    "previousCore": len(previous_core),
                    "fixedSurfaceCore": len(fixed_core),
                    "added": len(fixed_core - previous_core),
                    "removed": len(previous_core - fixed_core),
                    "tunedCore": len(tuned_core_order),
                },
            )
            db.commit()
        elif fixed_decision["mode"] == "carry":
            promotion_blocked_reason = fixed_decision["reason"]
            publish_core_order = fixed_core_order
            pipeline_audit._insert_event(
                db, stamp=stamp, source="challenger_daily",
                stage="promotion_review", status="blocked",
                reason=promotion_blocked_reason,
                payload={
                    "previousCore": len(previous_core),
                    "fixedSurfaceCore": len(fixed_core),
                    "proposedAdded": len(fixed_core - previous_core),
                    "proposedRemoved": len(previous_core - fixed_core),
                },
            )
            db.commit()
        _assert_automatic_formation_tuned(
            formation, required=membership_retune_triggered,
        )
        _assert_margin_equity_snapshot(db, p.margin_equity_pct)
        publication_stamp = now_iso()
        refresh_watchlist(
            db, publication_stamp,
            leaderboard_generation=generation_id, commit=False,
        )
        _apply_formation_params(db, formation, publication_stamp)
        proposed_selection_rows, marginal = _build_explicit_selection(
            db, generation_id, publication_stamp, now_ms,
            forced_core_order=publish_core_order,
            formation_meta=formation.get("search") or {},
            effective_qualifications=formation.get("qualifications") or {},
            effective_scores=formation.get("scores") or {},
            effective_policies=formation.get("policies") or {},
            effective_metrics=formation.get("walletMetrics") or {},
            effective_score_details=formation.get("scoreDetails") or {},
            effective_replay_params_hash=formation.get("replayParamsHash"),
            audit_stamp=stamp,
            allow_loo=False,
        )
        if promotion_blocked_reason:
            selection_rows = _carry_challenger_daily_core_rows(
                previous_selection_rows,
                proposed_selection_rows,
                daily_floor_order,
            )
            marginal = None
            affected = previous_core | {
                row.addr for row in proposed_selection_rows
                if row.role == selection.CORE
            }
            final_by_addr = {row.addr: row for row in selection_rows}
            last_open = {
                str(addr or "").lower(): value
                for addr, value in db.execute(
                    "SELECT addr,last_copyable_open_ms FROM profile"
                ).fetchall()
            }
            for addr in sorted(affected):
                row = final_by_addr.get(addr)
                if not row:
                    continue
                upsert_wallet_registry(
                    db, addr, generation=generation_id, seen_at=publication_stamp,
                    state=row.role, role=row.role,
                    data_status=row.data_status, reason=row.reason,
                    last_actionable_open_ms=last_open.get(addr),
                )
        else:
            selection_rows = proposed_selection_rows
        generation.mark_generation_ready(
            db, generation_id, profile_total=len(workset),
            profile_valid=valid_profiles, profile_deferred=deferred_profiles,
            profile_rejected=rejected, profile_complete=True,
            ready_at=publication_stamp,
        )
        selection.replace_selection_rows(
            db, generation_id, selection_rows, selected_at=publication_stamp,
        )
        market_validation = _selection_market_snapshot_validation(
            db, generation_id, selection_rows, now_ms,
        )
        generation.publish_generation(
            db, generation_id, published_at=publication_stamp,
            promote_leaderboard=False,
        )
        current_core = _record_explicit_follow_history(
            db, selection_rows, publication_stamp, previous_core, generation_id,
        )
        removed_core = sorted(previous_core - set(current_core))
        unexpected_removed = set(removed_core) - hard_safety_core
        if unexpected_removed:
            raise RuntimeError(
                f"challenger_daily_demotion_invariant:{len(unexpected_removed)}"
            )
        strategy_reason = (
            "challenger_daily_hard_safety_exit"
            if hard_safety_core
            else (
                "challenger_daily_promotion_retune"
                if membership_retune_triggered
                else (
                    "challenger_daily_promotion_only_core_carried"
                    if promotion_blocked_reason
                    else "challenger_daily_evidence_refresh"
                )
            )
        )
        active_strategy = strategy_revision.create_revision(
            db, generation_id, source="challenger_daily",
            reason=strategy_reason,
            validation={
                **((marginal.search_meta or {}) if marginal is not None else {}),
                "marketSnapshot": market_validation,
                "baseFullGeneration": base_generation,
                "promotionOnly": True,
                "promotionBlockedReason": promotion_blocked_reason,
                "verifiedSourceBlowups": len(verified_source_blowups),
                "severeCopyLiquidations": len(severe_copy_liquidations),
                "hardSafetyCoreRemoved": len(hard_safety_core),
                "carriedCoreEvidenceGeneration": (
                    previous_generation if promotion_blocked_reason else None
                ),
            },
            stamp=publication_stamp,
        )
        for row in selection_rows:
            pipeline_audit._insert_event(
                db, stamp=stamp, source="challenger_daily", stage="selection",
                addr=row.addr, status=row.role, reason=row.reason,
                follow_score=row.follow_score,
                payload={
                    "generation": generation_id,
                    "selectionRank": row.selection_rank,
                    "profitPriority": row.replay_profit_priority,
                    "marginalUtility": row.utility,
                    "dataStatus": row.data_status,
                    "evidenceStatus": row.evidence_status,
                },
            )
        added_core = sorted(set(current_core) - previous_core)
        pipeline_audit._insert_event(
            db, stamp=stamp, source="challenger_daily",
            stage="selection_summary", status="ok",
            reason=strategy_reason,
            payload={
                "generation": generation_id,
                "baseFullGeneration": base_generation,
                "core": len(current_core),
                "challenger": sum(
                    1 for row in selection_rows if row.role == selection.CHALLENGER
                ),
                "coreAdded": len(added_core), "coreRemoved": len(removed_core),
                "membershipRetuneTriggered": membership_retune_triggered,
                "retuneAttempted": retune_attempted,
                "promotionOnly": True,
                "promotionBlockedReason": promotion_blocked_reason,
                "verifiedSourceBlowups": len(verified_source_blowups),
                "severeCopyLiquidations": len(severe_copy_liquidations),
                "hardSafetyCoreRemoved": len(hard_safety_core),
                "strategyRevision": active_strategy["revision"],
            },
        )
        metrics_json = {
            "kind": "challenger_refresh",
            "baseFullGeneration": base_generation,
            "workset": len(workset), "profileValid": valid_profiles,
            "profileDeferred": deferred_profiles,
            "promotionUniverse": len(base_promotion_universe),
            "roughCopyQualified": len(rough_summary.get("qualified") or ()),
            **_pre_strict_counts(db, generation_id),
            "selectionCore": len(current_core),
            "selectionChallenger": sum(
                1 for row in selection_rows if row.role == selection.CHALLENGER
            ),
            "coreAdded": len(added_core), "coreRemoved": len(removed_core),
            "retuned": membership_retune_triggered,
            "membershipRetuneTriggered": membership_retune_triggered,
            "retuneAttempted": retune_attempted,
            "promotionOnly": True,
            "promotionBlockedReason": promotion_blocked_reason,
            "verifiedSourceBlowups": len(verified_source_blowups),
            "severeCopyLiquidations": len(severe_copy_liquidations),
            "hardSafetyCoreRemoved": len(hard_safety_core),
            "marketSnapshot": market_snapshot,
            "marketValidation": market_validation, "marketScopeAudit": scope_audit,
            **rest.request_stats(),
        }
        db.execute(
            "UPDATE scan_generation SET metrics_json=? WHERE generation=?",
            (json.dumps(metrics_json, sort_keys=True), generation_id),
        )
        portfolio_replay, selection_replay = _store_final_copy_summary(
            db, generation_id, marginal,
        )
        auto_tune.bind_active_tune_rollback_core(db, current_core)
        _record_run(
            db, started, t0, len(workset), profiled,
            len(added_core), len(removed_core),
            len(set(current_core) & previous_core), rejected, len(current_core),
            full=False, failed=0, complete=True, kind="challenger_refresh",
            generation_id=generation_id, api_stats=rest.request_stats(),
            commit=False,
        )
        db.commit()
        published = True
        _set_scan_progress(
            db, state="idle", stage="persist",
            candidates_scanned=len(workset), candidates_total=len(workset),
        )
        _set_scanner_proc(
            db, "idle",
            {"last_challenger_refresh_at": now_iso(), "active": len(current_core)},
        )
        db.commit()
        return {
            "status": "published", "generation": generation_id,
            "baseFullGeneration": base_generation,
            "core": len(current_core),
            "challenger": sum(
                1 for row in selection_rows if row.role == selection.CHALLENGER
            ),
            "coreAdded": len(added_core), "coreRemoved": len(removed_core),
            "promotionOnly": True,
            "promotionBlockedReason": promotion_blocked_reason,
            "verifiedSourceBlowups": len(verified_source_blowups),
            "severeCopyLiquidations": len(severe_copy_liquidations),
            "hardSafetyCoreRemoved": len(hard_safety_core),
            "portfolioReplay": portfolio_replay,
            "selectionReplay": selection_replay,
            "strategyRevision": active_strategy["revision"],
        }
    except Exception as exc:
        db.rollback()
        if published:
            _set_scan_progress(
                db, state="idle", stage="error",
                candidates_scanned=len(workset), candidates_total=len(workset),
            )
            _set_scanner_proc(
                db, "idle",
                {"last_error": str(exc)[:300], "active": len(current_core)},
            )
            db.commit()
            raise
        try:
            generation.fail_generation(db, generation_id, str(exc))
        except Exception:
            pass
        _record_run(
            db, started, t0, len(workset), profiled, 0, 0,
            len(previous_core), rejected, len(previous_core),
            full=False, failed=max(1, failed), complete=False,
            kind="challenger_refresh", generation_id=generation_id,
            reason=str(exc), api_stats=rest.request_stats(),
        )
        pipeline_audit._insert_event(
            db, stamp=stamp, source="challenger_daily",
            stage="selection_summary", status="failed", reason=str(exc)[:300],
            payload={
                "generation": generation_id,
                "baseFullGeneration": base_generation,
                "retainedGeneration": previous_generation,
            },
        )
        _set_scan_progress(
            db, state="idle", stage="error",
            candidates_scanned=profiled, candidates_total=len(workset),
        )
        _set_scanner_proc(
            db, "idle",
            {"last_error": str(exc)[:300], "active": len(previous_core)},
        )
        db.commit()
        raise


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
    # MANUAL (dashboard button → pending rescan command) vs AUTO (timer schedule, no command). The frontend
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
                    full=run_full, failed=1, complete=False, generation_id=generation_id,
                    reason=str(exc), api_stats=rest.request_stats())
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
    recall_cand = [r[0] for r in db.execute(
        f"SELECT addr FROM leaderboard_staging WHERE generation=? AND is_candidate=1 "
        f"ORDER BY {order} DESC",
        (generation_id,),
    ).fetchall()]
    _leaderboard_recall_audit(db, generation_id, stamp, p)
    prefilter_started_at = time.time()
    _set_scan_progress(db, stage="perp_prefilter", candidates_scanned=0,
                       candidates_total=len(recall_cand))
    # A complete production scan means a real Portfolio refresh, not merely a complete workset fed by the
    # two-hour restart cache. Direct recovery/test callers may still opt into exact-policy cache reuse.
    perp_results = _run_perp_prefilter(
        db, recall_cand, p, stamp,
        allow_cache=not bool(getattr(p, "full_scan", False)),
    )
    p.official_perp_results = dict(perp_results)
    prefilter_done_at = time.time()
    prefilter_api_stats = rest.request_stats()
    cand = [addr for addr in recall_cand if perp_results[addr].passed]
    print(
        f"  coarse recall {len(recall_cand)} · Portfolio/Perp precheck passed {len(cand)} · "
        f"deferred {sum(result.deferred for result in perp_results.values())}", flush=True,
    )
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
    # Cheap discovery gates only control *new* expensive collection. Current executable/observed roles and
    # open-position owners always receive a fresh retention replay, even when their latest official week or
    # Portfolio mix temporarily misses the discovery surface. Recently removed Core wallets also remain on
    # this evidence lane: an empty publication must stop execution without erasing recovery proof.
    former_core_addrs = _recent_former_core_addrs(db, as_of=stamp)
    retention_addrs = (
        set(core_addrs) | set(challenger_addrs)
        | set(position_addrs) | set(former_core_addrs)
    )
    for addr in retention_addrs:
        perp_results.setdefault(
            addr,
            perp_prefilter.Result(
                "passed", "retention_evidence_refresh",
                {"week": {"hardGate": False, "retentionRefresh": True}},
            ),
        )
    p.official_perp_results = dict(perp_results)
    # Freeze the open-copy PnL surface for the generation. Worker threads use it only to distinguish a
    # profitable carried mirrored episode from a dormant/losing wallet; it never bypasses economic/risk gates.
    p.open_copy_pnl_by_addr = dict(open_copy_pnl_by_addr)
    cand_set = set(cand)
    recent = db.execute(
        "SELECT duration_s,COALESCE(profiled,probed_new) FROM scan_runs "
        "WHERE COALESCE(profiled,probed_new)>0 AND complete=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    estimated_profile_s = max(1.0, min(120.0, (f(recent[0]) / int(recent[1])))) if recent else 12.0
    desired_cache_start_ms = now_ms - config.PROFILE_FETCH_DAYS * 86400_000
    full_refetch_due = set(_incomplete_fill_cache_addrs(
        db,
        set(cand) | retention_addrs,
        desired_cache_start_ms,
    )) | set(warmup_backfill_addrs)
    workset_info = schedule_profile_workset(
        cand,
        qualified_addrs=former_core_addrs,
        core_addrs=core_addrs,
        challenger_addrs=challenger_addrs,
        warmup_backfill_addrs=warmup_backfill_addrs,
        off_list_qualified_addrs=(),
        position_addrs=position_addrs,
        full_refetch_addrs=full_refetch_due,
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
        "formerCoreRecheck": len(former_core_addrs),
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
    continuity_recheck_n = len(retention_addrs - cand_set)
    full_refetch = set(workset_info["refresh"]["full_refetch"])
    priority_addrs = set(workset[:workset_info["counts"]["priority"]])
    _set_scan_progress(db, stage="fetch_history", candidates_total=len(workset))
    _pace = config.MIN_POST_INTERVAL   # live adaptive pace (fast when no copy-trading, slow trickle when observer up)
    print(f"scan: {mode} · {len(workset)} wallets (incl {continuity_recheck_n} role/position evidence rechecks), "
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
    p.source_only_profile = True

    def _work(addr):
        prior = priors.get(addr)
        gate = perp_results.get(addr)
        if gate is None:
            return addr, prior, _reject_prefilter_profile(
                db, addr, prior, stamp, generation_id, "official_roi_below_floor",
            )
        if gate.deferred:
            if gate.reason in {
                "history_under_7d", "history_under_28d",
                "boundary_sample_gap", "zero_start_equity",
            }:
                return addr, prior, _defer_official_evidence_profile(
                    db, addr, prior, stamp, generation_id, gate,
                )
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
    # A complete generation owns the same catastrophic source-risk proof as the daily safety path.  Historical
    # fills alone identify only a possible self-liquidation; the fresh clearinghouse snapshot is what turns it
    # into a hard source-account-zero rejection.  This check is sparse because it requests state only for
    # wallets whose cached fills contain a recent exchange-labelled self-liquidation.
    verified_source_blowups = _verified_zero_equity_source_liquidations(
        db, workset, now_ms,
    )
    for addr, evidence in verified_source_blowups.items():
        _record_wallet_risk_event(
            db, addr, "source_account_liquidated_zero",
            str(int(evidence.get("latestLiquidationMs") or now_ms)),
            occurred_at=int(evidence.get("latestLiquidationMs") or now_ms),
            coin=",".join(evidence.get("coins") or ()) or None,
            evidence={
                **evidence,
                "generation": generation_id,
                "stage": "complete_scan",
            },
        )
        db.execute(
            "UPDATE profile SET status='rejected',reason='source_account_liquidated_zero',"
            "data_status='valid',evidence_status='invalid' "
            "WHERE lower(addr)=? AND profile_generation=?",
            (addr, generation_id),
        )
        pipeline_audit._insert_event(
            db, stamp=stamp, source="scan", stage="hard_safety",
            addr=addr, status="rejected", reason="source_account_liquidated_zero",
            payload={
                **evidence,
                "action": "exclude_from_candidate_pool",
            },
        )
    if verified_source_blowups:
        db.commit()
    source_pool, source_tail = _source_quality_pool(
        db, generation_id,
    )
    db.commit()
    _set_scan_progress(
        db, stage="rough_copy", candidates_scanned=0, candidates_total=len(source_pool),
    )
    rough_summary = _rough_replay_source_pool(
        db, source_pool, generation_id, now_ms, p, stamp,
    )
    p.source_only_profile = False
    pipeline_audit._insert_event(
        db, stamp=stamp, source="scan", stage="source_quality_pool",
        status="ok", reason="pre_strict_queue_frozen",
        payload={
            "structuralPassed": len(source_pool),
            "roughCopyCompleted": rough_summary.get("attempted", 0),
            "preStrictPassed": len(rough_summary.get("qualified") or ()),
            "top32": len(rough_summary.get("queued") or ()),
        },
    )
    db.commit()

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
            scope_audit = _assert_scoped_fill_cache(
                db, set(profiled_addrs), universe,
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
        path_prefetch_error = None
        # Build only the bounded candidate universe in a rolled-back staging pass, then fetch its shared
        # market path before the atomic publication transaction.  The old flow ran a complete fills-only
        # selection here and repeated it during final publication merely to discover which paths to fetch.
        # Querying the same bounded near-Core universe removes that duplicate search while keeping network I/O
        # outside the Dashboard/Observer SQLite writer lock.
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
                preview_candidates = _selection_prefetch_candidates(
                    db, generation_id, now_ms,
                )
                db.rollback()
                if preview_candidates:
                    _set_scan_progress(
                        db, stage="prefetch_selection_paths",
                        candidates_scanned=len(workset), candidates_total=len(workset),
                    )
                    _prefetch_selection_paths(db, preview_candidates, now_ms, generation_id)
            except Exception as exc:  # noqa: BLE001 - publication guard below preserves the prior generation
                db.rollback()
                print(f"selection price-path prefetch unavailable: {exc}", flush=True)
                path_prefetch_error = exc
        try:
            if path_prefetch_error is not None:
                raise RuntimeError(
                    f"selection_price_path_prefetch_failed:{path_prefetch_error}"
                ) from path_prefetch_error
            _assert_margin_equity_snapshot(db, p.margin_equity_pct)
            formation = None
            if selection_mode == "auto":
                # The visible switch owns the complete publication contract.  When enabled, a new generation
                # must tune its own bounded Core pool and pass final strict replay on that exact surface before
                # Core membership and parameters are published together.  The tuner is now count-bounded and
                # summary-only, so reclaim profile temporaries before entering its largest allocation phase.
                automatic_retune = _automatic_formation_retune_enabled(db)
                gc.collect()
                formation = form_quality_prefix(
                    db, generation_id, stamp, now_ms,
                    retune=automatic_retune, force_retune=automatic_retune,
                )
                membership_retune_triggered = (
                    not automatic_retune
                    and _formation_membership_changed(formation, previous_core)
                )
                if membership_retune_triggered:
                    _set_scan_progress(
                        db, stage="core_membership_retune",
                        candidates_scanned=len(workset), candidates_total=len(workset),
                    )
                    formation = form_quality_prefix(
                        db, generation_id, stamp, now_ms,
                        retune=True, force_retune=True,
                    )
                _assert_automatic_formation_tuned(
                    formation,
                    required=bool(automatic_retune or membership_retune_triggered),
                )
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
                manual_core_ok = {
                    row["addr"] for row in _quality_core_profiles(
                        db, generation_id, core_only=True, now_ms=now_ms,
                    )
                }
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
                            and data_status == "valid" and item.addr.lower() in manual_core_ok:
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
                    effective_policies=(formation or {}).get("policies") or {},
                    effective_metrics=(formation or {}).get("walletMetrics") or {},
                    effective_score_details=(formation or {}).get("scoreDetails") or {},
                    effective_replay_params_hash=(formation or {}).get("replayParamsHash"),
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
                        "profitPriority": row.replay_profit_priority,
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
                    **(
                        (marginal.search_meta or {}) if marginal
                        else ((formation or {}).get("search") or {})
                    ),
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
            pre_strict_counts = _pre_strict_counts(db, generation_id)
            stage_metrics = {
                "durationSec": round(duration_s, 3),
                "leaderboardAndUniverseSec": round(harvest_done_at - harvest_started_at, 3),
                "perpPrefilterSec": round(prefilter_done_at - prefilter_started_at, 3),
                "dailySloSec": None,
                "dailySloMet": None,
                "profileDurationSec": round(profile_done_at - prefilter_done_at, 3),
                "profileSloSec": None,
                "profileSloMet": None,
                "coarseRecallPassed": len(recall_cand),
                "perpPrefilterPassed": len(cand),
                "perpPrefilterDeferred": sum(result.deferred for result in perp_results.values()),
                "structurePassed": len(source_pool),
                **pre_strict_counts,
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
        portfolio_replay, selection_replay = _store_final_copy_summary(
            db, generation_id, marginal,
        )
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
                full=run_full, failed=failed, complete=complete, generation_id=generation_id,
                reason=None if complete else "generation_not_published",
                api_stats=rest.request_stats())
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
