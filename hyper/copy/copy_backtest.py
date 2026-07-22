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
from .copy_engine import (OpenSizingParams, extract_master_leverage, isolated_liq_px,
                          plan_open_sizing, profit_tail_close_decision, reduce_leaves_dust,
                          smart_add_order_margin, smart_take_profit_decision, tier_for_sigma,
                          margin_cap_room, total_effective_margin, wallet_margin,
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
    "liquidity_blocked",
    "wallet_sector_side_cap_blocked",
    "wallet_cap_blocked",
    "total_margin_cap_blocked",
    "liquidation_cooldown_blocked",
    "forward_loss_blocked",
)
ADD_BLOCKED_OUTCOMES = tuple(
    outcome for outcome in ADD_OUTCOMES
    if outcome not in {"followed", "noise_merged"}
)
ADD_METRICS_VERSION = "add_metrics_v2"


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


def profit_structure_metrics(positions: list[dict], *, total_net: float, fee_drag: float) -> dict:
    """Return fee-paid distribution diagnostics for one replay endpoint.

    Open positions participate at their canonical marked endpoint.  This keeps qualification aligned with
    the same realized+unrealized dollars used by the public 30/14/7 return lines instead of letting a large
    open winner/loss disappear from concentration and tail tests.
    """
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
        # An exact 1.5x replay is still run for portfolio finalists.  At the individual-evidence layer this
        # same-path value is the deterministic extra half-fee stress and avoids doubling profile CPU work.
        "cost_stress_net_pnl": float(total_net) - 0.5 * max(0.0, float(fee_drag)),
    }


