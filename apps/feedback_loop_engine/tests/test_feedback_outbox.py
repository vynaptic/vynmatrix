from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from feedback_loop_engine.engine import FeedbackLoopEngine
from feedback_loop_engine.models import EvaluationHorizon
from feedback_loop_engine.price_provider import (
    PriceObservation,
    PriceObservationOrigin,
    PriceProvider,
    SqlOHLCPriceProvider,
    evaluation_target,
)
from lib_application.db.models import (
    Base,
    CanonicalSignal,
    Instrument,
    InstrumentPrice,
    SignalPerformance,
    Strategy,
    StrategyConsecutiveWrongTracker,
)
from lib_application.outbox import OutboxStore


class _FixedPriceProvider(PriceProvider):
    def get_entry_observation(
        self,
        instr_id: int,
        as_of: datetime,
        *,
        price_source: str | None = None,
        price_timeframe: str | None = None,
    ) -> PriceObservation:
        del instr_id
        return self._observation(
            price_id=1,
            price=100.0,
            target=as_of,
            source=price_source,
            timeframe=price_timeframe,
        )

    def get_exit_observation(
        self,
        instr_id: int,
        as_of: datetime,
        horizon: str,
        *,
        price_source: str | None = None,
        price_timeframe: str | None = None,
    ) -> PriceObservation:
        del instr_id
        return self._observation(
            price_id=2,
            price=110.0,
            target=evaluation_target(as_of, horizon),
            source=price_source,
            timeframe=price_timeframe,
        )

    @staticmethod
    def _observation(
        *,
        price_id: int,
        price: float,
        target: datetime,
        source: str | None,
        timeframe: str | None,
    ) -> PriceObservation:
        return PriceObservation(
            price_id=price_id,
            price=price,
            bar_open_ts=target - timedelta(minutes=1),
            bar_close_ts=target,
            source=source,
            timeframe=timeframe,
            origin=PriceObservationOrigin.PRICES_TABLE,
        )


def test_feedback_evaluation_cycle_persists_performance_and_outbox_event() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)

    signal_ts = datetime.now(UTC) - timedelta(days=2)
    with session_local() as session:
        session.add(
            Strategy(
                strategy_id="swing_high_low_pmo_v1",
                strategy_name="SwingHighLowPMO",
                asset_class="crypto",
            )
        )
        session.add(
            Instrument(
                instr_id=10,
                asset_class="crypto",
                canonical="BTCUSD",
                exchange="coinbase",
                settlement_currency="USD",
            )
        )
        session.flush()
        session.add(
            CanonicalSignal(
                signal_id=123,
                strategy_id="swing_high_low_pmo_v1",
                instr_id=10,
                action="long",
                direction="long",
                confidence=0.9,
                entry_price=100.0,
                horizon_seconds=86_400,
                signal_meta={"price_source": "coinbase_live", "price_timeframe": "15m"},
                ts=signal_ts,
                run_id="run-feedback-cycle",
                external_signal_id="ext-feedback-cycle",
            )
        )
        session.commit()

    feedback_engine = FeedbackLoopEngine(
        engine=engine,
        price_provider=_FixedPriceProvider(),
        outbox_store=OutboxStore(session_local),
    )

    result = feedback_engine.run_evaluation_cycle(horizon=EvaluationHorizon.D1, limit=10)

    assert result == {
        "signals_evaluated": 1,
        "correct_predictions": 1,
        "wrong_predictions": 0,
        "optimizations_triggered": 0,
        "skipped_no_price": 0,
        "errors": 0,
    }

    with session_local() as session:
        performance = session.execute(select(SignalPerformance)).scalar_one()
        assert performance.signal_id == 123
        assert performance.strategy_id == "swing_high_low_pmo_v1"
        assert performance.is_correct is True
        assert float(performance.pnl_pct) == 0.1
        assert performance.meta == {
            "price_provenance_schema": "v1",
            "evaluation_target_ts": (signal_ts + timedelta(days=1)).isoformat(),
            "entry_price_provenance": {
                "price_id": None,
                "price": 100.0,
                "bar_open_ts": None,
                "bar_close_ts": None,
                "source": "coinbase_live",
                "timeframe": "15m",
                "origin": "canonical_signal",
            },
            "exit_price_provenance": {
                "price_id": 2,
                "price": 110.0,
                "bar_open_ts": (signal_ts + timedelta(days=1, minutes=-1)).isoformat(),
                "bar_close_ts": (signal_ts + timedelta(days=1)).isoformat(),
                "source": "coinbase_live",
                "timeframe": "15m",
                "origin": "prices_table",
            },
            "execution_lineage_schema": "v1",
            "execution_lineage": {
                "canonical_signal_id": 123,
                "decision_ids": [],
                "decisions": [],
                "account_ids": [],
                "user_ids": [],
                "intent_ids": [],
                "order_ids": [],
                "fill_ids": [],
            },
        }

    records = feedback_engine._outbox_store.list_pending(topics=["feedback.ready"])
    assert len(records) == 1
    assert records[0].payload["signal_id"] == 123
    assert records[0].payload["is_correct"] is True


