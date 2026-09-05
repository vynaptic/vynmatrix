"""Tests for quality-compounder panel derivation and validation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from USQualityCompounder.panel import panel_input_to_payload

from lib_application.db.models import Base, User
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
    persist_equity_observation,
)
from lib_application.services.strategy_panel_inputs import (
    StrategyPanelPayloadValidationRequest,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.cross_sectional import FactorObservation
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.equity_market_factors import EquityMarketFactorPolicy
from lib_strategy.panels import (
    EffectivePanelMember,
    OfficialSessionCutoff,
    PanelObservationRef,
    PanelReadyInput,
    SessionAuthority,
)
from market_data_ingestor.equity_factors import MarketCapitalizationEvidence
from market_data_ingestor.quality_compounder_panel import (
    DatabaseQualityCompounderPanelResolver,
    QualityCompounderPanelError,
    QualityCompounderPanelPayloadValidator,
    QualityCompounderPanelResolution,
    build_quality_compounder_panel_input,
    persist_quality_compounder_panel_manifest,
    quality_compounder_provider_authority_policy,
)

_CUTOFF = datetime(2026, 6, 30, 22, 0, tzinfo=UTC)


def test_provider_authority_is_owner_bound_and_default_deny() -> None:
    policy = quality_compounder_provider_authority_policy("owner-1")

    assert policy.data_use_scope is DataUseScope.PAPER_FORWARD
    assert policy.effective_entitlement_owner_user_id == "owner-1"
    policy.require_authorized(
        provider="sec",
        entitlement_scope="public-sec-edgar",
        entitlement_owner_user_id=None,
    )
    policy.require_authorized(
        provider="ice_nyse",
        entitlement_scope="public-official-exchange-publications",
        entitlement_owner_user_id=None,
    )
    with pytest.raises(ValueError, match="not declared"):
        policy.require_authorized(
            provider="other",
            entitlement_scope="public",
            entitlement_owner_user_id=None,
        )


def _session(session_date: date) -> OfficialSessionCutoff:
    opens = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC).replace(
        hour=13, minute=30
    )
    closes = opens.replace(hour=20, minute=0)
    return OfficialSessionCutoff(
        mic="XNYS",
        session_date=session_date,
        opens_at=opens,
        closes_at=closes,
        authority=SessionAuthority.OFFICIAL_EXCHANGE,
        source_identity=f"test:{session_date.isoformat()}",
        content_sha256=canonical_json_hash(
            {"opens_at": opens.isoformat(), "closes_at": closes.isoformat()}
        ),
    )


def _generic_panel(count: int = 25) -> PanelReadyInput:
    authority = ProviderAuthorityPolicy(
        policy_version="test-v1",
        data_use_scope=DataUseScope.PAPER_FORWARD,
        rules=(
            ProviderAuthorityRule(
                provider="eodhd",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("eodhd-personal-use-paper-only",),
                entitlement_owner_user_id="owner-1",
            ),
            ProviderAuthorityRule(
                provider="vynmatrix",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("vynmatrix-owner-derived-paper-only",),
                entitlement_owner_user_id="owner-1",
            ),
        ),
    )
    return PanelReadyInput(
        cutoff=_CUTOFF,
        session=_session(date(2026, 6, 30)),
        execution_session=_session(date(2026, 7, 1)),
        data_use_scope=DataUseScope.PAPER_FORWARD,
        provider_authority_policy=authority,
        provider_authority_sha256=authority.digest,
        membership_sha256="1" * 64,
        factor_snapshot_sha256="2" * 64,
        members=tuple(
            EffectivePanelMember(
                security_id=f"security-{index}",
                issuer_id=f"issuer-{index}",
                instrument_id=index + 1,
                canonical_symbol=f"S{index}",
            )
            for index in range(count)
        ),
        observations=tuple(
            PanelObservationRef(
                security_id=f"security-{index}",
                observation_id=f"{index + 100:064x}",
                observed_at=_CUTOFF.replace(hour=20),
                available_at=_CUTOFF.replace(hour=21),
                content_revision=1,
                content_sha256=f"{index + 200:064x}",
            )
            for index in range(count)
        ),
    )


def _inputs() -> tuple[PanelReadyInput, object, object, dict[str, MarketCapitalizationEvidence]]:
    panel = _generic_panel()
    instruments = []
    fundamentals = []
    market_caps: dict[str, MarketCapitalizationEvidence] = {}
    for index, member in enumerate(panel.members):
        group = index // 5
        security = SimpleNamespace(
            instrument_id=member.instrument_id,
            security_id=member.security_id,
            issuer_id=member.issuer_id,
            symbol=member.canonical_symbol,
            sector=f"G{group}",
            industry=f"G{group}",
            quote_currency="USD",
            tradable=True,
        )
        instruments.append(
            SimpleNamespace(
                security=security,
                reference_price=100.0,
                price_momentum=float(group),
                trend_return=1.0 if group >= 2 else -1.0,
                median_dollar_volume=75_000_000.0,
                expected_round_trip_cost_bps=20.0,
                worst_gap_return=-0.10,
                downside_volatility=0.30,
                corporate_action_clear=True,
                data_quality_passed=True,
                source_observation_ids=(f"{index + 300:064x}",),
            )
        )
        for factor in ("fundamental_growth", "quality", "valuation"):
            fundamentals.append(
                FactorObservation(
                    entity_id=member.canonical_symbol,
                    factor_name=factor,
                    raw_value=float(group + 1),
                    source_observation_ids=(f"{index + len(factor) + 400:064x}",),
                )
            )
        market_caps[member.canonical_symbol] = MarketCapitalizationEvidence(
            symbol=member.canonical_symbol,
            value=Decimal("12000000000"),
            observed_at=_CUTOFF.replace(hour=20),
            available_at=_CUTOFF.replace(hour=21),
            source_observation_id=f"{index + 500:064x}",
        )
    market = SimpleNamespace(
        cutoff=_CUTOFF,
        effective_session=date(2026, 6, 30),
        instruments=tuple(instruments),
        regime=SimpleNamespace(
            benchmark_trend_score=1.0,
            breadth_score=0.60,
            breadth_coverage_ratio=1.0,
            realized_volatility=0.20,
        ),
    )
    fundamental_panel = SimpleNamespace(
        cutoff=_CUTOFF,
        sleeve_observations=tuple(fundamentals),
    )
    return panel, market, fundamental_panel, market_caps


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def test_builder_derives_complete_strategy_fields_without_caller_scores() -> None:
    panel, market, fundamentals, market_caps = _inputs()
    candidate = build_quality_compounder_panel_input(
        panel=panel,
        market=market,  # type: ignore[arg-type]
        fundamentals=fundamentals,  # type: ignore[arg-type]
        market_cap_by_symbol=market_caps,
        factor_snapshot_id_by_security={
            member.security_id: f"{member.instrument_id + 600:064x}" for member in panel.members
        },
    )

    assert candidate.entries_allowed is True
    assert len(candidate.securities) == 25
    assert all(item.market_eligible for item in candidate.securities)
    assert candidate.securities[-1].sector_score > candidate.securities[0].sector_score
    assert len(candidate.factor_observations) == 25 * 4


def test_builder_fails_when_market_cap_is_not_cutoff_safe() -> None:
    panel, market, fundamentals, market_caps = _inputs()
    market_caps.pop("S0")

    with pytest.raises(QualityCompounderPanelError, match="market capitalization"):
        build_quality_compounder_panel_input(
            panel=panel,
            market=market,  # type: ignore[arg-type]
            fundamentals=fundamentals,  # type: ignore[arg-type]
            market_cap_by_symbol=market_caps,
            factor_snapshot_id_by_security={
                member.security_id: f"{member.instrument_id + 600:064x}" for member in panel.members
            },
        )


def test_validator_accepts_only_the_resolver_recomputed_payload() -> None:
    panel, market, fundamentals, market_caps = _inputs()
    candidate = build_quality_compounder_panel_input(
        panel=panel,
        market=market,  # type: ignore[arg-type]
        fundamentals=fundamentals,  # type: ignore[arg-type]
        market_cap_by_symbol=market_caps,
        factor_snapshot_id_by_security={
            member.security_id: f"{member.instrument_id + 600:064x}" for member in panel.members
        },
    )
    payload = panel_input_to_payload(candidate)

    class _Resolver:
        def resolve_quality_compounder_panel(
            self,
            *,
            request: StrategyPanelPayloadValidationRequest,
        ) -> QualityCompounderPanelResolution:
            del request
            return QualityCompounderPanelResolution(
                panel_input=candidate,
                authority_payload={"market_snapshot_sha256": "a" * 64},
            )

    validator = QualityCompounderPanelPayloadValidator(
        resolver_factory=lambda _session: _Resolver()
    )
    request = StrategyPanelPayloadValidationRequest(
        strategy_id="us_quality_compounder_v1",
        strategy_version="0.2.0",
        universe_code="SP500",
        panel=panel,
        panel_sha256=panel.canonical_digest(),
        strategy_input_payload=payload,
        strategy_input_sha256=canonical_json_hash(payload),
    )

    proof = validator.validate_strategy_panel_payload(None, request=request)  # type: ignore[arg-type]
    assert proof.validated_input_sha256 == request.strategy_input_sha256

    with pytest.raises(QualityCompounderPanelError, match="differs from recomputed"):
        validator.validate_strategy_panel_payload(
            None,  # type: ignore[arg-type]
            request=replace(
                request,
                strategy_input_payload={**payload, "entries_allowed": False},
            ),
        )


def test_database_resolver_reconciles_the_persisted_derived_manifest() -> None:
    panel, market, fundamentals, market_caps = _inputs()
    candidate = build_quality_compounder_panel_input(
        panel=panel,
        market=market,  # type: ignore[arg-type]
        fundamentals=fundamentals,  # type: ignore[arg-type]
        market_cap_by_symbol=market_caps,
        factor_snapshot_id_by_security={
            member.security_id: f"{member.instrument_id + 600:064x}" for member in panel.members
        },
    )
    with Session(_engine()) as session:
        session.add(
            User(
                user_id="owner-1",
                email="owner@example.com",
                base_ccy="EUR",
                status="active",
            )
        )
        session.flush()
        upstream = persist_equity_observation(
            session,
            EquityObservationSubmission(
                provider="eodhd",
                product="historical-eod",
                endpoint="/api/eod/S0.US",
                dataset_version="prospective-v1",
                tool_version="test-v1",
                source_identity="eodhd:eod:S0.US",
                source_revision="a" * 64,
                retrieved_at=_CUTOFF,
                timestamp_semantics={"available_at": "test retrieval"},
                adjustment_policy="raw",
                entitlement_scope="eodhd-personal-use-paper-only",
                entitlement_owner_user_id="owner-1",
                missing_data_policy="fail-closed",
                artifact_content_sha256="b" * 64,
                instrument_id=None,
                observation_kind="benchmark",
                source_record_identity="test-upstream",
                event_at=_CUTOFF.replace(hour=20),
                available_at=_CUTOFF,
                disposition="observed",
                normalized_content_sha256="c" * 64,
                values=(
                    EquityObservationValueInput(
                        field_name="close",
                        value_type="decimal",
                        value=Decimal("100"),
                    ),
                ),
            ),
        )
        persist_quality_compounder_panel_manifest(
            session,
            panel_input=candidate,
            strategy_version="0.2.0",
            entitlement_owner_user_id="owner-1",
            market_policy=EquityMarketFactorPolicy(
                round_trip_commission_bps=1.25,
                cost_context_sha256="f" * 64,
                required_adjustment_policy=("in-house-split-and-dividend-total-return-v1"),
            ),
            market_snapshot_sha256="d" * 64,
            fundamental_snapshot_sha256="e" * 64,
            source_observation_ids=(str(upstream.observation_id),),
        )
        payload = panel_input_to_payload(candidate)
        request = StrategyPanelPayloadValidationRequest(
            strategy_id="us_quality_compounder_v1",
            strategy_version="0.2.0",
            universe_code="SP500",
            panel=panel,
            panel_sha256=panel.canonical_digest(),
            strategy_input_payload=payload,
            strategy_input_sha256=canonical_json_hash(payload),
        )
        proof = QualityCompounderPanelPayloadValidator(
            resolver_factory=DatabaseQualityCompounderPanelResolver
        ).validate_strategy_panel_payload(session, request=request)

        assert proof.validated_input_sha256 == request.strategy_input_sha256
        source_authority = proof.authority_payload["evidence"]["source_observations"]
        assert source_authority[0]["observation_id"] == str(upstream.observation_id)
        assert len(source_authority[0]["semantic_sha256"]) == 64
