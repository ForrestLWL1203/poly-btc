"""Versioned copy-evidence and portfolio-selection policy.

All consumers load this immutable value object instead of carrying their own evidence, Campaign/fold, risk,
selection, or tuning thresholds. Scanner params may override matching upper-case keys, while the version hash
keeps every published decision reproducible.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Mapping

from hyper import config


COPY_POLICY_PARAM_KEYS = (
    "COPY_BT_DAYS", "COPY_BT_RECENT_DAYS", "COPY_BT_MIN_CLOSED", "COPY_BT_MIN_CLOSED_14D",
    "COPY_BT_MIN_CLOSED_7D", "CORE_COPY_MIN_CAMPAIGNS_30D",
    "CORE_COPY_MIN_CAMPAIGN_WIN_RATE", "CORE_COPY_MIN_BODY_WIN_RATE",
    "CORE_MIN_FOLLOW_SCORE",
    "CORE_RETENTION_MIN_COPY_RETURN_30D", "CORE_SOFT_FAIL_CONFIRMATIONS",
    "CORE_COPY_MAX_LIQUIDATIONS_30D", "COPY_DEEP_BAG_EVENT_PCT",
    "COPY_DEEP_BAG_EVENT_MIN_HOURS", "COPY_DEEP_BAG_LONG_HOURS", "CORE_INTRATRADE_DD_MAX",
    "CORE_INTRATRADE_DD_REJECT", "CORE_DEEP_BAG_MAX_FAILED", "CORE_DEEP_BAG_MIN_RECOVERY_RATE",
    "COPY_MIN_EXPECTED_MARGIN_RETURN", "CORE_MIN_COPY_RETURN_30D", "COPY_MIN_RAW_PAYOFF_RATIO",
    "COPY_STABILITY_FOLD_DAYS", "COPY_STABILITY_FOLD_COUNT",
    "COPY_STABILITY_MIN_CAMPAIGNS_PER_FOLD", "COPY_STABILITY_MIN_EVALUABLE_FOLDS",
    "COPY_STABILITY_MIN_PROFITABLE_FOLDS", "COPY_STABILITY_MIN_RETURN",
    "SELECTION_MIN_ACTIONABLE_RATE", "SELECTION_MIN_CAPACITY_FIT",
)


@dataclass(frozen=True)
class CopyPolicy:
    windows: tuple[int, ...]
    min_closed_30d: int
    min_closed_14d: int
    min_closed_7d: int
    core_min_campaigns_30d: int
    core_min_campaign_win_rate: float
    core_min_body_win_rate: float
    core_min_follow_score: float
    retention_min_return_30d: float
    soft_fail_confirmations: int
    core_max_liquidations_30d: int
    deep_bag_event_pct: float
    deep_bag_event_min_hours: float
    deep_bag_long_hours: float
    intratrade_dd_core_max: float
    intratrade_dd_reject: float
    deep_bag_max_failed: int
    deep_bag_min_recovery_rate: float
    min_expected_margin_return: float
    core_min_return_30d: float
    min_raw_payoff_ratio: float
    stability_fold_days: int
    stability_fold_count: int
    stability_min_campaigns_per_fold: int
    stability_min_evaluable_folds: int
    stability_min_profitable_folds: int
    stability_min_return: float
    min_actionable_open_rate: float
    min_capacity_fit: float
    tune_min_relative_gain: float
    tune_min_shadow_days: int
    tune_min_forward_closed: int

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
        core_min_campaigns_30d=int(_value(values, "CORE_COPY_MIN_CAMPAIGNS_30D", 10) or 0),
        core_min_campaign_win_rate=float(_value(
            values, "CORE_COPY_MIN_CAMPAIGN_WIN_RATE", 0.45,
        )),
        core_min_body_win_rate=float(_value(values, "CORE_COPY_MIN_BODY_WIN_RATE", 0.40)),
        core_min_follow_score=float(_value(values, "CORE_MIN_FOLLOW_SCORE", 0.75)),
        retention_min_return_30d=float(_value(values, "CORE_RETENTION_MIN_COPY_RETURN_30D", 0.07)),
        soft_fail_confirmations=int(_value(values, "CORE_SOFT_FAIL_CONFIRMATIONS", 2) or 1),
        core_max_liquidations_30d=int(_value(
            values, "CORE_COPY_MAX_LIQUIDATIONS_30D", 1,
        ) or 0),
        deep_bag_event_pct=float(_value(values, "COPY_DEEP_BAG_EVENT_PCT", 0.08)),
        deep_bag_event_min_hours=float(_value(values, "COPY_DEEP_BAG_EVENT_MIN_HOURS", 4.0)),
        deep_bag_long_hours=float(_value(values, "COPY_DEEP_BAG_LONG_HOURS", 24.0)),
        intratrade_dd_core_max=float(_value(values, "CORE_INTRATRADE_DD_MAX", 0.12)),
        intratrade_dd_reject=float(_value(values, "CORE_INTRATRADE_DD_REJECT", 0.15)),
        deep_bag_max_failed=int(_value(values, "CORE_DEEP_BAG_MAX_FAILED", 1) or 0),
        deep_bag_min_recovery_rate=float(_value(values, "CORE_DEEP_BAG_MIN_RECOVERY_RATE", 0.50)),
        min_expected_margin_return=float(_value(values, "COPY_MIN_EXPECTED_MARGIN_RETURN", 0.02)),
        core_min_return_30d=float(_value(values, "CORE_MIN_COPY_RETURN_30D", 0.10)),
        min_raw_payoff_ratio=float(_value(values, "COPY_MIN_RAW_PAYOFF_RATIO", 0.60)),
        stability_fold_days=int(_value(values, "COPY_STABILITY_FOLD_DAYS", 7) or 7),
        stability_fold_count=int(_value(values, "COPY_STABILITY_FOLD_COUNT", 4) or 4),
        stability_min_campaigns_per_fold=int(_value(
            values, "COPY_STABILITY_MIN_CAMPAIGNS_PER_FOLD", 2,
        ) or 1),
        stability_min_evaluable_folds=int(_value(
            values, "COPY_STABILITY_MIN_EVALUABLE_FOLDS", 4,
        ) or 1),
        stability_min_profitable_folds=int(_value(
            values, "COPY_STABILITY_MIN_PROFITABLE_FOLDS", 4,
        ) or 1),
        stability_min_return=float(_value(values, "COPY_STABILITY_MIN_RETURN", 0.05)),
        min_actionable_open_rate=float(_value(values, "SELECTION_MIN_ACTIONABLE_RATE", 0.70)),
        min_capacity_fit=float(_value(values, "SELECTION_MIN_CAPACITY_FIT", 0.75)),
        tune_min_relative_gain=float(_value(values, "AUTO_TUNE_MIN_RELATIVE_GAIN", 0.05)),
        tune_min_shadow_days=int(_value(values, "AUTO_TUNE_APPLY_MIN_SHADOW_DAYS", 14)),
        tune_min_forward_closed=int(_value(values, "AUTO_TUNE_APPLY_MIN_FORWARD_CLOSED", 100)),
    )
