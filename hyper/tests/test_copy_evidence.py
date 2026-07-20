import unittest

from hyper.copy_evidence import summarize_copy_evidence


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


if __name__ == "__main__":
    unittest.main()
