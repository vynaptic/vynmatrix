"""Main entry point for the Market Data Ingestor service.

Usage:
    python -m market_data_ingestor          # default: run scheduler
    python -m market_data_ingestor ingest   # run scheduler with health server
    python -m market_data_ingestor fx-rates # run observed FX scheduler
    python -m market_data_ingestor market-calendars # official schedule writer
    python -m market_data_ingestor equity-catalogue-import # reviewed equity catalogue
"""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy.orm import sessionmaker

from lib_application.db.session import (
    create_engine_for_env,
    dispose_engine,
    get_session_factory,
)
from lib_application.services.price_ingestion_service import PriceIngestionService
from lib_common.app import start_background_health_server
from lib_common.env_utils import (
    build_database_url,
    parse_bool_env,
    parse_float_env,
    parse_int_env,
    parse_list_env,
)
from lib_common.logging import get_logger, setup_logging
from lib_data.market_data import MarketDataInstrument, normalize_product_symbol

from .health import create_health_app
from .scheduler import (
    EODHD_DAILY_QUOTA_MIN_COOLDOWN_DEFAULT_SEC,
    EODHD_DAILY_QUOTA_MIN_COOLDOWN_MAX_SEC,
    EODHD_DAILY_QUOTA_MIN_COOLDOWN_MIN_SEC,
    IngestionScheduler,
)

if TYPE_CHECKING:
    from .fx_rates import FXRateIngestor
    from .market_calendar_sync import MarketCalendarSyncWorker

logger = get_logger(__name__)

_SOURCE_BROKER_CODES = {
    "coinbase_live": "coinbase",
    "deribit": "deribit",
    "deribit_testnet": "deribit",
    "delta": "delta",
    "delta_india": "delta",
    "ibkr": "ibkr",
    "saxo_live": "saxo",
    "saxo_simulation": "saxo",
    "zerodha": "zerodha",
}
_TYPED_NUMERIC_IDENTIFIER_SOURCES = {
    "ibkr",
    "saxo_live",
    "saxo_simulation",
    "zerodha",
}
_TYPED_INSTRUMENT_TYPE_SOURCES = {"saxo_live", "saxo_simulation"}


class _ProviderCredentials(TypedDict):
    api_key: str | None
    api_secret: str | None
    access_token: str | None
    access_token_expires_at: str | None
    account_key: str | None


def _get_symbols() -> list[str]:
    raw = os.environ.get("INGESTOR_SYMBOLS", "BTC-USDC,ETH-USDC,SOL-USDC")
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _get_granularity() -> str:
    return os.environ.get("INGESTOR_GRANULARITY", "ONE_MINUTE").strip().upper()


def _get_source() -> str:
    return os.environ.get("INGESTOR_SOURCE", "coinbase_live").strip()


def _get_vendor_equity_instruments(
    session_factory: sessionmaker,
    *,
    symbols: list[str] | None = None,
) -> list[MarketDataInstrument]:
    """Resolve equity selectors against the instruments catalogue (no broker).

    EODHD is a data vendor, not a broker: its US tickers ARE the canonical
    equity symbols, so ingestion identity comes straight from the catalogue.
    Unknown or non-equity selectors fail startup — never guess instrument ids.
    """
    selectors = [s.strip().upper() for s in (symbols or _get_symbols()) if s.strip()]
    if not selectors:
        msg = "Market-data ingestion requires at least one instrument selector"
        raise ValueError(msg)
    instrument_map = PriceIngestionService(session_factory).load_instrument_map(
        asset_class="equity"
    )
    unknown = sorted(
        selector
        for selector in selectors
        if normalize_product_symbol(selector) not in instrument_map
    )
    if unknown:
        msg = (
            f"No equity instrument rows for: {', '.join(unknown)} — seed the "
            "instruments catalogue before ingesting (fail closed, never guess ids)"
        )
        raise ValueError(msg)
    return [
        MarketDataInstrument(
            canonical_symbol=selector,
            product_id=selector,
            asset_class="equity",
        )
        for selector in selectors
    ]


