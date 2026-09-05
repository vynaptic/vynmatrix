"""Configuration loader for vmdev CLI."""

from pathlib import Path
from typing import Any, cast

import yaml

from dev_cli.utils.helpers import get_project_root as _find_repo_root


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """
    Load configuration from build.yaml.

    Args:
        config_path: Path to config file. If None, uses default location.

    Returns:
        Configuration dictionary
    """
    path = (
        Path(config_path)
        if config_path is not None
        else _find_repo_root() / "config" / "build.yaml"
    )

    if not path.exists():
        message = (
            f"Configuration file not found: {path}\n"
            "Please ensure config/build.yaml exists in your repository root."
        )
        raise FileNotFoundError(message)

    with path.open("r") as f:
        return cast(dict[str, Any], yaml.safe_load(f) or {})
