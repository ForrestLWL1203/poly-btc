"""Discovery domain: the rolling scanner that maintains the live watchlist.

harvest leaderboard -> coarse candidates -> profile work-set (actives + top-N new)
over a short window -> perp episodes/metrics -> upsert active/rejected/retired.
Composes rest + fills + metrics + storage; holds no infra of its own.
"""
import concurrent.futures
import json
import os
import threading
import time

from . import config, metrics, rest, storage
from .fills import build_episodes, is_spot
from .util import f, now_iso

_db_lock = threading.Lock()   # serializes sqlite writes across scanner worker threads


# -- dashboard status (best-effort; a status write must never break a real scan) ----------
def _set_scanner_proc(db, state, detail=None):
    try:
        db.execute("INSERT INTO process_status (name,state,pid,heartbeat_at,detail_json) VALUES "
                   "('scanner',?,?,?,?) ON CONFLICT(name) DO UPDATE SET state=excluded.state,"
                   "pid=excluded.pid,heartbeat_at=excluded.heartbeat_at,detail_json=excluded.detail_json",
                   (state, os.getpid(), now_iso(), json.dumps(detail or {})))
        db.commit()
    except Exception:  # noqa: BLE001
        pass


def _set_scan_progress(db, **kw):
    try:
        cur = db.execute("SELECT id FROM scan_progress WHERE id=1").fetchone()
        if cur is None:
            db.execute("INSERT INTO scan_progress (id,state,updated_at) VALUES (1,'idle',?)", (now_iso(),))
        sets = ",".join(f"{k}=?" for k in kw) + ",updated_at=?"
        db.execute(f"UPDATE scan_progress SET {sets} WHERE id=1", tuple(kw.values()) + (now_iso(),))
        db.commit()
    except Exception:  # noqa: BLE001
        pass


# -------------------------------------------------------------------------- harvest
def harvest(db, min_acct: float, max_turnover: float, p=None) -> int:
    """STAGE-1 seed gate — leaderboard windows only, ZERO per-wallet API. Pre-bias on what the
    leaderboard CAN reliably say; defer true stability + copyability to the profile. Predicate:
      • acct ≥ floor                 → real capital (we copy by %, not $).
      • lifetime profitable (all_roi>0) → real track record, not a lucky streak on a loser.
      • 7d VOLUME ≥ floor (week_vlm)  → ACTIVE over the week (NOT 24h — that kills mid-hold holders).
      • 30d ROI ≥ floor AND ≤ ceiling → meaningful return (small capital needs %) + anti-lottery.
      • 7d ROI ≥ floor                → recent week STILL earning; paired w/ the 30d floor this blocks
                                        both '+50% day-1 then dormant' AND a one-lucky-week fluke.
      • turnover ≤ ceiling           → not a market-maker.
    Bots/grids are INVISIBLE to leaderboard aggregates (proven), so we DON'T filter them here — the
    profile's grid/worst_loss gates do. Unrealized ROI allowed on purpose; realized judgment is profile."""
    week_vlm_min = getattr(p, "week_vlm_min", config.HARVEST_WEEK_VLM_MIN)
    week_roi_min = getattr(p, "week_roi_min", config.HARVEST_WEEK_ROI_MIN)
    mon_roi_min = getattr(p, "mon_roi_min", config.HARVEST_MON_ROI_MIN)
    mon_roi_max = getattr(p, "mon_roi_max", config.HARVEST_MON_ROI_MAX)
    rows = rest.get_leaderboard()
    now = now_iso()
    n_cand = 0
    for r in rows:
        w = {name: perf for name, perf in r.get("windowPerformances", [])}
        d, wk, mo, al = w.get("day", {}), w.get("week", {}), w.get("month", {}), w.get("allTime", {})
        acct = f(r.get("accountValue"))
        turnover = (f(mo.get("vlm")) / acct / 30.0) if acct > 0 else 0.0
        cand = (acct >= min_acct and 0 < turnover <= max_turnover  # real capital, not a market-maker
                and f(al.get("roi")) > 0                       # lifetime profitable (track record)
                and f(wk.get("vlm")) >= week_vlm_min           # 7d activity (keeps mid-hold holders)
                and mon_roi_min <= f(mo.get("roi")) <= mon_roi_max  # 30d return: high enough + anti-lottery
                and f(wk.get("roi")) >= week_roi_min)          # 7d also earning (blocks earn-then-dormant)
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


