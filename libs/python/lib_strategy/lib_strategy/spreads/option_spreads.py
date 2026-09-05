"""Canonical option-spread construction for execution adapters.

The builder supports four vertical spreads plus iron condor, straddle, and
strangle. Callers must supply the observed contract multiplier for the
selected option contract; no asset-class multiplier is assumed.
"""

import math
from datetime import timedelta

from lib_common.time_utils import now_utc
from lib_strategy.types import OptionLeg, OptionSide, OptionSpread, OptionType, SpreadType


def build_spread(  # noqa: PLR0911
    spread_type: SpreadType,
    underlying_symbol: str,
    underlying_price: float,
    contract_multiplier: float,
    implied_volatility: float = 0.5,
    days_to_expiry: int = 30,
    strike_offset_pct: float = 0.05,
    spread_width_pct: float = 0.10,
    risk_amount: float | None = None,
    quantity: int | None = None,
) -> OptionSpread:
    """Build a canonical, contract-aware option spread for execution adapters.

    Pricing uses Black-Scholes estimates. ``contract_multiplier`` is required
    and must come from the selected broker contract's observed metadata; the
    builder never assumes a multiplier from an asset class or symbol.
    """
    try:
        contract_multiplier = float(contract_multiplier)
    except (TypeError, ValueError) as exc:
        msg = "contract_multiplier must be a finite positive number"
        raise ValueError(msg) from exc
    if not math.isfinite(contract_multiplier) or contract_multiplier <= 0:
        msg = "contract_multiplier must be a finite positive number"
        raise ValueError(msg)

    if spread_type == "bull_call":
        return _build_bull_call(
            underlying_symbol,
            underlying_price,
            implied_volatility,
            days_to_expiry,
            strike_offset_pct,
            spread_width_pct,
            risk_amount,
            quantity,
            contract_multiplier,
        )
    if spread_type == "bear_put":
        return _build_bear_put(
            underlying_symbol,
            underlying_price,
            implied_volatility,
            days_to_expiry,
            strike_offset_pct,
            spread_width_pct,
            risk_amount,
            quantity,
            contract_multiplier,
        )
    if spread_type == "bull_put":
        return _build_bull_put(
            underlying_symbol,
            underlying_price,
            implied_volatility,
            days_to_expiry,
            strike_offset_pct,
            spread_width_pct,
            risk_amount,
            quantity,
            contract_multiplier,
        )
    if spread_type == "bear_call":
        return _build_bear_call(
            underlying_symbol,
            underlying_price,
            implied_volatility,
            days_to_expiry,
            strike_offset_pct,
            spread_width_pct,
            risk_amount,
            quantity,
            contract_multiplier,
        )
    if spread_type == "iron_condor":
        return _build_iron_condor(
            underlying_symbol,
            underlying_price,
            implied_volatility,
            days_to_expiry,
            strike_offset_pct,
            spread_width_pct,
            risk_amount,
            quantity,
            contract_multiplier,
        )
    if spread_type == "straddle":
        return _build_straddle(
            underlying_symbol,
            underlying_price,
            implied_volatility,
            days_to_expiry,
            risk_amount,
            quantity,
            contract_multiplier,
        )
    if spread_type == "strangle":
        return _build_strangle(
            underlying_symbol,
            underlying_price,
            implied_volatility,
            days_to_expiry,
            strike_offset_pct,
            risk_amount,
            quantity,
            contract_multiplier,
        )
    msg = f"Unsupported options spread type: {spread_type}"
    raise ValueError(msg)


