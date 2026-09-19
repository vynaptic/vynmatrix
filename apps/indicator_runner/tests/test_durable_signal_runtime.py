"""Crash and recovery contracts for the durable indicator runtime."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, delete, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from indicator_runner import runtime_journal as runtime_journal_module
from indicator_runner.runtime_journal import (
    BufferedSignalEmitter,
    DurableSignalRelay,
    StrategyOperationalStatusReader,
    StrategyRuntimeIdentity,
    StrategyRuntimeStore,
)
from indicator_runner.signal_worker import SignalWorker
from lib_application.db.models import (
    Base,
    ConsumerWatermark,
    ExecutionDecisionLog,
    Instrument,
    InstrumentPrice,
    OutboxEvent,
    Strategy,
    StrategyDecision,
    StrategyRuntimeState,
    User,
)
from lib_application.db.session import (
    create_engine_for_env,
    dispose_engine,
    get_session_factory,
)
from lib_application.outbox import OutboxStore
from lib_application.services.price_ingestion_service import PriceIngestionService
from lib_data.market_data import CandleRow
from lib_data.watermark import Watermark
from lib_strategy.signals.emitter import BacktestSignalEmitter
from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import MarketState, ModelStateContractError
from lib_strategy.signals.signal import SignalAction

_REPO = Path(__file__).resolve().parents[3]
_FIXTURE = _REPO / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json"
_STRATEGY_DIR = _REPO / "tests/fixtures/strategies/PipelineExerciser"
_STRATEGY_ID = "pipeline_exerciser_v1"
_WORKER_ID = "durable-exerciser-real-coinbase"
_SYMBOL = "BTCUSD"
_SOURCE = "coinbase_exchange_public"
_TIMEFRAME = "1m"
_ENTRY_INDEX = 72
_CLOSE_INDEX = 82
_PROCESSING_AT = datetime(2026, 6, 10, 1, 12, 30, tzinfo=UTC)
_TENANT_IDS = ("durable-runtime-filled", "durable-runtime-rejected")


@pytest.fixture
def session_factory() -> Any:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, future=True)

    @contextmanager
    def _factory() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    return _factory


def _public_rows() -> list[dict[str, Any]]:
    payload = json.loads(_FIXTURE.read_text())
    assert payload["product"] == "BTC-USD"
    assert payload["source"] == _SOURCE
    assert payload["bar_count"] == 1501
    rows = payload["bars"]
    assert isinstance(rows, list)
    return rows


def _seed_catalogue(session_factory: Any) -> int:
    with session_factory() as session:
        instrument = Instrument(
            asset_class="crypto",
            canonical=_SYMBOL,
            settlement_currency="USD",
        )
        session.add(instrument)
        session.add(
            Strategy(
                strategy_id=_STRATEGY_ID,
                strategy_name="EMA Cross Scalper",
                asset_class="crypto",
                is_active=True,
            )
        )
        session.commit()
        session.refresh(instrument)
        return int(instrument.instr_id)


def _insert_public_rows(
    session_factory: Any,
    *,
    instr_id: int,
    rows: list[dict[str, Any]],
) -> None:
    with session_factory() as session:
        for row in rows:
            session.add(
                InstrumentPrice(
                    instr_id=instr_id,
                    ts=datetime.fromtimestamp(row["ts"], tz=UTC).replace(tzinfo=None),
                    timeframe=_TIMEFRAME,
                    source=_SOURCE,
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                )
            )
        session.commit()


def _build_worker(
    session_factory: Any,
    *,
    delivery_emitter: BacktestSignalEmitter | None = None,
    lease_seconds: int = 60,
    config_overrides: dict[str, Any] | None = None,
    consolidation_minutes: int = 0,
    bootstrap_bars: int = 500,
    strategy_dir: Path = _STRATEGY_DIR,
    symbols: list[str] | None = None,
) -> tuple[
    SignalWorker,
    BacktestSignalEmitter,
    StrategyRuntimeStore,
    OutboxStore,
]:
    config_payload = json.loads((strategy_dir / "config.json").read_text())
    strategy_id = config_payload["strategy_id"]
    config = dict(config_payload["parameters"])
    config["strategy_version"] = config_payload["strategy_version"]
    config.update(config_overrides or {})
    worker_symbols = symbols or [_SYMBOL]
    buffer = BufferedSignalEmitter()
    strategy = load_pure_strategy_core(strategy_dir)(
        strategy_id=strategy_id,
        config=config,
        emitter=buffer,
    )
    identity = StrategyRuntimeIdentity.from_strategy(
        worker_id=_WORKER_ID,
        strategy=strategy,
        symbols=worker_symbols,
        source=_SOURCE,
        timeframe=_TIMEFRAME,
        consolidation_minutes=consolidation_minutes,
    )
    store = StrategyRuntimeStore(session_factory, identity)
    outbox = OutboxStore(session_factory)
    delivery = delivery_emitter or BacktestSignalEmitter()
    relay = DurableSignalRelay(
        outbox=outbox,
        emitter=delivery,
        worker_id=_WORKER_ID,
        strategy_id=strategy_id,
        lease_seconds=lease_seconds,
    )
    worker = SignalWorker(
        strategy=strategy,
        session_factory=session_factory,
        worker_id=_WORKER_ID,
        symbols=worker_symbols,
        consolidation_minutes=consolidation_minutes,
        dsn="",
        bootstrap_bars=bootstrap_bars,
        source=_SOURCE,
        timeframe=_TIMEFRAME,
        clock=lambda: _PROCESSING_AT,
        signal_buffer=buffer,
        runtime_store=store,
        signal_relay=relay,
    )
    return worker, delivery, store, outbox


def test_cold_start_checkpoint_and_model_snapshot_commit_atomically(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed initial snapshot must not acknowledge its source history."""
    rows = _public_rows()
    instr_id = _seed_catalogue(session_factory)
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=rows[:30],
    )
    worker, _delivery, store, _outbox = _build_worker(session_factory)

    def _fail_initial_persistence(*_args: Any, **_kwargs: Any) -> None:
        msg = "injected initial snapshot failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(store, "persist_initial_on_session", _fail_initial_persistence)

    with pytest.raises(RuntimeError, match="injected initial snapshot failure"):
        worker.start()

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ConsumerWatermark)) == 0
        assert session.get(StrategyRuntimeState, _WORKER_ID) is None

    recovered, _delivery, _store, _outbox = _build_worker(session_factory)
    recovered.start()

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ConsumerWatermark)) == 1
        assert session.get(StrategyRuntimeState, _WORKER_ID) is not None


