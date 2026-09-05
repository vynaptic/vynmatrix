"""Integration tests for raw fundamental-to-factor-risk adaptation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from lib_common.hashing import canonical_json_hash
from lib_strategy.equity_factor_risk_model import (
    INTERNAL_FACTOR_RISK_MODEL_VERSION,
    FactorRiskBenchmarkInput,
    FactorRiskBenchmarkObservation,
    FactorRiskSourceReference,
)
from lib_strategy.equity_market_factors import (
    DailyEquityMarketObservation,
    EquityMarketFactorInput,
    PointInTimeEquitySecurity,
)
from lib_strategy.panels import OfficialSessionCutoff, SessionAuthority
from market_data_ingestor.equity_factor_risk_inputs import (
    build_internal_factor_risk_input,
    fundamental_source_references,
)
from market_data_ingestor.equity_factors import (
    CanonicalFundamentalFact,
    CanonicalFundamentalMetric,
    FundamentalCalculationConfig,
    IssuerFundamentalEvidence,
    MarketCapitalizationEvidence,
    calculate_fundamental_panel,
)

_CUTOFF = datetime(2026, 6, 30, 20, 0, tzinfo=UTC)
_CURRENT_ACCEPTED = datetime(2026, 2, 4, 21, 0, tzinfo=UTC)
_PRIOR_ACCEPTED = datetime(2025, 2, 4, 21, 0, tzinfo=UTC)
_FLOW_METRICS = frozenset(
    {
        CanonicalFundamentalMetric.NET_INCOME,
        CanonicalFundamentalMetric.OPERATING_CASH_FLOW,
        CanonicalFundamentalMetric.OPERATING_INCOME,
        CanonicalFundamentalMetric.REVENUE,
    }
)


def _sha(value: object) -> str:
    return canonical_json_hash(value)


def _fact(
    symbol: str,
    metric: CanonicalFundamentalMetric,
    value: Decimal,
    *,
    period_end: date,
) -> CanonicalFundamentalFact:
    current = period_end.year == 2025
    accepted = _CURRENT_ACCEPTED if current else _PRIOR_ACCEPTED
    accepted_raw = "2026-02-04T16:00:00Z" if current else "2025-02-04T16:00:00Z"
    identity = _sha({"metric": metric.value, "period": period_end.isoformat(), "symbol": symbol})
    return CanonicalFundamentalFact(
        symbol=symbol,
        metric=metric,
        value=value,
        period_start=(date(period_end.year, 1, 1) if metric in _FLOW_METRICS else None),
        period_end=period_end,
        acceptance_time_raw=accepted_raw,
        acceptance_time=accepted,
        accession=f"0000000001-{str(accepted.year)[-2:]}-000001",
        historical_sic=7370,
        taxonomy="us-gaap",
        raw_tag=metric.value,
        mapping_priority=0,
        source_sha256=_sha({"source": identity}),
        availability_source_sha256=_sha({"availability": identity}),
        classification_source_sha256=_sha({"classification": symbol}),
        observation_id=identity,
    )


def _evidence(symbol: str, *, scale: Decimal) -> IssuerFundamentalEvidence:
    current = date(2025, 12, 31)
    prior = date(2024, 12, 31)
    values = {
        CanonicalFundamentalMetric.ASSETS: (Decimal("1100"), Decimal("1000")),
        CanonicalFundamentalMetric.EQUITY: (Decimal("550"), Decimal("500")),
        CanonicalFundamentalMetric.NET_INCOME: (Decimal("120"), Decimal("100")),
        CanonicalFundamentalMetric.OPERATING_CASH_FLOW: (
            Decimal("160"),
            Decimal("130"),
        ),
        CanonicalFundamentalMetric.OPERATING_INCOME: (Decimal("150"), Decimal("120")),
        CanonicalFundamentalMetric.REVENUE: (Decimal("900"), Decimal("800")),
    }
    facts = tuple(
        _fact(symbol, metric, value * scale, period_end=period)
        for metric, pair in values.items()
        for value, period in zip(pair, (current, prior), strict=True)
    )
    return IssuerFundamentalEvidence(
        symbol=symbol,
        peer_group="sic-major-73",
        cutoff=_CUTOFF,
        historical_sic=7370,
        classification_available_at=_CURRENT_ACCEPTED,
        classification_source_id=_sha({"classification-source": symbol}),
        facts=facts,
        market_cap=MarketCapitalizationEvidence(
            symbol=symbol,
            value=Decimal("5000") * scale,
            observed_at=_CUTOFF,
            available_at=_CUTOFF,
            source_observation_id=_sha({"market-cap": symbol}),
        ),
    )


def _security(symbol: str, instrument_id: int) -> PointInTimeEquitySecurity:
    return PointInTimeEquitySecurity(
        instrument_id=instrument_id,
        security_id=f"security:{symbol}",
        issuer_id=f"issuer:{symbol}",
        symbol=symbol,
        sector="services",
        industry="business-services",
        quote_currency="USD",
        tradable=True,
        observation_id=_sha({"identity": symbol}),
        observation_sha256=_sha({"identity-authority": symbol}),
    )


def _price(security: PointInTimeEquitySecurity) -> DailyEquityMarketObservation:
    observation_id = _sha({"price": security.symbol})
    return DailyEquityMarketObservation(
        instrument_id=security.instrument_id,
        symbol=security.symbol,
        session_date=_CUTOFF.date(),
        observed_at=_CUTOFF,
        available_at=_CUTOFF,
        observation_id=observation_id,
        observation_sha256=_sha({"price-authority": security.symbol}),
        provider="eodhd",
        timeframe="1d",
        entitlement_scope="personal-research",
        entitlement_owner_user_id="owner-1",
        total_return_close=100.0,
        split_adjusted_open=99.0,
        split_adjusted_close=100.0,
        split_adjusted_volume=1_000_000.0,
        split_adjustment_factor=1.0,
        raw_close=100.0,
        round_trip_spread_bps=2.0,
        one_way_nonspread_cost_bps=1.0,
        cost_context_sha256=_sha("cost-context"),
        corporate_action_clear=True,
    )


def test_v2_calculator_snapshot_adapts_to_current_factor_risk_model() -> None:
    evidence = (_evidence("TEST", scale=Decimal(1)), _evidence("PEER", scale=Decimal("1.1")))
    panel = calculate_fundamental_panel(
        evidence,
        FundamentalCalculationConfig(max_fundamental_age_days=800, minimum_peer_count=2),
    )
    member = _security("TEST", 1)
    benchmark = _security("SPY", 999)
    official_session = OfficialSessionCutoff(
        mic="XNYS",
        session_date=_CUTOFF.date(),
        opens_at=_CUTOFF.replace(hour=13, minute=30),
        closes_at=_CUTOFF,
        authority=SessionAuthority.OFFICIAL_EXCHANGE,
        source_identity="test-official-session",
        content_sha256=_sha("official-session"),
    )
    market = EquityMarketFactorInput(
        effective_session=_CUTOFF.date(),
        cutoff=_CUTOFF,
        official_sessions=(official_session,),
        members=(member,),
        benchmark=benchmark,
        prices=(_price(member),),
    )
    observation_ids = (
        {item.classification_source_id for item in evidence}
        | {
            item.market_cap.source_observation_id
            for item in evidence
            if item.market_cap is not None
        }
        | {fact.observation_id for item in evidence for fact in item.facts}
    )
    sources = fundamental_source_references(
        evidence,
        authority_sha256_by_observation_id={
            observation_id: _sha({"authority": observation_id})
            for observation_id in observation_ids
        },
    )
    sources[member.observation_id] = FactorRiskSourceReference(
        observation_id=member.observation_id,
        authority_sha256=member.observation_sha256,
        available_at=_CUTOFF,
    )
    benchmark_source = FactorRiskSourceReference(
        observation_id=benchmark.observation_id,
        authority_sha256=benchmark.observation_sha256,
        available_at=_CUTOFF,
    )
    benchmark_price = _price(benchmark)
    adapted = build_internal_factor_risk_input(
        market=market,
        benchmark=FactorRiskBenchmarkInput(
            instrument_id=benchmark.instrument_id,
            security_id=benchmark.security_id,
            symbol=benchmark.symbol,
            identity_source=benchmark_source,
            observations=(
                FactorRiskBenchmarkObservation(
                    session_date=benchmark_price.session_date,
                    total_return_close=benchmark_price.total_return_close,
                    observed_at=benchmark_price.observed_at,
                    available_at=benchmark_price.available_at,
                    source=FactorRiskSourceReference(
                        observation_id=benchmark_price.observation_id,
                        authority_sha256=benchmark_price.observation_sha256,
                        available_at=benchmark_price.available_at,
                    ),
                ),
            ),
        ),
        fundamental_panel=panel,
        fundamental_evidence=evidence,
        required_security_ids=(member.security_id,),
        source_references=sources,
        membership_sha256=_sha("membership"),
        provider_authority_sha256=_sha("provider-authority"),
        market_policy_sha256=_sha("market-policy"),
    )

    assert INTERNAL_FACTOR_RISK_MODEL_VERSION == "2.0.0"
    assert tuple(item.name for item in adapted.fundamentals[0].quality_components) == (
        "accrual_quality",
        "balance_sheet_safety",
        "cash_return_on_assets",
        "operating_profitability",
    )
