"""Polling scheduler for periodic canonical candle ingestion.

After each committed batch, issues ``NOTIFY new_market_data`` carrying
symbol, timeframe, source, and latest timestamp so downstream consumers
(e.g. signal_worker) can react without polling the prices table.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from lib_application.services.price_ingestion_service import PriceIngestionService
from lib_common.logging import get_logger
from lib_common.metrics import counter, gauge
from lib_data.market_data import CandleRow, MarketDataInstrument, normalize_product_symbol
from lib_infrastructure.market_data.eodhd_client import (
    EODHDClient,
    EODHDErrorKind,
    EODHDMarketDataError,
)
from lib_infrastructure.market_data.eodhd_delayed_quote_store import (
    EODHDDelayedQuoteIngestion,
    EODHDDelayedQuotePersistenceError,
)
from lib_infrastructure.market_data.models import (
    GRANULARITY_TO_SECONDS,
    GRANULARITY_TO_TIMEFRAME,
    IngestionSummary,
)
from lib_infrastructure.market_data.providers import get_market_data_provider

logger = get_logger(__name__)

SessionFactory = Callable[[], Any]


# Freshness/health is otherwise invisible: a stalled feed leaves a static
# {"process": True} probe green while downstream strategies starve. These
# expose ingest progress so /ready and alerting can see it.
_CANDLES_RECEIVED = counter(
    "vm_market_data_candles_received_total",
    "Candles fetched from the market-data source",
    ("symbol", "timeframe"),
)
_ROWS_UPSERTED = counter(
    "vm_market_data_rows_upserted_total",
    "Price rows upserted into the prices table",
    ("symbol", "timeframe"),
)
_CYCLE_FAILURES = counter(
    "vm_market_data_cycle_failures_total",
    "Ingest cycles that failed, by reason",
    ("reason",),
)
_EMPTY_FETCHES = counter(
    "vm_market_data_empty_fetches_total",
    "Fetches that returned 0 closed candles for a symbol (L3 observability; a "
    "sustained run for one symbol while others advance flags a stalled source). "
    "An all-empty cycle is normal between candle closes — stall detection stays "
    "with the freshness gauge / readiness probe, not a per-cycle WARN.",
    ("symbol", "timeframe"),
)
_LAST_INGEST_TS = gauge(
    "vm_market_data_last_ingest_timestamp_seconds",
    "Unix timestamp of the most recent ingested candle, per symbol",
    ("symbol", "timeframe"),
)
_LAST_SUCCESS_TS = gauge(
    "vm_market_data_last_cycle_success_timestamp_seconds",
    "Unix timestamp of the last ingest cycle that completed without error",
)
_DELAYED_QUOTES_STORED = counter(
    "vm_eodhd_delayed_quotes_stored_total",
    "Owner-scoped EODHD delayed quote observations persisted",
)
_DELAYED_LAST_SUCCESS_TS = gauge(
    "vm_eodhd_delayed_quote_last_success_timestamp_seconds",
    "Unix timestamp of the last complete owner-scoped delayed quote batch",
)
_EODHD_DAILY_QUOTA_EXHAUSTIONS = counter(
    "vm_eodhd_daily_quota_exhaustions_total",
    "EODHD HTTP 402 daily-quota responses that opened the ingestion circuit",
)
_EODHD_DAILY_QUOTA_CIRCUIT_OPEN = gauge(
    "vm_eodhd_daily_quota_circuit_open",
    "Whether EODHD acquisition is paused until its bounded daily-quota retry time",
)

# Readiness defaults: stale once nothing has arrived for 2x the poll interval,
# floored at 2 minutes so a slow poll does not flap the probe.
_MIN_STALENESS_THRESHOLD_SEC = 120
_CANDLE_CLOSE_GUARD_SEC = 2
_MAX_CANDLES_PER_FETCH = 350
EODHD_DAILY_QUOTA_MIN_COOLDOWN_DEFAULT_SEC = 900
EODHD_DAILY_QUOTA_MIN_COOLDOWN_MIN_SEC = 60
EODHD_DAILY_QUOTA_MIN_COOLDOWN_MAX_SEC = 86400


def _metric_inc(metric: Any, amount: float = 1.0, **labels: str) -> None:
    if metric is None:  # prometheus-client absent
        return
    (metric.labels(**labels) if labels else metric).inc(amount)


def _metric_set(metric: Any, value: float, **labels: str) -> None:
    if metric is None:  # prometheus-client absent
        return
    (metric.labels(**labels) if labels else metric).set(value)


class IngestionScheduler:
    """Periodic venue ingestion with Postgres NOTIFY after each batch.

    This class owns only orchestration and health; provider/DB logic stays in
    shared libraries. Canonical symbols and venue product ids are distinct so
    opaque UICs/conids/tokens never become downstream identities.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        instruments: list[MarketDataInstrument],
        granularity: str = "ONE_MINUTE",
        source: str = "coinbase_live",
        poll_interval_sec: int = 60,
        candle_poll_interval_sec: int | None = None,
        startup_backfill_minutes: int = 180,
        api_key: str | None = None,
        api_secret: str | None = None,
        access_token: str | None = None,
        access_token_expires_at: str | None = None,
        account_key: str | None = None,
        notify_channel: str = "new_market_data",
        staleness_threshold_sec: int | None = None,
        delayed_quote_owner_user_id: str | None = None,
        delayed_quote_ingestion: EODHDDelayedQuoteIngestion | None = None,
        eodhd_daily_quota_min_cooldown_sec: int = (EODHD_DAILY_QUOTA_MIN_COOLDOWN_DEFAULT_SEC),
        quota_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._instruments = list(instruments)
        if not self._instruments:
            msg = "IngestionScheduler requires at least one instrument"
            raise ValueError(msg)
        normalized_symbols = [
            normalize_product_symbol(instrument.canonical_symbol)
            for instrument in self._instruments
        ]
        if len(set(normalized_symbols)) != len(normalized_symbols):
            msg = "IngestionScheduler canonical symbols must be unique"
            raise ValueError(msg)
        self._granularity = granularity.strip().upper()
        self._source = source.strip()
        self._poll_interval_sec = max(1, poll_interval_sec)
        resolved_candle_poll_interval = (
            self._poll_interval_sec
            if candle_poll_interval_sec is None
            else candle_poll_interval_sec
        )
        if (
            isinstance(resolved_candle_poll_interval, bool)
            or not isinstance(resolved_candle_poll_interval, int)
            or resolved_candle_poll_interval < self._poll_interval_sec
        ):
            msg = "candle_poll_interval_sec must be an integer at least poll_interval_sec"
            raise ValueError(msg)
        self._candle_poll_interval_sec = resolved_candle_poll_interval
        self._last_candle_cycle_success_at: datetime | None = None
        self._startup_backfill_minutes = max(1, startup_backfill_minutes)
        self._notify_channel = notify_channel
        self._stop_event = threading.Event()

        if self._granularity not in GRANULARITY_TO_TIMEFRAME:
            msg = f"Unsupported granularity: {self._granularity}"
            raise ValueError(msg)

        self._period_seconds = GRANULARITY_TO_SECONDS[self._granularity]
        # Closed-bar-only ingestion (MD-1) lags by up to one period, so the
        # freshness window must allow 2 polls + the candle period before a feed
        # is considered stale, otherwise /ready flaps near each boundary.
        self._staleness_threshold_sec = staleness_threshold_sec or max(
            _MIN_STALENESS_THRESHOLD_SEC,
            2 * self._poll_interval_sec + self._period_seconds,
        )
        # Per-(symbol, timeframe) timestamp of the most recent ingested candle,
        # read by the health thread for the freshness probe.
        self._last_ingest_ts: dict[tuple[str, str], datetime] = {}
        self._freshness_lock = threading.Lock()
        if not (
            EODHD_DAILY_QUOTA_MIN_COOLDOWN_MIN_SEC
            <= eodhd_daily_quota_min_cooldown_sec
            <= EODHD_DAILY_QUOTA_MIN_COOLDOWN_MAX_SEC
        ):
            msg = (
                "EODHD daily-quota minimum cooldown must be between "
                f"{EODHD_DAILY_QUOTA_MIN_COOLDOWN_MIN_SEC} and "
                f"{EODHD_DAILY_QUOTA_MIN_COOLDOWN_MAX_SEC} seconds"
            )
            raise ValueError(msg)
        self._eodhd_daily_quota_min_cooldown_sec = eodhd_daily_quota_min_cooldown_sec
        self._quota_clock = quota_clock or (lambda: datetime.now(tz=UTC))
        self._eodhd_daily_quota_retry_at: datetime | None = None
        if self._source == "eodhd":
            _metric_set(_EODHD_DAILY_QUOTA_CIRCUIT_OPEN, 0)

        # Resolve the candle provider by source (coinbase_live / deribit / ...)
        # so a new venue is an adapter + registry entry, not a scheduler rewrite.
        self._provider = get_market_data_provider(
            self._source,
            api_key=api_key,
            api_secret=api_secret,
            access_token=access_token,
            access_token_expires_at=access_token_expires_at,
            account_key=account_key,
        )
        self._session_factory = session_factory
        self._ingestion_svc = PriceIngestionService(session_factory)
        owner = str(delayed_quote_owner_user_id or "").strip()
        if delayed_quote_ingestion is not None and not owner:
            msg = "Delayed quote ingestion requires an exact entitlement owner"
            raise ValueError(msg)
        if owner:
            if self._source != "eodhd" or any(
                instrument.asset_class != "equity" for instrument in self._instruments
            ):
                msg = "Owner-scoped EODHD delayed quotes require the EODHD equity scheduler"
                raise ValueError(msg)
            if delayed_quote_ingestion is None:
                if not isinstance(self._provider, EODHDClient):
                    msg = "EODHD delayed quote ingestion requires the canonical EODHD client"
                    raise TypeError(msg)
                delayed_quote_ingestion = EODHDDelayedQuoteIngestion(
                    client=self._provider,
                    session_factory=session_factory,
                )
        self._delayed_quote_owner_user_id = owner or None
        self._delayed_quote_ingestion = delayed_quote_ingestion
        self._active_instruments = list(self._instruments)
        self._last_delayed_quote_success_at: datetime | None = None
        self._delayed_staleness_threshold_sec = max(
            _MIN_STALENESS_THRESHOLD_SEC,
            2 * self._poll_interval_sec,
        )

    @property
    def timeframe(self) -> str:
        return str(GRANULARITY_TO_TIMEFRAME[self._granularity])

    def run_forever(self) -> None:
        """Run startup backfill then poll indefinitely.

        A single failed cycle (provider auth/rate-limit/5xx, network blip, or a
        transient DB error) must not take the whole feed down — it is logged and
        the loop keeps polling, retrying from the current watermark on the next
        tick instead of crashing the process into a restart loop.
        """
        self._run_cycle_safely(lookback_minutes=self._startup_backfill_minutes)
        while not self._stop_event.wait(self._poll_interval_sec):
            self._run_cycle_safely(lookback_minutes=max(2, self._poll_interval_sec // 60 + 1))

    def _run_cycle_safely(self, *, lookback_minutes: int) -> None:
        """Run one ingest cycle, swallowing transient market-data/DB errors."""
        if self._eodhd_daily_quota_circuit_is_open():
            return
        try:
            self._ingest_cycle(lookback_minutes=lookback_minutes)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                _metric_inc(_CYCLE_FAILURES, reason="auth")
                logger.warning(
                    "Market-data source %r rejected the request (HTTP %s) — check "
                    "its credentials; retrying next poll",
                    self._source,
                    status,
                )
            else:
                _metric_inc(_CYCLE_FAILURES, reason="http")
                logger.warning(
                    "Market-data source %r returned HTTP %s; retrying next poll",
                    self._source,
                    status,
                    exc_info=True,
                )
        except httpx.RequestError:
            _metric_inc(_CYCLE_FAILURES, reason="network")
            logger.warning(
                "Market-data source %r request failed (network/timeout); retrying next poll",
                self._source,
                exc_info=True,
            )
        except SQLAlchemyError:
            _metric_inc(_CYCLE_FAILURES, reason="db")
            logger.warning(
                "Database error during ingestion cycle; retrying next poll",
                exc_info=True,
            )
        except EODHDMarketDataError as exc:
            if exc.kind is EODHDErrorKind.DAILY_QUOTA:
                self._open_eodhd_daily_quota_circuit(exc)
                return
            _metric_inc(_CYCLE_FAILURES, reason=f"eodhd_{exc.kind.value}")
            logger.warning(
                "EODHD ingestion cycle failed; retrying next poll",
                error_kind=exc.kind.value,
                status_code=exc.status_code,
            )
        except EODHDDelayedQuotePersistenceError:
            _metric_inc(_CYCLE_FAILURES, reason="delayed_quote")
            logger.warning(
                "Owner-scoped delayed quote cycle failed; retrying next poll",
                exc_info=True,
            )

    def _quota_now(self) -> datetime:
        now = self._quota_clock()
        if now.tzinfo is None or now.utcoffset() is None:
            msg = "EODHD daily-quota clock must return a timezone-aware datetime"
            raise ValueError(msg)
        return now.astimezone(UTC)

    def _open_eodhd_daily_quota_circuit(self, exc: EODHDMarketDataError) -> None:
        now = self._quota_now()
        minimum_retry_at = now + timedelta(seconds=self._eodhd_daily_quota_min_cooldown_sec)
        provider_retry_at = exc.retry_at or minimum_retry_at
        maximum_retry_at = now + timedelta(seconds=EODHD_DAILY_QUOTA_MIN_COOLDOWN_MAX_SEC)
        retry_at = min(max(minimum_retry_at, provider_retry_at), maximum_retry_at)
        with self._freshness_lock:
            self._eodhd_daily_quota_retry_at = retry_at
        _metric_inc(_CYCLE_FAILURES, reason="eodhd_daily_quota")
        _metric_inc(_EODHD_DAILY_QUOTA_EXHAUSTIONS)
        _metric_set(_EODHD_DAILY_QUOTA_CIRCUIT_OPEN, 1)
        logger.warning(
            "EODHD daily quota exhausted; acquisition circuit opened",
            status_code=exc.status_code,
            retry_at=retry_at.isoformat(),
        )

    def _eodhd_daily_quota_circuit_is_open(self) -> bool:
        if self._source != "eodhd":
            return False
        now = self._quota_now()
        with self._freshness_lock:
            retry_at = self._eodhd_daily_quota_retry_at
            if retry_at is None or now >= retry_at:
                was_open = retry_at is not None
                self._eodhd_daily_quota_retry_at = None
            else:
                return True
        if was_open:
            _metric_set(_EODHD_DAILY_QUOTA_CIRCUIT_OPEN, 0)
            logger.info("EODHD daily-quota circuit closed; acquisition may resume")
        return False

    def is_provider_ready(self) -> bool:
        """Whether provider acquisition is available to the polling loop."""

        return not self._eodhd_daily_quota_circuit_is_open()

    def freshness_snapshot(self) -> dict[str, float]:
        """Return per-``symbol:timeframe`` seconds since the last ingested candle."""
        now = datetime.now(tz=UTC)
        with self._freshness_lock:
            items = list(self._last_ingest_ts.items())
            delayed_success = self._last_delayed_quote_success_at
        snapshot = {f"{sym}:{tf}": (now - ts).total_seconds() for (sym, tf), ts in items}
        if delayed_success is not None:
            snapshot["eodhd_delayed_quotes:batch"] = (now - delayed_success).total_seconds()
        return snapshot

    def is_fresh(self) -> bool:
        """Whether every configured feed is within the staleness threshold.

        Missing symbols are not present in ``_last_ingest_ts`` at all, so checking
        only existing values lets one healthy product conceal a never-started
        configured feed. Readiness is an all-symbol contract.
        """
        if not self.is_provider_ready():
            return False
        expected = {
            (normalize_product_symbol(instrument.canonical_symbol), self.timeframe)
            for instrument in self._active_instruments
        }
        with self._freshness_lock:
            observed = dict(self._last_ingest_ts)
            delayed_success = self._last_delayed_quote_success_at
        if not expected or not expected.issubset(observed):
            return False
        now = datetime.now(tz=UTC)
        candles_fresh = all(
            (now - observed[key]).total_seconds() <= self._staleness_threshold_sec
            for key in expected
        )
        if not candles_fresh:
            return False
        if self._delayed_quote_ingestion is None:
            return True
        return (
            delayed_success is not None
            and (now - delayed_success).total_seconds() <= self._delayed_staleness_threshold_sec
        )

    def _closed_candles(self, rows: list[CandleRow], now: datetime) -> list[CandleRow]:
        """Drop the still-forming candle so strategies never act on a partial bar
        that mutates on the next poll (MD-1). A candle starting at ``ts`` is final
        once ``ts + period <= now``.
        """
        cutoff = now.replace(tzinfo=None) - timedelta(
            seconds=self._period_seconds + _CANDLE_CLOSE_GUARD_SEC
        )
        closed: list[CandleRow] = []
        for row in rows:
            ts = row.ts
            ts_naive = ts if ts.tzinfo is None else ts.astimezone(UTC).replace(tzinfo=None)
            if ts_naive <= cutoff:
                closed.append(row)
        return closed

    def _record_freshness(self, symbol: str, timeframe: str, latest_ts: datetime) -> None:
        ts = latest_ts if latest_ts.tzinfo is not None else latest_ts.replace(tzinfo=UTC)
        with self._freshness_lock:
            self._last_ingest_ts[(symbol, timeframe)] = ts
        _metric_set(_LAST_INGEST_TS, ts.timestamp(), symbol=symbol, timeframe=timeframe)

    def _validate_provider_rows(
        self,
        rows: list[CandleRow],
        *,
        instr_id: int,
        product_id: str,
    ) -> None:
        """Enforce the configured identity/provenance boundary before persistence."""

        for row in rows:
            if (
                row.instr_id != instr_id
                or row.source != self._source
                or row.timeframe != self.timeframe
            ):
                msg = (
                    "Market-data provider returned a row outside its configured "
                    f"identity boundary: product_id={product_id!r}, "
                    f"instr_id={row.instr_id!r}, source={row.source!r}, "
                    f"timeframe={row.timeframe!r}"
                )
                raise ValueError(msg)

    def stop(self) -> None:
        self._stop_event.set()
        self._provider.close()

    def _ingest_cycle(self, lookback_minutes: int) -> IngestionSummary:
        now = datetime.now(tz=UTC)
        default_start = now - timedelta(minutes=max(1, lookback_minutes))
        instrument_maps: dict[str, dict[str, int]] = {}
        summary = IngestionSummary()
        empty_products = 0
        candle_cycle_due = (
            self._last_candle_cycle_success_at is None
            or (now - self._last_candle_cycle_success_at).total_seconds()
            >= self._candle_poll_interval_sec
        )
        cycle_instruments = list(self._instruments)
        with self._freshness_lock:
            self._active_instruments = list(cycle_instruments)
        for instrument in cycle_instruments:
            product_id = instrument.product_id
            canonical = normalize_product_symbol(instrument.canonical_symbol)
            if instrument.asset_class not in instrument_maps:
                instrument_maps[instrument.asset_class] = self._ingestion_svc.load_instrument_map(
                    asset_class=instrument.asset_class,
                )
            instrument_map = instrument_maps[instrument.asset_class]
            instr_id = instrument_map.get(canonical)
            if instr_id is None:
                msg = (
                    "Configured canonical market-data instrument is unknown: "
                    f"{instrument.canonical_symbol!r} "
                    f"(asset_class={instrument.asset_class!r})"
                )
                raise ValueError(msg)
            if not candle_cycle_due:
                continue

            latest = self._ingestion_svc.latest_candle_ts(
                instr_id,
                source=self._source,
                timeframe=self.timeframe,
            )
            if latest is not None and latest.tzinfo is None:
                latest = latest.replace(tzinfo=UTC)
            start = min(
                default_start,
                latest - timedelta(seconds=self._period_seconds) if latest is not None else now,
            )
            # Keep requests within provider limits while recovering oldest-first.
            # Subsequent cycles resume from the newly committed latest candle until
            # the feed catches up, instead of silently abandoning a long outage.
            max_end = start + timedelta(seconds=self._period_seconds * _MAX_CANDLES_PER_FETCH)
            fetch_end = min(now, max_end)

            rows = self._provider.fetch_candle_rows(
                product_id=product_id,
                instr_id=instr_id,
                start_time=start,
                end_time=fetch_end,
                granularity=self._granularity,
                broker_instrument_type=instrument.broker_instrument_type,
            )
            self._validate_provider_rows(
                rows,
                instr_id=instr_id,
                product_id=product_id,
            )
            rows = self._closed_candles(rows, now)
            if not rows:
                # 200-but-no-closed-candles: make it countable instead of a silent
                # `continue` so a stalled source is distinguishable from a healthy
                # cycle (L3). Common between 1m candle closes, so DEBUG not WARN.
                empty_products += 1
                _metric_inc(_EMPTY_FETCHES, 1, symbol=canonical, timeframe=self.timeframe)
                logger.debug(
                    "No closed candles this cycle",
                    symbol=canonical,
                    timeframe=self.timeframe,
                )
                continue

            upserted = self._ingestion_svc.upsert_candles(rows)
            summary.products_processed += 1
            summary.candles_received += len(rows)
            summary.rows_upserted += upserted
            timeframe = self.timeframe
            _metric_inc(_CANDLES_RECEIVED, len(rows), symbol=canonical, timeframe=timeframe)
            _metric_inc(_ROWS_UPSERTED, upserted, symbol=canonical, timeframe=timeframe)

            # Issue NOTIFY after each symbol batch so consumers react quickly
            if upserted > 0:
                latest_ts = max(r.ts for r in rows)
                self._record_freshness(canonical, timeframe, latest_ts)
                self._pg_notify(
                    symbol=canonical,
                    timeframe=timeframe,
                    source=self._source,
                    latest_ts=latest_ts,
                )

        if candle_cycle_due:
            self._last_candle_cycle_success_at = now

        self._ingest_delayed_quotes(
            instrument_maps,
            instruments=cycle_instruments,
        )

        _metric_set(_LAST_SUCCESS_TS, now.timestamp())
        logger.info(
            "Ingestion cycle complete",
            products_processed=summary.products_processed,
            candles_received=summary.candles_received,
            rows_upserted=summary.rows_upserted,
            empty_products=empty_products,
            candle_cycle_due=candle_cycle_due,
            candle_poll_interval_sec=self._candle_poll_interval_sec,
        )
        return summary

    def _ingest_delayed_quotes(
        self,
        instrument_maps: dict[str, dict[str, int]],
        *,
        instruments: list[MarketDataInstrument] | None = None,
    ) -> None:
        ingestion = self._delayed_quote_ingestion
        owner = self._delayed_quote_owner_user_id
        if ingestion is None or owner is None:
            return
        instruments = list(self._instruments if instruments is None else instruments)
        if not instruments:
            completed_at = datetime.now(tz=UTC)
            with self._freshness_lock:
                self._last_delayed_quote_success_at = completed_at
            _metric_set(_DELAYED_LAST_SUCCESS_TS, completed_at.timestamp())
            return
        equity_map = instrument_maps.get("equity")
        if equity_map is None:
            msg = "EODHD delayed quote cycle has no resolved equity catalogue"
            raise EODHDDelayedQuotePersistenceError(msg)
        product_ids: list[str] = []
        instrument_ids: dict[str, int] = {}
        for instrument in instruments:
            canonical = normalize_product_symbol(instrument.canonical_symbol)
            instrument_id = equity_map.get(canonical)
            if instrument_id is None:
                msg = f"EODHD delayed quote instrument is unresolved: {canonical}"
                raise EODHDDelayedQuotePersistenceError(msg)
            product_id = instrument.product_id.strip().upper()
            source_symbol = product_id if "." in product_id else f"{product_id}.US"
            if source_symbol in instrument_ids:
                msg = f"EODHD delayed quote source symbol is duplicated: {source_symbol}"
                raise EODHDDelayedQuotePersistenceError(msg)
            product_ids.append(source_symbol)
            instrument_ids[source_symbol] = instrument_id
        stored = ingestion.acquire_and_persist(
            product_ids=product_ids,
            entitlement_owner_user_id=owner,
            instrument_ids=instrument_ids,
        )
        completed_at = datetime.now(tz=UTC)
        with self._freshness_lock:
            self._last_delayed_quote_success_at = completed_at
        _metric_inc(_DELAYED_QUOTES_STORED, len(stored))
        _metric_set(_DELAYED_LAST_SUCCESS_TS, completed_at.timestamp())
        logger.info(
            "Owner-scoped delayed quote batch persisted",
            owner_user_id=owner,
            quote_count=len(stored),
        )

    def _pg_notify(
        self,
        *,
        symbol: str,
        timeframe: str,
        source: str,
        latest_ts: datetime,
    ) -> None:
        """Issue PostgreSQL NOTIFY new_market_data with JSON payload."""
        payload: dict[str, str] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "source": source,
            "latest_ts": latest_ts.isoformat(),
        }
        try:
            with self._session_factory() as session:
                # Use raw SQL for NOTIFY (not supported by ORM)
                session.execute(
                    __import__("sqlalchemy").text(f"NOTIFY {self._notify_channel}, :payload"),
                    {"payload": json.dumps(payload)},
                )
                session.commit()
        except Exception:
            logger.warning(
                "Failed to issue NOTIFY %s",
                self._notify_channel,
                exc_info=True,
            )
