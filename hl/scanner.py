"""Discovery domain: the rolling scanner that maintains the live watchlist.

harvest leaderboard -> coarse candidates -> profile work-set (actives + top-N new)
over a short window -> perp episodes/metrics -> upsert active/rejected/retired.
Composes rest + fills + metrics + storage; holds no infra of its own.
"""
import concurrent.futures
import threading
import time

from . import metrics, rest, storage
from .fills import build_episodes, is_spot
from .util import f, now_iso

_db_lock = threading.Lock()   # serializes sqlite writes across scanner worker threads


# -------------------------------------------------------------------------- harvest
def harvest(db, min_acct: float, max_turnover: float, p=None) -> int:
    """COARSE candidate funnel only — the leaderboard ROI is unrealized-inflated (account MTM,
    incl held winners + builder/spot), so it's a weak floor, NOT the strength judge. Real
    strength = our realized-crypto profile. Keep: real capital, not an MM (turnover), lifetime
    profitable, a modest recent ROI floor, and active in the last 24h."""
    min_mon = getattr(p, "min_roi", 0.20)        # modest 30d ROI floor (exclude weak/losing)
    rows = rest.get_leaderboard()
    now = now_iso()
    n_cand = 0
    for r in rows:
        w = {name: perf for name, perf in r.get("windowPerformances", [])}
        d, wk, mo, al = w.get("day", {}), w.get("week", {}), w.get("month", {}), w.get("allTime", {})
        acct = f(r.get("accountValue"))
        turnover = (f(mo.get("vlm")) / acct / 30.0) if acct > 0 else 0.0
        cand = (acct >= min_acct and 0 < turnover <= max_turnover
                and f(al.get("roi")) > 0                       # lifetime profitable
                and f(mo.get("roi")) > min_mon                 # modest 30d floor
                and f(d.get("vlm")) > 0)                       # active last 24h
        db.execute(
            "INSERT OR REPLACE INTO leaderboard (addr,display_name,account_value,"
            "day_pnl,day_roi,day_vlm,week_pnl,week_roi,week_vlm,mon_pnl,mon_roi,mon_vlm,"
            "all_pnl,all_roi,all_vlm,daily_turnover,is_candidate,fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["ethAddress"].lower(), r.get("displayName"), acct,
             f(d.get("pnl")), f(d.get("roi")), f(d.get("vlm")),
             f(wk.get("pnl")), f(wk.get("roi")), f(wk.get("vlm")),
             f(mo.get("pnl")), f(mo.get("roi")), f(mo.get("vlm")),
             f(al.get("pnl")), f(al.get("roi")), f(al.get("vlm")),
             turnover, 1 if cand else 0, now))
        n_cand += 1 if cand else 0
    db.commit()
    return n_cand


# -------------------------------------------------------------------------- profile
def _self_liquidations(fills, addr, acct):
    """Self-liquidation events (liquidation.liquidatedUser == this wallet, NOT where it was the
    liquidator). Returns (count_by_coin, worst_single_loss_pct_of_equity<=0). Account blow-up
    doesn't transfer to our isolated per-trade copy, so this is a mild high-variance flag."""
    bycoin = {}
    for x in fills:
        liq = x.get("liquidation") or {}
        if (liq.get("liquidatedUser") or "").lower() == addr:
            bycoin[x["coin"]] = bycoin.get(x["coin"], 0.0) + f(x.get("closedPnl"))
    if not bycoin:
        return 0, 0.0
    worst = min(bycoin.values())
    return len(bycoin), (worst / acct * 100 if acct else 0.0)


def _margin_snapshot(addr):
    """(margin_type, current_account_leverage) from clearinghouseState. Snapshot only — flat
    wallet -> ('flat', 0). Mixed positions -> 'mixed'. Returns (None, 0) on fetch failure."""
    cs = rest.clearinghouse_state(addr)
    if not isinstance(cs, dict):
        return None, 0.0
    ms = cs.get("marginSummary", {})
    av = f(ms.get("accountValue")); ntl = f(ms.get("totalNtlPos"))
    pos = cs.get("assetPositions", []) or []
    if not pos:
        return "flat", 0.0
    types = {(pp.get("position", {}).get("leverage") or {}).get("type") for pp in pos}
    types.discard(None)
    mt = next(iter(types)) if len(types) == 1 else ("mixed" if types else "flat")
    return mt, (ntl / av if av else 0.0)


