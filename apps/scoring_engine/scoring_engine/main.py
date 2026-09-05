"""
Entrypoint for running the scoring engine API.
"""

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import uvicorn
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from lib_application.db.session import get_session_factory
from lib_common.app import start_background_health_server
from lib_common.config_validation import (
    RunMode,
    ScoringEngineConfig,
    ScoringFactorConfig,
    ScoringMarketContextConfig,
    ScoringRelayConfig,
    load_scoring_engine_config,
)
from lib_common.env_utils import parse_int_env
from lib_common.health import add_health_endpoints
from lib_common.logging import get_logger, setup_logging
from lib_common.metrics import gauge
from lib_common.observability import init_tracing
from lib_common.paper_promotion import (
    PaperPromotionScope,
    load_paper_promotion_scope,
)
from lib_common.shutdown import get_shutdown_coordinator
from lib_common.task_supervision import supervise_background_task

from .api import create_app
from .dispatcher import ExecutionDispatcher
from .engine import ScoreEngine
from .outbox_relay import OutboxRelayWorker, outbox_progress_ready
from .pipeline import PipelineConfig, ScoringPipeline
from .providers_db import DBProfileProvider, DBStrategyConfigProvider
from .services.meta_label_service import (
    AssetClassRoutingMarketContextProvider,
    MarketContextProvider,
    MetaLabelService,
    PriceBasedMarketContextProvider,
    PriceObservation,
)
from .storage import AppScoreStore, InMemoryScoreStore, ScoreStore

logger = get_logger(__name__)


@dataclass
class RuntimeState:
    dispatcher: ExecutionDispatcher | None = None
    relay_worker: OutboxRelayWorker | None = None
    relay_task: asyncio.Task[None] | None = None


_runtime = RuntimeState()
_REQUIRED_OUTBOX_TOPICS = ("execution.commands", "execution.rebalance.commands")

# 1 while the inline outbox relay task is running, else 0. A dead inline relay
# stops ALL execution-command delivery, so alerting must see it (OB-1).
_RELAY_UP = gauge(
    "vm_scoring_outbox_relay_up",
    "1 while the inline outbox relay task is running, else 0",
)


def _relay_ready(
    store: ScoreStore | None = None,
    relay_config: ScoringRelayConfig | None = None,
) -> bool:
    """Execution-command relay liveness and durable progress for /ready.

    True when the inline relay is not enabled in this process (separate relay or
    disabled) or when its task is still running. When a durable store and relay
    config are provided, old or dead-lettered execution commands also make the
    service unready, including when delivery runs in a separate process.
    """
    task = _runtime.relay_task
    if task is not None and task.done():
        return False
    if store is None or relay_config is None:
        return True
    return outbox_progress_ready(
        store,
        topics=_REQUIRED_OUTBOX_TOPICS,
        max_backlog_age_seconds=relay_config.max_backlog_age_seconds,
    )


def _start_health_server(
    *,
    service_name: str,
    readiness_check: Callable[[], dict[str, bool]] | None = None,
) -> None:
    """Start a lightweight FastAPI server for container health probes."""
    app = FastAPI(title=f"{service_name} health", version="0.1.0")
    add_health_endpoints(
        app,
        custom_health_check=lambda: {"service": service_name},
        custom_readiness_check=readiness_check,
    )
    start_background_health_server(
        app=app,
        service_name=service_name,
        default_port=8080,
    )


def build_engine(config: ScoringEngineConfig) -> ScoreEngine:
    store = AppScoreStore(
        config.database.url,
        bindings_cache_ttl_seconds=config.runtime.bindings_cache_ttl_seconds,
    )
    weights = dict(config.scoring.score_weights or {})
    half_life = config.scoring.half_life_bars
    pcfg = PipelineConfig()
    meta_service = _build_market_context_meta_service(
        store,
        pcfg,
        settings=config.runtime.market_context,
        mode=config.mode,
    )
    factor_blender = _build_factor_blender(store, config.runtime.factors)
    pipeline = ScoringPipeline(
        config=pcfg,
        meta_service=meta_service,
        factor_blender=factor_blender,
    )
    promotion_scope, promotion_required = _load_paper_promotion_policy(config)
    engine = ScoreEngine(
        store=store,
        weights=weights,
        half_life_bars=half_life,
        pipeline=pipeline,
        runtime_config=config.runtime,
        paper_promotion_scope=promotion_scope,
        paper_promotion_required=promotion_required,
    )
    _warm_rolling_stats(store, pipeline)
    return engine


