"""Read-only Dashboard projections for the AI risk radar."""

import json
import time

from . import config
from .api_common import iso_epoch, qall, q1
from .credentials import public_wrap_key
from .util import f


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


def _query_int(qs, key, default):
    try:
        return int((qs.get(key, [default]) or [default])[0])
    except (TypeError, ValueError):
        return default


def ep_risk_radar(db, qs=None):
    qs = qs or {}
    state = q1(db, "SELECT * FROM market_risk_state WHERE id=1")
    worker = q1(db, "SELECT state,heartbeat_at FROM process_status WHERE name='observer'")
    heartbeat = iso_epoch(worker["heartbeat_at"]) if worker else None
    worker_available = bool(heartbeat and time.time() - heartbeat <= 45)
    assessment_size = max(1, min(50, _query_int(qs, "assessmentSize", 10)))
    assessment_total_row = q1(db, "SELECT COUNT(*) total FROM market_risk_assessment")
    assessment_total = int(assessment_total_row["total"] or 0) if assessment_total_row else 0
    assessment_pages = max(1, (assessment_total + assessment_size - 1) // assessment_size)
    assessment_page = min(max(0, _query_int(qs, "assessmentPage", 0)), assessment_pages - 1)
    latest = qall(
        db,
        "SELECT * FROM market_risk_assessment ORDER BY assessed_for_ms DESC,assessment_id DESC LIMIT ? OFFSET ?",
        (assessment_size, assessment_page * assessment_size),
    )
    current = None
    if state and state["current_assessment_id"] is not None:
        current = q1(db, "SELECT * FROM market_risk_assessment WHERE assessment_id=?",
                     (state["current_assessment_id"],))
    if current is None:
        current = q1(db, "SELECT * FROM market_risk_assessment "
                         "ORDER BY assessed_for_ms DESC,assessment_id DESC LIMIT 1")
    legacy = q1(db, "SELECT COUNT(*) total,COALESCE(SUM(would_block),0) blocked,"
                    "COALESCE(SUM(CASE WHEN outcome='avoided_loss' THEN 1 ELSE 0 END),0) avoided_losses,"
                    "COALESCE(SUM(CASE WHEN outcome='missed_profit' THEN 1 ELSE 0 END),0) missed_profits,"
                    "COALESCE(-SUM(CASE WHEN would_block=1 AND status='resolved' THEN net_pnl ELSE 0 END),0) net_benefit "
                    "FROM market_risk_intent")
    episodes = q1(
        db,
        "SELECT COUNT(*) total,COALESCE(SUM(blocked_entries),0) blocked_entries,"
        "COALESCE(SUM(allowed_entries),0) allowed_entries,"
        "COALESCE(SUM(delayed_entry),0) delayed_entries,"
        "COALESCE(SUM(CASE WHEN status='resolved' THEN 1 ELSE 0 END),0) resolved,"
        "COALESCE(SUM(CASE WHEN outcome='improved' THEN 1 ELSE 0 END),0) improved,"
        "COALESCE(SUM(CASE WHEN outcome='harmed' THEN 1 ELSE 0 END),0) harmed,"
        "COALESCE(SUM(CASE WHEN status='resolved' AND baseline_net_pnl<0 AND net_benefit>0 THEN 1 ELSE 0 END),0) avoided_losses,"
        "COALESCE(SUM(CASE WHEN status='resolved' AND baseline_net_pnl>0 AND net_benefit<0 THEN 1 ELSE 0 END),0) missed_profits,"
        "COALESCE(SUM(CASE WHEN status='resolved' THEN baseline_net_pnl ELSE 0 END),0) baseline_pnl,"
        "COALESCE(SUM(CASE WHEN status='resolved' THEN shadow_net_pnl ELSE 0 END),0) shadow_pnl,"
        "COALESCE(SUM(CASE WHEN status='resolved' THEN net_benefit ELSE 0 END),0) net_benefit "
        "FROM market_risk_episode",
    )
    has_v2 = bool(episodes and episodes["total"])
    summary = {
        "intents": episodes["total"] if has_v2 else (legacy["total"] if legacy else 0),
        "wouldBlock": episodes["blocked_entries"] if has_v2 else (legacy["blocked"] if legacy else 0),
        "blockedEntries": episodes["blocked_entries"] if has_v2 else (legacy["blocked"] if legacy else 0),
        "allowedEntries": episodes["allowed_entries"] if has_v2 else 0,
        "delayedEntries": episodes["delayed_entries"] if has_v2 else 0,
        "resolvedEpisodes": episodes["resolved"] if has_v2 else 0,
        "improvedEpisodes": episodes["improved"] if has_v2 else 0,
        "harmedEpisodes": episodes["harmed"] if has_v2 else 0,
        "avoidedLosses": episodes["avoided_losses"] if has_v2 else (legacy["avoided_losses"] if legacy else 0),
        "missedProfits": episodes["missed_profits"] if has_v2 else (legacy["missed_profits"] if legacy else 0),
        "baselinePnl": episodes["baseline_pnl"] if has_v2 else None,
        "shadowPnl": episodes["shadow_pnl"] if has_v2 else None,
        "hypotheticalNetBenefit": episodes["net_benefit"] if has_v2 else (legacy["net_benefit"] if legacy else 0.0),
        "accountingVersion": 2 if has_v2 else 1,
    }
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
        "summary": summary,
        "currentAssessment": _assessment(current) if current else None,
        "assessments": [_assessment(row) for row in latest],
        "assessmentPagination": {
            "page": assessment_page,
            "size": assessment_size,
            "total": assessment_total,
            "totalPages": assessment_pages,
            "retentionLimit": config.RISK_RADAR_MAX_ASSESSMENTS,
            "retentionHours": config.RISK_RADAR_RETENTION_DAYS * 24,
        },
    }


