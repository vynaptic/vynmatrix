"""Explicit development canary activation, registered under vmdev db."""

from __future__ import annotations

import os

import click

from dev_cli.commands.user import _operation, _output
from dev_cli.utils.helpers import get_project_root


@click.command("activate-canary")
@click.option("--strategy-id", required=True)
@click.option("--version", required=True)
def activate_canary(strategy_id: str, version: str) -> None:
    """Activate an existing dev-only E2E canary; never authorize strategy promotion."""
    from dev_cli.core.canary_activation import load_canary  # noqa: PLC0415
    from lib_application.services.canary_activation import (  # noqa: PLC0415
        activate_canary as activate,
    )

    environment = os.environ.get("ENVIRONMENT", os.environ.get("ENV", ""))
    if os.environ.get("ENV", environment) != environment:
        msg = "ENV and ENVIRONMENT must agree for canary activation"
        raise click.ClickException(msg)
    try:
        source = load_canary(get_project_root(), strategy_id=strategy_id, version=version)
    except (ValueError, OSError) as exc:
        msg = "Unable to validate the exact enabled dev-only canary source"
        raise click.ClickException(msg) from exc
    with _operation("maintenance") as session:
        result = activate(
            session,
            source,
            environment=environment,
            execution_mode=os.environ.get("EXECUTION_MODE", ""),
            allow_live=os.environ.get("EXECUTION_ENGINE_ALLOW_LIVE", ""),
        )
    _output(result)
