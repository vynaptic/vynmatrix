"""Maintenance-only paper release and withdrawal of one strategy version, under vmdev db."""

from __future__ import annotations

import os

import click

from dev_cli.commands.user import _operation, _output
from dev_cli.core.deployment import environment as deployment_environment
from dev_cli.utils.helpers import get_project_root

_GATES = ("ENV", "ENVIRONMENT", "EXECUTION_MODE", "EXECUTION_ENGINE_ALLOW_LIVE")
_IMAGE_TAG = "VM_DEPLOY_IMAGE_TAG"


def _configured() -> dict[str, str]:
    """The configuration the stack and the database URL use: ``.env`` under the shell."""
    return {
        **deployment_environment.load_env_file(get_project_root() / ".env"),
        **os.environ,
    }


def _runtime() -> tuple[str, str, str]:
    """Resolve the paper/live gates; a missing value is a refusal, not a default."""
    configured = _configured()
    values = {name: str(configured.get(name) or "").strip() for name in _GATES}
    if missing := [name for name, value in values.items() if not value]:
        msg = f"{', '.join(missing)} must be set explicitly for a strategy release"
        raise click.ClickException(msg)
    if values["ENV"] != values["ENVIRONMENT"]:
        msg = "ENV and ENVIRONMENT must agree for a strategy release"
        raise click.ClickException(msg)
    return values["ENVIRONMENT"], values["EXECUTION_MODE"], values["EXECUTION_ENGINE_ALLOW_LIVE"]


def _require_mode(check: bool, apply: bool, reason: str | None) -> None:
    if check == apply:
        msg = "Choose exactly one of --check or --apply"
        raise click.UsageError(msg)
    if check and reason is not None:
        msg = "--reason is recorded only with --apply"
        raise click.UsageError(msg)


_strategy_id = click.option("--strategy-id", required=True, help="Registered strategy ID.")
_version = click.option("--version", required=True, help="Exact registered semver.")
_check = click.option("--check", is_flag=True, help="Run every check and write nothing.")
_apply = click.option("--apply", is_flag=True, help="Make the change and audit it.")
_reason = click.option("--reason", help="Why; recorded in the audit row (required with --apply).")


@click.command("release-strategy")
@_strategy_id
@_version
@_check
@_apply
@_reason
def release_strategy(
    strategy_id: str, version: str, check: bool, apply: bool, reason: str | None
) -> None:
    """Release one exact registered version for paper trading; never grant live authority."""
    from dev_cli.core.strategy_release import (  # noqa: PLC0415
        load_packaged_release,
        strategy_tree_at,
    )
    from lib_application.services.strategy_release import (  # noqa: PLC0415
        ReleaseSource,
        current_deployment,
        release_strategy_version,
        require_release_labels,
    )

    _require_mode(check, apply, reason)
    environment, execution_mode, allow_live = _runtime()
    root = get_project_root()
    try:
        packaged = load_packaged_release(root, strategy_id=strategy_id, version=version)
        require_release_labels(
            strategy_id, readiness=packaged.readiness, decision=packaged.decision
        )
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    image_tag = str(_configured().get(_IMAGE_TAG) or "").strip()
    with _operation("maintenance") as session:
        deployment = current_deployment(session)
        if image_tag != deployment.image_tag:
            # Compose runs whatever tag .env names; a tag set by other means
            # leaves the deployment record describing an image that is not running.
            msg = (
                f"{_IMAGE_TAG}={image_tag or '<unset>'} is not the image of deployment "
                f"{deployment.deployment_id} ({deployment.image_tag}); deploy with vmdev deploy"
            )
            raise ValueError(msg)
        deployed_tree = strategy_tree_at(
            root, deployment.source_commit, packaged.release.strategy_name
        )
        if deployed_tree != packaged.strategy_tree:
            msg = (
                f"{packaged.release.strategy_name} at HEAD {packaged.source_commit[:12]} "
                f"differs from deployment {deployment.deployment_id} "
                f"({deployment.source_commit[:12]}); deploy this commit first"
            )
            raise ValueError(msg)
        source = ReleaseSource(
            release=packaged.release,
            readiness=packaged.readiness,
            decision=packaged.decision,
            source_commit=packaged.source_commit,
            config_sha256=packaged.config_sha256,
            strategy_tree=packaged.strategy_tree,
            deployment_id=int(deployment.deployment_id),
        )
        result = release_strategy_version(
            session,
            source,
            environment=environment,
            execution_mode=execution_mode,
            allow_live=allow_live,
            reason=reason,
            apply=apply,
        )
    _output(result)


@click.command("withdraw-strategy")
@_strategy_id
@_version
@_check
@_apply
@_reason
def withdraw_strategy(
    strategy_id: str, version: str, check: bool, apply: bool, reason: str | None
) -> None:
    """Withdraw a paper release; refused while any binding or work still uses it."""
    from lib_application.services.strategy_release import (  # noqa: PLC0415
        withdraw_strategy_version,
    )

    _require_mode(check, apply, reason)
    environment, execution_mode, allow_live = _runtime()
    with _operation("maintenance") as session:
        result = withdraw_strategy_version(
            session,
            strategy_id=strategy_id,
            version=version,
            environment=environment,
            execution_mode=execution_mode,
            allow_live=allow_live,
            reason=reason,
            apply=apply,
        )
    _output(result)