def campaign_structure_metrics(positions: list[dict]) -> dict:
    """Collapse overlapping same-wallet/board/direction positions into independent trade campaigns.

    A basket trader may open ten correlated stock shorts at once. Counting those symbols as ten independent
    wins makes both win rate and Wilson confidence fictitious; economically they are one directional bet.
    """
    grouped = {}
    for position in positions or ():
        opened = int(f(position.get("opened_at")) or 0)
        closed = int(f(position.get("closed_at")) or 0)
        is_open = str(position.get("status") or "open") == "open" or closed <= 0
        end = float("inf") if is_open else closed
        key = (
            str(position.get("addr") or "").lower(),
            "stock" if str(position.get("coin") or "").lower().startswith("xyz:") else "crypto",
            str(position.get("side") or "").lower(),
        )
        grouped.setdefault(key, []).append((opened, end, position, is_open))

    campaigns = []
    for key, rows in grouped.items():
        rows.sort(key=lambda row: (row[0], row[1]))
        current = None
        for opened, end, position, is_open in rows:
            pnl = _endpoint_pnl(position)
            if current is None or opened > current["end"]:
                if current is not None:
                    campaigns.append(current)
                current = {
                    "key": key, "opened_at": opened, "end": end, "closed": not is_open,
                    "pnl": pnl, "position_n": 1, "positions": [position],
                }
            else:
                current["end"] = max(current["end"], end)
                current["closed"] = bool(current["closed"] and not is_open)
                current["pnl"] += pnl
                current["position_n"] += 1
                current["positions"].append(position)
        if current is not None:
            campaigns.append(current)

    closed_campaigns = [campaign for campaign in campaigns if campaign["closed"]]
    pnls = [float(campaign["pnl"]) for campaign in closed_campaigns]
    wins = sorted((value for value in pnls if value > 0.0), reverse=True)
    losses = [-value for value in pnls if value < 0.0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    peak_positions = 0
    peak_margin = 0.0
    for campaign in campaigns:
        events = []
        for position in campaign.get("positions") or ():
            opened = int(f(position.get("opened_at")))
            closed = int(f(position.get("closed_at")))
            margin = max(0.0, f(position.get("margin")))
            events.append((opened, 1, margin))
            if closed:
                events.append((closed, -1, -margin))
        current_n = 0
        current_margin = 0.0
        # Process opens before closes at an identical timestamp so a flip/rebalance cannot under-report
        # the momentary basket capacity required by a strict follower.
        for _stamp, count_delta, margin_delta in sorted(events, key=lambda row: (row[0], -row[1])):
            current_n += count_delta
            current_margin += margin_delta
            peak_positions = max(peak_positions, current_n)
            peak_margin = max(peak_margin, current_margin)
    open_campaigns = [campaign for campaign in campaigns if not campaign["closed"]]
    worst_open = min((float(campaign["pnl"]) for campaign in open_campaigns), default=0.0)
    oldest_losing_open = min(
        (int(campaign["opened_at"]) for campaign in open_campaigns if float(campaign["pnl"]) < 0.0),
        default=0,
    )
    return {
        "campaign_closed_n": len(closed_campaigns),
        "campaign_open_n": len(campaigns) - len(closed_campaigns),
        "campaign_wins": len(wins),
        "campaign_win_rate": len(wins) / len(closed_campaigns) if closed_campaigns else 0.0,
        "campaign_net_pnl": sum(pnls),
        "campaign_gross_profit": gross_profit,
        "campaign_gross_loss": gross_loss,
        "campaign_profit_factor": (
            gross_profit / gross_loss if gross_loss > 0.0 else (999.0 if gross_profit > 0.0 else 0.0)
        ),
        "campaign_top1_profit_share": wins[0] / gross_profit if gross_profit > 0.0 else 0.0,
        "campaign_top2_profit_share": sum(wins[:2]) / gross_profit if gross_profit > 0.0 else 0.0,
        "campaign_net_after_top1": sum(pnls) - sum(wins[:1]),
        "campaign_net_after_top2": sum(pnls) - sum(wins[:2]),
        "campaign_max_positions": max((campaign["position_n"] for campaign in campaigns), default=0),
        "campaign_peak_positions": peak_positions,
        "campaign_peak_margin": peak_margin,
        "campaign_worst_pnl": min((float(campaign["pnl"]) for campaign in campaigns), default=0.0),
        "campaign_worst_open_pnl": worst_open,
        "campaign_oldest_losing_open_ms": oldest_losing_open,
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
        self.add_outcome_counts = Counter()
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
        self.wallet_sector_side_cap_pct = overrides.get(
            "WALLET_SECTOR_SIDE_CAP_PCT", config.WALLET_SECTOR_SIDE_CAP_PCT,
        )
        dynamic_side_keys = {
            "WALLET_CRYPTO_STABLE_SIDE_CAP_PCT", "WALLET_CRYPTO_MID_SIDE_CAP_PCT",
            "WALLET_CRYPTO_HIGH_SIDE_CAP_PCT", "WALLET_STOCK_SIDE_CAP_PCT",
        }
        legacy_side_default = (
            self.wallet_sector_side_cap_pct if not dynamic_side_keys.intersection(overrides)
            else None
        )
        self.wallet_sector_side_caps = {
            "stable": overrides.get(
                "WALLET_CRYPTO_STABLE_SIDE_CAP_PCT",
                config.WALLET_CRYPTO_STABLE_SIDE_CAP_PCT if legacy_side_default is None else legacy_side_default,
            ),
            "mid": overrides.get(
                "WALLET_CRYPTO_MID_SIDE_CAP_PCT",
                config.WALLET_CRYPTO_MID_SIDE_CAP_PCT if legacy_side_default is None else legacy_side_default,
            ),
            "high": overrides.get(
                "WALLET_CRYPTO_HIGH_SIDE_CAP_PCT",
                config.WALLET_CRYPTO_HIGH_SIDE_CAP_PCT if legacy_side_default is None else legacy_side_default,
            ),
            "stock": overrides.get(
                "WALLET_STOCK_SIDE_CAP_PCT",
                config.WALLET_STOCK_SIDE_CAP_PCT if legacy_side_default is None else legacy_side_default,
            ),
        }
        self.wallet_margin_cap_pct = overrides.get("WALLET_MARGIN_CAP_PCT", config.WALLET_MARGIN_CAP_PCT)
        self.wallet_max_open_positions = int(overrides.get(
            "WALLET_MAX_OPEN_POSITIONS", config.WALLET_MAX_OPEN_POSITIONS,
        ))
        self.wallet_stock_side_max_positions = int(overrides.get(
            "WALLET_STOCK_SIDE_MAX_POSITIONS", config.WALLET_STOCK_SIDE_MAX_POSITIONS,
        ))
        self.max_total_margin_pct = overrides.get("MAX_TOTAL_MARGIN_PCT", config.MAX_TOTAL_MARGIN_PCT)
        self.liquidation_reentry_cooldown_ms = int(
            overrides.get("LIQUIDATION_REENTRY_COOLDOWN_HOURS", config.LIQUIDATION_REENTRY_COOLDOWN_HOURS)
            * 60 * 60 * 1000
        )
        self.repeat_liquidation_freeze_ms = int(
            overrides.get("REPEAT_LIQUIDATION_FREEZE_DAYS", config.REPEAT_LIQUIDATION_FREEZE_DAYS)
            * 24 * 60 * 60 * 1000
        )
        self.liquidation_times = {}
        self.liquidation_cooldown_until = {}
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
        self.path_equity_samples = []
        self.path_liquidation_times = []
        self.campaign_closed_net = Counter()
        self.campaign_risk_high_water = {}
        self.campaign_intratrade_max_drawdown = 0.0
        self.track_price_path = False

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
        if not self.track_price_path:
            return
        stamp = int(f(stamp))
        if stamp <= 0:
            return
        self.path_equity_samples.append({"time": stamp, "equity": self.marked_equity()})
        self._sample_campaign_risk()

    @staticmethod
    def _campaign_key(position):
        return (
            str(position.get("addr") or "").lower(),
            "stock" if str(position.get("coin") or "").lower().startswith("xyz:") else "crypto",
            str(position.get("side") or "").lower(),
        )

    def _sample_campaign_risk(self):
        """Mark every currently overlapping source/sector/direction basket on the same price path."""
        grouped = {}
        for position in self.open.values():
            key = self._campaign_key(position)
            mark = self.last_px.get(position.get("coin")) or position.get("entry_px")
            pnl = f(position.get("realized_net")) + f(position.get("rem_size")) * (
                f(mark) - f(position.get("entry_px"))
            ) * f(position.get("sign"))
            grouped[key] = grouped.get(key, 0.0) + pnl
        for key, open_pnl in grouped.items():
            campaign_pnl = f(self.campaign_closed_net.get(key)) + open_pnl
            campaign_equity = self.initial_balance + campaign_pnl
            peak = max(
                self.initial_balance,
                f(self.campaign_risk_high_water.get(key) or self.initial_balance),
                campaign_equity,
            )
            self.campaign_risk_high_water[key] = peak
            self.campaign_intratrade_max_drawdown = max(
                self.campaign_intratrade_max_drawdown,
                max(0.0, peak - campaign_equity) / self.initial_balance,
            )

    def risk_available(self):
        return max(0.0, self.available() + min(0.0, self.unrealized()))

    def _liquidation_freeze_active(self, addr, coin, t):
        stamp = int(t or 0)
        return max(
            int(self.liquidation_cooldown_until.get((str(addr or "").lower(), "*"), 0)),
            int(self.liquidation_cooldown_until.get((str(addr or "").lower(), str(coin or "").lower()), 0)),
        ) > stamp

    def _record_liquidation_freeze(self, addr, coin, t):
        stamp = int(t or 0)
        wallet = str(addr or "").lower()
        cutoff = stamp - 30 * 24 * 60 * 60 * 1000
        times = [value for value in self.liquidation_times.get(wallet, []) if value >= cutoff]
        times.append(stamp)
        self.liquidation_times[wallet] = times
        duration = self.repeat_liquidation_freeze_ms if len(times) >= 2 else self.liquidation_reentry_cooldown_ms
        until = stamp + duration
        self.liquidation_cooldown_until[(wallet, str(coin or "").lower())] = until
        self.liquidation_cooldown_until[(wallet, "*")] = max(
            until, int(self.liquidation_cooldown_until.get((wallet, "*"), 0)),
        )

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
                self._open_position(addr, coin, x.get("time"), px, pos1, oid, x)
            elif abs(pos1) >= config.FLAT:
                self.skip_reasons["skip_midway"] += 1
            return

        ep["master_current"] = abs(pos1)

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
        boundary_positions = [
            ep for (_addr, current_coin), ep in self.open.items()
            if current_coin == coin
            and x.get("open_time") is not None
            and int(ep.get("opened_at") or 0) > int(x.get("open_time") or 0)
        ]
        ambiguous_candle = bool(x.get("has_fill_events")) or bool(boundary_positions)
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
            ambiguous=bool(x.get("has_fill_events")), candle_close_time=x.get("close_time"),
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
        if coin_is_blocked(coin, self.coin_blacklist, block_korean_stocks=self.block_korean_stocks):
            self.skip_reasons["skip_coin_blacklist"] += 1
            return
        if self.liquidity_block_reason(coin):
            self.skip_reasons["skip_low_liquidity"] += 1
            return
        sigma = self.sigma(coin)
        side = "long" if pos1 > 0 else "short"
        sign = 1 if side == "long" else -1
        wallet_key = str(addr or "").lower()
        if self._liquidation_freeze_active(addr, coin, t):
            self.skip_reasons["skip_liquidation_cooldown"] += 1
            return
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
            wallet_sector_side_room=group_room,
            wallet_room=source_room,
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
            "master_current": abs(pos1),
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
            "last_target_add_px": px,
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

    def _record_add_outcome(self, ep, oid, outcome):
        """Assign one final outcome to a distinct target add order.

        A same-oid order can first look like noise and become actionable after later fill slices move its
        aggregate VWAP.  Reclassification decrements the old bucket before incrementing the new one, so an
        order is never simultaneously counted as both ignored and followed.
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
        return outcome == "followed"

    def _observe_add(self, ep, oid=None, reason="noise_merged"):
        self._record_add_outcome(ep, oid, reason)
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

        if self._liquidation_freeze_active(addr, coin, t):
            self.skip_reasons["skip_liquidation_cooldown_add"] += 1
            return self._observe_add(ep, oid, "liquidation_cooldown_blocked")

        # Once our first proactive profit cut has executed, the released exposure stays released.  Target
        # re-adds are observed for source state but never rebuild the protected position.
        if self.smart_tp_enable and int(ep.get("smart_tp_stage") or 0) > 0:
            self.skip_reasons["skip_smart_tp_readd"] += 1
            return self._observe_add(ep, oid, "noise_merged")

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
        total_room = margin_cap_room(
            cap_pct=self.max_total_margin_pct,
            risk_equity=risk_equity,
            existing_margin=total_effective_margin(self.open.values()),
        )
        existing = sum(
            p["margin"] * (p["rem_size"] / p["size"] if p["size"] else 1.0)
            for (addr, c), p in self.open.items()
            if c == coin and p["side"] == ep["side"]
        )
        coin_room = max(0.0, self.coin_cap_pct(tier) * risk_equity - existing)
        if self.liquidity_block_reason(coin):
            return self._observe_add(ep, oid, "liquidity_blocked")
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
                    return self._observe_add(ep, oid, "noise_merged")
                if ep["add_count"] >= self.add_max_hard:
                    return self._observe_add(ep, oid, "hard_cap_blocked")
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
                elif total_room + eps < desired_remaining and total_room <= min(coin_room, risk_available) + eps:
                    reason = "total_margin_cap_blocked"
                elif coin_room + eps < desired_remaining and coin_room <= risk_available + eps:
                    reason = "coin_cap_blocked"
                elif risk_available + eps < desired_remaining and risk_available < coin_room - eps:
                    reason = "cash_blocked"
                else:
                    reason = "min_margin_blocked"
                return self._observe_add(ep, oid, reason)
        else:
            max_adds = self.tier_max_adds[tier]
            if ep["add_count"] >= max_adds:
                return self._observe_add(ep, oid, "hard_cap_blocked")
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
                elif total_room <= min(coin_room, risk_available):
                    reason = "total_margin_cap_blocked"
                else:
                    reason = "coin_cap_blocked" if coin_room <= risk_available else "cash_blocked"
                return self._observe_add(ep, oid, reason)

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
            self._record_add_outcome(ep, oid, "followed")
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
            self.closed.append(ep)
            self.open.pop(key, None)
            campaign_key = self._campaign_key(ep)
            self.campaign_closed_net[campaign_key] += f(ep.get("realized_net"))
            self._sample_campaign_risk()
            if not any(self._campaign_key(position) == campaign_key for position in self.open.values()):
                final_campaign_pnl = f(self.campaign_closed_net.get(campaign_key))
                campaign_equity = self.initial_balance + final_campaign_pnl
                peak = max(
                    self.initial_balance,
                    f(self.campaign_risk_high_water.get(campaign_key) or self.initial_balance),
                    campaign_equity,
                )
                self.campaign_intratrade_max_drawdown = max(
                    self.campaign_intratrade_max_drawdown,
                    max(0.0, peak - campaign_equity) / self.initial_balance,
                )
                self.campaign_closed_net.pop(campaign_key, None)
                self.campaign_risk_high_water.pop(campaign_key, None)
            if status == "liquidated":
                self._record_liquidation_freeze(addr, coin, t)
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
        closed_net = sum(p["realized_net"] for p in self.closed)
        wins = sum(1 for p in self.closed if p["realized_net"] > 0)
        liquidations = sum(1 for p in self.closed if p.get("status") == "liquidated")
        tail_profit_closes = sum(1 for p in self.closed if p.get("status") == "tail_closed")
        natural_closes = max(0, len(self.closed) - liquidations)
        path_completion_rate = natural_closes / len(self.closed) if self.closed else 1.0
        initial_notl = sum(p["target_initial_notl"] for p in self.closed) + sum(p["target_initial_notl"] for p in self.open.values())
        add_notl = sum(p["target_add_notl"] for p in self.closed) + sum(p["target_add_notl"] for p in self.open.values())
        capacity_skips = sum(self.skip_reasons[k] for k in (
            "skip_coin_full", "skip_no_cash", "skip_deploy_cap", "skip_margin_too_small",
            "skip_wallet_sector_side_full", "skip_wallet_full", "skip_wallet_position_cap",
            "skip_wallet_stock_side_position_cap",
        ))
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
        add_metrics = add_fidelity_metrics(all_positions, self.add_outcome_counts)
        profit_metrics = profit_structure_metrics(
            all_positions,
            total_net=equity_pnl,
            fee_drag=self.fee_drag,
        )
        campaign_metrics = campaign_structure_metrics(all_positions)
        path_metrics = path_risk_metrics(
            self.path_equity_samples,
            initial_equity=self.initial_balance,
            liquidation_times=self.path_liquidation_times,
        )
        open_rate = self.opened_n / self.target_open_events if self.target_open_events else 1.0
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
        campaign_open_loss = min(0.0, f(campaign_metrics.get("campaign_worst_open_pnl"))) / self.initial_balance
        current_open_loss = min(aggregate_open_loss, campaign_open_loss)
        oldest_losing_open = int(campaign_metrics.get("campaign_oldest_losing_open_ms") or 0)
        current_bag_hours = (
            max(0, current_asof_ms - oldest_losing_open) / 3_600_000
            if current_open_loss < 0.0 and oldest_losing_open and current_asof_ms else 0.0
        )
        campaign_worst_endpoint = f(campaign_metrics.get("campaign_worst_pnl"))
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
            "max_total_margin_pct": self.max_total_margin_pct,
            "liquidation_reentry_blocks": self.skip_reasons["skip_liquidation_cooldown"],
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
            "target_open_events": self.target_open_events,
            "opened_n": self.opened_n,
            "open_fill_rate": self.opened_n / self.target_open_events if self.target_open_events else 1.0,
            "add_dependency": add_notl / initial_notl if initial_notl else 0.0,
            "target_peak_concurrent": self.target_peak_concurrent,
            "copy_peak_concurrent": self.copy_peak_concurrent,
            "max_concurrent_fit": self.copy_peak_concurrent / self.target_peak_concurrent if self.target_peak_concurrent else 1.0,
            "capacity_open_fit": self.opened_n / (self.opened_n + capacity_skips) if (self.opened_n + capacity_skips) else 1.0,
            "actionable_open_rate": self.opened_n / self.target_open_events if self.target_open_events else 1.0,
            "execution_fill_rate": self.opened_n / self.target_open_events if self.target_open_events else 1.0,
            "behavior_replication_rate": behavior_v2,
            "behavior_replication_v2": behavior_v2,
            "behavior_replication_rate_legacy": behavior_legacy,
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
            "path_equity_samples": self.path_equity_samples,
            "path_liquidation_times": self.path_liquidation_times,
            "current_open_loss_frac": current_open_loss,
            "current_bag_hours": current_bag_hours,
            "campaign_max_drawdown": max(
                self.campaign_intratrade_max_drawdown,
                abs(min(0.0, campaign_worst_endpoint)) / self.initial_balance,
            ),
            "campaign_peak_margin_pct": (
                f(campaign_metrics.get("campaign_peak_margin")) / self.initial_balance
            ),
            "master_leverage_known": self.master_leverage_known,
            "master_leverage_missing": self.master_leverage_missing,
            "master_leverage_coverage": leverage_coverage,
            "maintenance_margin_coverage": maintenance_coverage,
            "maintenance_margin_known": self.maintenance_margin_known,
            "maintenance_margin_missing": self.maintenance_margin_missing,
            "model_coverage": min(leverage_coverage, maintenance_coverage, price_path_coverage),
            "fallback_reasons": fallback_reasons,
            "skip_reasons": dict(self.skip_reasons),
            "positions": closed_positions,
            "open_positions": open_positions,
        }
        result.update(add_metrics)
        result.update(profit_metrics)
        result.update(campaign_metrics)
        result.update(path_metrics)
        return result


def summarize_position(p, *, mark_px=None, unrealized_pnl=None, valuation_complete=None, sigma=None):
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
        "add_outcome_counts": {
            key: int((p.get("add_outcome_counts") or {}).get(key) or 0)
            for key in ADD_OUTCOMES
        },
        "entry_px": p["entry_px"],
        "master_avg_px": p["master_open_px"],
        "master_leverage": p.get("master_leverage"),
        "leverage": p["leverage"],
        "margin": p["margin"],
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
    add_metrics = add_fidelity_metrics(
        positions + open_positions,
        out.get("add_outcome_counts"),
    )
    behavior_v2 = _clamp01(
        open_rate
        * (f(add_metrics.get("effective_add_fidelity")) if add_metrics.get("effective_add_fidelity") is not None else 1.0)
        * path_completion_rate
    )
    legacy_capture = 1.0 - (
        f(out.get("missed_add_rate")) if out.get("missed_add_rate") is not None else 0.0
    )

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
    path_risk = path_risk_metrics(
        path_samples,
        initial_equity=f(out.get("initial_margin_equity")) or config.INITIAL_BALANCE,
        liquidation_times=[
            value for value in (out.get("path_liquidation_times") or ())
            if int(f(value)) >= int(start_ms)
        ],
    )
    if str(out.get("path_risk_status") or "") != "complete":
        path_risk["path_risk_status"] = str(out.get("path_risk_status") or "missing")
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
        "ambiguous_liquidations": len(ambiguous_ranges),
        "ambiguous_path_ranges": ambiguous_ranges,
        "path_equity_samples": path_samples,
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
    out.update(add_metrics)
    out.update(profit_structure_metrics(
        positions + open_positions,
        total_net=closed_net + open_unrealized,
        fee_drag=fees,
    ))
    out.update(campaign_structure_metrics(positions + open_positions))
    out.update(path_risk)
    return out
