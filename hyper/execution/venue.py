"""Execution-mode and Hyperliquid venue definitions.

Signal collection remains Mainnet-only elsewhere in the product.  This module owns only the venue selected
for order execution.  Signed Mainnet actions are deliberately disabled during the Testnet-first build phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExecutionMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class ExecutionNetwork(str, Enum):
    TESTNET = "testnet"
    MAINNET = "mainnet"


@dataclass(frozen=True)
class VenueConfig:
    network: ExecutionNetwork
    api_url: str
    ws_url: str


_VENUES = {
    ExecutionNetwork.TESTNET: VenueConfig(
        network=ExecutionNetwork.TESTNET,
        api_url="https://api.hyperliquid-testnet.xyz",
        ws_url="wss://api.hyperliquid-testnet.xyz/ws",
    ),
    ExecutionNetwork.MAINNET: VenueConfig(
        network=ExecutionNetwork.MAINNET,
        api_url="https://api.hyperliquid.xyz",
        ws_url="wss://api.hyperliquid.xyz/ws",
    ),
}


def venue_config(network: ExecutionNetwork | str) -> VenueConfig:
    try:
        normalized = network if isinstance(network, ExecutionNetwork) else ExecutionNetwork(str(network))
    except ValueError as exc:
        raise ValueError(f"unsupported_execution_network:{network}") from exc
    return _VENUES[normalized]
