"""Durable Paper/Live execution control-plane state.

Dashboard requests arrive through the existing ``commands`` table.  Execution workers call this module to
mutate credential metadata, mode, sessions and safety state.  Public projections intentionally omit encrypted
credential envelopes and private operational details.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from hyper.util import now_iso
from .credentials import validate_envelope
from .sdk_clients import CredentialError, _normalize_address
from .venue import ExecutionMode, ExecutionNetwork


EXECUTION_STATES = {
    "paper",
    "live_ready",
    "live_canary",
    "live_running",
    "paused",
    "draining",
    "reconcile_required",
    "credential_error",
    "no_funds",
}


def ensure_execution_control(db) -> None:
    stamp = now_iso()
    db.execute(
        "INSERT INTO execution_control (id,selected_mode,state,updated_at) "
        "VALUES (1,'paper','paper',?) ON CONFLICT(id) DO NOTHING",
        (stamp,),
    )


def _network(value: ExecutionNetwork | str) -> ExecutionNetwork:
    try:
        return value if isinstance(value, ExecutionNetwork) else ExecutionNetwork(str(value))
    except ValueError:
        raise ValueError("unsupported_execution_network") from None


def credential_row(db, network: ExecutionNetwork | str, *, include_envelope: bool = False) -> Optional[dict]:
    normalized = _network(network).value
    columns = (
        "network,account_address,agent_address,wrap_key_id,status,valid_until,verified_at,"
        "error_code,created_at,updated_at" + (",envelope_json" if include_envelope else "")
    )
    row = db.execute(f"SELECT {columns} FROM execution_credential WHERE network=?", (normalized,)).fetchone()
    if not row:
        return None
    names = [item.strip() for item in columns.split(",")]
    data = dict(zip(names, row))
    if include_envelope:
        try:
            data["envelope"] = json.loads(data.pop("envelope_json") or "{}")
        except (TypeError, ValueError):
            raise CredentialError("invalid_stored_credential_envelope") from None
    return data


def store_encrypted_credential(
    db,
    *,
    network: ExecutionNetwork | str,
    account_address: str,
    agent_address: str,
    envelope: Any,
) -> dict:
    normalized_network = _network(network).value
    if normalized_network == ExecutionNetwork.MAINNET.value:
        active = db.execute(
            "SELECT 1 FROM execution_session WHERE network='mainnet' "
            "AND state IN ('starting','live_canary','live_running','paused','draining','reconcile_required') LIMIT 1"
        ).fetchone()
        if active:
            raise ValueError("mainnet_credential_in_use")
    account = _normalize_address(account_address, error_code="invalid_account_address")
    agent = _normalize_address(agent_address, error_code="invalid_expected_agent_address")
    normalized_envelope = validate_envelope(envelope)
    stamp = now_iso()
    db.execute(
        "INSERT INTO execution_credential "
        "(network,account_address,agent_address,envelope_json,wrap_key_id,status,valid_until,"
        "verified_at,error_code,created_at,updated_at) VALUES (?,?,?,?,?,'encrypted',?,NULL,NULL,?,?) "
        "ON CONFLICT(network) DO UPDATE SET account_address=excluded.account_address,"
        "agent_address=excluded.agent_address,envelope_json=excluded.envelope_json,"
        "wrap_key_id=excluded.wrap_key_id,status='encrypted',valid_until=excluded.valid_until,"
        "verified_at=NULL,error_code=NULL,updated_at=excluded.updated_at",
        (
            normalized_network,
            account,
            agent,
            json.dumps(normalized_envelope, sort_keys=True, separators=(",", ":")),
            normalized_envelope["wrapKeyId"],
            None,
            stamp,
            stamp,
        ),
    )
    ensure_execution_control(db)
    db.execute(
        "UPDATE execution_control SET state=CASE WHEN selected_mode='live' THEN 'credential_error' ELSE state END,"
        "last_error_code=NULL,updated_at=? WHERE id=1",
        (stamp,),
    )
    return {
        "network": normalized_network,
        "accountAddress": account,
        "agentAddress": agent,
        "status": "encrypted",
        "validUntil": None,
    }


def mark_credential_status(
    db,
    network: ExecutionNetwork | str,
    *,
    status: str,
    error_code: Optional[str] = None,
) -> None:
    if status not in {"encrypted", "verified", "error", "expired", "revoked"}:
        raise ValueError("invalid_credential_status")
    stamp = now_iso()
    db.execute(
        "UPDATE execution_credential SET status=?,verified_at=?,error_code=?,updated_at=? WHERE network=?",
        (
            status,
            stamp if status == "verified" else None,
            error_code,
            stamp,
            _network(network).value,
        ),
    )


def mark_credential_verified(
    db,
    network: ExecutionNetwork | str,
    *,
    valid_until: str,
) -> None:
    """Persist verification together with the venue-authoritative expiry."""
    stamp = now_iso()
    db.execute(
        "UPDATE execution_credential SET status='verified',valid_until=?,verified_at=?,"
        "error_code=NULL,updated_at=? WHERE network=?",
        (valid_until, stamp, stamp, _network(network).value),
    )


def delete_credential(db, network: ExecutionNetwork | str) -> dict:
    normalized = _network(network).value
    if normalized == ExecutionNetwork.MAINNET.value:
        active = db.execute(
            "SELECT 1 FROM execution_session WHERE network='mainnet' "
            "AND state IN ('starting','live_canary','live_running','paused','draining','reconcile_required') LIMIT 1"
        ).fetchone()
        if active:
            raise ValueError("mainnet_credential_in_use")
    deleted = db.execute("DELETE FROM execution_credential WHERE network=?", (normalized,)).rowcount
    return {"network": normalized, "deleted": bool(deleted), "authorizationRevocationRequired": bool(deleted)}


def set_control_state(db, state: str, *, error_code: Optional[str] = None) -> None:
    if state not in EXECUTION_STATES:
        raise ValueError("invalid_execution_state")
    ensure_execution_control(db)
    stamp = now_iso()
    db.execute(
        "UPDATE execution_control SET state=?,last_error_code=?,last_error_at=?,updated_at=? WHERE id=1",
        (state, error_code, stamp if error_code else None, stamp),
    )


def set_selected_mode(db, mode: ExecutionMode | str) -> dict:
    try:
        normalized = mode if isinstance(mode, ExecutionMode) else ExecutionMode(str(mode))
    except ValueError:
        raise ValueError("invalid_execution_mode") from None
    ensure_execution_control(db)
    if normalized is ExecutionMode.LIVE:
        credential = credential_row(db, ExecutionNetwork.MAINNET)
        if not credential or credential.get("status") != "verified":
            raise ValueError("mainnet_credential_not_verified")
    current = db.execute(
        "SELECT selected_mode,state,active_session_id FROM execution_control WHERE id=1"
    ).fetchone()
    if current and current[0] != normalized.value:
        observer = db.execute(
            "SELECT state FROM process_status WHERE name='observer'"
        ).fetchone()
        if observer and str(observer[0] or "stopped") not in {"stopped", "error", "failed"}:
            raise ValueError("observer_must_be_stopped")
    if normalized is ExecutionMode.PAPER and current and current[2]:
        exposure = db.execute(
            "SELECT COUNT(*) FROM execution_position_projection WHERE session_id=? AND ABS(signed_size)>1e-12",
            (current[2],),
        ).fetchone()[0]
        orders = db.execute(
            "SELECT COUNT(*) FROM execution_order_intent WHERE session_id=? "
            "AND state IN ('created','submitting','resting','ambiguous')",
            (current[2],),
        ).fetchone()[0]
        if exposure or orders:
            raise ValueError("live_exposure_prevents_paper_switch")
    stamp = now_iso()
    next_state = "paper" if normalized is ExecutionMode.PAPER else "live_ready"
    db.execute(
        "UPDATE execution_control SET selected_mode=?,state=?,"
        "active_session_id=CASE WHEN ?='paper' THEN NULL ELSE active_session_id END,updated_at=? WHERE id=1",
        (normalized.value, next_state, normalized.value, stamp),
    )
    return {"selectedMode": normalized.value, "state": next_state}


def execution_status(db) -> dict:
    control = db.execute(
        "SELECT selected_mode,state,active_session_id,canary_unlocked,last_error_code,last_error_at,updated_at "
        "FROM execution_control WHERE id=1"
    ).fetchone()
    # Status is also served through Dashboard read-only connections.  Fresh
    # databases therefore get a synthesized Paper projection instead of a
    # write-on-GET initialization.
    if not control:
        control = ("paper", "paper", None, 0, None, None, None)
    credentials = {}
    for row in db.execute(
        "SELECT network,account_address,agent_address,status,valid_until,verified_at,error_code,updated_at "
        "FROM execution_credential ORDER BY network"
    ).fetchall():
        credentials[row[0]] = {
            "network": row[0],
            "accountAddress": row[1],
            "agentAddress": row[2],
            "status": row[3],
            "validUntil": row[4],
            "verifiedAt": row[5],
            "errorCode": row[6],
            "updatedAt": row[7],
        }
    session = None
    if control[2]:
        row = db.execute(
            "SELECT session_id,state,network,account_address,agent_address,strategy_revision,sizing_anchor,"
            "margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at "
            "FROM execution_session WHERE session_id=?",
            (control[2],),
        ).fetchone()
        if row:
            session = {
                "sessionId": row[0], "state": row[1], "network": row[2],
                "accountAddress": row[3], "agentAddress": row[4], "strategyRevision": row[5],
                "sizingAnchor": row[6], "marginEquityPct": row[7], "sizingEquity": row[8],
                "canary": bool(row[9]), "canaryMarginCap": row[10], "startedAt": row[11],
                "updatedAt": row[12],
            }
    latest_preflight = db.execute(
        "SELECT preflight_id,status,code,equity,available,sizing_equity,position_count,open_order_count,"
        "created_at,expires_at,consumed_at,details_json FROM execution_preflight ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    checks = None
    if latest_preflight:
        try:
            details = json.loads(latest_preflight[11] or "{}")
            checks = {str(key): bool(value) for key, value in details.items()}
        except (TypeError, ValueError):
            checks = None
    preflight = None if not latest_preflight else {
        "preflightId": latest_preflight[0], "status": latest_preflight[1], "code": latest_preflight[2],
        "equity": latest_preflight[3], "available": latest_preflight[4],
        "sizingEquity": latest_preflight[5], "positionCount": latest_preflight[6],
        "openOrderCount": latest_preflight[7], "createdAt": latest_preflight[8],
        "expiresAt": latest_preflight[9], "consumedAt": latest_preflight[10], "checks": checks,
    }
    reconcile = None
    account = None
    if control[2]:
        row = db.execute(
            "SELECT status,position_count,open_order_count,unknown_positions,unknown_orders,created_at "
            "FROM execution_reconcile_checkpoint WHERE session_id=? ORDER BY checkpoint_id DESC LIMIT 1",
            (control[2],),
        ).fetchone()
        if row:
            reconcile = {
                "status": row[0], "positionCount": row[1], "openOrderCount": row[2],
                "unknownPositions": row[3], "unknownOrders": row[4], "createdAt": row[5],
            }
        row = db.execute(
            "SELECT equity,available,margin_used,unrealized_pnl,observed_at "
            "FROM execution_account_snapshot WHERE session_id=? ORDER BY snapshot_id DESC LIMIT 1",
            (control[2],),
        ).fetchone()
        if row:
            position_count = db.execute(
                "SELECT COUNT(*) FROM execution_position_projection "
                "WHERE session_id=? AND ABS(signed_size)>1e-12", (control[2],),
            ).fetchone()[0]
            active_order_count = db.execute(
                "SELECT COUNT(*) FROM execution_order_intent WHERE session_id=? "
                "AND state IN ('created','submitting','resting','ambiguous')", (control[2],),
            ).fetchone()[0]
            account = {
                "equity": row[0], "available": row[1], "marginUsed": row[2],
                "unrealizedPnl": row[3], "observedAt": row[4],
                "positionCount": position_count, "activeOrderCount": active_order_count,
            }
    return {
        "selectedMode": control[0],
        "state": control[1],
        "activeSessionId": control[2],
        "canaryUnlocked": bool(control[3]),
        "lastErrorCode": control[4],
        "lastErrorAt": control[5],
        "updatedAt": control[6],
        "credentials": credentials,
        "session": session,
        "preflight": preflight,
        "reconcile": reconcile,
        "account": account,
    }
