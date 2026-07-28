"""Explicit Paper cold-reset maintenance operation."""
from __future__ import annotations

from hyper import config, params
from hyper.util import now_iso


PRESERVED_TABLES = frozenset({"params", "provider_credential"})
DISCOVERY_CACHE_TABLES = frozenset({
    "candidate_fills",
    "fill_cache_state",
    "coin_price_candle",
    "coin_price_path_state",
    # Catastrophic-risk vetoes are source evidence, not Paper trading history.  They intentionally survive
    # rolling cache expiry and must also survive an execution/selection cold reset.
    "wallet_risk_event",
    "wallet_risk_state",
})


def reset(
    db, *, factory_params: bool = False, preserve_discovery_cache: bool = False,
) -> dict:
    """Clear Paper/selection state while retaining operator settings and optional immutable source caches."""
    tables = [
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    preserved = set(PRESERVED_TABLES)
    if preserve_discovery_cache:
        preserved.update(DISCOVERY_CACHE_TABLES)
    cleared = [name for name in tables if name not in preserved]
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
                "DELETE FROM sqlite_sequence WHERE name NOT IN (%s)" % ",".join("?" for _ in preserved),
                tuple(sorted(preserved)),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "status": "reset", "clearedTables": len(cleared),
        "params": "factory" if factory_params else "preserved",
        "discoveryCache": "preserved" if preserve_discovery_cache else "cleared",
        "initialBalance": float(config.INITIAL_BALANCE),
    }
