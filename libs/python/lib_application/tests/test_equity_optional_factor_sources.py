"""Provider-neutral optional-factor evidence boundary tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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
    Strategy,
    StrategyVersion,
)
from lib_application.services.equity_factor_snapshots import (
    EquityEvidenceReference,
    EquityFactorDetailInput,
    EquityFactorSnapshotPersistenceError,
    EquityFactorSnapshotSubmission,
    EquityFactorState,
    persist_equity_factor_snapshot,
)
from lib_application.services.equity_lineage import EquityObservationAuthorityError
from lib_application.services.equity_optional_factor_sources import (
    OptionalFactorEvidenceValidationError,
    validate_optional_factor_evidence,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.cross_sectional import PeerScaleMethod
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.equity_optional_factors import (
    OPTIONAL_FACTOR_SLEEVES,
    OPTIONAL_FACTOR_SOURCE_CONTRACTS,
    OPTIONAL_FACTOR_TIMESTAMP_SCHEMA,
    OptionalFactorApplication,
    optional_factor_source_contract,
    optional_factor_source_registry_sha256,
)

_CUTOFF = datetime(2025, 2, 3, 22, tzinfo=UTC)
_LINEAGE_ID = "a" * 64
_OBSERVATION_ID = "b" * 64
_ENTITLEMENT_SCOPE = "personal-research-news-pit"


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _authority(*, entitlement_scope: str = _ENTITLEMENT_SCOPE) -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="optional-factor-test-v1",
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        rules=(
            ProviderAuthorityRule(
                provider="licensed-archive",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=(entitlement_scope,),
            ),
        ),
    )


def _timestamp_semantics(*, retrieval_is_availability: bool = False) -> dict[str, object]:
    contract = optional_factor_source_contract("news_sentiment")
    return {
        "availability_is_retrieval_time": retrieval_is_availability,
        "availability_source_field": "firstPublishedAt",
        "available_at": contract.available_at_semantic.value,
        "correction_policy": contract.correction_policy.value,
        "event_at": contract.event_at_semantic.value,
        "event_source_field": "publishedAt",
        "revision_source_field": "revisionId",
        "schema": OPTIONAL_FACTOR_TIMESTAMP_SCHEMA,
        "source_timezone": "UTC",
    }


def _typed_value(
    *,
    observation_id: str,
    field_name: str,
    value: object,
) -> EquityObservationValue:
    kwargs: dict[str, object] = {
        "value_id": canonical_json_hash(
            {"field_name": field_name, "observation_id": observation_id}
        ),
        "observation_id": observation_id,
        "field_name": field_name,
        "ordinal": 0,
    }
    if isinstance(value, bool):
        kwargs.update(value_type="boolean", boolean_value=value)
    elif isinstance(value, datetime):
        kwargs.update(value_type="timestamp", timestamp_value=value)
    elif isinstance(value, date):
        kwargs.update(value_type="date", date_value=value)
    elif isinstance(value, Decimal):
        kwargs.update(value_type="decimal", decimal_value=value)
    elif isinstance(value, int):
        kwargs.update(value_type="integer", integer_value=value)
    elif isinstance(value, str):
        kwargs.update(value_type="text", text_value=value)
    else:
        message = f"unsupported optional-factor fixture value {field_name!r}"
        raise TypeError(message)
    return EquityObservationValue(**kwargs)


def _seed_optional(
    session: Session,
    *,
    sleeve_name: str,
    retrieval_is_availability: bool = False,
    model_input_sha256: str = "c" * 64,
    revision: int = 1,
) -> tuple[str, int | None]:
    contract = optional_factor_source_contract(sleeve_name)
    observation_id = (
        _OBSERVATION_ID
        if sleeve_name == "news_sentiment"
        else canonical_json_hash({"optional_sleeve": sleeve_name})
    )
    lineage_id = (
        _LINEAGE_ID
        if sleeve_name == "news_sentiment"
        else canonical_json_hash({"optional_lineage": sleeve_name})
    )
    instrument_id = 1 if contract.requires_instrument else None
    if instrument_id is not None:
        session.add(
            Instrument(
                instr_id=instrument_id,
                asset_class="equity",
                canonical="AAA",
                settlement_currency="USD",
                market_session_policy="scheduled",
            )
        )
    event_at = _CUTOFF - timedelta(hours=4)
    available_at = _CUTOFF - timedelta(hours=3, minutes=59)
    timestamp_semantics = {
        "availability_is_retrieval_time": retrieval_is_availability,
        "availability_source_field": "first_available_at",
        "available_at": contract.available_at_semantic.value,
        "correction_policy": contract.correction_policy.value,
        "event_at": contract.event_at_semantic.value,
        "event_source_field": contract.event_value_field,
        "revision_source_field": "revision_id",
        "schema": OPTIONAL_FACTOR_TIMESTAMP_SCHEMA,
        "source_timezone": "UTC",
    }
    session.add(
        EquitySourceLineage(
            lineage_id=lineage_id,
            provider="licensed-archive",
            product=f"historical-{contract.observation_kind.value}",
            endpoint=f"https://provider.invalid/{contract.observation_kind.value}/archive",
            dataset_version=f"{contract.observation_kind.value}-2025-r3",
            tool_version="optional-factor-adapter-v1",
            source_identity=f"{contract.observation_kind.value}/2025-02-03",
            source_revision="source-content-sha256-c",
            retrieved_at=_CUTOFF + timedelta(days=500),
            timestamp_semantics=timestamp_semantics,
            adjustment_policy="not-applicable",
            entitlement_scope=_ENTITLEMENT_SCOPE,
            missing_data_policy="fail-closed",
            content_sha256="d" * 64,
        )
    )
    session.flush()
    session.add(
        EquityObservation(
            observation_id=observation_id,
            lineage_id=lineage_id,
            instr_id=instrument_id,
            observation_kind=contract.observation_kind.value,
            source_record_identity=f"{contract.observation_kind.value}-123",
            event_at=event_at,
            available_at=available_at,
            revision=revision,
            disposition="observed",
            content_sha256="e" * 64,
        )
    )
    values: dict[str, object] = {}
    for requirement in contract.required_values:
        field_name = requirement.field_name
        if field_name == contract.event_value_field:
            value = event_at if requirement.value_type.value == "timestamp" else event_at.date()
        elif field_name == contract.availability_value_field:
            value = available_at
        elif field_name == "model_input_sha256":
            value = model_input_sha256
        elif field_name in contract.required_digest_fields:
            value = "c" * 64
        elif requirement.value_type.value == "boolean":
            value = False
        elif requirement.value_type.value == "date":
            value = event_at.date()
        elif requirement.value_type.value == "decimal":
            value = Decimal("1.0")
        elif requirement.value_type.value == "integer":
            value = 5
        elif requirement.value_type.value == "timestamp":
            value = event_at
        else:
            value = f"{field_name}-value"
        values[field_name] = value
    session.add_all(
        _typed_value(
            observation_id=observation_id,
            field_name=field_name,
            value=value,
        )
        for field_name, value in sorted(values.items())
    )
    session.commit()
    return observation_id, instrument_id


def _seed_news(
    session: Session,
    *,
    retrieval_is_availability: bool = False,
    model_input_sha256: str = "c" * 64,
    revision: int = 1,
) -> None:
    _seed_optional(
        session,
        sleeve_name="news_sentiment",
        retrieval_is_availability=retrieval_is_availability,
        model_input_sha256=model_input_sha256,
        revision=revision,
    )


def test_optional_source_registry_is_complete_unique_and_content_addressed() -> None:
    assert OPTIONAL_FACTOR_SLEEVES == (
        "analyst_revisions",
        "call_sentiment",
        "crowding",
        "insider_activity",
        "macro",
        "news_sentiment",
    )
    assert tuple(contract.sleeve.value for contract in OPTIONAL_FACTOR_SOURCE_CONTRACTS) == (
        tuple(sorted(OPTIONAL_FACTOR_SLEEVES))
    )
    assert len({contract.observation_kind for contract in OPTIONAL_FACTOR_SOURCE_CONTRACTS}) == len(
        OPTIONAL_FACTOR_SOURCE_CONTRACTS
    )
    assert len(optional_factor_source_registry_sha256()) == 64
    assert (
        optional_factor_source_contract("macro").activation.application
        is OptionalFactorApplication.MARKET_REGIME_OVERLAY
    )
    assert optional_factor_source_contract("crowding").activation.direction.value == (
        "lower_is_better"
    )
    for sleeve_name in ("call_sentiment", "news_sentiment"):
        contract = optional_factor_source_contract(sleeve_name)
        assert contract.activation.requires_model_transform is True
        assert {
            "inference_configuration_sha256",
            "model_artifact_sha256",
            "model_input_sha256",
            "model_output_sha256",
            "output_schema_sha256",
            "prompt_or_feature_schema_sha256",
        } <= set(contract.required_digest_fields)


@pytest.mark.parametrize("sleeve_name", OPTIONAL_FACTOR_SLEEVES)
def test_every_registered_optional_source_contract_is_enforced(sleeve_name: str) -> None:
    engine = _engine()
    with Session(engine) as session:
        observation_id, instrument_id = _seed_optional(session, sleeve_name=sleeve_name)

        result = validate_optional_factor_evidence(
            session,
            sleeve_name=sleeve_name,
            factor_version=f"{sleeve_name}-factor-v1",
            observation_id=observation_id,
            cutoff=_CUTOFF,
            provider_authority_policy=_authority(),
            expected_instrument_id=instrument_id,
        )

    contract = optional_factor_source_contract(sleeve_name)
    assert result.sleeve_name == sleeve_name
    assert result.source_contract_sha256 == contract.digest
    assert result.entitlement_owner_user_id is None


def test_complete_news_source_reuses_authority_cutoff_and_lineage_contracts() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_news(session)

        result = validate_optional_factor_evidence(
            session,
            sleeve_name="news_sentiment",
            factor_version="news-sentiment-model-v1",
            observation_id=_OBSERVATION_ID,
            cutoff=_CUTOFF,
            provider_authority_policy=_authority(),
            expected_instrument_id=1,
        )

    assert result.sleeve_name == "news_sentiment"
    assert result.observation_id == _OBSERVATION_ID
    assert result.lineage_id == _LINEAGE_ID
    assert result.provider == "licensed-archive"
    assert len(result.source_contract_sha256) == 64


def test_disabled_version_cannot_consume_optional_evidence() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_news(session)

        with pytest.raises(
            OptionalFactorEvidenceValidationError,
            match="factor_version must be a non-blank immutable identity",
        ):
            validate_optional_factor_evidence(
                session,
                sleeve_name="news_sentiment",
                factor_version=None,
                observation_id=_OBSERVATION_ID,
                cutoff=_CUTOFF,
                provider_authority_policy=_authority(),
                expected_instrument_id=1,
            )


@pytest.mark.parametrize(
    ("retrieval_is_availability", "model_input_sha256", "match"),
    [
        (True, "c" * 64, "timestamp semantics are invalid"),
        (False, "not-a-digest", "must be lowercase SHA-256"),
    ],
)
def test_optional_evidence_rejects_retrieval_time_and_unbound_model_input(
    retrieval_is_availability: bool,
    model_input_sha256: str,
    match: str,
) -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_news(
            session,
            retrieval_is_availability=retrieval_is_availability,
            model_input_sha256=model_input_sha256,
        )

        with pytest.raises(OptionalFactorEvidenceValidationError, match=match):
            validate_optional_factor_evidence(
                session,
                sleeve_name="news_sentiment",
                factor_version="news-sentiment-model-v1",
                observation_id=_OBSERVATION_ID,
                cutoff=_CUTOFF,
                provider_authority_policy=_authority(),
                expected_instrument_id=1,
            )


def test_optional_evidence_requires_exact_entitlement_authority() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_news(session)

        with pytest.raises(EquityObservationAuthorityError, match="outside provider authority"):
            validate_optional_factor_evidence(
                session,
                sleeve_name="news_sentiment",
                factor_version="news-sentiment-model-v1",
                observation_id=_OBSERVATION_ID,
                cutoff=_CUTOFF,
                provider_authority_policy=_authority(entitlement_scope="different-scope"),
                expected_instrument_id=1,
            )


def test_optional_correction_requires_explicit_supersession_parent() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_news(session, revision=2)

        with pytest.raises(
            OptionalFactorEvidenceValidationError,
            match="corrections require an explicit supersession parent",
        ):
            validate_optional_factor_evidence(
                session,
                sleeve_name="news_sentiment",
                factor_version="news-sentiment-model-v1",
                observation_id=_OBSERVATION_ID,
                cutoff=_CUTOFF,
                provider_authority_policy=_authority(),
                expected_instrument_id=1,
            )


def test_factor_snapshot_persistence_cannot_bypass_optional_source_contract() -> None:
    engine = _engine()
    with Session(engine) as session:
        observation_id, _instrument_id = _seed_optional(
            session,
            sleeve_name="news_sentiment",
        )
        session.add(
            Strategy(
                strategy_id="optional-contract-strategy",
                strategy_name="Optional contract boundary",
                asset_class="equity",
            )
        )
        version = StrategyVersion(
            strategy_id="optional-contract-strategy",
            semver="2.0.0",
            param_schema={},
            default_params={},
        )
        session.add(version)
        session.commit()

        news_contract = optional_factor_source_contract("news_sentiment")
        detail = EquityFactorDetailInput(
            factor_name="news_sentiment",
            sleeve_name="news_sentiment",
            factor_version="news-sentiment-v1",
            direction=news_contract.activation.direction,
            enabled=True,
            state=EquityFactorState.COMPLETE,
            weight=Decimal("1"),
            evidence=(EquityEvidenceReference(observation_id),),
            raw_value=Decimal("0.5"),
            peer_group="Information Technology",
            peer_count=10,
            peer_center=Decimal("0"),
            peer_scale=Decimal("1"),
            peer_scale_method=PeerScaleMethod.MEDIAN_ABSOLUTE_DEVIATION,
            unbounded_normalized_value=Decimal("0.5"),
            normalized_value=Decimal("0.5"),
            factor_rank=Decimal("1"),
            contribution=Decimal("0.5"),
        )
        submission = EquityFactorSnapshotSubmission(
            strategy_id="optional-contract-strategy",
            strategy_version_id=int(version.strat_ver_id),
            instrument_id=1,
            effective_session=date(2025, 2, 3),
            cutoff_at=_CUTOFF,
            calculation_version="optional-factor-panel-v1",
            configuration_digest="f" * 64,
            source_contract_registry_sha256=optional_factor_source_registry_sha256(),
            peer_taxonomy_version="point-in-time-sector-industry-v1",
            peer_group="Information Technology",
            details=(detail,),
        )

        with pytest.raises(
            EquityFactorSnapshotPersistenceError,
            match="requires provider authority",
        ):
            persist_equity_factor_snapshot(session, submission)

        insider_contract = optional_factor_source_contract("insider_activity")
        mismatched = EquityFactorDetailInput(
            factor_name="insider_activity",
            sleeve_name="insider_activity",
            factor_version="insider-activity-v1",
            direction=insider_contract.activation.direction,
            enabled=True,
            state=EquityFactorState.COMPLETE,
            weight=Decimal("1"),
            evidence=(EquityEvidenceReference(observation_id),),
            raw_value=Decimal("0.5"),
            peer_group="Information Technology",
            peer_count=10,
            peer_center=Decimal("0"),
            peer_scale=Decimal("1"),
            peer_scale_method=PeerScaleMethod.MEDIAN_ABSOLUTE_DEVIATION,
            unbounded_normalized_value=Decimal("0.5"),
            normalized_value=Decimal("0.5"),
            factor_rank=Decimal("1"),
            contribution=Decimal("0.5"),
        )
        with pytest.raises(
            EquityFactorSnapshotPersistenceError,
            match="evidence is invalid",
        ):
            persist_equity_factor_snapshot(
                session,
                EquityFactorSnapshotSubmission(
                    strategy_id=submission.strategy_id,
                    strategy_version_id=submission.strategy_version_id,
                    instrument_id=submission.instrument_id,
                    effective_session=submission.effective_session,
                    cutoff_at=submission.cutoff_at,
                    calculation_version=submission.calculation_version,
                    configuration_digest=submission.configuration_digest,
                    source_contract_registry_sha256=(submission.source_contract_registry_sha256),
                    peer_taxonomy_version=submission.peer_taxonomy_version,
                    peer_group=submission.peer_group,
                    details=(mismatched,),
                ),
                provider_authority_policy=_authority(),
            )
