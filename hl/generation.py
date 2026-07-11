"""Atomic scanner-generation staging, validation and publication helpers.

The legacy scanner writes discovery tables directly.  vNext callers stage a leaderboard snapshot and all
generation-scoped selections first, then publish them in one SQLite transaction.  Helpers deliberately do
not commit: callers can include profile/selection writes in the same ``with db:`` block.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import sqlite3
import uuid
from typing import Any, Callable, Iterable, Mapping, Sequence


MIN_LEADERBOARD_ROW_RATIO = 0.85
MIN_LEADERBOARD_COMPLETENESS = 0.99
WINDOW_NAMES = ("day", "week", "month", "allTime")
STAGED_COLUMNS = (
    "addr", "display_name", "account_value",
    "day_pnl", "day_roi", "day_vlm",
    "week_pnl", "week_roi", "week_vlm",
    "mon_pnl", "mon_roi", "mon_vlm",
    "all_pnl", "all_roi", "all_vlm",
    "daily_turnover", "is_candidate", "fetched_at",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class LeaderboardValidation:
    valid: bool
    row_count: int
    unique_count: int
    complete_count: int
    previous_count: int
    required_count: int
    row_ratio: float | None
    completeness: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reasons"] = list(self.reasons)
        return value


def validate_leaderboard_counts(
    row_count: int,
    unique_count: int,
    complete_count: int,
    *,
    previous_count: int = 0,
    min_row_ratio: float = MIN_LEADERBOARD_ROW_RATIO,
    min_completeness: float = MIN_LEADERBOARD_COMPLETENESS,
) -> LeaderboardValidation:
    """Validate already-counted snapshot inputs.

    The first generation has no previous-size floor, but must still be non-empty, unique and 99% complete.
    The 85% floor is inclusive and rounded up so small snapshots cannot pass by integer truncation.
    """
    row_count = max(0, int(row_count or 0))
    unique_count = max(0, int(unique_count or 0))
    complete_count = max(0, int(complete_count or 0))
    previous_count = max(0, int(previous_count or 0))
    min_row_ratio = float(min_row_ratio)
    min_completeness = float(min_completeness)
    required_count = math.ceil(previous_count * min_row_ratio) if previous_count else 1
    row_ratio = (row_count / previous_count) if previous_count else None
    completeness = (complete_count / row_count) if row_count else 0.0
    reasons: list[str] = []
    if row_count < required_count:
        reasons.append("row_count_below_previous_floor" if previous_count else "empty_snapshot")
    if unique_count != row_count:
        reasons.append("duplicate_or_missing_address")
    if completeness < min_completeness:
        reasons.append("window_completeness_below_floor")
    return LeaderboardValidation(
        valid=not reasons,
        row_count=row_count,
        unique_count=unique_count,
        complete_count=complete_count,
        previous_count=previous_count,
        required_count=required_count,
        row_ratio=row_ratio,
        completeness=completeness,
        reasons=tuple(reasons),
    )


def _windows(row: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = row.get("windowPerformances")
    if isinstance(raw, Mapping):
        return {str(k): v for k, v in raw.items() if isinstance(v, Mapping)}
    out: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) >= 2:
                name, value = item[0], item[1]
                if isinstance(value, Mapping):
                    out[str(name)] = value
    return out


def _default_addr(row: Mapping[str, Any]) -> str | None:
    value = row.get("addr") or row.get("ethAddress")
    value = str(value).strip().lower() if value is not None else ""
    return value or None


def _default_complete(row: Mapping[str, Any]) -> bool:
    if not _default_addr(row):
        return False
    windows = _windows(row)
    if windows:
        return all(
            name in windows and all(windows[name].get(field) is not None for field in ("pnl", "roi", "vlm"))
            for name in WINDOW_NAMES
        )
    return all(
        row.get(f"{prefix}_{field}") is not None
        for prefix in ("day", "week", "mon", "all")
        for field in ("pnl", "roi", "vlm")
    )


def validate_leaderboard_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    previous_count: int = 0,
    addr_getter: Callable[[Mapping[str, Any]], str | None] | None = None,
    completeness_getter: Callable[[Mapping[str, Any]], bool] | None = None,
    min_row_ratio: float = MIN_LEADERBOARD_ROW_RATIO,
    min_completeness: float = MIN_LEADERBOARD_COMPLETENESS,
) -> LeaderboardValidation:
    rows = list(rows)
    addr_getter = addr_getter or _default_addr
    completeness_getter = completeness_getter or _default_complete
    addresses = [addr_getter(row) for row in rows]
    unique_count = len({addr for addr in addresses if addr})
    complete_count = sum(1 for row in rows if completeness_getter(row))
    return validate_leaderboard_counts(
        len(rows), unique_count, complete_count,
        previous_count=previous_count,
        min_row_ratio=min_row_ratio,
        min_completeness=min_completeness,
    )


def begin_generation(
    db: sqlite3.Connection,
    *,
    generation: str | None = None,
    source: str = "scan",
    started_at: str | None = None,
    workset_mode: str | None = None,
    fill_mode: str | None = None,
    full_refresh_shard: int | None = None,
) -> str:
    started_at = started_at or now_iso()
    generation = generation or f"scan-{started_at}-{uuid.uuid4().hex[:8]}"
    previous = db.execute(
        "SELECT generation FROM scan_generation "
        "WHERE status='published' AND complete=1 AND is_current=1 LIMIT 1"
    ).fetchone()
    db.execute(
        "INSERT INTO scan_generation "
        "(generation,source,status,started_at,previous_published_generation,workset_mode,fill_mode,full_refresh_shard) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (generation, source, "staging", started_at, previous[0] if previous else None,
         workset_mode, fill_mode, full_refresh_shard),
    )
    return generation


def _normalise_row(row: Mapping[str, Any], fetched_at: str | None) -> dict[str, Any]:
    windows = _windows(row)

    def window_value(raw_name: str, prefix: str, field: str):
        if raw_name in windows:
            return windows[raw_name].get(field)
        return row.get(f"{prefix}_{field}")

    account_value = row.get("account_value", row.get("accountValue"))
    day_vlm = window_value("day", "day", "vlm")
    daily_turnover = row.get("daily_turnover")
    if daily_turnover is None and account_value:
        daily_turnover = (float(day_vlm or 0) / float(account_value)) if float(account_value) else None
    return {
        "addr": _default_addr(row),
        "display_name": row.get("display_name", row.get("displayName")),
        "account_value": account_value,
        "day_pnl": window_value("day", "day", "pnl"),
        "day_roi": window_value("day", "day", "roi"),
        "day_vlm": day_vlm,
        "week_pnl": window_value("week", "week", "pnl"),
        "week_roi": window_value("week", "week", "roi"),
        "week_vlm": window_value("week", "week", "vlm"),
        "mon_pnl": window_value("month", "mon", "pnl"),
        "mon_roi": window_value("month", "mon", "roi"),
        "mon_vlm": window_value("month", "mon", "vlm"),
        "all_pnl": window_value("allTime", "all", "pnl"),
        "all_roi": window_value("allTime", "all", "roi"),
        "all_vlm": window_value("allTime", "all", "vlm"),
        "daily_turnover": daily_turnover,
        "is_candidate": int(bool(row.get("is_candidate", 0))),
        "fetched_at": row.get("fetched_at") or fetched_at,
    }


def stage_leaderboard_rows(
    db: sqlite3.Connection,
    generation: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    fetched_at: str | None = None,
) -> int:
    """Replace one generation's staging rows. Invalid/missing addresses are not insertable.

    Call :func:`validate_leaderboard_rows` on the original response first; silently skipping a malformed
    address here cannot make an invalid generation publishable because recorded counts retain that failure.
    """
    fetched_at = fetched_at or now_iso()
    normalised = [_normalise_row(row, fetched_at) for row in rows]
    db.execute("DELETE FROM leaderboard_staging WHERE generation=?", (generation,))
    placeholders = ",".join("?" for _ in range(len(STAGED_COLUMNS) + 1))
    sql = (
        f"INSERT OR REPLACE INTO leaderboard_staging (generation,{','.join(STAGED_COLUMNS)}) "
        f"VALUES ({placeholders})"
    )
    values = [
        [generation] + [row.get(column) for column in STAGED_COLUMNS]
        for row in normalised if row.get("addr")
    ]
    if values:
        db.executemany(sql, values)
    return len(values)


def previous_published_row_count(db: sqlite3.Connection) -> int:
    row = db.execute(
        "SELECT leaderboard_rows FROM scan_generation "
        "WHERE status='published' AND complete=1 AND is_current=1 LIMIT 1"
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0])
    row = db.execute("SELECT COUNT(*) FROM leaderboard").fetchone()
    return int(row[0] if row else 0)


def record_leaderboard_validation(
    db: sqlite3.Connection,
    generation: str,
    validation: LeaderboardValidation,
    *,
    fetched_at: str | None = None,
) -> None:
    fetched_at = fetched_at or now_iso()
    error = ",".join(validation.reasons) or None
    status = "leaderboard_validated" if validation.valid else "failed"
    cur = db.execute(
        "UPDATE scan_generation SET status=?,leaderboard_fetched_at=?,leaderboard_rows=?,"
        "leaderboard_unique_rows=?,leaderboard_complete_rows=?,leaderboard_completeness=?,"
        "leaderboard_valid=?,failed_at=CASE WHEN ?=0 THEN ? ELSE failed_at END,error=? "
        "WHERE generation=?",
        (status, fetched_at, validation.row_count, validation.unique_count, validation.complete_count,
         validation.completeness, int(validation.valid), int(validation.valid), fetched_at, error, generation),
    )
    if cur.rowcount == 0:
        raise KeyError(f"unknown generation: {generation}")


def stage_and_validate_leaderboard(
    db: sqlite3.Connection,
    generation: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    previous_count: int | None = None,
    fetched_at: str | None = None,
) -> LeaderboardValidation:
    rows = list(rows)
    previous_count = previous_published_row_count(db) if previous_count is None else int(previous_count)
    validation = validate_leaderboard_rows(rows, previous_count=previous_count)
    stage_leaderboard_rows(db, generation, rows, fetched_at=fetched_at)
    record_leaderboard_validation(db, generation, validation, fetched_at=fetched_at)
    return validation


def record_workset(
    db: sqlite3.Connection,
    generation: str,
    *,
    workset_mode: str,
    fill_mode: str,
    full_refresh_shard: int | None,
    workset_n: int,
    deferred_n: int,
    metrics: Mapping[str, Any] | None = None,
) -> None:
    db.execute(
        "UPDATE scan_generation SET workset_mode=?,fill_mode=?,full_refresh_shard=?,workset_n=?,"
        "deferred_n=?,metrics_json=? WHERE generation=?",
        (workset_mode, fill_mode, full_refresh_shard, int(workset_n), int(deferred_n),
         json.dumps(dict(metrics or {}), separators=(",", ":"), sort_keys=True), generation),
    )


def mark_generation_ready(
    db: sqlite3.Connection,
    generation: str,
    *,
    profile_total: int,
    profile_valid: int,
    profile_deferred: int = 0,
    profile_rejected: int = 0,
    profile_complete: bool,
    ready_at: str | None = None,
) -> None:
    row = db.execute(
        "SELECT status,leaderboard_valid FROM scan_generation WHERE generation=?", (generation,)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown generation: {generation}")
    if row[0] == "failed" or not bool(row[1]):
        raise ValueError("generation has no valid leaderboard snapshot")
    ready_at = ready_at or now_iso()
    publishable = int(bool(profile_complete))
    db.execute(
        "UPDATE scan_generation SET status=?,ready_at=?,profile_total=?,profile_valid=?,profile_deferred=?,"
        "profile_rejected=?,profile_complete=?,publishable=?,error=? WHERE generation=?",
        ("ready" if profile_complete else "incomplete", ready_at, int(profile_total), int(profile_valid),
         int(profile_deferred), int(profile_rejected), int(bool(profile_complete)), publishable,
         None if profile_complete else "profile_generation_incomplete", generation),
    )


def promote_staged_leaderboard(db: sqlite3.Connection, generation: str) -> int:
    row = db.execute(
        "SELECT leaderboard_valid,leaderboard_rows FROM scan_generation WHERE generation=?", (generation,)
    ).fetchone()
    if row is None or not bool(row[0]):
        raise ValueError("cannot promote an unvalidated leaderboard generation")
    staged_n = db.execute(
        "SELECT COUNT(*) FROM leaderboard_staging WHERE generation=?", (generation,)
    ).fetchone()[0]
    if int(staged_n) != int(row[1] or 0):
        raise ValueError("staged leaderboard row count does not match validated snapshot")
    db.execute("DELETE FROM leaderboard")
    db.execute(
        "INSERT INTO leaderboard "
        "(addr,display_name,account_value,day_pnl,day_roi,day_vlm,week_pnl,week_roi,week_vlm,"
        "mon_pnl,mon_roi,mon_vlm,all_pnl,all_roi,all_vlm,daily_turnover,is_candidate,fetched_at,generation) "
        "SELECT addr,display_name,account_value,day_pnl,day_roi,day_vlm,week_pnl,week_roi,week_vlm,"
        "mon_pnl,mon_roi,mon_vlm,all_pnl,all_roi,all_vlm,daily_turnover,is_candidate,fetched_at,generation "
        "FROM leaderboard_staging WHERE generation=?",
        (generation,),
    )
    return int(staged_n)


def publish_generation(
    db: sqlite3.Connection,
    generation: str,
    *,
    published_at: str | None = None,
    promote_leaderboard: bool = True,
) -> dict[str, Any]:
    """Publish a ready generation without committing.

    Use ``with db:`` around selection writes plus this call.  SQLite then exposes either the complete old
    generation or the complete new one; there is no intermediate Observer-visible selection.
    """
    row = db.execute(
        "SELECT status,leaderboard_valid,profile_complete,publishable FROM scan_generation WHERE generation=?",
        (generation,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown generation: {generation}")
    if row[0] != "ready" or not all(bool(value) for value in row[1:]):
        raise ValueError("generation is not complete and publishable")
    staged_n = db.execute(
        "SELECT COUNT(*) FROM leaderboard_staging WHERE generation=?", (generation,)
    ).fetchone()[0]
    if promote_leaderboard and staged_n:
        promote_staged_leaderboard(db, generation)
    published_at = published_at or now_iso()
    db.execute("UPDATE scan_generation SET is_current=0 WHERE is_current=1 AND generation<>?", (generation,))
    db.execute(
        "UPDATE scan_generation SET status='published',complete=1,is_current=1,published_at=?,error=NULL "
        "WHERE generation=?",
        (published_at, generation),
    )
    return current_published_generation(db) or {}


def fail_generation(
    db: sqlite3.Connection,
    generation: str,
    error: str,
    *,
    failed_at: str | None = None,
) -> None:
    db.execute(
        "UPDATE scan_generation SET status='failed',complete=0,publishable=0,is_current=0,failed_at=?,error=? "
        "WHERE generation=?",
        (failed_at or now_iso(), str(error), generation),
    )


def current_published_generation(db: sqlite3.Connection) -> dict[str, Any] | None:
    cur = db.execute(
        "SELECT * FROM scan_generation WHERE status='published' AND complete=1 AND is_current=1 LIMIT 1"
    )
    row = cur.fetchone()
    return dict(zip((item[0] for item in cur.description), row)) if row else None
