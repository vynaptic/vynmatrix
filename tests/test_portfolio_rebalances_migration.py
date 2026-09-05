"""Migration contracts for atomic model and account rebalance aggregates."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from lib_application.db.models import (
    AccountExecutionGeneration,
    AccountRebalancePlan,
    AccountRebalancePlanLeg,
    AccountRebalancePlanResolution,
    EquityRankSnapshot,
    ModelRebalance,
    ModelRebalanceLeg,
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0088_portfolio_rebalances.py"
)
_FENCE_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0089_account_execution_fence.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("portfolio_rebalances_migration", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_fence_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "account_execution_fence_migration",
        _FENCE_MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))

    @staticmethod
    def get_bind() -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))


def test_revision_follows_synchronized_panel_runtime() -> None:
    migration = _load_migration()

    assert migration.revision == "0088_portfolio_rebalances"
    assert migration.down_revision == "0087_synchronized_panel_runtime"


def test_account_execution_fence_is_forward_only_after_deployed_0088() -> None:
    portfolio_migration = _load_migration()
    fence_migration = _load_fence_migration()
    portfolio_source = _MIGRATION_PATH.read_text(encoding="utf-8")
    fence_source = _FENCE_MIGRATION_PATH.read_text(encoding="utf-8")
    forward_only_identifiers = {
        "account_execution_generations",
        "completion_account_generation",
        "terminal_targets_sha256",
        "frozen_target_quantity",
        "target_account_generation",
        "target_frozen_at",
        "ck_account_rebalance_plan_terminal_fence",
        "ck_account_rebalance_leg_target_fence",
    }

    assert portfolio_migration.revision == "0088_portfolio_rebalances"
    assert fence_migration.revision == "0089_account_execution_fence"
    assert fence_migration.down_revision == portfolio_migration.revision
    assert all(identifier not in portfolio_source for identifier in forward_only_identifiers)
    assert all(identifier in fence_source for identifier in forward_only_identifiers)
    assert "NOT VALID" in fence_source
    assert "VALIDATE CONSTRAINT" in fence_source


def _named_constraint(table: sa.Table, name: str) -> sa.Constraint:
    return next(constraint for constraint in table.constraints if constraint.name == name)


def test_exact_rebalance_lineage_constraints_match_migration_and_models() -> None:
    source = "\n".join(
        (
            _MIGRATION_PATH.read_text(encoding="utf-8"),
            _FENCE_MIGRATION_PATH.read_text(encoding="utf-8"),
        )
    )
    names = {
        "uq_equity_rank_exact_model_lineage",
        "fk_model_rebalance_exact_rank_lineage",
        "uq_model_rebalance_exact_account_lineage",
        "fk_account_rebalance_plan_exact_model_lineage",
        "uq_model_rebalance_leg_exact_account_identity",
        "uq_model_rebalance_leg_exact_account_rank",
        "uq_model_rebalance_leg_prior_identity",
        "fk_model_rebalance_leg_prior_identity",
        "fk_account_rebalance_leg_exact_model_identity",
        "fk_account_rebalance_leg_exact_model_rank",
        "uq_account_rebalance_plan_owner_status",
        "fk_account_rebalance_resolution_exact_failed_plan",
        "fk_account_execution_generation_owner",
        "ck_account_rebalance_plan_terminal_fence",
        "ck_account_rebalance_leg_target_fence",
    }
    assert all(name in source for name in names)

    expected: dict[tuple[sa.Table, str], tuple[str, ...]] = {
        (
            EquityRankSnapshot.__table__,
            "uq_equity_rank_exact_model_lineage",
        ): (
            "rank_snapshot_id",
            "strategy_id",
            "strat_ver_id",
            "effective_session",
            "cutoff_at",
            "configuration_digest",
            "data_use_scope",
            "provider_authority_digest",
        ),
        (
            ModelRebalance.__table__,
            "fk_model_rebalance_exact_rank_lineage",
        ): (
            "rank_snapshot_id",
            "strategy_id",
            "strat_ver_id",
            "effective_session",
            "decision_cutoff",
            "configuration_sha256",
            "data_use_scope",
            "provider_authority_sha256",
        ),
        (
            ModelRebalance.__table__,
            "uq_model_rebalance_exact_account_lineage",
        ): (
            "rebalance_id",
            "strategy_id",
            "data_use_scope",
            "provider_authority_sha256",
            "decision_cutoff",
            "execute_not_before",
            "execution_session_sha256",
            "expected_leg_count",
        ),
        (
            AccountRebalancePlan.__table__,
            "fk_account_rebalance_plan_exact_model_lineage",
        ): (
            "model_rebalance_id",
            "strategy_id",
            "data_use_scope",
            "provider_authority_sha256",
            "decision_cutoff",
            "execute_not_before",
            "execution_session_sha256",
            "model_leg_count",
        ),
        (
            ModelRebalanceLeg.__table__,
            "uq_model_rebalance_leg_prior_identity",
        ): (
            "rebalance_id",
            "leg_id",
            "instr_id",
            "factor_snapshot_id",
        ),
        (
            ModelRebalanceLeg.__table__,
            "fk_model_rebalance_leg_prior_identity",
        ): (
            "prior_model_rebalance_id",
            "prior_model_leg_id",
            "instr_id",
            "factor_snapshot_id",
        ),
        (
            ModelRebalanceLeg.__table__,
            "uq_model_rebalance_leg_exact_account_identity",
        ): (
            "rebalance_id",
            "sequence",
            "leg_id",
            "instr_id",
            "external_signal_id",
            "signal_snapshot_sha256",
        ),
        (
            ModelRebalanceLeg.__table__,
            "uq_model_rebalance_leg_exact_account_rank",
        ): (
            "rebalance_id",
            "sequence",
            "leg_id",
            "instr_id",
            "external_signal_id",
            "signal_snapshot_sha256",
            "rank_position",
        ),
        (
            AccountRebalancePlanLeg.__table__,
            "fk_account_rebalance_leg_exact_model_identity",
        ): (
            "model_rebalance_id",
            "model_sequence",
            "model_leg_id",
            "instr_id",
            "external_signal_id",
            "model_signal_snapshot_sha256",
        ),
        (
            AccountRebalancePlanLeg.__table__,
            "fk_account_rebalance_leg_exact_model_rank",
        ): (
            "model_rebalance_id",
            "model_sequence",
            "model_leg_id",
            "instr_id",
            "external_signal_id",
            "model_signal_snapshot_sha256",
            "rank_position",
        ),
        (
            AccountRebalancePlan.__table__,
            "uq_account_rebalance_plan_owner_status",
        ): (
            "account_plan_id",
            "user_id",
            "broker_account_id",
            "status",
        ),
        (
            AccountRebalancePlanResolution.__table__,
            "fk_account_rebalance_resolution_exact_failed_plan",
        ): (
            "account_plan_id",
            "user_id",
            "broker_account_id",
            "plan_status",
        ),
        (
            AccountExecutionGeneration.__table__,
            "fk_account_execution_generation_owner",
        ): (
            "broker_account_id",
            "user_id",
        ),
    }
    for (table, name), columns in expected.items():
        constraint = _named_constraint(table, name)
        assert tuple(column.name for column in constraint.columns) == columns

    prior_identity = _named_constraint(
        ModelRebalanceLeg.__table__,
        "fk_model_rebalance_leg_prior_identity",
    )
    assert isinstance(prior_identity, sa.ForeignKeyConstraint)
    assert tuple(element.target_fullname for element in prior_identity.elements) == (
        "model_rebalance_legs.rebalance_id",
        "model_rebalance_legs.leg_id",
        "model_rebalance_legs.instr_id",
        "model_rebalance_legs.factor_snapshot_id",
    )

    assert {"signal_snapshot", "signal_snapshot_sha256"} <= set(
        ModelRebalanceLeg.__table__.c.keys()
    )
    assert "model_signal_snapshot_sha256" in AccountRebalancePlanLeg.__table__.c
    assert {
        "completion_account_generation",
        "terminal_targets_sha256",
    } <= set(AccountRebalancePlan.__table__.c.keys())
    assert {
        "frozen_target_quantity",
        "target_account_generation",
        "target_frozen_at",
    } <= set(AccountRebalancePlanLeg.__table__.c.keys())
    assert {
        "generation",
        "active_owner",
        "active_writer_kind",
        "active_account_plan_id",
    } <= set(AccountExecutionGeneration.__table__.c.keys())


def test_rebalance_leg_phase_action_semantics_match_migration_and_models() -> None:
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    model_semantics = str(
        _named_constraint(
            ModelRebalanceLeg.__table__,
            "ck_model_rebalance_leg_phase_action",
        ).sqltext
    )
    account_semantics = str(
        _named_constraint(
            AccountRebalancePlanLeg.__table__,
            "ck_account_rebalance_leg_selection",
        ).sqltext
    )

    assert "(phase = 'entry' AND action = 'long')" in model_semantics
    assert "(phase IN ('hold', 'reduce') AND action = 'hold')" in model_semantics
    assert "(phase = 'exit' AND action = 'flat')" in model_semantics
    assert "ck_model_rebalance_leg_phase_action" in source
    assert "disposition = 'selected' AND required" in account_semantics
    assert "command_sequence IS NOT NULL AND action = phase" in account_semantics
    assert "disposition = 'rejected' AND NOT required" in account_semantics
    assert "command_sequence IS NULL AND action = 'reject'" in account_semantics
    assert "ck_account_rebalance_leg_selection" in source
    assert '"AND command_sequence IS NOT NULL AND action = phase) OR "' in source
    assert "\"AND command_sequence IS NULL AND action = 'reject')\"" in source


def test_rebalance_leg_phase_action_checks_reject_semantic_mismatches() -> None:
    metadata = sa.MetaData()
    model_leg = sa.Table(
        "model_leg_semantics",
        metadata,
        sa.Column("leg_id", sa.Integer(), primary_key=True),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.CheckConstraint(
            str(
                _named_constraint(
                    ModelRebalanceLeg.__table__,
                    "ck_model_rebalance_leg_phase_action",
                ).sqltext
            )
        ),
    )
    account_leg = sa.Table(
        "account_leg_semantics",
        metadata,
        sa.Column("leg_id", sa.Integer(), primary_key=True),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("disposition", sa.String(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("command_sequence", sa.Integer()),
        sa.CheckConstraint(
            str(
                _named_constraint(
                    AccountRebalancePlanLeg.__table__,
                    "ck_account_rebalance_leg_selection",
                ).sqltext
            )
        ),
    )
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            model_leg.insert(),
            [
                {"leg_id": 1, "phase": "entry", "action": "long"},
                {"leg_id": 2, "phase": "hold", "action": "hold"},
                {"leg_id": 3, "phase": "reduce", "action": "hold"},
                {"leg_id": 4, "phase": "exit", "action": "flat"},
            ],
        )
        connection.execute(
            account_leg.insert(),
            [
                {
                    "leg_id": 1,
                    "phase": "entry",
                    "action": "entry",
                    "disposition": "selected",
                    "required": True,
                    "command_sequence": 0,
                },
                {
                    "leg_id": 2,
                    "phase": "entry",
                    "action": "reject",
                    "disposition": "rejected",
                    "required": False,
                    "command_sequence": None,
                },
            ],
        )
    for leg_id, phase, action in (
        (10, "entry", "hold"),
        (11, "hold", "long"),
        (12, "reduce", "flat"),
        (13, "exit", "hold"),
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(model_leg.insert().values(leg_id=leg_id, phase=phase, action=action))
    invalid_account_rows = (
        {
            "leg_id": 10,
            "phase": "entry",
            "action": "entry",
            "disposition": "selected",
            "required": False,
            "command_sequence": 0,
        },
        {
            "leg_id": 11,
            "phase": "entry",
            "action": "exit",
            "disposition": "selected",
            "required": True,
            "command_sequence": 1,
        },
        {
            "leg_id": 12,
            "phase": "exit",
            "action": "reject",
            "disposition": "rejected",
            "required": True,
            "command_sequence": None,
        },
        {
            "leg_id": 13,
            "phase": "exit",
            "action": "exit",
            "disposition": "rejected",
            "required": False,
            "command_sequence": None,
        },
    )
    for row in invalid_account_rows:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(account_leg.insert().values(**row))


def test_postgresql_rebalance_grants_preserve_atomic_service_role_ownership() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._enable_service_access()

    sql = "\n".join(recorder.statements)
    for table in (*migration._MODEL_TABLES, *migration._ACCOUNT_TABLES):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in recorder.statements
        assert f"GRANT SELECT, INSERT ON TABLE public.{table} TO vm_scoring" in recorder.statements
    for table in migration._MODEL_TABLES:
        assert f"GRANT SELECT ON TABLE public.{table} TO vm_execution" in recorder.statements
        assert f"GRANT SELECT, UPDATE ON TABLE public.{table} TO vm_execution" not in sql
    for table in migration._ACCOUNT_TABLES:
        assert (
            f"GRANT SELECT, UPDATE ON TABLE public.{table} TO vm_execution" in recorder.statements
        )
    for table in migration._RESOLUTION_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in recorder.statements
        assert f"GRANT SELECT, INSERT ON TABLE public.{table} TO vm_execution" in (
            recorder.statements
        )
        assert f"GRANT SELECT, INSERT ON TABLE public.{table} TO vm_scoring" not in sql
        assert f"GRANT SELECT, UPDATE ON TABLE public.{table} TO vm_execution" not in sql
    assert "GRANT UPDATE ON TABLE public.account_rebalance_plans TO vm_scoring" not in sql
    assert "GRANT INSERT ON TABLE public.account_rebalance_plan_legs TO vm_execution" not in sql


def test_postgresql_account_execution_fence_access_is_execution_only() -> None:
    migration = _load_fence_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._enable_postgresql_execution_fence_access()

    table = migration._EXECUTION_FENCE_TABLE
    sql = "\n".join(recorder.statements)
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in recorder.statements
    assert (
        f"GRANT SELECT, INSERT, UPDATE ON TABLE public.{table} TO vm_execution"
        in recorder.statements
    )
    for command in ("select", "insert", "update"):
        assert f"CREATE POLICY {table}_execution_{command}" in sql
    assert "TO vm_scoring" not in sql


def test_postgresql_failure_resolutions_are_append_only() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._create_resolution_protection_trigger()

    sql = "\n".join(recorder.statements)
    assert "BEFORE UPDATE OR DELETE ON account_rebalance_plan_resolutions" in sql
    assert "account rebalance failure resolutions are append-only" in sql
    assert "USING ERRCODE = '55000'" in sql


def test_postgresql_rebalance_triggers_seal_aggregates_and_terminal_progress() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._create_protection_triggers()

    sql = "\n".join(recorder.statements)
    for child, (parent, parent_key, child_key) in migration._SEALED_AGGREGATES.items():
        assert (
            f"CREATE TRIGGER {child}_aggregate_sealed "
            f"BEFORE INSERT ON {child} FOR EACH ROW "
            "EXECUTE FUNCTION public.vm_require_append_only_parent_current_xact"
            f"('{parent}', '{parent_key}', '{child_key}')"
        ) in recorder.statements
    assert "BEFORE INSERT OR UPDATE ON account_rebalance_plans" in sql
    assert "BEFORE INSERT OR UPDATE ON account_rebalance_plan_legs" in sql
    assert "terminal account rebalance plan progress is immutable" in sql
    assert "terminal account rebalance leg progress is immutable" in sql
    assert "must begin in pristine exit/pending state" in sql
    assert "must begin in pristine pending state" in sql
    assert "invalid account rebalance plan transition" in sql
    assert "invalid account rebalance leg transition" in sql
    assert "NEW.phase = 'complete'" in sql
    assert "NEW.status IN ('completed', 'degraded')" in sql
    assert "NEW.phase = 'expired' AND NEW.status = 'expired'" in sql
    assert "NEW.phase = 'failed' AND NEW.status IN ('failed', 'cancelled')" in sql
    assert "NEW.status IN ('running', 'blocked', 'failed', 'cancelled')" not in sql
    assert "USING ERRCODE = '55000'" in sql
    assert "USING ERRCODE = '23514'" in sql
    assert (
        "CREATE CONSTRAINT TRIGGER model_rebalance_complete "
        "AFTER INSERT ON model_rebalances "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_model_rebalance_complete()" in recorder.statements
    )
    assert (
        "CREATE CONSTRAINT TRIGGER account_rebalance_plan_complete "
        "AFTER INSERT ON account_rebalance_plans "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_account_rebalance_plan_complete()" in recorder.statements
    )
    assert (
        "CREATE CONSTRAINT TRIGGER account_rebalance_plan_terminal_coherent "
        "AFTER INSERT OR UPDATE ON account_rebalance_plans "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_account_rebalance_terminal_coherent()"
        in recorder.statements
    )
    assert "leg_count <> NEW.expected_leg_count" in sql
    assert "leg_count <> NEW.model_leg_count" in sql
    assert "selected_count <> NEW.execution_leg_count" in sql
    assert "terminal account rebalance plan % has nonterminal legs" in sql
    assert "completed account rebalance plan % has incoherent leg outcomes" in sql
    assert "degraded account rebalance plan % has incoherent leg outcomes" in sql
    assert "completion_account_generation" not in sql
    assert "terminal_targets_sha256" not in sql
    assert "frozen_target_quantity" not in sql
    assert "target_account_generation" not in sql


def test_postgresql_forward_migration_replaces_terminal_fence_guards() -> None:
    migration = _load_fence_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._replace_postgresql_rebalance_guards(target_fenced=True)

    sql = "\n".join(recorder.statements)
    assert len(recorder.statements) == 3
    assert all("CREATE OR REPLACE FUNCTION" in statement for statement in recorder.statements)
    assert "completion_account_generation" in sql
    assert "terminal_targets_sha256" in sql
    assert "frozen_target_quantity" in sql
    assert "target_account_generation" in sql
    assert "frozen account rebalance target is immutable" in sql
    assert "has unfrozen target quantities" in sql


def test_forward_migration_upgrades_populated_0088_shape_without_rewriting_rows() -> None:
    migration = _load_fence_migration()
    metadata = sa.MetaData()
    sa.Table(
        "linked_broker_accounts",
        metadata,
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint("account_id", "user_id"),
    )
    sa.Table(
        "account_rebalance_plans",
        metadata,
        sa.Column("account_plan_id", sa.String(length=64), primary_key=True),
        sa.Column("status", sa.String(length=24), nullable=False),
    )
    sa.Table(
        "account_rebalance_plan_legs",
        metadata,
        sa.Column("account_plan_id", sa.String(length=64), nullable=False),
        sa.Column("model_sequence", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("account_plan_id", "model_sequence"),
    )
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    metadata.create_all(engine)
    plan_id = "a" * 64
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO linked_broker_accounts (account_id, user_id) VALUES (17, 'tenant-a')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO account_rebalance_plans (account_plan_id, status) "
                "VALUES (:plan_id, 'pending')"
            ),
            {"plan_id": plan_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO account_rebalance_plan_legs "
                "(account_plan_id, model_sequence) VALUES (:plan_id, 0)"
            ),
            {"plan_id": plan_id},
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

    inspector = sa.inspect(engine)
    assert "account_execution_generations" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("account_rebalance_plans")} >= {
        "completion_account_generation",
        "terminal_targets_sha256",
    }
    assert {column["name"] for column in inspector.get_columns("account_rebalance_plan_legs")} >= {
        "frozen_target_quantity",
        "target_account_generation",
        "target_frozen_at",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints("account_rebalance_plans")
    } == {"ck_account_rebalance_plan_terminal_fence"}
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints("account_rebalance_plan_legs")
    } == {"ck_account_rebalance_leg_target_fence"}
    with engine.connect() as connection:
        assert connection.execute(
            sa.text(
                "SELECT account_plan_id, completion_account_generation, "
                "terminal_targets_sha256 FROM account_rebalance_plans"
            )
        ).one() == (plan_id, None, None)
        assert connection.execute(
            sa.text(
                "SELECT account_plan_id, model_sequence, frozen_target_quantity, "
                "target_account_generation, target_frozen_at "
                "FROM account_rebalance_plan_legs"
            )
        ).one() == (plan_id, 0, None, None, None)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO account_execution_generations "
                "(user_id, broker_account_id, generation) VALUES ('tenant-a', 17, 1)"
            )
        )
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="immutable account execution fence history"):
            migration.downgrade()
        connection.execute(sa.text("DELETE FROM account_execution_generations"))

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()

    inspector = sa.inspect(engine)
    assert "account_execution_generations" not in inspector.get_table_names()
    assert "completion_account_generation" not in {
        column["name"] for column in inspector.get_columns("account_rebalance_plans")
    }
    assert "frozen_target_quantity" not in {
        column["name"] for column in inspector.get_columns("account_rebalance_plan_legs")
    }
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM account_rebalance_plans WHERE account_plan_id = :plan_id"
                ),
                {"plan_id": plan_id},
            )
            == 1
        )


def _postgres_url() -> sa.URL:
    raw = os.getenv("DATABASE_URL")
    if not raw:
        pytest.skip("DATABASE_URL is required for PostgreSQL aggregate-sealing acceptance")
    url = make_url(raw)
    if not url.drivername.startswith("postgresql"):
        pytest.skip("aggregate-sealing acceptance requires PostgreSQL")
    return url


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)


def _create_completeness_probe_schema(connection: sa.Connection, schema: str) -> None:
    statements = (
        "CREATE TABLE equity_factor_snapshots ("
        "factor_snapshot_id text PRIMARY KEY, expected_factor_count integer NOT NULL, "
        "available_factor_count integer NOT NULL)",
        "CREATE TABLE equity_factor_snapshot_details ("
        "factor_snapshot_id text NOT NULL, factor_name text NOT NULL, "
        "enabled boolean NOT NULL, state text NOT NULL)",
        "CREATE TABLE equity_factor_evidence ("
        "factor_snapshot_id text NOT NULL, factor_name text NOT NULL)",
        "CREATE CONSTRAINT TRIGGER factor_complete AFTER INSERT ON "
        "equity_factor_snapshots DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_equity_factor_snapshot_complete()",
        "CREATE TABLE equity_rank_snapshots ("
        "rank_snapshot_id text PRIMARY KEY, expected_instrument_count integer NOT NULL, "
        "included_instrument_count integer NOT NULL, "
        "excluded_instrument_count integer NOT NULL)",
        "CREATE TABLE equity_rank_snapshot_rows ("
        "rank_snapshot_id text NOT NULL, eligible boolean NOT NULL, row_ordinal integer NOT NULL)",
        "CREATE CONSTRAINT TRIGGER rank_complete AFTER INSERT ON equity_rank_snapshots "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_equity_rank_snapshot_complete()",
        "CREATE TABLE model_rebalances ("
        "rebalance_id text PRIMARY KEY, expected_leg_count integer NOT NULL)",
        "CREATE TABLE model_rebalance_legs (rebalance_id text NOT NULL, sequence integer NOT NULL)",
        "CREATE CONSTRAINT TRIGGER model_complete AFTER INSERT ON model_rebalances "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_model_rebalance_complete()",
        "CREATE TABLE account_rebalance_plans ("
        "account_plan_id text PRIMARY KEY, model_leg_count integer NOT NULL, "
        "execution_leg_count integer NOT NULL)",
        "CREATE TABLE account_rebalance_plan_legs ("
        "account_plan_id text NOT NULL, disposition text NOT NULL, "
        "model_sequence integer NOT NULL, command_sequence integer)",
        "CREATE CONSTRAINT TRIGGER account_complete AFTER INSERT ON account_rebalance_plans "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_account_rebalance_plan_complete()",
    )
    connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}", public'))
    for statement in statements:
        connection.execute(sa.text(statement))


def _create_progress_probe_schema(connection: sa.Connection, schema: str) -> None:
    statements = (
        "CREATE TABLE account_rebalance_plans ("
        "account_plan_id text, model_rebalance_id text, user_id text, "
        "binding_id bigint, broker_account_id bigint, strategy_id text, "
        "data_use_scope text, provider_authority_sha256 text, "
        "decision_cutoff timestamptz, execute_not_before timestamptz, "
        "execution_session_sha256 text, expires_at timestamptz, "
        "execution_policy jsonb, broker_route jsonb, "
        "execution_policy_sha256 text, broker_route_sha256 text, "
        "content_sha256 text, model_leg_count integer, execution_leg_count integer, "
        "intentional_cash_slots integer, progress_deadline_at timestamptz, "
        "created_at timestamptz, phase text NOT NULL DEFAULT 'exit', "
        "status text NOT NULL DEFAULT 'pending', "
        "fence_generation bigint NOT NULL DEFAULT 0, lease_owner text, "
        "lease_expires_at timestamptz, last_progress_at timestamptz, "
        "last_error_code text, last_error_detail text, "
        "completion_account_generation bigint, terminal_targets_sha256 text, "
        "updated_at timestamptz)",
        "CREATE TRIGGER plan_progress_guard BEFORE INSERT OR UPDATE ON "
        "account_rebalance_plans FOR EACH ROW EXECUTE FUNCTION "
        "public.vm_protect_account_rebalance_plan()",
        "CREATE TABLE account_rebalance_plan_legs ("
        "account_plan_id text, user_id text, broker_account_id bigint, "
        "model_sequence integer, command_sequence integer, model_rebalance_id text, "
        "model_leg_id text, plan_leg_id text, model_signal_snapshot_sha256 text, "
        "instr_id integer, external_signal_id text, symbol text, phase text, action text, "
        "disposition text, rank_position numeric, allocation_hint numeric, required boolean, "
        "reason_code text, depends_on_sequences jsonb, "
        "status text NOT NULL DEFAULT 'pending', last_progress_at timestamptz, "
        "last_error_code text, frozen_target_quantity numeric, "
        "target_account_generation bigint, target_frozen_at timestamptz)",
        "CREATE TRIGGER leg_progress_guard BEFORE INSERT OR UPDATE ON "
        "account_rebalance_plan_legs FOR EACH ROW EXECUTE FUNCTION "
        "public.vm_protect_account_rebalance_leg()",
        "CREATE CONSTRAINT TRIGGER plan_terminal_coherent AFTER INSERT OR UPDATE ON "
        "account_rebalance_plans DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_account_rebalance_terminal_coherent()",
    )
    connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}", public'))
    for statement in statements:
        connection.execute(sa.text(statement))


def _commit_probe_statements(engine: sa.Engine, schema: str, *statements: str) -> None:
    with engine.begin() as connection:
        connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}", public'))
        for statement in statements:
            connection.execute(sa.text(statement))


@pytest.mark.integration
def test_migrated_postgresql_parent_guard_rejects_late_child_append() -> None:
    engine = sa.create_engine(_postgres_url(), future=True)
    schema = f"vm_aggregate_seal_{uuid4().hex}"
    try:
        with engine.begin() as connection:
            installed = connection.scalar(
                sa.text(
                    "SELECT to_regprocedure("
                    "'public.vm_require_append_only_parent_current_xact()') IS NOT NULL"
                )
            )
            assert installed is True
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(
                sa.text(f'CREATE TABLE "{schema}".aggregate_roots (root_id text PRIMARY KEY)')
            )
            connection.execute(
                sa.text(
                    f'CREATE TABLE "{schema}".aggregate_children ('
                    "child_id text PRIMARY KEY, root_id text NOT NULL REFERENCES "
                    f'"{schema}".aggregate_roots(root_id))'
                )
            )
            connection.execute(
                sa.text(
                    f'CREATE TRIGGER aggregate_children_sealed BEFORE INSERT ON "{schema}".'
                    "aggregate_children FOR EACH ROW EXECUTE FUNCTION "
                    "public.vm_require_append_only_parent_current_xact"
                    "('aggregate_roots', 'root_id', 'root_id')"
                )
            )
        with engine.begin() as connection:
            connection.execute(
                sa.text(f"INSERT INTO \"{schema}\".aggregate_roots VALUES ('atomic')")
            )
            connection.execute(
                sa.text(
                    f"INSERT INTO \"{schema}\".aggregate_children VALUES ('atomic-child', 'atomic')"
                )
            )
        with engine.begin() as connection:
            connection.execute(
                sa.text(f"INSERT INTO \"{schema}\".aggregate_roots VALUES ('sealed')")
            )
        with pytest.raises(DBAPIError) as exc_info, engine.begin() as connection:
            connection.execute(
                sa.text(
                    f"INSERT INTO \"{schema}\".aggregate_children VALUES ('late-child', 'sealed')"
                )
            )
        assert _sqlstate(exc_info.value) == "55000"
    finally:
        with engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


@pytest.mark.integration
def test_migrated_postgresql_deferred_checks_require_complete_aggregates() -> None:
    engine = sa.create_engine(_postgres_url(), future=True)
    schema = f"vm_aggregate_complete_{uuid4().hex}"
    try:
        with engine.begin() as connection:
            _create_completeness_probe_schema(connection, schema)
        with engine.begin() as connection:
            connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}", public'))
            connection.execute(
                sa.text("INSERT INTO equity_factor_snapshots VALUES ('factor-ok', 1, 1)")
            )
            connection.execute(
                sa.text(
                    "INSERT INTO equity_factor_snapshot_details "
                    "VALUES ('factor-ok', 'momentum', true, 'complete')"
                )
            )
            connection.execute(
                sa.text("INSERT INTO equity_factor_evidence VALUES ('factor-ok', 'momentum')")
            )
            connection.execute(
                sa.text("INSERT INTO equity_rank_snapshots VALUES ('rank-ok', 2, 1, 1)")
            )
            connection.execute(
                sa.text(
                    "INSERT INTO equity_rank_snapshot_rows VALUES "
                    "('rank-ok', true, 0), ('rank-ok', false, 1)"
                )
            )
            connection.execute(sa.text("INSERT INTO model_rebalances VALUES ('model-ok', 1)"))
            connection.execute(sa.text("INSERT INTO model_rebalance_legs VALUES ('model-ok', 0)"))
            connection.execute(
                sa.text("INSERT INTO account_rebalance_plans VALUES ('account-ok', 2, 1)")
            )
            connection.execute(
                sa.text(
                    "INSERT INTO account_rebalance_plan_legs VALUES "
                    "('account-ok', 'selected', 0, 0), "
                    "('account-ok', 'rejected', 1, NULL)"
                )
            )
        incomplete_headers = (
            "INSERT INTO equity_factor_snapshots VALUES ('factor-missing', 1, 1)",
            "INSERT INTO equity_rank_snapshots VALUES ('rank-missing', 1, 1, 0)",
            "INSERT INTO model_rebalances VALUES ('model-missing', 1)",
            "INSERT INTO account_rebalance_plans VALUES ('account-missing', 1, 1)",
        )
        for statement in incomplete_headers:
            with pytest.raises(DBAPIError) as exc_info:
                _commit_probe_statements(engine, schema, statement)
            assert _sqlstate(exc_info.value) == "23514"
        with pytest.raises(DBAPIError) as evidence_exc:
            _commit_probe_statements(
                engine,
                schema,
                "INSERT INTO equity_factor_snapshots VALUES ('factor-no-proof', 1, 1)",
                "INSERT INTO equity_factor_snapshot_details "
                "VALUES ('factor-no-proof', 'quality', true, 'complete')",
            )
        assert _sqlstate(evidence_exc.value) == "23514"
    finally:
        with engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


@pytest.mark.integration
def test_migrated_postgresql_guards_terminal_rebalance_progress() -> None:
    engine = sa.create_engine(_postgres_url(), future=True)
    schema = f"vm_rebalance_progress_{uuid4().hex}"
    try:
        with engine.begin() as connection:
            trigger_rows = {
                tuple(row)
                for row in connection.execute(
                    sa.text(
                        """
                        SELECT c.relname, t.tgname
                        FROM pg_trigger AS t
                        JOIN pg_class AS c ON c.oid = t.tgrelid
                        JOIN pg_namespace AS n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public'
                          AND t.tgname IN (
                              'trg_equity_observation_values_aggregate_sealed',
                              'trg_equity_factor_snapshot_details_aggregate_sealed',
                              'trg_equity_factor_evidence_aggregate_sealed',
                              'trg_equity_rank_snapshot_rows_aggregate_sealed',
                              'model_rebalance_legs_aggregate_sealed',
                              'account_rebalance_plan_legs_aggregate_sealed'
                          )
                        """
                    )
                )
            }
            assert trigger_rows == {
                (
                    "equity_observation_values",
                    "trg_equity_observation_values_aggregate_sealed",
                ),
                (
                    "equity_factor_snapshot_details",
                    "trg_equity_factor_snapshot_details_aggregate_sealed",
                ),
                ("equity_factor_evidence", "trg_equity_factor_evidence_aggregate_sealed"),
                (
                    "equity_rank_snapshot_rows",
                    "trg_equity_rank_snapshot_rows_aggregate_sealed",
                ),
                ("model_rebalance_legs", "model_rebalance_legs_aggregate_sealed"),
                (
                    "account_rebalance_plan_legs",
                    "account_rebalance_plan_legs_aggregate_sealed",
                ),
            }
            semantic_constraints = {
                row[0]
                for row in connection.execute(
                    sa.text(
                        """
                        SELECT conname
                        FROM pg_constraint
                        WHERE conname IN (
                            'ck_model_rebalance_leg_phase_action',
                            'ck_account_rebalance_leg_selection'
                        )
                        """
                    )
                )
            }
            assert semantic_constraints == {
                "ck_model_rebalance_leg_phase_action",
                "ck_account_rebalance_leg_selection",
            }
            _create_progress_probe_schema(connection, schema)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    f'INSERT INTO "{schema}".account_rebalance_plans '
                    "(account_plan_id, execution_policy, broker_route) "
                    "VALUES ('terminal-plan', '{}', '{}')"
                )
            )
            connection.execute(
                sa.text(
                    f'INSERT INTO "{schema}".account_rebalance_plan_legs '
                    "(account_plan_id, plan_leg_id, disposition, phase, "
                    "depends_on_sequences) VALUES "
                    "('terminal-plan', 'terminal-leg', 'selected', 'exit', '[]')"
                )
            )
            connection.execute(
                sa.text(
                    f'UPDATE "{schema}".account_rebalance_plan_legs '
                    "SET status = 'confirmed', frozen_target_quantity = 0, "
                    "target_account_generation = 1, target_frozen_at = now() "
                    "WHERE plan_leg_id = 'terminal-leg'"
                )
            )
            connection.execute(
                sa.text(
                    f"UPDATE \"{schema}\".account_rebalance_plans SET status = 'running' "
                    "WHERE account_plan_id = 'terminal-plan'"
                )
            )
            connection.execute(
                sa.text(
                    f'UPDATE "{schema}".account_rebalance_plans '
                    "SET phase = 'complete', status = 'completed', "
                    "completion_account_generation = 1, terminal_targets_sha256 = "
                    f"'{('a' * 64)}' "
                    "WHERE account_plan_id = 'terminal-plan'"
                )
            )
        with pytest.raises(DBAPIError) as plan_exc, engine.begin() as connection:
            connection.execute(
                sa.text(
                    f'UPDATE "{schema}".account_rebalance_plans '
                    "SET phase = 'exit', status = 'running' "
                    "WHERE account_plan_id = 'terminal-plan'"
                )
            )
        assert _sqlstate(plan_exc.value) == "55000"
        with pytest.raises(DBAPIError) as pair_exc, engine.begin() as connection:
            connection.execute(
                sa.text(
                    f'INSERT INTO "{schema}".account_rebalance_plans '
                    "(account_plan_id, execution_policy, broker_route, phase, status) "
                    "VALUES ('invalid-pair', '{}', '{}', 'complete', 'running')"
                )
            )
        assert _sqlstate(pair_exc.value) == "23514"
        with pytest.raises(DBAPIError) as initial_plan_exc, engine.begin() as connection:
            connection.execute(
                sa.text(
                    f'INSERT INTO "{schema}".account_rebalance_plans '
                    "(account_plan_id, execution_policy, broker_route, phase, status) "
                    "VALUES ('forged-terminal', '{}', '{}', 'complete', 'completed')"
                )
            )
        assert _sqlstate(initial_plan_exc.value) == "23514"
        with pytest.raises(DBAPIError) as initial_leg_exc, engine.begin() as connection:
            connection.execute(
                sa.text(
                    f'INSERT INTO "{schema}".account_rebalance_plan_legs '
                    "(account_plan_id, plan_leg_id, disposition, phase, "
                    "depends_on_sequences, status) VALUES "
                    "('terminal-plan', 'forged-leg', 'selected', 'entry', '[]', 'confirmed')"
                )
            )
        assert _sqlstate(initial_leg_exc.value) == "23514"
        with pytest.raises(DBAPIError) as leg_exc, engine.begin() as connection:
            connection.execute(
                sa.text(
                    f"UPDATE \"{schema}\".account_rebalance_plan_legs SET status = 'submitted' "
                    "WHERE plan_leg_id = 'terminal-leg'"
                )
            )
        assert _sqlstate(leg_exc.value) == "55000"

        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    f'INSERT INTO "{schema}".account_rebalance_plans '
                    "(account_plan_id, execution_policy, broker_route) "
                    "VALUES ('phase-plan', '{}', '{}')"
                )
            )
            connection.execute(
                sa.text(
                    f'INSERT INTO "{schema}".account_rebalance_plan_legs '
                    "(account_plan_id, plan_leg_id, disposition, phase, "
                    "depends_on_sequences) VALUES "
                    "('phase-plan', 'progress-leg', 'selected', 'entry', '[]')"
                )
            )
            connection.execute(
                sa.text(
                    f'UPDATE "{schema}".account_rebalance_plan_legs '
                    "SET status = 'submitted' WHERE plan_leg_id = 'progress-leg'"
                )
            )
            connection.execute(
                sa.text(
                    f'UPDATE "{schema}".account_rebalance_plan_legs '
                    "SET status = 'partial' WHERE plan_leg_id = 'progress-leg'"
                )
            )
            connection.execute(
                sa.text(
                    f"UPDATE \"{schema}\".account_rebalance_plans SET status = 'running' "
                    "WHERE account_plan_id = 'phase-plan'"
                )
            )
            connection.execute(
                sa.text(
                    f'UPDATE "{schema}".account_rebalance_plans '
                    "SET phase = 'account_refresh', status = 'running' "
                    "WHERE account_plan_id = 'phase-plan'"
                )
            )
        with pytest.raises(DBAPIError) as phase_exc, engine.begin() as connection:
            connection.execute(
                sa.text(
                    f'UPDATE "{schema}".account_rebalance_plans '
                    "SET phase = 'exit', status = 'blocked' "
                    "WHERE account_plan_id = 'phase-plan'"
                )
            )
        assert _sqlstate(phase_exc.value) == "55000"
        with pytest.raises(DBAPIError) as leg_regression_exc, engine.begin() as connection:
            connection.execute(
                sa.text(
                    f'UPDATE "{schema}".account_rebalance_plan_legs '
                    "SET status = 'submitted' WHERE plan_leg_id = 'progress-leg'"
                )
            )
        assert _sqlstate(leg_regression_exc.value) == "55000"

        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    f'INSERT INTO "{schema}".account_rebalance_plans '
                    "(account_plan_id, execution_policy, broker_route) "
                    "VALUES ('incomplete-plan', '{}', '{}')"
                )
            )
            connection.execute(
                sa.text(
                    f'INSERT INTO "{schema}".account_rebalance_plan_legs '
                    "(account_plan_id, plan_leg_id, disposition, phase, "
                    "depends_on_sequences) VALUES "
                    "('incomplete-plan', 'open-leg', 'selected', 'exit', '[]')"
                )
            )
        with pytest.raises(DBAPIError) as incomplete_exc:
            _commit_probe_statements(
                engine,
                schema,
                f"UPDATE \"{schema}\".account_rebalance_plans SET status = 'running' "
                "WHERE account_plan_id = 'incomplete-plan'",
                f'UPDATE "{schema}".account_rebalance_plans '
                "SET phase = 'complete', status = 'completed' "
                "WHERE account_plan_id = 'incomplete-plan'",
            )
        assert _sqlstate(incomplete_exc.value) == "23514"

        _commit_probe_statements(
            engine,
            schema,
            f'INSERT INTO "{schema}".account_rebalance_plans '
            "(account_plan_id, execution_policy, broker_route) "
            "VALUES ('degraded-plan', '{}', '{}')",
            f'INSERT INTO "{schema}".account_rebalance_plan_legs '
            "(account_plan_id, plan_leg_id, disposition, phase, depends_on_sequences) "
            "VALUES ('degraded-plan', 'degraded-exit', 'selected', 'exit', '[]'), "
            "('degraded-plan', 'degraded-entry', 'selected', 'entry', '[]'), "
            "('degraded-plan', 'degraded-reject', 'rejected', 'entry', '[]')",
            f'UPDATE "{schema}".account_rebalance_plan_legs SET status = CASE '
            "WHEN plan_leg_id = 'degraded-exit' THEN 'confirmed' "
            "WHEN plan_leg_id = 'degraded-entry' THEN 'failed' ELSE 'skipped' END, "
            "frozen_target_quantity = CASE WHEN disposition = 'selected' THEN 0 ELSE NULL END, "
            "target_account_generation = CASE WHEN disposition = 'selected' THEN 1 ELSE NULL END, "
            "target_frozen_at = CASE WHEN disposition = 'selected' THEN now() ELSE NULL END "
            "WHERE account_plan_id = 'degraded-plan'",
            f"UPDATE \"{schema}\".account_rebalance_plans SET status = 'running' "
            "WHERE account_plan_id = 'degraded-plan'",
            f'UPDATE "{schema}".account_rebalance_plans '
            "SET phase = 'account_refresh', status = 'running' "
            "WHERE account_plan_id = 'degraded-plan'",
            f'UPDATE "{schema}".account_rebalance_plans '
            "SET phase = 'entry', status = 'running' "
            "WHERE account_plan_id = 'degraded-plan'",
            f'UPDATE "{schema}".account_rebalance_plans '
            "SET phase = 'complete', status = 'degraded', "
            "completion_account_generation = 1, terminal_targets_sha256 = "
            f"'{('b' * 64)}' "
            "WHERE account_plan_id = 'degraded-plan'",
        )
    finally:
        with engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
