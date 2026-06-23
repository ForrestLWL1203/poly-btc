#!/usr/bin/env python3
"""Live market-context RECORDER for BTC 5min windows.

Records the EPHEMERAL data that can never be recovered later — the order book,
BTC ticks, open price, settlement — for every window, going forward. A target
wallet's own trades are NOT captured here: they are perfectly recoverable from
Polymarket's trade/activity API anytime, and aligned to this recorded context
post-hoc by timestamp. So this script depends only on the two reliable Polymarket
WS feeds (book + Chainlink BTC); no Alchemy RPC, no flaky on-chain subscription.

Everything is SQLite (data/watch.db). Read-only against the market.

  python3 watch.py --db data/watch.db
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import signal
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

from lib.market import MarketSeries, MarketWindow, find_current_or_next_window
from lib.price_feed import ChainlinkPriceFeed
from lib.clob_stream import ClobBookStream

UTC = dt.timezone.utc
GAMMA = "https://gamma-api.polymarket.com/markets"

REFRESH_WINDOWS_SEC = 15.0
DRAIN_SEC = 1.0              # flush in-memory book/BTC history to db this often
SETTLE_CHECK_SEC = 30.0
SETTLE_GRACE_SEC = 90.0      # gamma resolves ~90-120s after a window ends
DROP_TIMEOUT_SEC = 1800.0    # give up settling a window this long past end
SPOT_HISTORY_SEC = 180.0     # keep enough feed history to not drop ticks between drains
BOOK_HISTORY_SEC = 180.0

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS windows (
    slug         TEXT PRIMARY KEY,
    condition_id TEXT,
    up_token     TEXT,
    down_token   TEXT,
    start_epoch  INTEGER,
    end_epoch    INTEGER,
    open_price   REAL,           -- BTC spot at start_epoch (our Chainlink feed)
    winning_side TEXT,           -- 'Up'|'Down' from gamma authoritative resolution
    settled      INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT
);
-- top-of-book snapshot per token, one row per change (the irreplaceable data)
CREATE TABLE IF NOT EXISTS book (
    slug      TEXT,
    side      TEXT,              -- 'Up'|'Down'
    ts        REAL,              -- wall clock (epoch sec) of the snapshot
    bid       REAL,
    ask       REAL,
    bid_size  REAL,
    ask_size  REAL
);
CREATE INDEX IF NOT EXISTS idx_book_slug_ts ON book(slug, ts);
-- BTC Chainlink ticks (global; align trades by ts)
CREATE TABLE IF NOT EXISTS btc (
    ts    REAL PRIMARY KEY,
    price REAL
);
"""


def now_iso() -> str:
    return dt.datetime.now(UTC).isoformat()


def gamma_settled(slug: str):
    url = GAMMA + "?" + urllib.parse.urlencode({"slug": slug, "closed": "true"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "poly-btc-watch/0.1"}), timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return None
    if isinstance(data, list):
        for m in data:
            if isinstance(m, dict) and m.get("slug") == slug:
                return m
    return None


def winning_side(outcome_prices) -> str | None:
    try:
        p = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
        a, b = float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError):
        return None
    if a == b:
        return None
    return "Up" if a > b else "Down"


class WindowState:
    __slots__ = ("window", "open_written", "settled")

    def __init__(self, window: MarketWindow) -> None:
        self.window = window
        self.open_written = False
        self.settled = False


