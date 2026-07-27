import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hyper import storage
from hyper.discovery import perp_prefilter, scanner


def _window(start, end):
    step = (end - start) / 4
    return {
        "pnlHistory": [
            [index * 7 * 86400_000, str(start + index * step)]
            for index in range(5)
        ],
        "accountValueHistory": [
            [index * 7 * 86400_000, "1"]
            for index in range(5)
        ],
    }


def _stability_window(returns, *, equity=10_000):
    pnl = 0.0
    pnl_history = [[0, "0"]]
    equity_history = [[0, str(equity)]]
    for index, value in enumerate(returns, 1):
        pnl += float(value) * equity
        pnl_history.append([index * 7 * 86400_000, str(pnl)])
        equity_history.append([index * 7 * 86400_000, str(equity)])
    return {"pnlHistory": pnl_history, "accountValueHistory": equity_history}


def _daily_window(days, *, equity=10_000, pnl_by_day=None, leading_zero_days=0):
    pnl_by_day = dict(pnl_by_day or {})
    pnl = 0.0
    pnl_history = []
    equity_history = []
    for day in range(days + 1):
        pnl += float(pnl_by_day.get(day, 0.0))
        pnl_history.append([day * 86400_000, str(pnl)])
        account_value = 0.0 if day < leading_zero_days else float(equity)
        equity_history.append([day * 86400_000, str(account_value)])
    return {"pnlHistory": pnl_history, "accountValueHistory": equity_history}


def _replace_month(payload, window):
    return [
        [name, window] if name in {"month", "perpMonth"} else row
        for row in payload
        for name in [row[0]]
    ]


def _portfolio(*, total=(6000, 18000, 25000), perp=(5000, 15000, 20000)):
    rows = []
    for label, value in zip(("week", "month", "allTime"), total):
        rows.append([label, _window(0, value)])
    for label, value in zip(("perpWeek", "perpMonth", "perpAllTime"), perp):
        rows.append([label, _window(0, value)])
    return rows


