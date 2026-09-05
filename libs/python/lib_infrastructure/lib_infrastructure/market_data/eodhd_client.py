"""EODHD end-of-day candles and US extended delayed-quote client.

Daily bars only: EODHD supplies raw (unadjusted) OHLC but documents ``volume``
as split-adjusted. The historical-validation boundary retains that source
semantic and never relabels it as exact raw as-traded shares. Liquidity and
capacity require prices in a separately pinned matching split basis. Price
adjustments remain in-house from ``corporate_actions`` and the
vendor's ``adjusted_close`` field is deliberately ignored. Bar timestamps are
the exchange session close resolved through ``lib_data.sessions``, which is
what the daily-equity session-timing guard demands.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

import httpx

from lib_common.hashing import canonical_json_hash
from lib_common.logging import get_logger
from lib_data.market_data import CandleRow
from lib_data.sessions import is_trading_day, session_close_utc
from lib_infrastructure.market_data.ports import IMarketDataProvider

logger = get_logger(__name__)

_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_SEC = 0.5
_RETRY_MAX_DELAY_SEC = 8.0
_RETRY_JITTER_RATIO = 0.1
_MAX_INLINE_RETRY_AFTER_SEC = 30.0
_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_MIN_ERROR_STATUS = 400
_DAILY_GRANULARITY = "ONE_DAY"
_DAILY_TIMEFRAME = "1d"
_MIN_CURRENCY_LENGTH = 3
_MAX_CURRENCY_LENGTH = 10
_EODHD_REQUESTS_PER_MINUTE = 1_000
_RATE_LIMIT_WINDOW_SEC = 60.0
_RATE_LIMIT_HEADROOM_RATIO = 0.9
_RATE_LIMIT_HEADER = "X-RateLimit-Limit"
_RATE_LIMIT_REMAINING_HEADER = "X-RateLimit-Remaining"


class EODHDErrorKind(str, Enum):
    """Credential-safe classification for EODHD request failures."""

    AUTHENTICATION = "authentication"
    DAILY_QUOTA = "daily_quota"
    ENTITLEMENT = "entitlement"
    RATE_LIMIT = "rate_limit"
    TRANSIENT_HTTP = "transient_http"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    UNKNOWN = "unknown"


class EODHDMarketDataError(RuntimeError):
    """Raised on EODHD transport, status, or malformed-payload failures.

    Messages never carry the request URL or query params, so the API token
    cannot leak into logs or tracebacks.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: EODHDErrorKind = EODHDErrorKind.UNKNOWN,
        status_code: int | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        if not isinstance(kind, EODHDErrorKind):
            msg = "EODHD error kind must be an EODHDErrorKind"
            raise TypeError(msg)
        if retry_at is not None and (retry_at.tzinfo is None or retry_at.utcoffset() is None):
            msg = "EODHD retry_at must be timezone-aware"
            raise ValueError(msg)
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retry_at = retry_at.astimezone(UTC) if retry_at is not None else None


class EODHDMalformedRowPolicy(str, Enum):
    """Behavior when an EOD daily-bar row cannot be used safely."""

    SKIP = "skip"
    RAISE = "raise"


@dataclass(frozen=True)
class SplitRecord:
    """One split event parsed from ``/api/splits`` (ratio = new/old, e.g. 4)."""

    ex_date: date
    ratio: Decimal


@dataclass(frozen=True)
class DividendRecord:
    """One cash dividend parsed from ``/api/div`` (``date`` field = ex-date)."""

    ex_date: date
    amount: Decimal
    currency: str | None


@dataclass(frozen=True, slots=True)
class EODHDDelayedQuote:
    """One exact US extended delayed-quote record.

    EODHD names this product "Live v2", but the REST endpoint is delayed.  The
    model deliberately preserves the provider's distinct trade, bid, ask, and
    snapshot timestamps instead of presenting retrieval time as market-event
    time.
    """

    symbol: str
    exchange: str
    currency: str
    last_trade_price: Decimal
    last_trade_at: datetime
    last_trade_size: int | None
    bid_price: Decimal | None
    bid_size: int | None
    bid_at: datetime | None
    ask_price: Decimal | None
    ask_size: int | None
    ask_at: datetime | None
    snapshot_at: datetime
    content_sha256: str
    raw_response_sha256: str


@dataclass(frozen=True, slots=True)
class EODHDDelayedQuoteBatch:
    """Parsed delayed quotes plus their first local retrieval timestamp."""

    quotes: tuple[EODHDDelayedQuote, ...]
    retrieved_at: datetime
    raw_response_sha256: str