def test_fifteen_minute_transition_retains_exact_last_source_identity(
    session_factory: Any,
) -> None:
    """A derived strategy bar remains linked to its exact persisted 1m row."""
    rows = _public_rows()
    instr_id = _seed_catalogue(session_factory)
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=rows[:15],
    )
    worker, delivered, _store, _outbox = _build_worker(
        session_factory,
        consolidation_minutes=15,
    )
    observed: list[MarketState] = []
    on_data = worker._strategy.on_data

    def _observe(state: MarketState) -> None:
        observed.append(state)
        on_data(state)

    worker._strategy.on_data = _observe  # type: ignore[method-assign]
    worker.start()
    observed.clear()
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=rows[15:30],
    )
    worker._catchup_symbol(_SYMBOL)

    assert delivered.get_signals() == []
    assert len(observed) == 1
    state = observed[0]
    expected_source_ts = datetime.fromtimestamp(rows[29]["ts"], tz=UTC)
    assert state.timestamp == expected_source_ts + timedelta(minutes=1)
    assert state.metadata["timeframe"] == "15m"
    assert state.metadata["price_timeframe"] == "1m"
    assert state.metadata["source_price_ts"] == expected_source_ts.isoformat()
    with session_factory() as session:
        source_row = session.scalar(
            select(InstrumentPrice).where(
                InstrumentPrice.instr_id == instr_id,
                InstrumentPrice.ts == expected_source_ts.replace(tzinfo=None),
                InstrumentPrice.source == _SOURCE,
                InstrumentPrice.timeframe == _TIMEFRAME,
            )
        )
        decision = session.scalar(
            select(StrategyDecision).where(StrategyDecision.worker_id == _WORKER_ID)
        )
        runtime = session.get(StrategyRuntimeState, _WORKER_ID)
        assert source_row is not None
        assert decision is not None
        assert runtime is not None
        assert state.metadata["price_id"] == int(source_row.price_id)
        assert state.metadata["content_revision"] == int(source_row.content_revision)
        assert decision.source_price_id == source_row.price_id
        assert decision.source_content_revision == source_row.content_revision
        assert runtime.last_source_price_id == source_row.price_id


