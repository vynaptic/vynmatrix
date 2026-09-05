"""Validation-only reconciliation of production consolidation and provider daily bars."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lib_data.bars import Bar, ohlcv_invariant_error
from lib_data.consolidation import BarConsolidator


@dataclass(frozen=True)
class DailyFieldDelta:
    """One deterministic consolidated-versus-provider OHLCV comparison."""

    field: str
    consolidated: float
    provider: float
    absolute_delta: float
    tolerance: float
    within_tolerance: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "consolidated": self.consolidated,
            "provider": self.provider,
            "absolute_delta": self.absolute_delta,
            "tolerance": self.tolerance,
            "within_tolerance": self.within_tolerance,
        }


@dataclass(frozen=True)
class DailyBarReconciliation:
    """Comparison for one UTC day, keyed by the production close timestamp."""

    close_timestamp: datetime
    provider_start_timestamp: datetime | None
    status: str
    constituent_bars: int | None
    coverage: float | None
    field_deltas: tuple[DailyFieldDelta, ...]

    @property
    def matches(self) -> bool:
        return self.status == "matched"

    def to_dict(self) -> dict[str, object]:
        return {
            "close_timestamp": self.close_timestamp.isoformat(),
            "provider_start_timestamp": (
                self.provider_start_timestamp.isoformat()
                if self.provider_start_timestamp is not None
                else None
            ),
            "status": self.status,
            "constituent_bars": self.constituent_bars,
            "coverage": self.coverage,
            "field_deltas": [delta.to_dict() for delta in self.field_deltas],
        }


@dataclass(frozen=True)
class DailyConsolidationAudit:
    """Deterministic reconciliation of production 1m consolidation and 1d bars."""

    minute_bars: int
    provider_daily_bars: int
    consolidated_daily_bars: int
    minimum_constituent_coverage: float
    price_tick: float
    volume_absolute_tolerance: float
    volume_relative_tolerance: float
    input_errors: tuple[str, ...]
    bars: tuple[DailyBarReconciliation, ...]

    @property
    def matched_bars(self) -> int:
        return sum(bar.matches for bar in self.bars)

    @property
    def mismatched_bars(self) -> int:
        return len(self.bars) - self.matched_bars

    @property
    def is_match(self) -> bool:
        return not self.input_errors and self.mismatched_bars == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "minute_bars": self.minute_bars,
            "provider_daily_bars": self.provider_daily_bars,
            "consolidated_daily_bars": self.consolidated_daily_bars,
            "minimum_constituent_coverage": self.minimum_constituent_coverage,
            "price_tick": self.price_tick,
            "volume_absolute_tolerance": self.volume_absolute_tolerance,
            "volume_relative_tolerance": self.volume_relative_tolerance,
            "input_errors": list(self.input_errors),
            "matched_bars": self.matched_bars,
            "mismatched_bars": self.mismatched_bars,
            "is_match": self.is_match,
            "bars": [bar.to_dict() for bar in self.bars],
        }


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{field} must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC)


def _comparison_delta(
    *,
    field: str,
    consolidated: float,
    provider: float,
    tolerance: Decimal,
) -> DailyFieldDelta:
    consolidated_decimal = Decimal(str(consolidated))
    provider_decimal = Decimal(str(provider))
    absolute_delta = abs(consolidated_decimal - provider_decimal)
    return DailyFieldDelta(
        field=field,
        consolidated=float(consolidated_decimal),
        provider=float(provider_decimal),
        absolute_delta=float(absolute_delta),
        tolerance=float(tolerance),
        within_tolerance=absolute_delta <= tolerance,
    )


def _input_errors(minute_bars: list[Bar], provider_daily_bars: list[Bar]) -> tuple[str, ...]:
    errors: list[str] = []
    symbols_by_label: dict[str, set[str]] = {}
    for label, bars, timeframe in (
        ("minute", minute_bars, "1m"),
        ("provider_daily", provider_daily_bars, "1d"),
    ):
        timestamps: list[datetime] = []
        symbols: set[str] = set()
        for index, bar in enumerate(bars):
            try:
                timestamp = _aware_utc(bar.timestamp, field=f"{label}[{index}].timestamp")
            except ValueError as exc:
                errors.append(str(exc))
                continue
            timestamps.append(timestamp)
            symbols.add(bar.symbol.strip().upper())
            if bar.timeframe.strip().lower() != timeframe:
                errors.append(
                    f"{label}[{index}].timeframe={bar.timeframe!r}; expected {timeframe!r}"
                )
            ohlcv_error = ohlcv_invariant_error(
                open_price=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            if ohlcv_error is not None:
                errors.append(f"{label}[{index}] {ohlcv_error}")
        if timestamps != sorted(timestamps):
            errors.append(f"{label} bars are not in ascending timestamp order")
        if len(timestamps) != len(set(timestamps)):
            errors.append(f"{label} bars contain duplicate timestamps")
        if len(symbols) > 1:
            errors.append(f"{label} bars contain multiple symbols: {sorted(symbols)!r}")
        symbols_by_label[label] = symbols
    minute_symbols = symbols_by_label["minute"]
    provider_symbols = symbols_by_label["provider_daily"]
    if minute_symbols and provider_symbols and minute_symbols != provider_symbols:
        errors.append(
            "minute/provider symbols differ: "
            f"minute={sorted(minute_symbols)!r} provider={sorted(provider_symbols)!r}"
        )
    return tuple(sorted(set(errors)))


def audit_minute_daily_reconciliation(
    minute_bars: list[Bar],
    provider_daily_bars: list[Bar],
    *,
    price_tick: float,
    minimum_constituent_coverage: float = 0.95,
    volume_absolute_tolerance: float = 1e-8,
    volume_relative_tolerance: float = 1e-6,
) -> DailyConsolidationAudit:
    """Audit production 1m-to-1d output against provider ``ONE_DAY`` bars.

    Provider daily timestamps denote period starts. Production consolidation
    stamps period closes, so a provider bar at ``D 00:00 UTC`` is compared with
    the consolidated bar at ``D+1 00:00 UTC``. No timestamp, price, volume, or
    low-coverage discrepancy is silently filled or ignored.
    """

    numeric_parameters = {
        "price_tick": price_tick,
        "volume_absolute_tolerance": volume_absolute_tolerance,
        "volume_relative_tolerance": volume_relative_tolerance,
    }
    for name, value in numeric_parameters.items():
        if not math.isfinite(value) or value <= 0.0:
            msg = f"{name} must be finite and > 0"
            raise ValueError(msg)
    if not math.isfinite(minimum_constituent_coverage) or not (
        0.0 < minimum_constituent_coverage <= 1.0
    ):
        msg = "minimum_constituent_coverage must be finite and in (0, 1]"
        raise ValueError(msg)

    errors = _input_errors(minute_bars, provider_daily_bars)
    consolidated: list[Bar] = []
    if not errors:
        consolidator = BarConsolidator(period_minutes=1440, on_bar=consolidated.append)
        for bar in minute_bars:
            consolidator.update(bar)
        consolidator.flush()

    consolidated_by_close = {
        _aware_utc(bar.timestamp, field="consolidated timestamp"): bar for bar in consolidated
    }
    provider_by_close: dict[datetime, tuple[datetime, Bar]] = {}
    if not errors:
        for provider_bar in provider_daily_bars:
            provider_start = _aware_utc(provider_bar.timestamp, field="provider timestamp")
            provider_by_close[provider_start + timedelta(days=1)] = (provider_start, provider_bar)

    price_tolerance = Decimal(str(price_tick))
    volume_absolute = Decimal(str(volume_absolute_tolerance))
    volume_relative = Decimal(str(volume_relative_tolerance))
    reconciled: list[DailyBarReconciliation] = []
    for close_timestamp in sorted(set(consolidated_by_close) | set(provider_by_close)):
        consolidated_bar = consolidated_by_close.get(close_timestamp)
        provider_record = provider_by_close.get(close_timestamp)
        if consolidated_bar is None:
            assert provider_record is not None
            provider_start, _ = provider_record
            reconciled.append(
                DailyBarReconciliation(
                    close_timestamp=close_timestamp,
                    provider_start_timestamp=provider_start,
                    status="missing_consolidated",
                    constituent_bars=None,
                    coverage=None,
                    field_deltas=(),
                )
            )
            continue
        coverage = float(consolidated_bar.metadata.get("coverage", 0.0))
        constituents = int(consolidated_bar.metadata.get("constituent_bars", 0))
        if provider_record is None:
            reconciled.append(
                DailyBarReconciliation(
                    close_timestamp=close_timestamp,
                    provider_start_timestamp=None,
                    status="missing_provider",
                    constituent_bars=constituents,
                    coverage=coverage,
                    field_deltas=(),
                )
            )
            continue

        provider_start, provider_bar = provider_record
        deltas = tuple(
            _comparison_delta(
                field=field,
                consolidated=float(getattr(consolidated_bar, field)),
                provider=float(getattr(provider_bar, field)),
                tolerance=(
                    max(
                        volume_absolute,
                        abs(Decimal(str(provider_bar.volume))) * volume_relative,
                    )
                    if field == "volume"
                    else price_tolerance
                ),
            )
            for field in ("open", "high", "low", "close", "volume")
        )
        within_tolerance = all(delta.within_tolerance for delta in deltas)
        if coverage < minimum_constituent_coverage:
            status = "insufficient_coverage"
        elif within_tolerance:
            status = "matched"
        else:
            status = "field_mismatch"
        reconciled.append(
            DailyBarReconciliation(
                close_timestamp=close_timestamp,
                provider_start_timestamp=provider_start,
                status=status,
                constituent_bars=constituents,
                coverage=coverage,
                field_deltas=deltas,
            )
        )

    return DailyConsolidationAudit(
        minute_bars=len(minute_bars),
        provider_daily_bars=len(provider_daily_bars),
        consolidated_daily_bars=len(consolidated),
        minimum_constituent_coverage=minimum_constituent_coverage,
        price_tick=price_tick,
        volume_absolute_tolerance=volume_absolute_tolerance,
        volume_relative_tolerance=volume_relative_tolerance,
        input_errors=errors,
        bars=tuple(reconciled),
    )
