import asyncio

from lib_strategy.signals.signal import Signal, SignalAction
from scoring_engine.dispatcher import ExecutionDispatcher
from scoring_engine.models import TriggerDecision


class StubStore:
    def __init__(self, resolved_id: int | None) -> None:
        self._resolved_id = resolved_id
        self.calls = 0
        self.event_calls = 0

    def resolve_instrument_id(self, symbol: str) -> int | None:
        self.calls += 1
        return self._resolved_id

    def enqueue_event(self, **_: object) -> str:
        self.event_calls += 1
        return f"evt-{self.event_calls}"


async def _dispatch_with_store(
    signal: Signal,
    store: StubStore,
    *,
    broker_account_id: int | None = 17,
    configured_account_id: int | None = None,
) -> dict:
    dispatcher = ExecutionDispatcher(
        "http://example.test",
        profile_provider=None,
        strategy_config_provider=None,
        store=store,
    )
    decision = TriggerDecision(
        user_id="123",
        should_execute=True,
        reason="ok",
        binding_id=7,
        broker_account_id=broker_account_id,
    )
    account_profile = {
        "account_id": 17,
        "broker": "paper",
        "environment": "paper",
        "status": "connected",
    }
    strategy_config = (
        {"broker_account_id": configured_account_id} if configured_account_id is not None else {}
    )
    try:
        results = await dispatcher.dispatch(
            signal=signal,
            decisions=[decision],
            score_context={"asset_score": 0.5},
            user_profiles={
                "123": {
                    "accounts": {"17": account_profile},
                    "brokers": {"paper": account_profile},
                }
            },
            user_strategy_configs={7: strategy_config},
            original_signal=signal,
        )
    finally:
        await dispatcher.aclose()

    return results[0]


def test_dispatcher_instrument_id_resolution_order() -> None:
    # 1) signal.instrument_id takes precedence (store not called).
    store = StubStore(resolved_id=999)
    signal = Signal(
        strategy_id="s1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.9,
        instrument_id="101",
        external_signal_id="ext-instrument-explicit",
    )
    result = asyncio.run(_dispatch_with_store(signal, store))
    assert result["execution_decision"]["instrument_id"] == 101
    assert result["execution_command"]["broker_route"]["broker_account_id"] == 17
    assert result["execution_command"]["user_strategy_config"]["broker_account_id"] == 17
    assert result["execution_command"]["execution_policy"]["config"]["broker_account_id"] == 17
    assert store.calls == 0

    # 2) store lookup used when signal.instrument_id missing.
    store = StubStore(resolved_id=555)
    signal = Signal(
        strategy_id="s1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.9,
        instrument_id=None,
        external_signal_id="ext-instrument-store",
    )
    result = asyncio.run(_dispatch_with_store(signal, store))
    assert result["execution_decision"]["instrument_id"] == 555
    assert store.calls == 1

    # 3) unresolvable instrument → loud skip (no fabricated hashed id that could
    #    collide with real instrument_ids and corrupt downstream bookkeeping).
    store = StubStore(resolved_id=None)
    signal = Signal(
        strategy_id="s1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.9,
        instrument_id=None,
        external_signal_id="ext-instrument-unresolved",
    )
    result = asyncio.run(_dispatch_with_store(signal, store))
    assert result["status"] == "error"
    assert "Unresolvable instrument_id" in result["reason"]
    assert "execution_decision" not in result
    assert store.calls == 1


def test_dispatcher_rejects_config_only_or_conflicting_account_routing() -> None:
    signal = Signal(
        strategy_id="s1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.9,
        instrument_id="101",
        external_signal_id="ext-account-routing",
    )

    config_only = asyncio.run(
        _dispatch_with_store(
            signal,
            StubStore(resolved_id=999),
            broker_account_id=None,
            configured_account_id=17,
        )
    )
    assert config_only["status"] == "error"
    assert config_only["reason"] == "Active execution decision has no linked broker account"

    conflict = asyncio.run(
        _dispatch_with_store(
            signal,
            StubStore(resolved_id=999),
            broker_account_id=17,
            configured_account_id=18,
        )
    )
    assert conflict["status"] == "error"
    assert (
        conflict["reason"]
        == "Execution decision broker account 17 conflicts with configured account 18"
    )
