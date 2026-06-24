#!/usr/bin/env python3
"""Hyperliquid copy-trade discovery — a ROLLING SCANNER for followable PERP wallets.

Leg 1 of the plan: maintain a live watchlist of quality, stably-profitable,
COPYABLE perp wallets to follow. Not a one-shot screen — it runs on a schedule,
continuously DISCOVERING newly-emerged qualifiers and RETIRING ones that go
inactive/degrade (the strong rotate addresses on an open DEX, so freshness and
recency matter more than deep history).

Modelling rules baked in:
- fills != trades: one order is matched in many slices, so a round-trip "trade"
  can be thousands of fills. Frequency is measured in EPISODES, never raw fills.
- PERPS ONLY: spot fills (dir Buy/Sell, coin with '/' or '@') are dropped.
- Strength = ROI, not absolute $ (we have small capital). With leverage, ROI is
  return on EQUITY/margin, not notional: $200 at 5x = $1000 notional but only $200
  risked, so +$100 is 50% not 10%. fills carry no leverage field, so we measure
  return on equity = window net_pnl / account_value (leverage-correct, HL-equity).
  roi_notional (net/notional) is kept only as a secondary, leverage-free diagnostic.

  scan      : harvest -> gate on recent profitability -> profile work-set
              (all actives + top-N new) over a short window -> perp episodes /
              ROI / copyability / freshness -> upsert active/rejected/retired.
  watchlist : current active targets, ranked by score.
  harvest   : (manual) refresh candidate pool only.

  python3 hl_discover.py --db data/hl.db scan --days 14 --limit 120
  python3 hl_discover.py --db data/hl.db watchlist
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO = "https://api.hyperliquid.xyz/info"
UA = {"User-Agent": "hl-discover/0.3", "Accept": "application/json"}
FLAT = 1e-6

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS leaderboard (
    addr          TEXT PRIMARY KEY,
    display_name  TEXT,
    account_value REAL,
    day_pnl REAL,  day_roi REAL,  day_vlm REAL,
    week_pnl REAL, week_roi REAL, week_vlm REAL,
    mon_pnl REAL,  mon_roi REAL,  mon_vlm REAL,
    all_pnl REAL,  all_roi REAL,  all_vlm REAL,
    daily_turnover REAL,
    is_candidate  INTEGER DEFAULT 0,
    fetched_at    TEXT
);
CREATE TABLE IF NOT EXISTS profile (
    addr             TEXT PRIMARY KEY,
    status           TEXT,          -- active / rejected / retired
    reason           TEXT,
    score            REAL,
    n_fills          INTEGER,
    n_trades         INTEGER,       -- perp round-trip episodes
    window_days      REAL,
    trades_per_day   REAL,          -- EPISODES/day (not fills)
    taker_frac_notl  REAL,
    median_hold_s    REAL,
    win_rate         REAL,
    net_pnl          REAL,          -- window perp net (after fees), USD
    roi_equity       REAL,          -- net_pnl / account_value  (leverage-correct strength)
    roi_notional     REAL,          -- net_pnl / total notional (leverage-free diagnostic)
    total_notl       REAL,
    acct_value       REAL,          -- account equity snapshot at scan time
    perp_frac        REAL,          -- share of fills that are perp (not spot)
    gross_pnl        REAL,
    total_fee        REAL,
    n_coins          INTEGER,
    top_coin         TEXT,
    long_frac        REAL,
    max_drawdown     REAL,
    avg_notional     REAL,
    age_days         REAL,
    last_fill_ms     INTEGER,
    first_added      TEXT,
    last_refreshed   TEXT,
    times_seen       INTEGER DEFAULT 0,
    times_active     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS episode (
    addr        TEXT,
    coin        TEXT,
    side        TEXT,
    open_ms     INTEGER,
    close_ms    INTEGER,
    hold_s      REAL,
    net_pnl     REAL,
    fee         REAL,
    max_notl    REAL,
    n_fills     INTEGER,
    open_px     REAL,
    close_px    REAL,
    PRIMARY KEY (addr, coin, open_ms)
);
CREATE INDEX IF NOT EXISTS idx_ep_addr ON episode(addr);
CREATE INDEX IF NOT EXISTS idx_prof_status ON profile(status);
"""

