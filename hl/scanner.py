"""Discovery domain: the rolling scanner that maintains the live watchlist.

harvest leaderboard -> coarse candidates -> profile work-set (actives + new + top rechecks)
over a short window -> perp episodes/metrics -> upsert active/rejected/retired.
Composes rest + fills + metrics + storage; holds no infra of its own.
"""
import concurrent.futures
import json
import os
import threading
import time

from . import auto_tune, config, follow_score, metrics, params, pipeline_audit, rest, storage
from .fills import build_episodes, is_spot
from .scanner_copy_bt import (
    apply_copy_bt_gate as _apply_copy_bt_gate,
    apply_sector_copy_bt_gate as _apply_sector_copy_bt_gate,
    copy_bt_overrides as _copy_bt_overrides,
    copy_bt_results as _copy_bt_results,
    copy_bt_sigmas as _copy_bt_sigmas,
    sector_copy_bt_results as _sector_copy_bt_results,
)
from .scanner_lifecycle import (
    profile_workset_breakdown as _profile_workset_breakdown,
    prune_discovery_cache as _prune_discovery_cache,
)
from .util import f, now_iso

_db_lock = threading.Lock()   # serializes sqlite writes across scanner worker threads


def _episode_rows(addr: str, eps: list) -> list:
    """Rows for episode storage; seq preserves same-ms flip/reopen episodes instead of replacing them."""
    seen = {}
    rows = []
    for e in eps:
        key = (e["coin"], e["open_ms"])
        seq = seen.get(key, 0)
        seen[key] = seq + 1
        rows.append((addr, e["coin"], e["side"], e["open_ms"], seq, e["close_ms"], e["hold_s"],
                     e["net_pnl"], e["fee"], e["max_notl"], e["n_fills"], e["open_px"], e["close_px"]))
    return rows


def _apply_follow_eligibility_gate(m: dict) -> tuple[bool, str]:
    """Final profile-level copyability gate after copy replay evidence is recorded.

    `apply_copy_bt_gate` handles copy PnL loss. This catches other clear followability failures
    such as too many target opens we could not copy or too little recent sample to trust.
    Missing copy evidence stays annotated downstream for old/incomplete DBs.
    """
    eligibility = follow_score.evaluate_follow_eligibility(m)
    if not eligibility.get("eligible"):
        return False, eligibility.get("status") or "follow_ineligible"
    return True, "ok"


def _load_cached_fills(db, addr, since):
    """Cached raw fills for addr in the [since, now] window (ASC). Empty for a never-scanned candidate."""
    with _db_lock:
        rows = db.execute("SELECT fill_json FROM candidate_fills WHERE addr=? AND time>=? ORDER BY time",
                          (addr, since)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r[0]))
        except (ValueError, TypeError):
            pass
    return out


def _store_cached_fills(db, addr, fills, window_start):
    """Upsert fills (dedup by tid) + prune anything older than the window. CALLER HOLDS _db_lock."""
    rows = [(addr, x.get("tid"), x["time"], json.dumps(x)) for x in fills if x.get("tid") is not None]
    if rows:
        db.executemany("INSERT OR IGNORE INTO candidate_fills (addr,tid,time,fill_json) VALUES (?,?,?,?)", rows)
    db.execute("DELETE FROM candidate_fills WHERE addr=? AND time<?", (addr, window_start))


def _due_for_full_resync(db):
    """True if no FULL re-sync in the last FULL_RESYNC_DAYS (fresh db / missing col → True). A full re-sync
    re-fetches everyone's window to heal any incremental gap (append-only fills → gap can only be missing)."""
    try:
        r = db.execute("SELECT MAX(finished_at) FROM scan_runs WHERE full=1").fetchone()
    except Exception:  # noqa: BLE001 — `full` column not yet added (old db)
        return True
    if not r or not r[0]:
        return True
    try:
        from datetime import datetime, timezone
        last = datetime.fromisoformat(str(r[0]).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).total_seconds() / 86400 >= config.FULL_RESYNC_DAYS
    except Exception:  # noqa: BLE001
        return True


def _copy_bt_cached_fills(db, addr, now_ms, p):
    """Cached copyable fills for regate's no-network copy replay."""
    days = int(getattr(p, "copy_bt_days", config.COPY_BT_DAYS) or config.COPY_BT_DAYS)
    start_ms = now_ms - days * 86400_000
    out = []
    for x in _load_cached_fills(db, addr, start_ms):
        coin = x.get("coin")
        if not coin or is_spot(coin):
            continue
        row = dict(x)
        row["user"] = addr
        out.append(row)
    return out


def _fetch_profile_fills(db, addr, window_start, p, full):
    """(raw_full ASC, hit_cap, new_fills_to_persist). Incremental unless `full`: load the cached window,
    fetch ONLY the delta since our cursor (max cached time − overlap), merge (tid-dedup). A never-cached
    candidate, or a delta that blows past the page cap (can't be trusted), falls back to a full re-fetch."""
    if not full:
        stored = _load_cached_fills(db, addr, window_start)
        cursor = max((x["time"] for x in stored), default=None)
        if cursor is not None:
            delta, hit_cap = rest.fetch_window(addr, max(window_start, cursor - config.POLL_OVERLAP_MS), p.max_pages)
            if not hit_cap:
                merged = {x.get("tid"): x for x in stored}
                merged.update({x.get("tid"): x for x in delta})
                raw_full = sorted((x for x in merged.values() if x["time"] >= window_start), key=lambda x: x["time"])
                return raw_full, False, delta
            # delta hit the cap → too many new fills to trust incrementally → full re-fetch (self-heal)
    raw_full, hit_cap = rest.fetch_window(addr, window_start, p.max_pages)
    return raw_full, hit_cap, raw_full


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
def harvest(db, p) -> int:
    """STAGE-1 leaderboard BOX (v5) — leaderboard windows only, ZERO per-wallet API. Gate ONLY on what
    the leaderboard can HONESTLY say; defer ALL profit JUDGMENT to the profile (real fills). Predicate:
      • acct ≥ floor                         → real capital (we copy by %, not $).
      • vlm_min ≤ 7d VOLUME ≤ vlm_max        → genuinely trading this week, but NOT a market-maker
                                               (billion-$/wk bots sit above the ceiling).
      • 7d & 30d & all-time PnL all > 0      → MULTI-WINDOW consistency: profitable across three
                                               horizons, not a one-window fluke (cheap robustness).
      • pv_min ≤ 7d pnl/volume ≤ pv_max      → profit is a PLAUSIBLE fraction of traded volume: below =
                                               razor-thin MM, above = profit too big for the volume =
                                               NOT trading (deposit/spot/airdrop ghost; real = 0.2-4%).
    Leaderboard ROI/PnL MAGNITUDE is deliberately NOT a gate — it's contaminated (top-ROI wallets are
    $0-volume HODLers/ghosts) and return magnitude belongs in the SCORE, not eligibility. Bots/grids are
    INVISIBLE to leaderboard aggregates (proven), so the profile's grid/worst_loss gates handle them."""
    min_acct = getattr(p, "min_acct", config.HARVEST_MIN_ACCT)
    vlm_min = getattr(p, "week_vlm_min", config.HARVEST_WEEK_VLM_MIN)
    vlm_max = getattr(p, "week_vlm_max", config.HARVEST_WEEK_VLM_MAX)
    pv_min = getattr(p, "pnl_vol_min", config.HARVEST_PNL_VOL_MIN)
    pv_max = getattr(p, "pnl_vol_max", config.HARVEST_PNL_VOL_MAX)
    rows = rest.get_leaderboard()
    now = now_iso()
    n_cand = 0
    db.execute("UPDATE leaderboard SET is_candidate=0")
    for r in rows:
        w = {name: perf for name, perf in r.get("windowPerformances", [])}
        d, wk, mo, al = w.get("day", {}), w.get("week", {}), w.get("month", {}), w.get("allTime", {})
        acct = f(r.get("accountValue"))
        turnover = (f(mo.get("vlm")) / acct / 30.0) if acct > 0 else 0.0   # stored for display only
        wk_vlm, wk_pnl = f(wk.get("vlm")), f(wk.get("pnl"))
        pnl_vol = (wk_pnl / wk_vlm) if wk_vlm > 0 else 0.0
        cand = (acct >= min_acct
                and vlm_min <= wk_vlm <= vlm_max                # trading this week, not an MM/bot
                and wk_pnl > 0 and f(mo.get("pnl")) > 0 and f(al.get("pnl")) > 0  # 3-window consistency
                and pv_min <= pnl_vol <= pv_max)               # profit plausible for the volume (not ghost)
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


