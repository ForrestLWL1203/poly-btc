"""Explicit wallet lifecycle and published follow-selection helpers.

The selection generation is the single source of truth for new copy opens.  A
published generation may intentionally contain zero ``core`` wallets; callers
must distinguish that from a database which has never published a selection.
"""
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple

from .util import now_iso


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

    The strict vNext contract is ``published + complete + is_current``.  The
    small feature-detection fallback exists only so a rolling deploy can read a
    database created by an earlier vNext build.
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


def published_core_addrs(db, limit: Optional[int] = None) -> Optional[list]:
    """Load enabled Core addresses from the current published generation.

    ``None`` means no explicit selection has ever been published and permits
    the legacy watchlist fallback.  ``[]`` means an explicit empty Core and
    must *not* fall back.
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
        + expr("data_status", "'valid'") + ","
        + expr("evidence_status", "''") + ","
        + expr("model_version", "''") + ","
        + expr("policy_version", "''")
        + " FROM follow_selection WHERE generation=? ORDER BY addr",
        (generation,),
    ).fetchall()
    return [
        SelectionRow(
            addr=row[0], role=row[1], enabled=bool(row[2]), reason=row[3] or "",
            utility=row[4], data_status=row[5] or "valid", evidence_status=row[6] or "",
            model_version=row[7] or "", policy_version=row[8] or "",
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
    data_status: str = "valid"
    evidence_status: str = ""
    model_version: str = ""
    policy_version: str = ""


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
    normalized.sort(key=lambda r: (0 if r.role == CORE else 1, -(r.utility or 0.0), r.addr))
    selected_at = selected_at or now_iso()

    field_values = (
        ("generation", lambda r: generation),
        ("addr", lambda r: r.addr),
        ("role", lambda r: r.role),
        ("enabled", lambda r: 1 if r.enabled else 0),
        ("reason", lambda r: r.reason),
        ("utility", lambda r: r.utility),
        ("data_status", lambda r: r.data_status),
        ("evidence_status", lambda r: r.evidence_status),
        ("model_version", lambda r: r.model_version),
        ("policy_version", lambda r: r.policy_version),
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
    from .generation import publish_generation

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
class LifecyclePolicy:
    entry_complete_generations: int = 2
    entry_actionable_age_ms: int = 24 * 60 * 60 * 1000
    entry_oos_closes: int = 7
    entry_positive_probability: float = 0.70
    challenger_observation_ms: int = 24 * 60 * 60 * 1000
    keep_actionable_grace_ms: int = 72 * 60 * 60 * 1000
    soft_bad_generations: int = 2
    max_soft_membership_changes: int = 1


@dataclass(frozen=True)
class LifecycleEvidence:
    addr: str
    now_ms: int
    current_role: str = CHALLENGER
    data_status: str = "valid"
    consecutive_complete_good: int = 0
    consecutive_soft_bad: int = 0
    last_actionable_open_ms: Optional[int] = None
    oos_closed_n: int = 0
    positive_probability: float = 0.0
    challenger_since_ms: Optional[int] = None
    soft_bad: bool = False
    soft_bad_reason: str = "soft_bad"
    hard_exit: bool = False
    hard_exit_reason: str = "hard_exit"
    has_open_copy: bool = False


@dataclass(frozen=True)
class LifecycleDecision:
    addr: str
    previous_role: str
    role: str
    reason: str
    hard_change: bool = False
    soft_membership_change: bool = False


def decide_lifecycle(e: LifecycleEvidence, policy: LifecyclePolicy = LifecyclePolicy()) -> LifecycleDecision:
    """Apply entry/keep/exit rules for one fully evaluated wallet."""
    previous = (e.current_role or CHALLENGER).lower()
    addr = (e.addr or "").lower()
    was_core = previous == CORE

    if e.hard_exit:
        role = EXIT_ONLY if e.has_open_copy else REJECTED
        return LifecycleDecision(addr, previous, role, e.hard_exit_reason, True, was_core)

    if e.data_status != "valid":
        # A fetch/replay error is never entry evidence and never an immediate soft exit.
        role = CORE if was_core else (EXIT_ONLY if e.has_open_copy else QUARANTINE)
        return LifecycleDecision(addr, previous, role, "deferred_data_error")

    last_age = None if e.last_actionable_open_ms is None else max(0, e.now_ms - e.last_actionable_open_ms)
    inactive = last_age is None or last_age > policy.keep_actionable_grace_ms
    confirmed_soft_bad = (e.soft_bad or inactive) and e.consecutive_soft_bad >= policy.soft_bad_generations

    if was_core:
        if confirmed_soft_bad:
            role = EXIT_ONLY if e.has_open_copy else CHALLENGER
            reason = e.soft_bad_reason if e.soft_bad else "actionable_open_stale"
            return LifecycleDecision(addr, previous, role, reason, False, True)
        reason = "soft_bad_pending_confirmation" if (e.soft_bad or inactive) else "core_keep"
        return LifecycleDecision(addr, previous, CORE, reason)

    observed_ms = 0 if e.challenger_since_ms is None else max(0, e.now_ms - e.challenger_since_ms)
    entry_ok = (
        e.consecutive_complete_good >= policy.entry_complete_generations
        and last_age is not None and last_age <= policy.entry_actionable_age_ms
        and e.oos_closed_n >= policy.entry_oos_closes
        and e.positive_probability >= policy.entry_positive_probability
        and observed_ms >= policy.challenger_observation_ms
        and not e.soft_bad
    )
    if entry_ok:
        return LifecycleDecision(addr, previous, CORE, "core_entry", False, True)
    role = EXIT_ONLY if e.has_open_copy else CHALLENGER
    return LifecycleDecision(addr, previous, role, "challenger_evidence")


def decide_lifecycles(evidence: Iterable[LifecycleEvidence],
                      policy: LifecyclePolicy = LifecyclePolicy()) -> list:
    """Decide a generation and allow at most one non-emergency Core change.

    Confirmed Core removals are considered before entries; hard exits are never
    rate-limited.  Deferred soft decisions retain their previous membership.
    """
    decisions = [decide_lifecycle(e, policy) for e in evidence]
    budget = max(0, policy.max_soft_membership_changes)
    soft = [d for d in decisions if d.soft_membership_change and not d.hard_change]
    soft.sort(key=lambda d: (0 if d.previous_role == CORE else 1, d.addr))
    allowed = {id(d) for d in soft[:budget]}
    result = []
    for d in decisions:
        if d.soft_membership_change and not d.hard_change and id(d) not in allowed:
            result.append(replace(d, role=d.previous_role, reason="soft_change_budget", soft_membership_change=False))
        else:
            result.append(d)
    return sorted(result, key=lambda d: d.addr)


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
    poll_latency_degradation: float = 0.0


@dataclass(frozen=True)
class SelectionConstraints:
    min_relative_lcb_improvement: float = 0.05
    min_actionable_open_rate: float = 0.70
    min_capacity_fit: float = 0.85
    max_drawdown_worsening: float = 0.01
    max_deploy_pct: float = 0.80
    max_cost_drag_ratio: float = 0.25
    max_poll_latency_degradation: float = 0.10
    max_targets: int = 40


@dataclass(frozen=True)
class MarginalSelectionResult:
    selected: Tuple[str, ...]
    baseline: PortfolioMetrics
    metrics: PortfolioMetrics
    action: str = "keep"
    added: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()
    evaluated: int = 0


def _portfolio_passes(base: PortfolioMetrics, trial: PortfolioMetrics,
                      c: SelectionConstraints) -> bool:
    improvement = trial.net_lcb - base.net_lcb
    required = abs(base.net_lcb) * c.min_relative_lcb_improvement
    return (
        trial.net_lcb > 0
        and improvement > 0
        and improvement + 1e-12 >= required
        and trial.stress_net_lcb > 0
        and trial.liquidations <= base.liquidations
        and trial.actionable_open_rate >= c.min_actionable_open_rate
        and trial.capacity_fit >= c.min_capacity_fit
        and trial.max_drawdown <= base.max_drawdown + c.max_drawdown_worsening
        and trial.peak_deploy_pct <= c.max_deploy_pct
        and trial.cost_drag_ratio <= c.max_cost_drag_ratio
        and trial.poll_latency_degradation <= c.max_poll_latency_degradation
    )


def select_marginal_core(current_core: Sequence[str], challengers: Sequence[str],
                         evaluator: Callable[[Tuple[str, ...]], PortfolioMetrics],
                         constraints: SelectionConstraints = SelectionConstraints()) -> MarginalSelectionResult:
    """Choose at most one deterministic marginal add or one-for-one replacement.

    There is deliberately no minimum wallet count.  The evaluator must replay
    the supplied addresses through one shared account and return OOS/stress
    metrics.  If no trial clears every constraint, the current set is retained.
    """
    core = tuple(sorted({a.lower() for a in current_core if a}))
    if len(core) > constraints.max_targets:
        raise ValueError("current Core exceeds MAX_TARGETS")
    candidates = tuple(sorted({a.lower() for a in challengers if a and a.lower() not in core}))
    cache = {}

    def evaluate(addrs):
        key = tuple(sorted(addrs))
        if key not in cache:
            metrics = evaluator(key)
            if not isinstance(metrics, PortfolioMetrics):
                raise TypeError("portfolio evaluator must return PortfolioMetrics")
            cache[key] = metrics
        return cache[key]

    baseline = evaluate(core)
    trials = []
    if len(core) < constraints.max_targets:
        for addr in candidates:
            selected = tuple(sorted(core + (addr,)))
            metrics = evaluate(selected)
            if _portfolio_passes(baseline, metrics, constraints):
                trials.append((selected, metrics, "add", (addr,), ()))
    for incoming in candidates:
        for outgoing in core:
            selected = tuple(sorted((set(core) - {outgoing}) | {incoming}))
            metrics = evaluate(selected)
            if _portfolio_passes(baseline, metrics, constraints):
                trials.append((selected, metrics, "replace", (incoming,), (outgoing,)))

    if not trials:
        return MarginalSelectionResult(core, baseline, baseline, evaluated=len(cache))
    trials.sort(key=lambda x: (-x[1].net_lcb, x[1].max_drawdown, x[2], x[0]))
    selected, metrics, action, added, removed = trials[0]
    return MarginalSelectionResult(selected, baseline, metrics, action, added, removed, len(cache))
