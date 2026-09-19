"""Actual native decisions through isolated historical paper execution and feedback."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from backend.api import create_app as create_backend_app
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from execution_engine.engine import ExecutionEngine
from feedback_loop_engine.main import create_engine_instance as create_feedback_engine
from feedback_loop_engine.models import EvaluationHorizon
from lib_application.db import models
from lib_application.db.session import create_engine_for_env, dispose_engine, get_session_factory
from lib_application.services.price_ingestion_service import PriceIngestionService
from lib_common.runner_utils import build_strategy_core_parameters
from lib_data.market_data import CandleRow
from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import MarketState
from lib_strategy.signals.signal import Signal, SignalAction
from scripts.replay_canonical_signals import _run as replay_canonical_signals
from tests.test_public_strategy_pipeline_postgres_integration import (
    _build_scoring_surface,
    _normal_execution_engine,
    _post_signal_pairs,
    _provision_catalogue,
    _relay_historical_commands,
)

_ROOT = Path(__file__).resolve().parents[1]
_INPUT = _ROOT / "tests/fixtures/market_data/coinbase_btcusd_native_ports_pipeline.json"
_INPUT_SHA = "e5c5033c4b323662eb9464a7a829eeb185cef2420b68712c6b6c3eb71db16fde"
_SOURCE = "coinbase_live"
_SYMBOL = "BTC-USD"
_OWNER = "ci-native-ports-owner"
_NO_STOP = "bb_squeeze_breakout_v1"
_PAIRS = {
    "ATRBreakout": ("2020-07-28", "2020-08-04"),
    "AdaptiveVWAPMeanReversion": ("2019-11-16", "2019-11-22"),
    "BBSqueezeBreakout": ("2020-01-07", "2020-01-26"),
    "EngulfingPattern": ("2022-06-16", "2022-06-17"),
    "HurstRegime": ("2019-11-19", "2019-11-23"),
    "TimeDecayAdaptiveEMA": ("2019-10-26", "2019-11-09"),
    "VolatilityClusteringReversion": ("2020-02-21", "2020-02-27"),
    "VortexTrendCapture": ("2019-12-23", "2020-01-03"),
    "WilliamsPullback": ("2020-01-04", "2020-02-26"),
    "ZScoreMeanReversion": ("2020-01-01", "2020-01-03"),
}


def _inputs() -> dict[str, Any]:
    assert hashlib.sha256(_INPUT.read_bytes()).hexdigest() == _INPUT_SHA
    payload = json.loads(_INPUT.read_text())
    assert payload["source"] == _SOURCE
    product = payload["product_metadata"]
    assert product["requested_product_id"] == product["canonical_book"] == _SYMBOL
    assert product["quote_currency_id"] == "USD"
    assert len(payload["daily"]["bars"]) == 1000
    assert len(payload["minute"]["bars"]) == 304
    return payload


def _cases(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, tuple[Signal, ...]]]:
    states = [
        MarketState(
            symbol=_SYMBOL,
            timestamp=datetime.fromtimestamp(int(row["start"]), UTC) + timedelta(days=1),
            **{key: float(row[key]) for key in ("open", "high", "low", "close", "volume")},
            metadata={"price_source": _SOURCE, "price_timeframe": "1d"},
        )
        for row in payload["daily"]["bars"]
    ]
    configs, pairs = {}, {}
    for name, dates in _PAIRS.items():
        directory = _ROOT / "strategies/indicator" / name
        config = json.loads((directory / "config.json").read_text())
        assert config["enabled"] is False
        parameters = build_strategy_core_parameters(config, signal_source="paper")
        # Instrument selection and spot direction are explicit fixture authority.
        parameters.update(universe=_SYMBOL, trade_direction_mode="long_only")
        core = load_pure_strategy_core(directory)(
            strategy_id=config["strategy_id"], strategy_type="indicator", config=parameters
        )
        signals = core.run(states)
        entry, close = signals[:2]
        assert (entry.action, close.action) == (SignalAction.LONG, SignalAction.CLOSE)
        assert (entry.timestamp.date().isoformat(), close.timestamp.date().isoformat()) == dates
        assert entry.external_signal_id
        assert close.external_signal_id
        assert all(signal.horizon == "5d" and signal.horizon_days == 5 for signal in signals)
        assert all(signal.metadata["price_timeframe"] == "1d" for signal in signals)
        configs[config["strategy_id"]] = {
            **config,
            "parameters": parameters,
            "trade_direction_mode": "long_only",
        }
        pairs[config["strategy_id"]] = (entry, close)
    return configs, pairs


def _require_isolated_urls(database_url: str, feedback_url: str) -> None:
    primary, feedback = make_url(database_url), make_url(feedback_url)
    if (
        primary.get_backend_name() != "postgresql"
        or primary.host not in {"localhost", "127.0.0.1", "::1"}
        or not re.fullmatch(r"vm_ports_pipeline_test_[a-z0-9_]{1,32}", primary.database or "")
        or (primary.host, primary.port, primary.database)
        != (feedback.host, feedback.port, feedback.database)
        or feedback.username != "vm_feedback_login"
    ):
        msg = "Native pipeline requires matching local, explicitly isolated test database URLs"
        raise ValueError(msg)


def _assert_owner_ui(database_url: str, backend_url: str, strategy_ids: set[str]) -> None:
    primary, backend = make_url(database_url), make_url(backend_url)
    assert backend.get_backend_name() == "postgresql"
    assert backend.username == "vm_backend_login"
    assert (primary.host, primary.port, primary.database) == (
        backend.host,
        backend.port,
        backend.database,
    )
    engine = create_engine_for_env(db_url=backend_url)
    engine.update_execution_options(postgresql_readonly=True)
    try:
        app = create_backend_app(
            session_factory=get_session_factory(engine=engine),
            admin_api_key="native-port-test-only",
            allow_anon=False,
        )
        with TestClient(app) as client:
            assert client.get("/api/ui/strategies").status_code == 401
            headers = {"X-Admin-Key": "native-port-test-only"}
            responses = {
                page: client.get(f"/api/ui/{page}", headers=headers)
                for page in ("overview", "strategies", "pnl", "fills")
            }
            assert all(response.status_code == 200 for response in responses.values())
            overview = responses["overview"].json()
            assert overview["safety"]["execution_mode"] == "paper"
            assert overview["safety"]["allow_live"] is False
            assert len(overview["accounts"]) == 10
            rows = responses["strategies"].json()["strategies"]
            assert {row["strategy_id"] for row in rows} == strategy_ids
            assert all(row["last_signal"]["action"] == "flat" for row in rows)
            assert all(len(row["bindings"]) == 1 for row in rows)
            assert all(row["realized_pnl"] is not None for row in rows)
            assert sum(bool(row["realized_pnl"]) for row in rows) == 9
            fills = responses["fills"].json()
            assert len(fills["fills"]) == 18
            assert fills["next_before"] is None
            assert len(responses["pnl"].json()["accounts"]) == 10
            assert client.get("/ui/").status_code == 200
    finally:
        dispose_engine(engine)


def _ingest(factory: Any, instr_id: int, payload: dict[str, Any]) -> None:
    for key, timeframe in (("daily", "1d"), ("minute", "1m")):
        candles = [
            CandleRow(
                instr_id=instr_id,
                ts=datetime.fromtimestamp(int(row["start"]), UTC),
                timeframe=timeframe,
                source=_SOURCE,
                **{
                    field: float(row[field]) for field in ("open", "high", "low", "close", "volume")
                },
            )
            for row in payload[key]["bars"]
        ]
        assert PriceIngestionService(factory).upsert_candles(candles) == len(candles)


def _replay_args(route: Any, strategy_id: str, signals: tuple[Signal, ...]) -> argparse.Namespace:
    return argparse.Namespace(
        user_id=route.user_id,
        broker_account_id=route.account_id,
        strategy_id=strategy_id,
        symbols=_SYMBOL,
        start_date=signals[0].timestamp.date().isoformat(),
        end_date=(signals[-1].timestamp + timedelta(days=1)).date().isoformat(),
        timeframe="15m",
        source=_SOURCE,
        max_signals=None,
        require_minute_data=True,
        enable_shorting=False,
        require_stop_loss=True,
    )


def _assert_scoring_handoff(engine: Any, routes: dict[str, Any]) -> None:
    with Session(engine) as session:
        canonical = session.scalars(select(models.CanonicalSignal)).all()
        assert len(canonical) == 20
        assert {row.action for row in canonical} == {"long", "flat"}
        assert all(row.horizon_seconds == 432000 for row in canonical)
        assert all(row.signal_meta["price_timeframe"] == "1d" for row in canonical)
        decisions = session.scalars(select(models.ExecutionDecisionLog)).all()
        signals_by_id = {row.signal_id: row for row in canonical}
        assert len(decisions) == 20
        assert {row.canonical_signal_id for row in decisions} == set(signals_by_id)
        for decision in decisions:
            assert decision.should_execute is True
            assert decision.lineage_schema_version == "v1"
            route = routes[signals_by_id[decision.canonical_signal_id].strategy_id]
            assert decision.user_id == route.user_id
            assert decision.broker_account_id == route.account_id
        commands = session.scalars(
            select(models.OutboxEvent).where(models.OutboxEvent.topic == "execution.commands")
        ).all()
        assert len(commands) == 20
        assert all(row.status == "pending" for row in commands)


def _execution_ids(engine: Any) -> dict[str, set[Any]]:
    with Session(engine) as session:
        return {
            model.__tablename__: set(session.scalars(select(model.__mapper__.primary_key[0])))
            for model in (
                models.ExecutionLog,
                models.ExecutionDecisionLog,
                models.OrderIntent,
                models.Order,
                models.Execution,
                models.RiskBreach,
            )
        }


def _assert_missing_stop(engine: Any, route: Any) -> None:
    with Session(engine) as session:
        breach = session.scalars(
            select(models.RiskBreach).where(
                models.RiskBreach.broker_account_id == route.account_id,
                models.RiskBreach.rule_code == "stop_loss_required",
            )
        ).one()
        assert breach.user_id == route.user_id
        assert breach.severity == "block"
        entry = session.scalars(
            select(models.ExecutionLog).where(
                models.ExecutionLog.account_id == route.account_id,
                models.ExecutionLog.error_message == "Entry signals must include stop_loss",
            )
        ).one()
        assert entry.status == entry.execution_mode == "blocked"
        closes = session.scalars(
            select(models.ExecutionLog).where(
                models.ExecutionLog.account_id == route.account_id,
                models.ExecutionLog.status == "no_op",
            )
        ).all()
        assert closes
        assert all(row.execution_details["reason"] == "close_no_position" for row in closes)


def _assert_retry(engine: Any, args: argparse.Namespace, monkeypatch: Any) -> None:
    before_retry = _execution_ids(engine)
    retry_results = []
    real_handle = ExecutionEngine.handle_signal

    async def record_result(self, *args, **kwargs):
        result = await real_handle(self, *args, **kwargs)
        retry_results.append(result)
        return result

    with monkeypatch.context() as retry:
        retry.setattr(ExecutionEngine, "handle_signal", record_result)
        repeated = asyncio.run(replay_canonical_signals(args))
    assert repeated["signals_processed"] == repeated["blocked_results"] == 2
    assert repeated["signals_skipped_missing_price"] == repeated["failed_results"] == 0
    assert repeated["executed_results"] == repeated["orders_filled"] == 0
    assert len(retry_results) == 2
    assert all(row.success and row.execution_mode == "dedup" for row in retry_results)
    assert all(row.orders_submitted == row.orders_filled == 0 for row in retry_results)
    assert _execution_ids(engine) == before_retry


def _assert_fills(engine: Any, routes: dict[str, Any], payload: dict[str, Any]) -> None:
    opens = {
        datetime.fromtimestamp(int(row["start"]), UTC): float(row["open"])
        for row in payload["minute"]["bars"]
    }
    with Session(engine) as session:
        fills = session.execute(
            select(models.Execution, models.Order, models.OrderIntent, models.CanonicalSignal)
            .join(models.Order, models.Execution.order_id == models.Order.order_id)
            .join(models.OrderIntent, models.Order.intent_id == models.OrderIntent.intent_id)
            .join(
                models.CanonicalSignal,
                models.OrderIntent.canonical_signal_id == models.CanonicalSignal.signal_id,
            )
        ).all()
        assert len(fills) == 18
        assert len({execution.trade_id for execution, *_rest in fills}) == 18
        for execution, order, intent, signal in fills:
            assert signal.strategy_id != _NO_STOP
            route = routes[signal.strategy_id]
            assert order.account_id == intent.account_id == route.account_id
            assert intent.user_id == route.user_id
            assert execution.venue == "paper"
            assert execution.fee_ccy == "USD"
            fill_ts = signal.ts.replace(tzinfo=UTC) + timedelta(minutes=15)
            reference = opens[fill_ts]
            slippage = 1.001 if intent.side == "BUY" else 0.999
            assert float(execution.price) == pytest.approx(reference * slippage, abs=1e-7)
            assert float(execution.fee_amount) == pytest.approx(
                float(execution.qty) * float(execution.price) * 0.001, abs=1e-7
            )
        assert session.scalar(select(func.count()).select_from(models.Position)) == 0


def _feedback(engine: Any, feedback_url: str, payload: dict[str, Any], monkeypatch: Any) -> None:
    with monkeypatch.context() as env:
        env.setenv("DATABASE_URL", feedback_url)
        feedback, _review, _factory, feedback_engine = create_feedback_engine()
        try:
            first = feedback.run_evaluation_cycle(EvaluationHorizon.W1, limit=100)
            second = feedback.run_evaluation_cycle(EvaluationHorizon.W1, limit=100)
        finally:
            dispose_engine(feedback_engine)
    assert first["signals_evaluated"] == 10
    assert first["skipped_no_price"] == first["errors"] == 0
    assert second["signals_evaluated"] == second["errors"] == 0
    closes = {
        datetime.fromtimestamp(int(row["start"]), UTC) + timedelta(days=1): float(row["close"])
        for row in payload["daily"]["bars"]
    }
    with Session(engine) as session:
        rows = session.scalars(select(models.SignalPerformance)).all()
        assert len(rows) == 10
        for row in rows:
            assert row.evaluation_horizon == "1w"
            assert row.did_execute is (row.strategy_id != _NO_STOP)
            end = row.signal_ts.replace(tzinfo=UTC) + timedelta(days=7)
            assert float(row.exit_price) == pytest.approx(closes[end], abs=1e-7)
            actual = closes[end] / float(row.entry_price) - 1
            assert float(row.price_change_pct) == pytest.approx(actual, abs=0.0001)
            entry, exit_ = row.meta["entry_price_provenance"], row.meta["exit_price_provenance"]
            assert entry["source"] == exit_["source"] == _SOURCE
            assert entry["timeframe"] == exit_["timeframe"] == "1d"
            assert entry["origin"] == "canonical_signal"
            assert exit_["origin"] == "prices_table"
            assert exit_["price_id"] is not None


def test_native_pipeline_inputs_cover_real_default_decisions_and_execution_windows():
    payload = _inputs()
    configs, pairs = _cases(payload)
    assert len(configs) == len(pairs) == 10
    minutes = {int(row["start"]) for row in payload["minute"]["bars"]}
    daily = {int(row["start"]) + 86400 for row in payload["daily"]["bars"]}
    for strategy_id, signals in pairs.items():
        assert (signals[0].stop_loss is None) is (strategy_id == _NO_STOP)
        for signal in signals:
            assert int((signal.timestamp + timedelta(minutes=15)).timestamp()) in minutes
        assert int((signals[0].timestamp + timedelta(days=7)).timestamp()) in daily


def test_native_pipeline_refuses_existing_application_database():
    with pytest.raises(ValueError, match="isolated"):
        _require_isolated_urls(
            "postgresql://maintenance@localhost/vm_trading",
            "postgresql://vm_feedback_login@localhost/vm_trading",
        )


@pytest.mark.integration
def test_native_ports_complete_isolated_paper_pipeline(monkeypatch: pytest.MonkeyPatch):
    database_url = os.getenv("VM_PORTS_PIPELINE_DATABASE_URL")
    feedback_url = os.getenv("VM_PORTS_PIPELINE_FEEDBACK_DATABASE_URL")
    backend_url = os.getenv("VM_PORTS_PIPELINE_BACKEND_DATABASE_URL")
    if not database_url or not feedback_url or not backend_url:
        pytest.skip(
            "Explicit isolated native-port database, feedback and backend URLs are required"
        )
    _require_isolated_urls(database_url, feedback_url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setenv("EXECUTION_ENGINE_ALLOW_LIVE", "false")
    payload = _inputs()
    configs, pairs = _cases(payload)
    engine = create_engine_for_env(db_url=database_url)
    factory = get_session_factory(engine=engine)
    scoring = None
    try:
        with factory() as session:
            assert (
                session.scalar(text("SELECT current_database()")) == make_url(database_url).database
            )
            for model in (
                models.User,
                models.LinkedBrokerAccount,
                models.CanonicalSignal,
                models.Execution,
            ):
                assert session.scalar(select(func.count()).select_from(model)) == 0, (
                    "Test database must be fresh"
                )
        instr_id, routes = _provision_catalogue(
            factory, strategy_configs=configs, symbol=_SYMBOL, owner_id=_OWNER
        )
        _ingest(factory, instr_id, payload)
        scoring, app = _build_scoring_surface(
            database_url, factory, source=_SOURCE, timeframe="1d", max_age_seconds=86400
        )
        _post_signal_pairs(app, pairs)
        _post_signal_pairs(app, pairs)  # Redelivery must not create duplicate handoffs.
        _assert_scoring_handoff(engine, routes)
        assert (
            asyncio.run(
                _relay_historical_commands(
                    scoring.store, _normal_execution_engine(factory), limit=100
                )
            )
            == 20
        )
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(models.Execution)) == 0
            statuses = session.scalars(
                select(models.OutboxEvent.status).where(
                    models.OutboxEvent.topic == "execution.commands"
                )
            ).all()
            assert statuses == ["published"] * 20
            stale = session.scalars(
                select(models.ExecutionLog)
                .join(
                    models.CanonicalSignal,
                    models.ExecutionLog.canonical_signal_id == models.CanonicalSignal.signal_id,
                )
                .where(models.CanonicalSignal.action == "long")
            ).all()
            assert len(stale) == 10
            for log in stale:
                assert log.status == log.execution_mode == "blocked"
                assert log.error_message.startswith("Paper mode blocked: signal age ")
                assert " exceeds " in log.error_message
                assert log.execution_details["orders_submitted"] == 0
                assert log.execution_details["orders_filled"] == 0
        for strategy_id, route in routes.items():
            result = asyncio.run(
                replay_canonical_signals(_replay_args(route, strategy_id, pairs[strategy_id]))
            )
            assert result["signals_processed"] == 2
            assert result["signals_skipped_missing_price"] == result["failed_results"] == 0
            if strategy_id == _NO_STOP:
                assert result["orders_filled"] == 0
                assert result["blocked_results"] == 2
                _assert_missing_stop(engine, route)
            else:
                assert result["orders_filled"] == result["executed_results"] == 2, (
                    strategy_id,
                    result,
                )
                assert result["aggregate_pnl"]["complete"] is True
                assert result["aggregate_pnl"]["position_count"] == 0
            _assert_retry(engine, _replay_args(route, strategy_id, pairs[strategy_id]), monkeypatch)
        _assert_fills(engine, routes, payload)
        _feedback(engine, feedback_url, payload, monkeypatch)
        _assert_owner_ui(database_url, backend_url, set(configs))
    finally:
        if scoring is not None:
            dispose_engine(scoring.store._engine)
        dispose_engine(engine)
