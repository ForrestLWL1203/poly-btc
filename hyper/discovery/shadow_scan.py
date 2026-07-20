"""Isolated production-data discovery acceptance scan."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from hyper import params, storage
from hyper.market import rest
from . import scanner


def _mask(addr: str) -> str:
    return "wallet_" + hashlib.sha256(str(addr or "").lower().encode()).hexdigest()[:12]


_WALLET_RE = re.compile(r"0x[0-9a-fA-F]{40}")


def _redact_wallets(value):
    """Recursively replace wallet addresses, including addresses nested in metrics diagnostics."""
    if isinstance(value, dict):
        return {
            _redact_wallets(key) if isinstance(key, str) else key: _redact_wallets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_wallets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_wallets(item) for item in value)
    if isinstance(value, str):
        return _WALLET_RE.sub(lambda match: _mask(match.group(0)), value)
    return value


def _source_control_state(db: sqlite3.Connection) -> dict:
    def scalar(sql):
        try:
            row = db.execute(sql).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            return None
    try:
        services = dict(db.execute("SELECT name,state FROM process_status ORDER BY name").fetchall())
    except sqlite3.Error:
        services = {}
    return {
        "publishedGeneration": scalar(
            "SELECT generation FROM scan_generation WHERE is_current=1 AND status='published' LIMIT 1"
        ),
        "strategyRevision": scalar(
            "SELECT revision FROM active_strategy_revision WHERE id=1"
        ),
        "pendingCommands": scalar("SELECT COUNT(*) FROM commands WHERE status IN ('pending','acked')"),
        "serviceStates": services,
    }


def _read_only(path: str) -> sqlite3.Connection:
    uri = "file:" + Path(path).resolve().as_posix() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30)


def _atomic_report(path: str, report: dict) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".shadow-report-", suffix=".json", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _audit_reasons(db, stamp, stage):
    counts = Counter()
    for reason, payload in db.execute(
        "SELECT COALESCE(reason,'unknown'),payload_json FROM pipeline_audit WHERE stamp=? AND stage=?",
        (stamp, stage),
    ).fetchall():
        try:
            count = int((json.loads(payload or "{}") or {}).get("count", 1))
        except (TypeError, ValueError):
            count = 1
        counts[reason] += count
    return dict(counts.most_common())


def _build_report(db, *, started_at, duration_s, source_before, source_after):
    generation = db.execute(
        "SELECT generation,status,started_at,published_at,metrics_json FROM scan_generation "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not generation:
        raise RuntimeError("shadow scan created no generation")
    generation_id, generation_status, generation_started_at, published_at, metrics_json = generation
    audit_row = db.execute(
        "SELECT stamp FROM pipeline_audit WHERE source='scan' AND stage='official_roi' "
        "AND created_at>=? ORDER BY id DESC LIMIT 1", (generation_started_at,),
    ).fetchone()
    stamp = audit_row[0] if audit_row else generation_started_at
    metrics = _redact_wallets(json.loads(metrics_json or "{}"))
    funnel = {
        "leaderboard": db.execute(
            "SELECT COUNT(*) FROM leaderboard_staging WHERE generation=?", (generation_id,)
        ).fetchone()[0],
        "officialRoi": db.execute(
            "SELECT COUNT(*) FROM leaderboard_staging WHERE generation=? AND is_candidate=1",
            (generation_id,),
        ).fetchone()[0],
        "perpPrefilter": metrics.get("perpPrefilterPassed", 0),
        "profiled": metrics.get("profileValid", 0) + metrics.get("profileDeferred", 0),
    }
    roles = dict(db.execute(
        "SELECT role,COUNT(*) FROM follow_selection WHERE generation=? GROUP BY role", (generation_id,)
    ).fetchall())
    funnel.update(core=roles.get("core", 0), challenger=roles.get("challenger", 0),
                  exitOnly=roles.get("exit_only", 0))
    perp_payload = {}
    for addr, payload in db.execute(
        "SELECT addr,payload_json FROM pipeline_audit WHERE stamp=? AND stage='perp_prefilter'",
        (stamp,),
    ).fetchall():
        try:
            perp_payload[addr] = json.loads(payload or "{}")
        except (TypeError, ValueError):
            perp_payload[addr] = {}
    core_rows = []
    cur = db.execute(
        "SELECT fs.addr,lb.week_roi,lb.mon_roi,lb.all_roi,lb.week_pnl,lb.mon_pnl,lb.all_pnl,"
        "p.market_type,p.copy_expected_return,p.copy_recent_return_14d,p.copy_recent_return_7d,"
        "p.oos_max_drawdown,p.copy_risk_score,p.copy_bt_closed_n,p.copy_bt_14d_closed_n,p.copy_bt_7d_closed_n,"
        "p.last_copyable_open_ms,p.sector_policy_json FROM follow_selection fs "
        "JOIN leaderboard_staging lb ON lb.generation=fs.generation AND lb.addr=fs.addr "
        "JOIN profile p ON p.addr=fs.addr WHERE fs.generation=? AND fs.role='core' ORDER BY fs.selection_rank",
        (generation_id,),
    )
    for row in cur.fetchall():
        (addr, week_roi, month_roi, all_roi, week_pnl, month_pnl, all_pnl, market_type,
         return30, return14, return7, max_dd, risk_score, closed30, closed14, closed7, last_open, sector_json) = row
        windows = (perp_payload.get(addr) or {}).get("windows") or {}
        core_rows.append({
            "wallet": _mask(addr),
            "official": {"roi": {"7d": week_roi, "30d": month_roi, "all": all_roi},
                         "pnl": {"7d": week_pnl, "30d": month_pnl, "all": all_pnl}},
            "perpShare": {key: value.get("perpShare") for key, value in windows.items()},
            "marketType": market_type,
            "allowedMarkets": (json.loads(sector_json or "{}").get("allowed") if sector_json else []),
            "copyReturn": {"7d": return7, "14d": return14, "30d": return30},
            "maxDrawdown": max_dd if max_dd is not None else (
                max(0.0, 1.0 - float(risk_score)) if risk_score is not None else None
            ),
            "sample": {"7d": closed7, "14d": closed14, "30d": closed30},
            "lastOpenMs": last_open,
        })
    previous = {}
    try:
        old_gen = source_before.get("publishedGeneration")
        if old_gen:
            # The source selection was included in the online backup.
            previous = dict(db.execute(
                "SELECT addr,role FROM follow_selection WHERE generation=?", (old_gen,)
            ).fetchall())
    except sqlite3.Error:
        previous = {}
    proposed = dict(db.execute(
        "SELECT addr,role FROM follow_selection WHERE generation=?", (generation_id,)
    ).fetchall())
    transitions = Counter(
        f"{previous.get(addr, 'none')}->{proposed.get(addr, 'none')}"
        for addr in set(previous) | set(proposed)
    )
    return {
        "kind": "hyper-shadow-scan-v1",
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sourceUnchanged": source_before == source_after,
        "sourceControlBefore": source_before,
        "sourceControlAfter": source_after,
        "generation": {"id": generation_id, "status": generation_status, "publishedAt": published_at},
        "acceptance": {
            "valid": generation_status == "published",
            "coreFound": roles.get("core", 0),
            "note": "Zero Core is valid when every candidate fails an existing Copy or portfolio gate.",
        },
        "funnel": funnel,
        "roles": {"core": roles.get("core", 0), "challenger": roles.get("challenger", 0),
                  "exitOnly": roles.get("exit_only", 0)},
        "rejections": {
            "officialRoi": _audit_reasons(db, stamp, "official_roi"),
            "perpPrefilter": _audit_reasons(db, stamp, "perp_prefilter"),
            "profile": _audit_reasons(db, stamp, "profile"),
        },
        "roleTransitions": dict(sorted(transitions.items())),
        "proposedCore": core_rows,
        "runtime": {"startedAt": started_at, "durationSec": round(duration_s, 3),
                    "api": rest.request_stats(), "generationMetrics": metrics},
    }


def run(source_db: str, report_path: str, scan_args) -> dict:
    """Back up source online, scan only the private temporary DB, then destroy it."""
    source = _read_only(source_db)
    source_before = _source_control_state(source)
    fd, temp_path = tempfile.mkstemp(prefix="hyper-shadow-", suffix=".db")
    os.close(fd)
    os.chmod(temp_path, 0o600)
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    t0 = time.time()
    try:
        target = sqlite3.connect(temp_path)
        source.backup(target)
        target.close()
        source.close()
        shadow = storage.connect(temp_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        params.seed_params(shadow)
        shadow.execute("DELETE FROM commands WHERE status IN ('pending','acked')")
        shadow.commit()
        scan_args.full_scan = True
        scan_args.no_harvest = False
        params.apply_scanner_params(shadow, scan_args)
        scanner.scan(shadow, scan_args)
        shadow.close()
        source_check = _read_only(source_db)
        source_after = _source_control_state(source_check)
        source_check.close()
        shadow = sqlite3.connect(temp_path)
        report = _build_report(
            shadow, started_at=started, duration_s=time.time() - t0,
            source_before=source_before, source_after=source_after,
        )
        shadow.close()
        if not report["sourceUnchanged"]:
            raise RuntimeError("source control state changed during shadow scan")
        _atomic_report(report_path, report)
        return report
    finally:
        try:
            source.close()
        except sqlite3.Error:
            pass
        for suffix in ("", "-wal", "-shm"):
            candidate = temp_path + suffix
            if os.path.exists(candidate):
                os.unlink(candidate)
