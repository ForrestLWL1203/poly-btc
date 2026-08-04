"""Lifecycle-based SQLite retention and storage health.

Discovery detail is a resumable workspace, not a historical warehouse.  A
published generation keeps only the compact state required by execution and by
the next Challenger refresh.  Deletes are deliberately batched so maintenance
can run beside the Observer without recreating a multi-gigabyte WAL.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import shutil
import sqlite3
import time
from typing import Iterable

from hyper import config
from hyper.discovery import collection_blacklist
from hyper.market import price_path


TERMINAL_GENERATION_STATES = ("published", "failed")
TERMINAL_SIGNAL_STATES = ("completed", "policy_skipped", "failed_terminal")
TERMINAL_COMMAND_STATES = ("done", "failed")


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc,
        ).timestamp()
    except (TypeError, ValueError):
        return None


def _file_size(path: str) -> int:
    try:
        return int(os.path.getsize(path))
    except OSError:
        return 0


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone())


def _marks(values: Iterable) -> str:
    return ",".join("?" for _ in values)


def _delete_batches(
    db: sqlite3.Connection,
    table: str,
    where: str,
    args: tuple = (),
    *,
    batch_size: int | None = None,
    dry_run: bool = False,
) -> int:
    """Delete matching rowid tables in bounded commits; dry-run only counts."""
    if not _table_exists(db, table):
        return 0
    count = int(db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {where}", args,
    ).fetchone()[0] or 0)
    if dry_run or count == 0:
        return count
    batch_size = max(1, int(batch_size or config.STORAGE_DELETE_BATCH_ROWS))
    deleted = 0
    while True:
        before = db.total_changes
        db.execute(
            f"DELETE FROM {table} WHERE rowid IN ("
            f"SELECT rowid FROM {table} WHERE {where} LIMIT ?)",
            (*args, batch_size),
        )
        removed = int(db.total_changes - before)
        db.commit()
        deleted += removed
        if removed < batch_size:
            break
    return deleted


def protected_generations(db: sqlite3.Connection) -> dict[str, list[str] | str | None]:
    """Return the only generation ids allowed to keep reusable bulk state."""
    rows = db.execute(
        "SELECT generation,source,status,complete,is_current FROM scan_generation ORDER BY id"
    ).fetchall()
    nonterminal = {
        str(row[0]) for row in rows if str(row[2]) not in TERMINAL_GENERATION_STATES
    }
    current = next((str(row[0]) for row in reversed(rows) if int(row[4] or 0)), None)
    latest_full = next((
        str(row[0]) for row in reversed(rows)
        if str(row[1] or "") == "scan" and str(row[2]) == "published" and int(row[3] or 0)
    ), None)
    cache = set(nonterminal)
    if current:
        cache.add(current)
    if latest_full:
        cache.add(latest_full)
    evidence = set(nonterminal)
    if latest_full:
        evidence.add(latest_full)
    return {
        "current": current,
        "latestFullScan": latest_full,
        "nonterminal": sorted(nonterminal),
        "cache": sorted(cache),
        "evidence": sorted(evidence),
    }


def _outside_generations(keep: Iterable[str]) -> tuple[str, tuple]:
    values = tuple(sorted(set(keep)))
    if not values:
        return "1=1", ()
    return f"generation NOT IN ({_marks(values)})", values


def _prune_generation_data(
    db: sqlite3.Connection,
    protected: dict,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Apply one generation keep-set consistently across dependent tables."""
    result: dict[str, int] = {}
    cache_where, cache_args = _outside_generations(protected["cache"])
    evidence_where, evidence_args = _outside_generations(protected["evidence"])
    active_where, active_args = _outside_generations(protected["nonterminal"])
    for table in (
        "leaderboard_staging",
        "generation_market_snapshot",
        "generation_market_manifest",
        "follow_selection",
    ):
        result[table] = _delete_batches(
            db, table, cache_where, cache_args, dry_run=dry_run,
        )
    # Observer writes durable safety assessments under synthetic ``live-*``
    # generations. Only prune rows proven to belong to a scan generation.
    result["wallet_risk_assessment"] = _delete_batches(
        db,
        "wallet_risk_assessment",
        f"EXISTS (SELECT 1 FROM scan_generation sg "
        f"WHERE sg.generation=wallet_risk_assessment.generation) AND ({cache_where})",
        cache_args,
        dry_run=dry_run,
    )
    if _table_exists(db, "auto_tune_runs"):
        tune_where = (
            f"generation IS NOT NULL AND ({cache_where})"
            if cache_args else "generation IS NOT NULL"
        )
        result["auto_tune_runs"] = _delete_batches(
            db, "auto_tune_runs", tune_where, cache_args, dry_run=dry_run,
        )
    result["pre_strict_evidence"] = _delete_batches(
        db, "pre_strict_evidence", evidence_where, evidence_args, dry_run=dry_run,
    )
    # Formation prefixes are only resumable workspace. Published evidence is
    # already compacted into follow_selection/strategy_revision.
    result["formation_prefix_evidence"] = _delete_batches(
        db, "formation_prefix_evidence", active_where, active_args, dry_run=dry_run,
    )
    return result


