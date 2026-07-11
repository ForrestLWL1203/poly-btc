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
from .sector import apply_allowed_sector_copy_metrics


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
) -> dict:
    """Classify Core eligibility from normalized, non-overlapping evidence."""
    policy = load_copy_policy()
    min_closed30 = policy.min_closed_30d if min_closed30 is None else int(min_closed30)
    min_closed14 = policy.min_closed_14d if min_closed14 is None else int(min_closed14)
    min_closed7 = policy.min_closed_7d if min_closed7 is None else int(min_closed7)
    min_open_fill_rate = policy.min_actionable_open_rate if min_open_fill_rate is None else float(min_open_fill_rate)
    min_expected_return = (
        policy.min_expected_margin_return if min_expected_return is None else float(min_expected_return)
    )
    min_evidence_days = min(5, min_closed30) if min_evidence_days is None else int(min_evidence_days)
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
    data_status = str(metrics.get("copy_bt_data_status") or "").strip().lower()
    evidence_status = str(metrics.get("copy_bt_evidence_status") or "").strip().lower()
    if data_status and data_status not in {"valid", "ok"}:
        return {
            "eligible": False,
            "status": "copy_data_error",
            "role": "quarantine",
            "deferred": True,
            "reasons": ["copy回放数据无效，禁止进入实跟集合"],
        }
    if evidence_status in {"invalid", "no_evidence", "no_fills", "no_open_events"}:
        return {
            "eligible": False,
            "status": "no_copy_evidence" if evidence_status != "invalid" else "copy_data_error",
            "role": "rejected" if evidence_status != "invalid" else "quarantine",
            "deferred": evidence_status == "invalid",
            "reasons": ["缺少有效copy回测证据，资格阶段排除"],
        }
    if not _has_copy_evidence(metrics, c30, c14, c7):
        return {
            "eligible": False,
            "status": "normalized_evidence_missing",
            "role": "rejected",
            "reasons": ["缺少保证金归一化的非重叠copy证据"],
        }
    if c30 < min_closed30 or evidence_days < min_evidence_days:
        return {
            "eligible": False,
            "status": "thin_independent_evidence",
            "role": "rejected",
            "reasons": [f"独立证据不足({c30}笔/{evidence_days}天)"],
        }
    if _num(expected_return) < min_expected_return:
        return {
            "eligible": False,
            "status": "thin_copy_edge",
            "role": "rejected",
            "reasons": [f"保证金归一化预期收益低于{min_expected_return * 100:.1f}%经济底线"],
        }
    if _num(return_lcb) < policy.min_return_lcb:
        return {
            "eligible": False,
            "status": "copy_return_lcb_low",
            "role": "rejected",
            "reasons": ["按日Bootstrap收益下置信界仍为负"],
        }
    if _num(positive_probability, 0.5) < policy.entry_positive_probability:
        return {
            "eligible": False,
            "status": "positive_probability_low",
            "role": "rejected",
            "reasons": [f"未来copy盈利概率低于{policy.entry_positive_probability * 100:.0f}%"],
        }
    execution = metrics.get("execution_score")
    open_fill_rate = metrics.get("actionable_open_rate", metrics.get("copy_bt_open_fill_rate"))
    capacity = metrics.get("capacity_fit")
    if open_fill_rate is not None and _num(open_fill_rate, 1.0) < min_open_fill_rate:
        return {
            "eligible": False,
            "status": "low_fill_rate",
            "role": "rejected",
            "reasons": [f"开仓跟随率低于{min_open_fill_rate * 100:.0f}%"],
        }
    if capacity is not None and _num(capacity) < policy.min_capacity_fit:
        return {
            "eligible": False,
            "status": "capacity_fit_low",
            "role": "rejected",
            "reasons": [f"资金容量适配率低于{policy.min_capacity_fit * 100:.0f}%"],
        }
    if int(_num(metrics.get("copy_bt_liquidations"))) > 0:
        return {
            "eligible": False,
            "status": "copy_liquidation",
            "role": "rejected",
            "reasons": ["回放出现爆仓"],
        }
    if recent7 is not None and c7 >= min_closed7 and _num(recent7) <= 0.0:
        return {
            "eligible": False,
            "status": "recent_copy_loss",
            "role": "rejected",
            "reasons": ["7天有效样本的保证金净收益不为正"],
        }
    return {
        "eligible": True,
        "status": "eligible",
        "returnLcb": return_lcb,
        "executionScore": execution,
        "reasons": ["非重叠copy证据、执行与容量均合格"],
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
    if liqs > 0:
        score -= min(0.15, 0.05 * liqs)
        reasons.append(f"copy爆仓{liqs}次")
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
