from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import urllib.request
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

DATA_API = "https://data-api.polymarket.com"
LB_API = "https://lb-api.polymarket.com"
USER_PNL_API = "https://user-pnl-api.polymarket.com"


def _get_url_json(
    base_url: str,
    path: str,
    params: dict[str, Any],
    *,
    timeout: float = 10.0,
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> Any:
    url = base_url + path + "?" + urllib.parse.urlencode(params, doseq=True)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req_headers = {"User-Agent": "poly-monitor/0.1", "Accept": "application/json"}
            if headers:
                req_headers.update(headers)
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.25 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _get_json(path: str, params: dict[str, Any], *, timeout: float = 10.0, retries: int = 3) -> Any:
    return _get_url_json(DATA_API, path, params, timeout=timeout, retries=retries)


def normalize_trade(raw: dict[str, Any], *, symbol: str, observed_at: str) -> dict[str, Any]:
    price = float(raw.get("price") or 0.0)
    size = float(raw.get("size") or 0.0)
    return {
        "event": "trade_observed",
        "observed_at": observed_at,
        "exchange_ts": int(raw.get("timestamp") or 0),
        "symbol": symbol.upper(),
        "market_slug": str(raw.get("slug") or raw.get("eventSlug") or ""),
        "condition_id": str(raw.get("conditionId") or ""),
        "wallet": str(raw.get("proxyWallet") or "").lower(),
        "name": str(raw.get("name") or ""),
        "side": str(raw.get("side") or "").upper(),
        "outcome": str(raw.get("outcome") or ""),
        "price": price,
        "size": size,
        "usdc": round(price * size, 6),
        "tx_hash": str(raw.get("transactionHash") or ""),
        "fill_id": str(raw.get("id") or raw.get("fillId") or raw.get("logIndex") or raw.get("transactionIndex") or ""),
    }


def symbol_from_slug(slug: str) -> str:
    prefix = slug.split("-", 1)[0].upper() if slug else ""
    return prefix if prefix in {"BTC", "ETH", "SOL", "XRP"} else ""


def normalize_activity_event(raw: dict[str, Any], *, wallet: str, observed_at: str) -> dict[str, Any]:
    price = float(raw.get("price") or 0.0)
    size = float(raw.get("size") or 0.0)
    usdc = raw.get("usdcSize")
    slug = str(raw.get("slug") or raw.get("eventSlug") or "")
    return {
        "observed_at": observed_at,
        "exchange_ts": int(raw.get("timestamp") or 0),
        "symbol": symbol_from_slug(slug),
        "market_slug": slug,
        "condition_id": str(raw.get("conditionId") or ""),
        "wallet": str(raw.get("proxyWallet") or wallet or "").lower(),
        "activity_type": str(raw.get("type") or "").upper(),
        "side": str(raw.get("side") or "").upper(),
        "outcome": str(raw.get("outcome") or ""),
        "outcome_index": int(raw.get("outcomeIndex") if raw.get("outcomeIndex") is not None else -1),
        "price": price,
        "size": size,
        "usdc": float(usdc) if usdc is not None else round(price * size, 6),
        "asset": str(raw.get("asset") or ""),
        "tx_hash": str(raw.get("transactionHash") or ""),
        "fill_id": str(raw.get("id") or raw.get("fillId") or raw.get("logIndex") or raw.get("transactionIndex") or ""),
        "name": str(raw.get("name") or ""),
        "pseudonym": str(raw.get("pseudonym") or ""),
    }


def fetch_market_trades(condition_id: str, *, limit: int = 100, offset: int = 0, pages: int = 4) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    page_size = min(limit, 100)
    page_count = max(max(1, pages), (max(1, limit) + page_size - 1) // page_size)
    for page in range(page_count):
        data = _get_json("/trades", {"market": condition_id, "limit": page_size, "offset": offset + page * page_size})
        if not isinstance(data, list):
            break
        for row in data:
            if not isinstance(row, dict):
                continue
            key = (
                str(row.get("transactionHash") or ""),
                str(row.get("proxyWallet") or ""),
                str(row.get("outcome") or ""),
                str(row.get("price") or ""),
                str(row.get("size") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        if not data:
            break
    return rows


class AsyncDataApiClient:
    def __init__(self, *, base_url: str = DATA_API, timeout: float = 10.0, retries: int = 3) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.retries = retries
        self._session = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        if aiohttp is None:
            raise RuntimeError("aiohttp package is required for async Data API requests")
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            connector = aiohttp.TCPConnector(
                limit=10,
                limit_per_host=5,
                ttl_dns_cache=300,
                keepalive_timeout=30,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={"User-Agent": "poly-monitor/0.1", "Accept": "application/json"},
            )
        last_error: Exception | None = None
        url = self.base_url + path
        for attempt in range(self.retries):
            try:
                async with self._session.get(url, params=params) as resp:
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    await asyncio.sleep(0.25 * (attempt + 1))
        assert last_error is not None
        raise last_error

    async def fetch_market_trades(self, condition_id: str, *, limit: int = 100, offset: int = 0, pages: int = 4) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        page_size = min(limit, 100)
        page_count = max(max(1, pages), (max(1, limit) + page_size - 1) // page_size)
        for page in range(page_count):
            data = await self._get_json("/trades", {"market": condition_id, "limit": page_size, "offset": offset + page * page_size})
            if not isinstance(data, list):
                break
            for row in data:
                if not isinstance(row, dict):
                    continue
                key = (
                    str(row.get("transactionHash") or ""),
                    str(row.get("proxyWallet") or ""),
                    str(row.get("outcome") or ""),
                    str(row.get("price") or ""),
                    str(row.get("size") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
            if not data:
                break
        return rows

    async def fetch_user_activity(self, wallet: str, *, limit: int = 500, offset: int = 0, start: int | None = None, end: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"user": wallet, "limit": limit, "offset": offset}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        data = await self._get_json("/activity", params)
        return data if isinstance(data, list) else []


def fetch_user_activity(wallet: str, *, limit: int = 500, offset: int = 0, start: int | None = None, end: int | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"user": wallet, "limit": limit, "offset": offset}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    data = _get_json("/activity", params)
    return data if isinstance(data, list) else []


def fetch_closed_positions(wallet: str, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
    data = _get_json("/closed-positions", {"user": wallet, "limit": limit, "offset": offset})
    return data if isinstance(data, list) else []


def fetch_user_positions(wallet: str, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
    data = _get_json("/positions", {"user": wallet, "limit": limit, "offset": offset})
    return data if isinstance(data, list) else []


def fetch_user_profit(wallet: str, *, window: str) -> dict[str, Any] | None:
    data = _get_url_json(LB_API, "/profit", {"address": wallet, "window": window, "limit": 1})
    if isinstance(data, list) and data:
        row = data[0]
        return row if isinstance(row, dict) else None
    return None


def fetch_user_pnl_history(wallet: str, *, interval: str, fidelity: str) -> list[dict[str, Any]]:
    data = _get_url_json(
        USER_PNL_API,
        "/user-pnl",
        {"user_address": wallet, "interval": interval, "fidelity": fidelity},
        headers={
            "User-Agent": "Mozilla/5.0 poly-monitor/0.1",
            "Origin": "https://polymarket.com",
            "Referer": "https://polymarket.com/",
        },
    )
    return data if isinstance(data, list) else []
