"""Versioned copy-evidence and portfolio-selection policy.

All consumers load this immutable value object instead of carrying independent 30/14/7 sample floors or
selection/tuning thresholds.  Scanner params may override matching upper-case keys, while the version hash
keeps every published decision reproducible.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Mapping

from hyper import config


@dataclass(frozen=True)
class CopyPolicy:
    windows: tuple[int, ...]
    min_closed_30d: int
    min_closed_14d: int
    min_closed_7d: int
    core_min_closed_30d: int
    core_min_closed_14d: int
    core_min_closed_7d: int
    core_min_win_rate_30d: float
    core_min_win_rate_14d: float
    core_min_win_rate_7d: float
    core_win_rate_lcb_confidence: float
    core_min_win_rate_lcb_30d: float
    core_recent_body_min_closed: int
    core_max_liquidations_30d: int
    min_expected_margin_return: float
    min_return_lcb: float
    entry_positive_probability: float
    challenger_min_return_30d: float
    challenger_min_return_7d: float
    core_min_return_30d: float
    core_min_return_7d: float
    strong_core_return_30d: float
    strong_min_closed_30d: int
    strong_min_evidence_days: int
    recent_warning_loss_ratio: float
    recent_hard_loss_ratio: float
    min_raw_payoff_ratio: float
    min_profit_factor: float
    min_tail_return_30d: float
    max_top1_profit_share: float
    max_top3_profit_share: float
    concentration_min_positive_episodes: int
    concentration_body_min_episodes: int
    concentration_body_min_win_rate: float
    concentration_body_min_profit_factor: float
    strong_sparse_min_closed_30d: int
    strong_sparse_min_evidence_days: int
    strong_sparse_min_closed_7d: int
    strong_sparse_min_win_rate_7d: float
    min_actionable_open_rate: float
    min_capacity_fit: float
    min_marginal_gain: float
    max_drawdown_worsening: float
    tune_min_relative_gain: float
    tune_max_drawdown_worsening: float
    tune_min_shadow_days: int
    tune_min_forward_closed: int
    gate_enabled: bool

    def min_closed(self, days: int) -> int:
        if int(days) <= 7:
            return self.min_closed_7d
        if int(days) <= 14:
            return self.min_closed_14d
        return self.min_closed_30d

    def core_min_closed(self, days: int) -> int:
        if int(days) <= 7:
            return self.core_min_closed_7d
        if int(days) <= 14:
            return self.core_min_closed_14d
        return self.core_min_closed_30d

    def core_min_win_rate(self, days: int) -> float:
        if int(days) <= 7:
            return self.core_min_win_rate_7d
        if int(days) <= 14:
            return self.core_min_win_rate_14d
        return self.core_min_win_rate_30d

    @property
    def version(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return "copy-policy-" + hashlib.sha256(payload.encode()).hexdigest()[:12]


def _value(values: Mapping | None, key: str, default):
    if values and values.get(key) is not None:
        return values[key]
    return getattr(config, key, default)


def one_sided_wilson_lower_bound(wins: int, total: int, confidence: float = 0.80) -> float:
    """Return a one-sided Wilson lower confidence bound for a binomial win rate.

    The production policy uses 80%, whose standard-normal z-score is fixed here to avoid a scipy runtime
    dependency.  A conservative interpolation covers the narrow operator-tunable range while invalid or empty
    samples fail closed at zero.
    """
    total = max(0, int(total or 0))
    wins = max(0, min(total, int(wins or 0)))
    if total <= 0:
        return 0.0
    confidence = max(0.50, min(0.99, float(confidence or 0.80)))
    # Common one-sided normal quantiles. Linear interpolation is more than sufficient for a policy threshold;
    # the default 80% value is represented exactly.
    quantiles = (
        (0.50, 0.0), (0.75, 0.6744897502), (0.80, 0.8416212336),
        (0.85, 1.0364333895), (0.90, 1.2815515655), (0.95, 1.6448536270),
        (0.975, 1.9599639845), (0.99, 2.3263478740),
    )
    z = quantiles[-1][1]
    for (lo_c, lo_z), (hi_c, hi_z) in zip(quantiles, quantiles[1:]):
        if lo_c <= confidence <= hi_c:
            span = hi_c - lo_c
            z = lo_z + (confidence - lo_c) * (hi_z - lo_z) / span if span else lo_z
            break
    p = wins / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    radius = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return max(0.0, min(1.0, (centre - radius) / denominator))


def load_copy_policy(values: Mapping | None = None) -> CopyPolicy:
    primary = int(_value(values, "COPY_BT_DAYS", 30) or 30)
    recent = tuple(int(x) for x in _value(values, "COPY_BT_RECENT_DAYS", (14, 7)) if int(x) > 0)
    windows = tuple(dict.fromkeys((primary,) + recent))
    return CopyPolicy(
        windows=windows,
        min_closed_30d=int(_value(values, "COPY_BT_MIN_CLOSED", 7) or 0),
        min_closed_14d=int(_value(values, "COPY_BT_MIN_CLOSED_14D", 5) or 0),
        min_closed_7d=int(_value(values, "COPY_BT_MIN_CLOSED_7D", 5) or 0),
        core_min_closed_30d=int(_value(values, "CORE_COPY_MIN_CLOSED_30D", 15) or 0),
        core_min_closed_14d=int(_value(values, "CORE_COPY_MIN_CLOSED_14D", 7) or 0),
        core_min_closed_7d=int(_value(values, "CORE_COPY_MIN_CLOSED_7D", 5) or 0),
        core_min_win_rate_30d=float(_value(values, "CORE_COPY_WIN_RATE_30D_MIN", 0.65)),
        core_min_win_rate_14d=float(_value(values, "CORE_COPY_WIN_RATE_14D_MIN", 0.60)),
        core_min_win_rate_7d=float(_value(values, "CORE_COPY_WIN_RATE_7D_MIN", 0.60)),
        core_win_rate_lcb_confidence=float(_value(
            values, "CORE_COPY_WIN_RATE_LCB_CONFIDENCE", 0.80,
        )),
        core_min_win_rate_lcb_30d=float(_value(
            values, "CORE_COPY_WIN_RATE_LCB_30D_MIN", 0.50,
        )),
        core_recent_body_min_closed=int(_value(
            values, "CORE_COPY_RECENT_BODY_MIN_CLOSED", 10,
        ) or 0),
        core_max_liquidations_30d=int(_value(
            values, "CORE_COPY_MAX_LIQUIDATIONS_30D", 5,
        ) or 0),
        min_expected_margin_return=float(_value(values, "COPY_MIN_EXPECTED_MARGIN_RETURN", 0.02)),
        min_return_lcb=float(_value(values, "COPY_MIN_RETURN_LCB", 0.0)),
        entry_positive_probability=float(_value(values, "CORE_ENTRY_MIN_POSITIVE_PROB", 0.70)),
        challenger_min_return_30d=float(_value(values, "CHALLENGER_MIN_COPY_RETURN_30D", 0.10)),
        challenger_min_return_7d=float(_value(values, "CHALLENGER_MIN_COPY_RETURN_7D", 0.03)),
        core_min_return_30d=float(_value(values, "CORE_MIN_COPY_RETURN_30D", 0.10)),
        core_min_return_7d=float(_value(values, "CORE_MIN_COPY_RETURN_7D", 0.05)),
        strong_core_return_30d=float(_value(values, "CORE_STRONG_COPY_RETURN_30D", 0.20)),
        strong_min_closed_30d=int(_value(values, "CORE_STRONG_MIN_CLOSED_30D", 20)),
        strong_min_evidence_days=int(_value(values, "CORE_STRONG_MIN_EVIDENCE_DAYS", 10)),
        recent_warning_loss_ratio=float(_value(values, "CORE_RECENT_WARNING_LOSS_RATIO", 0.10)),
        recent_hard_loss_ratio=float(_value(values, "CORE_RECENT_HARD_LOSS_RATIO", 0.25)),
        min_raw_payoff_ratio=float(_value(values, "COPY_MIN_RAW_PAYOFF_RATIO", 0.60)),
        min_profit_factor=float(_value(values, "COPY_MIN_PROFIT_FACTOR", 1.30)),
        min_tail_return_30d=float(_value(values, "COPY_MIN_TAIL_RETURN_30D", 0.05)),
        max_top1_profit_share=float(_value(values, "COPY_MAX_TOP1_PROFIT_SHARE", 0.50)),
        max_top3_profit_share=float(_value(values, "COPY_MAX_TOP3_PROFIT_SHARE", 0.80)),
        concentration_min_positive_episodes=int(_value(
            values, "COPY_CONCENTRATION_MIN_POSITIVE_EPISODES", 5,
        )),
        concentration_body_min_episodes=int(_value(
            values, "COPY_CONCENTRATION_BODY_MIN_EPISODES", 5,
        )),
        concentration_body_min_win_rate=float(_value(
            values, "COPY_CONCENTRATION_BODY_MIN_WIN_RATE", 0.60,
        )),
        concentration_body_min_profit_factor=float(_value(
            values, "COPY_CONCENTRATION_BODY_MIN_PROFIT_FACTOR", 1.00,
        )),
        strong_sparse_min_closed_30d=int(_value(
            values, "CORE_STRONG_SPARSE_MIN_CLOSED_30D", 10,
        )),
        strong_sparse_min_evidence_days=int(_value(
            values, "CORE_STRONG_SPARSE_MIN_EVIDENCE_DAYS", 7,
        )),
        strong_sparse_min_closed_7d=int(_value(
            values, "CORE_STRONG_SPARSE_MIN_CLOSED_7D", 3,
        )),
        strong_sparse_min_win_rate_7d=float(_value(
            values, "CORE_STRONG_SPARSE_MIN_WIN_RATE_7D", 0.75,
        )),
        min_actionable_open_rate=float(_value(values, "SELECTION_MIN_ACTIONABLE_RATE", 0.70)),
        min_capacity_fit=float(_value(values, "SELECTION_MIN_CAPACITY_FIT", 0.75)),
        min_marginal_gain=float(_value(values, "SELECTION_MIN_RELATIVE_GAIN", 0.05)),
        max_drawdown_worsening=float(_value(values, "SELECTION_MAX_DD_WORSEN", 0.01)),
        tune_min_relative_gain=float(_value(values, "AUTO_TUNE_MIN_RELATIVE_GAIN", 0.05)),
        tune_max_drawdown_worsening=float(_value(values, "AUTO_TUNE_MAX_DD_WORSEN", 0.01)),
        tune_min_shadow_days=int(_value(values, "AUTO_TUNE_APPLY_MIN_SHADOW_DAYS", 14)),
        tune_min_forward_closed=int(_value(values, "AUTO_TUNE_APPLY_MIN_FORWARD_CLOSED", 100)),
        gate_enabled=bool(_value(values, "COPY_BT_GATE_ENABLE", True)),
    )
