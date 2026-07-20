"""Envelope-encrypted provider credentials for the Observer.

The browser encrypts a secret with a one-time AES-GCM key, then wraps that key with this instance's
RSA public key.  Only the Observer can unwrap it; SQLite and the dashboard command channel contain
ciphertext only.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .util import now_iso


def _db_path(db) -> Path:
    row = db.execute("PRAGMA database_list").fetchone()
    if not row or not row[2]:
        raise RuntimeError("risk radar requires a file-backed database")
    return Path(row[2]).resolve()


def _private_key_path(db) -> Path:
    configured = os.environ.get("RISK_RADAR_PRIVATE_KEY_FILE")
    if configured:
        return Path(configured)
    credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if credentials_dir:
        mounted = Path(credentials_dir) / "risk_wrap_private_key"
        if mounted.exists():
            return mounted
    return _db_path(db).with_name("risk_radar_private.pem")


def _public_key_path(db) -> Path:
    return _db_path(db).with_name("risk_radar_public.pem")


def ensure_instance_keypair(db) -> dict:
    """Create the local wrapping key once.  The private key is chmod 0600 and never returned."""
    private_path = _private_key_path(db)
    public_path = _public_key_path(db)
    if not private_path.exists():
        private_path.parent.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(str(private_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, pem)
        finally:
            os.close(fd)
    key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if not public_path.exists() or public_path.read_bytes() != public_pem:
        tmp = public_path.with_suffix(".tmp")
        tmp.write_bytes(public_pem)
        os.chmod(tmp, 0o644)
        os.replace(tmp, public_path)
    return public_wrap_key(db)


def public_wrap_key(db) -> dict:
    pem = _public_key_path(db).read_bytes()
    public = serialization.load_pem_public_key(pem)
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return {
        "algorithm": "RSA-OAEP-256+A256GCM",
        "envelopeVersion": 1,
        "keyId": hashlib.sha256(der).hexdigest()[:24],
        "spki": base64.b64encode(der).decode("ascii"),
    }


def decrypt_envelope(db, envelope: dict) -> str:
    expected = public_wrap_key(db)
    if int(envelope.get("envelopeVersion", 0)) != 1 or envelope.get("keyId") != expected["keyId"]:
        raise ValueError("credential envelope uses an unknown wrapping key")
    private = serialization.load_pem_private_key(_private_key_path(db).read_bytes(), password=None)
    wrapped = base64.b64decode(envelope["wrappedKey"], validate=True)
    nonce = base64.b64decode(envelope["nonce"], validate=True)
    ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
    dek = private.decrypt(
        wrapped,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    plaintext = AESGCM(dek).decrypt(nonce, ciphertext, None)
    secret = plaintext.decode("utf-8").strip()
    if not secret:
        raise ValueError("credential is empty")
    return secret


class CredentialStore:
    def __init__(self, db):
        self.db = db

    def save_envelope(self, provider: str, envelope: dict, status: str = "valid") -> None:
        now = now_iso()
        self.db.execute(
            "INSERT INTO provider_credential "
            "(provider,envelope_version,key_id,wrapped_key,nonce,ciphertext,status,last_error,created_at,updated_at,last_validated_at) "
            "VALUES (?,?,?,?,?,?,?,NULL,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
            "envelope_version=excluded.envelope_version,key_id=excluded.key_id,wrapped_key=excluded.wrapped_key,"
            "nonce=excluded.nonce,ciphertext=excluded.ciphertext,status=excluded.status,last_error=NULL,"
            "updated_at=excluded.updated_at,last_validated_at=excluded.last_validated_at",
            (provider, int(envelope["envelopeVersion"]), envelope["keyId"], envelope["wrappedKey"],
             envelope["nonce"], envelope["ciphertext"], status, now, now, now),
        )
        self.db.commit()

    def secret(self, provider: str) -> str | None:
        row = self.db.execute(
            "SELECT envelope_version,key_id,wrapped_key,nonce,ciphertext FROM provider_credential WHERE provider=?",
            (provider,),
        ).fetchone()
        if not row:
            return None
        return decrypt_envelope(self.db, {
            "envelopeVersion": row[0], "keyId": row[1], "wrappedKey": row[2],
            "nonce": row[3], "ciphertext": row[4],
        })

    def delete(self, provider: str) -> bool:
        changed = self.db.execute("DELETE FROM provider_credential WHERE provider=?", (provider,)).rowcount
        self.db.commit()
        return bool(changed)

    def mark_error(self, provider: str, error: str) -> None:
        self.db.execute(
            "UPDATE provider_credential SET status='error',last_error=?,updated_at=? WHERE provider=?",
            (str(error)[:500], now_iso(), provider),
        )
        self.db.commit()
