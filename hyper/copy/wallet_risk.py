"""Shared source-wallet high-water breaker state machine.

The live Observer and every strict replay feed the same normalized member-cycle equity into this module.
It is intentionally free of database and execution code: callers persist the returned state and execute the
requested freeze/reduce/exit action using their own book implementation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from hyper import config


@dataclass(frozen=True)
class HighWaterPolicy:
    freeze_drawdown: float = config.WALLET_HWM_FREEZE_DD_PCT
    reduce_drawdown: float = config.WALLET_HWM_REDUCE_DD_PCT
    exit_drawdown: float = config.WALLET_HWM_EXIT_DD_PCT
    release_drawdown: float = config.WALLET_HWM_RELEASE_DD_PCT
    cooldown_ms: int = int(config.WALLET_HWM_EXIT_COOLDOWN_DAYS * 86_400_000)


def new_high_water_state(
    *, membership_cycle: str, baseline_equity: float, pnl_baseline: float = 0.0,
    selection_generation: str | None = None, now_ms: int = 0,
) -> dict:
    baseline = max(1.0, float(baseline_equity or 0.0))
    return {
        "membership_cycle": str(membership_cycle),
        "selection_generation": selection_generation,
        "baseline_equity": baseline,
        "pnl_baseline": float(pnl_baseline or 0.0),
        "high_water_equity": baseline,
        "current_equity": baseline,
        "drawdown_frac": 0.0,
        "breaker_stage": 0,
        "reduced_in_cycle": False,
        "cooldown_until_ms": None,
        "started_ms": int(now_ms or 0),
    }


def advance_high_water(
    state: dict,
    *,
    current_equity: float,
    now_ms: int,
    policy: HighWaterPolicy | None = None,
    retention_passed: bool = False,
) -> tuple[dict, str | None]:
    """Advance a monotonic member-cycle breaker and return an execution action.

    Stage 1 may release only after drawdown recovers inside 2% *and* a new complete scan has passed the
    retention surface. Stage 2 never re-expands in the same cycle. Stage 3 persists through its cooldown.
    """
    policy = policy or HighWaterPolicy()
    out = dict(state or {})
    baseline = max(1.0, float(out.get("baseline_equity") or 0.0))
    current = float(current_equity)
    high = max(baseline, float(out.get("high_water_equity") or baseline), current)
    drawdown = max(0.0, (high - current) / baseline)
    prior_stage = max(0, int(out.get("breaker_stage") or 0))
    stage = prior_stage
    action = None

    if drawdown >= policy.exit_drawdown:
        stage = 3
    elif drawdown >= policy.reduce_drawdown:
        stage = max(stage, 2)
    elif drawdown >= policy.freeze_drawdown:
        stage = max(stage, 1)
    elif stage == 1 and drawdown <= policy.release_drawdown and retention_passed:
        stage = 0

    if stage == 3:
        action = "exit_all"
    elif stage == 2 and not bool(out.get("reduced_in_cycle")):
        action = "reduce_half"
    elif stage == 1 and prior_stage == 0:
        action = "freeze_new"
    elif stage == 0 and prior_stage == 1:
        action = "release_freeze"

    out.update({
        "high_water_equity": high,
        "current_equity": current,
        "drawdown_frac": drawdown,
        "breaker_stage": stage,
    })
    if stage == 3 and (prior_stage < 3 or not out.get("cooldown_until_ms")):
        out["cooldown_until_ms"] = max(
            int(out.get("cooldown_until_ms") or 0), int(now_ms) + int(policy.cooldown_ms),
        )
    return out, action


def policy_snapshot(policy: HighWaterPolicy | None = None) -> dict:
    return asdict(policy or HighWaterPolicy())
