"""
Configuration validation module.

Provides Pydantic models for validating configuration at startup and runtime.
Ensures all required settings are present and valid before the system starts.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lib_common.asset_classes import normalize_asset_class
from lib_common.env_utils import (
    build_database_url,
    parse_bool_env,
    parse_float_env,
    parse_int_env,
    parse_list_env,
    parse_weight_mapping,
)
from lib_common.logging import get_logger

logger = get_logger(__name__)
_CANONICAL_USER_ID_MAX_LENGTH = 50


class RunMode(StrEnum):
    """Runtime environment for the pipeline."""

    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class ExecutionMode(StrEnum):
    """User-level execution mode for order routing.

    Determines how a signal is translated to orders at the user/config level.
    This is the CANONICAL enum for user-facing execution mode configuration.

    For broker-level routing, see BrokerExecutionMethod in lib_strategy.types.
    """

    SPOT = "spot"
    MARGIN = "margin"
    PERPETUAL = "perpetual"
    FUTURES = "futures"
    BULL_CALL = "bull_call"
    BEAR_PUT = "bear_put"
    BULL_PUT = "bull_put"
    BEAR_CALL = "bear_call"
    IRON_CONDOR = "iron_condor"
    STRADDLE = "straddle"
    STRANGLE = "strangle"
    OPTIONS_SINGLE = "options_single"
    NOTIFY_ONLY = "notify_only"
    PAPER = "paper"

    @classmethod
    def is_options(cls, mode: ExecutionMode) -> bool:
        """Check if mode is an options strategy."""
        return mode in {
            cls.BULL_CALL,
            cls.BEAR_PUT,
            cls.BULL_PUT,
            cls.BEAR_CALL,
            cls.IRON_CONDOR,
            cls.STRADDLE,
            cls.STRANGLE,
            cls.OPTIONS_SINGLE,
        }

    @classmethod
    def is_futures(cls, mode: ExecutionMode) -> bool:
        """Check if mode is futures-based."""
        return mode in {cls.PERPETUAL, cls.FUTURES}

    @classmethod
    def is_spot(cls, mode: ExecutionMode) -> bool:
        """Check if mode is spot trading."""
        return mode in {cls.SPOT, cls.MARGIN}

    @classmethod
    def requires_broker(cls, mode: ExecutionMode) -> bool:
        """Check if mode requires a real broker (not notify-only or paper)."""
        return mode not in {cls.NOTIFY_ONLY, cls.PAPER}


class BrokerType(StrEnum):
    """Supported broker types."""

    PAPER = "paper"
    COINBASE = "coinbase"
    DERIBIT = "deribit"
    IBKR = "ibkr"
    DELTA = "delta"
    SAXO = "saxo"
    ZERODHA = "zerodha"


_BROKER_ALIASES: dict[str, str] = {
    "interactive_brokers": "ibkr",
    "interactivebrokers": "ibkr",
    "ib": "ibkr",
}

_RUN_MODE_ALIASES: dict[str, str] = {
    "prod": "live",
    "production": "live",
    "sim": "paper",
    "simulation": "paper",
}

_EXECUTION_METHOD_ALIASES: dict[str, str] = {
    "call_spread": "bull_call",
    "put_spread": "bear_put",
    "direct": "spot",
    "single_option": "options_single",
    "single_leg_option": "options_single",
}


def _normalize_broker_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, BrokerType):
        return value
    raw_value = getattr(value, "value", value)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        return _BROKER_ALIASES.get(normalized, normalized)
    return value


def normalize_broker_code(value: Any, default: str = "paper") -> str:
    """Normalize broker values to a lowercase broker code string."""
    if value is None:
        return default
    normalized = _normalize_broker_value(value)
    if isinstance(normalized, BrokerType):
        return normalized.value
    if isinstance(normalized, str):
        return normalized.strip().lower()
    return default


def _normalize_run_mode(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, RunMode):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        return _RUN_MODE_ALIASES.get(normalized, normalized)
    return value


def _normalize_execution_mode(value: Any) -> Any:
    """Normalize execution mode value to lowercase string."""
    if value is None:
        return None
    if isinstance(value, ExecutionMode):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        return _EXECUTION_METHOD_ALIASES.get(normalized, normalized)
    return value


def parse_run_mode(value: Any) -> RunMode:
    """Normalize and parse a runtime mode value."""
    return RunMode(_normalize_run_mode(value))


def parse_execution_mode(value: Any) -> ExecutionMode:
    """Normalize and parse an execution mode value."""
    return ExecutionMode(_normalize_execution_mode(value))


class DatabaseConfig(BaseModel):
    """Database connection configuration."""

    url: str = Field(..., description="Database connection URL")
    pool_size: int = Field(default=3, ge=1, le=50, description="Connection pool size")
    max_overflow: int = Field(default=2, ge=0, le=100, description="Max pool overflow")
    pool_timeout: int = Field(default=10, ge=1, le=300, description="Pool timeout seconds")
    pool_recycle: int = Field(
        default=1800,
        ge=60,
        le=86400,
        description="Seconds before a pooled connection is recycled",
    )
    echo: bool = Field(default=False, description="Echo SQL statements")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v.startswith(("postgresql://", "postgres://", "sqlite://")):
            msg = "Database URL must be PostgreSQL or SQLite"
            raise ValueError(msg)
        return v


class RiskLimits(BaseModel):
    """Risk management limits."""

    max_position_pct: Decimal = Field(
        default=Decimal("0.10"),
        ge=Decimal("0.01"),
        le=Decimal("1.0"),
        description="Maximum position size as % of portfolio",
    )
    max_total_exposure_pct: Decimal = Field(
        default=Decimal("0.50"),
        ge=Decimal("0.01"),
        le=Decimal("1.0"),
        description="Maximum gross exposure as % of portfolio",
    )
    max_daily_loss_pct: Decimal = Field(
        default=Decimal("0.05"),
        ge=Decimal("0.01"),
        le=Decimal("0.50"),
        description="Maximum daily loss as % of portfolio",
    )
    max_open_positions: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum concurrent open positions",
    )
    max_drawdown_pct: Decimal = Field(
        default=Decimal("0.20"),
        ge=Decimal("0.05"),
        le=Decimal("0.50"),
        description="Maximum drawdown before circuit breaker",
    )
    daily_trade_limit: int = Field(
        default=50,
        ge=1,
        le=1000,
        description="Maximum trades per day",
    )


class ExecutionFreshnessConfig(BaseModel):
    """Freshness limits for execution inputs and authoritative sessions."""

    max_signal_age_seconds: int = Field(
        default=300,
        ge=1,
        le=86400,
        description="Maximum accepted live signal age in seconds",
    )
    max_market_data_age_seconds: int = Field(
        default=60,
        ge=1,
        le=3600,
        description="Maximum accepted live market quote age in seconds",
    )
    max_delayed_paper_market_data_age_seconds: int = Field(
        default=1800,
        ge=60,
        le=7200,
        description=(
            "Maximum accepted delayed quote event age in paper mode; never applied to live"
        ),
    )
    max_account_state_age_seconds: int = Field(
        default=60,
        ge=1,
        le=3600,
        description="Maximum accepted live broker account snapshot age in seconds",
    )
    max_market_session_age_seconds: int = Field(
        default=3600,
        ge=60,
        le=604800,
        description="Maximum age of an authoritative broker/exchange calendar observation",
    )


class ScoringConfig(BaseModel):
    """Scoring engine configuration."""

    asset_score_threshold: Decimal = Field(
        default=Decimal("0.60"),
        ge=Decimal("0.0"),
        le=Decimal("1.0"),
        description="Minimum asset score to trigger execution",
    )
    sector_score_threshold: Decimal | None = Field(
        default=None,
        ge=Decimal("0.0"),
        le=Decimal("1.0"),
        description="Minimum sector score (optional)",
    )
    market_score_threshold: Decimal | None = Field(
        default=None,
        ge=Decimal("0.0"),
        le=Decimal("1.0"),
        description="Minimum market score (optional)",
    )
    half_life_bars: int = Field(
        default=20,
        ge=5,
        le=200,
        description="Half-life for score decay",
    )
    score_weights: dict[str, float] = Field(
        default_factory=dict,
        description="Strategy weight overrides",
    )

    @field_validator("score_weights")
    @classmethod
    def validate_score_weights(cls, value: dict[str, float]) -> dict[str, float]:
        return parse_weight_mapping(
            ",".join(f"{key}:{weight}" for key, weight in value.items()),
            name="score_weights",
        )


class _FrozenConfig(BaseModel):
    """Immutable validated startup configuration."""

    model_config = ConfigDict(frozen=True)


class IndicatorPanelRuntimeConfig(_FrozenConfig):
    """Explicit tenant and activation fence for a synchronized panel worker."""

    environment: str
    data_use_scope: str
    entitlement_owner_user_id: str
    activation_cutoff: datetime

    @field_validator("environment", "data_use_scope", "entitlement_owner_user_id")
    @classmethod
    def require_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "Indicator panel runtime binding values must not be empty"
            raise ValueError(msg)
        return normalized

    @field_validator("environment")
    @classmethod
    def normalize_panel_environment(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("data_use_scope")
    @classmethod
    def require_paper_forward_scope(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != "paper_forward":
            msg = "Synchronized indicator workers currently permit only paper_forward inputs"
            raise ValueError(msg)
        return normalized

    @field_validator("entitlement_owner_user_id")
    @classmethod
    def validate_panel_owner(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > _CANONICAL_USER_ID_MAX_LENGTH:
            msg = "Indicator panel entitlement owner exceeds the canonical user-id limit"
            raise ValueError(msg)
        return normalized

    @field_validator("activation_cutoff")
    @classmethod
    def require_utc_activation_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "Indicator panel activation cutoff must include a UTC offset"
            raise ValueError(msg)
        return value.astimezone(UTC)


class IndicatorRunnerConfig(_FrozenConfig):
    """Process-wide indicator supervisor controls resolved once at startup."""

    environment: str
    schema_path: Path
    target_strategy: str | None = None
    strategy_names: frozenset[str] = Field(default_factory=frozenset)
    allow_dev_discovery: bool = False
    start_delay_seconds: float = Field(default=0.0, ge=0.0)
    signal_api_url: str = ""
    api_key: str = Field(default="", repr=False)
    paper_promotion_manifest: Path | None = None
    deploy_image_tag: str | None = None
    max_signal_backlog_age_seconds: int = Field(default=300, ge=1, le=86400)
    max_strategy_lag_seconds: int = Field(default=300, ge=1, le=86400)
    panel_runtime: IndicatorPanelRuntimeConfig | None = None

    @field_validator("environment")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            msg = "Indicator runner environment must not be empty"
            raise ValueError(msg)
        return normalized

    @property
    def loading_mode(self) -> str:
        """Describe the frozen strategy-selection mode for health output."""
        if self.target_strategy:
            return "single"
        if self.strategy_names:
            return "bundle"
        if self.allow_dev_discovery:
            return "dev_discovery"
        return "unconfigured"


class IndicatorWorkerConfig(_FrozenConfig):
    """Process-wide indicator worker controls resolved once at startup."""

    run_mode: RunMode = RunMode.PAPER
    signal_api_url: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    database_url: str = Field(repr=False)
    signal_http_max_retries: int = Field(default=2, ge=0, le=10)
    signal_http_retry_base_delay_seconds: float = Field(default=0.5, ge=0.0, le=60.0)
    signal_http_retry_max_delay_seconds: float = Field(default=5.0, ge=0.0, le=300.0)
    signal_http_retry_jitter: float = Field(default=0.1, ge=0.0, le=1.0)
    notify_channel: str = "new_market_data"
    min_bar_coverage: float = Field(default=0.95, ge=0.0, le=1.0)
    catchup_batch_size: int = Field(default=5000, ge=1, le=100000)
    catchup_floor_seconds: int = Field(default=30, ge=1, le=3600)
    # Idle recovery pass cadence of the signal delivery loop; wakes after
    # commits make ordinary delivery independent of this value.
    signal_relay_idle_interval_seconds: int = Field(default=5, ge=1, le=60)
    panel_runtime: IndicatorPanelRuntimeConfig | None = None

    @field_validator("run_mode", mode="before")
    @classmethod
    def normalize_run_mode(cls, value: Any) -> Any:
        return _normalize_run_mode(value)

    @field_validator("notify_channel")
    @classmethod
    def require_notify_channel(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "Indicator notify channel must not be empty"
            raise ValueError(msg)
        return normalized


class ExecutionDedupConfig(_FrozenConfig):
    """Durable execution-command idempotency controls."""

    ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    allow_stale_executing_reclaim: bool = False
    use_memory_only: bool = False


class ExecutionCircuitBreakerConfig(_FrozenConfig):
    """Broker circuit-breaker timing and failure threshold."""

    threshold: int = Field(default=3, ge=1)
    window_seconds: int = Field(default=300, ge=1)
    cooldown_seconds: int = Field(default=900, ge=1)


class ExecutionPaperConfig(_FrozenConfig):
    """Local paper routing and calibrated transaction-cost defaults."""

    use_local_broker: bool = False
    slippage_pct: float = Field(default=0.001, ge=0.0, le=1.0)
    commission_pct: float = Field(default=0.001, ge=0.0, le=1.0)
    max_order_processing_lag_seconds: int = Field(default=300, ge=1, le=86400)


class ExecutionRuntimeConfig(_FrozenConfig):
    """Process-wide execution controls resolved exactly once at startup."""

    fx_max_age_seconds: int = Field(default=259200, ge=60, le=604800)
    short_eligible_asset_classes: frozenset[str] = Field(default_factory=frozenset)
    risk_guard_enabled: bool = True
    alerts_enabled: bool = False
    alert_environments: frozenset[str] = Field(default_factory=lambda: frozenset({"live"}))
    require_alert_sink: bool | None = None
    require_market_data_for_live: bool = True
    sandbox_certification_marker: Path = Path(".artifacts/coinbase_sandbox_certified.json")
    reconciliation_interval_seconds: int = Field(default=300, ge=1)
    reconciliation_position_drift_tolerance: float = Field(default=1e-6, ge=0.0)
    dedup: ExecutionDedupConfig = Field(default_factory=ExecutionDedupConfig)
    circuit_breaker: ExecutionCircuitBreakerConfig = Field(
        default_factory=ExecutionCircuitBreakerConfig
    )
    paper: ExecutionPaperConfig = Field(default_factory=ExecutionPaperConfig)

    @field_validator("short_eligible_asset_classes")
    @classmethod
    def normalize_asset_classes(cls, values: frozenset[str]) -> frozenset[str]:
        return frozenset(
            normalize_asset_class(value, field_name="short-eligible asset class")
            for value in values
            if value.strip()
        )

    @field_validator("alert_environments")
    @classmethod
    def normalize_alert_environments(cls, values: frozenset[str]) -> frozenset[str]:
        return frozenset(value.strip().lower() for value in values if value.strip())


class ExecutionEngineConfig(_FrozenConfig):
    """Execution engine configuration."""

    mode: RunMode = Field(default=RunMode.PAPER)
    allow_live: bool = Field(default=False)
    database: DatabaseConfig | None = None
    default_broker: BrokerType = Field(default=BrokerType.PAPER)
    risk_limits: RiskLimits = Field(default_factory=RiskLimits)
    freshness: ExecutionFreshnessConfig = Field(default_factory=ExecutionFreshnessConfig)
    max_concurrent_orders: int = Field(default=10, ge=1, le=100)
    runtime: ExecutionRuntimeConfig = Field(default_factory=ExecutionRuntimeConfig)

    @model_validator(mode="after")
    def validate_live_mode(self) -> ExecutionEngineConfig:
        if self.mode == RunMode.LIVE and not self.allow_live:
            msg = "Live mode requires explicit EXECUTION_ENGINE_ALLOW_LIVE=true"
            raise ValueError(msg)
        return self


class ScoringEnsembleConfig(_FrozenConfig):
    """Cross-strategy ensemble controls."""

    enabled: bool = False
    sibling_freshness_seconds: int = Field(default=3600, ge=1)
    max_siblings: int = Field(default=16, ge=0)


class ScoringFactorConfig(_FrozenConfig):
    """Optional price-derived factor-blending controls."""

    enabled: bool = False
    weights: tuple[tuple[str, float], ...] = ()
    lookback: int = Field(default=20, ge=1)
    winsorize_z: float = Field(default=3.0, gt=0.0)


class ScoringMarketContextClassConfig(_FrozenConfig):
    """Explicit per-asset-class market-context feed selection.

    Every field is required: an asset class routed away from the default feed
    must state its full source/cadence contract (an equity 1d feed cannot
    inherit the crypto 1m ``max_age_seconds``).
    """

    source: str
    timeframe: str
    window: int = Field(ge=2)
    max_age_seconds: int = Field(ge=1)

    @field_validator("source", "timeframe")
    @classmethod
    def require_non_empty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "Scoring market-context source and timeframe must be non-empty"
            raise ValueError(msg)
        return normalized


class ScoringMarketContextConfig(_FrozenConfig):
    """Point-in-time persisted-price context selection.

    The flat fields are the default feed; ``by_asset_class`` routes specific
    asset classes to their own source/cadence (e.g. equity ``eodhd``/1d next
    to the crypto default) so one scoring deployment can serve mixed feeds.
    """

    source: str = "coinbase_live"
    timeframe: str = "1m"
    window: int = Field(default=20, ge=2)
    max_age_seconds: int = Field(default=3600, ge=1)
    by_asset_class: tuple[tuple[str, ScoringMarketContextClassConfig], ...] = ()

    @field_validator("source", "timeframe")
    @classmethod
    def require_non_empty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "Scoring market-context source and timeframe must be non-empty"
            raise ValueError(msg)
        return normalized

    @field_validator("by_asset_class")
    @classmethod
    def require_known_asset_classes(
        cls, value: tuple[tuple[str, ScoringMarketContextClassConfig], ...]
    ) -> tuple[tuple[str, ScoringMarketContextClassConfig], ...]:
        normalized_entries: list[tuple[str, ScoringMarketContextClassConfig]] = []
        seen: set[str] = set()
        for asset_class, class_config in value:
            canonical = normalize_asset_class(asset_class, field_name="market-context asset class")
            if canonical in seen:
                msg = f"Duplicate market-context asset class {asset_class!r}"
                raise ValueError(msg)
            seen.add(canonical)
            normalized_entries.append((canonical, class_config))
        return tuple(normalized_entries)


class ScoringRelayConfig(_FrozenConfig):
    """Transactional-outbox relay polling and topic controls."""

    inline: bool = False
    # Wake the claim loop on the existing outbox_events notification; polling
    # on poll_interval_seconds remains the recovery path either way.
    notify_enabled: bool = True
    topics: tuple[str, ...] = (
        "execution.commands",
        "execution.rebalance.commands",
    )
    poll_interval_seconds: float = Field(default=2.0, gt=0.0)
    batch_size: int = Field(default=100, ge=1)
    lease_seconds: int = Field(default=60, ge=1)
    max_backlog_age_seconds: int = Field(default=300, ge=1)


class ScoringRuntimeConfig(_FrozenConfig):
    """Process-wide scoring controls resolved exactly once at startup."""

    environment: str = "dev"
    paper_promotion_manifest: Path | None = None
    deploy_image_tag: str | None = None
    persist_decision_context: bool = True
    bindings_cache_ttl_seconds: float = Field(default=5.0, ge=0.0)
    ensemble: ScoringEnsembleConfig = Field(default_factory=ScoringEnsembleConfig)
    factors: ScoringFactorConfig = Field(default_factory=ScoringFactorConfig)
    market_context: ScoringMarketContextConfig = Field(default_factory=ScoringMarketContextConfig)
    relay: ScoringRelayConfig = Field(default_factory=ScoringRelayConfig)

    @field_validator("environment")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            msg = "Scoring runtime environment must not be empty"
            raise ValueError(msg)
        return normalized


class ScoringEngineConfig(_FrozenConfig):
    """Scoring engine configuration."""

    mode: RunMode = RunMode.PAPER
    database: DatabaseConfig
    execution_engine_url: str | None = None
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    runtime: ScoringRuntimeConfig = Field(default_factory=ScoringRuntimeConfig)

    @field_validator("execution_engine_url")
    @classmethod
    def validate_exec_url(cls, v: str | None) -> str | None:
        if v and not v.startswith(("http://", "https://")):
            msg = "Execution engine URL must be HTTP(S)"
            raise ValueError(msg)
        return v


def load_indicator_runner_config(
    *,
    deployment_config: Mapping[str, Any] | None = None,
    secrets: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    environment: str | None = None,
) -> IndicatorRunnerConfig:
    """Load the immutable indicator-supervisor environment snapshot."""
    deployment = deployment_config or {}
    secret_values = secrets or {}
    root = repo_root or Path(__file__).resolve().parents[4]
    endpoints = deployment.get("endpoints", {})
    configured_signal_url = (
        str(endpoints.get("signal_api_url", "")) if isinstance(endpoints, Mapping) else ""
    )
    env_signal_url = os.getenv("SIGNAL_API_URL")
    resolved_environment = environment or os.getenv("ENVIRONMENT") or os.getenv("ENV") or "dev"

    return IndicatorRunnerConfig(
        environment=resolved_environment,
        schema_path=Path(
            os.getenv(
                "INDICATOR_SCHEMA_PATH",
                str(root / "config" / "schemas" / "indicator_strategy_config.schema.json"),
            )
        ),
        target_strategy=os.getenv("STRATEGY_NAME"),
        strategy_names=frozenset(parse_list_env("STRATEGY_LIST", default=[])),
        allow_dev_discovery=parse_bool_env(
            "INDICATOR_ALLOW_DEV_DISCOVERY",
            default=False,
            logger=logger,
            strict=True,
        ),
        start_delay_seconds=parse_float_env(
            "STRATEGY_START_DELAY_SECONDS",
            default=0.0,
            min_value=0.0,
            logger=logger,
            strict=True,
        ),
        signal_api_url=(configured_signal_url if env_signal_url is None else env_signal_url),
        api_key=os.getenv("API_KEY") or secret_values.get("api_key", ""),
        paper_promotion_manifest=(
            Path(manifest_path)
            if (manifest_path := os.getenv("INDICATOR_PAPER_PROMOTION_MANIFEST"))
            else None
        ),
        deploy_image_tag=os.getenv("VM_DEPLOY_IMAGE_TAG") or os.getenv("VM_IMAGE_TAG"),
        max_signal_backlog_age_seconds=parse_int_env(
            "INDICATOR_MAX_SIGNAL_BACKLOG_AGE_SECONDS",
            default=300,
            min_value=1,
            max_value=86400,
            logger=logger,
            strict=True,
        ),
        max_strategy_lag_seconds=parse_int_env(
            "INDICATOR_MAX_STRATEGY_LAG_SECONDS",
            default=300,
            min_value=1,
            max_value=86400,
            logger=logger,
            strict=True,
        ),
        panel_runtime=_load_indicator_panel_runtime_config(
            environment=resolved_environment,
        ),
    )


def load_indicator_worker_config() -> IndicatorWorkerConfig:
    """Load the immutable indicator-worker environment snapshot."""
    environment = os.getenv("ENVIRONMENT") or os.getenv("ENV") or "dev"
    return IndicatorWorkerConfig(
        run_mode=parse_run_mode(os.environ.get("RUN_MODE", "paper")),
        signal_api_url=os.getenv("SIGNAL_API_URL"),
        api_key=os.getenv("API_KEY"),
        database_url=build_database_url(),
        signal_http_max_retries=parse_int_env(
            "SIGNAL_HTTP_MAX_RETRIES",
            default=2,
            min_value=0,
            max_value=10,
            logger=logger,
            strict=True,
        ),
        signal_http_retry_base_delay_seconds=parse_float_env(
            "SIGNAL_HTTP_RETRY_BASE_DELAY_SEC",
            default=0.5,
            min_value=0.0,
            max_value=60.0,
            logger=logger,
            strict=True,
        ),
        signal_http_retry_max_delay_seconds=parse_float_env(
            "SIGNAL_HTTP_RETRY_MAX_DELAY_SEC",
            default=5.0,
            min_value=0.0,
            max_value=300.0,
            logger=logger,
            strict=True,
        ),
        signal_http_retry_jitter=parse_float_env(
            "SIGNAL_HTTP_RETRY_JITTER",
            default=0.1,
            min_value=0.0,
            max_value=1.0,
            logger=logger,
            strict=True,
        ),
        notify_channel=os.environ.get("INGESTOR_NOTIFY_CHANNEL", "new_market_data"),
        min_bar_coverage=parse_float_env(
            "INDICATOR_MIN_BAR_COVERAGE",
            default=0.95,
            min_value=0.0,
            max_value=1.0,
            logger=logger,
            strict=True,
        ),
        catchup_batch_size=parse_int_env(
            "SIGNAL_CATCHUP_BATCH_SIZE",
            default=5000,
            min_value=1,
            max_value=100000,
            logger=logger,
            strict=True,
        ),
        catchup_floor_seconds=parse_int_env(
            "SIGNAL_WORKER_CATCHUP_FLOOR_SEC",
            default=30,
            min_value=1,
            max_value=3600,
            logger=logger,
            strict=True,
        ),
        signal_relay_idle_interval_seconds=parse_int_env(
            "SIGNAL_RELAY_IDLE_INTERVAL_SEC",
            default=5,
            min_value=1,
            max_value=60,
            logger=logger,
            strict=True,
        ),
        panel_runtime=_load_indicator_panel_runtime_config(environment=environment),
    )


def _load_indicator_panel_runtime_config(
    *,
    environment: str,
) -> IndicatorPanelRuntimeConfig | None:
    """Load the all-or-nothing synchronized-panel tenant fence."""

    scope = os.getenv("INDICATOR_PANEL_DATA_USE_SCOPE")
    configured_owner = os.getenv("INDICATOR_PANEL_ENTITLEMENT_OWNER_USER_ID")
    activation_cutoff = os.getenv("INDICATOR_PANEL_ACTIVATION_CUTOFF")
    if not any((scope, configured_owner, activation_cutoff)):
        return None
    owner = configured_owner or os.getenv("SP500_RESEARCH_OWNER_USER_ID")
    missing = tuple(
        name
        for name, value in (
            ("INDICATOR_PANEL_DATA_USE_SCOPE", scope),
            ("INDICATOR_PANEL_ENTITLEMENT_OWNER_USER_ID", owner),
            ("INDICATOR_PANEL_ACTIVATION_CUTOFF", activation_cutoff),
        )
        if not value
    )
    if missing or scope is None or owner is None or activation_cutoff is None:
        msg = "Incomplete indicator panel runtime binding: missing " + ", ".join(missing)
        raise ValueError(msg)
    try:
        parsed_activation_cutoff = datetime.fromisoformat(activation_cutoff.replace("Z", "+00:00"))
    except ValueError as exc:
        msg = "INDICATOR_PANEL_ACTIVATION_CUTOFF must be an ISO-8601 timestamp"
        raise ValueError(msg) from exc
    return IndicatorPanelRuntimeConfig(
        environment=environment,
        data_use_scope=scope,
        entitlement_owner_user_id=owner,
        activation_cutoff=parsed_activation_cutoff,
    )


def load_indicator_log_level() -> str:
    """Resolve indicator subprocess logging before other startup work."""
    return os.getenv("LOG_LEVEL", "INFO")


def load_execution_engine_config(
    *,
    require_database: bool = True,
) -> ExecutionEngineConfig:
    """Load execution engine configuration from environment."""
    db_url = os.getenv("DATABASE_URL")
    if require_database and not db_url:
        msg = "DATABASE_URL environment variable required"
        raise ValueError(msg)

    mode_str: str = os.getenv("RUN_MODE") or os.getenv("EXECUTION_MODE") or "paper"
    mode_str = mode_str.lower()
    try:
        mode = parse_run_mode(mode_str)
    except ValueError as exc:
        msg = f"Invalid RUN_MODE: {mode_str}"
        raise ValueError(msg) from exc

    allow_live = parse_bool_env(
        "EXECUTION_ENGINE_ALLOW_LIVE",
        default=False,
        logger=logger,
        strict=True,
    )
    require_alert_sink_raw = os.getenv("EXECUTION_REQUIRE_ALERT_SINK")
    reconciliation_default = 60 if mode == RunMode.LIVE else 300

    return ExecutionEngineConfig(
        mode=mode,
        allow_live=allow_live,
        database=DatabaseConfig(url=db_url) if db_url else None,
        freshness=ExecutionFreshnessConfig(
            max_signal_age_seconds=parse_int_env(
                "EXECUTION_MAX_SIGNAL_AGE_SECONDS",
                default=300,
                min_value=1,
                logger=logger,
                strict=True,
            ),
            max_market_data_age_seconds=parse_int_env(
                "EXECUTION_MAX_MARKET_DATA_AGE_SECONDS",
                default=60,
                min_value=1,
                logger=logger,
                strict=True,
            ),
            max_delayed_paper_market_data_age_seconds=parse_int_env(
                "EXECUTION_MAX_DELAYED_PAPER_MARKET_DATA_AGE_SECONDS",
                default=1800,
                min_value=60,
                max_value=7200,
                logger=logger,
                strict=True,
            ),
            max_account_state_age_seconds=parse_int_env(
                "EXECUTION_MAX_ACCOUNT_STATE_AGE_SECONDS",
                default=60,
                min_value=1,
                logger=logger,
                strict=True,
            ),
            max_market_session_age_seconds=parse_int_env(
                "EXECUTION_MAX_MARKET_SESSION_AGE_SECONDS",
                default=3600,
                min_value=60,
                max_value=604800,
                logger=logger,
                strict=True,
            ),
        ),
        runtime=ExecutionRuntimeConfig(
            fx_max_age_seconds=parse_int_env(
                "EXECUTION_FX_MAX_AGE_SECONDS",
                default=259200,
                min_value=60,
                max_value=604800,
                logger=logger,
                strict=True,
            ),
            short_eligible_asset_classes=frozenset(
                parse_list_env("EXECUTION_SHORT_ELIGIBLE_ASSET_CLASSES", default=[])
            ),
            risk_guard_enabled=parse_bool_env(
                "EXECUTION_RISK_GUARD_ENABLED",
                default=True,
                logger=logger,
                strict=True,
            ),
            alerts_enabled=parse_bool_env(
                "EXECUTION_ALERTS_ENABLED",
                default=False,
                logger=logger,
                strict=True,
            ),
            alert_environments=frozenset(
                parse_list_env("EXECUTION_ALERT_ENVIRONMENTS", default=["live"])
            ),
            require_alert_sink=(
                None
                if require_alert_sink_raw is None
                else parse_bool_env(
                    "EXECUTION_REQUIRE_ALERT_SINK",
                    default=False,
                    logger=logger,
                    strict=True,
                )
            ),
            require_market_data_for_live=parse_bool_env(
                "EXECUTION_REQUIRE_MARKET_DATA_FOR_LIVE",
                default=True,
                logger=logger,
                strict=True,
            ),
            sandbox_certification_marker=Path(
                os.getenv(
                    "EXECUTION_SANDBOX_CERTIFICATION_MARKER",
                    ".artifacts/coinbase_sandbox_certified.json",
                )
            ),
            reconciliation_interval_seconds=parse_int_env(
                "EXECUTION_RECONCILIATION_INTERVAL_SEC",
                default=reconciliation_default,
                min_value=1,
                logger=logger,
                strict=True,
            ),
            reconciliation_position_drift_tolerance=parse_float_env(
                "EXECUTION_RECON_POSITION_DRIFT_TOLERANCE",
                default=1e-6,
                min_value=0.0,
                logger=logger,
                strict=True,
            ),
            dedup=ExecutionDedupConfig(
                ttl_seconds=parse_int_env(
                    "EXECUTION_DEDUP_TTL_SECONDS",
                    default=3600,
                    min_value=60,
                    max_value=86400,
                    logger=logger,
                    strict=True,
                ),
                allow_stale_executing_reclaim=parse_bool_env(
                    "EXECUTION_DEDUP_ALLOW_STALE_EXECUTING_RECLAIM",
                    default=False,
                    logger=logger,
                    strict=True,
                ),
                use_memory_only=parse_bool_env(
                    "EXECUTION_DEDUP_USE_MEMORY_ONLY",
                    default=False,
                    logger=logger,
                    strict=True,
                ),
            ),
            circuit_breaker=ExecutionCircuitBreakerConfig(
                threshold=parse_int_env(
                    "EXECUTION_CIRCUIT_BREAKER_THRESHOLD",
                    default=3,
                    min_value=1,
                    logger=logger,
                    strict=True,
                ),
                window_seconds=parse_int_env(
                    "EXECUTION_CIRCUIT_BREAKER_WINDOW_SEC",
                    default=300,
                    min_value=1,
                    logger=logger,
                    strict=True,
                ),
                cooldown_seconds=parse_int_env(
                    "EXECUTION_CIRCUIT_BREAKER_COOLDOWN_SEC",
                    default=900,
                    min_value=1,
                    logger=logger,
                    strict=True,
                ),
            ),
            paper=ExecutionPaperConfig(
                use_local_broker=parse_bool_env(
                    "EXECUTION_USE_LOCAL_PAPER_BROKER",
                    default=False,
                    logger=logger,
                    strict=True,
                ),
                slippage_pct=parse_float_env(
                    "PAPER_BROKER_SLIPPAGE_PCT",
                    default=0.001,
                    min_value=0.0,
                    max_value=1.0,
                    logger=logger,
                    strict=True,
                ),
                commission_pct=parse_float_env(
                    "PAPER_BROKER_COMMISSION_PCT",
                    default=0.001,
                    min_value=0.0,
                    max_value=1.0,
                    logger=logger,
                    strict=True,
                ),
                max_order_processing_lag_seconds=parse_int_env(
                    "EXECUTION_PAPER_ORDER_MAX_LAG_SECONDS",
                    default=300,
                    min_value=1,
                    max_value=86400,
                    logger=logger,
                    strict=True,
                ),
            ),
        ),
    )


def _parse_market_context_overrides_env() -> tuple[
    tuple[str, ScoringMarketContextClassConfig], ...
]:
    """Parse ``SCORING_MARKET_CONTEXT_BY_ASSET_CLASS`` (JSON object) strictly.

    Shape: ``{"equity": {"source": "eodhd", "timeframe": "1d", "window": 20,
    "max_age_seconds": 432000}}``. Every per-class entry must be complete —
    partial entries, unknown asset classes, or malformed JSON fail startup
    instead of silently scoring against the default feed.
    """
    raw = os.getenv("SCORING_MARKET_CONTEXT_BY_ASSET_CLASS", "").strip()
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"SCORING_MARKET_CONTEXT_BY_ASSET_CLASS is not valid JSON: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = "SCORING_MARKET_CONTEXT_BY_ASSET_CLASS must be a JSON object keyed by asset class"
        raise TypeError(msg)
    entries: list[tuple[str, ScoringMarketContextClassConfig]] = []
    for asset_class, class_payload in payload.items():
        if not isinstance(class_payload, dict):
            msg = (
                "SCORING_MARKET_CONTEXT_BY_ASSET_CLASS entry for "
                f"{asset_class!r} must be a JSON object"
            )
            raise TypeError(msg)
        entries.append((asset_class, ScoringMarketContextClassConfig(**class_payload)))
    return tuple(entries)


def load_scoring_engine_config() -> ScoringEngineConfig:
    """Load scoring engine configuration from environment."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        msg = "DATABASE_URL environment variable required"
        raise ValueError(msg)

    exec_url = os.getenv("EXEC_ENGINE_URL")
    raw_mode = os.getenv("RUN_MODE") or os.getenv("EXECUTION_MODE") or "paper"
    try:
        mode = parse_run_mode(raw_mode)
    except ValueError as exc:
        msg = f"Invalid scoring RUN_MODE: {raw_mode}"
        raise ValueError(msg) from exc
    half_life = parse_int_env(
        "HALF_LIFE_BARS",
        default=20,
        min_value=5,
        max_value=200,
        logger=logger,
        strict=True,
    )

    # Parse score weights
    weights = parse_weight_mapping(
        os.getenv("SCORE_WEIGHTS", ""),
        name="SCORE_WEIGHTS",
    )
    factor_weights = parse_weight_mapping(
        os.getenv("SCORING_FACTOR_WEIGHTS", ""),
        name="SCORING_FACTOR_WEIGHTS",
    )

    return ScoringEngineConfig(
        mode=mode,
        database=DatabaseConfig(url=db_url),
        execution_engine_url=exec_url,
        scoring=ScoringConfig(
            half_life_bars=half_life,
            score_weights=weights,
        ),
        runtime=ScoringRuntimeConfig(
            environment=os.getenv("ENVIRONMENT") or os.getenv("ENV") or "dev",
            paper_promotion_manifest=(
                Path(manifest_path)
                if (manifest_path := os.getenv("SCORING_PAPER_PROMOTION_MANIFEST"))
                else None
            ),
            deploy_image_tag=os.getenv("VM_DEPLOY_IMAGE_TAG") or os.getenv("VM_IMAGE_TAG"),
            persist_decision_context=parse_bool_env(
                "SCORING_PERSIST_DECISION_CONTEXT",
                default=True,
                logger=logger,
                strict=True,
            ),
            bindings_cache_ttl_seconds=parse_float_env(
                "SCORING_BINDINGS_CACHE_TTL_SECONDS",
                default=5.0,
                min_value=0.0,
                logger=logger,
                strict=True,
            ),
            ensemble=ScoringEnsembleConfig(
                enabled=parse_bool_env(
                    "SCORING_CROSS_STRATEGY_ENSEMBLE",
                    default=False,
                    logger=logger,
                    strict=True,
                ),
                sibling_freshness_seconds=parse_int_env(
                    "SCORING_SIBLING_FRESHNESS_SECONDS",
                    default=3600,
                    min_value=1,
                    logger=logger,
                    strict=True,
                ),
                max_siblings=parse_int_env(
                    "SCORING_MAX_SIBLINGS",
                    default=16,
                    min_value=0,
                    logger=logger,
                    strict=True,
                ),
            ),
            factors=ScoringFactorConfig(
                enabled=parse_bool_env(
                    "SCORING_MULTI_FACTOR_ENABLED",
                    default=False,
                    logger=logger,
                    strict=True,
                ),
                weights=tuple(factor_weights.items()),
                lookback=parse_int_env(
                    "SCORING_FACTOR_LOOKBACK",
                    default=20,
                    min_value=1,
                    logger=logger,
                    strict=True,
                ),
                winsorize_z=parse_float_env(
                    "SCORING_FACTOR_WINSORIZE_Z",
                    default=3.0,
                    min_value=0.000001,
                    logger=logger,
                    strict=True,
                ),
            ),
            market_context=ScoringMarketContextConfig(
                source=os.getenv(
                    "SCORING_MARKET_CONTEXT_SOURCE",
                    os.getenv("INGESTOR_SOURCE", "coinbase_live"),
                ),
                timeframe=os.getenv("SCORING_MARKET_CONTEXT_TIMEFRAME", "1m"),
                window=parse_int_env(
                    "SCORING_MARKET_CONTEXT_WINDOW",
                    default=20,
                    min_value=2,
                    logger=logger,
                    strict=True,
                ),
                max_age_seconds=parse_int_env(
                    "SCORING_MARKET_CONTEXT_MAX_AGE_SECONDS",
                    default=3600,
                    min_value=1,
                    logger=logger,
                    strict=True,
                ),
                by_asset_class=_parse_market_context_overrides_env(),
            ),
            relay=ScoringRelayConfig(
                inline=parse_bool_env(
                    "SCORING_OUTBOX_RELAY_INLINE",
                    default=False,
                    logger=logger,
                    strict=True,
                ),
                notify_enabled=parse_bool_env(
                    "SCORING_OUTBOX_NOTIFY_ENABLED",
                    default=True,
                    logger=logger,
                    strict=True,
                ),
                topics=tuple(
                    parse_list_env(
                        "SCORING_OUTBOX_TOPICS",
                        default=[
                            "execution.commands",
                            "execution.rebalance.commands",
                        ],
                    )
                ),
                poll_interval_seconds=parse_float_env(
                    "SCORING_OUTBOX_POLL_SEC",
                    default=2.0,
                    min_value=0.01,
                    logger=logger,
                    strict=True,
                ),
                batch_size=parse_int_env(
                    "SCORING_OUTBOX_BATCH_SIZE",
                    default=100,
                    min_value=1,
                    logger=logger,
                    strict=True,
                ),
                lease_seconds=parse_int_env(
                    "SCORING_OUTBOX_LEASE_SEC",
                    default=60,
                    min_value=1,
                    logger=logger,
                    strict=True,
                ),
                max_backlog_age_seconds=parse_int_env(
                    "SCORING_OUTBOX_MAX_AGE_SECONDS",
                    default=300,
                    min_value=1,
                    logger=logger,
                    strict=True,
                ),
            ),
        ),
    )
