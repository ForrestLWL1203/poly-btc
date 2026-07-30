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
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
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

    A healthy complete assessment clears low/medium.  High is durable.  Deferred
    evidence records a data block without advancing financial confirmations.
    """
    previous_level = str(previous_level or NORMAL)
    previous_count = max(0, int(previous_count or 0))
    previous_reasons = tuple(str(item) for item in (previous_reasons or ()) if item)
    if previous_level == HIGH:
        return RiskAssessment(
            HIGH, previous_reasons, previous_count,
            previous_first_confirmed_at, assessed_at, None, bool(complete),
            "high_persisted",
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
            "permanent_high" if kind == HIGH else "recoverable_unavailable",
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
    rows = db.execute(
        "SELECT pos_id,status,realized_pnl,COALESCE(unrealized_pnl,0),was_liq,"
        "opening_account_equity,closed_at FROM copy_position WHERE lower(addr)=lower(?)",
        (addr,),
    ).fetchall()
    evidence = {
        "closedPnl7d": 0.0, "closedPnl30d": 0.0,
        "closedN7d": 0, "closedN30d": 0, "openUnrealized": 0.0,
        "conservativePnl7d": 0.0, "conservativePnl30d": 0.0,
        "catastrophicPositionIds": [],
    }
    for row in rows:
        pos_id, status, realized, unrealized, was_liq, opening_equity, closed_at = row
        if status == "open":
            evidence["openUnrealized"] += float(unrealized or 0.0)
            continue
        closed_dt = _parse_time(closed_at)
        age_days = (now - closed_dt).total_seconds() / 86400.0 if closed_dt else None
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
    open_loss = min(0.0, evidence["openUnrealized"])
    evidence["conservativePnl7d"] = evidence["closedPnl7d"] + open_loss
    evidence["conservativePnl30d"] = evidence["closedPnl30d"] + open_loss
    return evidence


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
    if assessment.level == HIGH:
        event_reason = assessment.reasons[0] if assessment.reasons else "wallet_high_risk"
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
