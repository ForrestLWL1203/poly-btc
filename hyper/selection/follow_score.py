"""Source-quality, rough-Copy and final strict-Copy ranking.

Qualification answers four independent questions only: is the source wallet consistently good, is its
profit material, can our execution follow it, and is it currently active. The score never grants permission;
it orders wallets which already passed the applicable business contract.
"""
from __future__ import annotations

import math
import time
from typing import Mapping

from hyper import config
from hyper.copy.economics import (
    OPEN_LOSS_RATIO_LIMIT,
    PROFITABILITY_BASIS,
    conservative_profitability,
    open_loss_ratio_within_limit,
)
from hyper.copy.copy_policy import load_copy_policy
from hyper.copy.sector import apply_allowed_sector_copy_metrics, parse_json_obj


PROFIT_PRIORITY_30_WEIGHT = 0.70
PROFIT_PRIORITY_7_WEIGHT = 0.30
PROFIT_PRIORITY_MODE = "conservative_realized_profit_70_30"
ECONOMIC_REJECTION_REASONS = frozenset({
    "source_30d_closed_pnl_not_positive",
    "source_7d_closed_pnl_not_positive",
    "source_open_loss_over_50pct",
    "source_30d_conservative_pnl_not_positive",
    "source_7d_conservative_pnl_not_positive",
    "copy_30d_closed_pnl_not_positive",
    "copy_7d_closed_pnl_not_positive",
    "copy_open_loss_over_50pct",
    "rough_copy_30d_conservative_not_profitable",
    "rough_copy_7d_conservative_not_profitable",
    "strict_copy_30d_conservative_return_below_floor",
    "strict_copy_7d_conservative_return_below_floor",
})


def is_economic_rejection(reason) -> bool:
    return str(reason or "") in ECONOMIC_REJECTION_REASONS


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        return default if math.isnan(number) or math.isinf(number) else number
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _quality_above_floor(value: float, floor: float, span: float) -> float:
    """Give a qualified boundary 60%, then continue monotonically to a transparent cap."""
    return _clamp(0.60 + 0.40 * (float(value) - float(floor)) / max(1e-9, float(span)))


def _replay_window_equity(metrics: Mapping, days: int) -> float:
    key = "copy_bt_window_start_equity" if int(days) == 30 else f"copy_bt_{int(days)}d_window_start_equity"
    for candidate in (
        metrics.get(key),
        metrics.get("copy_bt_initial_margin_equity"),
        metrics.get("initial_margin_equity"),
        getattr(config, "INITIAL_BALANCE", 10_000.0),
    ):
        value = _num(candidate)
        if value > 0.0:
            return value
    return 1.0


def _copy_window_economics(metrics: Mapping, days: int) -> dict:
    prefix = "copy_bt" if int(days) == 30 else f"copy_bt_{int(days)}d"
    marked_key = f"{prefix}_net_pnl"
    unrealized_key = f"{prefix}_unrealized_pnl"
    closed_key = f"{prefix}_closed_net_pnl"
    marked = _num(metrics.get(marked_key))
    unrealized = _num(metrics.get(unrealized_key))
    exact_closed = metrics.get(closed_key) is not None
    closed = _num(metrics.get(closed_key)) if exact_closed else marked - unrealized
    return {
        **conservative_profitability(
            closed,
            unrealized,
            start_equity=_replay_window_equity(metrics, days),
        ),
        "available": metrics.get(marked_key) is not None,
        "exactClosedPnl": exact_closed,
        "markedPnl": marked,
    }


def _source_economics(metrics: Mapping) -> dict:
    unrealized = _num(metrics.get("open_unrealized"))
    return {
        str(days): conservative_profitability(
            metrics.get(f"source_net_pnl_{days}d"),
            unrealized,
        )
        for days in (30, 7)
    }


