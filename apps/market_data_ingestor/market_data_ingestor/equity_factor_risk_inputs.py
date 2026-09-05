"""Shared raw-fundamental adapter for the internal factor-risk model."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import NoReturn

from lib_strategy.equity_factor_risk_model import (
    FactorRiskBenchmarkInput,
    FactorRiskBenchmarkObservation,
    FactorRiskFundamentalInput,
    FactorRiskModelInput,
    FactorRiskRawComponent,
    FactorRiskSourceReference,
    factor_risk_quality_component_names,
)
from lib_strategy.equity_market_factors import EquityMarketFactorInput

from .equity_factors import (
    FundamentalPanelSnapshot,
    IssuerFundamentalEvidence,
)


class FactorRiskInputAdapterError(ValueError):
    """Raw SEC component evidence cannot form the frozen risk-model input."""


def _invalid(message: str) -> NoReturn:
    raise FactorRiskInputAdapterError(message)


def build_internal_factor_risk_input(
    *,
    market: EquityMarketFactorInput,
    benchmark: FactorRiskBenchmarkInput | None = None,
    fundamental_panel: FundamentalPanelSnapshot,
    fundamental_evidence: Sequence[IssuerFundamentalEvidence],
    required_security_ids: Sequence[str],
    source_references: Mapping[str, FactorRiskSourceReference],
    membership_sha256: str,
    provider_authority_sha256: str,
    market_policy_sha256: str,
) -> FactorRiskModelInput:
    """Adapt raw component ledgers without consuming normalized alpha values."""

    security_by_id = {item.security_id: item for item in market.members}
    calculation_by_symbol = {item.symbol: item for item in fundamental_panel.calculations}
    evidence_by_symbol = {item.symbol: item for item in fundamental_evidence}
    required = tuple(sorted(set(required_security_ids)))
    if len(required) != len(tuple(required_security_ids)):
        _invalid("factor-risk required security identities are duplicated")
    adapted: list[FactorRiskFundamentalInput] = []
    for security_id in required:
        security = security_by_id.get(security_id)
        if security is None:
            _invalid("factor-risk required security is absent from market input")
        calculation = calculation_by_symbol.get(security.symbol)
        evidence = evidence_by_symbol.get(security.symbol)
        if calculation is None or evidence is None:
            _invalid(f"{security.symbol} lacks a raw fundamental component ledger")
        if evidence.market_cap is None:
            _invalid(f"{security.symbol} lacks cutoff-safe persisted market capitalization")
        components = {item.name: item for item in calculation.components}
        quality_names = factor_risk_quality_component_names(calculation.issuer_type.value)
        quality: list[FactorRiskRawComponent] = []
        for name in quality_names:
            component = components.get(name)
            if component is None or component.value is None:
                _invalid(f"{security.symbol} quality component {name!r} is unavailable")
            quality.append(
                FactorRiskRawComponent(
                    name=name,
                    value=component.value,
                    sources=_resolve_sources(
                        component.source_observation_ids,
                        source_references=source_references,
                        field_name=f"{security.symbol} {name}",
                    ),
                )
            )
        book_to_market = components.get("book_to_market")
        if book_to_market is None or book_to_market.value is None:
            _invalid(f"{security.symbol} raw book-to-market component is unavailable")
        market_cap_sources = _resolve_sources(
            (evidence.market_cap.source_observation_id,),
            source_references=source_references,
            field_name=f"{security.symbol} market cap",
        )
        adapted.append(
            FactorRiskFundamentalInput(
                instrument_id=security.instrument_id,
                security_id=security.security_id,
                symbol=security.symbol,
                sector=security.sector,
                issuer_type=calculation.issuer_type.value,
                market_cap_usd=evidence.market_cap.value,
                book_to_market=book_to_market.value,
                quality_components=tuple(sorted(quality, key=lambda item: item.name)),
                market_cap_sources=market_cap_sources,
                book_to_market_sources=_resolve_sources(
                    book_to_market.source_observation_ids,
                    source_references=source_references,
                    field_name=f"{security.symbol} book to market",
                ),
            )
        )
    benchmark_input = benchmark or FactorRiskBenchmarkInput(
        instrument_id=market.benchmark.instrument_id,
        security_id=market.benchmark.security_id,
        symbol=market.benchmark.symbol,
        identity_source=_resolve_sources(
            (market.benchmark.observation_id,),
            source_references=source_references,
            field_name="factor-risk SPY identity",
        )[0],
        observations=tuple(
            FactorRiskBenchmarkObservation(
                session_date=item.session_date,
                total_return_close=item.total_return_close,
                observed_at=item.observed_at,
                available_at=item.available_at,
                source=source_references[item.observation_id],
            )
            for item in sorted(
                (
                    value
                    for value in market.prices
                    if value.instrument_id == market.benchmark.instrument_id
                ),
                key=lambda value: value.session_date,
            )
        ),
    )
    return FactorRiskModelInput(
        market=market,
        benchmark=benchmark_input,
        fundamentals=tuple(adapted),
        required_security_ids=required,
        security_identity_sources=_resolve_sources(
            tuple(
                sorted(
                    {
                        *(security_by_id[item].observation_id for item in required),
                    }
                )
            ),
            source_references=source_references,
            field_name="factor-risk security identities",
        ),
        membership_sha256=membership_sha256,
        provider_authority_sha256=provider_authority_sha256,
        market_policy_sha256=market_policy_sha256,
    )


def fundamental_source_references(
    evidence: Sequence[IssuerFundamentalEvidence],
    *,
    authority_sha256_by_observation_id: Mapping[str, str],
) -> dict[str, FactorRiskSourceReference]:
    """Bind raw fundamental source IDs to exact authority and availability."""

    result: dict[str, FactorRiskSourceReference] = {}
    for issuer in evidence:
        _register_source(
            result,
            observation_id=issuer.classification_source_id,
            authority_sha256_by_observation_id=authority_sha256_by_observation_id,
            available_at=issuer.classification_available_at,
        )
        for fact in issuer.facts:
            _register_source(
                result,
                observation_id=fact.observation_id,
                authority_sha256_by_observation_id=authority_sha256_by_observation_id,
                available_at=fact.acceptance_time,
            )
        if issuer.market_cap is not None:
            _register_source(
                result,
                observation_id=issuer.market_cap.source_observation_id,
                authority_sha256_by_observation_id=authority_sha256_by_observation_id,
                available_at=issuer.market_cap.available_at,
            )
    return result


def _register_source(
    result: dict[str, FactorRiskSourceReference],
    *,
    observation_id: str,
    authority_sha256_by_observation_id: Mapping[str, str],
    available_at: datetime,
) -> None:
    authority_sha256 = authority_sha256_by_observation_id.get(observation_id)
    if authority_sha256 is None:
        _invalid(f"factor-risk source {observation_id!r} lacks an authority digest")
    source = FactorRiskSourceReference(
        observation_id=observation_id,
        authority_sha256=authority_sha256,
        available_at=available_at,
    )
    previous = result.setdefault(observation_id, source)
    if previous != source:
        _invalid("one factor-risk source has divergent authority or availability")


def _resolve_sources(
    observation_ids: Sequence[str],
    *,
    source_references: Mapping[str, FactorRiskSourceReference],
    field_name: str,
) -> tuple[FactorRiskSourceReference, ...]:
    if not observation_ids:
        _invalid(f"{field_name} has no raw source observations")
    try:
        values = tuple(source_references[item] for item in sorted(set(observation_ids)))
    except KeyError as exc:
        message = f"{field_name} source {exc.args[0]!r} is unavailable"
        raise FactorRiskInputAdapterError(message) from exc
    return values


__all__ = [
    "FactorRiskInputAdapterError",
    "build_internal_factor_risk_input",
    "fundamental_source_references",
]
