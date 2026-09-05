"""Saxo OpenAPI broker adapter.

The adapter consumes an OAuth access token that has already been obtained by
the platform's authorization-code flow. Saxo rotates the refresh token on every
refresh, so this money-moving process never refreshes tokens itself. Instead it
reloads the complete, atomically persisted credential document before the
access token expires and fails closed when no safe replacement is available.

Official contracts:
- Environments: https://developer.saxobank.com/openapi/learn/environments
- OAuth/security: https://developer.saxobank.com/openapi/learn/security
- Orders: https://developer.saxobank.com/openapi/learn/order-placement
- Rate limits: https://developer.saxobank.com/openapi/learn/rate-limiting
- Pre-trade disclaimers:
  https://developer.saxobank.com/openapi/learn/pre-trade-disclaimers
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, cast
from urllib.parse import quote

import httpx

from lib_common.logging import get_logger
from lib_infrastructure.brokers.base import BaseBrokerAdapter, RateLimitConfig, RetryConfig
from lib_infrastructure.brokers.secrets import BrokerCredentials
from lib_strategy.types import (
    AccountBalance,
    BrokerAccountSnapshot,
    BrokerCapabilities,
    BrokerExecutionMethod,
    BrokerOrderIntent,
    BrokerOrderResult,
    BrokerPosition,
)

logger = get_logger(__name__)

CredentialLoader = Callable[[], Awaitable[BrokerCredentials | None]]


class SaxoSessionError(RuntimeError):
    """Raised when a Saxo OAuth session cannot be used safely."""


class SaxoAdapter(BaseBrokerAdapter):
    """Account-scoped Saxo OpenAPI adapter for automated single-leg orders.

    ``AccountKey``, ``ClientKey``, ``Uic`` and ``AssetType`` remain explicit at
    the broker boundary. SIM and LIVE use different Saxo applications and
    endpoints; an environment is never inferred from a token or account.

    Multi-leg execution is intentionally disabled until the option-root
    ``CanParticipateInMultiLegOrder`` capability can be proven for every routed
    order. Pre-trade disclaimers are never accepted automatically.
    """

    SIM_BASE_URL = "https://gateway.saxobank.com/sim/openapi"
    LIVE_BASE_URL = "https://gateway.saxobank.com/openapi"

    DEFAULT_RATE_LIMIT = RateLimitConfig(
        requests_per_second=2,
        requests_per_minute=120,
        burst_limit=2,
    )
    DEFAULT_RETRY = RetryConfig(
        max_retries=3,
        base_delay=1.0,
        max_delay=10.0,
        retryable_errors=(
            ConnectionError,
            TimeoutError,
            httpx.ConnectError,
            httpx.ReadTimeout,
        ),
    )

    TOKEN_EXPIRY_SAFETY = timedelta(seconds=60)
    MAX_PAGES = 1000
    MIN_CURRENCY_LENGTH = 3
    MAX_CURRENCY_LENGTH = 10

    _SUPPORTED_ASSET_TYPES: ClassVar[set[str]] = {
        "ContractFutures",
        "FuturesOption",
        "FxSpot",
        "Stock",
        "StockIndexOption",
        "StockOption",
    }
    _OPTION_ASSET_TYPES: ClassVar[set[str]] = {
        "FuturesOption",
        "StockIndexOption",
        "StockOption",
    }
    _DERIVATIVE_ASSET_TYPES: ClassVar[set[str]] = {
        "ContractFutures",
        "FuturesOption",
        "StockIndexOption",
        "StockOption",
    }
    _ORDER_TYPES: ClassVar[dict[str, str]] = {
        "market": "Market",
        "limit": "Limit",
        "stop": "Stop",
        "stop_limit": "StopLimit",
    }
    _DURATIONS: ClassVar[dict[str, str]] = {
        "day": "DayOrder",
        "gtc": "GoodTillCancel",
        "ioc": "ImmediateOrCancel",
        "fok": "FillOrKill",
    }

    def __init__(
        self,
        environment: str = "paper",
        rate_limit: RateLimitConfig | None = None,
        retry_config: RetryConfig | None = None,
    ) -> None:
        if environment not in {"paper", "live"}:
            msg = "Saxo environment must be 'paper' or 'live'"
            raise ValueError(msg)
        super().__init__(
            environment=environment,
            rate_limit_config=rate_limit or self.DEFAULT_RATE_LIMIT,
            retry_config=retry_config or self.DEFAULT_RETRY,
        )
        self._base_url = self.SIM_BASE_URL if environment == "paper" else self.LIVE_BASE_URL
        self._client: httpx.AsyncClient | None = None
        self._access_token = ""
        self._access_token_expires_at: datetime | None = None
        self._account_key = ""
        self._client_key = ""
        self._account_currency = ""
        self._credential_loader: CredentialLoader | None = None
        self._credential_lock = asyncio.Lock()
        self._order_lock = asyncio.Lock()
        self._last_order_request_at = 0.0

    @property
    def broker_code(self) -> str:
        return "saxo"

    @property
    def supported_asset_classes(self) -> list[str]:
        return ["equity", "etf", "futures", "options", "fx", "commodities"]

    @property
    def supported_execution_methods(self) -> list[BrokerExecutionMethod]:
        return [
            BrokerExecutionMethod.SPOT,
            BrokerExecutionMethod.MARGIN,
            BrokerExecutionMethod.FUTURES,
            BrokerExecutionMethod.OPTIONS_SINGLE,
        ]

    @property
    def supports_paper_trading(self) -> bool:
        return True

    @property
    def supports_multi_leg_orders(self) -> bool:
        return False

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            asset_classes=self.supported_asset_classes,
            execution_methods=[method.value for method in self.supported_execution_methods],
            multi_leg_orders=False,
            paper_trading={"native": True, "environment": "SIM"},
            regions=["global"],
            supported_order_types=list(self._ORDER_TYPES),
        )

    def configure_credential_loader(self, loader: CredentialLoader) -> None:
        """Configure the durable credential reload boundary.

        The loader must read the entire current credential document. The writer
        that performs Saxo's refresh-token exchange owns atomically persisting
        both the new access token and the newly rotated refresh token before
        this adapter can adopt the session.
        """
        self._credential_loader = loader

    async def _do_connect(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str | None = None,  # noqa: ARG002
        subaccount: str | None = None,  # noqa: ARG002
        **kwargs: str,
    ) -> bool:
        if not api_key.strip() or not api_secret.strip():
            msg = "Saxo AppKey and AppSecret are required"
            raise ValueError(msg)
        self._adopt_session_values(
            access_token=kwargs.get("access_token", ""),
            access_token_expires_at=kwargs.get("access_token_expires_at", ""),
            account_key=kwargs.get("account_key", ""),
            client_key=kwargs.get("client_key", ""),
            require_same_identity=False,
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        try:
            account = await self._request(
                "GET",
                f"/port/v1/accounts/{quote(self._account_key, safe='')}",
            )
            self._validate_account(account)
        except Exception:
            await self._client.aclose()
            self._client = None
            raise
        logger.info(
            "Connected to Saxo account",
            environment=self.environment,
            account_currency=self._account_currency,
        )
        return True

    async def _do_disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None
        self._access_token = ""
        self._access_token_expires_at = None
        self._account_currency = ""

    async def _do_place_order(  # noqa: PLR0911
        self,
        order: BrokerOrderIntent,
    ) -> BrokerOrderResult:
        try:
            payload = self._build_order_payload(order)
        except (KeyError, TypeError, ValueError) as exc:
            return self._error_result(str(exc), "invalid_order")

        client_order_id = str(order.get("client_order_id", "")).strip()
        stage = "precheck"
        try:
            precheck = await self._request(
                "POST",
                "/trade/v2/orders/precheck",
                json=payload,
            )
            precheck_error = self._response_error(precheck)
            if precheck_error is not None:
                code, message = precheck_error
                return self._error_result(message, code)
            disclaimers = precheck.get("PreTradeDisclaimers")
            if disclaimers:
                return {
                    "success": False,
                    "status": "rejected",
                    "filled_quantity": "0",
                    "client_order_id": client_order_id,
                    "error_code": "pre_trade_disclaimer_required",
                    "error_message": (
                        "Saxo requires an account owner to review pre-trade "
                        "disclaimers before this order can be placed"
                    ),
                    "raw_response": precheck,
                }
            if str(precheck.get("PreCheckResult", "")).strip().lower() != "ok":
                return self._error_result(
                    "Saxo pre-check did not return PreCheckResult=Ok",
                    "precheck_not_ok",
                )

            stage = "placement"
            async with self._order_lock:
                await self._wait_for_order_slot()
                # Saxo's order throttle applies to submitted attempts, including
                # requests whose transport outcome is ambiguous.
                self._last_order_request_at = time.monotonic()
                response = await self._request(
                    "POST",
                    "/trade/v2/orders",
                    json=payload,
                )
        except httpx.HTTPStatusError as exc:
            return self._http_order_error(exc, stage=stage, client_order_id=client_order_id)
        except httpx.TransportError as exc:
            return {
                "success": False,
                "status": "pending" if stage == "placement" else "rejected",
                "filled_quantity": "0",
                "client_order_id": client_order_id,
                "error_code": (
                    "order_transport_uncertain"
                    if stage == "placement"
                    else "precheck_transport_failed"
                ),
                "error_message": (
                    "Saxo order transport failed after submission began; do not retry automatically"
                    if stage == "placement"
                    else "Saxo pre-check transport failed; no order was submitted"
                ),
                "raw_response": {"exception_type": type(exc).__name__},
            }
        except (SaxoSessionError, TypeError, ValueError) as exc:
            return self._error_result(str(exc), "saxo_session_or_response_invalid")

        response_error = self._response_error(response)
        broker_order_id = str(response.get("OrderId", "")).strip()
        if response_error is not None:
            code, message = response_error
            if code == "TradeNotCompleted" and broker_order_id:
                return {
                    "success": True,
                    "status": "pending",
                    "broker_order_id": broker_order_id,
                    "client_order_id": client_order_id,
                    "filled_quantity": "0",
                    "error_code": code,
                    "error_message": message,
                    "raw_response": response,
                }
            result = self._error_result(message, code)
            result["client_order_id"] = client_order_id
            result["raw_response"] = response
            return result
        if not broker_order_id:
            return self._error_result(
                "Saxo accepted neither an OrderId nor ErrorInfo",
                "invalid_order_response",
            )
        return {
            "success": True,
            "status": "submitted",
            "broker_order_id": broker_order_id,
            "client_order_id": client_order_id,
            "filled_quantity": "0",
            "raw_response": response,
        }

    async def _do_cancel_order(self, broker_order_id: str) -> bool:
        order_id = broker_order_id.strip()
        if not order_id:
            msg = "Saxo broker_order_id is required"
            raise ValueError(msg)
        try:
            response = await self._request(
                "DELETE",
                f"/trade/v2/orders/{quote(order_id, safe='')}",
                params={"AccountKey": self._account_key},
            )
        except httpx.HTTPError:
            logger.exception(
                "Saxo cancellation outcome is uncertain; cancellation is not retried",
                order_id=order_id,
            )
            return False
        return self._response_error(response) is None

    async def _do_get_order_status(self, broker_order_id: str) -> BrokerOrderResult:
        order_id = broker_order_id.strip()
        if not order_id:
            msg = "Saxo broker_order_id is required"
            raise ValueError(msg)
        response = await self._request(
            "GET",
            "/cs/v1/audit/orderactivities",
            params={
                "AccountKey": self._account_key,
                "ClientKey": self._client_key,
                "EntryType": "Last",
                "FieldGroups": "DisplayAndFormat",
                "OrderId": order_id,
                "$top": 1,
            },
        )
        rows = self._collection_rows(response, "Saxo order activity")
        if not rows:
            msg = f"Saxo order {order_id!r} was not found in the account audit"
            raise ValueError(msg)
        return self._order_activity_result(rows[0], fallback_order_id=order_id)

    async def _do_get_account_balance(self) -> list[AccountBalance]:
        payload = await self._fetch_balance()
        return [self._balance_result(payload)]

    async def _do_get_account_snapshot(self) -> BrokerAccountSnapshot:
        balance, positions = await asyncio.gather(
            self._fetch_balance(),
            self._do_get_positions(),
        )
        currency = self._required_currency(balance.get("Currency"), "Saxo balance currency")
        total_value = self._required_decimal(balance.get("TotalValue"), "Saxo TotalValue")
        available = self._required_decimal(
            balance.get("CashAvailableForTrading"),
            "Saxo CashAvailableForTrading",
        )
        margin = abs(
            self._required_decimal(
                balance.get("MarginUsedByCurrentPositions"),
                "Saxo MarginUsedByCurrentPositions",
            )
        )
        return BrokerAccountSnapshot(
            balances=[self._balance_result(balance)],
            positions=positions,
            equity=[{"value": str(total_value), "currency": currency}],
            available_cash=[{"value": str(available), "currency": currency}],
            margin_used=[{"value": str(margin), "currency": currency}],
            equity_source="broker",
        )

    async def _do_get_open_orders(self, symbol: str | None = None) -> list[BrokerOrderResult]:
        rows = await self._get_all_pages(
            "/port/v1/orders",
            params={
                "AccountKey": self._account_key,
                "ClientKey": self._client_key,
                "FieldGroups": "DisplayAndFormat",
            },
            label="Saxo open orders",
        )
        results: list[BrokerOrderResult] = []
        for row in rows:
            display = row.get("DisplayAndFormat")
            display_mapping = display if isinstance(display, Mapping) else {}
            row_symbol = str(display_mapping.get("Symbol", "")).strip()
            if symbol is not None and row_symbol != symbol:
                continue
            order_id = str(row.get("OrderId", "")).strip()
            if not order_id:
                msg = "Saxo open order omitted OrderId"
                raise TypeError(msg)
            results.append(
                {
                    "success": True,
                    "status": self._map_open_order_status(row.get("Status")),
                    "broker_order_id": order_id,
                    # Saxo omits FilledAmount when an open order has no partial
                    # fill; the field's documented meaning makes absence zero.
                    "filled_quantity": str(row.get("FilledAmount", 0)),
                    "client_order_id": str(row.get("ExternalReference", "")),
                    "timestamp": str(row.get("OrderTime", "")),
                    "raw_response": dict(row),
                }
            )
        return results

    async def _do_get_positions(self) -> list[BrokerPosition]:
        rows = await self._get_all_pages(
            "/port/v1/positions",
            params={
                "AccountKey": self._account_key,
                "ClientKey": self._client_key,
                "FieldGroups": "PositionBase,PositionView,DisplayAndFormat",
            },
            label="Saxo positions",
        )
        positions: list[BrokerPosition] = []
        for row in rows:
            parsed = self._position_result(row)
            if parsed is not None:
                positions.append(parsed)
        return positions

    async def _fetch_balance(self) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "/port/v1/balances",
            params={"AccountKey": self._account_key, "ClientKey": self._client_key},
        )
        reliability = str(payload.get("CalculationReliability", "")).strip().lower()
        if reliability and reliability != "ok":
            msg = f"Saxo balance CalculationReliability={reliability!r}"
            raise RuntimeError(msg)
        return payload

    def _balance_result(self, payload: Mapping[str, Any]) -> AccountBalance:
        currency = self._required_currency(payload.get("Currency"), "Saxo balance currency")
        cash = self._required_decimal(payload.get("CashBalance"), "Saxo CashBalance")
        available = self._required_decimal(
            payload.get("CashAvailableForTrading"),
            "Saxo CashAvailableForTrading",
        )
        blocked = self._required_decimal(payload.get("CashBlocked"), "Saxo CashBlocked")
        return AccountBalance(
            currency=currency,
            total=str(cash),
            available=str(available),
            locked=str(blocked),
        )

    def _position_result(self, row: Mapping[str, Any]) -> BrokerPosition | None:
        base = row.get("PositionBase")
        view = row.get("PositionView")
        display = row.get("DisplayAndFormat")
        if not isinstance(base, Mapping) or not isinstance(view, Mapping):
            msg = "Saxo position omitted PositionBase or PositionView"
            raise TypeError(msg)
        if str(base.get("Status", "")).strip().lower() != "open":
            return None
        amount = self._required_decimal(base.get("Amount"), "Saxo position Amount")
        if amount == 0:
            return None
        asset_type = str(base.get("AssetType", "")).strip()
        if asset_type not in self._SUPPORTED_ASSET_TYPES:
            msg = f"Saxo position uses unsupported AssetType {asset_type!r}"
            raise RuntimeError(msg)
        display_mapping = display if isinstance(display, Mapping) else {}
        symbol = str(display_mapping.get("Symbol", "")).strip()
        if not symbol:
            msg = "Saxo position omitted DisplayAndFormat.Symbol"
            raise TypeError(msg)
        price_currency = self._required_currency(
            display_mapping.get("Currency"),
            f"Saxo position {symbol} price currency",
        )
        account_currency = self._required_currency(
            self._account_currency,
            "Saxo account currency",
        )
        entry_price = self._required_decimal(base.get("OpenPrice"), f"Saxo {symbol} OpenPrice")
        current_price = self._required_decimal(
            view.get("CurrentPrice"),
            f"Saxo {symbol} CurrentPrice",
        )
        gross_notional = abs(
            self._required_decimal(
                view.get("ExposureInBaseCurrency"),
                f"Saxo {symbol} ExposureInBaseCurrency",
            )
        )
        if gross_notional <= 0:
            msg = f"Saxo position {symbol!r} returned non-positive base-currency exposure"
            raise RuntimeError(msg)
        is_derivative = asset_type in self._DERIVATIVE_ASSET_TYPES
        position: BrokerPosition = {
            "symbol": symbol,
            "side": "long" if amount > 0 else "short",
            "quantity": str(amount),
            "quantity_unit": "contracts" if is_derivative else "asset",
            "notional_type": "broker_mark_value" if is_derivative else "spot",
            "is_quanto": False,
            "entry_price": str(entry_price),
            "current_price": str(current_price),
            "currency": price_currency,
            "quote_currency": price_currency,
            "settlement_currency": account_currency,
            "pnl_currency": account_currency,
            "notional_currency": account_currency,
            "gross_notional": str(gross_notional),
            "asset_class": self._asset_class_for(asset_type),
            "product": asset_type,
        }
        pnl = view.get("ProfitLossOnTradeInBaseCurrency")
        if pnl not in (None, ""):
            position["unrealized_pnl"] = str(
                self._required_decimal(pnl, f"Saxo {symbol} ProfitLossOnTradeInBaseCurrency")
            )
        # Saxo's open-position payload has no per-position realized P&L.
        # Derivative risk valuation therefore remains intentionally incomplete
        # and the execution bridge fails closed instead of inventing zero.
        return position

    def _build_order_payload(self, order: BrokerOrderIntent) -> dict[str, Any]:
        side = str(order.get("side", "")).strip().lower()
        if side not in {"buy", "sell"}:
            msg = "Saxo order side must be 'buy' or 'sell'"
            raise ValueError(msg)
        quantity = self._required_decimal(order.get("quantity"), "Saxo order quantity")
        if quantity <= 0:
            msg = "Saxo order quantity must be positive"
            raise ValueError(msg)
        uic = self._positive_integer_identifier(
            order.get("broker_instrument_id"),
            "Saxo UIC",
        )
        asset_type = str(order.get("broker_instrument_type", "")).strip()
        if asset_type not in self._SUPPORTED_ASSET_TYPES:
            msg = f"Saxo order uses unsupported broker_instrument_type {asset_type!r}"
            raise ValueError(msg)
        order_type_key = str(order.get("order_type", "")).strip().lower()
        try:
            order_type = self._ORDER_TYPES[order_type_key]
        except KeyError as exc:
            msg = f"Saxo order uses unsupported order_type {order_type_key!r}"
            raise ValueError(msg) from exc
        duration_key = str(order.get("time_in_force", "day")).strip().lower()
        try:
            duration = self._DURATIONS[duration_key]
        except KeyError as exc:
            msg = f"Saxo order uses unsupported time_in_force {duration_key!r}"
            raise ValueError(msg) from exc

        payload: dict[str, Any] = {
            "AccountKey": self._account_key,
            "Amount": self._json_number(quantity),
            "AssetType": asset_type,
            "BuySell": "Buy" if side == "buy" else "Sell",
            "ManualOrder": False,
            "OrderDuration": {"DurationType": duration},
            "OrderType": order_type,
            "Uic": uic,
            "WithAdvice": False,
        }
        client_order_id = str(order.get("client_order_id", "")).strip()
        if client_order_id:
            if len(client_order_id) > 50:  # noqa: PLR2004
                msg = "Saxo client_order_id exceeds 50 characters"
                raise ValueError(msg)
            payload["ExternalReference"] = client_order_id

        if order_type_key == "limit":
            payload["OrderPrice"] = self._json_number(
                self._positive_price(order.get("limit_price"), "Saxo limit_price")
            )
        elif order_type_key == "stop":
            payload["OrderPrice"] = self._json_number(
                self._positive_price(order.get("stop_price"), "Saxo stop_price")
            )
        elif order_type_key == "stop_limit":
            payload["OrderPrice"] = self._json_number(
                self._positive_price(order.get("limit_price"), "Saxo limit_price")
            )
            payload["StopLimitPrice"] = self._json_number(
                self._positive_price(order.get("stop_price"), "Saxo stop_price")
            )

        if asset_type in self._OPTION_ASSET_TYPES:
            to_open_close = str(order.get("to_open_close", "")).strip().lower()
            mapping = {"to_open": "ToOpen", "to_close": "ToClose"}
            if to_open_close not in mapping:
                msg = "Saxo option orders require to_open_close='to_open' or 'to_close'"
                raise ValueError(msg)
            payload["ToOpenClose"] = mapping[to_open_close]
        return payload

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._client is None:
            msg = "Saxo adapter is not connected"
            raise ConnectionError(msg)
        await self._ensure_access_token()
        response = await self._client.request(
            method,
            path,
            params=params,
            json=json,
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        response.raise_for_status()
        if response.status_code == httpx.codes.NO_CONTENT:
            return {}
        payload = response.json()
        if not isinstance(payload, dict):
            msg = "Saxo response was not a JSON object"
            raise TypeError(msg)
        return cast(dict[str, Any], payload)

    async def _get_all_pages(
        self,
        path: str,
        *,
        params: Mapping[str, Any],
        label: str,
    ) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        next_path: str | None = path
        next_params: Mapping[str, Any] | None = params
        seen: set[str] = set()
        for _ in range(self.MAX_PAGES):
            if next_path is None:
                return rows
            if next_path in seen:
                msg = f"{label} pagination returned a cycle"
                raise RuntimeError(msg)
            seen.add(next_path)
            page = await self._request("GET", next_path, params=next_params)
            rows.extend(self._collection_rows(page, label))
            raw_next = page.get("__next")
            if raw_next in (None, ""):
                return rows
            next_path = str(raw_next)
            if not next_path.startswith("/") or next_path.startswith("//"):
                msg = f"{label} returned an unsafe pagination URL"
                raise RuntimeError(msg)
            next_params = None
        msg = f"{label} exceeded {self.MAX_PAGES} pages"
        raise RuntimeError(msg)

    async def _ensure_access_token(self) -> None:
        if not self._token_needs_replacement():
            return
        async with self._credential_lock:
            if not self._token_needs_replacement():
                return
            if self._credential_loader is None:
                msg = (
                    "Saxo access token is expired or near expiry and no durable "
                    "credential reload boundary is configured"
                )
                raise SaxoSessionError(msg)
            credentials = await self._credential_loader()
            if credentials is None:
                msg = "Saxo credential reload returned no credentials"
                raise SaxoSessionError(msg)
            additional = credentials.additional or {}
            self._adopt_session_values(
                access_token=str(additional.get("access_token", "")),
                access_token_expires_at=str(additional.get("access_token_expires_at", "")),
                account_key=str(additional.get("account_key", "")),
                client_key=str(additional.get("client_key", "")),
                require_same_identity=True,
            )
            if self._token_needs_replacement():
                msg = "Persisted Saxo replacement token is already expired or near expiry"
                raise SaxoSessionError(msg)

    def _adopt_session_values(
        self,
        *,
        access_token: str,
        access_token_expires_at: str,
        account_key: str,
        client_key: str,
        require_same_identity: bool,
    ) -> None:
        token = access_token.strip()
        if not token:
            msg = "Saxo access_token is required"
            raise ValueError(msg)
        expiry = self._parse_expiry(access_token_expires_at, "access_token_expires_at")
        normalized_account = account_key.strip()
        normalized_client = client_key.strip()
        if not normalized_account or not normalized_client:
            msg = "Saxo account_key and client_key are required"
            raise ValueError(msg)
        if require_same_identity and (
            normalized_account != self._account_key or normalized_client != self._client_key
        ):
            msg = (
                "Refreshed Saxo credentials changed AccountKey or ClientKey; "
                "a running account adapter cannot change identity"
            )
            raise SaxoSessionError(msg)
        self._access_token = token
        self._access_token_expires_at = expiry
        self._account_key = normalized_account
        self._client_key = normalized_client
        if self._token_needs_replacement():
            msg = "Saxo access_token is expired or too close to expiry"
            raise SaxoSessionError(msg)

    def _validate_account(self, account: Mapping[str, Any]) -> None:
        if str(account.get("AccountKey", "")).strip() != self._account_key:
            msg = "Saxo token did not resolve the configured AccountKey"
            raise SaxoSessionError(msg)
        if str(account.get("ClientKey", "")).strip() != self._client_key:
            msg = "Saxo AccountKey does not belong to the configured ClientKey"
            raise SaxoSessionError(msg)
        if account.get("Active") is not True:
            msg = "Saxo account is not active"
            raise SaxoSessionError(msg)
        self._account_currency = self._required_currency(
            account.get("Currency"),
            "Saxo account Currency",
        )

    def _token_needs_replacement(self) -> bool:
        expiry = self._access_token_expires_at
        if not self._access_token or expiry is None:
            return True
        return expiry <= datetime.now(tz=UTC) + self.TOKEN_EXPIRY_SAFETY

    async def _wait_for_order_slot(self) -> None:
        elapsed = time.monotonic() - self._last_order_request_at
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)

    @staticmethod
    def _parse_expiry(value: str, field: str) -> datetime:
        raw = value.strip()
        if not raw:
            msg = f"Saxo {field} is required"
            raise ValueError(msg)
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            msg = f"Saxo {field} must be RFC3339"
            raise ValueError(msg) from exc
        if parsed.tzinfo is None:
            msg = f"Saxo {field} must include a timezone"
            raise ValueError(msg)
        return parsed.astimezone(UTC)

    @staticmethod
    def _required_decimal(value: Any, field: str) -> Decimal:
        if value in (None, "") or isinstance(value, bool):
            msg = f"{field} is required"
            raise TypeError(msg)
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            msg = f"{field} is not numeric"
            raise TypeError(msg) from exc
        if not parsed.is_finite():
            msg = f"{field} must be finite"
            raise ValueError(msg)
        return parsed

    @classmethod
    def _positive_price(cls, value: Any, field: str) -> Decimal:
        parsed = cls._required_decimal(value, field)
        if parsed <= 0:
            msg = f"{field} must be positive"
            raise ValueError(msg)
        return parsed

    @staticmethod
    def _positive_integer_identifier(value: Any, field: str) -> int:
        if isinstance(value, bool):
            msg = f"{field} must be a canonical positive integer"
            raise TypeError(msg)
        normalized = str(value or "").strip()
        if (
            not normalized
            or not normalized.isascii()
            or not normalized.isdigit()
            or normalized.startswith("0")
        ):
            msg = f"{field} must be a canonical positive integer"
            raise ValueError(msg)
        return int(normalized)

    @classmethod
    def _required_currency(cls, value: Any, field: str) -> str:
        currency = str(value or "").strip().upper()
        if (
            len(currency) < cls.MIN_CURRENCY_LENGTH
            or len(currency) > cls.MAX_CURRENCY_LENGTH
            or not currency.isalnum()
        ):
            msg = f"{field} is not a canonical currency code"
            raise ValueError(msg)
        return currency

    @staticmethod
    def _json_number(value: Decimal) -> int | float:
        return int(value) if value == value.to_integral_value() else float(value)

    @staticmethod
    def _collection_rows(payload: Mapping[str, Any], label: str) -> list[Mapping[str, Any]]:
        data = payload.get("Data")
        if not isinstance(data, list):
            msg = f"{label} response omitted a Data list"
            raise TypeError(msg)
        if any(not isinstance(row, Mapping) for row in data):
            msg = f"{label} response contained a non-object row"
            raise TypeError(msg)
        return cast(list[Mapping[str, Any]], data)

    @staticmethod
    def _response_error(payload: Mapping[str, Any]) -> tuple[str, str] | None:
        error = payload.get("ErrorInfo")
        if not error:
            return None
        if isinstance(error, Mapping):
            code = str(error.get("ErrorCode") or "saxo_error")
            message = str(error.get("Message") or code)
            return code, message
        return "saxo_error", str(error)

    def _http_order_error(
        self,
        exc: httpx.HTTPStatusError,
        *,
        stage: str,
        client_order_id: str,
    ) -> BrokerOrderResult:
        try:
            payload = exc.response.json()
        except ValueError:
            payload = {}
        response_payload = payload if isinstance(payload, Mapping) else {}
        error = self._response_error(response_payload)
        code, message = error or (
            f"http_{exc.response.status_code}",
            f"Saxo returned HTTP {exc.response.status_code}",
        )
        uncertain = (
            stage == "placement" and exc.response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
        )
        return {
            "success": False,
            "status": "pending" if uncertain else "rejected",
            "filled_quantity": "0",
            "client_order_id": client_order_id,
            "error_code": code,
            "error_message": message,
            "raw_response": dict(response_payload),
        }

    @classmethod
    def _order_activity_result(
        cls,
        row: Mapping[str, Any],
        *,
        fallback_order_id: str,
    ) -> BrokerOrderResult:
        status = str(row.get("Status", "")).strip()
        substatus = str(row.get("SubStatus", "")).strip()
        filled = cls._required_decimal(row.get("FilledAmount", 0), "Saxo FilledAmount")
        amount = cls._required_decimal(row.get("Amount"), "Saxo order Amount")
        normalized_status = cls._map_audit_status(
            status=status,
            substatus=substatus,
            filled=filled,
            amount=amount,
        )
        result: BrokerOrderResult = {
            "success": normalized_status != "rejected",
            "status": normalized_status,
            "broker_order_id": str(row.get("OrderId") or fallback_order_id),
            "client_order_id": str(row.get("ExternalReference", "")),
            "filled_quantity": str(filled),
            "timestamp": str(row.get("ActivityTime", "")),
            "raw_response": dict(row),
        }
        average_price = row.get("AveragePrice")
        if average_price not in (None, ""):
            result["filled_price"] = str(cls._required_decimal(average_price, "Saxo AveragePrice"))
        if normalized_status == "rejected":
            result["error_code"] = substatus or "saxo_order_rejected"
            result["error_message"] = f"Saxo order rejected: {substatus or status}"
        return result

    @staticmethod
    def _map_audit_status(
        *,
        status: str,
        substatus: str,
        filled: Decimal,
        amount: Decimal,
    ) -> str:
        normalized_status = status.lower()
        normalized_substatus = substatus.lower()
        if "reject" in normalized_substatus or "error" in normalized_substatus:
            return "rejected"
        if normalized_status == "finalfill":
            return "filled"
        if normalized_status == "fill":
            return "filled" if filled >= abs(amount) else "partially_filled"
        if normalized_status in {"cancelled", "expired", "doneforday"}:
            return "canceled"
        if normalized_status in {"placed", "working", "changed"}:
            return "working"
        msg = f"Unsupported Saxo order audit status {status!r}/{substatus!r}"
        raise ValueError(msg)

    @staticmethod
    def _map_open_order_status(value: Any) -> str:
        status = str(value or "").strip().lower()
        if status in {"working", "parked"}:
            return "working"
        if status in {"pending", "placed"}:
            return "submitted"
        msg = f"Unsupported Saxo open order status {value!r}"
        raise ValueError(msg)

    @staticmethod
    def _asset_class_for(asset_type: str) -> str:
        if asset_type == "Stock":
            return "equity"
        if asset_type == "FxSpot":
            return "fx"
        if asset_type == "ContractFutures":
            return "futures"
        if asset_type in {"FuturesOption", "StockIndexOption", "StockOption"}:
            return "options"
        msg = f"Unsupported Saxo AssetType {asset_type!r}"
        raise ValueError(msg)