def _get_ingestion_instruments(
    session_factory: sessionmaker,
    source: str,
    *,
    symbols: list[str] | None = None,
) -> list[MarketDataInstrument]:
    """Load canonical-to-venue mappings from the broker instrument catalogue."""
    if source == "eodhd":
        return _get_vendor_equity_instruments(session_factory, symbols=symbols)
    try:
        broker_code = _SOURCE_BROKER_CODES[source]
    except KeyError as exc:
        supported = ", ".join(sorted([*_SOURCE_BROKER_CODES, "eodhd"]))
        msg = f"Unknown market-data source: {source!r}; expected one of: {supported}"
        raise ValueError(msg) from exc

    typed_identifier = source in _TYPED_NUMERIC_IDENTIFIER_SOURCES
    return PriceIngestionService(session_factory).load_market_data_instruments(
        broker_code=broker_code,
        symbols=symbols if symbols is not None else _get_symbols(),
        identifier_field=("broker_instrument_id" if typed_identifier else "broker_symbol"),
        require_numeric_identifier=typed_identifier,
        require_instrument_type=source in _TYPED_INSTRUMENT_TYPE_SOURCES,
    )


def _get_provider_credentials(source: str) -> _ProviderCredentials:
    """Return only the dedicated feed credentials required by ``source``."""
    credentials: _ProviderCredentials = {
        "api_key": None,
        "api_secret": None,
        "access_token": None,
        "access_token_expires_at": None,
        "account_key": None,
    }
    if source == "coinbase_live":
        credentials["api_key"] = os.environ.get("COINBASE_API_KEY")
        credentials["api_secret"] = os.environ.get("COINBASE_API_SECRET")
    elif source == "eodhd":
        credentials["api_key"] = os.environ.get("EODHD_API_TOKEN")
    elif source == "zerodha":
        credentials["api_key"] = os.environ.get("ZERODHA_MARKET_DATA_API_KEY")
        credentials["access_token"] = os.environ.get("ZERODHA_MARKET_DATA_ACCESS_TOKEN")
    elif source in {"saxo_live", "saxo_simulation"}:
        credentials["access_token"] = os.environ.get("SAXO_MARKET_DATA_ACCESS_TOKEN")
        credentials["access_token_expires_at"] = os.environ.get(
            "SAXO_MARKET_DATA_ACCESS_TOKEN_EXPIRES_AT"
        )
        credentials["account_key"] = os.environ.get("SAXO_MARKET_DATA_ACCOUNT_KEY")
    return credentials


def _get_poll_interval() -> int:
    return parse_int_env("INGESTOR_POLL_INTERVAL_SEC", default=60, min_value=1, logger=logger)


def _get_candle_poll_interval() -> int:
    poll_interval = _get_poll_interval()
    return parse_int_env(
        "INGESTOR_CANDLE_POLL_INTERVAL_SEC",
        default=poll_interval,
        min_value=poll_interval,
        logger=logger,
        strict=True,
    )


def _get_backfill_minutes() -> int:
    return parse_int_env("INGESTOR_BACKFILL_MINUTES", default=180, min_value=0, logger=logger)


def _get_backfill_days() -> int:
    """Days of history requested by the separate one-shot backfill command.

    A value of 0 disables the days-scale one-shot. The live ingestor still owns
    only its short minute-window startup request and continuous polling.
    """
    return parse_int_env("INGESTOR_BACKFILL_DAYS", default=0, min_value=0, logger=logger)


def _get_notify_channel() -> str:
    return os.environ.get("INGESTOR_NOTIFY_CHANNEL", "new_market_data").strip()


def _get_fx_currencies() -> set[str]:
    return {
        currency.strip().upper()
        for currency in parse_list_env(
            "FX_RATE_CURRENCIES",
            default=["USD", "GBP", "INR"],
        )
        if currency.strip()
    }


def _get_fx_history_days() -> int:
    return parse_int_env(
        "FX_RATE_HISTORY_DAYS",
        default=90,
        min_value=1,
        max_value=366,
        logger=logger,
    )


