"""Generation-frozen pre-strict evidence and qualification policy.

This module is the sole production definition of recurring activity, conditional lottery protection and
the rough/strict economic gates.  Research jobs may consume it, but production never consumes research
tables or reports.
"""
from __future__ import annotations

import math
from typing import Mapping, Optional

from hyper import config
from hyper.copy.economics import conservative_profitability, open_loss_ratio_within_limit


DAY_MS = 86_400_000
POLICY_VERSION = "pre-strict32-pf125-activity-v1"
SELECTION_MODEL_VERSION = "selection-pre-strict32-pf125-profit-score-prefix-v5"


def _num(value, default=0.0):
    try:
        number = float(value)
        return default if math.isnan(number) or math.isinf(number) else number
    except (TypeError, ValueError):
        return default


def copy_activity(results: Mapping, as_of_ms: int) -> dict:
    """Freeze recurring, economically actionable source opens from the canonical Copy state machine."""
    primary = dict((results or {}).get(30) or (results or {}).get("30") or {})
    events = []
    for raw in primary.get("open_events") or ():
        event = dict(raw or {})
        minimum = _num(event.get("minimum_notional"))
        master = _num(event.get("master_notional"))
        if minimum > 0.0 and master + 1e-9 < minimum:
            continue
        if str(event.get("outcome") or "") in {"skip_coin_blacklist", "skip_small_notl"}:
            continue
        stamp = int(_num(event.get("time")))
        if stamp > 0:
            events.append(stamp)
    events.sort()

    bucket_days = int(config.PRE_STRICT_ACTIVITY_BUCKET_DAYS)
    lookback_days = int(config.PRE_STRICT_ACTIVITY_LOOKBACK_DAYS)
    bucket_ms = bucket_days * DAY_MS
    start_ms = int(as_of_ms) - lookback_days * DAY_MS
    recent = [stamp for stamp in events if start_ms <= stamp <= int(as_of_ms)]
    buckets = [0] * max(1, lookback_days // bucket_days)
    for stamp in recent:
        index = min(len(buckets) - 1, max(0, int((stamp - start_ms) // bucket_ms)))
        buckets[index] += 1
    active_weeks = sum(count > 0 for count in buckets)
    latest_active = bool(buckets and buckets[-1] > 0)
    points = [start_ms, *recent, int(as_of_ms)]
    max_gap_days = max(
        ((right - left) / DAY_MS for left, right in zip(points, points[1:])),
        default=float(lookback_days),
    )
    gaps = [(right - left) / DAY_MS for left, right in zip(recent, recent[1:])]
    median_gap = None
    if gaps:
        ordered = sorted(gaps)
        mid = len(ordered) // 2
        median_gap = (
            ordered[mid] if len(ordered) % 2
            else (ordered[mid - 1] + ordered[mid]) / 2.0
        )
    operational = bool(
        latest_active
        and active_weeks >= int(config.PRE_STRICT_ACTIVITY_MIN_ACTIVE_WEEKS)
        and max_gap_days <= float(config.PRE_STRICT_ACTIVITY_MAX_OPEN_GAP_DAYS)
    )
    if not recent:
        reason = "no_actionable_open_28d"
    elif not latest_active:
        reason = "no_actionable_open_7d"
    elif active_weeks < int(config.PRE_STRICT_ACTIVITY_MIN_ACTIVE_WEEKS):
        reason = "active_weeks_below_3_of_4"
    elif max_gap_days > float(config.PRE_STRICT_ACTIVITY_MAX_OPEN_GAP_DAYS):
        reason = "actionable_open_gap_over_10d"
    else:
        reason = "operational_activity"
    return {
        "definition": "oid_deduped_copy_threshold_flat_to_open_or_flip",
        "asOfMs": int(as_of_ms),
        "actionableOpenEvents30d": len(events),
        "actionableOpenEvents28d": len(recent),
        "actionableOpenEvents14d": sum(
            stamp >= int(as_of_ms) - 14 * DAY_MS for stamp in events
        ),
        "actionableOpenEvents7d": sum(
            stamp >= int(as_of_ms) - 7 * DAY_MS for stamp in events
        ),
        "activeOpenDays28d": len({stamp // DAY_MS for stamp in recent}),
        "weeklyOpenCountsOldestFirst": buckets,
        "activeWeeks4": active_weeks,
        "latest7dActive": latest_active,
        "maxOpenGapDays28d": max_gap_days,
        "medianOpenGapDays28d": median_gap,
        "operational": operational,
        "reason": reason,
    }


def conditional_lottery(
    *,
    win_rate,
    top3_profit_share,
    body_net_pnl,
    body_win_rate,
) -> dict:
    """Reject lucky outliers without imposing a blanket minimum win rate."""
    win = _num(win_rate)
    top3 = _num(top3_profit_share)
    body_net = _num(body_net_pnl)
    body_win = _num(body_win_rate)
    low_win_body_losing = win < 0.50 and body_net < 0.0
    concentrated_weak_body = top3 >= 0.70 and (body_net < 0.0 or body_win < 0.50)
    reason = (
        "low_win_rate_body_losing" if low_win_body_losing
        else "top3_concentrated_weak_body" if concentrated_weak_body
        else "distributed_or_profitable_body"
    )
    return {
        "passed": not (low_win_body_losing or concentrated_weak_body),
        "reason": reason,
        "winRate": win,
        "top3ProfitShare": top3,
        "bodyNetPnl": body_net,
        "bodyWinRate": body_win,
    }


def _copy_economics(metrics: Mapping, days: int) -> dict:
    prefix = "copy_bt" if int(days) == 30 else f"copy_bt_{int(days)}d"
    marked = metrics.get(f"{prefix}_net_pnl")
    unrealized = _num(metrics.get(f"{prefix}_unrealized_pnl"))
    closed = metrics.get(f"{prefix}_closed_net_pnl")
    if closed is None and marked is not None:
        closed = _num(marked) - unrealized
    equity_key = (
        "copy_bt_window_start_equity"
        if int(days) == 30 else f"copy_bt_{int(days)}d_window_start_equity"
    )
    equity = _num(
        metrics.get(equity_key),
        _num(metrics.get("copy_bt_initial_margin_equity"), config.INITIAL_BALANCE),
    )
    return conservative_profitability(closed, unrealized, start_equity=equity)


def _source_economics(metrics: Mapping, days: int) -> dict:
    return conservative_profitability(
        metrics.get(f"source_net_pnl_{int(days)}d"),
        metrics.get("open_unrealized"),
    )


def evaluate(
    metrics: Mapping,
    activity: Mapping,
    *,
    stage: str = "rough",
    require_path: Optional[bool] = None,
) -> dict:
    """Evaluate the frozen pre-strict or final strict contract with one ordered failure."""
    strict = str(stage).lower() in {"strict", "final"}
    if require_path is None:
        require_path = strict
    source30 = _source_economics(metrics, 30)
    source7 = _source_economics(metrics, 7)
    copy30 = _copy_economics(metrics, 30)
    copy7 = _copy_economics(metrics, 7)
    source_lottery = conditional_lottery(
        win_rate=metrics.get("source_win_rate_30d"),
        top3_profit_share=metrics.get("source_top3_profit_share"),
        body_net_pnl=metrics.get("source_body_after_top3_net_pnl"),
        body_win_rate=metrics.get("source_body_after_top3_win_rate"),
    )
    copy_lottery = conditional_lottery(
        win_rate=metrics.get("copy_bt_win_rate"),
        top3_profit_share=metrics.get("copy_bt_top3_profit_share"),
        body_net_pnl=metrics.get("copy_bt_body_after_top3_net_pnl"),
        body_win_rate=metrics.get("copy_bt_body_after_top3_win_rate"),
    )
    data_status = str(metrics.get("copy_bt_data_status") or metrics.get("data_status") or "valid")
    evidence_status = str(metrics.get("copy_bt_evidence_status") or metrics.get("evidence_status") or "")
    valuation_status = str(metrics.get("copy_bt_valuation_status") or "complete")
    path_status = str(metrics.get("copy_path_risk_status") or "")
    open_rate = metrics.get("actionable_open_rate", metrics.get("copy_bt_open_fill_rate"))
    checks = {
        "dataComplete": data_status in {"", "valid", "ok"} and evidence_status != "invalid",
        "sourceClosedSample": int(_num(metrics.get("source_episode_n_30d"))) >= 7,
        "copyClosedSample": int(_num(metrics.get("copy_bt_closed_n"))) >= 7,
        "sourceClosedProfit30d": source30["closedPnl"] > 0.0,
        "sourceClosedProfit7d": source7["closedPnl"] > 0.0,
        "sourceOpenLossRatio": open_loss_ratio_within_limit(source30),
        "sourceConservativeProfit30d": source30["qualificationPnl"] > 0.0,
        "sourceConservativeProfit7d": source7["qualificationPnl"] > 0.0,
        "copyClosedProfit30d": copy30["closedPnl"] > 0.0,
        "copyClosedProfit7d": copy7["closedPnl"] > 0.0,
        "copyOpenLossRatio": open_loss_ratio_within_limit(copy30),
        "copyConservativeProfit30d": copy30["qualificationPnl"] > 0.0,
        "copyConservativeProfit7d": copy7["qualificationPnl"] > 0.0,
        "activityOperational": bool(activity.get("operational")),
        "openExecution": open_rate is not None and _num(open_rate) >= 0.70,
        "copyProfitFactor": _num(metrics.get("copy_bt_profit_factor")) >= 1.25,
        "sourceLottery": bool(source_lottery["passed"]),
        "copyLottery": bool(copy_lottery["passed"]),
        "valuationComplete": valuation_status == "complete",
        "pathComplete": (
            not require_path
            or path_status not in {"", "pending", "missing", "invalid", "replay_error", "incomplete"}
        ),
        "strictReturn30d": (
            not strict or _num(copy30.get("qualificationReturn")) >= 0.10
        ),
        "strictReturn7d": (
            not strict or _num(copy7.get("qualificationReturn")) >= 0.03
        ),
        "liquidationsWithinLimit": (
            not strict or int(_num(metrics.get("copy_bt_liquidations"))) <= 3
        ),
        "singleLiquidationLossWithinLimit": (
            _num(metrics.get("copy_bt_max_liquidation_loss_pct")) + 1e-12 < 0.05
        ),
    }
    failures = (
        ("copy_data_error", "dataComplete", True),
        ("copy_single_liquidation_loss_over_5pct", "singleLiquidationLossWithinLimit", False),
        ("source_episode_evidence_insufficient", "sourceClosedSample", False),
        ("copy_episode_evidence_insufficient", "copyClosedSample", False),
        ("source_30d_closed_pnl_not_positive", "sourceClosedProfit30d", False),
        ("source_7d_closed_pnl_not_positive", "sourceClosedProfit7d", False),
        ("source_open_loss_over_50pct", "sourceOpenLossRatio", False),
        ("source_30d_conservative_pnl_not_positive", "sourceConservativeProfit30d", False),
        ("source_7d_conservative_pnl_not_positive", "sourceConservativeProfit7d", False),
        ("copy_30d_closed_pnl_not_positive", "copyClosedProfit30d", False),
        ("copy_7d_closed_pnl_not_positive", "copyClosedProfit7d", False),
        ("copy_open_loss_over_50pct", "copyOpenLossRatio", False),
        ("rough_copy_30d_conservative_not_profitable", "copyConservativeProfit30d", False),
        ("rough_copy_7d_conservative_not_profitable", "copyConservativeProfit7d", False),
        (str(activity.get("reason") or "activity_not_operational"), "activityOperational", False),
        (f"{'strict' if strict else 'rough'}_copy_open_rate_below_floor", "openExecution", False),
        ("copy_profit_factor_below_1_25", "copyProfitFactor", False),
        ("source_lottery_profile_rejected", "sourceLottery", False),
        ("copy_lottery_profile_rejected", "copyLottery", False),
        ("copy_valuation_incomplete", "valuationComplete", True),
        ("copy_path_incomplete", "pathComplete", True),
        ("strict_copy_30d_conservative_return_below_floor", "strictReturn30d", False),
        ("strict_copy_7d_conservative_return_below_floor", "strictReturn7d", False),
        ("strict_copy_liquidations_over_3", "liquidationsWithinLimit", False),
    )
    first_failure = None
    deferred = False
    for reason, key, is_deferred in failures:
        if not checks[key]:
            first_failure, deferred = reason, is_deferred
            break
    return30 = _num(copy30.get("qualificationReturn"))
    return7 = _num(copy7.get("qualificationReturn"))
    priority = 0.70 * return30 + 0.30 * return7
    tier = (
        "primary"
        if first_failure is None
        and return30 >= config.PRE_STRICT_PRIMARY_RETURN_30D
        and return7 >= config.PRE_STRICT_PRIMARY_RETURN_7D
        else "reserve" if first_failure is None else None
    )
    return {
        "eligible": first_failure is None,
        "deferred": deferred,
        "status": "strict_qualified" if strict and first_failure is None else
            "pre_strict_qualified" if first_failure is None else first_failure,
        "firstFailure": first_failure,
        "checks": checks,
        "activity": dict(activity),
        "sourceLottery": source_lottery,
        "copyLottery": copy_lottery,
        "copyProfitFactor": _num(metrics.get("copy_bt_profit_factor")),
        "copyPayoffRatio": _num(metrics.get("copy_bt_payoff_ratio")),
        "sourceEconomics": {"30": source30, "7": source7},
        "copyEconomics": {"30": copy30, "7": copy7},
        "profitPriority": priority,
        "tier": tier,
        "policyVersion": POLICY_VERSION,
        "stage": "strict" if strict else "rough",
    }


def sort_key(evidence: Mapping, *, quality_score=0.0, addr="") -> tuple:
    tier = str(evidence.get("tier") or "")
    tier_order = 0 if tier == "primary" else 1 if tier == "reserve" else 2
    economics = evidence.get("copyEconomics") or {}
    return (
        tier_order,
        -_num(evidence.get("profitPriority"), float("-inf")),
        -_num((economics.get("30") or {}).get("qualificationReturn")),
        -_num((economics.get("7") or {}).get("qualificationReturn")),
        -_num(evidence.get("copyProfitFactor")),
        -_num(quality_score),
        str(addr or "").lower(),
    )
