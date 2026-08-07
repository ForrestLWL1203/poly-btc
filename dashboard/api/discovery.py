"""Discovery and scanner-status dashboard endpoints."""

import json
import sqlite3
import time

from hyper import config

from .common import iso_epoch, q1, qall, score100


# Scanner has an independent minute heartbeat.  ``scan_progress.updated_at`` is
# also an authoritative fallback for a scan that started before that writer was
# deployed, or for a best-effort heartbeat skipped during a short SQLite lock.
SCANNER_STALE_SEC = 15 * 60


def scanner_status(db):
    """Live status of the continuous rolling scanner."""
    r = q1(db, "SELECT state,heartbeat_at,detail_json FROM process_status WHERE name='scanner'")
    try:
        progress = q1(
            db,
            "SELECT state,started_at,stage,candidates_scanned,candidates_total,updated_at,"
            "selected_source,effective_source,source_fallback_reason,source_fallback_at "
            "FROM scan_progress WHERE id=1",
        )
        if progress is None:  # compact legacy test/status databases omit started_at
            progress = q1(
                db,
                "SELECT state,stage,candidates_scanned,candidates_total,updated_at "
                "FROM scan_progress WHERE id=1",
            )
    except Exception:  # noqa: BLE001 - compatibility with compact/old status databases
        progress = None
    progress_active = bool(progress and progress["state"] == "scanning")
    if not r:
        if progress_active:
            detail = {
                "stage": progress["stage"],
                "scanned": progress["candidates_scanned"],
                "total": progress["candidates_total"],
            }
            if "selected_source" in progress.keys():
                detail.update({
                    "selectedSource": progress["selected_source"] or "official",
                    "effectiveSource": progress["effective_source"]
                    or progress["selected_source"] or "official",
                    "sourceFallbackReason": progress["source_fallback_reason"],
                    "sourceFallbackAt": progress["source_fallback_at"],
                })
            return {
                "mode": "scanning", "stale": False,
                "heartbeatAt": progress["updated_at"],
                "detail": detail,
            }
        ran = q1(db, "SELECT COUNT(*) c FROM scan_runs")
        return {"mode": "idle" if (ran and ran["c"]) else "unknown", "stale": False,
                "heartbeatAt": None, "detail": {}}
    try:
        detail = json.loads(r["detail_json"]) if r["detail_json"] else {}
    except (ValueError, TypeError):
        detail = {}
    heartbeat_at = r["heartbeat_at"]
    if progress_active:
        progress_hb = iso_epoch(progress["updated_at"])
        process_hb = iso_epoch(heartbeat_at)
        if progress_hb and (not process_hb or progress_hb > process_hb):
            heartbeat_at = progress["updated_at"]
        detail.update({
            "stage": progress["stage"],
            "scanned": progress["candidates_scanned"],
            "total": progress["candidates_total"],
        })
        if "selected_source" in progress.keys():
            detail.update({
                "selectedSource": progress["selected_source"] or "official",
                "effectiveSource": progress["effective_source"]
                or progress["selected_source"] or "official",
                "sourceFallbackReason": progress["source_fallback_reason"],
                "sourceFallbackAt": progress["source_fallback_at"],
            })
        if "started_at" in progress.keys():
            detail["startedAt"] = progress["started_at"]
    hb = iso_epoch(heartbeat_at)
    mode = "scanning" if progress_active else (r["state"] or "unknown")
    stale = bool(
        mode != "idle"
        and hb
        and (time.time() - hb) > SCANNER_STALE_SEC
    )
    return {"mode": mode,
            "stale": stale,
            "heartbeatAt": heartbeat_at, "detail": detail}


def followed_count(db):
    """Count the effective Core rows shown by the Dashboard's followed tab.

    ``draining`` wallets retain their Core seat and remain visible while exits are
    managed, even though their entry switch is disabled.  ``requalify`` is the
    state that releases the seat.  Keep this predicate identical to the wallet
    list instead of counting the legacy ``enabled`` flag.
    """
    try:
        selected = q1(
            db,
            "SELECT sg.generation FROM scan_generation sg "
            "WHERE sg.status='published' AND sg.complete=1 AND sg.is_current=1 "
            "ORDER BY sg.id DESC LIMIT 1",
        )
    except Exception:  # noqa: BLE001 - compatibility with pre-generation read replicas
        selected = None
    if not selected:
        return 0
    r = q1(
        db,
        "SELECT COUNT(*) cnt FROM follow_selection fs "
        "LEFT JOIN target_controls tc ON lower(tc.addr)=lower(fs.addr) "
        "WHERE fs.generation=? AND fs.role='core' AND COALESCE(fs.enabled,1)=1 "
        "AND COALESCE(tc.intent,'active')!='requalify'",
        (selected["generation"],),
    )
    return (r["cnt"] if r else 0)


