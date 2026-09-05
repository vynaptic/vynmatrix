"""PostgreSQL gate for the public-data strategy-to-feedback pipeline."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from execution_engine.api import create_app as create_execution_app
from execution_engine.canonical_execution_store import CanonicalExecutionStore
from execution_engine.engine import ExecutionEngine
from execution_engine.execution_log_store import ExecutionLogStore
from execution_engine.execution_metrics_store import ExecutionMetricsStore
from execution_engine.execution_position_store import ExecutionPositionStore
from execution_engine.market_data import SqlPriceQuoteProvider
from execution_engine.order_builder import OrderBuilder
from execution_engine.risk_breach_store import RiskBreachStore
from feedback_loop_engine.main import create_engine_instance as create_feedback_engine
from feedback_loop_engine.models import EvaluationHorizon
from feedback_loop_engine.optimizer import SuggestionGenerationError
from lib_application.db import models as app_models
from lib_application.db.session import (
    create_engine_for_env,
    dispose_engine,
    get_session_factory,
)
from lib_application.outbox import OutboxStore
from lib_application.services.price_ingestion_service import PriceIngestionService
from lib_common.config_validation import (
    DatabaseConfig,
    ExecutionPaperConfig,
    ExecutionRuntimeConfig,
    RunMode,
    ScoringEngineConfig,
    ScoringMarketContextConfig,
    ScoringRuntimeConfig,
)
from lib_data.market_data import CandleRow
from lib_strategy.signals.emitter import BacktestSignalEmitter, HttpSignalEmitter
from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import MarketState
from lib_strategy.signals.signal import Signal, SignalAction
from scoring_engine.api import create_app as create_scoring_app
from scoring_engine.dispatcher import ExecutionDispatcher
from scoring_engine.main import build_engine as build_scoring_engine
from scoring_engine.outbox_relay import OutboxRelayWorker
from scoring_engine.providers_db import DBProfileProvider, DBStrategyConfigProvider
from scripts.replay_canonical_signals import _run as replay_canonical_signals

_REPO = Path(__file__).resolve().parents[1]
_FIXTURE = _REPO / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json"
_SOURCE = "coinbase_exchange_public"
_SYMBOL = "BTCUSD"
_CANONICAL = "BTC/USD"
_EXPECTED_BARS = 1501
# The gate is driven by the test-owned deterministic exerciser core under
# tests/fixtures/strategies/PipelineExerciser: it replays a pre-registered
# LONG/CLOSE schedule against the real frozen fixture bars with the same emit
# contract the retired scalpers used, so every downstream pipeline assertion
# keeps its meaning without shipping a non-strategy in strategies/indicator.
_EXERCISER_DIR = _REPO / "tests/fixtures/strategies/PipelineExerciser"
_STRATEGIES = (
    ("PipelineExerciserAlpha", "pipeline_exerciser_alpha_v1"),
    ("PipelineExerciserBeta", "pipeline_exerciser_beta_v1"),
)
_FEEDBACK_SIGNAL_WINDOWS = {
    "pipeline_exerciser_alpha_v1": (
        ("2026-06-10T01:12:00+00:00", "2026-06-10T01:22:00+00:00"),
        ("2026-06-10T01:27:00+00:00", "2026-06-10T01:30:00+00:00"),
    ),
    "pipeline_exerciser_beta_v1": (
        ("2026-06-10T01:39:00+00:00", "2026-06-10T01:49:00+00:00"),
        ("2026-06-10T02:20:00+00:00", "2026-06-10T03:04:00+00:00"),
    ),
}


def _exerciser_config(strategy_id: str) -> dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "strategy_version": "1.0.0",
        "description": "Deterministic pipeline exerciser (test harness, no alpha claim).",
        "parameters": {
            "universe": _SYMBOL,
            "asset_class": "crypto",
            "trade_direction_mode": "long_only",
            "take_profit_pct": "0.004",
            "stop_loss_pct": "0.006",
            "signal_ttl_seconds": "60",
            "confidence": "0.7",
            # Inert for the exerciser core, but a supported optimizer knob so
            # the consecutive-wrong suggestion path stays exercisable.
            "ema_period": "50",
            "schedule": [list(pair) for pair in _FEEDBACK_SIGNAL_WINDOWS[strategy_id]],
        },
    }


_SIGNALS_PER_STRATEGY = 4
_TOTAL_SIGNALS = len(_STRATEGIES) * _SIGNALS_PER_STRATEGY


@dataclass(frozen=True)
class _Route:
    user_id: str
    account_id: int


def _load_public_candles() -> tuple[dict[str, Any], list[MarketState]]:
    payload = json.loads(_FIXTURE.read_text())
    assert payload["product"] == "BTC-USD"
    assert payload["source"] == _SOURCE
    assert payload["granularity_seconds"] == 60
    assert payload["bar_count"] == _EXPECTED_BARS
    rows = payload["bars"]
    assert isinstance(rows, list)
    assert len(rows) == _EXPECTED_BARS
    bars = [
        MarketState(
            symbol=_SYMBOL,
            timestamp=datetime.fromtimestamp(row["ts"], tz=UTC),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            metadata={"price_source": _SOURCE, "price_timeframe": "1m"},
        )
        for row in rows
    ]
    return payload, bars


def _strategy_pairs(strategy_id: str, bars: list[MarketState]) -> tuple[Signal, ...]:
    config = _exerciser_config(strategy_id)
    assert config["strategy_id"] == strategy_id
    emitter = BacktestSignalEmitter()
    parameters = dict(config["parameters"])
    parameters["strategy_version"] = config["strategy_version"]
    strategy = load_pure_strategy_core(_EXERCISER_DIR)(
        strategy_id=strategy_id,
        config=parameters,
        emitter=emitter,
    )
    strategy.initialize()
    for bar in bars:
        strategy.record_bar(bar.symbol)
        strategy.on_data(bar)

    signals = emitter.get_signals()
    selected: list[Signal] = []
    for entry_iso, close_iso in _FEEDBACK_SIGNAL_WINDOWS[strategy_id]:
        entry_ts = datetime.fromisoformat(entry_iso)
        close_ts = datetime.fromisoformat(close_iso)
        entry_index = next(
            index
            for index, signal in enumerate(signals)
            if signal.action == SignalAction.LONG and signal.timestamp == entry_ts
        )
        entry = signals[entry_index]
        close = next(
            signal
            for signal in signals[entry_index + 1 :]
            if signal.action == SignalAction.CLOSE and signal.timestamp == close_ts
        )
        selected.extend((entry, close))

    observed_timestamps = {bar.timestamp for bar in bars}
    for signal in selected:
        assert signal.external_signal_id
        assert signal.timestamp in observed_timestamps
        assert signal.metadata["price_source"] == _SOURCE
        assert signal.metadata["price_timeframe"] == "1m"
        assert signal.expires_at == signal.timestamp + timedelta(minutes=1)
    assert len(selected) == _SIGNALS_PER_STRATEGY
    return tuple(selected)


def _provision_catalogue(
    session_factory: Any,
    *,
    strategy_configs: dict[str, dict[str, Any]],
) -> tuple[int, dict[str, _Route]]:
    with session_factory() as session:
        instrument = (
            session.query(app_models.Instrument)
            .filter(
                app_models.Instrument.asset_class == "crypto",
                app_models.Instrument.canonical == _CANONICAL,
            )
            .one_or_none()
        )
        if instrument is None:
            instrument = app_models.Instrument(
                asset_class="crypto",
                canonical=_CANONICAL,
                settlement_currency="USD",
                is_tradable=True,
                market_session_policy="continuous",
            )
            session.add(instrument)
            session.flush()
        if (
            session.query(app_models.InstrumentAlias)
            .filter(app_models.InstrumentAlias.alias == _SYMBOL)
            .one_or_none()
            is None
        ):
            session.add(
                app_models.InstrumentAlias(
                    instr_id=instrument.instr_id,
                    alias=_SYMBOL,
                    source="canonical",
                )
            )

        sector = (
            session.query(app_models.Sector)
            .filter(app_models.Sector.code == "crypto")
            .one_or_none()
        )
        if sector is None:
            sector = app_models.Sector(code="crypto", name="Crypto", asset_class="crypto")
            session.add(sector)
            session.flush()
        if (
            session.get(
                app_models.InstrumentSector,
                (instrument.instr_id, sector.sector_id),
            )
            is None
        ):
            session.add(
                app_models.InstrumentSector(
                    instr_id=instrument.instr_id,
                    sector_id=sector.sector_id,
                    weight=Decimal("1"),
                )
            )

        broker = (
            session.query(app_models.Broker).filter(app_models.Broker.code == "paper").one_or_none()
        )
        if broker is None:
            broker = app_models.Broker(
                code="paper",
                name="Local Paper Broker",
                capabilities={"asset_classes": ["crypto"]},
            )
            session.add(broker)
            session.flush()

        routes: dict[str, _Route] = {}
        for index, (strategy_name, strategy_id) in enumerate(_STRATEGIES, start=1):
            config = strategy_configs[strategy_id]
            session.add(
                app_models.Strategy(
                    strategy_id=strategy_id,
                    strategy_name=strategy_name,
                    asset_class="crypto",
                    description=config["description"],
                    is_active=True,
                )
            )
            session.flush()
            session.add(
                app_models.StrategyVersion(
                    strategy_id=strategy_id,
                    semver=config["strategy_version"],
                    param_schema={},
                    default_params={
                        **config["parameters"],
                        "strategy_version": config["strategy_version"],
                    },
                    status="active",
                )
            )

            user_id = f"ci-public-pipeline-{index}"
            session.add(
                app_models.User(
                    user_id=user_id,
                    email=f"{user_id}@example.invalid",
                    full_name=f"Public Pipeline Tenant {index}",
                    tz="UTC",
                    base_ccy="USD",
                    status="active",
                )
            )
            session.flush()
            account = app_models.LinkedBrokerAccount(
                user_id=user_id,
                broker_id=broker.broker_id,
                environment="paper",
                display_name=f"CI public-data paper account {index}",
                external_ref=f"ci-public-pipeline:{index}",
                base_ccy="USD",
                status="connected",
                paper_initial_equity=Decimal("100000"),
                paper_initial_cash=Decimal("100000"),
            )
            session.add(account)
            session.flush()
            sizing = app_models.SizingProfile(
                user_id=user_id,
                name="Public-data fixed percentage",
                method="fixed_pct",
                params={"fixed_pct": 0.02},
                is_default=True,
            )
            session.add(sizing)
            session.flush()
            session.add(
                app_models.UserStrategyBinding(
                    user_id=user_id,
                    strategy_id=strategy_id,
                    broker_account_id=account.account_id,
                    asset_score_threshold=Decimal("0"),
                    execution_modes_allowed=["spot"],
                    preferred_mode="spot",
                    mode_selection_policy="fixed",
                    asset_classes_allowed=["crypto"],
                    instruments_allowed=[instrument.canonical],
                    sizing_profile_id=sizing.profile_id,
                    max_position_pct=Decimal("0.10"),
                    max_daily_loss_pct=Decimal("0.05"),
                    max_open_positions=5,
                    allowed_brokers=["paper"],
                    is_active=True,
                    autopilot=True,
                    entries_enabled=True,
                    exits_enabled=True,
                )
            )
            routes[strategy_id] = _Route(user_id=user_id, account_id=account.account_id)

        session.commit()
        return int(instrument.instr_id), routes


def _normal_execution_engine(session_factory: Any) -> ExecutionEngine:
    return ExecutionEngine(
        order_builder=OrderBuilder(),
        market_data_provider=SqlPriceQuoteProvider(session_factory=session_factory),
        execution_log_store=ExecutionLogStore(session_factory=session_factory),
        execution_metrics_store=ExecutionMetricsStore(session_factory=session_factory),
        execution_position_store=ExecutionPositionStore(session_factory=session_factory),
        risk_breach_store=RiskBreachStore(session_factory=session_factory),
        outbox_store=OutboxStore(session_factory),
        canonical_execution_store=CanonicalExecutionStore(session_factory=session_factory),
        default_mode="paper",
        allow_live=False,
        runtime_config=ExecutionRuntimeConfig(paper=ExecutionPaperConfig(use_local_broker=True)),
        session_factory=session_factory,
    )


async def _relay_historical_commands(store: Any, engine: ExecutionEngine) -> int:
    transport = httpx.ASGITransport(app=create_execution_app(engine))
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://execution.test",
        ) as client:
            worker = OutboxRelayWorker(
                store=store,
                exec_engine_url="http://execution.test",
                http_client=client,
            )
            return await worker.drain_once(topics=["execution.commands"], limit=10)
    finally:
        await engine.close()


def _replay_args(route: _Route, strategy_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        user_id=route.user_id,
        broker_account_id=route.account_id,
        strategy_id=strategy_id,
        symbols=_SYMBOL,
        start_date="2026-06-10",
        end_date="2026-06-11",
        timeframe="15m",
        source=_SOURCE,
        max_signals=None,
        position_size_pct=0.02,
        max_position_pct=0.10,
        max_open_positions=5,
        max_daily_trades=50,
        require_minute_data=True,
        enable_shorting=False,
        require_stop_loss=True,
    )


def _ingest_public_prices(
    session_factory: Any,
    *,
    instr_id: int,
    rows: list[dict[str, Any]],
) -> None:
    candles = [
        CandleRow(
            instr_id=instr_id,
            ts=datetime.fromtimestamp(row["ts"], tz=UTC),
            timeframe="1m",
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            source=_SOURCE,
        )
        for row in rows
    ]
    assert PriceIngestionService(session_factory).upsert_candles(candles) == _EXPECTED_BARS


def _build_scoring_surface(database_url: str, session_factory: Any) -> Any:
    scoring = build_scoring_engine(
        ScoringEngineConfig(
            mode=RunMode.PAPER,
            database=DatabaseConfig(url=database_url),
            execution_engine_url="http://execution.test",
            runtime=ScoringRuntimeConfig(
                bindings_cache_ttl_seconds=0,
                market_context=ScoringMarketContextConfig(
                    source=_SOURCE,
                    timeframe="1m",
                    window=20,
                    max_age_seconds=60,
                ),
            ),
        )
    )
    dispatcher = ExecutionDispatcher(
        "http://execution.test",
        profile_provider=DBProfileProvider(session_factory),
        strategy_config_provider=DBStrategyConfigProvider(session_factory),
        store=scoring.store,
        runtime_mode="paper",
    )
    app = create_scoring_app(
        scoring,
        dispatcher=dispatcher,
    )
    return scoring, app


def _post_signal_pairs(app: Any, signal_pairs: dict[str, tuple[Signal, ...]]) -> None:
    wire_emitter = HttpSignalEmitter(base_url="http://scoring.test")
    with TestClient(app) as client:
        for strategy_id, signals in signal_pairs.items():
            for signal in signals:
                response = client.post(
                    "/api/v1/signals",
                    json=wire_emitter._build_payload(signal),
                )
                assert response.status_code == 200, (strategy_id, response.text)


def _assert_scoring_handoff(db_engine: Any, routes: dict[str, _Route]) -> None:
    strategy_ids = list(routes)
    with Session(db_engine) as session:
        canonical = session.scalars(
            select(app_models.CanonicalSignal)
            .where(app_models.CanonicalSignal.strategy_id.in_(strategy_ids))
            .order_by(app_models.CanonicalSignal.ts)
        ).all()
        assert len(canonical) == _TOTAL_SIGNALS
        assert {row.action for row in canonical} == {"long", "flat"}
        assert all(
            row.signal_meta["price_source"] == _SOURCE
            and row.signal_meta["price_timeframe"] == "1m"
            and row.strat_ver_id is not None
            for row in canonical
        )
        decisions = session.scalars(
            select(app_models.ExecutionDecisionLog).where(
                app_models.ExecutionDecisionLog.user_id.in_(
                    [route.user_id for route in routes.values()]
                )
            )
        ).all()
        assert len(decisions) == _TOTAL_SIGNALS
        assert all(row.should_execute for row in decisions)
        commands = session.scalars(
            select(app_models.OutboxEvent).where(
                app_models.OutboxEvent.topic == "execution.commands",
                app_models.OutboxEvent.aggregate_id.like("%ci-public-pipeline%"),
            )
        ).all()
        assert len(commands) == _TOTAL_SIGNALS
        assert all(row.status == "pending" for row in commands)


def _assert_normal_historical_safety(
    db_engine: Any,
    *,
    account_ids: list[int],
) -> None:
    with Session(db_engine) as session:
        assert (
            session.scalar(
                select(app_models.Execution)
                .join(app_models.Order)
                .where(app_models.Order.account_id.in_(account_ids))
                .limit(1)
            )
            is None
        )
        command_statuses = session.scalars(
            select(app_models.OutboxEvent.status).where(
                app_models.OutboxEvent.topic == "execution.commands",
                app_models.OutboxEvent.aggregate_id.like("%ci-public-pipeline%"),
            )
        ).all()
        assert command_statuses == ["published"] * _TOTAL_SIGNALS


def _run_replays(routes: dict[str, _Route]) -> None:
    for strategy_id, route in routes.items():
        result = asyncio.run(replay_canonical_signals(_replay_args(route, strategy_id)))
        assert result["signals_processed"] == _SIGNALS_PER_STRATEGY
        assert result["signals_skipped_missing_price"] == 0
        assert result["executed_results"] == _SIGNALS_PER_STRATEGY
        assert result["blocked_results"] == 0
        assert result["failed_results"] == 0
        assert result["orders_filled"] == _SIGNALS_PER_STRATEGY
        assert result["ending_equity"] != result["starting_equity"]
        aggregate = result["aggregate_pnl"]
        realized_pnl = float(aggregate["total_realized_pnl"])
        assert math.isfinite(realized_pnl)
        assert realized_pnl != 0
        assert aggregate["total_unrealized_pnl"] == 0
        assert aggregate["position_count"] == 0
        assert aggregate["complete"] is True


def _assert_exact_ledger(
    db_engine: Any,
    *,
    routes: dict[str, _Route],
    public_rows: list[dict[str, Any]],
) -> None:
    account_ids = [route.account_id for route in routes.values()]
    opens = {datetime.fromtimestamp(row["ts"], tz=UTC): float(row["open"]) for row in public_rows}
    with Session(db_engine) as session:
        fill_rows = session.execute(
            select(
                app_models.Execution,
                app_models.Order,
                app_models.OrderIntent,
                app_models.CanonicalSignal,
            )
            .join(app_models.Order, app_models.Execution.order_id == app_models.Order.order_id)
            .join(
                app_models.OrderIntent,
                app_models.Order.intent_id == app_models.OrderIntent.intent_id,
            )
            .join(
                app_models.CanonicalSignal,
                app_models.OrderIntent.canonical_signal_id == app_models.CanonicalSignal.signal_id,
            )
            .where(app_models.Order.account_id.in_(account_ids))
            .order_by(app_models.CanonicalSignal.strategy_id, app_models.CanonicalSignal.ts)
        ).all()
        assert len(fill_rows) == _TOTAL_SIGNALS
        assert len({execution.trade_id for execution, *_rest in fill_rows}) == _TOTAL_SIGNALS
        for execution, order, intent, canonical in fill_rows:
            route = routes[canonical.strategy_id]
            assert order.account_id == route.account_id == intent.account_id
            assert intent.user_id == route.user_id
            assert execution.venue == "paper"
            assert execution.fee_ccy == "USD"
            assert execution.qty > 0
            assert execution.fee_amount > 0
            signal_ts = canonical.ts.replace(tzinfo=UTC)
            rounded = signal_ts.replace(second=0, microsecond=0)
            remainder = rounded.minute % 15
            fill_ts = rounded + timedelta(minutes=15 if remainder == 0 else 15 - remainder)
            reference = opens[fill_ts]
            slip = 1.001 if intent.side == "BUY" else 0.999
            assert float(execution.price) == pytest.approx(reference * slip, abs=1e-7)
            assert float(execution.fee_amount) == pytest.approx(
                float(execution.qty) * float(execution.price) * 0.001,
                abs=1e-7,
            )

        open_positions = session.scalar(
            select(func.count())
            .select_from(app_models.Position)
            .where(app_models.Position.account_id.in_(account_ids))
        )
        filled_orders = session.scalar(
            select(func.count())
            .select_from(app_models.PendingOrder)
            .where(
                app_models.PendingOrder.broker_account_id.in_(account_ids),
                app_models.PendingOrder.status == "filled",
            )
        )
        executed_logs = session.scalars(
            select(app_models.ExecutionLog).where(
                app_models.ExecutionLog.account_id.in_(account_ids),
                app_models.ExecutionLog.status == "executed",
            )
        ).all()
        execution_metrics = session.scalars(
            select(app_models.ExecutionMetric).where(
                app_models.ExecutionMetric.account_id.in_(account_ids),
                app_models.ExecutionMetric.orders_filled > 0,
            )
        ).all()
        realized_contributions = [
            contribution
            for metric in execution_metrics
            for contribution in (metric.metadata_json or {}).get(
                "realized_pnl_contributions",
                [],
            )
        ]
        long_signal_ids = {
            int(canonical.signal_id)
            for _execution, _order, _intent, canonical in fill_rows
            if canonical.action == "long"
        }
        close_signal_ids = {
            int(canonical.signal_id)
            for _execution, _order, _intent, canonical in fill_rows
            if canonical.action == "flat"
        }
        assert open_positions == 0
        assert filled_orders == _TOTAL_SIGNALS
        assert len(executed_logs) == _TOTAL_SIGNALS
        assert all(row.canonical_signal_id is not None for row in executed_logs)
        assert len(execution_metrics) == _TOTAL_SIGNALS
        assert len(realized_contributions) == len(_STRATEGIES) * 2
        assert {int(item["entry_canonical_signal_id"]) for item in realized_contributions}.issubset(
            long_signal_ids
        )
        assert {int(item["exit_canonical_signal_id"]) for item in realized_contributions}.issubset(
            close_signal_ids
        )
        assert all(
            Decimal(item["deployed_capital"]) > 0 and item["account_currency"] == "USD"
            for item in realized_contributions
        )


def _exercise_feedback_recovery(
    *,
    feedback_database_url: str,
    interrupted_strategy: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, int], dict[str, int], int, bool]:
    with monkeypatch.context() as feedback_env:
        feedback_env.setenv("DATABASE_URL", feedback_database_url)
        feedback, _review, _feedback_sessions, feedback_db_engine = create_feedback_engine()
        original_generate = feedback._generate_and_persist_optimization
        interruption_injected = False

        def _interrupt_one_post_tracker_suggestion(
            strategy_id: str,
            strat_ver_id: int | None,
            instr_id: int,
            horizon: EvaluationHorizon,
            consecutive_wrong: int,
        ) -> int:
            nonlocal interruption_injected
            if strategy_id == interrupted_strategy and not interruption_injected:
                interruption_injected = True
                message = "injected crash boundary after durable tracker commit"
                raise SuggestionGenerationError(message)
            return original_generate(
                strategy_id,
                strat_ver_id,
                instr_id,
                horizon,
                consecutive_wrong,
            )

        try:
            feedback._generate_and_persist_optimization = (  # type: ignore[method-assign]
                _interrupt_one_post_tracker_suggestion
            )
            first_result = feedback.run_evaluation_cycle(EvaluationHorizon.H1, limit=100)
            feedback._generate_and_persist_optimization = original_generate  # type: ignore[method-assign]
            recovery_result = feedback.run_evaluation_cycle(EvaluationHorizon.H1, limit=100)
            mode_rows_written = feedback.update_mode_performance()
            return (
                first_result,
                recovery_result,
                mode_rows_written,
                interruption_injected,
            )
        finally:
            dispose_engine(feedback_db_engine)


def _run_and_assert_feedback(
    db_engine: Any,
    strategy_ids: list[str],
    *,
    feedback_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_result, recovery_result, mode_rows_written, interruption_injected = (
        _exercise_feedback_recovery(
            feedback_database_url=feedback_database_url,
            interrupted_strategy=strategy_ids[0],
            monkeypatch=monkeypatch,
        )
    )
    assert interruption_injected is True
    assert first_result["signals_evaluated"] == len(_STRATEGIES) * 2
    assert first_result["wrong_predictions"] == len(_STRATEGIES) * 2
    assert first_result["optimizations_triggered"] == len(_STRATEGIES) - 1
    assert first_result["skipped_no_price"] == 0
    assert first_result["errors"] == 1
    assert recovery_result == {
        "signals_evaluated": 0,
        "correct_predictions": 0,
        "wrong_predictions": 0,
        "optimizations_triggered": 1,
        "skipped_no_price": 0,
        "errors": 0,
    }
    assert mode_rows_written == len(_STRATEGIES)

    with Session(db_engine) as session:
        performance = session.scalars(
            select(app_models.SignalPerformance).where(
                app_models.SignalPerformance.strategy_id.in_(strategy_ids),
                app_models.SignalPerformance.evaluation_horizon == "1h",
            )
        ).all()
        assert len(performance) == len(_STRATEGIES) * 2
        assert all(row.did_execute is True for row in performance)
        assert sum(bool(row.needs_optimization) for row in performance) == len(_STRATEGIES)
        for row in performance:
            entry = row.meta["entry_price_provenance"]
            exit_ = row.meta["exit_price_provenance"]
            assert entry["source"] == exit_["source"] == _SOURCE
            assert entry["timeframe"] == exit_["timeframe"] == "1m"
            assert entry["origin"] == "canonical_signal"
            assert entry["price_id"] is None
            assert exit_["origin"] == "prices_table"
            assert exit_["price_id"] is not None

        trackers = session.scalars(
            select(app_models.StrategyConsecutiveWrongTracker).where(
                app_models.StrategyConsecutiveWrongTracker.strategy_id.in_(strategy_ids),
                app_models.StrategyConsecutiveWrongTracker.horizon == "1h",
            )
        ).all()
        assert len(trackers) == len(_STRATEGIES)
        assert all(
            tracker.threshold_reached
            and tracker.consecutive_wrong_count == 2
            and tracker.feedback_id is not None
            for tracker in trackers
        )

        suggestions = session.scalars(
            select(app_models.StrategyParameterFeedback).where(
                app_models.StrategyParameterFeedback.strategy_id.in_(strategy_ids),
                app_models.StrategyParameterFeedback.horizon == "1h",
            )
        ).all()
        assert len(suggestions) == len(_STRATEGIES)
        assert len({suggestion.feedback_id for suggestion in suggestions}) == len(_STRATEGIES)
        suggestions_by_id = {row.feedback_id: row for row in suggestions}
        for tracker in trackers:
            suggestion = suggestions_by_id[tracker.feedback_id]
            assert suggestion.strategy_id == tracker.strategy_id
            assert suggestion.strat_ver_id == tracker.strat_ver_id
            assert suggestion.instr_id == tracker.instr_id
            assert suggestion.status == "pending"
            assert suggestion.trigger_reason == "consecutive_wrong"
            assert suggestion.current_params
            assert suggestion.suggested_params
            assert suggestion.current_params != suggestion.suggested_params
            assert suggestion.supporting_data["changed_parameters"]

        mode_performance = session.scalars(
            select(app_models.ModePerformance).where(
                app_models.ModePerformance.strategy_id.in_(strategy_ids),
            )
        ).all()
        assert len(mode_performance) == len(_STRATEGIES)
        assert all(
            row.execution_mode == "spot"
            and row.horizon == "intraday"
            and row.sample_size == 2
            and math.isfinite(float(row.total_return))
            for row in mode_performance
        )

        feedback_events = session.scalars(
            select(app_models.OutboxEvent).where(
                app_models.OutboxEvent.topic == "feedback.ready",
                app_models.OutboxEvent.ordering_key.in_(strategy_ids),
            )
        ).all()
        assert len(feedback_events) == len(_STRATEGIES) * 2
        assert sum(bool(row.payload["needs_optimization"]) for row in feedback_events) == len(
            _STRATEGIES
        )


@pytest.mark.integration
def test_public_coinbase_strategies_complete_account_scoped_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove both retained cores through exact paper fills and feedback."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the PostgreSQL pipeline gate")
    feedback_database_url = os.getenv("FEEDBACK_DATABASE_URL")
    if not feedback_database_url:
        pytest.skip("FEEDBACK_DATABASE_URL is required for the least-privilege feedback gate")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("WRONG_THRESHOLD", "2")

    payload, bars = _load_public_candles()
    strategy_configs = {
        strategy_id: _exerciser_config(strategy_id) for _name, strategy_id in _STRATEGIES
    }
    signal_pairs = {
        strategy_id: _strategy_pairs(strategy_id, bars) for _name, strategy_id in _STRATEGIES
    }
    db_engine = create_engine_for_env(db_url=database_url)
    session_factory = get_session_factory(engine=db_engine)
    scoring = None
    try:
        instr_id, routes = _provision_catalogue(
            session_factory,
            strategy_configs=strategy_configs,
        )
        _ingest_public_prices(session_factory, instr_id=instr_id, rows=payload["bars"])
        scoring, scoring_app = _build_scoring_surface(database_url, session_factory)
        _post_signal_pairs(scoring_app, signal_pairs)
        _assert_scoring_handoff(db_engine, routes)

        account_ids = [route.account_id for route in routes.values()]
        relayed = asyncio.run(
            _relay_historical_commands(
                scoring.store,
                _normal_execution_engine(session_factory),
            )
        )
        assert relayed == _TOTAL_SIGNALS
        _assert_normal_historical_safety(db_engine, account_ids=account_ids)

        _run_replays(routes)
        _assert_exact_ledger(db_engine, routes=routes, public_rows=payload["bars"])
        _run_and_assert_feedback(
            db_engine,
            list(routes),
            feedback_database_url=feedback_database_url,
            monkeypatch=monkeypatch,
        )
    finally:
        if scoring is not None:
            dispose_engine(scoring.store._engine)
        dispose_engine(db_engine)
