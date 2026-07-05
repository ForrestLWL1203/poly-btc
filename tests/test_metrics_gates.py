import unittest
from types import SimpleNamespace

from hl import metrics


def gate_params(**overrides):
    base = {
        "min_perp": 0.6,
        "evidence_min_days": 5,
        "evidence_min_trades": 7,
        "max_daily_eps": 30,
        "exclude_hft": True,
        "hft_min_hold_min": 3.0,
        "grid_max_adds": 3,
        "max_single_adds": 10,
        "max_fills_per_ep": 50,
        "max_concurrent_pos": 15,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def gate_metrics(**overrides):
    base = {
        "perp_frac": 1.0,
        "active_days": 8,
        "n_trades": 20,
        "median_eps": 2,
        "median_hold_s": 3600,
        "median_adds_per_ep": 0,
        "max_adds_per_ep": 0,
        "p90_fills_ep": 5,
        "max_concurrent": 4,
    }
    base.update(overrides)
    return base


class MetricsGateTests(unittest.TestCase):
    def test_rejects_one_off_heavy_dca_even_when_median_is_low(self):
        ok, reason = metrics.gates_structural(
            gate_metrics(median_adds_per_ep=0, max_adds_per_ep=23),
            gate_params(max_single_adds=20),
        )

        self.assertFalse(ok)
        self.assertEqual(reason, "heavy_dca")

    def test_allows_max_adds_at_single_episode_limit(self):
        ok, reason = metrics.gates_structural(
            gate_metrics(median_adds_per_ep=0, max_adds_per_ep=20),
            gate_params(max_single_adds=20),
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_rejects_habitual_dca_by_median_adds(self):
        ok, reason = metrics.gates_structural(
            gate_metrics(median_adds_per_ep=4, max_adds_per_ep=4),
            gate_params(grid_max_adds=3),
        )

        self.assertFalse(ok)
        self.assertEqual(reason, "grid_dca")


if __name__ == "__main__":
    unittest.main()
