# ruff: noqa: EM101
"""Fenced paper execution for durable account-scoped portfolio plans."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import uuid4

from lib_common.hashing import canonical_json_hash
from lib_common.internal_events import RebalanceExecutionCommandEvent
from lib_common.logging import get_logger
from lib_common.metrics import counter
from lib_data.market_data import normalize_product_symbol
from lib_strategy.signals.signal import Signal, SignalAction

from .account_execution_serializer import (
    AccountExecutionLease,
    AccountExecutionSerializer,
)
from .config import AccountState
from .execution_result import ExecutionResult, is_retryable_failure
from .metrics.normalization import normalize_position_side
from .models import (
    ORDINARY_REBALANCE_EXIT_METADATA_KEY,
    TargetPositionQuantityOverride,
)
from .rebalance_execution_adapter import (
    RebalanceExecutionAdapter,
    RebalanceExecutionError,
    RebalanceProcessResult,
    execution_error,
)
from .rebalance_store import (
    FailureResolutionType,
    PlanPhase,
    RebalancePlanLease,
    RebalancePlanLegSnapshot,
    RebalancePlanStore,
)

logger = get_logger(__name__)

_POSITION_TOLERANCE = Decimal("0.00000001")
_RECOVERABLE_RESULT_STATUSES = frozenset(
    {"new", "partially_filled", "pending", "submission_unknown", "submitted", "working"}
)
_PRESERVED_REJECTED_INCUMBENT_REASONS = frozenset(
    {
        "entry_authority_disabled_hold_preserved",
        "manual_approval_required_hold_preserved",
    }
)
_REBALANCE_TRANSITIONS = counter(
    "vm_execution_rebalance_transitions_total",
    "Durable portfolio rebalance transitions by phase and outcome",
    ("phase", "outcome"),
)


class PortfolioRebalanceOrchestrator:
    """Execute immutable paper-forward plans through fenced durable phases."""

    def __init__(
        self,
        store: RebalancePlanStore,
        adapter: RebalanceExecutionAdapter,
        *,
        worker_id: str,
        account_serializer: AccountExecutionSerializer | None = None,
    ) -> None:
        normalized_worker = str(worker_id or "").strip()
        if not normalized_worker:
            msg = "Portfolio rebalance worker_id must not be empty"
            raise ValueError(msg)
        self._store = store
        self._adapter = adapter
        self._worker_id = normalized_worker
        self._account_serializer = account_serializer or AccountExecutionSerializer(None)

    async def process(
        self,
        command: RebalanceExecutionCommandEvent,
        *,
        now: datetime | None = None,
    ) -> RebalanceProcessResult:
        """Claim and advance one plan inside the account-wide writer fence."""
        current = self._operational_now(now)
        self._validate_paper_command(command)
        async with self._account_serializer.hold(
            user_id=command.user_id,
            broker_account_id=command.broker_route.broker_account_id,
            writer_kind="rebalance",
            account_plan_id=command.account_plan_id,
        ) as account_lease:
            return await self._process_fenced(
                command,
                account_lease=account_lease,
                current=current,
            )

    async def _process_fenced(
        self,
        command: RebalanceExecutionCommandEvent,
        *,
        account_lease: AccountExecutionLease,
        current: datetime,
    ) -> RebalanceProcessResult:
        """Advance a plan while one exact account generation remains current."""
        self._account_serializer.assert_current(account_lease)
        lease = self._store.claim(
            command,
            owner=f"{self._worker_id}:{uuid4()}",
            now=current,
        )
        if not lease.acquired:
            return self._unclaimed(command.account_plan_id, lease)
        try:
            legs = self._store.load_legs(command)
            selected = tuple(leg for leg in legs if leg.disposition == "selected")
            self._validate_legs(command, selected)
            await self._freeze_preserved_targets(command, lease, legs)
            self._skip_rejected(lease, legs)
            exit_legs = tuple(leg for leg in selected if leg.phase in {"exit", "reduce"})
            entry_legs = tuple(leg for leg in selected if leg.phase == "entry")
            error = await self._process_declared_reductions(command, lease, exit_legs)
            if error is not None:
                return self._stop(lease, phase="exit", error=error)
            if not entry_legs:
                # The frozen model plan is a decision artifact, not the broker
                # or FIFO ledger. Even an all-cash or exits-only plan must
                # prove that no strategy-owned exposure was omitted before it
                # can become terminal completed.
                terminal_targets_sha256 = await self._terminal_target_reconciliation(
                    command,
                    lease,
                    exit_legs=exit_legs,
                )
                return self._complete(
                    lease,
                    degraded=False,
                    terminal_targets_sha256=terminal_targets_sha256,
                )
            self._store.transition(
                lease,
                phase="account_refresh",
                status="running",
                now=self._now(),
            )
            account, fifo = await self._refresh(
                command,
                self._target_signal(entry_legs[0], reduction=False),
            )
            self._validate_completed_exits(exit_legs, fifo)
            self._validate_positions_covered(legs, fifo)
            error = await self._process_entry_reductions(
                command,
                lease,
                entry_legs,
                account=account,
                fifo=fifo,
            )
            if error is not None:
                return self._stop(lease, phase="account_refresh", error=error)
            # Target-driven reductions update durable leg status. Reload the
            # fenced projection before the increase phase so a confirmed
            # reduction cannot be overwritten as an unauthorized/skipped
            # entry by stale in-memory snapshots.
            entry_legs = tuple(
                leg
                for leg in self._store.load_legs(command)
                if leg.disposition == "selected" and leg.phase == "entry"
            )
            await self._preflight_entry_funding(command, lease, entry_legs)
            self._store.transition(
                lease,
                phase="entry",
                status="running",
                now=self._now(),
            )
            degraded = await self._process_entries(command, lease, entry_legs)
            # A per-leg postcondition proves only the symbol just traded. Re-read
            # the strategy FIFO immediately before sealing the batch so exposure
            # introduced by another same-strategy path after the initial account
            # refresh cannot be silently omitted from the completed plan audit.
            terminal_targets_sha256 = await self._terminal_target_reconciliation(
                command,
                lease,
                exit_legs=exit_legs,
            )
            return self._complete(
                lease,
                degraded=degraded,
                terminal_targets_sha256=terminal_targets_sha256,
            )
        except RebalanceExecutionError as exc:
            current_phase = str(self._store.progress(command.account_plan_id).phase)
            return self._stop(lease, phase=current_phase, error=exc)

    def readiness(self, *, now: datetime | None = None) -> dict[str, object]:
        return self._store.readiness(now=self._operational_now(now)).to_dict()

    def progress(self, account_plan_id: str) -> dict[str, object]:
        return self._store.progress(account_plan_id).to_dict()

    def audit(self, account_plan_id: str) -> dict[str, object]:
        return self._store.audit(account_plan_id)

    def resolve_failure(
        self,
        account_plan_id: str,
        *,
        resolution_type: FailureResolutionType,
        reason: str,
        evidence: dict[str, object],
        resolved_by: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Append an operator-attributed terminal-failure resolution."""
        return self._store.resolve_failed_plan(
            account_plan_id,
            resolution_type=resolution_type,
            reason=reason,
            evidence=evidence,
            resolved_by=resolved_by,
            now=self._operational_now(now),
        ).to_dict()

    async def _process_declared_reductions(
        self,
        command: RebalanceExecutionCommandEvent,
        lease: RebalancePlanLease,
        legs: tuple[RebalancePlanLegSnapshot, ...],
    ) -> RebalanceExecutionError | None:
        for leg in legs:
            if not command.execution_policy.exits_enabled:
                self._mark(
                    lease,
                    leg,
                    "skipped",
                    code="exit_authority_disabled",
                    detail="Frozen policy does not authorize risk-reducing execution",
                )
                return execution_error(
                    "exit_authority_disabled",
                    "Required exit/reduction is not authorized by frozen policy",
                    retryable=False,
                )
            recovery = self._recover(command, lease, leg, phase="exit")
            if recovery is not None:
                if recovery is False:
                    continue
                if recovery == "confirmed":
                    postcondition_error = await self._validate_recovered_confirmation(
                        command,
                        lease,
                        leg,
                    )
                    if postcondition_error is not None:
                        return postcondition_error
                    continue
                if isinstance(recovery, RebalanceExecutionError):
                    return recovery
                raise AssertionError("Unknown rebalance recovery state")
            error = (
                await self._execute_full_exit(command, lease, leg)
                if leg.phase == "exit" and leg.action == "exit"
                else await self._execute_target_reduction(command, lease, leg)
            )
            if error is not None:
                return error
        return None

    async def _execute_full_exit(  # noqa: PLR0911
        self,
        command: RebalanceExecutionCommandEvent,
        lease: RebalancePlanLease,
        leg: RebalancePlanLegSnapshot,
    ) -> RebalanceExecutionError | None:
        signal = self._required_signal(leg)
        account, fifo = await self._refresh(command, signal)
        strategy_quantity = self._strategy_quantity(fifo, signal.symbol)
        if abs(strategy_quantity) <= _POSITION_TOLERANCE:
            self._freeze_quantity(lease, leg, Decimal("0"))
            self._mark(lease, leg, "confirmed")
            return None
        broker_quantity = self._broker_quantity(account, signal.symbol)
        if abs(broker_quantity) <= _POSITION_TOLERANCE:
            return execution_error(
                "broker_position_missing",
                "Strategy FIFO has exposure absent from the broker account",
                retryable=False,
            )
        if strategy_quantity.is_signed() != broker_quantity.is_signed():
            return execution_error(
                "position_direction_mismatch",
                "Strategy FIFO and broker exposure have opposite directions",
                retryable=False,
            )
        if strategy_quantity < 0 or broker_quantity < 0:
            return execution_error(
                "long_only_exit_short_exposure",
                "Long-only portfolio exit found short strategy or broker exposure",
                retryable=False,
            )
        exit_signal = self._ordinary_exit_signal(signal)
        override = await self._adapter.build_target_override(
            command,
            exit_signal,
            plan_leg_id=leg.plan_leg_id,
            target_allocation=Decimal("0"),
            account=account,
            strategy_quantity=strategy_quantity,
            broker_quantity=broker_quantity,
        )
        override = self._freeze_override_target(command, lease, leg, override)
        if override.delta_quantity >= 0:
            return execution_error(
                "full_exit_target_invalid",
                "Full exit did not produce a risk-reducing zero target",
                retryable=False,
            )
        self._mark(lease, leg, "submitted")
        result = await self._adapter.execute(
            command,
            exit_signal,
            target_allocation=Decimal("0"),
            close_quantity_override=None,
            target_position_override=override,
        )
        outcome = self._confirmed_outcome(command, leg, result)
        if outcome != "confirmed":
            return self._record_nonterminal_result(
                lease,
                leg,
                phase="exit",
                outcome=outcome,
                result=result,
            )
        _account, refreshed_fifo = await self._refresh(command, signal)
        remaining = self._strategy_quantity(refreshed_fifo, signal.symbol)
        if abs(remaining) > _POSITION_TOLERANCE:
            return execution_error(
                "post_exit_fifo_unresolved",
                (
                    "Clamped close completed but strategy FIFO remains non-zero; "
                    "blocking replacement entries"
                ),
                retryable=False,
            )
        self._mark(lease, leg, "confirmed")
        self._observe("exit", "confirmed")
        return None

    async def _execute_target_reduction(
        self,
        command: RebalanceExecutionCommandEvent,
        lease: RebalancePlanLease,
        leg: RebalancePlanLegSnapshot,
    ) -> RebalanceExecutionError | None:
        signal = self._target_signal(leg, reduction=True)
        account, fifo = await self._refresh(command, signal)
        override = await self._target_override(
            command,
            lease,
            leg,
            account=account,
            fifo=fifo,
        )
        if override.delta_quantity == 0:
            self._mark(lease, leg, "confirmed")
            return None
        if override.delta_quantity > 0:
            return execution_error(
                "reduction_would_increase_exposure",
                f"Reduction leg for {signal.symbol} would require an entry",
                retryable=False,
            )
        self._mark(lease, leg, "submitted")
        result = await self._adapter.execute(
            command,
            signal,
            target_allocation=leg.allocation_hint,
            close_quantity_override=None,
            target_position_override=override,
        )
        outcome = self._confirmed_outcome(command, leg, result)
        if outcome != "confirmed":
            return self._record_nonterminal_result(
                lease,
                leg,
                phase="reduce",
                outcome=outcome,
                result=result,
            )
        refreshed_account, refreshed_fifo = await self._refresh(command, signal)
        remaining = await self._target_override(
            command,
            lease,
            leg,
            account=refreshed_account,
            fifo=refreshed_fifo,
        )
        if remaining.delta_quantity != 0:
            return execution_error(
                "post_reduction_target_unresolved",
                f"Target for {signal.symbol} remains outside its drift band",
                retryable=True,
            )
        self._mark(lease, leg, "confirmed")
        self._observe("reduce", "confirmed")
        return None

    async def _process_entry_reductions(
        self,
        command: RebalanceExecutionCommandEvent,
        lease: RebalancePlanLease,
        legs: tuple[RebalancePlanLegSnapshot, ...],
        *,
        account: AccountState,
        fifo: Mapping[str, Decimal],
    ) -> RebalanceExecutionError | None:
        """Complete target-driven reductions before any target increase."""
        entry_authorized = bool(
            command.execution_policy.autopilot and command.execution_policy.entries_enabled
        )
        current_account = account
        current_fifo = fifo
        for leg in legs:
            recovery = self._recover(command, lease, leg, phase="entry")
            if recovery is not None:
                if recovery is False:
                    continue
                if recovery == "confirmed":
                    postcondition_error = await self._validate_recovered_confirmation(
                        command,
                        lease,
                        leg,
                    )
                    if postcondition_error is not None:
                        return postcondition_error
                    current_account, current_fifo = await self._refresh(
                        command,
                        self._target_signal(
                            leg,
                            reduction=not (
                                command.execution_policy.autopilot
                                and command.execution_policy.entries_enabled
                            ),
                        ),
                    )
                    continue
                if isinstance(recovery, RebalanceExecutionError):
                    return recovery
                raise AssertionError("Unknown rebalance recovery state")
            override = await self._target_override(
                command,
                lease,
                leg,
                account=current_account,
                fifo=current_fifo,
                freeze_if_missing=entry_authorized,
            )
            if override.delta_quantity >= 0:
                continue
            override = self._freeze_override_target(command, lease, leg, override)
            if not command.execution_policy.exits_enabled:
                return execution_error(
                    "target_reduction_authority_required",
                    "Target decrease requires exits_enabled=true",
                    retryable=False,
                )
            self._mark(lease, leg, "submitted")
            signal = self._target_signal(leg, reduction=True)
            result = await self._adapter.execute(
                command,
                signal,
                target_allocation=leg.allocation_hint,
                close_quantity_override=None,
                target_position_override=override,
            )
            outcome = self._confirmed_outcome(command, leg, result)
            if outcome != "confirmed":
                return self._record_nonterminal_result(
                    lease,
                    leg,
                    phase="reduce",
                    outcome=outcome,
                    result=result,
                )
            current_account, current_fifo = await self._refresh(command, signal)
            remaining = await self._target_override(
                command,
                lease,
                leg,
                account=current_account,
                fifo=current_fifo,
            )
            if remaining.delta_quantity != 0:
                return execution_error(
                    "post_reduction_target_unresolved",
                    f"Target for {signal.symbol} remains outside its drift band",
                    retryable=True,
                )
            self._mark(lease, leg, "confirmed")
        return None

    async def _process_entries(  # noqa: PLR0912
        self,
        command: RebalanceExecutionCommandEvent,
        lease: RebalancePlanLease,
        legs: tuple[RebalancePlanLegSnapshot, ...],
    ) -> bool:
        """Submit independent increases sequentially from refreshed account state."""
        entry_authorized = bool(
            command.execution_policy.autopilot and command.execution_policy.entries_enabled
        )
        degraded = False
        unauthorized_fifo: Mapping[str, Decimal] | None = None
        for leg in legs:
            if leg.status == "confirmed":
                continue
            if not entry_authorized:
                if unauthorized_fifo is None:
                    unauthorized_fifo = await self._adapter.strategy_positions(command)
                self._freeze_quantity(
                    lease,
                    leg,
                    max(
                        self._strategy_quantity(
                            unauthorized_fifo,
                            self._leg_symbol(leg),
                        ),
                        Decimal("0"),
                    ),
                )
                self._mark(
                    lease,
                    leg,
                    "skipped",
                    code="entry_authority_disabled",
                    detail=(
                        "Entry skipped because autopilot or entries_enabled is false; "
                        "risk-reducing legs remain independently authorized"
                    ),
                )
                degraded = True
                continue
            dependency_error = self._dependency_error(command, leg)
            if dependency_error is not None:
                raise dependency_error
            recovery = self._recover(command, lease, leg, phase="entry")
            if recovery is not None:
                if recovery is False:
                    continue
                if recovery == "confirmed":
                    postcondition_error = await self._validate_recovered_confirmation(
                        command,
                        lease,
                        leg,
                    )
                    if postcondition_error is not None:
                        if postcondition_error.retryable:
                            raise postcondition_error
                        degraded = True
                    continue
                if not isinstance(recovery, RebalanceExecutionError):
                    raise AssertionError("Unknown rebalance recovery state")
                if recovery.retryable:
                    raise recovery
                degraded = True
                continue
            signal = self._target_signal(leg, reduction=False)
            account, fifo = await self._refresh(command, signal)
            override = await self._target_override(
                command,
                lease,
                leg,
                account=account,
                fifo=fifo,
            )
            if override.delta_quantity < 0:
                raise execution_error(
                    "late_target_reduction_required",
                    "Target changed to a reduction after the exit-first phase",
                    retryable=True,
                )
            if override.delta_quantity == 0:
                self._mark(lease, leg, "confirmed")
                continue
            self._validate_entry_funding(command, account, (override,))
            self._mark(lease, leg, "submitted")
            result = await self._adapter.execute(
                command,
                signal,
                target_allocation=leg.allocation_hint,
                close_quantity_override=None,
                target_position_override=override,
            )
            outcome = self._confirmed_outcome(command, leg, result)
            if outcome == "confirmed":
                refreshed_account, refreshed_fifo = await self._refresh(command, signal)
                remaining = await self._target_override(
                    command,
                    lease,
                    leg,
                    account=refreshed_account,
                    fifo=refreshed_fifo,
                )
                if remaining.delta_quantity != 0:
                    raise execution_error(
                        "post_entry_target_unresolved",
                        f"Target for {signal.symbol} remains outside its drift band",
                        retryable=True,
                    )
                self._mark(lease, leg, "confirmed")
                self._observe("entry", "confirmed")
                continue
            error = self._record_nonterminal_result(
                lease,
                leg,
                phase="entry",
                outcome=outcome,
                result=result,
            )
            if error.retryable:
                raise error
            degraded = True
        return degraded

    async def _preflight_entry_funding(
        self,
        command: RebalanceExecutionCommandEvent,
        lease: RebalancePlanLease,
        legs: tuple[RebalancePlanLegSnapshot, ...],
    ) -> None:
        """Fund the complete remaining IBKR entry batch before its first BUY."""
        if (
            command.broker_route.broker.strip().lower() != "ibkr"
            or not command.execution_policy.autopilot
            or not command.execution_policy.entries_enabled
        ):
            return
        pending = self._adapter.recoverable_account_orders(command)
        if pending:
            raise execution_error(
                "account_orders_nonterminal",
                "Account has recoverable orders that can change entry funding",
                retryable=True,
            )
        candidates = tuple(leg for leg in legs if leg.status != "confirmed")
        if not candidates:
            return
        account, fifo = await self._refresh(
            command,
            self._target_signal(candidates[0], reduction=False),
        )
        overrides: list[TargetPositionQuantityOverride] = []
        for leg in candidates:
            dependency_error = self._dependency_error(command, leg)
            if dependency_error is not None:
                raise dependency_error
            override = await self._target_override(
                command,
                lease,
                leg,
                account=account,
                fifo=fifo,
            )
            if override.delta_quantity < 0:
                raise execution_error(
                    "late_target_reduction_required",
                    "Target changed to a reduction after the exit-first phase",
                    retryable=True,
                )
            if override.delta_quantity > 0:
                overrides.append(override)
        self._validate_entry_funding(command, account, tuple(overrides))

    def _validate_entry_funding(
        self,
        command: RebalanceExecutionCommandEvent,
        account: AccountState,
        overrides: tuple[TargetPositionQuantityOverride, ...],
    ) -> None:
        if command.broker_route.broker.strip().lower() != "ibkr":
            return
        self._adapter.validate_entry_funding(command, account, overrides)

    async def _freeze_preserved_targets(
        self,
        command: RebalanceExecutionCommandEvent,
        lease: RebalancePlanLease,
        legs: tuple[RebalancePlanLegSnapshot, ...],
    ) -> None:
        preserved = tuple(
            leg
            for leg in legs
            if leg.disposition == "rejected"
            and leg.reason_code in _PRESERVED_REJECTED_INCUMBENT_REASONS
        )
        if not preserved:
            return
        fifo = await self._adapter.strategy_positions(command)
        for leg in preserved:
            self._freeze_quantity(
                lease,
                leg,
                max(
                    self._strategy_quantity(fifo, self._leg_symbol(leg)),
                    Decimal("0"),
                ),
            )

    async def _refresh(
        self,
        command: RebalanceExecutionCommandEvent,
        signal: Signal,
    ) -> tuple[AccountState, Mapping[str, Decimal]]:
        account = await self._adapter.authoritative_account_state(command, signal)
        fifo = await self._adapter.strategy_positions(command)
        return account, fifo

    async def _target_override(
        self,
        command: RebalanceExecutionCommandEvent,
        lease: RebalancePlanLease,
        leg: RebalancePlanLegSnapshot,
        *,
        account: AccountState,
        fifo: Mapping[str, Decimal],
        freeze_if_missing: bool = True,
    ) -> TargetPositionQuantityOverride:
        signal = self._target_signal(leg, reduction=False)
        strategy_quantity = self._strategy_quantity(fifo, signal.symbol)
        broker_quantity = self._broker_quantity(account, signal.symbol)
        if strategy_quantity < -_POSITION_TOLERANCE:
            raise execution_error(
                "target_conflicts_with_short_attribution",
                "Long-only target has strategy-attributed short exposure",
                retryable=False,
            )
        if broker_quantity < -_POSITION_TOLERANCE:
            raise execution_error(
                "target_conflicts_with_broker_short",
                "Long-only target has broker short exposure",
                retryable=False,
            )
        if strategy_quantity - broker_quantity > _POSITION_TOLERANCE:
            raise execution_error(
                "strategy_exposure_exceeds_broker",
                "Strategy target exposure exceeds broker exposure",
                retryable=False,
            )
        override = await self._adapter.build_target_override(
            command,
            signal,
            plan_leg_id=leg.plan_leg_id,
            target_allocation=leg.allocation_hint,
            account=account,
            strategy_quantity=max(strategy_quantity, Decimal("0")),
            broker_quantity=max(broker_quantity, Decimal("0")),
        )
        return self._freeze_override_target(
            command,
            lease,
            leg,
            override,
            freeze_if_missing=freeze_if_missing,
        )

    def _freeze_override_target(
        self,
        command: RebalanceExecutionCommandEvent,
        lease: RebalancePlanLease,
        leg: RebalancePlanLegSnapshot,
        override: TargetPositionQuantityOverride,
        *,
        freeze_if_missing: bool = True,
    ) -> TargetPositionQuantityOverride:
        durable_leg = next(
            (
                candidate
                for candidate in self._store.load_legs(command)
                if candidate.plan_leg_id == leg.plan_leg_id
            ),
            None,
        )
        if durable_leg is None:
            raise execution_error(
                "plan_leg_missing_during_target_freeze",
                f"Plan leg {leg.plan_leg_id} disappeared during target generation",
                retryable=False,
            )
        if durable_leg.frozen_target_quantity is not None:
            return replace(override, target_quantity=durable_leg.frozen_target_quantity)
        if not freeze_if_missing:
            return override
        effective_target = (
            override.strategy_quantity if override.delta_quantity == 0 else override.target_quantity
        )
        frozen = self._freeze_quantity(lease, leg, effective_target)
        return replace(override, target_quantity=frozen)

    def _freeze_quantity(
        self,
        lease: RebalancePlanLease,
        leg: RebalancePlanLegSnapshot,
        quantity: Decimal,
    ) -> Decimal:
        account_lease = self._account_serializer.current_lease()
        self._account_serializer.assert_current(account_lease)
        return self._store.freeze_leg_target(
            lease,
            plan_leg_id=leg.plan_leg_id,
            target_quantity=quantity,
            account_generation=account_lease.generation,
            now=self._now(),
        )

    def _recover(  # noqa: PLR0911
        self,
        command: RebalanceExecutionCommandEvent,
        lease: RebalancePlanLease,
        leg: RebalancePlanLegSnapshot,
        *,
        phase: str,
    ) -> RebalanceExecutionError | Literal[False, "confirmed"] | None:
        if leg.status == "confirmed":
            return False
        if leg.status in {"failed", "rejected", "skipped"}:
            return execution_error(
                f"{phase}_{leg.status}",
                f"Required durable leg is terminal {leg.status}",
                retryable=False,
            )
        state = self._store.persisted_execution_state(
            leg,
            user_id=command.user_id,
            broker_account_id=command.broker_route.broker_account_id,
            strategy_id=command.strategy_id,
        )
        if state == "absent":
            if leg.status == "partial":
                return execution_error(
                    f"{phase}_partial_evidence_missing",
                    "Partial leg lost its durable order/fill evidence",
                    retryable=False,
                )
            return None
        if state == "retryable_pre_submission":
            if leg.status == "partial":
                return execution_error(
                    f"{phase}_partial_evidence_missing",
                    "Partial leg cannot be reclaimed as a pre-submission failure",
                    retryable=False,
                )
            self._observe(phase, "retryable_pre_submission")
            return None
        if state == "confirmed":
            # Durable fill confirmation is necessary but not sufficient. The
            # caller must re-read account/FIFO state and prove the economic
            # target before sealing the leg; a crash may have occurred between
            # the fill and its accounting projection.
            return "confirmed"
        if state in {"pending", "partial", "ambiguous"}:
            recovered_status = (
                "partial" if state == "partial" or leg.status == "partial" else "submitted"
            )
            self._mark(
                lease,
                leg,
                recovered_status,
                code=f"{phase}_{state}",
                detail="Durable order/fill state requires terminal reconciliation",
            )
            return execution_error(
                f"{phase}_{state}",
                "Durable order/fill state is not terminal confirmed",
                retryable=True,
            )
        self._mark(
            lease,
            leg,
            "rejected" if state == "rejected" else "failed",
            code=f"{phase}_{state}",
            detail=f"Durable execution is terminal {state}",
        )
        return execution_error(
            f"{phase}_{state}",
            f"Durable execution is terminal {state}",
            retryable=False,
        )

    async def _validate_recovered_confirmation(
        self,
        command: RebalanceExecutionCommandEvent,
        lease: RebalancePlanLease,
        leg: RebalancePlanLegSnapshot,
    ) -> RebalanceExecutionError | None:
        """Seal a recovered fill only after its economic postcondition holds."""
        signal = self._required_signal(leg)
        if leg.phase == "exit" and leg.action == "exit":
            _account, fifo = await self._refresh(command, signal)
            if abs(self._strategy_quantity(fifo, signal.symbol)) > _POSITION_TOLERANCE:
                return execution_error(
                    "recovered_exit_postcondition_unresolved",
                    "Recovered exit fill has not cleared the strategy FIFO position",
                    retryable=True,
                )
            self._freeze_quantity(lease, leg, Decimal("0"))
        else:
            entry_authorized = bool(
                command.execution_policy.autopilot and command.execution_policy.entries_enabled
            )
            target_signal = self._target_signal(
                leg,
                reduction=not entry_authorized,
            )
            account, fifo = await self._refresh(command, target_signal)
            remaining = await self._target_override(
                command,
                lease,
                leg,
                account=account,
                fifo=fifo,
            )
            if remaining.delta_quantity != 0:
                return execution_error(
                    "recovered_target_postcondition_unresolved",
                    f"Recovered target for {signal.symbol} remains outside its drift band",
                    retryable=True,
                )
        self._mark(lease, leg, "confirmed")
        self._observe("recovery", "confirmed")
        return None

    def _record_nonterminal_result(
        self,
        lease: RebalancePlanLease,
        leg: RebalancePlanLegSnapshot,
        *,
        phase: str,
        outcome: str,
        result: ExecutionResult,
    ) -> RebalanceExecutionError:
        retryable = outcome in {"ambiguous", "partial", "pending", "transient"}
        status = "partial" if outcome == "partial" else "submitted"
        if not retryable:
            status = "rejected"
        code = f"{phase}_{outcome}"
        detail = result.error_message or f"Execution result is {outcome}"
        self._mark(lease, leg, status, code=code, detail=detail)
        return execution_error(code, detail, retryable=retryable)

    def _confirmed_outcome(
        self,
        command: RebalanceExecutionCommandEvent,
        leg: RebalancePlanLegSnapshot,
        result: ExecutionResult,
    ) -> str:
        outcome = self._classify_result(result)
        if outcome != "confirmed":
            return outcome
        durable = self._store.persisted_execution_state(
            leg,
            user_id=command.user_id,
            broker_account_id=command.broker_route.broker_account_id,
            strategy_id=command.strategy_id,
        )
        return durable if durable in {"confirmed", "partial", "pending"} else "ambiguous"

    @staticmethod
    def _classify_result(result: ExecutionResult) -> str:  # noqa: PLR0911
        statuses = {str(item.get("status") or "").strip().lower() for item in result.order_results}
        if result.success and result.orders_submitted > 0:
            if result.orders_filled == result.orders_submitted and statuses <= {"filled"}:
                return "confirmed"
            if result.orders_filled > 0:
                return "partial"
            return "pending"
        if statuses & {"partially_filled"} or result.total_quantity > 0:
            return "partial"
        if statuses & _RECOVERABLE_RESULT_STATUSES:
            return "pending"
        if is_retryable_failure(result):
            return "transient"
        return "ambiguous" if result.success else "rejected"

    def _dependency_error(
        self,
        command: RebalanceExecutionCommandEvent,
        leg: RebalancePlanLegSnapshot,
    ) -> RebalanceExecutionError | None:
        if not leg.depends_on_sequences:
            return None
        current = {
            row.command_sequence: row
            for row in self._store.load_legs(command)
            if row.command_sequence is not None
        }
        unresolved = [
            sequence
            for sequence in leg.depends_on_sequences
            if sequence not in current or current[sequence].status != "confirmed"
        ]
        if not unresolved:
            return None
        return execution_error(
            "entry_dependency_unconfirmed",
            f"Entry dependencies are not terminal confirmed: {unresolved}",
            retryable=True,
        )

    async def _terminal_target_reconciliation(
        self,
        command: RebalanceExecutionCommandEvent,
        _lease: RebalancePlanLease,
        *,
        exit_legs: tuple[RebalancePlanLegSnapshot, ...],
    ) -> str:
        """Prove every frozen strategy target before sealing the plan."""
        account_lease = self._account_serializer.current_lease()
        self._account_serializer.assert_current(account_lease)
        pending_reader = getattr(self._adapter, "recoverable_account_orders", None)
        pending = pending_reader(command) if callable(pending_reader) else {}
        if pending:
            raise execution_error(
                "account_orders_nonterminal",
                "Account has recoverable orders that can change terminal target quantities",
                retryable=True,
            )

        fifo = await self._adapter.strategy_positions(command)
        legs = self._store.load_legs(command)
        self._validate_completed_exits(exit_legs, fifo)

        targets: dict[str, Decimal] = {}
        for leg in legs:
            if not self._models_strategy_position(leg):
                continue
            symbol = normalize_product_symbol(self._leg_symbol(leg))
            if symbol in targets:
                raise execution_error(
                    "duplicate_terminal_target_symbol",
                    f"Frozen plan contains duplicate modeled symbol {symbol}",
                    retryable=False,
                )
            if leg.frozen_target_quantity is None:
                raise execution_error(
                    "terminal_target_unfrozen",
                    f"Plan leg {leg.plan_leg_id} has no frozen terminal quantity",
                    retryable=False,
                )
            target = self._decimal(leg.frozen_target_quantity)
            if target < 0:
                raise execution_error(
                    "terminal_target_negative",
                    f"Long-only terminal target for {symbol} is negative",
                    retryable=False,
                )
            targets[symbol] = target

        actual: dict[str, Decimal] = {}
        for raw_symbol, raw_quantity in fifo.items():
            symbol = normalize_product_symbol(raw_symbol)
            actual[symbol] = actual.get(symbol, Decimal("0")) + self._decimal(raw_quantity)
        outside = sorted(
            symbol
            for symbol, quantity in actual.items()
            if abs(quantity) > _POSITION_TOLERANCE and symbol not in targets
        )
        if outside:
            raise execution_error(
                "strategy_position_outside_plan",
                f"Strategy FIFO positions are outside the frozen plan: {outside}",
                retryable=False,
            )
        mismatches = sorted(
            symbol
            for symbol, target in targets.items()
            if abs(actual.get(symbol, Decimal("0")) - target) > _POSITION_TOLERANCE
        )
        if mismatches:
            raise execution_error(
                "terminal_target_quantity_mismatch",
                f"Strategy FIFO differs from frozen terminal targets: {mismatches}",
                retryable=True,
            )
        self._account_serializer.assert_current(account_lease)
        return canonical_json_hash(
            [
                {"symbol": symbol, "target_quantity": self._canonical_quantity(targets[symbol])}
                for symbol in sorted(targets)
            ]
        )

    @staticmethod
    def _canonical_quantity(value: Decimal) -> str:
        normalized = value.normalize()
        return "0" if normalized == 0 else format(normalized, "f")

    def _complete(
        self,
        lease: RebalancePlanLease,
        *,
        degraded: bool,
        terminal_targets_sha256: str,
    ) -> RebalanceProcessResult:
        account_lease = self._account_serializer.current_lease()
        self._account_serializer.assert_current(account_lease)
        status: Literal["degraded", "completed"] = "degraded" if degraded else "completed"
        self._store.transition(
            lease,
            phase="complete",
            status=status,
            now=self._now(),
            error_code="entry_authority_disabled" if degraded else None,
            error_detail=(
                "One or more entries were not independently authorized or failed"
                if degraded
                else None
            ),
            release_lease=True,
            completion_account_generation=account_lease.generation,
            terminal_targets_sha256=terminal_targets_sha256,
        )
        self._observe("complete", status)
        return RebalanceProcessResult(
            progress=self._store.progress(lease.account_plan_id),
            acquired=True,
            retryable=False,
        )

    def _stop(
        self,
        lease: RebalancePlanLease,
        *,
        phase: str,
        error: RebalanceExecutionError,
    ) -> RebalanceProcessResult:
        terminal = not error.retryable
        self._store.transition(
            lease,
            phase="failed" if terminal else self._phase(phase),
            status="failed" if terminal else "blocked",
            now=self._now(),
            error_code=error.code,
            error_detail=error.detail,
            release_lease=True,
        )
        self._observe(phase, "failed" if terminal else "blocked")
        return RebalanceProcessResult(
            progress=self._store.progress(lease.account_plan_id),
            acquired=True,
            retryable=error.retryable,
        )

    def _unclaimed(
        self,
        account_plan_id: str,
        lease: RebalancePlanLease,
    ) -> RebalanceProcessResult:
        retryable = lease.blocked_reason in {
            "lease_held",
            "not_yet_actionable",
            "older_plan_unresolved",
        }
        self._observe("claim", lease.terminal_status or lease.blocked_reason or "duplicate")
        return RebalanceProcessResult(
            progress=self._store.progress(account_plan_id),
            acquired=False,
            retryable=retryable,
        )

    def _skip_rejected(
        self,
        lease: RebalancePlanLease,
        legs: tuple[RebalancePlanLegSnapshot, ...],
    ) -> None:
        for leg in legs:
            if leg.disposition == "rejected" and not leg.is_terminal:
                self._mark(lease, leg, "skipped")

    def _mark(
        self,
        lease: RebalancePlanLease,
        leg: RebalancePlanLegSnapshot,
        status: str,
        *,
        code: str | None = None,
        detail: str | None = None,
    ) -> None:
        self._store.mark_leg(
            lease,
            plan_leg_id=leg.plan_leg_id,
            status=status,  # type: ignore[arg-type]
            now=self._now(),
            error_code=code,
            error_detail=detail,
        )

    @staticmethod
    def _validate_paper_command(command: RebalanceExecutionCommandEvent) -> None:
        route = command.broker_route
        if command.data_use_scope != "paper_forward":
            raise execution_error(
                "rebalance_data_scope_forbidden",
                "Only paper_forward plans may reach rebalance execution",
                retryable=False,
            )
        if (
            route.broker_environment != "paper"
            or route.broker.strip().lower() not in {"paper", "ibkr"}
            or route.live_enabled
            or not route.sandbox
        ):
            raise execution_error(
                "live_rebalance_disabled",
                "Rebalance execution is restricted to sandboxed paper or IBKR paper routes",
                retryable=False,
            )

    @staticmethod
    def _validate_legs(
        command: RebalanceExecutionCommandEvent,
        legs: tuple[RebalancePlanLegSnapshot, ...],
    ) -> None:
        if len(legs) != command.expected_leg_count:
            raise execution_error(
                "plan_leg_count_mismatch",
                "Durable selected-leg count changed after command creation",
                retryable=False,
            )
        sequences = [leg.command_sequence for leg in legs]
        if sequences != list(range(command.expected_leg_count)):
            raise execution_error(
                "plan_leg_order_mismatch",
                "Durable selected-leg order changed after command creation",
                retryable=False,
            )

    @staticmethod
    def _required_signal(leg: RebalancePlanLegSnapshot) -> Signal:
        if leg.signal is None:
            raise execution_error(
                "plan_signal_missing",
                f"Selected plan leg {leg.plan_leg_id} has no canonical signal",
                retryable=False,
            )
        return leg.signal

    @classmethod
    def _target_signal(
        cls,
        leg: RebalancePlanLegSnapshot,
        *,
        reduction: bool,
    ) -> Signal:
        signal = cls._required_signal(leg)
        if signal.action not in {SignalAction.LONG, SignalAction.HOLD, SignalAction.CLOSE}:
            raise execution_error(
                "target_signal_action_invalid",
                f"Target leg {leg.plan_leg_id} action is {signal.action.value}",
                retryable=False,
            )
        metadata = {
            **signal.metadata,
            "portfolio_rebalance_original_action": signal.action.value,
            "portfolio_rebalance_target_action": "reduce" if reduction else "increase",
        }
        if reduction:
            metadata[ORDINARY_REBALANCE_EXIT_METADATA_KEY] = True
        return replace(
            signal,
            action=SignalAction.CLOSE if reduction else SignalAction.LONG,
            metadata=metadata,
        )

    @staticmethod
    def _ordinary_exit_signal(signal: Signal) -> Signal:
        return replace(
            signal,
            action=SignalAction.CLOSE,
            metadata={
                **signal.metadata,
                ORDINARY_REBALANCE_EXIT_METADATA_KEY: True,
            },
        )

    @classmethod
    def _validate_completed_exits(
        cls,
        legs: tuple[RebalancePlanLegSnapshot, ...],
        positions: Mapping[str, Decimal],
    ) -> None:
        unresolved = [
            leg.signal.symbol
            for leg in legs
            if leg.phase == "exit"
            and leg.signal is not None
            and abs(cls._strategy_quantity(positions, leg.signal.symbol)) > _POSITION_TOLERANCE
        ]
        if unresolved:
            raise execution_error(
                "post_exit_fifo_unresolved",
                f"Strategy FIFO remains after confirmed exits: {', '.join(unresolved)}",
                retryable=True,
            )

    @classmethod
    def _validate_positions_covered(
        cls,
        legs: tuple[RebalancePlanLegSnapshot, ...],
        positions: Mapping[str, Decimal],
    ) -> None:
        covered = {
            normalize_product_symbol(cls._leg_symbol(leg))
            for leg in legs
            if cls._models_strategy_position(leg)
        }
        outside = sorted(
            normalize_product_symbol(symbol)
            for symbol, quantity in positions.items()
            if abs(cls._decimal(quantity)) > _POSITION_TOLERANCE
            and normalize_product_symbol(symbol) not in covered
        )
        if outside:
            raise execution_error(
                "strategy_position_outside_plan",
                f"Strategy FIFO positions are outside the frozen plan: {outside}",
                retryable=False,
            )

    @staticmethod
    def _models_strategy_position(leg: RebalancePlanLegSnapshot) -> bool:
        """Return whether a frozen disposition accounts for incumbent FIFO exposure.

        Selected legs always model their symbol. A rejected leg counts only for
        the scoring contract's two explicit non-executable HOLD-preservation
        dispositions. Rejected new entries, failed thresholds, and unauthorized
        exits must not hide strategy exposure omitted from executable coverage.
        """

        return bool(
            leg.disposition == "selected"
            or (
                leg.disposition == "rejected"
                and leg.phase == "entry"
                and leg.action == "reject"
                and not leg.required
                and leg.command_sequence is None
                and leg.reason_code in _PRESERVED_REJECTED_INCUMBENT_REASONS
                and leg.signal is not None
                and leg.signal.action is SignalAction.HOLD
            )
        )

    @staticmethod
    def _leg_symbol(leg: RebalancePlanLegSnapshot) -> str:
        symbol = str(leg.symbol or (leg.signal.symbol if leg.signal is not None else "")).strip()
        if not symbol:
            raise execution_error(
                "plan_leg_symbol_missing",
                f"Plan leg {leg.plan_leg_id} has no canonical symbol",
                retryable=False,
            )
        return symbol

    @staticmethod
    def _strategy_quantity(
        positions: Mapping[str, Decimal],
        symbol: str,
    ) -> Decimal:
        target = normalize_product_symbol(symbol)
        matches = [
            PortfolioRebalanceOrchestrator._decimal(quantity)
            for position_symbol, quantity in positions.items()
            if normalize_product_symbol(position_symbol) == target
        ]
        if len(matches) > 1:
            raise execution_error(
                "strategy_fifo_ambiguous",
                f"Strategy FIFO returned duplicate normalized symbol {symbol}",
                retryable=False,
            )
        return matches[0] if matches else Decimal("0")

    @staticmethod
    def _broker_quantity(account: AccountState, symbol: str) -> Decimal:
        target = normalize_product_symbol(symbol)
        matches: list[Decimal] = []
        for position in account.positions:
            if normalize_product_symbol(str(position.get("symbol") or "")) != target:
                continue
            try:
                side = normalize_position_side(position.get("side"))
                quantity = abs(
                    PortfolioRebalanceOrchestrator._decimal(
                        position.get("qty", position.get("quantity", 0))
                    )
                )
            except (TypeError, ValueError) as exc:
                raise execution_error(
                    "broker_position_invalid",
                    str(exc),
                    retryable=False,
                ) from exc
            matches.append(-quantity if side == "short" else quantity)
        if len(matches) > 1:
            raise execution_error(
                "broker_position_ambiguous",
                f"Broker returned duplicate normalized position {symbol}",
                retryable=False,
            )
        return matches[0] if matches else Decimal("0")

    @staticmethod
    def _decimal(value: object) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            msg = f"Position quantity is invalid: {value!r}"
            raise ValueError(msg) from exc
        if not parsed.is_finite():
            msg = f"Position quantity is non-finite: {value!r}"
            raise ValueError(msg)
        return parsed

    @staticmethod
    def _phase(value: str) -> PlanPhase:
        if value in {"exit", "account_refresh", "entry"}:
            return value  # type: ignore[return-value]
        return "failed"

    @staticmethod
    def _operational_now(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(tz=UTC)
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "Rebalance operational now must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=UTC)

    @staticmethod
    def _observe(phase: str, outcome: str) -> None:
        if _REBALANCE_TRANSITIONS is not None:
            _REBALANCE_TRANSITIONS.labels(phase=phase, outcome=outcome).inc()


__all__ = ["PortfolioRebalanceOrchestrator"]
