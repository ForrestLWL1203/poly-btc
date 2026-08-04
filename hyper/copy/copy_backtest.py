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

from hyper import config
from hyper.market.coin_filter import coin_is_blocked, parse_coin_blacklist
from .copy_data import normalize_copyable_fills
from .copy_engine import (OpenSizingParams, isolated_liq_px,
                          plan_open_sizing, profit_tail_close_decision, reduce_leaves_dust,
                          rebase_isolated_position,
                          smart_add_order_margin, smart_take_profit_decision, tier_for_sigma,
                          margin_cap_room, wallet_margin,
                          wallet_sector_side_effective_cap_pct, wallet_sector_side_margin,
                          wallet_sector_side_margin_room, wallet_sector_side_position_count)
from .fill_transition import classify_fill_transition
from hyper.util import f


ADD_OUTCOMES = (
    "followed",
    "noise_merged",
    "hard_cap_blocked",
    "coin_cap_blocked",
    "cash_blocked",
    "min_margin_blocked",
    "wallet_sector_side_cap_blocked",
    "wallet_cap_blocked",
    "total_margin_cap_blocked",
    "forward_loss_blocked",
)
ADD_BLOCKED_OUTCOMES = tuple(
    outcome for outcome in ADD_OUTCOMES
    if outcome not in {"followed", "noise_merged"}
)
ADD_METRICS_VERSION = "add_metrics_v2"
CAPACITY_OPEN_OUTCOMES = frozenset({
    "skip_coin_full", "skip_no_cash", "skip_deploy_cap", "skip_margin_too_small",
    "skip_wallet_sector_side_full", "skip_wallet_full", "skip_wallet_position_cap",
    "skip_wallet_stock_side_position_cap",
})
OPEN_CONSTRAINT_GROUPS = {
    # Actual free-cash exhaustion is the only literal funding congestion.
    "cash": frozenset({"skip_no_cash"}),
    # The remaining groups are independent strategy/risk limits. They still
    # reduce execution capacity but must not be described as cash congestion.
    "aggregateDeploy": frozenset({"skip_deploy_cap"}),
    "coinCap": frozenset({"skip_coin_full"}),
    "concentration": frozenset({
        "skip_wallet_sector_side_full", "skip_wallet_full",
        "skip_wallet_position_cap", "skip_wallet_stock_side_position_cap",
    }),
    "minimumSizing": frozenset({"skip_margin_too_small"}),
}


def open_execution_metrics(open_events) -> dict:
    """Return one auditable historical-open denominator.

    Historical replay assumes the market was liquid enough to execute. Every source flat-to-open/flip signal
    is an opportunity regardless of source notional; every policy/capacity rejection is therefore a real miss.
    """
    events = [dict(event) for event in (open_events or ())]
    raw_n = len(events)
    opened_n = sum(event.get("outcome") == "opened" for event in events)
    effective_n = raw_n
    capacity_skips = sum(event.get("outcome") in CAPACITY_OPEN_OUTCOMES for event in events)
    details = {}
    for event in events:
        outcome = str(event.get("outcome") or "skip_unknown_open")
        if outcome == "opened":
            continue
        key = (str(event.get("coin") or ""), str(event.get("tier") or ""), outcome)
        item = details.setdefault(key, {
            "coin": key[0],
            "tier": key[1],
            "reason": outcome,
            "count": 0,
            "copyNotionalMin": None,
            "copyNotionalMax": None,
        })
        item["count"] += 1
        copy_notional = event.get("copy_notional")
        if copy_notional is not None:
            value = f(copy_notional)
            item["copyNotionalMin"] = (
                value if item["copyNotionalMin"] is None else min(item["copyNotionalMin"], value)
            )
            item["copyNotionalMax"] = (
                value if item["copyNotionalMax"] is None else max(item["copyNotionalMax"], value)
            )
    raw_rate = opened_n / raw_n if raw_n else 1.0
    effective_rate = opened_n / effective_n if effective_n else 1.0
    capacity_fit = (
        opened_n / (opened_n + capacity_skips)
        if (opened_n + capacity_skips) else 1.0
    )
    constraint_counts = {
        group: sum(event.get("outcome") in outcomes for event in events)
        for group, outcomes in OPEN_CONSTRAINT_GROUPS.items()
    }
    constraint_fit = {
        group: (
            opened_n / (opened_n + count)
            if (opened_n + count) else 1.0
        )
        for group, count in constraint_counts.items()
    }
    return {
        "raw_target_open_events": raw_n,
        "small_open_excluded_n": 0,
        "effective_target_open_events": effective_n,
        "opened_n": opened_n,
        "raw_open_capture_rate": raw_rate,
        "effective_open_follow_rate": effective_rate,
        "capacity_open_fit": capacity_fit,
        "execution_capacity_fit": capacity_fit,
        "cash_congestion_fit": constraint_fit["cash"],
        "open_constraint_counts": constraint_counts,
        "open_constraint_fit": constraint_fit,
        "open_execution_audit": {
            "rawTargetOpenN": raw_n,
            "smallOpenExcludedN": 0,
            "effectiveTargetOpenN": effective_n,
            "openedN": opened_n,
            "rawOpenCaptureRate": raw_rate,
            "effectiveOpenFollowRate": effective_rate,
            "capacityFit": capacity_fit,
            "capacityLabel": "execution_capacity",
            "constraintCounts": constraint_counts,
            "constraintFit": constraint_fit,
            "skipDetails": sorted(
                details.values(),
                key=lambda item: (-int(item["count"]), item["reason"], item["coin"]),
            ),
        },
    }


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _percentile(values: list[float], quantile: float) -> float:
    rows = sorted(float(value) for value in values)
    if not rows:
        return 0.0
    index = max(0, min(len(rows) - 1, int(math.ceil(len(rows) * quantile)) - 1))
    return rows[index]


def _endpoint_pnl(position: dict) -> float:
    return f(position.get("net_pnl")) + (
        f(position.get("unrealized_pnl")) if position.get("status") == "open" else 0.0
    )


def profit_structure_metrics(positions: list[dict], *, total_net: float) -> dict:
    """Return fee-paid distribution diagnostics for complete closed Episodes only."""
    pnls = [_endpoint_pnl(position) for position in positions]
    wins = sorted((value for value in pnls if value > 0.0), reverse=True)
    losses = [-value for value in pnls if value < 0.0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    win_n = len(wins)
    loss_n = len(losses)
    avg_win = gross_profit / win_n if win_n else 0.0
    avg_loss = gross_loss / loss_n if loss_n else 0.0
    top3 = wins[:3]
    # Do not confuse concentration with gambling.  Remove the largest three positive endpoints and inspect
    # the remaining body directly: Wallet A stays profitable/win-heavy; Wallet B exposes its many ordinary
    # losing bets.  ``total_net`` remains authoritative for dollars so fees and marked open PnL stay aligned.
    body = list(pnls)
    for winner in top3:
        body.remove(winner)
    body_wins = [value for value in body if value > 0.0]
    body_losses = [-value for value in body if value < 0.0]
    body_gross_profit = sum(body_wins)
    body_gross_loss = sum(body_losses)
    body_avg_win = body_gross_profit / len(body_wins) if body_wins else 0.0
    body_avg_loss = body_gross_loss / len(body_losses) if body_losses else 0.0
    body_sorted = sorted(body)
    body_mid = len(body_sorted) // 2
    body_median = (
        0.0 if not body_sorted else
        body_sorted[body_mid] if len(body_sorted) % 2 else
        (body_sorted[body_mid - 1] + body_sorted[body_mid]) / 2.0
    )
    return {
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": (
            gross_profit / gross_loss if gross_loss > 0.0 else (999.0 if gross_profit > 0.0 else 0.0)
        ),
        "payoff_ratio": (
            avg_win / avg_loss if avg_loss > 0.0 else (999.0 if avg_win > 0.0 else 0.0)
        ),
        "positive_episode_n": win_n,
        "negative_episode_n": loss_n,
        "top_positive_pnls": top3,
        "top1_profit_share": wins[0] / gross_profit if gross_profit > 0.0 else 0.0,
        "top3_profit_share": sum(wins[:3]) / gross_profit if gross_profit > 0.0 else 0.0,
        "net_after_top1": float(total_net) - sum(wins[:1]),
        "net_after_top2": float(total_net) - sum(wins[:2]),
        "body_after_top3_n": len(body),
        "body_after_top3_wins": len(body_wins),
        "body_after_top3_losses": len(body_losses),
        "body_after_top3_win_rate": len(body_wins) / len(body) if body else 0.0,
        "body_after_top3_net_pnl": float(total_net) - sum(top3),
        "body_after_top3_gross_profit": body_gross_profit,
        "body_after_top3_gross_loss": body_gross_loss,
        "body_after_top3_profit_factor": (
            body_gross_profit / body_gross_loss
            if body_gross_loss > 0.0 else (999.0 if body_gross_profit > 0.0 else 0.0)
        ),
        "body_after_top3_payoff_ratio": (
            body_avg_win / body_avg_loss
            if body_avg_loss > 0.0 else (999.0 if body_avg_win > 0.0 else 0.0)
        ),
        "body_after_top3_median_pnl": body_median,
    }


