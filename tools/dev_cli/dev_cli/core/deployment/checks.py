"""Validate configuration, prerequisites and installed state without changing any of it.

``doctor`` runs first inside ``deploy`` so a missing key or an unreadable Docker
fails in seconds rather than after the slowest build. Each check returns a
status and a sentence an operator can act on; nothing here connects as a
runtime role or prints a secret's value.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev_cli.core.deployment import artefact, configuration, environment

OK = "ok"
WARN = "warn"
FAIL = "fail"
_PLACEHOLDER = "CHANGE_ME_BEFORE_USE"
_REQUIRED_PYTHON = (3, 11)


@dataclass(frozen=True)
class Check:
    """One validated fact about this installation."""

    name: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def worst(checks: Sequence[Check]) -> str:
    """The most severe status present; ``deploy`` refuses to start on ``fail``."""
    statuses = {check.status for check in checks}
    if FAIL in statuses:
        return FAIL
    return WARN if WARN in statuses else OK


def check_files(root: Path) -> list[Check]:
    """The two private files an installation cannot run without."""
    checks = []
    for name, hint in (
        (configuration.ENV_FILE, "Run `vmdev init` to generate it."),
        (configuration.OWNER_FILE, "Run `vmdev init` to write your owner profile."),
    ):
        present = (root / name).is_file()
        checks.append(
            Check(
                name=f"file:{name}",
                status=OK if present else FAIL,
                detail=f"{name} is present" if present else f"{name} is missing. {hint}",
            )
        )
    return checks


def check_keys(root: Path) -> list[Check]:
    """Configuration keys that appeared or disappeared since ``.env`` was written."""
    example = environment.declared_keys(root / configuration.ENV_EXAMPLE)
    configured = environment.declared_keys(root / configuration.ENV_FILE)
    if not configured:
        return []
    diff = environment.key_diff(example, configured)
    checks = []
    missing = diff["missing"]
    checks.append(
        Check(
            name="configuration:new-keys",
            status=OK if not missing else FAIL,
            detail=(
                "every key in .env.example is configured"
                if not missing
                else f"{len(missing)} key(s) appeared upstream: {', '.join(missing)}. "
                "Run `vmdev init --update`."
            ),
        )
    )
    unexpected = diff["unexpected"]
    checks.append(
        Check(
            name="configuration:retired-keys",
            status=OK if not unexpected else WARN,
            detail=(
                ".env declares no key the template retired"
                if not unexpected
                else f"{len(unexpected)} key(s) are not in .env.example: {', '.join(unexpected)}"
            ),
        )
    )
    return checks


def check_values(env: Mapping[str, str]) -> list[Check]:
    """Secrets must be present, distinct and never the shipped placeholder."""
    blank = [key for key in configuration.SECRET_KEYS if not (env.get(key) or "").strip()]
    placeholder = [key for key in configuration.SECRET_KEYS if env.get(key) == _PLACEHOLDER]
    supplied = [
        value
        for value in ((env.get(key) or "").strip() for key in configuration.SECRET_KEYS)
        if value
    ]
    duplicated = len(supplied) != len(set(supplied))
    checks = [
        Check(
            name="secrets:supplied",
            status=OK if not blank and not placeholder else FAIL,
            detail=(
                "all fifteen secrets are set"
                if not blank and not placeholder
                else "unset or placeholder: " + ", ".join(sorted({*blank, *placeholder}))
            ),
        ),
        Check(
            name="secrets:distinct",
            status=FAIL if duplicated else OK,
            detail=(
                "two or more secrets share a value; every role must have its own"
                if duplicated
                else "every supplied secret is distinct"
            ),
        ),
    ]
    paper = (env.get("EXECUTION_MODE") or "paper").strip().lower()
    live = (env.get("EXECUTION_ENGINE_ALLOW_LIVE") or "false").strip().lower()
    checks.append(
        Check(
            name="safety:paper-only",
            status=OK if paper == "paper" and live == "false" else FAIL,
            detail=(
                "EXECUTION_MODE=paper with live execution disabled"
                if paper == "paper" and live == "false"
                else "this migration grants no live authority; keep EXECUTION_MODE=paper "
                "and EXECUTION_ENGINE_ALLOW_LIVE=false"
            ),
        )
    )
    return checks


def check_owner(root: Path) -> list[Check]:
    """The owner designation must be a profile bootstrap will accept."""
    import yaml  # noqa: PLC0415

    path = root / configuration.OWNER_FILE
    if not path.is_file():
        return []
    # ``OwnerOnboardingError`` is a ``ValueError``, so one clause covers both.
    from dev_cli.core.bootstrap import validate_owner_input  # noqa: PLC0415

    try:
        validate_owner_input(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return [Check(name="owner:profile", status=FAIL, detail=str(exc))]
    return [Check(name="owner:profile", status=OK, detail=f"{configuration.OWNER_FILE} is valid")]


def check_prerequisites(*, run: Callable[..., Any] = subprocess.run) -> list[Check]:
    """Docker, Compose and the interpreter the wheels were locked for."""
    checks = []
    version = sys.version_info[:2]
    checks.append(
        Check(
            name="python:version",
            status=OK if version == _REQUIRED_PYTHON else WARN,
            detail=(
                f"running Python {version[0]}.{version[1]}"
                if version == _REQUIRED_PYTHON
                else f"running Python {version[0]}.{version[1]}; wheels are locked for "
                f"{_REQUIRED_PYTHON[0]}.{_REQUIRED_PYTHON[1]}"
            ),
        )
    )
    if shutil.which("docker") is None:
        checks.append(Check(name="docker:available", status=FAIL, detail="docker is not on PATH"))
        return checks
    result = run(
        ["docker", "compose", "version", "--short"], capture_output=True, text=True, check=False
    )
    reachable = result.returncode == 0
    checks.append(
        Check(
            name="docker:available",
            status=OK if reachable else FAIL,
            detail=(
                f"docker compose {str(result.stdout).strip()}"
                if reachable
                else "docker is installed but not answering; start Docker Desktop or the daemon"
            ),
        )
    )
    return checks


def check_image(tag: str, *, run: Callable[..., Any] = subprocess.run) -> list[Check]:
    """Whether the tag Compose is pointed at exists, and what it says it holds."""
    if not tag:
        return [
            Check(
                name="image:tag",
                status=WARN,
                detail="VM_DEPLOY_IMAGE_TAG is empty; `vmdev deploy` will build and set it",
            )
        ]
    reference = f"{artefact.PLATFORM_REPOSITORY}:{tag}"
    identity = artefact.image_identity(reference, run=run)
    if identity is None:
        return [
            Check(
                name="image:tag",
                status=FAIL,
                detail=f"{reference} is not present locally; run `vmdev deploy`",
            )
        ]
    commit = identity["source_commit"] or "unstamped"
    return [
        Check(
            name="image:tag",
            status=OK if identity["source_commit"] else WARN,
            detail=f"{reference} was built from {commit}",
        )
    ]


def check_state(
    *,
    database_present: bool,
    installed_revision: str | None,
    target_head: str,
    deployed: Mapping[str, Any] | None,
    running: Mapping[str, str],
    image_tag: str,
) -> list[Check]:
    """Compare the schema, the record and what is actually running."""
    checks = [
        Check(
            name="database:present",
            status=OK if database_present else WARN,
            detail=(
                "the target database exists"
                if database_present
                else "no database yet; `vmdev deploy` will create one"
            ),
        )
    ]
    if not database_present:
        return checks
    aligned = installed_revision == target_head
    checks.append(
        Check(
            name="database:schema",
            status=OK if aligned else WARN,
            detail=(
                f"schema is at {target_head}"
                if aligned
                else f"schema is at {installed_revision or 'no revision'}; "
                f"this checkout expects {target_head}"
            ),
        )
    )
    if deployed is None:
        checks.append(
            Check(
                name="deployment:record",
                status=WARN,
                detail="no successful deployment is recorded; this stack predates `vmdev deploy`",
            )
        )
    else:
        recorded = str(deployed.get("image_tag") or "")
        checks.append(
            Check(
                name="deployment:record",
                status=OK if recorded == image_tag else WARN,
                detail=(
                    f"the record and .env agree on {recorded}"
                    if recorded == image_tag
                    else f"the record says {recorded} but .env points at {image_tag}"
                ),
            )
        )
        expected_head = str(deployed.get("alembic_head") or "")
        matched = expected_head == installed_revision
        checks.append(
            Check(
                name="deployment:schema",
                status=OK if matched else WARN,
                detail=(
                    f"the deployed build expects the installed schema {expected_head}"
                    if matched
                    else f"the deployed build expects {expected_head} but the database is at "
                    f"{installed_revision or 'no revision'}"
                ),
            )
        )
    expected = f"{artefact.PLATFORM_REPOSITORY}:{image_tag}"
    drifted = sorted(
        service for service, image in running.items() if service != "postgres" and image != expected
    )
    if running:
        checks.append(
            Check(
                name="runtime:image",
                status=OK if not drifted else WARN,
                detail=(
                    f"running services use {expected}"
                    if not drifted
                    else f"{', '.join(drifted)} are running an image other than {expected}"
                ),
            )
        )
    return checks


__all__ = [
    "FAIL",
    "OK",
    "WARN",
    "Check",
    "check_files",
    "check_image",
    "check_keys",
    "check_owner",
    "check_prerequisites",
    "check_state",
    "check_values",
    "worst",
]
