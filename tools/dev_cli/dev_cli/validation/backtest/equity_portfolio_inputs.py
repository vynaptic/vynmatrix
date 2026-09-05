"""Point-in-time universe, context, fee, and target-band calculations."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any

from dev_cli.validation.backtest.equity_portfolio_types import (
    DailyBar,
    EquityDataset,
    EquityPortfolioError,
    PortfolioConfig,
)
from lib_indicators import ExpandingPercentileRank
from lib_strategy.equity_market_factors import conservative_split_coordinate_notional

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_REBALANCE_MONTHS = frozenset({1, 4, 7, 10})
_SECTION31_PRE_2020_CONSERVATIVE_BPS = 0.278
_SECTION31_SCHEDULE = (
    # Effective dates and dollars-per-million rates from SEC fee advisories
    # 2020-7, 2021-8, 2022-60, 2023-15, 2024-47, 2025-2, and FY2026.
    (date(2020, 1, 1), 0.207),
    (date(2020, 2, 18), 0.221),
    (date(2021, 2, 25), 0.051),
    (date(2022, 5, 14), 0.229),
    (date(2023, 2, 27), 0.080),
    (date(2024, 5, 22), 0.278),
    (date(2025, 5, 14), 0.000),
    (date(2026, 4, 4), 0.206),
)
_FINRA_TAF_SCHEDULE = (
    # Effective calendar-year rates and per-trade caps from FINRA
    # SR-FINRA-2020-032 and the 2024-019 fee-adjustment schedule.
    (date(2020, 1, 1), 0.000119, 5.95),
    (date(2022, 1, 1), 0.000130, 6.49),
    (date(2023, 1, 1), 0.000145, 7.27),
    (date(2024, 1, 1), 0.000166, 8.30),
    (date(2026, 1, 1), 0.000195, 9.79),
)
_MIN_ADV_OBSERVATIONS = 60
_TARGET_NOTIONAL_ABS_TOLERANCE = 0.01
_TARGET_NOTIONAL_REL_TOLERANCE = 1e-6
_TARGET_WEIGHT_DRIFT_METADATA_KEY = "target_weight_drift_fraction"


# ---------------------------------------------------------------------------
# point-in-time universe construction (§3 + amendment A9)
# ---------------------------------------------------------------------------


def is_member_asof(
    membership: Mapping[str, Sequence[tuple[date, date | None]]], symbol: str, asof: date
) -> bool:
    for effective_from, effective_to in membership.get(symbol, ()):
        if effective_from <= asof and (effective_to is None or asof <= effective_to):
            return True
    return False


def rebalance_sessions(sessions: Sequence[date], start: date, end: date) -> list[date]:
    """First trading session of Jan/Apr/Jul/Oct within [start, end]."""
    out: list[date] = []
    seen: set[tuple[int, int]] = set()
    for session in sessions:
        if session < start or session > end:
            continue
        key = (session.year, session.month)
        if session.month in _REBALANCE_MONTHS and key not in seen:
            seen.add(key)
            out.append(session)
    return out


def median_dollar_adv(
    bars: Sequence[DailyBar], *, before: date, window: int
) -> tuple[float | None, int]:
    """Median conservative provider split-coordinate notional before ``before``."""
    values = [
        conservative_split_coordinate_notional(
            bar.split_adjusted_close,
            bar.split_adjusted_volume,
        )
        for bar in bars
        if bar.session < before and bar.split_adjusted_volume is not None
    ]
    tail = values[-window:]
    if len(tail) < _MIN_ADV_OBSERVATIONS:
        return None, len(tail)
    ordered = sorted(tail)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid], len(tail)
    return (ordered[mid - 1] + ordered[mid]) / 2.0, len(tail)


def select_universe(
    dataset: EquityDataset, *, asof: date, config: PortfolioConfig
) -> tuple[list[str], dict[str, Any]]:
    """Top-N members by trailing median dollar ADV, plus the A9 margin diagnostic."""
    ranked: list[tuple[float, str]] = []
    for symbol, bars in dataset.bars.items():
        if not is_member_asof(dataset.membership, symbol, asof):
            continue
        adv, _observations = median_dollar_adv(bars, before=asof, window=config.adv_window)
        if adv is None:
            continue
        ranked.append((adv, symbol))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]))
    chosen = [symbol for _, symbol in ranked[: config.universe_size]]
    margin: dict[str, Any] = {"asof": asof.isoformat()}
    if len(ranked) > config.universe_size:
        cutoff_adv = ranked[config.universe_size - 1][0]
        next_adv = ranked[config.universe_size][0]
        margin["rank10_adv"] = cutoff_adv
        margin["rank11_adv"] = next_adv
        margin["margin_ratio"] = cutoff_adv / next_adv if next_adv > 0 else float("inf")
    return chosen, margin


# ---------------------------------------------------------------------------
# feeder inputs (§4 metadata contract)
# ---------------------------------------------------------------------------


def vix_percentiles(dataset: EquityDataset) -> dict[date, float | None]:
    """Percentile rank of each session's VIX close vs all prior VIX history."""
    rank = ExpandingPercentileRank(min_samples=250)
    out: dict[date, float | None] = {}
    session_set = set(dataset.sessions)
    for day in sorted(dataset.vix):
        value = rank.update(dataset.vix[day])
        if day in session_set:
            out[day] = value
    return out


