import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from hyper import params, storage
from hyper.selection import auto_tune


class AutoTuneTests(unittest.TestCase):
    @staticmethod
    def _formation_validation(*, baseline_capacity, challenger_capacity,
                              baseline_open=.80, challenger_open=.90,
                              baseline_net=120, challenger_net=100):
        folds = [{
            "baselineNet": baseline_net,
            "challengerNet": challenger_net,
            "baselineMaxDD": .10,
            "challengerMaxDD": .10,
            "baselineOpenRate": baseline_open,
            "challengerOpenRate": challenger_open,
            "baselineCapacityFit": baseline_capacity,
            "challengerCapacityFit": challenger_capacity,
        } for _ in range(4)]
        return {
            "folds": folds,
            "foldWins": int(challenger_net > baseline_net) * 4,
            "holdout": folds[-1],
            "baselineStressNet": baseline_net,
            "stressNet": challenger_net,
            "pricePathCoverage": 1.0,
            "maintenanceMarginCoverage": 1.0,
        }

    def test_formation_does_not_treat_capacity_as_a_second_profit_veto(self):
        validation = self._formation_validation(
            baseline_capacity=.70, challenger_capacity=.80,
        )

        model = auto_tune._formation_model_validation(validation, auto_tune.load_copy_policy())

        self.assertFalse(model["eligible"])
        self.assertTrue(model["baselineFeasible"])
        self.assertNotIn("formation_admission_still_infeasible", model["reasons"])

    def test_formation_keeps_normal_profit_validation_when_baseline_is_fundable(self):
        validation = self._formation_validation(
            baseline_capacity=.90, challenger_capacity=.92,
        )

        model = auto_tune._formation_model_validation(validation, auto_tune.load_copy_policy())

        self.assertFalse(model["eligible"])
        self.assertTrue(model["baselineFeasible"])
        self.assertIn("relative_gain_below_floor", model["reasons"])

    def test_formation_rejects_surface_that_breaks_four_week_stability(self):
        validation = self._formation_validation(
            baseline_capacity=.90, challenger_capacity=.92,
            baseline_net=100, challenger_net=130,
        )
        validation["folds"][-1]["challengerNet"] = -200
        validation["holdout"] = validation["folds"][-1]
        validation["stressNet"] = -210

        model = auto_tune._formation_model_validation(
            validation, auto_tune.load_copy_policy(),
        )

        self.assertFalse(model["eligible"])
        self.assertFalse(model["challengerFeasible"])
        self.assertIn("holdout_not_profitable", model["reasons"])

    def test_candidate_can_improve_a_baseline_already_below_absolute_open_floor(self):
        baseline = {
            "params": {key: 1.0 for key in auto_tune.TUNE_KEYS},
            "windows": {
                30: {
                    "copy_net_pnl": 1000, "closed_n": 10,
                    "open_fill_rate": 0.60, "capacity_open_fit": 0.80,
                    "target_open_events": 10, "skip_reasons": {},
                },
            },
        }
        preserved = {
            **baseline,
            "windows": {30: {**baseline["windows"][30], "copy_net_pnl": 1100}},
        }
        degraded = {
            **baseline,
            "windows": {30: {
                **baseline["windows"][30], "copy_net_pnl": 1200,
                "open_fill_rate": 0.40, "capacity_open_fit": 0.60,
            }},
        }

        self.assertTrue(auto_tune._candidate_valid(preserved, baseline))
        self.assertFalse(auto_tune._candidate_valid(degraded, baseline))

    def test_walk_forward_validation_uses_relative_execution_floor_below_absolute_floor(self):
        policy = auto_tune.load_copy_policy()
        folds = [{
            "baselineNet": 100.0, "challengerNet": 120.0,
            "baselineOpenRate": 0.60, "challengerOpenRate": 0.60,
            "baselineCapacityFit": 0.80, "challengerCapacityFit": 0.80,
        } for _ in range(3)]
        validation = {
            "folds": folds, "foldWins": 3, "holdout": folds[-1],
            "baselineStressNet": 100.0, "stressNet": 120.0,
        }

        model = auto_tune._model_validation(validation, policy)

        self.assertTrue(model["eligible"])
        self.assertNotIn("open_rate_below_floor", model["reasons"])
        self.assertNotIn("capacity_fit_below_floor", model["reasons"])

    def test_positive_holdout_is_not_double_counted_after_two_fold_wins(self):
        policy = auto_tune.load_copy_policy()
        folds = [
            {
                "baselineNet": 100.0, "challengerNet": 120.0,
                "baselineOpenRate": .90, "challengerOpenRate": .90,
                "baselineCapacityFit": .95, "challengerCapacityFit": .95,
            },
            {
                "baselineNet": 100.0, "challengerNet": 120.0,
                "baselineOpenRate": .90, "challengerOpenRate": .90,
                "baselineCapacityFit": .95, "challengerCapacityFit": .95,
            },
            {
                "baselineNet": 100.0, "challengerNet": 95.0,
                "baselineOpenRate": .90, "challengerOpenRate": .90,
                "baselineCapacityFit": .95, "challengerCapacityFit": .95,
            },
        ]
        model = auto_tune._model_validation({
            "folds": folds,
            "foldWins": 2,
            "holdout": folds[-1],
            "stressNet": 90.0,
        }, policy)

        self.assertTrue(model["eligible"])
        self.assertNotIn("holdout_not_profitable", model["reasons"])

    def test_margin_candidates_obey_four_add_ceiling_in_all_tiers(self):
        follow = {
            "MARGIN_EQUITY_PCT": 1.0,
            "MIN_OPEN_MARGIN_PCT": 0.005,
            "STABLE_COIN_CAP_PCT": 0.40,
            "MID_COIN_CAP_PCT": 0.22,
            "HIGH_COIN_CAP_PCT": 0.15,
            "STABLE_MARGIN_MIN_PCT": 0.02,
            "MID_MARGIN_MIN_PCT": 0.02,
            "HIGH_MARGIN_MIN_PCT": 0.012,
        }
        base = {
            "STABLE_MARGIN_PCT": 0.13, "MID_MARGIN_PCT": 0.06, "HIGH_MARGIN_PCT": 0.04,
            "STABLE_LEV_CAP": 28, "MID_LEV_CAP": 9, "HIGH_LEV_CAP": 6,
        }

        ceilings = auto_tune.margin_add_capacity_ceilings(follow)
        self.assertEqual(ceilings, {
            "STABLE_MARGIN_PCT": 0.09875,
            "MID_MARGIN_PCT": 0.05375,
            "HIGH_MARGIN_PCT": 0.03625,
        })
        candidates = auto_tune.independent_margin_candidates(base, follow)
        for candidate in candidates:
            for key, ceiling in ceilings.items():
                self.assertLessEqual(candidate["params"][key], ceiling)

        cold_start = auto_tune.capacity_margin_candidates(base, follow)
        stable_values = {round(row["params"]["STABLE_MARGIN_PCT"], 8) for row in cold_start}
        self.assertIn(round(ceilings["STABLE_MARGIN_PCT"] * 0.75, 8), stable_values)
        self.assertIn(round(ceilings["STABLE_MARGIN_PCT"], 8), stable_values)

    def test_manual_margin_equity_pct_is_not_an_auto_tune_axis(self):
        self.assertNotIn("MARGIN_EQUITY_PCT", auto_tune.TUNE_KEYS)
        self.assertNotIn("MARGIN_EQUITY_PCT", auto_tune.ADD_TUNE_KEYS)

    def test_joint_tuner_covers_every_tier_open_and_smart_add_axis(self):
        self.assertEqual(auto_tune.MARGIN_KEYS, (
            "STABLE_MARGIN_PCT", "MID_MARGIN_PCT", "HIGH_MARGIN_PCT",
        ))
        self.assertEqual(auto_tune.LEV_KEYS, (
            "STABLE_LEV_CAP", "MID_LEV_CAP", "HIGH_LEV_CAP",
        ))
        self.assertNotIn("DEPLOY_FULL_PCT", auto_tune.TUNE_KEYS)
        self.assertNotIn("MAX_DEPLOY_PCT", auto_tune.TUNE_KEYS)
        self.assertEqual(auto_tune.ADD_TUNE_KEYS, (
            "ADD_GAP_K", "POS_ADD_GAP_K", "ADD_GAP_SHRINK_G", "ADD_MAX_HARD",
        ))

    def test_margin_polish_combines_two_profitable_tier_moves(self):
        db = self._db()
        params.seed_params(db)
        follow = params.load_follow(db)
        follow["STABLE_MARGIN_PCT"] = 0.04
        follow["HIGH_MARGIN_PCT"] = 0.02
        base_stable = float(follow["STABLE_MARGIN_PCT"])
        base_high = float(follow["HIGH_MARGIN_PCT"])

        def evaluate(_db, _addrs, _follow, candidate, **_kwargs):
            out = dict(candidate)
            values = dict(candidate.get("params") or {})
            reward = 0.0
            if float(values["HIGH_MARGIN_PCT"]) > base_high + 1e-9:
                reward += 1000.0
            if float(values["STABLE_MARGIN_PCT"]) > base_stable + 1e-9:
                reward += 500.0
            out["params"] = values
            out["margins"] = {key: values[key] for key in auto_tune.MARGIN_KEYS}
            out["lev_caps"] = {key: values[key] for key in auto_tune.LEV_KEYS}
            out["windows"] = {
                days: {
                    "copy_net_pnl": 1000.0 + reward,
                    "closed_n": 20,
                    "open_fill_rate": 0.90,
                    "capacity_open_fit": 0.95,
                    "target_open_events": 20,
                    "liquidations": 0,
                    "skip_reasons": {},
                }
                for days in (30, 14, 7)
            }
            return out

        fold = {
            "baselineNet": 100.0, "challengerNet": 130.0,
            "baselineOpenRate": 0.90, "challengerOpenRate": 0.90,
            "baselineCapacityFit": 0.95, "challengerCapacityFit": 0.95,
        }
        validation = {
            "folds": [dict(fold) for _ in range(3)], "foldWins": 3,
            "holdout": dict(fold), "baselineStressNet": 100.0, "stressNet": 130.0,
        }
        with patch.object(auto_tune, "_portfolio_window_fills", return_value={30: [{}], 14: [{}], 7: [{}]}), \
                patch.object(auto_tune, "prepare_refined_price_path", return_value=([], {})), \
                patch.object(auto_tune, "evaluate_tune_candidate", side_effect=evaluate), \
                patch.object(auto_tune, "add_candidates_from_axes", return_value=[]), \
                patch.object(auto_tune, "_walk_forward_validation", return_value=validation):
            result = auto_tune.maybe_tune_margins(
                db, source="test", dry_run=True, mode="apply",
                follow_values=follow, addrs_override=["0xaaa"], record_run=False,
            )

        self.assertTrue(result["eligible_to_apply"])
        self.assertGreater(result["proposal"]["HIGH_MARGIN_PCT"], base_high)
        self.assertGreater(result["proposal"]["STABLE_MARGIN_PCT"], base_stable)
        # The absolute capacity grid may combine both profitable tier moves before coordinate polish; the
        # bounded local rounds then only need to confirm that surface instead of rediscovering it one axis
        # at a time.
        self.assertGreaterEqual(len(result["margin_rounds"]), 1)

    def test_generation_bound_tune_skips_if_generation_changes_before_apply(self):
        db = self._db()
        params.seed_params(db)
        db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,publishable,is_current,started_at,published_at) "
            "VALUES ('g1','published',1,1,1,'2026-01-01','2026-01-01')"
        )
        db.commit()
        auto_tune.generation_market.Resolver(db, "g1", 1, set(), {})
        auto_tune.generation_market.seal(db, "g1")
        before = dict(db.execute("SELECT key,value FROM params").fetchall())

        def candidate_axis(base, *_args):
            changed = dict(base)
            changed["MID_MARGIN_PCT"] = float(base["MID_MARGIN_PCT"]) * 1.15
            return [auto_tune._candidate_from_params(changed, axis="test")]

        def evaluate(_db, _addrs, _follow, candidate, **_kwargs):
            out = dict(candidate)
            out["windows"] = {
                30: {"copy_net_pnl": 1000, "closed_n": 10, "open_fill_rate": .95,
                     "capacity_open_fit": .95, "liquidations": 0},
            }
            return out

        def generation_changes(*_args, **_kwargs):
            db.execute("UPDATE scan_generation SET is_current=0 WHERE generation='g1'")
            db.execute(
                "INSERT INTO scan_generation "
                "(generation,status,complete,publishable,is_current,started_at,published_at) "
                "VALUES ('g2','published',1,1,1,'2026-01-02','2026-01-02')"
            )
            db.commit()
            return {"eligible": True, "reasons": [], "relativeGain": .2}

        with patch.object(auto_tune, "_load_followed_wallets", return_value=["0xaaa"]), \
                patch.object(auto_tune, "_portfolio_window_fills", return_value={30: [{}]}), \
                patch.object(auto_tune, "independent_leverage_candidates", side_effect=candidate_axis), \
                patch.object(auto_tune, "independent_margin_candidates", side_effect=candidate_axis), \
                patch.object(auto_tune, "evaluate_tune_candidate", side_effect=evaluate), \
                patch.object(auto_tune, "choose_margin_candidate", side_effect=lambda rows, _base: rows[0]), \
                patch.object(auto_tune, "add_candidates_from_axes", return_value=[]), \
                patch.object(auto_tune, "_walk_forward_validation", return_value={}), \
                patch.object(auto_tune, "_model_validation", return_value={
                    "eligible": True, "reasons": [], "relativeGain": .2,
                }), \
                patch.object(auto_tune, "_proposal_apply_eligibility", side_effect=generation_changes):
            result = auto_tune.maybe_tune_margins(
                db, source="test", mode="apply", expected_generation="g1",
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "generation_changed_before_apply")
        self.assertEqual(result["expectedGeneration"], "g1")
        self.assertEqual(result["currentGeneration"], "g2")
        self.assertFalse(result["applied"])
        self.assertEqual(dict(db.execute("SELECT key,value FROM params").fetchall()), before)
        self.assertEqual(db.execute(
            "SELECT COUNT(*) FROM commands WHERE type='reload_params'"
        ).fetchone()[0], 0)
        self.assertEqual(db.execute(
            "SELECT generation FROM auto_tune_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0], "g1")

    def test_add_candidate_applies_positive_add_gap_independently(self):
        follow = {
            "ADD_GAP_K": 0.12, "POS_ADD_GAP_K": 0.08,
            "ADD_GAP_SHRINK_G": 1.2, "ADD_MAX_HARD": 8,
        }
        candidate = auto_tune.build_add_candidate(follow, 0.06, 1.3, 6, pos_gap_k=0.11)

        overrides = auto_tune.follow_overrides_for_add_candidate(follow, candidate)

        self.assertEqual(overrides["ADD_GAP_K"], 0.06)
        self.assertEqual(overrides["POS_ADD_GAP_K"], 0.11)

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
                30: {"copy_net_pnl": 1800, "closed_n": 10, "open_fill_rate": 0.88,
                     "capacity_open_fit": 0.88, "liquidations": 0, "target_open_events": 10, "skip_reasons": {}},
                14: {"copy_net_pnl": 350, "closed_n": 5, "open_fill_rate": 0.88,
                     "capacity_open_fit": 0.88, "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
                7: {"copy_net_pnl": 90, "closed_n": 3, "open_fill_rate": 1.0,
                    "capacity_open_fit": 1.0, "liquidations": 0, "target_open_events": 3, "skip_reasons": {}},
            },
        }

        selected = auto_tune.choose_margin_candidate([baseline, bad_fit, bad_recent, good], baseline)

        self.assertEqual(selected["mult"], 1.25)
        self.assertEqual(selected["windows"][30]["copy_net_pnl"], 1800)

    def test_diverse_sizing_candidates_reserves_slots_per_leverage_tuple(self):
        def candidate(levs, pnl):
            return {
                "params": dict(zip(auto_tune.LEV_KEYS, levs)),
                "windows": {30: {
                    "copy_net_pnl": pnl, "closed_n": 10, "open_fill_rate": 0.90,
                    "capacity_open_fit": 0.90, "liquidations": 0,
                    "target_open_events": 10, "skip_reasons": {},
                }},
            }

        baseline = candidate((35, 12, 4), 100)
        crowded = [candidate((12, 5, 3), 1000 - i) for i in range(8)]
        alternatives = [candidate((18, 7, 4), 700), candidate((20, 8, 4), 600)]

        selected = auto_tune._diverse_sizing_candidates(crowded + alternatives, baseline, 3)

        self.assertEqual(
            {tuple(row["params"][key] for key in auto_tune.LEV_KEYS) for row in selected},
            {(12, 5, 3), (18, 7, 4), (20, 8, 4)},
        )

    def test_leverage_polish_changes_one_tier_at_a_time(self):
        base = {
            "STABLE_MARGIN_PCT": 0.0644, "MID_MARGIN_PCT": 0.0552,
            "HIGH_MARGIN_PCT": 0.0368, "STABLE_LEV_CAP": 32.0,
            "MID_LEV_CAP": 12.0, "HIGH_LEV_CAP": 4.0,
        }

        candidates = auto_tune.independent_leverage_candidates(base)
        values = [candidate["params"] for candidate in candidates]

        self.assertTrue(any(row["STABLE_LEV_CAP"] == 32 and row["MID_LEV_CAP"] == 10 for row in values))
        self.assertTrue(any(row["STABLE_LEV_CAP"] == 32 and row["MID_LEV_CAP"] == 9 for row in values))
        self.assertTrue(any(row["HIGH_LEV_CAP"] == 6 for row in values))
        self.assertFalse(any(
            row["STABLE_LEV_CAP"] != 32 and row["MID_LEV_CAP"] != 12 for row in values
        ))

    def test_coarse_leverage_grid_keeps_only_baseline_and_tier_endpoints(self):
        base = {
            "STABLE_MARGIN_PCT": 0.0644, "MID_MARGIN_PCT": 0.0552,
            "HIGH_MARGIN_PCT": 0.0368, "STABLE_LEV_CAP": 32.0,
            "MID_LEV_CAP": 12.0, "HIGH_LEV_CAP": 4.0,
        }

        full = auto_tune.independent_leverage_candidates(base)
        coarse = auto_tune.coarse_leverage_candidates(base)

        self.assertLess(len(coarse), len(full))
        self.assertLessEqual(len(coarse), 7)
        self.assertEqual(
            {key: coarse[0]["params"][key] for key in base},
            base,
        )
        for candidate in coarse[1:]:
            changed = sum(
                candidate["params"][key] != base[key] for key in auto_tune.LEV_KEYS
            )
            self.assertEqual(changed, 1)

    def test_leverage_candidates_raise_margin_to_preserve_notional(self):
        db = self._db()
        params.seed_params(db)
        follow = params.load_follow(db)
        base = {
            "STABLE_MARGIN_PCT": 0.035, "MID_MARGIN_PCT": 0.03,
            "HIGH_MARGIN_PCT": 0.02, "STABLE_LEV_CAP": 35.0,
            "MID_LEV_CAP": 10.0, "HIGH_LEV_CAP": 4.0,
        }

        candidates = auto_tune.independent_leverage_candidates(base, follow)
        stable_25 = next(
            row["params"] for row in candidates
            if row["params"]["STABLE_LEV_CAP"] == 25.0
            and row["params"]["MID_LEV_CAP"] == 10.0
        )

        self.assertAlmostEqual(stable_25["STABLE_MARGIN_PCT"], 0.049)
        self.assertAlmostEqual(
            stable_25["STABLE_MARGIN_PCT"] * stable_25["STABLE_LEV_CAP"],
            base["STABLE_MARGIN_PCT"] * base["STABLE_LEV_CAP"],
        )

    def test_deploy_cap_and_retired_total_cap_are_not_tuned(self):
        self.assertNotIn("MAX_DEPLOY_PCT", auto_tune.TUNE_KEYS)
        self.assertFalse(hasattr(auto_tune.config, "MAX_TOTAL_MARGIN_PCT"))

    def test_near_best_profit_prefers_fewer_liquidations_then_capacity(self):
        def candidate(pnl, liqs, capacity, marker):
            return {
                "mult": 1.0,
                "marker": marker,
                "windows": {
                    30: {
                        "copy_net_pnl": pnl, "closed_n": 20,
                        "open_fill_rate": capacity, "capacity_open_fit": capacity,
                        "liquidations": liqs, "target_open_events": 20, "skip_reasons": {},
                    },
                    14: {
                        "copy_net_pnl": pnl * .30, "closed_n": 10,
                        "open_fill_rate": capacity, "capacity_open_fit": capacity,
                        "liquidations": liqs, "target_open_events": 10, "skip_reasons": {},
                    },
                    7: {
                        "copy_net_pnl": pnl * .10, "closed_n": 5,
                        "open_fill_rate": capacity, "capacity_open_fit": capacity,
                        "liquidations": 0, "target_open_events": 5, "skip_reasons": {},
                    },
                },
            }

        baseline = candidate(1000, 2, .90, "baseline")
        absolute_profit = candidate(1060, 5, .90, "profit")
        safer_crowded = candidate(1020, 1, .82, "safe-crowded")
        safer_fundable = candidate(1010, 1, .94, "safe-fundable")

        selected = auto_tune.choose_margin_candidate(
            [baseline, absolute_profit, safer_crowded, safer_fundable], baseline,
        )

        self.assertEqual(selected["marker"], "safe-fundable")

    def test_model_validation_allows_profit_retaining_liquidation_repair(self):
        folds = [{
            "baselineNet": 100.0, "challengerNet": 95.0,
            "baselineOpenRate": .90, "challengerOpenRate": .90,
            "baselineCapacityFit": .95, "challengerCapacityFit": .95,
            "baselineLiquidations": 2, "challengerLiquidations": 0,
        } for _ in range(3)]
        validation = {
            "folds": folds,
            "foldWins": 0,
            "holdout": folds[-1],
            "baselineStressNet": 90.0,
            "stressNet": 85.0,
            "baselineStressLiquidations": 1,
            "stressLiquidations": 0,
        }

        model = auto_tune._model_validation(
            validation, auto_tune.load_copy_policy({"AUTO_TUNE_MIN_RELATIVE_GAIN": .05}),
        )

        self.assertTrue(model["eligible"])
        self.assertTrue(model["safetyRepair"])
        self.assertAlmostEqual(model["profitRetention"], .95)
        self.assertLess(model["challengerLiquidations"], model["baselineLiquidations"])

    def test_liquidation_repair_cannot_buy_safety_with_large_profit_loss(self):
        folds = [{
            "baselineNet": 100.0, "challengerNet": 70.0,
            "baselineOpenRate": .90, "challengerOpenRate": .90,
            "baselineCapacityFit": .95, "challengerCapacityFit": .95,
            "baselineLiquidations": 2, "challengerLiquidations": 0,
        } for _ in range(3)]
        validation = {
            "folds": folds, "foldWins": 0, "holdout": folds[-1],
            "baselineStressNet": 90.0, "stressNet": 65.0,
            "baselineStressLiquidations": 1, "stressLiquidations": 0,
        }

        model = auto_tune._model_validation(
            validation, auto_tune.load_copy_policy({"AUTO_TUNE_MIN_RELATIVE_GAIN": .05}),
        )

        self.assertFalse(model["eligible"])
        self.assertFalse(model["safetyRepair"])
        self.assertIn("relative_gain_below_floor", model["reasons"])


    def test_load_portfolio_fills_filters_disallowed_wallet_sectors(self):
        db = self._db()
        db.execute(
            "INSERT INTO watchlist (rank,addr,score,sector_policy_json,updated_at) VALUES "
            "(1,'0xaaa',0.9,?,'now')",
            (json.dumps({"crypto": {"allow": True}, "stock": {"allow": False}, "allowed": ["crypto"]}),),
        )
        rows = [
            ("0xaaa", 1, 1_000, {"time": 1_000, "tid": 1, "coin": "BTC", "side": "B", "sz": "1", "px": "100", "startPosition": "0"}),
            ("0xaaa", 2, 2_000, {"time": 2_000, "tid": 2, "coin": "xyz:MU", "side": "B", "sz": "1", "px": "900", "startPosition": "0"}),
        ]
        db.executemany(
            "INSERT INTO candidate_fills (addr,tid,time,fill_json) VALUES (?,?,?,?)",
            [(addr, tid, ts, json.dumps(fill)) for addr, tid, ts, fill in rows],
        )
        db.commit()

        fills = auto_tune._load_portfolio_fills(db, ["0xaaa"], 0)

        self.assertEqual([x["coin"] for x in fills], ["BTC"])

    def test_formation_can_replay_watch_sector_without_granting_live_permission(self):
        db = self._db()
        policy = {
            "crypto": {"allow": False, "watch": True},
            "stock": {"allow": False, "watch": False},
            "allowed": [],
            "watch": ["crypto"],
        }
        db.execute(
            "INSERT INTO profile(addr,status,sector_policy_json) VALUES('0xaaa','active',?)",
            (json.dumps(policy),),
        )
        fill = {
            "time": 1_000, "tid": 1, "coin": "BTC", "side": "B",
            "sz": "1", "px": "100", "startPosition": "0",
        }
        db.execute(
            "INSERT INTO candidate_fills(addr,tid,time,fill_json) VALUES('0xaaa',1,1000,?)",
            (json.dumps(fill),),
        )
        db.commit()

        live = auto_tune._load_portfolio_fills(db, ["0xaaa"], 0)
        formation = auto_tune._load_portfolio_fills(
            db, ["0xaaa"], 0, include_watch=True,
        )

        self.assertEqual(live, [])
        self.assertEqual([row["coin"] for row in formation], ["BTC"])


    def test_choose_candidate_prices_liquidation_loss_through_net_pnl(self):
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
            "POS_ADD_GAP_K": 0.08,
            "ADD_GAP_SHRINK_G": 1.2,
            "ADD_MAX_HARD": 8,
            "SMART_ADD": True,
        }
        base = {k: follow[k] for k in auto_tune.ADD_TUNE_KEYS}
        candidate = auto_tune.build_add_candidate(base, 0.06, 1.3, 6)

        overrides = auto_tune.follow_overrides_for_add_candidate(follow, candidate)

        self.assertEqual(overrides["ADD_STRATEGY"], "smart")
        self.assertAlmostEqual(overrides["ADD_GAP_K"], 0.06)
        self.assertAlmostEqual(overrides["POS_ADD_GAP_K"], 0.08)
        self.assertAlmostEqual(overrides["ADD_GAP_SHRINK_G"], 1.3)
        self.assertEqual(overrides["ADD_MAX_HARD"], 6)


    def test_maybe_tune_margins_skips_when_fill_cache_guard_exceeded(self):
        db = self._db()
        params.seed_params(db)
        load_calls = []
        fake_fills = [
            {"user": "0xaaa", "time": 1, "tid": 1, "coin": "BTC", "side": "B",
             "sz": "1", "startPosition": "0", "px": "100", "oid": 1, "crossed": True},
            {"user": "0xaaa", "time": 2, "tid": 2, "coin": "BTC", "side": "A",
             "sz": "1", "startPosition": "1", "px": "101", "oid": 2, "crossed": True},
        ]

        def load_window(_db, addrs, start_ms):
            load_calls.append((tuple(addrs), start_ms))
            return list(fake_fills)

        with patch.object(auto_tune, "_load_followed_wallets", return_value=["0xaaa"]), \
                patch.object(auto_tune, "_portfolio_fill_json_bytes",
                             return_value=auto_tune.config.AUTO_TUNE_FILL_CACHE_MAX_BYTES + 1), \
                patch.object(auto_tune, "_load_portfolio_fills", side_effect=load_window), \
                patch.object(auto_tune.time, "time", return_value=10.0):
            res = auto_tune.maybe_tune_margins(db, source="test")

        self.assertEqual(res["status"], "skipped")
        self.assertEqual(res["reason"], "fill_cache_guard")
        self.assertFalse(res["applied"])
        self.assertEqual(len(load_calls), 0)


    def test_paper_apply_policy_still_requires_price_path_for_leverage_change(self):
        db = self._db()
        params.seed_params(db)
        current = {key: 1.0 for key in auto_tune.TUNE_KEYS + auto_tune.ADD_TUNE_KEYS}
        proposal = dict(current)
        proposal["STABLE_LEV_CAP"] = 2.0
        folds = [
            {
                "baselineNet": 100.0, "challengerNet": 120.0,
                "baselineMaxDD": 0.10, "challengerMaxDD": 0.10,
                "challengerOpenRate": 0.90, "challengerCapacityFit": 0.90,
            }
            for _ in range(3)
        ]
        follow = params.load_follow(db)
        follow.update({
            "AUTO_TUNE_APPLY_MIN_SHADOW_DAYS": 0,
            "AUTO_TUNE_APPLY_MIN_FORWARD_CLOSED": 0,
            "AUTO_TUNE_MIN_DIRECTION_STREAK": 1,
            "AUTO_TUNE_MASTER_LEVERAGE_MIN_COVERAGE": 0.0,
            "AUTO_TUNE_PRICE_PATH_MIN_COVERAGE": 0.0,
        })

        result = auto_tune._proposal_apply_eligibility(
            db, [], follow, current, proposal,
            {
                "folds": folds, "foldWins": 3, "holdout": folds[-1],
                "stressNet": 50.0, "stressLiquidations": 0,
                "masterLeverageCoverage": 0.0, "pricePathCoverage": 0.0,
            },
            "2026-07-11T00:00:00Z",
        )

        self.assertFalse(result["eligible"])
        self.assertIn("price_path_coverage_low", result["reasons"])

    def test_model_validation_allows_fewer_stress_liquidations_than_baseline(self):
        folds = [
            {
                "baselineNet": 100.0, "challengerNet": 120.0,
                "challengerOpenRate": 0.90, "challengerCapacityFit": 0.90,
            }
            for _ in range(3)
        ]
        validation = {
            "folds": folds,
            "foldWins": 3,
            "holdout": folds[-1],
            "baselineStressLiquidations": 8,
            "stressNet": 100.0,
            "stressLiquidations": 1,
        }
        policy = auto_tune.load_copy_policy({"AUTO_TUNE_MIN_RELATIVE_GAIN": 0.05})

        result = auto_tune._model_validation(validation, policy)

        self.assertTrue(result["eligible"])
        self.assertNotIn("stress_liquidation", result["reasons"])

    def test_model_validation_does_not_veto_profitable_stress_liquidations(self):
        folds = [
            {
                "baselineNet": 100.0, "challengerNet": 140.0,
                "challengerOpenRate": 0.90, "challengerCapacityFit": 0.90,
            }
            for _ in range(3)
        ]
        validation = {
            "folds": folds,
            "foldWins": 3,
            "holdout": folds[-1],
            "baselineStressLiquidations": 0,
            "stressNet": 500.0,
            "stressLiquidations": 2,
        }
        policy = auto_tune.load_copy_policy({"AUTO_TUNE_MIN_RELATIVE_GAIN": 0.05})

        result = auto_tune._model_validation(validation, policy)

        self.assertTrue(result["eligible"])
        self.assertNotIn("stress_liquidation", result["reasons"])


if __name__ == "__main__":
    unittest.main()