def _get_fx_poll_interval() -> int:
    return parse_int_env(
        "FX_RATE_POLL_INTERVAL_SEC",
        default=21600,
        min_value=60,
        max_value=86400,
        logger=logger,
    )


def _get_fx_coinbase_product() -> str:
    return os.environ.get("FX_RATE_COINBASE_PRODUCT", "USDC-EUR").strip().upper()


def _build_scheduler(session_factory: sessionmaker) -> IngestionScheduler:
    source = _get_source()
    return IngestionScheduler(
        session_factory=session_factory,
        instruments=_get_ingestion_instruments(session_factory, source),
        granularity=_get_granularity(),
        source=source,
        poll_interval_sec=_get_poll_interval(),
        candle_poll_interval_sec=_get_candle_poll_interval(),
        startup_backfill_minutes=_get_backfill_minutes(),
        **_get_provider_credentials(source),
        notify_channel=_get_notify_channel(),
        staleness_threshold_sec=_get_staleness_threshold(),
        delayed_quote_owner_user_id=_get_delayed_quote_owner(source),
        eodhd_daily_quota_min_cooldown_sec=(
            _get_eodhd_daily_quota_min_cooldown()
            if source == "eodhd"
            else EODHD_DAILY_QUOTA_MIN_COOLDOWN_DEFAULT_SEC
        ),
    )


def _get_delayed_quote_owner(source: str) -> str | None:
    enabled = parse_bool_env(
        "EODHD_DELAYED_QUOTES_ENABLED",
        default=False,
        logger=logger,
        strict=True,
    )
    if not enabled:
        return None
    if source != "eodhd":
        msg = "EODHD delayed quotes can be enabled only on the EODHD equity ingestor"
        raise ValueError(msg)
    owner = os.environ.get("SP500_RESEARCH_OWNER_USER_ID", "").strip()
    if not owner:
        msg = (
            "EODHD delayed quotes require SP500_RESEARCH_OWNER_USER_ID for exact "
            "personal-use ownership"
        )
        raise ValueError(msg)
    return owner


def _get_eodhd_daily_quota_min_cooldown() -> int:
    """Minimum pause around EODHD's documented midnight-UTC daily reset."""

    return parse_int_env(
        "EODHD_DAILY_QUOTA_MIN_COOLDOWN_SEC",
        default=EODHD_DAILY_QUOTA_MIN_COOLDOWN_DEFAULT_SEC,
        min_value=EODHD_DAILY_QUOTA_MIN_COOLDOWN_MIN_SEC,
        max_value=EODHD_DAILY_QUOTA_MIN_COOLDOWN_MAX_SEC,
        logger=logger,
        strict=True,
    )


def _get_staleness_threshold() -> int | None:
    raw = os.environ.get("INGESTOR_STALENESS_THRESHOLD_SEC")
    if not raw:
        return None
    return parse_int_env(
        "INGESTOR_STALENESS_THRESHOLD_SEC",
        default=0,
        min_value=1,
        logger=logger,
    )


def _start_health_server(scheduler: IngestionScheduler) -> None:
    """Start a background health server whose /ready reflects feed freshness."""
    app = create_health_app(
        readiness_check=lambda: {
            "market_data_fresh": scheduler.is_fresh(),
            "provider_available": scheduler.is_provider_ready(),
        }
    )
    start_background_health_server(
        app=app,
        service_name="market-data-ingestor",
        default_port=8003,
    )


