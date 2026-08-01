"""Process-local serialization and fail-closed CLOID recovery for signed execution actions.

Hyperliquid accepts multiple orders with the same CLOID, so CLOID is a reconciliation key, not an exchange-
side idempotency key.  This coordinator prevents same-process duplicates and refuses to resubmit a CLOID that
already exists after restart or an ambiguous transport failure.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable, Dict, Optional

from .hyperliquid_broker import BrokerError, HyperliquidBroker
from .orders import OrderIntent, SubmitResult


class SigningClockError(BrokerError):
    pass


class AmbiguousOrderError(BrokerError):
    pass


@dataclass(frozen=True)
class CoordinatedSubmit:
    submitted: bool
    result: Optional[SubmitResult] = None
    recovered_status: Optional[Any] = None


def order_status_exists(status: Any) -> bool:
    return isinstance(status, dict) and status.get("status") == "order" and isinstance(status.get("order"), dict)


class SerializedExecutionCoordinator:
    def __init__(
        self,
        broker: HyperliquidBroker,
        *,
        clock_ms: Optional[Callable[[], int]] = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_clock_regression_ms: int = 5_000,
    ):
        self.broker = broker
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._sleep = sleeper
        self._max_clock_regression_ms = int(max_clock_regression_ms)
        self._lock = threading.Lock()
        self._last_signing_ms = 0
        self._orders: Dict[str, CoordinatedSubmit] = {}

    def _next_signing_slot(self) -> int:
        now = int(self._clock_ms())
        if self._last_signing_ms and now < self._last_signing_ms - self._max_clock_regression_ms:
            raise SigningClockError("signing_clock_regressed")
        if now <= self._last_signing_ms:
            self._sleep((self._last_signing_ms + 1 - now) / 1000.0)
            now = int(self._clock_ms())
        if now <= self._last_signing_ms:
            raise SigningClockError("signing_clock_not_advancing")
        self._last_signing_ms = now
        return now

    def run_signed(self, action: Callable[[], Any]) -> Any:
        with self._lock:
            self._next_signing_slot()
            return action()

    def submit_once(self, intent: OrderIntent) -> CoordinatedSubmit:
        cloid = str(intent.cloid).lower()
        with self._lock:
            cached = self._orders.get(cloid)
            if cached is not None:
                return cached

            existing = self.broker.order_status(cloid)
            if order_status_exists(existing):
                recovered = CoordinatedSubmit(submitted=False, recovered_status=existing)
                self._orders[cloid] = recovered
                return recovered

            self._next_signing_slot()
            try:
                result = self.broker.submit_ioc(intent)
            except BrokerError:
                recovered_status = self.broker.order_status(cloid)
                if order_status_exists(recovered_status):
                    recovered = CoordinatedSubmit(submitted=False, recovered_status=recovered_status)
                    self._orders[cloid] = recovered
                    return recovered
                raise AmbiguousOrderError("order_transport_failed_status_unknown") from None
            submitted = CoordinatedSubmit(submitted=True, result=result)
            self._orders[cloid] = submitted
            return submitted
