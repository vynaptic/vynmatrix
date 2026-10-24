"""Storage contracts for the adverse-news gate (migration 0111)."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from lib_application.db.models.equity_observations import EquityModelInferenceRecord

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0111_adverse_news_gate_storage.py"
)
_PREVIOUS_KIND_CHECK = (
    "observation_kind IN ('price', 'corporate_action', 'membership', "
    "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
    "'market_cap', 'security_identity', 'analyst_estimate', 'news_event', "
    "'call_transcript', 'insider_transaction', 'positioning', 'macro_release', "
    "'factor_risk_exposure')"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("adverse_news_gate_storage", _MIGRATION_PATH)
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
    sa.Table("users", metadata, sa.Column("user_id", sa.String(50), primary_key=True))
    sa.Table(
        "equity_source_lineages",
        metadata,
        sa.Column("lineage_id", sa.String(64), primary_key=True),
    )
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


def _insert_inference(engine: sa.Engine, scope: str = "scope") -> None:
    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = OFF"))
        connection.execute(
            sa.text(
                "INSERT INTO equity_model_inference_records "
                "(model_input_sha256, lineage_id, model_output_sha256, request_json, "
                "response_json, request_bytes, response_bytes, received_at, entitlement_scope) "
                "VALUES (:i, :l, :o, '{}', '{}', x'01', x'02', '2026-01-01', :s)"
            ),
            {"i": "1" * 64, "l": "2" * 64, "o": "3" * 64, "s": scope},
        )


def test_revision_chain() -> None:
    migration = _load_migration()
    assert migration.revision == "0111_adverse_news_gate_storage"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0110_asset_class_binding_scope"


def test_upgrade_allows_news_coverage_kind() -> None:
    engine = _engine()
    with pytest.raises(sa.exc.IntegrityError):
        _insert_observation(engine, "news_coverage")

    _run(engine, "upgrade")

    _insert_observation(engine, "news_coverage")
    assert "equity_model_inference_records" in sa.inspect(engine).get_table_names()


def test_inference_record_is_append_only() -> None:
    engine = _engine()
    _run(engine, "upgrade")
    record = EquityModelInferenceRecord(
        model_input_sha256="1" * 64,
        lineage_id="2" * 64,
        model_output_sha256="3" * 64,
        request_json={},
        response_json={},
        request_bytes=b"{}",
        response_bytes=b"{}",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        entitlement_scope="scope",
    )
    with Session(engine) as session:
        session.execute(sa.text("PRAGMA foreign_keys = OFF"))
        session.add(record)
        session.commit()
        record.entitlement_scope = "changed"
        with pytest.raises(ValueError, match="immutable"):
            session.flush()
        session.rollback()
        session.delete(record)
        with pytest.raises(ValueError, match="append-only"):
            session.flush()


def test_blank_entitlement_scope_is_rejected() -> None:
    engine = _engine()
    _run(engine, "upgrade")

    with pytest.raises(sa.exc.IntegrityError):
        _insert_inference(engine, scope="   ")


def test_downgrade_refuses_when_news_coverage_rows_exist() -> None:
    engine = _engine()
    _run(engine, "upgrade")
    _insert_observation(engine, "news_coverage")

    with pytest.raises(RuntimeError, match="Cannot remove adverse-news gate storage"):
        _run(engine, "downgrade")


def test_downgrade_refuses_when_inference_records_exist() -> None:
    engine = _engine()
    _run(engine, "upgrade")
    _insert_inference(engine)

    with pytest.raises(RuntimeError, match="Cannot remove adverse-news gate storage"):
        _run(engine, "downgrade")


def test_downgrade_restores_previous_kind_check() -> None:
    engine = _engine()
    _run(engine, "upgrade")
    _run(engine, "downgrade")

    inspector = sa.inspect(engine)
    assert "equity_model_inference_records" not in inspector.get_table_names()
    check = next(
        constraint["sqltext"]
        for constraint in inspector.get_check_constraints("equity_observations")
        if constraint["name"] == "ck_equity_observation_kind"
    )
    assert "news_coverage" not in check
    assert "factor_risk_exposure" in check
