"""Position list/detail endpoints for the dashboard API."""

from .api_common import iso_epoch, q1, qall


def _follow_set_cte() -> str:
    return (
        "WITH current_selection AS ("
        "SELECT generation FROM scan_generation WHERE status='published' AND complete=1 AND is_current=1 "
        "ORDER BY id DESC LIMIT 1"
        "), follow_set AS ("
        "SELECT fs.addr, ROW_NUMBER() OVER (ORDER BY COALESCE(fs.utility,-1e999) DESC,fs.addr) AS follow_pos "
        "FROM follow_selection fs JOIN current_selection sg ON sg.generation=fs.generation "
        "LEFT JOIN target_controls tc ON tc.addr=fs.addr "
        "WHERE fs.role='core' AND fs.enabled=1 AND COALESCE(tc.enabled,1)=1"
        ") "
    )


def _close_type(row) -> str:
    status = row["status"] if "status" in row.keys() else None
    if status == "liquidated":
        return "liq"
    if status == "stopped":
        return "stop"
    return "mirror"


def ep_positions(db, qs):
    status = (qs.get("status", ["open"])[0])
    if status == "closed":
        where, args = ["cp.status!='open'"], []
        for col, key in (("cp.coin", "coin"), ("cp.addr", "wallet"), ("cp.side", "side")):
            if qs.get(key):
                where.append(f"{col}=?")
                args.append(qs[key][0])
        rows = qall(db, _follow_set_cte() +
                    ", closed_base AS ("
                    "SELECT cp.pos_id,cp.coin,cp.side,cp.status,cp.realized_pnl,cp.opened_at,cp.closed_at,"
                    "cp.entry_px,cp.leverage,cp.notional,cp.master_open_px,cp.master_leverage,cp.master_peak_sz,"
                    "cp.was_stopped,cp.was_liq,cp.add_count,cp.addr "
                    "FROM copy_position cp WHERE " + " AND ".join(where) +
                    " ORDER BY cp.closed_at DESC LIMIT 100"
                    ") "
                    "SELECT cb.*,w.rank AS wrank,fs.follow_pos,"
                    "(SELECT SUM(ABS(ca.our_qty_delta)*ca.our_px)/NULLIF(SUM(ABS(ca.our_qty_delta)),0) "
                    " FROM copy_action ca INDEXED BY idx_ca_pos_action_ts "
                    " WHERE ca.pos_id=cb.pos_id AND ca.action IN ('reduce','close') "
                    " AND ABS(ca.our_qty_delta)>1e-12 AND ca.our_px IS NOT NULL) AS exit_px "
                    "FROM closed_base cb "
                    "LEFT JOIN watchlist w ON w.addr=cb.addr "
                    "LEFT JOIN follow_set fs ON fs.addr=cb.addr "
                    "ORDER BY cb.closed_at DESC", tuple(args))
        out = []
        for r in rows:
            o, c = iso_epoch(r["opened_at"]), iso_epoch(r["closed_at"])
            pnl = r["realized_pnl"] or 0.0
            entry = r["entry_px"]
            notl = r["notional"] or 0.0
            size = (notl / entry) if entry else 0.0
            close_px = r["exit_px"]
            if close_px is None:
                close_px = (entry + (1 if r["side"] == "long" else -1) * pnl / size) if size else None
            out.append({"id": f"cls_{r['pos_id']}", "coin": r["coin"], "side": r["side"],
                        "realizedPnl": pnl, "durationSec": int(c - o) if (o and c) else None,
                        "closedAt": c,
                        "result": "win" if pnl > 0 else "loss", "wallet": r["addr"],
                        "closeType": _close_type(r),
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
        "WHERE " + " AND ".join(where) + " ORDER BY cp.opened_at DESC", tuple(args))
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
    acts = qall(db,
        "WITH base AS ("
        "  SELECT act_id,ts,action,our_px,our_qty_delta,realized_pnl,"
        "         CASE WHEN master_oid IS NOT NULL THEN 'm:'||master_oid ELSE 'n:'||ts END AS gkey "
        "  FROM copy_action WHERE pos_id=? AND ABS(our_qty_delta) > 1e-12"
        "), grouped AS ("
        "  SELECT gkey,MIN(ts) AS ts,MIN(act_id) AS first_act_id,"
        "         SUM(ABS(our_qty_delta)) AS sz,"
        "         SUM(COALESCE(our_px,0)*ABS(our_qty_delta)) AS px_n,"
        "         SUM(COALESCE(realized_pnl,0)) AS pnl,"
        "         COUNT(*) AS n,"
        "         MAX(CASE WHEN action='close' THEN 1 ELSE 0 END) AS has_close "
        "  FROM base GROUP BY gkey"
        ") "
        "SELECT g.ts,CASE WHEN g.has_close THEN 'close' ELSE b.action END AS action,"
        "       g.px_n,g.sz,g.pnl,g.n "
        "FROM grouped g JOIN base b ON b.act_id=g.first_act_id "
        "ORDER BY g.ts,g.first_act_id", (pos_id,))
    act_labels = {"open": "开仓", "add": "加仓", "reduce": "减仓", "close": "平仓"}
    fills = []
    for g in acts:
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
        "closeType": _close_type(p),
        "masterAdds": (c["m_adds"] if c else 0) or 0, "ourAdds": (c["our_adds"] if c else 0) or 0,
        "masterEntry": p["master_open_px"], "ourEntry": p["entry_px"], "ourLeverage": lev,
        "ourMargin": p["margin"] or 0.0,
        "realizedPnl": p["realized_pnl"], "unrealizedPnl": p["unrealized_pnl"],
        "fills": fills,
    }
