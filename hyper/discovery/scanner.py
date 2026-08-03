"""Discovery domain: the generation-safe scanner that maintains selection evidence.

Leaderboard recall feeds a bounded profile workset, a 37-day fill cache, canonical
Copy replay, and atomic Core/Challenger publication.
"""
import calendar
import concurrent.futures
from dataclasses import replace
import gc
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from types import SimpleNamespace

from hyper import config, params, storage
from hyper.copy.copy_backtest import (
    ADD_METRICS_VERSION,
    prepare_price_path,
)
from hyper.copy.fills import build_episodes
from hyper.copy.copy_data import (
    is_copyable_coin,
    load_copyable_fills,
    normalize_copyable_fills,
)
from hyper.copy.copy_policy import COPY_POLICY_PARAM_KEYS, load_copy_policy
from hyper.copy import replay_parallel
from hyper.copy.economics import (
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
from hyper.market import generation_market, price_path, rest
from hyper.execution.mode import selected_book
from hyper.ops import resource_guard
from hyper.selection import (
    auto_tune,
    core_retention,
    core_formation,
    follow_score,
    pre_strict,
    state as selection,
    strategy_revision,
    wallet_risk,
)
from . import collection_blacklist, generation, metrics, perp_prefilter, pipeline_audit
from .scanner_copy_bt import (
    apply_sector_copy_bt_gate as _apply_sector_copy_bt_gate,
    copy_bt_market_ctx as _copy_bt_market_ctx,
    copy_bt_overrides as _copy_bt_overrides,
    copy_bt_results as _copy_bt_results,
    copy_bt_sigmas as _copy_bt_sigmas,
    sector_copy_bt_results as _sector_copy_bt_results,
)
from .scanner_lifecycle import (
    apply_wallet_retention_decision,
    prune_discovery_cache as _prune_discovery_cache,
    schedule_profile_workset,
    upsert_wallet_registry,
    wallet_retention_state,
)
from hyper.util import f, now_iso

_db_lock = threading.Lock()   # serializes sqlite writes across scanner worker threads
_STRICT_REPLAY_PROCESS_CONTEXT = {}
_SCANNER_HEARTBEAT_INTERVAL_S = 60.0


def _execution_position_table(db) -> str:
    return selected_book(db).position

_SECTOR_RECOVERABLE_STRUCTURE_REASONS = {
    "bot_frequency", "hft_uncopyable", "grid_dca", "heavy_dca",
    "too_many_concurrent",
}
_SECTOR_RECOVERABLE_STATE_REASONS = set()


def _current_sector_structure_policy(perp_fills, p, *, source="current_generation"):
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
            fills, episodes, int(getattr(p, "days", 14) or 14),
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
            "rapidSameSideRetryRate": float(current.get("rapid_same_side_retry_rate") or 0.0),
            "rapidLossRetryRate": float(current.get("rapid_loss_retry_rate") or 0.0),
            "maxRetryChainEpisodes": int(current.get("rapid_retry_max_chain_episodes") or 0),
            "lossStartedRetryChainLoseRate": float(
                current.get("loss_started_retry_chain_lose_rate") or 0.0
            ),
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


def _invalid_cached_fill_tids(db, addr, universe=None) -> list:
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
    return invalid_tids


def _store_cached_fills(db, addr, fills, window_start, *, coverage_complete=False, coverage_end=None,
                        universe=None, invalid_tids=None):
    """Persist only executable Crypto/stock contracts; caller holds ``_db_lock``.

    This is a second fail-closed boundary behind the response-time filter.  A
    future caller cannot accidentally put spot, outcome or private-dex history
    back into the canonical replay cache.
    """
    # Heal rows written by an older release, and rows for a plain perp that has since been delisted.
    # Without this cleanup the publication audit would correctly fail, but could never self-recover on
    # a delta scan because an immutable stale row would remain in the cache forever.
    invalid_tids = (
        _invalid_cached_fill_tids(db, addr, universe)
        if invalid_tids is None else list(invalid_tids)
    )
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
    """Fail publication if a profiled wallet cache contains an out-of-scope row.

    Keep the audit streaming.  A complete generation can own millions of cached fills; materializing
    even a 400-wallet shard and then duplicating every decoded JSON object pushed the production scanner
    above the memory+swap budget immediately after rough replay.
    """
    owners = sorted({str(addr or "").lower() for addr in addrs or [] if addr})
    audited = invalid = 0
    for offset in range(0, len(owners), 100):
        batch = owners[offset:offset + 100]
        marks = ",".join("?" for _ in batch)
        rows = db.execute(
            f"SELECT fill_json FROM candidate_fills WHERE lower(addr) IN ({marks})",
            batch,
        )
        for (payload,) in rows:
            audited += 1
            try:
                row = json.loads(payload)
            except (TypeError, ValueError):
                invalid += 1
                continue
            if not is_copyable_coin(row.get("coin"), universe=universe):
                invalid += 1
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
        for addr in (selection.published_core_membership(db) or ())
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


def _queue_profile_persist(row: dict, **artifact) -> dict:
    row["_profile_persist"] = {"profile": True, **artifact}
    return row


def _persist_profile_batch(db, rows) -> int:
    """Persist completed worker artifacts in one parent-owned SQLite transaction."""
    pending = [row for row in rows if isinstance(row, dict) and row.get("_profile_persist")]
    if not pending:
        return 0
    cols = storage.PROFILE_COLS.split(",")
    invalid_cache_tids = {}
    try:
        with _db_lock:
            # Decode/audit old cache rows before the write transaction starts. Holding SQLite's single
            # writer slot while parsing several wallets' JSON would erase the benefit of bounded batching.
            for row in pending:
                cache = (row.get("_profile_persist") or {}).get("cache")
                if cache:
                    invalid_cache_tids[row["addr"]] = _invalid_cached_fill_tids(
                        db, row["addr"], cache.get("universe"),
                    )
        with _db_lock:
            for row in pending:
                artifact = dict(row.get("_profile_persist") or {})
                permanently_blocked = collection_blacklist.should_block(row)
                if permanently_blocked:
                    collection_blacklist.record(
                        db, row, stamp=row.get("evaluated_at") or row.get("last_refreshed"),
                    )
                    # Never write the freshly fetched raw history for a permanent automation reject. Remove
                    # any legacy discovery cache in the same bounded transaction; Paper/Live state lives in
                    # separate tables and is intentionally untouched.
                    collection_blacklist.purge_address(db, row["addr"])
                cache = artifact.get("cache")
                if cache and not permanently_blocked:
                    _store_cached_fills(
                        db,
                        row["addr"],
                        cache.get("fills") or (),
                        int(cache["window_start"]),
                        coverage_complete=bool(cache.get("coverage_complete")),
                        coverage_end=cache.get("coverage_end"),
                        universe=cache.get("universe"),
                        invalid_tids=invalid_cache_tids.get(row["addr"], ()),
                    )
                    cursor = cache.get("backfill_cursor")
                    if cursor is not None:
                        db.execute(
                            "INSERT INTO fill_cache_state"
                            "(addr,backfill_start_ms,backfill_cursor_ms,updated_at) "
                            "VALUES (?,?,?,?) ON CONFLICT(addr) DO UPDATE SET "
                            "backfill_start_ms=excluded.backfill_start_ms,"
                            "backfill_cursor_ms=MAX("
                            "COALESCE(fill_cache_state.backfill_cursor_ms,0),"
                            "excluded.backfill_cursor_ms),updated_at=excluded.updated_at",
                            (
                                row["addr"],
                                int(cache.get("backfill_start") or cache["window_start"]),
                                int(cursor),
                                now_iso(),
                            ),
                        )
                if "episodes" in artifact and not permanently_blocked:
                    _replace_episode_rows(db, row["addr"], artifact.get("episodes") or [])
                db.execute(
                    f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [row.get(column) for column in cols],
                )
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        for row in pending:
            row.pop("_profile_persist", None)
    return len(pending)


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
    return normalize_copyable_fills(
        [
            fill for fill in _load_cached_fills(db, addr, start_ms)
            if int(fill.get("time") or 0) <= int(now_ms)
        ],
        addr=addr,
    )


def _complete_cached_profile_fills(db, addr, window_start, asof_ms, *, universe=None):
    """Return a frozen cache window only when its transport proof is complete.

    A quiet wallet can legitimately have no fill near either boundary, so the
    first/last retained fill cannot prove coverage.  ``fill_cache_state`` owns
    that proof.  Returning ``None`` means the caller must fetch a bounded delta
    (or fail closed in explicitly offline mode).
    """
    coverage = db.execute(
        "SELECT coverage_start_ms,coverage_end_ms,backfill_cursor_ms "
        "FROM fill_cache_state WHERE lower(addr)=lower(?)",
        (addr,),
    ).fetchone()
    if (
        not coverage
        or int(coverage[0] or 0) > int(window_start)
        or int(coverage[1] or 0) < int(asof_ms)
        or int(coverage[2] or 0) > 0
    ):
        return None
    return normalize_copyable_fills(
        [
            fill for fill in _load_cached_fills(db, addr, window_start)
            if int(fill.get("time") or 0) <= int(asof_ms)
        ],
        addr=addr,
        universe=universe,
    )


def _fetch_profile_fills(db, addr, window_start, p, full, *, universe=None,
                         defer_persist=False):
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
                result = (scoped_full, False, scoped_delta, False)
                return (*result, None) if defer_persist else result
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
    cache_cursor = (
        {"backfill_start": int(resume_start), "backfill_cursor": int(next_cursor)}
        if hit_cap else None
    )
    if hit_cap and not defer_persist:
        with _db_lock:
            db.execute(
                "INSERT INTO fill_cache_state(addr,backfill_start_ms,backfill_cursor_ms,updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(addr) DO UPDATE SET "
                "backfill_start_ms=excluded.backfill_start_ms,"
                "backfill_cursor_ms=MAX("
                "COALESCE(fill_cache_state.backfill_cursor_ms,0),excluded.backfill_cursor_ms),"
                "updated_at=excluded.updated_at",
                (addr, resume_start, int(next_cursor), now_iso()),
            )
            db.commit()
    result = (scoped_full, hit_cap, scoped_delta, True)
    return (*result, cache_cursor) if defer_persist else result


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


def _scanner_db_path(db):
    """Return the on-disk database path used by a separate heartbeat writer."""
    try:
        row = db.execute("PRAGMA database_list").fetchone()
        path = row[2] if row else None
    except sqlite3.Error:
        return None
    return str(path) if path and str(path) != ":memory:" else None


def _write_scanner_heartbeat(db_path) -> bool:
    """Refresh scanner liveness without sharing the long-running scan connection.

    This deliberately updates one existing row and never appends history.  A busy
    writer is skipped quickly; the next minute retries without delaying replay.
    """
    if not db_path:
        return False
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=0.25)
        conn.execute("PRAGMA busy_timeout=250")
        progress = conn.execute(
            "SELECT state,stage,candidates_scanned,candidates_total "
            "FROM scan_progress WHERE id=1"
        ).fetchone()
        if not progress or str(progress[0] or "") != "scanning":
            return False
        detail = {
            "stage": progress[1],
            "scanned": int(progress[2] or 0),
            "total": int(progress[3] or 0),
        }
        changed = conn.execute(
            "UPDATE process_status SET pid=?,heartbeat_at=?,detail_json=? "
            "WHERE name='scanner' AND state='scanning'",
            (os.getpid(), now_iso(), json.dumps(detail, sort_keys=True)),
        ).rowcount
        conn.commit()
        return bool(changed)
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


class _ScannerHeartbeat:
    """Best-effort minute heartbeat for CPU-heavy path/tuning phases."""

    def __init__(self, db, interval_s=_SCANNER_HEARTBEAT_INTERVAL_S):
        self.db_path = _scanner_db_path(db)
        self.interval_s = max(0.01, float(interval_s))
        self.stop_event = threading.Event()
        self.thread = None

    def __enter__(self):
        if not self.db_path:
            return self

        def run():
            while not self.stop_event.wait(self.interval_s):
                _write_scanner_heartbeat(self.db_path)

        self.thread = threading.Thread(
            target=run, name="scanner-heartbeat", daemon=True,
        )
        self.thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=min(1.0, self.interval_s))


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
    week_perp_volume_min = getattr(
        p, "week_vlm_min", config.HARVEST_WEEK_VLM_MIN,
    )
    cache_policy = {
        "version": "official_perp_week_volume_v10",
        "weekPerpVolumeMin": float(week_perp_volume_min),
    }
    addr_set = {str(addr).lower() for addr in addrs}
    blocked = collection_blacklist.active_map(db)
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
        normalized_addr = str(addr).lower()
        blocked_reason = blocked.get(normalized_addr)
        result = (
            perp_prefilter.Result(
                "rejected",
                blocked_reason,
                {"scanResolution": {"source": "collection_blacklist"}},
            )
            if blocked_reason else cached_results.get(normalized_addr)
        )
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


def _copyable_perp_week_volume(fills, now_ms: int) -> float:
    """Return exact seven-day notional from the already-scoped executable fill cache.

    ``candidate_fills`` contains only executable Perp contracts.  Reaching the official Perp-volume
    floor on this narrower surface is therefore a one-way proof that the exchange-wide ``perpWeek``
    volume also reaches the floor.  Falling below the floor is *not* a rejection proof because the
    source may also trade a non-executable builder namespace; callers must fall back to Portfolio.
    """
    cutoff = int(now_ms) - 7 * 86_400_000
    return sum(
        abs(f(fill.get("px")) * f(fill.get("sz")))
        for fill in (fills or ())
        if int(fill.get("time") or 0) >= cutoff
    )


def _with_prefilter_resolution(result, *, source: str, local_week_volume=None):
    windows = dict((result.windows if result is not None else {}) or {})
    windows["scanResolution"] = {
        "source": str(source),
        "localCopyablePerpWeekVolume": (
            float(local_week_volume) if local_week_volume is not None else None
        ),
    }
    return perp_prefilter.Result(result.status, result.reason, windows)


def _resolve_profile_perp_prefilter(addr, perp_fills, now_ms, p, existing=None):
    """Resolve the volume-only official gate after cached structure is known.

    Complete-cache wallets no longer pay a Portfolio request before the latest delta can reject an
    unchanged HFT/grid/DCA structure.  A local executable-volume pass is a sufficient proof; every
    inconclusive local result preserves the old official Portfolio fallback and its exact decision.
    """
    if existing is not None:
        return _with_prefilter_resolution(existing, source="eager_or_retention")
    week_floor = float(getattr(p, "week_vlm_min", config.HARVEST_WEEK_VLM_MIN))
    local_volume = _copyable_perp_week_volume(perp_fills, int(now_ms))
    if local_volume >= week_floor:
        return perp_prefilter.Result(
            "passed",
            "copyable_perp_week_volume_proven",
            {
                "week": {
                    "perpVlm": local_volume,
                    "hardGate": True,
                    "auditStatus": "local_executable_volume_proof",
                },
                "scanResolution": {
                    "source": "local_copyable_volume",
                    "localCopyablePerpWeekVolume": local_volume,
                },
            },
        )
    try:
        payload = rest.portfolio(addr)
    except Exception as exc:  # noqa: BLE001
        result = perp_prefilter.Result(
            "deferred_data_error", f"portfolio_error:{type(exc).__name__}", {},
        )
    else:
        result = perp_prefilter.evaluate(payload, min_week_perp_volume=week_floor)
    return _with_prefilter_resolution(
        result, source="portfolio_fallback", local_week_volume=local_volume,
    )


def _official_profile_fields(result) -> dict:
    result = result or perp_prefilter.Result(
        "deferred_data_error", "perp_prefilter_result_missing", {},
    )
    official_month = dict((result.windows or {}).get("month") or {})
    official_return = dict((result.windows or {}).get("officialPerp30d") or {})
    return {
        "official_perp_status": result.status,
        "official_perp_reason": result.reason,
        "official_perp_evidence_json": json.dumps(
            result.payload(), sort_keys=True, separators=(",", ":"),
        ),
        "official_perp_return_30d": official_return.get("return"),
        "official_perp_pnl_30d": official_month.get("perpPnl"),
        "official_perp_pnl_share": official_month.get("perpShare"),
    }


def _profile_official_result(row):
    status = str((row or {}).get("official_perp_status") or "")
    if not status:
        return None
    try:
        payload = json.loads((row or {}).get("official_perp_evidence_json") or "{}")
    except (TypeError, ValueError):
        payload = {}
    return perp_prefilter.Result(
        status,
        str((row or {}).get("official_perp_reason") or "perp_prefilter_result_missing"),
        dict(payload.get("windows") or {}),
    )


def _record_perp_prefilter_results(db, addrs, results, stamp, *, p=None, source="scan") -> None:
    """Seal eager, local-proof, fallback and structural-skip decisions into one audit surface."""
    pipeline_audit._delete_stage(db, stamp, source, "perp_prefilter")
    week_floor = float(getattr(p, "week_vlm_min", config.HARVEST_WEEK_VLM_MIN))
    for rank, addr in enumerate(addrs, 1):
        result = results.get(addr) or perp_prefilter.Result(
            "deferred_data_error", "perp_prefilter_result_missing", {},
        )
        pipeline_audit._insert_event(
            db,
            stamp=stamp,
            source=source,
            stage="perp_prefilter",
            addr=addr,
            rank=rank,
            status=result.status,
            reason=result.reason,
            payload={
                **result.payload(),
                "policy": {
                    # Keep the decision-policy version stable: fill-first changes transport/order only. A
                    # fresh local executable-volume proof satisfies the same volume gate and remains safe for
                    # the existing short-TTL restart/daily cache.
                    "version": "official_perp_week_volume_v10",
                    "weekPerpVolumeMin": week_floor,
                },
                "cacheHit": False,
            },
        )
    db.commit()


def _perp_prefilter_resolution_counts(results, addrs) -> dict:
    counts = {}
    for addr in addrs:
        result = results.get(addr)
        resolution = dict(((result.windows if result else {}) or {}).get("scanResolution") or {})
        source = str(resolution.get("source") or "eager_portfolio")
        counts[source] = counts.get(source, 0) + 1
    return counts


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


def _copy_profile_evidence(m, results, p):
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


def _profile_copy_qualification(m, p) -> tuple[bool, str]:
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


def _defer_profile(db, addr, prior, stamp, reason, *, generation_id=None, persist=True):
    """Persist a tri-state data error while preserving the last usable market snapshot."""
    reason = str(reason or "data_error")[:120]
    if prior:
        m = dict(prior)
        m.update(
            addr=addr,
            data_status="deferred_data_error",
            evidence_status="invalid",
            evaluated_at=stamp,
            reason=reason,
        )
        if generation_id:
            m["profile_generation"] = generation_id
        _queue_profile_persist(m)
        if persist:
            _persist_profile_batch(db, [m])
        return (prior.get("status") or "quarantine"), reason, m, False
    row = {
        "addr": addr,
        "status": "quarantine",
        "reason": reason,
        "score": 0.0,
        "raw_quality_score": 0.0,
        "data_status": "deferred_data_error",
        "evidence_status": "invalid",
        "profile_generation": generation_id,
        "evaluated_at": stamp,
        "times_seen": 1,
        "times_active": 0,
    }
    _queue_profile_persist(row)
    if persist:
        _persist_profile_batch(db, [row])
    return "quarantine", reason, row, False


def _reject_prefilter_profile(db, addr, prior, stamp, generation_id, reason, *, persist=True):
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
    _queue_profile_persist(row)
    if persist:
        _persist_profile_batch(db, [row])
    return row["status"], row["reason"], row, False


def _defer_official_evidence_profile(db, addr, prior, stamp, generation_id, gate, *, persist=True):
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
    _queue_profile_persist(row)
    if persist:
        _persist_profile_batch(db, [row])
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