def test_signal_transition_rolls_back_and_fresh_core_recovers(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State, decision, signal envelope, and watermark share one commit."""
    rows = _public_rows()
    instr_id = _seed_catalogue(session_factory)
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=rows[:_ENTRY_INDEX],
    )
    worker, _delivery, store, _outbox = _build_worker(session_factory)
    worker.start()
    before = worker._watermark.get(_SYMBOL, _TIMEFRAME)
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=[rows[_ENTRY_INDEX]],
    )

    enqueue = store._outbox.enqueue_on_session

    def _crash_after_enqueue(*args: Any, **kwargs: Any) -> str:
        event_id = enqueue(*args, **kwargs)
        raise RuntimeError("injected crash before strategy transaction commit")

    monkeypatch.setattr(store._outbox, "enqueue_on_session", _crash_after_enqueue)
    with pytest.raises(RuntimeError, match="injected crash"):
        worker._catchup_symbol(_SYMBOL)

    with session_factory() as session:
        state = session.get(StrategyRuntimeState, _WORKER_ID)
        assert state is not None
        assert state.state_payload["symbol_states"][_SYMBOL]["position"] == 0
        assert session.query(StrategyDecision).count() == 0
        assert session.query(OutboxEvent).count() == 0
        watermark = session.get(
            ConsumerWatermark,
            (_WORKER_ID, _SYMBOL, _TIMEFRAME),
        )
        assert watermark is not None
        assert watermark.last_ts == before.replace(tzinfo=None)

    recovered, delivered, _store, _outbox = _build_worker(session_factory)
    recovered.start()
    assert recovered._strategy.state_for(_SYMBOL).position == 0
    recovered._catchup_symbol(_SYMBOL)

    signals = delivered.get_signals()
    assert [signal.action for signal in signals] == [SignalAction.LONG]
    with session_factory() as session:
        state = session.get(StrategyRuntimeState, _WORKER_ID)
        assert state is not None
        assert state.state_payload["symbol_states"][_SYMBOL]["position"] == 1
        assert session.query(StrategyDecision).count() == 1
        outbox = session.query(OutboxEvent).one()
        assert outbox.status == "published"
        assert outbox.event_key == f"strategy-signal:{signals[0].external_signal_id}"

    # Restart while the shared model is long, then consume the next real-history
    # exit. Bootstrap is flat, restoration is long, and no duplicate LONG escapes.
    restarted, after_restart, _store, _outbox = _build_worker(session_factory)
    restarted.start()
    assert restarted._strategy.state_for(_SYMBOL).position == 1
    assert after_restart.get_signals() == []
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=rows[_ENTRY_INDEX + 1 : _CLOSE_INDEX + 1],
    )
    restarted._catchup_symbol(_SYMBOL)
    assert [signal.action for signal in after_restart.get_signals()] == [SignalAction.CLOSE]

    # A later restart replays real history containing both the entry and close
    # to reconstruct indicators. Those already-journaled historical decisions
    # must not leak into the next live transaction buffer.
    after_close_restart, after_close_delivery, _store, _outbox = _build_worker(session_factory)
    after_close_restart.start()
    assert after_close_delivery.get_signals() == []
    assert after_close_restart._signal_buffer is not None
    assert after_close_restart._signal_buffer.pending() == ()


def test_acknowledged_signal_replays_same_identity_after_mark_crash(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP acknowledgement followed by a DB crash remains replay-safe."""
    rows = _public_rows()
    instr_id = _seed_catalogue(session_factory)
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=rows[:_ENTRY_INDEX],
    )
    first_delivery = BacktestSignalEmitter()
    worker, _delivery, _store, outbox = _build_worker(
        session_factory,
        delivery_emitter=first_delivery,
        lease_seconds=1,
    )
    worker.start()
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=[rows[_ENTRY_INDEX]],
    )

    def _crash_before_mark(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("injected crash after scoring acknowledgement")

    monkeypatch.setattr(outbox, "mark_published", _crash_before_mark)
    with pytest.raises(RuntimeError, match="after scoring acknowledgement"):
        worker._catchup_symbol(_SYMBOL)
    first_signal = first_delivery.get_signals()
    assert len(first_signal) == 1

    with session_factory() as session:
        row = session.query(OutboxEvent).one()
        row.claimed_at = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(seconds=2)
        session.commit()

    second_delivery = BacktestSignalEmitter()
    restarted, _delivery, _store, _outbox = _build_worker(
        session_factory,
        delivery_emitter=second_delivery,
        lease_seconds=1,
    )
    restarted.start()
    second_signal = second_delivery.get_signals()
    assert len(second_signal) == 1
    assert second_signal[0].external_signal_id == first_signal[0].external_signal_id
    with session_factory() as session:
        row = session.query(OutboxEvent).one()
        assert row.status == "published"
        assert row.attempts == 2


def test_correction_rebuild_snapshot_and_generation_ack_are_atomic(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real-history rebuild cannot clear its fence without its new snapshot."""
    rows = _public_rows()
    instr_id = _seed_catalogue(session_factory)
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=rows[:_ENTRY_INDEX],
    )
    worker, _delivery, _store, _outbox = _build_worker(session_factory)
    worker.start()
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=[rows[_ENTRY_INDEX]],
    )
    worker._catchup_symbol(_SYMBOL)
    with session_factory() as session:
        original = session.get(StrategyRuntimeState, _WORKER_ID)
        assert original is not None
        original_generation = int(original.generation)

    rebuild_from = datetime.fromtimestamp(rows[20]["ts"], tz=UTC)
    worker._watermark.request_rebuilds(
        [(_SYMBOL, _TIMEFRAME, instr_id)],
        rebuild_from=rebuild_from,
    )
    rebuilding, rebuilding_delivery, store, _outbox = _build_worker(session_factory)
    persist = store.persist_rebuild_on_session

    def _crash_after_snapshot(*args: Any, **kwargs: Any) -> StrategyRuntimeState:
        state = persist(*args, **kwargs)
        raise RuntimeError("injected crash before rebuild generation commit")

    monkeypatch.setattr(store, "persist_rebuild_on_session", _crash_after_snapshot)
    with pytest.raises(RuntimeError, match="rebuild generation commit"):
        rebuilding.start()
    assert rebuilding_delivery.get_signals() == []
    with session_factory() as session:
        state = session.get(StrategyRuntimeState, _WORKER_ID)
        watermark = session.get(
            ConsumerWatermark,
            (_WORKER_ID, _SYMBOL, _TIMEFRAME),
        )
        assert state is not None
        assert watermark is not None
        assert int(state.generation) == original_generation
        assert watermark.rebuild_from_ts == rebuild_from.replace(tzinfo=None)

    recovered, recovered_delivery, _store, _outbox = _build_worker(session_factory)
    recovered.start()
    assert recovered_delivery.get_signals() == []
    assert recovered._strategy.state_for(_SYMBOL).position == 1
    with session_factory() as session:
        state = session.get(StrategyRuntimeState, _WORKER_ID)
        watermark = session.get(
            ConsumerWatermark,
            (_WORKER_ID, _SYMBOL, _TIMEFRAME),
        )
        assert state is not None
        assert watermark is not None
        assert int(state.generation) == original_generation + 1
        assert watermark.rebuild_from_ts is None


def test_incompatible_snapshot_fails_before_new_bar_is_processed(
    session_factory: Any,
) -> None:
    rows = _public_rows()
    instr_id = _seed_catalogue(session_factory)
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=rows[:_ENTRY_INDEX],
    )
    worker, _delivery, _store, _outbox = _build_worker(session_factory)
    worker.start()
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=[rows[_ENTRY_INDEX]],
    )
    worker._catchup_symbol(_SYMBOL)
    checkpoint = worker._watermark.get(_SYMBOL, _TIMEFRAME)
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=[rows[_ENTRY_INDEX + 1]],
    )

    incompatible, delivered, _store, _outbox = _build_worker(
        session_factory,
        config_overrides={"confidence": "0.71"},
    )
    with pytest.raises(ModelStateContractError, match="Incompatible durable runtime state"):
        incompatible.start()

    assert delivered.get_signals() == []
    assert incompatible._watermark.get(_SYMBOL, _TIMEFRAME) == checkpoint
    with session_factory() as session:
        assert session.query(StrategyDecision).count() == 1


def test_parent_operational_status_tracks_real_feed_lag_and_dead_letter(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent readiness is derived from durable progress, not child metrics."""
    rows = _public_rows()
    instr_id = _seed_catalogue(session_factory)
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=rows[:_ENTRY_INDEX],
    )
    worker, delivered, _store, _outbox = _build_worker(session_factory)
    worker.start()
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=rows[_ENTRY_INDEX : _ENTRY_INDEX + 1],
    )
    worker._catchup_symbol(_SYMBOL)
    assert [signal.action for signal in delivered.get_signals()] == [SignalAction.LONG]
    _insert_public_rows(
        session_factory,
        instr_id=instr_id,
        rows=rows[_ENTRY_INDEX + 1 : _ENTRY_INDEX + 3],
    )

    watermark = Watermark(session_factory, _WORKER_ID, source=_SOURCE)
    latest_ts = datetime.fromtimestamp(rows[_ENTRY_INDEX + 2]["ts"], tz=UTC)
    observed_at = datetime.now(tz=UTC)
    metric_values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    class _Gauge:
        def __init__(self, name: str) -> None:
            self._name = name
            self._labels: dict[str, str] = {}

        def labels(self, **labels: str) -> _Gauge:
            self._labels = labels
            return self

        def set(self, value: float) -> None:
            metric_values[(self._name, tuple(sorted(self._labels.items())))] = value

    monkeypatch.setattr(
        runtime_journal_module,
        "gauge",
        lambda name, _documentation, _labelnames: _Gauge(name),
    )
    reader = StrategyOperationalStatusReader(
        session_factory,
        clock=lambda: observed_at,
    )

    lagging = reader.read(
        worker_id=_WORKER_ID,
        strategy_id=_STRATEGY_ID,
        symbols=[_SYMBOL],
        source=_SOURCE,
        timeframe=_TIMEFRAME,
        asset_class="crypto",
        max_outbox_age_seconds=300,
        max_strategy_lag_seconds=30,
    )
    assert lagging.ready is False
    assert lagging.feed_lags[0].lag_seconds == 120
    assert lagging.feed_lags[0].reason == "strategy_lag_exceeded"

    watermark.set(
        _SYMBOL,
        _TIMEFRAME,
        latest_ts,
        instr_id=instr_id,
    )
    current = reader.read(
        worker_id=_WORKER_ID,
        strategy_id=_STRATEGY_ID,
        symbols=[_SYMBOL],
        source=_SOURCE,
        timeframe=_TIMEFRAME,
        asset_class="crypto",
        max_outbox_age_seconds=300,
        max_strategy_lag_seconds=30,
    )
    assert current.ready is True
    assert current.feed_lags[0].lag_seconds == 0

    with session_factory() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.ordering_key == _WORKER_ID))
        assert event is not None
        event.status = "dead_letter"
        event.created_at = observed_at.replace(tzinfo=None) - timedelta(seconds=600)
        session.commit()

    dead_lettered = reader.read(
        worker_id=_WORKER_ID,
        strategy_id=_STRATEGY_ID,
        symbols=[_SYMBOL],
        source=_SOURCE,
        timeframe=_TIMEFRAME,
        asset_class="crypto",
        max_outbox_age_seconds=300,
        max_strategy_lag_seconds=30,
    )
    assert dead_lettered.ready is False
    assert dead_lettered.outbox_counts["dead_letter"] == 1
    assert dead_lettered.outbox_oldest_age_seconds == 600
    assert (
        metric_values[
            (
                "vm_indicator_strategy_lag_seconds",
                (
                    ("strategy_id", _STRATEGY_ID),
                    ("symbol", _SYMBOL),
                    ("worker_id", _WORKER_ID),
                ),
            )
        ]
        == 0
    )
    assert (
        metric_values[
            (
                "vm_indicator_signal_backlog_events",
                (
                    ("status", "dead_letter"),
                    ("strategy_id", _STRATEGY_ID),
                    ("worker_id", _WORKER_ID),
                ),
            )
        ]
        == 1
    )
    assert (
        metric_values[
            (
                "vm_indicator_signal_backlog_oldest_seconds",
                (
                    ("strategy_id", _STRATEGY_ID),
                    ("worker_id", _WORKER_ID),
                ),
            )
        ]
        == 600
    )
    assert (
        metric_values[
            (
                "vm_indicator_worker_operational_ready",
                (
                    ("strategy_id", _STRATEGY_ID),
                    ("worker_id", _WORKER_ID),
                ),
            )
        ]
        == 0
    )
    watermark.request_rebuilds(
        [(_SYMBOL, _TIMEFRAME, instr_id)],
        rebuild_from=datetime.fromtimestamp(rows[20]["ts"], tz=UTC),
    )
    rebuilding = reader.read(
        worker_id=_WORKER_ID,
        strategy_id=_STRATEGY_ID,
        symbols=[_SYMBOL],
        source=_SOURCE,
        timeframe=_TIMEFRAME,
        asset_class="crypto",
        max_outbox_age_seconds=300,
        max_strategy_lag_seconds=30,
    )
    assert rebuilding.feed_lags[0].reason == "historical_rebuild_pending"


