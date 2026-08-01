"""Construction helpers for public and explicitly capability-gated Hyperliquid SDK clients.

The signed boundary accepts a protected Agent-key file plus the expected public Agent address.  It never logs
or returns the key and rejects permissive/symlinked credential files. Production Mainnet construction accepts
only an in-memory signer unwrapped by the activated Dashboard/VPS credential boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Any, Iterable

from .venue import ExecutionNetwork, venue_config


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class SignedSdkClients:
    info: Any
    exchange: Any
    agent_address: str


def _normalize_address(address: str, *, error_code: str) -> str:
    value = str(address or "").lower()
    if len(value) != 42 or not value.startswith("0x"):
        raise CredentialError(error_code)
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise CredentialError(error_code) from exc
    return value


def load_agent_account(private_key_path: str | os.PathLike, *, expected_agent_address: str):
    """Load one local Testnet Agent key without exposing its value in errors or return metadata."""
    path = Path(private_key_path).expanduser()
    try:
        file_stat = path.lstat()
    except OSError:
        raise CredentialError("agent_private_key_file_unavailable") from None
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise CredentialError("agent_private_key_file_not_regular")
    if os.name == "posix" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise CredentialError("agent_private_key_file_permissions_too_open")
    if file_stat.st_size > 256:
        raise CredentialError("agent_private_key_file_too_large")

    try:
        raw_key = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise CredentialError("agent_private_key_file_unreadable") from None
    if len(raw_key) != 66 or not raw_key.startswith("0x"):
        raise CredentialError("invalid_agent_private_key")
    try:
        bytes.fromhex(raw_key[2:])
        from eth_account import Account

        wallet = Account.from_key(raw_key)
    except Exception:  # noqa: BLE001 - secret-derived SDK errors must never cross this boundary
        raise CredentialError("invalid_agent_private_key") from None

    expected = _normalize_address(expected_agent_address, error_code="invalid_expected_agent_address")
    if wallet.address.lower() != expected:
        raise CredentialError("agent_private_key_address_mismatch")
    return wallet


def create_public_info_client(
    network: ExecutionNetwork | str,
    *,
    supported_dexes: Iterable[str] = ("", "xyz"),
    timeout: float = 10.0,
):
    from hyperliquid.info import Info

    venue = venue_config(network)
    requested_dexes = tuple(dict.fromkeys(str(dex or "") for dex in supported_dexes))
    dexes = ("", *(dex for dex in requested_dexes if dex))
    return Info(
        venue.api_url,
        skip_ws=True,
        perp_dexs=list(dexes),
        timeout=float(timeout),
    )


def create_signed_testnet_clients(
    account_address: str,
    expected_agent_address: str,
    private_key_path: str | os.PathLike,
    *,
    supported_dexes: Iterable[str] = ("", "xyz"),
    timeout: float = 10.0,
) -> SignedSdkClients:
    """Construct official Info/Exchange clients for the local Testnet verifier only."""
    account = _normalize_address(account_address, error_code="invalid_account_address")
    agent = _normalize_address(expected_agent_address, error_code="invalid_expected_agent_address")
    wallet = load_agent_account(private_key_path, expected_agent_address=agent)
    return create_signed_clients_from_wallet(
        ExecutionNetwork.TESTNET,
        account,
        agent,
        wallet,
        supported_dexes=supported_dexes,
        timeout=timeout,
    )


def create_signed_clients_from_wallet(
    network: ExecutionNetwork | str,
    account_address: str,
    expected_agent_address: str,
    wallet,
    *,
    supported_dexes: Iterable[str] = ("", "xyz"),
    timeout: float = 10.0,
    allow_mainnet: bool = False,
) -> SignedSdkClients:
    """Construct clients from an already-unwrapped Agent signer.

    Mainnet construction requires an explicit capability from the completed activation flow.  Merely passing
    the Mainnet enum or changing an API URL is insufficient.
    """
    from hyperliquid.exchange import Exchange

    normalized_network = (
        network if isinstance(network, ExecutionNetwork) else ExecutionNetwork(str(network))
    )
    if normalized_network is ExecutionNetwork.MAINNET and not allow_mainnet:
        raise CredentialError("mainnet_signing_not_activated")
    account = _normalize_address(account_address, error_code="invalid_account_address")
    agent = _normalize_address(expected_agent_address, error_code="invalid_expected_agent_address")
    if str(getattr(wallet, "address", "")).lower() != agent:
        raise CredentialError("agent_private_key_address_mismatch")
    venue = venue_config(normalized_network)
    requested_dexes = tuple(dict.fromkeys(str(dex or "") for dex in supported_dexes))
    dexes = ("", *(dex for dex in requested_dexes if dex))
    info = create_public_info_client(
        normalized_network,
        supported_dexes=dexes,
        timeout=timeout,
    )
    exchange = Exchange(
        wallet,
        venue.api_url,
        account_address=account,
        perp_dexs=list(dexes),
        timeout=float(timeout),
    )
    return SignedSdkClients(info=info, exchange=exchange, agent_address=agent)
