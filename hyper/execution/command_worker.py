"""Execution control-plane command worker.

The Dashboard may enqueue these commands, but never writes execution business
state directly.  This worker is the single mutation boundary and is also the
only stopped-state process allowed to receive the RSA unwrap credential for a
Live preflight.
"""

from __future__ import annotations

import json
import os
from typing import Any

from hyper import storage
from hyper.util import now_iso

from . import control
from .credentials import decrypt_agent_wallet
from .hyperliquid_broker import HyperliquidBroker
from .live_preflight import activate_live_session, resolve_agent_expiry, run_live_preflight, unlock_live_canary
from .preflight import AccountPreflightCode, evaluate_account_preflight
from .sdk_clients import CredentialError, create_public_info_client
from .venue import ExecutionNetwork


CONTROL_COMMANDS = {
    "credential_upsert",
    "credential_verify",
    "credential_delete",
    "set_execution_mode",
    "execution_preflight",
    "activate_live",
    "unlock_live_canary",
}


def _private_wrap_key_path(explicit: str | None = None) -> str:
    path = explicit or os.environ.get("HL_CREDENTIAL_PRIVATE_KEY_FILE")
    if not path and os.path.isfile("secret/credential-wrap-private.pem"):
        path = "secret/credential-wrap-private.pem"
    if not path:
        raise RuntimeError("credential_worker_not_provisioned")
    return path


def _verify_credential(db, network: str, private_key_path: str) -> dict:
    normalized = ExecutionNetwork(str(network))
    row = control.credential_row(db, normalized, include_envelope=True)
    if not row:
        raise ValueError("credential_not_configured")
    try:
        wallet = decrypt_agent_wallet(
            row["envelope"],
            network=normalized.value,
            account_address=row["account_address"],
            agent_address=row["agent_address"],
            private_key_path=private_key_path,
        )
        info = create_public_info_client(normalized, supported_dexes=("", "xyz"), timeout=10.0)
        broker = HyperliquidBroker(
            normalized,
            row["account_address"],
            info_client=info,
            supported_dexes=("", "xyz"),
        )
        valid_until = resolve_agent_expiry(broker, wallet.address)
        identity = broker.identity_snapshot(wallet.address)
        snapshot = broker.account_snapshot()
        check = evaluate_account_preflight(identity, snapshot)
        # A credential remains valid while the account has positions, orders,
        # or too little money.  Those conditions belong to startup preflight.
        if check.code in {
            AccountPreflightCode.AGENT_MISMATCH,
            AccountPreflightCode.UNSUPPORTED_ACCOUNT_MODE,
            AccountPreflightCode.ACCOUNT_STATE_INVALID,
        }:
            raise ValueError(check.code.value)
        control.mark_credential_verified(db, normalized, valid_until=valid_until)
        return {
            "network": normalized.value,
            "status": "verified",
            "accountAddress": row["account_address"],
            "agentAddress": row["agent_address"],
            "accountMode": snapshot.abstraction,
            "validUntil": valid_until,
        }
    except CredentialError as exc:
        control.mark_credential_status(
            db, normalized, status="error", error_code=f"CREDENTIAL_{str(exc).upper()}",
        )
        raise ValueError("credential_verification_failed") from None


def execute_control_command(
    db,
    ctype: str,
    payload: dict[str, Any] | None,
    *,
    private_key_path: str | None = None,
) -> dict:
    payload = payload or {}
    if ctype == "credential_upsert":
        return control.store_encrypted_credential(
            db,
            network=payload["network"],
            account_address=payload["accountAddress"],
            agent_address=payload["agentAddress"],
            envelope=payload["envelope"],
        )
    if ctype == "credential_verify":
        return _verify_credential(
            db, payload["network"], _private_wrap_key_path(private_key_path),
        )
    if ctype == "credential_delete":
        return control.delete_credential(db, payload["network"])
    if ctype == "set_execution_mode":
        if payload.get("mode") == "live" and not control.credential_row(db, "mainnet"):
            raise ValueError("mainnet_credential_not_configured")
        return control.set_selected_mode(db, payload["mode"])
    if ctype == "execution_preflight":
        return run_live_preflight(
            db, private_wrap_key_path=_private_wrap_key_path(private_key_path),
        )
    if ctype == "activate_live":
        return activate_live_session(
            db, str(payload["preflightId"]), str(payload["confirmationPhrase"]),
        )
    if ctype == "unlock_live_canary":
        return unlock_live_canary(db, str(payload["confirmationPhrase"]))
    raise ValueError("unsupported_execution_control_command")


def process_pending_command(
    db_path: str,
    command_id: int,
    *,
    private_key_path: str | None = None,
) -> dict:
    db = storage.connect(db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT type,payload_json,status FROM commands WHERE id=?", (int(command_id),),
        ).fetchone()
        if not row:
            raise ValueError("command_not_found")
        ctype, payload_json, status = row
        if ctype not in CONTROL_COMMANDS:
            raise ValueError("command_not_owned_by_execution_control")
        if status == "done":
            db.rollback()
            return {"alreadyDone": True}
        if status not in {"pending", "acked"}:
            raise ValueError("command_not_processable")
        db.execute(
            "UPDATE commands SET status='acked',acked_at=? WHERE id=?",
            (now_iso(), int(command_id)),
        )
        db.commit()
        try:
            payload = json.loads(payload_json or "{}")
            result = execute_control_command(
                db, ctype, payload, private_key_path=private_key_path,
            )
            db.execute(
                "UPDATE commands SET status='done',done_at=?,result_json=?,error=NULL WHERE id=?",
                (now_iso(), json.dumps(result, sort_keys=True, separators=(",", ":")), int(command_id)),
            )
            db.commit()
            return result
        except Exception as exc:  # noqa: BLE001 - sanitized boundary below
            db.rollback()
            code = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            # Never propagate parser/crypto/SDK messages containing inputs.
            if "private" in code.lower() or len(code) > 160:
                code = "execution_control_failed"
            db.execute(
                "UPDATE commands SET status='failed',done_at=?,error=?,result_json=? WHERE id=?",
                (now_iso(), code, json.dumps({"error": code}), int(command_id)),
            )
            db.commit()
            raise RuntimeError(code) from None
    finally:
        db.close()


def process_all_pending(
    db_path: str,
    *,
    private_key_path: str | None = None,
) -> list[dict]:
    """Drain the bounded execution-control queue for the systemd oneshot."""
    db = storage.connect(db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
    try:
        ids = [
            int(row[0]) for row in db.execute(
                "SELECT id FROM commands WHERE status='pending' AND type IN ("
                + ",".join("?" for _ in CONTROL_COMMANDS)
                + ") ORDER BY id LIMIT 50",
                tuple(sorted(CONTROL_COMMANDS)),
            ).fetchall()
        ]
    finally:
        db.close()
    results = []
    for command_id in ids:
        try:
            results.append(process_pending_command(
                db_path, command_id, private_key_path=private_key_path,
            ))
        except RuntimeError:
            # One rejected operator request must not strand later independent
            # requests; its command row already contains the sanitized error.
            continue
    return results
