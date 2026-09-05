"""Observed-history market context is point-in-time and fails closed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lib_common.config_validation import (
    RunMode,
    ScoringMarketContextConfig,
)
from scoring_engine.domain import MarketRegime
from scoring_engine.main import _build_market_context_meta_service
from scoring_engine.pipeline import PipelineConfig
from scoring_engine.services.meta_label_service import (
    AssetClassRoutingMarketContextProvider,
    MarketContextUnavailableError,
    PriceBasedMarketContextProvider,
    PriceObservation,
)
from scoring_engine.storage_memory import InMemoryScoreStore

WINDOW = 5
NOW = datetime(2026, 7, 25, 12, tzinfo=UTC)


def _provider(closes: list[float]) -> PriceBasedMarketContextProvider:
    observations = [
        PriceObservation(
            timestamp=NOW - timedelta(minutes=len(closes) - index - 1),
            close=close,
        )
        for index, close in enumerate(closes)
    ]

    def _loader(_asset: str, limit: int, _as_of: datetime) -> list[PriceObservation]:
        return observations[-limit:]

    return PriceBasedMarketContextProvider(_loader, window=WINDOW)


def test_insufficient_history_fails_closed() -> None:
    with pytest.raises(MarketContextUnavailableError, match="Insufficient price history"):
        _provider([100.0, 101.0]).get_context("BTCUSD", NOW)


def test_uptrend_low_vol_is_bull_quiet() -> None:
    ctx = _provider([100.0, 101.0, 102.0, 103.0, 104.0, 105.0]).get_context("BTCUSD", NOW)
    assert ctx.regime is MarketRegime.BULL_QUIET
    assert ctx.realized_vol_20d > 0.0
    assert ctx.vix_level is None
    assert ctx.vix_percentile is None
    assert ctx.liquidity_score is None
    assert ctx.as_of == NOW


def test_flat_is_sideways_zero_vol() -> None:
    ctx = _provider([100.0] * (WINDOW + 1)).get_context("BTCUSD", NOW)
    assert ctx.regime is MarketRegime.SIDEWAYS
    assert ctx.realized_vol_20d == 0.0


def test_downtrend_high_vol_is_crisis() -> None:
    ctx = _provider([100.0, 90.0, 100.0, 85.0, 95.0, 80.0]).get_context("BTCUSD", NOW)
    assert ctx.regime is MarketRegime.CRISIS
    assert ctx.realized_vol_20d > 0.06


def test_empty_loader_fails_closed() -> None:
    provider = PriceBasedMarketContextProvider(lambda _a, _n, _t: [], window=WINDOW)
    with pytest.raises(MarketContextUnavailableError, match="Insufficient price history"):
        provider.get_context("BTCUSD", NOW)


def test_stale_latest_observation_fails_closed() -> None:
    observations = [
        PriceObservation(
            timestamp=NOW - timedelta(hours=3, minutes=WINDOW - index),
            close=100.0 + index,
        )
        for index in range(WINDOW + 1)
    ]
    provider = PriceBasedMarketContextProvider(
        lambda _a, _n, _t: observations,
        window=WINDOW,
        max_age=timedelta(hours=1),
    )
    with pytest.raises(MarketContextUnavailableError, match="stale"):
        provider.get_context("BTCUSD", NOW)


def test_future_observation_fails_closed() -> None:
    observations = [
        PriceObservation(timestamp=NOW - timedelta(minutes=WINDOW - index), close=100 + index)
        for index in range(WINDOW + 1)
    ]
    observations[-1] = PriceObservation(timestamp=NOW + timedelta(seconds=1), close=105)
    provider = PriceBasedMarketContextProvider(
        lambda _a, _n, _t: observations,
        window=WINDOW,
    )
    with pytest.raises(MarketContextUnavailableError, match="future"):
        provider.get_context("BTCUSD", NOW)


def test_invalid_close_fails_closed() -> None:
    observations = [
        PriceObservation(timestamp=NOW - timedelta(minutes=WINDOW - index), close=100 + index)
        for index in range(WINDOW + 1)
    ]
    observations[2] = PriceObservation(timestamp=observations[2].timestamp, close=float("nan"))
    provider = PriceBasedMarketContextProvider(
        lambda _a, _n, _t: observations,
        window=WINDOW,
    )
    with pytest.raises(MarketContextUnavailableError, match="Invalid observed close"):
        provider.get_context("BTCUSD", NOW)


def test_uncalibrated_scorer_cannot_start_in_live_mode() -> None:
    with pytest.raises(RuntimeError, match="approved only for paper/backtest"):
        _build_market_context_meta_service(
            InMemoryScoreStore(),
            PipelineConfig(),
            settings=ScoringMarketContextConfig(),
            mode=RunMode.LIVE,
        )


def _observations(count: int) -> list[PriceObservation]:
    return [
        PriceObservation(timestamp=NOW - timedelta(minutes=count - index), close=100.0 + index)
        for index in range(count)
    ]


def _stub_provider(marker: dict[str, str], name: str) -> PriceBasedMarketContextProvider:
    observations = _observations(WINDOW + 1)

    def _loader(asset: str, limit: int, _as_of: datetime) -> list[PriceObservation]:
        marker[asset] = name
        return observations[-limit:]

    return PriceBasedMarketContextProvider(_loader, window=WINDOW)


def test_router_dispatches_by_asset_class() -> None:
    calls: dict[str, str] = {}
    router = AssetClassRoutingMarketContextProvider(
        default=_stub_provider(calls, "default"),
        by_asset_class={"equity": _stub_provider(calls, "equity")},
        asset_class_resolver=lambda asset: "equity" if asset == "AAPL" else "crypto",
    )
    router.get_context("AAPL", NOW)
    assert calls["AAPL"] == "equity"
    router.get_context("BTCUSDC", NOW)
    assert calls["BTCUSDC"] == "default"  # unlisted class falls back to default


def test_router_fails_closed_on_unknown_instrument() -> None:
    calls: dict[str, str] = {}
    router = AssetClassRoutingMarketContextProvider(
        default=_stub_provider(calls, "default"),
        by_asset_class={"equity": _stub_provider(calls, "equity")},
        asset_class_resolver=lambda _asset: None,
    )
    with pytest.raises(MarketContextUnavailableError, match="Unknown instrument"):
        router.get_context("NOPE", NOW)
    assert not calls


def test_router_fails_closed_on_resolver_error() -> None:
    calls: dict[str, str] = {}

    def _broken(_asset: str) -> str | None:
        msg = "db down"
        raise ValueError(msg)

    router = AssetClassRoutingMarketContextProvider(
        default=_stub_provider(calls, "default"),
        by_asset_class={"equity": _stub_provider(calls, "equity")},
        asset_class_resolver=_broken,
    )
    with pytest.raises(MarketContextUnavailableError, match="Failed to resolve"):
        router.get_context("AAPL", NOW)


def test_router_without_overrides_never_resolves_asset_class() -> None:
    calls: dict[str, str] = {}

    def _must_not_run(_asset: str) -> str | None:
        msg = "resolver must not be consulted without overrides"
        raise AssertionError(msg)

    router = AssetClassRoutingMarketContextProvider(
        default=_stub_provider(calls, "default"),
        by_asset_class={},
        asset_class_resolver=_must_not_run,
    )
    router.get_context("BTCUSDC", NOW)
    assert calls["BTCUSDC"] == "default"
