"""Explicit maintenance targets and owner input, before database side effects."""

import pytest

from dev_cli.core.bootstrap import BootstrapSettings, validate_owner_input


def environment():
    return {
        "ADMIN_DATABASE_URL": "postgresql://unit_admin:unit_pass@postgres:5432/postgres",
        "MIGRATION_DATABASE_URL": "postgresql://unit_migrate:unit_other@postgres:5432/unit_database",
        "EXECUTION_MODE": "paper",
        "EXECUTION_ENGINE_ALLOW_LIVE": "false",
        **{
            f"VM_{role}_DB_PASSWORD": f"unit_{role}_pass"
            for role in ("BACKEND", "SCORING", "EXECUTION", "FEEDBACK", "MARKET_DATA", "INDICATOR")
        },
    }


@pytest.mark.parametrize(
    "change",
    [
        {"MIGRATION_DATABASE_URL": "sqlite://"},
        {"MIGRATION_DATABASE_URL": "postgresql://vm_backend_login:pw@postgres/unit_database"},
        {"MIGRATION_DATABASE_URL": "postgresql://unit_migrate:pw@anotherhost/unit_database"},
        {"ADMIN_DATABASE_URL": "postgresql://unit_admin:pw@postgres/unit_database"},
        {"EXECUTION_MODE": "live"},
        {"EXECUTION_ENGINE_ALLOW_LIVE": "true"},
        {"VM_BACKEND_DB_PASSWORD": ""},
    ],
)
def test_invalid_stage_authority_fails_without_exposing_credentials(change):
    with pytest.raises(ValueError, match=r".+") as exc:
        BootstrapSettings.parse(environment() | change)
    assert "unit_pass" not in str(exc.value)
    assert "unit_other" not in str(exc.value)


def test_distinct_explicit_target_and_passwords_are_retained():
    settings = BootstrapSettings.parse(environment())
    assert settings.migration.username == "unit_migrate"
    assert settings.migration.database == "unit_database"
    assert len(settings.passwords) == 6
    assert "unit_pass" not in repr(settings)


def test_owner_input_never_accepts_accounts_credentials_or_implicit_identity():
    for value in (
        {"user_id": "x"},
        {"accounts": []},
        {"profile": {"credentials": "x"}},
        {"profile": {"base_ccy": "EUR"}},
    ):
        with pytest.raises(ValueError, match=r".+"):
            validate_owner_input(value)
    assert validate_owner_input({"profile": {}}) == {"profile": {}}
    assert (
        validate_owner_input({"existing_user_id": "preserved-text-id", "profile": {}})[
            "existing_user_id"
        ]
        == "preserved-text-id"
    )
    assert (
        validate_owner_input(
            {
                "profile": {
                    "email": "owner@example.invalid",
                    "base_ccy": "EUR",
                    "tz": "Europe/Amsterdam",
                }
            }
        )["profile"]["base_ccy"]
        == "EUR"
    )


@pytest.mark.parametrize(
    "query",
    [
        "host=other",
        "hostaddr=192.0.2.1",
        "user=another",
        "service=hidden",
        "options=-crole%3Dvm_execution",
    ],
)
def test_connection_routing_overrides_are_rejected_before_provisioning(query):
    env = environment()
    env["MIGRATION_DATABASE_URL"] += "?" + query
    with pytest.raises(ValueError, match="override"):
        BootstrapSettings.parse(env)


def test_migration_identifiers_are_measured_in_postgresql_bytes():
    env = environment()
    env["MIGRATION_DATABASE_URL"] = (
        "postgresql://" + "é" * 40 + ":unit_other@postgres:5432/unit_database"
    )
    with pytest.raises(ValueError, match="bounded"):
        BootstrapSettings.parse(env)


def test_one_explicit_maintenance_admin_identity_is_supported():
    env = environment()
    env["MIGRATION_DATABASE_URL"] = "postgresql://unit_admin:unit_pass@postgres:5432/unit_database"
    settings = BootstrapSettings.parse(env)
    assert settings.admin.username == settings.migration.username


@pytest.mark.parametrize(
    "migration",
    [
        "postgresql://unit_admin:another@postgres/unit_database",
        "postgresql://unit_migrate:unit_pass@postgres/unit_database",
    ],
)
def test_maintenance_password_relationship_must_match_explicit_identities(migration):
    with pytest.raises(ValueError, match="password"):
        BootstrapSettings.parse(environment() | {"MIGRATION_DATABASE_URL": migration})


