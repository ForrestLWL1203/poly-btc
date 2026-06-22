from __future__ import annotations

import asyncio
import json
import time
from bisect import bisect_left, bisect_right
from collections import deque
from typing import Any, Iterable

try:
    import websockets
except ImportError:  # pragma: no cover - exercised only on minimal installs.
    websockets = None

LIVE_DATA_WS_URL = "wss://ws-live-data.polymarket.com"


def _normalize_symbol(symbol: str) -> str:
    return str(symbol).lower()


def subscribe_message(symbol: str | Iterable[str]) -> dict[str, Any]:
    symbols = [symbol] if isinstance(symbol, str) else list(symbol)
    return {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_chainlink",
                "type": "update",
                # Live-data WS now keys chainlink crypto prices as "<sym>/usd"
                # (e.g. "btc/usd"); the bare "btc" form is silently ignored.
                "filters": json.dumps({"symbol": f"{_normalize_symbol(item)}/usd"}, separators=(",", ":")),
            }
            for item in symbols
        ],
    }


def _parse_tick(raw: dict[str, Any]) -> tuple[float, float] | None:
    try:
        timestamp_ms = float(raw["timestamp"])
        value = float(raw["value"])
    except (KeyError, TypeError, ValueError):
        return None
    if timestamp_ms <= 0 or value <= 0:
        return None
    return timestamp_ms / 1000.0, value


def price_ticks_from_message(data: dict[str, Any]) -> list[tuple[float, float]]:
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return []
    batch = payload.get("data")
    ticks: list[tuple[float, float]] = []
    if isinstance(batch, list):
        for item in batch:
            if isinstance(item, dict):
                tick = _parse_tick(item)
                if tick is not None:
                    ticks.append(tick)
    else:
        tick = _parse_tick(payload)
        if tick is not None:
            ticks.append(tick)
    return ticks


def _symbol_from_tick(raw: dict[str, Any], fallback: str | None = None) -> str | None:
    symbol = raw.get("symbol") or raw.get("asset") or raw.get("pair") or fallback
    return _normalize_symbol(str(symbol)) if symbol else None


def price_ticks_by_symbol_from_message(data: dict[str, Any]) -> dict[str, list[tuple[float, float]]]:
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return {}
    fallback = _symbol_from_tick(payload)
    batch = payload.get("data")
    routed: dict[str, list[tuple[float, float]]] = {}
    if isinstance(batch, list):
        for item in batch:
            if not isinstance(item, dict):
                continue
            symbol = _symbol_from_tick(item, fallback)
            tick = _parse_tick(item)
            if symbol is not None and tick is not None:
                routed.setdefault(symbol, []).append(tick)
    else:
        tick = _parse_tick(payload)
        if fallback is not None and tick is not None:
            routed.setdefault(fallback, []).append(tick)
    return routed


