"""Dashboard strategy parameter endpoints and writes."""

import sqlite3

from . import params as params_mod
from .api_common import score100, score_from100
from .coin_filter import format_coin_blacklist
from .util import now_iso


WRITABLE_LEVELS = {"green", "yellow", "blue"}


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
            row = db.execute("SELECT category,level,type FROM params WHERE key=?", (key,)).fetchone()
            if not row:
                continue
            if row["category"] != category:
                continue
            if row["level"] not in WRITABLE_LEVELS or row["type"] == "display":
                raise ValueError(f"{key} is read-only")
            if key == "MIN_FOLLOW_SCORE" and val is not None:
                stored = score_from100(val)
            elif key == "COIN_BLACKLIST":
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


def ep_params(db):
    data = params_mod.get_all(db)
    # MIN_FOLLOW_SCORE is stored native [0,1], but displayed on the 0-100 score ruler.
    for pr in data.get("follow", []):
        if pr["key"] == "MIN_FOLLOW_SCORE":
            pr["value"] = score100(pr["value"])
            pr["default"] = score100(pr["default"])
            pr["scaled"] = True
    return data