_DAY_MS = 86400_000.0


def _open_snapshot(addr, dexes, open_eps, now_ms, acct):
    """Current OPEN-POSITION character across EVERY dex the wallet traded — the data that un-blinds the
    funnel to live positions (a trend trader's winning holds AND a 扛单's losing holds). clearinghouse-
    State is PER-DEX (standard call omits builder/stock xyz:* positions), so we query each dex and
    combine. Returns a dict (None if no dex answered):
      margin_type, cur_leverage, worst_underwater (<=0, most-negative adverse among material positions),
      open_unrealized (total signed $), open_loss_frac / open_win_frac (underwater / winning unrealized
      ÷ acct), bag_count (# material underwater positions), max_bag_days / max_win_days (longest hold,
      from the in-window open episodes' open_ms). Durations are a LOWER bound for positions opened
      pre-window. Tiny dust positions still count toward total unrealized, but do not drive the deep-bag
      score guard."""
    open_ms = {e["coin"]: e["open_ms"] for e in (open_eps or [])}    # coin -> when the live run started
    types, worst_uw = set(), 0.0
    tot_ntl, acct_val, answered, has_pos = 0.0, 0.0, False, False
    up_loss, up_win, bag_n, max_bag_d, max_win_d = 0.0, 0.0, 0, 0.0, 0.0
    perp_short, perp_notl = {}, 0.0                              # for spot-hedge detection
    for dex in dexes:
        cs = rest.clearinghouse_state(addr, dex=dex)             # dex None -> standard perp dex
        if not isinstance(cs, dict):
            continue
        answered = True
        ms = cs.get("marginSummary", {})
        acct_val = max(acct_val, f(ms.get("accountValue")))      # standard dex carries the real equity
        tot_ntl += f(ms.get("totalNtlPos"))
        for pp in cs.get("assetPositions", []) or []:
            has_pos = True
            p_ = pp.get("position", {})
            coin = p_.get("coin")
            types.add((p_.get("leverage") or {}).get("type"))
            szi, entry, pv = f(p_.get("szi")), f(p_.get("entryPx")), f(p_.get("positionValue"))
            upnl = f(p_.get("unrealizedPnl"))                    # HL's authoritative current unrealized
            days = (now_ms - open_ms[coin]) / _DAY_MS if coin in open_ms else 0.0
            perp_notl += abs(pv)
            if szi < 0:                                          # a SHORT — candidate hedge of a spot long
                perp_short[(coin or "").upper()] = perp_short.get((coin or "").upper(), 0.0) + abs(pv)
            risk_acct = acct or acct_val or 0.0
            material = True
            if risk_acct > 0:
                material = abs(pv) / risk_acct >= config.OPEN_RISK_MIN_POSITION_EQUITY_FRAC
            if entry and szi and material:
                mark = pv / abs(szi)
                worst_uw = min(worst_uw, (mark - entry) / entry * (1 if szi > 0 else -1))
            if upnl < 0:
                up_loss += upnl
                if material:
                    bag_n += 1
                    max_bag_d = max(max_bag_d, days)   # a material carried LOSS = a bag
            elif upnl > 0:
                up_win += upnl;   max_win_d = max(max_win_d, days)               # a carried WIN = trend value
    if not answered:
        return None
    # SPOT-HEDGE ratio: a perp SHORT offset by a spot LONG of the same token is a hedge (its perp PnL is
    # cancelled by spot → the naked perp leg we'd copy is a loss). Only fetch spot when there ARE shorts.
    hedge_ratio = 0.0
    if perp_short and perp_notl:
        ss = rest.spot_clearinghouse_state(addr)
        spot_val = {}
        for b in (ss.get("balances") if isinstance(ss, dict) else []) or []:
            tok, v = (b.get("coin") or "").upper(), f(b.get("entryNtl"))
            if v <= 0:
                continue
            spot_val[tok] = spot_val.get(tok, 0.0) + v
            if tok.startswith("U") and len(tok) > 1:            # Unit-wrapped major: UBTC->BTC, UETH->ETH
                spot_val[tok[1:]] = spot_val.get(tok[1:], 0.0) + v
        hedged = sum(min(notl, spot_val.get(c, 0.0)) for c, notl in perp_short.items())
        hedge_ratio = hedged / perp_notl
    types.discard(None)
    mt = next(iter(types)) if len(types) == 1 else ("mixed" if types else "flat")
    a = acct or acct_val or 1.0
    return {"margin_type": mt if has_pos else "flat",
            "cur_leverage": (tot_ntl / acct_val if acct_val else 0.0),
            "worst_underwater": worst_uw, "open_unrealized": up_loss + up_win,
            "open_loss_frac": up_loss / a, "open_win_frac": up_win / a,
            "bag_count": bag_n, "max_bag_days": max_bag_d, "max_win_days": max_win_d,
            "hedge_ratio": hedge_ratio}