def test_restart_after_tracker_commit_replays_without_double_increment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)
    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    entry_bar = fixture["bars"][9]
    exit_bar = fixture["bars"][68]
    signal_ts = datetime.fromtimestamp(entry_bar["ts"], tz=UTC)
    exit_bar_ts = datetime.fromtimestamp(exit_bar["ts"], tz=UTC)
    assert exit_bar_ts + timedelta(minutes=1) == signal_ts + timedelta(hours=1)
    assert exit_bar["close"] < entry_bar["close"]
    with session_local() as session:
        session.add(
            Strategy(
                strategy_id="restart_safe_strategy_v1",
                strategy_name="Restart safe strategy",
                asset_class="crypto",
            )
        )
        session.add(
            Instrument(
                instr_id=44,
                asset_class="crypto",
                canonical="BTCUSDC",
                exchange="coinbase",
                settlement_currency="USDC",
            )
        )
        session.flush()
        session.add(
            CanonicalSignal(
                signal_id=4401,
                strategy_id="restart_safe_strategy_v1",
                instr_id=44,
                action="long",
                direction="long",
                confidence=0.8,
                entry_price=entry_bar["close"],
                horizon_seconds=3_600,
                ts=signal_ts,
                external_signal_id="restart-safe-signal",
                signal_meta={
                    "price_source": fixture["source"],
                    "price_timeframe": "1m",
                },
            )
        )
        session.add(
            InstrumentPrice(
                instr_id=44,
                ts=exit_bar_ts,
                timeframe="1m",
                close=exit_bar["close"],
                source=fixture["source"],
            )
        )
        session.commit()

    interrupted = FeedbackLoopEngine(
        engine=engine,
        wrong_threshold=2,
        price_provider=SqlOHLCPriceProvider(session_local),
    )

    def _crash_before_performance_commit(*_args: object, **_kwargs: object) -> tuple[int, bool]:
        message = "injected crash after durable tracker commit"
        raise SQLAlchemyError(message)

    monkeypatch.setattr(
        interrupted.evaluator,
        "persist_evaluation",
        _crash_before_performance_commit,
    )
    first = interrupted.run_evaluation_cycle(horizon=EvaluationHorizon.H1, limit=10)

    assert first["signals_evaluated"] == 0
    assert first["errors"] == 1
    with session_local() as session:
        tracker = session.execute(select(StrategyConsecutiveWrongTracker)).scalar_one()
        assert tracker.last_signal_id == 4401
        assert tracker.consecutive_wrong_count == 1
        assert session.execute(select(SignalPerformance)).scalar_one_or_none() is None

    restarted = FeedbackLoopEngine(
        engine=engine,
        wrong_threshold=2,
        price_provider=SqlOHLCPriceProvider(session_local),
    )
    recovered = restarted.run_evaluation_cycle(horizon=EvaluationHorizon.H1, limit=10)

    assert recovered == {
        "signals_evaluated": 1,
        "correct_predictions": 0,
        "wrong_predictions": 1,
        "optimizations_triggered": 0,
        "skipped_no_price": 0,
        "errors": 0,
    }
    with session_local() as session:
        tracker = session.execute(select(StrategyConsecutiveWrongTracker)).scalar_one()
        performance = session.execute(select(SignalPerformance)).scalar_one()
        assert tracker.last_signal_id == 4401
        assert tracker.consecutive_wrong_count == 1
        assert performance.signal_id == 4401
        assert performance.consecutive_wrong_count == 1


