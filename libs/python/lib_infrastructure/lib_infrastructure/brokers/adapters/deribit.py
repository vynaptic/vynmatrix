"""Deribit API adapter.

This adapter implements the IBrokerAdapter interface for Deribit,
a cryptocurrency derivatives exchange specializing in BTC and ETH
options and perpetual futures.

Features:
- Crypto options trading (BTC, ETH options)
- Perpetual futures
- Multi-leg order support for spreads
- Testnet environment for paper trading

API Documentation:
    https://docs.deribit.com/

Note: This adapter follows patterns similar to QuantConnect LEAN's
brokerage implementations but in async Python for our use case.
"""

import contextlib
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import urlencode

import httpx

from lib_common.logging import get_logger
from lib_infrastructure.brokers.base import BaseBrokerAdapter
from lib_strategy.types import (
    AccountBalance,
    BrokerAccountSnapshot,
    BrokerCapabilities,
    BrokerExecutionMethod,
    BrokerFill,
    BrokerMoney,
    BrokerOrderIntent,
    BrokerOrderResult,
    BrokerPosition,
)

logger = get_logger(__name__)


class DeribitAdapter(BaseBrokerAdapter):
    """Deribit API adapter for crypto options and perpetuals.

    Supports:
    - BTC and ETH options
    - Perpetual futures
    - Multi-leg combo orders (spreads)
    - Testnet for paper trading

    Authentication:
    - Client ID + Client Secret
    - OAuth2 token-based authentication

    Example:
        >>> adapter = DeribitAdapter(environment="paper")
        >>> await adapter.connect(
        ...     api_key="your_client_id",
        ...     api_secret="your_client_secret"
        ... )
        >>> # Place an options order
        >>> result = await adapter.place_order({
        ...     "symbol": "BTC-27DEC24-100000-C",
        ...     "side": "buy",
        ...     "quantity": "1",
        ...     "order_type": "limit",
        ...     "limit_price": "0.05",
        ...     "execution_method": "options_single"
        ... })
    """

    # API endpoints
    LIVE_BASE_URL = "https://www.deribit.com"
    TESTNET_BASE_URL = "https://test.deribit.com"

    # API paths
    API_VERSION = "/api/v2"

    def __init__(self, environment: str = "paper") -> None:
        """Initialize Deribit adapter.

        Args:
            environment: "paper" for testnet, "live" for production
        """
        super().__init__(environment=environment)

        # Connection state
        self._client_id: str | None = None
        self._client_secret: str | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry: float = 0
        self._client: httpx.AsyncClient | None = None
        self._subaccount: str | None = None

        # Determine base URL
        self._base_url = self.TESTNET_BASE_URL if environment == "paper" else self.LIVE_BASE_URL

    # =========================================================================
    # IBrokerAdapter properties
    # =========================================================================

    @property
    def broker_code(self) -> str:
        return "deribit"

    @property
    def supported_asset_classes(self) -> list[str]:
        return ["crypto"]

    @property
    def supported_execution_methods(self) -> list[BrokerExecutionMethod]:
        return [
            BrokerExecutionMethod.PERPETUAL,
            BrokerExecutionMethod.FUTURES,
            BrokerExecutionMethod.OPTIONS_SINGLE,
            BrokerExecutionMethod.OPTIONS_STRATEGY,
        ]

    @property
    def supports_paper_trading(self) -> bool:
        return True  # Deribit has testnet

    @property
    def supports_multi_leg_orders(self) -> bool:
        return True  # Deribit supports combo orders

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            asset_classes=["crypto"],
            execution_methods=[
                "perpetual",
                "futures",
                "options_single",
                "options_strategy",
            ],
            multi_leg_orders=True,
            paper_trading={
                "type": "testnet",
                "base_url": self.TESTNET_BASE_URL,
            },
            regions=["GLOBAL"],
            supported_order_types=["market", "limit", "stop_limit"],
            exact_fill_retrieval=True,
        )

    # =========================================================================
    # Connection methods
    # =========================================================================

    async def _do_connect(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str | None = None,  # noqa: ARG002
        subaccount: str | None = None,
        **kwargs: str,  # noqa: ARG002
    ) -> bool:
        """Connect to Deribit API using OAuth2."""
        self._client_id = api_key
        self._client_secret = api_secret
        self._subaccount = subaccount

        # Create HTTP client
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=30.0,
        )

        # Authenticate
        try:
            await self._authenticate()
        except Exception:
            logger.exception("Failed to connect to Deribit")
            await self._do_disconnect()
            return False
        else:
            logger.info("Connected to Deribit (%s)", self.environment)
            return True

    async def _authenticate(self) -> None:
        """Authenticate with Deribit using client credentials."""
        params = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }

        response = await self._public_request(
            "public/auth",
            params=params,
        )

        result = response.get("result", {})
        self._access_token = result.get("access_token")
        self._refresh_token = result.get("refresh_token")
        expires_in = result.get("expires_in", 900)
        self._token_expiry = time.time() + expires_in - 60  # Refresh 1 min before expiry

        if not self._access_token:
            msg = "Failed to obtain access token"
            raise ValueError(msg)

    async def _ensure_authenticated(self) -> None:
        """Ensure we have a valid access token, refreshing if needed."""
        if time.time() >= self._token_expiry:
            await self._refresh_access_token()

    async def _refresh_access_token(self) -> None:
        """Refresh the access token."""
        if self._refresh_token:
            params = {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            }
        else:
            # Re-authenticate
            params = {
                "grant_type": "client_credentials",
                "client_id": self._client_id or "",
                "client_secret": self._client_secret or "",
            }

        response = await self._public_request("public/auth", params=params)
        result = response.get("result", {})
        self._access_token = result.get("access_token")
        self._refresh_token = result.get("refresh_token")
        expires_in = result.get("expires_in", 900)
        self._token_expiry = time.time() + expires_in - 60

    async def _do_disconnect(self) -> None:
        """Disconnect from Deribit API."""
        if self._client and self._access_token:
            with contextlib.suppress(Exception):
                await self._private_request("private/logout")

        if self._client:
            await self._client.aclose()
            self._client = None

        self._access_token = None
        self._refresh_token = None

    # =========================================================================
    # Trading methods
    # =========================================================================

    async def _do_place_order(self, order: BrokerOrderIntent) -> BrokerOrderResult:
        """Place order on Deribit."""
        try:
            await self._ensure_authenticated()

            instrument_name = order["symbol"]
            side = order["side"].lower()
            quantity = order["quantity"]
            order_type = order["order_type"].lower()

            # Build order params
            params: dict[str, Any] = {
                "instrument_name": instrument_name,
                "amount": quantity,
                "type": self._map_order_type(order_type),
            }

            # Add label (client order ID)
            if order.get("client_order_id"):
                params["label"] = order["client_order_id"]

            # Add price for limit orders
            if order_type in ("limit", "stop_limit"):
                params["price"] = order.get("limit_price", "0")

            # Add trigger price for stop orders
            if order_type == "stop_limit":
                params["trigger_price"] = order.get("stop_price", "0")
                params["trigger"] = "last_price"

            # Time in force
            tif = order.get("time_in_force", "gtc").lower()
            if tif == "gtc":
                params["time_in_force"] = "good_til_cancelled"
            elif tif == "ioc":
                params["time_in_force"] = "immediate_or_cancel"
            elif tif == "fok":
                params["time_in_force"] = "fill_or_kill"

            # Determine endpoint based on side
            endpoint = f"private/{side}"

            response = await self._private_request(endpoint, params=params)
            api_result = response.get("result", {})
            order_data = api_result.get("order", {})

            order_result: BrokerOrderResult = {
                "success": True,
                "broker_order_id": order_data.get("order_id"),
                "client_order_id": order_data.get("label"),
                "status": self._map_order_status(order_data.get("order_state", "")),
                "filled_quantity": str(order_data.get("filled_amount", 0)),
                "commission": str(order_data.get("commission", 0)),
                "timestamp": datetime.now(UTC).isoformat(),
                "raw_response": response,
            }
            if order_data.get("average_price"):
                order_result["filled_price"] = str(order_data.get("average_price"))
        except Exception as e:
            logger.exception("Error placing order on Deribit")
            return self._error_result(str(e))
        else:
            return order_result

    async def place_multi_leg_order(self, legs: list[BrokerOrderIntent]) -> BrokerOrderResult:
        """Place multi-leg combo order on Deribit.

        Deribit supports combo orders for spreads. This sends all legs
        as a single atomic order.
        """
        combo_leg_count = 2  # Deribit combo orders require exactly 2 legs
        try:
            await self._ensure_authenticated()

            # Build combo order
            # Deribit combo format: "leg1_instrument,leg2_instrument"
            # with amounts specified for each leg
            if len(legs) != combo_leg_count:
                return self._error_result("Deribit combo orders require exactly 2 legs")

            leg1, leg2 = legs[0], legs[1]

            # Create combo instrument name
            combo_name = f"{leg1['symbol']},{leg2['symbol']}"

            # For a spread, we typically buy one leg and sell another
            # The amount is the same for both legs in a standard spread
            params = {
                "instrument_name": combo_name,
                "amount": leg1["quantity"],
                "type": "limit",
                "price": leg1.get("limit_price", "0"),
            }

            # Determine side based on first leg
            side = leg1["side"].lower()
            endpoint = f"private/{side}"

            response = await self._private_request(endpoint, params=params)
            api_result = response.get("result", {})
            order_data = api_result.get("order", {})

            combo_result: BrokerOrderResult = {
                "success": True,
                "broker_order_id": order_data.get("order_id"),
                "status": self._map_order_status(order_data.get("order_state", "")),
                "filled_quantity": str(order_data.get("filled_amount", 0)),
                "timestamp": datetime.now(UTC).isoformat(),
                "raw_response": response,
            }
            if order_data.get("average_price"):
                combo_result["filled_price"] = str(order_data.get("average_price"))
        except Exception as e:
            logger.exception("Error placing combo order")
            return self._error_result(str(e))
        else:
            return combo_result

    async def _do_cancel_order(self, broker_order_id: str) -> bool:
        """Cancel order on Deribit."""
        try:
            await self._ensure_authenticated()

            response = await self._private_request(
                "private/cancel",
                params={"order_id": broker_order_id},
            )
        except Exception:
            logger.exception("Error canceling order")
            return False
        else:
            cancel_result = response.get("result", {})
            return bool(cancel_result.get("order_state") == "cancelled")

    async def _do_get_order_status(self, broker_order_id: str) -> BrokerOrderResult:
        """Get order status from Deribit."""
        try:
            await self._ensure_authenticated()

            response = await self._private_request(
                "private/get_order_state",
                params={"order_id": broker_order_id},
            )

            result = response.get("result", {})

            status_result: BrokerOrderResult = {
                "success": True,
                "broker_order_id": result.get("order_id"),
                "client_order_id": result.get("label"),
                "status": self._map_order_status(result.get("order_state", "")),
                "filled_quantity": str(result.get("filled_amount", 0)),
                "raw_response": response,
            }
            if result.get("average_price"):
                status_result["filled_price"] = str(result.get("average_price"))
        except Exception as e:
            return self._error_result(str(e))
        else:
            return status_result

    async def _do_get_fills(self, broker_order_id: str) -> list[BrokerFill]:
        """Return exact Deribit trades generated by one order."""
        normalized_order_id = str(broker_order_id or "").strip()
        if not normalized_order_id:
            msg = "Deribit fill retrieval requires broker_order_id"
            raise ValueError(msg)
        await self._ensure_authenticated()
        response = await self._private_request(
            "private/get_user_trades_by_order",
            params={"order_id": normalized_order_id, "historical": True},
        )
        rows = response.get("result")
        if not isinstance(rows, list):
            msg = "Deribit trades response was not a list"
            raise TypeError(msg)

        fills: list[BrokerFill] = []
        for row in rows:
            if not isinstance(row, dict):
                msg = "Deribit trades response contained a non-object row"
                raise TypeError(msg)
            order_id = str(row.get("order_id") or "").strip()
            if order_id != normalized_order_id:
                msg = "Deribit trades response crossed the requested order boundary"
                raise RuntimeError(msg)
            trade_id = str(row.get("trade_id") or "").strip()
            symbol = str(row.get("instrument_name") or "").strip()
            side = str(row.get("direction") or "").strip().lower()
            fee_currency = str(row.get("fee_currency") or "").strip().upper()
            if not trade_id or not symbol or side not in {"buy", "sell"} or not fee_currency:
                msg = "Deribit trade omitted stable identity, instrument, side, or fee currency"
                raise RuntimeError(msg)
            quantity = self._required_decimal(row, "amount", context=f"trade {trade_id}")
            price = self._required_decimal(row, "price", context=f"trade {trade_id}")
            fee = self._required_decimal(row, "fee", context=f"trade {trade_id}")
            if quantity <= 0 or price <= 0:
                msg = f"Deribit trade {trade_id} returned invalid fill economics"
                raise RuntimeError(msg)
            timestamp_raw = row.get("timestamp")
            if (
                isinstance(timestamp_raw, bool)
                or not isinstance(timestamp_raw, (int, float))
                or timestamp_raw <= 0
            ):
                msg = f"Deribit trade {trade_id} returned invalid timestamp"
                raise RuntimeError(msg)
            timestamp = datetime.fromtimestamp(timestamp_raw / 1000, tz=UTC).isoformat()
            fills.append(
                BrokerFill(
                    trade_id=trade_id,
                    broker_order_id=order_id,
                    symbol=symbol,
                    side=cast(Any, side),
                    quantity=format(quantity, "f"),
                    price=format(price, "f"),
                    fee_amount=format(fee, "f"),
                    fee_currency=fee_currency,
                    timestamp=timestamp,
                    venue=self.broker_code,
                    raw_response=dict(row),
                )
            )
        return fills

    async def _do_get_account_balance(self) -> list[AccountBalance]:
        """Get account balances from Deribit."""
        await self._ensure_authenticated()

        balances: list[AccountBalance] = []

        # Get balance for each currency (BTC and ETH)
        for currency in ["BTC", "ETH"]:
            response = await self._private_request(
                "private/get_account_summary",
                params={"currency": currency, "extended": True},
            )
            result = response.get("result")
            if not isinstance(result, dict):
                msg = f"Deribit {currency} account summary was not an object"
                raise TypeError(msg)
            balances.append(
                AccountBalance(
                    currency=currency,
                    total=str(
                        self._required_decimal(
                            result,
                            "equity",
                            context=f"{currency} account summary",
                        )
                    ),
                    available=str(
                        self._required_decimal(
                            result,
                            "available_funds",
                            context=f"{currency} account summary",
                        )
                    ),
                    locked=str(
                        self._required_decimal(
                            result,
                            "initial_margin",
                            context=f"{currency} account summary",
                        )
                    ),
                )
            )

        return balances

    async def _do_get_account_snapshot(self) -> BrokerAccountSnapshot:
        """Return Deribit's documented per-currency equity summary."""
        balances = await self._do_get_account_balance()
        return BrokerAccountSnapshot(
            balances=balances,
            positions=await self._do_get_positions(),
            equity=[
                BrokerMoney(value=balance["total"], currency=balance["currency"])
                for balance in balances
            ],
            available_cash=[
                BrokerMoney(value=balance["available"], currency=balance["currency"])
                for balance in balances
            ],
            margin_used=[
                BrokerMoney(value=balance["locked"], currency=balance["currency"])
                for balance in balances
            ],
            equity_source="broker",
        )

    async def _do_get_open_orders(self, symbol: str | None = None) -> list[BrokerOrderResult]:
        """Get open orders from Deribit."""
        await self._ensure_authenticated()

        results = []

        # Get orders for each currency
        for currency in ["BTC", "ETH"]:
            try:
                params: dict[str, Any] = {"currency": currency}
                if symbol:
                    params["instrument_name"] = symbol

                response = await self._private_request(
                    "private/get_open_orders_by_currency",
                    params=params,
                )

                for order_data in response.get("result", []):
                    order_result: BrokerOrderResult = {
                        "success": True,
                        "broker_order_id": order_data.get("order_id"),
                        "client_order_id": order_data.get("label"),
                        "status": self._map_order_status(order_data.get("order_state", "")),
                        "filled_quantity": str(order_data.get("filled_amount", 0)),
                    }
                    if order_data.get("average_price"):
                        order_result["filled_price"] = str(order_data.get("average_price"))
                    results.append(order_result)

            except Exception as e:
                logger.warning("Failed to get %s orders: %s", currency, e)

        return results

    async def _do_get_positions(self) -> list[BrokerPosition]:
        """Get positions from Deribit."""
        await self._ensure_authenticated()

        positions: list[BrokerPosition] = []

        for currency in ["BTC", "ETH"]:
            response = await self._private_request(
                "private/get_positions",
                params={"currency": currency},
            )
            result = response.get("result")
            if not isinstance(result, list):
                msg = f"Deribit {currency} positions response was not a list"
                raise TypeError(msg)
            for pos_data in result:
                size = self._required_decimal(
                    pos_data,
                    "size",
                    context=f"{currency} position",
                )
                if size == 0:
                    continue
                kind = str(pos_data.get("kind") or "")
                symbol = str(pos_data.get("instrument_name") or "")
                if kind != "future":
                    msg = f"Deribit position {symbol} has unsupported valuation kind {kind!r}"
                    raise RuntimeError(msg)
                mark_price = self._required_decimal(
                    pos_data,
                    "mark_price",
                    context=f"position {symbol}",
                )
                entry_price = self._required_decimal(
                    pos_data,
                    "average_price",
                    context=f"position {symbol}",
                )
                if mark_price <= 0 or entry_price <= 0:
                    msg = f"Deribit position {symbol} returned invalid prices"
                    raise RuntimeError(msg)
                positions.append(
                    BrokerPosition(
                        symbol=symbol,
                        quantity=str(abs(size)),
                        quantity_unit="quote_notional",
                        side="long" if size > 0 else "short",
                        entry_price=str(entry_price),
                        mark_price=str(mark_price),
                        unrealized_pnl=str(
                            self._required_decimal(
                                pos_data,
                                "floating_profit_loss",
                                context=f"position {symbol}",
                            )
                        ),
                        realized_pnl=str(
                            self._required_decimal(
                                pos_data,
                                "realized_profit_loss",
                                context=f"position {symbol}",
                            )
                        ),
                        margin_used=str(
                            self._required_decimal(
                                pos_data,
                                "initial_margin",
                                context=f"position {symbol}",
                            )
                        ),
                        liquidation_price=str(pos_data.get("estimated_liquidation_price") or ""),
                        asset_currency=currency,
                        quote_currency="USD",
                        settlement_currency=currency,
                        pnl_currency=currency,
                        notional_currency="USD",
                        gross_notional=str(abs(size)),
                        notional_type="broker_notional",
                        contract_type="future",
                        is_quanto=False,
                    )
                )

        return positions

    @staticmethod
    def _required_decimal(
        payload: dict[str, Any],
        field: str,
        *,
        context: str,
    ) -> Decimal:
        """Parse one required finite Deribit numeric field."""
        if field not in payload or payload[field] in (None, ""):
            msg = f"Deribit {context} omitted {field}"
            raise RuntimeError(msg)
        try:
            value = Decimal(str(payload[field]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            msg = f"Deribit {context} returned invalid {field}"
            raise RuntimeError(msg) from exc
        if not value.is_finite():
            msg = f"Deribit {context} returned non-finite {field}"
            raise RuntimeError(msg)
        return value

    # =========================================================================
    # HTTP request methods
    # =========================================================================

    async def _public_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make public (unauthenticated) request."""
        if not self._client:
            msg = "Not connected to Deribit"
            raise ConnectionError(msg)

        url = f"{self.API_VERSION}/{method}"
        if params:
            url = f"{url}?{urlencode(params)}"

        response = await self._client.get(url)
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    async def _private_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make private (authenticated) request."""
        if not self._client:
            msg = "Not connected to Deribit"
            raise ConnectionError(msg)

        if not self._access_token:
            msg = "Not authenticated with Deribit"
            raise ConnectionError(msg)

        url = f"{self.API_VERSION}/{method}"
        if params:
            url = f"{url}?{urlencode(params)}"

        headers = {"Authorization": f"Bearer {self._access_token}"}

        response = await self._client.get(url, headers=headers)

        http_unauthorized = 401
        if response.status_code == http_unauthorized:
            # Token expired, try to refresh
            await self._refresh_access_token()
            headers = {"Authorization": f"Bearer {self._access_token}"}
            response = await self._client.get(url, headers=headers)

        response.raise_for_status()
        data = cast(dict[str, Any], response.json())

        # Check for Deribit error
        if "error" in data:
            error = data["error"]
            error_msg = f"Deribit API error: {error.get('message', 'Unknown error')}"
            raise httpx.HTTPStatusError(
                error_msg,
                request=response.request,
                response=response,
            )

        return data

    # =========================================================================
    # Helper methods
    # =========================================================================

    def _map_order_type(self, order_type: str) -> str:
        """Map our order type to Deribit format."""
        type_map = {
            "market": "market",
            "limit": "limit",
            "stop": "stop_market",
            "stop_limit": "stop_limit",
        }
        return type_map.get(order_type, order_type)

    def _map_order_status(self, deribit_status: str) -> str:
        """Map Deribit order status to our standard status."""
        status_map = {
            "open": "working",
            "filled": "filled",
            "rejected": "rejected",
            "cancelled": "canceled",
            "untriggered": "new",
        }
        return status_map.get(deribit_status.lower(), deribit_status.lower())
