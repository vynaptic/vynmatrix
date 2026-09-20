"""``vmdev init``, ``vmdev doctor`` and ``vmdev deploy``: install, check, upgrade.

Three scenarios share these commands and one mental model: the maintainer
iterating on a stack that holds real paper history, the maintainer adopting
someone else's merged work, and a new user cloning the repository. ``deploy``
detects which of them it is and is safe to re-run.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import click

from dev_cli.core.deployment import checks, configuration, environment
from dev_cli.utils.helpers import enable_repository_libraries

PROJECT_ROOT = Path(__file__).resolve().parents[4]
_PRIVATE_MODE = 0o600
_STATUS_MARK = {checks.OK: "ok  ", checks.WARN: "warn", checks.FAIL: "FAIL"}


def _echo(message: str) -> None:
    click.echo(message)


def _project_env() -> dict[str, str]:
    """``.env`` with the process environment on top, as every command here reads it."""
    return {**environment.load_env_file(PROJECT_ROOT / configuration.ENV_FILE), **os.environ}


def _write_private(path: Path, content: str) -> None:
    """Create an owner-only file, refusing to replace one that already exists."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _PRIVATE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def _ask(value: str | None, prompt: str, *, default: str | None = None) -> str:
    """Use the supplied flag, otherwise prompt; a non-interactive run must pass flags."""
    if value is not None:
        return value
    if not sys.stdin.isatty():
        msg = f"{prompt} is required; supply it as a flag for a non-interactive run"
        raise click.UsageError(msg)
    return str(click.prompt(prompt, default=default, show_default=default is not None))


