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
    PROFITABILITY_BASIS,
    conservative_profitability,
    open_loss_ratio_within_limit,
)
from hyper.copy.copy_policy import load_copy_policy
from hyper.copy.sector import apply_allowed_sector_copy_metrics, parse_json_obj
from . import pre_strict


PROFIT_PRIORITY_30_WEIGHT = 0.70
PROFIT_PRIORITY_7_WEIGHT = 0.30
PROFIT_PRIORITY_MODE = "conservative_realized_profit_70_30"
FOLLOW_SCORE_MODE = "strict_qualification_anchor_profit_confidence_v3"
FOLLOW_SCORE_PROFIT_SCALE = 0.35
FOLLOW_SCORE_CONFIDENCE_FLOOR = 0.85
STRICT_SCORE_QUALIFICATION_BASE = 0.60
STRICT_SCORE_PROFIT_WEIGHT = 0.35
STRICT_SCORE_RELIABILITY_WEIGHT = 0.05
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


def strict_score_formula() -> dict:
    return {
        "qualificationBase": STRICT_SCORE_QUALIFICATION_BASE,
        "profitWeight": STRICT_SCORE_PROFIT_WEIGHT,
        "reliabilityWeight": STRICT_SCORE_RELIABILITY_WEIGHT,
    }


def project_strict_score_detail(detail: Mapping | None) -> float | None:
    """Project frozen Strict evidence through the current display formula without mutating its generation."""
    detail = dict(detail or {})
    if str(detail.get("stage") or "").lower() not in {"strict", "final"}:
        return None
    if detail.get("profitComponent") is None or detail.get("reliability") is None:
        return None
    formula = strict_score_formula()
    return _clamp(
        formula["qualificationBase"]
        + formula["profitWeight"] * _clamp(_num(detail.get("profitComponent")))
        + formula["reliabilityWeight"] * _clamp(_num(detail.get("reliability")))
    )


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


def follow_score_sort_key(
    metrics: Mapping,
    *,
    follow_score_value: float = 0.0,
    addr: str = "",
) -> tuple:
    """Exact formation order: published score, then its raw economic and quality tie-breaks."""
    priority, detail = compute_profit_priority(metrics)
    returns = detail["returns"]
    return (
        -_num(follow_score_value),
        -(priority if priority is not None else float("-inf")),
        -returns["30d"],
        -returns["7d"],
        -_num(metrics.get("copy_bt_profit_factor")),
        str(addr or "").lower(),
    )


