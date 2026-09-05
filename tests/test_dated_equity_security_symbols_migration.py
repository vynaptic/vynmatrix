"""Migration contracts for validity-dated equity symbols."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lib_application.db.models import Base

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0091_dated_equity_security_symbols.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dated_equity_symbols_migration", _MIGRATION_PATH)
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


def test_revision_follows_optional_factor_contracts() -> None:
    migration = _load_migration()

    assert migration.revision == "0091_dated_security_symbols"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0090_optional_factor_contracts"


def _previous_schema(engine: sa.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "instruments",
        metadata,
        sa.Column("instr_id", sa.Integer(), primary_key=True),
        sa.Column("canonical", sa.String(50), nullable=False),
    )
    sa.Table(
        "equity_security_identities",
        metadata,
        sa.Column("identity_id", sa.Integer(), primary_key=True),
        sa.Column("security_id", sa.String(100), nullable=False),
        sa.Column("issuer_id", sa.String(100), nullable=False),
        sa.Column("instr_id", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("source_ref", sa.String(200), nullable=False),
        sa.Column("observation_id", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "length(trim(security_id)) > 0 AND length(trim(issuer_id)) > 0 "
            "AND length(trim(source_ref)) > 0 AND length(observation_id) = 64",
            name="ck_equity_security_identity_values",
        ),
    )
    metadata.create_all(engine)


def test_migration_backfills_symbol_and_matches_model() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _previous_schema(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO instruments VALUES (1, 'AAA')"))
        connection.execute(
            sa.text(
                "INSERT INTO equity_security_identities VALUES "
                "(1, 'SEC-1', 'ISSUER-1', 1, '2020-01-01', NULL, 'source', :digest)"
            ),
            {"digest": "a" * 64},
        )

    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    migrated = {
        column["name"]: column["nullable"]
        for column in inspector.get_columns("equity_security_identities")
    }
    modeled = {
        column.name: column.nullable
        for column in Base.metadata.tables["equity_security_identities"].columns
    }
    assert migrated == modeled
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT canonical_symbol FROM equity_security_identities"))
            == "AAA"
        )
    _run(engine, "downgrade")
    assert "canonical_symbol" not in {
        column["name"] for column in sa.inspect(engine).get_columns("equity_security_identities")
    }


def test_migration_rejects_ambiguous_existing_intervals() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _previous_schema(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO instruments VALUES (1, 'AAA')"))
        connection.execute(
            sa.text(
                "INSERT INTO equity_security_identities VALUES "
                "(1, 'SEC-1', 'ISSUER-1', 1, '2020-01-01', '2020-12-31', 'a', :a), "
                "(2, 'SEC-1', 'ISSUER-1', 1, '2020-06-01', NULL, 'b', :b)"
            ),
            {"a": "a" * 64, "b": "b" * 64},
        )

    with pytest.raises(RuntimeError, match="intervals overlap"):
        _run(engine, "upgrade")


class _RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def test_postgresql_overlap_guard_covers_security_and_instrument() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder
    migration._install_postgresql_overlap_guard()

    sql = "\n".join(recorder.statements)
    assert "existing.security_id = NEW.security_id" in sql
    assert "existing.instr_id = NEW.instr_id" in sql
    assert "trg_equity_security_identity_no_overlap" in sql