@pytest.mark.parametrize("role", [None, (True, False), (False, True)])
def test_full_bootstrap_never_creates_or_elevates_migration_role(role):
    from dev_cli.core.bootstrap import _validate_provisioned_owner

    settings = BootstrapSettings.parse(environment())
    with pytest.raises(ValueError, match="already-provisioned SUPERUSER"):
        _validate_provisioned_owner(("unit_admin", "unit_admin", True), role, None, settings)


def test_existing_database_owner_cannot_be_implicitly_adopted():
    from dev_cli.core.bootstrap import _validate_provisioned_owner

    settings = BootstrapSettings.parse(environment())
    with pytest.raises(ValueError, match="will not adopt"):
        _validate_provisioned_owner(
            ("unit_admin", "unit_admin", True),
            (True, True),
            ("other", "UTF8", True, False),
            settings,
        )


def test_pending_historical_role_ddl_refuses_non_superuser_before_alembic(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from dev_cli.core import bootstrap

    root = Path(__file__).resolve().parents[3]
    connection = SimpleNamespace(scalar=lambda statement: False)
    monkeypatch.setattr(
        bootstrap.MigrationContext,
        "configure",
        lambda connection: SimpleNamespace(get_current_revision=lambda: "0038_managed_secrets"),
    )
    monkeypatch.setattr(
        bootstrap.command, "upgrade", lambda *args: pytest.fail("Historical DDL must not execute")
    )
    with pytest.raises(RuntimeError, match="SUPERUSER"):
        bootstrap.migrate(connection, root)


@pytest.fixture
def stages(monkeypatch):
    from contextlib import contextmanager

    from dev_cli.core import bootstrap, catalogue, runtime_roles

    events = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def scalar(self, *args, **kwargs):
            events.append("lock")
            return True

        def execute(self, *args, **kwargs):
            events.append("unlock")

        def commit(self):
            events.append("connection-commit")

        def rollback(self):
            events.append("connection-rollback")

        @contextmanager
        def begin(self):
            events.append("migration-begin")
            try:
                yield
            except BaseException:
                events.append("migration-rollback")
                raise
            else:
                events.append("migration-commit")

    class Engine:
        def connect(self):
            return Connection()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @contextmanager
        def begin(self):
            try:
                yield
            except BaseException:
                events.append("stage-rollback")
                raise
            else:
                events.append("stage-commit")

    monkeypatch.setattr(catalogue, "load_catalogue", lambda root: {})
    monkeypatch.setattr(
        bootstrap, "provision_database", lambda settings: events.append("provision")
    )
    monkeypatch.setattr(bootstrap, "create_engine_for_env", lambda **kwargs: Engine())
    monkeypatch.setattr(bootstrap, "dispose_engine", lambda engine: events.append("dispose"))
    monkeypatch.setattr(bootstrap, "get_session_factory", lambda **kwargs: Session)
    monkeypatch.setattr(
        bootstrap, "_require_schema_owner", lambda *args: events.append("schema-owner")
    )
    monkeypatch.setattr(bootstrap, "require_maintenance_database_role", lambda *args: None)
    monkeypatch.setattr(bootstrap, "migrate", lambda *args: events.append("migrate") or "test_head")
    monkeypatch.setattr(
        runtime_roles, "provision_runtime_roles", lambda *args: events.append("roles")
    )
    monkeypatch.setattr(
        catalogue, "reconcile_catalogue", lambda *args, **kwargs: events.append("references") or {}
    )
    monkeypatch.setattr(
        bootstrap,
        "initialize_owner",
        lambda *args, **kwargs: events.append("owner") or {"user_id": "preserved"},
    )
    return bootstrap, catalogue, events


def test_bootstrap_commits_stages_in_order_and_releases_lock(stages, tmp_path):
    bootstrap, _, events = stages
    result = bootstrap.bootstrap_database(
        tmp_path, BootstrapSettings.parse(environment()), {"profile": {}}
    )
    assert result["owner_id"] == "preserved"
    order = [
        event
        for event in events
        if event in {"provision", "schema-owner", "migrate", "roles", "references", "owner"}
    ]
    assert order == ["provision", "schema-owner", "migrate", "roles", "references", "owner"]
    assert events.index("migration-commit") < events.index("roles")
    assert events[-3:] == ["unlock", "connection-commit", "dispose"]


def test_failed_reference_stage_never_initializes_owner_and_retry_resumes(
    stages, monkeypatch, tmp_path
):
    bootstrap, catalogue, events = stages

    def rejected(*args, **kwargs):
        events.append("references-failed")
        raise ValueError("Reference conflict")

    monkeypatch.setattr(catalogue, "reconcile_catalogue", rejected)
    with pytest.raises(ValueError, match="Reference conflict"):
        bootstrap.bootstrap_database(
            tmp_path, BootstrapSettings.parse(environment()), {"profile": {}}
        )
    assert "owner" not in events
    assert "stage-rollback" in events
    assert "migration-commit" in events
    assert events[-3:] == ["unlock", "connection-commit", "dispose"]
    monkeypatch.setattr(catalogue, "reconcile_catalogue", lambda *args, **kwargs: {})
    result = bootstrap.bootstrap_database(
        tmp_path, BootstrapSettings.parse(environment()), {"profile": {}}
    )
    assert result["owner_id"] == "preserved"


def test_migration_failure_prevents_runtime_roles_and_unlocks(stages, monkeypatch, tmp_path):
    bootstrap, _, events = stages

    def rejected(*args):
        raise ValueError("Migration conflict")

    monkeypatch.setattr(bootstrap, "migrate", rejected)
    with pytest.raises(ValueError, match="Migration conflict"):
        bootstrap.bootstrap_database(
            tmp_path, BootstrapSettings.parse(environment()), {"profile": {}}
        )
    assert "roles" not in events
    assert "migration-rollback" in events
    assert "unlock" in events


def test_injected_alembic_connection_bypasses_environment_database_selection(monkeypatch):
    import runpy
    from contextlib import nullcontext
    from pathlib import Path
    from types import SimpleNamespace

    import sqlalchemy
    from alembic import context

    supplied = object()
    configured = []
    monkeypatch.setattr(
        context,
        "config",
        SimpleNamespace(attributes={"connection": supplied}, config_file_name=None),
        raising=False,
    )
    monkeypatch.setattr(context, "configure", lambda **kwargs: configured.append(kwargs))
    monkeypatch.setattr(context, "begin_transaction", nullcontext)
    monkeypatch.setattr(context, "run_migrations", lambda: None)
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(
        sqlalchemy,
        "engine_from_config",
        lambda *args, **kwargs: pytest.fail("Injected connection must be retained"),
    )
    runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/db/alembic/env.py"))
    assert configured[0]["connection"] is supplied


@pytest.fixture
def target_provisioning(monkeypatch):
    from types import SimpleNamespace

    from dev_cli.core import bootstrap, runtime_roles

    events = []
    state = {"role": (True, True), "database": None}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execution_options(self, **kwargs):
            return self

        def execute(self, statement, *args):
            query = str(statement)
            if "SELECT current_user" in query:
                return SimpleNamespace(one=lambda: ("unit_admin", "unit_admin", True))
            if "SELECT rolcanlogin" in query:
                return SimpleNamespace(first=lambda: state["role"])
            if "pg_catalog.pg_database" in query:
                return SimpleNamespace(first=lambda: state["database"])
            pytest.fail("Unexpected SQL in database provisioning")

    monkeypatch.setattr(
        bootstrap, "create_engine_for_env", lambda **kwargs: SimpleNamespace(connect=Connection)
    )
    monkeypatch.setattr(bootstrap, "dispose_engine", lambda engine: events.append("dispose"))
    monkeypatch.setattr(bootstrap, "_database_sql", lambda *args: events.append("create-database"))
    monkeypatch.setattr(runtime_roles, "_authenticate", lambda *args: events.append("authenticate"))
    return bootstrap, runtime_roles, events, state


def test_target_creation_authenticates_preprovisioned_owner_first(target_provisioning):
    bootstrap, _, events, _ = target_provisioning
    bootstrap.provision_database(BootstrapSettings.parse(environment()))
    assert events == ["authenticate", "create-database", "dispose"]


def test_database_rerun_only_authenticates_and_never_rewrites_roles(target_provisioning):
    bootstrap, _, events, state = target_provisioning
    state["database"] = ("unit_migrate", "UTF8", True, False)
    bootstrap.provision_database(BootstrapSettings.parse(environment()))
    assert events == ["authenticate", "dispose"]


def test_failed_owner_authentication_leaves_database_absent(target_provisioning, monkeypatch):
    bootstrap, runtime_roles, events, _ = target_provisioning

    def denied(*args):
        raise runtime_roles.RuntimeRoleError("Authentication failed")

    monkeypatch.setattr(runtime_roles, "_authenticate", denied)
    with pytest.raises(ValueError, match="Authentication"):
        bootstrap.provision_database(BootstrapSettings.parse(environment()))
    assert events == ["dispose"]


def test_raw_database_driver_error_never_exposes_connection_material(
    target_provisioning, monkeypatch
):
    import psycopg2

    bootstrap, _, events, _ = target_provisioning

    def denied(*args):
        raise psycopg2.OperationalError("private-fixture-connection-material")

    monkeypatch.setattr(bootstrap, "_database_sql", denied)
    with pytest.raises(RuntimeError, match="verify installed state") as caught:
        bootstrap.provision_database(BootstrapSettings.parse(environment()))
    assert "private-fixture-connection-material" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert events[-1] == "dispose"


@pytest.mark.parametrize("row", [None, ("other", "unit_migrate"), ("pg_database_owner", "other")])
def test_existing_schema_ownership_conflict_fails_before_migration(row):
    from types import SimpleNamespace

    from dev_cli.core.bootstrap import _require_schema_owner

    connection = SimpleNamespace(execute=lambda *args: SimpleNamespace(one_or_none=lambda: row))
    with pytest.raises(ValueError, match="ownership conflicts"):
        _require_schema_owner(connection, "unit_migrate")


def test_existing_foreign_owned_objects_are_not_adopted():
    from types import SimpleNamespace

    from dev_cli.core.bootstrap import _require_schema_owner

    connection = SimpleNamespace(
        execute=lambda *args: SimpleNamespace(
            one_or_none=lambda: ("pg_database_owner", "unit_migrate")
        ),
        scalar=lambda *args: True,
    )
    with pytest.raises(ValueError, match="another owner"):
        _require_schema_owner(connection, "unit_migrate")


def test_later_migrations_preserve_non_superuser_schema_owner_workflow(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from dev_cli.core import bootstrap

    root = Path(__file__).resolve().parents[3]
    state = {"revision": "0052_service_role_rls"}
    connection = SimpleNamespace(
        scalar=lambda *args: pytest.fail("Later heads need no historical SUPERUSER gate")
    )
    monkeypatch.setattr(
        bootstrap.MigrationContext,
        "configure",
        lambda connection: SimpleNamespace(get_current_revision=lambda: state["revision"]),
    )

    def upgrade(config, target):
        assert config.attributes["connection"] is connection
        state["revision"] = bootstrap.ScriptDirectory.from_config(config).get_current_head()

    monkeypatch.setattr(bootstrap.command, "upgrade", upgrade)
    assert bootstrap.migrate(connection, root) == state["revision"]


def test_migrate_rejects_non_string_alembic_head(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from dev_cli.core import bootstrap

    root = Path(__file__).resolve().parents[3]
    invalid_head = object()
    scripts = SimpleNamespace(
        iterate_revisions=lambda *_: (),
        get_current_head=lambda: invalid_head,
    )
    connection = SimpleNamespace(scalar=lambda *_: pytest.fail("No historical role check expected"))
    monkeypatch.setattr(bootstrap.ScriptDirectory, "from_config", lambda config: scripts)
    monkeypatch.setattr(
        bootstrap.MigrationContext,
        "configure",
        lambda connection: SimpleNamespace(get_current_revision=lambda: invalid_head),
    )
    monkeypatch.setattr(bootstrap.command, "upgrade", lambda *args: None)

    with pytest.raises(RuntimeError, match="Alembic head verification failed"):
        bootstrap.migrate(connection, root)


def test_bootstrap_emits_only_fixed_stage_markers(stages, tmp_path, capsys):
    bootstrap, _, _ = stages
    bootstrap.bootstrap_database(tmp_path, BootstrapSettings.parse(environment()), {"profile": {}})
    assert capsys.readouterr().out.splitlines() == [
        "[bootstrap] stage=owner",
        "[bootstrap] stage=catalogue",
        "[bootstrap] stage=provision",
        "[bootstrap] stage=migration",
        "[bootstrap] stage=roles",
        "[bootstrap] stage=catalogue",
        "[bootstrap] stage=owner",
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DB_USER", "contradictory"),
        ("DB_PASSWORD", "wrong-private-value"),
        ("DB_NAME", "wrong_database"),
        ("DB_PASSWORD", ""),
    ],
)
def test_compose_database_settings_must_match_explicit_urls(key, value):
    with pytest.raises(ValueError, match=key) as error:
        BootstrapSettings.parse(environment() | {key: value})
    assert value not in str(error.value) if value else True
    assert "unit_pass" not in str(error.value)


def test_matching_compose_database_settings_are_accepted():
    BootstrapSettings.parse(
        environment()
        | {"DB_USER": "unit_admin", "DB_PASSWORD": "unit_pass", "DB_NAME": "unit_database"}
    )
