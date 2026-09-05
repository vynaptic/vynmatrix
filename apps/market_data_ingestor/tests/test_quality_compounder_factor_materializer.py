"""Tests for the quality-compounder DB fundamental adapter."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from lib_application.db.models import Base, Instrument
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
    persist_equity_observation,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.equity_market_factors import EquityMarketFactorPolicy, PointInTimeEquitySecurity
from lib_strategy.equity_quality_compounder import (
    QualityCompounderEvidencePolicy,
    quality_compounder_configuration_sha256,
)
from lib_strategy.panels import (
    EffectivePanelMember,
    OfficialSessionCutoff,
    PanelObservationRef,
    PanelReadyInput,
    SessionAuthority,
)
from market_data_ingestor import equity_factors
from market_data_ingestor.equity_factors import (
    CanonicalFundamentalMetric,
    FundamentalCalculationConfig,
    FundamentalSleeve,
    IssuerType,
)
from market_data_ingestor.quality_compounder_factor_materializer import (
    QualityCompounderDatabaseFactorResolver,
    QualityCompounderFactorMaterializationError,
    _partition_listing_warmups,
)

_CUTOFF = datetime(2026, 6, 30, 22, 0, tzinfo=UTC)
_ACCEPTED = datetime(2026, 2, 4, 21, 0, tzinfo=UTC)
_CIK = "0000000001"


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _authority() -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="quality-factor-test-v1",
        data_use_scope=DataUseScope.PAPER_FORWARD,
        rules=(
            ProviderAuthorityRule(
                provider="sec",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("public-sec-edgar",),
            ),
        ),
    )


def _session(day: date) -> OfficialSessionCutoff:
    opens_at = datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC)
    closes_at = datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC)
    return OfficialSessionCutoff(
        mic="XNYS",
        session_date=day,
        opens_at=opens_at,
        closes_at=closes_at,
        authority=SessionAuthority.OFFICIAL_EXCHANGE,
        source_identity=f"test:{day.isoformat()}",
        content_sha256=canonical_json_hash({"day": day.isoformat()}),
    )


def _panel(authority: ProviderAuthorityPolicy, *, count: int = 1) -> PanelReadyInput:
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
                security_id=f"figi:{'TEST' if index == 0 else f'T{index:03d}'}",
                issuer_id=f"cik:{_CIK}",
                instrument_id=index + 1,
                canonical_symbol="TEST" if index == 0 else f"T{index:03d}",
            )
            for index in range(count)
        ),
        observations=tuple(
            PanelObservationRef(
                security_id=f"figi:{'TEST' if index == 0 else f'T{index:03d}'}",
                observation_id=f"{index + 3000:064x}",
                observed_at=_CUTOFF.replace(hour=20),
                available_at=_CUTOFF.replace(hour=21),
                content_revision=1,
                content_sha256=f"{index + 4000:064x}",
            )
            for index in range(count)
        ),
    )


def _fact_submission(*, include_sic: bool = True) -> EquityObservationSubmission:
    values = [
        EquityObservationValueInput(
            field_name="acceptance_time_raw",
            value_type="text",
            value="2026-02-04T16:00:00Z",
        ),
        EquityObservationValueInput(field_name="cik", value_type="text", value=_CIK),
        EquityObservationValueInput(
            field_name="end",
            value_type="date",
            value=date(2025, 12, 31),
        ),
        EquityObservationValueInput(
            field_name="filed",
            value_type="date",
            value=date(2026, 2, 4),
        ),
        EquityObservationValueInput(field_name="form", value_type="text", value="10-K"),
        EquityObservationValueInput(
            field_name="start",
            value_type="date",
            value=date(2025, 1, 1),
        ),
        EquityObservationValueInput(
            field_name="tag",
            value_type="text",
            value="Assets",
        ),
        EquityObservationValueInput(
            field_name="taxonomy",
            value_type="text",
            value="us-gaap",
        ),
        EquityObservationValueInput(field_name="unit", value_type="text", value="USD"),
        EquityObservationValueInput(
            field_name="value",
            value_type="decimal",
            value=Decimal("1000000000"),
            unit="USD",
            fiscal_year=2025,
            fiscal_period="FY",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
        ),
    ]
    if include_sic:
        values.append(
            EquityObservationValueInput(
                field_name="historical_sic",
                value_type="integer",
                value=7370,
            )
        )
    canonical = tuple(sorted(values, key=lambda item: (item.field_name, item.ordinal)))
    return EquityObservationSubmission(
        provider="sec",
        product="edgar-companyfacts",
        endpoint=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_CIK}.json",
        dataset_version="prospective-companyfacts-v1",
        tool_version="test-v1",
        source_identity=f"sec:CIK{_CIK}:companyfacts",
        source_revision="5" * 64,
        retrieved_at=_CUTOFF.replace(hour=21),
        timestamp_semantics={
            "filing_source_sha256": "6" * 64,
            "historical_sic_source_sha256": "7" * 64,
        },
        adjustment_policy="not-applicable",
        entitlement_scope="public-sec-edgar",
        entitlement_owner_user_id=None,
        missing_data_policy="unmatched-accession-or-sic-fails-closed",
        artifact_content_sha256="5" * 64,
        instrument_id=1,
        observation_kind="xbrl_fact",
        source_record_identity=f"{_CIK}:accession:Assets",
        event_at=_ACCEPTED,
        available_at=_CUTOFF.replace(hour=21),
        disposition="observed",
        normalized_content_sha256=canonical_json_hash(
            {"values": [item.payload() for item in canonical]}
        ),
        values=canonical,
        accession_number="0000000001-26-000001",
        filing_form="10-K",
        sic_code="7370" if include_sic else None,
    )


def _config() -> FundamentalCalculationConfig:
    return FundamentalCalculationConfig(
        max_fundamental_age_days=800,
        minimum_peer_count=2,
        winsorize_limit=3.0,
    )


def test_resolver_reconstructs_exact_sec_fact_and_three_sleeves() -> None:
    authority = _authority()
    with Session(_engine()) as session:
        session.add(
            Instrument(
                instr_id=1,
                asset_class="equity",
                canonical="TEST",
                exchange="NYSE",
                settlement_currency="USD",
                is_tradable=True,
            )
        )
        session.flush()
        persisted = persist_equity_observation(session, _fact_submission())
        prepared = QualityCompounderDatabaseFactorResolver(
            session,
            provider_authority_policy=authority,
        ).prepare(panel=_panel(authority), config=_config())

        issuer = prepared.fundamental_evidence[0]
        assert issuer.historical_sic == 7370
        assert issuer.facts[0].metric is CanonicalFundamentalMetric.ASSETS
        assert issuer.facts[0].observation_id == persisted.observation_id
        assert tuple(
            item.factor_name for item in prepared.fundamental_snapshot.sleeve_observations
        ) == ("fundamental_growth", "quality", "valuation")
        component_names = {
            item.name for item in prepared.fundamental_snapshot.calculations[0].components
        }
        assert "operating_profitability" in component_names
        assert "gross_profitability" not in component_names
        assert "filing_event_drift" not in component_names
        assert prepared.market_cap_by_symbol == {}
        assert prepared.maximum_available_at == _CUTOFF.replace(hour=21)


def test_resolver_fails_closed_without_historical_sic() -> None:
    authority = _authority()
    with Session(_engine()) as session:
        session.add(
            Instrument(
                instr_id=1,
                asset_class="equity",
                canonical="TEST",
                exchange="NYSE",
                settlement_currency="USD",
                is_tradable=True,
            )
        )
        session.flush()
        persist_equity_observation(session, _fact_submission(include_sic=False))

        with pytest.raises(
            QualityCompounderFactorMaterializationError,
            match="historical SIC",
        ):
            QualityCompounderDatabaseFactorResolver(
                session,
                provider_authority_policy=authority,
            ).prepare(panel=_panel(authority), config=_config())


def test_market_resolver_requires_exact_spy_etf_catalogue() -> None:
    authority = _authority()
    with (
        Session(_engine()) as session,
        pytest.raises(
            QualityCompounderFactorMaterializationError,
            match=r"SPY benchmark.*ETF catalogue",
        ),
    ):
        QualityCompounderDatabaseFactorResolver(
            session,
            provider_authority_policy=authority,
        ).resolve_market_input(
            panel=_panel(authority),
            market_policy=EquityMarketFactorPolicy(
                round_trip_commission_bps=1.0,
                cost_context_sha256="8" * 64,
                required_adjustment_policy="in-house-split-and-dividend-total-return-v1",
            ),
        )


def test_materialization_defaults_are_bound_to_strategy_configuration() -> None:
    policy = QualityCompounderEvidencePolicy()

    assert policy.max_shares_age_days == 120
    assert quality_compounder_configuration_sha256(
        "0.2.0",
        evidence_policy=policy,
    ) != quality_compounder_configuration_sha256(
        "0.2.0",
        evidence_policy=QualityCompounderEvidencePolicy(max_shares_age_days=121),
    )


def test_fundamental_growth_has_two_equal_issuer_appropriate_components() -> None:
    expected = {
        IssuerType.OPERATING_COMPANY: "operating_profit_growth",
        IssuerType.BANK_OR_DIVERSIFIED_FINANCIAL: "net_income_growth",
        IssuerType.INSURER: "net_income_growth",
        IssuerType.REIT: "cash_flow_growth",
    }

    for issuer_type, issuer_growth in expected.items():
        components = equity_factors._COMPONENTS[issuer_type][FundamentalSleeve.FUNDAMENTAL_GROWTH]
        assert [(item.name, item.weight) for item in components] == [
            ("revenue_growth", 0.5),
            (issuer_growth, 0.5),
        ]


def test_listing_prefix_creates_one_percent_structural_exclusion() -> None:
    authority = _authority()
    panel = _panel(authority, count=100)
    sessions = tuple(
        _session(day) for day in (date(2026, 6, 26), date(2026, 6, 29), date(2026, 6, 30))
    )
    benchmark_id = 1000
    identities = {
        member.instrument_id: PointInTimeEquitySecurity(
            instrument_id=member.instrument_id,
            security_id=member.security_id,
            issuer_id=member.issuer_id,
            symbol=member.canonical_symbol,
            sector="Test",
            industry="Test",
            quote_currency="USD",
            tradable=True,
            observation_id=f"{member.instrument_id + 5000:064x}",
            observation_sha256=f"{member.instrument_id + 6000:064x}",
        )
        for member in panel.members
    }
    identities[benchmark_id] = PointInTimeEquitySecurity(
        instrument_id=benchmark_id,
        security_id="figi:SPY",
        issuer_id="issuer:SPY",
        symbol="SPY",
        sector="Benchmark",
        industry="ETF",
        quote_currency="USD",
        tradable=True,
        observation_id="7" * 64,
        observation_sha256="8" * 64,
    )
    recent = panel.members[-1]
    identity_material = {
        recent.instrument_id: SimpleNamespace(
            observation=SimpleNamespace(
                observation_id=identities[recent.instrument_id].observation_id,
                source_record_identity="T099.US:security-identity",
            ),
            lineage=SimpleNamespace(
                provider="eodhd",
                retrieved_at=_CUTOFF.replace(hour=21),
            ),
            values={
                "listing_date": SimpleNamespace(
                    value_type="date",
                    date_value=date(2026, 6, 29),
                )
            },
            authority_sha256=identities[recent.instrument_id].observation_sha256,
        )
    }
    prices = []
    ordinal = 9000
    for instrument_id in (*range(1, 100), benchmark_id):
        for market_session in sessions:
            prices.append(
                SimpleNamespace(
                    instrument_id=instrument_id,
                    session_date=market_session.session_date,
                    observation_id=f"{ordinal:064x}",
                    observation_sha256=f"{ordinal + 1000:064x}",
                )
            )
            ordinal += 1
    for market_session in sessions[1:]:
        prices.append(
            SimpleNamespace(
                instrument_id=recent.instrument_id,
                session_date=market_session.session_date,
                observation_id=f"{ordinal:064x}",
                observation_sha256=f"{ordinal + 1000:064x}",
            )
        )
        ordinal += 1

    complete, exclusions = _partition_listing_warmups(
        panel=panel,
        sessions=sessions,
        benchmark_instrument_id=benchmark_id,
        identities=identities,
        identity_material=identity_material,  # type: ignore[arg-type]
        prices=prices,  # type: ignore[arg-type]
    )

    assert len(complete) == 99
    assert len(exclusions) == 1
    assert exclusions[0].security.security_id == recent.security_id
    assert exclusions[0].missing_session_dates == (date(2026, 6, 26),)
