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


_LEGACY_PARENTLESS_PUBLICATION_SOURCES = {
    "scanner",
    "resume_finalize",
    "challenger_daily",
}


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
        "SELECT lower(fs.addr),COALESCE(fs.entry_eligible,1),"
        "COALESCE(fs.retention_status,'healthy'),fs.retention_failure_reason,"
        "COALESCE(fs.retention_failure_streak,0) FROM follow_selection fs "
        "LEFT JOIN target_controls tc ON tc.addr=fs.addr WHERE fs.generation=? "
        "AND lower(fs.role)='core' AND COALESCE(fs.enabled,1)=1 "
        "ORDER BY COALESCE(tc.pinned,0) DESC,"
        "CASE WHEN COALESCE(tc.pinned,0)=1 THEN tc.pinned_at END,"
        "COALESCE(fs.selection_rank,999999),COALESCE(fs.utility,-1e999) DESC,"
        "lower(fs.addr),fs.addr",
        (generation,),
    ).fetchall()
    addrs = []
    execution_policy = {}
    seen = set()
    for row in rows:
        addr = (row[0] or "").strip().lower()
        if addr and addr not in seen:
            addrs.append(addr)
            execution_policy[addr] = {
                "entryEligible": bool(row[1]),
                "retentionStatus": row[2] or "healthy",
                "retentionFailureReason": row[3],
                "retentionFailureStreak": int(row[4] or 0),
            }
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
            **execution_policy.get(addr, {}),
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
    legacy_missing_policy = [
        row["addr"].lower() for row in targets if "entryEligible" not in row
    ]
    if legacy_missing_policy:
        marks = ",".join("?" for _ in legacy_missing_policy)
        policy = {
            (row[0] or "").lower(): {
                "entryEligible": bool(row[1]),
                "retentionStatus": row[2] or "healthy",
                "retentionFailureReason": row[3],
                "retentionFailureStreak": int(row[4] or 0),
            }
            for row in db.execute(
                f"SELECT addr,COALESCE(entry_eligible,1),"
                f"COALESCE(retention_status,'healthy'),retention_failure_reason,"
                f"COALESCE(retention_failure_streak,0) FROM follow_selection "
                f"WHERE generation=? AND lower(addr) IN ({marks})",
                (bundle.get("selectionGeneration"), *legacy_missing_policy),
            ).fetchall()
        }
        # Rolling-deploy bridge only. New revisions always freeze these fields in targets_json.
        targets = [{**row, **policy.get(row["addr"].lower(), {})} for row in targets]
    if targets:
        marks = ",".join("?" for _ in targets)
        control_cols = {
            row[1] for row in db.execute("PRAGMA table_info(target_controls)").fetchall()
        }
        intent_clause = (
            " OR COALESCE(intent,'active')!='active'"
            if "intent" in control_cols else ""
        )
        disabled = {
            (row[0] or "").lower()
            for row in db.execute(
                f"SELECT addr FROM target_controls WHERE "
                f"(COALESCE(enabled,1)=0{intent_clause}) AND lower(addr) IN ({marks})",
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
    allow_lineage_repair: bool = False,
) -> dict:
    """Create and optionally activate a revision without committing the caller's transaction."""
    previous = active_revision_id(db)
    if expected_active_revision is not None and previous != expected_active_revision:
        raise RuntimeError("strategy_revision_changed")
    if activate and previous:
        if parent_revision is None and not allow_lineage_repair:
            parent_revision = previous
        elif parent_revision != previous and not allow_lineage_repair:
            raise RuntimeError("strategy_revision_parent_not_active")
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


def repair_parentless_active_revision(
    db,
    *,
    live_parent_revision: str,
    enqueue_reload: bool = False,
) -> Optional[dict]:
    """Bridge a legacy parentless publication onto the currently bound Live lineage.

    Older complete/daily publishers activated a new immutable bundle without linking it to the previous
    active revision.  A running Live session correctly rejected that lateral history.  Only those known
    publication sources are repairable here; arbitrary lateral revisions continue to fail closed.
    """
    active = load_active(db)
    if not active or active.get("parentRevision") is not None:
        return None
    if active.get("source") not in _LEGACY_PARENTLESS_PUBLICATION_SOURCES:
        return None
    parent = load_revision(db, live_parent_revision)
    if not parent or live_parent_revision == active.get("revision"):
        return None
    validation = dict(active.get("validation") or {})
    validation["lineageRepair"] = {
        "replacedActiveRevision": active["revision"],
        "liveParentRevision": live_parent_revision,
        "reason": "legacy_parentless_publication",
    }
    return create_revision(
        db,
        active["selectionGeneration"],
        source="strategy_lineage_repair",
        follow_values=active.get("params") or {},
        targets=active.get("targets") or [],
        parent_revision=live_parent_revision,
        validation=validation,
        reason="legacy_parentless_publication",
        expected_active_revision=active["revision"],
        enqueue_reload=enqueue_reload,
        allow_lineage_repair=True,
    )


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
