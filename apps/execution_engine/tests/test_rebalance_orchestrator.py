from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from execution_engine.account_execution_serializer import (
    AccountExecutionBusyError,
    AccountExecutionSerializer,
)
from execution_engine.api import create_app
from execution_engine.config import AccountState, BrokerType
from execution_engine.execution_result import ExecutionResult
from execution_engine.models import (
    CloseQuantityOverride,
    ExecutionRequest,
    TargetPositionQuantityOverride,
)
from execution_engine.order_builder import OrderBuilder
from execution_engine.rebalance_execution_adapter import (
    ExecutionEngineRebalanceAdapter,
    RebalanceExecutionError,
    _floor_to_lot_size,
    execution_error,
)
from execution_engine.rebalance_orchestrator import PortfolioRebalanceOrchestrator
from execution_engine.rebalance_store import (
    RebalanceLeaseLostError,
    RebalancePlanContractError,
    RebalancePlanLease,
    RebalancePlanLegSnapshot,
    RebalancePlanProgressSnapshot,
    RebalancePlanStore,
    RebalanceReadinessSnapshot,
)
from lib_application.db.models import (
    AccountExecutionGeneration,
    AccountRebalancePlan,
    AccountRebalancePlanLeg,
    AccountRebalancePlanResolution,
    ApiAuditLog,
    Base,
    ExecutionDecisionLog,
)
from lib_common.hashing import canonical_json_hash
from lib_common.internal_events import (
    BrokerRouteSnapshot,
    CanonicalSignalSnapshot,
    ExecutionPolicySnapshot,
    RebalanceExecutionCommandEvent,
    RebalanceExecutionLegSnapshot,
    compute_account_plan_id,
    compute_rebalance_execution_content_sha256,
)
from lib_strategy.signals.signal import Signal, SignalAction

_NOW = datetime.now(tz=UTC).replace(microsecond=0)


def test_ibkr_target_flooring_never_exceeds_requested_quantity() -> None:
    assert _floor_to_lot_size(Decimal("12.99"), Decimal("1")) == Decimal("12")
    assert _floor_to_lot_size(Decimal("0.99"), Decimal("1")) == Decimal("0")


_STRATEGY = "us_quality_compounder_v1"
_EXIT_ID = "b" * 64
_ENTRY_ID = "c" * 64


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _command(
    *,
    autopilot: bool = True,
    entries_enabled: bool = True,
    exits_enabled: bool = True,
    data_use_scope: str = "paper_forward",
    broker_environment: str = "paper",
    legs: list[RebalanceExecutionLegSnapshot] | None = None,
    strategy_id: str = _STRATEGY,
    model_rebalance_id: str = "a" * 64,
    execution_offset: timedelta = timedelta(0),
    broker_account_id: int = 41,
    broker: str | None = None,
    entry_cash_buffer_bps: float | None = None,
) -> RebalanceExecutionCommandEvent:
    selected = (
        legs
        if legs is not None
        else [
            RebalanceExecutionLegSnapshot(
                plan_leg_id=_EXIT_ID,
                sequence=0,
                phase="exit",
                model_leg_id="model-exit",
                model_signal_snapshot_sha256="f" * 64,
                external_signal_id="signal-aapl",
                symbol="AAPL",
                allocation_hint=0,
            ),
            RebalanceExecutionLegSnapshot(
                plan_leg_id=_ENTRY_ID,
                sequence=1,
                phase="entry",
                model_leg_id="model-entry",
                model_signal_snapshot_sha256="9" * 64,
                external_signal_id="signal-msft",
                symbol="MSFT",
                rank=1,
                allocation_hint=0.05,
                depends_on_sequences=[0],
            ),
        ]
    )
    route_broker = broker or ("paper" if broker_environment == "paper" else "ibkr")
    risk_caps: dict[str, float] = {"max_position_pct": 0.08}
    if entry_cash_buffer_bps is not None:
        risk_caps["entry_cash_buffer_bps"] = entry_cash_buffer_bps
    policy = ExecutionPolicySnapshot(
        user_id="user-1",
        strategy_id=strategy_id,
        binding_id=17,
        autopilot=autopilot,
        entries_enabled=entries_enabled,
        exits_enabled=exits_enabled,
        execution_mode="spot",
        execution_modes_allowed=["spot"],
        allowed_brokers=[route_broker],
        sizing={"max_position_pct": 0.08},
        risk_caps=risk_caps,
    )
    route = BrokerRouteSnapshot(
        broker=route_broker,
        broker_account_id=broker_account_id,
        broker_environment=broker_environment,  # type: ignore[arg-type]
        allowed_brokers=[route_broker],
        live_enabled=broker_environment == "live",
        sandbox=broker_environment == "paper",
        asset_class="equity",
        execution_mode="spot",
    )
    decision_cutoff = _NOW - timedelta(hours=2)
    execute_not_before = _NOW - timedelta(hours=1) + execution_offset
    expires_at = _NOW + timedelta(days=1)
    provider_digest = "d" * 64
    session_digest = "e" * 64
    plan_id = compute_account_plan_id(
        model_rebalance_id=model_rebalance_id,
        user_id="user-1",
        binding_id=17,
        broker_account_id=broker_account_id,
        data_use_scope=data_use_scope,  # type: ignore[arg-type]
        provider_authority_sha256=provider_digest,
        execute_not_before=execute_not_before,
        execution_session_sha256=session_digest,
    )
    content = compute_rebalance_execution_content_sha256(
        account_plan_id=plan_id,
        model_rebalance_id=model_rebalance_id,
        user_id="user-1",
        strategy_id=strategy_id,
        binding_id=17,
        broker_account_id=broker_account_id,
        data_use_scope=data_use_scope,  # type: ignore[arg-type]
        provider_authority_sha256=provider_digest,
        decision_cutoff=decision_cutoff,
        execute_not_before=execute_not_before,
        execution_session_sha256=session_digest,
        execution_policy=policy,
        broker_route=route,
        expires_at=expires_at,
        intentional_cash_slots=1 if not selected else 0,
        legs=selected,
    )
    return RebalanceExecutionCommandEvent(
        account_plan_id=plan_id,
        model_rebalance_id=model_rebalance_id,
        user_id="user-1",
        strategy_id=strategy_id,
        binding_id=17,
        data_use_scope=data_use_scope,  # type: ignore[arg-type]
        provider_authority_sha256=provider_digest,
        decision_cutoff=decision_cutoff,
        execute_not_before=execute_not_before,
        execution_session_sha256=session_digest,
        execution_policy=policy,
        broker_route=route,
        expires_at=expires_at,
        content_sha256=content,
        expected_leg_count=len(selected),
        intentional_cash_slots=1 if not selected else 0,
        legs=selected,
    )


def _signal(symbol: str, action: SignalAction, instrument_id: int) -> Signal:
    return Signal(
        signal_id=f"signal-{symbol.lower()}",
        strategy_id=_STRATEGY,
        strategy_type="indicator",
        symbol=symbol,
        action=action,
        confidence=0.8,
        timestamp=_NOW - timedelta(hours=2),
        entry_price=100,
        asset_class="equity",
        instrument_id=instrument_id,
        strategy_version="1.0.0",
        external_signal_id=f"signal-{symbol.lower()}",
        metadata={"target_weight_drift_fraction": "0.01"},
    )


def _legs() -> tuple[RebalancePlanLegSnapshot, ...]:
    return (
        RebalancePlanLegSnapshot(
            plan_leg_id=_EXIT_ID,
            model_signal_snapshot_sha256="f" * 64,
            model_sequence=0,
            command_sequence=0,
            phase="exit",
            action="exit",
            disposition="selected",
            status="pending",
            instrument_id=1,
            allocation_hint=Decimal("0"),
            required=True,
            depends_on_sequences=(),
            reason_code="rotation_exit",
            canonical_signal_id=101,
            signal=_signal("AAPL", SignalAction.CLOSE, 1),
        ),
        RebalancePlanLegSnapshot(
            plan_leg_id=_ENTRY_ID,
            model_signal_snapshot_sha256="9" * 64,
            model_sequence=1,
            command_sequence=1,
            phase="entry",
            action="entry",
            disposition="selected",
            status="pending",
            instrument_id=2,
            allocation_hint=Decimal("0.05"),
            required=True,
            depends_on_sequences=(0,),
            reason_code="ranked_entry",
            canonical_signal_id=102,
            signal=_signal("MSFT", SignalAction.HOLD, 2),
        ),
    )


def _rejected_position_leg(
    *,
    signal_action: SignalAction,
    reason_code: str,
) -> RebalancePlanLegSnapshot:
    return RebalancePlanLegSnapshot(
        plan_leg_id="8" * 64,
        model_signal_snapshot_sha256="7" * 64,
        model_sequence=0,
        command_sequence=None,
        phase="entry",
        action="reject",
        disposition="rejected",
        status="pending",
        instrument_id=2,
        allocation_hint=Decimal("0.05"),
        required=False,
        depends_on_sequences=(),
        reason_code=reason_code,
        canonical_signal_id=102,
        signal=_signal("MSFT", signal_action, 2),
    )


