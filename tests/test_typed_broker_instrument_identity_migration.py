"""Contract tests for typed broker instrument catalogue identity."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0068_typed_broker_instrument_identity.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("typed_broker_instrument", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema() -> tuple[sa.Engine, sa.Table]:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    metadata = sa.MetaData()
    sa.Table(
        "brokers",
        metadata,
        sa.Column("broker_id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(50), nullable=False),
    )
    sa.Table(
        "instruments",
        metadata,
        sa.Column("instr_id", sa.Integer, primary_key=True),
        sa.Column("asset_class", sa.String(20), nullable=False),
        sa.Column("canonical", sa.String(50), nullable=False),
    )
    mappings = sa.Table(
        "instrument_broker_symbols",
        metadata,
        sa.Column(
            "instr_id",
            sa.Integer,
            sa.ForeignKey("instruments.instr_id"),
            primary_key=True,
        ),
        sa.Column(
            "broker_id",
            sa.Integer,
            sa.ForeignKey("brokers.broker_id"),
            primary_key=True,
        ),
        sa.Column("broker_symbol", sa.String(100), nullable=False),
    )
    metadata.create_all(engine)
    return engine, mappings


def _run(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def test_revision_follows_market_session_authority() -> None:
    migration = _load_migration()

    assert migration.revision == "0068_typed_broker_instrument"
    assert migration.down_revision == "0067_market_sessions"


def test_downgrade_refuses_to_delete_typed_identity() -> None:
    engine, mappings = _schema()
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO brokers VALUES (1, 'coinbase'), (7, 'saxo')"))
        connection.execute(
            sa.text("INSERT INTO instruments VALUES (1, 'crypto', 'BTC/USD'), (2, 'fx', 'EUR/USD')")
        )
        connection.execute(
            mappings.insert().values(
                instr_id=1,
                broker_id=1,
                broker_symbol="BTC-USD",
            )
        )

    _run(engine, "upgrade")
    reflected = sa.Table(
        "instrument_broker_symbols",
        sa.MetaData(),
        autoload_with=engine,
    )
    with engine.begin() as connection:
        existing = (
            connection.execute(sa.select(reflected).where(reflected.c.instr_id == 1))
            .mappings()
            .one()
        )
        assert existing["broker_symbol"] == "BTC-USD"
        assert existing["broker_instrument_id"] is None
        assert existing["broker_instrument_type"] is None

        saxo = (
            connection.execute(
                sa.select(reflected).where(
                    reflected.c.instr_id == 2,
                    reflected.c.broker_id == 7,
                )
            )
            .mappings()
            .one()
        )
        assert saxo["broker_symbol"] == "EURUSD"
        assert saxo["broker_instrument_id"] == "21"
        assert saxo["broker_instrument_type"] == "FxSpot"

    with pytest.raises(RuntimeError, match=r"1 typed mapping\(s\) exist"):
        _run(engine, "downgrade")

    columns = {column["name"] for column in sa.inspect(engine).get_columns(reflected.name)}
    assert "broker_symbol" in columns
    assert "broker_instrument_id" in columns
    assert "broker_instrument_type" in columns
    with engine.begin() as connection:
        saxo = connection.execute(
            sa.select(reflected).where(
                reflected.c.instr_id == 2,
                reflected.c.broker_id == 7,
            )
        ).one()
    assert saxo.broker_instrument_id == "21"
    assert saxo.broker_instrument_type == "FxSpot"


def test_downgrade_preserves_untyped_symbol_rows_after_explicit_typed_retirement() -> None:
    engine, mappings = _schema()
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO brokers VALUES (1, 'coinbase'), (7, 'saxo')"))
        connection.execute(
            sa.text("INSERT INTO instruments VALUES (1, 'crypto', 'BTC/USD'), (2, 'fx', 'EUR/USD')")
        )
        connection.execute(
            mappings.insert().values(
                instr_id=1,
                broker_id=1,
                broker_symbol="BTC-USD",
            )
        )

    _run(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "DELETE FROM instrument_broker_symbols "
                "WHERE broker_instrument_id IS NOT NULL "
                "OR broker_instrument_type IS NOT NULL"
            )
        )
    _run(engine, "downgrade")

    reflected = sa.Table(
        "instrument_broker_symbols",
        sa.MetaData(),
        autoload_with=engine,
    )
    assert set(reflected.c.keys()) == {"instr_id", "broker_id", "broker_symbol"}
    with engine.begin() as connection:
        assert connection.execute(sa.select(reflected)).one().broker_symbol == "BTC-USD"


def test_composite_identity_allows_same_venue_id_across_types() -> None:
    engine, _ = _schema()
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO brokers VALUES (7, 'saxo')"))
        connection.execute(
            sa.text(
                "INSERT INTO instruments VALUES "
                "(1, 'fx', 'EUR/USD'), "
                "(2, 'equity', 'INSTRUMENT-ONE'), "
                "(3, 'options', 'INSTRUMENT-TWO'), "
                "(4, 'equity', 'INSTRUMENT-THREE')"
            )
        )
    _run(engine, "upgrade")
    mappings = sa.Table(
        "instrument_broker_symbols",
        sa.MetaData(),
        autoload_with=engine,
    )

    with engine.begin() as connection:
        connection.execute(
            mappings.insert(),
            [
                {
                    "instr_id": 2,
                    "broker_id": 7,
                    "broker_symbol": "instrument-one",
                    "broker_instrument_id": "42",
                    "broker_instrument_type": "Stock",
                },
                {
                    "instr_id": 3,
                    "broker_id": 7,
                    "broker_symbol": "instrument-two",
                    "broker_instrument_id": "42",
                    "broker_instrument_type": "StockOption",
                },
            ],
        )

    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(
            mappings.insert().values(
                instr_id=4,
                broker_id=7,
                broker_symbol="duplicate-stock",
                broker_instrument_id="42",
                broker_instrument_type="Stock",
            )
        )


def test_untyped_venue_id_is_unique_within_one_broker() -> None:
    engine, _ = _schema()
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO brokers VALUES (7, 'saxo'), (8, 'ibkr')"))
        connection.execute(
            sa.text(
                "INSERT INTO instruments VALUES "
                "(1, 'fx', 'EUR/USD'), "
                "(2, 'equity', 'INSTRUMENT-ONE'), "
                "(3, 'equity', 'INSTRUMENT-TWO')"
            )
        )
    _run(engine, "upgrade")
    mappings = sa.Table(
        "instrument_broker_symbols",
        sa.MetaData(),
        autoload_with=engine,
    )

    with engine.begin() as connection:
        connection.execute(
            mappings.insert().values(
                instr_id=2,
                broker_id=8,
                broker_symbol="INSTRUMENT-ONE",
                broker_instrument_id="265598",
                broker_instrument_type=None,
            )
        )

    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(
            mappings.insert().values(
                instr_id=3,
                broker_id=8,
                broker_symbol="INSTRUMENT-TWO",
                broker_instrument_id="265598",
                broker_instrument_type=None,
            )
        )
