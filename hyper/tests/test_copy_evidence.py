import unittest

from hyper.copy.copy_evidence import summarize_campaign_stability, summarize_copy_evidence


DAY = 86_400_000


def positions(returns, margin=100.0):
    return [
        {"closed_at": (i + 1) * DAY, "margin": margin, "net_pnl": value * margin}
        for i, value in enumerate(returns)
    ]


class CopyEvidenceTests(unittest.TestCase):
    def test_is_deterministic(self):
        rows = positions([0.05, 0.03, -0.01, 0.06, 0.02])
        self.assertEqual(summarize_copy_evidence(rows, seed="a"), summarize_copy_evidence(rows, seed="a"))

    def test_is_capital_scale_invariant(self):
        a = summarize_copy_evidence(positions([0.05, -0.01, 0.08], 100), seed="same")
        b = summarize_copy_evidence(positions([0.05, -0.01, 0.08], 10_000), seed="same")
        self.assertEqual(a, b)

    def test_one_day_burst_does_not_claim_high_confidence(self):
        same_day = [
            {"closed_at": DAY, "margin": 100, "net_pnl": 10}
            for _ in range(20)
        ]
        result = summarize_copy_evidence(same_day, seed="burst")
        self.assertEqual(result.evidence_days, 1)
        self.assertLess(result.positive_probability, 0.70)
        self.assertLessEqual(result.return_lcb, 0.0)

    def test_consistent_multi_day_edge_has_positive_lcb(self):
        result = summarize_copy_evidence(positions([0.05] * 20), seed="steady")
        self.assertGreater(result.positive_probability, 0.90)
        self.assertGreater(result.return_lcb, 0.0)

    def test_four_nonoverlap_weeks_each_need_five_percent_and_cost_stress(self):
        now = 28 * DAY
        rows = []
        for day, pnl in (
            (2, 300), (4, 300), (9, 300), (11, 300),
            (16, 300), (18, 300), (23, 300), (25, 300),
        ):
            rows.append({
                "addr": "0xa", "coin": "BTC", "side": "long",
                "opened_at": day * DAY - 1_000, "closed_at": day * DAY,
                "status": "closed", "net_pnl": pnl, "fee_drag": 20,
            })
        result = summarize_campaign_stability(rows, now_ms=now)
        self.assertEqual(result["evaluableFolds"], 4)
        self.assertEqual(result["qualifiedFolds"], 4)
        self.assertTrue(result["passed"])
        self.assertTrue(all(fold["return"] >= .05 for fold in result["folds"]))
        self.assertTrue(result["allCostStressPositive"])

    def test_thin_week_is_unknown_and_cannot_enter_core(self):
        now = 28 * DAY
        rows = [
            {"addr": "0xa", "coin": "BTC", "side": "long", "opened_at": day * DAY - 1_000,
             "closed_at": day * DAY, "status": "closed", "net_pnl": 300}
            for day in (2, 9, 11, 16, 18, 23, 25)
        ]
        result = summarize_campaign_stability(rows, now_ms=now)
        self.assertEqual(result["evaluableFolds"], 3)
        self.assertFalse(result["passed"])
        self.assertFalse(result["folds"][0]["evaluable"])

    def test_marked_path_uses_floating_starting_equity(self):
        now = 28 * DAY
        rows = []
        for day in (2, 4, 9, 11, 16, 18, 23, 25):
            rows.append({
                "addr": "0xa", "coin": "BTC", "side": "long",
                "opened_at": day * DAY - 1_000, "closed_at": day * DAY,
                "status": "closed", "net_pnl": 1, "fee_drag": 0,
            })
        equities = [
            {"time": 0, "equity": 10_000},
            {"time": 7 * DAY, "equity": 10_500},
            {"time": 14 * DAY, "equity": 11_025},
            {"time": 21 * DAY, "equity": 11_576.25},
            {"time": 28 * DAY, "equity": 12_155.0625},
        ]
        result = summarize_campaign_stability(
            rows, now_ms=now, path_equity_samples=equities,
        )
        self.assertTrue(result["passed"])
        self.assertAlmostEqual(result["folds"][1]["startEquity"], 10_500)
        self.assertAlmostEqual(result["folds"][1]["return"], .05)

    def test_copy_weekly_floor_is_four_percent_not_official_five_percent(self):
        now = 28 * DAY
        rows = []
        equity = 10_000.0
        for week in range(4):
            fold_net = equity * .045
            for offset in (2, 4):
                day = week * 7 + offset
                rows.append({
                    "addr": "0xa", "coin": "BTC", "side": "long",
                    "opened_at": day * DAY - 1_000, "closed_at": day * DAY,
                    "status": "closed", "net_pnl": fold_net / 2, "fee_drag": 5,
                })
            equity += fold_net
        result = summarize_campaign_stability(
            rows, now_ms=now, min_return=.04, min_net_per_closed_return=.005,
        )
        self.assertTrue(result["passed"])
        self.assertTrue(all(.04 <= fold["return"] < .05 for fold in result["folds"]))

    def test_high_turnover_thin_week_fails_per_close_economic_density(self):
        now = 28 * DAY
        rows = []
        for week in range(4):
            for item in range(60):
                stamp = week * 7 * DAY + DAY + item * 60_000
                rows.append({
                    "addr": "0xa", "coin": "BTC", "side": "long",
                    "opened_at": stamp - 1_000, "closed_at": stamp,
                    "status": "closed", "net_pnl": 8, "fee_drag": .25,
                })
        result = summarize_campaign_stability(
            rows, now_ms=now, min_return=.04, min_net_per_closed_return=.005,
        )
        self.assertTrue(all(fold["return"] >= .04 for fold in result["folds"]))
        self.assertTrue(all(not fold["economicDensityPassed"] for fold in result["folds"]))
        self.assertFalse(result["passed"])

    def test_thirty_four_closes_earning_only_fifty_six_dollars_fails(self):
        now = 28 * DAY
        rows = []
        for week in range(4):
            per_close = 50.0 if week < 3 else 56.0 / 34.0
            for item in range(34):
                stamp = week * 7 * DAY + DAY + item * 60_000
                rows.append({
                    "addr": "0xa", "coin": "BTC", "side": "long",
                    "opened_at": stamp - 1_000, "closed_at": stamp,
                    "status": "closed", "net_pnl": per_close, "fee_drag": .25,
                })
        result = summarize_campaign_stability(
            rows, now_ms=now, min_return=.04, min_net_per_closed_return=.005,
        )
        latest = result["folds"][-1]
        self.assertEqual(latest["closedPositionN"], 34)
        self.assertAlmostEqual(latest["averageClosedNetPnl"], 56.0 / 34.0)
        self.assertFalse(latest["economicDensityPassed"])
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
