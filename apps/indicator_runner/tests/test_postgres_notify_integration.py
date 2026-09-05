"""Real PostgreSQL coverage for the market-data notification boundary."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select

from indicator_runner.runtime_journal import (
    BufferedSignalEmitter,
    DurableSignalRelay,
    StrategyRuntimeIdentity,
    StrategyRuntimeStore,
)
from indicator_runner.signal_worker import SignalWorker
from lib_application.db.models import (
    ConsumerWatermark,
    Instrument,
    OutboxEvent,
    Strategy,
    StrategyDecision,
    StrategyRuntimeState,
)
from lib_application.db.session import create_engine_for_env, dispose_engine, get_session_factory
from lib_application.outbox import OutboxStore
from lib_application.services.price_ingestion_service import PriceIngestionService
from lib_data.market_data import CandleRow, MarketDataInstrument
from lib_infrastructure.market_data.coinbase_client import CoinbaseCandleClient
from lib_infrastructure.market_data.ports import IMarketDataProvider
from lib_strategy.signals.emitter import BacktestSignalEmitter
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from market_data_ingestor.scheduler import IngestionScheduler

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COINBASE_FIXTURE = (
    _REPO_ROOT / "tests/fixtures/market_data/coinbase_btcusdc_1m_integration_2026-06-10.json"
)
_SOURCE = CoinbaseCandleClient.source
_SYMBOL = "BTCUSDC"
_PRODUCT_ID = "BTC-USDC"
_TIMEFRAME = "1m"
_WORKER_ID = "postgres-notify-real-coinbase"
_WAIT_TIMEOUT_SECONDS = 10.0


class _BarObserver(PureSignalStrategy):
    """Observe production worker delivery without creating synthetic signals."""

    def __init__(self) -> None:
        super().__init__(
            strategy_id="postgres_notify_pipeline_probe",
            strategy_type="indicator",
            config={"strategy_version": "1.0.0"},
        )
        self.observed: list[MarketState] = []

    def initialize(self) -> None:
        return None

    def on_data(self, state: MarketState) -> None:
        self.observed.append(state)


class _PublicCoinbaseFixtureProvider(IMarketDataProvider):
    """Serve the exact committed Coinbase observation through the provider port."""

    source = _SOURCE

    def __init__(self, rows: list[CandleRow]) -> None:
        self._rows = rows

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
        assert broker_instrument_type is None
        assert product_id == _PRODUCT_ID
        assert granularity == "ONE_MINUTE"
        return [
            row
            for row in self._rows
            if row.instr_id == instr_id and start_time <= row.ts.replace(tzinfo=UTC) <= end_time
        ]

    def close(self) -> None:
        return None


def _load_real_coinbase_rows(instr_id: int) -> list[CandleRow]:
    fixture: dict[str, Any] = json.loads(_COINBASE_FIXTURE.read_text())
    assert fixture["fixture_schema"] == "vynmatrix.real-provider-candle-fixture.v1"
    assert fixture["fixture_scope"] == "real_provider_integration"
    assert fixture["market_evidence"] is True
    assert fixture["provider"] == "Coinbase Advanced Trade public API"
    assert fixture["requested_product_id"] == _PRODUCT_ID

    rows = CoinbaseCandleClient.normalize_candles(
        instr_id=instr_id,
        product_id=_PRODUCT_ID,
        candles=fixture["candles"],
        timeframe=_TIMEFRAME,
    )
    assert len(rows) == 11
    assert all(row.source == _SOURCE for row in rows)
    return rows


def _wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    msg = "Timed out waiting for PostgreSQL notification delivery"
    raise AssertionError(msg)


def _build_durable_worker(
    session_factory: Any,
    strategy: _BarObserver,
    *,
    dsn: str,
    bootstrap_bars: int,
) -> SignalWorker:
    """Wire the mandatory durable runtime trio around the observer strategy."""
    signal_buffer = BufferedSignalEmitter()
    strategy._emitter = signal_buffer
    identity = StrategyRuntimeIdentity.from_strategy(
        worker_id=_WORKER_ID,
        strategy=strategy,
        symbols=[_SYMBOL],
        source=_SOURCE,
        timeframe=_TIMEFRAME,
        consolidation_minutes=0,
    )
    return SignalWorker(
        strategy=strategy,
        session_factory=session_factory,
        worker_id=_WORKER_ID,
        symbols=[_SYMBOL],
        consolidation_minutes=0,
        dsn=dsn,
        bootstrap_bars=bootstrap_bars,
        source=_SOURCE,
        timeframe=_TIMEFRAME,
        signal_buffer=signal_buffer,
        runtime_store=StrategyRuntimeStore(session_factory, identity),
        signal_relay=DurableSignalRelay(
            outbox=OutboxStore(session_factory),
            emitter=BacktestSignalEmitter(),
            worker_id=_WORKER_ID,
            strategy_id=strategy.strategy_id,
        ),
    )


def _cleanup_runtime_rows(
    session_factory: Any,
    *,
    instrument_id: int | None,
    created_strategy: bool,
) -> None:
    """Remove durable runtime rows before their RESTRICT-referenced catalogue rows."""
    with session_factory() as session:
        session.execute(delete(StrategyDecision).where(StrategyDecision.worker_id == _WORKER_ID))
        session.execute(
            delete(StrategyRuntimeState).where(StrategyRuntimeState.worker_id == _WORKER_ID)
        )
        session.execute(delete(OutboxEvent).where(OutboxEvent.ordering_key == _WORKER_ID))
        session.execute(delete(ConsumerWatermark).where(ConsumerWatermark.worker_id == _WORKER_ID))
        if instrument_id is not None:
            session.execute(delete(Instrument).where(Instrument.instr_id == instrument_id))
        if created_strategy:
            session.execute(
                delete(Strategy).where(Strategy.strategy_id == "postgres_notify_pipeline_probe")
            )
        session.commit()


@pytest.mark.integration
def test_committed_candle_notifies_listener_and_advances_worker_watermark() -> None:
    """Exercise committed upsert -> scheduler NOTIFY -> LISTEN -> catch-up.

    The integration database must be isolated: the test refuses to reuse an
    existing BTCUSDC catalogue row rather than mutating developer or production
    market history. The frozen input is a real public Coinbase response; the
    observer deliberately emits no signal.
    """
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not set; skipping PostgreSQL integration")

    engine = create_engine_for_env(db_url=db_url)
    session_factory = get_session_factory(engine=engine)
    instrument_id: int | None = None
    created_strategy = False
    worker: SignalWorker | None = None
    scheduler: IngestionScheduler | None = None
    try:
        with session_factory() as session:
            existing = session.execute(
                select(Instrument.instr_id).where(
                    Instrument.asset_class == "crypto",
                    Instrument.canonical == _SYMBOL,
                )
            ).scalar_one_or_none()
            if existing is not None:
                msg = (
                    "PostgreSQL notification integration requires an isolated "
                    "database without an existing BTCUSDC instrument"
                )
                raise RuntimeError(msg)
            instrument = Instrument(
                asset_class="crypto",
                canonical=_SYMBOL,
                exchange="coinbase",
                settlement_currency="USDC",
            )
            session.add(instrument)
            if session.get(Strategy, "postgres_notify_pipeline_probe") is None:
                session.add(
                    Strategy(
                        strategy_id="postgres_notify_pipeline_probe",
                        strategy_name="PostgreSQL Notify Pipeline Probe",
                        asset_class="crypto",
                        is_active=True,
                    )
                )
                created_strategy = True
            session.commit()
            session.refresh(instrument)
            instr_id = int(instrument.instr_id)
            instrument_id = instr_id

        rows = _load_real_coinbase_rows(instr_id)
        ingestion = PriceIngestionService(session_factory)
        assert ingestion.upsert_candles(rows[:-1]) == len(rows) - 1

        strategy = _BarObserver()
        worker = _build_durable_worker(
            session_factory,
            strategy,
            dsn=db_url,
            bootstrap_bars=len(rows) - 1,
        )
        worker.start()
        assert worker._listener is not None
        assert worker._listener.wait_until_listening(_WAIT_TIMEOUT_SECONDS)
        assert [state.timestamp for state in strategy.observed] == [
            row.ts.replace(tzinfo=UTC) for row in rows[:-1]
        ]

        scheduler = IngestionScheduler(
            session_factory=session_factory,
            instruments=[
                MarketDataInstrument(
                    canonical_symbol=_SYMBOL,
                    product_id=_PRODUCT_ID,
                    asset_class="crypto",
                )
            ],
            granularity="ONE_MINUTE",
            source=_SOURCE,
        )
        scheduler._provider.close()
        scheduler._provider = _PublicCoinbaseFixtureProvider([rows[-1]])

        summary = scheduler._ingest_cycle(lookback_minutes=2)
        assert summary.rows_upserted == 1

        expected_ts = rows[-1].ts.replace(tzinfo=UTC)
        _wait_until(
            lambda: worker is not None and worker._watermark.get(_SYMBOL, _TIMEFRAME) == expected_ts
        )

        assert worker.failed is False
        assert strategy.observed[-1].timestamp == expected_ts
        assert strategy.observed[-1].metadata["price_source"] == _SOURCE
        with session_factory() as session:
            durable = session.execute(
                select(ConsumerWatermark).where(
                    ConsumerWatermark.worker_id == _WORKER_ID,
                    ConsumerWatermark.symbol == _SYMBOL,
                    ConsumerWatermark.timeframe == _TIMEFRAME,
                )
            ).scalar_one()
            assert durable.last_ts.replace(tzinfo=UTC) == expected_ts
            assert durable.rebuild_from_ts is None
    finally:
        if worker is not None:
            worker.stop()
        if scheduler is not None:
            scheduler.stop()
        _cleanup_runtime_rows(
            session_factory,
            instrument_id=instrument_id,
            created_strategy=created_strategy,
        )
        dispose_engine(engine)