class _Store:
    def __init__(self, legs: tuple[RebalancePlanLegSnapshot, ...] = _legs()) -> None:
        self.legs = {leg.plan_leg_id: leg for leg in legs}
        self.durable = {leg.plan_leg_id: "absent" for leg in legs}
        self.phase = "exit"
        self.status = "pending"
        self.error_code: str | None = None
        self.error_detail: str | None = None
        self.completion_account_generation: int | None = None
        self.terminal_targets_sha256: str | None = None

    def claim(self, command: Any, *, owner: str, now: datetime) -> RebalancePlanLease:
        del owner, now
        return RebalancePlanLease(command.account_plan_id, "owner", 1, True)

    def load_legs(self, command: Any) -> tuple[RebalancePlanLegSnapshot, ...]:
        del command
        return tuple(sorted(self.legs.values(), key=lambda leg: leg.model_sequence))

    def persisted_execution_state(self, leg: RebalancePlanLegSnapshot, **_: Any) -> str:
        return self.durable[leg.plan_leg_id]

    def mark_leg(
        self,
        lease: Any,
        *,
        plan_leg_id: str,
        status: str,
        now: datetime,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        del lease, now
        self.legs[plan_leg_id] = replace(self.legs[plan_leg_id], status=status)
        self.error_code = error_code
        self.error_detail = error_detail

    def transition(
        self,
        lease: Any,
        *,
        phase: str,
        status: str,
        now: datetime,
        error_code: str | None = None,
        error_detail: str | None = None,
        release_lease: bool = False,
        completion_account_generation: int | None = None,
        terminal_targets_sha256: str | None = None,
    ) -> None:
        del lease, now, release_lease
        self.phase = phase
        self.status = status
        self.error_code = error_code
        self.error_detail = error_detail
        self.completion_account_generation = completion_account_generation
        self.terminal_targets_sha256 = terminal_targets_sha256

    def freeze_leg_target(
        self,
        lease: Any,
        *,
        plan_leg_id: str,
        target_quantity: Decimal,
        account_generation: int,
        now: datetime,
    ) -> Decimal:
        del lease, now
        leg = self.legs[plan_leg_id]
        if leg.frozen_target_quantity is not None:
            assert leg.frozen_target_quantity == target_quantity
            return leg.frozen_target_quantity
        self.legs[plan_leg_id] = replace(
            leg,
            frozen_target_quantity=target_quantity,
            target_account_generation=account_generation,
            target_frozen_at=_NOW,
        )
        return target_quantity

    def progress(self, account_plan_id: str) -> RebalancePlanProgressSnapshot:
        terminal = sum(
            leg.status in {"confirmed", "failed", "rejected", "skipped"}
            for leg in self.legs.values()
        )
        return RebalancePlanProgressSnapshot(
            account_plan_id=account_plan_id,
            model_rebalance_id="a" * 64,
            user_id="user-1",
            broker_account_id=41,
            strategy_id=_STRATEGY,
            phase=self.phase,
            status=self.status,
            completed_legs=terminal,
            pending_legs=len(self.legs) - terminal,
            last_error_code=self.error_code,
            last_error_detail=self.error_detail,
            last_progress_at=_NOW,
            progress_deadline_at=_NOW + timedelta(hours=1),
            expires_at=_NOW + timedelta(days=1),
        )

    def readiness(self, *, now: datetime) -> RebalanceReadinessSnapshot:
        del now
        return RebalanceReadinessSnapshot(True, 0, 0, 0, ())

    def audit(self, account_plan_id: str) -> dict[str, object]:
        return self.progress(account_plan_id).to_dict()


class _Adapter:
    def __init__(
        self,
        store: _Store,
        *,
        partial_exit: bool = False,
        target_error_code: str | None = None,
    ) -> None:
        self.store = store
        self.partial_exit = partial_exit
        self.target_error_code = target_error_code
        self.fifo: dict[str, Decimal] = {"AAPL": Decimal("4")}
        self.broker: dict[str, Decimal] = {"AAPL": Decimal("10")}
        self.calls: list[tuple[str, str, Decimal]] = []
        self.target_requests: list[tuple[str, Decimal]] = []

    async def authoritative_account_state(self, command: Any, signal: Signal) -> AccountState:
        del command, signal
        return AccountState(
            broker=BrokerType.PAPER,
            equity=10_000,
            available_cash=8_000,
            positions=[
                {"symbol": symbol, "qty": float(quantity), "side": "long"}
                for symbol, quantity in self.broker.items()
                if quantity > 0
            ],
            fetched_at=_NOW,
        )

    async def strategy_positions(self, command: Any) -> dict[str, Decimal]:
        del command
        return {symbol: quantity for symbol, quantity in self.fifo.items() if quantity != 0}

    def recoverable_account_orders(self, command: Any) -> dict[str, dict[str, object]]:
        del command
        return {}

    async def build_target_override(
        self,
        command: Any,
        signal: Signal,
        *,
        plan_leg_id: str,
        target_allocation: Decimal,
        account: AccountState,
        strategy_quantity: Decimal,
        broker_quantity: Decimal,
    ) -> TargetPositionQuantityOverride:
        del account
        self.target_requests.append((signal.symbol, target_allocation))
        if signal.symbol == "AAPL" and self.target_error_code is not None:
            raise execution_error(
                self.target_error_code,
                "Pinned execution-session quote is unavailable for the full exit",
                retryable=True,
            )
        target = Decimal("5") if signal.symbol == "MSFT" else Decimal("0")
        return TargetPositionQuantityOverride(
            account_plan_id=command.account_plan_id,
            plan_leg_id=plan_leg_id,
            symbol=signal.symbol,
            target_allocation=target_allocation,
            target_quantity=target,
            strategy_quantity=strategy_quantity,
            broker_quantity=broker_quantity,
            target_weight_drift_fraction=Decimal("0.01"),
            broker_observed_at=_NOW,
            reference_price=Decimal("100"),
            quote_observed_at=_NOW,
        )

    async def execute(
        self,
        command: Any,
        signal: Signal,
        *,
        target_allocation: Decimal | None,
        close_quantity_override: CloseQuantityOverride | None,
        target_position_override: TargetPositionQuantityOverride | None,
    ) -> ExecutionResult:
        del command, target_allocation
        override = close_quantity_override or target_position_override
        assert override is not None
        quantity = (
            close_quantity_override.quantity
            if close_quantity_override is not None
            else abs(target_position_override.delta_quantity)  # type: ignore[union-attr]
        )
        self.calls.append((signal.action.value, signal.symbol, quantity))
        leg_id = override.plan_leg_id
        if signal.symbol == "AAPL" and signal.action is SignalAction.CLOSE and self.partial_exit:
            self.store.durable[leg_id] = "partial"
            return ExecutionResult(
                success=True,
                signal_id=signal.signal_id,
                symbol=signal.symbol,
                execution_mode="spot",
                broker="paper",
                orders_submitted=1,
                orders_filled=0,
                total_quantity=float(quantity / 2),
                average_price=100,
                total_commission=0,
                order_results=[{"status": "partially_filled"}],
            )
        self.store.durable[leg_id] = "confirmed"
        if close_quantity_override is not None:
            self.fifo[signal.symbol] = Decimal("0")
            self.broker[signal.symbol] -= quantity
        else:
            assert target_position_override is not None
            self.fifo[signal.symbol] = target_position_override.target_quantity
            self.broker[signal.symbol] = (
                self.broker.get(
                    signal.symbol,
                    Decimal("0"),
                )
                + target_position_override.delta_quantity
            )
        return ExecutionResult(
            success=True,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            execution_mode="spot",
            broker="paper",
            orders_submitted=1,
            orders_filled=1,
            total_quantity=float(quantity),
            average_price=100,
            total_commission=0,
            order_results=[{"status": "filled"}],
        )


class _ConcurrentOutsidePositionAdapter(_Adapter):
    """Expose an out-of-plan FIFO position only after entry postcondition validation."""

    def __init__(self, store: _Store) -> None:
        super().__init__(store)
        self.post_entry_fifo_reads = 0

    async def strategy_positions(self, command: Any) -> dict[str, Decimal]:
        positions = await super().strategy_positions(command)
        if any(symbol == "MSFT" for _action, symbol, _quantity in self.calls):
            self.post_entry_fifo_reads += 1
            if self.post_entry_fifo_reads >= 2:
                positions["NVDA"] = Decimal("1")
        return positions


class _ConcurrentTargetDriftAdapter(_Adapter):
    """Change one modeled quantity only at the terminal all-symbol read."""

    def __init__(self, store: _Store) -> None:
        super().__init__(store)
        self.post_entry_fifo_reads = 0

    async def strategy_positions(self, command: Any) -> dict[str, Decimal]:
        positions = await super().strategy_positions(command)
        if any(symbol == "MSFT" for _action, symbol, _quantity in self.calls):
            self.post_entry_fifo_reads += 1
            if self.post_entry_fifo_reads >= 2:
                positions["MSFT"] = Decimal("4")
        return positions


class _PendingAccountOrderAdapter(_Adapter):
    def recoverable_account_orders(self, command: Any) -> dict[str, dict[str, object]]:
        del command
        return {"order-1": {"status": "submitted", "symbol": "MSFT"}}


class _RejectedIBKRFundingAdapter(_Adapter):
    async def authoritative_account_state(self, command: Any, signal: Signal) -> AccountState:
        del signal
        return AccountState(
            broker=BrokerType.IBKR,
            equity=10_000,
            available_cash=8_000,
            currency="EUR",
            positions=[
                {"symbol": symbol, "qty": float(quantity), "side": "long"}
                for symbol, quantity in self.broker.items()
                if quantity > 0
            ],
            balances=[],
            fetched_at=_NOW,
            source="broker",
            account_id=str(command.broker_route.broker_account_id),
        )

    def validate_entry_funding(
        self,
        command: Any,
        account: AccountState,
        overrides: tuple[TargetPositionQuantityOverride, ...],
    ) -> None:
        del command, account, overrides
        raise execution_error(
            "ibkr_usd_settled_cash_unavailable",
            "IBKR USD settled cash is unavailable",
            retryable=True,
        )


@pytest.mark.anyio
async def test_account_serializer_reenters_generation_and_rejects_local_contender() -> None:
    serializer = AccountExecutionSerializer(None)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _owner() -> tuple[int, int]:
        async with (
            serializer.hold(
                user_id="user-1",
                broker_account_id=41,
                writer_kind="rebalance",
                account_plan_id="a" * 64,
            ) as outer,
            serializer.hold(
                user_id="user-1",
                broker_account_id=41,
                writer_kind="ordinary",
            ) as nested,
        ):
            entered.set()
            await release.wait()
            return outer.generation, nested.generation

    owner = asyncio.create_task(_owner())
    await entered.wait()
    with pytest.raises(AccountExecutionBusyError):
        async with serializer.hold(
            user_id="user-1",
            broker_account_id=41,
            writer_kind="manual",
        ):
            pytest.fail("contending account writer must not acquire the generation")
    release.set()
    assert await owner == (1, 1)

    async with serializer.hold(
        user_id="user-1",
        broker_account_id=41,
        writer_kind="ordinary",
    ) as next_lease:
        assert next_lease.generation == 2


@pytest.mark.anyio
async def test_account_serializer_preserves_busy_error_when_pg_lock_is_not_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ScalarResult:
        @staticmethod
        def scalar_one() -> bool:
            return False

    class _Connection:
        def __init__(self) -> None:
            self.statements: list[str] = []
            self.closed = False

        def execution_options(self, **_: Any) -> _Connection:
            return self

        def execute(self, statement: Any, _parameters: Any) -> _ScalarResult:
            self.statements.append(str(statement))
            return _ScalarResult()

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    engine = SimpleNamespace(connect=lambda: connection)
    serializer = AccountExecutionSerializer(None)
    monkeypatch.setattr(serializer, "_postgres_engine", lambda: engine)

    with pytest.raises(AccountExecutionBusyError):
        async with serializer.hold(
            user_id="user-1",
            broker_account_id=41,
            writer_kind="ordinary",
        ):
            pytest.fail("a failed PostgreSQL advisory lock must not enter the writer context")

    assert connection.closed is True
    assert len(connection.statements) == 1
    assert "pg_try_advisory_lock" in connection.statements[0]
    assert all("pg_advisory_unlock" not in statement for statement in connection.statements)


@pytest.mark.anyio
async def test_exit_is_confirmed_before_dependent_entry_and_clamped_to_strategy_fifo() -> None:
    store = _Store()
    adapter = _Adapter(store)
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(), now=_NOW)

    assert result.progress.status == "completed"
    assert adapter.calls == [
        ("CLOSE", "AAPL", Decimal("4")),
        ("LONG", "MSFT", Decimal("5")),
    ]
    assert adapter.target_requests == [
        ("AAPL", Decimal("0")),
        ("MSFT", Decimal("0.05")),
        ("MSFT", Decimal("0.05")),
        ("MSFT", Decimal("0.05")),
    ]
    assert adapter.broker["AAPL"] == Decimal("6")
    assert store.completion_account_generation == 1
    assert store.terminal_targets_sha256 == canonical_json_hash(
        [
            {"symbol": "AAPL", "target_quantity": "0"},
            {"symbol": "MSFT", "target_quantity": "5"},
        ]
    )