def path_risk_metrics(
    samples: list[dict],
    *,
    initial_equity: float,
    liquidation_times=(),
    event_pct: float = config.COPY_DEEP_BAG_EVENT_PCT,
    event_min_hours: float = config.COPY_DEEP_BAG_EVENT_MIN_HOURS,
) -> dict:
    """Summarize time-weighted marked-equity risk from the canonical replay path."""
    initial = max(1.0, f(initial_equity))
    rows = sorted(
        (
            {"time": int(f(row.get("time"))), "equity": f(row.get("equity"))}
            for row in samples or () if int(f(row.get("time"))) > 0
        ),
        key=lambda row: row["time"],
    )
    deduped = []
    for row in rows:
        if deduped and row["time"] == deduped[-1]["time"]:
            # Preserve both an intra-candle adverse extreme and its close for max-DD while letting the
            # close become the state carried through the following time interval.
            deduped.append(row)
        else:
            deduped.append(row)
    rows = deduped
    if not rows:
        return {
            "path_risk_status": "missing", "intratrade_max_drawdown": None,
            "max_underwater_hours": None, "loss_over_5_time_ratio": None,
            "deep_bag_event_n": 0, "failed_deep_bag_n": 0,
            "deep_bag_recovery_rate": None, "max_deep_bag_hours": None,
        }

    peak = initial
    max_dd = 0.0
    below5_ms = 0
    total_ms = max(0, rows[-1]["time"] - rows[0]["time"])
    underwater_start = None
    max_underwater_ms = 0
    deep = None
    events = []
    liq_times = sorted(int(f(value)) for value in liquidation_times or () if int(f(value)) > 0)
    for index, row in enumerate(rows):
        stamp = row["time"]
        equity = row["equity"]
        peak = max(peak, equity)
        dd = max(0.0, (peak - equity) / initial)
        max_dd = max(max_dd, dd)
        next_stamp = rows[index + 1]["time"] if index + 1 < len(rows) else stamp
        interval = max(0, next_stamp - stamp)
        if dd >= 0.05:
            below5_ms += interval
        if dd > 1e-12:
            underwater_start = stamp if underwater_start is None else underwater_start
            max_underwater_ms = max(max_underwater_ms, next_stamp - underwater_start)
        else:
            if underwater_start is not None:
                max_underwater_ms = max(max_underwater_ms, stamp - underwater_start)
            underwater_start = None
        if deep is None and dd >= event_pct:
            deep = {"start": stamp, "recovery_equity": peak, "max_dd": dd,
                    "max_loss_frac": dd}
        elif deep is not None:
            deep["max_dd"] = max(deep["max_dd"], dd)
            deep["max_loss_frac"] = max(deep["max_loss_frac"], dd)
            # A recovered historical deep drawdown must regain the equity high that preceded the event;
            # merely remaining above the original $10k base after first making money is not a recovery.
            if equity >= deep["recovery_equity"] - 1e-9:
                duration = max(0, stamp - deep["start"])
                if duration >= event_min_hours * 3_600_000:
                    liquidated = any(deep["start"] <= value <= stamp for value in liq_times)
                    events.append({**deep, "end": stamp, "duration_ms": duration,
                                   "recovered": not liquidated, "liquidated": liquidated})
                deep = None
    if underwater_start is not None:
        max_underwater_ms = max(max_underwater_ms, rows[-1]["time"] - underwater_start)
    if deep is not None:
        duration = max(0, rows[-1]["time"] - deep["start"])
        if duration >= event_min_hours * 3_600_000:
            events.append({**deep, "end": None, "duration_ms": duration,
                           "recovered": False, "liquidated": any(value >= deep["start"] for value in liq_times)})
    recovered_n = sum(1 for event in events if event["recovered"])
    failed_n = len(events) - recovered_n
    return {
        "path_risk_status": "complete",
        "intratrade_max_drawdown": max_dd,
        "max_underwater_hours": max_underwater_ms / 3_600_000,
        "loss_over_5_time_ratio": below5_ms / total_ms if total_ms > 0 else 0.0,
        "deep_bag_event_n": len(events),
        "failed_deep_bag_n": failed_n,
        "deep_bag_recovery_rate": recovered_n / len(events) if events else 1.0,
        "max_deep_bag_hours": max((event["duration_ms"] for event in events), default=0) / 3_600_000,
        "deep_bag_events": events,
    }


def add_fidelity_metrics(positions: list[dict], outcome_counts: dict | None = None) -> dict:
    counts = Counter({key: int((outcome_counts or {}).get(key) or 0) for key in ADD_OUTCOMES})
    target_orders = sum(counts.values())
    followed = counts["followed"]
    noise = counts["noise_merged"]
    blocked = sum(counts[key] for key in ADD_BLOCKED_OUTCOMES)
    actionable = followed + blocked
    add_positions = [
        position for position in positions
        if int(position.get("target_adds") or 0) > 0
        and position.get("entry_gap_sigma") is not None
    ]
    gaps = [max(0.0, f(position.get("entry_gap_sigma"))) for position in add_positions]
    pct_gaps = [max(0.0, f(position.get("entry_gap_pct"))) for position in add_positions]
    weights = [max(0.0, f(position.get("margin"))) for position in add_positions]
    total_weight = sum(weights)
    weighted_gap = (
        sum(gap * weight for gap, weight in zip(gaps, weights)) / total_weight
        if total_weight > 0.0 else (sum(gaps) / len(gaps) if gaps else 0.0)
    )
    p90_gap = _percentile(gaps, 0.90)
    weighted_pct_gap = (
        sum(gap * weight for gap, weight in zip(pct_gaps, weights)) / total_weight
        if total_weight > 0.0 else (sum(pct_gaps) / len(pct_gaps) if pct_gaps else 0.0)
    )
    p90_pct_gap = _percentile(pct_gaps, 0.90)
    entry_alignment = _clamp01(1.0 - 0.5 * weighted_gap - 0.5 * p90_gap)
    add_execution = 1.0 - (blocked / actionable if actionable else 0.0)
    add_fidelity = 0.80 * entry_alignment + 0.20 * add_execution
    applied = len(add_positions) >= 5
    return {
        "add_metrics_version": ADD_METRICS_VERSION,
        "add_outcome_counts": {key: counts[key] for key in ADD_OUTCOMES},
        "target_adds": target_orders,
        "followed_adds": followed,
        "missed_adds": max(0, target_orders - followed),
        "missed_add_rate": (target_orders - followed) / target_orders if target_orders else 0.0,
        "raw_add_order_follow_rate": followed / target_orders if target_orders else 1.0,
        "noise_merged_adds": noise,
        "blocked_adds": blocked,
        "actionable_add_orders": actionable,
        "actionable_add_capture_rate": followed / actionable if actionable else 1.0,
        "true_blocked_add_rate": blocked / actionable if actionable else 0.0,
        "add_episode_count": len(add_positions),
        "entry_gap_sigma_weighted": weighted_gap,
        "entry_gap_sigma_p90": p90_gap,
        "entry_gap_pct_weighted": weighted_pct_gap,
        "entry_gap_pct_p90": p90_pct_gap,
        "entry_gap_sigma_samples": gaps,
        "entry_gap_pct_samples": pct_gaps,
        "entry_gap_weight": total_weight,
        "entry_gap_sigma_weighted_sum": sum(
            gap * weight for gap, weight in zip(gaps, weights)
        ),
        "entry_gap_pct_weighted_sum": sum(
            gap * weight for gap, weight in zip(pct_gaps, weights)
        ),
        "entry_alignment": entry_alignment,
        "add_execution": add_execution,
        "add_fidelity": add_fidelity,
        "add_fidelity_applied": applied,
        "effective_add_fidelity": add_fidelity if applied else 1.0,
    }


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


class PreparedReplayFills(list):
    """Canonical, sorted fill rows carrying the owner normalization they were prepared for."""

    def __init__(self, rows=(), *, owner=None):
        super().__init__(rows)
        self.owner = owner


def prepare_replay_fills(fills, *, addr=None) -> PreparedReplayFills:
    """Normalize/sort a fill surface once so repeated parameter candidates can reuse it."""
    owner = None if not addr or str(addr).lower() == "portfolio" else str(addr).lower()
    if isinstance(fills, PreparedReplayFills) and fills.owner == owner:
        return fills
    return PreparedReplayFills(
        normalize_copyable_fills(fills, addr=owner),
        owner=owner,
    )


def slice_prepared_replay_fills(
    fills: PreparedReplayFills,
    *,
    start_ms: int | None = None,
    allowed_addrs=None,
) -> PreparedReplayFills:
    """Create a lightweight view list reusing the canonical row dictionaries.

    The old window/subset paths normalized every row again, multiplying millions of Python dictionaries
    across 30/14/7 windows and membership probes. A slice owns only references to the one longest surface.
    """
    prepared = prepare_replay_fills(fills)
    allowed = (
        {str(addr or "").lower() for addr in allowed_addrs if addr}
        if allowed_addrs is not None else None
    )
    return PreparedReplayFills(
        (
            row for row in prepared
            if (start_ms is None or int(row.get("time") or 0) >= int(start_ms))
            and (
                allowed is None
                or str(row.get("user") or "").lower() in allowed
            )
        ),
        owner=prepared.owner,
    )


class PreparedPricePath(list):
    """Normalized, sorted candle events that are safe to reuse across replay candidates.

    ``run_backtest`` used to rebuild and sort a fresh dictionary for every candle on every parameter or
    membership candidate.  A 37-day, hundred-market surface contains roughly 400k candles, so the conversion
    itself dominated the optimizer and produced hundreds of megabytes of short-lived objects.
    """

    def __init__(self, rows=()):
        super().__init__(rows)
        self._subset_cache = {}


def _price_events(price_path) -> list[dict]:
    """Normalize optional tick/candle path data into per-coin high/low events."""
    if not price_path:
        return []
    if isinstance(price_path, PreparedPricePath):
        return price_path
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


def prepare_price_path(price_path) -> PreparedPricePath:
    """Normalize a candle surface once for reuse by many strict replays."""
    if isinstance(price_path, PreparedPricePath):
        return price_path
    return PreparedPricePath(_price_events(price_path))


