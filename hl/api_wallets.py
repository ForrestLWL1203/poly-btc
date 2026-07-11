"""Wallet list/detail endpoints for the dashboard API."""

import json
import time

from . import config
from . import follow_score
from . import params as params_mod
from .api_common import iso_epoch, q1, qall, recent_roi_pct, score100

NEW_WATCHLIST_WINDOW_SEC = 12 * 3600


def _col(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _json_obj(raw):
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sector_policy(row):
    policy = _json_obj(_col(row, "sector_policy_json"))
    if not policy:
        return None
    allowed = policy.get("allowed")
    if not isinstance(allowed, list):
        allowed = [
            sector for sector in ("crypto", "stock")
            if isinstance(policy.get(sector), dict) and policy[sector].get("allow")
        ]
        policy = {**policy, "allowed": allowed}
    return policy


def _score_breakdown(row):
    _score, detail = follow_score.compute_follow_score({
        "score": _col(row, "raw_score", _col(row, "profile_score", _col(row, "score"))),
        "copy_bt_net_pnl": _col(row, "copy_bt_net_pnl"),
        "copy_bt_win_rate": _col(row, "copy_bt_win_rate"),
        "copy_bt_closed_n": _col(row, "copy_bt_closed_n"),
        "copy_bt_open_fill_rate": _col(row, "copy_bt_open_fill_rate"),
        "copy_bt_liquidations": _col(row, "copy_bt_liquidations"),
        "copy_bt_fee_drag": _col(row, "copy_bt_fee_drag"),
        "copy_bt_14d_net_pnl": _col(row, "copy_bt_14d_net_pnl"),
        "copy_bt_14d_closed_n": _col(row, "copy_bt_14d_closed_n"),
        "copy_bt_7d_net_pnl": _col(row, "copy_bt_7d_net_pnl"),
        "copy_bt_7d_closed_n": _col(row, "copy_bt_7d_closed_n"),
        "copy_expected_return": _col(row, "copy_expected_return"),
        "copy_return_lcb": _col(row, "copy_return_lcb"),
        "copy_return_volatility": _col(row, "copy_return_volatility"),
        "copy_positive_probability": _col(row, "copy_positive_probability"),
        "copy_evidence_days": _col(row, "copy_evidence_days"),
        "copy_recent_return_14d": _col(row, "copy_recent_return_14d"),
        "copy_recent_return_7d": _col(row, "copy_recent_return_7d"),
        "copy_risk_score": _col(row, "copy_risk_score"),
        "execution_score": _col(row, "execution_score"),
        "actionable_open_rate": _col(row, "actionable_open_rate"),
        "capacity_fit": _col(row, "capacity_fit"),
        "open_probability_48h": _col(row, "open_probability_48h"),
        "sector_policy_json": _col(row, "sector_policy_json"),
        "sector_copy_json": _col(row, "sector_copy_json"),
    })
    return {
        "rawScore": score100(detail.get("rawScore")),
        "copyScore": score100(detail.get("copyScore")) if detail.get("copyScore") is not None else None,
        "confidencePct": round((detail.get("confidence") or 0.0) * 100, 0),
        "copyPnl": detail.get("copyPnl"),
        "closedN": detail.get("closedN"),
        "expectedReturnPct": round(detail.get("expectedReturn") * 100, 2) if detail.get("expectedReturn") is not None else None,
        "returnLcbPct": round(detail.get("returnLcb") * 100, 2) if detail.get("returnLcb") is not None else None,
        "positiveProbabilityPct": round(detail.get("positiveProbability") * 100, 1) if detail.get("positiveProbability") is not None else None,
        "evidenceDays": detail.get("evidenceDays"),
        "riskScore": score100(detail.get("riskScore")) if detail.get("riskScore") is not None else None,
        "executionScore": score100(detail.get("executionScore")) if detail.get("executionScore") is not None else None,
        "sectorPolicy": _sector_policy(row),
        "reasons": detail.get("reasons") or [],
    }


def _is_new_followed(first_followed_at):
    ts = iso_epoch(first_followed_at)
    return bool(ts and time.time() - ts <= NEW_WATCHLIST_WINDOW_SEC)


def _published_selection_generation(db):
    """Return the explicit selection generation, or None before the migration cut-over.

    A published generation is authoritative even when it deliberately contains zero Core wallets.  The
    existence check therefore lives on ``scan_generation`` rather than ``follow_selection``; otherwise an
    intentionally empty Core set would incorrectly fall back to the legacy score line.
    """
    try:
        row = q1(
            db,
            "SELECT generation FROM scan_generation "
            "WHERE status='published' AND complete=1 AND is_current=1 "
            "ORDER BY id DESC LIMIT 1",
        )
    except Exception:  # noqa: BLE001 - old read-only DBs may predate the migration
        return None
    return _col(row, "generation") if row else None


def _ms_epoch(value):
    try:
        return float(value) / 1000.0 if value else None
    except (TypeError, ValueError):
        return None


def _ep_selected_wallets(db, generation, role, page, size, line_native):
    """Serve one role from the immutable selection snapshot.

    The page CTE is intentionally selected first so episode/copy-position aggregates only touch the visible
    rows.  This preserves the endpoint's bounded-query behaviour for large registries.
    """
    total_row = q1(
        db,
        "SELECT COUNT(*) c FROM follow_selection fs "
        "WHERE fs.generation=? AND fs.role=?",
        (generation, role),
    )
    total = (_col(total_row, "c") or 0) if total_row else 0
    cutoff7d = int((time.time() - 7 * 86400) * 1000)
    rows = qall(
        db,
        "WITH page_selected AS ("
        "  SELECT fs.addr,fs.role,fs.reason AS selection_reason,fs.utility,"
        "         fs.data_status AS selection_data_status,fs.evidence_status AS selection_evidence_status,"
        "         fs.generation,COALESCE(w.rank,999999) AS sort_rank "
        "  FROM follow_selection fs "
        "  LEFT JOIN target_controls tc ON tc.addr=fs.addr "
        "  LEFT JOIN watchlist w ON w.addr=fs.addr "
        "  WHERE fs.generation=? AND fs.role=? "
        "  ORDER BY sort_rank,fs.utility DESC,fs.addr LIMIT ? OFFSET ?"
        "), ep7 AS ("
        "  SELECT f.addr,COUNT(e.addr) AS closed_7d "
        "  FROM page_selected f LEFT JOIN episode e ON e.addr=f.addr AND e.close_ms>=? GROUP BY f.addr"
        "), ep_all AS ("
        "  SELECT f.addr,COUNT(e.addr) AS episode_total "
        "  FROM page_selected f LEFT JOIN episode e ON e.addr=f.addr GROUP BY f.addr"
        "), copy_stats AS ("
        "  SELECT f.addr,COUNT(cp.pos_id) AS follow_count,"
        "         SUM(CASE WHEN cp.status!='open' THEN 1 ELSE 0 END) AS closed_n,"
        "         COALESCE(SUM(CASE WHEN cp.status!='open' THEN cp.realized_pnl ELSE cp.unrealized_pnl END),0) AS fwd_net "
        "  FROM page_selected f LEFT JOIN copy_position cp ON cp.addr=f.addr GROUP BY f.addr"
        ") "
        "SELECT s.addr,s.role,s.selection_reason,s.utility,s.selection_data_status,"
        "s.selection_evidence_status,s.generation,w.rank,w.market_type,w.score,w.win_rate,w.top_coin,"
        "w.worst_single_loss_pct,COALESCE(tc.enabled,1) AS enabled,p.score AS raw_score,p.worst_loss_pct,"
        "fh.first_followed_at,p.copy_bt_net_pnl,p.copy_bt_win_rate,p.copy_bt_closed_n,"
        "p.copy_bt_open_fill_rate,p.copy_bt_liquidations,p.copy_bt_fee_drag,p.copy_bt_14d_net_pnl,"
        "p.copy_bt_14d_closed_n,p.copy_bt_7d_net_pnl,p.copy_bt_7d_closed_n,p.sector_copy_json,"
        "p.sector_policy_json,p.data_status,p.evidence_status,p.profile_generation,p.evaluated_at,"
        "p.last_copyable_open_ms,p.actionable_open_rate,p.capacity_fit,p.oos_net_pnl,p.oos_max_drawdown,"
        "p.oos_cvar95,p.selection_marginal_utility,p.copy_expected_return,p.copy_return_lcb,"
        "p.copy_return_volatility,p.copy_positive_probability,p.copy_evidence_days,"
        "p.copy_recent_return_14d,p.copy_recent_return_7d,p.copy_risk_score,p.execution_score,"
        "p.open_probability_48h,l.week_roi,l.mon_roi,"
        "COALESCE(ep7.closed_7d,0) AS closed_7d,COALESCE(ep_all.episode_total,0) AS episode_total,"
        "COALESCE(cs.follow_count,0) AS follow_count,COALESCE(cs.closed_n,0) AS closed_n,"
        "COALESCE(cs.fwd_net,0) AS fwd_net "
        "FROM page_selected s LEFT JOIN watchlist w ON w.addr=s.addr "
        "LEFT JOIN target_controls tc ON tc.addr=s.addr LEFT JOIN profile p ON p.addr=s.addr "
        "LEFT JOIN follow_history fh ON fh.addr=s.addr LEFT JOIN leaderboard l ON l.addr=s.addr "
        "LEFT JOIN ep7 ON ep7.addr=s.addr LEFT JOIN ep_all ON ep_all.addr=s.addr "
        "LEFT JOIN copy_stats cs ON cs.addr=s.addr ORDER BY s.sort_rank,s.utility DESC,s.addr",
        (generation, role, size, page * size, cutoff7d),
    )
    out = []
    for i, r in enumerate(rows):
        worst = _col(r, "worst_single_loss_pct")
        if worst is None:
            worst = (_col(r, "worst_loss_pct") or 0.0) * 100
        closed7d = _col(r, "closed_7d") or 0
        if closed7d == 0 and (_col(r, "episode_total") or 0) == 0:
            closed7d = _col(r, "copy_bt_7d_closed_n") or 0
        out.append({
            "followPos": page * size + i + 1,
            "address": _col(r, "addr"),
            "rank": _col(r, "rank"),
            "role": _col(r, "role"),
            "selectionReason": _col(r, "selection_reason"),
            "selectionMarginalUtility": (
                _col(r, "utility") if _col(r, "utility") is not None
                else _col(r, "selection_marginal_utility")
            ),
            "selectionGeneration": _col(r, "generation"),
            "marketType": _col(r, "market_type") or "crypto",
            "score": score100(_col(r, "score") or 0.0),
            "rawScore": score100(_col(r, "raw_score") or 0.0),
            "scoreBreakdown": _score_breakdown(r),
            "roiEqPct": recent_roi_pct(_col(r, "week_roi"), _col(r, "mon_roi")),
            "winRatePct": (_col(r, "win_rate") or 0.0) * 100,
            "worstSingleLossPct": worst,
            "mainCoin": _col(r, "top_coin"),
            "followCount": _col(r, "follow_count") or 0,
            "enabled": bool(_col(r, "enabled", True)),
            "closed7d": closed7d,
            "closedN": _col(r, "closed_n") or 0,
            "forwardNetPnl": _col(r, "fwd_net") or 0,
            "firstFollowedAt": iso_epoch(_col(r, "first_followed_at")),
            "isNew": _is_new_followed(_col(r, "first_followed_at")),
            "dataStatus": _col(r, "selection_data_status") or _col(r, "data_status"),
            "evidenceStatus": _col(r, "selection_evidence_status") or _col(r, "evidence_status"),
            "profileGeneration": _col(r, "profile_generation"),
            "evaluatedAt": iso_epoch(_col(r, "evaluated_at")),
            "lastActionableOpenAt": _ms_epoch(_col(r, "last_copyable_open_ms")),
            "actionableOpenRate": _col(r, "actionable_open_rate"),
            "capacityFit": _col(r, "capacity_fit"),
            "oosNetPnl": _col(r, "oos_net_pnl"),
            "oosMaxDrawdown": _col(r, "oos_max_drawdown"),
            "oosCvar95": _col(r, "oos_cvar95"),
        })
    tab = "followed" if role == "core" else role
    return {
        "selectionMode": True,
        "selectionGeneration": generation,
        "followLine": score100(line_native),
        "tab": tab,
        "total": total,
        "followed": total if role == "core" else None,
        "page": page,
        "size": size,
        "wallets": out,
    }


def ep_wallets(db, qs=None):
    qs = qs or {}
    line_native = params_mod.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE
    page = max(0, int((qs.get("page", ["0"]))[0]))
    size = min(100, max(1, int((qs.get("size", ["30"]))[0])))

    requested_tab = (qs.get("tab", ["followed"]))[0]
    selection_generation = _published_selection_generation(db)
    if selection_generation and requested_tab in {"followed", "core", "challenger", "exit_only"}:
        role = "core" if requested_tab in {"followed", "core"} else requested_tab
        return _ep_selected_wallets(db, selection_generation, role, page, size, line_native)

    if requested_tab == "dropped":
        total_row = q1(db,
            "SELECT COUNT(*) c "
            "FROM follow_history fh JOIN profile p ON p.addr=fh.addr "
            "LEFT JOIN watchlist w ON w.addr=fh.addr "
            "WHERE (? IS NOT NULL AND NOT EXISTS ("
            "  SELECT 1 FROM follow_selection fs WHERE fs.generation=? AND fs.addr=fh.addr "
            "  AND fs.role='core' AND fs.enabled=1"
            ")) OR (? IS NULL AND NOT (w.addr IS NOT NULL AND w.score >= ?))",
            (selection_generation, selection_generation, selection_generation, line_native))
        total = (total_row["c"] if total_row else 0) or 0
        rows = qall(db,
            "WITH drop_events AS ("
            "  SELECT fh0.addr,pa.stamp,pa.source,pa.stage,pa.created_at,"
            "         ROW_NUMBER() OVER (PARTITION BY fh0.addr ORDER BY pa.stamp,pa.id) AS rn "
            "  FROM follow_history fh0 JOIN pipeline_audit pa ON pa.addr=fh0.addr "
            "  WHERE pa.stamp>fh0.last_followed_at AND ("
            "       (pa.stage='profile' AND pa.status IN ('retired','rejected')) "
            "    OR (pa.stage='watchlist' AND pa.status IN ('below_line','disabled')) "
            "    OR (pa.stage='selection' AND pa.status IN ('challenger','exit_only')))"
            ") "
            "SELECT fh.addr,fh.last_followed_at,fh.last_followed_score,"
            "COALESCE(de.stamp,p.last_refreshed,fh.last_followed_at) AS drop_at,"
            "de.source AS drop_source,de.stage AS drop_stage,de.created_at AS drop_decided_at,"
            "COALESCE(w.score,p.score) AS follow_score,p.score AS raw_score,p.status,p.reason,"
            "p.market_type,p.win_rate,p.top_coin,w.rank AS rank,"
            "p.copy_bt_net_pnl,p.copy_bt_win_rate,p.copy_bt_closed_n,p.copy_bt_open_fill_rate,"
            "p.copy_bt_liquidations,p.copy_bt_fee_drag,p.copy_bt_14d_net_pnl,p.copy_bt_14d_closed_n,"
            "p.copy_bt_7d_net_pnl,p.copy_bt_7d_closed_n,p.copy_expected_return,p.copy_return_lcb,"
            "p.copy_return_volatility,p.copy_positive_probability,p.copy_evidence_days,"
            "p.copy_recent_return_14d,p.copy_recent_return_7d,p.copy_risk_score,p.execution_score,"
            "p.actionable_open_rate,p.capacity_fit,p.open_probability_48h,"
            "p.sector_copy_json,p.sector_policy_json,"
            "fs.role AS selection_role,fs.reason AS selection_reason,"
            "l.week_roi,l.mon_roi "
            "FROM follow_history fh JOIN profile p ON p.addr=fh.addr "
            "LEFT JOIN watchlist w ON w.addr=fh.addr "
            "LEFT JOIN leaderboard l ON l.addr=fh.addr "
            "LEFT JOIN follow_selection fs ON fs.generation=? AND fs.addr=fh.addr "
            "LEFT JOIN drop_events de ON de.addr=fh.addr AND de.rn=1 "
            "WHERE (? IS NOT NULL AND NOT COALESCE(fs.role='core' AND fs.enabled=1,0)) "
            "OR (? IS NULL AND NOT (w.addr IS NOT NULL AND w.score >= ?)) "
            "ORDER BY drop_at DESC LIMIT ? OFFSET ?",
            (selection_generation, selection_generation, selection_generation, line_native, size, page * size))
        out = [{
            "address": r["addr"], "rank": r["rank"], "marketType": r["market_type"] or "crypto",
            "score": score100(r["follow_score"] or 0.0), "rawScore": score100(r["raw_score"] or 0.0),
            "scoreBreakdown": _score_breakdown(r),
            "lastFollowedScore": score100(r["last_followed_score"] or 0.0),
            "lastFollowedAt": iso_epoch(r["last_followed_at"]),
            "dropAt": iso_epoch(r["drop_at"]),
            "dropSource": r["drop_source"],
            "dropStage": r["drop_stage"],
            "dropDecidedAt": iso_epoch(r["drop_decided_at"]),
            "dropReason": (r["selection_reason"] or "退回挑战池" if r["selection_role"] in {"challenger", "exit_only"}
                else "掉出评分线" if r["status"] == "active" else {"inactive": "失活", "blowup_loss": "扛单爆亏",
                "spot_hedge": "对冲盘", "not_profitable": "转亏", "irregular": "低频", "grid_dca": "网格",
                "bot_frequency": "高频", "hft_uncopyable": "高频", "spot_dominant": "现货为主"}.get(r["reason"], r["reason"] or "淘汰")),
            "winRatePct": (r["win_rate"] or 0.0) * 100,
            "roiEqPct": recent_roi_pct(r["week_roi"], r["mon_roi"]),
            "mainCoin": r["top_coin"],
        } for r in rows]
        return {"followLine": score100(line_native), "total": total, "tab": "dropped",
                "page": page, "size": size, "wallets": out}

    cutoff7d = int((time.time() - 7 * 86400) * 1000)
    total_row = q1(db, "SELECT COUNT(*) c FROM watchlist WHERE score>=?", (line_native,))
    total = (total_row["c"] if total_row else 0) or 0
    rows = qall(db,
        "WITH page_followed AS ("
        "  SELECT addr,rank,market_type,score,win_rate,top_coin,worst_single_loss_pct "
        "  FROM watchlist WHERE score>=? ORDER BY rank LIMIT ? OFFSET ?"
        "), ep7 AS ("
        "  SELECT f.addr, COUNT(e.addr) AS closed_7d "
        "  FROM page_followed f LEFT JOIN episode e ON e.addr=f.addr AND e.close_ms>=? GROUP BY f.addr"
        "), ep_all AS ("
        "  SELECT f.addr, COUNT(e.addr) AS episode_total "
        "  FROM page_followed f LEFT JOIN episode e ON e.addr=f.addr GROUP BY f.addr"
        "), copy_stats AS ("
        "  SELECT f.addr, COUNT(cp.pos_id) AS follow_count,"
        "         SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END) AS closed_n,"
        "         COALESCE(SUM(CASE WHEN status!='open' THEN realized_pnl ELSE unrealized_pnl END),0) AS fwd_net "
        "  FROM page_followed f LEFT JOIN copy_position cp ON cp.addr=f.addr GROUP BY f.addr"
        ") "
        "SELECT w.addr,w.rank,w.market_type,w.score,w.win_rate,w.top_coin,w.worst_single_loss_pct,"
        "COALESCE(c.enabled,1) AS enabled,pr.score AS raw_score,pr.worst_loss_pct,"
        "fh.first_followed_at,"
        "pr.copy_bt_net_pnl,pr.copy_bt_win_rate,pr.copy_bt_closed_n,pr.copy_bt_open_fill_rate,"
        "pr.copy_bt_liquidations,pr.copy_bt_fee_drag,pr.copy_bt_14d_net_pnl,pr.copy_bt_14d_closed_n,"
        "pr.copy_bt_7d_net_pnl,pr.copy_bt_7d_closed_n,pr.copy_expected_return,pr.copy_return_lcb,"
        "pr.copy_return_volatility,pr.copy_positive_probability,pr.copy_evidence_days,"
        "pr.copy_recent_return_14d,pr.copy_recent_return_7d,pr.copy_risk_score,pr.execution_score,"
        "pr.actionable_open_rate,pr.capacity_fit,pr.open_probability_48h,"
        "pr.sector_copy_json,pr.sector_policy_json,"
        "l.week_roi,l.mon_roi,"
        "COALESCE(ep7.closed_7d,0) AS closed_7d,"
        "COALESCE(ep_all.episode_total,0) AS episode_total,"
        "COALESCE(cs.follow_count,0) AS follow_count,"
        "COALESCE(cs.closed_n,0) AS closed_n,"
        "COALESCE(cs.fwd_net,0) AS fwd_net "
        "FROM page_followed w "
        "LEFT JOIN target_controls c ON c.addr=w.addr "
        "LEFT JOIN profile pr ON pr.addr=w.addr "
        "LEFT JOIN follow_history fh ON fh.addr=w.addr "
        "LEFT JOIN leaderboard l ON l.addr=w.addr "
        "LEFT JOIN ep7 ON ep7.addr=w.addr "
        "LEFT JOIN ep_all ON ep_all.addr=w.addr "
        "LEFT JOIN copy_stats cs ON cs.addr=w.addr "
        "ORDER BY w.rank", (line_native, size, page * size, cutoff7d))

    out = []
    for i, r in enumerate(rows):
        worst = r["worst_single_loss_pct"]
        if worst is None:
            worst = (r["worst_loss_pct"] or 0.0) * 100
        closed7d = r["closed_7d"]
        if (closed7d or 0) == 0 and (r["episode_total"] or 0) == 0:
            closed7d = r["copy_bt_7d_closed_n"] or 0
        out.append({
            "followPos": page * size + i + 1,
            "address": r["addr"], "rank": r["rank"], "marketType": r["market_type"] or "crypto",
            "score": score100(r["score"] or 0.0),
            "rawScore": score100(r["raw_score"] or 0.0),
            "scoreBreakdown": _score_breakdown(r),
            "roiEqPct": recent_roi_pct(r["week_roi"], r["mon_roi"]),
            "winRatePct": (r["win_rate"] or 0.0) * 100,
            "worstSingleLossPct": worst, "mainCoin": r["top_coin"],
            "followCount": r["follow_count"], "enabled": bool(r["enabled"]),
            "closed7d": closed7d,
            "closedN": r["closed_n"],
            "forwardNetPnl": r["fwd_net"] or 0,
            "firstFollowedAt": iso_epoch(r["first_followed_at"]),
            "isNew": _is_new_followed(r["first_followed_at"]),
        })
    return {"followLine": score100(line_native), "tab": "followed", "total": total,
            "followed": total, "page": page, "size": size, "wallets": out}


def ep_wallet_detail(db, addr, qs=None):
    w = q1(db, "SELECT rank,score FROM watchlist WHERE addr=?", (addr,))
    pr = q1(db,
            "SELECT score,win_rate,n_trades,market_type,"
            "copy_bt_net_pnl,copy_bt_win_rate,copy_bt_closed_n,copy_bt_open_fill_rate,"
            "copy_bt_liquidations,copy_bt_fee_drag,copy_bt_14d_net_pnl,copy_bt_14d_closed_n,"
            "copy_bt_7d_net_pnl,copy_bt_7d_closed_n,copy_expected_return,copy_return_lcb,"
            "copy_return_volatility,copy_positive_probability,copy_evidence_days,"
            "copy_recent_return_14d,copy_recent_return_7d,copy_risk_score,execution_score,"
            "actionable_open_rate,capacity_fit,open_probability_48h,"
            "sector_copy_json,sector_policy_json "
            "FROM profile WHERE addr=?", (addr,))
    agg = q1(db,
             "SELECT COUNT(*) total_n,"
             "SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END) closed_n,"
             "SUM(CASE WHEN status!='open' AND realized_pnl>0 THEN 1 ELSE 0 END) wins,"
             "COALESCE(SUM(CASE WHEN status!='open' THEN realized_pnl ELSE 0 END),0) realized,"
             "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_n,"
             "COALESCE(SUM(CASE WHEN status='open' THEN unrealized_pnl ELSE 0 END),0) open_u "
             "FROM copy_position WHERE addr=?", (addr,))
    n = (agg["closed_n"] if agg else 0) or 0
    win_n = (agg["wins"] if agg else 0) or 0
    realized = (agg["realized"] if agg else 0.0) or 0.0
    open_n = (agg["open_n"] if agg else 0) or 0
    open_u = (agg["open_u"] if agg else 0.0) or 0.0
    total_recs = (agg["total_n"] if agg else 0) or 0
    rp = max(0, int((qs.get("recPage", ["0"]))[0])) if qs else 0
    rs = min(50, max(1, int((qs.get("recSize", ["20"]))[0]))) if qs else 20
    recs = qall(db,
        "SELECT cp.pos_id,cp.coin,cp.side,cp.status,cp.realized_pnl,cp.unrealized_pnl,cp.opened_at "
        "FROM copy_position cp WHERE cp.addr=? ORDER BY cp.opened_at DESC LIMIT ? OFFSET ?",
        (addr, rs, rp * rs))
    final_score = w["score"] if (w and w["score"] is not None) else (pr["score"] if pr else None)
    return {
        "address": addr, "rank": (w["rank"] if w else None),
        "marketType": (pr["market_type"] if pr else None),
        "score": score100(final_score) if final_score is not None else None,
        "scoreBreakdown": _score_breakdown(pr) if pr else {},
        "scoredWinRatePct": (pr["win_rate"] * 100) if (pr and pr["win_rate"] is not None) else None,
        "scoredTrades": (pr["n_trades"] if pr else None),
        "forwardWinRatePct": (win_n / n * 100) if n else None,
        "closedN": n, "winN": win_n, "lossN": n - win_n,
        "realizedPnl": realized, "openN": open_n, "openUnrealized": open_u,
        "netPnl": realized + open_u,
        "recordsTotal": total_recs, "recPage": rp, "recSize": rs,
        "records": [{
            "id": r["pos_id"], "coin": r["coin"], "side": r["side"], "status": r["status"],
            "pnl": (r["realized_pnl"] or 0.0) if r["status"] != "open" else (r["unrealized_pnl"] or 0.0),
            "openedAt": r["opened_at"],
        } for r in recs],
    }