PROFILE_COLS = (
    "addr,status,reason,score,n_fills,n_trades,window_days,trades_per_day,taker_frac_notl,"
    "median_hold_s,win_rate,net_pnl,roi_equity,roi_notional,total_notl,acct_value,perp_frac,"
    "gross_pnl,total_fee,n_coins,top_coin,long_frac,max_drawdown,avg_notional,age_days,"
    "last_fill_ms,first_added,last_refreshed,times_seen,times_active"
)  # 30 columns


# ----------------------------------------------------------------------------- http
def _get(url: str, retries: int = 3) -> object:
    err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(0.5 * (attempt + 1))
    raise err  # type: ignore[misc]


_last_post = [0.0]
MIN_INTERVAL = 0.16  # global pacing between any two POSTs (avoid 429)


def _post(body: dict, retries: int = 7) -> object:
    data = json.dumps(body).encode()
    hdr = {**UA, "Content-Type": "application/json"}
    err = None
    for attempt in range(retries):
        wait = MIN_INTERVAL - (time.time() - _last_post[0])
        if wait > 0:
            time.sleep(wait)
        _last_post[0] = time.time()
        try:
            req = urllib.request.Request(INFO, data=data, headers=hdr)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            err = exc
            time.sleep(min(2.0 ** attempt, 20.0) if exc.code == 429 else 0.5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(0.5 * (attempt + 1))
    raise err  # type: ignore[misc]


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _is_spot(coin: str) -> bool:
    """Spot coins are formatted 'TOKEN/USDC' or '@<index>'; perps are plain ('BTC')
    or builder perps ('xyz:CL')."""
    return ("/" in coin) or coin.startswith("@")


# -------------------------------------------------------------------------- harvest
def harvest(db: sqlite3.Connection, min_acct: float, max_turnover: float) -> int:
    data = _get(LEADERBOARD)
    rows = data["leaderboardRows"] if isinstance(data, dict) else data
    now = _now_iso()
    n_cand = 0
    for r in rows:
        w = {name: perf for name, perf in r.get("windowPerformances", [])}
        d, wk, mo, al = w.get("day", {}), w.get("week", {}), w.get("month", {}), w.get("allTime", {})
        acct = _f(r.get("accountValue"))
        turnover = (_f(mo.get("vlm")) / acct / 30.0) if acct > 0 else 0.0
        cand = (
            acct >= min_acct
            and _f(wk.get("pnl")) > 0
            and _f(mo.get("pnl")) > 0
            and 0 < turnover <= max_turnover
        )
        db.execute(
            "INSERT OR REPLACE INTO leaderboard (addr,display_name,account_value,"
            "day_pnl,day_roi,day_vlm,week_pnl,week_roi,week_vlm,mon_pnl,mon_roi,mon_vlm,"
            "all_pnl,all_roi,all_vlm,daily_turnover,is_candidate,fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["ethAddress"].lower(), r.get("displayName"), acct,
                _f(d.get("pnl")), _f(d.get("roi")), _f(d.get("vlm")),
                _f(wk.get("pnl")), _f(wk.get("roi")), _f(wk.get("vlm")),
                _f(mo.get("pnl")), _f(mo.get("roi")), _f(mo.get("vlm")),
                _f(al.get("pnl")), _f(al.get("roi")), _f(al.get("vlm")),
                turnover, 1 if cand else 0, now,
            ),
        )
        n_cand += 1 if cand else 0
    db.commit()
    return n_cand


