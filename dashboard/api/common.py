"""Shared helpers for dashboard API endpoint modules."""

import calendar
import sqlite3
import time

from hyper import config


def score100(raw):
    """Native v5 score [0,1] -> 0-100 display."""
    if raw is None:
        return None
    return round(min(max(raw, 0.0), 1.0) * 100, 1)


def recent_roi_pct(week_roi, mon_roi):
    """Weighted recent return-on-capital used by the dashboard ROI column."""
    parts = [(config.ROI_W_WEEK, week_roi), (config.ROI_W_MON, mon_roi)]
    w = sum(wt for wt, v in parts if v is not None)
    return (sum(wt * v for wt, v in parts if v is not None) / w * 100.0) if w else 0.0


def q1(db, sql, args=(), default=None):
    """First row, or default. Tolerates a missing table in an unmigrated DB."""
    try:
        return db.execute(sql, args).fetchone()
    except sqlite3.OperationalError:
        return default


def qall(db, sql, args=()):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def iso_epoch(s):
    if not s:
        return None
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None
