"""Explicit wallet lifecycle and published follow-selection helpers.

The selection generation is the single source of truth for new copy opens.  A
published generation may intentionally contain zero ``core`` wallets; callers
must distinguish that from a database which has never published a selection.
"""
from dataclasses import dataclass, replace
import time
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple

from .util import f, now_iso


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
        + expr("follow_score", "NULL") + ","
        + expr("selection_rank", "NULL") + ","
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
            utility=row[4], follow_score=row[5], selection_rank=row[6], data_status=row[7] or "valid",
            evidence_status=row[8] or "", model_version=row[9] or "", policy_version=row[10] or "",
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
    if e.consecutive_complete_good < policy.entry_complete_generations:
        reason = "entry_generation_confirmation"
    elif last_age is None or last_age > policy.entry_actionable_age_ms:
        reason = "entry_actionable_open_stale"
    elif e.oos_closed_n < policy.entry_oos_closes:
        reason = "entry_recent_copy_samples_low"
    elif e.positive_probability < policy.entry_positive_probability:
        reason = "entry_positive_probability_low"
    elif observed_ms < policy.challenger_observation_ms:
        reason = "entry_observation_pending"
    elif e.soft_bad:
        reason = e.soft_bad_reason
    else:
        reason = "entry_conditions_unmet"
    return LifecycleDecision(addr, previous, role, reason)


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
    net_pnl: Optional[float] = None
    stress_net_pnl: Optional[float] = None
    drawdown_dollars: Optional[float] = None
    risk_adjusted_utility: Optional[float] = None


@dataclass(frozen=True)
class SelectionConstraints:
    min_relative_lcb_improvement: float = 0.05
    min_actionable_open_rate: float = 0.70
    min_capacity_fit: float = 0.85
    max_drawdown_worsening: float = 0.01
    max_deploy_pct: float = 0.80
    max_cost_drag_ratio: float = 0.25
    max_targets: int = 40
    max_actionable_open_rate_drop: float = 0.05
    max_capacity_fit_drop: float = 0.05


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


class SmartCoreSearchTimeout(RuntimeError):
    """Raised before publication when the bounded Core search exhausts its time budget."""


def _portfolio_net(metrics: PortfolioMetrics) -> float:
    return f(metrics.net_pnl if metrics.net_pnl is not None else metrics.net_lcb)


def _portfolio_utility(metrics: PortfolioMetrics) -> float:
    return f(
        metrics.risk_adjusted_utility
        if metrics.risk_adjusted_utility is not None else _portfolio_net(metrics)
    )


def _smart_rank(item) -> tuple:
    addrs, metrics = item
    return (
        _portfolio_utility(metrics),
        _portfolio_net(metrics),
        -f(metrics.drawdown_dollars),
        f(metrics.capacity_fit),
        f(metrics.actionable_open_rate),
        tuple(addrs),
    )