def _profile_one(db, addr, now_ms, p, prior, lb, stamp, universe, force_full=False,
                 persist=None):
    # ONE aggregated fetch per wallet (aggregateByTime -> ~1 page, trade-level). No separate
    # pre-screen call: the response crosses the executable-market boundary before cache/metrics,
    # and gates reject dormant/no-copyable-contract evidence on that same scoped data.
    # Fetch a LONG window (PROFILE_FETCH_DAYS) via the paginated fetch_window — it sorts ASCENDING and
    # caps at max_pages*2000 fills (NOT a single 2000-row page: user_fills_latest truncated active wallets
    # at 2000 AND returned newest-first unsorted, which broke window_days/trades_per_day/last_fill_ms and
    # over-rejected as hit_page_cap). We slice the 14d window for the existing scoring metrics (behaviour
    # unchanged) and use the full fetch for the multi-window / lifetime nets — still ONE fetch per wallet.
    persist = (
        not bool(getattr(p, "defer_profile_persist", False))
        if persist is None else bool(persist)
    )
    blocked_reason = collection_blacklist.reason_for(db, addr)
    if blocked_reason:
        return _reject_prefilter_profile(
            db,
            str(addr).lower(),
            prior,
            stamp,
            getattr(p, "scan_generation", None),
            blocked_reason,
            persist=persist,
        )
    if not universe:
        return _defer_profile(
            db, addr, prior, stamp, "universe_unavailable",
            generation_id=getattr(p, "scan_generation", None),
            persist=persist,
        )
    window_start = now_ms - config.PROFILE_FETCH_DAYS * 86400_000
    # Workset scope and fill-fetch mode are independent.  A UI "full scan" may evaluate every candidate
    # while only the scheduler-selected migration/repair wallets perform a complete historical refetch.
    full = bool(force_full or not config.INCREMENTAL_SCAN)
    try:
        cached = (
            _complete_cached_profile_fills(
                db, addr, window_start, now_ms, universe=universe,
            )
            if getattr(p, "prefer_complete_profile_cache", False) else None
        )
        if cached is not None:
            fetched = (cached, False, [], False, None) if not persist else (
                cached, False, [], False,
            )
        elif getattr(p, "profile_cache_only", False):
            raise RuntimeError("profile_fill_cache_incomplete")
        else:
            fetched = _fetch_profile_fills(
                db, addr, window_start, p, full, universe=universe,
                defer_persist=not persist,
            )
        if persist:
            raw_full, hit_cap, new_fills, _fetched_full_window = fetched
            cache_cursor = None
        else:
            raw_full, hit_cap, new_fills, _fetched_full_window, cache_cursor = fetched
    except Exception as exc:  # noqa: BLE001 - network failures are a first-class deferred outcome
        outcome = _defer_profile(
            db, addr, prior, stamp, f"fills_error:{type(exc).__name__}",
            generation_id=getattr(p, "scan_generation", None), persist=False,
        )
        deferred = outcome[2]
        deferred.update(_official_profile_fields(perp_prefilter.Result(
            "deferred_data_error", "fills_unavailable_before_perp_prefilter", {},
        )))
        if persist:
            _persist_profile_batch(db, [deferred])
        return outcome
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
    m = metrics.compute_metrics(perp, eps, p.days)
    if m is None:
        m = {"n_fills": len(perp), "n_trades": 0, "window_days": 0, "trades_per_day": 0,
             "taker_frac_notl": 0, "median_hold_s": 0, "win_rate": 0, "net_pnl": 0,
             "total_notl": 0, "top_coin": None, "max_drawdown": 0, "avg_notional": 0, "hold_skew": 0,
             "last_fill_ms": perp[-1]["time"] if perp else 0, "active_days": 0, "activity_ratio": 0,
             "median_eps": 0, "pos_day_ratio": 0, "profit_conc": 0,
             "max_adds_per_ep": 0, "median_adds_per_ep": 0, "worst_loss": 0.0,
             "retry_transition_n": 0, "rapid_same_side_retry_n": 0,
             "rapid_same_side_retry_rate": 0.0, "loss_retry_transition_n": 0,
             "rapid_loss_retry_n": 0, "rapid_loss_retry_rate": 0.0,
             "rapid_retry_chain_n": 0, "rapid_retry_max_chain_episodes": 0,
             "loss_started_retry_chain_n": 0, "loss_started_retry_chain_losing_n": 0,
             "loss_started_retry_chain_lose_rate": 0.0,
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

    # STAGE A — cheap structural copyability (NO api). Front-of-funnel rejects (MM/HFT/grid/spot) that do
    # NOT kill a genuine trend trader. n_trades==0 (pure-hold) skips the episode-based checks → judged on
    # live positions in stage B. (Old behaviour auto-rejected n_trades==0 as 'no_closed_episode'.)
    sector_structure = _current_sector_structure_policy(perp_full, p)
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
        status, deferred_reason, deferred, _ = _defer_profile(
            db, addr, prior, stamp, "hit_page_cap",
            generation_id=getattr(p, "scan_generation", None),
            persist=False,
        )
        deferred.update(_official_profile_fields(perp_prefilter.Result(
            "deferred_data_error", "fill_cache_incomplete_before_perp_prefilter", {},
        )))
        _queue_profile_persist(
            deferred,
            cache={
                "fills": new_fills,
                "window_start": window_start,
                "coverage_complete": False,
                "coverage_end": now_ms,
                "universe": universe,
                **dict(cache_cursor or {}),
            },
        )
        if persist:
            _persist_profile_batch(db, [deferred])
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

    # Resolve the volume-only Perp gate only after the refreshed cache has had a chance to terminate a
    # structural reject. New/incomplete-cache wallets already carry an eager official result; complete-cache
    # wallets use a one-way local proof and fall back to Portfolio whenever that proof is inconclusive.
    if official is None and not ok:
        official = perp_prefilter.Result(
            "skipped",
            "structural_rejected_before_perp_prefilter",
            {
                "scanResolution": {
                    "source": "structural_short_circuit",
                    "structuralReason": reason,
                },
            },
        )
    elif ok:
        official = _resolve_profile_perp_prefilter(
            addr, perp_full, now_ms, p, existing=official,
        )
    m.update(_official_profile_fields(official))
    # Any later deferred-data path copies these current-generation official fields instead of reviving stale
    # evidence from the prior profile.
    prior = {**(prior or {}), **_official_profile_fields(official)}
    if ok and official.deferred:
        status, deferred_reason, deferred, _ = _defer_profile(
            db, addr, prior, stamp, official.reason,
            generation_id=getattr(p, "scan_generation", None), persist=False,
        )
        _queue_profile_persist(
            deferred,
            cache={
                "fills": new_fills,
                "window_start": window_start,
                "coverage_complete": True,
                "coverage_end": now_ms,
                "universe": universe,
            },
            episodes=eps,
        )
        if persist:
            _persist_profile_batch(db, [deferred])
        return status, deferred_reason, deferred, False
    if ok and not official.passed:
        ok, reason = False, official.reason

    # STAGE B — fetch the LIVE open-position snapshot (un-blinds the funnel to held positions), fold in
    # realized+unrealized roi, then re-judge: held position = ACTIVE, 扛单 bags drag roi_total negative,
    # trend holders kept. Only structural survivors pay the extra clearinghouse call.
    if ok:
        dexes = {(c.split(":")[0] if ":" in c else None) for c in {x["coin"] for x in perp}}
        snap = _open_snapshot(addr, dexes, open_eps, now_ms, acct_value, universe=universe)
        if snap is None:
            return _defer_profile(
                db, addr, prior, stamp, "clearinghouse_unavailable",
                generation_id=getattr(p, "scan_generation", None),
                persist=persist,
            )
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
        ok, reason = metrics.gates_state(m)
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
            return _defer_profile(
                db, addr, prior, stamp, str(exc),
                generation_id=getattr(p, "scan_generation", None),
                persist=persist,
            )
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
        _copy_profile_evidence(m, evidence_results, p)
        if (
            not sector_policy.get("allowed")
            and not sector_policy.get("watch")
            and m.get("evidence_status") not in {"missing", "invalid"}
        ):
            m["evidence_status"] = "economically_disqualified"
        if m.get("data_status") == "deferred_data_error":
            return _defer_profile(
                db, addr, prior, stamp, "copy_replay_unavailable",
                generation_id=getattr(p, "scan_generation", None),
                persist=persist,
            )
        if ok:
            _attach_open_copy_activity_context(
                m, addr, getattr(p, "open_copy_pnl_by_addr", {}),
            )
            ok, reason = _profile_copy_qualification(m, p)
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
    _queue_profile_persist(
        row,
        cache={
            "fills": new_fills,
            "window_start": window_start,
            # A delta fetch is only attempted from an already-complete cache. A successful response
            # therefore preserves that proof and advances its source cursor even when it contains no
            # in-scope fills. This avoids repeatedly downloading the same quiet/excluded-market interval.
            "coverage_complete": not hit_cap,
            "coverage_end": now_ms,
            "universe": universe,
        },
        episodes=eps,
    )
    if persist:
        _persist_profile_batch(db, [row])
    return status, reason, row, hit_cap


# ------------------------------------------------------------------ curated outputs
def refresh_watchlist(db, stamp, *, leaderboard_generation=None, commit=True) -> int:
    """Rebuild OUR tiny leaderboard (watchlist) from active profiles. Derived view —
    profile stays the source of truth; operator settings in target_controls survive.
    """
    if commit:
        params.seed_params(db)
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
    ranked = []
    for r in rows:
        score, detail = follow_score.compute_follow_score(r)
        detail = dict(detail or {})
        eligibility = follow_score.evaluate_follow_eligibility(r)
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
    retain_advisory_incumbents=False,
):
    """Publish the requested profit-ordered Core prefix.

    There is no minimum count, promotion delay, or incumbent tenure. Daily effective
    publication may explicitly retain advisory low/medium incumbents; structural,
    unavailable, and high-risk wallets still fail closed. Open copies are handled by
    the caller as Exit-only and never justify Core authority.
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
        retention_class = core_retention.qualification_failure(qualification)[0]
        retained_advisory = bool(
            retain_advisory_incumbents
            and addr in previous_core
            and addr in desired
            and retention_class in {core_retention.HEALTHY, "soft", "medium"}
        )
        core_ok = (
            row.get("status") in {"active", "qualified"}
            and (_formation_core_permission(qualification) or retained_advisory)
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


def _portfolio_selection_metrics(windows, selected_n=0):
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


_FORMATION_PREFIX_CACHE_POLICY = "count-first-local-surface-v1"


def _formation_prefix_membership_hash(addrs) -> str:
    return hashlib.sha256(
        "\n".join(sorted({str(addr or "").lower() for addr in addrs if addr})).encode("utf-8")
    ).hexdigest()


def _load_formation_prefix_evidence(db, generation_id, params_hash, addrs):
    membership_hash = _formation_prefix_membership_hash(addrs)
    # Early count-first builds accidentally stored the complete per-window
    # replay trajectories under individual evidence.  The formation contract
    # only consumes the compact effective metrics/qualification.  Strip that
    # legacy branch inside SQLite before it crosses into Python so resuming 16
    # rows cannot recreate hundreds of MiB of discarded objects.
    replay_expression = (
        "json_remove(replay_json,'$.effective.results')"
        if str(params_hash).startswith("individual:") else "replay_json"
    )
    row = db.execute(
        f"SELECT evaluation_json,{replay_expression} FROM formation_prefix_evidence "
        "WHERE generation=? AND policy_version=? AND params_hash=? AND membership_hash=?",
        (
            generation_id, _FORMATION_PREFIX_CACHE_POLICY,
            str(params_hash), membership_hash,
        ),
    ).fetchone()
    if not row:
        return None
    try:
        raw = json.loads(row[0] or "{}")
        replay = json.loads(row[1] or "{}")
        payload = dict(raw.get("payload") or {})
        if str(params_hash).startswith("final-shared:"):
            # Final count search and atomic publication must use the same
            # execution contract.  Historical final-shared rows already carry
            # open/capacity metrics, so tighten their interpretation in place
            # instead of replaying an otherwise identical surface.
            payload["requireCongestionFit"] = True
        value = core_formation.PrefixEvaluation(
            count=int(raw["count"]),
            net_pnl=f(raw.get("netPnl")),
            stress_net_pnl=f(raw.get("stressNetPnl")),
            max_drawdown=f(raw.get("maxDrawdown")),
            actionable_open_rate=f(raw.get("actionableOpenRate")),
            capacity_fit=f(raw.get("capacityFit")),
            liquidations=int(raw.get("liquidations") or 0),
            params=dict(raw.get("params") or {}),
            payload=payload,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if value.count != len({str(addr or "").lower() for addr in addrs if addr}):
        return None
    return value, replay


def _store_formation_prefix_evidence(
    db, generation_id, params_hash, addrs, evaluation, replay,
) -> None:
    membership_hash = _formation_prefix_membership_hash(addrs)
    stamp = now_iso()
    payload = {
        "count": int(evaluation.count),
        "netPnl": f(evaluation.net_pnl),
        "stressNetPnl": f(evaluation.stress_net_pnl),
        "maxDrawdown": f(evaluation.max_drawdown),
        "actionableOpenRate": f(evaluation.actionable_open_rate),
        "capacityFit": f(evaluation.capacity_fit),
        "liquidations": int(evaluation.liquidations),
        "params": dict(evaluation.params or {}),
        "payload": dict(evaluation.payload or {}),
    }
    db.execute(
        "INSERT INTO formation_prefix_evidence "
        "(generation,policy_version,params_hash,membership_hash,member_count,"
        "evaluation_json,replay_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT("
        "generation,policy_version,params_hash,membership_hash"
        ") DO UPDATE SET member_count=excluded.member_count,"
        "evaluation_json=excluded.evaluation_json,replay_json=excluded.replay_json,"
        "updated_at=excluded.updated_at",
        (
            generation_id, _FORMATION_PREFIX_CACHE_POLICY, str(params_hash),
            membership_hash, int(evaluation.count),
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=float),
            json.dumps(replay or {}, sort_keys=True, separators=(",", ":"), default=float),
            stamp, stamp,
        ),
    )
    db.commit()


def _paper_account_equity(db) -> float:
    """Current selected-ledger equity used for publication-scale replay."""
    book = selected_book(db)
    if book.mode == "live":
        # live_copy_account.balance is exchange-authoritative total equity and already includes uPnL.
        row = db.execute(f"SELECT balance FROM {book.account} WHERE id=1").fetchone()
    else:
        row = db.execute(
            f"SELECT ca.balance + COALESCE(("
            f"SELECT SUM(COALESCE(cp.unrealized_pnl,0)) FROM {book.position} cp "
            "WHERE cp.status='open'"
            f"),0) FROM {book.account} ca WHERE ca.id=1"
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


def _quality_core_profiles(
    db, generation_id, *, core_only=True, now_ms=None, retention_addrs=None,
) -> list[dict]:
    """Current-generation follow-quality profiles in immutable quality order.

    ``core_only=False`` returns the bounded Core+Challenger workset needed for final-parameter
    requalification; the default preserves the original Core-ready contract for callers/tests.
    """
    position_table = _execution_position_table(db)
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
        "WHERE p.profile_generation=? AND (pse.queue_rank IS NOT NULL OR EXISTS ("
        "SELECT 1 FROM scan_generation sg JOIN follow_selection fs "
        "ON fs.generation=sg.generation "
        "WHERE sg.status='published' AND sg.complete=1 AND sg.is_current=1 "
        "AND fs.role='core' AND COALESCE(fs.enabled,1)=1 "
        "AND lower(fs.addr)=lower(p.addr)))",
        (generation_id, generation_id),
    )
    names = [desc[0] for desc in cur.description]
    blocked_risk = {
        (addr or "").lower()
        for addr, level, block in db.execute(
            "SELECT addr,risk_level,risk_block_reason FROM wallet_registry"
        ).fetchall()
        if level == wallet_risk.HIGH
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
            f"SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END) FROM {position_table} GROUP BY lower(addr)"
        ).fetchall()
    }
    rows = []
    current_core = {
        str(addr or "").lower() for addr in (
            selection.published_core_membership(db)
            if retention_addrs is None else retention_addrs
        ) or () if addr
    }
    follow_values = params.load_follow(db)
    policy_values = {**follow_values, **params.load_category(db, "scanner")}
    for raw in cur.fetchall():
        row = dict(zip(names, raw))
        addr = (row.get("addr") or "").lower()
        row["addr"] = addr
        row["retention_lane"] = addr in current_core
        row.update(forward_risk.get(addr) or {})
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
        retention_candidate = bool(
            row["retention_lane"]
            and (row.get("data_status") or "valid") == "valid"
            and row.get("status") in {"active", "qualified"}
        )
        stage_passed = rough_passed or retention_candidate
        row["follow_qualification"] = {
            "eligible": stage_passed,
            "coreEligible": stage_passed,
            "stageEligible": stage_passed,
            "stage": "rough",
            "status": (
                "pre_strict_qualified" if rough_passed
                else "core_retention_lane" if retention_candidate
                else row.get("pre_strict_first_failure") or "pre_strict_not_qualified"
            ),
            "firstFailure": None if rough_passed else row.get("pre_strict_first_failure"),
            "role": "core_eligible" if stage_passed else "challenger",
            "deferred": False,
            "checks": {
                "frozenRoughCopyPassed": rough_passed,
                **({"coreRetentionLane": True} if retention_candidate else {}),
            },
            "reasons": [] if stage_passed else [
                str(row.get("pre_strict_first_failure") or "pre_strict_not_qualified")
            ],
        }
        qualified = (
            row.get("status") in {"active", "qualified"}
            and (row.get("follow_qualification") or {}).get("coreEligible")
        )
        if (
            qualified
            and (not core_only or rough_passed)
            and (row.get("data_status") or "valid") == "valid"
            and addr not in blocked_risk
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
    _copy_profile_evidence(effective, results, replay_ctx)
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
        policy_values=follow,
    )
    sector_policy_json = effective.get("sector_policy_json")
    # The final formation needs normalized scalar economics, not another copy of the complete sector replay.
    # ``sector_copy_json`` contains every 30/14/7 position and can be tens of MiB for one high-activity wallet.
    # Keeping it in each of the sixteen winning-surface metric rows made RSS grow monotonically throughout
    # individual strict replay and eventually pushed the 2-GiB VPS into Swap.  The sector-scoped fields above
    # have already been materialized, and the policy travels through its dedicated return field.
    for heavy_key in (
        "sector_copy_json", "pre_strict_activity_json", "official_perp_evidence_json",
    ):
        scoring_metrics.pop(heavy_key, None)
    return {
        "metrics": scoring_metrics,
        "qualification": qualification,
        "score": score,
        "scoreDetail": _detail,
        "sectorPolicyJson": sector_policy_json,
        "results": results,
    }


def _init_strict_replay_process(context: dict) -> None:
    global _STRICT_REPLAY_PROCESS_CONTEXT
    db_path = str(context["db_path"])
    read_db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    read_db.row_factory = sqlite3.Row
    _STRICT_REPLAY_PROCESS_CONTEXT = {**context, "db": read_db}


def _strict_replay_process(task):
    row, strict_path, qualification_stage = task
    context = _STRICT_REPLAY_PROCESS_CONTEXT
    return _effective_follow_replay(
        context["db"], row, context["now_ms"],
        generation_id=context["generation_id"],
        follow=context["follow"],
        valuation_marks=context["valuation_marks"],
        sigmas=context["sigmas"],
        market_ctx=context["market_ctx"],
        strict_path=bool(strict_path),
        qualification_stage=str(qualification_stage),
    )


def _parallel_effective_follow_replays(
    db,
    rows,
    now_ms,
    *,
    generation_id,
    follow,
    valuation_marks,
    sigmas,
    market_ctx,
    strict_path=True,
    qualification_stage="strict",
) -> list[dict]:
    """Replay independent wallets on CPU-count-aware read-only workers in stable score order."""
    rows = list(rows)
    if len(rows) <= 1 or replay_parallel.effective_worker_count(len(rows)) <= 1:
        return [
            _effective_follow_replay(
                db, row, now_ms, generation_id=generation_id, follow=follow,
                valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
                strict_path=strict_path, qualification_stage=qualification_stage,
            )
            for row in rows
        ]
    db_path = next(
        (path for _seq, name, path in db.execute("PRAGMA database_list") if name == "main"),
        "",
    )
    if not db_path:
        return [
            _effective_follow_replay(
                db, row, now_ms, generation_id=generation_id, follow=follow,
                valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
                strict_path=strict_path, qualification_stage=qualification_stage,
            )
            for row in rows
        ]
    context = {
        "db_path": db_path,
        "now_ms": int(now_ms),
        "generation_id": generation_id,
        "follow": dict(follow),
        "valuation_marks": dict(valuation_marks or {}),
        "sigmas": dict(sigmas or {}),
        "market_ctx": dict(market_ctx or {}),
    }
    tasks = [
        (row, bool(strict_path), str(qualification_stage))
        for row in rows
    ]
    return replay_parallel.map_ordered(
        _strict_replay_process,
        tasks,
        initializer=_init_strict_replay_process,
        initargs=(context,),
    )


def _source_quality_pool(db, generation_id: str) -> tuple[list[str], list[str]]:
    """Return every structurally valid deep-fill profile for fills-only pre-strict evaluation."""
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
    "copy_single_liquidation_loss_over_8pct",
    # Compatibility: old rows are filtered by loss_pct below.  Verified 8%+
    # rows remain permanent; 5-8% rows remain audit-only.
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
    if event_type in {
        "copy_single_liquidation_loss_over_8pct",
        "source_account_liquidated_zero",
    }:
        stamp = now_iso()
        db.execute(
            "INSERT INTO execution_wallet_safety "
            "(addr,state,event_key,occurred_at,reason,evidence_json,first_seen_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(addr) DO UPDATE SET "
            "state='confirmed',event_key=excluded.event_key,"
            "occurred_at=COALESCE(excluded.occurred_at,execution_wallet_safety.occurred_at),"
            "reason=excluded.reason,evidence_json=excluded.evidence_json,"
            "updated_at=excluded.updated_at",
            (
                str(addr or "").lower(), "confirmed", str(event_key),
                int(occurred_at) if occurred_at is not None else None,
                str(event_type),
                json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True, default=float),
                stamp, stamp,
            ),
        )


def _historical_major_liquidation_addrs(db, addrs=None) -> set[str]:
    clauses = [
        f"event_type IN ({','.join('?' for _ in _MAJOR_LIQUIDATION_EVENT_TYPES)})",
        "(event_type!='copy_single_liquidation_loss_over_5pct' "
        "OR loss_pct IS NULL OR loss_pct>=?)",
    ]
    args = list(_MAJOR_LIQUIDATION_EVENT_TYPES)
    args.append(float(config.COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT))
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
        "ORDER BY COALESCE(p.rough_copy_score,p.score,0) DESC,"
        "CASE pse.tier WHEN 'primary' THEN 0 WHEN 'reserve' THEN 1 ELSE 2 END,"
        "pse.rough_profit_priority DESC,pse.rough_return_30d DESC,pse.rough_return_7d DESC,"
        "pse.copy_profit_factor_30d DESC,"
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
    if hasattr(p, "copy_bt_valuation_marks"):
        valuation_marks = dict(getattr(p, "copy_bt_valuation_marks") or {})
    else:
        valuation_marks = _current_copy_valuation_marks()
    incumbent_core = set(selection.published_core_membership(db) or ())
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
        qualification = pre_strict.evaluate(
            effective, activity, stage="rough", policy_values=follow,
        )
        qualification["copyProfitFactor"] = f(effective.get("copy_bt_profit_factor"))
        retention_soft_failure = bool(
            addr in incumbent_core
            and core_retention.qualification_failure(qualification)[0] == "soft"
        )
        row.update(effective)
        row.update(
            status=(
                "active"
                if qualification.get("eligible")
                or qualification.get("deferred")
                or retention_soft_failure
                else "rejected"
            ),
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
        if qualification.get("firstFailure") == "copy_single_liquidation_loss_over_8pct":
            loss_pct = f(effective.get("copy_bt_max_liquidation_loss_pct"))
            loss_usd = f(effective.get("copy_bt_max_liquidation_loss"))
            coin = str(effective.get("copy_bt_max_liquidation_loss_coin") or "")
            closed_at = int(f(effective.get("copy_bt_max_liquidation_loss_closed_at")))
            _record_wallet_risk_event(
                db, addr, "copy_single_liquidation_loss_over_8pct",
                f"{coin or 'unknown'}:{closed_at or 'unknown'}",
                occurred_at=closed_at or None,
                coin=coin or None,
                loss_usd=loss_usd,
                loss_pct=loss_pct,
                evidence={
                    "generation": generation_id,
                    "stage": "rough",
                    "thresholdPct": (
                        load_copy_policy(follow).catastrophic_liquidation_loss_pct
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
                compact=True,
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
                and open_loss_ratio_within_limit(primary_economics)
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
            "paperReturn30d": return_30d if qualified else float("-inf"),
            "paperReturn7d": return_7d if qualified else float("-inf"),
            "paperBasis": "standardized_projection",
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
    """Return one frozen Top-N pool, with retained Core consuming normal seats."""
    candidates = [
        row for row in rows if _formation_prepath_candidate(row)
    ]
    candidates.sort(key=lambda row: (
        int(row.get("pre_strict_queue_rank") or 999999),
        row.get("addr") or "",
    ))
    retention = [row for row in candidates if row.get("retention_lane")]
    retained_addrs = {row.get("addr") for row in retention}
    entrants = [row for row in candidates if row.get("addr") not in retained_addrs]
    limit = max(0, int(limit))
    retained = retention[:limit]
    return retained + entrants[:max(0, limit - len(retained))]


def _core_prefix_retention() -> dict:
    return {
        "utility_retention": float(config.CORE_PREFIX_UTILITY_RETENTION),
        "net_retention": float(config.CORE_PREFIX_NET_RETENTION),
        "utility_slack": float(config.CORE_PREFIX_ABS_UTILITY_SLACK),
        "net_slack": float(config.CORE_PREFIX_ABS_NET_SLACK),
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
            "algorithm": "count_first_local_surface_v1",
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


def _retune_exact_membership_surface(
    db, addrs, candidate_rows, *, generation_id, stamp, round_index,
    now_ms, base_follow, valuation_marks, sigmas, market_ctx,
) -> dict:
    """Full-tune the exact proposed Core instead of inheriting a larger pool's congestion surface."""
    ordered_addrs = tuple(dict.fromkeys(
        str(addr or "").lower() for addr in (addrs or ()) if addr
    ))
    if not ordered_addrs:
        raise RuntimeError("core_formation_closure_empty_membership")
    row_by_addr = {
        str(row.get("addr") or "").lower(): row for row in (candidate_rows or ())
        if row.get("addr")
    }
    exact_rows = [row_by_addr[addr] for addr in ordered_addrs if addr in row_by_addr]
    if len(exact_rows) != len(ordered_addrs):
        raise RuntimeError("core_formation_closure_candidate_missing")
    window_fills = auto_tune._portfolio_window_fills(
        db, list(ordered_addrs), now_ms, include_watch=True,
    )
    if window_fills is None or not any(window_fills.values()):
        raise RuntimeError("core_formation_closure_fills_unavailable")
    full_run = auto_tune.maybe_tune_margins(
        db, source="core_formation_closure",
        stamp=f"{stamp}:closure:r{int(round_index)}:k{len(ordered_addrs)}",
        dry_run=True, mode="apply", follow_values=base_follow,
        data_complete=True, addrs_override=list(ordered_addrs),
        record_run=False, formation_admission=True,
        market_generation=generation_id, search_profile="efficient",
    )
    if full_run.get("status") != "ok":
        raise RuntimeError(
            "core_formation_closure_tune_failed:"
            + str(full_run.get("reason") or full_run.get("status"))
        )
    db.commit()
    finalist_surface, finalist_audit = _select_formation_finalist_surface(
        db, full_run, exact_rows,
        base_follow=base_follow, generation_id=generation_id,
        now_ms=now_ms, valuation_marks=valuation_marks,
        sigmas=sigmas, market_ctx=market_ctx, window_fills=window_fills,
    )
    chosen_run = {**full_run, "proposal": finalist_surface}
    tuned_params, eligible, reason = _formation_param_surface(
        base_follow, chosen_run, retune=True,
    )
    if eligible is not True:
        raise RuntimeError(f"core_formation_closure_not_eligible:{reason}")
    return {
        "addrs": ordered_addrs,
        "follow": {
            **base_follow,
            **tuned_params,
            "AMBIGUOUS_PATH_MODE": "liquidate",
        },
        "params": tuned_params,
        "eligible": eligible,
        "reason": reason,
        "run": chosen_run,
        "finalistAudit": finalist_audit,
    }


