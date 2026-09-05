"""Coinbase Advanced Trade API adapter.

This adapter implements the Coinbase App / Advanced Trade REST contract:
- REST host: ``https://api.coinbase.com``
- REST sandbox host: ``https://api-sandbox.coinbase.com``
- Authentication: ES256 JWT Bearer tokens built per request

Important operational note:
- Coinbase's Advanced Trade sandbox returns static mocked responses.
- We use it for auth/request-shape smoke only, not for paper-soak certification.
- Paper soak should run through the local paper broker with live Coinbase market data.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import urlencode, urlparse

import httpx

from lib_common.logging import get_logger
from lib_infrastructure.brokers.adapters.coinbase_auth import (
    build_coinbase_auth_headers,
    build_coinbase_rest_jwt,
    normalize_coinbase_private_key,
)
from lib_infrastructure.brokers.adapters.coinbase_order_sizing import (
    MarketOrderPrecision,
    MarketOrderSide,
    quantize_down_to_increment,
    translate_coinbase_market_order_size,
)
from lib_infrastructure.brokers.base import BaseBrokerAdapter
from lib_strategy.types import (
    AccountBalance,
    BrokerAccountSnapshot,
    BrokerCapabilities,
    BrokerExecutionMethod,
    BrokerFill,
    BrokerOrderIntent,
    BrokerOrderResult,
    BrokerPosition,
)

logger = get_logger(__name__)

# Market fills settle asynchronously on Coinbase; poll the order status after
# placement until the order reaches a TERMINAL state so the caller sees the real
# filled quantity/price. The total wait is bounded at 30 seconds; on timeout the
# remainder is CANCELLED so a late fill can never create an unprotected (naked)
# position. This is a broker safety invariant, not a deploy-time strategy knob.
_MARKET_FILL_POLL_DELAY_S = 0.5
_MARKET_FILL_TIMEOUT_S = 30.0
_MARKET_FILL_POLL_ATTEMPTS = max(1, int(_MARKET_FILL_TIMEOUT_S / _MARKET_FILL_POLL_DELAY_S))
# Mapped statuses (see ``_map_order_status``) after which the order can no longer fill.
_TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})


def _required_fill_decimal(
    row: dict[str, Any],
    field: str,
    *,
    positive: bool,
) -> Decimal:
    raw = row.get(field)
    if raw in (None, ""):
        msg = f"Coinbase fill omitted {field}"
        raise RuntimeError(msg)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        msg = f"Coinbase fill returned invalid {field}"
        raise RuntimeError(msg) from exc
    if not value.is_finite() or (positive and value <= 0) or (not positive and value < 0):
        msg = f"Coinbase fill returned invalid {field}"
        raise RuntimeError(msg)
    return value


def _required_fill_timestamp(row: dict[str, Any]) -> str:
    raw = row.get("trade_time")
    if raw in (None, ""):
        msg = "Coinbase fill omitted trade_time"
        raise RuntimeError(msg)
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        msg = "Coinbase fill returned invalid trade_time"
        raise RuntimeError(msg) from exc
    if parsed.tzinfo is None:
        msg = "Coinbase fill trade_time omitted a timezone"
        raise RuntimeError(msg)
    return parsed.astimezone(UTC).isoformat()


def _coinbase_commission(
    order_data: dict[str, Any],
    product_id: str | None,
) -> tuple[str, str | None]:
    """Return Coinbase's cumulative fee and its quote currency without float coercion."""
    details = order_data.get("commission_detail_total")
    if not isinstance(details, dict):
        details = {}
    raw_fee = details.get("total_commission")
    if raw_fee in (None, ""):
        raw_fee = order_data.get("total_fees")
    if raw_fee in (None, ""):
        raw_fee = order_data.get("fee")
    if raw_fee in (None, ""):
        raw_fee = "0"
    try:
        fee = Decimal(str(raw_fee))
    except (InvalidOperation, TypeError, ValueError) as exc:
        msg = f"Invalid Coinbase commission value: {raw_fee!r}"
        raise ValueError(msg) from exc
    if not fee.is_finite() or fee < 0:
        msg = f"Invalid Coinbase commission value: {raw_fee!r}"
        raise ValueError(msg)

    fee_currency = (
        details.get("commission_currency")
        or details.get("currency")
        or order_data.get("fee_currency")
    )
    resolved_product = str(order_data.get("product_id") or product_id or "")
    if not fee_currency and "-" in resolved_product:
        fee_currency = resolved_product.rsplit("-", 1)[-1]
    normalized_currency = str(fee_currency).strip().upper() if fee_currency else None
    return format(fee, "f"), normalized_currency or None


