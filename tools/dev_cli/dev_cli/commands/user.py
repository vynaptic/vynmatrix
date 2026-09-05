"""Explicit deployment-owner initialization and owner-relative lifecycle commands."""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click
import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from dev_cli.utils.helpers import enable_repository_libraries, get_project_root

_MAX_CONFIG_BYTES = 131072


@contextmanager
def _database_session(stage: str) -> Iterator[Session]:
    from lib_application.db.session import (  # noqa: PLC0415
        create_engine_for_env,
        dispose_engine,
        get_session_factory,
    )
    from lib_application.services.database_authority import (  # noqa: PLC0415
        require_backend_database_role,
        require_maintenance_database_role,
    )

    variable = "MIGRATION_DATABASE_URL" if stage == "maintenance" else "BACKEND_DATABASE_URL"
    url = os.environ.get(variable)
    if not url:
        msg = f"{variable} is required; generic DATABASE_URL is not accepted"
        raise click.ClickException(msg)
    try:
        parsed = make_url(url)
    except SQLAlchemyError as exc:
        msg = f"{variable} is invalid"
        raise click.ClickException(msg) from exc
    if parsed.get_backend_name() != "postgresql":
        msg = f"{variable} must use PostgreSQL"
        raise click.ClickException(msg)
    engine = create_engine_for_env(env="dev", db_url=url)
    try:
        with get_session_factory(engine=engine)() as session:
            checker = (
                require_maintenance_database_role
                if stage == "maintenance"
                else require_backend_database_role
            )
            checker(session)
            yield session
    finally:
        dispose_engine(engine)


def _load_mapping(path: str, *, protected: bool = False) -> dict[str, Any]:
    try:
        if path == "-":
            if not protected or sys.stdin.isatty():
                msg = "Secret stdin must be redirected from a protected source"
                raise click.ClickException(msg)
            content = sys.stdin.read(_MAX_CONFIG_BYTES + 1)
        elif protected:
            if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "geteuid"):
                msg = (
                    "Owner-only secrets files are unsupported on this platform; "
                    "use --secrets-file - with protected redirected stdin"
                )
                raise click.ClickException(msg)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "r") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077
                ):
                    msg = "Secrets file must be a regular owner-only file (mode 0600)"
                    raise click.ClickException(msg)
                content = stream.read(_MAX_CONFIG_BYTES + 1)
        else:
            with Path(path).open() as stream:
                content = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(content) > _MAX_CONFIG_BYTES:
            msg = "Configuration exceeds the size limit"
            raise click.ClickException(msg)
        value = yaml.safe_load(content)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        msg = "Unable to read a valid configuration mapping"
        raise click.ClickException(msg) from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        msg = "Configuration must contain a mapping"
        raise click.ClickException(msg)
    return value


@contextmanager
def _operation(stage: str) -> Iterator[Session]:
    from lib_application.services.deployment_owner import DeploymentOwnerError  # noqa: PLC0415

    try:
        with _database_session(stage) as session:
            yield session
            session.commit()
    except ValidationError as exc:
        msg = "Invalid lifecycle configuration; check the required fields and types"
        raise click.ClickException(msg) from exc
    except (ValueError, DeploymentOwnerError) as exc:
        raise click.ClickException(str(exc)) from exc
    except SQLAlchemyError as exc:
        msg = "Database lifecycle operation failed; verify current state before retrying"
        raise click.ClickException(msg) from exc


