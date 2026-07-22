"""Immutable, atomically activated Observer strategy bundles.

The mutable ``params`` table remains the operator/tuner control surface.  Observer executes only the
active revision once one exists; legacy databases without a revision temporarily fall back to the old
published-selection + params contract until a writer materialises the first bundle.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Optional

from hyper import config, params
from hyper.copy.sector import parse_json_obj
from hyper.copy.copy_policy import COPY_POLICY_PARAM_KEYS, load_copy_policy
from hyper.util import now_iso
from . import state as selection


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=float)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def active_revision_id(db) -> Optional[str]:
    row = db.execute("SELECT revision FROM active_strategy_revision WHERE id=1").fetchone()
    return str(row[0]) if row else None


def params_snapshot(db, values: Optional[dict] = None) -> tuple[dict, str]:
    snapshot = dict(values if values is not None else params.load_follow(db))
    scanner_values = params.load_category(db, "scanner")
    snapshot.update({
        key: scanner_values[key] if key in scanner_values else getattr(config, key)
        for key in COPY_POLICY_PARAM_KEYS
        if key in scanner_values or hasattr(config, key)
    })
    snapshot["COPY_POLICY_VERSION"] = load_copy_policy(snapshot).version
    return snapshot, _hash(snapshot)


def target_snapshot(db, generation: str) -> list[dict]:
    """Capture immutable Core execution context for one explicit selection generation."""
    rows = db.execute(
        "SELECT lower(fs.addr) FROM follow_selection fs "
        "LEFT JOIN target_controls tc ON tc.addr=fs.addr WHERE fs.generation=? "
        "AND lower(fs.role)='core' AND COALESCE(fs.enabled,1)=1 "
        "ORDER BY COALESCE(tc.pinned,0) DESC,"
        "CASE WHEN COALESCE(tc.pinned,0)=1 THEN tc.pinned_at END,"
        "COALESCE(fs.selection_rank,999999),COALESCE(fs.utility,-1e999) DESC,"
        "lower(fs.addr),fs.addr",
        (generation,),
    ).fetchall()
    addrs = []
    seen = set()
    for row in rows:
        addr = (row[0] or "").strip().lower()
        if addr and addr not in seen:
            addrs.append(addr)
            seen.add(addr)
    if not addrs:
        return []

    marks = ",".join("?" for _ in addrs)
    wallet = {
        (row[0] or "").lower(): {
            "acctValue": row[1],
            "sectorPolicy": parse_json_obj(row[2]),
        }
        for row in db.execute(
            f"SELECT addr,acct_value,sector_policy_json FROM follow_selection "
            f"WHERE generation=? AND lower(addr) IN ({marks})",
            (generation, *addrs),
        ).fetchall()
    }
    seed = {addr: [] for addr in addrs}
    for addr, coin in db.execute(
        f"SELECT lower(addr),coin FROM episode WHERE lower(addr) IN ({marks}) "
        "GROUP BY lower(addr),coin ORDER BY lower(addr),coin",
        tuple(addrs),
    ).fetchall():
        if addr in seed and coin:
            seed[addr].append(coin)
    missing = [addr for addr in addrs if not (wallet.get(addr, {}).get("sectorPolicy") or {}).get("allowed")]
    if missing:
        raise RuntimeError(f"strategy_target_policy_missing:{len(missing)}")
    return [
        {
            "addr": addr,
            "acctValue": wallet.get(addr, {}).get("acctValue"),
            "sectorPolicy": wallet.get(addr, {}).get("sectorPolicy") or {},
            "seedCoins": seed.get(addr) or [],
        }
        for addr in addrs
    ]


def load_revision(db, revision: str) -> Optional[dict]:
    row = db.execute(
        "SELECT revision,selection_generation,parent_revision,source,status,params_json,params_hash,"
        "targets_json,validation_json,reason,created_at,activated_at,superseded_at "
        "FROM strategy_revision WHERE revision=?",
        (revision,),
    ).fetchone()
    if not row:
        return None
    return {
        "revision": row[0],
        "selectionGeneration": row[1],
        "parentRevision": row[2],
        "source": row[3],
        "status": row[4],
        "params": json.loads(row[5] or "{}"),
        "paramsHash": row[6],
        "targets": json.loads(row[7] or "[]"),
        "validation": json.loads(row[8] or "{}"),
        "reason": row[9],
        "createdAt": row[10],
        "activatedAt": row[11],
        "supersededAt": row[12],
    }


def load_active(db) -> Optional[dict]:
    revision = active_revision_id(db)
    return load_revision(db, revision) if revision else None


def resolved_targets(db, bundle: dict, limit: Optional[int] = None) -> list[dict]:
    """Apply the live operator disable overlay without mutating the immutable target snapshot."""
    targets = [dict(row) for row in (bundle.get("targets") or []) if row.get("addr")]
    if targets:
        marks = ",".join("?" for _ in targets)
        disabled = {
            (row[0] or "").lower()
            for row in db.execute(
                f"SELECT addr FROM target_controls WHERE enabled=0 AND lower(addr) IN ({marks})",
                tuple(row["addr"] for row in targets),
            ).fetchall()
        }
        targets = [row for row in targets if row["addr"].lower() not in disabled]
    if limit is not None:
        targets = targets[:max(0, int(limit))]
    return targets


def create_revision(
    db,
    generation: str,
    *,
    source: str,
    follow_values: Optional[dict] = None,
    targets: Optional[list[dict]] = None,
    parent_revision: Optional[str] = None,
    validation: Optional[dict] = None,
    reason: Optional[str] = None,
    expected_active_revision: Optional[str] = None,
    activate: bool = True,
    enqueue_reload: bool = True,
    stamp: Optional[str] = None,
) -> dict:
    """Create and optionally activate a revision without committing the caller's transaction."""
    if expected_active_revision is not None and active_revision_id(db) != expected_active_revision:
        raise RuntimeError("strategy_revision_changed")
    current_generation = selection.latest_published_generation(db)
    if current_generation != generation:
        raise RuntimeError(
            f"strategy_generation_not_current:{generation}:{current_generation or 'none'}"
        )
    stamp = stamp or now_iso()
    snapshot, snapshot_hash = params_snapshot(db, follow_values)
    target_rows = target_snapshot(db, generation) if targets is None else list(targets)
    revision = f"strategy-{stamp.replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO strategy_revision "
        "(revision,selection_generation,parent_revision,source,status,params_json,params_hash,targets_json,"
        "validation_json,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            revision, generation, parent_revision, source, "staged", _json(snapshot), snapshot_hash,
            _json(target_rows), _json(validation or {}), reason, stamp,
        ),
    )
    if activate:
        previous = active_revision_id(db)
        if previous and previous != revision:
            db.execute(
                "UPDATE strategy_revision SET status='superseded',superseded_at=? WHERE revision=?",
                (stamp, previous),
            )
        db.execute(
            "UPDATE strategy_revision SET status='active',activated_at=?,superseded_at=NULL WHERE revision=?",
            (stamp, revision),
        )
        db.execute(
            "INSERT INTO active_strategy_revision (id,revision,updated_at) VALUES (1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,updated_at=excluded.updated_at",
            (revision, stamp),
        )
        if enqueue_reload:
            db.execute(
                "INSERT INTO commands (type,payload_json,owner,status,created_at) "
                "VALUES ('reload_params',?,?,'pending',?)",
                (_json({"by": "strategy_revision", "revision": revision, "source": source}), source, stamp),
            )
    return {
        "revision": revision,
        "selectionGeneration": generation,
        "parentRevision": parent_revision,
        "source": source,
        "paramsHash": snapshot_hash,
        "targetCount": len(target_rows),
    }


def materialize_current(
    db,
    *,
    source: str,
    reason: Optional[str] = None,
    enqueue_reload: bool = False,
) -> Optional[dict]:
    """Create a revision for the current generation and mutable params (rolling-deploy/manual bridge)."""
    generation = selection.latest_published_generation(db)
    if not generation:
        return None
    parent = active_revision_id(db)
    active = load_revision(db, parent) if parent else None
    targets = (
        active.get("targets")
        if active and active.get("selectionGeneration") == generation
        else target_snapshot(db, generation)
    )
    return create_revision(
        db,
        generation,
        source=source,
        targets=targets,
        parent_revision=parent,
        reason=reason,
        expected_active_revision=parent,
        enqueue_reload=enqueue_reload,
    )