@pytest.mark.anyio
async def test_final_fifo_fence_blocks_concurrent_position_outside_plan() -> None:
    store = _Store()
    adapter = _ConcurrentOutsidePositionAdapter(store)
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(), now=_NOW)

    assert result.retryable is False
    assert result.progress.phase == "failed"
    assert result.progress.status == "failed"
    assert result.progress.last_error_code == "strategy_position_outside_plan"
    assert adapter.post_entry_fifo_reads == 2


@pytest.mark.anyio
async def test_final_fifo_fence_blocks_quantity_drift_from_frozen_target() -> None:
    store = _Store()
    adapter = _ConcurrentTargetDriftAdapter(store)
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(), now=_NOW)

    assert result.retryable is True
    assert result.progress.status == "blocked"
    assert result.progress.last_error_code == "terminal_target_quantity_mismatch"
    assert store.legs[_EXIT_ID].frozen_target_quantity == Decimal("0")
    assert store.legs[_ENTRY_ID].frozen_target_quantity == Decimal("5")


@pytest.mark.anyio
async def test_final_fifo_fence_blocks_nonterminal_account_order() -> None:
    store = _Store()
    adapter = _PendingAccountOrderAdapter(store)
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(), now=_NOW)

    assert result.retryable is True
    assert result.progress.status == "blocked"
    assert result.progress.last_error_code == "account_orders_nonterminal"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error_code",
    [
        "rebalance_market_data_missing",
        "rebalance_market_data_before_session",
        "rebalance_market_data_stale",
    ],
)
async def test_full_exit_requires_fresh_pinned_session_quote(error_code: str) -> None:
    store = _Store()
    adapter = _Adapter(store, target_error_code=error_code)
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(), now=_NOW)

    assert result.retryable is True
    assert result.progress.phase == "exit"
    assert result.progress.status == "blocked"
    assert result.progress.last_error_code == error_code
    assert adapter.calls == []
    assert adapter.target_requests == [("AAPL", Decimal("0"))]


@pytest.mark.anyio
async def test_partial_exit_blocks_every_dependent_entry() -> None:
    store = _Store()
    adapter = _Adapter(store, partial_exit=True)
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(), now=_NOW)

    assert result.retryable is True
    assert result.progress.phase == "exit"
    assert result.progress.status == "blocked"
    assert adapter.calls == [("CLOSE", "AAPL", Decimal("4"))]


@pytest.mark.anyio
async def test_ambiguous_durable_exit_blocks_entry_without_duplicate_submission() -> None:
    store = _Store()
    store.durable[_EXIT_ID] = "ambiguous"
    adapter = _Adapter(store)
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(), now=_NOW)

    assert result.retryable is True
    assert result.progress.phase == "exit"
    assert result.progress.status == "blocked"
    assert store.legs[_EXIT_ID].status == "submitted"
    assert adapter.calls == []


@pytest.mark.anyio
async def test_retryable_pre_submission_exit_is_reclaimed_without_reconciliation() -> None:
    store = _Store()
    store.durable[_EXIT_ID] = "retryable_pre_submission"
    adapter = _Adapter(store)
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(), now=_NOW)

    assert result.progress.status == "completed"
    assert adapter.calls == [
        ("CLOSE", "AAPL", Decimal("4")),
        ("LONG", "MSFT", Decimal("5")),
    ]


@pytest.mark.anyio
async def test_partial_leg_never_regresses_to_submitted_during_recovery() -> None:
    partial_leg = replace(_legs()[0], status="partial")
    store = _Store(legs=(partial_leg, _legs()[1]))
    store.durable[_EXIT_ID] = "pending"
    adapter = _Adapter(store)
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(), now=_NOW)

    assert result.retryable is True
    assert store.legs[_EXIT_ID].status == "partial"
    assert adapter.calls == []


@pytest.mark.anyio
async def test_manual_plan_executes_risk_reduction_but_never_entry() -> None:
    store = _Store()
    adapter = _Adapter(store)
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(autopilot=False), now=_NOW)

    assert result.progress.status == "degraded"
    assert adapter.calls == [("CLOSE", "AAPL", Decimal("4"))]
    assert store.legs[_ENTRY_ID].status == "skipped"


@pytest.mark.anyio
async def test_manual_hold_target_can_reduce_but_cannot_increase() -> None:
    store = _Store()
    adapter = _Adapter(store)
    adapter.fifo["MSFT"] = Decimal("10")
    adapter.broker["MSFT"] = Decimal("10")
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(autopilot=False), now=_NOW)

    assert result.progress.status == "completed"
    assert adapter.calls == [
        ("CLOSE", "AAPL", Decimal("4")),
        ("CLOSE", "MSFT", Decimal("5")),
    ]
    assert adapter.fifo["MSFT"] == Decimal("5")
    assert adapter.broker["MSFT"] == Decimal("5")


