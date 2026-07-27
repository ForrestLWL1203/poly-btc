import unittest

from hyper.execution.liquidity import assess_order_book


def book(*, bid=99.99, ask=100.01, bid_size=100.0, ask_size=100.0):
    return {
        "levels": [
            [{"px": str(bid), "sz": str(bid_size)}],
            [{"px": str(ask), "sz": str(ask_size)}],
        ],
    }


class LiveLiquidityTests(unittest.TestCase):
    def test_small_copy_passes_deep_book(self):
        result = assess_order_book(
            book(), is_buy=True, planned_notional=2_700,
            max_spread_bps=20, max_impact_bps=35,
        )

        self.assertTrue(result["available"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["fill_ratio"], 1.0)
        self.assertLess(result["impact_bps"], 2.0)

    def test_actual_order_larger_than_visible_depth_is_rejected(self):
        result = assess_order_book(
            book(bid_size=1.0, ask_size=1.0),
            is_buy=True, planned_notional=2_700,
            max_spread_bps=20, max_impact_bps=35,
        )

        self.assertEqual(result["reason"], "book_depth")
        self.assertLess(result["fill_ratio"], 0.04)

    def test_wide_spread_is_rejected_even_with_enough_size(self):
        result = assess_order_book(
            book(bid=99.0, ask=101.0),
            is_buy=False, planned_notional=2_700,
            max_spread_bps=20, max_impact_bps=200,
        )

        self.assertEqual(result["reason"], "book_spread")

    def test_deep_levels_with_excessive_average_impact_are_rejected(self):
        value = {
            "levels": [
                [{"px": "100", "sz": "100"}],
                [{"px": "100.01", "sz": "1"}, {"px": "101", "sz": "100"}],
            ],
        }
        result = assess_order_book(
            value, is_buy=True, planned_notional=2_700,
            max_spread_bps=20, max_impact_bps=35,
        )

        self.assertEqual(result["reason"], "book_impact")
        self.assertEqual(result["fill_ratio"], 1.0)

    def test_malformed_book_defers_to_caller_fallback(self):
        result = assess_order_book(
            None, is_buy=True, planned_notional=2_700,
            max_spread_bps=20, max_impact_bps=35,
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "book_unavailable")
