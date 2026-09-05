"""Build commands."""

from pathlib import Path

import click
from rich.console import Console

from dev_cli.core.builder import Builder
from dev_cli.core.config import load_config
from dev_cli.core.docker_builder import CacheBackend

console = Console()


@click.group()
def build():
    """Build components (wheels, venvs, docker images)."""


@build.command()
@click.option("--component", "-c", help="Specific library to build")
def libs(component: str | None) -> None:
    """Build library wheels."""
    console.print("[bold blue]Building library wheels...[/bold blue]")

    config = load_config()
    builder = Builder(config)

    if component:
        builder.build_lib(component)
    else:
        builder.build_all_libs()

    console.print("[bold green]✓ Library wheels built successfully![/bold green]")


@build.command()
@click.option("--group", "-g", help="Specific strategy group")
def strategies(group: str | None) -> None:
    """Build strategy groups (wheels only)."""
    console.print("[bold blue]Building strategy groups...[/bold blue]")

    config = load_config()
    builder = Builder(config)

    if group:
        builder.build_strategy_group(group)
    else:
        builder.build_all_strategies()

    console.print("[bold green]✓ Strategy groups built![/bold green]")


@build.command()
@click.option("--group", "-g", help="Specific strategy group")
@click.option("--app", "-a", help="Specific application")
@click.option(
    "--validation",
    is_flag=True,
    help="Build only the installed-artifact strategy-validation environment.",
)
def venvs(group: str | None, app: str | None, validation: bool) -> None:
    """Create virtual environments for strategies and apps."""
    console.print("[bold blue]Creating virtual environments...[/bold blue]")

    if sum((group is not None, app is not None, validation)) > 1:
        message = "--group, --app, and --validation are mutually exclusive"
        raise click.UsageError(message)

    config = load_config()
    builder = Builder(config)

    if group:
        builder.create_venv_for_strategy(group)
    elif app:
        builder.create_venv_for_app(app)
    elif validation:
        builder.create_strategy_validation_venv()
    else:
        builder.create_all_venvs()

    console.print("[bold green]✓ Virtual environments created![/bold green]")


@build.command()
@click.option("--tag", "-t", default="latest", help="Docker image tag")
@click.option(
    "--from-config",
    "_from_config",
    is_flag=True,
    help="Build service images defined in config/containers.yaml",
)
@click.option(
    "--config-path",
    default="config/containers.yaml",
    show_default=True,
    help="Path to containers config",
)
@click.option(
    "--cache-backend",
    type=click.Choice(["gha"]),
    default=None,
    help="Export/import BuildKit layers through the selected CI cache backend.",
)
def docker(
    tag: str,
    _from_config: bool,
    config_path: str,
    cache_backend: CacheBackend | None,
) -> None:
    """Build service images from config/containers.yaml."""
    console.print("[bold blue]Building Docker images...[/bold blue]")
    config = load_config()
    builder = Builder(config)
    # ``--from-config`` remains accepted because it is the documented command;
    # the config-driven service fleet is now the only Docker build path.
    builder.docker_builder.build_from_containers_config(
        tag,
        Path(config_path),
        cache_backend=cache_backend,
    )

    console.print("[bold green]✓ Docker images built![/bold green]")


@build.command()
@click.option("--team", "-t", required=True, help="Team name")
def team(team: str) -> None:
    """Build all components owned by a specific team."""
    console.print(f"[bold blue]Building components for team: {team}[/bold blue]")

    config = load_config()
    builder = Builder(config)
    builder.build_for_team(team)

    console.print(f"[bold green]✓ All {team} team components built![/bold green]")