@pytest.mark.anyio
async def test_restart_recovers_confirmed_exit_without_duplicate_submission() -> None:
    store = _Store()
    store.durable[_EXIT_ID] = "confirmed"
    adapter = _Adapter(store)
    adapter.fifo["AAPL"] = Decimal("0")
    adapter.broker["AAPL"] = Decimal("6")
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(), now=_NOW)

    assert result.progress.status == "completed"
    assert adapter.calls == [("LONG", "MSFT", Decimal("5"))]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("current_quantity", "expected_status"),
    [(Decimal("0"), "blocked"), (Decimal("5"), "completed")],
)
async def test_recovered_entry_fill_requires_current_target_postcondition(
    current_quantity: Decimal,
    expected_status: str,
) -> None:
    store = _Store()
    store.durable[_EXIT_ID] = "confirmed"
    store.durable[_ENTRY_ID] = "confirmed"
    adapter = _Adapter(store)
    adapter.fifo["AAPL"] = Decimal("0")
    adapter.broker["AAPL"] = Decimal("6")
    adapter.fifo["MSFT"] = current_quantity
    adapter.broker["MSFT"] = current_quantity
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(), now=_NOW)

    assert result.progress.status == expected_status
    assert adapter.calls == []
    if current_quantity == 0:
        assert result.retryable is True
        assert result.progress.last_error_code == "recovered_target_postcondition_unresolved"
    else:
        assert store.legs[_ENTRY_ID].status == "confirmed"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("current_quantity", "expected_status"),
    [(Decimal("10"), "blocked"), (Decimal("5"), "completed")],
)
async def test_recovered_reduction_fill_requires_current_target_postcondition(
    current_quantity: Decimal,
    expected_status: str,
) -> None:
    store = _Store()
    store.durable[_EXIT_ID] = "confirmed"
    store.durable[_ENTRY_ID] = "confirmed"
    adapter = _Adapter(store)
    adapter.fifo["AAPL"] = Decimal("0")
    adapter.broker["AAPL"] = Decimal("6")
    adapter.fifo["MSFT"] = current_quantity
    adapter.broker["MSFT"] = current_quantity
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(autopilot=False), now=_NOW)

    assert result.progress.status == expected_status
    assert adapter.calls == []
    if current_quantity == 10:
        assert result.retryable is True
        assert result.progress.last_error_code == "recovered_target_postcondition_unresolved"
    else:
        assert store.legs[_ENTRY_ID].status == "confirmed"


@pytest.mark.anyio
async def test_zero_leg_all_cash_plan_completes_without_broker_io() -> None:
    store = _Store(legs=())
    adapter = _Adapter(store)
    adapter.fifo.clear()
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(legs=[]), now=_NOW)

    assert result.progress.status == "completed"
    assert adapter.calls == []


@pytest.mark.anyio
async def test_zero_leg_plan_rejects_unmodelled_strategy_exposure() -> None:
    store = _Store(legs=())
    adapter = _Adapter(store)
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(legs=[]), now=_NOW)

    assert result.progress.status == "failed"
    assert result.progress.last_error_code == "strategy_position_outside_plan"
    assert adapter.calls == []


@pytest.mark.anyio
async def test_zero_selected_plan_preserves_exact_rejected_incumbent_hold() -> None:
    preserved = _rejected_position_leg(
        signal_action=SignalAction.HOLD,
        reason_code="manual_approval_required_hold_preserved",
    )
    store = _Store(legs=(preserved,))
    adapter = _Adapter(store)
    adapter.fifo = {"MSFT": Decimal("5")}
    adapter.broker = {"MSFT": Decimal("5")}
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(
        _command(autopilot=False, exits_enabled=False, legs=[]),
        now=_NOW,
    )

    assert result.progress.status == "completed"
    assert store.legs[preserved.plan_leg_id].status == "skipped"
    assert adapter.calls == []


@pytest.mark.anyio
async def test_rejected_new_entry_cannot_mask_strategy_fifo_exposure() -> None:
    rejected_entry = _rejected_position_leg(
        signal_action=SignalAction.LONG,
        reason_code="max_open_positions",
    )
    store = _Store(legs=(rejected_entry,))
    adapter = _Adapter(store)
    adapter.fifo = {"MSFT": Decimal("5")}
    adapter.broker = {"MSFT": Decimal("5")}
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(legs=[]), now=_NOW)

    assert result.progress.status == "failed"
    assert result.progress.last_error_code == "strategy_position_outside_plan"
    assert adapter.calls == []


@pytest.mark.anyio
async def test_exits_only_plan_rejects_extra_strategy_fifo_symbol() -> None:
    exit_command_leg = _command().legs[0]
    store = _Store(legs=(_legs()[0],))
    adapter = _Adapter(store)
    adapter.fifo["NVDA"] = Decimal("2")
    adapter.broker["NVDA"] = Decimal("2")
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(_command(legs=[exit_command_leg]), now=_NOW)

    assert result.progress.status == "failed"
    assert result.progress.last_error_code == "strategy_position_outside_plan"
    assert adapter.calls == [("CLOSE", "AAPL", Decimal("4"))]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("command", "code"),
    [
        (_command(data_use_scope="historical_validation"), "rebalance_data_scope_forbidden"),
        (_command(broker_environment="live"), "live_rebalance_disabled"),
    ],
)
async def test_historical_and_live_rebalance_commands_fail_before_store_claim(
    command: RebalanceExecutionCommandEvent,
    code: str,
) -> None:
    store = _Store()
    orchestrator = PortfolioRebalanceOrchestrator(
        store,
        _Adapter(store),
        worker_id="worker",
    )  # type: ignore[arg-type]

    with pytest.raises(RebalanceExecutionError) as exc_info:
        await orchestrator.process(command, now=_NOW)
    assert exc_info.value.code == code


def test_ibkr_paper_rebalance_command_passes_the_execution_fence() -> None:
    command = _command(broker="ibkr")

    PortfolioRebalanceOrchestrator._validate_paper_command(command)

    assert command.data_use_scope == "paper_forward"
    assert command.broker_route.broker_environment == "paper"
    assert command.broker_route.live_enabled is False
    assert command.broker_route.sandbox is True


def test_unapproved_sandbox_broker_fails_the_execution_fence() -> None:
    with pytest.raises(RebalanceExecutionError) as exc_info:
        PortfolioRebalanceOrchestrator._validate_paper_command(_command(broker="coinbase"))

    assert exc_info.value.code == "live_rebalance_disabled"


@pytest.mark.parametrize(
    ("broker", "broker_type"),
    [("paper", BrokerType.PAPER), ("ibkr", BrokerType.IBKR)],
)
def test_rebalance_adapter_accepts_exact_paper_broker_resolution(
    broker: str,
    broker_type: BrokerType,
) -> None:
    command = _command(broker=broker)
    resolved: Any = SimpleNamespace(
        environment="paper",
        broker_type=broker_type,
        broker_account_id=command.broker_route.broker_account_id,
        user_id=command.user_id,
    )

    ExecutionEngineRebalanceAdapter._validate_paper_resolution(command, resolved)


def test_rebalance_adapter_rejects_broker_outside_frozen_paper_route() -> None:
    command = _command(broker="ibkr")
    resolved: Any = SimpleNamespace(
        environment="paper",
        broker_type=BrokerType.PAPER,
        broker_account_id=command.broker_route.broker_account_id,
        user_id=command.user_id,
    )

    with pytest.raises(RebalanceExecutionError) as exc_info:
        ExecutionEngineRebalanceAdapter._validate_paper_resolution(command, resolved)

    assert exc_info.value.code == "paper_partition_mismatch"


def test_ibkr_entry_funding_uses_raw_usd_settled_cash_with_eur_base() -> None:
    adapter = ExecutionEngineRebalanceAdapter(
        SimpleNamespace(gatekeeper=SimpleNamespace(check_account_state_freshness=lambda **_: None)),
        None,  # type: ignore[arg-type]
    )
    command = _command(broker="ibkr", entry_cash_buffer_bps=10)
    account = AccountState(
        broker=BrokerType.IBKR,
        equity=10_000,
        available_cash=8_000,
        currency="EUR",
        balances=[
            {"currency": "EUR", "total": "2000", "available": "2000", "locked": "0"},
            {"currency": "USD", "total": "1000", "available": "1000", "locked": "0"},
        ],
        fetched_at=_NOW,
        source="broker",
        account_id="41",
    )
    override = TargetPositionQuantityOverride(
        account_plan_id=command.account_plan_id,
        plan_leg_id=_ENTRY_ID,
        symbol="MSFT",
        target_allocation=Decimal("0.05"),
        target_quantity=Decimal("5"),
        strategy_quantity=Decimal("0"),
        broker_quantity=Decimal("0"),
        target_weight_drift_fraction=Decimal("0.01"),
        broker_observed_at=_NOW,
        reference_price=Decimal("100"),
        quote_observed_at=_NOW,
        settlement_currency="USD",
    )

    adapter.validate_entry_funding(command, account, (override,))