def _run_startup_backfill(session_factory: sessionmaker) -> None:
    """Page one-shot history into ``prices`` for daily/swing warmup.

    This helper is called only by :func:`run_backfill`, never by the live poll
    loop. It is deficit-aware and is a no-op when ``INGESTOR_BACKFILL_DAYS`` is
    0.
    """
    days = _get_backfill_days()
    if days <= 0:
        return
    from .backfill import HistoricalBackfiller  # noqa: PLC0415

    source = _get_source()
    instruments = _get_ingestion_instruments(session_factory, source)
    backfiller = HistoricalBackfiller(
        session_factory=session_factory,
        instruments=instruments,
        granularity=_get_granularity(),
        source=source,
        **_get_provider_credentials(source),
    )
    logger.info("One-shot warmup: ensuring %d days of historical data", days)
    try:
        backfiller.ensure_history(days=days)
        # Scheduled-market reference data rides the same one-shot: refresh
        # splits/dividends over the warmup window whenever the configured
        # provider exposes them (idempotent, and independent of price skips).
        if backfiller.corporate_actions_supported:
            action_end = datetime.now(tz=UTC).date()
            action_start = action_end - timedelta(days=days)
            inserted = backfiller.load_corporate_actions(start=action_start, end=action_end)
            logger.info("Corporate actions loaded: %s", inserted)
    finally:
        backfiller.close()


def run_ingestor() -> None:
    """Run the ingestion scheduler with a background health server."""
    import signal as _signal  # noqa: PLC0415

    db_url = build_database_url()
    engine = create_engine_for_env(db_url=db_url)
    session_local = get_session_factory(engine=engine)

    scheduler = _build_scheduler(session_local)
    _start_health_server(scheduler)

    # NOTE: historical auto-warm backfill is intentionally NOT run in-process.
    # It used to run as a daemon thread here, but paging months of history shares
    # this process's DB connection pool + venue provider/rate limit with the live
    # poll loop and could starve it (the feed silently stopped writing). Warmup for
    # daily/swing strategies now runs as a SEPARATE one-shot process
    # (``python -m market_data_ingestor.main backfill``, wired as the profile-gated
    # ``market-data-backfill`` compose service) so it can never stall the live feed.
    def _handle_shutdown(signum: int, _frame: object) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        scheduler.stop()

    _signal.signal(_signal.SIGTERM, _handle_shutdown)
    _signal.signal(_signal.SIGINT, _handle_shutdown)

    logger.info(
        "Starting market data ingestor",
        symbols=_get_symbols(),
        granularity=_get_granularity(),
        source=_get_source(),
        poll_interval=_get_poll_interval(),
        candle_poll_interval=_get_candle_poll_interval(),
    )

    # Run in foreground (blocks until stopped)
    try:
        scheduler.run_forever()
    finally:
        scheduler.stop()
        dispose_engine(engine)

    logger.info("Market data ingestor shutdown complete")


def run_backfill() -> None:
    """Run the deficit-aware historical backfill once, then exit.

    Runs as a SEPARATE one-shot process with its own DB engine/pool and
    source-specific provider, so warming daily/swing strategy history at deploy
    can never contend with — or stall — the live ingestor's poll loop (which is
    a different process). Deficit-aware, so a re-run fetches only missing
    history. No-op when ``INGESTOR_BACKFILL_DAYS`` <= 0.
    """
    db_url = build_database_url()
    engine = create_engine_for_env(db_url=db_url)
    session_local = get_session_factory(engine=engine)
    logger.info("Running one-shot historical backfill", days=_get_backfill_days())
    try:
        _run_startup_backfill(session_local)
    finally:
        dispose_engine(engine)
    logger.info("One-shot historical backfill complete")


def _build_fx_ingestor(session_factory: sessionmaker) -> FXRateIngestor:
    from .fx_rates import FXRateIngestor  # noqa: PLC0415

    return FXRateIngestor(
        session_factory=session_factory,
        quote_currencies=_get_fx_currencies(),
        history_days=_get_fx_history_days(),
        poll_interval_sec=_get_fx_poll_interval(),
        coinbase_product=_get_fx_coinbase_product(),
    )