def _real_candles(
    *,
    instr_id: int,
    rows: list[dict[str, Any]],
) -> list[CandleRow]:
    return [
        CandleRow(
            instr_id=instr_id,
            ts=datetime.fromtimestamp(row["ts"], tz=UTC),
            timeframe=_TIMEFRAME,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            source=_SOURCE,
        )
        for row in rows
    ]


def _clear_postgres_runtime_rows(session_factory: Any) -> None:
    """Remove only deterministic rows owned by this integration test."""
    with session_factory() as session:
        session.execute(
            delete(ExecutionDecisionLog).where(ExecutionDecisionLog.user_id.in_(_TENANT_IDS))
        )
        session.execute(delete(OutboxEvent).where(OutboxEvent.ordering_key == _WORKER_ID))
        session.execute(delete(StrategyDecision).where(StrategyDecision.worker_id == _WORKER_ID))
        session.execute(
            delete(StrategyRuntimeState).where(StrategyRuntimeState.worker_id == _WORKER_ID)
        )
        session.execute(delete(ConsumerWatermark).where(ConsumerWatermark.worker_id == _WORKER_ID))
        session.execute(delete(User).where(User.user_id.in_(_TENANT_IDS)))
        session.commit()


@pytest.mark.integration
def test_postgres_runtime_crash_recovery_and_tenant_independence(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise every durable-runtime recovery boundary on real Coinbase bars."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the PostgreSQL durable-runtime gate")
    engine = create_engine_for_env(db_url=database_url)
    if engine.dialect.name != "postgresql":
        dispose_engine(engine)
        pytest.skip("PostgreSQL durable-runtime gate requires a PostgreSQL DATABASE_URL")
    required_tables = {
        "strategy_runtime_states",
        "strategy_decisions",
        "outbox_events",
        "watermarks",
    }
    missing_tables = required_tables - set(inspect(engine).get_table_names())
    if missing_tables:
        dispose_engine(engine)
        pytest.fail(
            "PostgreSQL durable-runtime gate requires migrations through 0075; "
            f"missing={sorted(missing_tables)!r}"
        )

    session_factory = get_session_factory(engine=engine)
    rows = _public_rows()
    created_strategy = False
    created_instrument = False
    created_price_ids: set[int] = set()
    preexisting_price_ids: set[int] = set()
    instr_id: int | None = None
    try:
        _clear_postgres_runtime_rows(session_factory)
        with session_factory() as session:
            strategy = session.get(Strategy, _STRATEGY_ID)
            if strategy is None:
                strategy = Strategy(
                    strategy_id=_STRATEGY_ID,
                    strategy_name="EMA Cross Scalper",
                    asset_class="crypto",
                    is_active=True,
                )
                session.add(strategy)
                created_strategy = True

            candidates = session.scalars(
                select(Instrument).where(
                    Instrument.asset_class == "crypto",
                    Instrument.canonical.in_(["BTCUSD", "BTC/USD"]),
                )
            ).all()
            if len(candidates) > 1:
                pytest.fail(
                    "PostgreSQL durable-runtime gate requires one canonical BTCUSD instrument"
                )
            if candidates:
                instrument = candidates[0]
            else:
                instrument = Instrument(
                    asset_class="crypto",
                    canonical=_SYMBOL,
                    exchange="coinbase",
                    settlement_currency="USD",
                    is_tradable=True,
                    market_session_policy="continuous",
                )
                session.add(instrument)
                created_instrument = True
            session.commit()
            session.refresh(instrument)
            instr_id = int(instrument.instr_id)
            preexisting_price_ids = set(
                session.scalars(
                    select(InstrumentPrice.price_id).where(
                        InstrumentPrice.instr_id == instr_id,
                        InstrumentPrice.source == _SOURCE,
                        InstrumentPrice.timeframe == _TIMEFRAME,
                    )
                ).all()
            )

        ingestion = PriceIngestionService(session_factory)
        assert (
            ingestion.upsert_candles(
                _real_candles(
                    instr_id=instr_id,
                    rows=rows[:_ENTRY_INDEX],
                )
            )
            == _ENTRY_INDEX
        )
        with session_factory() as session:
            current_price_ids = set(
                session.scalars(
                    select(InstrumentPrice.price_id).where(
                        InstrumentPrice.instr_id == instr_id,
                        InstrumentPrice.source == _SOURCE,
                        InstrumentPrice.timeframe == _TIMEFRAME,
                    )
                ).all()
            )
        created_price_ids.update(current_price_ids - preexisting_price_ids)

        worker, _delivery, store, _outbox = _build_worker(session_factory)
        worker.start()
        initial_checkpoint = worker._watermark.get(_SYMBOL, _TIMEFRAME)
        assert (
            ingestion.upsert_candles(_real_candles(instr_id=instr_id, rows=[rows[_ENTRY_INDEX]]))
            == 1
        )
        with session_factory() as session:
            created_price_ids.update(
                set(
                    session.scalars(
                        select(InstrumentPrice.price_id).where(
                            InstrumentPrice.instr_id == instr_id,
                            InstrumentPrice.source == _SOURCE,
                            InstrumentPrice.timeframe == _TIMEFRAME,
                        )
                    ).all()
                )
                - preexisting_price_ids
            )

        enqueue = store._outbox.enqueue_on_session

        def _crash_after_enqueue(*args: Any, **kwargs: Any) -> str:
            event_id = enqueue(*args, **kwargs)
            raise RuntimeError("injected PostgreSQL crash before commit")

        with monkeypatch.context() as crash:
            crash.setattr(store._outbox, "enqueue_on_session", _crash_after_enqueue)
            with pytest.raises(RuntimeError, match="PostgreSQL crash"):
                worker._catchup_symbol(_SYMBOL)
        with session_factory() as session:
            state = session.get(StrategyRuntimeState, _WORKER_ID)
            watermark = session.get(
                ConsumerWatermark,
                (_WORKER_ID, _SYMBOL, _TIMEFRAME),
            )
            assert state is not None
            assert watermark is not None
            assert state.state_payload["symbol_states"][_SYMBOL]["position"] == 0
            assert watermark.last_ts == initial_checkpoint.replace(tzinfo=None)
            assert (
                session.scalar(
                    select(StrategyDecision).where(StrategyDecision.worker_id == _WORKER_ID)
                )
                is None
            )
            assert (
                session.scalar(select(OutboxEvent).where(OutboxEvent.ordering_key == _WORKER_ID))
                is None
            )

        recovered, delivered, _store, _outbox = _build_worker(session_factory)
        recovered.start()
        recovered._catchup_symbol(_SYMBOL)
        entry_signals = delivered.get_signals()
        assert [signal.action for signal in entry_signals] == [SignalAction.LONG]
        external_signal_id = entry_signals[0].external_signal_id
        assert external_signal_id
        operational = StrategyOperationalStatusReader(session_factory).read(
            worker_id=_WORKER_ID,
            strategy_id=_STRATEGY_ID,
            symbols=[_SYMBOL],
            source=_SOURCE,
            timeframe=_TIMEFRAME,
            asset_class="crypto",
            max_outbox_age_seconds=300,
            max_strategy_lag_seconds=30,
        )
        assert operational.ready is True
        assert operational.feed_lags[0].lag_seconds == 0

        # Shared runtime persistence has no tenant/account key. Two downstream
        # users can record opposite outcomes without rewriting that model.
        assert {
            "user_id",
            "account_id",
            "broker_account_id",
        }.isdisjoint(StrategyRuntimeState.__table__.columns.keys())
        with session_factory() as session:
            before_state = session.get(StrategyRuntimeState, _WORKER_ID)
            assert before_state is not None
            before_snapshot = json.dumps(before_state.state_payload, sort_keys=True)
            before_generation = int(before_state.generation)
            for user_id, status, should_execute in (
                (_TENANT_IDS[0], "executed", True),
                (_TENANT_IDS[1], "rejected", False),
            ):
                session.add(
                    User(
                        user_id=user_id,
                        email=f"{user_id}@example.invalid",
                        full_name=user_id,
                        tz="UTC",
                        base_ccy="USD",
                        status="active",
                    )
                )
                session.flush()
                session.add(
                    ExecutionDecisionLog(
                        user_id=user_id,
                        instr_id=instr_id,
                        should_execute=should_execute,
                        execution_mode="spot",
                        direction="long",
                        status=status,
                        idempotency_key=hashlib.sha256(
                            f"{user_id}|{external_signal_id}".encode()
                        ).hexdigest(),
                        signal_id=external_signal_id,
                        action="LONG",
                    )
                )
            session.commit()
        with session_factory() as session:
            after_outcomes = session.get(StrategyRuntimeState, _WORKER_ID)
            assert after_outcomes is not None
            assert json.dumps(after_outcomes.state_payload, sort_keys=True) == before_snapshot
            assert int(after_outcomes.generation) == before_generation

        assert (
            ingestion.upsert_candles(
                _real_candles(
                    instr_id=instr_id,
                    rows=[rows[_ENTRY_INDEX + 1]],
                )
            )
            == 1
        )
        recovered._catchup_symbol(_SYMBOL)
        with session_factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(StrategyDecision)
                    .where(StrategyDecision.worker_id == _WORKER_ID)
                )
                == 2
            )
            state = session.get(StrategyRuntimeState, _WORKER_ID)
            assert state is not None
            assert int(state.generation) == before_generation + 1
            assert state.state_payload["symbol_states"][_SYMBOL]["position"] == 1

        # A config fingerprint mismatch fails before the next real bar advances.
        assert (
            ingestion.upsert_candles(
                _real_candles(
                    instr_id=instr_id,
                    rows=[rows[_ENTRY_INDEX + 2]],
                )
            )
            == 1
        )
        incompatible, incompatible_delivery, _store, _outbox = _build_worker(
            session_factory,
            config_overrides={"confidence": "0.71"},
        )
        checkpoint = recovered._watermark.get(_SYMBOL, _TIMEFRAME)
        with pytest.raises(
            ModelStateContractError,
            match="Incompatible durable runtime state",
        ):
            incompatible.start()
        assert incompatible_delivery.get_signals() == []
        assert incompatible._watermark.get(_SYMBOL, _TIMEFRAME) == checkpoint

        # Rebuild the same real observations and prove snapshot+generation
        # acknowledgement rollback together at the PostgreSQL transaction.
        recovered._watermark.request_rebuilds(
            [(_SYMBOL, _TIMEFRAME, instr_id)],
            rebuild_from=datetime.fromtimestamp(rows[20]["ts"], tz=UTC),
        )
        rebuilding, rebuilding_delivery, rebuild_store, _outbox = _build_worker(session_factory)
        persist_rebuild = rebuild_store.persist_rebuild_on_session

        def _crash_after_rebuild(
            *args: Any,
            **kwargs: Any,
        ) -> StrategyRuntimeState:
            state = persist_rebuild(*args, **kwargs)
            raise RuntimeError("injected PostgreSQL rebuild commit crash")

        with monkeypatch.context() as rebuild_crash:
            rebuild_crash.setattr(
                rebuild_store,
                "persist_rebuild_on_session",
                _crash_after_rebuild,
            )
            with pytest.raises(RuntimeError, match="rebuild commit crash"):
                rebuilding.start()
        assert rebuilding_delivery.get_signals() == []
        with session_factory() as session:
            pending = session.get(
                ConsumerWatermark,
                (_WORKER_ID, _SYMBOL, _TIMEFRAME),
            )
            assert pending is not None
            assert pending.rebuild_from_ts is not None
        rebuilt, rebuilt_delivery, _store, _outbox = _build_worker(session_factory)
        rebuilt.start()
        assert rebuilt_delivery.get_signals() == []
        assert rebuilt._strategy.state_for(_SYMBOL).position == 1
        with session_factory() as session:
            acknowledged = session.get(
                ConsumerWatermark,
                (_WORKER_ID, _SYMBOL, _TIMEFRAME),
            )
            assert acknowledged is not None
            assert acknowledged.rebuild_from_ts is None

        # The real 01:22 close is accepted, then the simulated mark crash leaves
        # it leased. Restart replays exactly the same external identity.
        assert (
            ingestion.upsert_candles(
                _real_candles(
                    instr_id=instr_id,
                    rows=rows[_ENTRY_INDEX + 3 : _CLOSE_INDEX + 1],
                )
            )
            == _CLOSE_INDEX - _ENTRY_INDEX - 2
        )
        close_delivery = BacktestSignalEmitter()
        closing, _delivery, _store, close_outbox = _build_worker(
            session_factory,
            delivery_emitter=close_delivery,
            lease_seconds=1,
        )
        closing.start()

        def _crash_after_scoring(*_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("injected PostgreSQL crash after scoring acknowledgement")

        with monkeypatch.context() as mark_crash:
            mark_crash.setattr(close_outbox, "mark_published", _crash_after_scoring)
            with pytest.raises(RuntimeError, match="after scoring acknowledgement"):
                closing._catchup_symbol(_SYMBOL)
        first_close = close_delivery.get_signals()
        assert [signal.action for signal in first_close] == [SignalAction.CLOSE]
        with session_factory() as session:
            close_row = session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.event_key == f"strategy-signal:{first_close[0].external_signal_id}"
                )
            )
            assert close_row is not None
            assert close_row.status == "in_progress"
            close_row.claimed_at = datetime.now(tz=UTC) - timedelta(seconds=2)
            session.commit()

        replay_delivery = BacktestSignalEmitter()
        replayed, _delivery, _store, _outbox = _build_worker(
            session_factory,
            delivery_emitter=replay_delivery,
            lease_seconds=1,
        )
        replayed.start()
        assert len(replay_delivery.get_signals()) == 1
        assert (
            replay_delivery.get_signals()[0].external_signal_id == first_close[0].external_signal_id
        )
    finally:
        try:
            _clear_postgres_runtime_rows(session_factory)
            with session_factory() as session:
                if instr_id is not None:
                    current_price_ids = set(
                        session.scalars(
                            select(InstrumentPrice.price_id).where(
                                InstrumentPrice.instr_id == instr_id,
                                InstrumentPrice.source == _SOURCE,
                                InstrumentPrice.timeframe == _TIMEFRAME,
                            )
                        ).all()
                    )
                    created_price_ids.update(current_price_ids - preexisting_price_ids)
                if created_price_ids:
                    session.execute(
                        delete(InstrumentPrice).where(
                            InstrumentPrice.price_id.in_(created_price_ids)
                        )
                    )
                if created_strategy:
                    session.execute(delete(Strategy).where(Strategy.strategy_id == _STRATEGY_ID))
                if created_instrument and instr_id is not None:
                    session.execute(delete(Instrument).where(Instrument.instr_id == instr_id))
                session.commit()
        finally:
            dispose_engine(engine)


