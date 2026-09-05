"""SQLAlchemy models for user profiles and trading configuration.

Uses PostgreSQL for all environments. SQLite compatibility maintained for unit tests only.

Decomposed package layout (May 2026):
    Shared infrastructure (``Base``, ``JSONType``, ``ArrayType``,
    ``SQLiteBigInteger``, ``generate_uuid``) lives in :mod:`._base`.
    Per-domain ORM classes live in their own submodules
    (``identity``, ``consents``, ``brokers``, ``instruments``, ``strategies``,
    ``control_plane``, ``options_templates``, ``oms``,
    ``risk_audit``, ``scoring``, ``feedback``) and are re-exported from this
    ``__init__`` so external ``from lib_application.db.models import X``
    imports + Alembic ``target_metadata`` discovery keep working unchanged.
"""

from ._base import (
    ArrayType,
    Base,
    JSONType,
    SQLiteBigInteger,
    generate_uuid,
)
from .account_rebalances import (
    AccountExecutionGeneration,
    AccountRebalancePlan,
    AccountRebalancePlanLeg,
    AccountRebalancePlanResolution,
)

# ---------------------------------------------------------------------------
# Section C — Broker Connectors & User-Linked Accounts (extracted to .brokers)
# ---------------------------------------------------------------------------
from .brokers import (
    Broker,
    BrokerCredential,
    BrokerEnvironment,
    LinkedBrokerAccount,
    ManagedSecret,
)

# ---------------------------------------------------------------------------
# Section B — Legal, Consents & Suitability (extracted to .consents)
# ---------------------------------------------------------------------------
from .consents import (
    SuitabilityQuestionnaire,
    UserConsent,
    UserSuitabilityResponse,
)

# ---------------------------------------------------------------------------
# Section G — User Control Plane (extracted to .control_plane)
# ---------------------------------------------------------------------------
from .control_plane import (
    SizingProfile,
    UserBudgetBucket,
    UserStrategyBinding,
    UserTradingPolicy,
)

# ---------------------------------------------------------------------------
# Section K (dispatch) — Scoring → Execution Dispatch & Outbox (extracted to .dispatch)
# ---------------------------------------------------------------------------
from .dispatch import (
    ExecutionDecisionLog,
    OutboxEvent,
    PendingOrder,
)

# ---------------------------------------------------------------------------
# Section O — Immutable point-in-time equity factor evidence
# ---------------------------------------------------------------------------
from .equity_evidence import (
    EquityFactorEvidence,
    EquityFactorSnapshot,
    EquityFactorSnapshotDetail,
    EquityRankSnapshot,
    EquityRankSnapshotRow,
)
from .equity_observations import (
    EquityObservation,
    EquityObservationValue,
    EquitySourceLineage,
)

# ---------------------------------------------------------------------------
# Section L — Signal Performance & Feedback Loop (extracted to .feedback)
# ---------------------------------------------------------------------------
from .feedback import (
    BacktestExperiment,
    BacktestResult,
    BacktestTrial,
    ServiceHeartbeat,
    SignalPerformance,
    StrategyConsecutiveWrongTracker,
    StrategyParameterFeedback,
)

# ---------------------------------------------------------------------------
# Section A — Core Tenancy & Users (extracted to .identity)
# ---------------------------------------------------------------------------
from .identity import (
    Organization,
    Plan,
    User,
    UserPlanSubscription,
    UserRole,
)

# ---------------------------------------------------------------------------
# Section D — Instruments & Symbol Mapping (extracted to .instruments)
# ---------------------------------------------------------------------------
from .instruments import (
    ConsumerWatermark,
    Instrument,
    InstrumentAlias,
    InstrumentBrokerSymbol,
    InstrumentPrice,
    MarketCalendar,
    MarketSession,
)

# ---------------------------------------------------------------------------
# Section N — Equity Market Reference Data (.market_reference)
# ---------------------------------------------------------------------------
from .market_reference import (
    CorporateAction,
    EarningsEvent,
    EquitySecurityIdentity,
    IndexMembership,
)
from .model_rebalances import (
    ModelRebalance,
    ModelRebalanceLeg,
)

# ---------------------------------------------------------------------------
# Section I — OMS/EMS (extracted to .oms)
# ---------------------------------------------------------------------------
from .oms import (
    ChildOrder,
    DailyNav,
    Execution,
    ExecutionLog,
    ExecutionMetric,
    OptionOrderLeg,
    Order,
    OrderIntent,
    Position,
)

# ---------------------------------------------------------------------------
# Section H — Options Strategy Templates & User Presets (extracted to .options_templates)
# ---------------------------------------------------------------------------
from .options_templates import (
    OptionStrategyTemplate,
    OptionStrategyTemplateLeg,
    UserOptionPreset,
)

