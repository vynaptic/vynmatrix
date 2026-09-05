"""
Common utilities library for the vynmatrix platform.
Provides logging, configuration, signal clients, and base utilities.
"""

__version__ = "0.1.0"

from lib_common.config_validation import (
    BrokerType,
    ExecutionMode,
    RunMode,
    parse_execution_mode,
    parse_run_mode,
)
from lib_common.asset_classes import (
    CANONICAL_ASSET_CLASSES,
    CANONICAL_ASSET_CLASS_VALUES,
    REFERENCE_ONLY_ASSET_CLASSES,
    TRADABLE_ASSET_CLASSES,
    normalize_asset_class,
    normalize_optional_asset_class,
)
from lib_common.env_utils import (
    build_database_url,
    parse_bool_env,
    parse_bool_value,
    parse_float_env,
    parse_float_value,
    parse_int_env,
    parse_int_value,
    parse_list_env,
)
from lib_common.event_bus import (
    EventPublisher,
    NoOpEventPublisher,
)
from lib_common.exceptions import (
    ConfigurationError,
    VMStrategyError,
)
from lib_common.health import (
    HealthResponse,
    LivenessResponse,
    ReadinessResponse,
    add_health_endpoints,
    create_health_response,
    create_liveness_response,
    create_readiness_response,
)
from lib_common.http_exceptions import (
    APIError,
    BadGatewayError,
    BadRequestError,
    ClientError,
    ConnectionFailedError,
    ForbiddenError,
    GatewayTimeoutError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
    ServerError,
    ServiceUnavailableError,
)
from lib_common.http_exceptions import TimeoutError as HTTPTimeoutError
from lib_common.http_exceptions import (
    UnauthorizedError,
    is_retryable_status,
    raise_for_status,
)
from lib_common.internal_events import (
    BrokerRouteSnapshot,
    CanonicalSignalEvent,
    CanonicalSignalSnapshot,
    ExecutionCommandEvent,
    ExecutionPolicySnapshot,
    ExecutionResultEvent,
    FeedbackEvaluationEvent,
    ModelRebalanceLegSnapshot,
    ModelRebalanceSubmissionEvent,
    RebalanceExecutionCommandEvent,
    RebalanceExecutionLegSnapshot,
    ScoreContextSnapshot,
    ScoredSignalEvent,
    compute_account_plan_id,
    compute_model_rebalance_content_sha256,
    compute_model_rebalance_id,
    compute_rebalance_execution_content_sha256,
)
from lib_common.logging import get_logger, setup_logging
from lib_common.observability import build_trace_context, ensure_run_id
from lib_common.retries import retry_async, retry_sync

# Application lifecycle components.
# isort: off
try:
    from lib_common.app import ApplicationManager
    from lib_common.app import ConfigManager
    from lib_common.app import HealthCheckServer
    from lib_common.app import HealthStatus
    from lib_common.app import SecretsManager

    _ = (
        ApplicationManager,
        ConfigManager,
        HealthCheckServer,
        HealthStatus,
        SecretsManager,
    )

    _app_available = True
except ImportError:
    _app_available = False
# isort: on

__all__ = [
    "CANONICAL_ASSET_CLASSES",
    "CANONICAL_ASSET_CLASS_VALUES",
    "REFERENCE_ONLY_ASSET_CLASSES",
    "TRADABLE_ASSET_CLASSES",
    "APIError",
    "BadGatewayError",
    "BadRequestError",
    "BrokerRouteSnapshot",
    "BrokerType",
    "CanonicalSignalEvent",
    "CanonicalSignalSnapshot",
    "ClientError",
    "ConfigurationError",
    "ConnectionFailedError",
    "EventPublisher",
    "ExecutionCommandEvent",
    "ExecutionMode",
    "ExecutionPolicySnapshot",
    "ExecutionResultEvent",
    "FeedbackEvaluationEvent",
    "ForbiddenError",
    "GatewayTimeoutError",
    "HTTPTimeoutError",
    "HealthResponse",
    "InternalServerError",
    "LivenessResponse",
    "ModelRebalanceLegSnapshot",
    "ModelRebalanceSubmissionEvent",
    "NoOpEventPublisher",
    "NotFoundError",
    "RateLimitError",
    "ReadinessResponse",
    "RebalanceExecutionCommandEvent",
    "RebalanceExecutionLegSnapshot",
    "RunMode",
    "ScoreContextSnapshot",
    "ScoredSignalEvent",
    "ServerError",
    "ServiceUnavailableError",
    "UnauthorizedError",
    "VMStrategyError",
    "add_health_endpoints",
    "build_database_url",
    "build_trace_context",
    "compute_account_plan_id",
    "compute_model_rebalance_content_sha256",
    "compute_model_rebalance_id",
    "compute_rebalance_execution_content_sha256",
    "create_health_response",
    "create_liveness_response",
    "create_readiness_response",
    "ensure_run_id",
    "get_logger",
    "is_retryable_status",
    "normalize_asset_class",
    "normalize_optional_asset_class",
    "parse_bool_env",
    "parse_bool_value",
    "parse_execution_mode",
    "parse_float_env",
    "parse_float_value",
    "parse_int_env",
    "parse_int_value",
    "parse_list_env",
    "parse_run_mode",
    "raise_for_status",
    "retry_async",
    "retry_sync",
    "setup_logging",
]

# Add app components if available
if _app_available:
    __all__.extend(
        [
            "ApplicationManager",
            "ConfigManager",
            "HealthCheckServer",
            "HealthStatus",
            "SecretsManager",
        ]
    )