def _profile_one(db, addr, start_ms, now_ms, p, prior, lb, stamp, universe):
    # ONE aggregated fetch per wallet (aggregateByTime -> ~1 page, trade-level). No separate
    # pre-screen call: gates() already rejects dormant ("inactive"), spot/opaque-dominant
    # ("spot_dominant") and no-trades ("no_perp_trades") on this same data — the old two-stage
    # split only existed to avoid a heavy raw fetch, which aggregation made cheap.
    # Fetch a LONG window (PROFILE_FETCH_DAYS) via the paginated fetch_window — it sorts ASCENDING and
    # caps at max_pages*2000 fills (NOT a single 2000-row page: user_fills_latest truncated active wallets
    # at 2000 AND returned newest-first unsorted, which broke window_days/trades_per_day/last_fill_ms and
    # over-rejected as hit_page_cap). We slice the 14d window for the existing scoring metrics (behaviour
    # unchanged) and use the full fetch for the multi-window / lifetime nets — still ONE fetch per wallet.
    window_start = now_ms - config.PROFILE_FETCH_DAYS * 86400_000
    full = getattr(p, "full_scan", False) or not config.INCREMENTAL_SCAN
    raw_full, hit_cap, new_fills = _fetch_profile_fills(db, addr, window_start, p, full)
    for x in raw_full:
        x["user"] = addr
    # only COPYABLE activity counts: crypto perps + transparent builder perps (stocks/commodities,
    # e.g. xyz:AAPL — in `universe`). Spot is excluded (is_spot); opaque/private builder dexes are
    # excluded by not being in `universe`. perp_frac = copyable-perp share of fills.
    perp_full = [x for x in raw_full if not is_spot(x["coin"]) and (not universe or x["coin"] in universe)]
    raw = [x for x in raw_full if x["time"] >= start_ms]          # 14d window slice (scoring metrics)
    perp = [x for x in perp_full if x["time"] >= start_ms]
    perp_frac = (len(perp) / len(raw)) if raw else 0.0
    eps, open_eps = build_episodes(perp)
    m = metrics.compute_metrics(perp, eps, now_ms, p.days)
    if m is None:
        m = {"n_fills": len(perp), "n_trades": 0, "window_days": 0, "trades_per_day": 0,
             "taker_frac_notl": 0, "median_hold_s": 0, "win_rate": 0, "net_pnl": 0, "gross_pnl": 0,
             "roi_notional": 0, "total_notl": 0, "total_fee": 0, "n_coins": 0, "top_coin": None,
             "long_frac": 0, "max_drawdown": 0, "avg_notional": 0, "hold_skew": 0,
             "last_fill_ms": raw[-1]["time"] if raw else 0, "active_days": 0, "activity_ratio": 0,
             "median_eps": 0, "pos_day_ratio": 0, "profit_conc": 0,
             "max_adds_per_ep": 0, "median_adds_per_ep": 0, "worst_loss": 0.0,
             "tp_move_pct": 0.0, "market_type": None, "crypto_frac": None}
    # multi-window / lifetime realized nets from the FULL history (in-memory, no extra fetch) — the
    # long-term stability cross-check + the net_life datum the 14d window can't see. Computed even when
    # the 14d window is empty (dormant-but-historically-profitable wallets still get a true net_life).
    eps_full, _ = build_episodes(perp_full)
    m.update(metrics.window_nets(eps_full, now_ms))

    acct_value = f((lb or {}).get("account_value"))
    m["perp_frac"] = perp_frac
    m["acct_value"] = acct_value
    # HL 官方 return-on-capital(净利/本金)三窗口 → score() 的 ROI 支柱(取代 net/名义)。None 保留以便加权归一。
    _lbroi = lambda k: (f(lb[k]) if lb and lb.get(k) is not None else None)
    m["week_roi"], m["mon_roi"], m["all_roi"] = _lbroi("week_roi"), _lbroi("mon_roi"), _lbroi("all_roi")
    m["roi_equity"] = (m["net_pnl"] / acct_value) if acct_value else 0.0
    m["worst_loss_pct"] = (m["worst_loss"] / acct_value) if acct_value else 0.0  # loss discipline (realized)
    m["times_active"] = (prior or {}).get("times_active", 0)
    m["lev_proxy"] = (m["avg_notional"] / acct_value) if acct_value else 0.0  # hist. eff. leverage
    m["liq_count"], m["liq_worst_pct"] = _self_liquidations(raw, addr, acct_value)
    # open-position character defaults (filled by the live snapshot in stage B). roi_total starts as the
    # realized-only roi and is upgraded to realized+unrealized once we read the wallet's live positions.
    m.update(open_underwater=0.0, open_unrealized=0.0, open_loss_frac=0.0, open_win_frac=0.0,
             bag_count=0, max_bag_days=0.0, max_win_days=0.0, hedge_ratio=0.0, roi_total=m["roi_equity"])
    m["margin_type"] = (prior or {}).get("margin_type")
    m["cur_leverage"] = (prior or {}).get("cur_leverage") or 0.0

    # STAGE A — cheap structural copyability (NO api). Front-of-funnel rejects (MM/HFT/grid/spot) that do
    # NOT kill a genuine trend trader. n_trades==0 (pure-hold) skips the episode-based checks → judged on
    # live positions in stage B. (Old behaviour auto-rejected n_trades==0 as 'no_closed_episode'.)
    if not perp:
        ok, reason = False, "no_copyable_perp_fills"
    elif hit_cap:
        ok, reason = False, "hit_page_cap"
    else:
        ok, reason = metrics.gates_structural(m, p)

    # STAGE B — fetch the LIVE open-position snapshot (un-blinds the funnel to held positions), fold in
    # realized+unrealized roi, then re-judge: held position = ACTIVE, 扛单 bags drag roi_total negative,
    # trend holders kept. Only structural survivors pay the extra clearinghouse call.
    if ok:
        dexes = {(c.split(":")[0] if ":" in c else None) for c in {x["coin"] for x in perp}}
        snap = _open_snapshot(addr, dexes, open_eps, now_ms, acct_value)
        if snap is not None:
            m["margin_type"] = snap["margin_type"]
            m["cur_leverage"] = snap["cur_leverage"]
            m["open_underwater"] = snap["worst_underwater"]
            for k in ("open_unrealized", "open_loss_frac", "open_win_frac",
                      "bag_count", "max_bag_days", "max_win_days", "hedge_ratio"):
                m[k] = snap[k]
            m["roi_total"] = ((m["net_pnl"] + snap["open_unrealized"]) / acct_value) if acct_value else 0.0
        # v7 PORTFOLIO — authoritative NET-of-fees, deposit-adjusted account perf (one call, all windows).
        # Fed to the ROI pillar (net, replacing leaderboard gross) + the turnover/edge-bps copyability filters.
        _pf = rest.portfolio(addr)
        _pw = rest.parse_portfolio(_pf, "week") or {}
        _pm = rest.parse_portfolio(_pf, "month") or {}
        m["pf_week_pnl"], m["pf_week_vlm"] = _pw.get("pnl"), _pw.get("vlm")
        m["pf_mon_pnl"], m["pf_mon_vlm"] = _pm.get("pnl"), _pm.get("vlm")
        m["pf_equity"] = _pw.get("equity") or _pm.get("equity")
        m["pf_max_dd"] = _pm.get("max_drawdown") or _pw.get("max_drawdown")   # 30d curve = fuller DD picture
        m["pf_turnover"], m["pf_edge_bps"] = _pw.get("turnover"), _pw.get("edge_bps")
        ok, reason = metrics.gates_state(m, now_ms, p)
    if ok:
        copy_results = _copy_bt_results(addr, perp_full, now_ms, p)
        sector_results = _sector_copy_bt_results(addr, perp_full, now_ms, p)
        ok, reason = _apply_sector_copy_bt_gate(m, copy_results, sector_results, p)
    if ok:
        ok, reason = _apply_follow_eligibility_gate(m)
    m["times_active"] += 1 if ok else 0

    # age is NOT fetched (a full-history call just for account age = wasteful, and would penalise a
    # new wallet with strong recent performance). Survival now leans on times_active (our own observed
    # cross-scan persistence), not age. Keep any age a prior run already had; never fetch a new one.
    m["age_days"] = (prior or {}).get("age_days")

    prev_status = (prior or {}).get("status")
    m["score"] = metrics.score(m) if ok else 0.0
    if ok and m["score"] < getattr(p, "min_active_score", config.MIN_ACTIVE_SCORE):
        ok, reason, m["score"] = False, "low_quality", 0.0     # v10 质量线: 分不够 → 不进 active (watchlist=全好钱包)
    status = "active" if ok else ("retired" if prev_status == "active" else "rejected")
    row = dict(m)                                    # keys match column names -> robust positional build
    row.update(addr=addr, status=status, reason=reason, last_refreshed=stamp,
               first_added=(prior or {}).get("first_added") or (stamp if ok else None),
               times_seen=(prior or {}).get("times_seen", 0) + 1)
    cols = storage.PROFILE_COLS.split(",")
    with _db_lock:
        _store_cached_fills(db, addr, new_fills, window_start)   # persist the delta + prune the window
        if ok:
            erows = _episode_rows(addr, eps)
            db.execute("DELETE FROM episode WHERE addr=?", (addr,))
            db.executemany(
                "INSERT OR REPLACE INTO episode "
                "(addr,coin,side,open_ms,seq,close_ms,hold_s,net_pnl,fee,max_notl,n_fills,open_px,close_px)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                erows)
            stored = db.execute("SELECT COUNT(*) FROM episode WHERE addr=?", (addr,)).fetchone()[0]
            if stored != len(eps):
                raise RuntimeError(f"episode consistency failed for {addr}: stored {stored}, built {len(eps)}")
        db.execute(f"INSERT OR REPLACE INTO profile ({storage.PROFILE_COLS}) "
                   f"VALUES ({','.join('?' * len(cols))})", [row.get(c) for c in cols])
        db.commit()
    return status, reason, m, hit_cap


# ------------------------------------------------------------------ curated outputs
def refresh_watchlist(db, stamp, source: str = "watchlist") -> int:
    """Rebuild OUR tiny leaderboard (watchlist) from active profiles. Derived view —
    profile stays the source of truth; operator settings in target_controls survive."""
    params.seed_params(db)
    prev_line = float(params.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE)
    prev_followed = {
        (r[0] or "").lower()
        for r in db.execute(
            "SELECT w.addr FROM watchlist w LEFT JOIN target_controls c ON c.addr=w.addr "
            "WHERE w.score>=? AND COALESCE(c.enabled,1)=1",
            (prev_line,),
        ).fetchall()
    }
    db.execute("DELETE FROM watchlist")
    cur = db.execute(
        "SELECT p.addr, l.display_name, p.score, p.roi_equity, l.mon_roi, p.net_pnl, p.acct_value, "
        "p.n_trades, p.trades_per_day, p.taker_frac_notl, p.median_hold_s, p.win_rate, p.max_drawdown, "
        "p.age_days, p.top_coin, p.market_type, p.tp_move_pct, p.roi_total, p.open_loss_frac, p.open_win_frac, "
        "p.perp_frac, p.lev_proxy, p.margin_type, p.cur_leverage, p.liq_worst_pct, "
        "p.times_active, p.first_added, p.last_fill_ms, "
        "p.copy_bt_net_pnl,p.copy_bt_win_rate,p.copy_bt_closed_n,p.copy_bt_open_fill_rate,"
        "p.copy_bt_liquidations,p.copy_bt_fee_drag,p.copy_bt_14d_net_pnl,p.copy_bt_14d_closed_n,"
        "p.copy_bt_7d_net_pnl,p.copy_bt_7d_closed_n,p.sector_copy_json,p.sector_policy_json "
        "FROM profile p LEFT JOIN leaderboard l ON l.addr=p.addr "
        "WHERE p.status='active' ORDER BY p.score DESC, p.addr")
    row_cols = [d[0] for d in cur.description]
    rows = [dict(zip(row_cols, r)) for r in cur.fetchall()]
    ranked = []
    for r in rows:
        score, detail = follow_score.compute_follow_score(r)
        detail = dict(detail or {})
        eligibility = follow_score.evaluate_follow_eligibility(r)
        base_score = float(score or 0.0)
        stability = {
            "previouslyFollowed": (r["addr"] or "").lower() in prev_followed,
            "baseFollowScore": base_score,
            "bonus": 0.0,
            "status": "new_or_unfollowed",
        }
        if not eligibility.get("eligible"):
            floor = float(getattr(config, "AUTO_FOLLOW_MIN_SCORE", 0.60))
            score = min(score, max(0.0, floor - 1e-9))
            detail.setdefault("reasons", []).extend(eligibility.get("reasons") or [])
            stability["status"] = "ineligible" if stability["previouslyFollowed"] else "new_or_unfollowed"
        elif stability["previouslyFollowed"]:
            keep_min = float(getattr(config, "AUTO_FOLLOW_KEEP_MIN_SCORE", 0.60))
            if base_score >= keep_min:
                bonus = float(getattr(config, "AUTO_FOLLOW_KEEP_BONUS", 0.0) or 0.0)
                if bonus > 0:
                    score = min(1.0, base_score + bonus)
                    stability["bonus"] = score - base_score
                    stability["status"] = "keep_bonus"
            else:
                stability["status"] = "too_weak_to_keep"
        detail["stability"] = stability
        r["follow_detail"] = detail
        r["follow_eligibility"] = eligibility
        r["follow_score"] = score
        ranked.append(r)
    ranked.sort(key=lambda r: (-(r["follow_score"] or 0.0), r["addr"]))
    for rank, r in enumerate(ranked, 1):
        db.execute(
            "INSERT INTO watchlist (rank,addr,display_name,score,roi_equity,mon_roi,net_pnl,acct_value,"
            "n_trades,trades_per_day,taker_frac,median_hold_s,win_rate,max_drawdown,age_days,top_coin,"
            "market_type,tp_move_pct,roi_total,open_loss_frac,open_win_frac,"
            "perp_frac,lev_proxy,margin_type,cur_leverage,liq_worst_pct,sector_copy_json,sector_policy_json,"
            "times_active,first_added,last_fill_ms,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rank, r["addr"], r["display_name"], r["follow_score"], r["roi_equity"], r["mon_roi"],
                r["net_pnl"], r["acct_value"], r["n_trades"], r["trades_per_day"], r["taker_frac_notl"],
                r["median_hold_s"], r["win_rate"], r["max_drawdown"], r["age_days"], r["top_coin"],
                r["market_type"], r["tp_move_pct"], r["roi_total"], r["open_loss_frac"], r["open_win_frac"],
                r["perp_frac"], r["lev_proxy"], r["margin_type"], r["cur_leverage"], r["liq_worst_pct"],
                r["sector_copy_json"], r["sector_policy_json"], r["times_active"], r["first_added"], r["last_fill_ms"], stamp,
            ))
        db.execute("INSERT OR IGNORE INTO target_controls (addr,enabled,updated_at) VALUES (?,1,?)",
                   (r["addr"], stamp))
    if getattr(config, "AUTO_FOLLOW_LINE_ENABLE", True) and ranked:
        try:
            choice = auto_tune.choose_follow_line_by_portfolio(db, ranked, stamp=stamp)
        except Exception as exc:  # noqa: BLE001 — wallet-count tuning must never abort discovery
            print(f"auto-follow portfolio line: fallback after error: {exc}", flush=True)
            choice = {"status": "fallback", "reason": "portfolio_error"}
        if choice.get("status") != "ok":
            choice = follow_score.choose_follow_line(
                ranked,
                min_score=float(getattr(config, "AUTO_FOLLOW_MIN_SCORE", 0.60)),
                min_n=int(getattr(config, "AUTO_FOLLOW_MIN_N", 7)),
                target_n=min(int(getattr(config, "AUTO_FOLLOW_TARGET_N", 16)), int(config.MAX_TARGETS)),
                max_n=min(int(getattr(config, "AUTO_FOLLOW_MAX_N", 20)), int(config.MAX_TARGETS)),
                cliff_gap=float(getattr(config, "AUTO_FOLLOW_CLIFF_GAP", 0.045)),
            )
            choice["status"] = "heuristic"
        desired = choice["line"]
        pipeline_audit.record_follow_line_choice(db, stamp, source, choice)
        prev = params.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE
        if abs(float(prev) - desired) > 0.0005:
            db.execute("UPDATE params SET value=?,updated_at=? WHERE key='MIN_FOLLOW_SCORE'", (f"{desired:.9f}", stamp))
            db.execute(
                "INSERT INTO commands (type,payload_json,owner,created_at) VALUES (?,?,?,?)",
                ("reload_params", json.dumps({
                    "by": "auto_follow_line",
                    "reason": choice.get("reason"),
                    "status": choice.get("status"),
                    "count": choice.get("count"),
                }), "scanner", stamp))
    # stamp follow-history for everyone CURRENTLY on the follow line (≥ MIN_FOLLOW_SCORE). A wallet that
    # has since dropped below keeps its old stamp → surfaces in the UI's "dropped" tab until it recovers.
    line = params.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE
    detail_by_addr = {
        r["addr"]: {
            "follow_detail": r.get("follow_detail"),
            "follow_eligibility": r.get("follow_eligibility"),
        }
        for r in ranked
    }
    pipeline_audit.record_watchlist_snapshot(db, stamp, source, line, detail_by_addr)
    db.executemany(
        "INSERT INTO follow_history (addr,last_followed_at,last_followed_score) VALUES (?,?,?) "
        "ON CONFLICT(addr) DO UPDATE SET last_followed_at=excluded.last_followed_at, "
        "last_followed_score=excluded.last_followed_score",
        [(a, stamp, s) for (a, s) in
         db.execute("SELECT addr, score FROM watchlist WHERE score >= ?", (line,)).fetchall()])
    db.commit()
    return len(rows)


