import json
import os
import tempfile
import unittest

from hyper.discovery import profit_analysis, profit_distribution


def _record(wallet, return30, return14, return7, *, operational, episodes30, episodes7):
    def window(value, closed):
        return {
            "qualificationReturn": value,
            "closedEpisodes": closed,
            "wins": max(0, closed - 1),
            "liquidations": 0,
            "openLossRatio": 0.0,
            "actionableOpenRate": 1.0,
            "pathCompletionRate": 1.0,
        }
    return {
        "wallet": wallet,
        "status": "rough_complete",
        "reason": "structural_sample_collected",
        "leaderboardWeekVolume": 500_000,
        "officialPerpWeekVolume": 400_000,
        "leaderboardMonthRoi": 0.04,
        "source": {
            "source_episode_n_30d": episodes30,
            "source_episode_n_7d": episodes7,
            "source_win_rate_30d": 0.8,
            "source_win_rate_7d": 0.8,
            "medianHoldSeconds": 7_200,
            "medianEpisodesPerActiveDay": 2,
        },
        "current": {"accountValue": 50_000, "openLossFraction": 0.0},
        "rough": {"windows": {
            "30": window(return30, episodes30),
            "14": window(return14, max(1, episodes30 // 2)),
            "7": window(return7, episodes7),
        }},
        "activity": {
            "operational": operational,
            "activeWeeks4": 4 if operational else 1,
            "weeklyOpenCountsOldestFirst": [1, 1, 1, 1] if operational else [0, 0, 0, 1],
            "actionableOpenEvents28d": 4 if operational else 1,
            "actionableOpenEvents7d": 1,
            "maxOpenGapDays28d": 7 if operational else 21,
            "reason": "operational_activity" if operational else "active_weeks_below_3_of_4",
        },
    }


class ProfitAnalysisTests(unittest.TestCase):
    def test_analyzer_separates_high_return_operational_and_sparse_wallets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "research.db")
            report_path = os.path.join(tmp, "report.json")
            cache = profit_distribution._research_cache(db_path)
            profit_distribution._cache_run_context(
                cache, "run", report_path, "2026-01-01T00:00:00+00:00", {
                    "minimumPerpWeekVolume": 250_000,
                    "leaderboard": [],
                },
            )
            for wallet, operational in (("wallet_active", True), ("wallet_sparse", False)):
                record = _record(
                    wallet, 0.60, 0.30, 0.12,
                    operational=operational, episodes30=20, episodes7=5,
                )
                profit_distribution._cache_rough_record(
                    cache, "run",
                    {"wallet": wallet, "addr": "0xprivate"},
                    record, None, {"candidate": {"addr": "0xprivate"}},
                )
            cache.commit()
            cache.close()

            report = profit_analysis.analyze(db_path, report_path, run_key="run")
            tier = report["returnTiers"]["50_10"]
            self.assertEqual(tier["roughPassed"], 2)
            self.assertEqual(tier["operationalPassed"], 1)
            self.assertEqual(tier["activitySparse"], 1)
            self.assertEqual(
                tier["operationalWallets"][0]["wallet"], "wallet_active",
            )
            text = json.dumps(report)
            self.assertNotIn("0xprivate", text)
            self.assertEqual(os.stat(report_path).st_mode & 0o777, 0o600)

    def test_sample_depth_grid_reports_high_return_recall(self):
        rows = [
            profit_analysis._wallet_view(
                "deep", "rough_complete", "ok",
                _record("deep", 0.60, 0.30, 0.12, operational=True, episodes30=20, episodes7=5),
            ),
            profit_analysis._wallet_view(
                "thin", "rough_complete", "ok",
                _record("thin", 0.55, 0.30, 0.11, operational=True, episodes30=2, episodes7=1),
            ),
        ]
        grid = profit_analysis._sample_depth_grid(rows)
        cell = next(
            row for row in grid
            if row["minimumSourceEpisodes30"] == 8
            and row["minimumSourceEpisodes7"] == 3
        )
        self.assertEqual(cell["wallets"], 1)
        self.assertEqual(cell["tierCounts"]["50_10"], 1)
        self.assertEqual(cell["tierRecall"]["50_10"], 0.5)


if __name__ == "__main__":
    unittest.main()
