import unittest

from hl.core_formation import PrefixEvaluation, search_quality_prefix


def value(count, utility, *, feasible=True):
    drawdown = 0.02
    net = utility + drawdown * 10_000
    return PrefixEvaluation(
        count=count,
        net_pnl=net if feasible else -1,
        stress_net_pnl=max(1, net * 0.3) if feasible else -1,
        max_drawdown=drawdown,
        actionable_open_rate=0.9 if feasible else 0.1,
        capacity_fit=0.95 if feasible else 0.1,
        liquidations=0 if feasible else 1,
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

    def test_infeasible_full_prefix_searches_down_to_largest_feasible_count(self):
        result = search_quality_prefix(
            16, lambda count: value(count, 1000, feasible=count <= 12), tie_tolerance=0,
        )
        self.assertEqual(result.boundary, 12)
        self.assertEqual(result.selected.count, 12)

    def test_rejects_when_even_one_quality_wallet_is_infeasible(self):
        with self.assertRaisesRegex(RuntimeError, "no_feasible_quality_prefix"):
            search_quality_prefix(4, lambda count: value(count, 0, feasible=False))


if __name__ == "__main__":
    unittest.main()
