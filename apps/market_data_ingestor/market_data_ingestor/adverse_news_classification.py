"""Classify articles about instruments with TypeSafe and record query coverage.

Per (article, instrument) the job filters in code, asks TypeSafe, and persists the
answer in its own transaction. Coverage is written only when every article of an
instrument's batch was classified or skipped, so a partial run can never be mistaken
for a complete one. Articles first available after the batch cutoff are neither
classified nor listed: coverage attests only what was available by the cutoff.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lib_application.db.models import EquityModelInferenceRecord, EquityObservation
from lib_application.services.adverse_news_observations import (
    AdverseNewsPersistenceError,
    ProviderLineage,
    TypeSafeExchange,
    adverse_news_record_identity,
    persist_adverse_news_classification,
    persist_news_coverage,
)
from lib_common.hashing import canonical_json_hash
from lib_common.logging import get_logger
from lib_strategy.adverse_news_questions import (
    ADVERSE_NEWS_MODEL_VERSION,
    AdverseNewsAnswerError,
    AdverseNewsArticle,
    AdverseNewsCompany,
    adverse_news_request_body,
    article_skip_reason,
    build_adverse_news_state,
)
from market_data_ingestor.typesafe_client import TypeSafeClient, TypeSafeRequestError

logger = get_logger(__name__)

_NEWS_EVENT_KIND = "news_event"

_ARTICLE_ERRORS = (
    TypeSafeRequestError,
    AdverseNewsAnswerError,
    AdverseNewsPersistenceError,
    IntegrityError,
)


@dataclass(frozen=True, slots=True)
class InstrumentNewsBatch:
    """The articles one provider query returned for one instrument."""

    instrument_id: int
    company: AdverseNewsCompany
    articles: tuple[AdverseNewsArticle, ...]
    lineage: ProviderLineage
    query_window_start: datetime
    query_window_end: datetime
    cutoff: datetime
    provider_query_identity: str
    completed_at: datetime

    def __post_init__(self) -> None:
        if self.cutoff.tzinfo is None or self.cutoff.utcoffset() is None:
            message = "cutoff must be timezone-aware"
            raise ValueError(message)
        if self.query_window_end != self.cutoff:
            message = "query_window_end must equal the batch cutoff"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ClassificationRunReport:
    """Outcome of one classification run."""

    classified: int
    skipped: Mapping[str, int]
    failed_instruments: tuple[int, ...]
    covered_instruments: tuple[int, ...]
    excluded_after_cutoff: int = 0


def _already_classified(session: Session, *, article_id: str, instrument_id: int) -> bool:
    identity = adverse_news_record_identity(
        provider_article_id=article_id,
        instrument_id=instrument_id,
        model_version=ADVERSE_NEWS_MODEL_VERSION,
    )
    found = session.scalar(
        sa.select(EquityObservation.observation_id)
        .where(
            EquityObservation.observation_kind == _NEWS_EVENT_KIND,
            EquityObservation.instr_id == instrument_id,
            EquityObservation.source_record_identity == identity,
        )
        .limit(1)
    )
    return found is not None


def _recorded_exchange(
    session: Session, request_json: Mapping[str, object]
) -> TypeSafeExchange | None:
    """Return the stored exchange for an identical request, if one was recorded.

    Two provider articles can carry identical text, so their requests hash alike.
    TypeSafe is not deterministic, so asking again would record a conflicting
    output for the same input; the recorded answer is reused instead.
    """
    record = session.get(EquityModelInferenceRecord, canonical_json_hash(request_json))
    if record is None:
        return None
    if not isinstance(record.request_json, dict) or not isinstance(record.response_json, dict):
        message = "recorded TypeSafe exchange is not a pair of JSON objects"
        raise AdverseNewsPersistenceError(message)
    received_at = record.received_at
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        # SQLite drops the offset of a stored UTC instant.
        received_at = received_at.replace(tzinfo=UTC)
    return TypeSafeExchange(
        request_json=dict(record.request_json),
        response_json=dict(record.response_json),
        request_bytes=bytes(record.request_bytes),
        response_bytes=bytes(record.response_bytes),
        received_at=received_at,
    )


def _classify_article(
    session_factory: Callable[[], Session],
    client: TypeSafeClient,
    batch: InstrumentNewsBatch,
    article: AdverseNewsArticle,
) -> None:
    """Classify one article unless it already has a recorded classification."""
    state, truncated = build_adverse_news_state(batch.company, article)
    request_json = adverse_news_request_body(state)
    with session_factory() as session:
        if _already_classified(
            session, article_id=article.provider_article_id, instrument_id=batch.instrument_id
        ):
            return
        exchange = _recorded_exchange(session, request_json)
    if exchange is None:
        exchange = client.ask(request_json)
    else:
        logger.info(
            "adverse_news_recorded_exchange_reused",
            instrument_id=batch.instrument_id,
            provider_article_id=article.provider_article_id,
        )
    with session_factory() as session, session.begin():
        persist_adverse_news_classification(
            session,
            lineage=batch.lineage,
            instrument_id=batch.instrument_id,
            article=article,
            body_truncated=truncated,
            exchange=exchange,
        )


def classify_adverse_news(
    session_factory: Callable[[], Session],
    *,
    client: TypeSafeClient,
    batches: Sequence[InstrumentNewsBatch],
) -> ClassificationRunReport:
    """Classify every unskipped article and cover fully processed instrument batches."""
    classified = 0
    skipped: Counter[str] = Counter()
    failed: list[int] = []
    covered: list[int] = []
    excluded_after_cutoff = 0

    for batch in batches:
        skipped_ids: list[str] = []
        listed_ids: list[str] = []
        batch_failed = False
        for article in batch.articles:
            article_id = article.provider_article_id
            if article.source_available_at > batch.cutoff:
                excluded_after_cutoff += 1
                logger.info(
                    "adverse_news_article_after_cutoff",
                    instrument_id=batch.instrument_id,
                    provider_article_id=article_id,
                )
                continue
            listed_ids.append(article_id)
            reason = article_skip_reason(article)
            if reason is not None:
                skipped[reason] += 1
                skipped_ids.append(article_id)
                logger.info(
                    "adverse_news_article_skipped",
                    instrument_id=batch.instrument_id,
                    provider_article_id=article_id,
                    skip_reason=reason,
                )
                continue
            try:
                _classify_article(session_factory, client, batch, article)
                classified += 1
            except _ARTICLE_ERRORS as exc:
                batch_failed = True
                logger.warning(
                    "adverse_news_article_failed",
                    instrument_id=batch.instrument_id,
                    provider_article_id=article_id,
                    error_class=type(exc).__name__,
                )
        if batch_failed:
            failed.append(batch.instrument_id)
            continue
        try:
            with session_factory() as session, session.begin():
                persist_news_coverage(
                    session,
                    lineage=batch.lineage,
                    instrument_id=batch.instrument_id,
                    query_window_start=batch.query_window_start,
                    query_window_end=batch.query_window_end,
                    completed_at=batch.completed_at,
                    provider_query_identity=batch.provider_query_identity,
                    returned_article_ids=listed_ids,
                    skipped_article_ids=skipped_ids,
                )
        except (AdverseNewsPersistenceError, IntegrityError) as exc:
            failed.append(batch.instrument_id)
            logger.warning(
                "adverse_news_coverage_failed",
                instrument_id=batch.instrument_id,
                error_class=type(exc).__name__,
            )
            continue
        covered.append(batch.instrument_id)

    return ClassificationRunReport(
        classified=classified,
        skipped=dict(skipped),
        failed_instruments=tuple(failed),
        covered_instruments=tuple(covered),
        excluded_after_cutoff=excluded_after_cutoff,
    )


__all__ = ["ClassificationRunReport", "InstrumentNewsBatch", "classify_adverse_news"]