def test_late_historical_price_does_not_rewind_tracker_or_feedback_event() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)
    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    source = fixture["source"]
    bars = fixture["bars"]
    older_entry, older_exit = bars[9], bars[68]
    newer_entry, newer_exit = bars[10], bars[69]

    with session_local() as session:
        session.add(
            Strategy(
                strategy_id="late_price_strategy_v1",
                strategy_name="Late price strategy",
                asset_class="crypto",
            )
        )
        session.add(
            Instrument(
                instr_id=55,
                asset_class="crypto",
                canonical="BTCUSDC",
                exchange="coinbase",
                settlement_currency="USDC",
            )
        )
        session.flush()
        session.add_all(
            [
                CanonicalSignal(
                    signal_id=5501,
                    strategy_id="late_price_strategy_v1",
                    instr_id=55,
                    action="long",
                    direction="long",
                    confidence=0.8,
                    entry_price=older_entry["close"],
                    horizon_seconds=3_600,
                    ts=datetime.fromtimestamp(older_entry["ts"], tz=UTC),
                    external_signal_id="late-price-older",
                    signal_meta={
                        "price_source": source,
                        "price_timeframe": "1m",
                    },
                ),
                CanonicalSignal(
                    signal_id=5502,
                    strategy_id="late_price_strategy_v1",
                    instr_id=55,
                    action="long",
                    direction="long",
                    confidence=0.8,
                    entry_price=newer_entry["close"],
                    horizon_seconds=3_600,
                    ts=datetime.fromtimestamp(newer_entry["ts"], tz=UTC),
                    external_signal_id="late-price-newer",
                    signal_meta={
                        "price_source": source,
                        "price_timeframe": "1m",
                    },
                ),
                # Only the newer signal's exact one-hour exit is available in
                # the first cycle. These are Coinbase public-history bars.
                InstrumentPrice(
                    instr_id=55,
                    ts=datetime.fromtimestamp(newer_exit["ts"], tz=UTC),
                    timeframe="1m",
                    close=newer_exit["close"],
                    source=source,
                ),
            ]
        )
        session.commit()

    outbox_store = OutboxStore(session_local)
    feedback = FeedbackLoopEngine(
        engine=engine,
        wrong_threshold=2,
        price_provider=SqlOHLCPriceProvider(session_local),
        outbox_store=outbox_store,
    )
    first = feedback.run_evaluation_cycle(horizon=EvaluationHorizon.H1, limit=10)
    assert first["signals_evaluated"] == 1
    assert first["skipped_no_price"] == 1

    with session_local() as session:
        session.add(
            InstrumentPrice(
                instr_id=55,
                ts=datetime.fromtimestamp(older_exit["ts"], tz=UTC),
                timeframe="1m",
                close=older_exit["close"],
                source=source,
            )
        )
        session.commit()

    second = feedback.run_evaluation_cycle(horizon=EvaluationHorizon.H1, limit=10)
    assert second["signals_evaluated"] == 1
    assert second["wrong_predictions"] == 1
    assert second["errors"] == 0

    with session_local() as session:
        tracker = session.execute(select(StrategyConsecutiveWrongTracker)).scalar_one()
        performance = {
            row.signal_id: row
            for row in session.execute(
                select(SignalPerformance).order_by(SignalPerformance.signal_id)
            ).scalars()
        }
        assert tracker.last_signal_id == 5502
        assert tracker.consecutive_wrong_count == 1
        assert performance[5502].consecutive_wrong_count == 1
        assert performance[5501].consecutive_wrong_count == 0
        assert performance[5501].needs_optimization is False

    events = {
        int(record.payload["signal_id"]): record
        for record in outbox_store.list_pending(topics=["feedback.ready"])
    }
    assert events[5502].payload["consecutive_wrong_count"] == 1
    assert events[5501].payload["consecutive_wrong_count"] == 0
    assert events[5501].payload["needs_optimization"] is False


