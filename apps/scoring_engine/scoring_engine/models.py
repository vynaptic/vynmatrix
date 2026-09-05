from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from lib_common.internal_events import RebalanceExecutionCommandEvent
from lib_common.time_utils import now_utc as _now
from lib_strategy.scoring.types import ExecutionDecision
from lib_strategy.signals.signal import Signal, SignalAction


def action_to_score(action: SignalAction) -> float:
    """Map a signal action to a numeric score contribution."""
    if action == SignalAction.LONG:
        return 1.0
    if action == SignalAction.SHORT:
        return -1.0
    # CLOSE or HOLD treated as neutral
    return 0.0


@dataclass
class SignalRecord:
    """Stored representation of an incoming signal."""

    signal_id: str
    strategy_id: str
    strategy_type: str
    symbol: str
    action: str
    confidence: float
    timestamp: datetime
    expected_return: float | None = None  # mu over horizon
    predicted_risk: float | None = None  # sigma over horizon
    horizon_days: float | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    size_hint: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    sector: str | None = None
    industry: str | None = None  # e.g., "Technology", "Healthcare"
    index: str | None = None  # e.g., "SP500", "NASDAQ100", "NIFTY50"
    asset_class: str | None = None
    instrument_id: str | None = None
    strategy_version: str | None = None
    run_id: str | None = None
    source: str | None = None
    external_signal_id: str | None = None  # Idempotency key for canonical signal dedup
    expires_at: datetime | None = None
    score_value: float = 0.0  # numeric contribution after weighting


@dataclass
class ScoreComponent:
    """Contribution from a single strategy toward a score."""

    strategy_id: str
    weight: float
    raw_value: float
    weighted_value: float
    confidence: float
    timestamp: datetime


@dataclass
class ScoreRecord:
    """Aggregated score for an asset/sector/market."""

    target: str  # e.g., BTCUSD, crypto, market
    scope: str  # asset | sector | market
    score: float
    components: list[ScoreComponent]
    computed_at: datetime = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Stable identity of the originating signal (asset scope only). When set, the
    # asset-score write is idempotent on it so a re-delivered signal updates its
    # row instead of inserting a duplicate (SC-6).
    external_signal_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "scope": self.scope,
            "score": self.score,
            "computed_at": self.computed_at.isoformat(),
            "components": [
                {
                    "strategy_id": c.strategy_id,
                    "weight": c.weight,
                    "raw_value": c.raw_value,
                    "weighted_value": c.weighted_value,
                    "confidence": c.confidence,
                    "timestamp": c.timestamp.isoformat(),
                }
                for c in self.components
            ],
            "metadata": self.metadata,
            "external_signal_id": self.external_signal_id,
        }


@dataclass
class ScoringUserBinding:
    """
    DTO for user binding in the scoring engine layer.

    This is the scoring-engine read/evaluation shape, distinct from
    lib_application.db.models.UserStrategyBinding, the persistence model.

    The DTO uses string IDs and float values for API/serialization convenience.

    All three thresholds use AND logic - all configured thresholds must pass.
    Thresholds are evaluated on absolute score values (long/short symmetric).

    Attributes:
        binding_id: Unique identifier for the binding (for traceability)
        user_id: User identifier (string for API compatibility)
        asset_score_threshold: Minimum asset score to trigger (default 0.6)
        sector_score_threshold: Optional minimum sector score
        market_score_threshold: Optional minimum market score
        asset_filter: Which asset classes apply (empty = all)
        sector_filter: Which sectors apply (empty = all)
        execution_mode: Preferred mode (spot/options/futures/etc)
        sizing_profile: Opaque dict passed to execution for position sizing
        risk_caps: Opaque dict passed to execution for risk limits
        allowed_brokers: List of allowed broker codes (empty = all)
    """

    user_id: str
    binding_id: int | None = None
    strategy_id: str | None = None  # NULL = any strategy
    broker_account_id: int | None = None
    asset_score_threshold: float = 0.6
    asset_filter: list[str] = field(default_factory=list)
    sector_filter: list[str] = field(default_factory=list)
    execution_mode: str | None = None
    sizing_profile: dict[str, Any] = field(default_factory=dict)
    risk_caps: dict[str, Any] = field(default_factory=dict)
    sector_score_threshold: float | None = None
    market_score_threshold: float | None = None
    allowed_brokers: list[str] = field(default_factory=list)
    autopilot: bool = True  # True = auto-execute (default); False = require approval
    # Production DB readers always populate these explicit authority fields.
    # Defaults preserve the pre-authority semantics for isolated callers that
    # construct this scoring-layer DTO directly.
    entries_enabled: bool = True
    exits_enabled: bool = True
    # Modes the user permits in best/auto selection (e.g. ["spot"]); selection
    # never assigns a mode outside this set.
    execution_modes_allowed: list[str] = field(default_factory=lambda: ["spot"])
    # How best/auto mode is ranked: best_return / lowest_risk / highest_sharpe.
    # None means "unset" — the persistence path treats it as "fixed" so a
    # default binding is not silently turned into a policy-ranked one (BD-3).
    mode_selection_policy: str | None = None
    preferred_mode: str | None = None


