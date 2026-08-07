"""Protected control plane for the scanner's optional QuickNode credential.

The Dashboard queues only a browser-encrypted envelope. This worker is the
sole boundary that decrypts, validates, and atomically replaces the endpoint
file. Plaintext endpoints never enter SQLite, API responses, or logs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from hyper import config, storage
from hyper.execution.credentials import decrypt_wrapped_secret, validate_envelope
from hyper.execution.sdk_clients import CredentialError
from hyper.util import now_iso


COLLECTION_COMMANDS = {"collection_endpoint_upsert"}
COLLECTION_ENDPOINT_AAD = b"poly-btc-hyperliquid-collection-v1\nquicknode"
QUICKNODE_ENDPOINT_ENV = "HL_QUICKNODE_ENDPOINT_FILE"
DEFAULT_QUICKNODE_ENDPOINT_FILE = "secret/quicknode"
_MAX_ENDPOINT_BYTES = 2_048
VALID_COLLECTION_SOURCES = {"official", "quicknode"}


class CollectionSourceBusy(ValueError):
    """A source snapshot is already in use by a running collection job."""


class CollectionSourceUnavailable(ValueError):
    """QuickNode has not been configured and verified yet."""


def quicknode_endpoint_path(explicit: str | None = None) -> Path:
    return Path(
        explicit
        or os.environ.get(QUICKNODE_ENDPOINT_ENV)
        or DEFAULT_QUICKNODE_ENDPOINT_FILE
    )


def private_wrap_key_path(explicit: str | None = None) -> str:
    path = explicit or os.environ.get("HL_CREDENTIAL_PRIVATE_KEY_FILE")
    if not path and os.path.isfile("secret/credential-wrap-private.pem"):
        path = "secret/credential-wrap-private.pem"
    if not path:
        raise RuntimeError("collection_worker_not_provisioned")
    return path


def normalize_quicknode_endpoint(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw.encode("utf-8", errors="ignore")) > _MAX_ENDPOINT_BYTES:
        raise ValueError("quicknode_endpoint_invalid")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("quicknode_endpoint_invalid") from None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or not host.endswith(".quiknode.pro")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise ValueError("quicknode_endpoint_invalid")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if segments and segments[-1].lower() in {"evm", "nanoreth", "hypercore", "info"}:
        segments[-1] = "info"
    else:
        segments.append("info")
    path = "/" + "/".join(segments)
    return urllib.parse.urlunsplit(("https", parsed.netloc, path, "", ""))


def _quicknode_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in {400, 401, 403, 404, 422, 429} or 500 <= exc.code <= 599:
            return f"quicknode_http_{exc.code}"
        return "quicknode_http_error"
    if isinstance(exc, (TimeoutError, urllib.error.URLError)):
        return "quicknode_unavailable"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "quicknode_invalid_response"
    if isinstance(exc, ValueError) and str(exc).startswith("quicknode_"):
        return str(exc)
    return "quicknode_verification_failed"


def verify_quicknode_endpoint(endpoint: str, *, timeout: float = 15.0) -> dict:
    normalized = normalize_quicknode_endpoint(endpoint)
    request = urllib.request.Request(
        normalized,
        data=b'{"type":"meta"}',
        headers=config.UA,
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout))) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - sanitize before crossing the worker boundary
        raise ValueError(_quicknode_error(exc)) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("universe"), list) \
            or not payload["universe"]:
        raise ValueError("quicknode_invalid_response")
    return {"endpoint": normalized, "markets": len(payload["universe"])}


def _atomic_write_endpoint(path: Path, endpoint: str) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("quicknode_endpoint_file_invalid")
    fd, temporary = tempfile.mkstemp(prefix=".quicknode.", dir=str(parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        data = (endpoint + "\n").encode("utf-8")
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        try:
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _mark_verified(db, *, stamp: str) -> None:
    db.execute(
        "INSERT INTO collection_source_control "
        "(id,quicknode_configured,quicknode_status,quicknode_verified_at,"
        "quicknode_last_success_at,quicknode_error_code,quicknode_error_at,updated_at) "
        "VALUES (1,1,'verified',?,?,NULL,NULL,?) "
        "ON CONFLICT(id) DO UPDATE SET quicknode_configured=1,quicknode_status='verified',"
        "quicknode_verified_at=excluded.quicknode_verified_at,"
        "quicknode_last_success_at=excluded.quicknode_last_success_at,"
        "quicknode_error_code=NULL,quicknode_error_at=NULL,updated_at=excluded.updated_at",
        (stamp, stamp, stamp),
    )


def _mark_verification_error(db, code: str, *, stamp: str) -> None:
    db.execute(
        "INSERT INTO collection_source_control "
        "(id,quicknode_configured,quicknode_status,quicknode_error_code,"
        "quicknode_error_at,updated_at) VALUES (1,0,'error',?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "quicknode_status=CASE WHEN collection_source_control.quicknode_configured=1 "
        "AND collection_source_control.quicknode_verified_at IS NOT NULL "
        "THEN collection_source_control.quicknode_status ELSE 'error' END,"
        "quicknode_error_code=excluded.quicknode_error_code,"
        "quicknode_error_at=excluded.quicknode_error_at,updated_at=excluded.updated_at",
        (code, stamp, stamp),
    )


def store_encrypted_quicknode_endpoint(
    db,
    envelope: Any,
    *,
    private_key_path_value: str | None = None,
    endpoint_path_value: str | None = None,
) -> dict:
    validate_envelope(envelope)
    plaintext = decrypt_wrapped_secret(
        envelope,
        aad=COLLECTION_ENDPOINT_AAD,
        private_key_path=private_wrap_key_path(private_key_path_value),
    )
    try:
        try:
            candidate = bytes(plaintext).decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("quicknode_endpoint_invalid") from None
        verified = verify_quicknode_endpoint(candidate)
        _atomic_write_endpoint(
            quicknode_endpoint_path(endpoint_path_value), verified["endpoint"],
        )
    finally:
        for index in range(len(plaintext)):
            plaintext[index] = 0
    stamp = now_iso()
    _mark_verified(db, stamp=stamp)
    return {"provider": "quicknode", "status": "verified", "verifiedAt": stamp}


def verify_existing_endpoint(db, *, endpoint_path_value: str | None = None) -> dict:
    path = quicknode_endpoint_path(endpoint_path_value)
    try:
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode) \
                or file_stat.st_size <= 0 or file_stat.st_size > _MAX_ENDPOINT_BYTES:
            raise ValueError("quicknode_endpoint_file_invalid")
        candidate = path.read_text(encoding="utf-8")
    except OSError:
        raise ValueError("quicknode_endpoint_not_configured") from None
    verified = verify_quicknode_endpoint(candidate)
    _atomic_write_endpoint(path, verified["endpoint"])
    stamp = now_iso()
    _mark_verified(db, stamp=stamp)
    db.commit()
    return {"provider": "quicknode", "status": "verified", "verifiedAt": stamp}


def set_preferred_source(db_path: str, value: str) -> dict:
    """Change the source for future jobs without mutating an active snapshot."""
    source = str(value or "").strip().lower()
    if source not in VALID_COLLECTION_SOURCES:
        raise ValueError("collection_source_invalid")
    db = storage.connect(db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
    try:
        db.execute("BEGIN IMMEDIATE")
        progress = db.execute("SELECT state FROM scan_progress WHERE id=1").fetchone()
        process = db.execute(
            "SELECT state FROM process_status WHERE name='scanner'"
        ).fetchone()
        if (progress and progress[0] == "scanning") \
                or (process and process[0] == "scanning"):
            raise CollectionSourceBusy("collection_source_locked")
        if source == "quicknode":
            control = db.execute(
                "SELECT quicknode_configured,quicknode_verified_at "
                "FROM collection_source_control WHERE id=1"
            ).fetchone()
            if not control or not control[0] or not control[1]:
                raise CollectionSourceUnavailable("quicknode_not_verified")
        cur = db.execute(
            "UPDATE params SET value=?,updated_at=? WHERE key='COLLECTION_SOURCE'",
            (source, now_iso()),
        )
        if cur.rowcount != 1:
            raise RuntimeError("collection_source_param_missing")
        db.commit()
        return {"selectedSource": source}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def process_pending_command(
    db_path: str,
    command_id: int,
    *,
    private_key_path_value: str | None = None,
    endpoint_path_value: str | None = None,
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
        if ctype not in COLLECTION_COMMANDS:
            raise ValueError("command_not_owned_by_collection_control")
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
            result = store_encrypted_quicknode_endpoint(
                db,
                payload["envelope"],
                private_key_path_value=private_key_path_value,
                endpoint_path_value=endpoint_path_value,
            )
            db.execute(
                "UPDATE commands SET status='done',done_at=?,result_json=?,error=NULL WHERE id=?",
                (now_iso(), json.dumps(result, sort_keys=True, separators=(",", ":")), int(command_id)),
            )
            db.commit()
            return result
        except Exception as exc:  # noqa: BLE001 - all errors are fixed codes
            db.rollback()
            if isinstance(exc, (ValueError, RuntimeError, CredentialError)):
                code = str(exc)
            else:
                code = "collection_control_failed"
            if len(code) > 160 or any(token in code.lower() for token in ("http://", "https://")):
                code = "collection_control_failed"
            _mark_verification_error(db, code, stamp=now_iso())
            db.execute(
                "UPDATE commands SET status='failed',done_at=?,error=?,result_json=? WHERE id=?",
                (now_iso(), code, json.dumps({"error": code}), int(command_id)),
            )
            db.commit()
            raise RuntimeError(code) from None
    finally:
        db.close()


def process_all_pending(db_path: str) -> list[dict]:
    db = storage.connect(db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
    try:
        ids = [
            int(row[0])
            for row in db.execute(
                "SELECT id FROM commands WHERE status='pending' AND type=? ORDER BY id LIMIT 20",
                ("collection_endpoint_upsert",),
            ).fetchall()
        ]
    finally:
        db.close()
    results = []
    for command_id in ids:
        try:
            results.append(process_pending_command(db_path, command_id))
        except RuntimeError:
            continue
    return results
