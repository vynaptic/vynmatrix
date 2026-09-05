from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from lib_strategy.signals.signal import Signal, SignalAction
from scoring_engine.dispatcher import (
    _EXECUTION_COMMAND_MAX_ATTEMPTS,
    ExecutionDispatcher,
)
from scoring_engine.models import TriggerDecision
from scoring_engine.outbox_relay import OutboxRelayWorker
from scoring_engine.storage import InMemoryScoreStore


def _account_profile(
    *,
    account_id: int,
    broker: str = "paper",
    environment: str = "paper",
) -> dict[str, Any]:
    account = {
        "account_id": account_id,
        "broker": broker,
        "environment": environment,
        "status": "connected",
        "credential_ref": None,
    }
    return {
        "broker": broker,
        "broker_account_id": account_id,
        "broker_environment": environment,
        "sandbox": environment != "live",
        "live_enabled": environment == "live",
        "linked_brokers": [broker],
        "accounts": {str(account_id): account},
        "brokers": {broker: account},
    }


def test_dispatcher_queues_execution_command_event() -> None:
    store = InMemoryScoreStore()
    dispatcher = ExecutionDispatcher("http://execution.test", store=store)
    signal = Signal(
        signal_id="sig-queue-1",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.9,
        timestamp=datetime(2026, 3, 7, tzinfo=UTC),
        asset_class="crypto",
        run_id="run-queue-1",
        external_signal_id="ext-queue-1",
    )
    decision = TriggerDecision(
        user_id="user-1",
        should_execute=True,
        reason="threshold_passed",
        binding_id=11,
        broker_account_id=101,
        execution_mode="spot",
    )

    result = asyncio.run(
        dispatcher.dispatch(
            signal=signal,
            decisions=[decision],
            score_context={"asset_score": 0.75, "recommended_mode": "spot"},
            user_profiles={"user-1": _account_profile(account_id=101)},
        )
    )[0]

    assert result["status"] == "queued"
    assert result["execution_decision"]["instrument_id"] > 0
    assert result["execution_command"]["topic"] == "execution.commands"
    assert result["execution_command"]["signal"]["signal_id"] == "sig-queue-1"
    assert result["execution_command"]["signal"]["external_signal_id"] == "ext-queue-1"
    assert result["execution_command"]["signal"]["instrument_id"] == str(
        result["execution_decision"]["instrument_id"]
    )
    # M-12: causation_id is populated (the canonical signal causes the command).
    assert result["execution_command"]["causation_id"] == "sig-queue-1"
    assert result["execution_command"]["execution_policy"]["recommended_mode"] == "spot"
    assert "recommended_mode" not in result["execution_command"]["execution_policy"]["risk_caps"]
    assert result["execution_command"]["broker_route"]["broker_account_id"] == 101
    assert result["execution_command"]["user_strategy_config"]["broker_account_id"] == 101


def test_outbox_relay_posts_execution_command() -> None:
    store = InMemoryScoreStore()
    dispatcher = ExecutionDispatcher("http://execution.test", store=store)
    signal = Signal(
        signal_id="sig-relay-1",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="ETHUSD",
        action=SignalAction.SHORT,
        confidence=0.88,
        timestamp=datetime(2026, 3, 7, tzinfo=UTC),
        asset_class="crypto",
        instrument_id="42",
        run_id="run-relay-1",
        external_signal_id="ext-relay-1",
    )
    decision = TriggerDecision(
        user_id="user-2",
        should_execute=True,
        reason="threshold_passed",
        binding_id=12,
        broker_account_id=102,
        execution_mode="spot",
    )
    asyncio.run(
        dispatcher.dispatch(
            signal=signal,
            decisions=[decision],
            score_context={"asset_score": 0.81, "recommended_mode": "spot"},
            user_profiles={"user-2": _account_profile(account_id=102)},
        )
    )

    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "url": str(request.url),
                "run_id": request.headers.get("x-run-id"),
                "api_key": request.headers.get("x-api-key"),
                "payload": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(
            200,
            json={"status": "ok"},
            request=request,
        )

    async def _drain() -> int:
        worker = OutboxRelayWorker(
            store=store,
            exec_engine_url="http://execution.test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
            api_key="execution-secret",
        )
        try:
            return await worker.drain_once(topics=["execution.commands"])
        finally:
            await worker.aclose()

    processed = asyncio.run(_drain())

    assert processed == 1
    assert captured[0]["url"].endswith("/execute-command")
    assert captured[0]["run_id"] == "run-relay-1"
    assert captured[0]["api_key"] == "execution-secret"
    assert captured[0]["payload"]["event_type"] == "ExecutionCommand"
    assert captured[0]["payload"]["signal"]["instrument_id"] == "42"