def _build_bull_call(
    underlying_symbol: str,
    price: float,
    iv: float,
    days_to_expiry: int,
    strike_offset_pct: float,
    spread_width_pct: float,
    risk_amount: float | None,
    quantity: int | None,
    contract_multiplier: float,
) -> OptionSpread:
    long_strike = price * (1 + strike_offset_pct)
    short_strike = long_strike * (1 + spread_width_pct)

    long_strike = _round_strike(long_strike, price)
    short_strike = _round_strike(short_strike, price)

    long_premium = _estimate_call_price(price, long_strike, iv, days_to_expiry)
    short_premium = _estimate_call_price(price, short_strike, iv, days_to_expiry)

    net_debit = long_premium - short_premium
    spread_width = short_strike - long_strike
    max_profit = spread_width - net_debit
    max_loss = net_debit

    contracts = _contracts_for_risk(risk_amount, max_loss, quantity, contract_multiplier)
    expiry = _expiry_date(days_to_expiry)

    legs = [
        _leg("CALL", long_strike, expiry, "BUY", contracts, long_premium),
        _leg("CALL", short_strike, expiry, "SELL", contracts, short_premium),
    ]

    return _spread_result(
        spread_type="bull_call",
        underlying_symbol=underlying_symbol,
        underlying_price=price,
        implied_volatility=iv,
        legs=legs,
        net_debit=net_debit * contracts * contract_multiplier,
        max_profit=max_profit * contracts * contract_multiplier,
        max_loss=max_loss * contracts * contract_multiplier,
        breakeven=long_strike + net_debit,
        probability_of_profit=_estimate_pop(price, long_strike + net_debit, iv, days_to_expiry),
        days_to_expiry=days_to_expiry,
        notes=f"Bull Call Spread: {contracts} contracts",
        contract_multiplier=contract_multiplier,
    )


def _build_bear_put(
    underlying_symbol: str,
    price: float,
    iv: float,
    days_to_expiry: int,
    strike_offset_pct: float,
    spread_width_pct: float,
    risk_amount: float | None,
    quantity: int | None,
    contract_multiplier: float,
) -> OptionSpread:
    long_strike = price * (1 - strike_offset_pct)
    short_strike = long_strike * (1 - spread_width_pct)

    long_strike = _round_strike(long_strike, price)
    short_strike = _round_strike(short_strike, price)

    long_premium = _estimate_put_price(price, long_strike, iv, days_to_expiry)
    short_premium = _estimate_put_price(price, short_strike, iv, days_to_expiry)

    net_debit = long_premium - short_premium
    spread_width = long_strike - short_strike
    max_profit = spread_width - net_debit
    max_loss = net_debit

    contracts = _contracts_for_risk(risk_amount, max_loss, quantity, contract_multiplier)
    expiry = _expiry_date(days_to_expiry)

    legs = [
        _leg("PUT", long_strike, expiry, "BUY", contracts, long_premium),
        _leg("PUT", short_strike, expiry, "SELL", contracts, short_premium),
    ]

    return _spread_result(
        spread_type="bear_put",
        underlying_symbol=underlying_symbol,
        underlying_price=price,
        implied_volatility=iv,
        legs=legs,
        net_debit=net_debit * contracts * contract_multiplier,
        max_profit=max_profit * contracts * contract_multiplier,
        max_loss=max_loss * contracts * contract_multiplier,
        breakeven=long_strike - net_debit,
        probability_of_profit=_estimate_pop(price, long_strike - net_debit, iv, days_to_expiry),
        days_to_expiry=days_to_expiry,
        notes=f"Bear Put Spread: {contracts} contracts",
        contract_multiplier=contract_multiplier,
    )


def _build_bull_put(
    underlying_symbol: str,
    price: float,
    iv: float,
    days_to_expiry: int,
    strike_offset_pct: float,
    spread_width_pct: float,
    risk_amount: float | None,
    quantity: int | None,
    contract_multiplier: float,
) -> OptionSpread:
    short_strike = price * (1 - strike_offset_pct)
    long_strike = short_strike * (1 - spread_width_pct)

    short_strike = _round_strike(short_strike, price)
    long_strike = _round_strike(long_strike, price)

    short_premium = _estimate_put_price(price, short_strike, iv, days_to_expiry)
    long_premium = _estimate_put_price(price, long_strike, iv, days_to_expiry)

    net_credit = short_premium - long_premium
    spread_width = short_strike - long_strike
    max_profit = net_credit
    max_loss = spread_width - net_credit

    contracts = _contracts_for_risk(risk_amount, max_loss, quantity, contract_multiplier)
    expiry = _expiry_date(days_to_expiry)

    legs = [
        _leg("PUT", short_strike, expiry, "SELL", contracts, short_premium),
        _leg("PUT", long_strike, expiry, "BUY", contracts, long_premium),
    ]

    return _spread_result(
        spread_type="bull_put",
        underlying_symbol=underlying_symbol,
        underlying_price=price,
        implied_volatility=iv,
        legs=legs,
        net_debit=-net_credit * contracts * contract_multiplier,
        max_profit=max_profit * contracts * contract_multiplier,
        max_loss=max_loss * contracts * contract_multiplier,
        breakeven=short_strike - net_credit,
        probability_of_profit=_estimate_pop(price, short_strike - net_credit, iv, days_to_expiry),
        days_to_expiry=days_to_expiry,
        notes=f"Bull Put Spread (credit): {contracts} contracts",
        contract_multiplier=contract_multiplier,
    )


