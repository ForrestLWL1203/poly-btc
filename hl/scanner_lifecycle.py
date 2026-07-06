"""Scanner lifecycle helpers: workset selection and discovery-state pruning."""

from __future__ import annotations

from . import config


def dedupe_preserve(items):
    out = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def prune_discovery_cache(db):
    """Bound discovery state after a scan.

    Keep current candidates for incremental rechecks, and keep active profiles even if they fell off the
    leaderboard candidate set. Drop disappeared non-active profiles and their derived/cache rows.
    """
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
    before_fills = db.total_changes
    db.execute(
        "DELETE FROM candidate_fills WHERE addr NOT IN "
        "(SELECT addr FROM leaderboard WHERE is_candidate=1 "
        " UNION SELECT addr FROM profile WHERE status='active')"
    )
    n_fills = db.total_changes - before_fills
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
        "fills": int(n_fills or 0),
        "profiles": int(n_profiles or 0),
        "leaderboard": int(n_leaderboard or 0),
    }


def profile_workset(candidates, active_addrs, profiled, full_scan, limit, daily_recheck_top=None):
    """Choose wallets to profile this scan.

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
        return all_eligible[:limit], "FULL (30d re-fetch, all candidates)"

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
    covered = len(set(active_new) | set(top_recheck))
    deferred = max(0, len(candidates) - covered)
    mode = (
        f"INCREMENTAL daily-tier ({len(active_candidates)} active + {len(new_candidates)} new "
        f"+ {len(top_recheck)} top-recheck "
        f"of {len(candidates)} cand; {deferred} deferred-tail -> weekly full)"
    )
    return workset, mode
