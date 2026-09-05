"""Clean commands."""

import shutil
import subprocess

import click
from rich.console import Console

from dev_cli.utils.helpers import get_project_root

console = Console()
_PROJECT_IMAGE_PATTERN = "vynmatrix/*"


def _clean_project_docker_images() -> None:
    """Remove only locally tagged vynmatrix images."""
    try:
        listed = subprocess.run(
            [
                "docker",
                "image",
                "ls",
                "--filter",
                f"reference={_PROJECT_IMAGE_PATTERN}",
                "--format",
                "{{.Repository}}:{{.Tag}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        message = "Docker CLI is not installed"
        raise click.ClickException(message) from exc
    if listed.returncode != 0:
        detail = listed.stderr.strip() or "Docker image listing failed"
        raise click.ClickException(detail)

    image_refs = sorted(
        {
            line.strip()
            for line in listed.stdout.splitlines()
            if line.strip() and not line.endswith(":<none>")
        }
    )
    if not image_refs:
        console.print("[green]✓ No vynmatrix Docker images to clean[/green]")
        return

    removed = subprocess.run(
        ["docker", "image", "rm", *image_refs],
        capture_output=True,
        text=True,
        check=False,
    )
    if removed.returncode != 0:
        detail = removed.stderr.strip() or "Docker image removal failed"
        raise click.ClickException(detail)
    console.print(f"[green]✓ Removed {len(image_refs)} vynmatrix Docker image(s)[/green]")


@click.command()
@click.option(
    "--all",
    "-a",
    "clean_all",
    is_flag=True,
    help="Clean all repository build artifacts (default; excludes Docker)",
)
@click.option("--wheels", is_flag=True, help="Clean wheels only")
@click.option("--venvs", is_flag=True, help="Clean venvs only")
@click.option(
    "--docker",
    is_flag=True,
    help="Explicitly remove only locally tagged vynmatrix/* images",
)
def clean(clean_all: bool, wheels: bool, venvs: bool, docker: bool) -> None:
    """Clean build artifacts."""

    root = get_project_root()

    # Default to repository artifacts only. Docker cleanup always requires the
    # explicit flag because the Docker daemon is machine-global.
    if not (clean_all or wheels or venvs or docker):
        clean_all = True

    if clean_all or wheels:
        console.print("[yellow]Cleaning wheels...[/yellow]")
        wheels_dir = root / "build" / "wheels"
        if wheels_dir.exists():
            shutil.rmtree(wheels_dir)
            wheels_dir.mkdir(parents=True)
        console.print("[green]✓ Wheels cleaned[/green]")

    if clean_all or venvs:
        console.print("[yellow]Cleaning venvs...[/yellow]")
        venvs_dir = root / "build" / "venvs"
        if venvs_dir.exists():
            shutil.rmtree(venvs_dir)
            venvs_dir.mkdir(parents=True)
        console.print("[green]✓ Venvs cleaned[/green]")

    if docker:
        console.print("[yellow]Cleaning Docker images...[/yellow]")
        _clean_project_docker_images()

    if clean_all:
        console.print("[yellow]Cleaning __pycache__...[/yellow]")
        for pycache in root.rglob("__pycache__"):
            shutil.rmtree(pycache)
        console.print("[green]✓ __pycache__ cleaned[/green]")

    console.print("[bold green]✓ Clean completed![/bold green]")
