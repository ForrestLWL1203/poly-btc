"""Small audit helpers for the scanner/follow pipeline.

The tables being audited (`profile`, `watchlist`, `params`, `auto_tune_runs`)
remain the source of truth. This module snapshots the decision trail so the
dashboard/operator can answer "why did this wallet enter/leave/follow?" after a
scan without reverse-engineering transient logs.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import sqlite3
from typing import Iterable

from hyper import config, params, storage
from hyper.copy.copy_policy import load_copy_policy
from hyper.selection.follow_score import evaluate_follow_eligibility
from hyper.util import now_iso


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
        f"SELECT {storage.PROFILE_COLS} FROM profile WHERE 1=1{where} ORDER BY addr", args,
    ))
    try:
        policy_values = {**params.load_follow(db), **params.load_category(db, "scanner")}
    except sqlite3.Error:
        policy_values = {}
    policy = load_copy_policy(policy_values)
    thresholds = asdict(policy)
    margin_equity_pct = float(policy_values.get("MARGIN_EQUITY_PCT", config.MARGIN_EQUITY_PCT))
    for r in rows:
        qualification = evaluate_follow_eligibility(
            {
                **r,
                "copy_bt_data_status": r.get("data_status"),
                "copy_bt_evidence_status": r.get("evidence_status"),
                "initial_margin_equity": float(config.INITIAL_BALANCE),
            },
            margin_equity_pct=margin_equity_pct,
            policy_values=policy_values,
        )
        reason = str(r.get("reason") or qualification.get("status") or "unknown")
        structural_reasons = {
            "spot_dominant", "bot_frequency", "hft_uncopyable", "hft_turnover", "grid_dca",
            "heavy_dca", "too_many_concurrent", "no_copyable_perp_fills",
        }
        if qualification.get("role") == "quarantine" or str(r.get("data_status") or "") != "valid":
            decision_stage, failure_category = "data_validation", "data_error"
        elif qualification.get("hardRisk"):
            decision_stage, failure_category = "copy_risk", "hard_risk_exit"
        elif reason in structural_reasons:
            decision_stage, failure_category = "structure_filter", "business_reject"
        elif qualification.get("coreEligible"):
            decision_stage, failure_category = "personal_core", "passed"
        elif qualification.get("role") == "challenger":
            decision_stage, failure_category = "copy_qualification", "soft_retention_failure"
        else:
            decision_stage, failure_category = "copy_qualification", "business_reject"
        first_hard_failure = (
            qualification.get("status") if failure_category in {"data_error", "hard_risk_exit"}
            else (reason if reason in structural_reasons else None)
        )
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
                "campaigns30d": qualification.get("campaigns", {}).get(30),
            },
            "followEligibility": qualification,
            "decisionAudit": {
                "stage": decision_stage,
                "failureCategory": failure_category,
                "firstHardFailure": first_hard_failure,
                "thresholds": thresholds,
                "actual": {
                    "return30d": (qualification.get("returns") or {}).get(30),
                    "closed30d": r.get("copy_bt_closed_n"),
                    "closed14d": r.get("copy_bt_14d_closed_n"),
                    "closed7d": r.get("copy_bt_7d_closed_n"),
                    "campaignWinRate30d": (qualification.get("winRates") or {}).get(30),
                    "campaignWinRateLcb30d": qualification.get("winRateLcb30"),
                    "intratradeDrawdown": r.get("copy_intratrade_max_drawdown"),
                    "failedDeepEvents": r.get("copy_failed_deep_bag_n"),
                    "actionableOpenRate": r.get("actionable_open_rate"),
                    "capacityFit": r.get("capacity_fit"),
                },
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
