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


if __name__ == "__main__":
    unittest.main()
