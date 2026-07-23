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
    min_pnl_per_closed=None,
    margin_equity_pct: float | None = None,
    retention: bool = False,
    policy_values: Mapping | None = None,
    as_of_ms: int | None = None,
) -> dict:
    """Classify evidence once: positive wallets remain researchable; Core adds non-duplicated proof.

    The old policy independently gated 7/14/30 returns, win rates, PF, tail-2 and concentration body.  Those
    checks were correlated views of the same trades and created an exclusion cascade.  V3 keeps hard data,
    liquidation and deep-loss safety; profitability under canonical replay retains research eligibility.
    The bounded formation surface may publish that wallet as Challenger; Core additionally requires ten
    campaigns, three non-overlapping 10-day folds, fresh activity, one-winner removal, cost stress and
    executable capacity.
    """
    del min_closed14, min_closed7, min_pnl_per_closed, margin_equity_pct
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
    stability = policy_json.get("stability") if isinstance(policy_json.get("stability"), dict) else {}
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
    stability_sufficient = bool(stability.get("evidenceSufficient"))
    stability_ok = bool(stability.get("passed"))
    sample_ok = bool(
        c30 >= min_closed30 and campaigns >= policy.core_min_campaigns_30d
        and evidence_days >= min_evidence_days
    )
    checks = {
        "copyDataValid": not data_status or data_status in {"valid", "ok"},
        "normalizedEvidencePresent": _has_copy_evidence(scoped, c30, 0, 0),
        "strictCopy30dPositive": pnl30 > 0.0,
        "coreReturn": return30 >= (
            policy.retention_min_return_30d if retention else policy.core_min_return_30d
        ),
        "tenIndependentCampaigns": campaigns >= policy.core_min_campaigns_30d,
        "nonoverlapStability": stability_ok,
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
        "evidenceDays": evidence_days, "stability": stability,
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
        checks["coreReturn"] and sample_ok and stability_ok and activity_ok
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
                "reasons": ["严格Copy、10个Campaign、非重叠稳定性、72小时活动及执行压力均通过"]}

    if not stability_sufficient:
        status, reason = "challenger_stability_evidence_building", "非重叠10日折叠证据不足，继续积累而不按亏损淘汰"
    elif not stability_ok:
        status, reason = "challenger_stability_watch", "非重叠10日折叠尚未达到至少两个盈利区间"
    elif not sample_ok:
        status, reason = "challenger_campaign_evidence_building", "独立Campaign或证据日不足，继续积累"
    elif not activity_ok:
        status, reason = "challenger_activity_watch", "最近72小时没有新的可执行flat→open信号"
    elif not checks["coreReturn"]:
        status, reason = "challenger_return_watch", "严格Copy盈利但未达到Core收益线"
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


def compute_follow_score(metrics: Mapping) -> tuple[float, dict]:
    """Rank copyability, repeatability and account-normalized strict Copy economics."""
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
    policy = load_copy_policy()
    # Qualification already defines seven 30d closes and five independent days as sufficient evidence.
    # Continuing to shrink a qualified wallet toward a neutral 0.5 until 20 closes/10 days silently ranks
    # a five-close +30% week behind a much thinner but older wallet.  Saturate at the actual evidence floors;
    # below them the continuous factor still keeps observation-only wallets appropriately conservative.
    closed_confidence = _clamp(c30 / max(1.0, float(policy.min_closed_30d)))
    day_confidence = _clamp(evidence_days / max(1.0, float(min(5, policy.min_closed_30d))))
    confidence = min(closed_confidence, day_confidence)
    edge_score = _clamp(0.5 + 0.5 * math.tanh(expected / 0.05))
    lcb_score = _clamp(0.5 + 0.5 * math.tanh(lcb / 0.03))
    probability_score = _clamp((probability - 0.5) / 0.5)
    copy_score = (
        0.25 * edge_score + 0.25 * lcb_score + 0.20 * probability_score
        + 0.15 * risk + 0.15 * execution
    )
    shrunk_copy = 0.5 + confidence * (copy_score - 0.5)
    pnl30 = _num(metrics.get("copy_bt_net_pnl"))
    pnl14 = _num(metrics.get("copy_bt_14d_net_pnl"))
    pnl7 = _num(metrics.get("copy_bt_7d_net_pnl"))
    margin_equity_pct = _clamp(_num(metrics.get("margin_equity_pct"), config.MARGIN_EQUITY_PCT))
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
    # proportionally scaled replay PnL therefore receives the same economic score.  Confidence shrinkage
    # prevents a three-trade windfall from outranking a repeatable wallet solely on one large episode.
    economic_score = (
        0.50 * _clamp(returns["30d"] / 0.60)
        + 0.30 * _clamp(returns["14d"] / 0.40)
        + 0.20 * _clamp(returns["7d"] / 0.25)
    )
    shrunk_economics = 0.5 + confidence * (economic_score - 0.5)
    activity = _clamp(_num(metrics.get("open_probability_48h"), 0.0))
    score = 0.10 * raw + 0.40 * shrunk_copy + 0.40 * shrunk_economics + 0.10 * activity
    reasons = [
        f"预期保证金收益{expected * 100:+.1f}%",
        f"LCB {lcb * 100:+.1f}%",
        f"盈利概率{probability * 100:.0f}%",
        f"独立证据{evidence_days}天/{c30}笔",
    ]
    recent14 = metrics.get("copy_recent_return_14d")
    recent7 = metrics.get("copy_recent_return_7d")
    if recent14 is not None and _num(recent14) < 0:
        score -= min(0.08, abs(_num(recent14)) * 0.5)
        reasons.append("14天归一化收益为负")
    if recent7 is not None and _num(recent7) < 0:
        score -= min(0.06, abs(_num(recent7)) * 0.4)
        reasons.append("7天归一化收益为负")
    liqs = int(_num(metrics.get("copy_bt_liquidations")))
    liquidation_rate = liqs / c30 if c30 > 0 else 0.0
    # The monetary loss is already present in PnL/drawdown, so never charge a fixed amount per event or use
    # a zero-liquidation veto.  Frequency is separate repeatability evidence, however: recurring isolated
    # liquidations rank below an equally profitable wallet with a cleaner path.  Keep the continuous penalty
    # bounded so a thick post-loss net edge can still win.
    if liqs > 0:
        liquidation_frequency_penalty = min(0.10, liquidation_rate * 0.50)
        score -= liquidation_frequency_penalty
        reasons.append(
            f"copy爆仓{liqs}次/{c30}回合（损失已计收益，频率扣分{liquidation_frequency_penalty:.3f}）"
        )
    score = _clamp(score)
    return score, {
        "rawScore": raw,
        "copyScore": copy_score,
        "economicScore": economic_score,
        "economicReturns": returns,
        "economicEquity": economic_equity,
        "confidence": confidence,
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
        "profitFactor": metrics.get("copy_bt_profit_factor"),
        "payoffRatio": metrics.get("copy_bt_payoff_ratio"),
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