def test_ibkr_entry_funding_rejects_unfunded_aggregate_batch() -> None:
    adapter = ExecutionEngineRebalanceAdapter(
        SimpleNamespace(gatekeeper=SimpleNamespace(check_account_state_freshness=lambda **_: None)),
        None,  # type: ignore[arg-type]
    )
    command = _command(broker="ibkr", entry_cash_buffer_bps=10)
    account = AccountState(
        broker=BrokerType.IBKR,
        equity=10_000,
        available_cash=20_000,
        currency="EUR",
        balances=[{"currency": "USD", "total": "1000", "available": "1000", "locked": "0"}],
        fetched_at=_NOW,
        source="broker",
        account_id="41",
    )
    first = TargetPositionQuantityOverride(
        account_plan_id=command.account_plan_id,
        plan_leg_id=_ENTRY_ID,
        symbol="MSFT",
        target_allocation=Decimal("0.05"),
        target_quantity=Decimal("5"),
        strategy_quantity=Decimal("0"),
        broker_quantity=Decimal("0"),
        target_weight_drift_fraction=Decimal("0.01"),
        broker_observed_at=_NOW,
        reference_price=Decimal("100"),
        quote_observed_at=_NOW,
        settlement_currency="USD",
    )
    second = replace(
        first,
        plan_leg_id="d" * 64,
        symbol="NVDA",
    )

    with pytest.raises(RebalanceExecutionError) as exc_info:
        adapter.validate_entry_funding(command, account, (first, second))

    assert exc_info.value.code == "ibkr_usd_settled_cash_insufficient"


@pytest.mark.parametrize(
    ("total", "available", "locked"),
    [
        ("-100", "1000", "0"),
        ("100", "1000", "0"),
        ("1000", "900", "0"),
    ],
)
def test_ibkr_entry_funding_rejects_inconsistent_usd_cash(
    total: str,
    available: str,
    locked: str,
) -> None:
    adapter = ExecutionEngineRebalanceAdapter(
        SimpleNamespace(gatekeeper=SimpleNamespace(check_account_state_freshness=lambda **_: None)),
        None,  # type: ignore[arg-type]
    )
    command = _command(broker="ibkr", entry_cash_buffer_bps=10)
    account = AccountState(
        broker=BrokerType.IBKR,
        equity=10_000,
        available_cash=10_000,
        balances=[
            {
                "currency": "USD",
                "total": total,
                "available": available,
                "locked": locked,
            }
        ],
        fetched_at=_NOW,
        source="broker",
        account_id="41",
    )
    override = TargetPositionQuantityOverride(
        account_plan_id=command.account_plan_id,
        plan_leg_id=_ENTRY_ID,
        symbol="MSFT",
        target_allocation=Decimal("0.05"),
        target_quantity=Decimal("5"),
        strategy_quantity=Decimal("0"),
        broker_quantity=Decimal("0"),
        target_weight_drift_fraction=Decimal("0.01"),
        broker_observed_at=_NOW,
        reference_price=Decimal("100"),
        quote_observed_at=_NOW,
        settlement_currency="USD",
    )

    with pytest.raises(RebalanceExecutionError) as exc_info:
        adapter.validate_entry_funding(command, account, (override,))

    assert exc_info.value.code == "ibkr_usd_settled_cash_unavailable"


def test_ibkr_entry_funding_requires_explicit_cost_buffer() -> None:
    adapter = ExecutionEngineRebalanceAdapter(
        SimpleNamespace(gatekeeper=SimpleNamespace(check_account_state_freshness=lambda **_: None)),
        None,  # type: ignore[arg-type]
    )
    command = _command(broker="ibkr")
    account = AccountState(
        broker=BrokerType.IBKR,
        equity=10_000,
        available_cash=10_000,
        balances=[{"currency": "USD", "total": "1000", "available": "1000", "locked": "0"}],
        fetched_at=_NOW,
        source="broker",
        account_id="41",
    )
    override = TargetPositionQuantityOverride(
        account_plan_id=command.account_plan_id,
        plan_leg_id=_ENTRY_ID,
        symbol="MSFT",
        target_allocation=Decimal("0.05"),
        target_quantity=Decimal("5"),
        strategy_quantity=Decimal("0"),
        broker_quantity=Decimal("0"),
        target_weight_drift_fraction=Decimal("0.01"),
        broker_observed_at=_NOW,
        reference_price=Decimal("100"),
        quote_observed_at=_NOW,
        settlement_currency="USD",
    )

    with pytest.raises(RebalanceExecutionError) as exc_info:
        adapter.validate_entry_funding(command, account, (override,))

    assert exc_info.value.code == "ibkr_entry_funding_policy_missing"


@pytest.mark.anyio
async def test_ibkr_funding_failure_allows_exit_but_blocks_first_entry() -> None:
    store = _Store()
    adapter = _RejectedIBKRFundingAdapter(store)
    orchestrator = PortfolioRebalanceOrchestrator(store, adapter, worker_id="worker")  # type: ignore[arg-type]

    result = await orchestrator.process(
        _command(broker="ibkr", entry_cash_buffer_bps=10),
        now=_NOW,
    )

    assert result.retryable is True
    assert result.progress.phase == "account_refresh"
    assert result.progress.status == "blocked"
    assert result.progress.last_error_code == "ibkr_usd_settled_cash_unavailable"
    assert adapter.calls == [("CLOSE", "AAPL", Decimal("4"))]


@pytest.mark.anyio
async def test_naive_operational_now_is_rejected() -> None:
    store = _Store()
    orchestrator = PortfolioRebalanceOrchestrator(
        store,
        _Adapter(store),
        worker_id="worker",
    )  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="timezone-aware"):
        await orchestrator.process(_command(), now=_NOW.replace(tzinfo=None))


