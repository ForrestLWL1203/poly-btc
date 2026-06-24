"""Live WS observer + paper-copy driver.

Owns the stateful streaming: connect/reconnect/heartbeat/resubscribe, REST backfill
on reconnect (completeness), per-coin bbo buffers, streaming episode detection, and
liquidation alerts. Delegates pure sim math to paper.py and all persistence to the
storage tables. Monitors the top-N enabled watchlist targets (HL cap: 10 users/IP).
"""
import asyncio
import json
import logging
import time
from collections import deque

import websockets

from . import config, paper, rest, ws
from .util import f, now_ms

logging.getLogger("websockets").setLevel(logging.CRITICAL)  # silence library reconnect noise


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Observer:
    def __init__(self, db, addrs: list, seed_coins: dict):
        self.db = db
        self.addrs = addrs
        self.seed_coins = seed_coins
        self.coin_hist: dict = {}        # coin -> deque[(ts_ms, bid, ask)]
        self.sub_coins: set = set()
        self.pos: dict = {}              # (addr,coin) -> net position
        self.open_ep: dict = {}          # (addr,coin) -> live episode state
        self.last_fill_ms: dict = {a: now_ms() - 6 * 3600_000 for a in addrs}
        self.valid_coins: set = set()    # standard perp universe; empty => allow all
        self.last_msg_ms = now_ms()      # last time ANY ws message arrived (for gap sizing)
        self.connected_once = False
        self.ws = None
        self.stop = False

    def _bbo_ok(self, coin: str) -> bool:
        return bool(coin) and (not self.valid_coins or coin in self.valid_coins)

    # -- subscriptions --------------------------------------------------------
    async def _sub(self, subscription: dict):
        await self.ws.send(ws.sub_msg(subscription))
        await asyncio.sleep(0.05)

    async def subscribe_all(self):
        # userFills only — it already carries the `liquidation` field, so we skip
        # userEvents (a 2nd user-specific sub per wallet that risks tripping HL's limit).
        for a in self.addrs:
            await self._sub(ws.user_fills(a))
        for a in self.addrs:
            for c in self.seed_coins.get(a, set()):
                await self.ensure_coin(c)
        await self._sub(ws.bbo("BTC"))   # liveness baseline

    async def ensure_coin(self, coin: str):
        if coin and coin not in self.sub_coins and self._bbo_ok(coin):
            self.sub_coins.add(coin)
            self.coin_hist.setdefault(coin, deque())
            try:
                await self._sub(ws.bbo(coin))
            except Exception:  # noqa: BLE001
                self.sub_coins.discard(coin)

    async def heartbeat(self):
        while not self.stop:
            await asyncio.sleep(30)
            try:
                await self.ws.send(ws.PING)
            except Exception:  # noqa: BLE001
                return

    async def _announce(self):
        while not self.stop:
            await asyncio.sleep(300)
            ep = self.db.execute("SELECT count(*) FROM episodes_live").fetchone()[0]
            legs = self.db.execute("SELECT count(*) FROM paper_legs").fetchone()[0]
            _log(f"heartbeat: {ep} episodes, {legs} paper legs recorded")

    @staticmethod
    def _quiet(loop, context):
        msg = str(context.get("exception") or context.get("message"))
        if "SSL" in msg or "closed" in msg.lower():
            return                                  # transient closed-connection noise
        loop.default_exception_handler(context)

    # -- run loop with reconnect ---------------------------------------------
    async def run(self):
        asyncio.get_event_loop().set_exception_handler(self._quiet)
        self.valid_coins = rest.perp_universe()
        _log(f"perp universe: {len(self.valid_coins)} coins (bbo subs guarded against unknown names)")
        asyncio.create_task(self._announce())
        while not self.stop:
            try:
                async with websockets.connect(config.WS_URL, ping_interval=None, max_size=None) as conn:
                    self.ws = conn
                    hb = asyncio.create_task(self.heartbeat())
                    sub = asyncio.create_task(self.subscribe_all())  # subscribe WHILE reading,
                    if self.connected_once and self.open_ep:        # reconcile the disconnect gap:
                        asyncio.create_task(self.reconcile_gap(self.last_msg_ms))  # did a tracked
                    self.connected_once = True                      # master exit while we were down?
                    _log(f"connected, monitoring {len(self.addrs)} wallets")
                    async for raw in conn:
                        self.on_message(raw)
                    hb.cancel()
                    sub.cancel()
            except Exception as exc:  # noqa: BLE001
                _log(f"ws error: {exc}; reconnecting in 3s")
                await asyncio.sleep(3)

    # -- message router -------------------------------------------------------
    def on_message(self, raw: str):
        self.last_msg_ms = now_ms()
        m = json.loads(raw)
        ch = m.get("channel")
        if ch == "userFills":
            d = m.get("data", {})
            if d.get("isSnapshot"):
                return                  # drop the on-subscribe history dump — we only
                                        # paper-trade FUTURE round-trips observed live
            a = (d.get("user") or "").lower()
            fills = d.get("fills", []) or []
            for x in fills:
                self.process_fill(a, x, live=True)
            if fills:
                self.db.commit()        # one commit per message, not per fill
        elif ch == "userEvents":
            liq = (m.get("data") or {}).get("liquidation")
            if liq:
                _log(f"!! LIQUIDATION event: {json.dumps(liq)[:160]}")
        elif ch == "bbo":
            self.on_bbo(m.get("data", {}))

    def on_bbo(self, d: dict):
        coin = d.get("coin")
        ba = d.get("bbo") or []
        if not coin or len(ba) < 2 or not ba[0] or not ba[1]:
            return
        bid, ask = f(ba[0].get("px")), f(ba[1].get("px"))
        t = now_ms()
        h = self.coin_hist.setdefault(coin, deque())
        h.append((t, bid, ask))
        cutoff = t - config.BOOK_HIST_S * 1000
        while h and h[0][0] < cutoff:
            h.popleft()
        mid = (bid + ask) / 2
        for (a, c), ep in self.open_ep.items():       # track MAE on open paper positions
            if c == coin and ep.get("their_open_px"):
                adv = ((ep["their_open_px"] - mid) if ep["side"] == "long"
                       else (mid - ep["their_open_px"])) / ep["their_open_px"]
                ep["mae"] = max(ep.get("mae", 0.0), adv)

    # -- reconnect reconciliation (don't stay stranded in a position the master exited) --
    async def reconcile_gap(self, gap_since: int):
        """On reconnect, dynamic-lookback (gap + margin) ONLY for wallets we have an open copy
        on: if the master closed during the downtime, finalize our copy as a late (gap) exit.
        New positions opened during the gap are ignored — we never saw their live entry."""
        margin = 60_000
        tracked: dict = {}
        for (addr, coin) in list(self.open_ep.keys()):
            tracked.setdefault(addr, set()).add(coin)
        if not tracked:
            return
        _log(f"reconnect: reconciling {(now_ms()-gap_since)/1000:.0f}s gap across "
             f"{len(tracked)} wallet(s) with open copies")
        for addr in tracked:
            page = await asyncio.to_thread(
                rest.post_soft, {"type": "userFillsByTime", "user": addr, "startTime": int(gap_since - margin)})
            if not isinstance(page, list):
                continue
            for x in sorted(page, key=lambda fl: fl["time"]):
                key = (addr, x.get("coin"))
                ep = self.open_ep.get(key)
                if ep is None:                       # only reconcile positions we are tracking
                    continue
                signed = f(x.get("sz")) * (1 if x.get("side") == "B" else -1)
                pos1 = f(x.get("startPosition")) + signed
                self.pos[key] = pos1
                ep["their_close_px"] = f(x.get("px"))
                if x.get("liquidation"):
                    ep["was_liq"] = 1
                if abs(pos1) < config.FLAT:           # master exited while we were down
                    ep["close_ms"] = x["time"]
                    ep["gap_exit"] = True
                    self.open_ep.pop(key, None)
                    _log(f"GAP-EXIT {addr[:10]} {x.get('coin')}: master closed during downtime "
                         f"-> closing our copy late")
                    asyncio.create_task(self._finalize(addr, x.get("coin"), ep))

    # -- core: fills -> episodes ---------------------------------------------
    def process_fill(self, addr: str, x: dict, live: bool):
        tid, coin = x.get("tid"), x.get("coin")
        if not coin or tid is None:
            return
        t = x["time"]
        self.last_fill_ms[addr] = max(self.last_fill_ms.get(addr, 0), t)
        liq = x.get("liquidation")
        self.db.execute(
            "INSERT OR IGNORE INTO live_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (addr, tid, t, now_ms(), coin, x.get("side"), x.get("dir"), f(x.get("px")),
             f(x.get("sz")), f(x.get("closedPnl")), f(x.get("fee")), 1 if x.get("crossed") else 0,
             1 if liq else 0, (liq or {}).get("method"), x.get("hash")))
        # NB: caller (on_message / backfill) commits in batch — never commit per fill here.

        sz = f(x.get("sz"))
        signed = sz if x.get("side") == "B" else -sz
        pos0 = f(x.get("startPosition"))
        pos1 = pos0 + signed
        key = (addr, coin)
        self.pos[key] = pos1
        px = f(x.get("px"))
        if live and coin not in self.sub_coins:
            asyncio.create_task(self.ensure_coin(coin))

        ep = self.open_ep.get(key)
        if ep is None and abs(pos0) < config.FLAT and abs(pos1) >= config.FLAT:
            ep = {"side": "long" if pos1 > 0 else "short", "open_ms": t, "their_open_px": px,
                  "live": live, "mae": 0.0, "was_liq": 1 if liq else 0, "entries": None,
                  "entries_ready": asyncio.Event()}
            self.open_ep[key] = ep
            if live:
                asyncio.create_task(self._resolve_entries(coin, ep))
        elif ep is not None:
            ep["their_close_px"] = px
            if liq:
                ep["was_liq"] = 1
            if abs(pos1) < config.FLAT:
                ep["close_ms"] = t
                self.open_ep.pop(key, None)
                if ep.get("live"):
                    asyncio.create_task(self._finalize(addr, coin, ep))
                else:
                    self._record_episode(addr, coin, ep)

    async def _resolve_entries(self, coin, ep):
        await asyncio.sleep(max(config.LATENCIES) + 0.2)
        side_is_buy = ep["side"] == "long"
        h = self.coin_hist.get(coin, deque())
        entries = {}
        for L in config.LATENCIES:
            ba = paper.book_at(h, ep["open_ms"] + int(L * 1000))
            entries[L] = (ba[1] if side_is_buy else ba[0]) if ba else None
        ep["entries"] = entries
        ep["entries_ready"].set()

    async def _finalize(self, addr, coin, ep):
        await asyncio.sleep(max(config.LATENCIES) + 0.3)
        try:
            await asyncio.wait_for(ep["entries_ready"].wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
        side_is_buy = ep["side"] == "long"
        h = self.coin_hist.get(coin, deque())
        gap = ep.get("gap_exit")
        exits = {}
        for L in config.LATENCIES:
            # normal: exit at master-close + our latency. gap-exit: we only learned of the
            # master's close on reconnect, so we exit LATE at the reconnect price (same for all
            # bands) — this prices the cost of the disconnect into the result.
            at = now_ms() if gap else ep["close_ms"] + int(L * 1000)
            ba = paper.book_at(h, at)
            exits[L] = (ba[0] if side_is_buy else ba[1]) if ba else None   # close = opposite side
        self._record_episode(addr, coin, ep)
        their_op = ep["their_open_px"]
        their_cl = ep.get("their_close_px", their_op)
        legs = paper.compute_legs(ep["side"], their_op, their_cl, ep.get("entries") or {}, exits)
        for lg in legs:
            self.db.execute(
                "INSERT OR REPLACE INTO paper_legs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (addr, coin, ep["open_ms"], lg["latency_s"], lg["our_entry_px"], lg["our_exit_px"],
                 lg["slip_entry_bps"], lg["slip_exit_bps"], lg["pnl_usd"], lg["pnl_pct"], lg["fees_usd"]))
        self.db.commit()
        their_pct = (their_cl - their_op) / their_op * 100 * (1 if side_is_buy else -1)
        _log(f"copied {addr[:10]} {coin} {ep['side']} hold={(ep['close_ms']-ep['open_ms'])/3600000:.1f}h "
             f"theirPnL={their_pct:+.2f}% mae={ep.get('mae',0)*100:.2f}% legs={len(legs)}")

    def _record_episode(self, addr, coin, ep):
        if "close_ms" not in ep:
            return
        side_is_buy = ep["side"] == "long"
        op, cl = ep["their_open_px"], ep.get("their_close_px", ep["their_open_px"])
        self.db.execute(
            "INSERT OR REPLACE INTO episodes_live VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (addr, coin, ep["open_ms"], ep["close_ms"], ep["side"],
             (ep["close_ms"] - ep["open_ms"]) / 1000.0, op, cl,
             (cl - op) / op * (1 if side_is_buy else -1), ep.get("mae", 0.0), ep.get("was_liq", 0)))
        self.db.commit()


