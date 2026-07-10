"""Hyperliquid WebSocket protocol helpers (message builders).

Connection/reconnect orchestration lives in observer.py (it owns the stateful
buffers); this module only encodes the wire protocol.
"""
import json

from . import config

WS_URL = config.WS_URL
PING = json.dumps({"method": "ping"})


def sub_msg(subscription: dict) -> str:
    return json.dumps({"method": "subscribe", "subscription": subscription})


def user_fills(addr: str) -> dict:
    return {"type": "userFills", "user": addr}


def bbo(coin: str) -> dict:
    return {"type": "bbo", "coin": coin}
