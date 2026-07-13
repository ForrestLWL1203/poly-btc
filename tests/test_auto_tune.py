import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from hl import auto_tune, params, storage


class AutoTuneTests(unittest.TestCase):
    def test_strict_tune_comparison_reports_profit_and_liquidation_tradeoff(self):
        baseline = {
            "copy_net_pnl": 10_000.0, "closed_n": 20, "liquidations": 5,
            "max_drawdown": .30, "path_completion_rate": .75,
            "behavior_replication_rate": .60, "actionable_open_rate": .90,
            "capacity_open_fit": .90,
        }
        candidate = {
            "copy_net_pnl": 8_500.0, "closed_n": 20, "liquidations": 2,
            "max_drawdown": .18, "path_completion_rate": .90,
            "behavior_replication_rate": .82, "actionable_open_rate": .94,
            "capacity_open_fit": .92,
        }

        with patch.object(auto_tune, "evaluate_portfolio_window", side_effect=[baseline, candidate]) as replay:
            comparison = auto_tune.strict_tune_comparison(
                None, ["0xaaa"], {"MID_LEV_CAP": 10}, {"MID_LEV_CAP": 7},
                sigmas={}, now_ms=1, window_fills={30: [{}]}, risk_profile="balanced",
            )

        self.assertEqual(replay.call_count, 2)
        self.assertEqual(comparison["baseline"]["netPnl"], 10_000.0)
        self.assertEqual(comparison["candidate"]["netPnl"], 8_500.0)
        self.assertEqual(comparison["delta"]["netPnl"], -1_500.0)
        self.assertAlmostEqual(comparison["delta"]["netPnlPct"], -.15)
        self.assertEqual(comparison["delta"]["liquidationsAvoided"], 3)
        self.assertAlmostEqual(comparison["delta"]["liquidationReductionPct"], .60)
        self.assertAlmostEqual(comparison["delta"]["behaviorReplicationPctPoints"], .22)

    def test_generation_bound_tune_skips_if_generation_changes_before_apply(self):
        db = self._db()
        params.seed_params(db)
        db.execute("UPDATE params SET value='aggressive' WHERE key='AUTO_TUNE_RISK_PROFILE'")
        db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,publishable,is_current,started_at,published_at) "
            "VALUES ('g1','published',1,1,1,'2026-01-01','2026-01-01')"
        )
        db.commit()
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
                patch.object(auto_tune, "deploy_candidates", side_effect=candidate_axis), \
                patch.object(auto_tune, "evaluate_tune_candidate", side_effect=evaluate), \
                patch.object(auto_tune, "choose_margin_candidate", side_effect=lambda rows, _base, *_args: rows[0]), \
                patch.object(auto_tune, "add_candidates_from_axes", return_value=[]), \
                patch.object(auto_tune, "_walk_forward_validation", return_value={}), \
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

    def test_risk_profiles_run_one_objective_and_trade_profit_for_path_survival(self):
        def candidate(pnl, liqs, path):
            windows = {}
            for days, scale in ((30, 1.0), (14, .5), (7, .25)):
                windows[days] = {
                    "copy_net_pnl": pnl * scale,
                    "closed_n": 20,
                    "open_fill_rate": .92,
                    "capacity_open_fit": .92,
                    "liquidations": liqs,
                    "path_completion_rate": path,
                    "behavior_replication_rate": path * .92,
                    "target_open_events": 20,
                    "skip_reasons": {},
                }
            return {"mult": pnl, "windows": windows}

        baseline = candidate(1000, 5, .75)
        profit = candidate(2000, 5, .75)
        balanced = candidate(1700, 2, .90)
        conservative = candidate(1200, 0, 1.0)
        rows = [baseline, profit, balanced, conservative]

        self.assertIs(auto_tune.choose_margin_candidate(rows, baseline, "aggressive"), profit)
        self.assertIs(auto_tune.choose_margin_candidate(rows, baseline, "balanced"), balanced)
        self.assertIs(auto_tune.choose_margin_candidate(rows, baseline, "conservative"), conservative)

    def test_balanced_profile_rejects_exposure_increasing_parameter_directions(self):
        def row(params_, pnl=1000, liqs=2):
            return {
                "params": params_,
                "windows": {
                    days: {
                        "copy_net_pnl": pnl * scale, "closed_n": 20,
                        "open_fill_rate": .9, "capacity_open_fit": .9,
                        "liquidations": liqs, "max_drawdown": .10,
                        "path_completion_rate": .9, "behavior_replication_rate": .8,
                        "target_open_events": 20, "skip_reasons": {},
                    }
                    for days, scale in ((30, 1.0), (14, .5), (7, .25))
                },
            }

        baseline = row({
            "ADD_GAP_K": .08, "POS_ADD_GAP_K": .08,
            "ADD_GAP_SHRINK_G": 1.3, "ADD_MAX_HARD": 10,
        }, pnl=1000, liqs=4)
        earlier_adverse_add = row({
            "ADD_GAP_K": .04, "POS_ADD_GAP_K": .08,
            "ADD_GAP_SHRINK_G": 1.3, "ADD_MAX_HARD": 10,
        }, pnl=2000, liqs=2)
        safer_adds = row({
            "ADD_GAP_K": .12, "POS_ADD_GAP_K": .10,
            "ADD_GAP_SHRINK_G": 1.3, "ADD_MAX_HARD": 8,
        }, pnl=900, liqs=2)

        selected = auto_tune.choose_margin_candidate(
            [baseline, earlier_adverse_add, safer_adds], baseline, "balanced",
        )

        self.assertIs(selected, safer_adds)

    def test_balanced_validation_requires_full_liquidation_reduction_target(self):
        folds = [{
            "baselineNet": 100.0, "challengerNet": 90.0,
            "baselineClosedN": 10, "challengerClosedN": 10,
            "baselineLiquidations": 4, "challengerLiquidations": 3,
            "baselineMaxDD": .20, "challengerMaxDD": .18,
            "baselinePathCompletionRate": .8, "challengerPathCompletionRate": .85,
            "baselineReplicationRate": .7, "challengerReplicationRate": .75,
            "baselineOpenRate": .9, "challengerOpenRate": .9,
            "baselineCapacityFit": .9, "challengerCapacityFit": .9,
        } for _ in range(3)]
        validation = {
            "folds": folds, "holdout": folds[-1],
            "baselineStressNet": 100.0, "stressNet": 90.0,
            "baselineStressLiquidations": 4, "stressLiquidations": 3,
            "baselineStressMaxDD": .20, "stressMaxDD": .18,
        }

        result = auto_tune._model_validation(
            validation, auto_tune.load_copy_policy({}), risk_profile="balanced",
        )

        self.assertFalse(result["eligible"])
        self.assertIn("liquidation_reduction_below_profile_target", result["reasons"])

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
            "MID_LEV_CAP": 12.0, "HIGH_LEV_CAP": 4.0, "DEPLOY_FULL_PCT": 0.60,
        }

        candidates = auto_tune.independent_leverage_candidates(base)
        values = [candidate["params"] for candidate in candidates]

        self.assertTrue(any(row["STABLE_LEV_CAP"] == 32 and row["MID_LEV_CAP"] == 11 for row in values))
        self.assertTrue(any(row["STABLE_LEV_CAP"] == 32 and row["MID_LEV_CAP"] == 9 for row in values))
        self.assertTrue(any(row["HIGH_LEV_CAP"] == 5 for row in values))
        self.assertTrue(any(row["HIGH_LEV_CAP"] == 6 for row in values))
        self.assertFalse(any(
            row["STABLE_LEV_CAP"] != 32 and row["MID_LEV_CAP"] != 12 for row in values
        ))

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

    def test_choose_candidate_prioritizes_fewer_full_window_liquidations(self):
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

        self.assertEqual(selected["mult"], 1.0)

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

    def test_maybe_tune_margins_writes_add_params_after_sizing_grid(self):
        db = self._db()
        params.seed_params(db)
        db.execute("UPDATE params SET value='aggressive' WHERE key='AUTO_TUNE_RISK_PROFILE'")
        db.commit()
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

        def eval_tune(_db, _addrs, follow, candidate, sigmas=None, now_ms=None, **_kwargs):
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

        def eval_add(_db, _addrs, _follow, candidate, sigmas=None, now_ms=None, **_kwargs):
            out = dict(candidate)
            out["params"] = dict(candidate["params"])
            out["windows"] = better_windows if candidate["params"]["ADD_GAP_K"] == 0.06 else base_windows
            return out

        with patch.object(auto_tune, "_load_followed_wallets", return_value=["0xaaa"]), \
                patch.object(auto_tune, "_load_sigmas", return_value={}), \
                patch.object(auto_tune, "_portfolio_window_fills", return_value={30: [{}], 14: [{}], 7: [{}]}), \
                patch.object(auto_tune, "resolve_tune_baseline", side_effect=AssertionError("stale baseline used")), \
                patch.object(auto_tune, "resolve_add_baseline", side_effect=AssertionError("stale add baseline used")), \
                patch.object(auto_tune, "tune_candidates_from_axes", side_effect=tune_axes), \
                patch.object(auto_tune, "evaluate_tune_candidate", side_effect=eval_tune), \
                patch.object(auto_tune, "add_candidates_from_axes", side_effect=add_axes), \
                patch.object(auto_tune, "evaluate_add_candidate", side_effect=eval_add):
            res = auto_tune.maybe_tune_margins(db, source="test", mode="apply")

        self.assertFalse(res["applied"])
        self.assertFalse(res["eligible_to_apply"])
        self.assertIn("fewer_than_two_fold_wins", res["validation"]["reasons"])
        self.assertAlmostEqual(res["add_params"]["ADD_GAP_K"], 0.06)
        rows = dict(db.execute(
            "SELECT key,value FROM params WHERE key IN ('ADD_GAP_K','ADD_GAP_SHRINK_G','ADD_MAX_HARD')"
        ).fetchall())
        self.assertEqual(rows["ADD_GAP_K"], "0.12")
        self.assertEqual(rows["ADD_GAP_SHRINK_G"], "1.2")
        self.assertEqual(rows["ADD_MAX_HARD"], "8")

    def test_maybe_tune_margins_loads_portfolio_fills_once_for_both_grids(self):
        db = self._db()
        params.seed_params(db)
        load_calls = []
        fake_fills = [
            {"user": "0xaaa", "time": 1, "tid": 1, "coin": "BTC", "side": "B",
             "sz": "1", "startPosition": "0", "px": "100", "oid": 1, "crossed": True},
            {"user": "0xaaa", "time": 2, "tid": 2, "coin": "BTC", "side": "A",
             "sz": "1", "startPosition": "1", "px": "101", "oid": 2, "crossed": True},
        ]

        def one_tune_candidate(base):
            return [auto_tune.build_tune_candidate(
                base, 1.0, tuple(base[k] for k in auto_tune.LEV_KEYS), base["DEPLOY_FULL_PCT"]
            )]

        def one_add_candidate(base):
            return [auto_tune.build_add_candidate(
                base, float(base["ADD_GAP_K"]), float(base["ADD_GAP_SHRINK_G"]), int(base["ADD_MAX_HARD"])
            )]

        def load_once(_db, addrs, start_ms):
            load_calls.append((tuple(addrs), start_ms))
            return list(fake_fills)

        with patch.object(auto_tune, "_load_followed_wallets", return_value=["0xaaa"]), \
                patch.object(auto_tune, "_load_sigmas", return_value={"BTC": 0.04}), \
                patch.object(auto_tune, "tune_candidates_from_axes", side_effect=one_tune_candidate), \
                patch.object(auto_tune, "add_candidates_from_axes", side_effect=one_add_candidate), \
                patch.object(auto_tune, "_load_portfolio_fills", side_effect=load_once), \
                patch.object(auto_tune.time, "time", return_value=10.0):
            res = auto_tune.maybe_tune_margins(db, source="test")

        self.assertIn(res["status"], ("applied", "unchanged", "ok"))
        self.assertEqual(len(load_calls), 1)

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

        def one_tune_candidate(base):
            return [auto_tune.build_tune_candidate(
                base, 1.0, tuple(base[k] for k in auto_tune.LEV_KEYS), base["DEPLOY_FULL_PCT"]
            )]

        def one_add_candidate(base):
            return [auto_tune.build_add_candidate(
                base, float(base["ADD_GAP_K"]), float(base["ADD_GAP_SHRINK_G"]), int(base["ADD_MAX_HARD"])
            )]

        def load_window(_db, addrs, start_ms):
            load_calls.append((tuple(addrs), start_ms))
            return list(fake_fills)

        with patch.object(auto_tune, "_load_followed_wallets", return_value=["0xaaa"]), \
                patch.object(auto_tune, "_load_sigmas", return_value={"BTC": 0.04}), \
                patch.object(auto_tune, "tune_candidates_from_axes", side_effect=one_tune_candidate), \
                patch.object(auto_tune, "add_candidates_from_axes", side_effect=one_add_candidate), \
                patch.object(auto_tune, "_portfolio_fill_json_bytes",
                             return_value=auto_tune.config.AUTO_TUNE_FILL_CACHE_MAX_BYTES + 1), \
                patch.object(auto_tune, "_load_portfolio_fills", side_effect=load_window), \
                patch.object(auto_tune.time, "time", return_value=10.0):
            res = auto_tune.maybe_tune_margins(db, source="test")

        self.assertEqual(res["status"], "skipped")
        self.assertEqual(res["reason"], "fill_cache_guard")
        self.assertFalse(res["applied"])
        self.assertEqual(len(load_calls), 0)

    def test_shadow_mode_builds_proposal_without_writing_params(self):
        db = self._db()
        params.seed_params(db)
        before = dict(db.execute(
            "SELECT key,value FROM params WHERE key IN ('ADD_GAP_K','ADD_GAP_SHRINK_G','ADD_MAX_HARD')"
        ).fetchall())

        base_windows = {
            30: {"copy_net_pnl": 1000, "closed_n": 10, "capacity_open_fit": 0.9,
                 "liquidations": 0, "target_open_events": 10, "skip_reasons": {}},
            14: {"copy_net_pnl": 300, "closed_n": 5, "capacity_open_fit": 0.9,
                 "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
            7: {"copy_net_pnl": 100, "closed_n": 5, "capacity_open_fit": 1.0,
                "liquidations": 0, "target_open_events": 5, "skip_reasons": {}},
        }

        def one_tune(base):
            return [auto_tune.build_tune_candidate(
                base, 1.0, tuple(base[k] for k in auto_tune.LEV_KEYS), base["DEPLOY_FULL_PCT"]
            )]

        def fake_eval(_db, _addrs, follow, candidate, **_kwargs):
            out = dict(candidate)
            out["params"] = {k: follow[k] for k in auto_tune.TUNE_KEYS}
            out["margins"] = {k: follow[k] for k in auto_tune.MARGIN_KEYS}
            out["lev_caps"] = {k: follow[k] for k in auto_tune.LEV_KEYS}
            out["deploy_full_pct"] = follow["DEPLOY_FULL_PCT"]
            out["windows"] = base_windows
            return out

        with patch.object(auto_tune, "_load_followed_wallets", return_value=["0xaaa"]), \
                patch.object(auto_tune, "_portfolio_window_fills", return_value={30: [{}], 14: [{}], 7: [{}]}), \
                patch.object(auto_tune, "tune_candidates_from_axes", side_effect=one_tune), \
                patch.object(auto_tune, "evaluate_tune_candidate", side_effect=fake_eval), \
                patch.object(auto_tune, "add_candidates_from_axes", return_value=[]):
            result = auto_tune.maybe_tune_margins(db, source="test", mode="shadow")

        after = dict(db.execute(
            "SELECT key,value FROM params WHERE key IN ('ADD_GAP_K','ADD_GAP_SHRINK_G','ADD_MAX_HARD')"
        ).fetchall())
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["shadow"])
        self.assertFalse(result["applied"])
        self.assertEqual(after, before)

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

    def test_balanced_validation_accepts_profit_retention_with_material_safety_gain(self):
        folds = [
            {
                "baselineNet": 100.0, "challengerNet": 80.0,
                "baselineClosedN": 10, "challengerClosedN": 10,
                "baselineLiquidations": 2, "challengerLiquidations": 1,
                "baselinePathCompletionRate": .70, "challengerPathCompletionRate": .90,
                "baselineReplicationRate": .65, "challengerReplicationRate": .84,
                "challengerOpenRate": .90, "challengerCapacityFit": .90,
            }
            for _ in range(3)
        ]
        validation = {
            "folds": folds,
            "holdout": folds[-1],
            "baselineStressNet": 100.0,
            "stressNet": 80.0,
            "baselineStressLiquidations": 2,
            "stressLiquidations": 1,
        }
        policy = auto_tune.load_copy_policy({"AUTO_TUNE_MIN_RELATIVE_GAIN": 0.05})

        result = auto_tune._model_validation(validation, policy, risk_profile="balanced")

        self.assertTrue(result["eligible"], result["reasons"])
        self.assertEqual(result["riskProfile"], "balanced")

    def test_balanced_validation_rejects_low_open_capture_even_when_liquidations_fall(self):
        folds = [
            {
                "baselineNet": 100.0, "challengerNet": 80.0,
                "baselineClosedN": 10, "challengerClosedN": 10,
                "baselineLiquidations": 2, "challengerLiquidations": 0,
                "baselinePathCompletionRate": .70, "challengerPathCompletionRate": 1.0,
                "baselineReplicationRate": .65, "challengerReplicationRate": .50,
                "challengerOpenRate": .50, "challengerCapacityFit": .90,
            }
            for _ in range(3)
        ]
        validation = {
            "folds": folds, "holdout": folds[-1],
            "baselineStressNet": 100.0, "stressNet": 80.0,
            "baselineStressLiquidations": 2, "stressLiquidations": 0,
        }
        policy = auto_tune.load_copy_policy({"AUTO_TUNE_MIN_RELATIVE_GAIN": 0.05})

        result = auto_tune._model_validation(validation, policy, risk_profile="balanced")

        self.assertFalse(result["eligible"])
        self.assertIn("open_rate_below_floor", result["reasons"])

    def test_balanced_forward_check_keeps_intentional_profit_tradeoff_when_safety_improves(self):
        db = self._db()
        params.seed_params(db)
        current = {key: 1.0 for key in auto_tune.TUNE_KEYS + auto_tune.ADD_TUNE_KEYS}
        auto_tune._state_set(db, "active_tune_rollback", {
            "appliedAt": "2026-01-01T00:00:00Z",
            "addrs": ["0xaaa"],
            "oldParams": current,
            "newParams": current,
            "resolved": False,
        })
        db.commit()
        champion = {
            "copy_net_pnl": 1000.0,
            "closed_n": 10,
            "liquidations": 4,
            "path_completion_rate": .60,
            "behavior_replication_rate": .55,
        }
        applied = {
            "copy_net_pnl": 850.0,
            "closed_n": 10,
            "liquidations": 2,
            "path_completion_rate": .80,
            "behavior_replication_rate": .78,
        }

        with patch.object(auto_tune, "_load_portfolio_fills", return_value=[{}]), \
                patch.object(auto_tune, "run_backtest", side_effect=[champion, applied]):
            result = auto_tune._maybe_rollback_applied(
                db, params.load_follow(db), 2_000_000_000_000,
                risk_profile="balanced",
            )

        self.assertEqual(result["status"], "kept")
        self.assertTrue(result["safetyImproved"])
        self.assertEqual(result["riskProfile"], "balanced")

    def test_choose_follow_line_by_portfolio_prefers_profitable_prefix(self):
        db = self._db()
        params.seed_params(db)
        ranked = [
            {"addr": "0xaaa", "follow_score": 0.90},
            {"addr": "0xbbb", "follow_score": 0.84},
            {"addr": "0xccc", "follow_score": 0.78},
            {"addr": "0xddd", "follow_score": 0.72},
        ]
        windows_by_n = {
            2: {30: 2200, 14: 1800, 7: 600},
            3: {30: 1600, 14: 900, 7: 300},
            4: {30: 1500, 14: 850, 7: 250},
        }

        def fake_windows(_db, addrs, _sigmas, _overrides, _now_ms, window_fills=None):
            vals = windows_by_n[len(addrs)]
            return {
                day: {
                    "copy_net_pnl": pnl,
                    "closed_n": 10,
                    "capacity_open_fit": 0.95,
                    "open_fill_rate": 0.95,
                    "liquidations": 0,
                    "target_open_events": 10,
                    "skip_reasons": {},
                }
                for day, pnl in vals.items()
            }

        with patch.object(auto_tune.config, "AUTO_FOLLOW_MIN_N", 2), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_TARGET_N", 3), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_MAX_N", 4), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_MIN_SCORE", 0.60), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_PORTFOLIO_MIN_ABS_GAIN", 250.0), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_PORTFOLIO_MIN_REL_GAIN", 0.08), \
                patch.object(auto_tune, "_portfolio_window_fills", return_value={30: [{}], 14: [{}], 7: [{}]}), \
                patch.object(auto_tune, "_load_sigmas", return_value={}), \
                patch.object(auto_tune, "_candidate_windows", side_effect=fake_windows):
            choice = auto_tune.choose_follow_line_by_portfolio(db, ranked, stamp="2026-07-07T00:00:00Z")

        self.assertEqual(choice["status"], "ok")
        self.assertEqual(choice["reason"], "portfolio_topn")
        self.assertEqual(choice["count"], 2)
        self.assertLessEqual(choice["line"], 0.84)
        self.assertGreater(choice["line"], 0.83)

    def test_choose_follow_line_by_portfolio_skips_ineligible_wallets(self):
        db = self._db()
        params.seed_params(db)
        ranked = [
            {"addr": "0xaaa", "follow_score": 0.90, "follow_eligibility": {"eligible": True}},
            {"addr": "0xbad", "follow_score": 0.88, "follow_eligibility": {"eligible": False, "status": "low_fill_rate"}},
            {"addr": "0xbbb", "follow_score": 0.84, "follow_eligibility": {"eligible": True}},
            {"addr": "0xccc", "follow_score": 0.78, "follow_eligibility": {"eligible": True}},
        ]
        seen_prefixes = []

        def fake_windows(_db, addrs, _sigmas, _overrides, _now_ms, window_fills=None):
            seen_prefixes.append(tuple(addrs))
            return {
                day: {
                    "copy_net_pnl": pnl,
                    "closed_n": 10,
                    "capacity_open_fit": 0.95,
                    "open_fill_rate": 0.95,
                    "liquidations": 0,
                    "target_open_events": 10,
                    "skip_reasons": {},
                }
                for day, pnl in {30: 1200, 14: 800, 7: 300}.items()
            }

        with patch.object(auto_tune.config, "AUTO_FOLLOW_MIN_N", 2), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_TARGET_N", 3), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_MAX_N", 4), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_MIN_SCORE", 0.60), \
                patch.object(auto_tune, "_portfolio_window_fills", return_value={30: [{}], 14: [{}], 7: [{}]}), \
                patch.object(auto_tune, "_load_sigmas", return_value={}), \
                patch.object(auto_tune, "_candidate_windows", side_effect=fake_windows):
            choice = auto_tune.choose_follow_line_by_portfolio(db, ranked, stamp="2026-07-07T00:00:00Z")

        self.assertEqual(choice["status"], "ok")
        self.assertNotIn("0xbad", choice["selected"]["addrs"])
        self.assertTrue(all("0xbad" not in prefix for prefix in seen_prefixes))
        self.assertEqual(choice["max_n"], 3)

    def test_choose_follow_line_by_portfolio_keeps_capacity_when_edge_is_small(self):
        db = self._db()
        params.seed_params(db)
        ranked = [
            {"addr": "0xaaa", "follow_score": 0.90},
            {"addr": "0xbbb", "follow_score": 0.84},
            {"addr": "0xccc", "follow_score": 0.78},
        ]
        windows_by_n = {
            2: {30: 1200, 14: 1050, 7: 500},
            3: {30: 1180, 14: 1000, 7: 480},
        }

        def fake_windows(_db, addrs, _sigmas, _overrides, _now_ms, window_fills=None):
            vals = windows_by_n[len(addrs)]
            return {
                day: {
                    "copy_net_pnl": pnl,
                    "closed_n": 10,
                    "capacity_open_fit": 0.95,
                    "open_fill_rate": 0.95,
                    "liquidations": 0,
                    "target_open_events": 10,
                    "skip_reasons": {},
                }
                for day, pnl in vals.items()
            }

        with patch.object(auto_tune.config, "AUTO_FOLLOW_MIN_N", 2), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_TARGET_N", 3), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_MAX_N", 3), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_MIN_SCORE", 0.60), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_PORTFOLIO_MIN_ABS_GAIN", 250.0), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_PORTFOLIO_MIN_REL_GAIN", 0.08), \
                patch.object(auto_tune, "_portfolio_window_fills", return_value={30: [{}], 14: [{}], 7: [{}]}), \
                patch.object(auto_tune, "_load_sigmas", return_value={}), \
                patch.object(auto_tune, "_candidate_windows", side_effect=fake_windows):
            choice = auto_tune.choose_follow_line_by_portfolio(db, ranked, stamp="2026-07-07T00:00:00Z")

        self.assertEqual(choice["status"], "ok")
        self.assertEqual(choice["reason"], "portfolio_flat_capacity")
        self.assertEqual(choice["count"], 3)

    def test_follow_line_portfolio_requires_recent_sample_size(self):
        db = self._db()
        params.seed_params(db)
        ranked = [
            {"addr": "0xaaa", "follow_score": 0.90},
            {"addr": "0xbbb", "follow_score": 0.84},
            {"addr": "0xccc", "follow_score": 0.78},
        ]

        def fake_windows(_db, addrs, _sigmas, _overrides, _now_ms, window_fills=None):
            return {
                30: {"copy_net_pnl": 1400, "closed_n": 12, "capacity_open_fit": 0.95,
                     "open_fill_rate": 0.95, "liquidations": 0, "target_open_events": 12, "skip_reasons": {}},
                14: {"copy_net_pnl": 900, "closed_n": 6, "capacity_open_fit": 0.95,
                     "open_fill_rate": 0.95, "liquidations": 0, "target_open_events": 6, "skip_reasons": {}},
                7: {"copy_net_pnl": 500, "closed_n": 2, "capacity_open_fit": 1.0,
                    "open_fill_rate": 1.0, "liquidations": 0, "target_open_events": 2, "skip_reasons": {}},
            }

        with patch.object(auto_tune.config, "AUTO_FOLLOW_MIN_N", 2), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_TARGET_N", 3), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_MAX_N", 3), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_MIN_SCORE", 0.60), \
                patch.object(auto_tune.config, "COPY_BT_MIN_CLOSED", 7), \
                patch.object(auto_tune.config, "COPY_BT_MIN_CLOSED_14D", 5), \
                patch.object(auto_tune.config, "COPY_BT_MIN_CLOSED_7D", 5), \
                patch.object(auto_tune, "_portfolio_window_fills", return_value={30: [{}], 14: [{}], 7: [{}]}), \
                patch.object(auto_tune, "_load_sigmas", return_value={}), \
                patch.object(auto_tune, "_candidate_windows", side_effect=fake_windows):
            choice = auto_tune.choose_follow_line_by_portfolio(db, ranked, stamp="2026-07-07T00:00:00Z")

        self.assertEqual(choice["status"], "fallback")
        self.assertEqual(choice["reason"], "no_valid_portfolio_prefix")

    def test_follow_line_portfolio_cuts_before_recent_pnl_cliff(self):
        db = self._db()
        params.seed_params(db)
        ranked = [
            {"addr": "0xaaa", "follow_score": 0.92},
            {"addr": "0xbbb", "follow_score": 0.86},
            {"addr": "0xccc", "follow_score": 0.80},
            {"addr": "0xddd", "follow_score": 0.74},
        ]
        windows_by_n = {
            2: {30: 1000, 14: 800, 7: 400},
            3: {30: 1200, 14: 900, 7: 500},
            4: {30: 5000, 14: 500, 7: 50},
        }

        def fake_windows(_db, addrs, _sigmas, _overrides, _now_ms, window_fills=None):
            vals = windows_by_n[len(addrs)]
            return {
                day: {
                    "copy_net_pnl": pnl,
                    "closed_n": 10,
                    "capacity_open_fit": 0.95,
                    "open_fill_rate": 0.95,
                    "liquidations": 0,
                    "target_open_events": 10,
                    "skip_reasons": {},
                }
                for day, pnl in vals.items()
            }

        with patch.object(auto_tune.config, "AUTO_FOLLOW_MIN_N", 2), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_TARGET_N", 4), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_MAX_N", 4), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_MIN_SCORE", 0.60), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_PORTFOLIO_MIN_ABS_GAIN", 100.0), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_PORTFOLIO_MIN_REL_GAIN", 0.05), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_PORTFOLIO_MAX_RECENT_DROP_ABS", 100.0), \
                patch.object(auto_tune.config, "AUTO_FOLLOW_PORTFOLIO_MAX_RECENT_DROP_REL", 0.25), \
                patch.object(auto_tune, "_portfolio_window_fills", return_value={30: [{}], 14: [{}], 7: [{}]}), \
                patch.object(auto_tune, "_load_sigmas", return_value={}), \
                patch.object(auto_tune, "_candidate_windows", side_effect=fake_windows):
            choice = auto_tune.choose_follow_line_by_portfolio(db, ranked, stamp="2026-07-07T00:00:00Z")

        self.assertEqual(choice["status"], "ok")
        self.assertEqual(choice["reason"], "portfolio_recent_cliff")
        self.assertEqual(choice["count"], 3)
        self.assertEqual(choice["selected"]["addrs"], ["0xaaa", "0xbbb", "0xccc"])


if __name__ == "__main__":
    unittest.main()
