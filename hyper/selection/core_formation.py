"""Quality-first Core formation.

Each bounded binary prefix node may own an independently tuned sizing surface.  This preserves the intended
16 -> 8 -> 12 search: wallet count and capital sizing are evaluated together instead of measuring every count
with parameters fitted only to the incumbent Core.  A later strict leave-one-out pass may remove a non-tail
member only when its actual presence lowers funded shared-account net economics.
"""
from __future__ import annotations

from dataclasses import dataclass
import itertools
from typing import Callable, Mapping

from hyper import config


@dataclass(frozen=True)
class PrefixEvaluation:
    count: int
    net_pnl: float
    stress_net_pnl: float
    max_drawdown: float
    actionable_open_rate: float
    capacity_fit: float
    liquidations: int
    params: Mapping[str, float]
    payload: Mapping[str, object]

    @property
    def utility(self) -> float:
        # max_drawdown is an account-equity fraction.  The caller supplies the account anchor in payload.
        anchor = float(self.payload.get("initialBalance") or 10_000.0)
        return float(self.net_pnl) - float(self.max_drawdown) * anchor

    @property
    def feasible(self) -> bool:
        # Copy positions use isolated margin.  A liquidation loses that position's bounded margin and the
        # loss is already debited from net_pnl and reflected in max_drawdown.  Treating the count itself as
        # a veto double-charges the same loss and can let one profitable wallet collapse an otherwise valid
        # quality prefix.  Account ruin is still impossible to admit: the portfolio must stay solvent and
        # profitable in both the normal and stressed replays.
        return (
            self.net_pnl > 0
            and self.stress_net_pnl > 0
            and self.max_drawdown < 1.0
            and self.actionable_open_rate >= 0.70
            and self.capacity_fit >= float(config.SELECTION_MIN_CAPACITY_FIT)
        )


@dataclass(frozen=True)
class MembershipSearchResult:
    selected: tuple[str, ...]
    metrics: PrefixEvaluation
    evaluated: int
    algorithm: str


def validate_final_membership(
    candidate: PrefixEvaluation,
    candidate_folds: list[PrefixEvaluation],
    *,
    cost_stress_net: float,
    baseline: PrefixEvaluation | None = None,
    baseline_folds: list[PrefixEvaluation] | None = None,
    membership_changed: bool = False,
    replacing_qualified_core: bool = False,
    initial_margin_equity: float = 10_000.0,
    min_relative_utility_gain: float = 0.05,
    min_net_return_gain: float = 0.02,
    tail_after_top1: float | None = None,
    tail_after_top2: float | None = None,
    min_tail_return: float = 0.05,
    top_wallet_normal_net: float | None = None,
    top_wallet_stress_net: float | None = None,
    all_members_strong: bool = False,
) -> dict:
    """Final, expensive membership guard shared by production formation and tests.

    Parameter tuning already validates its chosen surface.  This separate check validates the *wallet set*
    on three disjoint regimes, cost stress, tail concentration and dominant-wallet removal.  It deliberately
    has no clock or multi-generation hysteresis: a currently unqualified old member is outside the baseline.
    """
    reasons = []
    if len(candidate_folds) != 3:
        reasons.append("membership_folds_unavailable")
    else:
        if any(
            fold.actionable_open_rate < 0.70
            or fold.capacity_fit < float(config.SELECTION_MIN_CAPACITY_FIT)
            or fold.max_drawdown >= 1.0
            for fold in candidate_folds
        ):
            reasons.append("membership_fold_infeasible")
        if candidate_folds[-1].net_pnl <= 0.0:
            reasons.append("membership_latest_fold_not_profitable")
    if candidate.net_pnl <= 0.0 or candidate.stress_net_pnl <= 0.0 or cost_stress_net <= 0.0:
        reasons.append("membership_cost_stress_not_profitable")

    baseline_folds = list(baseline_folds or [])
    fold_deltas = []
    compare_folds = membership_changed or replacing_qualified_core or baseline is None
    if compare_folds and len(candidate_folds) == len(baseline_folds) == 3:
        fold_deltas = [
            candidate_fold.net_pnl - baseline_fold.net_pnl
            for candidate_fold, baseline_fold in zip(candidate_folds, baseline_folds)
        ]
        if sum(delta > 0.0 for delta in fold_deltas) < 2:
            reasons.append("membership_fewer_than_two_fold_wins")
        if fold_deltas[-1] < 0.0:
            reasons.append("membership_latest_fold_degraded")
    elif compare_folds:
        reasons.append("membership_baseline_folds_unavailable")

    if membership_changed and baseline is not None and not replacing_qualified_core:
        # Adding a wallet is not a replacement of a still-qualified Core member.  It must improve funded
        # dollars and independent windows, but does not owe the 5% utility / 2%-of-equity hurdle designed
        # to prevent churn from swapping healthy incumbents.
        if candidate.net_pnl - baseline.net_pnl + 1e-9 < float(config.CORE_LOO_MIN_NET_GAIN):
            reasons.append("membership_addition_no_positive_marginal_net")

    if replacing_qualified_core and baseline is not None:
        utility_floor = baseline.utility + abs(baseline.utility) * max(0.0, min_relative_utility_gain)
        if candidate.utility + 1e-9 < utility_floor:
            reasons.append("membership_utility_gain_below_5pct")
        absolute_net_floor = max(0.0, initial_margin_equity) * max(0.0, min_net_return_gain)
        if candidate.net_pnl - baseline.net_pnl + 1e-9 < absolute_net_floor:
            reasons.append("membership_net_gain_below_2pct_equity")

    tail_floor = max(0.0, initial_margin_equity) * max(0.0, min_tail_return)
    if tail_after_top1 is None or tail_after_top2 is None:
        reasons.append("membership_tail_metrics_missing")
    elif tail_after_top1 < tail_floor or tail_after_top2 < tail_floor:
        reasons.append("membership_tail_profit_weak")

    dependency_warning = False
    if top_wallet_normal_net is None or top_wallet_stress_net is None:
        reasons.append("membership_top_wallet_stress_missing")
    elif top_wallet_normal_net <= 0.0 or top_wallet_stress_net <= 0.0:
        if all_members_strong:
            dependency_warning = True
        else:
            reasons.append("membership_single_wallet_dependency")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "foldWins": sum(delta > 0.0 for delta in fold_deltas),
        "foldDeltas": fold_deltas,
        "singleWalletDependencyWarning": dependency_warning,
    }


