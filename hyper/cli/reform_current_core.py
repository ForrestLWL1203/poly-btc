#!/usr/bin/env python3
"""Re-form the current Core on the active parameter surface and refresh its UI replay."""
from __future__ import annotations

import argparse
import json

from hyper import storage
from hyper.discovery import scanner
from hyper.ops import scan_lock
from hyper.selection import effective_replay


def main() -> int:
    parser = argparse.ArgumentParser(
        description="re-form current Core without tuning parameters",
    )
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    with scan_lock.acquire(args.db):
        db = storage.connect(args.db)
        try:
            with scanner._ScannerHeartbeat(db):
                result = scanner.reform_published_generation_current_surface(db)
                replay = effective_replay.certify_and_store(db)
        finally:
            db.close()
    print(json.dumps({
        "status": result.get("status"),
        "generation": result.get("generation"),
        "core": (result.get("selection") or {}).get("core"),
        "challenger": (result.get("selection") or {}).get("challenger"),
        "fixedCurrentSurface": (result.get("selection") or {}).get(
            "fixedCurrentSurface"
        ),
        "portfolioReplay": {
            "status": replay.get("status"),
            "coreCount": replay.get("coreCount"),
            "netPnl30": replay.get("netPnl30"),
            "dynamicReturn30d": replay.get("dynamicReturn30d"),
            "dynamicReturn7d": replay.get("dynamicReturn7d"),
            "maxDrawdown30": replay.get("maxDrawdown30"),
            "liquidations30": replay.get("liquidations30"),
            "openRate30": replay.get("openRate30"),
            "capacityFit30": replay.get("capacityFit30"),
        },
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