def search_smart_core(candidates: Sequence[str],
                      evaluator: Callable[[Tuple[str, ...]], PortfolioMetrics],
                      constraints: SelectionConstraints = SelectionConstraints(),
                      *, seed_target: int = 6, beam_width: int = 6,
                      swap_passes: int = 1, max_replace_out: int = 2,
                      min_marginal_gain_ratio: float = 0.0,
                      time_budget_s: Optional[float] = None,
                      validation_limit: Optional[int] = None,
                      strict_order_width: int = 2,
                      strict_challenger_limit: int = 4,
                      strict_expansion_passes: int = 1,
                      validation_evaluator: Optional[
                          Callable[[Tuple[str, ...]], PortfolioMetrics]
                      ] = None) -> MarginalSelectionResult:
    """Find a compact Core by streaming shared-account replay results.

    Score has already done its job before this function: every input wallet is quality-qualified.  Membership
    is determined by portfolio economics under one neutral parameter surface.  The search keeps only a tiny
    beam of address tuples plus compact metrics, grows toward ``seed_target``, then continues one wallet at a
    time until the best next level no longer raises shared-account economics.  Local one-for-one and bounded
    one-for-two checks reduce greedy path dependence without enumerating every subset.  When an effective
    (strict price-path) evaluator is supplied, it chooses the winner and orders its wallets by strict
    incremental contribution; the anchor is a search depth, not a score-filled quota.
    """
    started = time.monotonic()
    deadline = (started + max(0.0, float(time_budget_s))) if time_budget_s else None
    ordered = tuple(dict.fromkeys((addr or "").lower() for addr in candidates if addr))
    ordered = ordered[:max(0, int(constraints.max_targets))]
    score_order = {addr: index for index, addr in enumerate(ordered)}
    width = max(1, int(beam_width))
    target = min(len(ordered), max(0, int(seed_target)))
    cache = {}
    validation_cache = {}
    strict_eval_seconds = 0.0

    def check_budget():
        if deadline is not None and time.monotonic() >= deadline:
            raise SmartCoreSearchTimeout("smart_core_search_time_budget")

    def evaluate(addrs):
        check_budget()
        key = tuple(sorted(addrs))
        if key not in cache:
            metrics = evaluator(key)
            if not isinstance(metrics, PortfolioMetrics):
                raise TypeError("portfolio evaluator must return PortfolioMetrics")
            cache[key] = metrics
        return cache[key]

    def validate_strict(addrs):
        nonlocal strict_eval_seconds
        if validation_evaluator is None:
            raise RuntimeError("strict evaluator is unavailable")
        check_budget()
        key = tuple(sorted(addrs))
        if key not in validation_cache:
            tick = time.monotonic()
            value = validation_evaluator(key)
            strict_eval_seconds += time.monotonic() - tick
            if not isinstance(value, PortfolioMetrics):
                raise TypeError("portfolio validation evaluator must return PortfolioMetrics")
            validation_cache[key] = value
        return validation_cache[key]

    def feasible(metrics):
        return (
            _portfolio_net(metrics) > 0
            and _portfolio_utility(metrics) > 0
            and f(metrics.actionable_open_rate) >= f(constraints.min_actionable_open_rate)
            and f(metrics.capacity_fit) >= f(constraints.min_capacity_fit)
            and f(metrics.peak_deploy_pct) <= f(constraints.max_deploy_pct)
        )

    def top(states):
        dedup = {}
        for addrs, metrics in states:
            prior = dedup.get(addrs)
            if prior is None or _smart_rank((addrs, metrics)) > _smart_rank((addrs, prior)):
                dedup[addrs] = metrics
        states = list(dedup.items())
        if len(states) <= width:
            return sorted(states, key=_smart_rank, reverse=True)
        # Preserve different paths instead of pruning only by current net profit. A high-quality or
        # low-drawdown pair can be temporarily behind yet form the best shared portfolio at anchor size.
        rankings = (
            sorted(states, key=_smart_rank, reverse=True),
            sorted(states, key=lambda item: (
                _portfolio_net(item[1]), _portfolio_utility(item[1]), item[0]
            ), reverse=True),
            sorted(states, key=lambda item: (
                -sum(score_order.get(addr, len(ordered)) for addr in item[0]),
                -max((score_order.get(addr, len(ordered)) for addr in item[0]), default=0),
                _portfolio_utility(item[1]), item[0],
            ), reverse=True),
        )
        kept, seen = [], set()
        cursor = [0] * len(rankings)
        while len(kept) < width:
            added = False
            for index, ranked in enumerate(rankings):
                while cursor[index] < len(ranked):
                    item = ranked[cursor[index]]
                    cursor[index] += 1
                    if item[0] in seen:
                        continue
                    seen.add(item[0])
                    kept.append(item)
                    added = True
                    break
                if len(kept) >= width:
                    break
            if not added:
                break
        return sorted(kept, key=_smart_rank, reverse=True)

    baseline = evaluate(())
    beam = [((), baseline)]
    levels = []
    portfolio_finalists = []
    stop_reason = "candidate_pool_exhausted"
    stopped_marginal = None

    # Build the small starting portfolio.  ``seed_target`` is a search target, never a quota: if no wallet
    # improves the previous level, the production Core may contain fewer wallets.
    while beam and len(beam[0][0]) < target:
        previous_best = max(_portfolio_utility(metrics) for _, metrics in beam)
        expansions = []
        for addrs, parent in beam:
            selected = set(addrs)
            for addr in ordered:
                if addr in selected:
                    continue
                trial_addrs = tuple(sorted(addrs + (addr,)))
                trial = evaluate(trial_addrs)
                if feasible(trial) and _portfolio_economic_passes(parent, trial, constraints):
                    expansions.append((trial_addrs, trial))
        next_beam = top(expansions)
        if not next_beam or _portfolio_utility(next_beam[0][1]) <= previous_best:
            stop_reason = "no_positive_seed_marginal"
            break
        beam = next_beam
        portfolio_finalists.extend(beam)
        levels.append({
            "size": len(beam[0][0]), "bestNet": _portfolio_net(beam[0][1]),
            "beam": len(beam),
        })

    def polish_one_for_one(states):
        current = top(states)
        for _ in range(max(0, int(swap_passes))):
            trials = list(current)
            improved = False
            for addrs, parent in current:
                selected = set(addrs)
                outside = [addr for addr in ordered if addr not in selected]
                for outgoing in addrs:
                    for incoming in outside:
                        trial_addrs = tuple(sorted((selected - {outgoing}) | {incoming}))
                        trial = evaluate(trial_addrs)
                        if feasible(trial) and _portfolio_economic_passes(parent, trial, constraints):
                            trials.append((trial_addrs, trial))
                            improved = True
            next_states = top(trials)
            if (not improved
                    or _portfolio_utility(next_states[0][1]) <= _portfolio_utility(current[0][1])):
                break
            current = next_states
        return current

    seed_complete = bool(beam and len(beam[0][0]) >= target)
    if beam and beam[0][0]:
        beam = polish_one_for_one(beam)
        portfolio_finalists.extend(beam)

    # Continue expanding beyond the seed until the best attainable next level stops adding dollars.
    while seed_complete and beam and len(beam[0][0]) < len(ordered):
        best_current = max(beam, key=_smart_rank)
        current_net = _portfolio_net(best_current[1])
        current_utility = _portfolio_utility(best_current[1])
        expansions = []
        rejected_marginals = []
        for addrs, parent in beam:
            selected = set(addrs)
            for addr in ordered:
                if addr in selected:
                    continue
                trial_addrs = tuple(sorted(addrs + (addr,)))
                trial = evaluate(trial_addrs)
                if feasible(trial) and _portfolio_economic_passes(parent, trial, constraints):
                    # A deeper path must beat the best portfolio at the current size, not merely a weak
                    # parent retained for diversity.  Otherwise cardinality can grow while economics stay
                    # flat or regress.
                    gain = _portfolio_net(trial) - current_net
                    required = abs(current_net) * max(0.0, f(min_marginal_gain_ratio))
                    if (_portfolio_utility(trial) > current_utility
                            and gain + 1e-12 >= required):
                        expansions.append((trial_addrs, trial))
                    else:
                        rejected_marginals.append((gain, required, current_net))
        next_beam = top(expansions)
        if not next_beam:
            if rejected_marginals:
                gain, required_gain, parent_net = max(
                    rejected_marginals, key=lambda item: (item[0] - item[1], item[0])
                )
                stop_reason = (
                    "no_positive_expansion_marginal"
                    if gain <= 0 else "expansion_marginal_gain_below_floor"
                )
                stopped_marginal = {
                    "gain": gain,
                    "required": required_gain,
                    "ratio": gain / abs(parent_net) if parent_net else 0.0,
                }
            else:
                stop_reason = "no_positive_expansion_marginal"
                stopped_marginal = {"gain": 0.0, "required": 0.0}
            break
        # Every retained expansion already beats the best current-size portfolio and its marginal floor.
        beam = next_beam
        portfolio_finalists.extend(beam)
        levels.append({
            "size": len(beam[0][0]), "bestNet": _portfolio_net(beam[0][1]),
            "beam": len(beam),
        })

    if beam and beam[0][0]:
        beam = polish_one_for_one(beam)
        portfolio_finalists.extend(beam)

    # A bounded one-for-two check may deliberately reduce Core count.  Only the six wallets with the
    # smallest leave-one-out contribution participate, keeping daily runtime predictable.
    if beam and max_replace_out >= 2 and len(beam[0][0]) >= 2:
        finalists = list(beam)
        for addrs, parent in beam:
            selected = set(addrs)
            outside = [addr for addr in ordered if addr not in selected]
            removal_rank = []
            for outgoing in addrs:
                without = tuple(sorted(selected - {outgoing}))
                removal_rank.append((_portfolio_net(parent) - _portfolio_net(evaluate(without)), outgoing))
            weak = [addr for _, addr in sorted(removal_rank)[:6]]
            for i, first in enumerate(weak):
                for second in weak[i + 1:]:
                    for incoming in outside:
                        trial_addrs = tuple(sorted((selected - {first, second}) | {incoming}))
                        trial = evaluate(trial_addrs)
                        if feasible(trial) and _portfolio_economic_passes(parent, trial, constraints):
                            finalists.append((trial_addrs, trial))
        beam = top(finalists)
        portfolio_finalists.extend(beam)

    selected, metrics = beam[0] if beam else ((), baseline)
    if validation_evaluator is not None and portfolio_finalists:
        dedup_finalists = {}
        for addrs, neutral_metrics in portfolio_finalists:
            dedup_finalists[addrs] = neutral_metrics
        finalist_items = list(dedup_finalists.items())
        limit = max(1, int(validation_limit)) if validation_limit else None
        if limit and len(finalist_items) > limit:
            # Reserve the anchor sizes plus the largest expansion before filling remaining slots globally.
            # Covering every cardinality can consume the entire budget before validating the best large set.
            by_size = {}
            for item in finalist_items:
                by_size.setdefault(len(item[0]), []).append(item)
            shortlisted, shortlisted_keys = [], set()
            required_sizes = [size for size in sorted(by_size) if size <= target]
            if by_size:
                required_sizes.append(max(by_size))
            for size in dict.fromkeys(required_sizes):
                item = max(by_size[size], key=_smart_rank)
                if item[0] not in shortlisted_keys and len(shortlisted) < limit:
                    shortlisted.append(item)
                    shortlisted_keys.add(item[0])
            for item in sorted(finalist_items, key=_smart_rank, reverse=True):
                if item[0] not in shortlisted_keys and len(shortlisted) < limit:
                    shortlisted.append(item)
                    shortlisted_keys.add(item[0])
            finalist_items = shortlisted
        validated = []
        for addrs, neutral_metrics in finalist_items:
            value = validate_strict(addrs)
            stress = f(
                value.stress_net_pnl
                if value.stress_net_pnl is not None else value.stress_net_lcb
            )
            if (feasible(value) and stress > 0
                    and _portfolio_economic_passes(baseline, value, constraints)):
                validated.append((addrs, value, neutral_metrics))
        if validated:
            validated.sort(key=lambda item: (
                _portfolio_utility(item[1]),
                _portfolio_net(item[1]),
                _portfolio_net(item[2]),
                -len(item[0]),
                item[0],
            ), reverse=True)
            selected, metrics, _ = validated[0]
            # Give the published list an economically meaningful 1..N order. At each position use the fast
            # shared-account replay to shortlist the strongest marginal paths, then let strict K-line replay
            # choose between them. After the anchor, apply the same profit floor and stop rather than force
            # a quota. This is deliberately good-enough local search, not exhaustive subset optimization.
            remaining = set(selected)
            contribution_order = []
            strict_current = validate_strict(())

            def strict_shortlist(pool, prefix, limit):
                pool = set(pool)
                limit = max(1, int(limit))
                if len(pool) <= limit:
                    return sorted(pool, key=lambda addr: (
                        score_order.get(addr, len(ordered)), addr,
                    ))
                fast_current = evaluate(tuple(sorted(prefix)))
                ranked = []
                for addr in pool:
                    fast_trial = evaluate(tuple(sorted((*prefix, addr))))
                    ranked.append((
                        _portfolio_utility(fast_trial) - _portfolio_utility(fast_current),
                        _portfolio_net(fast_trial) - _portfolio_net(fast_current),
                        -score_order.get(addr, len(ordered)),
                        addr,
                    ))
                ranked.sort(reverse=True)
                # Preserve the highest published-score wallet as a tie-break path, then fill by fast shared
                # account marginal value. Strict K-line replay still makes the actual admission decision.
                score_pick = min(pool, key=lambda addr: score_order.get(addr, len(ordered)))
                out = [score_pick]
                for _utility, _net, _score, addr in ranked:
                    if addr not in out:
                        out.append(addr)
                    if len(out) >= limit:
                        break
                return out

            while remaining:
                ranked_additions = []
                for addr in strict_shortlist(
                        remaining, contribution_order, strict_order_width):
                    trial_addrs = tuple(sorted((*contribution_order, addr)))
                    value = validate_strict(trial_addrs)
                    if not feasible(value) or not _portfolio_economic_passes(
                            strict_current, value, constraints):
                        continue
                    gain = _portfolio_net(value) - _portfolio_net(strict_current)
                    required = (
                        abs(_portfolio_net(strict_current))
                        * max(0.0, f(min_marginal_gain_ratio))
                        if len(contribution_order) >= target else 0.0
                    )
                    if gain + 1e-12 < required:
                        continue
                    ranked_additions.append((
                        _portfolio_utility(value) - _portfolio_utility(strict_current),
                        gain,
                        -score_order.get(addr, len(ordered)),
                        addr,
                        value,
                    ))
                if not ranked_additions:
                    stop_reason = (
                        "strict_expansion_stopped" if contribution_order else "no_valid_strict_anchor"
                    )
                    break
                _utility_gain, _net_gain, _score_tie, addr, value = max(ranked_additions)
                contribution_order.append(addr)
                remaining.remove(addr)
                strict_current = value
            # The beam only bounds combinatorial discovery.  Give every qualified wallet outside the
            # winning finalist a direct strict chance to join the resulting portfolio, then repeat after an
            # admission.  This prevents a high-quality Challenger from being stranded solely because its
            # earlier fills-only beam path was pruned.
            outside = {addr for addr in ordered if addr not in contribution_order}
            for _ in range(max(0, int(strict_expansion_passes))):
                if not outside or len(contribution_order) >= constraints.max_targets:
                    break
                ranked_additions = []
                for addr in strict_shortlist(
                        outside, contribution_order, strict_challenger_limit):
                    trial_addrs = tuple(sorted((*contribution_order, addr)))
                    value = validate_strict(trial_addrs)
                    if not feasible(value) or not _portfolio_economic_passes(
                            strict_current, value, constraints):
                        continue
                    gain = _portfolio_net(value) - _portfolio_net(strict_current)
                    required = (
                        abs(_portfolio_net(strict_current))
                        * max(0.0, f(min_marginal_gain_ratio))
                        if len(contribution_order) >= target else 0.0
                    )
                    if gain + 1e-12 < required:
                        continue
                    ranked_additions.append((
                        _portfolio_utility(value) - _portfolio_utility(strict_current),
                        gain,
                        -score_order.get(addr, len(ordered)),
                        addr,
                        value,
                    ))
                if not ranked_additions:
                    stop_reason = "strict_expansion_stopped"
                    break
                _utility_gain, _net_gain, _score_tie, addr, value = max(ranked_additions)
                contribution_order.append(addr)
                outside.remove(addr)
                strict_current = value
            selected = tuple(contribution_order)
            metrics = strict_current
        else:
            # A strict evaluator is fail-closed.  The caller can distinguish this from an intentionally
            # empty qualified pool and retain the last published Core.
            selected, metrics = (), baseline
    duration = time.monotonic() - started
    return MarginalSelectionResult(
        selected=selected,
        baseline=baseline,
        metrics=metrics,
        action="smart_search" if selected else "keep_empty",
        added=selected,
        evaluated=len(cache) + len(validation_cache),
        search_meta={
            "seedTarget": target,
            "beamWidth": width,
            "selectedCount": len(selected),
            "neutralSelectedCount": len(beam[0][0]) if beam else 0,
            "validatedFinalists": len(validation_cache),
            "neutralEvaluations": len(cache),
            "strictEvaluations": len(validation_cache),
            "strictEvalSec": round(strict_eval_seconds, 3),
            "strictOrderWidth": max(1, int(strict_order_width)),
            "strictChallengerLimit": max(1, int(strict_challenger_limit)),
            "strictExpansionPasses": max(0, int(strict_expansion_passes)),
            "strictValidationPassed": bool(selected) if validation_evaluator is not None else None,
            "contributionOrder": tuple(selected),
            "minMarginalGainRatio": max(0.0, f(min_marginal_gain_ratio)),
            "stoppedMarginal": stopped_marginal,
            "stopReason": stop_reason,
            "levels": tuple(levels),
            "durationSec": round(duration, 3),
        },
    )


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
    )


