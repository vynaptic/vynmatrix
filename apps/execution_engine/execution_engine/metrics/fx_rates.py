"""Observed FX-rate boundary for monetary normalization.

Rates come from the platform's persisted market-price ledger.  The converter
has no implicit parity fallback: different currencies require a positive,
fresh observation or accounting fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any, Protocol

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError

_MAX_CURRENCY_CODE_LENGTH = 10
_CROSS_RATE_ANCHOR = "EUR"
_DIRECT_ONLY_CURRENCIES = frozenset({"BTC", "ETH"})
_RATE_ASSET_CLASSES = ("fx", "crypto")
_COINBASE_CLOSE_LAGS = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
}


class FXRateUnavailableError(RuntimeError):
    """A required observed currency conversion is unavailable or stale."""


def normalize_currency(currency: str) -> str:
    """Normalize and validate an ISO-style currency/settlement code."""
    normalized = str(currency or "").strip().upper()
    if not normalized or len(normalized) > _MAX_CURRENCY_CODE_LENGTH or not normalized.isalnum():
        msg = f"Invalid currency code: {currency!r}"
        raise ValueError(msg)
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _observation_available_at(
    *,
    timestamp: datetime,
    timeframe: str,
    source: str,
) -> datetime | None:
    """Return when a persisted price was actually knowable.

    Coinbase candle timestamps identify the interval start, while the stored
    close is only observable after the full interval. Unknown Coinbase
    timeframes fail closed instead of being treated as point observations.
    Non-Coinbase feeds (including ECB reference rates) persist their explicit
    publication timestamp and need no additional lag.
    """
    observed_at = _utc(timestamp)
    if not str(source or "").strip().lower().startswith("coinbase"):
        return observed_at
    close_lag = _COINBASE_CLOSE_LAGS.get(str(timeframe or "").strip().lower())
    return None if close_lag is None else observed_at + close_lag


@dataclass(frozen=True)
class ObservedFXRate:
    """One real rate observation: quote-currency units per base unit."""

    base_currency: str
    quote_currency: str
    rate: Decimal
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        base = normalize_currency(self.base_currency)
        quote = normalize_currency(self.quote_currency)
        try:
            rate = Decimal(str(self.rate))
        except (InvalidOperation, TypeError, ValueError) as exc:
            msg = "FX rate must be a finite positive decimal"
            raise ValueError(msg) from exc
        if not rate.is_finite() or rate <= 0:
            msg = "FX rate must be a finite positive decimal"
            raise ValueError(msg)
        if base == quote and rate != 1:
            msg = "Same-currency FX observations must have rate 1"
            raise ValueError(msg)
        source = str(self.source or "").strip()
        if not source:
            msg = "FX rate observation requires a source"
            raise ValueError(msg)
        object.__setattr__(self, "base_currency", base)
        object.__setattr__(self, "quote_currency", quote)
        object.__setattr__(self, "rate", rate)
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        object.__setattr__(self, "source", source)


class FXRateProvider(Protocol):
    """Port for a real point-in-time FX observation source."""

    def get_rate(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        as_of: datetime,
    ) -> ObservedFXRate | None:
        """Return the latest eligible observation at or before ``as_of``."""


class SqlFXRateProvider:
    """Read point-in-time FX observations from instruments + prices.

    A directly observed pair is preferred.  When no direct observation exists,
    one cross through EUR is allowed because ECB reference rates are EUR-based.
    Both legs must independently satisfy the point-in-time and freshness gates;
    no stablecoin parity or synthetic constant is ever assumed.
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        max_age: timedelta = timedelta(days=3),
    ) -> None:
        if max_age <= timedelta(0):
            msg = "FX max_age must be positive"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._max_age = max_age

    def get_rate(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        as_of: datetime,
    ) -> ObservedFXRate | None:
        base = normalize_currency(base_currency)
        quote = normalize_currency(quote_currency)
        requested_at = _utc(as_of)
        if base == quote:
            return ObservedFXRate(
                base_currency=base,
                quote_currency=quote,
                rate=Decimal("1"),
                observed_at=requested_at,
                source="identity",
            )

        requested_pairs = {(base, quote)}
        direct_only = bool({base, quote} & _DIRECT_ONLY_CURRENCIES)
        if not direct_only and _CROSS_RATE_ANCHOR not in (base, quote):
            requested_pairs.update(
                {
                    (base, _CROSS_RATE_ANCHOR),
                    (_CROSS_RATE_ANCHOR, quote),
                }
            )
        observations = self._load_observations(
            pairs=requested_pairs,
            requested_at=requested_at,
        )
        direct = observations.get((base, quote))
        if direct is not None:
            return direct

        first_leg = observations.get((base, _CROSS_RATE_ANCHOR))
        second_leg = observations.get((_CROSS_RATE_ANCHOR, quote))
        if first_leg is None or second_leg is None:
            return None
        return ObservedFXRate(
            base_currency=base,
            quote_currency=quote,
            rate=first_leg.rate * second_leg.rate,
            # The cross is only as recent as its oldest source leg.  This keeps
            # downstream freshness checks conservative.
            observed_at=min(first_leg.observed_at, second_leg.observed_at),
            source=(f"cross:{_CROSS_RATE_ANCHOR}:{first_leg.source}|{second_leg.source}"),
        )

    def _load_observations(
        self,
        *,
        pairs: set[tuple[str, str]],
        requested_at: datetime,
    ) -> dict[tuple[str, str], ObservedFXRate]:
        """Load the newest fresh direct/inverse observation for each pair."""
        from lib_application.db.models import (  # noqa: PLC0415
            Instrument,
            InstrumentPrice,
        )

        candidates: set[str] = set()
        compact_pairs: dict[str, tuple[tuple[str, str], bool]] = {}
        for base, quote in pairs:
            direct = f"{base}{quote}"
            inverse_symbol = f"{quote}{base}"
            compact_pairs[direct] = ((base, quote), False)
            compact_pairs[inverse_symbol] = ((base, quote), True)
            for separator in ("", "/", "-", "_"):
                candidates.add(f"{base}{separator}{quote}")
                candidates.add(f"{quote}{separator}{base}")
        query_at = requested_at.replace(tzinfo=None)
        try:
            with self._session_factory() as session:
                rows = (
                    session.query(
                        Instrument.canonical,
                        InstrumentPrice.close,
                        InstrumentPrice.ts,
                        InstrumentPrice.source,
                        InstrumentPrice.timeframe,
                    )
                    .join(
                        InstrumentPrice,
                        InstrumentPrice.instr_id == Instrument.instr_id,
                    )
                    .filter(
                        Instrument.asset_class.in_(_RATE_ASSET_CLASSES),
                        func.upper(Instrument.canonical).in_(candidates),
                        InstrumentPrice.ts <= query_at,
                    )
                    .order_by(InstrumentPrice.ts.desc())
                    .all()
                )
        except SQLAlchemyError as exc:
            rendered_pairs = ", ".join(f"{base}/{quote}" for base, quote in sorted(pairs))
            msg = f"Unable to read FX observations for {rendered_pairs}"
            raise FXRateUnavailableError(msg) from exc

        resolved: dict[tuple[str, str], ObservedFXRate] = {}
        for canonical, raw_rate, candle_at, source, timeframe in rows:
            pair = (
                str(canonical or "")
                .strip()
                .upper()
                .replace("/", "")
                .replace("-", "")
                .replace("_", "")
            )
            pair_match = compact_pairs.get(pair)
            if pair_match is None:
                continue
            requested_pair, is_inverse = pair_match
            if requested_pair in resolved:
                continue
            observed = _observation_available_at(
                timestamp=candle_at,
                timeframe=str(timeframe or ""),
                source=str(source or ""),
            )
            if observed is None:
                continue
            age = requested_at - observed
            if age < timedelta(0) or age > self._max_age:
                continue
            try:
                rate = Decimal(str(raw_rate))
                if is_inverse:
                    rate = Decimal("1") / rate
            except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
                continue
            if not rate.is_finite() or rate <= 0:
                continue
            resolved[requested_pair] = ObservedFXRate(
                base_currency=requested_pair[0],
                quote_currency=requested_pair[1],
                rate=rate,
                observed_at=observed,
                source=str(source or "prices"),
            )
        return resolved