def _load_paper_promotion_policy(
    config: ScoringEngineConfig,
) -> tuple[PaperPromotionScope | None, bool]:
    """Resolve the immutable scoring-side paper authority at startup."""
    required = config.mode == RunMode.PAPER and config.runtime.environment in {
        "staging",
        "production",
    }
    if not required:
        return None, False

    manifest_path = config.runtime.paper_promotion_manifest
    image_tag = (config.runtime.deploy_image_tag or "").strip()
    if manifest_path is None or not image_tag:
        logger.error(
            "Cloud-paper binding evaluation is disabled: "
            "SCORING_PAPER_PROMOTION_MANIFEST and VM_DEPLOY_IMAGE_TAG are required",
        )
        return None, True

    scope, errors = load_paper_promotion_scope(
        manifest_path=manifest_path,
        deploy_image_tag=image_tag,
    )
    if scope is None:
        logger.error(
            "Cloud-paper binding evaluation is disabled: %s",
            "; ".join(errors),
        )
        return None, True
    logger.info(
        "Loaded exact cloud-paper scoring scope strategy=%s version=%s "
        "model_scope=%s instrument_set_sha256=%s user=%s binding=%s account=%s",
        scope.strategy_id,
        scope.strategy_version,
        scope.model_scope,
        scope.instrument_set_sha256,
        scope.user_id,
        scope.strategy_binding_id,
        scope.broker_account_id,
    )
    return scope, True


def _build_factor_blender(
    store: AppScoreStore | InMemoryScoreStore,
    settings: ScoringFactorConfig,
) -> Any:
    """Construct the multi-factor blender (Q3), or None when the feature is off.

    Default-OFF: returns None unless ``SCORING_MULTI_FACTOR_ENABLED`` is true, so
    the scoring path is byte-identical (no blender is constructed and the ensemble
    takes the single-alpha branch). When on, factors are computed from the
    ``prices`` history via its closes loader; ``SCORING_FACTOR_WEIGHTS`` (csv
    ``name:weight``, default
    equal-weight), ``SCORING_FACTOR_LOOKBACK`` and ``SCORING_FACTOR_WINSORIZE_Z``
    tune the blend. NOTE: the price-derived factors are only meaningful when the
    ``prices`` table is populated upstream by the market-data ingestor; they read
    ``prices`` independently from the mandatory point-in-time context provider.
    """
    if not settings.enabled:
        return None
    from .factors import (  # noqa: PLC0415 - only import when enabled
        DEFAULT_FACTORS,
        FactorBlender,
    )

    factor_weights = dict(settings.weights)
    unknown_factors = set(factor_weights) - set(DEFAULT_FACTORS)
    if unknown_factors:
        msg = (
            "SCORING_FACTOR_WEIGHTS contains unsupported factor names: "
            f"{', '.join(sorted(unknown_factors))}"
        )
        raise ValueError(msg)
    blender = FactorBlender(
        closes_provider=_make_closes_loader(store),
        weights=factor_weights,
        lookback=settings.lookback,
        winsorize_z=settings.winsorize_z,
    )
    logger.info("Multi-factor scoring ENABLED (lookback=%d)", blender._lookback)
    return blender