def run_fx_rates(*, once: bool = False) -> None:
    """Run the isolated observed-rate process, optionally as a one-shot sync."""
    import signal as _signal  # noqa: PLC0415

    db_url = build_database_url()
    engine = create_engine_for_env(db_url=db_url)
    session_local = get_session_factory(engine=engine)
    ingestor = _build_fx_ingestor(session_local)

    def _handle_shutdown(signum: int, _frame: object) -> None:
        logger.info("Received signal %d, stopping FX-rate ingestion", signum)
        ingestor.stop()

    _signal.signal(_signal.SIGTERM, _handle_shutdown)
    _signal.signal(_signal.SIGINT, _handle_shutdown)

    if not once:
        app = create_health_app(
            readiness_check=lambda: {"fx_observations_fresh": ingestor.is_fresh()}
        )
        start_background_health_server(
            app=app,
            service_name="fx-rate-ingestor",
            default_port=8004,
        )
    logger.info(
        "Starting observed FX-rate ingestion",
        currencies=sorted(_get_fx_currencies()),
        history_days=_get_fx_history_days(),
        coinbase_product=_get_fx_coinbase_product(),
        once=once,
    )
    try:
        if once:
            ingestor.run_once()
        else:
            ingestor.run_forever()
    finally:
        ingestor.stop()
        dispose_engine(engine)
    logger.info("Observed FX-rate ingestion shutdown complete")


def _build_market_calendar_worker(
    session_factory: sessionmaker,
) -> MarketCalendarSyncWorker:
    from .market_calendar_sync import (  # noqa: PLC0415
        BackendMarketCalendarClient,
        MarketCalendarSyncWorker,
        build_official_market_calendar_provider,
    )

    api_client = BackendMarketCalendarClient(
        base_url=os.environ.get("MARKET_CALENDAR_API_URL", "http://backend:8081"),
        admin_api_key=os.environ.get("MARKET_CALENDAR_ADMIN_API_KEY"),
    )
    with ExitStack() as startup:
        startup.callback(api_client.close)
        provider_name = os.environ.get("MARKET_CALENDAR_PROVIDER", "").strip()
        provider = build_official_market_calendar_provider(
            provider=provider_name,
            ibkr_gateway_url=os.environ.get("IBKR_GATEWAY_URL"),
            saxo_access_token=os.environ.get("SAXO_MARKET_DATA_ACCESS_TOKEN"),
            saxo_access_token_expires_at=os.environ.get("SAXO_MARKET_DATA_ACCESS_TOKEN_EXPIRES_AT"),
            nse_market=os.environ.get("MARKET_CALENDAR_NSE_MARKET"),
            nse_lease_seconds=parse_int_env(
                "MARKET_CALENDAR_NSE_LEASE_SECONDS",
                default=120,
                min_value=30,
                max_value=300,
                logger=logger,
            ),
        )
        startup.callback(provider.close)
        worker = MarketCalendarSyncWorker(
            session_factory=session_factory,
            provider=provider,
            symbols=parse_list_env("MARKET_CALENDAR_SYMBOLS", default=[]),
            poll_interval_sec=parse_int_env(
                "MARKET_CALENDAR_POLL_INTERVAL_SEC",
                default=300,
                min_value=15,
                max_value=1800,
                logger=logger,
            ),
            api_client=api_client,
        )
        startup.pop_all()
        return worker


def run_market_calendars(*, once: bool = False) -> None:
    """Run the supervised official schedule writer."""
    import signal as _signal  # noqa: PLC0415

    db_url = build_database_url()
    engine = create_engine_for_env(db_url=db_url)
    with ExitStack() as startup:
        startup.callback(dispose_engine, engine)
        session_local = get_session_factory(engine=engine)
        worker = _build_market_calendar_worker(session_local)
        startup.pop_all()

    def _handle_shutdown(signum: int, _frame: object) -> None:
        logger.info("Received signal %d, stopping market-calendar sync", signum)
        worker.stop()

    try:
        _signal.signal(_signal.SIGTERM, _handle_shutdown)
        _signal.signal(_signal.SIGINT, _handle_shutdown)
        if not once:
            app = create_health_app(
                readiness_check=lambda: {"official_market_calendars_fresh": worker.is_fresh()}
            )
            start_background_health_server(
                app=app,
                service_name="market-calendar-sync",
                default_port=8005,
            )
        if once:
            worker.run_once()
        else:
            worker.run_forever()
    finally:
        worker.stop()
        dispose_engine(engine)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        msg = f"{name} is required"
        raise ValueError(msg)
    return value