@dataclass(frozen=True, slots=True)
class EODHDJsonEvidence:
    """Credential-free identity and exact bytes for one EODHD JSON response.

    This transport result is deliberately calculation-neutral.  Acquisition
    applications may parse it into their own provider-neutral persistence
    contracts while retaining the exact vendor bytes and first local
    retrieval time.  Strategy and execution packages must never consume it.
    """

    endpoint: str
    retrieved_at: datetime
    payload: Any
    content: bytes
    content_sha256: str

    def __post_init__(self) -> None:
        if not self.endpoint.startswith("/api/") or "api_token" in self.endpoint.lower():
            msg = "EODHD evidence endpoint must be credential-free"
            raise EODHDMarketDataError(msg)
        object.__setattr__(
            self,
            "retrieved_at",
            _aware_utc(self.retrieved_at, field_name="retrieved_at"),
        )
        if not isinstance(self.content, bytes):
            msg = "EODHD evidence content must be exact bytes"
            raise EODHDMarketDataError(msg)
        if hashlib.sha256(self.content).hexdigest() != self.content_sha256:
            msg = "EODHD evidence bytes differ from their content SHA-256"
            raise EODHDMarketDataError(msg)


class _EODHDRequestThrottle:
    """Thread-safe, evenly spaced request throttle shared by every client path."""

    def __init__(
        self,
        *,
        monotonic_clock: Callable[[], float],
        sleeper: Callable[[float], None],
    ) -> None:
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._last_request_monotonic: float | None = None
        self._blocked_until_monotonic = 0.0
        self._request_interval_sec = _request_interval_sec(_EODHD_REQUESTS_PER_MINUTE)

    def acquire(self) -> None:
        """Wait until both the evenly spaced slot and server cooldown are open."""

        with self._lock:
            now = self._monotonic_clock()
            earliest = self._blocked_until_monotonic
            if self._last_request_monotonic is not None:
                earliest = max(
                    earliest,
                    self._last_request_monotonic + self._request_interval_sec,
                )
            if earliest > now:
                self._sleeper(earliest - now)
            # Treat a sleeper that returns early as having consumed the reserved
            # slot. This preserves conservative spacing with injected sleepers.
            self._last_request_monotonic = max(self._monotonic_clock(), earliest)

    def observe_limit(self, requests_per_minute: int | None) -> None:
        """Tighten pacing when EODHD reports a lower per-minute allowance."""

        if requests_per_minute is None:
            return
        effective_limit = min(requests_per_minute, _EODHD_REQUESTS_PER_MINUTE)
        with self._lock:
            self._request_interval_sec = _request_interval_sec(effective_limit)

    def defer(self, delay_seconds: float) -> None:
        """Prevent another attempt before a retry or server cooldown expires."""

        if delay_seconds <= 0:
            return
        with self._lock:
            now = self._monotonic_clock()
            if self._last_request_monotonic is not None:
                now = max(now, self._last_request_monotonic)
            self._blocked_until_monotonic = max(
                self._blocked_until_monotonic,
                now + delay_seconds,
            )


