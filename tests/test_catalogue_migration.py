"""Safe catalogue registration preserves installed execution authority."""

from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from lib_application.db.models import Base, Strategy, StrategyVersion


@pytest.fixture
def model_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Strategy.__table__, StrategyVersion.__table__])
    yield engine
    engine.dispose()


def test_new_catalogue_models_do_not_authorize_execution(model_engine: sa.Engine) -> None:
    with Session(model_engine) as session:
        strategy = Strategy(strategy_id="new-strategy", strategy_name="New", asset_class="crypto")
        session.add(strategy)
        session.flush()
        version = StrategyVersion(
            strategy_id=strategy.strategy_id,
            semver="1.0.0",
            param_schema={},
            default_params={},
        )
        session.add(version)
        session.commit()

        assert strategy.is_active is False
        assert version.status == "registered"


@pytest.mark.parametrize("status", ["registered", "active", "deprecated", "pulled"])
def test_version_status_accepts_registration_and_preserves_existing_states(
    model_engine: sa.Engine, status: str
) -> None:
    with Session(model_engine) as session:
        session.add(Strategy(strategy_id="strategy", strategy_name="Strategy", is_active=False))
        session.flush()
        version = StrategyVersion(
            strategy_id="strategy",
            semver="1.0.0",
            param_schema={},
            default_params={},
            status=status,
        )
        session.add(version)
        session.commit()
        assert session.get(StrategyVersion, version.strat_ver_id).status == status