# ------------------------------------------------------------------------- loaders
def load_targets(db, n: int):
    """Top-N enabled watchlist targets (by rank) + the coins they recently traded."""
    addrs = [r[0] for r in db.execute(
        "SELECT w.addr FROM watchlist w LEFT JOIN target_controls c ON c.addr=w.addr "
        "WHERE COALESCE(c.enabled,1)=1 ORDER BY w.rank LIMIT ?", (n,)).fetchall()]
    seed = {a: {r[0] for r in db.execute("SELECT DISTINCT coin FROM episode WHERE addr=?", (a,)).fetchall()}
            for a in addrs}
    return addrs, seed


# -------------------------------------------------------------------------- report
def report(db) -> None:
    rows = db.execute(
        "SELECT addr, latency_s, count(*) n, avg(pnl_pct)*100 avg_pct, sum(pnl_usd) tot, "
        "avg(slip_entry_bps) sin, sum(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END)*1.0/count(*) winr "
        "FROM paper_legs GROUP BY addr, latency_s ORDER BY addr, latency_s").fetchall()
    if not rows:
        print("no paper legs yet — observer needs to run while targets trade.")
        return
    print(f"\nPAPER COPY RESULTS (notional ${config.NOTIONAL:g}/trade, taker {config.TAKER_FEE*1e4:.1f}bps/side)\n")
    hdr = f"{'addr':42} {'lat':>4} {'trades':>6} {'avgPnL%':>8} {'totUSD':>9} {'win%':>5} {'slipIn(bps)':>11}"
    print(hdr); print("-" * len(hdr))
    for addr, lat, n, avg_pct, tot, sin, winr in rows:
        print(f"{addr:42} {lat:>4.1f} {n:>6} {avg_pct:>+7.2f}% {tot:>9,.1f} {winr*100:>4.0f}% {sin:>11.1f}")
    print("\n(avgPnL% = our return per $ notional after slippage+fees, by copy latency)")
    ep = db.execute("SELECT count(*), sum(was_liquidated) FROM episodes_live").fetchone()
    print(f"observed master episodes: {ep[0]}  (liquidations: {ep[1] or 0})")
