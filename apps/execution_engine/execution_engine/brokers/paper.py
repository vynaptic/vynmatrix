"""
Paper trading broker adapter.

Simulates order execution for testing and paper trading.
Maintains virtual positions and P&L tracking.

Cost model is configurable via constructor parameters so paper/backtest results
approximate production broker costs. The process composition root resolves any
environment overrides once and injects the resulting immutable values.

Per-instrument overrides can be supplied via ``instrument_costs``:

    broker = PaperBroker(
        account_id="account-42",
        starting_balance=100_000,
        available_cash=100_000,
        currency="EUR",
        instrument_costs={
            "BTCUSD": {"slippage_pct": 0.0005, "commission_pct": 0.0008},
        },
    )
"""

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from execution_engine.brokers.base import (
    AccountInfo,
    BrokerAdapter,
    BrokerCapabilities,
    BrokerFill,
    BrokerOrderResult,
    OrderStatus,
    Position,
)
from execution_engine.brokers.paper_futures import PaperFuturesSimulator, _decimal_product
from execution_engine.brokers.paper_options import PaperOptionsSimulator
from execution_engine.config import BrokerType
from execution_engine.metrics.normalization import normalize_position_side
from execution_engine.models import (
    OptionsIntent,
    OrderCurrencyContext,
    OrderIntent,
    resolve_historical_replay_fill_context,
)
from lib_common.execution_costs import ExecutionCostRates
from lib_common.logging import get_logger

logger = get_logger(__name__)


class ReduceOnlyPositionUnavailableError(ValueError):
    """A reduce-only resting order no longer has a position to reduce.

    The durable lifecycle treats this as a terminal cancellation rather than a
    retryable fill failure: a protective order for a flat book protects nothing.
    """


@dataclass(frozen=True)
class PaperFillPlan:
    """Validated local-paper economics awaiting canonical ledger commit."""

    intent: OrderIntent
    result: BrokerOrderResult
    fill: BrokerFill
    trade: dict[str, Any]
    account_notional: float
    account_commission: float


