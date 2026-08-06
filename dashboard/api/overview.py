"""Overview, equity, insight, and strategy-revision dashboard endpoints."""

import json
import time

from hyper import config
from hyper.selection import strategy_revision
from .common import execution_copy_tables, iso_epoch, q1, qall
from .discovery import followed_count, scanner_status


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


def _gross_traded(db, action_table="copy_action"):
    key = (_db_cache_key(db), action_table)
    head = q1(db, f"SELECT MAX(act_id) max_id FROM {action_table}") or {"max_id": None}
    max_id = head["max_id"]
    cached = _GROSS_TRADED_CACHE.get(key)
    if cached and cached[0] == max_id:
        return cached[1]
    row = q1(db, f"SELECT COALESCE(SUM(ABS(our_qty_delta*our_px)),0) g FROM {action_table}") or {"g": 0.0}
    gross = row["g"] or 0.0
    if len(_GROSS_TRADED_CACHE) > 32:
        _GROSS_TRADED_CACHE.clear()
    _GROSS_TRADED_CACHE[key] = (max_id, gross)
    return gross


def _live_fill_performance(db, since_ms=None):
    """Confirmed PnL from orders created by this copy-trading system.

    Exchange fills are the Live source of truth.  Matching the durable CLOID
    intent excludes any unrelated/manual account activity that happened while
    a Live session was running.
    """
    cutoff_sql = " AND f.fill_time_ms>=?" if since_ms is not None else ""
    args = (int(since_ms),) if since_ms is not None else ()
    return q1(
        db,
        "SELECT COALESCE(SUM(f.closed_pnl),0)-COALESCE(SUM(f.fee),0) pnl,"
        "COALESCE(SUM(f.fee),0) fee FROM execution_fill f "
        "WHERE f.network='mainnet' AND EXISTS ("
        "SELECT 1 FROM execution_order_intent i WHERE i.session_id=f.session_id "
        "AND lower(i.cloid)=lower(f.cloid))" + cutoff_sql,
        args,
        {"pnl": 0.0, "fee": 0.0},
    ) or {"pnl": 0.0, "fee": 0.0}


