"""Offline replay of target fills through the current copy rules.

This is intentionally pure-Python and DB-free so scans and ad-hoc audits can answer
"would our engine have copied this wallet well?" without starting the live observer.
Historical fills do not include the live BBO seen by the observer, so execution uses
the target fill price plus our fee model; the rule decisions mirror the taker book.
"""
from __future__ import annotations

from collections import Counter
import math
import bisect

from . import config
from .coin_filter import coin_is_blocked, parse_coin_blacklist
from .copy_data import normalize_copyable_fills
from .copy_engine import (OpenSizingParams, extract_master_leverage, isolated_liq_px,
                          plan_open_sizing, profit_tail_close_decision, reduce_leaves_dust,
                          tier_for_sigma)
from .fill_transition import classify_fill_transition
from .util import f


def _row_time(row: dict) -> int:
    for key in ("time", "T", "t"):
        val = row.get(key)
        if val is not None:
            return int(f(val))
    return 0


def _row_price(row: dict, *keys: str) -> float:
    for key in keys:
        val = row.get(key)
        if val is not None:
            out = f(val)
            if out > 0:
                return out
    return 0.0


def _price_events(price_path) -> list[dict]:
    """Normalize optional tick/candle path data into per-coin high/low events."""
    if not price_path:
        return []
    rows = []
    if isinstance(price_path, dict):
        for coin, coin_rows in price_path.items():
            for row in coin_rows or []:
                if isinstance(row, dict):
                    item = dict(row)
                    item.setdefault("coin", coin)
                    rows.append(item)
    else:
        rows = [row for row in price_path if isinstance(row, dict)]

    out = []
    for row in rows:
        coin = row.get("coin")
        if not coin:
            continue
        lo = _row_price(row, "low", "l", "px", "price", "close", "c")
        hi = _row_price(row, "high", "h", "px", "price", "close", "c")
        if lo <= 0 or hi <= 0:
            continue
        if hi < lo:
            lo, hi = hi, lo
        out.append({
            "time": _row_time(row),
            "open_time": int(row.get("open_time") or row.get("t") or _row_time(row)),
            "close_time": int(row.get("close_time") or row.get("T") or _row_time(row)),
            "coin": coin,
            "low": lo,
            "high": hi,
            "close": _row_price(row, "close", "c", "px", "price") or (lo + hi) / 2,
            "interval": row.get("interval"),
        })
    out.sort(key=lambda x: x["time"])
    return out


