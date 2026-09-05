"""
PnL (Profit and Loss) computation service.

Computes realized and unrealized PnL from execution records.
Supports FIFO (First-In-First-Out) accounting method.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import SQLAlchemyError

from lib_common.logging import get_logger

from .canonical_executions import (
    CanonicalExecutionCurrencyError,
    CanonicalExecutionLedgerError,
    convert_canonical_executions,
    load_canonical_executions,
)
from .fx_rates import (
    FXConverter,
    FXRateProvider,
    FXRateUnavailableError,
    normalize_currency,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = get_logger(__name__)


class PnLLedgerUnavailableError(RuntimeError):
    """The realized-fill ledger could not be read completely and safely."""


class CurrencyConversionRequiredError(RuntimeError):
    """A monetary value cannot be normalized to the account currency safely."""


@dataclass
class PositionSnapshot:
    """Snapshot of a position's P&L state."""

    symbol: str
    user_id: str
    strategy_id: str | None
    account_id: int
    currency: str
    quantity: Decimal
    average_cost: Decimal
    current_price: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    cost_basis: Decimal
    market_value: Decimal
    return_pct: Decimal
    total_deployed: Decimal  # Total capital deployed (for lifetime ROI calculation)
    updated_at: datetime
    # False when no current market price was available: unrealized P&L is marked
    # to cost basis (0) rather than fabricated from a 0 price. Realized P&L is
    # unaffected. Consumers should treat unrealized/market_value as stale.
    price_available: bool = True


@dataclass
class TradeRecord:
    """Record of an individual trade for P&L calculation."""

    trade_id: str
    exec_id: int
    canonical_signal_id: int
    symbol: str
    side: str  # "buy" or "sell"
    quantity: Decimal
    price: Decimal
    commission: Decimal
    account_currency: str
    settlement_currency: str
    commission_currency: str | None
    timestamp: datetime
    order_id: str | None = None
    quantity_unit: str = "asset"
    contract_multiplier: Decimal | None = None


@dataclass
class FIFOLot:
    """A lot of shares/units for FIFO accounting."""

    entry_exec_id: int
    entry_canonical_signal_id: int
    quantity: Decimal
    price: Decimal
    timestamp: datetime
    account_currency: str
    entry_commission: Decimal = Decimal("0")  # Commission paid when opening this lot
    remaining: Decimal = field(init=False)

    def __post_init__(self) -> None:
        self.remaining = self.quantity


@dataclass(frozen=True)
class FIFORealizedPnLContribution:
    """Immutable economics for one FIFO entry-lot portion closed by one fill."""

    entry_exec_id: int
    exit_exec_id: int
    entry_canonical_signal_id: int
    exit_canonical_signal_id: int
    quantity: Decimal
    net_realized_pnl: Decimal
    deployed_entry_capital: Decimal
    account_currency: str
    exit_time: datetime


