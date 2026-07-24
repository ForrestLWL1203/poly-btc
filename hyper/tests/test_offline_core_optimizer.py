import unittest

from hyper.selection import offline_core_optimizer as optimizer
from hyper.selection import state as selection


def metrics(net, *, utility=None, stress=None, deploy=.5, open_rate=.9, capacity=.95):
    utility = net if utility is None else utility
    return selection.PortfolioMetrics(
        net_lcb=net,
        stress_net_lcb=net if stress is None else stress,
        liquidations=0,
        actionable_open_rate=open_rate,
        capacity_fit=capacity,
        max_drawdown=0,
        peak_deploy_pct=deploy,
        cost_drag_ratio=0,
        net_pnl=net,
        stress_net_pnl=net if stress is None else stress,
        drawdown_dollars=max(0, net - utility),
        risk_adjusted_utility=utility,
    )


class OfflineCoreOptimizerTests(unittest.TestCase):
    constraints = selection.SelectionConstraints(
        min_relative_lcb_improvement=0,
        min_actionable_open_rate=.7,
        min_capacity_fit=.85,
        max_deploy_pct=1,
        max_targets=20,
    )

    @staticmethod
    def evaluator(values):
        return lambda addrs: values.get(addrs, metrics(-1, utility=-1, stress=-1))

    def test_fixed_parameter_closure_can_add_multiple_wallets(self):
        values = {
            (): metrics(0),
            ("0xa",): metrics(10),
            ("0xb",): metrics(8),
            ("0xc",): metrics(7),
            ("0xa", "0xb"): metrics(20),
            ("0xa", "0xc"): metrics(17),
            ("0xb", "0xc"): metrics(14),
            ("0xa", "0xb", "0xc"): metrics(31),
        }

        result = optimizer.optimize_membership(
            ["0xa", "0xb", "0xc"], ["0xa"], self.evaluator(values), self.evaluator(values),
            self.constraints,
        )

        self.assertEqual(result.selected, ("0xa", "0xb", "0xc"))
        self.assertGreater(result.metrics.net_pnl, result.initial_metrics.net_pnl)

    def test_pair_addition_crosses_unhelpful_single_steps(self):
        values = {
            ("0xa",): metrics(10),
            ("0xa", "0xb"): metrics(9),
            ("0xa", "0xc"): metrics(9.5),
            ("0xa", "0xb", "0xc"): metrics(18),
        }

        selected, result, timed_out = optimizer.strict_local_closure(
            ["0xa"], ["0xa", "0xb", "0xc"], self.evaluator(values), self.evaluator(values),
            self.constraints,
        )

        self.assertFalse(timed_out)
        self.assertEqual(selected, ("0xa", "0xb", "0xc"))
        self.assertEqual(result.net_pnl, 18)

    def test_strict_closure_can_replace_a_weak_core(self):
        values = {
            ("0xa", "0xweak"): metrics(20),
            ("0xa",): metrics(15),
            ("0xweak",): metrics(4),
            ("0xa", "0xstrong"): metrics(30),
            ("0xstrong", "0xweak"): metrics(19),
            ("0xa", "0xstrong", "0xweak"): metrics(18),
        }

        selected, result, _ = optimizer.strict_local_closure(
            ["0xa", "0xweak"], ["0xa", "0xweak", "0xstrong"],
            self.evaluator(values), self.evaluator(values), self.constraints,
        )

        self.assertEqual(selected, ("0xa", "0xstrong"))
        self.assertEqual(result.net_pnl, 30)

    def test_historical_drawdown_utility_does_not_block_profitable_move(self):
        values = {
            ("0xa",): metrics(20, utility=18),
            ("0xa", "0xrisky"): metrics(30, utility=15),
            ("0xrisky",): metrics(8, utility=2),
        }

        selected, _, _ = optimizer.strict_local_closure(
            ["0xa"], ["0xa", "0xrisky"], self.evaluator(values), self.evaluator(values),
            self.constraints,
        )

        self.assertEqual(selected, ("0xa", "0xrisky"))

    def test_robust_gate_requires_continuous_and_two_fold_improvement(self):
        base = metrics(100, utility=90)
        trial = metrics(120, utility=105)
        base_folds = [metrics(30), metrics(35), metrics(35)]
        recent_only = [metrics(25), metrics(30), metrics(60)]
        stable = [metrics(29), metrics(40), metrics(45)]

        rejected = optimizer.robust_improvement(
            base, trial, base_folds, recent_only, metrics(30), metrics(40), self.constraints,
        )
        accepted = optimizer.robust_improvement(
            base, trial, base_folds, stable, metrics(30), metrics(40), self.constraints,
        )

        self.assertFalse(rejected.eligible)
        self.assertIn("fewer_than_required_fold_wins", rejected.reasons)
        self.assertTrue(accepted.eligible)

    def test_robust_gate_rejects_new_cost_stress_liquidation(self):
        comparison = optimizer.robust_improvement(
            metrics(100), metrics(120),
            [metrics(30), metrics(35), metrics(35)],
            [metrics(32), metrics(38), metrics(40)],
            metrics(30),
            selection.PortfolioMetrics(
                **{**metrics(40).__dict__, "liquidations": 1}
            ),
            self.constraints,
        )

        self.assertFalse(comparison.eligible)
        self.assertIn("cost_stress_new_liquidation", comparison.reasons)

    def test_robust_candidate_can_choose_lower_ranked_complementary_pair(self):
        full = {
            ("0xa",): metrics(100),
            ("0xa", "0xd"): metrics(150),
            ("0xa", "0xb"): metrics(110),
            ("0xa", "0xc"): metrics(108),
            ("0xa", "0xb", "0xc"): metrics(130),
            ("0xa", "0xb", "0xd"): metrics(155),
            ("0xa", "0xc", "0xd"): metrics(152),
        }
        fold_values = {
            ("0xa",): [30, 35, 35, 30],
            ("0xa", "0xd"): [25, 30, 70, 50],
            ("0xa", "0xb"): [29, 37, 38, 34],
            ("0xa", "0xc"): [28, 36, 39, 33],
            ("0xa", "0xb", "0xc"): [29, 42, 45, 40],
            ("0xa", "0xb", "0xd"): [24, 34, 75, 55],
            ("0xa", "0xc", "0xd"): [23, 33, 74, 54],
        }

        def fold(addrs, older, newer, cost):
            index = {(30, 20): 0, (20, 10): 1, (10, 0): 2}[(older, newer)]
            if cost > 1:
                index = 3
            return metrics(fold_values[addrs][index])

        result = optimizer.choose_robust_candidate(
            ["0xa"], ["0xa", "0xb", "0xc", "0xd"],
            [("0xa", "0xd")], self.evaluator(full), fold, self.constraints,
        )

        self.assertEqual(result.selected, ("0xa", "0xb", "0xc"))
        self.assertTrue(result.comparison.eligible)


if __name__ == "__main__":
    unittest.main()