class ChainlinkPriceFeed:
    def __init__(self, symbol: str, *, max_history_sec: float = 30.0, stale_reconnect_sec: float = 8.0) -> None:
        self.symbol = symbol.lower()
        self.max_history_sec = max_history_sec
        self.stale_reconnect_sec = max(1.0, stale_reconnect_sec)
        self._history: deque[tuple[float, float]] = deque()
        self._running = False
        self._task: asyncio.Task | None = None
        self._ws = None

    @property
    def latest_price(self) -> float | None:
        return self._history[-1][1] if self._history else None

    def latest_age_sec(self) -> float | None:
        if not self._history:
            return None
        return max(0.0, time.time() - self._history[-1][0])

    def price_at_or_before(self, ts: float, max_backward_sec: float | None = None) -> float | None:
        if not self._history:
            return None
        timestamps = [item[0] for item in self._history]
        idx = bisect_right(timestamps, ts) - 1
        if idx < 0:
            return None
        found_ts, price = self._history[idx]
        if max_backward_sec is not None and ts - found_ts > max_backward_sec:
            return None
        return price

    def return_bps(self, lookback_sec: float) -> float | None:
        latest = self.latest_price
        if latest is None or latest <= 0:
            return None
        previous = self.price_at_or_before(time.time() - lookback_sec, max_backward_sec=lookback_sec + 2.0)
        if previous is None or previous <= 0:
            return None
        return round(((latest - previous) / previous) * 10_000.0, 3)

    async def start(self) -> None:
        if websockets is None:
            raise RuntimeError("websockets package is required for live price feeds")
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._recv_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=3.0)
            except Exception:
                pass
            self._ws = None

    def inject_tick(self, ts: float, price: float) -> None:
        if not self._history or ts > self._history[-1][0]:
            self._history.append((ts, price))
        elif ts == self._history[-1][0]:
            self._history[-1] = (ts, price)
        else:
            timestamps = [item[0] for item in self._history]
            idx = bisect_left(timestamps, ts)
            self._history.insert(idx, (ts, price))
        self._prune(time.time())

    def _prune(self, now: float) -> None:
        cutoff = now - self.max_history_sec
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    async def _recv_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                self._ws = await websockets.connect(LIVE_DATA_WS_URL, ping_interval=20, ping_timeout=20)
                await self._ws.send(json.dumps(subscribe_message(self.symbol), separators=(",", ":")))
                backoff = 1.0
                last_tick = time.monotonic()
                while self._running:
                    timeout = max(0.1, self.stale_reconnect_sec - (time.monotonic() - last_tick))
                    try:
                        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        break
                    try:
                        message = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    ticks = price_ticks_from_message(message)
                    for ts, price in ticks:
                        self.inject_tick(ts, price)
                    if ticks:
                        last_tick = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                if self._ws is not None:
                    try:
                        await asyncio.wait_for(self._ws.close(), timeout=3.0)
                    except Exception:
                        pass
                    self._ws = None
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)


class ChainlinkPriceHub:
    def __init__(self, symbols: Iterable[str], *, max_history_sec: float = 30.0, stale_reconnect_sec: float = 8.0) -> None:
        self.symbols = [_normalize_symbol(symbol) for symbol in symbols]
        self.stale_reconnect_sec = max(1.0, stale_reconnect_sec)
        self._feeds = {
            symbol: ChainlinkPriceFeed(symbol, max_history_sec=max_history_sec, stale_reconnect_sec=stale_reconnect_sec)
            for symbol in self.symbols
        }
        self._running = False
        self._task: asyncio.Task | None = None
        self._ws = None

    def feed(self, symbol: str) -> ChainlinkPriceFeed:
        return self._feeds[_normalize_symbol(symbol)]

    async def start(self) -> None:
        if websockets is None:
            raise RuntimeError("websockets package is required for live price feeds")
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._recv_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=3.0)
            except Exception:
                pass
            self._ws = None

    def _handle_message(self, message: dict[str, Any]) -> bool:
        routed = price_ticks_by_symbol_from_message(message)
        updated = False
        for symbol, ticks in routed.items():
            feed = self._feeds.get(symbol)
            if feed is None:
                continue
            for ts, price in ticks:
                feed.inject_tick(ts, price)
                updated = True
        return updated

    async def _recv_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                self._ws = await websockets.connect(LIVE_DATA_WS_URL, ping_interval=20, ping_timeout=20)
                await self._ws.send(json.dumps(subscribe_message(self.symbols), separators=(",", ":")))
                backoff = 1.0
                last_tick = time.monotonic()
                while self._running:
                    timeout = max(0.1, self.stale_reconnect_sec - (time.monotonic() - last_tick))
                    try:
                        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        break
                    try:
                        message = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if self._handle_message(message):
                        last_tick = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                if self._ws is not None:
                    try:
                        await asyncio.wait_for(self._ws.close(), timeout=3.0)
                    except Exception:
                        pass
                    self._ws = None
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
