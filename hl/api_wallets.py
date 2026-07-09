"""Wallet list/detail endpoints for the dashboard API."""

import json
import time

from . import config
from . import follow_score
from . import params as params_mod
from .api_common import iso_epoch, q1, qall, recent_roi_pct, score100


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
        "score": _col(row, "raw_score", _col(row, "profile_score")),
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
        "sector_policy_json": _col(row, "sector_policy_json"),
        "sector_copy_json": _col(row, "sector_copy_json"),
    })
    return {
        "rawScore": score100(detail.get("rawScore")),
        "copyScore": score100(detail.get("copyScore")) if detail.get("copyScore") is not None else None,
        "confidencePct": round((detail.get("confidence") or 0.0) * 100, 0),
        "copyPnl": detail.get("copyPnl"),
        "closedN": detail.get("closedN"),
        "sectorPolicy": _sector_policy(row),
        "reasons": detail.get("reasons") or [],
    }


def ep_wallets(db, qs=None):
    qs = qs or {}
    line_native = params_mod.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE
    page = max(0, int((qs.get("page", ["0"]))[0]))
    size = min(100, max(1, int((qs.get("size", ["30"]))[0])))

    if (qs.get("tab", ["followed"]))[0] == "dropped":
        total_row = q1(db,
            "SELECT COUNT(*) c "
            "FROM follow_history fh JOIN profile p ON p.addr=fh.addr "
            "LEFT JOIN watchlist w ON w.addr=fh.addr "
            "WHERE NOT (w.addr IS NOT NULL AND w.score >= ?)", (line_native,))
        total = (total_row["c"] if total_row else 0) or 0
        rows = qall(db,
            "SELECT fh.addr,fh.last_followed_at,fh.last_followed_score,"
            "COALESCE(da.drop_at,p.last_refreshed,fh.last_followed_at) AS drop_at,"
            "COALESCE(w.score,p.score) AS follow_score,p.score AS raw_score,p.status,p.reason,"
            "p.market_type,p.win_rate,p.top_coin,w.rank AS rank,"
            "p.copy_bt_net_pnl,p.copy_bt_win_rate,p.copy_bt_closed_n,p.copy_bt_open_fill_rate,"
            "p.copy_bt_liquidations,p.copy_bt_fee_drag,p.copy_bt_14d_net_pnl,p.copy_bt_14d_closed_n,"
            "p.copy_bt_7d_net_pnl,p.copy_bt_7d_closed_n,p.sector_copy_json,p.sector_policy_json,"
            "l.week_roi,l.mon_roi "
            "FROM follow_history fh JOIN profile p ON p.addr=fh.addr "
            "LEFT JOIN watchlist w ON w.addr=fh.addr "
            "LEFT JOIN leaderboard l ON l.addr=fh.addr "
            "LEFT JOIN ("
            "  SELECT addr,MAX(created_at) AS drop_at FROM pipeline_audit "
            "  WHERE (stage='profile' AND status IN ('retired','rejected')) "
            "     OR (stage='watchlist' AND status IN ('below_line','disabled')) "
            "  GROUP BY addr"
            ") da ON da.addr=fh.addr "
            "WHERE NOT (w.addr IS NOT NULL AND w.score >= ?) "
            "ORDER BY drop_at DESC LIMIT ? OFFSET ?", (line_native, size, page * size))
        out = [{
            "address": r["addr"], "rank": r["rank"], "marketType": r["market_type"] or "crypto",
            "score": score100(r["follow_score"] or 0.0), "rawScore": score100(r["raw_score"] or 0.0),
            "scoreBreakdown": _score_breakdown(r),
            "lastFollowedScore": score100(r["last_followed_score"] or 0.0),
            "lastFollowedAt": iso_epoch(r["last_followed_at"]),
            "dropAt": iso_epoch(r["drop_at"]),
            "dropReason": ("掉出评分线" if r["status"] == "active" else {"inactive": "失活", "blowup_loss": "扛单爆亏",
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
        "), copy_stats AS ("
        "  SELECT f.addr, COUNT(cp.pos_id) AS follow_count,"
        "         SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END) AS closed_n,"
        "         COALESCE(SUM(CASE WHEN status!='open' THEN realized_pnl ELSE unrealized_pnl END),0) AS fwd_net "
        "  FROM page_followed f LEFT JOIN copy_position cp ON cp.addr=f.addr GROUP BY f.addr"
        ") "
        "SELECT w.addr,w.rank,w.market_type,w.score,w.win_rate,w.top_coin,w.worst_single_loss_pct,"
        "COALESCE(c.enabled,1) AS enabled,pr.score AS raw_score,pr.worst_loss_pct,"
        "pr.copy_bt_net_pnl,pr.copy_bt_win_rate,pr.copy_bt_closed_n,pr.copy_bt_open_fill_rate,"
        "pr.copy_bt_liquidations,pr.copy_bt_fee_drag,pr.copy_bt_14d_net_pnl,pr.copy_bt_14d_closed_n,"
        "pr.copy_bt_7d_net_pnl,pr.copy_bt_7d_closed_n,pr.sector_copy_json,pr.sector_policy_json,"
        "l.week_roi,l.mon_roi,"
        "COALESCE(ep7.closed_7d,0) AS closed_7d,"
        "COALESCE(cs.follow_count,0) AS follow_count,"
        "COALESCE(cs.closed_n,0) AS closed_n,"
        "COALESCE(cs.fwd_net,0) AS fwd_net "
        "FROM page_followed w "
        "LEFT JOIN target_controls c ON c.addr=w.addr "
        "LEFT JOIN profile pr ON pr.addr=w.addr "
        "LEFT JOIN leaderboard l ON l.addr=w.addr "
        "LEFT JOIN ep7 ON ep7.addr=w.addr "
        "LEFT JOIN copy_stats cs ON cs.addr=w.addr "
        "ORDER BY w.rank", (line_native, size, page * size, cutoff7d))

    out = []
    for i, r in enumerate(rows):
        worst = r["worst_single_loss_pct"]
        if worst is None:
            worst = (r["worst_loss_pct"] or 0.0) * 100
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
            "closed7d": r["closed_7d"],
            "closedN": r["closed_n"],
            "forwardNetPnl": r["fwd_net"] or 0,
        })
    return {"followLine": score100(line_native), "tab": "followed", "total": total,
            "followed": total, "page": page, "size": size, "wallets": out}


def ep_wallet_detail(db, addr, qs=None):
    w = q1(db, "SELECT rank FROM watchlist WHERE addr=?", (addr,))
    pr = q1(db, "SELECT score,win_rate,n_trades,market_type FROM profile WHERE addr=?", (addr,))
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
    return {
        "address": addr, "rank": (w["rank"] if w else None),
        "marketType": (pr["market_type"] if pr else None),
        "score": score100(pr["score"]) if pr else None,
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