def ep_discovery(db):
    leaderboard = (q1(db, "SELECT COUNT(*) c FROM leaderboard") or {"c": 0})["c"]
    candidates = (q1(db, "SELECT COUNT(*) c FROM leaderboard WHERE is_candidate=1") or {"c": 0})["c"]
    watchlist = followed_count(db)
    current_generation = q1(
        db,
        "SELECT generation,metrics_json FROM scan_generation "
        "WHERE status='published' AND complete=1 AND is_current=1 ORDER BY id DESC LIMIT 1",
    )
    # Challenger refresh generations intentionally cover only a small daily workset while carrying the
    # existing Core forward.  Mixing their 8/7-wallet qualification counts with the carried 10-wallet Core
    # made the Dashboard's so-called funnel grow at the final step.  The funnel must describe one complete
    # scan generation; current membership remains available separately through ``core``/``watchlist``.
    try:
        full_generation = q1(
            db,
            "SELECT generation,metrics_json,leaderboard_rows,profile_valid,published_at "
            "FROM scan_generation WHERE source='scan' AND status='published' AND complete=1 "
            "ORDER BY published_at DESC,id DESC LIMIT 1",
        )
    except Exception:  # noqa: BLE001 - compatibility with compact/old read replicas
        full_generation = None
    funnel_generation = full_generation or current_generation
    challenger = 0
    core = watchlist
    performance = {}
    pre_strict_counts = {}
    if current_generation:
        roles = qall(
            db,
            "SELECT role,COUNT(*) n FROM follow_selection WHERE generation=? AND enabled=1 GROUP BY role",
            (current_generation["generation"],),
        )
        role_counts = {r["role"]: r["n"] for r in roles}
        challenger = role_counts.get("challenger", 0)
        core = role_counts.get("core", 0)
    funnel_role_counts = {}
    if funnel_generation:
        funnel_roles = qall(
            db,
            "SELECT role,COUNT(*) n FROM follow_selection WHERE generation=? AND enabled=1 GROUP BY role",
            (funnel_generation["generation"],),
        )
        funnel_role_counts = {r["role"]: r["n"] for r in funnel_roles}
        try:
            performance = json.loads(funnel_generation["metrics_json"] or "{}")
        except (TypeError, ValueError):
            performance = {}
        try:
            evidence = q1(
                db,
                "SELECT COUNT(*) evidence_rows,COUNT(*) rough_completed,"
                "SUM(CASE WHEN latest_7d_active=1 AND active_weeks_4>=3 "
                "AND max_open_gap_days_28d<=10 THEN 1 ELSE 0 END) persistent_activity,"
                "SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) pf_lottery_passed,"
                "SUM(CASE WHEN status='passed' AND tier='primary' THEN 1 ELSE 0 END) primary_n,"
                "SUM(CASE WHEN status='passed' AND tier='reserve' THEN 1 ELSE 0 END) reserve_n,"
                "SUM(CASE WHEN queue_rank IS NOT NULL THEN 1 ELSE 0 END) top32_n,"
                "SUM(CASE WHEN strict_status='qualified' THEN 1 ELSE 0 END) strict_n "
                "FROM pre_strict_evidence WHERE generation=?",
                (funnel_generation["generation"],),
            )
            # Successful publication deliberately removes generation-scoped
            # ``pre_strict_evidence`` workspace.  SQLite aggregate queries still
            # return one all-zero row for that empty set, so only prefer the live
            # evidence counts while at least one evidence row actually exists.
            # Otherwise retain the durable counts frozen into metrics_json.
            if evidence and int(evidence["evidence_rows"] or 0) > 0:
                pre_strict_counts = {
                    key: int(evidence[key] or 0)
                    for key in evidence.keys()
                }
        except Exception:  # noqa: BLE001 - old read replicas may not have the new evidence table yet
            pre_strict_counts = {}
    funnel_leaderboard = leaderboard
    profile_valid = performance.get("profileValid")
    funnel_published_at = None
    if full_generation:
        if int(full_generation["leaderboard_rows"] or 0) > 0:
            funnel_leaderboard = int(full_generation["leaderboard_rows"])
        if full_generation["profile_valid"] is not None:
            profile_valid = int(full_generation["profile_valid"] or 0)
        funnel_published_at = full_generation["published_at"]
    if profile_valid is None:
        profile_valid = performance.get(
            "structurePassed", pre_strict_counts.get("rough_completed")
        )
    funnel_core = int(funnel_role_counts.get(
        "core", performance.get("selectionCore", core)
    ) or 0)
    selection_pool = (
        funnel_role_counts.get("core", 0) + funnel_role_counts.get("challenger", 0)
        if funnel_role_counts
        else int(performance.get("selectionCore", funnel_core) or 0)
             + int(performance.get("selectionChallenger", 0) or 0)
    )
    monotonic_counts = [
        funnel_leaderboard,
        performance.get("perpPrefilterPassed", candidates),
        profile_valid,
        selection_pool,
        funnel_core,
    ]
    funnel_consistent = all(
        left is not None and right is not None and int(left) >= int(right)
        for left, right in zip(monotonic_counts, monotonic_counts[1:])
    )
    last_scan = q1(db, "SELECT MAX(finished_at) m FROM scan_runs")
    funnel = {
        "leaderboard": funnel_leaderboard,
        "candidates": performance.get("coarseRecallPassed", candidates),
        "perpPrefilter": performance.get("perpPrefilterPassed", candidates),
        "profileValid": profile_valid,
        "selectionPool": selection_pool,
        "funnelConsistent": funnel_consistent,
        "funnelPublishedAt": funnel_published_at,
        "structurePassed": performance.get(
            "structurePassed", pre_strict_counts.get("rough_completed")
        ),
        "roughCompleted": pre_strict_counts.get(
            "rough_completed", performance.get("roughCopyCompleted")
        ),
        "persistentActivity": pre_strict_counts.get(
            "persistent_activity", performance.get("persistentActivityPassed")
        ),
        "pfLotteryPassed": pre_strict_counts.get(
            "pf_lottery_passed", performance.get("preStrictPassed")
        ),
        "primary": pre_strict_counts.get(
            "primary_n", performance.get("preStrictPrimary")
        ),
        "reserve": pre_strict_counts.get(
            "reserve_n", performance.get("preStrictReserve")
        ),
        "top32": pre_strict_counts.get(
            "top32_n", performance.get("preStrictTop32")
        ),
        "strict": pre_strict_counts.get(
            "strict_n", performance.get("strictQualified")
        ),
        "challenger": challenger,
        "core": core,
        "finalCore": funnel_core,
        "watchlist": watchlist,
    }
    return {"funnel": funnel,
            "scanner": scanner_status(db),
            "lastScanAt": (last_scan["m"] if last_scan else None)}


