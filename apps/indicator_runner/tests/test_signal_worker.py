"""Cold-start, NOTIFY race, and watermark recovery for ``SignalWorker``.

These tests pin down the DB-fed runtime behaviour that the indicator
pipeline depends on — specifically the parts that are hard to exercise
end-to-end without standing up Postgres + LISTEN/NOTIFY:

* **Cold start** — bootstrapping with N historical bars feeds the
  strategy via ``bootstrap_history`` and parks the watermark on the
  latest bar timestamp.
* **Empty cold start** — bootstrapping with no historical bars leaves
  ``warmed_up=False`` and does not crash.
* **NOTIFY catch-up** — after a NOTIFY arrives, ``_on_notify`` fetches
  bars strictly newer than the watermark and processes them via
  ``on_data``.
* **Watermark recovery** — restart bootstrap stops at the last acknowledged
  bar, and newer rows remain on the delivery-bearing catch-up path.

Notes
-----
Every worker is constructed with the mandatory durable runtime trio
(``BufferedSignalEmitter`` + ``StrategyRuntimeStore`` + ``DurableSignalRelay``)
over the same in-memory SQLite database, matching production wiring. The
worker is constructed with ``dsn=""`` so the LISTEN thread is never started;
all NOTIFY paths are exercised by calling ``_on_notify`` directly.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from indicator_runner import signal_worker as signal_worker_module
from indicator_runner.runtime_journal import (
    BufferedSignalEmitter,
    DurableSignalRelay,
    StrategyRuntimeIdentity,
    StrategyRuntimeStore,
)
from indicator_runner.signal_worker import SignalWorker, parse_strategy_symbols
from lib_application.db.models import (
    Base,
    ConsumerWatermark,
    Instrument,
    InstrumentPrice,
    OutboxEvent,
    Strategy,
    StrategyRuntimeState,
    User,
)
from lib_application.outbox import OutboxStore
from lib_data.bars import Bar
from lib_data.watermark import (
    HistoricalRebuildRequiredError,
    RebuildGenerationChangedError,
)
from lib_strategy.signals.emitter import BacktestSignalEmitter, SignalEmitter
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import Signal, SignalAction

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BTCUSDC, ETHUSDC,BTCUSDC", ["BTCUSDC", "ETHUSDC"]),
        (["btcusdc", " ETHUSDC "], ["BTCUSDC", "ETHUSDC"]),
    ],
)
def test_parse_symbols_accepts_every_schema_valid_universe_shape(
    raw: str | list[str], expected: list[str]
) -> None:
    assert parse_strategy_symbols(raw) == expected


@pytest.mark.parametrize("raw", [None, 12])
def test_parse_symbols_rejects_invalid_universe_type(raw: Any) -> None:
    with pytest.raises(TypeError, match="universe"):
        parse_strategy_symbols(raw)


@pytest.mark.parametrize("raw", [[], " , "])
def test_parse_symbols_rejects_empty_universe(raw: list[str] | str) -> None:
    with pytest.raises(ValueError, match="universe"):
        parse_strategy_symbols(raw)


class _RecordingStrategy(PureSignalStrategy):
    """Strategy double that records every framework call for assertions.

    ``warmup_bars_needed`` is configurable so we can exercise both warm
    and cold strategies without adding indicator wiring.
    """

    def __init__(self, *, warmup_needed: int = 0) -> None:
        # ``strategy_version`` is mandatory for the durable runtime identity.
        super().__init__(
            strategy_id="test_pmo",
            strategy_type="indicator",
            config={"strategy_version": "1.0.0"},
        )
        self._warmup_bars_needed = warmup_needed
        self.bootstrap_calls: list[list[MarketState]] = []
        self.on_data_calls: list[MarketState] = []

    def initialize(self) -> None:  # pragma: no cover - trivial
        return None

    def on_data(self, state: MarketState) -> None:
        self.on_data_calls.append(state)

    def bootstrap_history(self, bars: list[MarketState]) -> None:
        self.bootstrap_calls.append(list(bars))
        super().bootstrap_history(bars)


@pytest.fixture
def session_factory() -> sessionmaker:
    """Return a session factory bound to a fresh in-memory sqlite database."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        # Single connection so all sessions share the same in-memory DB.
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, future=True)

    @contextmanager
    def _ctx_factory() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    # SignalWorker calls ``self._session_factory()`` and uses the result as
    # a context manager, so wrap to provide ``__enter__/__exit__``.
    return _ctx_factory  # type: ignore[return-value]


def _seed_instrument(factory: Any, *, symbol: str = "BTCUSD") -> int:
    """Insert a single crypto instrument and return its id."""
    with factory() as session:
        settlement_currency = "USDC" if symbol.replace("-", "").endswith("USDC") else "USD"
        inst = Instrument(
            asset_class="crypto",
            canonical=symbol,
            settlement_currency=settlement_currency,
        )
        session.add(inst)
        session.commit()
        session.refresh(inst)
        return int(inst.instr_id)


def _seed_bars(
    factory: Any,
    *,
    instr_id: int,
    count: int,
    start: datetime,
    timeframe: str = "1m",
    source: str = "coinbase_live",
) -> list[datetime]:
    """Insert ``count`` consecutive bars at one-minute intervals.

    Returns the list of inserted timestamps oldest → newest.
    """
    timestamps: list[datetime] = []
    with factory() as session:
        for i in range(count):
            ts = (start + timedelta(minutes=i)).replace(tzinfo=None)
            session.add(
                InstrumentPrice(
                    instr_id=instr_id,
                    ts=ts,
                    timeframe=timeframe,
                    source=source,
                    open=100 + i,
                    high=101 + i,
                    low=99 + i,
                    close=100 + i,
                    volume=10,
                )
            )
            timestamps.append(ts)
        session.commit()
    return timestamps


def _mark_historical_change(
    factory: Any,
    *,
    instr_id: int,
    timestamp: datetime,
    symbol: str = "BTCUSD",
) -> None:
    """Commit a corrected source row and its durable rebuild request."""
    changed_at = timestamp.replace(tzinfo=None)
    with factory() as session:
        price = session.query(InstrumentPrice).filter_by(instr_id=instr_id, ts=changed_at).one()
        price.close = float(price.close) + 0.25
        price.content_revision += 1
        watermark = (
            session.query(ConsumerWatermark)
            .filter_by(worker_id="test-worker", symbol=symbol, timeframe="1m")
            .one()
        )
        watermark.rebuild_from_ts = changed_at
        watermark.rebuild_generation += 1
        session.commit()


