"""Versioned source-quality, Copy and portfolio-selection policy."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Mapping

from hyper import config


COPY_POLICY_PARAM_KEYS = (
    "COPY_BT_DAYS", "COPY_BT_RECENT_DAYS", "COPY_BT_MIN_CLOSED", "COPY_BT_MIN_CLOSED_14D",
    "SOURCE_QUALITY_MAX_N", "SOURCE_MIN_EPISODES_30D",
    "SOURCE_MIN_EPISODE_WIN_RATE", "SOURCE_TOP3_CONCENTRATION_TRIGGER",
    "SOURCE_BODY_MIN_RETAINED_NET",
    "SOURCE_LOW_FREQ_MIN_EPISODES_30D", "SOURCE_LOW_FREQ_MAX_EPISODES_30D",
    "SOURCE_LOW_FREQ_MIN_EPISODE_WIN_RATE", "SOURCE_LOW_FREQ_MIN_OFFICIAL_RETURN",
    "SOURCE_BODY_MIN_WIN_RATE", "ROUGH_COPY_MIN_CLOSED_30D",
    "ROUGH_COPY_MIN_WIN_RATE", "CORE_COPY_MIN_WIN_RATE",
    "CORE_COPY_MAX_LIQUIDATIONS_30D", "COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT",
    "COPY_DEEP_BAG_EVENT_PCT",
    "COPY_DEEP_BAG_EVENT_MIN_HOURS", "COPY_DEEP_BAG_LONG_HOURS",
    "OFFICIAL_PERP_MIN_RETURN_30D", "OFFICIAL_PERP_MIN_RETURN_7D",
    "OFFICIAL_PERP_LONG_HISTORY_DAYS", "OFFICIAL_PERP_SHORT_HISTORY_DAYS",
    "OFFICIAL_PERP_BOUNDARY_MAX_GAP_HOURS",
    "CORE_MIN_DYNAMIC_COPY_RETURN_30D", "CORE_MIN_DYNAMIC_COPY_RETURN_7D",
    "CORE_PORTFOLIO_MIN_RETURN_30D", "CORE_PORTFOLIO_MIN_RETURN_7D",
    "SELECTION_MIN_ACTIONABLE_RATE", "SELECTION_MIN_CAPACITY_FIT",
)


@dataclass(frozen=True)
class CopyPolicy:
    windows: tuple[int, ...]
    min_closed_30d: int
    min_closed_14d: int
    source_quality_max_n: int
    source_min_episodes_30d: int
    source_min_episode_win_rate: float
    source_low_freq_min_episodes_30d: int
    source_low_freq_max_episodes_30d: int
    source_low_freq_min_episode_win_rate: float
    source_low_freq_min_official_return: float
    source_top3_concentration_trigger: float
    source_body_min_retained_net: float
    source_body_min_win_rate: float
    rough_min_closed_30d: int
    rough_min_win_rate: float
    core_min_copy_win_rate: float
    core_max_liquidations_30d: int
    catastrophic_liquidation_loss_pct: float
    deep_bag_event_pct: float
    deep_bag_event_min_hours: float
    deep_bag_long_hours: float
    official_perp_min_return_30d: float
    official_perp_min_return_7d: float
    official_perp_long_history_days: int
    official_perp_short_history_days: int
    official_perp_boundary_max_gap_hours: float
    core_min_dynamic_copy_return_30d: float
    core_min_dynamic_copy_return_7d: float
    portfolio_min_return_30d: float
    portfolio_min_return_7d: float
    min_actionable_open_rate: float
    min_capacity_fit: float
    tune_min_relative_gain: float
    tune_min_shadow_days: int
    tune_min_forward_closed: int

    def min_closed(self, days: int) -> int:
        if int(days) <= 7:
            return 0
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
    values = values or {}
    primary = int(_value(values, "COPY_BT_DAYS", 30) or 30)
    recent = tuple(int(x) for x in _value(values, "COPY_BT_RECENT_DAYS", (14, 7)) if int(x) > 0)
    windows = tuple(dict.fromkeys((primary,) + recent))
    return CopyPolicy(
        windows=windows,
        min_closed_30d=int(_value(values, "COPY_BT_MIN_CLOSED", 7) or 0),
        min_closed_14d=int(_value(values, "COPY_BT_MIN_CLOSED_14D", 5) or 0),
        source_quality_max_n=int(_value(values, "SOURCE_QUALITY_MAX_N", 40) or 0),
        source_min_episodes_30d=int(_value(values, "SOURCE_MIN_EPISODES_30D", 10) or 0),
        source_min_episode_win_rate=float(_value(
            values, "SOURCE_MIN_EPISODE_WIN_RATE", 0.70,
        )),
        source_low_freq_min_episodes_30d=int(_value(
            values, "SOURCE_LOW_FREQ_MIN_EPISODES_30D", 7,
        ) or 0),
        source_low_freq_max_episodes_30d=int(_value(
            values, "SOURCE_LOW_FREQ_MAX_EPISODES_30D", 9,
        ) or 0),
        source_low_freq_min_episode_win_rate=float(_value(
            values, "SOURCE_LOW_FREQ_MIN_EPISODE_WIN_RATE", 0.85,
        )),
        source_low_freq_min_official_return=float(_value(
            values, "SOURCE_LOW_FREQ_MIN_OFFICIAL_RETURN", 0.30,
        )),
        source_top3_concentration_trigger=float(_value(
            values, "SOURCE_TOP3_CONCENTRATION_TRIGGER", 0.60,
        )),
        source_body_min_retained_net=float(_value(
            values, "SOURCE_BODY_MIN_RETAINED_NET", 0.20,
        )),
        source_body_min_win_rate=float(_value(values, "SOURCE_BODY_MIN_WIN_RATE", 0.70)),
        rough_min_closed_30d=int(_value(values, "ROUGH_COPY_MIN_CLOSED_30D", 7) or 0),
        rough_min_win_rate=float(_value(values, "ROUGH_COPY_MIN_WIN_RATE", 0.60)),
        core_min_copy_win_rate=float(_value(values, "CORE_COPY_MIN_WIN_RATE", 0.60)),
        core_max_liquidations_30d=int(_value(
            values, "CORE_COPY_MAX_LIQUIDATIONS_30D", 3,
        ) or 0),
        catastrophic_liquidation_loss_pct=max(
            float(getattr(config, "COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT", 0.08)),
            float(_value(
                values,
                (
                    "COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT"
                    if "COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT" in values
                    else "CORE_COPY_MAX_SINGLE_LIQUIDATION_LOSS_PCT"
                ),
                getattr(config, "COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT", 0.08),
            )),
        ),
        deep_bag_event_pct=float(_value(values, "COPY_DEEP_BAG_EVENT_PCT", 0.08)),
        deep_bag_event_min_hours=float(_value(values, "COPY_DEEP_BAG_EVENT_MIN_HOURS", 4.0)),
        deep_bag_long_hours=float(_value(values, "COPY_DEEP_BAG_LONG_HOURS", 24.0)),
        official_perp_min_return_30d=float(_value(
            values, "OFFICIAL_PERP_MIN_RETURN_30D", 0.20,
        )),
        official_perp_min_return_7d=float(_value(
            values, "OFFICIAL_PERP_MIN_RETURN_7D", 0.05,
        )),
        official_perp_long_history_days=int(_value(
            values, "OFFICIAL_PERP_LONG_HISTORY_DAYS", 28,
        )),
        official_perp_short_history_days=int(_value(
            values, "OFFICIAL_PERP_SHORT_HISTORY_DAYS", 7,
        )),
        official_perp_boundary_max_gap_hours=float(_value(
            values, "OFFICIAL_PERP_BOUNDARY_MAX_GAP_HOURS", 36,
        )),
        core_min_dynamic_copy_return_30d=float(_value(
            values, "CORE_MIN_DYNAMIC_COPY_RETURN_30D", 0.10,
        )),
        core_min_dynamic_copy_return_7d=float(_value(
            values, "CORE_MIN_DYNAMIC_COPY_RETURN_7D", 0.03,
        )),
        portfolio_min_return_30d=float(_value(
            values, "CORE_PORTFOLIO_MIN_RETURN_30D", 0.10,
        )),
        portfolio_min_return_7d=float(_value(
            values, "CORE_PORTFOLIO_MIN_RETURN_7D", 0.03,
        )),
        min_actionable_open_rate=float(_value(values, "SELECTION_MIN_ACTIONABLE_RATE", 0.70)),
        min_capacity_fit=float(_value(values, "SELECTION_MIN_CAPACITY_FIT", 0.75)),
        tune_min_relative_gain=float(_value(values, "AUTO_TUNE_MIN_RELATIVE_GAIN", 0.05)),
        tune_min_shadow_days=int(_value(values, "AUTO_TUNE_APPLY_MIN_SHADOW_DAYS", 14)),
        tune_min_forward_closed=int(_value(values, "AUTO_TUNE_APPLY_MIN_FORWARD_CLOSED", 100)),
    )