# ------------------------------------------------------------------------ fills/eps
def fetch_window(addr: str, start_ms: int, max_pages: int, sleep: float = 0.0) -> tuple[list[dict], bool]:
    out: list[dict] = []
    seen: set = set()
    cur = start_ms
    for _ in range(max_pages):
        page = _post({"type": "userFillsByTime", "user": addr, "startTime": cur})
        if not isinstance(page, list) or not page:
            return out, False
        page.sort(key=lambda f: f["time"])
        for f in page:
            if f.get("tid") not in seen:
                seen.add(f.get("tid"))
                out.append(f)
        if len(page) < 2000:
            return out, False
        cur = page[-1]["time"] + 1
        if sleep:
            time.sleep(sleep)
    return out, True  # hit page cap


def account_birth_ms(addr: str) -> int | None:
    page = _post({"type": "userFillsByTime", "user": addr, "startTime": 0})
    if isinstance(page, list) and page:
        return min(f["time"] for f in page)
    return None


def build_episodes(fills: list[dict]) -> list[dict]:
    fills = sorted(fills, key=lambda f: f["time"])
    by_coin: dict[str, list[dict]] = {}
    for f in fills:
        by_coin.setdefault(f["coin"], []).append(f)
    episodes: list[dict] = []
    for coin, fs in by_coin.items():
        ep = None
        for f in fs:
            sz = _f(f["sz"])
            signed = sz if f["side"] == "B" else -sz
            pos0 = _f(f.get("startPosition"))
            pos1 = pos0 + signed
            if ep is None and abs(pos0) < FLAT and abs(pos1) >= FLAT:
                ep = {"coin": coin, "side": "long" if pos1 > 0 else "short",
                      "open_ms": f["time"], "open_px": _f(f["px"]), "net_pnl": 0.0,
                      "fee": 0.0, "max_notl": 0.0, "n_fills": 0}
            if ep is not None:
                ep["net_pnl"] += _f(f.get("closedPnl"))
                ep["fee"] += _f(f.get("fee"))
                ep["n_fills"] += 1
                ep["max_notl"] = max(ep["max_notl"], abs(pos1) * _f(f["px"]))
                ep["close_ms"] = f["time"]
                ep["close_px"] = _f(f["px"])
                if abs(pos1) < FLAT:
                    ep["hold_s"] = (ep["close_ms"] - ep["open_ms"]) / 1000.0
                    ep["net_pnl"] -= ep["fee"]
                    episodes.append(ep)
                    ep = None
    return episodes


def _max_drawdown(curve: list[float]) -> float:
    peak, mdd = -1e30, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