@dataclass
class ModePerformance:
    """Scoring read model for persisted execution-mode performance."""

    asset: str
    execution_mode: str
    horizon: str  # e.g., 1d, 1w, 1m
    sharpe: float
    total_return: float
    max_drawdown: float
    account_id: int | None = None
    strategy_id: str | None = None
    updated_at: datetime = field(default_factory=_now)

    # Extended fields from domain type (previously missing, caused data loss)
    sortino: float | None = None
    win_rate: float = 0.0
    avg_win: float | None = None
    avg_loss: float | None = None
    sample_size: int = 0

    # Time period fields (for proper time-window semantics)
    period_start: datetime | None = None
    period_end: datetime | None = None

    # Optional hierarchy fields for filtering
    instrument_id: int | None = None
    sector_id: int | None = None
    asset_class: str | None = None

    def has_sufficient_data(self, min_samples: int = 30) -> bool:
        """Check if there's enough data for reliable mode selection."""
        return self.sample_size >= min_samples

    def is_profitable(self) -> bool:
        """Check if mode has positive return."""
        return self.total_return > 0.0


@dataclass
class TriggerDecision:
    """Result of evaluating bindings against a score."""

    user_id: str
    should_execute: bool
    reason: str
    binding_id: int | None = None
    broker_account_id: int | None = None
    execution_mode: str | None = None
    sizing_profile: dict[str, Any] = field(default_factory=dict)
    risk_caps: dict[str, Any] = field(default_factory=dict)
    allowed_brokers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AccountRebalancePlanLegDraft:
    """Frozen selected or rejected account target awaiting persistence."""

    model_sequence: int
    command_sequence: int | None
    model_leg_id: str
    model_signal_snapshot_sha256: str
    plan_leg_id: str
    instrument_id: int
    external_signal_id: str
    symbol: str
    phase: str
    action: str
    disposition: str
    rank: float | None
    allocation_hint: float
    required: bool
    depends_on_sequences: tuple[int, ...]
    reason_code: str


@dataclass(frozen=True)
class AccountRebalancePlanDraft:
    """One immutable command plus complete selected/rejected target audit."""

    command: RebalanceExecutionCommandEvent
    model_leg_count: int
    legs: tuple[AccountRebalancePlanLegDraft, ...]


@dataclass
class InstrumentHierarchy:
    """Static mapping for symbol -> classification hierarchy."""

    symbol: str
    settlement_currency: str | None = None
    asset_class: str | None = None
    sector: str | None = None
    industry: str | None = None
    index: str | None = None


# =============================================================================
# Adapter Functions - Bridge between scoring DTOs and domain types
# =============================================================================


def trigger_to_execution_decision(
    trigger: TriggerDecision,
    signal: Signal,
    binding_id: int,
    instrument_id: int,
    broker_account_id: int,
    asset_score: float,
    sector_score: float | None = None,
    market_score: float | None = None,
    execution_policy_snapshot: dict[str, Any] | None = None,
    broker_route_snapshot: dict[str, Any] | None = None,
) -> ExecutionDecision:
    """
    Convert TriggerDecision DTO to canonical ExecutionDecision domain type.

    This adapter preserves signal traceability fields (signal_id, strategy_id, symbol)
    that would otherwise be lost in the TriggerDecision DTO.

    Args:
        trigger: The TriggerDecision from binding evaluation
        signal: The original Signal that triggered this decision
        binding_id: The user binding ID
        instrument_id: The instrument ID being evaluated
        asset_score: The calculated asset score
        sector_score: Optional sector score
        market_score: Optional market score

    Returns:
        ExecutionDecision with full traceability
    """
    # Determine direction from signal action
    # HOLD/CLOSE/FLAT are neutral - use "flat" to distinguish from directional signals
    direction = "flat"  # Default for neutral/unknown actions
    action_str = str(signal.action).upper()
    if "LONG" in action_str:
        direction = "long"
    elif "SHORT" in action_str:
        direction = "short"
    # HOLD, CLOSE, FLAT explicitly set to "flat"

    return ExecutionDecision(
        user_id=trigger.user_id,
        binding_id=binding_id,
        instrument_id=instrument_id,
        broker_account_id=broker_account_id,
        asset_score=Decimal(str(asset_score)),
        sector_score=Decimal(str(sector_score)) if sector_score is not None else None,
        market_score=Decimal(str(market_score)) if market_score is not None else None,
        should_execute=trigger.should_execute,
        execution_mode=trigger.execution_mode or "spot",
        direction=direction,
        position_size_pct=Decimal(str(trigger.sizing_profile.get("position_size_pct", 0.0))),
        position_size_usd=(
            Decimal(str(trigger.sizing_profile["position_size_usd"]))
            if trigger.sizing_profile.get("position_size_usd") is not None
            else None
        ),
        signal_id=signal.signal_id,
        strategy_id=signal.strategy_id,
        symbol=signal.symbol,
        reasoning={
            "trigger_reason": trigger.reason,
            "sizing_profile": trigger.sizing_profile,
            "risk_caps": trigger.risk_caps,
            "allowed_brokers": trigger.allowed_brokers,
            "execution_policy_snapshot": execution_policy_snapshot or {},
            "broker_route_snapshot": broker_route_snapshot or {},
        },
    )