def _prescreen(addr, universe, p, now_ms):
    """Cheap Stage-1 (1 call, latest ~2000 fills): reject dormant / no-recent-crypto / builder-
    dominant BEFORE the heavy full 14d fetch — so we only fully-profile likely-copyable wallets."""
    latest = rest.user_fills_latest(addr)
    if not isinstance(latest, list) or not latest:
        return False, "no_fills"
    crypto = [x for x in latest if not is_spot(x["coin"]) and (not universe or x["coin"] in universe)]
    if not crypto:
        return False, "no_crypto"
    if (now_ms - max(x["time"] for x in crypto)) / 86400_000.0 > p.inactive_days:
        return False, "no_recent_crypto"          # no crypto trade in the last few days
    if len(crypto) / len(latest) < getattr(p, "min_crypto", 0.3):
        return False, "builder_dominant"          # recent activity is mostly non-crypto
    return True, "ok"


def _write_reject(db, addr, prior, stamp, reason):
    status = "retired" if (prior or {}).get("status") == "active" else "rejected"
    with _db_lock:
        db.execute(
            f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' * 35)})",
            (addr, status, reason, 0.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, None, 0, 0, 0,
             (prior or {}).get("age_days"), 0, 0, None, 0, 0, 0,
             (prior or {}).get("first_added"), stamp, (prior or {}).get("times_seen", 0) + 1,
             (prior or {}).get("times_active", 0)))
        db.commit()
    return status, reason, {"n_trades": 0, "score": 0.0}, False


def _profile_one(db, addr, start_ms, now_ms, p, prior, lb, stamp, universe):
    pre_ok, pre_reason = _prescreen(addr, universe, p, now_ms)   # Stage 1: cheap
    if not pre_ok:
        return _write_reject(db, addr, prior, stamp, pre_reason)
    raw, hit_cap = rest.fetch_window(addr, start_ms, p.max_pages)  # Stage 2: full
    for x in raw:
        x["user"] = addr
    # only COPYABLE activity counts: crypto perps + transparent builder perps (stocks/commodities,
    # e.g. xyz:AAPL — in `universe`). Spot is excluded (is_spot); opaque/private builder dexes are
    # excluded by not being in `universe`. perp_frac = copyable-perp share of fills.
    perp = [x for x in raw if not is_spot(x["coin"]) and (not universe or x["coin"] in universe)]
    perp_frac = (len(perp) / len(raw)) if raw else 0.0
    eps = build_episodes(perp)
    m = metrics.compute_metrics(perp, eps, now_ms)
    if m is None:
        m = {"n_fills": len(perp), "n_trades": 0, "window_days": 0, "trades_per_day": 0,
             "taker_frac_notl": 0, "median_hold_s": 0, "win_rate": 0, "net_pnl": 0,
             "roi_notional": 0, "total_notl": 0, "gross_pnl": 0, "total_fee": 0, "n_coins": 0,
             "top_coin": None, "long_frac": 0, "max_drawdown": 0, "avg_notional": 0,
             "last_fill_ms": raw[-1]["time"] if raw else 0}

    acct_value = f((lb or {}).get("account_value"))
    m["perp_frac"] = perp_frac
    m["acct_value"] = acct_value
    m["roi_equity"] = (m["net_pnl"] / acct_value) if acct_value else 0.0
    m["times_active"] = (prior or {}).get("times_active", 0)
    m["lev_proxy"] = (m["avg_notional"] / acct_value) if acct_value else 0.0  # hist. eff. leverage
    m["liq_count"], m["liq_worst_pct"] = _self_liquidations(raw, addr, acct_value)

    if m["n_trades"] == 0:
        ok, reason = False, "no_perp_trades"
    else:
        ok, reason = metrics.gates(m, now_ms, p)
    m["times_active"] += 1 if ok else 0

    age_days = (prior or {}).get("age_days")
    m["margin_type"] = (prior or {}).get("margin_type")
    m["cur_leverage"] = (prior or {}).get("cur_leverage") or 0.0
    if ok:
        if age_days is None:
            try:
                birth = rest.account_birth_ms(addr)
                if birth:
                    age_days = (now_ms - birth) / 86400_000.0
            except Exception:  # noqa: BLE001
                pass
        mt, cl = _margin_snapshot(addr)              # isolated/cross + current leverage (snapshot)
        if mt is not None:
            m["margin_type"], m["cur_leverage"] = mt, cl
    m["age_days"] = age_days

    prev_status = (prior or {}).get("status")
    status = "active" if ok else ("retired" if prev_status == "active" else "rejected")
    first_added = (prior or {}).get("first_added") or (stamp if ok else None)
    m["score"] = metrics.score(m) if ok else 0.0

    with _db_lock:
        if ok:
            db.execute("DELETE FROM episode WHERE addr=?", (addr,))
            db.executemany(
                "INSERT OR REPLACE INTO episode (addr,coin,side,open_ms,close_ms,hold_s,net_pnl,fee,max_notl,n_fills,open_px,close_px)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(addr, e["coin"], e["side"], e["open_ms"], e["close_ms"], e["hold_s"], e["net_pnl"],
                  e["fee"], e["max_notl"], e["n_fills"], e["open_px"], e["close_px"]) for e in eps])
        db.execute(
            f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' * 35)})",
            (addr, status, reason, m["score"], m["n_fills"], m["n_trades"], m["window_days"],
             m["trades_per_day"], m["taker_frac_notl"], m["median_hold_s"], m["win_rate"],
             m["net_pnl"], m["roi_equity"], m["roi_notional"], m["total_notl"], m["acct_value"],
             m["perp_frac"], m["gross_pnl"], m["total_fee"], m["n_coins"], m["top_coin"],
             m["long_frac"], m["max_drawdown"], m["avg_notional"], m["age_days"], m["last_fill_ms"],
             m["lev_proxy"], m["margin_type"], m["cur_leverage"], m["liq_count"], m["liq_worst_pct"],
             first_added, stamp, (prior or {}).get("times_seen", 0) + 1, m["times_active"]))
        db.commit()
    return status, reason, m, hit_cap