_NATIVE_DIR = _REPO / "strategies/indicator/TimeDecayAdaptiveEMA"


def _seed_native_catalogue(factory):
    instr_id = _seed_catalogue(factory)
    with factory() as session:
        session.add(
            Strategy(
                strategy_id="time_decay_adaptive_ema_v1",
                strategy_name="TimeDecayAdaptiveEMA",
                asset_class="crypto",
                is_active=False,
            )
        )
        session.commit()
    return instr_id


def test_native_restart_restores_partial_bucket_without_recent_history(
    session_factory, monkeypatch
):
    rows = _public_rows()
    instr_id = _seed_native_catalogue(session_factory)
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[:800])
    first, _, _, _ = _build_worker(
        session_factory, strategy_dir=_NATIVE_DIR, consolidation_minutes=5, bootstrap_bars=150
    )
    first.start()
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[800:803])
    first._catchup_symbol(_SYMBOL)
    expected_model = first._strategy.serialize_model_state()
    expected_bucket = first._consolidators[_SYMBOL].snapshot_state()
    assert expected_bucket["working_bar"] is not None

    restarted, delivery, _, _ = _build_worker(
        session_factory, strategy_dir=_NATIVE_DIR, consolidation_minutes=5, bootstrap_bars=1
    )

    def forbidden_recent_history(*args, **kwargs):
        pytest.fail(
            "Exact restart must restore the durable streams before any recent-history bootstrap"
        )

    monkeypatch.setattr(restarted, "_fetch_historical_bars", forbidden_recent_history)
    restarted.start()
    assert restarted._strategy.serialize_model_state() == expected_model
    assert restarted._consolidators[_SYMBOL].snapshot_state() == expected_bucket

    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[803:850])
    # Continue the original in-memory state without changing its already shared
    # durable journal, then compare the recovered worker's actual journaled path.
    raw = first._fetch_historical_range(
        _SYMBOL,
        start=datetime.fromtimestamp(rows[803]["ts"], UTC),
        through=datetime.fromtimestamp(rows[849]["ts"], UTC),
    )
    for bar in raw:
        first._consolidators[_SYMBOL].update(bar)
    restarted._catchup_symbol(_SYMBOL)
    assert restarted._strategy.serialize_model_state() == first._strategy.serialize_model_state()
    assert (
        restarted._consolidators[_SYMBOL].snapshot_state()
        == first._consolidators[_SYMBOL].snapshot_state()
    )
    assert [s.external_signal_id for s in delivery.get_signals()] == [
        s.external_signal_id for s in first._signal_buffer.pending()
    ]