def _maybe_auto_tune_margins(db, source: str, stamp: str) -> None:
    try:
        res = auto_tune.maybe_tune_margins(db, source=source, stamp=stamp)
    except Exception as exc:  # noqa: BLE001 — auto tuning must never abort discovery
        res = {
            "status": "error",
            "reason": "auto_tune_exception",
            "error": str(exc),
            "applied": False,
        }
        pipeline_audit.record_auto_tune_result(db, stamp, source, res)
        db.commit()
        print(f"auto-tune margin: skipped after {source}: {exc}", flush=True)
        return
    if res.get("status") != "ok":
        pipeline_audit.record_auto_tune_result(db, stamp, source, res)
        db.commit()
        print(f"auto-tune margin: {res.get('status')}", flush=True)
        return
    pipeline_audit.record_auto_tune_result(db, stamp, source, res)
    db.commit()
    margins = res.get("margins") or {}
    lev_caps = res.get("lev_caps") or {}
    add_params = res.get("add_params") or {}
    print(
        "auto-tune margin: "
        f"mult={res.get('selected_mult')} applied={bool(res.get('applied'))} "
        f"followed={res.get('followed_n')} "
        f"stable={margins.get('STABLE_MARGIN_PCT', 0) * 100:.2f}% "
        f"mid={margins.get('MID_MARGIN_PCT', 0) * 100:.2f}% "
        f"high={margins.get('HIGH_MARGIN_PCT', 0) * 100:.2f}% "
        f"lev={tuple(lev_caps.get(k) for k in ('STABLE_LEV_CAP', 'MID_LEV_CAP', 'HIGH_LEV_CAP'))} "
        f"full={(res.get('deploy_full_pct') or 0) * 100:.0f}% "
        f"add=k{add_params.get('ADD_GAP_K')} g{add_params.get('ADD_GAP_SHRINK_G')} "
        f"hard{add_params.get('ADD_MAX_HARD')}",
        flush=True,
    )