def _prune_pipeline_workspace(
    db: sqlite3.Connection,
    protected: dict,
    *,
    dry_run: bool = False,
) -> int:
    active = tuple(protected["nonterminal"])
    if active:
        owned = f"generation IS NOT NULL AND generation NOT IN ({_marks(active)})"
        args: tuple = active
    else:
        owned, args = "generation IS NOT NULL", ()
    # Legacy rows have no explicit owner. Exact started_at equality is the only
    # safe old association and protects an interrupted pre-migration scan.
    legacy = (
        "generation IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM scan_generation sg WHERE sg.started_at=pipeline_audit.stamp "
        "AND sg.status NOT IN ('published','failed'))"
    )
    return _delete_batches(
        db, "pipeline_audit", f"(({owned}) OR ({legacy}))", args, dry_run=dry_run,
    )


def _prune_scan_summaries(
    db: sqlite3.Connection,
    now_epoch: float,
    *,
    dry_run: bool = False,
) -> int:
    cutoff = _iso(now_epoch - float(config.SCAN_SUMMARY_RETENTION_DAYS) * 86400)
    return _delete_batches(
        db,
        "scan_runs",
        "(COALESCE(finished_at,started_at)<? OR id NOT IN ("
        "SELECT id FROM scan_runs ORDER BY id DESC LIMIT ?))",
        (cutoff, int(config.SCAN_SUMMARY_KEEP_COUNT)),
        dry_run=dry_run,
    )


