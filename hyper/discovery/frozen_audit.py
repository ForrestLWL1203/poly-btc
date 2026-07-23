"""Read-only, network-free pipeline waterfall reports for one frozen generation."""

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


def _mask(addr: str) -> str:
    return "wallet_" + hashlib.sha256(str(addr or "").lower().encode()).hexdigest()[:12]


_WALLET_RE = re.compile(r"0x[0-9a-fA-F]{3,40}")


def _redact(value):
    if isinstance(value, dict):
        return {_redact(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _WALLET_RE.sub(lambda match: _mask(match.group(0)), value)
    return value


def _read_only(path: str) -> sqlite3.Connection:
    return sqlite3.connect("file:" + Path(path).resolve().as_posix() + "?mode=ro", uri=True)


def _payload(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_report(path: str, report: dict) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pipeline-audit-", suffix=".json", dir=target.parent)
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


def build(db_path: str, report_path: str, *, generation: str | None = None,
          stamp: str | None = None) -> dict:
    """Build a deterministic report without migrations, network calls or database writes."""
    db = _read_only(db_path)
    try:
        if generation is None:
            row = db.execute(
                "SELECT generation FROM scan_generation WHERE complete=1 "
                "ORDER BY is_current DESC,id DESC LIMIT 1"
            ).fetchone()
            generation = row[0] if row else None
        if not generation:
            raise RuntimeError("no complete scan generation found")
        generation_row = db.execute(
            "SELECT started_at,published_at,status,metrics_json FROM scan_generation WHERE generation=?",
            (generation,),
        ).fetchone()
        if not generation_row:
            raise RuntimeError("generation not found")
        if stamp is None:
            row = db.execute(
                "SELECT stamp FROM pipeline_audit WHERE source='scan' AND stage='profile' "
                "AND created_at>=? ORDER BY id DESC LIMIT 1", (generation_row[0],),
            ).fetchone()
            stamp = row[0] if row else generation_row[0]

        wallets: dict[str, dict] = {}
        leaderboard_rows = db.execute(
            "SELECT addr,is_candidate,account_value,week_vlm,week_pnl,week_roi,mon_pnl,mon_roi "
            "FROM leaderboard_staging WHERE generation=? ORDER BY mon_roi DESC,addr", (generation,),
        ).fetchall()
        for addr, candidate, account, volume, week_pnl, week_roi, month_pnl, month_roi in leaderboard_rows:
            wallets[addr] = {
                "wallet": _mask(addr),
                "leaderboard": {
                    "passed": bool(candidate), "accountValue": account, "weekVolume": volume,
                    "weekPnl": week_pnl, "weekRoi": week_roi,
                    "monthPnl": month_pnl, "monthRoi": month_roi,
                },
            }
        for stage in ("official_roi", "perp_prefilter", "profile"):
            for addr, status, reason, raw in db.execute(
                "SELECT addr,status,reason,payload_json FROM pipeline_audit "
                "WHERE stamp=? AND source='scan' AND stage=? AND addr IS NOT NULL ORDER BY id",
                (stamp, stage),
            ).fetchall():
                item = wallets.setdefault(addr, {"wallet": _mask(addr)})
                item[stage] = {"status": status, "reason": reason, "evidence": _payload(raw)}
        for addr, role, reason, rank in db.execute(
            "SELECT addr,role,reason,selection_rank FROM follow_selection WHERE generation=?",
            (generation,),
        ).fetchall():
            wallets.setdefault(addr, {"wallet": _mask(addr)})["selection"] = {
                "role": role, "reason": reason, "rank": rank,
            }

        waterfall = Counter()
        first_failures = Counter()
        output_wallets = []
        for item in wallets.values():
            leaderboard = item.get("leaderboard") or {}
            if not leaderboard.get("passed"):
                first_stage = "leaderboard"
                reason = (item.get("official_roi") or {}).get("reason") or "leaderboard_rejected"
            elif (item.get("perp_prefilter") or {}).get("status") != "passed":
                first_stage = "perp_prefilter"
                reason = (item.get("perp_prefilter") or {}).get("reason") or "perp_evidence_missing"
            elif not item.get("profile"):
                first_stage, reason = "profile", "profile_not_evaluated"
            else:
                eligibility = ((item.get("profile") or {}).get("evidence") or {}).get("followEligibility") or {}
                if not eligibility.get("eligible"):
                    first_stage = "profile"
                    reason = eligibility.get("status") or (item.get("profile") or {}).get("reason")
                elif not eligibility.get("coreEligible"):
                    first_stage = "personal_core"
                    reason = eligibility.get("status") or "challenger"
                elif (item.get("selection") or {}).get("role") != "core":
                    first_stage = "portfolio"
                    reason = (item.get("selection") or {}).get("reason") or "portfolio_not_selected"
                else:
                    first_stage, reason = "core", "core"
            item["firstDecision"] = {"stage": first_stage, "reason": reason}
            waterfall[first_stage] += 1
            first_failures[f"{first_stage}:{reason}"] += 1
            output_wallets.append(item)

        report = {
            "kind": "hyper-frozen-pipeline-audit-v1",
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "readOnly": True, "networkUsed": False,
            "generation": {"id": generation, "status": generation_row[2],
                           "startedAt": generation_row[0], "publishedAt": generation_row[1]},
            "stamp": stamp,
            "funnel": {
                "leaderboard": len(leaderboard_rows),
                "coarseRecall": sum(bool(row[1]) for row in leaderboard_rows),
                "perpPrefilter": sum(
                    (item.get("perp_prefilter") or {}).get("status") == "passed"
                    for item in wallets.values()
                ),
                "profiled": sum(bool(item.get("profile")) for item in wallets.values()),
                "personalCore": sum(
                    bool((((item.get("profile") or {}).get("evidence") or {}).get("followEligibility") or {}).get("coreEligible"))
                    for item in wallets.values()
                ),
                "finalCore": sum((item.get("selection") or {}).get("role") == "core" for item in wallets.values()),
            },
            "firstDecisionCounts": dict(waterfall),
            "firstFailureReasons": dict(first_failures.most_common()),
            "wallets": sorted(output_wallets, key=lambda item: item["wallet"]),
        }
        report = _redact(report)
        _atomic_report(report_path, report)
        return report
    finally:
        db.close()