def run_quality_compounder_once() -> None:
    """Run the default-off, quarter-end US Quality Compounder panel job once."""

    enabled = parse_bool_env(
        "QUALITY_COMPOUNDER_PANELS_ENABLED",
        default=False,
        logger=logger,
        strict=True,
    )
    if not enabled:
        logger.info("US Quality Compounder panel production is disabled")
        return

    from lib_infrastructure.market_data.eodhd_client import EODHDClient  # noqa: PLC0415
    from lib_strategy.equity_market_factors import EquityMarketFactorPolicy  # noqa: PLC0415
    from lib_strategy.equity_transaction_costs import DailyBarCostModelPolicy  # noqa: PLC0415

    from .quality_compounder_market import (  # noqa: PLC0415
        QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY,
    )
    from .quality_compounder_producer import QualityCompounderPanelProducer  # noqa: PLC0415
    from .quality_compounder_quarterly import QualityCompounderQuarterlyJob  # noqa: PLC0415
    from .sec_edgar import SecEdgarClient, SecEdgarClientConfig  # noqa: PLC0415

    api_token = _required_env("EODHD_API_TOKEN")
    edgar_user_agent = _required_env("EDGAR_USER_AGENT")
    entitlement_owner = _required_env("QUALITY_COMPOUNDER_ENTITLEMENT_OWNER_USER_ID")
    _required_env("QUALITY_COMPOUNDER_ROUND_TRIP_COMMISSION_BPS")
    commission_bps = parse_float_env(
        "QUALITY_COMPOUNDER_ROUND_TRIP_COMMISSION_BPS",
        min_value=0.0,
        max_value=100.0,
        logger=logger,
        strict=True,
    )

    cost_policy = DailyBarCostModelPolicy()
    market_policy = EquityMarketFactorPolicy(
        round_trip_commission_bps=commission_bps,
        cost_context_sha256=cost_policy.configuration_sha256,
        required_adjustment_policy=QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY,
    )
    db_url = build_database_url()
    engine = create_engine_for_env(db_url=db_url)
    eodhd = EODHDClient(api_token)
    sec = SecEdgarClient(SecEdgarClientConfig(user_agent=edgar_user_agent))
    try:
        session_local = get_session_factory(engine=engine)
        result = QualityCompounderQuarterlyJob(
            session_factory=session_local,
            producer=QualityCompounderPanelProducer(
                session_factory=session_local,
                eodhd_client=eodhd,
                sec_client=sec,
                entitlement_owner_user_id=entitlement_owner,
                market_policy=market_policy,
                cost_policy=cost_policy,
            ),
            enabled=True,
        ).run_once()
        logger.info(
            "US Quality Compounder quarter-end check complete",
            status=result.status.value,
            decision_session=(
                result.decision_session.isoformat() if result.decision_session is not None else None
            ),
            input_sha256=result.input_sha256,
        )
    finally:
        sec.close()
        eodhd.close()
        dispose_engine(engine)


