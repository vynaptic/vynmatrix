"""Canonical execution-cost rate conversions."""

from decimal import Decimal

import pytest

from lib_common.execution_costs import (
    ExecutionCostRates,
    basis_points_to_fraction,
    fraction_to_basis_points,
)


def test_basis_point_fraction_round_trip_is_exact() -> None:
    rates = ExecutionCostRates.from_basis_points(
        commission_bps="10",
        slippage_bps="5",
    )

    assert rates.commission_fraction == Decimal("0.001")
    assert rates.slippage_fraction == Decimal("0.0005")
    assert rates.commission_bps == Decimal("10")
    assert rates.slippage_bps == Decimal("5")
    assert fraction_to_basis_points(basis_points_to_fraction("12.5")) == Decimal("12.5")


@pytest.mark.parametrize("value", [True, "not-a-rate", Decimal("NaN")])
def test_invalid_cost_rates_fail_closed(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        basis_points_to_fraction(value)  # type: ignore[arg-type]


def test_fraction_above_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not exceed 1"):
        ExecutionCostRates.from_fractions(
            commission_fraction="1.01",
            slippage_fraction="0",
        )
