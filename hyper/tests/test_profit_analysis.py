import json
import os
import tempfile
import unittest

from hyper.discovery import profit_analysis, profit_distribution
from hyper.copy.copy_policy import load_copy_policy


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

    def test_lower_return_tiers_keep_five_percent_recent_floor(self):
        rows = [
            profit_analysis._wallet_view(
                "twenty_five", "rough_complete", "ok",
                _record(
                    "twenty_five", 0.25, 0.15, 0.05,
                    operational=True, episodes30=10, episodes7=3,
                ),
            ),
            profit_analysis._wallet_view(
                "twenty", "rough_complete", "ok",
                _record(
                    "twenty", 0.20, 0.12, 0.05,
                    operational=True, episodes30=10, episodes7=3,
                ),
            ),
            profit_analysis._wallet_view(
                "recent_too_low", "rough_complete", "ok",
                _record(
                    "recent_too_low", 0.40, 0.20, 0.049,
                    operational=True, episodes30=10, episodes7=3,
                ),
            ),
        ]
        tiers = {
            name: sum(
                profit_analysis._tier_pass(row, floor30, floor7)
                for row in rows
            )
            for name, floor30, floor7 in profit_analysis.RETURN_TIERS
        }
        self.assertEqual(tiers["25_5"], 1)
        self.assertEqual(tiers["20_5"], 2)

    def test_gate_sensitivity_reports_false_negative_recall_and_missing_fail_open(self):
        high = profit_analysis._wallet_view(
            "high", "rough_complete", "ok",
            _record(
                "high", 0.60, 0.30, 0.12,
                operational=True, episodes30=20, episodes7=5,
            ),
        )
        high["leaderboardMonthRoi"] = 0.04
        lower = profit_analysis._wallet_view(
            "lower", "rough_complete", "ok",
            _record(
                "lower", 0.10, 0.05, 0.02,
                operational=True, episodes30=20, episodes7=5,
            ),
        )
        lower["leaderboardMonthRoi"] = 0.20
        missing = dict(lower, wallet="missing", leaderboardMonthRoi=None)
        sweeps = profit_analysis._gate_sensitivity([high, lower, missing])
        cell = next(
            row for row in sweeps
            if row["feature"] == "leaderboardMonthRoi"
            and row["threshold"] == 0.05
        )
        self.assertEqual(cell["knownPass"], 1)
        self.assertEqual(cell["missingEvidence"], 1)
        self.assertEqual(cell["failOpenCandidates"], 2)
        self.assertEqual(cell["tierOperationalTotals"]["50_10"], 1)
        self.assertEqual(cell["tierOperationalPass"]["50_10"], 0)
        self.assertEqual(cell["tierOperationalRecall"]["50_10"], 0.0)

    def test_repeatability_rejects_low_win_and_top3_dependent_wallets(self):
        row = profit_analysis._wallet_view(
            "lottery", "rough_complete", "ok",
            _record(
                "lottery", 0.60, 0.30, 0.12,
                operational=True, episodes30=20, episodes7=5,
            ),
        )
        row.update({
            "sourceWinRate30": 0.80,
            "sourceTop3ProfitShare": 0.80,
            "sourceBodyAfterTop3N": 17,
            "sourceBodyAfterTop3WinRate": 0.20,
            "sourceBodyAfterTop3Pnl": -100.0,
            "copyWinRate30": 0.80,
            "copyTop3ProfitShare": 0.80,
            "copyBodyAfterTop3N": 17,
            "copyBodyAfterTop3WinRate": 0.20,
            "copyBodyAfterTop3Pnl": -100.0,
        })
        decision = profit_analysis._repeatability_check(
            row, load_copy_policy(), copy_body_guard=True,
        )
        self.assertFalse(decision["passed"])
        self.assertEqual(
            decision["firstFailure"], "source_top3_dependent_body_weak",
        )

        row["sourceTop3ProfitShare"] = 0.20
        row["sourceWinRate30"] = 0.40
        decision = profit_analysis._repeatability_check(
            row, load_copy_policy(), copy_body_guard=True,
        )
        self.assertFalse(decision["passed"])
        self.assertEqual(decision["firstFailure"], "source_win_rate_below_floor")

    def test_conditional_lottery_guard_keeps_distributed_low_win_profit(self):
        row = profit_analysis._wallet_view(
            "asymmetric", "rough_complete", "ok",
            _record(
                "asymmetric", 0.60, 0.30, 0.12,
                operational=True, episodes30=20, episodes7=5,
            ),
        )
        row.update({
            "sourceWinRate30": 0.40,
            "sourceTop3ProfitShare": 0.20,
            "sourceBodyAfterTop3N": 17,
            "sourceBodyAfterTop3WinRate": 0.35,
            "sourceBodyAfterTop3Pnl": 1_000.0,
            "copyWinRate30": 0.40,
            "copyTop3ProfitShare": 0.20,
            "copyBodyAfterTop3N": 17,
            "copyBodyAfterTop3WinRate": 0.35,
            "copyBodyAfterTop3Pnl": 500.0,
        })
        decision = profit_analysis._conditional_lottery_check(
            row, load_copy_policy(),
        )
        self.assertTrue(decision["passed"])

        row["copyBodyAfterTop3Pnl"] = -1.0
        decision = profit_analysis._conditional_lottery_check(
            row, load_copy_policy(),
        )
        self.assertFalse(decision["passed"])
        self.assertEqual(
            decision["firstFailure"], "copy_low_win_losing_body",
        )


if __name__ == "__main__":
    unittest.main()
