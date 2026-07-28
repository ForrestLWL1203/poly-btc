import unittest
from dataclasses import replace

from hyper.selection.core_formation import (
    PrefixEvaluation, retains_reference, search_quality_prefix, validate_final_membership,
)


def value(
    count, utility, *, feasible=True, liquidations=0, capacity_fit=.95,
    return30=.20, return7=.08, require_return=False, open_loss_ratio=None,
):
    drawdown = 0.02
    net = utility + drawdown * 10_000
    return PrefixEvaluation(
        count=count,
        net_pnl=net if feasible else -1,
        stress_net_pnl=net if feasible else -1,
        max_drawdown=drawdown,
        actionable_open_rate=0.9 if feasible else 0.1,
        capacity_fit=capacity_fit if feasible else 0.1,
        liquidations=liquidations if feasible else max(1, liquidations),
        params={"n": count},
        payload={
            "initialBalance": 10_000,
            "return30d": return30,
            "return7d": return7,
            "requireReturnFit": require_return,
            "openLossRatio30d": open_loss_ratio,
        },
    )


class QualityPrefixSearchTests(unittest.TestCase):
    def test_final_membership_accepts_dynamic_returns(self):
        result = validate_final_membership(value(3, 1300, require_return=True))
        self.assertTrue(result["eligible"])
        self.assertEqual(result["returnFloors"], {"30d": .10, "7d": .03})

    def test_final_membership_rejects_weak_rolling_return(self):
        result = validate_final_membership(
            value(3, 1300, return7=.029, require_return=True),
        )
        self.assertFalse(result["eligible"])
        self.assertIn(
            "membership_dynamic_return_or_execution_failed", result["reasons"],
        )

    def test_final_membership_rejects_open_loss_over_half_of_closed_profit(self):
        result = validate_final_membership(
            value(3, 1300, require_return=True, open_loss_ratio=.5001),
        )
        self.assertFalse(result["eligible"])
        self.assertIn(
            "membership_dynamic_return_or_execution_failed", result["reasons"],
        )

    def test_final_membership_does_not_repeat_individual_wallet_gates(self):
        accepted = validate_final_membership(value(2, 1000, require_return=True))
        self.assertTrue(accepted["eligible"])

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
            lambda count: calls.append(count) or value(
                count, 1000 + (500 if count == 5 else 0),
            ),
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
            required_count=3,
        )
        self.assertEqual(sorted(calls), list(range(3, 8)))
        self.assertEqual(result.selected.count, 3)

    def test_inferior_full_prefix_cannot_force_tail_wallet_into_core(self):
        metrics = {
            16: PrefixEvaluation(
                16, 55_405, 55_405, .1111, .90, .95, 0, {},
                {"initialBalance": 10_000},
            ),
            15: PrefixEvaluation(
                15, 57_740, 57_740, .1417, .90, .95, 0, {},
                {"initialBalance": 10_000},
            ),
        }

        def evaluate(count):
            return metrics.get(
                count,
                PrefixEvaluation(
                    count, 40_000 + count, 40_000 + count, .12, .90, .95, 0, {},
                    {"initialBalance": 10_000},
                ),
            )

        result = search_quality_prefix(16, evaluate, tie_tolerance=0)
        self.assertTrue(retains_reference(result.reference, metrics[15]))
        self.assertEqual(result.selected.count, 15)
        self.assertGreater(result.selected.net_pnl, result.reference.net_pnl)

    def test_infeasible_full_prefix_does_not_fill_largest_feasible_count(self):
        result = search_quality_prefix(
            16, lambda count: value(count, 1000, feasible=count <= 12), tie_tolerance=0,
        )
        self.assertEqual(result.boundary, 12)
        self.assertEqual(result.selected.count, 1)

    def test_profitable_isolated_liquidations_do_not_veto_prefix(self):
        metrics = {
            16: value(16, 27_287, liquidations=14),
            8: value(8, 20_000, liquidations=8),
            12: value(12, 30_000, liquidations=12),
            10: value(10, 32_000, liquidations=10),
            9: value(9, 33_588, liquidations=11),
        }
        result = search_quality_prefix(
            16,
            lambda count: metrics.get(count, value(count, 1_000, liquidations=count)),
            tie_tolerance=0,
        )
        self.assertTrue(result.reference.feasible)
        self.assertEqual(result.selected.count, 9)

    def test_rejects_when_every_quality_prefix_is_infeasible(self):
        with self.assertRaisesRegex(RuntimeError, "no_feasible_quality_prefix"):
            search_quality_prefix(4, lambda count: value(count, 0, feasible=False))

    def test_capacity_is_only_a_count_search_constraint(self):
        self.assertTrue(value(1, 1000, capacity_fit=.20).feasible)
        adaptive = replace(
            value(1, 1000, capacity_fit=.20),
            payload={"initialBalance": 10_000, "requireCongestionFit": True},
        )
        self.assertFalse(adaptive.feasible)


if __name__ == "__main__":
    unittest.main()