# ------------------------------------------------------------------------- metrics
def compute_metrics(fills: list[dict], eps: list[dict], now_ms: int) -> dict | None:
    if not fills or not eps:
        return None
    n_fills = len(fills)
    taker_notl = sum(_f(f["px"]) * _f(f["sz"]) for f in fills if f.get("crossed"))
    tot_notl = sum(_f(f["px"]) * _f(f["sz"]) for f in fills)
    first_ms, last_ms = fills[0]["time"], fills[-1]["time"]
    window_days = max((last_ms - first_ms) / 86400_000.0, 1e-9)
    holds = sorted(e["hold_s"] for e in eps)
    coins: dict[str, int] = {}
    for e in eps:
        coins[e["coin"]] = coins.get(e["coin"], 0) + 1
    top_coin = max(coins.items(), key=lambda kv: kv[1])[0]
    cum, curve = 0.0, []
    for e in sorted(eps, key=lambda e: e["close_ms"]):
        cum += e["net_pnl"]
        curve.append(cum)
    total_notl = sum(e["max_notl"] for e in eps)
    return {
        "n_fills": n_fills, "n_trades": len(eps), "window_days": window_days,
        "trades_per_day": len(eps) / window_days,
        "taker_frac_notl": (taker_notl / tot_notl) if tot_notl else 0.0,
        "median_hold_s": holds[len(holds) // 2],
        "win_rate": sum(1 for e in eps if e["net_pnl"] > 0) / len(eps),
        "net_pnl": cum, "gross_pnl": sum(e["net_pnl"] + e["fee"] for e in eps),
        "roi_notional": (cum / total_notl) if total_notl else 0.0, "total_notl": total_notl,
        "total_fee": sum(e["fee"] for e in eps),
        "n_coins": len(coins), "top_coin": top_coin,
        "long_frac": sum(1 for e in eps if e["side"] == "long") / len(eps),
        "max_drawdown": _max_drawdown(curve),
        "avg_notional": total_notl / len(eps),
        "last_fill_ms": last_ms,
    }


def gates(m: dict, now_ms: int, p: argparse.Namespace) -> tuple[bool, str]:
    if m["perp_frac"] < p.min_perp:
        return False, "spot_dominant"
    if (now_ms - m["last_fill_ms"]) / 86400_000.0 > p.inactive_days:
        return False, "inactive"
    if m["n_trades"] < p.min_trades:
        return False, "too_few_trades"
    if m["net_pnl"] <= 0:
        return False, "not_profitable"
    if m["trades_per_day"] > p.max_tpd:
        return False, "too_frequent"
    if m["taker_frac_notl"] < p.min_taker:
        return False, "maker_heavy"
    if m["median_hold_s"] < p.min_hold_h * 3600:
        return False, "hold_too_short"
    return True, "ok"


def score(m: dict) -> float:
    # small-capital copier ranks by RETURN RATE on equity (leverage-correct), risk-adjusted.
    roi = m["roi_equity"]
    dd_eq = m["max_drawdown"] / (m["acct_value"] + 1.0)
    rr = roi / (dd_eq + 0.01)                                  # equity-ROI return / risk
    base = rr * (0.5 + m["win_rate"]) * (0.5 + m["taker_frac_notl"])
    age = m.get("age_days") or 999
    fresh = 1.0 + max(0.0, (30.0 - age) / 30.0) * 0.5         # younger -> up to +50%
    persist = 0.5 + 0.5 * min(m.get("times_active", 1), 10) / 10.0
    return base * fresh * persist


# ---------------------------------------------------------------------------- scan
now_ms_iso = ""  # set per scan run


def _profile_one(db, addr, start_ms, now_ms, p, prior, lb):
    raw, hit_cap = fetch_window(addr, start_ms, p.max_pages)
    for f in raw:
        f["user"] = addr
    perp = [f for f in raw if not _is_spot(f["coin"])]
    perp_frac = (len(perp) / len(raw)) if raw else 0.0
    eps = build_episodes(perp)
    m = compute_metrics(perp, eps, now_ms)
    if m is None:
        m = {"n_fills": len(perp), "n_trades": 0, "window_days": 0, "trades_per_day": 0,
             "taker_frac_notl": 0, "median_hold_s": 0, "win_rate": 0, "net_pnl": 0,
             "roi_notional": 0, "total_notl": 0, "gross_pnl": 0, "total_fee": 0, "n_coins": 0,
             "top_coin": None, "long_frac": 0, "max_drawdown": 0, "avg_notional": 0,
             "last_fill_ms": raw[-1]["time"] if raw else 0}

    acct_value = _f((lb or {}).get("account_value"))
    m["perp_frac"] = perp_frac
    m["acct_value"] = acct_value
    m["roi_equity"] = (m["net_pnl"] / acct_value) if acct_value else 0.0
    m["times_active"] = (prior or {}).get("times_active", 0)

    if m["n_trades"] == 0:
        ok, reason = False, "no_perp_trades"
    else:
        ok, reason = gates(m, now_ms, p)
    m["times_active"] += 1 if ok else 0

    age_days = (prior or {}).get("age_days")
    if ok and age_days is None:
        try:
            birth = account_birth_ms(addr)
            if birth:
                age_days = (now_ms - birth) / 86400_000.0
        except Exception:  # noqa: BLE001
            pass
    m["age_days"] = age_days

    prev_status = (prior or {}).get("status")
    status = "active" if ok else ("retired" if prev_status == "active" else "rejected")
    first_added = (prior or {}).get("first_added") or (now_ms_iso if ok else None)
    m["score"] = score(m) if ok else 0.0

    if ok:
        db.execute("DELETE FROM episode WHERE addr=?", (addr,))
        db.executemany(
            "INSERT OR REPLACE INTO episode (addr,coin,side,open_ms,close_ms,hold_s,net_pnl,fee,max_notl,n_fills,open_px,close_px)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(addr, e["coin"], e["side"], e["open_ms"], e["close_ms"], e["hold_s"], e["net_pnl"],
              e["fee"], e["max_notl"], e["n_fills"], e["open_px"], e["close_px"]) for e in eps],
        )
    db.execute(
        f"INSERT OR REPLACE INTO profile ({PROFILE_COLS}) VALUES ({','.join('?' * 30)})",
        (
            addr, status, reason, m["score"], m["n_fills"], m["n_trades"], m["window_days"],
            m["trades_per_day"], m["taker_frac_notl"], m["median_hold_s"], m["win_rate"],
            m["net_pnl"], m["roi_equity"], m["roi_notional"], m["total_notl"], m["acct_value"],
            m["perp_frac"], m["gross_pnl"], m["total_fee"], m["n_coins"], m["top_coin"],
            m["long_frac"], m["max_drawdown"], m["avg_notional"], m["age_days"], m["last_fill_ms"],
            first_added, now_ms_iso, (prior or {}).get("times_seen", 0) + 1, m["times_active"],
        ),
    )
    db.commit()
    return status, reason, m, hit_cap


