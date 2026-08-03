"""Bound discovery history and persist daily disk/database/WAL health.

The maintenance command always runs under the scanner process lock. It keeps compact decision history while
expiring only the high-volume, per-wallet pipeline stages and redundant Leaderboard snapshots. SQLite's normal
freelist reuse is intentional: this task never VACUUMs. It checkpoints only after its own transactions are
closed and truncates a WAL only when PASSIVE proves every frame checkpointed and no reader blocks the reset.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import shutil
import sqlite3
import time

from hyper import config
from hyper.discovery import collection_blacklist


HEAVY_PIPELINE_STAGES = (
    "official_roi",
    "perp_prefilter",
    "profile",
    "rough_copy",
)


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


def _prune_pipeline_detail(db: sqlite3.Connection, cutoff: str) -> int:
    marks = ",".join("?" for _ in HEAVY_PIPELINE_STAGES)
    result = db.execute(
        f"DELETE FROM pipeline_audit WHERE stamp<? "
        f"AND COALESCE(created_at,stamp)<? AND stage IN ({marks})",
        (cutoff, cutoff, *HEAVY_PIPELINE_STAGES),
    )
    return max(0, int(result.rowcount or 0))


def _prune_staged_generations(db: sqlite3.Connection, keep_recent: int) -> tuple[int, int]:
    """Drop redundant raw snapshots while preserving every generation that can still affect execution."""
    db.execute(
        "CREATE TEMP TABLE IF NOT EXISTS storage_guard_keep_generation "
        "(generation TEXT PRIMARY KEY)"
    )
    db.execute("DELETE FROM storage_guard_keep_generation")
    db.execute(
        "INSERT OR IGNORE INTO storage_guard_keep_generation(generation) "
        "SELECT generation FROM scan_generation WHERE is_current=1 "
        "OR status NOT IN ('published','failed')"
    )
    db.execute(
        "INSERT OR IGNORE INTO storage_guard_keep_generation(generation) "
        "SELECT generation FROM scan_generation WHERE source='scan' "
        "AND status='published' AND complete=1 ORDER BY id DESC LIMIT 1"
    )
    db.execute(
        "INSERT OR IGNORE INTO storage_guard_keep_generation(generation) "
        "SELECT generation FROM scan_generation ORDER BY id DESC LIMIT ?",
        (max(1, int(keep_recent)),),
    )
    old = db.execute(
        "SELECT COUNT(*),COUNT(DISTINCT generation) FROM leaderboard_staging "
        "WHERE generation NOT IN (SELECT generation FROM storage_guard_keep_generation)"
    ).fetchone()
    rows, generations = (int(old[0] or 0), int(old[1] or 0)) if old else (0, 0)
    if rows:
        db.execute(
            "DELETE FROM leaderboard_staging WHERE generation NOT IN "
            "(SELECT generation FROM storage_guard_keep_generation)"
        )
    db.execute("DELETE FROM storage_guard_keep_generation")
    return rows, generations


def _prune_expired_fill_cache(
    db: sqlite3.Connection,
    cutoff_ms: int,
    *,
    commit_every: int = 25,
) -> tuple[int, int]:
    """Expire the rolling source window with indexed address+time deletes and small commits."""
    addrs = [row[0] for row in db.execute("SELECT addr FROM fill_cache_state ORDER BY addr")]
    deleted = 0
    touched = 0
    for index, addr in enumerate(addrs, 1):
        before = db.total_changes
        db.execute(
            "DELETE FROM candidate_fills WHERE addr=? AND time<?",
            (addr, int(cutoff_ms)),
        )
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


def _safe_wal_checkpoint(db: sqlite3.Connection) -> dict:
    """Checkpoint committed frames, truncating only when doing so is immediately safe."""
    result = {
        "status": "not_run",
        "busy": None,
        "logFrames": None,
        "checkpointedFrames": None,
        "uncheckpointedFrames": None,
        "truncated": False,
    }
    if db.in_transaction:
        result.update(status="deferred", reason="transaction_open")
        return result
    try:
        busy, log_frames, checkpointed = (
            int(value or 0) for value in db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        )
        result.update(
            status="checkpointed" if not busy else "deferred",
            busy=busy,
            logFrames=log_frames,
            checkpointedFrames=checkpointed,
            uncheckpointedFrames=max(0, log_frames - checkpointed),
        )
        if busy == 0 and log_frames == checkpointed:
            old_timeout = int(db.execute("PRAGMA busy_timeout").fetchone()[0] or 0)
            try:
                db.execute("PRAGMA busy_timeout=250")
                truncate_busy, truncate_log, truncate_checkpointed = (
                    int(value or 0)
                    for value in db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                )
                result["truncated"] = (
                    truncate_busy == 0 and truncate_log == 0 and truncate_checkpointed == 0
                )
                if not result["truncated"]:
                    result.update(status="deferred", reason="truncate_busy")
            finally:
                db.execute(f"PRAGMA busy_timeout={old_timeout}")
    except sqlite3.OperationalError as exc:
        result.update(status="deferred", reason=f"{type(exc).__name__}:{exc}"[:160])
    return result


def _previous_growth_baseline(
    db: sqlite3.Connection,
    now_epoch: float,
) -> tuple[str, int] | None:
    latest_allowed = _iso(
        now_epoch - float(config.STORAGE_GUARD_GROWTH_BASELINE_MIN_HOURS) * 3600,
    )
    row = db.execute(
        "SELECT checked_at,db_main_bytes FROM storage_guard_run "
        "WHERE checked_at<=? ORDER BY checked_at DESC,id DESC LIMIT 1",
        (latest_allowed,),
    ).fetchone()
    return (str(row[0]), int(row[1] or 0)) if row else None


def run(
    db: sqlite3.Connection,
    db_path: str,
    *,
    now_epoch: float | None = None,
    disk_usage: tuple[int, int, int] | None = None,
    db_main_bytes: int | None = None,
    db_wal_bytes: int | None = None,
) -> dict:
    """Apply retention and record one storage-health sample.

    Optional metric overrides make threshold behavior deterministic in tests; production callers omit them.
    The caller must hold :func:`hyper.ops.scan_lock.acquire` for ``db_path``.
    """
    now_epoch = float(time.time() if now_epoch is None else now_epoch)
    checked_at = _iso(now_epoch)
    pipeline_cutoff = _iso(
        now_epoch - float(config.PIPELINE_DETAIL_RETENTION_DAYS) * 86400,
    )

    # This backfill uses only compact completed profile decisions. It never promotes raw fill-count into a
    # blacklist reason. Purging is committed in small batches so a large legacy cleanup cannot recreate the
    # multi-gigabyte WAL peak that prompted this maintenance path.
    with db:
        bootstrapped_blacklist = collection_blacklist.bootstrap_from_profiles(
            db, stamp=checked_at,
        )
    blacklisted_cleanup = collection_blacklist.purge_all(db, commit_every=25)
    expired_fills, expired_wallets = _prune_expired_fill_cache(
        db,
        int((now_epoch - float(config.PROFILE_FETCH_DAYS) * 86400) * 1000),
        commit_every=25,
    )

    with db:
        deleted_pipeline_rows = _prune_pipeline_detail(db, pipeline_cutoff)
        deleted_staging_rows, deleted_staging_generations = _prune_staged_generations(
            db, config.LEADERBOARD_STAGING_KEEP_GENERATIONS,
        )

    checkpoint = _safe_wal_checkpoint(db)

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
    pipeline_rows = int(db.execute("SELECT COUNT(*) FROM pipeline_audit").fetchone()[0] or 0)
    staging_generations = int(db.execute(
        "SELECT COUNT(DISTINCT generation) FROM leaderboard_staging"
    ).fetchone()[0] or 0)

    baseline = _previous_growth_baseline(db, now_epoch)
    growth_bytes = growth_24h_bytes = None
    if baseline:
        baseline_epoch = _epoch(baseline[0])
        if baseline_epoch is not None and now_epoch > baseline_epoch:
            growth_bytes = main_bytes - baseline[1]
            growth_24h_bytes = int(growth_bytes * 86400 / (now_epoch - baseline_epoch))

    reasons = []
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
        reasons.append("db_growth_24h_warning")
    if wal_bytes > int(config.STORAGE_GUARD_WAL_WARN_BYTES):
        if severity == "normal":
            severity = "warning"
        reasons.append("wal_size_warning")

    detail = {
        "status": severity,
        "checkedAt": checked_at,
        "reasons": reasons,
        "disk": {
            "totalBytes": disk_total,
            "usedBytes": disk_used,
            "freeBytes": disk_free,
            "usedPct": round(disk_used_pct, 2),
        },
        "database": {
            "mainBytes": main_bytes,
            "walBytes": wal_bytes,
            "walPhysicalBytes": wal_bytes,
            "walLogFrames": checkpoint.get("logFrames"),
            "walCheckpointedFrames": checkpoint.get("checkpointedFrames"),
            "walUncheckpointedFrames": checkpoint.get("uncheckpointedFrames"),
            "walCheckpoint": checkpoint,
            "journalSizeLimitBytes": int(config.SQLITE_JOURNAL_SIZE_LIMIT_BYTES),
            "growthBytes": growth_bytes,
            "growth24hBytes": growth_24h_bytes,
            "pageBytes": page_bytes,
            "freelistBytes": freelist_bytes,
        },
        "retention": {
            "pipelineDetailDays": int(config.PIPELINE_DETAIL_RETENTION_DAYS),
            "stagingGenerations": int(config.LEADERBOARD_STAGING_KEEP_GENERATIONS),
            "pipelineRows": pipeline_rows,
            "stagingGenerationCount": staging_generations,
            "deletedPipelineRows": deleted_pipeline_rows,
            "deletedStagingRows": deleted_staging_rows,
            "deletedStagingGenerations": deleted_staging_generations,
            "expiredCandidateFills": expired_fills,
            "expiredFillWallets": expired_wallets,
            "bootstrappedBlacklistWallets": bootstrapped_blacklist,
            "blacklistCleanup": blacklisted_cleanup,
        },
    }
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
            "db_page_bytes,db_freelist_bytes,pipeline_audit_rows,staging_generation_count,"
            "deleted_pipeline_rows,deleted_staging_rows,deleted_staging_generations) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                checked_at, severity, reasons_json, disk_total, disk_used, disk_free,
                disk_used_pct, main_bytes, wal_bytes, growth_bytes, growth_24h_bytes,
                page_bytes, freelist_bytes, pipeline_rows, staging_generations,
                deleted_pipeline_rows, deleted_staging_rows, deleted_staging_generations,
            ),
        )
        db.execute(
            "DELETE FROM storage_guard_run WHERE checked_at<?",
            (sample_cutoff,),
        )
        db.execute(
            "INSERT INTO process_status(name,state,pid,heartbeat_at,detail_json) "
            "VALUES ('storage_guard',?,NULL,?,?) ON CONFLICT(name) DO UPDATE SET "
            "state=excluded.state,pid=NULL,heartbeat_at=excluded.heartbeat_at,"
            "detail_json=excluded.detail_json",
            (severity, checked_at, detail_json),
        )
    return detail
