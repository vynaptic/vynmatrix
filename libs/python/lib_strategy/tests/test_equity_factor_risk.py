"""Separate point-in-time portfolio factor-risk contract tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from lib_common.hashing import canonical_json_hash
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.equity_factor_risk import (
    CANONICAL_STYLE_RISK_FACTORS,
    EquityFactorRiskExposure,
    EquityFactorRiskPanel,
    FactorRiskModelIdentity,
    PortfolioFactorRiskError,
    PortfolioFactorRiskPolicy,
    StyleRiskCap,
    StyleRiskExposure,
    equity_factor_risk_panel_from_payload,
    prepare_portfolio_factor_risk_context,
)

_CUTOFF = datetime(2025, 1, 31, 21, tzinfo=UTC)
_MODEL = FactorRiskModelIdentity(
    provider="licensed-risk",
    model_id="us-equity-style-model",
    model_version="2025.01.15",
    model_definition_sha256="a" * 64,
)
_PANEL_IDENTITIES = {
    name: canonical_json_hash({"panel": name})
    for name in (
        "calculation",
        "fundamental-input",
        "input-manifest",
        "market-input",
        "membership",
        "provider-authority",
    )
}


def _policy(*, model: FactorRiskModelIdentity | None = _MODEL) -> PortfolioFactorRiskPolicy:
    return PortfolioFactorRiskPolicy(
        policy_version="portfolio-risk-test-v1",
        model=model,
        maximum_age_days=10,
        maximum_market_beta=0.75,
        style_caps=tuple(
            StyleRiskCap(factor_name=name, maximum_absolute_exposure=1.0)
            for name in CANONICAL_STYLE_RISK_FACTORS
        ),
    )


def _authority(scope: DataUseScope) -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="portfolio-risk-authority-v1",
        data_use_scope=scope,
        rules=(
            ProviderAuthorityRule(
                provider="licensed-risk",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("licensed-pit-research",),
            ),
        ),
    )


def _exposure(
    security_id: str,
    symbol: str,
    instrument_id: int,
    *,
    market_beta: float,
    momentum: float = 0.0,
    observed_at: datetime = _CUTOFF - timedelta(days=1),
    model: FactorRiskModelIdentity = _MODEL,
) -> EquityFactorRiskExposure:
    digest = canonical_json_hash({"security_id": security_id})
    return EquityFactorRiskExposure(
        instrument_id=instrument_id,
        security_id=security_id,
        symbol=symbol,
        model=model,
        benchmark_security_id="security-spy",
        input_manifest_sha256=_PANEL_IDENTITIES["input-manifest"],
        market_input_sha256=_PANEL_IDENTITIES["market-input"],
        fundamental_input_sha256=_PANEL_IDENTITIES["fundamental-input"],
        membership_sha256=_PANEL_IDENTITIES["membership"],
        provider_authority_sha256=_PANEL_IDENTITIES["provider-authority"],
        source_observation_set_sha256=canonical_json_hash({"risk-sources": security_id}),
        source_observation_count=3,
        calculation_sha256=_PANEL_IDENTITIES["calculation"],
        observed_at=observed_at,
        available_at=observed_at + timedelta(minutes=1),
        retrieved_at=observed_at + timedelta(minutes=2),
        revision=1,
        observation_id=digest,
        observation_content_sha256=canonical_json_hash({"content": security_id}),
        observation_authority_sha256=canonical_json_hash({"authority": security_id}),
        lineage_id=canonical_json_hash({"lineage": security_id}),
        source_content_sha256=canonical_json_hash({"source": security_id}),
        source_product="us-equity-style-risk",
        dataset_version="2025.01",
        tool_version="normalized-risk-adapter-v1",
        source_revision=f"source-{security_id}-v1",
        timestamp_semantics_sha256=canonical_json_hash(
            {"schema": "pit-equity-factor-risk-timestamp-semantics-v1"}
        ),
        adjustment_policy="not-applicable-factor-risk-exposure-v1",
        missing_data_policy="fail-closed",
        entitlement_scope="licensed-pit-research",
        entitlement_owner_user_id=None,
        market_beta=market_beta,
        raw_descriptors=tuple(
            (name, momentum if name == "momentum" else 0.0) for name in CANONICAL_STYLE_RISK_FACTORS
        ),
        style_exposures=tuple(
            StyleRiskExposure(
                factor_name=name,
                standardized_exposure=(momentum if name == "momentum" else 0.0),
            )
            for name in CANONICAL_STYLE_RISK_FACTORS
        ),
    )


def _panel() -> EquityFactorRiskPanel:
    return EquityFactorRiskPanel(
        cutoff=_CUTOFF,
        model=_MODEL,
        exposures=(
            _exposure("security-a", "AAA", 1, market_beta=1.0, momentum=0.5),
            _exposure("security-b", "BBB", 2, market_beta=2.0, momentum=2.0),
        ),
    )


def test_historical_unconfigured_policy_is_explicitly_inactive() -> None:
    context = prepare_portfolio_factor_risk_context(
        policy=_policy(model=None),
        panel=None,
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        cutoff=_CUTOFF,
        provider_authority_policy=_authority(DataUseScope.HISTORICAL_VALIDATION),
    )

    audit = context.audit(
        selected_security_ids=("security-a",),
        slot_weight=0.5,
        rejected_candidates=(),
    )

    assert audit.cap_active is False
    assert audit.panel_sha256 is None
    assert audit.selected_security_ids == ("security-a",)


@pytest.mark.parametrize("scope", [DataUseScope.PAPER_FORWARD, DataUseScope.LIVE_FORWARD])
def test_forward_scopes_reject_an_unconfigured_risk_model(scope: DataUseScope) -> None:
    with pytest.raises(PortfolioFactorRiskError, match="requires a configured"):
        prepare_portfolio_factor_risk_context(
            policy=_policy(model=None),
            panel=None,
            data_use_scope=scope,
            cutoff=_CUTOFF,
            provider_authority_policy=_authority(scope),
        )


def test_active_cap_arithmetic_and_missing_selected_coverage_fail_closed() -> None:
    context = prepare_portfolio_factor_risk_context(
        policy=_policy(),
        panel=_panel(),
        data_use_scope=DataUseScope.PAPER_FORWARD,
        cutoff=_CUTOFF,
        provider_authority_policy=_authority(DataUseScope.PAPER_FORWARD),
    )

    assert (
        context.candidate_failure(
            selected_security_ids=(),
            candidate_security_id="security-a",
            slot_weight=0.5,
        )
        is None
    )
    assert (
        context.candidate_failure(
            selected_security_ids=("security-a",),
            candidate_security_id="security-b",
            slot_weight=0.5,
        )
        == "market_beta_factor_risk_limit"
    )
    style_policy = PortfolioFactorRiskPolicy(
        policy_version="portfolio-style-risk-test-v1",
        model=_MODEL,
        maximum_age_days=10,
        maximum_market_beta=10.0,
        style_caps=_policy().style_caps,
    )
    style_context = prepare_portfolio_factor_risk_context(
        policy=style_policy,
        panel=_panel(),
        data_use_scope=DataUseScope.PAPER_FORWARD,
        cutoff=_CUTOFF,
        provider_authority_policy=_authority(DataUseScope.PAPER_FORWARD),
    )
    assert (
        style_context.candidate_failure(
            selected_security_ids=("security-a",),
            candidate_security_id="security-b",
            slot_weight=0.5,
        )
        == "style_factor_risk_limit:momentum"
    )
    with pytest.raises(PortfolioFactorRiskError, match="lacks factor-risk evidence"):
        context.audit(
            selected_security_ids=("security-c",),
            slot_weight=0.5,
            rejected_candidates=(),
        )


def test_panel_codec_is_order_invariant_and_preserves_full_lineage() -> None:
    expected = _panel()
    payload = expected.identity_payload()
    payload["exposures"] = list(reversed(payload["exposures"]))

    restored = equity_factor_risk_panel_from_payload(payload)

    assert restored is not None
    assert restored.content_sha256 == expected.content_sha256
    assert restored.exposures[0].dataset_version == "2025.01"
    assert restored.exposures[0].timestamp_semantics_sha256


def test_future_or_wrong_model_evidence_is_rejected() -> None:
    with pytest.raises(PortfolioFactorRiskError, match="future-dated"):
        EquityFactorRiskPanel(
            cutoff=_CUTOFF,
            model=_MODEL,
            exposures=(
                _exposure(
                    "security-a",
                    "AAA",
                    1,
                    market_beta=1.0,
                    observed_at=_CUTOFF + timedelta(minutes=1),
                ),
            ),
        )

    late_retrieval_panel = EquityFactorRiskPanel(
        cutoff=_CUTOFF,
        model=_MODEL,
        exposures=(
            replace(
                _exposure("security-a", "AAA", 1, market_beta=1.0),
                retrieved_at=_CUTOFF + timedelta(minutes=1),
            ),
        ),
    )
    with pytest.raises(PortfolioFactorRiskError, match="retrieved after"):
        prepare_portfolio_factor_risk_context(
            policy=_policy(),
            panel=late_retrieval_panel,
            data_use_scope=DataUseScope.PAPER_FORWARD,
            cutoff=_CUTOFF,
            provider_authority_policy=_authority(DataUseScope.PAPER_FORWARD),
        )

    wrong_model = FactorRiskModelIdentity(
        provider="licensed-risk",
        model_id="different-model",
        model_version="2025.01.15",
        model_definition_sha256="b" * 64,
    )
    wrong_panel = EquityFactorRiskPanel(
        cutoff=_CUTOFF,
        model=wrong_model,
        exposures=(
            _exposure(
                "security-a",
                "AAA",
                1,
                market_beta=1.0,
                model=wrong_model,
            ),
        ),
    )
    with pytest.raises(PortfolioFactorRiskError, match="differs from the frozen policy"):
        prepare_portfolio_factor_risk_context(
            policy=_policy(),
            panel=wrong_panel,
            data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
            cutoff=_CUTOFF,
            provider_authority_policy=_authority(DataUseScope.HISTORICAL_VALIDATION),
        )
