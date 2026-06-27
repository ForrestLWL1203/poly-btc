#!/usr/bin/env python3
"""CLI entrypoint for the dashboard read-only API (M1).

  python3 hl_dashboard.py --db data/hl.db --port 8787
  python3 hl_dashboard.py --db data/hl.db --static web/dist      # also serve the built SPA

On startup it opens the db read-WRITE ONCE to ensure the dashboard tables exist (storage migrations)
and to seed the params table, then serves everything read-only. The Observer/Scanner remain the only
writers of business state.
"""
import argparse

from hl import api, config, params, storage


def main() -> int:
    ap = argparse.ArgumentParser(description="HL copy-trade dashboard API (read-only)")
    ap.add_argument("--db", default=config.DEFAULT_DB)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--static", default=None, help="dir of built frontend to serve (optional)")
    args = ap.parse_args()

    db = storage.connect(args.db, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
    params.seed_params(db)
    db.close()

    api.serve(args.db, host=args.host, port=args.port, static_dir=args.static)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
