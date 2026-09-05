"""PostgreSQL acceptance for feedback-cycle and catalogue concurrency fences."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from feedback_loop_engine.engine import FeedbackLoopEngine
from feedback_loop_engine.models import EvaluationHorizon
from feedback_loop_engine.price_provider import (
    PriceObservation,
    PriceProvider,
    SqlOHLCPriceProvider,
)
from lib_application.db.models import (
    CanonicalSignal,
    Instrument,
    InstrumentPrice,
    SignalPerformance,
    Strategy,
    StrategyConsecutiveWrongTracker,
    StrategyParameterFeedback,
    StrategyVersion,
)
from lib_application.services.strategy_feedback import exact_strategy_version_is_active
from lib_strategy.signals.emitter import BacktestSignalEmitter
from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import MarketState
from lib_strategy.signals.signal import Signal, SignalAction

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json"
)
_STRATEGY_PATH = Path(__file__).resolve().parents[3] / "tests/fixtures/strategies/PipelineExerciser"
_LEASE_NAMESPACE = "vynmatrix.feedback.evaluation-cycle"


@dataclass(frozen=True)
class _FeedbackScope:
    admin_engine: Engine
    runtime_engine: Engine
    instr_id: int
    signal_ids: tuple[int, int]
    strategy_id: str
    strat_ver_id: int


class _BlockingPriceProvider(PriceProvider):
    def __init__(
        self,
        delegate: PriceProvider,
        *,
        entered: Event,
        release: Event,
    ) -> None:
        self._delegate = delegate
        self._entered = entered
        self._release = release

    def get_entry_observation(
        self,
        instr_id: int,
        as_of: datetime,
        *,
        price_source: str | None = None,
        price_timeframe: str | None = None,
    ) -> PriceObservation | None:
        return self._delegate.get_entry_observation(
            instr_id,
            as_of,
            price_source=price_source,
            price_timeframe=price_timeframe,
        )

    def get_exit_observation(
        self,
        instr_id: int,
        as_of: datetime,
        horizon: str,
        *,
        price_source: str | None = None,
        price_timeframe: str | None = None,
    ) -> PriceObservation | None:
        self._entered.set()
        if not self._release.wait(timeout=10):
            message = "Timed out waiting to release the feedback-cycle test fence"
            raise TimeoutError(message)
        return self._delegate.get_exit_observation(
            instr_id,
            as_of,
            horizon,
            price_source=price_source,
            price_timeframe=price_timeframe,
        )


def _postgres_url(name: str, *, fallback: str | None = None) -> str:
    raw = os.getenv(name) or fallback
    if not raw:
        pytest.skip(f"{name} is required for PostgreSQL feedback acceptance")
    if not make_url(raw).drivername.startswith("postgresql"):
        pytest.skip("PostgreSQL feedback acceptance requires PostgreSQL URLs")
    return raw


def _real_strategy_signals() -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    tuple[tuple[Signal, dict[str, Any]], tuple[Signal, dict[str, Any]]],
]:
    """Run the production EMA core on tracked Coinbase public history."""

    fixture: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    bars = list(fixture["bars"])
    config: dict[str, Any] = json.loads(
        (_STRATEGY_PATH / "config.json").read_text(encoding="utf-8")
    )
    emitter = BacktestSignalEmitter()
    parameters = dict(config["parameters"])
    parameters["strategy_version"] = config["strategy_version"]
    strategy = load_pure_strategy_core(_STRATEGY_PATH)(
        strategy_id=str(config["strategy_id"]),
        strategy_type="indicator",
        config=parameters,
        emitter=emitter,
    )
    strategy.initialize()
    bar_by_close = {}
    for raw in bars:
        bar = dict(raw)
        bar_open_ts = datetime.fromtimestamp(float(bar["ts"]), tz=UTC)
        bar_by_close[bar_open_ts + timedelta(minutes=1)] = bar
        state = MarketState(
            symbol="BTCUSD",
            timestamp=bar_open_ts,
            open=float(bar["open"]),
            high=float(bar["high"]),
            low=float(bar["low"]),
            close=float(bar["close"]),
            volume=float(bar["volume"]),
            metadata={
                "price_source": str(fixture["source"]),
                "price_timeframe": "1m",
            },
        )
        strategy.record_bar(state.symbol)
        strategy.on_data(state)

    wrong_long_signals: list[tuple[Signal, dict[str, Any]]] = []
    for signal in emitter.get_signals():
        if signal.action is not SignalAction.LONG or signal.timestamp is None:
            continue
        exit_bar = bar_by_close.get(signal.timestamp + timedelta(hours=1))
        if exit_bar is None or signal.entry_price is None:
            continue
        price_change = (float(exit_bar["close"]) - signal.entry_price) / signal.entry_price
        if price_change < 0.001:
            wrong_long_signals.append((signal, exit_bar))
        if len(wrong_long_signals) == 2:
            break
    assert len(wrong_long_signals) == 2
    return config, bars, (wrong_long_signals[0], wrong_long_signals[1])


@pytest.fixture
def feedback_scope() -> Iterator[_FeedbackScope]:
    admin_url = _postgres_url("DATABASE_URL")
    runtime_url = _postgres_url("FEEDBACK_DATABASE_URL", fallback=admin_url)
    admin_engine = create_engine(admin_url, future=True)
    runtime_engine = create_engine(runtime_url, future=True, pool_size=3, max_overflow=0)
    suffix = uuid4().hex[:12]
    config, _bars, windows = _real_strategy_signals()
    strategy_id = f"{config['strategy_id']}_fence_{suffix}"
    source = str(windows[0][0].metadata["price_source"])

    with Session(admin_engine) as session:
        strategy = Strategy(
            strategy_id=strategy_id,
            strategy_name="EMA Cross Scalper",
            asset_class="crypto",
            is_active=True,
        )
        version = StrategyVersion(
            strategy_id=strategy_id,
            semver=str(config["strategy_version"]),
            param_schema={},
            default_params=dict(config["parameters"]),
            status="active",
        )
        instrument = Instrument(
            asset_class="crypto",
            canonical=f"BTC/FENCE-{suffix}",
            exchange="coinbase",
            settlement_currency="USD",
        )
        session.add_all([strategy, version, instrument])
        session.flush()

        signals: list[CanonicalSignal] = []
        prices: list[InstrumentPrice] = []
        for index, (observed_signal, exit_bar) in enumerate(windows, start=1):
            assert observed_signal.timestamp is not None
            assert observed_signal.entry_price is not None
            signal_ts = observed_signal.timestamp
            exit_bar_ts = datetime.fromtimestamp(exit_bar["ts"], tz=UTC)
            assert exit_bar_ts + timedelta(minutes=1) == signal_ts + timedelta(hours=1)
            signals.append(
                CanonicalSignal(
                    strategy_id=strategy_id,
                    strat_ver_id=version.strat_ver_id,
                    instr_id=instrument.instr_id,
                    action=observed_signal.action.value.lower(),
                    direction=observed_signal.action.value.lower(),
                    confidence=observed_signal.confidence,
                    entry_price=observed_signal.entry_price,
                    horizon_seconds=3_600,
                    signal_meta=dict(observed_signal.metadata),
                    ts=signal_ts,
                    run_id=f"feedback-fence-{suffix}",
                    external_signal_id=f"feedback-fence-{suffix}-{index}",
                )
            )
            prices.append(
                InstrumentPrice(
                    instr_id=instrument.instr_id,
                    ts=exit_bar_ts,
                    timeframe="1m",
                    close=exit_bar["close"],
                    source=source,
                )
            )
        session.add_all([*signals, *prices])
        session.commit()
        scope = _FeedbackScope(
            admin_engine=admin_engine,
            runtime_engine=runtime_engine,
            instr_id=int(instrument.instr_id),
            signal_ids=(int(signals[0].signal_id), int(signals[1].signal_id)),
            strategy_id=strategy_id,
            strat_ver_id=int(version.strat_ver_id),
        )

    try:
        yield scope
    finally:
        with Session(admin_engine) as session:
            session.query(StrategyParameterFeedback).filter_by(strategy_id=strategy_id).delete(
                synchronize_session=False
            )
            session.query(StrategyConsecutiveWrongTracker).filter_by(
                strategy_id=strategy_id
            ).delete(synchronize_session=False)
            session.query(SignalPerformance).filter(
                SignalPerformance.signal_id.in_(scope.signal_ids)
            ).delete(synchronize_session=False)
            session.query(CanonicalSignal).filter(
                CanonicalSignal.signal_id.in_(scope.signal_ids)
            ).delete(synchronize_session=False)
            session.query(InstrumentPrice).filter_by(instr_id=scope.instr_id).delete(
                synchronize_session=False
            )
            session.query(StrategyVersion).filter_by(strategy_id=strategy_id).delete(
                synchronize_session=False
            )
            session.query(Strategy).filter_by(strategy_id=strategy_id).delete(
                synchronize_session=False
            )
            session.query(Instrument).filter_by(instr_id=scope.instr_id).delete(
                synchronize_session=False
            )
            session.commit()
        runtime_engine.dispose()
        admin_engine.dispose()


@pytest.mark.integration
def test_concurrent_feedback_workers_serialize_one_horizon(
    feedback_scope: _FeedbackScope,
) -> None:
    session_factory = sessionmaker(
        feedback_scope.runtime_engine,
        expire_on_commit=False,
    )
    entered = Event()
    release = Event()
    provider = SqlOHLCPriceProvider(session_factory)
    first = FeedbackLoopEngine(
        feedback_scope.runtime_engine,
        wrong_threshold=99,
        price_provider=_BlockingPriceProvider(
            provider,
            entered=entered,
            release=release,
        ),
    )
    second = FeedbackLoopEngine(
        feedback_scope.runtime_engine,
        wrong_threshold=99,
        price_provider=provider,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            first.run_evaluation_cycle,
            EvaluationHorizon.H1,
            100,
        )
        assert entered.wait(timeout=10)
        with feedback_scope.admin_engine.connect() as connection:
            lease_available = connection.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:namespace), hashtext(:horizon))"),
                {"namespace": _LEASE_NAMESPACE, "horizon": "1h"},
            ).scalar_one()
            connection.rollback()
        assert lease_available is False

        second_future = executor.submit(
            second.run_evaluation_cycle,
            EvaluationHorizon.H1,
            100,
        )
        release.set()
        first_result = first_future.result(timeout=20)
        second_result = second_future.result(timeout=20)

    assert first_result["signals_evaluated"] == 2
    assert second_result["signals_evaluated"] == 0
    with Session(feedback_scope.admin_engine) as session:
        tracker = session.execute(
            select(StrategyConsecutiveWrongTracker).where(
                StrategyConsecutiveWrongTracker.strategy_id == feedback_scope.strategy_id,
                StrategyConsecutiveWrongTracker.instr_id == feedback_scope.instr_id,
                StrategyConsecutiveWrongTracker.horizon == "1h",
            )
        ).scalar_one()
        performance_count = session.scalar(
            select(func.count())
            .select_from(SignalPerformance)
            .where(SignalPerformance.signal_id.in_(feedback_scope.signal_ids))
        )
        assert tracker.last_signal_id == feedback_scope.signal_ids[1]
        assert tracker.consecutive_wrong_count == 2
        assert performance_count == 2


@pytest.mark.integration
def test_feedback_eligibility_lock_blocks_strategy_version_retirement(
    feedback_scope: _FeedbackScope,
) -> None:
    with feedback_scope.runtime_engine.connect() as eligibility_connection:
        eligibility_transaction = eligibility_connection.begin()
        assert bool(
            eligibility_connection.execute(
                text(
                    "SELECT public.vm_feedback_exact_strategy_version_active("
                    ":strategy_id, :strat_ver_id)"
                ),
                {
                    "strategy_id": feedback_scope.strategy_id,
                    "strat_ver_id": feedback_scope.strat_ver_id,
                },
            ).scalar_one()
        )

        with feedback_scope.admin_engine.connect() as retirement_connection:
            retirement_transaction = retirement_connection.begin()
            retirement_connection.exec_driver_sql("SET LOCAL lock_timeout = '250ms'")
            with pytest.raises(OperationalError):
                retirement_connection.execute(
                    text(
                        "UPDATE strategy_versions SET status = 'deprecated' "
                        "WHERE strat_ver_id = :strat_ver_id"
                    ),
                    {"strat_ver_id": feedback_scope.strat_ver_id},
                )
            retirement_transaction.rollback()

        eligibility_transaction.rollback()

    with feedback_scope.admin_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE strategy_versions SET status = 'deprecated' "
                "WHERE strat_ver_id = :strat_ver_id"
            ),
            {"strat_ver_id": feedback_scope.strat_ver_id},
        )

    with Session(feedback_scope.runtime_engine) as session:
        assert not exact_strategy_version_is_active(
            session,
            strategy_id=feedback_scope.strategy_id,
            strat_ver_id=feedback_scope.strat_ver_id,
        )
