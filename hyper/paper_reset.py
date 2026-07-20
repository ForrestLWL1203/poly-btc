"""Explicit Paper cold-reset maintenance operation."""
from __future__ import annotations

from . import config, params
from .util import now_iso


PRESERVED_TABLES = frozenset({"params", "provider_credential"})


def reset(db, *, factory_params: bool = False) -> dict:
    """Clear business/Paper state while retaining operator settings and encrypted credentials."""
    tables = [
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    cleared = [name for name in tables if name not in PRESERVED_TABLES]
    db.execute("BEGIN IMMEDIATE")
    try:
        for table in cleared:
            # Names originate exclusively from sqlite_master, not user input.
            db.execute(f'DELETE FROM "{table}"')
        if factory_params:
            params.reset_defaults(db, commit=False)
        db.execute(
            "INSERT INTO copy_account (id,initial_balance,balance,updated_at) VALUES (1,?,?,?)",
            (float(config.INITIAL_BALANCE), float(config.INITIAL_BALANCE), now_iso()),
        )
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone():
            db.execute(
                "DELETE FROM sqlite_sequence WHERE name NOT IN (%s)" % ",".join("?" for _ in PRESERVED_TABLES),
                tuple(sorted(PRESERVED_TABLES)),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "status": "reset", "clearedTables": len(cleared),
        "params": "factory" if factory_params else "preserved",
        "initialBalance": float(config.INITIAL_BALANCE),
    }
