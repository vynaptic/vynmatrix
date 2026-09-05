from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from lib_common.asset_classes import CANONICAL_ASSET_CLASS_VALUES

_ROOT = Path(__file__).resolve().parents[1]
_INSTRUMENT_CONFIG = _ROOT / "config" / "instruments.yaml"
_STRATEGY_SCHEMA = _ROOT / "config" / "schemas" / "indicator_strategy_config.schema.json"
_PRODUCTION_SEED = _ROOT / "docker" / "seed" / "02_seed_data.sql"
_INFRASTRUCTURE_PERSISTENCE = (
    _ROOT
    / "libs"
    / "python"
    / "lib_infrastructure"
    / "lib_infrastructure"
    / "persistence"
    / "sqlalchemy"
    / "__init__.py"
)


def _load_module() -> ModuleType:
    from lib_application.services import catalogue

    return catalogue


def test_infrastructure_persistence_does_not_create_schema() -> None:
    persistence_source = _INFRASTRUCTURE_PERSISTENCE.read_text(encoding="utf-8")

    assert "create_all" not in persistence_source
    assert "drop_all" not in persistence_source


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("crypto", "crypto"),
        ("INDEX", "index"),
        ("etf", "etf"),
        ("futures", "futures"),
        ("forex", "fx"),
        ("commodity", "commodities"),
    ],
)
def test_asset_class_normalization_is_explicit(value: str, expected: str) -> None:
    module = _load_module()

    assert module._normalize_asset_class(value) == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "asset_class is required"),
        ("", "asset_class is required"),
        ("stock", "Unsupported instrument asset_class"),
    ],
)
def test_asset_class_normalization_fails_closed(value: str | None, message: str) -> None:
    module = _load_module()

    with pytest.raises(ValueError, match=message):
        module._normalize_asset_class(value)


def test_cash_index_defaults_to_reference_only() -> None:
    module = _load_module()

    assert (
        module._resolve_is_tradable(
            {},
            asset_class="index",
            symbol="NIFTY50",
        )
        is False
    )
    assert (
        module._resolve_is_tradable(
            {},
            asset_class="etf",
            symbol="SPY",
        )
        is True
    )


def test_cash_index_cannot_be_configured_as_tradable() -> None:
    module = _load_module()

    with pytest.raises(ValueError, match="reference-only index"):
        module._resolve_is_tradable(
            {"is_tradable": True},
            asset_class="index",
            symbol="NIFTY50",
        )


def test_tradability_must_be_a_boolean() -> None:
    module = _load_module()

    with pytest.raises(TypeError, match="is_tradable must be a boolean"):
        module._resolve_is_tradable(
            {"is_tradable": "false"},
            asset_class="index",
            symbol="NIFTY50",
        )


def test_strategy_schema_and_instrument_catalogue_share_the_canonical_taxonomy() -> None:
    schema = json.loads(_STRATEGY_SCHEMA.read_text(encoding="utf-8"))
    catalogue = yaml.safe_load(_INSTRUMENT_CONFIG.read_text(encoding="utf-8"))
    instruments = catalogue["instruments"]

    assert tuple(schema["definitions"]["assetClass"]["enum"]) == CANONICAL_ASSET_CLASS_VALUES
    assert {item["asset_class"] for item in instruments} <= set(CANONICAL_ASSET_CLASS_VALUES)
    by_symbol = {item["symbol"]: item for item in instruments}
    assert by_symbol["SPY"]["asset_class"] == "etf"
    assert by_symbol["QQQ"]["asset_class"] == "etf"
    assert by_symbol["NIFTY50"]["asset_class"] == "index"
    assert by_symbol["NIFTY50"]["is_tradable"] is False
    assert by_symbol["BANKNIFTY"]["asset_class"] == "index"
    assert by_symbol["BANKNIFTY"]["is_tradable"] is False


def test_production_seed_preserves_etf_and_reference_index_distinctions() -> None:
    seed = _PRODUCTION_SEED.read_text(encoding="utf-8")

    assert "(6, 'etf', 'SPY'" in seed
    assert "(7, 'etf', 'QQQ'" in seed
    assert "('index', 'NIFTY50', 'NSE', 'INR', 0.05, 1, 'scheduled', FALSE)" in seed
    assert "('index', 'BANKNIFTY', 'NSE', 'INR', 0.05, 1, 'scheduled', FALSE)" in seed
    assert "('NIFTY50', 'broad_market_index')" in seed
    assert "('BANKNIFTY', 'banking_index')" in seed


def test_production_seed_advances_sector_sequence_before_dynamic_hierarchy() -> None:
    seed = _PRODUCTION_SEED.read_text(encoding="utf-8")

    explicit_sectors = seed.index("INSERT INTO sectors (sector_id")
    sequence_sync = seed.index(
        "SELECT setval('sectors_sector_id_seq'",
        explicit_sectors,
    )
    dynamic_hierarchy = seed.index(
        "-- ETF and cash-index hierarchies use dynamic IDs",
        explicit_sectors,
    )

    assert explicit_sectors < sequence_sync < dynamic_hierarchy
