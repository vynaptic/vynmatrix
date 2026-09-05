"""Test commands."""

import os
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console

from dev_cli.core.config import load_config

console = Console()
NO_TESTS_COLLECTED = 5


def _pytest_env() -> dict[str, str]:
    """Use only pytest's canonical pyproject source-path configuration."""

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _run_pytest(args: list[str]) -> int:
    """Run pytest through the repository test runtime with source imports enabled."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        env=_pytest_env(),
        capture_output=False,
        check=False,
    )
    return result.returncode


def _finish_pytest(returncode: int) -> None:
    """Report pytest status and propagate failures to the shell."""
    if returncode == 0:
        console.print("[bold green]✓ All tests passed![/bold green]")
        return
    if returncode == NO_TESTS_COLLECTED:
        console.print("[yellow]⚠ No tests collected[/yellow]")
    else:
        console.print("[bold red]✗ Some tests failed[/bold red]")
    raise click.exceptions.Exit(returncode)


@click.group()
def test():
    """Run tests."""


@test.command()
def all():
    """Run all tests."""
    console.print("[bold blue]Running all tests...[/bold blue]")

    # Let pytest read pyproject.toml testpaths so app-level suites are included
    # consistently with direct `python -m pytest` runs from the repo root.
    _finish_pytest(_run_pytest([]))


@test.command()
@click.option("--name", "-n", required=True, help="Library name")
def lib(name: str) -> None:
    """Test a specific library."""
    console.print(f"[bold blue]Testing library: {name}[/bold blue]")

    lib_path = Path(f"libs/python/{name}")
    test_path = lib_path / "tests"

    if not test_path.exists():
        console.print(f"[yellow]No tests found for {name}[/yellow]")
        return

    _finish_pytest(_run_pytest([str(test_path), "-v"]))


@test.command()
@click.option("--team", "-t", required=True, help="Team name")
def team(team: str) -> None:
    """Run tests for all team components."""
    console.print(f"[bold blue]Running tests for team: {team}[/bold blue]")

    config = load_config()
    team_components = config.get("teams", {}).get(team)

    if not team_components:
        console.print(f"[red]Team {team} not found[/red]")
        return

    # Map component names to paths
    component_paths = []

    for component_name in team_components:
        # Check libs
        for lib in config.get("libs", {}).get("components", []):
            if lib["name"] == component_name:
                component_paths.append(lib["path"])
                break
        # Check strategies (handle naming variations)
        for strat in config.get("strategies", {}).get("groups", []):
            if strat["name"] == component_name or f"{strat['name']}_strategies" == component_name:
                component_paths.append(strat["path"])
                break
        # Check apps
        for app in config.get("apps", {}).get("components", []):
            if app["name"] == component_name:
                component_paths.append(app["path"])
                break

    # Run tests for each component
    tests_run = 0
    exit_code = 0
    for component_path in component_paths:
        test_path = Path(component_path) / "tests"
        if test_path.exists():
            console.print(f"[cyan]Testing {component_path}...[/cyan]")
            result = _run_pytest([str(test_path), "-v"])
            if result != 0:
                exit_code = result
            tests_run += 1
        else:
            console.print(f"[yellow]No tests found for {component_path}[/yellow]")

    if tests_run == 0:
        console.print(f"[yellow]No test directories found for team {team}[/yellow]")
    else:
        console.print(f"[green]✓ Ran tests for {tests_run} components[/green]")
    if exit_code != 0:
        raise click.exceptions.Exit(exit_code)
