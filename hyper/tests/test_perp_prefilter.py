import unittest

from hyper.discovery import perp_prefilter, scanner


def _window(start, end):
    return {"pnlHistory": [[1, str(start)], [2, str(end)]], "accountValueHistory": [[1, "1"], [2, "1"]]}


def _portfolio(*, total=(6000, 18000, 25000), perp=(5000, 15000, 20000)):
    rows = []
    for label, value in zip(("week", "month", "allTime"), total):
        rows.append([label, _window(0, value)])
    for label, value in zip(("perpWeek", "perpMonth", "perpAllTime"), perp):
        rows.append([label, _window(0, value)])
    return rows


class PerpPrefilterTests(unittest.TestCase):
    minima = {"week": 5000, "month": 15000, "all": 20000}

    def test_accepts_all_boundaries_at_eighty_percent(self):
        result = perp_prefilter.evaluate(
            _portfolio(total=(6250, 18750, 25000)), pnl_minima=self.minima, share_min=0.8,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.windows["week"]["perpShare"], 0.8)

    def test_rejects_spot_or_vault_dominated_profit(self):
        result = perp_prefilter.evaluate(
            _portfolio(total=(10000, 18750, 25000)), pnl_minima=self.minima, share_min=0.8,
        )
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "perp_share_below_floor:week")

    def test_missing_window_and_network_shape_are_deferred(self):
        self.assertTrue(perp_prefilter.evaluate(None, pnl_minima=self.minima, share_min=0.8).deferred)
        partial = _portfolio()[:-1]
        result = perp_prefilter.evaluate(partial, pnl_minima=self.minima, share_min=0.8)
        self.assertTrue(result.deferred)
        self.assertEqual(result.reason, "portfolio_window_missing:all")

    def test_leveraged_volume_does_not_affect_leaderboard_decision(self):
        def row(volume):
            return {"ethAddress": "0x1", "accountValue": 30000, "windowPerformances": [
                ("week", {"pnl": 5000, "roi": 0.25, "vlm": volume}),
                ("month", {"pnl": 15000, "roi": 0.50, "vlm": volume * 2}),
                ("allTime", {"pnl": 20000, "roi": 0.50, "vlm": volume * 3}),
            ]}
        class P:
            min_acct = 30000
            week_vlm_min = 300000
            week_roi_min = 0.25
            month_roi_min = 0.50
            all_roi_min = 0.50
            week_pnl_min = 5000
            month_pnl_min = 15000
            all_pnl_min = 20000
        low = scanner._prepare_leaderboard_rows([row(300000)], P(), "now")[0]
        high = scanner._prepare_leaderboard_rows([row(300000000)], P(), "now")[0]
        self.assertEqual((low["is_candidate"], high["is_candidate"]), (1, 1))

    def test_each_official_roi_and_absolute_pnl_floor_is_hard(self):
        base = {"ethAddress": "0x1", "accountValue": 30000, "windowPerformances": [
            ("week", {"pnl": 5000, "roi": 0.25, "vlm": 300000}),
            ("month", {"pnl": 15000, "roi": 0.50, "vlm": 600000}),
            ("allTime", {"pnl": 20000, "roi": 0.50, "vlm": 900000}),
        ]}
        class P:
            min_acct = 30000
            week_vlm_min = 300000
            week_roi_min, month_roi_min, all_roi_min = 0.25, 0.50, 0.50
            week_pnl_min, month_pnl_min, all_pnl_min = 5000, 15000, 20000
        self.assertEqual(scanner._prepare_leaderboard_rows([base], P(), "now")[0]["is_candidate"], 1)
        for window_name, field in (("week", "roi"), ("month", "roi"), ("allTime", "roi"),
                                   ("week", "pnl"), ("month", "pnl"), ("allTime", "pnl")):
            row = {**base, "windowPerformances": [
                (name, {**values, field: float(values[field]) - 0.01})
                if name == window_name else (name, dict(values))
                for name, values in base["windowPerformances"]
            ]}
            self.assertEqual(scanner._prepare_leaderboard_rows([row], P(), "now")[0]["is_candidate"], 0)


if __name__ == "__main__":
    unittest.main()
