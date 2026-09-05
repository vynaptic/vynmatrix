"""
Strategy metrics aggregation service.

Computes strategy-level performance metrics from trade history:
- Win rate, profit factor
- Average win/loss
- Sharpe ratio from trade returns
- Max consecutive wins/losses
"""

from __future__ import annotations

import math
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

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

logger = get_logger(__name__)


class StrategyMetricsUnavailableError(RuntimeError):
    """Strategy economics cannot be computed from a complete owned ledger."""


class StrategyMetricsCurrencyError(StrategyMetricsUnavailableError):
    """Strategy economics cannot be normalized to the account currency."""


@dataclass
class StrategyMetrics:
    """Aggregated metrics for a strategy."""

    strategy_id: str
    user_id: str
    account_id: int
    currency: str
    mode: str  # backtest, paper, live

    # Trade counts
    total_trades: int
    winning_trades: int
    losing_trades: int
    break_even_trades: int

    # Win/Loss metrics
    win_rate: float
    loss_rate: float
    profit_factor: float

    # P&L metrics
    total_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    average_win: Decimal
    average_loss: Decimal
    largest_win: Decimal
    largest_loss: Decimal
    average_trade: Decimal

    # Risk metrics
    sharpe_ratio: float
    sortino_ratio: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    expectancy: Decimal

    # Time metrics
    average_trade_duration_hours: float
    first_trade_at: datetime | None
    last_trade_at: datetime | None
    computed_at: datetime


@dataclass
class TradeResult:
    """Result of a single completed trade (round-trip)."""

    trade_id: str
    strategy_id: str
    account_id: int
    currency: str
    symbol: str
    side: str  # long or short
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    pnl: Decimal
    return_pct: float
    commission: Decimal
    entry_time: datetime
    exit_time: datetime
    duration_hours: float


