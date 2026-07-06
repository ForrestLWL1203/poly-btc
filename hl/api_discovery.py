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
