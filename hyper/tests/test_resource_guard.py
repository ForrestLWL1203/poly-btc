import unittest
from unittest import mock

from hyper.ops import resource_guard


class ResourceGuardTests(unittest.TestCase):
    def test_decoded_surface_and_process_tree_are_budgeted(self):
        with mock.patch.object(resource_guard, "available_memory_bytes", return_value=300 * 1024**2), \
                mock.patch.object(resource_guard, "process_tree_usage_bytes", return_value=(800 * 1024**2, 0)), \
                mock.patch.object(resource_guard, "physical_memory_bytes", return_value=1600 * 1024**2):
            detail = resource_guard.assess_replay_budget(64 * 1024**2)
        self.assertEqual(detail["status"], "resource_deferred")
        self.assertIn("available_memory", detail["reasons"])
        self.assertIn("process_tree_rss", detail["reasons"])

    def test_require_raises_resumable_resource_signal(self):
        with mock.patch.object(resource_guard, "assess_replay_budget", return_value={
            "status": "resource_deferred", "reasons": ["process_tree_swap"],
        }):
            with self.assertRaises(resource_guard.ResourceDeferred):
                resource_guard.require_replay_budget()


if __name__ == "__main__":
    unittest.main()
