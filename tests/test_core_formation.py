import unittest

from hl.core_formation import PrefixEvaluation, retains_reference, search_quality_prefix


def value(count, utility, *, feasible=True, liquidations=0):
    drawdown = 0.02
    net = utility + drawdown * 10_000
    return PrefixEvaluation(
        count=count,
        net_pnl=net if feasible else -1,
        stress_net_pnl=max(1, net * 0.3) if feasible else -1,
        max_drawdown=drawdown,
        actionable_open_rate=0.9 if feasible else 0.1,
        capacity_fit=0.95 if feasible else 0.1,
        liquidations=liquidations if feasible else max(1, liquidations),
        params={"n": count},
        payload={"initialBalance": 10_000},
    )


class QualityPrefixSearchTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