def _ensure_strategy_row(factory: Any, *, strategy_id: str) -> None:
    """Idempotently satisfy the strategy-catalogue FK for durable runtime rows."""
    with factory() as session:
        if session.get(Strategy, strategy_id) is None:
            session.add(
                Strategy(
                    strategy_id=strategy_id,
                    strategy_name="Signal Worker Test Strategy",
                    asset_class="crypto",
                    is_active=True,
                )
            )
            session.commit()


def _build_worker(
    session_factory: Any,
    strategy: PureSignalStrategy,
    *,
    bootstrap_bars: int = 10,
    consolidation_minutes: int = 0,
    timeframe: str = "1m",
    source: str = "coinbase_live",
    symbols: list[str] | None = None,
    catchup_batch_size: int = 5000,
    clock: Callable[[], datetime] | None = None,
    delivery_emitter: SignalEmitter | None = None,
    delivery_loop: Any | None = None,
) -> SignalWorker:
    """Construct a durable worker with no DSN (no LISTEN thread).

    Mirrors production wiring: the strategy emits into the transaction-local
    ``BufferedSignalEmitter`` and the worker owns the mandatory durable trio
    (buffer + ``StrategyRuntimeStore`` + ``DurableSignalRelay``). Tests that
    assert on delivered signals pass their own ``delivery_emitter``.
    """
    resolved_symbols = symbols or ["BTCUSD"]
    _ensure_strategy_row(session_factory, strategy_id=strategy.strategy_id)
    signal_buffer = BufferedSignalEmitter()
    # The worker's transaction buffer must be the strategy's emitter so each
    # bar's emissions commit atomically with model state and watermark.
    strategy._emitter = signal_buffer
    identity = StrategyRuntimeIdentity.from_strategy(
        worker_id="test-worker",
        strategy=strategy,
        symbols=resolved_symbols,
        source=source,
        timeframe=timeframe,
        consolidation_minutes=consolidation_minutes,
    )
    return SignalWorker(
        strategy=strategy,
        session_factory=session_factory,
        worker_id="test-worker",
        symbols=resolved_symbols,
        consolidation_minutes=consolidation_minutes,
        dsn="",  # Disables LISTEN/NOTIFY thread.
        bootstrap_bars=bootstrap_bars,
        source=source,
        timeframe=timeframe,
        catchup_batch_size=catchup_batch_size,
        clock=clock,
        signal_buffer=signal_buffer,
        runtime_store=StrategyRuntimeStore(session_factory, identity),
        signal_relay=DurableSignalRelay(
            outbox=OutboxStore(session_factory),
            emitter=delivery_emitter or BacktestSignalEmitter(),
            worker_id="test-worker",
            strategy_id=strategy.strategy_id,
        ),
        delivery_loop=delivery_loop,
    )


# ---------------------------------------------------------------------------
# Cold start — full history
# ---------------------------------------------------------------------------


def test_cold_start_bootstraps_all_available_bars(session_factory: Any) -> None:
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    timestamps = _seed_bars(session_factory, instr_id=instr_id, count=10, start=base)

    strategy = _RecordingStrategy()
    worker = _build_worker(session_factory, strategy, bootstrap_bars=20)
    worker.start()

    # All 10 bars feed ``bootstrap_history`` once, oldest-first.
    assert len(strategy.bootstrap_calls) == 1
    bootstrap_states = strategy.bootstrap_calls[0]
    assert [s.timestamp for s in bootstrap_states] == [ts.replace(tzinfo=UTC) for ts in timestamps]
    assert all(s.symbol == "BTCUSD" for s in bootstrap_states)

    # Watermark is parked on the latest bar so a subsequent NOTIFY only
    # picks up strictly-newer bars.
    parked = worker._watermark.get("BTCUSD", "1m")  # type: ignore[attr-defined]
    assert parked.replace(tzinfo=None) == timestamps[-1].replace(tzinfo=None)

    # The same transaction journaled the initial model snapshot with the exact
    # source identity of the acknowledged bar.
    with session_factory() as session:
        runtime_row = session.get(StrategyRuntimeState, "test-worker")
        assert runtime_row is not None
        assert runtime_row.last_source_ts == timestamps[-1]


def test_bootstrap_replay_emissions_never_reach_the_durable_outbox(
    session_factory: Any,
) -> None:
    """Startup replay is state reconstruction; even CLOSE stays suppressed.

    The legacy runtime let reduce-only exits escape during bootstrap. The
    durable runtime restores the exact journaled model snapshot instead, so
    every bootstrap-time emission is suppressed and no envelope may appear in
    the transaction buffer or the outbox.
    """
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    _seed_bars(session_factory, instr_id=instr_id, count=3, start=base)

    class _ClosingStrategy(_RecordingStrategy):
        def on_data(self, state: MarketState) -> None:
            super().on_data(state)
            self.emit_close(state.symbol, exit_price=state.close, timestamp=state.timestamp)

    worker = _build_worker(session_factory, _ClosingStrategy(), bootstrap_bars=10)
    worker.start()

    assert worker._signal_buffer.pending() == ()  # type: ignore[attr-defined]
    with session_factory() as session:
        assert session.query(OutboxEvent).count() == 0


def test_cold_start_with_no_history_does_not_crash(session_factory: Any) -> None:
    """An empty ``prices`` table must not raise during bootstrap."""
    instr_id = _seed_instrument(session_factory)

    strategy = _RecordingStrategy(warmup_needed=5)
    worker = _build_worker(session_factory, strategy, bootstrap_bars=20)
    worker.start()

    # Strategy received no bars; ``warmed_up`` reflects strategy state.
    assert strategy.bootstrap_calls == []  # bootstrap_history never invoked
    assert strategy.on_data_calls == []
    assert worker._warmed_up is False  # type: ignore[attr-defined]


def test_cold_start_rejects_missing_ohlcv_before_bootstrap_or_watermark(
    session_factory: Any,
) -> None:
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    _seed_bars(session_factory, instr_id=instr_id, count=1, start=base)
    with session_factory() as session:
        row = session.query(InstrumentPrice).filter_by(instr_id=instr_id).one()
        row.open = None
        session.commit()

    strategy = _RecordingStrategy()
    worker = _build_worker(session_factory, strategy)
    before = worker._watermark.get("BTCUSD", "1m")  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="missing required OHLCV"):
        worker.start()

    assert strategy.bootstrap_calls == []
    assert worker._watermark.get("BTCUSD", "1m") == before  # type: ignore[attr-defined]