def profit_priority_sort_key(
    metrics: Mapping,
    *,
    follow_score_value: float = 0.0,
    addr: str = "",
) -> tuple:
    """Backward-compatible alias for the V5 profit-aligned score order."""
    return follow_score_sort_key(
        metrics, follow_score_value=follow_score_value, addr=addr,
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
    policy_values: Mapping | None = None,
) -> dict:
    """Classify rough/final Copy exclusively through the versioned pre-strict policy."""
    stage = "strict" if str(stage).lower() in {"strict", "final"} else "rough"
    scoped = apply_allowed_sector_copy_metrics(metrics)
    policy_json = parse_json_obj(scoped.get("sector_policy_json"))
    activity = scoped.get("pre_strict_activity")
    if not isinstance(activity, dict):
        activity = parse_json_obj(scoped.get("pre_strict_activity_json"))
    result = pre_strict.evaluate(
        scoped, activity, stage=stage, policy_values=policy_values,
    )
    allowed = set(policy_json.get("allowed") or ())
    watched = set(policy_json.get("watch") or ())
    sector_ready = bool(allowed) if "allowed" in policy_json else True
    if "allowed" in policy_json and not allowed and not watched:
        sector_ready = False
    if not sector_ready and result.get("eligible"):
        result = {
            **result, "eligible": False, "deferred": False,
            "status": "sector_not_executable", "firstFailure": "sector_not_executable",
        }
    economic30 = (result.get("copyEconomics") or {}).get("30") or {}
    economic7 = (result.get("copyEconomics") or {}).get("7") or {}
    checks = {
        **dict(result.get("checks") or {}),
        # Compatibility aliases for old immutable audit readers. They are not independent gates.
        "officialPerpPassed": True,
        "sourceQualityPassed": all(bool((result.get("checks") or {}).get(key)) for key in (
            "sourceClosedSample", "sourceClosedProfit30d", "sourceClosedProfit7d",
            "sourceOpenLossRatio", "sourceConservativeProfit30d",
            "sourceConservativeProfit7d", "sourceLottery",
        )),
        "minimumClosedEvidence": bool((result.get("checks") or {}).get("copyClosedSample")),
        "copy30dReturn": bool((result.get("checks") or {}).get(
            "strictReturn30d" if stage == "strict" else "copyConservativeProfit30d"
        )),
        "copy7dReturn": bool((result.get("checks") or {}).get(
            "strictReturn7d" if stage == "strict" else "copyConservativeProfit7d"
        )),
        "copyWinRate": bool((result.get("checks") or {}).get("copyLottery")),
        "activityWithin72h": bool((result.get("checks") or {}).get("activityOperational")),
        "sectorExecutable": sector_ready,
        "pathComplete": bool((result.get("checks") or {}).get("pathComplete")),
        "liquidationsWithinLimit": bool((result.get("checks") or {}).get("liquidationsWithinLimit")),
        "singleLiquidationLossWithinLimit": bool(
            (result.get("checks") or {}).get("singleLiquidationLossWithinLimit")
        ),
    }
    stage_eligible = bool(result.get("eligible") and sector_ready)
    deferred = bool(result.get("deferred"))
    first_failure = result.get("firstFailure")
    candidate_eligible = bool(stage_eligible or deferred)
    copy_win_rate = scoped.get("copy_bt_win_rate")
    open_rate = scoped.get("actionable_open_rate", scoped.get("copy_bt_open_fill_rate"))
    return {
        "eligible": candidate_eligible,
        "coreEligible": stage_eligible,
        "stageEligible": stage_eligible,
        "stage": stage,
        "status": f"{stage}_copy_qualified" if stage_eligible else first_failure,
        "firstFailure": first_failure,
        "role": "core_eligible" if stage_eligible else "challenger" if deferred else "rejected",
        "deferred": bool(deferred),
        "checks": checks,
        "returns": {
            "30": _num(economic30.get("qualificationReturn")),
            "7": _num(economic7.get("qualificationReturn")),
        },
        "profitabilityBasis": PROFITABILITY_BASIS,
        "returnFloors": {"30": 0.10 if stage == "strict" else 0.0, "7": 0.03 if stage == "strict" else 0.0},
        "windowStartEquity": {
            "30": economic30.get("windowStartEquity"),
            "7": economic7.get("windowStartEquity"),
        },
        "netPnl": {
            "30": economic30.get("qualificationPnl"),
            "7": economic7.get("qualificationPnl"),
        },
        "economics": {"30": economic30, "7": economic7},
        "closedN": int(_num(scoped.get("copy_bt_closed_n"))),
        "copyWinRate": _num(copy_win_rate) if copy_win_rate is not None else None,
        "copyWinRateFloor": None,
        "openFillRate": _num(open_rate) if open_rate is not None else None,
        "openFillRateFloor": 0.70,
        "activity": activity,
        "sourceQuality": {
            "eligible": checks["sourceQualityPassed"],
            "lottery": result.get("sourceLottery"),
        },
        "copyLottery": result.get("copyLottery"),
        "copyProfitFactor": _num(scoped.get("copy_bt_profit_factor")),
        "copyPayoffRatio": _num(scoped.get("copy_bt_payoff_ratio")),
        "officialPerpEvidence": {"status": "audit_only", "reason": "not_an_admission_gate"},
        "simulatedLiquidations": int(_num(scoped.get("copy_bt_liquidations"))),
        "maxSingleLiquidationLossPct": _num(
            scoped.get("copy_bt_max_liquidation_loss_pct")
        ),
        "maxSingleLiquidationLossLimitPct": float(
            config.COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT
        ),
        "reasons": [] if stage_eligible else [str(first_failure or "copy_not_qualified")],
    }


