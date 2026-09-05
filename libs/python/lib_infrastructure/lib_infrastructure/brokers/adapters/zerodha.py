"""Zerodha Kite Connect adapter implementation.

This module provides integration with Zerodha, India's largest
stock broker, using the Kite Connect API.

Zerodha supports:
- Equities (NSE, BSE)
- Futures & Options (F&O)
- Currency derivatives
- Commodities (MCX)

Authentication:
- OAuth-based with access token
- Requires daily login token refresh
- API key + secret for initial authentication

Paper Trading:
- Zerodha doesn't provide paper trading
- Paper connections fail closed; the execution service owns the local PaperBroker

API Documentation:
- https://kite.trade/docs/connect/v3/
"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

import httpx

from lib_common.logging import get_logger
from lib_infrastructure.brokers.base import (
    BaseBrokerAdapter,
    RateLimitConfig,
    RetryConfig,
)
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


class ZerodhaAdapter(BaseBrokerAdapter):
    """Zerodha Kite Connect adapter for Indian markets.

    This adapter connects to Zerodha for trading on Indian exchanges
    (NSE, BSE, MCX).

    Supported Features:
    - Equity trading (NSE, BSE)
    - Futures & Options trading
    - Currency derivatives
    - Commodity trading (MCX)
    - GTT (Good Till Triggered) orders

    Usage:
        >>> adapter = ZerodhaAdapter(environment="live")
        >>> await adapter.connect(
        ...     api_key="your_api_key",
        ...     api_secret="your_api_secret",
        ...     access_token="daily_access_token",  # From login flow
        ... )
        >>> result = await adapter.place_order({
        ...     "symbol": "RELIANCE",
        ...     "side": "buy",
        ...     "quantity": "10",
        ...     "order_type": "limit",
        ...     "limit_price": "2500",
        ...     "exchange": "NSE",
        ... })

    Authentication:
        The control plane stores a current Kite access token in the linked
        account's encrypted credential. Kite requires an interactive login each
        trading day; the execution service never guesses, refreshes, or revokes
        that external session.

    Thread Safety:
        This adapter is thread-safe for concurrent order placement.

    Note:
        An expired access token fails connection closed. Rotate it through the
        tenant credential write surface after the account owner completes Kite
        login.
    """

    # API endpoints
    BASE_URL = "https://api.kite.trade"

    # Rate limits (Zerodha has 3 requests/second limit)
    DEFAULT_RATE_LIMIT = RateLimitConfig(
        requests_per_second=3,
        requests_per_minute=180,
        burst_limit=10,
    )

    DEFAULT_RETRY = RetryConfig(
        max_retries=3,
        base_delay=1.0,
        max_delay=10.0,
    )

    # Exchange codes
    EXCHANGES: ClassVar[dict[str, str]] = {
        "NSE": "NSE",
        "BSE": "BSE",
        "NFO": "NFO",  # NSE F&O
        "BFO": "BFO",  # BSE F&O
        "CDS": "CDS",  # Currency
        "MCX": "MCX",  # Commodities
    }

    def __init__(
        self,
        environment: str = "paper",
        rate_limit: RateLimitConfig | None = None,
        retry_config: RetryConfig | None = None,
    ) -> None:
        """Initialize Zerodha adapter.

        Args:
            environment: "paper" or "live"
            rate_limit: Rate limiting configuration
            retry_config: Retry configuration
        """
        super().__init__(
            environment=environment,
            rate_limit_config=rate_limit or self.DEFAULT_RATE_LIMIT,
            retry_config=retry_config or self.DEFAULT_RETRY,
        )

        self._api_key: str = ""
        self._api_secret: str = ""
        self._access_token: str = ""
        self._client: httpx.AsyncClient | None = None

    @property
    def broker_code(self) -> str:
        """Get broker identifier."""
        return "zerodha"

    @property
    def supported_asset_classes(self) -> list[str]:
        """Get supported asset classes."""
        return ["equity", "etf", "options", "futures", "fx"]

    @property
    def supported_execution_methods(self) -> list[BrokerExecutionMethod]:
        """Get supported execution methods."""
        return [
            BrokerExecutionMethod.SPOT,
            BrokerExecutionMethod.MARGIN,
            BrokerExecutionMethod.OPTIONS_SINGLE,
            BrokerExecutionMethod.FUTURES,
        ]

    @property
    def supports_paper_trading(self) -> bool:
        """Zerodha has no paper-trading endpoint."""
        return False

    @property
    def supports_multi_leg_orders(self) -> bool:
        """Zerodha doesn't support multi-leg orders natively."""
        return False

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            asset_classes=self.supported_asset_classes,
            execution_methods=["spot", "margin", "options_single", "futures"],
            multi_leg_orders=False,
            regions=["IN"],
            supported_order_types=["market", "limit", "stop", "stop_limit"],
        )

    # -------------------------------------------------------------------------
    # Connection Management
    # -------------------------------------------------------------------------

    async def _do_connect(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str | None = None,  # noqa: ARG002
        subaccount: str | None = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> bool:
        """Connect to Zerodha Kite.

        Args:
            api_key: Kite API key
            api_secret: Kite API secret
            passphrase: Not used
            subaccount: Not used
            **kwargs: Additional parameters
                - access_token: Current access token obtained after interactive
                  Kite login and stored in the account credential
                - access_token_expires_at: Timezone-aware RFC 3339 expiry

        Returns:
            True if connection successful
        """
        self._api_key = api_key
        self._api_secret = api_secret

        access_token = kwargs.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            logger.error("A current Zerodha access_token is required")
            return False
        access_token_expires_at = kwargs.get("access_token_expires_at")
        try:
            self._parse_access_token_expiry(access_token_expires_at)
        except ValueError:
            logger.exception("Zerodha access_token_expires_at is invalid or expired")
            return False
        self._access_token = access_token.strip()

        # Create HTTP client
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=30.0,
            headers={
                "X-Kite-Version": "3",
                "Authorization": f"token {self._api_key}:{self._access_token}",
            },
        )

        # Verify credentials
        try:
            response = await self._client.get("/user/profile")
            data = response.json()
        except Exception:
            logger.exception("Failed to connect to Zerodha")
            await self._close_failed_connection()
            return False
        else:
            if data.get("status") == "success":
                profile = data.get("data", {})
                logger.info(
                    "Connected to Zerodha",
                    user_id=profile.get("user_id"),
                    user_name=profile.get("user_name"),
                    environment=self.environment,
                )
                return True
            logger.error("Zerodha auth failed: %s", data.get("message"))
            await self._close_failed_connection()
            return False

    @staticmethod
    def _parse_access_token_expiry(value: Any) -> datetime:
        if not isinstance(value, str) or not value.strip():
            msg = "access token expiry is required"
            raise ValueError(msg)
        try:
            expiry = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            msg = "access token expiry must be RFC 3339"
            raise ValueError(msg) from exc
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            msg = "access token expiry must be timezone-aware"
            raise ValueError(msg)
        if expiry.astimezone(UTC) <= datetime.now(tz=UTC):
            msg = "access token has expired"
            raise ValueError(msg)
        return expiry

    async def _close_failed_connection(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _do_disconnect(self) -> None:
        """Close local transport without revoking the account's daily token."""
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("Disconnected from Zerodha")

    # -------------------------------------------------------------------------
    # Order Management
    # -------------------------------------------------------------------------

    async def _do_place_order(self, order: BrokerOrderIntent) -> BrokerOrderResult:
        """Place order on Zerodha.

        Args:
            order: Order intent

        Returns:
            Order result
        """
        if not self._client:
            return {
                "success": False,
                "status": "rejected",
                "filled_quantity": "0",
                "error_message": "Not connected",
            }

        try:
            # Build order payload
            symbol = order["symbol"]
            exchange = str(order.get("exchange", "NSE"))

            # Determine trading symbol format
            trading_symbol = self._format_trading_symbol(
                symbol=symbol,
                exchange=exchange,
                option_type=str(order.get("option_type", "")) or None,
                strike=str(order.get("strike", "")) or None,
                expiry=str(order.get("expiry", "")) or None,
            )

            payload = {
                "exchange": exchange,
                "tradingsymbol": trading_symbol,
                "transaction_type": "BUY" if order["side"] == "buy" else "SELL",
                "quantity": int(float(order["quantity"])),
                "order_type": self._map_order_type(order.get("order_type", "market")),
                "product": self._get_product_type(order),
                "validity": self._map_validity(order.get("time_in_force", "day")),
            }

            # Add price for limit orders
            limit_price = order.get("limit_price")
            if limit_price is not None:
                payload["price"] = limit_price

            # Add trigger price for stop orders
            stop_price = order.get("stop_price")
            if stop_price is not None:
                payload["trigger_price"] = stop_price

            # Add disclosed quantity if specified (from extra fields)
            extra = order.get("extra", {})
            if isinstance(extra, dict) and extra.get("disclosed_quantity"):
                payload["disclosed_quantity"] = int(str(extra["disclosed_quantity"]))

            # Add tag for tracking
            if order.get("client_order_id"):
                payload["tag"] = order["client_order_id"][:20]  # Max 20 chars

            # Place order
            response = await self._client.post(
                "/orders/regular",
                data=payload,
            )
            data = response.json()

            if data.get("status") == "success":
                order_id = data.get("data", {}).get("order_id")
                return {
                    "success": True,
                    "status": "submitted",
                    "broker_order_id": str(order_id),
                    "filled_quantity": "0",
                }
            return {
                "success": False,
                "status": "rejected",
                "filled_quantity": "0",
                "error_message": data.get("message", "Unknown error"),
            }

        except Exception as e:
            logger.exception("Failed to place Zerodha order")
            return {
                "success": False,
                "status": "rejected",
                "filled_quantity": "0",
                "error_message": str(e),
            }

    async def _do_cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an order.

        Args:
            broker_order_id: Zerodha order ID

        Returns:
            True if cancelled
        """
        if not self._client:
            return False

        try:
            response = await self._client.delete(f"/orders/regular/{broker_order_id}")
            data = response.json()
        except Exception:
            logger.exception("Failed to cancel order")
            return False
        else:
            return bool(data.get("status") == "success")

    async def _do_get_order_status(
        self,
        broker_order_id: str,
    ) -> BrokerOrderResult:
        """Get order status.

        Args:
            broker_order_id: Zerodha order ID

        Returns:
            Order result with current status
        """
        if not self._client:
            return {
                "success": False,
                "status": "unknown",
                "filled_quantity": "0",
                "error_message": "Not connected",
            }

        try:
            response = await self._client.get(f"/orders/{broker_order_id}")
            data = response.json()
        except Exception as e:
            logger.exception("Failed to get order status")
            return {
                "success": False,
                "status": "unknown",
                "filled_quantity": "0",
                "error_message": str(e),
            }
        else:
            if data.get("status") == "success":
                order_data = data.get("data", [{}])[0]
                return {
                    "success": True,
                    "status": self._map_order_status(order_data.get("status", "")),
                    "broker_order_id": broker_order_id,
                    "filled_quantity": str(order_data.get("filled_quantity", 0)),
                    "filled_price": str(order_data.get("average_price", "")),
                }
            return {
                "success": False,
                "status": "unknown",
                "filled_quantity": "0",
                "error_message": data.get("message", ""),
            }

    # -------------------------------------------------------------------------
    # Account Information
    # -------------------------------------------------------------------------

    async def _do_get_account_balance(self) -> list[AccountBalance]:
        """Get account balances.

        Returns:
            List of balances
        """
        if not self._client:
            return []

        try:
            response = await self._client.get("/user/margins")
            data = response.json()
        except Exception:
            logger.exception("Failed to get balance")
            return []
        else:
            if data.get("status") != "success":
                return []

            balances: list[AccountBalance] = []
            margins = data.get("data", {})

            # Equity segment
            if "equity" in margins:
                equity = margins["equity"]
                balances.append(
                    AccountBalance(
                        currency="INR",
                        total=str(equity.get("net", 0)),
                        available=str(equity.get("available", {}).get("cash", 0)),
                        locked=str(equity.get("utilised", {}).get("debits", 0)),
                    )
                )

            # Commodity segment
            if "commodity" in margins:
                commodity = margins["commodity"]
                balances.append(
                    AccountBalance(
                        currency="INR",
                        total=str(commodity.get("net", 0)),
                        available=str(commodity.get("available", {}).get("cash", 0)),
                        locked=str(commodity.get("utilised", {}).get("debits", 0)),
                    )
                )

            return balances

    async def _do_get_account_snapshot(self) -> BrokerAccountSnapshot:
        """Return Zerodha inputs without treating margin funds as equity.

        Kite's margins response documents ``net`` as available trading funds,
        not portfolio net-liquidation value. Gross positions must therefore not
        be added to it to manufacture equity.
        """
        return BrokerAccountSnapshot(
            balances=await self._do_get_account_balance(),
            positions=await self._do_get_positions(),
            equity=[],
            available_cash=[],
            margin_used=[],
            equity_source="unavailable",
        )

    async def _do_get_open_orders(self, symbol: str | None = None) -> list[BrokerOrderResult]:
        """Get open (non-terminal) orders from Zerodha (Kite GET /orders).

        Mirrors _do_get_order_status: fetch the order book and map each
        still-open order to a BrokerOrderResult via _map_order_status.
        """
        if not self._client:
            return []

        try:
            response = await self._client.get("/orders")
            data = response.json()
        except Exception:
            logger.exception("Failed to get open orders")
            return []

        if data.get("status") != "success":
            return []

        terminal = {"filled", "rejected", "cancelled"}
        results: list[BrokerOrderResult] = []
        for order in data.get("data", []) or []:
            if symbol and order.get("tradingsymbol") != symbol:
                continue
            mapped = self._map_order_status(order.get("status", ""))
            if mapped in terminal:
                continue
            results.append(
                {
                    "success": True,
                    "status": mapped,
                    "broker_order_id": str(order.get("order_id", "")),
                    "filled_quantity": str(order.get("filled_quantity", 0)),
                    "filled_price": str(order.get("average_price", "")),
                }
            )
        return results

    async def _do_get_positions(self) -> list[BrokerPosition]:
        """Get current positions.

        Returns:
            List of positions
        """
        if not self._client:
            return []

        response = await self._client.get("/portfolio/positions")
        data = response.json()
        if not isinstance(data, dict) or data.get("status") != "success":
            msg = "Zerodha positions request failed"
            raise RuntimeError(msg)
        payload = data.get("data")
        if not isinstance(payload, dict) or not isinstance(payload.get("net"), list):
            msg = "Zerodha positions response did not contain a net position list"
            raise TypeError(msg)

        positions: list[BrokerPosition] = []
        for pos in payload["net"]:
            if not isinstance(pos, dict):
                msg = "Zerodha positions response contained a non-object row"
                raise TypeError(msg)
            quantity = int(pos.get("quantity", 0))
            if quantity != 0:
                symbol = str(pos.get("tradingsymbol") or "")
                multiplier = self._required_decimal(
                    pos,
                    "multiplier",
                    context=f"position {symbol}",
                )
                current_price = self._required_decimal(
                    pos,
                    "last_price",
                    context=f"position {symbol}",
                )
                entry_price = self._required_decimal(
                    pos,
                    "average_price",
                    context=f"position {symbol}",
                )
                if multiplier <= 0 or current_price <= 0 or entry_price <= 0:
                    msg = f"Zerodha position {symbol} has non-positive valuation fields"
                    raise RuntimeError(msg)
                if "unrealised" not in pos or "realised" not in pos:
                    msg = f"Zerodha position {symbol} omitted P&L fields"
                    raise RuntimeError(msg)
                gross_notional = abs(Decimal(quantity)) * multiplier * current_price
                positions.append(
                    BrokerPosition(
                        symbol=symbol,
                        exchange=str(pos.get("exchange") or ""),
                        quantity=str(quantity),
                        quantity_unit="contracts",
                        side="long" if quantity > 0 else "short",
                        entry_price=str(entry_price),
                        current_price=str(current_price),
                        unrealized_pnl=str(
                            self._required_decimal(
                                pos,
                                "unrealised",
                                context=f"position {symbol}",
                            )
                        ),
                        realized_pnl=str(
                            self._required_decimal(
                                pos,
                                "realised",
                                context=f"position {symbol}",
                            )
                        ),
                        product=str(pos.get("product") or ""),
                        currency="INR",
                        quote_currency="INR",
                        settlement_currency="INR",
                        pnl_currency="INR",
                        notional_currency="INR",
                        gross_notional=str(gross_notional),
                        contract_multiplier=str(multiplier),
                        notional_type="linear",
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
        """Parse one required finite Kite numeric field."""
        if field not in payload or payload[field] in (None, ""):
            msg = f"Zerodha {context} omitted {field}"
            raise RuntimeError(msg)
        try:
            value = Decimal(str(payload[field]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            msg = f"Zerodha {context} returned invalid {field}"
            raise RuntimeError(msg) from exc
        if not value.is_finite():
            msg = f"Zerodha {context} returned non-finite {field}"
            raise RuntimeError(msg)
        return value

    async def get_holdings(self) -> list[dict[str, Any]]:
        """Get equity holdings (long-term positions).

        Returns:
            List of holdings
        """
        if not self._client:
            return []

        try:
            response = await self._client.get("/portfolio/holdings")
            data = response.json()
        except Exception:
            logger.exception("Failed to get holdings")
            return []
        else:
            if data.get("status") != "success":
                return []

            holdings = []
            for holding in data.get("data", []):
                holdings.append(
                    {
                        "symbol": holding.get("tradingsymbol", ""),
                        "exchange": holding.get("exchange", ""),
                        "isin": holding.get("isin", ""),
                        "quantity": str(holding.get("quantity", 0)),
                        "average_price": str(holding.get("average_price", 0)),
                        "last_price": str(holding.get("last_price", 0)),
                        "pnl": str(holding.get("pnl", 0)),
                    }
                )

            return holdings

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    def _format_trading_symbol(
        self,
        symbol: str,
        exchange: str,
        option_type: str | None = None,
        strike: str | None = None,
        expiry: str | None = None,
    ) -> str:
        """Format trading symbol for Zerodha.

        Zerodha uses specific formats:
        - Equity: RELIANCE, INFY
        - Futures: NIFTY23NOVFUT
        - Options: NIFTY2311019500CE

        Args:
            symbol: Base symbol
            exchange: Exchange code
            option_type: "call" or "put" for options
            strike: Strike price for options
            expiry: Expiry date for derivatives

        Returns:
            Formatted trading symbol
        """
        # Remove any "/" from symbol
        symbol = symbol.replace("/", "").upper()

        if exchange in ("NSE", "BSE"):
            # Plain equity
            return symbol

        if exchange == "NFO":
            if option_type and strike and expiry:
                # Option: NIFTY2311019500CE
                opt_suffix = "CE" if option_type.lower() == "call" else "PE"
                return f"{symbol}{expiry}{strike}{opt_suffix}"
            if expiry:
                # Future: NIFTY23NOVFUT
                return f"{symbol}{expiry}FUT"

        return symbol

    def _map_order_type(self, order_type: str) -> str:
        """Map order type to Zerodha format."""
        mapping = {
            "market": "MARKET",
            "limit": "LIMIT",
            "stop": "SL-M",  # Stop-loss market
            "stop_limit": "SL",  # Stop-loss limit
        }
        return mapping.get(order_type, "MARKET")

    def _map_validity(self, time_in_force: str) -> str:
        """Map time in force to Zerodha validity."""
        mapping = {
            "day": "DAY",
            "ioc": "IOC",
            "gtc": "DAY",  # Zerodha doesn't support GTC for regular orders
        }
        return mapping.get(time_in_force, "DAY")

    def _map_order_status(self, status: str) -> str:
        """Map Zerodha order status to standard status."""
        mapping = {
            "COMPLETE": "filled",
            "REJECTED": "rejected",
            "CANCELLED": "cancelled",
            "OPEN": "submitted",
            "PENDING": "pending",
            "TRIGGER PENDING": "pending",
        }
        return mapping.get(status.upper(), "unknown")

    def _get_product_type(self, order: BrokerOrderIntent) -> str:
        """Get Zerodha product type.

        Product types:
        - CNC: Cash and Carry (equity delivery)
        - MIS: Margin Intraday Square off
        - NRML: Normal (F&O overnight)

        Args:
            order: Order intent

        Returns:
            Product type code
        """
        execution_method = order.get("execution_method", "spot")

        if execution_method == "spot":
            # Delivery for equity
            return "CNC"
        if execution_method == "margin":
            # Intraday
            return "MIS"
        # F&O, futures, options
        return "NRML"
