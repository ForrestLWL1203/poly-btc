from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import urllib.request
from typing import Any

from .market import MarketWindow

POLYMARKET_CRYPTO_PRICE_API = "https://polymarket.com/api/crypto/crypto-price"


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def crypto_price_api_url(window: MarketWindow) -> str:
    return POLYMARKET_CRYPTO_PRICE_API + "?" + urllib.parse.urlencode({
        "symbol": window.symbol.upper(),
        "eventStartTime": iso_z(window.start_time),
        "variant": "fiveminute",
        "endDate": iso_z(window.end_time),
    })


def parse_crypto_price_response(data: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(data, dict) or data.get("openPrice") is None:
        return None
    try:
        return {
            "openPrice": float(data["openPrice"]),
            "closePrice": float(data["closePrice"]) if data.get("closePrice") is not None else None,
            "completed": bool(data.get("completed")),
            "incomplete": bool(data.get("incomplete")),
            "cached": bool(data.get("cached")),
        }
    except (TypeError, ValueError):
        return None


def fetch_crypto_price_api(window: MarketWindow, *, timeout: float = 10.0) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(
            crypto_price_api_url(window),
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return parse_crypto_price_response(raw)
