"""Durable fenced progress and exact lineage for account rebalance execution."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import and_, exists, or_, text
from sqlalchemy.orm import Session

from lib_application.db.models import (
    AccountRebalancePlan,
    AccountRebalancePlanLeg,
    AccountRebalancePlanResolution,
    ApiAuditLog,
    CanonicalSignal,
    Execution,
    ExecutionDecisionLog,
    Instrument,
    ModelRebalanceLeg,
    Order,
    OrderIntent,
    PendingOrder,
    StrategyVersion,
)
from lib_common.hashing import canonical_json_bytes, canonical_json_hash
from lib_common.internal_events import (
    CanonicalSignalSnapshot,
    RebalanceExecutionCommandEvent,
)
from lib_strategy.signals.normalization import normalize_signal_action
from lib_strategy.signals.signal import Signal

_DEFAULT_LEASE_DURATION = timedelta(seconds=90)
_TERMINAL_PLAN_STATUSES = frozenset({"cancelled", "completed", "degraded", "expired", "failed"})
_TERMINAL_LEG_STATUSES = frozenset({"confirmed", "failed", "rejected", "skipped"})
_RECOVERABLE_ORDER_STATUSES = frozenset(
    {"partially_filled", "pending", "submission_unknown", "submitted", "working"}
)
_FAILURE_RESOLUTION_TYPES = frozenset({"acknowledged", "reconciled", "remediated"})
_MAX_RESOLUTION_REASON_LENGTH = 2_000
_MAX_RESOLUTION_EVIDENCE_BYTES = 16_384
_MAX_RESOLUTION_OPERATOR_LENGTH = 100
_SHA256_HEX_LENGTH = 64

PlanLegStatus = Literal[
    "pending",
    "submitted",
    "partial",
    "confirmed",
    "rejected",
    "failed",
    "skipped",
]
PlanPhase = Literal["exit", "account_refresh", "entry", "complete", "failed", "expired"]
PlanStatus = Literal[
    "pending",
    "running",
    "blocked",
    "completed",
    "degraded",
    "failed",
    "expired",
    "cancelled",
]
PersistedExecutionState = Literal[
    "absent",
    "retryable_pre_submission",
    "pending",
    "partial",
    "confirmed",
    "rejected",
    "failed",
    "ambiguous",
]
FailureResolutionType = Literal["acknowledged", "reconciled", "remediated"]


class RebalancePlanContractError(ValueError):
    """An incoming command differs from immutable persisted plan content."""


class RebalanceLeaseLostError(RuntimeError):
    """The caller no longer owns the current fencing generation."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _operational_utc(value: datetime, *, field_name: str = "now") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"Rebalance operational {field_name} must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class RebalancePlanLease:
    """One claim and its monotonic fencing generation."""

    account_plan_id: str
    owner: str
    generation: int
    acquired: bool
    terminal_status: str | None = None
    blocked_reason: str | None = None


@dataclass(frozen=True)
class RebalancePlanLegSnapshot:
    """Execution view of one durable plan leg and exact canonical signal."""

    plan_leg_id: str
    model_signal_snapshot_sha256: str
    model_sequence: int
    command_sequence: int | None
    phase: str
    action: str
    disposition: str
    status: str
    instrument_id: int
    allocation_hint: Decimal
    required: bool
    depends_on_sequences: tuple[int, ...]
    reason_code: str
    canonical_signal_id: int | None
    signal: Signal | None
    symbol: str = ""
    frozen_target_quantity: Decimal | None = None
    target_account_generation: int | None = None
    target_frozen_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_LEG_STATUSES


@dataclass(frozen=True)
class RebalanceReadinessSnapshot:
    """Actionable plan health without high-cardinality metric labels."""

    healthy: bool
    stale_count: int
    failed_count: int
    degraded_count: int
    affected_partitions: tuple[str, ...]
    resolved_failed_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "healthy": self.healthy,
            "stale_count": self.stale_count,
            "failed_count": self.failed_count,
            "degraded_count": self.degraded_count,
            "resolved_failed_count": self.resolved_failed_count,
            "affected_partitions": list(self.affected_partitions),
        }


@dataclass(frozen=True)
class RebalanceFailureResolutionSnapshot:
    """Immutable operator disposition for one terminal failed plan."""

    resolution_id: str
    account_plan_id: str
    user_id: str
    broker_account_id: int
    plan_status: str
    failure_code: str | None
    resolution_type: str
    reason: str
    evidence: dict[str, object]
    resolved_by: str
    resolved_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "resolution_id": self.resolution_id,
            "account_plan_id": self.account_plan_id,
            "user_id": self.user_id,
            "broker_account_id": self.broker_account_id,
            "plan_status": self.plan_status,
            "failure_code": self.failure_code,
            "resolution_type": self.resolution_type,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at.isoformat(),
        }


@dataclass(frozen=True)
class RebalanceFailureResolutionResult:
    """Idempotent failed-plan resolution result."""

    resolution: RebalanceFailureResolutionSnapshot
    replayed: bool

    def to_dict(self) -> dict[str, object]:
        return {**self.resolution.to_dict(), "replayed": self.replayed}


@dataclass(frozen=True)
class RebalancePlanProgressSnapshot:
    """Auditable progress summary for an account-scoped plan."""

    account_plan_id: str
    model_rebalance_id: str
    user_id: str
    broker_account_id: int
    strategy_id: str
    phase: str
    status: str
    completed_legs: int
    pending_legs: int
    last_error_code: str | None
    last_error_detail: str | None
    last_progress_at: datetime | None
    progress_deadline_at: datetime
    expires_at: datetime
    completion_account_generation: int | None = None
    terminal_targets_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "account_plan_id": self.account_plan_id,
            "model_rebalance_id": self.model_rebalance_id,
            "user_id": self.user_id,
            "broker_account_id": self.broker_account_id,
            "strategy_id": self.strategy_id,
            "phase": self.phase,
            "status": self.status,
            "completed_legs": self.completed_legs,
            "pending_legs": self.pending_legs,
            "last_error_code": self.last_error_code,
            "last_error_detail": self.last_error_detail,
            "last_progress_at": (
                self.last_progress_at.isoformat() if self.last_progress_at else None
            ),
            "progress_deadline_at": self.progress_deadline_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "completion_account_generation": self.completion_account_generation,
            "terminal_targets_sha256": self.terminal_targets_sha256,
        }