def _margin_snapshot(addr, dexes):
    """(margin_type, current_account_leverage, worst_open_underwater) aggregated across EVERY dex the
    wallet traded. clearinghouseState is PER-DEX: the standard call omits builder/stock-perp (xyz:*)
    positions, so a stock-heavy wallet's underwater would read 0 and inflate Health — we must query
    each dex (None = standard crypto) and combine. worst_open_underwater = most-negative adverse move
    ((mark-entry)/entry × side) across ALL open positions, <=0. Returns (None,0,0) if no dex answered;
    ('flat',0,0) if every queried dex is flat."""
    types, worst_uw = set(), 0.0
    tot_ntl, acct_val, answered, has_pos = 0.0, 0.0, False, False
    for dex in dexes:
        cs = rest.clearinghouse_state(addr, dex=dex)             # dex None -> standard perp dex
        if not isinstance(cs, dict):
            continue
        answered = True
        ms = cs.get("marginSummary", {})
        acct_val = max(acct_val, f(ms.get("accountValue")))      # standard dex carries the real equity
        tot_ntl += f(ms.get("totalNtlPos"))                      # notional sums across dexes
        for pp in cs.get("assetPositions", []) or []:
            has_pos = True
            p_ = pp.get("position", {})
            types.add((p_.get("leverage") or {}).get("type"))
            szi, entry, pv = f(p_.get("szi")), f(p_.get("entryPx")), f(p_.get("positionValue"))
            if entry and szi:
                mark = pv / abs(szi)                              # positionValue = |szi| × markPx
                adverse = (mark - entry) / entry * (1 if szi > 0 else -1)   # <0 = underwater
                worst_uw = min(worst_uw, adverse)
    if not answered:
        return None, 0.0, 0.0
    if not has_pos:
        return "flat", 0.0, 0.0
    types.discard(None)
    mt = next(iter(types)) if len(types) == 1 else ("mixed" if types else "flat")
    return mt, (tot_ntl / acct_val if acct_val else 0.0), worst_uw