class PaperBroker(BrokerAdapter):
    """
    Paper trading broker for simulation and testing.

    Features:
    - Virtual account with configurable starting balance
    - Simulated order execution with realistic fills
    - Position and P&L tracking
    - Trade history logging
    - Supports spot, margin, futures, and options simulation
    - Per-instrument cost overrides for calibrated simulation
    """

    def __init__(
        self,
        *,
        account_id: str,
        starting_balance: Decimal | float,
        available_cash: Decimal | float,
        currency: str,
        realized_pnl: Decimal | float = 0.0,
        slippage_pct: float = 0.001,
        commission_pct: float = 0.001,
        instrument_costs: dict[str, dict[str, float]] | None = None,
        allow_reversal: bool = False,
        durable_fill_committer: Callable[..., None] | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize paper broker.

        Args:
            account_id: Explicit linked broker account identity.
            starting_balance: Starting account balance
            available_cash: Current cash before any rehydrated open positions.
            currency: Account currency
            realized_pnl: Current realized P&L when restoring an account.
            slippage_pct: Default simulated slippage percentage
            commission_pct: Default commission percentage per trade
            instrument_costs: Per-instrument cost overrides, e.g.
                ``{"BTCUSD": {"slippage_pct": 0.0005, "commission_pct": 0.0008}}``
        """
        super().__init__(**kwargs)
        account_id = str(account_id).strip()
        currency = str(currency).strip().upper()
        if not account_id:
            msg = "PaperBroker requires an explicit account_id"
            raise ValueError(msg)
        if not currency or not currency.isalnum() or len(currency) > 10:  # noqa: PLR2004
            msg = "PaperBroker requires an explicit valid account currency"
            raise ValueError(msg)
        for field, value in (
            ("starting_balance", starting_balance),
            ("available_cash", available_cash),
            ("realized_pnl", realized_pnl),
        ):
            if isinstance(value, bool) or not math.isfinite(float(value)):
                msg = f"PaperBroker {field} must be finite"
                raise ValueError(msg)
        if starting_balance <= 0:
            msg = "PaperBroker starting_balance must be positive"
            raise ValueError(msg)
        if available_cash < 0:
            msg = "PaperBroker available_cash cannot be negative"
            raise ValueError(msg)
        self._account_id = account_id
        self._starting_balance = float(starting_balance)
        self._initial_cash = float(available_cash)
        self._initial_realized_pnl = float(realized_pnl)
        self._currency = currency
        default_costs = ExecutionCostRates.from_fractions(
            commission_fraction=commission_pct,
            slippage_fraction=slippage_pct,
        )
        self._slippage_pct = float(default_costs.slippage_fraction)
        self._commission_pct = float(default_costs.commission_fraction)
        self._instrument_costs: dict[str, dict[str, float]] = {}
        for raw_symbol, raw_overrides in (instrument_costs or {}).items():
            symbol = str(raw_symbol).strip()
            if not symbol:
                msg = "PaperBroker instrument cost symbol must not be blank"
                raise ValueError(msg)
            unexpected = set(raw_overrides) - {"slippage_pct", "commission_pct"}
            if unexpected:
                msg = (
                    f"PaperBroker instrument costs for {symbol!r} contain "
                    f"unsupported fields: {', '.join(sorted(unexpected))}"
                )
                raise ValueError(msg)
            rates = ExecutionCostRates.from_fractions(
                commission_fraction=raw_overrides.get(
                    "commission_pct",
                    default_costs.commission_fraction,
                ),
                slippage_fraction=raw_overrides.get(
                    "slippage_pct",
                    default_costs.slippage_fraction,
                ),
            )
            self._instrument_costs[symbol] = {
                "commission_pct": float(rates.commission_fraction),
                "slippage_pct": float(rates.slippage_fraction),
            }
        # Spot is long-only: by default an oversell (SELL > held long, or BUY >
        # held short) clamps to flat instead of silently opening a reverse
        # position — a real spot broker rejects the excess, and a reversed short
        # is the negative-qty state corruption the soak acceptance gate flags.
        # Opt in (futures/margin) by constructing with allow_reversal=True.
        self._allow_reversal = allow_reversal
        # Production local-paper fills cross this boundary before the in-memory
        # account book changes. The callback writes the exact fill to the
        # canonical ledger, making a process crash recoverable by replay.
        self._durable_fill_committer = durable_fill_committer

        # Account state
        self._balance = float(available_cash)
        self._realized_pnl = float(realized_pnl)
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, dict[str, Any]] = {}
        self._trade_history: list[dict[str, Any]] = []

        # Observed paper marks supplied by the market-data boundary.
        self._prices: dict[str, float] = {}
        self._contract_multipliers: dict[str, float] = {}
        self._futures_leverages: dict[str, float] = {}
        self._futures_contract_types: dict[str, str] = {}
        self._currency_contexts: dict[str, OrderCurrencyContext] = {}
        # Account-currency cost/proceeds basis for each open position.  This
        # preserves realized P&L when FX changes between entry and exit.
        self._position_account_basis: dict[str, float] = {}

        # Per-asset-class fill models. Stateless strategy objects holding only
        # this broker reference; the broker remains the single mutable ledger.
        self._futures_sim = PaperFuturesSimulator(self)
        self._options_sim = PaperOptionsSimulator(self)

        self._connected = True  # Paper broker is always "connected"

    def bind_canonical_order(
        self,
        intent: OrderIntent | OptionsIntent,
        *,
        order_id: int,
        instrument_id: int,
    ) -> None:
        """Bind one pending canonical order to its imminent local fill."""
        if self._durable_fill_committer is None:
            msg = "Durable paper execution requires a canonical fill committer"
            raise RuntimeError(msg)
        if order_id <= 0 or instrument_id <= 0:
            msg = "Durable paper execution requires positive canonical identities"
            raise ValueError(msg)
        intent._canonical_paper_order_id = order_id
        intent._canonical_paper_instrument_id = instrument_id

    def _commit_fill_before_state(
        self,
        intent: OrderIntent | OptionsIntent,
        *,
        result: BrokerOrderResult,
        trade: dict[str, Any],
    ) -> None:
        """Commit exact paper economics before mutating the process-local book."""
        if self._durable_fill_committer is None:
            return
        order_id = getattr(intent, "_canonical_paper_order_id", None)
        instrument_id = getattr(intent, "_canonical_paper_instrument_id", None)
        if order_id is None or instrument_id is None:
            msg = "Paper fill reached state mutation without canonical order attribution"
            raise RuntimeError(msg)
        fill = BrokerFill(
            trade_id=str(trade["trade_id"]),
            order_id=str(trade["order_id"]),
            symbol=str(trade["symbol"]),
            side=str(trade["side"]),
            quantity=Decimal(str(trade["quantity"])),
            price=Decimal(str(trade["price"])),
            commission=Decimal(str(trade["commission"])),
            commission_currency=str(trade["commission_currency"]),
            timestamp=trade["timestamp"],
            venue="paper",
            raw_response=(
                dict(trade["source_provenance"])
                if isinstance(trade.get("source_provenance"), dict)
                else None
            ),
        )
        self._durable_fill_committer(
            order_id=order_id,
            result=result,
            fills=[fill],
            instr_id=instrument_id,
        )
        intent._canonical_paper_order_id = None
        intent._canonical_paper_instrument_id = None

    def prepare_resting_fill(
        self,
        intent: OrderIntent,
        *,
        broker_order_id: str,
        trade_id: str,
        quantity: Decimal,
        reference_price: Decimal,
        timestamp: datetime,
        source_provenance: dict[str, Any],
        apply_market_slippage: bool,
        is_terminal: bool,
    ) -> PaperFillPlan:
        """Validate and price a triggered resting fill without mutating state.

        The lifecycle worker commits the returned exact fill to PostgreSQL
        before calling :meth:`apply_committed_fill`. This preserves the same
        ledger-before-projection invariant as immediate market orders.
        """
        validation_error = self.validate_order(intent)
        if validation_error:
            raise ValueError(validation_error)
        if self._futures_sim._resolve_linear_futures_terms(intent) is not None:
            msg = "Durable resting-fill lifecycle currently supports spot orders only"
            raise ValueError(msg)
        context = self._require_currency_context(intent)
        normalized_quantity = Decimal(str(quantity))
        normalized_reference = Decimal(str(reference_price))
        if (
            not normalized_quantity.is_finite()
            or normalized_quantity <= 0
            or not normalized_reference.is_finite()
            or normalized_reference <= 0
        ):
            msg = "Paper resting fill requires positive finite quantity and price"
            raise ValueError(msg)
        requested_quantity, position_error = self._effective_resting_quantity(intent)
        if position_error:
            raise ReduceOnlyPositionUnavailableError(position_error)
        normalized_quantity = min(normalized_quantity, Decimal(str(requested_quantity)))
        slip = Decimal(str(self._get_slippage_pct(intent.symbol)))
        fill_price = normalized_reference
        if apply_market_slippage:
            fill_price *= Decimal("1") + slip if intent.side == "BUY" else Decimal("1") - slip
        commission_rate = Decimal(str(self._get_commission_pct(intent.symbol)))
        notional = normalized_quantity * fill_price
        commission = abs(notional * commission_rate)
        account_notional = self._settlement_to_account(context, notional)
        account_commission = self._settlement_to_account(context, commission)
        if intent.side == "BUY" and account_notional + account_commission > self._balance:
            msg = (
                f"Insufficient balance: need {account_notional + account_commission:.2f} "
                f"{self._currency}, have {self._balance:.2f} {self._currency}"
            )
            raise ValueError(msg)
        normalized_ts = timestamp.astimezone(UTC)
        status = OrderStatus.FILLED if is_terminal else OrderStatus.PARTIALLY_FILLED
        result = BrokerOrderResult(
            success=True,
            order_id=str(broker_order_id),
            status=status,
            filled_quantity=float(normalized_quantity),
            filled_price=float(fill_price),
            commission=commission,
            commission_currency=context.settlement_currency,
            timestamp=normalized_ts,
        )
        fill = BrokerFill(
            trade_id=str(trade_id),
            order_id=str(broker_order_id),
            symbol=intent.symbol,
            side=intent.side,
            quantity=normalized_quantity,
            price=fill_price,
            commission=commission,
            commission_currency=context.settlement_currency,
            timestamp=normalized_ts,
            venue="paper",
            raw_response=dict(source_provenance),
        )
        trade = {
            "trade_id": fill.trade_id,
            "order_id": fill.order_id,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": float(normalized_quantity),
            "price": float(fill_price),
            "commission": commission,
            "commission_currency": context.settlement_currency,
            "account_notional": account_notional,
            "account_commission": account_commission,
            "account_currency": self._currency,
            "timestamp": normalized_ts,
            "source_provenance": dict(source_provenance),
        }
        return PaperFillPlan(
            intent=intent,
            result=result,
            fill=fill,
            trade=trade,
            account_notional=account_notional,
            account_commission=account_commission,
        )

    def _effective_resting_quantity(self, intent: OrderIntent) -> tuple[float, str | None]:
        """Enforce persisted reduce-only protection against the current book."""
        if not bool((intent.metadata or {}).get("reduce_only")):
            return self._effective_fill_quantity(intent)
        position = self._positions.get(intent.symbol)
        expected_side = "LONG" if intent.side == "SELL" else "SHORT"
        if position is None or position.side != expected_side or position.quantity <= 0:
            return (
                0.0,
                f"Reduce-only {intent.side} has no {expected_side.lower()} "
                f"position for {intent.symbol}",
            )
        return min(float(intent.quantity), float(position.quantity)), None

    def apply_committed_fill(self, plan: PaperFillPlan) -> None:
        """Apply a previously validated fill after its database commit."""
        intent = plan.intent
        quantity = float(plan.fill.quantity)
        price = float(plan.fill.price)
        if intent.side == "BUY":
            self._balance -= plan.account_notional + plan.account_commission
        else:
            self._balance += plan.account_notional - plan.account_commission
        self._update_position(
            intent.symbol,
            intent.side,
            quantity,
            price,
            account_notional=plan.account_notional,
        )
        self._realized_pnl -= plan.account_commission
        self._trade_history.append(dict(plan.trade))
        if plan.result.is_terminal:
            self._orders.pop(str(plan.result.order_id), None)
        else:
            order = self._orders.get(str(plan.result.order_id))
            if order is not None:
                order["filled_quantity"] = float(order.get("filled_quantity") or 0) + quantity

    @property
    def capabilities(self) -> BrokerCapabilities:
        """Advertise only canonical classes implemented by paper execution."""
        return BrokerCapabilities(
            broker_type=BrokerType.PAPER,
            supports_spot=True,
            supports_margin=True,
            supports_futures=True,
            supports_perpetual=True,
            supports_options=True,
            supports_multi_leg=True,
            supports_exact_fill_retrieval=True,
            supported_assets=[
                "crypto",
                "equity",
                "etf",
                "futures",
                "options",
                "fx",
                "commodities",
            ],
            max_leverage=100.0,
            min_order_size=0.0001,
            has_paper_trading=True,
        )

    async def connect(self) -> bool:
        """Paper broker is always connected."""
        self._connected = True
        logger.info(
            "Paper broker connected",
            account_id=self._account_id,
            balance=self._balance,
            currency=self._currency,
        )
        return True

    async def disconnect(self) -> None:
        """Disconnect paper broker."""
        self._connected = False
        logger.info("Paper broker disconnected")

    async def get_account_info(self) -> AccountInfo:
        """Get current account information."""
        positions_list = [replace(position) for position in self._positions.values()]
        equity, available_balance, margin_used, unrealized_pnl = self._account_economics()

        return AccountInfo(
            broker=BrokerType.PAPER,
            account_id=self._account_id,
            equity=equity,
            available_balance=available_balance,
            margin_used=margin_used,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=self._realized_pnl,
            positions=positions_list,
            currency=self._currency,
        )

    def _account_economics(self) -> tuple[float, float, float, float]:
        """Return equity, available balance, margin, and unrealized P&L.

        Spot/options positions exchange premium or principal cash, so their
        marked value remains part of equity. Linear futures exchange only
        variation P&L and fees; their gross contract notional is exposure, not
        an account asset or liability.
        """
        non_futures_long_value = 0.0
        non_futures_short_value = 0.0
        non_futures_margin = 0.0
        futures_unrealized = 0.0
        futures_margin = 0.0
        unrealized_pnl = 0.0
        for position in self._positions.values():
            gross_notional = abs(
                self._position_notional(
                    position.symbol,
                    position.quantity,
                    position.current_price,
                )
            )
            position_unrealized = float(position.unrealized_pnl or 0.0)
            unrealized_pnl += position_unrealized
            leverage = self._futures_leverages.get(position.symbol)
            if leverage is not None:
                futures_unrealized += position_unrealized
                futures_margin += gross_notional / leverage
            else:
                non_futures_margin += gross_notional * 0.1
                if position.side == "LONG":
                    non_futures_long_value += gross_notional
                else:
                    non_futures_short_value += gross_notional

        margin_used = non_futures_margin + futures_margin
        equity = (
            self._balance + non_futures_long_value - non_futures_short_value + futures_unrealized
        )
        available_balance = self._balance - non_futures_margin + futures_unrealized - futures_margin
        return equity, available_balance, margin_used, unrealized_pnl

    async def get_positions(self) -> list[Position]:
        """Get all open positions."""
        return list(self._positions.values())

    async def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Return working resting orders (stop / take-profit / limit).

        Reconciliation matches these against locally-tracked pending orders (by
        order_id); exposing them keeps a paper resting order from being flagged
        missing-at-broker every cycle (M1). Market orders fill on submission and
        never appear here.
        """
        orders = list(self._orders.values())
        if symbol is not None:
            orders = [order for order in orders if order.get("symbol") == symbol]
        return orders

    async def submit_order(  # noqa: PLR0911, PLR0912
        self,
        intent: OrderIntent,
    ) -> BrokerOrderResult:
        """
        Submit and simulate order execution.

        Args:
            intent: Order intent with details

        Returns:
            Simulated order result
        """
        # Validate order
        validation_error = self.validate_order(intent)
        if validation_error:
            return BrokerOrderResult.rejected(validation_error)
        try:
            futures_terms = self._futures_sim._resolve_linear_futures_terms(intent)
        except ValueError as exc:
            return BrokerOrderResult.rejected(str(exc), code="contract_terms_invalid")
        try:
            currency_context = self._require_currency_context(intent)
        except (TypeError, ValueError) as exc:
            return BrokerOrderResult.rejected(str(exc), code="currency_context_invalid")

        # Generate order ID
        order_id = str(uuid.uuid4())[:12]

        # Resting orders (stop / take-profit / limit) are NOT filled on
        # submission. A real broker holds them until the market price triggers
        # them; the paper simulator accepts them as working orders so they do
        # not count as realized trades. Only market orders fill immediately.
        if intent.order_type and intent.order_type != "market":
            # Track the working order so reconciliation can see it as an open
            # broker order (M1). Without this, every resting stop/take-profit
            # looked missing-at-broker each cycle (missing_broker_open_order) and
            # its pending_orders row never reconciled. cancel_order/reset already
            # read self._orders; this is the missing writer.
            self._orders[order_id] = {
                "order_id": order_id,
                "symbol": intent.symbol,
                "side": intent.side,
                "order_type": intent.order_type,
                "quantity": intent.quantity,
                "limit_price": intent.limit_price,
                "stop_price": intent.stop_price,
                "metadata": dict(intent.metadata or {}),
                "status": "working",
            }
            logger.info(
                "Paper order accepted (resting %s): %s %.4f %s",
                intent.order_type,
                intent.side,
                intent.quantity,
                intent.symbol,
            )
            return BrokerOrderResult.accepted(order_id=order_id)

        # Resolve an observed/reference price; never invent a numeric fill.
        price = self._get_price(intent.symbol)
        if price is None:
            reference = None
            if intent.metadata:
                reference = intent.metadata.get("reference_price") or intent.metadata.get("price")
            if reference is not None:
                try:
                    price = float(reference)
                    self._prices[intent.symbol] = price
                except (TypeError, ValueError):
                    price = None
            # Use limit price if available, otherwise reject
            if intent.limit_price:
                price = intent.limit_price
            else:
                return BrokerOrderResult.rejected(f"No price available for {intent.symbol}")

        # Resolve per-instrument cost overrides (falls back to defaults)
        slip_pct = self._get_slippage_pct(intent.symbol)
        comm_pct = self._get_commission_pct(intent.symbol)

        # Apply slippage for market orders
        if intent.order_type == "market":
            fill_price = price * (1 + slip_pct) if intent.side == "BUY" else price * (1 - slip_pct)
        else:
            fill_price = intent.limit_price or price

        fill_quantity, position_error = self._effective_fill_quantity(
            intent,
            allow_reversal=futures_terms is not None,
        )
        if position_error:
            return BrokerOrderResult.rejected(position_error)
        if futures_terms is not None:
            return self._futures_sim._submit_linear_futures_fill(
                intent=intent,
                order_id=order_id,
                fill_quantity=fill_quantity,
                fill_price=fill_price,
                commission_pct=comm_pct,
                currency_context=currency_context,
                terms=futures_terms,
            )

        # Calculate commission
        notional = _decimal_product(fill_quantity, fill_price)
        commission = _decimal_product(notional, comm_pct)
        account_notional = self._settlement_to_account(currency_context, notional)
        account_commission = self._settlement_to_account(currency_context, commission)

        # Check if we have enough balance
        if intent.side == "BUY":
            cost = account_notional + account_commission
            if cost > self._balance:
                return BrokerOrderResult.rejected(
                    f"Insufficient balance: need {cost:.2f} {self._currency}, "
                    f"have {self._balance:.2f} {self._currency}"
                )
        replay_context = resolve_historical_replay_fill_context(intent.metadata)
        fill_timestamp, source_provenance = replay_context or (datetime.now(tz=UTC), None)
        trade = {
            "trade_id": f"{order_id}:fill:1",
            "order_id": order_id,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": fill_quantity,
            "price": fill_price,
            "commission": commission,
            "commission_currency": currency_context.settlement_currency,
            "account_notional": account_notional,
            "account_commission": account_commission,
            "account_currency": self._currency,
            "timestamp": fill_timestamp,
        }
        if source_provenance is not None:
            trade["source_provenance"] = source_provenance
        result = BrokerOrderResult(
            success=True,
            order_id=order_id,
            status=OrderStatus.FILLED,
            filled_quantity=fill_quantity,
            filled_price=fill_price,
            commission=commission,
            commission_currency=currency_context.settlement_currency,
            timestamp=fill_timestamp,
            raw_response=source_provenance,
        )
        self._commit_fill_before_state(intent, result=result, trade=trade)

        if intent.side == "BUY":
            self._balance -= cost
        else:
            self._balance += account_notional - account_commission

        # Update or create position only after exact economics are durable.
        self._update_position(
            intent.symbol,
            intent.side,
            fill_quantity,
            fill_price,
            account_notional=account_notional,
        )

        # Realized P&L is net of fees (PL-3): every fill's commission reduces it,
        # so a round trip's realized equals the price move minus entry+exit fees,
        # matching the commission-inclusive FIFO / strategy-metrics surfaces.
        # (Cash balance above already nets commission; this is the separate P&L view.)
        self._realized_pnl -= account_commission
        self._trade_history.append(trade)

        logger.info(
            "Paper trade executed: %s %s %.4f @ %.4f (commission: %.2f)",
            intent.side,
            intent.symbol,
            fill_quantity,
            fill_price,
            commission,
        )

        return result

    async def submit_options_order(self, intent: OptionsIntent) -> BrokerOrderResult:
        """
        Submit and simulate options order execution.

        Args:
            intent: Options order intent with legs

        Returns:
            Simulated order result
        """
        if not intent.legs:
            return BrokerOrderResult.rejected("No option legs specified")

        if len(intent.legs) == 1:
            return self._options_sim._submit_single_leg_options_order(intent)

        return self._options_sim._submit_multi_leg_options_order(intent)

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order (always succeeds for paper)."""
        if order_id in self._orders:
            del self._orders[order_id]
            logger.info("Paper order cancelled: %s", order_id)
            return True
        return False

    async def get_order_status(self, order_id: str) -> BrokerOrderResult:
        """Get order status."""
        # All paper orders are immediately filled
        for trade in self._trade_history:
            if trade["order_id"] == order_id:
                provenance = (
                    dict(trade["source_provenance"])
                    if isinstance(trade.get("source_provenance"), dict)
                    else None
                )
                return BrokerOrderResult(
                    success=True,
                    order_id=order_id,
                    status=OrderStatus.FILLED,
                    filled_quantity=trade["quantity"],
                    filled_price=trade["price"],
                    commission=trade["commission"],
                    commission_currency=trade["commission_currency"],
                    timestamp=trade["timestamp"],
                    raw_response=provenance,
                )

        return BrokerOrderResult.rejected(f"Order not found: {order_id}")

    async def get_fills(self, order_id: str) -> list[BrokerFill]:
        """Return exact local-paper fills for one resolver-owned order."""
        return [
            BrokerFill(
                trade_id=str(trade["trade_id"]),
                order_id=str(trade["order_id"]),
                symbol=str(trade["symbol"]),
                side=str(trade["side"]),
                quantity=Decimal(str(trade["quantity"])),
                price=Decimal(str(trade["price"])),
                commission=Decimal(str(trade["commission"])),
                commission_currency=str(trade["commission_currency"]),
                timestamp=trade["timestamp"],
                venue="paper",
                raw_response=(
                    dict(trade["source_provenance"])
                    if isinstance(trade.get("source_provenance"), dict)
                    else None
                ),
            )
            for trade in self._trade_history
            if trade["order_id"] == order_id
        ]

    def set_currency_context(self, symbol: str, context: OrderCurrencyContext) -> None:
        """Set the observed conversion used to value a paper instrument."""
        normalized_symbol = str(symbol or "").strip()
        if not normalized_symbol:
            msg = "Paper order currency context requires a symbol"
            raise ValueError(msg)
        self._validate_currency_context(context)
        previous = self._currency_contexts.get(normalized_symbol)
        if previous is not None and previous.settlement_currency != context.settlement_currency:
            msg = (
                f"Paper instrument {normalized_symbol} settlement currency changed "
                f"from {previous.settlement_currency} to {context.settlement_currency}"
            )
            raise ValueError(msg)
        self._currency_contexts[normalized_symbol] = context
        position = self._positions.get(normalized_symbol)
        if position is not None:
            self._update_position_prices(normalized_symbol, position.current_price)

    def _validate_currency_context(self, context: OrderCurrencyContext) -> None:
        if not isinstance(context, OrderCurrencyContext):
            msg = "Paper execution requires an explicit order currency context"
            raise TypeError(msg)
        if context.account_currency != self._currency:
            msg = (
                f"Paper order account currency {context.account_currency} conflicts "
                f"with broker account currency {self._currency}"
            )
            raise ValueError(msg)

    def _require_currency_context(
        self,
        intent: OrderIntent | OptionsIntent,
    ) -> OrderCurrencyContext:
        context = intent.currency_context
        if context is None:
            msg = "Paper execution requires an explicit order currency context"
            raise ValueError(msg)
        self.set_currency_context(intent.symbol, context)
        return context

    @staticmethod
    def _settlement_to_account(
        context: OrderCurrencyContext,
        amount: Decimal | float,
    ) -> float:
        decimal_amount = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        converted = context.settlement_to_account(decimal_amount)
        if not converted.is_finite():
            msg = "Paper execution produced a non-finite account-currency amount"
            raise ValueError(msg)
        return float(converted)

    def set_price(self, symbol: str, price: float) -> None:
        """Set price for a symbol (for simulation)."""
        self._prices[symbol] = price
        self._update_position_prices(symbol, price)

    def _get_price(self, symbol: str) -> float | None:
        """Get current price for a symbol."""
        return self._prices.get(symbol)

    def _update_position(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        *,
        account_notional: float,
        allow_reversal: bool | None = None,
    ) -> float:
        """Update a position with an account-currency cash-flow basis."""
        if not math.isfinite(account_notional) or account_notional < 0:
            msg = "Paper position update requires finite non-negative account notional"
            raise ValueError(msg)
        if symbol in self._positions:
            pos = self._positions[symbol]
            basis = self._position_account_basis.get(symbol)
            if basis is None:
                msg = f"Paper position {symbol} omitted account-currency basis"
                raise ValueError(msg)

            if (pos.side == "LONG" and side == "BUY") or (pos.side == "SHORT" and side == "SELL"):
                # Adding to position
                total_qty = pos.quantity + quantity
                avg_price = (pos.entry_price * pos.quantity + price * quantity) / total_qty
                pos.quantity = total_qty
                pos.entry_price = avg_price
                self._position_account_basis[symbol] = basis + account_notional
                self._update_position_prices(symbol, price)
                return 0.0
            # Reducing or reversing position
            closed_quantity = min(quantity, pos.quantity)
            fill_fraction = closed_quantity / quantity
            position_fraction = closed_quantity / pos.quantity
            closing_notional = account_notional * fill_fraction
            closed_basis = basis * position_fraction
            pnl = (
                closing_notional - closed_basis
                if pos.side == "LONG"
                else closed_basis - closing_notional
            )
            self._realized_pnl += pnl
            pos.realized_pnl = (pos.realized_pnl or 0.0) + pnl
            if quantity >= pos.quantity:
                remaining = quantity - pos.quantity
                reversal_allowed = (
                    self._allow_reversal if allow_reversal is None else allow_reversal
                )
                if remaining > 1e-12 and reversal_allowed:  # noqa: PLR2004
                    # Open reverse position (margin/futures; opt-in only).
                    new_side = "SHORT" if side == "SELL" else "LONG"
                    remaining_notional = max(0.0, account_notional - closing_notional)
                    self._positions[symbol] = Position(
                        symbol=symbol,
                        side=new_side,
                        quantity=remaining,
                        entry_price=price,
                        current_price=price,
                        unrealized_pnl=0.0,
                        opened_at=datetime.now(tz=UTC),
                    )
                    self._position_account_basis[symbol] = remaining_notional
                    self._update_position_prices(symbol, price)
                else:
                    if remaining > 1e-12:  # noqa: PLR2004
                        # Spot long-only: drop the oversell excess, close to flat.
                        logger.warning(
                            "Oversell clamped to flat for %s: requested %.8f vs held "
                            "%.8f; excess %.8f dropped (spot long-only, no reverse)",
                            symbol,
                            quantity,
                            pos.quantity,
                            remaining,
                        )
                    del self._positions[symbol]
                    self._position_account_basis.pop(symbol, None)
            else:
                # Partial close
                pos.quantity -= quantity
                self._position_account_basis[symbol] = max(0.0, basis - closed_basis)
                self._update_position_prices(symbol, price)
            return pnl
        # New position
        new_side = "LONG" if side == "BUY" else "SHORT"
        self._positions[symbol] = Position(
            symbol=symbol,
            side=new_side,
            quantity=quantity,
            entry_price=price,
            current_price=price,
            unrealized_pnl=0.0,
            opened_at=datetime.now(tz=UTC),
        )
        self._position_account_basis[symbol] = account_notional
        self._update_position_prices(symbol, price)
        return 0.0

    def _update_position_prices(self, symbol: str, price: float) -> None:
        """Update position with new price."""
        if symbol in self._positions:
            pos = self._positions[symbol]
            pos.current_price = price
            multiplier = self._contract_multipliers.get(symbol)
            pos.quantity_unit = "contracts" if multiplier is not None else "asset"
            pos.contract_multiplier = multiplier
            leverage = self._futures_leverages.get(symbol)
            pos.leverage = leverage
            pos.contract_type = self._futures_contract_types.get(symbol)
            market_value = abs(self._position_notional(symbol, pos.quantity, price))
            pos.gross_notional = market_value
            pos.notional_currency = self._currency
            pos.margin_used = market_value / leverage if leverage is not None else None
            basis = self._position_account_basis.get(symbol)
            if basis is None:
                msg = f"Paper position {symbol} omitted account-currency basis"
                raise ValueError(msg)
            if pos.side == "LONG":
                pos.unrealized_pnl = market_value - basis
            else:
                pos.unrealized_pnl = basis - market_value

    def _get_slippage_pct(self, symbol: str) -> float:
        """Get slippage percentage for a symbol (per-instrument or default)."""
        overrides = self._instrument_costs.get(symbol)
        if overrides and "slippage_pct" in overrides:
            return float(overrides["slippage_pct"])
        return self._slippage_pct

    def _get_commission_pct(self, symbol: str) -> float:
        """Get commission percentage for a symbol (per-instrument or default)."""
        overrides = self._instrument_costs.get(symbol)
        if overrides and "commission_pct" in overrides:
            return float(overrides["commission_pct"])
        return self._commission_pct

    def _effective_fill_quantity(
        self,
        intent: OrderIntent,
        *,
        allow_reversal: bool = False,
    ) -> tuple[float, str | None]:
        """Clamp spot sells to held inventory before any economic mutation."""
        if allow_reversal or self._allow_reversal or intent.side != "SELL":
            return intent.quantity, None
        position = self._positions.get(intent.symbol)
        held_quantity = (
            position.quantity if position is not None and position.side == "LONG" else 0.0
        )
        if held_quantity <= 0:
            return (
                0.0,
                f"Insufficient position: cannot sell {intent.symbol} without a held long",
            )
        if intent.quantity > held_quantity:
            logger.warning(
                "Paper oversell clamped before fill: %s requested %.8f, held %.8f",
                intent.symbol,
                intent.quantity,
                held_quantity,
            )
        return min(intent.quantity, held_quantity), None

    def get_trade_history(self) -> list[dict[str, Any]]:
        """Get all trade history."""
        return list(self._trade_history)

    def get_pnl_summary(self) -> dict[str, float]:
        """Get P&L summary."""
        current_equity, _available, _margin, unrealized_pnl = self._account_economics()

        return {
            "starting_balance": self._starting_balance,
            "current_balance": self._balance,
            "realized_pnl": self._realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": current_equity - self._starting_balance,
            "return_pct": (current_equity - self._starting_balance) / self._starting_balance * 100,
        }

    def load_positions(
        self,
        positions: list[dict[str, Any]],
        *,
        adjust_cash: bool = True,
    ) -> None:
        """Rehydrate open positions from the persisted book (restart-safety).

        A paper broker is process-memory only, so after a service restart a
        freshly-constructed PaperBroker would report zero positions and full
        starting balance — making open positions unclosable (the CLOSE path reads
        ``get_account_info().positions``) and resetting exposure. Production
        restart state is replayed from canonical ``executions`` plus each order's
        persisted FX provenance. ``positions`` and ``execution_metrics`` are
        downstream projections and are never accounting inputs. The replay
        supplies ``account_cost_basis`` so no historical currency or price is
        inferred here.

        Idempotent: only seeds symbols not already tracked, so re-seeding a warm
        broker is a no-op.
        """
        validated = [
            seed for entry in positions if (seed := self._validate_position_seed(entry)) is not None
        ]

        for entry in validated:
            symbol = entry["symbol"]
            multiplier = entry["contract_multiplier"]
            if multiplier is not None:
                self._contract_multipliers[symbol] = multiplier
            leverage = entry["leverage"]
            contract_type = entry["contract_type"]
            if leverage is not None:
                self._futures_leverages[symbol] = leverage
                self._futures_contract_types[symbol] = contract_type
            unrealized_pnl = (
                entry["gross_notional"] - entry["account_basis"]
                if entry["side"] == "LONG"
                else entry["account_basis"] - entry["gross_notional"]
            )
            self._positions[symbol] = Position(
                symbol=symbol,
                side=entry["side"],
                quantity=entry["quantity"],
                entry_price=entry["entry_price"],
                current_price=entry["current_price"],
                unrealized_pnl=unrealized_pnl,
                opened_at=datetime.now(tz=UTC),
                quantity_unit=entry["quantity_unit"],
                contract_multiplier=multiplier,
                leverage=leverage,
                contract_type=contract_type,
                gross_notional=entry["gross_notional"],
                notional_currency=entry["notional_currency"],
            )
            self._position_account_basis[symbol] = entry["account_basis"]
            self._prices.setdefault(symbol, entry["current_price"] or entry["entry_price"])
            if adjust_cash and leverage is None:
                # A configured initial account has not yet reflected its open
                # positions in cash.  Persisted account snapshots already include
                # that cash effect and therefore pass ``adjust_cash=False``.
                opening_notional = entry["account_basis"]
                self._balance += -opening_notional if entry["side"] == "LONG" else opening_notional
            if symbol in self._currency_contexts:
                self._update_position_prices(
                    symbol,
                    entry["current_price"] or entry["entry_price"],
                )

    def _validate_position_seed(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """Validate one canonical replay position without mutating broker state."""
        symbol = str(entry.get("symbol") or "")
        quantity = abs(float(entry.get("quantity") or 0.0))
        if not symbol or symbol in self._positions or quantity < 1e-12:  # noqa: PLR2004
            return None
        side = normalize_position_side(entry.get("side")).upper()
        entry_price = float(entry.get("entry_price") or 0.0)
        current_price = float(entry.get("current_price") or entry_price)
        if (
            not math.isfinite(entry_price)
            or not math.isfinite(current_price)
            or entry_price <= 0
            or current_price <= 0
        ):
            msg = "Position rehydration requires positive finite prices"
            raise ValueError(msg)
        quantity_unit = str(entry.get("quantity_unit") or "").strip().lower()
        if quantity_unit not in {"asset", "contracts"}:
            msg = f"Unknown paper position quantity unit: {quantity_unit!r}"
            raise ValueError(msg)
        multiplier = self._seed_contract_multiplier(entry, quantity_unit)
        leverage, contract_type = self._futures_sim._seed_futures_terms(
            entry,
            quantity_unit=quantity_unit,
            contract_multiplier=multiplier,
        )
        raw_notional = entry.get("gross_notional")
        if raw_notional in (None, ""):
            msg = "Position rehydration requires an explicit gross_notional"
            raise ValueError(msg)
        gross_notional = float(str(raw_notional))
        if not math.isfinite(gross_notional) or gross_notional <= 0:
            msg = "Position rehydration requires a positive gross_notional"
            raise ValueError(msg)
        notional_currency = str(entry.get("notional_currency") or "").strip().upper()
        if notional_currency != self._currency:
            msg = (
                f"Paper position {symbol} notional currency "
                f"{notional_currency or '<missing>'} conflicts with account "
                f"currency {self._currency}"
            )
            raise ValueError(msg)
        raw_account_basis = entry.get("account_cost_basis")
        if raw_account_basis in (None, ""):
            msg = "Position rehydration requires explicit account-currency basis"
            raise ValueError(msg)
        account_basis = float(str(raw_account_basis))
        if not math.isfinite(account_basis) or account_basis <= 0:
            msg = "Position rehydration requires a positive account-currency basis"
            raise ValueError(msg)
        return {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "current_price": current_price,
            "quantity_unit": quantity_unit,
            "contract_multiplier": multiplier,
            "leverage": leverage,
            "contract_type": contract_type,
            "gross_notional": gross_notional,
            "notional_currency": notional_currency,
            "account_basis": account_basis,
        }

    @staticmethod
    def _seed_contract_multiplier(
        entry: dict[str, Any],
        quantity_unit: str,
    ) -> float | None:
        """Validate an optional position contract multiplier."""
        if quantity_unit != "contracts":
            return None
        try:
            multiplier = float(str(entry.get("contract_multiplier")))
        except (TypeError, ValueError) as exc:
            msg = "Contract position rehydration requires a positive multiplier"
            raise ValueError(msg) from exc
        if not math.isfinite(multiplier) or multiplier <= 0:
            msg = "Contract position rehydration requires a positive multiplier"
            raise ValueError(msg)
        return multiplier

    def reset(self) -> None:
        """Reset paper broker to initial state."""
        self._balance = self._initial_cash
        self._realized_pnl = self._initial_realized_pnl
        self._positions.clear()
        self._orders.clear()
        self._trade_history.clear()
        self._prices.clear()
        self._contract_multipliers.clear()
        self._futures_leverages.clear()
        self._futures_contract_types.clear()
        self._currency_contexts.clear()
        self._position_account_basis.clear()
        logger.info("Paper broker reset to initial state")

    def _position_notional(self, symbol: str, quantity: float, price: float) -> float:
        """Compute account-currency notional using the latest observed context."""
        settlement_notional = quantity * price * self._contract_multipliers.get(symbol, 1.0)
        context = self._currency_contexts.get(symbol)
        if context is not None:
            return self._settlement_to_account(context, settlement_notional)
        position = self._positions.get(symbol)
        if (
            position is not None
            and position.gross_notional is not None
            and position.notional_currency == self._currency
            and math.isclose(price, position.current_price, rel_tol=1e-12, abs_tol=1e-12)
            and position.quantity > 0
        ):
            # Restart recovery may load an already account-normalized mark
            # before the next observed FX context is delivered.
            return float(position.gross_notional) * quantity / position.quantity
        msg = f"Paper position {symbol} has no observed settlement-to-account FX context"
        raise ValueError(msg)
