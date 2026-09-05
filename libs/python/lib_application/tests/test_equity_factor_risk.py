"""DB-backed point-in-time portfolio factor-risk evidence tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    EquityObservation,
    EquityObservationValue,
    EquitySourceLineage,
    Instrument,
)
from lib_application.services.equity_factor_risk import (
    FACTOR_RISK_ADJUSTMENT_POLICY,
    FACTOR_RISK_TIMESTAMP_SEMANTICS,
    EquityFactorRiskEvidenceError,
    load_equity_factor_risk_panel,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.equity_factor_risk import (
    CANONICAL_STYLE_RISK_FACTORS,
    FACTOR_RISK_EXPOSURE_CONTRACT,
    FACTOR_RISK_OBSERVATION_KIND,
    FactorRiskModelIdentity,
    PortfolioFactorRiskPolicy,
    StyleRiskCap,
)
from lib_strategy.panels import EffectivePanelMember

_CUTOFF = datetime(2025, 1, 31, 21, tzinfo=UTC)
_MODEL = FactorRiskModelIdentity(
    provider="licensed-risk",
    model_id="us-equity-style-model",
    model_version="2025.01.15",
    model_definition_sha256="a" * 64,
)


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _policy() -> PortfolioFactorRiskPolicy:
    return PortfolioFactorRiskPolicy(
        policy_version="portfolio-risk-test-v1",
        model=_MODEL,
        maximum_age_days=10,
        maximum_market_beta=1.15,
        style_caps=tuple(
            StyleRiskCap(factor_name=name, maximum_absolute_exposure=1.5)
            for name in CANONICAL_STYLE_RISK_FACTORS
        ),
    )


def _authority() -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="portfolio-risk-authority-v1",
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        rules=(
            ProviderAuthorityRule(
                provider="licensed-risk",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("licensed-pit-research",),
            ),
        ),
    )


def _member(instrument_id: int, symbol: str) -> EffectivePanelMember:
    return EffectivePanelMember(
        security_id=f"security-{symbol.lower()}",
        issuer_id=f"issuer-{symbol.lower()}",
        instrument_id=instrument_id,
        canonical_symbol=symbol,
    )


def _value(
    *,
    observation_id: str,
    field_name: str,
    value: str | Decimal | datetime | int,
    unit: str | None = None,
    context_identity: str | None = None,
) -> EquityObservationValue:
    kwargs: dict[str, object] = {
        "value_id": canonical_json_hash(
            {"field_name": field_name, "observation_id": observation_id}
        ),
        "observation_id": observation_id,
        "field_name": field_name,
        "ordinal": 0,
        "unit": unit,
        "context_identity": context_identity,
    }
    if isinstance(value, Decimal):
        kwargs.update(value_type="decimal", decimal_value=value)
    elif isinstance(value, datetime):
        kwargs.update(value_type="timestamp", timestamp_value=value)
    elif isinstance(value, int):
        kwargs.update(value_type="integer", integer_value=value)
    else:
        kwargs.update(value_type="text", text_value=value)
    return EquityObservationValue(**kwargs)


def _seed_exposure(
    session: Session,
    *,
    member: EffectivePanelMember,
    identity: str,
    source_record_identity: str | None = None,
    available_at: datetime = _CUTOFF - timedelta(hours=1),
    timestamp_semantics: dict[str, str] | None = None,
) -> None:
    observation_id = canonical_json_hash({"observation": identity})
    lineage_id = canonical_json_hash({"lineage": identity})
    input_manifest_sha256 = canonical_json_hash({"input-manifest": identity})
    calculation_sha256 = canonical_json_hash({"calculation": identity})
    event_at = _CUTOFF - timedelta(hours=2)
    session.add(
        EquitySourceLineage(
            lineage_id=lineage_id,
            provider=_MODEL.provider,
            product="us-equity-style-risk",
            endpoint="normalized://portfolio-factor-risk",
            dataset_version="2025.01",
            tool_version="normalized-risk-adapter-v1",
            source_identity=f"risk-model/{identity}",
            source_revision=calculation_sha256,
            retrieved_at=_CUTOFF + timedelta(days=30),
            timestamp_semantics=(
                FACTOR_RISK_TIMESTAMP_SEMANTICS
                if timestamp_semantics is None
                else timestamp_semantics
            ),
            adjustment_policy=FACTOR_RISK_ADJUSTMENT_POLICY,
            entitlement_scope="licensed-pit-research",
            missing_data_policy="fail-closed",
            content_sha256=input_manifest_sha256,
        )
    )
    session.flush()
    session.add(
        EquityObservation(
            observation_id=observation_id,
            lineage_id=lineage_id,
            instr_id=member.instrument_id,
            observation_kind=FACTOR_RISK_OBSERVATION_KIND,
            source_record_identity=(source_record_identity or f"risk/{member.security_id}"),
            event_at=event_at,
            available_at=available_at,
            revision=1,
            disposition="observed",
            content_sha256=canonical_json_hash({"normalized": identity}),
        )
    )
    text_values = {
        "factor_risk_contract": FACTOR_RISK_EXPOSURE_CONTRACT,
        "security_id": member.security_id,
        "symbol": member.canonical_symbol,
        "model_provider": _MODEL.provider,
        "model_id": _MODEL.model_id,
        "model_version": _MODEL.model_version,
        "model_definition_sha256": _MODEL.model_definition_sha256,
        "benchmark_security_id": "security-spy",
        "input_manifest_sha256": input_manifest_sha256,
        "market_input_sha256": canonical_json_hash({"market-input": identity}),
        "fundamental_input_sha256": canonical_json_hash({"fundamental-input": identity}),
        "membership_sha256": canonical_json_hash({"membership": identity}),
        "provider_authority_sha256": canonical_json_hash({"provider-authority": identity}),
        "source_observation_set_sha256": canonical_json_hash({"source-observations": identity}),
        "calculation_sha256": calculation_sha256,
    }
    values = [
        _value(observation_id=observation_id, field_name=name, value=value)
        for name, value in text_values.items()
    ]
    values.extend(
        (
            _value(
                observation_id=observation_id,
                field_name="exposure_observed_at",
                value=event_at,
            ),
            _value(
                observation_id=observation_id,
                field_name="source_available_at",
                value=available_at,
            ),
            _value(
                observation_id=observation_id,
                field_name="market_beta",
                value=Decimal("1.05"),
                unit="beta",
                context_identity=_MODEL.model_definition_sha256,
            ),
            _value(
                observation_id=observation_id,
                field_name="source_observation_count",
                value=3,
            ),
        )
    )
    values.extend(
        _value(
            observation_id=observation_id,
            field_name=f"raw_descriptor_{name}",
            value=Decimal("0.50"),
            unit="raw_descriptor",
            context_identity=_MODEL.model_definition_sha256,
        )
        for name in CANONICAL_STYLE_RISK_FACTORS
    )
    values.extend(
        _value(
            observation_id=observation_id,
            field_name=f"style_exposure_{name}",
            value=Decimal("0.25"),
            unit="standard_deviation",
            context_identity=_MODEL.model_definition_sha256,
        )
        for name in CANONICAL_STYLE_RISK_FACTORS
    )
    session.add_all(values)
    session.commit()


def _seed_instruments(session: Session, members: tuple[EffectivePanelMember, ...]) -> None:
    session.add_all(
        Instrument(
            instr_id=member.instrument_id,
            asset_class="equity",
            canonical=member.canonical_symbol,
            settlement_currency="USD",
            market_session_policy="scheduled",
        )
        for member in members
    )
    session.commit()


def test_loader_returns_exact_model_partial_coverage_with_full_lineage() -> None:
    engine = _engine()
    members = (_member(1, "AAA"), _member(2, "BBB"))
    with Session(engine) as session:
        _seed_instruments(session, members)
        _seed_exposure(session, member=members[0], identity="aaa")

        panel = load_equity_factor_risk_panel(
            session,
            members=members,
            cutoff=_CUTOFF,
            policy=_policy(),
            provider_authority_policy=_authority(),
        )

    assert panel is not None
    assert panel.model == _MODEL
    assert tuple(item.security_id for item in panel.exposures) == ("security-aaa",)
    assert panel.exposures[0].dataset_version == "2025.01"
    assert panel.exposures[0].market_beta == 1.05
    assert len(panel.exposures[0].observation_authority_sha256) == 64


def test_loader_rejects_ambiguous_latest_source_and_timestamp_semantic_drift() -> None:
    engine = _engine()
    member = _member(1, "AAA")
    with Session(engine) as session:
        _seed_instruments(session, (member,))
        _seed_exposure(
            session,
            member=member,
            identity="first",
            source_record_identity="risk/security-aaa/first",
        )
        _seed_exposure(
            session,
            member=member,
            identity="second",
            source_record_identity="risk/security-aaa/second",
        )

        with pytest.raises(EquityFactorRiskEvidenceError, match="ambiguous"):
            load_equity_factor_risk_panel(
                session,
                members=(member,),
                cutoff=_CUTOFF,
                policy=_policy(),
                provider_authority_policy=_authority(),
            )

    engine = _engine()
    with Session(engine) as session:
        _seed_instruments(session, (member,))
        _seed_exposure(
            session,
            member=member,
            identity="bad-semantics",
            timestamp_semantics={"schema": "unregistered"},
        )
        with pytest.raises(EquityFactorRiskEvidenceError, match="timestamp semantics"):
            load_equity_factor_risk_panel(
                session,
                members=(member,),
                cutoff=_CUTOFF,
                policy=_policy(),
                provider_authority_policy=_authority(),
            )
