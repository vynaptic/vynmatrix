"""Resumable PostgreSQL provisioning, migrations, references and explicit owner adoption."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import click
import psycopg2
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from psycopg2 import sql
from sqlalchemy import text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import SQLAlchemyError

from dev_cli.core.runtime_roles import PASSWORD_ENV, RUNTIME_ROLES
from dev_cli.core.runtime_roles import _url as parse_database_url
from lib_application.db.session import create_engine_for_env, dispose_engine, get_session_factory
from lib_application.services.database_authority import require_maintenance_database_role
from lib_application.services.owner_onboarding import _validate_profile, initialize_owner

_POSTGRES_IDENTIFIER_BYTES = 63
_USER_ID_MAX_LENGTH = 50
_MIGRATION_LOCK = (18472, 0)


@dataclass(frozen=True, repr=False)
class BootstrapSettings:
    admin: URL = field(repr=False)
    migration: URL = field(repr=False)
    passwords: dict[str, str] = field(repr=False)

    @classmethod
    def parse(cls, env: Mapping[str, str]) -> BootstrapSettings:
        if (
            env.get("EXECUTION_MODE", "paper") != "paper"
            or env.get("EXECUTION_ENGINE_ALLOW_LIVE", "false").lower() != "false"
        ):
            msg = "Bootstrap requires paper mode with live execution disabled"
            raise ValueError(msg)
        urls = []
        for key in ("ADMIN_DATABASE_URL", "MIGRATION_DATABASE_URL"):
            url = parse_database_url(env.get(key, ""))
            login = str(url.username)
            database = str(url.database)
            if (
                login in {"vm_app", *(name for pair in RUNTIME_ROLES for name in pair)}
                or login.startswith("pg_")
                or any(
                    len(value.encode("utf-8")) > _POSTGRES_IDENTIFIER_BYTES or "\x00" in value
                    for value in (login, database)
                )
            ):
                msg = f"{key} must name a bounded maintenance identity, never a runtime role"
                raise ValueError(msg)
            urls.append(url)
        admin, migration = urls
        if (
            (admin.host, admin.port or 5432) != (migration.host, migration.port or 5432)
            or admin.database != "postgres"
            or migration.database in {"postgres", "template0", "template1"}
        ):
            msg = (
                "Use administrator/postgres and migration/target connections "
                "on the same PostgreSQL server"
            )
            raise ValueError(msg)
        # Compose initializes the cluster login; its target database remains a
        # separate explicit maintenance stage. Reject contradictions before DDL.
        for key, expected in (
            ("DB_USER", admin.username),
            ("DB_PASSWORD", admin.password),
            ("DB_NAME", migration.database),
        ):
            if key in env and env[key] != expected:
                msg = f"{key} conflicts with the explicit maintenance database URLs"
                raise ValueError(msg)
        passwords = {}
        for login, key in PASSWORD_ENV.items():
            value = env.get(key, "")
            if not value or value == "CHANGE_ME_BEFORE_USE":
                msg = f"{key} requires an explicit non-placeholder password"
                raise ValueError(msg)
            passwords[login] = value
        same_identity = admin.username == migration.username
        if (same_identity and admin.password != migration.password) or (
            not same_identity and admin.password == migration.password
        ):
            msg = (
                "Maintenance passwords must match only when administrator "
                "and migration identity match"
            )
            raise ValueError(msg)
        if len(set(passwords.values())) != len(PASSWORD_ENV) or (
            set(passwords.values()) & {admin.password, migration.password}
        ):
            msg = "Six runtime passwords must be distinct from each other and maintenance passwords"
            raise ValueError(msg)
        return cls(admin, migration, passwords)


def validate_owner_input(value: Any) -> dict[str, Any]:
    """An empty profile means verify an existing designation, never choose a user."""
    if (
        not isinstance(value, dict)
        or "profile" not in value
        or value.keys() - {"profile", "existing_user_id"}
    ):
        msg = "Owner input requires profile and optional existing_user_id only"
        raise ValueError(msg)
    if not isinstance(value["profile"], dict):
        msg = "Owner profile must be an object"
        raise ValueError(msg)  # noqa: TRY004 - CLI configuration errors use ValueError.
    existing = value.get("existing_user_id")
    if existing is not None and (
        not isinstance(existing, str) or not existing.strip() or len(existing) > _USER_ID_MAX_LENGTH
    ):
        msg = "Owner adoption requires an explicit preserved user identifier"
        raise ValueError(msg)
    _validate_profile(
        value["profile"], require_complete=bool(value["profile"]) and existing is None
    )
    return value


def _url(value: URL) -> str:
    return value.render_as_string(hide_password=False)


def _database_sql(connection: Connection, statement: sql.Composable) -> None:
    driver = connection.connection.driver_connection
    if driver is None:
        msg = "Database provisioning connection is unavailable"
        raise RuntimeError(msg)
    with driver.cursor() as cursor:
        cursor.execute(statement)


def _validate_provisioned_owner(
    actor: tuple[Any, ...],
    migration_role: tuple[Any, ...] | None,
    database: tuple[Any, ...] | None,
    settings: BootstrapSettings,
) -> None:
    if actor != (settings.admin.username, settings.admin.username, True):
        msg = "Database provisioning requires the explicit cluster administrator login"
        raise ValueError(msg)
    if migration_role != (True, True):
        msg = (
            "Full bootstrap requires an already-provisioned SUPERUSER maintenance login "
            "for historical role DDL; bootstrap never creates or elevates that login"
        )
        raise ValueError(msg)
    if database is not None and database != (settings.migration.username, "UTF8", True, False):
        msg = "Existing database ownership/settings conflict; bootstrap will not adopt or reset it"
        raise ValueError(msg)


def provision_database(settings: BootstrapSettings) -> None:
    """Create only the explicit database; maintenance identities are pre-provisioned."""
    from dev_cli.core.runtime_roles import _authenticate  # noqa: PLC0415

    engine = create_engine_for_env(db_url=_url(settings.admin))
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            actor = connection.execute(
                text(
                    "SELECT current_user, session_user, rolsuper "
                    "FROM pg_catalog.pg_roles WHERE rolname=current_user"
                )
            ).one()
            role = connection.execute(
                text("SELECT rolcanlogin, rolsuper FROM pg_catalog.pg_roles WHERE rolname=:name"),
                {"name": settings.migration.username},
            ).first()
            database = connection.execute(
                text(
                    "SELECT pg_catalog.pg_get_userbyid(datdba), "
                    "pg_catalog.pg_encoding_to_char(encoding), datallowconn, datistemplate "
                    "FROM pg_catalog.pg_database WHERE datname=:name"
                ),
                {"name": settings.migration.database},
            ).first()
            _validate_provisioned_owner(
                tuple(actor),
                tuple(role) if role else None,
                tuple(database) if database else None,
                settings,
            )
            # Authenticate before CREATE DATABASE; mismatched supplied credentials
            # must not leave behind a stage created for an inaccessible owner.
            _authenticate(settings.migration.set(database="postgres"))
            if database is None:
                _database_sql(
                    connection,
                    sql.SQL(
                        "CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0"
                    ).format(
                        sql.Identifier(settings.migration.database),
                        sql.Identifier(settings.migration.username),
                    ),
                )
    except (SQLAlchemyError, psycopg2.Error):
        msg = "Database bootstrap stage failed; verify installed state before retrying"
        raise RuntimeError(msg) from None
    finally:
        dispose_engine(engine)


def _require_schema_owner(connection: Connection, owner: str) -> None:
    row = connection.execute(
        text("""
        SELECT pg_catalog.pg_get_userbyid(n.nspowner), pg_catalog.pg_get_userbyid(d.datdba)
        FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_database d
          ON d.datname=pg_catalog.current_database()
        WHERE n.nspname='public'
    """)
    ).one_or_none()
    if row is None or row[1] != owner or row[0] not in {owner, "pg_database_owner"}:
        msg = "Existing database/schema ownership conflicts; bootstrap will not adopt it"
        raise ValueError(msg)
    foreign = connection.scalar(
        text("""
        SELECT EXISTS (
          SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='public' AND pg_catalog.pg_get_userbyid(c.relowner) <> :owner
          UNION ALL
          SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname='public' AND pg_catalog.pg_get_userbyid(p.proowner) <> :owner
        )
    """),
        {"owner": owner},
    )
    if foreign:
        msg = (
            "Existing public objects have another owner; "
            "explicit maintenance disposition is required"
        )
        raise ValueError(msg)


def migrate(connection: Connection, root: Path) -> str:
    """Alembic uses the locked explicit connection, including complete historical DDL."""
    config = Config(str(root / "scripts/db/alembic.ini"))
    config.set_main_option("script_location", str(root / "scripts/db/alembic"))
    config.attributes["connection"] = connection
    scripts = ScriptDirectory.from_config(config)
    current = MigrationContext.configure(connection).get_current_revision()
    pending = {item.revision for item in scripts.iterate_revisions("head", current or "base")}
    if pending & {"0039_rls_tenant_isolation", "0052_service_role_rls"} and not connection.scalar(
        text("SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname=current_user")
    ):
        msg = (
            "Pending historical role DDL requires an explicitly configured SUPERUSER "
            "maintenance schema owner; migration was refused before DDL"
        )
        raise RuntimeError(msg)
    command.upgrade(config, "head")
    expected = scripts.get_current_head()
    if (
        not isinstance(expected, str)
        or MigrationContext.configure(connection).get_current_revision() != expected
    ):
        msg = "Alembic head verification failed"
        raise RuntimeError(msg)
    return expected


def _announce_stage(
    stage: Literal["provision", "migration", "roles", "catalogue", "owner"],
) -> None:
    """Let the host identify failed stages without forwarding untrusted process output."""
    click.echo(f"[bootstrap] stage={stage}")


def bootstrap_database(
    root: Path, settings: BootstrapSettings, owner: dict[str, Any]
) -> dict[str, Any]:
    """Stages commit independently; failure leaves runtime stopped and supports re-entry."""
    from dev_cli.core.catalogue import load_catalogue, reconcile_catalogue  # noqa: PLC0415
    from dev_cli.core.runtime_roles import provision_runtime_roles  # noqa: PLC0415

    _announce_stage("owner")
    validate_owner_input(owner)
    _announce_stage("catalogue")
    sources = load_catalogue(root)
    _announce_stage("provision")
    provision_database(settings)
    _announce_stage("migration")
    engine = create_engine_for_env(db_url=_url(settings.migration))
    try:
        with engine.connect() as connection:
            acquired = connection.scalar(
                text("SELECT pg_try_advisory_lock(:family, :lock)"),
                {"family": _MIGRATION_LOCK[0], "lock": _MIGRATION_LOCK[1]},
            )
            connection.commit()
            if not acquired:
                msg = (
                    "Another bootstrap or migration holds the database lock; "
                    "retry after it finishes"
                )
                raise RuntimeError(msg)
            try:
                with connection.begin():
                    _require_schema_owner(connection, str(settings.migration.username))
                    head = migrate(connection, root)
                _announce_stage("roles")
                provision_runtime_roles(
                    _url(settings.admin.set(database=settings.migration.database)),
                    _url(settings.migration),
                    settings.passwords,
                )
                _announce_stage("catalogue")
                factory = get_session_factory(engine=engine)
                with factory() as session, session.begin():
                    require_maintenance_database_role(session)
                    references = reconcile_catalogue(session, sources, apply=True, maintenance=True)
                _announce_stage("owner")
                with factory() as session, session.begin():
                    profile = initialize_owner(
                        session,
                        profile=owner["profile"],
                        existing_user_id=owner.get("existing_user_id"),
                    )
                return {
                    "revision": head,
                    "references": references,
                    "owner_id": profile["user_id"],
                    "execution_mode": "paper",
                }
            finally:
                connection.rollback()
                connection.execute(
                    text("SELECT pg_advisory_unlock(:family, :lock)"),
                    {"family": _MIGRATION_LOCK[0], "lock": _MIGRATION_LOCK[1]},
                )
                connection.commit()
    except (SQLAlchemyError, psycopg2.Error):
        msg = "Database bootstrap stage failed; verify installed state before retrying"
        raise RuntimeError(msg) from None
    finally:
        dispose_engine(engine)
