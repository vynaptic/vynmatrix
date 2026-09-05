"""OBS-1 / MD-2: the ingestor exposes a real freshness probe so a stalled feed
reads as not-ready instead of leaving a static {"process": True} probe green."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lib_data.market_data import CandleRow, MarketDataInstrument
from lib_infrastructure.market_data.eodhd_client import (
    EODHDErrorKind,
    EODHDMarketDataError,
)
from market_data_ingestor.scheduler import IngestionScheduler


def _scheduler(
    *,
    poll: int = 60,
    threshold: int | None = None,
    symbols: list[str] | None = None,
) -> IngestionScheduler:
    # coinbase_live resolves a provider without network/creds at construction.
    return IngestionScheduler(
        session_factory=lambda: None,
        instruments=[
            MarketDataInstrument(
                canonical_symbol=symbol.replace("-", ""),
                product_id=symbol,
                asset_class="crypto",
            )
            for symbol in (symbols or ["BTC-USD"])
        ],
        granularity="ONE_MINUTE",
        source="coinbase_live",
        poll_interval_sec=poll,
        staleness_threshold_sec=threshold,
    )


def test_not_fresh_before_any_ingest() -> None:
    assert _scheduler().is_fresh() is False


def test_fresh_after_recent_candle() -> None:
    sched = _scheduler(threshold=120)
    sched._record_freshness("BTCUSD", "1m", datetime.now(tz=UTC))
    assert sched.is_fresh() is True


def test_stale_after_threshold() -> None:
    sched = _scheduler(threshold=120)
    sched._record_freshness("BTCUSD", "1m", datetime.now(tz=UTC) - timedelta(seconds=600))
    assert sched.is_fresh() is False


def test_one_fresh_symbol_does_not_mask_missing_configured_symbol() -> None:
    sched = _scheduler(threshold=120, symbols=["BTC-USD", "ETH-USD"])
    sched._record_freshness("BTCUSD", "1m", datetime.now(tz=UTC))

    assert sched.is_fresh() is False


def test_freshness_snapshot_reports_age() -> None:
    sched = _scheduler()
    sched._record_freshness("BTCUSD", "1m", datetime.now(tz=UTC) - timedelta(seconds=30))
    snapshot = sched.freshness_snapshot()
    assert "BTCUSD:1m" in snapshot
    assert 25 <= snapshot["BTCUSD:1m"] <= 90


def test_default_threshold_scales_with_poll_and_period() -> None:
    # 2*poll + candle period (60s for 1m), floored at 120s.
    assert _scheduler(poll=300)._staleness_threshold_sec == 660  # 600 + 60
    assert _scheduler(poll=10)._staleness_threshold_sec == 120  # max(120, 20+60)


def test_owner_scoped_delayed_batch_uses_exact_configured_equity_mapping() -> None:
    calls: list[dict[str, object]] = []

    class _DelayedIngestion:
        def acquire_and_persist(self, **kwargs: object) -> tuple[object, ...]:
            calls.append(dict(kwargs))
            return (object(),)

    scheduler = IngestionScheduler(
        session_factory=lambda: None,
        instruments=[
            MarketDataInstrument(
                canonical_symbol="AAPL",
                product_id="AAPL",
                asset_class="equity",
            )
        ],
        granularity="ONE_DAY",
        source="eodhd",
        api_key="unit-test-token",
        delayed_quote_owner_user_id="owner-1",
        delayed_quote_ingestion=_DelayedIngestion(),  # type: ignore[arg-type]
    )
    try:
        scheduler._ingest_delayed_quotes({"equity": {"AAPL": 8}})
        scheduler._record_freshness("AAPL", "1d", datetime.now(tz=UTC))

        assert calls == [
            {
                "product_ids": ["AAPL.US"],
                "entitlement_owner_user_id": "owner-1",
                "instrument_ids": {"AAPL.US": 8},
            }
        ]
        assert "eodhd_delayed_quotes:batch" in scheduler.freshness_snapshot()
        assert scheduler.is_fresh() is True
    finally:
        scheduler.stop()


def test_candle_cadence_cannot_be_faster_than_scheduler_poll() -> None:
    with pytest.raises(ValueError, match="at least poll_interval_sec"):
        IngestionScheduler(
            session_factory=lambda: None,
            instruments=[
                MarketDataInstrument(
                    canonical_symbol="BTCUSD",
                    product_id="BTC-USD",
                    asset_class="crypto",
                )
            ],
            granularity="ONE_MINUTE",
            source="coinbase_live",
            poll_interval_sec=60,
            candle_poll_interval_sec=30,
        )


def test_delayed_ingestion_without_owner_is_rejected() -> None:
    class _DelayedIngestion:
        pass

    with pytest.raises(ValueError, match="requires an exact entitlement owner"):
        IngestionScheduler(
            session_factory=lambda: None,
            instruments=[
                MarketDataInstrument(
                    canonical_symbol="AAPL",
                    product_id="AAPL",
                    asset_class="equity",
                )
            ],
            granularity="ONE_DAY",
            source="eodhd",
            api_key="unit-test-token",
            delayed_quote_ingestion=_DelayedIngestion(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("kind", "status_code"),
    [
        (EODHDErrorKind.AUTHENTICATION, 401),
        (EODHDErrorKind.ENTITLEMENT, 403),
    ],
)
def test_eodhd_auth_or_entitlement_does_not_masquerade_as_daily_quota(
    kind: EODHDErrorKind,
    status_code: int,
) -> None:
    scheduler = IngestionScheduler(
        session_factory=lambda: None,
        instruments=[
            MarketDataInstrument(
                canonical_symbol="AAPL",
                product_id="AAPL",
                asset_class="equity",
            )
        ],
        granularity="ONE_DAY",
        source="eodhd",
        api_key="unit-test-token",
    )

    def _raise_contract_error(**_kwargs: object) -> list[CandleRow]:
        msg = "EODHD request failed"
        raise EODHDMarketDataError(msg, kind=kind, status_code=status_code)

    scheduler._provider.fetch_candle_rows = _raise_contract_error  # type: ignore[method-assign]
    scheduler._ingestion_svc.load_instrument_map = lambda **_kwargs: {"AAPL": 8}
    scheduler._ingestion_svc.latest_candle_ts = lambda *_args, **_kwargs: None

    scheduler._run_cycle_safely(lookback_minutes=2)
    scheduler._run_cycle_safely(lookback_minutes=2)

    assert scheduler.is_provider_ready() is True


def test_closed_candles_drops_the_forming_candle() -> None:
    # MD-1: a candle whose period has not closed yet (start within the last
    # period) must be dropped; closed candles are kept.
    sched = _scheduler()  # ONE_MINUTE -> period 60s
    now = datetime(2026, 6, 26, 12, 0, 30, tzinfo=UTC)  # 30s into the 12:00 candle

    def _candle(ts: datetime) -> CandleRow:
        return CandleRow(
            instr_id=1, ts=ts, timeframe="1m", open=1, high=1, low=1, close=1, volume=1, source="x"
        )

    rows = [
        _candle(datetime(2026, 6, 26, 11, 58)),  # closed
        _candle(datetime(2026, 6, 26, 11, 59)),  # closed with guard elapsed
        _candle(datetime(2026, 6, 26, 12, 0)),  # forming (ends 12:01 > now) -> dropped
    ]
    kept = sched._closed_candles(rows, now)
    kept_ts = [r.ts for r in kept]
    assert datetime(2026, 6, 26, 12, 0) not in kept_ts
    assert datetime(2026, 6, 26, 11, 59) in kept_ts
    assert len(kept) == 2


def test_closed_candles_applies_clock_skew_guard() -> None:
    sched = _scheduler()
    now = datetime(2026, 6, 26, 12, 0, 1, tzinfo=UTC)
    row = CandleRow(
        instr_id=1,
        ts=datetime(2026, 6, 26, 11, 59),
        timeframe="1m",
        open=1,
        high=1,
        low=1,
        close=1,
        volume=1,
        source="x",
    )

    assert sched._closed_candles([row], now) == []


def test_naive_candle_ts_is_treated_as_utc() -> None:
    # prices.ts is stored naive UTC; freshness must coerce it, not crash.
    sched = _scheduler(threshold=120)
    naive_now = datetime.now(tz=UTC).replace(tzinfo=None)
    sched._record_freshness("BTCUSD", "1m", naive_now)
    assert sched.is_fresh() is True


def test_ingest_cycle_resumes_from_durable_latest_candle() -> None:
    sched = _scheduler()
    now = datetime.now(tz=UTC)
    durable_latest = now - timedelta(minutes=600)
    captured: dict[str, datetime] = {}

    class _Provider:
        def fetch_candle_rows(self, **kwargs):  # type: ignore[no-untyped-def]
            captured["start"] = kwargs["start_time"]
            captured["end"] = kwargs["end_time"]
            return [
                CandleRow(
                    instr_id=1,
                    ts=(kwargs["start_time"] + timedelta(minutes=1)).replace(tzinfo=None),
                    timeframe="1m",
                    open=1,
                    high=1,
                    low=1,
                    close=1,
                    volume=1,
                    source="coinbase_live",
                )
            ]

        def close(self) -> None:
            return None

    sched._provider = _Provider()
    sched._ingestion_svc.load_instrument_map = lambda **_kwargs: {"BTCUSD": 1}
    sched._ingestion_svc.latest_candle_ts = lambda *_args, **_kwargs: durable_latest
    sched._ingestion_svc.upsert_candles = lambda rows: len(rows)

    sched._ingest_cycle(lookback_minutes=2)

    assert captured["start"] <= durable_latest
    assert captured["end"] - captured["start"] <= timedelta(minutes=350)


def test_opaque_provider_id_notifies_with_canonical_symbol() -> None:
    sched = IngestionScheduler(
        session_factory=lambda: None,
        instruments=[
            MarketDataInstrument(
                canonical_symbol="AAPL",
                product_id="265598",
                asset_class="equity",
                broker_instrument_type="Stock",
            )
        ],
        source="coinbase_live",
    )
    captured: dict[str, object] = {}

    class _Provider:
        source = "ibkr"

        def fetch_candle_rows(self, **kwargs):  # type: ignore[no-untyped-def]
            captured["product_id"] = kwargs["product_id"]
            captured["broker_instrument_type"] = kwargs["broker_instrument_type"]
            return [
                CandleRow(
                    instr_id=8,
                    ts=datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(minutes=2),
                    timeframe="1m",
                    open=100,
                    high=101,
                    low=99,
                    close=100,
                    volume=10,
                    source="ibkr",
                )
            ]

        def close(self) -> None:
            return None

    sched._provider.close()
    sched._provider = _Provider()
    sched._source = "ibkr"
    sched._ingestion_svc.load_instrument_map = lambda **kwargs: (
        {"AAPL": 8} if kwargs["asset_class"] == "equity" else {}
    )
    sched._ingestion_svc.latest_candle_ts = lambda *_args, **_kwargs: None
    sched._ingestion_svc.upsert_candles = lambda rows: len(rows)
    sched._pg_notify = lambda **kwargs: captured.update(kwargs)  # type: ignore[method-assign]

    sched._ingest_cycle(lookback_minutes=2)

    assert captured["product_id"] == "265598"
    assert captured["broker_instrument_type"] == "Stock"
    assert captured["symbol"] == "AAPL"
    assert captured["source"] == "ibkr"


def test_provider_row_with_wrong_provenance_fails_before_persistence() -> None:
    sched = _scheduler()

    class _Provider:
        def fetch_candle_rows(self, **kwargs):  # type: ignore[no-untyped-def]
            return [
                CandleRow(
                    instr_id=1,
                    ts=datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(minutes=2),
                    timeframe="1m",
                    open=1,
                    high=1,
                    low=1,
                    close=1,
                    volume=1,
                    source="deribit",
                )
            ]

        def close(self) -> None:
            return None

    sched._provider.close()
    sched._provider = _Provider()
    sched._ingestion_svc.load_instrument_map = lambda **_kwargs: {"BTCUSD": 1}
    sched._ingestion_svc.latest_candle_ts = lambda *_args, **_kwargs: None
    persisted: list[CandleRow] = []
    sched._ingestion_svc.upsert_candles = lambda rows: persisted.extend(rows)

    with pytest.raises(ValueError, match="identity boundary"):
        sched._ingest_cycle(lookback_minutes=2)
    assert persisted == []
