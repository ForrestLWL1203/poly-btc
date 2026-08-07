"""Scanner-only QuickNode provider bootstrap and durable status projection."""

from __future__ import annotations

import json
import sqlite3
import stat

from hyper import params
from hyper.util import now_iso

from . import rest
from .collection_control import normalize_quicknode_endpoint, quicknode_endpoint_path


def _db_path(db) -> str | None:
    try:
        row = db.execute("PRAGMA database_list").fetchone()
        value = row[2] if row else None
    except sqlite3.Error:
        return None
    return str(value) if value and str(value) != ":memory:" else None


def read_quicknode_endpoint() -> str | None:
    path = quicknode_endpoint_path()
    try:
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            return None
        if stat.S_IMODE(file_stat.st_mode) & 0o077:
            return None
        if file_stat.st_size <= 0 or file_stat.st_size > 2_048:
            return None
        return normalize_quicknode_endpoint(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _persist_runtime_state(db_path: str | None, state: dict) -> None:
    if not db_path:
        return
    connection = None
    try:
        connection = sqlite3.connect(db_path, timeout=2)
        connection.execute("PRAGMA busy_timeout=2000")
        selected = str(state.get("selectedSource") or "official")
        effective = str(state.get("effectiveSource") or "official")
        reason = str(state.get("fallbackReason") or "")[:80] or None
        fallback_at = state.get("fallbackAt")
        connection.execute(
            "UPDATE scan_progress SET selected_source=?,effective_source=?,"
            "source_fallback_reason=?,source_fallback_at=? WHERE id=1",
            (selected, effective, reason, fallback_at),
        )
        if selected == "quicknode" and state.get("quicknodeHealthy"):
            stamp = now_iso()
            connection.execute(
                "INSERT INTO collection_source_control "
                "(id,quicknode_configured,quicknode_status,quicknode_last_success_at,updated_at) "
                "VALUES (1,1,'verified',?,?) ON CONFLICT(id) DO UPDATE SET "
                "quicknode_configured=1,quicknode_status='verified',"
                "quicknode_last_success_at=excluded.quicknode_last_success_at,"
                "quicknode_error_code=NULL,quicknode_error_at=NULL,updated_at=excluded.updated_at",
                (stamp, stamp),
            )
        elif selected == "quicknode" and effective == "official" and reason:
            stamp = fallback_at or now_iso()
            configured = 0 if reason == "quicknode_not_configured" else 1
            status = "missing" if not configured else "fallback"
            connection.execute(
                "INSERT INTO collection_source_control "
                "(id,quicknode_configured,quicknode_status,quicknode_error_code,"
                "quicknode_error_at,updated_at) VALUES (1,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET quicknode_configured=excluded.quicknode_configured,"
                "quicknode_status=excluded.quicknode_status,"
                "quicknode_error_code=excluded.quicknode_error_code,"
                "quicknode_error_at=excluded.quicknode_error_at,updated_at=excluded.updated_at",
                (configured, status, reason, stamp, stamp),
            )
        connection.commit()
    except sqlite3.Error:
        if connection is not None:
            connection.rollback()
    finally:
        if connection is not None:
            connection.close()


def _inherited_generation_state(db, generation_id: str | None) -> dict:
    if not generation_id:
        row = db.execute(
            "SELECT metrics_json FROM scan_generation "
            "WHERE status NOT IN ('published','failed') AND leaderboard_valid=1 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        row = db.execute(
            "SELECT metrics_json FROM scan_generation WHERE generation=?", (generation_id,),
        ).fetchone()
    if not row:
        return {}
    try:
        metrics = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return {}
    state = metrics.get("collectionSource")
    return state if isinstance(state, dict) else {}


def configure_for_job(db, *, generation_id: str | None = None, inherit: bool = False) -> dict:
    """Snapshot operator selection for one scanner/finalizer job."""
    selected = str(params.get(db, "COLLECTION_SOURCE", "official") or "official").lower()
    if selected not in {"official", "quicknode"}:
        selected = "official"
    path = _db_path(db)
    state = rest.configure_collection_source(
        selected=selected,
        quicknode_endpoint=read_quicknode_endpoint(),
        inherited_state=_inherited_generation_state(db, generation_id) if inherit else None,
        quicknode_rps=10.0,
        on_change=lambda next_state: _persist_runtime_state(path, next_state),
    )
    return state


def progress_fields() -> dict:
    state = rest.collection_source_state()
    return {
        "selected_source": state["selectedSource"],
        "effective_source": state["effectiveSource"],
        "source_fallback_reason": state.get("fallbackReason"),
        "source_fallback_at": state.get("fallbackAt"),
    }


def generation_metrics() -> dict:
    state = rest.collection_source_state()
    return {
        "selectedSource": state["selectedSource"],
        "effectiveSource": state["effectiveSource"],
        "fallbackReason": state.get("fallbackReason"),
        "fallbackAt": state.get("fallbackAt"),
    }