def test_native_empty_checkpoint_restores_without_rebootstrap(session_factory, monkeypatch):
    _seed_native_catalogue(session_factory)
    first, _, _, _ = _build_worker(session_factory, strategy_dir=_NATIVE_DIR)
    first.start()
    restarted, _, _, _ = _build_worker(session_factory, strategy_dir=_NATIVE_DIR)

    def forbidden_recent_history(*args, **kwargs):
        pytest.fail("An explicit empty durable checkpoint is still a checkpoint")

    monkeypatch.setattr(restarted, "_fetch_historical_bars", forbidden_recent_history)
    restarted.start()
    assert not restarted._strategy.warmup_complete()


def test_native_correction_preserves_original_flat_boundary_and_recursive_origin(session_factory):
    rows = _public_rows()
    instr_id = _seed_native_catalogue(session_factory)
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[:600])
    first, _, _, _ = _build_worker(
        session_factory,
        strategy_dir=_NATIVE_DIR,
        bootstrap_bars=100,
        config_overrides={"atr_multiplier": "100"},
    )
    first.start()
    assert first._strategy.state_for(_SYMBOL).position == 0
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[600:900])
    first._catchup_symbol(_SYMBOL)
    expected = first._strategy.serialize_model_state()
    with session_factory() as session:
        before_decisions = session.scalar(select(func.count()).select_from(StrategyDecision))
        before_outbox = session.scalar(select(func.count()).select_from(OutboxEvent))
    first._watermark.request_rebuilds(
        [(_SYMBOL, _TIMEFRAME, instr_id)], rebuild_from=datetime.fromtimestamp(rows[500]["ts"], UTC)
    )
    rebuilt, delivery, _, _ = _build_worker(
        session_factory,
        strategy_dir=_NATIVE_DIR,
        bootstrap_bars=1,
        config_overrides={"atr_multiplier": "100"},
    )
    rebuilt.start()
    assert rebuilt._strategy.serialize_model_state() == expected
    assert delivery.get_signals() == []
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(StrategyDecision)) == before_decisions
        )
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == before_outbox
    assert not rebuilt._watermark.get_state(_SYMBOL, _TIMEFRAME).rebuild_pending


