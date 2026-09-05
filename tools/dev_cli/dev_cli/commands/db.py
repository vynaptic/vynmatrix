"""Explicit PostgreSQL stages and the bounded single-owner Compose lifecycle."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import click
import yaml

from dev_cli.commands.db_canary import activate_canary
from dev_cli.core.database_lifecycle import PlatformLifecycle
from dev_cli.utils.helpers import enable_repository_libraries

PROJECT_ROOT = Path(__file__).resolve().parents[4]
_MAX_INPUT_BYTES = 131072
_RETRY_BACKOFF = (0.1, 0.3)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _load_project_env() -> dict[str, str]:
    from lib_common.env_utils import load_dotenv_file  # noqa: PLC0415

    return load_dotenv_file(str(PROJECT_ROOT / ".env"))


def _compose_env() -> dict[str, str]:
    return {**_load_project_env(), **os.environ}


def _lifecycle() -> PlatformLifecycle:
    return PlatformLifecycle(PROJECT_ROOT, _compose_env())


def _platform_processes() -> Any:
    """Import the checkout's process composition module for host-side preflight.

    ``scripts`` is a repository directory, not an installed package, so the
    ``vmdev`` console script must add the repository root explicitly.
    """
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from scripts import platform_processes  # noqa: PLC0415

    return platform_processes


def _read_input(path: str) -> Any:
    try:
        if path == "-":
            _require(not sys.stdin.isatty(), "Redirect a reviewed configuration document to stdin")
            content = sys.stdin.read(_MAX_INPUT_BYTES + 1)
        else:
            with Path(path).open(encoding="utf-8") as stream:
                content = stream.read(_MAX_INPUT_BYTES + 1)
        _require(len(content) <= _MAX_INPUT_BYTES, "Configuration exceeds the size limit")
        return yaml.safe_load(content)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        msg = "Unable to read valid lifecycle configuration"
        raise click.ClickException(msg) from exc


def _output(value: Any) -> None:
    click.echo(json.dumps(value, sort_keys=True, default=str))


@click.group()
def db() -> None:
    """Bootstrap and maintain one PostgreSQL installation without resetting history."""
    enable_repository_libraries(PROJECT_ROOT)


db.add_command(activate_canary)


@db.command()
@click.option(
    "--owner-config",
    type=str,
    help="Reviewed YAML/JSON {profile, existing_user_id?}; - reads stdin.",
)
@click.option("--start-runtime/--no-start-runtime", default=True)
@click.option("--inside-container", is_flag=True, hidden=True)
def bootstrap(owner_config: str | None, start_runtime: bool, inside_container: bool) -> None:
    """Provision, migrate, register inactive references, and initialize the explicit owner."""
    from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415

    from dev_cli.core.bootstrap import (  # noqa: PLC0415
        BootstrapSettings,
        bootstrap_database,
        validate_owner_input,
    )
    from dev_cli.core.catalogue import load_catalogue  # noqa: PLC0415

    try:
        if inside_container:
            owner = validate_owner_input(_read_input("-"))
            settings = BootstrapSettings.parse(os.environ)
            _output(bootstrap_database(PROJECT_ROOT, settings, owner))
            return
        _require(
            owner_config is not None,
            "--owner-config is required; use profile: {} only to verify an existing owner",
        )
        assert owner_config is not None
        owner = validate_owner_input(_read_input(owner_config))
        env = _compose_env()
        BootstrapSettings.parse(env)
        load_catalogue(PROJECT_ROOT)
        lifecycle = PlatformLifecycle(PROJECT_ROOT, env)
        if start_runtime:
            processes = _platform_processes()
            runtime = {
                key: value for key, value in env.items() if key not in processes._MAINTENANCE
            }
            processes.build_processes("all", runtime)
        lifecycle.bootstrap(json.dumps(owner), start_runtime=start_runtime)
        click.echo(
            "Bootstrap completed; references remain subject to explicit strategy/account authority."
        )
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    except SQLAlchemyError as exc:
        msg = (
            "Bootstrap database stage failed; inspect the migration state before retrying. "
            "No reset was performed."
        )
        raise click.ClickException(msg) from exc


@db.command()
@click.option("--check", "check_only", is_flag=True)
@click.option("--apply", "apply_changes", is_flag=True)
@click.option("--changes", type=click.Path(exists=True, dir_okay=False))
@click.option("--strategy-id")
@click.option("--broker-code")
def catalogue(
    check_only: bool,
    apply_changes: bool,
    changes: str | None,
    strategy_id: str | None,
    broker_code: str | None,
) -> None:
    """Check/apply missing references or expected-value patches with backend authority."""
    from dataclasses import asdict  # noqa: PLC0415

    from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415

    from dev_cli.commands.user import _database_session  # noqa: PLC0415
    from dev_cli.core.catalogue import (  # noqa: PLC0415
        load_broker_references,
        load_catalogue,
        reconcile_catalogue,
    )
    from lib_application.services.catalogue_changes import apply_catalogue_changes  # noqa: PLC0415

    if check_only == apply_changes or (changes and (strategy_id or broker_code)):
        msg = "Choose exactly --check or --apply; --changes cannot combine with source selectors"
        raise click.UsageError(msg)
    try:
        payload = _read_input(changes) if changes else None
        _require(
            not changes or isinstance(payload, list),
            "Catalogue changes must be a list of expected-value patch objects",
        )
        sources = (
            None
            if changes
            else load_catalogue(PROJECT_ROOT, strategy_id=strategy_id, broker_code=broker_code)
        )
        capabilities = (
            {item["code"]: item["capabilities"] for item in load_broker_references(PROJECT_ROOT)}
            if changes
            else {}
        )
        for attempt in range(3):
            try:
                with _database_session("backend") as session:
                    result: Any
                    if changes:
                        assert isinstance(payload, list)
                        result = [
                            asdict(item)
                            for item in apply_catalogue_changes(
                                session,
                                payload,
                                dry_run=check_only,
                                broker_capabilities=capabilities,
                            )
                        ]
                    else:
                        assert sources is not None
                        result = reconcile_catalogue(session, sources, apply=apply_changes)
                    if apply_changes:
                        session.commit()
                    else:
                        session.rollback()
            except SQLAlchemyError as exc:
                code = (
                    getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", "")
                    if hasattr(exc, "orig")
                    else ""
                )
                retry = (
                    code in {"40001", "40P01"}
                    or str(code).startswith("08")
                    or getattr(exc, "connection_invalidated", False)
                )
                if not retry or attempt == len(_RETRY_BACKOFF):
                    raise
                # A new connection re-reads stable keys/desired patch state. This
                # also resolves an uncertain commit without duplicating records.
                time.sleep(_RETRY_BACKOFF[attempt])
            else:
                _output(result)
                return
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    except SQLAlchemyError as exc:
        msg = "Catalogue transaction failed; current settings were not implicitly replaced"
        raise click.ClickException(msg) from exc


@db.command()
@click.option(
    "--rotate",
    is_flag=True,
    help="Explicitly rotate all six supplied runtime passwords atomically.",
)
def roles(rotate: bool) -> None:
    """Create missing runtime logins or verify unchanged credentials under admin authority."""
    from sqlalchemy.engine import make_url  # noqa: PLC0415
    from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415

    from dev_cli.core.runtime_roles import PASSWORD_ENV, provision_runtime_roles  # noqa: PLC0415

    env = _compose_env()
    try:
        target = make_url(env.get("MIGRATION_DATABASE_URL", ""))
        admin = make_url(env.get("ADMIN_DATABASE_URL", "")).set(database=target.database)
        _output(
            provision_runtime_roles(
                admin.render_as_string(hide_password=False),
                target.render_as_string(hide_password=False),
                {login: env.get(key, "") for login, key in PASSWORD_ENV.items()},
                rotate=rotate,
            )
        )
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    except SQLAlchemyError as exc:
        msg = "Invalid database role configuration or failed role transaction; verify current state"
        raise click.ClickException(msg) from exc


@db.command()
def migrate() -> None:
    """Apply Alembic to an existing, explicitly owned database; never create or seed it."""
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415

    from dev_cli.commands.user import _database_session  # noqa: PLC0415
    from dev_cli.core.bootstrap import migrate as apply_migrations  # noqa: PLC0415

    try:
        lifecycle = _lifecycle()
        lifecycle.stop_runtime()
        with _database_session("maintenance") as session:
            _require(
                bool(session.scalar(text("SELECT pg_try_advisory_xact_lock(18472,0)"))),
                "Another migration is active",
            )
            head = apply_migrations(session.connection(), PROJECT_ROOT)
            session.commit()
        _output({"revision": head, "runtime": "stopped"})
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    except SQLAlchemyError as exc:
        msg = "Migration failed; preserve the database and inspect the failing revision"
        raise click.ClickException(msg) from exc


@db.command()
def start() -> None:
    """Start only the declared PostgreSQL service."""
    try:
        _lifecycle().command("up", "-d", "--wait", "postgres")
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@db.command()
def stop() -> None:
    """Stop workers and application gracefully, then stop PostgreSQL; retain volumes."""
    try:
        lifecycle = _lifecycle()
        lifecycle.stop_runtime()
        lifecycle.command("stop", "--timeout", "60", "postgres")
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@db.command()
def status() -> None:
    """Show declared service state without exposing environment values."""
    try:
        click.echo(_lifecycle().command("ps", "--format", "json"))
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@db.command()
@click.argument("backup_file", type=click.Path(dir_okay=False, path_type=Path))
def backup(backup_file: Path) -> None:
    """Stream a new protected archive; existing files are never overwritten."""
    from dev_cli.core.database_backup import DatabaseBackup  # noqa: PLC0415

    try:
        DatabaseBackup(_lifecycle()).backup(backup_file)
        click.echo("Backup completed; keep the archive and separate secret keys protected.")
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@db.command()
@click.argument("backup_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def restore(backup_file: Path) -> None:
    """Restore a reviewed archive to the explicit maintenance target; leave runtime stopped."""
    from dev_cli.core.database_backup import DatabaseBackup  # noqa: PLC0415

    try:
        operation = DatabaseBackup(_lifecycle())
        click.confirm(
            f"Replace database {operation.database} from this archive?", default=False, abort=True
        )
        operation.restore(backup_file)
        click.echo(
            "Restore completed; validate schema, roles and paper authority before restarting."
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@db.command()
def connect() -> None:
    """Open psql using the explicit maintenance identity, without another container."""
    from dev_cli.core.database_backup import DatabaseBackup  # noqa: PLC0415

    try:
        DatabaseBackup(_lifecycle()).connect()
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