def refresh_watchlist_and_auto_tune(db, stamp: str, source: str = "scan", before_auto_tune=None) -> int:
    """Rebuild watchlist, choose the follow line, then tune sizing/add on the final followed set."""
    n_active = refresh_watchlist(db, stamp, source=source)
    if before_auto_tune:
        before_auto_tune()
        db.commit()
    _maybe_auto_tune_margins(db, source, stamp)
    return n_active


def _active_profile_addrs(db):
    return [r[0] for r in db.execute(
        "SELECT addr FROM profile WHERE status='active' ORDER BY score DESC, addr").fetchall()]


def _watchlist_addrs(db):
    return [r[0] for r in db.execute("SELECT addr FROM watchlist ORDER BY rank").fetchall()]


def ensure_watchlist_current(db, stamp=None) -> int:
    """Repair the derived watchlist if a previous scan died after profile updates but before rebuild."""
    active = _active_profile_addrs(db)
    current = _watchlist_addrs(db)
    if set(current) == set(active):
        return len(current)
    return refresh_watchlist(db, stamp or now_iso(), source="repair")


def _record_run(db, started, t0, candidates, profiled, added, retired, kept, rejected, n_active, full=0):
    db.execute(
        "INSERT INTO scan_runs (started_at,finished_at,duration_s,candidates,profiled,probed_new,added,"
        "retired,kept,rejected,n_active,full) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (started, now_iso(), round(time.time() - t0, 1), candidates, profiled, profiled, added, retired,
         kept, rejected, n_active, 1 if full else 0))
    db.commit()


