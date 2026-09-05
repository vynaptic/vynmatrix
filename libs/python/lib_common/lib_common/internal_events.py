"""Versioned internal event contracts for the trading platform."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .asset_classes import normalize_optional_asset_class
from .hashing import canonical_json_hash
from .time_utils import now_utc as _utcnow

EXTERNAL_SIGNAL_ID_MAX_LENGTH = 128
SHA256_PATTERN = r"^[0-9a-f]{64}$"
DataUseScopeValue = Literal[
    "historical_validation",
    "paper_forward",
    "live_forward",
]


class CanonicalSignalSnapshot(BaseModel):
    """Serializable snapshot of a canonical signal."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    signal_id: str
    strategy_id: str
    strategy_type: str
    symbol: str
    action: str
    confidence: float
    timestamp: datetime
    horizon: str | None = None
    expected_return: float | None = None
    predicted_risk: float | None = None
    horizon_days: float | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    size_hint: float | None = None
    asset_class: str | None = None
    sector: str | None = None
    industry: str | None = None
    index: str | None = None
    instrument_id: str | None = None
    strategy_version: str | None = None
    run_id: str | None = None
    source: str | None = None
    external_signal_id: str = Field(
        min_length=1,
        max_length=EXTERNAL_SIGNAL_ID_MAX_LENGTH,
    )
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _normalize_asset_class = field_validator("asset_class", mode="before")(
        normalize_optional_asset_class
    )


class ScoreContextSnapshot(BaseModel):
    """Serializable score context shared across scoring and execution."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    asset_score: float
    sector_score: float | None = None
    market_score: float | None = None
    recommended_mode: str | None = None
    score_scope: str = "asset"
    score_target: str | None = None


class ExecutionPolicySnapshot(BaseModel):
    """Frozen user policy/config used to make an execution decision."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    user_id: str
    strategy_id: str
    binding_id: int | None = None
    autopilot: bool = True
    entries_enabled: bool = True
    exits_enabled: bool = True
    execution_mode: str | None = None

    execution_modes_allowed: list[str] = Field(default_factory=list)
    mode_selection_policy: str | None = None
    recommended_mode: str | None = None
    asset_score_threshold: float | None = None
    sector_score_threshold: float | None = None
    market_score_threshold: float | None = None
    allowed_brokers: list[str] = Field(default_factory=list)
    sizing: dict[str, Any] = Field(default_factory=dict)
    risk_caps: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class BrokerRouteSnapshot(BaseModel):
    """Frozen broker routing inputs or outputs for reproducibility."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    broker: str = Field(min_length=1)
    broker_account_id: int = Field(gt=0)
    broker_environment: Literal["paper", "live"]
    credential_ref: str | None = None
    allowed_brokers: list[str] = Field(default_factory=list)
    route_source: str = "policy"
    live_enabled: bool = False
    sandbox: bool = True
    asset_class: str | None = None
    execution_mode: str | None = None

    _normalize_asset_class = field_validator("asset_class", mode="before")(
        normalize_optional_asset_class
    )


class PlatformEvent(BaseModel):
    """Base shape shared by all internal events."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = "v1"
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    occurred_at: datetime = Field(default_factory=_utcnow)
    run_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    producer: str | None = None
    topic: str
    event_type: str


class CanonicalSignalEvent(PlatformEvent):
    """Signal ingestion event emitted after canonical persistence."""

    topic: Literal["signals.ingested"] = "signals.ingested"
    event_type: Literal["CanonicalSignal"] = "CanonicalSignal"
    signal: CanonicalSignalSnapshot
    canonical_signal_db_id: int | None = None


class ScoredSignalEvent(PlatformEvent):
    """Score computation event emitted after score persistence."""

    topic: Literal["signals.scored"] = "signals.scored"
    event_type: Literal["ScoredSignal"] = "ScoredSignal"
    signal: CanonicalSignalSnapshot
    score_context: ScoreContextSnapshot
    queued_users: list[str] = Field(default_factory=list)


class ExecutionCommandEvent(PlatformEvent):
    """Execution command queued by the scoring engine."""

    topic: Literal["execution.commands"] = "execution.commands"
    event_type: Literal["ExecutionCommand"] = "ExecutionCommand"
    user_id: str
    signal: CanonicalSignalSnapshot
    score_context: ScoreContextSnapshot
    execution_policy: ExecutionPolicySnapshot
    broker_route: BrokerRouteSnapshot
    profile: dict[str, Any] = Field(default_factory=dict)
    user_strategy_config: dict[str, Any] = Field(default_factory=dict)


