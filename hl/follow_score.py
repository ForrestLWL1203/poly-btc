"""Copy-follow score used to rank the final watchlist.

`profile.score` remains the raw profile quality score. This module blends it with
copy-backtest evidence so the observer follows wallets that are actually copyable
under our own sizing/add/stop rules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping


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


def _pnl_signal(pnl: float, scale: float) -> float:
    """Smooth copy PnL into [-1, 1] without letting one huge win dominate."""
    return math.tanh(pnl / scale) if scale > 0 else 0.0


@dataclass(frozen=True)
class FollowScore:
    score: float
    detail: dict


def compute_follow_score(metrics: Mapping) -> tuple[float, dict]:
    """Return `(score01, detail)` for final follow ranking.

    Missing copy-backtest data intentionally falls back to raw score so old or
    partially seeded DBs keep their current behaviour until the next scan fills
    the replay fields.
    """
    raw = _clamp(_num(metrics.get("score")))
    c30 = int(_num(metrics.get("copy_bt_closed_n")))
    c14 = int(_num(metrics.get("copy_bt_14d_closed_n")))
    c7 = int(_num(metrics.get("copy_bt_7d_closed_n")))
    p30 = _num(metrics.get("copy_bt_net_pnl"))
    p14 = _num(metrics.get("copy_bt_14d_net_pnl"))
    p7 = _num(metrics.get("copy_bt_7d_net_pnl"))

    has_copy = any(metrics.get(k) is not None for k in (
        "copy_bt_net_pnl", "copy_bt_14d_net_pnl", "copy_bt_7d_net_pnl",
    )) or c30 > 0 or c14 > 0 or c7 > 0
    if not has_copy:
        return raw, {
            "rawScore": raw,
            "copyScore": None,
            "confidence": 0.0,
            "copyPnl": {"30d": None, "14d": None, "7d": None},
            "closedN": {"30d": c30, "14d": c14, "7d": c7},
            "reasons": ["暂无copy回测,使用原始评分"],
        }

    s30 = _pnl_signal(p30, 3000.0)
    s14 = _pnl_signal(p14, 1800.0)
    s7 = _pnl_signal(p7, 800.0)
    pnl_signal = 0.35 * s30 + 0.40 * s14 + 0.25 * s7
    copy_score = _clamp(0.5 + 0.5 * pnl_signal)

    confidence = (
        0.25 * _clamp(c30 / 20.0) +
        0.45 * _clamp(c14 / 14.0) +
        0.30 * _clamp(c7 / 6.0)
    )
    score = raw * (0.55 - 0.20 * confidence) + copy_score * (0.45 + 0.20 * confidence)

    reasons = []
    if p30 > 0 and p14 > 0 and p7 > 0:
        score += 0.03
        reasons.append("30/14/7天copy均为正")
    if c7 < 3:
        score -= 0.12
        reasons.append(f"7天样本偏少({c7}笔)")
    if c14 < 5:
        score -= 0.05
        reasons.append(f"14天样本偏少({c14}笔)")
    if c30 < 7:
        score -= 0.04
        reasons.append(f"30天样本偏少({c30}笔)")
    if p14 < 0 and c14 >= 4:
        score -= 0.12
        reasons.append("近期copy亏损(14天)")
    if p7 < 0 and c7 >= 3:
        score -= 0.08
        reasons.append("近期copy亏损(7天)")
    if p30 < 0 and c30 >= 7:
        score -= 0.10
        reasons.append("30天copy亏损")

    open_fill_rate = metrics.get("copy_bt_open_fill_rate")
    if open_fill_rate is not None:
        fill_rate = _num(open_fill_rate)
        if fill_rate < 0.75:
            score -= 0.06
            reasons.append(f"开仓跟随率偏低({fill_rate * 100:.0f}%)")

    liqs = int(_num(metrics.get("copy_bt_liquidations")))
    if liqs > 0:
        score -= min(0.15, 0.05 * liqs)
        reasons.append(f"copy爆仓{liqs}次")

    fee_drag = _num(metrics.get("copy_bt_fee_drag"))
    gross_abs = abs(p30) + abs(fee_drag)
    if gross_abs > 0 and fee_drag / gross_abs > 0.35:
        score -= 0.04
        reasons.append("手续费拖累偏高")

    score = _clamp(score)
    if not reasons:
        reasons.append("copy表现与原始评分基本一致")

    return score, {
        "rawScore": raw,
        "copyScore": copy_score,
        "confidence": confidence,
        "copyPnl": {"30d": p30, "14d": p14, "7d": p7},
        "closedN": {"30d": c30, "14d": c14, "7d": c7},
        "openFillRate": _num(open_fill_rate, default=1.0) if open_fill_rate is not None else None,
        "liquidations": liqs,
        "feeDrag": fee_drag,
        "reasons": reasons,
    }


def score_from_row(row) -> tuple[float, dict]:
    """Small adapter for sqlite rows/tuples converted to dicts by callers."""
    return compute_follow_score(row)


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

    available = len(rows)
    min_n = max(1, min(int(min_n), available))
    max_n = max(min_n, min(int(max_n), available))
    target_n = max(min_n, min(int(target_n), max_n))

    for n in range(min_n, max_n):
        prev_score = _num(rows[n - 1].get("follow_score", rows[n - 1].get("score")))
        next_score = _num(rows[n].get("follow_score", rows[n].get("score")))
        if prev_score - next_score >= cliff_gap:
            return {
                "line": max(float(min_score), prev_score),
                "count": n,
                "reason": "quality_cliff",
                "gap": prev_score - next_score,
            }

    chosen_score = _num(rows[target_n - 1].get("follow_score", rows[target_n - 1].get("score")))
    return {
        "line": max(float(min_score), chosen_score),
        "count": target_n,
        "reason": "capacity_cap",
        "gap": 0.0,
    }
