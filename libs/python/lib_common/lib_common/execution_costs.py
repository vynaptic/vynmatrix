"""Canonical conversion contract for deterministic execution-cost rates."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TypeAlias

RateInput: TypeAlias = Decimal | float | int | str

BASIS_POINTS_PER_UNIT = Decimal("10000")
_MAX_FRACTION = Decimal("1")


def _rate(name: str, value: RateInput) -> Decimal:
    if isinstance(value, bool):
        msg = f"{name} must be numeric"
        raise TypeError(msg)
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        msg = f"{name} must be numeric"
        raise TypeError(msg) from exc
    if not parsed.is_finite() or parsed < 0:
        msg = f"{name} must be finite and non-negative"
        raise ValueError(msg)
    return parsed


def basis_points_to_fraction(value: RateInput) -> Decimal:
    """Convert non-negative basis points to a fractional rate."""
    basis_points = _rate("basis points", value)
    if basis_points > BASIS_POINTS_PER_UNIT:
        msg = "basis points must not exceed 10,000"
        raise ValueError(msg)
    return basis_points / BASIS_POINTS_PER_UNIT


def fraction_to_basis_points(value: RateInput) -> Decimal:
    """Convert a non-negative fractional rate to basis points."""
    fraction = _rate("fraction", value)
    if fraction > _MAX_FRACTION:
        msg = "fraction must not exceed 1"
        raise ValueError(msg)
    return fraction * BASIS_POINTS_PER_UNIT


@dataclass(frozen=True, slots=True)
class ExecutionCostRates:
    """Commission and adverse slippage expressed as exact fractional rates."""

    commission_fraction: Decimal
    slippage_fraction: Decimal

    def __post_init__(self) -> None:
        commission = basis_points_to_fraction(fraction_to_basis_points(self.commission_fraction))
        slippage = basis_points_to_fraction(fraction_to_basis_points(self.slippage_fraction))
        object.__setattr__(self, "commission_fraction", commission)
        object.__setattr__(self, "slippage_fraction", slippage)

    @classmethod
    def from_basis_points(
        cls,
        *,
        commission_bps: RateInput,
        slippage_bps: RateInput,
    ) -> ExecutionCostRates:
        return cls(
            commission_fraction=basis_points_to_fraction(commission_bps),
            slippage_fraction=basis_points_to_fraction(slippage_bps),
        )

    @classmethod
    def from_fractions(
        cls,
        *,
        commission_fraction: RateInput,
        slippage_fraction: RateInput,
    ) -> ExecutionCostRates:
        return cls(
            commission_fraction=_rate("commission_fraction", commission_fraction),
            slippage_fraction=_rate("slippage_fraction", slippage_fraction),
        )

    @property
    def commission_bps(self) -> Decimal:
        return fraction_to_basis_points(self.commission_fraction)

    @property
    def slippage_bps(self) -> Decimal:
        return fraction_to_basis_points(self.slippage_fraction)


__all__ = [
    "BASIS_POINTS_PER_UNIT",
    "ExecutionCostRates",
    "basis_points_to_fraction",
    "fraction_to_basis_points",
]