def ep_risk_intents(db, qs):
    qs = qs or {}
    affected_only = str((qs.get("affectedOnly", [""]) or [""])[0]).lower() in {"1", "true", "yes", "on"}
    affected_where = (" WHERE (i.would_block=1 OR COALESCE(e.entry_blocked,0)=1 "
                      "OR COALESCE(e.delayed_entry,0)=1 OR COALESCE(e.blocked_entries,0)>0 "
                      "OR ABS(COALESCE(e.net_benefit,0))>0.000000001)" if affected_only else "")
    legacy_limit = _query_int(qs, "limit", 5)
    size = max(1, min(50, _query_int(qs, "size", legacy_limit)))
    total_row = q1(db, "SELECT COUNT(*) total FROM market_risk_intent i "
                       "LEFT JOIN market_risk_episode e ON e.pos_id=i.pos_id" + affected_where)
    total = int(total_row["total"] or 0) if total_row else 0
    total_pages = max(1, (total + size - 1) // size)
    page = min(max(0, _query_int(qs, "page", 0)), total_pages - 1)
    rows = qall(db, "SELECT i.*,a.bullish_score,a.bearish_score,a.block_side,"
                    "cp.unrealized_pnl,cp.realized_pnl AS baseline_realized,cp.mark_px,cp.entry_px,"
                    "e.episode_id,e.entry_blocked,e.delayed_entry,e.blocked_entries,e.allowed_entries,"
                    "e.shadow_qty,e.shadow_entry_px,e.shadow_realized_pnl,e.shadow_fee,e.baseline_net_pnl,"
                    "e.shadow_net_pnl,e.net_benefit,e.outcome AS episode_outcome,e.status AS episode_status,"
                    "(SELECT COALESCE(SUM(ABS(ca.our_qty_delta*ca.our_px)*?),0) FROM copy_action ca "
                    " WHERE ca.pos_id=i.pos_id AND ca.action IN ('open','add')) AS baseline_entry_fee "
                    "FROM market_risk_intent i "
                    "LEFT JOIN market_risk_assessment a ON a.assessment_id=i.assessment_id "
                    "LEFT JOIN copy_position cp ON cp.pos_id=i.pos_id "
                    "LEFT JOIN market_risk_episode e ON e.pos_id=i.pos_id "
                    + affected_where + " "
                    "ORDER BY i.intent_id DESC LIMIT ? OFFSET ?",
                (config.TAKER_FEE, size, page * size))
    pos_ids = [r["pos_id"] for r in rows]
    actions_by_pos = {pid: [] for pid in pos_ids}
    if pos_ids:
        marks = ",".join("?" for _ in pos_ids)
        actions = qall(
            db,
            "SELECT risk_action_id,pos_id,action,side,source_oid,risk_score,would_block,confirmation_mode,"
            "decision,decision_reason,baseline_qty_delta,baseline_px,shadow_qty_delta,shadow_px,"
            "shadow_realized_pnl,created_at FROM market_risk_action WHERE pos_id IN (" + marks + ") "
            "ORDER BY risk_action_id",
            tuple(pos_ids),
        )
        for a in actions:
            actions_by_pos[a["pos_id"]].append({
                "id": a["risk_action_id"], "action": a["action"], "side": a["side"],
                "sourceOid": a["source_oid"], "riskScore": a["risk_score"],
                "wouldBlock": bool(a["would_block"]), "confirmationMode": a["confirmation_mode"],
                "decision": a["decision"], "decisionReason": a["decision_reason"],
                "baselineQtyDelta": a["baseline_qty_delta"], "baselinePx": a["baseline_px"],
                "shadowQtyDelta": a["shadow_qty_delta"], "shadowPx": a["shadow_px"],
                "shadowRealizedPnl": a["shadow_realized_pnl"], "createdAt": a["created_at"],
            })
    intents = []
    for r in rows:
        baseline_estimate = shadow_estimate = benefit_estimate = None
        if r["status"] == "open" and r["episode_id"] is not None:
            baseline_estimate = f(r["baseline_realized"]) + f(r["unrealized_pnl"]) - f(r["baseline_entry_fee"])
            mark = f(r["mark_px"]) or f(r["entry_px"])
            sign = 1.0 if r["side"] == "long" else -1.0
            shadow_estimate = f(r["shadow_realized_pnl"])
            if f(r["shadow_qty"]) > 0 and mark > 0:
                shadow_estimate += f(r["shadow_qty"]) * (mark - f(r["shadow_entry_px"])) * sign
            benefit_estimate = shadow_estimate - baseline_estimate
        intents.append({
            "id": r["intent_id"], "positionId": r["pos_id"], "wallet": r["addr"], "coin": r["coin"],
            "side": r["side"], "assessmentId": r["assessment_id"], "riskScore": r["risk_score"],
            "wouldBlock": bool(r["would_block"]), "confirmationMode": r["confirmation_mode"],
            "decisionReason": r["decision_reason"], "openedAt": r["opened_at"], "status": r["status"],
            "realizedPnl": r["realized_pnl"], "netPnl": r["net_pnl"],
            "outcome": r["episode_outcome"] or r["outcome"],
            "estimatedPnl": r["unrealized_pnl"] if r["status"] == "open" else None,
            "resolvedAt": r["resolved_at"], "bullishScore": r["bullish_score"],
            "bearishScore": r["bearish_score"], "blockSide": r["block_side"],
            "shadow": ({"status": r["episode_status"], "entryBlocked": bool(r["entry_blocked"]),
                        "delayedEntry": bool(r["delayed_entry"]), "blockedEntries": r["blocked_entries"],
                        "allowedEntries": r["allowed_entries"], "qty": r["shadow_qty"],
                        "entryPx": r["shadow_entry_px"], "fee": r["shadow_fee"],
                        "baselineNetPnl": r["baseline_net_pnl"] if r["episode_status"] == "resolved" else baseline_estimate,
                        "shadowNetPnl": r["shadow_net_pnl"] if r["episode_status"] == "resolved" else shadow_estimate,
                        "netBenefit": r["net_benefit"] if r["episode_status"] == "resolved" else benefit_estimate,
                        "estimated": r["episode_status"] == "open"} if r["episode_id"] is not None else None),
            "actions": actions_by_pos.get(r["pos_id"], []),
        })
    return {"intents": intents, "pagination": {
        "page": page, "size": size, "total": total, "totalPages": total_pages,
        "affectedOnly": affected_only,
    }}


def ep_risk_thresholds(db):
    episodes = qall(db, "SELECT episode_id,side,baseline_net_pnl FROM market_risk_episode WHERE status='resolved'")
    episode_ids = [r["episode_id"] for r in episodes]
    if not episode_ids:
        legacy = qall(db, "SELECT i.side,i.status,i.net_pnl,a.block_side,"
                          "MAX(a.bullish_score,a.bearish_score) risk FROM market_risk_intent i "
                          "LEFT JOIN market_risk_assessment a ON a.assessment_id=i.assessment_id")
        comparison = []
        for threshold in (60, 65, 70, 75, 80, 85, 90):
            candidates = [r for r in legacy if r["block_side"] == r["side"] and f(r["risk"]) >= threshold]
            resolved = [r for r in candidates if r["status"] == "resolved" and r["net_pnl"] is not None]
            comparison.append({"threshold": threshold, "wouldBlock": len(candidates), "resolved": len(resolved),
                               "avoidedLosses": sum(1 for r in resolved if r["net_pnl"] < 0),
                               "missedProfits": sum(1 for r in resolved if r["net_pnl"] > 0),
                               "hypotheticalNetBenefit": -sum(r["net_pnl"] for r in resolved)})
        return {"comparison": comparison,
                "note": "legacy position-level estimate; action-level replay begins with resolved V2 episodes"}
    marks = ",".join("?" for _ in episode_ids)
    actions = qall(
        db,
        "SELECT ra.episode_id,ra.decision_group,ra.action,ra.side,ra.risk_score,ra.baseline_qty_delta,ra.baseline_px,"
        "ra.close_fraction,a.block_side FROM market_risk_action ra "
        "LEFT JOIN market_risk_assessment a ON a.assessment_id=ra.assessment_id "
        "WHERE ra.episode_id IN (" + marks + ") ORDER BY ra.episode_id,ra.risk_action_id",
        tuple(episode_ids),
    )
    by_episode = {eid: [] for eid in episode_ids}
    for row in actions:
        by_episode[row["episode_id"]].append(row)
    result = []
    for threshold in (60, 65, 70, 75, 80, 85, 90):
        blocked_groups = set()
        comparisons = []
        for episode in episodes:
            sign = 1.0 if episode["side"] == "long" else -1.0
            rows = by_episode.get(episode["episode_id"], [])

            def replay(apply_threshold):
                qty = entry = realized = 0.0
                for action in rows:
                    kind, px = action["action"], f(action["baseline_px"])
                    if kind in ("open", "add"):
                        blocked = (apply_threshold is not None and action["block_side"] == action["side"]
                                   and f(action["risk_score"]) >= apply_threshold)
                        if blocked:
                            blocked_groups.add((episode["episode_id"], action["decision_group"]))
                            continue
                        amount = abs(f(action["baseline_qty_delta"]))
                        if amount <= 0 or px <= 0:
                            continue
                        new_qty = qty + amount
                        entry = ((qty * entry + amount * px) / new_qty) if new_qty else px
                        qty = new_qty
                        realized -= amount * px * config.TAKER_FEE
                    elif kind in ("reduce", "close"):
                        fraction = 1.0 if kind == "close" else max(0.0, min(1.0, f(action["close_fraction"])))
                        close_qty = qty * fraction
                        realized += close_qty * (px - entry) * sign - close_qty * px * config.TAKER_FEE
                        qty = max(0.0, qty - close_qty)
                        if qty <= config.FLAT:
                            entry = 0.0
                return realized

            baseline = replay(None)
            shadow = replay(threshold)
            comparisons.append((baseline, shadow, shadow - baseline))
        result.append({"threshold": threshold, "wouldBlock": len(blocked_groups), "resolved": len(comparisons),
                       "avoidedLosses": sum(1 for baseline, _shadow, benefit in comparisons if baseline < 0 and benefit > 0),
                       "missedProfits": sum(1 for baseline, _shadow, benefit in comparisons if baseline > 0 and benefit < 0),
                       "hypotheticalNetBenefit": sum(benefit for _baseline, _shadow, benefit in comparisons)})
    return {"comparison": result,
            "note": "action-level threshold replay; production labels still use confirmed entry-time decisions"}


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
