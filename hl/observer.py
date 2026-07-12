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

from . import config, rest, selection, volatility, ws
from .coin_filter import coin_is_blacklisted, parse_coin_blacklist
from .copy_engine import (OpenSizingParams, isolated_liq_px, plan_open_sizing, reduce_leaves_dust,
                          stop_px as engine_stop_px, tier_for_sigma)
from .fill_transition import classify_fill_transition
from .sector import parse_json_obj, policy_allows_coin
from .util import f, now_iso, now_ms

logging.getLogger("websockets").setLevel(logging.CRITICAL)
STALE_MS = 30_000          # a detected fill older than this priced at master px (book unreliable)
MARK_WRITE_MIN_MS = 1_000  # dashboard mark freshness: persist at most once/sec/coin from live book ticks
MANUAL_CLOSE_COOLDOWN_S = 24 * 60 * 60


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Book:
    """One isolated paper account. The SAME strategy (sizing/adds/stops/caps) is applied to each — only
    EXECUTION differs, and BOTH books copy EVERY target fill. match_exec decides HOW we fill each one:
      • taker book (match_exec=False): we always CROSS (taker fee), even when the target rested — guaranteed fill.
      • maker book (match_exec=True): we MATCH the target — maker-rest when they maker (maker fee + their price),
        cross when they take (taker fee). Fees are charged per fill at the matching rate; separate tables."""
    def __init__(self, name, pos_table, act_table, acct_table, match_exec):
        self.name = name
        self.pos_table = pos_table          # copy_position / shadow_position
        self.act_table = act_table          # copy_action   / shadow_action
        self.acct_table = acct_table        # copy_account   / shadow_account
        self.match_exec = match_exec        # False = always taker; True = match the target's maker/taker per fill
        self.balance = config.INITIAL_BALANCE
        self.initial_balance = config.INITIAL_BALANCE
        self.wallet_initial_balance = config.PAPER_WALLET_INITIAL_BALANCE
        self.protected_reserve = max(0.0, self.wallet_initial_balance - self.initial_balance)
        self.open_ep: dict = {}             # (addr,coin) -> position state
        self._acct_lock = None              # created lazily inside the running loop (sync inspection creates none)
        # Lifetime dashboard counters. Initialized once from history at startup, then maintained per action/close
        # so the 5-minute stats snapshot never rescans the ever-growing action/position tables.
        self.closed_n = 0
        self.wins_n = 0
        self.gross_traded = 0.0
        self.fees_cum = 0.0
        self.stats_loaded = False

    @property
    def acct_lock(self):
        """Serialize margin allocation across opens without creating an orphan event loop at construction."""
        loop = asyncio.get_running_loop()
        if self._acct_lock is None or getattr(self._acct_lock, "_loop", loop) not in (None, loop):
            self._acct_lock = asyncio.Lock()
        return self._acct_lock


