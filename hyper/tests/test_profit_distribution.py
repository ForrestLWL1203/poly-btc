import inspect
import sqlite3
import unittest

from hyper.cli import discover
from hyper.discovery import profit_distribution


def _row(addr, volume, *, pnl=-1000, roi=-0.5, account=0):
    return {
        "ethAddress": addr,
        "accountValue": account,
        "windowPerformances": [
            ("week", {"vlm": volume, "pnl": pnl, "roi": roi}),
            ("month", {"vlm": volume * 2, "pnl": pnl * 2, "roi": roi}),
        ],
    }


def _strict(wallet, return30, return7):
    def window(value):
        return {"qualificationReturn": value}
    return {
        "wallet": wallet,
        "status": "strict_complete",
        "reason": "structural_sample_collected",
        "strict": {"windows": {"30": window(return30), "14": window(0), "7": window(return7)}},
    }


class ProfitDistributionTests(unittest.TestCase):
    def test_volume_recall_deliberately_ignores_old_quality_gates(self):
        rows = profit_distribution._leaderboard_candidates([
            _row("0xbbb", 250_000, pnl=-50_000, roi=-9, account=0),
            _row("0xaaa", 249_999, pnl=100_000, roi=20, account=1_000_000),
        ], 250_000)
        self.assertEqual([row["addr"] for row in rows], ["0xbbb"])
        self.assertEqual(rows[0]["leaderboardWeekPnl"], -50_000)
        self.assertEqual(rows[0]["accountValue"], 0)

    def test_bounded_sample_is_stratified_and_keeps_current_selection(self):
        candidates = [
            {"addr": f"0x{index:040x}", "leaderboardWeekVolume": 1000 - index}
            for index in range(100)
        ]
        current = candidates[73]["addr"]
        sampled = profit_distribution._stratified_sample(
            candidates, 10, must_include={current},
        )
        addrs = {row["addr"] for row in sampled}
        self.assertEqual(len(sampled), 10)
        self.assertIn(current, addrs)
        self.assertIn(candidates[0]["addr"], addrs)
        self.assertIn(candidates[-1]["addr"], addrs)

    def test_threshold_matrix_uses_only_complete_strict_replays(self):
        wallets = [
            _strict("one", 0.50, 0.03),
            _strict("two", 0.80, 0.02),
            {"wallet": "deferred", "status": "deferred", "strict": {
                "windows": {
                    "30": {"qualificationReturn": 9},
                    "7": {"qualificationReturn": 9},
                },
            }},
        ]
        summary = profit_distribution.summarize(wallets)
        self.assertEqual(summary["strictSampleCount"], 2)
        cell = next(
            row for row in summary["thresholdMatrix"]
            if row["return30Floor"] == 0.50 and row["return7Floor"] == 0.03
        )
        self.assertEqual(cell["passed"], 1)
        self.assertEqual(cell["passRate"], 0.5)

    def test_research_gate_list_excludes_profitability_and_activity(self):
        joined = " ".join(profit_distribution.STRUCTURAL_GATES)
        self.assertNotIn("roi", joined.lower())
        self.assertNotIn("win_rate", joined.lower())
        self.assertNotIn("72", joined)
        self.assertIn("oid_bot_frequency", profit_distribution.STRUCTURAL_GATES)
        self.assertIn("source_zero_or_major_liquidation", profit_distribution.STRUCTURAL_GATES)

    def test_market_evidence_reads_legacy_coin_vol_schema(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE coin_vol(coin TEXT,sigma REAL)")
        db.execute("INSERT INTO coin_vol VALUES('BTC',0.04)")
        sigmas, context = profit_distribution._market_evidence(db)
        self.assertEqual(sigmas, {"BTC": 0.04})
        self.assertEqual(context["BTC"]["max_leverage"], None)

    def test_collector_uses_the_shared_scanner_process_lock(self):
        source = inspect.getsource(discover.main)
        self.assertIn("with scan_lock.acquire(args.db)", source)

    def test_page_cap_is_checked_only_after_structural_rejection(self):
        source = inspect.getsource(profit_distribution._rough_wallet)
        self.assertLess(
            source.index("if not structure.get(\"allowed\")"),
            source.index("if hit_cap:"),
        )


if __name__ == "__main__":
    unittest.main()
