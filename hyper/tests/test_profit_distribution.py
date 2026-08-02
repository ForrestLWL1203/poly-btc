import inspect
import json
import os
import sqlite3
import tempfile
import unittest
import zlib

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


def _rough(wallet, return30, return7):
    def window(value):
        return {"qualificationReturn": value}
    return {
        "wallet": wallet,
        "status": "rough_complete",
        "rough": {"windows": {"30": window(return30), "14": window(0), "7": window(return7)}},
    }


def _activity_results(now_ms, offsets_and_details):
    events = []
    for offset_days, detail in offsets_and_details:
        events.append({
            "time": now_ms - int(offset_days * profit_distribution.DAY_MS),
            "outcome": detail.get("outcome", "opened"),
            "minimum_notional": detail.get("minimum", 2_500),
            "master_notional": detail.get("master", 5_000),
        })
    return {30: {"open_events": events}}


class ProfitDistributionTests(unittest.TestCase):
    def test_cli_summary_accepts_targeted_resume_without_fresh_recall_count(self):
        result = discover._profit_distribution_cli_result({
            "status": "rough_complete",
            "sampledCandidates": 5_448,
            "summary": {"strictSampleCount": 0},
        }, "rough.json")
        self.assertEqual(result["status"], "rough_complete")
        self.assertEqual(result["sampledCandidates"], 5_448)
        self.assertEqual(result["strictSampleCount"], 0)
        self.assertIsNone(result["leaderboardVolumeRecall"])

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

    def test_collector_has_a_deeper_page_cap_recovery_stage(self):
        source = inspect.getsource(profit_distribution.run)
        self.assertIn("progress(\"history_repair\"", source)
        self.assertIn("recovery_pages", source)

    def test_bounded_strict_replay_uses_rough_profit_priority(self):
        rows = [
            _rough("recent", 0.40, 0.50),
            _rough("month", 0.60, 0.00),
            _rough("weak", 0.10, 0.10),
        ]
        ranked = sorted(rows, key=profit_distribution._rough_profit_sort_key)
        self.assertEqual([row["wallet"] for row in ranked], ["recent", "month", "weak"])
        source = inspect.getsource(profit_distribution.run)
        self.assertIn("strict_replay_inputs", source)
        self.assertIn("strictRankingMode", source)

    def test_activity_counts_small_source_opens_and_weekly_continuity(self):
        now_ms = 40 * profit_distribution.DAY_MS
        results = _activity_results(now_ms, [
            (26, {}), (19, {}), (12, {}), (5, {}),
            # Legacy threshold metadata no longer filters a valid source opening.
            (2, {"outcome": "skip_small_notl", "master": 100}),
        ])
        activity = profit_distribution._copy_activity(results, now_ms)
        self.assertEqual(activity["weeklyOpenCountsOldestFirst"], [1, 1, 1, 2])
        self.assertEqual(activity["activeWeeks4"], 4)
        self.assertEqual(activity["actionableOpenEvents28d"], 5)
        self.assertTrue(activity["continuous4of4"])
        self.assertTrue(activity["operational"])

    def test_activity_rejects_one_trade_windfall_before_strict(self):
        now_ms = 40 * profit_distribution.DAY_MS
        activity = profit_distribution._copy_activity(
            _activity_results(now_ms, [(2, {})]), now_ms,
        )
        self.assertEqual(activity["activeWeeks4"], 1)
        self.assertFalse(activity["operational"])
        self.assertEqual(activity["reason"], "active_weeks_below_3_of_4")

    def test_history_repair_checkpoint_precedes_strict_and_supports_rough_only(self):
        source = inspect.getsource(profit_distribution.run)
        checkpoint = source.index("_atomic_json(report_path, pre_strict_report)")
        strict_path = source.index("path_audit = price_path.ensure")
        self.assertLess(checkpoint, strict_path)
        self.assertIn("if rough_only:", source)
        self.assertIn("operational_wallets", source)
        self.assertIn("_load_cached_replay", source)

    def test_resume_mode_is_rough_only_and_refreshes_capped_plus_profit_prefix(self):
        source = inspect.getsource(profit_distribution.resume_rough)
        self.assertIn("capped_ids | ranked_ids", source)
        self.assertLess(source.index("ranked_order"), source.index("capped_order"))
        self.assertIn("cached_records", source)
        self.assertIn("pending_ids", source)
        self.assertIn("artifact_blob IS NOT NULL", source)
        self.assertIn("persist raw evidence", source)
        self.assertIn('"strictReplayCandidates": 0', source)
        self.assertNotIn("price_path.ensure", source)
        cli = inspect.getsource(discover.main)
        self.assertIn("resume_rough_report", cli)

    def test_private_research_cache_persists_record_and_replay_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "research.db")
            cache = profit_distribution._research_cache(path)
            profit_distribution._cache_rough_record(
                cache,
                "run",
                {"wallet": "wallet_test", "addr": "0xprivate"},
                {"wallet": "wallet_test", "status": "rough_complete", "reason": "ok"},
                {
                    "addr": "0xprivate",
                    "wallet": "wallet_test",
                    "fills": [{"coin": "BTC", "time": 1}],
                    "marks": {"BTC": 100.0},
                },
                {"portfolioPayload": [["perpWeek", {"vlm": 1}]]},
            )
            cache.commit()
            row = cache.execute(
                "SELECT addr,record_json,replay_blob,artifact_blob "
                "FROM profit_research_wallet_cache"
            ).fetchone()
            cache.close()
            self.assertEqual(row[0], "0xprivate")
            self.assertEqual(json.loads(row[1])["status"], "rough_complete")
            replay = json.loads(zlib.decompress(row[2]))
            self.assertEqual(replay["fills"][0]["coin"], "BTC")
            reopened = profit_distribution._research_cache(path)
            loaded = profit_distribution._load_cached_replay(
                reopened, "run", "wallet_test",
            )
            reopened.close()
            self.assertEqual(loaded["marks"]["BTC"], 100.0)
            artifact = json.loads(zlib.decompress(row[3]))
            self.assertEqual(artifact["portfolioPayload"][0][0], "perpWeek")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
