"""Migration contracts for immutable point-in-time equity evidence revision 0086."""

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
    / "0086_equity_factor_evidence.py"
)
_TABLES = {
    "equity_source_lineages",
    "equity_observations",
    "equity_observation_values",
    "equity_factor_snapshots",
    "equity_factor_snapshot_details",
    "equity_factor_evidence",
    "equity_rank_snapshots",
    "equity_rank_snapshot_rows",
}


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "equity_factor_evidence_migration", _MIGRATION_PATH
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


def _create_previous_schema(engine: sa.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "instruments",
        metadata,
        sa.Column("instr_id", sa.Integer(), primary_key=True),
    )
    sa.Table(
        "strategies",
        metadata,
        sa.Column("strategy_id", sa.String(50), primary_key=True),
    )
    sa.Table(
        "strategy_versions",
        metadata,
        sa.Column("strat_ver_id", sa.BigInteger(), primary_key=True),
        sa.Column("strategy_id", sa.String(50), nullable=False),
        sa.UniqueConstraint(
            "strat_ver_id",
            "strategy_id",
            name="uq_strategy_version_lineage",
        ),
    )
    metadata.create_all(engine)


def test_revision_follows_equity_reference_grants() -> None:
    migration = _load_migration()

    assert migration.revision == "0086_equity_factor_evidence"
    assert migration.down_revision == "0085_equity_reference_grants"


def test_migration_creates_and_removes_equity_evidence_schema() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_previous_schema(engine)

    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    assert set(inspector.get_table_names()) >= _TABLES
    assert {index["name"] for index in inspector.get_indexes("equity_observations")} >= {
        "ix_equity_observation_instr_available",
        "ix_equity_observation_lineage",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("equity_rank_snapshots")
    } >= {"uq_equity_rank_factor_lineage"}
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("equity_factor_snapshots")
    } >= {"uq_equity_factor_snapshot_cutoff"}
    assert {
        "provider_authority_digest",
        "provider_authority_policy",
    } <= {column["name"] for column in inspector.get_columns("equity_rank_snapshots")}
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints("equity_factor_snapshots")
    } >= {
        "ck_equity_factor_completeness",
        "ck_equity_factor_content_identity",
        "ck_equity_factor_digests",
    }
    observation_kind_constraint = next(
        constraint["sqltext"]
        for constraint in inspector.get_check_constraints("equity_observations")
        if constraint["name"] == "ck_equity_observation_kind"
    )
    assert "analyst_estimate" not in observation_kind_constraint

    _run(engine, "downgrade")
    assert _TABLES.isdisjoint(sa.inspect(engine).get_table_names())


def test_migration_columns_match_orm_metadata() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_previous_schema(engine)
    _run(engine, "upgrade")
    inspector = sa.inspect(engine)

    for table_name in _TABLES:
        migrated = {
            column["name"]: column["nullable"] for column in inspector.get_columns(table_name)
        }
        modeled = {
            column.name: column.nullable for column in Base.metadata.tables[table_name].columns
        }
        if table_name == "equity_source_lineages":
            modeled.pop("entitlement_owner_user_id")
        if table_name == "equity_factor_snapshots":
            modeled.pop("source_contract_registry_sha256")
        assert migrated == modeled, f"model/migration column drift for {table_name}"


def test_downgrade_refuses_to_delete_immutable_evidence() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_previous_schema(engine)
    _run(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO equity_source_lineages (
                    lineage_id, provider, product, endpoint, dataset_version,
                    tool_version, source_identity, source_revision, retrieved_at,
                    timestamp_semantics, adjustment_policy, entitlement_scope,
                    missing_data_policy, content_sha256
                ) VALUES (
                    :lineage_id, 'sec', 'edgar-submissions',
                    'https://data.sec.gov/submissions/CIK0000320193.json',
                    'retrieved-2026-08-01', 'equity-evidence-v1',
                    'CIK0000320193/submissions', 'sha256-content',
                    '2026-08-01 12:00:00',
                    '{}', 'not-applicable', 'public-sec', 'fail-closed',
                    :content_sha256
                )
                """
            ),
            {"lineage_id": "a" * 64, "content_sha256": "b" * 64},
        )

    with pytest.raises(RuntimeError, match="Cannot remove immutable equity evidence"):
        _run(engine, "downgrade")


class _RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def test_postgresql_grants_are_least_privilege_and_rows_are_append_only() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._enable_postgresql_evidence_access()

    sql = "\n".join(recorder.statements)
    for role in ("vm_indicator", "vm_scoring"):
        assert any(
            statement.startswith("GRANT SELECT ") and statement.endswith(f"TO {role}")
            for statement in recorder.statements
        )
    assert (
        "GRANT SELECT, INSERT ON TABLE public.equity_source_lineages, "
        "public.equity_observations, public.equity_observation_values, "
        "public.equity_factor_snapshots, public.equity_factor_snapshot_details, "
        "public.equity_factor_evidence "
        "TO vm_market_data"
    ) in recorder.statements
    assert (
        "GRANT INSERT ON TABLE public.equity_rank_snapshots, "
        "public.equity_rank_snapshot_rows TO vm_indicator" in recorder.statements
    )
    assert (
        "GRANT INSERT ON TABLE public.equity_rank_snapshots, "
        "public.equity_rank_snapshot_rows TO vm_scoring" not in recorder.statements
    )
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    assert "ENABLE ROW LEVEL SECURITY" not in sql
    assert sql.count("BEFORE UPDATE OR DELETE") == len(_TABLES)
    assert "CREATE FUNCTION public.vm_require_append_only_parent_current_xact()" in sql
    assert "xmin = pg_current_xact_id()::xid" in sql
    for child, (parent, parent_key, child_key) in migration._SEALED_AGGREGATES.items():
        assert (
            f"CREATE TRIGGER trg_{child}_aggregate_sealed "
            f"BEFORE INSERT ON public.{child} FOR EACH ROW "
            "EXECUTE FUNCTION public.vm_require_append_only_parent_current_xact"
            f"('{parent}', '{parent_key}', '{child_key}')"
        ) in recorder.statements
    assert (
        "CREATE CONSTRAINT TRIGGER trg_equity_factor_snapshot_complete "
        "AFTER INSERT ON public.equity_factor_snapshots "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_equity_factor_snapshot_complete()" in recorder.statements
    )
    assert (
        "CREATE CONSTRAINT TRIGGER trg_equity_rank_snapshot_complete "
        "AFTER INSERT ON public.equity_rank_snapshots "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_equity_rank_snapshot_complete()" in recorder.statements
    )
    assert "enabled_count <> NEW.expected_factor_count" in sql
    assert "available_count <> NEW.available_factor_count" in sql
    assert "complete_without_evidence" in sql
    assert "row_count <> NEW.expected_instrument_count" in sql


def test_downgrade_revokes_reference_table_privileges_introduced_by_revision() -> None:
    source = _MIGRATION_PATH.read_text(encoding="utf-8")

    assert "REVOKE SELECT ON TABLE public.strategies, public.strategy_versions" in source
    assert "FROM vm_market_data, vm_indicator" in source
