#!/usr/bin/env python3
"""BTC 5min wallet collector — pair-arb centric.

One loop. For each active 5min window we poll the public market trade feed (the
only unbiased, all-wallets source), buffer every fill in memory with its book
context (top-of-book as of the fill's exchange_ts), and at settlement:

  * compute per-(window, wallet) aggregates for EVERY wallet -> wallet_window,
    the unbiased discovery substrate. The pair-arb signal lives here:
    pair_cost = mean(up_vwap + down_vwap); pair_cost < $1 => risk-free edge.
  * persist per-fill detail only for two-sided wallets (the targets).

Settlement (winning side) is computed ourselves from crypto-price open/close.
We never touch any per-wallet positions endpoint (it only returns winners).
Read-only against the market. No keys, no orders.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import datetime as dt
import signal
import sqlite3
import time
from pathlib import Path

from lib.market import MarketSeries, MarketWindow, find_current_or_next_window
from lib.price_feed import ChainlinkPriceFeed
from lib.clob_stream import ClobBookStream
from lib.data_api import fetch_market_trades, normalize_trade

UTC = dt.timezone.utc

REFRESH_WINDOWS_SEC = 15.0
POLL_TRADES_SEC = 2.0
SETTLE_CHECK_SEC = 20.0
TRADE_STRAGGLER_SEC = 60.0
SETTLE_GRACE_SEC = 75.0      # settle this long past end (after straggler polling completes)
DROP_TIMEOUT_SEC = 300.0     # give up on a window unsettled this long past end (avoid pile-up)
RET_LOOKBACKS = (1, 3, 5, 10)
TRADE_PAGE_SIZE = 100
TRADE_MAX_PAGES = 25
SPOT_HISTORY_SEC = 120.0     # feed history; must still hold the window boundaries at settle time
EPS = 1e-6


def now_utc_iso() -> str:
    return dt.datetime.now(UTC).isoformat()


def _spot_and_age(feed: ChainlinkPriceFeed, ts: float) -> tuple[float | None, float | None]:
    hist = feed._history
    if not hist:
        return None, None
    stamps = [row[0] for row in hist]
    idx = bisect.bisect_right(stamps, ts) - 1
    if idx < 0:
        return None, None
    tick_ts, price = hist[idx]
    return price, round(ts - tick_ts, 3)


def _ret_bps(feed: ChainlinkPriceFeed, ts: float, lookback: int) -> float | None:
    later = feed.price_at_or_before(ts, max_backward_sec=4.0)
    earlier = feed.price_at_or_before(ts - lookback, max_backward_sec=lookback + 4.0)
    if not later or not earlier or earlier <= 0:
        return None
    return round((later - earlier) / earlier * 10_000.0, 3)


class WindowState:
    __slots__ = ("window", "open_price", "close_price", "seen", "fills", "settled")

    def __init__(self, window: MarketWindow) -> None:
        self.window = window
        self.open_price: float | None = None    # BTC spot at start_epoch (from our feed)
        self.close_price: float | None = None   # BTC spot at end_epoch
        self.seen: set[str] = set()
        self.fills: list[dict] = []      # buffered until settlement
        self.settled = False


class Collector:
    def __init__(self, db_path: str, symbol: str = "BTC", seconds: float | None = None) -> None:
        self.series = MarketSeries.from_symbol(symbol)
        self.symbol = symbol.upper()
        self.seconds = seconds
        self.db = sqlite3.connect(db_path)
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text())
        self.feed = ChainlinkPriceFeed(self.symbol.lower(), max_history_sec=SPOT_HISTORY_SEC)
        self.stream = ClobBookStream(top_history_sec=90.0)
        self.active: dict[str, WindowState] = {}
        self._sub_tokens: tuple[str, ...] = ()
        self._stop = asyncio.Event()
        self._t_windows = 0.0
        self._t_trades = 0.0
        self._t_settle = 0.0

    # ---- window lifecycle -------------------------------------------------
    async def refresh_windows(self) -> None:
        window = await asyncio.to_thread(find_current_or_next_window, self.series)
        if window is not None and window.slug not in self.active:
            self.active[window.slug] = WindowState(window)
            self._upsert_window(window)
            print(f"[{now_utc_iso()}] +window {window.slug} ({len(self.active)} active)")
        await self._sync_subscription()

    async def _sync_subscription(self) -> None:
        tokens: list[str] = []
        for state in self.active.values():
            tokens.extend((state.window.up_token, state.window.down_token))
        key = tuple(sorted(set(tokens)))
        if key == self._sub_tokens:
            return
        self._sub_tokens = key
        if not self.stream._tokens:
            await self.stream.connect(list(key))
        else:
            await self.stream.switch_tokens(list(key))

    # ---- trade polling (buffer only) -------------------------------------
    async def poll_trades(self) -> None:
        now = time.time()
        for state in list(self.active.values()):
            if state.settled or now > state.window.end_epoch + TRADE_STRAGGLER_SEC:
                continue
            await self._poll_window_trades(state)

    async def _poll_window_trades(self, state: WindowState) -> None:
        window = state.window
        for page in range(TRADE_MAX_PAGES):
            batch = await asyncio.to_thread(
                fetch_market_trades, window.condition_id,
                limit=TRADE_PAGE_SIZE, offset=page * TRADE_PAGE_SIZE, pages=1,
                taker_only=False,   # full fills (maker+taker); classify roles later
            )
            if not batch:
                break
            # Update `seen` inline: new trades arriving between paginated HTTP
            # calls shift offsets, so the same fill can surface on two pages in
            # one poll. Deferring the seen-update would double-count it.
            fresh = 0
            for raw in batch:
                key = self._fill_key(raw)
                if key in state.seen:
                    continue
                state.seen.add(key)
                state.fills.append(self._build_fill(state, raw))
                fresh += 1
            if fresh < len(batch):  # hit already-seen => older pages seen too
                break

    @staticmethod
    def _fill_key(raw: dict) -> str:
        return "|".join((
            str(raw.get("transactionHash") or ""),
            str(raw.get("proxyWallet") or "").lower(),
            str(raw.get("outcome") or ""),
            str(raw.get("price") or ""),
            str(raw.get("size") or ""),
        ))

    def _build_fill(self, state: WindowState, raw: dict) -> dict:
        window = state.window
        t = normalize_trade(raw, symbol=self.symbol, observed_at=now_utc_iso())
        ets = t["exchange_ts"] or 0
        outcome = t["outcome"]
        token = window.up_token if outcome.lower() == "up" else window.down_token
        spot, spot_age = _spot_and_age(self.feed, ets) if ets else (None, None)
        rets = {lb: (_ret_bps(self.feed, ets, lb) if ets else None) for lb in RET_LOOKBACKS}
        up_top = self.stream.top_at_or_before(window.up_token, ets) if ets else None
        down_top = self.stream.top_at_or_before(window.down_token, ets) if ets else None
        ages = [s["age_sec"] for s in (up_top, down_top) if s]
        return {
            "fill_key": self._fill_key(raw),
            "wallet": t["wallet"], "name": t["name"], "side": t["side"],
            "outcome": outcome, "token": token, "price": t["price"],
            "size": t["size"], "usdc": t["usdc"], "exchange_ts": ets,
            "observed_at": t["observed_at"],
            "window_age_sec": (ets - window.start_epoch) if ets else None,
            "window_remaining_sec": (window.end_epoch - ets) if ets else None,
            "up_bid": _g(up_top, "bid"), "up_ask": _g(up_top, "ask"),
            "down_bid": _g(down_top, "bid"), "down_ask": _g(down_top, "ask"),
            "book_lag_sec": max(ages) if ages else None,
            "spot": spot, "spot_age_sec": spot_age,
            "ret_1s_bps": rets[1], "ret_3s_bps": rets[3],
            "ret_5s_bps": rets[5], "ret_10s_bps": rets[10],
        }

    # ---- settlement: feed-based open/close, aggregate + persist ----------
    def _snapshot_boundaries(self) -> None:
        """Record BTC spot at each window's start/end from our Chainlink feed.

        Replaces the (now Cloudflare-blocked) crypto-price API: the live-data WS
        is the same Chainlink BTC/USD source the official resolution uses. Called
        every loop tick so boundary ticks are captured before they age out of the
        feed history.
        """
        now = time.time()
        dirty = False
        for state in self.active.values():
            w = state.window
            if state.open_price is None and now >= w.start_epoch:
                p = self.feed.price_at_or_before(w.start_epoch, max_backward_sec=90.0)
                if p:
                    state.open_price = p
                    self.db.execute("UPDATE windows SET open_price=? WHERE slug=?", (round(p, 2), w.slug))
                    dirty = True
            if state.close_price is None and now >= w.end_epoch:
                p = self.feed.price_at_or_before(w.end_epoch, max_backward_sec=90.0)
                if p:
                    state.close_price = p
                    dirty = True
        if dirty:
            self.db.commit()

    def settle_windows(self) -> None:
        now = time.time()
        for state in list(self.active.values()):
            if state.settled:
                continue
            w = state.window
            if now < w.end_epoch + SETTLE_GRACE_SEC:
                continue
            if state.open_price is not None and state.close_price is not None:
                self._settle(state)
            elif now > w.end_epoch + DROP_TIMEOUT_SEC:
                # never captured boundary prices (joined mid-window / feed gap) — drop, don't pile up
                del self.active[w.slug]
                state.fills = []
                print(f"[{now_utc_iso()}] DROP {w.slug} (no boundary price "
                      f"open={state.open_price} close={state.close_price})")

    def _settle(self, state: WindowState) -> None:
        open_p, close_p = state.open_price, state.close_price
        winning = "UP" if close_p > open_p else "DOWN"
        self.db.execute(
            """UPDATE windows SET open_price=?, close_price=?, winning_side=?,
               settled=1, settled_at=? WHERE slug=?""",
            (round(open_p, 2), round(close_p, 2), winning, now_utc_iso(), state.window.slug),
        )
        n_fills = len(state.fills)
        n_wallets, n_dual = self._persist_window(state, winning)
        self.db.commit()
        state.settled = True
        del self.active[state.window.slug]
        print(f"[{now_utc_iso()}] settled {state.window.slug} {winning} "
              f"(open={open_p:.2f} close={close_p:.2f}) fills={n_fills} "
              f"wallets={n_wallets} dual={n_dual}")

    def _aggregate(self, state: WindowState, winning: str) -> dict[str, dict]:
        agg: dict[str, dict] = {}
        for f in state.fills:
            w = agg.setdefault(f["wallet"], {
                "name": f["name"], "n": 0,
                "up_buys": 0, "down_buys": 0, "up_sells": 0, "down_sells": 0,
                "up_buy_usdc": 0.0, "down_buy_usdc": 0.0, "up_sell_usdc": 0.0, "down_sell_usdc": 0.0,
                "up_buy_sh": 0.0, "down_buy_sh": 0.0,
                "up_sh": 0.0, "down_sh": 0.0,
                "first_ts": f["exchange_ts"], "last_ts": f["exchange_ts"],
            })
            w["n"] += 1
            up = f["outcome"].lower() == "up"
            buy = f["side"].upper() == "BUY"
            size, usdc = f["size"], f["usdc"]
            if up and buy:
                w["up_buys"] += 1; w["up_buy_usdc"] += usdc; w["up_buy_sh"] += size; w["up_sh"] += size
            elif up and not buy:
                w["up_sells"] += 1; w["up_sell_usdc"] += usdc; w["up_sh"] -= size
            elif not up and buy:
                w["down_buys"] += 1; w["down_buy_usdc"] += usdc; w["down_buy_sh"] += size; w["down_sh"] += size
            else:
                w["down_sells"] += 1; w["down_sell_usdc"] += usdc; w["down_sh"] -= size
            if f["exchange_ts"]:
                w["first_ts"] = min(w["first_ts"] or f["exchange_ts"], f["exchange_ts"])
                w["last_ts"] = max(w["last_ts"] or f["exchange_ts"], f["exchange_ts"])
        return agg

    def _persist_window(self, state: WindowState, winning: str) -> tuple[int, int]:
        agg = self._aggregate(state, winning)
        dual_wallets: set[str] = set()
        for wallet, w in agg.items():
            up_vwap = w["up_buy_usdc"] / w["up_buy_sh"] if w["up_buy_sh"] > EPS else None
            down_vwap = w["down_buy_usdc"] / w["down_buy_sh"] if w["down_buy_sh"] > EPS else None
            pair_cost = (up_vwap + down_vwap) if (up_vwap and down_vwap) else None
            pair_shares = min(w["up_sh"], w["down_sh"]) if (w["up_sh"] > EPS and w["down_sh"] > EPS) else None
            dual = 1 if (w["up_buys"] > 0 and w["down_buys"] > 0) else 0
            incomplete = 1 if (w["up_sh"] < -EPS or w["down_sh"] < -EPS) else 0
            cash = (w["up_sell_usdc"] + w["down_sell_usdc"]) - (w["up_buy_usdc"] + w["down_buy_usdc"])
            win_shares = w["up_sh"] if winning == "UP" else w["down_sh"]
            realized = round(cash + max(win_shares, 0.0), 6)
            if dual:
                dual_wallets.add(wallet)
            self.db.execute(
                """INSERT OR REPLACE INTO wallet_window
                   (window_slug, wallet, name, n_trades, up_buys, down_buys, up_sells, down_sells,
                    up_buy_usdc, down_buy_usdc, up_sell_usdc, down_sell_usdc,
                    up_shares, down_shares, pair_shares, pair_cost, dual,
                    realized_pnl, incomplete, first_ts, last_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (state.window.slug, wallet, w["name"], w["n"], w["up_buys"], w["down_buys"],
                 w["up_sells"], w["down_sells"], round(w["up_buy_usdc"], 6), round(w["down_buy_usdc"], 6),
                 round(w["up_sell_usdc"], 6), round(w["down_sell_usdc"], 6),
                 round(w["up_sh"], 6), round(w["down_sh"], 6),
                 round(pair_shares, 6) if pair_shares else None,
                 round(pair_cost, 6) if pair_cost else None, dual,
                 realized, incomplete, w["first_ts"], w["last_ts"]),
            )
        # per-fill detail for ALL wallets (complete substrate; roles classified
        # later by re-pulling target wallets). dual_wallets kept only for stats.
        for f in state.fills:
            self.db.execute(
                """INSERT OR IGNORE INTO trades
                   (fill_key, window_slug, condition_id, wallet, name, side, outcome, token,
                    price, size, usdc, exchange_ts, observed_at, window_age_sec, window_remaining_sec,
                    up_bid, up_ask, down_bid, down_ask, book_lag_sec,
                    spot, spot_age_sec, ret_1s_bps, ret_3s_bps, ret_5s_bps, ret_10s_bps)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f["fill_key"], state.window.slug, state.window.condition_id, f["wallet"], f["name"],
                 f["side"], f["outcome"], f["token"], f["price"], f["size"], f["usdc"], f["exchange_ts"],
                 f["observed_at"], f["window_age_sec"], f["window_remaining_sec"],
                 f["up_bid"], f["up_ask"], f["down_bid"], f["down_ask"], f["book_lag_sec"],
                 f["spot"], f["spot_age_sec"], f["ret_1s_bps"], f["ret_3s_bps"], f["ret_5s_bps"], f["ret_10s_bps"]),
            )
        n_wallets, n_dual = len(agg), len(dual_wallets)
        state.fills = []  # free buffer
        return n_wallets, n_dual

    # ---- persistence helpers ---------------------------------------------
    def _upsert_window(self, window: MarketWindow) -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO windows
               (slug, condition_id, up_token, down_token, start_epoch, end_epoch, settled, first_seen_at)
               VALUES (?,?,?,?,?,?,0,?)""",
            (window.slug, window.condition_id, window.up_token, window.down_token,
             window.start_epoch, window.end_epoch, now_utc_iso()),
        )
        self.db.commit()

    # ---- main loop --------------------------------------------------------
    async def run(self) -> int:
        await self.feed.start()
        await self.refresh_windows()
        print(f"[{now_utc_iso()}] collector up; feed+book warming...")
        started = time.monotonic()
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if self.seconds is not None and now - started >= self.seconds:
                    break
                if now - self._t_windows >= REFRESH_WINDOWS_SEC:
                    self._t_windows = now
                    await self.refresh_windows()
                if now - self._t_trades >= POLL_TRADES_SEC:
                    self._t_trades = now
                    await self.poll_trades()
                self._snapshot_boundaries()   # cheap; capture open/close before they age out
                if now - self._t_settle >= SETTLE_CHECK_SEC:
                    self._t_settle = now
                    self.settle_windows()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.stream.close()
            await self.feed.stop()
            self.db.commit()
            self.db.close()
        return 0

    def stop(self) -> None:
        self._stop.set()


def _g(snap: dict | None, key: str):
    return snap.get(key) if snap else None


def main() -> int:
    ap = argparse.ArgumentParser(description="BTC 5min pair-arb wallet collector")
    ap.add_argument("--db", default="btc5min.db")
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--seconds", type=float, default=None, help="auto-stop after N seconds")
    args = ap.parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    collector = Collector(args.db, args.symbol, seconds=args.seconds)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, collector.stop)
        except NotImplementedError:
            pass
    try:
        return loop.run_until_complete(collector.run())
    finally:
        loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
