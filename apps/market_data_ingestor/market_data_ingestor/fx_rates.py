"""Observed FX-rate ingestion.

The service persists two independent, real source legs:

* official EUR-based ECB reference rates (business-day, daily), and
* Coinbase's traded USDC/EUR hourly candles.

Execution accounting may then derive USDC/USD, USDC/INR, and similar values
through EUR without assuming stablecoin parity.  This process is isolated from
the primary crypto-candle scheduler so an ECB outage cannot stall strategies.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from lib_application.services.price_ingestion_service import PriceIngestionService
from lib_common.logging import get_logger
from lib_data.market_data import CandleRow, normalize_product_symbol
from lib_infrastructure.market_data.coinbase_client import CoinbaseCandleClient
from lib_infrastructure.market_data.ecb_client import (
    ECBReferenceRateClient,
    ECBReferenceRateError,
)

logger = get_logger(__name__)

_ECB_BASE_CURRENCY = "EUR"
_COINBASE_GRANULARITY = "ONE_HOUR"
_COINBASE_TIMEFRAME = "1h"
_COINBASE_PERIOD = timedelta(hours=1)
_MAX_COINBASE_HOURS_PER_REQUEST = 300
_MAX_HISTORY_DAYS = 90
_MIN_POLL_INTERVAL_SECONDS = 60


class FXRateIngestionError(RuntimeError):
    """A required FX observation could not be ingested safely."""


@dataclass(frozen=True)
class FXIngestionSummary:
    """One completed FX sync."""

    ecb_observations: int
    coinbase_observations: int
    rows_upserted: int
    completed_at: datetime


class FXRateIngestor:
    """Persist point-in-time ECB and Coinbase FX observations."""

    def __init__(
        self,
        *,
        session_factory: Any,
        quote_currencies: set[str],
        history_days: int = 90,
        poll_interval_sec: int = 21600,
        coinbase_product: str = "USDC-EUR",
        ecb_client: ECBReferenceRateClient | None = None,
        coinbase_client: CoinbaseCandleClient | None = None,
    ) -> None:
        currencies = {
            str(currency or "").strip().upper()
            for currency in quote_currencies
            if str(currency or "").strip()
        }
        currencies.discard(_ECB_BASE_CURRENCY)
        if not currencies:
            msg = "FX ingestion requires at least one non-EUR ECB quote currency"
            raise ValueError(msg)
        if history_days < 1 or history_days > _MAX_HISTORY_DAYS:
            msg = "FX history_days must be between 1 and 90"
            raise ValueError(msg)
        if poll_interval_sec < _MIN_POLL_INTERVAL_SECONDS:
            msg = "FX poll_interval_sec must be at least 60"
            raise ValueError(msg)
        normalized_product = coinbase_product.strip().upper()
        if normalized_product != "USDC-EUR":
            msg = "The observed stablecoin FX leg must be Coinbase USDC-EUR"
            raise ValueError(msg)

        self._quote_currencies = currencies
        self._history_days = history_days
        self._poll_interval_sec = poll_interval_sec
        self._coinbase_product = normalized_product
        self._ecb_client = ecb_client or ECBReferenceRateClient()
        self._coinbase_client = coinbase_client or CoinbaseCandleClient()
        self._ingestion_svc = PriceIngestionService(session_factory)
        self._stop_event = threading.Event()
        self._last_success_at: datetime | None = None
        self._closed = False

    def run_once(self, *, now: datetime | None = None) -> FXIngestionSummary:
        """Fetch and persist all required source observations once."""
        requested_at = _as_utc(now or datetime.now(tz=UTC))
        instrument_map = self._ingestion_svc.load_instrument_map(asset_class="fx")

        ecb_rows = self._ecb_rows(instrument_map)
        coinbase_rows = self._coinbase_rows(
            instrument_map=instrument_map,
            requested_at=requested_at,
        )
        upserted = self._ingestion_svc.upsert_candles([*ecb_rows, *coinbase_rows])
        completed_at = datetime.now(tz=UTC)
        self._last_success_at = completed_at
        summary = FXIngestionSummary(
            ecb_observations=len(ecb_rows),
            coinbase_observations=len(coinbase_rows),
            rows_upserted=upserted,
            completed_at=completed_at,
        )
        logger.info(
            "FX observations synchronized",
            ecb_observations=summary.ecb_observations,
            coinbase_observations=summary.coinbase_observations,
            rows_upserted=summary.rows_upserted,
        )
        return summary

    def run_forever(self) -> None:
        """Run immediately, then retry source/DB failures on the configured interval."""
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except (ECBReferenceRateError, FXRateIngestionError, httpx.HTTPError, SQLAlchemyError):
                logger.exception(
                    "FX observation sync failed; retaining prior persisted rates and retrying"
                )
            if self._stop_event.wait(self._poll_interval_sec):
                break

    def stop(self) -> None:
        self._stop_event.set()
        if self._closed:
            return
        self._closed = True
        self._ecb_client.close()
        self._coinbase_client.close()

    def is_fresh(self) -> bool:
        """Whether a complete source sync succeeded within two poll intervals."""
        if self._last_success_at is None:
            return False
        age = datetime.now(tz=UTC) - self._last_success_at
        return timedelta(0) <= age <= timedelta(seconds=2 * self._poll_interval_sec)

    def _ecb_rows(self, instrument_map: dict[str, int]) -> list[CandleRow]:
        observations = self._ecb_client.fetch_rates(quote_currencies=self._quote_currencies)
        rows: list[CandleRow] = []
        for observation in observations:
            canonical = f"{_ECB_BASE_CURRENCY}{observation.quote_currency}"
            instr_id = instrument_map.get(canonical)
            if instr_id is None:
                msg = (
                    "FX instrument catalogue is missing "
                    f"{_ECB_BASE_CURRENCY}/{observation.quote_currency}"
                )
                raise FXRateIngestionError(msg)
            value = float(observation.rate)
            rows.append(
                CandleRow(
                    instr_id=instr_id,
                    ts=observation.observed_at.astimezone(UTC).replace(tzinfo=None),
                    timeframe="1d",
                    open=value,
                    high=value,
                    low=value,
                    close=value,
                    volume=0.0,
                    source=observation.source,
                )
            )
        if not rows:
            msg = "ECB reference-rate feed returned no requested observations"
            raise FXRateIngestionError(msg)
        return rows

    def _coinbase_rows(
        self,
        *,
        instrument_map: dict[str, int],
        requested_at: datetime,
    ) -> list[CandleRow]:
        canonical = normalize_product_symbol(self._coinbase_product)
        instr_id = instrument_map.get(canonical)
        if instr_id is None:
            msg = f"FX instrument catalogue is missing {self._coinbase_product}"
            raise FXRateIngestionError(msg)

        closed_end = requested_at.replace(minute=0, second=0, microsecond=0)
        desired_start = closed_end - timedelta(days=self._history_days)
        latest = self._ingestion_svc.latest_candle_ts(
            instr_id,
            source=CoinbaseCandleClient.source,
            timeframe=_COINBASE_TIMEFRAME,
        )
        ranges: list[tuple[datetime, datetime]] = []
        if latest is None:
            ranges.append((desired_start, closed_end))
        else:
            latest = _as_utc(latest)
            oldest = self._ingestion_svc.oldest_candle_ts(
                instr_id,
                source=CoinbaseCandleClient.source,
                timeframe=_COINBASE_TIMEFRAME,
            )
            if oldest is not None and _as_utc(oldest) > desired_start:
                ranges.append((desired_start, min(_as_utc(oldest), closed_end)))
            catchup_start = max(desired_start, latest - _COINBASE_PERIOD)
            if catchup_start < closed_end:
                ranges.append((catchup_start, closed_end))

        rows: list[CandleRow] = []
        for start, end in _merge_ranges(ranges):
            rows.extend(
                self._fetch_coinbase_range(
                    instr_id=instr_id,
                    start=start,
                    end=end,
                )
            )
        if ranges and not rows:
            msg = f"Coinbase returned no closed {self._coinbase_product} observations"
            raise FXRateIngestionError(msg)
        return rows

    def _fetch_coinbase_range(
        self,
        *,
        instr_id: int,
        start: datetime,
        end: datetime,
    ) -> list[CandleRow]:
        rows: list[CandleRow] = []
        cursor = start
        request_span = timedelta(hours=_MAX_COINBASE_HOURS_PER_REQUEST)
        while cursor < end:
            chunk_end = min(cursor + request_span, end)
            fetched = self._coinbase_client.fetch_candle_rows(
                product_id=self._coinbase_product,
                instr_id=instr_id,
                start_time=cursor,
                end_time=chunk_end,
                granularity=_COINBASE_GRANULARITY,
            )
            # Coinbase bounds are inclusive.  Keep a half-open range locally so
            # adjacent requests cannot duplicate the boundary observation.
            rows.extend(row for row in fetched if cursor <= _as_utc(row.ts) < chunk_end)
            cursor = chunk_end
        return rows


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _merge_ranges(
    ranges: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    ordered = sorted((start, end) for start, end in ranges if start < end)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return merged


__all__ = [
    "FXIngestionSummary",
    "FXRateIngestionError",
    "FXRateIngestor",
]
