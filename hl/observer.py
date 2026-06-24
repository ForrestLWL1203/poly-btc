"""Live WS observer + paper-copy driver.

Owns the stateful streaming: connect/reconnect/heartbeat/resubscribe, reconnect
reconciliation (don't stay stranded when the master exits during a disconnect),
per-coin bbo buffers, and a PARTIAL-AWARE copy state machine. Every master action
on a tracked position (open / add / reduce / close) is persisted immediately to
copy_action with full detail + our mirrored fill; each copied position lives in
copy_position (open→closed), so nothing is held only in memory — it survives
restarts (open positions are reloaded on startup).

Sizing: fixed `NOTIONAL` per position, entered at the master's open. We do NOT add
on scale-in; on scale-out we reduce our copy PROPORTIONALLY (|master pos| / peak).
Primary copy = 2s latency (what copy_action records); pnl_05/2/5 on copy_position
carry the 3-band latency sensitivity. Exit price comes from the live book at
fill_time + latency (taker). Leverage is irrelevant to PnL (notional × move).
"""
import asyncio
import json
import logging
import time
from collections import deque

import websockets

from . import config, paper, rest, ws
from .util import f, now_iso, now_ms

logging.getLogger("websockets").setLevel(logging.CRITICAL)
LAT = config.LATENCIES
PRIMARY = 2.0 if 2.0 in LAT else LAT[len(LAT) // 2]


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Observer:
    def __init__(self, db, addrs: list, seed_coins: dict):
        self.db = db
        self.addrs = addrs
        self.seed_coins = seed_coins
        self.coin_hist: dict = {}        # coin -> deque[(ts_ms, bid, ask)]
        self.sub_coins: set = set()
        self.open_ep: dict = {}          # (addr,coin) -> position state
        self.valid_coins: set = set()
        self.last_msg_ms = now_ms()
        self.connected_once = False
        self.ws = None
        self.stop = False

    def _bbo_ok(self, coin: str) -> bool:
        return bool(coin) and (not self.valid_coins or coin in self.valid_coins)

    # -- restart recovery: reload open copies from db ------------------------
    def _reload_open(self):
        rows = self.db.execute(
            "SELECT pos_id,addr,coin,side,master_open_ms,master_open_px,master_peak_sz,"
            "entry_05,entry_2,entry_5,rem_05,rem_2,rem_5,pnl_05,pnl_2,pnl_5,mae_pct,num_actions "
            "FROM copy_position WHERE status='open'").fetchall()
        for r in rows:
            (pid, addr, coin, side, mo, mpx, peak, e05, e2, e5, r05, r2, r5, p05, p2, p5, mae, na) = r
            ev = asyncio.Event()
            if e2 is not None:
                ev.set()
            self.open_ep[(addr, coin)] = {
                "pos_id": pid, "side": side, "sign": 1 if side == "long" else -1,
                "master_open_ms": mo, "master_open_px": mpx, "master_peak": peak or 0.0,
                "entries": {0.5: e05, 2.0: e2, 5.0: e5} if e2 is not None else None,
                "rem": {0.5: r05 or 0, 2.0: r2 or 0, 5.0: r5 or 0},
                "pnl": {0.5: p05 or 0, 2.0: p2 or 0, 5.0: p5 or 0},
                "entries_ready": ev, "lock": asyncio.Lock(), "mae": mae or 0.0,
                "num_actions": na or 0, "gap": False}
        if rows:
            _log(f"reloaded {len(rows)} open copy positions from db")

    # -- subscriptions --------------------------------------------------------
    async def _sub(self, subscription: dict):
        await self.ws.send(ws.sub_msg(subscription))
        await asyncio.sleep(0.05)

    async def subscribe_all(self):
        for a in self.addrs:
            await self._sub(ws.user_fills(a))
        for a in self.addrs:
            for c in self.seed_coins.get(a, set()):
                await self.ensure_coin(c)
        for (_, coin) in self.open_ep:          # reloaded positions need their book too
            await self.ensure_coin(coin)
        await self._sub(ws.bbo("BTC"))

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
            o = self.db.execute("SELECT count(*) FROM copy_position WHERE status='open'").fetchone()[0]
            c = self.db.execute("SELECT count(*) FROM copy_position WHERE status!='open'").fetchone()[0]
            a = self.db.execute("SELECT count(*) FROM copy_action").fetchone()[0]
            _log(f"heartbeat: {o} open / {c} closed positions, {a} actions recorded")

    @staticmethod
    def _quiet(loop, context):
        msg = str(context.get("exception") or context.get("message"))
        if "SSL" in msg or "closed" in msg.lower():
            return
        loop.default_exception_handler(context)

    # -- run loop with reconnect ---------------------------------------------
    async def run(self):
        asyncio.get_event_loop().set_exception_handler(self._quiet)
        self.valid_coins = rest.perp_universe()
        _log(f"perp universe: {len(self.valid_coins)} coins (bbo subs guarded)")
        self._reload_open()                         # restore open copies from db (restart-safe)
        asyncio.create_task(self._announce())
        while not self.stop:
            try:
                async with websockets.connect(config.WS_URL, ping_interval=None, max_size=None) as conn:
                    self.ws = conn
                    hb = asyncio.create_task(self.heartbeat())
                    sub = asyncio.create_task(self.subscribe_all())
                    if self.connected_once and self.open_ep:
                        asyncio.create_task(self.reconcile_gap(self.last_msg_ms))
                    self.connected_once = True
                    _log(f"connected, monitoring {len(self.addrs)} wallets ({len(self.open_ep)} open copies)")
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
                return                  # forward-only: ignore the on-subscribe history dump
            a = (d.get("user") or "").lower()
            fills = d.get("fills", []) or []
            for x in fills:
                self.process_fill(a, x)
            if fills:
                self.db.commit()
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
        for (a, c), ep in self.open_ep.items():
            if c == coin:
                adv = ((ep["master_open_px"] - mid) if ep["side"] == "long"
                       else (mid - ep["master_open_px"])) / ep["master_open_px"]
                ep["mae"] = max(ep.get("mae", 0.0), adv)

    def _record_fill(self, addr, x):
        liq = x.get("liquidation")
        self.db.execute(
            "INSERT OR IGNORE INTO live_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (addr, x.get("tid"), x.get("time"), now_ms(), x.get("coin"), x.get("side"), x.get("dir"),
             f(x.get("px")), f(x.get("sz")), f(x.get("closedPnl")), f(x.get("fee")),
             1 if x.get("crossed") else 0, 1 if liq else 0, (liq or {}).get("method"), x.get("hash")))

    # -- core: master fills -> copy actions ----------------------------------
    def process_fill(self, addr: str, x: dict):
        coin = x.get("coin")
        if not coin or x.get("tid") is None:
            return
        self._record_fill(addr, x)
        t = x["time"]
        sz = f(x.get("sz"))
        signed = sz if x.get("side") == "B" else -sz
        pos0 = f(x.get("startPosition"))
        pos1 = pos0 + signed
        px = f(x.get("px"))
        key = (addr, coin)
        liq = bool(x.get("liquidation"))
        if coin not in self.sub_coins:
            asyncio.create_task(self.ensure_coin(coin))

        ep = self.open_ep.get(key)
        if ep is None:
            if abs(pos0) < config.FLAT and abs(pos1) >= config.FLAT:
                self._open_position(addr, coin, t, px, pos1)
            return
        ep["master_peak"] = max(ep["master_peak"], abs(pos1))
        if liq:
            ep["was_liq"] = 1
        if abs(pos1) >= abs(pos0) - config.FLAT and not abs(pos1) < config.FLAT:
            self._record_action(ep, addr, coin, t, "add", px, signed, pos1, 0.0, px, 0.0, 0.0)
        else:
            asyncio.create_task(self._apply_reduce(addr, coin, ep, t, px, signed, pos1,
                                                   closing=abs(pos1) < config.FLAT, liq=liq))

    def _open_position(self, addr, coin, t, px, pos1):
        side = "long" if pos1 > 0 else "short"
        cur = self.db.execute(
            "INSERT INTO copy_position (addr,coin,side,status,master_open_ms,master_open_px,"
            "master_peak_sz,our_notional,opened_at,num_actions) VALUES (?,?,?,'open',?,?,?,?,?,0)",
            (addr, coin, side, t, px, abs(pos1), config.NOTIONAL, now_iso()))
        ep = {"pos_id": cur.lastrowid, "side": side, "sign": 1 if side == "long" else -1,
              "master_open_ms": t, "master_open_px": px, "master_peak": abs(pos1),
              "entries": None, "rem": {L: 0.0 for L in LAT}, "pnl": {L: 0.0 for L in LAT},
              "entries_ready": asyncio.Event(), "lock": asyncio.Lock(), "mae": 0.0,
              "num_actions": 0, "gap": False}
        self.open_ep[(addr, coin)] = ep
        asyncio.create_task(self._resolve_entry(addr, coin, ep, t, px))

    async def _resolve_entry(self, addr, coin, ep, t, master_px):
        await asyncio.sleep(max(LAT) + 0.3)
        side_is_buy = ep["side"] == "long"
        h = self.coin_hist.get(coin, deque())
        entries = {}
        for L in LAT:
            ba = paper.book_at(h, t + int(L * 1000))
            entries[L] = (ba[1] if side_is_buy else ba[0]) if ba else master_px  # taker: buy@ask
        ep["entries"] = entries
        ep["rem"] = {L: config.NOTIONAL / entries[L] for L in LAT}
        ep["entries_ready"].set()
        self.db.execute(
            "UPDATE copy_position SET entry_05=?,entry_2=?,entry_5=?,rem_05=?,rem_2=?,rem_5=? WHERE pos_id=?",
            (entries[0.5], entries[2.0], entries[5.0], ep["rem"][0.5], ep["rem"][2.0], ep["rem"][5.0], ep["pos_id"]))
        slip = (entries[PRIMARY] - master_px) / master_px * 1e4 * ep["sign"]
        msz = ep["master_peak"] * ep["sign"]        # master's signed open size / position-after
        self._record_action(ep, addr, coin, t, "open", master_px, msz, msz,
                            ep["rem"][PRIMARY] * ep["sign"], entries[PRIMARY], 0.0, slip)
        self.db.commit()

    async def _apply_reduce(self, addr, coin, ep, t, master_px, signed, pos1, closing, liq, gap=False):
        async with ep["lock"]:
            try:
                await asyncio.wait_for(ep["entries_ready"].wait(), timeout=12)
            except asyncio.TimeoutError:
                pass
            if ep.get("entries") is None:
                return
            await asyncio.sleep(max(LAT) + 0.3)
            side_is_buy = ep["side"] == "long"
            h = self.coin_hist.get(coin, deque())
            target_frac = 0.0 if closing else abs(pos1) / max(ep["master_peak"], 1e-12)
            prim = {"qty": 0.0, "px": master_px, "pnl": 0.0}
            for L in LAT:
                qty_total = config.NOTIONAL / ep["entries"][L]
                close_qty = max(0.0, ep["rem"][L] - qty_total * target_frac)
                at = now_ms() if gap else t + int(L * 1000)
                ba = paper.book_at(h, at)
                exit_px = (ba[0] if side_is_buy else ba[1]) if ba else master_px  # taker: sell@bid
                pnl = close_qty * (exit_px - ep["entries"][L]) * ep["sign"]
                ep["pnl"][L] += pnl
                ep["rem"][L] -= close_qty
                if L == PRIMARY:
                    prim = {"qty": close_qty, "px": exit_px, "pnl": pnl}
            slip = (master_px - prim["px"]) / master_px * 1e4 * ep["sign"]
            action = "close" if closing else "reduce"
            self._record_action(ep, addr, coin, t, action, master_px, signed, pos1,
                                -prim["qty"] * ep["sign"], prim["px"], prim["pnl"], slip)
            status = ("liquidated" if (closing and liq) else "gap_closed" if (closing and gap)
                      else "closed" if closing else "open")
            self.db.execute(
                "UPDATE copy_position SET rem_05=?,rem_2=?,rem_5=?,pnl_05=?,pnl_2=?,pnl_5=?,"
                "mae_pct=?,was_liq=?,status=?,closed_at=? WHERE pos_id=?",
                (ep["rem"][0.5], ep["rem"][2.0], ep["rem"][5.0], ep["pnl"][0.5], ep["pnl"][2.0],
                 ep["pnl"][5.0], ep["mae"], ep.get("was_liq", 0), status,
                 now_iso() if closing else None, ep["pos_id"]))
            self.db.commit()
            if closing:
                self.open_ep.pop((addr, coin), None)
                pct = ep["pnl"][PRIMARY] / config.NOTIONAL * 100
                _log(f"CLOSED {addr[:10]} {coin} {ep['side']} pnl(2s)={pct:+.2f}% "
                     f"mae={ep['mae']*100:.2f}% actions={ep['num_actions']}{' [GAP]' if gap else ''}"
                     f"{' [LIQ]' if liq else ''}")

    def _record_action(self, ep, addr, coin, t, action, master_px, sz_delta, pos_after,
                       our_qty_delta, our_px, realized, slip):
        ep["num_actions"] += 1
        self.db.execute(
            "INSERT INTO copy_action (pos_id,addr,coin,ts,recv_ms,action,master_px,master_sz_delta,"
            "master_pos_after,our_qty_delta,our_px,realized_pnl,slippage_bps) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ep["pos_id"], addr, coin, t, now_ms(), action, master_px, sz_delta, pos_after,
             our_qty_delta, our_px, realized, slip))
        self.db.execute("UPDATE copy_position SET num_actions=?, master_peak_sz=? WHERE pos_id=?",
                        (ep["num_actions"], ep["master_peak"], ep["pos_id"]))

    # -- reconnect reconciliation --------------------------------------------
    async def reconcile_gap(self, gap_since: int):
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
                if ep is None:
                    continue
                signed = f(x.get("sz")) * (1 if x.get("side") == "B" else -1)
                pos1 = f(x.get("startPosition")) + signed
                ep["master_peak"] = max(ep["master_peak"], abs(pos1))
                if abs(pos1) < abs(f(x.get("startPosition"))) - config.FLAT:   # a reduce/close in the gap
                    await self._apply_reduce(addr, x.get("coin"), ep, x["time"], f(x.get("px")), signed,
                                             pos1, closing=abs(pos1) < config.FLAT,
                                             liq=bool(x.get("liquidation")), gap=True)