def test_version_status_rejects_unknown_authority(model_engine: sa.Engine) -> None:
    with Session(model_engine) as session:
        session.add(Strategy(strategy_id="strategy", strategy_name="Strategy"))
        session.flush()
        session.add(
            StrategyVersion(
                strategy_id="strategy", semver="1.0.0", param_schema={}, status="approved"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            session.commit()


def _migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1] / "scripts/db/alembic/versions/0100_safe_catalogue.py"
    )
    spec = importlib.util.spec_from_file_location("safe_catalogue", path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _run_migration(connection: sa.Connection, direction: str) -> None:
    migration = _migration()
    migration.op = Operations(MigrationContext.configure(connection))
    getattr(migration, direction)()


@pytest.fixture
def previous_engine() -> Iterator[sa.Engine]:
    """The pre-0100 catalogue contract, independent of current model defaults."""
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "strategies",
        metadata,
        sa.Column("strategy_id", sa.String(50), primary_key=True),
        sa.Column("strategy_name", sa.String(255), nullable=False),
        sa.Column("asset_class", sa.String(50)),
        sa.Column("description", sa.String(500)),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    sa.Table(
        "strategy_versions",
        metadata,
        sa.Column("strat_ver_id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.String(50), sa.ForeignKey("strategies.strategy_id")),
        sa.Column("semver", sa.String(20), nullable=False),
        sa.Column("param_schema", sa.JSON(), nullable=False),
        sa.Column("default_params", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.UniqueConstraint("strategy_id", "semver", name="uq_strategy_version_semver"),
        sa.CheckConstraint(
            "status IN ('active', 'deprecated', 'pulled')", name="ck_version_status"
        ),
    )
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_upgrade_changes_defaults_without_rewriting_installed_authority(
    previous_engine: sa.Engine,
) -> None:
    with previous_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO strategies (strategy_id, strategy_name, is_active) VALUES "
                "('installed-active', 'Active', true), ('installed-disabled', 'Disabled', false), "
                "('legacy-null', 'Legacy', NULL)"
            )
        )
        for version_id, status in enumerate(("active", "deprecated", "pulled"), start=41):
            connection.execute(
                sa.text(
                    "INSERT INTO strategy_versions "
                    "(strat_ver_id, strategy_id, semver, param_schema, default_params, status) "
                    "VALUES (:id, 'installed-active', :semver, '{}', '{}', :status)"
                ),
                {"id": version_id, "semver": f"1.0.{version_id}", "status": status},
            )
        before = connection.execute(
            sa.text("SELECT * FROM strategy_versions ORDER BY strat_ver_id")
        ).all()
        _run_migration(connection, "upgrade")

        assert _migration().down_revision == "0099_single_owner_authority"
        assert (
            connection.execute(
                sa.text("SELECT * FROM strategy_versions ORDER BY strat_ver_id")
            ).all()
            == before
        )
        assert dict(
            connection.execute(sa.text("SELECT strategy_id, is_active FROM strategies")).all()
        ) == {
            "installed-active": True,
            "installed-disabled": False,
            "legacy-null": None,
        }
        connection.execute(
            sa.text("INSERT INTO strategies (strategy_id, strategy_name) VALUES ('new', 'New')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO strategy_versions (strategy_id, semver, param_schema, default_params) "
                "VALUES ('new', '1.0.0', '{}', '{}')"
            )
        )
        assert (
            connection.scalar(sa.text("SELECT is_active FROM strategies WHERE strategy_id = 'new'"))
            == 0
        )
        assert (
            connection.scalar(
                sa.text("SELECT status FROM strategy_versions WHERE strategy_id = 'new'")
            )
            == "registered"
        )


def test_downgrade_refuses_registration_before_changing_schema(previous_engine: sa.Engine) -> None:
    with previous_engine.begin() as connection:
        _run_migration(connection, "upgrade")
        connection.execute(
            sa.text("INSERT INTO strategies (strategy_id, strategy_name) VALUES ('new', 'New')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO strategy_versions (strategy_id, semver, param_schema, default_params) "
                "VALUES ('new', '1.0.0', '{}', '{}')"
            )
        )
        with pytest.raises(RuntimeError, match="Cannot downgrade registered"):
            _run_migration(connection, "downgrade")
        assert connection.scalar(sa.text("SELECT status FROM strategy_versions")) == "registered"
        assert connection.scalar(sa.text("SELECT is_active FROM strategies")) == 0
        assert (
            sa.inspect(connection).get_columns("strategy_versions")[-1]["default"] == "'registered'"
        )


def test_unconfigured_downgrade_restores_defaults_without_relabeling_rows(
    previous_engine: sa.Engine,
) -> None:
    with previous_engine.begin() as connection:
        _run_migration(connection, "upgrade")
        connection.execute(
            sa.text(
                "INSERT INTO strategies (strategy_id, strategy_name) VALUES ('disabled', 'Disabled')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO strategy_versions (strategy_id, semver, param_schema, default_params, status) "
                "VALUES ('disabled', '1.0.0', '{}', '{}', 'pulled')"
            )
        )
        _run_migration(connection, "downgrade")
        assert connection.scalar(sa.text("SELECT status FROM strategy_versions")) == "pulled"
        assert connection.scalar(sa.text("SELECT is_active FROM strategies")) == 0
        connection.execute(
            sa.text(
                "INSERT INTO strategies (strategy_id, strategy_name) VALUES ('legacy-default', 'Legacy')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO strategy_versions (strategy_id, semver, param_schema, default_params) "
                "VALUES ('legacy-default', '1.0.0', '{}', '{}')"
            )
        )
        assert (
            connection.scalar(
                sa.text("SELECT is_active FROM strategies WHERE strategy_id = 'legacy-default'")
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text("SELECT status FROM strategy_versions WHERE strategy_id = 'legacy-default'")
            )
            == "active"
        )


@pytest.fixture
def postgres_connection() -> Iterator[sa.Connection]:
    """Explicit maintenance URL for an isolated database migrated through 0100."""
    raw = os.getenv("CATALOGUE_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("CATALOGUE_TEST_DATABASE_URL is required for isolated PostgreSQL acceptance")
    engine = sa.create_engine(raw, hide_parameters=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("Catalogue registration acceptance requires PostgreSQL")
    try:
        with engine.connect() as connection, connection.begin():
            yield connection
            connection.rollback()
    finally:
        engine.dispose()


def _create_strategy(connection: sa.Connection, **changes: Any) -> str:
    values = {
        "id": "catalogue-acceptance",
        "name": "Catalogue acceptance",
        "asset_class": "crypto",
        "description": "Installed description",
        **changes,
    }
    return str(
        connection.scalar(
            sa.text(
                "SELECT public.vm_catalogue_create_strategy(:id, :name, :asset_class, :description)"
            ),
            values,
        )
    )


def _create_version(connection: sa.Connection, **changes: Any) -> int:
    values = {
        "strategy_id": "catalogue-acceptance",
        "semver": "1.0.0",
        "param_schema": {"type": "object"},
        "default_params": {"period": 20},
        "docker_image": "vynmatrix/indicator-runner:1.0.0",
        "git_repo": "local-source",
        "git_commit": "frozen-commit",
        **changes,
    }
    for key in ("param_schema", "default_params"):
        values[key] = json.dumps(values[key])
    return int(
        connection.scalar(
            sa.text(
                "SELECT public.vm_catalogue_create_version(:strategy_id, :semver, "
                "CAST(:param_schema AS jsonb), CAST(:default_params AS jsonb), "
                ":docker_image, :git_repo, :git_commit)"
            ),
            values,
        )
    )


def _sqlstate(error: sa.exc.DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)


@pytest.mark.integration
def test_postgres_backend_registration_is_idempotent_and_non_executable(
    postgres_connection: sa.Connection,
) -> None:
    connection = postgres_connection
    connection.execute(sa.text("SET LOCAL ROLE vm_backend"))
    strategy_id = _create_strategy(connection)
    version_id = _create_version(connection)
    assert _create_strategy(connection) == strategy_id
    assert _create_version(connection) == version_id
    assert (
        connection.scalar(
            sa.text("SELECT count(*) FROM public.strategies WHERE strategy_id = :id"),
            {"id": strategy_id},
        )
        == 1
    )
    assert (
        connection.scalar(
            sa.text("SELECT is_active FROM public.strategies WHERE strategy_id = :id"),
            {"id": strategy_id},
        )
        is False
    )
    assert (
        connection.scalar(
            sa.text("SELECT status FROM public.strategy_versions WHERE strat_ver_id = :id"),
            {"id": version_id},
        )
        == "registered"
    )
    for table in ("strategies", "strategy_versions"):
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            assert (
                connection.scalar(
                    sa.text("SELECT has_table_privilege(current_user, :table, :privilege)"),
                    {"table": f"public.{table}", "privilege": privilege},
                )
                is False
            )


@pytest.mark.integration
@pytest.mark.parametrize("status", ["registered", "active", "deprecated", "pulled"])
def test_postgres_retry_preserves_status_identity_and_omitted_provenance(
    postgres_connection: sa.Connection, status: str
) -> None:
    connection = postgres_connection
    _create_strategy(connection)
    version_id = _create_version(connection)
    connection.execute(
        sa.text(
            "UPDATE public.strategies SET is_active = true WHERE strategy_id = 'catalogue-acceptance'"
        )
    )
    connection.execute(
        sa.text("UPDATE public.strategy_versions SET status = :status WHERE strat_ver_id = :id"),
        {"status": status, "id": version_id},
    )
    connection.execute(sa.text("SET LOCAL ROLE vm_backend"))
    assert _create_strategy(connection, description=None) == "catalogue-acceptance"
    assert (
        _create_version(connection, docker_image=None, git_repo=None, git_commit=None) == version_id
    )
    assert connection.execute(
        sa.text(
            "SELECT status, docker_image, git_repo, git_commit FROM public.strategy_versions WHERE strat_ver_id = :id"
        ),
        {"id": version_id},
    ).one() == (status, "vynmatrix/indicator-runner:1.0.0", "local-source", "frozen-commit")
    assert connection.execute(
        sa.text(
            "SELECT is_active, description FROM public.strategies WHERE strategy_id = 'catalogue-acceptance'"
        )
    ).one() == (True, "Installed description")
    newer = _create_version(connection, semver="1.0.1")
    assert newer != version_id
    assert (
        connection.scalar(
            sa.text("SELECT status FROM public.strategy_versions WHERE strat_ver_id = :id"),
            {"id": newer},
        )
        == "registered"
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Conflicting name"),
        ("asset_class", "equity"),
        ("description", "Conflicting description"),
    ],
)
def test_postgres_conflicting_strategy_retry_preserves_installed_record(
    postgres_connection: sa.Connection, field: str, value: str
) -> None:
    connection = postgres_connection
    connection.execute(sa.text("SET LOCAL ROLE vm_backend"))
    _create_strategy(connection)
    with pytest.raises(sa.exc.DBAPIError) as caught, connection.begin_nested():
        _create_strategy(connection, **{field: value})
    assert _sqlstate(caught.value) == "23505"
    assert _create_strategy(connection) == "catalogue-acceptance"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("param_schema", {"type": "object", "required": ["period"]}),
        ("default_params", {"period": 30}),
        ("docker_image", "vynmatrix/indicator-runner:2.0.0"),
        ("git_repo", "other-source"),
        ("git_commit", "other-commit"),
    ],
)
def test_postgres_conflicting_release_retry_preserves_immutable_identity(
    postgres_connection: sa.Connection, field: str, value: Any
) -> None:
    connection = postgres_connection
    connection.execute(sa.text("SET LOCAL ROLE vm_backend"))
    _create_strategy(connection)
    version_id = _create_version(connection)
    with pytest.raises(sa.exc.DBAPIError) as caught, connection.begin_nested():
        _create_version(connection, **{field: value})
    assert _sqlstate(caught.value) == "23505"
    assert _create_version(connection) == version_id