class ModelRebalanceLegSnapshot(BaseModel):
    """One ordered, account-neutral leg in a model rebalance."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    leg_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    phase: Literal["exit", "reduce", "hold", "entry"]
    signal: CanonicalSignalSnapshot
    rank: float | None = Field(default=None, gt=0)
    allocation_hint: float = Field(ge=0, le=1)
    factor_snapshot_id: str = Field(pattern=SHA256_PATTERN)
    rank_snapshot_id: str = Field(pattern=SHA256_PATTERN)
    current_rank_row_present: bool
    current_rank_factor_snapshot_id: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )
    membership_change_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_lineage(self) -> ModelRebalanceLegSnapshot:
        """Require current rank evidence or a narrowly proven membership exit."""

        current_rank_factor = self.current_rank_factor_snapshot_id
        if current_rank_factor is not None and current_rank_factor != self.factor_snapshot_id:
            message = "current rank evidence must use the exact factor snapshot"
            raise ValueError(message)
        expected_action = {
            "entry": "long",
            "hold": "hold",
            "reduce": "hold",
            "exit": "close",
        }[self.phase]
        normalized_action = self.signal.action.lower()
        if normalized_action == "flat":
            normalized_action = "close"
        if normalized_action != expected_action:
            message = "model leg phase and canonical signal action are inconsistent"
            raise ValueError(message)
        if not self.current_rank_row_present:
            if current_rank_factor is not None:
                message = "a missing current rank row cannot carry current factor linkage"
                raise ValueError(message)
            if self.phase != "exit":
                message = "a missing current rank row is valid only for a full exit"
                raise ValueError(message)
            if self.membership_change_reason != "left_effective_universe":
                message = "a missing current rank row requires left_effective_universe"
                raise ValueError(message)
            if self.rank is not None:
                message = "a missing current rank row cannot claim a current rank"
                raise ValueError(message)
            return self
        if current_rank_factor is None and self.phase != "exit":
            message = "non-exit model legs require current rank-row evidence"
            raise ValueError(message)
        if current_rank_factor is not None:
            return self
        if self.phase != "exit":
            message = "model leg without a current rank row must be a full close"
            raise ValueError(message)
        if self.rank is not None:
            message = "rank-incomplete model exit cannot claim a current rank"
            raise ValueError(message)
        if self.membership_change_reason is None:
            message = "rank-incomplete model exit requires its hard-gate reason"
            raise ValueError(message)
        return self


def _as_aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        message = f"{field_name} must be timezone-aware"
        raise ValueError(message)
    return value.astimezone(UTC)


def _canonical_sha256(payload: dict[str, Any]) -> str:
    return canonical_json_hash(payload)


def compute_model_rebalance_content_sha256(
    *,
    strategy_id: str,
    strategy_version: str,
    effective_session: date,
    decision_cutoff: datetime,
    execute_not_before: datetime,
    execution_session_sha256: str,
    data_use_scope: DataUseScopeValue,
    provider_authority_sha256: str,
    configuration_sha256: str,
    input_snapshot_sha256: str,
    rank_snapshot_sha256: str,
    intentional_cash_slots: int,
    legs: list[ModelRebalanceLegSnapshot],
) -> str:
    """Hash the complete ordered, tenant-neutral model-rebalance content."""

    return _canonical_sha256(
        {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "effective_session": effective_session.isoformat(),
            "decision_cutoff": decision_cutoff.isoformat(),
            "execute_not_before": execute_not_before.isoformat(),
            "execution_session_sha256": execution_session_sha256,
            "data_use_scope": data_use_scope,
            "provider_authority_sha256": provider_authority_sha256,
            "configuration_sha256": configuration_sha256,
            "input_snapshot_sha256": input_snapshot_sha256,
            "rank_snapshot_sha256": rank_snapshot_sha256,
            "intentional_cash_slots": intentional_cash_slots,
            "legs": [leg.model_dump(mode="json") for leg in legs],
        }
    )


def compute_model_rebalance_id(
    *,
    strategy_id: str,
    strategy_version: str,
    effective_session: date,
    execute_not_before: datetime,
    execution_session_sha256: str,
    data_use_scope: DataUseScopeValue,
    provider_authority_sha256: str,
    configuration_sha256: str,
    input_snapshot_sha256: str,
    content_sha256: str,
) -> str:
    """Derive the stable identity used for idempotent model-batch replay."""

    return _canonical_sha256(
        {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "effective_session": effective_session.isoformat(),
            "execute_not_before": execute_not_before.isoformat(),
            "execution_session_sha256": execution_session_sha256,
            "data_use_scope": data_use_scope,
            "provider_authority_sha256": provider_authority_sha256,
            "configuration_sha256": configuration_sha256,
            "input_snapshot_sha256": input_snapshot_sha256,
            "content_sha256": content_sha256,
        }
    )


class ModelRebalanceSubmissionEvent(PlatformEvent):
    """Atomic canonical-signal batch submitted by a portfolio strategy."""

    topic: Literal["signals.rebalances.submit"] = "signals.rebalances.submit"
    event_type: Literal["ModelRebalanceSubmission"] = "ModelRebalanceSubmission"
    rebalance_id: str = Field(pattern=SHA256_PATTERN)
    strategy_id: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    effective_session: date
    decision_cutoff: datetime
    execute_not_before: datetime
    execution_session_sha256: str = Field(pattern=SHA256_PATTERN)
    data_use_scope: DataUseScopeValue
    provider_authority_sha256: str = Field(pattern=SHA256_PATTERN)
    configuration_sha256: str = Field(pattern=SHA256_PATTERN)
    input_snapshot_sha256: str = Field(pattern=SHA256_PATTERN)
    rank_snapshot_sha256: str = Field(pattern=SHA256_PATTERN)
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    expected_leg_count: int = Field(ge=0)
    intentional_cash_slots: int = Field(ge=0)
    legs: list[ModelRebalanceLegSnapshot] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_atomic_identity(self) -> ModelRebalanceSubmissionEvent:
        """Reject partial, reordered, or content-divergent replay payloads."""

        self.decision_cutoff = _as_aware_utc(
            self.decision_cutoff,
            field_name="decision_cutoff",
        )
        self.execute_not_before = _as_aware_utc(
            self.execute_not_before,
            field_name="execute_not_before",
        )
        if self.execute_not_before <= self.decision_cutoff:
            message = "execute_not_before must be later than decision_cutoff"
            raise ValueError(message)

        if len(self.legs) != self.expected_leg_count:
            message = "expected_leg_count does not match model rebalance legs"
            raise ValueError(message)
        sequences = [leg.sequence for leg in self.legs]
        if sequences != list(range(len(self.legs))):
            message = "model rebalance leg sequences must be contiguous and ordered"
            raise ValueError(message)
        if len({leg.leg_id for leg in self.legs}) != len(self.legs):
            message = "model rebalance leg_id values must be unique"
            raise ValueError(message)
        if any(leg.signal.strategy_id != self.strategy_id for leg in self.legs):
            message = "every model rebalance signal must use the event strategy_id"
            raise ValueError(message)
        calculated_content = compute_model_rebalance_content_sha256(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            effective_session=self.effective_session,
            decision_cutoff=self.decision_cutoff,
            execute_not_before=self.execute_not_before,
            execution_session_sha256=self.execution_session_sha256,
            data_use_scope=self.data_use_scope,
            provider_authority_sha256=self.provider_authority_sha256,
            configuration_sha256=self.configuration_sha256,
            input_snapshot_sha256=self.input_snapshot_sha256,
            rank_snapshot_sha256=self.rank_snapshot_sha256,
            intentional_cash_slots=self.intentional_cash_slots,
            legs=self.legs,
        )
        if self.content_sha256 != calculated_content:
            message = "model rebalance content_sha256 does not match ordered content"
            raise ValueError(message)
        calculated_id = compute_model_rebalance_id(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            effective_session=self.effective_session,
            execute_not_before=self.execute_not_before,
            execution_session_sha256=self.execution_session_sha256,
            data_use_scope=self.data_use_scope,
            provider_authority_sha256=self.provider_authority_sha256,
            configuration_sha256=self.configuration_sha256,
            input_snapshot_sha256=self.input_snapshot_sha256,
            content_sha256=self.content_sha256,
        )
        if self.rebalance_id != calculated_id:
            message = "rebalance_id does not match deterministic model identity"
            raise ValueError(message)
        return self


class RebalanceExecutionLegSnapshot(BaseModel):
    """One frozen account-plan leg, still unsized for broker execution."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    plan_leg_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    phase: Literal["exit", "reduce", "entry"]
    model_leg_id: str = Field(min_length=1)
    model_signal_snapshot_sha256: str = Field(pattern=SHA256_PATTERN)
    external_signal_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    rank: float | None = Field(default=None, gt=0)
    allocation_hint: float = Field(ge=0, le=1)
    required: bool = True
    depends_on_sequences: list[int] = Field(default_factory=list)