def test_feedback_cycle_persists_exact_sql_price_observations() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)

    signal_ts = datetime.now(UTC) - timedelta(hours=2)
    target_ts = signal_ts + timedelta(hours=1)
    with session_local() as session:
        session.add(
            Strategy(
                strategy_id="swing_high_low_pmo_v1",
                strategy_name="SwingHighLowPMO",
                asset_class="crypto",
            )
        )
        session.add(
            Instrument(
                instr_id=10,
                asset_class="crypto",
                canonical="BTCUSD",
                exchange="coinbase",
                settlement_currency="USD",
            )
        )
        session.flush()
        session.add(
            CanonicalSignal(
                signal_id=321,
                strategy_id="swing_high_low_pmo_v1",
                instr_id=10,
                action="long",
                direction="long",
                confidence=0.9,
                entry_price=None,
                signal_meta={"price_source": "coinbase_live", "price_timeframe": "1m"},
                horizon_seconds=3600,
                ts=signal_ts,
                run_id="run-feedback-sql-provenance",
                external_signal_id="ext-feedback-sql-provenance",
            )
        )
        session.add_all(
            [
                InstrumentPrice(
                    price_id=31,
                    instr_id=10,
                    ts=signal_ts - timedelta(minutes=1),
                    timeframe="1m",
                    close=100.0,
                    source="coinbase_live",
                ),
                InstrumentPrice(
                    price_id=32,
                    instr_id=10,
                    ts=target_ts - timedelta(minutes=1),
                    timeframe="1m",
                    close=110.0,
                    source="coinbase_live",
                ),
                InstrumentPrice(
                    price_id=33,
                    instr_id=10,
                    ts=target_ts,
                    timeframe="1m",
                    close=999.0,
                    source="coinbase_live",
                ),
            ]
        )
        session.commit()

    feedback_engine = FeedbackLoopEngine(
        engine=engine,
        price_provider=SqlOHLCPriceProvider(session_local),
    )

    result = feedback_engine.run_evaluation_cycle(horizon=EvaluationHorizon.H1, limit=10)

    assert result["signals_evaluated"] == 1
    assert result["errors"] == 0
    with session_local() as session:
        performance = session.execute(select(SignalPerformance)).scalar_one()

    assert performance.meta == {
        "price_provenance_schema": "v1",
        "evaluation_target_ts": target_ts.isoformat(),
        "entry_price_provenance": {
            "price_id": 31,
            "price": 100.0,
            "bar_open_ts": (signal_ts - timedelta(minutes=1)).isoformat(),
            "bar_close_ts": signal_ts.isoformat(),
            "source": "coinbase_live",
            "timeframe": "1m",
            "origin": "prices_table",
        },
        "exit_price_provenance": {
            "price_id": 32,
            "price": 110.0,
            "bar_open_ts": (target_ts - timedelta(minutes=1)).isoformat(),
            "bar_close_ts": target_ts.isoformat(),
            "source": "coinbase_live",
            "timeframe": "1m",
            "origin": "prices_table",
        },
        "execution_lineage_schema": "v1",
        "execution_lineage": {
            "canonical_signal_id": 321,
            "decision_ids": [],
            "decisions": [],
            "account_ids": [],
            "user_ids": [],
            "intent_ids": [],
            "order_ids": [],
            "fill_ids": [],
        },
    }
