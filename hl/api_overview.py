"""Overview, equity, insight, and shadow dashboard endpoints."""

import time

from . import config
from . import params as params_mod
from .api_common import iso_epoch, q1, qall
from .api_discovery import followed_count, scanner_status


PROC_STALE_SEC = 90


def _iso_ago(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


def ep_shadow(db):
    """Taker vs maker-shadow A/B paper books."""
    def book(acct, pos):
        br = db.execute(f"SELECT balance FROM {acct} WHERE id=1").fetchone()
        bal = float(br["balance"]) if br else 0.0
        o = db.execute(
            f"SELECT COALESCE(SUM(unrealized_pnl),0) u, COUNT(*) n FROM {pos} WHERE status='open'"
        ).fetchone()
        c = db.execute(
            f"SELECT COALESCE(SUM(realized_pnl),0) r, COUNT(*) n, "
            f"SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) w FROM {pos} WHERE status!='open'"
        ).fetchone()
        upnl = float(o["u"] or 0.0)
        return {"balance": bal, "unrealized": upnl, "equity": bal + upnl,
                "realized": float(c["r"] or 0.0), "openN": o["n"], "closedN": c["n"],
                "winRatePct": ((c["w"] or 0) / c["n"] * 100.0) if c["n"] else 0.0}

    taker = book("copy_account", "copy_position")
    maker = book("shadow_account", "shadow_position")
    mpos = [{"addr": r["addr"], "coin": r["coin"], "side": r["side"], "entry": r["entry_px"],
             "lev": r["leverage"], "margin": r["margin"], "mark": r["mark_px"],
             "upnl": r["unrealized_pnl"], "addN": r["add_count"], "openedAt": r["opened_at"]}
            for r in db.execute("SELECT addr,coin,side,entry_px,leverage,margin,mark_px,unrealized_pnl,"
                                "add_count,opened_at FROM shadow_position "
                                "WHERE status='open' ORDER BY opened_at DESC").fetchall()]
    return {"enabled": bool(config.SHADOW_MAKER_ENABLED), "taker": taker,
            "maker": maker, "makerPositions": mpos}


def ep_overview(db):
    # LIVE-DERIVE from copy_position + copy_account so cards are not delayed by account_stats snapshots.
    acct = q1(db, "SELECT initial_balance, balance FROM copy_account WHERE id=1")
    if acct is None:
        base = {"equity": 0, "roiPct": 0, "todayPct": 0, "realizedPnl": 0, "unrealizedPnl": 0,
                "winRatePct": 0, "openCount": 0, "availableBalance": 0, "availablePctOfEquity": 0,
                "risk": {"gross": 0, "net": 0, "netGrossRatioPct": 0, "longPct": 0, "shortPct": 0},
                "fees": {"cumulative": 0, "netPerGrossBp": 0}, "lastUpdate": None}
    else:
        init = acct["initial_balance"] or 1.0
        balance = acct["balance"] or 0.0
        upnl = locked = gross = net = 0.0
        for r in qall(db, "SELECT side,rem_size,size,entry_px,mark_px,unrealized_pnl,margin,notional "
                          "FROM copy_position WHERE status='open' AND size>0"):
            sgn = 1 if r["side"] == "long" else -1
            mark = r["mark_px"] if r["mark_px"] else (r["entry_px"] or 0)
            u = r["unrealized_pnl"] if r["unrealized_pnl"] is not None else \
                (r["rem_size"] or 0) * (mark - (r["entry_px"] or 0)) * sgn
            upnl += u
            frac = (r["rem_size"] / r["size"]) if r["size"] else 0
            locked += (r["margin"] or 0) * frac
            cur_notl = (r["notional"] or 0) * frac
            gross += cur_notl
            net += cur_notl * sgn
        open_n = (q1(db, "SELECT COUNT(*) c FROM copy_position WHERE status='open'") or {"c": 0})["c"]
        closed = q1(db, "SELECT COUNT(*) n, SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins "
                        "FROM copy_position WHERE status!='open'") or {"n": 0, "wins": 0}
        closed_n = closed["n"] or 0
        win_rate = ((closed["wins"] or 0) / closed_n) if closed_n else 0.0
        gross_traded = (q1(db, "SELECT COALESCE(SUM(ABS(our_qty_delta*our_px)),0) g FROM copy_action")
                        or {"g": 0})["g"] or 0.0
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
            "winRatePct": win_rate * 100, "openCount": open_n,
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
    line = params_mod.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE
    wl = {"c": followed_count(db, line)}

    def _stale(row):
        if not row or not row["heartbeat_at"]:
            return False
        hb = iso_epoch(row["heartbeat_at"])
        return bool(hb and (time.time() - hb) > PROC_STALE_SEC)

    obs_state = ("stopped" if (not obs or obs["state"] == "stopped" or _stale(obs))
                 else (obs["state"] or "running"))
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
        "mode": "paper",
    }
    return base


def ep_equity(db, rng):
    cutoff = {"1d": _iso_ago(86400), "7d": _iso_ago(7 * 86400)}.get(rng)
    if cutoff:
        rows = qall(db, "SELECT ts,equity FROM account_stats WHERE ts>=? ORDER BY ts", (cutoff,))
    else:
        rng = "all"
        rows = qall(db, "SELECT ts,equity FROM account_stats ORDER BY ts")
    pts = [{"t": r["ts"], "equity": r["equity"]} for r in rows]
    max_pts = 300
    if len(pts) > max_pts:
        stride = len(pts) // max_pts + 1
        pts = pts[::stride] + ([pts[-1]] if (len(pts) - 1) % stride else [])
    return {"range": rng, "points": pts}


def _top_bottom(rows, key, top=5, bottom=3):
    s = sorted(rows, key=lambda r: r[key], reverse=True)
    if len(s) <= top + bottom:
        return s
    return s[:top] + s[-bottom:]


def ep_insights(db):
    NET = "COALESCE(SUM(CASE WHEN cp.status!='open' THEN cp.realized_pnl ELSE cp.unrealized_pnl END),0)"
    wallets = [{
        "address": r["addr"], "rank": r["rank"], "netPnl": r["net"] or 0.0, "closedN": r["cn"] or 0,
        "winRatePct": (r["wn"] / r["cn"] * 100) if r["cn"] else None,
    } for r in qall(db,
        f"SELECT cp.addr, {NET} net, w.rank, "
        "SUM(CASE WHEN cp.status!='open' THEN 1 ELSE 0 END) cn, "
        "SUM(CASE WHEN cp.status!='open' AND cp.realized_pnl>0 THEN 1 ELSE 0 END) wn "
        "FROM copy_position cp LEFT JOIN watchlist w ON w.addr=cp.addr GROUP BY cp.addr")]
    coins = [{"coin": r["coin"], "netPnl": r["net"] or 0.0, "n": r["n"]} for r in qall(db,
        f"SELECT cp.coin, {NET} net, COUNT(*) n FROM copy_position cp GROUP BY cp.coin")]
    return {"walletContrib": _top_bottom(wallets, "netPnl"), "coinPnl": _top_bottom(coins, "netPnl")}
