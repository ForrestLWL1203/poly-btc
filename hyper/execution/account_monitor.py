"""Mainnet self-account WebSocket monitor with REST-baseline recovery.

This stream is intentionally separate from the Observer's public BBO stream.
It owns only exchange facts (orders, fills and replaceable account snapshots);
the ordered Observer signal worker remains the sole owner of copy-trade ledger
mutation.
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from typing import Any

import websockets

from hyper import config
from hyper.market.rate_usage import USAGE


def _subscription_key(subscription: dict) -> str:
    kind = str(subscription.get("type") or "")
    if kind == "openOrders":
        return f"openOrders:{str(subscription.get('dex') or '')}"
    return kind


class LiveAccountMonitor:
    """One-user WS stream, ordered persistence queue and WS info RPC."""

    def __init__(self, executor, *, ws_url: str | None = None, mode: str | None = None):
        self.executor = executor
        self.address = str(executor.broker.account_address).lower()
        self.supported_dexes = tuple(executor.broker.supported_dexes)
        self.ws_url = str(ws_url or executor.broker.venue.ws_url)
        self.mode = str(mode or config.LIVE_ACCOUNT_MONITOR_MODE or "rest_only").lower()
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=int(config.ACCOUNT_WS_QUEUE_MAX))
        self.state = "connecting" if self.mode == "ws_primary" else "rest_fallback"
        self.connected_at = None
        self.healthy_since = None
        self.last_message_at = None
        self.last_pong_at = None
        self.last_order_update_at = None
        self.last_fill_at = None
        self.last_position_at = None
        self.last_rest_audit_at = None
        self.last_fallback_at = None
        self.reconnect_count = 0
        self.fallback_count = 0
        self.last_error = None
        self.queue_lag_ms = 0
        self._conn = None
        self._loop = None
        self._stop = False
        self._ready = asyncio.Event()
        self._initial_done = asyncio.Event()
        self._initial_result = None
        self._pending_posts: dict[int, asyncio.Future] = {}
        self._post_id = 0
        self._last_connection_healthy_s = 0.0
        self._last_position_recovery_fill_at = 0.0
        self._last_open_orders_at = 0.0
        self._last_spot_at = 0.0
        self._consumer_busy = False
        self._lag_recovery_until_drained = False
        self._meta_lock = threading.RLock()
        self._subscriptions = self._build_subscriptions()
        self._required_acks = {_subscription_key(item) for item in self._subscriptions}
        self._required_snapshots = {
            "userFills", "allDexsClearinghouseState", "spotState",
            *(f"openOrders:{dex}" for dex in self.supported_dexes),
        }
        self.executor.attach_account_monitor(self)

    def _build_subscriptions(self) -> list[dict]:
        rows = [
            {"type": "orderUpdates", "user": self.address},
            {"type": "userFills", "user": self.address, "aggregateByTime": False},
            {"type": "allDexsClearinghouseState", "user": self.address},
            {"type": "spotState", "user": self.address},
            {"type": "userNonFundingLedgerUpdates", "user": self.address},
        ]
        rows.extend(
            {"type": "openOrders", "user": self.address, "dex": dex}
            for dex in self.supported_dexes
        )
        return rows

    def _set_state(self, state: str, *, error: str | None = None) -> None:
        with self._meta_lock:
            prior = self.state
            self.state = str(state)
            if error:
                self.last_error = str(error)[:160]
            elif state == "healthy":
                self.last_error = None
            if state == "healthy" and prior != "healthy":
                self.healthy_since = time.time()
            elif state != "healthy":
                self.healthy_since = None
            if state == "rest_fallback" and prior != "rest_fallback":
                self.fallback_count += 1
                self.last_fallback_at = time.time()

    def is_healthy(self) -> bool:
        with self._meta_lock:
            last = float(self.last_message_at or 0.0)
            return bool(
                self.mode == "ws_primary"
                and self.state == "healthy"
                and last
                and time.time() - last <= float(config.ACCOUNT_WS_STALE_S)
            )

    def acceleration_eligible(self) -> bool:
        with self._meta_lock:
            return bool(
                self.is_healthy()
                and self.healthy_since
                and time.time() - float(self.healthy_since) >= float(config.SCANNER_WS_HEALTHY_MIN_S)
                and self.last_rest_audit_at
                and time.time() - float(self.last_rest_audit_at) <= (
                    float(config.ACCOUNT_REST_AUDIT_INTERVAL_S) + 30.0
                )
                and self.queue_lag_ms < 1000
            )

    async def wait_initial_sync(self, timeout: float = 25.0):
        if self.mode != "ws_primary":
            return None
        try:
            await asyncio.wait_for(self._initial_done.wait(), timeout=float(timeout))
        except asyncio.TimeoutError:
            return None
        return self._initial_result

    def note_rest_audit(self, result: dict | None = None) -> None:
        with self._meta_lock:
            self.last_rest_audit_at = time.time()
        if result is not None and not result.get("ok"):
            self._set_state("reconcile_required", error="rest_audit_drift")

    def restore_after_rest(self, result: dict | None) -> None:
        self.note_rest_audit(result)
        with self._meta_lock:
            if self.state == "reconcile_required":
                return
            connected = self._conn is not None
            recent = bool(
                self.last_message_at
                and time.time() - float(self.last_message_at) <= float(config.ACCOUNT_WS_STALE_S)
            )
            replaying = self._consumer_busy or not self.queue.empty()
        if result and result.get("ok") and connected and recent and self._ready.is_set():
            # Messages received while the authoritative REST pull was in flight
            # are a recovery replay, not evidence that the live queue is still
            # falling behind.  Let the ordered consumer drain that bounded
            # backlog before queue-lag alarms become eligible again.
            with self._meta_lock:
                self._lag_recovery_until_drained = bool(replaying)
            self._set_state("healthy")

    def mark_degraded(self, error: str) -> None:
        if self.state != "reconcile_required":
            self._set_state("degraded", error=error)

    def mark_reconcile_required(self, error: str) -> None:
        self._set_state("reconcile_required", error=error)

    def snapshot(self) -> dict:
        unmatched = self.executor.unmatched_ws_fill_count()
        pending = self.executor.pending_confirmation_count()
        pending_age = self.executor.oldest_pending_confirmation_age_s()
        with self._meta_lock:
            now = time.time()
            alerts = []
            if self.mode == "ws_primary" and self.state != "healthy" and self.last_message_at \
                    and now - float(self.last_message_at) > 60.0:
                alerts.append("account_ws_disconnected_over_60s")
            if self.state == "rest_fallback" and self.last_fallback_at \
                    and now - float(self.last_fallback_at) > 300.0:
                alerts.append("account_rest_fallback_over_5m")
            if unmatched:
                alerts.append("unmatched_account_fill")
            if pending and pending_age > 5.0:
                alerts.append("order_confirmation_over_5s")
            if self.state == "reconcile_required":
                alerts.append("account_reconcile_required")
            if self.last_rest_audit_at and now - float(self.last_rest_audit_at) > (
                float(config.ACCOUNT_REST_AUDIT_INTERVAL_S) + 30.0
            ):
                alerts.append("account_rest_audit_overdue")
            return {
                "mode": self.mode,
                "state": self.state,
                "connectedAt": self.connected_at,
                "healthySince": self.healthy_since,
                "lastMessageAt": self.last_message_at,
                "lastPongAt": self.last_pong_at,
                "lastOrderUpdateAt": self.last_order_update_at,
                "lastFillAt": self.last_fill_at,
                "lastPositionAt": self.last_position_at,
                "lastRestAuditAt": self.last_rest_audit_at,
                "lastFallbackAt": self.last_fallback_at,
                "queueDepth": self.queue.qsize(),
                "queueLagMs": int(self.queue_lag_ms),
                "reconnectCount": int(self.reconnect_count),
                "fallbackCount": int(self.fallback_count),
                "unmatchedFillCount": int(unmatched),
                "pendingConfirmationCount": int(pending),
                "oldestPendingConfirmationAgeSec": round(float(pending_age), 3),
                "lastError": self.last_error,
                "accelerationEligible": self.acceleration_eligible(),
                "alerts": alerts,
            }

    async def stop(self) -> None:
        self._stop = True
        conn = self._conn
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.mode != "ws_primary":
            self._set_state("rest_fallback")
            while not self._stop:
                await asyncio.sleep(1)
            return
        consumer = asyncio.create_task(self._consume(), name="observer:account_ws_consumer")
        backoff = 1.0
        try:
            while not self._stop:
                try:
                    await self._connect_once()
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reconnect is the recovery boundary
                    self._ready.clear()
                    self.reconnect_count += 1
                    if self._last_connection_healthy_s >= 300.0:
                        backoff = 1.0
                    self._set_state(
                        "rest_fallback", error=f"{type(exc).__name__}:{str(exc)[:100]}",
                    )
                    if self._stop:
                        break
                    await asyncio.sleep(backoff + random.random() * min(1.0, backoff / 4.0))
                    backoff = min(15.0, backoff * 2.0)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            for future in list(self._pending_posts.values()):
                if not future.done():
                    future.cancel()
            self._pending_posts.clear()

    async def _connect_once(self) -> None:
        self._set_state("connecting")
        self._ready.clear()
        acks = set()
        snapshots = set()
        sync_task = None
        async with websockets.connect(
            self.ws_url,
            ping_interval=None,
            max_size=None,
            open_timeout=float(config.ACCOUNT_WS_ACK_TIMEOUT_S),
        ) as conn:
            self._conn = conn
            now = time.time()
            with self._meta_lock:
                self.connected_at = now
                self.last_message_at = now
                self.last_error = None
            self._set_state("syncing")
            for subscription in self._subscriptions:
                await conn.send(json.dumps({"method": "subscribe", "subscription": subscription}))
            ping_task = asyncio.create_task(self._ping_loop(conn), name="observer:account_ws_ping")
            watchdog = asyncio.create_task(self._watchdog(conn), name="observer:account_ws_watchdog")
            ack_deadline = time.monotonic() + float(config.ACCOUNT_WS_ACK_TIMEOUT_S)
            try:
                async for raw in conn:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        continue
                    with self._meta_lock:
                        self.last_message_at = time.time()
                    channel = str(message.get("channel") or "")
                    if channel == "pong":
                        with self._meta_lock:
                            self.last_pong_at = time.time()
                        continue
                    if channel == "post":
                        self._resolve_post(message)
                        continue
                    if channel == "subscriptionResponse":
                        data = message.get("data") or {}
                        subscription = data.get("subscription") if isinstance(data, dict) else None
                        if isinstance(subscription, dict):
                            acks.add(_subscription_key(subscription))
                        continue
                    key = self._snapshot_key(channel, message.get("data"))
                    if key:
                        snapshots.add(key)
                    try:
                        self.queue.put_nowait((time.monotonic(), message))
                    except asyncio.QueueFull:
                        self._set_state("rest_fallback", error="account_ws_queue_overflow")
                        # Preserve the event at the overflow boundary.  The
                        # consumer keeps draining the ordered queue and the
                        # subsequent REST gap fill proves the complete range.
                        await self.queue.put((time.monotonic(), message))
                        await conn.close(code=1011, reason="account queue overflow")
                        raise RuntimeError("account_ws_queue_overflow") from None
                    if (
                        sync_task is None
                        and self._required_acks.issubset(acks)
                        and self._required_snapshots.issubset(snapshots)
                    ):
                        sync_task = asyncio.create_task(
                            self._synchronize(), name="observer:account_ws_sync",
                        )
                    if sync_task is None and time.monotonic() > ack_deadline:
                        raise RuntimeError("account_ws_initial_snapshot_timeout")
            finally:
                healthy_since = self.healthy_since
                self._last_connection_healthy_s = (
                    max(0.0, time.time() - float(healthy_since)) if healthy_since else 0.0
                )
                ping_task.cancel()
                watchdog.cancel()
                if sync_task is not None:
                    sync_task.cancel()
                await asyncio.gather(
                    ping_task, watchdog, *(tuple([sync_task]) if sync_task else ()),
                    return_exceptions=True,
                )
                self._conn = None
                self._ready.clear()
        if not self._stop:
            raise RuntimeError("account_ws_disconnected")

    @staticmethod
    def _snapshot_key(channel: str, data: Any) -> str | None:
        if channel == "openOrders" and isinstance(data, dict):
            return f"openOrders:{str(data.get('dex') or '')}"
        if channel in {"userFills", "allDexsClearinghouseState", "spotState"}:
            return channel
        return None

    async def _synchronize(self) -> None:
        try:
            result = await asyncio.to_thread(
                self.executor.reconcile, usage_category="account_audit",
            )
            self.note_rest_audit(result)
            if not result.get("ok"):
                self._set_state("reconcile_required", error="startup_rest_baseline_drift")
                # Keep consuming exchange facts while exposure is frozen so an
                # operator/REST reconciliation can resolve the exact drift.
                self._ready.set()
                await self.queue.join()
                self._initial_result = result
                self._initial_done.set()
                return
            self._ready.set()
            await self.queue.join()
            if self.state == "reconcile_required":
                failed = dict(result)
                failed.update(ok=False, status="reconcile_required", wsEventInvalid=True)
                self._initial_result = failed
                self._initial_done.set()
                return
            self._set_state("healthy")
            self._initial_result = result
            self._initial_done.set()
        except Exception as exc:  # noqa: BLE001 - REST fallback loop owns retry
            self._set_state("rest_fallback", error=f"baseline:{type(exc).__name__}")
            self._initial_result = None
            self._initial_done.set()
            conn = self._conn
            if conn is not None:
                await conn.close(code=1011, reason="account baseline failed")

    async def _consume(self) -> None:
        while not self._stop:
            received, message = await self.queue.get()
            with self._meta_lock:
                self._consumer_busy = True
            try:
                await self._ready.wait()
                lag_ms = max(0.0, (time.monotonic() - float(received)) * 1000.0)
                with self._meta_lock:
                    self.queue_lag_ms = lag_ms
                await asyncio.to_thread(self.executor.apply_ws_message, message)
                channel = str(message.get("channel") or "")
                now = time.time()
                with self._meta_lock:
                    if channel == "orderUpdates":
                        self.last_order_update_at = now
                    elif channel == "userFills":
                        self.last_fill_at = now
                    elif channel == "allDexsClearinghouseState":
                        self.last_position_at = now
                    elif channel == "openOrders":
                        self._last_open_orders_at = now
                    elif channel == "spotState":
                        self._last_spot_at = now
                    lag_alarm_suppressed = self._lag_recovery_until_drained
                    healthy = self.state == "healthy"
                # A completed isolated event is no longer a queue backlog.  In
                # production SQLite writer contention can make that one event
                # take >2s; escalating after it has committed caused a REST
                # audit loop and 429s even though the queue was already empty.
                if lag_ms > 2000 and healthy and not lag_alarm_suppressed \
                        and not self.queue.empty():
                    self.mark_degraded("account_ws_queue_lag")
            except Exception as exc:  # noqa: BLE001 - a corrupt account event is fail closed
                self._set_state(
                    "reconcile_required", error=f"event:{type(exc).__name__}:{str(exc)[:80]}",
                )
                self.executor.freeze_from_monitor("ACCOUNT_WS_EVENT_INVALID")
            finally:
                self.queue.task_done()
                with self._meta_lock:
                    self._consumer_busy = False
                    if self.queue.empty():
                        self.queue_lag_ms = 0
                        self._lag_recovery_until_drained = False

    async def _ping_loop(self, conn) -> None:
        while not self._stop:
            await asyncio.sleep(float(config.ACCOUNT_WS_PING_INTERVAL_S))
            await conn.send(json.dumps({"method": "ping"}))

    async def _watchdog(self, conn) -> None:
        while not self._stop:
            await asyncio.sleep(5)
            with self._meta_lock:
                last = float(self.last_message_at or 0.0)
                connected_at = float(self.connected_at or 0.0)
                state = self.state
                last_position = float(self.last_position_at or 0.0)
                last_fill = float(self.last_fill_at or 0.0)
                last_open_orders = float(self._last_open_orders_at or 0.0)
                last_spot = float(self._last_spot_at or 0.0)
            if state == "syncing" and connected_at and (
                time.time() - connected_at > float(config.ACCOUNT_WS_ACK_TIMEOUT_S)
            ):
                await conn.close(code=1011, reason="account initial sync timeout")
                return
            if last and time.time() - last > float(config.ACCOUNT_WS_STALE_S):
                await conn.close(code=1011, reason="account stream stale")
                return
            now = time.time()
            position_stale = bool(
                state == "healthy" and (
                    (last_position and now - last_position > float(config.ACCOUNT_WS_STATE_STALE_S))
                    or (last_spot and now - last_spot > float(config.ACCOUNT_WS_STATE_STALE_S))
                )
            )
            fill_position_timeout = bool(
                state == "healthy" and last_fill > last_position
                and now - last_fill >= float(config.ACCOUNT_WS_POSITION_WAIT_S)
                and last_fill > self._last_position_recovery_fill_at
            )
            unmatched_fill_timeout = bool(
                state == "healthy" and last_fill
                and now - last_fill >= float(config.ACCOUNT_WS_ORDER_WAIT_S)
                and self.executor.unmatched_ws_fill_count()
            )
            unmatched_order_timeout = bool(
                state == "healthy" and last_open_orders
                and now - last_open_orders >= float(config.ACCOUNT_WS_ORDER_WAIT_S)
                and self.executor.unmatched_ws_open_order_count()
            )
            if unmatched_fill_timeout or unmatched_order_timeout:
                error = "unmatched_account_fill" if unmatched_fill_timeout else "unknown_open_order"
                code = "UNMATCHED_ACCOUNT_FILL" if unmatched_fill_timeout else "UNKNOWN_EXCHANGE_ORDER"
                self.mark_reconcile_required(error)
                self.executor.freeze_from_monitor(code)
                return
            if position_stale or fill_position_timeout:
                if fill_position_timeout:
                    self._last_position_recovery_fill_at = last_fill
                self.mark_degraded(
                    "account_position_after_fill_timeout" if fill_position_timeout
                    else "account_state_snapshot_stale"
                )
                try:
                    result = await asyncio.to_thread(
                        self.executor.reconcile, usage_category="account_fallback",
                    )
                except Exception as exc:  # noqa: BLE001 - reconnect enters REST fallback
                    self._set_state("rest_fallback", error=f"state_pull:{type(exc).__name__}")
                    await conn.close(code=1011, reason="account state pull failed")
                    return
                self.note_rest_audit(result)
                if not result.get("ok"):
                    self.executor.freeze_from_monitor("ACCOUNT_STATE_DRIFT")
                    return
                with self._meta_lock:
                    # The active REST pull is authoritative for freshness until
                    # the next streaming clearinghouse snapshot arrives.
                    self.last_position_at = time.time()
                    self._last_spot_at = time.time()
                self.restore_after_rest(result)

    def _resolve_post(self, message: dict) -> None:
        data = message.get("data") or {}
        try:
            request_id = int(data.get("id"))
        except (TypeError, ValueError):
            return
        future = self._pending_posts.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(data.get("response"))

    async def _post_info(self, payload: dict, *, weight: int) -> Any:
        conn = self._conn
        if conn is None or not self.is_healthy():
            return None
        self._post_id += 1
        request_id = self._post_id
        future = asyncio.get_running_loop().create_future()
        self._pending_posts[request_id] = future
        USAGE.record(category="order_recovery", weight=weight, requests=1, transport="ws")
        try:
            await conn.send(json.dumps({
                "method": "post",
                "id": request_id,
                "request": {"type": "info", "payload": payload},
            }))
        except Exception:  # noqa: BLE001 - caller proceeds to REST recovery
            self._pending_posts.pop(request_id, None)
            return None
        try:
            return await asyncio.wait_for(
                future, timeout=float(config.ACCOUNT_WS_POST_TIMEOUT_S),
            )
        except asyncio.TimeoutError:
            self._pending_posts.pop(request_id, None)
            return None

    @staticmethod
    def _post_payload(response: Any) -> Any:
        if not isinstance(response, dict) or response.get("type") == "error":
            return None
        payload = response.get("payload")
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    async def _recover_order(self, cloid: str, start_ms: int) -> dict:
        status_response = await self._post_info({
            "type": "orderStatus", "user": self.address, "oid": str(cloid),
        }, weight=2)
        fills_response = await self._post_info({
            "type": "userFillsByTime", "user": self.address,
            "startTime": max(0, int(start_ms) - 2000), "aggregateByTime": False,
        }, weight=20)
        return {
            "status": self._post_payload(status_response),
            "fills": self._post_payload(fills_response),
        }

    def recover_order(self, cloid: str, start_ms: int) -> dict | None:
        loop = self._loop
        if loop is None or not self.is_healthy():
            return None
        future = asyncio.run_coroutine_threadsafe(
            self._recover_order(str(cloid), int(start_ms)), loop,
        )
        try:
            return future.result(timeout=float(config.ACCOUNT_WS_POST_TIMEOUT_S) * 2 + 2.0)
        except Exception:  # noqa: BLE001 - caller proceeds to REST recovery
            future.cancel()
            return None