def portfolio_rejection_reason(base: PortfolioMetrics, trial: PortfolioMetrics,
                               c: SelectionConstraints) -> str:
    """Return the first concrete failed portfolio constraint for dashboard/audit use."""
    improvement = trial.net_lcb - base.net_lcb
    required = abs(base.net_lcb) * c.min_relative_lcb_improvement
    if trial.net_lcb <= 0 or improvement <= 0:
        return "portfolio_no_profit_improvement"
    if improvement + 1e-12 < required:
        return "portfolio_gain_below_floor"
    if trial.stress_net_lcb <= 0:
        return "portfolio_recent_stress_loss"
    if trial.liquidations > base.liquidations:
        return "portfolio_new_liquidation"
    if trial.actionable_open_rate < c.min_actionable_open_rate:
        return "portfolio_open_rate_low"
    if trial.capacity_fit < c.min_capacity_fit:
        return "portfolio_capacity_low"
    if trial.max_drawdown > base.max_drawdown + c.max_drawdown_worsening:
        return "portfolio_drawdown_worse"
    if trial.peak_deploy_pct > c.max_deploy_pct:
        return "portfolio_deploy_limit"
    if trial.cost_drag_ratio > c.max_cost_drag_ratio:
        return "portfolio_cost_drag_high"
    return "portfolio_not_selected"