class Recorder:
    def __init__(self, db_path: str, symbol: str = "BTC", seconds: float | None = None) -> None:
        self.series = MarketSeries.from_symbol(symbol)
        self.symbol = symbol.upper()
        self.seconds = seconds
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)
        self.feed = ChainlinkPriceFeed(self.symbol.lower(), max_history_sec=SPOT_HISTORY_SEC)
        self.stream = ClobBookStream(top_history_sec=BOOK_HISTORY_SEC)
        self.active: dict[str, WindowState] = {}
        self._sub: tuple[str, ...] = ()
        self._stop = asyncio.Event()
        self._last_btc_ts = 0.0
        self._last_book_ts: dict[str, float] = {}     # per token
        self._last_book_px: dict[str, tuple] = {}     # per token, last (bid,ask) recorded
        self._t_windows = self._t_drain = self._t_settle = 0.0

    # ---- windows ----------------------------------------------------------
    async def refresh_windows(self) -> None:
        w = await asyncio.to_thread(find_current_or_next_window, self.series)
        if w is not None and w.slug not in self.active:
            self.active[w.slug] = WindowState(w)
            self.db.execute(
                """INSERT OR IGNORE INTO windows
                   (slug, condition_id, up_token, down_token, start_epoch, end_epoch, settled, first_seen)
                   VALUES (?,?,?,?,?,?,0,?)""",
                (w.slug, w.condition_id, w.up_token, w.down_token, w.start_epoch, w.end_epoch, now_iso()),
            )
            self.db.commit()
            print(f"[{now_iso()}] +window {w.slug} ({len(self.active)} active)")
        await self._sync_sub()

    async def _sync_sub(self) -> None:
        toks: list[str] = []
        for st in self.active.values():
            toks.extend((st.window.up_token, st.window.down_token))
        key = tuple(sorted(set(toks)))
        if key == self._sub:
            return
        self._sub = key
        if not self.stream._tokens:
            await self.stream.connect(list(key))
        else:
            await self.stream.switch_tokens(list(key))

    # ---- drain ephemeral history into the db ------------------------------
    def _token_side(self, token: str) -> tuple[str, str] | None:
        for st in self.active.values():
            if token == st.window.up_token:
                return st.window.slug, "Up"
            if token == st.window.down_token:
                return st.window.slug, "Down"
        return None

    def drain(self) -> None:
        dirty = False
        # BTC ticks
        for ts, price in list(self.feed._history):
            if ts > self._last_btc_ts:
                self.db.execute("INSERT OR IGNORE INTO btc VALUES (?,?)", (round(ts, 3), round(price, 4)))
                self._last_btc_ts = ts
                dirty = True
        # book top-of-book snapshots, per subscribed token
        for token, hist in list(self.stream._top_history.items()):
            side = self._token_side(token)
            if side is None:
                continue
            slug, s = side
            last = self._last_book_ts.get(token, 0.0)
            last_px = self._last_book_px.get(token)
            for row in list(hist):
                ts, bid, ask, bsz, asz = row
                if ts <= last:
                    continue
                last = ts
                if (bid, ask) == last_px:      # size-only flicker -> skip, keep it sparse
                    continue
                last_px = (bid, ask)
                self.db.execute("INSERT INTO book VALUES (?,?,?,?,?,?,?)",
                                (slug, s, round(ts, 3), bid, ask, bsz, asz))
                dirty = True
            self._last_book_ts[token] = last
            self._last_book_px[token] = last_px
        if dirty:
            self.db.commit()

    def snapshot_opens(self) -> None:
        now = time.time()
        for st in self.active.values():
            if st.open_written or now < st.window.start_epoch:
                continue
            p = self.feed.price_at_or_before(st.window.start_epoch, max_backward_sec=90.0)
            if p:
                st.open_written = True
                self.db.execute("UPDATE windows SET open_price=? WHERE slug=?", (round(p, 2), st.window.slug))
                self.db.commit()

    # ---- settlement via gamma authoritative resolution --------------------
    async def settle(self) -> None:
        now = time.time()
        for slug in list(self.active):
            st = self.active[slug]
            if st.settled or now < st.window.end_epoch + SETTLE_GRACE_SEC:
                if now > st.window.end_epoch + DROP_TIMEOUT_SEC:
                    del self.active[slug]
                continue
            m = await asyncio.to_thread(gamma_settled, slug)
            ws = winning_side(m.get("outcomePrices")) if m else None
            if ws is None:
                if now > st.window.end_epoch + DROP_TIMEOUT_SEC:
                    del self.active[slug]
                    print(f"[{now_iso()}] drop {slug} (unresolved)")
                continue
            st.settled = True
            self.db.execute("UPDATE windows SET winning_side=?, settled=1 WHERE slug=?", (ws, slug))
            self.db.commit()
            del self.active[slug]
            nb = self.db.execute("SELECT COUNT(*) FROM book WHERE slug=?", (slug,)).fetchone()[0]
            print(f"[{now_iso()}] settled {slug} {ws}  (book snapshots={nb})")

    # ---- main loop --------------------------------------------------------
    async def run(self) -> int:
        await self.feed.start()
        await self.refresh_windows()
        print(f"[{now_iso()}] recorder up; book+BTC warming...")
        started = time.monotonic()
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if self.seconds is not None and now - started >= self.seconds:
                    break
                if now - self._t_windows >= REFRESH_WINDOWS_SEC:
                    self._t_windows = now
                    await self.refresh_windows()
                if now - self._t_drain >= DRAIN_SEC:
                    self._t_drain = now
                    self.drain()
                    self.snapshot_opens()
                if now - self._t_settle >= SETTLE_CHECK_SEC:
                    self._t_settle = now
                    await self.settle()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=DRAIN_SEC)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.drain()
            await self.stream.close()
            await self.feed.stop()
            self.db.commit()
            self.db.close()
        return 0

    def stop(self) -> None:
        self._stop.set()


def main() -> int:
    ap = argparse.ArgumentParser(description="BTC 5min live context recorder")
    ap.add_argument("--db", default="data/watch.db")
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--seconds", type=float, default=None, help="auto-stop after N seconds")
    args = ap.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    rec = Recorder(args.db, args.symbol, seconds=args.seconds)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, rec.stop)
        except NotImplementedError:
            pass
    try:
        return loop.run_until_complete(rec.run())
    finally:
        loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
