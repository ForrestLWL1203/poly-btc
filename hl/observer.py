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
import os
import time

import websockets

from . import config, rest, volatility, ws
from .util import f, now_iso, now_ms

logging.getLogger("websockets").setLevel(logging.CRITICAL)
STALE_MS = 30_000          # a detected fill older than this priced at master px (book unreliable)


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Observer:
    def __init__(self, db, addrs: list, seed_coins: dict, top_n: int = None, min_score: float = None,
                 add_margin_pct: float = None):
        self.db = db
        self.addrs = addrs
        self.seed_coins = seed_coins
        self.top_n = top_n or config.MAX_TARGETS    # hard cap on followed wallets (REST-rate ceiling)
        self.min_score = config.MIN_FOLLOW_SCORE if min_score is None else min_score  # quality threshold
        # v8 sizing (UI-tunable): 3 σ-tiers, each with margin% + lev cap; leverage scales ∝1/σ within the
        # tier (full at σ=stable_sigma_max), capped by the tier. margin = available × <tier>_margin_pct.
        self.add_margin_pct = config.ADD_MARGIN_PCT if add_margin_pct is None else add_margin_pct
        self.stable_sigma_max = config.STABLE_SIGMA_MAX   # σ≤this → stable tier (also lev-formula σ ref)
        self.high_sigma_min = config.HIGH_SIGMA_MIN       # σ≥this → high-vol tier; between → mid tier
        self.tier_margin = {"stable": config.STABLE_MARGIN_PCT, "mid": config.MID_MARGIN_PCT, "high": config.HIGH_MARGIN_PCT}
        self.tier_lev_cap = {"stable": config.STABLE_LEV_CAP, "mid": config.MID_LEV_CAP, "high": config.HIGH_LEV_CAP}
        # UI-tunable sizing knobs (refreshed from the params table by _reload_params; config = fallback)
        self.max_lev = config.MAX_LEV
        self.min_lev = config.MIN_LEV
        self.min_open_margin_pct = config.MIN_OPEN_MARGIN_PCT
        self.coin_margin_cap_pct = config.COIN_MARGIN_CAP_PCT   # per-coin margin ceiling (anti-stacking)
        self.max_adds = config.MAX_ADDS
        self.max_entry_chase_pct = config.MAX_ENTRY_CHASE_PCT
        self.vol_fallback_sigma = config.VOL_FALLBACK_SIGMA
        # 扛单 copy-side stop: flat adverse-price cut (isolated tail guard). UI-tunable via _reload_params.
        self.copy_stop_enable = config.COPY_STOP_ENABLE
        self.copy_stop_pct = config.COPY_STOP_PCT            # legacy flat-% fallback (σ unavailable)
        self.risk_budget = config.RISK_BUDGET                # v9: lev = RISK_BUDGET/σ (margin loss per 1σ)
        self.stop_sigma_mult = config.STOP_SIGMA_MULT        # v9: σ-stop cut = this × σ adverse
        self.vol: dict = {}              # coin -> σ (read-cache mirror of coin_vol; refreshed off hot path)
        self.vol_coins: set = set()      # coins we've encountered -> the periodic σ-refresh work set
        self.held_off: set = set()       # wallets polled ONLY because we hold a copy (off-watchlist) ->
        #                                  EXIT-ONLY: follow their reduce/close, never open a NEW position
        self.target_acct: dict = {}      # addr -> target's account value (conviction denominator)
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
        self.paused = False              # dashboard pause: stop opening NEW copies; existing keep to close
        self._proc_state = "running"     # process_status state machine (running|pausing|paused|resuming)
        self._proc_owner = f"observer:{os.getpid()}"

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

    def _target_snapshot(self, addr, coin):
        """The master's CURRENT position on this coin from clearinghouseState — returns
        (capped_leverage_we_mirror, raw_leverage, margin_used, entry_px). ONE call serves both our
        sizing (capped lev) and the at-OPEN record we persist (the target's leverage/margin/entry so
        the report never has to re-fetch and shows them even after the position closes). MUST name the
        builder dex for stock/builder perps (xyz:*) — the standard call returns [] for those, which
        silently fell back to MAX_LEV and over-levered every stock-perp copy to 10x. capped lev falls
        back to MAX_LEV if unreadable (mirror-capped intent); raw/margin/entry are None if unreadable."""
        dex = coin.split(":")[0] if ":" in coin else None
        cs = rest.clearinghouse_state(addr, dex)
        raw_lev = margin = entry = None
        if isinstance(cs, dict):
            for ap in cs.get("assetPositions", []):
                pos = ap.get("position", {})
                if pos.get("coin") == coin:
                    raw_lev = f((pos.get("leverage") or {}).get("value"))
                    entry = f(pos.get("entryPx"))
                    margin = f(pos.get("marginUsed")) or (f(pos.get("positionValue")) / raw_lev
                                                          if raw_lev else None)
                    break
        return max(1.0, min(raw_lev or self.max_lev, self.max_lev)), raw_lev, margin, entry

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
        if maker and config.EXEC_MAKER_MIRROR:    # only rest passively if we proactively mirror the
            return bid if is_buy else ask         # target's resting order (else assuming our rest fills
        #                                           instantly = optimistic; see config.EXEC_MAKER_MIRROR)
        return ask if is_buy else bid             # default: taker catch-up across the spread, CURRENT book

    # -- restart recovery: reload open copies from db ------------------------
    def _reload_params(self):
        """Refresh UI-tunable strategy params from the params table (engine units; config = fallback).
        Called at startup + each watchlist reload so dashboard edits take effect on the NEXT new copy.
        Fully defensive: any failure keeps the current values (never disrupts the live engine)."""
        try:
            from . import params as P
            f = P.load_follow(self.db)
            if f.get("MIN_FOLLOW_SCORE") is not None: self.min_score = f["MIN_FOLLOW_SCORE"]
            if f.get("MAX_TARGETS"): self.top_n = int(f["MAX_TARGETS"])
            if f.get("ADD_MARGIN_PCT") is not None: self.add_margin_pct = f["ADD_MARGIN_PCT"]
            if f.get("MAX_LEV"): self.max_lev = f["MAX_LEV"]
            if f.get("MIN_LEV"): self.min_lev = f["MIN_LEV"]
            if f.get("STABLE_SIGMA_MAX") is not None: self.stable_sigma_max = f["STABLE_SIGMA_MAX"]
            if f.get("HIGH_SIGMA_MIN") is not None: self.high_sigma_min = f["HIGH_SIGMA_MIN"]
            for tier, mk, lk in (("stable", "STABLE_MARGIN_PCT", "STABLE_LEV_CAP"),
                                 ("mid", "MID_MARGIN_PCT", "MID_LEV_CAP"),
                                 ("high", "HIGH_MARGIN_PCT", "HIGH_LEV_CAP")):
                if f.get(mk) is not None: self.tier_margin[tier] = f[mk]
                if f.get(lk): self.tier_lev_cap[tier] = f[lk]
            if f.get("MIN_OPEN_MARGIN_PCT") is not None: self.min_open_margin_pct = f["MIN_OPEN_MARGIN_PCT"]
            if f.get("COIN_MARGIN_CAP_PCT"): self.coin_margin_cap_pct = f["COIN_MARGIN_CAP_PCT"]
            if f.get("MAX_ADDS") is not None: self.max_adds = int(f["MAX_ADDS"])
            self.max_entry_chase_pct = f.get("MAX_ENTRY_CHASE_PCT")     # None = chase guard off
            if f.get("VOL_FALLBACK_SIGMA"): self.vol_fallback_sigma = f["VOL_FALLBACK_SIGMA"]
            if f.get("COPY_STOP_ENABLE") is not None: self.copy_stop_enable = bool(f["COPY_STOP_ENABLE"])
            if f.get("COPY_STOP_PCT"): self.copy_stop_pct = f["COPY_STOP_PCT"]
            if f.get("RISK_BUDGET"): self.risk_budget = f["RISK_BUDGET"]
            if f.get("STOP_SIGMA_MULT") is not None: self.stop_sigma_mult = f["STOP_SIGMA_MULT"]
        except Exception as exc:  # noqa: BLE001
            _log(f"param reload failed (keeping current): {exc}")

    def _reload_open(self):
        rows = self.db.execute(
            "SELECT pos_id,addr,coin,side,master_open_ms,master_open_px,master_peak_sz,leverage,"
            "margin,notional,entry_px,size,rem_size,liq_px,realized_pnl,add_count,mae_pct,num_actions,stop_px "
            "FROM copy_position WHERE status='open'").fetchall()
        for r in rows:
            (pid, addr, coin, side, mo, mpx, peak, lev, mgn, notl, epx, sz, rem, liq, rpnl, adds, mae, na, stopx) = r
            ev = asyncio.Event()
            if epx is not None:
                ev.set()
            self.open_ep[(addr, coin)] = {
                "pos_id": pid, "side": side, "sign": 1 if side == "long" else -1,
                "master_open_ms": mo, "master_open_px": mpx, "master_peak": peak or 0.0,
                "open_maker": False, "open_oid": None, "leverage": lev or 0.0, "margin": mgn or 0.0,
                "notional": notl or 0.0, "entry_px": epx, "size": sz or 0.0, "rem_size": rem or 0.0,
                "liq_px": liq or 0.0, "stop_px": stopx or 0.0, "realized_pnl": rpnl or 0.0,
                "add_count": adds or 0, "entries_ready": ev, "lock": asyncio.Lock(),
                "mae": mae or 0.0, "num_actions": na or 0, "gap": False,
                "seen_oids": {o for (o,) in self.db.execute(   # orders already consumed (restart-safe)
                    "SELECT DISTINCT master_oid FROM copy_action WHERE pos_id=? AND action IN "
                    "('open','add')", (pid,)).fetchall() if o is not None}}
        if rows:
            _log(f"reloaded {len(rows)} open copy positions from db")

    async def _reconcile_open(self):
        """Startup state-reconcile (replaces the deleted time-based backfill for EXITS). Forward-only
        means we can't see a master's close that happened while we were down → a reloaded copy could
        orphan-hold. So for ONLY the wallets we still hold a copy on, fetch the master's CURRENT
        positions (clearinghouseState); if the master no longer holds ours (flat on that coin, or
        flipped to the opposite side), close our copy now at the live book. Masters still in the
        position (same side) are left untouched — forward polling follows their next action."""
        held = sorted({addr for (addr, _) in self.open_ep})
        for addr in held:
            # standard perp + each builder dex we hold a position on (stock perps aren't in the
            # standard clearinghouseState — without the dex they'd read as flat and get wrong-closed).
            dexes = sorted({c.split(":")[0] for (a, c) in self.open_ep if a == addr and ":" in c})
            szi, all_ok = {}, True
            for dex in [None] + dexes:
                st = await asyncio.to_thread(rest.clearinghouse_state, addr, dex)
                if not isinstance(st, dict):
                    all_ok = False
                    break                             # a fetch failed — safer to hold than wrong-close
                for ap in st.get("assetPositions", []):
                    p = ap.get("position", {}) or {}
                    if p.get("coin") is not None:
                        szi[p["coin"]] = f(p.get("szi")) or 0.0
            if not all_ok:
                continue
            for (a, coin), ep in list(self.open_ep.items()):
                if a != addr:
                    continue
                m = szi.get(coin, 0.0)                # master's signed size on this coin, now
                still = (m > config.FLAT) if ep["side"] == "long" else (m < -config.FLAT)
                if still:
                    continue                          # master still in it (same side) -> keep & follow
                ba = await asyncio.to_thread(rest.book_top, coin)
                mid = ((ba[0] + ba[1]) / 2) if ba else ep["entry_px"]
                await self._apply_reduce(addr, coin, ep, now_ms(), mid, 0.0, 0.0,
                                         closing=True, liq=False, maker=False, gap=True, forced_px=mid)
                _log(f"RECONCILE-CLOSE {addr[:10]} {coin} {ep['side']} @ {mid:g} "
                     f"pnl=${ep['realized_pnl']:+,.1f}  bal=${self.balance:,.0f} (master no longer holds it)")

    async def reconcile_loop(self):
        """Periodic safety net for the startup reconcile. Forward polling should catch a master's close
        live, but a missed fill would orphan-hold; this re-checks every held wallet's CURRENT positions
        every RECONCILE_INTERVAL_S and closes any copy whose master has gone flat/flipped. Runs even when
        paused (an orphan whose master already left is pure risk with no copy value)."""
        while not self.stop:
            await asyncio.sleep(config.RECONCILE_INTERVAL_S)
            try:
                await self._reconcile_open()
            except Exception as exc:  # noqa: BLE001
                _log(f"reconcile loop error: {exc}")

    # -- watchlist sync (the copy engine tracks rolling discovery) -----------
    def _reload_targets(self, init=False):
        addrs, seed = load_targets(self.db, self.top_n, self.min_score)
        self.seed_coins = seed
        self.target_acct = {a: v for a, v in                 # conviction denominator (target's account)
                            self.db.execute("SELECT addr, acct_value FROM watchlist").fetchall()}
        # SAFEGUARD: never stop polling a wallet we still hold a copy on, even if it fell off the
        # watchlist this scan — else we'd miss its exit and dumb-hold the position to liquidation.
        held_off = [a for a in {addr for (addr, _) in self.open_ep} if a not in addrs]
        addrs = addrs + held_off
        self.held_off = set(held_off)         # EXIT-ONLY set: poll them to unwind, but open nothing new
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

    # -- per-coin volatility (regime-aware σ for risk-targeted sizing) --------
    def _sigma(self, coin: str) -> float:
        """Latest σ for coin from the read-cache (mirrors coin_vol); fallback if not refreshed yet."""
        return self.vol.get(coin) or self.vol_fallback_sigma

    def _tier(self, sigma: float) -> str:
        """σ-tier: stable (σ ≤ stable_sigma_max) / high (σ ≥ high_sigma_min) / mid (between)."""
        if sigma <= self.stable_sigma_max:
            return "stable"
        return "high" if sigma >= self.high_sigma_min else "mid"

    def _sizing_for(self, sigma: float):
        """v9 (margin_pct, leverage) for a coin's σ. margin% by tier; leverage = floor(clip(RISK_BUDGET/σ,
        MIN_LEV, tier cap)). RISK_BUDGET = the margin loss a 1σ move should cost (so lev·σ ≈ RISK_BUDGET);
        ties directly to the σ-stop. INTEGER leverage (floor). NOT mirrored from the master."""
        tier = self._tier(sigma)
        cap = self.tier_lev_cap[tier]
        lev_raw = (self.risk_budget / sigma) if sigma > 0 else cap
        lev = max(self.min_lev, float(int(min(lev_raw, cap, self.max_lev))))
        return self.tier_margin[tier], lev

    def _stop_px_for(self, entry_px: float, is_buy: bool, sigma: float = 0.0) -> float:
        """Stop PRICE from entry: a σ-ADAPTIVE adverse move = STOP_SIGMA_MULT × σ (down for a long, up for
        a short — same geometry as liq_px). σ = the coin's daily high-low range, so BTC cuts at ~4% and ZEC
        at ~15% — never noise-stopped, always before liq (lev=RISK_BUDGET/σ ⇒ liq at σ/RISK_BUDGET > σ).
        Falls back to the legacy flat COPY_STOP_PCT only when σ is unavailable. 0 = disabled."""
        if not self.copy_stop_enable or not entry_px:
            return 0.0
        d = (self.stop_sigma_mult * sigma) if sigma and sigma > 0 else self.copy_stop_pct
        if not d:
            return 0.0
        return entry_px * (1 - d) if is_buy else entry_px * (1 + d)

    async def _ensure_vol(self, coin: str):
        """Track coin for the periodic σ refresh, and fetch it NOW if we have no fresh value (so a
        first-seen coin gets a real σ within seconds; sizing uses the fallback only in the meantime)."""
        if not coin:
            return
        self.vol_coins.add(coin)
        if coin not in self.vol:
            self.vol[coin] = await asyncio.to_thread(volatility.refresh, self.db, coin)

    async def prewarm_vol(self):
        """Warm σ for the top-N-by-24h-volume crypto + each builder dex at startup (background, gentle):
        the liquid coins our targets are likeliest to trade get σ before their first fill — no first-open
        latency, warm restart. The long tail is still lazy-fetched on first fill. Skips already-warm coins."""
        for dex in (None, *rest.BUILDER_DEXES):
            vols = await asyncio.to_thread(rest.asset_volumes, dex)
            for coin, _ in sorted(vols.items(), key=lambda kv: -kv[1])[:config.VOL_PREWARM_TOP]:
                if coin in self.vol or self.stop:
                    continue
                self.vol_coins.add(coin)
                try:
                    self.vol[coin] = await asyncio.to_thread(volatility.refresh, self.db, coin)
                except Exception:  # noqa: BLE001
                    pass
        _log(f"vol prewarmed: {len(self.vol)} coins (top {config.VOL_PREWARM_TOP}/pool by 24h vol)")

    async def vol_refresh_loop(self):
        """Periodically re-compute σ for every tracked coin into coin_vol — OFF the signal hot path, so
        sizing only ever reads the cache. Catches a calm→volatile regime change within VOL_REFRESH_S."""
        while not self.stop:
            await asyncio.sleep(config.VOL_REFRESH_S)
            for coin in list(self.vol_coins):
                try:
                    self.vol[coin] = await asyncio.to_thread(volatility.refresh, self.db, coin)
                except Exception:  # noqa: BLE001
                    pass

    async def ensure_coin(self, coin: str):
        """Route a coin to its pricing source: crypto -> WS bbo subscription; transparent builder
        (stock/commodity) -> the REST l2Book poll set (WS bbo can't serve builder dexes)."""
        if not coin or coin in self.sub_coins or coin in self.stock_coins:
            return
        asyncio.create_task(self._ensure_vol(coin))   # make sure we have this coin's σ for sizing
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

    def _write_stats(self):
        """Snapshot the paper account into account_stats — the DASHBOARD time-series (equity curve, ROI,
        win rate, hedge ratio = net/gross, fee drag). Mark-to-market open positions off the live book."""
        init = config.INITIAL_BALANCE or 1.0
        upnl = locked = gross = net = 0.0
        for pos_id, coin, side, rem, size, entry, margin, notional in self.db.execute(
                "SELECT pos_id,coin,side,rem_size,size,entry_px,margin,notional FROM copy_position "
                "WHERE status='open' AND size>0").fetchall():
            ba = self.bbo.get(coin)
            mark = ((ba[0] + ba[1]) / 2) if (ba and ba[0] and ba[1]) else (entry or 0)
            sgn = 1 if side == "long" else -1
            pos_upnl = rem * (mark - (entry or 0)) * sgn
            upnl += pos_upnl
            locked += margin * rem / size
            cur_notl = notional * rem / size
            gross += cur_notl
            net += cur_notl * sgn
            self.db.execute(                          # persist per-position realtime fields for the dashboard
                "UPDATE copy_position SET mark_px=?, unrealized_pnl=? WHERE pos_id=?",
                (mark, pos_upnl, pos_id))
        open_n = self.db.execute("SELECT count(*) FROM copy_position WHERE status='open'").fetchone()[0]
        closed = [r[0] for r in self.db.execute(
            "SELECT realized_pnl FROM copy_position WHERE status!='open'").fetchall()]
        win_rate = (sum(1 for r in closed if r > 0) / len(closed)) if closed else 0.0
        fees = self.db.execute("SELECT COALESCE(SUM(ABS(our_qty_delta*our_px))*?,0) FROM copy_action",
                               (config.TAKER_FEE,)).fetchone()[0]
        equity = self.balance + upnl
        self.db.execute(
            "INSERT INTO account_stats (ts,balance,unrealized_pnl,equity,realized_pnl_cum,roi,open_n,"
            "closed_n,win_rate,locked_margin,available,gross_notional,net_notional,fees_cum) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), self.balance, upnl, equity, self.balance - init, equity / init - 1,
             open_n, len(closed), win_rate, locked, self.balance - locked, gross, net, fees))
        self.db.commit()

    def _refresh_marks(self):
        """Mark-to-market open positions into copy_position (mark_px/unrealized_pnl) WITHOUT appending
        an account_stats row. Lets the read-only dashboard show near-real-time浮盈 (account_stats stays
        the 5-min equity-curve series). Cheap: one UPDATE per open position off the in-memory book."""
        for pos_id, coin, side, rem, size, entry in self.db.execute(
                "SELECT pos_id,coin,side,rem_size,size,entry_px FROM copy_position "
                "WHERE status='open' AND size>0").fetchall():
            ba = self.bbo.get(coin)
            if not (ba and ba[0] and ba[1]):
                continue
            mark = (ba[0] + ba[1]) / 2
            sgn = 1 if side == "long" else -1
            self.db.execute("UPDATE copy_position SET mark_px=?, unrealized_pnl=? WHERE pos_id=?",
                            (mark, rem * (mark - (entry or 0)) * sgn, pos_id))
        self.db.commit()

    async def mark_refresh_loop(self):
        """Frequent mark refresh for dashboard freshness (between the 5-min account_stats snapshots)."""
        while not self.stop:
            await asyncio.sleep(25)
            try:
                self._refresh_marks()
            except Exception as exc:  # noqa: BLE001 — never let dashboard freshness kill the engine
                _log(f"mark refresh failed: {exc}")

    async def _announce(self):
        while not self.stop:
            await asyncio.sleep(300)
            o = self.db.execute("SELECT count(*) FROM copy_position WHERE status='open'").fetchone()[0]
            c = self.db.execute("SELECT count(*) FROM copy_position WHERE status!='open'").fetchone()[0]
            a = self.db.execute("SELECT count(*) FROM copy_action").fetchone()[0]
            _log(f"heartbeat: {o} open / {c} closed positions, {a} actions recorded")
            try:
                self._write_stats()                # append a dashboard snapshot every 5 min
            except Exception as exc:  # noqa: BLE001
                _log(f"stats snapshot failed: {exc}")

    async def prune_live_fills(self):
        """Keep live_fills bounded on disk. tid-dedup only needs the last POLL_OVERLAP_MS of history
        (the forward-only cursor re-fetches only a few seconds back), so deleting rows past the
        retention window can't cause re-processing — the rest is just audit. Runs at startup then 6h."""
        while not self.stop:
            cutoff = now_ms() - config.LIVE_FILLS_RETENTION_DAYS * 86400_000
            n = self.db.execute("DELETE FROM live_fills WHERE time_ms < ?", (cutoff,)).rowcount
            self.db.commit()
            if n:
                _log(f"pruned {n} live_fills older than {config.LIVE_FILLS_RETENTION_DAYS}d")
            await asyncio.sleep(6 * 3600)

    # -- dashboard control plane (command channel) ---------------------------
    def _write_proc_status(self, state):
        """Upsert this process's liveness + state machine row for the dashboard. heartbeat_at lets the
        UI flag a dead observer (stale) and lets the command channel self-heal."""
        self._proc_state = state
        self.db.execute(
            "INSERT INTO process_status (name,state,pid,heartbeat_at,detail_json) VALUES "
            "('observer',?,?,?,?) ON CONFLICT(name) DO UPDATE SET state=excluded.state,"
            "pid=excluded.pid,heartbeat_at=excluded.heartbeat_at,detail_json=excluded.detail_json",
            (state, os.getpid(), now_iso(),
             json.dumps({"paused": self.paused, "targets": len(self.addrs),
                         "open": len(self.open_ep)})))
        self.db.commit()

    async def consume_commands(self):
        """Poll the command channel and execute the commands this process OWNS (pause/resume/close/
        toggle). Each: acked -> done/failed. Scanner-owned commands (rescan) are left untouched. Also
        refreshes process_status heartbeat each loop so the dashboard sees the observer alive."""
        OWNED = ("pause", "resume", "close_position", "close_all", "wallet_toggle", "reload_params")
        last_hb = 0.0
        while not self.stop:
            try:
                rows = self.db.execute(
                    "SELECT id,type,payload_json FROM commands WHERE status='pending' AND type IN "
                    "(" + ",".join("?" * len(OWNED)) + ") ORDER BY id", OWNED).fetchall()
                for cmd_id, ctype, payload_json in rows:
                    self.db.execute("UPDATE commands SET status='acked',acked_at=? WHERE id=?",
                                    (now_iso(), cmd_id))
                    self.db.commit()
                    try:
                        result = await self._dispatch_command(ctype, json.loads(payload_json or "{}"))
                        self.db.execute(
                            "UPDATE commands SET status='done',done_at=?,result_json=? WHERE id=?",
                            (now_iso(), json.dumps(result), cmd_id))
                        self.db.commit()
                        _log(f"command #{cmd_id} {ctype} -> done {result}")
                    except Exception as exc:  # noqa: BLE001 — a bad command must not kill the engine
                        self.db.execute("UPDATE commands SET status='failed',done_at=?,error=? WHERE id=?",
                                        (now_iso(), str(exc), cmd_id))
                        self.db.commit()
                        _log(f"command #{cmd_id} {ctype} -> FAILED {exc}")
                if time.time() - last_hb > 15:        # refresh liveness heartbeat (throttled)
                    self._write_proc_status(self._proc_state)
                    last_hb = time.time()
            except Exception as exc:  # noqa: BLE001
                _log(f"command loop error: {exc}")
            await asyncio.sleep(1.5)

    async def _dispatch_command(self, ctype, payload):
        if ctype == "pause":
            self.paused = True
            self._write_proc_status("paused")
            return {"paused": True}
        if ctype == "resume":
            self.paused = False
            self._write_proc_status("running")
            return {"paused": False}
        if ctype == "close_position":
            return await self._cmd_close(int(payload["positionId"]))
        if ctype == "close_all":
            return await self._cmd_close_all()
        if ctype == "wallet_toggle":
            return self._cmd_wallet_toggle(payload["address"], bool(payload["enabled"]))
        if ctype == "reload_params":               # UI saved follow params → apply NOW (incl. follow line)
            self._reload_params()                  # re-read sizing/stop/line from params table
            self._reload_targets()                 # rebuild the follow set with the fresh line
            return {"reloaded": True, "score_line": self.min_score, "targets": len(self.addrs)}
        raise ValueError(f"unhandled command type {ctype}")

    def _ep_by_pos(self, pos_id):
        for (addr, coin), ep in self.open_ep.items():
            if ep.get("pos_id") == pos_id:
                return addr, coin, ep
        return None

    async def _cmd_close(self, pos_id):
        """Manual flatten of one live copy at the current book (operator emergency exit). Reuses the
        normal close path so PnL/account/status finalize identically to a master-driven close."""
        found = self._ep_by_pos(pos_id)
        if not found:
            raise ValueError(f"position {pos_id} not open/live")
        addr, coin, ep = found
        if ep.get("entry_px") is None:
            raise ValueError(f"position {pos_id} still opening")
        ba = self.bbo.get(coin)
        mid = ((ba[0] + ba[1]) / 2) if (ba and ba[0] and ba[1]) else ep["entry_px"]
        await self._apply_reduce(addr, coin, ep, now_ms(), mid, 0.0, 0.0,
                                 closing=True, liq=False, maker=False, forced_px=mid)
        _log(f"MANUAL-CLOSE {addr[:10]} {coin} {ep['side']} @ {mid:g} "
             f"pnl=${ep['realized_pnl']:+,.1f}  bal=${self.balance:,.0f}")
        return {"positionId": pos_id, "exit": mid, "realizedPnl": round(ep["realized_pnl"], 2)}

    async def _cmd_close_all(self):
        pos_ids = [ep["pos_id"] for ep in self.open_ep.values()]
        closed = []
        for pid in pos_ids:
            try:
                await self._cmd_close(pid)
                closed.append(pid)
            except Exception as exc:  # noqa: BLE001
                _log(f"close_all: skip {pid}: {exc}")
        return {"closed": closed, "count": len(closed)}

    def _cmd_wallet_toggle(self, addr, enabled):
        """Flip a target's enabled flag (Observer is the single writer of target_controls), then
        re-sync targets so the effect lands now: disabled + we hold a copy -> exit-only (held_off);
        disabled + flat -> dropped from polling; enabled -> back in the rotation."""
        addr = addr.lower()
        ts, e = now_iso(), 1 if enabled else 0
        cur = self.db.execute("UPDATE target_controls SET enabled=?,updated_at=? WHERE addr=?", (e, ts, addr))
        if cur.rowcount == 0:
            self.db.execute("INSERT INTO target_controls (addr,enabled,updated_at) VALUES (?,?,?)", (addr, e, ts))
        self.db.commit()
        self._reload_targets()
        return {"address": addr, "enabled": bool(enabled)}

    # -- SIGNAL: continuous REST poll of the whole watchlist -----------------
    async def poll_loop(self):
        """Primary engine. Round-robin every watchlist wallet's recent fills (cursor − a few-second
        overlap so a fill landing between rounds isn't missed; tid-dedup absorbs the re-fetch),
        replaying each through the idempotent process_fill. No fixed period — the REST pacer sets the
        cadence; a full round over ~tens of wallets takes a few seconds. Strictly FORWARD-ONLY: each
        wallet's cursor starts at now (set in _reload_targets), so we only ever copy fills that
        happen while we're live — never history. Re-reads the watchlist periodically so rolling
        discovery flows in without a restart."""
        last_reload = now_ms()
        while not self.stop:
            if now_ms() - last_reload > config.WATCHLIST_RELOAD_S * 1000:
                self._reload_params()              # params FIRST (pick up UI edits) ...
                self._reload_targets()             # ... then load targets with the fresh follow line
                last_reload = now_ms()
            for addr in list(self.addrs):
                since = self.last_fill_ms.get(addr, now_ms()) - config.POLL_OVERLAP_MS
                await self._poll_fills(addr, since)
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
                    self._maybe_stop(coin, mid)                  # 扛单 copy-side stop if stop_px crossed
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
        self.vol = volatility.load_all(self.db)    # warm the σ read-cache from coin_vol (restart-safe)
        self._reload_open()                        # restore open copies (restart-safe)
        for (_, coin) in self.open_ep:             # reloaded stock positions need REST book polling
            if coin not in self.crypto_coins and self._copyable(coin):
                self.stock_coins.add(coin)
        await self._reconcile_open()               # close any copy whose master went flat while we were down
        self._reload_params()                      # load UI-tunable strategy params FIRST (config = fallback)
        self._reload_targets(init=True)            # then load watchlist with the live follow line (forward-only)
        try:
            self._write_proc_status("running")     # advertise liveness + state to the dashboard
        except Exception as exc:  # noqa: BLE001 — status is non-essential; never block the engine
            _log(f"proc status init failed: {exc}")
        asyncio.create_task(self.consume_commands())  # dashboard control plane (pause/close/toggle)
        asyncio.create_task(self.mark_refresh_loop())  # dashboard freshness (25s mark-to-market)
        asyncio.create_task(self._announce())
        asyncio.create_task(self.prewarm_vol())       # warm σ for top-volume coins (no first-open latency)
        asyncio.create_task(self.vol_refresh_loop())  # periodic regime-aware σ refresh (off hot path)
        asyncio.create_task(self.reconcile_loop())    # periodic orphan-check: close copies whose master exited
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
        self._maybe_stop(coin, mid)                # 扛单 copy-side stop if price crossed stop_px

    def _record_fill(self, addr, x) -> bool:
        """Insert the (aggregated, trade-level) fill; True if NEW, False if this tid was already seen
        (dedup) — what makes process_fill idempotent so overlapping poll rounds can't double-copy."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO live_fills (addr,tid,time_ms,coin,side,dir,px,sz,closed_pnl,crossed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (addr, x.get("tid"), x.get("time"), x.get("coin"), x.get("side"), x.get("dir"),
             f(x.get("px")), f(x.get("sz")), f(x.get("closedPnl")), 1 if x.get("crossed") else 0))
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
            if (abs(pos0) < config.FLAT and abs(pos1) >= config.FLAT
                    and addr not in self.held_off       # held-off (off-watchlist) = exit-only, no new opens
                    and not self.paused):               # dashboard pause = no new opens (existing keep to close)
                self._open_position(addr, coin, t, px, pos1, maker, oid)
            return
        ep["master_peak"] = max(ep["master_peak"], abs(pos1))
        if liq:
            ep["was_liq"] = 1
        if abs(pos1) >= abs(pos0) - config.FLAT and not abs(pos1) < config.FLAT:
            # A scale-in is a NEW ORDER (new oid) growing the position. Same-oid continued fills are
            # one resting order filling over time (slices) — aggregateByTime only merges same-INSTANT
            # fills, so a limit order filling over several seconds reappears as same-oid fills; counting
            # those as adds is the slice-as-add bug. Fold them in (peak already tracked above), no add.
            if oid is not None and oid in ep.get("seen_oids", ()):
                return
            ep.setdefault("seen_oids", set()).add(oid)
            asyncio.create_task(self._apply_add(addr, coin, ep, t, px, signed, pos1, maker, oid))
        else:
            asyncio.create_task(self._apply_reduce(addr, coin, ep, t, px, signed, pos1,
                                                   closing=abs(pos1) < config.FLAT, liq=liq, maker=maker, oid=oid))

    def _open_position(self, addr, coin, t, px, pos1, maker, oid):
        if not self._copyable(coin):
            return              # copy crypto + transparent builder (stocks); skip opaque/unknown
        side = "long" if pos1 > 0 else "short"
        lag_sec = max(0.0, (now_ms() - t) / 1000.0)   # copy latency: master fill -> our detection (dashboard)
        cur = self.db.execute(
            "INSERT INTO copy_position (addr,coin,side,status,master_open_ms,master_open_px,"
            "master_peak_sz,opened_at,num_actions,open_lag_sec) VALUES (?,?,?,'open',?,?,?,?,0,?)",
            (addr, coin, side, t, px, abs(pos1), now_iso(), lag_sec))
        ep = {"pos_id": cur.lastrowid, "side": side, "sign": 1 if side == "long" else -1,
              "master_open_ms": t, "master_open_px": px, "master_peak": abs(pos1),
              "open_maker": maker, "open_oid": oid, "leverage": 0.0, "margin": 0.0, "notional": 0.0,
              "entry_px": None, "size": 0.0, "rem_size": 0.0, "liq_px": 0.0, "stop_px": 0.0, "realized_pnl": 0.0,
              "add_count": 0, "entries_ready": asyncio.Event(), "lock": asyncio.Lock(), "mae": 0.0,
              "num_actions": 0, "gap": False, "seen_oids": {oid}}   # orders consumed (open + real adds)
        self.open_ep[(addr, coin)] = ep
        asyncio.create_task(self._resolve_entry(addr, coin, ep, t, px))

    async def _resolve_entry(self, addr, coin, ep, t, master_px):
        is_buy = ep["side"] == "long"                # opening a long => we buy
        stale = (now_ms() - t) > STALE_MS            # backfilled-late: book is no longer the fill's
        px = master_px if stale else self._fill_px(coin, is_buy, ep["open_maker"], master_px)
        if not px or px <= 0 or not master_px or master_px <= 0:   # can't price it -> don't hold a 0-price
            self.db.execute("DELETE FROM copy_position WHERE pos_id=?", (ep["pos_id"],))  # position (also
            self.db.commit()                                                              # avoids /0 below)
            self.open_ep.pop((addr, coin), None)
            _log(f"skip {coin}: unpriceable (px={px}, master_px={master_px}) — not followed")
            return
        chase = (px - master_px) / master_px * 1e4 * ep["sign"]   # bps worse than master (+ = worse)
        we_rest = ep["open_maker"] and config.EXEC_MAKER_MIRROR    # only a true maker-mirror rests (no chase)
        if (self.max_entry_chase_pct is not None and not we_rest
                and chase > self.max_entry_chase_pct * 100):       # spike too far past master -> skip
            self.db.execute("DELETE FROM copy_position WHERE pos_id=?", (ep["pos_id"],))
            self.db.commit()
            self.open_ep.pop((addr, coin), None)
            return                                            # chase-skip (rare); recorded by absence
        master_cap, m_lev, m_mgn, m_entry = await asyncio.to_thread(self._target_snapshot, addr, coin)  # master ctx
        # v8 sizing: σ → tier (stable/mid/high) → margin% + lev cap; leverage scales ∝1/σ within the tier
        #  margin = available × <tier>_margin_pct
        #  lev    = floor(clip( RISK_BUDGET/σ , MIN_LEV , <tier>_lev_cap ))   (v9: RISK_BUDGET = margin/1σ)
        #  notional = margin·lev. NOT mirrored from the master (σ alone sizes us). A calm coin (BTC, GOLD)
        #  lands in the stable tier with big margin + high lev; a wild one (ZEC/meme) in high tier, small.
        await self._ensure_vol(coin)                 # fetch THIS coin's real σ once (else first open = fallback)
        sigma = self._sigma(coin)
        margin_pct, lev = self._sizing_for(sigma)    # v8: tier margin% + σ-scaled-capped leverage
        async with self.acct_lock:                   # serialize margin allocation across opens
            margin = max(0.0, self._available() * margin_pct)
            # PER-COIN cap: total margin across our open positions on this coin IN THE SAME DIRECTION ≤
            # COIN_MARGIN_CAP_PCT of the account. Stops a single move making N wallets pile the SAME way
            # into one coin (e.g. all short BTC). Same-direction ONLY on purpose: an opposite-side signal
            # (a wallet flips long while we hold shorts) OFFSETS our exposure, it doesn't stack it, so it
            # must NOT be blocked. Shrink to the remaining room; skip if that side of the coin is full.
            existing_coin = sum(e.get("margin", 0.0) for (a2, c2), e in self.open_ep.items()
                                if c2 == coin and e.get("side") == ep["side"] and e is not ep)
            room = max(0.0, self.coin_margin_cap_pct * self.balance - existing_coin)
            capped = min(margin, room)
            if capped < self.min_open_margin_pct * self.balance:     # free balance too low OR side full
                why = "coin_full" if room < margin else "margin_too_small"
                _log(f"skip {coin} {ep['side']} {addr[:10]}: {why} (room ${room:,.0f} / want ${margin:,.0f})")
                self.db.execute("DELETE FROM copy_position WHERE pos_id=?", (ep["pos_id"],))  # -> skip
                self.db.commit()
                self.open_ep.pop((addr, coin), None)
                return
            margin = capped
            notional = margin * lev
            # NEVER exceed the MASTER's own notional on this coin — we're a small isolated account
            # copying them; a position bigger than the source's is more exposed than the thing we're
            # copying (the twins-style small-notional scalpers especially). Cap notional, shrink margin
            # to match (margin stays = notional/lev so the isolated loss bound tracks the real size).
            master_notl = (m_mgn or 0.0) * (m_lev or 0.0)        # master_margin × master_leverage = their notional
            if master_notl > 0 and notional > master_notl:
                notional = master_notl
                margin = notional / lev if lev else margin
            size = notional / px if px else 0.0
            liq_px = px * (1 - 1.0 / lev) if is_buy else px * (1 + 1.0 / lev)  # isolated: loss = margin
            stop_px = self._stop_px_for(px, is_buy, sigma)  # 扛单 cut at STOP_SIGMA_MULT×σ adverse (0 = off)
            ep.update(leverage=lev, margin=margin, notional=notional, entry_px=px,
                      size=size, rem_size=size, liq_px=liq_px, stop_px=stop_px)
            self.db.execute(                         # also persist the TARGET's lev/margin/entry at open
                "UPDATE copy_position SET leverage=?,margin=?,notional=?,entry_px=?,size=?,rem_size=?,"
                "liq_px=?,stop_px=?,master_leverage=?,master_margin=?,master_open_px=COALESCE(?,master_open_px) "
                "WHERE pos_id=?",
                (lev, margin, notional, px, size, size, liq_px, stop_px, m_lev, m_mgn, m_entry, ep["pos_id"]))
            self.db.commit()
        ep["entries_ready"].set()
        msz = ep["master_peak"] * ep["sign"]
        self._record_action(ep, addr, coin, t, "open", ep["open_maker"], ep["open_oid"], master_px,
                            msz, msz, size * ep["sign"], px, 0.0, chase)
        self.db.commit()                                      # the open is in copy_position/copy_action

    async def _apply_add(self, addr, coin, ep, t, master_px, signed, pos1, maker, oid):
        """Master scaled in -> we follow (average down/up) up to MAX_ADDS, each add committing
        another ADD_MARGIN_PCT of available at the current price; the avg entry + liq_px recompute.
        Past the cap we record his add but don't follow (the delta-based exit still mirrors him)."""
        async with ep["lock"]:
            try:
                await asyncio.wait_for(ep["entries_ready"].wait(), timeout=12)
            except asyncio.TimeoutError:
                pass
            if ep.get("entry_px") is None or (addr, coin) not in self.open_ep:
                return
            if ep["add_count"] >= self.max_adds:      # cap reached — observe but don't follow
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
            ep["stop_px"] = self._stop_px_for(ep["entry_px"], is_buy, self._sigma(coin))  # re-anchor σ-stop to new avg entry
            ep["add_count"] += 1
            slip = (px - master_px) / master_px * 1e4 * ep["sign"] if master_px else 0.0
            self._record_action(ep, addr, coin, t, "add", maker, oid, master_px, signed, pos1,
                                add_size * ep["sign"], px, 0.0, slip)
            self.db.execute(
                "UPDATE copy_position SET margin=?,notional=?,entry_px=?,size=?,rem_size=?,liq_px=?,stop_px=?,"
                "add_count=? WHERE pos_id=?", (ep["margin"], ep["notional"], ep["entry_px"], ep["size"],
                ep["rem_size"], ep["liq_px"], ep["stop_px"], ep["add_count"], ep["pos_id"]))
            self.db.commit()                                  # the add is in copy_action

    async def _apply_reduce(self, addr, coin, ep, t, master_px, signed, pos1, closing, liq, maker,
                            oid=None, gap=False, forced_px=None, stop=False):
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
            status = ("liquidated" if (closing and liq) else "stopped" if (closing and stop)
                      else "gap_closed" if (closing and gap) else "closed" if closing else "open")
            self.db.execute(
                "UPDATE copy_position SET rem_size=?,realized_pnl=?,mae_pct=?,was_liq=?,was_stopped=?,status=?,"
                "closed_at=? WHERE pos_id=?", (ep["rem_size"], ep["realized_pnl"], ep["mae"],
                ep.get("was_liq", 0), ep.get("was_stopped", 0), status,
                now_iso() if closing else None, ep["pos_id"]))
            self._save_account()
            self.db.commit()
            if closing:
                self.open_ep.pop((addr, coin), None)         # normal closes are in copy_position; only
                if liq:                                       # liquidation (our isolated stop-out) is logged
                    _log(f"LIQUIDATED {addr[:10]} {coin} {ep['side']} -${ep['margin']:,.0f}  bal=${self.balance:,.0f}")
                elif stop:                                    # 扛单 copy-side stop: cut before the master does
                    _log(f"STOPPED {addr[:10]} {coin} {ep['side']} pnl=${ep['realized_pnl']:+,.0f}  bal=${self.balance:,.0f}")

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

    async def _stop_out(self, addr, coin, ep, mid):
        """扛单 copy-side stop: the target's thesis (mean-revert by ~tp_move) is broken — price ran the
        adverse way past our stop. Exit NOW at the live mid instead of riding it to our far liquidation
        (we don't bag-hold with the target). A real reduce/close on the master still flows normally."""
        if ep.get("stopping") or ep.get("liquidating") or (addr, coin) not in self.open_ep:
            return
        ep["stopping"] = True
        ep["was_stopped"] = 1
        await self._apply_reduce(addr, coin, ep, now_ms(), mid, 0.0, 0.0,
                                 closing=True, liq=False, maker=False, forced_px=mid, stop=True)

    def _maybe_stop(self, coin, mid):
        if not self.copy_stop_enable:
            return
        for (a, c), ep in list(self.open_ep.items()):
            if (c == coin and ep.get("stop_px") and ep["rem_size"] > config.FLAT
                    and not ep.get("liquidating") and not ep.get("stopping")):
                hit = mid <= ep["stop_px"] if ep["side"] == "long" else mid >= ep["stop_px"]
                if hit:
                    asyncio.create_task(self._stop_out(a, coin, ep, mid))

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

    async def _poll_fills(self, addr: str, since: int):
        """SIGNAL fetch: REST-pull the wallet's fills since `since` (a few seconds back — the live
        poll window, NOT history) and replay through the idempotent process_fill (dedup by tid).
        aggregateByTime MERGES an order's partial fills into one TRADE-level row, so (a) one sliced
        order = one record (not N), and (b) it isn't mis-counted as N scale-ins. The cursor lives
        only in memory (self.last_fill_ms): startup is strictly forward-only — we never catch up on
        fills we missed while down, because copying an entry we didn't see live is meaningless."""
        page = await asyncio.to_thread(rest.post_soft, {
            "type": "userFillsByTime", "user": addr, "startTime": int(max(0, since)), "aggregateByTime": True})
        if isinstance(page, list) and page:
            for x in sorted(page, key=lambda fl: fl["time"]):
                self.process_fill(addr, x)
            self.db.commit()


