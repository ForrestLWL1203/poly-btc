"""One-shot execution control worker entry point."""

from __future__ import annotations

import argparse

from hyper import config
from hyper.execution.command_worker import process_all_pending, process_pending_command
from hyper.execution.credentials import generate_wrap_keypair


def main() -> int:
    parser = argparse.ArgumentParser(description="Hyperliquid execution control worker")
    parser.add_argument("--db", default=config.DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    process = sub.add_parser("process")
    process.add_argument("--command-id", required=True, type=int)
    sub.add_parser("process-pending")
    keys = sub.add_parser("generate-wrap-key")
    keys.add_argument("--private", required=True)
    keys.add_argument("--public", required=True)
    args = parser.parse_args()

    if args.command == "generate-wrap-key":
        result = generate_wrap_keypair(args.private, args.public)
        print(f"credential wrap key ready: {result['wrapKeyId']}")
        return 0
    if args.command == "process-pending":
        process_all_pending(args.db)
        return 0
    process_pending_command(args.db, args.command_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