def form_quality_prefix(db, generation_id, stamp, now_ms=None, *, retune=True,
                        force_entry_requalification=False, force_retune=False,
                        retention_addrs=None, _follow_override=None) -> dict:
    """Certify wallets once, search fills quickly, then seal one final strict surface."""
    now_ms = int(now_ms or time.time() * 1000)
    resource_peak = {
        "processTreeRssBytes": 0,
        "processTreeSwapBytes": 0,
        "cgroupMemoryCurrentBytes": 0,
        "availableMemoryFloorBytes": None,
    }

    def sample_resource_peak():
        detail = resource_guard.assess_replay_budget()
        resource_peak["processTreeRssBytes"] = max(
            int(resource_peak["processTreeRssBytes"] or 0),
            int(detail.get("processTreeRssBytes") or 0),
        )
        resource_peak["processTreeSwapBytes"] = max(
            int(resource_peak["processTreeSwapBytes"] or 0),
            int(detail.get("processTreeSwapBytes") or 0),
        )
        resource_peak["cgroupMemoryCurrentBytes"] = max(
            int(resource_peak["cgroupMemoryCurrentBytes"] or 0),
            int(detail.get("cgroupMemoryCurrentBytes") or 0),
        )
        available = detail.get("availableMemoryBytes")
        if available is not None:
            previous = resource_peak.get("availableMemoryFloorBytes")
            resource_peak["availableMemoryFloorBytes"] = (
                int(available) if previous is None else min(int(previous), int(available))
            )

    sample_resource_peak()
    base_follow = params.load_follow(db)
    scanner_values = params.load_category(db, "scanner")
    base_follow.update({
        key: scanner_values[key] for key in COPY_POLICY_PARAM_KEYS if key in scanner_values
    })
    if _follow_override:
        base_follow.update(dict(_follow_override))
    if "SMART_ADD" in base_follow:
        base_follow["ADD_STRATEGY"] = "smart" if base_follow["SMART_ADD"] else "hardcap"
    sigmas = auto_tune._load_sigmas(db, generation_id)
    market_ctx = auto_tune._load_market_ctx(db, generation_id)
    valuation_marks = _current_copy_valuation_marks()
    current_core = (
        () if force_entry_requalification else tuple(selection.published_core_membership(db) or ())
    )
    core_upper = max(1, min(
        int(config.MAX_TARGETS),
        int(getattr(config, "CORE_TARGET_MAX_N", 16)),
        int(params.get(
            db, "CORE_INITIAL_MAX_N", config.CORE_INITIAL_MAX_N,
        ) or config.CORE_INITIAL_MAX_N),
    ))
    all_ranked_candidates = _quality_core_profiles(
        db, generation_id, core_only=False, now_ms=now_ms,
        retention_addrs=(
            () if force_entry_requalification else retention_addrs
        ),
    )
    # Top32 remains the rough/Challenger evidence pool.  Automatic formation freezes one bounded Top16
    # before any parameter work; ranks 17-32 are never reabsorbed after seeing a tuned surface.
    pre_strict_candidates = _bounded_formation_candidates(
        all_ranked_candidates,
        core_upper,
    )
    prepath_rows = list(pre_strict_candidates)
    prepath_rejected = []
    for row in prepath_rows:
        db.execute(
            "UPDATE pre_strict_evidence SET strict_status=?,strict_first_failure=NULL "
            "WHERE generation=? AND lower(addr)=?",
            ("frozen_top16", generation_id, row["addr"]),
        )
    db.commit()
    ranked_candidates = list(prepath_rows)
    rebalance_interval = max(1, int(params.get(
        db, "CORE_REBALANCE_INTERVAL_DAYS", config.CORE_REBALANCE_INTERVAL_DAYS,
    ) or 1))
    rebalance_due, core_age_days = _core_rebalance_due(
        db, current_core, now_ms=now_ms, interval_days=rebalance_interval,
    )
    # Every complete generation may publish the membership proven by its current strict replay. The expensive
    # parameter grid remains periodic; it must not also freeze wallet membership or overwrite a newly proven
    # set with the previous Core merely because the parameter-retune interval has not elapsed.
    # The visible switch/caller mode is authoritative.  A complete generation no longer silently skips
    # local tuning merely because a historical rebalance interval has not elapsed.
    retune = bool(retune)
    # Profile construction already performed the cheap profitability, evidence and valuation checks that
    # establish this rough profit-aligned score order. Do not run a path-complete individual replay on the active/default surface:
    # that duplicated the expensive work and could reject a wallet for parameters the following tuner exists
    # to repair. The winning surface below receives the one authoritative per-wallet strict replay.
    tune_ranked = ranked_candidates[:core_upper]
    if not tune_ranked:
        return _explicit_empty_core_formation(
            ranked_candidates, reason="no_core_qualified_wallets", tunePoolCount=0,
        )
    tune_ordered = tuple(row["addr"] for row in tune_ranked)

    # Keep the longest prepared sequence lazy. A resumed generation can often
    # rebuild the entire count/tune decision from compact evidence; eagerly
    # decoding every fill before the first cache lookup defeated that contract.
    tune_fill_context = {}

    # Preserve the fresh-generation contract: a genuinely empty/guarded fill
    # pool publishes an explicit empty Core rather than surfacing a tuner
    # exception. Only a resumed generation with compact evidence can defer the
    # decode until an exact cache miss is observed.
    has_formation_evidence = db.execute(
        "SELECT 1 FROM formation_prefix_evidence "
        "WHERE generation=? AND policy_version=? LIMIT 1",
        (generation_id, _FORMATION_PREFIX_CACHE_POLICY),
    ).fetchone() is not None
    if not has_formation_evidence:
        initial_fills = auto_tune._portfolio_window_fills(
            db, list(tune_ordered), now_ms, include_watch=True,
        )
        if initial_fills is None or not any(initial_fills.values()):
            return _explicit_empty_core_formation(
                ranked_candidates,
                reason=(
                    "fill_cache_guard" if initial_fills is None
                    else "no_cached_fills"
                ),
                tunePoolCount=len(tune_ordered),
            )
        tune_fill_context["fills"] = initial_fills

    def get_tune_window_fills():
        if "fills" not in tune_fill_context:
            fills = auto_tune._portfolio_window_fills(
                db, list(tune_ordered), now_ms, include_watch=True,
            )
            if fills is None or not any(fills.values()):
                raise RuntimeError(
                    "core_formation_fill_cache_guard"
                    if fills is None else "core_formation_no_cached_fills"
                )
            tune_fill_context["fills"] = fills
        return tune_fill_context["fills"]

    tune_eligible = None
    tune_reason = "retune_disabled"
    tune_search = None
    tune_runs = {}
    chosen_run = {}
    winning_count = len(tune_ordered)
    finalist_admission_audit = []
    retention = _core_prefix_retention()
    local_eval_cache = {}
    local_cache_stats = {"hits": 0, "persistentHits": 0, "writes": 0}

    def quick_surface_evaluate(count, surface, stage):
        count = max(1, min(len(tune_ordered), int(count)))
        surface = {
            key: f(surface.get(key, base_follow.get(key)))
            for key in (*auto_tune.TUNE_KEYS, *auto_tune.ADD_TUNE_KEYS)
        }
        surface_hash = hashlib.sha256(json.dumps(
            surface, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()
        cache_params_hash = f"quick:{surface_hash}"
        addrs = tuple(tune_ordered[:count])
        cache_key = (count, surface_hash)
        if cache_key in local_eval_cache:
            local_cache_stats["hits"] += 1
            return local_eval_cache[cache_key]
        cached = _load_formation_prefix_evidence(
            db, generation_id, cache_params_hash, addrs,
        )
        if cached is not None:
            local_cache_stats["hits"] += 1
            local_cache_stats["persistentHits"] += 1
            value, replay_summary = cached
        else:
            # The longest prepared fill sequence is already resident.  Check
            # the live working set only for a real replay miss; cached compact
            # evidence must remain resumable even when SQLite file pages make
            # cgroup memory.current look large.
            resource_guard.require_replay_budget()
            _set_scan_progress(
                db, stage=stage, candidates_scanned=count,
                candidates_total=len(tune_ordered),
            )
            tune_window_fills = get_tune_window_fills()
            filtered = auto_tune._filter_window_fills_by_addr(
                tune_window_fills, addrs,
            )
            windows = auto_tune._candidate_windows(
                db, list(addrs), sigmas,
                {**base_follow, **surface, "AMBIGUOUS_PATH_MODE": "liquidate"},
                now_ms, window_fills=filtered, market_ctx=market_ctx,
                path_rows=None, path_meta=None, compact=True,
            )
            metrics_ = _portfolio_selection_metrics(windows, selected_n=count)
            primary = windows.get(30) or windows.get(max(windows)) or {}
            recent = windows.get(7) or {}
            start_equity = f(
                primary.get("window_start_equity")
                or primary.get("initial_margin_equity")
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
                "return30d": return_30d,
                "return7d": return_7d,
                "openLossRatio30d": primary_economics.get("openLossRatio"),
                "tierEconomics": primary.get("tier_economics") or {},
                "addCaptureRate": f(
                    primary.get("actionable_add_capture_rate")
                    if primary.get("actionable_add_capture_rate") is not None else 1.0
                ),
                "pnlConcentration": primary.get("pnl_concentration") or {},
            }
            value = core_formation.PrefixEvaluation(
                count=count,
                net_pnl=f(metrics_.net_pnl),
                stress_net_pnl=f(metrics_.net_pnl),
                max_drawdown=f(metrics_.max_drawdown),
                actionable_open_rate=f(metrics_.actionable_open_rate),
                capacity_fit=f(metrics_.capacity_fit),
                liquidations=int(metrics_.liquidations),
                params=surface,
                payload={
                    "initialBalance": start_equity,
                    "recentStartEquity": recent_start_equity,
                    "return30d": return_30d,
                    "return7d": return_7d,
                    "openLossRatio30d": primary_economics.get("openLossRatio"),
                    "requireCongestionFit": True,
                    "requireReturnFit": True,
                    "tierEconomics": replay_summary["tierEconomics"],
                },
            )
            _store_formation_prefix_evidence(
                db, generation_id, cache_params_hash, addrs, value, replay_summary,
            )
            local_cache_stats["writes"] += 1
            del windows
            gc.collect()
            sample_resource_peak()
        row = {
            "count": count,
            "netPnl": f(value.net_pnl),
            "feasible": bool(value.feasible),
            "liquidations": int(value.liquidations),
            "maxDrawdown": f(value.max_drawdown),
            "openRate": f(value.actionable_open_rate),
            "capacityFit": f(value.capacity_fit),
            "addCaptureRate": f(replay_summary.get("addCaptureRate") or 0.0),
            "return30d": replay_summary.get("return30d"),
            "return7d": replay_summary.get("return7d"),
            "tierEconomics": replay_summary.get("tierEconomics") or {},
            "prefixEvaluation": value,
            "paramsHash": surface_hash,
        }
        local_eval_cache[cache_key] = row
        return row

    if retune:
        # Locate the count first on the active surface.  The bounded search probes N -> N/2 -> midpoint and
        # boundary neighbours, with no historical eight-wallet floor.
        count_rows = {}

        def current_surface_evaluate(count):
            row = quick_surface_evaluate(
                count,
                {
                    key: f(base_follow.get(key))
                    for key in (*auto_tune.TUNE_KEYS, *auto_tune.ADD_TUNE_KEYS)
                },
                "portfolio_count_search",
            )
            count_rows[int(count)] = row
            return row["prefixEvaluation"]

        try:
            tune_search = core_formation.search_quality_prefix(
                len(tune_ordered), current_surface_evaluate,
                retention_kwargs=retention,
                tie_tolerance=float(config.CORE_PREFIX_TIE_TOLERANCE),
                exhaustive_below=0,
                required_count=0,
            )
            winning_count = int(tune_search.selected.count)
        except RuntimeError as exc:
            if str(exc) != "no_feasible_quality_prefix":
                raise
            # Keep the best repairable count as the local-search center; it cannot publish until a strict
            # finalist passes below.
            repairable = list(count_rows.values())
            if not repairable:
                repairable.append(quick_surface_evaluate(
                    1,
                    {
                        key: f(base_follow.get(key))
                        for key in (*auto_tune.TUNE_KEYS, *auto_tune.ADD_TUNE_KEYS)
                    },
                    "portfolio_count_search",
                ))
            repair_center = max(repairable, key=lambda row: (
                f(row.get("openRate")) + f(row.get("capacityFit")),
                f(row.get("netPnl")), -int(row.get("count") or 0),
            ))
            winning_count = int(repair_center["count"])
            tune_reason = "active_surface_repair_center"

        validation_path_start = now_ms - (
            max(auto_tune._tune_days())
            + int(getattr(config, "COPY_BT_WARMUP_DAYS", 7) or 0)
        ) * 86_400_000
        strict_path_context = {}

        def strict_validation_path():
            """Load the shared refined path only for a strict cache miss.

            Recovery commonly has every finalist in
            ``formation_prefix_evidence``.  Eagerly rebuilding the full path
            before checking those rows both wasted minutes and overlapped the
            largest fill/path objects in memory.
            """
            if not strict_path_context:
                resource_guard.require_replay_budget()
                tune_window_fills = get_tune_window_fills()
                validation_fills = list(
                    tune_window_fills.get(max(tune_window_fills)) or []
                )
                strict_path_context["path"] = prepare_price_path(
                    price_path.load_refined(
                        db, validation_fills, validation_path_start, now_ms,
                    )
                )
                strict_path_context["meta"] = price_path.coverage(
                    db, validation_fills, validation_path_start, now_ms,
                )
                del validation_fills
            return strict_path_context["path"], strict_path_context["meta"]

        policy = load_copy_policy(base_follow)

        def strict_local_validate(count, surface):
            _set_scan_progress(
                db, stage="local_finalist_validation",
                candidates_scanned=int(count), candidates_total=len(tune_ordered),
            )
            addrs = tuple(tune_ordered[:int(count)])
            strict_surface_hash = hashlib.sha256(json.dumps(
                surface, sort_keys=True, separators=(",", ":"), default=str,
            ).encode("utf-8")).hexdigest()
            strict_cache_hash = f"strict-finalist:{strict_surface_hash}"
            cached = _load_formation_prefix_evidence(
                db, generation_id, strict_cache_hash, addrs,
            )
            if cached is not None:
                _cached_value, replay_summary = cached
                if replay_summary.get("validationMode") == "strict-finalist":
                    local_cache_stats["hits"] += 1
                    local_cache_stats["persistentHits"] += 1
                    return dict(replay_summary.get("validation") or {})
            validation_path, validation_path_meta = strict_validation_path()
            tune_window_fills = get_tune_window_fills()
            filtered = auto_tune._filter_window_fills_by_addr(
                tune_window_fills, addrs,
            )
            windows = auto_tune._candidate_windows(
                db, list(addrs), sigmas,
                {**base_follow, **surface, "AMBIGUOUS_PATH_MODE": "liquidate"},
                now_ms, window_fills=filtered, market_ctx=market_ctx,
                path_rows=validation_path, path_meta=validation_path_meta,
                initial_balance=float(config.INITIAL_BALANCE), compact=True,
            )
            primary = windows.get(30) or windows.get(max(windows)) or {}
            recent = windows.get(7) or {}
            metrics_ = _portfolio_selection_metrics(windows, selected_n=len(addrs))
            primary_economics = replay_result_profitability(primary)
            recent_economics = replay_result_profitability(recent)
            start_30 = f(
                primary.get("window_start_equity")
                or primary.get("initial_margin_equity")
                or config.INITIAL_BALANCE
            )
            start_7 = f(
                recent.get("window_start_equity")
                or recent.get("initial_margin_equity")
            )
            return_30 = (
                f(primary_economics.get("qualificationPnl")) / start_30
                if start_30 > 0.0 else float("-inf")
            )
            return_7 = (
                f(recent_economics.get("qualificationPnl")) / start_7
                if start_7 > 0.0 else float("-inf")
            )
            reasons = []
            if f(primary_economics.get("qualificationPnl")) <= 0.0:
                reasons.append("net_not_positive")
            if f(recent_economics.get("qualificationPnl")) <= 0.0:
                reasons.append("recent_net_not_positive")
            if return_30 < policy.portfolio_min_return_30d:
                reasons.append("dynamic_return_30d")
            if return_7 < policy.portfolio_min_return_7d:
                reasons.append("dynamic_return_7d")
            if not open_loss_ratio_within_limit(primary_economics):
                reasons.append("open_loss_over_50pct")
            if f(metrics_.actionable_open_rate) < policy.min_actionable_open_rate:
                reasons.append("open_follow_rate")
            if f(metrics_.capacity_fit) < policy.min_capacity_fit:
                reasons.append("capacity_fit")
            if f(primary.get("price_path_coverage")) < float(config.CORE_PRICE_PATH_MIN_COVERAGE):
                reasons.append("path_coverage")
            if f(primary.get("maintenance_margin_coverage")) < float(
                config.CORE_MAINTENANCE_META_MIN_COVERAGE
            ):
                reasons.append("maintenance_coverage")
            result = {
                "eligible": not reasons, "reasons": reasons,
                "netPnl30d": f(primary_economics.get("qualificationPnl")),
                "netPnl7d": f(recent_economics.get("qualificationPnl")),
                "dynamicReturn30d": return_30, "dynamicReturn7d": return_7,
                "liquidations": int(metrics_.liquidations),
                "openRate": f(metrics_.actionable_open_rate),
                "capacityFit": f(metrics_.capacity_fit),
                "tierEconomics": primary.get("tier_economics") or {},
                "pricePathCoverage": f(primary.get("price_path_coverage")),
                "maintenanceMarginCoverage": f(primary.get("maintenance_margin_coverage")),
            }
            evidence = core_formation.PrefixEvaluation(
                count=len(addrs),
                net_pnl=f(primary_economics.get("qualificationPnl")),
                stress_net_pnl=f(primary_economics.get("qualificationPnl")),
                max_drawdown=f(metrics_.max_drawdown),
                actionable_open_rate=f(metrics_.actionable_open_rate),
                capacity_fit=f(metrics_.capacity_fit),
                liquidations=int(metrics_.liquidations),
                params=dict(surface),
                payload={
                    "return30d": return_30, "return7d": return_7,
                    "validationMode": "strict-finalist",
                },
            )
            _store_formation_prefix_evidence(
                db, generation_id, strict_cache_hash, addrs, evidence,
                {"validationMode": "strict-finalist", "validation": result},
            )
            del windows
            gc.collect()
            sample_resource_peak()
            return result

        def local_progress(stage, completed, _increment):
            _set_scan_progress(
                db, stage=stage, candidates_scanned=int(completed),
                candidates_total=1,
            )

        chosen_run = auto_tune.tune_local_prefix_surfaces(
            candidate_count=len(tune_ordered), center_count=winning_count,
            follow=base_follow, evaluate=quick_surface_evaluate,
            validate=strict_local_validate, progress=local_progress,
        )
        db.commit()
        sample_resource_peak()
        winning_count = int(chosen_run.get("selected_count") or winning_count)
        finalist_admission_audit = list(chosen_run.get("finalists") or ())
        tuned_params, tune_eligible, tune_reason = _formation_param_surface(
            base_follow, chosen_run, retune=True,
        )
        # The final Top16 individual and shared-count stages load their own
        # bounded paths.  Do not retain the finalist path across that phase
        # boundary; this overlap was the dominant single-process RSS peak.
        strict_path_context.clear()
        tune_fill_context.clear()
        gc.collect()
    else:
        tuned_params, tune_eligible, tune_reason = _formation_param_surface(
            base_follow, None, retune=False,
        )
    tune_fill_context.clear()
    gc.collect()
    fixed_follow = {**base_follow, **tuned_params, "AMBIGUOUS_PATH_MODE": "liquidate"}
    # ``winning_count`` says which score prefix fitted this parameter surface; it must not permanently
    # delete the rest of the bounded Top16 before strict replay.  Every Top16 wallet receives the winning
    # surface, individual failures are removed, and only then may the shared-account prefix search choose
    # its final count.  Otherwise fitting k=9 silently made ranks 10–16 ineligible without evaluating them.
    # The frozen Top16 receives the one authoritative individual strict replay on the winning surface.
    # Rough ranks 17-32 remain Challenger evidence and cannot re-enter after observing tuned parameters.
    tuned_candidate_rows = list(prepath_rows)

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
        individual_cache_hash = f"individual:{surface_key}"
        for index, row in enumerate(tuned_candidate_rows, start=1):
            addr = row["addr"]
            _set_scan_progress(
                db, stage="top16_individual_strict",
                candidates_scanned=index - 1, candidates_total=len(tuned_candidate_rows),
            )
            cached = _load_formation_prefix_evidence(
                db, generation_id, individual_cache_hash, (addr,),
            )
            effective = None
            if cached is not None:
                _cached_value, replay_summary = cached
                if replay_summary.get("validationMode") == "individual":
                    effective = dict(replay_summary.get("effective") or {})
                    local_cache_stats["hits"] += 1
                    local_cache_stats["persistentHits"] += 1
            if not effective:
                effective = _effective_follow_replay(
                    db, row, now_ms,
                    generation_id=generation_id, follow=follow_surface,
                    valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
                    strict_path=True, qualification_stage="strict",
                )
                effective_metrics = dict(effective.get("metrics") or {})
                effective_qualification = dict(effective.get("qualification") or {})
                evidence = core_formation.PrefixEvaluation(
                    count=1,
                    net_pnl=f(effective_metrics.get("copy_bt_net_pnl")),
                    stress_net_pnl=f(effective_metrics.get("copy_bt_net_pnl")),
                    max_drawdown=f(effective_metrics.get("copy_bt_max_drawdown")),
                    actionable_open_rate=f(effective_metrics.get("actionable_open_rate")),
                    capacity_fit=f(effective_metrics.get("capacity_fit")),
                    liquidations=int(f(effective_metrics.get("copy_bt_liquidations"))),
                    params=dict(follow_surface),
                    payload={
                        "validationMode": "individual",
                        "status": effective_qualification.get("status"),
                    },
                )
                _store_formation_prefix_evidence(
                    db, generation_id, individual_cache_hash, (addr,), evidence,
                    {
                        "validationMode": "individual",
                        "effective": {
                            key: value for key, value in effective.items()
                            if key != "results"
                        },
                    },
                )
            qualification = dict(effective.get("qualification") or {})
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
                    db, addr, "copy_single_liquidation_loss_over_8pct",
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
            # Release strict-status/risk-event writes before replaying the next wallet. The final
            # selection/params/revision publication remains one separate atomic transaction.
            db.commit()
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
    sample_resource_peak()
    tune_coverage_fallback = False
    effective_ranked.sort(key=lambda row: follow_score.follow_score_sort_key(
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
                "algorithm": "count_first_local_surface_v1", "initialCount": 0,
                "selectedCount": 0,
                "explicitEmptyCore": True,
                "tunePoolCount": len(tune_ordered),
                "tunedInputCount": winning_count,
                "fullTuneRuns": 1 if chosen_run.get("search_profile") == "local" else 0,
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
    membership_fill_context = {}

    def get_membership_window_fills():
        if "fills" not in membership_fill_context:
            fills = auto_tune._portfolio_window_fills(
                db, list(ordered), now_ms, include_watch=True,
            )
            if fills is None or not any(fills.values()):
                raise RuntimeError("core_prefix_replay_unavailable")
            membership_fill_context["fills"] = fills
        return membership_fill_context["fills"]

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
        final_cache_hash = f"final-shared:{effective_surface_hash}"
        cached = _load_formation_prefix_evidence(
            db, generation_id, final_cache_hash, key,
        )
        if cached is not None:
            value, replay_summary = cached
            membership_eval_cache[key] = value
            membership_replay_cache[key] = replay_summary
            local_cache_stats["hits"] += 1
            local_cache_stats["persistentHits"] += 1
            _set_scan_progress(
                db, stage="portfolio_prefix_cache_hit",
                candidates_scanned=len(key), candidates_total=len(ordered),
            )
            return value
        _set_scan_progress(
            db, stage="portfolio_prefix_strict",
            candidates_scanned=len(key), candidates_total=len(ordered),
        )
        window_fills = get_membership_window_fills()
        filtered = auto_tune._filter_window_fills_by_addr(window_fills, key)
        windows = auto_tune._candidate_windows(
            db, list(key), sigmas, fixed_follow, now_ms,
            window_fills=filtered, market_ctx=market_ctx,
            path_rows=None, path_meta=None, compact=True,
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
                "requireCongestionFit": True,
                "requireReturnFit": True,
            },
        )
        membership_eval_cache[key] = value
        # The optimizer may inspect dozens of sets. Retaining six complete replay results per set (positions,
        # open positions and equity curves) exhausted the 1GB production host. Robust validation needs only
        # these contribution/outlier summaries; all ranking metrics already live in ``value``.
        membership_replay_cache[key] = replay_summary
        _store_formation_prefix_evidence(
            db, generation_id, final_cache_hash, key, value,
            {"validationMode": "final-shared", **replay_summary},
        )
        return value

    def evaluate(count):
        return evaluate_members(ordered[:int(count)])

    prefix_search = core_formation.search_quality_prefix(
        len(ordered), evaluate, retention_kwargs=retention,
        tie_tolerance=float(config.CORE_PREFIX_TIE_TOLERANCE),
        exhaustive_below=int(getattr(config, "CORE_PREFIX_EXHAUSTIVE_MAX_N", 8) or 0),
        required_count=0,
    )
    # Core membership is a strict prefix of the final profit-aligned score order. An arbitrary add/swap search
    # would turn the deterministic ranking contract into an overfit subset search.
    chosen = prefix_search.selected
    chosen_addrs = tuple(ordered[:chosen.count])
    membership_algorithm = "profit_aligned_score_prefix"

    robust_cache = {}

    def validate_members(addrs):
        key = tuple(sorted(dict.fromkeys(addrs)))
        if key in robust_cache:
            return robust_cache[key]
        value = evaluate_members(key)
        replay_summary = membership_replay_cache[key]
        check = core_formation.validate_final_membership(value)
        loo_marginals = {}
        if bool(getattr(config, "CORE_FORMATION_ENABLE_LOO", False)):
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
    # the resulting set has passed these same membership stress rules. Only the lowest-score suffix is removable.
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
    sample_resource_peak()
    membership_fill_context.clear()
    gc.collect()
    result = {
        "selected": chosen_addrs, "ranked": ordered,
        "params": dict(chosen.params), "evaluations": evaluations,
        "qualifications": effective_qualifications, "scores": effective_scores,
        "scoreDetails": effective_score_details,
        "profitPriorities": effective_profit_priorities,
        "policies": effective_policies, "walletMetrics": effective_metrics,
        "replayParamsHash": effective_surface_hash,
        "search": {
            "algorithm": "count_first_local_surface_v1", "initialCount": len(ordered),
            "selectedCount": len(chosen_addrs), "boundary": prefix_search.boundary,
            "evaluatedCounts": [value.count for value in prefix_search.evaluated],
            "evaluations": evaluations,
            "membershipAlgorithm": membership_algorithm,
            "rankingMode": follow_score.FOLLOW_SCORE_MODE,
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
            "fullTuneRuns": 1 if chosen_run.get("search_profile") == "local" else 0,
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
            "candidatePoolCount": len(tune_ordered),
            "countCenter": chosen_run.get("count_center"),
            "primaryCounts": list(chosen_run.get("primary_counts") or ()),
            "guardCounts": list(chosen_run.get("guard_counts") or ()),
            "guardPromoted": chosen_run.get("guard_promoted"),
            "sharedSurfaceCount": int(chosen_run.get("shared_surface_count") or 0),
            "tierBreakoutProbe": chosen_run.get("breakout_tier"),
            "quickReplayCount": int(chosen_run.get("quick_replay_count") or 0),
            "expensiveFinalistCount": len(chosen_run.get("finalists") or ()),
            "cacheHitCount": int(chosen_run.get("cache_hit_count") or 0)
                + int(local_cache_stats.get("hits") or 0),
            "tierEconomics": dict(chosen_run.get("tier_economics") or {}),
            "finalCountDrift": len(chosen_addrs) - int(
                chosen_run.get("selected_count") or len(chosen_addrs)
            ),
            "stageDurations": dict(chosen_run.get("stage_durations") or {}),
            "resourcePeak": dict(resource_peak),
            "fallbackReason": (
                chosen_run.get("reason")
                if chosen_run.get("status") not in {None, "ok"} else None
            ),
            "resumeCount": int(bool(local_cache_stats.get("persistentHits"))),
            "deferredReasons": [],
            "qualificationRejected": qualification_rejected,
            "admission": admission_audit,
            "finalSurfaceUniverseCount": len(tuned_candidate_rows),
            "finalSurfaceQualifiedCount": len(effective_ranked),
            "closureRounds": [],
            "closureStable": True,
        },
    }
    # Membership changes after the one tuned surface are confirmed by the strict shared-account replay
    # already contained in ``result``. They never recursively start another parameter pool.
    result["search"]["initialTunedInputCount"] = winning_count
    result["search"]["closureRounds"] = []
    result["search"]["closureStable"] = True
    result["search"]["membershipConfirmedWithoutRetune"] = True
    return result


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


def _active_pinned_core_order(db) -> tuple[str, ...]:
    """Return operator-starred Core seats which are still active execution targets.

    A complete scan is a strict membership reset.  Historical Core membership alone never grants a
    retention lane; only an explicit operator star does, and a disabled/draining/requalify control cannot
    use that star to recover an active seat implicitly.
    """
    active = {
        str(addr or "").lower()
        for addr in (selection.published_core_addrs(db) or ()) if addr
    }
    return tuple(
        addr for addr in (
            str(item.get("addr") or "").lower()
            for item in selection.pinned_core_controls(db, enabled_only=True)
        )
        if addr and addr in active
    )


def _assert_daily_promotion_parity(
    db, generation_id, *, previous_core, proposed_core, promotion_universe, formation,
) -> dict:
    """Fail closed unless every daily entrant satisfies the full-scan admission contract."""
    previous = {
        str(addr or "").lower() for addr in (previous_core or ()) if addr
    }
    proposed = tuple(dict.fromkeys(
        str(addr or "").lower() for addr in (proposed_core or ()) if addr
    ))
    universe = {
        str(addr or "").lower() for addr in (promotion_universe or ()) if addr
    }
    qualifications = {
        str(addr or "").lower(): dict(value or {})
        for addr, value in dict((formation or {}).get("qualifications") or {}).items()
    }
    added = tuple(addr for addr in proposed if addr not in previous)
    failures = []
    for addr in added:
        evidence = db.execute(
            "SELECT status,queue_rank,strict_status,policy_version,model_version "
            "FROM pre_strict_evidence WHERE generation=? AND lower(addr)=?",
            (generation_id, addr),
        ).fetchone()
        qualification = qualifications.get(addr) or {}
        valid = bool(
            addr in universe
            and evidence
            and evidence[0] == "passed"
            and evidence[1] is not None
            and evidence[2] == "qualified"
            and evidence[3] == pre_strict.POLICY_VERSION
            and evidence[4] == pre_strict.SELECTION_MODEL_VERSION
            and qualification.get("eligible")
            and qualification.get("coreEligible")
            and qualification.get("individualCoreEligible")
            and qualification.get("formationEligible")
            and not qualification.get("deferred")
            and qualification.get("role") != "quarantine"
        )
        if not valid:
            failures.append(addr)
    if failures:
        raise RuntimeError(f"challenger_daily_promotion_parity_failed:{len(failures)}")
    return {
        "checked": len(added),
        "passed": len(added),
        "policyVersion": pre_strict.POLICY_VERSION,
        "modelVersion": pre_strict.SELECTION_MODEL_VERSION,
    }


def _complete_retention_decisions(
    db, generation_id, previous_core, formation, *, strict_deferred_mode="error",
) -> dict[str, core_retention.RetentionDecision]:
    """Classify frozen current-generation evidence for every prior Core."""
    qualifications = {
        str(addr or "").lower(): dict(value or {})
        for addr, value in dict((formation or {}).get("qualifications") or {}).items()
    }
    decisions = {}
    for addr in sorted({str(value or "").lower() for value in previous_core if value}):
        evidence = db.execute(
            "SELECT p.status,COALESCE(p.data_status,'valid'),p.reason,"
            "pse.status,pse.first_failure,pse.strict_status,pse.strict_first_failure,"
            "p.acct_value,COALESCE(p.open_position_count,0) "
            "FROM profile p LEFT JOIN pre_strict_evidence pse "
            "ON pse.generation=? AND lower(pse.addr)=lower(p.addr) "
            "WHERE p.profile_generation=? AND lower(p.addr)=lower(?)",
            (generation_id, generation_id, addr),
        ).fetchone()
        if not evidence or evidence[1] != "valid":
            raise RuntimeError(f"core_retention_evidence_incomplete:{addr}")
        if evidence[3] is None:
            profile_reason = str(evidence[2] or "")
            if core_retention.failure_class(profile_reason) != "hard":
                raise RuntimeError(f"core_retention_evidence_incomplete:{addr}")
            qualification = {}
            deferred = False
            reason = profile_reason
            previous = wallet_retention_state(db, addr)
            decisions[addr] = core_retention.advance(
                previous_status=previous["status"],
                previous_streak=previous["failureStreak"],
                previous_reason=previous["failureReason"],
                previous_started_generation=previous["startedGeneration"],
                generation=generation_id,
                scan_kind="complete",
                scan_successful=True,
                reason=reason,
                deferred=False,
            )
            continue
        qualification = qualifications.get(addr) or {}
        deferred = bool(
            qualification.get("deferred")
            or str(evidence[5] or "") == "deferred"
        )
        if deferred:
            if strict_deferred_mode == "defer":
                # Complete scans may retain only explicit operator-starred Core.  A temporary path/data
                # gap must not silently waive that override or publish it with incomplete strict proof;
                # preserve the generation and let the finalizer retry after its bounded path repair.
                raise resource_guard.ResourceDeferred({
                    "status": "resource_deferred",
                    "reasons": ["pinned_core_strict_evidence_deferred"],
                    "generation": generation_id,
                    "deferredPinned": 1,
                })
            if strict_deferred_mode != "retain":
                raise RuntimeError(f"core_retention_strict_evidence_deferred:{addr}")
            previous = wallet_retention_state(db, addr)
            decisions[addr] = core_retention.advance(
                previous_status=previous["status"],
                previous_streak=previous["failureStreak"],
                previous_reason=previous["failureReason"],
                previous_started_generation=previous["startedGeneration"],
                generation=generation_id,
                scan_kind="challenger_refresh",
                scan_successful=True,
                reason=(
                    qualification.get("firstFailure")
                    or evidence[6] or "strict_evidence_deferred"
                ),
                deferred=True,
            )
            continue
        qualification_class, qualification_reason = (
            core_retention.qualification_failure(qualification)
        )
        reason = None if qualification_class == core_retention.HEALTHY else qualification_reason
        reason = reason or evidence[6] or evidence[4]
        if (
            evidence[7] is not None
            and f(evidence[7]) <= max(float(config.FLAT), 1e-6)
            and int(evidence[8] or 0) == 0
        ):
            reason = "source_zero_equity_no_positions"
        safety = db.execute(
            "SELECT state,reason FROM execution_wallet_safety WHERE lower(addr)=lower(?)",
            (addr,),
        ).fetchone()
        if safety and safety[0] == "confirmed":
            reason = safety[1] or "source_account_liquidated_zero"
        if wallet_risk.actual_copy_evidence(
            db, addr,
        ).get("catastrophicPositionIds"):
            reason = "actual_copy_single_liquidation_loss_over_8pct"
        previous = wallet_retention_state(db, addr)
        confirmation_eligible = _retention_confirmation_eligible(
            db,
            previous.get("startedGeneration"),
            generation_id,
            previous_streak=previous.get("failureStreak"),
        )
        decisions[addr] = core_retention.advance(
            previous_status=previous["status"],
            previous_streak=previous["failureStreak"],
            previous_reason=previous["failureReason"],
            previous_started_generation=previous["startedGeneration"],
            generation=generation_id,
            scan_kind="complete",
            scan_successful=True,
            reason=reason,
            deferred=deferred,
            confirmation_eligible=confirmation_eligible,
        )
    return decisions


def _retention_confirmation_eligible(
    db, started_generation, current_generation, *, previous_streak=0,
) -> bool:
    """Require independent wall-clock evidence before confirming an ordinary demotion."""
    if int(previous_streak or 0) <= 0 or not started_generation:
        return True
    rows = {
        str(row[0]): row[1]
        for row in db.execute(
            "SELECT generation,started_at FROM scan_generation "
            "WHERE generation IN (?,?)",
            (started_generation, current_generation),
        ).fetchall()
    }
    try:
        started_s = calendar.timegm(time.strptime(
            str(rows[started_generation]), "%Y-%m-%dT%H:%M:%SZ",
        ))
        current_s = calendar.timegm(time.strptime(
            str(rows[current_generation]), "%Y-%m-%dT%H:%M:%SZ",
        ))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    minimum_s = float(config.CORE_RETENTION_MIN_CONFIRMATION_HOURS) * 3600.0
    return current_s - started_s >= minimum_s


def _retention_exact_formation(
    db, generation_id, stamp, now_ms, desired_order, *,
    base_follow, replacement_gate, decisions, retune=False,
) -> dict:
    """Replay an exact effective membership after a blocked replacement."""
    candidate_rows = _quality_core_profiles(
        db, generation_id, core_only=False, now_ms=now_ms,
    )
    row_by_addr = {
        str(row.get("addr") or "").lower(): row for row in candidate_rows
    }
    missing = [addr for addr in desired_order if addr not in row_by_addr]
    if missing:
        raise RuntimeError(f"core_retention_candidate_missing:{len(missing)}")
    sigmas = auto_tune._load_sigmas(db, generation_id)
    market_ctx = auto_tune._load_market_ctx(db, generation_id)
    valuation_marks = _current_copy_valuation_marks()
    if retune:
        exact = _retune_exact_membership_surface(
            db, desired_order, candidate_rows,
            generation_id=generation_id, stamp=stamp, round_index=1,
            now_ms=now_ms, base_follow=dict(base_follow),
            valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
        )
    else:
        exact = {"follow": dict(base_follow), "params": dict(base_follow)}
    exact_results = _parallel_effective_follow_replays(
        db, [row_by_addr[addr] for addr in desired_order], now_ms,
        generation_id=generation_id, follow=exact["follow"],
        valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
        strict_path=True, qualification_stage="strict",
    )
    qualifications = {}
    scores = {}
    metrics_by_addr = {}
    score_details = {}
    policies = {}
    for addr, effective in zip(desired_order, exact_results):
        qualification = dict(effective.get("qualification") or {})
        classification, _classification_reason = (
            core_retention.qualification_failure(qualification)
        )
        if classification in {"deferred", "hard"}:
            raise RuntimeError(
                f"core_retention_exact_replay_invalid:{addr}:"
                f"{qualification.get('firstFailure') or classification}"
            )
        qualifications[addr] = qualification
        scores[addr] = f(effective.get("score"))
        metrics_by_addr[addr] = dict(effective.get("metrics") or {})
        score_details[addr] = dict(effective.get("scoreDetail") or {})
        policies[addr] = (
            effective.get("sectorPolicyJson")
            or row_by_addr[addr].get("sector_policy_json")
        )
    return {
        "selected": tuple(desired_order),
        "ranked": tuple(desired_order),
        "params": dict(exact.get("params") or {}),
        "qualifications": qualifications,
        "scores": scores,
        "scoreDetails": score_details,
        "policies": policies,
        "walletMetrics": metrics_by_addr,
        "replayParamsHash": hashlib.sha256(
            json.dumps(
                exact["follow"], sort_keys=True, separators=(",", ":"), default=float,
            ).encode()
        ).hexdigest(),
        "search": {
            "algorithm": "effective_incumbent_membership_v2",
            "selectedCount": len(desired_order),
            "membershipChanged": bool(retune),
            "retuneApplied": bool(retune),
            "retentionHysteresis": True,
            "replacementGate": replacement_gate,
            "retentionDecisions": {
                addr: {
                    "status": decision.status,
                    "streak": decision.failure_streak,
                    "reason": decision.failure_reason,
                    "action": decision.action,
                }
                for addr, decision in decisions.items()
            },
        },
    }


def _retention_evidence_formation(
    formation, desired_order, *, replacement_gate, decisions,
) -> dict:
    """Overlay incumbent retention using the winning Top16 evidence without another replay.

    Automatic formation has already strictly replayed every frozen Top16 wallet on the winning surface.
    Retaining an incumbent from that same pool must consume those immutable results; replaying the effective
    membership wallet-by-wallet here would create an accidental second strict pass after tuning completed.
    """
    desired = tuple(dict.fromkeys(
        str(addr or "").lower() for addr in (desired_order or ()) if addr
    ))
    qualifications = {
        str(addr or "").lower(): dict(value or {})
        for addr, value in dict((formation or {}).get("qualifications") or {}).items()
    }
    scores = {
        str(addr or "").lower(): value
        for addr, value in dict((formation or {}).get("scores") or {}).items()
    }
    policies = {
        str(addr or "").lower(): value
        for addr, value in dict((formation or {}).get("policies") or {}).items()
    }
    metrics = {
        str(addr or "").lower(): dict(value or {})
        for addr, value in dict((formation or {}).get("walletMetrics") or {}).items()
    }
    score_details = {
        str(addr or "").lower(): dict(value or {})
        for addr, value in dict((formation or {}).get("scoreDetails") or {}).items()
    }
    incomplete = [
        addr for addr in desired
        if addr not in qualifications or addr not in metrics or addr not in policies
    ]
    if incomplete:
        raise RuntimeError(f"core_retention_cached_evidence_missing:{len(incomplete)}")
    invalid = []
    for addr in desired:
        classification, _reason = core_retention.qualification_failure(
            qualifications[addr]
        )
        if classification in {"deferred", "hard"}:
            invalid.append(addr)
    if invalid:
        raise RuntimeError(f"core_retention_cached_evidence_invalid:{len(invalid)}")
    ranked = tuple(dict.fromkeys((
        *desired,
        *(
            str(addr or "").lower()
            for addr in ((formation or {}).get("ranked") or ())
            if addr
        ),
    )))
    search = {
        **dict((formation or {}).get("search") or {}),
        "selectedCount": len(desired),
        "retentionHysteresis": True,
        "retentionEvidenceReused": True,
        "replacementGate": dict(replacement_gate or {}),
        "retentionDecisions": {
            addr: {
                "status": decision.status,
                "streak": decision.failure_streak,
                "reason": decision.failure_reason,
                "action": decision.action,
            }
            for addr, decision in decisions.items()
        },
    }
    return {
        **dict(formation or {}),
        "selected": desired,
        "ranked": ranked,
        "qualifications": qualifications,
        "scores": scores,
        "policies": policies,
        "walletMetrics": metrics,
        "scoreDetails": score_details,
        "search": search,
    }


def _effective_core_order_from_addrs(previous_core, proposed_addrs, decisions):
    """Retain every non-blocked incumbent, then fill remaining seats from a proven order."""
    previous = tuple(dict.fromkeys(
        str(addr or "").lower() for addr in (previous_core or ()) if addr
    ))
    retained = [
        addr for addr in previous
        if decisions.get(addr) is not None and decisions[addr].retain_enabled
    ]
    blocked_incumbents = {
        addr for addr in previous
        if decisions.get(addr) is not None and not decisions[addr].retain_enabled
    }
    proposed = tuple(dict.fromkeys(
        str(addr or "").lower() for addr in (proposed_addrs or ()) if addr
    ))
    cap = max(1, min(
        int(config.MAX_TARGETS),
        int(getattr(config, "CORE_TARGET_MAX_N", 16)),
        int(getattr(config, "CORE_INITIAL_MAX_N", 16)),
    ))
    desired = list(retained)
    for addr in proposed:
        if addr not in desired and addr not in blocked_incumbents and len(desired) < cap:
            desired.append(addr)
    return tuple(desired)


def _effective_core_order(previous_core, proposed_rows, decisions):
    """Retain every non-blocked incumbent and only fill genuinely empty seats."""
    proposed = [
        row.addr.lower() for row in sorted(
            (row for row in proposed_rows if row.role == selection.CORE and row.enabled),
            key=lambda row: (row.selection_rank or 999999, row.addr),
        )
    ]
    return _effective_core_order_from_addrs(previous_core, proposed, decisions)


def _decorate_retention_rows(rows, previous_core, decisions):
    previous_core = {str(addr or "").lower() for addr in previous_core}
    out = []
    for row in rows:
        addr = row.addr.lower()
        decision = decisions.get(addr)
        if addr in previous_core and decision:
            row = replace(
                row,
                # Low/medium financial risk is advisory.  Only a financial
                # catastrophe or a structural/system block revokes entry.
                entry_eligible=decision.retain_enabled,
                retention_status=decision.status,
                retention_failure_reason=decision.failure_reason,
                retention_failure_streak=decision.failure_streak,
                retained_by_hysteresis=bool(
                    row.role == selection.CORE and decision.retained_by_hysteresis
                ),
                reason=(
                    f"core_probation:{decision.failure_reason}"
                    if row.role == selection.CORE
                    and decision.status == core_retention.PROBATION
                    else row.reason
                ),
            )
        out.append(row)
    return out


def _apply_shared_retention_failure(
    db, generation_id, previous_core, decisions, marginal,
):
    validation = (
        ((marginal.search_meta or {}).get("finalStrictCopy"))
        if marginal else {}
    ) or {}
    if validation.get("status") not in {"probation", "operator_review_degraded"}:
        return decisions
    updated = dict(decisions)
    for addr in previous_core:
        addr = str(addr or "").lower()
        current = updated.get(addr)
        if current is None or not current.retain_enabled:
            continue
        if current.failure_reason:
            # One successful generation is one confirmation point even when
            # both wallet and shared-account evidence are degraded.
            continue
        previous = wallet_retention_state(db, addr)
        updated[addr] = core_retention.advance(
            previous_status=previous["status"],
            previous_streak=previous["failureStreak"],
            previous_reason=previous["failureReason"],
            previous_started_generation=previous["startedGeneration"],
            generation=generation_id,
            scan_kind="complete",
            scan_successful=True,
            reason="shared_copy_return_below_floor",
        )
    return updated


def _persist_wallet_risk_assessment(
    db, generation_id, addr, decision, *, source, assessed_at, complete=True,
):
    """Project compatibility retention evidence into the new risk history."""
    assessment, _evidence = wallet_risk.assess_actual_copy(
        db,
        generation=generation_id,
        addr=addr,
        source=source,
        assessed_at=assessed_at,
        fallback_reason=decision.failure_reason,
        complete=complete,
        min_confirmation_hours=config.CORE_RETENTION_MIN_CONFIRMATION_HOURS,
        cumulative_high_loss_pct=config.COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT,
    )
    return assessment


def _build_retained_selection(
    db, generation_id, stamp, now_ms, retained_formation,
):
    """Materialize the exact-retuned hysteresis membership."""
    return _build_explicit_selection(
        db, generation_id, stamp, now_ms,
        forced_core_order=retained_formation.get("selected") or (),
        formation_meta=retained_formation.get("search") or {},
        effective_qualifications=retained_formation.get("qualifications") or {},
        effective_scores=retained_formation.get("scores") or {},
        effective_policies=retained_formation.get("policies") or {},
        effective_metrics=retained_formation.get("walletMetrics") or {},
        effective_score_details=retained_formation.get("scoreDetails") or {},
        effective_replay_params_hash=retained_formation.get("replayParamsHash"),
        allow_loo=False,
    )


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
    """Prepare the bounded path cache and close transient gaps before Strict/grid work."""
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
    retry_attempts = 0
    retry_limit = max(
        0, int(getattr(config, "SELECTION_PATH_RETRY_ATTEMPTS", 5) or 0),
    )
    retry_interval = max(
        0.0, float(getattr(config, "SELECTION_PATH_RETRY_INTERVAL_SEC", 10.0) or 0.0),
    )
    missing = set(meta.get("missingCoins") or ())
    while missing and retry_attempts < retry_limit:
        retry_attempts += 1
        if retry_interval:
            time.sleep(retry_interval)
        retry_fills = [row for row in fills if row.get("coin") in missing]
        price_path.ensure(
            db, retry_fills, path_start, int(now_ms),
            interval=price_path.BASE_INTERVAL, force_retry=True,
        )
        meta = price_path.coverage(db, fills, path_start, int(now_ms))
        missing = set(meta.get("missingCoins") or ())
    if retry_attempts:
        # Build/refine the complete path once after missing base markets recovered (or exhausted all retries).
        # Repeating the liquidation probe on every network attempt would waste CPU before the actual tuner.
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
        "pathRetryAttempts": retry_attempts,
    }


def _build_forced_prefix_selection(db, generation_id, stamp, now_ms, *, profiles,
                                   previous_roles, controls, held,
                                   desired_order, formation_meta,
                                   effective_qualifications=None, effective_scores=None,
                                   effective_policies=None, effective_metrics=None,
                                   effective_score_details=None,
                                   effective_replay_params_hash=None,
                                   allow_loo=False):
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
    profiles.sort(key=lambda row: follow_score.follow_score_sort_key(
        replay_by_addr.get((row.get("addr") or "").lower()) or row,
        follow_score_value=f(row.get("follow_score")),
        addr=row.get("addr") or "",
    ))
    for rank, row in enumerate(profiles, 1):
        row["rank"] = rank
    desired = tuple(dict.fromkeys((addr or "").lower() for addr in desired_order if addr))
    previous_core_members = {
        addr for addr, role in previous_roles.items() if role == selection.CORE
    }
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
    retention_hysteresis = bool(
        (formation_meta or {}).get("retentionHysteresis")
        or (
            previous_core_members
            and set(desired) == previous_core_members
        )
    )

    def retention_admissible(addr):
        qualification = dict(by_addr.get(addr, {}).get("follow_qualification") or {})
        return bool(
            retention_hysteresis
            and core_retention.qualification_failure(qualification)[0]
            in {core_retention.HEALTHY, "soft", "medium"}
        )

    invalid = [
        addr for addr in desired
        if addr not in by_addr
        or by_addr[addr].get("profile_generation") != generation_id
        or by_addr[addr].get("status") not in {"active", "qualified"}
        or (
            not _formation_core_permission(by_addr[addr].get("follow_qualification"))
            and not retention_admissible(addr)
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
                    path_rows=None, path_meta=None, compact=True,
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
        retain_advisory_incumbents=retention_hysteresis,
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
        failures = []
        if f(final_economics.get("qualificationPnl")) <= 0.0:
            failures.append("net_not_positive")
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
                # Compatibility projection only.  Formation is always certified on the standardized
                # account; Paper and Live scale the immutable strategy at runtime from their own equity.
                "basis": "standardized_projection",
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
            "maxDrawdown30d": f(final_metrics.max_drawdown),
            "liquidations30d": int(final_metrics.liquidations),
            "actionableOpenRate30d": f(final_metrics.actionable_open_rate),
            "paperActionableOpenRate30d": f(final_metrics.actionable_open_rate),
            "capacityFit30d": f(final_metrics.capacity_fit),
            "pricePathCoverage30d": f(final_result.get("price_path_coverage")),
            "maintenanceMarginCoverage30d": f(
                final_result.get("maintenance_margin_coverage")
            ),
            "failures": failures,
        }
        del final_windows
        # Economic degradation is surfaced for operator review; only missing
        # path/maintenance proof is a publication blocker.
        hard_shared_failures = {"path_coverage", "maintenance_coverage"}
        blocking_failures = (
            failures
            if not retention_hysteresis
            else [reason for reason in failures if reason in hard_shared_failures]
        )
        if failures and retention_hysteresis and not blocking_failures:
            final_strict_validation["status"] = "operator_review_degraded"
            final_strict_validation["retainedByHysteresis"] = True
        if blocking_failures:
            raise RuntimeError(
                "final_strict_copy_failed:" + ",".join(blocking_failures)
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
        selection_enabled = controls.get(addr, True)
        refreshed = row.get("profile_generation") == generation_id
        data_status = row.get("data_status") or "valid"
        selection_data_status = data_status if refreshed or data_status == "deferred_data_error" else "stale"
        active = row.get("status") in {"active", "qualified"}
        qualification = row.get("follow_qualification") or {}
        candidate_ok = refreshed and active and bool(qualification.get("eligible"))
        current_failure = (
            qualification.get("firstFailure")
            or qualification.get("status")
            or row.get("reason")
        )
        exit_kind = wallet_risk.reason_kind(current_failure)
        if (
            row.get("acct_value") is not None
            and f(row.get("acct_value")) <= max(float(config.FLAT), 1e-6)
            and int(row.get("open_position_count") or 0) == 0
        ):
            exit_kind = wallet_risk.UNAVAILABLE
        include = True
        research_only = False
        if addr in selected_set and selection_enabled:
            role = selection.CORE
            reason = transition_reasons.get(addr, "core_quality_selected")
        elif explicit_empty_core and addr in previous_core and candidate_ok:
            # Empty Core is an execution decision, not evidence deletion. A still-profitable former Core
            # opens nothing as Challenger but remains visible and receives the next retention replay.
            role = selection.CHALLENGER
            reason = qualification.get("status") or "no_robust_core_latest_evidence"
        elif addr in previous_core and exit_kind in {
            wallet_risk.HIGH, wallet_risk.UNAVAILABLE, "structural",
        }:
            role = selection.CHALLENGER
            selection_enabled = False
            reason = (
                "high_risk_isolation" if exit_kind == wallet_risk.HIGH
                else "funds_withdrawn_requalify" if exit_kind == wallet_risk.UNAVAILABLE
                else "structural_unfollowable"
            )
        elif addr in held and data_status != "valid":
            role, reason = selection.EXIT_ONLY, transition_reasons.get(addr, "exit_only_open_position")
        elif data_status != "valid":
            role = selection.QUARANTINE
            reason = "deferred_data_error" if data_status == "deferred_data_error" else "copy_data_error"
            include = False
        elif candidate_ok and addr in operational_candidate_set:
            role = selection.CHALLENGER
            if _formation_core_permission(qualification):
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
                addr=addr, role=role, enabled=selection_enabled, reason=reason,
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
                replay_copy_bt_max_liquidation_loss_pct=replay.get(
                    "copy_bt_max_liquidation_loss_pct"
                ),
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
                entry_eligible=bool(qualification.get("eligible")),
            ))
            if role == selection.CORE:
                # A recoverable operator exit has completed strict
                # requalification.  Draining remains untouched until its
                # captured cohort settles.
                db.execute(
                    "UPDATE target_controls SET enabled=1,intent='active',"
                    "intent_resolved_at=?,intent_resolution='strict_requalified',updated_at=? "
                    "WHERE lower(addr)=lower(?) AND intent='requalify'",
                    (stamp, stamp, addr),
                )
                db.execute(
                    "UPDATE wallet_registry SET risk_level='normal',risk_reasons_json='[]',"
                    "risk_confirmation_count=0,risk_first_confirmed_at=NULL,"
                    "risk_assessed_at=?,risk_block_reason=NULL,updated_at=? "
                    "WHERE lower(addr)=lower(?) AND risk_level='unavailable'",
                    (stamp, stamp, addr),
                )
                db.execute(
                    "UPDATE wallet_registry SET risk_block_reason=NULL,risk_assessed_at=?,updated_at=? "
                    "WHERE lower(addr)=lower(?) AND risk_level!='high'",
                    (stamp, stamp, addr),
                )
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
                              forced_core_order=None, formation_meta=None,
                              effective_qualifications=None, effective_scores=None,
                              effective_policies=None, effective_metrics=None,
                              effective_score_details=None,
                              effective_replay_params_hash=None,
                              allow_loo=False):
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

    position_table = _execution_position_table(db)
    held = {(addr or "").lower() for (addr,) in db.execute(
        f"SELECT DISTINCT addr FROM {position_table} WHERE status='open'"
    ).fetchall()}
    controls = {
        (addr or "").lower(): level != wallet_risk.HIGH
        for addr, level, block_reason in db.execute(
            "SELECT addr,risk_level,risk_block_reason FROM wallet_registry"
        ).fetchall()
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
        "p.actionable_open_rate,p.capacity_fit,p.open_position_count,p.copy_bt_net_pnl,p.copy_bt_win_rate,"
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
            f"SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END) FROM {position_table} GROUP BY lower(addr)"
        ).fetchall()
    }
    # watchlist.score is the published final Copy-follow score.  Selection must consume that exact value
    # rather than recomputing from a narrower row projection and creating an invisible second score line.
    watch_scores = {
        (addr or "").lower(): score
        for addr, score in db.execute("SELECT addr,score FROM watchlist").fetchall()
    }
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
        })
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


