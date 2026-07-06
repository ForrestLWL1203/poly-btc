import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hl import auto_tune, params, storage


class AutoTuneTests(unittest.TestCase):
    def _db(self):
        td = tempfile.TemporaryDirectory()
        db = storage.connect(str(Path(td.name) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
        self.addCleanup(td.cleanup)
        return db

    def test_choose_candidate_requires_recent_profit_and_preserved_capacity_fit(self):
        baseline = {
            "mult": 1.0,
            "windows": {
                30: {"copy_net_pnl": 1000, "closed_n": 10, "open_fill_rate": 0.90,
                     "capacity_open_fit": 0.90, "liquidations": 0, "target_open_events": 10, "skip_reasons": {}},
                14: {"copy_net_pnl": 250, "closed_n": 5, "open_fill_rate": 0.90,
                     "capacity_open_fit": 0.90, "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
                7: {"copy_net_pnl": 80, "closed_n": 3, "open_fill_rate": 1.0,
                    "capacity_open_fit": 1.0, "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
            },
        }
        bad_fit = {
            "mult": 1.5,
            "windows": {
                30: {"copy_net_pnl": 4000, "closed_n": 10, "open_fill_rate": 0.55,
                     "capacity_open_fit": 0.55, "liquidations": 0, "target_open_events": 10,
                     "skip_reasons": {"skip_deploy_cap": 4}},
                14: {"copy_net_pnl": 500, "closed_n": 5, "open_fill_rate": 0.60,
                     "capacity_open_fit": 0.60, "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
                7: {"copy_net_pnl": 100, "closed_n": 3, "open_fill_rate": 1.0,
                    "capacity_open_fit": 1.0, "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
            },
        }
        bad_recent = {
            "mult": 1.25,
            "windows": {
                30: {"copy_net_pnl": 2000, "closed_n": 10, "open_fill_rate": 0.90,
                     "capacity_open_fit": 0.90, "liquidations": 0, "target_open_events": 10, "skip_reasons": {}},
                14: {"copy_net_pnl": -1, "closed_n": 5, "open_fill_rate": 0.90,
                     "capacity_open_fit": 0.90, "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
                7: {"copy_net_pnl": 100, "closed_n": 3, "open_fill_rate": 1.0,
                    "capacity_open_fit": 1.0, "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
            },
        }
        good = {
            "mult": 1.25,
            "windows": {
                30: {"copy_net_pnl": 1800, "closed_n": 10, "open_fill_rate": 0.65,
                     "capacity_open_fit": 0.88, "liquidations": 0, "target_open_events": 10, "skip_reasons": {}},
                14: {"copy_net_pnl": 350, "closed_n": 5, "open_fill_rate": 0.65,
                     "capacity_open_fit": 0.88, "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
                7: {"copy_net_pnl": 90, "closed_n": 3, "open_fill_rate": 1.0,
                    "capacity_open_fit": 1.0, "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
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

    def test_candidate_margin_tune_changes_max_bounds_only(self):
        follow = {
            "STABLE_MARGIN_MIN_PCT": 0.020,
            "MID_MARGIN_MIN_PCT": 0.020,
            "HIGH_MARGIN_MIN_PCT": 0.012,
            "STABLE_MARGIN_PCT": 0.030,
            "MID_MARGIN_PCT": 0.030,
            "HIGH_MARGIN_PCT": 0.020,
            "SMART_ADD": True,
        }
        margins = {
            "STABLE_MARGIN_PCT": 0.036,
            "MID_MARGIN_PCT": 0.036,
            "HIGH_MARGIN_PCT": 0.024,
        }

        overrides = auto_tune.follow_overrides_for_margin_candidate(follow, margins)

        self.assertEqual(overrides["STABLE_MARGIN_MIN_PCT"], 0.020)
        self.assertEqual(overrides["MID_MARGIN_MIN_PCT"], 0.020)
        self.assertEqual(overrides["HIGH_MARGIN_MIN_PCT"], 0.012)
        self.assertEqual(overrides["STABLE_MARGIN_PCT"], 0.036)
        self.assertEqual(overrides["MID_MARGIN_PCT"], 0.036)
        self.assertEqual(overrides["HIGH_MARGIN_PCT"], 0.024)
        self.assertEqual(overrides["ADD_STRATEGY"], "smart")

    def test_build_tune_candidate_changes_margin_lev_and_deploy_full(self):
        follow = {
            "STABLE_MARGIN_MIN_PCT": 0.020,
            "MID_MARGIN_MIN_PCT": 0.020,
            "HIGH_MARGIN_MIN_PCT": 0.020,
            "STABLE_MARGIN_PCT": 0.040,
            "MID_MARGIN_PCT": 0.030,
            "HIGH_MARGIN_PCT": 0.030,
            "STABLE_LEV_CAP": 25.0,
            "MID_LEV_CAP": 10.0,
            "HIGH_LEV_CAP": 4.0,
            "DEPLOY_FULL_PCT": 0.40,
            "SMART_ADD": True,
        }
        base = {k: follow[k] for k in auto_tune.TUNE_KEYS}
        candidate = auto_tune.build_tune_candidate(base, 1.4, (35, 12, 5), 0.50)

        overrides = auto_tune.follow_overrides_for_tune_candidate(follow, candidate)

        self.assertAlmostEqual(overrides["STABLE_MARGIN_PCT"], 0.056)
        self.assertAlmostEqual(overrides["MID_MARGIN_PCT"], 0.042)
        self.assertAlmostEqual(overrides["HIGH_MARGIN_PCT"], 0.042)
        self.assertEqual(overrides["STABLE_LEV_CAP"], 35)
        self.assertEqual(overrides["MID_LEV_CAP"], 12)
        self.assertEqual(overrides["HIGH_LEV_CAP"], 5)
        self.assertEqual(overrides["DEPLOY_FULL_PCT"], 0.50)
        self.assertEqual(overrides["ADD_STRATEGY"], "smart")

    def test_choose_candidate_uses_recent_window_for_capacity_and_liquidation_guard(self):
        baseline = {
            "mult": 1.0,
            "windows": {
                30: {"copy_net_pnl": 1000, "closed_n": 10, "capacity_open_fit": 0.90,
                     "liquidations": 1, "target_open_events": 10, "skip_reasons": {}},
                14: {"copy_net_pnl": 300, "closed_n": 5, "capacity_open_fit": 0.82,
                     "liquidations": 1, "target_open_events": 5, "skip_reasons": {}},
                7: {"copy_net_pnl": 80, "closed_n": 3, "capacity_open_fit": 0.90,
                    "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
            },
        }
        recent_winner_with_older_liqs = {
            "mult": 1.4,
            "windows": {
                30: {"copy_net_pnl": 1500, "closed_n": 10, "capacity_open_fit": 0.75,
                     "liquidations": 5, "target_open_events": 10, "skip_reasons": {"skip_deploy_cap": 3}},
                14: {"copy_net_pnl": 700, "closed_n": 5, "capacity_open_fit": 0.75,
                     "liquidations": 1, "target_open_events": 5, "skip_reasons": {"skip_deploy_cap": 1}},
                7: {"copy_net_pnl": 120, "closed_n": 3, "capacity_open_fit": 0.80,
                    "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
            },
        }

        selected = auto_tune.choose_margin_candidate([baseline, recent_winner_with_older_liqs], baseline)

        self.assertEqual(selected["mult"], 1.4)

    def test_build_add_candidate_changes_smart_add_core_params(self):
        follow = {
            "ADD_GAP_K": 0.12,
            "ADD_GAP_SHRINK_G": 1.2,
            "ADD_MAX_HARD": 8,
            "SMART_ADD": True,
        }
        base = {k: follow[k] for k in auto_tune.ADD_TUNE_KEYS}
        candidate = auto_tune.build_add_candidate(base, 0.06, 1.3, 6)

        overrides = auto_tune.follow_overrides_for_add_candidate(follow, candidate)

        self.assertEqual(overrides["ADD_STRATEGY"], "smart")
        self.assertAlmostEqual(overrides["ADD_GAP_K"], 0.06)
        self.assertAlmostEqual(overrides["ADD_GAP_SHRINK_G"], 1.3)
        self.assertEqual(overrides["ADD_MAX_HARD"], 6)

    def test_maybe_tune_margins_writes_add_params_after_sizing_grid(self):
        db = self._db()
        params.seed_params(db)
        base_windows = {
            30: {"copy_net_pnl": 1000, "closed_n": 10, "capacity_open_fit": 0.9,
                 "liquidations": 0, "target_open_events": 10, "skip_reasons": {}},
            14: {"copy_net_pnl": 300, "closed_n": 5, "capacity_open_fit": 0.9,
                 "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
            7: {"copy_net_pnl": 100, "closed_n": 3, "capacity_open_fit": 1.0,
                "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
        }
        better_windows = {
            30: {"copy_net_pnl": 1100, "closed_n": 10, "capacity_open_fit": 0.9,
                 "liquidations": 0, "target_open_events": 10, "skip_reasons": {}},
            14: {"copy_net_pnl": 500, "closed_n": 5, "capacity_open_fit": 0.9,
                 "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
            7: {"copy_net_pnl": 150, "closed_n": 3, "capacity_open_fit": 1.0,
                "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
        }

        def tune_axes(base):
            return [auto_tune.build_tune_candidate(
                base, 1.0, tuple(base[k] for k in auto_tune.LEV_KEYS), base["DEPLOY_FULL_PCT"]
            )]

        def eval_tune(_db, _addrs, follow, candidate, sigmas=None, now_ms=None):
            out = dict(candidate)
            out["params"] = {k: follow[k] for k in auto_tune.TUNE_KEYS}
            out["margins"] = {k: follow[k] for k in auto_tune.MARGIN_KEYS}
            out["lev_caps"] = {k: follow[k] for k in auto_tune.LEV_KEYS}
            out["deploy_full_pct"] = follow["DEPLOY_FULL_PCT"]
            out["windows"] = dict(base_windows)
            return out

        def add_axes(base):
            return [
                auto_tune.build_add_candidate(base, 0.12, 1.2, 8),
                auto_tune.build_add_candidate(base, 0.06, 1.3, 6),
            ]

        def eval_add(_db, _addrs, _follow, candidate, sigmas=None, now_ms=None):
            out = dict(candidate)
            out["params"] = dict(candidate["params"])
            out["windows"] = better_windows if candidate["params"]["ADD_GAP_K"] == 0.06 else base_windows
            return out

        with patch.object(auto_tune, "_load_followed_wallets", return_value=["0xaaa"]), \
                patch.object(auto_tune, "_load_sigmas", return_value={}), \
                patch.object(auto_tune, "tune_candidates_from_axes", side_effect=tune_axes), \
                patch.object(auto_tune, "evaluate_tune_candidate", side_effect=eval_tune), \
                patch.object(auto_tune, "add_candidates_from_axes", side_effect=add_axes), \
                patch.object(auto_tune, "evaluate_add_candidate", side_effect=eval_add):
            res = auto_tune.maybe_tune_margins(db, source="test")

        self.assertTrue(res["applied"])
        self.assertAlmostEqual(res["add_params"]["ADD_GAP_K"], 0.06)
        rows = dict(db.execute(
            "SELECT key,value FROM params WHERE key IN ('ADD_GAP_K','ADD_GAP_SHRINK_G','ADD_MAX_HARD')"
        ).fetchall())
        self.assertEqual(rows["ADD_GAP_K"], "0.06")
        self.assertEqual(rows["ADD_GAP_SHRINK_G"], "1.3")
        self.assertEqual(rows["ADD_MAX_HARD"], "6")


if __name__ == "__main__":
    unittest.main()