class StrategyMetricsService:
    """
    Service for computing strategy-level performance metrics.

    Aggregates trade-level data into strategy performance metrics
    including win rate, Sharpe ratio, profit factor, and more.

    Usage:
        service = StrategyMetricsService(session_factory=get_session)

        # Compute metrics for a strategy
        metrics = await service.compute_metrics(
            strategy_id="my_strategy_v1",
            user_id="user_001",
            account_id=101,
            mode="paper",
        )
    """

    _SECONDS_PER_CALENDAR_YEAR = 365.2425 * 24 * 60 * 60
    _MIN_RISK_RATIO_RETURNS = 2
    RISK_FREE_RATE = 0.05  # 5% annual risk-free rate

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Any]] | None = None,
        fx_rate_provider: FXRateProvider | None = None,
    ) -> None:
        """
        Initialize strategy metrics service.

        Args:
            session_factory: Factory for database sessions
            fx_rate_provider: Point-in-time observed FX-rate provider
        """
        self._session_factory = session_factory
        self._fx_converter = FXConverter(fx_rate_provider) if fx_rate_provider is not None else None

    async def compute_metrics(
        self,
        strategy_id: str,
        user_id: str,
        *,
        account_id: int,
        mode: str = "paper",
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> StrategyMetrics:
        """
        Compute aggregated metrics for a strategy.

        Args:
            strategy_id: Strategy identifier
            user_id: User identifier
            account_id: Explicit linked broker account owned by the user
            mode: Execution mode (backtest, paper, live)
            start_date: Optional start date filter
            end_date: Optional end date filter

        Returns:
            StrategyMetrics with aggregated performance data
        """
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"backtest", "paper", "live"}:
            msg = f"Unsupported strategy metrics mode: {mode!r}"
            raise StrategyMetricsUnavailableError(msg)
        account_currency = self._resolve_account_currency(
            user_id=user_id,
            account_id=account_id,
            mode=normalized_mode,
        )
        trades = await self._get_completed_trades(
            strategy_id=strategy_id,
            user_id=user_id,
            account_id=account_id,
            account_currency=account_currency,
            mode=normalized_mode,
            start_date=start_date,
            end_date=end_date,
        )

        if not trades:
            return self._empty_metrics(
                strategy_id,
                user_id,
                account_id,
                account_currency,
                normalized_mode,
            )

        return self._compute_metrics_from_trades(
            trades,
            strategy_id,
            user_id,
            account_id,
            account_currency,
            normalized_mode,
        )

    def _compute_metrics_from_trades(
        self,
        trades: list[TradeResult],
        strategy_id: str,
        user_id: str,
        account_id: int,
        currency: str,
        mode: str,
    ) -> StrategyMetrics:
        """Compute all metrics from a list of trades."""
        total_trades = len(trades)

        # Separate wins, losses, break-evens
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        break_evens = [t for t in trades if t.pnl == 0]

        winning_trades = len(wins)
        losing_trades = len(losses)
        break_even_trades = len(break_evens)

        # Win/Loss rates
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
        loss_rate = losing_trades / total_trades if total_trades > 0 else 0.0

        # P&L metrics
        gross_profit = sum((t.pnl for t in wins), Decimal("0"))
        gross_loss = abs(sum((t.pnl for t in losses), Decimal("0")))
        total_pnl = gross_profit - gross_loss

        # Profit factor
        profit_factor = (
            float(gross_profit / gross_loss)
            if gross_loss > 0
            else float("inf")
            if gross_profit > 0
            else 0.0
        )

        # Average metrics
        average_win = gross_profit / winning_trades if winning_trades > 0 else Decimal("0")
        average_loss = gross_loss / losing_trades if losing_trades > 0 else Decimal("0")
        average_trade = total_pnl / total_trades if total_trades > 0 else Decimal("0")

        # Largest win/loss
        largest_win = max((t.pnl for t in wins), default=Decimal("0"))
        largest_loss = abs(min((t.pnl for t in losses), default=Decimal("0")))

        # Consecutive wins/losses
        max_consecutive_wins = self._max_consecutive(trades, winning=True)
        max_consecutive_losses = self._max_consecutive(trades, winning=False)

        # Risk-adjusted returns
        returns = [t.return_pct for t in trades]
        trades_per_year = self._observed_trades_per_year(trades)
        sharpe_ratio = self._compute_sharpe_ratio(
            returns,
            trades_per_year=trades_per_year,
        )
        sortino_ratio = self._compute_sortino_ratio(
            returns,
            trades_per_year=trades_per_year,
        )

        # Expectancy (average expected return per trade)
        expectancy = (
            (Decimal(str(win_rate)) * average_win) - (Decimal(str(loss_rate)) * average_loss)
            if total_trades > 0
            else Decimal("0")
        )

        # Time metrics
        durations = [t.duration_hours for t in trades]
        average_duration = sum(durations) / len(durations) if durations else 0.0

        timestamps = [t.entry_time for t in trades] + [t.exit_time for t in trades]
        first_trade = min(timestamps) if timestamps else None
        last_trade = max(timestamps) if timestamps else None

        return StrategyMetrics(
            strategy_id=strategy_id,
            user_id=user_id,
            account_id=account_id,
            currency=currency,
            mode=mode,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            break_even_trades=break_even_trades,
            win_rate=win_rate,
            loss_rate=loss_rate,
            profit_factor=profit_factor,
            total_pnl=total_pnl,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            average_win=average_win,
            average_loss=average_loss,
            largest_win=largest_win,
            largest_loss=largest_loss,
            average_trade=average_trade,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            max_consecutive_wins=max_consecutive_wins,
            max_consecutive_losses=max_consecutive_losses,
            expectancy=expectancy,
            average_trade_duration_hours=average_duration,
            first_trade_at=first_trade,
            last_trade_at=last_trade,
            computed_at=datetime.now(tz=UTC),
        )

    def _empty_metrics(
        self,
        strategy_id: str,
        user_id: str,
        account_id: int,
        currency: str,
        mode: str,
    ) -> StrategyMetrics:
        """Return empty metrics when no trades exist."""
        return StrategyMetrics(
            strategy_id=strategy_id,
            user_id=user_id,
            account_id=account_id,
            currency=currency,
            mode=mode,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            break_even_trades=0,
            win_rate=0.0,
            loss_rate=0.0,
            profit_factor=0.0,
            total_pnl=Decimal("0"),
            gross_profit=Decimal("0"),
            gross_loss=Decimal("0"),
            average_win=Decimal("0"),
            average_loss=Decimal("0"),
            largest_win=Decimal("0"),
            largest_loss=Decimal("0"),
            average_trade=Decimal("0"),
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            max_consecutive_wins=0,
            max_consecutive_losses=0,
            expectancy=Decimal("0"),
            average_trade_duration_hours=0.0,
            first_trade_at=None,
            last_trade_at=None,
            computed_at=datetime.now(tz=UTC),
        )

    def _max_consecutive(self, trades: list[TradeResult], winning: bool) -> int:
        """Calculate maximum consecutive wins or losses."""
        max_streak = 0
        current_streak = 0

        for trade in sorted(trades, key=lambda t: t.exit_time):
            is_win = trade.pnl > 0
            if (winning and is_win) or (not winning and trade.pnl < 0):
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0

        return max_streak

    def _observed_trades_per_year(self, trades: list[TradeResult]) -> float:
        """Annualize from observed completion cadence, never a strategy constant."""
        if len(trades) < self._MIN_RISK_RATIO_RETURNS:
            return 0.0
        completed_at = sorted(trade.exit_time for trade in trades)
        observed_seconds = (completed_at[-1] - completed_at[0]).total_seconds()
        if observed_seconds <= 0:
            return 0.0
        return (len(completed_at) - 1) * self._SECONDS_PER_CALENDAR_YEAR / observed_seconds

    def _compute_sharpe_ratio(
        self,
        returns: list[float],
        *,
        trades_per_year: float,
    ) -> float:
        """
        Compute annualized Sharpe ratio from trade-level returns.

        For trade-level returns (not daily), we:
        1. Convert percent returns to decimal (divide by 100)
        2. Annualize using the observed completed-trade cadence

        Sharpe = (mean_return - per_trade_rf) / std_dev * sqrt(trades_per_year)

        Args:
            returns: List of trade return percentages (e.g., 5.0 for 5%)
            trades_per_year: Observed completed-trade cadence

        Returns:
            Annualized Sharpe ratio
        """
        if (
            len(returns) < self._MIN_RISK_RATIO_RETURNS
            or not math.isfinite(trades_per_year)
            or trades_per_year <= 0
        ):
            return 0.0

        # Convert percent to decimal
        decimal_returns = [r / 100.0 for r in returns]

        mean_return = sum(decimal_returns) / len(decimal_returns)
        variance = sum((r - mean_return) ** 2 for r in decimal_returns) / (len(decimal_returns) - 1)
        std_dev = math.sqrt(variance) if variance > 0 else 0.0

        if std_dev == 0:
            return 0.0

        # Per-trade risk-free rate (annual rate / trades per year)
        per_trade_rf = self.RISK_FREE_RATE / trades_per_year
        excess_return = mean_return - per_trade_rf

        # Annualize: multiply by sqrt(trades per year)
        return (excess_return / std_dev) * math.sqrt(trades_per_year)

    def _compute_sortino_ratio(
        self,
        returns: list[float],
        *,
        trades_per_year: float,
    ) -> float:
        """
        Compute annualized Sortino ratio from trade-level returns.

        For trade-level returns (not daily), we:
        1. Convert percent returns to decimal (divide by 100)
        2. Annualize using the observed completed-trade cadence

        Sortino = (mean_return - per_trade_rf) / downside_std * sqrt(trades_per_year)

        Args:
            returns: List of trade return percentages (e.g., 5.0 for 5%)
            trades_per_year: Observed completed-trade cadence

        Returns:
            Annualized Sortino ratio
        """
        if (
            len(returns) < self._MIN_RISK_RATIO_RETURNS
            or not math.isfinite(trades_per_year)
            or trades_per_year <= 0
        ):
            return 0.0

        # Convert percent to decimal
        decimal_returns = [r / 100.0 for r in returns]

        mean_return = sum(decimal_returns) / len(decimal_returns)

        # Downside deviation (only negative returns, relative to zero target)
        negative_returns = [r for r in decimal_returns if r < 0]
        if not negative_returns:
            return float("inf") if mean_return > 0 else 0.0

        # Use full sample for downside variance calculation
        downside_variance = sum(r**2 for r in negative_returns) / len(decimal_returns)
        downside_std = math.sqrt(downside_variance) if downside_variance > 0 else 0.0

        if downside_std == 0:
            return 0.0

        # Per-trade risk-free rate
        per_trade_rf = self.RISK_FREE_RATE / trades_per_year
        excess_return = mean_return - per_trade_rf

        # Annualize: multiply by sqrt(trades per year)
        return (excess_return / downside_std) * math.sqrt(trades_per_year)

    def _resolve_account_currency(
        self,
        *,
        user_id: str,
        account_id: int,
        mode: str,
    ) -> str:
        """Resolve the owned account's reporting currency and environment."""
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            msg = "Strategy metrics require a positive broker account_id"
            raise StrategyMetricsUnavailableError(msg)
        if self._session_factory is None:
            msg = "Strategy metrics ledger session is not configured"
            raise StrategyMetricsUnavailableError(msg)

        try:
            from lib_application.db.models import LinkedBrokerAccount  # noqa: PLC0415

            with self._session_factory() as session:
                account = session.get(LinkedBrokerAccount, account_id)
                account_context = (
                    None
                    if account is None
                    else (
                        str(account.user_id),
                        str(account.environment or "").strip().lower(),
                        str(account.base_ccy or ""),
                    )
                )
        except (ImportError, SQLAlchemyError, OSError) as exc:
            msg = f"Unable to resolve linked broker account {account_id}"
            raise StrategyMetricsUnavailableError(msg) from exc

        if account_context is None or account_context[0] != str(user_id):
            msg = f"Linked broker account {account_id} is unavailable for user {user_id}"
            raise StrategyMetricsUnavailableError(msg)
        _, account_environment, account_currency = account_context
        if mode in {"paper", "live"} and account_environment != mode:
            msg = (
                f"Linked broker account {account_id} is configured for "
                f"{account_environment or 'an unknown environment'}, not {mode}"
            )
            raise StrategyMetricsUnavailableError(msg)
        try:
            return str(normalize_currency(account_currency))
        except ValueError as exc:
            msg = f"Linked broker account {account_id} has no valid base currency"
            raise StrategyMetricsUnavailableError(msg) from exc

    def _convert_money(
        self,
        value: Decimal,
        *,
        from_currency: str,
        to_currency: str,
        as_of: datetime,
    ) -> Decimal:
        """Convert one monetary value using an observed point-in-time rate."""
        try:
            source = normalize_currency(from_currency)
            target = normalize_currency(to_currency)
        except ValueError as exc:
            msg = "Strategy fill has invalid currency context"
            raise StrategyMetricsCurrencyError(msg) from exc
        if not value.is_finite():
            msg = "Strategy fill has a non-finite monetary value"
            raise StrategyMetricsCurrencyError(msg)
        if source == target:
            return value
        if self._fx_converter is None:
            msg = (
                f"Cannot combine {source} with {target} strategy economics "
                "without an observed FX-rate provider"
            )
            raise StrategyMetricsCurrencyError(msg)
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
        except (FXRateUnavailableError, ValueError) as exc:
            msg = f"Observed FX conversion unavailable for {source}/{target}"
            raise StrategyMetricsCurrencyError(msg) from exc

    def _rows_to_executions(
        self,
        rows: list[Any],
        *,
        account_id: int,
        account_currency: str,
    ) -> list[dict[str, Any]]:
        """Adapt shared account-currency deltas to the metrics matcher shape."""
        executions = convert_canonical_executions(
            rows,
            account_currency=account_currency,
            convert_money=self._convert_money,
        )
        for execution in executions:
            if execution.account_id != account_id:
                msg = f"Canonical order {execution.order_id} is outside broker account {account_id}"
                raise StrategyMetricsUnavailableError(msg)
        return [
            {
                "exec_id": str(execution.exec_id),
                "order_id": str(execution.order_id),
                "account_id": execution.account_id,
                "currency": execution.account_currency,
                "fill_ts": execution.fill_ts,
                "symbol": execution.symbol,
                "side": execution.side,
                "qty": execution.quantity,
                "price": execution.price,
                "fee_amount": execution.commission,
            }
            for execution in executions
        ]

    async def _get_completed_trades(
        self,
        strategy_id: str,
        user_id: str,
        account_id: int,
        account_currency: str,
        mode: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[TradeResult]:
        """
        Fetch exact canonical fills and match them into round trips.

        Ownership, strategy, side, and environment filters are relational
        ``order_intents`` columns. Cumulative pending-order snapshots are not
        accounting inputs.
        """
        if self._session_factory is None:
            msg = "Strategy metrics ledger session is not configured"
            raise StrategyMetricsUnavailableError(msg)

        try:
            with self._session_factory() as session:
                # An opening fill may predate the reporting window while its
                # closing fill falls inside it.  Load all prior inventory
                # through the requested end boundary, match FIFO first, and
                # then select completed trades by exit time.  Filtering raw
                # fills at ``start_date`` would turn valid closes into phantom
                # short opens and understate realized performance.
                rows = load_canonical_executions(
                    session,
                    user_id=user_id,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    mode=mode,
                    end_date=end_date,
                )
                executions = self._rows_to_executions(
                    rows,
                    account_id=account_id,
                    account_currency=account_currency,
                )
                trades = self._match_executions_to_trades_normalized(
                    executions,
                    strategy_id,
                    account_id=account_id,
                    currency=account_currency,
                )
                if start_date is None:
                    return trades
                normalized_start = (
                    start_date.replace(tzinfo=UTC)
                    if start_date.tzinfo is None
                    else start_date.astimezone(UTC)
                )
                return [trade for trade in trades if trade.exit_time >= normalized_start]

        except StrategyMetricsUnavailableError:
            raise
        except CanonicalExecutionCurrencyError as exc:
            raise StrategyMetricsCurrencyError(str(exc)) from exc
        except (CanonicalExecutionLedgerError, SQLAlchemyError, OSError) as exc:
            logger.exception("Error fetching completed trades from canonical executions")
            msg = (
                f"Unable to load complete strategy ledger for user={user_id} "
                f"account={account_id} strategy={strategy_id}"
            )
            raise StrategyMetricsUnavailableError(msg) from exc

    def _group_owned_executions(
        self,
        executions: list[dict[str, Any]],
        *,
        account_id: int,
        currency: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Group fills only after reasserting their account-currency boundary."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for execution in executions:
            if execution["account_id"] != account_id or execution["currency"] != currency:
                msg = "Strategy metrics fill escaped its broker-account boundary"
                raise StrategyMetricsUnavailableError(msg)
            grouped.setdefault(execution["symbol"], []).append(execution)
        return grouped

    def _match_executions_to_trades_normalized(
        self,
        executions: list[dict[str, Any]],
        strategy_id: str,
        *,
        account_id: int,
        currency: str,
    ) -> list[TradeResult]:
        """
        Match buy/sell executions into round-trip trades (normalized schema).

        Uses dict access: ex["symbol"], ex["side"], ex["qty"], ex["price"], ex["fee_amount"]
        Supports both long trades (buy→sell) and short trades (sell→buy).
        Uses FIFO matching within each position type.
        """
        by_symbol = self._group_owned_executions(
            executions,
            account_id=account_id,
            currency=currency,
        )

        trades: list[TradeResult] = []
        trade_counter = 0

        for symbol, symbol_executions in by_symbol.items():
            long_positions: list[dict[str, Any]] = []
            short_positions: list[dict[str, Any]] = []

            for ex in sorted(symbol_executions, key=lambda e: e["fill_ts"]):
                side = ex["side"]
                qty = Decimal(str(ex["qty"]))
                price = Decimal(str(ex["price"]))
                total_commission = Decimal(str(ex.get("fee_amount", 0) or 0))
                original_qty = qty
                remaining = qty

                if side == "buy":
                    while remaining > 0 and short_positions:
                        pos = short_positions[0]
                        close_qty = min(remaining, pos["quantity"])

                        entry_comm_portion = (
                            pos["entry_commission"] * (close_qty / pos["original_qty"])
                            if pos["original_qty"] > 0
                            else Decimal("0")
                        )
                        exit_comm_portion = total_commission * (close_qty / original_qty)

                        entry_price = pos["price"]
                        exit_price = price
                        pnl = (
                            (entry_price - exit_price) * close_qty
                            - entry_comm_portion
                            - exit_comm_portion
                        )
                        gross_pnl = (entry_price - exit_price) * close_qty
                        cost_basis = entry_price * close_qty
                        return_pct = (
                            float(
                                (gross_pnl - entry_comm_portion - exit_comm_portion)
                                / cost_basis
                                * 100
                            )
                            if cost_basis
                            else 0.0
                        )

                        duration = (ex["fill_ts"] - pos["timestamp"]).total_seconds() / 3600

                        trade_counter += 1
                        trades.append(
                            TradeResult(
                                trade_id=f"{strategy_id}_trade_{trade_counter}",
                                strategy_id=strategy_id,
                                account_id=account_id,
                                currency=currency,
                                symbol=symbol,
                                side="short",
                                entry_price=entry_price,
                                exit_price=exit_price,
                                quantity=close_qty,
                                pnl=pnl,
                                return_pct=return_pct,
                                commission=entry_comm_portion + exit_comm_portion,
                                entry_time=pos["timestamp"],
                                exit_time=ex["fill_ts"],
                                duration_hours=duration,
                            )
                        )

                        remaining -= close_qty
                        pos["quantity"] -= close_qty
                        if pos["quantity"] <= 0:
                            short_positions.pop(0)

                    if remaining > 0:
                        opening_comm = total_commission * (remaining / original_qty)
                        long_positions.append(
                            {
                                "quantity": remaining,
                                "original_qty": remaining,
                                "price": price,
                                "timestamp": ex["fill_ts"],
                                "entry_commission": opening_comm,
                            }
                        )

                else:  # sell
                    while remaining > 0 and long_positions:
                        pos = long_positions[0]
                        close_qty = min(remaining, pos["quantity"])

                        entry_comm_portion = (
                            pos["entry_commission"] * (close_qty / pos["original_qty"])
                            if pos["original_qty"] > 0
                            else Decimal("0")
                        )
                        exit_comm_portion = total_commission * (close_qty / original_qty)

                        entry_price = pos["price"]
                        exit_price = price
                        pnl = (
                            (exit_price - entry_price) * close_qty
                            - entry_comm_portion
                            - exit_comm_portion
                        )
                        gross_pnl = (exit_price - entry_price) * close_qty
                        cost_basis = entry_price * close_qty + entry_comm_portion
                        return_pct = float(pnl / cost_basis * 100) if cost_basis else 0.0

                        duration = (ex["fill_ts"] - pos["timestamp"]).total_seconds() / 3600

                        trade_counter += 1
                        trades.append(
                            TradeResult(
                                trade_id=f"{strategy_id}_trade_{trade_counter}",
                                strategy_id=strategy_id,
                                account_id=account_id,
                                currency=currency,
                                symbol=symbol,
                                side="long",
                                entry_price=entry_price,
                                exit_price=exit_price,
                                quantity=close_qty,
                                pnl=pnl,
                                return_pct=return_pct,
                                commission=entry_comm_portion + exit_comm_portion,
                                entry_time=pos["timestamp"],
                                exit_time=ex["fill_ts"],
                                duration_hours=duration,
                            )
                        )

                        remaining -= close_qty
                        pos["quantity"] -= close_qty
                        if pos["quantity"] <= 0:
                            long_positions.pop(0)

                    if remaining > 0:
                        opening_comm = total_commission * (remaining / original_qty)
                        short_positions.append(
                            {
                                "quantity": remaining,
                                "original_qty": remaining,
                                "price": price,
                                "timestamp": ex["fill_ts"],
                                "entry_commission": opening_comm,
                            }
                        )

        return trades
