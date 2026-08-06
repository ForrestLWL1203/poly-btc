#!/usr/bin/env python3
"""Full-tune the exact published Core and refresh its certified UI replay."""
from __future__ import annotations

import argparse
import json

from hyper import storage
from hyper.discovery import scanner
from hyper.ops import scan_lock


def main() -> int:
    parser = argparse.ArgumentParser(
        description="full-tune the exact current Core without changing membership",
    )
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--apply", action="store_true",
        help="atomically publish the certified parameter surface and refresh UI replay",
    )
    args = parser.parse_args()
    with scan_lock.acquire(args.db):
        db = storage.connect(args.db)
        try:
            with scanner._ScannerHeartbeat(db):
                result = scanner.optimize_current_core_surface(
                    db, apply=bool(args.apply),
                )
        finally:
            db.close()
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