# ------------------------------------------------------------------ curated outputs
def refresh_watchlist(db, stamp) -> int:
    """Rebuild OUR tiny leaderboard (watchlist) from active profiles. Derived view —
    profile stays the source of truth; operator settings in target_controls survive."""
    db.execute("DELETE FROM watchlist")
    rows = db.execute(
        "SELECT p.addr, l.display_name, p.score, p.roi_equity, l.mon_roi, p.net_pnl, p.acct_value, "
        "p.n_trades, p.trades_per_day, p.taker_frac_notl, p.median_hold_s, p.win_rate, p.max_drawdown, "
        "p.age_days, p.top_coin, p.perp_frac, p.lev_proxy, p.margin_type, p.cur_leverage, p.liq_worst_pct, "
        "p.times_active, p.first_added, p.last_fill_ms "
        "FROM profile p LEFT JOIN leaderboard l ON l.addr=p.addr "
        "WHERE p.status='active' ORDER BY p.score DESC").fetchall()
    for rank, r in enumerate(rows, 1):
        db.execute(
            "INSERT INTO watchlist (rank,addr,display_name,score,roi_equity,mon_roi,net_pnl,acct_value,"
            "n_trades,trades_per_day,taker_frac,median_hold_s,win_rate,max_drawdown,age_days,top_coin,"
            "perp_frac,lev_proxy,margin_type,cur_leverage,liq_worst_pct,times_active,first_added,last_fill_ms,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (rank,) + r + (stamp,))
        db.execute("INSERT OR IGNORE INTO target_controls (addr,enabled,updated_at) VALUES (?,1,?)",
                   (r[0], stamp))
    db.commit()
    return len(rows)


def _record_run(db, started, t0, candidates, probed, added, retired, kept, rejected, n_active):
    db.execute(
        "INSERT INTO scan_runs (started_at,finished_at,duration_s,candidates,probed_new,added,"
        "retired,kept,rejected,n_active) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (started, now_iso(), round(time.time() - t0, 1), candidates, probed, added, retired,
         kept, rejected, n_active))
    db.commit()