def _build_bear_call(
    underlying_symbol: str,
    price: float,
    iv: float,
    days_to_expiry: int,
    strike_offset_pct: float,
    spread_width_pct: float,
    risk_amount: float | None,
    quantity: int | None,
    contract_multiplier: float,
) -> OptionSpread:
    short_strike = price * (1 + strike_offset_pct)
    long_strike = short_strike * (1 + spread_width_pct)

    short_strike = _round_strike(short_strike, price)
    long_strike = _round_strike(long_strike, price)

    short_premium = _estimate_call_price(price, short_strike, iv, days_to_expiry)
    long_premium = _estimate_call_price(price, long_strike, iv, days_to_expiry)

    net_credit = short_premium - long_premium
    spread_width = long_strike - short_strike
    max_profit = net_credit
    max_loss = spread_width - net_credit

    contracts = _contracts_for_risk(risk_amount, max_loss, quantity, contract_multiplier)
    expiry = _expiry_date(days_to_expiry)

    legs = [
        _leg("CALL", short_strike, expiry, "SELL", contracts, short_premium),
        _leg("CALL", long_strike, expiry, "BUY", contracts, long_premium),
    ]

    return _spread_result(
        spread_type="bear_call",
        underlying_symbol=underlying_symbol,
        underlying_price=price,
        implied_volatility=iv,
        legs=legs,
        net_debit=-net_credit * contracts * contract_multiplier,
        max_profit=max_profit * contracts * contract_multiplier,
        max_loss=max_loss * contracts * contract_multiplier,
        breakeven=short_strike + net_credit,
        probability_of_profit=_estimate_pop(price, short_strike + net_credit, iv, days_to_expiry),
        days_to_expiry=days_to_expiry,
        notes=f"Bear Call Spread (credit): {contracts} contracts",
        contract_multiplier=contract_multiplier,
    )