def _build_market_context_meta_service(
    store: AppScoreStore | InMemoryScoreStore,
    pcfg: PipelineConfig,
    *,
    settings: ScoringMarketContextConfig,
    mode: RunMode,
) -> MetaLabelService:
    """Build the fail-closed production market-context path from persisted prices."""
    if mode == RunMode.LIVE:
        msg = (
            "DeterministicMetaScorer is uncalibrated and approved only for "
            "paper/backtest validation; live scoring is disabled"
        )
        raise RuntimeError(msg)
    default_provider = PriceBasedMarketContextProvider(
        _make_price_history_loader(
            store,
            source=settings.source,
            timeframe=settings.timeframe,
        ),
        window=settings.window,
        max_age=timedelta(seconds=settings.max_age_seconds),
    )
    logger.info(
        "Scoring market-context provider: persisted_prices "
        "(window=%d max_age_seconds=%d source=%s timeframe=%s)",
        settings.window,
        settings.max_age_seconds,
        settings.source,
        settings.timeframe,
    )
    provider: MarketContextProvider = default_provider
    if settings.by_asset_class:
        class_providers: dict[str, MarketContextProvider] = {}
        for asset_class, class_settings in settings.by_asset_class:
            class_providers[asset_class] = PriceBasedMarketContextProvider(
                _make_price_history_loader(
                    store,
                    source=class_settings.source,
                    timeframe=class_settings.timeframe,
                ),
                window=class_settings.window,
                max_age=timedelta(seconds=class_settings.max_age_seconds),
            )
            logger.info(
                "Scoring market-context override: asset_class=%s "
                "(window=%d max_age_seconds=%d source=%s timeframe=%s)",
                asset_class,
                class_settings.window,
                class_settings.max_age_seconds,
                class_settings.source,
                class_settings.timeframe,
            )
        provider = AssetClassRoutingMarketContextProvider(
            default=default_provider,
            by_asset_class=class_providers,
            asset_class_resolver=store.resolve_instrument_asset_class,
        )
    return MetaLabelService(context_provider=provider, config=pcfg.to_meta_config())


def _make_price_history_loader(
    store: AppScoreStore | InMemoryScoreStore,
    *,
    source: str,
    timeframe: str,
) -> Callable[[str, int, datetime], list[PriceObservation]]:
    """Load point-in-time close observations oldest→newest.

    Source and timeframe are explicit so parallel broker feeds or bar cadences
    cannot be silently mixed. ``as_of`` prevents historical replay look-ahead.
    Storage failures return no observations, which the provider treats as an
    unavailable context and therefore a non-actionable score.
    """

    def _load(asset: str, limit: int, as_of: datetime) -> list[PriceObservation]:
        try:
            session = store.get_session()
            if session is None:
                return []
            try:
                instr_id = store.resolve_instrument_id(asset)
                if instr_id is None:
                    return []
                cutoff = as_of.astimezone(UTC).replace(tzinfo=None)
                rows = session.execute(
                    text(
                        """
                        SELECT ts, close
                        FROM prices
                        WHERE instr_id = :i
                          AND source = :source
                          AND timeframe = :timeframe
                          AND ts <= :as_of
                        ORDER BY ts DESC
                        LIMIT :n
                        """
                    ),
                    {
                        "i": instr_id,
                        "source": source,
                        "timeframe": timeframe,
                        "as_of": cutoff,
                        "n": limit,
                    },
                ).fetchall()
                return [
                    PriceObservation(
                        timestamp=(
                            row[0].replace(tzinfo=UTC)
                            if row[0].tzinfo is None
                            else row[0].astimezone(UTC)
                        ),
                        close=float(row[1]),
                    )
                    for row in reversed(rows)
                ]
            finally:
                session.close()
        except (SQLAlchemyError, OSError, TypeError, ValueError):
            logger.warning(
                "Price-history loader query failed for %s source=%s timeframe=%s",
                asset,
                source,
                timeframe,
            )
            return []

    return _load


def _make_closes_loader(
    store: AppScoreStore | InMemoryScoreStore,
) -> Callable[[str, int], list[float]]:
    """Closes loader reading the ``prices`` table via the store, oldest→newest.

    This loader is used only by optional factor blending. Returns ``[]`` when
    the store has no DB or price history so the optional factor contributes
    nothing; Layer-2 context uses the separate fail-closed point-in-time loader.
    """

    def _load(asset: str, limit: int) -> list[float]:
        # Wrap the WHOLE body (incl. get_session) so the loader honors its
        # "returns []" contract for connection-level failures too, not just query
        # errors — the optional factor blender relies on it never raising.
        try:
            session = store.get_session()
            if session is None:
                return []
            try:
                instr_id = store.resolve_instrument_id(asset)
                if instr_id is None:
                    return []
                rows = session.execute(
                    text("SELECT close FROM prices WHERE instr_id = :i ORDER BY ts DESC LIMIT :n"),
                    {"i": instr_id, "n": limit},
                ).fetchall()
                return [float(r[0]) for r in reversed(rows)]
            finally:
                session.close()
        except (SQLAlchemyError, OSError):
            logger.warning("Closes loader query failed for %s", asset)
            return []

    return _load


