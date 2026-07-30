"""Core membership hysteresis and replacement policy.

Entry qualification remains owned by :mod:`hyper.selection.pre_strict`.  This
module classifies the *same frozen evidence* for an already published Core and
advances its retention state only after a successful complete generation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from hyper import config
from hyper.selection import wallet_risk


HEALTHY = "healthy"
PROBATION = "probation"
MEDIUM_RISK = "medium_risk"
SAFETY_PENDING = "safety_pending"
SAFETY_FROZEN = "safety_frozen"
EXIT_ONLY = "exit_only"

HARD_FAILURE_REASONS = frozenset({
    "copy_single_liquidation_loss_over_8pct",
    "source_account_liquidated_zero",
    "historical_major_liquidation",
    "source_30d_closed_pnl_not_positive",
    "source_open_loss_over_50pct",
    "source_30d_conservative_pnl_not_positive",
    "copy_30d_closed_pnl_not_positive",
    "copy_open_loss_over_50pct",
    "rough_copy_30d_conservative_not_profitable",
    "sector_not_executable",
    "effective_sector_policy_missing",
    "source_hft",
    "source_oid_robot",
    "source_grid",
    "source_heavy_dca",
    "source_spot_hedged",
    "source_opaque_market",
    "source_extreme_concurrency",
    "bot_frequency",
    "hft_uncopyable",
    "grid_dca",
    "heavy_dca",
    "spot_hedged",
    "opaque_or_outcome",
    "too_many_concurrent",
})

SOFT_FAILURE_REASONS = frozenset({
    "source_episode_evidence_insufficient",
    "copy_episode_evidence_insufficient",
    "source_7d_closed_pnl_not_positive",
    "source_7d_conservative_pnl_not_positive",
    "copy_7d_closed_pnl_not_positive",
    "rough_copy_7d_conservative_not_profitable",
    "activity_not_operational",
    "latest_7d_inactive",
    "active_weeks_below_3_of_4",
    "max_open_gap_over_10d",
    "rough_copy_open_rate_below_floor",
    "strict_copy_open_rate_below_floor",
    "copy_profit_factor_below_1_25",
    "source_lottery_profile_rejected",
    "copy_lottery_profile_rejected",
    "strict_copy_30d_conservative_return_below_floor",
    "strict_copy_7d_conservative_return_below_floor",
    "strict_copy_liquidations_over_3",
    "shared_copy_return_below_floor",
})

DEFERRED_FAILURE_REASONS = frozenset({
    "copy_data_error",
    "copy_valuation_incomplete",
    "copy_path_incomplete",
    "strict_evidence_deferred",
    "market_snapshot_incomplete",
    "price_path_incomplete",
})

HARD_CHECK_REASONS = (
    ("singleLiquidationLossWithinLimit", "copy_single_liquidation_loss_over_8pct"),
    ("sourceClosedProfit30d", "source_30d_closed_pnl_not_positive"),
    ("sourceOpenLossRatio", "source_open_loss_over_50pct"),
    ("sourceConservativeProfit30d", "source_30d_conservative_pnl_not_positive"),
    ("copyClosedProfit30d", "copy_30d_closed_pnl_not_positive"),
    ("copyOpenLossRatio", "copy_open_loss_over_50pct"),
    ("copyConservativeProfit30d", "rough_copy_30d_conservative_not_profitable"),
)


def failure_class(reason: Optional[str], *, deferred: bool = False) -> str:
    """Return a compatibility classification for the risk model."""
    kind = wallet_risk.reason_kind(reason, deferred=deferred)
    if kind == wallet_risk.NORMAL:
        return HEALTHY
    if kind == "deferred":
        return "deferred"
    if kind == wallet_risk.MEDIUM:
        return "medium"
    if kind in {wallet_risk.HIGH, wallet_risk.UNAVAILABLE, "structural"}:
        return "hard"
    return "soft"


def qualification_failure(qualification: Optional[Mapping]) -> tuple[str, Optional[str]]:
    """Classify all checks so an earlier soft failure cannot hide a later hard one."""
    qualification = dict(qualification or {})
    if qualification.get("deferred"):
        reason = qualification.get("firstFailure") or qualification.get("status")
        return "deferred", reason
    checks = dict(qualification.get("checks") or {})
    for key, reason in HARD_CHECK_REASONS:
        if key in checks and not bool(checks[key]):
            return failure_class(reason), reason
    reason = qualification.get("firstFailure")
    if qualification.get("eligible") is True and not reason:
        return HEALTHY, None
    reason = reason or qualification.get("status")
    return failure_class(reason), reason


@dataclass(frozen=True)
class RetentionDecision:
    status: str
    failure_streak: int
    failure_reason: Optional[str]
    started_generation: Optional[str]
    last_generation: Optional[str]
    retain_enabled: bool
    retained_by_hysteresis: bool
    action: str


def advance(
    *,
    previous_status: str = HEALTHY,
    previous_streak: int = 0,
    previous_reason: Optional[str] = None,
    previous_started_generation: Optional[str] = None,
    generation: str,
    scan_kind: str,
    scan_successful: bool,
    reason: Optional[str],
    deferred: bool = False,
    confirmation_eligible: bool = True,
) -> RetentionDecision:
    """Advance one Core retention state.

    Any successful daily/full assessment may advance or clear the state.  The
    caller supplies ``confirmation_eligible`` to enforce the 72-hour separation.
    """
    previous_status = str(previous_status or HEALTHY)
    previous_streak = max(0, int(previous_streak or 0))
    if not scan_successful:
        return RetentionDecision(
            previous_status, previous_streak, previous_reason,
            previous_started_generation, None,
            previous_status not in {EXIT_ONLY, SAFETY_FROZEN},
            previous_status in {PROBATION, MEDIUM_RISK}, "unchanged_failed_scan",
        )

    classification = failure_class(reason, deferred=deferred)
    if classification == "deferred":
        return RetentionDecision(
            previous_status, previous_streak, previous_reason,
            previous_started_generation, None,
            previous_status not in {EXIT_ONLY, SAFETY_FROZEN},
            previous_status in {PROBATION, MEDIUM_RISK}, "unchanged_incomplete_evidence",
        )
    if classification == HEALTHY:
        return RetentionDecision(
            HEALTHY, 0, None, None, generation, True, False,
            "recovered" if previous_streak else "healthy",
        )
    if classification == "hard":
        catastrophic = wallet_risk.reason_kind(reason) == wallet_risk.HIGH
        return RetentionDecision(
            SAFETY_FROZEN if catastrophic else EXIT_ONLY,
            previous_streak, reason, previous_started_generation,
            generation, False, False, "immediate_demotion",
        )
    if classification == "medium":
        return RetentionDecision(
            MEDIUM_RISK, max(1, previous_streak), reason,
            previous_started_generation or generation, generation,
            True, True, "immediate_medium",
        )

    if previous_streak > 0 and not confirmation_eligible:
        return RetentionDecision(
            previous_status if previous_status in {PROBATION, MEDIUM_RISK} else PROBATION,
            previous_streak, reason, previous_started_generation,
            None, True, True, "confirmation_interval_pending",
        )

    streak = previous_streak + 1
    started = previous_started_generation or generation
    if streak >= int(config.CORE_RETENTION_CONFIRMATIONS):
        return RetentionDecision(
            MEDIUM_RISK, streak, reason, started, generation,
            True, True, "confirmed_medium",
        )
    return RetentionDecision(
        PROBATION, streak, reason, started, generation,
        True, True, "probation",
    )


def _account_metrics(validation: Mapping, key: str) -> Mapping:
    return (validation or {}).get(key) or {}


def baseline_protectable(validation: Optional[Mapping]) -> bool:
    """Whether an incumbent shared portfolio is safe enough to receive replacement protection."""
    validation = validation or {}
    failures = set(validation.get("failures") or ())
    if failures & {
        "net_not_positive", "paper_net_not_positive",
        "open_loss_over_50pct", "paper_open_loss_over_50pct",
        "path_coverage", "maintenance_coverage",
    }:
        return False
    for key in ("standardizedAccount", "paperAccount"):
        account = _account_metrics(validation, key)
        if account.get("netPnl30d") is None or float(account["netPnl30d"]) <= 0.0:
            return False
        ratio = account.get("openLossRatio30d")
        if ratio is not None and float(ratio) > 0.50:
            return False
    return True


def replacement_gate(
    baseline_validation: Optional[Mapping],
    proposal_validation: Optional[Mapping],
) -> dict:
    """Distinguish missing shared proof from a baseline proven unsafe.

    Parameter-only revisions intentionally do not claim that an old replay
    validates new parameters. Missing validation therefore fails closed into
    exact retained-membership replay instead of waiving probation protection.
    """
    baseline_validation = dict(baseline_validation or {})
    has_account_proof = all(
        _account_metrics(baseline_validation, key).get("netPnl30d") is not None
        for key in ("standardizedAccount", "paperAccount")
    )
    if not has_account_proof:
        return {
            "eligible": False,
            "reason": "baseline_shared_validation_missing",
        }
    if not baseline_protectable(baseline_validation):
        return {
            "eligible": True,
            "reason": "baseline_shared_safety_unprotectable",
        }
    return replacement_gain(baseline_validation, proposal_validation)


def replacement_gain(
    baseline_validation: Optional[Mapping],
    proposal_validation: Optional[Mapping],
    *,
    min_gain: Optional[float] = None,
) -> dict:
    """Require dual-account 30d PnL gain and non-decreasing 7d returns."""
    min_gain = float(
        config.CORE_REPLACEMENT_MIN_SHARED_GAIN if min_gain is None else min_gain
    )
    baseline_validation = baseline_validation or {}
    proposal_validation = proposal_validation or {}
    failures = []
    detail = {}
    for key in ("standardizedAccount", "paperAccount"):
        base = _account_metrics(baseline_validation, key)
        proposal = _account_metrics(proposal_validation, key)
        base_pnl = base.get("netPnl30d")
        proposal_pnl = proposal.get("netPnl30d")
        base_return7 = base.get("dynamicReturn7d")
        proposal_return7 = proposal.get("dynamicReturn7d")
        if None in (base_pnl, proposal_pnl, base_return7, proposal_return7):
            failures.append(f"{key}:missing")
            continue
        base_pnl = float(base_pnl)
        proposal_pnl = float(proposal_pnl)
        required = base_pnl * (1.0 + min_gain)
        # A protected baseline must itself be economically viable. Invalid
        # baselines are handled by the caller's safety-combination path.
        pnl_ok = base_pnl > 0.0 and proposal_pnl + 1e-9 >= required
        recent_ok = float(proposal_return7) + 1e-12 >= float(base_return7)
        detail[key] = {
            "baselineNetPnl30d": base_pnl,
            "proposalNetPnl30d": proposal_pnl,
            "requiredNetPnl30d": required,
            "baselineReturn7d": float(base_return7),
            "proposalReturn7d": float(proposal_return7),
            "pnlGainPassed": pnl_ok,
            "return7dPassed": recent_ok,
        }
        if not pnl_ok:
            failures.append(f"{key}:30d_gain_below_{min_gain:.0%}")
        if not recent_ok:
            failures.append(f"{key}:7d_decreased")
    return {
        "eligible": not failures,
        "minGain": min_gain,
        "failures": failures,
        "accounts": detail,
    }
