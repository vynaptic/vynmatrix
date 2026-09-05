"""Contract tests for canonical asset taxonomy migration 0070."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from lib_application.db.models import Base, Instrument
from lib_common.asset_classes import (
    ASSET_CLASS_CHECK_SQL,
    CANONICAL_ASSET_CLASS_VALUES,
    NULLABLE_ASSET_CLASS_CHECK_SQL,
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0070_canonical_asset_taxonomy.py"
)
_LEGACY_CONSTRAINTS = {
    "instruments": (
        "ck_asset_class",
        "asset_class IN ('crypto', 'equity', 'futures', 'options', 'fx')",
    ),
    "user_trading_policies": (
        "ck_policy_asset_class",
        "asset_class IN ('crypto', 'equity', 'futures', 'options', 'fx')",
    ),
    "sectors": (
        "ck_sector_asset_class",
        "asset_class IN ('crypto', 'equity', 'futures', 'options', 'fx', 'commodities')",
    ),
    "market_scores": (
        "ck_market_score_asset_class",
        "asset_class IN ('crypto', 'equity', 'futures', 'options', 'fx', 'commodities')",
    ),
    "backtest_results": (
        "ck_backtest_asset_class",
        "asset_class IN ('crypto', 'equity', 'futures', 'options', 'fx')",
    ),
}
_MIGRATION_CHAIN_CONSTRAINT_TABLES = {
    "user_trading_policies",
    "market_scores",
    "backtest_results",
}
_NULLABLE_TABLES = {
    "mode_performance",
    "scoring_rules",
    "strategies",
    "execution_metrics",
}
_REQUIRED_TABLES = {
    "user_budget_buckets",
    *_NULLABLE_TABLES,
    *_LEGACY_CONSTRAINTS,
}


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("canonical_asset_taxonomy", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_schema(
    engine: sa.Engine,
    *,
    include_optional_metadata_checks: bool = False,
) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    tables: dict[str, sa.Table] = {}
    for table_name in sorted(_REQUIRED_TABLES):
        primary_key_name = "instr_id" if table_name == "instruments" else f"{table_name}_id"
        columns: list[sa.Column] = [
            sa.Column(primary_key_name, sa.Integer(), primary_key=True),
            sa.Column(
                "asset_class",
                sa.String(20),
                nullable=table_name in _NULLABLE_TABLES,
            ),
        ]
        if table_name == "instruments":
            columns.append(sa.Column("canonical", sa.String(50), nullable=False))
        args: list[object] = [*columns]
        legacy_constraint = _LEGACY_CONSTRAINTS.get(table_name)
        if legacy_constraint is not None and (
            table_name in _MIGRATION_CHAIN_CONSTRAINT_TABLES or include_optional_metadata_checks
        ):
            constraint_name, condition = legacy_constraint
            args.append(sa.CheckConstraint(condition, name=constraint_name))
        tables[table_name] = sa.Table(table_name, metadata, *args)

    tables["user_strategy_bindings"] = sa.Table(
        "user_strategy_bindings",
        metadata,
        sa.Column("binding_id", sa.BigInteger(), primary_key=True),
        sa.Column("asset_classes_allowed", sa.JSON(), nullable=False),
    )
    tables["brokers"] = sa.Table(
        "brokers",
        metadata,
        sa.Column("broker_id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
    )
    metadata.create_all(engine)
    return tables


def _run(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def _constraint_sql(table_name: str) -> dict[str, str]:
    table = Base.metadata.tables[table_name]
    return {
        str(constraint.name): str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }


def test_revision_follows_broker_account_contracts() -> None:
    migration = _load_migration()

    assert migration.revision == "0070_canonical_asset_taxonomy"
    assert migration.down_revision == "0069_broker_account_contracts"


def test_model_taxonomy_constraints_match_the_shared_contract() -> None:
    required = {
        "instruments": ("ck_asset_class", ASSET_CLASS_CHECK_SQL),
        "user_trading_policies": ("ck_policy_asset_class", ASSET_CLASS_CHECK_SQL),
        "user_budget_buckets": ("ck_budget_asset_class", ASSET_CLASS_CHECK_SQL),
        "sectors": ("ck_sector_asset_class", ASSET_CLASS_CHECK_SQL),
        "market_scores": ("ck_market_score_asset_class", ASSET_CLASS_CHECK_SQL),
        "mode_performance": (
            "ck_mode_performance_asset_class",
            NULLABLE_ASSET_CLASS_CHECK_SQL,
        ),
        "scoring_rules": (
            "ck_scoring_rule_asset_class",
            NULLABLE_ASSET_CLASS_CHECK_SQL,
        ),
        "strategies": ("ck_strategy_asset_class", NULLABLE_ASSET_CLASS_CHECK_SQL),
        "execution_metrics": (
            "ck_execution_metric_asset_class",
            NULLABLE_ASSET_CLASS_CHECK_SQL,
        ),
        "backtest_results": ("ck_backtest_asset_class", ASSET_CLASS_CHECK_SQL),
    }

    for table_name, (constraint_name, expected_sql) in required.items():
        assert _constraint_sql(table_name)[constraint_name] == expected_sql

    instrument_constraints = _constraint_sql("instruments")
    assert instrument_constraints["ck_index_reference_only"] == (
        "asset_class <> 'index' OR is_tradable = false"
    )
    assert Instrument.__table__.c.is_tradable.nullable is False


def test_upgrade_normalizes_every_surface_and_preserves_broker_metadata() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    tables = _legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            tables["instruments"].insert(),
            [
                {
                    "instr_id": 1,
                    "asset_class": "equity",
                    "canonical": "SPY",
                },
                {
                    "instr_id": 2,
                    "asset_class": "equity",
                    "canonical": "NIFTY50",
                },
                {
                    "instr_id": 3,
                    "asset_class": "crypto",
                    "canonical": "BTC/USD",
                },
            ],
        )
        scalar_values = {
            "user_trading_policies": "fx",
            "user_budget_buckets": "FOREX",
            "sectors": "commodities",
            "market_scores": "commodities",
            "mode_performance": "commodity",
            "scoring_rules": "indices",
            "strategies": "ETF",
            "execution_metrics": "FOREX",
            "backtest_results": "futures",
        }
        for table_name, asset_class in scalar_values.items():
            connection.execute(
                tables[table_name]
                .insert()
                .values({f"{table_name}_id": 1, "asset_class": asset_class})
            )
        connection.execute(
            tables["user_strategy_bindings"]
            .insert()
            .values(
                binding_id=1,
                asset_classes_allowed=[
                    "forex",
                    "fx",
                    "indices",
                    "commodity",
                ],
            )
        )
        connection.execute(
            tables["brokers"].insert(),
            [
                {
                    "broker_id": 1,
                    "code": "ibkr",
                    "capabilities": {
                        "asset_classes": ["equity", "forex"],
                        "exact_fill_retrieval": False,
                        "sentinel": "preserve",
                    },
                },
                {
                    "broker_id": 2,
                    "code": "paper",
                    "capabilities": {
                        "asset_classes": ["crypto"],
                        "exact_fill_retrieval": True,
                    },
                },
            ],
        )

    _run(engine, "upgrade")

    with engine.begin() as connection:
        instruments = connection.execute(
            sa.text("SELECT canonical, asset_class, is_tradable FROM instruments ORDER BY instr_id")
        ).all()
        assert instruments == [
            ("SPY", "etf", True),
            ("NIFTY50", "index", False),
            ("BTC/USD", "crypto", True),
        ]
        assert (
            connection.execute(
                sa.text(
                    "SELECT asset_class FROM user_budget_buckets WHERE user_budget_buckets_id = 1"
                )
            ).scalar_one()
            == "fx"
        )
        assert (
            connection.execute(
                sa.text("SELECT asset_class FROM mode_performance WHERE mode_performance_id = 1")
            ).scalar_one()
            == "commodities"
        )
        assert (
            connection.execute(
                sa.text("SELECT asset_class FROM scoring_rules WHERE scoring_rules_id = 1")
            ).scalar_one()
            == "index"
        )
        assert (
            connection.execute(
                sa.text("SELECT asset_classes_allowed FROM user_strategy_bindings")
            ).scalar_one()
            == '["fx", "index", "commodities"]'
        )

        broker_rows = connection.execute(
            sa.text("SELECT code, capabilities FROM brokers ORDER BY broker_id")
        ).all()
        ibkr_capabilities = json.loads(broker_rows[0][1])
        paper_capabilities = json.loads(broker_rows[1][1])
        assert ibkr_capabilities == {
            "asset_classes": ["equity", "etf", "fx"],
            "exact_fill_retrieval": False,
            "sentinel": "preserve",
        }
        assert paper_capabilities == {
            "asset_classes": [
                "crypto",
                "equity",
                "etf",
                "futures",
                "options",
                "fx",
                "commodities",
            ],
            "exact_fill_retrieval": True,
        }

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO instruments "
                "(instr_id, asset_class, canonical, is_tradable) "
                "VALUES (4, 'index', 'SENSEX', true)"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO strategies (strategies_id, asset_class) VALUES (2, 'stock')")
        )

    with pytest.raises(RuntimeError, match="converting ETFs or cash indices"):
        _run(engine, "downgrade")

    assert "is_tradable" in {
        column["name"] for column in sa.inspect(engine).get_columns("instruments")
    }
    with engine.begin() as connection:
        retained = connection.execute(
            sa.text(
                "SELECT canonical, asset_class, is_tradable FROM instruments "
                "WHERE canonical IN ('SPY', 'NIFTY50') ORDER BY canonical"
            )
        ).all()
        assert retained == [
            ("NIFTY50", "index", False),
            ("SPY", "etf", True),
        ]


def test_downgrade_succeeds_without_canonical_etf_or_index_semantics() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    tables = _legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            tables["instruments"]
            .insert()
            .values(
                instr_id=1,
                asset_class="crypto",
                canonical="BTC/USD",
            )
        )

    _run(engine, "upgrade")
    _run(engine, "downgrade")

    assert "is_tradable" not in {
        column["name"] for column in sa.inspect(engine).get_columns("instruments")
    }
    with engine.begin() as connection:
        assert connection.execute(
            sa.text("SELECT canonical, asset_class FROM instruments")
        ).one() == ("BTC/USD", "crypto")


def test_upgrade_rejects_unknown_existing_taxonomy_before_schema_mutation() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    tables = _legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            tables["strategies"]
            .insert()
            .values(
                strategies_id=1,
                asset_class="stock",
            )
        )

    with pytest.raises(RuntimeError, match="unsupported asset class 'stock'"):
        _run(engine, "upgrade")

    assert "is_tradable" not in {
        column["name"] for column in sa.inspect(engine).get_columns("instruments")
    }


def test_upgrade_converges_optional_orm_metadata_constraints() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _legacy_schema(engine, include_optional_metadata_checks=True)

    _run(engine, "upgrade")

    instrument_checks = {
        constraint["name"] for constraint in sa.inspect(engine).get_check_constraints("instruments")
    }
    sector_checks = {
        constraint["name"] for constraint in sa.inspect(engine).get_check_constraints("sectors")
    }
    assert "ck_asset_class" in instrument_checks
    assert "ck_sector_asset_class" in sector_checks


def test_migration_and_runtime_taxonomies_are_exactly_equal() -> None:
    migration = _load_migration()

    assert migration._CANONICAL_ASSET_CLASSES == CANONICAL_ASSET_CLASS_VALUES
