import unittest

from hyper.selection import volatility_sizing_lab as lab


class VolatilitySizingLabTests(unittest.TestCase):
    def setUp(self):
        self.follow = {
            "STABLE_MARGIN_PCT": .055,
            "MID_MARGIN_PCT": .035,
            "HIGH_MARGIN_PCT": .045,
            "STABLE_LEV_CAP": 30,
            "MID_LEV_CAP": 10,
            "HIGH_LEV_CAP": 6,
        }

    def test_search_is_bounded_and_not_a_leverage_cartesian_product(self):
        initial = lab.initial_candidates(self.follow, .025)
        coordinate = lab.coordinate_candidates(self.follow, .025, .85)

        self.assertEqual(len(initial), 3)
        self.assertEqual(len(coordinate), 4)
        self.assertEqual(
            {(row["midLeverage"], row["highLeverage"]) for row in coordinate},
            {(10, 6), (15, 6), (12, 5), (12, 8)},
        )
        self.assertTrue(all(
            row["overrides"]["STABLE_MARGIN_PCT"] == .05
            and row["overrides"]["STABLE_LEV_CAP"] == 30
            for row in initial + coordinate
        ))

    def test_candidate_settings_are_private_replay_overrides(self):
        candidate = lab.build_candidate(
            self.follow, name="probe", btc_sigma=.025,
            risk_scale=.85, mid_leverage=15, high_leverage=8,
        )

        self.assertNotIn("_VOLATILITY_NOTIONAL_SIZING", self.follow)
        self.assertEqual(candidate["overrides"]["MID_LEV_CAP"], 15)
        self.assertEqual(candidate["overrides"]["HIGH_LEV_CAP"], 8)
        self.assertEqual(
            candidate["overrides"]["_VOLATILITY_NOTIONAL_SIZING"]["risk_scale"],
            .85,
        )

    def test_strict_shortlist_fills_three_distinct_slots_when_champions_overlap(self):
        candidates = []
        for index, pnl in enumerate((130.0, 125.0, 121.0, 80.0)):
            candidates.append({
                "name": f"candidate_{index}",
                "windows": {
                    "30": {
                        "netPnl": pnl, "liquidations": index,
                        "maxDrawdown": .05 + index * .01, "capacityFit": .90 - index * .01,
                    },
                    "14": {"netPnl": 10.0},
                    "7": {"netPnl": 5.0},
                },
            })

        shortlisted = lab._strict_shortlist(candidates, limit=3)

        self.assertEqual(len(shortlisted), 3)
        self.assertEqual(len({row["name"] for row in shortlisted}), 3)


if __name__ == "__main__":
    unittest.main()