# ------------------------------------------------------------------------- loaders
def load_targets(db, n: int):
    addrs = [r[0] for r in db.execute(
        "SELECT w.addr FROM watchlist w LEFT JOIN target_controls c ON c.addr=w.addr "
        "WHERE COALESCE(c.enabled,1)=1 ORDER BY w.rank LIMIT ?", (n,)).fetchall()]
    seed = {a: {r[0] for r in db.execute("SELECT DISTINCT coin FROM episode WHERE addr=?", (a,)).fetchall()}
            for a in addrs}
    return addrs, seed


# -------------------------------------------------------------------------- report
def report(db) -> None:
    rows = db.execute(
        "SELECT addr, count(*) n, "
        "sum(CASE WHEN pnl_2>0 THEN 1 ELSE 0 END)*1.0/count(*) winr, "
        "sum(pnl_05) p05, sum(pnl_2) p2, sum(pnl_5) p5, avg(pnl_2/our_notional)*100 avg2, "
        "avg(num_actions) acts FROM copy_position WHERE status!='open' GROUP BY addr ORDER BY p2 DESC").fetchall()
    op = db.execute("SELECT count(*) FROM copy_position WHERE status='open'").fetchone()[0]
    if not rows:
        print(f"no closed copy positions yet ({op} open). Observer needs a target to complete a round-trip.")
        return
    print(f"\nPAPER COPY RESULTS (notional ${config.NOTIONAL:g}/position; pnl by latency band)  [{op} still open]\n")
    hdr = f"{'addr':42} {'closed':>6} {'win%':>5} {'pnl@0.5s':>9} {'pnl@2s':>9} {'pnl@5s':>9} {'avg2%':>7} {'acts':>5}"
    print(hdr); print("-" * len(hdr))
    for addr, n, winr, p05, p2, p5, avg2, acts in rows:
        print(f"{addr:42} {n:>6} {winr*100:>4.0f}% {p05:>9,.1f} {p2:>9,.1f} {p5:>9,.1f} {avg2:>+6.2f}% {acts:>5.1f}")
    print("\n(pnl@Xs = total $ if we'd copied at that reaction latency; primary copy = 2s)")
