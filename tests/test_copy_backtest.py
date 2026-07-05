import unittest

from hl.copy_backtest import run_backtest


def fill(t, coin, side, sz, start, px, oid, crossed=True):
    return {
        "time": t,
        "tid": t,
        "coin": coin,
        "side": side,
        "sz": str(sz),
        "startPosition": str(start),
        "px": str(px),
        "oid": oid,
        "crossed": crossed,
    }


class CopyBacktestTests(unittest.TestCase):
    def test_smart_add_skips_small_adverse_add_and_reports_dependency(self):
        fills = [
            fill(1, "ZEC", "A", 100, 0, 100.0, 10),
            fill(2, "ZEC", "A", 100, -100, 100.5, 11),
            fill(3, "ZEC", "B", 200, -200, 101.0, 12),
        ]

        result = run_backtest("0xabc", fills, sigmas={"ZEC": 0.10})

        self.assertEqual(result["closed_n"], 1)
        self.assertEqual(result["wins"], 0)
        self.assertEqual(result["missed_adds"], 1)
        self.assertEqual(result["followed_adds"], 0)
        self.assertGreater(result["add_dependency"], 0.9)
        self.assertGreater(result["fee_drag"], 0)
        self.assertLess(result["copy_net_pnl"], 0)

    def test_smart_add_follows_large_adverse_add(self):
        fills = [
            fill(1, "ZEC", "B", 100, 0, 100.0, 20),
            fill(2, "ZEC", "B", 100, 100, 98.0, 21),
            fill(3, "ZEC", "A", 200, 200, 101.0, 22),
        ]

        result = run_backtest("0xabc", fills, sigmas={"ZEC": 0.10})

        self.assertEqual(result["closed_n"], 1)
        self.assertEqual(result["missed_adds"], 0)
        self.assertEqual(result["followed_adds"], 1)
        self.assertGreater(result["copy_net_pnl"], 0)


if __name__ == "__main__":
    unittest.main()