def regate(db, p) -> int:
    """Re-apply gates() + score() on ALREADY-STORED profile metrics (no network, no re-fetch) and
    rebuild the watchlist. Thresholds (win/roiEq/dd/tpd/hold/...) can be tuned in seconds without a
    full re-sweep — the expensive part (fetching fills, building episodes) is already done."""
    now = int(time.time() * 1000)
    stamp = now_iso()
    rows = db.execute(
        "SELECT addr,status,n_trades,perp_frac,last_fill_ms,net_pnl,win_rate,roi_equity,"
        "max_drawdown,acct_value,trades_per_day,median_hold_s,age_days,times_active,liq_worst_pct "
        "FROM profile").fetchall()
    n_active = 0
    for r in rows:
        (addr, old, n_tr, perp_frac, last_fill, net, win, roi_eq, mdd, acct, tpd, hold, age, ta, liqw) = r
        m = {"n_trades": n_tr or 0, "perp_frac": perp_frac or 0.0, "last_fill_ms": last_fill or 0,
             "net_pnl": net or 0.0, "win_rate": win or 0.0, "roi_equity": roi_eq or 0.0,
             "max_drawdown": mdd or 0.0, "acct_value": acct or 0.0, "trades_per_day": tpd or 0.0,
             "median_hold_s": hold or 0, "age_days": age, "times_active": ta or 0,
             "liq_worst_pct": liqw or 0.0}
        if m["n_trades"] == 0:
            ok, reason = False, "no_perp_trades"
        else:
            ok, reason = metrics.gates(m, now, p)
        status = "active" if ok else ("retired" if old == "active" else "rejected")
        score = metrics.score(m) if ok else 0.0
        db.execute("UPDATE profile SET status=?,reason=?,score=? WHERE addr=?", (status, reason, score, addr))
        n_active += 1 if ok else 0
    db.commit()
    n = refresh_watchlist(db, stamp)
    print(f"regate: {n_active} active / {len(rows)} profiles  ->  watchlist {n}")
    return n


# ----------------------------------------------------------------------------- scan
def scan(db, p) -> None:
    now_ms = int(time.time() * 1000)
    started, t0 = now_iso(), time.time()
    stamp = now_iso()
    start_ms = now_ms - p.days * 86400_000

    universe = rest.copyable_universe()          # crypto perps + transparent builder (stocks/commodities)
    if not p.no_harvest:
        print("harvest leaderboard ...", flush=True)
        n_cand = harvest(db, p.min_acct, p.max_turnover, p)
        turn = "off" if p.max_turnover >= 1e8 else f"<={p.max_turnover:g}x"
        print(f"  {n_cand} candidates (acct>=${p.min_acct:g}, turnover {turn}, "
              f"mon_roi>{getattr(p,'min_roi',0.2):.0%}, all_roi>0, 24h-active)", flush=True)

    order = {"mon_roi": "mon_roi", "week_roi": "week_roi", "mon_pnl": "mon_pnl"}.get(p.order, "mon_roi")
    actives = [r[0] for r in db.execute("SELECT addr FROM profile WHERE status='active'").fetchall()]
    new = [r[0] for r in db.execute(
        "SELECT addr FROM leaderboard WHERE is_candidate=1 AND addr NOT IN (SELECT addr FROM profile)"
        f" ORDER BY {order} DESC LIMIT ?", (p.limit,)).fetchall()]
    workset = actives + new
    print(f"scan: refresh {len(actives)} active + probe {len(new)} new = {len(workset)} wallets, window {p.days}d\n")

    # bulk pre-fetch prior profiles + lb account values once, so the worker threads never read the DB
    cols = storage.PROFILE_COLS.split(",")
    priors = {r[0]: dict(zip(cols, r)) for r in
              db.execute(f"SELECT {storage.PROFILE_COLS} FROM profile").fetchall()}
    lbs = {a: {"account_value": av} for a, av in
           db.execute("SELECT addr, account_value FROM leaderboard").fetchall()}

    added = retired = rejected = kept = 0
    workers = max(1, getattr(p, "workers", 8))      # I/O-bound; the REST pacer still caps total rate

    def _work(addr):
        prior = priors.get(addr)
        return addr, prior, _profile_one(db, addr, start_ms, now_ms, p, prior, lbs.get(addr, {}), stamp, universe)

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in concurrent.futures.as_completed([ex.submit(_work, a) for a in workset]):
            done += 1
            try:
                addr, prior, (status, reason, m, hit_cap) = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  [{done}/{len(workset)}] FAIL: {exc}")
                continue
            if status == "active":
                if (prior or {}).get("status") == "active":
                    kept += 1
                else:
                    added += 1
                    print(f"  + NEW  {addr}  roiEq={m['roi_equity']*100:+.1f}% net=${m['net_pnl']:,.0f} "
                          f"trd={m['n_trades']} {m['trades_per_day']:.1f}/d taker={m['taker_frac_notl']*100:.0f}% "
                          f"hold={m['median_hold_s']/3600:.1f}h win={m['win_rate']*100:.0f}% "
                          f"perp={m['perp_frac']*100:.0f}% age={m.get('age_days') or 0:.0f}d{' [capped]' if hit_cap else ''}")
            elif status == "retired":
                retired += 1
                print(f"  - RETIRE {addr}  ({reason})")
            else:
                rejected += 1

    n_active = refresh_watchlist(db, stamp)
    candidates = db.execute("SELECT count(*) FROM leaderboard WHERE is_candidate=1").fetchone()[0]
    _record_run(db, started, t0, candidates, len(new), added, retired, kept, rejected, n_active)
    print(f"\nscan done in {time.time()-t0:.0f}s: +{added} new, -{retired} retired, {kept} kept, "
          f"{rejected} rejected. watchlist now: {n_active} active.", flush=True)


