"""Copy-follow score used to rank the final watchlist.

`profile.score` remains the raw profile quality score. This module blends it with
copy-backtest evidence so the observer follows wallets that are actually copyable
under our own sizing/add/stop rules.
"""

from __future__ import annotations

import math
import time
from typing import Mapping

from hyper import config
from hyper.copy.copy_policy import load_copy_policy
from hyper.copy.sector import apply_allowed_sector_copy_metrics, parse_json_obj


def _num(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _has_copy_evidence(metrics: Mapping, c30: int, c14: int, c7: int) -> bool:
    return any(metrics.get(k) is not None for k in (
        "copy_expected_return", "copy_return_lcb", "copy_positive_probability",
    )) and c30 > 0


def evaluate_follow_eligibility(
    metrics: Mapping,
    *,
    min_closed30: int | None = None,
    min_closed14: int | None = None,
    min_closed7: int | None = None,
    min_open_fill_rate: float | None = None,
    min_expected_return: float | None = None,
    min_evidence_days: int | None = None,
    margin_equity_pct: float | None = None,
    retention: bool = False,
    policy_values: Mapping | None = None,
    as_of_ms: int | None = None,
) -> dict:
    """Classify evidence once: positive wallets remain researchable; Core adds non-duplicated proof.

    The old policy independently gated 7/14/30 returns, win rates, PF, tail-2 and concentration body.  Those
    checks were correlated views of the same trades and created an exclusion cascade.  V3 keeps hard data,
    liquidation and deep-loss safety; profitability under canonical replay retains research eligibility.
    The bounded formation surface may publish that wallet as Challenger. Target-wallet return stability is
    already owned by the official Portfolio front gate; Core adds ten campaigns, fresh activity, one-winner
    removal, whole-window cost stress and executable capacity without repeating four weekly Copy vetoes.
    """
    del min_closed14, min_closed7, margin_equity_pct
    policy = load_copy_policy(policy_values)
    min_closed30 = policy.min_closed_30d if min_closed30 is None else int(min_closed30)
    min_evidence_days = min(5, min_closed30) if min_evidence_days is None else int(min_evidence_days)
    min_open_fill_rate = (
        policy.min_actionable_open_rate if min_open_fill_rate is None else float(min_open_fill_rate)
    )
    min_expected_return = (
        policy.min_expected_margin_return if min_expected_return is None else float(min_expected_return)
    )
    source = metrics
    scoped = apply_allowed_sector_copy_metrics(metrics)
    policy_json = parse_json_obj(source.get("sector_policy_json"))
    copy_weekly = (
        policy_json.get("copyWeeklyProfitability")
        if isinstance(policy_json.get("copyWeeklyProfitability"), dict) else {}
    )
    allowed = set(policy_json.get("allowed") or ())
    watched = set(policy_json.get("watch") or ())
    c30 = int(_num(scoped.get("copy_bt_closed_n")))
    campaigns = int(_num(scoped.get("copy_bt_campaign_closed_n"), c30))
    pnl30 = _num(scoped.get("copy_bt_net_pnl"))
    equity = max(1.0, _num(
        scoped.get("initial_margin_equity"), getattr(config, "INITIAL_BALANCE", 10_000.0),
    ))
    return30 = pnl30 / equity
    evidence_days = int(_num(scoped.get("copy_evidence_days")))
    data_status = str(scoped.get("copy_bt_data_status") or "").strip().lower()
    evidence_status = str(scoped.get("copy_bt_evidence_status") or "").strip().lower()
    valuation_status = str(scoped.get("copy_bt_valuation_status") or "complete").strip().lower()
    path_status = str(scoped.get("copy_path_risk_status") or "").strip().lower()
    intratrade_dd = max(0.0, _num(scoped.get("copy_intratrade_max_drawdown")))
    failed_deep = int(_num(scoped.get("copy_failed_deep_bag_n")))
    deep_events = int(_num(scoped.get("copy_deep_bag_event_n")))
    recovery_rate = _num(scoped.get("copy_deep_bag_recovery_rate"), 1.0)
    current_loss = max(
        abs(min(0.0, _num(scoped.get("copy_current_open_loss_frac")))),
        max(0.0, _num(scoped.get("copy_current_drawdown_frac"))),
    )
    current_bag_hours = max(0.0, _num(scoped.get("copy_current_bag_hours")))
    liquidations = int(_num(scoped.get("copy_bt_liquidations")))
    forward_liquidations = int(_num(scoped.get("forward_liquidations")))
    top1 = scoped.get("copy_bt_campaign_net_after_top1")
    if top1 is None:
        # Compact legacy results always carried top2 but not every caller exposed top1. Do not invent a pass.
        top1 = scoped.get("copy_bt_net_after_top1")
    cost_stress = scoped.get("copy_bt_cost_stress_net_pnl")
    open_rate = scoped.get("actionable_open_rate", scoped.get("copy_bt_open_fill_rate"))
    capacity = scoped.get("capacity_fit")
    expected = scoped.get("copy_expected_return")
    last_open = int(_num(scoped.get("last_copyable_open_ms")))
    as_of_ms = int(as_of_ms or time.time() * 1000)
    activity_age_ms = as_of_ms - last_open if last_open > 0 else None
    activity_ok = bool(
        last_open > 0 and 0 <= activity_age_ms <= int(config.INACTIVE_DAYS * 86_400_000)
    )
    sector_ready = bool(allowed) if "allowed" in policy_json else True
    path_complete = path_status not in {"pending", "missing", "invalid", "replay_error", "incomplete"}
    copy_weekly_sufficient = bool(copy_weekly.get("evidenceSufficient"))
    copy_weekly_positive = bool(copy_weekly.get("passed"))
    campaign_win_rate = _num(scoped.get(
        "copy_bt_campaign_win_rate", scoped.get("copy_bt_win_rate"),
    ))
    body_n = int(_num(scoped.get("copy_bt_body_after_top3_n")))
    body_win_rate = _num(scoped.get("copy_bt_body_after_top3_win_rate"))
    body_net_pnl = scoped.get("copy_bt_body_after_top3_net_pnl")
    follow_score_value = compute_follow_score(scoped, policy_values=policy_values)[0]
    sample_ok = bool(
        c30 >= min_closed30 and campaigns >= policy.core_min_campaigns_30d
        and evidence_days >= min_evidence_days
    )
    checks = {
        "copyDataValid": not data_status or data_status in {"valid", "ok"},
        "normalizedEvidencePresent": _has_copy_evidence(scoped, c30, 0, 0),
        "strictCopy30dPositive": pnl30 > 0.0,
        "strictCopyWeeklyPositive": copy_weekly_positive,
        # Diagnostic/score reference only. Official four-week Portfolio stability owns the target-wallet
        # return floor; strict Copy owns whether our funded implementation is net profitable.
        "coreReturnReference": return30 >= (
            policy.retention_min_return_30d if retention else policy.core_min_return_30d
        ),
        "tenIndependentCampaigns": campaigns >= policy.core_min_campaigns_30d,
        "campaignWinRate": campaign_win_rate >= policy.core_min_campaign_win_rate,
        "repeatableBodyWinRate": (
            body_n > 0 and body_win_rate >= policy.core_min_body_win_rate
        ),
        "repeatableBodyPositive": body_net_pnl is not None and _num(body_net_pnl) > 0.0,
        "coreFollowScore": follow_score_value >= policy.core_min_follow_score,
        "activityWithin72h": activity_ok,
        "oneWinnerRemovalPositive": top1 is not None and _num(top1) > 0.0,
        "costStressPositive": cost_stress is not None and _num(cost_stress) > 0.0,
        "openExecution": open_rate is None or _num(open_rate, 1.0) >= min_open_fill_rate,
        "capacity": capacity is None or _num(capacity) >= policy.min_capacity_fit,
        "valuationComplete": valuation_status == "complete",
        "pathRiskComplete": path_complete,
        "pathDrawdownWithinCore": intratrade_dd <= policy.intratrade_dd_core_max,
        "sectorExecutable": sector_ready,
        "expectedEdge": expected is None or _num(expected) >= min_expected_return,
        "noRepeatedLiquidation": liquidations <= policy.core_max_liquidations_30d,
        "noForwardLiquidation": forward_liquidations == 0,
    }
    detail = {
        "returns": {"30": return30}, "campaigns": {"30": campaigns},
        "evidenceDays": evidence_days, "copyWeeklyProfitability": copy_weekly,
        "repeatability": {
            "campaignWinRate": campaign_win_rate,
            "campaignWinRateFloor": policy.core_min_campaign_win_rate,
            "bodyAfterTop3N": body_n,
            "bodyAfterTop3WinRate": body_win_rate if body_n > 0 else None,
            "bodyWinRateFloor": policy.core_min_body_win_rate,
            "bodyAfterTop3NetPnl": _num(body_net_pnl) if body_net_pnl is not None else None,
        },
        "followScore": follow_score_value,
        "coreFollowScoreFloor": policy.core_min_follow_score,
        "activityAgeHours": activity_age_ms / 3_600_000 if activity_age_ms is not None else None,
        "campaignNetAfterTop1": _num(top1) if top1 is not None else None,
        "costStressNetPnl": _num(cost_stress) if cost_stress is not None else None,
        "checks": checks, "retentionSurface": bool(retention),
        "softFailConfirmationsRequired": policy.soft_fail_confirmations,
    }

    if not checks["copyDataValid"] or evidence_status == "invalid":
        return {"eligible": False, "coreEligible": False, "status": "copy_data_error",
                "role": "quarantine", "deferred": True, **detail,
                "reasons": ["copy回放数据无效，等待同一钱包重新生成证据"]}
    if evidence_status in {"no_evidence", "no_fills", "no_open_events"}:
        return {"eligible": False, "coreEligible": False, "status": "no_copy_evidence",
                "role": "rejected", **detail, "reasons": ["没有可执行flat→open事件，无法进行跟单"]}
    current_deep_risk = bool(
        current_loss >= policy.deep_bag_event_pct
        or (current_loss >= 0.05 and current_bag_hours >= policy.deep_bag_long_hours)
    )
    if current_deep_risk:
        return {"eligible": False, "coreEligible": False, "status": "current_deep_loss_freeze",
                "role": "exit_only", "hardRisk": True, **detail,
                "reasons": ["当前深度浮亏触发硬风险，仅允许退出"]}
    deep_reject = bool(
        intratrade_dd > policy.intratrade_dd_reject
        or failed_deep > policy.deep_bag_max_failed
        or (deep_events >= 2 and recovery_rate < policy.deep_bag_min_recovery_rate)
    )
    if deep_reject or liquidations > policy.core_max_liquidations_30d:
        return {"eligible": False, "coreEligible": False, "status": "hard_copy_risk",
                "role": "rejected", "hardRisk": True, **detail,
                "reasons": ["严格Copy存在重复爆仓或不可接受的历史深亏"]}
    if pnl30 <= 0.0:
        return {"eligible": False, "coreEligible": False, "status": "copy_not_profitable",
                "role": "rejected", **detail, "reasons": ["30天严格Copy净收益不为正"]}
    if not checks["normalizedEvidencePresent"]:
        return {"eligible": False, "coreEligible": False, "status": "normalized_evidence_missing",
                "role": "quarantine", "deferred": True, **detail,
                "reasons": ["归一化Copy证据缺失，保留并等待重放而不是经济淘汰"]}
    if "allowed" in policy_json and not allowed and not watched:
        return {"eligible": False, "coreEligible": False, "status": "no_copyable_sector",
                "role": "rejected", **detail, "reasons": ["没有盈利且可执行的Crypto/Stock板块"]}

    core_eligible = bool(
        sample_ok and copy_weekly_positive and activity_ok
        and checks["campaignWinRate"] and checks["repeatableBodyWinRate"]
        and checks["repeatableBodyPositive"] and checks["coreFollowScore"]
        and checks["oneWinnerRemovalPositive"] and checks["costStressPositive"]
        and checks["openExecution"] and checks["capacity"] and checks["valuationComplete"]
        and checks["pathRiskComplete"] and checks["pathDrawdownWithinCore"]
        and checks["sectorExecutable"] and checks["expectedEdge"]
        and checks["noForwardLiquidation"] and not bool(policy_json.get("coreBlocked"))
    )
    if core_eligible:
        return {"eligible": True, "coreEligible": True,
                "status": "core_retention_eligible" if retention else "core_eligible",
                "role": "core_eligible", **detail,
                "reasons": ["官方四段7日稳定性、严格Copy、可重复胜率、75分质量线、72小时活动及执行压力均通过"]}

    if not copy_weekly_sufficient:
        status, reason = (
            "challenger_copy_weekly_evidence_building",
            "严格Copy四个非重叠7日区间证据不足，继续积累而不按亏损淘汰",
        )
    elif not copy_weekly_positive:
        status, reason = (
            "challenger_copy_weekly_loss",
            "目标钱包官方周收益达标，但我们的严格Copy至少一个7日区间净亏损",
        )
    elif not sample_ok:
        status, reason = "challenger_campaign_evidence_building", "独立Campaign或证据日不足，继续积累"
    elif not activity_ok:
        status, reason = "challenger_activity_watch", "最近72小时没有新的可执行flat→open信号"
    elif (
        not checks["campaignWinRate"] or not checks["repeatableBodyWinRate"]
        or not checks["repeatableBodyPositive"]
    ):
        status, reason = (
            "challenger_repeatability_watch",
            "Campaign或去除前三大赢家后的主体胜率/收益不足，暂不承担随机入场时点风险",
        )
    elif not checks["coreFollowScore"]:
        status, reason = "challenger_score_watch", "综合质量分未达到Core 75分准入线"
    elif not checks["oneWinnerRemovalPositive"]:
        status, reason = "challenger_outlier_watch", "移除最大一个盈利Campaign后不再为正"
    elif not checks["costStressPositive"]:
        status, reason = "challenger_cost_stress_watch", "1.5倍成本压力后暂不盈利"
    elif not checks["pathRiskComplete"]:
        status, reason = "challenger_path_risk_pending", "路径风险证据尚未完整"
    elif not checks["pathDrawdownWithinCore"]:
        status, reason = "challenger_intratrade_drawdown", "盘中回撤高于Core线但未触发淘汰线"
    elif not checks["sectorExecutable"]:
        status, reason = "challenger_sector_watch", "板块证据仍处于观察状态"
    else:
        status, reason = "challenger_execution_watch", "执行、容量、估值或前向风险条件尚未全部通过"
    return {"eligible": True, "coreEligible": False, "status": status,
            "role": "challenger", "researchEligible": True, **detail, "reasons": [reason]}


def compute_follow_score(
    metrics: Mapping, *, policy_values: Mapping | None = None,
) -> tuple[float, dict]:
    """Score five non-duplicated properties on a calibrated 0–100 quality scale.

    Economics answers whether our canonical account made enough money. Repeatability answers whether that
    money survives an arbitrary follow start rather than depending on a rare winner. Edge confidence,
    operability and path risk retain their separate meanings. Evidence completeness shrinks the total score;
    it no longer lets a six-Campaign perfect streak masquerade as Core-grade proof.
    """
    metrics = apply_allowed_sector_copy_metrics(metrics)
    raw = _clamp(_num(metrics.get("score")))
    c30 = int(_num(metrics.get("copy_bt_closed_n")))
    c14 = int(_num(metrics.get("copy_bt_14d_closed_n")))
    c7 = int(_num(metrics.get("copy_bt_7d_closed_n")))
    has_copy = _has_copy_evidence(metrics, c30, c14, c7)
    if not has_copy:
        return raw * 0.35, {
            "rawScore": raw,
            "copyScore": None,
            "confidence": 0.0,
            "copyPnl": {"30d": None, "14d": None, "7d": None},
            "closedN": {"30d": c30, "14d": c14, "7d": c7},
            "reasons": ["暂无归一化copy证据，仅保留Raw先验"],
        }
    expected = _num(metrics.get("copy_expected_return"))
    lcb = _num(metrics.get("copy_return_lcb"))
    probability = _num(metrics.get("copy_positive_probability"), 0.5)
    risk = _clamp(_num(metrics.get("copy_risk_score"), 0.5))
    execution = metrics.get("execution_score")
    if execution is None:
        execution = (
            _num(metrics.get("actionable_open_rate", metrics.get("copy_bt_open_fill_rate")), 0.0)
            + _num(metrics.get("capacity_fit"), 0.0)
        ) / 2.0
    execution = _clamp(_num(execution))
    if metrics.get("copy_bt_add_fidelity_applied"):
        # V2 add fidelity is continuous ranking evidence only. Noise-merged fragments are excluded inside
        # the metric; only capacity/liquidity blocks and entry-path divergence can lower this component.
        execution = _clamp((execution + _num(metrics.get("copy_bt_add_fidelity"), 1.0)) / 2.0)
    evidence_days = int(_num(metrics.get("copy_evidence_days")))
    policy = load_copy_policy(policy_values)
    # Qualification already defines seven 30d closes and five independent days as sufficient evidence.
    # Continuing to shrink a qualified wallet toward a neutral 0.5 until 20 closes/10 days silently ranks
    # a five-close +30% week behind a much thinner but older wallet.  Saturate at the actual evidence floors;
    # below them the continuous factor still keeps observation-only wallets appropriately conservative.
    closed_confidence = _clamp(c30 / max(1.0, float(policy.min_closed_30d)))
    day_confidence = _clamp(evidence_days / max(1.0, float(min(5, policy.min_closed_30d))))
    confidence = min(closed_confidence, day_confidence)
    edge_score = _clamp(0.5 + 0.5 * math.tanh((expected - 0.02) / 0.05))
    lcb_score = _clamp(0.5 + 0.5 * math.tanh(lcb / 0.03))
    probability_score = _clamp((probability - 0.90) / 0.10)
    pnl30 = _num(metrics.get("copy_bt_net_pnl"))
    pnl14 = _num(metrics.get("copy_bt_14d_net_pnl"))
    pnl7 = _num(metrics.get("copy_bt_7d_net_pnl"))
    economic_equity = max(
        1.0,
        _num(metrics.get("initial_margin_equity"), float(getattr(config, "INITIAL_BALANCE", 10_000.0))),
    )
    returns = {
        "30d": pnl30 / economic_equity,
        "14d": pnl14 / economic_equity,
        "7d": pnl7 / economic_equity,
    }
    # These are saturating percentage scales, not fixed dollar bonuses.  A future $20k/$30k account with
    # proportionally scaled replay PnL therefore receives the same economic score.  The final evidence
    # shrinkage prevents a three-trade windfall from outranking mature proof solely on one large episode.
    economic_score = (
        0.60 * _clamp(0.5 + (returns["30d"] - policy.core_min_return_30d) / 0.10)
        + 0.25 * _clamp(0.5 + 0.5 * math.tanh(returns["14d"] / 0.05))
        + 0.15 * _clamp(0.5 + 0.5 * math.tanh(returns["7d"] / 0.05))
    )
    campaign_n = int(_num(metrics.get("copy_bt_campaign_closed_n"), c30))
    campaign_win_rate = _clamp(_num(
        metrics.get("copy_bt_campaign_win_rate", metrics.get("copy_bt_win_rate")),
    ))
    body_n = int(_num(metrics.get("copy_bt_body_after_top3_n")))
    body_win_rate = _clamp(_num(metrics.get("copy_bt_body_after_top3_win_rate")))
    body_net_pnl = _num(metrics.get("copy_bt_body_after_top3_net_pnl"))
    top3_share = _clamp(_num(metrics.get("copy_bt_top3_profit_share"), 1.0))
    campaign_win_quality = _clamp((campaign_win_rate - 0.30) / 0.40)
    body_win_quality = _clamp((body_win_rate - 0.30) / 0.40) if body_n > 0 else 0.0
    concentration_quality = _clamp((0.80 - top3_share) / 0.30)
    repeatability_score = (
        0.35 * campaign_win_quality
        + 0.30 * body_win_quality
        + 0.20 * concentration_quality
        + 0.15 * (1.0 if body_n > 0 and body_net_pnl > 0.0 else 0.0)
    )
    profit_factor = max(0.0, _num(metrics.get("copy_bt_profit_factor")))
    payoff_ratio = max(0.0, _num(metrics.get("copy_bt_payoff_ratio")))
    profit_factor_quality = _clamp(math.log(max(1.0, profit_factor)) / math.log(5.0))
    payoff_quality = _clamp(payoff_ratio / 2.0)
    edge_confidence_score = (
        0.25 * edge_score + 0.25 * lcb_score + 0.20 * probability_score
        + 0.15 * profit_factor_quality + 0.15 * payoff_quality
    )
    activity = _clamp(_num(metrics.get("open_probability_48h"), 0.0))
    actionable_rate = _clamp(_num(
        metrics.get("actionable_open_rate", metrics.get("copy_bt_open_fill_rate")),
    ))
    capacity_fit = _clamp(_num(metrics.get("capacity_fit")))
    operability_score = (
        0.45 * execution + 0.20 * actionable_rate + 0.15 * capacity_fit + 0.20 * activity
    )
    liqs = int(_num(metrics.get("copy_bt_liquidations")))
    liquidation_cleanliness = 1.0 - _clamp(liqs / max(1.0, float(campaign_n)))
    risk_score = 0.75 * risk + 0.25 * liquidation_cleanliness
    score = (
        0.05 * raw + 0.22 * economic_score + 0.30 * repeatability_score
        + 0.18 * edge_confidence_score + 0.13 * operability_score + 0.12 * risk_score
    )
    campaign_confidence = _clamp(
        campaign_n / max(1.0, float(policy.core_min_campaigns_30d))
    )
    readiness_confidence = min(confidence, campaign_confidence)
    score *= 0.70 + 0.30 * readiness_confidence
    reasons = [
        f"预期保证金收益{expected * 100:+.1f}%",
        f"LCB {lcb * 100:+.1f}%",
        f"盈利概率{probability * 100:.0f}%",
        f"独立证据{evidence_days}天/{c30}笔",
    ]
    liquidation_rate = liqs / c30 if c30 > 0 else 0.0
    if liqs > 0:
        reasons.append(f"copy爆仓{liqs}次/{c30}回合（按Campaign频率进入风险柱）")
    score = _clamp(score)
    return score, {
        "rawScore": raw,
        "copyScore": edge_confidence_score,
        "economicScore": economic_score,
        "repeatabilityScore": repeatability_score,
        "edgeConfidenceScore": edge_confidence_score,
        "operabilityScore": operability_score,
        "calibratedRiskScore": risk_score,
        "economicReturns": returns,
        "economicEquity": economic_equity,
        "confidence": readiness_confidence,
        "sampleConfidence": confidence,
        "copyPnl": {
            "30d": metrics.get("copy_bt_net_pnl"),
            "14d": metrics.get("copy_bt_14d_net_pnl"),
            "7d": metrics.get("copy_bt_7d_net_pnl"),
        },
        "closedN": {"30d": c30, "14d": c14, "7d": c7},
        "expectedReturn": expected,
        "returnLcb": lcb,
        "positiveProbability": probability,
        "evidenceDays": evidence_days,
        "riskScore": risk,
        "executionScore": execution,
        "profitFactor": profit_factor,
        "payoffRatio": payoff_ratio,
        "netAfterTop1": metrics.get("copy_bt_net_after_top1"),
        "netAfterTop2": metrics.get("copy_bt_net_after_top2"),
        "top1ProfitShare": metrics.get("copy_bt_top1_profit_share"),
        "top3ProfitShare": metrics.get("copy_bt_top3_profit_share"),
        "costStressNetPnl": metrics.get("copy_bt_cost_stress_net_pnl"),
        "bodyAfterTop3": {
            "episodes": metrics.get("copy_bt_body_after_top3_n"),
            "wins": metrics.get("copy_bt_body_after_top3_wins"),
            "losses": metrics.get("copy_bt_body_after_top3_losses"),
            "winRate": metrics.get("copy_bt_body_after_top3_win_rate"),
            "netPnl": metrics.get("copy_bt_body_after_top3_net_pnl"),
            "profitFactor": metrics.get("copy_bt_body_after_top3_profit_factor"),
            "payoffRatio": metrics.get("copy_bt_body_after_top3_payoff_ratio"),
            "medianPnl": metrics.get("copy_bt_body_after_top3_median_pnl"),
        },
        "addMetrics": {
            "version": metrics.get("copy_bt_add_metrics_version"),
            "outcomeCounts": metrics.get("copy_bt_add_outcome_counts"),
            "rawAddOrderFollowRate": metrics.get("copy_bt_raw_add_order_follow_rate"),
            "actionableAddCaptureRate": metrics.get("copy_bt_actionable_add_capture_rate"),
            "entryGapPctWeighted": metrics.get("copy_bt_entry_gap_pct_weighted"),
            "entryGapPctP90": metrics.get("copy_bt_entry_gap_pct_p90"),
            "addFidelity": metrics.get("copy_bt_add_fidelity"),
            "behaviorReplicationV2": metrics.get("copy_bt_behavior_replication_v2"),
        },
        "openFillRate": metrics.get("actionable_open_rate", metrics.get("copy_bt_open_fill_rate")),
        "liquidations": liqs,
        "liquidationRate": liquidation_rate,
        "feeDrag": metrics.get("copy_bt_fee_drag"),
        "reasons": reasons,
    }
