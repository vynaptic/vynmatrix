"""Format commands."""

import sys

import click
from rich.console import Console

from dev_cli.core.executor import run_command

console = Console()


@click.command()
@click.option("--check", is_flag=True, help="Check only, do not modify files")
def format(check: bool) -> None:
    """Apply or verify the repository's single Ruff lint/format contract."""
    lint_args = [sys.executable, "-m", "ruff", "check"]
    format_args = [sys.executable, "-m", "ruff", "format"]
    if check:
        format_args.append("--check")
    else:
        lint_args.append("--fix")
    lint_args.append(".")
    format_args.append(".")

    console.print("[cyan]Running Ruff lint fixes...[/cyan]")
    run_command(lint_args)
    console.print("[cyan]Running Ruff formatter...[/cyan]")
    run_command(format_args)
    console.print(
        "[bold green]✓ Code format verified![/bold green]"
        if check
        else "[bold green]✓ Code formatted successfully![/bold green]"
    )
