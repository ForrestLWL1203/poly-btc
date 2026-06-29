#!/usr/bin/env python3
"""CLI entrypoint for the live observer + paper-copy simulator. Logic lives in
hl/ (observer, paper, ws, rest, storage). Needs the venv (websockets).

  python3 hl_observe.py --db data/hl.db observe --top 10
  python3 hl_observe.py --db data/hl.db report
"""
import argparse
import asyncio

from hl import config, observer, params, storage


def main() -> int:
    ap = argparse.ArgumentParser(description="HL copy-trade observer + paper sim")
    ap.add_argument("--db", default=config.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("observe")
    o.add_argument("--min-score", type=float, default=config.MIN_FOLLOW_SCORE,
                   help=f"follow watchlist wallets with v3 score >= this (quality threshold, default {config.MIN_FOLLOW_SCORE})")
    o.add_argument("--top", type=int, default=config.MAX_TARGETS,
                   help=f"hard cap on followed wallets (REST-rate ceiling, default {config.MAX_TARGETS})")
    o.add_argument("--add-margin-pct", type=float, default=config.ADD_MARGIN_PCT,
                   help=f"margin on each scale-in ADD as fraction of available (default {config.ADD_MARGIN_PCT}); "
                        f"OPEN margin = available×{config.MAX_MARGIN_PCT*100:g}%×conviction, lev mirrors master (tune in config)")
    o.add_argument("--extra", action="append", default=[],
                   help="extra address(es) to monitor for debugging")
    sub.add_parser("report")
    args = ap.parse_args()

    db = storage.connect(args.db, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
    params.seed_params(db)                          # ensure UI-tunable params exist (idempotent)
    if args.cmd == "observe":
        n = args.top
        f = params.load_follow(db)                     # UI-tuned follow line (params win over CLI)
        min_score = f.get("MIN_FOLLOW_SCORE") if f.get("MIN_FOLLOW_SCORE") is not None else args.min_score
        addrs, seed = observer.load_targets(db, n, min_score)
        merged = []                                   # extras first, then watchlist, capped at n
        for a in [x.lower() for x in args.extra] + addrs:
            if a not in merged:
                merged.append(a)
        addrs = merged[:n]
        seed = {a: seed.get(a, set()) for a in addrs}
        if not addrs:
            print("no enabled watchlist targets yet — run the scanner first.")
            return 1
        print(f"observing {len(addrs)} targets (score>={min_score}, cap {n}): {', '.join(a[:8] for a in addrs)}")
        try:
            asyncio.run(observer.Observer(db, addrs, seed, top_n=n, min_score=min_score,
                                          add_margin_pct=args.add_margin_pct).run())
        except KeyboardInterrupt:
            print("stopped.")
    elif args.cmd == "report":
        observer.report(db)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
