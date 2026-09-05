"""Invariant + pricing tests for the production option-spread builder (H-10).

build_spread (the canonical multi-leg builder called by the execution engine's
OptionsOrderBuilder) drives real option-spread orders via Black-Scholes pricing
and previously had ZERO tests. A strike/premium/breakeven error here silently
mis-prices live option legs, so these lock in the structural invariants and
pricing monotonicity rather than (brittle) exact BS golden values.
"""

from __future__ import annotations

import pytest

from lib_strategy.spreads.option_spreads import build_spread

_PRICE = 45000.0
_KW = {
    "underlying_symbol": "BTCUSD",
    "underlying_price": _PRICE,
    "implied_volatility": 0.5,
    "days_to_expiry": 30,
    "strike_offset_pct": 0.05,
    "spread_width_pct": 0.10,
    "quantity": 1,
    "contract_multiplier": 100.0,
}


@pytest.mark.parametrize("contract_multiplier", [0.0, -1.0, float("nan")])
def test_contract_multiplier_must_be_finite_and_positive(
    contract_multiplier: float,
) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        build_spread(
            spread_type="bull_call",
            **{**_KW, "contract_multiplier": contract_multiplier},
        )


def test_bull_call_is_a_debit_spread_with_correct_structure() -> None:
    s = build_spread(spread_type="bull_call", **_KW)
    legs = s["legs"]
    assert len(legs) == 2
    assert [leg["option_type"] for leg in legs] == ["CALL", "CALL"]
    assert [leg["side"] for leg in legs] == ["BUY", "SELL"]
    long_strike, short_strike = legs[0]["strike"], legs[1]["strike"]
    # Bull call: buy the lower strike, sell the higher.
    assert long_strike < short_strike
    # Debit spread: you pay (net_debit > 0) and that is the max loss.
    assert s["net_debit"] > 0
    assert s["max_loss"] == s["net_debit"]
    assert s["max_profit"] > 0
    # Breakeven sits strictly between the strikes.
    assert long_strike < s["breakeven"] < short_strike
    # Risk + reward exhaust the strike width (x contract multiplier).
    width = (short_strike - long_strike) * s["contract_multiplier"]
    assert abs((s["max_profit"] + s["max_loss"]) - width) < 1.0


def test_bear_put_is_a_debit_spread_long_above_short() -> None:
    s = build_spread(spread_type="bear_put", **_KW)
    legs = s["legs"]
    assert [leg["option_type"] for leg in legs] == ["PUT", "PUT"]
    assert [leg["side"] for leg in legs] == ["BUY", "SELL"]
    long_strike, short_strike = legs[0]["strike"], legs[1]["strike"]
    # Bear put: buy the higher strike, sell the lower.
    assert long_strike > short_strike
    assert s["net_debit"] > 0
    assert s["max_loss"] == s["net_debit"]


def test_bull_put_is_a_credit_spread() -> None:
    s = build_spread(spread_type="bull_put", **_KW)
    # Credit spread: net_debit is negative; you collect a credit up front.
    assert s["net_debit"] < 0
    assert s["max_profit"] > 0
    # Max profit equals the credit collected.
    assert abs(s["max_profit"] - (-s["net_debit"])) < 1.0
    assert s["max_loss"] > s["max_profit"]


def test_bear_call_is_a_credit_spread() -> None:
    s = build_spread(spread_type="bear_call", **_KW)
    assert s["net_debit"] < 0
    assert s["max_profit"] > 0
    assert s["max_loss"] > 0


@pytest.mark.parametrize("spread_type", ["straddle", "strangle"])
def test_unbounded_long_volatility_spreads_do_not_fabricate_max_profit(
    spread_type: str,
) -> None:
    spread = build_spread(spread_type=spread_type, **_KW)

    assert spread["max_profit"] is None
    assert spread["max_loss"] == spread["net_debit"]


def test_long_leg_premium_exceeds_short_leg_premium_bull_call() -> None:
    # The bought (closer-to-money) call must cost more than the sold (further) one.
    legs = build_spread(spread_type="bull_call", **_KW)["legs"]
    assert legs[0]["premium"] > legs[1]["premium"] > 0


def test_call_premium_increases_with_implied_volatility() -> None:
    # Black-Scholes monotonicity: higher IV -> richer option premium.
    low_iv = build_spread(spread_type="bull_call", **{**_KW, "implied_volatility": 0.3})
    high_iv = build_spread(spread_type="bull_call", **{**_KW, "implied_volatility": 0.8})
    assert high_iv["legs"][0]["premium"] > low_iv["legs"][0]["premium"]


def test_more_days_to_expiry_increases_premium() -> None:
    # More time value -> higher premium.
    short = build_spread(spread_type="bull_call", **{**_KW, "days_to_expiry": 7})
    long_dte = build_spread(spread_type="bull_call", **{**_KW, "days_to_expiry": 90})
    assert long_dte["legs"][0]["premium"] > short["legs"][0]["premium"]