# ------------------------------------------------------------------------ watchlist
def watchlist(db, top: int) -> None:
    """Show OUR curated tiny leaderboard (the watchlist table)."""
    rows = db.execute(
        "SELECT w.rank,w.addr,w.score,w.roi_equity,w.mon_roi,w.win_rate,w.max_drawdown,w.acct_value,"
        "w.lev_proxy,w.margin_type,w.cur_leverage,w.liq_worst_pct,w.taker_frac,w.median_hold_s,"
        "w.age_days,w.times_active,w.top_coin,w.display_name,COALESCE(c.enabled,1) "
        "FROM watchlist w LEFT JOIN target_controls c ON c.addr=w.addr ORDER BY w.rank LIMIT ?",
        (top,)).fetchall()
    print(f"\nWATCHLIST — {len(rows)} crypto-perp targets (core=consistent profit+survival; "
          f"lev/margin/liq are OBSERVED context, we copy isolated per-trade w/ our own cap)\n")
    hdr = (f"{'#':>2} {'addr':42} {'on':>2} {'score':>6} {'roiEq':>7} {'monRoi':>7} {'win':>4} {'maxDD%':>6} "
           f"{'lev':>5} {'margin':>7} {'worstLiq':>8} {'taker':>5} {'hold':>6} {'age':>5} {'seen':>4} {'coin':>6}")
    print(hdr); print("-" * len(hdr))
    for (rank, addr, sc, roi_eq, mon_roi, win, dd, acct, lev, mtype, curlev, liqw, taker, hold,
         age, ta, coin, name, on) in rows:
        ddp = (dd / acct * 100) if acct else 0
        levshow = curlev if curlev else (lev or 0)
        print(f"{rank:>2} {addr:42} {'Y' if on else 'n':>2} {sc:>6.1f} {roi_eq*100:>+6.1f}% "
              f"{(mon_roi or 0)*100:>+6.1f}% {win*100:>3.0f}% {ddp:>5.1f}% {levshow:>4.1f}x "
              f"{(mtype or '?'):>7} {(liqw or 0):>+7.1f}% {taker*100:>4.0f}% {hold/3600:>5.1f}h "
              f"{age or 0:>4.0f}d {ta:>4} {coin or '':>6}  {name or ''}")