def _validate_settlement_product(product_id: str, settlement_currency: str) -> None:
    if "-" in product_id and product_id.rsplit("-", 1)[-1] == settlement_currency:
        return
    msg = f"Coinbase order product {product_id!r} does not settle in {settlement_currency}"
    raise ValueError(msg)


class CoinbaseAdapter(BaseBrokerAdapter):
    """Coinbase Advanced Trade API adapter.

    Supports:
    - Crypto spot trading (BTC, ETH, SOL, etc.)
    - Market and limit orders
    - Advanced Trade sandbox smoke testing

    Authentication:
    - Coinbase App CDP API key ID
    - ES256 private key
    - Bearer JWT generated per REST request

    Example:
        >>> adapter = CoinbaseAdapter(environment="paper")
        >>> adapter.configure_settlement_currency("USD")
        >>> await adapter.connect(
        ...     api_key="organizations/{org_id}/apiKeys/{key_id}",
        ...     api_secret="<PEM-encoded EC private key>"
        ... )
        >>> balances = await adapter.get_account_balance()
        >>> result = await adapter.place_order({
        ...     "symbol": "BTC-USD",
        ...     "side": "buy",
        ...     "quantity": "0.001",
        ...     "order_type": "market",
        ...     "execution_method": "spot"
        ... })
    """

    # API endpoints
    LIVE_BASE_URL = "https://api.coinbase.com"
    SANDBOX_BASE_URL = "https://api-sandbox.coinbase.com"
    LIVE_WS_URL = "wss://advanced-trade-ws.coinbase.com"

    # Advanced Trade API paths
    API_VERSION = "/api/v3/brokerage"

    def __init__(self, environment: str = "paper") -> None:
        """Initialize Coinbase adapter.

        Args:
            environment: "paper" for sandbox, "live" for production
        """
        super().__init__(environment=environment)

        # Connection state
        self._api_secret: str | None = None
        self._passphrase: str | None = None
        self._client: httpx.AsyncClient | None = None

        # Determine base URL
        self._base_url = self.SANDBOX_BASE_URL if environment == "paper" else self.LIVE_BASE_URL
        self._request_host = urlparse(self._base_url).netloc

        # Per-product precision cache (base_increment / quote_increment / price).
        self._product_cache: dict[str, dict[str, Any]] = {}
        # Configured by the account-scoped execution bridge.  Coinbase's
        # balance API does not identify which quote product owns an asset
        # position, so position marking must use the exact routed settlement
        # currency rather than a process-global default.
        self._settlement_currency: str | None = None

    def configure_settlement_currency(self, settlement_currency: str) -> None:
        """Bind this adapter instance to one explicit product quote currency."""
        normalized = str(settlement_currency or "").strip().upper()
        if not normalized or len(normalized) > 10 or not normalized.isalnum():  # noqa: PLR2004
            msg = "Coinbase adapter requires a valid settlement currency"
            raise ValueError(msg)
        if self._settlement_currency is not None and self._settlement_currency != normalized:
            msg = f"Coinbase adapter settlement is already scoped to {self._settlement_currency}"
            raise ValueError(msg)
        self._settlement_currency = normalized

    def _require_settlement_currency(self) -> str:
        if self._settlement_currency is None:
            msg = "Coinbase adapter requires an explicit settlement currency"
            raise RuntimeError(msg)
        return self._settlement_currency

    # =========================================================================
    # IBrokerAdapter properties
    # =========================================================================

    @property
    def broker_code(self) -> str:
        return "coinbase"

    @property
    def supported_asset_classes(self) -> list[str]:
        return ["crypto"]

    @property
    def supported_execution_methods(self) -> list[BrokerExecutionMethod]:
        return [BrokerExecutionMethod.SPOT]

    @property
    def supports_paper_trading(self) -> bool:
        return True  # Advanced Trade sandbox exists, but it is smoke/static only.

    @property
    def supports_multi_leg_orders(self) -> bool:
        return False  # Coinbase doesn't support options

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            asset_classes=["crypto"],
            execution_methods=["spot"],
            multi_leg_orders=False,
            paper_trading={
                "type": "sandbox_smoke",
                "base_url": self.SANDBOX_BASE_URL,
                "smoke_only": True,
            },
            regions=["US", "EU", "UK"],
            supported_order_types=["market", "limit", "stop", "stop_limit"],
            exact_fill_retrieval=True,
        )

    # =========================================================================
    # Connection methods
    # =========================================================================

    async def _do_connect(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str | None = None,
        subaccount: str | None = None,  # noqa: ARG002
        **kwargs: str,  # noqa: ARG002
    ) -> bool:
        """Connect to Coinbase API."""
        self._api_key = api_key
        self._api_secret = normalize_coinbase_private_key(api_secret)
        self._passphrase = passphrase or ""

        # Create HTTP client
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=30.0,
        )

        # Test connection by fetching accounts
        try:
            accounts = await self._request("GET", f"{self.API_VERSION}/accounts")
        except Exception:
            logger.exception("Failed to connect to Coinbase")
            await self._do_disconnect()
            return False
        else:
            logger.info(
                "Connected to Coinbase (%s)",
                self.environment,
                accounts_count=len(accounts.get("accounts", [])),
            )
            return True

    async def _do_disconnect(self) -> None:
        """Disconnect from Coinbase API."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._api_secret = None
        self._passphrase = None

    # =========================================================================
    # Trading methods
    # =========================================================================

    async def _do_place_order(self, order: BrokerOrderIntent) -> BrokerOrderResult:
        """Place order on Coinbase."""
        try:
            # Build order request
            client_order_id = order.get("client_order_id") or str(uuid.uuid4())
            product_id = self._normalize_symbol(order["symbol"])
            settlement_currency = self._require_settlement_currency()
            _validate_settlement_product(product_id, settlement_currency)

            # Per-product precision + current price. Coinbase rejects orders whose size or
            # price carry more precision than the product's base_increment/quote_increment
            # ("Too many decimals..."), and market BUYs are quote-denominated. Cached.
            product = await self._get_product_info(product_id)
            base_inc = str(product.get("base_increment") or "0.00000001")
            quote_inc = str(product.get("quote_increment") or "0.01")
            ref_price = float(product.get("price") or 0.0)
            base_qty = float(order.get("quantity") or 0.0)

            order_config: dict[str, Any] = {}
            otype = order["order_type"]

            if otype == "market":
                # Coinbase Advanced Trade market BUYs are quote-denominated: spend
                # `quote_size` (quote ccy), not base_size. Derive from qty x price.
                if order["side"] == "buy" and ref_price <= 0:
                    return self._error_result("No product price for market-buy quote_size")
                market_size = translate_coinbase_market_order_size(
                    base_quantity=base_qty,
                    side=MarketOrderSide(str(order["side"]).lower()),
                    precision=MarketOrderPrecision(
                        base_increment=base_inc,
                        quote_increment=quote_inc,
                    ),
                    reference_price=ref_price if ref_price > 0 else None,
                )
                order_config = {
                    "market_market_ioc": {market_size.denomination.value: market_size.amount}
                }
            elif otype == "limit":
                order_config = {
                    "limit_limit_gtc": {
                        "base_size": quantize_down_to_increment(base_qty, base_inc),
                        "limit_price": quantize_down_to_increment(
                            float(order.get("limit_price") or 0.0), quote_inc
                        ),
                        "post_only": False,
                    }
                }
            elif otype == "stop_limit":
                order_config = {
                    "stop_limit_stop_limit_gtc": {
                        "base_size": quantize_down_to_increment(base_qty, base_inc),
                        "limit_price": quantize_down_to_increment(
                            float(order.get("limit_price") or 0.0), quote_inc
                        ),
                        "stop_price": quantize_down_to_increment(
                            float(order.get("stop_price") or 0.0), quote_inc
                        ),
                    }
                }
            elif otype == "bracket":
                # Native OCO exit bracket: take-profit limit + stop-loss trigger as ONE
                # order. Two separate SELLs double-commit the same balance (the first
                # reserves it, the second fails "insufficient balance").
                order_config = {
                    "trigger_bracket_gtc": {
                        "base_size": quantize_down_to_increment(base_qty, base_inc),
                        "limit_price": quantize_down_to_increment(
                            float(order.get("limit_price") or 0.0), quote_inc
                        ),
                        "stop_trigger_price": quantize_down_to_increment(
                            float(order.get("stop_price") or 0.0), quote_inc
                        ),
                    }
                }
            else:
                return self._error_result(f"Unsupported order type: {order['order_type']}")

            payload = {
                "client_order_id": client_order_id,
                "product_id": product_id,
                "side": order["side"].upper(),
                "order_configuration": order_config,
            }

            response = await self._request(
                "POST",
                f"{self.API_VERSION}/orders",
                json_body=payload,
            )

            # Parse response. Coinbase Advanced Trade returns the created order
            # under "success_response" (the create endpoint has no top-level
            # "order" key — that shape is only returned by the GET historical
            # order endpoint). Missing this leaves broker_order_id=None on every
            # placement, which breaks order tracking, status polling,
            # reconciliation, and cancellation in live trading.
            if response.get("success"):
                order_data = response.get("success_response") or response.get("order", {})
                broker_order_id = order_data.get("order_id")
                commission, commission_currency = _coinbase_commission(order_data, product_id)
                result = BrokerOrderResult(
                    success=True,
                    broker_order_id=broker_order_id,
                    client_order_id=client_order_id,
                    status=self._map_order_status(order_data.get("status", "")),
                    filled_quantity=order_data.get("filled_size", "0"),
                    filled_price=order_data.get("average_filled_price"),
                    commission=commission,
                    timestamp=datetime.now(UTC).isoformat(),
                    raw_response=response,
                )
                if commission_currency is not None:
                    result["commission_currency"] = commission_currency
                # Market orders fill asynchronously — the create response reports
                # filled_size=0. Poll briefly so the caller sees the real fill (needed to
                # record the position and to size bracket exits to what actually filled).
                if otype == "market" and broker_order_id:
                    return await self._await_market_fill(broker_order_id, result)
                return result
            error_msg = response.get("error_response", {}).get("message", "Unknown error")
            return self._error_result(error_msg)

        except Exception as e:
            logger.exception("Error placing order")
            return self._error_result(str(e))

    async def _await_market_fill(
        self, broker_order_id: str, fallback: BrokerOrderResult
    ) -> BrokerOrderResult:
        """Poll a just-placed market order until it reaches a TERMINAL status.

        Coinbase settles market fills asynchronously. Returning early on a
        non-terminal state is dangerous in two ways:

        - reporting filled_quantity=0 on timeout while the order is still LIVE
          makes the caller skip the protective exits, and the order can fill
          seconds later as a naked position;
        - returning on the first partial fill sizes the exits to the partial
          quantity while the remainder keeps filling unprotected.

        So: poll until FILLED/CANCELLED/EXPIRED/FAILED. If the bounded window
        elapses with the order still live, CANCEL the remainder, then fetch the
        final state once — if the cancel raced a fill, the final state carries
        the real filled quantity so the caller still places correctly-sized
        exits; if it cancelled with a partial fill, the caller sizes exits to
        the quantity that actually filled.
        """
        last_seen = fallback
        for _ in range(_MARKET_FILL_POLL_ATTEMPTS):
            await asyncio.sleep(_MARKET_FILL_POLL_DELAY_S)
            status = await self._do_get_order_status(broker_order_id)
            if not status.get("success"):
                # Transient status-fetch error; keep polling.
                continue
            last_seen = status
            if str(status.get("status") or "").lower() in _TERMINAL_ORDER_STATUSES:
                return status
        # Timed out with the order still live at Coinbase: cancel the remainder so
        # a late, unseen fill can never create an unprotected position.
        logger.warning(
            "Market order %s not terminal after %.1fs; cancelling remainder",
            broker_order_id,
            _MARKET_FILL_POLL_ATTEMPTS * _MARKET_FILL_POLL_DELAY_S,
        )
        cancelled = await self._do_cancel_order(broker_order_id)
        final_status = await self._do_get_order_status(broker_order_id)
        if final_status.get("success"):
            return final_status
        if not cancelled:
            logger.warning(
                "Cancel of market order %s failed and final status fetch failed; "
                "returning last observed state",
                broker_order_id,
            )
        return last_seen

    async def _do_cancel_order(self, broker_order_id: str) -> bool:
        """Cancel order on Coinbase."""
        try:
            response = await self._request(
                "POST",
                f"{self.API_VERSION}/orders/batch_cancel",
                json_body={"order_ids": [broker_order_id]},
            )
        except Exception:
            logger.exception("Error canceling order")
            return False
        else:
            results = response.get("results", [])
            return bool(results and results[0].get("success"))

    async def _do_get_order_status(self, broker_order_id: str) -> BrokerOrderResult:
        """Get order status from Coinbase."""
        try:
            response = await self._request(
                "GET",
                f"{self.API_VERSION}/orders/historical/{broker_order_id}",
            )

            order_data = response.get("order", {})
            commission, commission_currency = _coinbase_commission(
                order_data,
                str(order_data.get("product_id") or "") or None,
            )
            result = BrokerOrderResult(
                success=True,
                broker_order_id=order_data.get("order_id"),
                client_order_id=order_data.get("client_order_id"),
                status=self._map_order_status(order_data.get("status", "")),
                filled_quantity=order_data.get("filled_size", "0"),
                filled_price=order_data.get("average_filled_price"),
                commission=commission,
                raw_response=response,
            )
        except Exception as e:
            return self._error_result(str(e))
        else:
            if commission_currency is not None:
                result["commission_currency"] = commission_currency
            return result

    async def _do_get_fills(self, broker_order_id: str) -> list[BrokerFill]:
        """Return Coinbase's exact Advanced Trade fills for one order."""
        normalized_order_id = str(broker_order_id or "").strip()
        if not normalized_order_id:
            msg = "Coinbase fill retrieval requires broker_order_id"
            raise ValueError(msg)

        cursor: str | None = None
        seen_cursors: set[str] = set()
        fills: list[BrokerFill] = []
        while True:
            params = {"order_ids": normalized_order_id, "limit": "100"}
            if cursor is not None:
                params["cursor"] = cursor
            response = await self._request(
                "GET",
                f"{self.API_VERSION}/orders/historical/fills",
                params=params,
            )
            rows = response.get("fills")
            if not isinstance(rows, list):
                msg = "Coinbase fills response omitted fills"
                raise TypeError(msg)
            for row in rows:
                if not isinstance(row, dict):
                    msg = "Coinbase fills response contained a non-object row"
                    raise TypeError(msg)
                order_id = str(row.get("order_id") or "").strip()
                if order_id != normalized_order_id:
                    msg = "Coinbase fills response crossed the requested order boundary"
                    raise RuntimeError(msg)
                trade_id = str(row.get("trade_id") or "").strip()
                symbol = str(row.get("product_id") or "").strip().upper()
                side = str(row.get("side") or "").strip().lower()
                if not trade_id or not symbol or side not in {"buy", "sell"}:
                    msg = "Coinbase fill omitted stable identity, product, or side"
                    raise RuntimeError(msg)
                if "-" not in symbol:
                    msg = f"Coinbase fill product {symbol!r} has no quote currency"
                    raise RuntimeError(msg)
                fee_currency = symbol.rsplit("-", 1)[-1]
                quantity = _required_fill_decimal(row, "size", positive=True)
                price = _required_fill_decimal(row, "price", positive=True)
                fee = _required_fill_decimal(row, "commission", positive=False)
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
                        timestamp=_required_fill_timestamp(row),
                        venue=self.broker_code,
                        raw_response=dict(row),
                    )
                )

            next_cursor = response.get("cursor")
            if next_cursor in (None, ""):
                break
            cursor = str(next_cursor)
            if cursor in seen_cursors:
                msg = f"Coinbase fills pagination repeated cursor {cursor!r}"
                raise RuntimeError(msg)
            seen_cursors.add(cursor)
        return fills

    async def _do_get_account_balance(self) -> list[AccountBalance]:
        """Get account balances from Coinbase."""
        response = await self._request("GET", f"{self.API_VERSION}/accounts")

        balances = []
        for account in response.get("accounts", []):
            available = account.get("available_balance", {})
            hold = account.get("hold", {})

            currency = available.get("currency", account.get("currency", ""))
            available_value = available.get("value", "0")
            hold_value = hold.get("value", "0")

            try:
                available_amount = Decimal(str(available_value))
                held_amount = Decimal(str(hold_value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                msg = f"Coinbase returned an invalid account balance for {currency!r}"
                raise RuntimeError(msg) from exc
            if (
                not available_amount.is_finite()
                or not held_amount.is_finite()
                or available_amount < 0
                or held_amount < 0
            ):
                msg = f"Coinbase returned an invalid account balance for {currency!r}"
                raise RuntimeError(msg)
            total = str(available_amount + held_amount)

            balances.append(
                AccountBalance(
                    currency=currency,
                    total=total,
                    available=str(available_amount),
                    locked=str(held_amount),
                )
            )

        return balances

    async def _do_get_account_snapshot(self) -> BrokerAccountSnapshot:
        """Return a marked spot-only portfolio snapshot.

        Coinbase's accounts endpoint returns asset quantities rather than an
        authoritative account-equity field. The bridge may reconstruct equity
        because this adapter is spot-only and every non-cash holding below is
        paired with Coinbase's observed product price.
        """
        balances = await self._do_get_account_balance()
        return BrokerAccountSnapshot(
            balances=balances,
            positions=await self._positions_from_balances(balances),
            equity=[],
            available_cash=[],
            margin_used=[],
            equity_source="spot_holdings",
        )

    async def _do_get_open_orders(self, symbol: str | None = None) -> list[BrokerOrderResult]:
        """Get open orders from Coinbase."""
        params: dict[str, str] = {"order_status": "OPEN"}
        if symbol:
            params["product_id"] = self._normalize_symbol(symbol)

        # Coinbase Advanced Trade list-orders endpoint is `/orders/historical/batch`;
        # the bare `/orders/historical` 404s (only `/orders/historical/{order_id}` exists
        # for single-order lookups). A 404 here fails reconciliation and opens the breaker.
        response = await self._request(
            "GET",
            f"{self.API_VERSION}/orders/historical/batch",
            params=params,
        )

        results = []
        for order_data in response.get("orders", []):
            commission, commission_currency = _coinbase_commission(
                order_data,
                str(order_data.get("product_id") or "") or None,
            )
            result = BrokerOrderResult(
                success=True,
                broker_order_id=order_data.get("order_id"),
                client_order_id=order_data.get("client_order_id"),
                status=self._map_order_status(order_data.get("status", "")),
                filled_quantity=order_data.get("filled_size", "0"),
                filled_price=order_data.get("average_filled_price"),
                commission=commission,
            )
            if commission_currency is not None:
                result["commission_currency"] = commission_currency
            results.append(result)

        return results

    async def _do_get_positions(self) -> list[BrokerPosition]:
        """Get positions (non-zero balances) from Coinbase."""
        balances = await self._do_get_account_balance()
        return await self._positions_from_balances(balances)

    async def _positions_from_balances(
        self,
        balances: list[AccountBalance],
    ) -> list[BrokerPosition]:
        """Mark non-cash spot balances with Coinbase product prices."""
        positions: list[BrokerPosition] = []
        quote_currency = self._require_settlement_currency()
        for balance in balances:
            try:
                total = Decimal(balance["total"])
                # USDC/USDT are cash/quote settlement currencies, not tradeable positions;
                # excluding them (like fiat) avoids a phantom "USDC-USD" position that
                # reconciliation flags as drift and opens the live circuit breaker.
                cash_currencies = {"USD", "EUR", "GBP", "USDC", "USDT", quote_currency}
                if total > 0 and balance["currency"] not in cash_currencies:
                    symbol = f"{balance['currency']}-{quote_currency}"
                    product = await self._get_product_info(symbol)
                    mark_price = Decimal(str(product.get("price") or "0"))
                    if not mark_price.is_finite() or mark_price <= 0:
                        msg = f"Coinbase product {symbol} omitted a positive observed price"
                        raise RuntimeError(msg)
                    positions.append(
                        BrokerPosition(
                            symbol=symbol,
                            quantity=str(total),
                            quantity_unit="asset",
                            side="long",
                            current_price=str(mark_price),
                            asset_currency=balance["currency"],
                            quote_currency=quote_currency,
                            settlement_currency=quote_currency,
                            notional_currency=quote_currency,
                            gross_notional=str(total * mark_price),
                            contract_multiplier="1",
                            notional_type="spot",
                            is_quanto=False,
                        )
                    )
            except (InvalidOperation, ValueError, TypeError) as exc:
                msg = f"Coinbase returned an invalid account balance: {balance!r}"
                raise RuntimeError(msg) from exc

        return positions

    # =========================================================================
    # HTTP request handling with authentication
    # =========================================================================

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Coinbase API."""
        if not self._client:
            msg = "Not connected to Coinbase"
            raise ConnectionError(msg)

        # Build URL
        url = path
        if params:
            url = f"{path}?{urlencode(params)}"

        # Build headers
        headers = self._build_auth_headers(method=method, path=path)

        # Make request
        response = await self._client.request(
            method,
            url,
            headers=headers,
            json=json_body if json_body else None,
        )

        # Handle response
        http_error_threshold = 400
        if response.status_code >= http_error_threshold:
            error_data = response.json() if response.text else {}
            api_error_msg = error_data.get("message", response.text)
            full_error_msg = f"Coinbase API error: {api_error_msg}"
            raise httpx.HTTPStatusError(
                full_error_msg,
                request=response.request,
                response=response,
            )

        return cast(dict[str, Any], response.json())

    # =========================================================================
    # Helper methods
    # =========================================================================

    def _build_auth_headers(self, *, method: str, path: str) -> dict[str, str]:
        if not self._api_key or not self._api_secret:
            msg = "Coinbase API key and ES256 private key are required"
            raise ValueError(msg)
        return cast(
            dict[str, str],
            build_coinbase_auth_headers(
                api_key=self._api_key,
                api_secret=self._api_secret,
                method=method,
                host=self._request_host,
                path=path,
            ),
        )

    def _build_rest_jwt(self, *, method: str, path: str) -> str:
        if not self._api_key or not self._api_secret:
            msg = "Coinbase API key and ES256 private key are required"
            raise ValueError(msg)
        return str(
            build_coinbase_rest_jwt(
                api_key=self._api_key,
                api_secret=self._api_secret,
                method=method,
                host=self._request_host,
                path=path,
            )
        )

    def _normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol to Coinbase format (e.g., BTCUSD -> BTC-USD)."""
        symbol = symbol.upper()
        if "-" in symbol:
            base, _, quote = symbol.partition("-")
            return f"{base}-{quote}"

        # Common crypto pairs
        quote_currencies = ["USD", "EUR", "GBP", "USDT", "USDC", "BTC", "ETH"]
        for quote in quote_currencies:
            if symbol.endswith(quote):
                base = symbol[: -len(quote)]
                return f"{base}-{quote}"

        return symbol

    async def _get_product_info(self, product_id: str) -> dict[str, Any]:
        """Fetch + cache a product's trading rules (base/quote increment, price)."""
        cached = self._product_cache.get(product_id)
        if cached is not None:
            return cached
        info = await self._request("GET", f"{self.API_VERSION}/products/{product_id}")
        self._product_cache[product_id] = info
        return info

    def _map_order_status(self, coinbase_status: str) -> str:
        """Map Coinbase order status to our standard status."""
        status_map = {
            "PENDING": "new",
            "OPEN": "working",
            "FILLED": "filled",
            "CANCELLED": "canceled",
            "EXPIRED": "expired",
            "FAILED": "rejected",
        }
        return status_map.get(coinbase_status.upper(), coinbase_status.lower())