def _retune_repaired_membership_closure(
    db,
    generation_id,
    stamp,
    now_ms,
    initial_formation,
    *,
    force_entry_requalification,
):
    """Reuse the published tuned surface, then full-tune only the repaired exact membership.

    A path-cache repair must not repeat the earlier coarse wallet-count grid.  The active immutable revision
    already supplies a valid seed surface.  Re-evaluate all bounded wallets on that surface, tune the exact
    proposed membership, and replay the bounded universe again; at most the normal closure-round limit is
    needed to make membership and parameters agree.
    """
    expected = tuple(initial_formation.get("selected") or ())
    if not expected:
        return initial_formation
    candidate_rows = _quality_core_profiles(
        db, generation_id, core_only=False, now_ms=now_ms,
    )
    sigmas = auto_tune._load_sigmas(db, generation_id)
    market_ctx = auto_tune._load_market_ctx(db, generation_id)
    valuation_marks = _current_copy_valuation_marks()
    follow = params.load_follow(db)
    scanner_values = params.load_category(db, "scanner")
    follow.update({
        key: scanner_values[key] for key in COPY_POLICY_PARAM_KEYS
        if key in scanner_values
    })
    if "SMART_ADD" in follow:
        follow["ADD_STRATEGY"] = "smart" if follow["SMART_ADD"] else "hardcap"
    closure_audit = []
    max_rounds = max(
        1, int(getattr(config, "CORE_FORMATION_CLOSURE_MAX_ROUNDS", 2) or 2),
    )
    last = initial_formation
    for round_index in range(1, max_rounds + 1):
        exact = _retune_exact_membership_surface(
            db, expected, candidate_rows,
            generation_id=generation_id, stamp=stamp, round_index=round_index,
            now_ms=now_ms, base_follow=follow,
            valuation_marks=valuation_marks, sigmas=sigmas, market_ctx=market_ctx,
        )
        last = form_quality_prefix(
            db, generation_id, stamp, now_ms,
            retune=False,
            force_entry_requalification=force_entry_requalification,
            force_retune=False,
            _follow_override=exact["follow"],
        )
        actual = tuple(last.get("selected") or ())
        stable = actual == expected
        closure_audit.append({
            "round": round_index,
            "tunedInputCount": len(expected),
            "selectedCount": len(actual),
            "membershipStable": stable,
            "reason": exact.get("reason"),
        })
        if stable:
            search = dict(last.get("search") or {})
            search.update({
                "algorithm": "published_surface_exact_repair_v1",
                "retuneApplied": True,
                "formationTuneEligible": exact.get("eligible"),
                "formationTuneReason": exact.get("reason"),
                "tunePoolCount": len(candidate_rows),
                "tunedInputCount": len(expected),
                "coarseTuneRuns": 0,
                "fullTuneRuns": round_index,
                "reusedPublishedSurface": True,
                "closureRounds": closure_audit,
                "closureStable": True,
            })
            last["search"] = search
            return last
        if not actual:
            break
        expected = actual
        follow = dict(exact["follow"])
    raise RuntimeError(
        "repaired_core_membership_parameter_not_converged:"
        f"{len(expected)}:{len(tuple(last.get('selected') or ()))}"
    )


