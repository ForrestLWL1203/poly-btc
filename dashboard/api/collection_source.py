"""Dashboard projection for the scanner-only REST data source."""

from hyper.market.collection_control import (
    CollectionSourceBusy,
    CollectionSourceUnavailable,
    set_preferred_source,
)

from .common import q1


VALID_SOURCES = {"official", "quicknode"}


def _source(value, default="official"):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_SOURCES else default


def _quicknode_control(db):
    row = q1(
        db,
        "SELECT quicknode_configured,quicknode_status,quicknode_verified_at,"
        "quicknode_last_success_at,quicknode_error_code,quicknode_error_at,updated_at "
        "FROM collection_source_control WHERE id=1",
    )
    if not row:
        return {
            "configured": False,
            "status": "not_configured",
            "verifiedAt": None,
            "lastSuccessAt": None,
            "errorCode": None,
            "errorAt": None,
            "updatedAt": None,
        }
    return {
        "configured": bool(row["quicknode_configured"]),
        "status": row["quicknode_status"] or "not_configured",
        "verifiedAt": row["quicknode_verified_at"],
        "lastSuccessAt": row["quicknode_last_success_at"],
        "errorCode": row["quicknode_error_code"],
        "errorAt": row["quicknode_error_at"],
        "updatedAt": row["updated_at"],
    }


def ep_collection_source(db):
    preferred_row = q1(db, "SELECT value FROM params WHERE key='COLLECTION_SOURCE'")
    selected = _source(preferred_row["value"] if preferred_row else None)
    try:
        progress = q1(
            db,
            "SELECT state,selected_source,effective_source,source_fallback_reason,"
            "source_fallback_at FROM scan_progress WHERE id=1",
        )
        if progress is None:
            progress = q1(db, "SELECT state FROM scan_progress WHERE id=1")
    except Exception:  # noqa: BLE001 - compact legacy databases used by diagnostics/tests
        progress = q1(db, "SELECT state FROM scan_progress WHERE id=1")
    active = bool(progress and progress["state"] == "scanning")
    effective = None
    fallback_reason = None
    fallback_at = None
    current_selected = selected
    if active:
        keys = progress.keys()
        if "selected_source" in keys:
            current_selected = _source(progress["selected_source"], selected)
            effective = _source(progress["effective_source"], current_selected)
            fallback_reason = progress["source_fallback_reason"]
            fallback_at = progress["source_fallback_at"]
        else:
            effective = current_selected
    return {
        "selectedSource": selected,
        "currentTaskSelectedSource": current_selected if active else None,
        "effectiveSource": effective,
        "active": active,
        "switchLocked": active,
        "fallback": bool(active and effective != current_selected),
        "fallbackReason": fallback_reason,
        "fallbackAt": fallback_at,
        "quicknode": _quicknode_control(db),
    }


def set_collection_source(db_path, value):
    """Backward-compatible Dashboard adapter for the product control plane."""
    return set_preferred_source(db_path, value)
