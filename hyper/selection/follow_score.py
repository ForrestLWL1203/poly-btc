"""Copy-follow score used to rank the final watchlist.

`profile.score` remains the raw profile quality score. This module blends it with
copy-backtest evidence so the observer follows wallets that are actually copyable
under our own sizing/add/stop rules.
"""

from __future__ import annotations

import math
from typing import Mapping

from hyper import config
from hyper.copy.copy_policy import load_copy_policy, one_sided_wilson_lower_bound
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


def _evaluate_follow_eligibility_legacy(
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
) -> dict:
    """Classify one wallet as Core-eligible, Challenger-quality, or rejected.

    ``eligible`` means the wallet is good enough to remain in the discovery/Challenger pool.
    ``coreEligible`` is the stricter individual admission signal consumed by the portfolio selector.
    This split keeps thin-but-promising wallets visible without letting weak wallets originate opens.
    """
    policy = load_copy_policy()
    min_closed30 = policy.min_closed_30d if min_closed30 is None else int(min_closed30)
    min_closed14 = policy.min_closed_14d if min_closed14 is None else int(min_closed14)
    min_closed7 = policy.min_closed_7d if min_closed7 is None else int(min_closed7)
    min_open_fill_rate = policy.min_actionable_open_rate if min_open_fill_rate is None else float(min_open_fill_rate)
    min_expected_return = (
        policy.min_expected_margin_return if min_expected_return is None else float(min_expected_return)
    )
    min_evidence_days = min(5, min_closed30) if min_evidence_days is None else int(min_evidence_days)
    source_metrics = metrics
    metrics = apply_allowed_sector_copy_metrics(metrics)
    c30 = int(_num(metrics.get("copy_bt_closed_n")))
    c14 = int(_num(metrics.get("copy_bt_14d_closed_n")))
    c7 = int(_num(metrics.get("copy_bt_7d_closed_n")))
    expected_return = metrics.get("copy_expected_return")
    return_lcb = metrics.get("copy_return_lcb")
    positive_probability = metrics.get("copy_positive_probability")
    evidence_days = int(_num(metrics.get("copy_evidence_days")))
    recent14 = metrics.get("copy_recent_return_14d")
    recent7 = metrics.get("copy_recent_return_7d")
    # Canonical ``copy_net_pnl`` is already the realized+marked endpoint.  ``unrealized_pnl`` is retained
    # only to split the public display into 已平/持仓; adding it again here double-counts every open winner or
    # loser and can flip qualification at the boundary.
    pnl30 = _num(metrics.get("copy_bt_net_pnl"))
    pnl14 = _num(metrics.get("copy_bt_14d_net_pnl"))
    pnl7 = _num(metrics.get("copy_bt_7d_net_pnl"))
    initial_balance = max(1.0, float(getattr(config, "INITIAL_BALANCE", 10_000.0)))
    if margin_equity_pct is None:
        margin_equity_pct = metrics.get("margin_equity_pct", config.MARGIN_EQUITY_PCT)
    margin_equity_pct = max(0.0, min(1.0, _num(margin_equity_pct, config.MARGIN_EQUITY_PCT)))
    # Canonical replay records the actual capital basis it used.  Prefer that value so the same percentages
    # scale with a future $20k/$30k live account as well as today's $10k Paper account.  Legacy rows fall
    # back to the configured account equity times the manual margin-equity percentage.
    qualification_equity = max(
        1.0,
        _num(metrics.get("initial_margin_equity"), initial_balance * margin_equity_pct),
    )
    challenger_floor = qualification_equity * policy.challenger_min_return_30d
    challenger_weekly_floor = qualification_equity * policy.challenger_min_return_7d
    core_floor = qualification_equity * policy.core_min_return_30d
    weekly_core_floor = qualification_equity * policy.core_min_return_7d
    strong_floor = qualification_equity * policy.strong_core_return_30d
    data_status = str(metrics.get("copy_bt_data_status") or "").strip().lower()
    evidence_status = str(metrics.get("copy_bt_evidence_status") or "").strip().lower()
    economic_disqualification_hint = evidence_status == "economically_disqualified"
    valuation_status = str(metrics.get("copy_bt_valuation_status") or "complete").strip().lower()
    if data_status and data_status not in {"valid", "ok"}:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "copy_data_error",
            "role": "quarantine",
            "deferred": True,
            "reasons": ["copy回放数据无效，禁止进入实跟集合"],
        }
    if evidence_status in {"invalid", "no_evidence", "no_fills", "no_open_events", "economically_disqualified"}:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "no_copy_evidence" if evidence_status != "invalid" else "copy_data_error",
            "role": "rejected" if evidence_status != "invalid" else "quarantine",
            "deferred": evidence_status == "invalid",
            "reasons": ["缺少有效copy回测证据，资格阶段排除"],
        }
    if not _has_copy_evidence(metrics, c30, c14, c7):
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "normalized_evidence_missing",
            "role": "rejected",
            "reasons": ["缺少保证金归一化的非重叠copy证据"],
        }
    policy_json = parse_json_obj(source_metrics.get("sector_policy_json"))
    if economic_disqualification_hint and not policy_json:
        return {
            "eligible": False, "coreEligible": False, "status": "economically_disqualified",
            "role": "rejected", "deferred": False,
            "reasons": ["严格Copy经济证据未通过，且旧记录缺少可细分的板块失败标签"],
        }
    allowed = set(policy_json.get("allowed") or ())
    watched = set(policy_json.get("watch") or ())
    structural_core_blocked = bool(policy_json.get("coreBlocked"))
    if "allowed" in policy_json and not allowed and not watched:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "no_allowed_sector",
            "role": "rejected",
            "reasons": ["当轮没有同时通过结构与严格Copy验证的专精板块"],
        }
    policy_hard_recent = any(
        isinstance(policy_json.get(sector), dict)
        and isinstance(policy_json[sector].get("recent"), dict)
        and bool(policy_json[sector]["recent"].get("hard"))
        for sector in allowed
    )
    recent_loss_ratio = abs(min(0.0, pnl7)) / max(1.0, pnl30)
    sustained_recent_loss = c14 >= min_closed14 and c7 >= min_closed7 and pnl14 <= 0.0 and pnl7 <= 0.0
    severe_recent_loss = (
        c7 >= min_closed7 and pnl7 < 0.0
        and recent_loss_ratio >= policy.recent_hard_loss_ratio
    ) or sustained_recent_loss or policy_hard_recent
    if severe_recent_loss:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "recent_copy_collapse",
            "role": "rejected",
            "recentLossRatio": recent_loss_ratio,
            "reasons": ["近期严格Copy收益相对30天优势严重恶化"],
        }
    if pnl30 < challenger_floor:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "copy_value_below_challenger_floor",
            "role": "rejected",
            "reasons": [
                f"30天严格Copy收益{pnl30:+.0f}低于候选价值线{challenger_floor:.0f}"
            ],
        }
    if c7 >= min_closed7 and pnl7 < challenger_weekly_floor:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "copy_recent_value_below_challenger_floor",
            "role": "rejected",
            "reasons": [
                f"7天严格Copy总收益{pnl7:+.0f}低于候选价值线{challenger_weekly_floor:.0f}"
            ],
        }
    thin_edge = _num(expected_return) < min_expected_return
    execution = metrics.get("execution_score")
    open_fill_rate = metrics.get("actionable_open_rate", metrics.get("copy_bt_open_fill_rate"))
    capacity = metrics.get("capacity_fit")
    if open_fill_rate is not None and _num(open_fill_rate, 1.0) < min_open_fill_rate:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "low_fill_rate",
            "role": "rejected",
            "reasons": [f"开仓跟随率低于{min_open_fill_rate * 100:.0f}%"],
        }
    if capacity is not None and _num(capacity) < policy.min_capacity_fit:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "capacity_fit_low",
            "role": "rejected",
            "reasons": [f"资金容量适配率低于{policy.min_capacity_fit * 100:.0f}%"],
        }
    closed_by_window = {30: c30, 14: c14, 7: c7}
    campaign_by_window = {
        30: int(_num(metrics.get("copy_bt_campaign_closed_n", c30))),
        14: int(_num(metrics.get("copy_bt_14d_campaign_closed_n", c14))),
        7: int(_num(metrics.get("copy_bt_7d_campaign_closed_n", c7))),
    }
    win_rate_by_window = {
        30: _num(metrics.get("copy_bt_win_rate")),
        14: _num(metrics.get("copy_bt_14d_win_rate")),
        7: _num(metrics.get("copy_bt_7d_win_rate")),
    }
    wins_by_window = {}
    for days in (30, 14, 7):
        explicit_key = "copy_bt_campaign_wins" if days == 30 else f"copy_bt_{days}d_campaign_wins"
        wins_by_window[days] = int(_num(
            metrics.get(explicit_key), round(win_rate_by_window[days] * campaign_by_window[days])
        ))
        wins_by_window[days] = max(0, min(campaign_by_window[days], wins_by_window[days]))
    sampled_win_failures = [
        days for days in (30, 14, 7)
        if closed_by_window[days] >= policy.core_min_closed(days)
        and campaign_by_window[days] >= policy.core_min_campaigns(days)
        and win_rate_by_window[days] < policy.core_min_win_rate(days)
    ]
    win_lcb30 = one_sided_wilson_lower_bound(
        wins_by_window[30], campaign_by_window[30], policy.core_win_rate_lcb_confidence,
    )
    if sampled_win_failures:
        labels = "/".join(f"{days}日" for days in sampled_win_failures)
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "copy_win_rate_below_floor",
            "role": "rejected",
            "winRates": win_rate_by_window,
            "winRateLcb30": win_lcb30,
            "reasons": [f"允许板块{labels}严格Copy胜率低于实跟硬门槛"],
        }
    if (c30 >= policy.core_min_closed_30d
            and campaign_by_window[30] >= policy.core_min_campaigns_30d
            and win_lcb30 < policy.core_min_win_rate_lcb_30d):
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "copy_win_rate_confidence_low",
            "role": "rejected",
            "winRates": win_rate_by_window,
            "winRateLcb30": win_lcb30,
            "reasons": [
                f"30日严格Copy胜率{policy.core_win_rate_lcb_confidence * 100:.0f}%单侧置信下界"
                f"低于{policy.core_min_win_rate_lcb_30d * 100:.0f}%"
            ],
        }
    core_samples = all(
        closed_by_window[days] >= policy.core_min_closed(days)
        and campaign_by_window[days] >= policy.core_min_campaigns(days)
        for days in (30, 14, 7)
    )
    standard_samples = bool(
        c30 >= min_closed30 and evidence_days >= min_evidence_days and c7 >= min_closed7
        and core_samples
    )
    recent_warning = (
        (
            c7 >= min_closed7 and pnl7 < 0.0
            and recent_loss_ratio >= policy.recent_warning_loss_ratio
        )
        or (c14 >= min_closed14 and pnl14 <= 0.0)
    )
    strong_samples = c30 >= policy.strong_min_closed_30d and evidence_days >= policy.strong_min_evidence_days
    structure_sampled = c30 >= min_closed30
    profit_factor = metrics.get("copy_bt_profit_factor")
    tail30 = metrics.get("copy_bt_net_after_top2")
    campaign_tail30 = metrics.get("copy_bt_campaign_net_after_top2")
    tail7 = metrics.get("copy_bt_7d_net_after_top1")
    cost_stress = metrics.get("copy_bt_cost_stress_net_pnl")
    positive_episodes = int(_num(metrics.get("copy_bt_positive_episode_n")))
    top1_share = _num(metrics.get("copy_bt_top1_profit_share"))
    top3_share = _num(metrics.get("copy_bt_top3_profit_share"))
    body_n = int(_num(metrics.get("copy_bt_body_after_top3_n")))
    body_win_rate = _num(metrics.get("copy_bt_body_after_top3_win_rate"))
    body_net = _num(metrics.get("copy_bt_body_after_top3_net_pnl"))
    body_profit_factor = _num(metrics.get("copy_bt_body_after_top3_profit_factor"))
    body_median = _num(metrics.get("copy_bt_body_after_top3_median_pnl"))
    recent_body_floor = policy.core_recent_body_min_closed
    recent_body_negative = bool(
        int(_num(metrics.get("copy_bt_14d_body_after_top3_n"))) >= recent_body_floor
        and int(_num(metrics.get("copy_bt_7d_body_after_top3_n"))) >= recent_body_floor
        and _num(metrics.get("copy_bt_14d_body_after_top3_net_pnl")) < 0.0
        and _num(metrics.get("copy_bt_7d_body_after_top3_net_pnl")) < 0.0
    )
    final_liquidations = int(_num(metrics.get("copy_bt_liquidations")))
    liquidation_limit_exceeded = final_liquidations > policy.core_max_liquidations_30d
    forward_liquidations = int(_num(metrics.get("forward_liquidations")))
    forward_net_raw = metrics.get("forward_net_pnl")
    forward_net_pnl = _num(forward_net_raw)
    forward_loss_limit = float(config.INITIAL_BALANCE) * float(config.WALLET_FORWARD_LOSS_FREEZE_PCT)
    forward_loss_exceeded = bool(
        forward_net_raw is not None and forward_loss_limit > 0 and forward_net_pnl <= -forward_loss_limit
    )
    forward_risk_exceeded = forward_liquidations > 0 or forward_loss_exceeded
    concentration_sampled = positive_episodes >= policy.concentration_min_positive_episodes
    concentration_warning = bool(
        concentration_sampled
        and (
            top1_share > policy.max_top1_profit_share
            or top3_share > policy.max_top3_profit_share
        )
    )
    concentration_body_sampled = body_n >= policy.concentration_body_min_episodes
    concentration_body_strong = bool(
        concentration_body_sampled
        and body_win_rate >= policy.concentration_body_min_win_rate
        and body_net > 0.0
        and body_profit_factor >= policy.concentration_body_min_profit_factor
        and body_median > 0.0
    )
    if structure_sampled and profit_factor is not None and _num(profit_factor) < policy.min_profit_factor:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "copy_profit_structure_weak",
            "role": "rejected",
            "profitFactor": _num(profit_factor),
            "reasons": [f"扣费后严格Copy PF低于{policy.min_profit_factor:.2f}"],
        }
    tail_floor = qualification_equity * policy.min_tail_return_30d
    if structure_sampled and tail30 is not None and _num(tail30) < tail_floor:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "copy_tail_profit_weak",
            "role": "rejected",
            "tail30NetPnl": _num(tail30),
            "reasons": [f"30天移除最大两笔盈利后低于{policy.min_tail_return_30d * 100:.0f}%收益线"],
        }
    if (campaign_by_window[30] >= policy.core_min_campaigns_30d
            and campaign_tail30 is not None and _num(campaign_tail30) <= 0.0):
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "copy_campaign_tail_weak",
            "role": "rejected",
            "campaigns": campaign_by_window,
            "reasons": ["合并相关篮子仓后，30天收益依赖最大的两个独立方向批次"],
        }
    if c7 >= min_closed7 and tail7 is not None and _num(tail7) <= 0.0:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "copy_recent_tail_weak",
            "role": "rejected",
            "tail7NetPnl": _num(tail7),
            "reasons": ["7天移除最大一笔盈利后不再为正"],
        }
    if structure_sampled and cost_stress is not None and _num(cost_stress) <= 0.0:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "copy_cost_stress_weak",
            "role": "rejected",
            "costStressNetPnl": _num(cost_stress),
            "reasons": ["1.5倍手续费/滑点压力后严格Copy不盈利"],
        }
    if concentration_warning and concentration_body_sampled and not concentration_body_strong:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "copy_profit_concentration_body_weak",
            "role": "rejected",
            "profitConcentrationWarning": True,
            "bodyAfterTop3": {
                "episodes": body_n, "winRate": body_win_rate,
                "netPnl": body_net, "profitFactor": body_profit_factor,
                "medianPnl": body_median,
            },
            "reasons": [
                "利润集中且移除前三大盈利后，剩余交易主体不是稳定盈利结构"
            ],
        }
    concentration_exception = bool(
        concentration_warning
        and concentration_body_strong
        and tail30 is not None and _num(tail30) >= tail_floor
        and (c7 < min_closed7 or (tail7 is not None and _num(tail7) > 0.0))
        and cost_stress is not None and _num(cost_stress) > 0.0
    )
    weekly_economics_ok = pnl7 >= weekly_core_floor
    # A narrow miss of the 2% normalized edge line may remain visible as Challenger when the strict Copy
    # account result, recent economics, samples and win probability are all strong.  Materially thin or
    # negative expected edge remains a real rejection: a large historical dollar result alone must not
    # override an unstable/negative episode distribution.
    challenger_edge_floor = max(0.0, min_expected_return * 0.75)
    thin_edge_watch = bool(
        thin_edge
        and _num(expected_return) >= challenger_edge_floor
        and pnl30 >= core_floor
        and weekly_economics_ok
        and standard_samples
    )
    if thin_edge and not thin_edge_watch:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "thin_copy_edge",
            "role": "rejected",
            "reasons": [
                f"保证金归一化预期收益低于候选观察线{challenger_edge_floor * 100:.1f}%"
            ],
        }
    # Core never uses a small-sample win-rate exception.  High ROI/PnL may keep a thin wallet visible as a
    # Challenger, but new opens require the complete 15/7/5 strict-Copy evidence surface.
    sparse_recent_strong = False
    strong_samples = bool(strong_samples and core_samples)
    strong_entry = (
        pnl30 >= strong_floor and (strong_samples or sparse_recent_strong) and not recent_warning
        and weekly_economics_ok and valuation_status == "complete" and not thin_edge
    )
    standard_entry = (
        pnl30 >= core_floor and standard_samples and not recent_warning
        and weekly_economics_ok and valuation_status == "complete" and not thin_edge
    )
    # ``watch`` sectors are evidence-only and cannot become executable merely because their joint wallet
    # aggregate crosses the return line.  Formation may replay them to discover a safe parameter surface,
    # but the final-parameter sector gate must promote at least one sector into ``allowed`` first.
    live_sector_ready = bool(allowed) if "allowed" in policy_json else True
    core_eligible = bool((strong_entry or standard_entry) and live_sector_ready)
    if concentration_warning and not concentration_exception:
        core_eligible = False
    if recent_body_negative or liquidation_limit_exceeded or forward_risk_exceeded:
        core_eligible = False
    if core_eligible and not structural_core_blocked:
        return {
            "eligible": True,
            "coreEligible": True,
            "strongEntry": bool(strong_entry),
            "sparseRecentEntry": bool(sparse_recent_strong),
            "status": (
                "core_eligible_profit_concentrated_body_strong"
                if concentration_warning else
                ("core_eligible_strong" if strong_entry else "core_eligible")
            ),
            "role": "core_eligible",
            "returnLcb": return_lcb,
            "executionScore": execution,
            "recentLossRatio": recent_loss_ratio,
            "profitConcentrationWarning": concentration_warning,
            "bodyAfterTop3": {
                "episodes": body_n, "winRate": body_win_rate,
                "netPnl": body_net, "profitFactor": body_profit_factor,
                "medianPnl": body_median,
            },
            "reasons": [
                "个人严格Copy证据达到Core准入线"
                + ("，利润集中但其余交易主体仍稳定盈利" if concentration_warning else "")
                + ("，近期3–4笔强证据路径通过" if sparse_recent_strong else "")
            ],
        }

    if forward_liquidations > 0:
        status, reason = (
            "challenger_forward_liquidation",
            f"真实跟单近30日已发生{forward_liquidations}次爆仓，停止该钱包新开仓",
        )
    elif forward_loss_exceeded:
        status, reason = (
            "challenger_forward_loss_freeze",
            f"真实跟单净亏{forward_net_pnl:+.0f}已触发单钱包亏损熔断",
        )
    elif liquidation_limit_exceeded:
        status, reason = (
            "challenger_liquidation_limit",
            f"最终参数30日严格回放爆仓{final_liquidations}次，超过Core上限"
            f"{policy.core_max_liquidations_30d}次",
        )
    elif recent_body_negative:
        status, reason = (
            "challenger_recent_body_negative",
            "7日与14日移除前三大盈利后的交易主体持续为负，暂不允许新开仓",
        )
    elif structural_core_blocked:
        status, reason = (
            "challenger_structural_watch",
            "单次Heavy-DCA受限回放通过，但结构压力验证只允许候选观察",
        )
    elif valuation_status != "complete":
        status, reason = "challenger_open_valuation_pending", "开放仓位缺少可靠末端估值，暂不进入Core"
    elif recent_warning:
        status, reason = "challenger_recent_decline", "近期回撤尚未达到硬淘汰线"
    elif thin_edge_watch:
        status, reason = (
            "challenger_thin_edge_watch",
            f"保证金归一化预期收益低于{min_expected_return * 100:.1f}%Core线，但总收益与样本仍合格",
        )
    elif pnl30 < core_floor:
        status, reason = "challenger_return_watch", "30天Copy收益达到候选线但未达到Core线"
    elif concentration_warning and not concentration_body_sampled:
        status, reason = (
            "challenger_profit_concentration_sample",
            "利润集中，移除前三大盈利后的剩余样本不足，暂留观察",
        )
    elif not core_samples or evidence_days < min_evidence_days:
        status, reason = "challenger_sample_watch", "Core所需15/7/5 Copy样本或独立证据尚不足"
    elif concentration_warning:
        status, reason = "challenger_profit_concentration", "利润集中度偏高，暂不进入Core"
    elif pnl7 < weekly_core_floor:
        status, reason = (
            "challenger_weekly_return_watch",
            f"7天Copy经济收益{pnl7:+.0f}低于Core周收益线{weekly_core_floor:.0f}",
        )
    else:
        # All economic/sample/valuation/structure paths above are explicit.  Keep a fail-closed business
        # label for unforeseen policy combinations, but never call it a confidence failure.
        status, reason = "challenger_policy_watch", "其他Core业务条件尚未满足"
    return {
        "eligible": True,
        "coreEligible": False,
        "status": status,
        "role": "challenger",
        "returnLcb": return_lcb,
        "executionScore": execution,
        "recentLossRatio": recent_loss_ratio,
        "profitConcentrationWarning": concentration_warning,
        "bodyAfterTop3": {
            "episodes": body_n, "winRate": body_win_rate,
            "netPnl": body_net, "profitFactor": body_profit_factor,
            "medianPnl": body_median,
        },
        "reasons": [reason],
    }


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
) -> dict:
    """Return the strict-Copy economic role for one wallet.

    Research is deliberately not executable. Challenger is the minimum economic evidence surface. Core uses
    entry or retention thresholds, while hard risk failures always override the two-scan soft-failure grace.
    Missing optional legacy metrics do not manufacture a pass: path-risk data must be explicitly complete for
    a new Core once a row advertises the path-risk schema.
    """
    del min_pnl_per_closed  # Kept for API compatibility; dollar-per-trade is not an economic gate.
    policy = load_copy_policy(policy_values)
    min_closed30 = policy.min_closed_30d if min_closed30 is None else int(min_closed30)
    min_closed14 = policy.min_closed_14d if min_closed14 is None else int(min_closed14)
    min_closed7 = policy.min_closed_7d if min_closed7 is None else int(min_closed7)
    min_open_fill_rate = (
        policy.min_actionable_open_rate if min_open_fill_rate is None else float(min_open_fill_rate)
    )
    min_expected_return = (
        policy.min_expected_margin_return if min_expected_return is None else float(min_expected_return)
    )
    min_evidence_days = min(5, min_closed30) if min_evidence_days is None else int(min_evidence_days)

    source_metrics = metrics
    metrics = apply_allowed_sector_copy_metrics(metrics)
    c30 = int(_num(metrics.get("copy_bt_closed_n")))
    c14 = int(_num(metrics.get("copy_bt_14d_closed_n")))
    c7 = int(_num(metrics.get("copy_bt_7d_closed_n")))
    closed = {30: c30, 14: c14, 7: c7}
    pnl = {
        30: _num(metrics.get("copy_bt_net_pnl")),
        14: _num(metrics.get("copy_bt_14d_net_pnl")),
        7: _num(metrics.get("copy_bt_7d_net_pnl")),
    }
    evidence_days = int(_num(metrics.get("copy_evidence_days")))
    expected_return = metrics.get("copy_expected_return")
    return_lcb = metrics.get("copy_return_lcb")
    execution = metrics.get("execution_score")
    valuation_status = str(metrics.get("copy_bt_valuation_status") or "complete").strip().lower()
    data_status = str(metrics.get("copy_bt_data_status") or "").strip().lower()
    evidence_status = str(metrics.get("copy_bt_evidence_status") or "").strip().lower()
    economic_disqualification_hint = evidence_status == "economically_disqualified"

    if data_status and data_status not in {"valid", "ok"}:
        return {
            "eligible": False, "coreEligible": False, "status": "copy_data_error",
            "role": "quarantine", "deferred": True,
            "reasons": ["copy回放数据无效，禁止进入实跟集合"],
        }
    if evidence_status == "invalid":
        return {
            "eligible": False, "coreEligible": False, "status": "copy_data_error",
            "role": "quarantine", "deferred": True,
            "reasons": ["copy回放证据无效，等待重新生成"],
        }
    if evidence_status in {"no_evidence", "no_fills", "no_open_events"}:
        return {
            "eligible": False, "coreEligible": False, "status": "no_copy_evidence",
            "role": "rejected", "deferred": False,
            "reasons": ["缺少有效copy回测证据，资格阶段排除"],
        }
    if not _has_copy_evidence(metrics, c30, c14, c7):
        return {
            "eligible": False, "coreEligible": False, "status": "normalized_evidence_missing",
            "role": "rejected", "reasons": ["缺少保证金归一化的非重叠copy证据"],
        }

    policy_json = parse_json_obj(source_metrics.get("sector_policy_json"))
    if economic_disqualification_hint and not policy_json:
        return {
            "eligible": False, "coreEligible": False, "status": "economically_disqualified",
            "role": "rejected", "deferred": False,
            "reasons": ["严格Copy经济证据未通过，且旧记录缺少可细分的板块失败标签"],
        }
    allowed = set(policy_json.get("allowed") or ())
    watched = set(policy_json.get("watch") or ())
    structural_core_blocked = bool(policy_json.get("coreBlocked"))
    if "allowed" in policy_json and not allowed and not watched:
        return {
            "eligible": False, "coreEligible": False, "status": "no_allowed_sector",
            "role": "rejected", "reasons": ["当轮没有同时通过结构与严格Copy验证的专精板块"],
        }

    initial_balance = max(1.0, float(getattr(config, "INITIAL_BALANCE", 10_000.0)))
    if margin_equity_pct is None:
        margin_equity_pct = metrics.get("margin_equity_pct", config.MARGIN_EQUITY_PCT)
    margin_equity_pct = max(0.0, min(1.0, _num(margin_equity_pct, config.MARGIN_EQUITY_PCT)))
    qualification_equity = max(1.0, _num(metrics.get("initial_margin_equity"), initial_balance))
    returns = {days: pnl[days] / qualification_equity for days in (30, 14, 7)}
    campaign = {
        30: int(_num(metrics.get("copy_bt_campaign_closed_n"), c30)),
        14: int(_num(metrics.get("copy_bt_14d_campaign_closed_n"), c14)),
        7: int(_num(metrics.get("copy_bt_7d_campaign_closed_n"), c7)),
    }
    position_win_rate = {
        30: _num(metrics.get("copy_bt_win_rate")),
        14: _num(metrics.get("copy_bt_14d_win_rate")),
        7: _num(metrics.get("copy_bt_7d_win_rate")),
    }
    wins = {}
    win_rate = {}
    for days in (30, 14, 7):
        explicit = "copy_bt_campaign_wins" if days == 30 else f"copy_bt_{days}d_campaign_wins"
        wins[days] = int(_num(metrics.get(explicit), round(position_win_rate[days] * campaign[days])))
        wins[days] = max(0, min(campaign[days], wins[days]))
        win_rate[days] = wins[days] / campaign[days] if campaign[days] else 0.0
    win_lcb30 = one_sided_wilson_lower_bound(
        wins[30], campaign[30], policy.core_win_rate_lcb_confidence,
    )

    policy_hard_recent = any(
        isinstance(policy_json.get(sector), dict)
        and isinstance(policy_json[sector].get("recent"), dict)
        and bool(policy_json[sector]["recent"].get("hard"))
        for sector in allowed
    )
    recent_hard_collapse = bool(
        policy_hard_recent
        or (campaign[7] >= 5 and win_rate[7] < policy.core_min_win_rate_7d and pnl[7] < 0.0)
    )
    if recent_hard_collapse:
        return {
            "eligible": False, "coreEligible": False, "status": "recent_copy_collapse",
            "role": "rejected", "hardRisk": True, "winRates": win_rate,
            "reasons": ["7日已有至少5个独立Campaign且胜率低于40%、净收益为负，判定近期硬崩塌"],
        }

    # Historical/open path-risk evidence. Negative current loss values are accepted for backwards-compatible
    # storage; normalize them to a positive drawdown fraction here.
    path_status = str(metrics.get("copy_path_risk_status") or "").strip().lower()
    intratrade_dd = max(0.0, _num(metrics.get("copy_intratrade_max_drawdown")))
    failed_deep = int(_num(metrics.get("copy_failed_deep_bag_n")))
    deep_events = int(_num(metrics.get("copy_deep_bag_event_n")))
    recovery_rate = _num(metrics.get("copy_deep_bag_recovery_rate"), 1.0)
    max_deep_bag_hours = max(0.0, _num(metrics.get("copy_max_deep_bag_hours")))
    current_loss = abs(min(0.0, _num(metrics.get("copy_current_open_loss_frac"))))
    current_loss = max(current_loss, max(0.0, _num(metrics.get("copy_current_drawdown_frac"))))
    current_bag_hours = max(0.0, _num(metrics.get("copy_current_bag_hours")))
    current_deep_risk = bool(
        current_loss >= policy.deep_bag_event_pct
        or (current_loss >= 0.05 and current_bag_hours >= policy.deep_bag_long_hours)
    )
    deep_reject = bool(
        intratrade_dd > policy.intratrade_dd_reject
        or failed_deep > policy.deep_bag_max_failed
        or (deep_events >= 2 and recovery_rate < policy.deep_bag_min_recovery_rate)
    )
    if current_deep_risk:
        return {
            "eligible": False, "coreEligible": False, "status": "current_deep_loss_freeze",
            "role": "exit_only", "hardRisk": True,
            "reasons": ["当前严格Copy浮亏已触发-8%或-5%持续24小时硬风险线，仅允许退出"],
        }
    if deep_reject:
        return {
            "eligible": False, "coreEligible": False, "status": "historical_deep_loss_reject",
            "role": "rejected", "hardRisk": True,
            "intratradeDrawdown": intratrade_dd, "failedDeepBagEvents": failed_deep,
            "deepBagRecoveryRate": recovery_rate,
            "reasons": ["历史盘中深亏超过15%、失败事件过多或多次深亏恢复率不足"],
        }
    # Research is intentionally non-executable: positive strict-Copy economics are visible without becoming
    # a source of live orders. A zero or losing 30-day replay is a real economic rejection.
    if pnl[30] <= 0.0:
        return {
            "eligible": False, "coreEligible": False, "status": "copy_not_profitable",
            "role": "rejected", "returns": returns,
            "reasons": ["30天严格Copy净收益不为正"],
        }
    if returns[30] < policy.challenger_min_return_30d:
        return {
            "eligible": False, "coreEligible": False, "status": "research_copy_positive",
            "role": "research", "researchEligible": True, "returns": returns,
            "reasons": [
                f"30天严格Copy盈利但收益率低于{policy.challenger_min_return_30d * 100:.0f}% Challenger线"
            ],
        }

    challenger_samples = bool(
        c30 >= min_closed30 and campaign[30] >= 5 and evidence_days >= min_evidence_days
    )
    if not challenger_samples:
        return {
            "eligible": False, "coreEligible": False, "status": "research_insufficient_evidence",
            "role": "research", "researchEligible": True, "returns": returns,
            "campaigns": campaign,
            "reasons": ["收益已达Challenger线，但不足7个已平回合、5个Campaign或5个证据日"],
        }

    profit_factor = metrics.get("copy_bt_profit_factor")
    tail30 = metrics.get("copy_bt_net_after_top2")
    campaign_tail30 = metrics.get("copy_bt_campaign_net_after_top2")
    cost_stress = metrics.get("copy_bt_cost_stress_net_pnl")
    final_liquidations = int(_num(metrics.get("copy_bt_liquidations")))
    repeated_liquidation = final_liquidations > policy.core_max_liquidations_30d
    if campaign_tail30 is None or cost_stress is None:
        return {
            "eligible": False, "coreEligible": False, "status": "research_stress_evidence_missing",
            "role": "research", "researchEligible": True, "returns": returns,
            "reasons": ["缺少Campaign去极值或1.5倍成本压力证据，不得进入可执行角色"],
        }
    if _num(campaign_tail30) <= 0.0:
        return {
            "eligible": False, "coreEligible": False, "status": "copy_campaign_tail_weak",
            "role": "rejected", "hardRisk": True,
            "reasons": ["移除最大两个独立Campaign盈利后严格Copy不再盈利"],
        }
    if _num(cost_stress) <= 0.0:
        return {
            "eligible": False, "coreEligible": False, "status": "copy_cost_stress_weak",
            "role": "rejected", "hardRisk": True,
            "reasons": ["1.5倍手续费/滑点压力后严格Copy不盈利"],
        }
    if repeated_liquidation:
        return {
            "eligible": False, "coreEligible": False, "status": "repeated_copy_liquidation",
            "role": "rejected", "hardRisk": True,
            "reasons": [f"30日严格回放爆仓{final_liquidations}次，超过允许的一次孤立爆仓"],
        }

    open_fill_rate = metrics.get("actionable_open_rate", metrics.get("copy_bt_open_fill_rate"))
    capacity = metrics.get("capacity_fit")
    execution_ok = open_fill_rate is None or _num(open_fill_rate, 1.0) >= min_open_fill_rate
    capacity_ok = capacity is None or _num(capacity) >= policy.min_capacity_fit
    thin_edge = expected_return is not None and _num(expected_return) < min_expected_return
    core_return_floor = policy.retention_min_return_30d if retention else policy.core_min_return_30d
    recent_return_ok = returns[7] >= policy.core_min_return_7d
    core_win_floor = policy.retention_min_win_rate_30d if retention else policy.core_min_win_rate_30d
    core_lcb_floor = (
        policy.retention_min_win_rate_lcb_30d if retention else policy.core_min_win_rate_lcb_30d
    )
    core_samples = bool(
        c30 >= policy.core_min_closed_30d
        and c14 >= policy.core_min_closed_14d
        and c7 >= policy.core_min_closed_7d
        and campaign[30] >= policy.core_min_campaigns_30d
    )
    win30_ok = win_rate[30] >= core_win_floor and win_lcb30 >= core_lcb_floor
    recent14_ok = bool(
        campaign[14] < policy.core_min_campaigns_14d
        or (win_rate[14] >= policy.core_min_win_rate_14d and pnl[14] > 0.0)
    )
    pf_ok = profit_factor is not None and _num(profit_factor) >= policy.min_profit_factor
    tail_ok = _num(campaign_tail30) >= qualification_equity * policy.min_tail_return_30d
    path_complete = path_status not in {"pending", "missing", "invalid", "replay_error", "incomplete"}
    path_core_ok = path_complete and intratrade_dd <= policy.intratrade_dd_core_max
    long_deep_bag = deep_events > 0 and max_deep_bag_hours >= policy.deep_bag_long_hours
    path_core_ok = path_core_ok and not long_deep_bag
    forward_liquidations = int(_num(metrics.get("forward_liquidations")))
    # Cumulative forward PnL is deliberately not an eligibility gate. A plain daily net-PnL threshold would
    # recreate the old churn where a source is removed before its later winning Campaign can be copied.
    forward_risk = bool(forward_liquidations > 0)
    live_sector_ready = bool(allowed) if "allowed" in policy_json else True
    # July's stricter 12/5/3 + ten-Campaign surface accidentally removed the existing high-confidence
    # sparse route altogether.  That made a 9/10 winner with >20% strict-Copy return observation-only even
    # when its Wilson bound, tail, cost stress, execution and path risk were all already safe.  Restore a
    # deliberately narrow alternative: it can waive only the ordinary sample shape, never an economic,
    # repeatability, execution, liquidation or deep-loss gate.
    strong_sparse_entry = bool(
        not retention
        and returns[30] >= policy.strong_core_return_30d
        and c30 >= policy.strong_sparse_min_closed_30d
        and evidence_days >= policy.strong_sparse_min_evidence_days
        and c7 >= policy.strong_sparse_min_closed_7d
        and campaign[30] >= min(
            policy.core_min_campaigns_30d, policy.strong_sparse_min_closed_30d,
        )
        and win_rate[30] >= max(policy.core_min_win_rate_30d, 0.75)
        and win_lcb30 >= policy.core_min_win_rate_lcb_30d
        and win_rate[7] >= policy.strong_sparse_min_win_rate_7d
    )
    core_eligible = bool(
        returns[30] >= core_return_floor
        and recent_return_ok
        and (core_samples or strong_sparse_entry)
        and win30_ok
        and recent14_ok
        and pf_ok
        and tail_ok
        and execution_ok
        and capacity_ok
        and valuation_status == "complete"
        and not structural_core_blocked
        and not thin_edge
        and path_core_ok
        and not forward_risk
        and live_sector_ready
    )
    strong_entry = bool(core_eligible and not retention and returns[30] >= policy.strong_core_return_30d)
    base_detail = {
        "returnLcb": return_lcb,
        "executionScore": execution,
        "returns": returns,
        "campaigns": campaign,
        "winRates": win_rate,
        "winRateLcb30": win_lcb30,
        "intratradeDrawdown": intratrade_dd,
        "profitFactor": _num(profit_factor) if profit_factor is not None else None,
        "campaignNetAfterTop2": _num(campaign_tail30),
        "costStressNetPnl": _num(cost_stress),
        "strongSparseEntry": strong_sparse_entry,
        "retentionSurface": bool(retention),
        "softFailConfirmationsRequired": policy.soft_fail_confirmations,
    }
    if core_eligible:
        return {
            "eligible": True, "coreEligible": True, "strongEntry": strong_entry,
            "status": "core_retention_eligible" if retention else (
                "core_eligible_strong" if strong_entry else "core_eligible"
            ),
            "role": "core_eligible", **base_detail,
            "reasons": [
                "个人严格Copy证据达到Core保留线" if retention else (
                    "高收益高置信小样本路径达到Core新进入线"
                    if strong_sparse_entry else "个人严格Copy证据达到Core新进入线"
                )
            ],
        }

    if forward_liquidations > 0:
        status, reason = "challenger_forward_liquidation", "真实跟单已发生爆仓，冻结该来源新开仓"
    elif intratrade_dd > policy.intratrade_dd_core_max:
        status, reason = "challenger_intratrade_drawdown", "30日最大盘中回撤超过12%，只允许Challenger"
    elif long_deep_bag:
        status, reason = "challenger_long_deep_bag", "历史8%以上浮亏持续超过24小时，最多只允许Challenger"
    elif not path_complete:
        status, reason = "challenger_path_risk_pending", "价格路径风险尚未完整重建，不得授予新Core权限"
    elif valuation_status != "complete":
        status, reason = "challenger_open_valuation_pending", "开放仓位缺少可靠末端估值，暂不进入Core"
    elif structural_core_blocked:
        status, reason = "challenger_structural_watch", "结构压力回放只允许候选观察"
    elif not execution_ok:
        status, reason = "challenger_execution_watch", f"开仓跟随率低于{min_open_fill_rate * 100:.0f}% Core线"
    elif not capacity_ok:
        status, reason = "challenger_capacity_watch", f"容量适配低于{policy.min_capacity_fit * 100:.0f}% Core线"
    elif thin_edge:
        status, reason = "challenger_thin_edge_watch", "归一化单回合预期边际未达Core质量线"
    elif returns[30] < core_return_floor:
        status, reason = "challenger_return_watch", f"30日收益率未达到{core_return_floor * 100:.0f}% Core线"
    elif not recent_return_ok:
        status, reason = (
            "challenger_weekly_return_watch",
            f"7日收益率未达到{policy.core_min_return_7d * 100:.0f}% Core线",
        )
    elif not core_samples:
        status, reason = "challenger_sample_watch", "Core所需12/5/5已平回合或10个30日Campaign不足"
    elif not win30_ok:
        status, reason = "challenger_win_rate_watch", "30日Campaign胜率或Wilson下界未达到Core线"
    elif not recent14_ok:
        status, reason = "challenger_recent_decline", "14日已有5个Campaign但胜率低于55%或净收益不为正"
    elif not pf_ok:
        status, reason = "challenger_profit_structure_watch", f"严格Copy PF低于{policy.min_profit_factor:.2f}"
    elif not tail_ok:
        status, reason = "challenger_tail_profit_watch", "移除最大两个回合盈利后收益率未达到3%"
    else:
        status, reason = "challenger_policy_watch", "其他Core软条件尚未满足"
    return {
        "eligible": True, "coreEligible": False, "status": status, "role": "challenger",
        **base_detail, "reasons": [reason],
    }


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
