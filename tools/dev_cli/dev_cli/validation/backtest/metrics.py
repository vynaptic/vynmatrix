"""Deterministic, dependency-free performance metrics for historical validation.

The metrics make annualisation, benchmark assumptions, contribution grouping,
and unavailable inputs explicit. No statistic in this module is itself a
paper- or live-promotion decision.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median

# Preserve the historical crypto-daily default.  Callers validating a
# session-based habitat must pass its registered annualisation factor.
_DEFAULT_ANNUALIZATION_FACTOR = 365.0
_MIN_RETURNS_FOR_RATIO = 2
_VAR_CONFIDENCE = 0.95


@dataclass(frozen=True)
class BacktestMetrics:
    """Summary performance metrics for one backtest run.

    P&L fields are in account currency.  Percentage fields are on a 0-100
    scale.  ``break_even_cost_bps`` is the gross aggregate P&L divided by total
    traded notional; it is an all-in cost budget, not a venue fee estimate.
    """

    initial_capital: float
    final_equity: float
    peak_equity: float
    total_return_pct: float
    cagr_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    annualization_factor: float
    annualized_volatility_pct: float
    calmar_ratio: float
    recovery_duration_days: float
    expectancy: float
    payoff_ratio: float
    exposure_pct: float
    turnover_ratio: float
    average_holding_period_days: float
    median_holding_period_days: float
    value_at_risk_95_pct: float
    expected_shortfall_95_pct: float
    break_even_cost_bps: float


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: Sequence[float], *, mean: float | None = None) -> float:
    if len(xs) < _MIN_RETURNS_FOR_RATIO:
        return 0.0
    m = _mean(xs) if mean is None else mean
    variance = sum((x - m) ** 2 for x in xs) / len(xs)
    return float(variance**0.5)


def _validate_annualization_factor(annualization_factor: float) -> float:
    factor = float(annualization_factor)
    if not math.isfinite(factor) or factor <= 0.0:
        msg = "annualization_factor must be finite and positive"
        raise ValueError(msg)
    return factor


def daily_returns(equity_curve: Sequence[tuple[datetime, float]]) -> list[float]:
    """Return consecutive end-of-calendar-day simple returns.

    Intraday observations collapse to the final equity of each UTC/calendar day
    represented by their timestamps.  The caller owns timezone normalization.
    """

    if not equity_curve:
        return []
    last_by_day: dict[date, float] = {}
    for timestamp, equity in equity_curve:
        last_by_day[timestamp.date()] = equity
    end_of_day = [last_by_day[day] for day in sorted(last_by_day)]
    returns: list[float] = []
    for previous, current in itertools.pairwise(end_of_day):
        if previous != 0:
            returns.append((current - previous) / previous)
    return returns


def sharpe_ratio(
    returns: Sequence[float],
    *,
    annualization_factor: float = _DEFAULT_ANNUALIZATION_FACTOR,
    risk_free_return: float = 0.0,
) -> float:
    """Annualised Sharpe using an explicitly per-observation risk-free return."""

    factor = _validate_annualization_factor(annualization_factor)
    if len(returns) < _MIN_RETURNS_FOR_RATIO:
        return 0.0
    excess = [value - risk_free_return for value in returns]
    average = _mean(excess)
    deviation = _std(excess, mean=average)
    if deviation == 0.0:
        return 0.0
    return float((average / deviation) * factor**0.5)


def sortino_ratio(
    returns: Sequence[float],
    *,
    annualization_factor: float = _DEFAULT_ANNUALIZATION_FACTOR,
    minimum_acceptable_return: float = 0.0,
) -> float:
    """Annualised Sortino using downside deviation over all observations."""

    factor = _validate_annualization_factor(annualization_factor)
    if len(returns) < _MIN_RETURNS_FOR_RATIO:
        return 0.0
    excess = [value - minimum_acceptable_return for value in returns]
    downside = [min(value, 0.0) for value in excess]
    downside_deviation = (sum(value**2 for value in downside) / len(excess)) ** 0.5
    if downside_deviation == 0.0:
        return 0.0
    return float((_mean(excess) / downside_deviation) * factor**0.5)


def annualized_volatility_pct(
    returns: Sequence[float],
    *,
    annualization_factor: float = _DEFAULT_ANNUALIZATION_FACTOR,
) -> float:
    """Population volatility on a 0-100 annualised percentage scale."""

    factor = _validate_annualization_factor(annualization_factor)
    return float(_std(returns) * factor**0.5 * 100.0)


def max_drawdown_pct(equity_values: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a positive percentage."""

    peak = float("-inf")
    worst = 0.0
    for equity in equity_values:
        peak = max(peak, equity)
        if peak > 0:
            drawdown = (peak - equity) / peak
            worst = max(worst, drawdown)
    return worst * 100.0