def ep_overview(db):
    tables = execution_copy_tables(db)
    mode = tables["mode"]
    position_table, action_table, account_table = tables["position"], tables["action"], tables["account"]
    live_control = (
        q1(db, "SELECT active_session_id FROM execution_control WHERE id=1")
        if mode == "live" else None
    )
    active_live_session = live_control["active_session_id"] if live_control else None
    account_last_update = None
    # LIVE-DERIVE from copy_position + copy_account so cards are not delayed by account_stats snapshots.
    acct = q1(
        db,
        ("SELECT initial_balance,balance,available FROM live_copy_account WHERE id=1"
         if mode == "live" else "SELECT initial_balance,balance,NULL AS available FROM copy_account WHERE id=1"),
    )
    if mode == "live" and not active_live_session:
        preview = q1(
            db,
            "SELECT p.equity,p.available,p.observed_at FROM execution_account_preview p "
            "JOIN execution_credential c ON c.network=p.network "
            "AND lower(c.account_address)=lower(p.account_address) WHERE p.network='mainnet'",
        )
        if preview:
            initial = (
                acct["initial_balance"]
                if acct is not None and acct["initial_balance"] is not None
                else preview["equity"]
            )
            acct = {
                "initial_balance": initial,
                "balance": preview["equity"],
                "available": preview["available"],
            }
            account_last_update = preview["observed_at"]
    closed = q1(db, "SELECT COUNT(*) n, SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins "
                    f"FROM {position_table} WHERE status!='open'") or {"n": 0, "wins": 0}
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
            "COALESCE(SUM(CASE WHEN size>0 THEN ABS(COALESCE(rem_size,0))*"
            "  (CASE WHEN mark_px IS NOT NULL AND mark_px!=0 THEN mark_px ELSE COALESCE(entry_px,0) END) ELSE 0 END),0) gross, "
            "COALESCE(SUM(CASE WHEN size>0 THEN ABS(COALESCE(rem_size,0))*"
            "  (CASE WHEN mark_px IS NOT NULL AND mark_px!=0 THEN mark_px ELSE COALESCE(entry_px,0) END)*"
            "  (CASE WHEN side='long' THEN 1 ELSE -1 END) ELSE 0 END),0) net "
            f"FROM {position_table} WHERE status='open'")
        open_n = (open_risk["open_n"] if open_risk else 0) or 0
        upnl = (open_risk["upnl"] if open_risk else 0.0) or 0.0
        locked = (open_risk["locked"] if open_risk else 0.0) or 0.0
        gross = (open_risk["gross"] if open_risk else 0.0) or 0.0
        net = (open_risk["net"] if open_risk else 0.0) or 0.0
        gross_traded = _gross_traded(db, action_table)
        equity = balance if mode == "live" else balance + upnl
        if mode == "live":
            live_performance = _live_fill_performance(db)
            realized = live_performance["pnl"] or 0.0
        else:
            realized = balance - init
        available = (acct["available"] if mode == "live" else balance - locked)
        available = available if available is not None else max(0.0, balance - locked)
        long_n = (gross + net) / 2 if gross else 0.0
        short_n = (gross - net) / 2 if gross else 0.0
        if mode == "live":
            # Funding flows change real equity and available funds, but they are not strategy profit.
            # Derive Live returns only from confirmed copy fills plus marked open-copy PnL.
            strategy_pnl = realized + upnl
            funded_capital = equity - strategy_pnl
            roi = (strategy_pnl / funded_capital * 100) if funded_capital > 0 else 0.0

            cutoff_epoch = time.time() - 24 * 3600
            today_fill = _live_fill_performance(db, cutoff_epoch * 1000)
            prior_upnl = q1(
                db,
                "SELECT unrealized_pnl FROM execution_account_snapshot WHERE observed_at<=? "
                "ORDER BY observed_at DESC LIMIT 1",
                (_iso_ago(24 * 3600),),
            )
            today_pnl = (today_fill["pnl"] or 0.0) + upnl - (
                (prior_upnl["unrealized_pnl"] or 0.0) if prior_upnl else 0.0
            )
            today_base = equity - today_pnl
            today = (today_pnl / today_base * 100) if today_base > 0 else 0.0
        else:
            eq24 = q1(db, "SELECT equity FROM account_stats WHERE ts<=? ORDER BY ts DESC LIMIT 1",
                      (_iso_ago(24 * 3600),))
            roi = (equity / init - 1) * 100
            today = ((equity / eq24["equity"] - 1) * 100) if (eq24 and eq24["equity"]) else 0.0
        bp = (realized / gross_traded * 1e4) if gross_traded else 0.0
        base = {
            "equity": equity, "roiPct": roi, "todayPct": today,
            "realizedPnl": realized, "unrealizedPnl": upnl,
            "winRatePct": win_rate * 100, "openCount": open_n, "closedCount": closed_n,
            "availableBalance": available,
            "availablePctOfEquity": (available / equity * 100) if equity else 0.0,
            "risk": {"gross": gross, "net": net,
                     "netGrossRatioPct": (net / gross * 100) if gross else 0.0,
                     "longPct": (long_n / gross * 100) if gross else 0.0,
                     "shortPct": (short_n / gross * 100) if gross else 0.0},
            "fees": {"cumulative": (
                live_performance["fee"]
                if mode == "live" else gross_traded * config.TAKER_FEE
            ), "netPerGrossBp": bp},
            "lastUpdate": (account_last_update or (q1(db, "SELECT updated_at m FROM live_copy_account WHERE id=1") or {"m": None})["m"]
                           if mode == "live" else (q1(db, "SELECT MAX(ts) m FROM account_stats") or {"m": None})["m"]),
        }

    obs = q1(db, "SELECT state,heartbeat_at FROM process_status WHERE name='observer'")
    ss = scanner_status(db)
    scan_progress = q1(db, "SELECT state,stage FROM scan_progress WHERE id=1")
    last_scan = q1(db, "SELECT MAX(finished_at) m FROM scan_runs")
    wl = {"c": followed_count(db)}

    def _stale(row):
        if not row or not row["heartbeat_at"]:
            return False
        hb = iso_epoch(row["heartbeat_at"])
        return bool(hb and (time.time() - hb) > PROC_STALE_SEC)

    obs_state = ("stopped" if (not obs or obs["state"] == "stopped" or _stale(obs))
                 else (obs["state"] or "running"))
    active_strategy = strategy_revision.load_active(db)
    storage_guard = q1(
        db,
        "SELECT checked_at,severity,reasons_json,disk_used_pct,disk_free_bytes,"
        "db_main_bytes,db_wal_bytes,db_growth_24h_bytes "
        "FROM storage_guard_run ORDER BY checked_at DESC,id DESC LIMIT 1",
    )
    try:
        storage_reasons = json.loads(storage_guard["reasons_json"] or "[]") if storage_guard else []
    except (TypeError, ValueError):
        storage_reasons = ["invalid_storage_guard_record"]
    base["system"] = {
        "observer": obs_state,
        "observerStale": _stale(obs),
        "observerHeartbeatAt": (obs["heartbeat_at"] if obs else None),
        "scanner": ss["mode"],
        "scannerStale": ss["stale"],
        "scannerHeartbeatAt": ss["heartbeatAt"],
        "scannerDetail": ss["detail"],
        "scannerStage": (scan_progress["stage"] if scan_progress and scan_progress["state"] == "scanning" else None),
        "lastScanAt": (last_scan["m"] if last_scan else None),
        "watchlistCount": (wl["c"] if wl else 0),
        "mode": mode,
        "strategyRevision": (active_strategy or {}).get("revision"),
        "strategyGeneration": (active_strategy or {}).get("selectionGeneration"),
        "strategySource": (active_strategy or {}).get("source"),
        "strategyActivatedAt": (active_strategy or {}).get("activatedAt"),
        "strategyParamsHash": (active_strategy or {}).get("paramsHash"),
        "storageGuard": {
            "status": (storage_guard["severity"] if storage_guard else "unknown"),
            "checkedAt": (storage_guard["checked_at"] if storage_guard else None),
            "reasons": storage_reasons,
            "diskUsedPct": (storage_guard["disk_used_pct"] if storage_guard else None),
            "diskFreeBytes": (storage_guard["disk_free_bytes"] if storage_guard else None),
            "dbMainBytes": (storage_guard["db_main_bytes"] if storage_guard else None),
            "dbWalBytes": (storage_guard["db_wal_bytes"] if storage_guard else None),
            "dbGrowth24hBytes": (storage_guard["db_growth_24h_bytes"] if storage_guard else None),
        },
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
    tables = execution_copy_tables(db)
    if tables["mode"] == "live":
        session = q1(db, "SELECT active_session_id FROM execution_control WHERE id=1")
        active_session = session["active_session_id"] if session else None
        if active_session:
            live_source = (
                "SELECT observed_at ts,equity FROM execution_account_snapshot WHERE session_id=?"
                + (" AND observed_at>=?" if cutoff else "")
            )
            live_args = (active_session,) + (args if cutoff else ())
        else:
            snapshot_cutoff = "WHERE s.observed_at>=?" if cutoff else ""
            preview_cutoff = "WHERE p.observed_at>=?" if cutoff else ""
            live_source = (
                "SELECT s.observed_at ts,s.equity FROM execution_account_snapshot s "
                "JOIN execution_session es ON es.session_id=s.session_id "
                "JOIN execution_credential ec ON ec.network='mainnet' "
                "AND lower(ec.account_address)=lower(es.account_address) " + snapshot_cutoff +
                " UNION ALL "
                "SELECT p.observed_at ts,p.equity FROM execution_account_preview p "
                "JOIN execution_credential ec ON ec.network=p.network "
                "AND lower(ec.account_address)=lower(p.account_address) " + preview_cutoff
            )
            live_args = ((args + args) if cutoff else ())
        rows = qall(db,
            "WITH source AS (" + live_source +
            "), ordered AS (SELECT ts,equity,ROW_NUMBER() OVER (ORDER BY ts)-1 rn,"
            "COUNT(*) OVER () total FROM source " +
            "), sampled AS (SELECT ts,equity,rn,total,CASE WHEN total>? THEN CAST(total/? AS INTEGER)+1 ELSE 1 END stride FROM ordered) "
            "SELECT ts,equity FROM sampled WHERE rn%stride=0 OR rn=total-1 ORDER BY ts",
            live_args + (max_pts, max_pts))
    else:
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
    tables = execution_copy_tables(db)
    position_table = tables["position"]
    NET = "COALESCE(SUM(CASE WHEN cp.status!='open' THEN cp.realized_pnl ELSE cp.unrealized_pnl END),0)"
    wallet_sql = (
        f"SELECT cp.addr, {NET} net, MAX(core.selection_rank) rank, "
        "SUM(CASE WHEN cp.status!='open' THEN 1 ELSE 0 END) cn, "
        "SUM(CASE WHEN cp.status!='open' AND cp.realized_pnl>0 THEN 1 ELSE 0 END) wn "
        f"FROM {position_table} cp LEFT JOIN ("
        "SELECT fs.addr,fs.selection_rank FROM follow_selection fs "
        "JOIN scan_generation sg ON sg.generation=fs.generation "
        "LEFT JOIN target_controls tc ON lower(tc.addr)=lower(fs.addr) "
        "WHERE sg.status='published' AND sg.complete=1 AND sg.is_current=1 "
        "AND fs.role='core' AND fs.enabled=1 "
        "AND COALESCE(tc.intent,'active')!='requalify'"
        ") core ON lower(core.addr)=lower(cp.addr) GROUP BY cp.addr"
    )
    wallets = [{
        "address": r["addr"], "rank": r["rank"], "netPnl": r["net"] or 0.0, "closedN": r["cn"] or 0,
        "winRatePct": (r["wn"] / r["cn"] * 100) if r["cn"] else None,
    } for r in _top_bottom_group_rows(db, wallet_sql)]
    coin_sql = f"SELECT cp.coin, {NET} net, COUNT(*) n FROM {position_table} cp GROUP BY cp.coin"
    coins = [{"coin": r["coin"], "netPnl": r["net"] or 0.0, "n": r["n"]} for r in
             _top_bottom_group_rows(db, coin_sql)]
    return {"walletContrib": wallets, "coinPnl": coins}
