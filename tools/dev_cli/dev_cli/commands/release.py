"""Release helpers: normalize partner version labels to SemVer and tag releases.

Image builds are SemVer-tag-gated: pushing a ``vX.Y.Z`` tag triggers
``.github/workflows/build-and-push.yml`` to build and push images to DOCR. The
partner-facing scheme ``V1_1p0`` is normalized here to the SemVer tag ``v1.1.0``
that the image build workflow expects.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import click
from rich.console import Console

from dev_cli.utils.helpers import get_project_root

console = Console()

_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_CHANGELOG_RELEASE_RE_TEMPLATE = r"^## \[{version}\] - \d{{4}}-\d{{2}}-\d{{2}}$"


def normalize_version(label: str) -> str:
    """Normalize a version label to a SemVer git tag ``vX.Y.Z``.

    Accepts canonical SemVer (``v1.1.0`` / ``1.1.0``) and the partner-facing
    scheme ``V1_1p0`` (``V``/``v`` prefix, ``_`` as the major/minor separator,
    ``p`` as the patch separator). Raises ``ValueError`` when the result is not a
    valid ``X.Y.Z`` (SemVer forbids leading-zero / non-numeric segments).
    """
    core = label.strip()
    if core[:1] in {"v", "V"}:
        core = core[1:]
    core = core.replace("_", ".").replace("p", ".").replace("P", ".")
    core = re.sub(r"\.+", ".", core).strip(".")
    if not _SEMVER_RE.match(core):
        msg = (
            f"Cannot normalize '{label}' to SemVer X.Y.Z (got '{core}'). "
            "Use e.g. V1_1p0, v1.1.0 or 1.1.0."
        )
        raise ValueError(msg)
    return f"v{core}"


def _run_git(args: list[str]) -> str:
    """Run a git command from the repo root and return stripped stdout."""
    root = get_project_root()
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _require_main_release_ref(ref: str) -> str:
    """Resolve ``ref`` and require it to be the local main tip."""
    try:
        commit_sha = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"])
        main_sha = _run_git(["rev-parse", "--verify", "main^{commit}"])
    except subprocess.CalledProcessError as exc:
        msg = (
            f"Cannot resolve release ref {ref!r} and local main. "
            "Fetch/update main and select its reviewed tip."
        )
        raise click.ClickException(msg) from exc
    if commit_sha != main_sha:
        msg = (
            f"Release ref {ref!r} resolves to {commit_sha[:12]}, not local main "
            f"tip {main_sha[:12]}. Releases must tag the reviewed main tip."
        )
        raise click.ClickException(msg)
    return commit_sha


def _require_changelog_release(version: str) -> Path:
    """Require one dated Keep-a-Changelog section for the release tag."""
    release_version = version.removeprefix("v")
    changelog_path = get_project_root() / "CHANGELOG.md"
    try:
        changelog = changelog_path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"Cannot read release changelog {changelog_path}: {exc}"
        raise click.ClickException(msg) from exc
    pattern = _CHANGELOG_RELEASE_RE_TEMPLATE.format(version=re.escape(release_version))
    matches = re.findall(pattern, changelog, flags=re.MULTILINE)
    if len(matches) != 1:
        msg = (
            f"CHANGELOG.md must contain exactly one dated release section "
            f"'## [{release_version}] - YYYY-MM-DD' before tagging"
        )
        raise click.ClickException(msg)
    return changelog_path


@click.group()
def release() -> None:
    """Release tagging helpers (SemVer-tag-gated image builds)."""


@release.command()
@click.argument("label")
def normalize(label: str) -> None:
    """Print the SemVer git tag for a version LABEL (e.g. V1_1p0 -> v1.1.0)."""
    try:
        console.print(normalize_version(label))
    except ValueError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise click.exceptions.Exit(1) from exc


@release.command()
@click.argument("label")
@click.option("--ref", default="HEAD", show_default=True, help="Commit to tag")
@click.option("--push/--no-push", default=False, help="Push the tag to origin after creating it")
@click.option("--message", default=None, help="Annotated-tag message (defaults to the tag name)")
def tag(label: str, ref: str, push: bool, message: str | None) -> None:
    """Create a SemVer release tag from a version LABEL and optionally push it.

    REF must resolve to the local main tip. Pushing a ``vX.Y.Z`` tag triggers
    the image workflow, which independently requires the current origin/main
    tip and a successful full-CI check for the exact commit.
    """
    try:
        version = normalize_version(label)
    except ValueError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise click.exceptions.Exit(1) from exc

    try:
        commit_sha = _require_main_release_ref(ref)
    except click.ClickException as exc:
        console.print(f"[red]✗ {exc.format_message()}[/red]")
        raise click.exceptions.Exit(1) from exc

    try:
        _require_changelog_release(version)
    except click.ClickException as exc:
        console.print(f"[red]✗ {exc.format_message()}[/red]")
        raise click.exceptions.Exit(1) from exc

    tag_message = message or version
    try:
        _run_git(["tag", "-a", version, commit_sha, "-m", tag_message])
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or str(exc)
        console.print(f"[red]✗ Failed to create tag {version}: {detail}[/red]")
        raise click.exceptions.Exit(1) from exc

    console.print(
        f"[bold green]✓ Created tag {version}[/bold green] (from {label}, commit {commit_sha[:12]})"
    )

    if push:
        try:
            _run_git(["push", "origin", version])
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip() or str(exc)
            console.print(f"[red]✗ Failed to push tag {version}: {detail}[/red]")
            raise click.exceptions.Exit(1) from exc
        console.print(f"[green]Pushed {version} → origin; the DOCR release build starts.[/green]")
    else:
        console.print(f"[dim]Push to trigger the release build: git push origin {version}[/dim]")