def repair_published_selection(db, generation_id=None, stamp=None, *, replace_existing=False,
                               retune_formation=False, force_entry_requalification=False,
                               reuse_tuned_surface=False):
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
        "SELECT sg.complete,sg.profile_complete,"
        "COALESCE(gmm.asof_ms,CAST(strftime('%s',sg.started_at) AS INTEGER)*1000) "
        "FROM scan_generation sg LEFT JOIN generation_market_manifest gmm "
        "ON gmm.generation=sg.generation "
        "WHERE sg.generation=? AND sg.status='published'",
        (generation_id,),
    ).fetchone()
    if not meta or not int(meta[0] or 0) or not int(meta[1] or 0):
        raise RuntimeError("selection_repair_requires_complete_generation")
    repair_now_ms = int(meta[2] or 0)
    if repair_now_ms <= 0:
        raise RuntimeError("selection_repair_generation_asof_missing")
    position_table = _execution_position_table(db)
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
            f"EXISTS (SELECT 1 FROM {position_table} cp WHERE lower(cp.addr)=lower(p.addr) "
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
            f"AND NOT EXISTS (SELECT 1 FROM {position_table} cp WHERE lower(cp.addr)=lower(profile.addr) "
            "AND cp.status='open')",
            (generation_id,),
        )
        db.commit()

    stamp = stamp or now_iso()
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
        if reuse_tuned_surface:
            formation = _retune_repaired_membership_closure(
                db, generation_id, stamp, repair_now_ms, formation,
                force_entry_requalification=force_entry_requalification,
            )
        else:
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