def subset_price_path(price_path, fills, *, start_ms=None, end_ms=None) -> PreparedPricePath:
    """Return only the reusable path events relevant to one fill set and optional time range."""
    prepared = prepare_price_path(price_path)
    if not prepared or not fills:
        return PreparedPricePath()
    coins = {row.get("coin") for row in fills if row.get("coin")}
    if not coins:
        return PreparedPricePath()
    lower = None if start_ms is None else int(start_ms)
    upper = None if end_ms is None else int(end_ms)
    cache_key = (tuple(sorted(str(coin) for coin in coins)), lower, upper)
    cached = prepared._subset_cache.get(cache_key)
    if cached is not None:
        return cached
    subset = PreparedPricePath(
        row for row in prepared
        if row.get("coin") in coins
        and (lower is None or int(row.get("close_time") or row.get("time") or 0) >= lower)
        and (upper is None or int(row.get("open_time") or row.get("time") or 0) <= upper)
    )
    if len(prepared._subset_cache) >= 16:
        prepared._subset_cache.clear()
    prepared._subset_cache[cache_key] = subset
    return subset


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
        # Keep the timestamped outcome of every target flat->open/flip.  Aggregate counters alone cannot be
        # sliced into recent windows: a 30-day compounding replay used to leak its whole-period open/capacity
        # rates into 14/7-day views, while replaying those windows independently incorrectly reset capital to
        # $10k.  The event stream lets recent diagnostics preserve the one real capital path.
        self.open_events = []
        self.followed_adds = 0
        self.missed_adds = 0
        self.target_adds = 0
        self.add_outcome_counts = Counter()
        self.add_events = []
        self.add_event_by_order = {}
        self.fee_drag = 0.0
        self.gross_pnl = 0.0
        self.high_sigma_min = overrides.get("HIGH_SIGMA_MIN", config.HIGH_SIGMA_MIN)
        self.tier_margin = {
            "stable": overrides.get("STABLE_MARGIN_PCT", config.STABLE_MARGIN_PCT),
            "mid": overrides.get("MID_MARGIN_PCT", config.MID_MARGIN_PCT),
            "high": overrides.get("HIGH_MARGIN_PCT", config.HIGH_MARGIN_PCT),
        }
        self.tier_lev_cap = {
            "stable": overrides.get("STABLE_LEV_CAP", config.STABLE_LEV_CAP),
            "mid": overrides.get("MID_LEV_CAP", config.MID_LEV_CAP),
            "high": overrides.get("HIGH_LEV_CAP", config.HIGH_LEV_CAP),
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
        self.add_strategy = overrides.get("ADD_STRATEGY", config.ADD_STRATEGY)
        self.add_gap_k = overrides.get("ADD_GAP_K", config.ADD_GAP_K)
        self.pos_add_gap_k = overrides.get("POS_ADD_GAP_K", config.POS_ADD_GAP_K)
        self.add_shrink_g = overrides.get("ADD_GAP_SHRINK_G", config.ADD_GAP_SHRINK_G)
        self.add_max_hard = int(overrides.get("ADD_MAX_HARD", config.ADD_MAX_HARD))
        self.follow_pos_add = bool(overrides.get("FOLLOW_POS_ADD", config.FOLLOW_POS_ADD))
        self.add_frac = overrides.get("ADD_FRAC", config.ADD_FRAC)
        self.wallet_sector_side_cap_pct = 1.0
        self.wallet_sector_side_caps = {
            "stable": 1.0, "mid": 1.0, "high": 1.0, "stock": 1.0,
        }
        self.wallet_margin_cap_pct = 1.0
        self.wallet_max_open_positions = config.MAX_CONCURRENT_POS
        self.wallet_stock_side_max_positions = config.MAX_CONCURRENT_POS
        self.margin_equity_pct = overrides.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
        self.min_open_margin_pct = overrides.get("MIN_OPEN_MARGIN_PCT", config.MIN_OPEN_MARGIN_PCT)
        self.tail_close_enable = bool(overrides.get("TAIL_CLOSE_ENABLE", config.TAIL_CLOSE_ENABLE))
        self.tail_close_hard_remain_pct = overrides.get(
            "TAIL_CLOSE_HARD_REMAIN_PCT", config.TAIL_CLOSE_HARD_REMAIN_PCT)
        self.tail_close_risk_remain_pct = overrides.get(
            "TAIL_CLOSE_RISK_REMAIN_PCT", config.TAIL_CLOSE_RISK_REMAIN_PCT)
        self.tail_close_profit_giveback_pct = overrides.get(
            "TAIL_CLOSE_PROFIT_GIVEBACK_PCT", config.TAIL_CLOSE_PROFIT_GIVEBACK_PCT)
        self.smart_tp_enable = bool(overrides.get("SMART_TP_ENABLE", config.SMART_TP_ENABLE))
        self.smart_tp_arm_sigma = {
            "stable": overrides.get("SMART_TP_STABLE_ARM_SIGMA", config.SMART_TP_STABLE_ARM_SIGMA),
            "mid": overrides.get("SMART_TP_MID_ARM_SIGMA", config.SMART_TP_MID_ARM_SIGMA),
            "high": overrides.get("SMART_TP_HIGH_ARM_SIGMA", config.SMART_TP_HIGH_ARM_SIGMA),
        }
        self.smart_tp_giveback_pcts = tuple(overrides.get(key, default) for key, default in (
            ("SMART_TP_GIVEBACK_1_PCT", config.SMART_TP_GIVEBACK_1_PCT),
            ("SMART_TP_GIVEBACK_2_PCT", config.SMART_TP_GIVEBACK_2_PCT),
            ("SMART_TP_GIVEBACK_3_PCT", config.SMART_TP_GIVEBACK_3_PCT),
        ))
        self.smart_tp_close_pcts = tuple(overrides.get(key, default) for key, default in (
            ("SMART_TP_CLOSE_1_PCT", config.SMART_TP_CLOSE_1_PCT),
            ("SMART_TP_CLOSE_2_PCT", config.SMART_TP_CLOSE_2_PCT),
            ("SMART_TP_CLOSE_3_PCT", config.SMART_TP_CLOSE_3_PCT),
        ))
        self.smart_tp_tail_remain_pct = overrides.get(
            "SMART_TP_TAIL_REMAIN_PCT", config.SMART_TP_TAIL_REMAIN_PCT)
        self.smart_tp_target_reduce_exit_pct = overrides.get(
            "SMART_TP_TARGET_REDUCE_EXIT_PCT", config.SMART_TP_TARGET_REDUCE_EXIT_PCT)
        self.smart_tp_min_fee_mult = overrides.get(
            "SMART_TP_MIN_FEE_MULT", config.SMART_TP_MIN_FEE_MULT)
        self.coin_blacklist = parse_coin_blacklist(overrides.get("COIN_BLACKLIST", config.COIN_BLACKLIST))
        self.block_korean_stocks = bool(overrides.get("BLOCK_KOREAN_STOCKS", config.BLOCK_KOREAN_STOCKS))
        self.market_ctx = market_ctx or {}
        self.replay_cost_mult = max(1.0, f(overrides.get("REPLAY_COST_MULT", 1.0)))
        self.path_refinement_probe = bool(overrides.get("_PATH_REFINEMENT_PROBE", False))
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
        self.maintenance_margin_known = 0
        self.maintenance_margin_missing = 0
        self.deploy_samples = []
        self.tier_deploy_samples = {"stable": [], "mid": [], "high": []}
        self.path_equity_samples = []
        self.path_liquidation_times = []
        self.track_price_path = False
        self._last_open_detail = {}

    def open_sizing_params(self):
        return OpenSizingParams(
            high_sigma_min=self.high_sigma_min,
            tier_margin=self.tier_margin,
            tier_lev_cap=self.tier_lev_cap,
            tier_coin_cap=self.tier_coin_cap,
            min_lev=self.min_lev,
            min_open_margin_pct=self.min_open_margin_pct,
            capital_anchor=self.initial_balance,
            drawdown_exponent=config.SIZING_DRAWDOWN_EXPONENT,
            drawdown_max_multiplier=config.SIZING_DRAWDOWN_MAX_MULTIPLIER,
            margin_equity_pct=self.margin_equity_pct,
        )

    def sigma(self, coin):
        return self.sigmas.get(coin) or config.VOL_FALLBACK_SIGMA

    def tier(self, sigma: float, coin: str | None = None) -> str:
        return tier_for_sigma(sigma, self.high_sigma_min, coin)

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
        # though the engine was respecting its configured cap, which in turn falsely blocked every later selection.
        stamp = int(t or 0)
        risk_equity = max(1.0, self.risk_equity())
        self.deploy_samples.append((stamp, self.locked_margin() / risk_equity))
        tier_locked = {"stable": 0.0, "mid": 0.0, "high": 0.0}
        for position in self.open.values():
            tier = str(position.get("tier") or self.tier(
                self.sigma(position.get("coin")), position.get("coin"),
            ))
            if tier not in tier_locked:
                continue
            tier_locked[tier] += f(position.get("margin")) * (
                f(position.get("rem_size")) / max(config.FLAT, f(position.get("size")))
            )
        for tier, locked in tier_locked.items():
            self.tier_deploy_samples[tier].append((stamp, locked / risk_equity))

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

    def marked_equity(self):
        """Full marked equity with isolated-loss floors for path-risk reconstruction."""
        unrealized = 0.0
        for position in self.open.values():
            mark = self.last_px.get(position.get("coin")) or position.get("entry_px")
            raw = f(position.get("rem_size")) * (
                f(mark) - f(position.get("entry_px"))
            ) * f(position.get("sign"))
            effective_margin = max(0.0, f(position.get("margin"))) * (
                f(position.get("rem_size")) / max(f(position.get("size")), 1e-12)
            )
            unrealized += max(-effective_margin, raw)
        return self.balance + unrealized

    def _sample_path_equity(self, stamp):
        if not self.track_price_path or self.path_refinement_probe:
            return
        stamp = int(f(stamp))
        if stamp <= 0:
            return
        sample = {"time": stamp, "equity": self.marked_equity()}
        samples = self.path_equity_samples
        # A diversified source can touch more than one hundred markets, while the copied account normally
        # holds only a few of them at once.  The strict path still visits every cached candle.  Recording the
        # same marked equity for every unrelated candle used to create more than a million dictionaries for
        # one wallet and exhausted Swap during Top16 replay.
        #
        # Keep both ends of each constant-equity interval: the first sample owns the interval start and the
        # last sample owns the exact boundary before the next change.  Replacing only the middle/tail of an
        # equal run therefore preserves elapsed-time drawdown math, intra-candle adverse extrema and window
        # slicing while making storage proportional to actual equity changes rather than total market rows.
        if (
            len(samples) >= 2
            and f(samples[-1].get("equity")) == f(sample["equity"])
            and f(samples[-2].get("equity")) == f(sample["equity"])
        ):
            samples[-1] = sample
        else:
            samples.append(sample)

    def risk_available(self):
        return max(0.0, self.available() + min(0.0, self.unrealized()))

    def coin_cap_pct(self, tier):
        return self.tier_coin_cap[tier]

    def wallet_group_cap_pct(self, addr, coin, side, tier):
        return wallet_sector_side_effective_cap_pct(
            self.open.values(), addr=addr, coin=coin, side=side, candidate_tier=tier,
            tier_for_coin=lambda current_coin: self.tier(self.sigma(current_coin), current_coin),
            crypto_stable=self.wallet_sector_side_caps["stable"],
            crypto_mid=self.wallet_sector_side_caps["mid"],
            crypto_high=self.wallet_sector_side_caps["high"],
            stock=self.wallet_sector_side_caps["stock"],
        )

    def run(self, fills, price_path=None):
        fills = prepare_replay_fills(
            fills, addr=None if self.addr == "portfolio" else self.addr,
        )
        path_events = prepare_price_path(price_path)
        self.price_path_points = len(path_events)
        self.track_price_path = bool(path_events)
        if not path_events:
            for x in fills:
                self.process_fill(x)
            return self.result()

        fill_times = {}
        for row in fills:
            fill_times.setdefault(row.get("coin"), []).append(int(row.get("time") or 0))
        for times in fill_times.values():
            times.sort()
        path_has_fill_events = []
        for row in path_events:
            times = fill_times.get(row.get("coin")) or []
            lo = bisect.bisect_left(times, int(row.get("open_time") or row["time"]))
            hi = bisect.bisect_right(times, int(row.get("close_time") or row["time"]))
            path_has_fill_events.append(hi > lo)
        # Both streams are already sorted. A linear merge avoids allocating and sorting hundreds of
        # thousands of candle/fill tuples for every tuner candidate.
        path_i = fill_i = 0
        while path_i < len(path_events) or fill_i < len(fills):
            path_time = path_events[path_i]["time"] if path_i < len(path_events) else None
            fill_time = int(fills[fill_i].get("time") or 0) if fill_i < len(fills) else None
            if path_time is not None and (fill_time is None or path_time <= fill_time):
                self.process_price(
                    path_events[path_i], has_fill_events=path_has_fill_events[path_i],
                )
                path_i += 1
            else:
                self.process_fill(fills[fill_i])
                self._sample_path_equity(fill_time)
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
                self._attempt_open(addr, coin, x.get("time"), px, pos1, oid, x)
            elif abs(pos1) >= config.FLAT:
                self.skip_reasons["skip_midway"] += 1
            return

        ep["master_current"] = abs(pos1)

        if transition == "flip":
            ep["master_peak"] = max(ep["master_peak"], abs(pos0))
            self._apply_reduce(addr, coin, px, -pos0, 0.0, closing=True, t=x.get("time"))
            self._attempt_open(addr, coin, x.get("time"), px, pos1, oid, x)
            return

        ep["master_peak"] = max(ep["master_peak"], abs(pos1))
        if transition == "add":
            add_orders = ep.setdefault("add_orders", {})
            if oid is not None and oid in ep.get("source_open_oids", ()):
                # More fills from the source order that confirmed this opening extend the opening anchor.
                # They are not adds and must not consume smart-add spacing/capacity.
                source_open_notional = f(ep.get("master_first_notl")) + abs(signed) * px
                ep["master_first_notl"] = source_open_notional
                ep["target_initial_notl"] = source_open_notional
                if abs(pos1) > 0:
                    ep["master_open_px"] = source_open_notional / abs(pos1)
                return
            if oid is not None and oid in ep["seen_oids"] and oid not in add_orders:
                # One source add order is one Copy decision. Preserve the source's final exposure/average
                # after our single execution, but never top the Copy position up again for later fill slices.
                m_now = abs(pos1)
                if m_now > 0 and ep.get("master_open_px"):
                    m_prev = abs(pos1 - signed)
                    ep["master_open_px"] = (
                        m_prev * ep["master_open_px"] + abs(signed) * px
                    ) / m_now
                ep["target_add_notl"] += abs(signed) * px
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

    def _attempt_open(self, addr, coin, t, px, pos1, oid, fill=None):
        """Open once and retain the time-local outcome for continuous-window slicing."""
        opened_before = self.opened_n
        skips_before = Counter(self.skip_reasons)
        self._last_open_detail = {}
        self._open_position(addr, coin, t, px, pos1, oid, fill)
        if self.opened_n > opened_before:
            outcome = "opened"
        else:
            changed = [
                key for key, value in self.skip_reasons.items()
                if int(value) > int(skips_before.get(key, 0))
            ]
            outcome = changed[-1] if changed else "skip_unknown_open"
        event = {
            "time": int(f(t)),
            "outcome": outcome,
            **dict(self._last_open_detail or {}),
        }
        self.open_events.append(event)
        return event

    def process_price(self, x, *, has_fill_events=None):
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
        boundary_positions = [
            ep for (_addr, current_coin), ep in self.open.items()
            if current_coin == coin
            and x.get("open_time") is not None
            and int(ep.get("opened_at") or 0) > int(x.get("open_time") or 0)
        ]
        has_fill_events = (
            bool(x.get("has_fill_events"))
            if has_fill_events is None else bool(has_fill_events)
        )
        ambiguous_candle = has_fill_events or bool(boundary_positions)
        # Capture the candle's adverse account-equity extreme without assuming high/low order. The close
        # sample below is the state carried into elapsed-time deep-bag calculations. If a fill occurred
        # inside this candle, its high/low may predate the changed position. Never let that unresolved range
        # manufacture a deep-drawdown sample; finer path data must resolve it.
        if not ambiguous_candle:
            for probe in (lo, hi):
                self.last_px[coin] = probe
                self._sample_path_equity(x.get("time"))
        self.last_px[coin] = close
        self.path_mark_coins.add(coin)
        self._mark_liquidations_range(
            coin, lo, hi, x.get("time"), candle_open_time=x.get("open_time"),
            ambiguous=has_fill_events, candle_close_time=x.get("close_time"),
        )
        # Candle close is always after its favorable extreme, so it is safe to update the high-water from
        # high/low and evaluate giveback at close without inventing an intra-candle high/low ordering.
        for (addr, c), ep in list(self.open.items()):
            if c != coin:
                continue
            boundary = (
                x.get("open_time") is not None
                and int(ep.get("opened_at") or 0) > int(x.get("open_time") or 0)
            )
            if boundary or ambiguous_candle:
                # The candle's favorable extreme may predate an entry/add/reduce inside that candle.
                # Without a finer path, skipping the TP update is safer than manufacturing a high-water.
                continue
            favorable = hi if ep["side"] == "long" else lo
            self._advance_smart_take_profit(addr, coin, ep, favorable, x.get("time"), allow_cut=False)
            if (addr, coin) in self.open:
                self._advance_smart_take_profit(addr, coin, ep, close, x.get("time"), allow_cut=True)
        self._sample_path_equity(x.get("close_time") or x.get("time"))

    def _open_position(self, addr, coin, t, px, pos1, oid, fill=None):
        sigma = self.sigma(coin)
        tier = self.tier(sigma, coin)
        self._last_open_detail = {
            "coin": coin,
            "tier": tier,
            "master_notional": abs(pos1) * px,
        }
        if coin_is_blocked(coin, self.coin_blacklist, block_korean_stocks=self.block_korean_stocks):
            self.skip_reasons["skip_coin_blacklist"] += 1
            return
        side = "long" if pos1 > 0 else "short"
        sign = 1 if side == "long" else -1
        wallet_key = str(addr or "").lower()
        wallet_open_n = sum(
            1 for position in self.open.values()
            if str(position.get("addr") or "").lower() == wallet_key
        )
        if wallet_open_n >= self.wallet_max_open_positions:
            self.skip_reasons["skip_wallet_position_cap"] += 1
            return
        if str(coin).lower().startswith("xyz:") and wallet_sector_side_position_count(
            self.open.values(), addr=addr, coin=coin, side=side,
        ) >= self.wallet_stock_side_max_positions:
            self.skip_reasons["skip_wallet_stock_side_position_cap"] += 1
            return
        target_notl = abs(pos1) * px
        risk_equity = self.risk_equity()
        avail = self.risk_available()
        existing_coin = sum(
            p["margin"] * (p["rem_size"] / p["size"] if p["size"] else 1.0)
            for (addr, c), p in self.open.items()
            if c == coin and p["side"] == side
        )
        group_existing = wallet_sector_side_margin(
            self.open.values(), addr=addr, coin=coin, side=side,
        )
        group_cap = self.wallet_group_cap_pct(addr, coin, side, self.tier(sigma, coin))
        group_room = wallet_sector_side_margin_room(
            cap_pct=group_cap,
            risk_equity=risk_equity,
            existing_margin=group_existing,
        )
        source_existing = wallet_margin(self.open.values(), addr=addr)
        source_room = margin_cap_room(
            cap_pct=self.wallet_margin_cap_pct,
            risk_equity=risk_equity,
            existing_margin=source_existing,
        )
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
            master_leverage=None,
            params=self.open_sizing_params(),
            maintenance_leverage=maintenance_leverage,
            wallet_sector_side_room=group_room,
            wallet_room=source_room,
        )
        self._last_open_detail.update({
            "tier": plan.tier,
            "master_notional": f(plan.master_notional),
            "copy_notional": f(plan.notional),
        })
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
            "tier": tier,
            "sign": sign,
            "opened_at": t,
            "master_open_px": px,
            "master_peak": abs(pos1),
            "master_current": abs(pos1),
            "master_first_notl": target_notl,
            "target_initial_notl": target_notl,
            "target_add_notl": 0.0,
            "target_adds": 0,
            "entry_px": px,
            "risk_equity_at_open": risk_equity,
            "size": size,
            "rem_size": size,
            "peak_size": size,
            "margin": margin,
            "first_margin": margin,
            "notional": notional,
            "leverage": lev,
            "maintenance_leverage": maintenance_leverage,
            "liq_px": plan.liq_px,
            "last_target_add_px": px,
            "add_count": 0,
            "followed_adds": 0,
            "missed_adds": 0,
            "entry_fees": fee,
            "exit_fees": 0.0,
            "gross_pnl": 0.0,
            "realized_net": -fee,
            "seen_oids": ({oid} if oid is not None else set()),
            "source_open_oids": ({oid} if oid is not None else set()),
            "add_orders": {},
            "observed_add_oids": set(),
            "missed_add_oids": set(),
            "add_order_outcomes": {},
            "add_outcome_counts": Counter(),
            "reduce_anchor": None,
            "smart_tp_armed": False,
            "smart_tp_stage": 0,
            "smart_tp_peak_pnl": 0.0,
            "smart_tp_base_size": 0.0,
            "smart_tp_master_anchor": 0.0,
        }
        self.opened_n += 1
        self.copy_peak_concurrent = max(self.copy_peak_concurrent, len(self.open))
        self._sample_deploy(t)

    def _record_add_outcome(self, ep, oid, outcome, *, t=None):
        """Assign one final outcome to a distinct target add order.

        A same-oid order can first look like noise and become actionable after later fill slices move its
        aggregate VWAP. Once followed, that OID is final and later slices cannot create another Copy order.
        Reclassification decrements the old bucket before incrementing the new one, so an order is never
        simultaneously counted as both ignored and followed.
        """
        if outcome not in ADD_OUTCOMES:
            raise ValueError(f"unknown add outcome: {outcome}")
        outcomes = ep.setdefault("add_order_outcomes", {})
        prior = outcomes.get(oid) if oid is not None else None
        if prior == outcome:
            return outcome == "followed"
        if prior:
            ep["add_outcome_counts"][prior] -= 1
            self.add_outcome_counts[prior] -= 1
            if prior == "followed":
                ep["followed_adds"] = max(0, ep["followed_adds"] - 1)
                self.followed_adds = max(0, self.followed_adds - 1)
            else:
                ep["missed_adds"] = max(0, ep["missed_adds"] - 1)
                self.missed_adds = max(0, self.missed_adds - 1)
        if oid is not None:
            outcomes[oid] = outcome
        ep["add_outcome_counts"][outcome] += 1
        self.add_outcome_counts[outcome] += 1
        if outcome == "followed":
            ep["followed_adds"] += 1
            self.followed_adds += 1
        else:
            ep["missed_adds"] += 1
            self.missed_adds += 1
        event_key = None
        if oid is not None:
            event_key = (
                str(ep.get("addr") or "").lower(),
                str(ep.get("coin") or ""),
                str(oid),
            )
        event = self.add_event_by_order.get(event_key) if event_key is not None else None
        if event is None:
            event = {
                "time": int(f(t)),
                "outcome": outcome,
                "coin": ep.get("coin"),
                "tier": ep.get("tier") or self.tier(
                    self.sigma(ep.get("coin")), ep.get("coin"),
                ),
            }
            self.add_events.append(event)
            if event_key is not None:
                self.add_event_by_order[event_key] = event
        else:
            # A later slice of the same target order may turn initial noise into one followed add.  Attribute
            # the final decision to the latest decisive slice so a recent-window view does not inherit an old
            # classification or count the same order twice.
            event["time"] = int(f(t))
            event["outcome"] = outcome
        return outcome == "followed"

    def _observe_add(self, ep, oid=None, reason="noise_merged", *, t=None):
        self._record_add_outcome(ep, oid, reason, t=t)
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

        # Once our first proactive profit cut has executed, the released exposure stays released.  Target
        # re-adds are observed for source state but never rebuild the protected position.
        if self.smart_tp_enable and int(ep.get("smart_tp_stage") or 0) > 0:
            self.skip_reasons["skip_smart_tp_readd"] += 1
            return self._observe_add(ep, oid, "noise_merged", t=t)

        sigma = self.sigma(coin)
        tier = self.tier(sigma, coin)
        is_buy = ep["side"] == "long"
        risk_equity = self.risk_equity()
        risk_available = self.risk_available()
        group_existing = wallet_sector_side_margin(
            self.open.values(), addr=addr, coin=coin, side=ep["side"],
        )
        group_cap = self.wallet_group_cap_pct(addr, coin, ep["side"], tier)
        group_room = wallet_sector_side_margin_room(
            cap_pct=group_cap,
            risk_equity=risk_equity,
            existing_margin=group_existing,
        )
        source_room = margin_cap_room(
            cap_pct=self.wallet_margin_cap_pct,
            risk_equity=risk_equity,
            existing_margin=wallet_margin(self.open.values(), addr=addr),
        )
        total_room = risk_available
        existing = sum(
            p["margin"] * (p["rem_size"] / p["size"] if p["size"] else 1.0)
            for (addr, c), p in self.open.items()
            if c == coin and p["side"] == ep["side"]
        )
        coin_room = max(0.0, self.coin_cap_pct(tier) * risk_equity - existing)
        if self.add_strategy == "smart":
            last = ep.get("last_target_add_px") or ep["master_open_px"]
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
                    return self._observe_add(ep, oid, "noise_merged", t=t)
                if ep["add_count"] >= self.add_max_hard:
                    return self._observe_add(ep, oid, "hard_cap_blocked", t=t)
            ratio = target_order_notl / ep["master_first_notl"] if ep["master_first_notl"] else self.add_frac
            followed_margin = order["followed_margin"] if order else 0.0
            desired_remaining = max(
                0.0,
                min(max(0.0, ratio) * ep["first_margin"], ep["first_margin"]) - followed_margin,
            )
            add_margin = smart_add_order_margin(
                first_margin=ep["first_margin"],
                target_ratio=ratio,
                followed_margin=followed_margin,
                coin_room=coin_room,
                risk_available=risk_available,
                wallet_sector_side_room=group_room,
                wallet_room=source_room,
                total_margin_room=total_room,
            )
            if add_margin < self.min_open_margin_pct * risk_equity * self.margin_equity_pct:
                if already_counted:
                    return False
                eps = 1e-12
                if group_room + eps < desired_remaining and group_room <= min(coin_room, risk_available) + eps:
                    reason = "wallet_sector_side_cap_blocked"
                elif source_room + eps < desired_remaining and source_room <= min(coin_room, risk_available) + eps:
                    reason = "wallet_cap_blocked"
                elif coin_room + eps < desired_remaining and coin_room <= risk_available + eps:
                    reason = "coin_cap_blocked"
                elif risk_available + eps < desired_remaining and risk_available < coin_room - eps:
                    reason = "cash_blocked"
                else:
                    reason = "min_margin_blocked"
                return self._observe_add(ep, oid, reason, t=t)
        else:
            max_adds = self.tier_max_adds[tier]
            if ep["add_count"] >= max_adds:
                return self._observe_add(ep, oid, "hard_cap_blocked", t=t)
            add_margin = max(0.0, min(
                ep["first_margin"] * self.add_frac,
                coin_room,
                risk_available,
                group_room,
                source_room,
                total_room,
            ))
            if add_margin <= 0:
                if group_room <= min(coin_room, risk_available):
                    reason = "wallet_sector_side_cap_blocked"
                elif source_room <= min(coin_room, risk_available):
                    reason = "wallet_cap_blocked"
                else:
                    reason = "coin_cap_blocked" if coin_room <= risk_available else "cash_blocked"
                return self._observe_add(ep, oid, reason, t=t)

        basis = rebase_isolated_position(
            ep["entry_px"], ep["side"], ep["rem_size"], ep["leverage"],
            ep.get("maintenance_leverage"),
        )
        ep.update(
            size=basis["size"], margin=basis["margin"],
            notional=basis["notional"], liq_px=basis["liq_px"],
        )
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
            ep.get("maintenance_leverage"),
        )
        first_copy_for_order = not (order and order["counted"])
        if first_copy_for_order:
            ep["add_count"] += 1
            self._record_add_outcome(ep, oid, "followed", t=t)
        ep["last_target_add_px"] = decision_px
        ep["reduce_anchor"] = None
        # A followed add changes both size and average entry.  Before the first proactive cut it starts a
        # fresh arm/high-water episode; after a cut adds are blocked above.
        ep["smart_tp_armed"] = False
        ep["smart_tp_stage"] = 0
        ep["smart_tp_peak_pnl"] = 0.0
        ep["smart_tp_base_size"] = 0.0
        ep["smart_tp_master_anchor"] = 0.0
        fee = abs(add_size * px) * config.TAKER_FEE * self.replay_cost_mult
        ep["entry_fees"] += fee
        ep["realized_net"] -= fee
        self.balance -= fee
        self.fee_drag += fee
        if order is not None:
            order["followed_margin"] += add_margin
            order["counted"] = True
            # The first successful Copy execution seals this source OID. ``process_fill`` will still merge
            # later source slices into audit exposure, but it cannot dispatch another add.
            if oid is not None:
                ep.setdefault("add_orders", {}).pop(oid, None)
        self._sample_deploy(t)
        return True

    def _apply_reduce(self, addr, coin, px, signed, pos1, closing=False, status="closed", t=None,
                      smart_tp_stage=None, forced_frac=None):
        key = (addr, coin)
        ep = self.open.get(key)
        if not ep:
            return
        old_rem = ep["rem_size"]
        if forced_frac is not None:
            reduce_frac = max(0.0, min(1.0, f(forced_frac)))
            closing = closing or reduce_frac >= 1.0
        elif smart_tp_stage is not None:
            if int(ep.get("smart_tp_stage") or 0) != int(smart_tp_stage):
                return
            decision = self._smart_take_profit_decision(ep, px)
            if not decision.trigger:
                return
            reduce_frac = min(1.0, decision.close_size / max(ep["rem_size"], 1e-12))
        elif closing or abs(pos1 - signed) < config.FLAT:
            reduce_frac = 1.0
            closing = True
        else:
            if (self.smart_tp_enable
                    and int(ep.get("smart_tp_stage") or 0) >= len(self.smart_tp_close_pcts)):
                anchor = float(ep.get("smart_tp_master_anchor") or 0.0)
                if anchor > 0 and abs(pos1) <= anchor * (1.0 - self.smart_tp_target_reduce_exit_pct) + config.FLAT:
                    reduce_frac = 1.0
                    closing = True
                    status = "tail_closed"
                else:
                    # The protected 30% tail is intentionally not chipped into dust.  Ignore target trims
                    # below the cumulative exit line and close it once that line is reached.
                    return
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
        elif not closing and smart_tp_stage is None and not self.smart_tp_enable:
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
        if not closing and ep["rem_size"] > config.FLAT:
            basis = rebase_isolated_position(
                ep["entry_px"], ep["side"], ep["rem_size"], ep["leverage"],
                ep.get("maintenance_leverage"),
            )
            ep.update(
                size=basis["size"], margin=basis["margin"],
                notional=basis["notional"], liq_px=basis["liq_px"],
            )
        if smart_tp_stage is not None:
            ep["smart_tp_stage"] = int(smart_tp_stage) + 1
            if int(smart_tp_stage) == 0 and not ep.get("smart_tp_master_anchor"):
                ep["smart_tp_master_anchor"] = float(ep.get("master_current") or abs(pos1) or 0.0)
            ep["smart_tp_peak_pnl"] = max(
                0.0, ep["rem_size"] * (px - ep["entry_px"]) * ep["sign"],
            )
            self.skip_reasons["smart_tp_cut"] += 1
        elif not closing and ep.get("smart_tp_armed") and old_rem > 0:
            # A normal mirrored reduce changes dollars, not the high-water price.  Scale the stored peak
            # with remaining size so the next drawdown comparison stays on the same price level.
            ep["smart_tp_peak_pnl"] = max(
                0.0, float(ep.get("smart_tp_peak_pnl") or 0.0) * ep["rem_size"] / old_rem,
            )
        if closing:
            ep["closed_at"] = t
            ep["status"] = status
            if status == "liquidated":
                # Keep the actual forced-close loss separate from cumulative episode PnL. Earlier partial
                # profit must not hide a material liquidation on the remaining isolated position.
                ep["liquidation_loss"] = max(0.0, -pnl)
            self.closed.append(ep)
            self.open.pop(key, None)
            if status == "liquidated":
                self.path_liquidation_times.append(int(f(t)))
        self._sample_deploy(t)

    def _smart_take_profit_decision(self, ep, mark_px):
        sigma = self.sigma(ep["coin"])
        return smart_take_profit_decision(
            enabled=self.smart_tp_enable,
            rem_size=ep["rem_size"],
            base_size=ep.get("smart_tp_base_size", 0.0),
            entry_px=ep["entry_px"],
            mark_px=mark_px,
            side=ep["side"],
            sigma=sigma,
            tier=self.tier(sigma, ep["coin"]),
            armed=bool(ep.get("smart_tp_armed")),
            stage=int(ep.get("smart_tp_stage") or 0),
            peak_pnl=float(ep.get("smart_tp_peak_pnl") or 0.0),
            arm_sigma=self.smart_tp_arm_sigma,
            giveback_pcts=self.smart_tp_giveback_pcts,
            close_pcts=self.smart_tp_close_pcts,
            tail_remain_pct=self.smart_tp_tail_remain_pct,
            fee_rate=config.TAKER_FEE * self.replay_cost_mult,
            min_fee_multiple=self.smart_tp_min_fee_mult,
        )

    def _advance_smart_take_profit(self, addr, coin, ep, mark_px, t, *, allow_cut):
        if not self.smart_tp_enable or (addr, coin) not in self.open:
            return
        decision = self._smart_take_profit_decision(ep, mark_px)
        ep["smart_tp_armed"] = decision.armed
        ep["smart_tp_peak_pnl"] = decision.peak_pnl
        ep["smart_tp_base_size"] = decision.base_size
        if allow_cut and decision.trigger:
            self._apply_reduce(
                addr, coin, mark_px, 0.0, float(ep.get("master_current") or 0.0),
                t=t, smart_tp_stage=decision.stage,
            )

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
                sigma=self.sigma(coin),
            ))
        closed_positions = [summarize_position(p, sigma=self.sigma(p.get("coin"))) for p in self.closed]
        all_positions = closed_positions + open_positions
        liquidation_risk = liquidation_loss_metrics(
            closed_positions, fallback_equity=self.initial_balance,
        )
        closed_net = sum(p["realized_net"] for p in self.closed)
        wins = sum(1 for p in self.closed if p["realized_net"] > 0)
        liquidations = sum(1 for p in self.closed if p.get("status") == "liquidated")
        tail_profit_closes = sum(1 for p in self.closed if p.get("status") == "tail_closed")
        natural_closes = max(0, len(self.closed) - liquidations)
        path_completion_rate = natural_closes / len(self.closed) if self.closed else 1.0
        initial_notl = sum(p["target_initial_notl"] for p in self.closed) + sum(p["target_initial_notl"] for p in self.open.values())
        add_notl = sum(p["target_add_notl"] for p in self.closed) + sum(p["target_add_notl"] for p in self.open.values())
        open_metrics = open_execution_metrics(self.open_events)
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
        deploy_distribution = deployment_distribution(
            self.deploy_samples, end_ms=self.valuation_asof_ms,
        )
        add_metrics = add_fidelity_metrics(all_positions, self.add_outcome_counts)
        profit_metrics = profit_structure_metrics(closed_positions, total_net=closed_net)
        path_metrics = path_risk_metrics(
            self.path_equity_samples,
            initial_equity=self.initial_balance,
            liquidation_times=self.path_liquidation_times,
        )
        open_rate = f(open_metrics.get("effective_open_follow_rate"))
        behavior_v2 = _clamp01(
            open_rate
            * (f(add_metrics.get("effective_add_fidelity")) if add_metrics.get("effective_add_fidelity") is not None else 1.0)
            * path_completion_rate
        )
        behavior_legacy = _clamp01(
            open_rate
            * (1.0 - (self.missed_adds / self.target_adds if self.target_adds else 0.0))
            * path_completion_rate
        )

        def concentration(key):
            buckets = {}
            total_abs = 0.0
            for position in all_positions:
                value = _endpoint_pnl(position)
                bucket = key(position)
                buckets[bucket] = buckets.get(bucket, 0.0) + value
                total_abs += abs(value)
            return (max((abs(value) for value in buckets.values()), default=0.0) / total_abs) if total_abs else 0.0

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
        path_complete = bool(
            self.price_path_points
            and price_path_coverage >= float(getattr(config, "AUTO_TUNE_PRICE_PATH_MIN_COVERAGE", 0.95))
            and not self.price_path_meta.get("missingCoins")
        )
        if not path_complete:
            path_metrics["path_risk_status"] = "missing"
        latest_path_ms = max((int(row.get("time") or 0) for row in self.path_equity_samples), default=0)
        current_asof_ms = int(self.valuation_asof_ms or latest_path_ms or 0)
        aggregate_open_loss = min(0.0, unreal) / self.initial_balance
        losing_open = [
            position for position in open_positions if _endpoint_pnl(position) < 0.0
        ]
        worst_position_open_loss = min(
            (_endpoint_pnl(position) for position in losing_open), default=0.0,
        ) / self.initial_balance
        current_open_loss = min(aggregate_open_loss, worst_position_open_loss)
        oldest_losing_open = min(
            (int(position.get("opened_at") or 0) for position in losing_open),
            default=0,
        )
        current_bag_hours = (
            max(0, current_asof_ms - oldest_losing_open) / 3_600_000
            if current_open_loss < 0.0 and oldest_losing_open and current_asof_ms else 0.0
        )
        result = {
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
            "wallet_margin_cap_pct": self.wallet_margin_cap_pct,
            "wallet_sector_side_cap_pct": self.wallet_sector_side_cap_pct,
            "wallet_sector_side_caps": dict(self.wallet_sector_side_caps),
            "wallet_max_open_positions": self.wallet_max_open_positions,
            "wallet_stock_side_max_positions": self.wallet_stock_side_max_positions,
            # Qualification returns are normalized to the full Paper risk capital.
            # ``MARGIN_EQUITY_PCT`` is a sizing budget, not a smaller return denominator.
            "initial_margin_equity": self.initial_balance,
            "closed_net_pnl": closed_net,
            "copy_gross_pnl": self.gross_pnl,
            "unrealized_pnl": unreal,
            "valuation_status": "complete" if not missing_mark_coins else "missing_marks",
            "valuation_coverage": valued_open / len(self.open) if self.open else 1.0,
            "valuation_missing_coins": sorted(set(missing_mark_coins)),
            "valuation_asof_ms": self.valuation_asof_ms,
            "fee_drag": self.fee_drag,
            "add_events": list(self.add_events),
            "target_open_events": self.target_open_events,
            "opened_n": self.opened_n,
            "open_events": list(self.open_events),
            "raw_target_open_events": open_metrics["raw_target_open_events"],
            "small_open_excluded_n": open_metrics["small_open_excluded_n"],
            "effective_target_open_events": open_metrics["effective_target_open_events"],
            "raw_open_capture_rate": open_metrics["raw_open_capture_rate"],
            "effective_open_follow_rate": open_rate,
            "open_execution_audit": open_metrics["open_execution_audit"],
            "open_fill_rate": open_rate,
            "add_dependency": add_notl / initial_notl if initial_notl else 0.0,
            "target_peak_concurrent": self.target_peak_concurrent,
            "copy_peak_concurrent": self.copy_peak_concurrent,
            "max_concurrent_fit": self.copy_peak_concurrent / self.target_peak_concurrent if self.target_peak_concurrent else 1.0,
            "capacity_open_fit": open_metrics["capacity_open_fit"],
            "execution_capacity_fit": open_metrics["execution_capacity_fit"],
            "cash_congestion_fit": open_metrics["cash_congestion_fit"],
            "open_constraint_counts": open_metrics["open_constraint_counts"],
            "open_constraint_fit": open_metrics["open_constraint_fit"],
            "actionable_open_rate": open_rate,
            "execution_fill_rate": open_rate,
            "behavior_replication_rate": behavior_v2,
            "behavior_replication_v2": behavior_v2,
            "behavior_replication_rate_legacy": behavior_legacy,
            "equity_curve": curve,
            "max_drawdown": max_drawdown,
            "worst_day": min(daily_values, default=0.0),
            "cvar95": cvar95,
            "peak_deploy_pct": peak_deploy_pct,
            "avg_deploy_pct": avg_deploy_pct,
            "deployment_distribution": deploy_distribution,
            "deploy_samples": [
                {"time": int(stamp), "pct": float(value)}
                for stamp, value in self.deploy_samples
            ],
            "tier_deploy_samples": {
                tier: [
                    {"time": int(stamp), "pct": float(value)}
                    for stamp, value in samples
                ]
                for tier, samples in self.tier_deploy_samples.items()
            },
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
            "path_equity_samples": self.path_equity_samples,
            "path_liquidation_times": self.path_liquidation_times,
            "current_open_loss_frac": current_open_loss,
            "current_bag_hours": current_bag_hours,
            "maintenance_margin_coverage": maintenance_coverage,
            "maintenance_margin_known": self.maintenance_margin_known,
            "maintenance_margin_missing": self.maintenance_margin_missing,
            "model_coverage": min(maintenance_coverage, price_path_coverage),
            "fallback_reasons": fallback_reasons,
            "skip_reasons": dict(self.skip_reasons),
            "positions": closed_positions,
            "open_positions": open_positions,
        }
        result.update(liquidation_risk)
        result.update(add_metrics)
        result.update(profit_metrics)
        result.update(path_metrics)
        result["tier_economics"] = tier_economics(
            closed_positions, open_positions,
            open_events=self.open_events,
            add_events=self.add_events,
            tier_deploy_samples=result["tier_deploy_samples"],
        )
        return result