def portfolio_economic_rejection_reason(base: PortfolioMetrics, trial: PortfolioMetrics,
                                        c: SelectionConstraints) -> str:
    """Explain the actual-dollar net/drawdown rule used by production selection."""
    base_net = base.net_pnl if base.net_pnl is not None else base.net_lcb
    trial_net = trial.net_pnl if trial.net_pnl is not None else trial.net_lcb
    base_utility = (
        base.risk_adjusted_utility if base.risk_adjusted_utility is not None else base.net_lcb
    )
    trial_utility = (
        trial.risk_adjusted_utility if trial.risk_adjusted_utility is not None else trial.net_lcb
    )
    if trial_net <= 0 or trial_net <= base_net:
        return "portfolio_no_profit_improvement"
    if trial_utility <= base_utility:
        return "portfolio_risk_adjusted_gain_low"
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


def _portfolio_economic_passes(base: PortfolioMetrics, trial: PortfolioMetrics,
                               c: SelectionConstraints) -> bool:
    return portfolio_economic_rejection_reason(base, trial, c) == "portfolio_not_selected"


def select_ranked_positive_core(candidates: Sequence[str],
                                evaluator: Callable[[Tuple[str, ...]], PortfolioMetrics],
                                constraints: SelectionConstraints = SelectionConstraints(),
                                *, initial_core: Sequence[str] = (),
                                score_by_addr: Optional[Mapping[str, float]] = None,
                                individual_net_by_addr: Optional[Mapping[str, float]] = None,
                                max_replace_out: int = 2) -> MarginalSelectionResult:
    """Challenge Core in score order with additions plus bounded 1-for-1/1-for-2 replacements.

    Liquidation losses are already debited from replay PnL and visible in the equity drawdown.  They are
    therefore measured economically instead of being counted as an automatic veto.  Candidate order is the
    published wallet-score order, but every addition or replacement must improve shared-account economics.
    This prevents incumbents from becoming permanent merely because they were selected earlier without
    turning an individually strong wallet into a forced portfolio member.

    A replacement is considered only when the incoming wallet has both a higher published score and a
    higher standalone replay net profit than every outgoing wallet.  This only bounds the expensive search;
    it never bypasses the shared-account improvement requirement.
    A 1-for-2 action may intentionally reduce the Core count when removing the two lowest dominated wallets
    already improves current portfolio economics.  There is no minimum target count.
    """
    initial = tuple(sorted(dict.fromkeys(a.lower() for a in initial_core if a)))[:constraints.max_targets]
    initial_set = set(initial)
    ordered = tuple(
        addr for addr in dict.fromkeys(a.lower() for a in candidates if a)
        if addr not in initial_set
    )[:constraints.max_targets]
    cache = {}

    def evaluate(addrs):
        key = tuple(sorted(addrs))
        if key not in cache:
            value = evaluator(key)
            if not isinstance(value, PortfolioMetrics):
                raise TypeError("portfolio evaluator must return PortfolioMetrics")
            cache[key] = value
        return cache[key]

    selected = initial
    baseline = evaluate(selected)
    current = baseline
    scores = {(addr or "").lower(): f(value) for addr, value in (score_by_addr or {}).items()}
    individual_nets = {
        (addr or "").lower(): f(value) for addr, value in (individual_net_by_addr or {}).items()
    }
    for addr in ordered:
        trials = []
        if len(selected) < constraints.max_targets:
            trial_addrs = tuple(sorted(selected + (addr,)))
            trial = evaluate(trial_addrs)
            if _portfolio_economic_passes(current, trial, constraints):
                trials.append((trial, trial_addrs, "add", ""))
        mandatory_replacement = None
        # Production supplies both maps from the same published profile/replay snapshot.  Without them we
        # still support cold-bootstrap additions, but do not guess which incumbents are lower quality.
        if addr in scores and addr in individual_nets:
            eligible_out = [
                outgoing for outgoing in selected
                if outgoing in scores and outgoing in individual_nets
                and scores[outgoing] < scores[addr]
                and individual_nets[outgoing] < individual_nets[addr]
            ]
            eligible_out.sort(key=lambda outgoing: (
                scores[outgoing], individual_nets[outgoing], outgoing,
            ))
            if eligible_out:
                outgoing = (eligible_out[0],)
                trial_addrs = tuple(sorted((set(selected) - set(outgoing)) | {addr}))
                mandatory_replacement = (
                    evaluate(trial_addrs), trial_addrs, "replace_1_ranked", outgoing,
                )
                if _portfolio_economic_passes(current, mandatory_replacement[0], constraints):
                    trials.append(mandatory_replacement)
            # Only the two lowest-scored dominated wallets are eligible for count reduction.  Searching
            # arbitrary pairs could evict stronger wallets merely because old parameters happened to favor
            # a historical event ordering.
            if min(max(0, int(max_replace_out)), len(eligible_out)) >= 2:
                outgoing = tuple(eligible_out[:2])
                trial_addrs = tuple(sorted((set(selected) - set(outgoing)) | {addr}))
                trial = evaluate(trial_addrs)
                if _portfolio_economic_passes(current, trial, constraints):
                    trials.append((trial, trial_addrs, "replace_2", outgoing))
        if not trials:
            continue
        # The candidate already earned priority by score order.  For that candidate choose the economically
        # strongest feasible action; deterministic address/action keys break exact replay ties.
        trials.sort(key=lambda item: (
            -f(item[0].risk_adjusted_utility),
            -f(item[0].net_pnl),
            f(item[0].max_drawdown),
            item[2],
            item[3],
        ))
        chosen = trials[0]
        current, selected = chosen[0], chosen[1]
    added = tuple(sorted(set(selected) - set(initial)))
    removed = tuple(sorted(set(initial) - set(selected)))
    if removed:
        action = "replace" if len(selected) == len(initial) else "rebalance"
    elif added:
        action = "add"
    else:
        action = "keep"
    return MarginalSelectionResult(
        selected=selected,
        baseline=baseline,
        metrics=current,
        action=action,
        added=added,
        removed=removed,
        evaluated=len(cache),
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


def select_bootstrap_core(challengers: Sequence[str],
                          evaluator: Callable[[Tuple[str, ...]], PortfolioMetrics],
                          constraints: SelectionConstraints = SelectionConstraints()) -> MarginalSelectionResult:
    """Greedily form the first Core from an empty account.

    Bootstrap repeatedly adds the best positive-marginal wallet until no
    remaining candidate clears every portfolio constraint.
    """
    return select_core_until_stable((), challengers, evaluator, constraints, action="bootstrap")


def select_core_until_stable(current_core: Sequence[str], challengers: Sequence[str],
                             evaluator: Callable[[Tuple[str, ...]], PortfolioMetrics],
                             constraints: SelectionConstraints = SelectionConstraints(),
                             *, action: str = "rebalance") -> MarginalSelectionResult:
    """Apply positive-marginal additions/replacements until the set is stable.

    Lifecycle confirmation and portfolio constraints already provide the
    membership hysteresis.  Artificially limiting a generation to one action
    only delays profitable, independently-qualified wallets and leaves a cold
    Paper account under-formed.
    """
    initial = tuple(sorted({a.lower() for a in current_core if a}))
    universe = tuple(sorted({a.lower() for a in challengers if a} | set(initial)))
    cache = {}

    def evaluate(addrs):
        key = tuple(sorted(addrs))
        if key not in cache:
            metrics = evaluator(key)
            if not isinstance(metrics, PortfolioMetrics):
                raise TypeError("portfolio evaluator must return PortfolioMetrics")
            cache[key] = metrics
        return cache[key]

    selected = initial
    baseline = evaluate(selected)
    current = baseline
    seen = {selected}
    max_steps = max(1, len(universe) * 2 + constraints.max_targets)
    for _ in range(max_steps):
        candidates = [addr for addr in universe if addr not in set(selected)]
        result = select_marginal_core(selected, candidates, evaluate, constraints)
        if result.action == "keep" or result.selected == selected or result.selected in seen:
            break
        selected, current = result.selected, result.metrics
        seen.add(selected)

    return MarginalSelectionResult(
        selected=selected,
        baseline=baseline,
        metrics=current,
        action=action if selected != initial else "keep",
        added=tuple(sorted(set(selected) - set(initial))),
        removed=tuple(sorted(set(initial) - set(selected))),
        evaluated=len(cache),
    )