def compute_profit_priority(metrics: Mapping) -> tuple[float | None, dict]:
    """Return the immutable 70/30 conservative strict-Copy return priority."""
    scoped = apply_allowed_sector_copy_metrics(metrics)
    economic30 = _copy_window_economics(scoped, 30)
    economic7 = _copy_window_economics(scoped, 7)
    available = economic30["available"] and economic7["available"]
    return30 = _num(economic30.get("qualificationReturn"))
    return7 = _num(economic7.get("qualificationReturn"))
    priority = (
        PROFIT_PRIORITY_30_WEIGHT * return30
        + PROFIT_PRIORITY_7_WEIGHT * return7
        if available else None
    )
    return priority, {
        "available": available,
        "mode": PROFIT_PRIORITY_MODE,
        "weights": {
            "30d": PROFIT_PRIORITY_30_WEIGHT,
            "7d": PROFIT_PRIORITY_7_WEIGHT,
        },
        "profitabilityBasis": PROFITABILITY_BASIS,
        "returns": {"30d": return30, "7d": return7},
        "windowStartEquity": {
            "30d": economic30["windowStartEquity"],
            "7d": economic7["windowStartEquity"],
        },
        "netPnl": {
            "30d": economic30["qualificationPnl"],
            "7d": economic7["qualificationPnl"],
        },
        "closedPnl": {
            "30d": economic30["closedPnl"],
            "7d": economic7["closedPnl"],
        },
        "openProfitReference": {
            "30d": economic30["openProfitReference"],
            "7d": economic7["openProfitReference"],
        },
        "openLoss": {
            "30d": economic30["openLoss"],
            "7d": economic7["openLoss"],
        },
        "openLossRatio": {
            "30d": economic30["openLossRatio"],
            "7d": economic7["openLossRatio"],
        },
        "value": priority,
    }


def profit_priority_sort_key(
    metrics: Mapping,
    *,
    follow_score_value: float = 0.0,
    addr: str = "",
) -> tuple:
    """Exact formation order: 70/30 priority, 30d, 7d, quality score, address."""
    priority, detail = compute_profit_priority(metrics)
    returns = detail["returns"]
    return (
        -(priority if priority is not None else float("-inf")),
        -returns["30d"],
        -returns["7d"],
        -_num(follow_score_value),
        str(addr or "").lower(),
    )


def _activity_age_hours(metrics: Mapping, as_of_ms: int) -> float | None:
    last_open = int(_num(metrics.get("last_copyable_open_ms")))
    if last_open <= 0:
        return None
    return max(0.0, (int(as_of_ms) - last_open) / 3_600_000.0)


def _official_return_context(metrics: Mapping, policy) -> dict:
    """Recover the qualifying official window without calling a 7-day return "30-day ROI"."""
    payload = parse_json_obj(metrics.get("official_perp_evidence_json"))
    windows = payload.get("windows") if isinstance(payload.get("windows"), dict) else {}
    evidence = (
        windows.get("officialPerp30d")
        if isinstance(windows.get("officialPerp30d"), dict)
        else {}
    )
    floor = _num(evidence.get("minimumReturn"), policy.official_perp_min_return_30d)
    return {
        "return": _num(metrics.get("official_perp_return_30d")),
        "floor": floor,
        "historyTier": evidence.get("historyTier") or "full_history",
        "windowDays": (
            _num(evidence.get("windowDays"))
            if evidence.get("windowDays") is not None else None
        ),
        "fundedCoverageDays": (
            _num(evidence.get("fundedCoverageDays", evidence.get("positiveCoverageDays")))
            if evidence.get("fundedCoverageDays", evidence.get("positiveCoverageDays")) is not None
            else None
        ),
    }


