#!/usr/bin/env python3
"""CLI entrypoint for the dashboard read-only API (M1).

  python3 hl_dashboard.py --db data/hl.db --port 8787
  python3 hl_dashboard.py --db data/hl.db --static web/dist      # also serve the built SPA

On startup it opens the db read-WRITE ONCE to ensure the dashboard tables exist (storage migrations)
and to seed the params table, then serves everything read-only. The Observer/Scanner remain the only
writers of business state.
"""
import argparse
import sqlite3

from hl import api, config, params, storage


_DASHBOARD_REQUIRED_TABLES = {
    "commands", "params", "scan_progress", "scan_generation", "follow_selection",
}


def _initialize_db(path: str) -> None:
    """Run migrations when possible, but do not make a read service depend on a scanner write lock.

    A selection publication intentionally holds one SQLite write transaction.  If the Dashboard happens
    to restart during that bounded transaction, the already-migrated database is safe to serve read-only;
    systemd restart loops and a blank operator screen are not.  A genuinely incomplete schema still fails
    closed and is retried after the writer releases the database.
    """
    try:
        db = storage.connect(path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        db = api.ro_connect(path)
        try:
            present = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            db.close()
        missing = _DASHBOARD_REQUIRED_TABLES - present
        if missing:
            raise RuntimeError(f"dashboard_schema_incomplete:{len(missing)}") from exc
        return
    try:
        params.seed_params(db)
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="HL copy-trade dashboard API (read-only)")
    ap.add_argument("--db", default=config.DEFAULT_DB)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--static", default=None, help="dir of built frontend to serve (optional)")
    args = ap.parse_args()

    _initialize_db(args.db)

    api.serve(args.db, host=args.host, port=args.port, static_dir=args.static)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
