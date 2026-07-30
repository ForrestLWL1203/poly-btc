import unittest
from types import SimpleNamespace

from hyper.discovery import metrics


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
        "p90_orders_ep": 5,
        "heavy_orders_episode_n": 0,
        "max_concurrent": 4,
    }
    base.update(overrides)
    return base


def state_params(**overrides):
    base = {
        "inactive_days": 3,
        "min_activity": 0.21,
        "windfall_conc": 0.8,
        "windfall_win_max": 0.6,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def state_metrics(**overrides):
    base = {
        "bag_count": 0,
        "open_win_frac": 0.0,
        "last_fill_ms": 1000,
        "roi_total": 0.1,
        "net_pnl": 100.0,
        "activity_ratio": 0.8,
        "net_life": 100.0,
        "net_30d": 100.0,
        "hedge_ratio": 0.0,
        "pf_equity": 10000.0,
        "pf_turnover": 10.0,
        "pf_mon_vlm": 1_000_000.0,
        "pf_mon_pnl": 20_000.0,
        "pf_week_vlm": 500_000.0,
        "pf_week_pnl": 1_000.0,
        "profit_conc": 0.2,
        "win_rate": 0.5,
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

    def test_rejects_habitual_dca_only_with_five_complete_majority_grid_episodes(self):
        ok, reason = metrics.gates_structural(
            gate_metrics(median_adds_per_ep=4, max_adds_per_ep=4,
                         complete_episode_n=5, grid_episode_n=3),
            gate_params(grid_max_adds=3),
        )

        self.assertFalse(ok)
        self.assertEqual(reason, "grid_dca")

    def test_small_grid_sample_and_one_fill_outlier_are_not_structural_rejects(self):
        grid_ok, _ = metrics.gates_structural(
            gate_metrics(median_adds_per_ep=8, complete_episode_n=2, grid_episode_n=2),
            gate_params(grid_max_adds=3),
        )
        hft_ok, _ = metrics.gates_structural(
            gate_metrics(n_trades=10, p90_fills_ep=100),
            gate_params(max_fills_per_ep=50),
        )
        self.assertTrue(grid_ok)
        self.assertTrue(hft_ok)

    def test_systematic_distinct_order_slicing_requires_two_or_more_heavy_episodes(self):
        ok, reason = metrics.gates_structural(
            gate_metrics(n_trades=10, p90_fills_ep=500, p90_orders_ep=100,
                         heavy_orders_episode_n=2),
            gate_params(max_fills_per_ep=50),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "hft_uncopyable")

    def test_recent_copyable_loss_is_left_for_authoritative_copy_profile_gate(self):
        ok, reason = metrics.gates_state(
            state_metrics(net_pnl=-1.0, roi_total=0.1, net_30d=100.0, pf_mon_pnl=20_000.0),
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_target_account_edge_is_ignored_in_favor_of_scoped_copy_replay(self):
        values = state_metrics(pf_mon_pnl=500.0, pf_mon_vlm=1_000_000.0)

        ok, reason = metrics.gates_state(values)

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertNotIn("thin_edge_warning", values)


if __name__ == "__main__":
    unittest.main()
