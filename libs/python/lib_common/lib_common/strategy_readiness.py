"""Readiness labels that certify a strategy for a capital mode.

A strategy's ``config.json`` carries ``metadata.readiness``. These sets name the
labels that count as certification; the indicator runner's process gate and the
maintenance paper release both read them, so the two can never disagree.
"""

from __future__ import annotations

from typing import Final

PAPER_READY_STATES: Final[frozenset[str]] = frozenset({"READY_FOR_PAPER_TRADING", "LIVE_CANDIDATE"})
LIVE_READY_STATES: Final[frozenset[str]] = frozenset({"LIVE_CANDIDATE"})

__all__ = ["LIVE_READY_STATES", "PAPER_READY_STATES"]