def evaluate_source_quality(
    metrics: Mapping,
    *,
    policy_values: Mapping | None = None,
    as_of_ms: int | None = None,
) -> dict:
    """Apply the sole deep-fill source-wallet quality contract."""
    policy = load_copy_policy(policy_values)
    as_of_ms = int(as_of_ms or time.time() * 1000)
    episodes = int(_num(metrics.get("source_episode_n_30d")))
    win_rate = metrics.get("source_win_rate_30d")
    top3_share = metrics.get("source_top3_profit_share")
    body_n = int(_num(metrics.get("source_body_after_top3_n")))
    body_win_rate = metrics.get("source_body_after_top3_win_rate")
    body_net = metrics.get("source_body_after_top3_net_pnl")
    activity_age = _activity_age_hours(metrics, as_of_ms)
    official = _official_return_context(metrics, policy)
    recent_net = _num(metrics.get("source_net_pnl_7d"))
    source_economics = _source_economics(metrics)
    source30 = source_economics["30"]
    source7 = source_economics["7"]
    standard_lane = episodes >= policy.source_min_episodes_30d
    low_frequency_lane = (
        policy.source_low_freq_min_episodes_30d
        <= episodes
        <= policy.source_low_freq_max_episodes_30d
    )
    source_lane = "standard" if standard_lane else "strong_low_frequency" if low_frequency_lane else None
    lane_win_floor = (
        policy.source_min_episode_win_rate
        if standard_lane else policy.source_low_freq_min_episode_win_rate
    )
    concentration_triggered = bool(
        top3_share is not None
        and _num(top3_share) >= policy.source_top3_concentration_trigger
    )
    checks = {
        "sourceClosedProfit30d": source30["closedPnl"] > 0.0,
        "sourceClosedProfit7d": source7["closedPnl"] > 0.0,
        "sourceOpenLossRatio": open_loss_ratio_within_limit(source30),
        "sourceConservativeProfit30d": source30["qualificationPnl"] > 0.0,
        "sourceConservativeProfit7d": source7["qualificationPnl"] > 0.0,
        "minimumCompleteEpisodes": source_lane is not None,
        "sourceWinRate": (
            win_rate is not None
            and _num(win_rate) >= lane_win_floor
        ),
        "lowFrequencyOfficialReturn": (
            not low_frequency_lane
            or official["return"] >= policy.source_low_freq_min_official_return
        ),
        "lowFrequencyRecentProfit": not low_frequency_lane or recent_net > 0.0,
        "activityWithin72h": activity_age is not None and activity_age <= 72.0,
        "concentratedBodyWinRate": (
            not concentration_triggered
            or (
                body_n > 0 and body_win_rate is not None
                and _num(body_win_rate) >= policy.source_body_min_win_rate
            )
        ),
        "concentratedBodyNonNegative": (
            not concentration_triggered
            or (body_net is not None and _num(body_net) >= 0.0)
        ),
    }
    failures = (
        ("source_30d_closed_pnl_not_positive", "sourceClosedProfit30d"),
        ("source_7d_closed_pnl_not_positive", "sourceClosedProfit7d"),
        ("source_open_loss_over_50pct", "sourceOpenLossRatio"),
        ("source_30d_conservative_pnl_not_positive", "sourceConservativeProfit30d"),
        ("source_7d_conservative_pnl_not_positive", "sourceConservativeProfit7d"),
        ("source_episode_evidence_insufficient", "minimumCompleteEpisodes"),
        (
            "source_low_frequency_win_rate_below_floor"
            if low_frequency_lane else "source_win_rate_below_floor",
            "sourceWinRate",
        ),
        ("source_low_frequency_official_return_below_floor", "lowFrequencyOfficialReturn"),
        ("source_low_frequency_recent_not_profitable", "lowFrequencyRecentProfit"),
        ("source_activity_stale", "activityWithin72h"),
        ("source_concentrated_body_win_rate_low", "concentratedBodyWinRate"),
        ("source_concentrated_body_unprofitable", "concentratedBodyNonNegative"),
    )
    first_failure = next((reason for reason, key in failures if not checks[key]), None)
    return {
        "eligible": first_failure is None,
        "status": "source_quality_passed" if first_failure is None else first_failure,
        "firstFailure": first_failure,
        "checks": checks,
        "episodeN30d": episodes,
        "qualityLane": source_lane,
        "winRateFloor": lane_win_floor,
        "winRate30d": _num(win_rate) if win_rate is not None else None,
        "activityAgeHours": activity_age,
        "top3ProfitShare": _num(top3_share) if top3_share is not None else None,
        "concentrationTriggered": concentration_triggered,
        "bodyAfterTop3N": body_n,
        "bodyAfterTop3WinRate": _num(body_win_rate) if body_win_rate is not None else None,
        "bodyAfterTop3NetPnl": _num(body_net) if body_net is not None else None,
        "profitabilityBasis": PROFITABILITY_BASIS,
        "economics": source_economics,
    }


