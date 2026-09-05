"""Provider-neutral historical warmup for daily/swing strategies.

The live scheduler only backfills a short startup window. This isolated
orchestrator fetches a configured venue's official candle API through the same
provider registry and exact broker catalogue identity as live ingestion,
upserting each batch idempotently with bounded transient retries. A provider's
intrinsic source tag is validated on every row; data is never relabelled or
silently fetched from another venue.

Two per-market policies share this single backfill surface:

- **Continuous markets** (crypto): request-sized window paging with wall-clock
  coverage floors and a bounded empty-window budget.
- **Scheduled markets at daily granularity** (equities/indices): one request
  per symbol, calendar-aware session coverage, and corporate-action loading
  when the configured provider exposes it. Policy rationale lives in
  ``_scheduled_backfill.py`` (private to this module).

Usage:
    python -m market_data_ingestor.backfill --days 150
    python -m market_data_ingestor.backfill --days 150 --symbols BTC-USDC,ETH-USDC
"""

from __future__ import annotations

import argparse
import math
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from lib_application.services.price_ingestion_service import PriceIngestionService
from lib_common.logging import get_logger
from lib_data.market_data import CandleRow, MarketDataInstrument, normalize_product_symbol
from lib_infrastructure.market_data.models import (
    GRANULARITY_TO_SECONDS,
    GRANULARITY_TO_TIMEFRAME,
    IngestionSummary,
)
from lib_infrastructure.market_data.ports import IMarketDataProvider
from lib_infrastructure.market_data.providers import get_market_data_provider

from ._scheduled_backfill import (
    SCHEDULED_DAILY_MIN_COVERAGE,
    ICorporateActionSource,
    ScheduledDailySummary,
    compute_scheduled_daily_summary,
    derive_corporate_action_source,
    expected_scheduled_sessions,
    upsert_corporate_actions,
)

logger = get_logger(__name__)

# A conservative common window stays within every registered provider's
# documented/sample-tested historical response bounds.
_MAX_CANDLES_PER_REQUEST = 300
_GRANULARITY_MINUTES: dict[str, int] = {
    "ONE_MINUTE": 1,
    "FIVE_MINUTE": 5,
    "FIFTEEN_MINUTE": 15,
    "THIRTY_MINUTE": 30,
    "ONE_HOUR": 60,
    "TWO_HOUR": 120,
    "SIX_HOUR": 360,
    "ONE_DAY": 1440,
}
_MAX_WINDOW_ATTEMPTS = 4
_MAX_EMPTY_WINDOWS_PER_SYMBOL = 1
_CONTINUOUS_COVERAGE_FLOOR = 0.95
_SCHEDULED_COVERAGE_FLOOR = 0.15
_SCHEDULED_BOUNDARY_TOLERANCE = timedelta(days=7)
_DAILY_GRANULARITY = "ONE_DAY"


class HistoricalBackfillError(RuntimeError):
    """Raised when a requested historical window cannot be retrieved safely."""


class UnknownInstrumentError(HistoricalBackfillError):
    """Raised when a configured symbol has no instrument row (fail loud)."""


