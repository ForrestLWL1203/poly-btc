import unittest

from hyper.execution.signal_bridge_verifier import _open_signal


class SignalBridgeVerifierTests(unittest.TestCase):
    def test_only_standard_perp_open_transitions_are_signals(self):
        self.assertTrue(_open_signal({"coin": "BTC", "dir": "Open Long", "side": "B"}))
        self.assertTrue(_open_signal({"coin": "ETH", "dir": "Open Short", "side": "A"}))
        self.assertFalse(_open_signal({"coin": "BTC", "dir": "Close Long", "side": "A"}))
        self.assertFalse(_open_signal({"coin": "xyz:XYZ100", "dir": "Open Long", "side": "B"}))
        self.assertFalse(_open_signal({"coin": "@107", "dir": "Open Long", "side": "B"}))


if __name__ == "__main__":
    unittest.main()
