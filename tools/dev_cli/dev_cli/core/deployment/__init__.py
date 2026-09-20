"""One installed deployment: its configuration, its artefact and its record.

``vmdev init`` writes the private configuration, ``vmdev doctor`` validates it
without changing anything, and ``vmdev deploy`` installs or upgrades the stack
behind a single idempotent command. The modules here hold the logic; the click
surface is in :mod:`dev_cli.commands.deployment`.
"""

from __future__ import annotations

__all__ = [
    "artefact",
    "checks",
    "configuration",
    "environment",
    "pipeline",
    "record",
]
