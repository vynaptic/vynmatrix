"""Explicit process and credential composition for the single-owner paper runtime."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ProcessSpec:
    """One existing application entrypoint, with its own environment and listener."""

    name: str
    command: tuple[str, ...]
    environment: dict[str, str]
    port: int
    stop_order: int = 0
    readiness_path: str = "/ready"


_COMMON = frozenset(
    [
        "PATH",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "LANG",
        "LC_ALL",
        "TZ",
        "ENV",
        "ENVIRONMENT",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_POOL_TIMEOUT_SECONDS",
        "DB_POOL_RECYCLE_SECONDS",
        "DB_POOL_CONNECTION_BUDGET",
    ]
)
_SCORING = frozenset(
    [
        "VM_DEPLOY_IMAGE_TAG",
        "SCORING_PAPER_PROMOTION_MANIFEST",
        "HALF_LIFE_BARS",
        "SCORE_WEIGHTS",
        "SCORING_PERSIST_DECISION_CONTEXT",
        "SCORING_BINDINGS_CACHE_TTL_SECONDS",
        "SCORING_OUTBOX_TOPICS",
        "SCORING_OUTBOX_POLL_SEC",
        "SCORING_OUTBOX_BATCH_SIZE",
        "SCORING_OUTBOX_LEASE_SEC",
        "SCORING_OUTBOX_MAX_AGE_SECONDS",
        "SCORING_OUTBOX_NOTIFY_ENABLED",
        "SCORING_CROSS_STRATEGY_ENSEMBLE",
        "SCORING_SIBLING_FRESHNESS_SECONDS",
        "SCORING_MAX_SIBLINGS",
        "SCORING_MARKET_CONTEXT_SOURCE",
        "SCORING_MARKET_CONTEXT_TIMEFRAME",
        "SCORING_MARKET_CONTEXT_WINDOW",
        "SCORING_MARKET_CONTEXT_MAX_AGE_SECONDS",
        "SCORING_MARKET_CONTEXT_BY_ASSET_CLASS",
        "SCORING_MULTI_FACTOR_ENABLED",
        "SCORING_FACTOR_WEIGHTS",
        "SCORING_FACTOR_LOOKBACK",
        "SCORING_FACTOR_WINSORIZE_Z",
    ]
)
_EXECUTION = frozenset(
    [
        "PAPER_BROKER_SLIPPAGE_PCT",
        "PAPER_BROKER_COMMISSION_PCT",
        "EXECUTION_PAPER_ORDER_MAX_LAG_SECONDS",
        "EXECUTION_DEDUP_TTL_SECONDS",
        "EXECUTION_DEDUP_ALLOW_STALE_EXECUTING_RECLAIM",
        "EXECUTION_CIRCUIT_BREAKER_THRESHOLD",
        "EXECUTION_CIRCUIT_BREAKER_WINDOW_SEC",
        "EXECUTION_CIRCUIT_BREAKER_COOLDOWN_SEC",
        "EXECUTION_RISK_GUARD_ENABLED",
        "EXECUTION_REQUIRE_MARKET_DATA_FOR_LIVE",
        "EXECUTION_ALERTS_ENABLED",
        "EXECUTION_ALERT_ENVIRONMENTS",
        "ALERT_WEBHOOK_URL",
        "ALERT_TELEGRAM_BOT_TOKEN",
        "ALERT_TELEGRAM_CHAT_ID",
        "ALERT_EMAIL_SMTP_HOST",
        "ALERT_EMAIL_SMTP_PORT",
        "ALERT_EMAIL_FROM",
        "ALERT_EMAIL_TO",
        "ALERT_EMAIL_USERNAME",
        "ALERT_EMAIL_PASSWORD",
        "ALERT_EMAIL_TLS",
        "EXECUTION_SHORT_ELIGIBLE_ASSET_CLASSES",
        "EXECUTION_FX_MAX_AGE_SECONDS",
        "EXECUTION_MAX_SIGNAL_AGE_SECONDS",
        "EXECUTION_MAX_MARKET_DATA_AGE_SECONDS",
        "EXECUTION_MAX_DELAYED_PAPER_MARKET_DATA_AGE_SECONDS",
        "EXECUTION_MAX_ACCOUNT_STATE_AGE_SECONDS",
        "EXECUTION_MAX_MARKET_SESSION_AGE_SECONDS",
        "EXECUTION_RECON_POSITION_DRIFT_TOLERANCE",
    ]
)
_INDICATOR = frozenset(
    [
        "STRATEGY_LIST",
        "VM_DEPLOY_IMAGE_TAG",
        "INDICATOR_PAPER_PROMOTION_MANIFEST",
        "INDICATOR_MIN_BAR_COVERAGE",
        "SIGNAL_CATCHUP_BATCH_SIZE",
        "SIGNAL_RELAY_IDLE_INTERVAL_SEC",
        "INDICATOR_MAX_SIGNAL_BACKLOG_AGE_SECONDS",
        "INDICATOR_MAX_STRATEGY_LAG_SECONDS",
        "INDICATOR_PANEL_DATA_USE_SCOPE",
        "INDICATOR_PANEL_ENTITLEMENT_OWNER_USER_ID",
        "INDICATOR_PANEL_ACTIVATION_CUTOFF",
    ]
)
_FEEDBACK = frozenset(
    [
        "WRONG_THRESHOLD",
        "EVALUATION_INTERVAL",
        "FEEDBACK_PRICE_MAX_STALENESS_DAYS",
        "FEEDBACK_EVALUATION_HORIZONS",
        "FEEDBACK_HEARTBEAT_MAX_AGE_SECONDS",
    ]
)
_MARKET = frozenset(
    [
        "INGESTOR_SYMBOLS",
        "INGESTOR_SOURCE",
        "INGESTOR_GRANULARITY",
        "INGESTOR_POLL_INTERVAL_SEC",
        "INGESTOR_CANDLE_POLL_INTERVAL_SEC",
        "INGESTOR_BACKFILL_MINUTES",
        "INGESTOR_NOTIFY_CHANNEL",
        "INGESTOR_STALENESS_THRESHOLD_SEC",
        "INGESTOR_BACKFILL_DAYS",
    ]
)
_PROVIDER_ENV = {
    "coinbase_live": frozenset({"COINBASE_API_KEY", "COINBASE_API_SECRET"}),
    "eodhd": frozenset(
        [
            "EODHD_API_TOKEN",
            "EDGAR_USER_AGENT",
            "EODHD_DELAYED_QUOTES_ENABLED",
            "EODHD_DAILY_QUOTA_MIN_COOLDOWN_SEC",
            "SP500_RESEARCH_OWNER_USER_ID",
        ]
    ),
    "ibkr": frozenset({"IBKR_GATEWAY_URL", "IBKR_CA_CERT"}),
    "zerodha": frozenset({"ZERODHA_MARKET_DATA_API_KEY", "ZERODHA_MARKET_DATA_ACCESS_TOKEN"}),
    "saxo_live": frozenset(
        [
            "SAXO_MARKET_DATA_ACCESS_TOKEN",
            "SAXO_MARKET_DATA_ACCESS_TOKEN_EXPIRES_AT",
            "SAXO_MARKET_DATA_ACCOUNT_KEY",
        ]
    ),
    "deribit": frozenset(),
    "deribit_testnet": frozenset(),
    "delta": frozenset(),
    "delta_india": frozenset(),
}
_PROVIDER_ENV["saxo_simulation"] = _PROVIDER_ENV["saxo_live"]
_PANEL = frozenset(
    [
        "EODHD_API_TOKEN",
        "EDGAR_USER_AGENT",
        "QUALITY_COMPOUNDER_PANELS_ENABLED",
        "QUALITY_COMPOUNDER_ENTITLEMENT_OWNER_USER_ID",
        "QUALITY_COMPOUNDER_ROUND_TRIP_COMMISSION_BPS",
        "QUALITY_COMPOUNDER_OFFICIAL_SESSION_ARTIFACT",
        "QUALITY_COMPOUNDER_OFFICIAL_SESSION_SHA256",
        "EQUITY_CATALOGUE_ARTIFACT",
        "EQUITY_CATALOGUE_SHA256",
        "EQUITY_CATALOGUE_DRY_RUN",
    ]
)
_MAINTENANCE = frozenset(
    [
        "DATABASE_URL",
        "DB_USER",
        "DB_PASSWORD",
        "MIGRATION_DATABASE_URL",
        "ADMIN_DATABASE_URL",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "PGPASSWORD",
        "PGUSER",
    ]
)
_CALENDARS = {
    "calendar-ibkr": ("IBKR", "ibkr", 8005),
    "calendar-saxo": ("SAXO", "saxo_live", 8006),
    "calendar-zerodha": ("ZERODHA", "nse", 8008),
}
_MARKET_MODULE = "apps.market_data_ingestor.market_data_ingestor.main"


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        msg = f"{key} must be explicitly configured"
        raise ValueError(msg)
    return value


def _validate_runtime(env: Mapping[str, str]) -> None:
    for key in sorted(_MAINTENANCE):
        if env.get(key, "").strip():
            msg = f"Maintenance credential {key} is forbidden in a runtime group"
            raise ValueError(msg)
    for key, expected in {
        "EXECUTION_MODE": "paper",
        "RUN_MODE": "paper",
        "EXECUTION_ENGINE_ALLOW_LIVE": "false",
    }.items():
        if env.get(key, expected).strip().lower() != expected:
            msg = f"Unsafe runtime mode setting: {key}"
            raise ValueError(msg)


def _environment(env: Mapping[str, str], role: str, keys: frozenset[str]) -> dict[str, str]:
    key = f"{role.upper()}_DATABASE_URL"
    database_url = _required(env, key)
    try:
        parsed = urlsplit(database_url)
        valid = (
            parsed.scheme in {"postgresql", "postgresql+psycopg2"}
            and parsed.username == f"vm_{role}_login"
            and parsed.password
            and parsed.hostname
            and parsed.path.strip("/")
        )
    except ValueError:
        valid = False
    if not valid:
        msg = f"{key} must name the explicit vm_{role}_login PostgreSQL role"
        raise ValueError(msg)
    # Compose represents unset optional settings as empty strings. Omit those
    # settings so existing entrypoints retain their defaults (some are dynamic,
    # such as feedback progress age and candle polling/freshness).
    child = {
        name: env[name]
        for name in _COMMON | keys
        if name in env and (name in _COMMON or env[name] != "")
    }
    child.update(
        {
            "DATABASE_URL": database_url,
            "EXECUTION_MODE": "paper",
            "RUN_MODE": "paper",
            "EXECUTION_ENGINE_ALLOW_LIVE": "false",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LOG_FORMAT": "json",
            "HOST": "0.0.0.0",
        }
    )
    if role in {"feedback", "market_data"}:
        child["API_KEY"] = _required(env, f"{role.upper()}_API_KEY")
    return child


def _spec(
    name: str,
    module: str,
    child: dict[str, str],
    port: int,
    *,
    argument: str | None = None,
    stop_order: int = 0,
    readiness_path: str = "/ready",
) -> ProcessSpec:
    child["PORT"] = str(port)
    command: tuple[str, ...] = (sys.executable, "-m", module)
    if argument:
        command += (argument,)
    return ProcessSpec(name, command, child, port, stop_order, readiness_path)


def _applications(env: Mapping[str, str]) -> list[ProcessSpec]:
    keys = [
        _required(env, key)
        for key in (
            "BACKEND_ADMIN_API_KEY",
            "SCORING_API_KEY",
            "EXECUTION_API_KEY",
            "SCORING_ADMIN_API_KEY",
            "EXECUTION_ADMIN_API_KEY",
        )
    ]
    configured_keys = keys + [
        env[name].strip()
        for name in ("FEEDBACK_API_KEY", "MARKET_DATA_API_KEY")
        if env.get(name, "").strip()
    ]
    if len(set(configured_keys)) != len(configured_keys):
        msg = "Configured service and administrative keys must be distinct"
        raise ValueError(msg)
    backend = _environment(env, "backend", frozenset())
    backend.update({"BACKEND_ADMIN_API_KEY": keys[0], "BACKEND_ALLOW_ANON": "false"})
    scoring = _environment(env, "scoring", _SCORING)
    scoring.update(
        {
            "API_KEY": keys[1],
            "ADMIN_API_KEY": keys[3],
            "EXEC_ENGINE_API_KEY": keys[2],
            "EXEC_ENGINE_URL": "http://127.0.0.1:8000",
            "SCORING_OUTBOX_RELAY_INLINE": "true",
        }
    )
    execution = _environment(env, "execution", _EXECUTION)
    execution.update(
        {
            "API_KEY": keys[2],
            "ADMIN_API_KEY": keys[4],
            "EXECUTION_USE_LOCAL_PAPER_BROKER": "true",
            "EXECUTION_DEDUP_USE_MEMORY_ONLY": "false",
        }
    )
    for child in (backend, execution):
        child.update(
            {"SECRETS_BACKEND": "db", "SECRETS_MASTER_KEYS": _required(env, "SECRETS_MASTER_KEYS")}
        )
    return [
        _spec("backend", "apps.backend.backend.main", backend, 8081, stop_order=1),
        _spec("scoring", "apps.scoring_engine.scoring_engine.main", scoring, 8001, stop_order=1),
        _spec("execution", "apps.execution_engine.main", execution, 8000, stop_order=2),
    ]


def _market_environment(env: Mapping[str, str], *, equity: bool = False) -> dict[str, str]:
    source = "eodhd" if equity else env.get("INGESTOR_SOURCE", "coinbase_live")
    if source not in _PROVIDER_ENV:
        msg = "INGESTOR_SOURCE is not supported by the platform process selection"
        raise ValueError(msg)
    child = _environment(env, "market_data", _MARKET | _PROVIDER_ENV[source])
    child["INGESTOR_SOURCE"] = source
    child["INGESTOR_SYMBOLS"] = _required(
        env, "EQUITY_INGESTOR_SYMBOLS" if equity else "INGESTOR_SYMBOLS"
    )
    if equity:
        child["INGESTOR_GRANULARITY"] = "ONE_DAY"
        for name, default in {
            "INGESTOR_POLL_INTERVAL_SEC": "300",
            "INGESTOR_CANDLE_POLL_INTERVAL_SEC": "3600",
            "INGESTOR_STALENESS_THRESHOLD_SEC": "350000",
        }.items():
            child[name] = env.get(f"EQUITY_{name}", default)
    return child


def _calendar(name: str, env: Mapping[str, str], app_host: str) -> ProcessSpec:
    prefix, provider, port = _CALENDARS[name]
    child = _environment(env, "market_data", _PROVIDER_ENV.get(provider, frozenset()))
    child.update(
        {
            "MARKET_CALENDAR_PROVIDER": provider,
            "MARKET_CALENDAR_SYMBOLS": _required(env, f"{prefix}_MARKET_CALENDAR_SYMBOLS"),
            "MARKET_CALENDAR_API_URL": f"http://{app_host}:8081",
            "MARKET_CALENDAR_ADMIN_API_KEY": _required(env, "BACKEND_ADMIN_API_KEY"),
            "MARKET_CALENDAR_POLL_INTERVAL_SEC": env.get(
                f"{prefix}_MARKET_CALENDAR_POLL_INTERVAL_SEC", "30" if provider == "nse" else "300"
            ),
        }
    )
    if provider == "nse":
        child["MARKET_CALENDAR_NSE_MARKET"] = env.get(
            "ZERODHA_MARKET_CALENDAR_NSE_MARKET", "Capital Market"
        )
        child["MARKET_CALENDAR_NSE_LEASE_SECONDS"] = env.get(
            "ZERODHA_MARKET_CALENDAR_NSE_LEASE_SECONDS", "120"
        )
    return _spec(name, _MARKET_MODULE, child, port, argument="market-calendars")


def build_processes(group: str, env: Mapping[str, str]) -> list[ProcessSpec]:
    """Build only explicitly selected producers, preserving each service's authority."""
    _validate_runtime(env)
    application_group = env.get("PLATFORM_APPLICATION_GROUP", "application")
    if application_group not in {"application", "all"}:
        msg = "PLATFORM_APPLICATION_GROUP must be application or all"
        raise ValueError(msg)
    if group == "workers" and application_group == "all":
        msg = "PLATFORM_APPLICATION_GROUP=all cannot start another workers group"
        raise ValueError(msg)
    if group not in {"application", "workers", "all"}:
        msg = "Unknown platform process group"
        raise ValueError(msg)
    specs = _applications(env) if group in {"application", "all"} else []
    if group == "application":
        return specs
    feedback = _environment(env, "feedback", _FEEDBACK)
    feedback["FEEDBACK_RUN_MODE"] = "daemon"
    feedback.setdefault("EVALUATION_INTERVAL", "300")
    specs.append(
        _spec(
            "feedback",
            "apps.feedback_loop_engine.feedback_loop_engine.main",
            feedback,
            8002,
            argument="daemon",
        )
    )
    raw = env.get("PLATFORM_WORKERS", "").strip()
    selected = [name.strip() for name in raw.split(",")] if raw else []
    allowed = {"market-data", "equity", "fx", *_CALENDARS}
    if len(set(selected)) != len(selected) or not set(selected) <= allowed:
        msg = "PLATFORM_WORKERS must list unique, known workers"
        raise ValueError(msg)
    app_host = "127.0.0.1" if group == "all" else "application"
    for name in selected:
        if name in {"market-data", "equity"}:
            child = _market_environment(env, equity=name == "equity")
            specs.append(
                _spec(
                    name,
                    _MARKET_MODULE,
                    child,
                    8007 if name == "equity" else 8003,
                    argument="ingest",
                )
            )
        elif name == "fx":
            child = _environment(
                env,
                "market_data",
                frozenset(
                    {"FX_RATE_CURRENCIES", "FX_RATE_HISTORY_DAYS", "FX_RATE_POLL_INTERVAL_SEC"}
                ),
            )
            child["FX_RATE_COINBASE_PRODUCT"] = "USDC-EUR"
            specs.append(_spec(name, _MARKET_MODULE, child, 8004, argument="fx-rates"))
        else:
            specs.append(_calendar(name, env, app_host))
    if env.get("STRATEGY_LIST", "").strip():
        child = _environment(env, "indicator", _INDICATOR)
        child.update(
            {
                "API_KEY": _required(env, "SCORING_API_KEY"),
                "SIGNAL_API_URL": f"http://{app_host}:8001",
                "INDICATOR_ALLOW_DEV_DISCOVERY": "false",
                "HEALTH_CHECK_PORT": "8080",
                "PROMETHEUS_MULTIPROC_DIR": "/tmp/vynmatrix-prometheus/indicator",
                # Every strategy grandchild inherits this environment. A worker
                # has exactly two pool users (processing thread and delivery
                # loop) and one raw LISTEN connection, so bound it explicitly
                # instead of inheriting the 3+2 default of the API children.
                "DB_POOL_SIZE": "2",
                "DB_MAX_OVERFLOW": "0",
                "DB_POOL_CONNECTION_BUDGET": "2",
            }
        )
        specs.append(
            _spec(
                "indicator",
                "apps.indicator_runner.indicator_runner.main",
                child,
                8080,
                readiness_path="/health",
            )
        )
    return specs


def build_job(name: str, env: Mapping[str, str]) -> ProcessSpec:
    """Compose an explicit one-shot; the existing job validates its evidence gates."""
    _validate_runtime(env)
    if name == "backfill":
        child = _market_environment(env)
        command = "backfill"
    elif name == "quality-compounder":
        if env.get("QUALITY_COMPOUNDER_PANELS_ENABLED", "").lower() != "true":
            msg = "QUALITY_COMPOUNDER_PANELS_ENABLED must explicitly enable panel work"
            raise ValueError(msg)
        for key in (
            "EODHD_API_TOKEN",
            "EDGAR_USER_AGENT",
            "QUALITY_COMPOUNDER_ENTITLEMENT_OWNER_USER_ID",
            "QUALITY_COMPOUNDER_ROUND_TRIP_COMMISSION_BPS",
        ):
            _required(env, key)
        child = _environment(env, "market_data", _PANEL)
        command = "quality-compounder-once"
    else:
        msg = "Unknown bounded platform job"
        raise ValueError(msg)
    return _spec(name, _MARKET_MODULE, child, 0, argument=command)