class Observer:
    # self.balance / self.open_ep / self.acct_lock delegate to the PRIMARY (taker) book so all existing
    # non-apply code (logs, equity stats, poll/stop loops) keeps working unchanged; the apply/helper methods
    # take an explicit `book` to operate on either account.
    @property
    def balance(self):
        return self.taker.balance

    @balance.setter
    def balance(self, v):
        self.taker.balance = v

    @property
    def open_ep(self):
        return self.taker.open_ep

    @property
    def acct_lock(self):
        return self.taker.acct_lock

    def __init__(self, db, addrs: list, seed_coins: dict, top_n: int = None, min_score: float = None,
                 add_frac: float = None):
        self.db = db
        self.addrs = addrs
        self.seed_coins = seed_coins
        self.top_n = top_n or config.MAX_TARGETS    # hard cap on followed wallets (REST-rate ceiling)
        self.min_score = 0.0  # retired score-line compatibility field; Core selection owns membership
        # v8 sizing (UI-tunable): 3 σ-tiers, each with margin% + lev cap. Margin uses the adaptive strategy
        # equity base; real risk equity and available cash enforce coin/deployment caps.
        self.add_frac = config.ADD_FRAC if add_frac is None else add_frac  # each ADD = first-open margin × this
        self.stable_sigma_max = config.STABLE_SIGMA_MAX   # σ≤this → stable tier (also lev-formula σ ref)
        self.high_sigma_min = config.HIGH_SIGMA_MIN       # σ≥this → high-vol tier; between → mid tier
        self.tier_margin = {"stable": config.STABLE_MARGIN_PCT, "mid": config.MID_MARGIN_PCT, "high": config.HIGH_MARGIN_PCT}
        self.tier_margin_min = {"stable": config.STABLE_MARGIN_MIN_PCT, "mid": config.MID_MARGIN_MIN_PCT,
                                "high": config.HIGH_MARGIN_MIN_PCT}
        self.tier_lev_cap = {"stable": config.STABLE_LEV_CAP, "mid": config.MID_LEV_CAP, "high": config.HIGH_LEV_CAP}
        # UI-tunable sizing knobs (refreshed from the params table by _reload_params; config = fallback)
        self.max_lev = config.MAX_LEV
        self.min_lev = config.MIN_LEV
        self.stock_max_lev = config.STOCK_MAX_LEV            # hard lev ceiling for stock/builder perps (xyz:*)
        self.coin_blacklist = parse_coin_blacklist(config.COIN_BLACKLIST)
        self.low_liquidity_filter_enable = config.LOW_LIQUIDITY_FILTER_ENABLE
        self.min_coin_day_ntl_vlm = config.MIN_COIN_DAY_NTL_VLM
        self.min_coin_oi_notional = config.MIN_COIN_OI_NOTIONAL
        self.deploy_full_pct = config.DEPLOY_FULL_PCT        # <= this deployed margin: use tier margin upper bound
        self.max_deploy_pct = config.MAX_DEPLOY_PCT          # portfolio deployment cap (new opens stop here; adds may dip in)
        self.min_open_margin_pct = config.MIN_OPEN_MARGIN_PCT
        self.tier_min_notional = {"stable": config.STABLE_MIN_NOTIONAL, "mid": config.MID_MIN_NOTIONAL,
                                  "high": config.HIGH_MIN_NOTIONAL}   # per-tier min order notional ($); skip below
        self.tier_max_adds = {"stable": config.STABLE_MAX_ADDS, "mid": config.MID_MAX_ADDS,
                              "high": config.HIGH_MAX_ADDS}   # per-σ-tier scale-in cap (hardcap mode)
        # ── 加仓策略引擎 (B 逆向): smart(σ波动闸+比例镜像+三档预算) vs hardcap(次数cap+ADD_FRAC) ──
        self.add_strategy = config.ADD_STRATEGY
        self.add_gap_k = config.ADD_GAP_K                       # 波动闸 x = k×σ
        self.pos_add_gap_k = config.POS_ADD_GAP_K               # 顺势加仓也要过价差闸,避免小碎单全跟
        self.add_shrink_g = config.ADD_GAP_SHRINK_G             # 每加一次 x×此
        self.add_max_hard = config.ADD_MAX_HARD                 # smart 硬顶
        self.follow_pos_add = config.FOLLOW_POS_ADD             # A 正向加仓开关(开=过 POS_ADD_GAP_K 才跟)
        self.tier_coin_cap = {"stable": config.STABLE_COIN_CAP_PCT, "mid": config.MID_COIN_CAP_PCT,
                              "high": config.HIGH_COIN_CAP_PCT}  # 三档单币最大保证金占用%
        self.max_entry_chase_pct = config.MAX_ENTRY_CHASE_PCT
        self.vol_fallback_sigma = config.VOL_FALLBACK_SIGMA
        # 扛单 copy-side stop: MARGIN-based catastrophe cut (v10). UI-tunable via _reload_params.
        self.copy_stop_enable = config.COPY_STOP_ENABLE
        # (v10: RISK_BUDGET / σ-scaled leverage removed — leverage is now the σ-tier's cap, see _sizing_for)
        self.stop_margin_pct = config.STOP_MARGIN_PCT        # v10: cut at this fraction of the position's margin
        self.vol: dict = {}              # coin -> σ (read-cache mirror of coin_vol; refreshed off hot path)
        self.vol_coins: set = set()      # coins we've encountered -> the periodic σ-refresh work set
        self.held_off: set = set()       # wallets polled ONLY because we hold a copy (off-watchlist) ->
        #                                  EXIT-ONLY: follow their reduce/close, never open a NEW position
        self.target_acct: dict = {}      # addr -> target's account value (conviction denominator)
        self.target_sector_policy: dict = {}  # addr -> sector allow/deny policy from watchlist
        self.bbo: dict = {}              # coin -> (bid, ask) current top-of-book (any source)
        self.mark_mid: dict = {}         # coin -> authoritative display/risk mark (builder allMids, etc.)
        self.mark_write_ms: dict = {}    # coin -> last DB mark write ms (throttle BBO-triggered writes)
        self.px_ext: dict = {}           # coin -> [lo_bid, hi_ask, reset_ms] rolling ~window extreme, for the
        #                                  maker-shadow 戳破 check: did the price trade THROUGH our resting price?
        self.hb: dict = {}               # per-heartbeat-interval tally (fills seen / copied / skipped-by-reason);
        #                                  taker book only (real copy); reset each _announce. Answers "why no trades".
        self.sub_coins: set = set()      # crypto coins we've sent a WS bbo subscription for
        self.stock_coins: set = set()    # builder/stock coins we price via REST l2Book poll
        self.last_fill_ms: dict = {}     # addr -> cursor (latest processed fill time)
        self.valid_coins: set = set()    # COPYABLE universe (crypto perps + transparent builder)
        self.crypto_coins: set = set()   # standard crypto perps (these price via WS bbo)
        # two isolated paper books; self.balance/open_ep/acct_lock delegate to `taker` (see properties above).
        self.taker = Book("taker", "copy_position", "copy_action", "copy_account", match_exec=False)
        self.maker = Book("maker", "shadow_position", "shadow_action", "shadow_account", match_exec=True)
        self.books = [self.taker] + ([self.maker] if config.SHADOW_MAKER_ENABLED else [])
        # Maker-shadow orders that the target got filled but our simulated queue position has not filled yet.
        # Open actions keep the legacy pending_maker_opens view for tests/debugging; execution uses the generic map.
        self.pending_maker_actions: dict = {}
        self.pending_maker_opens: dict = {}
        self.ws = None
        self.stop = False
        self.paused = False              # dashboard pause: stop opening NEW copies; existing keep to close
        self._proc_state = "running"     # process_status state machine (running|pausing|paused|resuming)
        self._proc_owner = f"observer:{os.getpid()}"

    # -- paper account ------------------------------------------------------
    def _available(self, book=None) -> float:
        """Balance not currently tied up as isolated margin (margin scales with rem_size/size as a
        position is partially closed). Per-book; defaults to the taker book."""
        book = book or self.taker
        locked = self.db.execute(
            f"SELECT COALESCE(SUM(margin * rem_size / size),0) FROM {book.pos_table} "
            "WHERE status='open' AND size>0").fetchone()[0]
        return book.balance - (locked or 0.0)

    def _load_account(self, book=None):
        book = book or self.taker
        row = self.db.execute(f"SELECT initial_balance,balance FROM {book.acct_table} WHERE id=1").fetchone()
        if row:
            book.initial_balance = row[0] or config.INITIAL_BALANCE
            book.balance = row[1]
        else:
            self.db.execute(f"INSERT INTO {book.acct_table} (id,initial_balance,balance,updated_at) "
                            "VALUES (1,?,?,?)", (config.INITIAL_BALANCE, config.INITIAL_BALANCE, now_iso()))
            self.db.commit()
        book.protected_reserve = max(0.0, book.wallet_initial_balance - book.initial_balance)
        closed = self.db.execute(
            f"SELECT COUNT(*),COALESCE(SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END),0) "
            f"FROM {book.pos_table} WHERE status!='open'"
        ).fetchone()
        traded = self.db.execute(
            f"SELECT COALESCE(SUM(ABS(our_qty_delta*our_px)),0) FROM {book.act_table}"
        ).fetchone()[0]
        book.closed_n, book.wins_n = int(closed[0] or 0), int(closed[1] or 0)
        book.gross_traded = float(traded or 0.0)
        book.fees_cum = book.gross_traded * config.TAKER_FEE
        book.stats_loaded = True
        _log(f"account[{book.name}]: balance ${book.balance:,.2f} / available ${self._available(book):,.2f}")

    def _save_account(self, book=None):
        book = book or self.taker
        self.db.execute(f"UPDATE {book.acct_table} SET balance=?, updated_at=? WHERE id=1",
                        (book.balance, now_iso()))

    def _book_unrealized(self, book=None) -> float:
        book = book or self.taker
        total = 0.0
        for (_addr, coin), ep in book.open_ep.items():
            if not ep.get("rem_size"):
                continue
            entry = ep.get("entry_px") or 0.0
            mark = self._mark_px(coin, entry)
            sign = 1 if ep.get("side") == "long" else -1
            total += ep["rem_size"] * (mark - entry) * sign
        return total

    def _risk_equity(self, book=None) -> float:
        book = book or self.taker
        return max(0.0, book.balance + min(0.0, self._book_unrealized(book)))

    def _risk_available(self, book=None) -> float:
        book = book or self.taker
        return max(0.0, self._available(book) + min(0.0, self._book_unrealized(book)))

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

    def _sector_allowed(self, addr: str, coin: str) -> bool:
        return policy_allows_coin(self.target_sector_policy.get((addr or "").lower()), coin, default=True)

    def _manual_close_cooldown_until(self, addr: str, coin: str):
        """Return the active manual-close cooldown expiry for wallet+coin, or None.

        Manual closes are an operator override: after we flatten a wallet's risky coin, the observer should
        stay out of that wallet+coin for a full day even if the master adds, flips, or reopens.
        """
        addr = (addr or "").lower()
        if not addr or not coin:
            return None
        row = self.db.execute(
            "SELECT expires_at FROM manual_close_cooldown WHERE addr=? AND lower(coin)=lower(?)",
            (addr, coin),
        ).fetchone()
        if not row:
            return None
        expires_at = row[0]
        if expires_at > now_iso():
            return expires_at
        self.db.execute(
            "DELETE FROM manual_close_cooldown WHERE addr=? AND lower(coin)=lower(?)",
            (addr, coin),
        )
        self.db.commit()
        return None

    def _add_manual_close_cooldown(self, addr: str, coin: str, pos_id: int):
        addr = (addr or "").lower()
        created_at = now_iso()
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + MANUAL_CLOSE_COOLDOWN_S))
        self.db.execute(
            "INSERT INTO manual_close_cooldown (addr,coin,pos_id,reason,created_at,expires_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(addr,coin) DO UPDATE SET "
            "pos_id=excluded.pos_id,reason=excluded.reason,created_at=excluded.created_at,"
            "expires_at=excluded.expires_at",
            (addr, coin, pos_id, "manual_close", created_at, expires_at),
        )
        self.db.commit()
        return expires_at

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

    def _mark_px(self, coin: str, fallback=None):
        mid = self.mark_mid.get(coin)
        if mid and mid > 0:
            return mid
        ba = self.bbo.get(coin)
        if ba and ba[0] and ba[1]:
            return (ba[0] + ba[1]) / 2
        return fallback

    def _track_px(self, coin, bid, ask):
        """Roll a coin's recent price extreme (reset every MAKER_THROUGH_WINDOW_MS) so the maker-shadow can
        ask: did the price trade THROUGH a resting price lately?"""
        e = self.px_ext.get(coin)
        now = now_ms()
        if e is None or now - e[2] > config.MAKER_THROUGH_WINDOW_MS:
            self.px_ext[coin] = [bid, ask, now]
        else:
            if bid < e[0]:
                e[0] = bid
            if ask > e[1]:
                e[1] = ask

    def _maker_filled(self, coin, is_buy, px):
        """v2 戳破: our resting maker order at `px` fills ONLY if the price traded STRICTLY THROUGH it — we
        sit BEHIND the target in the queue, so 'price merely touched px' means only the target filled, not us.
        Buy rests on the bid → filled iff a bid printed below px; sell rests on the ask → iff an ask above px.
        No recent extreme yet (just-warmed coin) → optimistic True (fall back to v1) so we don't under-count."""
        e = self.px_ext.get(coin)
        if not e:
            return True
        return (e[0] < px) if is_buy else (e[1] > px)

    def _pending_maker_key(self, addr, coin, action, oid, t):
        return (addr, coin, action, oid if oid is not None else t)

    def _drop_pending_maker_action(self, key):
        pending = self.pending_maker_actions.pop(key, None)
        if pending and pending.get("action") == "open":
            self.pending_maker_opens.pop((pending["addr"], pending["coin"]), None)
        return pending

    def _queue_pending_maker_action(self, action, addr, coin, t, signed, pos0, pos1, px, oid,
                                    closing=False, liq=False):
        """Keep a maker-shadow order alive until a later book tick trades strictly through its limit price."""
        if not px:
            return
        if action == "open":
            open_key = (addr, coin)
            if abs(pos1) < config.FLAT or open_key in self.pending_maker_opens or open_key in self.maker.open_ep:
                return
        key = self._pending_maker_key(addr, coin, action, oid, t)
        if key in self.pending_maker_actions:
            return
        is_buy = signed > 0
        side = "long" if pos1 > 0 else "short" if pos1 < 0 else None
        pending = {
            "key": key,
            "action": action,
            "addr": addr,
            "coin": coin,
            "side": side,
            "sign": 1 if (pos1 or signed) > 0 else -1,
            "is_buy": is_buy,
            "t": t,
            "signed": signed,
            "pos0": pos0,
            "pos1": pos1,
            "px": px,
            "oid": oid,
            "closing": closing,
            "liq": liq,
            "queued_ms": now_ms(),
        }
        self.pending_maker_actions[key] = pending
        if action == "open":
            self.pending_maker_opens[(addr, coin)] = pending

    def _queue_pending_maker_open(self, addr, coin, t, pos1, px, oid):
        self._queue_pending_maker_action("open", addr, coin, t, pos1, 0.0, pos1, px, oid)

    def _cancel_pending_maker_open_if_target_left(self, addr, coin, pos1):
        pending = self.pending_maker_opens.get((addr, coin))
        if not pending:
            return
        if pos1 * pending["sign"] <= config.FLAT:
            self._drop_pending_maker_action(pending["key"])

    def _fill_pending_maker_actions(self, coin, bid, ask):
        if not self.pending_maker_actions:
            return
        pending_items = sorted(
            list(self.pending_maker_actions.items()),
            key=lambda kv: (kv[1].get("queued_ms") or 0, kv[0]),
        )
        for key, pending in pending_items:
            if pending["coin"] != coin:
                continue
            px = pending["px"]
            hit = (bid < px) if pending["is_buy"] else (ask > px)
            if not hit:
                continue
            pending = self._drop_pending_maker_action(key)
            if not pending:
                continue
            addr = pending["addr"]
            ep_key = (addr, coin)
            action = pending["action"]
            if action == "open":
                if ep_key in self.maker.open_ep or self._manual_close_cooldown_until(addr, coin):
                    continue
                self._open_position(
                    addr,
                    coin,
                    pending["t"],
                    px,
                    pending["pos1"],
                    True,
                    pending["oid"],
                    self.maker,
                    forced_entry_px=px,
                )
            elif action == "add":
                ep = self.maker.open_ep.get(ep_key)
                if ep is not None:
                    asyncio.create_task(self._apply_add(
                        addr, coin, ep, pending["t"], px, pending["signed"], pending["pos1"],
                        True, pending["oid"], book=self.maker, forced_px=px))
            elif action == "flip":
                ep = self.maker.open_ep.get(ep_key)
                if ep is not None:
                    asyncio.create_task(self._apply_flip(
                        addr, coin, ep, pending["t"], px, pending["pos0"], pending["pos1"],
                        pending["liq"], True, pending["oid"], book=self.maker, forced_px=px))
            else:
                ep = self.maker.open_ep.get(ep_key)
                if ep is not None:
                    asyncio.create_task(self._apply_reduce(
                        addr, coin, ep, pending["t"], px, pending["signed"], pending["pos1"],
                        closing=pending["closing"], liq=pending["liq"], maker=True,
                        oid=pending["oid"], forced_px=px, book=self.maker))

    def _tally(self, key, book=None):
        """Count one heartbeat event for the diagnostic rollup. Only the taker (real copy) book counts;
        the maker shadow is excluded so the numbers reflect actual copy activity. No per-fill log lines —
        the 5-min heartbeat prints the aggregate, so 'why no trades' is answerable at zero log growth."""
        if book is self.maker:
            return
        self.hb[key] = self.hb.get(key, 0) + 1

    # -- restart recovery: reload open copies from db ------------------------
    def _reload_params(self):
        """Refresh UI-tunable strategy params from the params table (engine units; config = fallback).
        Called at startup + each watchlist reload so dashboard edits take effect on the NEXT new copy.
        Fully defensive: any failure keeps the current values (never disrupts the live engine)."""
        try:
            from . import params as P
            f = P.load_follow(self.db)
            if f.get("COIN_BLACKLIST") is not None: self.coin_blacklist = parse_coin_blacklist(f["COIN_BLACKLIST"])
            if f.get("LOW_LIQUIDITY_FILTER_ENABLE") is not None: self.low_liquidity_filter_enable = bool(f["LOW_LIQUIDITY_FILTER_ENABLE"])
            if f.get("MIN_COIN_DAY_NTL_VLM") is not None: self.min_coin_day_ntl_vlm = f["MIN_COIN_DAY_NTL_VLM"]
            if f.get("MIN_COIN_OI_NOTIONAL") is not None: self.min_coin_oi_notional = f["MIN_COIN_OI_NOTIONAL"]
            if f.get("MAX_TARGETS"): self.top_n = int(f["MAX_TARGETS"])
            # (v10: FOLLOW_MIN_TRADES/FOLLOW_MIN_ACTIVE_DAYS dropped — evidence enforced once at profile time)
            if f.get("ADD_FRAC") is not None: self.add_frac = f["ADD_FRAC"]
            if f.get("MAX_LEV"): self.max_lev = f["MAX_LEV"]
            if f.get("MIN_LEV"): self.min_lev = f["MIN_LEV"]
            if f.get("STOCK_MAX_LEV"): self.stock_max_lev = f["STOCK_MAX_LEV"]
            if f.get("DEPLOY_FULL_PCT") is not None: self.deploy_full_pct = f["DEPLOY_FULL_PCT"]
            if f.get("MAX_DEPLOY_PCT"): self.max_deploy_pct = f["MAX_DEPLOY_PCT"]
            if f.get("STABLE_SIGMA_MAX") is not None: self.stable_sigma_max = f["STABLE_SIGMA_MAX"]
            if f.get("HIGH_SIGMA_MIN") is not None: self.high_sigma_min = f["HIGH_SIGMA_MIN"]
            for tier, mk, min_mk, lk, nk, ak in (("stable", "STABLE_MARGIN_PCT", "STABLE_MARGIN_MIN_PCT", "STABLE_LEV_CAP", "STABLE_MIN_NOTIONAL", "STABLE_MAX_ADDS"),
                                                 ("mid", "MID_MARGIN_PCT", "MID_MARGIN_MIN_PCT", "MID_LEV_CAP", "MID_MIN_NOTIONAL", "MID_MAX_ADDS"),
                                                 ("high", "HIGH_MARGIN_PCT", "HIGH_MARGIN_MIN_PCT", "HIGH_LEV_CAP", "HIGH_MIN_NOTIONAL", "HIGH_MAX_ADDS")):
                if f.get(mk) is not None: self.tier_margin[tier] = f[mk]
                if f.get(min_mk) is not None: self.tier_margin_min[tier] = f[min_mk]
                if f.get(lk): self.tier_lev_cap[tier] = f[lk]
                if f.get(nk) is not None: self.tier_min_notional[tier] = f[nk]
                if f.get(ak) is not None: self.tier_max_adds[tier] = int(f[ak])
            if f.get("MIN_OPEN_MARGIN_PCT") is not None: self.min_open_margin_pct = f["MIN_OPEN_MARGIN_PCT"]
            if f.get("SMART_ADD") is not None: self.add_strategy = "smart" if f["SMART_ADD"] else "hardcap"
            if f.get("ADD_GAP_K") is not None: self.add_gap_k = f["ADD_GAP_K"]
            if f.get("POS_ADD_GAP_K") is not None: self.pos_add_gap_k = f["POS_ADD_GAP_K"]
            if f.get("ADD_GAP_SHRINK_G"): self.add_shrink_g = f["ADD_GAP_SHRINK_G"]
            if f.get("ADD_MAX_HARD") is not None: self.add_max_hard = int(f["ADD_MAX_HARD"])
            if f.get("FOLLOW_POS_ADD") is not None: self.follow_pos_add = bool(f["FOLLOW_POS_ADD"])
            for tier, ck in (("stable", "STABLE_COIN_CAP_PCT"), ("mid", "MID_COIN_CAP_PCT"), ("high", "HIGH_COIN_CAP_PCT")):
                if f.get(ck) is not None: self.tier_coin_cap[tier] = f[ck]
            self.max_entry_chase_pct = f.get("MAX_ENTRY_CHASE_PCT")     # None = chase guard off
            if f.get("VOL_FALLBACK_SIGMA"): self.vol_fallback_sigma = f["VOL_FALLBACK_SIGMA"]
            if f.get("COPY_STOP_ENABLE") is not None: self.copy_stop_enable = bool(f["COPY_STOP_ENABLE"])
            # (v10: RISK_BUDGET removed — leverage = σ-tier cap)
            if f.get("STOP_MARGIN_PCT"): self.stop_margin_pct = f["STOP_MARGIN_PCT"]
        except Exception as exc:  # noqa: BLE001
            _log(f"param reload failed (keeping current): {exc}")

    def _reload_open(self, book=None):
        book = book or self.taker
        rows = self.db.execute(
            "SELECT pos_id,addr,coin,side,master_open_ms,master_open_px,master_peak_sz,leverage,"
            "margin,notional,entry_px,size,rem_size,liq_px,realized_pnl,add_count,mae_pct,num_actions,stop_px,"
            f"master_margin,master_leverage FROM {book.pos_table} WHERE status='open'").fetchall()
        loaded = 0
        closed_dust = 0
        for r in rows:
            (pid, addr, coin, side, mo, mpx, peak, lev, mgn, notl, epx, sz, rem, liq, rpnl, adds, mae, na, stopx,
             m_mgn, m_lev) = r
            rem = rem or 0.0
            sz = sz or 0.0
            dust_px = epx or ((notl or 0.0) / sz if sz else 0.0)
            if dust_px > 0 and reduce_leaves_dust(rem, 0.0, dust_px):
                self._close_reloaded_dust(book, pid, addr, coin, side, rem, dust_px)
                closed_dust += 1
                continue
            ev = asyncio.Event()
            if epx is not None:
                ev.set()
            book.open_ep[(addr, coin)] = {
                "pos_id": pid, "side": side, "sign": 1 if side == "long" else -1,
                "master_open_ms": mo, "master_open_px": mpx, "master_peak": peak or 0.0,
                "open_maker": False, "open_oid": None, "leverage": lev or 0.0, "margin": mgn or 0.0,
                "notional": notl or 0.0, "entry_px": epx, "size": sz, "rem_size": rem,
                "liq_px": liq or 0.0, "stop_px": stopx or 0.0, "realized_pnl": rpnl or 0.0,
                "add_count": adds or 0, "entries_ready": ev, "lock": asyncio.Lock(),
                # smart-add restart recovery: first_margin ≈ margin/(1+adds·frac); master首仓额 from open snapshot;
                # 波动闸 reference resets to current avg (safe — next add just needs a fresh x move from here).
                "first_margin": (mgn or 0.0) / (1 + (adds or 0) * self.add_frac),
                "master_first_notl": (m_mgn or 0.0) * (m_lev or 0.0), "last_add_px": epx,
                "mae": mae or 0.0, "num_actions": na or 0, "gap": False,
                "seen_oids": {o for (o,) in self.db.execute(   # orders already consumed (restart-safe)
                    f"SELECT DISTINCT master_oid FROM {book.act_table} WHERE pos_id=? AND action IN "
                    "('open','add')", (pid,)).fetchall() if o is not None}}
            loaded += 1
        if loaded or closed_dust:
            extra = f", closed {closed_dust} dust" if closed_dust else ""
            _log(f"reloaded {loaded} open {book.name} copy positions from db{extra}")

    def _close_reloaded_dust(self, book, pos_id, addr, coin, side, rem_size, px):
        sign = 1 if side == "long" else -1
        t = now_ms()
        self.db.execute(
            f"INSERT INTO {book.act_table} "
            "(pos_id,addr,coin,ts,recv_ms,action,maker,master_oid,master_px,master_sz_delta,"
            "master_pos_after,our_qty_delta,our_px,realized_pnl,slippage_bps) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pos_id, addr, coin, t, t, "close", 0, None, px, 0.0, 0.0,
             -rem_size * sign, px, 0.0, 0.0),
        )
        self.db.execute(
            f"UPDATE {book.pos_table} SET rem_size=0,status='closed',closed_at=?,"
            "num_actions=COALESCE(num_actions,0)+1,mark_px=?,unrealized_pnl=0 WHERE pos_id=?",
            (now_iso(), px, pos_id),
        )
        if book.stats_loaded:
            book.closed_n += 1
            book.gross_traded += abs(rem_size * px)
            book.fees_cum += abs(rem_size * px) * config.TAKER_FEE
        self.db.commit()

    async def _reconcile_open(self):
        """Startup state-reconcile (replaces the deleted time-based backfill for EXITS). Forward-only
        means we can't see a master's close that happened while we were down → a reloaded copy could
        orphan-hold. So for ONLY the wallets we still hold a copy on, fetch the master's CURRENT
        positions (clearinghouseState); if the master no longer holds ours (flat on that coin, or
        flipped to the opposite side), close our copy now at the live book. Masters still in the
        position (same side) are left untouched — forward polling follows their next action."""
        held = sorted({addr for book in self.books for (addr, _) in book.open_ep})
        for addr in held:
            # standard perp + each builder dex we hold a position on (stock perps aren't in the
            # standard clearinghouseState — without the dex they'd read as flat and get wrong-closed).
            dexes = sorted({c.split(":")[0] for book in self.books for (a, c) in book.open_ep if a == addr and ":" in c})
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
            for book in self.books:
                for (a, coin), ep in list(book.open_ep.items()):
                    if a != addr:
                        continue
                    m = szi.get(coin, 0.0)                # master's signed size on this coin, now
                    still = (m > config.FLAT) if ep["side"] == "long" else (m < -config.FLAT)
                    if still:
                        continue                          # master still in it (same side) -> keep & follow
                    ba = await asyncio.to_thread(rest.book_top, coin)
                    mid = ((ba[0] + ba[1]) / 2) if ba else ep["entry_px"]
                    await self._apply_reduce(addr, coin, ep, now_ms(), mid, 0.0, 0.0,
                                             closing=True, liq=False, maker=False, gap=True, forced_px=mid, book=book)
                    _log(f"[{book.name}] RECONCILE-CLOSE {addr[:10]} {coin} {ep['side']} @ {mid:g} "
                         f"pnl=${ep['realized_pnl']:+,.1f}  bal=${book.balance:,.0f} (master no longer holds it)")

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
        self.target_sector_policy = {
            (r[0] or "").lower(): parse_json_obj(r[1])
            for r in self.db.execute("SELECT addr, sector_policy_json FROM watchlist").fetchall()
        }
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

    def _tier(self, sigma: float, coin: str = None) -> str:
        """BTC alone may enter stable; every other market starts at mid and can rise to high by σ."""
        return tier_for_sigma(sigma, self.stable_sigma_max, self.high_sigma_min, coin)

    def _stop_px_for(self, entry_px: float, is_buy: bool, leverage: float = 0.0) -> float:
        """Stop PRICE from entry (v10, MARGIN-based): cut when the position's unrealized loss reaches
        STOP_MARGIN_PCT of its margin. For an isolated position margin-loss% = leverage × adverse-price-%,
        so the adverse move that costs STOP_MARGIN_PCT of margin is (STOP_MARGIN_PCT ÷ leverage) — down for
        a long, up for a short (same geometry as liq_px). Leverage-aware and coin-agnostic, and always
        before liquidation (liq is at 1.0/lev = 100% of margin > STOP_MARGIN_PCT/lev). 0 = disabled."""
        if not self.copy_stop_enable or not entry_px or not leverage:
            return 0.0
        return engine_stop_px(entry_px, is_buy, leverage, self.copy_stop_enable, self.stop_margin_pct)

    def _open_sizing_params(self, book=None):
        book = book or self.taker
        return OpenSizingParams(
            stable_sigma_max=self.stable_sigma_max,
            high_sigma_min=self.high_sigma_min,
            tier_margin=self.tier_margin,
            tier_margin_min=self.tier_margin_min,
            tier_lev_cap=self.tier_lev_cap,
            tier_min_notional=self.tier_min_notional,
            tier_coin_cap=self.tier_coin_cap,
            min_lev=self.min_lev,
            stock_max_lev=self.stock_max_lev,
            deploy_full_pct=self.deploy_full_pct,
            max_deploy_pct=self.max_deploy_pct,
            min_open_margin_pct=self.min_open_margin_pct,
            copy_stop_enable=self.copy_stop_enable,
            stop_margin_pct=self.stop_margin_pct,
            capital_anchor=book.initial_balance,
            drawdown_exponent=config.SIZING_DRAWDOWN_EXPONENT,
            drawdown_max_multiplier=config.SIZING_DRAWDOWN_MAX_MULTIPLIER,
        )

    def _coin_liquidity_block_reason(self, coin: str):
        if not self.low_liquidity_filter_enable or not coin or ":" in coin:
            return None
        row = self.db.execute(
            "SELECT day_ntl_vlm,oi_notional FROM coin_vol WHERE coin=?",
            (coin,),
        ).fetchone()
        if not row:
            return None
        day_ntl_vlm, oi_notional = row[0], row[1]
        if day_ntl_vlm is None or oi_notional is None:
            return None
        if day_ntl_vlm < self.min_coin_day_ntl_vlm:
            return "day_volume"
        if oi_notional < self.min_coin_oi_notional:
            return "open_interest"
        return None

    async def _ensure_vol(self, coin: str):
        """Track coin for the periodic σ refresh, and fetch it NOW if we have no fresh value (so a
        first-seen coin gets a real σ within seconds; sizing uses the fallback only in the meantime)."""
        if not coin:
            return
        self.vol_coins.add(coin)
        needs_market_ctx = False
        if self.low_liquidity_filter_enable and ":" not in coin:
            row = self.db.execute(
                "SELECT day_ntl_vlm,oi_notional FROM coin_vol WHERE coin=?",
                (coin,),
            ).fetchone()
            needs_market_ctx = (not row) or row[0] is None or row[1] is None
        if coin not in self.vol or needs_market_ctx:
            self.vol[coin] = await asyncio.to_thread(volatility.refresh, self.db, coin)

    async def prewarm_vol(self):
        """Warm σ for the top-N-by-24h-volume crypto + each builder dex at startup (background, gentle):
        the liquid coins our targets are likeliest to trade get σ before their first fill — no first-open
        latency, warm restart. The long tail is still lazy-fetched on first fill. Skips already-warm coins."""
        for dex in (None, *rest.BUILDER_DEXES):
            ctxs = await asyncio.to_thread(rest.asset_contexts, dex)
            def _day_vlm(item):
                try:
                    return float((item[1] or {}).get("dayNtlVlm") or 0.0)
                except (TypeError, ValueError):
                    return 0.0
            for coin, ctx in sorted(ctxs.items(), key=_day_vlm, reverse=True)[:config.VOL_PREWARM_TOP]:
                if coin in self.vol or self.stop:
                    continue
                self.vol_coins.add(coin)
                try:
                    self.vol[coin] = await asyncio.to_thread(volatility.refresh, self.db, coin, ctx)
                except Exception:  # noqa: BLE001
                    pass
        _log(f"vol prewarmed: {len(self.vol)} coins (top {config.VOL_PREWARM_TOP}/pool by 24h vol)")

    async def vol_refresh_loop(self):
        """Periodically re-compute σ for every tracked coin into coin_vol — OFF the signal hot path, so
        sizing only ever reads the cache. Catches a calm→volatile regime change within VOL_REFRESH_S."""
        while not self.stop:
            await asyncio.sleep(config.VOL_REFRESH_S)
            ctxs = await asyncio.to_thread(rest.asset_contexts)
            for coin in list(self.vol_coins):
                try:
                    self.vol[coin] = await asyncio.to_thread(volatility.refresh, self.db, coin, ctxs.get(coin))
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
        mark_updates = []
        for pos_id, coin, side, rem, size, entry, margin, notional in self.db.execute(
                "SELECT pos_id,coin,side,rem_size,size,entry_px,margin,notional FROM copy_position "
                "WHERE status='open' AND size>0").fetchall():
            mark = self._mark_px(coin, entry or 0)
            sgn = 1 if side == "long" else -1
            pos_upnl = rem * (mark - (entry or 0)) * sgn
            upnl += pos_upnl
            locked += margin * rem / size
            cur_notl = notional * rem / size
            gross += cur_notl
            net += cur_notl * sgn
            mark_updates.append((mark, pos_upnl, pos_id))
        if mark_updates:
            self.db.executemany(                      # one prepared statement/transaction for all live positions
                "UPDATE copy_position SET mark_px=?, unrealized_pnl=? WHERE pos_id=?", mark_updates)
        open_n = self.db.execute("SELECT count(*) FROM copy_position WHERE status='open'").fetchone()[0]
        if not self.taker.stats_loaded:
            self._load_account(self.taker)
        closed_n = self.taker.closed_n
        win_rate = self.taker.wins_n / closed_n if closed_n else 0.0
        equity = self.balance + upnl
        self.db.execute(
            "INSERT INTO account_stats (ts,balance,unrealized_pnl,equity,realized_pnl_cum,roi,open_n,"
            "closed_n,win_rate,locked_margin,available,gross_notional,net_notional,fees_cum) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), self.balance, upnl, equity, self.balance - init, equity / init - 1,
             open_n, closed_n, win_rate, locked, self.balance - locked, gross, net, self.taker.fees_cum))
        self.db.commit()

    def _refresh_marks(self, book=None):
        """Mark-to-market open positions into the book's position table (mark_px/unrealized_pnl) WITHOUT
        appending an account_stats row. Lets the read-only dashboard show near-real-time浮盈. Per-book."""
        book = book or self.taker
        updates = []
        for pos_id, coin, side, rem, size, entry in self.db.execute(
                f"SELECT pos_id,coin,side,rem_size,size,entry_px FROM {book.pos_table} "
                "WHERE status='open' AND size>0").fetchall():
            mark = self._mark_px(coin)
            if not mark:
                continue
            sgn = 1 if side == "long" else -1
            updates.append((mark, rem * (mark - (entry or 0)) * sgn, pos_id))
        if updates:
            self.db.executemany(
                f"UPDATE {book.pos_table} SET mark_px=?, unrealized_pnl=? WHERE pos_id=?", updates)
        self.db.commit()

    def _refresh_coin_marks(self, coin: str, book=None) -> int:
        """Persist live mark/unrealized PnL for one coin only. Used by BBO/l2Book ticks so the dashboard
        does not wait for the slower full mark_refresh_loop."""
        book = book or self.taker
        mark = self._mark_px(coin)
        if not (coin and mark):
            return 0
        n = 0
        updates = []
        for pos_id, side, rem, entry in self.db.execute(
                f"SELECT pos_id,side,rem_size,entry_px FROM {book.pos_table} "
                "WHERE status='open' AND coin=? AND size>0", (coin,)).fetchall():
            sgn = 1 if side == "long" else -1
            updates.append((mark, rem * (mark - (entry or 0)) * sgn, pos_id))
            n += 1
        if n:
            self.db.executemany(
                f"UPDATE {book.pos_table} SET mark_px=?, unrealized_pnl=? WHERE pos_id=?", updates)
            self.db.commit()
        return n

    def _refresh_coin_marks_throttled(self, coin: str):
        now = now_ms()
        if now - self.mark_write_ms.get(coin, 0) < MARK_WRITE_MIN_MS:
            return
        wrote = 0
        for b in self.books:
            wrote += self._refresh_coin_marks(coin, b)
        if wrote:
            self.mark_write_ms[coin] = now

    async def mark_refresh_loop(self):
        """Frequent mark refresh for dashboard freshness (between the 5-min account_stats snapshots)."""
        while not self.stop:
            await asyncio.sleep(25)
            try:
                for b in self.books:
                    self._refresh_marks(b)
            except Exception as exc:  # noqa: BLE001 — never let dashboard freshness kill the engine
                _log(f"mark refresh failed: {exc}")

    async def _announce(self):
        while not self.stop:
            await asyncio.sleep(300)
            if not self.taker.stats_loaded:
                self._load_account(self.taker)
            o, c = len(self.open_ep), self.taker.closed_n
            h, self.hb = self.hb, {}           # snapshot + reset this interval's diagnostic tally
            seen = h.get("seen", 0)
            acts = {k[4:]: v for k, v in h.items() if k.startswith("act_")}   # open/add/reduce/stop/close
            skips = {k[5:]: v for k, v in h.items() if k.startswith("skip_")}
            act_s = ", ".join(f"{k} {v}" for k, v in sorted(acts.items(), key=lambda x: -x[1])) or "-"
            skip_s = ", ".join(f"{k} {v}" for k, v in sorted(skips.items(), key=lambda x: -x[1])) or "-"
            _log(f"heartbeat: {o} open / {c} closed | 本轮看到 {seen} → 跟 {sum(acts.values())} ({act_s}), "
                 f"跳 {sum(skips.values())} ({skip_s})")
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
            stats_cutoff = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(time.time() - config.ACCOUNT_STATS_RETENTION_DAYS * 86400),
            )
            self.db.execute("DELETE FROM account_stats WHERE ts < ?", (stats_cutoff,))
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
            return await self._cmd_close(int(payload["positionId"]), float(payload.get("fraction", 1.0)))
        if ctype == "close_all":
            return await self._cmd_close_all()
        if ctype == "wallet_toggle":
            return self._cmd_wallet_toggle(payload["address"], bool(payload["enabled"]))
        if ctype == "reload_params":               # UI saved follow params or Core membership changed
            self._reload_params()
            self._reload_targets()
            return {"reloaded": True, "source": "published_core", "targets": len(self.addrs)}
        raise ValueError(f"unhandled command type {ctype}")

    def _ep_by_pos(self, pos_id):
        for (addr, coin), ep in self.open_ep.items():
            if ep.get("pos_id") == pos_id:
                return addr, coin, ep
        return None

    async def _cmd_close(self, pos_id, frac=1.0):
        """Manual close of one live copy at the current book (operator exit). `frac` ∈ (0,1] closes that
        fraction of the remaining size — <100% is a partial reduce (position stays open; freed margin
        returns to available via rem_size/size). Reuses the normal reduce path so PnL/account/status
        finalize identically to a master-driven close."""
        found = self._ep_by_pos(pos_id)
        if not found:
            raise ValueError(f"position {pos_id} not open/live")
        addr, coin, ep = found
        if ep.get("entry_px") is None:
            raise ValueError(f"position {pos_id} still opening")
        frac = max(0.0, min(1.0, frac))
        if frac <= 0:
            raise ValueError("fraction must be > 0")
        ba = self.bbo.get(coin)
        if ba and ba[0] and ba[1]:
            exit_px = ba[0] if ep.get("sign", 1) > 0 else ba[1]
        else:
            exit_px = ep["entry_px"]
        await self._apply_reduce(addr, coin, ep, now_ms(), exit_px, 0.0, 0.0,
                                 closing=(frac >= 0.999), liq=False, maker=False,
                                 forced_px=exit_px, forced_frac=frac)
        full = frac >= 0.999
        cooldown_until = self._add_manual_close_cooldown(addr, coin, pos_id) if full else None
        _log(f"MANUAL-{'CLOSE' if full else f'REDUCE {int(round(frac*100))}%'} {addr[:10]} {coin} {ep['side']} "
             f"@ {exit_px:g}  pnl=${ep['realized_pnl']:+,.1f}  bal=${self.balance:,.0f}")
        return {"positionId": pos_id, "exit": exit_px, "fraction": frac, "closed": full,
                "realizedPnl": round(ep["realized_pnl"], 2), "remSize": round(ep["rem_size"], 8),
                "cooldownUntil": cooldown_until}

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
        sem = asyncio.Semaphore(config.POLL_CONCURRENCY)

        async def _poll_one(addr):                 # CONCURRENT fetch — the pacer serializes each POST's SPAWN,
            since = self.last_fill_ms.get(addr, now_ms()) - config.POLL_OVERLAP_MS   # but the network RTTs overlap
            async with sem:
                try:
                    await self._poll_fills(addr, since)
                except Exception as exc:  # noqa: BLE001 — one wallet's failure must not abort the whole round
                    _log(f"poll_fills {addr[:10]} error: {exc}")

        while not self.stop:
            if now_ms() - last_reload > config.WATCHLIST_RELOAD_S * 1000:
                self._reload_params()              # params FIRST (pick up UI edits) ...
                self._reload_targets()             # ... then load targets with the fresh follow line
                last_reload = now_ms()
            await asyncio.gather(*(_poll_one(a) for a in list(self.addrs)))
            await asyncio.sleep(1)                 # small breath between rounds

    # -- REST poll of targets' resting orders (limit ladders + TP/SL) --------
    async def poll_orders(self):
        """Every ORDER_POLL_S, snapshot each target's open orders via frontendOpenOrders and persist to
        target_orders — their INTENTIONS (maker entries, take-profit/stop levels) ahead of execution.
        Diff-based: orders that vanish flip to 'gone'. Display/analysis only (NOT the copy hot path), so
        it's polled infrequently — it used to run near-continuously and steal ~half the REST budget from
        the fill signal, roughly doubling copy LAG."""
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
            await asyncio.sleep(config.ORDER_POLL_S)   # display-only intentions → poll infrequently; the global
            #                                            pacer already spaces the calls, so no per-wallet sleep needed

    # -- PRICING for builder/stock perps (WS bbo can't serve builder dexes) ---
    async def poll_stock_mids(self):
        """Keep builder/stock dashboard marks fresh from allMids.

        This is separate from l2Book warming so live marks are never blocked behind the slower global
        REST pacer used by fill polling and book-top requests."""
        last_log = 0
        while not self.stop:
            coins = sorted(self.stock_coins)
            try:
                mids = await asyncio.to_thread(rest.all_mids, "xyz", True)
                for coin in coins:
                    mid = f(mids.get(coin)) if isinstance(mids, dict) else 0.0
                    if mid > 0:
                        self.mark_mid[coin] = mid
                        self._refresh_coin_marks_throttled(coin)
                        for book in self.books:
                            for (a, c), ep in book.open_ep.items():
                                if c == coin and ep["master_open_px"]:
                                    adv = ((ep["master_open_px"] - mid) if ep["side"] == "long"
                                           else (mid - ep["master_open_px"])) / ep["master_open_px"]
                                    ep["mae"] = max(ep.get("mae", 0.0), adv)
                            self._maybe_liquidate(coin, mid, book)
                            self._maybe_stop(coin, mid, book)
                if coins and time.time() - last_log > 300:
                    _log(f"stock mids refreshed: {len(coins)} coins")
                    last_log = time.time()
            except Exception as exc:  # noqa: BLE001
                _log(f"stock mids refresh failed: {exc}")
            await asyncio.sleep(2 if self.stock_coins else 5)

    async def poll_stock_books(self):
        """Keep best bid/ask warm for stock execution pricing. Round-robin: marks come from allMids."""
        book_i = 0
        while not self.stop:
            coins = sorted(self.stock_coins)

            if coins:
                coin = coins[book_i % len(coins)]
                book_i += 1
                try:
                    ba = await asyncio.to_thread(rest.book_top, coin)
                except Exception as exc:  # noqa: BLE001
                    _log(f"stock book {coin} failed: {exc}")
                    ba = None
                if ba and ba[0] and ba[1] and ba[0] > 0 and ba[1] > 0:   # need a REAL two-sided book —
                    if ba[1] < ba[0]:                                    # crossed/garbage book → distrust this tick
                        ba = None
                    if ba:
                        self.bbo[coin] = ba                              # used for execution bid/ask
                        self._track_px(coin, ba[0], ba[1])               # roll extreme for the maker 戳破 check
                        self._fill_pending_maker_actions(coin, ba[0], ba[1])
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
        for b in self.books:                       # restore each paper account + its open copies (restart-safe)
            self._load_account(b)
            self._reload_open(b)
        self.vol = volatility.load_all(self.db)    # warm the σ read-cache from coin_vol (restart-safe)
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
        asyncio.create_task(self.poll_stock_mids())   # stock/commodity marks (REST allMids, fast)
        asyncio.create_task(self.poll_stock_books())  # stock/commodity top-of-book (REST l2Book, slower)
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
        if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:   # crossed/zero book → junk tick, ignore
            return
        self.bbo[coin] = (bid, ask)
        self._refresh_coin_marks_throttled(coin)
        self._track_px(coin, bid, ask)             # roll the recent extreme for the maker 戳破 check
        self._fill_pending_maker_actions(coin, bid, ask)
        mid = (bid + ask) / 2
        for book in self.books:                    # both accounts: track MAE + run stops per book
            for (a, c), ep in book.open_ep.items():    # track worst adverse excursion while open
                if c == coin and ep["master_open_px"]:
                    adv = ((ep["master_open_px"] - mid) if ep["side"] == "long"
                           else (mid - ep["master_open_px"])) / ep["master_open_px"]
                    ep["mae"] = max(ep.get("mae", 0.0), adv)
            self._maybe_liquidate(coin, mid, book)     # isolated stop-out if price crossed liq_px
            self._maybe_stop(coin, mid, book)          # 扛单 copy-side stop if price crossed stop_px

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
        self._tally("seen")                 # a fresh target fill reached us (proves ingestion is alive)
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

        for book in self.books:                # BOTH books copy every fill; they differ only in HOW we execute it
            self._dispatch_fill(book, addr, coin, key, t, signed, pos0, pos1, px, liq, maker, oid)

    def _dispatch_fill(self, book, addr, coin, key, t, signed, pos0, pos1, px, liq, maker, oid):
        # our_maker = do WE rest a maker order on this fill? taker book always crosses (False); maker book
        # matches the target (their maker fill → we rest; their taker fill → we cross). Drives fill price + fee.
        our_maker = maker if book.match_exec else False
        transition = classify_fill_transition(pos0, pos1)
        target_in_position = abs(pos1) >= config.FLAT
        cooldown_until = self._manual_close_cooldown_until(addr, coin) if target_in_position else None
        ep = book.open_ep.get(key)
        if book is self.maker:
            self._cancel_pending_maker_open_if_target_left(addr, coin, pos1)
        if our_maker and not self._maker_filled(coin, signed > 0, px):
            if book is self.maker:
                if ep is None:
                    if cooldown_until:
                        self._tally("skip_manual_cooldown", book)
                    elif (transition in ("open", "flip") and target_in_position
                            and addr not in self.held_off
                            and not self.paused
                            and self._sector_allowed(addr, coin)):
                        self._queue_pending_maker_open(addr, coin, t, pos1, px, oid)
                elif transition == "flip":
                    ep["master_peak"] = max(ep["master_peak"], abs(pos0))
                    self._queue_pending_maker_action(
                        "flip", addr, coin, t, signed, pos0, pos1, px, oid, closing=True, liq=liq)
                elif transition == "add":
                    ep["master_peak"] = max(ep["master_peak"], abs(pos1))
                    if oid is not None and oid in ep.get("seen_oids", ()):
                        return
                    ep.setdefault("seen_oids", set()).add(oid)
                    if self.paused or addr in self.held_off or not self._sector_allowed(addr, coin):
                        self._tally("skip_paused_add" if self.paused else
                                    "skip_heldoff_add" if addr in self.held_off else
                                    "skip_sector_add", book)
                        return
                    self._queue_pending_maker_action(
                        "add", addr, coin, t, signed, pos0, pos1, px, oid)
                else:
                    ep["master_peak"] = max(ep["master_peak"], abs(pos1))
                    closing = abs(pos1) < config.FLAT
                    self._queue_pending_maker_action(
                        "close" if closing else "reduce", addr, coin, t, signed, pos0, pos1,
                        px, oid, closing=closing, liq=liq)
            return   # v2 戳破: price didn't trade THROUGH our resting price → our maker order didn't fill (miss)
        if ep is None:
            if transition in ("open", "flip") and target_in_position:
                if cooldown_until:
                    self._tally("skip_manual_cooldown", book)
                elif (addr not in self.held_off       # held-off (off-watchlist) = exit-only, no new opens
                        and not self.paused           # dashboard pause = no new opens (existing keep to close)
                        and self._sector_allowed(addr, coin)):
                    self._open_position(addr, coin, t, px, pos1, our_maker, oid, book)
                else:
                    self._tally("skip_paused" if self.paused else
                                "skip_heldoff" if addr in self.held_off else
                                "skip_sector_disabled" if not self._sector_allowed(addr, coin) else
                                "skip_midway", book)
            elif abs(pos1) < config.FLAT:
                pass                                    # target closed a position we never held — nothing to copy
            else:                                       # a fresh open we chose not to take → tally the reason
                self._tally("skip_manual_cooldown" if cooldown_until else
                            "skip_paused" if self.paused else
                            "skip_heldoff" if addr in self.held_off else
                            "skip_sector_disabled" if not self._sector_allowed(addr, coin) else
                            "skip_midway", book)         # midway = target already in the position when we saw it
            return
        if transition == "flip":
            ep["master_peak"] = max(ep["master_peak"], abs(pos0))
            asyncio.create_task(self._apply_flip(addr, coin, ep, t, px, pos0, pos1, liq, our_maker, oid, book))
            return
        ep["master_peak"] = max(ep["master_peak"], abs(pos1))
        if transition == "add":
            # A scale-in is a NEW ORDER (new oid) growing the position. Same-oid continued fills are
            # one resting order filling over time (slices) — aggregateByTime only merges same-INSTANT
            # fills, so a limit order filling over several seconds reappears as same-oid fills; counting
            # those as adds is the slice-as-add bug. Fold them in (peak already tracked above), no add.
            if oid is not None and oid in ep.get("seen_oids", ()):
                return
            ep.setdefault("seen_oids", set()).add(oid)
            if self.paused or addr in self.held_off or not self._sector_allowed(addr, coin):
                self._tally("skip_paused_add" if self.paused else
                            "skip_heldoff_add" if addr in self.held_off else
                            "skip_sector_add", book)
                return
            asyncio.create_task(self._apply_add(addr, coin, ep, t, px, signed, pos1, our_maker, oid, book))
        else:
            asyncio.create_task(self._apply_reduce(addr, coin, ep, t, px, signed, pos1,
                                                   closing=abs(pos1) < config.FLAT, liq=liq, maker=our_maker, oid=oid, book=book))

    async def _apply_flip(self, addr, coin, ep, t, master_px, pos0, pos1, liq, maker, oid,
                          book=None, forced_px=None):
        book = book or self.taker
        await self._apply_reduce(addr, coin, ep, t, master_px, -pos0, 0.0,
                                 closing=True, liq=liq, maker=maker, oid=oid, book=book, forced_px=forced_px)
        if (addr, coin) in book.open_ep:
            return
        if (addr in self.held_off or self.paused or not self._sector_allowed(addr, coin)
                or self._manual_close_cooldown_until(addr, coin)):
            return
        self._open_position(addr, coin, t, master_px, pos1, maker, oid, book, forced_entry_px=forced_px)

    def _open_position(self, addr, coin, t, px, pos1, maker, oid, book=None, forced_entry_px=None):
        book = book or self.taker
        if coin_is_blacklisted(coin, self.coin_blacklist):
            self._tally("skip_coin_blacklist", book)
            return
        if not self._copyable(coin):
            self._tally("skip_opaque", book)
            return              # copy crypto + transparent builder (stocks); skip opaque/unknown
        side = "long" if pos1 > 0 else "short"
        lag_sec = max(0.0, (now_ms() - t) / 1000.0)   # copy latency: master fill -> our detection (dashboard)
        cur = self.db.execute(
            f"INSERT INTO {book.pos_table} (addr,coin,side,status,master_open_ms,master_open_px,"
            "master_peak_sz,opened_at,num_actions,open_lag_sec) VALUES (?,?,?,'open',?,?,?,?,0,?)",
            (addr, coin, side, t, px, abs(pos1), now_iso(), lag_sec))
        ep = {"pos_id": cur.lastrowid, "side": side, "sign": 1 if side == "long" else -1,
              "master_open_ms": t, "master_open_px": px, "master_peak": abs(pos1),
              "open_maker": maker, "open_oid": oid, "leverage": 0.0, "margin": 0.0, "notional": 0.0,
              "entry_px": None, "size": 0.0, "rem_size": 0.0, "liq_px": 0.0, "stop_px": 0.0, "realized_pnl": 0.0,
              "add_count": 0, "entries_ready": asyncio.Event(), "lock": asyncio.Lock(), "mae": 0.0,
              "num_actions": 0, "gap": False, "seen_oids": {oid}}   # orders consumed (open + real adds)
        if forced_entry_px is not None:
            ep["forced_entry_px"] = forced_entry_px
        book.open_ep[(addr, coin)] = ep
        asyncio.create_task(self._resolve_entry(addr, coin, ep, t, px, book))
        return ep

    async def _resolve_entry(self, addr, coin, ep, t, master_px, book=None):
        book = book or self.taker
        is_buy = ep["side"] == "long"                # opening a long => we buy
        stale = (now_ms() - t) > STALE_MS            # backfilled-late: book is no longer the fill's
        forced_entry_px = ep.get("forced_entry_px")
        px = forced_entry_px if forced_entry_px is not None else (
            master_px if stale else self._fill_px(coin, is_buy, ep["open_maker"], master_px)
        )
        if not px or px <= 0 or not master_px or master_px <= 0:   # can't price it -> don't hold a 0-price
            self.db.execute(f"DELETE FROM {book.pos_table} WHERE pos_id=?", (ep["pos_id"],))  # position (also
            self.db.commit()                                                              # avoids /0 below)
            book.open_ep.pop((addr, coin), None)
            self._tally("skip_unpriceable", book)
            _log(f"skip {coin}: unpriceable (px={px}, master_px={master_px}) — not followed")
            return
        chase = (px - master_px) / master_px * 1e4 * ep["sign"]   # bps worse than master (+ = worse)
        we_rest = ep["open_maker"] and config.EXEC_MAKER_MIRROR    # only a true maker-mirror rests (no chase)
        if (self.max_entry_chase_pct is not None and not we_rest
                and chase > self.max_entry_chase_pct * 100):       # spike too far past master -> skip
            self.db.execute(f"DELETE FROM {book.pos_table} WHERE pos_id=?", (ep["pos_id"],))
            self.db.commit()
            book.open_ep.pop((addr, coin), None)
            self._tally("skip_chase", book)
            return                                            # chase-skip: price ran past master before we detected
        await self._ensure_vol(coin)                 # fetch THIS coin's real σ once (else first open = fallback)
        liquidity_reason = self._coin_liquidity_block_reason(coin)
        if liquidity_reason:
            self.db.execute(f"DELETE FROM {book.pos_table} WHERE pos_id=?", (ep["pos_id"],))
            self.db.commit()
            book.open_ep.pop((addr, coin), None)
            self._tally("skip_low_liquidity", book)
            _log(f"skip {coin} {ep['side']} {addr[:10]}: low liquidity ({liquidity_reason})")
            return
        master_cap, m_lev, m_mgn, m_entry = await asyncio.to_thread(self._target_snapshot, addr, coin)  # master ctx
        # v10 sizing: σ → tier (stable/mid/high) → margin% + leverage = the tier's LEV CAP
        #  margin = adaptive sizing equity × <tier>_margin_pct
        #  lev    = <tier>_lev_cap (clipped MIN/MAX_LEV, then ≤ master lev + stock cap)
        #  notional = margin·lev. NOT mirrored from the master (σ alone sizes us). A calm coin (BTC, GOLD)
        #  lands in the stable tier with big margin + high lev; a wild one (ZEC/meme) in high tier, small.
        sigma = self._sigma(coin)
        async with book.acct_lock:                   # serialize margin allocation across opens
            # Dynamic equity-based sizing: below DEPLOY_FULL_PCT, use the tier's upper-bound margin; between
            # DEPLOY_FULL_PCT and MAX_DEPLOY_PCT, linearly shrink new opens toward the lower bound. Adds still
            # may dip into the reserve because they usually matter more to copy fidelity than fresh opens.
            risk_equity = self._risk_equity(book)
            avail = self._risk_available(book)           # cash gate after recognizing floating losses
            # PER-COIN cap (catastrophe backstop, NOT a per-wallet tax): total margin across our open positions
            # on this coin IN THE SAME DIRECTION ≤ the σ-tier's per-coin cap (STABLE/MID/HIGH_COIN_CAP_PCT).
            # Bounds how much of the account one coin's single move can destroy when N wallets pile the SAME way
            # (e.g. all short BTC). Same-direction ONLY on purpose: an opposite-side signal (a wallet flips long
            # while we hold shorts) OFFSETS our exposure, it doesn't stack it, so it must NOT be blocked.
            existing_coin = sum(e.get("margin", 0.0) * (e["rem_size"] / e["size"] if e.get("size") else 1.0)
                                for (a2, c2), e in book.open_ep.items()   # EFFECTIVE margin (partial-close aware)
                                if c2 == coin and e.get("side") == ep["side"] and e is not ep)
            target_notl = abs(ep["master_peak"]) * master_px if master_px else 0.0
            master_notl = (m_mgn or 0.0) * (m_lev or 0.0) or target_notl
            margin_row = self.db.execute(
                "SELECT max_leverage FROM coin_vol WHERE coin=?", (coin,),
            ).fetchone()
            maintenance_leverage = margin_row[0] if margin_row and margin_row[0] else None
            plan = plan_open_sizing(
                coin=coin,
                side=ep["side"],
                entry_px=px,
                sigma=sigma,
                balance=risk_equity,
                available=avail,
                existing_coin_margin=existing_coin,
                master_notional=master_notl,
                master_leverage=m_lev,
                params=self._open_sizing_params(book),
                maintenance_leverage=maintenance_leverage,
            )
            if not plan.ok:
                self._tally(f"skip_{plan.reason}", book)
                if plan.reason == "small_notl":
                    why = f"below {plan.tier}-tier min notl ${plan.notional:,.0f} < ${self.tier_min_notional.get(plan.tier, 0.0):,.0f} (master notl ${plan.master_notional:,.0f})"
                else:
                    why = plan.reason
                _log(f"skip {coin} {ep['side']} {addr[:10]}: {why} (room ${plan.room:,.0f} / deploy ${plan.deploy_room:,.0f} / cash ${plan.available:,.0f} / want ${plan.wanted_margin:,.0f})")
                self.db.execute(f"DELETE FROM {book.pos_table} WHERE pos_id=?", (ep["pos_id"],))  # -> skip
                self.db.commit()
                book.open_ep.pop((addr, coin), None)
                return
            lev = plan.leverage
            margin = plan.margin
            notional = plan.notional
            size = plan.size
            liq_px = plan.liq_px
            stop_px = plan.stop_px
            ep.update(leverage=lev, margin=margin, notional=notional, entry_px=px, first_margin=margin,
                      size=size, rem_size=size, liq_px=liq_px, stop_px=stop_px,
                      master_first_notl=master_notl,      # 目标首仓名义额 → smart 加仓比例基准
                      last_add_px=px)                     # 我们上次入场价 → smart 波动闸基准 (adds = first_margin × ...)
            self.db.execute(                         # also persist the TARGET's lev/margin/entry at open
                f"UPDATE {book.pos_table} SET leverage=?,margin=?,notional=?,entry_px=?,size=?,rem_size=?,"
                "liq_px=?,stop_px=?,master_leverage=?,master_margin=?,master_open_px=COALESCE(?,master_open_px) "
                "WHERE pos_id=?",
                (lev, margin, notional, px, size, size, liq_px, stop_px, m_lev, m_mgn, m_entry, ep["pos_id"]))
            book.balance -= abs(size * px) * (config.MAKER_FEE if ep["open_maker"] else config.TAKER_FEE)  # OPEN fee
            self._save_account(book)
            self.db.commit()
        ep["entries_ready"].set()
        msz = ep["master_peak"] * ep["sign"]
        self._record_action(ep, addr, coin, t, "open", ep["open_maker"], ep["open_oid"], master_px,
                            msz, msz, size * ep["sign"], px, 0.0, chase, book=book)
        self.db.commit()                                      # the open is in copy_position/copy_action

    async def _apply_add(self, addr, coin, ep, t, master_px, signed, pos1, maker, oid,
                         book=None, forced_px=None):
        """Master scaled in -> we follow (average down/up) up to MAX_ADDS, each add committing
        first_margin × ADD_FRAC (half the first-open by default) at the current price; avg entry + liq_px recompute.
        Past the cap we record his add but don't follow (the delta-based exit still mirrors him)."""
        book = book or self.taker
        async with ep["lock"]:
            try:
                await asyncio.wait_for(ep["entries_ready"].wait(), timeout=12)
            except asyncio.TimeoutError:
                pass
            if ep.get("entry_px") is None or (addr, coin) not in book.open_ep:
                return
            # 源(目标)加权均价:每次目标加仓都把 master_open_px 更新为其 size 加权均价(此前只存首开价 →
            # 多次加仓的"源"价没更新、和我们的均价没法比)。用目标的真实仓位量(pos1 = 加仓后仓位,signed = 本笔量)。
            # 即使超过我们的跟随上限(下面 observe-only 分支)也更新 —— 目标仍在摊他的均价。
            m_now = abs(pos1)
            if m_now > 0 and master_px and ep.get("master_open_px"):
                m_prev = abs(pos1 - signed)           # 目标加仓前的仓位量
                ep["master_open_px"] = (m_prev * ep["master_open_px"] + abs(signed) * master_px) / m_now
            is_buy = ep["side"] == "long"             # adding to a long => buy more
            stale = (now_ms() - t) > STALE_MS
            px = forced_px if forced_px is not None else (
                master_px if stale else self._fill_px(coin, is_buy, maker, master_px)
            )
            lev = ep["leverage"]
            sigma = self._sigma(coin); tier = self._tier(sigma, coin)
            fm = ep.get("first_margin", ep["margin"])

            def _observe_only():                      # record his add, DON'T follow; keep the source avg fresh
                self._record_action(ep, addr, coin, t, "add", maker, oid, master_px, signed, pos1,
                                    0.0, master_px, 0.0, 0.0, book=book)
                self.db.execute(f"UPDATE {book.pos_table} SET master_open_px=? WHERE pos_id=?",
                                (ep["master_open_px"], ep["pos_id"]))
                self.db.commit()

            if self._coin_liquidity_block_reason(coin):
                self._tally("skip_low_liquidity_add", book)
                return _observe_only()

            if self.add_strategy == "smart":
                # 逆向(adv>0=价格朝我们不利方向,摊低)走 ADD_GAP_K;
                # 正向(adv<0=顺势加仓)也要过 POS_ADD_GAP_K,避免 1.01/1.02/1.03 这类小碎追单全跟。
                last = ep.get("last_add_px") or ep["entry_px"]
                adv = (((last - master_px) if is_buy else (master_px - last)) / last) if last else 0.0
                gap_mult = self.add_shrink_g ** ep["add_count"]
                x = self.add_gap_k * sigma * gap_mult
                pos_x = self.pos_add_gap_k * sigma * gap_mult
                if adv >= x:                                     # ① B 逆向:摊低幅度够 → 跟
                    pass
                elif adv < 0 and self.follow_pos_add and abs(adv) >= pos_x:
                    pass
                else:                                            # 逆向但幅度不够 / 正向但A关 → 只观察
                    return _observe_only()
                if ep["add_count"] >= self.add_max_hard:         # 硬顶(A/B 共用)
                    return _observe_only()
                # ③ 比例镜像(目标本次加仓额 ÷ 目标首仓额)× 我们首仓,封顶到该币"三档单币预算"剩余
                ratio = (abs(signed) * master_px) / ep["master_first_notl"] if ep.get("master_first_notl") else self.add_frac
                async with book.acct_lock:
                    risk_equity = self._risk_equity(book)
                    coin_cap = self.tier_coin_cap[tier] * risk_equity
                    existing = sum(e.get("margin", 0.0) * (e["rem_size"] / e["size"] if e.get("size") else 1.0)
                                   for (a2, c2), e in book.open_ep.items()
                                   if c2 == coin and e.get("side") == ep["side"])   # incl THIS ep (its current margin)
                    add_margin = max(0.0, min(ratio * fm, coin_cap - existing, self._risk_available(book)))
                if add_margin < self.min_open_margin_pct * risk_equity:  # 预算用尽 / 太小,不值得
                    return _observe_only()
            else:                                     # hardcap: 分档次数上限 + 固定 ADD_FRAC(老逻辑)
                if ep["add_count"] >= self.tier_max_adds.get(tier, 0):
                    return _observe_only()
                async with book.acct_lock:
                    risk_equity = self._risk_equity(book)
                    coin_cap = self.tier_coin_cap[tier] * risk_equity
                    existing = sum(
                        e.get("margin", 0.0) * (e["rem_size"] / e["size"] if e.get("size") else 1.0)
                        for (a2, c2), e in book.open_ep.items()
                        if c2 == coin and e.get("side") == ep["side"]
                    )
                    add_margin = max(0.0, min(
                        fm * self.add_frac,
                        coin_cap - existing,
                        self._risk_available(book),
                    ))
                if add_margin <= 0:
                    return _observe_only()
            add_size = (add_margin * lev / px) if px else 0.0
            new_size = ep["rem_size"] + add_size
            ep["entry_px"] = ((ep["rem_size"] * ep["entry_px"] + add_size * px) / new_size
                              if new_size else px)    # size-weighted average entry
            ep["rem_size"] = new_size
            ep["size"] += add_size
            ep["margin"] += add_margin
            ep["notional"] += add_margin * lev
            margin_row = self.db.execute(
                "SELECT max_leverage FROM coin_vol WHERE coin=?", (coin,),
            ).fetchone()
            ep["liq_px"] = isolated_liq_px(
                ep["entry_px"], ep["side"], ep["size"], ep["margin"],
                margin_row[0] if margin_row and margin_row[0] else None, lev,
            )
            ep["stop_px"] = self._stop_px_for(ep["entry_px"], is_buy, lev)  # re-anchor margin-stop to new avg entry
            ep["add_count"] += 1
            ep["last_add_px"] = px                    # advance the smart 波动闸 reference to this fill
            ep["reduce_anchor"] = None                # master grew → invalidate the reduce-step window
            slip = (px - master_px) / master_px * 1e4 * ep["sign"] if master_px else 0.0
            self._record_action(ep, addr, coin, t, "add", maker, oid, master_px, signed, pos1,
                                add_size * ep["sign"], px, 0.0, slip, book=book)
            book.balance -= abs(add_size * px) * (config.MAKER_FEE if maker else config.TAKER_FEE)  # ADD fee
            self._save_account(book)
            self.db.execute(
                f"UPDATE {book.pos_table} SET margin=?,notional=?,entry_px=?,size=?,rem_size=?,liq_px=?,stop_px=?,"
                "add_count=?,master_open_px=? WHERE pos_id=?", (ep["margin"], ep["notional"], ep["entry_px"], ep["size"],
                ep["rem_size"], ep["liq_px"], ep["stop_px"], ep["add_count"], ep["master_open_px"], ep["pos_id"]))
            self.db.commit()                                  # the add is in the action table

    async def _apply_reduce(self, addr, coin, ep, t, master_px, signed, pos1, closing, liq, maker,
                            oid=None, gap=False, forced_px=None, stop=False, forced_frac=None, book=None):
        book = book or self.taker
        async with ep["lock"]:
            try:
                await asyncio.wait_for(ep["entries_ready"].wait(), timeout=12)
            except asyncio.TimeoutError:
                pass
            if ep.get("entry_px") is None or (addr, coin) not in book.open_ep:
                return
            is_buy = ep["side"] == "short"           # closing a long => sell; closing a short => buy
            stale = (now_ms() - t) > STALE_MS
            exit_px = (forced_px if forced_px is not None
                       else master_px if stale else self._fill_px(coin, is_buy, maker, master_px))
            # delta-based: close the SAME fraction of our position the master just closed of his —
            # correct for any build-up (adds we followed, adds we skipped past the cap, or none).
            if forced_frac is not None:                       # operator manual close: EXACT fraction of rem_size
                reduce_frac = max(0.0, min(1.0, forced_frac))
                closing = reduce_frac >= 0.999                # <100% keeps the position OPEN (partial reduce)
            elif closing or abs(pos1 - signed) < config.FLAT:
                reduce_frac = 1.0                             # full close always executes → exact flat
            else:
                # STEP-mirror: an algo master unwinds a big position in 100s of tiny orders. Instead of
                # mirroring every dust reduce, only act once his cumulative unwind since our last reduce
                # reaches REDUCE_STEP_FRAC of his position (→ ≤~10 partial reduces). `reduce_anchor` = his
                # |position| at our last executed reduce (re-anchored to pre-fill size if he grew via an add).
                pos0 = pos1 - signed
                anchor = ep.get("reduce_anchor")
                if not anchor or anchor <= abs(pos1):         # first reduce, or he added since → re-anchor
                    anchor = abs(pos0)
                cum_frac = (anchor - abs(pos1)) / anchor if anchor else 0.0
                if cum_frac < config.REDUCE_STEP_FRAC:
                    ep["reduce_anchor"] = anchor              # accumulate; skip this sub-step (no fill/log)
                    return
                reduce_frac = min(1.0, cum_frac)              # rem still matches `anchor` → cut the whole ratio
                ep["reduce_anchor"] = abs(pos1)               # open a fresh 10% window from here
            if not closing and reduce_leaves_dust(ep["rem_size"], reduce_frac, exit_px):
                reduce_frac = 1.0
                closing = True
            close_size = ep["rem_size"] * reduce_frac
            fee = abs(close_size * exit_px) * (config.MAKER_FEE if maker else config.TAKER_FEE)  # exit fee (per our_maker)
            pnl = close_size * (exit_px - ep["entry_px"]) * ep["sign"] - fee    # NET of our exit fee
            ep["rem_size"] -= close_size
            ep["realized_pnl"] += pnl
            book.balance += pnl                       # realize (net of fee) into the paper account
            slip = (master_px - exit_px) / master_px * 1e4 * ep["sign"] if master_px else 0.0
            action = "close" if closing else "reduce"
            self._record_action(ep, addr, coin, t, action, maker, oid, master_px, signed, pos1,
                                -close_size * ep["sign"], exit_px, pnl, slip, book=book)
            status = ("liquidated" if (closing and liq) else "stopped" if (closing and stop)
                      else "gap_closed" if (closing and gap) else "closed" if closing else "open")
            was_liq = 1 if (closing and liq) else 0
            was_stopped = 1 if (closing and stop) else 0
            ep["was_liq"] = was_liq
            ep["was_stopped"] = was_stopped
            self.db.execute(
                f"UPDATE {book.pos_table} SET rem_size=?,realized_pnl=?,mae_pct=?,was_liq=?,was_stopped=?,status=?,"
                "closed_at=? WHERE pos_id=?", (ep["rem_size"], ep["realized_pnl"], ep["mae"],
                was_liq, was_stopped, status,
                now_iso() if closing else None, ep["pos_id"]))
            self._save_account(book)
            self.db.commit()
            if closing:
                if book.stats_loaded:
                    book.closed_n += 1
                    book.wins_n += 1 if ep["realized_pnl"] > 0 else 0
                book.open_ep.pop((addr, coin), None)         # normal closes are in the position table; only
                if liq:                                       # liquidation (our isolated stop-out) is logged
                    _log(f"[{book.name}] LIQUIDATED {addr[:10]} {coin} {ep['side']} -${ep['margin']:,.0f}  bal=${book.balance:,.0f}")
                elif stop:                                    # 扛单 copy-side stop: cut before the master does
                    _log(f"[{book.name}] STOPPED {addr[:10]} {coin} {ep['side']} pnl=${ep['realized_pnl']:+,.0f}  bal=${book.balance:,.0f}")

    async def _liquidate(self, addr, coin, ep, book=None):
        book = book or self.taker
        if ep.get("liquidating") or (addr, coin) not in book.open_ep:
            return
        ep["liquidating"] = True
        await self._apply_reduce(addr, coin, ep, now_ms(), ep["liq_px"], 0.0, 0.0,
                                 closing=True, liq=True, maker=False, forced_px=ep["liq_px"], book=book)

    def _maybe_liquidate(self, coin, mid, book=None):
        book = book or self.taker
        if not mid or mid <= 0:          # bad/one-sided book tick → never liquidate on a garbage price
            return
        for (a, c), ep in list(book.open_ep.items()):
            if c == coin and ep.get("liq_px") and ep["rem_size"] > config.FLAT and not ep.get("liquidating"):
                hit = mid <= ep["liq_px"] if ep["side"] == "long" else mid >= ep["liq_px"]
                if hit:
                    asyncio.create_task(self._liquidate(a, coin, ep, book))

    async def _stop_out(self, addr, coin, ep, mid, book=None):
        """扛单 copy-side stop: the target's thesis (mean-revert by ~tp_move) is broken — price ran the
        adverse way past our stop. Exit NOW at the live mid instead of riding it to our far liquidation
        (we don't bag-hold with the target). A real reduce/close on the master still flows normally."""
        book = book or self.taker
        if ep.get("stopping") or ep.get("liquidating") or (addr, coin) not in book.open_ep:
            return
        ep["stopping"] = True
        await self._apply_reduce(addr, coin, ep, now_ms(), mid, 0.0, 0.0,
                                 closing=True, liq=False, maker=False, forced_px=mid, stop=True, book=book)

    def _maybe_stop(self, coin, mid, book=None):
        book = book or self.taker
        if not self.copy_stop_enable:
            return
        if not mid or mid <= 0:          # bad/one-sided book tick → never stop on a garbage price
            return
        for (a, c), ep in list(book.open_ep.items()):
            if (c == coin and ep.get("stop_px") and ep["rem_size"] > config.FLAT
                    and not ep.get("liquidating") and not ep.get("stopping")):
                hit = mid <= ep["stop_px"] if ep["side"] == "long" else mid >= ep["stop_px"]
                if hit:
                    asyncio.create_task(self._stop_out(a, coin, ep, mid, book))

    def _record_action(self, ep, addr, coin, t, action, maker, oid, master_px, sz_delta, pos_after,
                       our_qty_delta, our_px, realized, slip, book=None):
        book = book or self.taker
        self._tally(f"act_{action}", book)   # copy activity by kind (open/add/reduce/stop/close) — taker only
        ep["num_actions"] += 1
        self.db.execute(
            f"INSERT INTO {book.act_table} (pos_id,addr,coin,ts,recv_ms,action,maker,master_oid,master_px,"
            "master_sz_delta,master_pos_after,our_qty_delta,our_px,realized_pnl,slippage_bps) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ep["pos_id"], addr, coin, t, now_ms(), action, 1 if maker else 0, oid, master_px, sz_delta,
             pos_after, our_qty_delta, our_px, realized, slip))
        if book.stats_loaded:
            traded = abs((our_qty_delta or 0.0) * (our_px or 0.0))
            book.gross_traded += traded
            # Preserve the dashboard's historical definition: taker-equivalent fee drag for the primary book.
            book.fees_cum += traded * config.TAKER_FEE
        self.db.execute(f"UPDATE {book.pos_table} SET num_actions=?, master_peak_sz=? WHERE pos_id=?",
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
    """Load only the explicit published Core.

    Before the first successful Selection publication there are deliberately no new-entry targets. Wallets
    with existing copies are still re-added EXIT-ONLY by ``_reload_targets`` for safe position management.
    ``min_score`` remains in the signature only for CLI compatibility and is intentionally ignored.
    """
    explicit = selection.published_core_addrs(db, n)
    addrs = explicit or []
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
    print(f"\n(sizing: σ-tiers margin/lev-cap [stable BTC-only σ≤{config.STABLE_SIGMA_MAX*100:g}%: {config.STABLE_MARGIN_PCT*100:g}%/{config.STABLE_LEV_CAP:g}x · "
          f"mid: {config.MID_MARGIN_PCT*100:g}%/{config.MID_LEV_CAP:g}x · high σ≥{config.HIGH_SIGMA_MIN*100:g}%: {config.HIGH_MARGIN_PCT*100:g}%/{config.HIGH_LEV_CAP:g}x], "
          f"lev=tier cap clipped by market/master max, add={config.ADD_FRAC:g}×first "
          f"(max {config.STABLE_MAX_ADDS}/{config.MID_MAX_ADDS}/{config.HIGH_MAX_ADDS} by tier), isolated)")
