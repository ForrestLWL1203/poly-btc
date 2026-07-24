"""Explicit wallet lifecycle and published follow-selection helpers.

The selection generation is the single source of truth for new copy opens.  A
published generation may intentionally contain zero ``core`` wallets; callers
must distinguish that from a database which has never published a selection.
"""
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Optional, Tuple

from hyper.util import f, now_iso


CORE = "core"
CHALLENGER = "challenger"
EXIT_ONLY = "exit_only"
COOLDOWN = "cooldown"
REJECTED = "rejected"
QUARANTINE = "quarantine"

VALID_ROLES = {CORE, CHALLENGER, EXIT_ONLY, COOLDOWN, REJECTED, QUARANTINE, "qualified"}


def _columns(db, table: str):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(db, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def latest_published_generation(db) -> Optional[str]:
    """Return the deterministic current published generation, or ``None``.

    The current contract is ``published + complete + is_current``. Small schema
    feature checks let a rolling deployment read an earlier generation database.
    """
    if not _table_exists(db, "scan_generation"):
        return None
    cols = _columns(db, "scan_generation")
    if "generation" not in cols:
        return None

    where = []
    args = []
    if "status" in cols:
        where.append("status='published'")
    elif "published_at" in cols:
        where.append("published_at IS NOT NULL")
    else:
        return None
    if "complete" in cols:
        where.append("complete=1")
    if "is_current" in cols:
        where.append("is_current=1")

    order = []
    if "published_at" in cols:
        order.append("published_at DESC")
    if "id" in cols:
        order.append("id DESC")
    order.append("generation DESC")
    row = db.execute(
        "SELECT generation FROM scan_generation WHERE " + " AND ".join(where)
        + " ORDER BY " + ",".join(order) + " LIMIT 1",
        args,
    ).fetchone()
    return str(row[0]) if row else None


def pinned_core_controls(db, *, enabled_only: bool = False) -> list[dict]:
    """Return durable operator Core locks in their user-defined order.

    ``pinned_at`` is the authoritative order.  Address ordering is a deterministic rolling-migration
    fallback for databases whose old pin rows predate the timestamp column.
    """
    if not _table_exists(db, "target_controls"):
        return []
    cols = _columns(db, "target_controls")
    if not {"addr", "pinned"}.issubset(cols):
        return []
    enabled = "COALESCE(enabled,1)" if "enabled" in cols else "1"
    pinned_at = "pinned_at" if "pinned_at" in cols else "NULL"
    where = "COALESCE(pinned,0)=1"
    if enabled_only:
        where += f" AND {enabled}=1"
    rows = db.execute(
        f"SELECT lower(addr),{enabled},{pinned_at} FROM target_controls WHERE {where} "
        f"ORDER BY CASE WHEN {pinned_at} IS NULL THEN 1 ELSE 0 END,{pinned_at},lower(addr),addr"
    ).fetchall()
    return [
        {"addr": (row[0] or "").strip().lower(), "enabled": bool(row[1]), "pinnedAt": row[2]}
        for row in rows if (row[0] or "").strip()
    ]


def published_core_addrs(db, limit: Optional[int] = None) -> Optional[list]:
    """Load enabled Core addresses from the current published generation.

    ``None`` means no explicit selection has ever been published; execution must
    remain idle. ``[]`` means an explicit empty Core and is equally authoritative.
    """
    generation = latest_published_generation(db)
    if generation is None or not _table_exists(db, "follow_selection"):
        return None
    cols = _columns(db, "follow_selection")
    if not {"generation", "addr", "role"}.issubset(cols):
        return None

    enabled_sql = " AND COALESCE(enabled,1)=1" if "enabled" in cols else ""
    utility = "COALESCE(utility,-1e999) DESC," if "utility" in cols else ""
    rows = db.execute(
        "SELECT addr FROM follow_selection WHERE generation=? "
        "AND lower(role)='core'" + enabled_sql
        + f" ORDER BY {utility} lower(addr),addr",
        (generation,),
    ).fetchall()
    addrs = []
    seen = set()
    for row in rows:
        addr = (row[0] or "").strip().lower()
        if addr and addr not in seen:
            addrs.append(addr)
            seen.add(addr)

    # Current operator controls always win over the published snapshot.
    if addrs and _table_exists(db, "target_controls"):
        tc_cols = _columns(db, "target_controls")
        if {"addr", "enabled"}.issubset(tc_cols):
            marks = ",".join("?" for _ in addrs)
            disabled = {
                (r[0] or "").lower() for r in db.execute(
                    f"SELECT addr FROM target_controls WHERE enabled=0 AND lower(addr) IN ({marks})",
                    tuple(addrs),
                ).fetchall()
            }
            addrs = [a for a in addrs if a not in disabled]
    if limit is not None:
        addrs = addrs[:max(0, int(limit))]
    return addrs


def current_selection_rows(db) -> list:
    """Return the complete current selection snapshot for manual-mode carry-forward.

    A scan still publishes a fresh market-data generation in manual mode, but it
    must not silently replace the operator-owned Core/Challenger membership.
    Copying the rows into the new generation keeps the generation/selection
    atomicity contract while preserving that ownership boundary.
    """
    generation = latest_published_generation(db)
    if generation is None or not _table_exists(db, "follow_selection"):
        return []
    cols = _columns(db, "follow_selection")
    required = {"generation", "addr", "role"}
    if not required.issubset(cols):
        return []

    def expr(column: str, fallback: str) -> str:
        return column if column in cols else fallback

    rows = db.execute(
        "SELECT addr,role,"
        + expr("enabled", "1") + ","
        + expr("reason", "''") + ","
        + expr("utility", "NULL") + ","
        + expr("follow_score", "NULL") + ","
        + expr("selection_rank", "NULL") + ","
        + expr("data_status", "'valid'") + ","
        + expr("evidence_status", "''") + ","
        + expr("model_version", "''") + ","
        + expr("policy_version", "''")
        + "," + expr("acct_value", "NULL")
        + "," + expr("sector_policy_json", "NULL")
        + " FROM follow_selection WHERE generation=? ORDER BY addr",
        (generation,),
    ).fetchall()
    return [
        SelectionRow(
            addr=row[0], role=row[1], enabled=bool(row[2]), reason=row[3] or "",
            utility=row[4], follow_score=row[5], selection_rank=row[6], data_status=row[7] or "valid",
            evidence_status=row[8] or "", model_version=row[9] or "", policy_version=row[10] or "",
            acct_value=row[11], sector_policy_json=row[12],
        )
        for row in rows
    ]


@dataclass(frozen=True)
class SelectionRow:
    addr: str
    role: str
    enabled: bool = True
    reason: str = ""
    utility: Optional[float] = None
    follow_score: Optional[float] = None
    selection_rank: Optional[int] = None
    data_status: str = "valid"
    evidence_status: str = ""
    model_version: str = ""
    policy_version: str = ""
    acct_value: Optional[float] = None
    sector_policy_json: Optional[str] = None


def _coerce_selection_row(value) -> SelectionRow:
    if isinstance(value, SelectionRow):
        row = value
    elif isinstance(value, Mapping):
        row = SelectionRow(**value)
    else:
        raise TypeError("selection rows must be SelectionRow or mappings")
    addr = (row.addr or "").strip().lower()
    role = (row.role or "").strip().lower()
    if not addr:
        raise ValueError("selection address is required")
    if role not in VALID_ROLES:
        raise ValueError(f"unknown selection role: {role}")
    return replace(row, addr=addr, role=role)


def replace_selection_rows(db, generation: str, rows: Iterable, *, selected_at: Optional[str] = None) -> int:
    """Replace one generation's rows without committing.

    Callers can stage rows in a larger transaction.  Use :func:`publish_selection`
    for the normal atomic replace-and-publish operation.
    """
    if not _table_exists(db, "follow_selection"):
        raise RuntimeError("follow_selection table is unavailable")
    cols = _columns(db, "follow_selection")
    required = {"generation", "addr", "role"}
    if not required.issubset(cols):
        raise RuntimeError("follow_selection schema is incomplete")

    normalized = [_coerce_selection_row(r) for r in rows]
    if len({r.addr for r in normalized}) != len(normalized):
        raise ValueError("duplicate wallet in selection generation")
    normalized.sort(key=lambda r: (
        0 if r.role == CORE else 1,
        r.selection_rank if r.selection_rank is not None else 999999,
        -(r.follow_score or 0.0),
        r.addr,
    ))
    selected_at = selected_at or now_iso()

    field_values = (
        ("generation", lambda r: generation),
        ("addr", lambda r: r.addr),
        ("role", lambda r: r.role),
        ("enabled", lambda r: 1 if r.enabled else 0),
        ("reason", lambda r: r.reason),
        ("utility", lambda r: r.utility),
        ("follow_score", lambda r: r.follow_score),
        ("selection_rank", lambda r: r.selection_rank),
        ("data_status", lambda r: r.data_status),
        ("evidence_status", lambda r: r.evidence_status),
        ("model_version", lambda r: r.model_version),
        ("policy_version", lambda r: r.policy_version),
        ("acct_value", lambda r: r.acct_value),
        ("sector_policy_json", lambda r: r.sector_policy_json),
        ("selected_at", lambda r: selected_at),
    )
    fields = [(name, getter) for name, getter in field_values if name in cols]
    db.execute("DELETE FROM follow_selection WHERE generation=?", (generation,))
    if normalized:
        names = ",".join(name for name, _ in fields)
        marks = ",".join("?" for _ in fields)
        db.executemany(
            f"INSERT INTO follow_selection ({names}) VALUES ({marks})",
            [tuple(getter(row) for _, getter in fields) for row in normalized],
        )
    return len(normalized)


def publish_selection(db, generation: str, rows: Iterable, *, selected_at: Optional[str] = None):
    """Atomically replace selection rows and publish their ready generation."""
    from hyper.discovery.generation import publish_generation

    db.execute("SAVEPOINT publish_follow_selection")
    try:
        count = replace_selection_rows(db, generation, rows, selected_at=selected_at)
        result = publish_generation(db, generation, published_at=selected_at)
        db.execute("RELEASE SAVEPOINT publish_follow_selection")
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT publish_follow_selection")
        db.execute("RELEASE SAVEPOINT publish_follow_selection")
        raise
    return {"generation": generation, "selection_count": count, "published": result}


@dataclass(frozen=True)
class PortfolioMetrics:
    net_lcb: float
    stress_net_lcb: float
    liquidations: int
    actionable_open_rate: float
    capacity_fit: float
    max_drawdown: float
    peak_deploy_pct: float
    cost_drag_ratio: float
    net_pnl: Optional[float] = None
    stress_net_pnl: Optional[float] = None
    drawdown_dollars: Optional[float] = None
    risk_adjusted_utility: Optional[float] = None


@dataclass(frozen=True)
class SelectionConstraints:
    min_relative_lcb_improvement: float = 0.05
    min_actionable_open_rate: float = 0.70
    min_capacity_fit: float = 0.75
    max_deploy_pct: float = 0.80
    max_cost_drag_ratio: float = 0.25
    max_targets: int = 40
    max_actionable_open_rate_drop: float = 0.05
    max_capacity_fit_drop: float = 0.05
    min_absolute_net_gain: float = 0.0


@dataclass(frozen=True)
class MarginalSelectionResult:
    selected: Tuple[str, ...]
    baseline: PortfolioMetrics
    metrics: PortfolioMetrics
    action: str = "keep"
    added: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()
    evaluated: int = 0
    search_meta: Optional[Mapping[str, object]] = None


def portfolio_economic_rejection_reason(base: PortfolioMetrics, trial: PortfolioMetrics,
                                        c: SelectionConstraints) -> str:
    """Explain the actual-dollar net-profit and execution-fit rule used by production selection."""
    base_net = base.net_pnl if base.net_pnl is not None else base.net_lcb
    trial_net = trial.net_pnl if trial.net_pnl is not None else trial.net_lcb
    if trial_net <= 0 or trial_net <= base_net:
        return "portfolio_no_profit_improvement"
    relative_net_floor = base_net + abs(base_net) * max(
        0.0, c.min_relative_lcb_improvement,
    )
    if trial_net + 1e-12 < relative_net_floor:
        return "portfolio_gain_below_floor"
    if trial_net - base_net + 1e-12 < max(0.0, c.min_absolute_net_gain):
        return "portfolio_absolute_net_gain_low"
    if trial.actionable_open_rate < c.min_actionable_open_rate:
        return "portfolio_open_rate_low"
    if (base_net > 0 and trial.actionable_open_rate + c.max_actionable_open_rate_drop
            < base.actionable_open_rate):
        return "portfolio_open_rate_drop"
    if trial.capacity_fit < c.min_capacity_fit:
        return "portfolio_capacity_low"
    if (base_net > 0 and trial.capacity_fit + c.max_capacity_fit_drop < base.capacity_fit):
        return "portfolio_capacity_drop"
    if trial.peak_deploy_pct > c.max_deploy_pct:
        return "portfolio_deploy_limit"
    return "portfolio_not_selected"
