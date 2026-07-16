import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hl import scanner_copy_bt


class ScannerCopyBacktestSafetyTests(unittest.TestCase):
    def _params(self):
        return SimpleNamespace(
            copy_bt_days=30,
            copy_bt_sigmas={},
            copy_bt_overrides={},
            copy_bt_market_ctx={},
            scan_generation="gen-1",
        )

    def test_empty_window_returns_explicit_no_evidence(self):
        result = scanner_copy_bt.copy_bt_result("0xabc", [], 30 * 86400_000, self._params())

        self.assertTrue(result["valid"])
        self.assertFalse(result["has_evidence"])
        self.assertEqual(result["evidence_status"], "no_fills")

    def test_replay_exception_returns_explicit_invalid_result(self):
        fill = {
            "time": 1,
            "tid": 1,
            "coin": "BTC",
            "side": "B",
            "sz": "1",
            "px": "100",
            "startPosition": "0",
        }
        with patch.object(scanner_copy_bt, "run_backtest", side_effect=RuntimeError("boom")):
            result = scanner_copy_bt.copy_bt_result("0xabc", [fill], 30 * 86400_000, self._params())

        self.assertFalse(result["valid"])
        self.assertEqual(result["data_status"], "replay_error")
        self.assertEqual(result["evidence_status"], "invalid")
        self.assertNotIn("error", result)

    def test_historical_cutoff_never_sees_future_cached_fills(self):
        now = 30 * 86_400_000
        fill = {
            "time": now + 1, "tid": 1, "coin": "BTC", "side": "B",
            "sz": "1", "px": "100", "startPosition": "0",
        }

        result = scanner_copy_bt.copy_bt_result("0xabc", [fill], now, self._params())

        self.assertTrue(result["valid"])
        self.assertFalse(result["has_evidence"])
        self.assertEqual(result["evidence_status"], "no_fills")

    def test_warmup_restores_position_opened_before_30_day_boundary(self):
        day = 86_400_000
        now = 40 * day
        addr = "0xabc"
        fills = [
            {"user": addr, "time": now - 32 * day, "tid": 1, "coin": "BTC", "side": "B",
             "sz": "100", "startPosition": "0", "px": "100", "oid": 1, "crossed": True},
            {"user": addr, "time": now - 2 * day, "tid": 2, "coin": "BTC", "side": "A",
             "sz": "100", "startPosition": "100", "px": "110", "oid": 2, "crossed": True},
        ]

        direct = scanner_copy_bt.copy_bt_result(addr, fills, now, self._params(), days=30)
        windows = scanner_copy_bt.copy_bt_results(addr, fills, now, self._params())

        self.assertEqual(direct["closed_n"], 0)
        self.assertEqual(windows[30]["closed_n"], 1)
        self.assertGreater(windows[30]["copy_net_pnl"], 0)

    def test_open_position_terminal_mark_is_included_in_every_economic_window(self):
        day = 86_400_000
        now = 40 * day
        addr = "0xabc"
        fills = [{
            "user": addr, "time": now - day, "tid": 1, "coin": "BTC", "side": "B",
            "sz": "100", "startPosition": "0", "px": "100", "oid": 1, "crossed": True,
        }]

        windows = scanner_copy_bt.copy_bt_results(
            addr, fills, now, self._params(), valuation_marks={"BTC": 90},
        )

        for days in (30, 14, 7):
            self.assertEqual(windows[days]["valuation_status"], "complete")
            self.assertLess(windows[days]["unrealized_pnl"], 0)
            self.assertAlmostEqual(
                windows[days]["copy_net_pnl"],
                windows[days]["closed_net_pnl"] + windows[days]["unrealized_pnl"],
            )

    def test_open_position_without_terminal_mark_is_not_core_safe(self):
        day = 86_400_000
        now = 40 * day
        addr = "0xabc"
        fills = [{
            "user": addr, "time": now - day, "tid": 1, "coin": "BTC", "side": "B",
            "sz": "100", "startPosition": "0", "px": "100", "oid": 1, "crossed": True,
        }]

        windows = scanner_copy_bt.copy_bt_results(addr, fills, now, self._params())

        self.assertEqual(windows[7]["valuation_status"], "missing_marks")


if __name__ == "__main__":
    unittest.main()
