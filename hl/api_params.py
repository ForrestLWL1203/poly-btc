"""Dashboard strategy parameter endpoints and writes."""

import json
import sqlite3

from . import params as params_mod
from .api_common import score100
from .coin_filter import format_coin_blacklist
from .util import now_iso


WRITABLE_LEVELS = {"green", "yellow", "blue"}
REMOVED_PARAMS = {"MIN_FOLLOW_SCORE", "COPY_STOP_ENABLE", "STOP_MARGIN_PCT"}


def rw_connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    return db


def _enqueue_follow_revision(db, source):
    """Ask Observer (a business-state writer) to materialise the edited params as a revision."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='commands'"
    ).fetchone():
        return
    stamp = now_iso()
    db.execute(
        "INSERT INTO commands (type,payload_json,owner,status,created_at) "
        "VALUES ('reload_params',?,'dashboard','pending',?)",
        (json.dumps({
            "by": source,
            "createStrategyRevision": True,
            "reason": "operator_follow_params_changed",
        }, sort_keys=True), stamp),
    )


def patch_params(db_path, category, updates):
    """Write UI param edits to the params table."""
    db = rw_connect(db_path)
    try:
        out = {}
        tail_pct_keys = {
            "TAIL_CLOSE_HARD_REMAIN_PCT", "TAIL_CLOSE_RISK_REMAIN_PCT",
            "TAIL_CLOSE_PROFIT_GIVEBACK_PCT",
        }
        for key, val in (updates or {}).items():
            if key in REMOVED_PARAMS:
                continue
            row = db.execute("SELECT category,level,type FROM params WHERE key=?", (key,)).fetchone()
            if not row:
                continue
            if row["category"] != category:
                continue
            if row["level"] not in WRITABLE_LEVELS or row["type"] == "display":
                raise ValueError(f"{key} is read-only")
            if key == "MARGIN_EQUITY_PCT":
                try:
                    margin_equity_pct = float(val)
                except (TypeError, ValueError) as exc:
                    raise ValueError("MARGIN_EQUITY_PCT must be numeric") from exc
                if not 10.0 <= margin_equity_pct <= 100.0:
                    raise ValueError("MARGIN_EQUITY_PCT must be between 10 and 100")
            if key in tail_pct_keys:
                try:
                    tail_pct = float(val)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be numeric") from exc
                if not 0.0 <= tail_pct <= 100.0:
                    raise ValueError(f"{key} must be between 0 and 100")
            if key == "COIN_BLACKLIST":
                stored = format_coin_blacklist(val)
            else:
                stored = val
            sval = (None if stored is None else "true" if stored is True
                    else "false" if stored is False else str(stored))
            db.execute("UPDATE params SET value=?,updated_at=? WHERE key=?", (sval, now_iso(), key))
            out[key] = val
        tail_enabled = out.get("TAIL_CLOSE_ENABLE")
        if tail_enabled is None:
            enabled_row = db.execute(
                "SELECT value FROM params WHERE key='TAIL_CLOSE_ENABLE'"
            ).fetchone()
            tail_enabled = (str(enabled_row["value"]).lower() in ("1", "true", "yes")
                            if enabled_row else True)
        if category == "follow" and tail_enabled and tail_pct_keys.intersection(out):
            tail_values = {
                row["key"]: float(row["value"])
                for row in db.execute(
                    "SELECT key,value FROM params WHERE key IN (?,?,?)",
                    tuple(sorted(tail_pct_keys)),
                ).fetchall()
            }
            if (tail_values.get("TAIL_CLOSE_HARD_REMAIN_PCT", 0.0)
                    > tail_values.get("TAIL_CLOSE_RISK_REMAIN_PCT", 100.0)):
                raise ValueError("TAIL_CLOSE_HARD_REMAIN_PCT must not exceed TAIL_CLOSE_RISK_REMAIN_PCT")
        if category == "follow" and out:
            _enqueue_follow_revision(db, "dashboard_params")
        db.commit()
        return out
    finally:
        db.close()


def reset_params(db_path, category):
    """Restore strategy params to code defaults."""
    db = rw_connect(db_path)
    try:
        cat = None if category == "all" else category
        count = params_mod.reset_defaults(db, cat, commit=False)
        if cat in (None, "follow"):
            _enqueue_follow_revision(db, "dashboard_params_reset")
        db.commit()
        return count
    finally:
        db.close()


def _score_dist(db):
    """All watchlist display scores (0-100), sorted desc."""
    scores = [round(score100(r["score"] or 0.0), 1)
              for r in db.execute("SELECT score FROM watchlist ORDER BY score DESC").fetchall()]
    return {"scores": scores, "total": len(scores)}


def ep_params(db, include_score_dist=False):
    data = params_mod.get_all(db)
    # Explicit published Core is the only production target truth.  Existing databases may retain the
    # retired score-line row for migration compatibility, but it must never reappear in the operator UI.
    for category in list(data):
        if isinstance(data.get(category), list):
            data[category] = [pr for pr in data[category] if pr.get("key") not in REMOVED_PARAMS]
    if include_score_dist:
        try:
            data["scoreDist"] = _score_dist(db)
        except sqlite3.OperationalError:
            data["scoreDist"] = {"scores": [], "total": 0}
    return data
