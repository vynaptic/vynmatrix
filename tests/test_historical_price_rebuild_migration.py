"""Contract tests for historical-price rebuild migration 0062."""

from __future__ import annotations

import importlib.util
from datetime import datetime
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
    / "0062_historical_price_rebuild.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "historical_price_rebuild_migration",
        _MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def _create_legacy_schema(engine: sa.Engine) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    instruments = sa.Table(
        "instruments",
        metadata,
        sa.Column("instr_id", sa.Integer, primary_key=True),
        sa.Column("canonical", sa.String(50), nullable=False),
    )
    aliases = sa.Table(
        "instrument_aliases",
        metadata,
        sa.Column("alias_id", sa.Integer, primary_key=True),
        sa.Column("instr_id", sa.Integer, nullable=False),
        sa.Column("alias", sa.String(100), nullable=False),
    )
    broker_symbols = sa.Table(
        "instrument_broker_symbols",
        metadata,
        sa.Column("instr_id", sa.Integer, primary_key=True),
        sa.Column("broker_id", sa.Integer, primary_key=True),
        sa.Column("broker_symbol", sa.String(100), nullable=False),
    )
    prices = sa.Table(
        "prices",
        metadata,
        sa.Column("price_id", sa.Integer, primary_key=True),
        sa.Column("instr_id", sa.Integer, nullable=False),
        sa.Column("ts", sa.DateTime, nullable=False),
        sa.Column("timeframe", sa.String(20), nullable=False),
        sa.Column("open", sa.Numeric(20, 8)),
        sa.Column("high", sa.Numeric(20, 8)),
        sa.Column("low", sa.Numeric(20, 8)),
        sa.Column("close", sa.Numeric(20, 8), nullable=False),
        sa.Column("volume", sa.Numeric(20, 8)),
        sa.Column("source", sa.String(50), nullable=False),
        sa.UniqueConstraint(
            "instr_id",
            "ts",
            "timeframe",
            "source",
            name="uq_prices_instr_ts",
        ),
    )
    watermarks = sa.Table(
        "watermarks",
        metadata,
        sa.Column("worker_id", sa.String(100), primary_key=True),
        sa.Column("symbol", sa.String(50), primary_key=True),
        sa.Column("timeframe", sa.String(20), primary_key=True),
        sa.Column("last_ts", sa.DateTime, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )
    metadata.create_all(engine)
    return {
        "instruments": instruments,
        "aliases": aliases,
        "broker_symbols": broker_symbols,
        "prices": prices,
        "watermarks": watermarks,
    }


def _seed_single_feed(
    engine: sa.Engine,
    tables: dict[str, sa.Table],
    *,
    sources: tuple[str, ...] = ("coinbase_live",),
) -> datetime:
    last_ts = datetime(2026, 7, 25, 12, 0)
    with engine.begin() as connection:
        connection.execute(
            tables["instruments"]
            .insert()
            .values(
                instr_id=11,
                canonical="BTC/USDC",
            )
        )
        connection.execute(
            tables["aliases"]
            .insert()
            .values(
                alias_id=1,
                instr_id=11,
                alias="XBT_USDC",
            )
        )
        connection.execute(
            tables["broker_symbols"]
            .insert()
            .values(
                instr_id=11,
                broker_id=1,
                broker_symbol="BTC-USDC-PERP",
            )
        )
        if sources:
            connection.execute(
                tables["prices"].insert(),
                [
                    {
                        "price_id": index,
                        "instr_id": 11,
                        "ts": last_ts,
                        "timeframe": "1m",
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100,
                        "volume": 10,
                        "source": source,
                    }
                    for index, source in enumerate(sources, start=1)
                ],
            )
    return last_ts


def test_revision_follows_account_scoped_daily_nav() -> None:
    migration = _load_migration()

    assert migration.revision == "0062_historical_price_rebuild"
    assert migration.down_revision == "0061_account_scoped_daily_nav"


def test_upgrade_infers_legacy_feed_identity_and_round_trips() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    tables = _create_legacy_schema(engine)
    last_ts = _seed_single_feed(engine, tables)
    with engine.begin() as connection:
        connection.execute(
            tables["watermarks"].insert(),
            [
                {
                    "worker_id": "canonical-worker",
                    "symbol": "btc-usdc",
                    "timeframe": "1m",
                    "last_ts": last_ts,
                },
                {
                    "worker_id": "alias-worker",
                    "symbol": "xbt/usdc",
                    "timeframe": "1m",
                    "last_ts": last_ts,
                },
                {
                    "worker_id": "broker-worker",
                    "symbol": "BTC_USDC_PERP",
                    "timeframe": "1m",
                    "last_ts": last_ts,
                },
            ],
        )

    _run(engine, "upgrade")

    inspector = sa.inspect(engine)
    price_columns = {column["name"]: column for column in inspector.get_columns("prices")}
    watermark_columns = {column["name"]: column for column in inspector.get_columns("watermarks")}
    assert price_columns["content_revision"]["nullable"] is False
    assert watermark_columns["instr_id"]["nullable"] is False
    assert watermark_columns["source"]["nullable"] is False
    assert watermark_columns["rebuild_generation"]["nullable"] is False
    assert "rebuild_from_ts" in watermark_columns
    assert {constraint["name"] for constraint in inspector.get_check_constraints("watermarks")} >= {
        "ck_watermarks_rebuild_generation_nonnegative"
    }
    assert {index["name"] for index in inspector.get_indexes("watermarks")} >= {
        "ix_watermarks_feed_rebuild"
    }

    with engine.connect() as connection:
        assert connection.execute(
            sa.text(
                """
                SELECT worker_id, instr_id, source, rebuild_from_ts, rebuild_generation
                FROM watermarks
                ORDER BY worker_id
                """
            )
        ).all() == [
            ("alias-worker", 11, "coinbase_live", None, 0),
            ("broker-worker", 11, "coinbase_live", None, 0),
            ("canonical-worker", 11, "coinbase_live", None, 0),
        ]
        assert connection.execute(sa.text("SELECT content_revision FROM prices")).scalar_one() == 1

    _run(engine, "downgrade")

    downgraded = sa.inspect(engine)
    assert {column["name"] for column in downgraded.get_columns("prices")} == {
        "price_id",
        "instr_id",
        "ts",
        "timeframe",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "source",
    }
    assert {column["name"] for column in downgraded.get_columns("watermarks")} == {
        "worker_id",
        "symbol",
        "timeframe",
        "last_ts",
        "updated_at",
    }
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT worker_id, symbol FROM watermarks ORDER BY worker_id")
        ).all() == [
            ("alias-worker", "xbt/usdc"),
            ("broker-worker", "BTC_USDC_PERP"),
            ("canonical-worker", "btc-usdc"),
        ]