def _sql_store(
    command: RebalanceExecutionCommandEvent,
) -> tuple[RebalancePlanStore, sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    _persist_plan(sessions, command)
    return RebalancePlanStore(sessions), sessions


def _persist_plan(
    sessions: sessionmaker[Session],
    command: RebalanceExecutionCommandEvent,
) -> None:
    policy = command.execution_policy.model_dump(mode="json")
    route = command.broker_route.model_dump(mode="json")
    with sessions() as session:
        session.add(
            AccountRebalancePlan(
                account_plan_id=command.account_plan_id,
                model_rebalance_id=command.model_rebalance_id,
                user_id=command.user_id,
                binding_id=command.binding_id,
                broker_account_id=command.broker_route.broker_account_id,
                strategy_id=command.strategy_id,
                data_use_scope=command.data_use_scope,
                provider_authority_sha256=command.provider_authority_sha256,
                decision_cutoff=command.decision_cutoff,
                execute_not_before=command.execute_not_before,
                execution_session_sha256=command.execution_session_sha256,
                expires_at=command.expires_at,
                execution_policy=policy,
                broker_route=route,
                execution_policy_sha256=canonical_json_hash(policy),
                broker_route_sha256=canonical_json_hash(route),
                content_sha256=command.content_sha256,
                model_leg_count=len(command.legs),
                execution_leg_count=len(command.legs),
                intentional_cash_slots=command.intentional_cash_slots,
                progress_deadline_at=_NOW + timedelta(hours=1),
            )
        )
        for model_sequence, leg in enumerate(command.legs):
            session.add(
                AccountRebalancePlanLeg(
                    account_plan_id=command.account_plan_id,
                    user_id=command.user_id,
                    broker_account_id=command.broker_route.broker_account_id,
                    model_sequence=model_sequence,
                    command_sequence=leg.sequence,
                    model_rebalance_id=command.model_rebalance_id,
                    model_leg_id=leg.model_leg_id,
                    model_signal_snapshot_sha256=leg.model_signal_snapshot_sha256,
                    plan_leg_id=leg.plan_leg_id,
                    instr_id=model_sequence + 1,
                    external_signal_id=leg.external_signal_id,
                    symbol=leg.symbol,
                    phase=leg.phase,
                    action="exit" if leg.phase == "exit" else "entry",
                    disposition="selected",
                    rank_position=(Decimal(str(leg.rank)) if leg.rank else None),
                    allocation_hint=Decimal(str(leg.allocation_hint)),
                    required=leg.required,
                    depends_on_sequences=list(leg.depends_on_sequences),
                    reason_code="selected_for_test",
                )
            )
        session.commit()


@pytest.mark.parametrize(
    ("decision_status", "expected"),
    [
        ("pending", "retryable_pre_submission"),
        ("executing", "pending"),
    ],
)
def test_persisted_execution_state_distinguishes_retryable_from_inflight_claim(
    decision_status: str,
    expected: str,
) -> None:
    store, sessions = _sql_store(_command())
    leg = _legs()[0]
    assert leg.canonical_signal_id is not None
    assert leg.signal is not None
    with sessions() as session:
        session.add(
            ExecutionDecisionLog(
                user_id="user-1",
                broker_account_id=41,
                canonical_signal_id=leg.canonical_signal_id,
                lineage_schema_version="v1",
                idempotency_key=f"dedup-{decision_status}",
                signal_id=leg.signal.signal_id,
                action="flat",
                status=decision_status,
            )
        )
        session.commit()

    assert (
        store.persisted_execution_state(
            leg,
            user_id="user-1",
            broker_account_id=41,
            strategy_id=_STRATEGY,
        )
        == expected
    )


def test_terminal_store_transition_seals_every_open_leg_in_same_transaction() -> None:
    command = _command()
    store, sessions = _sql_store(command)
    lease = store.claim(command, owner="terminal-test", now=_NOW)
    assert lease.acquired is True

    store.transition(
        lease,
        phase="failed",
        status="failed",
        now=_NOW,
        error_code="terminal_test_failure",
        error_detail="Terminal test failure",
        release_lease=True,
    )

    with sessions() as session:
        plan = session.get(AccountRebalancePlan, command.account_plan_id)
        assert plan is not None
        assert (plan.phase, plan.status) == ("failed", "failed")
        legs = (
            session.query(AccountRebalancePlanLeg)
            .filter(AccountRebalancePlanLeg.account_plan_id == command.account_plan_id)
            .all()
        )
        assert {leg.status for leg in legs} == {"failed"}
        assert {leg.last_error_code for leg in legs} == {"terminal_test_failure"}


def _mark_plan_failed(
    sessions: sessionmaker[Session],
    account_plan_id: str,
) -> None:
    with sessions() as session:
        plan = session.get(AccountRebalancePlan, account_plan_id)
        assert plan is not None
        plan.phase = "failed"
        plan.status = "failed"
        plan.last_error_code = "operator_review_required"
        plan.last_error_detail = "Broker state requires operator reconciliation"
        plan.last_progress_at = _NOW
        plan.updated_at = _NOW
        session.commit()


def _age_plan_past_expiry(
    sessions: sessionmaker[Session],
    account_plan_id: str,
    *,
    inflight: bool,
) -> None:
    with sessions() as session:
        plan = session.get(AccountRebalancePlan, account_plan_id)
        assert plan is not None
        plan.phase = "exit"
        plan.status = "blocked"
        plan.progress_deadline_at = _NOW - timedelta(minutes=1)
        plan.expires_at = _NOW - timedelta(seconds=1)
        plan.updated_at = _NOW - timedelta(minutes=2)
        if inflight:
            leg = (
                session.query(AccountRebalancePlanLeg)
                .filter(
                    AccountRebalancePlanLeg.account_plan_id == account_plan_id,
                    AccountRebalancePlanLeg.disposition == "selected",
                )
                .order_by(AccountRebalancePlanLeg.command_sequence)
                .first()
            )
            assert leg is not None
            leg.status = "submitted"
            leg.last_progress_at = _NOW - timedelta(minutes=2)
        session.commit()


def test_failed_plan_resolution_is_append_only_idempotent_and_restores_readiness() -> None:
    command = _command(legs=[])
    store, sessions = _sql_store(command)
    _mark_plan_failed(sessions, command.account_plan_id)

    before = store.readiness(now=_NOW)
    assert before.healthy is False
    assert before.failed_count == 1
    assert before.resolved_failed_count == 0

    first = store.resolve_failed_plan(
        command.account_plan_id,
        resolution_type="acknowledged",
        reason="Reviewed broker and canonical ledger state",
        evidence={"incident": "INC-42", "reconciliation": "complete"},
        resolved_by="operator@example.com",
        now=_NOW,
    )
    replay = store.resolve_failed_plan(
        command.account_plan_id,
        resolution_type="acknowledged",
        reason="Reviewed broker and canonical ledger state",
        evidence={"reconciliation": "complete", "incident": "INC-42"},
        resolved_by="operator@example.com",
        now=_NOW + timedelta(minutes=1),
    )
    follow_up = store.resolve_failed_plan(
        command.account_plan_id,
        resolution_type="remediated",
        reason="Manual reconciliation evidence retained",
        evidence={"incident": "INC-42", "artifact": "reconciliation-20260801.json"},
        resolved_by="operator@example.com",
        now=_NOW + timedelta(minutes=2),
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.resolution.resolution_id == first.resolution.resolution_id
    assert follow_up.replayed is False
    assert follow_up.resolution.resolution_id != first.resolution.resolution_id
    after = store.readiness(now=_NOW + timedelta(minutes=3))
    assert after.healthy is True
    assert after.failed_count == 0
    assert after.resolved_failed_count == 1
    assert after.affected_partitions == ()
    audit = store.audit(command.account_plan_id)
    assert [row["resolution_type"] for row in audit["failure_resolutions"]] == [
        "acknowledged",
        "remediated",
    ]
    with sessions() as session:
        plan = session.get(AccountRebalancePlan, command.account_plan_id)
        assert plan is not None
        assert plan.phase == "failed"
        assert plan.status == "failed"
        assert session.scalar(select(func.count()).select_from(AccountRebalancePlanResolution)) == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(ApiAuditLog)
                .where(ApiAuditLog.action == "rebalance.plan.failure_resolved")
            )
            == 2
        )


def test_nonfailed_plan_cannot_receive_failure_resolution() -> None:
    command = _command(legs=[])
    store, sessions = _sql_store(command)

    with pytest.raises(RebalancePlanContractError, match="only terminal failed"):
        store.resolve_failed_plan(
            command.account_plan_id,
            resolution_type="acknowledged",
            reason="Not actually failed",
            evidence={},
            resolved_by="operator@example.com",
            now=_NOW,
        )

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AccountRebalancePlanResolution)) == 0


def test_store_fences_concurrent_owner_and_rejects_stale_generation() -> None:
    command = _command()
    store, _sessions = _sql_store(command)

    first = store.claim(command, owner="worker-1", now=_NOW)
    second = store.claim(command, owner="worker-2", now=_NOW)

    assert first.acquired is True
    assert first.generation == 1
    assert second.acquired is False
    assert second.blocked_reason == "lease_held"
    store.transition(
        first,
        phase="exit",
        status="blocked",
        now=_NOW,
        release_lease=True,
    )
    resumed = store.claim(command, owner="worker-2", now=_NOW)
    assert resumed.acquired is True
    assert resumed.generation == 2
    with pytest.raises(RebalanceLeaseLostError):
        store.transition(
            first,
            phase="failed",
            status="failed",
            now=_NOW,
        )


def test_newer_strategy_plan_waits_for_same_account_older_plan() -> None:
    older = _command(
        legs=[],
        strategy_id="first_account_strategy",
        model_rebalance_id="1" * 64,
    )
    newer = _command(
        legs=[],
        strategy_id="second_account_strategy",
        model_rebalance_id="2" * 64,
        execution_offset=timedelta(minutes=1),
    )
    store, sessions = _sql_store(older)
    _persist_plan(sessions, newer)

    blocked = store.claim(newer, owner="worker-2", now=_NOW)

    assert blocked.acquired is False
    assert blocked.blocked_reason == "older_plan_unresolved"
    with sessions() as session:
        newer_plan = session.get(AccountRebalancePlan, newer.account_plan_id)
        assert newer_plan is not None
        assert newer_plan.status == "blocked"
        assert newer_plan.last_error_code == "older_plan_unresolved"


def test_unresolved_failed_older_plan_blocks_until_operator_resolution() -> None:
    older = _command(
        legs=[],
        strategy_id="first_account_strategy",
        model_rebalance_id="3" * 64,
    )
    newer = _command(
        legs=[],
        strategy_id="second_account_strategy",
        model_rebalance_id="4" * 64,
        execution_offset=timedelta(minutes=1),
    )
    store, sessions = _sql_store(older)
    _persist_plan(sessions, newer)
    _mark_plan_failed(sessions, older.account_plan_id)

    blocked = store.claim(newer, owner="worker-2", now=_NOW)

    assert blocked.acquired is False
    assert blocked.blocked_reason == "older_plan_unresolved"
    assert store.readiness(now=_NOW).failed_count == 1

    store.resolve_failed_plan(
        older.account_plan_id,
        resolution_type="reconciled",
        reason="Canonical and broker ledgers reconcile with no open submission",
        evidence={"incident": "INC-FAILED-OLDER", "orders_open": 0},
        resolved_by="operator@example.com",
        now=_NOW,
    )
    resumed = store.claim(newer, owner="worker-2", now=_NOW)

    assert resumed.acquired is True


def test_inflight_expiry_fails_closed_and_blocks_until_resolution() -> None:
    older = _command(
        strategy_id="first_account_strategy",
        model_rebalance_id="5" * 64,
    )
    newer = _command(
        legs=[],
        strategy_id="second_account_strategy",
        model_rebalance_id="6" * 64,
        execution_offset=timedelta(minutes=1),
    )
    store, sessions = _sql_store(older)
    _persist_plan(sessions, newer)
    _age_plan_past_expiry(sessions, older.account_plan_id, inflight=True)

    blocked = store.claim(newer, owner="worker-2", now=_NOW)

    assert blocked.acquired is False
    assert blocked.blocked_reason == "older_plan_unresolved"
    with sessions() as session:
        expired = session.get(AccountRebalancePlan, older.account_plan_id)
        assert expired is not None
        assert expired.phase == "failed"
        assert expired.status == "failed"
        assert expired.last_error_code == "plan_expired_with_inflight_execution"
    assert store.readiness(now=_NOW).failed_count == 1

    store.resolve_failed_plan(
        older.account_plan_id,
        resolution_type="reconciled",
        reason="Expired in-flight order is terminal and broker ledger reconciles",
        evidence={"incident": "INC-EXPIRED-INFLIGHT", "orders_open": 0},
        resolved_by="operator@example.com",
        now=_NOW,
    )
    resumed = store.claim(newer, owner="worker-2", now=_NOW)

    assert resumed.acquired is True


