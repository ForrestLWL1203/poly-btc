"""Discovery and scanner-status dashboard endpoints."""

import json
import time

from . import config
from . import params as params_mod
from .api_common import iso_epoch, q1, qall, score100


PROC_STALE_SEC = 90


def scanner_status(db):
    """Live status of the continuous rolling scanner."""
    r = q1(db, "SELECT state,heartbeat_at,detail_json FROM process_status WHERE name='scanner'")
    if not r:
        ran = q1(db, "SELECT COUNT(*) c FROM scan_runs")
        return {"mode": "idle" if (ran and ran["c"]) else "unknown", "stale": False,
                "heartbeatAt": None, "detail": {}}
    try:
        detail = json.loads(r["detail_json"]) if r["detail_json"] else {}
    except (ValueError, TypeError):
        detail = {}
    hb = iso_epoch(r["heartbeat_at"])
    stale = bool((r["state"] or "unknown") != "idle" and hb and (time.time() - hb) > PROC_STALE_SEC)
    return {"mode": r["state"] or "unknown",
            "stale": stale,
            "heartbeatAt": r["heartbeat_at"], "detail": detail}


def followed_count(db, line):
    """Count of wallets the observer will actually copy."""
    try:
        selected = q1(
            db,
            "SELECT sg.generation FROM scan_generation sg "
            "WHERE sg.status='published' AND sg.complete=1 AND sg.is_current=1 "
            "ORDER BY sg.id DESC LIMIT 1",
        )
    except Exception:  # noqa: BLE001 - compatibility with pre-generation read replicas
        selected = None
    if selected:
        r = q1(
            db,
            "SELECT COUNT(*) cnt FROM follow_selection fs "
            "LEFT JOIN target_controls tc ON tc.addr=fs.addr "
            "WHERE fs.generation=? AND fs.role='core' AND fs.enabled=1 AND COALESCE(tc.enabled,1)=1",
            (selected["generation"],),
        )
        return (r["cnt"] if r else 0)
    r = q1(db, "SELECT COUNT(*) cnt FROM watchlist w "
               "LEFT JOIN target_controls tc ON tc.addr=w.addr "
               "WHERE COALESCE(tc.enabled,1)=1 AND w.score>=?", (line,))
    return (r["cnt"] if r else 0)


# gate reason -> the 4 UI buckets (kept here so it's tweakable in one place)
_REJECT_BUCKETS = [
    ("样本 / 开仓活跃不足", {
        "inactive", "inactive_copyable_open", "thin_independent_evidence",
        "normalized_evidence_missing", "no_copy_evidence", "low_quality",
    }),
    ("净Edge不足 / 近期亏损", {
        "thin_edge", "thin_copy_edge", "recent_copy_loss", "copy_return_lcb_low",
        "positive_probability_low", "not_profitable",
    }),
    ("执行结构 / 容量不可跟", {
        "spot_dominant", "bot_frequency", "hft_uncopyable", "hft_turnover", "grid_dca",
        "heavy_dca", "too_many_concurrent", "low_fill_rate", "capacity_fit_low",
    }),
    ("爆仓 / 灾难风险", {"blowup_loss", "copy_liquidation"}),
]


