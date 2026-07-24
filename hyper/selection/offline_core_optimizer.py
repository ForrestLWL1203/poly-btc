"""Pure joint Core membership optimizer used by production and the offline lab.

This module deliberately has no SQLite or process-control dependencies. Callers
provide a cheap evaluator and an expensive strict evaluator, so the same finite
search and robustness rules can be tested locally, run read-only against a VPS
snapshot, and invoked by the generation publisher without duplicating policy.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from hyper.util import f
from . import state as selection


Evaluator = Callable[[tuple[str, ...]], selection.PortfolioMetrics]
FoldEvaluator = Callable[[tuple[str, ...], int, int, float], selection.PortfolioMetrics]


def _key(addrs: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys((addr or "").lower() for addr in addrs if addr)))


def _net(metrics: selection.PortfolioMetrics) -> float:
    return f(metrics.net_pnl if metrics.net_pnl is not None else metrics.net_lcb)


def _utility(metrics: selection.PortfolioMetrics) -> float:
    return _net(metrics)


def conservative_metrics(first: selection.PortfolioMetrics,
                         second: selection.PortfolioMetrics) -> selection.PortfolioMetrics:
    """Combine two replay path policies component-wise without optimistic mixing."""
    net = min(_net(first), _net(second))
    stress = min(f(first.stress_net_pnl), f(second.stress_net_pnl))
    drawdown = max(f(first.drawdown_dollars), f(second.drawdown_dollars))
    utility = net
    return selection.PortfolioMetrics(
        net_lcb=net,
        stress_net_lcb=stress,
        liquidations=max(int(first.liquidations), int(second.liquidations)),
        actionable_open_rate=min(
            f(first.actionable_open_rate), f(second.actionable_open_rate)
        ),
        capacity_fit=min(f(first.capacity_fit), f(second.capacity_fit)),
        max_drawdown=max(f(first.max_drawdown), f(second.max_drawdown)),
        peak_deploy_pct=max(f(first.peak_deploy_pct), f(second.peak_deploy_pct)),
        cost_drag_ratio=max(f(first.cost_drag_ratio), f(second.cost_drag_ratio)),
        net_pnl=net,
        stress_net_pnl=stress,
        drawdown_dollars=drawdown,
        risk_adjusted_utility=utility,
    )


def _rank(item: tuple[tuple[str, ...], selection.PortfolioMetrics]) -> tuple:
    addrs, metrics = item
    return (
        _net(metrics), f(metrics.stress_net_pnl), f(metrics.capacity_fit),
        f(metrics.actionable_open_rate), -len(addrs), addrs,
    )


def _feasible(metrics: selection.PortfolioMetrics,
              constraints: selection.SelectionConstraints) -> bool:
    return (
        _net(metrics) > 0
        and f(metrics.stress_net_pnl) > 0
        and f(metrics.actionable_open_rate) >= f(constraints.min_actionable_open_rate)
        and f(metrics.capacity_fit) >= f(constraints.min_capacity_fit)
        and f(metrics.peak_deploy_pct) <= f(constraints.max_deploy_pct)
    )


def _improves(base: selection.PortfolioMetrics, trial: selection.PortfolioMetrics,
              constraints: selection.SelectionConstraints,
              min_utility_gain: float = 0.0) -> bool:
    return (
        _feasible(trial, constraints)
        and selection.portfolio_economic_rejection_reason(base, trial, constraints)
        == "portfolio_not_selected"
        and _utility(trial) > _utility(base) + max(0.0, f(min_utility_gain))
    )


@dataclass(frozen=True)
class OfflineSearchConfig:
    finalist_limit: int = 12
    strict_move_shortlist: int = 8
    max_strict_moves: int = 16
    pair_add_limit: int = 6
    time_budget_s: float = 0.0
    min_utility_gain: float = 0.0


@dataclass(frozen=True)
class SearchStep:
    phase: str
    action: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    net: float
    utility: float


@dataclass
class OfflineSearchResult:
    selected: tuple[str, ...]
    metrics: selection.PortfolioMetrics
    initial: tuple[str, ...]
    initial_metrics: selection.PortfolioMetrics
    fast_evaluated: int
    strict_evaluated: int
    finalists: tuple[tuple[str, ...], ...] = ()
    steps: list[SearchStep] = field(default_factory=list)
    timed_out: bool = False


@dataclass(frozen=True)
class RobustComparison:
    eligible: bool
    reasons: tuple[str, ...]
    fold_wins: int
    fold_deltas: tuple[float, ...]
    fold_total_gain: float
    cost_stress_gain: float


@dataclass
class RobustSelectionResult:
    selected: tuple[str, ...]
    metrics: selection.PortfolioMetrics
    comparison: Optional[RobustComparison]
    evaluated: int
    audit: list[dict] = field(default_factory=list)


def robust_improvement(
        base: selection.PortfolioMetrics,
        trial: selection.PortfolioMetrics,
        base_folds: Sequence[selection.PortfolioMetrics],
        trial_folds: Sequence[selection.PortfolioMetrics],
        base_cost_stress: selection.PortfolioMetrics,
        trial_cost_stress: selection.PortfolioMetrics,
        constraints: selection.SelectionConstraints,
        *, min_fold_wins: int = 2,
        min_total_gain_ratio: float = .01) -> RobustComparison:
    """Validate a membership move on continuous and non-overlapping paths.

    The continuous 30-day replay captures cross-boundary positions and funding
    contention.  Non-overlapping folds catch recent-regime overfit.  Both must
    pass; a profitable reset-at-each-fold simulation cannot override a losing
    continuous shared-account path.
    """
    reasons = []
    if not _improves(base, trial, constraints):
        reasons.append("continuous_portfolio_not_improved")
    if len(base_folds) != len(trial_folds) or not trial_folds:
        reasons.append("folds_unavailable")
        deltas: tuple[float, ...] = ()
    else:
        deltas = tuple(
            _net(after) - _net(before) for before, after in zip(base_folds, trial_folds)
        )
        if any(not _feasible(metrics, constraints) for metrics in trial_folds):
            reasons.append("fold_infeasible")
        if sum(delta > 0 for delta in deltas) < max(1, int(min_fold_wins)):
            reasons.append("fewer_than_required_fold_wins")
        base_total = sum(_net(metrics) for metrics in base_folds)
        required = abs(base_total) * max(0.0, f(min_total_gain_ratio))
        if sum(deltas) + 1e-12 < required:
            reasons.append("fold_total_gain_below_floor")
        if deltas[-1] < 0:
            reasons.append("holdout_not_better")
    cost_gain = _net(trial_cost_stress) - _net(base_cost_stress)
    if _net(trial_cost_stress) <= 0 or cost_gain <= 0:
        reasons.append("cost_stress_not_better")
    # Equal stress liquidations are allowed for membership: they pre-existed in
    # the baseline and were not introduced by the new wallets.
    if trial_cost_stress.liquidations > base_cost_stress.liquidations:
        reasons.append("cost_stress_new_liquidation")
    return RobustComparison(
        eligible=not reasons,
        reasons=tuple(reasons),
        fold_wins=sum(delta > 0 for delta in deltas),
        fold_deltas=deltas,
        fold_total_gain=sum(deltas),
        cost_stress_gain=cost_gain,
    )


def choose_robust_candidate(
        current_core: Sequence[str], candidates: Sequence[str],
        discovered_states: Sequence[Sequence[str]], strict_evaluator: Evaluator,
        fold_evaluator: FoldEvaluator, constraints: selection.SelectionConstraints,
        *, finalist_limit: int = 12) -> RobustSelectionResult:
    """Choose the best continuous winner that also survives independent folds.

    One- and two-wallet additions around the current Core are always represented,
    so a lower-ranked but complementary pair cannot disappear merely because the
    30-day in-sample winner dominated the strict shortlist.
    """
    current = _key(current_core)
    candidates = _key(candidates)
    states = {_key(state) for state in discovered_states if state}
    states.add(current)
    outside = [addr for addr in candidates if addr not in set(current)]
    states.update(_key((*current, addr)) for addr in outside)
    states.update(
        _key((*current, outside[i], outside[j]))
        for i in range(len(outside)) for j in range(i + 1, len(outside))
    )
    strict = _CachedEvaluator(strict_evaluator)
    baseline = strict(current)
    ranked = []
    for state in states:
        metrics = strict(state)
        if state != current and _improves(baseline, metrics, constraints):
            ranked.append((state, metrics))
    ranked.sort(key=_rank, reverse=True)

    base_folds = [fold_evaluator(current, older, newer, 1.0)
                  for older, newer in ((30, 20), (20, 10), (10, 0))]
    base_stress = fold_evaluator(current, 10, 0, 1.5)
    passed = []
    audit = []
    for state, metrics in ranked[:max(1, int(finalist_limit))]:
        trial_folds = [fold_evaluator(state, older, newer, 1.0)
                       for older, newer in ((30, 20), (20, 10), (10, 0))]
        trial_stress = fold_evaluator(state, 10, 0, 1.5)
        comparison = robust_improvement(
            baseline, metrics, base_folds, trial_folds, base_stress, trial_stress,
            constraints,
        )
        audit.append({
            "addrs": state,
            "eligible": comparison.eligible,
            "reasons": comparison.reasons,
            "foldWins": comparison.fold_wins,
            "foldDeltas": comparison.fold_deltas,
            "foldTotalGain": comparison.fold_total_gain,
            "costStressGain": comparison.cost_stress_gain,
            "net": _net(metrics),
            "utility": _utility(metrics),
        })
        if comparison.eligible:
            passed.append((state, metrics, comparison))
    if passed:
        selected, metrics, comparison = max(
            passed, key=lambda item: _rank((item[0], item[1])),
        )
    else:
        selected, metrics, comparison = current, baseline, None
    return RobustSelectionResult(
        selected=selected, metrics=metrics, comparison=comparison,
        evaluated=len(strict.cache), audit=audit,
    )


class _CachedEvaluator:
    def __init__(self, evaluator: Evaluator):
        self.evaluator = evaluator
        self.cache: dict[tuple[str, ...], selection.PortfolioMetrics] = {}

    def __call__(self, addrs: Iterable[str]) -> selection.PortfolioMetrics:
        key = _key(addrs)
        if key not in self.cache:
            value = self.evaluator(key)
            if not isinstance(value, selection.PortfolioMetrics):
                raise TypeError("offline evaluator must return PortfolioMetrics")
            self.cache[key] = value
        return self.cache[key]


def _forward_path(candidates: tuple[str, ...], evaluate: _CachedEvaluator,
                  constraints: selection.SelectionConstraints,
                  states: set[tuple[str, ...]]) -> None:
    current: tuple[str, ...] = ()
    current_metrics = evaluate(current)
    states.add(current)
    while len(current) < len(candidates):
        trials = []
        selected = set(current)
        for addr in candidates:
            if addr in selected:
                continue
            trial = _key((*current, addr))
            metrics = evaluate(trial)
            states.add(trial)
            if _improves(current_metrics, metrics, constraints):
                trials.append((trial, metrics))
        if not trials:
            break
        current, current_metrics = max(trials, key=_rank)


def _backward_path(candidates: tuple[str, ...], evaluate: _CachedEvaluator,
                   constraints: selection.SelectionConstraints,
                   states: set[tuple[str, ...]]) -> None:
    current = candidates
    current_metrics = evaluate(current)
    states.add(current)
    while len(current) > 1:
        trials = []
        for addr in current:
            trial = _key(a for a in current if a != addr)
            metrics = evaluate(trial)
            states.add(trial)
            trials.append((trial, metrics))
        feasible = [item for item in trials if _feasible(item[1], constraints)]
        if not feasible:
            # An infeasible all-in start is still useful: remove the least harmful
            # wallet until a feasible state is reached.
            current, current_metrics = max(trials, key=_rank)
            continue
        best = max(feasible, key=_rank)
        if _feasible(current_metrics, constraints) and _utility(best[1]) <= _utility(current_metrics):
            break
        current, current_metrics = best


def _warm_fast_path(start: tuple[str, ...], candidates: tuple[str, ...],
                    evaluate: _CachedEvaluator,
                    constraints: selection.SelectionConstraints,
                    states: set[tuple[str, ...]]) -> None:
    current = start
    current_metrics = evaluate(current)
    states.add(current)
    visited = {current}
    while True:
        selected, outside = set(current), [a for a in candidates if a not in current]
        trials: set[tuple[str, ...]] = set()
        trials.update(_key((*current, addr)) for addr in outside)
        trials.update(_key(a for a in current if a != outgoing) for outgoing in current)
        trials.update(
            _key((*[a for a in current if a != outgoing], incoming))
            for outgoing in current for incoming in outside
        )
        # Pair additions let the path cross a weak single-wallet step without
        # running a parameter tune at every cardinality.
        trials.update(
            _key((*current, outside[i], outside[j]))
            for i in range(len(outside)) for j in range(i + 1, len(outside))
        )
        ranked = []
        for trial in trials:
            metrics = evaluate(trial)
            states.add(trial)
            if trial not in visited and _improves(current_metrics, metrics, constraints):
                ranked.append((trial, metrics))
        if not ranked:
            break
        current, current_metrics = max(ranked, key=_rank)
        visited.add(current)


def _finalists(states: set[tuple[str, ...]], evaluate: _CachedEvaluator,
               constraints: selection.SelectionConstraints, limit: int) -> list[tuple[str, ...]]:
    rows = [
        (state, evaluate(state)) for state in states
        if state and _feasible(evaluate(state), constraints)
    ]
    if not rows:
        return []
    kept: list[tuple[str, ...]] = []
    # Preserve the best state at every cardinality before filling globally.
    for size in sorted({len(state) for state, _ in rows}):
        item = max((row for row in rows if len(row[0]) == size), key=_rank)
        if item[0] not in kept:
            kept.append(item[0])
    for state, _ in sorted(rows, key=_rank, reverse=True):
        if state not in kept:
            kept.append(state)
        if len(kept) >= max(1, int(limit)):
            break
    if len(kept) > limit:
        # Cardinalities nearest the global winners are more useful than forcing
        # every size into a small strict budget.
        best = max(rows, key=_rank)[0]
        kept.sort(key=lambda state: (
            0 if state == best else 1, abs(len(state) - len(best)), -_utility(evaluate(state)), state,
        ))
        kept = kept[:limit]
    return kept


def _move_name(before: tuple[str, ...], after: tuple[str, ...]) -> str:
    added = len(set(after) - set(before))
    removed = len(set(before) - set(after))
    if added and removed:
        return f"swap_{removed}_for_{added}"
    if added:
        return f"add_{added}"
    return f"remove_{removed}"


def strict_local_closure(start: Sequence[str], candidates: Sequence[str],
                         fast_evaluator: Evaluator, strict_evaluator: Evaluator,
                         constraints: selection.SelectionConstraints,
                         config: OfflineSearchConfig = OfflineSearchConfig(),
                         *, deadline: Optional[float] = None,
                         fast_cache: Optional[_CachedEvaluator] = None,
                         strict_cache: Optional[_CachedEvaluator] = None,
                         steps: Optional[list[SearchStep]] = None,
                         phase: str = "strict_closure") -> tuple[tuple[str, ...], selection.PortfolioMetrics, bool]:
    candidates = _key(candidates)
    fast = fast_cache or _CachedEvaluator(fast_evaluator)
    strict = strict_cache or _CachedEvaluator(strict_evaluator)
    trace = steps if steps is not None else []
    if deadline is None:
        deadline = (
            float("inf") if float(config.time_budget_s) <= 0
            else time.monotonic() + float(config.time_budget_s)
        )
    current = _key(start)
    current_metrics = strict(current)
    visited = {current}
    timed_out = False

    for _ in range(max(0, int(config.max_strict_moves))):
        if time.monotonic() >= deadline:
            timed_out = True
            break
        selected = set(current)
        outside = [addr for addr in candidates if addr not in selected]
        by_kind: dict[str, list[tuple[tuple[str, ...], selection.PortfolioMetrics]]] = {
            "add": [], "remove": [], "swap": [], "pair_add": [],
        }
        for incoming in outside:
            trial = _key((*current, incoming))
            by_kind["add"].append((trial, fast(trial)))
        for outgoing in current:
            trial = _key(a for a in current if a != outgoing)
            if trial:
                by_kind["remove"].append((trial, fast(trial)))
        for outgoing in current:
            for incoming in outside:
                trial = _key((*[a for a in current if a != outgoing], incoming))
                by_kind["swap"].append((trial, fast(trial)))
        for i in range(len(outside)):
            for j in range(i + 1, len(outside)):
                trial = _key((*current, outside[i], outside[j]))
                by_kind["pair_add"].append((trial, fast(trial)))

        shortlisted: list[tuple[str, ...]] = []
        per_kind = max(1, int(config.strict_move_shortlist) // 3)
        for kind, rows in by_kind.items():
            kind_limit = min(
                int(config.pair_add_limit) if kind == "pair_add" else per_kind,
                len(rows),
            )
            for trial, _ in sorted(rows, key=_rank, reverse=True)[:kind_limit]:
                if trial not in visited and trial not in shortlisted:
                    shortlisted.append(trial)
        # Fill the remaining strict budget globally without losing move diversity.
        all_rows = [row for rows in by_kind.values() for row in rows]
        for trial, _ in sorted(all_rows, key=_rank, reverse=True):
            if trial not in visited and trial not in shortlisted:
                shortlisted.append(trial)
            if len(shortlisted) >= max(1, int(config.strict_move_shortlist)):
                break

        improved = []
        for trial in shortlisted:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            metrics = strict(trial)
            visited.add(trial)
            if _improves(
                current_metrics, metrics, constraints,
                min_utility_gain=config.min_utility_gain,
            ):
                improved.append((trial, metrics))
        if timed_out or not improved:
            break
        before = current
        current, current_metrics = max(improved, key=_rank)
        trace.append(SearchStep(
            phase=phase, action=_move_name(before, current), before=before, after=current,
            net=_net(current_metrics), utility=_utility(current_metrics),
        ))
    return current, current_metrics, timed_out


def optimize_membership(candidates: Sequence[str], current_core: Sequence[str],
                        fast_evaluator: Evaluator, strict_evaluator: Evaluator,
                        constraints: selection.SelectionConstraints,
                        config: OfflineSearchConfig = OfflineSearchConfig()) -> OfflineSearchResult:
    """Run multi-start cheap discovery, strict finalist selection and repeated local closure."""
    candidates = _key(candidates)[:max(0, int(constraints.max_targets))]
    initial = _key(addr for addr in current_core if addr in set(candidates))
    fast, strict = _CachedEvaluator(fast_evaluator), _CachedEvaluator(strict_evaluator)
    deadline = (
        float("inf") if float(config.time_budget_s) <= 0
        else time.monotonic() + float(config.time_budget_s)
    )
    states: set[tuple[str, ...]] = set()

    _forward_path(candidates, fast, constraints, states)
    _backward_path(candidates, fast, constraints, states)
    _warm_fast_path(initial, candidates, fast, constraints, states)
    # Score order is still useful as a diverse family of starts, but never a
    # membership rule by itself.
    for size in range(1, len(candidates) + 1):
        states.add(_key(candidates[:size]))
    finalists = _finalists(states, fast, constraints, config.finalist_limit)
    strict_rows = []
    for state in finalists:
        if time.monotonic() >= deadline:
            break
        metrics = strict(state)
        if _feasible(metrics, constraints):
            strict_rows.append((state, metrics))
    initial_metrics = strict(initial)
    if strict_rows:
        selected, metrics = max(strict_rows, key=_rank)
    elif _feasible(initial_metrics, constraints):
        selected, metrics = initial, initial_metrics
    else:
        selected, metrics = (), strict(())

    steps: list[SearchStep] = []
    selected, metrics, timed_out = strict_local_closure(
        selected, candidates, fast_evaluator, strict_evaluator, constraints, config,
        deadline=deadline, fast_cache=fast, strict_cache=strict, steps=steps,
    )
    return OfflineSearchResult(
        selected=selected, metrics=metrics, initial=initial, initial_metrics=initial_metrics,
        fast_evaluated=len(fast.cache), strict_evaluated=len(strict.cache),
        finalists=tuple(finalists), steps=steps,
        timed_out=timed_out or (
            deadline != float("inf") and time.monotonic() >= deadline
        ),
    )
