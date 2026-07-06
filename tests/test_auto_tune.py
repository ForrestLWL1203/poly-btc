import tempfile
import unittest
from pathlib import Path

from hl import auto_tune, storage


class AutoTuneTests(unittest.TestCase):
    def _db(self):
        td = tempfile.TemporaryDirectory()
        db = storage.connect(str(Path(td.name) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        self.addCleanup(td.cleanup)
        return db

    def test_choose_candidate_requires_recent_profit_and_preserved_fit(self):
        baseline = {
            "mult": 1.0,
            "windows": {
                30: {"copy_net_pnl": 1000, "closed_n": 10, "open_fill_rate": 0.90,
                     "liquidations": 0, "target_open_events": 10, "skip_reasons": {}},
                14: {"copy_net_pnl": 250, "closed_n": 5, "open_fill_rate": 0.90,
                     "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
                7: {"copy_net_pnl": 80, "closed_n": 3, "open_fill_rate": 1.0,
                    "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
            },
        }
        bad_fit = {
            "mult": 1.5,
            "windows": {
                30: {"copy_net_pnl": 4000, "closed_n": 10, "open_fill_rate": 0.55,
                     "liquidations": 0, "target_open_events": 10, "skip_reasons": {"skip_deploy_cap": 4}},
                14: {"copy_net_pnl": 500, "closed_n": 5, "open_fill_rate": 0.60,
                     "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
                7: {"copy_net_pnl": 100, "closed_n": 3, "open_fill_rate": 1.0,
                    "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
            },
        }
        bad_recent = {
            "mult": 1.25,
            "windows": {
                30: {"copy_net_pnl": 2000, "closed_n": 10, "open_fill_rate": 0.90,
                     "liquidations": 0, "target_open_events": 10, "skip_reasons": {}},
                14: {"copy_net_pnl": -1, "closed_n": 5, "open_fill_rate": 0.90,
                     "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
                7: {"copy_net_pnl": 100, "closed_n": 3, "open_fill_rate": 1.0,
                    "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
            },
        }
        good = {
            "mult": 1.25,
            "windows": {
                30: {"copy_net_pnl": 1800, "closed_n": 10, "open_fill_rate": 0.88,
                     "liquidations": 0, "target_open_events": 10, "skip_reasons": {}},
                14: {"copy_net_pnl": 350, "closed_n": 5, "open_fill_rate": 0.88,
                     "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
                7: {"copy_net_pnl": 90, "closed_n": 3, "open_fill_rate": 1.0,
                    "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
            },
        }

        selected = auto_tune.choose_margin_candidate([baseline, bad_fit, bad_recent, good], baseline)

        self.assertEqual(selected["mult"], 1.25)
        self.assertEqual(selected["windows"][30]["copy_net_pnl"], 1800)

    def test_margin_baseline_tracks_manual_values_not_last_auto_values(self):
        db = self._db()
        base = {"STABLE_MARGIN_PCT": 0.015, "MID_MARGIN_PCT": 0.015, "HIGH_MARGIN_PCT": 0.010}
        last_auto = {"STABLE_MARGIN_PCT": 0.0225, "MID_MARGIN_PCT": 0.0225, "HIGH_MARGIN_PCT": 0.015}
        auto_tune.store_margin_state(db, base, last_auto)

        resolved, reset = auto_tune.resolve_margin_baseline(db, dict(last_auto))

        self.assertFalse(reset)
        self.assertEqual(resolved, base)

        manual = {"STABLE_MARGIN_PCT": 0.020, "MID_MARGIN_PCT": 0.017, "HIGH_MARGIN_PCT": 0.012}
        resolved, reset = auto_tune.resolve_margin_baseline(db, manual)

        self.assertTrue(reset)
        self.assertEqual(resolved, manual)


if __name__ == "__main__":
    unittest.main()
