import unittest

from hyper.execution.hyperliquid_broker import AccountSnapshot, IdentitySnapshot
from hyper.execution.preflight import AccountPreflightCode, evaluate_account_preflight
from hyper.execution.venue import ExecutionNetwork


ACCOUNT = "0x" + "a" * 40
AGENT = "0x" + "b" * 40


def identity(owner=ACCOUNT):
    return IdentitySnapshot(
        account_address=ACCOUNT,
        account_role={"role": "user"},
        agent_address=AGENT,
        agent_role={"role": "agent", "data": {"user": owner}},
    )


def snapshot(total="100", hold="0", *, abstraction="unifiedAccount", positions=None, orders=None):
    return AccountSnapshot(
        network=ExecutionNetwork.TESTNET,
        account_address=ACCOUNT,
        abstraction=abstraction,
        collateral_state={"balances": [{"coin": "USDC", "total": total, "hold": hold}]},
        perp_states={"": {"assetPositions": positions or []}},
        open_orders={"": orders or []},
        frontend_open_orders={"": orders or []},
    )


class ExecutionPreflightTests(unittest.TestCase):
    def test_clean_unified_account_with_capacity_passes(self):
        result = evaluate_account_preflight(identity(), snapshot())

        self.assertTrue(result.ok)
        self.assertEqual(result.code, AccountPreflightCode.OK)
        self.assertEqual(result.available_collateral, 100)

    def test_agent_mode_funds_capacity_and_cleanliness_fail_closed(self):
        cases = [
            (identity("0x" + "c" * 40), snapshot(), AccountPreflightCode.AGENT_MISMATCH),
            (identity(), snapshot(abstraction="default"), AccountPreflightCode.UNSUPPORTED_ACCOUNT_MODE),
            (identity(), snapshot(total="0"), AccountPreflightCode.NO_AVAILABLE_COLLATERAL),
            (identity(), snapshot(total="9.99"), AccountPreflightCode.NO_EXECUTABLE_CAPACITY),
            (identity(), snapshot(positions=[{"position": {"szi": "0.1"}}]), AccountPreflightCode.ACCOUNT_NOT_CLEAN),
            (identity(), snapshot(orders=[{"oid": 17}]), AccountPreflightCode.ACCOUNT_NOT_CLEAN),
        ]
        for ident, account, expected in cases:
            with self.subTest(expected=expected):
                result = evaluate_account_preflight(ident, account)
                self.assertFalse(result.ok)
                self.assertEqual(result.code, expected)


if __name__ == "__main__":
    unittest.main()