def regate(db, p) -> int:
    """Re-apply gates() + score() on ALREADY-STORED profile metrics (no network, no re-fetch) and
    rebuild the watchlist. Thresholds (win/roiEq/dd/tpd/hold/...) can be tuned in seconds without a
    full re-sweep — the expensive part (fetching fills, building episodes) is already done."""
    now = int(time.time() * 1000)
    stamp = now_iso()
    p.copy_bt_sigmas = getattr(p, "copy_bt_sigmas", None) or _copy_bt_sigmas(db)
    p.copy_bt_overrides = getattr(p, "copy_bt_overrides", None) or _copy_bt_overrides(db)
    rows = db.execute(
        "SELECT p.addr,status,n_trades,n_fills,perp_frac,last_fill_ms,net_pnl,roi_equity,max_drawdown,"
        "acct_value,age_days,times_active,liq_worst_pct,active_days,activity_ratio,median_eps,avg_notional,"
        "pos_day_ratio,profit_conc,hold_skew,open_underwater,max_adds_per_ep,median_adds_per_ep,worst_loss_pct,median_hold_s,win_rate,"
        "roi_total,open_loss_frac,open_win_frac,bag_count,max_bag_days,liq_count,hedge_ratio,net_30d,net_life,reason,"
        "l.week_roi,l.mon_roi,l.all_roi,"                      # HL return-on-capital windows for the ROI pillar
        "p.pf_turnover,p.pf_mon_pnl,p.pf_mon_vlm,p.pf_week_pnl,p.pf_equity,"   # v7 portfolio net metrics (gates + ROI)
        "p.payoff_ratio,p.pf_week_vlm,"   # v9: needed so regate applies the SAME payoff + edge-decay gates as a scan
        "p.copy_bt_net_pnl,p.copy_bt_win_rate,p.copy_bt_closed_n,p.copy_bt_open_fill_rate,"
        "p.copy_bt_liquidations,p.copy_bt_fee_drag,p.copy_bt_14d_net_pnl,p.copy_bt_14d_closed_n,"
        "p.copy_bt_7d_net_pnl,p.copy_bt_7d_closed_n,p.sector_copy_json,p.sector_policy_json "
        "FROM profile p LEFT JOIN leaderboard l ON p.addr=l.addr").fetchall()
    # p90 per-episode fill count per wallet, from the stored episode table (regate has no fills to rebuild
    # from) — feeds the algo-slicer gate. p90 (not max) so a swing trader who sliced ONE illiquid-stock fill
    # isn't killed for a single outlier; only SYSTEMATIC slicing (≥10% heavy round-trips) trips it.
    _epw = {}
    for a, nf in db.execute("SELECT addr, n_fills FROM episode WHERE n_fills IS NOT NULL"):
        _epw.setdefault(a, []).append(nf)
    p90fe = {a: sorted(xs)[min(len(xs) - 1, int(len(xs) * 0.9))] for a, xs in _epw.items() if xs}
    # peak concurrent positions per wallet (sweep line over each episode's [open,close]) — the too_many_concurrent
    # gate. Computed HERE from the episode table (not a stored col) so regate applies the SAME gate as a scan.
    _iv = {}
    for a, om, cm in db.execute("SELECT addr, open_ms, close_ms FROM episode WHERE open_ms IS NOT NULL AND close_ms IS NOT NULL"):
        _iv.setdefault(a, []).append((om, cm))
    def _peakc(ivs):
        evts = sorted([(o, 1) for o, _c in ivs] + [(_c, -1) for _o, _c in ivs], key=lambda x: (x[0], x[1]))
        cur = pk = 0
        for _, d in evts:
            cur += d; pk = max(pk, cur)
        return pk
    concw = {a: _peakc(v) for a, v in _iv.items()}
    # win_pt (median winning per-trade % on notional) from the episode table → audit metric (same as scan)
    _wpt = {}
    for a, npnl, mnotl in db.execute("SELECT addr, net_pnl, max_notl FROM episode WHERE net_pnl>0 AND max_notl>0"):
        _wpt.setdefault(a, []).append(npnl / mnotl * 100)
    winptw = {a: sorted(v)[len(v) // 2] for a, v in _wpt.items() if v}
    n_active = 0
    for r in rows:
        (addr, old, n_tr, n_fills, perp_frac, last_fill, net, roi_eq, mdd, acct, age, ta, liqw,
         ad, ar, meps, avgnotl, pdr, conc, skew, uw, mxadds, mdadds, wloss, mhold, wr,
         roi_tot, oloss, owin, bagn, bagd, liqc, hedge, net30, netlife, old_reason,
         wkroi, moroi, alroi, pf_turn, pf_mpnl, pf_mvlm, pf_wpnl, pf_eq, pay, pf_wvlm,
         copy_net, copy_wr, copy_closed, copy_open_fill_rate, copy_liqs, copy_fee,
         copy14_net, copy14_closed, copy7_net, copy7_closed, sector_copy_json, sector_policy_json) = r
        m = {"n_trades": n_tr or 0, "n_fills": n_fills or 0, "perp_frac": perp_frac or 0.0, "last_fill_ms": last_fill or 0,
             "net_pnl": net or 0.0, "roi_equity": roi_eq or 0.0, "max_drawdown": mdd or 0.0,
             "acct_value": acct or 0.0, "age_days": age, "times_active": ta or 0,
             "liq_worst_pct": liqw or 0.0, "active_days": ad or 0, "activity_ratio": ar or 0.0,
             "median_eps": meps or 0.0, "avg_notional": avgnotl or 0.0, "pos_day_ratio": pdr or 0.0, "profit_conc": conc or 0.0,
             "hold_skew": skew or 0.0, "open_underwater": uw or 0.0, "median_hold_s": mhold,
             "win_rate": wr or 0.0, "max_adds_per_ep": mxadds or 0, "median_adds_per_ep": mdadds or 0,
             "p90_fills_ep": p90fe.get(addr, 0),   # p90 single-episode fills → algo-slicer gate (from episode table)
             "max_concurrent": concw.get(addr, 0), # peak simultaneous positions → too_many_concurrent gate
             "win_pt": winptw.get(addr, 0.0),       # median winning per-trade % → audit metric
             "worst_loss_pct": wloss or 0.0,
             # v4 open-position character (stored from the last scan; regate doesn't re-fetch live state)
             "roi_total": roi_tot if roi_tot is not None else (roi_eq or 0.0),
             "open_loss_frac": oloss or 0.0, "open_win_frac": owin or 0.0,
             "bag_count": bagn or 0, "max_bag_days": bagd or 0.0, "liq_count": liqc or 0,
             "hedge_ratio": hedge or 0.0,
             # v6 nets: None when scanned before this datum existed → net gates skip (safe pre-rescan)
             "net_30d": net30, "net_life": netlife,
             # HL return-on-capital windows (from leaderboard join) → score() ROI pillar. None → weight-renormalized.
             "week_roi": wkroi, "mon_roi": moroi, "all_roi": alroi,
             # v7 portfolio net metrics → turnover/edge gates + net-ROI pillar (None on profiles scanned pre-v7 → skip)
             "pf_turnover": pf_turn, "pf_mon_pnl": pf_mpnl, "pf_mon_vlm": pf_mvlm,
             "pf_week_pnl": pf_wpnl, "pf_equity": pf_eq,
             # v9: payoff (大亏小赚 gate) + week vlm (edge-decay gate) — MUST be here or regate skips both
             # gates the scan applies, silently re-activating wallets the scan rejected (the 128 vs 165 bug).
             "payoff_ratio": pay, "pf_week_vlm": pf_wvlm,
             "copy_bt_net_pnl": copy_net, "copy_bt_win_rate": copy_wr,
             "copy_bt_closed_n": copy_closed, "copy_bt_open_fill_rate": copy_open_fill_rate,
             "copy_bt_liquidations": copy_liqs, "copy_bt_fee_drag": copy_fee,
             "copy_bt_14d_net_pnl": copy14_net, "copy_bt_14d_closed_n": copy14_closed,
             "copy_bt_7d_net_pnl": copy7_net, "copy_bt_7d_closed_n": copy7_closed,
             "sector_copy_json": sector_copy_json, "sector_policy_json": sector_policy_json}
        # realized loss-asymmetry from the STORED episodes (no network) — works even for profiles scanned
        # before loss_pain existed, so a regate alone re-ranks 小赚大亏 wallets without a full re-scan.
        m["loss_pain"] = metrics.loss_pain(
            [r0 for (r0,) in db.execute("SELECT net_pnl FROM episode WHERE addr=?", (addr,)).fetchall()])
        ok, reason = metrics.gates_structural(m, p)
        if ok:
            ok, reason = metrics.gates_state(m, now, p)        # uses the stored open-position metrics
        if ok:
            replay_fills = _copy_bt_cached_fills(db, addr, now, p)
            copy_results = _copy_bt_results(addr, replay_fills, now, p)
            sector_results = _sector_copy_bt_results(addr, replay_fills, now, p)
            ok, reason = _apply_sector_copy_bt_gate(m, copy_results, sector_results, p)
        if ok:
            ok, reason = _apply_follow_eligibility_gate(m)
        score = metrics.score(m) if ok else 0.0
        if ok and score < getattr(p, "min_active_score", config.MIN_ACTIVE_SCORE):
            ok, reason, score = False, "low_quality", 0.0      # v10 质量线: 分不够 → 不进 active (watchlist=全好钱包)
        status = "active" if ok else ("retired" if old == "active" else "rejected")
        db.execute(
            "UPDATE profile SET status=?,reason=?,score=?,loss_pain=?,max_concurrent=?,win_pt=?,"
            "copy_bt_net_pnl=?,copy_bt_win_rate=?,copy_bt_closed_n=?,copy_bt_open_fill_rate=?,"
            "copy_bt_liquidations=?,copy_bt_fee_drag=?,copy_bt_14d_net_pnl=?,copy_bt_14d_closed_n=?,"
            "copy_bt_7d_net_pnl=?,copy_bt_7d_closed_n=?,sector_copy_json=?,sector_policy_json=? WHERE addr=?",
            (status, reason, score, m["loss_pain"], concw.get(addr, 0), winptw.get(addr, 0.0),
             m.get("copy_bt_net_pnl"), m.get("copy_bt_win_rate"), m.get("copy_bt_closed_n"),
             m.get("copy_bt_open_fill_rate"), m.get("copy_bt_liquidations"), m.get("copy_bt_fee_drag"),
             m.get("copy_bt_14d_net_pnl"), m.get("copy_bt_14d_closed_n"),
             m.get("copy_bt_7d_net_pnl"), m.get("copy_bt_7d_closed_n"),
             m.get("sector_copy_json"), m.get("sector_policy_json"),
             addr),
        )
        n_active += 1 if ok else 0
    db.commit()
    def _record_regate_profile_audit():
        pipeline_audit.record_profile_snapshot(db, stamp, "regate")

    n = refresh_watchlist_and_auto_tune(
        db,
        stamp,
        source="regate",
        before_auto_tune=_record_regate_profile_audit,
    )
    print(f"regate: {n_active} active / {len(rows)} profiles  ->  watchlist {n}")
    return n


# ----------------------------------------------------------------------------- scan
def scan(db, p) -> None:
    now_ms = int(time.time() * 1000)
    started, t0 = now_iso(), time.time()
    stamp = now_iso()
    start_ms = now_ms - p.days * 86400_000
    ensure_watchlist_current(db, stamp)

    # dashboard: advertise we're scanning + consume any operator-queued rescan command
    rescan_rows = db.execute(
        "SELECT id, payload_json FROM commands WHERE status='pending' AND type='rescan'").fetchall()
    rescan_ids = [r[0] for r in rescan_rows]
    for cid in rescan_ids:
        db.execute("UPDATE commands SET status='acked',acked_at=? WHERE id=?", (now_iso(), cid))
    db.commit()
    # a rescan command may request a FULL sweep (dashboard 全量 checkbox) via its payload → re-profile
    # EVERYONE (not just the daily active+new tier); picked up by p.full_scan at the workset split below.
    for _, pj in rescan_rows:
        try:
            if pj and json.loads(pj).get("full"):
                p.full_scan = True
        except (ValueError, TypeError):
            pass
    # MANUAL (dashboard button → pending rescan command) vs AUTO (24h schedule, no command). The frontend
    # locks the page ONLY for manual scans; the auto scan runs SILENTLY in the background (it must be slow
    # since the observer owns the rate budget, so locking the UI for its full duration is unacceptable).
    manual = bool(rescan_ids)
    for tbl, col in (("scan_progress", "manual"), ("scan_runs", "full"), ("scan_runs", "profiled")):
        try:
            db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} INTEGER DEFAULT 0"); db.commit()
        except Exception:  # noqa: BLE001 — column already exists
            pass
    _set_scanner_proc(db, "scanning", {"phase": "harvest"})
    _set_scan_progress(db, state="scanning", started_at=started, stage="scan_leaderboard",
                       candidates_scanned=0, candidates_total=0, manual=1 if manual else 0)
    p.copy_bt_sigmas = _copy_bt_sigmas(db)
    p.copy_bt_overrides = _copy_bt_overrides(db)

    universe = rest.copyable_universe()          # crypto perps + transparent builder (stocks/commodities)
    if not p.no_harvest:
        print("harvest leaderboard ...", flush=True)
        n_cand = harvest(db, p)
        print(f"  {n_cand} candidates (acct>=${getattr(p,'min_acct',config.HARVEST_MIN_ACCT):,.0f}, "
              f"vol7d ${getattr(p,'week_vlm_min',config.HARVEST_WEEK_VLM_MIN):,.0f}.."
              f"${getattr(p,'week_vlm_max',config.HARVEST_WEEK_VLM_MAX):,.0f}, "
              f"pnl/vol {getattr(p,'pnl_vol_min',config.HARVEST_PNL_VOL_MIN):.1%}.."
              f"{getattr(p,'pnl_vol_max',config.HARVEST_PNL_VOL_MAX):.1%}, "
              f"7d&30d&all PnL>0)", flush=True)

    # FULL sweep every cycle (now cheap): re-profile EVERY candidate fresh — so a wallet that was
    # rejected on a past bad window gets re-discovered when it improves, and degraded actives retire.
    # No incremental "120 new + NOT IN profile" -> no permanent exclusion, no stale profiles.
    order = {"mon_roi": "mon_roi", "week_roi": "week_roi", "mon_pnl": "mon_pnl"}.get(p.order, "mon_roi")
    cand = [r[0] for r in db.execute(
        f"SELECT addr FROM leaderboard WHERE is_candidate=1 ORDER BY {order} DESC").fetchall()]
    active_addrs = [r[0] for r in db.execute("SELECT addr FROM profile WHERE status='active'").fetchall()]
    # FULL re-fetch when: --full, INCREMENTAL_SCAN off, or the FULL_RESYNC_DAYS self-heal cadence is due.
    p.full_scan = getattr(p, "full_scan", False) or (not config.INCREMENTAL_SCAN) or _due_for_full_resync(db)
    profiled = {r[0] for r in db.execute("SELECT addr FROM profile").fetchall()}
    workset_info = _profile_workset_breakdown(cand, active_addrs, profiled, p.full_scan, p.limit)
    pipeline_audit.record_workset_summary(db, stamp, "scan", workset_info)
    db.commit()
    workset, mode = workset_info["workset"], workset_info["mode"]
    cand_set = set(cand)
    off_active_n = len([a for a in active_addrs if a not in cand_set])
    _set_scan_progress(db, stage="fetch_history", candidates_total=len(workset))
    _pace = config.MIN_POST_INTERVAL   # live adaptive pace (fast when no copy-trading, slow trickle when observer up)
    print(f"scan: {mode} · {len(workset)} wallets (incl {off_active_n} off-list actives), "
          f"{p.days}d window, pace {_pace:g}s/req ({'FULL-SPEED 无跟单' if _pace <= config.SCAN_IDLE_INTERVAL else '慢采·跟单进行中'})\n")

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
            # progress on EVERY completion (single cheap 1-row UPDATE) so the mask's xxx/yyy moves
            # smoothly (~1 wallet/sec) instead of jumping every 10 (~14s frozen gaps → looked stuck).
            _set_scan_progress(db, stage="score_filter", candidates_scanned=done)
            if done % 10 == 0:
                _set_scanner_proc(db, "scanning", {"stage": "score_filter",   # refresh heartbeat so the
                                  "scanned": done, "total": len(workset)})    # dashboard isn't "心跳超时"

    _set_scan_progress(db, stage="rebuild_watchlist", candidates_scanned=len(workset))
    def _scan_before_auto_tune():
        pipeline_audit.record_profile_snapshot(db, stamp, "scan", workset)
        _set_scan_progress(db, stage="auto_tune", candidates_scanned=len(workset))

    n_active = refresh_watchlist_and_auto_tune(
        db,
        stamp,
        source="scan",
        before_auto_tune=_scan_before_auto_tune,
    )
    candidates = db.execute("SELECT count(*) FROM leaderboard WHERE is_candidate=1").fetchone()[0]
    _set_scan_progress(db, stage="persist")
    pruned = _prune_discovery_cache(db)
    pipeline_audit.record_prune_summary(db, stamp, "scan", pruned)
    db.commit()
    if any(pruned.values()):
        print(f"pruned discovery cache: {pruned}", flush=True)
    _record_run(db, started, t0, candidates, len(workset), added, retired, kept, rejected, n_active,
                full=getattr(p, "full_scan", False))
    print(f"\nscan done in {time.time()-t0:.0f}s: +{added} new, -{retired} retired, {kept} kept, "
          f"{rejected} rejected. watchlist now: {n_active} active.", flush=True)
    # dashboard: scan finished -> idle + resolve ALL queued rescan(s), INCLUDING any clicked DURING this
    # scan: a full sweep just completed so they're already satisfied -> absorb them instead of triggering
    # a redundant back-to-back scan.
    _set_scan_progress(db, state="idle", candidates_scanned=len(workset))
    _set_scanner_proc(db, "idle", {"last_scan_at": now_iso(), "active": n_active})
    db.execute("UPDATE commands SET status='done',done_at=?,result_json=? "
               "WHERE type='rescan' AND status IN ('pending','acked')",
               (now_iso(), json.dumps({"active": n_active})))
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
