import json
import unittest

from hl import sector


def bt(net, closed, wins=None, target_open=None, opened=None):
    return {
        "copy_net_pnl": net,
        "closed_n": closed,
        "wins": closed if wins is None else wins,
        "target_open_events": closed if target_open is None else target_open,
        "opened_n": closed if opened is None else opened,
        "liquidations": 0,
        "fee_drag": 10,
    }


class SectorPolicyTests(unittest.TestCase):
    def test_classifies_crypto_and_xyz_stock_sectors(self):
        self.assertEqual(sector.classify_coin("BTC"), "crypto")
        self.assertEqual(sector.classify_coin("HYPE"), "crypto")
        self.assertEqual(sector.classify_coin("xyz:SP500"), "stock")
        self.assertEqual(sector.classify_coin("XYZ:MU"), "stock")

    def test_policy_allows_profitable_sector_and_denies_losing_sector(self):
        sector_results = {
            "crypto": {
                30: bt(1200, 10),
                14: bt(600, 6),
                7: bt(220, 5),
            },
            "stock": {
                30: bt(-900, 10, wins=2),
                14: bt(-500, 6, wins=1),
                7: bt(-200, 5, wins=1),
            },
        }

        policy = sector.evaluate_sector_policy(sector_results)

        self.assertTrue(policy["crypto"]["allow"])
        self.assertEqual(policy["crypto"]["status"], "allowed")
        self.assertFalse(policy["stock"]["allow"])
        self.assertEqual(policy["stock"]["status"], "recent_loss")
        self.assertEqual(policy["allowed"], ["crypto"])

    def test_allowed_sector_metrics_ignore_disallowed_losing_sector(self):
        metrics = {
            "score": 0.70,
            "copy_bt_net_pnl": -1000,
            "copy_bt_14d_net_pnl": -600,
            "copy_bt_7d_net_pnl": -250,
            "copy_bt_closed_n": 20,
            "copy_bt_14d_closed_n": 12,
            "copy_bt_7d_closed_n": 8,
            "sector_policy_json": json.dumps({
                "crypto": {"allow": True},
                "stock": {"allow": False},
            }),
            "sector_copy_json": json.dumps({
                "crypto": {
                    "30": bt(1500, 10),
                    "14": bt(700, 6),
                    "7": bt(300, 5),
                },
                "stock": {
                    "30": bt(-2500, 10),
                    "14": bt(-1300, 6),
                    "7": bt(-550, 5),
                },
            }),
        }

        adjusted = sector.apply_allowed_sector_copy_metrics(metrics)

        self.assertEqual(adjusted["copy_bt_net_pnl"], 1500)
        self.assertEqual(adjusted["copy_bt_14d_net_pnl"], 700)
        self.assertEqual(adjusted["copy_bt_7d_net_pnl"], 300)
        self.assertEqual(adjusted["copy_bt_closed_n"], 10)
        self.assertEqual(adjusted["copy_bt_14d_closed_n"], 6)
        self.assertEqual(adjusted["copy_bt_7d_closed_n"], 5)


if __name__ == "__main__":
    unittest.main()
