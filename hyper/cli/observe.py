#!/usr/bin/env python3
"""CLI entrypoint for the live observer + paper-copy simulator.

  python3 -m hyper.cli.observe --db data/hl.db observe --top 10
  python3 -m hyper.cli.observe --db data/hl.db report
"""
import argparse
import asyncio

from hyper import config, observer, params, selection, storage


def main() -> int:
    ap = argparse.ArgumentParser(description="HL copy-trade observer + paper sim")
    ap.add_argument("--db", default=config.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("observe")
    o.add_argument("--top", type=int, default=config.MAX_TARGETS,
                   help=f"hard cap on followed wallets (REST-rate ceiling, default {config.MAX_TARGETS})")
    o.add_argument("--add-frac", type=float, default=config.ADD_FRAC,
                   help=f"each scale-in ADD = first-open margin × this (default {config.ADD_FRAC}); OPEN sizing "
                        f"is volatility-TIERED: margin = available × <tier>_MARGIN_PCT (tune per-tier in dashboard)")
    o.add_argument("--extra", action="append", default=[],
                   help="extra address(es) to monitor for debugging")
    sub.add_parser("report")
    args = ap.parse_args()

    db = storage.connect(args.db, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
    params.seed_params(db)                          # ensure UI-tunable params exist (idempotent)
    if args.cmd == "observe":
        # A fill insert that loses a brief scanner write race is safely retried from the in-memory cursor.
        # Do not let SQLite's general 30s maintenance timeout freeze every Observer coroutine meanwhile.
        db.execute(f"PRAGMA busy_timeout={int(config.OBSERVER_DB_BUSY_TIMEOUT_MS)}")
        n = args.top
        addrs, seed = observer.load_targets(db, n)
        merged = []                                   # extras first, then watchlist, capped at n
        for a in [x.lower() for x in args.extra] + addrs:
            if a not in merged:
                merged.append(a)
        addrs = merged[:n]
        seed = {a: seed.get(a, set()) for a in addrs}
        if not addrs:
            published = selection.latest_published_generation(db)
            held_n = db.execute(
                "SELECT COUNT(DISTINCT addr) FROM copy_position WHERE status='open'"
            ).fetchone()[0]
            if published is None:
                print("no published Core yet; observer is running idle and waiting for the first scan.")
            elif not held_n:
                print(f"selection {published} has zero enabled Core wallets; observer is running idle.")
            else:
                print(f"selection {published} has zero enabled Core wallets; managing {held_n} exit-only wallet(s).")
        else:
            print(f"observing {len(addrs)} targets (cap {n}): {', '.join(a[:8] for a in addrs)}")
        try:
            asyncio.run(observer.Observer(db, addrs, seed, top_n=n,
                                          add_frac=args.add_frac).run())
        except KeyboardInterrupt:
            print("stopped.")
    elif args.cmd == "report":
        observer.report(db)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