def _warm_rolling_stats(
    store: AppScoreStore | InMemoryScoreStore, pipeline: ScoringPipeline
) -> None:
    """Warm Layer-3 rolling standardization from persisted history on boot.

    Makes the standardized asset score reproducible across restarts instead of
    resetting to the cold-start default. No-op on a fresh DB / in-memory store.
    """
    window = getattr(pipeline.ensemble_service, "_rolling_window", 100)
    try:
        history = store.recent_asset_alpha_history(window)
    except SQLAlchemyError:
        logger.exception("Failed to load alpha history; scoring starts cold")
        return
    if history:
        applied = pipeline.ensemble_service.warm(history)
        # When multi-factor scoring is enabled, the live path standardizes the
        # composite in the ``mf:<asset>`` namespace, so warm THAT key too — from the
        # same single-alpha history (the composite differs only by the additive
        # factor_alpha, a far better warm-start than the cold std=0.1). Otherwise
        # enabling the flag would re-incur a ~10x first-window amplification on
        # every restart. ``warm()`` treats each dict key as the stats key.
        if getattr(pipeline.ensemble_service, "_factor_blender", None) is not None:
            pipeline.ensemble_service.warm(
                {f"mf:{asset}": values for asset, values in history.items()}
            )
        logger.info(
            "Warmed scoring rolling stats from history: assets=%d values=%d",
            len(history),
            applied,
        )


async def cleanup_dispatcher() -> None:
    """Clean up dispatcher HTTP client on shutdown."""
    if _runtime.relay_task is not None:
        _runtime.relay_task.cancel()
        await asyncio.gather(_runtime.relay_task, return_exceptions=True)
        _runtime.relay_task = None
    if _runtime.relay_worker is not None:
        logger.info("Closing scoring outbox relay")
        await _runtime.relay_worker.aclose()
        _runtime.relay_worker = None
    if _runtime.dispatcher:
        logger.info("Closing dispatcher")
        await _runtime.dispatcher.aclose()
        _runtime.dispatcher = None