def prune_execution_transients(
    db: sqlite3.Connection,
    *,
    now_epoch: float | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Bound replaceable Observer diagnostics without touching trade ledgers."""
    now_epoch = float(time.time() if now_epoch is None else now_epoch)
    normal_cutoff = _iso(
        now_epoch - float(config.EXECUTION_DIAGNOSTIC_RETENTION_DAYS) * 86400,
    )
    anomaly_cutoff = _iso(
        now_epoch - float(config.EXECUTION_RECONCILE_ANOMALY_RETENTION_DAYS) * 86400,
    )
    signal_marks = _marks(TERMINAL_SIGNAL_STATES)
    command_marks = _marks(TERMINAL_COMMAND_STATES)
    result = {
        "execution_account_snapshot": _delete_batches(
            db, "execution_account_snapshot", "observed_at<?", (normal_cutoff,), dry_run=dry_run,
        ),
        "execution_reconcile_ok": _delete_batches(
            db, "execution_reconcile_checkpoint", "status='ok' AND created_at<?",
            (normal_cutoff,), dry_run=dry_run,
        ),
        "execution_reconcile_anomaly": _delete_batches(
            db, "execution_reconcile_checkpoint", "status<>'ok' AND created_at<?",
            (anomaly_cutoff,), dry_run=dry_run,
        ),
        "execution_signal": _delete_batches(
            db, "execution_signal",
            f"state IN ({signal_marks}) AND completed_at<?",
            (*TERMINAL_SIGNAL_STATES, normal_cutoff), dry_run=dry_run,
        ),
        "commands": _delete_batches(
            db, "commands",
            f"status IN ({command_marks}) AND COALESCE(done_at,created_at)<?",
            (*TERMINAL_COMMAND_STATES, normal_cutoff), dry_run=dry_run,
        ),
        "execution_preflight": _delete_batches(
            db, "execution_preflight",
            "(consumed_at IS NOT NULL AND consumed_at<?) OR expires_at<?",
            (normal_cutoff, normal_cutoff), dry_run=dry_run,
        ),
        "live_fills": _delete_batches(
            db, "live_fills", "time_ms<?",
            (int((now_epoch - float(config.LIVE_FILLS_RETENTION_DAYS) * 86400) * 1000),),
            dry_run=dry_run,
        ),
        "account_stats": _delete_batches(
            db, "account_stats", "ts<?",
            (_iso(now_epoch - float(config.ACCOUNT_STATS_RETENTION_DAYS) * 86400),),
            dry_run=dry_run,
        ),
        "live_policy_skip": _delete_batches(
            db, "live_policy_skip", "last_ms<?",
            (int((now_epoch - 90 * 86400) * 1000),), dry_run=dry_run,
        ),
    }
    return result


def _prune_expired_fill_cache(
    db: sqlite3.Connection,
    cutoff_ms: int,
    *,
    commit_every: int = 25,
    dry_run: bool = False,
) -> tuple[int, int]:
    if dry_run:
        rows = db.execute(
            "SELECT COUNT(*),COUNT(DISTINCT addr) FROM candidate_fills WHERE time<?",
            (int(cutoff_ms),),
        ).fetchone()
        return int(rows[0] or 0), int(rows[1] or 0)
    addrs = [row[0] for row in db.execute("SELECT addr FROM fill_cache_state ORDER BY addr")]
    deleted = touched = 0
    for index, addr in enumerate(addrs, 1):
        before = db.total_changes
        db.execute("DELETE FROM candidate_fills WHERE addr=? AND time<?", (addr, int(cutoff_ms)))
        removed = db.total_changes - before
        if removed:
            deleted += removed
            touched += 1
        db.execute(
            "UPDATE fill_cache_state SET coverage_start_ms=MAX(COALESCE(coverage_start_ms,?),?) "
            "WHERE addr=?",
            (int(cutoff_ms), int(cutoff_ms), addr),
        )
        if index % max(1, int(commit_every)) == 0:
            db.commit()
    db.commit()
    return deleted, touched


def _prune_price_candles(
    db: sqlite3.Connection, now_epoch: float, *, dry_run: bool = False,
) -> int:
    now_ms = int(now_epoch * 1000)
    if not dry_run:
        deleted = int(price_path.prune(db, now_ms=now_ms) or 0)
        db.commit()
        return deleted
    total = 0
    for interval, days in price_path.RETENTION_DAYS.items():
        total += int(db.execute(
            "SELECT COUNT(*) FROM coin_price_candle WHERE interval=? AND close_time<?",
            (interval, now_ms - int(days) * 86_400_000),
        ).fetchone()[0] or 0)
    return total


def _safe_wal_checkpoint(db: sqlite3.Connection) -> dict:
    result = {
        "status": "not_run", "busy": None, "logFrames": None,
        "checkpointedFrames": None, "uncheckpointedFrames": None, "truncated": False,
    }
    if db.in_transaction:
        result.update(status="deferred", reason="transaction_open")
        return result
    try:
        busy, log_frames, checkpointed = (
            int(value or 0) for value in db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        )
        result.update(
            status="checkpointed" if not busy else "deferred", busy=busy,
            logFrames=log_frames, checkpointedFrames=checkpointed,
            uncheckpointedFrames=max(0, log_frames - checkpointed),
        )
        if busy == 0 and log_frames == checkpointed:
            old_timeout = int(db.execute("PRAGMA busy_timeout").fetchone()[0] or 0)
            try:
                db.execute("PRAGMA busy_timeout=250")
                values = tuple(
                    int(value or 0)
                    for value in db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                )
                result["truncated"] = values == (0, 0, 0)
                if not result["truncated"]:
                    result.update(status="deferred", reason="truncate_busy")
            finally:
                db.execute(f"PRAGMA busy_timeout={old_timeout}")
    except sqlite3.OperationalError as exc:
        result.update(status="deferred", reason=f"{type(exc).__name__}:{exc}"[:160])
    return result


def _previous_growth_baseline(
    db: sqlite3.Connection, now_epoch: float,
) -> tuple[str, int] | None:
    latest_allowed = _iso(
        now_epoch - float(config.STORAGE_GUARD_GROWTH_BASELINE_MIN_HOURS) * 3600,
    )
    row = db.execute(
        "SELECT checked_at,COALESCE(db_active_bytes,db_main_bytes) FROM storage_guard_run "
        "WHERE checked_at<=? ORDER BY checked_at DESC,id DESC LIMIT 1",
        (latest_allowed,),
    ).fetchone()
    return (str(row[0]), int(row[1] or 0)) if row else None


def _table_counts(db: sqlite3.Connection) -> dict[str, int]:
    names = (
        "candidate_fills", "pipeline_audit", "leaderboard_staging",
        "pre_strict_evidence", "formation_prefix_evidence",
        "generation_market_snapshot", "execution_account_snapshot",
        "execution_reconcile_checkpoint", "execution_signal",
    )
    return {
        name: int(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] or 0)
        for name in names if _table_exists(db, name)
    }


def _estimated_reclaimed_pages(
    db: sqlite3.Connection, deleted: dict[str, int], page_size: int,
) -> int | None:
    """Estimate pages from only affected b-trees; never traverse the whole DB.

    An unfiltered ``dbstat`` aggregation reads every page (including the 37-day
    fill cache) and can consume a CPU core for minutes on production.  Equality
    constraints let SQLite visit only each affected table and its indexes.
    """
    estimated = 0.0
    for table, delete_n in deleted.items():
        if not delete_n or not _table_exists(db, table):
            continue
        total = int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
        if not total:
            continue
        objects = [table] + [
            str(row[0]) for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
                (table,),
            ).fetchall()
        ]
        try:
            size = int(db.execute(
                f"SELECT COALESCE(SUM(pgsize),0) FROM dbstat "
                f"WHERE name IN ({_marks(objects)})",
                tuple(objects),
            ).fetchone()[0] or 0)
        except sqlite3.Error:
            return None
        estimated += float(size) * min(1.0, float(delete_n) / total)
    return int(estimated / max(1, page_size))


def post_publish_cleanup(
    db: sqlite3.Connection,
    generation: str,
    *,
    now_epoch: float | None = None,
) -> dict:
    """Idempotently compact one already-published generation after commit."""
    row = db.execute(
        "SELECT status,complete,started_at FROM scan_generation WHERE generation=?", (generation,),
    ).fetchone()
    if not row or str(row[0]) != "published" or not int(row[1] or 0):
        raise ValueError("post_publish_cleanup_requires_published_generation")
    legacy_stamp = str(row[2] or "")
    deleted_pipeline = _delete_batches(
        db, "pipeline_audit", "generation=? OR (generation IS NULL AND stamp=?)",
        (generation, legacy_stamp),
    )
    protected = protected_generations(db)
    generations = _prune_generation_data(db, protected)
    now_epoch = float(time.time() if now_epoch is None else now_epoch)
    execution = prune_execution_transients(db, now_epoch=now_epoch)
    expired_fills, expired_wallets = _prune_expired_fill_cache(
        db,
        int((now_epoch - float(config.PROFILE_FETCH_DAYS) * 86400) * 1000),
    )
    price_candles = _prune_price_candles(db, now_epoch)
    blacklist_cleanup = collection_blacklist.purge_all(db, commit_every=25)
    return {
        "generation": generation,
        "pipelineAudit": deleted_pipeline,
        "generationData": generations,
        "executionDiagnostics": execution,
        "expiredCandidateFills": expired_fills,
        "expiredFillWallets": expired_wallets,
        "expiredPriceCandles": price_candles,
        "blacklistCleanup": blacklist_cleanup,
        "protectedGenerations": protected,
    }


def run(
    db: sqlite3.Connection,
    db_path: str,
    *,
    now_epoch: float | None = None,
    disk_usage: tuple[int, int, int] | None = None,
    db_main_bytes: int | None = None,
    db_wal_bytes: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Apply lifecycle retention and record one storage-health sample."""
    now_epoch = float(time.time() if now_epoch is None else now_epoch)
    checked_at = _iso(now_epoch)
    protected = protected_generations(db)

    if dry_run:
        bootstrapped_blacklist = 0
        blacklisted_cleanup = {
            "candidate_fills": int(db.execute(
                "SELECT COUNT(*) FROM candidate_fills cf WHERE EXISTS ("
                "SELECT 1 FROM wallet_scan_blacklist b WHERE lower(b.addr)=lower(cf.addr))"
            ).fetchone()[0] or 0),
        }
    else:
        with db:
            bootstrapped_blacklist = collection_blacklist.bootstrap_from_profiles(
                db, stamp=checked_at,
            )
        blacklisted_cleanup = collection_blacklist.purge_all(db, commit_every=25)

    expired_fills, expired_wallets = _prune_expired_fill_cache(
        db,
        int((now_epoch - float(config.PROFILE_FETCH_DAYS) * 86400) * 1000),
        commit_every=25,
        dry_run=dry_run,
    )
    expired_price_candles = _prune_price_candles(db, now_epoch, dry_run=dry_run)
    deleted_pipeline_rows = _prune_pipeline_workspace(db, protected, dry_run=dry_run)
    generation_cleanup = _prune_generation_data(db, protected, dry_run=dry_run)
    execution_cleanup = prune_execution_transients(
        db, now_epoch=now_epoch, dry_run=dry_run,
    )
    deleted_scan_runs = _prune_scan_summaries(db, now_epoch, dry_run=dry_run)
    if not dry_run:
        db.commit()
    checkpoint = {
        "status": "dry_run", "busy": None, "logFrames": None,
        "checkpointedFrames": None, "uncheckpointedFrames": None, "truncated": False,
    } if dry_run else _safe_wal_checkpoint(db)

    if disk_usage is None:
        usage = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path)) or ".")
        disk_total, disk_used, disk_free = int(usage.total), int(usage.used), int(usage.free)
    else:
        disk_total, disk_used, disk_free = (int(value) for value in disk_usage)
    disk_used_pct = (100.0 * disk_used / disk_total) if disk_total else 100.0
    main_bytes = _file_size(db_path) if db_main_bytes is None else int(db_main_bytes)
    wal_bytes = _file_size(db_path + "-wal") if db_wal_bytes is None else int(db_wal_bytes)
    page_size = int(db.execute("PRAGMA page_size").fetchone()[0] or 0)
    page_count = int(db.execute("PRAGMA page_count").fetchone()[0] or 0)
    freelist_count = int(db.execute("PRAGMA freelist_count").fetchone()[0] or 0)
    page_bytes = page_size * page_count
    freelist_bytes = page_size * freelist_count
    active_bytes = max(0, page_bytes - freelist_bytes)
    counts = _table_counts(db)

    baseline = _previous_growth_baseline(db, now_epoch)
    growth_bytes = growth_24h_bytes = None
    if baseline:
        baseline_epoch = _epoch(baseline[0])
        if baseline_epoch is not None and now_epoch > baseline_epoch:
            growth_bytes = active_bytes - baseline[1]
            growth_24h_bytes = int(growth_bytes * 86400 / (now_epoch - baseline_epoch))

    reasons: list[str] = []
    severity = "normal"
    if disk_used_pct >= float(config.STORAGE_GUARD_DISK_CRITICAL_PCT):
        severity = "critical"
        reasons.append("disk_used_critical")
    elif disk_used_pct >= float(config.STORAGE_GUARD_DISK_WARN_PCT):
        severity = "warning"
        reasons.append("disk_used_warning")
    if growth_24h_bytes is not None and growth_24h_bytes > int(
        config.STORAGE_GUARD_DB_GROWTH_WARN_BYTES_24H
    ):
        if severity == "normal":
            severity = "warning"
        reasons.append("db_active_growth_24h_warning")
    if wal_bytes > int(config.STORAGE_GUARD_WAL_WARN_BYTES):
        if severity == "normal":
            severity = "warning"
        reasons.append("wal_size_warning")

    delete_by_table = dict(generation_cleanup)
    delete_by_table["pipeline_audit"] = deleted_pipeline_rows
    for key, value in execution_cleanup.items():
        table = key.split("_ok")[0].split("_anomaly")[0]
        delete_by_table[table] = delete_by_table.get(table, 0) + int(value)
    estimated_pages = _estimated_reclaimed_pages(db, delete_by_table, page_size)
    detail = {
        "status": "dry_run" if dry_run else severity,
        "checkedAt": checked_at,
        "reasons": reasons,
        "dryRun": bool(dry_run),
        "disk": {
            "totalBytes": disk_total, "usedBytes": disk_used, "freeBytes": disk_free,
            "usedPct": round(disk_used_pct, 2),
        },
        "database": {
            "mainBytes": main_bytes, "activeBytes": active_bytes,
            "pageBytes": page_bytes, "freelistBytes": freelist_bytes,
            "walBytes": wal_bytes, "walPhysicalBytes": wal_bytes,
            "walLogFrames": checkpoint.get("logFrames"),
            "walCheckpointedFrames": checkpoint.get("checkpointedFrames"),
            "walUncheckpointedFrames": checkpoint.get("uncheckpointedFrames"),
            "walCheckpoint": checkpoint,
            "journalSizeLimitBytes": int(config.SQLITE_JOURNAL_SIZE_LIMIT_BYTES),
            "activeGrowthBytes": growth_bytes, "activeGrowth24hBytes": growth_24h_bytes,
            # Backward-compatible aliases now intentionally describe active data growth.
            "growthBytes": growth_bytes, "growth24hBytes": growth_24h_bytes,
            "tableRows": counts,
        },
        "retention": {
            "protectedGenerations": protected,
            "deletedPipelineRows": deleted_pipeline_rows,
            "generationCleanup": generation_cleanup,
            "executionDiagnostics": execution_cleanup,
            "deletedScanRuns": deleted_scan_runs,
            "expiredCandidateFills": expired_fills,
            "expiredFillWallets": expired_wallets,
            "expiredPriceCandles": expired_price_candles,
            "bootstrappedBlacklistWallets": bootstrapped_blacklist,
            "blacklistCleanup": blacklisted_cleanup,
            "estimatedReclaimedPages": estimated_pages,
        },
    }
    if dry_run:
        return detail

    reasons_json = json.dumps(reasons, separators=(",", ":"), sort_keys=True)
    detail_json = json.dumps(detail, separators=(",", ":"), sort_keys=True)
    sample_cutoff = _iso(
        now_epoch - float(config.STORAGE_GUARD_SAMPLE_RETENTION_DAYS) * 86400,
    )
    with db:
        db.execute(
            "INSERT INTO storage_guard_run "
            "(checked_at,severity,reasons_json,disk_total_bytes,disk_used_bytes,disk_free_bytes,"
            "disk_used_pct,db_main_bytes,db_wal_bytes,db_growth_bytes,db_growth_24h_bytes,"
            "db_page_bytes,db_freelist_bytes,db_active_bytes,pipeline_audit_rows,"
            "staging_generation_count,deleted_pipeline_rows,deleted_staging_rows,"
            "deleted_staging_generations) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                checked_at, severity, reasons_json, disk_total, disk_used, disk_free,
                disk_used_pct, main_bytes, wal_bytes, growth_bytes, growth_24h_bytes,
                page_bytes, freelist_bytes, active_bytes, counts.get("pipeline_audit", 0),
                len({row[0] for row in db.execute(
                    "SELECT DISTINCT generation FROM leaderboard_staging"
                ).fetchall()}),
                deleted_pipeline_rows,
                generation_cleanup.get("leaderboard_staging", 0),
                0,
            ),
        )
        db.execute("DELETE FROM storage_guard_run WHERE checked_at<?", (sample_cutoff,))
        db.execute(
            "INSERT INTO process_status(name,state,pid,heartbeat_at,detail_json) "
            "VALUES ('storage_guard',?,NULL,?,?) ON CONFLICT(name) DO UPDATE SET "
            "state=excluded.state,pid=NULL,heartbeat_at=excluded.heartbeat_at,"
            "detail_json=excluded.detail_json",
            (severity, checked_at, detail_json),
        )
    return detail