# ---------------------------------------------------------------------------
# Section M (provenance) — immutable decision-context snapshots (extracted to
# .provenance)
# ---------------------------------------------------------------------------
from .provenance import (
    DecisionContext,
)

# ---------------------------------------------------------------------------
# Section J — Risk, Mandates, Audit & Notifications (extracted to .risk_audit)
# ---------------------------------------------------------------------------
from .risk_audit import (
    ApiAuditLog,
    FeatureFlag,
    OutboundWebhook,
    RiskBreach,
    RiskMandate,
    UserFeatureFlag,
    UserNotification,
)

# ---------------------------------------------------------------------------
# Section K — Scoring Engine: Sectors, Scores, Mode Performance (extracted to .scoring)
# ---------------------------------------------------------------------------
from .scoring import (
    AssetScore,
    CanonicalSignal,
    InstrumentSector,
    MarketScore,
    ModePerformance,
    ScoringRule,
    Sector,
    SectorScore,
)

# ---------------------------------------------------------------------------
# Section E — Strategy Catalog & Versions (extracted to .strategies)
# ---------------------------------------------------------------------------
from .strategies import (
    Strategy,
    StrategyDecision,
    StrategyRuntimeState,
    StrategyVersion,
    UserStrategyConfig,
)
from .strategy_panels import (
    StrategyPanelDecision,
    StrategyPanelInputRevision,
)

__all__ = [
    "AccountExecutionGeneration",
    # Shared infrastructure
    "ArrayType",
    "Base",
    "JSONType",
    "SQLiteBigInteger",
    "generate_uuid",
    # Portfolio rebalance persistence
    "AccountRebalancePlan",
    "AccountRebalancePlanLeg",
    "AccountRebalancePlanResolution",
    "ModelRebalance",
    "ModelRebalanceLeg",
    # Section A — identity
    "Organization",
    "Plan",
    "User",
    "UserPlanSubscription",
    "UserRole",
    # Section B — consents
    "SuitabilityQuestionnaire",
    "UserConsent",
    "UserSuitabilityResponse",
    # Section C — brokers
    "Broker",
    "BrokerCredential",
    "BrokerEnvironment",
    "LinkedBrokerAccount",
    "ManagedSecret",
    # Section D — instruments
    "ConsumerWatermark",
    "Instrument",
    "InstrumentAlias",
    "InstrumentBrokerSymbol",
    "InstrumentPrice",
    "MarketCalendar",
    "MarketSession",
    # Section E — strategies
    "Strategy",
    "StrategyDecision",
    "StrategyPanelDecision",
    "StrategyPanelInputRevision",
    "StrategyRuntimeState",
    "StrategyVersion",
    "UserStrategyConfig",
    # Section G — control_plane
    "SizingProfile",
    "UserBudgetBucket",
    "UserStrategyBinding",
    "UserTradingPolicy",
    # Section H — options_templates
    "OptionStrategyTemplate",
    "OptionStrategyTemplateLeg",
    "UserOptionPreset",
    # Section I — oms
    "ChildOrder",
    "DailyNav",
    "Execution",
    "ExecutionLog",
    "ExecutionMetric",
    "OptionOrderLeg",
    "Order",
    "OrderIntent",
    "Position",
    # Section J — risk_audit
    "ApiAuditLog",
    "FeatureFlag",
    "OutboundWebhook",
    "RiskBreach",
    "RiskMandate",
    "UserFeatureFlag",
    "UserNotification",
    # Section K — scoring
    "AssetScore",
    "CanonicalSignal",
    "ExecutionDecisionLog",
    "InstrumentSector",
    "MarketScore",
    "ModePerformance",
    "OutboxEvent",
    "PendingOrder",
    "ScoringRule",
    "Sector",
    "SectorScore",
    # Section L — feedback
    "BacktestExperiment",
    "BacktestResult",
    "BacktestTrial",
    "ServiceHeartbeat",
    "SignalPerformance",
    "StrategyConsecutiveWrongTracker",
    "StrategyParameterFeedback",
    # Section M — provenance
    "DecisionContext",
    # Section N — market_reference
    "CorporateAction",
    "EarningsEvent",
    "EquitySecurityIdentity",
    "IndexMembership",
    # Section O — equity_evidence
    "EquityFactorEvidence",
    "EquityFactorSnapshot",
    "EquityFactorSnapshotDetail",
    "EquityObservation",
    "EquityObservationValue",
    "EquityRankSnapshot",
    "EquityRankSnapshotRow",
    "EquitySourceLineage",
]
