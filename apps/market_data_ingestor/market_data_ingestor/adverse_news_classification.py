"""Classify articles about instruments with TypeSafe and record query coverage.

Per (article, instrument) the job filters in code, asks TypeSafe, and persists the
answer in its own transaction. Coverage is written only when every article of an
instrument's batch was classified or skipped, so a partial run can never be mistaken
for a complete one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lib_application.db.models import EquityObservation
from lib_application.services.adverse_news_observations import (
    AdverseNewsPersistenceError,
    ProviderLineage,
    adverse_news_record_identity,
    persist_adverse_news_classification,
    persist_news_coverage,
)
from lib_common.logging import get_logger
from lib_strategy.adverse_news_questions import (
    ADVERSE_NEWS_MODEL,
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
_MODEL_VERSION = ADVERSE_NEWS_MODEL.rsplit("-", 1)[1]

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
    provider_query_identity: str
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class ClassificationRunReport:
    """Outcome of one classification run."""

    classified: int
    skipped: Mapping[str, int]
    failed_instruments: tuple[int, ...]
    covered_instruments: tuple[int, ...]


def _already_classified(session: Session, *, article_id: str, instrument_id: int) -> bool:
    identity = adverse_news_record_identity(
        provider_article_id=article_id,
        instrument_id=instrument_id,
        model_version=_MODEL_VERSION,
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


def _classify_article(
    session_factory: Callable[[], Session],
    client: TypeSafeClient,
    batch: InstrumentNewsBatch,
    article: AdverseNewsArticle,
) -> bool:
    """Classify one article; return False when it was already classified."""
    with session_factory() as session:
        if _already_classified(
            session, article_id=article.provider_article_id, instrument_id=batch.instrument_id
        ):
            return False
    state, truncated = build_adverse_news_state(batch.company, article)
    exchange = client.ask(adverse_news_request_body(state))
    with session_factory() as session, session.begin():
        persist_adverse_news_classification(
            session,
            lineage=batch.lineage,
            instrument_id=batch.instrument_id,
            article=article,
            body_truncated=truncated,
            exchange=exchange,
        )
    return True


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

    for batch in batches:
        skipped_ids: list[str] = []
        batch_failed = False
        for article in batch.articles:
            article_id = article.provider_article_id
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
                if _classify_article(session_factory, client, batch, article):
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
                    returned_article_ids=[a.provider_article_id for a in batch.articles],
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
    )


__all__ = ["ClassificationRunReport", "InstrumentNewsBatch", "classify_adverse_news"]