class PnLService:
    """
    Service for computing Profit and Loss metrics.

    Uses FIFO (First-In-First-Out) method for cost basis calculation.
    Supports both realized (closed trades) and unrealized (open positions) P&L.

    Usage:
        pnl_service = PnLService(session_factory=get_session)

        # Get position P&L
        snapshot = await pnl_service.get_position_pnl(
            user_id="user_001",
            symbol="BTCUSD",
            account_id=101,
            current_price=Decimal("45000.00"),
        )

        # Get aggregate P&L
        totals = await pnl_service.get_aggregate_pnl(
            user_id="user_001",
            account_id=101,
        )
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Any]] | None = None,
        market_data_provider: Any | None = None,
        fx_rate_provider: FXRateProvider | None = None,
    ) -> None:
        """
        Initialize PnL service.

        Args:
            session_factory: Factory for database sessions
            market_data_provider: Provider for current market prices
        """
        self._session_factory = session_factory
        self._market_data = market_data_provider
        self._fx_converter = FXConverter(fx_rate_provider) if fx_rate_provider is not None else None

    async def get_position_pnl(
        self,
        user_id: str,
        symbol: str,
        *,
        account_id: int,
        current_price: Decimal | None = None,
        strategy_id: str | None = None,
    ) -> PositionSnapshot:
        """
        Compute P&L for a specific position.

        Args:
            user_id: User identifier
            symbol: Trading symbol
            current_price: Current market price (fetched if not provided)
            strategy_id: Optional strategy filter
            account_id: Linked broker account that owns the position

        Returns:
            PositionSnapshot with P&L metrics
        """
        # Fetch executions
        trades = await self._get_trades(
            user_id,
            symbol,
            account_id=account_id,
            strategy_id=strategy_id,
        )
        account_currency = (
            trades[0].account_currency
            if trades
            else self._resolve_account_currency(user_id=user_id, account_id=account_id)
        )

        # Get current price if not provided (may be None when market data is
        # unavailable — do NOT fabricate a 0 price).
        price_available = True
        if current_price is None:
            current_price = await self._get_current_price(
                symbol,
                user_id=user_id,
                environment=self._resolve_account_environment(
                    user_id=user_id,
                    account_id=account_id,
                ),
            )
            price_available = current_price is not None
        if current_price is not None and trades:
            multipliers = {
                trade.contract_multiplier
                for trade in trades
                if trade.contract_multiplier is not None
            }
            if len(multipliers) > 1:
                msg = f"Contract multiplier changed within the {symbol} P&L ledger"
                raise PnLLedgerUnavailableError(msg)
            if multipliers:
                current_price *= next(iter(multipliers))
            current_price = self._convert_money(
                current_price,
                from_currency=trades[-1].settlement_currency,
                to_currency=account_currency,
                as_of=datetime.now(tz=UTC),
            )

        # Calculate FIFO P&L
        realized_pnl, position_qty, avg_cost, _lots, _contributions = self._calculate_fifo_pnl(
            trades
        )

        # Calculate unrealized P&L
        cost_basis = position_qty * avg_cost
        if price_available and current_price is not None:
            market_value = position_qty * current_price
            unrealized_pnl = market_value - cost_basis
        else:
            # No current price: mark to cost basis so unrealized P&L is 0 rather
            # than -cost_basis (a fabricated total loss from a 0 price). Realized
            # P&L is unaffected. Flag the snapshot so consumers know it is stale.
            if position_qty != 0:
                logger.warning(
                    "Current price unavailable for %s; unrealized P&L marked to cost basis (stale)",
                    symbol,
                )
            current_price = avg_cost
            market_value = cost_basis
            unrealized_pnl = Decimal("0")

        # Total P&L
        total_pnl = realized_pnl + unrealized_pnl

        # Return percentage based on total capital deployed
        # For accurate return %, we need cumulative cost of all closed + open positions
        total_deployed = self._calculate_total_deployed(trades)
        if total_deployed > 0:
            return_pct = total_pnl / total_deployed * 100
        elif abs(cost_basis) > 0:
            # Fallback to current cost basis for open positions
            return_pct = total_pnl / abs(cost_basis) * 100
        else:
            return_pct = Decimal("0")

        return PositionSnapshot(
            symbol=symbol,
            user_id=user_id,
            strategy_id=strategy_id,
            account_id=account_id,
            currency=account_currency,
            quantity=position_qty,
            average_cost=avg_cost,
            current_price=current_price,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_pnl=total_pnl,
            cost_basis=cost_basis,
            market_value=market_value,
            return_pct=return_pct,
            total_deployed=total_deployed,
            updated_at=datetime.now(tz=UTC),
            price_available=price_available,
        )

    def partition_realized_pnl(
        self,
        user_id: str,
        symbol: str,
        *,
        account_id: int,
        strategy_id: str | None = None,
    ) -> Decimal:
        """Cumulative P&L for one account/strategy/symbol partition (synchronous).

        ExecutionMetricsStore diffs the realized P&L it is handed against the
        previous row in the same account/strategy/symbol/mode partition. Each
        partition therefore receives only its own cumulative FIFO total.
        """
        realized, _contributions = self._partition_realized_pnl_economics(
            user_id,
            symbol,
            account_id=account_id,
            strategy_id=strategy_id,
        )
        return realized

    def realized_pnl_contributions_for_exit_signal(
        self,
        user_id: str,
        symbol: str,
        *,
        account_id: int,
        strategy_id: str,
        exit_canonical_signal_id: int,
    ) -> list[FIFORealizedPnLContribution]:
        """Return exact FIFO contributions closed by one canonical signal.

        The complete account/strategy/symbol ledger is replayed before filtering
        because an exit can close entry lots from any earlier fill in the same
        partition. An empty result is valid when the requested signal only opens
        exposure or has no exact fills.
        """
        _realized, contributions = self.partition_realized_pnl_with_exit_contributions(
            user_id,
            symbol,
            account_id=account_id,
            strategy_id=strategy_id,
            exit_canonical_signal_id=exit_canonical_signal_id,
        )
        return contributions

    def partition_realized_pnl_with_exit_contributions(
        self,
        user_id: str,
        symbol: str,
        *,
        account_id: int,
        strategy_id: str,
        exit_canonical_signal_id: int,
    ) -> tuple[Decimal, list[FIFORealizedPnLContribution]]:
        """Load once and return cumulative P&L plus one exit's contributions."""
        normalized_strategy = self._validate_contribution_lookup(
            strategy_id=strategy_id,
            exit_canonical_signal_id=exit_canonical_signal_id,
        )
        realized, contributions = self._partition_realized_pnl_economics(
            user_id,
            symbol,
            account_id=account_id,
            strategy_id=normalized_strategy,
        )
        return realized, [
            item
            for item in contributions
            if item.exit_canonical_signal_id == exit_canonical_signal_id
        ]

    def _partition_realized_pnl_economics(
        self,
        user_id: str,
        symbol: str,
        *,
        account_id: int,
        strategy_id: str | None,
    ) -> tuple[Decimal, list[FIFORealizedPnLContribution]]:
        trades = self._load_trades(
            user_id,
            symbol,
            account_id=account_id,
            strategy_id=strategy_id,
        )
        realized, _qty, _avg_cost, _lots, contributions = self._calculate_fifo_pnl(trades)
        return realized, contributions

    @staticmethod
    def _validate_contribution_lookup(
        *,
        strategy_id: str,
        exit_canonical_signal_id: int,
    ) -> str:
        normalized_strategy = str(strategy_id).strip()
        if not normalized_strategy:
            msg = "Realized P&L contribution lookup requires an exact strategy_id"
            raise PnLLedgerUnavailableError(msg)
        if (
            isinstance(exit_canonical_signal_id, bool)
            or not isinstance(exit_canonical_signal_id, int)
            or exit_canonical_signal_id <= 0
        ):
            msg = "Realized P&L contribution lookup requires a positive canonical signal ID"
            raise PnLLedgerUnavailableError(msg)
        return normalized_strategy

    async def get_fifo_positions(
        self,
        user_id: str,
        *,
        account_id: int,
        strategy_id: str | None = None,
        broker: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Decimal]:
        """Net open FIFO position quantity per symbol (signed: + long, - short).

        Computed from exact canonical ``executions`` via the same
        ``_calculate_fifo_pnl`` matcher — needs no current price. Used to
        reconcile our independent ledger position against the broker's reported
        position (PL-2). ``broker``/``mode`` scope the ledger to the broker +
        run-environment being reconciled, so fills on another broker (or in
        paper) don't pollute the comparison. Flat (zero) symbols are omitted so
        only real open positions are compared.
        """
        symbols = await self._get_traded_symbols(
            user_id,
            account_id=account_id,
            strategy_id=strategy_id,
            broker=broker,
            mode=mode,
        )
        positions: dict[str, Decimal] = {}
        for symbol in symbols:
            trades = await self._get_trades(
                user_id,
                symbol,
                account_id=account_id,
                strategy_id=strategy_id,
                broker=broker,
                mode=mode,
            )
            _realized, position_qty, _avg_cost, _lots, _contributions = self._calculate_fifo_pnl(
                trades
            )
            if position_qty != 0:
                positions[symbol] = position_qty
        return positions

    async def get_aggregate_pnl(
        self,
        user_id: str,
        *,
        account_id: int,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Compute aggregate P&L across all positions.

        Args:
            user_id: User identifier
            strategy_id: Optional strategy filter
            account_id: Linked broker account whose ledger is aggregated

        Returns:
            Dictionary with aggregate P&L metrics
        """
        # Get all symbols with trades
        symbols = await self._get_traded_symbols(
            user_id,
            account_id=account_id,
            strategy_id=strategy_id,
        )

        total_realized = Decimal("0")
        total_unrealized = Decimal("0")
        total_cost_basis = Decimal("0")
        total_market_value = Decimal("0")
        # Track total capital deployed across all positions (for lifetime ROI)
        total_deployed_capital = Decimal("0")
        positions: list[PositionSnapshot] = []
        # Symbols whose P&L could not be computed (data/Decimal error) or whose
        # current price was unavailable — so the aggregate is explicitly flagged
        # partial/stale instead of silently under-reporting.
        failed_symbols: list[str] = []
        stale_symbols: list[str] = []

        for symbol in symbols:
            try:
                snapshot = await self.get_position_pnl(
                    user_id=user_id,
                    symbol=symbol,
                    strategy_id=strategy_id,
                    account_id=account_id,
                )
                positions.append(snapshot)
                total_realized += snapshot.realized_pnl
                total_unrealized += snapshot.unrealized_pnl
                total_cost_basis += snapshot.cost_basis
                total_market_value += snapshot.market_value
                # Sum total deployed capital (includes closed + open positions)
                total_deployed_capital += snapshot.total_deployed
                if not snapshot.price_available and snapshot.quantity != 0:
                    stale_symbols.append(symbol)
            except (CurrencyConversionRequiredError, PnLLedgerUnavailableError):
                raise
            except Exception:
                logger.exception("Error computing P&L for %s", symbol)
                failed_symbols.append(symbol)

        total_pnl = total_realized + total_unrealized
        # Use total deployed capital for lifetime ROI (handles closed + open positions)
        total_return_pct = (
            (total_pnl / total_deployed_capital * 100)
            if total_deployed_capital > 0
            else Decimal("0")
        )

        return {
            "user_id": user_id,
            "strategy_id": strategy_id,
            "account_id": account_id,
            "currency": self._resolve_account_currency(
                user_id=user_id,
                account_id=account_id,
            ),
            "total_realized_pnl": float(total_realized),
            "total_unrealized_pnl": float(total_unrealized),
            "total_pnl": float(total_pnl),
            "total_cost_basis": float(total_cost_basis),
            "total_market_value": float(total_market_value),
            "total_return_pct": float(total_return_pct),
            "position_count": len([p for p in positions if p.quantity != 0]),
            # Explicit partial/stale signalling so consumers (risk gates,
            # dashboards) never treat an incomplete aggregate as complete.
            "complete": not failed_symbols,
            "failed_symbols": failed_symbols,
            "stale_price_symbols": stale_symbols,
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": float(p.quantity),
                    "realized_pnl": float(p.realized_pnl),
                    "unrealized_pnl": float(p.unrealized_pnl),
                    "total_pnl": float(p.total_pnl),
                }
                for p in positions
            ],
            "updated_at": datetime.now(tz=UTC).isoformat(),
        }

    def _calculate_fifo_pnl(
        self, trades: list[TradeRecord]
    ) -> tuple[
        Decimal,
        Decimal,
        Decimal,
        list[FIFOLot],
        list[FIFORealizedPnLContribution],
    ]:
        """
        Calculate P&L using FIFO method.

        Handles both long and short positions:
        - Long: buy opens, sell closes
        - Short: sell opens, buy closes

        Commissions are tracked at both entry and exit:
        - Entry commissions are stored with the lot
        - On close, both entry and exit commissions are deducted from realized P&L

        Args:
            trades: List of trades sorted by timestamp

        Returns:
            Tuple of realized P&L, position quantity, average cost, remaining
            lots, and exact closed-lot contributions.
        """
        # Separate long lots (positive remaining) and short lots (negative remaining)
        long_lots: list[FIFOLot] = []
        short_lots: list[FIFOLot] = []
        contributions: list[FIFORealizedPnLContribution] = []
        realized_pnl = Decimal("0")

        for trade in sorted(trades, key=lambda t: (t.timestamp, t.exec_id)):
            # Track original quantities for commission proration on flip trades
            original_qty = trade.quantity
            total_commission = trade.commission

            if trade.side.lower() == "buy":
                buy_qty = trade.quantity
                buy_price = trade.price

                # First, cover any short positions (buy to close short)
                while buy_qty > 0 and short_lots:
                    short_lot = short_lots[0]
                    short_qty = abs(short_lot.remaining)

                    close_qty = min(buy_qty, short_qty)

                    # Prorate entry commission based on quantity closed
                    entry_comm_portion = (
                        short_lot.entry_commission * (close_qty / abs(short_lot.quantity))
                        if short_lot.quantity != 0
                        else Decimal("0")
                    )

                    # Prorate exit commission for this closing portion
                    exit_comm_portion = total_commission * (close_qty / original_qty)

                    # Short P&L: sold high, bought low = profit minus commissions
                    # (short_price - buy_price) * qty - entry_comm - exit_comm
                    pnl = (
                        (short_lot.price - buy_price) * close_qty
                        - entry_comm_portion
                        - exit_comm_portion
                    )
                    realized_pnl += pnl
                    contributions.append(
                        self._realized_contribution(
                            entry_lot=short_lot,
                            exit_trade=trade,
                            quantity=close_qty,
                            entry_commission=entry_comm_portion,
                            net_realized_pnl=pnl,
                        )
                    )

                    buy_qty -= close_qty
                    short_lot.remaining += close_qty  # Moves toward zero (less negative)

                    if short_lot.remaining >= 0:
                        short_lots.pop(0)

                # Remaining buy quantity opens new long position
                if buy_qty > 0:
                    # Prorate entry commission for the opening portion only
                    opening_comm = total_commission * (buy_qty / original_qty)
                    long_lots.append(
                        FIFOLot(
                            entry_exec_id=trade.exec_id,
                            entry_canonical_signal_id=trade.canonical_signal_id,
                            quantity=buy_qty,
                            price=buy_price,
                            timestamp=trade.timestamp,
                            account_currency=trade.account_currency,
                            entry_commission=opening_comm,
                        )
                    )

            else:  # sell
                sell_qty = trade.quantity
                sell_price = trade.price

                # First, close any long positions (sell to close long)
                while sell_qty > 0 and long_lots:
                    long_lot = long_lots[0]

                    close_qty = min(sell_qty, long_lot.remaining)

                    # Prorate entry commission based on quantity closed
                    entry_comm_portion = (
                        long_lot.entry_commission * (close_qty / long_lot.quantity)
                        if long_lot.quantity != 0
                        else Decimal("0")
                    )

                    # Prorate exit commission for this closing portion
                    exit_comm_portion = total_commission * (close_qty / original_qty)

                    # Long P&L: bought low, sold high = profit minus commissions
                    pnl = (
                        (sell_price - long_lot.price) * close_qty
                        - entry_comm_portion
                        - exit_comm_portion
                    )
                    realized_pnl += pnl
                    contributions.append(
                        self._realized_contribution(
                            entry_lot=long_lot,
                            exit_trade=trade,
                            quantity=close_qty,
                            entry_commission=entry_comm_portion,
                            net_realized_pnl=pnl,
                        )
                    )

                    sell_qty -= close_qty
                    long_lot.remaining -= close_qty

                    if long_lot.remaining <= 0:
                        long_lots.pop(0)

                # Remaining sell quantity opens new short position
                if sell_qty > 0:
                    # Prorate entry commission for the opening portion only
                    opening_comm = total_commission * (sell_qty / original_qty)
                    short_lots.append(
                        FIFOLot(
                            entry_exec_id=trade.exec_id,
                            entry_canonical_signal_id=trade.canonical_signal_id,
                            quantity=-sell_qty,  # Negative for short
                            price=sell_price,
                            timestamp=trade.timestamp,
                            account_currency=trade.account_currency,
                            entry_commission=opening_comm,
                        )
                    )

        # Combine remaining lots
        all_lots = long_lots + short_lots

        # Calculate current position (positive = long, negative = short)
        position_qty = sum((lot.remaining for lot in all_lots), Decimal("0"))

        # Calculate average cost of remaining lots (including prorated entry commissions)
        # This ensures unrealized PnL is net (consistent with realized PnL)
        if position_qty > 0:
            # Long: cost basis = price * qty + entry commission
            total_cost = Decimal("0")
            for lot in long_lots:
                # Prorate entry commission by remaining quantity
                remaining_comm = (
                    lot.entry_commission * (lot.remaining / lot.quantity)
                    if lot.quantity != 0
                    else Decimal("0")
                )
                total_cost += lot.remaining * lot.price + remaining_comm
            avg_cost = total_cost / position_qty
        elif position_qty < 0:
            # Short: cost basis = price * qty - entry commission (reduces proceeds)
            # For shorts, entry commission reduces the effective sell price
            total_cost = Decimal("0")
            for lot in short_lots:
                remaining_comm = (
                    lot.entry_commission * (abs(lot.remaining) / abs(lot.quantity))
                    if lot.quantity != 0
                    else Decimal("0")
                )
                # Subtract commission from effective price for shorts
                total_cost += abs(lot.remaining) * lot.price - remaining_comm
            avg_cost = total_cost / abs(position_qty)
        else:
            avg_cost = Decimal("0")

        return realized_pnl, position_qty, avg_cost, all_lots, contributions

    @staticmethod
    def _realized_contribution(
        *,
        entry_lot: FIFOLot,
        exit_trade: TradeRecord,
        quantity: Decimal,
        entry_commission: Decimal,
        net_realized_pnl: Decimal,
    ) -> FIFORealizedPnLContribution:
        if entry_lot.account_currency != exit_trade.account_currency:
            msg = (
                "FIFO lot and closing execution use different account "
                f"currencies: {entry_lot.account_currency}/{exit_trade.account_currency}"
            )
            raise CurrencyConversionRequiredError(msg)
        deployed_entry_capital = entry_lot.price * quantity + entry_commission
        if not deployed_entry_capital.is_finite() or deployed_entry_capital <= 0:
            msg = "FIFO realized P&L contribution requires positive finite deployed entry capital"
            raise PnLLedgerUnavailableError(msg)
        return FIFORealizedPnLContribution(
            entry_exec_id=entry_lot.entry_exec_id,
            exit_exec_id=exit_trade.exec_id,
            entry_canonical_signal_id=entry_lot.entry_canonical_signal_id,
            exit_canonical_signal_id=exit_trade.canonical_signal_id,
            quantity=quantity,
            net_realized_pnl=net_realized_pnl,
            deployed_entry_capital=deployed_entry_capital,
            account_currency=entry_lot.account_currency,
            exit_time=exit_trade.timestamp,
        )

    def _calculate_total_deployed(self, trades: list[TradeRecord]) -> Decimal:
        """
        Calculate total capital deployed across all trades.

        Tracks capital deployed when OPENING positions:
        - Long positions: buy notional when opening (not when closing shorts)
        - Short positions: sell notional when opening (not when closing longs)

        This requires simulating position changes to distinguish
        opening trades from closing trades.

        Args:
            trades: List of trades

        Returns:
            Total capital deployed for position entries
        """
        total = Decimal("0")

        # Track current long and short quantities to determine if
        # a trade is opening vs closing
        long_qty = Decimal("0")
        short_qty = Decimal("0")  # Stored as positive

        for trade in sorted(trades, key=lambda t: (t.timestamp, t.exec_id)):
            qty = trade.quantity
            price = trade.price

            if trade.side.lower() == "buy":
                # First, close any short positions (buy to cover)
                cover_qty = min(qty, short_qty)
                if cover_qty > 0:
                    short_qty -= cover_qty
                    qty -= cover_qty
                    # Closing short - not new capital deployed

                # Remaining buy opens long position
                if qty > 0:
                    # Opening long - this IS capital deployed
                    total += qty * price
                    long_qty += qty

            else:  # sell
                # First, close any long positions (sell to close)
                close_qty = min(qty, long_qty)
                if close_qty > 0:
                    long_qty -= close_qty
                    qty -= close_qty
                    # Closing long - not new capital deployed

                # Remaining sell opens short position
                if qty > 0:
                    # Opening short - this IS capital deployed (margin requirement)
                    total += qty * price
                    short_qty += qty

        return total

    def _convert_money(
        self,
        value: Decimal,
        *,
        from_currency: str,
        to_currency: str,
        as_of: datetime,
    ) -> Decimal:
        source = normalize_currency(from_currency)
        target = normalize_currency(to_currency)
        if source == target:
            return value
        if self._fx_converter is None:
            msg = (
                f"Cannot combine {source} with {target} accounting values "
                "without an observed FX-rate provider"
            )
            raise CurrencyConversionRequiredError(msg)
        try:
            return Decimal(
                str(
                    self._fx_converter.convert(
                        value,
                        from_currency=source,
                        to_currency=target,
                        as_of=as_of,
                    )
                )
            )
        except FXRateUnavailableError as exc:
            msg = f"Observed FX conversion unavailable for {source}/{target}"
            raise CurrencyConversionRequiredError(msg) from exc

    def _rows_to_trades(
        self,
        rows: list[Any],
        *,
        account_currency: str,
    ) -> list[TradeRecord]:
        """Adapt shared account-currency deltas to the FIFO trade shape."""
        executions = convert_canonical_executions(
            rows,
            account_currency=account_currency,
            convert_money=self._convert_money,
        )
        return [
            TradeRecord(
                trade_id=str(execution.exec_id),
                exec_id=execution.exec_id,
                canonical_signal_id=execution.canonical_signal_id,
                symbol=execution.symbol,
                side=execution.side,
                quantity=execution.quantity,
                price=execution.price,
                commission=execution.commission,
                account_currency=execution.account_currency,
                settlement_currency=execution.settlement_currency,
                commission_currency=execution.commission_currency,
                timestamp=execution.fill_ts,
                order_id=str(execution.order_id),
                quantity_unit=execution.quantity_unit,
                contract_multiplier=execution.contract_multiplier,
            )
            for execution in executions
        ]

    def _resolve_account_currency(self, *, user_id: str, account_id: int) -> str:
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            msg = "P&L accounting requires a positive broker account_id"
            raise PnLLedgerUnavailableError(msg)
        if self._session_factory is None:
            msg = "PnL ledger session is not configured"
            raise PnLLedgerUnavailableError(msg)

        with self._session_factory() as session:
            return self._resolve_scoped_account_currency(
                session,
                user_id=user_id,
                account_id=account_id,
            )

    def _resolve_account_environment(self, *, user_id: str, account_id: int) -> str:
        if self._session_factory is None:
            msg = "PnL ledger session is not configured"
            raise PnLLedgerUnavailableError(msg)
        from lib_application.db.models import LinkedBrokerAccount  # noqa: PLC0415

        with self._session_factory() as session:
            account = (
                session.query(LinkedBrokerAccount)
                .filter(
                    LinkedBrokerAccount.account_id == account_id,
                    LinkedBrokerAccount.user_id == str(user_id),
                )
                .one_or_none()
            )
        if account is None or str(account.environment) not in {"paper", "live"}:
            msg = (
                f"Linked broker account {account_id} has no exact execution environment "
                f"for user {user_id}"
            )
            raise PnLLedgerUnavailableError(msg)
        return str(account.environment)

    def _load_trades(
        self,
        user_id: str,
        symbol: str,
        *,
        account_id: int,
        strategy_id: str | None = None,
        broker: str | None = None,
        mode: str | None = None,
    ) -> list[TradeRecord]:
        """Fetch exact canonical fills for one user/account/symbol.

        The DB work is synchronous, so both the async helpers and the
        synchronous persistence path share this loader. ``broker``/``mode``
        scope to a broker + run-environment so reconciliation against one broker
        does not mix another broker's or environment's fills.
        """
        if self._session_factory is None:
            msg = "PnL ledger session is not configured"
            raise PnLLedgerUnavailableError(msg)

        try:
            account_currency = self._resolve_account_currency(
                user_id=user_id,
                account_id=account_id,
            )
            with self._session_factory() as session:
                rows = load_canonical_executions(
                    session,
                    user_id=user_id,
                    account_id=account_id,
                    symbol=symbol,
                    strategy_id=strategy_id,
                    broker=broker,
                    mode=mode,
                )
                return self._rows_to_trades(rows, account_currency=account_currency)
        except (CurrencyConversionRequiredError, PnLLedgerUnavailableError):
            raise
        except CanonicalExecutionCurrencyError as exc:
            raise CurrencyConversionRequiredError(str(exc)) from exc
        except (
            CanonicalExecutionLedgerError,
            SQLAlchemyError,
            AttributeError,
            TypeError,
            ValueError,
        ) as exc:
            msg = f"Unable to load complete PnL ledger for user={user_id} symbol={symbol}"
            raise PnLLedgerUnavailableError(msg) from exc

    async def _get_trades(
        self,
        user_id: str,
        symbol: str,
        *,
        account_id: int,
        strategy_id: str | None = None,
        broker: str | None = None,
        mode: str | None = None,
    ) -> list[TradeRecord]:
        """Async wrapper over the synchronous :meth:`_load_trades` (DB work is sync)."""
        return self._load_trades(
            user_id,
            symbol,
            account_id=account_id,
            strategy_id=strategy_id,
            broker=broker,
            mode=mode,
        )

    async def _get_traded_symbols(
        self,
        user_id: str,
        *,
        account_id: int,
        strategy_id: str | None = None,
        broker: str | None = None,
        mode: str | None = None,
    ) -> list[str]:
        """Get distinct symbols with filled trades for a user (optionally per strategy).

        ``broker``/``mode`` scope to a broker + run-environment (PL-2).
        """
        if self._session_factory is None:
            msg = "PnL ledger session is not configured"
            raise PnLLedgerUnavailableError(msg)
        self._resolve_account_currency(user_id=user_id, account_id=account_id)

        try:
            with self._session_factory() as session:
                rows = load_canonical_executions(
                    session,
                    user_id=user_id,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    broker=broker,
                    mode=mode,
                )
                return sorted({row.symbol for row in rows if row.quantity > 0})
        except (CanonicalExecutionLedgerError, SQLAlchemyError) as exc:
            msg = f"Unable to enumerate complete PnL ledger for user={user_id}"
            raise PnLLedgerUnavailableError(msg) from exc

    async def _get_current_price(
        self,
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> Decimal | None:
        """Get current market price for a symbol.

        Returns None (not 0) when no price is available, so callers mark to cost
        basis instead of fabricating a total loss from a 0 price.
        """
        if self._market_data:
            try:
                quote = await self._market_data.get_quote(
                    symbol,
                    user_id=user_id,
                    environment=environment,
                )
                price: Decimal | None = None
                if quote and hasattr(quote, "price"):
                    price = Decimal(str(quote.price))
                elif quote and isinstance(quote, dict) and "price" in quote:
                    price = Decimal(str(quote["price"]))
                # A non-positive price is not a usable mark.
                if price is not None and price > 0:
                    return price
            except Exception:
                logger.exception("Error fetching market data for %s", symbol)

        return None

    async def persist_daily_nav(
        self,
        user_id: str,
        equity: Decimal,
        *,
        account_id: int,
        nav_ccy: str,
    ) -> None:
        """Persist today's explicitly scoped account NAV observation.

        ``DailyNav`` records NAV only; it has no columns for the cash /
        positions / realized / unrealized P&L breakdown, so only the total
        equity is stored. The observation currency must match the owned linked
        account's persisted ``base_ccy``. The value remains in that account
        currency so accounts with different capital and settlement boundaries
        cannot be combined implicitly. Neither identity nor currency is
        inferred.

        Args:
            user_id: User identifier
            account_id: Connected linked broker account that produced the NAV
            equity: Total equity (today's NAV), denominated in ``nav_ccy``
            nav_ccy: Explicit currency of ``equity``; it must equal the linked
                account's persisted base currency
        """
        if self._session_factory is None:
            msg = "PnL ledger session is not configured"
            raise PnLLedgerUnavailableError(msg)

        try:
            from lib_application.db.models import DailyNav  # noqa: PLC0415

            with self._session_factory() as session:
                account_ccy = self._resolve_scoped_account_currency(
                    session,
                    user_id=user_id,
                    account_id=account_id,
                    require_connected=True,
                )
                self._validate_nav_currency(
                    nav_ccy,
                    account_currency=account_ccy,
                    account_id=account_id,
                )
                observed_at = datetime.now(tz=UTC)
                normalized_equity = Decimal(str(equity))
                self._validate_nav_equity(normalized_equity)
                today = observed_at.date()
                existing = (
                    session.query(DailyNav)
                    .filter(
                        DailyNav.user_id == user_id,
                        DailyNav.account_id == account_id,
                        DailyNav.date == today,
                    )
                    .first()
                )

                if existing:
                    existing.nav_value = normalized_equity
                    existing.nav_ccy = account_ccy
                else:
                    session.add(
                        DailyNav(
                            user_id=user_id,
                            account_id=account_id,
                            date=today,
                            nav_ccy=account_ccy,
                            nav_value=normalized_equity,
                        )
                    )

                session.commit()
        except PnLLedgerUnavailableError:
            raise
        except (ImportError, SQLAlchemyError, TypeError, ValueError) as exc:
            msg = f"Unable to persist daily NAV for user={user_id} account={account_id}"
            raise PnLLedgerUnavailableError(msg) from exc

    @staticmethod
    def _validate_nav_equity(equity: Decimal) -> None:
        if not equity.is_finite() or equity < 0:
            msg = "Daily NAV equity must be a finite non-negative amount"
            raise PnLLedgerUnavailableError(msg)

    @staticmethod
    def _validate_nav_currency(
        nav_ccy: str,
        *,
        account_currency: str,
        account_id: int,
    ) -> None:
        try:
            supplied_ccy = normalize_currency(nav_ccy)
        except ValueError as exc:
            msg = "Daily NAV requires an explicit valid nav_ccy"
            raise PnLLedgerUnavailableError(msg) from exc
        if supplied_ccy != account_currency:
            msg = (
                f"Daily NAV currency {supplied_ccy} does not match linked "
                f"broker account {account_id} base currency {account_currency}"
            )
            raise PnLLedgerUnavailableError(msg)

    @staticmethod
    def _resolve_scoped_account_currency(
        session: Session,
        *,
        user_id: str,
        account_id: int,
        require_connected: bool = False,
    ) -> str:
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            msg = "P&L accounting requires a positive broker account_id"
            raise PnLLedgerUnavailableError(msg)
        from lib_application.db.models import Broker, LinkedBrokerAccount  # noqa: PLC0415

        query = (
            session.query(LinkedBrokerAccount)
            .join(Broker, LinkedBrokerAccount.broker_id == Broker.broker_id)
            .filter(
                LinkedBrokerAccount.account_id == account_id,
                LinkedBrokerAccount.user_id == str(user_id),
            )
        )
        if require_connected:
            query = query.filter(LinkedBrokerAccount.status == "connected")
        account = query.one_or_none()
        if account is None:
            state = "connected " if require_connected else ""
            msg = (
                f"{state.capitalize()}linked broker account {account_id} "
                f"is unavailable for user {user_id}"
            )
            raise PnLLedgerUnavailableError(msg)
        try:
            return str(normalize_currency(account.base_ccy))
        except ValueError as exc:
            msg = f"Linked broker account {account_id} has no valid base currency"
            raise PnLLedgerUnavailableError(msg) from exc