def run_quality_compounder_calendar_import() -> None:
    """Import one content-pinned official XNYS compiler artifact."""

    from .quality_compounder_calendar import (  # noqa: PLC0415
        QUALITY_COMPOUNDER_CALENDAR_MAX_ARTIFACT_BYTES,
        load_quality_compounder_calendar_artifact,
        persist_quality_compounder_calendar,
    )

    artifact_path = Path(_required_env("QUALITY_COMPOUNDER_OFFICIAL_SESSION_ARTIFACT")).resolve(
        strict=True
    )
    if not artifact_path.is_file():
        msg = "QUALITY_COMPOUNDER_OFFICIAL_SESSION_ARTIFACT must name one file"
        raise ValueError(msg)
    size = artifact_path.stat().st_size
    if not 0 < size <= QUALITY_COMPOUNDER_CALENDAR_MAX_ARTIFACT_BYTES:
        msg = "official XNYS artifact is empty or exceeds the byte limit"
        raise ValueError(msg)
    artifact = load_quality_compounder_calendar_artifact(
        artifact_path.read_bytes(),
        expected_sha256=_required_env("QUALITY_COMPOUNDER_OFFICIAL_SESSION_SHA256"),
    )
    engine = create_engine_for_env(db_url=build_database_url())
    try:
        session_local = get_session_factory(engine=engine)
        with session_local() as session, session.begin():
            calendar = persist_quality_compounder_calendar(session, artifact)
            calendar_id = int(calendar.calendar_id)
        logger.info(
            "Official XNYS calendar imported",
            calendar_id=calendar_id,
            content_sha256=artifact.content_sha256,
            coverage_from=artifact.coverage_from.isoformat(),
            coverage_to=artifact.coverage_to.isoformat(),
        )
    finally:
        dispose_engine(engine)


def run_equity_catalogue_import() -> None:
    """Dry-run or atomically apply one reviewed equity catalogue artifact."""

    from .equity_catalogue_import import (  # noqa: PLC0415
        EQUITY_CATALOGUE_MAX_ARTIFACT_BYTES,
        apply_reviewed_equity_catalogue,
        load_reviewed_equity_catalogue_artifact,
    )

    artifact_path = Path(_required_env("EQUITY_CATALOGUE_ARTIFACT")).resolve(strict=True)
    if not artifact_path.is_file():
        msg = "EQUITY_CATALOGUE_ARTIFACT must name one file"
        raise ValueError(msg)
    size = artifact_path.stat().st_size
    if not 0 < size <= EQUITY_CATALOGUE_MAX_ARTIFACT_BYTES:
        msg = "reviewed equity catalogue is empty or exceeds the byte limit"
        raise ValueError(msg)
    artifact = load_reviewed_equity_catalogue_artifact(
        artifact_path.read_bytes(),
        expected_sha256=_required_env("EQUITY_CATALOGUE_SHA256"),
    )
    dry_run = parse_bool_env(
        "EQUITY_CATALOGUE_DRY_RUN",
        default=True,
        logger=logger,
        strict=True,
    )
    engine = create_engine_for_env(db_url=build_database_url())
    try:
        session_local = get_session_factory(engine=engine)
        with session_local() as session, session.begin():
            result = apply_reviewed_equity_catalogue(
                session,
                artifact,
                dry_run=dry_run,
            )
        logger.info(
            "Reviewed equity catalogue import complete",
            content_sha256=result.content_sha256,
            dry_run=result.dry_run,
            instrument_count=len(artifact.instruments),
            reviewed_at=artifact.reviewed_at.isoformat(),
            reviewer=artifact.reviewer,
            source_reference=artifact.source_reference,
            instruments_created=result.instruments_created,
            instruments_completed=result.instruments_completed,
            broker_mappings_created=result.broker_mappings_created,
            exact_replays=result.exact_replays,
        )
    finally:
        dispose_engine(engine)


def main() -> None:
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))

    command = sys.argv[1] if len(sys.argv) > 1 else "ingest"

    if command == "ingest":
        run_ingestor()
    elif command == "backfill":
        run_backfill()
    elif command == "fx-rates":
        run_fx_rates()
    elif command == "fx-rates-once":
        run_fx_rates(once=True)
    elif command == "market-calendars":
        run_market_calendars()
    elif command == "market-calendars-once":
        run_market_calendars(once=True)
    elif command == "quality-compounder-once":
        run_quality_compounder_once()
    elif command == "quality-compounder-calendar-import":
        run_quality_compounder_calendar_import()
    elif command == "equity-catalogue-import":
        run_equity_catalogue_import()
    else:
        print(f"Unknown command: {command}")
        print(
            "Available commands: ingest, backfill, fx-rates, fx-rates-once, "
            "market-calendars, market-calendars-once, quality-compounder-once, "
            "quality-compounder-calendar-import, equity-catalogue-import"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