def recovery_duration_days(equity_curve: Sequence[tuple[datetime, float]]) -> float:
    """Longest peak-to-recovery (or still-underwater) duration in days."""

    if not equity_curve:
        return 0.0
    peak = equity_curve[0][1]
    peak_timestamp = equity_curve[0][0]
    underwater_since: datetime | None = None
    longest_seconds = 0.0
    for timestamp, equity in equity_curve[1:]:
        if equity >= peak:
            if underwater_since is not None:
                longest_seconds = max(
                    longest_seconds,
                    (timestamp - underwater_since).total_seconds(),
                )
                underwater_since = None
            peak = equity
            peak_timestamp = timestamp
        elif underwater_since is None:
            underwater_since = peak_timestamp
    if underwater_since is not None:
        longest_seconds = max(
            longest_seconds,
            (equity_curve[-1][0] - underwater_since).total_seconds(),
        )
    return longest_seconds / 86_400.0


def cagr_pct(
    initial: float,
    final: float,
    *,
    days: float,
    calendar_days_per_year: float = _DEFAULT_ANNUALIZATION_FACTOR,
) -> float:
    """Compound annual growth rate as a percentage."""

    year_days = _validate_annualization_factor(calendar_days_per_year)
    if initial <= 0 or final <= 0 or days <= 0:
        return 0.0
    years = days / year_days
    if years == 0:
        return 0.0
    return float(((final / initial) ** (1.0 / years) - 1.0) * 100.0)


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def tail_risk_pct(
    returns: Sequence[float],
    *,
    confidence: float = _VAR_CONFIDENCE,
) -> tuple[float, float]:
    """Return historical VaR and Expected Shortfall as positive loss percentages."""

    if not 0.0 < confidence < 1.0:
        msg = "confidence must be in (0, 1)"
        raise ValueError(msg)
    if not returns:
        return 0.0, 0.0
    cutoff = _quantile(returns, 1.0 - confidence)
    tail = [value for value in returns if value <= cutoff]
    value_at_risk = max(0.0, -cutoff) * 100.0
    expected_shortfall = max(0.0, -_mean(tail)) * 100.0
    return value_at_risk, expected_shortfall


