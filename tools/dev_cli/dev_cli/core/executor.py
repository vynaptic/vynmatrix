"""Command executor utilities."""

import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from rich.console import Console

console = Console()


def run_command(
    cmd: list[str], cwd: Path | None = None, capture_output: bool = False, check: bool = True
) -> subprocess.CompletedProcess[Any]:
    """
    Run a shell command.

    Args:
        cmd: Command and arguments as list
        cwd: Working directory
        capture_output: Whether to capture stdout/stderr
        check: Whether to raise exception on error

    Returns:
        CompletedProcess instance
    """
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=capture_output, text=True, check=check)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Command failed: {' '.join(cmd)}[/red]")
        if e.stderr:
            console.print(f"[red]{e.stderr}[/red]")
        if check:
            raise
        return cast(subprocess.CompletedProcess[Any], e)


def check_command_exists(command: str) -> bool:
    """
    Check if a command exists in PATH.

    Args:
        command: Command name

    Returns:
        True if command exists
    """
    try:
        result = subprocess.run(
            ["which", command] if sys.platform != "win32" else ["where", command],
            capture_output=True,
            check=False,
        )
    except Exception:
        return False
    else:
        return result.returncode == 0