@dataclass(frozen=True)
class _CachedRate:
    quote: ObservedFXRate
    cached_at: datetime


class CachedFXRateProvider:
    """Small process-local cache in front of a real FX provider."""

    def __init__(
        self,
        upstream: FXRateProvider,
        *,
        cache_ttl: timedelta = timedelta(minutes=5),
        max_observation_age: timedelta = timedelta(days=3),
    ) -> None:
        if cache_ttl <= timedelta(0) or max_observation_age <= timedelta(0):
            msg = "FX cache durations must be positive"
            raise ValueError(msg)
        self._upstream = upstream
        self._cache_ttl = cache_ttl
        self._max_observation_age = max_observation_age
        self._cache: dict[tuple[str, str, int], _CachedRate] = {}
        self._lock = RLock()

    def get_rate(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        as_of: datetime,
    ) -> ObservedFXRate | None:
        base = normalize_currency(base_currency)
        quote = normalize_currency(quote_currency)
        requested_at = _utc(as_of)
        now = datetime.now(tz=UTC)
        # Include the requested point-in-time bucket.  A process replaying
        # historical fills must not reuse Monday's quote for Tuesday merely
        # because both calls happened within the same five wall-clock minutes.
        bucket_seconds = max(1, int(self._cache_ttl.total_seconds()))
        key = (base, quote, int(requested_at.timestamp()) // bucket_seconds)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                observation_age = requested_at - cached.quote.observed_at
                cache_age = now - cached.cached_at
                if (
                    timedelta(0) <= observation_age <= self._max_observation_age
                    and timedelta(0) <= cache_age <= self._cache_ttl
                ):
                    return cached.quote

        quote_value = self._upstream.get_rate(
            base_currency=base,
            quote_currency=quote,
            as_of=requested_at,
        )
        if quote_value is None:
            return None
        observation_age = requested_at - quote_value.observed_at
        if not timedelta(0) <= observation_age <= self._max_observation_age:
            return None
        with self._lock:
            self._cache[key] = _CachedRate(quote=quote_value, cached_at=now)
        return quote_value


class FXConverter:
    """Convert monetary amounts with explicit observed rates."""

    def __init__(self, provider: FXRateProvider) -> None:
        self._provider = provider

    def convert(
        self,
        amount: Decimal,
        *,
        from_currency: str,
        to_currency: str,
        as_of: datetime,
    ) -> Decimal:
        source = normalize_currency(from_currency)
        target = normalize_currency(to_currency)
        value = Decimal(str(amount))
        if not value.is_finite():
            msg = "Monetary amount must be finite"
            raise ValueError(msg)
        if source == target:
            return value
        quote = self._provider.get_rate(
            base_currency=source,
            quote_currency=target,
            as_of=as_of,
        )
        if quote is None:
            msg = f"No observed FX rate for {source}/{target} at {_utc(as_of).isoformat()}"
            raise FXRateUnavailableError(msg)
        return value * quote.rate


__all__ = [
    "CachedFXRateProvider",
    "FXConverter",
    "FXRateProvider",
    "FXRateUnavailableError",
    "ObservedFXRate",
    "SqlFXRateProvider",
    "normalize_currency",
]