def compute_account_plan_id(
    *,
    model_rebalance_id: str,
    user_id: str,
    binding_id: int,
    broker_account_id: int,
    data_use_scope: DataUseScopeValue,
    provider_authority_sha256: str,
    execute_not_before: datetime,
    execution_session_sha256: str,
) -> str:
    """Derive one immutable plan identity per model/binding/account boundary."""

    return _canonical_sha256(
        {
            "model_rebalance_id": model_rebalance_id,
            "user_id": user_id,
            "binding_id": binding_id,
            "broker_account_id": broker_account_id,
            "data_use_scope": data_use_scope,
            "provider_authority_sha256": provider_authority_sha256,
            "execute_not_before": execute_not_before.isoformat(),
            "execution_session_sha256": execution_session_sha256,
        }
    )


def compute_rebalance_execution_content_sha256(
    *,
    account_plan_id: str,
    model_rebalance_id: str,
    user_id: str,
    strategy_id: str,
    binding_id: int,
    broker_account_id: int,
    data_use_scope: DataUseScopeValue,
    provider_authority_sha256: str,
    decision_cutoff: datetime,
    execute_not_before: datetime,
    execution_session_sha256: str,
    execution_policy: ExecutionPolicySnapshot,
    broker_route: BrokerRouteSnapshot,
    expires_at: datetime,
    intentional_cash_slots: int,
    legs: list[RebalanceExecutionLegSnapshot],
) -> str:
    """Hash one tenant/account-scoped frozen execution-plan command."""

    return _canonical_sha256(
        {
            "account_plan_id": account_plan_id,
            "model_rebalance_id": model_rebalance_id,
            "user_id": user_id,
            "strategy_id": strategy_id,
            "binding_id": binding_id,
            "broker_account_id": broker_account_id,
            "data_use_scope": data_use_scope,
            "provider_authority_sha256": provider_authority_sha256,
            "decision_cutoff": decision_cutoff.isoformat(),
            "execute_not_before": execute_not_before.isoformat(),
            "execution_session_sha256": execution_session_sha256,
            "execution_policy": execution_policy.model_dump(mode="json"),
            "broker_route": broker_route.model_dump(mode="json"),
            "expires_at": expires_at.isoformat(),
            "intentional_cash_slots": intentional_cash_slots,
            "legs": [leg.model_dump(mode="json") for leg in legs],
        }
    )


