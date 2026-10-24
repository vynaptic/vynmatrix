"""Persist adverse-news classifications and news-coverage records.

One TypeSafe exchange about one (article, instrument) becomes an immutable
``news_event`` observation plus an append-only inference record. One completed
provider query becomes a ``news_coverage`` observation. Both go through
``persist_equity_observation``; nothing here bypasses its write-once checks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, NoReturn

from sqlalchemy.orm import Session

from lib_application.db.models import EquityModelInferenceRecord, EquityObservation
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
    EquityObservationWriteError,
    persist_equity_observation,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.adverse_news_questions import (
    ADVERSE_NEWS_MODEL,
    ADVERSE_NEWS_MODEL_VERSION,
    ADVERSE_NEWS_QUESTION_SET_VERSION,
    ADVERSE_NEWS_QUESTIONS,
    AdverseNewsAnswers,
    AdverseNewsArticle,
    inference_configuration_sha256,
    model_artifact_sha256,
    output_schema_sha256,
    prompt_schema_sha256,
    split_model_id,
)

_MODEL_PROVIDER: Final = "typesafe"
_ADJUSTMENT_POLICY: Final = "not-applicable"
_MISSING_DATA_POLICY: Final = "fail-closed"
_NEWS_EVENT_KIND: Final = "news_event"
_NEWS_COVERAGE_KIND: Final = "news_coverage"
_DISPOSITION: Final = "observed"
NEWS_COVERAGE_FACTOR_VERSION: Final = "news-coverage-v1"
# Matches the Numeric(38, 18) column so stored, hashed and replayed values agree.
_DECIMAL_QUANTUM: Final = Decimal(1).scaleb(-18)


class AdverseNewsPersistenceError(RuntimeError):
    """Adverse-news evidence cannot be written or replayed safely."""


def _invalid(message: str) -> NoReturn:
    raise AdverseNewsPersistenceError(message)


@dataclass(frozen=True, slots=True)
class ProviderLineage:
    """Provider-supplied lineage fields shared by every record from one artifact."""

    provider: str
    product: str
    endpoint: str
    dataset_version: str
    tool_version: str
    source_identity: str
    source_revision: str
    entitlement_scope: str
    entitlement_owner_user_id: str | None
    retrieved_at: datetime
    timestamp_semantics: Mapping[str, object]
    artifact_content_sha256: str


@dataclass(frozen=True, slots=True)
class TypeSafeExchange:
    """The exact request and response of one TypeSafe call."""

    request_json: Mapping[str, object]
    response_json: Mapping[str, object]
    request_bytes: bytes
    response_bytes: bytes
    received_at: datetime


def adverse_news_record_identity(
    *, provider_article_id: str, instrument_id: int, model_version: str
) -> str:
    """Return the write-once record identity for one (article, instrument, model)."""
    return (
        f"{provider_article_id}|{instrument_id}|{ADVERSE_NEWS_QUESTION_SET_VERSION}|{model_version}"
    )


def _submission(
    lineage: ProviderLineage,
    *,
    instrument_id: int,
    observation_kind: str,
    source_record_identity: str,
    event_at: datetime,
    available_at: datetime,
    values: Sequence[EquityObservationValueInput],
) -> EquityObservationSubmission:
    ordered = tuple(sorted(values, key=lambda item: (item.field_name, item.ordinal)))
    return EquityObservationSubmission(
        provider=lineage.provider,
        product=lineage.product,
        endpoint=lineage.endpoint,
        dataset_version=lineage.dataset_version,
        tool_version=lineage.tool_version,
        source_identity=lineage.source_identity,
        source_revision=lineage.source_revision,
        retrieved_at=lineage.retrieved_at,
        timestamp_semantics=lineage.timestamp_semantics,
        adjustment_policy=_ADJUSTMENT_POLICY,
        entitlement_scope=lineage.entitlement_scope,
        entitlement_owner_user_id=lineage.entitlement_owner_user_id,
        missing_data_policy=_MISSING_DATA_POLICY,
        artifact_content_sha256=lineage.artifact_content_sha256,
        instrument_id=instrument_id,
        observation_kind=observation_kind,
        source_record_identity=source_record_identity,
        event_at=event_at,
        available_at=available_at,
        disposition=_DISPOSITION,
        normalized_content_sha256=canonical_json_hash([item.payload() for item in ordered]),
        values=ordered,
    )


def _write(session: Session, submission: EquityObservationSubmission) -> str:
    try:
        observation = persist_equity_observation(session, submission)
    except EquityObservationWriteError as exc:
        raise AdverseNewsPersistenceError(str(exc)) from exc
    return str(observation.observation_id)


def _text(field: str, value: str) -> EquityObservationValueInput:
    return EquityObservationValueInput(field, "text", value)


def _decimal(field: str, probability: float, ordinal: int = 0) -> EquityObservationValueInput:
    return EquityObservationValueInput(
        field, "decimal", Decimal(str(probability)).quantize(_DECIMAL_QUANTUM), ordinal
    )


def _timestamp(field: str, value: datetime) -> EquityObservationValueInput:
    return EquityObservationValueInput(field, "timestamp", value)


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _split_model(model: object) -> tuple[str, str]:
    model_name, model_version = split_model_id(model)
    if model_version != ADVERSE_NEWS_MODEL_VERSION:
        _invalid(f"model version {model_version!r} is not {ADVERSE_NEWS_MODEL_VERSION!r}")
    return model_name, model_version


def _matches_bytes(raw: bytes, parsed: Mapping[str, object]) -> bool:
    try:
        return bool(json.loads(raw) == parsed)
    except ValueError:
        return False


def _check_exchange(exchange: TypeSafeExchange) -> None:
    """Require the pinned request and bytes that are exactly the recorded JSON."""
    request = exchange.request_json
    if request.get("model") != ADVERSE_NEWS_MODEL:
        _invalid(f"request model {request.get('model')!r} is not {ADVERSE_NEWS_MODEL!r}")
    if request.get("questions") != ADVERSE_NEWS_QUESTIONS:
        _invalid("request questions are not the pinned adverse-news questions")
    if not _matches_bytes(exchange.request_bytes, request):
        _invalid("request bytes do not match the request JSON")
    if not _matches_bytes(exchange.response_bytes, exchange.response_json):
        _invalid("response bytes do not match the response JSON")


def _record_inference(
    session: Session,
    *,
    lineage: ProviderLineage,
    lineage_id: str,
    exchange: TypeSafeExchange,
    input_sha256: str,
    output_sha256: str,
) -> None:
    existing = session.get(EquityModelInferenceRecord, input_sha256)
    if existing is not None:
        if str(existing.model_output_sha256) != output_sha256:
            _invalid("model input already has a different recorded output")
        return
    session.add(
        EquityModelInferenceRecord(
            model_input_sha256=input_sha256,
            lineage_id=lineage_id,
            model_output_sha256=output_sha256,
            request_json=dict(exchange.request_json),
            response_json=dict(exchange.response_json),
            request_bytes=exchange.request_bytes,
            response_bytes=exchange.response_bytes,
            received_at=exchange.received_at,
            entitlement_scope=lineage.entitlement_scope,
            entitlement_owner_user_id=lineage.entitlement_owner_user_id,
        )
    )
    session.flush()


def persist_adverse_news_classification(
    session: Session,
    *,
    lineage: ProviderLineage,
    instrument_id: int,
    article: AdverseNewsArticle,
    body_truncated: bool,
    exchange: TypeSafeExchange,
) -> str:
    """Write one classification and its inference record; return the observation id."""
    answers = AdverseNewsAnswers.from_response(exchange.response_json)
    _check_exchange(exchange)
    model = exchange.response_json["model"]
    model_name, model_version = _split_model(model)
    input_sha256 = canonical_json_hash(exchange.request_json)
    output_sha256 = canonical_json_hash(exchange.response_json)

    existing = session.get(EquityModelInferenceRecord, input_sha256)
    if existing is not None and str(existing.model_output_sha256) != output_sha256:
        _invalid("model input already has a different recorded output")

    values = [
        _decimal("about_company_probability", answers.about_company),
        EquityObservationValueInput("body_truncated", "boolean", bool(body_truncated)),
        _timestamp("classified_at", exchange.received_at),
        _text("document_type", article.document_type),
        _decimal("entity_relevance", answers.about_company),
        _text("inference_configuration_sha256", inference_configuration_sha256()),
        _text("language", article.language),
        _text("model_artifact_sha256", model_artifact_sha256(str(model))),
        _text("model_input_sha256", input_sha256),
        _text("model_name", model_name),
        _text("model_output_sha256", output_sha256),
        _text("model_provider", _MODEL_PROVIDER),
        _text("model_version", model_version),
        _decimal("negative_confirmed_probability", answers.negative_confirmed),
        _decimal("negative_development_probability", answers.negative_development),
        *(
            _decimal("negative_severity_probability", probability, ordinal)
            for ordinal, probability in enumerate(answers.negative_severity)
        ),
        _decimal("new_development_probability", answers.new_development),
        _timestamp("original_published_at", article.published_at),
        _text("output_schema_sha256", output_schema_sha256()),
        _decimal("price_recap_probability", answers.price_recap),
        _text("prompt_or_feature_schema_sha256", prompt_schema_sha256()),
        _text("question_set_version", ADVERSE_NEWS_QUESTION_SET_VERSION),
        _timestamp("source_available_at", article.source_available_at),
    ]
    submission = _submission(
        lineage,
        instrument_id=instrument_id,
        observation_kind=_NEWS_EVENT_KIND,
        source_record_identity=adverse_news_record_identity(
            provider_article_id=article.provider_article_id,
            instrument_id=instrument_id,
            model_version=model_version,
        ),
        event_at=article.published_at,
        available_at=article.source_available_at,
        values=values,
    )
    observation_id = _write(session, submission)
    persisted = session.get(EquityObservation, observation_id)
    if persisted is None:
        _invalid("persisted observation is missing")
    _record_inference(
        session,
        lineage=lineage,
        lineage_id=str(persisted.lineage_id),
        exchange=exchange,
        input_sha256=input_sha256,
        output_sha256=output_sha256,
    )
    return observation_id


def persist_news_coverage(
    session: Session,
    *,
    lineage: ProviderLineage,
    instrument_id: int,
    query_window_start: datetime,
    query_window_end: datetime,
    completed_at: datetime,
    provider_query_identity: str,
    returned_article_ids: Sequence[str],
    skipped_article_ids: Sequence[str],
) -> str:
    """Write one completed provider query as a coverage observation."""
    returned = tuple(sorted(returned_article_ids))
    skipped = tuple(sorted(skipped_article_ids))
    if len(set(returned)) != len(returned) or len(set(skipped)) != len(skipped):
        _invalid("coverage article identities must be unique")
    if not set(skipped) <= set(returned):
        _invalid("skipped articles must be a subset of returned articles")
    query_window_start = _aware_utc(query_window_start, "query_window_start")
    query_window_end = _aware_utc(query_window_end, "query_window_end")
    completed_at = _aware_utc(completed_at, "completed_at")
    if query_window_end <= query_window_start:
        _invalid("query window must end after it starts")
    if completed_at < query_window_end:
        _invalid("completed_at cannot precede query_window_end")
    values = [
        _text("provider_query_identity", provider_query_identity),
        _timestamp("query_window_end", query_window_end),
        _timestamp("query_window_start", query_window_start),
        EquityObservationValueInput("returned_article_count", "integer", len(returned)),
        _timestamp("source_available_at", completed_at),
        *(
            EquityObservationValueInput("returned_article_identity", "text", identity, ordinal)
            for ordinal, identity in enumerate(returned)
        ),
        *(
            EquityObservationValueInput("skipped_article_identity", "text", identity, ordinal)
            for ordinal, identity in enumerate(skipped)
        ),
    ]
    submission = _submission(
        lineage,
        instrument_id=instrument_id,
        observation_kind=_NEWS_COVERAGE_KIND,
        source_record_identity=(
            f"{provider_query_identity}|{instrument_id}|"
            f"{query_window_start.isoformat()}|{query_window_end.isoformat()}"
        ),
        event_at=query_window_end,
        available_at=completed_at,
        values=values,
    )
    return _write(session, submission)


__all__ = [
    "NEWS_COVERAGE_FACTOR_VERSION",
    "AdverseNewsPersistenceError",
    "ProviderLineage",
    "TypeSafeExchange",
    "adverse_news_record_identity",
    "persist_adverse_news_classification",
    "persist_news_coverage",
]
