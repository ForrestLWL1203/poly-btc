"""Read-only Dashboard projections for the AI risk radar."""

import json
import time

from .api_common import iso_epoch, qall, q1
from .credentials import public_wrap_key


def _loads(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _assessment(row):
    return {
        "id": row["assessment_id"], "assessedForMs": row["assessed_for_ms"], "status": row["status"],
        "model": row["model"], "bullishScore": row["bullish_score"], "bearishScore": row["bearish_score"],
        "confidence": row["confidence"], "regime": row["regime"], "riskyDirection": row["risky_direction"],
        "blockSide": row["block_side"], "activeBlock": bool(row["active_block"]),
        "confirmationMode": row["confirmation_mode"], "validUntilMs": row["valid_until_ms"],
        "reason": row["reason"], "evidence": _loads(row["evidence_json"], []),
        "invalidatingConditions": _loads(row["invalidation_json"], []), "latencyMs": row["latency_ms"],
        "error": row["error"], "createdAt": row["created_at"],
    }


def ep_risk_radar(db, qs=None):
    state = q1(db, "SELECT * FROM market_risk_state WHERE id=1")
    worker = q1(db, "SELECT state,heartbeat_at FROM process_status WHERE name='observer'")
    heartbeat = iso_epoch(worker["heartbeat_at"]) if worker else None
    worker_available = bool(heartbeat and time.time() - heartbeat <= 45)
    latest = qall(db, "SELECT * FROM market_risk_assessment ORDER BY assessed_for_ms DESC,assessment_id DESC LIMIT 24")
    totals = q1(db, "SELECT COUNT(*) total,COALESCE(SUM(would_block),0) blocked,"
                    "COALESCE(SUM(CASE WHEN outcome='avoided_loss' THEN 1 ELSE 0 END),0) avoided_losses,"
                    "COALESCE(SUM(CASE WHEN outcome='missed_profit' THEN 1 ELSE 0 END),0) missed_profits,"
                    "COALESCE(-SUM(CASE WHEN would_block=1 AND status='resolved' THEN net_pnl ELSE 0 END),0) net_benefit "
                    "FROM market_risk_intent")
    return {
        "mode": state["mode"] if state else "off", "status": state["status"] if state else "stopped",
        "connectionStatus": state["connection_status"] if state else "not_configured",
        "currentAssessmentId": state["current_assessment_id"] if state else None,
        "blockSide": state["block_side"] if state else None, "riskScore": state["risk_score"] if state else None,
        "confirmationMode": state["confirmation_mode"] if state else None,
        "validUntilMs": state["valid_until_ms"] if state else None,
        "lastAssessedAt": state["last_assessed_at"] if state else None,
        "lastError": state["last_error"] if state else None,
        "workerAvailable": worker_available,
        "workerState": worker["state"] if worker else "stopped",
        "summary": {"intents": totals["total"] if totals else 0, "wouldBlock": totals["blocked"] if totals else 0,
                    "avoidedLosses": totals["avoided_losses"] if totals else 0,
                    "missedProfits": totals["missed_profits"] if totals else 0,
                    "hypotheticalNetBenefit": totals["net_benefit"] if totals else 0.0},
        "assessments": [_assessment(row) for row in latest],
    }


def ep_risk_intents(db, qs):
    limit = max(1, min(200, int((qs.get("limit", [100]) or [100])[0])))
    rows = qall(db, "SELECT i.*,a.bullish_score,a.bearish_score,a.block_side,cp.unrealized_pnl FROM market_risk_intent i "
                    "LEFT JOIN market_risk_assessment a ON a.assessment_id=i.assessment_id "
                    "LEFT JOIN copy_position cp ON cp.pos_id=i.pos_id "
                    "ORDER BY i.intent_id DESC LIMIT ?", (limit,))
    return {"intents": [{
        "id": r["intent_id"], "positionId": r["pos_id"], "wallet": r["addr"], "coin": r["coin"],
        "side": r["side"], "assessmentId": r["assessment_id"], "riskScore": r["risk_score"],
        "wouldBlock": bool(r["would_block"]), "confirmationMode": r["confirmation_mode"],
        "decisionReason": r["decision_reason"], "openedAt": r["opened_at"], "status": r["status"],
        "realizedPnl": r["realized_pnl"], "netPnl": r["net_pnl"], "outcome": r["outcome"],
        "estimatedPnl": r["unrealized_pnl"] if r["status"] == "open" else None,
        "resolvedAt": r["resolved_at"], "bullishScore": r["bullish_score"],
        "bearishScore": r["bearish_score"], "blockSide": r["block_side"],
    } for r in rows]}


def ep_risk_thresholds(db):
    rows = qall(db, "SELECT i.side,i.status,i.net_pnl,a.block_side,MAX(a.bullish_score,a.bearish_score) risk "
                    "FROM market_risk_intent i LEFT JOIN market_risk_assessment a ON a.assessment_id=i.assessment_id")
    result = []
    for threshold in (60, 65, 70, 75, 80, 85, 90):
        candidates = [r for r in rows if r["block_side"] == r["side"] and (r["risk"] or 0) >= threshold]
        resolved = [r for r in candidates if r["status"] == "resolved" and r["net_pnl"] is not None]
        result.append({"threshold": threshold, "wouldBlock": len(candidates), "resolved": len(resolved),
                       "avoidedLosses": sum(1 for r in resolved if r["net_pnl"] < 0),
                       "missedProfits": sum(1 for r in resolved if r["net_pnl"] > 0),
                       "hypotheticalNetBenefit": -sum(r["net_pnl"] for r in resolved)})
    return {"comparison": result, "note": "threshold-only retrospective estimate; production shadow labels use confirmed entry-time decisions"}


def ep_connections(db):
    credential = q1(db, "SELECT provider,status,last_error,created_at,updated_at,last_validated_at "
                        "FROM provider_credential WHERE provider='deepseek'")
    balance = q1(db, "SELECT currency,total_balance,granted_balance,topped_up_balance,is_available,"
                     "estimated_days,estimated_requests,error,checked_at FROM provider_balance_snapshot "
                     "WHERE provider='deepseek' ORDER BY balance_id DESC LIMIT 1")
    worker = q1(db, "SELECT heartbeat_at FROM process_status WHERE name='observer'")
    heartbeat = iso_epoch(worker["heartbeat_at"]) if worker else None
    provider_status = ("insufficient_balance" if balance and not balance["is_available"]
                       else credential["status"] if credential else "not_configured")
    return {"workerAvailable": bool(heartbeat and time.time() - heartbeat <= 45), "deepseek": {
        "configured": bool(credential), "status": provider_status,
        "lastError": credential["last_error"] if credential else None,
        "updatedAt": credential["updated_at"] if credential else None,
        "lastValidatedAt": credential["last_validated_at"] if credential else None,
        "balance": ({"currency": balance["currency"], "total": balance["total_balance"],
                     "granted": balance["granted_balance"], "toppedUp": balance["topped_up_balance"],
                     "isAvailable": bool(balance["is_available"]), "estimatedDays": balance["estimated_days"],
                     "estimatedRequests": balance["estimated_requests"], "checkedAt": balance["checked_at"],
                     "error": balance["error"]} if balance else None),
    }, "hyperliquid": {"status": "placeholder", "method": "wallet_connect_plus_agent_wallet"}}


def ep_credential_wrap_key(db):
    try:
        return {"ready": True, **public_wrap_key(db)}
    except (OSError, RuntimeError, ValueError):
        return {"ready": False, "reason": "observer_not_initialized"}
