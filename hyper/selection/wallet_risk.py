"""Generation-scoped wallet risk assessment.

Financial risk, structural copyability and incomplete evidence are intentionally
orthogonal.  Only a confirmed catastrophe is permanent; low/medium observations
remain visible without revoking Core entry permission.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Mapping, Optional, Sequence


NORMAL = "normal"
LOW = "low"
MEDIUM = "medium"
HIGH = "high"
UNAVAILABLE = "unavailable"

CATASTROPHIC_REASONS = frozenset({
    "copy_single_liquidation_loss_over_8pct",
    "actual_copy_single_liquidation_loss_over_8pct",
    "actual_copy_cumulative_loss_over_8pct",
    "source_account_liquidated_zero",
    "historical_major_liquidation",
})
DURABLE_HIGH_REASONS = frozenset({
    "copy_single_liquidation_loss_over_8pct",
    "actual_copy_single_liquidation_loss_over_8pct",
    "source_account_liquidated_zero",
    "historical_major_liquidation",
})
UNAVAILABLE_REASONS = frozenset({
    "source_zero_equity_no_positions",
    "wallet_funds_withdrawn",
})
IMMEDIATE_MEDIUM_REASONS = frozenset({
    "source_30d_closed_pnl_not_positive",
    "source_open_loss_over_50pct",
    "source_30d_conservative_pnl_not_positive",
    "copy_30d_closed_pnl_not_positive",
    "copy_open_loss_over_50pct",
    "rough_copy_30d_conservative_not_profitable",
    "actual_copy_30d_conservative_pnl_not_positive",
    "actual_copy_open_loss_over_50pct",
})
STRUCTURAL_REASONS = frozenset({
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
DEFERRED_REASONS = frozenset({
    "copy_data_error",
    "copy_valuation_incomplete",
    "copy_path_incomplete",
    "strict_evidence_deferred",
    "market_snapshot_incomplete",
    "price_path_incomplete",
})


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def reason_kind(reason: Optional[str], *, deferred: bool = False) -> str:
    reason = str(reason or "").strip()
    if not reason:
        return NORMAL
    if deferred or reason in DEFERRED_REASONS or any(
        token in reason for token in ("data_error", "valuation_incomplete", "path_incomplete")
    ):
        return "deferred"
    if reason in CATASTROPHIC_REASONS:
        return HIGH
    if reason in UNAVAILABLE_REASONS:
        return UNAVAILABLE
    if reason in STRUCTURAL_REASONS or any(
        token in reason for token in (
            "hft", "oid_robot", "grid", "heavy_dca", "spot_hedge",
            "opaque", "extreme_concurrency", "too_many_concurrent",
            "sector_not_executable",
        )
    ):
        return "structural"
    if reason in IMMEDIATE_MEDIUM_REASONS:
        return MEDIUM
    return LOW


@dataclass(frozen=True)
class RiskAssessment:
    level: str
    reasons: tuple[str, ...]
    confirmation_count: int
    first_confirmed_at: Optional[str]
    assessed_at: str
    block_reason: Optional[str]
    complete: bool
    action: str

    @property
    def entry_allowed(self) -> bool:
        return self.level in {NORMAL, LOW, MEDIUM} and not self.block_reason

    @property
    def automatically_exits(self) -> bool:
        return self.level in {HIGH, UNAVAILABLE} or self.block_reason == "structural_unfollowable"


def advance(
    *,
    previous_level: str = NORMAL,
    previous_count: int = 0,
    previous_reasons: Sequence[str] = (),
    previous_first_confirmed_at: Optional[str] = None,
    assessed_at: str,
    reason: Optional[str],
    complete: bool = True,
    deferred: bool = False,
    min_confirmation_hours: float = 72.0,
) -> RiskAssessment:
    """Advance risk using independent successful evidence points.

    A healthy complete assessment clears low/medium and recoverable cumulative
    high risk.  A confirmed single catastrophic event remains durable.  Deferred
    evidence records a data block without advancing financial confirmations.
    """
    previous_level = str(previous_level or NORMAL)
    previous_count = max(0, int(previous_count or 0))
    previous_reasons = tuple(str(item) for item in (previous_reasons or ()) if item)
    if (
        previous_level == HIGH
        and any(reason in DURABLE_HIGH_REASONS for reason in previous_reasons)
    ):
        return RiskAssessment(
            HIGH, previous_reasons, previous_count,
            previous_first_confirmed_at, assessed_at, None, bool(complete),
            "durable_high_persisted",
        )

    kind = reason_kind(reason, deferred=deferred or not complete)
    if kind == "deferred":
        return RiskAssessment(
            previous_level, previous_reasons, previous_count,
            previous_first_confirmed_at, assessed_at, "data_incomplete", False,
            "deferred_no_advance",
        )
    if kind == NORMAL:
        return RiskAssessment(
            NORMAL, (), 0, None, assessed_at, None, True,
            "recovered" if previous_level in {LOW, MEDIUM} else "healthy",
        )
    if kind == "structural":
        return RiskAssessment(
            previous_level, (str(reason),), previous_count,
            previous_first_confirmed_at, assessed_at, "structural_unfollowable", True,
            "structural_block",
        )
    if kind in {HIGH, UNAVAILABLE}:
        return RiskAssessment(
            kind, (str(reason),), max(1, previous_count),
            previous_first_confirmed_at or assessed_at, assessed_at, None, True,
            (
                "permanent_high"
                if kind == HIGH and str(reason) in DURABLE_HIGH_REASONS
                else "recoverable_high"
                if kind == HIGH
                else "recoverable_unavailable"
            ),
        )
    if kind == MEDIUM:
        return RiskAssessment(
            MEDIUM, (str(reason),), max(1, previous_count),
            previous_first_confirmed_at or assessed_at, assessed_at, None, True,
            "immediate_medium",
        )

    first_at = previous_first_confirmed_at or assessed_at
    count = max(1, previous_count)
    level = LOW
    action = "low_risk"
    if previous_level in {LOW, MEDIUM} and previous_count > 0:
        first_dt = _parse_time(first_at)
        current_dt = _parse_time(assessed_at)
        independent = bool(
            first_dt and current_dt
            and (current_dt - first_dt).total_seconds() >= float(min_confirmation_hours) * 3600.0
        )
        if independent:
            count = previous_count + 1
            level = MEDIUM
            action = "confirmed_medium"
        else:
            action = "confirmation_interval_pending"
    return RiskAssessment(
        level, (str(reason),), count, first_at, assessed_at, None, True, action,
    )


def registry_state(db, addr: str) -> dict:
    row = db.execute(
        "SELECT COALESCE(risk_level,'normal'),risk_reasons_json,"
        "COALESCE(risk_confirmation_count,0),risk_first_confirmed_at,"
        "risk_assessed_at,risk_block_reason FROM wallet_registry WHERE lower(addr)=lower(?)",
        (addr,),
    ).fetchone()
    if not row:
        return {
            "level": NORMAL, "reasons": (), "confirmationCount": 0,
            "firstConfirmedAt": None, "assessedAt": None, "blockReason": None,
        }
    try:
        reasons = tuple(json.loads(row[1] or "[]"))
    except (TypeError, ValueError):
        reasons = ()
    return {
        "level": row[0] or NORMAL,
        "reasons": reasons,
        "confirmationCount": int(row[2] or 0),
        "firstConfirmedAt": row[3],
        "assessedAt": row[4],
        "blockReason": row[5],
    }


def actual_copy_evidence(db, addr: str, *, now: Optional[datetime] = None) -> dict:
    """Return conservative live-copy 7/30 day evidence and catastrophe proof."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rows = db.execute(
        "SELECT pos_id,status,realized_pnl,COALESCE(unrealized_pnl,0),was_liq,"
        "opening_account_equity,opened_at,closed_at FROM copy_position "
        "WHERE lower(addr)=lower(?) ORDER BY COALESCE(opened_at,closed_at),pos_id",
        (addr,),
    ).fetchall()
    evidence = {
        "closedPnl7d": 0.0, "closedPnl30d": 0.0,
        "closedN7d": 0, "closedN30d": 0,
        "openRealized": 0.0, "openUnrealized": 0.0,
        "conservativePnl7d": 0.0, "conservativePnl30d": 0.0,
        "openingEquity30d": None, "cumulativeLossPct30d": 0.0,
        "catastrophicPositionIds": [],
    }
    for row in rows:
        (
            pos_id, status, realized, unrealized, was_liq, opening_equity,
            opened_at, closed_at,
        ) = row
        closed_dt = _parse_time(closed_at)
        closed_age_days = (
            (now - closed_dt).total_seconds() / 86400.0 if closed_dt else None
        )
        in_30d = status == "open" or (
            closed_age_days is not None and closed_age_days <= 30
        )
        if (
            in_30d and evidence["openingEquity30d"] is None
            and opening_equity is not None and float(opening_equity) > 0
        ):
            evidence["openingEquity30d"] = float(opening_equity)
        if status == "open":
            evidence["openRealized"] += float(realized or 0.0)
            evidence["openUnrealized"] += float(unrealized or 0.0)
            continue
        age_days = closed_age_days
        if age_days is not None and age_days <= 30:
            evidence["closedPnl30d"] += float(realized or 0.0)
            evidence["closedN30d"] += 1
        if age_days is not None and age_days <= 7:
            evidence["closedPnl7d"] += float(realized or 0.0)
            evidence["closedN7d"] += 1
        if (
            was_liq and opening_equity is not None and float(opening_equity) > 0
            and -float(realized or 0.0) / float(opening_equity) >= 0.08
        ):
            evidence["catastrophicPositionIds"].append(int(pos_id))
    open_loss = evidence["openRealized"] + min(0.0, evidence["openUnrealized"])
    evidence["conservativePnl7d"] = evidence["closedPnl7d"] + open_loss
    evidence["conservativePnl30d"] = evidence["closedPnl30d"] + open_loss
    reference_equity = evidence["openingEquity30d"]
    if reference_equity:
        evidence["cumulativeLossPct30d"] = max(
            0.0, -evidence["conservativePnl30d"] / reference_equity,
        )
    return evidence