def scan(db: sqlite3.Connection, p: argparse.Namespace) -> None:
    global now_ms_iso
    now_ms = int(time.time() * 1000)
    now_ms_iso = _now_iso()
    start_ms = now_ms - p.days * 86400_000

    if not p.no_harvest:
        print("harvest leaderboard ...")
        n_cand = harvest(db, p.min_acct, p.max_turnover)
        print(f"  {n_cand} candidates (acct>={p.min_acct:g}, turnover<={p.max_turnover:g}x, week&month pnl>0)")

    order = {"mon_roi": "mon_roi", "week_roi": "week_roi", "mon_pnl": "mon_pnl"}.get(p.order, "mon_roi")
    actives = [r[0] for r in db.execute("SELECT addr FROM profile WHERE status='active'").fetchall()]
    new = [r[0] for r in db.execute(
        f"SELECT addr FROM leaderboard WHERE is_candidate=1 AND addr NOT IN (SELECT addr FROM profile)"
        f" ORDER BY {order} DESC LIMIT ?", (p.limit,)).fetchall()]
    workset = actives + new
    print(f"scan: refresh {len(actives)} active + probe {len(new)} new = {len(workset)} wallets, window {p.days}d\n")

    added, retired, rejected, kept = 0, 0, 0, 0
    for i, addr in enumerate(workset, 1):
        time.sleep(0.1)
        prior = None
        row = db.execute(f"SELECT {PROFILE_COLS} FROM profile WHERE addr=?", (addr,)).fetchone()
        if row:
            prior = dict(zip(PROFILE_COLS.split(","), row))
        lbrow = db.execute("SELECT account_value, week_roi, mon_roi FROM leaderboard WHERE addr=?", (addr,)).fetchone()
        lb = {"account_value": lbrow[0], "week_roi": lbrow[1], "mon_roi": lbrow[2]} if lbrow else {}
        try:
            status, reason, m, hit_cap = _profile_one(db, addr, start_ms, now_ms, p, prior, lb)
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

    n_active = db.execute("SELECT count(*) FROM profile WHERE status='active'").fetchone()[0]
    print(f"\nscan done: +{added} new, -{retired} retired, {kept} kept, {rejected} rejected this run.")
    print(f"watchlist now: {n_active} active.  ->  python3 hl_discover.py --db <db> watchlist")


