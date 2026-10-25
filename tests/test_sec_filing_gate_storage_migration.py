"""Storage contracts for the SEC filing event gate (migration 0112)."""

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
    / "0112_sec_filing_gate_storage.py"
)
_PREVIOUS_KIND_CHECK = (
    "observation_kind IN ('price', 'corporate_action', 'membership', "
    "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
    "'market_cap', 'security_identity', 'analyst_estimate', 'news_event', "
    "'call_transcript', 'insider_transaction', 'positioning', 'macro_release', "
    "'factor_risk_exposure', 'news_coverage')"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sec_filing_gate_storage", _MIGRATION_PATH)
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


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    metadata = sa.MetaData()
    sa.Table(
        "equity_observations",
        metadata,
        sa.Column("observation_id", sa.String(64), primary_key=True),
        sa.Column("observation_kind", sa.String(30), nullable=False),
        sa.CheckConstraint(_PREVIOUS_KIND_CHECK, name="ck_equity_observation_kind"),
    )
    metadata.create_all(engine)
    return engine


def _insert_observation(engine: sa.Engine, kind: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO equity_observations (observation_id, observation_kind) "
                "VALUES (:observation_id, :kind)"
            ),
            {"observation_id": "a" * 64, "kind": kind},
        )


def test_revision_chain() -> None:
    migration = _load_migration()
    assert migration.revision == "0112_sec_filing_gate_storage"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0111_adverse_news_gate_storage"


def test_upgrade_allows_filing_coverage_kind() -> None:
    engine = _engine()
    with pytest.raises(sa.exc.IntegrityError):
        _insert_observation(engine, "filing_coverage")

    _run(engine, "upgrade")

    _insert_observation(engine, "filing_coverage")


def test_downgrade_refuses_when_filing_coverage_rows_exist() -> None:
    engine = _engine()
    _run(engine, "upgrade")
    _insert_observation(engine, "filing_coverage")

    with pytest.raises(
        RuntimeError, match="Cannot remove SEC filing gate storage after evidence exists"
    ):
        _run(engine, "downgrade")


def test_downgrade_restores_previous_kind_check() -> None:
    engine = _engine()
    _run(engine, "upgrade")
    _run(engine, "downgrade")

    check = next(
        constraint["sqltext"]
        for constraint in sa.inspect(engine).get_check_constraints("equity_observations")
        if constraint["name"] == "ck_equity_observation_kind"
    )
    assert "filing_coverage" not in check
    assert "news_coverage" in check
    with pytest.raises(sa.exc.IntegrityError):
        _insert_observation(engine, "filing_coverage")
