"""Live copy-trade observer + paper-copy driver.

Two decoupled data planes, by design:
  • SIGNAL (who traded what) — a continuous REST poll over the FULL watchlist (per-wallet
    userFillsByTime, cursor + small overlap, idempotent by tid). REST has no 10-user cap, so we
    can follow the whole watchlist; our targets are low-freq long-hold, so a few-seconds poll
    latency is fine. This is the primary engine.
  • PRICING (what we'd fill at) — a WS bbo subscription PER COIN (top-of-book). bbo subs are
    per-coin, NOT subject to the 10-user cap (only the 1000-sub cap, and we touch a few dozen
    coins). We price our copy off the LIVE book at detection: taker buy→best ask, sell→best bid;
    maker buy→best bid, sell→best ask. (No user subscriptions on this WS, so no 10-user concern.)

A PARTIAL-AWARE copy state machine persists every master action (open/add/reduce/close) to
copy_action with full detail + our mirrored fill; each copied position lives in copy_position
(open→closed) and is reloaded on restart. Sizing: fixed NOTIONAL per position at the master's
open; no scale-in add; proportional scale-out. Leverage is irrelevant to PnL (notional × move).

The legacy 0.5/2/5s latency bands collapse to a single live-book price in REST mode (the columns
are kept for schema/report compatibility, all three carry the same value).
"""
import asyncio
import json
import logging
import time

import websockets

from . import config, rest, ws
from .util import f, now_iso, now_ms

