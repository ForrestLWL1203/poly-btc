"""Credential-agnostic Hyperliquid SDK adapter.

The adapter accepts already-constructed SDK clients. Mainnet reads are always allowed; signed Mainnet actions
require the explicit capability created only by the activated Live execution boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from hyper.market.rate_usage import USAGE

from .orders import (
    ActionResult,
    ClientOrderId,
    MarketSpec,
    OrderIntent,
    PreparedOrder,
    SubmitResult,
    dex_for_coin,
    normalize_action_response,
    normalize_order_response,
    prepare_ioc_order,
)
from .venue import ExecutionNetwork, VenueConfig, venue_config


class BrokerError(RuntimeError):
    pass


class MainnetSigningDisabled(BrokerError):
    pass


class BrokerClientUnavailable(BrokerError):
    pass


class BrokerProtocolError(BrokerError):
    pass


@dataclass(frozen=True)
class AccountSnapshot:
    network: ExecutionNetwork
    account_address: str
    abstraction: Any
    collateral_state: Any
    perp_states: Dict[str, Any]
    open_orders: Dict[str, list]
    frontend_open_orders: Dict[str, list]


@dataclass(frozen=True)
class IdentitySnapshot:
    account_address: str
    account_role: Any
    agent_address: Optional[str]
    agent_role: Any


class HyperliquidBroker:
    """Thin normalized broker over injected official SDK Info/Exchange clients."""

    def __init__(
        self,
        network: ExecutionNetwork | str,
        account_address: str,
        *,
        info_client,
        exchange_client=None,
        supported_dexes: Iterable[str] = ("", "xyz"),
        allow_mainnet_signing: bool = False,
    ):
        self.venue: VenueConfig = venue_config(network)
        self.account_address = self._normalize_address(account_address)
        self.info = info_client
        self.exchange = exchange_client
        self.allow_mainnet_signing = bool(allow_mainnet_signing)
        requested_dexes = tuple(dict.fromkeys(str(dex or "") for dex in supported_dexes))
        self.supported_dexes = ("", *(dex for dex in requested_dexes if dex))
        self._market_specs: Dict[str, MarketSpec] = {}
        self._usage_category = contextvars.ContextVar(
            f"broker_usage_category_{id(self)}", default="market_safety",
        )

    @contextmanager
    def request_category(self, category: str):
        token = self._usage_category.set(str(category or "other"))
        try:
            yield
        finally:
            self._usage_category.reset(token)

    @staticmethod
    def _info_weight(request_type: str) -> int:
        if request_type in {
            "l2Book", "allMids", "clearinghouseState", "orderStatus",
            "spotClearinghouseState", "exchangeStatus",
        }:
            return 2
        if request_type == "userRole":
            return 60
        return 20

    def _info_call(self, request_type: str, fn):
        category = str(self._usage_category.get() or "other")
        weight = self._info_weight(request_type)
        USAGE.record(category=category, weight=weight, requests=1)
        try:
            result = fn()
        except Exception as exc:
            if "429" in str(exc):
                USAGE.record(
                    category=category, weight=0, requests=0, rate_limited=True,
                )
            raise BrokerError(
                f"info_transport_error:{request_type}:{type(exc).__name__}"
            ) from None
        divisors = {
            "userFills": 20, "userFillsByTime": 20,
            "frontendOpenOrders": 20, "historicalOrders": 20,
        }
        divisor = divisors.get(request_type)
        if divisor and isinstance(result, list) and result:
            extra = (len(result) + divisor - 1) // divisor
            USAGE.record(category=category, weight=extra, requests=0)
        return result

    @staticmethod
    def _normalize_address(address: str) -> str:
        value = str(address or "").lower()
        if len(value) != 42 or not value.startswith("0x"):
            raise ValueError("invalid_hyperliquid_account_address")
        try:
            int(value[2:], 16)
        except ValueError as exc:
            raise ValueError("invalid_hyperliquid_account_address") from exc
        return value

    def _require_info(self):
        if self.info is None:
            raise BrokerClientUnavailable("info_client_unavailable")

    def _require_signed_testnet(self):
        if self.venue.network is ExecutionNetwork.MAINNET and not self.allow_mainnet_signing:
            raise MainnetSigningDisabled("mainnet_signed_trading_not_enabled")
        if self.exchange is None:
            raise BrokerClientUnavailable("exchange_client_unavailable")

    def load_market_specs(self, *, force: bool = False) -> Dict[str, MarketSpec]:
        self._require_info()
        if self._market_specs and not force:
            return dict(self._market_specs)

        dex_offsets = {"": 0}
        if len(self.supported_dexes) > 1:
            rows = self._info_call("perpDexs", self.info.perp_dexs)
            if not isinstance(rows, list):
                raise BrokerProtocolError("invalid_perp_dexs_response")
            for index, row in enumerate(rows[1:]):
                if isinstance(row, dict) and row.get("name"):
                    dex_offsets[str(row["name"])] = 110_000 + index * 10_000

        specs: Dict[str, MarketSpec] = {}
        for dex in self.supported_dexes:
            if dex not in dex_offsets:
                raise BrokerProtocolError(f"supported_dex_missing:{dex}")
            meta = self._info_call("meta", lambda d=dex: self.info.meta(dex=d))
            universe = meta.get("universe") if isinstance(meta, dict) else None
            if not isinstance(universe, list) or not universe:
                raise BrokerProtocolError(f"invalid_meta_response:{dex or 'standard'}")
            for index, row in enumerate(universe):
                if not isinstance(row, dict) or not row.get("name"):
                    raise BrokerProtocolError(f"invalid_market_row:{dex or 'standard'}:{index}")
                coin = str(row["name"])
                if dex and dex_for_coin(coin) != dex:
                    raise BrokerProtocolError(f"unqualified_builder_market:{coin}")
                try:
                    sz_decimals = int(row["szDecimals"])
                    max_leverage = int(row["maxLeverage"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise BrokerProtocolError(f"invalid_market_precision_or_leverage:{coin}") from exc
                if not 0 <= sz_decimals <= 6 or max_leverage <= 0:
                    raise BrokerProtocolError(f"invalid_market_precision_or_leverage:{coin}")
                specs[coin] = MarketSpec(
                    coin=coin,
                    dex=dex,
                    asset_id=dex_offsets[dex] + index,
                    sz_decimals=sz_decimals,
                    max_leverage=max_leverage,
                )
        self._market_specs = specs
        return dict(specs)

    def market_spec(self, coin: str) -> MarketSpec:
        specs = self.load_market_specs()
        try:
            return specs[str(coin)]
        except KeyError as exc:
            raise BrokerProtocolError(f"unsupported_market:{coin}") from exc

    def l2_book(self, coin: str):
        self._require_info()
        self.market_spec(coin)
        book = self._info_call("l2Book", lambda: self.info.l2_snapshot(coin))
        if not isinstance(book, dict) or not isinstance(book.get("levels"), list):
            raise BrokerProtocolError(f"invalid_l2_book:{coin}")
        return book

    def all_mids(self, dex: str = "") -> Dict[str, str]:
        self._require_info()
        normalized_dex = str(dex or "")
        if normalized_dex not in self.supported_dexes:
            raise BrokerProtocolError(f"unsupported_dex:{normalized_dex}")
        mids = self._info_call("allMids", lambda: self.info.all_mids(dex=normalized_dex))
        if not isinstance(mids, dict):
            raise BrokerProtocolError(f"invalid_all_mids:{normalized_dex or 'standard'}")
        return mids

    def market_contexts(self, dex: str = ""):
        """Return official metaAndAssetCtxs for one perp DEX.

        SDK 0.24.0 does not expose the documented ``dex`` argument on its convenience method, so HIP-3 reads
        use the SDK's public ``post`` transport with the official info payload.  No signing is involved.
        """
        self._require_info()
        normalized_dex = str(dex or "")
        if normalized_dex not in self.supported_dexes:
            raise BrokerProtocolError(f"unsupported_dex:{normalized_dex}")
        if normalized_dex:
            contexts = self._info_call(
                "metaAndAssetCtxs",
                lambda: self.info.post(
                    "/info", {"type": "metaAndAssetCtxs", "dex": normalized_dex}
                ),
            )
        else:
            contexts = self._info_call("metaAndAssetCtxs", self.info.meta_and_asset_ctxs)
        if not isinstance(contexts, list) or len(contexts) != 2:
            raise BrokerProtocolError(f"invalid_market_contexts:{normalized_dex or 'standard'}")
        return contexts

    def identity_snapshot(self, agent_address: Optional[str] = None) -> IdentitySnapshot:
        self._require_info()
        normalized_agent = self._normalize_address(agent_address) if agent_address else None
        return IdentitySnapshot(
            account_address=self.account_address,
            account_role=self._info_call(
                "userRole", lambda: self.info.user_role(self.account_address),
            ),
            agent_address=normalized_agent,
            agent_role=(
                self._info_call("userRole", lambda: self.info.user_role(normalized_agent))
                if normalized_agent else None
            ),
        )

    def agent_authorization(self, agent_address: str) -> Optional[dict]:
        """Return the authoritative named-Agent authorization for this account.

        Hyperliquid stores an Agent's expiry in the account's ``extraAgents``
        state.  It is not encoded in the private key and therefore must never
        be accepted as operator-entered credential metadata.
        """
        self._require_info()
        normalized_agent = self._normalize_address(agent_address)
        fetch = getattr(self.info, "extra_agents", None)
        rows = (
            self._info_call("extraAgents", lambda: fetch(self.account_address))
            if callable(fetch)
            else self._info_call(
                "extraAgents",
                lambda: self.info.post(
                    "/info", {"type": "extraAgents", "user": self.account_address}
                ),
            )
        )
        if not isinstance(rows, list):
            raise BrokerProtocolError("invalid_extra_agents_response")
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("address") or "").lower() != normalized_agent:
                continue
            try:
                valid_until = int(row.get("validUntil"))
            except (TypeError, ValueError) as exc:
                raise BrokerProtocolError("invalid_agent_authorization_expiry") from exc
            if valid_until <= 0:
                raise BrokerProtocolError("invalid_agent_authorization_expiry")
            return {
                "address": normalized_agent,
                "name": str(row.get("name") or ""),
                "validUntil": valid_until,
            }
        return None

    def account_snapshot(self) -> AccountSnapshot:
        self._require_info()
        perp_states = {
            dex: self._info_call(
                "clearinghouseState",
                lambda d=dex: self.info.user_state(self.account_address, dex=d),
            )
            for dex in self.supported_dexes
        }
        open_orders = {
            dex: self._info_call(
                "openOrders",
                lambda d=dex: self.info.open_orders(self.account_address, dex=d),
            )
            for dex in self.supported_dexes
        }
        frontend_open_orders = {
            dex: self._info_call(
                "frontendOpenOrders",
                lambda d=dex: self.info.frontend_open_orders(self.account_address, dex=d),
            )
            for dex in self.supported_dexes
        }
        for dex, orders in open_orders.items():
            if not isinstance(orders, list):
                raise BrokerProtocolError(f"invalid_open_orders:{dex or 'standard'}")
        for dex, orders in frontend_open_orders.items():
            if not isinstance(orders, list):
                raise BrokerProtocolError(f"invalid_frontend_open_orders:{dex or 'standard'}")
        return AccountSnapshot(
            network=self.venue.network,
            account_address=self.account_address,
            abstraction=self._info_call(
                "userAbstraction", lambda: self.info.query_user_abstraction_state(self.account_address),
            ),
            collateral_state=self._info_call(
                "spotClearinghouseState", lambda: self.info.spot_user_state(self.account_address),
            ),
            perp_states=perp_states,
            open_orders=open_orders,
            frontend_open_orders=frontend_open_orders,
        )

    def recent_fills(self):
        self._require_info()
        fills = self._info_call("userFills", lambda: self.info.user_fills(self.account_address))
        if not isinstance(fills, list):
            raise BrokerProtocolError("invalid_user_fills")
        return fills

    def fills_by_time(self, start_time_ms: int, end_time_ms: Optional[int] = None):
        self._require_info()
        start = int(start_time_ms)
        end = int(end_time_ms) if end_time_ms is not None else None
        if start < 0 or (end is not None and end < start):
            raise ValueError("invalid_fill_time_range")
        fills = self._info_call(
            "userFillsByTime",
            lambda: self.info.user_fills_by_time(
                self.account_address,
                start,
                end_time=end,
                aggregate_by_time=False,
            ),
        )
        if not isinstance(fills, list):
            raise BrokerProtocolError("invalid_user_fills_by_time")
        return fills

    def historical_orders(self):
        self._require_info()
        orders = self._info_call(
            "historicalOrders", lambda: self.info.historical_orders(self.account_address),
        )
        if not isinstance(orders, list):
            raise BrokerProtocolError("invalid_historical_orders")
        return orders

    def prepare_order(self, intent: OrderIntent) -> PreparedOrder:
        return prepare_ioc_order(intent, self.market_spec(intent.coin))

    def submit_ioc(self, intent: OrderIntent) -> SubmitResult:
        self._require_signed_testnet()
        order = self.prepare_order(intent)
        try:
            response = self.exchange.order(
                order.coin,
                order.is_buy,
                order.size,
                order.limit_px,
                order_type={"limit": {"tif": "Ioc"}},
                reduce_only=order.reduce_only,
                cloid=ClientOrderId(order.cloid),
            )
        except Exception as exc:  # noqa: BLE001 - normalized at the broker boundary
            raise BrokerError(f"order_transport_error:{type(exc).__name__}") from None
        return normalize_order_response(response, requested_size=order.size)

    def set_leverage(self, coin: str, leverage: int, *, is_cross: bool) -> ActionResult:
        self._require_signed_testnet()
        market = self.market_spec(coin)
        requested = int(leverage)
        if requested < 1 or requested > market.max_leverage:
            raise ValueError(f"invalid_leverage:{requested}:max={market.max_leverage}")
        try:
            response = self.exchange.update_leverage(requested, coin, is_cross=bool(is_cross))
        except Exception as exc:  # noqa: BLE001 - normalized at the broker boundary
            raise BrokerError(f"leverage_transport_error:{type(exc).__name__}") from None
        return normalize_action_response(response)

    def set_isolated_leverage(self, coin: str, leverage: int) -> ActionResult:
        return self.set_leverage(coin, leverage, is_cross=False)

    def set_cross_leverage(self, coin: str, leverage: int) -> ActionResult:
        return self.set_leverage(coin, leverage, is_cross=True)

    def cancel_by_cloid(self, coin: str, cloid: str) -> ActionResult:
        self._require_signed_testnet()
        self.market_spec(coin)
        try:
            response = self.exchange.cancel_by_cloid(coin, ClientOrderId(cloid))
        except Exception as exc:  # noqa: BLE001 - normalized at the broker boundary
            raise BrokerError(f"cancel_transport_error:{type(exc).__name__}") from None
        return normalize_action_response(response)

    def cancel_by_oid(self, coin: str, oid: int) -> ActionResult:
        self._require_signed_testnet()
        self.market_spec(coin)
        normalized_oid = int(oid)
        if normalized_oid <= 0:
            raise ValueError("invalid_order_id")
        try:
            response = self.exchange.cancel(coin, normalized_oid)
        except Exception as exc:  # noqa: BLE001 - normalized at the broker boundary
            raise BrokerError(f"cancel_transport_error:{type(exc).__name__}") from None
        return normalize_action_response(response)

    def order_status(self, cloid: str):
        self._require_info()
        return self._info_call(
            "orderStatus",
            lambda: self.info.query_order_by_cloid(self.account_address, ClientOrderId(cloid)),
        )

    def order_status_by_oid(self, oid: int):
        self._require_info()
        normalized_oid = int(oid)
        if normalized_oid <= 0:
            raise ValueError("invalid_order_id")
        return self._info_call(
            "orderStatus", lambda: self.info.query_order_by_oid(self.account_address, normalized_oid),
        )