def compute_follow_score(
    metrics: Mapping,
    *,
    policy_values: Mapping | None = None,
    stage: str | None = None,
) -> tuple[float, dict]:
    """Return the profit-aligned score used by both the funnel and final formation.

    Conservative 70/30 Copy return owns the score. Qualified execution/repeatability evidence may only
    haircut it by at most 15%; it can never manufacture a high score for a low-return wallet.
    """
    scoped = apply_allowed_sector_copy_metrics(metrics)
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
    economic30 = _copy_window_economics(scoped, 30)
    economic7 = _copy_window_economics(scoped, 7)
    pnl30 = economic30["qualificationPnl"]
    pnl7 = economic7["qualificationPnl"]
    return30 = _num(economic30.get("qualificationReturn"))
    return7 = _num(economic7.get("qualificationReturn"))
    profit_priority = (
        PROFIT_PRIORITY_30_WEIGHT * return30
        + PROFIT_PRIORITY_7_WEIGHT * return7
    )
    profit_component = _clamp(
        1.0 - math.exp(
            -max(0.0, profit_priority)
            / max(1e-9, FOLLOW_SCORE_PROFIT_SCALE)
        )
    )
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
    profit_factor = _num(scoped.get("copy_bt_profit_factor"))
    profit_factor_score = _quality_above_floor(profit_factor, 1.25, 2.75)
    sample_score = _quality_above_floor(c30, 7, 23)
    execution_score = (
        0.75 * _quality_above_floor(open_rate, 0.70, 0.30)
        + 0.25 * behavior_score
    )
    top3_share = _clamp(_num(scoped.get("copy_bt_top3_profit_share"), 0.50))
    body_n = int(_num(scoped.get("copy_bt_body_after_top3_n")))
    body_net_raw = scoped.get("copy_bt_body_after_top3_net_pnl")
    body_score = (
        0.50 if body_net_raw is None or body_n <= 0
        else 1.0 if _num(body_net_raw) >= 0.0 else 0.0
    )
    repeatability_score = 0.65 * _clamp(1.0 - top3_share) + 0.35 * body_score
    activity = scoped.get("pre_strict_activity")
    if not isinstance(activity, dict):
        activity = parse_json_obj(scoped.get("pre_strict_activity_json"))
    as_of_ms = int(_num(scoped.get("score_as_of_ms"), time.time() * 1000))
    activity_age = _activity_age_hours(scoped, as_of_ms)
    active_weeks = _clamp(_num(activity.get("activeWeeks4")) / 4.0)
    latest_active = 1.0 if activity.get("latest7dActive") else 0.0
    max_gap_days = _num(activity.get("maxOpenGapDays28d"), 10.0)
    activity_score = (
        0.50 * active_weeks
        + 0.25 * latest_active
        + 0.25 * _clamp(1.0 - max_gap_days / 10.0)
    )
    liquidations = int(_num(scoped.get("copy_bt_liquidations")))
    max_liquidation_loss = _num(scoped.get("copy_bt_max_liquidation_loss_pct"))
    catastrophic_liquidation_loss_pct = max(
        1e-9, float(config.COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT)
    )
    liquidation_score = 0.50 * _clamp(1.0 - liquidations / 4.0) + 0.50 * _clamp(
        1.0 - max_liquidation_loss / catastrophic_liquidation_loss_pct
    )
    components = {
        "profitPriority": profit_component,
        "profitFactorConfidence": profit_factor_score,
        "sampleConfidence": sample_score,
        "executionConfidence": execution_score,
        "repeatabilityConfidence": repeatability_score,
        "activityConfidence": activity_score,
        "liquidationSafety": liquidation_score,
    }
    reliability = (
        0.25 * profit_factor_score
        + 0.20 * sample_score
        + 0.20 * execution_score
        + 0.15 * repeatability_score
        + 0.10 * activity_score
        + 0.10 * liquidation_score
    )
    confidence_multiplier = (
        FOLLOW_SCORE_CONFIDENCE_FLOOR
        + (1.0 - FOLLOW_SCORE_CONFIDENCE_FLOOR) * reliability
    )
    if strict:
        # A final-Strict wallet has already passed the complete source, activity, PF, execution, path and
        # liquidation contract.  Give that certification a visible baseline, then preserve profit-led order
        # inside the qualified pool.  Reliability remains a small differentiator instead of duplicating
        # hard gates or making a valid Core look like a failing 48/100 wallet. Rough/pre-strict ranking keeps
        # the unanchored economic score so unverified wallets cannot inherit the qualification baseline.
        score = (
            STRICT_SCORE_QUALIFICATION_BASE
            + STRICT_SCORE_PROFIT_WEIGHT * profit_component
            + STRICT_SCORE_RELIABILITY_WEIGHT * reliability
        )
        score_formula = strict_score_formula()
    else:
        score = profit_component * confidence_multiplier
        score_formula = {
            "qualificationBase": 0.0,
            "profitWeight": confidence_multiplier,
            "reliabilityWeight": 0.0,
        }
    return _clamp(score), {
        "sourceOnly": False,
        "stage": "strict" if strict else "rough",
        "mode": FOLLOW_SCORE_MODE,
        "components": components,
        "profitPriorityValue": profit_priority,
        "profitComponent": profit_component,
        "reliability": reliability,
        "confidenceMultiplier": confidence_multiplier,
        "scoreFormula": score_formula,
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
        "activity": activity,
        "activityAgeHours": activity_age,
        "liquidations": liquidations,
        "maxSingleLiquidationLossPct": _num(
            scoped.get("copy_bt_max_liquidation_loss_pct")
        ),
        "feeDrag": scoped.get("copy_bt_fee_drag"),
        "reasons": [
            f"盈利优先 {profit_priority * 100:+.1f}%"
            f"（30d {return30 * 100:+.1f}% / 7d {return7 * 100:+.1f}%）",
            f"可信度 {reliability * 100:.1f}% / 系数 {confidence_multiplier:.3f}",
            f"PF {profit_factor:.2f} / 开仓跟随率 {open_rate * 100:.1f}%",
        ],
    }