def _build_iron_condor(
    underlying_symbol: str,
    price: float,
    iv: float,
    days_to_expiry: int,
    strike_offset_pct: float,
    spread_width_pct: float,
    risk_amount: float | None,
    quantity: int | None,
    contract_multiplier: float,
) -> OptionSpread:
    put_short_strike = price * (1 - strike_offset_pct)
    put_long_strike = put_short_strike * (1 - spread_width_pct / 2)
    call_short_strike = price * (1 + strike_offset_pct)
    call_long_strike = call_short_strike * (1 + spread_width_pct / 2)

    put_short_strike = _round_strike(put_short_strike, price)
    put_long_strike = _round_strike(put_long_strike, price)
    call_short_strike = _round_strike(call_short_strike, price)
    call_long_strike = _round_strike(call_long_strike, price)

    put_short_prem = _estimate_put_price(price, put_short_strike, iv, days_to_expiry)
    put_long_prem = _estimate_put_price(price, put_long_strike, iv, days_to_expiry)
    call_short_prem = _estimate_call_price(price, call_short_strike, iv, days_to_expiry)
    call_long_prem = _estimate_call_price(price, call_long_strike, iv, days_to_expiry)

    net_credit = (put_short_prem - put_long_prem) + (call_short_prem - call_long_prem)
    max_width = max(put_short_strike - put_long_strike, call_long_strike - call_short_strike)
    max_loss = max_width - net_credit
    max_profit = net_credit

    contracts = _contracts_for_risk(risk_amount, max_loss, quantity, contract_multiplier)
    expiry = _expiry_date(days_to_expiry)

    legs = [
        _leg("PUT", put_long_strike, expiry, "BUY", contracts, put_long_prem),
        _leg("PUT", put_short_strike, expiry, "SELL", contracts, put_short_prem),
        _leg("CALL", call_short_strike, expiry, "SELL", contracts, call_short_prem),
        _leg("CALL", call_long_strike, expiry, "BUY", contracts, call_long_prem),
    ]

    return _spread_result(
        spread_type="iron_condor",
        underlying_symbol=underlying_symbol,
        underlying_price=price,
        implied_volatility=iv,
        legs=legs,
        net_debit=-net_credit * contracts * contract_multiplier,
        max_profit=max_profit * contracts * contract_multiplier,
        max_loss=max_loss * contracts * contract_multiplier,
        breakeven=put_short_strike,
        probability_of_profit=0.65,
        days_to_expiry=days_to_expiry,
        notes=(
            f"Iron Condor: {contracts} contracts, "
            f"range ${put_short_strike:.0f}-${call_short_strike:.0f}"
        ),
        contract_multiplier=contract_multiplier,
    )


def _build_straddle(
    underlying_symbol: str,
    price: float,
    iv: float,
    days_to_expiry: int,
    risk_amount: float | None,
    quantity: int | None,
    contract_multiplier: float,
) -> OptionSpread:
    strike = _round_strike(price, price)

    call_prem = _estimate_call_price(price, strike, iv, days_to_expiry)
    put_prem = _estimate_put_price(price, strike, iv, days_to_expiry)

    net_debit = call_prem + put_prem
    max_loss = net_debit

    contracts = _contracts_for_risk(risk_amount, max_loss, quantity, contract_multiplier)
    expiry = _expiry_date(days_to_expiry)

    legs = [
        _leg("CALL", strike, expiry, "BUY", contracts, call_prem),
        _leg("PUT", strike, expiry, "BUY", contracts, put_prem),
    ]

    return _spread_result(
        spread_type="straddle",
        underlying_symbol=underlying_symbol,
        underlying_price=price,
        implied_volatility=iv,
        legs=legs,
        net_debit=net_debit * contracts * contract_multiplier,
        max_profit=None,
        max_loss=max_loss * contracts * contract_multiplier,
        breakeven=strike + net_debit,
        probability_of_profit=0.35,
        days_to_expiry=days_to_expiry,
        notes=f"Long Straddle: {contracts} contracts @ ${strike:.0f}",
        contract_multiplier=contract_multiplier,
    )


def _build_strangle(
    underlying_symbol: str,
    price: float,
    iv: float,
    days_to_expiry: int,
    strike_offset_pct: float,
    risk_amount: float | None,
    quantity: int | None,
    contract_multiplier: float,
) -> OptionSpread:
    call_strike = _round_strike(price * (1 + strike_offset_pct), price)
    put_strike = _round_strike(price * (1 - strike_offset_pct), price)

    call_prem = _estimate_call_price(price, call_strike, iv, days_to_expiry)
    put_prem = _estimate_put_price(price, put_strike, iv, days_to_expiry)

    net_debit = call_prem + put_prem
    max_loss = net_debit

    contracts = _contracts_for_risk(risk_amount, max_loss, quantity, contract_multiplier)
    expiry = _expiry_date(days_to_expiry)

    legs = [
        _leg("CALL", call_strike, expiry, "BUY", contracts, call_prem),
        _leg("PUT", put_strike, expiry, "BUY", contracts, put_prem),
    ]

    return _spread_result(
        spread_type="strangle",
        underlying_symbol=underlying_symbol,
        underlying_price=price,
        implied_volatility=iv,
        legs=legs,
        net_debit=net_debit * contracts * contract_multiplier,
        max_profit=None,
        max_loss=max_loss * contracts * contract_multiplier,
        breakeven=call_strike + net_debit,
        probability_of_profit=0.30,
        days_to_expiry=days_to_expiry,
        notes=f"Long Strangle: {contracts} contracts, ${put_strike:.0f}/${call_strike:.0f}",
        contract_multiplier=contract_multiplier,
    )


