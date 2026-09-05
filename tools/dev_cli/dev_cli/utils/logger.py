"""Logging utilities for CLI."""

import logging

from rich.console import Console
from rich.logging import RichHandler

console = Console()


def setup_logger(name: str = "vmdev", level: str = "INFO") -> logging.Logger:
    """
    Setup logger with rich handler.

    Args:
        name: Logger name
        level: Log level

    Returns:
        Logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))

    # Only add handler if not already added
    if not logger.handlers:
        handler = RichHandler(console=console, rich_tracebacks=True, tracebacks_show_locals=True)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    return logger


def get_logger(name: str = "vmdev") -> logging.Logger:
    """Get logger instance."""
    return logging.getLogger(name)