def _profile_one(db, addr, start_ms, now_ms, p, prior, lb, stamp, universe):
    # ONE aggregated fetch per wallet (aggregateByTime -> ~1 page, trade-level). No separate
    # pre-screen call: gates() already rejects dormant ("inactive"), spot/opaque-dominant
    # ("spot_dominant") and no-trades ("no_perp_trades") on this same data — the old two-stage
    # split only existed to avoid a heavy raw fetch, which aggregation made cheap.
    raw, hit_cap = rest.fetch_window(addr, start_ms, p.max_pages)
    for x in raw:
        x["user"] = addr
    # only COPYABLE activity counts: crypto perps + transparent builder perps (stocks/commodities,
    # e.g. xyz:AAPL — in `universe`). Spot is excluded (is_spot); opaque/private builder dexes are
    # excluded by not being in `universe`. perp_frac = copyable-perp share of fills.
    perp = [x for x in raw if not is_spot(x["coin"]) and (not universe or x["coin"] in universe)]
    perp_frac = (len(perp) / len(raw)) if raw else 0.0
    eps = build_episodes(perp)
    m = metrics.compute_metrics(perp, eps, now_ms, p.days)
    if m is None:
        m = {"n_fills": len(perp), "n_trades": 0, "window_days": 0, "trades_per_day": 0,
             "taker_frac_notl": 0, "median_hold_s": 0, "win_rate": 0, "net_pnl": 0, "gross_pnl": 0,
             "roi_notional": 0, "total_notl": 0, "total_fee": 0, "n_coins": 0, "top_coin": None,
             "long_frac": 0, "max_drawdown": 0, "avg_notional": 0, "hold_skew": 0,
             "last_fill_ms": raw[-1]["time"] if raw else 0, "active_days": 0, "activity_ratio": 0,
             "median_eps": 0, "pos_day_ratio": 0, "profit_conc": 0,
             "max_adds_per_ep": 0, "median_adds_per_ep": 0, "worst_loss": 0.0,
             "market_type": None, "crypto_frac": None}

    acct_value = f((lb or {}).get("account_value"))
    m["perp_frac"] = perp_frac
    m["acct_value"] = acct_value
    m["roi_equity"] = (m["net_pnl"] / acct_value) if acct_value else 0.0
    m["worst_loss_pct"] = (m["worst_loss"] / acct_value) if acct_value else 0.0  # loss discipline
    m["times_active"] = (prior or {}).get("times_active", 0)
    m["lev_proxy"] = (m["avg_notional"] / acct_value) if acct_value else 0.0  # hist. eff. leverage
    m["liq_count"], m["liq_worst_pct"] = _self_liquidations(raw, addr, acct_value)
    m["open_underwater"] = 0.0

    if m["n_trades"] == 0:
        # split the old catch-all 'no_perp_trades' so the funnel explains itself: genuinely no
        # copyable fills, vs. has fills but no flat->flat round-trip in-window (long-hold / inventory),
        # vs. truncated at the page cap (too many slices to trust the episode reconstruction).
        ok = False
        if not perp:
            reason = "no_copyable_perp_fills"
        elif hit_cap:
            reason = "hit_page_cap"
        else:
            reason = "no_closed_episode"
    else:
        ok, reason = metrics.gates(m, now_ms, p)
    m["times_active"] += 1 if ok else 0

    m["margin_type"] = (prior or {}).get("margin_type")
    m["cur_leverage"] = (prior or {}).get("cur_leverage") or 0.0
    if ok:
        dexes = {(c.split(":")[0] if ":" in c else None) for c in {x["coin"] for x in perp}}
        mt, cl, uw = _margin_snapshot(addr, dexes)   # per-dex: standard + each builder dex traded
        if mt is not None:
            m["margin_type"], m["cur_leverage"], m["open_underwater"] = mt, cl, uw
    # age is NOT fetched (a full-history call just for account age = wasteful, and would penalise a
    # new wallet with strong recent performance). Survival now leans on times_active (our own observed
    # cross-scan persistence), not age. Keep any age a prior run already had; never fetch a new one.
    m["age_days"] = (prior or {}).get("age_days")

    prev_status = (prior or {}).get("status")
    status = "active" if ok else ("retired" if prev_status == "active" else "rejected")
    m["score"] = metrics.score(m) if ok else 0.0
    row = dict(m)                                    # keys match column names -> robust positional build
    row.update(addr=addr, status=status, reason=reason, last_refreshed=stamp,
               first_added=(prior or {}).get("first_added") or (stamp if ok else None),
               times_seen=(prior or {}).get("times_seen", 0) + 1)
    cols = storage.PROFILE_COLS.split(",")
    with _db_lock:
        if ok:
            db.execute("DELETE FROM episode WHERE addr=?", (addr,))
            db.executemany(
                "INSERT OR REPLACE INTO episode (addr,coin,side,open_ms,close_ms,hold_s,net_pnl,fee,max_notl,n_fills,open_px,close_px)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(addr, e["coin"], e["side"], e["open_ms"], e["close_ms"], e["hold_s"], e["net_pnl"],
                  e["fee"], e["max_notl"], e["n_fills"], e["open_px"], e["close_px"]) for e in eps])
        db.execute(f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                   f"VALUES ({','.join('?' * len(cols))})", [row.get(c) for c in cols])
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
        "p.age_days, p.top_coin, p.market_type, p.perp_frac, p.lev_proxy, p.margin_type, p.cur_leverage, p.liq_worst_pct, "
        "p.times_active, p.first_added, p.last_fill_ms "
        "FROM profile p LEFT JOIN leaderboard l ON l.addr=p.addr "
        "WHERE p.status='active' ORDER BY p.score DESC").fetchall()
    for rank, r in enumerate(rows, 1):
        db.execute(
            "INSERT INTO watchlist (rank,addr,display_name,score,roi_equity,mon_roi,net_pnl,acct_value,"
            "n_trades,trades_per_day,taker_frac,median_hold_s,win_rate,max_drawdown,age_days,top_coin,"
            "market_type,perp_frac,lev_proxy,margin_type,cur_leverage,liq_worst_pct,times_active,first_added,last_fill_ms,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (rank,) + r + (stamp,))
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
        "SELECT addr,status,n_trades,perp_frac,last_fill_ms,net_pnl,roi_equity,max_drawdown,"
        "acct_value,age_days,times_active,liq_worst_pct,active_days,activity_ratio,median_eps,"
        "pos_day_ratio,profit_conc,hold_skew,open_underwater,max_adds_per_ep,worst_loss_pct,reason "
        "FROM profile").fetchall()
    n_active = 0
    for r in rows:
        (addr, old, n_tr, perp_frac, last_fill, net, roi_eq, mdd, acct, age, ta, liqw,
         ad, ar, meps, pdr, conc, skew, uw, mxadds, wloss, old_reason) = r
        m = {"n_trades": n_tr or 0, "perp_frac": perp_frac or 0.0, "last_fill_ms": last_fill or 0,
             "net_pnl": net or 0.0, "roi_equity": roi_eq or 0.0, "max_drawdown": mdd or 0.0,
             "acct_value": acct or 0.0, "age_days": age, "times_active": ta or 0,
             "liq_worst_pct": liqw or 0.0, "active_days": ad or 0, "activity_ratio": ar or 0.0,
             "median_eps": meps or 0.0, "pos_day_ratio": pdr or 0.0, "profit_conc": conc or 0.0,
             "hold_skew": skew or 0.0, "open_underwater": uw or 0.0,
             "max_adds_per_ep": mxadds or 0, "worst_loss_pct": wloss or 0.0}  # grid_dca / blowup_loss gates
        if m["n_trades"] == 0:
            # no fills/episode info stored to re-derive the split -> keep the refined reason the scan
            # already recorded; map the legacy catch-all to the closest new bucket.
            ok = False
            reason = old_reason if old_reason in (
                "no_copyable_perp_fills", "hit_page_cap", "no_closed_episode") else "no_closed_episode"
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

    # dashboard: advertise we're scanning + consume any operator-queued rescan command
    rescan_ids = [r[0] for r in db.execute(
        "SELECT id FROM commands WHERE status='pending' AND type='rescan'").fetchall()]
    for cid in rescan_ids:
        db.execute("UPDATE commands SET status='acked',acked_at=? WHERE id=?", (now_iso(), cid))
    db.commit()
    _set_scanner_proc(db, "scanning", {"phase": "harvest"})
    _set_scan_progress(db, state="scanning", started_at=started, stage="scan_leaderboard",
                       candidates_scanned=0, candidates_total=0)

    universe = rest.copyable_universe()          # crypto perps + transparent builder (stocks/commodities)
    if not p.no_harvest:
        print("harvest leaderboard ...", flush=True)
        n_cand = harvest(db, p.min_acct, p.max_turnover, p)
        turn = "off" if p.max_turnover >= 1e8 else f"<={p.max_turnover:g}x"
        print(f"  {n_cand} candidates (acct>=${p.min_acct:g}, turnover {turn}, all_roi>0, "
              f"vol7d>=${getattr(p,'week_vlm_min',config.HARVEST_WEEK_VLM_MIN):,.0f}, "
              f"roi30d>={getattr(p,'mon_roi_min',config.HARVEST_MON_ROI_MIN):.0%}, "
              f"roi7d>={getattr(p,'week_roi_min',config.HARVEST_WEEK_ROI_MIN):.0%})", flush=True)

    # FULL sweep every cycle (now cheap): re-profile EVERY candidate fresh — so a wallet that was
    # rejected on a past bad window gets re-discovered when it improves, and degraded actives retire.
    # No incremental "120 new + NOT IN profile" -> no permanent exclusion, no stale profiles.
    order = {"mon_roi": "mon_roi", "week_roi": "week_roi", "mon_pnl": "mon_pnl"}.get(p.order, "mon_roi")
    cand = [r[0] for r in db.execute(
        f"SELECT addr FROM leaderboard WHERE is_candidate=1 ORDER BY {order} DESC").fetchall()]
    seen = set(cand)
    off_active = [r[0] for r in db.execute("SELECT addr FROM profile WHERE status='active'").fetchall()
                  if r[0] not in seen]                 # actives that fell off the candidate list — recheck too
    workset = (cand + off_active)[:p.limit]
    _set_scan_progress(db, stage="fetch_history", candidates_total=len(workset))
    print(f"scan: FULL sweep {len(workset)} wallets ({len(cand)} candidates + {len(off_active)} off-list "
          f"actives), {p.days}d window, pace {getattr(p, 'scan_interval', 0.8):g}s/req\n")

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
            if status == "active":                # per-wallet detail is in the profile table, not the log
                if (prior or {}).get("status") == "active":
                    kept += 1
                else:
                    added += 1
            elif status == "retired":
                retired += 1
            else:
                rejected += 1
            if done % 10 == 0:
                _set_scan_progress(db, stage="score_filter", candidates_scanned=done)

    _set_scan_progress(db, stage="rebuild_watchlist", candidates_scanned=len(workset))
    n_active = refresh_watchlist(db, stamp)
    candidates = db.execute("SELECT count(*) FROM leaderboard WHERE is_candidate=1").fetchone()[0]
    _set_scan_progress(db, stage="persist")
    _record_run(db, started, t0, candidates, len(workset), added, retired, kept, rejected, n_active)
    print(f"\nscan done in {time.time()-t0:.0f}s: +{added} new, -{retired} retired, {kept} kept, "
          f"{rejected} rejected. watchlist now: {n_active} active.", flush=True)
    # dashboard: scan finished -> idle + resolve queued rescan(s)
    _set_scan_progress(db, state="idle", candidates_scanned=len(workset))
    _set_scanner_proc(db, "idle", {"last_scan_at": now_iso(), "active": n_active})
    for cid in rescan_ids:
        db.execute("UPDATE commands SET status='done',done_at=?,result_json=? WHERE id=?",
                   (now_iso(), json.dumps({"active": n_active}), cid))
    db.commit()