def _spread_result(
    *,
    spread_type: SpreadType,
    underlying_symbol: str,
    underlying_price: float,
    implied_volatility: float,
    legs: list[OptionLeg],
    net_debit: float,
    max_profit: float | None,
    max_loss: float,
    breakeven: float,
    probability_of_profit: float,
    days_to_expiry: int,
    notes: str,
    contract_multiplier: float,
) -> OptionSpread:
    result: OptionSpread = {
        "spread_type": spread_type,
        "underlying": underlying_symbol,
        "underlying_price": underlying_price,
        "legs": legs,
        "net_debit": net_debit,
        "max_profit": max_profit,
        "max_loss": max_loss,
        "breakeven": breakeven,
        "probability_of_profit": probability_of_profit,
        "days_to_expiry": days_to_expiry,
        "notes": notes,
        "contract_multiplier": contract_multiplier,
        "implied_volatility": implied_volatility,
    }

    if len(legs) >= 1:
        result["leg1"] = legs[0]
    if len(legs) >= 2:  # noqa: PLR2004
        result["leg2"] = legs[1]
    if len(legs) >= 3:  # noqa: PLR2004
        result["leg3"] = legs[2]
    if len(legs) >= 4:  # noqa: PLR2004
        result["leg4"] = legs[3]

    return result


def _leg(
    option_type: OptionType,
    strike: float,
    expiry: str,
    side: OptionSide,
    quantity: int,
    premium: float,
) -> OptionLeg:
    return {
        "option_type": option_type,
        "strike": strike,
        "expiry": expiry,
        "side": side,
        "quantity": quantity,
        "premium": premium,
    }


def _contracts_for_risk(
    risk_amount: float | None,
    max_loss: float,
    quantity: int | None,
    contract_multiplier: float,
) -> int:
    if quantity is not None:
        return max(1, int(quantity))
    if risk_amount is None or max_loss <= 0:
        return 1
    return max(1, int(risk_amount / (max_loss * contract_multiplier)))


def _expiry_date(days_to_expiry: int) -> str:
    return (now_utc() + timedelta(days=days_to_expiry)).strftime("%Y-%m-%d")


def _round_strike(strike: float, underlying: float) -> float:
    if underlying < 50:  # noqa: PLR2004
        increment = 1.0
    elif underlying < 200:  # noqa: PLR2004
        increment = 5.0
    else:
        increment = 10.0
    return round(strike / increment) * increment


def _estimate_call_price(
    spot: float,
    strike: float,
    iv: float,
    days: int,
    risk_free_rate: float = 0.05,
) -> float:
    t = days / 365.0
    if t <= 0:
        return max(0, spot - strike)

    d1 = (math.log(spot / strike) + (risk_free_rate + iv**2 / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)

    call_price = spot * _norm_cdf(d1) - strike * math.exp(-risk_free_rate * t) * _norm_cdf(d2)
    return max(0, call_price)


def _estimate_put_price(
    spot: float,
    strike: float,
    iv: float,
    days: int,
    risk_free_rate: float = 0.05,
) -> float:
    t = days / 365.0
    if t <= 0:
        return max(0, strike - spot)

    d1 = (math.log(spot / strike) + (risk_free_rate + iv**2 / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)

    put_price = strike * math.exp(-risk_free_rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return max(0, put_price)


def _norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _estimate_pop(current: float, breakeven: float, iv: float, days_to_expiry: int) -> float:
    if iv <= 0:
        return 0.5

    move_required = abs(breakeven - current) / current
    expected_move = iv * math.sqrt(days_to_expiry / 365.0)

    z_score = move_required / expected_move

    if breakeven > current:
        return 1 - _norm_cdf(z_score)
    return _norm_cdf(z_score)