def test_in_memory_outbox_claim_accepts_single_pass_topic_iterables() -> None:
    store = InMemoryScoreStore()
    first_id = store.enqueue_event(
        topic="execution.commands",
        event_type="ExecutionCommand",
        payload={"signal_id": "sig-iter-1"},
        event_key="exec:sig-iter-1",
    )
    second_id = store.enqueue_event(
        topic="execution.commands",
        event_type="ExecutionCommand",
        payload={"signal_id": "sig-iter-2"},
        event_key="exec:sig-iter-2",
    )

    claimed = store.claim_outbox_batch(
        topics=(topic for topic in ["execution.commands"]),
        consumer="relay",
        limit=10,
    )

    assert [record.event_id for record in claimed] == [first_id, second_id]


def test_dispatcher_forces_paper_route_snapshot_when_runtime_mode_is_paper(
    monkeypatch,
) -> None:
    store = InMemoryScoreStore()
    dispatcher = ExecutionDispatcher(
        "http://execution.test",
        store=store,
        runtime_mode="paper",
    )
    # A process-environment mutation after construction cannot alter the
    # immutable route snapshot used for this dispatcher instance.
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("RUN_MODE", "live")
    signal = Signal(
        signal_id="sig-paper-route-1",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="SOLUSD",
        action=SignalAction.LONG,
        confidence=0.82,
        timestamp=datetime(2026, 3, 7, tzinfo=UTC),
        asset_class="crypto",
        run_id="run-paper-route-1",
        external_signal_id="ext-paper-route-1",
    )
    decision = TriggerDecision(
        user_id="user-1",
        should_execute=True,
        reason="threshold_passed",
        binding_id=13,
        broker_account_id=103,
        execution_mode="spot",
    )

    result = asyncio.run(
        dispatcher.dispatch(
            signal=signal,
            decisions=[decision],
            score_context={"asset_score": 0.83, "recommended_mode": "spot"},
            user_profiles={"user-1": _account_profile(account_id=103, broker="delta")},
            user_strategy_configs={13: {"execution_mode": "spot"}},
        )
    )[0]

    route = result["execution_command"]["broker_route"]

    assert route["broker"] == "delta"
    assert route["broker_environment"] == "paper"
    assert route["sandbox"] is True
    assert route["live_enabled"] is False


def test_single_broker_steer_pins_route_snapshot_broker() -> None:
    # D3: the config 'broker' key (emitted by DBStrategyConfigProvider for a
    # single-entry allowed_brokers) wins over the profile default broker in the
    # route snapshot, so a 'paper'-pinned binding actually paper-trades instead
    # of resolving coinbase and being vetoed at the execution allow-list gate.
    store = InMemoryScoreStore()
    dispatcher = ExecutionDispatcher("http://execution.test", store=store)
    signal = Signal(
        signal_id="sig-steer-1",
        strategy_id="test_strategy_alpha_v1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.9,
        timestamp=datetime(2026, 3, 7, tzinfo=UTC),
        asset_class="crypto",
        run_id="run-steer-1",
        external_signal_id="ext-steer-1",
    )
    decision = TriggerDecision(
        user_id="user-1",
        should_execute=True,
        reason="threshold_passed",
        binding_id=21,
        broker_account_id=104,
        execution_mode="spot",
        allowed_brokers=["paper"],
    )

    result = asyncio.run(
        dispatcher.dispatch(
            signal=signal,
            decisions=[decision],
            score_context={"asset_score": 0.8, "recommended_mode": "spot"},
            user_profiles={"user-1": _account_profile(account_id=104, broker="paper")},
            user_strategy_configs={
                21: {"execution_mode": "spot", "broker": "paper"},
            },
        )
    )[0]

    route = result["execution_command"]["broker_route"]
    assert route["broker"] == "paper"
    assert route["allowed_brokers"] == ["paper"]


