"""Adverse-news gate resolver tests at a quarter-end cutoff."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import lib_application.services.adverse_news_observations as observations_module
from lib_application.db.models import (
    Base,
    EquityObservation,
    EquityObservationValue,
    Instrument,
)
from lib_application.services.adverse_news_gate import (
    AdverseNewsGatePolicy,
    AdverseNewsGateResult,
    resolve_quality_compounder_adverse_news_gate,
)
from lib_application.services.adverse_news_observations import (
    ProviderLineage,
    TypeSafeExchange,
    persist_adverse_news_classification,
    persist_news_coverage,
)
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
    persist_equity_observation,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.adverse_news_questions import (
    ADVERSE_NEWS_MODEL,
    AdverseNewsArticle,
    GateReason,
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
_PROVIDER = "licensed-archive"
_CUTOFF = datetime(2026, 12, 31, 21, tzinfo=UTC)
_PUBLISHED = _CUTOFF - timedelta(days=10)
_CLASSIFIED = _CUTOFF + timedelta(hours=12)
_COMPLETED = _CUTOFF + timedelta(hours=7)
_POLICY = AdverseNewsGatePolicy()

# Probabilities are binary fractions so SQLite's float-backed NUMERIC round-trips them.
_ADVERSE = {
    "about_company": 0.875,
    "price_recap": 0.125,
    "new_development": 0.75,
    "negative_development": 0.875,
    "negative_confirmed": 0.75,
}
_CLEAR = {**_ADVERSE, "negative_development": 0.125}
_UNSETTLED = {**_ADVERSE, "negative_development": 0.5}
_SEVERITY = {"0": 0.125, "1": 0.125, "2": 0.25, "3": 0.5}


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
        policy_version="adverse-news-gate-test-v1",
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        rules=(
            ProviderAuthorityRule(
                provider=_PROVIDER,
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=(_SCOPE,),
            ),
        ),
    )


def _lineage(
    contract: OptionalFactorSourceContract, source: str, *, provider: str = _PROVIDER
) -> ProviderLineage:
    return ProviderLineage(
        provider=provider,
        product=f"historical-{contract.observation_kind.value}",
        endpoint="https://provider.invalid/news",
        dataset_version="news-2026-r1",
        tool_version="adverse-news-adapter-v1",
        source_identity=source,
        source_revision="source-content-sha256-c",
        entitlement_scope=_SCOPE,
        entitlement_owner_user_id=None,
        retrieved_at=_CUTOFF - timedelta(days=1),
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
    session: Session,
    *,
    article_id: str = "art-1",
    instrument_id: int = 1,
    answers: dict[str, float] | None = None,
    published_at: datetime = _PUBLISHED,
    available_after: timedelta = timedelta(minutes=2),
    received_at: datetime = _CLASSIFIED,
    model: str = ADVERSE_NEWS_MODEL,
    provider: str = _PROVIDER,
) -> str:
    probabilities = answers or _ADVERSE
    response: dict[str, object] = {
        "model": model,
        "answers": {
            **{key: {"type": "noul", "noul": value} for key, value in probabilities.items()},
            "negative_severity": {"type": "score", "probabilities": _SEVERITY},
        },
        "usage": {"input_tokens": 10},
    }
    request: dict[str, object] = {
        "model": model,
        "state": {"article": article_id, "instrument": instrument_id, "answers": probabilities},
        "questions": {},
    }
    return persist_adverse_news_classification(
        session,
        lineage=_lineage(ADVERSE_NEWS_GATE_CONTRACT, f"article/{article_id}", provider=provider),
        instrument_id=instrument_id,
        article=AdverseNewsArticle(
            provider_article_id=article_id,
            headline="Recall",
            source="wire",
            body="The company recalled a product.",
            language="en",
            document_type="news",
            published_at=published_at,
            source_available_at=published_at + available_after,
        ),
        body_truncated=False,
        exchange=TypeSafeExchange(
            request_json=request,
            response_json=response,
            request_bytes=b"request",
            response_bytes=b"response",
            received_at=received_at,
        ),
    )


def _coverage(
    session: Session,
    returned: list[str],
    skipped: list[str] | None = None,
    *,
    instrument_id: int = 1,
    start: datetime = _CUTOFF - timedelta(days=90),
    end: datetime = _CUTOFF,
    completed_at: datetime = _COMPLETED,
) -> str:
    return persist_news_coverage(
        session,
        lineage=_lineage(NEWS_COVERAGE_GATE_CONTRACT, f"query/{instrument_id}"),
        instrument_id=instrument_id,
        query_window_start=start,
        query_window_end=end,
        completed_at=completed_at,
        provider_query_identity=f"query-{instrument_id}-{start.isoformat()}",
        returned_article_ids=returned,
        skipped_article_ids=skipped or [],
    )


def _resolve(
    session: Session,
    *,
    instrument_ids: tuple[int, ...] = (1,),
    policy: AdverseNewsGatePolicy = _POLICY,
) -> dict[int, AdverseNewsGateResult]:
    session.commit()
    return dict(
        resolve_quality_compounder_adverse_news_gate(
            session,
            cutoff=_CUTOFF,
            instrument_ids=instrument_ids,
            policy=policy,
            provider_authority_policy=_authority(),
        )
    )


def test_policy_defaults_match_the_frozen_gate() -> None:
    assert _POLICY.question_set_version == "adverse-news-questions-v1"
    assert _POLICY.model_version == "1.14.0"
    assert _POLICY.lookback_days == 90
    assert _POLICY.coverage_min_delay == timedelta(hours=6)
    assert _POLICY.deadline == timedelta(hours=72)


def test_clear_when_covered_and_no_adverse_articles() -> None:
    with _session() as session:
        classification = _classify(session, answers=_CLEAR)
        coverage = _coverage(session, ["art-1"])
        results = _resolve(session, instrument_ids=(1, 2))
    assert results[1] == AdverseNewsGateResult(
        reason=None, observation_ids=tuple(sorted((classification, coverage))), unsettled_count=0
    )
    assert results[2].reason is GateReason.NEWS_COVERAGE_UNAVAILABLE


def test_zero_articles_with_full_coverage_is_clear() -> None:
    with _session() as session:
        coverage = _coverage(session, [])
        result = _resolve(session)[1]
    assert result == AdverseNewsGateResult(None, (coverage,), 0)


def test_material_adverse_news_blocks() -> None:
    with _session() as session:
        _classify(session)
        _coverage(session, ["art-1"])
        result = _resolve(session)[1]
    assert result.reason is GateReason.MATERIAL_ADVERSE_NEWS
    assert len(result.observation_ids) == 2


def test_unsettled_counts_but_does_not_block() -> None:
    with _session() as session:
        _classify(session, answers=_UNSETTLED)
        _classify(session, article_id="art-2", answers=_CLEAR)
        _coverage(session, ["art-1", "art-2"])
        result = _resolve(session)[1]
    assert result.reason is None
    assert result.unsettled_count == 1


def test_coverage_gap_blocks_with_coverage_unavailable() -> None:
    with _session() as session:
        _classify(session, answers=_CLEAR)
        _coverage(session, ["art-1"], start=_CUTOFF - timedelta(days=60))
        result = _resolve(session)[1]
    assert result.reason is GateReason.NEWS_COVERAGE_UNAVAILABLE


def test_contiguous_coverage_records_cover_the_window() -> None:
    with _session() as session:
        _classify(session, answers=_CLEAR)
        _coverage(session, [], start=_CUTOFF - timedelta(days=90), end=_CUTOFF + timedelta(hours=1))
        _coverage(session, ["art-1"], start=_CUTOFF - timedelta(days=30))
        result = _resolve(session)[1]
    assert result.reason is None
    assert len(result.observation_ids) == 3


def test_coverage_completed_before_t_plus_6h_is_rejected() -> None:
    with _session() as session:
        _coverage(session, [], completed_at=_CUTOFF + timedelta(hours=5))
        result = _resolve(session)[1]
    assert result.reason is GateReason.NEWS_COVERAGE_UNAVAILABLE


def test_coverage_after_deadline_is_stale() -> None:
    with _session() as session:
        _coverage(session, [], completed_at=_CUTOFF + timedelta(hours=73))
        result = _resolve(session)[1]
    assert result.reason is GateReason.NEWS_COVERAGE_UNAVAILABLE


def test_coverage_window_not_reaching_cutoff_is_stale() -> None:
    with _session() as session:
        _coverage(session, [], end=_CUTOFF - timedelta(hours=1))
        result = _resolve(session)[1]
    assert result.reason is GateReason.NEWS_COVERAGE_UNAVAILABLE


def test_classified_after_deadline_is_invalid() -> None:
    with _session() as session:
        _classify(session, answers=_CLEAR, received_at=_CUTOFF + timedelta(hours=73))
        _coverage(session, ["art-1"])
        result = _resolve(session)[1]
    assert result.reason is GateReason.NEWS_EVIDENCE_INVALID


def test_model_released_after_cutoff_is_invalid() -> None:
    policy = replace(_POLICY, model_released_on=(_CUTOFF + timedelta(days=1)).date())
    with _session() as session:
        _classify(session, answers=_CLEAR)
        _coverage(session, ["art-1"])
        result = _resolve(session, policy=policy)[1]
    assert result.reason is GateReason.NEWS_EVIDENCE_INVALID


def test_unauthorized_classification_is_invalid_not_raised() -> None:
    with _session() as session:
        _classify(session, answers=_CLEAR, provider="unlicensed-archive")
        _coverage(session, ["art-1"])
        result = _resolve(session)[1]
    assert result.reason is GateReason.NEWS_EVIDENCE_INVALID


def test_counted_article_without_classification_is_invalid() -> None:
    with _session() as session:
        _classify(session, answers=_CLEAR)
        _coverage(session, ["art-1", "art-2"])
        result = _resolve(session)[1]
    assert result.reason is GateReason.NEWS_EVIDENCE_INVALID


def test_skipped_article_is_not_missing() -> None:
    with _session() as session:
        _classify(session, answers=_CLEAR)
        _coverage(session, ["art-1", "art-2"], ["art-2"])
        result = _resolve(session)[1]
    assert result.reason is None


def test_article_available_after_cutoff_is_not_selected() -> None:
    with _session() as session:
        late = _classify(
            session, article_id="art-late", available_after=timedelta(days=10, hours=1)
        )
        coverage = _coverage(session, [])
        result = _resolve(session)[1]
    assert result == AdverseNewsGateResult(None, (coverage,), 0)
    assert late not in result.observation_ids


def test_listed_article_available_after_cutoff_is_invalid() -> None:
    # Coverage counts only articles available by T; listing a later one is not excused.
    with _session() as session:
        _classify(session, article_id="art-late", available_after=timedelta(days=10, hours=1))
        _coverage(session, ["art-late"])
        result = _resolve(session)[1]
    assert result.reason is GateReason.NEWS_EVIDENCE_INVALID


def test_other_question_set_version_is_skipped_not_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _session() as session:
        with monkeypatch.context() as patch:
            patch.setattr(
                observations_module,
                "ADVERSE_NEWS_QUESTION_SET_VERSION",
                "adverse-news-questions-v0",
            )
            older = _classify(session, answers=_ADVERSE)
        current = _classify(session, answers=_CLEAR)
        coverage = _coverage(session, ["art-1"])
        result = _resolve(session)[1]
    assert result == AdverseNewsGateResult(None, tuple(sorted((current, coverage))), 0)
    assert older not in result.observation_ids


def test_reclassification_under_new_version_does_not_hide_original_on_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newer_version = "adverse-news-questions-v2"
    with _session() as session:
        original = _classify(session, answers=_ADVERSE)
        with monkeypatch.context() as patch:
            patch.setattr(observations_module, "ADVERSE_NEWS_QUESTION_SET_VERSION", newer_version)
            newer = _classify(session, answers=_CLEAR)
        coverage = _coverage(session, ["art-1"])
        replayed = _resolve(session)[1]
        upgraded = _resolve(session, policy=replace(_POLICY, question_set_version=newer_version))[1]
    assert replayed.reason is GateReason.MATERIAL_ADVERSE_NEWS
    assert replayed.observation_ids == tuple(sorted((original, coverage)))
    assert upgraded.reason is None
    assert upgraded.observation_ids == tuple(sorted((newer, coverage)))


def _rewrite(
    session: Session,
    original_id: str,
    *,
    available_at: datetime,
    overrides: dict[str, object],
    revision: int,
    supersedes: str | None,
    record_identity: str | None = None,
) -> str:
    """Write a variant of a stored classification directly through the observation writer.

    Task 5 has no correction API and its persist function only accepts the pinned
    model, so corrections and other-model records are built here from stored rows.
    """
    original = session.get(EquityObservation, original_id)
    assert original is not None
    values: list[EquityObservationValueInput] = []
    for row in session.scalars(
        sa.select(EquityObservationValue).where(
            EquityObservationValue.observation_id == original_id
        )
    ):
        value: object
        if row.field_name == "source_available_at":
            value = available_at
        elif (row.field_name, row.ordinal) in {(key, 0) for key in overrides}:
            value = overrides[row.field_name]
        else:
            value = {
                "decimal": row.decimal_value,
                "integer": row.integer_value,
                "text": row.text_value,
                "boolean": row.boolean_value,
                "timestamp": (
                    row.timestamp_value.replace(tzinfo=UTC)
                    if row.timestamp_value is not None and row.timestamp_value.tzinfo is None
                    else row.timestamp_value
                ),
            }[row.value_type]
        values.append(
            EquityObservationValueInput(row.field_name, row.value_type, value, row.ordinal)  # type: ignore[arg-type]
        )
    ordered = tuple(sorted(values, key=lambda item: (item.field_name, item.ordinal)))
    lineage = _lineage(ADVERSE_NEWS_GATE_CONTRACT, "article/art-1")
    written = persist_equity_observation(
        session,
        EquityObservationSubmission(
            provider=lineage.provider,
            product=lineage.product,
            endpoint=lineage.endpoint,
            dataset_version=lineage.dataset_version,
            tool_version=lineage.tool_version,
            source_identity=lineage.source_identity,
            source_revision=f"source-content-sha256-rewrite-{revision}",
            retrieved_at=available_at,
            timestamp_semantics=lineage.timestamp_semantics,
            adjustment_policy="not-applicable",
            entitlement_scope=lineage.entitlement_scope,
            entitlement_owner_user_id=None,
            missing_data_policy="fail-closed",
            artifact_content_sha256="e" * 64,
            instrument_id=1,
            observation_kind="news_event",
            source_record_identity=record_identity or str(original.source_record_identity),
            event_at=_PUBLISHED,
            available_at=available_at,
            disposition="observed",
            normalized_content_sha256=canonical_json_hash([item.payload() for item in ordered]),
            values=ordered,
            revision=revision,
            supersedes_observation_id=supersedes,
        ),
    )
    session.commit()
    return str(written.observation_id)


def _correct(session: Session, original_id: str, *, available_at: datetime) -> str:
    """Write a provider correction (revision 2) whose answers are no longer adverse."""
    return _rewrite(
        session,
        original_id,
        available_at=available_at,
        overrides={"negative_development_probability": Decimal("0.125")},
        revision=2,
        supersedes=original_id,
    )


def test_provider_correction_after_cutoff_keeps_original() -> None:
    with _session() as session:
        original = _classify(session, answers=_ADVERSE)
        session.commit()
        correction = _correct(session, original, available_at=_CUTOFF + timedelta(days=1))
        coverage = _coverage(session, ["art-1"])
        result = _resolve(session)[1]
    assert correction != original
    assert result.reason is GateReason.MATERIAL_ADVERSE_NEWS
    assert result.observation_ids == tuple(sorted((original, coverage)))
    assert correction not in result.observation_ids


def test_provider_correction_before_cutoff_replaces_original() -> None:
    with _session() as session:
        original = _classify(session, answers=_ADVERSE)
        session.commit()
        correction = _correct(session, original, available_at=_CUTOFF - timedelta(days=1))
        coverage = _coverage(session, ["art-1"])
        result = _resolve(session)[1]
    assert result.reason is None
    assert result.observation_ids == tuple(sorted((correction, coverage)))
    assert original not in result.observation_ids


def test_other_model_version_is_skipped() -> None:
    with _session() as session:
        pinned = _classify(session, answers=_CLEAR)
        session.commit()
        other = _rewrite(
            session,
            pinned,
            available_at=_PUBLISHED + timedelta(minutes=2),
            overrides={
                "model_version": "1.15.0",
                "negative_development_probability": Decimal("0.875"),
            },
            revision=1,
            supersedes=None,
            record_identity=f"art-1|1|{_POLICY.question_set_version}|1.15.0",
        )
        coverage = _coverage(session, ["art-1"])
        replayed = _resolve(session)[1]
        upgraded = _resolve(session, policy=replace(_POLICY, model_version="1.15.0"))[1]
    assert replayed == AdverseNewsGateResult(None, tuple(sorted((pinned, coverage))), 0)
    assert other not in replayed.observation_ids
    assert upgraded.reason is GateReason.MATERIAL_ADVERSE_NEWS
    assert upgraded.observation_ids == tuple(sorted((other, coverage)))


def test_other_version_correction_does_not_excuse_missing_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reviewer probe: art-1 has only a v0 record and a v0 correction available after T.
    with _session() as session:
        with monkeypatch.context() as patch:
            patch.setattr(
                observations_module,
                "ADVERSE_NEWS_QUESTION_SET_VERSION",
                "adverse-news-questions-v0",
            )
            older = _classify(session, answers=_ADVERSE)
        session.commit()
        _correct(session, older, available_at=_CUTOFF + timedelta(days=1))
        _coverage(session, ["art-1"])
        result = _resolve(session)[1]
    assert result.reason is GateReason.NEWS_EVIDENCE_INVALID


def test_column_value_mismatch_excluded_by_prefilter_fails_closed() -> None:
    # The writer cannot produce a column/value mismatch, so the column is updated
    # directly: the stored source_available_at value stays before T, the column says after.
    with _session() as session:
        classification = _classify(session, answers=_CLEAR)
        session.commit()
        session.execute(
            sa.update(EquityObservation)
            .where(EquityObservation.observation_id == classification)
            .values(available_at=(_CUTOFF + timedelta(days=1)).replace(tzinfo=None))
        )
        _coverage(session, ["art-1"])
        result = _resolve(session)[1]
    assert result.reason is GateReason.NEWS_EVIDENCE_INVALID
    assert classification not in result.observation_ids


def test_article_outside_window_is_skipped() -> None:
    with _session() as session:
        stale = _classify(session, article_id="art-old", published_at=_CUTOFF - timedelta(days=91))
        coverage = _coverage(session, [])
        result = _resolve(session)[1]
    assert result == AdverseNewsGateResult(None, (coverage,), 0)
    assert stale not in result.observation_ids


def test_cutoff_must_be_timezone_aware() -> None:
    with _session() as session, pytest.raises(ValueError, match="timezone-aware"):
        resolve_quality_compounder_adverse_news_gate(
            session,
            cutoff=datetime(2026, 12, 31, 21),
            instrument_ids=(1,),
            policy=_POLICY,
            provider_authority_policy=_authority(),
        )
