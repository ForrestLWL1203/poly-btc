"""Mainnet Live preflight and short-lived activation grant.

This module performs no exchange action.  It unwraps the configured Agent only to prove the public Agent
address, validates the authoritative Mainnet account/strategy state, and persists a bounded preflight grant.
The actual signer remains disabled unless a later execution worker loads a live session created from that grant.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import subprocess
import time
import uuid
from typing import Callable, Optional

from hyper import config
from hyper.selection import strategy_revision
from hyper.util import now_iso
from .control import (
    credential_row,
    ensure_execution_control,
    mark_credential_status,
    mark_credential_verified,
    set_control_state,
)
from .credentials import decrypt_agent_wallet
from .hyperliquid_broker import BrokerError, HyperliquidBroker
from .preflight import AccountPreflightCode, evaluate_account_preflight
from .sdk_clients import CredentialError, create_public_info_client
from .venue import ExecutionNetwork, venue_config


PREFLIGHT_TTL_SECONDS = 300
LIVE_CONFIRMATION_PHRASE = "启动实盘"
CANARY_UNLOCK_PHRASE = "解除 Canary"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_agent_expiry(value: str | None, *, minimum_days: int = 7) -> datetime:
    if not value:
        raise CredentialError("agent_expiry_required")
    try:
        expiry = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise CredentialError("agent_expiry_invalid") from None
    if expiry <= datetime.now(timezone.utc):
        raise CredentialError("agent_expired")
    if expiry - datetime.now(timezone.utc) < timedelta(days=minimum_days):
        raise CredentialError(f"agent_expiry_under_{minimum_days}d")
    return expiry


def resolve_agent_expiry(
    broker: HyperliquidBroker,
    agent_address: str,
    *,
    minimum_days: int = 7,
) -> str:
    """Resolve and validate an Agent expiry from Hyperliquid account state."""
    try:
        authorization = broker.agent_authorization(agent_address)
    except BrokerError:
        raise CredentialError("agent_authorization_query_failed") from None
    if not authorization:
        raise CredentialError("agent_authorization_not_found")
    try:
        expiry = datetime.fromtimestamp(
            int(authorization["validUntil"]) / 1000,
            tz=timezone.utc,
        )
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        raise CredentialError("agent_expiry_invalid") from None
    value = _iso(expiry)
    validate_agent_expiry(value, minimum_days=minimum_days)
    return value


def _usdc(snapshot) -> tuple[float, float]:
    balances = snapshot.collateral_state.get("balances") if isinstance(snapshot.collateral_state, dict) else None
    for row in balances or []:
        if isinstance(row, dict) and row.get("coin") == "USDC":
            try:
                total = float(row.get("total") or 0)
                hold = float(row.get("hold") or 0)
            except (TypeError, ValueError):
                return 0.0, 0.0
            return max(0.0, total), max(0.0, total - hold)
    return 0.0, 0.0


def _snapshot_hash(
    *,
    account: str,
    agent: str,
    revision: str,
    params_hash: str,
    equity: float,
    available: float,
    positions: int,
    orders: int,
) -> str:
    payload = {
        "account": account.lower(), "agent": agent.lower(), "revision": revision,
        "paramsHash": params_hash, "equity": format(equity, ".8f"),
        "available": format(available, ".8f"), "positions": int(positions), "orders": int(orders),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _observer_stopped(db) -> bool:
    row = db.execute("SELECT state FROM process_status WHERE name='observer'").fetchone()
    return not row or str(row[0] or "stopped") in {"stopped", "error", "failed"}


def _system_clock_ok() -> bool:
    override = os.environ.get("HL_ASSUME_CLOCK_SYNCED")
    if override is not None:
        return override.lower() in {"1", "true", "yes"}
    if os.name != "posix" or not os.path.exists("/run/systemd/system"):
        return True  # local development host; production systemd is checked below
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            check=False, capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            return result.stdout.strip().lower() == "yes"
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.exists("/run/systemd/timesync/synchronized")


def probe_websocket(timeout: float = 6.0) -> bool:
    from websockets.sync.client import connect

    with connect(venue_config(ExecutionNetwork.MAINNET).ws_url, open_timeout=timeout, close_timeout=1) as ws:
        ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            if message.get("channel") in {"subscriptionResponse", "allMids"}:
                return True
    return False


def _store_result(
    db,
    *,
    preflight_id: str,
    account: str,
    agent: str,
    revision: str,
    snapshot_hash: str,
    ok: bool,
    code: str,
    equity: float,
    available: float,
    sizing_equity: float,
    positions: int,
    orders: int,
    details: dict,
) -> dict:
    created = datetime.now(timezone.utc)
    expires = created + timedelta(seconds=PREFLIGHT_TTL_SECONDS)
    db.execute(
        "INSERT INTO execution_preflight "
        "(preflight_id,network,account_address,agent_address,strategy_revision,snapshot_hash,status,code,"
        "equity,available,sizing_equity,position_count,open_order_count,details_json,created_at,expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            preflight_id, "mainnet", account, agent, revision or "none", snapshot_hash,
            "passed" if ok else "failed", code, equity, available, sizing_equity, positions, orders,
            json.dumps(details, sort_keys=True, separators=(",", ":")), _iso(created), _iso(expires),
        ),
    )
    set_control_state(
        db,
        "live_ready" if ok else "no_funds" if code in {
            AccountPreflightCode.NO_AVAILABLE_COLLATERAL.value,
            AccountPreflightCode.NO_EXECUTABLE_CAPACITY.value,
        } else "credential_error" if code.startswith("CREDENTIAL_") else "reconcile_required",
        error_code=None if ok else code,
    )
    return {
        "ok": ok, "preflightId": preflight_id, "status": "passed" if ok else "failed",
        "code": code, "equity": equity, "available": available, "sizingEquity": sizing_equity,
        "positionCount": positions, "openOrderCount": orders, "expiresAt": _iso(expires),
        "checks": details,
    }


def run_live_preflight(
    db,
    *,
    private_wrap_key_path: str,
    websocket_probe: Callable[[], bool] = probe_websocket,
    info_factory: Callable = create_public_info_client,
) -> dict:
    """Run a clean-account Mainnet activation preflight and persist a five-minute grant."""
    ensure_execution_control(db)
    preflight_id = f"preflight-{uuid.uuid4().hex}"
    credential = credential_row(db, ExecutionNetwork.MAINNET, include_envelope=True)
    account = credential.get("account_address") if credential else "0x" + "0" * 40
    agent = credential.get("agent_address") if credential else "0x" + "0" * 40
    revision_bundle = strategy_revision.load_active(db)
    revision = str((revision_bundle or {}).get("revision") or "none")
    params_hash = str((revision_bundle or {}).get("paramsHash") or "")
    equity = available = sizing_equity = 0.0
    positions = orders = 0
    snapshot_hash = _snapshot_hash(
        account=account, agent=agent, revision=revision, params_hash=params_hash,
        equity=0, available=0, positions=0, orders=0,
    )
    checks = {
        "observerStopped": _observer_stopped(db),
        "clockSynchronized": _system_clock_ok(),
        "credentialConfigured": bool(credential),
        "credentialVerified": False,
        "agentOwnerMatches": False,
        "unifiedAccount": False,
        "rest": False,
        "websocket": False,
        "strategyRevision": False,
        "activeTargets": False,
        "marketMetadata": False,
        "cleanAccount": False,
        "funded": False,
    }
    code = "OK"
    try:
        if not checks["observerStopped"]:
            raise RuntimeError("OBSERVER_MUST_BE_STOPPED")
        if not checks["clockSynchronized"]:
            raise RuntimeError("SYSTEM_CLOCK_NOT_SYNCHRONIZED")
        if not credential:
            raise CredentialError("credential_not_configured")
        wallet = decrypt_agent_wallet(
            credential["envelope"], network="mainnet", account_address=account,
            agent_address=agent, private_key_path=private_wrap_key_path,
        )
        checks["credentialVerified"] = wallet.address.lower() == agent.lower()

        info = info_factory(ExecutionNetwork.MAINNET, supported_dexes=("", "xyz"), timeout=10.0)
        broker = HyperliquidBroker(
            ExecutionNetwork.MAINNET, account, info_client=info, supported_dexes=("", "xyz"),
        )
        valid_until = resolve_agent_expiry(broker, agent)
        identity = broker.identity_snapshot(agent)
        snapshot = broker.account_snapshot()
        checks["rest"] = True
        account_check = evaluate_account_preflight(identity, snapshot)
        checks["agentOwnerMatches"] = account_check.code is not AccountPreflightCode.AGENT_MISMATCH
        checks["unifiedAccount"] = snapshot.abstraction == "unifiedAccount"
        positions = account_check.position_count
        orders = account_check.open_order_count
        checks["cleanAccount"] = positions == 0 and orders == 0
        equity, available = _usdc(snapshot)
        margin_pct = float((revision_bundle or {}).get("params", {}).get(
            "MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT,
        ))
        margin_pct = max(0.0, min(1.0, margin_pct))
        sizing_equity = equity * margin_pct
        # Hyperliquid's $10 rule is order notional, not wallet equity or isolated margin. A leveraged
        # order may satisfy it with less than $10 collateral, so preflight only proves a positive sizing
        # base; the shared per-order planner remains authoritative for the venue notional floor.
        checks["funded"] = available > 0 and sizing_equity > 0
        if not account_check.ok:
            raise RuntimeError(account_check.code.value)
        if not checks["funded"]:
            raise RuntimeError(AccountPreflightCode.NO_EXECUTABLE_CAPACITY.value)

        checks["strategyRevision"] = bool(
            revision_bundle and revision_bundle.get("status") == "active"
            and revision_bundle.get("selectionGeneration")
        )
        if not checks["strategyRevision"]:
            raise RuntimeError("STRATEGY_REVISION_INVALID")
        targets = strategy_revision.resolved_targets(db, revision_bundle)
        checks["activeTargets"] = bool(targets)
        if not targets:
            raise RuntimeError("NO_EXECUTABLE_CORE_TARGETS")
        specs = broker.load_market_specs()
        required_coins = {
            coin for target in targets for coin in (target.get("seedCoins") or []) if coin
        }
        checks["marketMetadata"] = bool(specs) and required_coins.issubset(specs)
        if not checks["marketMetadata"]:
            raise RuntimeError("MARKET_METADATA_INCOMPLETE")
        checks["websocket"] = bool(websocket_probe())
        if not checks["websocket"]:
            raise RuntimeError("WEBSOCKET_UNAVAILABLE")

        snapshot_hash = _snapshot_hash(
            account=account, agent=agent, revision=revision, params_hash=params_hash,
            equity=equity, available=available, positions=positions, orders=orders,
        )
        mark_credential_verified(
            db, ExecutionNetwork.MAINNET, valid_until=valid_until,
        )
        return _store_result(
            db, preflight_id=preflight_id, account=account, agent=agent, revision=revision,
            snapshot_hash=snapshot_hash, ok=True, code="OK", equity=equity, available=available,
            sizing_equity=sizing_equity, positions=positions, orders=orders, details=checks,
        )
    except CredentialError as exc:
        code = f"CREDENTIAL_{str(exc).upper()}"
        if credential:
            mark_credential_status(db, ExecutionNetwork.MAINNET, status="error", error_code=code)
    except Exception as exc:  # noqa: BLE001 - only enumerated/sanitized codes leave this boundary
        candidate = str(exc)
        allowed = {
            "OBSERVER_MUST_BE_STOPPED", "SYSTEM_CLOCK_NOT_SYNCHRONIZED",
            "UNSUPPORTED_ACCOUNT_MODE", "ACCOUNT_STATE_INVALID", "ACCOUNT_NOT_CLEAN",
            "NO_AVAILABLE_COLLATERAL", "NO_EXECUTABLE_CAPACITY", "AGENT_MISMATCH",
            "STRATEGY_REVISION_INVALID", "NO_EXECUTABLE_CORE_TARGETS", "MARKET_METADATA_INCOMPLETE",
            "WEBSOCKET_UNAVAILABLE",
        }
        code = candidate if candidate in allowed else f"PREFLIGHT_{type(exc).__name__.upper()}"
    return _store_result(
        db, preflight_id=preflight_id, account=account, agent=agent, revision=revision,
        snapshot_hash=snapshot_hash, ok=False, code=code, equity=equity, available=available,
        sizing_equity=sizing_equity, positions=positions, orders=orders, details=checks,
    )


def activate_live_session(db, preflight_id: str, confirmation_phrase: str) -> dict:
    if confirmation_phrase != LIVE_CONFIRMATION_PHRASE:
        raise ValueError("live_confirmation_phrase_mismatch")
    row = db.execute(
        "SELECT network,account_address,agent_address,strategy_revision,status,code,equity,sizing_equity,"
        "expires_at,consumed_at FROM execution_preflight WHERE preflight_id=?",
        (str(preflight_id),),
    ).fetchone()
    if not row or row[4] != "passed" or row[5] != "OK":
        raise ValueError("live_preflight_not_passed")
    if row[9]:
        raise ValueError("live_preflight_already_consumed")
    try:
        expires = datetime.strptime(row[8], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise ValueError("live_preflight_invalid_expiry") from None
    if datetime.now(timezone.utc) >= expires:
        raise ValueError("live_preflight_expired")
    active = db.execute(
        "SELECT session_id FROM execution_session WHERE state IN "
        "('starting','live_canary','live_running','paused','draining','reconcile_required') LIMIT 1"
    ).fetchone()
    if active:
        raise ValueError("live_session_already_active")
    bundle = strategy_revision.load_active(db)
    if not bundle or bundle.get("revision") != row[3]:
        raise ValueError("strategy_revision_changed_since_preflight")
    margin_pct = float(bundle.get("params", {}).get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT))
    session_id = f"live-{uuid.uuid4().hex}"
    stamp = now_iso()
    db.execute(
        "INSERT INTO execution_session "
        "(session_id,mode,network,state,account_address,agent_address,strategy_revision,preflight_id,"
        "sizing_anchor,margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at,updated_at) "
        "VALUES (?,'live',?,'starting',?,?,?,?,?,?,?,0,NULL,?,?)",
        (
            session_id, row[0], row[1], row[2], row[3], preflight_id, float(row[6]), margin_pct,
            float(row[7]), stamp, stamp,
        ),
    )
    db.execute("UPDATE execution_preflight SET consumed_at=? WHERE preflight_id=?", (stamp, preflight_id))
    ensure_execution_control(db)
    db.execute(
        "UPDATE execution_control SET selected_mode='live',state='live_running',active_session_id=?,"
        "canary_unlocked=1,last_error_code=NULL,last_error_at=NULL,updated_at=? WHERE id=1",
        (session_id, stamp),
    )
    return {
        "sessionId": session_id, "state": "live_running", "canary": False,
        "canaryMarginCap": None, "sizingAnchor": float(row[6]),
        "sizingEquity": float(row[7]),
    }


def unlock_live_canary(db, confirmation_phrase: str) -> dict:
    """Promote a legacy active Canary after a clean, flat reconciliation.

    New sessions start directly in full Live mode. This compatibility command
    exists only to remove the retired cap from a session created by an older
    deployment without mutating execution state outside the control worker.
    """
    if confirmation_phrase != CANARY_UNLOCK_PHRASE:
        raise ValueError("canary_confirmation_phrase_mismatch")
    row = db.execute(
        "SELECT s.session_id,s.state,s.canary,s.started_at FROM execution_control c "
        "JOIN execution_session s ON s.session_id=c.active_session_id "
        "WHERE c.id=1 AND c.selected_mode='live'"
    ).fetchone()
    if not row or not row[2] or row[1] not in {"live_canary", "paused"}:
        raise ValueError("live_canary_not_active")
    session_id = row[0]
    checkpoint = db.execute(
        "SELECT status,unknown_positions,unknown_orders FROM execution_reconcile_checkpoint "
        "WHERE session_id=? ORDER BY checkpoint_id DESC LIMIT 1", (session_id,),
    ).fetchone()
    if not checkpoint or checkpoint[0] != "ok" or checkpoint[1] or checkpoint[2]:
        raise ValueError("live_canary_reconcile_not_clean")
    positions = db.execute(
        "SELECT COUNT(*) FROM execution_position_projection WHERE session_id=? AND ABS(signed_size)>1e-12",
        (session_id,),
    ).fetchone()[0]
    orders = db.execute(
        "SELECT COUNT(*) FROM execution_order_intent WHERE session_id=? "
        "AND state IN ('created','submitting','resting','ambiguous')", (session_id,),
    ).fetchone()[0]
    if positions or orders:
        raise ValueError("live_canary_must_be_flat")
    stamp = now_iso()
    db.execute(
        "UPDATE execution_session SET canary=0,state='live_running',updated_at=? WHERE session_id=?",
        (stamp, session_id),
    )
    db.execute(
        "UPDATE execution_control SET state='live_running',canary_unlocked=1,updated_at=? WHERE id=1",
        (stamp,),
    )
    return {"sessionId": session_id, "state": "live_running", "canary": False}
