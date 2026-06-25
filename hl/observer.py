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
STALE_MS = 30_000          # a detected fill older than this priced at master px (book unreliable)


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Observer:
    def __init__(self, db, addrs: list, seed_coins: dict, top_n: int = None, min_score: float = None,
                 margin_pct: float = None, add_margin_pct: float = None):
        self.db = db
        self.addrs = addrs
        self.seed_coins = seed_coins
        self.top_n = top_n or config.MAX_TARGETS    # hard cap on followed wallets (REST-rate ceiling)
        self.min_score = config.MIN_FOLLOW_SCORE if min_score is None else min_score  # quality threshold
        # strategy sizing — dynamic (UI-tunable): margin on open vs each follow-on add, as % of available
        self.margin_pct = config.MARGIN_PCT if margin_pct is None else margin_pct
        self.add_margin_pct = config.ADD_MARGIN_PCT if add_margin_pct is None else add_margin_pct
        self.bbo: dict = {}              # coin -> (bid, ask) current top-of-book (any source)
        self.sub_coins: set = set()      # crypto coins we've sent a WS bbo subscription for
        self.stock_coins: set = set()    # builder/stock coins we price via REST l2Book poll
        self.open_ep: dict = {}          # (addr,coin) -> position state
        self.last_fill_ms: dict = {}     # addr -> cursor (latest processed fill time)
        self.valid_coins: set = set()    # COPYABLE universe (crypto perps + transparent builder)
        self.crypto_coins: set = set()   # standard crypto perps (these price via WS bbo)
        self.balance = config.INITIAL_BALANCE   # paper account realized equity (persisted)
        self.acct_lock = asyncio.Lock()  # serialize margin allocation across concurrent opens
        self.ws = None
        self.stop = False

    # -- paper account ------------------------------------------------------
    def _available(self) -> float:
        """Balance not currently tied up as isolated margin (margin scales with rem_size/size as a
        position is partially closed)."""
        locked = self.db.execute(
            "SELECT COALESCE(SUM(margin * rem_size / size),0) FROM copy_position "
            "WHERE status='open' AND size>0").fetchone()[0]
        return self.balance - (locked or 0.0)

    def _load_account(self):
        row = self.db.execute("SELECT balance FROM copy_account WHERE id=1").fetchone()
        if row:
            self.balance = row[0]
        else:
            self.db.execute("INSERT INTO copy_account (id,initial_balance,balance,updated_at) "
                            "VALUES (1,?,?,?)", (config.INITIAL_BALANCE, config.INITIAL_BALANCE, now_iso()))
            self.db.commit()
        _log(f"account: balance ${self.balance:,.2f} / available ${self._available():,.2f}")

    def _save_account(self):
        self.db.execute("UPDATE copy_account SET balance=?, updated_at=? WHERE id=1",
                        (self.balance, now_iso()))

    def _target_leverage(self, addr, coin):
        """The master's leverage for this coin (from clearinghouseState), capped to MAX_LEV. Falls
        back to MAX_LEV if it can't be read (mirror-capped intent)."""
        cs = rest.clearinghouse_state(addr)
        lev = None
        if isinstance(cs, dict):
            for ap in cs.get("assetPositions", []):
                pos = ap.get("position", {})
                if pos.get("coin") == coin:
                    lev = f((pos.get("leverage") or {}).get("value"))
                    break
        return max(1.0, min(lev or config.MAX_LEV, config.MAX_LEV))

    def _copyable(self, coin: str) -> bool:
        """A coin we can copy + price: crypto perp, or transparent builder perp (stock/commodity).
        Opaque/unknown names are skipped (and subscribing their bbo would close the WS anyway)."""
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
            "SELECT pos_id,addr,coin,side,master_open_ms,master_open_px,master_peak_sz,leverage,"
            "margin,notional,entry_px,size,rem_size,liq_px,realized_pnl,add_count,mae_pct,num_actions "
            "FROM copy_position WHERE status='open'").fetchall()
        for r in rows:
            (pid, addr, coin, side, mo, mpx, peak, lev, mgn, notl, epx, sz, rem, liq, rpnl, adds, mae, na) = r
            ev = asyncio.Event()
            if epx is not None:
                ev.set()
            self.open_ep[(addr, coin)] = {
                "pos_id": pid, "side": side, "sign": 1 if side == "long" else -1,
                "master_open_ms": mo, "master_open_px": mpx, "master_peak": peak or 0.0,
                "open_maker": False, "open_oid": None, "leverage": lev or 0.0, "margin": mgn or 0.0,
                "notional": notl or 0.0, "entry_px": epx, "size": sz or 0.0, "rem_size": rem or 0.0,
                "liq_px": liq or 0.0, "realized_pnl": rpnl or 0.0, "add_count": adds or 0,
                "entries_ready": ev, "lock": asyncio.Lock(), "mae": mae or 0.0, "num_actions": na or 0,
                "gap": False}
        if rows:
            _log(f"reloaded {len(rows)} open copy positions from db")

    # -- watchlist sync (the copy engine tracks rolling discovery) -----------
    def _reload_targets(self, init=False):
        addrs, seed = load_targets(self.db, self.top_n, self.min_score)
        self.seed_coins = seed
        # SAFEGUARD: never stop polling a wallet we still hold a copy on, even if it fell off the
        # watchlist this scan — else we'd miss its exit and dumb-hold the position to liquidation.
        held_off = [a for a in {addr for (addr, _) in self.open_ep} if a not in addrs]
        addrs = addrs + held_off
        new = [a for a in addrs if a not in self.last_fill_ms]
        for a in new:
            self.last_fill_ms[a] = now_ms()       # forward-only: don't copy a new wallet's old fills
        dropped = [a for a in self.addrs if a not in addrs]
        self.addrs = addrs
        if init or new or dropped or held_off:
            extra = f", {len(held_off)} held-off-list" if held_off else ""
            _log(f"watchlist: tracking {len(addrs)} wallets (+{len(new)} new, -{len(dropped)} dropped{extra})")

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
        """Route a coin to its pricing source: crypto -> WS bbo subscription; transparent builder
        (stock/commodity) -> the REST l2Book poll set (WS bbo can't serve builder dexes)."""
        if not coin or coin in self.sub_coins or coin in self.stock_coins:
            return
        if coin in self.crypto_coins:
            if self.ws is not None:
                self.sub_coins.add(coin)
                try:
                    await self._sub(ws.bbo(coin))
                except Exception:  # noqa: BLE001
                    self.sub_coins.discard(coin)
        elif self._copyable(coin):                 # builder/stock perp -> REST l2Book pricing
            self.stock_coins.add(coin)

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

    async def prune_live_fills(self):
        """Keep live_fills bounded on disk. tid-dedup only needs ~MAX_BACKFILL_S of history (the
        poll cursor never re-fetches older than that), so deleting rows past the retention window
        can't cause re-processing — the rest is just audit. Runs at startup then every 6h."""
        while not self.stop:
            cutoff = now_ms() - config.LIVE_FILLS_RETENTION_DAYS * 86400_000
            n = self.db.execute("DELETE FROM live_fills WHERE time_ms < ?", (cutoff,)).rowcount
            self.db.commit()
            if n:
                _log(f"pruned {n} live_fills older than {config.LIVE_FILLS_RETENTION_DAYS}d")
            await asyncio.sleep(6 * 3600)

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

    # -- PRICING for builder/stock perps (WS bbo can't serve builder dexes) ---
    async def poll_stock_books(self):
        """Keep self.bbo fresh for builder/stock coins we're tracking via REST l2Book (best bid/ask).
        Only polls coins we've actually seen a fill on (added to stock_coins on first sight), so the
        cost is a handful of calls — and zero when no stock positions are in play."""
        while not self.stop:
            for coin in list(self.stock_coins):
                ba = await asyncio.to_thread(rest.book_top, coin)
                if ba:
                    self.bbo[coin] = ba
                    mid = (ba[0] + ba[1]) / 2
                    for (a, c), ep in self.open_ep.items():      # track adverse excursion while open
                        if c == coin and ep["master_open_px"]:
                            adv = ((ep["master_open_px"] - mid) if ep["side"] == "long"
                                   else (mid - ep["master_open_px"])) / ep["master_open_px"]
                            ep["mae"] = max(ep.get("mae", 0.0), adv)
                    self._maybe_liquidate(coin, mid)             # isolated stop-out if liq_px crossed
            await asyncio.sleep(2 if self.stock_coins else 5)

    @staticmethod
    def _quiet(loop, context):
        msg = str(context.get("exception") or context.get("message"))
        if "SSL" in msg or "closed" in msg.lower():
            return
        loop.default_exception_handler(context)

    # -- run: REST signal tasks + a WS connection for bbo pricing ------------
    async def run(self):
        asyncio.get_event_loop().set_exception_handler(self._quiet)
        self.crypto_coins = rest.perp_universe()           # price via WS bbo
        if not self.crypto_coins:                          # load-bearing: empty would make crypto
            raise RuntimeError("perp_universe() empty — refusing a crypto-less copy universe")
        self.valid_coins = self.crypto_coins | rest.builder_universe()  # + transparent stocks (l2Book)
        _log(f"universe: {len(self.crypto_coins)} crypto (WS bbo) + "
             f"{len(self.valid_coins) - len(self.crypto_coins)} builder/stock (REST l2Book)")
        self._load_account()                       # restore paper account balance (restart-safe)
        self._reload_open()                        # restore open copies (restart-safe)
        for (_, coin) in self.open_ep:             # reloaded stock positions need REST book polling
            if coin not in self.crypto_coins and self._copyable(coin):
                self.stock_coins.add(coin)
        self._load_cursors()                       # restore per-wallet REST cursors
        self._reload_targets(init=True)            # load watchlist + forward-only cursors for new
        asyncio.create_task(self._announce())
        asyncio.create_task(self.prune_live_fills())  # bound live_fills on disk (retention)
        asyncio.create_task(self.poll_orders())    # resting-order intentions (REST)
        asyncio.create_task(self.poll_stock_books())  # stock/commodity top-of-book (REST l2Book)
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
        self._maybe_liquidate(coin, mid)           # isolated stop-out if price crossed liq_px

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
        if coin not in self.sub_coins and coin not in self.stock_coins:
            asyncio.create_task(self.ensure_coin(coin))   # route to its pricing source (bbo / l2Book)

        ep = self.open_ep.get(key)
        if ep is None:
            if abs(pos0) < config.FLAT and abs(pos1) >= config.FLAT:
                self._open_position(addr, coin, t, px, pos1, maker, oid)
            return
        ep["master_peak"] = max(ep["master_peak"], abs(pos1))
        if liq:
            ep["was_liq"] = 1
        if abs(pos1) >= abs(pos0) - config.FLAT and not abs(pos1) < config.FLAT:
            asyncio.create_task(self._apply_add(addr, coin, ep, t, px, signed, pos1, maker, oid))
        else:
            asyncio.create_task(self._apply_reduce(addr, coin, ep, t, px, signed, pos1,
                                                   closing=abs(pos1) < config.FLAT, liq=liq, maker=maker, oid=oid))

    def _open_position(self, addr, coin, t, px, pos1, maker, oid):
        if not self._copyable(coin):
            return              # copy crypto + transparent builder (stocks); skip opaque/unknown
        side = "long" if pos1 > 0 else "short"
        cur = self.db.execute(
            "INSERT INTO copy_position (addr,coin,side,status,master_open_ms,master_open_px,"
            "master_peak_sz,opened_at,num_actions) VALUES (?,?,?,'open',?,?,?,?,0)",
            (addr, coin, side, t, px, abs(pos1), now_iso()))
        ep = {"pos_id": cur.lastrowid, "side": side, "sign": 1 if side == "long" else -1,
              "master_open_ms": t, "master_open_px": px, "master_peak": abs(pos1),
              "open_maker": maker, "open_oid": oid, "leverage": 0.0, "margin": 0.0, "notional": 0.0,
              "entry_px": None, "size": 0.0, "rem_size": 0.0, "liq_px": 0.0, "realized_pnl": 0.0,
              "add_count": 0, "entries_ready": asyncio.Event(), "lock": asyncio.Lock(), "mae": 0.0,
              "num_actions": 0, "gap": False}
        self.open_ep[(addr, coin)] = ep
        asyncio.create_task(self._resolve_entry(addr, coin, ep, t, px))

    async def _resolve_entry(self, addr, coin, ep, t, master_px):
        is_buy = ep["side"] == "long"                # opening a long => we buy
        stale = (now_ms() - t) > STALE_MS            # backfilled-late: book is no longer the fill's
        px = master_px if stale else self._fill_px(coin, is_buy, ep["open_maker"], master_px)
        chase = (px - master_px) / master_px * 1e4 * ep["sign"]   # bps worse than master (+ = worse)
        if (config.MAX_ENTRY_CHASE_PCT is not None and not ep["open_maker"]
                and chase > config.MAX_ENTRY_CHASE_PCT * 100):     # spike too far past master -> skip
            self.db.execute("DELETE FROM copy_position WHERE pos_id=?", (ep["pos_id"],))
            self.db.commit()
            self.open_ep.pop((addr, coin), None)
            _log(f"SKIP {addr[:10]} {coin} open: chase {chase:+.1f}bps > {config.MAX_ENTRY_CHASE_PCT}% (spike)")
            return
        lev = await asyncio.to_thread(self._target_leverage, addr, coin)   # master's leverage, capped
        async with self.acct_lock:                   # serialize margin allocation across opens
            margin = max(0.0, self._available() * self.margin_pct)
            notional = margin * lev
            size = notional / px if px else 0.0
            liq_px = px * (1 - 1.0 / lev) if is_buy else px * (1 + 1.0 / lev)  # isolated: loss = margin
            ep.update(leverage=lev, margin=margin, notional=notional, entry_px=px,
                      size=size, rem_size=size, liq_px=liq_px)
            self.db.execute(
                "UPDATE copy_position SET leverage=?,margin=?,notional=?,entry_px=?,size=?,rem_size=?,"
                "liq_px=? WHERE pos_id=?", (lev, margin, notional, px, size, size, liq_px, ep["pos_id"]))
            self.db.commit()
        ep["entries_ready"].set()
        msz = ep["master_peak"] * ep["sign"]
        self._record_action(ep, addr, coin, t, "open", ep["open_maker"], ep["open_oid"], master_px,
                            msz, msz, size * ep["sign"], px, 0.0, chase)
        self.db.commit()
        _log(f"OPEN {addr[:10]} {coin} {ep['side']} ${margin:,.0f}m {lev:.0f}x notl=${notional:,.0f} "
             f"@ {px:g} liq={liq_px:g} avail=${self._available():,.0f}")

    async def _apply_add(self, addr, coin, ep, t, master_px, signed, pos1, maker, oid):
        """Master scaled in -> we follow (average down/up) up to MAX_ADDS, each add committing
        another MARGIN_PCT of available at the current price; the avg entry + liq_px recompute.
        Past the cap we record his add but don't follow (the delta-based exit still mirrors him)."""
        async with ep["lock"]:
            try:
                await asyncio.wait_for(ep["entries_ready"].wait(), timeout=12)
            except asyncio.TimeoutError:
                pass
            if ep.get("entry_px") is None or (addr, coin) not in self.open_ep:
                return
            if ep["add_count"] >= config.MAX_ADDS:    # cap reached — observe but don't follow
                self._record_action(ep, addr, coin, t, "add", maker, oid, master_px, signed, pos1,
                                    0.0, master_px, 0.0, 0.0)
                self.db.commit()
                return
            is_buy = ep["side"] == "long"             # adding to a long => buy more
            stale = (now_ms() - t) > STALE_MS
            px = master_px if stale else self._fill_px(coin, is_buy, maker, master_px)
            lev = ep["leverage"]
            async with self.acct_lock:
                add_margin = max(0.0, self._available() * self.add_margin_pct)
            add_size = (add_margin * lev / px) if px else 0.0
            new_size = ep["rem_size"] + add_size
            ep["entry_px"] = ((ep["rem_size"] * ep["entry_px"] + add_size * px) / new_size
                              if new_size else px)    # size-weighted average entry
            ep["rem_size"] = new_size
            ep["size"] += add_size
            ep["margin"] += add_margin
            ep["notional"] += add_margin * lev
            ep["liq_px"] = ep["entry_px"] * (1 - 1.0 / lev) if is_buy else ep["entry_px"] * (1 + 1.0 / lev)
            ep["add_count"] += 1
            slip = (px - master_px) / master_px * 1e4 * ep["sign"] if master_px else 0.0
            self._record_action(ep, addr, coin, t, "add", maker, oid, master_px, signed, pos1,
                                add_size * ep["sign"], px, 0.0, slip)
            self.db.execute(
                "UPDATE copy_position SET margin=?,notional=?,entry_px=?,size=?,rem_size=?,liq_px=?,"
                "add_count=? WHERE pos_id=?", (ep["margin"], ep["notional"], ep["entry_px"], ep["size"],
                ep["rem_size"], ep["liq_px"], ep["add_count"], ep["pos_id"]))
            self.db.commit()
            _log(f"ADD {addr[:10]} {coin} #{ep['add_count']} +${add_margin:,.0f}m avg={ep['entry_px']:g} "
                 f"liq={ep['liq_px']:g} avail=${self._available():,.0f}")

    async def _apply_reduce(self, addr, coin, ep, t, master_px, signed, pos1, closing, liq, maker,
                            oid=None, gap=False, forced_px=None):
        async with ep["lock"]:
            try:
                await asyncio.wait_for(ep["entries_ready"].wait(), timeout=12)
            except asyncio.TimeoutError:
                pass
            if ep.get("entry_px") is None or (addr, coin) not in self.open_ep:
                return
            is_buy = ep["side"] == "short"           # closing a long => sell; closing a short => buy
            stale = (now_ms() - t) > STALE_MS
            exit_px = (forced_px if forced_px is not None
                       else master_px if stale else self._fill_px(coin, is_buy, maker, master_px))
            # delta-based: close the SAME fraction of our position the master just closed of his —
            # correct for any build-up (adds we followed, adds we skipped past the cap, or none).
            pos0 = pos1 - signed
            reduce_frac = (1.0 if closing or abs(pos0) < config.FLAT
                           else max(0.0, min(1.0, (abs(pos0) - abs(pos1)) / abs(pos0))))
            close_size = ep["rem_size"] * reduce_frac
            pnl = close_size * (exit_px - ep["entry_px"]) * ep["sign"]
            ep["rem_size"] -= close_size
            ep["realized_pnl"] += pnl
            self.balance += pnl                       # realize into the paper account
            slip = (master_px - exit_px) / master_px * 1e4 * ep["sign"] if master_px else 0.0
            action = "close" if closing else "reduce"
            self._record_action(ep, addr, coin, t, action, maker, oid, master_px, signed, pos1,
                                -close_size * ep["sign"], exit_px, pnl, slip)
            status = ("liquidated" if (closing and liq) else "gap_closed" if (closing and gap)
                      else "closed" if closing else "open")
            self.db.execute(
                "UPDATE copy_position SET rem_size=?,realized_pnl=?,mae_pct=?,was_liq=?,status=?,"
                "closed_at=? WHERE pos_id=?", (ep["rem_size"], ep["realized_pnl"], ep["mae"],
                ep.get("was_liq", 0), status, now_iso() if closing else None, ep["pos_id"]))
            self._save_account()
            self.db.commit()
            if closing:
                self.open_ep.pop((addr, coin), None)
                ret = (ep["realized_pnl"] / ep["margin"] * 100) if ep["margin"] else 0.0
                _log(f"CLOSED {addr[:10]} {coin} {ep['side']} pnl=${ep['realized_pnl']:+,.1f} "
                     f"({ret:+.0f}% on margin) bal=${self.balance:,.0f}"
                     f"{' [GAP]' if gap else ''}{' [LIQ]' if liq else ''}")

    async def _liquidate(self, addr, coin, ep):
        if ep.get("liquidating") or (addr, coin) not in self.open_ep:
            return
        ep["liquidating"] = True
        ep["was_liq"] = 1                             # isolated stop-out at liq_px: loss = remaining margin
        await self._apply_reduce(addr, coin, ep, now_ms(), ep["liq_px"], 0.0, 0.0,
                                 closing=True, liq=True, maker=False, forced_px=ep["liq_px"])

    def _maybe_liquidate(self, coin, mid):
        for (a, c), ep in list(self.open_ep.items()):
            if c == coin and ep.get("liq_px") and ep["rem_size"] > config.FLAT and not ep.get("liquidating"):
                hit = mid <= ep["liq_px"] if ep["side"] == "long" else mid >= ep["liq_px"]
                if hit:
                    asyncio.create_task(self._liquidate(a, coin, ep))

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
def load_targets(db, n: int, min_score: float = 0.0):
    addrs = [r[0] for r in db.execute(
        "SELECT w.addr FROM watchlist w LEFT JOIN target_controls c ON c.addr=w.addr "
        "WHERE COALESCE(c.enabled,1)=1 AND w.score >= ? ORDER BY w.rank LIMIT ?",
        (min_score, n)).fetchall()]
    seed = {a: {r[0] for r in db.execute("SELECT DISTINCT coin FROM episode WHERE addr=?", (a,)).fetchall()}
            for a in addrs}
    return addrs, seed


