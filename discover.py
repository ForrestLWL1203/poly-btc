#!/usr/bin/env python3
"""Historical discovery — find RECURRING profitable wallets in BTC 5min windows.

For each of the last N settled 5-minute windows:
  1. gamma  /markets?slug=<slug>&closed=true        -> conditionId + authoritative
     winner (outcomePrices; Polymarket's own resolution, NOT our Chainlink calc).
  2. data-api /v1/market-positions?market=<cid>&sortBy=TOTAL_PNL&limit=K  -> the
     top-K profitable positions PER SIDE (Up block + Down block). Both sides kept:
     winners (held to redeem) AND "technical" wallets that bought the losing side
     cheap and sold before settlement — winners-only would miss the latter.

Accumulate across windows and rank wallets by RECURRENCE (how many windows they
land in the top-K), not absolute PnL — capital sizes differ, consistency is the
signal. Settled windows are final, so collection is incremental/cacheable.

  python3 discover.py --db data/discover.db collect --windows 288 --per-side 5
  python3 discover.py --db data/discover.db rank --min-windows 5
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

from lib.market import MarketSeries, current_epoch_start

GAMMA = "https://gamma-api.polymarket.com/markets"
MARKET_POSITIONS = "https://data-api.polymarket.com/v1/market-positions"
UA = {"User-Agent": "poly-btc-discover/0.1", "Accept": "application/json"}

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS windows (
    slug         TEXT PRIMARY KEY,
    condition_id TEXT,
    start_epoch  INTEGER,
    winner_index INTEGER,      -- 0=Up 1=Down
    winner_side  TEXT,
    fetched_at   TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    slug          TEXT,
    wallet        TEXT,
    name          TEXT,
    outcome       TEXT,
    outcome_index INTEGER,
    won           INTEGER,     -- this position's side won the window
    total_pnl     REAL,
    realized_pnl  REAL,
    cash_pnl      REAL,
    avg_price     REAL,
    total_bought  REAL,
    PRIMARY KEY (slug, wallet, outcome_index),
    FOREIGN KEY (slug) REFERENCES windows(slug)
);
CREATE INDEX IF NOT EXISTS idx_pos_wallet ON positions(wallet);
"""


def _get(url: str, params: dict, retries: int = 3) -> object:
    full = url + "?" + urllib.parse.urlencode(params, doseq=True)
    err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(full, headers=UA), timeout=12) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(0.3 * (attempt + 1))
    raise err  # type: ignore[misc]


def gamma_settled(slug: str) -> dict | None:
    """The settled market for a window slug (closed=true surfaces historical ones)."""
    data = _get(GAMMA, {"slug": slug, "closed": "true"})
    if isinstance(data, list):
        for m in data:
            if isinstance(m, dict) and m.get("slug") == slug:
                return m
    return None


def winner_index(outcome_prices) -> int | None:
    try:
        p = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
        a, b = float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError):
        return None
    if a == b:
        return None
    return 0 if a > b else 1


def market_positions(cid: str, per_side: int) -> list[dict]:
    data = _get(MARKET_POSITIONS, {"market": cid, "sortBy": "TOTAL_PNL",
                                   "sortDirection": "DESC", "limit": per_side})
    return data if isinstance(data, list) else []


def collect(db: sqlite3.Connection, n_windows: int, per_side: int) -> None:
    series = MarketSeries.from_symbol("BTC")
    base = current_epoch_start()
    done = {r[0] for r in db.execute("SELECT slug FROM windows")}
    new = skipped = unresolved = 0
    for k in range(1, n_windows + 1):
        epoch = base - k * series.slug_step
        slug = series.epoch_to_slug(epoch)
        if slug in done:
            skipped += 1
            continue
        try:
            m = gamma_settled(slug)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {slug} gamma error: {exc}")
            continue
        if not m or not m.get("conditionId"):
            unresolved += 1
            continue
        wi = winner_index(m.get("outcomePrices"))
        if wi is None:
            unresolved += 1
            continue
        cid = m["conditionId"]
        tokens = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
        db.execute(
            "INSERT OR REPLACE INTO windows VALUES (?,?,?,?,?,?)",
            (slug, cid, epoch, wi, ["Up", "Down"][wi],
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )
        try:
            blocks = market_positions(cid, per_side)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {slug} positions error: {exc}")
            db.commit()
            continue
        for blk in blocks:
            for p in blk.get("positions", []):
                wallet = str(p.get("proxyWallet") or "").lower()
                if not wallet:
                    continue
                oi = int(p.get("outcomeIndex") if p.get("outcomeIndex") is not None else (0 if blk.get("token") == tokens[0] else 1))
                db.execute(
                    "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (slug, wallet, p.get("name") or "", p.get("outcome") or "", oi,
                     1 if oi == wi else 0,
                     _f(p.get("totalPnl")), _f(p.get("realizedPnl")), _f(p.get("cashPnl")),
                     _f(p.get("avgPrice")), _f(p.get("totalBought"))),
                )
        db.commit()
        new += 1
        if new % 20 == 0:
            print(f"  ... {new} new windows collected (k={k}/{n_windows})")
    print(f"collect done: +{new} windows  (skipped cached {skipped}, unresolved/missing {unresolved})")


def _f(v) -> float | None:
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return None


def rank(db: sqlite3.Connection, min_windows: int) -> None:
    n_win = db.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
    rows = db.execute(
        """SELECT wallet, MAX(name) name,
                  COUNT(DISTINCT slug) wins_in,
                  SUM(total_pnl) tot_pnl,
                  AVG(total_pnl) avg_pnl
           FROM positions
           GROUP BY wallet
           HAVING wins_in >= ?
           ORDER BY wins_in DESC, tot_pnl DESC
           LIMIT 40""",
        (min_windows,),
    ).fetchall()
    # Discovery only answers "who is consistently profitable" (recurrence). It does
    # NOT define behaviour — the market-positions snapshot only shows a wallet's
    # profitable leg, so it can't tell two-sided from one-sided. True behaviour comes
    # from per-wallet deep profiling, never from this table.
    print(f"windows in db: {n_win}   (ranked by RECURRENCE — a candidate finder, not a profiler)\n")
    hdr = f"{'wallet':42} {'name':18} {'windows':>8} {'cover':>6} {'totPnl':>10} {'avgPnl/win':>10}"
    print(hdr); print("-" * len(hdr))
    for w, name, wins_in, tot, avg in rows:
        cov = f"{100*wins_in//n_win}%" if n_win else "-"
        print(f"{w:42} {(name or '')[:18]:18} {wins_in:>8} {cov:>6} {tot:>10.0f} {avg:>10.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="BTC 5min historical wallet discovery")
    ap.add_argument("--db", default="data/discover.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--windows", type=int, default=288)
    c.add_argument("--per-side", type=int, default=5)
    r = sub.add_parser("rank")
    r.add_argument("--min-windows", type=int, default=5)
    args = ap.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
    if args.cmd == "collect":
        collect(db, args.windows, args.per_side)
    elif args.cmd == "rank":
        rank(db, args.min_windows)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
