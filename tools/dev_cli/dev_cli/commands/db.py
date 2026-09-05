"""Database management commands."""

import os
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console

console = Console()

# Project root detection
# db.py -> commands -> dev_cli -> dev_cli -> tools -> PROJECT_ROOT
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent.resolve()
SCRIPTS_DIR = PROJECT_ROOT / "scripts" / "db"
DOCKER_DIR = PROJECT_ROOT / "docker"

# Constants
TABLE_PARTS_COUNT = 2  # Expected parts when parsing table|rows output
MIN_QUOTED_VALUE_LENGTH = 2


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    """Parse a single .env line using the same rules as the helper scripts."""
    clean = line.strip()
    if not clean or clean.startswith("#") or "=" not in clean:
        return None

    key, value = clean.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None

    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1] if len(value) >= MIN_QUOTED_VALUE_LENGTH else ""
    else:
        value = value.split(" #", 1)[0].rstrip()

    return key, value


def _load_project_env() -> dict[str, str]:
    """Load environment variables from the repo-root .env file if present."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return {}

    project_env: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_dotenv_line(line)
        if parsed is None:
            continue
        key, value = parsed
        project_env[key] = value
    return project_env


def _compose_env() -> dict[str, str]:
    """Return subprocess environment with repo-root .env values applied."""
    env = os.environ.copy()
    env.update(_load_project_env())
    return env


def _get_docker_compose_cmd() -> list[str]:
    """Detect docker compose command (V2 vs V1)."""
    # Check for docker compose V2
    result = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        check=False,
        env=_compose_env(),
    )
    if result.returncode == 0:
        return ["docker", "compose"]

    # Fallback to docker-compose V1
    result = subprocess.run(
        ["docker-compose", "--version"],
        capture_output=True,
        check=False,
        env=_compose_env(),
    )
    if result.returncode == 0:
        return ["docker-compose"]

    console.print("[red]Error: Docker Compose not found. Please install Docker Desktop.[/red]")
    sys.exit(1)


def _is_windows() -> bool:
    return os.name == "nt"


def _run_manage_db(command: str, *args: str, input_text: str | None = None) -> int:
    """Run the manage_db helper script with given command."""
    script_path = SCRIPTS_DIR / ("manage_db.ps1" if _is_windows() else "manage_db.sh")
    if not script_path.exists():
        console.print(f"[red]Error: Script not found: {script_path}[/red]")
        return 1

    cmd_args = list(args)
    if _is_windows():
        translated = []
        for arg in cmd_args:
            if arg.startswith("--"):
                translated.append(arg.replace("--", "-", 1))
            else:
                translated.append(arg)
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            command,
            *translated,
        ]
    else:
        cmd = ["bash", str(script_path), command, *cmd_args]

    result = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        check=False,
        input=input_text,
        text=input_text is not None,
        env=_compose_env(),
    )
    return result.returncode


@click.group()
def db() -> None:
    """Database management commands.

    Manage PostgreSQL database for the vynmatrix platform.
    """


@db.command()
def start() -> None:
    """Start PostgreSQL container (delegates to scripts/db/manage_db)."""
    sys.exit(_run_manage_db("start"))


@db.command()
def stop() -> None:
    """Stop PostgreSQL container (delegates to scripts/db/manage_db)."""
    sys.exit(_run_manage_db("stop"))


@db.command()
def status() -> None:
    """Show database status and table counts (delegates to scripts/db/manage_db)."""
    sys.exit(_run_manage_db("status"))


@db.command()
def init() -> None:
    """Initialize database schema."""
    console.print("[cyan]Initializing database schema...[/cyan]")

    result = _run_manage_db("init")

    if result == 0:
        console.print("[bold green]✓ Database initialized![/bold green]")
    else:
        console.print("[red]✗ Failed to initialize database[/red]")
        sys.exit(1)


@db.command()
def reset() -> None:
    """Reset database (drop all tables and recreate)."""
    if not click.confirm("[yellow]This will DROP ALL DATA. Are you sure?[/yellow]", default=False):
        console.print("[dim]Cancelled[/dim]")
        return

    console.print("[cyan]Resetting database...[/cyan]")

    # Run reset non-interactively by passing 'y' to stdin
    result = _run_manage_db("reset", input_text="y\n")

    if result == 0:
        console.print("[bold green]✓ Database reset complete![/bold green]")
    else:
        console.print("[red]✗ Failed to reset database[/red]")
        sys.exit(1)


@db.command()
def connect() -> None:
    """Connect to database via psql (interactive)."""
    console.print("[cyan]Connecting to PostgreSQL...[/cyan]")
    console.print("[dim]Type \\q to exit[/dim]\n")

    docker_compose = _get_docker_compose_cmd()
    compose_file = DOCKER_DIR / "docker-compose.yml"

    subprocess.run(
        [
            *docker_compose,
            "-f",
            str(compose_file),
            "exec",
            "postgres",
            "psql",
            "-U",
            "trader",
            "-d",
            "vm_trading",
        ],
        cwd=str(DOCKER_DIR),
        check=False,
        env=_compose_env(),
    )


@db.command()
@click.argument("backup_file", required=False)
def backup(backup_file: str | None) -> None:
    """Backup database to SQL file."""
    args = [backup_file] if backup_file else []

    console.print("[cyan]Backing up database...[/cyan]")
    result = _run_manage_db("backup", *args)

    if result == 0:
        console.print("[bold green]✓ Backup complete![/bold green]")
    else:
        console.print("[red]✗ Backup failed[/red]")
        sys.exit(1)


@db.command()
@click.argument("backup_file")
def restore(backup_file: str) -> None:
    """Restore database from SQL file."""
    if not Path(backup_file).exists():
        console.print(f"[red]Error: Backup file not found: {backup_file}[/red]")
        sys.exit(1)

    if not click.confirm(
        "[yellow]This will OVERWRITE the database. Are you sure?[/yellow]", default=False
    ):
        console.print("[dim]Cancelled[/dim]")
        return

    console.print(f"[cyan]Restoring database from {backup_file}...[/cyan]")

    # Run restore non-interactively
    result = _run_manage_db("restore", backup_file, input_text="y\n")

    if result == 0:
        console.print("[bold green]✓ Restore complete![/bold green]")
    else:
        console.print("[red]✗ Restore failed[/red]")
        sys.exit(1)


@db.command()
def pgadmin() -> None:
    """Start pgAdmin web UI."""
    console.print("[cyan]Starting pgAdmin...[/cyan]")

    docker_compose = _get_docker_compose_cmd()
    compose_file = DOCKER_DIR / "docker-compose.yml"
    compose_env = _compose_env()
    email = compose_env.get("PGADMIN_EMAIL", "").strip()
    password = compose_env.get("PGADMIN_PASSWORD", "").strip()
    if not email or not password or password == "CHANGE_ME_BEFORE_USE":
        msg = "Set PGADMIN_EMAIL and a non-placeholder PGADMIN_PASSWORD in the repo-root .env"
        raise click.UsageError(msg)

    result = subprocess.run(
        [*docker_compose, "-f", str(compose_file), "--profile", "admin", "up", "-d", "pgadmin"],
        cwd=str(DOCKER_DIR),
        check=False,
        env=compose_env,
    )

    if result.returncode == 0:
        port = compose_env.get("PGADMIN_PORT", "5050")
        console.print("[bold green]✓ pgAdmin started![/bold green]")
        console.print(f"\n[cyan]Access:[/cyan] http://localhost:{port}")
        console.print(f"[cyan]Login:[/cyan] {email}")
        console.print("[dim]Password: use PGADMIN_PASSWORD from the repo-root .env[/dim]")
    else:
        console.print("[red]✗ Failed to start pgAdmin[/red]")
        sys.exit(1)