def _rerank_cached_pre_strict_queue(db, generation_id: str, *, now_ms: int) -> dict:
    """Apply the current score model to one generation's frozen rough evidence.

    This is deliberately narrower than rough replay: it never fetches or rewrites fills, economics,
    activity or qualification status. It only upgrades the derived score/order before a manual cached
    Strict -> Core re-formation, so a newly deployed ranking model is not trapped behind the old Top32.
    """
    profile_columns = storage.PROFILE_COLS.split(",")
    selected_columns = ",".join(f"p.{column}" for column in profile_columns)
    rows = db.execute(
        f"SELECT {selected_columns},pse.activity_json "
        "FROM profile p JOIN pre_strict_evidence pse "
        "ON pse.generation=? AND lower(pse.addr)=lower(p.addr) "
        "WHERE p.profile_generation=? AND pse.status='passed'",
        (generation_id, generation_id),
    ).fetchall()
    scored = 0
    for raw in rows:
        row = dict(zip(profile_columns, raw[:len(profile_columns)]))
        try:
            activity = json.loads(raw[len(profile_columns)] or "{}")
        except (TypeError, ValueError):
            activity = {}
        row["pre_strict_activity"] = activity if isinstance(activity, dict) else {}
        row["pre_strict_activity_json"] = raw[len(profile_columns)]
        row["score_as_of_ms"] = int(now_ms)
        score, detail = follow_score.compute_follow_score(row, stage="rough")
        if (detail or {}).get("sourceOnly"):
            raise RuntimeError("pre_strict_rerank_missing_copy_evidence")
        db.execute(
            "UPDATE profile SET score=?,rough_copy_score=? "
            "WHERE profile_generation=? AND lower(addr)=?",
            (score, score, generation_id, str(row.get("addr") or "").lower()),
        )
        scored += 1
    db.execute(
        "UPDATE pre_strict_evidence SET model_version=? WHERE generation=?",
        (pre_strict.SELECTION_MODEL_VERSION, generation_id),
    )
    queued = _finalize_pre_strict_queue(db, generation_id)
    db.commit()
    return {
        "scored": scored,
        "queued": len(queued),
        "modelVersion": pre_strict.SELECTION_MODEL_VERSION,
    }


def optimize_published_generation(
    db,
    generation_id=None,
    stamp=None,
    *,
    reuse_tuned_surface=False,
) -> dict:
    """Re-form one published generation with the synchronous quality-prefix tuner."""
    generation_id = generation_id or selection.latest_published_generation(db)
    stamp = stamp or now_iso()
    meta = db.execute(
        "SELECT COALESCE(gmm.asof_ms,CAST(strftime('%s',sg.started_at) AS INTEGER)*1000) "
        "FROM scan_generation sg LEFT JOIN generation_market_manifest gmm "
        "ON gmm.generation=sg.generation "
        "WHERE sg.generation=? AND sg.status='published'",
        (generation_id,),
    ).fetchone()
    generation_asof_ms = int(meta[0] or 0) if meta else 0
    if generation_asof_ms <= 0:
        raise RuntimeError("selection_repair_generation_asof_missing")
    rerank = _rerank_cached_pre_strict_queue(
        db, generation_id, now_ms=generation_asof_ms,
    )
    if reuse_tuned_surface:
        selection_result = repair_published_selection(
            db, generation_id, stamp=stamp, replace_existing=True,
            retune_formation=False,
            force_entry_requalification=True,
            reuse_tuned_surface=True,
        )
    else:
        selection_result = repair_published_selection(
            db, generation_id, stamp=stamp, replace_existing=True,
            retune_formation=True,
            force_entry_requalification=True,
        )
    return {
        "status": "ok" if selection_result.get("status") == "repaired" else selection_result.get("status"),
        "generation": generation_id,
        "preStrictRerank": rerank,
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
                reason=None, api_stats=None, retention_metrics=None, commit=True):
    api_stats = dict(api_stats or {})
    retention_metrics = dict(retention_metrics or {})
    db.execute(
        "INSERT INTO scan_runs (started_at,finished_at,duration_s,candidates,profiled,probed_new,added,"
        "retired,kept,rejected,n_active,full,failed,complete,kind,generation,api_requests,api_weight,"
        "outcome_reason,core_added,core_removed,core_probation,core_recovered,"
        "core_confirmed_demotion,core_safety_exit,replacement_blocked) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (started, now_iso(), round(time.time() - t0, 1), candidates, profiled, profiled, added, retired,
         kept, rejected, n_active, 1 if full else 0, failed, 1 if complete else 0,
         str(kind or "complete"), generation_id, int(api_stats.get("requests") or 0),
         int(api_stats.get("estimated_weight") or 0), str(reason)[:300] if reason else None,
         int(retention_metrics.get("coreAdded") or 0),
         int(retention_metrics.get("coreRemoved") or 0),
         int(retention_metrics.get("probation") or 0),
         int(retention_metrics.get("recovered") or 0),
         int(retention_metrics.get("confirmedDemotion") or 0),
         int(retention_metrics.get("safetyExit") or 0),
         int(bool(retention_metrics.get("replacementBlocked")))))
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


def _regate_profile_status(old_status, ok, *, complete_cached_snapshot=False):
    """Resolve cache-only qualification without reviving profiles that never got a full market snapshot."""
    if old_status == "active" or (ok and complete_cached_snapshot):
        return "active" if ok else "retired"
    return old_status


