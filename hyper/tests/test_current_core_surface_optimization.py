import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hyper import params, storage
from hyper.discovery import scanner


class CurrentCoreSurfaceOptimizationTests(unittest.TestCase):
    def open_db(self, td):
        return storage.connect(
            str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
        )

    def seed_current_core(self, db):
        db.execute(
            "INSERT INTO scan_generation "
            "(generation,status,complete,publishable,is_current,started_at,profile_complete) "
            "VALUES ('g-current','published',1,1,1,'2026-01-01T00:00:00Z',1)"
        )
        db.executemany(
            "INSERT INTO follow_selection "
            "(generation,addr,role,enabled,selected_at,selection_rank) VALUES (?,?,?,?,?,?)",
            [
                ("g-current", "0xaaa", "core", 1, "2026-01-01T00:01:00Z", 1),
                ("g-current", "0xbbb", "core", 1, "2026-01-01T00:01:00Z", 2),
            ],
        )
        db.commit()

    def test_full_surface_optimization_locks_membership_and_refreshes_replay(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            self.seed_current_core(db)
            before = params.load_follow(db)
            proposal = {
                key: before[key]
                for key in (*scanner.auto_tune.TUNE_KEYS, *scanner.auto_tune.ADD_TUNE_KEYS)
            }
            proposal["STABLE_MARGIN_PCT"] = float(proposal["STABLE_MARGIN_PCT"]) + .01
            exact_follow = {**before, **proposal, "AMBIGUOUS_PATH_MODE": "liquidate"}
            exact = {
                "follow": exact_follow,
                "reason": "validated_proposal",
                "run": {"search_profile": "efficient", "quick_replay_count": 9},
                "finalistAudit": [{"feasible": True}],
            }
            formation = {
                "selected": ("0xaaa", "0xbbb"),
                "params": proposal,
                "search": {
                    "finalMarginCalibration": {
                        "status": "ok", "quick_replay_count": 4,
                        "finalists": [{"eligible": True}],
                    },
                    "finalDeploymentUtilization": {"timeWeightedAvgDeployPct": 71.0},
                    "tierEconomics": {}, "resourcePeak": {},
                },
            }
            with patch.object(
                scanner, "_quality_core_profiles",
                return_value=[{"addr": "0xaaa"}, {"addr": "0xbbb"}],
            ), patch.object(
                scanner.auto_tune, "_load_sigmas", return_value={},
            ), patch.object(
                scanner.auto_tune, "_load_market_ctx", return_value={},
            ), patch.object(
                scanner, "_retune_exact_membership_surface", return_value=exact,
            ) as full_tune, patch.object(
                scanner, "form_quality_prefix", return_value=formation,
            ) as final_calibration, patch.object(
                scanner.strategy_revision, "active_revision_id", return_value="r0",
            ), patch.object(
                scanner.strategy_revision, "create_revision", return_value={"revision": "r1"},
            ) as create, patch.object(
                scanner, "_apply_formation_params", return_value=True,
            ) as apply_params, patch.object(
                scanner.effective_replay, "certify_and_store",
                return_value={"status": "ok", "dynamicReturn30d": 1.5},
            ) as certify:
                result = scanner.optimize_current_core_surface(db, apply=True)

            self.assertEqual(result["status"], "applied")
            self.assertEqual(result["coreCount"], 2)
            self.assertTrue(result["changed"])
            self.assertEqual(result["strategyRevision"], "r1")
            self.assertEqual(full_tune.call_args.args[1], ("0xaaa", "0xbbb"))
            self.assertEqual(
                final_calibration.call_args.kwargs["_fixed_membership_addrs"],
                ("0xaaa", "0xbbb"),
            )
            self.assertEqual(
                final_calibration.call_args.kwargs["_follow_override"], exact_follow,
            )
            self.assertTrue(
                final_calibration.call_args.kwargs["_margin_calibration_only"],
            )
            self.assertEqual(
                create.call_args.kwargs["source"],
                "current_core_full_surface_calibration",
            )
            apply_params.assert_called_once()
            certify.assert_called_once_with(db, "g-current")
            self.assertEqual(result["portfolioReplay"]["dynamicReturn30d"], 1.5)
            db.close()

    def test_full_surface_optimization_rejects_member_loss(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.open_db(td)
            self.seed_current_core(db)
            before = params.load_follow(db)
            exact = {
                "follow": {**before, "AMBIGUOUS_PATH_MODE": "liquidate"},
                "reason": "validated_proposal", "run": {}, "finalistAudit": [],
            }
            with patch.object(
                scanner, "_quality_core_profiles",
                return_value=[{"addr": "0xaaa"}, {"addr": "0xbbb"}],
            ), patch.object(
                scanner.auto_tune, "_load_sigmas", return_value={},
            ), patch.object(
                scanner.auto_tune, "_load_market_ctx", return_value={},
            ), patch.object(
                scanner, "_retune_exact_membership_surface", return_value=exact,
            ), patch.object(
                scanner, "form_quality_prefix",
                return_value={"selected": ("0xaaa",), "params": {}, "search": {}},
            ), patch.object(scanner, "_apply_formation_params") as apply_params:
                with self.assertRaisesRegex(
                    RuntimeError, "membership_not_certified:2:1",
                ):
                    scanner.optimize_current_core_surface(db, apply=True)
            apply_params.assert_not_called()
            db.close()


if __name__ == "__main__":
    unittest.main()