def search_quality_membership(candidates, evaluate, *, initial=(), required=(), exhaustive_below: int = 8):
    """Find a feasible quality subset without letting one congested wallet block every later wallet.

    Small Core-ready pools are exhaustively evaluated.  Larger pools start from the count search's winning
    prefix and apply bounded best-add/best-swap closure.  The evaluator owns the shared-account parameter
    surface; membership only compares complete portfolio replays on that one surface.
    """
    ordered = tuple(dict.fromkeys(str(addr).lower() for addr in candidates if addr))
    if not ordered:
        raise ValueError("candidates must not be empty")
    required_set = {str(addr).lower() for addr in required if addr}
    if not required_set.issubset(set(ordered)):
        raise ValueError("required wallets must be present in candidates")
    cache = {}

    def get(addrs):
        key = tuple(sorted(dict.fromkeys(addrs)))
        if key not in cache:
            value = evaluate(key)
            if int(value.count) != len(key):
                raise ValueError("membership evaluation count mismatch")
            cache[key] = value
        return cache[key]

    def rank(item):
        addrs, value = item
        return (value.utility, value.net_pnl, value.stress_net_pnl, -value.max_drawdown, -len(addrs), addrs)

    if len(ordered) <= max(1, int(exhaustive_below)):
        states = []
        for count in range(max(1, len(required_set)), len(ordered) + 1):
            for addrs in itertools.combinations(ordered, count):
                if not required_set.issubset(addrs):
                    continue
                value = get(addrs)
                if value.feasible:
                    states.append((tuple(sorted(addrs)), value))
        if not states:
            raise RuntimeError("no_feasible_quality_membership")
        selected, metrics = max(states, key=rank)
        return MembershipSearchResult(selected, metrics, len(cache), "exhaustive_subset")

    selected = tuple(sorted(set(dict.fromkeys(initial)) | required_set))
    current = get(selected) if selected else None
    if current is None or not current.feasible:
        base = tuple(sorted(required_set))
        seeds = []
        if base:
            seeds.append(base)
            seeds.extend(tuple(sorted((*base, addr))) for addr in ordered if addr not in required_set)
        else:
            seeds.extend((addr,) for addr in ordered)
        feasible = [(seed, get(seed)) for seed in seeds if get(seed).feasible]
        if not feasible:
            raise RuntimeError("no_feasible_quality_membership")
        selected, current = max(feasible, key=rank)
    seen = {selected}
    for _ in range(len(ordered) * 2):
        selected_set = set(selected)
        outside = [addr for addr in ordered if addr not in selected_set]
        trials = []
        for incoming in outside:
            addrs = tuple(sorted((*selected, incoming)))
            value = get(addrs)
            if value.feasible and value.utility > current.utility:
                trials.append((addrs, value))
            for outgoing in selected:
                if outgoing in required_set:
                    continue
                swapped = tuple(sorted((selected_set - {outgoing}) | {incoming}))
                value = get(swapped)
                if value.feasible and value.utility > current.utility:
                    trials.append((swapped, value))
        if not trials:
            break
        next_selected, next_metrics = max(trials, key=rank)
        if next_selected in seen:
            break
        seen.add(next_selected)
        selected, current = next_selected, next_metrics
    return MembershipSearchResult(selected, current, len(cache), "bounded_add_swap")