def _output(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    click.echo(json.dumps(value, default=str, sort_keys=True))


@click.group()
def user() -> None:
    """Initialize and manage the single deployment owner."""
    enable_repository_libraries(get_project_root())


@user.command("init")
@click.option("--email")
@click.option("--full-name")
@click.option("--base-currency", "base_ccy")
@click.option("--timezone", "tz")
@click.option(
    "--existing-user-id",
    help="Explicitly adopt a preserved existing user using maintenance authority.",
)
def init_owner(
    email: str | None,
    full_name: str | None,
    base_ccy: str | None,
    tz: str | None,
    existing_user_id: str | None,
) -> None:
    """Initialize once; reruns verify supplied fields without overwriting edits."""
    from lib_application.services.owner_onboarding import initialize_owner  # noqa: PLC0415

    profile = {
        key: value
        for key, value in {
            "email": email,
            "full_name": full_name,
            "base_ccy": base_ccy,
            "tz": tz,
        }.items()
        if value is not None
    }
    with _operation("maintenance") as session:
        result = initialize_owner(session, profile=profile, existing_user_id=existing_user_id)
    _output(result)


@user.command("show")
def show_owner() -> None:
    """Show the designated owner using backend authority."""
    from lib_application.services.owner_onboarding import get_owner_profile  # noqa: PLC0415

    with _operation("backend") as session:
        result = get_owner_profile(session)
    _output(result)


@user.command("update")
@click.option("--config", required=True, type=click.Path(exists=True, dir_okay=False))
def update_owner(config: str) -> None:
    """Apply a profile mapping containing expected and changes objects."""
    from lib_application.services.owner_onboarding import apply_owner_patch  # noqa: PLC0415

    value = _load_mapping(config)
    if set(value) != {"expected", "changes"} or any(
        not isinstance(value[key], dict) for key in value
    ):
        msg = "Profile update requires only expected and changes mappings"
        raise click.ClickException(msg)
    with _operation("backend") as session:
        result = apply_owner_patch(session, expected=value["expected"], changes=value["changes"])
    _output(result)


@user.command("account")
@click.option("--config", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--secrets-file", help="Owner-only credentials file, or - for protected redirected stdin."
)
@click.option("--existing-account-id", type=click.IntRange(min=1))
def account(config: str, secrets_file: str | None, existing_account_id: int | None) -> None:
    """Add, adopt or patch an owner account by stable config_key."""
    from lib_application.db.models import LinkedBrokerAccount  # noqa: PLC0415
    from lib_application.db.session import get_session_factory  # noqa: PLC0415
    from lib_application.services.account_onboarding import (  # noqa: PLC0415
        BrokerAccountIn,
        adopt_account,
        onboard_account,
        owner_scope,
        patch_account,
    )
    from lib_infrastructure.brokers.secrets import create_secrets_provider  # noqa: PLC0415

    value = _load_mapping(config)
    if "credentials" in value:
        msg = "Credentials must use --secrets-file, never the public configuration"
        raise click.ClickException(msg)
    if existing_account_id is not None:
        if (
            set(value) != {"config_key"}
            or not isinstance(value["config_key"], str)
            or secrets_file is not None
        ):
            msg = "Account adoption accepts only config_key and no credentials"
            raise click.ClickException(msg)
        with _operation("backend") as session:
            result = adopt_account(session, existing_account_id, value["config_key"])
    elif "expected" in value or "changes" in value:
        if (
            set(value) != {"config_key", "expected", "changes"}
            or not isinstance(value["config_key"], str)
            or not isinstance(value["expected"], dict)
            or not isinstance(value["changes"], dict)
            or secrets_file is not None
        ):
            msg = (
                "Account patch requires config_key, expected and changes; "
                "credentials use explicit rotation"
            )
            raise click.ClickException(msg)
        with _operation("backend") as session, owner_scope(session) as owner_id:
            account_id = session.scalar(
                select(LinkedBrokerAccount.account_id).where(
                    LinkedBrokerAccount.user_id == owner_id,
                    LinkedBrokerAccount.config_key == value["config_key"],
                )
            )
            if account_id is None:
                msg = "Account config_key does not exist for the deployment owner"
                raise click.ClickException(msg)
            result = patch_account(session, account_id, value["expected"], value["changes"])
    else:
        if secrets_file is not None:
            value["credentials"] = _load_mapping(secrets_file, protected=True)
        with _operation("backend") as session:
            payload = BrokerAccountIn.model_validate(value)
            provider = create_secrets_provider(
                backend="db", session_factory=get_session_factory(engine=session.get_bind().engine)
            )
            result = onboard_account(session, payload, provider)
    _output(result)