@pytest.mark.parametrize(
    "sources",
    [
        (),
        ("coinbase_live", "deribit"),
    ],
    ids=["missing-source", "ambiguous-source"],
)
def test_upgrade_fails_closed_when_legacy_source_is_not_unique(
    sources: tuple[str, ...],
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    tables = _create_legacy_schema(engine)
    last_ts = _seed_single_feed(engine, tables, sources=sources)
    with engine.begin() as connection:
        connection.execute(
            tables["watermarks"]
            .insert()
            .values(
                worker_id="source-worker",
                symbol="BTCUSDC",
                timeframe="1m",
                last_ts=last_ts,
            )
        )

    with pytest.raises(RuntimeError, match="cannot attribute every existing watermark"):
        _run(engine, "upgrade")


def test_downgrade_refuses_pending_rebuild_marker() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    tables = _create_legacy_schema(engine)
    last_ts = _seed_single_feed(engine, tables)
    with engine.begin() as connection:
        connection.execute(
            tables["watermarks"]
            .insert()
            .values(
                worker_id="pending-worker",
                symbol="BTCUSDC",
                timeframe="1m",
                last_ts=last_ts,
            )
        )
    _run(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                UPDATE watermarks
                SET rebuild_from_ts = :rebuild_from_ts,
                    rebuild_generation = 1
                WHERE worker_id = 'pending-worker'
                """
            ),
            {"rebuild_from_ts": last_ts},
        )

    with pytest.raises(RuntimeError, match=r"rebuilds are pending \(1 watermark row"):
        _run(engine, "downgrade")

    columns = {column["name"] for column in sa.inspect(engine).get_columns("watermarks")}
    assert {
        "instr_id",
        "source",
        "rebuild_from_ts",
        "rebuild_generation",
    }.issubset(columns)