def actual_copy_reason(
    evidence: Mapping,
    *,
    fallback_reason: Optional[str] = None,
    cumulative_high_loss_pct: float = 0.08,
) -> Optional[str]:
    """Classify actual copy results, including recoverable cumulative loss."""
    if evidence.get("catastrophicPositionIds"):
        return "actual_copy_single_liquidation_loss_over_8pct"
    if (
        int(evidence.get("closedN30d") or 0) >= 2
        and float(evidence.get("cumulativeLossPct30d") or 0.0)
        >= float(cumulative_high_loss_pct)
    ):
        return "actual_copy_cumulative_loss_over_8pct"
    if (
        int(evidence.get("closedN30d") or 0) >= 3
        and float(evidence.get("conservativePnl30d") or 0.0) <= 0
    ):
        return "actual_copy_30d_conservative_pnl_not_positive"
    if (
        float(evidence.get("closedPnl30d") or 0.0) > 0
        and -min(0.0, float(evidence.get("openUnrealized") or 0.0))
        > float(evidence.get("closedPnl30d") or 0.0) * 0.50
    ):
        return "actual_copy_open_loss_over_50pct"
    if (
        int(evidence.get("closedN30d") or 0) in {1, 2}
        and float(evidence.get("conservativePnl30d") or 0.0) < 0
        and not fallback_reason
    ):
        return "actual_copy_negative_insufficient_sample"
    return fallback_reason