class RebalancePlanStore:
    """SQLAlchemy persistence owner for fenced rebalance progress."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]],
        *,
        lease_duration: timedelta = _DEFAULT_LEASE_DURATION,
    ) -> None:
        if lease_duration <= timedelta(0):
            msg = "Rebalance lease duration must be positive"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._lease_duration = lease_duration

    def claim(  # noqa: PLR0911
        self,
        command: RebalanceExecutionCommandEvent,
        *,
        owner: str,
        now: datetime,
    ) -> RebalancePlanLease:
        """Claim one exact plan while fencing workers and older generations."""
        current = _operational_utc(now)
        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            msg = "Rebalance lease owner must not be empty"
            raise ValueError(msg)
        with self._session_factory() as session:
            plan = self._locked_plan(session, command.account_plan_id)
            self._lock_partition(session, plan)
            self._validate_command(session, plan=plan, command=command)
            terminal_status = (
                str(plan.status) if str(plan.status) in _TERMINAL_PLAN_STATUSES else None
            )
            if terminal_status is not None:
                return self._lease_result(
                    plan,
                    owner=normalized_owner,
                    acquired=False,
                    terminal_status=terminal_status,
                )
            if current < _as_utc(plan.execute_not_before):
                return self._lease_result(
                    plan,
                    owner=normalized_owner,
                    acquired=False,
                    blocked_reason="not_yet_actionable",
                )
            if current >= _as_utc(plan.expires_at):
                self._expire_locked_plan(session, plan=plan, now=current)
                session.commit()
                return self._lease_result(
                    plan,
                    owner=normalized_owner,
                    acquired=False,
                    terminal_status=str(plan.status),
                )
            if current >= _as_utc(plan.progress_deadline_at):
                self._fail_deadline_locked_plan(session, plan=plan, now=current)
                session.commit()
                return self._lease_result(
                    plan,
                    owner=normalized_owner,
                    acquired=False,
                    terminal_status="failed",
                )
            older = self._older_unresolved_plan(session, plan=plan)
            while (
                older is not None
                and str(older.status) not in _TERMINAL_PLAN_STATUSES
                and (
                    current >= _as_utc(older.expires_at)
                    or current >= _as_utc(older.progress_deadline_at)
                )
            ):
                if current >= _as_utc(older.expires_at):
                    self._expire_locked_plan(session, plan=older, now=current)
                else:
                    self._fail_deadline_locked_plan(session, plan=older, now=current)
                session.flush()
                older = self._older_unresolved_plan(session, plan=plan)
            if older is not None:
                plan.status = "blocked"
                plan.last_error_code = "older_plan_unresolved"
                plan.last_error_detail = (
                    f"Older plan {older.account_plan_id} remains {older.status}/{older.phase}"
                )
                plan.updated_at = current
                session.commit()
                return self._lease_result(
                    plan,
                    owner=normalized_owner,
                    acquired=False,
                    blocked_reason="older_plan_unresolved",
                )
            lease_expires = _as_utc(plan.lease_expires_at) if plan.lease_expires_at else None
            if (
                plan.lease_owner
                and str(plan.lease_owner) != normalized_owner
                and lease_expires is not None
                and lease_expires > current
            ):
                return self._lease_result(
                    plan,
                    owner=normalized_owner,
                    acquired=False,
                    blocked_reason="lease_held",
                )
            plan.fence_generation = int(plan.fence_generation) + 1
            plan.lease_owner = normalized_owner
            plan.lease_expires_at = current + self._lease_duration
            plan.status = "running"
            plan.last_progress_at = current
            plan.last_error_code = None
            plan.last_error_detail = None
            plan.updated_at = current
            generation = int(plan.fence_generation)
            self._append_audit(
                session,
                plan=plan,
                action="rebalance.plan.claim",
                request={"account_plan_id": plan.account_plan_id},
                response={"fence_generation": generation, "status": "running"},
            )
            session.commit()
        return RebalancePlanLease(
            account_plan_id=command.account_plan_id,
            owner=normalized_owner,
            generation=generation,
            acquired=True,
        )

    def load_legs(
        self,
        command: RebalanceExecutionCommandEvent,
    ) -> tuple[RebalancePlanLegSnapshot, ...]:
        """Load all target dispositions and exact canonical signal lineage."""
        with self._session_factory() as session:
            plan = session.get(AccountRebalancePlan, command.account_plan_id)
            if plan is None:
                msg = f"Account rebalance plan {command.account_plan_id} does not exist"
                raise RebalancePlanContractError(msg)
            self._validate_command(session, plan=plan, command=command)
            rows = (
                session.query(
                    AccountRebalancePlanLeg,
                    ModelRebalanceLeg,
                    CanonicalSignal,
                    Instrument,
                    StrategyVersion,
                )
                .join(
                    ModelRebalanceLeg,
                    and_(
                        ModelRebalanceLeg.rebalance_id
                        == AccountRebalancePlanLeg.model_rebalance_id,
                        ModelRebalanceLeg.sequence == AccountRebalancePlanLeg.model_sequence,
                        ModelRebalanceLeg.leg_id == AccountRebalancePlanLeg.model_leg_id,
                        ModelRebalanceLeg.instr_id == AccountRebalancePlanLeg.instr_id,
                        ModelRebalanceLeg.external_signal_id
                        == AccountRebalancePlanLeg.external_signal_id,
                        ModelRebalanceLeg.signal_snapshot_sha256
                        == AccountRebalancePlanLeg.model_signal_snapshot_sha256,
                    ),
                )
                .outerjoin(
                    CanonicalSignal,
                    CanonicalSignal.external_signal_id
                    == AccountRebalancePlanLeg.external_signal_id,
                )
                .outerjoin(Instrument, Instrument.instr_id == AccountRebalancePlanLeg.instr_id)
                .outerjoin(
                    StrategyVersion,
                    StrategyVersion.strat_ver_id == CanonicalSignal.strat_ver_id,
                )
                .filter(
                    AccountRebalancePlanLeg.account_plan_id == command.account_plan_id,
                    AccountRebalancePlanLeg.user_id == command.user_id,
                    AccountRebalancePlanLeg.broker_account_id
                    == command.broker_route.broker_account_id,
                )
                .order_by(
                    AccountRebalancePlanLeg.command_sequence.asc().nullslast(),
                    AccountRebalancePlanLeg.model_sequence,
                )
                .all()
            )
            return tuple(
                self._leg_snapshot(
                    plan_leg=plan_leg,
                    model_leg=model_leg,
                    canonical=canonical,
                    instrument=instrument,
                    version=version,
                )
                for plan_leg, model_leg, canonical, instrument, version in rows
            )

    def renew(self, lease: RebalancePlanLease, *, now: datetime) -> None:
        """Extend only the current fenced generation."""
        current = _operational_utc(now)
        with self._session_factory() as session:
            plan = self._require_lease(session, lease=lease, now=current)
            plan.lease_expires_at = current + self._lease_duration
            plan.updated_at = current
            session.commit()

    def mark_leg(
        self,
        lease: RebalancePlanLease,
        *,
        plan_leg_id: str,
        status: PlanLegStatus,
        now: datetime,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Persist one leg transition inside the current fencing generation."""
        current = _operational_utc(now)
        with self._session_factory() as session:
            plan = self._require_lease(session, lease=lease, now=current)
            leg = (
                session.query(AccountRebalancePlanLeg)
                .filter(
                    AccountRebalancePlanLeg.account_plan_id == lease.account_plan_id,
                    AccountRebalancePlanLeg.plan_leg_id == plan_leg_id,
                    AccountRebalancePlanLeg.user_id == plan.user_id,
                    AccountRebalancePlanLeg.broker_account_id == plan.broker_account_id,
                )
                .with_for_update()
                .one_or_none()
            )
            if leg is None:
                msg = f"Plan leg {plan_leg_id} is outside the fenced plan"
                raise RebalancePlanContractError(msg)
            previous_status = str(leg.status)
            leg.status = status
            leg.last_progress_at = current
            leg.last_error_code = error_code
            plan.last_progress_at = current
            plan.last_error_code = error_code
            plan.last_error_detail = error_detail
            plan.lease_expires_at = current + self._lease_duration
            plan.updated_at = current
            self._append_audit(
                session,
                plan=plan,
                action="rebalance.leg.transition",
                request={
                    "account_plan_id": plan.account_plan_id,
                    "plan_leg_id": plan_leg_id,
                    "from_status": previous_status,
                },
                response={
                    "to_status": status,
                    "error_code": error_code,
                    "fence_generation": lease.generation,
                },
            )
            session.commit()

    def transition(
        self,
        lease: RebalancePlanLease,
        *,
        phase: PlanPhase,
        status: PlanStatus,
        now: datetime,
        error_code: str | None = None,
        error_detail: str | None = None,
        release_lease: bool = False,
        completion_account_generation: int | None = None,
        terminal_targets_sha256: str | None = None,
    ) -> None:
        """Move the fenced plan phase/status without changing immutable content."""
        current = _operational_utc(now)
        with self._session_factory() as session:
            plan = self._require_lease(session, lease=lease, now=current)
            previous_phase = str(plan.phase)
            previous_status = str(plan.status)
            if status in {"failed", "expired", "cancelled"}:
                # The deferred database invariant requires terminal plans to
                # have no open children. Terminal failure is an explicit
                # stop-the-plan operation, so seal every remaining leg in the
                # same fenced transaction before the parent becomes terminal.
                self._fail_open_legs(
                    session,
                    plan=plan,
                    now=current,
                    code=error_code or f"plan_{status}",
                )
            plan.phase = phase
            plan.status = status
            plan.last_progress_at = current
            plan.last_error_code = error_code
            plan.last_error_detail = error_detail
            if status in {"completed", "degraded"}:
                if completion_account_generation is None or completion_account_generation <= 0:
                    message = "Completed rebalance requires a positive account generation"
                    raise RebalancePlanContractError(message)
                if (
                    terminal_targets_sha256 is None
                    or len(terminal_targets_sha256) != _SHA256_HEX_LENGTH
                ):
                    message = "Completed rebalance requires a terminal target digest"
                    raise RebalancePlanContractError(message)
                plan.completion_account_generation = completion_account_generation
                plan.terminal_targets_sha256 = terminal_targets_sha256
            elif completion_account_generation is not None or terminal_targets_sha256 is not None:
                message = "Only completed or degraded plans may seal terminal targets"
                raise RebalancePlanContractError(message)
            plan.updated_at = current
            if release_lease:
                plan.lease_owner = None
                plan.lease_expires_at = None
            else:
                plan.lease_expires_at = current + self._lease_duration
            self._append_audit(
                session,
                plan=plan,
                action="rebalance.plan.transition",
                request={
                    "account_plan_id": plan.account_plan_id,
                    "from_phase": previous_phase,
                    "from_status": previous_status,
                },
                response={
                    "to_phase": phase,
                    "to_status": status,
                    "error_code": error_code,
                    "fence_generation": lease.generation,
                },
            )
            session.commit()

    def freeze_leg_target(
        self,
        lease: RebalancePlanLease,
        *,
        plan_leg_id: str,
        target_quantity: Decimal,
        account_generation: int,
        now: datetime,
    ) -> Decimal:
        """Freeze one effective terminal target exactly once under both fences."""
        current = _operational_utc(now)
        quantity = Decimal(str(target_quantity))
        if not quantity.is_finite() or quantity < 0:
            message = "Terminal target quantity must be finite and nonnegative"
            raise RebalancePlanContractError(message)
        if account_generation <= 0:
            message = "Terminal target requires a positive account generation"
            raise RebalancePlanContractError(message)
        with self._session_factory() as session:
            plan = self._require_lease(session, lease=lease, now=current)
            leg = (
                session.query(AccountRebalancePlanLeg)
                .filter(
                    AccountRebalancePlanLeg.account_plan_id == lease.account_plan_id,
                    AccountRebalancePlanLeg.plan_leg_id == plan_leg_id,
                    AccountRebalancePlanLeg.user_id == plan.user_id,
                    AccountRebalancePlanLeg.broker_account_id == plan.broker_account_id,
                )
                .with_for_update()
                .one_or_none()
            )
            if leg is None:
                message = f"Plan leg {plan_leg_id} is outside the fenced plan"
                raise RebalancePlanContractError(message)
            if leg.frozen_target_quantity is not None:
                frozen = Decimal(str(leg.frozen_target_quantity))
                if frozen != quantity:
                    message = (
                        f"Plan leg {plan_leg_id} terminal target changed "
                        f"from {frozen} to {quantity}"
                    )
                    raise RebalancePlanContractError(message)
                return frozen
            leg.frozen_target_quantity = quantity
            leg.target_account_generation = account_generation
            leg.target_frozen_at = current
            plan.last_progress_at = current
            plan.lease_expires_at = current + self._lease_duration
            plan.updated_at = current
            self._append_audit(
                session,
                plan=plan,
                action="rebalance.leg.target_frozen",
                request={
                    "account_plan_id": plan.account_plan_id,
                    "plan_leg_id": plan_leg_id,
                },
                response={
                    "target_quantity": str(quantity),
                    "account_generation": account_generation,
                    "fence_generation": lease.generation,
                },
            )
            session.commit()
            return quantity

    def persisted_execution_state(  # noqa: PLR0911
        self,
        leg: RebalancePlanLegSnapshot,
        *,
        user_id: str,
        broker_account_id: int,
        strategy_id: str,
    ) -> PersistedExecutionState:
        """Classify durable order/fill truth for retry and restart recovery."""
        if leg.canonical_signal_id is None or leg.signal is None:
            return "absent"
        with self._session_factory() as session:
            decisions = (
                session.query(ExecutionDecisionLog)
                .filter(
                    ExecutionDecisionLog.user_id == user_id,
                    ExecutionDecisionLog.broker_account_id == broker_account_id,
                    ExecutionDecisionLog.canonical_signal_id == leg.canonical_signal_id,
                )
                .all()
            )
            intents = (
                session.query(OrderIntent)
                .filter(
                    OrderIntent.user_id == user_id,
                    OrderIntent.account_id == broker_account_id,
                    OrderIntent.strategy_id == strategy_id,
                    OrderIntent.canonical_signal_id == leg.canonical_signal_id,
                )
                .all()
            )
            intent_ids = tuple(int(row.intent_id) for row in intents)
            orders = (
                session.query(Order)
                .filter(
                    Order.account_id == broker_account_id,
                    Order.intent_id.in_(intent_ids),
                )
                .all()
                if intent_ids
                else []
            )
            order_ids = tuple(int(row.order_id) for row in orders)
            fills = (
                session.query(Execution).filter(Execution.order_id.in_(order_ids)).all()
                if order_ids
                else []
            )
            pending = (
                session.query(PendingOrder)
                .filter(
                    PendingOrder.user_id == user_id,
                    PendingOrder.broker_account_id == broker_account_id,
                    PendingOrder.strategy_id == strategy_id,
                    PendingOrder.signal_id == leg.signal.signal_id,
                )
                .all()
            )
        recoverable = [
            row for row in pending if str(row.status).lower() in _RECOVERABLE_ORDER_STATUSES
        ]
        if recoverable:
            partial = any(
                Decimal(str(row.cumulative_filled_quantity or 0)) > 0 for row in recoverable
            )
            return "partial" if partial else "pending"
        order_states = {str(row.state).lower() for row in orders}
        fill_order_ids = {int(row.order_id) for row in fills}
        if orders and order_states == {"filled"} and fill_order_ids == set(order_ids):
            return "confirmed"
        if fills or "partially_filled" in order_states:
            return "partial"
        if order_states & {"rejected", "canceled"}:
            return "rejected"
        decision_states = {str(row.status).lower() for row in decisions}
        if "failed" in decision_states:
            return "failed"
        if "rejected" in decision_states:
            return "rejected"
        if "executed" in decision_states or orders or intents:
            return "ambiguous"
        # A retryable failure deliberately restores the execution decision to
        # ``pending`` before any order artifact exists. That state is safe to
        # reclaim. ``executing`` is different: a worker may have crossed the
        # broker-I/O boundary before crashing, so it remains reconciliation-
        # only even when no downstream row is visible yet.
        if "executing" in decision_states:
            return "pending"
        if "pending" in decision_states:
            return "retryable_pre_submission"
        return "absent"

    def readiness(self, *, now: datetime) -> RebalanceReadinessSnapshot:
        """Report stale and terminal problem plans for readiness/alerts."""
        current = _operational_utc(now)
        with self._session_factory() as session:
            stale = (
                session.query(AccountRebalancePlan)
                .filter(
                    AccountRebalancePlan.status.notin_(_TERMINAL_PLAN_STATUSES),
                    AccountRebalancePlan.progress_deadline_at < current,
                )
                .all()
            )
            failed = (
                session.query(AccountRebalancePlan)
                .outerjoin(
                    AccountRebalancePlanResolution,
                    and_(
                        AccountRebalancePlanResolution.account_plan_id
                        == AccountRebalancePlan.account_plan_id,
                        AccountRebalancePlanResolution.user_id == AccountRebalancePlan.user_id,
                        AccountRebalancePlanResolution.broker_account_id
                        == AccountRebalancePlan.broker_account_id,
                    ),
                )
                .filter(
                    AccountRebalancePlan.status == "failed",
                    AccountRebalancePlanResolution.resolution_id.is_(None),
                )
                .all()
            )
            resolved_failed_count = (
                session.query(AccountRebalancePlan.account_plan_id)
                .join(
                    AccountRebalancePlanResolution,
                    and_(
                        AccountRebalancePlan.account_plan_id
                        == AccountRebalancePlanResolution.account_plan_id,
                        AccountRebalancePlan.user_id == AccountRebalancePlanResolution.user_id,
                        AccountRebalancePlan.broker_account_id
                        == AccountRebalancePlanResolution.broker_account_id,
                    ),
                )
                .filter(AccountRebalancePlan.status == "failed")
                .distinct()
                .count()
            )
            degraded = (
                session.query(AccountRebalancePlan)
                .filter(AccountRebalancePlan.status == "degraded")
                .all()
            )
        affected = {
            f"{row.user_id}:{row.broker_account_id}:{row.strategy_id}:{row.phase}"
            for row in (*stale, *failed, *degraded)
        }
        return RebalanceReadinessSnapshot(
            healthy=not stale and not failed,
            stale_count=len(stale),
            failed_count=len(failed),
            degraded_count=len(degraded),
            affected_partitions=tuple(sorted(affected)),
            resolved_failed_count=resolved_failed_count,
        )

    def resolve_failed_plan(
        self,
        account_plan_id: str,
        *,
        resolution_type: FailureResolutionType,
        reason: str,
        evidence: dict[str, object],
        resolved_by: str,
        now: datetime,
    ) -> RebalanceFailureResolutionResult:
        """Append one idempotent operator resolution without mutating plan history."""
        current = _operational_utc(now)
        normalized_type = str(resolution_type or "").strip().lower()
        normalized_reason = str(reason or "").strip()
        normalized_operator = str(resolved_by or "").strip()
        normalized_evidence = dict(evidence or {})
        if normalized_type not in _FAILURE_RESOLUTION_TYPES:
            msg = f"Unsupported rebalance failure resolution type {normalized_type!r}"
            raise ValueError(msg)
        if not normalized_reason or len(normalized_reason) > _MAX_RESOLUTION_REASON_LENGTH:
            msg = (
                "Rebalance failure resolution reason must contain 1 to "
                f"{_MAX_RESOLUTION_REASON_LENGTH} characters"
            )
            raise ValueError(msg)
        if not normalized_operator or len(normalized_operator) > _MAX_RESOLUTION_OPERATOR_LENGTH:
            msg = (
                "Rebalance failure resolution operator must contain 1 to "
                f"{_MAX_RESOLUTION_OPERATOR_LENGTH} characters"
            )
            raise ValueError(msg)
        if len(canonical_json_bytes(normalized_evidence)) > _MAX_RESOLUTION_EVIDENCE_BYTES:
            msg = (
                "Rebalance failure resolution evidence exceeds "
                f"{_MAX_RESOLUTION_EVIDENCE_BYTES} canonical JSON bytes"
            )
            raise ValueError(msg)

        with self._session_factory() as session:
            plan = self._locked_plan(session, account_plan_id)
            self._lock_partition(session, plan)
            if str(plan.status) != "failed":
                msg = (
                    f"Account rebalance plan {account_plan_id} has status {plan.status!r}; "
                    "only terminal failed plans may be resolved"
                )
                raise RebalancePlanContractError(msg)
            failure_code = str(plan.last_error_code) if plan.last_error_code else None
            resolution_id = canonical_json_hash(
                {
                    "schema": "account-rebalance-failure-resolution-v1",
                    "account_plan_id": str(plan.account_plan_id),
                    "user_id": str(plan.user_id),
                    "broker_account_id": int(plan.broker_account_id),
                    "plan_status": str(plan.status),
                    "failure_code": failure_code,
                    "resolution_type": normalized_type,
                    "reason": normalized_reason,
                    "evidence": normalized_evidence,
                    "resolved_by": normalized_operator,
                }
            )
            existing = (
                session.query(AccountRebalancePlanResolution)
                .filter(AccountRebalancePlanResolution.resolution_id == resolution_id)
                .one_or_none()
            )
            if existing is not None:
                return RebalanceFailureResolutionResult(
                    resolution=self._resolution_snapshot(existing),
                    replayed=True,
                )

            row = AccountRebalancePlanResolution(
                resolution_id=resolution_id,
                account_plan_id=str(plan.account_plan_id),
                user_id=str(plan.user_id),
                broker_account_id=int(plan.broker_account_id),
                plan_status=str(plan.status),
                failure_code=failure_code,
                resolution_type=normalized_type,
                reason=normalized_reason,
                evidence=normalized_evidence,
                resolved_by=normalized_operator,
                resolved_at=current,
                created_at=current,
            )
            session.add(row)
            self._append_audit(
                session,
                plan=plan,
                action="rebalance.plan.failure_resolved",
                request={
                    "account_plan_id": str(plan.account_plan_id),
                    "resolution_id": resolution_id,
                    "resolution_type": normalized_type,
                    "reason": normalized_reason,
                    "evidence": normalized_evidence,
                    "resolved_by": normalized_operator,
                },
                response={
                    "plan_status": str(plan.status),
                    "failure_code": failure_code,
                    "readiness_failure_resolved": True,
                },
            )
            session.flush()
            snapshot = self._resolution_snapshot(row)
            session.commit()
        return RebalanceFailureResolutionResult(resolution=snapshot, replayed=False)

    def progress(self, account_plan_id: str) -> RebalancePlanProgressSnapshot:
        """Return current progress for one exact plan."""
        with self._session_factory() as session:
            plan = session.get(AccountRebalancePlan, account_plan_id)
            if plan is None:
                msg = f"Account rebalance plan {account_plan_id} does not exist"
                raise RebalancePlanContractError(msg)
            statuses = [
                str(value)
                for (value,) in (
                    session.query(AccountRebalancePlanLeg.status)
                    .filter(
                        AccountRebalancePlanLeg.account_plan_id == account_plan_id,
                        AccountRebalancePlanLeg.user_id == plan.user_id,
                        AccountRebalancePlanLeg.broker_account_id == plan.broker_account_id,
                    )
                    .all()
                )
            ]
            terminal = sum(status in _TERMINAL_LEG_STATUSES for status in statuses)
            return RebalancePlanProgressSnapshot(
                account_plan_id=str(plan.account_plan_id),
                model_rebalance_id=str(plan.model_rebalance_id),
                user_id=str(plan.user_id),
                broker_account_id=int(plan.broker_account_id),
                strategy_id=str(plan.strategy_id),
                phase=str(plan.phase),
                status=str(plan.status),
                completed_legs=terminal,
                pending_legs=len(statuses) - terminal,
                last_error_code=(str(plan.last_error_code) if plan.last_error_code else None),
                last_error_detail=(str(plan.last_error_detail) if plan.last_error_detail else None),
                last_progress_at=(
                    _as_utc(plan.last_progress_at) if plan.last_progress_at else None
                ),
                progress_deadline_at=_as_utc(plan.progress_deadline_at),
                expires_at=_as_utc(plan.expires_at),
                completion_account_generation=(
                    int(plan.completion_account_generation)
                    if plan.completion_account_generation is not None
                    else None
                ),
                terminal_targets_sha256=(
                    str(plan.terminal_targets_sha256) if plan.terminal_targets_sha256 else None
                ),
            )

    def audit(self, account_plan_id: str) -> dict[str, object]:
        """Expose canonical signal, decision, order, and fill lineage."""
        progress = self.progress(account_plan_id)
        with self._session_factory() as session:
            legs = (
                session.query(AccountRebalancePlanLeg)
                .filter(
                    AccountRebalancePlanLeg.account_plan_id == account_plan_id,
                    AccountRebalancePlanLeg.user_id == progress.user_id,
                    AccountRebalancePlanLeg.broker_account_id == progress.broker_account_id,
                )
                .order_by(AccountRebalancePlanLeg.model_sequence)
                .all()
            )
            canonical_by_external = {
                str(row.external_signal_id): row
                for row in (
                    session.query(CanonicalSignal)
                    .filter(
                        CanonicalSignal.external_signal_id.in_(
                            tuple(str(leg.external_signal_id) for leg in legs)
                        )
                    )
                    .all()
                    if legs
                    else []
                )
            }
            signal_ids = tuple(int(row.signal_id) for row in canonical_by_external.values())
            decisions = (
                session.query(ExecutionDecisionLog)
                .filter(
                    ExecutionDecisionLog.user_id == progress.user_id,
                    ExecutionDecisionLog.broker_account_id == progress.broker_account_id,
                    ExecutionDecisionLog.canonical_signal_id.in_(signal_ids),
                )
                .all()
                if signal_ids
                else []
            )
            intents = (
                session.query(OrderIntent)
                .filter(
                    OrderIntent.user_id == progress.user_id,
                    OrderIntent.account_id == progress.broker_account_id,
                    OrderIntent.strategy_id == progress.strategy_id,
                    OrderIntent.canonical_signal_id.in_(signal_ids),
                )
                .all()
                if signal_ids
                else []
            )
            intent_ids = tuple(int(row.intent_id) for row in intents)
            orders = (
                session.query(Order).filter(Order.intent_id.in_(intent_ids)).all()
                if intent_ids
                else []
            )
            order_ids = tuple(int(row.order_id) for row in orders)
            fills = (
                session.query(Execution).filter(Execution.order_id.in_(order_ids)).all()
                if order_ids
                else []
            )
            resolutions = (
                session.query(AccountRebalancePlanResolution)
                .filter(
                    AccountRebalancePlanResolution.account_plan_id == account_plan_id,
                    AccountRebalancePlanResolution.user_id == progress.user_id,
                    AccountRebalancePlanResolution.broker_account_id == progress.broker_account_id,
                )
                .order_by(
                    AccountRebalancePlanResolution.resolved_at,
                    AccountRebalancePlanResolution.resolution_id,
                )
                .all()
            )
            transition_rows = (
                session.query(ApiAuditLog)
                .filter(
                    ApiAuditLog.user_id == progress.user_id,
                    ApiAuditLog.account_id == progress.broker_account_id,
                    ApiAuditLog.action.in_(
                        (
                            "rebalance.plan.claim",
                            "rebalance.plan.transition",
                            "rebalance.leg.transition",
                            "rebalance.leg.target_frozen",
                            "rebalance.plan.failure_resolved",
                        )
                    ),
                )
                .order_by(ApiAuditLog.created_at, ApiAuditLog.audit_id)
                .all()
            )
            transition_rows = [
                row
                for row in transition_rows
                if isinstance(row.req, dict)
                and str(row.req.get("account_plan_id")) == account_plan_id
            ]
        return {
            **progress.to_dict(),
            "legs": [
                {
                    "plan_leg_id": str(leg.plan_leg_id),
                    "model_sequence": int(leg.model_sequence),
                    "command_sequence": leg.command_sequence,
                    "external_signal_id": str(leg.external_signal_id),
                    "canonical_signal_id": (
                        int(canonical_by_external[str(leg.external_signal_id)].signal_id)
                        if str(leg.external_signal_id) in canonical_by_external
                        else None
                    ),
                    "phase": str(leg.phase),
                    "status": str(leg.status),
                    "symbol": str(leg.symbol),
                    "frozen_target_quantity": (
                        str(leg.frozen_target_quantity)
                        if leg.frozen_target_quantity is not None
                        else None
                    ),
                    "target_account_generation": leg.target_account_generation,
                    "target_frozen_at": (
                        _as_utc(leg.target_frozen_at).isoformat() if leg.target_frozen_at else None
                    ),
                }
                for leg in legs
            ],
            "decisions": [
                {
                    "decision_id": int(row.decision_id),
                    "canonical_signal_id": row.canonical_signal_id,
                    "status": str(row.status),
                }
                for row in decisions
            ],
            "order_intents": [
                {
                    "intent_id": int(row.intent_id),
                    "canonical_signal_id": int(row.canonical_signal_id),
                    "status": str(row.status),
                }
                for row in intents
            ],
            "orders": [{"order_id": int(row.order_id), "state": str(row.state)} for row in orders],
            "fills": [
                {
                    "execution_id": int(row.exec_id),
                    "order_id": int(row.order_id),
                    "trade_id": str(row.trade_id),
                }
                for row in fills
            ],
            "failure_resolutions": [
                self._resolution_snapshot(resolution).to_dict() for resolution in resolutions
            ],
            "transitions": [
                {
                    "audit_id": int(row.audit_id),
                    "action": str(row.action),
                    "request": dict(row.req or {}),
                    "response": dict(row.resp or {}),
                    "created_at": _as_utc(row.created_at).isoformat(),
                }
                for row in transition_rows
            ],
        }

    @staticmethod
    def _append_audit(
        session: Session,
        *,
        plan: AccountRebalancePlan,
        action: str,
        request: dict[str, object],
        response: dict[str, object],
    ) -> None:
        session.add(
            ApiAuditLog(
                user_id=str(plan.user_id),
                account_id=int(plan.broker_account_id),
                action=action,
                req=request,
                resp=response,
                status="ok",
            )
        )

    @staticmethod
    def _resolution_snapshot(
        row: AccountRebalancePlanResolution,
    ) -> RebalanceFailureResolutionSnapshot:
        return RebalanceFailureResolutionSnapshot(
            resolution_id=str(row.resolution_id),
            account_plan_id=str(row.account_plan_id),
            user_id=str(row.user_id),
            broker_account_id=int(row.broker_account_id),
            plan_status=str(row.plan_status),
            failure_code=str(row.failure_code) if row.failure_code else None,
            resolution_type=str(row.resolution_type),
            reason=str(row.reason),
            evidence=dict(row.evidence or {}),
            resolved_by=str(row.resolved_by),
            resolved_at=_as_utc(row.resolved_at),
        )

    @staticmethod
    def _lock_partition(session: Session, plan: AccountRebalancePlan) -> None:
        bind = session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"rebalance:{plan.user_id}:{plan.broker_account_id}"},
            )

    @staticmethod
    def _locked_plan(session: Session, account_plan_id: str) -> AccountRebalancePlan:
        plan = (
            session.query(AccountRebalancePlan)
            .filter(AccountRebalancePlan.account_plan_id == account_plan_id)
            .with_for_update()
            .one_or_none()
        )
        if plan is None:
            msg = f"Account rebalance plan {account_plan_id} does not exist"
            raise RebalancePlanContractError(msg)
        return plan

    @staticmethod
    def _validate_command(
        session: Session,
        *,
        plan: AccountRebalancePlan,
        command: RebalanceExecutionCommandEvent,
    ) -> None:
        policy = command.execution_policy.model_dump(mode="json")
        route = command.broker_route.model_dump(mode="json")
        persisted = {
            "account_plan_id": str(plan.account_plan_id),
            "model_rebalance_id": str(plan.model_rebalance_id),
            "user_id": str(plan.user_id),
            "binding_id": int(plan.binding_id),
            "broker_account_id": int(plan.broker_account_id),
            "strategy_id": str(plan.strategy_id),
            "data_use_scope": str(plan.data_use_scope),
            "provider_authority_sha256": str(plan.provider_authority_sha256),
            "decision_cutoff": _as_utc(plan.decision_cutoff),
            "execute_not_before": _as_utc(plan.execute_not_before),
            "execution_session_sha256": str(plan.execution_session_sha256),
            "expires_at": _as_utc(plan.expires_at),
            "execution_policy": plan.execution_policy,
            "broker_route": plan.broker_route,
            "execution_policy_sha256": str(plan.execution_policy_sha256),
            "broker_route_sha256": str(plan.broker_route_sha256),
            "content_sha256": str(plan.content_sha256),
            "execution_leg_count": int(plan.execution_leg_count),
            "intentional_cash_slots": int(plan.intentional_cash_slots),
        }
        submitted = {
            "account_plan_id": command.account_plan_id,
            "model_rebalance_id": command.model_rebalance_id,
            "user_id": command.user_id,
            "binding_id": command.binding_id,
            "broker_account_id": command.broker_route.broker_account_id,
            "strategy_id": command.strategy_id,
            "data_use_scope": command.data_use_scope,
            "provider_authority_sha256": command.provider_authority_sha256,
            "decision_cutoff": _as_utc(command.decision_cutoff),
            "execute_not_before": _as_utc(command.execute_not_before),
            "execution_session_sha256": command.execution_session_sha256,
            "expires_at": _as_utc(command.expires_at),
            "execution_policy": policy,
            "broker_route": route,
            "execution_policy_sha256": canonical_json_hash(policy),
            "broker_route_sha256": canonical_json_hash(route),
            "content_sha256": command.content_sha256,
            "execution_leg_count": command.expected_leg_count,
            "intentional_cash_slots": command.intentional_cash_slots,
        }
        if persisted != submitted:
            msg = f"Rebalance command {command.account_plan_id} differs from durable plan"
            raise RebalancePlanContractError(msg)
        rows = (
            session.query(AccountRebalancePlanLeg)
            .filter(
                AccountRebalancePlanLeg.account_plan_id == command.account_plan_id,
                AccountRebalancePlanLeg.user_id == command.user_id,
                AccountRebalancePlanLeg.broker_account_id == command.broker_route.broker_account_id,
                AccountRebalancePlanLeg.command_sequence.is_not(None),
            )
            .order_by(AccountRebalancePlanLeg.command_sequence)
            .all()
        )
        durable_legs_list: list[
            tuple[
                str,
                int,
                str,
                str,
                str,
                str,
                str,
                float | None,
                float,
                bool,
                tuple[int, ...],
            ]
        ] = []
        for row in rows:
            if row.command_sequence is None:
                msg = f"Selected plan leg {row.plan_leg_id} has no command sequence"
                raise RebalancePlanContractError(msg)
            durable_legs_list.append(
                (
                    str(row.plan_leg_id),
                    int(row.command_sequence),
                    str(row.phase),
                    str(row.model_leg_id),
                    str(row.model_signal_snapshot_sha256),
                    str(row.external_signal_id),
                    str(row.symbol),
                    float(row.rank_position) if row.rank_position is not None else None,
                    float(row.allocation_hint),
                    bool(row.required),
                    tuple(int(value) for value in (row.depends_on_sequences or [])),
                )
            )
        durable_legs = tuple(durable_legs_list)
        command_legs = tuple(
            (
                row.plan_leg_id,
                row.sequence,
                row.phase,
                row.model_leg_id,
                row.model_signal_snapshot_sha256,
                row.external_signal_id,
                row.symbol,
                row.rank,
                row.allocation_hint,
                row.required,
                tuple(row.depends_on_sequences),
            )
            for row in command.legs
        )
        if durable_legs != command_legs:
            msg = f"Rebalance command {command.account_plan_id} leg content changed"
            raise RebalancePlanContractError(msg)

    @staticmethod
    def _older_unresolved_plan(
        session: Session,
        *,
        plan: AccountRebalancePlan,
    ) -> AccountRebalancePlan | None:
        resolution_exists = exists().where(
            and_(
                AccountRebalancePlanResolution.account_plan_id
                == AccountRebalancePlan.account_plan_id,
                AccountRebalancePlanResolution.user_id == AccountRebalancePlan.user_id,
                AccountRebalancePlanResolution.broker_account_id
                == AccountRebalancePlan.broker_account_id,
            )
        )
        return (
            session.query(AccountRebalancePlan)
            .filter(
                AccountRebalancePlan.account_plan_id != plan.account_plan_id,
                AccountRebalancePlan.user_id == plan.user_id,
                AccountRebalancePlan.broker_account_id == plan.broker_account_id,
                or_(
                    AccountRebalancePlan.status.notin_(_TERMINAL_PLAN_STATUSES),
                    and_(
                        AccountRebalancePlan.status == "failed",
                        ~resolution_exists,
                    ),
                ),
                or_(
                    AccountRebalancePlan.execute_not_before < plan.execute_not_before,
                    and_(
                        AccountRebalancePlan.execute_not_before == plan.execute_not_before,
                        AccountRebalancePlan.created_at < plan.created_at,
                    ),
                    and_(
                        AccountRebalancePlan.execute_not_before == plan.execute_not_before,
                        AccountRebalancePlan.created_at == plan.created_at,
                        AccountRebalancePlan.account_plan_id < plan.account_plan_id,
                    ),
                ),
            )
            .order_by(
                AccountRebalancePlan.execute_not_before,
                AccountRebalancePlan.created_at,
                AccountRebalancePlan.account_plan_id,
            )
            .with_for_update()
            .first()
        )

    @staticmethod
    def _expire_locked_plan(
        session: Session,
        *,
        plan: AccountRebalancePlan,
        now: datetime,
    ) -> None:
        previous_phase = str(plan.phase)
        previous_status = str(plan.status)
        has_inflight_execution = (
            session.query(AccountRebalancePlanLeg.plan_leg_id)
            .filter(
                AccountRebalancePlanLeg.account_plan_id == plan.account_plan_id,
                AccountRebalancePlanLeg.disposition == "selected",
                AccountRebalancePlanLeg.status.in_({"submitted", "partial"}),
            )
            .first()
            is not None
        )
        terminal_phase = "failed" if has_inflight_execution else "expired"
        terminal_status = "failed" if has_inflight_execution else "expired"
        error_code = (
            "plan_expired_with_inflight_execution" if has_inflight_execution else "plan_expired"
        )
        error_detail = (
            "Plan expired with submitted or partially filled execution requiring "
            "operator resolution"
            if has_inflight_execution
            else "Plan expiry elapsed before terminal completion"
        )
        plan.phase = terminal_phase
        plan.status = terminal_status
        plan.last_progress_at = now
        plan.last_error_code = error_code
        plan.last_error_detail = error_detail
        plan.lease_owner = None
        plan.lease_expires_at = None
        plan.updated_at = now
        RebalancePlanStore._fail_open_legs(
            session,
            plan=plan,
            now=now,
            code=error_code,
        )
        RebalancePlanStore._append_audit(
            session,
            plan=plan,
            action="rebalance.plan.transition",
            request={
                "account_plan_id": plan.account_plan_id,
                "from_phase": previous_phase,
                "from_status": previous_status,
            },
            response={
                "to_phase": terminal_phase,
                "to_status": terminal_status,
                "error_code": error_code,
                "fence_generation": int(plan.fence_generation),
            },
        )

    @staticmethod
    def _fail_deadline_locked_plan(
        session: Session,
        *,
        plan: AccountRebalancePlan,
        now: datetime,
    ) -> None:
        previous_phase = str(plan.phase)
        previous_status = str(plan.status)
        plan.phase = "failed"
        plan.status = "failed"
        plan.last_progress_at = now
        plan.last_error_code = "progress_deadline_elapsed"
        plan.last_error_detail = "Plan progress deadline elapsed before completion"
        plan.lease_owner = None
        plan.lease_expires_at = None
        plan.updated_at = now
        RebalancePlanStore._fail_open_legs(
            session,
            plan=plan,
            now=now,
            code="progress_deadline_elapsed",
        )
        RebalancePlanStore._append_audit(
            session,
            plan=plan,
            action="rebalance.plan.transition",
            request={
                "account_plan_id": plan.account_plan_id,
                "from_phase": previous_phase,
                "from_status": previous_status,
            },
            response={
                "to_phase": "failed",
                "to_status": "failed",
                "error_code": "progress_deadline_elapsed",
                "fence_generation": int(plan.fence_generation),
            },
        )

    @staticmethod
    def _fail_open_legs(
        session: Session,
        *,
        plan: AccountRebalancePlan,
        now: datetime,
        code: str,
    ) -> None:
        (
            session.query(AccountRebalancePlanLeg)
            .filter(
                AccountRebalancePlanLeg.account_plan_id == plan.account_plan_id,
                AccountRebalancePlanLeg.status.notin_(_TERMINAL_LEG_STATUSES),
            )
            .update(
                {
                    AccountRebalancePlanLeg.status: "failed",
                    AccountRebalancePlanLeg.last_progress_at: now,
                    AccountRebalancePlanLeg.last_error_code: code,
                },
                synchronize_session=False,
            )
        )

    @staticmethod
    def _require_lease(
        session: Session,
        *,
        lease: RebalancePlanLease,
        now: datetime,
    ) -> AccountRebalancePlan:
        plan = (
            session.query(AccountRebalancePlan)
            .filter(
                AccountRebalancePlan.account_plan_id == lease.account_plan_id,
                AccountRebalancePlan.lease_owner == lease.owner,
                AccountRebalancePlan.fence_generation == lease.generation,
            )
            .with_for_update()
            .one_or_none()
        )
        if plan is None or plan.lease_expires_at is None or _as_utc(plan.lease_expires_at) <= now:
            msg = (
                f"Rebalance plan {lease.account_plan_id} generation "
                f"{lease.generation} is no longer current"
            )
            raise RebalanceLeaseLostError(msg)
        return plan

    @staticmethod
    def _lease_result(
        plan: AccountRebalancePlan,
        *,
        owner: str,
        acquired: bool,
        terminal_status: str | None = None,
        blocked_reason: str | None = None,
    ) -> RebalancePlanLease:
        return RebalancePlanLease(
            account_plan_id=str(plan.account_plan_id),
            owner=owner,
            generation=int(plan.fence_generation),
            acquired=acquired,
            terminal_status=terminal_status,
            blocked_reason=blocked_reason,
        )

    @staticmethod
    def _leg_snapshot(
        *,
        plan_leg: AccountRebalancePlanLeg,
        model_leg: ModelRebalanceLeg,
        canonical: CanonicalSignal | None,
        instrument: Instrument | None,
        version: StrategyVersion | None,
    ) -> RebalancePlanLegSnapshot:
        signal: Signal | None = None
        canonical_signal_id: int | None = None
        if plan_leg.disposition == "selected":
            if canonical is None or instrument is None or version is None:
                msg = f"Selected plan leg {plan_leg.plan_leg_id} has incomplete signal lineage"
                raise RebalancePlanContractError(msg)
            snapshot_digest = canonical_json_hash(dict(model_leg.signal_snapshot or {}))
            if snapshot_digest != str(model_leg.signal_snapshot_sha256) or snapshot_digest != str(
                plan_leg.model_signal_snapshot_sha256
            ):
                msg = f"Selected plan leg {plan_leg.plan_leg_id} signal snapshot changed"
                raise RebalancePlanContractError(msg)
            try:
                frozen = CanonicalSignalSnapshot.model_validate(model_leg.signal_snapshot)
                frozen_instrument_id = int(frozen.instrument_id or 0)
            except (TypeError, ValueError) as exc:
                msg = f"Selected plan leg {plan_leg.plan_leg_id} signal snapshot is invalid"
                raise RebalancePlanContractError(msg) from exc
            if (
                int(canonical.instr_id) != int(plan_leg.instr_id)
                or frozen_instrument_id != int(plan_leg.instr_id)
                or str(canonical.strategy_id) != str(version.strategy_id)
                or frozen.strategy_id != str(canonical.strategy_id)
                or frozen.strategy_version != str(version.semver)
                or frozen.external_signal_id != str(plan_leg.external_signal_id)
                or str(canonical.external_signal_id) != frozen.external_signal_id
                or normalize_signal_action(str(canonical.action))
                is not normalize_signal_action(frozen.action)
                or _as_utc(canonical.ts) != _as_utc(frozen.timestamp)
                or frozen.symbol != str(instrument.canonical)
                or frozen.asset_class != str(instrument.asset_class)
            ):
                msg = f"Selected plan leg {plan_leg.plan_leg_id} lineage is inconsistent"
                raise RebalancePlanContractError(msg)
            canonical_signal_id = int(canonical.signal_id)
            payload = frozen.model_dump(mode="json")
            payload["signal_id"] = frozen.external_signal_id
            metadata = dict(frozen.metadata)
            metadata.update(
                {
                    "canonical_signal_id": canonical_signal_id,
                    "account_plan_id": str(plan_leg.account_plan_id),
                    "rebalance_plan_leg_id": str(plan_leg.plan_leg_id),
                }
            )
            payload["metadata"] = metadata
            payload["size_hint"] = float(plan_leg.allocation_hint)
            signal = Signal.from_dict(payload)
        return RebalancePlanLegSnapshot(
            plan_leg_id=str(plan_leg.plan_leg_id),
            model_signal_snapshot_sha256=str(plan_leg.model_signal_snapshot_sha256),
            model_sequence=int(plan_leg.model_sequence),
            command_sequence=(
                int(plan_leg.command_sequence) if plan_leg.command_sequence is not None else None
            ),
            phase=str(plan_leg.phase),
            action=str(plan_leg.action),
            disposition=str(plan_leg.disposition),
            status=str(plan_leg.status),
            instrument_id=int(plan_leg.instr_id),
            allocation_hint=Decimal(str(plan_leg.allocation_hint)),
            required=bool(plan_leg.required),
            depends_on_sequences=tuple(
                int(value) for value in (plan_leg.depends_on_sequences or [])
            ),
            reason_code=str(plan_leg.reason_code),
            canonical_signal_id=canonical_signal_id,
            signal=signal,
            symbol=str(plan_leg.symbol),
            frozen_target_quantity=(
                Decimal(str(plan_leg.frozen_target_quantity))
                if plan_leg.frozen_target_quantity is not None
                else None
            ),
            target_account_generation=(
                int(plan_leg.target_account_generation)
                if plan_leg.target_account_generation is not None
                else None
            ),
            target_frozen_at=(
                _as_utc(plan_leg.target_frozen_at) if plan_leg.target_frozen_at else None
            ),
        )


__all__ = [
    "FailureResolutionType",
    "PersistedExecutionState",
    "PlanLegStatus",
    "PlanPhase",
    "PlanStatus",
    "RebalanceFailureResolutionResult",
    "RebalanceFailureResolutionSnapshot",
    "RebalanceLeaseLostError",
    "RebalancePlanContractError",
    "RebalancePlanLease",
    "RebalancePlanLegSnapshot",
    "RebalancePlanProgressSnapshot",
    "RebalancePlanStore",
    "RebalanceReadinessSnapshot",
]
