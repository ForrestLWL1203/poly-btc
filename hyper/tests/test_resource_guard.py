import unittest
from unittest import mock

from hyper.ops import resource_guard


class ResourceGuardTests(unittest.TestCase):
    def test_decoded_surface_and_process_tree_are_budgeted(self):
        with mock.patch.object(resource_guard, "available_memory_bytes", return_value=300 * 1024**2), \
                mock.patch.object(resource_guard, "process_tree_usage_bytes", return_value=(800 * 1024**2, 0)), \
                mock.patch.object(resource_guard, "physical_memory_bytes", return_value=1600 * 1024**2), \
                mock.patch.object(resource_guard, "cgroup_memory_metrics", return_value={
                    "currentBytes": 900 * 1024**2,
                    "anonymousBytes": 800 * 1024**2,
                    "fileCacheBytes": 100 * 1024**2,
                    "unreclaimableBytes": 0,
                    "workingSetBytes": 800 * 1024**2,
                    "events": {},
                }):
            detail = resource_guard.assess_replay_budget(64 * 1024**2)
        self.assertEqual(detail["status"], "resource_deferred")
        self.assertIn("available_memory", detail["reasons"])
        self.assertIn("process_tree_rss", detail["reasons"])

    def test_reclaimable_sqlite_file_cache_does_not_defer_resume(self):
        with mock.patch.object(resource_guard, "available_memory_bytes", return_value=1200 * 1024**2), \
                mock.patch.object(resource_guard, "process_tree_usage_bytes", return_value=(220 * 1024**2, 8 * 1024**2)), \
                mock.patch.object(resource_guard, "physical_memory_bytes", return_value=1600 * 1024**2), \
                mock.patch.object(resource_guard, "cgroup_memory_metrics", return_value={
                    "currentBytes": 1060 * 1024**2,
                    "anonymousBytes": 225 * 1024**2,
                    "fileCacheBytes": 825 * 1024**2,
                    "unreclaimableBytes": 10 * 1024**2,
                    "workingSetBytes": 235 * 1024**2,
                    "events": {"high": 10, "oom": 0, "oom_kill": 0},
                }):
            detail = resource_guard.assess_replay_budget()
        self.assertEqual(detail["status"], "ok")
        self.assertEqual(detail["cgroupFileCacheBytes"], 825 * 1024**2)
        self.assertNotIn("cgroup_memory", detail["reasons"])

    def test_untracked_cgroup_working_set_still_defers(self):
        with mock.patch.object(resource_guard, "available_memory_bytes", return_value=1200 * 1024**2), \
                mock.patch.object(resource_guard, "process_tree_usage_bytes", return_value=(200 * 1024**2, 0)), \
                mock.patch.object(resource_guard, "physical_memory_bytes", return_value=1600 * 1024**2), \
                mock.patch.object(resource_guard, "cgroup_memory_metrics", return_value={
                    "currentBytes": 1100 * 1024**2,
                    "anonymousBytes": 1030 * 1024**2,
                    "fileCacheBytes": 50 * 1024**2,
                    "unreclaimableBytes": 10 * 1024**2,
                    "workingSetBytes": 1040 * 1024**2,
                    "events": {},
                }):
            detail = resource_guard.assess_replay_budget()
        self.assertEqual(detail["status"], "resource_deferred")
        self.assertIn("cgroup_working_set", detail["reasons"])

    def test_require_raises_resumable_resource_signal(self):
        with mock.patch.object(resource_guard, "assess_replay_budget", return_value={
            "status": "resource_deferred", "reasons": ["process_tree_swap"],
        }):
            with self.assertRaises(resource_guard.ResourceDeferred):
                resource_guard.require_replay_budget()


if __name__ == "__main__":
    unittest.main()
