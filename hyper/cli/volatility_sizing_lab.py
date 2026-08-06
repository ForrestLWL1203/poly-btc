#!/usr/bin/env python3
"""Run the read-only BTC-anchored volatility sizing lab."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from hyper.selection.volatility_sizing_lab import run_lab


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only current-Core BTC volatility sizing comparison",
    )
    parser.add_argument("--db", required=True, help="SQLite clone/snapshot; opened mode=ro")
    parser.add_argument("--initial-balance", type=float)
    parser.add_argument("--output", help="optional JSON report path")
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()

    db = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")

    def progress(name: str, validation: str) -> None:
        if args.progress:
            print(f"volatility_lab {validation} {name}", file=sys.stderr, flush=True)

    try:
        report = run_lab(
            db,
            initial_balance=args.initial_balance,
            progress=progress,
        )
    finally:
        db.close()
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
        print(json.dumps({
            "status": report["status"],
            "readOnly": report["readOnly"],
            "report": str(output),
            "recommendation": report["recommendation"],
        }, ensure_ascii=False, sort_keys=True))
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
