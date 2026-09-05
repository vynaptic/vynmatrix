"""Unit tests for the historical candle backfiller (no network/DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
import pytest

from lib_data.market_data import CandleRow, MarketDataInstrument
from market_data_ingestor import backfill as backfill_mod
from market_data_ingestor.backfill import HistoricalBackfiller, HistoricalBackfillError

_INSTRUMENT = MarketDataInstrument(
    canonical_symbol="BTC/USD",
    product_id="BTC-USD",
    asset_class="crypto",
)


class _FakeService:
    """Stand-in for PriceIngestionService — records upserts, no DB."""

    instance: _FakeService | None = None
    # Configurable per-test oldest-stored ts for deficit-aware ensure_history.
    oldest: datetime | None = None
    stored_timestamps: ClassVar[set[datetime]] = set()

    def __init__(self, _session_factory: Any) -> None:
        self.upserts: list[int] = []
        _FakeService.instance = self

    def load_instrument_map(self, *, asset_class: str = "crypto") -> dict[str, int]:
        if asset_class == "equity":
            return {"AAPL": 2}
        return {"BTCUSD": 1}

    def upsert_candles(self, rows: Any) -> int:
        self.upserts.append(len(rows))
        if rows:
            row_oldest = min(row.ts for row in rows)
            if row_oldest.tzinfo is None:
                row_oldest = row_oldest.replace(tzinfo=UTC)
            current = _FakeService.oldest
            if current is None or row_oldest < current:
                _FakeService.oldest = row_oldest
            _FakeService.stored_timestamps.update(
                row.ts.replace(tzinfo=UTC) if row.ts.tzinfo is None else row.ts for row in rows
            )
        return len(rows)

    def oldest_candle_ts(self, _instr_id: int, *, source: str, timeframe: str) -> datetime | None:
        return _FakeService.oldest

    def candle_count(
        self,
        _instr_id: int,
        *,
        source: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> int:
        return sum(start <= ts < end for ts in _FakeService.stored_timestamps)


class _FakeClient:
    source = "coinbase_live"

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[datetime, datetime]] = []
        self.requests: list[tuple[str, int, str | None]] = []
        self._raise = raise_exc

    def fetch_candles(
        self, *, product_id: str, start_time: datetime, end_time: datetime, granularity: str
    ) -> list[dict[str, Any]]:
        self.calls.append((start_time, end_time))
        if self._raise is not None:
            raise self._raise
        ts = int(start_time.timestamp())
        return [
            {"start": str(ts), "low": "1", "high": "2", "open": "1", "close": "2", "volume": "3"}
        ]

    def fetch_candle_rows(
        self,
        *,
        product_id: str,
        instr_id: int,
        start_time: datetime,
        end_time: datetime,
        granularity: str,
        broker_instrument_type: str | None = None,
    ) -> list[CandleRow]:
        self.requests.append((product_id, instr_id, broker_instrument_type))
        raw = self.fetch_candles(
            product_id=product_id,
            start_time=start_time,
            end_time=end_time,
            granularity=granularity,
        )
        rows: list[CandleRow] = []
        for candle in raw:
            try:
                rows.append(
                    CandleRow(
                        instr_id=instr_id,
                        ts=datetime.fromtimestamp(int(candle["start"]), tz=UTC).replace(
                            tzinfo=None
                        ),
                        timeframe="1m",
                        open=float(candle["open"]),
                        high=float(candle["high"]),
                        low=float(candle["low"]),
                        close=float(candle["close"]),
                        volume=float(candle["volume"]),
                        source=self.source,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return rows

    def close(self) -> None:
        pass


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.coinbase.com/candles")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


@pytest.fixture(autouse=True)
def _patch_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backfill_mod, "PriceIngestionService", _FakeService)
    _FakeService.instance = None
    _FakeService.oldest = None
    _FakeService.stored_timestamps = set()


def _make(client: _FakeClient) -> HistoricalBackfiller:
    return HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[_INSTRUMENT],
        granularity="ONE_MINUTE",
        provider=client,
        request_pause_sec=0.0,
        minimum_window_coverage=0.0,
        minimum_total_coverage=0.0,
        sleep=lambda _s: None,
    )


def test_pages_windows_and_upserts() -> None:
    client = _FakeClient()
    bf = _make(client)
    now = datetime(2026, 6, 19, tzinfo=UTC)

    summary = bf.backfill(days=1, now=now)

    # 1 day = 1440 1m candles / 300 per window -> 5 windows (4 full + 1 partial).
    assert len(client.calls) == 5
    assert client.calls[0][0] < client.calls[-1][0]  # windows advance
    assert client.calls[-1][1] == now  # last window ends at `now`
    assert summary.products_processed == 1
    assert summary.rows_upserted == 5  # one normalized row per window
    assert _FakeService.instance is not None
    assert _FakeService.instance.upserts == [1, 1, 1, 1, 1]


def test_exact_range_uses_half_open_boundaries_without_wall_clock_substitution() -> None:
    client = _FakeClient()
    bf = _make(client)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 1, 1, 1, tzinfo=UTC)

    summary = bf.backfill_range(
        start=start,
        end=end,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert client.calls[0][0] == start
    assert client.calls[-1][1] == end
    assert all(window_end <= end for _, window_end in client.calls)
    assert summary.products_processed == 1


@pytest.mark.parametrize(
    ("start", "end", "match"),
    [
        (
            datetime(2025, 1, 1),
            datetime(2025, 1, 2, tzinfo=UTC),
            "timezone-aware",
        ),
        (
            datetime(2025, 1, 2, tzinfo=UTC),
            datetime(2025, 1, 1, tzinfo=UTC),
            "start < end",
        ),
        (
            datetime(2025, 1, 1, 0, 0, 30, tzinfo=UTC),
            datetime(2025, 1, 2, tzinfo=UTC),
            "must align",
        ),
    ],
)
def test_exact_range_rejects_ambiguous_or_invalid_boundaries(
    start: datetime,
    end: datetime,
    match: str,
) -> None:
    client = _FakeClient()
    with pytest.raises(ValueError, match=match):
        _make(client).backfill_range(
            start=start,
            end=end,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
    assert client.calls == []


def test_cli_exact_range_rejects_naive_boundaries_before_database_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        backfill_mod.main(
            [
                "--start",
                "2025-01-01T00:00:00",
                "--end",
                "2025-01-02T00:00:00Z",
            ]
        )

    assert exc_info.value.code == 2
    assert "UTC boundary must be timezone-aware" in capsys.readouterr().err


def test_exact_range_rejects_unclosed_future_endpoint() -> None:
    client = _FakeClient()
    with pytest.raises(ValueError, match="last fully closed candle"):
        _make(client).backfill_range(
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 3, tzinfo=UTC),
            now=datetime(2026, 1, 2, tzinfo=UTC),
        )
    assert client.calls == []


def test_unknown_instrument_fails_closed() -> None:
    client = _FakeClient()
    bf = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[
            MarketDataInstrument(
                canonical_symbol="DOGE/USD",
                product_id="DOGE-USD",
                asset_class="crypto",
            )
        ],
        provider=client,
        request_pause_sec=0.0,
        sleep=lambda _s: None,
    )
    with pytest.raises(HistoricalBackfillError, match="instrument is unknown"):
        bf.backfill(days=1, now=datetime(2026, 6, 19, tzinfo=UTC))
    assert client.calls == []


def test_empty_instrument_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one instrument"):
        HistoricalBackfiller(session_factory=lambda: None, instruments=[])


def test_backfill_rejects_provider_source_mismatch() -> None:
    with pytest.raises(ValueError, match="provider/source mismatch"):
        HistoricalBackfiller(
            session_factory=lambda: None,
            instruments=[_INSTRUMENT],
            source="ibkr",
            provider=_FakeClient(),
        )


def test_non_coinbase_provider_preserves_exact_identity_and_provenance() -> None:
    class _IBKRProvider(_FakeClient):
        source = "ibkr"

    provider = _IBKRProvider()
    instrument = MarketDataInstrument(
        canonical_symbol="AAPL",
        product_id="265598",
        asset_class="equity",
    )
    backfiller = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[instrument],
        source="ibkr",
        provider=provider,
        request_pause_sec=0.0,
        minimum_total_coverage=0.0,
        sleep=lambda _seconds: None,
    )

    summary = backfiller.backfill_range(
        start=datetime(2026, 6, 18, tzinfo=UTC),
        end=datetime(2026, 6, 18, 1, tzinfo=UTC),
        now=datetime(2026, 6, 19, tzinfo=UTC),
    )

    assert summary.rows_upserted == 1
    assert provider.requests == [("265598", 2, None)]
    assert _FakeService.stored_timestamps == {datetime(2026, 6, 18, tzinfo=UTC)}


def test_provider_rows_cannot_be_relabelled_to_the_configured_source() -> None:
    class _MislabeledProvider(_FakeClient):
        source = "ibkr"

        def fetch_candle_rows(self, **kwargs: Any) -> list[CandleRow]:
            rows = super().fetch_candle_rows(**kwargs)
            rows[0].source = "coinbase_live"
            return rows

    provider = _MislabeledProvider()
    instrument = MarketDataInstrument(
        canonical_symbol="AAPL",
        product_id="265598",
        asset_class="equity",
    )
    backfiller = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[instrument],
        source="ibkr",
        provider=provider,
        request_pause_sec=0.0,
        minimum_total_coverage=0.0,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(HistoricalBackfillError, match="provider source mismatch"):
        backfiller.backfill_range(
            start=datetime(2026, 6, 18, tzinfo=UTC),
            end=datetime(2026, 6, 18, 1, tzinfo=UTC),
            now=datetime(2026, 6, 19, tzinfo=UTC),
        )

    assert _FakeService.instance is not None
    assert _FakeService.instance.upserts == []


def test_persistent_network_error_fails_fast() -> None:
    client = _FakeClient(raise_exc=httpx.ConnectError("boom"))
    bf = _make(client)
    with pytest.raises(HistoricalBackfillError, match="coinbase_live backfill network failure"):
        bf.backfill(days=1, now=datetime(2026, 6, 19, tzinfo=UTC))
    # Abort after the first persistently failing window instead of returning a
    # successful deployment job with silent holes in strategy warm-up data.
    assert len(client.calls) == backfill_mod._MAX_WINDOW_ATTEMPTS


def test_nonretryable_http_error_fails_on_first_attempt() -> None:
    error = _http_status_error(httpx.codes.UNAUTHORIZED)
    client = _FakeClient(raise_exc=error)
    bf = _make(client)

    with pytest.raises(HistoricalBackfillError, match=r"HTTP 401.*after 1 attempt") as exc_info:
        bf.backfill(days=1, now=datetime(2026, 6, 19, tzinfo=UTC))

    assert len(client.calls) == 1
    assert exc_info.value.__cause__ is error


def test_transient_server_error_retries_then_succeeds() -> None:
    class _RecoveringClient(_FakeClient):
        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            if not self.calls:
                self.calls.append((start_time, end_time))
                raise _http_status_error(httpx.codes.SERVICE_UNAVAILABLE)
            return super().fetch_candles(
                product_id=product_id,
                start_time=start_time,
                end_time=end_time,
                granularity=granularity,
            )

    client = _RecoveringClient()
    summary = _make(client).backfill(days=1, now=datetime(2026, 6, 19, tzinfo=UTC))
    assert summary.rows_upserted == 5
    assert len(client.calls) == 6  # first window retried once, four later windows


def test_persistent_rate_limit_retries_then_fails() -> None:
    error = _http_status_error(httpx.codes.TOO_MANY_REQUESTS)
    client = _FakeClient(raise_exc=error)
    with pytest.raises(HistoricalBackfillError, match=r"HTTP 429.*after 4 attempt") as exc_info:
        _make(client).backfill(days=1, now=datetime(2026, 6, 19, tzinfo=UTC))
    assert len(client.calls) == backfill_mod._MAX_WINDOW_ATTEMPTS
    assert exc_info.value.__cause__ is error


def test_empty_window_retries_then_fails_without_aggregate_gate() -> None:
    class _InvalidWindowClient(_FakeClient):
        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            self.calls.append((start_time, end_time))
            return []

    client = _InvalidWindowClient()
    with pytest.raises(HistoricalBackfillError, match=r"no candles.*after 4 attempt"):
        _make(client).backfill(days=1, now=datetime(2026, 6, 19, tzinfo=UTC))
    assert len(client.calls) == backfill_mod._MAX_WINDOW_ATTEMPTS


def test_entirely_malformed_provider_output_fails_after_empty_retries() -> None:
    class _InvalidWindowClient(_FakeClient):
        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            self.calls.append((start_time, end_time))
            return [{"low": "1", "high": "2", "open": "1", "close": "2", "volume": "3"}]

    client = _InvalidWindowClient()
    with pytest.raises(HistoricalBackfillError, match=r"no candles.*after 4 attempt"):
        _make(client).backfill(days=1, now=datetime(2026, 6, 19, tzinfo=UTC))
    assert len(client.calls) == backfill_mod._MAX_WINDOW_ATTEMPTS


def test_one_retried_empty_window_can_defer_to_total_coverage_gate() -> None:
    client = _FakeClient()
    client.fetch_candles = lambda **_kwargs: []  # type: ignore[method-assign]
    bf = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[_INSTRUMENT],
        provider=client,
        request_pause_sec=0.0,
        minimum_window_coverage=0.0,
        minimum_total_coverage=0.95,
        sleep=lambda _seconds: None,
    )
    start = datetime(2026, 5, 8, 2, 0, tzinfo=UTC)

    assert bf._backfill_window(_INSTRUMENT, 1, start, start + timedelta(hours=5)) == 0


def test_out_of_range_only_window_retries_then_uses_bounded_gap_policy() -> None:
    class _EndBoundaryOnlyClient(_FakeClient):
        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            self.calls.append((start_time, end_time))
            return [
                {
                    "start": str(int(end_time.timestamp())),
                    "low": "1",
                    "high": "2",
                    "open": "1",
                    "close": "2",
                    "volume": "3",
                }
            ]

    client = _EndBoundaryOnlyClient()
    bf = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[_INSTRUMENT],
        provider=client,
        request_pause_sec=0.0,
        minimum_window_coverage=0.0,
        minimum_total_coverage=0.95,
        sleep=lambda _seconds: None,
    )
    start = datetime(2026, 5, 8, 2, 0, tzinfo=UTC)

    assert bf._backfill_window(_INSTRUMENT, 1, start, start + timedelta(hours=5)) == 0
    assert len(client.calls) == backfill_mod._MAX_WINDOW_ATTEMPTS


def test_partially_rejected_window_is_not_tolerated_as_venue_gap() -> None:
    class _TwoRowClient(_FakeClient):
        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            self.calls.append((start_time, end_time))
            return [
                {
                    "start": str(int((start_time + timedelta(minutes=index)).timestamp())),
                    "low": "1",
                    "high": "2",
                    "open": "1",
                    "close": "2",
                    "volume": "3",
                }
                for index in range(2)
            ]

    client = _TwoRowClient()
    bf = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[_INSTRUMENT],
        provider=client,
        request_pause_sec=0.0,
        minimum_window_coverage=0.0,
        minimum_total_coverage=0.95,
        sleep=lambda _seconds: None,
    )
    assert _FakeService.instance is not None
    _FakeService.instance.upsert_candles = lambda _rows: 1  # type: ignore[method-assign]
    start = datetime(2026, 5, 8, 2, 0, tzinfo=UTC)

    with pytest.raises(HistoricalBackfillError, match=r"count mismatch.*rows=1 expected=2"):
        bf._backfill_window(_INSTRUMENT, 1, start, start + timedelta(hours=5))

    assert len(client.calls) == 1


def test_second_retried_empty_window_fails_bounded_gap_policy() -> None:
    class _EmptyClient(_FakeClient):
        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            self.calls.append((start_time, end_time))
            return []

    client = _EmptyClient()
    bf = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[_INSTRUMENT],
        provider=client,
        request_pause_sec=0.0,
        minimum_window_coverage=0.0,
        minimum_total_coverage=0.95,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(HistoricalBackfillError, match=r"too many empty windows.*count=2 allowed=1"):
        bf.backfill(days=1, now=datetime(2026, 6, 19, tzinfo=UTC))
    assert len(client.calls) == 2 * backfill_mod._MAX_WINDOW_ATTEMPTS


def test_session_based_feed_allows_closed_windows_but_still_meets_total_gate() -> None:
    class _ScheduledIBKRProvider(_FakeClient):
        source = "ibkr"

        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            self.calls.append((start_time, end_time))
            if len(self.calls) > 1:
                return []
            return [
                {
                    "start": str(int((start_time + timedelta(minutes=index)).timestamp())),
                    "low": "1",
                    "high": "2",
                    "open": "1",
                    "close": "2",
                    "volume": "3",
                }
                for index in range(300)
            ]

    instrument = MarketDataInstrument(
        canonical_symbol="AAPL",
        product_id="265598",
        asset_class="equity",
    )
    provider = _ScheduledIBKRProvider()
    backfiller = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[instrument],
        source="ibkr",
        provider=provider,
        request_pause_sec=0.0,
        sleep=lambda _seconds: None,
    )

    summary = backfiller.ensure_history(
        days=1,
        now=datetime(2026, 6, 19, tzinfo=UTC),
    )

    assert summary.products_processed == 1
    assert summary.rows_upserted == 300
    assert len(provider.calls) == 5


def test_empty_window_budget_is_shared_across_partial_and_repair_passes() -> None:
    class _PassAwareClient(_FakeClient):
        def __init__(self, *, first_empty: tuple[datetime, datetime]) -> None:
            super().__init__()
            self._first_empty = first_empty
            self._full_pass_started = False

        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            self.calls.append((start_time, end_time))
            if end_time == now:
                self._full_pass_started = True
            # The repair pass reaches ``first_empty`` again before this new,
            # adjacent gap. Re-fetching the same gap must not consume a second
            # budget slot; the distinct interval must then fail closed.
            second_empty = (
                self._first_empty[0] - timedelta(hours=5),
                self._first_empty[0],
            )
            if (start_time, end_time) == self._first_empty or (
                self._full_pass_started and (start_time, end_time) == second_empty
            ):
                return []
            return [
                {
                    "start": str(int(cursor.timestamp())),
                    "low": "1",
                    "high": "2",
                    "open": "1",
                    "close": "2",
                    "volume": "3",
                }
                for cursor in (
                    start_time + timedelta(minutes=index)
                    for index in range(int((end_time - start_time).total_seconds() // 60))
                )
            ]

    now = datetime(2026, 6, 19, tzinfo=UTC)
    oldest = now - timedelta(hours=50)
    desired_start = now - timedelta(days=10)
    first_empty = (oldest - timedelta(hours=5), oldest)
    _FakeService.oldest = oldest
    _FakeService.stored_timestamps = {oldest}
    client = _PassAwareClient(first_empty=first_empty)
    bf = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[_INSTRUMENT],
        provider=client,
        request_pause_sec=0.0,
        minimum_window_coverage=0.0,
        minimum_total_coverage=0.95,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(HistoricalBackfillError, match=r"too many empty windows.*count=2"):
        bf.ensure_history(days=10, now=now)

    assert first_empty[0] >= desired_start
    assert client._full_pass_started
    assert client.calls.count(first_empty) == 2 * backfill_mod._MAX_WINDOW_ATTEMPTS


def test_invalid_granularity_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported granularity"):
        HistoricalBackfiller(
            session_factory=lambda: None,
            instruments=[_INSTRUMENT],
            granularity="NOPE",
        )


# --- SW-2: deficit-aware auto-warm (ensure_history) ---------------------------


def test_ensure_history_backfills_full_window_when_no_data() -> None:
    # No stored history → behaves like a full backfill of the window.
    _FakeService.oldest = None
    client = _FakeClient()
    bf = _make(client)
    now = datetime(2026, 6, 19, tzinfo=UTC)

    summary = bf.ensure_history(days=1, now=now)

    assert len(client.calls) == 5  # same 5 windows as a full 1-day backfill
    assert client.calls[0][1] == now
    assert summary.products_processed == 1


def test_ensure_history_skips_when_already_covered() -> None:
    # Oldest stored candle predates the desired start → nothing to fetch.
    now = datetime(2026, 6, 19, tzinfo=UTC)
    _FakeService.oldest = datetime(2026, 6, 1, tzinfo=UTC)  # well before now-1d
    client = _FakeClient()
    bf = _make(client)

    summary = bf.ensure_history(days=1, now=now)

    assert client.calls == []  # fully covered → no requests
    assert summary.products_processed == 0
    assert summary.rows_upserted == 0


def test_ensure_history_repairs_stale_tail_despite_passing_aggregate_coverage() -> None:
    class _CompleteWindowClient(_FakeClient):
        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            self.calls.append((start_time, end_time))
            count = int((end_time - start_time).total_seconds() // 60)
            return [
                {
                    "start": str(int((start_time + timedelta(minutes=index)).timestamp())),
                    "low": "1",
                    "high": "2",
                    "open": "1",
                    "close": "2",
                    "volume": "3",
                }
                for index in range(count)
            ]

    now = datetime(2026, 6, 19, tzinfo=UTC)
    desired_start = now - timedelta(days=10)
    _FakeService.oldest = desired_start - timedelta(days=1)
    # A six-hour stale suffix still leaves 97.5% aggregate coverage and used to
    # make the one-shot incorrectly declare convergence.
    cursor = desired_start
    while cursor < now - timedelta(hours=6):
        _FakeService.stored_timestamps.add(cursor)
        cursor += timedelta(minutes=1)

    client = _CompleteWindowClient()
    bf = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[_INSTRUMENT],
        provider=client,
        request_pause_sec=0.0,
        minimum_window_coverage=0.0,
        minimum_total_coverage=0.95,
        sleep=lambda _seconds: None,
    )

    summary = bf.ensure_history(days=10, now=now)

    assert summary.products_processed == 1
    assert client.calls == [(now - timedelta(minutes=300), now)]
    assert now - timedelta(minutes=1) in _FakeService.stored_timestamps


def test_ensure_history_handles_naive_oldest_ts() -> None:
    # Regression: ``prices.ts`` is ``timestamp without time zone`` so the real
    # service hands back a tz-naive datetime. ensure_history must not crash with
    # "can't compare offset-naive and offset-aware datetimes" — a startup-backfill
    # blocker that crash-looped the ingestor on every restart once history existed.
    now = datetime(2026, 6, 19, tzinfo=UTC)
    _FakeService.oldest = datetime(2026, 6, 1)  # naive (no tzinfo), well before now-1d
    client = _FakeClient()
    bf = _make(client)

    summary = bf.ensure_history(days=1, now=now)

    assert client.calls == []  # naive oldest normalized to UTC → recognized as covered
    assert summary.products_processed == 0


def test_ensure_history_backfills_only_missing_older_portion() -> None:
    # Partial coverage: stored history starts mid-window → fetch only the older
    # gap [now-2d, oldest], not the already-stored [oldest, now].
    now = datetime(2026, 6, 19, tzinfo=UTC)
    oldest = datetime(2026, 6, 18, tzinfo=UTC)  # 1 day of history exists
    _FakeService.oldest = oldest
    client = _FakeClient()
    bf = _make(client)

    bf.ensure_history(days=2, now=now)

    assert client.calls, "expected the missing older window to be fetched"
    # Every fetched window stays at or before the oldest stored candle — the
    # covered recent portion is never re-fetched.
    assert all(end <= oldest for _start, end in client.calls)


def test_ensure_history_resumes_contiguous_suffix_after_partial_failure() -> None:
    class _FailAfterOneWindow(_FakeClient):
        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            if self.calls:
                self.calls.append((start_time, end_time))
                raise httpx.ConnectError("transient outage")
            return super().fetch_candles(
                product_id=product_id,
                start_time=start_time,
                end_time=end_time,
                granularity=granularity,
            )

    now = datetime(2026, 6, 19, tzinfo=UTC)
    failing_client = _FailAfterOneWindow()
    with pytest.raises(HistoricalBackfillError):
        _make(failing_client).ensure_history(days=1, now=now)

    partial_oldest = _FakeService.oldest
    assert partial_oldest is not None
    assert failing_client.calls[0][1] == now

    resume_client = _FakeClient()
    _make(resume_client).ensure_history(days=1, now=now)

    desired_start = now - timedelta(days=1)
    assert resume_client.calls[0][1] == partial_oldest
    assert resume_client.calls[-1][0] == desired_start
    assert all(end <= partial_oldest for _start, end in resume_client.calls)


def test_backfill_excludes_current_forming_candle() -> None:
    class _BoundaryClient(_FakeClient):
        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            self.calls.append((start_time, end_time))
            closed_start = int((end_time - timedelta(minutes=1)).timestamp())
            forming_start = int(end_time.timestamp())
            return [
                {
                    "start": str(closed_start),
                    "low": "1",
                    "high": "2",
                    "open": "1",
                    "close": "2",
                    "volume": "3",
                },
                {
                    "start": str(forming_start),
                    "low": "2",
                    "high": "3",
                    "open": "2",
                    "close": "3",
                    "volume": "4",
                },
            ]

    client = _BoundaryClient()
    bf = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[_INSTRUMENT],
        provider=client,
        request_pause_sec=0.0,
        minimum_window_coverage=0.0,
        minimum_total_coverage=0.0,
        sleep=lambda _seconds: None,
    )
    now = datetime(2026, 6, 19, 12, 34, 50, tzinfo=UTC)
    bf.ensure_history(days=1, now=now)

    assert client.calls[0][1] == datetime(2026, 6, 19, 12, 34, tzinfo=UTC)
    assert datetime(2026, 6, 19, 12, 33, tzinfo=UTC) in _FakeService.stored_timestamps
    assert datetime(2026, 6, 19, 12, 34, tzinfo=UTC) not in _FakeService.stored_timestamps


def test_configured_window_coverage_rejects_284_of_300_unique_bars() -> None:
    class _SparseClient(_FakeClient):
        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            self.calls.append((start_time, end_time))
            return [
                {
                    "start": str(int((start_time + timedelta(minutes=index)).timestamp())),
                    "low": "1",
                    "high": "2",
                    "open": "1",
                    "close": "2",
                    "volume": "3",
                }
                for index in range(284)
            ]

    start = datetime(2026, 6, 19, tzinfo=UTC)
    bf = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[_INSTRUMENT],
        provider=_SparseClient(),
        request_pause_sec=0.0,
        minimum_window_coverage=0.95,
        minimum_total_coverage=0.0,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(HistoricalBackfillError, match="rows=284 required=285"):
        bf._backfill_window(_INSTRUMENT, 1, start, start + timedelta(minutes=300))


def test_sparse_legacy_history_is_repaired_with_full_coverage() -> None:
    class _FullWindowClient(_FakeClient):
        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, Any]]:
            self.calls.append((start_time, end_time))
            cursor = start_time
            candles: list[dict[str, Any]] = []
            while cursor < end_time:
                candles.append(
                    {
                        "start": str(int(cursor.timestamp())),
                        "low": "1",
                        "high": "2",
                        "open": "1",
                        "close": "2",
                        "volume": "3",
                    }
                )
                cursor += timedelta(minutes=1)
            return candles

    now = datetime(2026, 6, 19, tzinfo=UTC)
    desired_start = now - timedelta(days=1)
    _FakeService.oldest = desired_start
    _FakeService.stored_timestamps = {desired_start}
    client = _FullWindowClient()
    bf = HistoricalBackfiller(
        session_factory=lambda: None,
        instruments=[_INSTRUMENT],
        provider=client,
        request_pause_sec=0.0,
        minimum_window_coverage=0.95,
        sleep=lambda _seconds: None,
    )

    summary = bf.ensure_history(days=1, now=now)

    assert summary.products_processed == 1
    assert len(_FakeService.stored_timestamps) == 1440
    assert client.calls[0][1] == now
    assert client.calls[-1][0] == desired_start