logging.getLogger("websockets").setLevel(logging.CRITICAL)
LAT = config.LATENCIES
PRIMARY = 2.0 if 2.0 in LAT else LAT[len(LAT) // 2]
STALE_MS = 30_000          # a detected fill older than this priced at master px (book unreliable)


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Observer:
    def __init__(self, db, addrs: list, seed_coins: dict):
        self.db = db
        self.addrs = addrs
        self.seed_coins = seed_coins
        self.bbo: dict = {}              # coin -> (bid, ask) current top-of-book (live via WS)
        self.sub_coins: set = set()      # coins we've sent a bbo subscription for (this connection)
        self.open_ep: dict = {}          # (addr,coin) -> position state
        self.last_fill_ms: dict = {}     # addr -> cursor (latest processed fill time)
        self.valid_coins: set = set()
        self.ws = None
        self.stop = False

    def _std_coin(self, coin: str) -> bool:
        """Standard perp coin (we have a real book / can price). Subscribing bbo for an unknown
        coin name (#NNNN builder, xyz:* stock) closes the WS — guard against it."""
        return bool(coin) and (not self.valid_coins or coin in self.valid_coins)

    # -- pricing off the live book -------------------------------------------
    def _fill_px(self, coin, is_buy, maker, fallback):
        ba = self.bbo.get(coin)
        if not ba or not ba[0] or not ba[1]:
            return fallback                       # book not ready -> master px (slippage ~0 anyway)
        bid, ask = ba
        if maker:
            return bid if is_buy else ask         # rest passively on our side of the book
        return ask if is_buy else bid             # take across the spread

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
                "open_maker": False, "open_oid": None,
                "entries": {0.5: e05, 2.0: e2, 5.0: e5} if e2 is not None else None,
                "rem": {0.5: r05 or 0, 2.0: r2 or 0, 5.0: r5 or 0},
                "pnl": {0.5: p05 or 0, 2.0: p2 or 0, 5.0: p5 or 0},
                "entries_ready": ev, "lock": asyncio.Lock(), "mae": mae or 0.0,
                "num_actions": na or 0, "gap": False}
        if rows:
            _log(f"reloaded {len(rows)} open copy positions from db")

    # -- watchlist sync (the copy engine tracks rolling discovery) -----------
    def _reload_targets(self, init=False):
        addrs, seed = load_targets(self.db, config.MAX_TARGETS)
        self.seed_coins = seed
        new = [a for a in addrs if a not in self.last_fill_ms]
        for a in new:
            self.last_fill_ms[a] = now_ms()       # forward-only: don't copy a new wallet's old fills
        dropped = [a for a in self.addrs if a not in addrs]
        self.addrs = addrs
        if init or new or dropped:
            _log(f"watchlist: tracking {len(addrs)} wallets (+{len(new)} new, -{len(dropped)} dropped)")

    # -- WS bbo (pricing only; no user subscriptions) ------------------------
    async def _sub(self, subscription: dict):
        await self.ws.send(ws.sub_msg(subscription))
        await asyncio.sleep(0.05)

    async def subscribe_bbo(self):
        self.sub_coins.clear()                    # subs are gone on a fresh connection — re-add
        coins = {"BTC", "ETH", "SOL", "HYPE"}     # majors warm so most fills price off a live book
        for a in self.addrs:
            coins |= self.seed_coins.get(a, set())
        for (_, c) in self.open_ep:
            coins.add(c)
        for c in coins:
            await self.ensure_coin(c)

    async def ensure_coin(self, coin: str):
        if coin and coin not in self.sub_coins and self._std_coin(coin) and self.ws is not None:
            self.sub_coins.add(coin)
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

    # -- SIGNAL: continuous REST poll of the whole watchlist -----------------
    async def poll_loop(self):
        """Primary engine. Round-robin every watchlist wallet's recent fills (cursor − overlap,
        capped at MAX_BACKFILL_S so a stale cursor can't replay ancient history), replaying each
        through the idempotent process_fill. No fixed period — the REST pacer sets the cadence;
        a full round over ~tens of wallets takes a few seconds. Re-reads the watchlist table
        periodically so rolling discovery flows in without a restart."""
        last_reload = now_ms()
        while not self.stop:
            if now_ms() - last_reload > config.WATCHLIST_RELOAD_S * 1000:
                self._reload_targets()
                last_reload = now_ms()
            for addr in list(self.addrs):
                floor = now_ms() - config.MAX_BACKFILL_S * 1000
                since = max(self.last_fill_ms.get(addr, now_ms()), floor) - config.POLL_OVERLAP_MS
                await self.backfill(addr, since)
            await asyncio.sleep(1)                 # small breath between rounds

    # -- REST poll of targets' resting orders (limit ladders + TP/SL) --------
    async def poll_orders(self):
        """Every ~5s, snapshot each target's open orders via frontendOpenOrders and persist to
        target_orders — their INTENTIONS (maker entries, take-profit/stop levels) ahead of
        execution. Diff-based: orders that vanish flip to 'gone'."""
        while not self.stop:
            for addr in list(self.addrs):
                oo = await asyncio.to_thread(rest.post_soft, {"type": "frontendOpenOrders", "user": addr})
                if not isinstance(oo, list):
                    continue
                seen = set()
                for o in oo:
                    oid = o.get("oid")
                    if oid is None:
                        continue
                    seen.add(oid)
                    self.db.execute(
                        "INSERT INTO target_orders (addr,oid,coin,side,order_type,limit_px,trigger_px,"
                        "sz,reduce_only,is_trigger,status,first_seen,last_seen) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?) "
                        "ON CONFLICT(addr,oid) DO UPDATE SET sz=excluded.sz,limit_px=excluded.limit_px,"
                        "trigger_px=excluded.trigger_px,order_type=excluded.order_type,status='open',"
                        "last_seen=excluded.last_seen",
                        (addr, oid, o.get("coin"), o.get("side"), o.get("orderType"), f(o.get("limitPx")),
                         f(o.get("triggerPx")), f(o.get("sz")), 1 if o.get("reduceOnly") else 0,
                         1 if o.get("isTrigger") else 0, now_iso(), now_iso()))
                for (oid,) in self.db.execute(
                        "SELECT oid FROM target_orders WHERE addr=? AND status='open'", (addr,)).fetchall():
                    if oid not in seen:
                        self.db.execute("UPDATE target_orders SET status='gone', last_seen=? "
                                        "WHERE addr=? AND oid=?", (now_iso(), addr, oid))
                self.db.commit()
                await asyncio.sleep(0.3)            # pace REST across wallets
            await asyncio.sleep(5)

    @staticmethod
    def _quiet(loop, context):
        msg = str(context.get("exception") or context.get("message"))
        if "SSL" in msg or "closed" in msg.lower():
            return
        loop.default_exception_handler(context)

    # -- run: REST signal tasks + a WS connection for bbo pricing ------------
    async def run(self):
        asyncio.get_event_loop().set_exception_handler(self._quiet)
        self.valid_coins = rest.perp_universe()
        _log(f"perp universe: {len(self.valid_coins)} coins (bbo subs guarded)")
        self._reload_open()                        # restore open copies (restart-safe)
        self._load_cursors()                       # restore per-wallet REST cursors
        self._reload_targets(init=True)            # load watchlist + forward-only cursors for new
        asyncio.create_task(self._announce())
        asyncio.create_task(self.poll_orders())    # resting-order intentions (REST)
        asyncio.create_task(self.poll_loop())      # SIGNAL: continuous REST poll (the engine)
        while not self.stop:                        # WS: PRICING only (per-coin bbo, no user subs)
            try:
                async with websockets.connect(config.WS_URL, ping_interval=None, max_size=None) as conn:
                    self.ws = conn
                    hb = asyncio.create_task(self.heartbeat())
                    asyncio.create_task(self.subscribe_bbo())
                    _log(f"bbo ws connected ({len(self.addrs)} wallets polled, {len(self.open_ep)} open copies)")
                    async for raw in conn:
                        self.on_message(raw)
                    hb.cancel()
            except Exception as exc:  # noqa: BLE001
                self.ws = None
                _log(f"bbo ws error: {exc}; reconnecting in 3s")
                await asyncio.sleep(3)

    # -- WS message router (bbo only) ----------------------------------------
    def on_message(self, raw: str):
        m = json.loads(raw)
        if m.get("channel") == "bbo":
            self.on_bbo(m.get("data", {}))

    def on_bbo(self, d: dict):
        coin = d.get("coin")
        ba = d.get("bbo") or []
        if not coin or len(ba) < 2 or not ba[0] or not ba[1]:
            return
        bid, ask = f(ba[0].get("px")), f(ba[1].get("px"))
        self.bbo[coin] = (bid, ask)
        mid = (bid + ask) / 2
        for (a, c), ep in self.open_ep.items():    # track worst adverse excursion while open
            if c == coin and ep["master_open_px"]:
                adv = ((ep["master_open_px"] - mid) if ep["side"] == "long"
                       else (mid - ep["master_open_px"])) / ep["master_open_px"]
                ep["mae"] = max(ep.get("mae", 0.0), adv)

    def _record_fill(self, addr, x) -> bool:
        """Insert the fill; True if NEW, False if this tid was already seen (dedup). This is what
        makes process_fill idempotent so overlapping poll rounds can't double-copy."""
        liq = x.get("liquidation")
        cur = self.db.execute(
            "INSERT OR IGNORE INTO live_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (addr, x.get("tid"), x.get("time"), now_ms(), x.get("coin"), x.get("side"), x.get("dir"),
             f(x.get("px")), f(x.get("sz")), f(x.get("closedPnl")), f(x.get("fee")),
             1 if x.get("crossed") else 0, 1 if liq else 0, (liq or {}).get("method"), x.get("hash")))
        return cur.rowcount > 0

    # -- core: master fills -> copy actions ----------------------------------
    def process_fill(self, addr: str, x: dict):
        coin = x.get("coin")
        if not coin or x.get("tid") is None:
            return
        if not self._record_fill(addr, x):
            return                          # already processed this tid (poll overlap) — idempotent
        self.last_fill_ms[addr] = max(self.last_fill_ms.get(addr, 0), x["time"])  # advance cursor
        t = x["time"]
        sz = f(x.get("sz"))
        signed = sz if x.get("side") == "B" else -sz
        pos0 = f(x.get("startPosition"))
        pos1 = pos0 + signed
        px = f(x.get("px"))
        key = (addr, coin)
        liq = bool(x.get("liquidation"))
        maker = not bool(x.get("crossed"))   # crossed=False -> a resting-limit (maker) fill
        oid = x.get("oid")
        if coin not in self.sub_coins:
            asyncio.create_task(self.ensure_coin(coin))   # make sure we have a book to price this

        ep = self.open_ep.get(key)
        if ep is None:
            if abs(pos0) < config.FLAT and abs(pos1) >= config.FLAT:
                self._open_position(addr, coin, t, px, pos1, maker, oid)
            return
        ep["master_peak"] = max(ep["master_peak"], abs(pos1))
        if liq:
            ep["was_liq"] = 1
        if abs(pos1) >= abs(pos0) - config.FLAT and not abs(pos1) < config.FLAT:
            self._record_action(ep, addr, coin, t, "add", maker, oid, px, signed, pos1, 0.0, px, 0.0, 0.0)
        else:
            asyncio.create_task(self._apply_reduce(addr, coin, ep, t, px, signed, pos1,
                                                   closing=abs(pos1) < config.FLAT, liq=liq, maker=maker, oid=oid))

    def _open_position(self, addr, coin, t, px, pos1, maker, oid):
        if not self._std_coin(coin):
            return              # only copy standard-universe coins (skip stock/builder perps)
        side = "long" if pos1 > 0 else "short"
        cur = self.db.execute(
            "INSERT INTO copy_position (addr,coin,side,status,master_open_ms,master_open_px,"
            "master_peak_sz,our_notional,opened_at,num_actions) VALUES (?,?,?,'open',?,?,?,?,?,0)",
            (addr, coin, side, t, px, abs(pos1), config.NOTIONAL, now_iso()))
        ep = {"pos_id": cur.lastrowid, "side": side, "sign": 1 if side == "long" else -1,
              "master_open_ms": t, "master_open_px": px, "master_peak": abs(pos1),
              "open_maker": maker, "open_oid": oid, "entries": None, "rem": {L: 0.0 for L in LAT},
              "pnl": {L: 0.0 for L in LAT}, "entries_ready": asyncio.Event(),
              "lock": asyncio.Lock(), "mae": 0.0, "num_actions": 0, "gap": False}
        self.open_ep[(addr, coin)] = ep
        asyncio.create_task(self._resolve_entry(addr, coin, ep, t, px))

    async def _resolve_entry(self, addr, coin, ep, t, master_px):
        is_buy = ep["side"] == "long"                # opening a long => we buy
        stale = (now_ms() - t) > STALE_MS            # backfilled-late: book is no longer the fill's
        px = master_px if stale else self._fill_px(coin, is_buy, ep["open_maker"], master_px)
        chase = (px - master_px) / master_px * 1e4 * ep["sign"]   # bps worse than master (+ = worse)
        # Chase guard (UI-tunable): on a spike the master eats the book with size and our taker fill
        # lands worse — past the threshold we DON'T chase. Taker opens only; maker rests passively.
        if (config.MAX_ENTRY_CHASE_PCT is not None and not ep["open_maker"]
                and chase > config.MAX_ENTRY_CHASE_PCT * 100):
            self.db.execute("DELETE FROM copy_position WHERE pos_id=?", (ep["pos_id"],))
            self.db.commit()
            self.open_ep.pop((addr, coin), None)
            _log(f"SKIP {addr[:10]} {coin} open: chase {chase:+.1f}bps > {config.MAX_ENTRY_CHASE_PCT}% (spike)")
            return
        entries = {L: px for L in LAT}               # one live-book price (REST single latency)
        ep["entries"] = entries
        ep["rem"] = {L: config.NOTIONAL / px for L in LAT}
        ep["entries_ready"].set()
        self.db.execute(
            "UPDATE copy_position SET entry_05=?,entry_2=?,entry_5=?,rem_05=?,rem_2=?,rem_5=? WHERE pos_id=?",
            (px, px, px, ep["rem"][0.5], ep["rem"][2.0], ep["rem"][5.0], ep["pos_id"]))
        msz = ep["master_peak"] * ep["sign"]         # master's signed open size / position-after
        self._record_action(ep, addr, coin, t, "open", ep["open_maker"], ep["open_oid"], master_px,
                            msz, msz, ep["rem"][PRIMARY] * ep["sign"], px, 0.0, chase)
        self.db.commit()

    async def _apply_reduce(self, addr, coin, ep, t, master_px, signed, pos1, closing, liq, maker, oid=None, gap=False):
        async with ep["lock"]:
            try:
                await asyncio.wait_for(ep["entries_ready"].wait(), timeout=12)
            except asyncio.TimeoutError:
                pass
            if ep.get("entries") is None:
                return
            is_buy = ep["side"] == "short"           # closing a long => sell; closing a short => buy
            stale = (now_ms() - t) > STALE_MS
            exit_px = master_px if stale else self._fill_px(coin, is_buy, maker, master_px)
            target_frac = 0.0 if closing else abs(pos1) / max(ep["master_peak"], 1e-12)
            prim = {"qty": 0.0, "px": exit_px, "pnl": 0.0}
            for L in LAT:
                qty_total = config.NOTIONAL / ep["entries"][L]
                close_qty = max(0.0, ep["rem"][L] - qty_total * target_frac)
                pnl = close_qty * (exit_px - ep["entries"][L]) * ep["sign"]
                ep["pnl"][L] += pnl
                ep["rem"][L] -= close_qty
                if L == PRIMARY:
                    prim = {"qty": close_qty, "px": exit_px, "pnl": pnl}
            slip = (master_px - prim["px"]) / master_px * 1e4 * ep["sign"]
            action = "close" if closing else "reduce"
            self._record_action(ep, addr, coin, t, action, maker, oid, master_px, signed, pos1,
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
                _log(f"CLOSED {addr[:10]} {coin} {ep['side']} pnl={pct:+.2f}% "
                     f"mae={ep['mae']*100:.2f}% actions={ep['num_actions']}{' [GAP]' if gap else ''}"
                     f"{' [LIQ]' if liq else ''}")

    def _record_action(self, ep, addr, coin, t, action, maker, oid, master_px, sz_delta, pos_after,
                       our_qty_delta, our_px, realized, slip):
        ep["num_actions"] += 1
        self.db.execute(
            "INSERT INTO copy_action (pos_id,addr,coin,ts,recv_ms,action,maker,master_oid,master_px,"
            "master_sz_delta,master_pos_after,our_qty_delta,our_px,realized_pnl,slippage_bps) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ep["pos_id"], addr, coin, t, now_ms(), action, 1 if maker else 0, oid, master_px, sz_delta,
             pos_after, our_qty_delta, our_px, realized, slip))
        self.db.execute("UPDATE copy_position SET num_actions=?, master_peak_sz=? WHERE pos_id=?",
                        (ep["num_actions"], ep["master_peak"], ep["pos_id"]))

    # -- per-wallet REST cursor ----------------------------------------------
    def _load_cursors(self):
        for addr, lfm in self.db.execute("SELECT addr, last_fill_ms FROM wallet_cursor").fetchall():
            if lfm:
                self.last_fill_ms[addr] = max(self.last_fill_ms.get(addr, 0), lfm)

    def _save_cursor(self, addr):
        lfm = self.last_fill_ms.get(addr)
        if lfm:
            self.db.execute(
                "INSERT INTO wallet_cursor (addr,last_fill_ms,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(addr) DO UPDATE SET last_fill_ms=excluded.last_fill_ms,updated_at=excluded.updated_at",
                (addr, lfm, now_iso()))

    async def backfill(self, addr: str, since: int):
        """REST-fetch the wallet's fills since `since` and replay through the idempotent
        process_fill (dedup by tid). This is the signal path AND the catch-up path — same code."""
        page = await asyncio.to_thread(
            rest.post_soft, {"type": "userFillsByTime", "user": addr, "startTime": int(max(0, since))})
        if isinstance(page, list) and page:
            for x in sorted(page, key=lambda fl: fl["time"]):
                self.process_fill(addr, x)
            self.db.commit()
        self._save_cursor(addr)
        self.db.commit()


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
        "sum(pnl_2) p2, avg(pnl_2/our_notional)*100 avg2, "
        "avg(num_actions) acts FROM copy_position WHERE status!='open' GROUP BY addr ORDER BY p2 DESC").fetchall()
    op = db.execute("SELECT count(*) FROM copy_position WHERE status='open'").fetchone()[0]
    if not rows:
        print(f"no closed copy positions yet ({op} open). Observer needs a target to complete a round-trip.")
        return
    print(f"\nPAPER COPY RESULTS (notional ${config.NOTIONAL:g}/position; live-book pricing)  [{op} still open]\n")
    hdr = f"{'addr':42} {'closed':>6} {'win%':>5} {'pnl$':>10} {'avg%':>7} {'acts':>5}"
    print(hdr); print("-" * len(hdr))
    for addr, n, winr, p2, avg2, acts in rows:
        print(f"{addr:42} {n:>6} {winr*100:>4.0f}% {p2:>10,.1f} {avg2:>+6.2f}% {acts:>5.1f}")
    print("\n(pnl = total $ at our copy fills vs the master; fixed ${:g} notional/position)".format(config.NOTIONAL))