def test_signal_worker_fails_closed_without_durable_watermark_table(
    session_factory: Any,
) -> None:
    _seed_instrument(session_factory)
    with session_factory() as session:
        session.execute(text("DROP TABLE watermarks"))
        session.commit()

    worker = _build_worker(session_factory, _RecordingStrategy())

    with pytest.raises(RuntimeError, match="Durable watermarks table is unavailable"):
        worker.start()


# ---------------------------------------------------------------------------
# NOTIFY catch-up
# ---------------------------------------------------------------------------


def test_notify_catchup_processes_only_bars_newer_than_watermark(
    session_factory: Any,
) -> None:
    """NOTIFY → ``_on_notify`` fetches bars strictly newer than watermark."""
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    historical = _seed_bars(session_factory, instr_id=instr_id, count=5, start=base)

    strategy = _RecordingStrategy()
    worker = _build_worker(session_factory, strategy, bootstrap_bars=10)
    worker.start()

    # Now add three new bars after the watermark.
    new_base = base + timedelta(minutes=5)
    new_timestamps = _seed_bars(session_factory, instr_id=instr_id, count=3, start=new_base)

    # Reset on_data_calls to ignore bootstrap-time invocations.
    bootstrap_count = len(strategy.on_data_calls)
    worker._on_notify("new_market_data", {"symbol": "BTCUSD"})  # type: ignore[attr-defined]

    catchup_calls = strategy.on_data_calls[bootstrap_count:]
    assert [s.timestamp for s in catchup_calls] == [ts.replace(tzinfo=UTC) for ts in new_timestamps]
    # Watermark advanced to the newest of the catchup bars.
    parked = worker._watermark.get("BTCUSD", "1m")  # type: ignore[attr-defined]
    assert parked.replace(tzinfo=None) == new_timestamps[-1].replace(tzinfo=None)


def test_catchup_rejects_missing_volume_before_strategy_or_watermark(
    session_factory: Any,
) -> None:
    instr_id = _seed_instrument(session_factory)
    strategy = _RecordingStrategy()
    worker = _build_worker(session_factory, strategy, bootstrap_bars=0)
    worker.start()
    before = worker._watermark.get("BTCUSD", "1m")  # type: ignore[attr-defined]
    _seed_bars(
        session_factory,
        instr_id=instr_id,
        count=1,
        start=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )
    with session_factory() as session:
        row = session.query(InstrumentPrice).filter_by(instr_id=instr_id).one()
        row.volume = None
        session.commit()

    with pytest.raises(ValueError, match="missing required OHLCV"):
        worker._catchup_symbol("BTCUSD")  # type: ignore[attr-defined]

    assert strategy.on_data_calls == []
    assert worker._watermark.get("BTCUSD", "1m") == before  # type: ignore[attr-defined]


def test_live_catchup_adds_processing_timestamp_but_bootstrap_does_not(
    session_factory: Any,
) -> None:
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    historical = _seed_bars(session_factory, instr_id=instr_id, count=2, start=base)
    processed_at = base + timedelta(minutes=3, seconds=7)
    strategy = _RecordingStrategy()
    worker = _build_worker(
        session_factory,
        strategy,
        bootstrap_bars=10,
        clock=lambda: processed_at,
    )
    worker.start()

    assert len(strategy.bootstrap_calls) == 1
    assert all(
        "processing_timestamp" not in state.metadata for state in strategy.bootstrap_calls[0]
    )

    new_timestamp = _seed_bars(
        session_factory,
        instr_id=instr_id,
        count=1,
        start=base + timedelta(minutes=2),
    )[0]
    bootstrap_count = len(strategy.on_data_calls)
    worker._on_notify("new_market_data", {"symbol": "BTCUSD"})  # type: ignore[attr-defined]

    live_states = strategy.on_data_calls[bootstrap_count:]
    assert len(live_states) == 1
    assert live_states[0].timestamp == new_timestamp.replace(tzinfo=UTC)
    assert live_states[0].metadata["processing_timestamp"] == processed_at


class _EmittingStrategy(_RecordingStrategy):
    """Record every processed bar and emit a deterministic LONG for it."""

    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[Signal] = []

    def on_data(self, state: MarketState) -> None:
        super().on_data(state)
        if self._bootstrapping:
            return
        self.emitted.append(
            self.emit_long(
                symbol=state.symbol,
                entry_price=state.close,
                timestamp=state.timestamp,
            )
        )


def test_delivery_failure_stays_in_outbox_and_never_retires_the_worker(
    session_factory: Any,
) -> None:
    """A scoring outage leaves a fenced retryable envelope, not a dead worker.

    Durable semantics: the strategy transaction (model state, decision record,
    signal envelope, source watermark) commits before any HTTP happens, so the
    bar is acknowledged, the worker stays healthy, and the undelivered envelope
    is retried later with exactly the same external identity.
    """
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    request = httpx.Request("POST", "http://scoring-engine:8001/api/v1/signals")

    class _FailingDelivery(SignalEmitter):
        def emit(self, signal: Signal) -> None:
            raise httpx.ConnectError("scoring unavailable", request=request)

        def flush(self) -> None:
            return None

    strategy = _EmittingStrategy()
    worker = _build_worker(
        session_factory,
        strategy,
        bootstrap_bars=0,
        delivery_emitter=_FailingDelivery(),
    )
    worker.start()
    bar_ts = _seed_bars(session_factory, instr_id=instr_id, count=1, start=base)[0]

    worker._catchup_symbol("BTCUSD")  # type: ignore[attr-defined]

    # The bar is acknowledged: durable delivery is decoupled from acceptance.
    parked = worker._watermark.get("BTCUSD", "1m")  # type: ignore[attr-defined]
    assert parked.replace(tzinfo=None) == bar_ts
    assert worker.failed is False
    assert worker.terminal_error is None
    assert len(strategy.on_data_calls) == 1
    expected_signal = strategy.emitted[-1]

    with session_factory() as session:
        event = session.query(OutboxEvent).one()
        assert event.status == "failed"
        assert event.attempts == 1
        assert event.event_key == f"strategy-signal:{expected_signal.external_signal_id}"
        # Collapse the retry backoff so the restarted relay can claim it now.
        event.available_at = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(seconds=5)
        session.commit()

    delivery = BacktestSignalEmitter()
    restarted = _build_worker(
        session_factory,
        _EmittingStrategy(),
        bootstrap_bars=10,
        delivery_emitter=delivery,
    )
    restarted.start()

    assert [signal.external_signal_id for signal in delivery.get_signals()] == [
        expected_signal.external_signal_id
    ]
    with session_factory() as session:
        assert session.query(OutboxEvent).one().status == "published"