class HistoricalBackfiller:
    """Page one exact venue's historical candles into the ``prices`` table."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        instruments: list[MarketDataInstrument],
        granularity: str = "ONE_MINUTE",
        source: str = "coinbase_live",
        api_key: str | None = None,
        api_secret: str | None = None,
        access_token: str | None = None,
        access_token_expires_at: str | None = None,
        account_key: str | None = None,
        request_pause_sec: float = 0.25,
        minimum_window_coverage: float = 0.0,
        minimum_total_coverage: float | None = None,
        provider: IMarketDataProvider | None = None,
        calendar_mic: str = "XNYS",
        ingestion_service: PriceIngestionService | None = None,
        corporate_action_source: ICorporateActionSource | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._instruments = list(instruments)
        if not self._instruments:
            msg = "Historical backfill requires at least one instrument"
            raise ValueError(msg)
        canonical_keys = [
            normalize_product_symbol(instrument.canonical_symbol)
            for instrument in self._instruments
        ]
        if len(set(canonical_keys)) != len(canonical_keys):
            msg = "Historical backfill canonical instruments must be unique"
            raise ValueError(msg)
        self._granularity = granularity.strip().upper()
        if self._granularity not in GRANULARITY_TO_TIMEFRAME:
            msg = f"Unsupported granularity: {self._granularity}"
            raise ValueError(msg)
        configured_source = source.strip()
        if not configured_source:
            msg = "Historical backfill source must be non-empty"
            raise ValueError(msg)
        self._source = configured_source
        self._request_pause_sec = max(0.0, request_pause_sec)
        if not 0.0 <= minimum_window_coverage <= 1.0:
            msg = "minimum_window_coverage must be between 0 and 1"
            raise ValueError(msg)
        if minimum_total_coverage is not None and not 0.0 <= minimum_total_coverage <= 1.0:
            msg = "minimum_total_coverage must be between 0 and 1"
            raise ValueError(msg)
        self._minimum_window_coverage = minimum_window_coverage
        self._minimum_total_coverage = minimum_total_coverage
        self._period_seconds = GRANULARITY_TO_SECONDS[self._granularity]
        self._provider = provider or get_market_data_provider(
            self._source,
            api_key=api_key,
            api_secret=api_secret,
            access_token=access_token,
            access_token_expires_at=access_token_expires_at,
            account_key=account_key,
        )
        if self._provider.source != self._source:
            msg = (
                "HistoricalBackfiller provider/source mismatch: configured "
                f"{self._source!r}, provider emits {self._provider.source!r}"
            )
            raise ValueError(msg)
        normalized_mic = calendar_mic.strip().upper()
        if not normalized_mic:
            msg = "Historical backfill calendar_mic must be non-empty"
            raise ValueError(msg)
        self._calendar_mic = normalized_mic
        self._session_factory = session_factory
        self._ingestion_svc = ingestion_service or PriceIngestionService(session_factory)
        self._corporate_actions = corporate_action_source or derive_corporate_action_source(
            self._provider
        )
        self._scheduled_summaries: list[ScheduledDailySummary] = []
        self._sleep = sleep

    @property
    def timeframe(self) -> str:
        return str(GRANULARITY_TO_TIMEFRAME[self._granularity])

    @property
    def scheduled_daily_summaries(self) -> tuple[ScheduledDailySummary, ...]:
        """Per-symbol scheduled-daily outcomes of the most recent run."""
        return tuple(self._scheduled_summaries)

    @property
    def corporate_actions_supported(self) -> bool:
        """Whether this run can load splits/dividends for scheduled instruments."""
        return self._corporate_actions is not None and any(
            not self._is_continuous(instrument) for instrument in self._instruments
        )

    def close(self) -> None:
        self._provider.close()

    def _closed_boundary(self, value: datetime) -> datetime:
        """Return the exclusive end boundary of the last fully closed bucket."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        epoch = int(value.astimezone(UTC).timestamp())
        return datetime.fromtimestamp(epoch - (epoch % self._period_seconds), tz=UTC)

    def _minimum_rows(self, start: datetime, end: datetime, *, coverage: float) -> int:
        expected = max(1, math.ceil((end - start).total_seconds() / self._period_seconds))
        return math.ceil(expected * coverage)

    @staticmethod
    def _is_continuous(instrument: MarketDataInstrument) -> bool:
        return instrument.asset_class == "crypto"

    def _is_scheduled_daily(self, instrument: MarketDataInstrument) -> bool:
        """Whether the scheduled-market daily policy applies to ``instrument``.

        Only non-continuous instruments at daily granularity qualify; scheduled
        intraday feeds keep the generic paging path with its wall-clock floors.
        """
        return not self._is_continuous(instrument) and self._granularity == _DAILY_GRANULARITY

    @staticmethod
    def _last_session_date(end: datetime) -> date:
        """Inclusive last session date covered by the half-open end boundary."""
        return (end - timedelta(microseconds=1)).astimezone(UTC).date()

    def _coverage_floor(self, instrument: MarketDataInstrument) -> float:
        if self._minimum_total_coverage is not None:
            return self._minimum_total_coverage
        return (
            _CONTINUOUS_COVERAGE_FLOOR
            if self._is_continuous(instrument)
            else _SCHEDULED_COVERAGE_FLOOR
        )

    def _has_required_coverage(
        self,
        instr_id: int,
        start: datetime,
        end: datetime,
        *,
        instrument: MarketDataInstrument,
    ) -> bool:
        coverage = self._coverage_floor(instrument)
        if coverage <= 0:
            return True
        actual = self._ingestion_svc.candle_count(
            instr_id,
            source=self._source,
            timeframe=self.timeframe,
            start=start,
            end=end,
        )
        required = self._minimum_rows(start, end, coverage=coverage)
        if actual >= required:
            return True
        logger.warning(
            "Historical coverage below gate for %s: rows=%d required=%d window=%s..%s",
            instrument.canonical_symbol,
            actual,
            required,
            start,
            end,
        )
        return False

    def _has_recent_tail(
        self,
        instr_id: int,
        end: datetime,
        *,
        instrument: MarketDataInstrument,
    ) -> bool:
        """Require at least one recently closed candle when coverage is gated.

        Aggregate coverage alone can hide a stale suffix: six missing hours are
        only a small fraction of a 150-day one-minute window. Deployment warmup
        must therefore prove both depth and a current tail. Continuous feeds use
        two periods; scheduled feeds use a conservative seven-day bound without
        inventing weekdays, holidays, or venue hours.
        """
        if self._coverage_floor(instrument) <= 0:
            return True
        tail_window = (
            timedelta(seconds=2 * self._period_seconds)
            if self._is_continuous(instrument)
            else _SCHEDULED_BOUNDARY_TOLERANCE
        )
        tail_start = end - tail_window
        actual = self._ingestion_svc.candle_count(
            instr_id,
            source=self._source,
            timeframe=self.timeframe,
            start=tail_start,
            end=end,
        )
        if actual > 0:
            return True
        logger.warning(
            "Historical tail is stale for %s: no candle in %s..%s",
            instrument.canonical_symbol,
            tail_start,
            end,
        )
        return False

    def _load_instrument_ids(self) -> dict[str, int]:
        maps_by_asset_class: dict[str, dict[str, int]] = {}
        resolved: dict[str, int] = {}
        unknown: list[str] = []
        for instrument in self._instruments:
            if instrument.asset_class not in maps_by_asset_class:
                maps_by_asset_class[instrument.asset_class] = (
                    self._ingestion_svc.load_instrument_map(
                        asset_class=instrument.asset_class,
                    )
                )
            instrument_map = maps_by_asset_class[instrument.asset_class]
            canonical_key = normalize_product_symbol(instrument.canonical_symbol)
            instr_id = instrument_map.get(canonical_key)
            if instr_id is None:
                unknown.append(instrument.canonical_symbol)
                continue
            resolved[canonical_key] = instr_id
        if unknown:
            msg = (
                "Configured historical backfill instrument is unknown: "
                f"{', '.join(sorted(unknown))} — seed the instruments catalogue "
                "before backfilling (fail closed, never guess ids)"
            )
            raise UnknownInstrumentError(msg)
        return resolved

    def backfill(self, *, days: int, now: datetime | None = None) -> IngestionSummary:
        """Backfill ``days`` of history for every configured symbol (full window)."""
        end = self._closed_boundary(now or datetime.now(tz=UTC))
        start = end - timedelta(days=max(1, days))
        summary = self._backfill_range(start=start, end=end)
        logger.info(
            "Historical backfill finished: products=%d rows_upserted=%d days=%d",
            summary.products_processed,
            summary.rows_upserted,
            days,
        )
        return summary

    def backfill_range(
        self,
        *,
        start: datetime,
        end: datetime,
        now: datetime | None = None,
    ) -> IngestionSummary:
        """Backfill one exact half-open UTC range ``[start, end)``.

        Unlike the deployment-oriented ``days`` interface, this contract is
        suitable for frozen research windows: neither boundary is derived from
        wall-clock time and a future or granularity-misaligned endpoint fails
        before any provider request or DB write.
        """

        start_utc = self._validate_range_boundary(start, name="start")
        end_utc = self._validate_range_boundary(end, name="end")
        if start_utc >= end_utc:
            msg = "Historical backfill range requires start < end"
            raise ValueError(msg)
        latest_closed = self._closed_boundary(now or datetime.now(tz=UTC))
        if end_utc > latest_closed:
            msg = (
                "Historical backfill end exceeds the last fully closed candle: "
                f"end={end_utc.isoformat()} latest_closed={latest_closed.isoformat()}"
            )
            raise ValueError(msg)
        summary = self._backfill_range(start=start_utc, end=end_utc)
        logger.info(
            "Exact historical backfill finished: products=%d rows_upserted=%d range=%s..%s",
            summary.products_processed,
            summary.rows_upserted,
            start_utc,
            end_utc,
        )
        return summary

    def _validate_range_boundary(self, value: datetime, *, name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = f"Historical backfill {name} must be timezone-aware"
            raise ValueError(msg)
        value_utc = value.astimezone(UTC)
        if int(value_utc.timestamp()) % self._period_seconds != 0:
            msg = (
                f"Historical backfill {name} must align to {self._granularity}: "
                f"{value_utc.isoformat()}"
            )
            raise ValueError(msg)
        return value_utc

    def _backfill_range(self, *, start: datetime, end: datetime) -> IngestionSummary:
        instrument_ids = self._load_instrument_ids()
        summary = IngestionSummary()
        self._scheduled_summaries = []

        for instrument in self._instruments:
            empty_windows: set[tuple[datetime, datetime]] = set()
            instr_id = instrument_ids[normalize_product_symbol(instrument.canonical_symbol)]
            if self._is_scheduled_daily(instrument):
                self._backfill_scheduled_daily(instrument, instr_id, start, end, summary)
                summary.products_processed += 1
                continue
            self._page_symbol(
                instrument,
                instr_id,
                start,
                end,
                summary,
                empty_windows=empty_windows,
            )
            if not self._has_required_coverage(
                instr_id,
                start,
                end,
                instrument=instrument,
            ):
                msg = (
                    "Historical coverage gate failed after full backfill for "
                    f"{instrument.canonical_symbol}"
                )
                raise HistoricalBackfillError(msg)
            summary.products_processed += 1
        return summary

    def ensure_history(self, *, days: int, now: datetime | None = None) -> IngestionSummary:
        """Backfill only the history *missing* before the oldest stored candle.

        Deficit-aware variant of :meth:`backfill` for an auto-warm-at-deploy run
        (SW-2): for each symbol it fetches only the window between the desired
        start (``now - days``) and the oldest candle already stored, so a restart
        does not re-page history that is already present. A symbol already
        covered back to the desired start is skipped entirely.
        """
        end = self._closed_boundary(now or datetime.now(tz=UTC))
        desired_start = end - timedelta(days=max(1, days))
        instrument_ids = self._load_instrument_ids()
        summary = IngestionSummary()
        self._scheduled_summaries = []

        for instrument in self._instruments:
            # Keep one bounded-gap budget for the entire symbol operation. A
            # sparse-history repair can page the same range a second time; the
            # same window remains one gap, while a distinct second empty window
            # must fail closed.
            empty_windows: set[tuple[datetime, datetime]] = set()
            instr_id = instrument_ids[normalize_product_symbol(instrument.canonical_symbol)]
            if self._is_scheduled_daily(instrument):
                # Deficit awareness for the daily policy is a calendar-coverage
                # + recent-tail pre-check; when not covered, the whole window is
                # re-fetched in one idempotent request (no resume watermark to
                # protect at ~1 row per session).
                if self._scheduled_daily_history_is_current(
                    instr_id,
                    desired_start,
                    end,
                    instrument=instrument,
                ):
                    logger.info(
                        "Backfill skipped for %s: scheduled daily history already covers %s",
                        instrument.canonical_symbol,
                        desired_start,
                    )
                    continue
                self._backfill_scheduled_daily(instrument, instr_id, desired_start, end, summary)
                summary.products_processed += 1
                continue
            oldest = self._ingestion_svc.oldest_candle_ts(
                instr_id, source=self._source, timeframe=self.timeframe
            )
            # Guard the tz-aware comparisons below: a service backed by a
            # ``timestamp without time zone`` column can hand back a naive value.
            if oldest is not None and oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=UTC)
            start_tolerance = (
                timedelta(0) if self._is_continuous(instrument) else _SCHEDULED_BOUNDARY_TOLERANCE
            )
            if oldest is not None and oldest <= desired_start + start_tolerance:
                has_coverage = self._has_required_coverage(
                    instr_id,
                    desired_start,
                    end,
                    instrument=instrument,
                )
                if has_coverage and self._has_recent_tail(
                    instr_id,
                    end,
                    instrument=instrument,
                ):
                    logger.info(
                        "Backfill skipped for %s: history already covers %s (oldest=%s)",
                        instrument.canonical_symbol,
                        desired_start,
                        oldest,
                    )
                    continue
                if has_coverage:
                    # Repair only one API-sized recent suffix first. This closes
                    # normal feed downtime without needlessly re-paging 150 days;
                    # the aggregate and tail gates below still fail closed.
                    tail_start = max(
                        desired_start,
                        end - timedelta(seconds=self._period_seconds * _MAX_CANDLES_PER_REQUEST),
                    )
                    logger.warning(
                        "Repairing stale historical tail for %s from %s",
                        instrument.canonical_symbol,
                        tail_start,
                    )
                    self._page_symbol(
                        instrument,
                        instr_id,
                        tail_start,
                        end,
                        summary,
                        newest_first=True,
                        empty_windows=empty_windows,
                    )
                    if self._has_required_coverage(
                        instr_id,
                        desired_start,
                        end,
                        instrument=instrument,
                    ) and self._has_recent_tail(
                        instr_id,
                        end,
                        instrument=instrument,
                    ):
                        summary.products_processed += 1
                        continue
                # An interrupted run may leave a boundary row and interior
                # holes. Re-fetch the full desired range idempotently.
                oldest = None
            # Fetch only the missing older portion (up to the oldest we have, or
            # the full window when nothing is stored yet).
            window_end = oldest if (oldest is not None and oldest < end) else end
            # Page newest-to-oldest. Each committed partial run therefore leaves
            # one contiguous suffix ending at the previously stored history, so
            # ``oldest_candle_ts`` remains a safe resume watermark after failure.
            self._page_symbol(
                instrument,
                instr_id,
                desired_start,
                window_end,
                summary,
                newest_first=True,
                empty_windows=empty_windows,
            )
            if not self._has_required_coverage(
                instr_id,
                desired_start,
                end,
                instrument=instrument,
            ) or not self._has_recent_tail(
                instr_id,
                end,
                instrument=instrument,
            ):
                if window_end < end:
                    logger.warning(
                        "Repairing sparse historical suffix for %s with a full-range pass",
                        instrument.canonical_symbol,
                    )
                    self._page_symbol(
                        instrument,
                        instr_id,
                        desired_start,
                        end,
                        summary,
                        newest_first=True,
                        empty_windows=empty_windows,
                    )
                if not self._has_required_coverage(
                    instr_id,
                    desired_start,
                    end,
                    instrument=instrument,
                ) or not self._has_recent_tail(
                    instr_id,
                    end,
                    instrument=instrument,
                ):
                    msg = (
                        "Historical coverage gate failed after repair for "
                        f"{instrument.canonical_symbol}"
                    )
                    raise HistoricalBackfillError(msg)
            summary.products_processed += 1

        logger.info(
            "Auto-warm backfill finished: products=%d rows_upserted=%d days=%d",
            summary.products_processed,
            summary.rows_upserted,
            days,
        )
        return summary

    def _page_symbol(
        self,
        instrument: MarketDataInstrument,
        instr_id: int,
        start: datetime,
        end: datetime,
        summary: IngestionSummary,
        *,
        newest_first: bool = False,
        empty_windows: set[tuple[datetime, datetime]] | None = None,
    ) -> None:
        """Page [start, end) for one symbol in request-sized windows, upserting each."""
        if start >= end:
            return
        window = timedelta(
            minutes=_GRANULARITY_MINUTES[self._granularity] * _MAX_CANDLES_PER_REQUEST
        )
        symbol_rows = 0
        observed_empty_windows = empty_windows if empty_windows is not None else set()
        cursor = end if newest_first else start
        while (newest_first and cursor > start) or (not newest_first and cursor < end):
            window_start = max(start, cursor - window) if newest_first else cursor
            window_end = cursor if newest_first else min(cursor + window, end)
            upserted = self._backfill_window(
                instrument,
                instr_id,
                window_start,
                window_end,
            )
            if upserted == 0:
                observed_empty_windows.add((window_start, window_end))
                if (
                    self._is_continuous(instrument)
                    and len(observed_empty_windows) > _MAX_EMPTY_WINDOWS_PER_SYMBOL
                ):
                    msg = (
                        f"{self._source} returned too many empty windows for "
                        f"{instrument.canonical_symbol}: "
                        f"count={len(observed_empty_windows)} "
                        f"allowed={_MAX_EMPTY_WINDOWS_PER_SYMBOL}"
                    )
                    raise HistoricalBackfillError(msg)
            symbol_rows += upserted
            summary.rows_upserted += upserted
            cursor = window_start if newest_first else window_end
            if self._request_pause_sec:
                self._sleep(self._request_pause_sec)

        logger.info(
            "Backfill complete for %s: %d rows upserted",
            instrument.canonical_symbol,
            symbol_rows,
        )

    def _backfill_window(
        self,
        instrument: MarketDataInstrument,
        instr_id: int,
        start: datetime,
        end: datetime,
    ) -> int:
        """Fetch and upsert one window, retrying transient failures and empty results."""
        filtered_rows = self._request_window_rows(
            instrument,
            instr_id,
            start,
            end,
        )
        if not filtered_rows:
            return 0
        required = self._minimum_rows(start, end, coverage=self._minimum_window_coverage)
        if len(filtered_rows) < required:
            msg = (
                f"{self._source} candle coverage too sparse for "
                f"{instrument.canonical_symbol} window "
                f"{start.isoformat()}..{end.isoformat()}: rows={len(filtered_rows)} "
                f"required={required}"
            )
            raise HistoricalBackfillError(msg)
        upserted = self._ingestion_svc.upsert_candles(filtered_rows)
        if upserted != len(filtered_rows):
            # PostgreSQL counts both inserted rows and rows handled by this
            # statement's unconditional ON CONFLICT DO UPDATE branch. A
            # mismatch therefore means input was rejected by DB-layer candle
            # validation (or the driver violated its affected-row contract),
            # not a legitimate idempotent replay or venue outage.
            msg = (
                f"Validated candle upsert count mismatch for "
                f"{instrument.canonical_symbol} window "
                f"{start.isoformat()}..{end.isoformat()}: rows={upserted} "
                f"expected={len(filtered_rows)}"
            )
            raise HistoricalBackfillError(msg)
        return upserted

    def _fetch_rows_once(
        self,
        instrument: MarketDataInstrument,
        instr_id: int,
        start: datetime,
        end: datetime,
        *,
        attempt: int,
    ) -> list[CandleRow] | None:
        """One provider request; ``None`` after backing off on a retryable failure.

        Non-retryable failures — and retryable ones on the final attempt — fail
        closed with :class:`HistoricalBackfillError`.
        """
        try:
            return self._provider.fetch_candle_rows(
                product_id=instrument.product_id,
                instr_id=instr_id,
                start_time=start,
                end_time=end,
                granularity=self._granularity,
                broker_instrument_type=instrument.broker_instrument_type,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            retryable = (
                status
                in {
                    httpx.codes.REQUEST_TIMEOUT,
                    httpx.codes.TOO_MANY_REQUESTS,
                }
                or status >= httpx.codes.INTERNAL_SERVER_ERROR
            )
            if retryable and attempt < _MAX_WINDOW_ATTEMPTS:
                self._sleep(0.5 * 2**attempt)  # exponential backoff on rate limit
                return None
            msg = (
                f"{self._source} backfill request failed with HTTP {status} for "
                f"{instrument.canonical_symbol} "
                f"window {start.isoformat()}..{end.isoformat()} after {attempt} attempt(s)"
            )
            raise HistoricalBackfillError(msg) from exc
        except httpx.RequestError as exc:
            if attempt < _MAX_WINDOW_ATTEMPTS:
                self._sleep(0.5 * 2**attempt)
                return None
            msg = (
                f"{self._source} backfill network failure for "
                f"{instrument.canonical_symbol} window "
                f"{start.isoformat()}..{end.isoformat()} after {attempt} attempt(s)"
            )
            raise HistoricalBackfillError(msg) from exc
        except (TypeError, ValueError, RuntimeError) as exc:
            msg = (
                f"{self._source} historical provider contract failed for "
                f"{instrument.canonical_symbol} window "
                f"{start.isoformat()}..{end.isoformat()}"
            )
            raise HistoricalBackfillError(msg) from exc

    def _request_window_rows(
        self,
        instrument: MarketDataInstrument,
        instr_id: int,
        start: datetime,
        end: datetime,
    ) -> list[CandleRow]:
        """Return validated rows for one half-open window after bounded retries."""
        for attempt in range(1, _MAX_WINDOW_ATTEMPTS + 1):
            rows = self._fetch_rows_once(instrument, instr_id, start, end, attempt=attempt)
            if rows is None:
                continue

            filtered_rows = self._validated_window_rows(
                rows,
                instrument=instrument,
                instr_id=instr_id,
                start=start,
                end=end,
            )
            if not filtered_rows and not self._is_continuous(instrument):
                # A clean empty response is normal outside an official session.
                # Retrying every overnight/weekend window would add more than an
                # hour to a 150-day one-minute warmup. Transport/HTTP failures
                # above still receive bounded retries; clean scheduled gaps
                # defer immediately to the aggregate and seven-day tail gates.
                return self._handle_empty_window(
                    instrument,
                    start,
                    end,
                    attempt=attempt,
                )
            if not filtered_rows and attempt < _MAX_WINDOW_ATTEMPTS:
                # A 200/empty response is ambiguous: it may be a transient API
                # anomaly, a provider returning only an inclusive end candle, or
                # a genuine closed-session window. Retry before source-aware
                # coverage policy classifies it.
                self._sleep(0.5 * 2**attempt)
                continue
            if filtered_rows:
                return filtered_rows
            return self._handle_empty_window(
                instrument,
                start,
                end,
                attempt=attempt,
            )
        msg = "unreachable backfill retry state"
        raise AssertionError(msg)

    def _validated_window_rows(
        self,
        rows: list[CandleRow],
        *,
        instrument: MarketDataInstrument,
        instr_id: int,
        start: datetime,
        end: datetime,
    ) -> list[CandleRow]:
        """Validate provenance/identity and enforce the half-open UTC range."""
        start_naive = start.astimezone(UTC).replace(tzinfo=None)
        end_naive = end.astimezone(UTC).replace(tzinfo=None)
        rows_by_ts: dict[datetime, CandleRow] = {}
        for row in rows:
            if row.source != self._source:
                msg = (
                    "Historical provider source mismatch: configured "
                    f"{self._source!r}, row emits {row.source!r}"
                )
                raise HistoricalBackfillError(msg)
            if row.instr_id != instr_id:
                msg = (
                    "Historical provider instrument mismatch for "
                    f"{instrument.canonical_symbol}: row instr_id={row.instr_id}, "
                    f"expected={instr_id}"
                )
                raise HistoricalBackfillError(msg)
            if row.timeframe != self.timeframe:
                msg = (
                    "Historical provider timeframe mismatch for "
                    f"{instrument.canonical_symbol}: row timeframe={row.timeframe!r}, "
                    f"expected={self.timeframe!r}"
                )
                raise HistoricalBackfillError(msg)
            row_ts = row.ts
            normalized_row = row
            if row_ts.tzinfo is not None and row_ts.utcoffset() is not None:
                row_ts = row_ts.astimezone(UTC).replace(tzinfo=None)
                normalized_row = replace(row, ts=row_ts)
            if start_naive <= row_ts < end_naive:
                rows_by_ts[row_ts] = normalized_row
        return [rows_by_ts[ts] for ts in sorted(rows_by_ts)]

    def _handle_empty_window(
        self,
        instrument: MarketDataInstrument,
        start: datetime,
        end: datetime,
        *,
        attempt: int,
    ) -> list[CandleRow]:
        """Defer legitimate source gaps only when aggregate gates remain active."""
        if self._minimum_window_coverage == 0.0 and self._coverage_floor(instrument) > 0.0:
            logger.warning(
                "%s returned no in-window historical candles after %d attempts; "
                "deferring to source-aware total-coverage gates: instrument=%s "
                "window=%s..%s",
                self._source,
                attempt,
                instrument.canonical_symbol,
                start.isoformat(),
                end.isoformat(),
            )
            return []
        msg = (
            f"{self._source} returned no candles in requested window for "
            f"{instrument.canonical_symbol} window "
            f"{start.isoformat()}..{end.isoformat()} after {attempt} attempt(s)"
        )
        raise HistoricalBackfillError(msg)

    # ------------------------------------------------------------------
    # Scheduled-market daily policy. Deliberately distinct from the
    # continuous (crypto) paging path above: calendar-aware session coverage,
    # a single request per symbol, and corporate-action loading. The full
    # rationale lives in ``_scheduled_backfill.py``.
    # ------------------------------------------------------------------

    def _fetch_scheduled_daily_rows(
        self,
        instrument: MarketDataInstrument,
        instr_id: int,
        start: datetime,
        end: datetime,
    ) -> list[CandleRow]:
        """Single-request daily fetch with provenance validation.

        Transient transport failures get the same bounded retries as the
        paging path; a clean empty response is returned as-is for the policy
        gate to fail loud (never retried — outside a session it stays empty).
        """
        for attempt in range(1, _MAX_WINDOW_ATTEMPTS + 1):
            rows = self._fetch_rows_once(instrument, instr_id, start, end, attempt=attempt)
            if rows is None:
                continue
            return self._validated_window_rows(
                rows,
                instrument=instrument,
                instr_id=instr_id,
                start=start,
                end=end,
            )
        msg = "unreachable scheduled backfill retry state"
        raise AssertionError(msg)

    def _backfill_scheduled_daily(
        self,
        instrument: MarketDataInstrument,
        instr_id: int,
        start: datetime,
        end: datetime,
        summary: IngestionSummary,
    ) -> None:
        """Fetch + upsert daily bars for one scheduled instrument over [start, end).

        A symbol whose data begins after ``start`` or ends before ``end``
        (listing/delisting) is accepted with a NARROWED span — coverage is then
        judged on the span the vendor served, and the caller must cross-check
        the narrowed span against ``index_membership``. Coverage below
        ``SCHEDULED_DAILY_MIN_COVERAGE`` of the expected trading sessions
        fails loud.
        """
        symbol = instrument.canonical_symbol
        requested_first = start.astimezone(UTC).date()
        requested_last = self._last_session_date(end)
        rows = self._fetch_scheduled_daily_rows(instrument, instr_id, start, end)
        if not rows:
            msg = (
                f"{symbol}: provider returned no daily bars in {requested_first}..{requested_last}"
            )
            raise HistoricalBackfillError(msg)
        upserted = self._ingestion_svc.upsert_candles(rows)
        summary.rows_upserted += upserted
        if upserted < len(rows):
            logger.warning(
                "%s: %d of %d fetched bars rejected by OHLC invariants",
                symbol,
                len(rows) - upserted,
                len(rows),
            )
        symbol_summary = compute_scheduled_daily_summary(
            symbol=symbol,
            instr_id=instr_id,
            rows=rows,
            rows_upserted=upserted,
            requested_first=requested_first,
            requested_last=requested_last,
            calendar_mic=self._calendar_mic,
        )
        if not symbol_summary.meets_coverage:
            msg = (
                f"{symbol}: coverage {upserted}/{symbol_summary.expected_sessions} sessions in "
                f"{symbol_summary.first_session}..{symbol_summary.last_session} is below the "
                f"{SCHEDULED_DAILY_MIN_COVERAGE:.0%} gate"
            )
            raise HistoricalBackfillError(msg)
        if symbol_summary.narrowed:
            logger.warning(
                "%s: span narrowed to %s..%s (listing/delisting?) — "
                "cross-check against index_membership",
                symbol,
                symbol_summary.first_session.isoformat(),
                symbol_summary.last_session.isoformat(),
            )
        self._scheduled_summaries.append(symbol_summary)

    def _scheduled_daily_history_is_current(
        self,
        instr_id: int,
        start: datetime,
        end: datetime,
        *,
        instrument: MarketDataInstrument,
    ) -> bool:
        """Calendar-aware deficit check for :meth:`ensure_history` skips.

        A late-listed (narrowed) symbol never satisfies the full-window session
        count and is simply re-fetched — one idempotent request — where the
        policy gate then judges the vendor-served span.
        """
        expected = expected_scheduled_sessions(
            self._calendar_mic,
            start.astimezone(UTC).date(),
            self._last_session_date(end),
        )
        if expected == 0:
            return False
        actual = self._ingestion_svc.candle_count(
            instr_id,
            source=self._source,
            timeframe=self.timeframe,
            start=start,
            end=end,
        )
        if actual / expected < SCHEDULED_DAILY_MIN_COVERAGE:
            return False
        return self._has_recent_tail(instr_id, end, instrument=instrument)

    def load_corporate_actions(self, *, start: date, end: date) -> dict[str, int]:
        """Upsert splits/dividends for scheduled instruments; returns insert counts.

        Idempotent on the ``(instr_id, action_type, ex_date, source)`` unique
        key via a portable select-then-insert (volumes here are tiny). Fails
        loud when no corporate-action source is available — never silently
        skipped once requested.
        """
        if self._corporate_actions is None:
            msg = (
                "load_corporate_actions requires a corporate-action source "
                f"(the {self._source} provider does not expose splits/dividends)"
            )
            raise HistoricalBackfillError(msg)
        scheduled = [
            instrument for instrument in self._instruments if not self._is_continuous(instrument)
        ]
        if not scheduled:
            return {}
        instrument_ids = self._load_instrument_ids()
        inserted_by_symbol: dict[str, int] = {}
        for instrument in scheduled:
            symbol = instrument.canonical_symbol
            inserted_by_symbol[symbol] = upsert_corporate_actions(
                session_factory=self._session_factory,
                source=self._corporate_actions.source,
                instr_id=instrument_ids[normalize_product_symbol(symbol)],
                splits=self._corporate_actions.fetch_splits(
                    product_id=instrument.product_id, start=start, end=end
                ),
                dividends=self._corporate_actions.fetch_dividends(
                    product_id=instrument.product_id, start=start, end=end
                ),
            )
        return inserted_by_symbol


def main(argv: list[str] | None = None) -> int:
    from lib_application.db.session import (  # noqa: PLC0415
        create_engine_for_env,
        dispose_engine,
        get_session_factory,
    )
    from lib_common.env_utils import build_database_url  # noqa: PLC0415
    from lib_common.logging import setup_logging  # noqa: PLC0415

    from .main import (  # noqa: PLC0415
        _get_granularity,
        _get_ingestion_instruments,
        _get_provider_credentials,
        _get_source,
        _get_symbols,
    )

    setup_logging()
    parser = argparse.ArgumentParser(description="Backfill historical candles into prices.")
    parser.add_argument("--days", type=int, default=None, help="Relative days of history.")
    parser.add_argument("--start", default=None, help="Exact inclusive UTC start (ISO-8601).")
    parser.add_argument("--end", default=None, help="Exact exclusive UTC end (ISO-8601).")
    parser.add_argument("--symbols", default=None, help="Comma-separated override (default: env).")
    parser.add_argument("--granularity", default=None, help="Canonical granularity override.")
    parser.add_argument(
        "--source",
        default=None,
        help="Canonical source from the live market-data provider registry.",
    )
    args = parser.parse_args(argv)

    if bool(args.start) != bool(args.end):
        parser.error("--start and --end must be provided together")
    if args.start and args.days is not None:
        parser.error("--days cannot be combined with --start/--end")

    def _parse_boundary(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            parser.error(f"Invalid ISO-8601 boundary: {value}")
            raise AssertionError from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parser.error(f"UTC boundary must be timezone-aware: {value}")
        return parsed.astimezone(UTC)

    exact_start = _parse_boundary(args.start) if args.start else None
    exact_end = _parse_boundary(args.end) if args.end else None
    symbols = [s for s in args.symbols.split(",") if s.strip()] if args.symbols else _get_symbols()
    source = args.source or _get_source()
    engine = create_engine_for_env(db_url=build_database_url())
    session_factory = get_session_factory(engine=engine)
    backfiller = HistoricalBackfiller(
        session_factory=session_factory,
        instruments=_get_ingestion_instruments(
            session_factory,
            source,
            symbols=symbols,
        ),
        granularity=args.granularity or _get_granularity(),
        source=source,
        **_get_provider_credentials(source),
    )
    try:
        if exact_start is not None and exact_end is not None:
            backfiller.backfill_range(
                start=exact_start,
                end=exact_end,
            )
            action_start = exact_start.date()
            action_end = (exact_end - timedelta(microseconds=1)).date()
        else:
            days = args.days if args.days is not None else 150
            backfiller.backfill(days=days)
            action_end = datetime.now(tz=UTC).date()
            action_start = action_end - timedelta(days=days)
        # Same one-shot surface serves scheduled-market reference data: load
        # splits/dividends over the priced window when the provider exposes them.
        if backfiller.corporate_actions_supported:
            inserted = backfiller.load_corporate_actions(start=action_start, end=action_end)
            logger.info("Corporate actions loaded: %s", inserted)
    finally:
        backfiller.close()
        dispose_engine(engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