def compute_source_quality_score(
    metrics: Mapping,
    *,
    policy_values: Mapping | None = None,
    as_of_ms: int | None = None,
) -> tuple[float, dict]:
    """Rank source-qualified wallets before the global Top40 cap."""
    policy = load_copy_policy(policy_values)
    as_of_ms = int(as_of_ms or time.time() * 1000)
    win_rate = _num(metrics.get("source_win_rate_30d"))
    episodes = int(_num(metrics.get("source_episode_n_30d")))
    activity_age = _activity_age_hours(metrics, as_of_ms)
    source = evaluate_source_quality(
        metrics, policy_values=policy_values, as_of_ms=as_of_ms,
    )
    low_frequency = source.get("qualityLane") == "strong_low_frequency"
    win_floor = (
        policy.source_low_freq_min_episode_win_rate
        if low_frequency else policy.source_min_episode_win_rate
    )
    episode_floor = (
        policy.source_low_freq_min_episodes_30d
        if low_frequency else policy.source_min_episodes_30d
    )
    win_score = _quality_above_floor(
        win_rate, win_floor, 0.25,
    )
    sample_score = _quality_above_floor(
        episodes, episode_floor, 30,
    )
    recency_score = 0.0 if activity_age is None else _clamp(1.0 - activity_age / 180.0)
    score = 0.55 * win_score + 0.30 * sample_score + 0.15 * recency_score
    return _clamp(score), {
        "profitabilityBasis": PROFITABILITY_BASIS,
        "officialPerpContribution": 0.0,
        "sourceQualityLane": source.get("qualityLane"),
        "sourceWinRateScore": win_score,
        "sourceSampleScore": sample_score,
        "sourceRecencyScore": recency_score,
    }


