"""Discovery domain: the rolling scanner that maintains the live watchlist.

harvest leaderboard -> coarse candidates -> profile work-set (actives + top-N new)
over a short window -> perp episodes/metrics -> upsert active/rejected/retired.
Composes rest + fills + metrics + storage; holds no infra of its own.
"""
import time

from . import metrics, rest, storage
from .fills import build_episodes, is_spot
from .util import f, now_iso


# -------------------------------------------------------------------------- harvest
def harvest(db, min_acct: float, max_turnover: float) -> int:
    rows = rest.get_leaderboard()
    now = now_iso()
    n_cand = 0
    for r in rows:
        w = {name: perf for name, perf in r.get("windowPerformances", [])}
        d, wk, mo, al = w.get("day", {}), w.get("week", {}), w.get("month", {}), w.get("allTime", {})
        acct = f(r.get("accountValue"))
        turnover = (f(mo.get("vlm")) / acct / 30.0) if acct > 0 else 0.0
        cand = (acct >= min_acct and f(wk.get("pnl")) > 0 and f(mo.get("pnl")) > 0
                and 0 < turnover <= max_turnover)
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
def _profile_one(db, addr, start_ms, now_ms, p, prior, lb, stamp):
    raw, hit_cap = rest.fetch_window(addr, start_ms, p.max_pages)
    for x in raw:
        x["user"] = addr
    perp = [x for x in raw if not is_spot(x["coin"])]
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

    if m["n_trades"] == 0:
        ok, reason = False, "no_perp_trades"
    else:
        ok, reason = metrics.gates(m, now_ms, p)
    m["times_active"] += 1 if ok else 0

    age_days = (prior or {}).get("age_days")
    if ok and age_days is None:
        try:
            birth = rest.account_birth_ms(addr)
            if birth:
                age_days = (now_ms - birth) / 86400_000.0
        except Exception:  # noqa: BLE001
            pass
    m["age_days"] = age_days

    prev_status = (prior or {}).get("status")
    status = "active" if ok else ("retired" if prev_status == "active" else "rejected")
    first_added = (prior or {}).get("first_added") or (stamp if ok else None)
    m["score"] = metrics.score(m) if ok else 0.0

    if ok:
        db.execute("DELETE FROM episode WHERE addr=?", (addr,))
        db.executemany(
            "INSERT OR REPLACE INTO episode (addr,coin,side,open_ms,close_ms,hold_s,net_pnl,fee,max_notl,n_fills,open_px,close_px)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(addr, e["coin"], e["side"], e["open_ms"], e["close_ms"], e["hold_s"], e["net_pnl"],
              e["fee"], e["max_notl"], e["n_fills"], e["open_px"], e["close_px"]) for e in eps])
    db.execute(
        f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) VALUES ({','.join('?' * 30)})",
        (addr, status, reason, m["score"], m["n_fills"], m["n_trades"], m["window_days"],
         m["trades_per_day"], m["taker_frac_notl"], m["median_hold_s"], m["win_rate"],
         m["net_pnl"], m["roi_equity"], m["roi_notional"], m["total_notl"], m["acct_value"],
         m["perp_frac"], m["gross_pnl"], m["total_fee"], m["n_coins"], m["top_coin"],
         m["long_frac"], m["max_drawdown"], m["avg_notional"], m["age_days"], m["last_fill_ms"],
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
        "p.age_days, p.top_coin, p.perp_frac, p.times_active, p.first_added, p.last_fill_ms "
        "FROM profile p LEFT JOIN leaderboard l ON l.addr=p.addr "
        "WHERE p.status='active' ORDER BY p.score DESC").fetchall()
    for rank, r in enumerate(rows, 1):
        db.execute(
            "INSERT INTO watchlist (rank,addr,display_name,score,roi_equity,mon_roi,net_pnl,acct_value,"
            "n_trades,trades_per_day,taker_frac,median_hold_s,win_rate,max_drawdown,age_days,top_coin,"
            "perp_frac,times_active,first_added,last_fill_ms,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (rank,) + r + (stamp,))
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


# ----------------------------------------------------------------------------- scan
def scan(db, p) -> None:
    now_ms = int(time.time() * 1000)
    started, t0 = now_iso(), time.time()
    stamp = now_iso()
    start_ms = now_ms - p.days * 86400_000

    if not p.no_harvest:
        print("harvest leaderboard ...", flush=True)
        n_cand = harvest(db, p.min_acct, p.max_turnover)
        print(f"  {n_cand} candidates (acct>={p.min_acct:g}, turnover<={p.max_turnover:g}x, week&month pnl>0)", flush=True)

    order = {"mon_roi": "mon_roi", "week_roi": "week_roi", "mon_pnl": "mon_pnl"}.get(p.order, "mon_roi")
    actives = [r[0] for r in db.execute("SELECT addr FROM profile WHERE status='active'").fetchall()]
    new = [r[0] for r in db.execute(
        "SELECT addr FROM leaderboard WHERE is_candidate=1 AND addr NOT IN (SELECT addr FROM profile)"
        f" ORDER BY {order} DESC LIMIT ?", (p.limit,)).fetchall()]
    workset = actives + new
    print(f"scan: refresh {len(actives)} active + probe {len(new)} new = {len(workset)} wallets, window {p.days}d\n")

    added = retired = rejected = kept = 0
    for i, addr in enumerate(workset, 1):
        time.sleep(0.1)
        row = db.execute(f"SELECT {storage.PROFILE_COLS} FROM profile WHERE addr=?", (addr,)).fetchone()
        prior = dict(zip(storage.PROFILE_COLS.split(","), row)) if row else None
        lbrow = db.execute("SELECT account_value, week_roi, mon_roi FROM leaderboard WHERE addr=?", (addr,)).fetchone()
        lb = {"account_value": lbrow[0]} if lbrow else {}
        try:
            status, reason, m, hit_cap = _profile_one(db, addr, start_ms, now_ms, p, prior, lb, stamp)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(workset)}] {addr[:12]} FAIL: {exc}")
            continue
        was_active = (prior or {}).get("status") == "active"
        if status == "active":
            if was_active:
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
        "SELECT w.rank,w.addr,w.score,w.roi_equity,w.mon_roi,w.net_pnl,w.n_trades,w.trades_per_day,"
        "w.taker_frac,w.median_hold_s,w.win_rate,w.age_days,w.times_active,w.last_fill_ms,w.top_coin,"
        "w.display_name,COALESCE(c.enabled,1) "
        "FROM watchlist w LEFT JOIN target_controls c ON c.addr=w.addr ORDER BY w.rank LIMIT ?",
        (top,)).fetchall()
    now_ms = int(time.time() * 1000)
    print(f"\nWATCHLIST (our tiny leaderboard) — {len(rows)} perp targets; roiEq/monRoi = leverage-correct equity ROI\n")
    hdr = (f"{'#':>2} {'addr':42} {'on':>2} {'score':>6} {'roiEq':>7} {'monRoi':>7} {'net$':>9} {'trd':>4} "
           f"{'t/d':>4} {'taker':>6} {'hold':>6} {'win':>4} {'age':>5} {'seen':>4} {'idle':>5} {'coin':>6}")
    print(hdr); print("-" * len(hdr))
    for (rank, addr, sc, roi_eq, mon_roi, net, trd, tpd, taker, hold, win, age, ta, lastfill, coin, name, on) in rows:
        idle_h = (now_ms - (lastfill or now_ms)) / 3600_000.0
        print(f"{rank:>2} {addr:42} {'Y' if on else 'n':>2} {sc:>6.1f} {roi_eq*100:>+6.1f}% "
              f"{(mon_roi or 0)*100:>+6.1f}% {net:>9,.0f} {trd:>4} {tpd:>4.1f} {taker*100:>5.0f}% "
              f"{hold/3600:>5.1f}h {win*100:>3.0f}% {age or 0:>4.0f}d {ta:>4} {idle_h:>4.0f}h {coin or '':>6}  {name or ''}")
