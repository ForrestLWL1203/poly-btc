"""Command helpers for the offline copy backtest."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time

from . import config, params as params_mod
from .copy_backtest import run_backtest


def load_sigmas(db) -> dict:
    try:
        return {coin: sigma for coin, sigma in db.execute("SELECT coin,sigma FROM coin_vol WHERE sigma IS NOT NULL")}
    except sqlite3.Error:
        return {}


def load_cached_fills(db, addr: str, start_ms: int = 0) -> list:
    rows = db.execute(
        "SELECT fill_json FROM candidate_fills WHERE addr=? AND time>=? ORDER BY time",
        ((addr or "").lower(), int(start_ms or 0)),
    ).fetchall()
    fills = []
    for (raw,) in rows:
        try:
            fills.append(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return fills


def load_follow_overrides(db) -> dict:
    """Current dashboard follow params in engine units.

    Older test/dev DBs may not have the params table; in that case the backtest
    intentionally falls back to code defaults.
    """
    try:
        vals = params_mod.load_follow(db)
    except sqlite3.Error:
        return {}
    out = dict(vals)
    if "SMART_ADD" in vals:
        out["ADD_STRATEGY"] = "smart" if vals["SMART_ADD"] else "hardcap"
    return out


def wallet_context(db, addr: str) -> dict:
    out = {"addr": (addr or "").lower()}
    try:
        row = db.execute(
            "SELECT rank,score,win_rate,n_trades FROM watchlist WHERE addr=?",
            (out["addr"],),
        ).fetchone()
        if row:
            out.update(rank=row[0], score=row[1], hist_win_rate=row[2], hist_trades=row[3])
    except sqlite3.Error:
        pass
    try:
        row = db.execute(
            "SELECT payoff_ratio,max_adds_per_ep,median_adds_per_ep,max_concurrent FROM profile WHERE addr=?",
            (out["addr"],),
        ).fetchone()
        if row:
            out.update(payoff_ratio=row[0], max_adds_per_ep=row[1], median_adds_per_ep=row[2], max_concurrent=row[3])
    except sqlite3.Error:
        pass
    return out


def run_wallet(db, addr: str, days: int = 30, start_ms: int | None = None) -> dict:
    addr = (addr or "").lower()
    if start_ms is None:
        start_ms = int(time.time() * 1000) - int(days * 86400_000)
    sigmas = load_sigmas(db)
    fills = load_cached_fills(db, addr, start_ms)
    result = run_backtest(addr, fills, sigmas=sigmas, overrides=load_follow_overrides(db))
    used = {p["coin"] for p in result.get("positions", [])} | {p["coin"] for p in result.get("open_positions", [])}
    result.update(wallet_context(db, addr))
    result["fills"] = len(fills)
    result["sigmas"] = {coin: sigmas.get(coin) for coin in sorted(used) if sigmas.get(coin) is not None}
    return result


def _param_float(db, key, default):
    try:
        row = db.execute("SELECT value FROM params WHERE key=?", (key,)).fetchone()
        return float(row[0]) if row and row[0] is not None else default
    except (sqlite3.Error, TypeError, ValueError):
        return default


def followed_wallets(db, limit: int, min_score: float | None = None) -> list[str]:
    line = config.MIN_FOLLOW_SCORE if min_score is None else min_score
    if min_score is None:
        line = _param_float(db, "MIN_FOLLOW_SCORE", line)
    return [
        r[0]
        for r in db.execute(
            "SELECT w.addr FROM watchlist w LEFT JOIN target_controls c ON c.addr=w.addr "
            "WHERE COALESCE(c.enabled,1)=1 AND w.score>=? ORDER BY w.rank LIMIT ?",
            (line, int(limit)),
        ).fetchall()
    ]


def compact_row(r: dict) -> dict:
    return {
        "rank": r.get("rank"),
        "addr": r.get("addr"),
        "score": _pct(r.get("score")),
        "hist_wr": _pct(r.get("hist_win_rate")),
        "copy_wr": _pct(r.get("copy_win_rate")),
        "copy_pnl": round(r.get("copy_net_pnl") or 0.0, 2),
        "closed": r.get("closed_n"),
        "open": r.get("open_n"),
        "fees": round(r.get("fee_drag") or 0.0, 2),
        "miss_add": r.get("missed_adds"),
        "miss_add_rate": _pct(r.get("missed_add_rate")),
        "add_dep": round(r.get("add_dependency") or 0.0, 2),
        "conc_fit": round(r.get("max_concurrent_fit") or 0.0, 2),
        "open_fit": _pct(r.get("open_fill_rate")),
        "skips": r.get("skip_reasons") or {},
    }


def _pct(v):
    return None if v is None else round(float(v) * 100, 1)


def print_table(rows):
    headers = ["rk", "wallet", "score", "hist", "copy", "pnl", "closed", "fees", "miss/add", "dep", "fit"]
    print(" ".join(f"{h:>10}" for h in headers))
    for r in rows:
        c = compact_row(r)
        wallet = (c["addr"][:6] + "..." + c["addr"][-4:]) if c.get("addr") else "-"
        miss = f"{c['miss_add']}/{c['miss_add_rate']}%"
        print(
            f"{str(c['rank'] or '-'):>10} {wallet:>10} {str(c['score']):>10} {str(c['hist_wr']):>10} "
            f"{str(c['copy_wr']):>10} {c['copy_pnl']:>10.2f} {str(c['closed']):>10} {c['fees']:>10.2f} "
            f"{miss:>10} {c['add_dep']:>10.2f} {c['conc_fit']:>10.2f}"
        )


def position_rows(rows, limit=20):
    out = []
    for r in rows:
        for p in r.get("positions", []):
            q = dict(p)
            q["addr"] = r.get("addr")
            q["rank"] = r.get("rank")
            out.append(q)
    out.sort(key=lambda p: (p.get("add_dependency") or 0.0, p.get("missed_adds") or 0, abs(p.get("net_pnl") or 0.0)), reverse=True)
    return out[:limit]


def print_positions(rows, limit):
    pos = position_rows(rows, limit)
    if not pos:
        return
    print("\npositions by add dependency")
    headers = ["rk", "wallet", "coin", "side", "net", "dep", "target_add", "missed", "followed", "fees"]
    print(" ".join(f"{h:>10}" for h in headers))
    for p in pos:
        wallet = (p["addr"][:6] + "..." + p["addr"][-4:]) if p.get("addr") else "-"
        print(
            f"{str(p.get('rank') or '-'):>10} {wallet:>10} {p.get('coin','-'):>10} {p.get('side','-'):>10} "
            f"{(p.get('net_pnl') or 0.0):>10.2f} {(p.get('add_dependency') or 0.0):>10.2f} "
            f"{str(p.get('target_adds') or 0):>10} {str(p.get('missed_adds') or 0):>10} "
            f"{str(p.get('followed_adds') or 0):>10} {(p.get('fee_drag') or 0.0):>10.2f}"
        )


def main(argv=None):
    ap = argparse.ArgumentParser(description="Replay cached Hyperliquid fills through the copy engine rules.")
    ap.add_argument("--db", default="data/hl.db")
    ap.add_argument("--addr", action="append", default=[], help="wallet address; repeatable")
    ap.add_argument("--followed", action="store_true", help="run enabled watchlist wallets above the follow line")
    ap.add_argument("--limit", type=int, default=config.MAX_TARGETS)
    ap.add_argument("--min-score", type=float, default=None, help="native score line, e.g. 0.66")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--positions", type=int, default=0, help="also print top N high-add-dependency positions")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    db = sqlite3.connect(args.db)
    addrs = [(a or "").lower() for a in args.addr]
    if args.followed or not addrs:
        addrs.extend(a for a in followed_wallets(db, args.limit, args.min_score) if a not in addrs)
    rows = [run_wallet(db, a, days=args.days) for a in addrs]
    rows.sort(key=lambda r: (r.get("copy_net_pnl") or 0.0))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_table(rows)
        if args.positions:
            print_positions(rows, args.positions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
