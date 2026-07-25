"""Scanner lifecycle helpers: workset selection and discovery-state pruning."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import time
from typing import Iterable

from hyper import config


WALLET_STATES = {
    "qualified", "challenger", "core", "cooldown", "exit_only", "rejected", "quarantine",
}
QUALIFIED_STATES = {"qualified", "challenger", "core"}
BAD_STATES = {"cooldown", "rejected"}
ROLE_STATES = {"challenger", "core", "exit_only"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def dedupe_preserve(items):
    out = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def upsert_wallet_registry(
    db: sqlite3.Connection,
    addr: str,
    *,
    generation: str | None = None,
    seen_at: str | None = None,
    state: str | None = None,
    role: str | None = None,
    data_status: str = "valid",
    reason: str | None = None,
    cooldown_until: str | None = None,
    last_actionable_open_ms: int | None = None,
) -> dict:
    """Durably record one wallet lifecycle evaluation without committing.

    Lifecycle counters advance at most once per generation. A deferred data error increments its own
    counter but does not turn a previously qualified wallet into a rejection.
    """
    addr = str(addr or "").strip().lower()
    if not addr:
        raise ValueError("wallet address is required")
    if state is not None and state not in WALLET_STATES:
        raise ValueError(f"unsupported wallet state: {state}")
    if role is not None and role not in ROLE_STATES:
        raise ValueError(f"unsupported wallet role: {role}")
    seen_at = seen_at or _now_iso()
    cur = db.execute(
        "SELECT state,current_role,first_seen_at,last_seen_at,first_qualified_at,last_qualified_at,"
        "first_core_at,last_core_at,last_rejected_at,last_reject_reason,cooldown_until,data_error_count,"
        "consecutive_qualified,consecutive_bad,core_entries,core_exits,recovery_count,last_valid_generation,"
        "last_evaluated_generation,last_actionable_open_ms,updated_at FROM wallet_registry WHERE addr=?",
        (addr,),
    )
    old_row = cur.fetchone()
    old = dict(zip((column[0] for column in cur.description), old_row)) if old_row else None
    previous_state = old["state"] if old else None
    previous_role = old["current_role"] if old else None
    next_state = state or previous_state or "qualified"
    if role is None:
        next_role = next_state if next_state in ROLE_STATES else (previous_role if state is None else None)
    else:
        next_role = role

    same_generation = bool(generation and old and old["last_evaluated_generation"] == generation)
    valid_evaluation = data_status == "valid"
    qualified = next_state in QUALIFIED_STATES
    bad = next_state in BAD_STATES
    old_core = previous_state == "core" or previous_role == "core"
    next_core = next_state == "core" or next_role == "core"

    first_seen_at = old["first_seen_at"] if old else seen_at
    first_qualified_at = old["first_qualified_at"] if old else None
    last_qualified_at = old["last_qualified_at"] if old else None
    first_core_at = old["first_core_at"] if old else None
    last_core_at = old["last_core_at"] if old else None
    last_rejected_at = old["last_rejected_at"] if old else None
    last_reject_reason = old["last_reject_reason"] if old else None
    data_error_count = int(old["data_error_count"] if old else 0)
    consecutive_qualified = int(old["consecutive_qualified"] if old else 0)
    consecutive_bad = int(old["consecutive_bad"] if old else 0)
    core_entries = int(old["core_entries"] if old else 0)
    core_exits = int(old["core_exits"] if old else 0)
    recovery_count = int(old["recovery_count"] if old else 0)
    if data_status == "deferred_data_error" and not same_generation:
        data_error_count += 1
    if valid_evaluation and not same_generation:
        if qualified:
            consecutive_qualified += 1
            consecutive_bad = 0
            first_qualified_at = first_qualified_at or seen_at
            last_qualified_at = seen_at
        elif bad:
            consecutive_bad += 1
            consecutive_qualified = 0
        if next_state == "rejected":
            last_rejected_at = seen_at
            last_reject_reason = reason
        if next_core and not old_core:
            if core_entries > 0:
                recovery_count += 1
            core_entries += 1
            first_core_at = first_core_at or seen_at
        if old_core and not next_core:
            core_exits += 1
        if next_core:
            last_core_at = seen_at

    next_last_open = old["last_actionable_open_ms"] if old else None
    if last_actionable_open_ms is not None:
        next_last_open = max(int(last_actionable_open_ms), int(next_last_open or 0))
    values = {
        "addr": addr,
        "state": next_state,
        "current_role": next_role,
        "first_seen_at": first_seen_at,
        "last_seen_at": seen_at,
        "first_qualified_at": first_qualified_at,
        "last_qualified_at": last_qualified_at,
        "first_core_at": first_core_at,
        "last_core_at": last_core_at,
        "last_rejected_at": last_rejected_at,
        "last_reject_reason": last_reject_reason,
        "cooldown_until": cooldown_until if cooldown_until is not None else (old["cooldown_until"] if old else None),
        "data_error_count": data_error_count,
        "consecutive_qualified": consecutive_qualified,
        "consecutive_bad": consecutive_bad,
        "core_entries": core_entries,
        "core_exits": core_exits,
        "recovery_count": recovery_count,
        "last_valid_generation": generation if valid_evaluation else (old["last_valid_generation"] if old else None),
        "last_evaluated_generation": generation or (old["last_evaluated_generation"] if old else None),
        "last_actionable_open_ms": next_last_open,
        "updated_at": seen_at,
    }
    columns = tuple(values)
    assignments = ",".join(f"{column}=excluded.{column}" for column in columns if column != "addr")
    db.execute(
        f"INSERT INTO wallet_registry ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        f"ON CONFLICT(addr) DO UPDATE SET {assignments}",
        tuple(values[column] for column in columns),
    )
    return values


def schedule_profile_workset(
    candidates: Iterable[str],
    *,
    qualified_addrs: Iterable[str] = (),
    core_addrs: Iterable[str] = (),
    challenger_addrs: Iterable[str] = (),
    warmup_backfill_addrs: Iterable[str] = (),
    off_list_qualified_addrs: Iterable[str] = (),
    position_addrs: Iterable[str] = (),
    full_refetch_addrs: Iterable[str] = (),
) -> dict:
    """Build the complete auditable workset; only cache completeness controls full versus delta fills."""
    candidates = dedupe_preserve(str(addr).strip().lower() for addr in candidates if str(addr).strip())
    priority_lanes = [
        dedupe_preserve(str(addr).strip().lower() for addr in position_addrs if str(addr).strip()),
        dedupe_preserve(str(addr).strip().lower() for addr in core_addrs if str(addr).strip()),
        dedupe_preserve(str(addr).strip().lower() for addr in qualified_addrs if str(addr).strip()),
        dedupe_preserve(str(addr).strip().lower() for addr in challenger_addrs if str(addr).strip()),
        dedupe_preserve(
            str(addr).strip().lower() for addr in off_list_qualified_addrs if str(addr).strip()
        ),
    ]
    priority = dedupe_preserve(item for lane in priority_lanes for item in lane)
    # Every generation has one deterministic lane: every strict candidate plus the role/open-position safety
    # set. There is no Top-N, rotation shard, recovery quota, exploration quota or deferred tail.
    workset = dedupe_preserve(priority + candidates)
    full_refetch_set = {
        str(addr).strip().lower() for addr in full_refetch_addrs if str(addr).strip()
    }
    full_refetch = [addr for addr in workset if addr in full_refetch_set]
    delta = [addr for addr in workset if addr not in full_refetch_set]
    workset_set = set(workset)
    return {
        "workset": workset,
        "mode": "all",
        "workset_mode": "all",
        "fill_mode": "full_refetch" if full_refetch and not delta else ("mixed" if full_refetch else "delta"),
        "full_scan": True,
        "counts": {
            "priority": len(priority),
            "position": sum(1 for addr in priority_lanes[0] if addr in workset_set),
            "core": sum(1 for addr in priority_lanes[1] if addr in workset_set),
            "qualified": sum(1 for addr in priority_lanes[2] if addr in workset_set),
            "challenger": sum(1 for addr in priority_lanes[3] if addr in workset_set),
            "off_list_qualified": sum(1 for addr in priority_lanes[4] if addr in workset_set),
            "warmup_backfill": sum(1 for addr in warmup_backfill_addrs if addr in workset_set),
            "rotation": 0, "new": 0, "recovery": 0, "exploration": 0,
            "workset": len(workset), "deferred": 0,
        },
        "limit": None,
        "time_capacity": None,
        "refresh": {
            "shard_count": 0, "shard_index": None,
            "full_refetch": full_refetch, "delta": delta, "deferred_in_shard": 0,
        },
    }


def prune_discovery_cache(db, *, attempts: int = 3, retry_sleep_s: float = 2.0):
    """Bound discovery state after a scan.

    Keep current candidates for incremental rechecks, and keep active profiles even if they fell off the
    leaderboard candidate set. Drop disappeared non-active profiles and their derived/cache rows.
    """
    last_exc = None
    for attempt in range(max(1, attempts)):
        try:
            return _prune_discovery_cache_once(db)
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if "locked" not in str(exc).lower() or attempt >= attempts - 1:
                raise
            try:
                db.rollback()
            except sqlite3.Error:
                pass
            time.sleep(retry_sleep_s * (attempt + 1))
    raise last_exc


def _prune_discovery_cache_once(db):
    db.execute("CREATE TEMP TABLE IF NOT EXISTS prune_discovery_addrs (addr TEXT PRIMARY KEY)")
    db.execute("DELETE FROM prune_discovery_addrs")
    db.execute(
        "INSERT OR IGNORE INTO prune_discovery_addrs(addr) "
        "SELECT p.addr FROM profile p "
        "WHERE COALESCE(p.status,'')!='active' "
        "AND NOT EXISTS (SELECT 1 FROM leaderboard l WHERE l.addr=p.addr AND l.is_candidate=1) "
        "AND NOT EXISTS (SELECT 1 FROM copy_position cp WHERE cp.addr=p.addr AND cp.status='open') "
        "AND NOT EXISTS (SELECT 1 FROM follow_selection fs JOIN scan_generation sg "
        "ON sg.generation=fs.generation WHERE fs.addr=p.addr AND sg.is_current=1 "
        "AND fs.role IN ('core','challenger','exit_only'))"
    )
    n_stale = db.execute("SELECT COUNT(*) FROM prune_discovery_addrs").fetchone()[0]
    before_episode = db.total_changes
    db.execute("DELETE FROM episode WHERE addr IN (SELECT addr FROM prune_discovery_addrs)")
    n_episode = db.total_changes - before_episode
    cutoff_ms = int((time.time() - config.PROFILE_FETCH_DAYS * 86_400) * 1000)
    before_fills = db.total_changes
    db.execute("DELETE FROM candidate_fills WHERE time<?", (cutoff_ms,))
    n_expired_fills = db.total_changes - before_fills
    before_fills = db.total_changes
    db.execute(
        "DELETE FROM candidate_fills WHERE addr NOT IN "
        "(SELECT addr FROM leaderboard WHERE is_candidate=1 "
        " UNION SELECT addr FROM profile WHERE status='active'"
        " UNION SELECT addr FROM copy_position WHERE status='open'"
        " UNION SELECT fs.addr FROM follow_selection fs JOIN scan_generation sg "
        " ON sg.generation=fs.generation WHERE sg.is_current=1 "
        " AND fs.role IN ('core','challenger','exit_only'))"
    )
    n_fills = db.total_changes - before_fills
    before_cache_state = db.total_changes
    db.execute(
        "DELETE FROM fill_cache_state WHERE addr NOT IN "
        "(SELECT addr FROM leaderboard WHERE is_candidate=1 "
        " UNION SELECT addr FROM profile WHERE status='active'"
        " UNION SELECT addr FROM copy_position WHERE status='open'"
        " UNION SELECT fs.addr FROM follow_selection fs JOIN scan_generation sg "
        " ON sg.generation=fs.generation WHERE sg.is_current=1 "
        " AND fs.role IN ('core','challenger','exit_only'))"
    )
    n_cache_state = db.total_changes - before_cache_state
    before_profiles = db.total_changes
    db.execute("DELETE FROM profile WHERE addr IN (SELECT addr FROM prune_discovery_addrs)")
    n_profiles = db.total_changes - before_profiles
    current_fetch = db.execute("SELECT MAX(fetched_at) FROM leaderboard").fetchone()[0]
    before_leaderboard = db.total_changes
    if current_fetch:
        db.execute(
            "DELETE FROM leaderboard WHERE COALESCE(fetched_at,'')<>? "
            "AND NOT EXISTS (SELECT 1 FROM profile p WHERE p.addr=leaderboard.addr AND p.status='active')",
            (current_fetch,),
        )
    n_leaderboard = db.total_changes - before_leaderboard
    db.execute("DELETE FROM prune_discovery_addrs")
    db.commit()
    return {
        "stale_profiles": int(n_stale or 0),
        "episodes": int(n_episode or 0),
        "expired_fills": int(n_expired_fills or 0),
        "fills": int(n_fills or 0),
        "cache_state": int(n_cache_state or 0),
        "profiles": int(n_profiles or 0),
        "leaderboard": int(n_leaderboard or 0),
    }