def compute_metrics(
    equity_curve: list[tuple[datetime, float]],
    trade_pnls: list[float],
    *,
    initial_capital: float,
    annualization_factor: float = _DEFAULT_ANNUALIZATION_FACTOR,
    calendar_days_per_year: float = _DEFAULT_ANNUALIZATION_FACTOR,
    gross_trade_pnls: Sequence[float] | None = None,
    turnover_notional: float = 0.0,
    exposure_seconds: float = 0.0,
    holding_periods_seconds: Sequence[float] | None = None,
) -> BacktestMetrics:
    """Aggregate an equity curve and trade ledger into summary metrics.

    Callers may omit unavailable execution evidence. Complete validation runs
    should pass gross P&L, traded notional, exposure, and holding
    periods so zeros mean observed zero rather than unavailable data.
    """

    factor = _validate_annualization_factor(annualization_factor)
    calendar_year_days = _validate_annualization_factor(calendar_days_per_year)
    if not math.isfinite(initial_capital) or initial_capital < 0.0:
        msg = "initial_capital must be finite and non-negative"
        raise ValueError(msg)
    if any(not math.isfinite(value) for value in trade_pnls):
        msg = "trade_pnls must be finite"
        raise ValueError(msg)
    if not math.isfinite(turnover_notional) or turnover_notional < 0.0:
        msg = "turnover_notional must be finite and non-negative"
        raise ValueError(msg)
    if not math.isfinite(exposure_seconds) or exposure_seconds < 0.0:
        msg = "exposure_seconds must be finite and non-negative"
        raise ValueError(msg)
    if gross_trade_pnls is not None and len(gross_trade_pnls) != len(trade_pnls):
        msg = "gross_trade_pnls must align with trade_pnls"
        raise ValueError(msg)
    if gross_trade_pnls is not None and any(not math.isfinite(value) for value in gross_trade_pnls):
        msg = "gross_trade_pnls must be finite"
        raise ValueError(msg)
    if holding_periods_seconds is not None and any(
        not math.isfinite(value) or value < 0.0 for value in holding_periods_seconds
    ):
        msg = "holding_periods_seconds must be finite and non-negative"
        raise ValueError(msg)
    if any(not math.isfinite(equity) for _, equity in equity_curve):
        msg = "equity_curve values must be finite"
        raise ValueError(msg)
    if any(
        current_timestamp <= previous_timestamp
        for (previous_timestamp, _), (current_timestamp, _) in itertools.pairwise(equity_curve)
    ):
        msg = "equity_curve timestamps must be strictly increasing"
        raise ValueError(msg)
    equity_values = [equity for _, equity in equity_curve]
    final_equity = equity_values[-1] if equity_values else initial_capital
    peak_equity = max(equity_values) if equity_values else initial_capital
    total_return_pct = (
        (final_equity - initial_capital) / initial_capital * 100.0 if initial_capital else 0.0
    )
    elapsed_seconds = 0.0
    if len(equity_curve) >= _MIN_RETURNS_FOR_RATIO:
        elapsed_seconds = (equity_curve[-1][0] - equity_curve[0][0]).total_seconds()
    elapsed_days = elapsed_seconds / 86_400.0

    returns = daily_returns(equity_curve)
    wins = [pnl for pnl in trade_pnls if pnl > 0]
    losses = [pnl for pnl in trade_pnls if pnl < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    maximum_drawdown = max_drawdown_pct(equity_values)
    cagr = cagr_pct(
        initial_capital,
        final_equity,
        days=elapsed_days,
        calendar_days_per_year=calendar_year_days,
    )
    average_win = _mean(wins)
    average_loss = -_mean(losses)
    holding_periods = list(holding_periods_seconds or [])
    gross_pnls = list(gross_trade_pnls) if gross_trade_pnls is not None else list(trade_pnls)
    value_at_risk, expected_shortfall = tail_risk_pct(returns)
    average_equity = _mean(equity_values) if equity_values else initial_capital

    return BacktestMetrics(
        initial_capital=initial_capital,
        final_equity=final_equity,
        peak_equity=peak_equity,
        total_return_pct=total_return_pct,
        cagr_pct=cagr,
        sharpe_ratio=sharpe_ratio(returns, annualization_factor=factor),
        sortino_ratio=sortino_ratio(returns, annualization_factor=factor),
        max_drawdown_pct=maximum_drawdown,
        profit_factor=(gross_profit / gross_loss if gross_loss > 0 else 0.0),
        win_rate_pct=(len(wins) / len(trade_pnls) * 100.0 if trade_pnls else 0.0),
        total_trades=len(trade_pnls),
        winning_trades=len(wins),
        losing_trades=len(losses),
        annualization_factor=factor,
        annualized_volatility_pct=annualized_volatility_pct(
            returns,
            annualization_factor=factor,
        ),
        calmar_ratio=(cagr / maximum_drawdown if maximum_drawdown > 0.0 else 0.0),
        recovery_duration_days=recovery_duration_days(equity_curve),
        expectancy=_mean(trade_pnls),
        payoff_ratio=(average_win / average_loss if average_loss > 0.0 else 0.0),
        exposure_pct=(
            min(max(exposure_seconds / elapsed_seconds * 100.0, 0.0), 100.0)
            if elapsed_seconds > 0.0
            else 0.0
        ),
        turnover_ratio=(turnover_notional / average_equity if average_equity > 0.0 else 0.0),
        average_holding_period_days=(_mean(holding_periods) / 86_400.0 if holding_periods else 0.0),
        median_holding_period_days=(median(holding_periods) / 86_400.0 if holding_periods else 0.0),
        value_at_risk_95_pct=value_at_risk,
        expected_shortfall_95_pct=expected_shortfall,
        break_even_cost_bps=(
            sum(gross_pnls) / turnover_notional * 10_000.0 if turnover_notional > 0.0 else 0.0
        ),
    )
