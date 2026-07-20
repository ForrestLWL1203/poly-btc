"""Scanner lifecycle helpers: workset selection and discovery-state pruning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import math
import sqlite3
import time
from typing import Iterable, Sequence

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


def stable_refresh_shard(addr: str, shard_count: int = 7) -> int:
    shard_count = int(shard_count)
    if shard_count <= 0:
        raise ValueError("shard_count must be positive")
    digest = hashlib.sha256(str(addr).strip().lower().encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % shard_count


def refresh_shard_for_day(day: date | str | None = None, shard_count: int = 7) -> int:
    if day is None:
        value = datetime.now(timezone.utc).date()
    elif isinstance(day, str):
        value = date.fromisoformat(day[:10])
    else:
        value = day
    return value.toordinal() % int(shard_count)


def subsequent_refresh_shard_batches(
    candidates: Iterable[str], used_addrs: Iterable[str], *, current_shard: int,
    shard_count: int = 7, max_shards: int = 1,
) -> list[dict]:
    """Return bounded later shard batches in candidate order, excluding work already scheduled."""
    shard_count = int(shard_count)
    if shard_count <= 0:
        raise ValueError("shard_count must be positive")
    used = {str(addr).strip().lower() for addr in used_addrs if str(addr).strip()}
    ordered = dedupe_preserve(
        str(addr).strip().lower() for addr in candidates if str(addr).strip()
    )
    batches = []
    for offset in range(1, min(max(0, int(max_shards)), shard_count - 1) + 1):
        shard = (int(current_shard) + offset) % shard_count
        batch = [
            addr for addr in ordered
            if addr not in used and stable_refresh_shard(addr, shard_count) == shard
        ]
        used.update(batch)
        if batch:
            batches.append({"shard": shard, "workset": batch})
    return batches


@dataclass(frozen=True)
class ScanTimeBudget:
    started_monotonic: float
    total_s: float
    finalize_reserve_s: float = 0.0

    @property
    def discovery_deadline(self) -> float:
        return self.started_monotonic + max(0.0, self.total_s - self.finalize_reserve_s)

    def remaining_discovery_s(self, now_monotonic: float | None = None) -> float:
        now_monotonic = time.monotonic() if now_monotonic is None else float(now_monotonic)
        return max(0.0, self.discovery_deadline - now_monotonic)

    def can_start_discovery(self, estimated_next_s: float = 0.0, now_monotonic: float | None = None) -> bool:
        return self.remaining_discovery_s(now_monotonic) >= max(0.0, float(estimated_next_s))


def _stable_exploration_order(addresses: Sequence[str], seed: str) -> list[str]:
    return sorted(
        addresses,
        key=lambda addr: (hashlib.sha256(f"{seed}:{addr.lower()}".encode()).digest(), addr),
    )


def _weighted_take(groups: Sequence[Sequence[str]], capacity: int, ratios: Sequence[float]) -> tuple[list[str], list[int]]:
    """Take 40/40/20-style quotas, then backfill unused quota without wasting capacity."""
    capacity = max(0, int(capacity))
    if capacity == 0:
        return [], [0 for _ in groups]
    total_ratio = sum(max(0.0, float(value)) for value in ratios)
    ratios = [max(0.0, float(value)) / total_ratio for value in ratios] if total_ratio else [0.0] * len(groups)
    raw = [capacity * value for value in ratios]
    quotas = [int(math.floor(value)) for value in raw]
    for index in sorted(range(len(groups)), key=lambda i: (raw[i] - quotas[i], -i), reverse=True):
        if sum(quotas) >= capacity:
            break
        quotas[index] += 1
    selected_by_group = [list(group[:quota]) for group, quota in zip(groups, quotas)]
    selected = dedupe_preserve(item for group in selected_by_group for item in group)
    if len(selected) < capacity:
        tails = [list(group[len(selected_by_group[index]):]) for index, group in enumerate(groups)]
        while len(selected) < capacity and any(tails):
            progressed = False
            for tail in tails:
                if tail and len(selected) < capacity:
                    item = tail.pop(0)
                    if item not in selected:
                        selected.append(item)
                    progressed = True
            if not progressed:
                break
    counts = [sum(1 for item in selected if item in set(group)) for group in groups]
    return selected, counts


def schedule_profile_workset(
    candidates: Iterable[str],
    *,
    qualified_addrs: Iterable[str] = (),
    core_addrs: Iterable[str] = (),
    challenger_addrs: Iterable[str] = (),
    warmup_backfill_addrs: Iterable[str] = (),
    off_list_qualified_addrs: Iterable[str] = (),
    position_addrs: Iterable[str] = (),
    profiled_addrs: Iterable[str] = (),
    near_threshold_addrs: Iterable[str] = (),
    recovery_addrs: Iterable[str] = (),
    exploration_addrs: Iterable[str] | None = None,
    full_refetch_addrs: Iterable[str] = (),
    limit: int = 300,
    budget: ScanTimeBudget | None = None,
    estimated_profile_s: float = 0.0,
    now_monotonic: float | None = None,
    shard_count: int = 7,
    refresh_shard: int | None = None,
    exploration_seed: str = "",
    full_scan: bool = False,
) -> dict:
    """Build an auditable generation workset.

    Position/Core/qualified/Challenger/off-list-qualified wallets are mandatory and never dropped for a
    discovery budget. One-time warm-up backfills are the first ordinary lane and therefore consume the
    configured daily candidate budget instead of masquerading as Challenger priority. The selected daily
    evaluation shard comes next; remaining capacity is divided new/recovery/exploration 40/40/20. Evaluation
    rotation never implies a source-history re-download: ``full_refetch_addrs`` explicitly identifies only
    new or incomplete caches.
    """
    candidates = dedupe_preserve(str(addr).strip().lower() for addr in candidates if str(addr).strip())
    candidate_set = set(candidates)
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
    priority_set = set(priority)
    profiled_set = {str(addr).strip().lower() for addr in profiled_addrs}
    refresh_shard = refresh_shard_for_day(shard_count=shard_count) if refresh_shard is None else int(refresh_shard)
    if not 0 <= refresh_shard < int(shard_count):
        raise ValueError("refresh_shard outside shard range")

    configured_limit = max(0, int(limit or 0))
    discovery_capacity = max(0, configured_limit - len(priority))
    time_capacity = None
    if budget is not None and estimated_profile_s > 0:
        remaining = budget.remaining_discovery_s(now_monotonic)
        time_capacity = max(0, int(remaining // float(estimated_profile_s)))
        discovery_capacity = min(discovery_capacity, time_capacity)

    eligible_tail = [addr for addr in candidates if addr not in priority_set]
    if full_scan:
        discovery = eligible_tail[:discovery_capacity]
        rotation = discovery
        warmup_backfill = [
            addr for addr in dedupe_preserve(
                str(item).strip().lower() for item in warmup_backfill_addrs if str(item).strip()
            ) if addr in set(discovery)
        ]
        weighted = []
        weighted_counts = [0, 0, 0]
    else:
        warmup_due = [
            addr for addr in dedupe_preserve(
                str(item).strip().lower() for item in warmup_backfill_addrs if str(item).strip()
            ) if addr in candidate_set and addr not in priority_set
        ]
        warmup_backfill = warmup_due[:discovery_capacity]
        capacity_after_warmup = max(0, discovery_capacity - len(warmup_backfill))
        due_rotation = [
            addr for addr in eligible_tail
            if addr not in set(warmup_backfill)
            and stable_refresh_shard(addr, shard_count) == refresh_shard
        ]
        rotation = due_rotation[:capacity_after_warmup]
        capacity_after_rotation = max(0, capacity_after_warmup - len(rotation))
        used = priority_set | set(warmup_backfill) | set(rotation)
        new = [addr for addr in candidates if addr not in used and addr not in profiled_set]
        recovery_source = dedupe_preserve(
            str(addr).strip().lower()
            for addr in list(near_threshold_addrs) + list(recovery_addrs)
            if str(addr).strip().lower() in candidate_set
        )
        recovery = [addr for addr in recovery_source if addr not in used and addr not in set(new)]
        used_for_explore = used | set(new) | set(recovery)
        if exploration_addrs is None:
            exploration_pool = [addr for addr in candidates if addr not in used_for_explore]
        else:
            exploration_pool = dedupe_preserve(
                str(addr).strip().lower() for addr in exploration_addrs
                if str(addr).strip().lower() in candidate_set and str(addr).strip().lower() not in used_for_explore
            )
        exploration = _stable_exploration_order(exploration_pool, exploration_seed)
        weighted, weighted_counts = _weighted_take(
            (new, recovery, exploration), capacity_after_rotation, (0.40, 0.40, 0.20)
        )
        discovery = dedupe_preserve(warmup_backfill + rotation + weighted)

    workset = dedupe_preserve(priority + discovery)
    workset_set = set(workset)
    # The profile/reporting window is 30 days, but replay needs seven earlier warm-up days so positions opened
    # before the reporting boundary and closed inside it are reconstructed correctly. The 37-day source fetch
    # is a one-time cache bootstrap/repair, while every later evaluation remains cursor-based and incremental.
    # Candidate rotation and source-history repair are separate decisions. A wallet with a confirmed
    # PROFILE_FETCH_DAYS coverage marker stays cursor/delta-only forever; re-evaluating its daily shard must
    # not download the same 37-day history again. Callers explicitly nominate only new/incomplete caches.
    full_refetch_set = {
        str(addr).strip().lower() for addr in full_refetch_addrs if str(addr).strip()
    }
    full_refetch = [addr for addr in workset if addr in full_refetch_set]
    delta = [addr for addr in workset if addr not in set(full_refetch)]
    fill_mode = (
        "full_refetch" if full_refetch and not delta
        else ("mixed" if full_refetch else "delta")
    )
    all_eligible = set(candidates) | priority_set
    return {
        "workset": workset,
        "mode": "all" if full_scan else "priority+rotation+discovery",
        "workset_mode": "all" if full_scan else "priority",
        "fill_mode": fill_mode,
        "full_scan": bool(full_scan),
        "counts": {
            "priority": len(priority),
            "position": sum(1 for addr in priority_lanes[0] if addr in workset_set),
            "core": sum(1 for addr in priority_lanes[1] if addr in workset_set),
            "qualified": sum(1 for addr in priority_lanes[2] if addr in workset_set),
            "challenger": sum(1 for addr in priority_lanes[3] if addr in workset_set),
            "off_list_qualified": sum(1 for addr in priority_lanes[4] if addr in workset_set),
            "warmup_backfill": len(warmup_backfill),
            "rotation": len(rotation),
            "new": weighted_counts[0],
            "recovery": weighted_counts[1],
            "exploration": weighted_counts[2],
            "workset": len(workset),
            "deferred": max(0, len(all_eligible - workset_set)),
        },
        "limit": configured_limit,
        "time_capacity": time_capacity,
        "refresh": {
            "shard_count": int(shard_count),
            "shard_index": refresh_shard,
            "full_refetch": full_refetch,
            "delta": delta,
            "deferred_in_shard": max(0, len([
                addr for addr in candidates
                if stable_refresh_shard(addr, shard_count) == refresh_shard and addr not in workset_set
            ])),
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
        "AND NOT EXISTS (SELECT 1 FROM leaderboard l WHERE l.addr=p.addr AND l.is_candidate=1)"
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
        " UNION SELECT addr FROM profile WHERE status='active')"
    )
    n_fills = db.total_changes - before_fills
    before_cache_state = db.total_changes
    db.execute(
        "DELETE FROM fill_cache_state WHERE addr NOT IN "
        "(SELECT addr FROM leaderboard WHERE is_candidate=1 "
        " UNION SELECT addr FROM profile WHERE status='active')"
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


def profile_workset_breakdown(candidates, active_addrs, profiled, full_scan, limit, daily_recheck_top=None):
    """Choose wallets to profile this scan and return an auditable breakdown.

    Full scans sweep every current candidate plus off-list actives. Daily incremental scans still keep the
    cheap active/new path, but also re-check the current leaderboard's top old candidates so recovered wallets
    can replenish the watchlist before the weekly full resync.
    """
    candidates = list(candidates or [])
    active_addrs = list(active_addrs or [])
    limit = int(limit or 0)
    limit = limit if limit > 0 else len(candidates) + len(active_addrs)
    active_set = set(active_addrs)
    profiled_set = set(profiled or [])
    candidate_set = set(candidates)
    off_active = [a for a in active_addrs if a not in candidate_set]
    all_eligible = dedupe_preserve(candidates + off_active)
    if full_scan:
        workset = all_eligible[:limit]
        workset_set = set(workset)
        counts = {
            "candidate": len(candidates),
            "profiled_before": len(profiled_set),
            "active_total": len(active_addrs),
            "active_candidate": len([a for a in candidates if a in active_set and a in workset_set]),
            "new_candidate": len([a for a in candidates if a not in profiled_set and a in workset_set]),
            "top_recheck": 0,
            "off_list_active": len([a for a in off_active if a in workset_set]),
            "workset": len(workset),
            "deferred_tail": max(0, len(all_eligible) - len(workset)),
        }
        return {
            "workset": workset,
            "mode": "FULL (30d re-fetch, all candidates)",
            "counts": counts,
            "full_scan": True,
            "limit": limit,
            "daily_recheck_top": 0,
        }

    active_candidates = [a for a in candidates if a in active_set]
    new_candidates = [a for a in candidates if a not in profiled_set]
    active_new = dedupe_preserve(active_candidates + new_candidates)
    daily_recheck_top = (
        config.DAILY_RECHECK_TOP_N if daily_recheck_top is None else int(daily_recheck_top or 0)
    )
    already = set(active_new)
    top_recheck = []
    if daily_recheck_top > 0:
        for addr in candidates[:daily_recheck_top]:
            if addr in already:
                continue
            top_recheck.append(addr)
            already.add(addr)
    workset = dedupe_preserve(active_new + top_recheck + off_active)[:limit]
    workset_set = set(workset)
    covered = len(set(active_new) | set(top_recheck))
    deferred = max(0, len(candidates) - covered)
    mode = (
        f"INCREMENTAL daily-tier ({len(active_candidates)} active + {len(new_candidates)} new "
        f"+ {len(top_recheck)} top-recheck "
        f"of {len(candidates)} cand; {deferred} deferred-tail -> weekly full)"
    )
    counts = {
        "candidate": len(candidates),
        "profiled_before": len(profiled_set),
        "active_total": len(active_addrs),
        "active_candidate": len([a for a in active_candidates if a in workset_set]),
        "new_candidate": len([a for a in new_candidates if a in workset_set]),
        "top_recheck": len([a for a in top_recheck if a in workset_set]),
        "off_list_active": len([a for a in off_active if a in workset_set]),
        "workset": len(workset),
        "deferred_tail": deferred,
    }
    return {
        "workset": workset,
        "mode": mode,
        "counts": counts,
        "full_scan": False,
        "limit": limit,
        "daily_recheck_top": daily_recheck_top,
    }


def profile_workset(candidates, active_addrs, profiled, full_scan, limit, daily_recheck_top=None):
    """Choose wallets to profile this scan."""
    breakdown = profile_workset_breakdown(
        candidates,
        active_addrs,
        profiled,
        full_scan,
        limit,
        daily_recheck_top=daily_recheck_top,
    )
    return breakdown["workset"], breakdown["mode"]