@pytest.mark.parametrize("consolidation", [0, 5])
def test_native_checkpoint_cannot_lose_a_flat_indicator_stream(session_factory, consolidation):
    rows = _public_rows()
    instr_id = _seed_native_catalogue(session_factory)
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[:300])
    first, _, _, _ = _build_worker(
        session_factory, strategy_dir=_NATIVE_DIR, consolidation_minutes=consolidation
    )
    first.start()
    with session_factory() as session:
        row = session.get(StrategyRuntimeState, _WORKER_ID)
        payload = json.loads(json.dumps(row.state_payload))
        assert payload["symbol_states"][_SYMBOL]["position"] == 0
        del payload["streams"]["symbols"][_SYMBOL]
        row.state_payload = payload
        session.commit()
    restarted, _, _, _ = _build_worker(
        session_factory, strategy_dir=_NATIVE_DIR, consolidation_minutes=consolidation
    )
    with pytest.raises(ModelStateContractError, match="stream"):
        restarted.start()


def test_native_restore_checks_the_other_symbols_empty_watermark(session_factory):
    rows = _public_rows()
    instr_id = _seed_native_catalogue(session_factory)
    with session_factory() as session:
        session.add(Instrument(asset_class="crypto", canonical="ETHUSD", settlement_currency="USD"))
        session.commit()
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[:30])
    first, _, _, _ = _build_worker(
        session_factory, strategy_dir=_NATIVE_DIR, symbols=[_SYMBOL, "ETHUSD"]
    )
    first.start()
    with session_factory() as session:
        other = session.scalar(
            select(ConsumerWatermark).where(
                ConsumerWatermark.worker_id == _WORKER_ID, ConsumerWatermark.symbol == "ETHUSD"
            )
        )
        other.last_ts = datetime.fromtimestamp(rows[5]["ts"], UTC).replace(tzinfo=None)
        session.commit()
    restarted, _, _, _ = _build_worker(
        session_factory, strategy_dir=_NATIVE_DIR, symbols=[_SYMBOL, "ETHUSD"]
    )
    with pytest.raises(ModelStateContractError, match="watermark"):
        restarted.start()


@pytest.mark.parametrize("rebuild", [False, True])
def test_native_restore_requires_its_recorded_inception_row(session_factory, rebuild):
    rows = _public_rows()
    instr_id = _seed_native_catalogue(session_factory)
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[:100])
    first, _, _, _ = _build_worker(session_factory, strategy_dir=_NATIVE_DIR)
    first.start()
    with session_factory() as session:
        session.execute(
            delete(InstrumentPrice).where(
                InstrumentPrice.instr_id == instr_id,
                InstrumentPrice.ts
                == datetime.fromtimestamp(rows[0]["ts"], UTC).replace(tzinfo=None),
            )
        )
        session.commit()
    if rebuild:
        first._watermark.request_rebuilds(
            [(_SYMBOL, _TIMEFRAME, instr_id)],
            rebuild_from=datetime.fromtimestamp(rows[0]["ts"], UTC),
        )
    restarted, _, _, _ = _build_worker(session_factory, strategy_dir=_NATIVE_DIR)
    with pytest.raises(ModelStateContractError, match="anchor"):
        restarted.start()


def test_native_empty_feed_establishes_a_flat_boundary_when_history_arrives(session_factory):
    rows = _public_rows()
    instr_id = _seed_native_catalogue(session_factory)
    first, _, _, _ = _build_worker(session_factory, strategy_dir=_NATIVE_DIR)
    first.start()
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[:100])
    first._watermark.request_rebuilds(
        [(_SYMBOL, _TIMEFRAME, instr_id)], rebuild_from=datetime.fromtimestamp(rows[0]["ts"], UTC)
    )
    restarted, delivery, _, _ = _build_worker(session_factory, strategy_dir=_NATIVE_DIR)
    restarted.start()
    assert restarted._strategy.warmup_complete(_SYMBOL)
    assert restarted._strategy.state_for(_SYMBOL).position == 0
    assert delivery.get_signals() == []
    with session_factory() as session:
        feed = session.get(StrategyRuntimeState, _WORKER_ID).state_payload["bar_runtime"]["feeds"][
            _SYMBOL
        ]
        assert (
            feed["inception"]["timestamp"] == datetime.fromtimestamp(rows[0]["ts"], UTC).isoformat()
        )
        assert (
            feed["bootstrap_through"]["timestamp"]
            == datetime.fromtimestamp(rows[99]["ts"], UTC).isoformat()
        )


