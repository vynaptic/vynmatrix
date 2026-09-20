"""What is being deployed: one stamped image, identified by the commit it holds.

Nothing stamped a commit into the platform image before this module, so the
question "which build is live?" had no answer. The build now receives the
commit as a build argument and records it as a label and a file, the image is
tagged with that commit, and Compose is pointed at that immutable tag instead
of ``latest``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PLATFORM_REPOSITORY = "vynmatrix/platform"
SVC_BASE_REPOSITORY = "vynmatrix/svc-base"
IMAGE_TAG_KEY = "VM_DEPLOY_IMAGE_TAG"
SOURCE_COMMIT_LABEL = "com.vynmatrix.source-commit"
SOURCE_DIRTY_LABEL = "com.vynmatrix.source-dirty"
BUILD_TIME_LABEL = "com.vynmatrix.build-time"
BASE_IMAGE_LABEL = "com.vynmatrix.base-image"
BUILD_INFO_PATH = "/app/BUILD_INFO.json"
#: Current generation plus the two the runbook expects to be able to roll back to.
RETAINED_GENERATIONS = 3
_SHORT_COMMIT = 12
_LISTING_FIELDS = 3

Runner = Callable[..., Any]


@dataclass(frozen=True)
class SourceState:
    """The working tree a build would capture."""

    commit: str
    dirty: bool

    @property
    def short(self) -> str:
        return self.commit[:_SHORT_COMMIT]

    @property
    def tag(self) -> str:
        """An immutable tag; a dirty tree is marked, never silently published."""
        return f"sha-{self.short}-dirty" if self.dirty else f"sha-{self.short}"


def _capture(run: Runner, args: Sequence[str], *, cwd: Path | None = None) -> str:
    result = run(list(args), cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or "").strip().splitlines()
        msg = f"{args[0]} {args[1]} failed: {detail[-1] if detail else 'no output'}"
        raise RuntimeError(msg)
    return str(result.stdout)


def source_state(root: Path, *, run: Runner = subprocess.run) -> SourceState:
    """Read the checkout's commit and whether it has uncommitted changes.

    A dirty tree is allowed: the maintainer deploys from one constantly. It is
    recorded, tagged and displayed instead of being refused.
    """
    commit = _capture(run, ["git", "rev-parse", "HEAD"], cwd=root).strip()
    if len(commit) < _SHORT_COMMIT:
        msg = "Unable to identify the source commit of this checkout"
        raise RuntimeError(msg)
    status = _capture(run, ["git", "status", "--porcelain"], cwd=root)
    return SourceState(commit=commit, dirty=bool(status.strip()))


def build_time() -> str:
    """A second-resolution UTC stamp; the image carries it as a label."""
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def inspect_image(reference: str, *, run: Runner = subprocess.run) -> dict[str, Any] | None:
    """Inspect a local image, returning ``None`` when it is not present."""
    result = run(
        ["docker", "image", "inspect", "--format", "{{json .}}", reference],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return None
    try:
        inspected = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return inspected if isinstance(inspected, dict) else None


def image_identity(reference: str, *, run: Runner = subprocess.run) -> dict[str, str] | None:
    """The identity a deployment record keeps: image id and stamped source."""
    inspected = inspect_image(reference, run=run)
    if inspected is None:
        return None
    config = inspected.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    labels = labels if isinstance(labels, dict) else {}
    digests = inspected.get("RepoDigests")
    digest = next(
        (item for item in digests if isinstance(item, str))
        if isinstance(digests, list)
        else iter(()),
        None,
    )
    return {
        "image_id": str(inspected.get("Id") or ""),
        "image_digest": digest or str(inspected.get("Id") or ""),
        "source_commit": str(labels.get(SOURCE_COMMIT_LABEL) or ""),
        "source_dirty": str(labels.get(SOURCE_DIRTY_LABEL) or ""),
        "build_time": str(labels.get(BUILD_TIME_LABEL) or ""),
        "base_image": str(labels.get(BASE_IMAGE_LABEL) or ""),
    }


def generations(repository: str, *, run: Runner = subprocess.run) -> list[tuple[str, str]]:
    """Owned tags of one repository, newest first, as ``(tag, image id)`` pairs."""
    result = run(
        [
            "docker",
            "image",
            "ls",
            repository,
            "--format",
            "{{.Tag}}\t{{.ID}}\t{{.CreatedAt}}",
            "--no-trunc",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return []
    rows: list[tuple[str, str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == _LISTING_FIELDS and parts[0] and parts[0] != "<none>":
            rows.append((parts[2], parts[0], parts[1]))
    rows.sort(reverse=True)
    return [(tag, image_id) for _, tag, image_id in rows]


def prune_generations(
    repository: str,
    *,
    keep: Iterable[str],
    retain: int = RETAINED_GENERATIONS,
    run: Runner = subprocess.run,
) -> list[str]:
    """Retain the newest ``retain`` stamped generations plus every pinned tag.

    Rollback targets are the point of retention, so the tags named in ``keep``
    are never removed regardless of age, and a tag Docker refuses to remove
    (because a container still uses it) is reported, not forced.
    """
    protected = {tag for tag in keep if tag}
    stamped = [
        (tag, image_id)
        for tag, image_id in generations(repository, run=run)
        if tag.startswith("sha-")
    ]
    survivors = {tag for tag, _ in stamped[:retain]} | protected
    removed: list[str] = []
    for tag, _ in stamped:
        if tag in survivors:
            continue
        result = run(
            ["docker", "image", "rm", f"{repository}:{tag}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            removed.append(tag)
    return removed


def compose_services(output: str) -> list[dict[str, Any]]:
    """Decode ``docker compose ps --format json``, which emits an array or JSON lines."""
    text = output.strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(decoded, dict):
        decoded = [decoded]
    return [item for item in decoded if isinstance(item, dict)]


def running_images(services: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Map each running service to the image reference it was started from."""
    return {
        str(item.get("Service") or ""): str(item.get("Image") or "")
        for item in services
        if item.get("State") == "running"
    }


def unhealthy_services(services: Sequence[Mapping[str, Any]]) -> list[str]:
    """Services that are running without reporting healthy."""
    return sorted(
        str(item.get("Service") or "")
        for item in services
        if item.get("State") == "running" and str(item.get("Health") or "") not in {"", "healthy"}
    )


__all__ = [
    "BASE_IMAGE_LABEL",
    "BUILD_INFO_PATH",
    "BUILD_TIME_LABEL",
    "IMAGE_TAG_KEY",
    "PLATFORM_REPOSITORY",
    "RETAINED_GENERATIONS",
    "SOURCE_COMMIT_LABEL",
    "SOURCE_DIRTY_LABEL",
    "SVC_BASE_REPOSITORY",
    "SourceState",
    "build_time",
    "compose_services",
    "generations",
    "image_identity",
    "inspect_image",
    "prune_generations",
    "running_images",
    "source_state",
    "unhealthy_services",
]
