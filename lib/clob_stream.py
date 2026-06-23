from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from bisect import bisect_right
from collections import Counter, deque
from typing import Any

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

CLOB_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class ClobBookStream:
    """CLOB market WS with a rolling top-of-book history per token.

    The history is the redesign's core addition: data-api reports fills several
    seconds late, so to test "spot moved, book had not followed yet AT the fill"
    we must reconstruct the book as of the fill's exchange_ts, not the book we
    happen to see when the (delayed) fill surfaces. History is keyed by wall
    clock (time.time()) so it aligns with trade exchange_ts (epoch seconds).
    """

    def __init__(
        self,
        *,
        idle_reconnect_sec: float = 20.0,
        max_trade_events: int = 1000,
        top_history_sec: float = 90.0,
    ) -> None:
        self.idle_reconnect_sec = idle_reconnect_sec
        self.max_trade_events = max(1, int(max_trade_events))
        self.top_history_sec = top_history_sec
        self._tokens: list[str] = []
        self._books: dict[str, dict[str, Any]] = {}
        self._top_history: dict[str, deque[tuple[float, float | None, float | None, float | None, float | None]]] = {}
        self._running = False
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._last_message_at = 0.0
        self._event_counts: Counter[str] = Counter()
        self._trade_events: deque[dict[str, Any]] = deque(maxlen=self.max_trade_events)

    async def connect(self, token_ids: list[str]) -> None:
        if websockets is None:
            raise RuntimeError("websockets package is required for CLOB market stream")
        self._tokens = list(token_ids)
        self._running = True
        await self._connect_once()
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def switch_tokens(self, token_ids: list[str]) -> None:
        self._tokens = list(token_ids)
        self._books.clear()
        if self._ws is not None:
            await self._reconnect()

    async def close(self) -> None:
        self._running = False
        if self._recv_task is not None:
            self._recv_task.cancel()
            await asyncio.gather(self._recv_task, return_exceptions=True)
            self._recv_task = None
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=3.0)
            except Exception:
                pass
            self._ws = None

    def get_book(self, token_id: str, *, max_age_sec: float | None = None) -> tuple[list[tuple[float, float]], list[tuple[float, float]], int | None]:
        book = self._books.get(token_id)
        if book is None:
            return [], [], None
        age_sec = time.monotonic() - float(book["received_at"])
        if max_age_sec is not None and age_sec > max_age_sec:
            return [], [], round(age_sec * 1000)
        bids = sorted(book["bids"].items(), key=lambda pair: pair[0], reverse=True)
        asks = sorted(book["asks"].items(), key=lambda pair: pair[0])
        return bids, asks, round(age_sec * 1000)

    def top_at_or_before(
        self, token_id: str, ts: float, *, max_backward_sec: float = 30.0
    ) -> dict[str, Any] | None:
        """Reconstruct the top-of-book for ``token_id`` as of wall-clock ``ts``.

        Returns ``{"bid","ask","bid_size","ask_size","age_sec"}`` where age_sec is
        how stale the chosen snapshot was relative to ``ts``. ``None`` when no
        snapshot at-or-before ``ts`` exists within ``max_backward_sec``.
        """
        history = self._top_history.get(token_id)
        if not history:
            return None
        stamps = [row[0] for row in history]
        idx = bisect_right(stamps, ts) - 1
        if idx < 0:
            return None
        snap_ts, bid, ask, bid_size, ask_size = history[idx]
        age = ts - snap_ts
        if age > max_backward_sec:
            return None
        return {
            "bid": bid,
            "ask": ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "age_sec": round(age, 3),
        }

    def diagnostics(self, *, reset_counts: bool = False) -> dict[str, Any]:
        age_ms = None if self._last_message_at <= 0 else round((time.monotonic() - self._last_message_at) * 1000)
        row = {
            "last_message_age_ms": age_ms,
            "subscribed_tokens": len(self._tokens),
            "event_counts": dict(self._event_counts),
            "trade_events_queued": len(self._trade_events),
            "top_history_tokens": {tok: len(hist) for tok, hist in self._top_history.items()},
        }
        if reset_counts:
            self._event_counts.clear()
        return row

    def pop_trade_events(self) -> list[dict[str, Any]]:
        rows = list(self._trade_events)
        self._trade_events.clear()
        return rows

    async def _connect_once(self) -> None:
        self._ws = await websockets.connect(CLOB_MARKET_WS_URL, ping_interval=10, ping_timeout=15)
        await self._subscribe()
        self._last_message_at = time.monotonic()

    async def _subscribe(self) -> None:
        await self._ws.send(json.dumps({
            "type": "market",
            "assets_ids": self._tokens,
            "operation": "subscribe",
            "custom_feature_enabled": True,
        }))

    async def _reconnect(self) -> None:
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=3.0)
            except Exception:
                pass
            self._ws = None
        self._books.clear()
        await self._connect_once()

    async def _recv_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                if self._ws is None:
                    await self._reconnect()
                raw = await asyncio.wait_for(self._ws.recv(), timeout=self.idle_reconnect_sec)
                self._last_message_at = time.monotonic()
                self._dispatch(raw)
                backoff = 1.0
            except asyncio.TimeoutError:
                await self._reconnect()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._running:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 30.0)
                    self._ws = None

    def _dispatch(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        events = data if isinstance(data, list) else [data]
        for event in events:
            if isinstance(event, dict):
                self._handle_event(event)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("event_type") or "")
        self._event_counts[event_type or "missing"] += 1
        if event_type == "book":
            self._handle_book(event)
        elif event_type == "price_change":
            self._handle_price_change(event)
        elif event_type == "best_bid_ask":
            self._handle_best_bid_ask(event)
        elif event_type == "last_trade_price":
            self._handle_last_trade(event)

    @staticmethod
    def _parse_side(levels: list[dict[str, Any]], *, reverse: bool) -> dict[float, float]:
        parsed: list[tuple[float, float]] = []
        for item in levels:
            try:
                price = float(item.get("price"))
                size = float(item.get("size", 0))
            except (AttributeError, TypeError, ValueError):
                continue
            if price > 0 and size > 0:
                parsed.append((price, size))
        if len(parsed) > 1 and not _is_sorted_prices(parsed, reverse=reverse):
            parsed.sort(key=lambda pair: pair[0], reverse=reverse)
        return dict(parsed)

    def _handle_best_bid_ask(self, event: dict[str, Any]) -> None:
        """Record top-of-book from the dedicated ``best_bid_ask`` event.

        This is the clean source: the CLOB WS emits ``best_bid_ask`` only when the
        best bid/ask PRICE moves — far lower rate than the ``price_change`` firehose
        (which fires on every size flicker at any level). Sizes are read from the
        book maintained by book/price_change. History dedups on (bid, ask) price.
        """
        token = str(event.get("asset_id") or "")
        if not token:
            return
        try:
            best_bid = float(event["best_bid"]) if event.get("best_bid") else None
            best_ask = float(event["best_ask"]) if event.get("best_ask") else None
        except (TypeError, ValueError):
            return
        book = self._books.get(token)
        bid_size = book["bids"].get(best_bid) if (book and best_bid is not None) else None
        ask_size = book["asks"].get(best_ask) if (book and best_ask is not None) else None
        hist = self._top_history.setdefault(token, deque())
        if hist:
            _, p_bid, p_ask, _, _ = hist[-1]
            if (p_bid, p_ask) == (best_bid, best_ask):   # price unchanged -> skip size flicker
                return
        now = time.time()
        hist.append((now, best_bid, best_ask, bid_size, ask_size))
        cutoff = now - self.top_history_sec
        while hist and hist[0][0] < cutoff:
            hist.popleft()

    def _handle_book(self, event: dict[str, Any]) -> None:
        token = str(event.get("asset_id") or "")
        if not token:
            return
        self._books[token] = {
            "bids": self._parse_side(event.get("bids", []), reverse=True),
            "asks": self._parse_side(event.get("asks", []), reverse=False),
            "received_at": time.monotonic(),
        }
        # top-of-book HISTORY is fed by best_bid_ask (clean); book/price_change only
        # maintain depth here.

    def _handle_price_change(self, event: dict[str, Any]) -> None:
        changes = event.get("price_changes") or ([event] if event.get("price") else [])
        touched: set[str] = set()
        for change in changes:
            token = str(change.get("asset_id") or "")
            book = self._books.get(token)
            if not book:
                continue
            try:
                price = float(change.get("price"))
                size = float(change.get("size"))
            except (TypeError, ValueError):
                continue
            side_key = "bids" if change.get("side") == "BUY" else "asks" if change.get("side") == "SELL" else None
            if side_key is None:
                continue
            if size > 0:
                book[side_key][price] = size
            else:
                book[side_key].pop(price, None)
            book["received_at"] = time.monotonic()
            touched.add(token)

    def _handle_last_trade(self, event: dict[str, Any]) -> None:
        token = str(event.get("asset_id") or "")
        if not token:
            return
        try:
            price = float(event.get("price"))
        except (TypeError, ValueError):
            return
        size = _optional_float(event.get("size"))
        ts = _exchange_ts_seconds(event.get("timestamp"))
        usdc = price * size if size is not None else None
        tx_hash = event.get("hash") or event.get("transaction_hash") or event.get("tx_hash")
        fill_id = event.get("fill_id") or ":".join(
            str(item)
            for item in (tx_hash or "ws", token, ts, price, size if size is not None else "")
        )
        self._trade_events.append(
            {
                "event": "ws_trade_observed",
                "source": "clob_ws",
                "asset_id": token,
                "market": event.get("market"),
                "exchange_ts": ts,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "side": event.get("side"),
                "price": price,
                "size": size,
                "usdc": round(usdc, 6) if usdc is not None else None,
                "tx_hash": tx_hash,
                "fill_id": fill_id,
            }
        )


def _is_sorted_prices(levels: list[tuple[float, float]], *, reverse: bool) -> bool:
    if len(levels) < 2:
        return True
    prices = [price for price, _size in levels]
    if reverse:
        return all(left >= right for left, right in zip(prices, prices[1:]))
    return all(left <= right for left, right in zip(prices, prices[1:]))


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _exchange_ts_seconds(value: Any) -> int:
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return int(time.time())
    if raw > 10_000_000_000:
        raw /= 1000.0
    return int(raw)