class RebalanceExecutionCommandEvent(PlatformEvent):
    """Frozen account-plan command consumed by the rebalance orchestrator."""

    topic: Literal["execution.rebalance.commands"] = "execution.rebalance.commands"
    event_type: Literal["RebalanceExecutionCommand"] = "RebalanceExecutionCommand"
    account_plan_id: str = Field(pattern=SHA256_PATTERN)
    model_rebalance_id: str = Field(pattern=SHA256_PATTERN)
    user_id: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    binding_id: int = Field(gt=0)
    data_use_scope: DataUseScopeValue
    provider_authority_sha256: str = Field(pattern=SHA256_PATTERN)
    decision_cutoff: datetime
    execute_not_before: datetime
    execution_session_sha256: str = Field(pattern=SHA256_PATTERN)
    execution_policy: ExecutionPolicySnapshot
    broker_route: BrokerRouteSnapshot
    expires_at: datetime
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    expected_leg_count: int = Field(ge=0)
    intentional_cash_slots: int = Field(ge=0)
    legs: list[RebalanceExecutionLegSnapshot] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_frozen_plan(self) -> RebalanceExecutionCommandEvent:
        """Validate account ownership, ordering, dependencies, and content."""

        self.decision_cutoff = _as_aware_utc(
            self.decision_cutoff,
            field_name="decision_cutoff",
        )
        self.execute_not_before = _as_aware_utc(
            self.execute_not_before,
            field_name="execute_not_before",
        )
        self.expires_at = _as_aware_utc(self.expires_at, field_name="expires_at")
        if not self.decision_cutoff < self.execute_not_before < self.expires_at:
            message = (
                "account plan timing must satisfy decision_cutoff < execute_not_before < expires_at"
            )
            raise ValueError(message)

        if self.execution_policy.user_id != self.user_id:
            message = "execution policy user does not own the rebalance command"
            raise ValueError(message)
        if self.execution_policy.strategy_id != self.strategy_id:
            message = "execution policy strategy does not match the rebalance command"
            raise ValueError(message)
        if self.execution_policy.binding_id != self.binding_id:
            message = "execution policy binding does not match the rebalance command"
            raise ValueError(message)
        calculated_plan_id = compute_account_plan_id(
            model_rebalance_id=self.model_rebalance_id,
            user_id=self.user_id,
            binding_id=self.binding_id,
            broker_account_id=self.broker_route.broker_account_id,
            data_use_scope=self.data_use_scope,
            provider_authority_sha256=self.provider_authority_sha256,
            execute_not_before=self.execute_not_before,
            execution_session_sha256=self.execution_session_sha256,
        )
        if self.account_plan_id != calculated_plan_id:
            message = "account_plan_id does not match deterministic account identity"
            raise ValueError(message)
        if len(self.legs) != self.expected_leg_count:
            message = "expected_leg_count does not match account-plan legs"
            raise ValueError(message)
        sequences = [leg.sequence for leg in self.legs]
        if sequences != list(range(len(self.legs))):
            message = "account-plan leg sequences must be contiguous and ordered"
            raise ValueError(message)
        if len({leg.plan_leg_id for leg in self.legs}) != len(self.legs):
            message = "account-plan leg identifiers must be unique"
            raise ValueError(message)
        for leg in self.legs:
            invalid_dependency = any(
                dependency >= leg.sequence or dependency < 0
                for dependency in leg.depends_on_sequences
            )
            if invalid_dependency:
                message = "account-plan dependencies must reference earlier non-negative sequences"
                raise ValueError(message)
        calculated = compute_rebalance_execution_content_sha256(
            account_plan_id=self.account_plan_id,
            model_rebalance_id=self.model_rebalance_id,
            user_id=self.user_id,
            strategy_id=self.strategy_id,
            binding_id=self.binding_id,
            broker_account_id=self.broker_route.broker_account_id,
            data_use_scope=self.data_use_scope,
            provider_authority_sha256=self.provider_authority_sha256,
            decision_cutoff=self.decision_cutoff,
            execute_not_before=self.execute_not_before,
            execution_session_sha256=self.execution_session_sha256,
            execution_policy=self.execution_policy,
            broker_route=self.broker_route,
            expires_at=self.expires_at,
            intentional_cash_slots=self.intentional_cash_slots,
            legs=self.legs,
        )
        if self.content_sha256 != calculated:
            message = "rebalance execution content_sha256 does not match ordered content"
            raise ValueError(message)
        return self