@dataclass(frozen=True)
class PrefixSearchResult:
    selected: PrefixEvaluation
    reference: PrefixEvaluation
    evaluated: tuple[PrefixEvaluation, ...]
    boundary: int


def retains_reference(reference: PrefixEvaluation, candidate: PrefixEvaluation, *,
                      utility_retention: float = .97, net_retention: float = .95,
                      stress_retention: float = .90, utility_slack: float = 50.0,
                      net_slack: float = 100.0, stress_slack: float = 100.0,
                      max_dd_worsen: float = .01) -> bool:
    """Whether a smaller quality prefix preserves the full-prefix portfolio.

    Absolute slack keeps the predicate stable near zero; relative retention governs meaningful portfolios.
    The full reference always passes when feasible.
    """
    if not candidate.feasible:
        return False
    utility_floor = reference.utility - max(abs(reference.utility) * (1.0 - utility_retention), utility_slack)
    net_floor = reference.net_pnl - max(abs(reference.net_pnl) * (1.0 - net_retention), net_slack)
    stress_floor = max(
        0.0,
        reference.stress_net_pnl
        - max(abs(reference.stress_net_pnl) * (1.0 - stress_retention), stress_slack),
    )
    return (
        candidate.utility >= utility_floor
        and candidate.net_pnl >= net_floor
        and candidate.stress_net_pnl >= stress_floor
        and candidate.max_drawdown <= reference.max_drawdown + max_dd_worsen
    )


def search_quality_prefix(initial_count: int, evaluate: Callable[[int], PrefixEvaluation], *,
                          retention_kwargs: Mapping[str, float] | None = None,
                          tie_tolerance: float = .02,
                          exhaustive_below: int = 0,
                          min_count: int = 1) -> PrefixSearchResult:
    """Evaluate quality prefixes and return the best safe economic state.

    Small pools are cheap enough to search exhaustively.  Larger pools use the original bounded binary
    direction (16 -> 8 -> 12 ...) plus boundary neighbours.  The full-size prefix is only the search anchor,
    never a privileged answer; the final answer is the highest-utility feasible evaluated state.
    """
    initial_count = int(initial_count)
    if initial_count < 1:
        raise ValueError("initial_count must be positive")
    min_count = int(min_count)
    if min_count < 1 or min_count > initial_count:
        raise ValueError("min_count must be between one and initial_count")
    cache: dict[int, PrefixEvaluation] = {}

    def get(count: int) -> PrefixEvaluation:
        count = max(min_count, min(initial_count, int(count)))
        if count not in cache:
            value = evaluate(count)
            if int(value.count) != count:
                raise ValueError("prefix evaluation count mismatch")
            cache[count] = value
        return cache[count]

    reference = get(initial_count)
    retain_args = dict(retention_kwargs or {})
    lo, hi = min_count, initial_count
    if initial_count <= max(0, int(exhaustive_below)):
        for count in range(min_count, initial_count + 1):
            get(count)
        if reference.feasible:
            retained = [
                value.count for value in cache.values()
                if retains_reference(reference, value, **retain_args)
            ]
            boundary = min(retained or [initial_count])
        else:
            boundary = max((value.count for value in cache.values() if value.feasible), default=0)
    elif reference.feasible:
        # Find the smallest prefix that preserves the fully funded initial portfolio.
        while lo < hi:
            mid = (lo + hi) // 2
            if retains_reference(reference, get(mid), **retain_args):
                hi = mid
            else:
                lo = mid + 1
        boundary = lo
    else:
        # Capital contention can make the full high-quality set infeasible.  Feasibility is monotone in
        # the useful direction: removing a low-quality suffix releases capacity.  Find the largest feasible
        # prefix, then compare its neighbours.  This is the same 16 -> 8 -> 12 search direction.
        first = get(min_count)
        if not first.feasible:
            raise RuntimeError("no_feasible_quality_prefix")
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if get(mid).feasible:
                lo = mid
            else:
                hi = mid - 1
        boundary = lo
    for count in {boundary - 1, boundary, boundary + 1, initial_count}:
        if min_count <= count <= initial_count:
            get(count)
    feasible = [value for value in cache.values() if value.feasible]
    if not feasible:
        raise RuntimeError("no_feasible_quality_prefix")
    best_utility = max(value.utility for value in feasible)
    tolerance = max(0.0, float(tie_tolerance))
    near_best = [
        value for value in feasible
        if value.utility >= best_utility - max(1.0, abs(best_utility) * tolerance)
    ]
    selected = min(near_best, key=lambda value: (value.count, -value.utility, -value.net_pnl))
    return PrefixSearchResult(
        selected=selected,
        reference=reference,
        evaluated=tuple(cache[count] for count in sorted(cache)),
        boundary=boundary,
    )
