import unittest
from unittest.mock import MagicMock, patch

from hyper.discovery import scanner


class FixedSurfaceReformTests(unittest.TestCase):
    def test_cached_strict_replay_is_rescored_by_current_model(self):
        cached = {
            "metrics": {"source_episode_n_7d": 123},
            "score": 0.99,
            "scoreDetail": {"mode": "stale"},
            "qualification": {"eligible": True},
        }
        with patch.object(
            scanner.follow_score, "compute_follow_score",
            return_value=(0.8628, {"mode": "current"}),
        ) as compute:
            refreshed = scanner._rescore_cached_effective_replay(
                cached, {"MARGIN_EQUITY_PCT": 0.01},
            )

        self.assertEqual(refreshed["score"], 0.8628)
        self.assertEqual(refreshed["scoreDetail"], {"mode": "current"})
        self.assertEqual(refreshed["qualification"], {"eligible": True})
        self.assertEqual(cached["score"], 0.99)
        compute.assert_called_once_with(
            cached["metrics"],
            policy_values={"MARGIN_EQUITY_PCT": 0.01},
            stage="strict",
        )

    def test_wrapper_reranks_and_repairs_without_retuning(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = (1_800_000_000_000,)
        with patch.object(
            scanner.selection, "latest_published_generation", return_value="g1",
        ), patch.object(
            scanner, "_rerank_cached_pre_strict_queue",
            return_value={"scored": 10, "queued": 10},
        ) as rerank, patch.object(
            scanner, "repair_published_selection",
            return_value={"status": "repaired", "core": 4},
        ) as repair:
            result = scanner.reform_published_generation_current_surface(
                db, stamp="now",
            )

        self.assertEqual(result["status"], "ok")
        rerank.assert_called_once_with(db, "g1", now_ms=1_800_000_000_000)
        repair.assert_called_once_with(
            db, "g1", stamp="now", replace_existing=True,
            retune_formation=False, force_entry_requalification=True,
            fixed_current_surface=True,
        )

    def test_fixed_surface_and_retune_are_mutually_exclusive(self):
        with self.assertRaisesRegex(
            ValueError, "fixed_current_surface_conflicts_with_retune",
        ):
            scanner.repair_published_selection(
                MagicMock(), retune_formation=True, fixed_current_surface=True,
            )


if __name__ == "__main__":
    unittest.main()
