"""Canonical option-spread construction (domain logic).

Pure Black-Scholes-based calculation logic with no external dependencies.
"""

from lib_strategy.spreads.option_spreads import build_spread

__all__ = ["build_spread"]
