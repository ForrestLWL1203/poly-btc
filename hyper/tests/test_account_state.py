import unittest

from hyper.execution.account_state import snapshot_account_values, snapshot_positions
from hyper.execution.hyperliquid_broker import AccountSnapshot
from hyper.execution.venue import ExecutionNetwork


ACCOUNT = "0x" + "a" * 40


class AccountStateTests(unittest.TestCase):
    def test_unified_hold_is_not_double_counted_with_isolated_margin(self):
        snapshot = AccountSnapshot(
            network=ExecutionNetwork.MAINNET,
            account_address=ACCOUNT,
            abstraction="unifiedAccount",
            collateral_state={
                "balances": [{"coin": "USDC", "total": "2243.3759", "hold": "879.2184"}],
            },
            perp_states={
                "": {
                    "assetPositions": [{
                        "position": {
                            "coin": "BTC",
                            "szi": "0.05537",
                            "entryPx": "64000",
                            "positionValue": "3551.82",
                            "marginUsed": "879.2184",
                            "leverage": {"type": "isolated", "value": 20},
                            "unrealizedPnl": "10.45",
                            "liquidationPx": "66342",
                        },
                    }],
                },
            },
            open_orders={"": []},
            frontend_open_orders={"": []},
        )

        positions = snapshot_positions(snapshot)
        equity, available = snapshot_account_values(snapshot, positions)

        self.assertAlmostEqual(equity, 2243.3759)
        self.assertAlmostEqual(available, 1364.1575)

    def test_unified_hold_covers_all_reserved_collateral(self):
        snapshot = AccountSnapshot(
            network=ExecutionNetwork.TESTNET,
            account_address=ACCOUNT,
            abstraction="unifiedAccount",
            collateral_state={"balances": [{"coin": "USDC", "total": "100", "hold": "30"}]},
            perp_states={"": {"assetPositions": []}},
            open_orders={"": [{"oid": 1}]},
            frontend_open_orders={"": [{"oid": 1}]},
        )

        self.assertEqual(snapshot_account_values(snapshot, []), (100.0, 70.0))


if __name__ == "__main__":
    unittest.main()