def ep_scan_runs(db, limit):
    limit = max(0, min(int(limit), int(config.SCAN_HISTORY_KEEP_COUNT)))
    base_sql = (
        "SELECT started_at,finished_at,candidates,COALESCE(profiled,probed_new) AS profiled,"
        "added,retired,kept,rejected,n_active,COALESCE(failed,0) AS failed,"
        "COALESCE(complete,1) AS complete,COALESCE(full,0) AS full,"
        "COALESCE(kind,'complete') AS kind,COALESCE(api_requests,0) AS api_requests,"
        "COALESCE(api_weight,0) AS api_weight,outcome_reason,"
        "COALESCE(core_added,0) AS core_added,COALESCE(core_removed,0) AS core_removed,"
        "COALESCE(core_probation,0) AS core_probation,"
        "COALESCE(core_recovered,0) AS core_recovered,"
        "COALESCE(core_confirmed_demotion,0) AS core_confirmed_demotion,"
        "COALESCE(core_safety_exit,0) AS core_safety_exit,"
        "COALESCE(replacement_blocked,0) AS replacement_blocked"
    )
    try:
        rows = db.execute(
            base_sql + ",COALESCE(selected_source,'official') AS selected_source,"
            "COALESCE(effective_source,selected_source,'official') AS effective_source,"
            "source_fallback_reason,source_fallback_at "
            "FROM scan_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        has_source = True
    except sqlite3.OperationalError:
        rows = qall(db, base_sql + " FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,))
        has_source = False
    return {"runs": [{"at": _col(r, "started_at", 0), "finishedAt": _col(r, "finished_at", 1),
                      "candidates": _col(r, "candidates", 2), "profiled": _col(r, "profiled", 3),
                      "added": _col(r, "added", 4), "retired": _col(r, "retired", 5),
                      "kept": _col(r, "kept", 6), "rejected": _col(r, "rejected", 7),
                      "active": _col(r, "n_active", 8), "failed": _col(r, "failed", 9) or 0,
                      "complete": bool(_col(r, "complete", 10)), "full": bool(_col(r, "full", 11)),
                      "kind": _col(r, "kind", 12) or "complete",
                      "apiRequests": _col(r, "api_requests", 13) or 0,
                      "apiWeight": _col(r, "api_weight", 14) or 0,
                      "reason": _col(r, "outcome_reason", 15),
                      "coreAdded": _col(r, "core_added", 16) or 0,
                      "coreRemoved": _col(r, "core_removed", 17) or 0,
                      "coreProbation": _col(r, "core_probation", 18) or 0,
                      "coreRecovered": _col(r, "core_recovered", 19) or 0,
                      "coreConfirmedDemotion": _col(r, "core_confirmed_demotion", 20) or 0,
                      "coreSafetyExit": _col(r, "core_safety_exit", 21) or 0,
                      "replacementBlocked": bool(_col(r, "replacement_blocked", 22)),
                      "selectedSource": (_col(r, "selected_source", 23) or "official")
                      if has_source else "official",
                      "effectiveSource": (_col(r, "effective_source", 24) or "official")
                      if has_source else "official",
                      "sourceFallbackReason": _col(r, "source_fallback_reason", 25)
                      if has_source else None,
                      "sourceFallbackAt": _col(r, "source_fallback_at", 26)
                      if has_source else None}
                     for r in rows]}


def ep_scan_status(db):
    r = q1(db, "SELECT * FROM scan_progress WHERE id=1")
    if not r or (r["state"] or "idle") != "scanning":
        return {"state": "idle"}
    started = iso_epoch(r["started_at"])
    elapsed = int(time.time() - started) if started else 0
    total, scanned, eta = r["candidates_total"] or 0, r["candidates_scanned"] or 0, r["eta_sec"] or 1200
    pct = round(scanned / total * 100) if total else min(99, round(elapsed / eta * 100))
    manual = bool(r["manual"]) if "manual" in r.keys() else True
    selected_source = r["selected_source"] if "selected_source" in r.keys() else "official"
    effective_source = r["effective_source"] if "effective_source" in r.keys() else selected_source
    return {"state": "scanning", "manual": manual, "startedAt": r["started_at"], "elapsedSec": elapsed,
            "etaSec": eta, "progressPct": pct, "candidatesScanned": scanned,
            "candidatesTotal": total, "stage": r["stage"],
            "selectedSource": selected_source or "official",
            "effectiveSource": effective_source or selected_source or "official",
            "sourceFallbackReason": r["source_fallback_reason"]
            if "source_fallback_reason" in r.keys() else None,
            "sourceFallbackAt": r["source_fallback_at"]
            if "source_fallback_at" in r.keys() else None}


def ep_score_dist(db):
    """All watchlist display scores (0-100), sorted desc."""
    scores = [round(score100(r["score"] or 0.0), 1)
              for r in qall(db, "SELECT score FROM watchlist ORDER BY score DESC")]
    return {"scores": scores, "total": len(scores)}


def _dict_rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _limit(qs, default=100, max_limit=500):
    try:
        val = int(qs.get("limit", [default])[0])
    except (TypeError, ValueError):
        val = default
    return max(1, min(max_limit, val))


def _truthy(qs, key):
    return str((qs.get(key, [""]) or [""])[0]).lower() in {"1", "true", "yes", "on"}


def _pick_nonempty(obj, keys):
    if not isinstance(obj, dict):
        return {}
    return {
        key: obj[key]
        for key in keys
        if key in obj and obj[key] not in (None, {}, [])
    }


def _compact_sector_copy(sector_copy):
    if not isinstance(sector_copy, dict):
        return {}
    out = {}
    metric_keys = (
        "copy_net_pnl", "closed_n", "win_rate", "open_fill_rate",
        "liquidations", "fee_drag",
    )
    for sector, windows in sector_copy.items():
        if not isinstance(windows, dict):
            continue
        sector_out = {}
        for window, metrics in windows.items():
            slim = _pick_nonempty(metrics, metric_keys)
            if slim:
                sector_out[str(window)] = slim
        if sector_out:
            out[sector] = sector_out
    return out


def _compact_sector_policy(policy):
    if not isinstance(policy, dict):
        return {}
    out = {}
    allowed = policy.get("allowed")
    if isinstance(allowed, list):
        out["allowed"] = allowed
    for sector in ("crypto", "stock"):
        slim = _pick_nonempty(policy.get(sector), ("allow", "status", "reason", "pnl", "closed"))
        if slim:
            out[sector] = slim
    return out


def _compact_audit_payload(payload):
    """Small payload shape used by dashboard rows; full payload remains available by default."""
    if not isinstance(payload, dict) or not payload:
        return {}
    compact = {
        "copyBt": _pick_nonempty(payload.get("copyBt"), (
            "30dNetPnl", "30dClosedN", "14dNetPnl", "14dClosedN", "7dNetPnl", "7dClosedN",
            "winRate", "openFillRate", "liquidations", "feeDrag",
        )),
        "followEligibility": _pick_nonempty(payload.get("followEligibility"), (
            "eligible", "coreEligible", "role", "status", "hardRisk", "reasons",
        )),
        "decisionAudit": _pick_nonempty(payload.get("decisionAudit"), (
            "stage", "failureCategory", "firstHardFailure", "thresholds", "actual",
        )),
        "sectorCopy": _compact_sector_copy(payload.get("sectorCopy")),
        "sectorPolicy": _compact_sector_policy(payload.get("sectorPolicy")),
    }
    return {key: val for key, val in compact.items() if val not in (None, {}, [])}


def ep_pipeline_audit(db, qs):
    """Recent scanner/follow pipeline decisions for ops debugging."""
    where, args = [], []
    compact = _truthy(qs, "compact")
    for key in ("generation", "stamp", "source", "stage", "addr"):
        val = (qs.get(key, [None]) or [None])[0]
        if not val:
            continue
        col = "addr" if key == "addr" else key
        where.append(f"{col}=?")
        args.append(val.lower() if key == "addr" else val)
    sql = (
        "SELECT id,generation,stamp,source,stage,addr,rank,status,reason,raw_score,follow_score,payload_json,created_at "
        "FROM pipeline_audit"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    rows = _dict_rows(db.execute(sql, (*args, _limit(qs))))
    events = []
    for r in rows:
        try:
            payload = json.loads(r.pop("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        if compact:
            payload = _compact_audit_payload(payload)
        events.append({
            "id": r["id"],
            "generation": r["generation"],
            "stamp": r["stamp"],
            "source": r["source"],
            "stage": r["stage"],
            "addr": r["addr"],
            "rank": r["rank"],
            "status": r["status"],
            "reason": r["reason"],
            "rawScore": score100(r["raw_score"]) if r["raw_score"] is not None else None,
            "followScore": score100(r["follow_score"]) if r["follow_score"] is not None else None,
            "payload": payload,
            "createdAt": r["created_at"],
        })
    requested_generation = (qs.get("generation", [None]) or [None])[0]
    terminal = False
    if not events:
        if requested_generation:
            row = q1(db, "SELECT status FROM scan_generation WHERE generation=?", (requested_generation,))
        else:
            row = q1(
                db,
                "SELECT status FROM scan_generation WHERE is_current=1 ORDER BY id DESC LIMIT 1",
            )
        terminal = bool(row and _col(row, "status", 0) in {"published", "failed"})
    return {"events": events, "total": len(events), "evidenceExpired": terminal}


def _published_pipeline_summary(db, qs):
    """Project compact authoritative state after transient audit has been cleaned."""
    requested = (qs.get("generation", [None]) or [None])[0]
    if requested:
        row = db.execute(
            "SELECT generation,source,status,started_at,published_at,profile_total,profile_valid,"
            "profile_deferred,profile_rejected,workset_n,metrics_json,error "
            "FROM scan_generation WHERE generation=?",
            (requested,),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT generation,source,status,started_at,published_at,profile_total,profile_valid,"
            "profile_deferred,profile_rejected,workset_n,metrics_json,error "
            "FROM scan_generation WHERE is_current=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {
            "stamp": None, "source": None, "generation": requested,
            "evidenceExpired": False, "profile": {}, "selection": {},
            "autoTune": None, "workset": None, "prune": None,
        }
    generation = str(row[0])
    try:
        metrics = json.loads(row[10] or "{}")
    except (TypeError, ValueError):
        metrics = {}
    counts = {
        str(role): int(n or 0)
        for role, n in db.execute(
            "SELECT role,COUNT(*) FROM follow_selection WHERE generation=? GROUP BY role",
            (generation,),
        ).fetchall()
    }
    return {
        "stamp": row[4] or row[3],
        "source": row[1],
        "generation": generation,
        "evidenceExpired": str(row[2]) in {"published", "failed"},
        "profile": {
            "total": int(row[5] or 0), "active": int(row[6] or 0),
            "qualified": int(row[6] or 0), "rejected": int(row[8] or 0),
            "deferred": int(row[7] or 0), "retired": 0, "reasonCounts": [],
        },
        "selection": {
            "generation": generation, "action": None,
            "core": counts.get("core", int(metrics.get("selectionCore") or 0)),
            "challenger": counts.get(
                "challenger", int(metrics.get("selectionChallenger") or 0),
            ),
            "exitOnly": counts.get("exit_only", 0),
        },
        "autoTune": None,
        "workset": {
            "mode": metrics.get("worksetMode"),
            "profiled": int(row[9] or 0),
            "candidates": metrics.get("coarseRecallPassed"),
            "qualified": metrics.get("profileValid", row[6]),
            "deferredTail": metrics.get("profileDeferred", row[7]),
        },
        "prune": None,
        "error": row[11],
    }


def _latest_pipeline_key(db, qs):
    stamp = (qs.get("stamp", [None]) or [None])[0]
    source = (qs.get("source", [None]) or [None])[0]
    if stamp:
        if source:
            return stamp, source
        row = q1(db,
            "SELECT source FROM pipeline_audit "
            "WHERE stamp=? AND stage IN (?,?) ORDER BY id DESC LIMIT 1",
            (stamp, "tuner_finalize", "selection_summary"),
        )
        if not row:
            row = q1(db,
                "SELECT source FROM pipeline_audit WHERE stamp=? ORDER BY id DESC LIMIT 1",
                (stamp,),
            )
        return (stamp, _col(row, "source", 0)) if row else (stamp, None)
    if source:
        row = q1(db,
            "SELECT stamp,source FROM pipeline_audit "
            "WHERE source=? AND stage IN (?,?) ORDER BY id DESC LIMIT 1",
            (source, "tuner_finalize", "selection_summary"),
        )
        if not row:
            row = q1(db,
                "SELECT stamp,source FROM pipeline_audit WHERE source=? ORDER BY id DESC LIMIT 1",
                (source,),
            )
        return (_col(row, "stamp", 0), _col(row, "source", 1)) if row else (None, source)
    row = q1(db,
        "SELECT stamp,source FROM pipeline_audit "
        "WHERE stage IN (?,?) ORDER BY id DESC LIMIT 1",
        ("tuner_finalize", "selection_summary"),
    )
    if not row:
        row = q1(db, "SELECT stamp,source FROM pipeline_audit ORDER BY id DESC LIMIT 1")
    return (_col(row, "stamp", 0), _col(row, "source", 1)) if row else (None, None)


def _col(row, key, idx=None):
    if row is None:
        return None
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return row[idx] if idx is not None else None


def _payload(row):
    if not row:
        return {}
    try:
        return json.loads(_col(row, "payload_json", -1) or "{}")
    except (TypeError, ValueError):
        return {}


def ep_pipeline_summary(db, qs):
    """Compact latest pipeline audit into the Discovery page's operator summary."""
    stamp, source = _latest_pipeline_key(db, qs)
    if not stamp:
        return _published_pipeline_summary(db, qs)
    base = [stamp] + ([source] if source else [])
    src_where = " AND source=?" if source else ""

    status_rows = qall(db,
        "SELECT status,COUNT(*) n FROM pipeline_audit "
        f"WHERE stamp=?{src_where} AND stage='profile' GROUP BY status",
        tuple(base),
    )
    status_counts = {(_col(r, "status", 0) or "unknown"): _col(r, "n", 1) for r in status_rows}
    reason_rows = qall(db,
        "SELECT status,reason,COUNT(*) n FROM pipeline_audit "
        f"WHERE stamp=?{src_where} AND stage='profile' AND status!='active' "
        "GROUP BY status,reason ORDER BY n DESC,reason LIMIT 8",
        tuple(base),
    )

    selection_rows = qall(db,
        "SELECT status,COUNT(*) n FROM pipeline_audit "
        f"WHERE stamp=?{src_where} AND stage='selection' GROUP BY status",
        tuple(base),
    )
    selection_counts = {(_col(r, "status", 0) or "unknown"): _col(r, "n", 1) for r in selection_rows}
    selection_summary_row = q1(db,
        "SELECT payload_json FROM pipeline_audit "
        f"WHERE stamp=?{src_where} AND stage='selection_summary' ORDER BY id DESC LIMIT 1",
        tuple(base),
    )
    selection_summary = _payload(selection_summary_row)

    workset_row = q1(db,
        "SELECT payload_json FROM pipeline_audit "
        f"WHERE stamp=?{src_where} AND stage='workset' ORDER BY id DESC LIMIT 1",
        tuple(base),
    )
    workset_payload = _payload(workset_row)
    work_counts = workset_payload.get("counts") or {}
    workset = None
    if workset_row:
        workset = {
            "mode": workset_payload.get("mode"),
            "fullScan": bool(workset_payload.get("fullScan")),
            "limit": workset_payload.get("limit"),
            "dailyRecheckTop": workset_payload.get("dailyRecheckTop"),
            "candidates": work_counts.get("candidate"),
            "profiledBefore": work_counts.get("profiled_before"),
            "activeTotal": work_counts.get("active_total"),
            "active": work_counts.get("active_candidate"),
            "qualified": work_counts.get("qualified", work_counts.get("active_candidate")),
            "core": work_counts.get("core"),
            "challenger": work_counts.get("challenger"),
            "positions": work_counts.get("position"),
            "warmupBackfill": work_counts.get("warmup_backfill"),
            "new": work_counts.get("new_candidate"),
            "topRecheck": work_counts.get("top_recheck"),
            "offListActive": work_counts.get("off_list_active"),
            "offListQualified": work_counts.get(
                "off_list_qualified", work_counts.get("off_list_active")
            ),
            "profiled": work_counts.get("workset"),
            "deferredTail": work_counts.get("deferred_tail"),
        }

    tune_row = q1(db,
        "SELECT status,reason,payload_json FROM pipeline_audit "
        f"WHERE stamp=?{src_where} AND stage='tuner_finalize' ORDER BY id DESC LIMIT 1",
        tuple(base),
    )
    tune_payload = _payload(tune_row)
    auto_tune = None
    if tune_row:
        auto_tune = {
            "status": _col(tune_row, "status", 0),
            "reason": _col(tune_row, "reason", 1),
            "portfolioReplay": tune_payload.get("portfolioReplay"),
            "selectionReplay": tune_payload.get("selectionReplay"),
        }

    prune_row = q1(db,
        "SELECT payload_json FROM pipeline_audit "
        f"WHERE stamp=?{src_where} AND stage='prune' ORDER BY id DESC LIMIT 1",
        tuple(base),
    )
    prune = _payload(prune_row) if prune_row else None

    return {
        "stamp": stamp,
        "source": source,
        "generation": selection_summary.get("generation"),
        "evidenceExpired": False,
        "profile": {
            "total": sum(status_counts.values()),
            "active": status_counts.get("active", 0),
            "qualified": status_counts.get("active", 0),
            "rejected": status_counts.get("rejected", 0),
            "retired": status_counts.get("retired", 0),
            "reasonCounts": [{"status": _col(r, "status", 0), "reason": _col(r, "reason", 1), "count": _col(r, "n", 2)}
                             for r in reason_rows],
        },
        "selection": {
            "generation": selection_summary.get("generation"),
            "action": selection_summary.get("action"),
            "core": selection_counts.get("core", selection_summary.get("core", 0)),
            "challenger": selection_counts.get("challenger", selection_summary.get("challenger", 0)),
            "exitOnly": selection_counts.get("exit_only", selection_summary.get("exitOnly", 0)),
        },
        "autoTune": auto_tune,
        "workset": workset,
        "prune": prune,
    }