def test_untouched_expired_plan_does_not_block_newer_same_account_plan() -> None:
    older = _command(
        legs=[],
        strategy_id="first_account_strategy",
        model_rebalance_id="7" * 64,
    )
    newer = _command(
        legs=[],
        strategy_id="second_account_strategy",
        model_rebalance_id="8" * 64,
        execution_offset=timedelta(minutes=1),
    )
    store, sessions = _sql_store(older)
    _persist_plan(sessions, newer)
    _age_plan_past_expiry(sessions, older.account_plan_id, inflight=False)

    acquired = store.claim(newer, owner="worker-2", now=_NOW)

    assert acquired.acquired is True
    with sessions() as session:
        expired = session.get(AccountRebalancePlan, older.account_plan_id)
        assert expired is not None
        assert expired.phase == "expired"
        assert expired.status == "expired"


@pytest.mark.parametrize(
    ("inflight", "expected_status"),
    [(False, "expired"), (True, "failed")],
)
def test_expiry_claim_reports_the_exact_persisted_terminal_status(
    inflight: bool,
    expected_status: str,
) -> None:
    command = _command()
    store, sessions = _sql_store(command)
    if inflight:
        with sessions() as session:
            leg = (
                session.query(AccountRebalancePlanLeg)
                .filter(AccountRebalancePlanLeg.plan_leg_id == _EXIT_ID)
                .one_or_none()
            )
            assert leg is not None
            leg.status = "submitted"
            session.commit()

    lease = store.claim(
        command,
        owner="expiry-worker",
        now=command.expires_at + timedelta(seconds=1),
    )

    assert lease.acquired is False
    assert lease.terminal_status == expected_status
    with sessions() as session:
        plan = session.get(AccountRebalancePlan, command.account_plan_id)
        assert plan is not None
        assert plan.status == expected_status


def test_unresolved_failed_plan_isolated_to_exact_user_account_partition() -> None:
    older = _command(
        legs=[],
        strategy_id="first_account_strategy",
        model_rebalance_id="a" * 63 + "1",
        broker_account_id=41,
    )
    other_account = _command(
        legs=[],
        strategy_id="second_account_strategy",
        model_rebalance_id="a" * 63 + "2",
        execution_offset=timedelta(minutes=1),
        broker_account_id=42,
    )
    store, sessions = _sql_store(older)
    _persist_plan(sessions, other_account)
    _mark_plan_failed(sessions, older.account_plan_id)

    acquired = store.claim(other_account, owner="worker-2", now=_NOW)

    assert acquired.acquired is True


def _postgres_rebalance_store(
    command: RebalanceExecutionCommandEvent,
) -> tuple[Engine, Engine, RebalancePlanStore, sessionmaker[Session], str]:
    raw_url = os.getenv("DATABASE_URL")
    if not raw_url:
        pytest.skip("DATABASE_URL is required for PostgreSQL rebalance acceptance")
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql") or "async" in url.drivername:
        pytest.skip("PostgreSQL rebalance acceptance requires a synchronous PostgreSQL URL")

    admin_engine = create_engine(raw_url, future=True)
    schema = f"rebalance_fence_{uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        for table_name in (
            "account_execution_generations",
            "account_rebalance_plans",
            "account_rebalance_plan_legs",
            "api_audit_logs",
        ):
            connection.exec_driver_sql(
                f'CREATE TABLE "{schema}"."{table_name}" (LIKE public."{table_name}" INCLUDING ALL)'
            )
    runtime_engine = create_engine(
        raw_url,
        future=True,
        pool_size=3,
        max_overflow=0,
        connect_args={"options": f"-csearch_path={schema},public"},
    )
    sessions = sessionmaker(runtime_engine, expire_on_commit=False)
    _persist_plan(sessions, command)
    return admin_engine, runtime_engine, RebalancePlanStore(sessions), sessions, schema


@pytest.mark.integration
def test_postgres_concurrent_claim_fences_duplicate_execution() -> None:
    command = _command(legs=[])
    admin_engine, runtime_engine, store, sessions, schema = _postgres_rebalance_store(command)
    ready = Barrier(2)

    def _claim(owner: str) -> RebalancePlanLease:
        ready.wait(timeout=10)
        return store.claim(command, owner=owner, now=_NOW)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(_claim, f"worker-{index}") for index in range(2)]
            claims = [future.result(timeout=20) for future in futures]

        acquired = [claim for claim in claims if claim.acquired]
        blocked = [claim for claim in claims if not claim.acquired]
        assert len(acquired) == 1
        assert acquired[0].generation == 1
        assert len(blocked) == 1
        assert blocked[0].blocked_reason == "lease_held"
        assert blocked[0].generation == 1
        with sessions() as session:
            plan = session.get(AccountRebalancePlan, command.account_plan_id)
            assert plan is not None
            assert plan.fence_generation == 1
            claim_count = session.scalar(
                select(func.count())
                .select_from(ApiAuditLog)
                .where(ApiAuditLog.action == "rebalance.plan.claim")
            )
            assert claim_count == 1
    finally:
        runtime_engine.dispose()
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin_engine.dispose()


@pytest.mark.anyio
@pytest.mark.integration
async def test_postgres_account_serializer_fences_replicas_and_advances_generation() -> None:
    command = _command(legs=[])
    admin_engine, runtime_engine, _store, sessions, schema = _postgres_rebalance_store(command)
    first = AccountExecutionSerializer(sessions)
    contender = AccountExecutionSerializer(sessions)

    try:
        async with first.hold(
            user_id=command.user_id,
            broker_account_id=command.broker_route.broker_account_id,
            writer_kind="rebalance",
            account_plan_id=command.account_plan_id,
        ) as first_lease:
            assert first_lease.generation == 1
            with pytest.raises(AccountExecutionBusyError):
                async with contender.hold(
                    user_id=command.user_id,
                    broker_account_id=command.broker_route.broker_account_id,
                    writer_kind="ordinary",
                ):
                    pytest.fail("another replica must not acquire the advisory account lock")

        async with contender.hold(
            user_id=command.user_id,
            broker_account_id=command.broker_route.broker_account_id,
            writer_kind="ordinary",
        ) as second_lease:
            assert second_lease.generation == 2
            with sessions() as session:
                row = session.get(
                    AccountExecutionGeneration,
                    {
                        "user_id": command.user_id,
                        "broker_account_id": command.broker_route.broker_account_id,
                    },
                )
                assert row is not None
                assert row.generation == 2
                assert row.active_owner == second_lease.owner
                assert row.active_writer_kind == "ordinary"

        with sessions() as session:
            row = session.get(
                AccountExecutionGeneration,
                {
                    "user_id": command.user_id,
                    "broker_account_id": command.broker_route.broker_account_id,
                },
            )
            assert row is not None
            assert row.generation == 2
            assert row.active_owner is None
            assert row.released_at is not None
    finally:
        runtime_engine.dispose()
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin_engine.dispose()


def test_store_rejects_cross_tenant_command_before_mutation() -> None:
    command = _command()
    store, sessions = _sql_store(command)
    spoofed = command.model_copy(update={"user_id": "attacker"})

    with pytest.raises(RebalancePlanContractError):
        store.claim(spoofed, owner="worker", now=_NOW)

    with sessions() as session:
        plan = session.get(AccountRebalancePlan, command.account_plan_id)
        assert plan is not None
        assert plan.status == "pending"
        assert plan.fence_generation == 0


def test_store_audit_preserves_claim_and_phase_leg_transitions() -> None:
    command = _command()
    store, _sessions = _sql_store(command)
    lease = store.claim(command, owner="worker", now=_NOW)
    store.mark_leg(
        lease,
        plan_leg_id=_EXIT_ID,
        status="submitted",
        now=_NOW,
    )
    store.transition(
        lease,
        phase="exit",
        status="blocked",
        now=_NOW,
        error_code="exit_pending",
        release_lease=True,
    )

    audit = store.audit(command.account_plan_id)

    assert [row["action"] for row in audit["transitions"]] == [
        "rebalance.plan.claim",
        "rebalance.leg.transition",
        "rebalance.plan.transition",
    ]