class PerpPrefilterTests(unittest.TestCase):
    minima = {"week": 5000, "month": 15000, "all": 20000}

    def test_accepts_month_boundary_and_keeps_other_windows_for_audit(self):
        result = perp_prefilter.evaluate(
            _portfolio(total=(6250, 18750, 25000), perp=(5000, 15000, 20000)),
            pnl_minima=self.minima, share_min=0.8,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.windows["week"]["perpShare"], 0.8)
        self.assertEqual(result.windows["month"]["perpShare"], 0.8)
        self.assertGreater(result.windows["officialPerp30d"]["return"], 0.20)

    def test_week_and_lifetime_are_audit_only(self):
        weak_week = perp_prefilter.evaluate(
            _portfolio(total=(6250, 18750, 25000), perp=(4999, 15000, 20000)),
            pnl_minima=self.minima, share_min=0.8,
        )
        self.assertTrue(weak_week.passed)
        weak_all = perp_prefilter.evaluate(
            _portfolio(total=(6250, 18750, 25000), perp=(5000, 15000, 19999)),
            pnl_minima=self.minima, share_min=0.8,
        )
        self.assertTrue(weak_all.passed)

    def test_rejects_non_profitable_month_perp(self):
        result = perp_prefilter.evaluate(
            _portfolio(total=(6250, 18750, 25000), perp=(5000, 0, 20000)),
            pnl_minima=self.minima, share_min=0.8,
        )
        self.assertEqual(result.reason, "perp_pnl_not_profitable:month")

    def test_rejects_spot_or_vault_dominated_profit(self):
        result = perp_prefilter.evaluate(
            _portfolio(total=(6250, 20000, 25000), perp=(5000, 15000, 20000)),
            pnl_minima=self.minima, share_min=0.8,
        )
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "perp_share_below_floor:month")

    def test_missing_window_and_network_shape_are_deferred(self):
        self.assertTrue(perp_prefilter.evaluate(None, pnl_minima=self.minima, share_min=0.8).deferred)
        partial = [row for row in _portfolio() if row[0] != "perpMonth"]
        result = perp_prefilter.evaluate(partial, pnl_minima=self.minima, share_min=0.8)
        self.assertTrue(result.deferred)
        self.assertEqual(result.reason, "portfolio_window_missing:month")

    def test_official_month_uses_full_window_not_four_week_vetoes(self):
        payload = _portfolio()
        payload = [
            [name, _stability_window([0.06, 0.04, 0.07, 0.08])]
            if name == "perpMonth" else row
            for row in payload
            for name in [row[0]]
        ]
        result = perp_prefilter.evaluate(payload, pnl_minima=self.minima, share_min=0.1)
        self.assertTrue(result.passed)
        self.assertAlmostEqual(result.windows["officialPerp30d"]["return"], .25)

    def test_exact_sparse_month_endpoints_are_valid_official_evidence(self):
        payload = _portfolio()
        sparse = {
            "pnlHistory": [[0, "0"], [28 * 86400_000, "4000"]],
            "accountValueHistory": [[0, "10000"], [28 * 86400_000, "14000"]],
        }
        payload = [
            [name, sparse] if name == "perpMonth" else row
            for row in payload
            for name in [row[0]]
        ]
        result = perp_prefilter.evaluate(payload, pnl_minima=self.minima, share_min=0.1)
        self.assertTrue(result.passed)
        self.assertAlmostEqual(result.windows["officialPerp30d"]["return"], .40)

    def test_official_perp_return_twenty_percent_boundary_is_inclusive(self):
        payload = _portfolio()
        exact = {
            "pnlHistory": [[0, "0"], [28 * 86400_000, "2000"]],
            "accountValueHistory": [[0, "10000"], [28 * 86400_000, "12000"]],
        }
        payload = [
            [name, exact] if name == "perpMonth" else row
            for row in payload
            for name in [row[0]]
        ]
        passed = perp_prefilter.evaluate(
            payload, pnl_minima=self.minima, share_min=0.1, min_return_30d=.20,
        )
        exact["pnlHistory"][-1][1] = "1999"
        failed = perp_prefilter.evaluate(
            payload, pnl_minima=self.minima, share_min=0.1, min_return_30d=.20,
        )

        self.assertTrue(passed.passed)
        self.assertEqual(failed.reason, "official_perp_return_below_floor:month")

    def test_fifteen_day_wallet_uses_all_observed_funded_history(self):
        short = _daily_window(
            15,
            pnl_by_day={day: 500 / 7 for day in range(9, 16)},
        )
        result = perp_prefilter.evaluate(
            _replace_month(_portfolio(), short),
            pnl_minima=self.minima,
            share_min=0.1,
        )
        evidence = result.windows["officialPerp30d"]

        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "perp_prefilter_passed_short_history")
        self.assertEqual(evidence["historyTier"], "short_history_7d")
        self.assertAlmostEqual(evidence["windowDays"], 15.0)
        self.assertAlmostEqual(evidence["return"], .05)
        self.assertAlmostEqual(evidence["minimumReturn"], .05)

    def test_short_history_seven_day_return_below_five_percent_is_rejected(self):
        short = _daily_window(
            15,
            pnl_by_day={day: 499 / 7 for day in range(9, 16)},
        )
        result = perp_prefilter.evaluate(
            _replace_month(_portfolio(), short),
            pnl_minima=self.minima,
            share_min=0.1,
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, "official_perp_return_below_floor:short_7d")

    def test_history_under_seven_days_is_deferred_not_business_rejection(self):
        short = _daily_window(6, pnl_by_day={day: 100 for day in range(1, 7)})
        result = perp_prefilter.evaluate(
            _replace_month(_portfolio(), short),
            pnl_minima=self.minima,
            share_min=0.1,
        )

        self.assertTrue(result.deferred)
        self.assertEqual(result.reason, "history_under_7d")

    def test_leading_zero_is_skipped_when_positive_history_still_covers_28_days(self):
        funded = _daily_window(
            30,
            leading_zero_days=2,
            pnl_by_day={day: 2000 / 28 for day in range(3, 31)},
        )
        result = perp_prefilter.evaluate(
            _replace_month(_portfolio(), funded),
            pnl_minima=self.minima,
            share_min=0.1,
        )
        evidence = result.windows["officialPerp30d"]

        self.assertTrue(result.passed)
        self.assertEqual(evidence["historyTier"], "full_history")
        self.assertGreaterEqual(evidence["positiveCoverageDays"], 28)
        self.assertAlmostEqual(evidence["return"], .20)

    def test_leading_zero_with_fifteen_positive_days_uses_short_history(self):
        funded = _daily_window(
            20,
            leading_zero_days=6,
            pnl_by_day={day: 500 / 7 for day in range(14, 21)},
        )
        result = perp_prefilter.evaluate(
            _replace_month(_portfolio(), funded),
            pnl_minima=self.minima,
            share_min=0.1,
        )

        self.assertTrue(result.passed)
        self.assertEqual(
            result.windows["officialPerp30d"]["historyTier"],
            "short_history_7d",
        )

    def test_temporary_full_withdrawal_compounds_funded_segments(self):
        pnl_history = []
        equity_history = []
        pnl = 0.0
        for day in range(31):
            if 1 <= day <= 10:
                pnl += 100.0
            elif 14 <= day <= 30:
                pnl += 200.0
            pnl_history.append([day * 86400_000, str(pnl)])
            equity = 0.0 if day in {11, 12} else (10_000.0 if day <= 10 else 20_000.0)
            equity_history.append([day * 86400_000, str(equity)])
        window = {
            "pnlHistory": pnl_history,
            "accountValueHistory": equity_history,
        }

        evidence = perp_prefilter.official_perp_month_return(window)

        self.assertTrue(evidence["evidenceSufficient"])
        self.assertEqual(evidence["historyTier"], "full_history")
        self.assertEqual(evidence["fundedSegmentCount"], 2)
        self.assertEqual(evidence["fundingResetCount"], 1)
        self.assertAlmostEqual(evidence["fundedCoverageDays"], 28.0)
        self.assertAlmostEqual(evidence["return"], (1.10 * 1.17) - 1.0)

    def test_redeposit_uses_each_segments_own_capital_base(self):
        window = {
            "pnlHistory": [
                [0, "0"], [8 * 86400_000, "100"], [9 * 86400_000, "100"],
                [10 * 86400_000, "100"], [30 * 86400_000, "10100"],
            ],
            "accountValueHistory": [
                [0, "1000"], [8 * 86400_000, "1100"], [9 * 86400_000, "0"],
                [10 * 86400_000, "100000"], [30 * 86400_000, "110000"],
            ],
        }

        evidence = perp_prefilter.official_perp_month_return(window)

        self.assertTrue(evidence["evidenceSufficient"])
        self.assertAlmostEqual(evidence["return"], .21)
        self.assertLess(evidence["return"], 1.0)

    def test_liquidation_to_zero_cannot_be_repaired_by_redeposit(self):
        window = {
            "pnlHistory": [
                [0, "0"], [10 * 86400_000, "0"], [11 * 86400_000, "-10000"],
                [13 * 86400_000, "-10000"], [30 * 86400_000, "-7000"],
            ],
            "accountValueHistory": [
                [0, "10000"], [10 * 86400_000, "10000"], [11 * 86400_000, "0"],
                [13 * 86400_000, "10000"], [30 * 86400_000, "13000"],
            ],
        }

        evidence = perp_prefilter.official_perp_month_return(window)

        self.assertTrue(evidence["evidenceSufficient"])
        self.assertAlmostEqual(evidence["return"], -1.0)

    def test_short_sparse_history_uses_exact_observed_endpoints(self):
        sparse = {
            "pnlHistory": [[0, "0"], [15 * 86400_000, "500"]],
            "accountValueHistory": [[0, "10000"], [15 * 86400_000, "10500"]],
        }
        result = perp_prefilter.evaluate(
            _replace_month(_portfolio(), sparse),
            pnl_minima=self.minima,
            share_min=0.1,
        )

        self.assertTrue(result.passed)
        self.assertAlmostEqual(
            result.windows["officialPerp30d"]["windowDays"], 15.0,
        )

    def test_leveraged_volume_does_not_affect_leaderboard_decision(self):
        def row(volume):
            return {"ethAddress": "0x1", "accountValue": 30000, "windowPerformances": [
                ("week", {"pnl": 2000, "roi": 0.15, "vlm": volume}),
                ("month", {"pnl": 8000, "roi": 0.30, "vlm": volume * 2}),
                ("allTime", {"pnl": 0, "roi": 0.30, "vlm": volume * 3}),
            ]}
        class P:
            min_acct = 30000
            week_vlm_min = 300000
            week_roi_min = 0.15
            month_roi_min = 0.30
            all_roi_min = 0.30
            week_pnl_min = 2000
            month_pnl_min = 8000
            all_pnl_min = 0
        low = scanner._prepare_leaderboard_rows([row(300000)], P(), "now")[0]
        high = scanner._prepare_leaderboard_rows([row(300000000)], P(), "now")[0]
        self.assertEqual((low["is_candidate"], high["is_candidate"]), (1, 1))

    def test_leaderboard_roi_is_audit_only_and_positive_week_month_pnl_are_recall_gates(self):
        base = {"ethAddress": "0x1", "accountValue": 30000, "windowPerformances": [
            ("week", {"pnl": 2000, "roi": 0.15, "vlm": 300000}),
            ("month", {"pnl": 8000, "roi": 0.30, "vlm": 600000}),
            ("allTime", {"pnl": 0, "roi": 0.30, "vlm": 900000}),
        ]}
        class P:
            min_acct = 30000
            week_vlm_min = 300000
            week_roi_min, month_roi_min, all_roi_min = 0.15, 0.30, 0.30
            week_pnl_min, month_pnl_min, all_pnl_min = 2000, 8000, 0
            roi_windows_min_pass = 2
        self.assertEqual(scanner._prepare_leaderboard_rows([base], P(), "now")[0]["is_candidate"], 1)
        all_roi_miss = {**base, "windowPerformances": [
            (name, {**values, "roi": -10.0}) for name, values in base["windowPerformances"]
        ]}
        self.assertEqual(scanner._prepare_leaderboard_rows([all_roi_miss], P(), "now")[0]["is_candidate"], 1)
        all_time_only_miss = {**base, "windowPerformances": [
            (name, {**values, "roi": -10.0}) if name == "allTime" else (name, dict(values))
            for name, values in base["windowPerformances"]
        ]}
        self.assertEqual(
            scanner._prepare_leaderboard_rows([all_time_only_miss], P(), "now")[0]["is_candidate"], 1,
        )
        week_roi_miss = {**base, "windowPerformances": [
            (name, {**values, "roi": -10.0}) if name == "week" else (name, dict(values))
            for name, values in base["windowPerformances"]
        ]}
        self.assertEqual(
            scanner._prepare_leaderboard_rows([week_roi_miss], P(), "now")[0]["is_candidate"], 1,
        )
        weak_week = {**base, "windowPerformances": [
            (name, {**values, "pnl": 1999.0}) if name == "week" else (name, dict(values))
            for name, values in base["windowPerformances"]
        ]}
        weak_month = {**base, "windowPerformances": [
            (name, {**values, "pnl": 7999.0}) if name == "month" else (name, dict(values))
            for name, values in base["windowPerformances"]
        ]}
        self.assertEqual(scanner._prepare_leaderboard_rows([weak_week], P(), "now")[0]["is_candidate"], 0)
        self.assertEqual(scanner._prepare_leaderboard_rows([weak_month], P(), "now")[0]["is_candidate"], 0)

    def test_recent_exact_policy_portfolio_result_is_reused_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA)
            db.row_factory = sqlite3.Row
            policy = SimpleNamespace(
                week_pnl_min=5000, month_pnl_min=15000, all_pnl_min=20000,
                perp_pnl_share_min=0.8,
            )
            payload = _portfolio()
            with mock.patch.object(scanner.rest, "portfolio", return_value=payload) as fetch:
                first = scanner._run_perp_prefilter(db, ["0xabc"], policy, "scan-one")
                second = scanner._run_perp_prefilter(db, ["0xabc"], policy, "scan-two")
            self.assertTrue(first["0xabc"].passed)
            self.assertTrue(second["0xabc"].passed)
            fetch.assert_called_once_with("0xabc")
            audit = json.loads(db.execute(
                "SELECT payload_json FROM pipeline_audit WHERE stamp='scan-two' AND addr='0xabc'"
            ).fetchone()[0])
            self.assertTrue(audit["cacheHit"])

            changed = SimpleNamespace(**{**vars(policy), "perp_pnl_share_min": 0.9})
            with mock.patch.object(scanner.rest, "portfolio", return_value=payload) as refetch:
                scanner._run_perp_prefilter(db, ["0xabc"], changed, "scan-three")
            refetch.assert_called_once_with("0xabc")

            with mock.patch.object(scanner.rest, "portfolio", return_value=payload) as full_refetch:
                scanner._run_perp_prefilter(
                    db, ["0xabc"], policy, "scan-full", allow_cache=False,
                )
            full_refetch.assert_called_once_with("0xabc")
            full_audit = json.loads(db.execute(
                "SELECT payload_json FROM pipeline_audit WHERE stamp='scan-full' AND addr='0xabc'"
            ).fetchone()[0])
            self.assertFalse(full_audit["cacheHit"])


if __name__ == "__main__":
    unittest.main()
