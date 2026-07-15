"""Copy-follow score used to rank the final watchlist.

`profile.score` remains the raw profile quality score. This module blends it with
copy-backtest evidence so the observer follows wallets that are actually copyable
under our own sizing/add/stop rules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from . import config
from .copy_policy import load_copy_policy
from .sector import apply_allowed_sector_copy_metrics, parse_json_obj


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


@dataclass(frozen=True)
class FollowScore:
    score: float
    detail: dict


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
    pnl30 = _num(metrics.get("copy_bt_net_pnl"))
    pnl14 = _num(metrics.get("copy_bt_14d_net_pnl"))
    pnl7 = _num(metrics.get("copy_bt_7d_net_pnl"))
    initial_balance = max(1.0, float(getattr(config, "INITIAL_BALANCE", 10_000.0)))
    if margin_equity_pct is None:
        margin_equity_pct = metrics.get("margin_equity_pct", config.MARGIN_EQUITY_PCT)
    margin_equity_pct = max(0.0, min(1.0, _num(margin_equity_pct, config.MARGIN_EQUITY_PCT)))
    qualification_equity = initial_balance * margin_equity_pct
    challenger_floor = qualification_equity * policy.challenger_min_return_30d
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
    if _num(expected_return) < min_expected_return:
        return {
            "eligible": False,
            "coreEligible": False,
            "status": "thin_copy_edge",
            "role": "rejected",
            "reasons": [f"保证金归一化预期收益低于{min_expected_return * 100:.1f}%经济底线"],
        }
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
    probability_ok = _num(positive_probability, 0.5) >= policy.entry_positive_probability
    lcb_ok = _num(return_lcb) >= policy.min_return_lcb
    standard_samples = c30 >= min_closed30 and evidence_days >= min_evidence_days and c7 >= min_closed7
    recent_warning = (
        (
            c7 >= min_closed7 and pnl7 < 0.0
            and recent_loss_ratio >= policy.recent_warning_loss_ratio
        )
        or (c14 >= min_closed14 and pnl14 <= 0.0)
    )
    strong_samples = c30 >= policy.strong_min_closed_30d and evidence_days >= policy.strong_min_evidence_days
    weekly_economics_ok = pnl7 >= weekly_core_floor
    # Strong 30d evidence may waive the thin 7d/LCB confidence checks, but never a confirmed recent
    # deterioration.  A wallet whose sampled 14d copy window is already non-positive remains worth
    # observing, not opening in Core, even when its older 30d history was excellent.
    strong_entry = (
        pnl30 >= strong_floor and strong_samples and probability_ok and not recent_warning
        and weekly_economics_ok and valuation_status == "complete"
    )
    standard_entry = (
        pnl30 >= core_floor and standard_samples and probability_ok and lcb_ok and not recent_warning
        and weekly_economics_ok and valuation_status == "complete"
    )
    core_eligible = bool(strong_entry or standard_entry)
    if core_eligible:
        return {
            "eligible": True,
            "coreEligible": True,
            "strongEntry": bool(strong_entry),
            "status": "core_eligible_strong" if strong_entry else "core_eligible",
            "role": "core_eligible",
            "returnLcb": return_lcb,
            "executionScore": execution,
            "recentLossRatio": recent_loss_ratio,
            "reasons": ["个人严格Copy证据达到Core准入线"],
        }

    if valuation_status != "complete":
        status, reason = "challenger_open_valuation_pending", "开放仓位缺少可靠末端估值，暂不进入Core"
    elif recent_warning:
        status, reason = "challenger_recent_decline", "近期回撤尚未达到硬淘汰线"
    elif pnl30 < core_floor:
        status, reason = "challenger_return_watch", "30天Copy收益达到候选线但未达到Core线"
    elif not strong_samples and (
        c30 < min_closed30 or evidence_days < min_evidence_days or c7 < min_closed7
    ):
        status, reason = "challenger_sample_watch", "Copy样本或独立证据尚不足"
    elif pnl7 < weekly_core_floor:
        status, reason = (
            "challenger_weekly_return_watch",
            f"7天Copy经济收益{pnl7:+.0f}低于Core周收益线{weekly_core_floor:.0f}",
        )
    else:
        status, reason = "challenger_confidence_watch", "LCB或盈利概率尚未达到Core线"
    return {
        "eligible": True,
        "coreEligible": False,
        "status": status,
        "role": "challenger",
        "returnLcb": return_lcb,
        "executionScore": execution,
        "recentLossRatio": recent_loss_ratio,
        "reasons": [reason],
    }


def compute_follow_score(metrics: Mapping) -> tuple[float, dict]:
    """Rank copyability without absolute-dollar PnL or overlapping-window confidence."""
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
    evidence_days = int(_num(metrics.get("copy_evidence_days")))
    confidence = 0.55 * _clamp(c30 / 20.0) + 0.45 * _clamp(evidence_days / 10.0)
    edge_score = _clamp(0.5 + 0.5 * math.tanh(expected / 0.05))
    lcb_score = _clamp(0.5 + 0.5 * math.tanh(lcb / 0.03))
    probability_score = _clamp((probability - 0.5) / 0.5)
    copy_score = (
        0.25 * edge_score + 0.25 * lcb_score + 0.20 * probability_score
        + 0.15 * risk + 0.15 * execution
    )
    shrunk_copy = 0.5 + confidence * (copy_score - 0.5)
    activity = _clamp(_num(metrics.get("open_probability_48h"), 0.0))
    score = 0.15 * raw + 0.75 * shrunk_copy + 0.10 * activity
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
        "openFillRate": metrics.get("actionable_open_rate", metrics.get("copy_bt_open_fill_rate")),
        "liquidations": liqs,
        "liquidationRate": liquidation_rate,
        "feeDrag": metrics.get("copy_bt_fee_drag"),
        "reasons": reasons,
    }


def choose_follow_line(
    ranked: list[Mapping],
    *,
    min_score: float = 0.60,
    min_n: int = 7,
    target_n: int = 16,
    max_n: int = 20,
    cliff_gap: float = 0.045,
) -> dict:
    """Pick the automatic follow threshold from already-ranked wallets.

    Quality decides first: if there is a visible score cliff, cut before the
    cliff. If quality is flat, there is no honest score boundary, so the line is
    set by capacity (`target_n`). Wallets below `min_score` are never included.
    """
    rows = [r for r in ranked if _num(r.get("follow_score", r.get("score"))) >= min_score]
    if not rows:
        return {"line": float(min_score), "count": 0, "reason": "no_wallet_above_floor"}

    def inclusive_line(score: float) -> float:
        score = _num(score)
        return max(float(min_score), score - 1e-9 if score > min_score else score)

    available = len(rows)
    min_n = max(1, min(int(min_n), available))
    max_n = max(min_n, min(int(max_n), available))
    target_n = max(min_n, min(int(target_n), max_n))

    for n in range(min_n, max_n):
        prev_score = _num(rows[n - 1].get("follow_score", rows[n - 1].get("score")))
        next_score = _num(rows[n].get("follow_score", rows[n].get("score")))
        if prev_score - next_score >= cliff_gap:
            return {
                "line": inclusive_line(prev_score),
                "count": n,
                "reason": "quality_cliff",
                "gap": prev_score - next_score,
            }

    chosen_score = _num(rows[target_n - 1].get("follow_score", rows[target_n - 1].get("score")))
    return {
        "line": inclusive_line(chosen_score),
        "count": target_n,
        "reason": "capacity_cap",
        "gap": 0.0,
    }