def summarize_position(p, *, mark_px=None, unrealized_pnl=None, valuation_complete=None, sigma=None):
    out = {
        "addr": p.get("addr"),
        "coin": p["coin"],
        "tier": p.get("tier") or tier_for_sigma(
            f(sigma) if sigma is not None else config.VOL_FALLBACK_SIGMA,
            config.HIGH_SIGMA_MIN,
            p.get("coin"),
        ),
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
        "add_outcome_counts": {
            key: int((p.get("add_outcome_counts") or {}).get(key) or 0)
            for key in ADD_OUTCOMES
        },
        "entry_px": p["entry_px"],
        "master_avg_px": p["master_open_px"],
        "leverage": p["leverage"],
        "margin": p["margin"],
        "risk_equity_at_open": p.get("risk_equity_at_open"),
        "liquidation_loss": p.get("liquidation_loss"),
        "remaining_size": p.get("rem_size"),
    }
    if p.get("target_adds"):
        entry_px = f(p.get("entry_px"))
        master_px = f(p.get("master_open_px"))
        if entry_px > 0.0 and master_px > 0.0:
            log_gap = abs(math.log(entry_px / master_px))
            out["entry_gap_pct"] = abs(entry_px / master_px - 1.0)
            out["entry_gap_sigma"] = log_gap / max(
                1e-9,
                f(sigma) if sigma is not None else config.VOL_FALLBACK_SIGMA,
            )
    if mark_px is not None:
        out["mark_px"] = mark_px
    if unrealized_pnl is not None:
        out["unrealized_pnl"] = unrealized_pnl
    if valuation_complete is not None:
        out["valuation_complete"] = bool(valuation_complete)
    return out