class ExecutionResultEvent(PlatformEvent):
    """Execution outcome emitted by the execution engine."""

    topic: Literal["execution.results"] = "execution.results"
    event_type: Literal["ExecutionResult"] = "ExecutionResult"
    user_id: str
    signal_id: str
    strategy_id: str
    symbol: str
    success: bool
    status: str
    execution_mode: str
    broker: str
    broker_account_id: int = Field(gt=0)
    orders_submitted: int
    orders_filled: int
    total_quantity: float
    average_price: float
    total_commission: float
    error_message: str | None = None
    executed_at: datetime
    score_context: ScoreContextSnapshot | None = None
    execution_policy: ExecutionPolicySnapshot | None = None
    broker_route: BrokerRouteSnapshot | None = None
    order_results: list[dict[str, Any]] = Field(default_factory=list)


class FeedbackEvaluationEvent(PlatformEvent):
    """Feedback-loop evaluation emitted after signal-performance persistence."""

    topic: Literal["feedback.ready"] = "feedback.ready"
    event_type: Literal["FeedbackEvaluation"] = "FeedbackEvaluation"
    signal_id: int
    strategy_id: str
    symbol: str
    evaluation_horizon: str
    is_correct: bool
    price_change_pct: float
    pnl_pct: float
    consecutive_wrong_count: int
    needs_optimization: bool
    evaluated_at: datetime


__all__ = [
    "EXTERNAL_SIGNAL_ID_MAX_LENGTH",
    "BrokerRouteSnapshot",
    "CanonicalSignalEvent",
    "CanonicalSignalSnapshot",
    "DataUseScopeValue",
    "ExecutionCommandEvent",
    "ExecutionPolicySnapshot",
    "ExecutionResultEvent",
    "FeedbackEvaluationEvent",
    "ModelRebalanceLegSnapshot",
    "ModelRebalanceSubmissionEvent",
    "PlatformEvent",
    "RebalanceExecutionCommandEvent",
    "RebalanceExecutionLegSnapshot",
    "ScoreContextSnapshot",
    "ScoredSignalEvent",
    "compute_account_plan_id",
    "compute_model_rebalance_content_sha256",
    "compute_model_rebalance_id",
    "compute_rebalance_execution_content_sha256",
]
