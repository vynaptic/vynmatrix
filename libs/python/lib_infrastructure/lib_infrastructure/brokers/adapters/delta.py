"""Delta Exchange adapter implementation.

This module provides integration with Delta Exchange for crypto
derivatives trading, popular in India and Asia.

Delta Exchange supports:
- Perpetual futures (BTC, ETH, SOL, etc.)
- Options on crypto
- Inverse and linear contracts

Authentication:
- API key + secret with HMAC-SHA256 signing
- Similar to Binance/FTX style authentication

Paper Trading:
- Testnet available at testnet.delta.exchange

API Documentation:
- https://docs.delta.exchange/
"""

import hashlib
import hmac
import json as json_module
import time
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, cast
from urllib.parse import urlencode

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


class DeltaExchangeAdapter(BaseBrokerAdapter):
    """Delta Exchange adapter for crypto derivatives.

    This adapter connects to Delta Exchange for trading crypto
    perpetual futures and options.

    Supported Features:
    - Perpetual futures trading
    - Options trading
    - Leverage up to 100x
    - USD-margined contracts

    Usage:
        >>> adapter = DeltaExchangeAdapter(environment="paper")
        >>> await adapter.connect(
        ...     api_key="your_api_key",
        ...     api_secret="your_api_secret",
        ... )
        >>> result = await adapter.place_order({
        ...     "symbol": "BTC/USD",
        ...     "side": "buy",
        ...     "quantity": "100",  # Contracts
        ...     "order_type": "limit",
        ...     "limit_price": "45000",
        ... })

    Thread Safety:
        This adapter is thread-safe for concurrent order placement.
    """

    # API endpoints
    LIVE_BASE_URL = "https://api.delta.exchange"
    TESTNET_BASE_URL = "https://testnet-api.delta.exchange"
    INDIA_LIVE_BASE_URL = "https://api.india.delta.exchange"
    INDIA_TESTNET_BASE_URL = "https://cdn-ind.testnet.deltaex.org"

    # Rate limits
    DEFAULT_RATE_LIMIT = RateLimitConfig(
        requests_per_second=10,
        requests_per_minute=600,
        burst_limit=30,
    )

    DEFAULT_RETRY = RetryConfig(
        max_retries=3,
        base_delay=0.5,
        max_delay=5.0,
    )

    # Symbol mappings
    SYMBOL_MAP: ClassVar[dict[str, str]] = {
        "BTC/USD": "BTCUSD",
        "ETH/USD": "ETHUSD",
        "SOL/USD": "SOLUSD",
        "BNB/USD": "BNBUSD",
        "XRP/USD": "XRPUSD",
        "AVAX/USD": "AVAXUSD",
        "DOGE/USD": "DOGEUSD",
        "LINK/USD": "LINKUSD",
    }

    def __init__(
        self,
        environment: str = "paper",
        rate_limit: RateLimitConfig | None = None,
        retry_config: RetryConfig | None = None,
    ) -> None:
        """Initialize Delta Exchange adapter.

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
        self._base_url: str = ""
        self._region: str = ""
        self._client: httpx.AsyncClient | None = None

    @property
    def broker_code(self) -> str:
        """Get broker identifier."""
        return "delta_exchange"

    @property
    def supported_asset_classes(self) -> list[str]:
        """Get supported asset classes."""
        return ["crypto"]

    @property
    def supported_execution_methods(self) -> list[BrokerExecutionMethod]:
        """Get supported execution methods."""
        return [
            BrokerExecutionMethod.PERPETUAL,
            BrokerExecutionMethod.FUTURES,
            BrokerExecutionMethod.OPTIONS_SINGLE,
        ]

    @property
    def supports_paper_trading(self) -> bool:
        """Delta has testnet for paper trading."""
        return True

    @property
    def supports_multi_leg_orders(self) -> bool:
        """Delta doesn't support combo orders natively."""
        return False

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            asset_classes=["crypto"],
            execution_methods=["perpetual", "futures", "options_single"],
            multi_leg_orders=False,
            paper_trading={
                "type": "testnet",
                "base_url": self.TESTNET_BASE_URL,
                "india_base_url": self.INDIA_TESTNET_BASE_URL,
            },
            regions=["GLOBAL", "IN"],
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
        """Connect to Delta Exchange.

        Args:
            api_key: Delta API key
            api_secret: Delta API secret
            passphrase: Not used
            subaccount: Not used
            **kwargs: ``region`` must be exactly ``global`` or ``india``.

        Returns:
            True if connection successful
        """
        if not api_key.strip() or not api_secret.strip():
            logger.error("Delta api_key and api_secret are required")
            return False
        region = kwargs.get("region")
        if not isinstance(region, str) or not region.strip():
            logger.error("Delta region must be explicitly set to global or india")
            return False

        self._api_key = api_key
        self._api_secret = api_secret
        self._region = region.strip().lower()
        try:
            self._base_url = self._resolve_base_url(
                environment=self.environment,
                region=self._region,
            )
        except ValueError:
            logger.exception("Delta connection rejected an unsupported region")
            return False

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=30.0,
        )

        # Verify credentials
        try:
            response = await self._request("GET", "/v2/profile")
        except Exception:
            logger.exception("Failed to connect to Delta")
            await self._close_failed_connection()
            return False
        else:
            if response.get("success"):
                user = response.get("result", {})
                logger.info(
                    "Connected to Delta Exchange",
                    user_id=user.get("id"),
                    environment=self.environment,
                    region=self._region,
                )
                return True
            logger.error("Delta auth failed: %s", response.get("error"))
            await self._close_failed_connection()
            return False

    async def _close_failed_connection(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @classmethod
    def _resolve_base_url(cls, *, environment: str, region: str) -> str:
        """Resolve the exact regional host without a cross-region fallback."""
        hosts = {
            ("paper", "global"): cls.TESTNET_BASE_URL,
            ("live", "global"): cls.LIVE_BASE_URL,
            ("paper", "india"): cls.INDIA_TESTNET_BASE_URL,
            ("live", "india"): cls.INDIA_LIVE_BASE_URL,
        }
        try:
            return hosts[(environment, region)]
        except KeyError as exc:
            msg = (
                "Delta host selection requires environment paper/live and "
                f"region global/india; got {environment!r}/{region!r}"
            )
            raise ValueError(msg) from exc

    async def _do_disconnect(self) -> None:
        """Disconnect from Delta Exchange."""
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("Disconnected from Delta Exchange")

    # -------------------------------------------------------------------------
    # Order Management
    # -------------------------------------------------------------------------

    async def _do_place_order(self, order: BrokerOrderIntent) -> BrokerOrderResult:
        """Place order on Delta Exchange.

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
            # Get product ID
            symbol = order["symbol"]
            product_symbol = self.SYMBOL_MAP.get(symbol, symbol.replace("/", ""))
            product = await self._get_product(product_symbol)

            if not product:
                return {
                    "success": False,
                    "status": "rejected",
                    "filled_quantity": "0",
                    "error_message": f"Product not found: {symbol}",
                }

            # Build order payload
            payload = {
                "product_id": product["id"],
                "size": int(float(order["quantity"])),
                "side": order["side"],
                "order_type": self._map_order_type(order.get("order_type", "market")),
            }

            # Add limit price
            if order.get("limit_price"):
                payload["limit_price"] = order["limit_price"]

            # Add stop price
            if order.get("stop_price"):
                payload["stop_price"] = order["stop_price"]

            # Add time in force
            tif_map = {
                "gtc": "gtc",
                "ioc": "ioc",
                "fok": "fok",
            }
            payload["time_in_force"] = tif_map.get(order.get("time_in_force", "gtc"), "gtc")

            # Add client order ID
            if order.get("client_order_id"):
                payload["client_order_id"] = order["client_order_id"]

            # Place order
            response = await self._request("POST", "/v2/orders", json_body=payload)

            if response.get("success"):
                result = response.get("result", {})
                return {
                    "success": True,
                    "status": self._map_order_status(result.get("state", "")),
                    "broker_order_id": str(result.get("id", "")),
                    "filled_quantity": str(result.get("size_filled", 0)),
                    "filled_price": str(result.get("average_fill_price", "")),
                }
            return {
                "success": False,
                "status": "rejected",
                "filled_quantity": "0",
                "error_message": response.get("error", {}).get("message", "Unknown error"),
            }

        except Exception as e:
            logger.exception("Failed to place Delta order")
            return {
                "success": False,
                "status": "rejected",
                "filled_quantity": "0",
                "error_message": str(e),
            }

    async def _do_cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an order.

        Args:
            broker_order_id: Delta order ID

        Returns:
            True if cancelled
        """
        if not self._client:
            return False

        try:
            response = await self._request(
                "DELETE",
                f"/v2/orders/{broker_order_id}",
            )
        except Exception:
            logger.exception("Failed to cancel order")
            return False
        else:
            return bool(response.get("success", False))

    async def _do_get_order_status(
        self,
        broker_order_id: str,
    ) -> BrokerOrderResult:
        """Get order status.

        Args:
            broker_order_id: Delta order ID

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
            response = await self._request("GET", f"/v2/orders/{broker_order_id}")
        except Exception as e:
            logger.exception("Failed to get order status")
            return {
                "success": False,
                "status": "unknown",
                "filled_quantity": "0",
                "error_message": str(e),
            }
        else:
            if response.get("success"):
                result = response.get("result", {})
                return {
                    "success": True,
                    "status": self._map_order_status(result.get("state", "")),
                    "broker_order_id": broker_order_id,
                    "filled_quantity": str(result.get("size_filled", 0)),
                    "filled_price": str(result.get("average_fill_price", "")),
                }
            return {
                "success": False,
                "status": "unknown",
                "filled_quantity": "0",
                "error_message": response.get("error", {}).get("message", ""),
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
            response = await self._request("GET", "/v2/wallet/balances")
        except Exception:
            logger.exception("Failed to get balance")
            return []
        else:
            if not response.get("success"):
                return []

            balances: list[AccountBalance] = []
            for asset in response.get("result", []):
                if not isinstance(asset, dict):
                    msg = "Delta balances response contained a non-object row"
                    raise TypeError(msg)
                currency = str(asset.get("asset_symbol") or "").strip().upper()
                if not currency:
                    msg = "Delta balance omitted asset_symbol"
                    raise RuntimeError(msg)
                total = self._required_decimal(asset, "balance", context=f"{currency} balance")
                available = self._required_decimal(
                    asset,
                    "available_balance",
                    context=f"{currency} balance",
                )
                if total < 0 or available < 0 or available > total:
                    msg = f"Delta {currency} balance returned inconsistent amounts"
                    raise RuntimeError(msg)
                balances.append(
                    AccountBalance(
                        currency=currency,
                        total=str(total),
                        available=str(available),
                        locked=str(total - available),
                    )
                )

            return balances

    async def _do_get_account_snapshot(self) -> BrokerAccountSnapshot:
        """Return Delta inputs without fabricating account equity.

        The wallet endpoint documents balances and blocked margin, but its
        account-level ``net_equity`` metadata has no currency contract. Until
        that currency is authoritative, execution must not treat wallet balance
        or gross derivative notional as account equity.
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
        """Get live (open) orders from Delta.

        Mirrors _do_get_order_status: query the live orders endpoint and map each
        row to a BrokerOrderResult via _map_order_status.
        """
        if not self._client:
            return []

        params: dict[str, Any] = {"state": "open"}
        if symbol:
            params["product_symbol"] = symbol
        try:
            response = await self._request("GET", "/v2/orders", params=params)
        except Exception:
            logger.exception("Failed to get open orders")
            return []

        if not response.get("success"):
            return []

        results: list[BrokerOrderResult] = []
        for order in response.get("result", []) or []:
            results.append(
                {
                    "success": True,
                    "status": self._map_order_status(order.get("state", "")),
                    "broker_order_id": str(order.get("id", "")),
                    "filled_quantity": str(order.get("size_filled", 0)),
                    "filled_price": str(order.get("average_fill_price", "")),
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

        raw_positions = await self._get_all_margined_positions()
        positions: list[BrokerPosition] = []
        for raw in raw_positions:
            size = self._required_decimal(raw, "size", context="position")
            if size == 0:
                continue

            product_id = raw.get("product_id")
            symbol = str(raw.get("product_symbol") or "").strip()
            if not isinstance(product_id, int) or not symbol:
                msg = "Delta position omitted product_id or product_symbol"
                raise RuntimeError(msg)

            product = await self._get_product_by_id(product_id)
            if product is None:
                msg = f"Delta product metadata unavailable for position {symbol}"
                raise RuntimeError(msg)
            if product.get("id") != product_id or product.get("symbol") != symbol:
                msg = f"Delta product metadata did not match position {symbol}"
                raise RuntimeError(msg)
            ticker = await self._get_ticker(symbol)
            if ticker is None or ticker.get("mark_price") in (None, ""):
                msg = f"Delta mark price unavailable for position {symbol}"
                raise RuntimeError(msg)
            if ticker.get("symbol") != symbol:
                msg = f"Delta ticker metadata did not match position {symbol}"
                raise RuntimeError(msg)

            quote_currency = self._asset_symbol(product.get("quoting_asset"))
            settlement_currency = self._asset_symbol(product.get("settling_asset"))
            if quote_currency is None or settlement_currency is None:
                msg = f"Delta currency metadata unavailable for position {symbol}"
                raise RuntimeError(msg)
            notional_type = str(product.get("notional_type") or "").strip().lower()
            contract_type = str(product.get("contract_type") or "").strip().lower()
            if (
                notional_type != "vanilla"
                or contract_type != "perpetual_futures"
                or product.get("is_quanto") is not False
                or quote_currency != settlement_currency
            ):
                msg = (
                    f"Delta position {symbol} has unsupported valuation model "
                    f"(notional_type={notional_type!r}, contract_type={contract_type!r}, "
                    f"is_quanto={product.get('is_quanto')!r})"
                )
                raise RuntimeError(msg)

            contract_multiplier = self._required_decimal(
                product,
                "contract_value",
                context=f"product {symbol}",
            )
            if contract_multiplier <= 0:
                msg = f"Delta product {symbol} has non-positive contract_value"
                raise RuntimeError(msg)
            entry_price = self._required_decimal(raw, "entry_price", context=f"position {symbol}")
            mark_price = self._required_decimal(ticker, "mark_price", context=f"ticker {symbol}")
            if entry_price <= 0 or mark_price <= 0:
                msg = f"Delta position {symbol} has a non-positive entry or mark price"
                raise RuntimeError(msg)
            if "realized_pnl" not in raw or "margin" not in raw:
                msg = f"Delta position {symbol} omitted realized P&L or margin"
                raise RuntimeError(msg)

            # Delta's documented vanilla-futures formula is:
            # signed contracts * contract value * (mark - entry).
            # Inverse, option, and quanto contracts are rejected above.
            gross_notional = abs(size) * contract_multiplier * mark_price
            unrealized_pnl = size * contract_multiplier * (mark_price - entry_price)

            positions.append(
                BrokerPosition(
                    symbol=symbol,
                    quantity=str(size),
                    quantity_unit="contracts",
                    side="long" if size > 0 else "short",
                    entry_price=str(entry_price),
                    mark_price=str(mark_price),
                    unrealized_pnl=str(unrealized_pnl),
                    realized_pnl=str(
                        self._required_decimal(
                            raw,
                            "realized_pnl",
                            context=f"position {symbol}",
                        )
                    ),
                    margin_used=str(
                        self._required_decimal(raw, "margin", context=f"position {symbol}")
                    ),
                    liquidation_price=str(raw.get("liquidation_price", "")),
                    quote_currency=quote_currency,
                    settlement_currency=settlement_currency,
                    pnl_currency=settlement_currency,
                    notional_currency=quote_currency,
                    gross_notional=str(gross_notional),
                    contract_multiplier=str(contract_multiplier),
                    notional_type="vanilla",
                    contract_type=contract_type,
                    is_quanto=False,
                )
            )

        return positions

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    async def _get_all_margined_positions(self) -> list[dict[str, Any]]:
        """Return every documented V2 margined-position page."""
        positions: list[dict[str, Any]] = []
        after: str | None = None
        seen_cursors: set[str] = set()

        while True:
            params: dict[str, Any] = {"page_size": 50}
            if after is not None:
                params["after"] = after
            await self._rate_limit()
            response = await self._request(
                "GET",
                "/v2/positions/margined",
                params=params,
            )
            if not response.get("success"):
                msg = f"Delta positions request failed: {response.get('error')!r}"
                raise RuntimeError(msg)
            result = response.get("result")
            if not isinstance(result, list):
                msg = "Delta positions response did not contain a result list"
                raise TypeError(msg)
            positions.extend(cast(list[dict[str, Any]], result))

            meta = response.get("meta")
            next_cursor = meta.get("after") if isinstance(meta, dict) else None
            if not next_cursor:
                return positions
            after = str(next_cursor)
            if after in seen_cursors:
                msg = f"Delta positions pagination repeated cursor {after!r}"
                raise RuntimeError(msg)
            seen_cursors.add(after)

    async def _get_product_by_id(self, product_id: int) -> dict[str, Any] | None:
        """Get authoritative product metadata for a position."""
        await self._rate_limit()
        response = await self._request("GET", f"/v2/products/{product_id}")
        result = response.get("result")
        if response.get("success") and isinstance(result, dict):
            return cast(dict[str, Any], result)
        return None

    async def _get_ticker(self, symbol: str) -> dict[str, Any] | None:
        """Get the current public mark-price payload for a product."""
        await self._rate_limit()
        response = await self._request("GET", f"/v2/tickers/{symbol}")
        result = response.get("result")
        if response.get("success") and isinstance(result, dict):
            return cast(dict[str, Any], result)
        return None

    @staticmethod
    def _asset_symbol(asset: Any) -> str | None:
        """Return a normalized currency symbol from a Delta asset object."""
        if not isinstance(asset, dict):
            return None
        symbol = str(asset.get("symbol") or "").strip().upper()
        return symbol or None

    @staticmethod
    def _required_decimal(
        payload: dict[str, Any],
        field: str,
        *,
        context: str,
    ) -> Decimal:
        """Parse one required finite Delta numeric field."""
        if field not in payload or payload[field] in (None, ""):
            msg = f"Delta {context} omitted {field}"
            raise RuntimeError(msg)
        try:
            value = Decimal(str(payload[field]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            msg = f"Delta {context} returned invalid {field}"
            raise RuntimeError(msg) from exc
        if not value.is_finite():
            msg = f"Delta {context} returned non-finite {field}"
            raise RuntimeError(msg)
        return value

    async def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Delta API.

        Args:
            method: HTTP method
            path: API path
            params: Query parameters
            json_body: JSON body

        Returns:
            Response data
        """
        if not self._client:
            return {"success": False, "error": {"message": "Not connected"}}

        timestamp = str(int(time.time()))

        # Build signature payload
        if method == "GET":
            query_string = urlencode(params) if params else ""
            signature_payload = f"{method}{timestamp}{path}"
            if query_string:
                signature_payload += f"?{query_string}"
        else:
            body = json_module.dumps(json_body) if json_body else ""
            signature_payload = f"{method}{timestamp}{path}{body}"

        # Generate signature
        signature = hmac.new(
            self._api_secret.encode(),
            signature_payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        # Build headers
        headers = {
            "api-key": self._api_key,
            "timestamp": timestamp,
            "signature": signature,
            "Content-Type": "application/json",
            "User-Agent": "vynmatrix-Execution-Engine",
        }

        # Make request
        response = await self._client.request(
            method=method,
            url=path,
            params=params,
            json=json_body,
            headers=headers,
        )

        # Delta returns {"success": false, "error": {...}} with a 4xx for API
        # errors, which callers handle. A non-JSON response (e.g. a 5xx proxy/HTML
        # error) must not crash the caller, which only understands JSON — surface
        # a structured error instead.
        try:
            return cast(dict[str, Any], response.json())
        except (json_module.JSONDecodeError, ValueError):
            return {
                "success": False,
                "error": {
                    "code": response.status_code,
                    "message": f"Non-JSON response from Delta ({response.status_code})",
                },
            }

    async def _get_product(self, symbol: str) -> dict | None:
        """Get product info by symbol.

        Args:
            symbol: Product symbol (e.g., "BTCUSD")

        Returns:
            Product info or None
        """
        try:
            response = await self._request(
                "GET",
                "/v2/products",
                params={"symbol": symbol},
            )
        except Exception:
            logger.exception("Failed to get product")
            return None
        else:
            if response.get("success"):
                products = response.get("result", [])
                for product in products:
                    if product.get("symbol") == symbol:
                        return cast(dict[str, Any], product)

            return None

    def _map_order_type(self, order_type: str) -> str:
        """Map order type to Delta format."""
        mapping = {
            "market": "market_order",
            "limit": "limit_order",
            "stop": "stop_market_order",
            "stop_limit": "stop_limit_order",
        }
        return mapping.get(order_type, "market_order")

    def _map_order_status(self, state: str) -> str:
        """Map Delta order state to standard status."""
        mapping = {
            "open": "submitted",
            "pending": "pending",
            "closed": "filled",
            "cancelled": "cancelled",
        }
        return mapping.get(state.lower(), "unknown")