# -------------------------------------------------------------------------- report
def report(db) -> None:
    acct = db.execute("SELECT initial_balance, balance FROM copy_account WHERE id=1").fetchone()
    init, bal = acct if acct else (config.INITIAL_BALANCE, config.INITIAL_BALANCE)
    liqd = db.execute("SELECT count(*) FROM copy_position WHERE status='liquidated'").fetchone()[0]
    # OPEN positions: fetch live top-of-book per coin -> mark-to-market unrealized + true equity
    opens = db.execute("SELECT addr,coin,side,entry_px,rem_size,margin,size,realized_pnl "
                       "FROM copy_position WHERE status='open' AND size>0").fetchall()
    px, unreal, locked, rows_open = {}, 0.0, 0.0, []
    for a, coin, side, entry, rem, mgn, size, rpnl in opens:
        if coin not in px:
            ba = rest.book_top(coin)
            px[coin] = ((ba[0] + ba[1]) / 2) if ba else entry
        u = rem * (px[coin] - entry) * (1 if side == "long" else -1)
        cur_mgn = mgn * rem / size
        unreal += u; locked += cur_mgn
        rows_open.append((a[:10], coin, side, cur_mgn, rpnl, u))
    equity = bal + unreal
    print(f"\nPAPER ACCOUNT  equity ${equity:,.2f}  ({equity/init-1:+.2%} vs ${init:,.0f} start)")
    print(f"  realized balance ${bal:,.2f} | unrealized ${unreal:+,.2f} | available ${bal-locked:,.2f} | "
          f"{len(opens)} open, {liqd} liquidated\n")
    rows = db.execute(
        "SELECT addr, count(*) n, sum(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END)*1.0/count(*) winr, "
        "sum(realized_pnl) pnl, sum(CASE WHEN status='liquidated' THEN 1 ELSE 0 END) liq "
        "FROM copy_position WHERE status!='open' GROUP BY addr ORDER BY pnl DESC").fetchall()
    if rows:
        print("CLOSED (realized PnL, per wallet):")
        h = f"  {'addr':42} {'closed':>6} {'win%':>5} {'pnl$':>9} {'liq':>4}"
        print(h + "\n  " + "-" * (len(h) - 2))
        for addr, n, winr, pnl, liq in rows:
            print(f"  {addr:42} {n:>6} {winr*100:>4.0f}% {pnl:>+9,.1f} {liq:>4}")
    if rows_open:
        print("\nOPEN (mark-to-market):")
        h = f"  {'addr':10} {'coin':12} {'side':5} {'margin$':>8} {'real$':>7} {'unreal$':>9} {'tot%mgn':>8}"
        print(h + "\n  " + "-" * (len(h) - 2))
        for a, coin, side, cur_mgn, rpnl, u in sorted(rows_open, key=lambda r: -(r[4] + r[5])):
            tot = rpnl + u
            print(f"  {a:10} {coin:12} {side:5} {cur_mgn:>8.0f} {rpnl:>+7.1f} {u:>+9.1f} "
                  f"{(tot/cur_mgn*100 if cur_mgn else 0):>+7.0f}%")
    print(f"\n(margin {config.MARGIN_PCT*100:g}% open / {config.ADD_MARGIN_PCT*100:g}% per add of available, "
          f"master leverage capped {config.MAX_LEV:g}x, isolated, no stop)")
