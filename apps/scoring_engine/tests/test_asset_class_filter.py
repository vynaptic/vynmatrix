"""BD-2: a binding restricted by asset_classes_allowed (and no specific
instruments) must match only instruments in those classes — not every asset
(the loader previously returned the match-all ["*"])."""

from __future__ import annotations

from types import SimpleNamespace

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