def test_same_user_signal_dispatches_independently_to_two_broker_accounts() -> None:
    store = InMemoryScoreStore()
    dispatcher = ExecutionDispatcher("http://execution.test", store=store)
    signal = Signal(
        signal_id="sig-multi-account-1",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.9,
        timestamp=datetime(2026, 3, 7, tzinfo=UTC),
        asset_class="crypto",
        run_id="run-multi-account-1",
        external_signal_id="ext-multi-account-1",
    )
    decisions = [
        TriggerDecision(
            user_id="user-1",
            should_execute=True,
            reason="threshold_passed",
            binding_id=31,
            broker_account_id=201,
            execution_mode="spot",
            allowed_brokers=["coinbase"],
        ),
        TriggerDecision(
            user_id="user-1",
            should_execute=True,
            reason="threshold_passed",
            binding_id=32,
            broker_account_id=202,
            execution_mode="spot",
            allowed_brokers=["ibkr"],
        ),
    ]
    account_201 = {
        "account_id": 201,
        "broker": "coinbase",
        "environment": "paper",
        "status": "connected",
        "credential_ref": None,
    }
    account_202 = {
        "account_id": 202,
        "broker": "ibkr",
        "environment": "paper",
        "status": "connected",
        "credential_ref": None,
    }
    profile = {
        "broker": "coinbase",
        "broker_account_id": 201,
        "broker_environment": "paper",
        "sandbox": True,
        "live_enabled": False,
        "linked_brokers": ["coinbase", "ibkr"],
        "accounts": {"201": account_201, "202": account_202},
        "brokers": {"coinbase": account_201, "ibkr": account_202},
    }

    results = asyncio.run(
        dispatcher.dispatch(
            signal=signal,
            decisions=decisions,
            score_context={"asset_score": 0.8, "recommended_mode": "spot"},
            user_profiles={"user-1": profile},
            user_strategy_configs={
                31: {
                    "binding_id": 31,
                    "broker_account_id": 201,
                    "execution_mode": "spot",
                    "broker": "coinbase",
                },
                32: {
                    "binding_id": 32,
                    "broker_account_id": 202,
                    "execution_mode": "spot",
                    "broker": "ibkr",
                },
            },
        )
    )

    assert [result["status"] for result in results] == ["queued", "queued"]
    assert [
        result["execution_command"]["broker_route"]["broker_account_id"] for result in results
    ] == [201, 202]
    assert [result["execution_command"]["broker_route"]["broker"] for result in results] == [
        "coinbase",
        "ibkr",
    ]


def test_execution_command_enqueued_with_extended_max_attempts() -> None:
    # D5: execution.commands must survive a multi-hour execution-engine outage
    # instead of dead-lettering on the outbox default of 10 attempts (~23 min) —
    # a dead-lettered CLOSE strands a live position and there is no requeue tool.
    store = InMemoryScoreStore()
    dispatcher = ExecutionDispatcher("http://execution.test", store=store)
    signal = Signal(
        signal_id="sig-attempts-1",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.9,
        timestamp=datetime(2026, 3, 7, tzinfo=UTC),
        asset_class="crypto",
        run_id="run-attempts-1",
        external_signal_id="ext-attempts-1",
    )
    decision = TriggerDecision(
        user_id="user-1",
        should_execute=True,
        reason="threshold_passed",
        binding_id=22,
        broker_account_id=105,
        execution_mode="spot",
    )

    result = asyncio.run(
        dispatcher.dispatch(
            signal=signal,
            decisions=[decision],
            score_context={"asset_score": 0.8, "recommended_mode": "spot"},
            user_profiles={"user-1": _account_profile(account_id=105)},
        )
    )[0]
    assert result["status"] == "queued"

    claimed = store.claim_outbox_batch(topics=["execution.commands"], consumer="test", limit=10)
    assert claimed[0].max_attempts == _EXECUTION_COMMAND_MAX_ATTEMPTS
    assert claimed[0].max_attempts == 60


def test_dispatcher_refuses_signal_without_external_identity() -> None:
    store = InMemoryScoreStore()
    dispatcher = ExecutionDispatcher("http://execution.test", store=store)
    signal = Signal(
        signal_id="sig-no-identity",
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.9,
        timestamp=datetime(2026, 3, 7, tzinfo=UTC),
        asset_class="crypto",
        run_id="run-no-identity",
    )
    decision = TriggerDecision(
        user_id="user-1",
        should_execute=True,
        reason="threshold_passed",
        binding_id=23,
        broker_account_id=106,
        execution_mode="spot",
    )

    with pytest.raises(ValueError, match="refusing execution dispatch"):
        asyncio.run(
            dispatcher.dispatch(
                signal=signal,
                decisions=[decision],
                score_context={"asset_score": 0.8, "recommended_mode": "spot"},
                user_profiles={"user-1": _account_profile(account_id=106)},
            )
        )

    assert (
        store.claim_outbox_batch(
            topics=["execution.commands"],
            consumer="test",
            limit=10,
        )
        == []
    )
