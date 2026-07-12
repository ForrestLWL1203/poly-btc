"""Dashboard strategy parameter endpoints and writes."""

import sqlite3

from . import params as params_mod
from .api_common import score100
from .coin_filter import format_coin_blacklist
from .util import now_iso


WRITABLE_LEVELS = {"green", "yellow", "blue"}
REMOVED_PARAMS = {"MIN_FOLLOW_SCORE"}


def rw_connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    return db


def patch_params(db_path, category, updates):
    """Write UI param edits to the params table."""
    db = rw_connect(db_path)
    try:
        out = {}
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
            if key == "COIN_BLACKLIST":
                stored = format_coin_blacklist(val)
            else:
                stored = val
            sval = (None if stored is None else "true" if stored is True
                    else "false" if stored is False else str(stored))
            db.execute("UPDATE params SET value=?,updated_at=? WHERE key=?", (sval, now_iso(), key))
            out[key] = val
        db.commit()
        return out
    finally:
        db.close()


def reset_params(db_path, category):
    """Restore strategy params to code defaults."""
    db = rw_connect(db_path)
    try:
        cat = None if category == "all" else category
        return params_mod.reset_defaults(db, cat)
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
