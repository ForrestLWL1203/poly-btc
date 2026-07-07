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
    r = q1(db, "SELECT COUNT(*) cnt FROM watchlist w "
               "LEFT JOIN target_controls tc ON tc.addr=w.addr "
               "WHERE COALESCE(tc.enabled,1)=1 AND w.score>=?", (line,))
    return (r["cnt"] if r else 0)


# gate reason -> the 4 UI buckets (kept here so it's tweakable in one place)
_REJECT_BUCKETS = [
    ("不活跃 / 成交不足", {"inactive", "spot_dominant", "bot_frequency", "irregular"}),
    ("网格度过高", {"grid_dca"}),
    ("扛单 / 单笔大亏", {"blowup_loss", "not_profitable"}),
]


def ep_discovery(db):
    candidates = (q1(db, "SELECT COUNT(*) c FROM leaderboard WHERE is_candidate=1") or {"c": 0})["c"]
    active = (q1(db, "SELECT COUNT(*) c FROM profile WHERE status='active'") or {"c": 0})["c"]
    # Funnel's final stage = wallets ABOVE the follow line, not the whole watchlist.
    line_native = params_mod.get(db, "MIN_FOLLOW_SCORE", config.MIN_FOLLOW_SCORE) or config.MIN_FOLLOW_SCORE
    watchlist = followed_count(db, line_native)
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
    return {"funnel": {"candidates": candidates, "active": active, "watchlist": watchlist},
            "rejectReasons": reject_reasons,
            "scoreHistogram": {"bins": bins, "followLineBinIndex": follow_idx},
            "scanner": scanner_status(db),
            "lastScanAt": (last_scan["m"] if last_scan else None)}


def ep_scan_runs(db, limit):
    rows = qall(db, "SELECT started_at,finished_at,candidates,added,retired,kept,rejected,n_active "
                    "FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,))
    return {"runs": [{"at": r["started_at"], "finishedAt": r["finished_at"],
                      "candidates": r["candidates"], "added": r["added"], "retired": r["retired"],
                      "kept": r["kept"], "rejected": r["rejected"], "active": r["n_active"]}
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


def ep_pipeline_audit(db, qs):
    """Recent scanner/follow pipeline decisions for ops debugging."""
    where, args = [], []
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
        row = q1(db, """
            SELECT source FROM (
                SELECT source,MAX(id) max_id,
                       MAX(CASE WHEN stage IN ('follow_line','auto_tune') THEN id END) decision_id
                FROM pipeline_audit WHERE stamp=? GROUP BY source
            )
            ORDER BY (decision_id IS NOT NULL) DESC,COALESCE(decision_id,max_id) DESC LIMIT 1
        """, (stamp,))
        return (stamp, _col(row, "source", 0)) if row else (stamp, None)
    row = q1(db, """
        SELECT stamp,source FROM (
            SELECT stamp,source,MAX(id) max_id,
                   MAX(CASE WHEN stage IN ('follow_line','auto_tune') THEN id END) decision_id
            FROM pipeline_audit GROUP BY stamp,source
        )
        ORDER BY (decision_id IS NOT NULL) DESC,COALESCE(decision_id,max_id) DESC LIMIT 1
    """)
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
                "followLine": None, "autoTune": None}
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
            "followedN": tune_payload.get("followedN"),
            "selectedMult": tune_payload.get("selectedMult"),
            "candidateCount": tune_payload.get("candidateCount"),
            "addCandidateCount": tune_payload.get("addCandidateCount"),
            "params": tune_payload.get("params") or {},
            "addParams": tune_payload.get("addParams") or {},
        }

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
        "followLine": follow_line,
        "autoTune": auto_tune,
    }
