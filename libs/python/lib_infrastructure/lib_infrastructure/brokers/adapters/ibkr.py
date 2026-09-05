"""Interactive Brokers adapter implementation.

This module provides integration with Interactive Brokers (IBKR) using
the Client Portal API for multi-asset trading.

IBKR supports:
- Equities (stocks, ETFs)
- Options (single legs and complex strategies)
- Futures
- Forex
- Bonds
- Crypto (limited markets)

Authentication:
- Uses session-based authentication via Client Portal Gateway
- Requires IBKR account with API access enabled

Paper Trading:
- IBKR provides dedicated paper trading accounts
- Same API, different account credentials

API Documentation:
- https://ibkrcampus.com/campus/ibkr-api-page/cpapi-v1/
"""

import asyncio
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any

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
    BrokerMoney,
    BrokerOrderIntent,
    BrokerOrderResult,
    BrokerPosition,
    PositionQuantityUnit,
)

logger = get_logger(__name__)


class IBKRSessionError(RuntimeError):
    """The Client Portal brokerage session is unavailable or unauthenticated."""


class IBKRAdapter(BaseBrokerAdapter):
    """Interactive Brokers adapter using Client Portal API.

    This adapter connects to IBKR via the Client Portal Gateway,
    which must be running and authenticated before use.

    Supported Features:
    - Spot trading for equities, ETFs
    - Options trading (single and multi-leg)
    - Futures trading
    - Forex trading
    - Account balance and positions

    Usage:
        >>> adapter = IBKRAdapter(environment="paper")
        >>> await adapter.connect(
        ...     api_key="",  # Not used - session based
        ...     api_secret="",
        ...     gateway_url="https://localhost:5000",  # Client Portal Gateway
        ... )
        >>> result = await adapter.place_order({
        ...     "symbol": "AAPL",
        ...     "side": "buy",
        ...     "quantity": "10",
        ...     "order_type": "limit",
        ...     "limit_price": "150.00",
        ... })

    Thread Safety:
        This adapter is thread-safe. Each request uses its own HTTP client.

    Note:
        The Client Portal Gateway must be running and authenticated
        before using this adapter. See IBKR documentation for setup.
    """

    # IBKR-specific configuration
    DEFAULT_GATEWAY_URL = "https://localhost:5000"
    PAPER_GATEWAY_URL = "https://localhost:5000"  # Same gateway, different account

    # Rate limiting (IBKR has strict limits)
    DEFAULT_RATE_LIMIT = RateLimitConfig(
        requests_per_second=10,
        requests_per_minute=600,
        burst_limit=20,
    )

    DEFAULT_RETRY = RetryConfig(
        max_retries=3,
        base_delay=1.0,
        max_delay=10.0,
    )
    SESSION_CHECK_INTERVAL_SECONDS = 55.0

    def __init__(
        self,
        environment: str = "paper",
        rate_limit: RateLimitConfig | None = None,
        retry_config: RetryConfig | None = None,
    ) -> None:
        """Initialize IBKR adapter.

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

        self._gateway_url: str = ""
        self._account_id: str = ""
        self._client: httpx.AsyncClient | None = None
        self._session_valid: bool = False
        self._session_check_lock = asyncio.Lock()
        self._last_session_check_at: float | None = None

    @property
    def broker_code(self) -> str:
        """Get broker identifier."""
        return "interactive_brokers"

    @property
    def supported_asset_classes(self) -> list[str]:
        """Get supported asset classes."""
        return ["equity", "etf", "options", "futures", "fx", "crypto", "commodities"]

    @property
    def supported_execution_methods(self) -> list[BrokerExecutionMethod]:
        """Get supported execution methods."""
        return [
            BrokerExecutionMethod.SPOT,
            BrokerExecutionMethod.MARGIN,
            BrokerExecutionMethod.OPTIONS_SINGLE,
            BrokerExecutionMethod.OPTIONS_STRATEGY,
            BrokerExecutionMethod.FUTURES,
        ]

    @property
    def supports_paper_trading(self) -> bool:
        """IBKR has dedicated paper trading accounts."""
        return True

    @property
    def supports_multi_leg_orders(self) -> bool:
        """IBKR supports combo orders for options."""
        return True

    @property
    def capabilities(self) -> BrokerCapabilities:
        """Get broker capabilities."""
        return BrokerCapabilities(
            asset_classes=self.supported_asset_classes,
            execution_methods=[
                "spot",
                "margin",
                "futures",
                "options_single",
                "options_strategy",
            ],
            multi_leg_orders=True,
            paper_trading={
                "type": "separate_account",
                "note": "Use paper trading account ID (DUxxxxxx)",
            },
            regions=["US", "EU", "ASIA"],
            supported_order_types=[
                "market",
                "limit",
                "stop",
                "stop_limit",
                "trailing_stop",
                "bracket",
            ],
        )

    # -------------------------------------------------------------------------
    # Connection Management
    # -------------------------------------------------------------------------

    async def _do_connect(
        self,
        api_key: str,  # noqa: ARG002
        api_secret: str,  # noqa: ARG002
        passphrase: str | None = None,  # noqa: ARG002
        subaccount: str | None = None,
        **kwargs: Any,
    ) -> bool:
        """Connect to IBKR Client Portal Gateway.

        Args:
            api_key: Not used for Client Portal (session-based)
            api_secret: Not used for Client Portal
            passphrase: Not used
            subaccount: IBKR account ID (e.g., "DU123456" for paper)
            **kwargs: Additional parameters
                - gateway_url: Client Portal Gateway URL

        Returns:
            True if connection successful
        """
        configured_account = subaccount.strip() if isinstance(subaccount, str) else ""
        raw_gateway_url = kwargs.get("gateway_url", self.DEFAULT_GATEWAY_URL)
        configured_gateway_url = raw_gateway_url if isinstance(raw_gateway_url, str) else ""
        configuration_error: str | None = None
        if not configured_account:
            configuration_error = (
                "IBKR subaccount is required; automatic account selection is disabled"
            )
        elif not configured_gateway_url.startswith("https://"):
            configuration_error = "IBKR gateway_url must be an HTTPS URL"
        if configuration_error is not None:
            logger.error(configuration_error)
            return False
        self._gateway_url = configured_gateway_url
        self._account_id = configured_account

        # SSL verification: live connections require an explicitly owned CA.
        ibkr_ca_cert = kwargs.get("ca_cert")
        ssl_verify: bool | str = True
        if ibkr_ca_cert:
            ssl_verify = ibkr_ca_cert
        elif self.environment != "live":
            ssl_verify = False
            logger.warning(
                "IBKR SSL verification disabled (non-live environment). "
                "Store ca_cert in the account credential to pin the gateway certificate."
            )
        else:
            logger.error("IBKR live credentials require a pinned ca_cert")
            return False

        self._client = httpx.AsyncClient(
            base_url=self._gateway_url,
            timeout=30.0,
            verify=ssl_verify,
        )

        # Verify session is active
        try:
            # Check authentication status
            response = await self._client.get("/v1/api/iserver/auth/status")
            response.raise_for_status()
            data = response.json()
            if (
                not isinstance(data, dict)
                or data.get("authenticated") is not True
                or data.get("connected") is not True
            ):
                logger.warning("IBKR session not authenticated")
                await self._close_failed_connection()
                return False

            accounts_response = await self._client.get("/v1/api/iserver/accounts")
            accounts_response.raise_for_status()
            accounts_payload = accounts_response.json()
        except Exception:
            logger.exception("Failed to connect to IBKR")
            await self._close_failed_connection()
            return False
        else:
            accounts = (
                accounts_payload.get("accounts") if isinstance(accounts_payload, dict) else None
            )
            if (
                not isinstance(accounts, list)
                or any(not isinstance(account, str) for account in accounts)
                or self._account_id not in accounts
            ):
                logger.error(
                    "IBKR gateway session cannot trade the configured account",
                    account_id=self._account_id,
                )
                await self._close_failed_connection()
                return False
            self._session_valid = True
            self._last_session_check_at = monotonic()
            logger.info(
                "Connected to IBKR",
                account_id=self._account_id,
                environment=self.environment,
            )
            return True

    async def _close_failed_connection(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._session_valid = False
        self._last_session_check_at = None

    async def _do_disconnect(self) -> None:
        """Disconnect from IBKR."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._session_valid = False
        self._last_session_check_at = None
        logger.info("Disconnected from IBKR")

    async def _ensure_active_session(self) -> None:
        """Refresh and verify the cached Client Portal brokerage session.

        IBKR sessions expire after inactivity. The documented ``/tickle``
        endpoint both keeps the session alive and returns the authoritative
        brokerage authentication state. A cached adapter must therefore prove
        that state immediately before a broker-visible order operation.
        """
        if self._client is None or not self._session_valid:
            msg = "IBKR Client Portal brokerage session is not connected"
            raise IBKRSessionError(msg)

        async with self._session_check_lock:
            now = monotonic()
            if (
                self._last_session_check_at is not None
                and now - self._last_session_check_at < self.SESSION_CHECK_INTERVAL_SECONDS
            ):
                return
            try:
                response = await self._client.post("/v1/api/tickle", json={})
                response.raise_for_status()
                body = response.json()
                auth_status = (
                    body.get("iserver", {}).get("authStatus", {}) if isinstance(body, dict) else {}
                )
            except (httpx.HTTPError, TypeError, ValueError) as exc:
                self._session_valid = False
                msg = "IBKR Client Portal session health check failed"
                raise IBKRSessionError(msg) from exc

            if (
                not isinstance(auth_status, dict)
                or auth_status.get("authenticated") is not True
                or auth_status.get("connected") is not True
            ):
                self._session_valid = False
                msg = "IBKR Client Portal brokerage session is not authenticated"
                raise IBKRSessionError(msg)
            self._last_session_check_at = now

    # -------------------------------------------------------------------------
    # Order Management
    # -------------------------------------------------------------------------

    async def _do_place_order(self, order: BrokerOrderIntent) -> BrokerOrderResult:
        """Place order via IBKR API.

        Args:
            order: Order intent

        Returns:
            Order result
        """
        if not self._client or not self._session_valid:
            return {
                "success": False,
                "status": "rejected",
                "filled_quantity": "0",
                "error_message": "Not connected to IBKR",
            }

        try:
            conid = self._require_conid(order.get("broker_instrument_id"))
            await self._ensure_active_session()
            # Build IBKR order
            ibkr_order = self._build_order(order, conid)

            # Submit order
            response = await self._client.post(
                f"/v1/api/iserver/account/{self._account_id}/orders",
                json={"orders": [ibkr_order]},
            )

            data = response.json()

            # Handle order confirmation (IBKR requires confirmation)
            if isinstance(data, list) and data:
                first_response = data[0]

                # Check if confirmation required
                if "id" in first_response and "message" in first_response:
                    # Confirm order
                    confirm_response = await self._client.post(
                        f"/v1/api/iserver/reply/{first_response['id']}",
                        json={"confirmed": True},
                    )
                    data = confirm_response.json()

                # Check for order ID
                if isinstance(data, list) and data:
                    order_data = data[0]
                    if "order_id" in order_data:
                        return {
                            "success": True,
                            "status": "submitted",
                            "broker_order_id": str(order_data["order_id"]),
                            "filled_quantity": "0",
                        }

            # Handle errors
            error_msg: str = "Unknown error"
            if isinstance(data, dict):
                error_msg = str(data.get("error", data.get("message", str(data))))
            elif isinstance(data, list) and data:
                error_msg = str(data[0].get("error", data[0].get("message", str(data))))

            return {  # noqa: TRY300
                "success": False,
                "status": "rejected",
                "filled_quantity": "0",
                "error_message": error_msg,
            }

        except Exception as e:
            logger.exception("Failed to place IBKR order")
            return {
                "success": False,
                "status": "rejected",
                "filled_quantity": "0",
                "error_message": str(e),
            }

    async def place_multi_leg_order(
        self,
        legs: list[BrokerOrderIntent],
    ) -> BrokerOrderResult:
        """Place multi-leg combo order using IBKR native support.

        Overrides base class to use IBKR native combo orders.

        Args:
            legs: List of order legs

        Returns:
            Order result
        """
        self._ensure_connected()
        await self._rate_limit()
        return await self._do_place_multi_leg_order(legs)

    async def _do_place_multi_leg_order(
        self,
        legs: list[BrokerOrderIntent],
    ) -> BrokerOrderResult:
        """Place multi-leg combo order.

        IBKR supports combo orders for options strategies.

        Args:
            legs: List of order legs

        Returns:
            Order result
        """
        if not self._client or not self._session_valid:
            return {
                "success": False,
                "status": "rejected",
                "filled_quantity": "0",
                "error_message": "Not connected to IBKR",
            }
        if not legs:
            return {
                "success": False,
                "status": "rejected",
                "filled_quantity": "0",
                "error_message": "IBKR multi-leg order requires at least one exact contract",
            }

        try:
            conids = self._require_distinct_conids(legs)
            await self._ensure_active_session()
            combo_legs = []
            for leg, conid in zip(legs, conids, strict=True):
                combo_legs.append(
                    {
                        "conid": conid,
                        "ratio": int(leg.get("quantity", "1")),
                        "action": "BUY" if leg["side"] == "buy" else "SELL",
                    }
                )

            # Build combo order
            combo_order = {
                "acctId": self._account_id,
                "conidex": f"{combo_legs[0]['conid']};;;",  # Combo notation
                "orderType": "LMT",
                "side": "BUY",  # Net direction
                "quantity": 1,
                "tif": "DAY",
                "legs": combo_legs,
            }

            # Add limit price if available
            if legs[0].get("limit_price"):
                combo_order["price"] = float(legs[0]["limit_price"])

            # Submit combo order
            response = await self._client.post(
                f"/v1/api/iserver/account/{self._account_id}/orders",
                json={"orders": [combo_order]},
            )

            data = response.json()

            # Handle response (similar to single order)
            if isinstance(data, list) and data:
                first_response = data[0]
                if "id" in first_response:
                    confirm_response = await self._client.post(
                        f"/v1/api/iserver/reply/{first_response['id']}",
                        json={"confirmed": True},
                    )
                    data = confirm_response.json()

                if isinstance(data, list) and data:
                    order_data = data[0]
                    if "order_id" in order_data:
                        return {
                            "success": True,
                            "status": "submitted",
                            "broker_order_id": str(order_data["order_id"]),
                            "filled_quantity": "0",
                        }

            return {  # noqa: TRY300
                "success": False,
                "status": "rejected",
                "filled_quantity": "0",
                "error_message": "Failed to submit combo order",
            }

        except Exception as e:
            logger.exception("Failed to place IBKR combo order")
            return {
                "success": False,
                "status": "rejected",
                "filled_quantity": "0",
                "error_message": str(e),
            }

    async def _do_cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an order.

        Args:
            broker_order_id: IBKR order ID

        Returns:
            True if cancelled
        """
        if not self._client or not self._session_valid:
            return False

        try:
            response = await self._client.delete(
                f"/v1/api/iserver/account/{self._account_id}/order/{broker_order_id}"
            )
            data = response.json()
        except Exception:
            logger.exception("Failed to cancel order")
            return False
        else:
            msg = str(data.get("msg", ""))
            return msg.lower().startswith("cancelled")

    async def _do_get_order_status(
        self,
        broker_order_id: str,
    ) -> BrokerOrderResult:
        """Get order status.

        Args:
            broker_order_id: IBKR order ID

        Returns:
            Order result with current status
        """
        if not self._client or not self._session_valid:
            return {
                "success": False,
                "status": "unknown",
                "filled_quantity": "0",
                "error_message": "Not connected",
            }

        try:
            response = await self._client.get(
                f"/v1/api/iserver/account/{self._account_id}/order/status/{broker_order_id}"
            )
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
            status_map = {
                "Submitted": "submitted",
                "Filled": "filled",
                "Cancelled": "cancelled",
                "PendingSubmit": "pending",
                "PreSubmitted": "pending",
                "Inactive": "rejected",
            }

            ibkr_status = data.get("status", "Unknown")
            status = status_map.get(ibkr_status, "unknown")

            result: BrokerOrderResult = {
                "success": status in ("submitted", "filled", "pending"),
                "status": status,
                "broker_order_id": broker_order_id,
                "filled_quantity": str(data.get("filled_qty", 0)),
            }
            if data.get("avg_price"):
                result["filled_price"] = str(data.get("avg_price", 0))
            return result

    # -------------------------------------------------------------------------
    # Account Information
    # -------------------------------------------------------------------------

    async def _do_get_account_balance(self) -> list[AccountBalance]:
        """Get account balances.

        Returns:
            List of balances by currency
        """
        if not self._client or not self._session_valid:
            return []

        try:
            response = await self._client.get(f"/v1/api/portfolio/{self._account_id}/ledger")
            balances = self._ledger_balances(response.json())
        except Exception:
            # Equity and position evidence remain usable for risk-reducing
            # orders when the currency ledger is unavailable. Entry funding
            # fails closed later because the snapshot carries no balances.
            logger.exception("Failed to get account balance")
            return []
        else:
            return balances

    def _ledger_balances(self, data: object) -> list[AccountBalance]:
        if not isinstance(data, dict):
            msg = "IBKR account ledger response was not an object"
            raise TypeError(msg)
        balances: list[AccountBalance] = []
        for currency, ledger in data.items():
            if currency == "BASE" or not isinstance(ledger, dict):
                continue
            cash_balance = self._required_decimal(
                ledger,
                "cashbalance",
                context=f"{currency} ledger",
            )
            settled_cash = self._required_decimal(
                ledger,
                "settledcash",
                context=f"{currency} ledger",
            )
            if cash_balance < 0 or settled_cash < 0:
                msg = f"{currency} ledger contains negative cash"
                raise ValueError(msg)
            available_cash = min(cash_balance, settled_cash)
            balances.append(
                AccountBalance(
                    currency=str(currency).upper(),
                    total=str(cash_balance),
                    available=str(available_cash),
                    locked=str(cash_balance - available_cash),
                )
            )
        return balances

    async def _do_get_account_snapshot(self) -> BrokerAccountSnapshot:
        """Get IBKR's authoritative universal-account equity and margin."""
        if not self._client or not self._session_valid:
            msg = "IBKR account snapshot requested without an active session"
            raise RuntimeError(msg)
        response = await self._client.get(f"/v1/api/portfolio/{self._account_id}/summary")
        summary = response.json()
        if not isinstance(summary, dict):
            msg = "IBKR account summary response was not an object"
            raise TypeError(msg)

        return BrokerAccountSnapshot(
            balances=await self._do_get_account_balance(),
            positions=await self._do_get_positions(),
            equity=[self._summary_money(summary, "netliquidation")],
            available_cash=[self._summary_money(summary, "availablefunds")],
            margin_used=[self._summary_money(summary, "maintmarginreq")],
            equity_source="broker",
        )

    async def _do_get_positions(self) -> list[BrokerPosition]:
        """Get current positions.

        Returns:
            List of position dictionaries
        """
        if not self._client or not self._session_valid:
            return []

        response = await self._client.get(f"/v1/api/portfolio/{self._account_id}/positions/0")
        data = response.json()
        if not isinstance(data, list):
            msg = "IBKR positions response was not a list"
            raise TypeError(msg)
        positions: list[BrokerPosition] = []
        for pos in data:
            if not isinstance(pos, dict):
                msg = "IBKR positions response contained a non-object row"
                raise TypeError(msg)
            currency = pos.get("currency")
            symbol = str(pos.get("contractDesc") or pos.get("description") or "")
            quantity = self._required_decimal(
                pos,
                "position",
                context=f"position {symbol}",
            )
            if quantity == 0:
                continue
            entry_price = self._required_decimal(
                pos,
                "avgPrice",
                context=f"position {symbol}",
            )
            current_price = self._required_decimal(
                pos,
                "mktPrice",
                context=f"position {symbol}",
            )
            market_value = self._required_decimal(
                pos,
                "mktValue",
                context=f"position {symbol}",
            )
            if entry_price <= 0 or current_price <= 0 or market_value == 0:
                msg = f"IBKR position {symbol} returned invalid valuation fields"
                raise RuntimeError(msg)
            asset_class = str(pos.get("assetClass") or pos.get("secType") or "")
            quantity_unit: PositionQuantityUnit = (
                "contracts" if asset_class.upper() in {"FUT", "FOP", "OPT", "WAR"} else "asset"
            )
            position = BrokerPosition(
                symbol=symbol,
                quantity=str(quantity),
                quantity_unit=quantity_unit,
                side="long" if quantity > 0 else "short",
                entry_price=str(entry_price),
                current_price=str(current_price),
                asset_class=asset_class,
                currency=str(currency or ""),
                quote_currency=str(currency or ""),
                settlement_currency=str(currency or ""),
                pnl_currency=str(currency or ""),
                notional_currency=str(currency or ""),
                gross_notional=str(abs(market_value)),
                notional_type="broker_mark_value",
                is_quanto=False,
            )
            if pos.get("unrealizedPnl") not in (None, ""):
                position["unrealized_pnl"] = str(
                    self._required_decimal(
                        pos,
                        "unrealizedPnl",
                        context=f"position {symbol}",
                    )
                )
            if pos.get("realizedPnl") not in (None, ""):
                position["realized_pnl"] = str(
                    self._required_decimal(
                        pos,
                        "realizedPnl",
                        context=f"position {symbol}",
                    )
                )
            positions.append(position)

        return positions

    @staticmethod
    def _summary_money(summary: dict[str, Any], field: str) -> BrokerMoney:
        """Parse one documented Client Portal account-summary amount."""
        payload = summary.get(field)
        if not isinstance(payload, dict):
            msg = f"IBKR account summary omitted {field}"
            raise TypeError(msg)
        value = IBKRAdapter._required_decimal(
            payload,
            "amount",
            context=f"account summary {field}",
        )
        currency = str(payload.get("currency") or "").strip().upper()
        if not currency:
            msg = f"IBKR account summary returned incomplete {field}"
            raise RuntimeError(msg)
        return BrokerMoney(value=str(value), currency=currency)

    @staticmethod
    def _required_decimal(
        payload: dict[str, Any],
        field: str,
        *,
        context: str,
    ) -> Decimal:
        """Parse one required finite Client Portal numeric field."""
        if field not in payload or payload[field] in (None, ""):
            msg = f"IBKR {context} omitted {field}"
            raise RuntimeError(msg)
        try:
            value = Decimal(str(payload[field]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            msg = f"IBKR {context} returned invalid {field}"
            raise RuntimeError(msg) from exc
        if not value.is_finite():
            msg = f"IBKR {context} returned non-finite {field}"
            raise RuntimeError(msg)
        return value

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    @staticmethod
    def _require_conid(value: Any) -> int:
        """Return one exact persisted IBKR conid without symbol inference."""
        if isinstance(value, bool):
            msg = "IBKR order requires an exact positive-integer broker_instrument_id conid"
            raise TypeError(msg)
        text = str(value or "").strip()
        if (
            not text
            or not text.isascii()
            or not text.isdigit()
            or text.startswith("0")
            or int(text) <= 0
        ):
            msg = "IBKR order requires an exact positive-integer broker_instrument_id conid"
            raise ValueError(msg)
        return int(text)

    @classmethod
    def _require_distinct_conids(cls, legs: list[BrokerOrderIntent]) -> list[int]:
        """Validate that every combo leg carries its own persisted conid."""
        conids = [cls._require_conid(leg.get("broker_instrument_id")) for leg in legs]
        if len(set(conids)) != len(conids):
            msg = "IBKR multi-leg orders require one distinct persisted conid per leg"
            raise ValueError(msg)
        return conids

    def _build_order(
        self,
        order: BrokerOrderIntent,
        conid: int,
    ) -> dict[str, Any]:
        """Build IBKR order from intent.

        Args:
            order: Order intent
            conid: Contract ID

        Returns:
            IBKR order dictionary
        """
        # Map order type
        order_type_map = {
            "market": "MKT",
            "limit": "LMT",
            "stop": "STP",
            "stop_limit": "STP LMT",
        }

        # Map time in force
        tif_map = {
            "gtc": "GTC",
            "ioc": "IOC",
            "fok": "FOK",
            "day": "DAY",
        }

        quantity = self._validated_order_quantity(order)
        ibkr_order: dict[str, Any] = {
            "acctId": self._account_id,
            "conid": conid,
            "orderType": order_type_map.get(order.get("order_type", "market"), "MKT"),
            "side": "BUY" if order["side"] == "buy" else "SELL",
            "quantity": (
                int(quantity) if quantity == quantity.to_integral_value() else float(quantity)
            ),
            "tif": tif_map.get(order.get("time_in_force", "day"), "DAY"),
        }

        # Add price for limit orders
        if order.get("limit_price"):
            ibkr_order["price"] = float(order["limit_price"])

        # Add stop price
        if order.get("stop_price"):
            ibkr_order["auxPrice"] = float(order["stop_price"])

        # Add client order ID
        if order.get("client_order_id"):
            ibkr_order["cOID"] = order["client_order_id"]

        return ibkr_order

    @staticmethod
    def _validated_order_quantity(order: BrokerOrderIntent) -> Decimal:
        """Require catalogue lot compliance before an IBKR order leaves the process."""

        try:
            quantity = Decimal(str(order["quantity"]))
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            msg = "IBKR order quantity must be a finite positive decimal"
            raise ValueError(msg) from exc
        if not quantity.is_finite() or quantity <= 0:
            msg = "IBKR order quantity must be a finite positive decimal"
            raise ValueError(msg)

        instrument_type = str(order.get("broker_instrument_type") or "").strip().upper()
        if instrument_type != "STK":
            return quantity
        raw_lot_size = order.get("lot_size")
        try:
            lot_size = Decimal(str(raw_lot_size))
        except (InvalidOperation, TypeError, ValueError) as exc:
            msg = "IBKR stock order requires an exact positive catalogue lot_size"
            raise ValueError(msg) from exc
        if not lot_size.is_finite() or lot_size <= 0:
            msg = "IBKR stock order requires an exact positive catalogue lot_size"
            raise ValueError(msg)
        if quantity % lot_size != 0:
            msg = "IBKR stock order quantity is not an exact catalogue lot multiple"
            raise ValueError(msg)
        return quantity

    async def _do_get_open_orders(
        self,
        symbol: str | None = None,
    ) -> list[BrokerOrderResult]:
        """Get open orders from IBKR.

        Args:
            symbol: Optional symbol filter

        Returns:
            List of open order results
        """
        if not self._client or not self._session_valid:
            return []

        try:
            response = await self._client.get(
                "/v1/api/iserver/account/orders",
                params={"accountId": self._account_id},
            )
            data = response.json()
        except Exception:
            logger.exception("Failed to get open orders")
            return []
        else:
            results: list[BrokerOrderResult] = []
            for order in data.get("orders", []):
                # Filter by symbol if provided
                if symbol and order.get("ticker") != symbol:
                    continue

                # Only include active orders
                if order.get("status") in ("PreSubmitted", "Submitted", "PendingSubmit"):
                    results.append(
                        BrokerOrderResult(
                            success=True,
                            status=order.get("status", "").lower(),
                            broker_order_id=str(order.get("orderId")),
                            filled_quantity=str(order.get("filledQuantity", 0)),
                        )
                    )

            return results