# ------------------------------------------------------------------------ watchlist
def watchlist(db, top: int) -> None:
    """Show OUR curated tiny leaderboard (the watchlist table)."""
    rows = db.execute(
        "SELECT w.rank,w.addr,w.score,w.roi_equity,w.mon_roi,w.win_rate,w.max_drawdown,w.acct_value,"
        "w.lev_proxy,w.margin_type,w.cur_leverage,w.liq_worst_pct,w.taker_frac,w.median_hold_s,"
        "w.age_days,w.times_active,w.top_coin,w.display_name,COALESCE(c.enabled,1),"
        "COALESCE(p.max_adds_per_ep,0),COALESCE(p.worst_loss_pct,0) "
        "FROM watchlist w LEFT JOIN target_controls c ON c.addr=w.addr "
        "LEFT JOIN profile p ON p.addr=w.addr ORDER BY w.rank LIMIT ?",
        (top,)).fetchall()
    print(f"\nWATCHLIST — {len(rows)} crypto-perp targets (core=consistent profit+survival; "
          f"lev/margin/liq are OBSERVED context, we copy isolated per-trade w/ our own cap)\n"
          f"  grid = most scale-ins in one round-trip (gated); wLoss = worst single round-trip loss "
          f"(deep = 扛单到爆, shallow = 及时止损)\n")
    hdr = (f"{'#':>2} {'addr':42} {'on':>2} {'score':>6} {'roiEq':>7} {'monRoi':>7} {'win':>4} {'maxDD%':>6} "
           f"{'lev':>5} {'taker':>5} {'hold':>6} {'age':>5} {'seen':>4} {'grid':>5} {'wLoss':>6} {'coin':>6}")
    print(hdr); print("-" * len(hdr))
    for (rank, addr, sc, roi_eq, mon_roi, win, dd, acct, lev, mtype, curlev, liqw, taker, hold,
         age, ta, coin, name, on, grid, wloss) in rows:
        ddp = (dd / acct * 100) if acct else 0
        levshow = curlev if curlev else (lev or 0)
        flag = f"{grid:>4}!" if grid >= 10 else f"{grid:>5}"   # ! marks a likely grid/DCA wallet
        print(f"{rank:>2} {addr:42} {'Y' if on else 'n':>2} {sc:>6.1f} {roi_eq*100:>+6.1f}% "
              f"{(mon_roi or 0)*100:>+6.1f}% {win*100:>3.0f}% {ddp:>5.1f}% {levshow:>4.1f}x "
              f"{taker*100:>4.0f}% {hold/3600:>5.1f}h "
              f"{age or 0:>4.0f}d {ta:>4} {flag:>5} {(wloss or 0)*100:>+5.1f}% {coin or '':>6}  {name or ''}")