def persist(
    db,
    *,
    generation: str,
    addr: str,
    source: str,
    assessment: RiskAssessment,
    evidence: Optional[Mapping] = None,
) -> None:
    """Persist history and latest projection without committing the caller transaction."""
    reasons_json = json.dumps(list(assessment.reasons), separators=(",", ":"))
    evidence_json = json.dumps(dict(evidence or {}), sort_keys=True, separators=(",", ":"), default=float)
    db.execute(
        "INSERT INTO wallet_risk_assessment "
        "(generation,addr,source,risk_level,reasons_json,evidence_json,confirmation_count,"
        "first_confirmed_at,assessed_at,complete,block_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(generation,addr) DO UPDATE SET source=excluded.source,"
        "risk_level=excluded.risk_level,reasons_json=excluded.reasons_json,"
        "evidence_json=excluded.evidence_json,confirmation_count=excluded.confirmation_count,"
        "first_confirmed_at=excluded.first_confirmed_at,assessed_at=excluded.assessed_at,"
        "complete=excluded.complete,block_reason=excluded.block_reason",
        (
            generation, addr.lower(), source, assessment.level, reasons_json, evidence_json,
            assessment.confirmation_count, assessment.first_confirmed_at,
            assessment.assessed_at, 1 if assessment.complete else 0, assessment.block_reason,
        ),
    )
    db.execute(
        "UPDATE wallet_registry SET risk_level=?,risk_reasons_json=?,"
        "risk_confirmation_count=?,risk_first_confirmed_at=?,risk_assessed_at=?,"
        "risk_block_reason=?,updated_at=? WHERE lower(addr)=lower(?)",
        (
            assessment.level, reasons_json, assessment.confirmation_count,
            assessment.first_confirmed_at, assessment.assessed_at,
            assessment.block_reason, assessment.assessed_at, addr,
        ),
    )
    event_reason = assessment.reasons[0] if assessment.reasons else "wallet_high_risk"
    if assessment.level == HIGH and event_reason in DURABLE_HIGH_REASONS:
        db.execute(
            "INSERT INTO wallet_risk_event "
            "(addr,event_type,event_key,evidence_json,first_seen_at,last_seen_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(addr,event_type,event_key) DO UPDATE SET "
            "evidence_json=excluded.evidence_json,last_seen_at=excluded.last_seen_at",
            (
                addr.lower(), "wallet_high_risk", event_reason, evidence_json,
                assessment.assessed_at, assessment.assessed_at,
            ),
        )


