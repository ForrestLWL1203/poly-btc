import unittest

from hyper.execution.orders import (
    ClientOrderId,
    MarketSpec,
    OrderIntent,
    OrderOutcome,
    OrderValidationError,
    deterministic_cloid,
    normalize_action_response,
    normalize_order_response,
    prepare_ioc_order,
    quantize_perp_price,
)


class ExecutionOrderTests(unittest.TestCase):
    def setUp(self):
        self.market = MarketSpec("BTC", "", 0, 5, 50)

    def intent(self, **changes):
        values = {
            "coin": "BTC",
            "is_buy": True,
            "size": 0.001239,
            "limit_px": 64_321.987,
            "reduce_only": False,
            "cloid": deterministic_cloid("session", "fill", "BTC", "open"),
        }
        values.update(changes)
        return OrderIntent(**values)

    def test_deterministic_cloid_is_stable_valid_and_domain_sensitive(self):
        first = deterministic_cloid("session", 1, "BTC", "open")
        second = deterministic_cloid("session", 1, "BTC", "open")
        different = deterministic_cloid("session", 1, "BTC", "close")

        self.assertEqual(first, second)
        self.assertNotEqual(first, different)
        self.assertEqual(len(first), 34)
        self.assertEqual(ClientOrderId(first).to_raw(), first)

    def test_prepare_ioc_rounds_size_down_and_buy_price_toward_safe_side(self):
        order = prepare_ioc_order(self.intent(), self.market)

        self.assertEqual(order.size, 0.00123)
        self.assertEqual(order.limit_px, 64_321.0)
        self.assertGreater(order.notional, 10)

    def test_sell_price_rounds_up_not_more_aggressive(self):
        value = quantize_perp_price(123.4567, 3, is_buy=False)

        self.assertEqual(float(value), 123.46)

    def test_integer_price_is_allowed_without_five_digit_truncation(self):
        value = quantize_perp_price(123456, 2, is_buy=True)

        self.assertEqual(float(value), 123456.0)

    def test_order_below_exchange_minimum_is_rejected_before_transport(self):
        with self.assertRaisesRegex(OrderValidationError, "min_trade_notional"):
            prepare_ioc_order(self.intent(size=0.0001, limit_px=50_000), self.market)

    def test_invalid_cloid_is_rejected(self):
        with self.assertRaisesRegex(OrderValidationError, "invalid_cloid"):
            prepare_ioc_order(self.intent(cloid="0x1234"), self.market)

    def test_full_and_partial_fills_are_normalized(self):
        full = normalize_order_response({
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [
                {"filled": {"totalSz": "1", "avgPx": "100", "oid": 7}},
            ]}},
        }, requested_size=1)
        partial = normalize_order_response({
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [
                {"filled": {"totalSz": "0.4", "avgPx": "100", "oid": 8}},
            ]}},
        }, requested_size=1)

        self.assertEqual(full.outcome, OrderOutcome.FILLED)
        self.assertEqual(partial.outcome, OrderOutcome.PARTIAL)
        self.assertEqual(partial.filled_size, 0.4)

    def test_ioc_cancel_is_not_misreported_as_fill(self):
        result = normalize_order_response({
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [
                {"error": "IocCancel: Order could not immediately match"},
            ]}},
        }, requested_size=1)

        self.assertEqual(result.outcome, OrderOutcome.CANCELED)
        self.assertEqual(result.error_code, "ioc_cancel")

    def test_cancel_nested_error_is_not_misreported_as_success(self):
        failed = normalize_action_response({
            "status": "ok",
            "response": {"type": "cancel", "data": {"statuses": [
                {"error": "Order was never placed, already canceled, or filled."},
            ]}},
        })
        succeeded = normalize_action_response({
            "status": "ok",
            "response": {"type": "cancel", "data": {"statuses": ["success"]}},
        })

        self.assertFalse(failed.ok)
        self.assertEqual(failed.error_code, "missing_order")
        self.assertTrue(succeeded.ok)

    def test_malformed_filled_size_fails_closed(self):
        result = normalize_order_response({
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [
                {"filled": {"totalSz": "not-a-number", "avgPx": "100", "oid": 7}},
            ]}},
        }, requested_size=1)

        self.assertEqual(result.outcome, OrderOutcome.UNKNOWN)
        self.assertEqual(result.error_code, "invalid_fill_status")

    def test_documented_exchange_rejections_are_normalized(self):
        cases = {
            "Insufficient margin to place order.": "insufficient_margin",
            "Order would increase open interest while open interest is capped": "open_interest_cap",
            "Price must be divisible by tick size.": "invalid_tick",
            "No liquidity available for market order.": "no_liquidity",
            "Order could not immediately match against any resting orders. asset=3": "ioc_cancel",
            "Order price too far from oracle": "oracle_reject",
            "Order would cause position to exceed max position": "max_position",
            "Reduce only order would increase position.": "reduce_only",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                result = normalize_order_response({
                    "status": "ok",
                    "response": {"type": "order", "data": {"statuses": [{"error": message}]}},
                }, requested_size=1)
                expected_outcome = OrderOutcome.CANCELED if expected == "ioc_cancel" else OrderOutcome.REJECTED
                self.assertEqual(result.outcome, expected_outcome)
                self.assertEqual(result.error_code, expected)


if __name__ == "__main__":
    unittest.main()