def test_native_bootstrap_read_error_cannot_be_persisted_as_empty_history(
    session_factory, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    _seed_native_catalogue(session_factory)
    worker, _, _, _ = _build_worker(session_factory, strategy_dir=_NATIVE_DIR)

    def fail_read(*args, **kwargs):
        raise OperationalError("SELECT prices", {}, OSError("unavailable"))

    monkeypatch.setattr(worker._ingestion_service, "fetch_recent_bars", fail_read)
    with pytest.raises(OperationalError):
        worker.start()
    with session_factory() as session:
        assert session.get(StrategyRuntimeState, _WORKER_ID) is None
        assert session.scalar(select(func.count()).select_from(ConsumerWatermark)) == 0


def test_native_failed_commit_recovers_core_bucket_and_source_anchor_together(
    session_factory, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    rows = _public_rows()
    instr_id = _seed_native_catalogue(session_factory)
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[:100])
    first, _, _, _ = _build_worker(
        session_factory, strategy_dir=_NATIVE_DIR, consolidation_minutes=5
    )
    first.start()
    with session_factory() as session:
        before = json.loads(json.dumps(session.get(StrategyRuntimeState, _WORKER_ID).state_payload))
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[100:101])

    def fail_ack(*args, **kwargs):
        raise OperationalError("UPDATE watermarks", {}, OSError("commit failed"))

    monkeypatch.setattr(first._watermark, "advance_locked", fail_ack)
    with pytest.raises(OperationalError):
        first._catchup_symbol(_SYMBOL)
    assert first.failed
    expected_bucket = first._consolidators[_SYMBOL].snapshot_state()
    with session_factory() as session:
        assert session.get(StrategyRuntimeState, _WORKER_ID).state_payload == before
    restarted, _, _, _ = _build_worker(
        session_factory, strategy_dir=_NATIVE_DIR, consolidation_minutes=5
    )
    restarted.start()
    restarted._catchup_symbol(_SYMBOL)
    assert restarted._consolidators[_SYMBOL].snapshot_state() == expected_bucket
    with session_factory() as session:
        after = session.get(StrategyRuntimeState, _WORKER_ID).state_payload
        assert (
            after["bar_runtime"]["feeds"][_SYMBOL]["acknowledged"]["timestamp"]
            == datetime.fromtimestamp(rows[100]["ts"], UTC).isoformat()
        )
        assert after["streams"] == before["streams"]


def test_native_rebuild_generation_race_cannot_commit_candidate_state(session_factory, monkeypatch):
    from lib_data.watermark import RebuildGenerationChangedError

    rows = _public_rows()
    instr_id = _seed_native_catalogue(session_factory)
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[:100])
    first, _, _, _ = _build_worker(session_factory, strategy_dir=_NATIVE_DIR)
    first.start()
    with session_factory() as session:
        before = json.loads(json.dumps(session.get(StrategyRuntimeState, _WORKER_ID).state_payload))
    boundary = datetime.fromtimestamp(rows[0]["ts"], UTC)
    first._watermark.request_rebuilds([(_SYMBOL, _TIMEFRAME, instr_id)], rebuild_from=boundary)
    rebuilding, delivery, _, _ = _build_worker(session_factory, strategy_dir=_NATIVE_DIR)
    original_rebuild = rebuilding._bar_recovery.rebuild

    def mutate_after_replay(*args, **kwargs):
        result = original_rebuild(*args, **kwargs)
        # A provider mutation increments the generation even when its earliest
        # boundary is unchanged; request_rebuilds only expands that boundary.
        with session_factory() as session:
            watermark = session.scalar(
                select(ConsumerWatermark).where(
                    ConsumerWatermark.worker_id == _WORKER_ID,
                    ConsumerWatermark.symbol == _SYMBOL,
                )
            )
            watermark.rebuild_generation += 1
            session.commit()
        return result

    monkeypatch.setattr(rebuilding._bar_recovery, "rebuild", mutate_after_replay)
    with pytest.raises(RebuildGenerationChangedError):
        rebuilding.start()
    assert delivery.get_signals() == []
    with session_factory() as session:
        assert session.get(StrategyRuntimeState, _WORKER_ID).state_payload == before
    assert first._watermark.get_state(_SYMBOL, _TIMEFRAME).rebuild_pending


def test_native_rebuild_uses_live_trigger_for_a_bucket_from_bootstrap(session_factory):
    rows = _public_rows()
    instr_id = _seed_native_catalogue(session_factory)
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[:129])
    first, delivery, _, _ = _build_worker(
        session_factory,
        strategy_dir=_NATIVE_DIR,
        consolidation_minutes=5,
    )
    first.start()
    assert first._strategy.state_for(_SYMBOL).position == 0
    # Omit row 129: the live row 130 flushes the partial bootstrap bucket,
    # whose last constituent (128) is also the original flat boundary.
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[130:131])
    first._catchup_symbol(_SYMBOL)
    assert [signal.action for signal in delivery.get_signals()] == [SignalAction.SHORT]
    expected = first._strategy.serialize_model_state()
    expected_bucket = first._consolidators[_SYMBOL].snapshot_state()
    first._watermark.request_rebuilds(
        [(_SYMBOL, _TIMEFRAME, instr_id)],
        rebuild_from=datetime.fromtimestamp(rows[0]["ts"], UTC),
    )
    rebuilt, replay_delivery, _, _ = _build_worker(
        session_factory,
        strategy_dir=_NATIVE_DIR,
        consolidation_minutes=5,
    )
    rebuilt.start()
    assert rebuilt._strategy.serialize_model_state() == expected
    assert rebuilt._consolidators[_SYMBOL].snapshot_state() == expected_bucket
    assert replay_delivery.get_signals() == []


def test_native_restore_rejects_a_dropped_partial_bucket(session_factory):
    rows = _public_rows()
    instr_id = _seed_native_catalogue(session_factory)
    _insert_public_rows(session_factory, instr_id=instr_id, rows=rows[:803])
    first, _, _, _ = _build_worker(
        session_factory,
        strategy_dir=_NATIVE_DIR,
        consolidation_minutes=5,
    )
    first.start()
    with session_factory() as session:
        row = session.get(StrategyRuntimeState, _WORKER_ID)
        payload = json.loads(json.dumps(row.state_payload))
        bucket = payload["bar_runtime"]["feeds"][_SYMBOL]["consolidator"]
        assert bucket["bar_count"] == 3
        bucket.update(working_bar=None, current_period_end=None, bar_count=0)
        row.state_payload = payload
        session.commit()
    restarted, _, _, _ = _build_worker(
        session_factory,
        strategy_dir=_NATIVE_DIR,
        consolidation_minutes=5,
    )
    with pytest.raises(ModelStateContractError, match="partial bucket"):
        restarted.start()
    assert not restarted._strategy.warmup_complete()