def _is_current_published_core(db, addr: str) -> bool:
    from hyper.selection import state as selection_state

    generation = selection_state.latest_published_generation(db)
    if not generation:
        return False
    return bool(db.execute(
        "SELECT 1 FROM follow_selection WHERE generation=? AND lower(addr)=lower(?) "
        "AND lower(role)='core' AND COALESCE(enabled,1)=1 LIMIT 1",
        (generation, addr),
    ).fetchone())


def sync_execution_control(
    db,
    addr: str,
    assessment: RiskAssessment,
) -> None:
    """Apply or release only the execution control owned by wallet risk."""
    if assessment.level == HIGH:
        db.execute(
            "INSERT INTO target_controls "
            "(addr,enabled,intent,intent_requested_at,intent_resolved_at,intent_resolution,updated_at) "
            "VALUES (?,0,'requalify',?,?,?,?) ON CONFLICT(addr) DO UPDATE SET "
            "enabled=0,intent='requalify',intent_resolved_at=excluded.intent_resolved_at,"
            "intent_resolution=excluded.intent_resolution,updated_at=excluded.updated_at",
            (
                addr.lower(), assessment.assessed_at, assessment.assessed_at,
                "high_risk_override", assessment.assessed_at,
            ),
        )
        return
    if not _is_current_published_core(db, addr):
        return
    db.execute(
        "UPDATE target_controls SET enabled=1,intent='active',intent_resolved_at=?,"
        "intent_resolution='cumulative_risk_recovered',updated_at=? "
        "WHERE lower(addr)=lower(?) AND intent='requalify' "
        "AND intent_resolution='high_risk_override'",
        (assessment.assessed_at, assessment.assessed_at, addr),
    )


def assess_actual_copy(
    db,
    *,
    generation: str,
    addr: str,
    source: str,
    assessed_at: str,
    fallback_reason: Optional[str] = None,
    complete: bool = True,
    min_confirmation_hours: float = 72.0,
    cumulative_high_loss_pct: float = 0.08,
) -> tuple[RiskAssessment, dict]:
    """Assess, persist and enforce one wallet from current actual-copy evidence."""
    previous = registry_state(db, addr)
    preserved_fallback = fallback_reason
    if preserved_fallback is None and previous["reasons"]:
        prior_reason = str(previous["reasons"][0] or "")
        if prior_reason and not prior_reason.startswith("actual_copy_"):
            preserved_fallback = prior_reason
    evidence = actual_copy_evidence(db, addr)
    reason = actual_copy_reason(
        evidence,
        fallback_reason=preserved_fallback,
        cumulative_high_loss_pct=cumulative_high_loss_pct,
    )
    actual_evidence_complete = bool(
        reason and str(reason).startswith("actual_copy_")
    )
    assessment = advance(
        previous_level=previous["level"],
        previous_count=previous["confirmationCount"],
        previous_reasons=previous["reasons"],
        previous_first_confirmed_at=previous["firstConfirmedAt"],
        assessed_at=assessed_at,
        reason=reason,
        complete=bool(complete or actual_evidence_complete),
        min_confirmation_hours=min_confirmation_hours,
    )
    persist(
        db,
        generation=generation,
        addr=addr,
        source=source,
        assessment=assessment,
        evidence=evidence,
    )
    sync_execution_control(db, addr, assessment)
    return assessment, evidence
