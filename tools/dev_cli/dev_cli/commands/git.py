"""Git workflow commands."""

from pathlib import Path

import click
from rich.console import Console

from dev_cli.core.git_mr import (
    GitMRFlowError,
    SubmitOptions,
    install_git_mr_alias_and_hooks,
    submit_from_main,
)
from dev_cli.utils.helpers import get_project_root

console = Console()


def _ensure_prepush_executable(repo_root: Path) -> None:
    """Set executable bit on the pre-push hook for POSIX systems."""
    hook_path = repo_root / ".githooks" / "pre-push"
    if not hook_path.exists():
        return

    try:
        current_mode = hook_path.stat().st_mode
        hook_path.chmod(current_mode | 0o111)
    except OSError:
        # Non-fatal on Windows or restricted filesystems.
        pass


@click.group()
def git() -> None:
    """Git workflow commands."""


@git.command()
def install() -> None:
    """Install git alias and hooks path for MR workflow."""
    repo_root = get_project_root()
    _ensure_prepush_executable(repo_root)

    try:
        result = install_git_mr_alias_and_hooks(repo_root)
    except GitMRFlowError as exc:
        console.print(f"[red]✗ Install failed: {exc}[/red]")
        raise click.exceptions.Exit(1) from exc

    console.print("[bold green]✓ Git MR workflow installed[/bold green]")
    console.print(f"[cyan]alias.mr[/cyan] = {result.alias_value}")
    console.print(f"[cyan]core.hooksPath[/cyan] = {result.hooks_path_value}")
    console.print("[dim]Verify with: git mr --help[/dim]")


@git.group()
def mr() -> None:
    """Merge-request style workflows (GitHub pull requests)."""


@mr.command()
@click.option("--draft", is_flag=True, help="Create pull request as draft")
@click.option("--title", help="Explicit PR title (defaults to commit-derived)")
@click.option("--body", help="Explicit PR body (defaults to commit-derived)")
def submit(draft: bool, title: str | None, body: str | None) -> None:
    """Submit local main commits to GitHub PR targeting main."""
    repo_root = get_project_root()
    options = SubmitOptions(draft=draft, title=title, body=body)

    try:
        result = submit_from_main(repo_root, options)
    except GitMRFlowError as exc:
        console.print(f"[red]✗ Submit failed: {exc}[/red]")
        raise click.exceptions.Exit(1) from exc

    console.print("[bold green]✓ PR created successfully[/bold green]")
    console.print(f"[cyan]Branch:[/cyan] {result.branch_name}")
    console.print(f"[cyan]PR:[/cyan] {result.pr_url}")
    console.print("[green]Local main has been reset to origin/main.[/green]")