def evaluate_follow_eligibility(
    metrics: Mapping,
    *,
    stage: str = "rough",
    min_closed30: int | None = None,
    min_closed14: int | None = None,
    min_closed7: int | None = None,
    min_open_fill_rate: float | None = None,
    min_evidence_days: int | None = None,
    margin_equity_pct: float | None = None,
    policy_values: Mapping | None = None,
    as_of_ms: int | None = None,
    follow_score_value: float | None = None,
) -> dict:
    """Classify one rough or final strict Copy result with one ordered failure reason."""
    del min_closed14, min_closed7, min_evidence_days, margin_equity_pct, follow_score_value
    stage = "strict" if str(stage).lower() in {"strict", "final"} else "rough"
    policy = load_copy_policy(policy_values)
    as_of_ms = int(as_of_ms or time.time() * 1000)
    scoped = apply_allowed_sector_copy_metrics(metrics)
    policy_json = parse_json_obj(scoped.get("sector_policy_json"))
    c30 = int(_num(scoped.get("copy_bt_closed_n")))
    copy_win_rate = scoped.get("copy_bt_win_rate")
    economic30 = _copy_window_economics(scoped, 30)
    economic7 = _copy_window_economics(scoped, 7)
    pnl30 = economic30["qualificationPnl"]
    pnl7 = economic7["qualificationPnl"]
    equity30 = economic30["windowStartEquity"]
    equity7 = economic7["windowStartEquity"]
    return30 = _num(economic30.get("qualificationReturn"))
    return7 = _num(economic7.get("qualificationReturn"))
    open_rate = scoped.get("actionable_open_rate", scoped.get("copy_bt_open_fill_rate"))
    activity_age = _activity_age_hours(scoped, as_of_ms)
    data_status = str(scoped.get("copy_bt_data_status") or scoped.get("data_status") or "valid").lower()
    evidence_status = str(
        scoped.get("copy_bt_evidence_status") or scoped.get("evidence_status") or ""
    ).lower()
    valuation_status = str(scoped.get("copy_bt_valuation_status") or "complete").lower()
    path_status = str(scoped.get("copy_path_risk_status") or "").lower()
    official_status = str(scoped.get("official_perp_status") or "").lower()
    official_reason = str(scoped.get("official_perp_reason") or "official_perp_evidence_missing")
    source = evaluate_source_quality(scoped, policy_values=policy_values, as_of_ms=as_of_ms)
    minimum_closed = int(
        policy.rough_min_closed_30d if min_closed30 is None else min_closed30
    )
    minimum_open_rate = (
        policy.min_actionable_open_rate
        if min_open_fill_rate is None else float(min_open_fill_rate)
    )
    return_floor30 = policy.core_min_dynamic_copy_return_30d if stage == "strict" else 0.0
    return_floor7 = policy.core_min_dynamic_copy_return_7d if stage == "strict" else 0.0
    win_floor = (
        policy.core_min_copy_win_rate if stage == "strict" else policy.rough_min_win_rate
    )
    allowed = set(policy_json.get("allowed") or ())
    watched = set(policy_json.get("watch") or ())
    sector_ready = bool(allowed) if "allowed" in policy_json else True
    checks = {
        "copyDataValid": data_status in {"", "valid", "ok"} and evidence_status != "invalid",
        "officialPerpPassed": official_status == "passed",
        "sourceQualityPassed": bool(source.get("eligible")),
        "minimumClosedEvidence": c30 >= minimum_closed,
        "copyClosedProfit30d": economic30["closedPnl"] > 0.0,
        "copyClosedProfit7d": economic7["closedPnl"] > 0.0,
        "copyOpenLossRatio": open_loss_ratio_within_limit(economic30),
        # Fills-only rough replay runs before unified parameter tuning. It proves that both windows point in
        # the profitable direction; return magnitude belongs to the later profit-priority order. The tuned strict surface owns
        # the material 10%/3% admission contract.
        "copy30dReturn": (
            return30 >= return_floor30 if stage == "strict" else return30 > 0.0
        ),
        "copy7dReturn": (
            return7 >= return_floor7 if stage == "strict" else return7 > 0.0
        ),
        "copyWinRate": copy_win_rate is not None and _num(copy_win_rate) >= win_floor,
        "openExecution": open_rate is not None and _num(open_rate) >= minimum_open_rate,
        "activityWithin72h": activity_age is not None and activity_age <= 72.0,
        "valuationComplete": valuation_status == "complete",
        "sectorExecutable": sector_ready,
        "pathComplete": (
            stage != "strict"
            or path_status not in {"", "pending", "missing", "invalid", "replay_error", "incomplete"}
        ),
        "liquidationsWithinLimit": (
            stage != "strict"
            or int(_num(scoped.get("copy_bt_liquidations"))) <= policy.core_max_liquidations_30d
        ),
        # One material isolated loss is enough to reject the wallet even when its liquidation count is
        # otherwise within the tolerated sizing-noise budget. Apply this in rough and strict qualification
        # so a known 5% account hit cannot remain a Challenger waiting for promotion.
        "singleLiquidationLossWithinLimit": (
            _num(scoped.get("copy_bt_max_liquidation_loss_pct"))
            + 1e-12
            < policy.core_max_single_liquidation_loss_pct
        ),
    }
    if "allowed" in policy_json and not allowed and not watched:
        checks["sectorExecutable"] = False
    failures = (
        ("copy_data_error", "copyDataValid", True),
        (
            "copy_single_liquidation_loss_over_5pct",
            "singleLiquidationLossWithinLimit",
            False,
        ),
        (
            official_reason if official_status == "deferred_data_error" else "official_perp_not_qualified",
            "officialPerpPassed", official_status == "deferred_data_error",
        ),
        (source.get("firstFailure") or "source_quality_not_qualified", "sourceQualityPassed", False),
        ("copy_episode_evidence_insufficient", "minimumClosedEvidence", True),
        ("copy_30d_closed_pnl_not_positive", "copyClosedProfit30d", False),
        ("copy_7d_closed_pnl_not_positive", "copyClosedProfit7d", False),
        ("copy_open_loss_over_50pct", "copyOpenLossRatio", False),
        (
            "strict_copy_30d_conservative_return_below_floor"
            if stage == "strict" else "rough_copy_30d_conservative_not_profitable",
            "copy30dReturn", False,
        ),
        (
            "strict_copy_7d_conservative_return_below_floor"
            if stage == "strict" else "rough_copy_7d_conservative_not_profitable",
            "copy7dReturn", False,
        ),
        (f"{stage}_copy_win_rate_below_floor", "copyWinRate", False),
        (f"{stage}_copy_open_rate_below_floor", "openExecution", False),
        ("activity_over_72h", "activityWithin72h", False),
        ("copy_valuation_incomplete", "valuationComplete", True),
        ("sector_not_executable", "sectorExecutable", False),
        ("copy_path_incomplete", "pathComplete", True),
        ("strict_copy_liquidations_over_3", "liquidationsWithinLimit", False),
    )
    first_failure = None
    deferred = False
    for reason, key, is_deferred in failures:
        if not checks[key]:
            first_failure, deferred = reason, is_deferred
            break
    stage_eligible = first_failure is None
    research_eligible = (
        checks["copyDataValid"]
        and checks["singleLiquidationLossWithinLimit"]
        and checks["copyClosedProfit30d"]
        and checks["copyClosedProfit7d"]
        and checks["copyOpenLossRatio"]
        and (
            (pnl30 > 0.0 and pnl7 > 0.0)
            or deferred
            or official_status == "deferred_data_error"
        )
    )
    return {
        "eligible": research_eligible,
        "coreEligible": stage_eligible,
        "stageEligible": stage_eligible,
        "stage": stage,
        "status": f"{stage}_copy_qualified" if stage_eligible else first_failure,
        "firstFailure": first_failure,
        "role": "core_eligible" if stage_eligible else "challenger" if research_eligible else "rejected",
        "deferred": bool(deferred),
        "checks": checks,
        "returns": {"30": return30, "7": return7},
        "profitabilityBasis": PROFITABILITY_BASIS,
        "returnFloors": {"30": return_floor30, "7": return_floor7},
        "windowStartEquity": {"30": equity30, "7": equity7},
        "netPnl": {"30": pnl30, "7": pnl7},
        "economics": {"30": economic30, "7": economic7},
        "closedN": c30,
        "copyWinRate": _num(copy_win_rate) if copy_win_rate is not None else None,
        "copyWinRateFloor": win_floor,
        "openFillRate": _num(open_rate) if open_rate is not None else None,
        "openFillRateFloor": minimum_open_rate,
        "activityAgeHours": activity_age,
        "sourceQuality": source,
        "officialPerpEvidence": {"status": official_status or "missing", "reason": official_reason},
        "simulatedLiquidations": int(_num(scoped.get("copy_bt_liquidations"))),
        "maxSingleLiquidationLossPct": _num(
            scoped.get("copy_bt_max_liquidation_loss_pct")
        ),
        "maxSingleLiquidationLossLimitPct": (
            policy.core_max_single_liquidation_loss_pct
        ),
        "reasons": [] if stage_eligible else [str(first_failure or "copy_not_qualified")],
    }


