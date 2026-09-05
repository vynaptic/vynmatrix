"""Lint commands."""

import subprocess
import sys

import click
from rich.console import Console

console = Console()


def _run(label: str, cmd: list[str]) -> int:
    """Run a lint command and return its process status."""
    console.print(f"[cyan]Running {label}...[/cyan]")
    result = subprocess.run(cmd, capture_output=False, check=False)
    if result.returncode == 0:
        console.print(f"[green]✓ {label} passed[/green]")
    else:
        console.print(f"[red]✗ {label} failed[/red]")
    return result.returncode


@click.command()
@click.option(
    "--include-format",
    is_flag=True,
    help="Also verify Ruff formatting without modifying files.",
)
@click.option("--mypy/--no-mypy", default=True, show_default=True, help="Run mypy.")
def lint(include_format: bool, mypy: bool) -> None:
    """Run repository lint checks."""
    commands: list[tuple[str, list[str]]] = [
        ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
    ]
    if include_format:
        commands.append(("ruff format", [sys.executable, "-m", "ruff", "format", "--check", "."]))
    if mypy:
        commands.append(("mypy", [sys.executable, "-m", "mypy", "."]))

    failures = [_run(label, cmd) for label, cmd in commands]
    if any(code != 0 for code in failures):
        raise click.exceptions.Exit(1)
    console.print("[bold green]✓ Lint checks passed[/bold green]")