@pytest.mark.integration
def test_postgres_registration_functions_are_locked_and_backend_only(
    postgres_connection: sa.Connection,
) -> None:
    connection = postgres_connection
    for signature in (
        "public.vm_catalogue_create_strategy(text,text,text,text)",
        "public.vm_catalogue_create_version(text,text,jsonb,jsonb,text,text,text)",
    ):
        row = connection.execute(
            sa.text(
                "SELECT p.prosecdef, p.proconfig, p.proowner = c.relowner AS table_owned "
                "FROM pg_proc p JOIN pg_class c ON c.oid = 'public.strategies'::regclass "
                "WHERE p.oid = CAST(:signature AS regprocedure)"
            ),
            {"signature": signature},
        ).one()
        assert row.prosecdef is True
        assert row.proconfig == ["search_path=pg_catalog, pg_temp"]
        assert row.table_owned is True
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_proc p, LATERAL aclexplode(p.proacl) acl "
                    "WHERE p.oid = CAST(:signature AS regprocedure) "
                    "AND acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'"
                ),
                {"signature": signature},
            )
            == 0
        )
        for role in (
            "vm_backend",
            "vm_scoring",
            "vm_execution",
            "vm_feedback",
            "vm_market_data",
            "vm_indicator",
        ):
            assert connection.scalar(
                sa.text("SELECT has_function_privilege(:role, :signature, 'EXECUTE')"),
                {"role": role, "signature": signature},
            ) is (role == "vm_backend")
    for table in (
        "broker_environments",
        "sectors",
        "instrument_sectors",
        "instrument_aliases",
        "instrument_broker_symbols",
    ):
        assert (
            connection.scalar(
                sa.text("SELECT has_table_privilege('vm_backend', :table, 'SELECT')"),
                {"table": f"public.{table}"},
            )
            is True
        )


