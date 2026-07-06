"""Offline replay of target fills through the current copy rules.

This is intentionally pure-Python and DB-free so scans and ad-hoc audits can answer
"would our engine have copied this wallet well?" without starting the live observer.
Historical fills do not include the live BBO seen by the observer, so execution uses
the target fill price plus our fee model; the rule decisions mirror the taker book.
"""
from __future__ import annotations

from collections import Counter

from . import config
from .sizing import margin_pct_for_deploy
from .util import f


def _stop_px(entry_px: float, is_buy: bool, lev: float, copy_stop_enable: bool, stop_margin_pct: float) -> float:
    if not copy_stop_enable or not entry_px or not lev or not stop_margin_pct:
        return 0.0
    d = stop_margin_pct / lev
    return entry_px * (1 - d) if is_buy else entry_px * (1 + d)


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
            "coin": coin,
            "low": lo,
            "high": hi,
            "close": _row_price(row, "close", "c", "px", "price") or (lo + hi) / 2,
        })
    out.sort(key=lambda x: x["time"])
    return out


class Backtest:
    def __init__(self, addr, sigmas=None, initial_balance=None, overrides=None):
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
        self.min_open_margin_pct = overrides.get("MIN_OPEN_MARGIN_PCT", config.MIN_OPEN_MARGIN_PCT)
        self.copy_stop_enable = bool(overrides.get("COPY_STOP_ENABLE", config.COPY_STOP_ENABLE))
        self.stop_margin_pct = overrides.get("STOP_MARGIN_PCT", config.STOP_MARGIN_PCT)
        self.price_path_points = 0

    def sigma(self, coin):
        return self.sigmas.get(coin) or config.VOL_FALLBACK_SIGMA

    def tier(self, sigma: float, coin: str | None = None) -> str:
        if sigma <= self.stable_sigma_max:
            return "stable"
        return "high" if sigma >= self.high_sigma_min else "mid"

    def sizing_for(self, sigma: float, coin: str | None = None) -> tuple[float, float]:
        tier = self.tier(sigma, coin)
        lev = max(self.min_lev, float(int(self.tier_lev_cap[tier])))
        return self.tier_margin[tier], lev

    def available(self):
        locked = sum(p["margin"] * (p["rem_size"] / p["size"] if p["size"] else 1.0) for p in self.open.values())
        return self.balance - locked

    def coin_cap_pct(self, tier):
        return self.tier_coin_cap[tier]

    def min_notional(self, tier):
        return self.tier_min_notional[tier]

    def run(self, fills, price_path=None):
        path_events = _price_events(price_path)
        self.price_path_points = len(path_events)
        if not path_events:
            for x in sorted((fills or []), key=lambda r: r.get("time", 0)):
                self.process_fill(x)
            return self.result()

        events = []
        for row in path_events:
            events.append((row["time"], 0, row))
        for row in fills or []:
            events.append((int(row.get("time", 0) or 0), 1, row))
        for _, kind, row in sorted(events, key=lambda x: (x[0], x[1])):
            if kind == 0:
                self.process_price(row)
            else:
                self.process_fill(row)
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
        self._mark_stops(coin, px, x.get("time"))

        sz = f(x.get("sz"))
        signed = sz if x.get("side") == "B" else -sz
        pos0 = f(x.get("startPosition"))
        pos1 = pos0 + signed
        key = (addr, coin)
        oid = x.get("oid")

        was_flat = abs(pos0) < config.FLAT
        if was_flat and abs(pos1) >= config.FLAT:
            self.target_open_events += 1
        if abs(pos1) < config.FLAT:
            self.target_pos.pop(key, None)
        else:
            self.target_pos[key] = pos1
        self.target_peak_concurrent = max(self.target_peak_concurrent, len(self.target_pos))

        ep = self.open.get(key)
        if ep is None:
            if was_flat and abs(pos1) >= config.FLAT:
                self._open_position(addr, coin, x.get("time"), px, pos1, oid)
            elif abs(pos1) >= config.FLAT:
                self.skip_reasons["skip_midway"] += 1
            return

        ep["master_peak"] = max(ep["master_peak"], abs(pos1))
        growing = abs(pos1) >= abs(pos0) - config.FLAT and abs(pos1) >= config.FLAT
        if growing:
            if oid is not None and oid in ep["seen_oids"]:
                return
            ep["seen_oids"].add(oid)
            self._apply_add(addr, coin, px, signed, pos1, oid)
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
        self._mark_stops_range(coin, lo, hi, x.get("time"))

    def _open_position(self, addr, coin, t, px, pos1, oid):
        sigma = self.sigma(coin)
        tier = self.tier(sigma, coin)
        margin_pct, lev = self.sizing_for(sigma, coin)
        if coin.startswith("xyz:"):
            lev = max(self.min_lev, min(lev, self.stock_max_lev))
        side = "long" if pos1 > 0 else "short"
        sign = 1 if side == "long" else -1
        target_notl = abs(pos1) * px
        avail = self.available()
        locked = max(0.0, self.balance - avail)
        margin_pct = margin_pct_for_deploy(
            self.tier_margin[tier],
            self.tier_margin_min[tier],
            self.deploy_full_pct,
            self.max_deploy_pct,
            locked,
            self.balance,
        )
        margin = max(0.0, self.balance * margin_pct)
        existing_coin = sum(
            p["margin"] * (p["rem_size"] / p["size"] if p["size"] else 1.0)
            for (addr, c), p in self.open.items()
            if c == coin and p["side"] == side
        )
        room = max(0.0, self.coin_cap_pct(tier) * self.balance - existing_coin)
        deploy_room = max(0.0, avail - (1.0 - self.max_deploy_pct) * self.balance)
        capped = min(margin, room, deploy_room)
        if capped < self.min_open_margin_pct * self.balance:
            why = "coin_full" if room < margin else "no_cash" if avail < margin else "deploy_cap" if deploy_room < margin else "margin_too_small"
            self.skip_reasons[f"skip_{why}"] += 1
            return
        margin = capped
        notional = margin * lev
        if target_notl > 0 and notional > target_notl:
            notional = target_notl
            margin = notional / lev if lev else margin
        if notional < self.min_notional(tier):
            self.skip_reasons["skip_small_notl"] += 1
            return
        size = notional / px
        fee = abs(size * px) * config.TAKER_FEE
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
            "margin": margin,
            "first_margin": margin,
            "notional": notional,
            "leverage": lev,
            "liq_px": px * (1 - 1.0 / lev) if is_buy else px * (1 + 1.0 / lev),
            "stop_px": _stop_px(px, is_buy, lev, self.copy_stop_enable, self.stop_margin_pct),
            "last_add_px": px,
            "add_count": 0,
            "followed_adds": 0,
            "missed_adds": 0,
            "entry_fees": fee,
            "exit_fees": 0.0,
            "gross_pnl": 0.0,
            "realized_net": -fee,
            "seen_oids": {oid},
            "reduce_anchor": None,
        }
        self.opened_n += 1
        self.copy_peak_concurrent = max(self.copy_peak_concurrent, len(self.open))

    def _observe_add(self, ep):
        ep["missed_adds"] += 1
        self.missed_adds += 1

    def _apply_add(self, addr, coin, px, signed, pos1, oid):
        ep = self.open[(addr, coin)]
        m_now = abs(pos1)
        if m_now > 0 and ep["master_open_px"]:
            m_prev = abs(pos1 - signed)
            ep["master_open_px"] = (m_prev * ep["master_open_px"] + abs(signed) * px) / m_now
        add_notl = abs(signed) * px
        ep["target_add_notl"] += add_notl
        ep["target_adds"] += 1
        self.target_adds += 1

        sigma = self.sigma(coin)
        tier = self.tier(sigma, coin)
        is_buy = ep["side"] == "long"
        if self.add_strategy == "smart":
            last = ep.get("last_add_px") or ep["entry_px"]
            adv = (((last - px) if is_buy else (px - last)) / last) if last else 0.0
            gap_mult = self.add_shrink_g ** ep["add_count"]
            threshold = self.add_gap_k * sigma * gap_mult
            pos_threshold = self.pos_add_gap_k * sigma * gap_mult
            if adv >= threshold:
                pass
            elif adv < 0 and self.follow_pos_add and abs(adv) >= pos_threshold:
                pass
            else:
                return self._observe_add(ep)
            if ep["add_count"] >= self.add_max_hard:
                return self._observe_add(ep)
            ratio = add_notl / ep["master_first_notl"] if ep["master_first_notl"] else self.add_frac
            existing = sum(
                p["margin"] * (p["rem_size"] / p["size"] if p["size"] else 1.0)
                for (addr, c), p in self.open.items()
                if c == coin and p["side"] == ep["side"]
            )
            add_margin = max(0.0, min(ratio * ep["first_margin"],
                                      self.coin_cap_pct(tier) * self.balance - existing,
                                      self.available()))
            if add_margin < self.min_open_margin_pct * self.balance:
                return self._observe_add(ep)
        else:
            max_adds = self.tier_max_adds[tier]
            if ep["add_count"] >= max_adds:
                return self._observe_add(ep)
            add_margin = max(0.0, min(ep["first_margin"] * self.add_frac, self.available()))
            if add_margin <= 0:
                return self._observe_add(ep)

        add_size = (add_margin * ep["leverage"] / px) if px else 0.0
        new_size = ep["rem_size"] + add_size
        ep["entry_px"] = ((ep["rem_size"] * ep["entry_px"] + add_size * px) / new_size if new_size else px)
        ep["rem_size"] = new_size
        ep["size"] += add_size
        ep["margin"] += add_margin
        ep["notional"] += add_margin * ep["leverage"]
        ep["liq_px"] = ep["entry_px"] * (1 - 1.0 / ep["leverage"]) if is_buy else ep["entry_px"] * (1 + 1.0 / ep["leverage"])
        ep["stop_px"] = _stop_px(ep["entry_px"], is_buy, ep["leverage"], self.copy_stop_enable, self.stop_margin_pct)
        ep["add_count"] += 1
        ep["followed_adds"] += 1
        ep["last_add_px"] = px
        ep["reduce_anchor"] = None
        fee = abs(add_size * px) * config.TAKER_FEE
        ep["entry_fees"] += fee
        ep["realized_net"] -= fee
        self.balance -= fee
        self.fee_drag += fee
        self.followed_adds += 1

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
        close_size = ep["rem_size"] * reduce_frac
        gross = close_size * (px - ep["entry_px"]) * ep["sign"]
        fee = abs(close_size * px) * config.TAKER_FEE
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

    def _mark_stops(self, coin, px, t):
        for (addr, c), ep in list(self.open.items()):
            if c != coin:
                continue
            liq_hit = px <= ep["liq_px"] if ep["side"] == "long" else px >= ep["liq_px"]
            stop_hit = self.copy_stop_enable and ep["stop_px"] and (px <= ep["stop_px"] if ep["side"] == "long" else px >= ep["stop_px"])
            if liq_hit:
                self._apply_reduce(addr, coin, ep["liq_px"], 0.0, 0.0, closing=True, status="liquidated", t=t)
            elif stop_hit:
                self._apply_reduce(addr, coin, px, 0.0, 0.0, closing=True, status="stopped", t=t)

    def _mark_stops_range(self, coin, low, high, t):
        for (addr, c), ep in list(self.open.items()):
            if c != coin:
                continue
            if ep["side"] == "long":
                liq_hit = low <= ep["liq_px"]
                stop_hit = self.copy_stop_enable and ep["stop_px"] and low <= ep["stop_px"]
            else:
                liq_hit = high >= ep["liq_px"]
                stop_hit = self.copy_stop_enable and ep["stop_px"] and high >= ep["stop_px"]
            if liq_hit:
                self._apply_reduce(addr, coin, ep["liq_px"], 0.0, 0.0, closing=True, status="liquidated", t=t)
            elif stop_hit:
                self._apply_reduce(addr, coin, ep["stop_px"], 0.0, 0.0, closing=True, status="stopped", t=t)

    def result(self):
        unreal = 0.0
        for (_, coin), ep in self.open.items():
            px = self.last_px.get(coin) or ep["entry_px"]
            unreal += ep["rem_size"] * (px - ep["entry_px"]) * ep["sign"]
        closed_net = sum(p["realized_net"] for p in self.closed)
        wins = sum(1 for p in self.closed if p["realized_net"] > 0)
        stops = sum(1 for p in self.closed if p.get("status") == "stopped")
        liquidations = sum(1 for p in self.closed if p.get("status") == "liquidated")
        initial_notl = sum(p["target_initial_notl"] for p in self.closed) + sum(p["target_initial_notl"] for p in self.open.values())
        add_notl = sum(p["target_add_notl"] for p in self.closed) + sum(p["target_add_notl"] for p in self.open.values())
        capacity_skips = sum(self.skip_reasons[k] for k in ("skip_coin_full", "skip_no_cash", "skip_deploy_cap", "skip_margin_too_small"))
        equity_pnl = self.balance - self.initial_balance + unreal
        return {
            "addr": self.addr,
            "closed_n": len(self.closed),
            "open_n": len(self.open),
            "wins": wins,
            "stops": stops,
            "liquidations": liquidations,
            "copy_win_rate": wins / len(self.closed) if self.closed else 0.0,
            "copy_net_pnl": equity_pnl,
            "closed_net_pnl": closed_net,
            "copy_gross_pnl": self.gross_pnl,
            "unrealized_pnl": unreal,
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
            "price_path_points": self.price_path_points,
            "skip_reasons": dict(self.skip_reasons),
            "positions": [summarize_position(p) for p in self.closed],
            "open_positions": [summarize_position(p) for p in self.open.values()],
        }


def summarize_position(p):
    return {
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
        "leverage": p["leverage"],
        "margin": p["margin"],
    }


def run_backtest(addr, fills, sigmas=None, initial_balance=None, overrides=None, price_path=None):
    return Backtest(addr, sigmas=sigmas, initial_balance=initial_balance, overrides=overrides).run(fills, price_path=price_path)