def ep_discovery(db):
    leaderboard = (q1(db, "SELECT COUNT(*) c FROM leaderboard") or {"c": 0})["c"]
    candidates = (q1(db, "SELECT COUNT(*) c FROM leaderboard WHERE is_candidate=1") or {"c": 0})["c"]
    active = (q1(db, "SELECT COUNT(*) c FROM profile WHERE status='active'") or {"c": 0})["c"]
    # Funnel's final stage = wallets ABOVE the follow line, not the whole watchlist.
    line_native = params_mod.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE
    watchlist = followed_count(db, line_native)
    generation = q1(
        db,
        "SELECT generation,published_at,leaderboard_valid,profile_complete,workset_mode,fill_mode,"
        "workset_n,deferred_n,metrics_json FROM scan_generation "
        "WHERE status='published' AND complete=1 AND is_current=1 ORDER BY id DESC LIMIT 1",
    )
    qualified = active
    challenger = 0
    core = watchlist
    generation_out = None
    if generation:
        roles = qall(
            db,
            "SELECT role,COUNT(*) n FROM follow_selection WHERE generation=? AND enabled=1 GROUP BY role",
            (generation["generation"],),
        )
        role_counts = {r["role"]: r["n"] for r in roles}
        challenger = role_counts.get("challenger", 0)
        core = role_counts.get("core", 0)
        qualified_row = q1(
            db,
            "SELECT COUNT(*) c FROM profile WHERE profile_generation=? AND data_status='valid'",
            (generation["generation"],),
        )
        qualified = (qualified_row["c"] if qualified_row else 0) or 0
        try:
            perf = json.loads(generation["metrics_json"] or "{}")
        except (TypeError, ValueError):
            perf = {}
        generation_out = {
            "generation": generation["generation"],
            "publishedAt": generation["published_at"],
            "leaderboardValid": bool(generation["leaderboard_valid"]),
            "profileComplete": bool(generation["profile_complete"]),
            "worksetMode": generation["workset_mode"],
            "fillMode": generation["fill_mode"],
            "profiled": generation["workset_n"] or 0,
            "deferred": generation["deferred_n"] or 0,
            "performance": perf,
        }
    reason_rows = qall(db, "SELECT reason,COUNT(*) n FROM profile WHERE status='rejected' GROUP BY reason")
    counts = {row["reason"]: row["n"] for row in reason_rows}
    total_rej = sum(counts.values()) or 0
    buckets, used = [], set()
    for label, keys in _REJECT_BUCKETS:
        n = sum(counts.get(k, 0) for k in keys)
        used |= keys
        buckets.append([label, n])
    other = sum(v for k, v in counts.items() if k not in used)
    buckets.append(["其他", other])
    reject_reasons = [{"label": lbl, "pct": round(n / total_rej * 100) if total_rej else 0}
                      for lbl, n in buckets]

    follow_line = params_mod.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE
    nbins = 16
    bins = [0] * nbins
    for row in qall(db,
        "SELECT CASE WHEN score*?>=? THEN ? ELSE CAST(score*? AS INTEGER) END idx,COUNT(*) n "
        "FROM profile WHERE score IS NOT NULL AND score>0 GROUP BY idx",
        (nbins, nbins - 1, nbins - 1, nbins)):
        idx = int(row["idx"])
        if 0 <= idx < nbins:
            bins[idx] = row["n"]
    follow_idx = min(int(follow_line * nbins), nbins - 1)
    last_scan = q1(db, "SELECT MAX(finished_at) m FROM scan_runs")
    return {"funnel": {"leaderboard": leaderboard, "candidates": candidates, "qualified": qualified,
                        "challenger": challenger, "core": core, "active": active, "watchlist": watchlist},
            "rejectReasons": reject_reasons,
            "scoreHistogram": {"bins": bins, "followLineBinIndex": follow_idx},
            "scanner": scanner_status(db),
            "generation": generation_out,
            "lastScanAt": (last_scan["m"] if last_scan else None)}


def ep_scan_runs(db, limit):
    rows = qall(db, "SELECT started_at,finished_at,candidates,COALESCE(profiled,probed_new) AS profiled,"
                    "added,retired,kept,rejected,n_active,COALESCE(failed,0) AS failed,"
                    "COALESCE(complete,1) AS complete,COALESCE(full,0) AS full "
                    "FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,))
    return {"runs": [{"at": _col(r, "started_at", 0), "finishedAt": _col(r, "finished_at", 1),
                      "candidates": _col(r, "candidates", 2), "profiled": _col(r, "profiled", 3),
                      "added": _col(r, "added", 4), "retired": _col(r, "retired", 5),
                      "kept": _col(r, "kept", 6), "rejected": _col(r, "rejected", 7),
                      "active": _col(r, "n_active", 8), "failed": _col(r, "failed", 9) or 0,
                      "complete": bool(_col(r, "complete", 10)), "full": bool(_col(r, "full", 11))}
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
    return {"state": "scanning", "manual": manual, "startedAt": r["started_at"], "elapsedSec": elapsed,
            "etaSec": eta, "progressPct": pct, "candidatesScanned": scanned,
            "candidatesTotal": total, "stage": r["stage"]}


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
            "eligible", "status", "reasons",
        )),
        "sectorCopy": _compact_sector_copy(payload.get("sectorCopy")),
        "sectorPolicy": _compact_sector_policy(payload.get("sectorPolicy")),
    }
    return {key: val for key, val in compact.items() if val not in (None, {}, [])}