def tier_economics(closed_positions, open_positions, *, open_events=(), add_events=(),
                   tier_deploy_samples=None) -> dict:
    """Compact economic attribution for each volatility tier from an existing replay.

    This is deliberately derived from the replay's already-materialized endpoints and event streams;
    it never starts another replay and never persists per-trade detail.
    """
    tiers = {
        tier: {
            "closedEpisodes": 0, "openEpisodes": 0, "wins": 0,
            "liquidations": 0, "realizedNetPnl": 0.0, "unrealizedPnl": 0.0,
            "netPnl": 0.0, "fees": 0.0, "grossProfit": 0.0, "grossLoss": 0.0,
            "losses": 0,
            "worstLoss": 0.0,
            "marginSum": 0.0, "notionalSum": 0.0, "positionSamples": 0,
            "targetOpens": 0, "opened": 0, "missedOpens": 0,
            "targetAdds": 0, "followedAdds": 0, "missedAdds": 0,
            "avgDeployPct": 0.0, "peakDeployPct": 0.0,
        }
        for tier in ("stable", "mid", "high")
    }
    for is_open, rows in ((False, closed_positions or ()), (True, open_positions or ())):
        for position in rows:
            tier = str(position.get("tier") or "")
            if tier not in tiers:
                continue
            item = tiers[tier]
            item["openEpisodes" if is_open else "closedEpisodes"] += 1
            endpoint = f(position.get("net_pnl")) + (
                f(position.get("unrealized_pnl")) if is_open else 0.0
            )
            item["realizedNetPnl"] += f(position.get("net_pnl"))
            item["unrealizedPnl"] += f(position.get("unrealized_pnl")) if is_open else 0.0
            item["netPnl"] += endpoint
            item["fees"] += f(position.get("fee_drag"))
            item["grossProfit"] += max(0.0, endpoint)
            item["grossLoss"] += max(0.0, -endpoint)
            item["worstLoss"] = min(item["worstLoss"], endpoint)
            item["wins"] += int(endpoint > 0.0)
            item["losses"] += int(endpoint < 0.0)
            item["liquidations"] += int(position.get("status") == "liquidated")
            margin = f(position.get("margin"))
            leverage = f(position.get("leverage"))
            item["marginSum"] += margin
            item["notionalSum"] += margin * leverage
            item["positionSamples"] += 1
            item["targetAdds"] += int(position.get("target_adds") or 0)
            item["followedAdds"] += int(position.get("followed_adds") or 0)
            item["missedAdds"] += int(position.get("missed_adds") or 0)
    for event in open_events or ():
        tier = str(event.get("tier") or "")
        if tier not in tiers:
            continue
        tiers[tier]["targetOpens"] += 1
        if event.get("outcome") == "opened":
            tiers[tier]["opened"] += 1
        else:
            tiers[tier]["missedOpens"] += 1
    # Add events are retained for future outcome expansion; position aggregates remain the canonical
    # deduplicated add counts because one OID can be reclassified by later fill slices.
    for tier, item in tiers.items():
        samples = list((tier_deploy_samples or {}).get(tier) or ())
        values = [f(row.get("pct")) for row in samples]
        n = int(item.pop("positionSamples"))
        item["averageMargin"] = item.pop("marginSum") / n if n else 0.0
        item["averageNotional"] = item.pop("notionalSum") / n if n else 0.0
        item["profitFactor"] = (
            item["grossProfit"] / item["grossLoss"]
            if item["grossLoss"] > 0.0 else None
        )
        item["payoffRatio"] = (
            (item["grossProfit"] / item["wins"])
            / (item["grossLoss"] / item["losses"])
            if item["wins"] and item["losses"] and item["grossLoss"] > 0.0
            else None
        )
        item["openCaptureRate"] = (
            item["opened"] / item["targetOpens"] if item["targetOpens"] else 1.0
        )
        item["addCaptureRate"] = (
            item["followedAdds"] / item["targetAdds"] if item["targetAdds"] else 1.0
        )
        weighted_total = weighted_time = 0.0
        ordered_samples = sorted(samples, key=lambda row: int(row.get("time") or 0))
        deltas = [
            max(1, int(right.get("time") or 0) - int(left.get("time") or 0))
            for left, right in zip(ordered_samples, ordered_samples[1:])
        ]
        tail_weight = deltas[-1] if deltas else 1
        for index, row in enumerate(ordered_samples):
            weight = deltas[index] if index < len(deltas) else tail_weight
            weighted_total += f(row.get("pct")) * weight
            weighted_time += weight
        item["avgDeployPct"] = weighted_total / weighted_time if weighted_time else 0.0
        item["peakDeployPct"] = max(values, default=0.0)
    total_signals = sum(item["targetOpens"] for item in tiers.values())
    total_net = sum(item["netPnl"] for item in tiers.values())
    total_deploy = sum(item["avgDeployPct"] for item in tiers.values())
    for item in tiers.values():
        item["signalShare"] = item["targetOpens"] / total_signals if total_signals else 0.0
        item["profitContribution"] = item["netPnl"] / total_net if total_net else 0.0
        item["capitalContribution"] = (
            item["avgDeployPct"] / total_deploy if total_deploy else 0.0
        )
    return tiers


