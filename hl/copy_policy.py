"""Versioned copy-evidence and portfolio-selection policy.

All consumers load this immutable value object instead of carrying independent 30/14/7 sample floors or
selection/tuning thresholds.  Scanner params may override matching upper-case keys, while the version hash
keeps every published decision reproducible.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Mapping

from . import config


@dataclass(frozen=True)
class CopyPolicy:
    windows: tuple[int, ...]
    min_closed_30d: int
    min_closed_14d: int
    min_closed_7d: int
    min_expected_margin_return: float
    min_return_lcb: float
    entry_positive_probability: float
    challenger_min_return_30d: float
    core_min_return_30d: float
    strong_core_return_30d: float
    strong_min_closed_30d: int
    strong_min_evidence_days: int
    recent_warning_loss_ratio: float
    recent_hard_loss_ratio: float
    entry_max_open_age_h: float
    keep_max_open_age_h: float
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

    @property
    def version(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return "copy-policy-" + hashlib.sha256(payload.encode()).hexdigest()[:12]


def _value(values: Mapping | None, key: str, default):
    if values and values.get(key) is not None:
        return values[key]
    return getattr(config, key, default)


def load_copy_policy(values: Mapping | None = None) -> CopyPolicy:
    primary = int(_value(values, "COPY_BT_DAYS", 30) or 30)
    recent = tuple(int(x) for x in _value(values, "COPY_BT_RECENT_DAYS", (14, 7)) if int(x) > 0)
    windows = tuple(dict.fromkeys((primary,) + recent))
    return CopyPolicy(
        windows=windows,
        min_closed_30d=int(_value(values, "COPY_BT_MIN_CLOSED", 7) or 0),
        min_closed_14d=int(_value(values, "COPY_BT_MIN_CLOSED_14D", 5) or 0),
        min_closed_7d=int(_value(values, "COPY_BT_MIN_CLOSED_7D", 5) or 0),
        min_expected_margin_return=float(_value(values, "COPY_MIN_EXPECTED_MARGIN_RETURN", 0.02)),
        min_return_lcb=float(_value(values, "COPY_MIN_RETURN_LCB", 0.0)),
        entry_positive_probability=float(_value(values, "CORE_ENTRY_MIN_POSITIVE_PROB", 0.70)),
        challenger_min_return_30d=float(_value(values, "CHALLENGER_MIN_COPY_RETURN_30D", 0.03)),
        core_min_return_30d=float(_value(values, "CORE_MIN_COPY_RETURN_30D", 0.05)),
        strong_core_return_30d=float(_value(values, "CORE_STRONG_COPY_RETURN_30D", 0.10)),
        strong_min_closed_30d=int(_value(values, "CORE_STRONG_MIN_CLOSED_30D", 20)),
        strong_min_evidence_days=int(_value(values, "CORE_STRONG_MIN_EVIDENCE_DAYS", 10)),
        recent_warning_loss_ratio=float(_value(values, "CORE_RECENT_WARNING_LOSS_RATIO", 0.10)),
        recent_hard_loss_ratio=float(_value(values, "CORE_RECENT_HARD_LOSS_RATIO", 0.25)),
        entry_max_open_age_h=float(_value(values, "CORE_ENTRY_MAX_OPEN_AGE_H", 24.0)),
        keep_max_open_age_h=float(_value(values, "CORE_KEEP_MAX_OPEN_AGE_H", 72.0)),
        min_actionable_open_rate=float(_value(values, "SELECTION_MIN_ACTIONABLE_RATE", 0.70)),
        min_capacity_fit=float(_value(values, "SELECTION_MIN_CAPACITY_FIT", 0.85)),
        min_marginal_gain=float(_value(values, "SELECTION_MIN_RELATIVE_GAIN", 0.05)),
        max_drawdown_worsening=float(_value(values, "SELECTION_MAX_DD_WORSEN", 0.01)),
        tune_min_relative_gain=float(_value(values, "AUTO_TUNE_MIN_RELATIVE_GAIN", 0.05)),
        tune_max_drawdown_worsening=float(_value(values, "AUTO_TUNE_MAX_DD_WORSEN", 0.01)),
        tune_min_shadow_days=int(_value(values, "AUTO_TUNE_APPLY_MIN_SHADOW_DAYS", 14)),
        tune_min_forward_closed=int(_value(values, "AUTO_TUNE_APPLY_MIN_FORWARD_CLOSED", 100)),
        gate_enabled=bool(_value(values, "COPY_BT_GATE_ENABLE", True)),
    )
