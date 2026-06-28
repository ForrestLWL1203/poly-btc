#!/usr/bin/env python3
"""CLI entrypoint for the discovery scanner. Logic lives in hl/ (scanner, metrics,
rest, fills, storage). Run from the repo root so `import hl` resolves.

  python3 hl_discover.py --db data/hl.db scan --days 14 --scan-interval 8   # full sweep, paced
  python3 hl_discover.py --db data/hl.db watchlist
  python3 hl_discover.py --db data/hl.db harvest
"""
import argparse
import time
from types import SimpleNamespace

from hl import config, params, scanner, storage


def _scan_ns():
    """A scan args-namespace with operational defaults (matches the `scan` subparser); gate/harvest
    params get overlaid from the DB by params.apply_scanner_params."""
    return SimpleNamespace(days=14, limit=100000, order="mon_roi", no_harvest=False,
                           workers=4, scan_interval=8.0, max_pages=5, min_crypto=0.3,
                           exclude_hft=True, hft_min_hold_min=3.0)


def _serve_rescan(db):
    """Daemon: run a full stop-the-world scan ON DEMAND when the dashboard queues a `rescan` command.
    scanner.scan() consumes the command(s) + writes scan_progress/status. Skips if a scan is running."""
    config.MIN_POST_INTERVAL = 8.0                   # gentle REST pace; coexist with the observer
    print("rescan trigger daemon: watching commands ...", flush=True)
    while True:
        try:
            sp = db.execute("SELECT state FROM scan_progress WHERE id=1").fetchone()
            scanning = bool(sp and sp[0] == "scanning")
            if not scanning:
                scanner._set_scanner_proc(db, "idle", {"watching": True})   # keep heartbeat fresh (alive)
            pend = db.execute("SELECT id FROM commands WHERE status='pending' AND type='rescan' LIMIT 1").fetchone()
            if pend and not scanning:
                ns = params.apply_scanner_params(db, _scan_ns())
                print(f"rescan command #{pend[0]} -> running full scan", flush=True)
                scanner.scan(db, ns)                 # consumes pending rescan + writes progress/status
        except Exception as exc:  # noqa: BLE001
            print(f"rescan daemon error: {exc}", flush=True)
        time.sleep(3)


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
        pr.add_argument("--min-activity", type=float, default=0.21,
                        help="MINIMAL floor on active_days/lookback (~3 of 14d) — just rejects one-shot "
                             "noise. Low-freq-but-real traders are NOT killed here; the evidence-shrink "
                             "in score() ranks them DOWN until round-trips accumulate (soft, not hard)")
        pr.add_argument("--grid-max-adds", type=float, default=5.0,
                        help="reject grid/DCA: max scale-in orders in a single round-trip we can copy "
                             "(our model = open + MAX_ADDS adds; far above that we only get the worst entries)")
        pr.add_argument("--max-single-loss", type=float, default=0.10,
                        help="reject 扛单到爆: worst single round-trip loss as fraction of account "
                             "(cuts-losses-small wallets pass even at 50%% win; one disaster loss = out)")
        pr.add_argument("--no-exclude-hft", dest="exclude_hft", action="store_false", default=True,
                        help="by default reject sub-minute HFT scalpers (uncopyable at our latency); "
                             "pass this to allow them (only once a high-freq feed exists)")
        pr.add_argument("--hft-min-hold-min", type=float, default=3.0,
                        help="when excluding HFT: min median hold time in MINUTES (below = HFT, rejected)")

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

    sub.add_parser("serve-rescan", help="daemon: run a full scan on demand when a dashboard rescan command is queued")

    args = ap.parse_args()
    db = storage.connect(args.db, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)  # +control-plane tables
    params.seed_params(db)                               # ensure UI-tunable params exist (idempotent)
    if args.cmd == "scan":
        config.MIN_POST_INTERVAL = args.scan_interval   # slow this PROCESS's REST pace (trickle);
        params.apply_scanner_params(db, args)           # UI-tuned gates/harvest override CLI defaults
        scanner.scan(db, args)                          # the observer process keeps its own fast pace
    elif args.cmd == "serve-rescan":
        _serve_rescan(db)
    elif args.cmd == "watchlist":
        scanner.watchlist(db, args.top)
    elif args.cmd == "harvest":
        print(f"{scanner.harvest(db, args.min_acct, args.max_turnover, args)} candidates")
    elif args.cmd == "regate":
        params.apply_scanner_params(db, args)            # honor UI-tuned gates (incl HFT switch) on regate
        scanner.regate(db, args)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