class Backtest:
    def __init__(self, addr, sigmas=None, initial_balance=None, overrides=None, market_ctx=None,
                 price_path_meta=None, valuation_marks=None, valuation_asof_ms=None):
        overrides = overrides or {}
        self.addr = (addr or "").lower()
        self.sigmas = sigmas or {}
        self.initial_balance = config.INITIAL_BALANCE if initial_balance is None else float(initial_balance)
        self.balance = self.initial_balance
        self.open = {}
        self.closed = []
        self.last_px = {}
        self.skip_reasons = Counter()
        self.target_pos = {}
        self.target_peak_concurrent = 0
        self.copy_peak_concurrent = 0
        self.target_open_events = 0
        self.opened_n = 0
        self.followed_adds = 0
        self.missed_adds = 0
        self.target_adds = 0
        self.fee_drag = 0.0
        self.gross_pnl = 0.0
        self.stable_sigma_max = overrides.get("STABLE_SIGMA_MAX", config.STABLE_SIGMA_MAX)
        self.high_sigma_min = overrides.get("HIGH_SIGMA_MIN", config.HIGH_SIGMA_MIN)
        self.tier_margin = {
            "stable": overrides.get("STABLE_MARGIN_PCT", config.STABLE_MARGIN_PCT),
            "mid": overrides.get("MID_MARGIN_PCT", config.MID_MARGIN_PCT),
            "high": overrides.get("HIGH_MARGIN_PCT", config.HIGH_MARGIN_PCT),
        }
        self.tier_margin_min = {
            "stable": overrides.get("STABLE_MARGIN_MIN_PCT", config.STABLE_MARGIN_MIN_PCT),
            "mid": overrides.get("MID_MARGIN_MIN_PCT", config.MID_MARGIN_MIN_PCT),
            "high": overrides.get("HIGH_MARGIN_MIN_PCT", config.HIGH_MARGIN_MIN_PCT),
        }
        self.tier_lev_cap = {
            "stable": overrides.get("STABLE_LEV_CAP", config.STABLE_LEV_CAP),
            "mid": overrides.get("MID_LEV_CAP", config.MID_LEV_CAP),
            "high": overrides.get("HIGH_LEV_CAP", config.HIGH_LEV_CAP),
        }
        self.tier_min_notional = {
            "stable": overrides.get("STABLE_MIN_NOTIONAL", config.STABLE_MIN_NOTIONAL),
            "mid": overrides.get("MID_MIN_NOTIONAL", config.MID_MIN_NOTIONAL),
            "high": overrides.get("HIGH_MIN_NOTIONAL", config.HIGH_MIN_NOTIONAL),
        }
        self.tier_coin_cap = {
            "stable": overrides.get("STABLE_COIN_CAP_PCT", config.STABLE_COIN_CAP_PCT),
            "mid": overrides.get("MID_COIN_CAP_PCT", config.MID_COIN_CAP_PCT),
            "high": overrides.get("HIGH_COIN_CAP_PCT", config.HIGH_COIN_CAP_PCT),
        }
        self.tier_max_adds = {
            "stable": int(overrides.get("STABLE_MAX_ADDS", config.STABLE_MAX_ADDS)),
            "mid": int(overrides.get("MID_MAX_ADDS", config.MID_MAX_ADDS)),
            "high": int(overrides.get("HIGH_MAX_ADDS", config.HIGH_MAX_ADDS)),
        }
        self.min_lev = overrides.get("MIN_LEV", config.MIN_LEV)
        self.stock_max_lev = overrides.get("STOCK_MAX_LEV", config.STOCK_MAX_LEV)
        self.add_strategy = overrides.get("ADD_STRATEGY", config.ADD_STRATEGY)
        self.add_gap_k = overrides.get("ADD_GAP_K", config.ADD_GAP_K)
        self.pos_add_gap_k = overrides.get("POS_ADD_GAP_K", config.POS_ADD_GAP_K)
        self.add_shrink_g = overrides.get("ADD_GAP_SHRINK_G", config.ADD_GAP_SHRINK_G)
        self.add_max_hard = int(overrides.get("ADD_MAX_HARD", config.ADD_MAX_HARD))
        self.follow_pos_add = bool(overrides.get("FOLLOW_POS_ADD", config.FOLLOW_POS_ADD))
        self.add_frac = overrides.get("ADD_FRAC", config.ADD_FRAC)
        self.deploy_full_pct = overrides.get("DEPLOY_FULL_PCT", config.DEPLOY_FULL_PCT)
        self.max_deploy_pct = overrides.get("MAX_DEPLOY_PCT", config.MAX_DEPLOY_PCT)
        self.margin_equity_pct = overrides.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
        self.min_open_margin_pct = overrides.get("MIN_OPEN_MARGIN_PCT", config.MIN_OPEN_MARGIN_PCT)
        self.tail_close_enable = bool(overrides.get("TAIL_CLOSE_ENABLE", config.TAIL_CLOSE_ENABLE))
        self.tail_close_hard_remain_pct = overrides.get(
            "TAIL_CLOSE_HARD_REMAIN_PCT", config.TAIL_CLOSE_HARD_REMAIN_PCT)
        self.tail_close_risk_remain_pct = overrides.get(
            "TAIL_CLOSE_RISK_REMAIN_PCT", config.TAIL_CLOSE_RISK_REMAIN_PCT)
        self.tail_close_profit_giveback_pct = overrides.get(
            "TAIL_CLOSE_PROFIT_GIVEBACK_PCT", config.TAIL_CLOSE_PROFIT_GIVEBACK_PCT)
        self.coin_blacklist = parse_coin_blacklist(overrides.get("COIN_BLACKLIST", config.COIN_BLACKLIST))
        self.block_korean_stocks = bool(overrides.get("BLOCK_KOREAN_STOCKS", config.BLOCK_KOREAN_STOCKS))
        self.low_liquidity_filter_enable = bool(overrides.get(
            "LOW_LIQUIDITY_FILTER_ENABLE", config.LOW_LIQUIDITY_FILTER_ENABLE))
        self.min_coin_day_ntl_vlm = overrides.get("MIN_COIN_DAY_NTL_VLM", config.MIN_COIN_DAY_NTL_VLM)
        self.min_coin_oi_notional = overrides.get("MIN_COIN_OI_NOTIONAL", config.MIN_COIN_OI_NOTIONAL)
        self.market_ctx = market_ctx or {}
        self.replay_cost_mult = max(1.0, f(overrides.get("REPLAY_COST_MULT", 1.0)))
        self.price_path_points = 0
        self.path_mark_coins = set()
        self.price_path_meta = price_path_meta or {}
        self.valuation_marks = {
            str(coin): f(px) for coin, px in (valuation_marks or {}).items() if f(px) > 0
        }
        self.valuation_asof_ms = int(valuation_asof_ms or 0) or None
        self.path_boundary_skips = 0
        self.ambiguous_path_events = set()
        self.ambiguous_path_mode = str(overrides.get("AMBIGUOUS_PATH_MODE", "ignore") or "ignore")
        self.master_leverage_known = 0
        self.master_leverage_missing = 0
        self.maintenance_margin_known = 0
        self.maintenance_margin_missing = 0
        self.deploy_samples = []

    def open_sizing_params(self):
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
            capital_anchor=self.initial_balance,
            drawdown_exponent=config.SIZING_DRAWDOWN_EXPONENT,
            drawdown_max_multiplier=config.SIZING_DRAWDOWN_MAX_MULTIPLIER,
            margin_equity_pct=self.margin_equity_pct,
        )

    def sigma(self, coin):
        return self.sigmas.get(coin) or config.VOL_FALLBACK_SIGMA

    def tier(self, sigma: float, coin: str | None = None) -> str:
        return tier_for_sigma(sigma, self.stable_sigma_max, self.high_sigma_min, coin)

    def available(self):
        locked = sum(p["margin"] * (p["rem_size"] / p["size"] if p["size"] else 1.0) for p in self.open.values())
        return self.balance - locked

    def locked_margin(self):
        return sum(
            p["margin"] * (p["rem_size"] / p["size"] if p["size"] else 1.0)
            for p in self.open.values()
        )

    def _sample_deploy(self, t=None):
        # Deployment limits are defined against contemporaneous risk equity.  Dividing by the initial
        # balance made a profitable compounding replay report impossible values such as 468% deployed even
        # though the engine was respecting an 80% cap, which in turn falsely blocked every later selection.
        self.deploy_samples.append((
            int(t or 0),
            self.locked_margin() / max(1.0, self.risk_equity()),
        ))

    def unrealized(self):
        total = 0.0
        for (_, coin), ep in self.open.items():
            px = self.last_px.get(coin) or ep["entry_px"]
            total += ep["rem_size"] * (px - ep["entry_px"]) * ep["sign"]
        return total

    def risk_equity(self):
        # Unbanked gains do not increase the next trade; floating losses reduce
        # risk immediately, matching the live sizing path.
        return max(0.0, self.balance + min(0.0, self.unrealized()))

    def risk_available(self):
        return max(0.0, self.available() + min(0.0, self.unrealized()))

    def coin_cap_pct(self, tier):
        return self.tier_coin_cap[tier]

    def liquidity_block_reason(self, coin):
        if not self.low_liquidity_filter_enable or not coin or ":" in coin:
            return None
        ctx = self.market_ctx.get(coin)
        if not ctx:
            return None
        day_ntl_vlm = ctx.get("day_ntl_vlm")
        oi_notional = ctx.get("oi_notional")
        if day_ntl_vlm is None or oi_notional is None:
            return None
        if day_ntl_vlm < self.min_coin_day_ntl_vlm:
            return "day_volume"
        if oi_notional < self.min_coin_oi_notional:
            return "open_interest"
        return None

    def run(self, fills, price_path=None):
        fills = normalize_copyable_fills(
            fills,
            addr=None if self.addr == "portfolio" else self.addr,
        )
        path_events = _price_events(price_path)
        self.price_path_points = len(path_events)
        if not path_events:
            for x in fills:
                self.process_fill(x)
            return self.result()

        fill_times = {}
        for row in fills:
            fill_times.setdefault(row.get("coin"), []).append(int(row.get("time") or 0))
        for times in fill_times.values():
            times.sort()
        for row in path_events:
            times = fill_times.get(row.get("coin")) or []
            lo = bisect.bisect_left(times, int(row.get("open_time") or row["time"]))
            hi = bisect.bisect_right(times, int(row.get("close_time") or row["time"]))
            row["has_fill_events"] = hi > lo
        # Both streams are already sorted. A linear merge avoids allocating and sorting hundreds of
        # thousands of candle/fill tuples for every tuner candidate.
        path_i = fill_i = 0
        while path_i < len(path_events) or fill_i < len(fills):
            path_time = path_events[path_i]["time"] if path_i < len(path_events) else None
            fill_time = int(fills[fill_i].get("time") or 0) if fill_i < len(fills) else None
            if path_time is not None and (fill_time is None or path_time <= fill_time):
                self.process_price(path_events[path_i])
                path_i += 1
            else:
                self.process_fill(fills[fill_i])
                fill_i += 1
        return self.result()

    def process_fill(self, x):
        addr = (x.get("user") or self.addr or "").lower()
        coin = x.get("coin")
        if not coin:
            return
        px = f(x.get("px"))
        if px <= 0:
            return
        self.last_px[coin] = px
        self._mark_liquidations(coin, px, x.get("time"))

        sz = f(x.get("sz"))
        signed = sz if x.get("side") == "B" else -sz
        pos0 = f(x.get("startPosition"))
        pos1 = pos0 + signed
        key = (addr, coin)
        oid = x.get("oid")
        transition = classify_fill_transition(pos0, pos1)

        was_flat = abs(pos0) < config.FLAT
        if transition in ("open", "flip") and abs(pos1) >= config.FLAT:
            self.target_open_events += 1
        if abs(pos1) < config.FLAT:
            self.target_pos.pop(key, None)
        else:
            self.target_pos[key] = pos1
        self.target_peak_concurrent = max(self.target_peak_concurrent, len(self.target_pos))

        ep = self.open.get(key)
        if ep is None:
            if transition in ("open", "flip") and abs(pos1) >= config.FLAT:
                self._open_position(addr, coin, x.get("time"), px, pos1, oid, x)
            elif abs(pos1) >= config.FLAT:
                self.skip_reasons["skip_midway"] += 1
            return

        if transition == "flip":
            ep["master_peak"] = max(ep["master_peak"], abs(pos0))
            self._apply_reduce(addr, coin, px, -pos0, 0.0, closing=True, t=x.get("time"))
            self._open_position(addr, coin, x.get("time"), px, pos1, oid, x)
            return

        ep["master_peak"] = max(ep["master_peak"], abs(pos1))
        if transition == "add":
            add_orders = ep.setdefault("add_orders", {})
            if oid is not None and oid in ep["seen_oids"] and oid not in add_orders:
                return
            # Do not consume an order id until an add was actually copied.  HL
            # can match one order in many slices; the first tiny slice may miss
            # the smart-add gap while a later slice of that same order reaches
            # it.  Marking the oid before the decision permanently hid those
            # later actionable slices.
            if self._apply_add(addr, coin, px, signed, pos1, oid, t=x.get("time")) and oid is not None:
                ep["seen_oids"].add(oid)
        else:
            self._apply_reduce(addr, coin, px, signed, pos1, closing=abs(pos1) < config.FLAT, t=x.get("time"))

    def process_price(self, x):
        coin = x.get("coin")
        if not coin:
            return
        lo = f(x.get("low"))
        hi = f(x.get("high"))
        if lo <= 0 or hi <= 0:
            return
        if hi < lo:
            lo, hi = hi, lo
        close = f(x.get("close")) or (lo + hi) / 2
        self.last_px[coin] = close
        self.path_mark_coins.add(coin)
        self._mark_liquidations_range(
            coin, lo, hi, x.get("time"), candle_open_time=x.get("open_time"),
            ambiguous=bool(x.get("has_fill_events")), candle_close_time=x.get("close_time"),
        )

    def _open_position(self, addr, coin, t, px, pos1, oid, fill=None):
        if coin_is_blocked(coin, self.coin_blacklist, block_korean_stocks=self.block_korean_stocks):
            self.skip_reasons["skip_coin_blacklist"] += 1
            return
        if self.liquidity_block_reason(coin):
            self.skip_reasons["skip_low_liquidity"] += 1
            return
        sigma = self.sigma(coin)
        side = "long" if pos1 > 0 else "short"
        sign = 1 if side == "long" else -1
        target_notl = abs(pos1) * px
        risk_equity = self.risk_equity()
        avail = self.risk_available()
        existing_coin = sum(
            p["margin"] * (p["rem_size"] / p["size"] if p["size"] else 1.0)
            for (addr, c), p in self.open.items()
            if c == coin and p["side"] == side
        )
        master_lev = extract_master_leverage(fill)
        if master_lev:
            self.master_leverage_known += 1
        else:
            self.master_leverage_missing += 1
        maintenance_leverage = (self.market_ctx.get(coin) or {}).get("max_leverage")
        if maintenance_leverage:
            self.maintenance_margin_known += 1
        else:
            self.maintenance_margin_missing += 1
        plan = plan_open_sizing(
            coin=coin,
            side=side,
            entry_px=px,
            sigma=sigma,
            balance=risk_equity,
            available=avail,
            existing_coin_margin=existing_coin,
            master_notional=target_notl,
            master_leverage=master_lev,
            params=self.open_sizing_params(),
            maintenance_leverage=maintenance_leverage,
        )
        tier = plan.tier
        if not plan.ok:
            why = plan.reason
            self.skip_reasons[f"skip_{why}"] += 1
            return
        margin = plan.margin
        notional = plan.notional
        lev = plan.leverage
        size = plan.size
        fee = abs(size * px) * config.TAKER_FEE * self.replay_cost_mult
        self.balance -= fee
        self.fee_drag += fee
        is_buy = side == "long"
        self.open[(addr, coin)] = {
            "addr": addr,
            "coin": coin,
            "side": side,
            "sign": sign,
            "opened_at": t,
            "master_open_px": px,
            "master_peak": abs(pos1),
            "master_first_notl": target_notl,
            "target_initial_notl": target_notl,
            "target_add_notl": 0.0,
            "target_adds": 0,
            "entry_px": px,
            "size": size,
            "rem_size": size,
            "peak_size": size,
            "margin": margin,
            "first_margin": margin,
            "notional": notional,
            "leverage": lev,
            "maintenance_leverage": maintenance_leverage,
            "master_leverage": master_lev,
            "liq_px": plan.liq_px,
            "last_add_px": px,
            "add_count": 0,
            "followed_adds": 0,
            "missed_adds": 0,
            "entry_fees": fee,
            "exit_fees": 0.0,
            "gross_pnl": 0.0,
            "realized_net": -fee,
            "seen_oids": ({oid} if oid is not None else set()),
            "add_orders": {},
            "observed_add_oids": set(),
            "missed_add_oids": set(),
            "reduce_anchor": None,
        }
        self.opened_n += 1
        self.copy_peak_concurrent = max(self.copy_peak_concurrent, len(self.open))
        self._sample_deploy(t)

    def _observe_add(self, ep, oid=None):
        # Count one missed target add ORDER, not every exchange fill slice.
        if oid is None or oid not in ep["missed_add_oids"]:
            ep["missed_adds"] += 1
            self.missed_adds += 1
            if oid is not None:
                ep["missed_add_oids"].add(oid)
        return False

    def _apply_add(self, addr, coin, px, signed, pos1, oid, t=None):
        ep = self.open[(addr, coin)]
        m_now = abs(pos1)
        if m_now > 0 and ep["master_open_px"]:
            m_prev = abs(pos1 - signed)
            ep["master_open_px"] = (m_prev * ep["master_open_px"] + abs(signed) * px) / m_now
        add_notl = abs(signed) * px
        ep["target_add_notl"] += add_notl
        order = None
        decision_px = px
        target_order_notl = add_notl
        if oid is not None and self.add_strategy == "smart":
            order = ep.setdefault("add_orders", {}).setdefault(oid, {
                "target_notl": 0.0,
                "target_abs_sz": 0.0,
                "target_px_notl": 0.0,
                "followed_margin": 0.0,
                "counted": False,
                "base_add_count": ep.get("add_count", 0),
            })
            order["target_notl"] += add_notl
            order["target_abs_sz"] += abs(signed)
            order["target_px_notl"] += abs(signed) * px
            target_order_notl = order["target_notl"]
            if order["target_abs_sz"] > 0:
                decision_px = order["target_px_notl"] / order["target_abs_sz"]
        if oid is None or oid not in ep["observed_add_oids"]:
            ep["target_adds"] += 1
            self.target_adds += 1
            if oid is not None:
                ep["observed_add_oids"].add(oid)

        sigma = self.sigma(coin)
        tier = self.tier(sigma, coin)
        is_buy = ep["side"] == "long"
        risk_equity = self.risk_equity()
        existing = sum(
            p["margin"] * (p["rem_size"] / p["size"] if p["size"] else 1.0)
            for (addr, c), p in self.open.items()
            if c == coin and p["side"] == ep["side"]
        )
        coin_room = max(0.0, self.coin_cap_pct(tier) * risk_equity - existing)
        if self.add_strategy == "smart":
            last = ep.get("last_add_px") or ep["entry_px"]
            adv = (((last - decision_px) if is_buy else (decision_px - last)) / last) if last else 0.0
            base_add_count = order["base_add_count"] if order else ep["add_count"]
            gap_mult = self.add_shrink_g ** base_add_count
            threshold = self.add_gap_k * sigma * gap_mult
            pos_threshold = self.pos_add_gap_k * sigma * gap_mult
            already_counted = bool(order and order["counted"])
            if not already_counted:
                if adv >= threshold:
                    pass
                elif adv < 0 and self.follow_pos_add and abs(adv) >= pos_threshold:
                    pass
                else:
                    return self._observe_add(ep, oid)
                if ep["add_count"] >= self.add_max_hard:
                    return self._observe_add(ep, oid)
            ratio = target_order_notl / ep["master_first_notl"] if ep["master_first_notl"] else self.add_frac
            followed_margin = order["followed_margin"] if order else 0.0
            desired_total = min(
                ratio * ep["first_margin"],
                followed_margin + coin_room,
                followed_margin + self.risk_available(),
            )
            add_margin = max(0.0, desired_total - followed_margin)
            if add_margin < self.min_open_margin_pct * risk_equity:
                return False if already_counted else self._observe_add(ep, oid)
        else:
            max_adds = self.tier_max_adds[tier]
            if ep["add_count"] >= max_adds:
                return self._observe_add(ep, oid)
            add_margin = max(0.0, min(
                ep["first_margin"] * self.add_frac,
                coin_room,
                self.risk_available(),
            ))
            if add_margin <= 0:
                return self._observe_add(ep, oid)

        add_size = (add_margin * ep["leverage"] / px) if px else 0.0
        new_size = ep["rem_size"] + add_size
        ep["entry_px"] = ((ep["rem_size"] * ep["entry_px"] + add_size * px) / new_size if new_size else px)
        ep["rem_size"] = new_size
        ep["size"] += add_size
        ep["peak_size"] = max(ep.get("peak_size", 0.0), new_size)
        ep["margin"] += add_margin
        ep["notional"] += add_margin * ep["leverage"]
        ep["liq_px"] = isolated_liq_px(
            ep["entry_px"], ep["side"], ep["size"], ep["margin"],
            ep.get("maintenance_leverage"), ep["leverage"],
        )
        first_copy_for_order = not (order and order["counted"])
        if first_copy_for_order:
            ep["add_count"] += 1
            ep["followed_adds"] += 1
            self.followed_adds += 1
        ep["last_add_px"] = decision_px
        ep["reduce_anchor"] = None
        fee = abs(add_size * px) * config.TAKER_FEE * self.replay_cost_mult
        ep["entry_fees"] += fee
        ep["realized_net"] -= fee
        self.balance -= fee
        self.fee_drag += fee
        if order is not None:
            order["followed_margin"] += add_margin
            order["counted"] = True
        # A later slice of a previously rejected order can make the order
        # actionable.  In that case it is followed, not both missed and
        # followed, at order granularity.
        if oid is not None and oid in ep["missed_add_oids"]:
            ep["missed_add_oids"].remove(oid)
            ep["missed_adds"] = max(0, ep["missed_adds"] - 1)
            self.missed_adds = max(0, self.missed_adds - 1)
        self._sample_deploy(t)
        return True

    def _apply_reduce(self, addr, coin, px, signed, pos1, closing=False, status="closed", t=None):
        key = (addr, coin)
        ep = self.open.get(key)
        if not ep:
            return
        if closing or abs(pos1 - signed) < config.FLAT:
            reduce_frac = 1.0
            closing = True
        else:
            pos0 = pos1 - signed
            anchor = ep.get("reduce_anchor")
            if not anchor or anchor <= abs(pos1):
                anchor = abs(pos0)
            reduce_frac = (anchor - abs(pos1)) / anchor if anchor else 0.0
            if reduce_frac < config.REDUCE_STEP_FRAC:
                ep["reduce_anchor"] = anchor
                return
            reduce_frac = min(1.0, reduce_frac)
            ep["reduce_anchor"] = abs(pos1)
        dust_close = not closing and reduce_leaves_dust(ep["rem_size"], reduce_frac, px)
        if dust_close:
            reduce_frac = 1.0
            closing = True
            status = "closed"
        elif not closing:
            decision = profit_tail_close_decision(
                rem_size=ep["rem_size"],
                peak_size=ep.get("peak_size") or max(ep["size"], ep["rem_size"]),
                reduce_frac=reduce_frac,
                execution_px=px,
                risk_px=self.last_px.get(coin) or px,
                entry_px=ep["entry_px"],
                side=ep["side"],
                realized_pnl=ep["gross_pnl"] - ep["exit_fees"],
                liq_px=ep.get("liq_px", 0.0),
                fee_rate=config.TAKER_FEE * self.replay_cost_mult,
                enabled=self.tail_close_enable,
                hard_remain_pct=self.tail_close_hard_remain_pct,
                risk_remain_pct=self.tail_close_risk_remain_pct,
                max_profit_giveback_pct=self.tail_close_profit_giveback_pct,
            )
            if decision.close:
                reduce_frac = 1.0
                closing = True
                status = "tail_closed"
        close_size = ep["rem_size"] * reduce_frac
        gross = close_size * (px - ep["entry_px"]) * ep["sign"]
        fee = abs(close_size * px) * config.TAKER_FEE * self.replay_cost_mult
        pnl = gross - fee
        ep["rem_size"] -= close_size
        ep["gross_pnl"] += gross
        ep["exit_fees"] += fee
        ep["realized_net"] += pnl
        self.gross_pnl += gross
        self.fee_drag += fee
        self.balance += pnl
        if closing:
            ep["closed_at"] = t
            ep["status"] = status
            self.closed.append(ep)
            self.open.pop(key, None)
        self._sample_deploy(t)

    def _mark_liquidations(self, coin, px, t):
        for (addr, c), ep in list(self.open.items()):
            if c != coin:
                continue
            liq_hit = px <= ep["liq_px"] if ep["side"] == "long" else px >= ep["liq_px"]
            if liq_hit:
                self._apply_reduce(addr, coin, ep["liq_px"], 0.0, 0.0, closing=True, status="liquidated", t=t)

    def _mark_liquidations_range(self, coin, low, high, t, candle_open_time=None,
                                 ambiguous=False, candle_close_time=None):
        for (addr, c), ep in list(self.open.items()):
            if c != coin:
                continue
            # A candle's low/high may have occurred before a position opened inside that candle. Applying
            # the entire range would create false liquidations. Boundary candles remain explicitly
            # unresolved until a finer path is available.
            boundary = candle_open_time is not None and int(ep.get("opened_at") or 0) > int(candle_open_time)
            if ep["side"] == "long":
                liq_hit = low <= ep["liq_px"]
            else:
                liq_hit = high >= ep["liq_px"]
            if (ambiguous or boundary) and liq_hit:
                self.path_boundary_skips += 1
                self.ambiguous_path_events.add((coin, int(candle_open_time or t or 0),
                                                int(candle_close_time or t or 0)))
                if self.ambiguous_path_mode != "liquidate":
                    continue
            if liq_hit:
                self._apply_reduce(addr, coin, ep["liq_px"], 0.0, 0.0, closing=True, status="liquidated", t=t)

    def result(self):
        unreal = 0.0
        valued_open = 0
        missing_mark_coins = []
        open_positions = []
        for (_, coin), ep in self.open.items():
            terminal_mark = self.valuation_marks.get(coin)
            path_mark = self.last_px.get(coin) if coin in self.path_mark_coins else None
            mark_px = terminal_mark or path_mark
            mark_valid = bool(mark_px and mark_px > 0)
            if mark_valid:
                valued_open += 1
            else:
                missing_mark_coins.append(coin)
                # Retain the historical fallback for diagnostics only. Qualification consumes
                # valuation_status and must not treat a last fill as a trustworthy current mark.
                mark_px = self.last_px.get(coin) or ep["entry_px"]
            position_unreal = ep["rem_size"] * (mark_px - ep["entry_px"]) * ep["sign"]
            unreal += position_unreal
            open_positions.append(summarize_position(
                ep, mark_px=mark_px, unrealized_pnl=position_unreal,
                valuation_complete=mark_valid,
            ))
        closed_net = sum(p["realized_net"] for p in self.closed)
        wins = sum(1 for p in self.closed if p["realized_net"] > 0)
        liquidations = sum(1 for p in self.closed if p.get("status") == "liquidated")
        tail_profit_closes = sum(1 for p in self.closed if p.get("status") == "tail_closed")
        natural_closes = max(0, len(self.closed) - liquidations)
        path_completion_rate = natural_closes / len(self.closed) if self.closed else 1.0
        initial_notl = sum(p["target_initial_notl"] for p in self.closed) + sum(p["target_initial_notl"] for p in self.open.values())
        add_notl = sum(p["target_add_notl"] for p in self.closed) + sum(p["target_add_notl"] for p in self.open.values())
        capacity_skips = sum(self.skip_reasons[k] for k in ("skip_coin_full", "skip_no_cash", "skip_deploy_cap", "skip_margin_too_small"))
        equity_pnl = self.balance - self.initial_balance + unreal
        curve = []
        equity = self.initial_balance
        peak = equity
        max_drawdown = 0.0
        daily_pnl = {}
        ordered_closed = sorted(self.closed, key=lambda p: int(p.get("closed_at") or 0))
        for position in ordered_closed:
            equity += f(position.get("realized_net"))
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak if peak > 0 else 0.0)
            closed_at = int(position.get("closed_at") or 0)
            curve.append({"time": closed_at, "equity": equity})
            day = closed_at // 86400_000 if closed_at else 0
            daily_pnl[day] = daily_pnl.get(day, 0.0) + f(position.get("realized_net"))
        if unreal:
            marked_equity = equity + unreal
            peak = max(peak, marked_equity)
            max_drawdown = max(max_drawdown, (peak - marked_equity) / peak if peak > 0 else 0.0)
            curve.append({"time": max((int(p.get("closed_at") or 0) for p in ordered_closed), default=0),
                          "equity": marked_equity})
        daily_values = sorted(daily_pnl.values())
        tail_n = max(1, int(math.ceil(len(daily_values) * 0.05))) if daily_values else 0
        cvar95 = (sum(daily_values[:tail_n]) / tail_n) if tail_n else 0.0
        deploy_values = [value for _, value in self.deploy_samples]
        peak_deploy_pct = max(deploy_values, default=0.0)
        avg_deploy_pct = (sum(deploy_values) / len(deploy_values)) if deploy_values else 0.0

        def concentration(key):
            buckets = {}
            total_abs = 0.0
            for position in self.closed:
                value = f(position.get("realized_net"))
                bucket = key(position)
                buckets[bucket] = buckets.get(bucket, 0.0) + value
                total_abs += abs(value)
            return (max((abs(value) for value in buckets.values()), default=0.0) / total_abs) if total_abs else 0.0

        leverage_coverage = (
            self.master_leverage_known / (self.master_leverage_known + self.master_leverage_missing)
            if (self.master_leverage_known + self.master_leverage_missing) else 1.0
        )
        maintenance_coverage = (
            self.maintenance_margin_known / (self.maintenance_margin_known + self.maintenance_margin_missing)
            if (self.maintenance_margin_known + self.maintenance_margin_missing) else 1.0
        )
        price_path_coverage = float(self.price_path_meta.get(
            "coverage", 1.0 if self.price_path_points > 0 else 0.0,
        ))
        fallback_reasons = []
        if not self.price_path_points:
            fallback_reasons.append("missing_price_path")
        if leverage_coverage < 1.0:
            fallback_reasons.append("missing_master_leverage")
        return {
            "addr": self.addr,
            "closed_n": len(self.closed),
            "open_n": len(self.open),
            "wins": wins,
            "liquidations": liquidations,
            "tail_profit_closes": tail_profit_closes,
            "natural_closes": natural_closes,
            "path_completion_rate": path_completion_rate,
            "liquidation_rate": liquidations / len(self.closed) if self.closed else 0.0,
            "copy_win_rate": wins / len(self.closed) if self.closed else 0.0,
            "copy_net_pnl": equity_pnl,
            "margin_equity_pct": self.margin_equity_pct,
            "initial_margin_equity": self.initial_balance * self.margin_equity_pct,
            "closed_net_pnl": closed_net,
            "copy_gross_pnl": self.gross_pnl,
            "unrealized_pnl": unreal,
            "valuation_status": "complete" if not missing_mark_coins else "missing_marks",
            "valuation_coverage": valued_open / len(self.open) if self.open else 1.0,
            "valuation_missing_coins": sorted(set(missing_mark_coins)),
            "valuation_asof_ms": self.valuation_asof_ms,
            "fee_drag": self.fee_drag,
            "target_open_events": self.target_open_events,
            "opened_n": self.opened_n,
            "open_fill_rate": self.opened_n / self.target_open_events if self.target_open_events else 1.0,
            "target_adds": self.target_adds,
            "followed_adds": self.followed_adds,
            "missed_adds": self.missed_adds,
            "missed_add_rate": self.missed_adds / self.target_adds if self.target_adds else 0.0,
            "add_dependency": add_notl / initial_notl if initial_notl else 0.0,
            "target_peak_concurrent": self.target_peak_concurrent,
            "copy_peak_concurrent": self.copy_peak_concurrent,
            "max_concurrent_fit": self.copy_peak_concurrent / self.target_peak_concurrent if self.target_peak_concurrent else 1.0,
            "capacity_open_fit": self.opened_n / (self.opened_n + capacity_skips) if (self.opened_n + capacity_skips) else 1.0,
            "actionable_open_rate": self.opened_n / self.target_open_events if self.target_open_events else 1.0,
            "execution_fill_rate": self.opened_n / self.target_open_events if self.target_open_events else 1.0,
            "behavior_replication_rate": (
                (self.opened_n / self.target_open_events if self.target_open_events else 1.0)
                * (1.0 - (self.missed_adds / self.target_adds if self.target_adds else 0.0))
                * path_completion_rate
            ),
            "equity_curve": curve,
            "max_drawdown": max_drawdown,
            "worst_day": min(daily_values, default=0.0),
            "cvar95": cvar95,
            "peak_deploy_pct": peak_deploy_pct,
            "avg_deploy_pct": avg_deploy_pct,
            "fee_slippage_drag": self.fee_drag,
            "pnl_concentration": {
                "wallet": concentration(lambda p: p.get("addr")),
                "coin": concentration(lambda p: p.get("coin")),
                "side": concentration(lambda p: p.get("side")),
                "day": concentration(lambda p: int(p.get("closed_at") or 0) // 86400_000),
            },
            "price_path_points": self.price_path_points,
            "price_path_coverage": price_path_coverage,
            "price_path_boundary_skips": self.path_boundary_skips,
            "ambiguous_liquidations": len(self.ambiguous_path_events),
            "ambiguous_path_ranges": [
                {"coin": coin, "open_time": lo, "close_time": hi}
                for coin, lo, hi in sorted(self.ambiguous_path_events)
            ],
            "price_path_missing_coins": list(self.price_path_meta.get("missingCoins") or []),
            "master_leverage_known": self.master_leverage_known,
            "master_leverage_missing": self.master_leverage_missing,
            "master_leverage_coverage": leverage_coverage,
            "maintenance_margin_coverage": maintenance_coverage,
            "maintenance_margin_known": self.maintenance_margin_known,
            "maintenance_margin_missing": self.maintenance_margin_missing,
            "model_coverage": min(leverage_coverage, maintenance_coverage, price_path_coverage),
            "fallback_reasons": fallback_reasons,
            "skip_reasons": dict(self.skip_reasons),
            "positions": [summarize_position(p) for p in self.closed],
            "open_positions": open_positions,
        }


def summarize_position(p, *, mark_px=None, unrealized_pnl=None, valuation_complete=None):
    out = {
        "addr": p.get("addr"),
        "coin": p["coin"],
        "side": p["side"],
        "status": p.get("status", "open"),
        "opened_at": p.get("opened_at"),
        "closed_at": p.get("closed_at"),
        "net_pnl": p["realized_net"],
        "gross_pnl": p["gross_pnl"],
        "entry_fees": p["entry_fees"],
        "exit_fees": p["exit_fees"],
        "fee_drag": p["entry_fees"] + p["exit_fees"],
        "target_initial_notl": p["target_initial_notl"],
        "target_add_notl": p["target_add_notl"],
        "add_dependency": p["target_add_notl"] / p["target_initial_notl"] if p["target_initial_notl"] else 0.0,
        "target_adds": p["target_adds"],
        "followed_adds": p["followed_adds"],
        "missed_adds": p["missed_adds"],
        "entry_px": p["entry_px"],
        "master_avg_px": p["master_open_px"],
        "master_leverage": p.get("master_leverage"),
        "leverage": p["leverage"],
        "margin": p["margin"],
        "remaining_size": p.get("rem_size"),
    }
    if mark_px is not None:
        out["mark_px"] = mark_px
    if unrealized_pnl is not None:
        out["unrealized_pnl"] = unrealized_pnl
    if valuation_complete is not None:
        out["valuation_complete"] = bool(valuation_complete)
    return out


def run_backtest(addr, fills, sigmas=None, initial_balance=None, overrides=None, price_path=None,
                 market_ctx=None, price_path_meta=None, valuation_marks=None,
                 valuation_asof_ms=None):
    return Backtest(addr, sigmas=sigmas, initial_balance=initial_balance,
                    overrides=overrides, market_ctx=market_ctx,
                    price_path_meta=price_path_meta, valuation_marks=valuation_marks,
                    valuation_asof_ms=valuation_asof_ms).run(fills, price_path=price_path)


def slice_backtest_result(result: dict, start_ms: int, *, window_days=None) -> dict:
    """Slice a warm replay into a current economic evaluation window.

    The replay starts before ``start_ms`` so positions already open at the
    boundary are reconstructed. Closed samples remain window-local, while currently
    open canonical positions contribute their terminal mark-to-market overlay. This
    prevents an open loss from disappearing merely because it has not closed yet.
    """
    out = dict(result or {})
    positions = [
        dict(position)
        for position in (out.get("positions") or [])
        if int(position.get("closed_at") or 0) >= int(start_ms)
    ]
    positions.sort(key=lambda position: int(position.get("closed_at") or 0))
    closed_net = sum(f(position.get("net_pnl")) for position in positions)
    open_positions = [dict(position) for position in (out.get("open_positions") or [])]
    open_unrealized = sum(f(position.get("unrealized_pnl")) for position in open_positions)
    valuation_status = str(out.get("valuation_status") or (
        "complete" if not open_positions else "missing_marks"
    ))
    gross = sum(f(position.get("gross_pnl")) for position in positions)
    fees = sum(f(position.get("fee_drag")) for position in positions)
    wins = sum(1 for position in positions if f(position.get("net_pnl")) > 0)
    liquidations = sum(1 for position in positions if position.get("status") == "liquidated")
    tail_profit_closes = sum(1 for position in positions if position.get("status") == "tail_closed")
    natural_closes = max(0, len(positions) - liquidations)
    path_completion_rate = natural_closes / len(positions) if positions else 1.0
    open_rate = f(out.get("actionable_open_rate")) if out.get("actionable_open_rate") is not None else 1.0
    add_capture = 1.0 - (f(out.get("missed_add_rate")) if out.get("missed_add_rate") is not None else 0.0)

    equity = float(config.INITIAL_BALANCE)
    peak = equity
    max_drawdown = 0.0
    curve = []
    daily_pnl = {}
    for position in positions:
        pnl = f(position.get("net_pnl"))
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak if peak > 0 else 0.0)
        closed_at = int(position.get("closed_at") or 0)
        curve.append({"time": closed_at, "equity": equity})
        day = closed_at // 86_400_000 if closed_at else 0
        daily_pnl[day] = daily_pnl.get(day, 0.0) + pnl
    daily_values = sorted(daily_pnl.values())
    tail_n = max(1, int(math.ceil(len(daily_values) * 0.05))) if daily_values else 0

    def concentration(key):
        buckets = {}
        total_abs = 0.0
        for position in positions:
            pnl = f(position.get("net_pnl"))
            bucket = key(position)
            buckets[bucket] = buckets.get(bucket, 0.0) + pnl
            total_abs += abs(pnl)
        return max((abs(value) for value in buckets.values()), default=0.0) / total_abs if total_abs else 0.0

    ambiguous_ranges = [
        row for row in (out.get("ambiguous_path_ranges") or [])
        if int(row.get("close_time") or 0) >= int(start_ms)
    ]
    out.update({
        "closed_n": len(positions),
        "wins": wins,
        "liquidations": liquidations,
        "tail_profit_closes": tail_profit_closes,
        "natural_closes": natural_closes,
        "path_completion_rate": path_completion_rate,
        "liquidation_rate": liquidations / len(positions) if positions else 0.0,
        "behavior_replication_rate": max(0.0, min(1.0, open_rate * add_capture * path_completion_rate)),
        "ambiguous_liquidations": len(ambiguous_ranges),
        "ambiguous_path_ranges": ambiguous_ranges,
        "copy_win_rate": wins / len(positions) if positions else 0.0,
        "copy_net_pnl": closed_net + open_unrealized,
        "closed_net_pnl": closed_net,
        "copy_gross_pnl": gross,
        "unrealized_pnl": open_unrealized,
        "valuation_status": valuation_status,
        "fee_drag": fees,
        "fee_slippage_drag": fees,
        "equity_curve": curve,
        "max_drawdown": max_drawdown,
        "worst_day": min(daily_values, default=0.0),
        "cvar95": sum(daily_values[:tail_n]) / tail_n if tail_n else 0.0,
        "positions": positions,
        "open_positions": open_positions,
        "pnl_concentration": {
            "wallet": concentration(lambda position: position.get("addr")),
            "coin": concentration(lambda position: position.get("coin")),
            "side": concentration(lambda position: position.get("side")),
            "day": concentration(lambda position: int(position.get("closed_at") or 0) // 86_400_000),
        },
        "_window_start_ms": int(start_ms),
        "_window_days": int(window_days) if window_days is not None else None,
        "_warmup_applied": True,
    })
    return out
