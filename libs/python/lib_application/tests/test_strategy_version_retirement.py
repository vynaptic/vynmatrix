from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol, cast

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lib_application.db.models import StrategyVersion

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MIGRATION_PATH = (
    _REPO_ROOT
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0065_retire_dormant_strategy_metadata.py"
)
_DORMANT_COLUMNS = {
    "engine_kind",
    "mlflow_run_id",
    "rl_policy_uri",
    "agent_graph",
}


class _MigrationModule(Protocol):
    op: Operations

    def upgrade(self) -> None: ...

    def downgrade(self) -> None: ...

    def _assert_no_dormant_metadata(self, connection: sa.Connection) -> None: ...


def _load_migration() -> _MigrationModule:
    spec = importlib.util.spec_from_file_location("migration_0065", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(_MigrationModule, module)


def _legacy_strategy_versions(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            CREATE TABLE strategy_versions (
                strat_ver_id INTEGER PRIMARY KEY,
                engine_kind VARCHAR(50),
                mlflow_run_id VARCHAR(100),
                rl_policy_uri VARCHAR(500),
                agent_graph JSON,
                CONSTRAINT ck_engine_kind
                    CHECK (engine_kind IN ('python_service', 'spark', 'ray'))
            )
            """
        )
    )


def test_strategy_version_model_has_no_dormant_runtime_metadata() -> None:
    table = cast(sa.Table, StrategyVersion.__table__)
    assert _DORMANT_COLUMNS.isdisjoint(table.columns.keys())
    assert "ck_engine_kind" not in {constraint.name for constraint in table.constraints}


def test_retirement_accepts_only_canonical_empty_metadata() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        _legacy_strategy_versions(connection)
        connection.execute(
            sa.text(
                """
                INSERT INTO strategy_versions (
                    strat_ver_id, engine_kind, mlflow_run_id, rl_policy_uri, agent_graph
                ) VALUES (1, 'python_service', NULL, NULL, NULL)
                """
            )
        )
        migration._assert_no_dormant_metadata(connection)


def test_retirement_upgrade_and_downgrade_preserve_schema_mechanics() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        _legacy_strategy_versions(connection)
        connection.execute(
            sa.text(
                """
                INSERT INTO strategy_versions (
                    strat_ver_id, engine_kind, mlflow_run_id, rl_policy_uri, agent_graph
                ) VALUES (1, 'python_service', NULL, NULL, NULL)
                """
            )
        )
        migration.op = Operations(MigrationContext.configure(connection))

        migration.upgrade()
        upgraded_columns = {
            column["name"] for column in sa.inspect(connection).get_columns("strategy_versions")
        }
        assert _DORMANT_COLUMNS.isdisjoint(upgraded_columns)
        assert (
            connection.execute(sa.text("SELECT strat_ver_id FROM strategy_versions")).scalar_one()
            == 1
        )

        migration.downgrade()
        downgraded_columns = {
            column["name"] for column in sa.inspect(connection).get_columns("strategy_versions")
        }
        assert downgraded_columns >= _DORMANT_COLUMNS
        assert (
            connection.execute(sa.text("SELECT engine_kind FROM strategy_versions")).scalar_one()
            == "python_service"
        )
        assert "ck_engine_kind" in {
            constraint["name"]
            for constraint in sa.inspect(connection).get_check_constraints("strategy_versions")
        }


@pytest.mark.parametrize(
    ("engine_kind", "mlflow_run_id", "rl_policy_uri", "agent_graph"),
    [
        ("spark", None, None, None),
        ("ray", None, None, None),
        ("python_service", "run-1", None, None),
        ("python_service", None, "policy://retired", None),
        ("python_service", None, None, "{}"),
    ],
)
def test_retirement_rejects_dormant_metadata_without_rewriting(
    engine_kind: str,
    mlflow_run_id: str | None,
    rl_policy_uri: str | None,
    agent_graph: str | None,
) -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        _legacy_strategy_versions(connection)
        connection.execute(
            sa.text(
                """
                INSERT INTO strategy_versions (
                    strat_ver_id, engine_kind, mlflow_run_id, rl_policy_uri, agent_graph
                ) VALUES (
                    1, :engine_kind, :mlflow_run_id, :rl_policy_uri, :agent_graph
                )
                """
            ),
            {
                "engine_kind": engine_kind,
                "mlflow_run_id": mlflow_run_id,
                "rl_policy_uri": rl_policy_uri,
                "agent_graph": agent_graph,
            },
        )
        with pytest.raises(RuntimeError, match="will not reinterpret production history"):
            migration._assert_no_dormant_metadata(connection)