def regate(db, p, *, stamp=None, source: str = "regate", quiet: bool = False) -> int:
    """Re-apply gates() + score() on ALREADY-STORED profile metrics (no network, no re-fetch) and
    rebuild the watchlist. Thresholds (win/roiEq/dd/tpd/hold/...) can be tuned in seconds without a
    full re-sweep — the expensive part (fetching fills, building episodes) is already done."""
    now = int(time.time() * 1000)
    position_table = _execution_position_table(db)
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
            f"SELECT addr,SUM(COALESCE(unrealized_pnl,0)) FROM {position_table} "
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
        "pos_day_ratio,profit_conc,hold_skew,open_underwater,max_adds_per_ep,median_adds_per_ep,"
        "retry_transition_n,rapid_same_side_retry_n,rapid_same_side_retry_rate,"
        "loss_retry_transition_n,rapid_loss_retry_n,rapid_loss_retry_rate,"
        "rapid_retry_chain_n,rapid_retry_max_chain_episodes,loss_started_retry_chain_n,"
        "loss_started_retry_chain_losing_n,loss_started_retry_chain_lose_rate,"
        "worst_loss_pct,median_hold_s,win_rate,"
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
         ad, ar, meps, avgnotl, pdr, conc, skew, uw, mxadds, mdadds,
         retry_n, rapid_retry_n, rapid_retry_rate,
         loss_retry_n, rapid_loss_retry_n, rapid_loss_retry_rate,
         retry_chain_n, retry_chain_max, loss_chain_n, loss_chain_losing_n, loss_chain_lose_rate,
         wloss, mhold, wr,
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
             "retry_transition_n": retry_n or 0,
             "rapid_same_side_retry_n": rapid_retry_n or 0,
             "rapid_same_side_retry_rate": rapid_retry_rate or 0.0,
             "loss_retry_transition_n": loss_retry_n or 0,
             "rapid_loss_retry_n": rapid_loss_retry_n or 0,
             "rapid_loss_retry_rate": rapid_loss_retry_rate or 0.0,
             "rapid_retry_chain_n": retry_chain_n or 0,
             "rapid_retry_max_chain_episodes": retry_chain_max or 0,
             "loss_started_retry_chain_n": loss_chain_n or 0,
             "loss_started_retry_chain_losing_n": loss_chain_losing_n or 0,
             "loss_started_retry_chain_lose_rate": loss_chain_lose_rate or 0.0,
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
            structural_fills, p, source="current_generation_regate",
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
            ok, reason = metrics.gates_state(m)        # uses the stored open-position metrics
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
            _copy_profile_evidence(m, evidence_results, p)
            if (
                not current_policy.get("allowed")
                and not current_policy.get("watch")
                and m.get("evidence_status") not in {"missing", "invalid"}
            ):
                m["evidence_status"] = "economically_disqualified"
            _attach_open_copy_activity_context(m, addr, open_copy_pnl_by_addr)
            ok, reason = _profile_copy_qualification(m, p)
        ok, reason, score = _finalize_profile_qualification(m, ok, reason)
        # Only policy-only outcomes removed by this release may be safely reactivated from the current
        # cached replay. Structural/data failures still require a fresh network generation.
        complete_cached_snapshot = bool(
            float(acct or 0.0) > 0.0
            and str(m.get("data_status") or "valid").lower() == "valid"
            and str(m.get("evidence_status") or "").lower() not in {"invalid", "missing"}
        )
        status = _regate_profile_status(
            old, ok, complete_cached_snapshot=complete_cached_snapshot,
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


def _recover_missing_profile_outcomes(
    db, generation_id: str, scan_stamp, workset_n: int,
) -> int:
    """Close legacy/interrupted Profile holes with a conservative deferred outcome.

    New scans persist ``workset_member`` rows and retry a failed worker serially.  A generation produced by
    older code can still have one caught worker exception and therefore one fewer Profile audit row than its
    frozen workset.  Re-fetching that wallet after the generation market surface has moved would splice new
    evidence into an old generation, so recovery fails closed for the wallet while preserving the other
    completed work.  The next complete scan retries it normally.
    """
    if not scan_stamp or int(workset_n or 0) <= 0:
        return 0
    expected = [
        str(row[0] or "").lower() for row in db.execute(
            "SELECT addr FROM pipeline_audit WHERE stamp=? AND source='scan' "
            "AND stage='workset_member' AND addr IS NOT NULL ORDER BY rank,id",
            (scan_stamp,),
        ).fetchall()
        if row[0]
    ]
    if not expected:
        # Compatibility for the in-flight generation created immediately before exact workset persistence
        # was deployed.  Only passed/skipped Perp candidates can be proven members here; already-audited
        # priority wallets remain represented by the existing Profile rows.
        expected = [
            str(row[0] or "").lower() for row in db.execute(
                "SELECT addr FROM pipeline_audit WHERE stamp=? AND source='scan' "
                "AND stage='perp_prefilter' AND status IN ('passed','skipped') "
                "AND addr IS NOT NULL ORDER BY rank,id",
                (scan_stamp,),
            ).fetchall()
            if row[0]
        ]
    covered = {
        str(row[0] or "").lower() for row in db.execute(
            "SELECT DISTINCT addr FROM pipeline_audit WHERE stamp=? AND source='scan' "
            "AND stage='profile' AND addr IS NOT NULL",
            (scan_stamp,),
        ).fetchall()
        if row[0]
    }
    missing = [addr for addr in dict.fromkeys(expected) if addr not in covered]
    if not missing:
        return 0
    # Never paper over a broad or ambiguous interruption.  The normal checkpoint/retry path must recover
    # those generations from exact workset evidence instead of bulk-quarantining candidates.
    if len(missing) > max(1, min(8, int(workset_n) // 100)):
        raise RuntimeError(f"profile_recovery_gap_too_large:{len(missing)}")

    cols = storage.PROFILE_COLS.split(",")
    recovered_rows = []
    for addr in missing:
        raw = db.execute(
            f"SELECT {storage.PROFILE_COLS} FROM profile WHERE lower(addr)=lower(?)",
            (addr,),
        ).fetchone()
        prior = dict(zip(cols, raw)) if raw else None
        audit = db.execute(
            "SELECT status,reason,payload_json FROM pipeline_audit "
            "WHERE stamp=? AND source='scan' AND stage='perp_prefilter' "
            "AND lower(addr)=lower(?) ORDER BY id DESC LIMIT 1",
            (scan_stamp, addr),
        ).fetchone()
        try:
            payload = json.loads((audit[2] if audit else None) or "{}")
        except (TypeError, ValueError):
            payload = {}
        official = perp_prefilter.Result(
            str((audit[0] if audit else None) or "deferred_data_error"),
            str((audit[1] if audit else None) or "profile_worker_unhandled_error"),
            dict(payload.get("windows") or {}),
        )
        _status, reason, row, _ = _defer_profile(
            db,
            addr,
            prior,
            scan_stamp,
            "profile_worker_unhandled_error",
            generation_id=generation_id,
            persist=False,
        )
        row.update(_official_profile_fields(official))
        recovered_rows.append(row)
        pipeline_audit._insert_event(
            db,
            stamp=scan_stamp,
            source="scan",
            stage="profile_recovery",
            addr=addr,
            status="deferred",
            reason=reason,
            payload={"generation": generation_id, "source": "missing_worker_outcome"},
        )
    _persist_profile_batch(db, recovered_rows)
    all_profiled = sorted(covered | set(missing))
    if len(all_profiled) != int(workset_n):
        raise RuntimeError(
            f"profile_recovery_workset_mismatch:{len(all_profiled)}:{int(workset_n)}"
        )
    pipeline_audit.record_profile_snapshot(
        db, scan_stamp, "scan", all_profiled,
    )
    db.commit()
    return len(missing)


def _adopt_resumable_deferred_profiles(db, generation_id: str, scan_stamp) -> int:
    """Attach already-evaluated deferred rows to a generation interrupted before its profile audit.

    Older `_defer_profile` calls preserved the prior profile generation.  If the process died between
    completing rough replay and recording the generation-scoped profile audit, `finalize-profiled` could
    therefore mistake current `hit_page_cap` outcomes for wallets that had never been processed.  The exact
    scan timestamp plus the immutable successful Perp-prefilter audit identifies only rows evaluated by this
    run; ordinary stale profiles cannot be adopted.
    """
    if not scan_stamp:
        return 0
    result = db.execute(
        "UPDATE profile SET profile_generation=? "
        "WHERE data_status='deferred_data_error' AND evaluated_at=? "
        "AND COALESCE(profile_generation,'')<>? "
        "AND EXISTS (SELECT 1 FROM pipeline_audit a "
        "WHERE a.source='scan' AND a.stamp=? AND a.stage='perp_prefilter' "
        "AND a.status='passed' AND lower(a.addr)=lower(profile.addr))",
        (generation_id, scan_stamp, generation_id, scan_stamp),
    )
    return max(0, int(result.rowcount or 0))


def _resumable_profile_params(db, generation_id: str, now_ms: int, addrs) -> SimpleNamespace:
    """Rebuild the narrow scanner context needed to repair deferred incumbents.

    The normal profile process owns a mutable generation resolver while the
    finalizer sees an already sealed generation.  Recovery therefore uses the
    read-only resolver and the generation's terminal marks; it never falls back
    to Observer's mutable volatility cache.
    """
    p = SimpleNamespace(
        days=14,
        max_pages=5,
        min_perp=0.6,
        inactive_days=config.INACTIVE_DAYS,
        max_daily_eps=30.0,
        grid_max_adds=3.0,
        max_single_adds=config.MAX_SINGLE_ADDS_PER_EP,
        max_fills_per_ep=50,
        max_concurrent_pos=config.MAX_CONCURRENT_POS,
        exclude_hft=True,
        hft_min_hold_min=3.0,
    )
    params.apply_scanner_params(db, p)
    p.scan_generation = generation_id
    p.full_scan = False
    p.no_harvest = True
    p.rebuild_sector_policy = True
    p.source_only_profile = True
    p.defer_profile_persist = False
    p.prefer_complete_profile_cache = True
    p.profile_cache_only = False
    p.copy_bt_overrides = _copy_bt_overrides(db)
    p.margin_equity_pct = p.copy_bt_overrides.get(
        "MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT,
    )
    p.generation_market_resolver = generation_market.SealedResolver(
        db, generation_id,
    )
    snapshot_sigmas, snapshot_ctx = generation_market.load(db, generation_id)
    p.copy_bt_sigmas = snapshot_sigmas
    p.copy_bt_market_ctx = snapshot_ctx
    p.copy_bt_valuation_marks = {
        coin: f((ctx or {}).get("mark_px"))
        for coin, ctx in snapshot_ctx.items()
        if f((ctx or {}).get("mark_px")) > 0.0
    }
    window_start = int(now_ms) - int(config.PROFILE_FETCH_DAYS) * 86_400_000
    cached_coins = {
        str(fill.get("coin"))
        for addr in addrs
        for fill in _load_cached_fills(db, addr, window_start)
        if fill.get("coin") and int(fill.get("time") or 0) <= int(now_ms)
    }
    p.copyable_universe = frozenset(set(snapshot_ctx) | cached_coins)
    p.official_perp_results = {
        str(addr or "").lower(): perp_prefilter.Result(
            "passed",
            "retention_evidence_refresh",
            {"week": {"hardGate": False, "retentionRefresh": True}},
        )
        for addr in addrs
    }
    position_table = _execution_position_table(db)
    p.open_copy_pnl_by_addr = {
        str(addr or "").lower(): f(unrealized)
        for addr, unrealized in db.execute(
            f"SELECT addr,SUM(COALESCE(unrealized_pnl,0)) FROM {position_table} "
            "WHERE status='open' GROUP BY addr"
        ).fetchall()
    }
    return p


def _repair_resumable_previous_core_profiles(
    db, generation_id: str, previous_core, now_ms: int, stamp: str, *, offline=False,
) -> dict:
    """Retry only prior-Core profiles left deferred by the interrupted scan.

    A complete cache is replayed without a fill-history request.  If its source
    cursor predates the frozen generation boundary, the ordinary incremental
    transport fetches only that wallet's missing delta.  The rest of the scan
    workset and all completed tuning evidence remain untouched.
    """
    previous = sorted({str(addr or "").lower() for addr in previous_core if addr})
    if not previous:
        return {"attempted": 0, "repaired": 0, "cacheComplete": 0, "deltaRequired": 0}
    marks = ",".join("?" for _ in previous)
    rows = db.execute(
        "SELECT lower(addr),COALESCE(data_status,'valid'),reason "
        "FROM profile WHERE profile_generation=? "
        f"AND lower(addr) IN ({marks})",
        (generation_id, *previous),
    ).fetchall()
    deferred = [row[0] for row in rows if str(row[1]) == "deferred_data_error"]
    if not deferred:
        return {"attempted": 0, "repaired": 0, "cacheComplete": 0, "deltaRequired": 0}
    if offline:
        raise RuntimeError(f"core_profile_repair_requires_online:{len(deferred)}")

    p = _resumable_profile_params(db, generation_id, now_ms, deferred)
    cols = storage.PROFILE_COLS.split(",")
    cache_complete = 0
    repaired = 0
    failures = []
    for index, addr in enumerate(deferred, 1):
        raw = db.execute(
            f"SELECT {storage.PROFILE_COLS} FROM profile WHERE lower(addr)=lower(?)",
            (addr,),
        ).fetchone()
        prior = dict(zip(cols, raw)) if raw else None
        lb = db.execute(
            "SELECT account_value,week_roi,mon_roi,all_roi "
            "FROM leaderboard_staging WHERE generation=? AND lower(addr)=lower(?)",
            (generation_id, addr),
        ).fetchone()
        lb_fields = dict(zip(("account_value", "week_roi", "mon_roi", "all_roi"), lb or ()))
        window_start = int(now_ms) - int(config.PROFILE_FETCH_DAYS) * 86_400_000
        wallet_cache_complete = _complete_cached_profile_fills(
            db, addr, window_start, now_ms, universe=p.copyable_universe,
        ) is not None
        if wallet_cache_complete:
            cache_complete += 1
        _set_scan_progress(
            db, state="scanning", stage="repair_deferred_core_profile",
            candidates_scanned=index - 1, candidates_total=len(deferred),
        )
        status, reason, row, _hit_cap = _profile_one(
            db, addr, int(now_ms), p, prior, lb_fields, stamp,
            p.copyable_universe, force_full=False, persist=True,
        )
        if str(row.get("data_status") or "") != "valid":
            failures.append(f"{addr}:{reason}")
            continue
        if status == "active" and reason == "source_structure_passed":
            p.source_only_profile = False
            rough = _rough_replay_source_pool(
                db, [addr], generation_id, int(now_ms), p, stamp,
                source="finalize_repair",
            )
            p.source_only_profile = True
            current = db.execute(
                "SELECT COALESCE(data_status,'valid'),reason FROM profile "
                "WHERE profile_generation=? AND lower(addr)=lower(?)",
                (generation_id, addr),
            ).fetchone()
            if not current or current[0] != "valid":
                failures.append(f"{addr}:{current[1] if current else 'rough_missing'}")
                continue
            if int(rough.get("attempted") or 0) != 1:
                failures.append(f"{addr}:rough_not_attempted")
                continue
        pipeline_audit._insert_event(
            db, stamp=stamp, source="finalize_repair", stage="profile",
            addr=addr, status="passed", reason="deferred_core_profile_repaired",
            payload={
                "generation": generation_id,
                "fillTransport": (
                    "complete_cache" if wallet_cache_complete else "bounded_delta"
                ),
            },
        )
        repaired += 1
        db.commit()
    _set_scan_progress(
        db, state="scanning", stage="repair_deferred_core_profile",
        candidates_scanned=len(deferred), candidates_total=len(deferred),
    )
    if failures:
        # Addresses stay private in operator logs; the durable per-wallet rows
        # already carry the concrete failure reason for the next resume.
        raise RuntimeError(f"core_profile_repair_incomplete:{len(failures)}")
    return {
        "attempted": len(deferred),
        "repaired": repaired,
        "cacheComplete": cache_complete,
        "deltaRequired": len(deferred) - cache_complete,
    }


def _repair_resumable_pinned_strict_paths(
    db, generation_id: str, pinned_core, now_ms: int, stamp: str, *, offline=False,
) -> dict:
    """Retry only operator-starred Core whose winning-surface strict path was incomplete.

    Count-first and quick-surface evidence is path-independent and remains reusable.  Once the bounded
    path cache changes, only strict-finalist, individual and final-shared rows are invalidated; this avoids
    rerunning the completed profile phase or discarding the inexpensive count search.
    """
    pinned = sorted({str(addr or "").lower() for addr in pinned_core if addr})
    if not pinned:
        return {
            "attempted": 0, "pathRows": 0, "missingCoins": 0,
            "strictEvidenceInvalidated": 0,
        }
    marks = ",".join("?" for _ in pinned)
    deferred = [
        str(row[0]).lower()
        for row in db.execute(
            "SELECT addr FROM pre_strict_evidence WHERE generation=? "
            f"AND lower(addr) IN ({marks}) AND strict_status='deferred' "
            "AND strict_first_failure='copy_path_incomplete'",
            (generation_id, *pinned),
        ).fetchall()
    ]
    if not deferred:
        return {
            "attempted": 0, "pathRows": 0, "missingCoins": 0,
            "strictEvidenceInvalidated": 0,
        }
    if offline:
        raise RuntimeError(
            f"pinned_core_strict_path_repair_requires_online:{len(deferred)}"
        )
    _set_scan_progress(
        db, state="scanning", stage="repair_deferred_pinned_strict_path",
        candidates_scanned=0, candidates_total=len(deferred),
    )
    path_summary = _prefetch_selection_paths(
        db, deferred, int(now_ms), generation_id,
    )
    deleted = db.execute(
        "DELETE FROM formation_prefix_evidence WHERE generation=? "
        "AND policy_version=? AND (params_hash LIKE 'strict-finalist:%' "
        "OR params_hash LIKE 'individual:%' OR params_hash LIKE 'final-shared:%')",
        (generation_id, _FORMATION_PREFIX_CACHE_POLICY),
    ).rowcount
    deferred_marks = ",".join("?" for _ in deferred)
    db.execute(
        "UPDATE pre_strict_evidence SET strict_status='frozen_top16',"
        "strict_first_failure=NULL WHERE generation=? "
        f"AND lower(addr) IN ({deferred_marks})",
        (generation_id, *deferred),
    )
    for addr in deferred:
        pipeline_audit._insert_event(
            db, stamp=stamp, source="finalize_repair",
            stage="strict_path_repair", addr=addr, status="retried",
            reason="operator_starred_path_repair",
            payload={
                "generation": generation_id,
                "pathRows": int(path_summary.get("pathRows") or 0),
                "missingCoins": int(path_summary.get("missingCoins") or 0),
                "pathRetryAttempts": int(
                    path_summary.get("pathRetryAttempts") or 0
                ),
            },
        )
    db.commit()
    _set_scan_progress(
        db, state="scanning", stage="repair_deferred_pinned_strict_path",
        candidates_scanned=len(deferred), candidates_total=len(deferred),
    )
    return {
        "attempted": len(deferred),
        "pathRows": int(path_summary.get("pathRows") or 0),
        "coverage": float(path_summary.get("coverage") or 0.0),
        "missingCoins": int(path_summary.get("missingCoins") or 0),
        "pathRetryAttempts": int(path_summary.get("pathRetryAttempts") or 0),
        "strictEvidenceInvalidated": max(0, int(deleted or 0)),
    }


def finalize_profiled_generation(
    db, generation_id=None, stamp=None, *, retune=True, offline=False,
) -> dict:
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
        "SELECT sg.status,sg.leaderboard_valid,sg.workset_n,sg.leaderboard_rows,"
        "sg.metrics_json,sg.started_at,"
        "COALESCE(gmm.asof_ms,CAST(strftime('%s',sg.started_at) AS INTEGER)*1000) "
        "FROM scan_generation sg LEFT JOIN generation_market_manifest gmm "
        "ON gmm.generation=sg.generation WHERE sg.generation=?",
        (generation_id,),
    ).fetchone()
    if not meta or meta[0] in {"published", "failed"} or not int(meta[1] or 0):
        raise RuntimeError("generation_not_resumable")
    recovered_missing = _recover_missing_profile_outcomes(
        db, generation_id, meta[5], int(meta[2] or 0),
    )
    adopted_deferred = _adopt_resumable_deferred_profiles(
        db, generation_id, meta[5],
    )
    if adopted_deferred:
        db.commit()
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
    candidate_count = int(db.execute(
        "SELECT COUNT(*) FROM leaderboard_staging "
        "WHERE generation=? AND is_candidate=1",
        (generation_id,),
    ).fetchone()[0] or 0)

    # Resuming a cached generation must replay the same immutable evidence horizon that the original scan
    # sealed. Using the recovery day's clock would silently add an empty tail and could trigger fresh market
    # fetches, changing both count selection and parameter economics.
    now_ms = int(meta[6] or 0)
    if now_ms <= 0:
        raise RuntimeError("profile_generation_asof_missing")
    # A profile worker failure previously skipped sealing.  Only after every frozen member and immutable
    # generation invariant above is proven may recovery seal the already-built surface for strict replay.
    market_snapshot = generation_market.seal(db, generation_id)
    previous_core = selection.published_core_membership(db) or []
    pinned_core_order = _active_pinned_core_order(db)
    repair_summary = _repair_resumable_previous_core_profiles(
        db, generation_id, previous_core, now_ms, stamp, offline=bool(offline),
    )
    repair_summary["pinnedStrictPath"] = _repair_resumable_pinned_strict_paths(
        db, generation_id, pinned_core_order, now_ms, stamp,
        offline=bool(offline),
    )
    repair_summary["recoveredMissing"] = recovered_missing
    repair_summary["marketSnapshotCoins"] = int(market_snapshot.get("coins") or 0)
    if repair_summary.get("repaired"):
        profile_coverage = _profiled_generation_coverage(db, generation_id, meta[5])
    pre_strict_counts = _pre_strict_counts(db, generation_id)
    _set_scan_progress(
        db, state="scanning", stage="prepare_selection_candidates",
        candidates_scanned=profile_total, candidates_total=profile_total,
    )
    refresh_watchlist(
        db, stamp, leaderboard_generation=generation_id, commit=False,
    )
    db.rollback()
    formation = form_quality_prefix(
        db, generation_id, stamp, now_ms,
        retune=bool(retune), force_retune=bool(retune),
        retention_addrs=pinned_core_order,
    )
    _assert_automatic_formation_tuned(
        formation, required=bool(retune),
    )
    recommended_core_order = tuple(formation.get("selected") or ())
    retention_decisions = _complete_retention_decisions(
        db, generation_id, pinned_core_order, formation,
        strict_deferred_mode="defer",
    )
    desired_retained = _effective_core_order_from_addrs(
        pinned_core_order, recommended_core_order, retention_decisions,
    )
    retained_incumbents = {
        addr for addr in pinned_core_order
        if retention_decisions[addr].retain_enabled
    }
    replacement_gate = {
        "eligible": True,
        "reason": "winning_prefix_includes_effective_incumbents",
    }
    if tuple(desired_retained) != recommended_core_order:
        replacement_gate = {
            "eligible": False,
            "reason": "operator_starred_core_retained",
        }
        formation = _retention_evidence_formation(
            formation, desired_retained,
            replacement_gate=replacement_gate,
            decisions=retention_decisions,
        )
    publication_core_order = tuple(formation.get("selected") or ())
    # Recovery is deliberately cache-first.  The sealed formation evidence already owns the frozen Top16,
    # its local parameter surface and the winning final count.  Prefetching the wider Top32 before reading
    # that evidence needlessly decoded every candidate fill and rebuilt price trajectories (the exact
    # multi-hundred-MiB high-water allocation this recovery path is intended to avoid).  Once formation has
    # selected its bounded prefix, prepare paths only for the wallets that can actually reach publication.
    # A genuine formation cache miss still prepares its own strict-finalist paths in auto_tune; this final
    # selected-prefix pass merely keeps network work outside the atomic publication transaction.
    if publication_core_order and not offline:
        _set_scan_progress(
            db, stage="prefetch_selection_paths",
            candidates_scanned=len(publication_core_order),
            candidates_total=len(publication_core_order),
        )
        _prefetch_selection_paths(
            db, publication_core_order, now_ms, generation_id,
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
        )
        proposed_core = {
            item.addr for item in rows
            if item.role == selection.CORE and item.enabled
        }
        protected_removed = [
            addr for addr in retained_incumbents if addr not in proposed_core
        ]
        if protected_removed:
            raise RuntimeError(
                f"core_retention_overlay_not_materialized:{len(protected_removed)}"
            )
        retention_decisions = _apply_shared_retention_failure(
            db, generation_id, pinned_core_order, retention_decisions, marginal,
        )
        rows = _decorate_retention_rows(rows, pinned_core_order, retention_decisions)
        for addr, decision in retention_decisions.items():
            apply_wallet_retention_decision(
                db, addr, decision, generation=generation_id,
                stamp=publication_stamp,
            )
            _persist_wallet_risk_assessment(
                db, generation_id, addr, decision,
                source="complete", assessed_at=publication_stamp,
            )
        _assert_margin_equity_snapshot(db, expected_margin_equity_pct)
        valid = int(profile_coverage["valid"])
        deferred = int(profile_coverage["deferred"])
        rejected = int(profile_coverage["rejected"])
        core_count = sum(1 for item in rows if item.role == selection.CORE)
        challenger_count = sum(
            1 for item in rows if item.role == selection.CHALLENGER
        )
        resumed_metrics = {
            **generation_metrics,
            "coarseRecallPassed": candidate_count,
            "perpPrefilterPassed": workset_n,
            "structurePassed": pre_strict_counts["roughCopyCompleted"],
            **pre_strict_counts,
            "profileValid": valid,
            "profileDeferred": deferred,
            "profileRejected": rejected,
            "selectionCore": core_count,
            "selectionChallenger": challenger_count,
            "selectionSearch": marginal.search_meta or {},
            "resumedFinalize": True,
            "deferredCoreRepair": repair_summary,
            "operatorStarredRetention": len(pinned_core_order),
        }
        db.execute(
            "UPDATE scan_generation SET metrics_json=? WHERE generation=?",
            (json.dumps(resumed_metrics, sort_keys=True), generation_id),
        )
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
                **(marginal.search_meta or {}),
                "recommendedCore": list(recommended_core_order),
                "effectiveCore": list(current_core),
                "operatorStarredRetention": len(pinned_core_order),
                "marketSnapshot": market_validation,
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
    try:
        pruned = _prune_discovery_cache(db)
        pipeline_audit.record_prune_summary(db, stamp, "resume_finalize", pruned)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - publication is already durable; maintenance retries later
        db.rollback()
        pipeline_audit._insert_event(
            db, stamp=stamp, source="resume_finalize", stage="prune",
            status="deferred", reason=str(exc)[:300], payload={"generation": generation_id},
        )
        db.commit()
    _set_scan_progress(
        db, state="idle", stage="persist", candidates_scanned=profile_total,
        candidates_total=profile_total,
    )
    _set_scanner_proc(db, "idle", {"last_scan_at": now_iso(), "active": len(current_core)})
    existing_run = db.execute(
        "SELECT 1 FROM scan_runs WHERE generation=? AND kind='complete' LIMIT 1",
        (generation_id,),
    ).fetchone()
    if not existing_run:
        try:
            started_epoch = calendar.timegm(
                time.strptime(str(meta[5]), "%Y-%m-%dT%H:%M:%SZ")
            )
        except (TypeError, ValueError):
            started_epoch = time.time()
        business_rejected = int(db.execute(
            "SELECT COUNT(*) FROM profile WHERE profile_generation=? "
            "AND status IN ('rejected','retired')",
            (generation_id,),
        ).fetchone()[0] or 0)
        previous_set = {str(addr or "").lower() for addr in previous_core}
        current_set = {str(addr or "").lower() for addr in current_core}
        _record_run(
            db, str(meta[5]), started_epoch,
            candidate_count, profile_total,
            len(current_set - previous_set), len(previous_set - current_set),
            len(previous_set & current_set), business_rejected, len(current_core),
            full=True, failed=0, complete=True, kind="complete",
            generation_id=generation_id, reason="resumed_profiled_generation",
            retention_metrics={
                "coreAdded": len(current_set - previous_set),
                "coreRemoved": len(previous_set - current_set),
                "probation": sum(
                    decision.status == core_retention.PROBATION
                    for decision in retention_decisions.values()
                ),
                "recovered": sum(
                    decision.action == "recovered"
                    for decision in retention_decisions.values()
                ),
                "confirmedDemotion": sum(
                    decision.action == "confirmed_demotion"
                    for decision in retention_decisions.values()
                ),
                "safetyExit": sum(
                    decision.action == "immediate_demotion"
                    for decision in retention_decisions.values()
                ),
                "replacementBlocked": not bool(
                    replacement_gate.get("eligible", True)
                ),
            },
            commit=False,
        )
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
    position_table = _execution_position_table(db)
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
        for addr in (selection.published_core_membership(db) or ())
        if addr
    }
    held = {
        str(addr or "").lower()
        for (addr,) in db.execute(
            f"SELECT DISTINCT addr FROM {position_table} WHERE status='open'"
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
    """Fill empty seats from strict proposals without replacing incumbents."""
    previous = tuple(dict.fromkeys(
        str(addr or "").lower() for addr in (previous_order or ()) if addr
    ))
    proposed = tuple(dict.fromkeys(
        str(addr or "").lower() for addr in (proposed_order or ()) if addr
    ))
    previous_set = set(previous)
    proposed_set = set(proposed)
    removed = tuple(addr for addr in previous if addr not in proposed_set)
    proposed_added = tuple(addr for addr in proposed if addr not in previous_set)
    cap = max(1, min(
        int(config.MAX_TARGETS),
        int(getattr(config, "CORE_TARGET_MAX_N", 16)),
        int(getattr(config, "CORE_INITIAL_MAX_N", 16)),
    ))
    added = proposed_added[:max(0, cap - len(previous))]
    if added:
        mode = "promote"
        selected = previous + added
        reason = "daily_fill_open_core_seats"
    elif removed:
        mode = "carry"
        selected = previous
        reason = "daily_proposal_would_remove_core"
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
        for addr in (selection.published_core_membership(db) or ())
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
        position_table = _execution_position_table(db)
        open_copy_pnl_by_addr = {
            str(addr or "").lower(): f(unrealized)
            for addr, unrealized in db.execute(
                f"SELECT addr,SUM(COALESCE(unrealized_pnl,0)) FROM {position_table} "
                "WHERE status='open' GROUP BY addr"
            ).fetchall()
        }
        p.open_copy_pnl_by_addr = dict(open_copy_pnl_by_addr)
        p.defer_profile_persist = True
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
                    generation_id=generation_id, persist=False,
                )
            gate = perp_results.get(addr)
            if gate is None:
                return addr, prior, _defer_profile(
                    db, addr, prior, stamp, "official_perp_evidence_missing",
                    generation_id=generation_id, persist=False,
                )
            if gate.deferred:
                return addr, prior, _defer_profile(
                    db, addr, prior, stamp, gate.reason,
                    generation_id=generation_id, persist=False,
                )
            # The frozen daily pool already owns a complete 37-day cache, so even an official business-gate
            # failure or normal evidence-building state must consume its cheap delta. A zeroed wallet commonly
            # fails Portfolio first; skipping here would hide the liquidation fill needed by hard safety below.
            result = _profile_one(
                db, addr, now_ms, p, prior, lbs.get(addr, {}), stamp, universe,
                force_full=False,
            )
            # Portfolio week volume is a new-wallet download decision only. Every frozen strict
            # Core/Challenger already owns a complete cache and must be requalified from fills even when its
            # current cheap-recall telemetry falls below the new-wallet floor.
            return addr, prior, result

        workers = max(1, int(getattr(p, "workers", 4) or 4))
        done = 0
        persist_rows = []
        persist_batch_size = max(8, workers * 2)

        def flush_daily_profiles():
            if persist_rows:
                _persist_profile_batch(db, persist_rows)
                persist_rows.clear()

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
                            generation_id=generation_id, persist=False,
                        )
                        persist_rows.append(profile)
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
                    persist_rows.append(profile)
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
                if len(persist_rows) >= persist_batch_size:
                    flush_daily_profiles()
                _set_scan_progress(
                    db, stage="challenger_score", candidates_scanned=done,
                    candidates_total=len(workset),
                )
                if done % 10 == 0:
                    _set_scanner_proc(
                        db, "scanning",
                        {"stage": "challenger_score", "scanned": done, "total": len(workset)},
                    )
        flush_daily_profiles()
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
                "AND reason='copy_single_liquidation_loss_over_8pct'",
                (generation_id,),
            ).fetchall()
        }
        severe_copy_core = previous_core & severe_copy_liquidations
        hard_safety_core |= severe_copy_core
        for addr in sorted(severe_copy_liquidations):
            pipeline_audit._insert_event(
                db, stamp=stamp, source="challenger_daily",
                stage="hard_safety", addr=addr, status="rejected",
                reason="copy_single_liquidation_loss_over_8pct",
                payload={
                    "wasCore": addr in previous_core,
                    "action": (
                        "exit_only" if addr in previous_core
                        else "exclude_from_candidate_pool"
                    ),
                    "thresholdPct": config.COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT,
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
        prepublication_high_core = set()
        risk_stamp = now_iso()
        for addr in sorted(previous_core):
            assessment, _evidence = wallet_risk.assess_actual_copy(
                db,
                generation=generation_id,
                addr=addr,
                source="challenger_daily_prepublication",
                assessed_at=risk_stamp,
                fallback_reason=(outcomes.get(addr) or {}).get("reason"),
                complete=True,
                min_confirmation_hours=config.CORE_RETENTION_MIN_CONFIRMATION_HOURS,
                cumulative_high_loss_pct=config.COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT,
            )
            if assessment.level == wallet_risk.HIGH:
                prepublication_high_core.add(addr)
        pipeline_audit._insert_event(
            db, stamp=stamp, source="challenger_daily",
            stage="wallet_risk_prepublication", status="ok",
            reason="actual_copy_risk_persisted",
            payload={
                "assessedCore": len(previous_core),
                "highRiskCore": len(prepublication_high_core),
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
        db.rollback()

        automatic_retune = _automatic_formation_retune_enabled(db)
        fixed_formation = form_quality_prefix(
            db, generation_id, stamp, now_ms,
            retune=False, force_retune=False,
            retention_addrs=previous_core_order,
        )
        daily_retention_evidence_complete = True
        try:
            daily_retention_decisions = _complete_retention_decisions(
                db, generation_id, previous_core_order, fixed_formation,
                strict_deferred_mode="retain",
            )
        except RuntimeError as exc:
            if not str(exc).startswith("core_retention_evidence_incomplete:"):
                raise
            daily_retention_evidence_complete = False
            # Daily refreshes may carry immutable Core rows when a wallet's
            # research profile was intentionally outside the mocked/bounded
            # candidate surface.  Preserve state; never invent a confirmation.
            daily_retention_decisions = {}
            for addr in previous_core_order:
                previous = wallet_retention_state(db, addr)
                daily_retention_decisions[addr] = core_retention.advance(
                    previous_status=previous["status"],
                    previous_streak=previous["failureStreak"],
                    previous_reason=previous["failureReason"],
                    previous_started_generation=previous["startedGeneration"],
                    generation=generation_id,
                    scan_kind="challenger_refresh",
                    scan_successful=False,
                    reason=previous["failureReason"],
                )
        structural_core = {
            addr for addr, decision in daily_retention_decisions.items()
            if (
                not decision.retain_enabled
                and wallet_risk.reason_kind(decision.failure_reason) == "structural"
            )
        }
        actual_catastrophic_core = {
            addr for addr in previous_core
            if wallet_risk.actual_copy_evidence(
                db, addr,
            ).get("catastrophicPositionIds")
        }
        automatic_exit_core = (
            set(hard_safety_core) | structural_core | actual_catastrophic_core
            | prepublication_high_core
        )
        fixed_core_order = tuple(
            str(addr or "").lower()
            for addr in (fixed_formation.get("selected") or ())
            if addr
        )
        fixed_core = set(fixed_core_order)
        daily_floor_order = tuple(
            addr for addr in previous_core_order if addr not in automatic_exit_core
        )
        fixed_decision = _challenger_daily_membership_decision(
            daily_floor_order, fixed_core_order,
        )
        membership_retune_triggered = False
        retune_attempted = False
        fixed_surface_promotion = False
        promotion_blocked_reason = None
        formation = fixed_formation
        publish_core_order = fixed_decision["selected"]
        if automatic_exit_core:
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
                    "structuralRemoved": len(structural_core),
                    "actualCatastropheRemoved": len(actual_catastrophic_core),
                    "actualHighRiskRemoved": len(prepublication_high_core),
                    "protectedCore": len(daily_floor_order),
                    "fixedSurfaceCore": len(fixed_core),
                    "promotionSuppressed": len(fixed_core - set(daily_floor_order)),
                },
            )
            db.commit()
        elif fixed_decision["mode"] == "promote":
            if automatic_retune:
                retune_attempted = True
                _set_scan_progress(
                    db, stage="challenger_membership_retune",
                    candidates_scanned=len(workset), candidates_total=len(workset),
                )
                tuned_formation = form_quality_prefix(
                    db, generation_id, stamp, now_ms,
                    retune=True, force_retune=True,
                    retention_addrs=previous_core_order,
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
            else:
                # A disabled auto-tune switch is a hard fixed-surface contract.  The current parameters have
                # already certified this strict profit prefix, so a membership/order change must not smuggle
                # the expensive tuner back into the generation. Congestion is resolved by the fixed-surface
                # prefix search shrinking Core, never by changing leverage, margin or add parameters.
                fixed_surface_promotion = True
                formation = fixed_formation
                publish_core_order = fixed_decision["selected"]
                pipeline_audit._insert_event(
                    db, stamp=stamp, source="challenger_daily",
                    stage="promotion_review", status="ok",
                    reason="challenger_daily_promotion_fixed_surface",
                    payload={
                        "previousCore": len(previous_core),
                        "fixedSurfaceCore": len(fixed_core),
                        "added": len(fixed_core - previous_core),
                        "removed": len(previous_core - fixed_core),
                        "autoTuneEnabled": False,
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
        effective_publish_order = (
            daily_floor_order if promotion_blocked_reason else publish_core_order
        )
        promotion_parity = _assert_daily_promotion_parity(
            db, generation_id,
            previous_core=previous_core_order,
            proposed_core=effective_publish_order,
            promotion_universe=base_promotion_universe,
            formation=formation,
        )
        if publish_core_order:
            _set_scan_progress(
                db, stage="prefetch_selection_paths",
                candidates_scanned=len(publish_core_order),
                candidates_total=len(publish_core_order),
            )
            _prefetch_selection_paths(
                db, publish_core_order, now_ms, generation_id,
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
            formation_meta={
                **dict(formation.get("search") or {}),
                # Daily membership is the effective Core: low/medium incumbents
                # remain alongside any strict new entrant.  Without this flag,
                # the pure recommended prefix rejected those incumbents before
                # the retention overlay could be published.
                "retentionHysteresis": True,
            },
            effective_qualifications=formation.get("qualifications") or {},
            effective_scores=formation.get("scores") or {},
            effective_policies=formation.get("policies") or {},
            effective_metrics=formation.get("walletMetrics") or {},
            effective_score_details=formation.get("scoreDetails") or {},
            effective_replay_params_hash=formation.get("replayParamsHash"),
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
        selection_rows = _decorate_retention_rows(
            selection_rows, previous_core, daily_retention_decisions,
        )
        for addr, decision in daily_retention_decisions.items():
            apply_wallet_retention_decision(
                db, addr, decision, generation=generation_id,
                stamp=publication_stamp,
            )
            _persist_wallet_risk_assessment(
                db, generation_id, addr, decision,
                source="challenger_daily", assessed_at=publication_stamp,
                complete=daily_retention_evidence_complete,
            )
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
        unexpected_removed = set(removed_core) - automatic_exit_core
        if unexpected_removed:
            raise RuntimeError(
                f"challenger_daily_demotion_invariant:{len(unexpected_removed)}"
            )
        strategy_reason = (
            "challenger_daily_hard_safety_exit"
            if automatic_exit_core
            else (
                "challenger_daily_promotion_retune"
                if membership_retune_triggered
                else (
                    "challenger_daily_promotion_fixed_surface"
                    if fixed_surface_promotion
                    else (
                        "challenger_daily_promotion_only_core_carried"
                        if promotion_blocked_reason
                        else "challenger_daily_evidence_refresh"
                    )
                )
            )
        )
        active_strategy = strategy_revision.create_revision(
            db, generation_id, source="challenger_daily",
            reason=strategy_reason,
            validation={
                **((marginal.search_meta or {}) if marginal is not None else {}),
                "recommendedCore": list(fixed_core_order),
                "effectiveCore": list(current_core),
                "marketSnapshot": market_validation,
                "baseFullGeneration": base_generation,
                "promotionOnly": True,
                "promotionBlockedReason": promotion_blocked_reason,
                "promotionParity": promotion_parity,
                "verifiedSourceBlowups": len(verified_source_blowups),
                "severeCopyLiquidations": len(severe_copy_liquidations),
                "hardSafetyCoreRemoved": len(hard_safety_core),
                "structuralCoreRemoved": len(structural_core),
                "actualCatastropheCoreRemoved": len(actual_catastrophic_core),
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
                "fixedSurfacePromotion": fixed_surface_promotion,
                "promotionOnly": True,
                "promotionBlockedReason": promotion_blocked_reason,
                "promotionParity": promotion_parity,
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
            "promotionParity": promotion_parity,
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
def scan(db, p):
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
        old_core = selection.published_core_membership(db) or []
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
    recall_cand_raw = [r[0] for r in db.execute(
        f"SELECT addr FROM leaderboard_staging WHERE generation=? AND is_candidate=1 "
        f"ORDER BY {order} DESC",
        (generation_id,),
    ).fetchall()]
    _leaderboard_recall_audit(db, generation_id, stamp, p)
    # Upgrade legacy high-confidence decisions once, then enforce the permanent boundary before any
    # per-wallet Portfolio/history request. Leaderboard staging remains intact as coarse-recall audit.
    collection_blacklist.bootstrap_from_profiles(db, stamp=stamp)
    recall_cand, blacklisted_recall = collection_blacklist.filter_addresses(db, recall_cand_raw)
    db.commit()
    current_selection_generation = selection.latest_published_generation(db)
    core_addrs = selection.published_core_membership(db) or []
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
    position_table = _execution_position_table(db)
    open_copy_pnl_by_addr = {
        str(addr or "").lower(): f(unrealized)
        for addr, unrealized in db.execute(
            f"SELECT addr,SUM(COALESCE(unrealized_pnl,0)) FROM {position_table} "
            "WHERE status='open' GROUP BY addr"
        ).fetchall()
    }
    position_addrs = sorted(open_copy_pnl_by_addr)
    # Cheap discovery gates only control *new* expensive collection. Current executable/observed roles and
    # open-position owners always receive a fresh retention replay, even when their latest official week or
    # Portfolio mix temporarily misses the discovery surface. Recently removed Core wallets also remain on
    # this evidence lane: an empty publication must stop execution without erasing recovery proof.
    former_core_addrs = _recent_former_core_addrs(db, as_of=stamp)
    retention_addrs_raw = (
        set(core_addrs) | set(challenger_addrs)
        | set(position_addrs) | set(former_core_addrs)
    )
    retention_addrs, blacklisted_retention = collection_blacklist.filter_addresses(
        db, retention_addrs_raw,
    )
    retention_addrs = set(retention_addrs)
    blacklisted_generation = {**blacklisted_recall, **blacklisted_retention}
    if blacklisted_retention:
        profile_columns = storage.PROFILE_COLS.split(",")
        for addr, blocked_reason in blacklisted_retention.items():
            prior_values = db.execute(
                f"SELECT {storage.PROFILE_COLS} FROM profile WHERE addr=?", (addr,),
            ).fetchone()
            prior = dict(zip(profile_columns, prior_values)) if prior_values else None
            _reject_prefilter_profile(
                db, addr, prior, stamp, generation_id, blocked_reason,
            )
            pipeline_audit._insert_event(
                db, stamp=stamp, source="scan", stage="collection_blacklist",
                addr=addr, status="rejected", reason=blocked_reason,
                payload={"generation": generation_id, "apiCalls": 0},
            )
        db.commit()

    # Hybrid fill-first transport:
    #   * new/incomplete caches keep the eager official volume precheck, avoiding multi-page history for an
    #     obvious Perp-volume miss;
    #   * complete caches refresh their delta and structure first, then resolve the volume gate lazily only
    #     when structure survives. This is the high-value reuse path for repeated HFT/grid/DCA rejects.
    prefilter_started_at = time.time()
    desired_cache_start_ms = now_ms - config.PROFILE_FETCH_DAYS * 86400_000
    bootstrap_recall = set(_incomplete_fill_cache_addrs(
        db, recall_cand, desired_cache_start_ms,
    ))
    eager_prefilter_addrs = [
        addr for addr in recall_cand
        if addr in bootstrap_recall and addr not in retention_addrs
    ]
    _set_scan_progress(
        db, stage="perp_prefilter", candidates_scanned=0,
        candidates_total=len(eager_prefilter_addrs),
    )
    perp_results = _run_perp_prefilter(
        db, eager_prefilter_addrs, p, stamp, allow_cache=False,
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
    prefilter_done_at = time.time()
    prefilter_api_stats = rest.request_stats()
    eager_set = set(eager_prefilter_addrs)
    # Complete-cache candidates are intentionally optimistic here: their current-generation local structure
    # and lazy official proof are resolved inside `_profile_one`. Eager bootstrap candidates retain the old
    # requirement that Portfolio pass before history collection.
    cand = [
        addr for addr in recall_cand
        if addr not in eager_set or (perp_results.get(addr) and perp_results[addr].passed)
    ]
    print(
        f"  coarse recall {len(recall_cand_raw)} · permanent automation blacklist "
        f"{len(blacklisted_generation)} · eager Portfolio {len(eager_prefilter_addrs)} "
        f"({sum(bool(perp_results.get(addr) and perp_results[addr].passed) for addr in eager_prefilter_addrs)} passed) "
        f"· cached fill-first {len(recall_cand) - len(eager_prefilter_addrs)}",
        flush=True,
    )
    # Freeze the open-copy PnL surface for the generation. Worker threads use it only to distinguish a
    # profitable carried mirrored episode from a dormant/losing wallet; it never bypasses economic/risk gates.
    p.open_copy_pnl_by_addr = dict(open_copy_pnl_by_addr)
    cand_set = set(cand)
    recent = db.execute(
        "SELECT duration_s,COALESCE(profiled,probed_new) FROM scan_runs "
        "WHERE COALESCE(profiled,probed_new)>0 AND complete=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    estimated_profile_s = max(1.0, min(120.0, (f(recent[0]) / int(recent[1])))) if recent else 12.0
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
    pipeline_audit.record_workset_members(
        db,
        stamp,
        "scan",
        workset_info["workset"],
        full_refetch_addrs=workset_info["refresh"]["full_refetch"],
    )
    workset_metrics = {
        "estimatedProfileSec": estimated_profile_s,
        "warmupBackfillDue": len(warmup_backfill_addrs),
        "warmupBackfillScheduled": len(migration_backfill),
        "formerCoreRecheck": len(former_core_addrs),
        "eagerPortfolioPrefilter": len(eager_prefilter_addrs),
        "cachedFillFirst": len(recall_cand) - len(eager_prefilter_addrs),
        "collectionBlacklisted": len(blacklisted_generation),
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
    p.defer_profile_persist = True

    def _work(addr):
        prior = priors.get(addr)
        return addr, prior, _profile_one(
            db, addr, now_ms, p, prior, lbs.get(addr, {}), stamp, universe,
            force_full=addr in full_refetch,
        )

    done = 0
    priority_done_at = time.time() if not priority_addrs else None
    def _profile_batch(batch):
        nonlocal done, priority_done_at, added, retired, rejected, kept, failed
        nonlocal profiled_ok, deferred_profiles, valid_profiles
        if not batch:
            return
        persist_rows = []
        persist_batch_size = max(8, workers * 2)

        def flush_persist():
            if persist_rows:
                _persist_profile_batch(db, persist_rows)
                persist_rows.clear()

        def consume_profile_result(addr, prior, result):
            nonlocal added, retired, rejected, kept
            nonlocal profiled_ok, deferred_profiles, valid_profiles
            status, reason, m, _hit_cap = result
            resolved_official = _profile_official_result(m)
            if resolved_official is None:
                # Compatibility for tests/offline profile adapters. Production `_profile_one` always
                # seals one of eager/local/fallback/structural-skip into the row.
                resolved_official = perp_results.get(addr) or perp_prefilter.Result(
                    "passed" if status == "active" else "skipped",
                    "profile_adapter_without_prefilter_result",
                    {"scanResolution": {"source": "profile_adapter"}},
                )
            perp_results[addr] = resolved_official
            profiled_ok += 1
            profiled_addrs.append(addr)
            persist_rows.append(m)
            if len(persist_rows) >= persist_batch_size:
                flush_persist()
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

        worker_failures = []

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
                        # A shared SQLite connection may reject one concurrent operation even though the
                        # wallet/API evidence is sound.  Do not create a permanent hole in this generation;
                        # retry after the pool has fully closed, when this connection is main-thread-only.
                        worker_failures.append((expected_addr, type(exc).__name__))
                        print(
                            f"  [{done}/{len(workset)}] RETRY: profile worker "
                            f"{type(exc).__name__}",
                            flush=True,
                        )
                        continue
                    consume_profile_result(addr, prior, (status, reason, m, hit_cap))
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
        flush_persist()

        # Retry worker-only failures after every worker has released the shared connection.  A second
        # exception becomes an explicit deferred outcome: formation excludes it, but the complete workset
        # remains auditable and the next generation can retry normally.
        for addr, first_error in worker_failures:
            prior = priors.get(addr)
            try:
                _addr, retry_prior, result = _work(addr)
            except Exception as exc:  # noqa: BLE001
                resolved = perp_results.get(addr) or perp_prefilter.Result(
                    "deferred_data_error",
                    "profile_worker_unhandled_error",
                    {"scanResolution": {"source": "profile_worker_deferred"}},
                )
                status, reason, row, _ = _defer_profile(
                    db,
                    addr,
                    prior,
                    stamp,
                    f"profile_worker_error:{type(exc).__name__}",
                    generation_id=generation_id,
                    persist=False,
                )
                row.update(_official_profile_fields(resolved))
                result = (status, reason, row, False)
                retry_prior = prior
                pipeline_audit._insert_event(
                    db,
                    stamp=stamp,
                    source="scan",
                    stage="profile_worker_retry",
                    addr=addr,
                    status="deferred",
                    reason=f"{first_error}:{type(exc).__name__}",
                    payload={"attempts": 2},
                )
            else:
                pipeline_audit._insert_event(
                    db,
                    stamp=stamp,
                    source="scan",
                    stage="profile_worker_retry",
                    addr=addr,
                    status="recovered",
                    reason=first_error,
                    payload={"attempts": 2},
                )
            consume_profile_result(addr, retry_prior, result)
            _set_scan_progress(
                db, stage="score_filter", candidates_scanned=done,
                candidates_total=len(workset),
            )
        flush_persist()

    _profile_batch(list(workset))
    # Replace the temporary eager-only audit with one complete generation surface. Cached candidates now
    # contribute their local proof, Portfolio fallback, or structural short-circuit decision here.
    _record_perp_prefilter_results(db, recall_cand, perp_results, stamp, p=p)
    cand = [
        addr for addr in recall_cand
        if perp_results.get(addr) is not None and perp_results[addr].passed
    ]
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
    # Record durable per-wallet coverage before the market-scope audit and strict formation.  Either later
    # step can fail (including a host-level OOM), and `finalize-profiled` must still be able to prove that
    # every wallet in the frozen workset already reached a terminal profile/deferred outcome.
    pipeline_audit.record_profile_snapshot(db, stamp, "scan", profiled_addrs)
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
    if complete and bool(getattr(p, "defer_finalize", False)):
        # The profile process has held millions of decoded fill objects over many hours. Persist the complete
        # handoff and replace this process before final formation so Python's high-water allocator, worker
        # threads and transient API surfaces cannot be inherited by the memory-heavy replay stage.
        handoff_stamp = now_iso()
        try:
            prior_metrics = json.loads(db.execute(
                "SELECT metrics_json FROM scan_generation WHERE generation=?",
                (generation_id,),
            ).fetchone()[0] or "{}")
        except (TypeError, ValueError):
            prior_metrics = {}
        handoff_metrics = {
            **prior_metrics,
            "profileStageComplete": True,
            "profileDurationSec": round(profile_done_at - prefilter_done_at, 3),
            "coarseRecallPassed": len(recall_cand),
            "collectionBlacklisted": len(blacklisted_generation),
            "profileValid": valid_profiles,
            "profileDeferred": deferred_profiles,
            "profileRejected": rejected,
            "marketScopeAudit": scope_audit,
            "marketSnapshotProfiled": market_snapshot_audit,
            "marginEquityPct": float(p.margin_equity_pct),
            "initialMarginEquity": float(config.INITIAL_BALANCE),
        }
        generation.mark_generation_ready(
            db,
            generation_id,
            profile_total=profiled_ok,
            profile_valid=valid_profiles,
            profile_deferred=deferred_profiles,
            profile_rejected=rejected,
            profile_complete=True,
            ready_at=handoff_stamp,
        )
        db.execute(
            "UPDATE scan_generation SET metrics_json=? WHERE generation=?",
            (json.dumps(handoff_metrics, sort_keys=True), generation_id),
        )
        _set_scan_progress(
            db, state="scanning", stage="finalize_handoff",
            candidates_scanned=len(workset), candidates_total=len(workset),
        )
        _set_scanner_proc(db, "scanning", {
            "stage": "finalize_handoff", "generation": generation_id,
            "profiled": profiled_ok,
        })
        db.commit()
        return {
            "status": "profiled",
            "generation": generation_id,
            "retune": bool(_automatic_formation_retune_enabled(db)),
            "profiled": profiled_ok,
        }
    published = False
    publication_stamp = None
    previous_core = selection.published_core_membership(db) or []
    pinned_core_order = _active_pinned_core_order(db)
    n_active = len(previous_core)
    if complete:
        _set_scan_progress(db, stage="rebuild_watchlist", candidates_scanned=len(workset))
        selection_mode = str(
            params.get(db, "FOLLOW_SELECTION_MODE", config.FOLLOW_SELECTION_MODE) or "auto"
        ).lower()
        try:
            _assert_margin_equity_snapshot(db, p.margin_equity_pct)
            formation = None
            recommended_core_order = ()
            retention_decisions = {}
            retained_incumbents = set()
            replacement_gate = {
                "eligible": True, "reason": "not_applicable",
            }
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
                    retention_addrs=pinned_core_order,
                )
                _assert_automatic_formation_tuned(
                    formation,
                    required=bool(automatic_retune),
                )
                recommended_core_order = tuple(formation.get("selected") or ())
                retention_decisions = _complete_retention_decisions(
                    db, generation_id, pinned_core_order, formation,
                    strict_deferred_mode="defer",
                )
                desired_retained = _effective_core_order_from_addrs(
                    pinned_core_order, recommended_core_order, retention_decisions,
                )
                retained_incumbents = {
                    addr for addr in pinned_core_order
                    if retention_decisions[addr].retain_enabled
                }
                if tuple(desired_retained) != recommended_core_order:
                    replacement_gate = {
                        "eligible": False,
                        "reason": "operator_starred_core_retained",
                    }
                    formation = _retention_evidence_formation(
                        formation, desired_retained,
                        replacement_gate=replacement_gate,
                        decisions=retention_decisions,
                    )
                publication_core_order = tuple(formation.get("selected") or ())
                if publication_core_order:
                    _set_scan_progress(
                        db, stage="prefetch_selection_paths",
                        candidates_scanned=len(publication_core_order),
                        candidates_total=len(publication_core_order),
                    )
                    _prefetch_selection_paths(
                        db, publication_core_order, now_ms, generation_id,
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
                    f"SELECT DISTINCT addr FROM {position_table} WHERE status='open'"
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
                    db, generation_id, selection_stamp, now_ms,
                    forced_core_order=(formation or {}).get("selected") or (),
                    formation_meta=(formation or {}).get("search") or {},
                    effective_qualifications=(formation or {}).get("qualifications") or {},
                    effective_scores=(formation or {}).get("scores") or {},
                    effective_policies=(formation or {}).get("policies") or {},
                    effective_metrics=(formation or {}).get("walletMetrics") or {},
                    effective_score_details=(formation or {}).get("scoreDetails") or {},
                    effective_replay_params_hash=(formation or {}).get("replayParamsHash"),
                )
                proposed_core = {
                    row.addr for row in selection_rows
                    if row.role == selection.CORE and row.enabled
                }
                protected_removed = [
                    addr for addr in retained_incumbents if addr not in proposed_core
                ]
                if protected_removed:
                    raise RuntimeError(
                        f"core_retention_overlay_not_materialized:{len(protected_removed)}"
                    )
                retention_decisions = _apply_shared_retention_failure(
                    db, generation_id, pinned_core_order, retention_decisions, marginal,
                )
                selection_rows = _decorate_retention_rows(
                    selection_rows, pinned_core_order, retention_decisions,
                )
                for addr, decision in retention_decisions.items():
                    apply_wallet_retention_decision(
                        db, addr, decision, generation=generation_id,
                        stamp=selection_stamp,
                    )
                    _persist_wallet_risk_assessment(
                        db, generation_id, addr, decision,
                        source="complete", assessed_at=selection_stamp,
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
                    "retention": {
                        "probation": sum(
                            decision.status == core_retention.PROBATION
                            for decision in retention_decisions.values()
                        ),
                        "recovered": sum(
                            decision.action == "recovered"
                            for decision in retention_decisions.values()
                        ),
                        "confirmedDemotion": sum(
                            decision.action == "confirmed_demotion"
                            for decision in retention_decisions.values()
                        ),
                        "safetyExit": sum(
                            decision.action == "immediate_demotion"
                            for decision in retention_decisions.values()
                        ),
                        "replacementGate": replacement_gate,
                    },
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
                    "recommendedCore": list(
                        locals().get("recommended_core_order", ())
                    ),
                    "effectiveCore": list(current_core),
                    "operatorStarredRetention": len(pinned_core_order),
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
                "perpPrefilterResolution": _perp_prefilter_resolution_counts(
                    perp_results, recall_cand,
                ),
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
                "operatorStarredRetention": len(pinned_core_order),
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
            retryable = isinstance(exc, resource_guard.ResourceDeferred)
            db.execute(
                "UPDATE scan_generation SET status=?,complete=0,publishable=0,"
                "is_current=0,error=? WHERE generation=?",
                (
                    "ready" if retryable else "leaderboard_validated",
                    str(exc)[:500] if retryable else f"finalize_error:{str(exc)[:500]}",
                    generation_id,
                ),
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
                api_stats=rest.request_stats(),
                retention_metrics={
                    "coreAdded": len(set(current_core) - set(previous_core)) if published else 0,
                    "coreRemoved": len(set(previous_core) - set(current_core)) if published else 0,
                    "probation": sum(
                        decision.status == core_retention.PROBATION
                        for decision in locals().get("retention_decisions", {}).values()
                    ),
                    "recovered": sum(
                        decision.action == "recovered"
                        for decision in locals().get("retention_decisions", {}).values()
                    ),
                    "confirmedDemotion": sum(
                        decision.action == "confirmed_demotion"
                        for decision in locals().get("retention_decisions", {}).values()
                    ),
                    "safetyExit": sum(
                        decision.action == "immediate_demotion"
                        for decision in locals().get("retention_decisions", {}).values()
                    ),
                    "replacementBlocked": not bool(
                        locals().get("replacement_gate", {}).get("eligible", True)
                    ),
                })
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