def test_processed_bar_metric_counts_only_completed_strategy_bars(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, str]] = []
    monkeypatch.setattr(
        signal_worker_module,
        "_record_processed_bar",
        lambda **labels: observed.append(labels),
    )
    bar = Bar(
        symbol="BTCUSD",
        timestamp=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        timeframe="1m",
        source="coinbase_live",
    )

    worker = _build_worker(session_factory, _RecordingStrategy(), bootstrap_bars=0)
    worker._process_bar(bar)  # type: ignore[attr-defined]

    assert observed == [{"strategy_id": "test_pmo", "timeframe": "1m"}]

    class _FailingStrategy(_RecordingStrategy):
        def on_data(self, state: MarketState) -> None:
            super().on_data(state)
            raise ValueError("strategy failed")

    failing_worker = _build_worker(
        session_factory,
        _FailingStrategy(),
        bootstrap_bars=0,
    )
    with pytest.raises(ValueError, match="strategy failed"):
        failing_worker._process_bar(bar)  # type: ignore[attr-defined]

    assert observed == [{"strategy_id": "test_pmo", "timeframe": "1m"}]


def test_indicator_metrics_use_bounded_strategy_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[tuple[str, dict[str, str], float]] = []

    class _BoundCounter:
        def __init__(self, name: str, labels: dict[str, str]) -> None:
            self._name = name
            self._labels = labels

        def inc(self, amount: float = 1.0) -> None:
            observations.append((self._name, self._labels, amount))

    class _Counter:
        def __init__(self, name: str) -> None:
            self._name = name

        def labels(self, **labels: str) -> _BoundCounter:
            return _BoundCounter(self._name, labels)

    monkeypatch.setattr(
        signal_worker_module,
        "counter",
        lambda name, *_args: _Counter(name),
    )

    signal_worker_module._record_processed_bar(
        strategy_id="test_strategy_alpha_v1",
        timeframe="5m",
    )
    signal_worker_module._record_emitted_signal(
        Signal(
            strategy_id="test_strategy_alpha_v1",
            strategy_type="indicator",
            symbol="BTCUSDC",
            action=SignalAction.LONG,
            confidence=0.8,
        )
    )

    assert observations == [
        (
            "vm_indicator_bars_processed_total",
            {"strategy_id": "test_strategy_alpha_v1", "timeframe": "5m"},
            1.0,
        ),
        (
            "vm_indicator_signals_emitted_total",
            {"strategy_id": "test_strategy_alpha_v1", "action": "LONG"},
            1.0,
        ),
    ]


