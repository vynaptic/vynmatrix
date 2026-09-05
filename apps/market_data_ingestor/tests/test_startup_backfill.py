"""Historical auto-warm backfill, decoupled from the live poll loop.

The ``_run_startup_backfill`` helper runs the deficit-aware warmup; it is invoked
by the standalone ``backfill`` command (``run_backfill``, a separate one-shot
process), NOT in the live ingestor process — an in-process daemon thread shared
the poll loop's DB pool + venue provider and could stall the feed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from lib_data.market_data import MarketDataInstrument
from market_data_ingestor import backfill as backfill_mod
from market_data_ingestor import main as main_mod


class _SpyBackfiller:
    """Records ensure_history calls instead of touching a venue or database."""

    last: _SpyBackfiller | None = None
    corporate_actions_supported = False

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.ensure_calls: list[int] = []
        self.action_calls: list[tuple[date, date]] = []
        self.closed = False
        _SpyBackfiller.last = self

    def ensure_history(self, *, days: int) -> None:
        self.ensure_calls.append(days)

    def load_corporate_actions(self, *, start: date, end: date) -> dict[str, int]:
        self.action_calls.append((start, end))
        return {}

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_spy() -> None:
    _SpyBackfiller.last = None


def _stub_coinbase_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_mod,
        "_get_ingestion_instruments",
        lambda _session_factory, _source: [
            MarketDataInstrument(
                canonical_symbol="BTC/USD",
                product_id="BTC-USD",
                asset_class="crypto",
            )
        ],
    )


def test_get_backfill_days_defaults_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INGESTOR_BACKFILL_DAYS", raising=False)
    assert main_mod._get_backfill_days() == 0


def test_ingestor_symbols_default_to_deployed_usdc_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INGESTOR_SYMBOLS", raising=False)

    assert main_mod._get_symbols() == ["BTC-USDC", "ETH-USDC", "SOL-USDC"]


def test_coinbase_instruments_load_exact_broker_symbols_from_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INGESTOR_SYMBOLS", "BTC-USDC")

    calls: list[dict[str, Any]] = []

    class _Catalogue:
        def __init__(self, _session_factory: Any) -> None:
            pass

        def load_market_data_instruments(self, **kwargs: Any) -> list[MarketDataInstrument]:
            calls.append(kwargs)
            return [
                MarketDataInstrument(
                    canonical_symbol="BTC/USDC",
                    product_id="BTC-USDC",
                    asset_class="crypto",
                )
            ]

    monkeypatch.setattr(main_mod, "PriceIngestionService", _Catalogue)
    instruments = main_mod._get_ingestion_instruments(lambda: None, "coinbase_live")

    assert len(instruments) == 1
    assert instruments[0].canonical_symbol == "BTC/USDC"
    assert instruments[0].product_id == "BTC-USDC"
    assert instruments[0].asset_class == "crypto"
    assert calls == [
        {
            "broker_code": "coinbase",
            "symbols": ["BTC-USDC"],
            "identifier_field": "broker_symbol",
            "require_numeric_identifier": False,
            "require_instrument_type": False,
        }
    ]


@pytest.mark.parametrize(
    ("source", "broker_code", "requires_type"),
    [
        ("ibkr", "ibkr", False),
        ("zerodha", "zerodha", False),
        ("saxo_live", "saxo", True),
        ("saxo_simulation", "saxo", True),
    ],
)
def test_opaque_venue_instruments_require_typed_database_identity(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    broker_code: str,
    requires_type: bool,
) -> None:
    monkeypatch.setenv("INGESTOR_SYMBOLS", "AAPL")
    calls: list[dict[str, Any]] = []

    class _Catalogue:
        def __init__(self, _session_factory: Any) -> None:
            pass

        def load_market_data_instruments(self, **kwargs: Any) -> list[MarketDataInstrument]:
            calls.append(kwargs)
            return [
                MarketDataInstrument(
                    canonical_symbol="AAPL",
                    product_id="265598",
                    asset_class="equity",
                    broker_instrument_type="Stock" if requires_type else None,
                )
            ]

    monkeypatch.setattr(main_mod, "PriceIngestionService", _Catalogue)
    instruments = main_mod._get_ingestion_instruments(lambda: None, source)

    assert instruments[0].canonical_symbol == "AAPL"
    assert calls == [
        {
            "broker_code": broker_code,
            "symbols": ["AAPL"],
            "identifier_field": "broker_instrument_id",
            "require_numeric_identifier": True,
            "require_instrument_type": requires_type,
        }
    ]


def test_ingestion_catalogue_rejects_noncanonical_source() -> None:
    with pytest.raises(ValueError, match="Unknown market-data source"):
        main_mod._get_ingestion_instruments(lambda: None, "saxo")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "zerodha",
            {
                "api_key": "kite-market-data-key",
                "api_secret": None,
                "access_token": "kite-market-data-token",
                "access_token_expires_at": None,
                "account_key": None,
            },
        ),
        (
            "saxo_live",
            {
                "api_key": None,
                "api_secret": None,
                "access_token": "saxo-market-data-token",
                "access_token_expires_at": "2026-07-26T00:00:00Z",
                "account_key": "saxo-market-data-account",
            },
        ),
    ],
)
def test_authenticated_feed_uses_dedicated_market_data_credentials(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    expected: dict[str, str | None],
) -> None:
    monkeypatch.setenv("INGESTOR_SOURCE", source)
    monkeypatch.setenv("ZERODHA_MARKET_DATA_API_KEY", "kite-market-data-key")
    monkeypatch.setenv("ZERODHA_MARKET_DATA_ACCESS_TOKEN", "kite-market-data-token")
    monkeypatch.setenv("SAXO_MARKET_DATA_ACCESS_TOKEN", "saxo-market-data-token")
    monkeypatch.setenv(
        "SAXO_MARKET_DATA_ACCESS_TOKEN_EXPIRES_AT",
        "2026-07-26T00:00:00Z",
    )
    monkeypatch.setenv("SAXO_MARKET_DATA_ACCOUNT_KEY", "saxo-market-data-account")
    monkeypatch.setenv("COINBASE_API_KEY", "must-not-leak")
    monkeypatch.setenv("COINBASE_API_SECRET", "must-not-leak")
    monkeypatch.setattr(
        main_mod,
        "_get_ingestion_instruments",
        lambda _session_factory, _source: [
            MarketDataInstrument(
                canonical_symbol="AAPL",
                product_id="265598",
                asset_class="equity",
                broker_instrument_type="Stock" if source.startswith("saxo") else None,
            )
        ],
    )
    captured: dict[str, Any] = {}

    class _Scheduler:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(main_mod, "IngestionScheduler", _Scheduler)

    main_mod._build_scheduler(lambda: None)

    assert {name: captured[name] for name in expected} == expected


def test_startup_backfill_noop_when_days_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGESTOR_BACKFILL_DAYS", "0")
    monkeypatch.setattr(backfill_mod, "HistoricalBackfiller", _SpyBackfiller)

    main_mod._run_startup_backfill(lambda: None)

    # Disabled → the backfiller is never even constructed.
    assert _SpyBackfiller.last is None


def test_startup_backfill_runs_ensure_history_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INGESTOR_BACKFILL_DAYS", "150")
    monkeypatch.setenv("INGESTOR_SYMBOLS", "BTCUSD")
    _stub_coinbase_catalogue(monkeypatch)
    monkeypatch.setattr(backfill_mod, "HistoricalBackfiller", _SpyBackfiller)

    main_mod._run_startup_backfill(lambda: None)

    spy = _SpyBackfiller.last
    assert spy is not None
    assert spy.kwargs["instruments"] == [
        MarketDataInstrument(
            canonical_symbol="BTC/USD",
            product_id="BTC-USD",
            asset_class="crypto",
        )
    ]
    assert spy.kwargs["source"] == "coinbase_live"
    assert spy.ensure_calls == [150]
    assert spy.action_calls == []  # provider exposes no corporate actions
    assert spy.closed is True  # always closed, even on the happy path


def test_one_shot_backfill_loads_corporate_actions_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ScheduledSpy(_SpyBackfiller):
        corporate_actions_supported = True

    monkeypatch.setenv("INGESTOR_BACKFILL_DAYS", "150")
    monkeypatch.setenv("INGESTOR_SOURCE", "eodhd")
    monkeypatch.setattr(
        main_mod,
        "_get_ingestion_instruments",
        lambda *_args: [
            MarketDataInstrument(
                canonical_symbol="AAPL",
                product_id="AAPL",
                asset_class="equity",
            )
        ],
    )
    monkeypatch.setattr(backfill_mod, "HistoricalBackfiller", _ScheduledSpy)

    main_mod._run_startup_backfill(lambda: None)

    spy = _SpyBackfiller.last
    assert spy is not None
    assert spy.ensure_calls == [150]
    # The same one-shot refreshes splits/dividends over the warmup window.
    (action_window,) = spy.action_calls
    start, end = action_window
    assert end == datetime.now(tz=UTC).date()
    assert end - start == timedelta(days=150)
    assert spy.closed is True


def test_one_shot_backfill_propagates_errors_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom(_SpyBackfiller):
        def ensure_history(self, *, days: int) -> None:
            raise RuntimeError("coinbase down")

    monkeypatch.setenv("INGESTOR_BACKFILL_DAYS", "150")
    _stub_coinbase_catalogue(monkeypatch)
    monkeypatch.setattr(backfill_mod, "HistoricalBackfiller", _Boom)

    # The helper is only used by the isolated deployment job. It must fail the
    # job rather than report a false-green warm-up; the live feed is a separate
    # process and is unaffected.
    with pytest.raises(RuntimeError, match="coinbase down"):
        main_mod._run_startup_backfill(lambda: None)
    assert _SpyBackfiller.last is not None
    assert _SpyBackfiller.last.closed is True


def test_run_backfill_runs_once_in_own_process_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The standalone `backfill` command runs the warmup once with its OWN engine
    # (decoupled from the live ingestor) and always disposes it.
    monkeypatch.setenv("INGESTOR_BACKFILL_DAYS", "150")
    monkeypatch.setenv("INGESTOR_SYMBOLS", "BTCUSD")
    _stub_coinbase_catalogue(monkeypatch)
    monkeypatch.setattr(backfill_mod, "HistoricalBackfiller", _SpyBackfiller)
    monkeypatch.setattr(main_mod, "build_database_url", lambda: "postgresql://x/y")
    sentinel_engine = object()
    monkeypatch.setattr(main_mod, "create_engine_for_env", lambda **_kw: sentinel_engine)
    monkeypatch.setattr(main_mod, "get_session_factory", lambda **_kw: (lambda: None))
    disposed: list[Any] = []
    monkeypatch.setattr(main_mod, "dispose_engine", lambda engine: disposed.append(engine))

    main_mod.run_backfill()

    spy = _SpyBackfiller.last
    assert spy is not None
    assert spy.ensure_calls == [150]
    assert disposed == [sentinel_engine]  # engine always disposed


def test_one_shot_backfill_uses_non_coinbase_active_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INGESTOR_BACKFILL_DAYS", "1")
    monkeypatch.setenv("INGESTOR_SOURCE", "ibkr")
    monkeypatch.setattr(
        main_mod,
        "_get_ingestion_instruments",
        lambda *_args: [
            MarketDataInstrument(
                canonical_symbol="AAPL",
                product_id="265598",
                asset_class="equity",
            )
        ],
    )
    monkeypatch.setattr(backfill_mod, "HistoricalBackfiller", _SpyBackfiller)

    main_mod._run_startup_backfill(lambda: None)

    spy = _SpyBackfiller.last
    assert spy is not None
    assert spy.kwargs["source"] == "ibkr"
    assert spy.kwargs["instruments"][0].product_id == "265598"
    assert spy.ensure_calls == [1]


@pytest.mark.parametrize(
    ("source", "instrument", "expected_credentials"),
    [
        (
            "zerodha",
            MarketDataInstrument(
                canonical_symbol="NIFTY50",
                product_id="256265",
                asset_class="index",
            ),
            {
                "api_key": "kite-market-data-key",
                "api_secret": None,
                "access_token": "kite-market-data-token",
                "access_token_expires_at": None,
                "account_key": None,
            },
        ),
        (
            "saxo_live",
            MarketDataInstrument(
                canonical_symbol="EUR/USD",
                product_id="21",
                asset_class="fx",
                broker_instrument_type="FxSpot",
            ),
            {
                "api_key": None,
                "api_secret": None,
                "access_token": "saxo-market-data-token",
                "access_token_expires_at": "2026-07-26T00:00:00Z",
                "account_key": "saxo-market-data-account",
            },
        ),
    ],
)
def test_one_shot_backfill_uses_dedicated_feed_credentials(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    instrument: MarketDataInstrument,
    expected_credentials: dict[str, str | None],
) -> None:
    monkeypatch.setenv("INGESTOR_BACKFILL_DAYS", "1")
    monkeypatch.setenv("INGESTOR_SOURCE", source)
    monkeypatch.setenv("ZERODHA_MARKET_DATA_API_KEY", "kite-market-data-key")
    monkeypatch.setenv("ZERODHA_MARKET_DATA_ACCESS_TOKEN", "kite-market-data-token")
    monkeypatch.setenv("SAXO_MARKET_DATA_ACCESS_TOKEN", "saxo-market-data-token")
    monkeypatch.setenv(
        "SAXO_MARKET_DATA_ACCESS_TOKEN_EXPIRES_AT",
        "2026-07-26T00:00:00Z",
    )
    monkeypatch.setenv("SAXO_MARKET_DATA_ACCOUNT_KEY", "saxo-market-data-account")
    monkeypatch.setenv("COINBASE_API_KEY", "must-not-leak")
    monkeypatch.setenv("COINBASE_API_SECRET", "must-not-leak")
    monkeypatch.setattr(
        main_mod,
        "_get_ingestion_instruments",
        lambda *_args: [instrument],
    )
    monkeypatch.setattr(backfill_mod, "HistoricalBackfiller", _SpyBackfiller)

    main_mod._run_startup_backfill(lambda: None)

    spy = _SpyBackfiller.last
    assert spy is not None
    assert {key: spy.kwargs[key] for key in expected_credentials} == expected_credentials


def test_one_shot_backfill_rejects_noncanonical_source_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INGESTOR_BACKFILL_DAYS", "1")
    monkeypatch.setenv("INGESTOR_SOURCE", "saxo")
    monkeypatch.setattr(backfill_mod, "HistoricalBackfiller", _SpyBackfiller)

    with pytest.raises(ValueError, match="Unknown market-data source"):
        main_mod._run_startup_backfill(lambda: None)

    assert _SpyBackfiller.last is None


def test_run_ingestor_does_not_backfill_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: the live ingestor must NOT construct/run a backfiller in-process
    # (that daemon thread starved the poll loop and stalled the feed).
    monkeypatch.setenv("INGESTOR_BACKFILL_DAYS", "150")
    monkeypatch.setattr(backfill_mod, "HistoricalBackfiller", _SpyBackfiller)
    called: list[str] = []
    monkeypatch.setattr(main_mod, "_run_startup_backfill", lambda _sf: called.append("backfill"))
    monkeypatch.setattr(main_mod, "build_database_url", lambda: "postgresql://x/y")
    monkeypatch.setattr(main_mod, "create_engine_for_env", lambda **_kw: object())
    monkeypatch.setattr(main_mod, "get_session_factory", lambda **_kw: (lambda: None))
    monkeypatch.setattr(main_mod, "dispose_engine", lambda _e: None)

    # Stop the scheduler immediately so run_forever returns without polling.
    class _NoopScheduler:
        def run_forever(self) -> None: ...
        def stop(self) -> None: ...

    monkeypatch.setattr(main_mod, "_build_scheduler", lambda _sf: _NoopScheduler())
    monkeypatch.setattr(main_mod, "_start_health_server", lambda _s: None)

    main_mod.run_ingestor()

    assert called == []  # no in-process backfill
    assert _SpyBackfiller.last is None


def test_run_ingestor_propagates_scheduler_failure_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_engine = object()
    stopped: list[bool] = []
    disposed: list[object] = []

    class _CrashScheduler:
        def run_forever(self) -> None:
            raise RuntimeError("venue contract failed")

        def stop(self) -> None:
            stopped.append(True)

    monkeypatch.setattr(main_mod, "build_database_url", lambda: "postgresql://x/y")
    monkeypatch.setattr(main_mod, "create_engine_for_env", lambda **_kw: sentinel_engine)
    monkeypatch.setattr(main_mod, "get_session_factory", lambda **_kw: (lambda: None))
    monkeypatch.setattr(main_mod, "_build_scheduler", lambda _sf: _CrashScheduler())
    monkeypatch.setattr(main_mod, "_start_health_server", lambda _s: None)
    monkeypatch.setattr(main_mod, "dispose_engine", lambda engine: disposed.append(engine))

    with pytest.raises(RuntimeError, match="venue contract failed"):
        main_mod.run_ingestor()

    assert stopped == [True]
    assert disposed == [sentinel_engine]


def test_eodhd_instruments_resolve_from_equity_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INGESTOR_SYMBOLS", "AAPL,NVDA")
    calls: list[dict[str, Any]] = []

    class _Catalogue:
        def __init__(self, _session_factory: Any) -> None:
            pass

        def load_instrument_map(self, **kwargs: Any) -> dict[str, int]:
            calls.append(kwargs)
            return {"AAPL": 8, "NVDA": 9}

    monkeypatch.setattr(main_mod, "PriceIngestionService", _Catalogue)
    instruments = main_mod._get_ingestion_instruments(lambda: None, "eodhd")

    assert [i.canonical_symbol for i in instruments] == ["AAPL", "NVDA"]
    assert [i.product_id for i in instruments] == ["AAPL", "NVDA"]
    assert all(i.asset_class == "equity" for i in instruments)
    assert calls == [{"asset_class": "equity"}]


def test_eodhd_instruments_fail_closed_on_unknown_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INGESTOR_SYMBOLS", "AAPL,ZZZZ")

    class _Catalogue:
        def __init__(self, _session_factory: Any) -> None:
            pass

        def load_instrument_map(self, **kwargs: Any) -> dict[str, int]:
            return {"AAPL": 8}

    monkeypatch.setattr(main_mod, "PriceIngestionService", _Catalogue)
    with pytest.raises(ValueError, match="No equity instrument rows for: ZZZZ"):
        main_mod._get_ingestion_instruments(lambda: None, "eodhd")


def test_eodhd_credentials_come_from_dedicated_token_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EODHD_API_TOKEN", "unit-test-token")
    credentials = main_mod._get_provider_credentials("eodhd")
    assert credentials["api_key"] == "unit-test-token"
    assert credentials["api_secret"] is None


def test_delayed_quotes_require_explicit_exact_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EODHD_DELAYED_QUOTES_ENABLED", "true")
    monkeypatch.delenv("SP500_RESEARCH_OWNER_USER_ID", raising=False)

    with pytest.raises(ValueError, match="SP500_RESEARCH_OWNER_USER_ID"):
        main_mod._get_delayed_quote_owner("eodhd")

    monkeypatch.setenv("SP500_RESEARCH_OWNER_USER_ID", "owner-1")
    assert main_mod._get_delayed_quote_owner("eodhd") == "owner-1"


def test_delayed_quotes_reject_non_eodhd_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EODHD_DELAYED_QUOTES_ENABLED", "true")
    monkeypatch.setenv("SP500_RESEARCH_OWNER_USER_ID", "owner-1")

    with pytest.raises(ValueError, match="only on the EODHD equity ingestor"):
        main_mod._get_delayed_quote_owner("coinbase_live")


def test_eodhd_daily_quota_cooldown_is_strictly_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EODHD_DAILY_QUOTA_MIN_COOLDOWN_SEC", "900")
    assert main_mod._get_eodhd_daily_quota_min_cooldown() == 900

    monkeypatch.setenv("EODHD_DAILY_QUOTA_MIN_COOLDOWN_SEC", "59")
    with pytest.raises(ValueError, match="below minimum 60"):
        main_mod._get_eodhd_daily_quota_min_cooldown()

    monkeypatch.setenv("EODHD_DAILY_QUOTA_MIN_COOLDOWN_SEC", "86401")
    with pytest.raises(ValueError, match="above maximum 86400"):
        main_mod._get_eodhd_daily_quota_min_cooldown()
