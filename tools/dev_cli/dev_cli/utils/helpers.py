"""Helper utilities."""

import sys
from pathlib import Path


def get_project_root() -> Path:
    """Get the project root directory.

    Searches upward from cwd for markers indicating the repo root.
    Priority: .git > config/build.yaml > pyproject.toml

    Returns:
        Path to project root, or cwd if not found
    """
    current = Path.cwd()

    while current != current.parent:
        # .git is most reliable marker
        if (current / ".git").exists():
            return current
        # config/build.yaml marks the repo root
        if (current / "config" / "build.yaml").exists():
            return current
        # pyproject.toml as fallback
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent

    return Path.cwd()


def find_python_executable(venv_path: Path) -> Path:
    """Find Python executable in a virtual environment."""
    if sys.platform == "win32":
        python_path = venv_path / "Scripts" / "python.exe"
    else:
        python_path = venv_path / "bin" / "python"

    if not python_path.exists():
        message = f"Python executable not found at {python_path}"
        raise FileNotFoundError(message)

    return python_path
