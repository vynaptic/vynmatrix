"""Create or explicitly rotate six runtime logins without repairing installed authority."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg2
from psycopg2 import sql
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

RUNTIME_ROLES = (
    ("vm_backend_login", "vm_backend"),
    ("vm_scoring_login", "vm_scoring"),
    ("vm_execution_login", "vm_execution"),
    ("vm_feedback_login", "vm_feedback"),
    ("vm_market_data_login", "vm_market_data"),
    ("vm_indicator_login", "vm_indicator"),
)
PASSWORD_ENV = {login: f"{group.upper()}_DB_PASSWORD" for login, group in RUNTIME_ROLES}
_ELEVATED = ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")


class RuntimeRoleError(ValueError):
    """A credential-free provisioning error suitable for command output."""


@dataclass(frozen=True)
class RuntimeRolePlan:
    create: tuple[str, ...]
    authenticate: tuple[str, ...]
    rotate: tuple[str, ...]


def plan_runtime_roles(
    catalog: Mapping[str, Mapping[str, Any]],
    *,
    rotate: bool,
) -> RuntimeRolePlan:
    """Validate all installed roles before choosing any mutations."""
    existing: list[str] = []
    missing: list[str] = []
    for login, group in RUNTIME_ROLES:
        group_row = catalog.get(group)
        if (
            group_row is None
            or group_row["rolcanlogin"]
            or group_row["owns_objects"]
            or any(group_row[key] for key in _ELEVATED)
            or group_row["memberships"]
        ):
            msg = f"Service group {group} is missing or has unexpected authority"
            raise RuntimeRoleError(msg)
        row = catalog.get(login)
        if row is None:
            missing.append(login)
            continue
        membership = row["memberships"]
        if (
            not row["rolcanlogin"]
            or not row["rolinherit"]
            or row["owns_objects"]
            or any(row[key] for key in _ELEVATED)
            or len(membership) != 1
            or membership[0]["role"] != group
            or not membership[0]["inherit"]
            or membership[0]["admin"]
        ):
            msg = (
                f"Runtime login {login} has unexpected authority; explicit remediation is required"
            )
            raise RuntimeRoleError(msg)
        existing.append(login)
    return RuntimeRolePlan(
        create=tuple(missing),
        authenticate=() if rotate else tuple(existing),
        rotate=tuple(existing) if rotate else (),
    )


@contextmanager
def _session(url: URL) -> Iterator[Session]:
    from lib_application.db.session import (  # noqa: PLC0415
        create_engine_for_env,
        dispose_engine,
        get_session_factory,
    )

    engine = create_engine_for_env(env="dev", db_url=url.render_as_string(hide_password=False))
    try:
        with get_session_factory(engine=engine)() as session:
            yield session
    finally:
        dispose_engine(engine)


def _url(value: str) -> URL:
    try:
        parsed = make_url(value)
    except SQLAlchemyError:
        msg = "An explicit valid PostgreSQL connection URL is required"
        raise RuntimeRoleError(msg) from None
    if (
        parsed.get_backend_name() != "postgresql"
        or not parsed.host
        or not parsed.database
        or not parsed.username
        or not parsed.password
        or any(
            key in parsed.query
            for key in (
                "host",
                "hostaddr",
                "port",
                "dbname",
                "user",
                "password",
                "service",
                "options",
            )
        )
    ):
        msg = (
            "PostgreSQL URLs require explicit host, database, login and credentials "
            "without connection overrides"
        )
        raise RuntimeRoleError(msg)
    return parsed.set(drivername="postgresql+psycopg2")


def _authenticate(url: URL, *, maintenance: bool = False) -> None:
    with _session(url) as session:
        row = session.execute(text("SELECT current_user, session_user, current_database()")).one()
        if row != (url.username, url.username, url.database):
            msg = "Database authentication resolved unexpected authority"
            raise RuntimeRoleError(msg)
        if maintenance:
            from lib_application.services.database_authority import (  # noqa: PLC0415
                require_maintenance_database_role,
            )

            require_maintenance_database_role(session)


def _require_admin(session: Session, login: str) -> None:
    row = session.execute(
        text("""
        SELECT current_user, session_user, r.rolsuper OR r.rolcreaterole
        FROM pg_catalog.pg_roles r WHERE r.rolname=current_user
    """)
    ).one()
    if row != (login, login, True) or login in {name for pair in RUNTIME_ROLES for name in pair}:
        msg = "Runtime login provisioning requires explicit administrative role authority"
        raise RuntimeRoleError(msg)


def _catalog(session: Session) -> dict[str, Mapping[str, Any]]:
    rows = session.execute(
        text("""
        SELECT r.rolname, r.rolcanlogin, r.rolinherit, r.rolsuper, r.rolcreatedb,
          r.rolcreaterole, r.rolreplication, r.rolbypassrls,
          EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend d
                  WHERE d.refclassid='pg_catalog.pg_authid'::regclass
                    AND d.refobjid=r.oid AND d.deptype='o') AS owns_objects,
          COALESCE((SELECT pg_catalog.json_agg(pg_catalog.json_build_object(
              'role', parent.rolname, 'inherit', m.inherit_option,
              'set', m.set_option, 'admin', m.admin_option))
            FROM pg_catalog.pg_auth_members m
            JOIN pg_catalog.pg_roles parent ON parent.oid=m.roleid
            WHERE m.member=r.oid), '[]'::json) AS memberships
        FROM pg_catalog.pg_roles r WHERE r.rolname=ANY(:names)
    """),
        {"names": [name for pair in RUNTIME_ROLES for name in pair]},
    ).mappings()
    return {str(row["rolname"]): dict(row) for row in rows}


def _driver(session: Session) -> Any:
    connection = session.connection().connection.driver_connection
    if connection is None:
        msg = "Runtime role connection is unavailable"
        raise RuntimeRoleError(msg)
    return connection


def _require_complete_catalog(session: Session) -> None:
    if plan_runtime_roles(_catalog(session), rotate=False).create:
        msg = "Runtime role creation is incomplete; the transaction was refused"
        raise RuntimeRoleError(msg)


def _create_login(session: Session, login: str, group: str, password: str) -> None:
    # Bypass SQLAlchemy's SQL/parameter logging for password-bearing ROLE DDL.
    connection = _driver(session)
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS PASSWORD %s"
            ).format(sql.Identifier(login)),
            (password,),
        )
        cursor.execute(
            sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET TRUE, ADMIN FALSE").format(
                sql.Identifier(group),
                sql.Identifier(login),
            )
        )


def _rotate_login(session: Session, login: str, password: str) -> None:
    connection = _driver(session)
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("ALTER ROLE {} PASSWORD %s").format(sql.Identifier(login)), (password,)
        )


def provision_runtime_roles(
    admin_url: str,
    migration_url: str,
    passwords: Mapping[str, str],
    *,
    rotate: bool = False,
) -> dict[str, int]:
    """Verify existing logins, create missing logins atomically, or explicitly rotate.

    Existing passwords are tested through independent authentication, never read
    from PostgreSQL or overwritten on routine retries. The migration identity must
    own the already-installed schema; Alembic owns the six service groups.
    """
    admin = _url(admin_url)
    migration = _url(migration_url)
    if (admin.host, admin.port or 5432, admin.database) != (
        migration.host,
        migration.port or 5432,
        migration.database,
    ):
        msg = "Admin and migration URLs must target the same database and server"
        raise RuntimeRoleError(msg)
    if set(passwords) != set(PASSWORD_ENV) or any(
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or value == "CHANGE_ME_BEFORE_USE"
        for value in passwords.values()
    ):
        msg = "All six runtime logins require explicit non-placeholder passwords"
        raise RuntimeRoleError(msg)
    try:
        _authenticate(migration, maintenance=True)
        with _session(admin) as session, session.begin():
            _require_admin(session, str(admin.username))
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            session.execute(text("SET LOCAL password_encryption = 'scram-sha-256'"))
            session.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtextextended('runtime-role-provisioning', 0))"
                )
            )
            plan = plan_runtime_roles(_catalog(session), rotate=rotate)
            for login in plan.authenticate:
                _authenticate(migration.set(username=login, password=passwords[login]))
            for login, group in RUNTIME_ROLES:
                if login in plan.create:
                    _create_login(session, login, group, passwords[login])
                elif login in plan.rotate:
                    _rotate_login(session, login, passwords[login])
            _require_complete_catalog(session)
        return {
            "created": len(plan.create),
            "verified": len(plan.authenticate),
            "rotated": len(plan.rotate),
        }
    except RuntimeRoleError:
        raise
    except (SQLAlchemyError, psycopg2.Error):
        msg = (
            "Runtime role provisioning or credential authentication failed; "
            "verify installed state before retrying"
        )
        raise RuntimeRoleError(msg) from None
