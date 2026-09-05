"""Migration contracts for durable synchronized-panel revision 0087."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

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
    / "0087_synchronized_panel_runtime.py"
)
_NEW_TABLES = {
    "equity_security_identities",
    "strategy_panel_input_revisions",
    "strategy_panel_decisions",
}
_LATER_COLUMNS = {
    "equity_security_identities": {"canonical_symbol"},
    "strategy_panel_input_revisions": {"entitlement_owner_user_id"},
    "strategy_panel_decisions": {
        "activation_cutoff",
        "entitlement_owner_user_id",
        "runtime_environment",
    },
}


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "synchronized_panel_runtime_migration", _MIGRATION_PATH
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
        "strategy_runtime_states",
        metadata,
        sa.Column("worker_id", sa.String(100), primary_key=True),
    )
    sa.Table(
        "equity_rank_snapshots",
        metadata,
        sa.Column("rank_snapshot_id", sa.String(64), primary_key=True),
    )
    sa.Table(
        "equity_observations",
        metadata,
        sa.Column("observation_id", sa.String(64), primary_key=True),
        sa.Column("observation_kind", sa.String(30), nullable=False),
        sa.CheckConstraint(
            "observation_kind IN ('price', 'corporate_action', 'membership', "
            "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
            "'market_cap')",
            name="ck_equity_observation_kind",
        ),
    )
    sa.Table(
        "index_membership",
        metadata,
        sa.Column("membership_id", sa.Integer(), primary_key=True),
        sa.Column("index_code", sa.String(20), nullable=False),
        sa.Column("instr_id", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("source_ref", sa.String(200), nullable=False),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_index_membership_span",
        ),
    )
    sa.Table(
        "market_calendars",
        metadata,
        sa.Column("calendar_id", sa.Integer(), primary_key=True),
    )
    metadata.create_all(engine)


def test_migration_round_trips_and_matches_new_model_columns() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _create_previous_schema(engine)

    _run(engine, "upgrade")
    inspector = sa.inspect(engine)
    assert set(inspector.get_table_names()) >= _NEW_TABLES
    for table_name in _NEW_TABLES:
        migrated = {
            column["name"]: column["nullable"] for column in inspector.get_columns(table_name)
        }
        modeled = {
            column.name: column.nullable
            for column in Base.metadata.tables[table_name].columns
            if column.name not in _LATER_COLUMNS.get(table_name, set())
        }
        assert migrated == modeled, f"model/migration column drift for {table_name}"
    assert "observation_id" in {
        column["name"] for column in inspector.get_columns("index_membership")
    }
    assert "observation_id" in {
        column["name"] for column in inspector.get_columns("market_calendars")
    }
    observation_kind_constraint = next(
        constraint["sqltext"]
        for constraint in inspector.get_check_constraints("equity_observations")
        if constraint["name"] == "ck_equity_observation_kind"
    )
    assert "security_identity" in observation_kind_constraint
    assert "analyst_estimate" not in observation_kind_constraint

    _run(engine, "downgrade")
    downgraded = sa.inspect(engine)
    assert _NEW_TABLES.isdisjoint(downgraded.get_table_names())
    assert "observation_id" not in {
        column["name"] for column in downgraded.get_columns("index_membership")
    }
    membership_span = next(
        constraint["sqltext"]
        for constraint in downgraded.get_check_constraints("index_membership")
        if constraint["name"] == "ck_index_membership_span"
    )
    assert ">=" not in membership_span
    downgraded_observation_kinds = next(
        constraint["sqltext"]
        for constraint in downgraded.get_check_constraints("equity_observations")
        if constraint["name"] == "ck_equity_observation_kind"
    )
    assert "security_identity" not in downgraded_observation_kinds
    assert "analyst_estimate" not in downgraded_observation_kinds


class _RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def test_postgresql_access_is_append_only_and_batch_topic_is_allowed() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._enable_postgresql_runtime_access()

    sql = "\n".join(recorder.statements)
    assert (
        "GRANT SELECT ON TABLE public.equity_security_identities TO vm_indicator"
        in recorder.statements
    )
    assert (
        "GRANT SELECT, INSERT ON TABLE public.equity_security_identities TO vm_market_data"
        in recorder.statements
    )
    assert (
        "GRANT USAGE, SELECT ON SEQUENCE "
        "public.equity_security_identities_identity_id_seq TO vm_market_data" in recorder.statements
    )
    assert "trg_equity_security_identities_immutable" in sql
    assert "signals.rebalances.submit" in sql
    assert (
        "GRANT SELECT ON TABLE public.strategy_panel_decisions TO vm_scoring" in recorder.statements
    )
    assert (
        "CREATE POLICY strategy_panel_decisions_scoring_select ON "
        "strategy_panel_decisions FOR SELECT TO vm_scoring "
        "USING (status = 'completed')" in recorder.statements
    )
    assert "GRANT DELETE" not in sql
