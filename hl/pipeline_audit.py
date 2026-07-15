"""Small audit helpers for the scanner/follow pipeline.

The tables being audited (`profile`, `watchlist`, `params`, `auto_tune_runs`)
remain the source of truth. This module snapshots the decision trail so the
dashboard/operator can answer "why did this wallet enter/leave/follow?" after a
scan without reverse-engineering transient logs.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from . import config, follow_score, params
from .util import now_iso


def _json(obj) -> str:
    return json.dumps(obj or {}, ensure_ascii=False, sort_keys=True, default=float)


def _delete_stage(db: sqlite3.Connection, stamp: str, source: str, stage: str) -> None:
    db.execute("DELETE FROM pipeline_audit WHERE stamp=? AND source=? AND stage=?", (stamp, source, stage))


def _insert_event(db: sqlite3.Connection, *, stamp: str, source: str, stage: str, addr: str | None = None,
                  rank: int | None = None, status: str | None = None, reason: str | None = None,
                  raw_score: float | None = None, follow_score: float | None = None,
                  payload: dict | None = None) -> None:
    db.execute(
        "INSERT INTO pipeline_audit "
        "(stamp,source,stage,addr,rank,status,reason,raw_score,follow_score,payload_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            stamp,
            source,
            stage,
            (addr or "").lower() if addr else None,
            rank,
            status,
            reason,
            raw_score,
            follow_score,
            _json(payload),
            now_iso(),
        ),
    )


def _addr_filter(addrs: Iterable[str] | None) -> tuple[str, list[str]]:
    vals = sorted({(a or "").lower() for a in (addrs or []) if a})
    if not vals:
        return "", []
    return f" AND lower(addr) IN ({','.join('?' for _ in vals)})", vals


def _fetch_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def record_workset_summary(db: sqlite3.Connection, stamp: str, source: str, breakdown: dict) -> None:
    """Snapshot why this scan profiled this wallet set size."""
    _delete_stage(db, stamp, source, "workset")
    counts = dict(breakdown.get("counts") or {})
    payload = {
        "mode": breakdown.get("mode"),
        "fullScan": bool(breakdown.get("full_scan")),
        "limit": breakdown.get("limit"),
        "dailyRecheckTop": breakdown.get("daily_recheck_top"),
        "counts": counts,
    }
    _insert_event(
        db,
        stamp=stamp,
        source=source,
        stage="workset",
        status="ok",
        reason="profile_workset",
        payload=payload,
    )


def record_prune_summary(db: sqlite3.Connection, stamp: str, source: str, counts: dict) -> None:
    """Snapshot discovery cache pruning performed at scan end."""
    _delete_stage(db, stamp, source, "prune")
    payload = {k: int(v or 0) for k, v in (counts or {}).items()}
    _insert_event(
        db,
        stamp=stamp,
        source=source,
        stage="prune",
        status="ok",
        reason="discovery_cache_prune",
        payload=payload,
    )


def record_profile_snapshot(db: sqlite3.Connection, stamp: str, source: str,
                            addrs: Iterable[str] | None = None) -> None:
    """Snapshot profile gate results for scanned/regated wallets."""
    _delete_stage(db, stamp, source, "profile")
    where, args = _addr_filter(addrs)
    rows = _fetch_dicts(db.execute(
        "SELECT addr,status,reason,score,market_type,net_7d,net_14d,net_30d,net_life,"
        "copy_bt_net_pnl,copy_bt_win_rate,copy_bt_closed_n,copy_bt_open_fill_rate,"
        "copy_bt_liquidations,copy_bt_fee_drag,copy_bt_14d_net_pnl,copy_bt_14d_closed_n,"
        "copy_bt_7d_net_pnl,copy_bt_7d_closed_n,sector_copy_json,sector_policy_json,"
        "copy_expected_return,copy_return_lcb,copy_positive_probability,copy_evidence_days,"
        "copy_recent_return_14d,copy_recent_return_7d,copy_risk_score,execution_score,"
        "last_copyable_open_ms,actionable_open_rate,capacity_fit,data_status,evidence_status,"
        "open_loss_frac,open_win_frac,bag_count,max_bag_days "
        f"FROM profile WHERE 1=1{where} ORDER BY addr",
        args,
    ))
    for r in rows:
        payload = {
            "marketType": r["market_type"],
            "net": {
                "7d": r["net_7d"],
                "14d": r["net_14d"],
                "30d": r["net_30d"],
                "life": r["net_life"],
            },
            "copyBt": {
                "30dNetPnl": r["copy_bt_net_pnl"],
                "30dClosedN": r["copy_bt_closed_n"],
                "14dNetPnl": r["copy_bt_14d_net_pnl"],
                "14dClosedN": r["copy_bt_14d_closed_n"],
                "7dNetPnl": r["copy_bt_7d_net_pnl"],
                "7dClosedN": r["copy_bt_7d_closed_n"],
                "winRate": r["copy_bt_win_rate"],
                "openFillRate": r["copy_bt_open_fill_rate"],
                "liquidations": r["copy_bt_liquidations"],
                "feeDrag": r["copy_bt_fee_drag"],
                "expectedReturn": r["copy_expected_return"],
                "returnLcb": r["copy_return_lcb"],
                "positiveProbability": r["copy_positive_probability"],
                "evidenceDays": r["copy_evidence_days"],
                "recentReturn14d": r["copy_recent_return_14d"],
                "recentReturn7d": r["copy_recent_return_7d"],
                "riskScore": r["copy_risk_score"],
                "executionScore": r["execution_score"],
                "actionableOpenRate": r["actionable_open_rate"],
                "capacityFit": r["capacity_fit"],
            },
            "qualification": {
                "dataStatus": r["data_status"],
                "evidenceStatus": r["evidence_status"],
                "lastCopyableOpenMs": r["last_copyable_open_ms"],
            },
            "sectorCopy": json.loads(r["sector_copy_json"] or "{}"),
            "sectorPolicy": json.loads(r["sector_policy_json"] or "{}"),
            "openState": {
                "openLossFrac": r["open_loss_frac"],
                "openWinFrac": r["open_win_frac"],
                "bagCount": r["bag_count"],
                "maxBagDays": r["max_bag_days"],
            },
        }
        _insert_event(
            db,
            stamp=stamp,
            source=source,
            stage="profile",
            addr=r["addr"],
            status=r["status"],
            reason=r["reason"],
            raw_score=r["score"],
            payload=payload,
        )


def record_follow_line_choice(db: sqlite3.Connection, stamp: str, source: str, choice: dict) -> None:
    _delete_stage(db, stamp, source, "follow_line")
    _insert_event(
        db,
        stamp=stamp,
        source=source,
        stage="follow_line",
        status=choice.get("status"),
        reason=choice.get("reason"),
        follow_score=choice.get("line"),
        payload=choice,
    )


def record_watchlist_snapshot(db: sqlite3.Connection, stamp: str, source: str, follow_line: float,
                              detail_by_addr: dict | None = None) -> None:
    _delete_stage(db, stamp, source, "watchlist")
    rows = _fetch_dicts(db.execute(
        "SELECT w.rank,w.addr,w.score AS follow_score,p.score AS raw_score,p.reason,"
        "COALESCE(c.enabled,1) AS enabled,p.copy_bt_net_pnl,p.copy_bt_14d_net_pnl,p.copy_bt_7d_net_pnl,"
        "p.copy_bt_closed_n,p.copy_bt_14d_closed_n,p.copy_bt_7d_closed_n,p.copy_bt_open_fill_rate,"
        "p.copy_bt_liquidations,p.copy_bt_fee_drag,p.market_type,p.sector_copy_json,p.sector_policy_json "
        "FROM watchlist w LEFT JOIN profile p ON p.addr=w.addr "
        "LEFT JOIN target_controls c ON c.addr=w.addr ORDER BY w.rank"
    ))
    margin_equity_pct = params.load_follow(db).get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT)
    for r in rows:
        detail = (detail_by_addr or {}).get(r["addr"]) or {}
        enabled = int(r["enabled"] if r["enabled"] is not None else 1) == 1
        followed = enabled and float(r["follow_score"] or 0.0) >= float(follow_line or 0.0)
        eligibility = detail.get("follow_eligibility") or follow_score.evaluate_follow_eligibility(
            r, margin_equity_pct=margin_equity_pct,
        )
        if not enabled:
            status, reason = "disabled", "operator_disabled"
        elif not eligibility.get("eligible"):
            status, reason = "below_line", eligibility.get("status")
        elif followed:
            status, reason = "followed", "score_above_follow_line"
        else:
            status, reason = "below_line", "score_below_follow_line"
        payload = {
            "line": follow_line,
            "profileReason": r["reason"],
            "marketType": r["market_type"],
            "copyBt": {
                "30dNetPnl": r["copy_bt_net_pnl"],
                "30dClosedN": r["copy_bt_closed_n"],
                "14dNetPnl": r["copy_bt_14d_net_pnl"],
                "14dClosedN": r["copy_bt_14d_closed_n"],
                "7dNetPnl": r["copy_bt_7d_net_pnl"],
                "7dClosedN": r["copy_bt_7d_closed_n"],
                "openFillRate": r["copy_bt_open_fill_rate"],
                "liquidations": r["copy_bt_liquidations"],
                "feeDrag": r["copy_bt_fee_drag"],
            },
            "sectorCopy": json.loads(r["sector_copy_json"] or "{}"),
            "sectorPolicy": json.loads(r["sector_policy_json"] or "{}"),
            "followEligibility": eligibility,
            "followDetail": detail.get("follow_detail"),
        }
        _insert_event(
            db,
            stamp=stamp,
            source=source,
            stage="watchlist",
            addr=r["addr"],
            rank=r["rank"],
            status=status,
            reason=reason,
            raw_score=r["raw_score"],
            follow_score=r["follow_score"],
            payload=payload,
        )


def record_auto_tune_result(db: sqlite3.Connection, stamp: str, source: str, result: dict) -> None:
    _delete_stage(db, stamp, source, "auto_tune")
    payload = {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "error": result.get("error"),
        "applied": result.get("applied"),
        "appliedSizing": result.get("applied_sizing"),
        "appliedAdd": result.get("applied_add"),
        "mode": result.get("mode"),
        "shadow": result.get("shadow"),
        "eligibleToApply": result.get("eligible_to_apply"),
        "validation": result.get("validation"),
        "proposal": result.get("proposal"),
        "rollback": result.get("rollback"),
        "followedN": result.get("followed_n"),
        "selectedMult": result.get("selected_mult"),
        "params": result.get("params"),
        "addParams": result.get("add_params"),
        "candidateCount": len(result.get("candidates") or []),
        "addCandidateCount": len(result.get("add_candidates") or []),
    }
    _insert_event(
        db,
        stamp=stamp,
        source=source,
        stage="auto_tune",
        status=result.get("status"),
        reason=result.get("reason") or ("applied" if result.get("applied") else "unchanged"),
        payload=payload,
    )
