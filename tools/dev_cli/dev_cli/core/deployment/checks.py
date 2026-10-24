"""Validate configuration, prerequisites and installed state without changing any of it.

``doctor`` runs first inside ``deploy`` so a missing key or an unreadable Docker
fails in seconds rather than after the slowest build. Each check returns a
status and a sentence an operator can act on; nothing here connects as a
runtime role or prints a secret's value.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev_cli.core.database_lifecycle import CREATED_BIND_SOURCES
from dev_cli.core.deployment import artefact, configuration, environment

OK = "ok"
WARN = "warn"
FAIL = "fail"
_PLACEHOLDER = "CHANGE_ME_BEFORE_USE"
_REQUIRED_PYTHON = (3, 11)
#: A Docker CLI probe that hangs (Docker Desktop still starting) is a failure.
_PROBE_TIMEOUT_SECONDS = 30
#: ``DOCKER_BUILDKIT`` values the Docker CLI reads as false (Go's ParseBool).
_BUILDKIT_DISABLED = frozenset({"0", "f", "false"})


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


def check_bind_sources(root: Path) -> list[Check]:
    """Directories Compose bind-mounts must stay writable by the user who deploys.

    Docker Engine on Linux creates a missing bind source as root. An
    installation that started before ``vmdev`` created ``.artifacts`` itself can
    hold a root-owned copy, and the next pre-upgrade snapshot cannot be written.
    """
    checks = []
    for relative in CREATED_BIND_SOURCES:
        path = root / relative
        name = f"directory:{relative}"
        if not path.exists():
            checks.append(
                Check(name=name, status=OK, detail=f"{relative} will be created by `vmdev deploy`")
            )
            continue
        if not path.is_dir():
            checks.append(
                Check(
                    name=name, status=FAIL, detail=f"{relative} is not a directory; move it aside"
                )
            )
            continue
        blocked = (
            [path]
            if not _writable(path)
            else [
                child for child in sorted(path.iterdir()) if child.is_dir() and not _writable(child)
            ]
        )
        checks.append(
            Check(
                name=name,
                status=FAIL if blocked else OK,
                detail=(
                    f"{relative} is writable"
                    if not blocked
                    else ", ".join(str(item.relative_to(root)) for item in blocked)
                    + " cannot be written by this user, so a snapshot would fail; run "
                    f'`sudo chown -R "$(id -u):$(id -g)" {relative}` in the repository'
                ),
            )
        )
    return checks


def _writable(directory: Path) -> bool:
    return os.access(directory, os.R_OK | os.W_OK | os.X_OK)


def _probe(run: Callable[..., Any], command: Sequence[str]) -> str | None:
    """Stdout of a Docker CLI probe that succeeded, or None if it failed or hung."""
    try:
        result = run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return str(result.stdout).strip() if result.returncode == 0 else None


def _check_buildkit(run: Callable[..., Any], environ: Mapping[str, str]) -> Check:
    """The image build uses ``--build-context`` and ``RUN --mount``: BuildKit only."""
    buildx = _probe(run, ["docker", "buildx", "version"])
    if buildx is None:
        return Check(
            name="docker:buildx",
            status=FAIL,
            detail=(
                "docker buildx is missing; the image build needs BuildKit. Install "
                "docker-buildx-plugin (Linux) or Docker Desktop (macOS, Windows)"
            ),
        )
    if (environ.get("DOCKER_BUILDKIT") or "").strip().lower() in _BUILDKIT_DISABLED:
        return Check(
            name="docker:buildx",
            status=FAIL,
            detail=(
                "DOCKER_BUILDKIT disables BuildKit, and the legacy builder cannot build "
                "the image; unset DOCKER_BUILDKIT"
            ),
        )
    words = buildx.split()
    return Check(
        name="docker:buildx",
        status=OK,
        detail=f"docker buildx {words[1] if len(words) > 1 else buildx}",
    )


def check_prerequisites(
    *,
    run: Callable[..., Any] = subprocess.run,
    environ: Mapping[str, str] = os.environ,
) -> list[Check]:
    """Docker Engine, Compose, BuildKit and the interpreter the wheels were locked for."""
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
    compose = _probe(run, ["docker", "compose", "version", "--short"])
    checks.append(
        Check(
            name="docker:compose",
            status=OK if compose else FAIL,
            detail=(
                f"docker compose {compose}"
                if compose
                else "the Docker Compose plugin is missing; install docker-compose-plugin "
                "(Linux) or Docker Desktop (macOS, Windows)"
            ),
        )
    )
    checks.append(_check_buildkit(run, environ))
    # ``docker compose version`` and ``docker buildx version`` never contact the
    # daemon; only a server field proves it is running and this user may use it.
    server = _probe(run, ["docker", "version", "--format", "{{.Server.Version}}"])
    checks.append(
        Check(
            name="docker:daemon",
            status=OK if server else FAIL,
            detail=(
                f"Docker Engine {server} is answering"
                if server
                else "the Docker daemon is not answering; start Docker Desktop or the "
                "docker service, and on Linux make sure your user is in the docker group"
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
    "check_bind_sources",
    "check_files",
    "check_image",
    "check_keys",
    "check_owner",
    "check_prerequisites",
    "check_state",
    "check_values",
    "worst",
]
