"""Adverse-news classification and coverage persistence tests."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    EquityModelInferenceRecord,
    EquityObservation,
    EquityObservationValue,
    Instrument,
)
from lib_application.services.adverse_news_observations import (
    AdverseNewsPersistenceError,
    ProviderLineage,
    TypeSafeExchange,
    adverse_news_record_identity,
    persist_adverse_news_classification,
    persist_news_coverage,
)
from lib_application.services.equity_optional_factor_sources import (
    validate_optional_factor_evidence,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.adverse_news_questions import (
    ADVERSE_NEWS_MODEL,
    ADVERSE_NEWS_QUESTION_SET_VERSION,
    AdverseNewsAnswerError,
    AdverseNewsArticle,
)
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.equity_optional_factors import (
    ADVERSE_NEWS_GATE_CONTRACT,
    NEWS_COVERAGE_GATE_CONTRACT,
    OPTIONAL_FACTOR_TIMESTAMP_SCHEMA,
    OptionalFactorSourceContract,
)

_SCOPE = "personal-research-news-pit"
_PUBLISHED = datetime(2025, 1, 20, 14, tzinfo=UTC)
_AVAILABLE = _PUBLISHED + timedelta(minutes=2)
_RECEIVED = datetime(2025, 1, 21, 9, tzinfo=UTC)
_CUTOFF = datetime(2025, 2, 3, 22, tzinfo=UTC)


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)

    @sa.event.listens_for(engine, "connect")
    def _fk(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _authority() -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="adverse-news-test-v1",
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        rules=(
            ProviderAuthorityRule(
                provider="licensed-archive",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=(_SCOPE,),
            ),
        ),
    )


def _lineage(contract: OptionalFactorSourceContract, source: str) -> ProviderLineage:
    return ProviderLineage(
        provider="licensed-archive",
        product=f"historical-{contract.observation_kind.value}",
        endpoint="https://provider.invalid/news",
        dataset_version="news-2025-r3",
        tool_version="adverse-news-adapter-v1",
        source_identity=source,
        source_revision="source-content-sha256-c",
        entitlement_scope=_SCOPE,
        entitlement_owner_user_id=None,
        retrieved_at=_RECEIVED,
        timestamp_semantics={
            "availability_is_retrieval_time": False,
            "availability_source_field": "first_available_at",
            "available_at": contract.available_at_semantic.value,
            "correction_policy": contract.correction_policy.value,
            "event_at": contract.event_at_semantic.value,
            "event_source_field": contract.event_value_field,
            "revision_source_field": "revision_id",
            "schema": OPTIONAL_FACTOR_TIMESTAMP_SCHEMA,
            "source_timezone": "UTC",
        },
        artifact_content_sha256="d" * 64,
    )


def _article() -> AdverseNewsArticle:
    return AdverseNewsArticle(
        provider_article_id="art-1",
        headline="Recall",
        source="wire",
        body="The company recalled a product.",
        language="en",
        document_type="news",
        published_at=_PUBLISHED,
        source_available_at=_AVAILABLE,
    )


# Probabilities are binary fractions so SQLite's float-backed NUMERIC round-trips them
# exactly; PostgreSQL stores any decimal exactly.
def _response(model: str = ADVERSE_NEWS_MODEL) -> dict[str, object]:
    def noul(value: float) -> dict[str, object]:
        return {"type": "noul", "noul": value}

    return {
        "model": model,
        "answers": {
            "about_company": noul(0.875),
            "price_recap": noul(0.125),
            "new_development": noul(0.75),
            "negative_development": noul(0.875),
            "negative_confirmed": noul(0.75),
            "negative_severity": {
                "type": "score",
                "probabilities": {"0": 0.125, "1": 0.125, "2": 0.25, "3": 0.5},
            },
        },
        "usage": {"input_tokens": 10},
    }


def _exchange(
    response: dict[str, object] | None = None, *, request: dict[str, object] | None = None
) -> TypeSafeExchange:
    request = request or {"model": ADVERSE_NEWS_MODEL, "state": {"a": 1}, "questions": {}}
    response = response or _response()
    return TypeSafeExchange(
        request_json=request,
        response_json=response,
        request_bytes=b"request",
        response_bytes=b"response",
        received_at=_RECEIVED,
    )


def _session() -> Session:
    session = Session(_engine())
    for instr_id, canonical in ((1, "AAA"), (2, "BBB")):
        session.add(
            Instrument(
                instr_id=instr_id,
                asset_class="equity",
                canonical=canonical,
                settlement_currency="USD",
                market_session_policy="scheduled",
            )
        )
    session.commit()
    return session


def _classify(
    session: Session, *, instrument_id: int = 1, exchange: TypeSafeExchange | None = None
) -> str:
    return persist_adverse_news_classification(
        session,
        lineage=_lineage(ADVERSE_NEWS_GATE_CONTRACT, "article/art-1"),
        instrument_id=instrument_id,
        article=_article(),
        body_truncated=False,
        exchange=exchange or _exchange(),
    )


def _values(session: Session, observation_id: str, field: str) -> list[EquityObservationValue]:
    return list(
        session.scalars(
            sa.select(EquityObservationValue)
            .where(
                EquityObservationValue.observation_id == observation_id,
                EquityObservationValue.field_name == field,
            )
            .order_by(EquityObservationValue.ordinal)
        )
    )


def test_classification_writes_every_contract_value() -> None:
    with _session() as session:
        observation_id = _classify(session)
        session.commit()
        result = validate_optional_factor_evidence(
            session,
            contract=ADVERSE_NEWS_GATE_CONTRACT,
            factor_version=ADVERSE_NEWS_QUESTION_SET_VERSION,
            observation_id=observation_id,
            cutoff=_CUTOFF,
            provider_authority_policy=_authority(),
            expected_instrument_id=1,
        )
        record = session.get(
            EquityModelInferenceRecord, canonical_json_hash(_exchange().request_json)
        )
    assert result.observation_id == observation_id
    assert record is not None
    assert record.entitlement_scope == _SCOPE
    assert record.model_output_sha256 == canonical_json_hash(_response())


def test_model_version_parsed_from_response_model() -> None:
    with _session() as session:
        observation_id = _classify(session)
        assert _values(session, observation_id, "model_name")[0].text_value == "jev"
        assert _values(session, observation_id, "model_version")[0].text_value == "1.14.0"
        observation = session.get(EquityObservation, observation_id)
        assert observation is not None
        assert observation.source_record_identity == adverse_news_record_identity(
            provider_article_id="art-1", instrument_id=1, model_version="1.14.0"
        )


def test_malformed_model_is_rejected() -> None:
    with _session() as session, pytest.raises(AdverseNewsAnswerError, match="model"):
        _classify(session, exchange=_exchange(_response("jev_1.14")))


def test_severity_written_as_four_ordinals() -> None:
    with _session() as session:
        rows = _values(session, _classify(session), "negative_severity_probability")
    assert [row.ordinal for row in rows] == [0, 1, 2, 3]
    assert [row.decimal_value for row in rows] == [
        Decimal("0.125"),
        Decimal("0.125"),
        Decimal("0.25"),
        Decimal("0.5"),
    ]


def test_entity_relevance_equals_about_company_probability() -> None:
    with _session() as session:
        observation_id = _classify(session)
        relevance = _values(session, observation_id, "entity_relevance")[0].decimal_value
        about = _values(session, observation_id, "about_company_probability")[0].decimal_value
    assert relevance == about == Decimal("0.875")


def test_replay_same_exchange_is_idempotent() -> None:
    with _session() as session:
        first = _classify(session)
        second = _classify(session)
        count = session.scalar(sa.select(sa.func.count()).select_from(EquityModelInferenceRecord))
    assert first == second
    assert count == 1


def test_conflicting_output_for_same_input_raises() -> None:
    changed = _response()
    changed["usage"] = {"input_tokens": 11}
    with _session() as session:
        _classify(session)
        with pytest.raises(AdverseNewsPersistenceError, match="different recorded output"):
            _classify(session, exchange=_exchange(changed))


def test_conflicting_observation_content_raises() -> None:
    changed = copy.deepcopy(_response())
    changed["answers"]["about_company"]["noul"] = 0.5  # type: ignore[index]
    with _session() as session:
        _classify(session)
        with pytest.raises(AdverseNewsPersistenceError):
            _classify(session, exchange=_exchange(changed, request={"model": "x", "state": {}}))


def test_one_article_two_instruments_gets_two_records() -> None:
    with _session() as session:
        first = _classify(session, instrument_id=1)
        second = _classify(
            session,
            instrument_id=2,
            exchange=_exchange(request={"model": ADVERSE_NEWS_MODEL, "state": {"b": 2}}),
        )
        identities = {
            str(row.source_record_identity) for row in session.scalars(sa.select(EquityObservation))
        }
    assert first != second
    assert len(identities) == 2


def _coverage(session: Session, returned: list[str], skipped: list[str]) -> str:
    return persist_news_coverage(
        session,
        lineage=_lineage(NEWS_COVERAGE_GATE_CONTRACT, "query/aaa"),
        instrument_id=1,
        query_window_start=_CUTOFF - timedelta(days=90),
        query_window_end=_CUTOFF,
        completed_at=_CUTOFF + timedelta(hours=7),
        provider_query_identity="query-1",
        returned_article_ids=returned,
        skipped_article_ids=skipped,
    )


def test_coverage_zero_articles_is_valid() -> None:
    with _session() as session:
        observation_id = _coverage(session, [], [])
        session.commit()
        result = validate_optional_factor_evidence(
            session,
            contract=NEWS_COVERAGE_GATE_CONTRACT,
            factor_version="news-coverage-v1",
            observation_id=observation_id,
            cutoff=_CUTOFF + timedelta(hours=7),
            provider_authority_policy=_authority(),
            expected_instrument_id=1,
        )
        count = _values(session, observation_id, "returned_article_count")[0].integer_value
    assert result.observation_id == observation_id
    assert count == 0


def test_coverage_lists_returned_and_skipped_per_ordinal() -> None:
    with _session() as session:
        observation_id = _coverage(session, ["b", "a"], ["b"])
        assert _coverage(session, ["a", "b"], ["b"]) == observation_id
        returned = _values(session, observation_id, "returned_article_identity")
        skipped = _values(session, observation_id, "skipped_article_identity")
    assert [row.text_value for row in returned] == ["a", "b"]
    assert [row.text_value for row in skipped] == ["b"]


def test_coverage_skipped_must_be_subset_of_returned() -> None:
    with _session() as session, pytest.raises(AdverseNewsPersistenceError, match="subset"):
        _coverage(session, ["a"], ["z"])