# ----------------------------------------------------------------------- watchlist
def watchlist(db: sqlite3.Connection, top: int) -> None:
    rows = db.execute(
        "SELECT addr,score,roi_equity,net_pnl,acct_value,max_drawdown,n_trades,trades_per_day,"
        "taker_frac_notl,median_hold_s,win_rate,age_days,times_active,last_fill_ms,top_coin,perp_frac "
        "FROM profile WHERE status='active' ORDER BY score DESC LIMIT ?", (top,)).fetchall()
    meta = {r[0]: (r[1], r[2]) for r in db.execute("SELECT addr, display_name, mon_roi FROM leaderboard").fetchall()}
    now_ms = int(time.time() * 1000)
    print(f"\nACTIVE WATCHLIST — {len(rows)} perp targets (by score; roiEq/monRoi are leverage-correct equity ROI)\n")
    hdr = (f"{'#':>2} {'addr':42} {'score':>6} {'roiEq':>7} {'monRoi':>7} {'net$':>9} {'trd':>4} "
           f"{'t/d':>4} {'taker':>6} {'hold':>6} {'win':>4} {'age':>5} {'seen':>4} {'idle':>5} {'coin':>6}")
    print(hdr); print("-" * len(hdr))
    for i, r in enumerate(rows, 1):
        (addr, sc, roi_eq, net, acct, dd, trd, tpd, taker, hold, win, age, ta, lastfill, coin, perp) = r
        name, mon_roi = meta.get(addr, (None, 0))
        idle_h = (now_ms - (lastfill or now_ms)) / 3600_000.0
        print(f"{i:>2} {addr:42} {sc:>6.1f} {roi_eq*100:>+6.1f}% {(mon_roi or 0)*100:>+6.1f}% {net:>9,.0f} "
              f"{trd:>4} {tpd:>4.1f} {taker*100:>5.0f}% {hold/3600:>5.1f}h {win*100:>3.0f}% "
              f"{age or 0:>4.0f}d {ta:>4} {idle_h:>4.0f}h {coin or '':>6}  {name or ''}")


# ------------------------------------------------------------------------------ cli
def main() -> int:
    ap = argparse.ArgumentParser(description="Hyperliquid copy-trade rolling scanner (perps)")
    ap.add_argument("--db", default="data/hl.db")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="harvest + refresh actives + probe new -> update watchlist")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--limit", type=int, default=120, help="max NEW candidates to probe this run")
    s.add_argument("--order", choices=["mon_roi", "week_roi", "mon_pnl"], default="mon_roi")
    s.add_argument("--min-acct", type=float, default=50000)
    s.add_argument("--max-turnover", type=float, default=5.0)
    s.add_argument("--max-pages", type=int, default=15)
    s.add_argument("--max-tpd", type=float, default=10.0, help="max EPISODES/day")
    s.add_argument("--min-trades", type=int, default=4)
    s.add_argument("--min-taker", type=float, default=0.4)
    s.add_argument("--min-hold-h", type=float, default=1.0)
    s.add_argument("--min-perp", type=float, default=0.6, help="min fraction of fills that are perp")
    s.add_argument("--inactive-days", type=float, default=3.0)
    s.add_argument("--no-harvest", action="store_true")

    w = sub.add_parser("watchlist", help="show current active targets")
    w.add_argument("--top", type=int, default=40)

    h = sub.add_parser("harvest", help="refresh candidate pool only")
    h.add_argument("--min-acct", type=float, default=50000)
    h.add_argument("--max-turnover", type=float, default=5.0)

    args = ap.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
    if args.cmd == "scan":
        scan(db, args)
    elif args.cmd == "watchlist":
        watchlist(db, args.top)
    elif args.cmd == "harvest":
        print(f"{harvest(db, args.min_acct, args.max_turnover)} candidates")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
