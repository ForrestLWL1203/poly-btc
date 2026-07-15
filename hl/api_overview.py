"""Overview, equity, insight, and strategy-revision dashboard endpoints."""

import json
import time

from . import config, strategy_revision
from .api_common import iso_epoch, q1, qall
from .api_discovery import followed_count, scanner_status


PROC_STALE_SEC = 90
_GROSS_TRADED_CACHE = {}


def _iso_ago(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


def _db_cache_key(db):
    try:
        row = db.execute("PRAGMA database_list").fetchone()
        path = row["file"] if hasattr(row, "keys") else row[2]
        if path:
            return path
    except Exception:  # noqa: BLE001 - cache key fallback only
        pass
    return id(db)


def _gross_traded(db):
    key = _db_cache_key(db)
    head = q1(db, "SELECT MAX(act_id) max_id FROM copy_action") or {"max_id": None}
    max_id = head["max_id"]
    cached = _GROSS_TRADED_CACHE.get(key)
    if cached and cached[0] == max_id:
        return cached[1]
    row = q1(db, "SELECT COALESCE(SUM(ABS(our_qty_delta*our_px)),0) g FROM copy_action") or {"g": 0.0}
    gross = row["g"] or 0.0
    if len(_GROSS_TRADED_CACHE) > 32:
        _GROSS_TRADED_CACHE.clear()
    _GROSS_TRADED_CACHE[key] = (max_id, gross)
    return gross


def ep_overview(db):
    # LIVE-DERIVE from copy_position + copy_account so cards are not delayed by account_stats snapshots.
    acct = q1(db, "SELECT initial_balance, balance FROM copy_account WHERE id=1")
    closed = q1(db, "SELECT COUNT(*) n, SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins "
                    "FROM copy_position WHERE status!='open'") or {"n": 0, "wins": 0}
    closed_n = closed["n"] or 0
    win_rate = ((closed["wins"] or 0) / closed_n) if closed_n else 0.0
    if acct is None:
        base = {"equity": 0, "roiPct": 0, "todayPct": 0, "realizedPnl": 0, "unrealizedPnl": 0,
                "winRatePct": win_rate * 100, "openCount": 0, "closedCount": closed_n,
                "availableBalance": 0, "availablePctOfEquity": 0,
                "risk": {"gross": 0, "net": 0, "netGrossRatioPct": 0, "longPct": 0, "shortPct": 0},
                "fees": {"cumulative": 0, "netPerGrossBp": 0}, "lastUpdate": None}
    else:
        init = acct["initial_balance"] or 1.0
        balance = acct["balance"] or 0.0
        open_risk = q1(db,
            "SELECT COUNT(*) open_n, "
            "COALESCE(SUM(CASE WHEN size>0 THEN "
            "  CASE WHEN unrealized_pnl IS NOT NULL THEN unrealized_pnl ELSE "
            "    COALESCE(rem_size,0) * "
            "    ((CASE WHEN mark_px IS NOT NULL AND mark_px!=0 THEN mark_px ELSE COALESCE(entry_px,0) END) "
            "     - COALESCE(entry_px,0)) * "
            "    (CASE WHEN side='long' THEN 1 ELSE -1 END) "
            "  END ELSE 0 END),0) upnl, "
            "COALESCE(SUM(CASE WHEN size>0 THEN COALESCE(margin,0)*COALESCE(rem_size,0)/size ELSE 0 END),0) locked, "
            "COALESCE(SUM(CASE WHEN size>0 THEN COALESCE(notional,0)*COALESCE(rem_size,0)/size ELSE 0 END),0) gross, "
            "COALESCE(SUM(CASE WHEN size>0 THEN COALESCE(notional,0)*COALESCE(rem_size,0)/size*"
            "  (CASE WHEN side='long' THEN 1 ELSE -1 END) ELSE 0 END),0) net "
            "FROM copy_position WHERE status='open'")
        open_n = (open_risk["open_n"] if open_risk else 0) or 0
        upnl = (open_risk["upnl"] if open_risk else 0.0) or 0.0
        locked = (open_risk["locked"] if open_risk else 0.0) or 0.0
        gross = (open_risk["gross"] if open_risk else 0.0) or 0.0
        net = (open_risk["net"] if open_risk else 0.0) or 0.0
        gross_traded = _gross_traded(db)
        equity = balance + upnl
        realized = balance - init
        available = balance - locked
        long_n = (gross + net) / 2 if gross else 0.0
        short_n = (gross - net) / 2 if gross else 0.0
        eq24 = q1(db, "SELECT equity FROM account_stats WHERE ts<=? ORDER BY ts DESC LIMIT 1",
                  (_iso_ago(24 * 3600),))
        today = ((equity / eq24["equity"] - 1) * 100) if (eq24 and eq24["equity"]) else 0.0
        bp = (realized / gross_traded * 1e4) if gross_traded else 0.0
        base = {
            "equity": equity, "roiPct": (equity / init - 1) * 100, "todayPct": today,
            "realizedPnl": realized, "unrealizedPnl": upnl,
            "winRatePct": win_rate * 100, "openCount": open_n, "closedCount": closed_n,
            "availableBalance": available,
            "availablePctOfEquity": (available / equity * 100) if equity else 0.0,
            "risk": {"gross": gross, "net": net,
                     "netGrossRatioPct": (net / gross * 100) if gross else 0.0,
                     "longPct": (long_n / gross * 100) if gross else 0.0,
                     "shortPct": (short_n / gross * 100) if gross else 0.0},
            "fees": {"cumulative": gross_traded * config.TAKER_FEE, "netPerGrossBp": bp},
            "lastUpdate": (q1(db, "SELECT MAX(ts) m FROM account_stats") or {"m": None})["m"],
        }

    obs = q1(db, "SELECT state,heartbeat_at FROM process_status WHERE name='observer'")
    ss = scanner_status(db)
    last_scan = q1(db, "SELECT MAX(finished_at) m FROM scan_runs")
    wl = {"c": followed_count(db)}

    def _stale(row):
        if not row or not row["heartbeat_at"]:
            return False
        hb = iso_epoch(row["heartbeat_at"])
        return bool(hb and (time.time() - hb) > PROC_STALE_SEC)

    obs_state = ("stopped" if (not obs or obs["state"] == "stopped" or _stale(obs))
                 else (obs["state"] or "running"))
    radar = q1(
        db,
        "SELECT s.mode,s.status,a.bullish_score,a.bearish_score "
        "FROM market_risk_state s "
        "LEFT JOIN market_risk_assessment a ON a.assessment_id=s.current_assessment_id "
        "WHERE s.id=1",
    )
    active_strategy = strategy_revision.load_active(db)
    base["system"] = {
        "observer": obs_state,
        "observerStale": _stale(obs),
        "observerHeartbeatAt": (obs["heartbeat_at"] if obs else None),
        "scanner": ss["mode"],
        "scannerStale": ss["stale"],
        "scannerHeartbeatAt": ss["heartbeatAt"],
        "scannerDetail": ss["detail"],
        "lastScanAt": (last_scan["m"] if last_scan else None),
        "watchlistCount": (wl["c"] if wl else 0),
        "riskRadar": {
            "enabled": bool(radar and radar["mode"] == "shadow" and radar["status"] == "running"),
            "bullishScore": radar["bullish_score"] if radar else None,
            "bearishScore": radar["bearish_score"] if radar else None,
        },
        "mode": "paper",
        "strategyRevision": (active_strategy or {}).get("revision"),
        "strategyGeneration": (active_strategy or {}).get("selectionGeneration"),
        "strategySource": (active_strategy or {}).get("source"),
        "strategyActivatedAt": (active_strategy or {}).get("activatedAt"),
        "strategyParamsHash": (active_strategy or {}).get("paramsHash"),
    }
    return base


def ep_strategy_revisions(db, limit=50):
    limit = min(200, max(1, int(limit)))
    active = strategy_revision.active_revision_id(db)
    rows = qall(
        db,
        "SELECT revision,selection_generation,parent_revision,source,status,params_hash,reason,"
        "created_at,activated_at,superseded_at,targets_json "
        "FROM strategy_revision ORDER BY created_at DESC,revision DESC LIMIT ?",
        (limit,),
    )
    return {
        "activeRevision": active,
        "revisions": [{
            "revision": row["revision"],
            "selectionGeneration": row["selection_generation"],
            "parentRevision": row["parent_revision"],
            "source": row["source"],
            "status": row["status"],
            "paramsHash": row["params_hash"],
            "reason": row["reason"],
            "targetCount": len(json.loads(row["targets_json"] or "[]")),
            "createdAt": row["created_at"],
            "activatedAt": row["activated_at"],
            "supersededAt": row["superseded_at"],
        } for row in rows],
    }


def ep_equity(db, rng):
    cutoff = {"1d": _iso_ago(86400), "7d": _iso_ago(7 * 86400)}.get(rng)
    max_pts = 300
    if cutoff:
        where_sql, args = "WHERE ts>=?", (cutoff,)
    else:
        rng = "all"
        where_sql, args = "", ()
    rows = qall(db,
        "WITH ordered AS ("
        "  SELECT ts,equity,ROW_NUMBER() OVER (ORDER BY ts)-1 rn,COUNT(*) OVER () total "
        "  FROM account_stats " + where_sql +
        "), sampled AS ("
        "  SELECT ts,equity,rn,total,"
        "         CASE WHEN total>? THEN CAST(total/? AS INTEGER)+1 ELSE 1 END stride "
        "  FROM ordered"
        ") "
        "SELECT ts,equity FROM sampled "
        "WHERE rn%stride=0 OR rn=total-1 ORDER BY ts",
        tuple(args) + (max_pts, max_pts))
    pts = [{"t": r["ts"], "equity": r["equity"]} for r in rows]
    return {"range": rng, "points": pts}


def _top_bottom_group_rows(db, stats_sql, top=5, bottom=3):
    return qall(db,
        "WITH stats AS ("
        + stats_sql +
        "), ranked AS ("
        "  SELECT stats.*,COUNT(*) OVER() total,"
        "         ROW_NUMBER() OVER (ORDER BY net DESC) desc_rn,"
        "         ROW_NUMBER() OVER (ORDER BY net ASC) asc_rn "
        "  FROM stats"
        ") "
        "SELECT * FROM ranked "
        "WHERE total<=? OR desc_rn<=? OR asc_rn<=? "
        "ORDER BY CASE WHEN total<=? OR desc_rn<=? THEN 0 ELSE 1 END, net DESC",
        (top + bottom, top, bottom, top + bottom, top))


def ep_insights(db):
    NET = "COALESCE(SUM(CASE WHEN cp.status!='open' THEN cp.realized_pnl ELSE cp.unrealized_pnl END),0)"
    wallet_sql = (
        f"SELECT cp.addr, {NET} net, w.rank, "
        "SUM(CASE WHEN cp.status!='open' THEN 1 ELSE 0 END) cn, "
        "SUM(CASE WHEN cp.status!='open' AND cp.realized_pnl>0 THEN 1 ELSE 0 END) wn "
        "FROM copy_position cp LEFT JOIN watchlist w ON w.addr=cp.addr GROUP BY cp.addr"
    )
    wallets = [{
        "address": r["addr"], "rank": r["rank"], "netPnl": r["net"] or 0.0, "closedN": r["cn"] or 0,
        "winRatePct": (r["wn"] / r["cn"] * 100) if r["cn"] else None,
    } for r in _top_bottom_group_rows(db, wallet_sql)]
    coin_sql = f"SELECT cp.coin, {NET} net, COUNT(*) n FROM copy_position cp GROUP BY cp.coin"
    coins = [{"coin": r["coin"], "netPnl": r["net"] or 0.0, "n": r["n"]} for r in
             _top_bottom_group_rows(db, coin_sql)]
    return {"walletContrib": wallets, "coinPnl": coins}
