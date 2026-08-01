"""Bound discovery history and persist daily disk/database growth health.

The maintenance command always runs under the scanner process lock. It keeps compact decision history while
expiring only the high-volume, per-wallet pipeline stages and redundant Leaderboard snapshots. SQLite's normal
freelist reuse is intentional: this task does not VACUUM or checkpoint a live database.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import shutil
import sqlite3
import time

from hyper import config


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

    with db:
        deleted_pipeline_rows = _prune_pipeline_detail(db, pipeline_cutoff)
        deleted_staging_rows, deleted_staging_generations = _prune_staged_generations(
            db, config.LEADERBOARD_STAGING_KEEP_GENERATIONS,
        )

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