def test_execution_reconstructs_signal_from_frozen_model_snapshot() -> None:
    frozen = CanonicalSignalSnapshot(
        signal_id="transient-worker-id",
        strategy_id=_STRATEGY,
        strategy_type="indicator",
        symbol="MSFT",
        action="long",
        confidence=0.8,
        timestamp=_NOW,
        entry_price=100,
        asset_class="equity",
        instrument_id="2",
        strategy_version="1.0.0",
        source="paper",
        external_signal_id="signal-msft",
        metadata={"target_weight_drift_fraction": "0.15"},
    )
    snapshot = frozen.model_dump(mode="json")
    digest = canonical_json_hash(snapshot)
    model_leg = SimpleNamespace(
        signal_snapshot=snapshot,
        signal_snapshot_sha256=digest,
    )
    plan_leg = SimpleNamespace(
        plan_leg_id=_ENTRY_ID,
        account_plan_id="a" * 64,
        model_signal_snapshot_sha256=digest,
        disposition="selected",
        instr_id=2,
        symbol="MSFT",
        external_signal_id="signal-msft",
        model_sequence=1,
        command_sequence=1,
        phase="entry",
        action="entry",
        status="pending",
        allocation_hint=Decimal("0.05"),
        required=True,
        depends_on_sequences=[],
        reason_code="ranked_entry",
        frozen_target_quantity=None,
        target_account_generation=None,
        target_frozen_at=None,
    )
    mutated_canonical = SimpleNamespace(
        signal_id=101,
        instr_id=2,
        strategy_id=_STRATEGY,
        external_signal_id="signal-msft",
        action="long",
        ts=_NOW,
        confidence=Decimal("0.01"),
        signal_meta={"target_weight_drift_fraction": "0.99"},
    )

    result = RebalancePlanStore._leg_snapshot(
        plan_leg=plan_leg,
        model_leg=model_leg,
        canonical=mutated_canonical,
        instrument=SimpleNamespace(instr_id=2, canonical="MSFT", asset_class="equity"),
        version=SimpleNamespace(strategy_id=_STRATEGY, semver="1.0.0"),
    )

    assert result.signal is not None
    assert result.signal.signal_id == "signal-msft"
    assert result.signal.confidence == 0.8
    assert result.signal.metadata["target_weight_drift_fraction"] == "0.15"

    model_leg.signal_snapshot["confidence"] = 0.01
    with pytest.raises(RebalancePlanContractError, match="signal snapshot changed"):
        RebalancePlanStore._leg_snapshot(
            plan_leg=plan_leg,
            model_leg=model_leg,
            canonical=mutated_canonical,
            instrument=SimpleNamespace(instr_id=2, canonical="MSFT", asset_class="equity"),
            version=SimpleNamespace(strategy_id=_STRATEGY, semver="1.0.0"),
        )


def test_order_builder_close_override_does_not_change_ordinary_close_semantics() -> None:
    signal = _signal("AAPL", SignalAction.CLOSE, 1)
    account = AccountState(
        broker=BrokerType.PAPER,
        equity=10_000,
        available_cash=8_000,
        positions=[{"symbol": "AAPL", "qty": 10.0, "side": "long"}],
        fetched_at=_NOW,
    )
    config = {"broker": "paper", "execution_mode": "spot"}
    ordinary = OrderBuilder().build(
        ExecutionRequest(
            user_id="user-1",
            profile={},
            user_strategy_config=config,
            signal=signal,
        ),
        account=account,
        market_data={"price": 100},
    )
    fenced = OrderBuilder().build(
        ExecutionRequest(
            user_id="user-1",
            profile={},
            user_strategy_config=config,
            signal=signal,
            close_quantity_override=CloseQuantityOverride(
                account_plan_id="a" * 64,
                plan_leg_id="b" * 64,
                symbol="AAPL",
                quantity=Decimal("4"),
                strategy_quantity=Decimal("4"),
                broker_quantity=Decimal("10"),
                broker_observed_at=_NOW,
            ),
        ),
        account=account,
        market_data={"price": 100},
    )

    assert ordinary[0].quantity == 10
    assert fenced[0].quantity == 4
    assert fenced[0].metadata is not None
    assert fenced[0].metadata["rebalance_close_override"]["clamped_quantity"] == "4"  # type: ignore[index]


def test_order_builder_submits_only_target_delta_after_revalidation() -> None:
    signal = _signal("MSFT", SignalAction.LONG, 2)
    account = AccountState(
        broker=BrokerType.PAPER,
        equity=10_000,
        available_cash=1_000,
        positions=[{"symbol": "MSFT", "qty": 2.0, "side": "long"}],
        fetched_at=_NOW,
    )
    override = TargetPositionQuantityOverride(
        account_plan_id="a" * 64,
        plan_leg_id="c" * 64,
        symbol="MSFT",
        target_allocation=Decimal("0.05"),
        target_quantity=Decimal("5"),
        strategy_quantity=Decimal("2"),
        broker_quantity=Decimal("2"),
        target_weight_drift_fraction=Decimal("0.01"),
        broker_observed_at=_NOW,
        reference_price=Decimal("100"),
        quote_observed_at=_NOW,
    )
    intents = OrderBuilder().build(
        ExecutionRequest(
            user_id="user-1",
            profile={},
            user_strategy_config={
                "broker": "paper",
                "execution_mode": "spot",
                "sizing": {
                    "method": "fixed_pct",
                    "fixed_pct": 0.05,
                    "max_position_pct": 0.05,
                },
            },
            signal=signal,
            target_position_override=override,
        ),
        account=account,
        market_data={"price": 80},
    )

    assert len(intents) == 1
    assert intents[0].side == "BUY"
    assert intents[0].quantity == 3
    assert intents[0].metadata is not None
    assert intents[0].metadata["reference_price"] == 100
    assert intents[0].metadata["rebalance_target_override"]["quote_observed_at"] == (  # type: ignore[index]
        _NOW.isoformat()
    )


def test_dedicated_api_completes_zero_leg_paper_plan() -> None:
    command = _command(legs=[])
    _store, sessions = _sql_store(command)

    class _Engine:
        _session_factory = sessions

        @staticmethod
        def _execution_services() -> object:
            class _PnLService:
                @staticmethod
                async def get_fifo_positions(*_args: Any, **_kwargs: Any) -> dict[str, Decimal]:
                    return {}

            class _ReconciliationTracker:
                @staticmethod
                def pending_orders_for_account(*_args: Any) -> dict[str, object]:
                    return {}

            return SimpleNamespace(
                pnl_service=_PnLService(),
                reconciliation_tracker=_ReconciliationTracker(),
            )

        @staticmethod
        async def handle_signal(**_: Any) -> ExecutionResult:
            raise AssertionError("zero-leg plan must not invoke single-signal execution")

        @staticmethod
        def is_reconciliation_healthy() -> bool:
            return True

        @staticmethod
        def is_paper_rehydration_healthy() -> bool:
            return True

        @staticmethod
        def reconciliation_status() -> dict[str, object]:
            return {"initial_reconciliation_complete": True, "discovered_partitions": 0}

    response = TestClient(create_app(_Engine())).post(  # type: ignore[arg-type]
        "/execute-rebalance-command",
        json=command.model_dump(mode="json"),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["result"]["status"] == "completed"


def test_admin_failed_plan_resolution_requires_admin_key_and_operator(monkeypatch: Any) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY", "service-key")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key")
    command = _command(legs=[])
    _store, sessions = _sql_store(command)
    _mark_plan_failed(sessions, command.account_plan_id)

    class _Engine:
        _session_factory = sessions

        @staticmethod
        def _execution_services() -> object:
            return object()

        @staticmethod
        async def handle_signal(**_: Any) -> ExecutionResult:
            raise AssertionError("failure resolution must not invoke signal execution")

        @staticmethod
        def is_reconciliation_healthy() -> bool:
            return True

        @staticmethod
        def is_paper_rehydration_healthy() -> bool:
            return True

        @staticmethod
        def reconciliation_status() -> dict[str, object]:
            return {"initial_reconciliation_complete": True, "discovered_partitions": 0}

    client = TestClient(create_app(_Engine()))  # type: ignore[arg-type]
    path = f"/admin/rebalance-plans/{command.account_plan_id}/resolve-failure"
    body = {
        "resolution_type": "reconciled",
        "reason": "Broker and canonical ledger reconciled",
        "evidence": {"incident": "INC-42"},
    }

    missing_admin = client.post(path, json=body, headers={"X-API-Key": "service-key"})
    missing_operator = client.post(
        path,
        json=body,
        headers={"X-API-Key": "service-key", "X-Admin-API-Key": "admin-key"},
    )
    resolved = client.post(
        path,
        json=body,
        headers={
            "X-API-Key": "service-key",
            "X-Admin-API-Key": "admin-key",
            "X-Admin-User": "operator@example.com",
        },
    )

    assert missing_admin.status_code == 403
    assert missing_operator.status_code == 400
    assert resolved.status_code == 200
    assert resolved.json()["resolution"]["replayed"] is False
    readiness = client.get(
        "/admin/rebalance-readiness",
        headers={"X-API-Key": "service-key", "X-Admin-API-Key": "admin-key"},
    )
    assert readiness.status_code == 200
    assert readiness.json()["rebalance"]["healthy"] is True
    assert readiness.json()["rebalance"]["resolved_failed_count"] == 1