def compute_follow_score(
    metrics: Mapping,
    *,
    policy_values: Mapping | None = None,
    stage: str | None = None,
) -> tuple[float, dict]:
    """Return the exact 40/30/20/10 monotonic ranking score; never a permission line."""
    scoped = apply_allowed_sector_copy_metrics(metrics)
    policy = load_copy_policy(policy_values)
    c30 = int(_num(scoped.get("copy_bt_closed_n")))
    if c30 <= 0 or scoped.get("copy_bt_net_pnl") is None:
        source_score = scoped.get("source_quality_score")
        if source_score is None:
            source_score = compute_source_quality_score(scoped, policy_values=policy_values)[0]
        return _clamp(_num(source_score)), {
            "sourceOnly": True,
            "sourceQualityScore": _clamp(_num(source_score)),
            "copyScore": None,
            "reasons": ["尚未进入Top40粗略Copy"],
        }
    stage = str(stage or scoped.get("copy_replay_stage") or "rough").lower()
    strict = stage in {"strict", "final"}
    floor30 = policy.core_min_dynamic_copy_return_30d if strict else 0.0
    floor7 = policy.core_min_dynamic_copy_return_7d if strict else 0.0
    economic30 = _copy_window_economics(scoped, 30)
    economic7 = _copy_window_economics(scoped, 7)
    pnl30 = economic30["qualificationPnl"]
    pnl7 = economic7["qualificationPnl"]
    return30 = _num(economic30.get("qualificationReturn"))
    return7 = _num(economic7.get("qualificationReturn"))
    source_win = _num(scoped.get("source_win_rate_30d"))
    copy_win = _num(scoped.get("copy_bt_win_rate"))
    open_rate = _clamp(_num(
        scoped.get("actionable_open_rate", scoped.get("copy_bt_open_fill_rate"))
    ))
    behavior = scoped.get(
        "copy_bt_behavior_replication_rate",
        scoped.get("copy_bt_add_fidelity"),
    )
    behavior_score = 0.50 if behavior is None else _clamp(_num(behavior))
    as_of_ms = int(_num(scoped.get("score_as_of_ms"), time.time() * 1000))
    activity_age = _activity_age_hours(scoped, as_of_ms)
    activity_score = 0.0 if activity_age is None else _clamp(1.0 - activity_age / 180.0)
    source_opens = int(_num(scoped.get("open_events_30d")))
    components = {
        "copy30d": _quality_above_floor(return30, floor30, 0.60),
        "copy7d": _quality_above_floor(return7, floor7, 0.25),
        "sourceWinRate": _quality_above_floor(
            source_win, policy.source_min_episode_win_rate, 0.25,
        ),
        "copyWinRate": _quality_above_floor(
            copy_win,
            policy.core_min_copy_win_rate if strict else policy.rough_min_win_rate,
            0.35,
        ),
        "openFollowRate": _quality_above_floor(
            open_rate, policy.min_actionable_open_rate, 0.30,
        ),
        "behaviorReplication": behavior_score,
        "activityRecency": activity_score,
        "independentOpens": _quality_above_floor(source_opens, 10, 30),
    }
    score = (
        0.25 * components["copy30d"]
        + 0.15 * components["copy7d"]
        + 0.20 * components["sourceWinRate"]
        + 0.10 * components["copyWinRate"]
        + 0.15 * components["openFollowRate"]
        + 0.05 * components["behaviorReplication"]
        + 0.05 * components["activityRecency"]
        + 0.05 * components["independentOpens"]
    )
    return _clamp(score), {
        "sourceOnly": False,
        "stage": "strict" if strict else "rough",
        "components": components,
        "profitabilityBasis": PROFITABILITY_BASIS,
        "economicReturns": {"30d": return30, "7d": return7},
        "economicEquities": {
            "30d": _replay_window_equity(scoped, 30),
            "7d": _replay_window_equity(scoped, 7),
        },
        "copyPnl": {"30d": pnl30, "7d": pnl7},
        "copyEconomics": {"30d": economic30, "7d": economic7},
        "closedN": {"30d": c30, "7d": int(_num(scoped.get("copy_bt_7d_closed_n")))},
        "sourceWinRate": source_win,
        "copyWinRate": copy_win,
        "openFillRate": open_rate,
        "activityAgeHours": activity_age,
        "liquidations": int(_num(scoped.get("copy_bt_liquidations"))),
        "maxSingleLiquidationLossPct": _num(
            scoped.get("copy_bt_max_liquidation_loss_pct")
        ),
        "feeDrag": scoped.get("copy_bt_fee_drag"),
        "reasons": [
            f"保守Copy 30d {return30 * 100:+.1f}% / 7d {return7 * 100:+.1f}%",
            f"源胜率 {source_win * 100:.1f}% / Copy胜率 {copy_win * 100:.1f}%",
            f"开仓跟随率 {open_rate * 100:.1f}%",
        ],
    }
