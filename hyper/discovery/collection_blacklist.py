"""Durable collection short-circuit for proven automated, uncopyable wallets.

This is deliberately narrower than profile rejection. An address enters only after a complete current
profile proves one of the stable automation signatures below with enough closed-Episode evidence. Future
Leaderboard scans subtract it before Portfolio/history API calls; raw discovery history can then be removed
without touching Paper/Live books, selections, controls or wallet lifecycle records.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from hyper.util import now_iso


PERMANENT_REASONS = frozenset({"bot_frequency", "hft_uncopyable", "grid_dca"})
MIN_TRADES = {
    "bot_frequency": 20,
    "hft_uncopyable": 10,
    "grid_dca": 5,
}


def normalize(addr: str | None) -> str:
    return str(addr or "").strip().lower()


def should_block(profile: dict | None) -> bool:
    """Return true only for complete, high-confidence whole-wallet automation evidence."""
    row = dict(profile or {})
    reason = str(row.get("reason") or "")
    if reason not in PERMANENT_REASONS:
        return False
    if str(row.get("data_status") or "valid") != "valid":
        return False
    n_trades = int(row.get("n_trades") or row.get("complete_episode_n") or 0)
    return n_trades >= MIN_TRADES[reason]


def evidence(profile: dict) -> dict:
    """Keep compact structural proof; never retain raw fills in the blacklist."""
    keys = (
        "n_trades", "median_eps", "median_hold_s", "complete_episode_n", "grid_episode_n",
        "max_adds_per_ep", "median_adds_per_ep", "heavy_orders_episode_n", "p90_orders_ep",
        "profile_generation", "evaluated_at",
    )
    return {key: profile.get(key) for key in keys if profile.get(key) is not None}


def record(db: sqlite3.Connection, profile: dict, *, stamp: str | None = None) -> bool:
    if not should_block(profile):
        return False
    addr = normalize(profile.get("addr"))
    if not addr:
        return False
    at = stamp or now_iso()
    db.execute(
        "INSERT INTO wallet_scan_blacklist"
        "(addr,reason,evidence_json,generation,created_at,updated_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(addr) DO UPDATE SET reason=excluded.reason,evidence_json=excluded.evidence_json,"
        "generation=excluded.generation,updated_at=excluded.updated_at",
        (
            addr,
            str(profile.get("reason")),
            json.dumps(evidence(profile), sort_keys=True, separators=(",", ":")),
            profile.get("profile_generation"),
            at,
            at,
        ),
    )
    return True


def active_map(db: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = db.execute(
            "SELECT addr,reason FROM wallet_scan_blacklist ORDER BY addr"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # Read paths must remain compatible with a database opened with only the legacy Discovery schema.
        # Production startup installs OBSERVE_SCHEMA before scanning, but repair tools and focused tests may
        # intentionally use the smaller schema. Treat an absent migration table as an empty blacklist.
        if "no such table" not in str(exc).lower():
            raise
        rows = ()
    return {
        normalize(addr): str(reason)
        for addr, reason in rows
        if normalize(addr)
    }


def reason_for(db: sqlite3.Connection, addr: str) -> str | None:
    try:
        row = db.execute(
            "SELECT reason FROM wallet_scan_blacklist WHERE addr=?",
            (normalize(addr),),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        row = None
    return str(row[0]) if row else None


def remove(db: sqlite3.Connection, addr: str) -> bool:
    """Explicit operator escape hatch; history is refetched normally on a later complete scan."""
    addr = normalize(addr)
    if len(addr) != 42 or not addr.startswith("0x"):
        raise ValueError("invalid_wallet_address")
    try:
        int(addr[2:], 16)
    except ValueError as exc:
        raise ValueError("invalid_wallet_address") from exc
    before = db.total_changes
    db.execute("DELETE FROM wallet_scan_blacklist WHERE addr=?", (addr,))
    return db.total_changes > before


def filter_addresses(db: sqlite3.Connection, addrs: Iterable[str]) -> tuple[list[str], dict[str, str]]:
    blocked = active_map(db)
    kept = []
    rejected = {}
    for raw in addrs:
        addr = normalize(raw)
        if not addr:
            continue
        if addr in blocked:
            rejected[addr] = blocked[addr]
        else:
            kept.append(addr)
    return list(dict.fromkeys(kept)), rejected


def purge_address(db: sqlite3.Connection, addr: str) -> dict[str, int]:
    """Delete only discovery history using indexed exact-address predicates."""
    addr = normalize(addr)
    deleted = {}
    for table in ("candidate_fills", "fill_cache_state", "episode"):
        before = db.total_changes
        db.execute(f"DELETE FROM {table} WHERE addr=?", (addr,))
        deleted[table] = db.total_changes - before
    return deleted


def bootstrap_from_profiles(db: sqlite3.Connection, *, stamp: str | None = None) -> int:
    """Seed legacy decisions from compact profile rows, never from raw fill-count heuristics."""
    cols = (
        "addr,reason,n_trades,median_eps,median_hold_s,profile_generation,evaluated_at,data_status"
    )
    names = cols.split(",")
    inserted = 0
    for values in db.execute(
        f"SELECT {cols} FROM profile WHERE reason IN ({','.join('?' for _ in PERMANENT_REASONS)})",
        tuple(sorted(PERMANENT_REASONS)),
    ).fetchall():
        row = dict(zip(names, values))
        if should_block(row) and record(db, row, stamp=stamp):
            inserted += 1
    return inserted


def purge_all(db: sqlite3.Connection, *, commit_every: int = 25) -> dict[str, int]:
    """Bound WAL growth with small committed batches during maintenance."""
    totals = {"wallets": 0, "candidate_fills": 0, "fill_cache_state": 0, "episode": 0}
    addrs = [row[0] for row in db.execute("SELECT addr FROM wallet_scan_blacklist ORDER BY addr")]
    for index, addr in enumerate(addrs, 1):
        deleted = purge_address(db, addr)
        totals["wallets"] += 1
        for key, value in deleted.items():
            totals[key] += int(value or 0)
        if index % max(1, int(commit_every)) == 0:
            db.commit()
    db.commit()
    return totals