def ep_pipeline_audit(db, qs):
    """Recent scanner/follow pipeline decisions for ops debugging."""
    where, args = [], []
    compact = _truthy(qs, "compact")
    for key in ("stamp", "source", "stage", "addr"):
        val = (qs.get(key, [None]) or [None])[0]
        if not val:
            continue
        col = "addr" if key == "addr" else key
        where.append(f"{col}=?")
        args.append(val.lower() if key == "addr" else val)
    sql = (
        "SELECT id,stamp,source,stage,addr,rank,status,reason,raw_score,follow_score,payload_json,created_at "
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
    return {"events": events, "total": len(events)}


def _latest_pipeline_key(db, qs):
    stamp = (qs.get("stamp", [None]) or [None])[0]
    source = (qs.get("source", [None]) or [None])[0]
    if stamp:
        if source:
            return stamp, source
        row = q1(db,
            "SELECT source FROM pipeline_audit "
            "WHERE stamp=? AND stage IN (?,?,?) ORDER BY id DESC LIMIT 1",
            (stamp, "follow_line", "auto_tune", "selection_summary"),
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
            "WHERE source=? AND stage IN (?,?,?) ORDER BY id DESC LIMIT 1",
            (source, "follow_line", "auto_tune", "selection_summary"),
        )
        if not row:
            row = q1(db,
                "SELECT stamp,source FROM pipeline_audit WHERE source=? ORDER BY id DESC LIMIT 1",
                (source,),
            )
        return (_col(row, "stamp", 0), _col(row, "source", 1)) if row else (None, source)
    row = q1(db,
        "SELECT stamp,source FROM pipeline_audit "
        "WHERE stage IN (?,?,?) ORDER BY id DESC LIMIT 1",
        ("follow_line", "auto_tune", "selection_summary"),
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
        return {"stamp": None, "source": None, "profile": {}, "watchlist": {},
                "followLine": None, "autoTune": None, "workset": None, "prune": None}
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

    watch_rows = qall(db,
        "SELECT status,COUNT(*) n FROM pipeline_audit "
        f"WHERE stamp=?{src_where} AND stage='watchlist' GROUP BY status",
        tuple(base),
    )
    watch_counts = {(_col(r, "status", 0) or "unknown"): _col(r, "n", 1) for r in watch_rows}
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
            "new": work_counts.get("new_candidate"),
            "topRecheck": work_counts.get("top_recheck"),
            "offListActive": work_counts.get("off_list_active"),
            "profiled": work_counts.get("workset"),
            "deferredTail": work_counts.get("deferred_tail"),
        }

    follow_row = q1(db,
        "SELECT status,reason,follow_score,payload_json FROM pipeline_audit "
        f"WHERE stamp=?{src_where} AND stage='follow_line' ORDER BY id DESC LIMIT 1",
        tuple(base),
    )
    follow_payload = _payload(follow_row)
    follow_line = None
    if follow_row:
        follow_line = {
            "status": _col(follow_row, "status", 0),
            "reason": _col(follow_row, "reason", 1),
            "score": score100(_col(follow_row, "follow_score", 2)),
            "line": _col(follow_row, "follow_score", 2),
            "count": follow_payload.get("count"),
            "targetN": follow_payload.get("target_n"),
            "selected": follow_payload.get("selected"),
            "reference": follow_payload.get("reference"),
        }

    tune_row = q1(db,
        "SELECT status,reason,payload_json FROM pipeline_audit "
        f"WHERE stamp=?{src_where} AND stage='auto_tune' ORDER BY id DESC LIMIT 1",
        tuple(base),
    )
    tune_payload = _payload(tune_row)
    auto_tune = None
    if tune_row:
        auto_tune = {
            "status": _col(tune_row, "status", 0),
            "reason": _col(tune_row, "reason", 1),
            "applied": bool(tune_payload.get("applied")),
            "appliedSizing": bool(tune_payload.get("appliedSizing")),
            "appliedAdd": bool(tune_payload.get("appliedAdd")),
            "mode": tune_payload.get("mode"),
            "shadow": bool(tune_payload.get("shadow")),
            "eligibleToApply": bool(tune_payload.get("eligibleToApply")),
            "validation": tune_payload.get("validation") or {},
            "proposal": tune_payload.get("proposal") or {},
            "rollback": tune_payload.get("rollback"),
            "followedN": tune_payload.get("followedN"),
            "selectedMult": tune_payload.get("selectedMult"),
            "candidateCount": tune_payload.get("candidateCount"),
            "addCandidateCount": tune_payload.get("addCandidateCount"),
            "params": tune_payload.get("params") or {},
            "addParams": tune_payload.get("addParams") or {},
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
        "profile": {
            "total": sum(status_counts.values()),
            "active": status_counts.get("active", 0),
            "rejected": status_counts.get("rejected", 0),
            "retired": status_counts.get("retired", 0),
            "reasonCounts": [{"status": _col(r, "status", 0), "reason": _col(r, "reason", 1), "count": _col(r, "n", 2)}
                             for r in reason_rows],
        },
        "watchlist": {
            "total": sum(watch_counts.values()),
            "followed": watch_counts.get("followed", 0),
            "belowLine": watch_counts.get("below_line", 0),
            "disabled": watch_counts.get("disabled", 0),
        },
        "selection": {
            "generation": selection_summary.get("generation"),
            "action": selection_summary.get("action"),
            "core": selection_counts.get("core", 0),
            "challenger": selection_counts.get("challenger", 0),
            "exitOnly": selection_counts.get("exit_only", 0),
        },
        "followLine": follow_line,
        "autoTune": auto_tune,
        "workset": workset,
        "prune": prune,
    }
