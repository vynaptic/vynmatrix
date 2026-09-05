"""Run commands for local development."""

import subprocess
from pathlib import Path

import click
from rich.console import Console

from dev_cli.utils.helpers import find_python_executable
from dev_cli.utils.helpers import get_project_root as _find_repo_root

console = Console()


@click.group()
def run():
    """Run applications locally."""


def _resolve_app_module(app_path: Path, app_name: str) -> str:
    """Return the executable module for one configured application layout."""
    package_name = app_name.replace("-", "_")
    if (app_path / package_name / "main.py").is_file():
        return f"{package_name}.main"
    if (app_path / "main.py").is_file():
        return "main"
    msg = f"Application {app_name!r} has no executable main.py under {app_path}"
    raise click.ClickException(msg)


@run.command()
@click.option("--name", "-n", required=True, help="Application name")
def app(name: str) -> None:
    """Run an application locally using its venv."""
    console.print(f"[bold blue]Running application: {name}[/bold blue]")

    # Find repo root
    repo_root = _find_repo_root()

    # Find venv
    venv_path = repo_root / "build" / "venvs" / f"app-{name}"
    if not venv_path.exists():
        msg = f"Venv not found at {venv_path}; run: vmdev build venvs --app={name}"
        raise click.ClickException(msg)

    # Get python path
    try:
        python_path = find_python_executable(venv_path)
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e

    # Run the app
    app_path = repo_root / "apps" / name
    if not app_path.exists():
        msg = f"App directory not found: {app_path}"
        raise click.ClickException(msg)
    module = _resolve_app_module(app_path, name)

    console.print(f"[cyan]Starting {name}...[/cyan]")
    try:
        result = subprocess.run(
            [str(python_path), "-m", module],
            cwd=app_path,
            check=False,
        )
    except OSError as exc:
        msg = f"Failed to start {name}: {exc}"
        raise click.ClickException(msg) from exc
    if result.returncode != 0:
        exit_code = result.returncode if result.returncode > 0 else 128 + abs(result.returncode)
        raise click.exceptions.Exit(exit_code)
