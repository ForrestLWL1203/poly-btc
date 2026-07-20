import unittest

from hyper.core_formation import (
    PrefixEvaluation, retains_reference, search_quality_membership, search_quality_prefix,
    validate_final_membership,
)


def value(count, utility, *, feasible=True, liquidations=0, capacity_fit=.95):
    drawdown = 0.02
    net = utility + drawdown * 10_000
    return PrefixEvaluation(
        count=count,
        net_pnl=net if feasible else -1,
        stress_net_pnl=max(1, net * 0.3) if feasible else -1,
        max_drawdown=drawdown,
        actionable_open_rate=0.9 if feasible else 0.1,
        capacity_fit=capacity_fit if feasible else 0.1,
        liquidations=liquidations if feasible else max(1, liquidations),
        params={"n": count},
        payload={"initialBalance": 10_000},
    )


class QualityPrefixSearchTests(unittest.TestCase):
    def test_final_membership_requires_two_fold_wins_latest_profit_and_replacement_hurdle(self):
        baseline = value(2, 1000)
        candidate = value(3, 1300)
        base_folds = [value(2, 100), value(2, 100), value(2, 100)]
        good_folds = [value(3, 120), value(3, 120), value(3, 110)]

        result = validate_final_membership(
            candidate, good_folds, cost_stress_net=100,
            baseline=baseline, baseline_folds=base_folds,
            replacing_qualified_core=True, initial_margin_equity=10_000,
            tail_after_top1=700, tail_after_top2=600,
            top_wallet_normal_net=50, top_wallet_stress_net=25,
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["foldWins"], 3)

    def test_pure_addition_needs_positive_net_and_folds_but_not_replacement_hurdle(self):
        baseline = value(2, 1000)
        # Only +$10: deliberately below the replacement 2%-of-equity hurdle, but still a real addition.
        candidate = value(3, 1010)
        base_folds = [value(2, 100), value(2, 100), value(2, 100)]
        good_folds = [value(3, 110), value(3, 105), value(3, 102)]

        result = validate_final_membership(
            candidate, good_folds, cost_stress_net=100,
            baseline=baseline, baseline_folds=base_folds,
            membership_changed=True, replacing_qualified_core=False,
            initial_margin_equity=10_000,
            tail_after_top1=700, tail_after_top2=600,
            top_wallet_normal_net=50, top_wallet_stress_net=25,
        )

        self.assertTrue(result["eligible"])
        self.assertNotIn("membership_utility_gain_below_5pct", result["reasons"])
        self.assertNotIn("membership_net_gain_below_2pct_equity", result["reasons"])

    def test_final_membership_rejects_single_wallet_dependency_unless_all_strong(self):
        candidate = value(2, 1000)
        folds = [value(2, 100), value(2, 100), value(2, 100)]
        rejected = validate_final_membership(
            candidate, folds, cost_stress_net=100,
            baseline=None, baseline_folds=[value(0, 0)] * 3,
            tail_after_top1=700, tail_after_top2=600,
            top_wallet_normal_net=-1, top_wallet_stress_net=-1,
        )
        warned = validate_final_membership(
            candidate, folds, cost_stress_net=100,
            baseline=None, baseline_folds=[value(0, 0)] * 3,
            tail_after_top1=700, tail_after_top2=600,
            top_wallet_normal_net=-1, top_wallet_stress_net=-1,
            all_members_strong=True,
        )

        self.assertFalse(rejected["eligible"])
        self.assertIn("membership_single_wallet_dependency", rejected["reasons"])
        self.assertTrue(warned["eligible"])
        self.assertTrue(warned["singleWalletDependencyWarning"])
    def test_binary_search_follows_16_8_12_direction_and_checks_neighbours(self):
        calls = []

        def evaluate(count):
            calls.append(count)
            return value(count, 1000 if count >= 12 else 500)

        result = search_quality_prefix(16, evaluate, tie_tolerance=0)

        self.assertEqual(calls[:3], [16, 8, 12])
        self.assertEqual(result.boundary, 12)
        self.assertEqual(result.selected.count, 12)
        self.assertLessEqual(len(calls), 7)
        self.assertEqual(len(calls), len(set(calls)))

    def test_prefers_fewer_wallets_when_utility_is_within_tolerance(self):
        utilities = {16: 1000, 8: 990, 4: 900, 6: 980, 7: 985, 5: 970}

        result = search_quality_prefix(
            16, lambda count: value(count, utilities.get(count, 990)), tie_tolerance=.02,
        )

        self.assertLess(result.selected.count, 16)
        self.assertGreaterEqual(result.selected.utility, 980)

    def test_small_pool_tunes_every_wallet_count_instead_of_approximating(self):
        calls = []

        result = search_quality_prefix(
            7,
            lambda count: calls.append(count) or value(count, 1000 + (500 if count == 5 else 0)),
            tie_tolerance=0,
            exhaustive_below=8,
        )

        self.assertEqual(sorted(calls), list(range(1, 8)))
        self.assertEqual(result.selected.count, 5)

    def test_prefix_search_never_evaluates_below_required_starred_count(self):
        calls = []
        result = search_quality_prefix(
            7,
            lambda count: calls.append(count) or value(count, 1000 - count),
            tie_tolerance=0,
            exhaustive_below=8,
            min_count=3,
        )

        self.assertEqual(sorted(calls), list(range(3, 8)))
        self.assertEqual(result.selected.count, 3)

    def test_inferior_full_prefix_cannot_force_the_sixteenth_wallet_into_core(self):
        # Production-shaped regression: removing the quality-tail wallet improves normal PnL, stressed PnL,
        # and risk-adjusted utility.  The old implementation nevertheless forced 16 because 15 had a little
        # more drawdown than the full-size reference and therefore failed the reference-retention predicate.
        metrics = {
            16: PrefixEvaluation(16, 55_405, 25_829, .1111, .90, .95, 0, {}, {"initialBalance": 10_000}),
            15: PrefixEvaluation(15, 57_740, 27_882, .1417, .90, .95, 0, {}, {"initialBalance": 10_000}),
        }

        def evaluate(count):
            return metrics.get(
                count,
                PrefixEvaluation(count, 40_000 + count, 20_000 + count, .12, .90, .95, 0, {},
                                 {"initialBalance": 10_000}),
            )

        result = search_quality_prefix(16, evaluate, tie_tolerance=0)

        self.assertFalse(retains_reference(result.reference, metrics[15], max_dd_worsen=.01))
        self.assertEqual(result.selected.count, 15)
        self.assertGreater(result.selected.net_pnl, result.reference.net_pnl)
        self.assertGreater(result.selected.stress_net_pnl, result.reference.stress_net_pnl)
        self.assertGreater(result.selected.utility, result.reference.utility)

    def test_infeasible_full_prefix_does_not_fill_the_largest_feasible_count(self):
        result = search_quality_prefix(
            16, lambda count: value(count, 1000, feasible=count <= 12), tie_tolerance=0,
        )
        self.assertEqual(result.boundary, 12)
        # Twelve is the capacity boundary, not a quota.  With identical economics the smallest evaluated
        # portfolio wins instead of filling every safe slot.
        self.assertEqual(result.selected.count, 1)

    def test_profitable_isolated_liquidations_do_not_veto_a_prefix(self):
        # The loss from each isolated liquidation is already present in net PnL and drawdown.  The full
        # portfolio remains the economic reference, and the more profitable/risk-adjusted 9-wallet prefix
        # is allowed to win instead of forcing the search down to a zero-liquidation 2-wallet prefix.
        metrics = {
            16: value(16, 27_287, liquidations=14),
            8: value(8, 20_000, liquidations=8),
            12: value(12, 30_000, liquidations=12),
            10: value(10, 32_000, liquidations=10),
            9: value(9, 33_588, liquidations=11),
        }

        result = search_quality_prefix(
            16, lambda count: metrics.get(count, value(count, 1_000, liquidations=count)),
            tie_tolerance=0,
        )

        self.assertTrue(result.reference.feasible)
        self.assertEqual(result.selected.count, 9)

    def test_rejects_when_even_one_quality_wallet_is_infeasible(self):
        with self.assertRaisesRegex(RuntimeError, "no_feasible_quality_prefix"):
            search_quality_prefix(4, lambda count: value(count, 0, feasible=False))

    def test_capacity_floor_is_seventy_five_percent(self):
        self.assertTrue(value(1, 1000, capacity_fit=.75).feasible)
        self.assertFalse(value(1, 1000, capacity_fit=.749).feasible)

    def test_small_pool_skips_congested_wallet_and_selects_later_strong_wallet(self):
        candidates = ("0xa", "0xb", "0xc")
        utilities = {
            ("0xa",): (1000, .95),
            ("0xa", "0xb"): (1300, .70),
            ("0xa", "0xc"): (1800, .80),
            ("0xa", "0xb", "0xc"): (2000, .72),
        }

        def evaluate(addrs):
            utility, capacity = utilities.get(tuple(sorted(addrs)), (100, .90))
            return value(len(addrs), utility, capacity_fit=capacity)

        result = search_quality_membership(candidates, evaluate, exhaustive_below=8)

        self.assertEqual(result.selected, ("0xa", "0xc"))
        self.assertEqual(result.algorithm, "exhaustive_subset")

    def test_required_starred_wallet_survives_membership_search(self):
        candidates = ("0xstar", "0xbest", "0xtail")
        utilities = {
            ("0xbest",): 5000,
            ("0xbest", "0xstar"): 4500,
            ("0xstar",): 1000,
            ("0xstar", "0xtail"): 1100,
            ("0xbest", "0xstar", "0xtail"): 4400,
        }

        def evaluate(addrs):
            key = tuple(sorted(addrs))
            return value(len(key), utilities.get(key, 100))

        result = search_quality_membership(
            candidates, evaluate, required=("0xstar",), exhaustive_below=8,
        )

        self.assertIn("0xstar", result.selected)
        self.assertEqual(set(result.selected), {"0xstar", "0xbest"})


if __name__ == "__main__":
    unittest.main()
