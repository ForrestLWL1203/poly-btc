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

from . import follow_score
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


def record_profile_snapshot(db: sqlite3.Connection, stamp: str, source: str,
                            addrs: Iterable[str] | None = None) -> None:
    """Snapshot profile gate results for scanned/regated wallets."""
    _delete_stage(db, stamp, source, "profile")
    where, args = _addr_filter(addrs)
    rows = _fetch_dicts(db.execute(
        "SELECT addr,status,reason,score,market_type,net_7d,net_14d,net_30d,net_life,"
        "copy_bt_net_pnl,copy_bt_win_rate,copy_bt_closed_n,copy_bt_open_fill_rate,"
        "copy_bt_liquidations,copy_bt_fee_drag,copy_bt_14d_net_pnl,copy_bt_14d_closed_n,"
        "copy_bt_7d_net_pnl,copy_bt_7d_closed_n,open_loss_frac,open_win_frac,bag_count,max_bag_days "
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
            },
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


def record_watchlist_snapshot(db: sqlite3.Connection, stamp: str, source: str, follow_line: float) -> None:
    _delete_stage(db, stamp, source, "watchlist")
    rows = _fetch_dicts(db.execute(
        "SELECT w.rank,w.addr,w.score AS follow_score,p.score AS raw_score,p.reason,"
        "COALESCE(c.enabled,1) AS enabled,p.copy_bt_net_pnl,p.copy_bt_14d_net_pnl,p.copy_bt_7d_net_pnl,"
        "p.copy_bt_closed_n,p.copy_bt_14d_closed_n,p.copy_bt_7d_closed_n,p.market_type "
        "FROM watchlist w LEFT JOIN profile p ON p.addr=w.addr "
        "LEFT JOIN target_controls c ON c.addr=w.addr ORDER BY w.rank"
    ))
    for r in rows:
        enabled = int(r["enabled"] if r["enabled"] is not None else 1) == 1
        followed = enabled and float(r["follow_score"] or 0.0) >= float(follow_line or 0.0)
        eligibility = follow_score.evaluate_follow_eligibility(r)
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
            },
            "followEligibility": eligibility,
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
        "applied": result.get("applied"),
        "appliedSizing": result.get("applied_sizing"),
        "appliedAdd": result.get("applied_add"),
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
        reason="applied" if result.get("applied") else "unchanged",
        payload=payload,
    )
