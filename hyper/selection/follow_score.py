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
    win_rate_by_window = {
        30: _num(metrics.get("copy_bt_win_rate")),
        14: _num(metrics.get("copy_bt_14d_win_rate")),
        7: _num(metrics.get("copy_bt_7d_win_rate")),
    }
    wins_by_window = {}
    for days in (30, 14, 7):
        # Profile storage persists the canonical rate/count pair, while scoped sector JSON also carries the
        # integer win count.  Derive from the public pair so both paths make the identical boundary decision.
        wins_by_window[days] = int(round(win_rate_by_window[days] * closed_by_window[days]))
        wins_by_window[days] = max(0, min(closed_by_window[days], wins_by_window[days]))
    sampled_win_failures = [
        days for days in (30, 14, 7)
        if closed_by_window[days] >= policy.core_min_closed(days)
        and win_rate_by_window[days] < policy.core_min_win_rate(days)
    ]
    win_lcb30 = one_sided_wilson_lower_bound(
        wins_by_window[30], c30, policy.core_win_rate_lcb_confidence,
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
    if c30 >= policy.core_min_closed_30d and win_lcb30 < policy.core_min_win_rate_lcb_30d:
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
    core_eligible = bool(strong_entry or standard_entry)
    if concentration_warning and not concentration_exception:
        core_eligible = False
    if recent_body_negative or liquidation_limit_exceeded:
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

    if liquidation_limit_exceeded:
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
        _num(
            metrics.get("initial_margin_equity"),
            float(getattr(config, "INITIAL_BALANCE", 10_000.0)) * margin_equity_pct,
        ),
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
