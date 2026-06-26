#!/usr/bin/env python3
"""CLI entrypoint for the discovery scanner. Logic lives in hl/ (scanner, metrics,
rest, fills, storage). Run from the repo root so `import hl` resolves.

  python3 hl_discover.py --db data/hl.db scan --days 14 --scan-interval 8   # full sweep, paced
  python3 hl_discover.py --db data/hl.db watchlist
  python3 hl_discover.py --db data/hl.db harvest
"""
import argparse

from hl import config, scanner, storage


def main() -> int:
    ap = argparse.ArgumentParser(description="Hyperliquid copy-trade rolling scanner (perps)")
    ap.add_argument("--db", default=config.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_gate_args(pr):
        # v3 ELIGIBILITY gates (the few binary thresholds; QUALITY is the continuous score in
        # metrics.score, shaped by config constants). No more hardcoded win/roi/dd cutoffs.
        pr.add_argument("--min-perp", type=float, default=0.6, help="min copyable-perp share of fills")
        pr.add_argument("--inactive-days", type=float, default=3.0, help="reject if no fill within N days")
        pr.add_argument("--max-daily-eps", type=float, default=30.0, help="reject bots: max median episodes/active-day")
        pr.add_argument("--min-activity", type=float, default=0.5, help="min active_days/lookback (regular trading)")
        pr.add_argument("--grid-max-adds", type=float, default=5.0,
                        help="reject grid/DCA: max scale-in orders in a single round-trip we can copy "
                             "(our model = open + MAX_ADDS adds; far above that we only get the worst entries)")
        pr.add_argument("--max-single-loss", type=float, default=0.10,
                        help="reject 扛单到爆: worst single round-trip loss as fraction of account "
                             "(cuts-losses-small wallets pass even at 50%% win; one disaster loss = out)")

    def add_harvest_args(pr):
        # STAGE-1 leaderboard prefilter (3-window cascade; 0 per-wallet API). Defaults in config.
        pr.add_argument("--min-acct", type=float, default=config.HARVEST_MIN_ACCT,
                        help="real-capital noise guard (we copy by pct, not $)")
        pr.add_argument("--max-turnover", type=float, default=config.HARVEST_MAX_TURNOVER,
                        help="anti-MM: daily turnover (mon_vlm/acct/30) ceiling")
        pr.add_argument("--week-vlm-min", type=float, default=config.HARVEST_WEEK_VLM_MIN,
                        help="7d volume floor = active over the WEEK (not 24h — keeps mid-hold holders)")
        pr.add_argument("--mon-roi-min", type=float, default=config.HARVEST_MON_ROI_MIN,
                        help="30d ROI FLOOR = meaningful return (small capital needs high %%)")
        pr.add_argument("--week-roi-min", type=float, default=config.HARVEST_WEEK_ROI_MIN,
                        help="7d ROI floor — paired w/ 30d floor: recent week must ALSO earn")
        pr.add_argument("--mon-roi-max", type=float, default=config.HARVEST_MON_ROI_MAX,
                        help="anti-lottery: max 30d ROI (cut tiny-acct gamblers)")

    s = sub.add_parser("scan", help="full sweep: re-profile ALL candidates -> rebuild watchlist")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--limit", type=int, default=100000, help="cap workset size (default ~unbounded = full sweep)")
    s.add_argument("--order", choices=["mon_roi", "week_roi", "mon_pnl"], default="mon_roi")
    add_harvest_args(s)
    s.add_argument("--min-crypto", type=float, default=0.3, help="(unused) legacy prescreen arg")
    s.add_argument("--max-pages", type=int, default=5, help="cap fill pages/wallet (aggregateByTime -> "
                   "14d is ~1 page; >5 pages of trade-level fills = HFT/MM we reject anyway)")
    s.add_argument("--workers", type=int, default=4, help="concurrent profiling threads (rate is capped by --scan-interval)")
    s.add_argument("--scan-interval", type=float, default=8.0,
                   help="REST pace (s/request) for the scan PROCESS — slow trickle so it shares the IP "
                        "rate limit with the always-on observer (8s = ~7.5/min, leaves ~67/min for copy)")
    add_gate_args(s)
    s.add_argument("--no-harvest", action="store_true")

    w = sub.add_parser("watchlist", help="show our curated tiny leaderboard")
    w.add_argument("--top", type=int, default=40)

    h = sub.add_parser("harvest", help="refresh candidate pool only")
    add_harvest_args(h)

    g = sub.add_parser("regate", help="re-apply gate thresholds on STORED profiles (no re-fetch) + rebuild watchlist")
    add_gate_args(g)

    args = ap.parse_args()
    db = storage.connect(args.db, storage.DISCOVERY_SCHEMA)
    if args.cmd == "scan":
        config.MIN_POST_INTERVAL = args.scan_interval   # slow this PROCESS's REST pace (trickle);
        scanner.scan(db, args)                          # the observer process keeps its own fast pace
    elif args.cmd == "watchlist":
        scanner.watchlist(db, args.top)
    elif args.cmd == "harvest":
        print(f"{scanner.harvest(db, args.min_acct, args.max_turnover, args)} candidates")
    elif args.cmd == "regate":
        scanner.regate(db, args)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