@pytest.mark.integration
def test_postgres_registration_joins_callers_atomic_transaction(
    postgres_connection: sa.Connection,
) -> None:
    connection = postgres_connection
    connection.execute(sa.text("SET LOCAL ROLE vm_backend"))
    with pytest.raises(sa.exc.DBAPIError) as caught, connection.begin_nested():
        _create_invalid_release_batch(connection)
    assert _sqlstate(caught.value) == "23503"
    assert (
        connection.scalar(
            sa.text(
                "SELECT count(*) FROM public.strategies WHERE strategy_id = 'catalogue-acceptance'"
            )
        )
        == 0
    )


def _create_invalid_release_batch(connection: sa.Connection) -> None:
    _create_strategy(connection)
    _create_version(connection, strategy_id="missing-strategy")


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["strategy", "version"])
def test_postgres_registration_uses_the_shared_catalogue_lock(
    postgres_connection: sa.Connection, kind: str
) -> None:
    connection = postgres_connection
    connection.execute(sa.text("SELECT pg_advisory_xact_lock(18472, 1)"))
    register = _create_strategy if kind == "strategy" else _create_version
    with connection.engine.connect() as contender, contender.begin():
        contender.execute(sa.text("SET LOCAL ROLE vm_backend"))
        contender.execute(sa.text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(sa.exc.DBAPIError) as caught, contender.begin_nested():
            register(contender)
        assert _sqlstate(caught.value) == "55P03"
        contender.rollback()
