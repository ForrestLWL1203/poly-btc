"""Position list/detail endpoints for the dashboard API."""

from . import config
from . import params as params_mod
from .api_common import iso_epoch, q1, qall


def _follow_set_cte() -> str:
    return (
        "WITH follow_set AS ("
        "SELECT w.addr, ROW_NUMBER() OVER (ORDER BY w.rank) AS follow_pos "
        "FROM watchlist w LEFT JOIN target_controls tc ON tc.addr=w.addr "
        "WHERE COALESCE(tc.enabled,1)=1 AND w.score>=?"
        ") "
    )


def ep_positions(db, qs):
    status = (qs.get("status", ["open"])[0])
    line = params_mod.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE
    if status == "closed":
        where, args = ["cp.status!='open'"], []
        for col, key in (("cp.coin", "coin"), ("cp.addr", "wallet"), ("cp.side", "side")):
            if qs.get(key):
                where.append(f"{col}=?")
                args.append(qs[key][0])
        rows = qall(db, _follow_set_cte() +
                    "SELECT cp.pos_id,cp.coin,cp.side,cp.realized_pnl,cp.opened_at,cp.closed_at,"
                    "cp.entry_px,cp.leverage,cp.notional,cp.master_open_px,cp.master_leverage,cp.master_peak_sz,"
                    "cp.was_stopped,cp.was_liq,cp.add_count,cp.addr,w.rank AS wrank,fs.follow_pos "
                    "FROM copy_position cp "
                    "LEFT JOIN watchlist w ON w.addr=cp.addr "
                    "LEFT JOIN follow_set fs ON fs.addr=cp.addr "
                    "WHERE " + " AND ".join(where) +
                    " ORDER BY cp.closed_at DESC LIMIT 100", tuple([line] + args))
        out = []
        for r in rows:
            o, c = iso_epoch(r["opened_at"]), iso_epoch(r["closed_at"])
            pnl = r["realized_pnl"] or 0.0
            entry = r["entry_px"]
            notl = r["notional"] or 0.0
            size = (notl / entry) if entry else 0.0
            close_px = (entry + (1 if r["side"] == "long" else -1) * pnl / size) if size else None
            out.append({"id": f"cls_{r['pos_id']}", "coin": r["coin"], "side": r["side"],
                        "realizedPnl": pnl, "durationSec": int(c - o) if (o and c) else None,
                        "closedAt": c,
                        "result": "win" if pnl > 0 else "loss", "wallet": r["addr"],
                        "closeType": "liq" if r["was_liq"] else ("stop" if r["was_stopped"] else "mirror"),
                        "walletRank": r["wrank"],
                        "followPos": r["follow_pos"],
                        "entry": r["entry_px"], "closePx": close_px, "addCount": r["add_count"] or 0,
                        "leverage": r["leverage"], "notional": r["notional"] or 0.0,
                        "masterEntry": r["master_open_px"], "masterLeverage": r["master_leverage"],
                        "masterNotional": (r["master_peak_sz"] or 0.0) * (r["master_open_px"] or 0.0)})
        sw = "cp.status!='open'" + ("".join(f" AND {c}=?" for c, k in
             (("cp.coin", "coin"), ("cp.addr", "wallet"), ("cp.side", "side")) if qs.get(k)))
        s = q1(db,
            "SELECT COUNT(*) n, "
            "SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins, "
            "COALESCE(SUM(realized_pnl),0) total, AVG(realized_pnl) avg_pnl, "
            "MAX(realized_pnl) best, MIN(realized_pnl) worst, "
            "COALESCE(SUM(CASE WHEN realized_pnl>0 THEN realized_pnl ELSE 0 END),0) gwin, "
            "COALESCE(SUM(CASE WHEN realized_pnl<0 THEN realized_pnl ELSE 0 END),0) gloss, "
            "AVG(CASE WHEN realized_pnl>0 THEN realized_pnl END) avg_win, "
            "AVG(CASE WHEN realized_pnl<0 THEN realized_pnl END) avg_loss, "
            "AVG((julianday(closed_at)-julianday(opened_at))*86400.0) avg_hold "
            "FROM copy_position cp WHERE " + sw, tuple(args))
        n = (s["n"] if s else 0) or 0
        wins = (s["wins"] if s else 0) or 0
        gloss = (s["gloss"] if s else 0.0) or 0.0
        stats = {
            "total": n, "wins": wins, "losses": n - wins,
            "winRatePct": (wins / n * 100) if n else None,
            "totalPnl": (s["total"] if s else 0.0) or 0.0,
            "avgPnl": s["avg_pnl"] if s else None,
            "bestPnl": s["best"] if s else None, "worstPnl": s["worst"] if s else None,
            "avgWin": s["avg_win"] if s else None, "avgLoss": s["avg_loss"] if s else None,
            "profitFactor": ((s["gwin"] or 0.0) / abs(gloss)) if gloss else None,
            "avgHoldSec": s["avg_hold"] if s else None,
        }
        return {"positions": out, "stats": stats}

    where, args = ["cp.status='open'", "cp.size>0", "cp.entry_px IS NOT NULL", "cp.entry_px>0"], []
    for col, key in (("cp.coin", "coin"), ("cp.addr", "wallet"), ("cp.side", "side"),
                     ("COALESCE(w.market_type,pr.market_type)", "type")):
        if qs.get(key):
            where.append(f"{col}=?")
            args.append(qs[key][0])
    rows = qall(db,
        _follow_set_cte() +
        "SELECT cp.pos_id,cp.coin,cp.side,cp.entry_px,cp.leverage,cp.margin,cp.notional,cp.size,"
        "cp.rem_size,cp.liq_px,cp.mark_px,cp.unrealized_pnl,cp.open_lag_sec,cp.addr,cp.add_count,"
        "cp.master_open_px,cp.master_leverage,cp.master_peak_sz,"
        "w.rank AS wrank,COALESCE(w.market_type,pr.market_type) AS mtype,fs.follow_pos "
        "FROM copy_position cp "
        "LEFT JOIN watchlist w ON w.addr=cp.addr "
        "LEFT JOIN profile pr ON pr.addr=cp.addr "
        "LEFT JOIN follow_set fs ON fs.addr=cp.addr "
        "WHERE " + " AND ".join(where) + " ORDER BY cp.opened_at DESC", tuple([line] + args))
    out, float_total = [], 0.0
    for r in rows:
        entry = r["entry_px"] or 0.0
        mark = r["mark_px"] if r["mark_px"] else entry
        held = (r["rem_size"] / r["size"]) if r["size"] else 1.0
        margin = (r["margin"] or 0.0) * held
        upnl = r["unrealized_pnl"] if r["unrealized_pnl"] is not None else 0.0
        float_total += upnl
        liq = r["liq_px"]
        liq_dist = (-abs(liq / mark - 1) * 100) if (liq and mark) else None
        out.append({
            "id": f"pos_{r['pos_id']}", "coin": r["coin"], "marketType": r["mtype"] or "crypto",
            "side": r["side"], "entry": entry, "leverage": r["leverage"],
            "notional": (r["notional"] or 0.0) * held, "mark": mark,
            "unrealizedPnl": upnl,
            "unrealizedPctOfMargin": (upnl / margin * 100) if margin else 0.0,
            "wallet": r["addr"], "walletRank": r["wrank"], "followPos": r["follow_pos"],
            "lagSec": r["open_lag_sec"], "liqPx": liq, "liqDistancePct": liq_dist,
            "masterEntry": r["master_open_px"], "masterLeverage": r["master_leverage"],
            "masterNotional": (r["master_peak_sz"] or 0.0) * (r["master_open_px"] or 0.0),
            "addCount": r["add_count"] or 0,
        })
    return {"summary": {"floatingPnl": float_total, "openCount": len(out)}, "positions": out}


