"""Production registration boundary tests for synchronized strategy panels."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    EquityFactorSnapshot,
    EquityObservation,
    EquitySecurityIdentity,
    EquitySourceLineage,
    IndexMembership,
    Instrument,
    Strategy,
    StrategyVersion,
)
from lib_application.services.equity_lineage import (
    equity_observation_semantic_sha256,
)
from lib_application.services.equity_rank_snapshots import (
    EquityRankSnapshotPersistenceError,
    persist_equity_rank_snapshot,
)
from lib_application.services.strategy_panel_inputs import (
    StrategyPanelInputPersistenceError,
    StrategyPanelPayloadValidationRequest,
    StrategyPanelPayloadValidationResult,
    effective_membership_sha256,
    factor_panel_sha256,
    persist_strategy_panel_input_revision,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.cross_sectional import (
    CrossSectionalSnapshot,
    FactorSpec,
    MissingFactorDecision,
    MissingFactorReason,
    PeerFallbackPolicy,
)
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.equity_optional_factors import optional_factor_source_registry_sha256
from lib_strategy.panels import (
    EffectivePanelMember,
    OfficialSessionCutoff,
    PanelAuditDecision,
    PanelEvaluationAudit,
    PanelEvaluationRow,
    PanelExclusion,
    PanelReadyInput,
    SessionAuthority,
    panel_ready_input_to_payload,
)

_SESSION_DATE = date(2025, 2, 3)
_CUTOFF = datetime(2025, 2, 3, 21, tzinfo=UTC)
_MEMBERSHIP_OBSERVATION_ID = "1" * 64
_IDENTITY_OBSERVATION_ID = "2" * 64
_LINEAGE_ID = "3" * 64
_FACTOR_SNAPSHOT_ID = "4" * 64


class _ExactManifestValidator:
    def validate_strategy_panel_payload(
        self,
        _session: Session,
        *,
        request: StrategyPanelPayloadValidationRequest,
    ) -> StrategyPanelPayloadValidationResult:
        authority_payload = {
            "schema": "test-exact-strategy-authority-v1",
            "strategy_input_sha256": request.strategy_input_sha256,
            "validated_fields": ["panel"],
        }
        return StrategyPanelPayloadValidationResult(
            validator_id="test-exact-manifest-validator",
            validator_version="1.0.0",
            validated_input_sha256=request.strategy_input_sha256,
            authority_sha256=canonical_json_hash(authority_payload),
            authority_payload=authority_payload,
        )


def _policy() -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="historical-authority-v1",
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        rules=(
            ProviderAuthorityRule(
                provider="reference-provider",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("licensed-history",),
            ),
        ),
    )


def _session(session_date: date, *, execution: bool = False) -> OfficialSessionCutoff:
    day_offset = 1 if execution else 0
    opens_at = datetime(2025, 2, 3 + day_offset, 14, 30, tzinfo=UTC)
    closes_at = datetime(2025, 2, 3 + day_offset, 21, tzinfo=UTC)
    return OfficialSessionCutoff(
        mic="XNYS",
        session_date=session_date + timedelta(days=day_offset),
        opens_at=opens_at,
        closes_at=closes_at,
        authority=SessionAuthority.RESEARCH_LIBRARY,
        source_identity="research-calendar:exchange-calendars:XNYS",
        content_sha256=("6" if execution else "5") * 64,
    )


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed(session: Session) -> tuple[IndexMembership, EquitySecurityIdentity, int]:
    session.add(
        Strategy(
            strategy_id="sp500-rotation",
            strategy_name="S&P 500 rotation",
            asset_class="equity",
        )
    )
    version = StrategyVersion(
        strategy_id="sp500-rotation",
        semver="1.0.0",
        param_schema={},
        default_params={},
    )
    session.add(version)
    session.add(
        Instrument(
            instr_id=1,
            asset_class="equity",
            canonical="AAA",
            settlement_currency="USD",
            is_tradable=True,
            market_session_policy="scheduled",
        )
    )
    lineage = EquitySourceLineage(
        lineage_id=_LINEAGE_ID,
        provider="reference-provider",
        product="sp500-reference-history",
        endpoint="https://provider.example/reference",
        dataset_version="2025-v1",
        tool_version="reference-ingestor-v1",
        source_identity="sp500/reference/2025-02-03",
        source_revision="revision-1",
        retrieved_at=_CUTOFF,
        timestamp_semantics={"available_at": "provider publication timestamp"},
        adjustment_policy="not-applicable",
        entitlement_scope="licensed-history",
        missing_data_policy="fail-closed",
        content_sha256="7" * 64,
    )
    session.add(lineage)
    session.flush()
    membership_observation = EquityObservation(
        observation_id=_MEMBERSHIP_OBSERVATION_ID,
        lineage_id=_LINEAGE_ID,
        instr_id=1,
        observation_kind="membership",
        source_record_identity="sp500-membership:AAA:2025-02-03",
        event_at=_CUTOFF - timedelta(hours=2),
        available_at=_CUTOFF - timedelta(hours=1),
        revision=1,
        disposition="observed",
        content_sha256="8" * 64,
    )
    identity_observation = EquityObservation(
        observation_id=_IDENTITY_OBSERVATION_ID,
        lineage_id=_LINEAGE_ID,
        instr_id=1,
        observation_kind="security_identity",
        source_record_identity="security-identity:FIGI-AAA",
        event_at=_CUTOFF - timedelta(hours=2),
        available_at=_CUTOFF - timedelta(hours=1),
        revision=1,
        disposition="observed",
        content_sha256="9" * 64,
    )
    membership = IndexMembership(
        index_code="SP500",
        instr_id=1,
        effective_from=date(2024, 1, 1),
        effective_to=_SESSION_DATE,
        source_ref=membership_observation.source_record_identity,
        observation_id=membership_observation.observation_id,
    )
    identity = EquitySecurityIdentity(
        security_id="FIGI-AAA",
        issuer_id="ISSUER-AAA",
        canonical_symbol="AAA",
        instr_id=1,
        effective_from=date(2024, 1, 1),
        effective_to=_SESSION_DATE,
        source_ref=identity_observation.source_record_identity,
        observation_id=identity_observation.observation_id,
    )
    session.add_all((membership_observation, identity_observation, membership, identity))
    session.flush()
    session.add(
        EquityFactorSnapshot(
            factor_snapshot_id=_FACTOR_SNAPSHOT_ID,
            strategy_id="sp500-rotation",
            strat_ver_id=int(version.strat_ver_id),
            instr_id=1,
            effective_session=_SESSION_DATE,
            cutoff_at=_CUTOFF,
            calculation_version="panel-v1",
            configuration_digest="a" * 64,
            source_contract_registry_sha256=optional_factor_source_registry_sha256(),
            peer_taxonomy_version="taxonomy-v1",
            peer_group="Industrials",
            completeness_status="incomplete",
            expected_factor_count=1,
            available_factor_count=0,
            content_sha256=_FACTOR_SNAPSHOT_ID,
        )
    )
    session.flush()
    return membership, identity, int(version.strat_ver_id)


def _panel(
    membership: IndexMembership,
    identity: EquitySecurityIdentity,
    session: Session,
) -> PanelReadyInput:
    policy = _policy()
    member = EffectivePanelMember("FIGI-AAA", "ISSUER-AAA", 1, "AAA")
    snapshot = session.get(EquityFactorSnapshot, _FACTOR_SNAPSHOT_ID)
    assert snapshot is not None
    panel = PanelReadyInput(
        cutoff=_CUTOFF,
        session=_session(_SESSION_DATE),
        execution_session=_session(_SESSION_DATE, execution=True),
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        provider_authority_policy=policy,
        provider_authority_sha256=policy.digest,
        membership_sha256="b" * 64,
        factor_snapshot_sha256=factor_panel_sha256([snapshot]),
        members=(member,),
        observations=(),
        exclusions=(
            PanelExclusion(
                security_id=member.security_id,
                reason_code="factor_evidence_incomplete",
                disposition_identity=_FACTOR_SNAPSHOT_ID,
                content_sha256=_FACTOR_SNAPSHOT_ID,
            ),
        ),
    )
    membership_observation = session.get(EquityObservation, _MEMBERSHIP_OBSERVATION_ID)
    identity_observation = session.get(EquityObservation, _IDENTITY_OBSERVATION_ID)
    lineage = session.get(EquitySourceLineage, _LINEAGE_ID)
    assert membership_observation is not None
    assert identity_observation is not None
    assert lineage is not None
    digest = effective_membership_sha256(
        universe_code="SP500",
        panel=panel,
        membership_rows={1: membership},
        observation_sha256_by_instrument={
            1: equity_observation_semantic_sha256(membership_observation, lineage)
        },
        security_identity_rows={1: identity},
        security_identity_observation_sha256_by_instrument={
            1: equity_observation_semantic_sha256(identity_observation, lineage)
        },
    )
    return replace(panel, membership_sha256=digest)


def test_registration_accepts_inclusive_end_date_and_persists_validator_proof() -> None:
    engine = _engine()
    with Session(engine) as session:
        membership, identity, _ = _seed(session)
        panel = _panel(membership, identity, session)
        payload = {"schema": "strategy-input-test-v1", "panel": panel_ready_input_to_payload(panel)}

        row = persist_strategy_panel_input_revision(
            session,
            strategy_id="sp500-rotation",
            strategy_version="1.0.0",
            universe_code="sp500",
            panel=panel,
            strategy_input_payload=payload,
            now=_CUTOFF,
            strategy_payload_validator=_ExactManifestValidator(),
        )
        session.commit()
        assert row.official_session_date == _SESSION_DATE
        assert row.strategy_validator_id == "test-exact-manifest-validator"
        assert row.strategy_input_authority_sha256 == canonical_json_hash(
            row.strategy_input_authority_payload
        )


def test_registration_rejects_caller_relabelled_issuer() -> None:
    engine = _engine()
    with Session(engine) as session:
        membership, identity, _ = _seed(session)
        panel = _panel(membership, identity, session)
        relabelled = replace(
            panel,
            members=(replace(panel.members[0], issuer_id="OTHER-ISSUER"),),
        )
        payload = {
            "schema": "strategy-input-test-v1",
            "panel": panel_ready_input_to_payload(relabelled),
        }

        with pytest.raises(
            StrategyPanelInputPersistenceError,
            match="security or issuer identity differs",
        ):
            persist_strategy_panel_input_revision(
                session,
                strategy_id="sp500-rotation",
                strategy_version="1.0.0",
                universe_code="SP500",
                panel=relabelled,
                strategy_input_payload=payload,
                now=_CUTOFF,
                strategy_payload_validator=_ExactManifestValidator(),
            )


def test_membership_digest_excludes_database_surrogate_ids() -> None:
    engine = _engine()
    with Session(engine) as session:
        membership, identity, _ = _seed(session)
        panel = _panel(membership, identity, session)
        membership_observation = session.get(EquityObservation, _MEMBERSHIP_OBSERVATION_ID)
        identity_observation = session.get(EquityObservation, _IDENTITY_OBSERVATION_ID)
        lineage = session.get(EquitySourceLineage, _LINEAGE_ID)
        assert membership_observation is not None
        assert identity_observation is not None
        assert lineage is not None
        membership_observation_sha256 = equity_observation_semantic_sha256(
            membership_observation, lineage
        )
        identity_observation_sha256 = equity_observation_semantic_sha256(
            identity_observation, lineage
        )
        copied_membership = IndexMembership(
            membership_id=999,
            index_code=membership.index_code,
            instr_id=membership.instr_id,
            effective_from=membership.effective_from,
            effective_to=membership.effective_to,
            source_ref=membership.source_ref,
            observation_id=membership.observation_id,
        )
        copied_identity = EquitySecurityIdentity(
            identity_id=999,
            security_id=identity.security_id,
            issuer_id=identity.issuer_id,
            canonical_symbol=identity.canonical_symbol,
            instr_id=identity.instr_id,
            effective_from=identity.effective_from,
            effective_to=identity.effective_to,
            source_ref=identity.source_ref,
            observation_id=identity.observation_id,
        )

        first = effective_membership_sha256(
            universe_code="SP500",
            panel=panel,
            membership_rows={1: membership},
            observation_sha256_by_instrument={1: membership_observation_sha256},
            security_identity_rows={1: identity},
            security_identity_observation_sha256_by_instrument={1: identity_observation_sha256},
        )
        second = effective_membership_sha256(
            universe_code="SP500",
            panel=panel,
            membership_rows={1: copied_membership},
            observation_sha256_by_instrument={1: membership_observation_sha256},
            security_identity_rows={1: copied_identity},
            security_identity_observation_sha256_by_instrument={1: identity_observation_sha256},
        )

    assert first == second


def _incomplete_rank_audit() -> PanelEvaluationAudit:
    rank_snapshot = CrossSectionalSnapshot(
        cutoff=_CUTOFF,
        calculation_version="panel-v1",
        minimum_peer_count=2,
        winsorize_limit=3.0,
        fallback_policy=PeerFallbackPolicy.INELIGIBLE,
        factor_specs=(FactorSpec(name="quality", weight=1.0),),
        contributions=(),
        ranks=(),
        missing_decisions=(
            MissingFactorDecision(
                entity_id="FIGI-AAA",
                symbol="AAA",
                factor_name="quality",
                reason=MissingFactorReason.SOURCE_MISSING,
                detail="factor_evidence_incomplete",
            ),
        ),
    )
    return PanelEvaluationAudit(
        rank_snapshot=rank_snapshot,
        rows=(
            PanelEvaluationRow(
                entity_id="FIGI-AAA",
                symbol="AAA",
                instrument_id=1,
                factor_snapshot_id=None,
                rank_complete=False,
                strategy_eligible=False,
                decision=PanelAuditDecision.EXCLUDED,
                incumbent=False,
                rank=None,
                composite_score=None,
                target_allocation_hint=None,
                exclusion_reason="factor_evidence_incomplete",
            ),
        ),
        configuration_sha256="a" * 64,
        peer_taxonomy_version="taxonomy-v1",
        replayed=False,
        intentional_cash_slots=1,
        required_factor_versions=(("quality", "quality-v1"),),
    )


def test_rank_persistence_records_factor_incomplete_rows_and_exact_replay() -> None:
    engine = _engine()
    with Session(engine) as session:
        membership, identity, _ = _seed(session)
        panel = _panel(membership, identity, session)
        audit = _incomplete_rank_audit()

        first = persist_equity_rank_snapshot(
            session,
            strategy_id="sp500-rotation",
            strategy_version="1.0.0",
            panel=panel,
            audit=audit,
        )
        session.commit()
        replay = persist_equity_rank_snapshot(
            session,
            strategy_id="sp500-rotation",
            strategy_version="1.0.0",
            panel=panel,
            audit=audit,
        )

    assert first.created is True
    assert replay.created is False
    assert replay.rank_snapshot_id == first.rank_snapshot_id


def test_rank_persistence_rejects_cutoff_mismatch_before_writing() -> None:
    engine = _engine()
    with Session(engine) as session:
        membership, identity, _ = _seed(session)
        panel = _panel(membership, identity, session)
        audit = _incomplete_rank_audit()
        mismatched_snapshot = replace(
            audit.rank_snapshot,
            cutoff=_CUTOFF - timedelta(days=1),
        )
        mismatched_audit = replace(audit, rank_snapshot=mismatched_snapshot)

        with pytest.raises(
            EquityRankSnapshotPersistenceError,
            match="cutoff does not match",
        ):
            persist_equity_rank_snapshot(
                session,
                strategy_id="sp500-rotation",
                strategy_version="1.0.0",
                panel=panel,
                audit=mismatched_audit,
            )
