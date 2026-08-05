"""Durable Mainnet execution adapter used by the Live Observer.

Exchange state is authoritative.  SQLite stores an intent/attempt/fill audit
and a replaceable projection, but never invents a fill from an HTTP success.
Every increase in exposure is gated by the activated session, a single-writer
lease, fresh Mainnet market data, deterministic CLOIDs and Canary limits.
Testnet verification deliberately uses its separate, non-persistent API
scenario runner because Testnet market state has no strategy value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import threading
import time
import uuid
from typing import Any

from hyper import config
from hyper.execution.liquidity import assess_order_book
from hyper.util import now_iso, now_ms

from . import control
from .account_state import snapshot_account_values, snapshot_orders, snapshot_positions
from .coordinator import AmbiguousOrderError, SerializedExecutionCoordinator, SigningClockError
from .credentials import decrypt_agent_wallet
from .hyperliquid_broker import BrokerError, HyperliquidBroker
from .orders import OrderIntent, OrderOutcome, deterministic_cloid, dex_for_coin
from .sdk_clients import create_signed_clients_from_wallet
from .venue import ExecutionNetwork


ACTIVE_SESSION_STATES = {"starting", "live_canary", "live_running", "paused", "draining", "reconcile_required"}
INCREASE_STATES = {"starting", "live_canary", "live_running"}
TERMINAL_INTENT_STATES = {"filled", "partial", "canceled", "rejected"}


@dataclass(frozen=True)
class LiveExecutionResult:
    filled_size: float
    average_px: float | None
    fee: float
    closed_pnl: float
    cloids: tuple[str, ...]
    oids: tuple[int, ...]
    outcome: str
    error_code: str | None = None


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _valid_until(value: str | None) -> bool:
    if not value:
        return True
    try:
        expiry = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return expiry.astimezone(timezone.utc) > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def _iso_ms(value: str | None) -> int:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


class LiveExecutor:
    def __init__(self, db, session: dict, broker: HyperliquidBroker, *, owner: str | None = None):
        self.db = db
        self.session = session
        self.broker = broker
        self.network = broker.venue.network
        if self.network is not ExecutionNetwork.MAINNET:
            raise ValueError("live_executor_requires_mainnet_broker")
        session_network = str(session.get("network") or self.network.value)
        if session_network != self.network.value:
            raise ValueError("execution_session_broker_network_mismatch")
        self.session["network"] = self.network.value
        self.coordinator = SerializedExecutionCoordinator(broker)
        self.owner = owner or f"live-observer:{os.getpid()}:{uuid.uuid4().hex[:10]}"
        self._lock = threading.RLock()
        self.available = 0.0
        self.equity = 0.0
        self._acquire_lease()

    @classmethod
    def from_db(cls, db, *, private_key_path: str | None = None):
        ctl = db.execute(
            "SELECT selected_mode,state,active_session_id FROM execution_control WHERE id=1"
        ).fetchone()
        if not ctl or ctl[0] != "live" or not ctl[2] or ctl[1] not in {
            "live_canary", "live_running", "paused", "draining", "reconcile_required",
        }:
            raise RuntimeError("live_session_not_activated")
        row = db.execute(
            "SELECT session_id,network,state,account_address,agent_address,strategy_revision,sizing_anchor,"
            "margin_equity_pct,sizing_equity,canary,canary_margin_cap,started_at FROM execution_session "
            "WHERE session_id=?",
            (ctl[2],),
        ).fetchone()
        if not row or row[2] not in ACTIVE_SESSION_STATES or row[1] != ExecutionNetwork.MAINNET.value:
            raise RuntimeError("live_session_invalid")
        session = {
            "session_id": row[0], "network": row[1], "state": row[2], "account_address": row[3],
            "agent_address": row[4], "strategy_revision": row[5], "sizing_anchor": row[6],
            "margin_equity_pct": row[7], "sizing_equity": row[8], "canary": bool(row[9]),
            "canary_margin_cap": _float(row[10]), "started_at": row[11],
        }
        credential = control.credential_row(db, "mainnet", include_envelope=True)
        if not credential or credential.get("status") != "verified":
            raise RuntimeError("mainnet_credential_not_verified")
        if credential["account_address"] != session["account_address"] \
                or credential["agent_address"] != session["agent_address"]:
            raise RuntimeError("live_session_credential_mismatch")
        if not _valid_until(credential.get("valid_until")):
            control.mark_credential_status(db, "mainnet", status="expired", error_code="AGENT_EXPIRED")
            control.set_control_state(db, "credential_error", error_code="AGENT_EXPIRED")
            db.commit()
            raise RuntimeError("mainnet_agent_expired")
        session["valid_until"] = credential.get("valid_until")
        key_path = private_key_path or os.environ.get("HL_CREDENTIAL_PRIVATE_KEY_FILE")
        if not key_path and os.path.isfile("secret/credential-wrap-private.pem"):
            key_path = "secret/credential-wrap-private.pem"
        if not key_path:
            raise RuntimeError("live_credential_worker_not_provisioned")
        wallet = decrypt_agent_wallet(
            credential["envelope"], network="mainnet",
            account_address=session["account_address"], agent_address=session["agent_address"],
            private_key_path=key_path,
        )
        clients = create_signed_clients_from_wallet(
            ExecutionNetwork.MAINNET, session["account_address"], session["agent_address"], wallet,
            supported_dexes=("", "xyz"), timeout=10.0, allow_mainnet=True,
        )
        broker = HyperliquidBroker(
            ExecutionNetwork.MAINNET, session["account_address"], info_client=clients.info,
            exchange_client=clients.exchange, supported_dexes=("", "xyz"), allow_mainnet_signing=True,
        )
        return cls(db, session, broker)

    def _acquire_lease(self) -> None:
        stamp = now_ms()
        expires = stamp + 45_000
        self.db.execute("BEGIN IMMEDIATE")
        row = self.db.execute("SELECT owner,expires_at_ms FROM execution_lease WHERE id=1").fetchone()
        if (row and row[0] != self.owner and int(row[1] or 0) > stamp
                and self._lease_owner_process_alive(row[0])):
            self.db.rollback()
            raise RuntimeError("live_execution_lease_held")
        self.db.execute(
            "INSERT INTO execution_lease (id,owner,acquired_at,heartbeat_at,expires_at_ms) VALUES (1,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,acquired_at=excluded.acquired_at,"
            "heartbeat_at=excluded.heartbeat_at,expires_at_ms=excluded.expires_at_ms",
            (self.owner, now_iso(), now_iso(), expires),
        )
        self.db.commit()

    @staticmethod
    def _lease_owner_process_alive(owner: str | None) -> bool:
        """Fail closed for unknown owners; reclaim our own lease immediately after a dead worker.

        systemd terminates a Python worker with SIGTERM during deployment, so its normal async cleanup
        may not run.  The lease owner embeds that worker's local PID.  Waiting for the full lease TTL
        would leave real positions unmanaged and make systemd report several false crash loops.  A live
        PID still owns the lease; an absent PID is safe for the replacement worker to take over.
        """
        parts = str(owner or "").split(":")
        if len(parts) != 3 or parts[0] != "live-observer":
            return True
        try:
            pid = int(parts[1])
            if pid <= 0:
                return True
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, ValueError):
            return True

    def heartbeat_lease(self) -> None:
        updated = self.db.execute(
            "UPDATE execution_lease SET heartbeat_at=?,expires_at_ms=? WHERE id=1 AND owner=?",
            (now_iso(), now_ms() + 45_000, self.owner),
        ).rowcount
        if updated != 1:
            self.db.rollback()
            raise RuntimeError("live_execution_lease_lost")
        self.db.commit()

    def release_lease(self) -> None:
        self.db.execute("DELETE FROM execution_lease WHERE id=1 AND owner=?", (self.owner,))
        self.db.commit()

    def rollback_after_error(self) -> None:
        """Restore the dedicated execution connection after an interrupted operation."""
        with self._lock:
            try:
                self.db.rollback()
            except Exception:  # noqa: BLE001 - recovery must not hide the original execution error
                pass

    def _freeze_reconcile(self, error_code: str) -> None:
        """Atomically block all exposure increases until exchange truth is clean."""
        control.set_control_state(self.db, "reconcile_required", error_code=error_code)
        self.db.execute(
            "UPDATE execution_session SET state='reconcile_required',updated_at=? WHERE session_id=?",
            (now_iso(), self.session["session_id"]),
        )
        self.session["state"] = "reconcile_required"

    @staticmethod
    def _account_values(snapshot, positions: list[dict]) -> tuple[float, float]:
        """Unified total equity and conservative available USDC.

        Hyperliquid exposes the unified total-equity balance in spotClearinghouseState; isolated position
        margin must still be removed from that total to derive available-to-trade collateral. Spot holds are
        also unavailable and are deducted here.
        """
        return snapshot_account_values(snapshot, positions)

    @staticmethod
    def _positions(snapshot) -> list[dict]:
        return snapshot_positions(snapshot)

    @staticmethod
    def _orders(snapshot) -> list[dict]:
        return snapshot_orders(snapshot)

    def _sync_fills(self) -> list[dict]:
        session_start_ms = _iso_ms(self.session.get("started_at"))
        last_row = self.db.execute(
            "SELECT MAX(fill_time_ms) FROM execution_fill WHERE network=? AND session_id=?",
            (self.network.value, self.session["session_id"]),
        ).fetchone()
        last_fill_ms = int(last_row[0] or 0) if last_row else 0
        start_ms = max(session_start_ms, max(0, last_fill_ms - 1))
        # A long-running session must not lose fills when the recent-fills
        # window rolls over.  Keep recent_fills only as an adapter fallback.
        if start_ms and hasattr(self.broker, "fills_by_time"):
            fills = self.broker.fills_by_time(start_ms)
        else:
            fills = self.broker.recent_fills()
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            tid = str(fill.get("tid") or "")
            if not tid:
                continue
            fill_time_ms = int(fill.get("time") or 0)
            if session_start_ms and fill_time_ms < session_start_ms:
                continue
            cloid = str(fill.get("cloid") or "").lower() or None
            self.db.execute(
                "INSERT OR IGNORE INTO execution_fill "
                "(network,tid,session_id,cloid,oid,coin,side,size,px,fee,closed_pnl,fill_time_ms,raw_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.network.value, tid, self.session["session_id"], cloid, fill.get("oid"),
                    str(fill.get("coin") or ""),
                    fill.get("side"), abs(_float(fill.get("sz"))), _float(fill.get("px")),
                    _float(fill.get("fee")), _float(fill.get("closedPnl")), fill_time_ms,
                    json.dumps(fill, sort_keys=True, separators=(",", ":")), now_iso(),
                ),
            )
        return fills

    def _confirmed_fill_totals(self, cloids: tuple[str, ...]) -> tuple[float | None, float | None]:
        if not cloids:
            return None, None
        placeholders = ",".join("?" for _ in cloids)
        row = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(fee),0),COALESCE(SUM(closed_pnl),0) FROM execution_fill "
            f"WHERE session_id=? AND lower(cloid) IN ({placeholders})",
            (self.session["session_id"], *(str(item).lower() for item in cloids)),
        ).fetchone()
        return (float(row[1]), float(row[2])) if row and row[0] else (None, None)

    @staticmethod
    def _queried_order_state(status: Any) -> tuple[str | None, int | None]:
        """Normalize the terminal/open portion of an official orderStatus response."""
        if not isinstance(status, dict) or status.get("status") != "order":
            return None, None
        wrapper = status.get("order")
        if not isinstance(wrapper, dict):
            return None, None
        order = wrapper.get("order") if isinstance(wrapper.get("order"), dict) else wrapper
        try:
            oid = int(order.get("oid")) if isinstance(order, dict) and order.get("oid") is not None else None
        except (TypeError, ValueError):
            oid = None
        raw = str(wrapper.get("status") or (order.get("status") if isinstance(order, dict) else "") or "").lower()
        if raw in {"open", "triggered"}:
            return "resting", oid
        if raw == "filled":
            return "filled", oid
        if raw and raw not in {"unknown", "unknownoid"}:
            # IOC terminal statuses such as canceled/marginCanceled are final
            # and therefore safe not to resubmit.
            return "canceled", oid
        return None, oid

    def _reconcile_pending_intents(self) -> None:
        """Resolve crash/transport-ambiguous intents from fills and CLOID status.

        This path never resubmits an order. A still-unknown CLOID remains
        ambiguous and keeps exposure increases frozen.
        """
        rows = self.db.execute(
            "SELECT cloid,requested_size,state FROM execution_order_intent WHERE session_id=? "
            "AND state IN ('created','submitting','resting','ambiguous')",
            (self.session["session_id"],),
        ).fetchall()
        for cloid, requested_size, current_state in rows:
            fill = self.db.execute(
                "SELECT COALESCE(SUM(size),0),COALESCE(SUM(size*px),0),MAX(oid) FROM execution_fill "
                "WHERE network=? AND session_id=? AND lower(cloid)=lower(?)",
                (self.network.value, self.session["session_id"], cloid),
            ).fetchone()
            filled = max(0.0, _float(fill[0])) if fill else 0.0
            weighted = _float(fill[1]) if fill else 0.0
            fill_oid = int(fill[2]) if fill and fill[2] is not None else None
            try:
                queried_state, status_oid = self._queried_order_state(self.broker.order_status(cloid))
            except BrokerError:
                queried_state, status_oid = None, None
            if filled > 0:
                state = "filled" if filled + 1e-12 >= _float(requested_size) else "partial"
                self.db.execute(
                    "UPDATE execution_order_intent SET state=?,oid=COALESCE(oid,?),filled_size=?,"
                    "average_px=?,error_code=NULL,updated_at=? WHERE cloid=?",
                    (state, fill_oid or status_oid, filled, weighted / filled, now_iso(), cloid),
                )
            elif queried_state in TERMINAL_INTENT_STATES or queried_state == "resting":
                self.db.execute(
                    "UPDATE execution_order_intent SET state=?,oid=COALESCE(oid,?),"
                    "error_code=CASE WHEN ?='canceled' THEN NULL ELSE error_code END,updated_at=? "
                    "WHERE cloid=?",
                    (queried_state, status_oid, queried_state, now_iso(), cloid),
                )
            elif current_state != "ambiguous":
                # A durable pre-send row with no terminal evidence is
                # indistinguishable from an interrupted submission.
                self.db.execute(
                    "UPDATE execution_order_intent SET state='ambiguous',error_code='exchange_status_unknown',"
                    "updated_at=? WHERE cloid=?",
                    (now_iso(), cloid),
                )

    def reconcile(self) -> dict:
        with self._lock:
            self.heartbeat_lease()
            snapshot = self.broker.account_snapshot()
            positions = self._positions(snapshot)
            orders = self._orders(snapshot)
            self.equity, self.available = self._account_values(snapshot, positions)
            fills = self._sync_fills()
            self._reconcile_pending_intents()

            session_id = self.session["session_id"]
            self.db.execute("DELETE FROM execution_position_projection WHERE session_id=?", (session_id,))
            self.db.executemany(
                "INSERT INTO execution_position_projection "
                "(session_id,dex,coin,signed_size,entry_px,position_value,margin_used,leverage_type,"
                "leverage_value,unrealized_pnl,liquidation_px,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(
                    session_id, p["dex"], p["coin"], p["signed_size"], p["entry_px"], p["position_value"],
                    p["margin_used"], p["leverage_type"], p["leverage_value"], p["unrealized_pnl"],
                    p["liquidation_px"], now_iso(),
                ) for p in positions],
            )
            self.db.execute(
                "INSERT INTO execution_account_snapshot "
                "(session_id,equity,available,margin_used,unrealized_pnl,equity_projection_version,"
                "observed_at) VALUES (?,?,?,?,?,?,?)",
                (
                    session_id, self.equity, self.available,
                    sum(max(0.0, _float(p.get("margin_used"))) for p in positions),
                    sum(_float(p.get("unrealized_pnl")) for p in positions), 2, now_iso(),
                ),
            )
            expected_sizes = {
                str(row[0]): _float(row[1]) for row in self.db.execute(
                    "SELECT coin,COALESCE(SUM(CASE WHEN side='buy' THEN filled_size ELSE -filled_size END),0) "
                    "FROM execution_order_intent WHERE session_id=? GROUP BY coin",
                    (session_id,),
                ).fetchall()
            }
            actual_sizes = {str(item["coin"]): _float(item["signed_size"]) for item in positions}
            managed = set(expected_sizes)
            known_cloids = {
                str(row[0]).lower() for row in self.db.execute(
                    "SELECT cloid FROM execution_order_intent WHERE session_id=? "
                    "AND state IN ('created','submitting','resting','ambiguous')", (session_id,),
                ).fetchall()
            }
            drift_coins = {
                coin for coin in set(expected_sizes) | set(actual_sizes)
                if abs(expected_sizes.get(coin, 0.0) - actual_sizes.get(coin, 0.0)) > 1e-8
            }
            unknown_positions = sorted(
                {p["coin"] for p in positions if p["coin"] not in managed} | drift_coins
            )
            unknown_orders = [o for o in orders if str(o.get("cloid") or "").lower() not in known_cloids]
            ambiguous_intents = int(self.db.execute(
                "SELECT COUNT(*) FROM execution_order_intent WHERE session_id=? AND state='ambiguous'",
                (session_id,),
            ).fetchone()[0])
            status = (
                "ok" if not unknown_positions and not unknown_orders and not ambiguous_intents
                else "reconcile_required"
            )
            digest_payload = {
                "positions": [(p["dex"], p["coin"], p["signed_size"]) for p in positions],
                "orders": [(o.get("coin"), o.get("oid"), o.get("cloid")) for o in orders],
            }
            import hashlib
            exchange_hash = hashlib.sha256(
                json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            self.db.execute(
                "INSERT INTO execution_reconcile_checkpoint "
                "(session_id,status,exchange_hash,position_count,open_order_count,unknown_positions,"
                "unknown_orders,details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    session_id, status, exchange_hash, len(positions), len(orders), len(unknown_positions),
                    len(unknown_orders), json.dumps({
                        "unknownPositionCoins": unknown_positions,
                        "expectedSignedSizes": expected_sizes,
                        "actualSignedSizes": actual_sizes,
                        "ambiguousIntents": ambiguous_intents,
                    }, sort_keys=True),
                    now_iso(),
                ),
            )
            self.db.execute(
                "INSERT INTO live_copy_account "
                "(id,initial_balance,balance,available,equity_projection_version,updated_at) "
                "VALUES (1,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET balance=excluded.balance,available=excluded.available,"
                "equity_projection_version=2,updated_at=excluded.updated_at",
                (self.session["sizing_anchor"], self.equity, self.available, 2, now_iso()),
            )
            if status != "ok":
                self._freeze_reconcile(
                    "AMBIGUOUS_ORDER_STATE" if ambiguous_intents else "UNKNOWN_EXCHANGE_STATE"
                )
            elif self.session["state"] == "reconcile_required":
                # Recovery only reaches operator-paused state. It never
                # silently resumes real-money exposure after ambiguity.
                self.db.execute(
                    "UPDATE execution_session SET state='paused',updated_at=? WHERE session_id=?",
                    (now_iso(), session_id),
                )
                control.set_control_state(self.db, "paused")
                self.session["state"] = "paused"
            elif self.session["state"] == "starting":
                running_state = "live_canary" if self.session["canary"] else "live_running"
                self.db.execute(
                    "UPDATE execution_session SET state=?,updated_at=? WHERE session_id=?",
                    (running_state, now_iso(), session_id),
                )
                control.set_control_state(self.db, running_state)
                self.session["state"] = running_state
            self.db.commit()
            return {
                "ok": status == "ok", "status": status, "equity": self.equity,
                "available": self.available, "positions": positions, "orders": orders,
                "fills": fills, "unknownPositions": len(unknown_positions),
                "unknownOrders": len(unknown_orders), "ambiguousIntents": ambiguous_intents,
            }

    def _refresh_session_state(self) -> str:
        row = self.db.execute(
            "SELECT state,canary,canary_margin_cap,strategy_revision FROM execution_session WHERE session_id=?",
            (self.session["session_id"],),
        ).fetchone()
        if not row:
            raise RuntimeError("live_session_missing")
        self.session.update(state=row[0], canary=bool(row[1]), canary_margin_cap=_float(row[2]))
        if row[3] != self.session["strategy_revision"]:
            raise RuntimeError("live_strategy_revision_changed")
        return row[0]

    def _increase_margin_cap(self, coin: str, planned_margin: float) -> float:
        state = self._refresh_session_state()
        if state not in INCREASE_STATES:
            raise RuntimeError(f"live_increase_blocked:{state}")
        if self.available <= 0:
            raise RuntimeError("NO_AVAILABLE_COLLATERAL")
        valid_until = self.session.get("valid_until")
        if valid_until:
            try:
                expiry = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                raise RuntimeError("AGENT_EXPIRY_INVALID") from None
            if expiry - datetime.now(timezone.utc) < timedelta(hours=24):
                control.set_control_state(self.db, "credential_error", error_code="AGENT_EXPIRING_UNDER_24H")
                self.db.commit()
                raise RuntimeError("AGENT_EXPIRING_UNDER_24H")
        allowed = min(max(0.0, planned_margin), self.available)
        if self.session["canary"]:
            projections = self.db.execute(
                "SELECT coin,COALESCE(margin_used,0) FROM execution_position_projection WHERE session_id=?",
                (self.session["session_id"],),
            ).fetchall()
            active_coins = {str(row[0]) for row in projections if abs(_float(row[1])) > 1e-12}
            used = sum(max(0.0, _float(row[1])) for row in projections)
            if (active_coins and coin not in active_coins) or len(active_coins) > 1:
                raise RuntimeError("live_canary_position_limit")
            allowed = min(allowed, max(0.0, self.session["canary_margin_cap"] - used))
            if allowed <= 0:
                raise RuntimeError("live_canary_margin_limit")
        return allowed

    def _market_tradeable(self, coin: str) -> None:
        spec = self.broker.market_spec(coin)
        contexts = self.broker.market_contexts(spec.dex)
        meta, rows = contexts
        universe = meta.get("universe") if isinstance(meta, dict) else None
        local_index = spec.asset_id if not spec.dex else spec.asset_id % 10_000
        if not isinstance(universe, list) or local_index >= len(universe) or local_index >= len(rows):
            raise RuntimeError("market_context_missing")
        market = universe[local_index]
        context = rows[local_index]
        if not isinstance(market, dict) or not isinstance(context, dict) or market.get("isDelisted"):
            raise RuntimeError("market_not_tradeable")
        if _float(context.get("markPx")) <= 0:
            raise RuntimeError("market_mark_unavailable")

    def _mid(self, coin: str) -> float:
        last_error = None
        for attempt in range(int(config.LIVE_QUOTE_READ_ATTEMPTS)):
            try:
                value = _float(self.broker.all_mids(dex_for_coin(coin)).get(coin))
                if value <= 0:
                    raise RuntimeError("live_mid_unavailable")
                return value
            except (BrokerError, RuntimeError) as exc:
                last_error = exc
                if attempt + 1 < int(config.LIVE_QUOTE_READ_ATTEMPTS):
                    time.sleep(0.05)
        if isinstance(last_error, RuntimeError):
            raise last_error
        raise RuntimeError("live_mid_unavailable") from None

    def _quote_with_retry(self, coin: str, is_buy: bool, notional: float, *, reduce_only: bool) -> float:
        last_error = None
        for attempt in range(int(config.LIVE_QUOTE_READ_ATTEMPTS)):
            try:
                return self._quote(coin, is_buy, notional, reduce_only=reduce_only)
            except (BrokerError, RuntimeError) as exc:
                last_error = exc
                if attempt + 1 < int(config.LIVE_QUOTE_READ_ATTEMPTS):
                    time.sleep(0.05)
        if isinstance(last_error, RuntimeError):
            raise last_error
        raise RuntimeError("live_quote_unavailable") from None

    def _quote(self, coin: str, is_buy: bool, notional: float, *, reduce_only: bool) -> float:
        self._market_tradeable(coin)
        started = time.monotonic()
        book = self.broker.l2_book(coin)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if elapsed_ms > 1_000:
            raise RuntimeError("live_quote_stale")
        exchange_time = int(book.get("time") or 0) if isinstance(book, dict) else 0
        if exchange_time and abs(now_ms() - exchange_time) > 1_000:
            raise RuntimeError("live_quote_stale")
        assessment = assess_order_book(
            book, is_buy=is_buy, planned_notional=notional,
            max_spread_bps=config.LIVE_BOOK_MAX_SPREAD_BPS,
            max_impact_bps=config.LIVE_BOOK_MAX_IMPACT_BPS,
        )
        if not assessment.get("available"):
            raise RuntimeError("live_book_unavailable")
        if not reduce_only and assessment.get("reason"):
            raise RuntimeError(f"live_{assessment['reason']}")
        if reduce_only:
            top = assessment.get("best_ask" if is_buy else "best_bid")
            slippage = max(
                float(config.LIVE_EXIT_SLIPPAGE_BPS), float(config.LIVE_BOOK_MAX_IMPACT_BPS) * 2.0,
            ) / 10_000.0
            return float(top) * (1.0 + slippage if is_buy else 1.0 - slippage)
        worst = assessment.get("worst_px")
        if not worst:
            raise RuntimeError("live_book_depth")
        return float(worst)

    def _existing_intent(self, cloid: str):
        return self.db.execute(
            "SELECT state,oid,filled_size,average_px,error_code FROM execution_order_intent WHERE cloid=?",
            (cloid,),
        ).fetchone()

    def _logical_cloid(
        self, *, coin, action, source_address, source_fill_id, source_order_id,
        action_seq, attempt_index,
    ) -> str:
        return deterministic_cloid(
            self.session["session_id"], self.session["strategy_revision"], source_address.lower(),
            source_fill_id, source_order_id, coin, action, int(action_seq), int(attempt_index),
        )

    def _submit_attempt(
        self,
        *,
        coin: str,
        is_buy: bool,
        requested_size: float,
        leverage: float,
        reduce_only: bool,
        action: str,
        source_address: str,
        source_fill_id: str,
        source_order_id: str | None,
        source_time_ms: int,
        action_seq: int,
        attempt_index: int,
    ) -> LiveExecutionResult:
        cloid = self._logical_cloid(
            coin=coin, action=action, source_address=source_address,
            source_fill_id=source_fill_id, source_order_id=source_order_id,
            action_seq=action_seq, attempt_index=attempt_index,
        )
        existing = self._existing_intent(cloid)
        if existing:
            if existing[0] in TERMINAL_INTENT_STATES:
                filled = _float(existing[2])
                return LiveExecutionResult(
                    filled, _float(existing[3], None), filled * _float(existing[3]) * config.TAKER_FEE,
                    0.0, (cloid,), tuple([int(existing[1])] if existing[1] else []), existing[0], existing[4],
                )
            # A durable row without a terminal result may have crossed the
            # network boundary before a crash. Never blind-resubmit it.
            status = self.broker.order_status(cloid)
            self._freeze_reconcile("AMBIGUOUS_DURABLE_INTENT")
            self.db.execute(
                "UPDATE execution_order_intent SET state='ambiguous',error_code=?,updated_at=? WHERE cloid=?",
                ("exchange_order_found" if isinstance(status, dict) and status.get("status") == "order" else
                 "exchange_status_unknown", now_iso(), cloid),
            )
            self.db.commit()
            raise RuntimeError("ambiguous_durable_intent")

        notional = requested_size * self._mid(coin)
        limit_px = self._quote_with_retry(coin, is_buy, notional, reduce_only=reduce_only)
        intent = OrderIntent(coin, is_buy, requested_size, limit_px, reduce_only, cloid)
        prepared = self.broker.prepare_order(intent)
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO execution_order_intent "
            "(cloid,session_id,strategy_revision,source_address,source_fill_id,source_order_id,source_time_ms,"
            "action_seq,action,coin,side,reduce_only,leverage,requested_size,requested_limit_px,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'created',?,?)",
            (
                cloid, self.session["session_id"], self.session["strategy_revision"], source_address.lower(),
                source_fill_id, source_order_id, int(source_time_ms), int(action_seq), action, coin,
                "buy" if is_buy else "sell", 1 if reduce_only else 0, leverage,
                prepared.size, prepared.limit_px, stamp, stamp,
            ),
        )
        self.db.execute(
            "INSERT INTO execution_order_attempt "
            "(cloid,attempt_no,request_json,transport_status,started_at) VALUES (?,?,?,'created',?)",
            (cloid, 1, json.dumps({
                "coin": coin, "isBuy": is_buy, "size": prepared.size, "limitPx": prepared.limit_px,
                "reduceOnly": reduce_only, "leverage": leverage,
            }, sort_keys=True, separators=(",", ":")), stamp),
        )
        self.db.execute(
            "UPDATE execution_order_intent SET state='submitting',updated_at=? WHERE cloid=?", (now_iso(), cloid),
        )
        self.db.commit()  # durable before the first signed byte leaves the process
        try:
            coordinated = self.coordinator.submit_once(intent)
            if not coordinated.submitted:
                raise AmbiguousOrderError("existing_cloid_requires_reconcile")
            result = coordinated.result
        except (BrokerError, AmbiguousOrderError) as exc:
            self.db.execute(
                "UPDATE execution_order_attempt SET transport_status='ambiguous',error_code=?,completed_at=? "
                "WHERE cloid=? AND attempt_no=1",
                (type(exc).__name__, now_iso(), cloid),
            )
            self.db.execute(
                "UPDATE execution_order_intent SET state='ambiguous',error_code=?,updated_at=? WHERE cloid=?",
                ("transport_ambiguous", now_iso(), cloid),
            )
            self._freeze_reconcile("ORDER_STATUS_AMBIGUOUS")
            self.db.commit()
            raise RuntimeError("live_order_status_ambiguous") from None

        state = {
            OrderOutcome.FILLED: "filled", OrderOutcome.PARTIAL: "partial",
            OrderOutcome.CANCELED: "canceled", OrderOutcome.REJECTED: "rejected",
            OrderOutcome.RESTING: "ambiguous", OrderOutcome.UNKNOWN: "ambiguous",
        }[result.outcome]
        response = {
            "outcome": result.outcome.value, "oid": result.oid, "filledSize": result.filled_size,
            "averagePx": result.average_px, "errorCode": result.error_code,
        }
        self.db.execute(
            "UPDATE execution_order_attempt SET response_json=?,transport_status=?,error_code=?,completed_at=? "
            "WHERE cloid=? AND attempt_no=1",
            (json.dumps(response, sort_keys=True, separators=(",", ":")), state, result.error_code, now_iso(), cloid),
        )
        self.db.execute(
            "UPDATE execution_order_intent SET state=?,oid=?,filled_size=?,average_px=?,error_code=?,updated_at=? "
            "WHERE cloid=?",
            (state, result.oid, result.filled_size, result.average_px, result.error_code, now_iso(), cloid),
        )
        self.db.commit()
        if state == "ambiguous":
            self._freeze_reconcile("UNEXPECTED_ORDER_STATE")
            self.db.commit()
            raise RuntimeError("live_order_state_ambiguous")
        filled = max(0.0, _float(result.filled_size))
        px = _float(result.average_px, None)
        fee = filled * _float(px) * config.TAKER_FEE
        return LiveExecutionResult(
            filled, px, fee, 0.0, (cloid,), tuple([int(result.oid)] if result.oid else []),
            state, result.error_code,
        )

    def execute(
        self,
        *,
        coin: str,
        is_buy: bool,
        size: float,
        leverage: float,
        reduce_only: bool,
        action: str,
        source_address: str,
        source_fill_id: str,
        source_order_id: str | None,
        source_time_ms: int,
        action_seq: int,
    ) -> LiveExecutionResult:
        with self._lock:
            self.heartbeat_lease()
            if size <= 0 or leverage <= 0:
                raise ValueError("invalid_live_order_size_or_leverage")
            reconcile = self.reconcile()
            if not reconcile.get("ok"):
                raise RuntimeError("live_reconcile_required")
            mark = self._mid(coin)
            recovery_cloids = {
                attempt_index: self._logical_cloid(
                    coin=coin, action=action, source_address=source_address,
                    source_fill_id=source_fill_id, source_order_id=source_order_id,
                    action_seq=action_seq, attempt_index=attempt_index,
                )
                for attempt_index in range(int(config.LIVE_ORDER_MAX_ATTEMPTS))
            }
            recovery_only = False
            if not reduce_only:
                state = self._refresh_session_state()
                recovery_only = state not in INCREASE_STATES
                if recovery_only:
                    if not any(self._existing_intent(cloid) for cloid in recovery_cloids.values()):
                        raise RuntimeError(f"live_increase_blocked:{state}")
                else:
                    allowed_margin = self._increase_margin_cap(coin, size * mark / leverage)
                    size = min(size, allowed_margin * leverage / mark)
                    if size * mark < config.HYPERLIQUID_MIN_PERP_NOTIONAL_USD:
                        raise RuntimeError("NO_EXECUTABLE_CAPACITY")
                    leverage_result = None
                    for leverage_attempt in range(int(config.LIVE_QUOTE_READ_ATTEMPTS)):
                        try:
                            leverage_result = self.coordinator.run_signed(
                                lambda: self.broker.set_isolated_leverage(coin, int(leverage))
                            )
                            break
                        except SigningClockError:
                            raise RuntimeError("live_signing_clock_invalid") from None
                        except BrokerError:
                            if leverage_attempt + 1 < int(config.LIVE_QUOTE_READ_ATTEMPTS):
                                time.sleep(0.05)
                                continue
                            raise RuntimeError("live_leverage_status_ambiguous") from None
                    if not leverage_result.ok:
                        raise RuntimeError(f"live_leverage_rejected:{leverage_result.error_code}")

            remaining = size
            results: list[LiveExecutionResult] = []
            for attempt_index in range(int(config.LIVE_ORDER_MAX_ATTEMPTS)):
                if remaining <= 1e-12:
                    break
                if recovery_only and not self._existing_intent(recovery_cloids[attempt_index]):
                    break
                result = self._submit_attempt(
                    coin=coin, is_buy=is_buy, requested_size=remaining, leverage=leverage,
                    reduce_only=reduce_only, action=action, source_address=source_address,
                    source_fill_id=source_fill_id, source_order_id=source_order_id,
                    source_time_ms=source_time_ms, action_seq=action_seq,
                    attempt_index=attempt_index,
                )
                results.append(result)
                remaining = max(0.0, remaining - result.filled_size)
                if remaining <= 1e-12:
                    break
                if result.error_code not in {None, "ioc_cancel", "no_liquidity"}:
                    break
                if remaining * mark < config.HYPERLIQUID_MIN_PERP_NOTIONAL_USD:
                    break

            total = sum(item.filled_size for item in results)
            average = (
                sum(item.filled_size * _float(item.average_px) for item in results) / total
                if total > 0 else None
            )
            self._sync_fills()
            self.db.commit()
            self.reconcile()
            cloids = tuple(cloid for item in results for cloid in item.cloids)
            confirmed_fee, confirmed_closed_pnl = self._confirmed_fill_totals(cloids)
            return LiveExecutionResult(
                total, average,
                confirmed_fee if confirmed_fee is not None else sum(item.fee for item in results),
                confirmed_closed_pnl if confirmed_closed_pnl is not None else sum(item.closed_pnl for item in results),
                cloids,
                tuple(oid for item in results for oid in item.oids),
                "filled" if total + 1e-12 >= size else "partial" if total > 0 else "unfilled",
                next((item.error_code for item in reversed(results) if item.error_code), None),
            )

    def cancel_managed_orders(self) -> dict:
        """Cancel only this session's exchange-visible orders; unknown orders remain a reconcile stop."""
        with self._lock:
            state = self.reconcile()
            if state.get("unknownOrders"):
                raise RuntimeError("unknown_exchange_orders_prevent_cancel")
            canceled = []
            for order in state.get("orders") or []:
                cloid = str(order.get("cloid") or "").lower()
                coin = str(order.get("coin") or "")
                if not cloid or not coin:
                    continue
                known = self.db.execute(
                    "SELECT 1 FROM execution_order_intent WHERE session_id=? AND lower(cloid)=?",
                    (self.session["session_id"], cloid),
                ).fetchone()
                if not known:
                    raise RuntimeError("unknown_exchange_order_prevents_cancel")
                result = self.coordinator.run_signed(lambda c=coin, i=cloid: self.broker.cancel_by_cloid(c, i))
                if not result.ok:
                    self._freeze_reconcile("MANAGED_CANCEL_FAILED")
                    self.db.commit()
                    raise RuntimeError("managed_order_cancel_failed")
                self.db.execute(
                    "UPDATE execution_order_intent SET state='canceled',updated_at=? "
                    "WHERE session_id=? AND lower(cloid)=?",
                    (now_iso(), self.session["session_id"], cloid),
                )
                canceled.append(cloid)
            self.db.commit()
            return {"canceled": canceled, "count": len(canceled)}
