"""Browser-encrypted Agent credential envelope and Observer-only unwrap boundary.

The Dashboard receives and persists only an RSA-OAEP/AES-GCM envelope.  The RSA private key is supplied to
the execution worker through a protected file (systemd ``LoadCredential`` in production).  Decrypted Agent
key bytes exist only long enough to construct an ``eth_account`` signer and are never included in errors.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from .sdk_clients import CredentialError, _normalize_address


ENVELOPE_VERSION = 1
ENVELOPE_ALGORITHM = "RSA-OAEP-256+A256GCM"
_MAX_ENVELOPE_JSON_BYTES = 8_192
_MAX_CIPHERTEXT_BYTES = 512
_MAX_WRAPPED_KEY_BYTES = 1_024


def credential_aad(network: str, account_address: str, agent_address: str) -> bytes:
    network_value = str(network or "").lower()
    if network_value not in {"testnet", "mainnet"}:
        raise CredentialError("invalid_credential_network")
    account = _normalize_address(account_address, error_code="invalid_account_address")
    agent = _normalize_address(agent_address, error_code="invalid_expected_agent_address")
    return f"poly-btc-hyperliquid-agent-v1\n{network_value}\n{account}\n{agent}".encode()


def _decode_b64(value: Any, *, code: str, maximum: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > maximum * 2:
        raise CredentialError(code)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise CredentialError(code) from None
    if not decoded or len(decoded) > maximum:
        raise CredentialError(code)
    return decoded


def validate_envelope(envelope: Any) -> dict:
    if not isinstance(envelope, dict):
        raise CredentialError("invalid_credential_envelope")
    try:
        encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        raise CredentialError("invalid_credential_envelope") from None
    if len(encoded) > _MAX_ENVELOPE_JSON_BYTES:
        raise CredentialError("credential_envelope_too_large")
    if set(envelope) != {"version", "algorithm", "wrapKeyId", "wrappedKey", "iv", "ciphertext"}:
        raise CredentialError("invalid_credential_envelope_fields")
    if envelope.get("version") != ENVELOPE_VERSION or envelope.get("algorithm") != ENVELOPE_ALGORITHM:
        raise CredentialError("unsupported_credential_envelope")
    wrap_key_id = envelope.get("wrapKeyId")
    if not isinstance(wrap_key_id, str) or len(wrap_key_id) != 64:
        raise CredentialError("invalid_wrap_key_id")
    try:
        int(wrap_key_id, 16)
    except ValueError:
        raise CredentialError("invalid_wrap_key_id") from None
    wrapped = _decode_b64(
        envelope.get("wrappedKey"), code="invalid_wrapped_key", maximum=_MAX_WRAPPED_KEY_BYTES,
    )
    iv = _decode_b64(envelope.get("iv"), code="invalid_credential_iv", maximum=32)
    ciphertext = _decode_b64(
        envelope.get("ciphertext"), code="invalid_credential_ciphertext", maximum=_MAX_CIPHERTEXT_BYTES,
    )
    if len(iv) != 12 or len(ciphertext) < 17 or len(wrapped) < 256:
        raise CredentialError("invalid_credential_envelope_lengths")
    return dict(envelope)


def _protected_regular_file(path_value: str | os.PathLike, *, private: bool) -> Path:
    path = Path(path_value).expanduser()
    try:
        file_stat = path.lstat()
    except OSError:
        raise CredentialError("credential_wrap_key_unavailable") from None
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise CredentialError("credential_wrap_key_not_regular")
    if private and os.name == "posix" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise CredentialError("credential_wrap_key_permissions_too_open")
    if file_stat.st_size <= 0 or file_stat.st_size > 32_768:
        raise CredentialError("credential_wrap_key_invalid_size")
    return path


def _public_key_id(public_key) -> str:
    from cryptography.hazmat.primitives import serialization

    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def load_wrap_public_key(path_value: str | os.PathLike):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    path = _protected_regular_file(path_value, private=False)
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except Exception:  # noqa: BLE001 - key parser details are not an API contract
        raise CredentialError("invalid_credential_wrap_public_key") from None
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise CredentialError("credential_wrap_key_too_weak")
    return key


def public_wrap_key_payload(path_value: str | os.PathLike) -> dict:
    from cryptography.hazmat.primitives import serialization

    key = load_wrap_public_key(path_value)
    pem = key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    der = key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return {
        "version": ENVELOPE_VERSION,
        "algorithm": ENVELOPE_ALGORITHM,
        "wrapKeyId": _public_key_id(key),
        "publicKeyPem": pem,
        "spki": base64.b64encode(der).decode("ascii"),
    }


def decrypt_agent_wallet(
    envelope: Any,
    *,
    network: str,
    account_address: str,
    agent_address: str,
    private_key_path: str | os.PathLike,
):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from eth_account import Account

    normalized = validate_envelope(envelope)
    path = _protected_regular_file(private_key_path, private=True)
    try:
        private_key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except Exception:  # noqa: BLE001
        raise CredentialError("invalid_credential_wrap_private_key") from None
    if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 3072:
        raise CredentialError("credential_wrap_key_too_weak")
    if _public_key_id(private_key.public_key()) != normalized["wrapKeyId"]:
        raise CredentialError("credential_wrap_key_mismatch")

    wrapped = _decode_b64(normalized["wrappedKey"], code="invalid_wrapped_key", maximum=_MAX_WRAPPED_KEY_BYTES)
    iv = _decode_b64(normalized["iv"], code="invalid_credential_iv", maximum=32)
    ciphertext = _decode_b64(
        normalized["ciphertext"], code="invalid_credential_ciphertext", maximum=_MAX_CIPHERTEXT_BYTES,
    )
    try:
        aes_key = private_key.decrypt(
            wrapped,
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        plaintext = bytearray(AESGCM(aes_key).decrypt(
            iv, ciphertext, credential_aad(network, account_address, agent_address),
        ))
        raw_text = bytes(plaintext).decode("ascii").strip()
        # Rabby/MetaMask exports and browser libraries disagree on whether the
        # conventional ``0x`` prefix is present.  Accept both exact shapes,
        # while still rejecting whitespace, JSON, mnemonics and arbitrary
        # strings at this secret boundary.
        if raw_text.startswith("0x"):
            raw_text = raw_text[2:]
        if len(raw_text) != 64:
            raise ValueError("shape")
        raw_key = bytes.fromhex(raw_text)
        if len(raw_key) != 32:
            raise ValueError("length")
        wallet = Account.from_key(raw_key)
    except Exception:  # noqa: BLE001 - never expose cryptographic or secret-derived details
        raise CredentialError("credential_decryption_failed") from None
    finally:
        if "plaintext" in locals():
            for index in range(len(plaintext)):
                plaintext[index] = 0
    expected = _normalize_address(agent_address, error_code="invalid_expected_agent_address")
    if wallet.address.lower() != expected:
        raise CredentialError("agent_private_key_address_mismatch")
    return wallet


def generate_wrap_keypair(
    private_path_value: str | os.PathLike,
    public_path_value: str | os.PathLike,
) -> dict:
    """Create one non-overwriting RSA-3072 keypair for deployment provisioning."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_path = Path(private_path_value)
    public_path = Path(public_path_value)
    if private_path.exists() or public_path.exists():
        raise CredentialError("credential_wrap_key_already_exists")
    private_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    public_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(private_fd, private_pem)
    finally:
        os.close(private_fd)
    public_fd = os.open(public_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(public_fd, public_pem)
    finally:
        os.close(public_fd)
    return {"wrapKeyId": _public_key_id(key.public_key()), "privateMode": "0600"}
