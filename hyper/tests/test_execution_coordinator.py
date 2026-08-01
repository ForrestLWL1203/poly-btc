import threading
import unittest

from hyper.execution.coordinator import (
    AmbiguousOrderError,
    SerializedExecutionCoordinator,
    SigningClockError,
)
from hyper.execution.hyperliquid_broker import BrokerError
from hyper.execution.orders import OrderIntent, OrderOutcome, SubmitResult, deterministic_cloid


class FakeBroker:
    def __init__(self):
        self.submit_calls = 0
        self.statuses = {}
        self.fail_after_record = False
        self.fail_without_record = False

    def order_status(self, cloid):
        return self.statuses.get(str(cloid).lower(), {"status": "unknownOid"})

    def submit_ioc(self, intent):
        self.submit_calls += 1
        if self.fail_without_record:
            raise BrokerError("order_transport_error:TimeoutError")
        status = {"status": "order", "order": {"order": {"cloid": intent.cloid, "oid": 17}}}
        self.statuses[intent.cloid.lower()] = status
        if self.fail_after_record:
            raise BrokerError("order_transport_error:TimeoutError")
        return SubmitResult(OrderOutcome.FILLED, oid=17, filled_size=intent.size, average_px=intent.limit_px)


def intent():
    return OrderIntent(
        "BTC", True, 0.001, 60_000, False,
        deterministic_cloid("coordinator", "session", "source-fill"),
    )


class ExecutionCoordinatorTests(unittest.TestCase):
    def coordinator(self, broker, values=None):
        values = iter(values or range(1_000, 2_000))
        return SerializedExecutionCoordinator(broker, clock_ms=lambda: next(values), sleeper=lambda _s: None)

    def test_same_process_duplicate_is_returned_from_cache_without_second_submit(self):
        broker = FakeBroker()
        coordinator = self.coordinator(broker)

        first = coordinator.submit_once(intent())
        second = coordinator.submit_once(intent())

        self.assertTrue(first.submitted)
        self.assertIs(first, second)
        self.assertEqual(broker.submit_calls, 1)

    def test_restart_recovers_existing_cloid_without_submit(self):
        broker = FakeBroker()
        order = intent()
        broker.statuses[order.cloid] = {"status": "order", "order": {"order": {"oid": 17}}}

        recovered = self.coordinator(broker).submit_once(order)

        self.assertFalse(recovered.submitted)
        self.assertIsNotNone(recovered.recovered_status)
        self.assertEqual(broker.submit_calls, 0)

    def test_timeout_after_exchange_acceptance_recovers_instead_of_resubmitting(self):
        broker = FakeBroker()
        broker.fail_after_record = True

        recovered = self.coordinator(broker).submit_once(intent())

        self.assertFalse(recovered.submitted)
        self.assertEqual(broker.submit_calls, 1)

    def test_timeout_with_unknown_status_fails_closed(self):
        broker = FakeBroker()
        broker.fail_without_record = True

        with self.assertRaisesRegex(AmbiguousOrderError, "status_unknown"):
            self.coordinator(broker).submit_once(intent())
        self.assertEqual(broker.submit_calls, 1)

    def test_concurrent_same_cloid_submits_once(self):
        broker = FakeBroker()
        coordinator = self.coordinator(broker)
        results = []
        threads = [threading.Thread(target=lambda: results.append(coordinator.submit_once(intent()))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 8)
        self.assertEqual(broker.submit_calls, 1)

    def test_non_advancing_or_regressed_clock_fails_closed(self):
        broker = FakeBroker()
        frozen = SerializedExecutionCoordinator(
            broker, clock_ms=lambda: 10, sleeper=lambda _s: None,
        )
        frozen.run_signed(lambda: None)
        with self.assertRaisesRegex(SigningClockError, "not_advancing"):
            frozen.run_signed(lambda: None)

        regressed = self.coordinator(broker, values=[10_000, 1])
        regressed.run_signed(lambda: None)
        with self.assertRaisesRegex(SigningClockError, "regressed"):
            regressed.run_signed(lambda: None)


if __name__ == "__main__":
    unittest.main()