def deployment_distribution(samples, *, end_ms=None) -> dict:
    """Time-weighted deployment distribution derived from one replay's event samples."""
    by_time = {}
    for row in samples or ():
        item = (
            {"time": int(row.get("time") or 0), "pct": max(0.0, f(row.get("pct")))}
            if isinstance(row, dict)
            else {"time": int(row[0] or 0), "pct": max(0.0, f(row[1]))}
        )
        # Several fills can share a millisecond. The last state at that instant owns the next interval.
        by_time[item["time"]] = item
    rows = sorted(by_time.values(), key=lambda row: row["time"])
    if not rows:
        return {
            "timeWeightedAvgDeployPct": 0.0,
            "activeTimeWeightedAvgDeployPct": 0.0,
            "activeTimeShare": 0.0,
            "percentiles": {key: 0.0 for key in ("p50", "p75", "p90", "p95", "p99")},
            "timeAbove": {key: 0.0 for key in ("70", "80", "90", "95")},
        }
    deltas = [max(0, right["time"] - left["time"]) for left, right in zip(rows, rows[1:])]
    terminal = int(end_ms or rows[-1]["time"])
    tail_weight = max(0, terminal - rows[-1]["time"])
    weighted = [
        (row["pct"], deltas[index] if index < len(deltas) else tail_weight)
        for index, row in enumerate(rows)
    ]
    weighted = [(value, weight) for value, weight in weighted if weight > 0]
    if not weighted:
        weighted = [(rows[-1]["pct"], 1)]
    total_weight = sum(weight for _value, weight in weighted) or 1
    active_weight = sum(weight for value, weight in weighted if value > 1e-12)

    def weighted_quantile(q: float) -> float:
        threshold = total_weight * q
        cumulative = 0
        for value, weight in sorted(weighted):
            cumulative += weight
            if cumulative >= threshold:
                return value
        return max(value for value, _weight in weighted)

    return {
        "timeWeightedAvgDeployPct": sum(value * weight for value, weight in weighted) / total_weight,
        "activeTimeWeightedAvgDeployPct": (
            sum(value * weight for value, weight in weighted if value > 1e-12) / active_weight
            if active_weight else 0.0
        ),
        "activeTimeShare": active_weight / total_weight,
        "percentiles": {
            key: weighted_quantile(q)
            for key, q in (("p50", .50), ("p75", .75), ("p90", .90), ("p95", .95), ("p99", .99))
        },
        "timeAbove": {
            str(int(threshold * 100)): sum(
                weight for value, weight in weighted if value >= threshold
            ) / total_weight
            for threshold in (.70, .80, .90, .95)
        },
    }