def ep_position_detail(db, pos_id):
    p = q1(db, "SELECT pos_id,coin,side,status,entry_px,leverage,margin,size,rem_size,master_open_px,"
               "realized_pnl,unrealized_pnl,was_liq,was_stopped,opened_at,closed_at FROM copy_position WHERE pos_id=?", (pos_id,))
    if not p:
        return {"error": "not_found"}
    lev = p["leverage"] or 1.0
    c = q1(db, "SELECT COUNT(DISTINCT CASE WHEN action='add' THEN master_oid END) m_adds, "
               "COUNT(DISTINCT CASE WHEN action='add' AND ABS(our_qty_delta)>1e-12 THEN master_oid END) our_adds "
               "FROM copy_action WHERE pos_id=?", (pos_id,))
    acts = qall(db, "SELECT ts,action,our_px,our_qty_delta,realized_pnl,master_oid FROM copy_action "
                    "WHERE pos_id=? AND ABS(our_qty_delta) > 1e-12 ORDER BY ts,act_id", (pos_id,))
    act_labels = {"open": "开仓", "add": "加仓", "reduce": "减仓", "close": "平仓"}
    groups = {}
    for a in acts:
        key = a["master_oid"] if a["master_oid"] is not None else f"_n{a['ts']}"
        g = groups.get(key)
        if g is None:
            g = {"action": a["action"], "ts": a["ts"], "px_n": 0.0, "sz": 0.0, "pnl": 0.0, "n": 0}
            groups[key] = g
        sz = abs(a["our_qty_delta"] or 0.0)
        g["px_n"] += (a["our_px"] or 0.0) * sz
        g["sz"] += sz
        g["pnl"] += a["realized_pnl"] or 0.0
        g["n"] += 1
        if a["action"] == "close":
            g["action"] = "close"
    fills = []
    for g in groups.values():
        sz = g["sz"]
        px = (g["px_n"] / sz) if sz else None
        entry = g["action"] in ("open", "add")
        fills.append({
            "atSec": (g["ts"] or 0) / 1000.0, "action": g["action"],
            "actionLabel": act_labels.get(g["action"], g["action"]),
            "fillCount": g["n"], "px": px, "qty": sz,
            "margin": (sz * px / lev) if (px and lev) else None,
            "pnl": g["pnl"] if not entry else None,
        })
    return {
        "id": p["pos_id"], "coin": p["coin"], "side": p["side"], "status": p["status"],
        "closeType": "liq" if p["was_liq"] else ("stop" if p["was_stopped"] else "mirror"),
        "masterAdds": (c["m_adds"] if c else 0) or 0, "ourAdds": (c["our_adds"] if c else 0) or 0,
        "masterEntry": p["master_open_px"], "ourEntry": p["entry_px"], "ourLeverage": lev,
        "ourMargin": p["margin"] or 0.0,
        "realizedPnl": p["realized_pnl"], "unrealizedPnl": p["unrealized_pnl"],
        "fills": fills,
    }