def earnings_distances(
    report_dates: Sequence[date], sessions: Sequence[date]
) -> Callable[[int], tuple[int | None, int | None]]:
    """Return (days_until, days_since) in TRADING days for a session index."""
    report_indices = sorted(bisect_left(sessions, report) for report in report_dates)

    def at(session_index: int) -> tuple[int | None, int | None]:
        next_pos = bisect_left(report_indices, session_index)
        prev_pos = bisect_right(report_indices, session_index) - 1
        days_until = (
            report_indices[next_pos] - session_index if next_pos < len(report_indices) else None
        )
        days_since = session_index - report_indices[prev_pos] if prev_pos >= 0 else None
        return days_until, days_since

    return at


def _section31_bps(session: date) -> float:
    if session < _SECTION31_SCHEDULE[0][0]:
        return _SECTION31_PRE_2020_CONSERVATIVE_BPS
    rate = _SECTION31_SCHEDULE[0][1]
    for effective_date, candidate in _SECTION31_SCHEDULE[1:]:
        if session < effective_date:
            break
        rate = candidate
    return rate


def _finra_taf_bps(
    session: date,
    *,
    reference_price: float,
    reference_notional: float,
) -> float:
    """Return the dated per-share TAF and trade cap as notional basis points."""

    if (
        not math.isfinite(reference_price)
        or reference_price <= 0.0
        or not math.isfinite(reference_notional)
        or reference_notional < 0.0
    ):
        msg = "FINRA TAF reference price/notional is invalid"
        raise EquityPortfolioError(msg)
    if reference_notional == 0.0:
        return 0.0
    rate_per_share = _FINRA_TAF_SCHEDULE[0][1]
    maximum_fee = _FINRA_TAF_SCHEDULE[0][2]
    for effective_date, candidate_rate, candidate_cap in _FINRA_TAF_SCHEDULE[1:]:
        if session < effective_date:
            break
        rate_per_share = candidate_rate
        maximum_fee = candidate_cap
    if reference_price < rate_per_share:
        return 0.0
    quantity = reference_notional / reference_price
    fee = min(quantity * rate_per_share, maximum_fee)
    return fee / reference_notional * 10_000.0


def _target_notional_tolerance(signal: Any, *, target_notional: float) -> float:
    """Resolve an optional strategy-owned no-trade band without changing legacy runs."""

    metadata = getattr(signal, "metadata", {})
    raw_fraction = (
        metadata.get(_TARGET_WEIGHT_DRIFT_METADATA_KEY) if isinstance(metadata, Mapping) else None
    )
    if raw_fraction is None:
        fraction = _TARGET_NOTIONAL_REL_TOLERANCE
    else:
        if isinstance(raw_fraction, bool) or not isinstance(raw_fraction, (int, float)):
            msg = f"{_TARGET_WEIGHT_DRIFT_METADATA_KEY} must be numeric"
            raise EquityPortfolioError(msg)
        fraction = float(raw_fraction)
        if not math.isfinite(fraction) or not 0.0 <= fraction < 1.0:
            msg = f"{_TARGET_WEIGHT_DRIFT_METADATA_KEY} must be finite and in [0, 1)"
            raise EquityPortfolioError(msg)
    return max(_TARGET_NOTIONAL_ABS_TOLERANCE, target_notional * fraction)