def _report(collected: list[checks.Check], *, as_json: bool) -> str:
    if as_json:
        click.echo(
            json.dumps(
                {
                    "status": checks.worst(collected),
                    "checks": [check.as_dict() for check in collected],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return checks.worst(collected)
    for check in collected:
        click.echo(f"  [{_STATUS_MARK[check.status]}] {check.name}: {check.detail}")
    return checks.worst(collected)


def _configuration_checks(root: Path, env: dict[str, str]) -> list[checks.Check]:
    """Everything that can be validated before contacting Docker or PostgreSQL."""
    collected = [*checks.check_files(root), *checks.check_keys(root)]
    if (root / configuration.ENV_FILE).is_file():
        collected.extend(checks.check_values(env))
    collected.extend(checks.check_owner(root))
    return collected


@click.command("init")
@click.option("--email", help="The deployment owner's email address.")
@click.option("--full-name", help="Optional display name for the owner.")
@click.option("--base-currency", "base_ccy", help="Accounting currency, for example EUR.")
@click.option("--timezone", "tz", help="IANA timezone, for example Europe/Amsterdam.")
@click.option("--paper-equity", help="Starting equity of the local paper account.")
@click.option(
    "--update",
    "update_only",
    is_flag=True,
    help="Add configuration keys that appeared in .env.example; change nothing else.",
)
def init(
    email: str | None,
    full_name: str | None,
    base_ccy: str | None,
    tz: str | None,
    paper_equity: str | None,
    update_only: bool,
) -> None:
    """Write the private .env and owner profile this installation needs."""
    enable_repository_libraries(PROJECT_ROOT)
    template = PROJECT_ROOT / configuration.ENV_EXAMPLE
    env_path = PROJECT_ROOT / configuration.ENV_FILE
    owner_path = PROJECT_ROOT / configuration.OWNER_FILE
    try:
        if update_only:
            _update(template, env_path)
            return
        if env_path.is_file():
            msg = (
                f"{configuration.ENV_FILE} already exists and is never overwritten. "
                "Use `vmdev init --update` to add keys that appeared upstream."
            )
            raise click.ClickException(msg)
        owner = configuration.OwnerInputs(
            email=_ask(email, "Owner email"),
            base_ccy=_ask(base_ccy, "Accounting currency", default="EUR").upper(),
            tz=_ask(tz, "Timezone", default="UTC"),
            paper_equity=configuration.parse_equity(
                _ask(paper_equity, "Starting paper equity", default="100000")
            ),
            full_name=full_name,
        )
        values = configuration.build_values(
            owner,
            host=environment.CONTAINER_DATABASE_HOST,
            port=environment.DEFAULT_DB_PORT,
        )
        _write_private(
            env_path,
            environment.render_values(template.read_text(encoding="utf-8"), values),
        )
        if owner_path.is_file():
            click.echo(f"{configuration.OWNER_FILE} already exists and was left untouched.")
        else:
            _write_private(owner_path, configuration.render_owner_file(owner))
        _validate_written(env_path, owner_path)
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"Wrote {configuration.ENV_FILE} and {configuration.OWNER_FILE} with generated secrets.\n"
        "Both are private, owner-only and untracked: keep them out of commits and logs.\n"
        "Next: `vmdev deploy`."
    )


def _update(template: Path, env_path: Path) -> None:
    """Add keys the template gained, generating new secrets and keeping every value."""
    if not env_path.is_file():
        msg = f"{configuration.ENV_FILE} does not exist yet; run `vmdev init` first"
        raise click.ClickException(msg)
    declared = environment.declared_keys(template)
    existing = environment.load_env_file(env_path)
    diff = environment.key_diff(declared, environment.declared_keys(env_path))
    if not diff["missing"]:
        click.echo(f"{configuration.ENV_FILE} already declares every key in the template.")
        return
    values = configuration.fill_missing(
        existing, environment.load_env_file(template), diff["missing"]
    )
    env_path.write_text(
        environment.render_values(env_path.read_text(encoding="utf-8"), values),
        encoding="utf-8",
    )
    os.chmod(env_path, _PRIVATE_MODE)  # noqa: PTH101 - Path.chmod has the same effect.
    secrets_added = sorted(set(diff["missing"]) & set(configuration.SECRET_KEYS))
    click.echo(
        f"Added {len(diff['missing'])} key(s): {', '.join(diff['missing'])}."
        + (f" Generated new secrets for: {', '.join(secrets_added)}." if secrets_added else "")
    )


def _validate_written(env_path: Path, owner_path: Path) -> None:
    """Refuse to report success on a file that the lifecycle would reject."""
    import yaml  # noqa: PLC0415

    from dev_cli.core.bootstrap import BootstrapSettings, validate_owner_input  # noqa: PLC0415

    written = environment.load_env_file(env_path)
    BootstrapSettings.parse(environment.resolve_host_env(written))
    validate_owner_input(yaml.safe_load(owner_path.read_text(encoding="utf-8")))
    for path in (env_path, owner_path):
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            msg = f"{path.name} must remain owner-only"
            raise RuntimeError(msg)


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable results.")
def doctor(as_json: bool) -> None:
    """Validate configuration, prerequisites and installed state; change nothing."""
    enable_repository_libraries(PROJECT_ROOT)
    env = _project_env()
    collected = _configuration_checks(PROJECT_ROOT, env)
    collected.extend(checks.check_prerequisites())
    collected.extend(_state_checks(env))
    status = _report(collected, as_json=as_json)
    if status == checks.FAIL:
        raise SystemExit(1)


def _state_checks(env: dict[str, str]) -> list[checks.Check]:
    """Database, record and runtime checks, which need a reachable installation."""
    from dev_cli.core.deployment.pipeline import describe_state  # noqa: PLC0415

    return describe_state(PROJECT_ROOT, env)


@click.command("deploy")
@click.option(
    "--owner-config",
    default=configuration.OWNER_FILE,
    show_default=True,
    help="Reviewed YAML {profile, existing_user_id?} designating the deployment owner.",
)
@click.option("--plan", "plan_only", is_flag=True, help="Print what would happen and exit.")
@click.option("--start-only", is_flag=True, help="Start a stopped stack without changing it.")
@click.option(
    "--skip-snapshot",
    is_flag=True,
    help="Upgrade without a pre-upgrade dump. Recorded, and it disables auto-rollback.",
)
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable results.")
def deploy(
    owner_config: str,
    plan_only: bool,
    start_only: bool,
    skip_snapshot: bool,
    as_json: bool,
) -> None:
    """Install or upgrade this deployment with one idempotent command."""
    import yaml  # noqa: PLC0415
    from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415

    enable_repository_libraries(PROJECT_ROOT)
    from dev_cli.core.bootstrap import validate_owner_input  # noqa: PLC0415
    from dev_cli.core.deployment.pipeline import Deployer  # noqa: PLC0415

    if plan_only and start_only:
        msg = "--plan and --start-only cannot be combined"
        raise click.UsageError(msg)
    env = _project_env()
    try:
        preflight = _configuration_checks(PROJECT_ROOT, env)
        preflight.extend(checks.check_prerequisites())
        if checks.worst(preflight) == checks.FAIL:
            click.echo("Preflight failed; nothing was changed:")
            _report(preflight, as_json=False)
            msg = "Fix the failing checks, then run `vmdev deploy` again."
            raise click.ClickException(msg)
        deployer = Deployer(PROJECT_ROOT, env, echo=_echo)
        if start_only:
            deployer.start_only()
            click.echo("The stack is running on the image it already recorded.")
            return
        if not plan_only:
            # Every branch reads the database to decide what it is doing, so the
            # one declared service it needs comes up before anything is decided.
            deployer.start_database()
        plan = deployer.plan(skip_snapshot=skip_snapshot)
        if plan_only:
            _print_plan(plan, as_json=as_json)
            return
        owner = validate_owner_input(
            yaml.safe_load((PROJECT_ROOT / owner_config).read_text(encoding="utf-8"))
        )
        _print_plan(plan, as_json=False)
        result = deployer.deploy(plan, owner)
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    except SQLAlchemyError as exc:
        msg = "A database stage failed; inspect the installation before retrying"
        raise click.ClickException(msg) from exc
    if as_json:
        click.echo(json.dumps(result, indent=2, sort_keys=True, default=str))
        return
    port = (env.get("BACKEND_PORT") or "8081").strip()
    click.echo(
        f"\nDeployed {result['image_tag']} at schema {result['revision']}.\n"
        f"Open http://127.0.0.1:{port}/ and paste BACKEND_ADMIN_API_KEY from your .env.\n"
        "No strategy is released or bound, so nothing can trade yet."
    )


def _print_plan(plan: Any, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(plan.as_dict(), indent=2, sort_keys=True, default=str))
        return
    deployed = plan.deployed or {}
    lines = [
        f"Mode:            {plan.mode}",
        f"Source:          {plan.source_commit[:12]}{' (dirty)' if plan.source_dirty else ''}",
        f"Image:           {plan.image_tag}"
        + ("" if plan.image_present else " (not built yet)")
        + (" — will rebuild" if plan.rebuild else " — reused"),
        f"Deployed now:    {deployed.get('image_tag', 'nothing recorded')}",
        f"Schema:          {plan.current_revision or 'none'} -> {plan.target_head}",
        f"Migrations:      {len(plan.pending_revisions)} pending",
        f"Snapshot:        {plan.snapshot_path or plan.snapshot_reason}",
    ]
    if plan.pending_revisions:
        lines.append("  " + ", ".join(plan.pending_revisions))
    missing = plan.configuration_keys.get("missing") or []
    if missing:
        lines.append(f"New .env keys:   {', '.join(missing)} (run `vmdev init --update`)")
    lines.extend(f"Warning:         {warning}" for warning in plan.warnings)
    click.echo("\n".join(lines))


__all__ = ["deploy", "doctor", "init"]
