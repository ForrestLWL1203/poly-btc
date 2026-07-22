import unittest

from hyper.copy.wallet_risk import HighWaterPolicy, advance_high_water, new_high_water_state


class WalletHighWaterTests(unittest.TestCase):
    def setUp(self):
        self.policy = HighWaterPolicy(
            freeze_drawdown=0.03, reduce_drawdown=0.06, exit_drawdown=0.10,
            release_drawdown=0.02, cooldown_ms=7 * 86_400_000,
        )
        self.state = new_high_water_state(
            membership_cycle="g1:0xabc", baseline_equity=10_000,
            selection_generation="g1", now_ms=1,
        )

    def advance(self, equity, now=2, retention=False):
        self.state, action = advance_high_water(
            self.state, current_equity=equity, now_ms=now,
            policy=self.policy, retention_passed=retention,
        )
        return action

    def test_profit_high_water_then_giveback_triggers_3_6_10(self):
        self.assertIsNone(self.advance(12_000))
        self.assertEqual(self.state["high_water_equity"], 12_000)
        self.assertEqual(self.advance(11_600), "freeze_new")
        self.assertEqual(self.state["breaker_stage"], 1)
        self.assertEqual(self.advance(11_300), "reduce_half")
        self.assertEqual(self.state["breaker_stage"], 2)
        self.state["reduced_in_cycle"] = True
        self.assertEqual(self.advance(10_900, now=10), "exit_all")
        self.assertEqual(self.state["breaker_stage"], 3)
        self.assertEqual(self.state["cooldown_until_ms"], 10 + 7 * 86_400_000)

    def test_stage_one_requires_recovery_and_new_retention_pass(self):
        self.advance(12_000)
        self.advance(11_600)
        self.assertIsNone(self.advance(11_900, retention=False))
        self.assertEqual(self.state["breaker_stage"], 1)
        self.assertEqual(self.advance(11_900, retention=True), "release_freeze")
        self.assertEqual(self.state["breaker_stage"], 0)

    def test_stage_two_never_refills_in_same_member_cycle(self):
        self.advance(12_000)
        self.advance(11_300)
        self.state["reduced_in_cycle"] = True
        self.assertIsNone(self.advance(12_100, retention=True))
        self.assertEqual(self.state["breaker_stage"], 2)

    def test_stage_three_cooldown_deadline_does_not_slide_on_every_tick(self):
        self.advance(12_000)
        self.advance(10_900, now=100)
        deadline = self.state["cooldown_until_ms"]
        self.assertEqual(self.advance(10_800, now=200), "exit_all")
        self.assertEqual(self.state["cooldown_until_ms"], deadline)


if __name__ == "__main__":
    unittest.main()
