import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hyper import storage
from hyper.discovery import perp_prefilter, scanner


def _window(*, volume=1_000_000, pnl=100):
    return {
        "vlm": str(volume),
        "pnlHistory": [[0, "0"], [7 * 86_400_000, str(pnl)]],
        "accountValueHistory": [[0, "1"], [7 * 86_400_000, "1"]],
    }


def _portfolio(*, perp_week_volume=1_000_000, perp_month_pnl=100):
    return [
        ["week", _window()],
        ["month", _window()],
        ["allTime", _window()],
        ["perpWeek", _window(volume=perp_week_volume)],
        ["perpMonth", _window(pnl=perp_month_pnl)],
        ["perpAllTime", _window()],
    ]


class PerpPrefilterTests(unittest.TestCase):
    def test_only_perp_week_volume_is_a_business_gate(self):
        self.assertEqual(
            perp_prefilter.evaluate(
                _portfolio(perp_week_volume=249_999),
                min_week_perp_volume=250_000,
            ).reason,
            "perp_week_volume_below_floor",
        )
        passed = perp_prefilter.evaluate(
            _portfolio(perp_week_volume=250_000, perp_month_pnl=-999_999),
            min_week_perp_volume=250_000,
        )
        self.assertTrue(passed.passed)
        self.assertEqual(passed.reason, "perp_week_volume_confirmed")
        self.assertEqual(passed.windows["week"]["perpVlm"], 250_000)
        self.assertNotIn("officialPerp30d", passed.windows)

    def test_missing_or_invalid_perp_week_is_deferred(self):
        missing = [row for row in _portfolio() if row[0] != "perpWeek"]
        self.assertTrue(perp_prefilter.evaluate(
            missing, min_week_perp_volume=250_000,
        ).deferred)
        self.assertTrue(perp_prefilter.evaluate(
            {"bad": "shape"}, min_week_perp_volume=250_000,
        ).deferred)
        invalid = _portfolio()
        dict(invalid)["perpWeek"]["vlm"] = "bad"
        self.assertTrue(perp_prefilter.evaluate(
            invalid, min_week_perp_volume=250_000,
        ).deferred)

    def test_leaderboard_recall_uses_week_volume_and_nonnegative_7d_30d_pnl(self):
        def row(*, volume=250_000, week_pnl=0, month_pnl=0, roi=-99, account=1):
            return {
                "ethAddress": "0x1",
                "accountValue": account,
                "windowPerformances": [
                    ("week", {"pnl": week_pnl, "roi": roi, "vlm": volume}),
                    ("month", {"pnl": month_pnl, "roi": roi, "vlm": volume * 2}),
                    ("allTime", {"pnl": -9e9, "roi": roi, "vlm": volume * 3}),
                ],
            }

        policy = SimpleNamespace(
            min_acct=9e9,
            week_vlm_min=250_000,
            week_pnl_min=0,
            month_pnl_min=0,
            all_pnl_min=9e9,
            week_roi_min=99,
            month_roi_min=99,
            all_roi_min=99,
        )
        self.assertEqual(
            scanner._prepare_leaderboard_rows([row()], policy, "now")[0]["is_candidate"],
            1,
        )
        for candidate in (
            row(volume=249_999),
            row(week_pnl=-.01),
            row(month_pnl=-.01),
        ):
            self.assertEqual(
                scanner._prepare_leaderboard_rows([candidate], policy, "now")[0][
                    "is_candidate"
                ],
                0,
            )

    def test_prefilter_cache_is_versioned_only_by_week_volume_policy(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA)
            db.row_factory = sqlite3.Row
            policy = SimpleNamespace(
                week_vlm_min=250_000,
                week_pnl_min=0,
                month_pnl_min=0,
                all_pnl_min=0,
                perp_pnl_share_min=.8,
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

            changed_irrelevant = SimpleNamespace(**{
                **vars(policy), "perp_pnl_share_min": .99,
            })
            with mock.patch.object(scanner.rest, "portfolio", return_value=payload) as no_fetch:
                scanner._run_perp_prefilter(
                    db, ["0xabc"], changed_irrelevant, "scan-three",
                )
            no_fetch.assert_not_called()

            changed_volume = SimpleNamespace(**{
                **vars(policy), "week_vlm_min": 300_000,
            })
            with mock.patch.object(scanner.rest, "portfolio", return_value=payload) as refetch:
                scanner._run_perp_prefilter(db, ["0xabc"], changed_volume, "scan-four")
            refetch.assert_called_once_with("0xabc")

            with mock.patch.object(scanner.rest, "portfolio", return_value=payload) as full:
                scanner._run_perp_prefilter(
                    db, ["0xabc"], policy, "scan-full", allow_cache=False,
                )
            full.assert_called_once_with("0xabc")


if __name__ == "__main__":
    unittest.main()
