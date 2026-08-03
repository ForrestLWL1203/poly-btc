"""Execution-mode table routing shared by Observer, Scanner and tuning.

The collection/selection pipeline is shared across Paper and Live, but any
forward-result or held-position input must come from the currently selected
execution ledger.  Keeping that routing in one tiny module prevents another
hard-coded ``copy_position`` leak into Live decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True)
class ExecutionBookTables:
    mode: str
    position: str
    action: str
    account: str


PAPER_BOOK = ExecutionBookTables("paper", "copy_position", "copy_action", "copy_account")
LIVE_BOOK = ExecutionBookTables(
    "live", "live_copy_position", "live_copy_action", "live_copy_account",
)


def selected_mode(db) -> str:
    try:
        row = db.execute("SELECT selected_mode FROM execution_control WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        return "paper"
    return "live" if row and str(row[0] or "").lower() == "live" else "paper"


def selected_book(db) -> ExecutionBookTables:
    return LIVE_BOOK if selected_mode(db) == "live" else PAPER_BOOK
