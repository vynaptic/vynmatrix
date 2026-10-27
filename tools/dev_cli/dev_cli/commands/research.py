"""Foreground owner research supervisor; submitted code runs only in the sandbox."""

from __future__ import annotations

from pathlib import Path

import click

from dev_cli.core.research.bundle import freeze_bundle, launch_bundle
from dev_cli.utils.helpers import get_project_root


@click.group()
def research() -> None:
    """Run isolated historical research without granting trading authority."""


@research.command()
@click.option("--once", is_flag=True, help="Process at most one queued job, then stop.")
def worker(once: bool) -> None:
    """Freeze trusted code and run one stoppable host supervisor in the foreground."""
    root: Path = get_project_root()
    try:
        bundle, digest = freeze_bundle(root, root / ".artifacts/research/bundles")
        click.echo(f"Research supervisor bundle: {digest}")
        result = launch_bundle(bundle, digest, root, once=once)
    except (OSError, ValueError) as exc:
        msg = "Unable to prepare the verified research supervisor bundle"
        raise click.ClickException(msg) from exc
    if result:
        msg = f"Research supervisor stopped with exit status {result}"
        raise click.ClickException(msg)
