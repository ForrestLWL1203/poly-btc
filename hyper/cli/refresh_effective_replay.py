#!/usr/bin/env python3
"""Refresh the Dashboard replay estimate from the active immutable revision."""
from __future__ import annotations

import argparse
import json

from hyper import storage
from hyper.ops import scan_lock
from hyper.selection import effective_replay


def main() -> int:
    parser = argparse.ArgumentParser(description="refresh active Core strict replay estimate")
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    with scan_lock.acquire(args.db):
        db = storage.connect(args.db)
        try:
            summary = effective_replay.certify_and_store(db)
        finally:
            db.close()
    print(json.dumps({
        "status": summary.get("status"),
        "coreCount": summary.get("coreCount"),
        "dynamicReturn30d": summary.get("dynamicReturn30d"),
        "dynamicReturn7d": summary.get("dynamicReturn7d"),
        "validationSource": summary.get("validationSource"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
