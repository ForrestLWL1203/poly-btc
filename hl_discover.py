#!/usr/bin/env python3
"""CLI entrypoint for the discovery scanner. Logic lives in hl/ (scanner, metrics,
rest, fills, storage). Run from the repo root so `import hl` resolves.

  python3 hl_discover.py --db data/hl.db scan --days 14 --limit 120
  python3 hl_discover.py --db data/hl.db watchlist
  python3 hl_discover.py --db data/hl.db harvest
"""
import argparse

from hl import config, scanner, storage


def main() -> int:
    ap = argparse.ArgumentParser(description="Hyperliquid copy-trade rolling scanner (perps)")
    ap.add_argument("--db", default=config.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="harvest + refresh actives + probe new -> update watchlist")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--limit", type=int, default=120, help="max NEW candidates to probe this run")
    s.add_argument("--order", choices=["mon_roi", "week_roi", "mon_pnl"], default="mon_roi")
    s.add_argument("--min-acct", type=float, default=50000)
    s.add_argument("--max-turnover", type=float, default=5.0)
    s.add_argument("--min-roi", type=float, default=0.20, help="modest 30d (month) ROI floor (coarse)")
    s.add_argument("--min-crypto", type=float, default=0.3, help="pre-screen: min recent crypto-fill share")
    s.add_argument("--max-pages", type=int, default=15)
    s.add_argument("--max-tpd", type=float, default=10.0, help="max EPISODES/day")
    s.add_argument("--min-trades", type=int, default=4)
    s.add_argument("--min-taker", type=float, default=0.4)
    s.add_argument("--min-hold-h", type=float, default=1.0)
    s.add_argument("--min-perp", type=float, default=0.6, help="min fraction of fills that are perp")
    s.add_argument("--inactive-days", type=float, default=3.0)
    s.add_argument("--no-harvest", action="store_true")

    w = sub.add_parser("watchlist", help="show our curated tiny leaderboard")
    w.add_argument("--top", type=int, default=40)

    h = sub.add_parser("harvest", help="refresh candidate pool only")
    h.add_argument("--min-acct", type=float, default=50000)
    h.add_argument("--max-turnover", type=float, default=5.0)
    h.add_argument("--min-roi", type=float, default=0.20)

    args = ap.parse_args()
    db = storage.connect(args.db, storage.DISCOVERY_SCHEMA)
    if args.cmd == "scan":
        scanner.scan(db, args)
    elif args.cmd == "watchlist":
        scanner.watchlist(db, args.top)
    elif args.cmd == "harvest":
        print(f"{scanner.harvest(db, args.min_acct, args.max_turnover, args)} candidates")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
