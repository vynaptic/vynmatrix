"""Frozen descriptive factor-risk model tests."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from lib_common.hashing import canonical_json_hash
from lib_strategy.equity_factor_risk import CANONICAL_STYLE_RISK_FACTORS
from lib_strategy.equity_factor_risk_model import (
    INTERNAL_FACTOR_RISK_MODEL,
    INTERNAL_FACTOR_RISK_MODEL_DEFINITION_SHA256,
    FactorRiskBenchmarkInput,
    FactorRiskBenchmarkObservation,
    FactorRiskFundamentalInput,
    FactorRiskModelError,
    FactorRiskModelInput,
    FactorRiskRawComponent,
    FactorRiskSourceReference,
    calculate_internal_factor_risk_panel,
    factor_risk_input_manifest_payload,
)
from lib_strategy.equity_market_factors import (
    DailyEquityMarketObservation,
    EquityMarketFactorInput,
    PointInTimeEquitySecurity,
)
from lib_strategy.panels import OfficialSessionCutoff, SessionAuthority

_START = date(2023, 1, 2)
_SESSIONS = 274


def _sha(value: object) -> str:
    return canonical_json_hash(value)


def _source(name: str, available_at: datetime) -> FactorRiskSourceReference:
    return FactorRiskSourceReference(
        observation_id=_sha({"observation": name}),
        authority_sha256=_sha({"authority": name}),
        available_at=available_at,
    )


def _security(index: int, *, sector: str) -> PointInTimeEquitySecurity:
    symbol = f"S{index:02d}"
    return PointInTimeEquitySecurity(
        instrument_id=index + 1,
        security_id=f"security-{index:02d}",
        issuer_id=f"issuer-{index:02d}",
        symbol=symbol,
        sector=sector,
        industry=f"industry-{sector}",
        quote_currency="USD",
        tradable=True,
        observation_id=_sha({"identity": symbol}),
        observation_sha256=_sha({"identity-authority": symbol}),
    )


def _benchmark() -> PointInTimeEquitySecurity:
    return PointInTimeEquitySecurity(
        instrument_id=999,
        security_id="benchmark-spy",
        issuer_id="benchmark-spy",
        symbol="SPY",
        sector="benchmark",
        industry="benchmark",
        quote_currency="USD",
        tradable=True,
        observation_id=_sha({"identity": "SPY"}),
        observation_sha256=_sha({"identity-authority": "SPY"}),
    )


def _market() -> EquityMarketFactorInput:
    members = tuple(
        _security(index, sector="sector-a" if index < 5 else "sector-b") for index in range(10)
    )
    benchmark = _benchmark()
    sessions: list[OfficialSessionCutoff] = []
    closes: list[datetime] = []
    for offset in range(_SESSIONS):
        session_date = _START + timedelta(days=offset)
        opens = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC) + timedelta(
            hours=14,
            minutes=30,
        )
        close = opens + timedelta(hours=6, minutes=30)
        closes.append(close)
        sessions.append(
            OfficialSessionCutoff(
                mic="XNYS",
                session_date=session_date,
                opens_at=opens,
                closes_at=close,
                authority=SessionAuthority.OFFICIAL_EXCHANGE,
                source_identity="test-official-session",
                content_sha256=_sha({"session": session_date.isoformat()}),
            )
        )
    spy_price = 100.0
    asset_prices = {item.instrument_id: 50.0 + item.instrument_id for item in members}
    prices: list[DailyEquityMarketObservation] = []
    for offset, close in enumerate(closes):
        spy_return = 0.0004 + 0.004 * math.sin(offset * 0.31)
        if offset:
            spy_price *= 1.0 + spy_return
        prices.append(_price(benchmark, close, spy_price, volume=5_000_000.0))
        for index, member in enumerate(members):
            if offset:
                residual = (0.0005 + index * 0.00007) * math.cos(offset * (0.13 + index * 0.003))
                asset_return = 0.0001 * (index - 4) + (0.65 + index * 0.08) * spy_return + residual
                asset_prices[member.instrument_id] *= 1.0 + asset_return
            prices.append(
                _price(
                    member,
                    close,
                    asset_prices[member.instrument_id],
                    volume=1_000_000.0 + index * 125_000.0 + offset * 10.0,
                )
            )
    return EquityMarketFactorInput(
        effective_session=sessions[-1].session_date,
        cutoff=sessions[-1].closes_at,
        official_sessions=tuple(sessions),
        members=members,
        benchmark=benchmark,
        prices=tuple(prices),
    )


def _price(
    security: PointInTimeEquitySecurity,
    close: datetime,
    price: float,
    *,
    volume: float,
) -> DailyEquityMarketObservation:
    identity = _sha({"symbol": security.symbol, "session": close.date().isoformat()})
    return DailyEquityMarketObservation(
        instrument_id=security.instrument_id,
        symbol=security.symbol,
        session_date=close.date(),
        observed_at=close,
        available_at=close,
        observation_id=identity,
        observation_sha256=_sha({"authority": identity}),
        provider="eodhd",
        timeframe="1d",
        entitlement_scope="personal-research",
        entitlement_owner_user_id="owner-1",
        total_return_close=price,
        split_adjusted_open=price * 0.999,
        split_adjusted_close=price,
        split_adjusted_volume=volume,
        split_adjustment_factor=1.0,
        raw_close=price,
        round_trip_spread_bps=2.0,
        one_way_nonspread_cost_bps=1.0,
        cost_context_sha256=_sha("cost-context"),
        corporate_action_clear=True,
    )


def _model_input() -> FactorRiskModelInput:
    market = _market()
    benchmark_prices = tuple(
        item for item in market.prices if item.instrument_id == market.benchmark.instrument_id
    )
    fundamentals: list[FactorRiskFundamentalInput] = []
    for index, security in enumerate(market.members):
        available = market.cutoff - timedelta(days=10)
        names = (
            "accrual_quality",
            "balance_sheet_safety",
            "cash_return_on_assets",
            "operating_profitability",
        )
        components = tuple(
            FactorRiskRawComponent(
                name=name,
                value=0.02 * (position + 1) + index * 0.003,
                sources=(_source(f"{security.symbol}:{name}", available),),
            )
            for position, name in enumerate(names)
        )
        cap_source = _source(f"{security.symbol}:market-cap", market.cutoff)
        book_source = _source(f"{security.symbol}:book", available)
        fundamentals.append(
            FactorRiskFundamentalInput(
                instrument_id=security.instrument_id,
                security_id=security.security_id,
                symbol=security.symbol,
                sector=security.sector,
                issuer_type="operating_company",
                market_cap_usd=Decimal(10_000_000_000 + index * 2_000_000_000),
                book_to_market=0.15 + index * 0.025,
                quality_components=components,
                market_cap_sources=(cap_source,),
                book_to_market_sources=tuple(
                    sorted((book_source, cap_source), key=lambda item: item.observation_id)
                ),
            )
        )
    return FactorRiskModelInput(
        market=market,
        benchmark=FactorRiskBenchmarkInput(
            instrument_id=market.benchmark.instrument_id,
            security_id=market.benchmark.security_id,
            symbol=market.benchmark.symbol,
            identity_source=FactorRiskSourceReference(
                observation_id=market.benchmark.observation_id,
                authority_sha256=market.benchmark.observation_sha256,
                available_at=market.cutoff - timedelta(days=30),
            ),
            observations=tuple(
                FactorRiskBenchmarkObservation(
                    session_date=item.session_date,
                    total_return_close=item.total_return_close,
                    observed_at=item.observed_at,
                    available_at=item.available_at,
                    source=FactorRiskSourceReference(
                        observation_id=item.observation_id,
                        authority_sha256=item.observation_sha256,
                        available_at=item.available_at,
                    ),
                )
                for item in benchmark_prices
            ),
        ),
        fundamentals=tuple(fundamentals),
        required_security_ids=tuple(item.security_id for item in market.members),
        security_identity_sources=tuple(
            sorted(
                (
                    FactorRiskSourceReference(
                        observation_id=item.observation_id,
                        authority_sha256=item.observation_sha256,
                        available_at=market.cutoff - timedelta(days=30),
                    )
                    for item in market.members
                ),
                key=lambda item: item.observation_id,
            )
        ),
        membership_sha256=_sha("membership"),
        provider_authority_sha256=_sha("authority-policy"),
        market_policy_sha256=_sha("market-policy"),
    )


def test_model_is_frozen_quantized_and_order_invariant() -> None:
    model_input = _model_input()
    first = calculate_internal_factor_risk_panel(model_input)
    reordered_market = replace(
        model_input.market,
        members=tuple(reversed(model_input.market.members)),
        prices=tuple(reversed(model_input.market.prices)),
    )
    second = calculate_internal_factor_risk_panel(
        replace(
            model_input,
            market=reordered_market,
            fundamentals=tuple(reversed(model_input.fundamentals)),
        )
    )

    assert first.model == INTERNAL_FACTOR_RISK_MODEL
    assert INTERNAL_FACTOR_RISK_MODEL_DEFINITION_SHA256 == (
        "503028cf42361aa8f8f495aa358fa7862e4f0071e37d4f1b41f02937b2501090"
    )
    assert first.model.model_definition_sha256 == INTERNAL_FACTOR_RISK_MODEL_DEFINITION_SHA256
    assert first.calculation_sha256 == second.calculation_sha256
    assert first.input_manifest_sha256 == second.input_manifest_sha256
    assert factor_risk_input_manifest_payload(first)["benchmark"]["symbol"] == "SPY"  # type: ignore[index]
    for exposure in first.exposures:
        assert tuple(name for name, _value in exposure.style_exposures) == (
            CANONICAL_STYLE_RISK_FACTORS
        )
        assert exposure.market_beta.as_tuple().exponent == -18
        assert all(value.as_tuple().exponent == -18 for _name, value in exposure.style_exposures)


def test_cap_weighted_styles_have_zero_mean_and_unit_rms() -> None:
    model_input = _model_input()
    result = calculate_internal_factor_risk_panel(model_input)
    caps = {item.security_id: float(item.market_cap_usd) for item in model_input.fundamentals}
    total = math.fsum(caps.values())
    for factor_name in CANONICAL_STYLE_RISK_FACTORS:
        values = {
            item.security_id: float(dict(item.style_exposures)[factor_name])
            for item in result.exposures
        }
        weighted_mean = math.fsum(caps[key] * values[key] for key in values) / total
        weighted_rms = math.sqrt(math.fsum(caps[key] * values[key] ** 2 for key in values) / total)
        assert weighted_mean == pytest.approx(0.0, abs=1e-14)
        assert weighted_rms == pytest.approx(1.0, abs=1e-14)


def test_model_rejects_incomplete_or_undersized_cross_sections() -> None:
    model_input = _model_input()
    missing = model_input.required_security_ids[-1]
    with pytest.raises(FactorRiskModelError, match="exactly equal"):
        calculate_internal_factor_risk_panel(
            replace(
                model_input,
                fundamentals=tuple(
                    item for item in model_input.fundamentals if item.security_id != missing
                ),
            )
        )

    moved = replace(model_input.fundamentals[0], sector="sector-c")
    changed_member = replace(model_input.market.members[0], sector="sector-c")
    with pytest.raises(FactorRiskModelError, match="fewer than five"):
        calculate_internal_factor_risk_panel(
            replace(
                model_input,
                market=replace(
                    model_input.market,
                    members=(changed_member, *model_input.market.members[1:]),
                ),
                fundamentals=(moved, *model_input.fundamentals[1:]),
            )
        )


def test_model_rejects_alpha_objects_and_non_spy_benchmark() -> None:
    with pytest.raises(FactorRiskModelError, match="FactorRiskModelInput"):
        calculate_internal_factor_risk_panel(object())  # type: ignore[arg-type]

    model_input = _model_input()
    with pytest.raises(FactorRiskModelError, match="SPY total return"):
        replace(model_input, benchmark=replace(model_input.benchmark, symbol="QQQ"))
