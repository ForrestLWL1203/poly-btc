"""One-shot QuickNode collection credential worker."""

from __future__ import annotations

import argparse

from hyper import config, storage
from hyper.market.collection_control import (
    process_all_pending,
    process_pending_command,
    set_preferred_source,
    verify_existing_endpoint,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Hyperliquid collection source control worker")
    parser.add_argument("--db", default=config.DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    process = sub.add_parser("process")
    process.add_argument("--command-id", required=True, type=int)
    sub.add_parser("process-pending")
    sub.add_parser("verify-existing")
    select = sub.add_parser("select-source")
    select.add_argument("source", choices=("official", "quicknode"))
    args = parser.parse_args()

    if args.command == "process-pending":
        process_all_pending(args.db)
        return 0
    if args.command == "verify-existing":
        db = storage.connect(args.db, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        try:
            result = verify_existing_endpoint(db)
        finally:
            db.close()
        print(f"quicknode endpoint {result['status']}")
        return 0
    if args.command == "select-source":
        result = set_preferred_source(args.db, args.source)
        print(f"collection source {result['selectedSource']}")
        return 0
    process_pending_command(args.db, args.command_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