def test_post_mutation_checkpoint_failure_retires_core_without_in_place_retry(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instr_id = _seed_instrument(session_factory)
    worker = _build_worker(session_factory, _RecordingStrategy(), bootstrap_bars=0)
    worker.start()
    before = worker._watermark.get("BTCUSD", "1m")  # type: ignore[attr-defined]
    _seed_bars(
        session_factory,
        instr_id=instr_id,
        count=1,
        start=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )

    def _fail_checkpoint(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("UPDATE watermarks", {}, OSError("connection lost"))

    monkeypatch.setattr(worker._watermark, "advance_locked", _fail_checkpoint)  # type: ignore[attr-defined]

    # Periodic catch-up contains the transient DB exception, but the worker must
    # retain terminal state so its supervisor replaces the mutated core.
    worker.catchup_all()
    assert worker.failed is True
    assert worker.rebuild_required is False
    assert isinstance(worker.terminal_error, OperationalError)
    assert len(worker._strategy.on_data_calls) == 1  # type: ignore[attr-defined]
    assert worker._watermark.get("BTCUSD", "1m") == before  # type: ignore[attr-defined]

    worker.catchup_all()
    assert len(worker._strategy.on_data_calls) == 1  # type: ignore[attr-defined]


def test_notify_with_unknown_symbol_is_ignored(session_factory: Any) -> None:
    """A NOTIFY for a symbol the worker does not own does nothing."""
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    _seed_bars(session_factory, instr_id=instr_id, count=2, start=base)

    strategy = _RecordingStrategy()
    worker = _build_worker(session_factory, strategy)
    worker.start()
    bootstrap_count = len(strategy.on_data_calls)

    worker._on_notify("new_market_data", {"symbol": "ETHUSD"})  # type: ignore[attr-defined]

    assert len(strategy.on_data_calls) == bootstrap_count


def test_catchup_payload_with_no_symbol_scans_all_subscribed_symbols(
    session_factory: Any,
) -> None:
    """A periodic catch-up NOTIFY (no ``symbol`` key) hits every subscribed symbol."""
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    _seed_bars(session_factory, instr_id=instr_id, count=2, start=base)

    strategy = _RecordingStrategy()
    worker = _build_worker(session_factory, strategy)
    worker.start()
    bootstrap_count = len(strategy.on_data_calls)

    new_ts = _seed_bars(
        session_factory, instr_id=instr_id, count=1, start=base + timedelta(minutes=2)
    )
    # Catch-up payload: ``type=catchup`` triggers a per-symbol scan.
    worker._on_notify("new_market_data", {"type": "catchup"})  # type: ignore[attr-defined]

    delta = strategy.on_data_calls[bootstrap_count:]
    assert len(delta) == 1
    assert delta[0].timestamp == new_ts[0].replace(tzinfo=UTC)


def test_catchup_all_processes_new_bars_without_notify(session_factory: Any) -> None:
    """The periodic floor (catchup_all) picks up new bars with no NOTIFY at all,
    so a missed/dropped notification cannot stall the strategy indefinitely."""
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    _seed_bars(session_factory, instr_id=instr_id, count=2, start=base)

    strategy = _RecordingStrategy()
    worker = _build_worker(session_factory, strategy)
    worker.start()
    bootstrap_count = len(strategy.on_data_calls)

    new_ts = _seed_bars(
        session_factory, instr_id=instr_id, count=1, start=base + timedelta(minutes=2)
    )
    # No NOTIFY is delivered — the self-healing floor catches up directly.
    worker.catchup_all()

    delta = strategy.on_data_calls[bootstrap_count:]
    assert len(delta) == 1
    assert delta[0].timestamp == new_ts[0].replace(tzinfo=UTC)


def test_catchup_is_memory_bounded_and_resumes_from_durable_checkpoint(
    session_factory: Any,
) -> None:
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    expected = _seed_bars(session_factory, instr_id=instr_id, count=5, start=base)
    strategy = _RecordingStrategy()
    worker = _build_worker(
        session_factory,
        strategy,
        bootstrap_bars=0,
        catchup_batch_size=2,
    )
    worker.start()

    worker.catchup_all()
    assert [state.timestamp for state in strategy.on_data_calls] == [
        value.replace(tzinfo=UTC) for value in expected[:2]
    ]

    worker.catchup_all()
    worker.catchup_all()
    assert [state.timestamp for state in strategy.on_data_calls] == [
        value.replace(tzinfo=UTC) for value in expected
    ]


def test_concurrent_catchups_serialize_shared_multi_asset_state(session_factory: Any) -> None:
    """LISTEN and polling cannot concurrently mutate a multi-symbol core."""
    btc_id = _seed_instrument(session_factory)
    eth_id = _seed_instrument(session_factory, symbol="ETHUSD")
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    _seed_bars(session_factory, instr_id=btc_id, count=1, start=base)
    _seed_bars(session_factory, instr_id=eth_id, count=1, start=base)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    class _BlockingStrategy(_RecordingStrategy):
        def on_data(self, state: MarketState) -> None:
            super().on_data(state)
            if len(self.on_data_calls) == 1:
                first_entered.set()
                assert release_first.wait(timeout=5.0)

    strategy = _BlockingStrategy()
    worker = _build_worker(
        session_factory,
        strategy,
        bootstrap_bars=0,
        symbols=["BTCUSD", "ETHUSD"],
    )
    worker.start()
    errors: list[BaseException] = []

    def _catchup(symbol: str, *, second: bool = False) -> None:
        try:
            if second:
                second_started.set()
            worker._catchup_symbol(symbol)  # type: ignore[attr-defined]
        except BaseException as exc:  # pragma: no cover - assertion aid
            errors.append(exc)

    first = threading.Thread(target=_catchup, args=("BTCUSD",))
    second = threading.Thread(target=_catchup, args=("ETHUSD",), kwargs={"second": True})
    first.start()
    assert first_entered.wait(timeout=5.0)
    second.start()
    assert second_started.wait(timeout=5.0)

    # The first thread owns the worker transaction lock throughout on_data and
    # acknowledgement. Even a different symbol cannot enter shared core state.
    acquired = worker._processing_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    assert acquired is False
    release_first.set()
    first.join(timeout=5.0)
    second.join(timeout=5.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert [state.symbol for state in strategy.on_data_calls] == ["BTCUSD", "ETHUSD"]
    for symbol in ("BTCUSD", "ETHUSD"):
        parked = worker._watermark.get(symbol, "1m")  # type: ignore[attr-defined]
        assert parked.replace(tzinfo=None) == base.replace(tzinfo=None)


def test_watermark_cannot_regress_in_memory_or_database(session_factory: Any) -> None:
    """A stale catch-up acknowledgement never moves a checkpoint backward."""
    instr_id = _seed_instrument(session_factory)
    latest_worker = _build_worker(session_factory, _RecordingStrategy(), bootstrap_bars=0)
    stale_worker = _build_worker(session_factory, _RecordingStrategy(), bootstrap_bars=0)
    newer = datetime(2026, 5, 4, 12, 2, 0, tzinfo=UTC)
    older = newer - timedelta(minutes=1)

    # Cache the absent checkpoint in a second process before the first process
    # advances it. Its later stale write exercises the conditional DB upsert,
    # not merely the in-instance monotonicity guard.
    stale_worker._watermark.get("BTCUSD", "1m")  # type: ignore[attr-defined]
    latest_worker._watermark.set(  # type: ignore[attr-defined]
        "BTCUSD", "1m", newer, instr_id=instr_id
    )
    stale_worker._watermark.set(  # type: ignore[attr-defined]
        "BTCUSD", "1m", older, instr_id=instr_id
    )

    assert stale_worker._watermark.get("BTCUSD", "1m") == newer  # type: ignore[attr-defined]

    # A separate Watermark instance proves the durable row also stayed at max.
    restarted = _build_worker(session_factory, _RecordingStrategy(), bootstrap_bars=0)
    assert restarted._watermark.get("BTCUSD", "1m") == newer  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Watermark recovery on restart
# ---------------------------------------------------------------------------


def test_restart_with_existing_watermark_does_not_replay_old_bars(
    session_factory: Any,
) -> None:
    """A restart whose durable checkpoint is at the latest bar performs no catch-up."""
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    timestamps = _seed_bars(session_factory, instr_id=instr_id, count=5, start=base)

    # A real first run acknowledges through the latest bar and journals the
    # matching model snapshot in the same transaction.
    first_worker = _build_worker(session_factory, _RecordingStrategy(), bootstrap_bars=10)
    first_worker.start()
    checkpoint = first_worker._watermark.get("BTCUSD", "1m")  # type: ignore[attr-defined]
    assert checkpoint.replace(tzinfo=None) == timestamps[-1]

    # Restart reconstructs indicators through the checkpoint and restores the
    # journaled snapshot. A fresh NOTIFY then produces nothing because no bars
    # exist past the checkpoint.
    strategy = _RecordingStrategy()
    worker = _build_worker(session_factory, strategy, bootstrap_bars=10)
    worker.start()
    bootstrap_count = len(strategy.on_data_calls)

    worker._on_notify("new_market_data", {"symbol": "BTCUSD"})  # type: ignore[attr-defined]

    assert len(strategy.on_data_calls) == bootstrap_count


def test_restart_fails_closed_when_checkpoint_has_no_journaled_snapshot(
    session_factory: Any,
) -> None:
    """An acknowledged watermark without its model snapshot cannot start.

    The durable runtime commits checkpoint and snapshot in one transaction, so
    a checkpoint written outside that journal (the retired legacy behavior) is
    corruption and must retire the worker instead of silently re-bootstrapping
    fresh model state onto acknowledged history.
    """
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    timestamps = _seed_bars(session_factory, instr_id=instr_id, count=3, start=base)

    worker = _build_worker(session_factory, _RecordingStrategy(), bootstrap_bars=10)
    worker._watermark.set(  # type: ignore[attr-defined]
        "BTCUSD",
        "1m",
        timestamps[-1].replace(tzinfo=None),
        instr_id=instr_id,
    )

    with pytest.raises(RuntimeError, match="Missing durable model state"):
        worker.start()


def test_restart_retries_uncommitted_bar_instead_of_consuming_it_during_bootstrap(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bar whose journal transaction never committed survives restart as catch-up.

    Durable semantics: model state, decision record, signal envelopes, and the
    source watermark share one commit. A crash after the in-memory mutation
    rolls all of it back, so a fresh core rebuilds only through the
    acknowledged checkpoint and re-processes the failed row on catch-up.
    """
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    acknowledged_ts = _seed_bars(session_factory, instr_id=instr_id, count=1, start=base)[0]

    # Establish the same durable checkpoint a normally bootstrapped worker has.
    first_strategy = _RecordingStrategy()
    first_worker = _build_worker(session_factory, first_strategy, bootstrap_bars=10)
    first_worker.start()
    assert (
        first_worker._watermark.get("BTCUSD", "1m").replace(  # type: ignore[attr-defined]
            tzinfo=None
        )
        == acknowledged_ts
    )

    failed_and_later = _seed_bars(
        session_factory,
        instr_id=instr_id,
        count=2,
        start=base + timedelta(minutes=1),
    )

    # Model the crashed process: bar 12:01 mutated strategy state but the
    # atomic journal commit failed, so the acknowledged watermark stays at
    # 12:00 and the core retires itself.
    crashing_strategy = _RecordingStrategy()
    crashing_worker = _build_worker(session_factory, crashing_strategy, bootstrap_bars=10)
    crashing_worker.start()
    crash_bootstrap_count = len(crashing_strategy.on_data_calls)

    def _fail_commit(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("UPDATE watermarks", {}, OSError("connection lost"))

    monkeypatch.setattr(crashing_worker._watermark, "advance_locked", _fail_commit)  # type: ignore[attr-defined]
    with pytest.raises(OperationalError):
        crashing_worker._catchup_symbol("BTCUSD")  # type: ignore[attr-defined]
    assert crashing_worker.failed is True
    # Exactly the 12:01 bar mutated in-memory state before the failed commit.
    assert len(crashing_strategy.on_data_calls) == crash_bootstrap_count + 1

    # A fresh process rebuilds only through 12:00. The failed 12:01 row and the
    # following 12:02 row are both delivered through normal catch-up.
    recovered_strategy = _RecordingStrategy()
    recovered_worker = _build_worker(session_factory, recovered_strategy, bootstrap_bars=10)
    recovered_worker.start()

    assert len(recovered_strategy.bootstrap_calls) == 1
    assert [state.timestamp for state in recovered_strategy.bootstrap_calls[0]] == [
        acknowledged_ts.replace(tzinfo=UTC)
    ]
    bootstrap_count = len(recovered_strategy.on_data_calls)

    recovered_worker._on_notify(  # type: ignore[attr-defined]
        "new_market_data",
        {"symbol": "BTCUSD"},
    )

    assert [state.timestamp for state in recovered_strategy.on_data_calls[bootstrap_count:]] == [
        timestamp.replace(tzinfo=UTC) for timestamp in failed_and_later
    ]
    parked = recovered_worker._watermark.get("BTCUSD", "1m")  # type: ignore[attr-defined]
    assert parked.replace(tzinfo=None) == failed_and_later[-1]


def test_missed_notify_detects_historical_change_before_any_new_bar(
    session_factory: Any,
) -> None:
    """Polling must stop on a durable correction marker even without NOTIFY."""
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    historical = _seed_bars(session_factory, instr_id=instr_id, count=4, start=base)
    strategy = _RecordingStrategy()
    worker = _build_worker(session_factory, strategy)
    worker.start()
    acknowledged = worker._watermark.get("BTCUSD", "1m")  # type: ignore[attr-defined]
    processed = len(strategy.on_data_calls)

    _mark_historical_change(
        session_factory,
        instr_id=instr_id,
        timestamp=historical[1],
    )
    _seed_bars(
        session_factory,
        instr_id=instr_id,
        count=1,
        start=base + timedelta(minutes=4),
    )

    with pytest.raises(HistoricalRebuildRequiredError, match="Historical rebuild pending"):
        worker.catchup_all()

    state = worker._watermark.get_state("BTCUSD", "1m")  # type: ignore[attr-defined]
    assert state.last_ts == acknowledged
    assert state.rebuild_from_ts == historical[1].replace(tzinfo=UTC)
    assert len(strategy.on_data_calls) == processed
    assert worker.rebuild_required is True


def test_restart_rebuilds_all_symbols_in_deterministic_event_order(
    session_factory: Any,
) -> None:
    """One corrected feed rebuilds the complete shared multi-asset core."""
    btc_id = _seed_instrument(session_factory)
    eth_id = _seed_instrument(session_factory, symbol="ETHUSD")
    base = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    btc_ts = _seed_bars(session_factory, instr_id=btc_id, count=4, start=base)
    # The second feed deliberately trails the corrected feed. A shared-core
    # rebuild still has to replay it through its own acknowledged boundary.
    eth_ts = _seed_bars(session_factory, instr_id=eth_id, count=2, start=base)

    first = _build_worker(
        session_factory,
        _RecordingStrategy(),
        symbols=["BTCUSD", "ETHUSD"],
    )
    first.start()
    _mark_historical_change(
        session_factory,
        instr_id=btc_id,
        timestamp=btc_ts[2],
    )

    rebuilt_strategy = _RecordingStrategy()
    rebuilt = _build_worker(
        session_factory,
        rebuilt_strategy,
        symbols=["BTCUSD", "ETHUSD"],
    )
    rebuilt.start()

    # A durable correction rebuild replays authoritative history through
    # ``rebuild_model_history`` (fresh-core decision replay), not through the
    # startup ``bootstrap_history`` path.
    assert rebuilt_strategy.bootstrap_calls == []
    replay = rebuilt_strategy.on_data_calls
    expected_order = sorted(
        [(timestamp.replace(tzinfo=UTC), "BTCUSD") for timestamp in btc_ts]
        + [(timestamp.replace(tzinfo=UTC), "ETHUSD") for timestamp in eth_ts]
    )
    assert [(state.timestamp, state.symbol) for state in replay] == expected_order
    assert next(
        state.close
        for state in replay
        if state.symbol == "BTCUSD" and state.timestamp == btc_ts[2].replace(tzinfo=UTC)
    ) == pytest.approx(102.25)
    for symbol, expected_last_ts in (
        ("BTCUSD", btc_ts[-1]),
        ("ETHUSD", eth_ts[-1]),
    ):
        state = rebuilt._watermark.get_state(symbol, "1m")  # type: ignore[attr-defined]
        assert state.rebuild_pending is False
        assert state.last_ts == expected_last_ts.replace(tzinfo=UTC)
        assert state.rebuild_generation == 1


def test_rebuild_acknowledgement_rejects_a_new_generation(session_factory: Any) -> None:
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    timestamps = _seed_bars(session_factory, instr_id=instr_id, count=3, start=base)
    worker = _build_worker(session_factory, _RecordingStrategy())
    worker.start()
    _mark_historical_change(
        session_factory,
        instr_id=instr_id,
        timestamp=timestamps[1],
    )
    captured = worker._watermark.get_state("BTCUSD", "1m")  # type: ignore[attr-defined]

    # A second provider correction at the same earliest boundary must still
    # advance the generation. Boundary-only comparison would otherwise let the
    # first replay acknowledge stale content.
    _mark_historical_change(
        session_factory,
        instr_id=instr_id,
        timestamp=timestamps[1],
    )

    with pytest.raises(RebuildGenerationChangedError):
        worker._watermark.acknowledge_rebuilds([captured])  # type: ignore[attr-defined]

    current = worker._watermark.get_state("BTCUSD", "1m")  # type: ignore[attr-defined]
    assert current.rebuild_from_ts == timestamps[1].replace(tzinfo=UTC)
    assert current.rebuild_generation == captured.rebuild_generation + 1


def test_empty_cold_start_rebuilds_when_history_first_appears(session_factory: Any) -> None:
    instr_id = _seed_instrument(session_factory)
    empty_worker = _build_worker(session_factory, _RecordingStrategy())
    empty_worker.start()
    empty_state = empty_worker._watermark.get_state("BTCUSD", "1m")  # type: ignore[attr-defined]
    assert empty_worker._watermark.is_initial(empty_state.last_ts)  # type: ignore[attr-defined]

    # The epoch checkpoint is an absence sentinel, not a lower bound on valid
    # market history. Rebuilds must also support data older than that sentinel.
    base = datetime(1999, 5, 4, 12, 0, tzinfo=UTC)
    timestamps = _seed_bars(session_factory, instr_id=instr_id, count=3, start=base)
    _mark_historical_change(
        session_factory,
        instr_id=instr_id,
        timestamp=timestamps[0],
    )

    strategy = _RecordingStrategy()
    rebuilt = _build_worker(session_factory, strategy)
    rebuilt.start()

    # Durable rebuilds replay through ``rebuild_model_history`` (on_data on a
    # fresh core), never through the startup ``bootstrap_history`` path.
    assert strategy.bootstrap_calls == []
    assert [state.timestamp for state in strategy.on_data_calls] == [
        timestamp.replace(tzinfo=UTC) for timestamp in timestamps
    ]
    state = rebuilt._watermark.get_state("BTCUSD", "1m")  # type: ignore[attr-defined]
    assert state.last_ts == timestamps[-1].replace(tzinfo=UTC)
    assert state.rebuild_pending is False


def test_catchup_picks_up_bars_inserted_during_bootstrap(
    session_factory: Any,
) -> None:
    """Bars inserted *between* bootstrap and the first NOTIFY are not lost.

    Models the race where market_data_ingestor commits new bars while the
    worker is still warming indicators on historical data.
    """
    instr_id = _seed_instrument(session_factory)
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    historical = _seed_bars(session_factory, instr_id=instr_id, count=3, start=base)

    strategy = _RecordingStrategy()
    worker = _build_worker(session_factory, strategy, bootstrap_bars=10)
    worker.start()

    # Bootstrap parked the watermark on ``historical[-1]``; new bars now
    # arrive ahead of the (async) NOTIFY. The next NOTIFY catches them up.
    race_ts = _seed_bars(
        session_factory, instr_id=instr_id, count=2, start=base + timedelta(minutes=3)
    )
    bootstrap_count = len(strategy.on_data_calls)

    worker._on_notify("new_market_data", {"symbol": "BTCUSD"})  # type: ignore[attr-defined]

    delta = strategy.on_data_calls[bootstrap_count:]
    assert [s.timestamp for s in delta] == [t.replace(tzinfo=UTC) for t in race_ts]


# ── strategy_id resolution (N3 regression) ───────────────────────────────────
def test_resolve_strategy_id_prefers_top_level_config() -> None:
    from indicator_runner.signal_worker import _resolve_strategy_id

    # Every indicator config declares strategy_id at the TOP LEVEL; parameters is
    # for tuning knobs. Reading only params made all strategies mislabel as
    # swing_high_low_pmo_v1 and miss their own bindings.
    config = {"strategy_id": "test_strategy_alpha_v1", "parameters": {"fast_period": "5"}}
    assert _resolve_strategy_id(config, name="TestStrategyAlpha") == "test_strategy_alpha_v1"


def test_resolve_strategy_id_raises_when_missing() -> None:
    from indicator_runner.signal_worker import _resolve_strategy_id

    with pytest.raises(ValueError, match="strategy_id missing"):
        _resolve_strategy_id({"parameters": {}}, name="Broken")


def test_main_configures_logging_before_work(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """O4: each strategy runs as its own signal_worker subprocess, so main() must
    configure logging (default INFO) — otherwise the subprocess root logger
    defaults to WARNING and drops every per-strategy INFO line (bootstrap, warmup,
    "Posted signal ..."), which is why the soak's indicator-runner log was a
    single line despite 866 emissions."""
    from indicator_runner import signal_worker

    calls: list[str] = []
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setattr(signal_worker, "setup_logging", lambda level: calls.append(level))

    sentinel = RuntimeError("stop after logging setup")

    def _boom(*_args: Any, **_kwargs: Any):  # type: ignore[no-untyped-def]
        raise sentinel

    monkeypatch.setattr(signal_worker, "_build_worker", _boom)

    with pytest.raises(RuntimeError, match="stop after logging setup"):
        signal_worker.main(["--strategy-path", str(tmp_path)])

    assert calls == ["INFO"]


def test_main_returns_nonzero_for_listener_delivery_failure(
    session_factory: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """A callback error swallowed by PgNotifyListener still fails the process."""
    from indicator_runner import signal_worker

    class _AliveThread:
        def is_alive(self) -> bool:
            return True

    class _FailedWorker:
        _listener_thread = _AliveThread()
        _dsn = "postgresql://listener"
        catchup_floor_seconds = 30
        delivery_alive = True
        failed = True
        rebuild_required = False
        terminal_error = httpx.ConnectError("scoring unavailable")

        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        def start(self) -> None:
            self.started = True

        def request_stop(self) -> None:
            return None

        def stop(self) -> bool:
            self.stopped = True
            return True

    worker = _FailedWorker()
    with session_factory() as session:
        session.add(
            User(
                user_id="owner",
                email="owner@example.invalid",
                base_ccy="USD",
                is_deployment_owner=True,
            )
        )
        session.commit()
        engine = session.get_bind()
    monkeypatch.setattr(signal_worker, "setup_logging", lambda _level: None)
    monkeypatch.setattr(signal_worker, "_build_worker", lambda *_args, **_kwargs: (worker, engine))
    monkeypatch.setattr(signal_worker, "dispose_engine", lambda _engine: None)
    monkeypatch.setattr(signal_worker.os_signal, "signal", lambda *_args: None)

    exit_code = signal_worker.main(["--strategy-path", str(tmp_path)])

    assert exit_code == 1
    assert worker.started is True
    assert worker.stopped is True


def test_main_disposes_the_engine_when_delivery_outlives_its_stop_budget(
    session_factory: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from indicator_runner import signal_worker

    class _AliveThread:
        def is_alive(self) -> bool:
            return True

    class _StuckWorker:
        _listener_thread = _AliveThread()
        _dsn = "postgresql://listener"
        catchup_floor_seconds = 30
        delivery_alive = True
        failed = True
        rebuild_required = False
        terminal_error = httpx.ConnectError("scoring unavailable")

        def start(self) -> None:
            return None

        def request_stop(self) -> None:
            return None

        def stop(self) -> bool:
            return False  # a pass is still in flight after the stop budget

    disposed: list[Any] = []
    with session_factory() as session:
        session.add(
            User(
                user_id="owner",
                email="owner@example.invalid",
                base_ccy="USD",
                is_deployment_owner=True,
            )
        )
        session.commit()
        engine = session.get_bind()
    monkeypatch.setattr(signal_worker, "setup_logging", lambda _level: None)
    monkeypatch.setattr(
        signal_worker, "_build_worker", lambda *_args, **_kwargs: (_StuckWorker(), engine)
    )
    monkeypatch.setattr(signal_worker, "dispose_engine", disposed.append)
    monkeypatch.setattr(signal_worker.os_signal, "signal", lambda *_args: None)

    assert signal_worker.main(["--strategy-path", str(tmp_path)]) == 1
    assert disposed == [engine]  # the in-flight pass owns its connection; the lease recovers it


def test_executable_entrypoint_requires_owner_before_starting_worker(
    session_factory: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from lib_application.services.deployment_owner import DeploymentOwnerError

    worker = _build_worker(session_factory, _RecordingStrategy(), bootstrap_bars=0)
    with session_factory() as session:
        engine = session.get_bind()
    monkeypatch.setattr(
        signal_worker_module, "_build_worker", lambda *args, **kwargs: (worker, engine)
    )
    monkeypatch.setattr(signal_worker_module.os_signal, "signal", lambda *args: None)

    def refuse_unfenced_start() -> None:
        raise AssertionError("Executable worker started without deployment ownership")

    monkeypatch.setattr(worker, "start", refuse_unfenced_start)
    with pytest.raises(DeploymentOwnerError, match="active deployment owner"):
        signal_worker_module.main(["--strategy-path", str(tmp_path)])


class _ObservingStrategy(PureSignalStrategy):
    def __init__(self) -> None:
        super().__init__(
            strategy_id="delivery_wiring_probe",
            strategy_type="indicator",
            config={"strategy_version": "1.0.0"},
        )

    def initialize(self) -> None:
        return None

    def on_data(self, state: MarketState) -> None:
        return None


class _FakeDeliveryLoop:
    def __init__(self, *, stops_cleanly: bool = True) -> None:
        self.wakes = 0
        self.started = False
        self.stop_requests = 0
        self.stopped_with: float | None = None
        self.alive = False
        self.failure_handler: Any = None
        self._stops_cleanly = stops_cleanly

    def bind_failure_handler(self, handler: Any) -> None:
        self.failure_handler = handler

    def start(self) -> None:
        self.started = True
        self.alive = True

    def wake(self) -> None:
        self.wakes += 1

    def request_stop(self) -> None:
        self.stop_requests += 1

    def stop(self, *, timeout: float) -> bool:
        self.stopped_with = timeout
        self.alive = not self._stops_cleanly
        return self._stops_cleanly


def test_worker_wakes_an_injected_delivery_loop_instead_of_delivering_inline(
    session_factory: sessionmaker,
) -> None:
    # start() bootstraps the configured universe and fails closed on an unknown
    # instrument, so the default BTCUSD row must exist even with no bars.
    _seed_instrument(session_factory)
    loop = _FakeDeliveryLoop()
    worker = _build_worker(session_factory, _ObservingStrategy(), delivery_loop=loop)

    worker.start()
    assert loop.started is True
    assert loop.wakes == 1  # start() hands the committed backlog to the loop
    assert loop.failure_handler is not None
    assert worker.delivery_alive is True

    worker.deliver_after_commit()
    assert loop.wakes == 2

    assert worker.stop() is True
    assert loop.stop_requests == 1
    assert loop.stopped_with is not None
    assert loop.stopped_with >= 10.0
    assert worker.delivery_alive is False


def test_worker_stop_reports_a_delivery_loop_that_outlived_its_budget(
    session_factory: sessionmaker,
) -> None:
    _seed_instrument(session_factory)
    loop = _FakeDeliveryLoop(stops_cleanly=False)
    worker = _build_worker(session_factory, _ObservingStrategy(), delivery_loop=loop)
    worker.start()
    assert worker.stop() is False
    assert worker.delivery_alive is True  # the caller can see the pass is still in flight


def test_worker_without_delivery_loop_delivers_inline(session_factory: sessionmaker) -> None:
    worker = _build_worker(session_factory, _ObservingStrategy())
    assert worker.delivery_alive is True
    assert worker.deliver_after_commit() is None  # runs relay_once synchronously


def test_main_loop_wait_follows_the_catchup_floor_not_the_delivery_interval() -> None:
    from indicator_runner.signal_worker import _seconds_until_catchup

    # Far from the floor: one liveness tick, whatever the delivery idle interval is.
    assert _seconds_until_catchup(100.0, 90.0, 30.0) == 1.0
    # Close to the floor: wait exactly until it is due.
    assert _seconds_until_catchup(119.5, 90.0, 30.0) == 0.5
    # Overdue: run catch-up now.
    assert _seconds_until_catchup(130.0, 90.0, 30.0) == 0.0
    # A floor shorter than a tick is honoured too.
    assert _seconds_until_catchup(100.0, 100.0, 0.25) == 0.25


def test_delivery_failure_marks_the_worker_failed(session_factory: sessionmaker) -> None:
    _seed_instrument(session_factory)
    loop = _FakeDeliveryLoop()
    worker = _build_worker(session_factory, _ObservingStrategy(), delivery_loop=loop)
    worker.start()
    loop.failure_handler(RuntimeError("relay database gone"))
    assert worker.failed is True
    assert isinstance(worker.terminal_error, RuntimeError)
    worker.stop()