class EODHDClient(IMarketDataProvider):
    """Daily-bar fetcher for the EODHD ``/api/eod/{symbol}`` endpoint.

    Symbol mapping: a ``product_id`` that already contains a dot is passed
    verbatim (e.g. ``GSPC.INDX``, ``VIX.INDX``); otherwise the US suffix is
    appended (``AAPL`` -> ``AAPL.US``). v1 serves the US sleeve only, so every
    bare ticker is a US listing by construction.

    Does **not** own DB upsert or scheduling — those belong to
    ``PriceIngestionService`` and the ingestion apps, same as the sibling
    clients.
    """

    BASE_URL = "https://eodhd.com"
    source = "eodhd"

    def __init__(
        self,
        api_token: str,
        *,
        base_url: str = BASE_URL,
        exchange_mic: str = "XNYS",
        client: httpx.Client | None = None,
        malformed_row_policy: EODHDMalformedRowPolicy = EODHDMalformedRowPolicy.SKIP,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if not api_token:
            msg = "EODHD requires a non-empty api_token"
            raise ValueError(msg)
        if not isinstance(malformed_row_policy, EODHDMalformedRowPolicy):
            msg = "malformed_row_policy must be an EODHDMalformedRowPolicy"
            raise TypeError(msg)
        self._api_token = api_token
        self._exchange_mic = exchange_mic
        self._client = client or httpx.Client(base_url=base_url, timeout=30.0)
        self._malformed_row_policy = malformed_row_policy
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._sleep = sleep
        self._random_uniform = random_uniform
        self._throttle = _EODHDRequestThrottle(
            monotonic_clock=monotonic,
            sleeper=sleep,
        )

    def close(self) -> None:
        self._client.close()

    def fetch_candle_rows(
        self,
        *,
        product_id: str,
        instr_id: int,
        start_time: datetime,
        end_time: datetime,
        granularity: str = "ONE_MINUTE",
        broker_instrument_type: str | None = None,
    ) -> list[CandleRow]:
        """IMarketDataProvider entrypoint: fetch + normalize daily bars."""
        del broker_instrument_type
        normalized_granularity = granularity.strip().upper()
        if normalized_granularity != _DAILY_GRANULARITY:
            msg = f"EODHD provider supports only {_DAILY_GRANULARITY}, got {granularity!r}"
            raise ValueError(msg)
        symbol = self._to_eodhd_symbol(product_id)
        end_bound = _to_naive_utc(end_time)
        payload = self._fetch_list(
            path=f"/api/eod/{symbol}",
            label=symbol,
            from_date=_to_naive_utc(start_time).date(),
            to_date=end_bound.date(),
            extra_params={"period": "d"},
        )
        rows = [
            row
            for item in payload
            if (
                row := self._normalize_item(
                    item, symbol=symbol, instr_id=instr_id, end_bound=end_bound
                )
            )
            is not None
        ]
        rows.sort(key=lambda row: row.ts)
        return rows

    @staticmethod
    def _to_eodhd_symbol(product_id: str) -> str:
        cleaned = product_id.strip().upper()
        if "." in cleaned:
            return cleaned
        return f"{cleaned}.US"

    def fetch_splits(self, *, product_id: str, start: date, end: date) -> list[SplitRecord]:
        """Split events with ex-dates in ``[start, end]``; malformed rows fail loud.

        Corporate actions feed the in-house adjustment pipeline, so a bad row
        is raised, never skipped — a silently missing split corrupts every
        adjusted price before its ex-date.
        """
        symbol = self._to_eodhd_symbol(product_id)
        payload = self._fetch_list(
            path=f"/api/splits/{symbol}", label=symbol, from_date=start, to_date=end
        )
        records = []
        for item in payload:
            try:
                ex_date = date.fromisoformat(str(item["date"]))
                numerator, _, denominator = str(item["split"]).partition("/")
                ratio = Decimal(numerator) / Decimal(denominator)
            except (KeyError, ValueError, TypeError, InvalidOperation, ZeroDivisionError) as exc:
                msg = f"EODHD returned a malformed split row for {symbol}"
                raise EODHDMarketDataError(msg) from exc
            if ratio <= 0:
                msg = f"EODHD returned a non-positive split ratio for {symbol} on {ex_date}"
                raise EODHDMarketDataError(msg)
            records.append(SplitRecord(ex_date=ex_date, ratio=ratio))
        records.sort(key=lambda record: record.ex_date)
        return records

    def fetch_dividends(self, *, product_id: str, start: date, end: date) -> list[DividendRecord]:
        """Cash dividends with ex-dates in ``[start, end]``; malformed rows fail loud."""
        symbol = self._to_eodhd_symbol(product_id)
        payload = self._fetch_list(
            path=f"/api/div/{symbol}", label=symbol, from_date=start, to_date=end
        )
        records = []
        for item in payload:
            try:
                ex_date = date.fromisoformat(str(item["date"]))
                amount = Decimal(str(item["value"]))
            except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
                msg = f"EODHD returned a malformed dividend row for {symbol}"
                raise EODHDMarketDataError(msg) from exc
            if amount <= 0:
                msg = f"EODHD returned a non-positive dividend for {symbol} on {ex_date}"
                raise EODHDMarketDataError(msg)
            raw_currency = item.get("currency")
            currency = str(raw_currency) if raw_currency is not None else None
            records.append(DividendRecord(ex_date=ex_date, amount=amount, currency=currency))
        records.sort(key=lambda record: record.ex_date)
        return records

    def fetch_us_extended_delayed_quotes(
        self,
        *,
        product_ids: Sequence[str],
    ) -> EODHDDelayedQuoteBatch:
        """Fetch EODHD ``/api/us-quote-delayed`` quote snapshots.

        The endpoint silently omits unknown symbols.  This production boundary
        instead fails closed when any requested symbol is missing so a caller
        cannot mistake partial coverage for a complete rebalance quote set.
        API credentials are added only inside :meth:`_fetch_json`; exception
        messages and logs contain symbol labels but never the request URL.
        """

        symbols = tuple(self._normalize_us_delayed_symbol(item) for item in product_ids)
        if not symbols:
            msg = "EODHD delayed quote request requires at least one US symbol"
            raise ValueError(msg)
        if len(set(symbols)) != len(symbols):
            msg = "EODHD delayed quote request contains duplicate symbols"
            raise ValueError(msg)
        label = ",".join(symbols)
        _payload, content = self._fetch_json(
            path="/api/us-quote-delayed",
            label=label,
            params={"s": label},
        )
        retrieved_at = _aware_utc(self._clock(), field_name="retrieved_at")
        try:
            payload = json.loads(content, parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = f"EODHD returned a malformed JSON delayed quote body for {label}"
            raise EODHDMarketDataError(msg) from exc
        raw_sha256 = hashlib.sha256(content).hexdigest()
        quotes = self._parse_us_delayed_payload(
            payload,
            requested_symbols=symbols,
            raw_response_sha256=raw_sha256,
        )
        return EODHDDelayedQuoteBatch(
            quotes=quotes,
            retrieved_at=retrieved_at,
            raw_response_sha256=raw_sha256,
        )

    def fetch_daily_bar_evidence(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> EODHDJsonEvidence:
        """Fetch exact EOD response bytes for immutable acquisition writers."""

        if end < start:
            msg = "EODHD daily-bar evidence end cannot precede start"
            raise ValueError(msg)
        symbol = self._to_eodhd_symbol(product_id)
        endpoint = (
            f"/api/eod/{symbol}?from={start.isoformat()}&to={end.isoformat()}&period=d&fmt=json"
        )
        payload, content = self._fetch_json(
            path=f"/api/eod/{symbol}",
            label=f"{symbol} daily evidence",
            params={"from": start.isoformat(), "to": end.isoformat(), "period": "d"},
        )
        return self._json_evidence(endpoint, payload, content)

    def fetch_split_evidence(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> EODHDJsonEvidence:
        """Fetch exact split response bytes through the pinned retrieval date."""

        return self._fetch_dated_evidence(
            product_id=product_id,
            start=start,
            end=end,
            endpoint_name="splits",
        )

    def fetch_dividend_evidence(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> EODHDJsonEvidence:
        """Fetch exact dividend response bytes for total-return reconstruction."""

        return self._fetch_dated_evidence(
            product_id=product_id,
            start=start,
            end=end,
            endpoint_name="div",
        )

    def fetch_index_membership_history_evidence(
        self,
        *,
        index_symbol: str,
        start: date,
        end: date,
    ) -> tuple[EODHDJsonEvidence, EODHDJsonEvidence]:
        """Fetch licensed interval and full-state index-component evidence."""

        symbol = self._index_symbol(index_symbol)
        ticker_endpoint = f"/api/fundamentals/{symbol}?filter=HistoricalTickerComponents&fmt=json"
        ticker_payload, ticker_content = self._fetch_json(
            path=f"/api/fundamentals/{symbol}",
            label=f"{symbol} historical ticker components",
            params={"filter": "HistoricalTickerComponents"},
        )
        snapshot_endpoint = (
            f"/api/v1.1/fundamentals/{symbol}?historical=1"
            f"&from={start.isoformat()}&to={end.isoformat()}&fmt=json"
        )
        snapshot_payload, snapshot_content = self._fetch_json(
            path=f"/api/v1.1/fundamentals/{symbol}",
            label=f"{symbol} historical component snapshots",
            params={
                "historical": "1",
                "from": start.isoformat(),
                "to": end.isoformat(),
            },
        )
        return (
            self._json_evidence(ticker_endpoint, ticker_payload, ticker_content),
            self._json_evidence(snapshot_endpoint, snapshot_payload, snapshot_content),
        )

    def fetch_current_index_components_evidence(
        self,
        *,
        index_symbol: str,
    ) -> EODHDJsonEvidence:
        """Fetch the provider-documented current index constituent full state."""

        symbol = self._index_symbol(index_symbol)
        endpoint = f"/api/v1.1/fundamentals/{symbol}?filter=Components&fmt=json"
        payload, content = self._fetch_json(
            path=f"/api/v1.1/fundamentals/{symbol}",
            label=f"{symbol} current components",
            params={"filter": "Components"},
        )
        return self._json_evidence(endpoint, payload, content)

    def fetch_id_mapping_evidence(self, *, provider_symbol: str) -> EODHDJsonEvidence:
        """Fetch current permanent identifiers for one EODHD US security."""

        qualified = self._us_security_symbol(provider_symbol)
        endpoint = (
            f"/api/id-mapping?filter[symbol]={qualified}&page[limit]=100&page[offset]=0&fmt=json"
        )
        payload, content = self._fetch_json(
            path="/api/id-mapping",
            label=f"{qualified} identity mapping",
            params={
                "filter[symbol]": qualified,
                "page[limit]": "100",
                "page[offset]": "0",
            },
        )
        return self._json_evidence(endpoint, payload, content)

    def fetch_security_general_evidence(
        self,
        *,
        provider_symbol: str,
    ) -> EODHDJsonEvidence:
        """Fetch the exact current Fundamentals ``General`` identity block."""

        qualified = self._us_security_symbol(provider_symbol)
        endpoint = f"/api/v1.1/fundamentals/{qualified}?filter=General&fmt=json"
        payload, content = self._fetch_json(
            path=f"/api/v1.1/fundamentals/{qualified}",
            label=f"{qualified} Fundamentals General evidence",
            params={"filter": "General"},
        )
        return self._json_evidence(endpoint, payload, content)

    def _fetch_dated_evidence(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
        endpoint_name: str,
    ) -> EODHDJsonEvidence:
        if end < start:
            msg = f"EODHD {endpoint_name} evidence end cannot precede start"
            raise ValueError(msg)
        symbol = self._to_eodhd_symbol(product_id)
        endpoint = (
            f"/api/{endpoint_name}/{symbol}?from={start.isoformat()}&to={end.isoformat()}&fmt=json"
        )
        payload, content = self._fetch_json(
            path=f"/api/{endpoint_name}/{symbol}",
            label=f"{symbol} {endpoint_name} evidence",
            params={"from": start.isoformat(), "to": end.isoformat()},
        )
        return self._json_evidence(endpoint, payload, content)

    @staticmethod
    def _index_symbol(value: str) -> str:
        symbol = value.strip().upper()
        if not symbol.endswith(".INDX") or any(character.isspace() for character in symbol):
            msg = "EODHD index membership requires a non-blank .INDX symbol"
            raise ValueError(msg)
        return symbol

    @staticmethod
    def _us_security_symbol(value: str) -> str:
        symbol = value.strip().upper()
        if not symbol or any(character.isspace() for character in symbol):
            msg = "EODHD US security symbol must be non-blank and contain no whitespace"
            raise ValueError(msg)
        return symbol if symbol.endswith(".US") else f"{symbol}.US"

    def _json_evidence(
        self,
        endpoint: str,
        payload: Any,
        content: bytes,
    ) -> EODHDJsonEvidence:
        return EODHDJsonEvidence(
            endpoint=endpoint,
            retrieved_at=_aware_utc(self._clock(), field_name="retrieved_at"),
            payload=payload,
            content=content,
            content_sha256=hashlib.sha256(content).hexdigest(),
        )

    @staticmethod
    def _normalize_us_delayed_symbol(product_id: str) -> str:
        cleaned = str(product_id).strip().upper()
        if not cleaned:
            msg = "EODHD delayed quote symbol must not be empty"
            raise ValueError(msg)
        symbol = cleaned if "." in cleaned else f"{cleaned}.US"
        if not symbol.endswith(".US") or symbol.count(".") != 1:
            msg = f"EODHD delayed quote endpoint requires a US equity symbol, got {product_id!r}"
            raise ValueError(msg)
        return symbol

    def _parse_us_delayed_payload(
        self,
        payload: Any,
        *,
        requested_symbols: tuple[str, ...],
        raw_response_sha256: str,
    ) -> tuple[EODHDDelayedQuote, ...]:
        if not isinstance(payload, dict):
            msg = "EODHD delayed quote endpoint returned an unexpected top-level payload"
            raise EODHDMarketDataError(msg)
        meta = payload.get("meta")
        data = payload.get("data")
        links = payload.get("links")
        if not isinstance(meta, dict) or not isinstance(data, dict) or not isinstance(links, dict):
            msg = "EODHD delayed quote endpoint returned an incomplete response envelope"
            raise EODHDMarketDataError(msg)
        count = meta.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count != len(data):
            msg = "EODHD delayed quote response count does not match its data records"
            raise EODHDMarketDataError(msg)
        if links.get("next") is not None:
            msg = "EODHD delayed quote response unexpectedly requires pagination"
            raise EODHDMarketDataError(msg)

        returned_symbols = {str(key).strip().upper() for key in data}
        missing = sorted(set(requested_symbols) - returned_symbols)
        extra = sorted(returned_symbols - set(requested_symbols))
        if missing or extra:
            msg = "EODHD delayed quote response did not exactly match requested US symbols"
            raise EODHDMarketDataError(msg)

        return tuple(
            self._parse_us_delayed_quote(
                key=key,
                item=data[key],
                raw_response_sha256=raw_response_sha256,
            )
            for key in sorted(data)
        )

    def _parse_us_delayed_quote(
        self,
        *,
        key: str,
        item: Any,
        raw_response_sha256: str,
    ) -> EODHDDelayedQuote:
        symbol = str(key).strip().upper()
        if not isinstance(item, dict) or str(item.get("symbol") or "").strip().upper() != symbol:
            msg = f"EODHD delayed quote record identity is malformed for {symbol}"
            raise EODHDMarketDataError(msg)
        try:
            exchange = _required_text(item.get("exchange"), field_name="exchange")
            currency = _required_text(item.get("currency"), field_name="currency").upper()
            last_trade_price = _positive_decimal(
                item.get("lastTradePrice"), field_name="lastTradePrice"
            )
            last_trade_at = _epoch_utc(
                item.get("lastTradeTime"), milliseconds=True, field_name="lastTradeTime"
            )
            last_trade_size = _optional_nonnegative_integer(item.get("size"), field_name="size")
            bid_price, bid_size, bid_at = _parse_bbo_side(item, side="bid")
            ask_price, ask_size, ask_at = _parse_bbo_side(item, side="ask")
            snapshot_at = _epoch_utc(
                item.get("timestamp"), milliseconds=False, field_name="timestamp"
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            msg = f"EODHD returned a malformed delayed quote for {symbol}"
            raise EODHDMarketDataError(msg) from exc
        if bid_price is None or ask_price is None or bid_price > ask_price:
            msg = f"EODHD returned an invalid or crossed delayed BBO for {symbol}"
            raise EODHDMarketDataError(msg)
        if (
            len(currency) < _MIN_CURRENCY_LENGTH
            or len(currency) > _MAX_CURRENCY_LENGTH
            or not currency.isalnum()
        ):
            msg = f"EODHD returned an invalid delayed quote currency for {symbol}"
            raise EODHDMarketDataError(msg)
        content_sha256 = eodhd_delayed_quote_content_sha256(
            symbol=symbol,
            exchange=exchange,
            currency=currency,
            last_trade_price=last_trade_price,
            last_trade_at=last_trade_at,
            last_trade_size=last_trade_size,
            bid_price=bid_price,
            bid_size=bid_size,
            bid_at=bid_at,
            ask_price=ask_price,
            ask_size=ask_size,
            ask_at=ask_at,
            snapshot_at=snapshot_at,
        )
        return EODHDDelayedQuote(
            symbol=symbol,
            exchange=exchange,
            currency=currency,
            last_trade_price=last_trade_price,
            last_trade_at=last_trade_at,
            last_trade_size=last_trade_size,
            bid_price=bid_price,
            bid_size=bid_size,
            bid_at=bid_at,
            ask_price=ask_price,
            ask_size=ask_size,
            ask_at=ask_at,
            snapshot_at=snapshot_at,
            content_sha256=content_sha256,
            raw_response_sha256=raw_response_sha256,
        )

    def _fetch_list(
        self,
        *,
        path: str,
        label: str,
        from_date: date,
        to_date: date,
        extra_params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        params = {
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            **(extra_params or {}),
        }
        payload, _content = self._fetch_json(
            path=path,
            label=label,
            params=params,
        )
        if not isinstance(payload, list):
            msg = f"EODHD returned an unexpected payload shape for {label}"
            raise EODHDMarketDataError(msg)
        return payload

    def _fetch_json(
        self,
        *,
        path: str,
        label: str,
        params: dict[str, str],
    ) -> tuple[Any, bytes]:
        """Fetch JSON with shared retry and token-redaction behavior.

        The raw response bytes are returned so validation-only adapters can
        content-address provider evidence without reconstructing it from a
        decoded object. Callers never receive the credential-bearing URL or
        request parameters.
        """

        request_params = {
            "api_token": self._api_token,
            "fmt": "json",
            **params,
        }
        last_error: Exception | None = None
        last_status_code: int | None = None
        for attempt in range(_MAX_ATTEMPTS):
            self._throttle.acquire()
            try:
                response = self._client.get(path, params=request_params)
            except httpx.TimeoutException as exc:
                last_error = exc
                last_status_code = None
                logger.warning(
                    "EODHD timeout for %s (attempt %d/%d)",
                    label,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                )
                self._defer_retry(attempt=attempt, server_delay_seconds=None)
                continue
            except httpx.RequestError as exc:
                # str(exc) may embed the request URL (and thus the token);
                # keep only the exception type in our message.
                last_error = exc
                last_status_code = None
                logger.warning(
                    "EODHD request error for %s (attempt %d/%d): %s",
                    label,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    type(exc).__name__,
                )
                self._defer_retry(attempt=attempt, server_delay_seconds=None)
                continue
            self._observe_rate_limit_headers(response)
            if response.status_code in _RETRYABLE_STATUSES:
                server_delay = _retry_after_seconds(
                    response.headers.get("Retry-After"),
                    now=self._clock(),
                )
                if server_delay is not None and server_delay > _MAX_INLINE_RETRY_AFTER_SEC:
                    long_retry_at = _aware_utc(self._clock(), field_name="clock") + timedelta(
                        seconds=server_delay
                    )
                    message = (
                        f"EODHD request for {label} is rate limited beyond inline retry policy"
                    )
                    raise EODHDMarketDataError(
                        message,
                        kind=_eodhd_error_kind(response.status_code),
                        status_code=response.status_code,
                        retry_at=long_retry_at,
                    )
                logger.warning(
                    "EODHD HTTP %d for %s (attempt %d/%d)",
                    response.status_code,
                    label,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                )
                last_error = None
                last_status_code = response.status_code
                self._defer_retry(
                    attempt=attempt,
                    server_delay_seconds=server_delay,
                )
                continue
            if response.status_code >= _MIN_ERROR_STATUS:
                kind = _eodhd_error_kind(response.status_code)
                quota_retry_at = (
                    _next_eodhd_daily_reset(self._clock())
                    if kind is EODHDErrorKind.DAILY_QUOTA
                    else None
                )
                msg = f"EODHD request for {label} failed with HTTP {response.status_code}"
                raise EODHDMarketDataError(
                    msg,
                    kind=kind,
                    status_code=response.status_code,
                    retry_at=quota_retry_at,
                )
            try:
                payload = response.json()
            except ValueError as exc:
                msg = f"EODHD returned a non-JSON body for {label}"
                raise EODHDMarketDataError(msg) from exc
            return payload, response.content
        msg = f"EODHD request for {label} failed after {_MAX_ATTEMPTS} attempts"
        if last_error is not None:
            # The httpx cause can retain the credential-bearing request URL.
            # Suppress the chain rather than leaking it through a traceback.
            kind = (
                EODHDErrorKind.TIMEOUT
                if isinstance(last_error, httpx.TimeoutException)
                else EODHDErrorKind.TRANSPORT
            )
            raise EODHDMarketDataError(msg, kind=kind) from None
        raise EODHDMarketDataError(
            msg,
            kind=(
                _eodhd_error_kind(last_status_code)
                if last_status_code is not None
                else EODHDErrorKind.UNKNOWN
            ),
            status_code=last_status_code,
        )

    def _observe_rate_limit_headers(self, response: httpx.Response) -> None:
        limit = _optional_positive_integer_header(
            response.headers.get(_RATE_LIMIT_HEADER),
        )
        self._throttle.observe_limit(limit)
        remaining = _optional_nonnegative_integer_header(
            response.headers.get(_RATE_LIMIT_REMAINING_HEADER),
        )
        if remaining == 0:
            delay = _retry_after_seconds(
                response.headers.get("Retry-After"),
                now=self._clock(),
            )
            if delay is not None:
                self._throttle.defer(min(delay, _MAX_INLINE_RETRY_AFTER_SEC))

    def _defer_retry(
        self,
        *,
        attempt: int,
        server_delay_seconds: float | None,
    ) -> None:
        if attempt + 1 >= _MAX_ATTEMPTS:
            return
        exponential = min(
            _RETRY_MAX_DELAY_SEC,
            _RETRY_BASE_DELAY_SEC * (2**attempt),
        )
        jitter = self._random_uniform(
            -_RETRY_JITTER_RATIO,
            _RETRY_JITTER_RATIO,
        )
        bounded_jitter = min(_RETRY_JITTER_RATIO, max(-_RETRY_JITTER_RATIO, jitter))
        client_delay = max(0.0, exponential * (1.0 + bounded_jitter))
        self._throttle.defer(max(client_delay, server_delay_seconds or 0.0))

    def _normalize_item(
        self,
        item: dict[str, Any],
        *,
        symbol: str,
        instr_id: int,
        end_bound: datetime,
    ) -> CandleRow | None:
        try:
            session_date = date.fromisoformat(str(item["date"]))
            open_ = float(item["open"])
            high = float(item["high"])
            low = float(item["low"])
            close = float(item["close"])  # raw close by design; adjusted_close ignored
        except (KeyError, TypeError, ValueError) as exc:
            if self._malformed_row_policy is EODHDMalformedRowPolicy.RAISE:
                msg = f"EODHD returned a malformed daily bar for {symbol}"
                raise EODHDMarketDataError(msg) from exc
            logger.warning("Skipping malformed EODHD row for %s: keys=%s", symbol, sorted(item))
            return None
        volume: float | None = None
        if self._malformed_row_policy is EODHDMalformedRowPolicy.RAISE:
            try:
                volume = self._parse_volume(item)
            except (TypeError, ValueError) as exc:
                msg = f"EODHD returned a malformed daily bar for {symbol}"
                raise EODHDMarketDataError(msg) from exc
        if not is_trading_day(self._exchange_mic, session_date):
            # A provider may publish index reference values on dates outside
            # the exchange schedule.  They are not bars on the authoritative
            # session axis and therefore cannot enter a stored market series.
            # Snapshot acquisition separately requires exact coverage of its
            # pinned official sessions, so dropping an out-of-axis row cannot
            # hide a missing required observation.
            logger.warning(
                "Skipping EODHD row for %s on non-trading day %s (%s)",
                symbol,
                session_date.isoformat(),
                self._exchange_mic,
            )
            return None
        close_ts = session_close_utc(self._exchange_mic, session_date)
        if close_ts > end_bound:
            # The session has not closed relative to the requested bound —
            # never emit a still-forming bar.
            return None
        if self._malformed_row_policy is EODHDMalformedRowPolicy.SKIP:
            # Preserve the runtime/backfill ordering and error behavior.
            volume = self._parse_volume(item)
        return CandleRow(
            instr_id=instr_id,
            ts=close_ts,
            timeframe=_DAILY_TIMEFRAME,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            source=self.source,
        )

    @staticmethod
    def _parse_volume(item: dict[str, Any]) -> float | None:
        source_volume = item.get("volume")
        return float(source_volume) if source_volume is not None else None


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _eodhd_error_kind(status_code: int) -> EODHDErrorKind:
    """Map EODHD HTTP semantics without retaining or inspecting response bodies."""

    if status_code == httpx.codes.UNAUTHORIZED:
        return EODHDErrorKind.AUTHENTICATION
    if status_code == httpx.codes.PAYMENT_REQUIRED:
        return EODHDErrorKind.DAILY_QUOTA
    if status_code == httpx.codes.FORBIDDEN:
        return EODHDErrorKind.ENTITLEMENT
    if status_code == httpx.codes.TOO_MANY_REQUESTS:
        return EODHDErrorKind.RATE_LIMIT
    if status_code in _RETRYABLE_STATUSES:
        return EODHDErrorKind.TRANSIENT_HTTP
    return EODHDErrorKind.UNKNOWN


def _next_eodhd_daily_reset(value: datetime) -> datetime:
    """Return the next direct-subscription daily reset (midnight UTC/GMT)."""

    now = _aware_utc(value, field_name="clock")
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _request_interval_sec(requests_per_minute: int) -> float:
    if (
        isinstance(requests_per_minute, bool)
        or not isinstance(requests_per_minute, int)
        or requests_per_minute <= 0
    ):
        msg = "EODHD request limit must be a positive integer"
        raise ValueError(msg)
    conservative_limit = max(1, math.floor(requests_per_minute * _RATE_LIMIT_HEADROOM_RATIO))
    return _RATE_LIMIT_WINDOW_SEC / conservative_limit


def _optional_positive_integer_header(value: str | None) -> int | None:
    parsed = _optional_nonnegative_integer_header(value)
    return parsed if parsed is not None and parsed > 0 else None


def _optional_nonnegative_integer_header(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized.isdecimal():
        return None
    return int(normalized)


def _retry_after_seconds(value: str | None, *, now: datetime) -> float | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    try:
        seconds = float(normalized)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(normalized)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at.astimezone(UTC) - _aware_utc(now, field_name="clock")).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"{field_name} must be non-empty text"
        raise ValueError(msg)
    return value.strip()


def _positive_decimal(value: object, *, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        msg = f"{field_name} must be a positive decimal"
        raise ValueError(msg)
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        msg = f"{field_name} must be a finite positive decimal"
        raise ValueError(msg)
    return parsed


def _optional_nonnegative_integer(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        msg = f"{field_name} must be a non-negative integer"
        raise ValueError(msg)
    return value


def _epoch_utc(value: object, *, milliseconds: bool, field_name: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"{field_name} must be a positive Unix timestamp"
        raise ValueError(msg)
    try:
        if not milliseconds:
            return datetime.fromtimestamp(value, tz=UTC)
        seconds, remainder_ms = divmod(value, 1000)
        return datetime.fromtimestamp(seconds, tz=UTC) + timedelta(milliseconds=remainder_ms)
    except (OSError, OverflowError) as exc:
        msg = f"{field_name} is outside the supported timestamp range"
        raise ValueError(msg) from exc


def _parse_bbo_side(
    item: dict[str, Any],
    *,
    side: str,
) -> tuple[Decimal | None, int | None, datetime | None]:
    price_field = f"{side}Price"
    size_field = f"{side}Size"
    time_field = f"{side}Time"
    values = (item.get(price_field), item.get(size_field), item.get(time_field))
    if all(value is None for value in values):
        msg = f"{side} quote fields are required for an actionable delayed quote"
        raise ValueError(msg)
    if any(value is None for value in values):
        msg = f"{side} quote fields must be present as one timestamped set"
        raise ValueError(msg)
    size = _optional_nonnegative_integer(values[1], field_name=size_field)
    if size is None or size <= 0:
        msg = f"{size_field} must be a positive integer"
        raise ValueError(msg)
    return (
        _positive_decimal(values[0], field_name=price_field),
        size,
        _epoch_utc(values[2], milliseconds=True, field_name=time_field),
    )


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        msg = f"{field_name} must be a timezone-aware datetime"
        raise ValueError(msg)
    return value.astimezone(UTC)


def eodhd_delayed_quote_content_sha256(
    *,
    symbol: str,
    exchange: str,
    currency: str,
    last_trade_price: Decimal,
    last_trade_at: datetime,
    last_trade_size: int | None,
    bid_price: Decimal | None,
    bid_size: int | None,
    bid_at: datetime | None,
    ask_price: Decimal | None,
    ask_size: int | None,
    ask_at: datetime | None,
    snapshot_at: datetime,
) -> str:
    """Hash normalized quote content independently of HTTP formatting/retrieval."""

    content_payload = {
        "schema": "eodhd-us-extended-delayed-quote-v1",
        "symbol": symbol,
        "exchange": exchange,
        "currency": currency,
        "last_trade_price": canonical_eodhd_quote_decimal(last_trade_price),
        "last_trade_at": _aware_utc(last_trade_at, field_name="last_trade_at").isoformat(),
        "last_trade_size": last_trade_size,
        "bid_price": (canonical_eodhd_quote_decimal(bid_price) if bid_price is not None else None),
        "bid_size": bid_size,
        "bid_at": (
            _aware_utc(bid_at, field_name="bid_at").isoformat() if bid_at is not None else None
        ),
        "ask_price": (canonical_eodhd_quote_decimal(ask_price) if ask_price is not None else None),
        "ask_size": ask_size,
        "ask_at": (
            _aware_utc(ask_at, field_name="ask_at").isoformat() if ask_at is not None else None
        ),
        "snapshot_at": _aware_utc(snapshot_at, field_name="snapshot_at").isoformat(),
    }
    return canonical_json_hash(content_payload)


def canonical_eodhd_quote_decimal(value: Decimal) -> str:
    """Return the exact decimal identity used by delayed-quote evidence."""

    if not value.is_finite():
        msg = "delayed quote content identity requires a finite decimal"
        raise ValueError(msg)
    return format(value.normalize(), "f")