def _make_lifespan(
    store: ScoreStore,
    exec_url: str | None,
    *,
    relay_config: ScoringRelayConfig,
    exec_engine_api_key: str | None,
    notify_dsn: str | None,
) -> object:
    """Create lifespan context manager for FastAPI with proper cleanup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """FastAPI lifespan for startup and shutdown handling."""
        # Startup
        logger.info("Scoring engine starting up")

        # Setup graceful shutdown coordinator
        shutdown = get_shutdown_coordinator(timeout_seconds=30)
        # NOTE: Do NOT call register_signals() here - uvicorn handles SIGTERM/SIGINT
        # and registering our own handlers would override uvicorn's graceful shutdown
        shutdown.on_shutdown(cleanup_dispatcher)

        if exec_url and relay_config.inline:
            _start_inline_relay(
                store,
                exec_url,
                relay_config=relay_config,
                exec_engine_api_key=exec_engine_api_key,
                notify_dsn=notify_dsn,
            )

        # Store references in app state
        app.state.shutdown = shutdown

        logger.info("Scoring engine ready")

        yield

        # Shutdown - uvicorn triggers this on SIGTERM/SIGINT
        logger.info("Scoring engine shutting down")
        await shutdown.execute()
        logger.info("Scoring engine shutdown complete")

    return lifespan


async def _run_relay_worker(
    store: ScoreStore,
    exec_url: str,
    *,
    relay_config: ScoringRelayConfig,
    exec_engine_api_key: str | None,
    notify_dsn: str | None,
) -> None:
    worker = OutboxRelayWorker(
        store=store,
        exec_engine_url=exec_url,
        api_key=exec_engine_api_key,
        notify_dsn=notify_dsn,
    )
    try:
        await worker.run_forever(
            topics=relay_config.topics,
            poll_interval_seconds=relay_config.poll_interval_seconds,
            limit=relay_config.batch_size,
            lease_seconds=relay_config.lease_seconds,
        )
    finally:
        await worker.aclose()


def _start_inline_relay(
    store: ScoreStore,
    exec_url: str,
    *,
    relay_config: ScoringRelayConfig,
    exec_engine_api_key: str | None,
    notify_dsn: str | None,
) -> OutboxRelayWorker:
    _runtime.relay_worker = OutboxRelayWorker(
        store=store,
        exec_engine_url=exec_url,
        api_key=exec_engine_api_key,
        notify_dsn=notify_dsn,
    )
    _runtime.relay_task = asyncio.create_task(
        _runtime.relay_worker.run_forever(
            topics=relay_config.topics,
            poll_interval_seconds=relay_config.poll_interval_seconds,
            limit=relay_config.batch_size,
            lease_seconds=relay_config.lease_seconds,
        ),
        name="scoring-outbox-relay",
    )
    # A dead inline relay stops ALL execution-command delivery while /health
    # stays green, so alerting must see it (OB-1).
    supervise_background_task(
        _runtime.relay_task,
        name="Scoring outbox relay task",
        up_gauge=_RELAY_UP,
    )
    return _runtime.relay_worker


def main() -> None:
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    command = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "api"
    # No-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set + otel deps installed (G7).
    init_tracing("scoring-outbox-relay" if command == "relay" else "scoring-engine")

    config = load_scoring_engine_config()
    db_url = config.database.url
    engine = build_engine(config)
    exec_url = config.execution_engine_url
    exec_engine_api_key = os.getenv("EXEC_ENGINE_API_KEY") or os.getenv("API_KEY")
    # The relay listens as the scoring login on the existing outbox_events
    # trigger; LISTEN needs no extra privilege. Polling stays as recovery.
    notify_dsn = db_url if config.runtime.relay.notify_enabled else None

    # Canonical session factory — applies environment-aware pool sizing and
    # ``pool_pre_ping`` semantics in one place.
    session_factory = get_session_factory(db_url=db_url)

    profile_provider = DBProfileProvider(session_factory)
    strat_cfg_provider = DBStrategyConfigProvider(session_factory)

    _runtime.dispatcher = (
        ExecutionDispatcher(
            exec_url,
            profile_provider=profile_provider,
            strategy_config_provider=strat_cfg_provider,
            store=engine.store,
            runtime_mode=config.mode.value,
        )
        if exec_url
        else None
    )

    if command == "relay":
        if not exec_url:
            msg = "EXEC_ENGINE_URL is required for scoring relay mode"
            raise RuntimeError(msg)
        logger.info("Starting scoring outbox relay", extra={"exec_engine_url": exec_url})
        _start_health_server(
            service_name="scoring-outbox-relay",
            readiness_check=lambda: {
                "outbox_progress": _relay_ready(engine.store, config.runtime.relay),
            },
        )
        asyncio.run(
            _run_relay_worker(
                engine.store,
                exec_url,
                relay_config=config.runtime.relay,
                exec_engine_api_key=exec_engine_api_key,
                notify_dsn=notify_dsn,
            )
        )
        return

    # Create lifespan for proper shutdown handling
    lifespan = _make_lifespan(
        engine.store,
        exec_url,
        relay_config=config.runtime.relay,
        exec_engine_api_key=exec_engine_api_key,
        notify_dsn=notify_dsn,
    )

    app = create_app(
        engine,
        dispatcher=_runtime.dispatcher,
        user_profiles={},
        user_strategy_configs={},
        lifespan=lifespan,
        relay_readiness=lambda: _relay_ready(engine.store, config.runtime.relay),
    )

    host = os.getenv("HOST", "0.0.0.0")
    port = parse_int_env("PORT", default=8001, min_value=1, max_value=65535, logger=logger)

    logger.info(
        "Scoring engine starting",
        extra={"host": host, "port": port, "exec_engine_url": exec_url},
    )

    # Run with uvicorn
    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