def liquidation_loss_metrics(positions, *, fallback_equity):
    """Return the worst liquidated Copy episode loss relative to equity when that episode opened."""
    rolling_equity = max(1.0, f(fallback_equity))
    worst = {
        "max_liquidation_loss_pct": 0.0,
        "max_liquidation_loss": 0.0,
        "max_liquidation_loss_coin": None,
        "max_liquidation_loss_closed_at": None,
    }
    for position in sorted(
        (dict(row) for row in (positions or ())),
        key=lambda row: int(row.get("closed_at") or 0),
    ):
        pnl = f(position.get("net_pnl"))
        if position.get("status") == "liquidated":
            loss = f(position.get("liquidation_loss"))
            if loss <= 0.0:
                loss = max(0.0, -pnl)
            denominator = f(position.get("risk_equity_at_open")) or rolling_equity
            loss_pct = loss / max(1.0, denominator)
            if loss_pct > worst["max_liquidation_loss_pct"]:
                worst = {
                    "max_liquidation_loss_pct": loss_pct,
                    "max_liquidation_loss": loss,
                    "max_liquidation_loss_coin": position.get("coin"),
                    "max_liquidation_loss_closed_at": int(
                        position.get("closed_at") or 0
                    ) or None,
                }
        rolling_equity = max(1.0, rolling_equity + pnl)
    return worst


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
    full_endpoint_net = f(out.get("copy_net_pnl"))
    all_positions = [dict(position) for position in (out.get("positions") or [])]
    positions = [
        dict(position)
        for position in all_positions
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
    all_open_events = list(out.get("open_events") or ())
    window_open_events = [
        dict(event) for event in all_open_events
        if int(event.get("time") or 0) >= int(start_ms)
    ]
    if all_open_events:
        open_metrics = open_execution_metrics(window_open_events)
        opened_n = open_metrics["opened_n"]
        target_open_events = open_metrics["raw_target_open_events"]
        open_rate = open_metrics["effective_open_follow_rate"]
        capacity_fit = open_metrics["capacity_open_fit"]
        sliced_skip_reasons = dict(out.get("skip_reasons") or {})
        logged_outcomes = {
            str(event.get("outcome") or "")
            for event in all_open_events
            if event.get("outcome") != "opened"
        }
        window_outcomes = Counter(
            str(event.get("outcome") or "")
            for event in window_open_events
            if event.get("outcome") != "opened"
        )
        for outcome in logged_outcomes:
            sliced_skip_reasons[outcome] = int(window_outcomes.get(outcome, 0))
    else:
        opened_n = int(out.get("opened_n") or 0)
        target_open_events = int(out.get("target_open_events") or 0)
        open_rate = (
            f(out.get("actionable_open_rate"))
            if out.get("actionable_open_rate") is not None else 1.0
        )
        capacity_fit = (
            f(out.get("capacity_open_fit"))
            if out.get("capacity_open_fit") is not None else open_rate
        )
        sliced_skip_reasons = dict(out.get("skip_reasons") or {})
        open_metrics = {
            "raw_target_open_events": int(
                out.get("raw_target_open_events") or target_open_events
            ),
            "small_open_excluded_n": int(out.get("small_open_excluded_n") or 0),
            "effective_target_open_events": int(
                out.get("effective_target_open_events") or target_open_events
            ),
            "raw_open_capture_rate": f(
                out.get("raw_open_capture_rate")
                if out.get("raw_open_capture_rate") is not None else open_rate
            ),
            "effective_open_follow_rate": open_rate,
            "capacity_open_fit": capacity_fit,
            "open_execution_audit": dict(out.get("open_execution_audit") or {}),
        }
    all_add_events = list(out.get("add_events") or ())
    window_add_events = [
        dict(event) for event in all_add_events
        if int(event.get("time") or 0) >= int(start_ms)
    ]
    window_add_counts = (
        Counter(str(event.get("outcome") or "") for event in window_add_events)
        if all_add_events else out.get("add_outcome_counts")
    )
    add_metrics = add_fidelity_metrics(
        positions + open_positions,
        window_add_counts,
    )
    behavior_v2 = _clamp01(
        open_rate
        * (f(add_metrics.get("effective_add_fidelity")) if add_metrics.get("effective_add_fidelity") is not None else 1.0)
        * path_completion_rate
    )
    legacy_capture = 1.0 - (
        f(out.get("missed_add_rate")) if out.get("missed_add_rate") is not None else 0.0
    )

    initial_equity = (
        f(out.get("initial_margin_equity")) or float(config.INITIAL_BALANCE)
    )
    realized_before_start = sum(
        f(position.get("net_pnl"))
        for position in all_positions
        if int(position.get("closed_at") or 0) < int(start_ms)
    )
    equity = max(1.0, initial_equity + realized_before_start)
    window_start_equity = equity
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
        for position in positions + open_positions:
            pnl = _endpoint_pnl(position)
            bucket = key(position)
            buckets[bucket] = buckets.get(bucket, 0.0) + pnl
            total_abs += abs(pnl)
        return max((abs(value) for value in buckets.values()), default=0.0) / total_abs if total_abs else 0.0

    ambiguous_ranges = [
        row for row in (out.get("ambiguous_path_ranges") or [])
        if int(row.get("close_time") or 0) >= int(start_ms)
    ]
    all_path_samples = list(out.get("path_equity_samples") or ())
    prior_sample = max(
        (row for row in all_path_samples if int(row.get("time") or 0) < int(start_ms)),
        key=lambda row: int(row.get("time") or 0),
        default=None,
    )
    path_samples = []
    if prior_sample:
        path_samples.append({"time": int(start_ms), "equity": f(prior_sample.get("equity"))})
    path_samples.extend(
        dict(row) for row in all_path_samples if int(row.get("time") or 0) >= int(start_ms)
    )
    if prior_sample and f(prior_sample.get("equity")) > 0.0:
        window_start_equity = f(prior_sample.get("equity"))
    terminal_equity = max(1.0, initial_equity + full_endpoint_net)
    # A close inside the window may belong to a position opened before the boundary. Summing its lifetime
    # PnL would assign pre-window profit/loss to the recent window. When the strict path provides a marked
    # boundary, the only correct rolling result is endpoint marked equity minus boundary marked equity.
    window_net_pnl = (
        terminal_equity - window_start_equity
        if prior_sample else closed_net + open_unrealized
    )
    path_risk = path_risk_metrics(
        path_samples,
        initial_equity=window_start_equity,
        liquidation_times=[
            value for value in (out.get("path_liquidation_times") or ())
            if int(f(value)) >= int(start_ms)
        ],
    )
    liquidation_risk = liquidation_loss_metrics(
        positions, fallback_equity=window_start_equity,
    )
    if str(out.get("path_risk_status") or "") != "complete":
        path_risk["path_risk_status"] = str(out.get("path_risk_status") or "missing")
    def sliced_deploy_samples(rows):
        rows = list(rows or ())
        prior = max(
            (row for row in rows if int(row.get("time") or 0) < int(start_ms)),
            key=lambda row: int(row.get("time") or 0), default=None,
        )
        sliced = []
        if prior:
            sliced.append({"time": int(start_ms), "pct": f(prior.get("pct"))})
        sliced.extend(
            dict(row) for row in rows if int(row.get("time") or 0) >= int(start_ms)
        )
        return sliced

    deploy_samples = sliced_deploy_samples(out.get("deploy_samples"))
    deploy_values = [f(row.get("pct")) for row in deploy_samples]
    tier_deploy_samples = {
        tier: sliced_deploy_samples(rows)
        for tier, rows in dict(out.get("tier_deploy_samples") or {}).items()
    }
    out.update({
        "closed_n": len(positions),
        "wins": wins,
        "liquidations": liquidations,
        "tail_profit_closes": tail_profit_closes,
        "natural_closes": natural_closes,
        "path_completion_rate": path_completion_rate,
        "liquidation_rate": liquidations / len(positions) if positions else 0.0,
        "behavior_replication_rate": behavior_v2,
        "behavior_replication_v2": behavior_v2,
        "behavior_replication_rate_legacy": _clamp01(
            open_rate * legacy_capture * path_completion_rate
        ),
        "target_open_events": target_open_events,
        "opened_n": opened_n,
        "open_events": window_open_events,
        "raw_target_open_events": open_metrics["raw_target_open_events"],
        "small_open_excluded_n": open_metrics["small_open_excluded_n"],
        "effective_target_open_events": open_metrics["effective_target_open_events"],
        "raw_open_capture_rate": open_metrics["raw_open_capture_rate"],
        "effective_open_follow_rate": open_metrics["effective_open_follow_rate"],
        "open_execution_audit": open_metrics["open_execution_audit"],
        "open_fill_rate": open_rate,
        "actionable_open_rate": open_rate,
        "execution_fill_rate": open_rate,
        "capacity_open_fit": capacity_fit,
        "execution_capacity_fit": open_metrics.get(
            "execution_capacity_fit", capacity_fit
        ),
        "cash_congestion_fit": open_metrics.get("cash_congestion_fit", 1.0),
        "open_constraint_counts": open_metrics.get(
            "open_constraint_counts", {}
        ),
        "open_constraint_fit": open_metrics.get("open_constraint_fit", {}),
        "skip_reasons": sliced_skip_reasons,
        "add_events": window_add_events,
        "ambiguous_liquidations": len(ambiguous_ranges),
        "ambiguous_path_ranges": ambiguous_ranges,
        "path_equity_samples": path_samples,
        "copy_win_rate": wins / len(positions) if positions else 0.0,
        "copy_net_pnl": window_net_pnl,
        "window_start_equity": window_start_equity,
        "window_end_equity": terminal_equity if prior_sample else max(
            1.0, window_start_equity + window_net_pnl,
        ),
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
        "peak_deploy_pct": max(deploy_values, default=0.0),
        "avg_deploy_pct": (
            sum(deploy_values) / len(deploy_values) if deploy_values else 0.0
        ),
        "deployment_distribution": deployment_distribution(
            deploy_samples, end_ms=out.get("valuation_asof_ms"),
        ),
        "deploy_samples": deploy_samples,
        "tier_deploy_samples": tier_deploy_samples,
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
    out.update(liquidation_risk)
    out.update(add_metrics)
    out.update(profit_structure_metrics(positions, total_net=closed_net))
    out.update(path_risk)
    out["tier_economics"] = tier_economics(
        positions, open_positions,
        open_events=window_open_events,
        add_events=window_add_events,
        tier_deploy_samples=tier_deploy_samples,
    )
    return out