# ------------------------------------------------------------------------- loaders
def load_targets(db, n: int, min_score: float = 0.0):
    """Followable set = enabled watchlist wallets with score ≥ line, top-n by rank. (Dormancy is already
    handled at the watchlist level by the scanner's `inactive` gate; per-trade risk by the σ-stop +
    isolated margin — so no observer-side dormant/open-bag bench. Wallets we still hold a copy on are
    re-added EXIT-ONLY by the caller's held_off safeguard.)"""
    addrs = [r[0] for r in db.execute(
        "SELECT w.addr FROM watchlist w LEFT JOIN target_controls c ON c.addr=w.addr "
        "WHERE COALESCE(c.enabled,1)=1 AND w.score >= ? "
        "ORDER BY w.rank LIMIT ?",
        (min_score, n)).fetchall()]
    seed = {a: {r[0] for r in db.execute("SELECT DISTINCT coin FROM episode WHERE addr=?", (a,)).fetchall()}
            for a in addrs}
    return addrs, seed


# -------------------------------------------------------------------------- report
def report(db) -> None:
    """On-demand snapshot. ONE table row per (followed-wallet, coin) — open + closed copies merged.
    The target's side (margin/entry/leverage) is read from what we PERSISTED AT OPEN (so it shows
    even after the position closes — no live re-fetch). OPEN rows mark-to-market the live book for
    unrealized PnL (tagged 浮); CLOSED rows show realized PnL (tagged 实)."""
    from collections import defaultdict
    acct = db.execute("SELECT initial_balance, balance FROM copy_account WHERE id=1").fetchone()
    init, bal = acct if acct else (config.INITIAL_BALANCE, config.INITIAL_BALANCE)
    rank_of = {a: r for r, a in db.execute("SELECT rank, addr FROM watchlist").fetchall()}

    groups = defaultdict(list)                       # (addr,coin) -> [position rows]
    for row in db.execute(
            "SELECT pos_id,addr,coin,side,leverage,margin,entry_px,size,rem_size,realized_pnl,status,"
            "master_open_px,master_leverage,master_margin FROM copy_position").fetchall():
        groups[(row[1], row[2])].append(row)

    open_keys = {(a, c) for (a, c), rs in groups.items() if any(x[10] == "open" for x in rs)}
    mark = {}                                        # coin -> live mid (open positions: unrealized PnL)
    for (_, coin) in open_keys:
        if coin not in mark:
            ba = rest.book_top(coin)
            mark[coin] = ((ba[0] + ba[1]) / 2) if ba else None

    def lag_of(pos_id):
        lr = db.execute("SELECT recv_ms-ts FROM copy_action WHERE pos_id=? AND action='open' "
                        "ORDER BY act_id LIMIT 1", (pos_id,)).fetchone()
        return lr[0] if lr else None

    table, open_margin, total_unreal = [], 0.0, 0.0
    for (addr, coin), rs in groups.items():
        realized = sum(x[9] for x in rs)
        opens = [x for x in rs if x[10] == "open"]
        num = rank_of.get(addr)
        num_s = f"#{num}" if num else addr[:6]
        ref = opens[0] if opens else rs[-1]          # target side persisted at open (survives close)
        m_entry, m_lev, m_mgn = ref[11], ref[12], ref[13]
        if opens:
            r = opens[0]
            our_lev, our_mgn, our_entry, rem, side = r[4], r[5], r[6], r[8], r[3]
            mk = mark.get(coin) or our_entry
            unreal = rem * (mk - our_entry) * (1 if side == "long" else -1)
            open_margin += our_mgn; total_unreal += unreal
            table.append((num_s, coin, side, m_mgn, m_entry, m_lev, lag_of(r[0]),
                          our_entry, our_mgn, our_lev, realized + unreal, "浮"))
        else:
            r = rs[-1]
            table.append((num_s, coin, r[3], m_mgn, m_entry, m_lev, lag_of(r[0]),
                          r[6], r[5], r[4], realized, "实"))
    equity = bal + total_unreal

    print(f"\n{'='*100}")
    print(f"PAPER COPY 报告    权益 ${equity:,.2f}    ROI {equity/init-1:+.2%}   (起始 ${init:,.0f})")
    print(f"  已实现余额 ${bal:,.2f}   浮动盈亏 ${total_unreal:+,.2f}   "
          f"持仓占用保证金 ${open_margin:,.2f}   可动用余额 ${bal-open_margin:,.2f}")
    n_open = sum(1 for t in table if t[11] == "浮")
    print(f"  在持 {n_open} 笔 / 已平 {len(table)-n_open} 笔   (按 钱包+币种 合并)")
    print("=" * 100)
    if not table:
        print("  (还没有跟单记录)\n"); return
    h = ("  {:>4} {:10} {:5}|{:>10} {:>11} {:>6}|{:>7}|{:>11} {:>10} {:>6}|{:>11}".format(
        "编号", "coin", "side", "tgt_mgn", "tgt_px", "tgt_lv", "lag", "our_px", "our_mgn", "our_lv", "pnl$"))
    print(h + "\n  " + "-" * (len(h) - 2))
    def s(v, spec, pre="", suf=""): return (pre + format(v, spec) + suf) if v is not None else "—"
    for t in sorted(table, key=lambda r: -r[10]):
        num_s, coin, side, m_mgn, m_entry, m_lev, lag_ms, o_entry, o_mgn, o_lev, pnl, lbl = t
        lag = f"{lag_ms/1000:.1f}s" if lag_ms is not None else "—"
        print("  {:>4} {:10} {:5}|{:>10} {:>11} {:>6}|{:>7}|{:>11} {:>10} {:>6}|{:>+10,.1f}{}".format(
            num_s, coin, side, s(m_mgn, ",.0f", "$"), s(m_entry, "g"), s(m_lev, ".0f", "", "x"),
            lag, format(o_entry, "g"), format(o_mgn, ",.0f"), format(o_lev, ".0f") + "x", pnl, lbl))
    print("\n  列: 编号=watchlist排名 · tgt_*=目标(保证金/均价/杠杆,在持为实时) · lag=跟单延迟 · "
          "our_*=我方(均价/保证金/杠杆) · pnl 浮=未平(mark) 实=已平(realized)")
    print(f"\n(sizing: σ-tiers margin/lev-cap [stable σ≤{config.STABLE_SIGMA_MAX*100:g}%: {config.STABLE_MARGIN_PCT*100:g}%/{config.STABLE_LEV_CAP:g}x · "
          f"mid: {config.MID_MARGIN_PCT*100:g}%/{config.MID_LEV_CAP:g}x · high σ≥{config.HIGH_SIGMA_MIN*100:g}%: {config.HIGH_MARGIN_PCT*100:g}%/{config.HIGH_LEV_CAP:g}x], "
          f"lev=cap×{config.STABLE_SIGMA_MAX*100:g}%/σ, {config.ADD_MARGIN_PCT*100:g}%/add (max {config.MAX_ADDS}), isolated)")
