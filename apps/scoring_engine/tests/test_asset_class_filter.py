"""BD-2: a binding restricted by asset_classes_allowed (and no specific
instruments) must match only instruments in those classes — not every asset
(the loader previously returned the match-all ["*"])."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.orm import Session

from lib_application.db import models as app_models
from scoring_engine.binding_evaluation import _binding_matches_asset
from scoring_engine.storage import AppScoreStore


def _store() -> AppScoreStore:
    return AppScoreStore("sqlite+pysqlite:///:memory:")


_INSTRUMENT_MAP = {
    1: SimpleNamespace(canonical="BTCUSD", asset_class="crypto"),
    2: SimpleNamespace(canonical="ETHUSD", asset_class="crypto"),
    3: SimpleNamespace(canonical="AAPL", asset_class="equity"),
}


def test_asset_classes_allowed_resolves_to_class_symbols_only() -> None:
    store = _store()
    row = SimpleNamespace(instruments_allowed=[], asset_classes_allowed=["crypto"])

    asset_filter = store._resolve_asset_filter(row, _INSTRUMENT_MAP)

    assert set(asset_filter) == {"BTCUSD", "ETHUSD"}
    assert "*" not in asset_filter
    # An equity score must NOT match a crypto-only binding (the BD-2 regression).
    assert _binding_matches_asset("AAPL", asset_filter) is False
    assert _binding_matches_asset("BTCUSD", asset_filter) is True


def test_asset_classes_allowed_with_no_instruments_matches_nothing() -> None:
    store = _store()
    row = SimpleNamespace(instruments_allowed=[], asset_classes_allowed=["fx"])

    asset_filter = store._resolve_asset_filter(row, _INSTRUMENT_MAP)

    # No FX instruments -> sentinel -> matches nothing (not everything).
    assert _binding_matches_asset("BTCUSD", asset_filter) is False


def test_no_restrictions_matches_all() -> None:
    store = _store()
    row = SimpleNamespace(instruments_allowed=[], asset_classes_allowed=[])

    asset_filter = store._resolve_asset_filter(row, _INSTRUMENT_MAP)

    assert asset_filter == []  # empty -> match all
    assert _binding_matches_asset("ANYTHING", asset_filter) is True


def test_explicit_instruments_take_precedence() -> None:
    store = _store()
    row = SimpleNamespace(instruments_allowed=[1], asset_classes_allowed=["equity"])

    asset_filter = store._resolve_asset_filter(row, _INSTRUMENT_MAP)

    assert asset_filter == ["BTCUSD"]


def _catalogued_store() -> AppScoreStore:
    """A store whose catalogue holds crypto, equity and ETF instruments."""
    store = AppScoreStore("sqlite+pysqlite:///:memory:", bindings_cache_ttl_seconds=0)
    with Session(store._engine) as session:
        broker = app_models.Broker(code="paper", name="Paper Broker", capabilities={})
        session.add_all(
            [
                app_models.User(
                    user_id="owner",
                    email="owner@example.com",
                    base_ccy="USD",
                    is_deployment_owner=True,
                ),
                broker,
                *(
                    app_models.Instrument(
                        asset_class=asset_class, canonical=symbol, settlement_currency="USD"
                    )
                    for symbol, asset_class in (
                        ("BTCUSDC", "crypto"),
                        ("AAPL", "equity"),
                        ("MSFT", "equity"),
                        ("SPY", "etf"),
                    )
                ),
            ]
        )
        session.flush()
        session.add(
            app_models.LinkedBrokerAccount(
                account_id=1,
                user_id="owner",
                broker_id=broker.broker_id,
                environment="paper",
                display_name="Owner paper",
                base_ccy="USD",
                paper_initial_equity=100_000,
                paper_initial_cash=100_000,
                status="connected",
            )
        )
        session.flush()
        session.add_all(
            [
                # A membership universe: no list, bounded to equity.
                app_models.UserStrategyBinding(
                    user_id="owner",
                    strategy_id="quality",
                    broker_account_id=1,
                    asset_classes_allowed=["equity"],
                    instruments_allowed=None,
                    is_active=True,
                ),
                app_models.UserStrategyBinding(
                    user_id="owner",
                    strategy_id="swing",
                    broker_account_id=1,
                    asset_classes_allowed=["crypto"],
                    instruments_allowed=["BTCUSDC"],
                    is_active=True,
                ),
            ]
        )
        session.commit()
    return store


def test_a_binding_without_a_list_matches_every_catalogued_instrument_of_its_class() -> None:
    """The stored rows, not a hand-built map: the loader reads the whole class.

    The loader used to fetch only instruments named by id in some binding's list.
    Active lists hold canonical symbols, so the map was empty and a binding
    without a list matched nothing at all.
    """
    bindings = {binding.strategy_id: binding for binding in _catalogued_store().list_bindings()}

    quality = bindings["quality"].asset_filter
    assert sorted(quality) == ["AAPL", "MSFT"]
    assert _binding_matches_asset("AAPL", quality) is True
    assert _binding_matches_asset("BTCUSDC", quality) is False
    # An ETF is not an equity.
    assert _binding_matches_asset("SPY", quality) is False
    assert bindings["swing"].asset_filter == ["BTCUSDC"]
