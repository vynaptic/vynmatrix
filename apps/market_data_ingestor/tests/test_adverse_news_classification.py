"""Adverse-news classification job tests with a fake TypeSafe client and SQLite."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

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
)
from lib_strategy.adverse_news_questions import (
    ADVERSE_NEWS_MODEL,
    AdverseNewsArticle,
    AdverseNewsCompany,
)
from lib_strategy.equity_optional_factors import (
    ADVERSE_NEWS_GATE_CONTRACT,
    OPTIONAL_FACTOR_TIMESTAMP_SCHEMA,
)
from market_data_ingestor import adverse_news_classification as classification
from market_data_ingestor.adverse_news_classification import (
    ClassificationRunReport,
    InstrumentNewsBatch,
    classify_adverse_news,
)
from market_data_ingestor.typesafe_client import TypeSafeRequestError

_PUBLISHED = datetime(2025, 1, 20, 14, tzinfo=UTC)
_RECEIVED = datetime(2025, 1, 21, 9, tzinfo=UTC)
_START = datetime(2024, 10, 22, tzinfo=UTC)
_END = datetime(2025, 1, 21, tzinfo=UTC)


def _factory() -> sessionmaker[Session]:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)

    @sa.event.listens_for(engine, "connect")
    def _fk(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session, session.begin():
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
    return factory


def _lineage() -> ProviderLineage:
    contract = ADVERSE_NEWS_GATE_CONTRACT
    return ProviderLineage(
        provider="licensed-archive",
        product="historical-news_event",
        endpoint="https://provider.invalid/news",
        dataset_version="news-2025-r3",
        tool_version="adverse-news-adapter-v1",
        source_identity="batch",
        source_revision="rev-1",
        entitlement_scope="personal-research-news-pit",
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


def _article(
    article_id: str, *, language: str = "en", available_at: datetime | None = None
) -> AdverseNewsArticle:
    return AdverseNewsArticle(
        provider_article_id=article_id,
        headline=f"Headline {article_id}",
        source="wire",
        body="The company recalled a product.",
        language=language,
        document_type="news",
        published_at=_PUBLISHED,
        source_available_at=available_at or _PUBLISHED + timedelta(minutes=2),
    )


def _batch(instrument_id: int, *articles: AdverseNewsArticle) -> InstrumentNewsBatch:
    return InstrumentNewsBatch(
        instrument_id=instrument_id,
        company=AdverseNewsCompany(
            name="Acme", ticker="AAA", sector="Industrials", industry="Machinery"
        ),
        articles=tuple(articles),
        lineage=_lineage(),
        query_window_start=_START,
        query_window_end=_END,
        cutoff=_END,
        provider_query_identity=f"query-{instrument_id}",
        completed_at=_RECEIVED,
    )


def _response() -> dict[str, object]:
    def noul(value: float) -> dict[str, object]:
        return {"type": "noul", "noul": value}

    return {
        "model": ADVERSE_NEWS_MODEL,
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
    }


_USAGE = itertools.count()


class _FakeClient:
    def __init__(
        self,
        fail_on: Callable[[int], bool] = lambda _n: False,
        model: str = ADVERSE_NEWS_MODEL,
        *,
        varying: bool = False,
    ) -> None:
        self._model = model
        self.requests: list[dict[str, object]] = []
        self._fail_on = fail_on
        # TypeSafe is not deterministic: a varying client never repeats a response.
        self._varying = varying

    def ask(self, request_json: dict[str, object]) -> TypeSafeExchange:
        self.requests.append(request_json)
        if self._fail_on(len(self.requests)):
            raise TypeSafeRequestError("TypeSafe returned HTTP 500")
        request_bytes = json.dumps(request_json, separators=(",", ":")).encode()
        response = _response()
        response["model"] = self._model
        if self._varying:
            response["usage"] = {"input_tokens": next(_USAGE)}
        return TypeSafeExchange(
            request_json=request_json,
            response_json=response,
            request_bytes=request_bytes,
            response_bytes=json.dumps(response).encode(),
            received_at=_RECEIVED,
        )


def _kinds(factory: sessionmaker[Session]) -> list[str]:
    with factory() as session:
        return list(session.scalars(sa.select(EquityObservation.observation_kind)))


def _run(
    factory: sessionmaker[Session], client: _FakeClient, *batches: InstrumentNewsBatch
) -> ClassificationRunReport:
    return classify_adverse_news(factory, client=client, batches=batches)  # type: ignore[arg-type]


def test_skipped_articles_never_reach_client() -> None:
    factory = _factory()
    client = _FakeClient()
    report = _run(factory, client, _batch(1, _article("a", language="de"), _article("b")))
    assert len(client.requests) == 1
    assert report.classified == 1
    assert sum(report.skipped.values()) == 1


def test_failed_answer_writes_nothing_for_that_article() -> None:
    factory = _factory()
    client = _FakeClient(model="jev-1.13.0")
    report = _run(factory, client, _batch(1, _article("a")))
    assert report.classified == 0
    assert report.failed_instruments == (1,)
    assert report.covered_instruments == ()
    assert len(client.requests) == 1
    assert _kinds(factory) == []
    with factory() as session:
        assert (
            session.scalar(sa.select(sa.func.count()).select_from(EquityModelInferenceRecord)) == 0
        )


def test_partial_run_writes_no_coverage() -> None:
    factory = _factory()
    client = _FakeClient(fail_on=lambda n: n == 2)
    report = _run(factory, client, _batch(1, _article("a"), _article("b"), _article("c")))
    assert report.failed_instruments == (1,)
    assert report.covered_instruments == ()
    assert report.classified == 2
    assert len(client.requests) == 3
    assert "news_coverage" not in _kinds(factory)
    assert _kinds(factory).count("news_event") == 2


def test_complete_batch_writes_coverage_with_all_article_ids() -> None:
    factory = _factory()
    client = _FakeClient()
    report = _run(factory, client, _batch(1, _article("a"), _article("b", language="de")))
    assert report.covered_instruments == (1,)
    assert report.failed_instruments == ()
    with factory() as session:
        coverage_id = session.scalar(
            sa.select(EquityObservation.observation_id).where(
                EquityObservation.observation_kind == "news_coverage"
            )
        )

        def texts(field: str) -> list[str]:
            return [
                str(v.text_value)
                for v in session.scalars(
                    sa.select(EquityObservationValue).where(
                        EquityObservationValue.observation_id == coverage_id,
                        EquityObservationValue.field_name == field,
                    )
                )
            ]

        assert sorted(texts("returned_article_identity")) == ["a", "b"]
        assert texts("skipped_article_identity") == ["b"]


def test_already_classified_article_is_not_requested_again() -> None:
    factory = _factory()
    batch = _batch(1, _article("a"))
    first = _FakeClient()
    _run(factory, first, batch)
    second = _FakeClient()
    report = _run(factory, second, _batch(1, _article("a")))
    assert second.requests == []
    assert report.classified == 1
    assert report.failed_instruments == ()
    assert report.covered_instruments == (1,)


def test_failure_in_one_instrument_does_not_block_others() -> None:
    factory = _factory()
    client = _FakeClient(fail_on=lambda n: n == 1)
    report = _run(factory, client, _batch(1, _article("a")), _batch(2, _article("b")))
    assert report.failed_instruments == (1,)
    assert report.covered_instruments == (2,)


def test_coverage_write_failure_marks_instrument_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: object, **_k: object) -> str:
        raise AdverseNewsPersistenceError("coverage rejected")

    monkeypatch.setattr(classification, "persist_news_coverage", boom)
    factory = _factory()
    report = _run(factory, _FakeClient(), _batch(1, _article("a")))
    assert report.failed_instruments == (1,)
    assert report.covered_instruments == ()
    assert report.classified == 1
    assert "news_coverage" not in _kinds(factory)


def _texts(factory: sessionmaker[Session], field: str) -> list[str]:
    with factory() as session:
        coverage_id = session.scalar(
            sa.select(EquityObservation.observation_id).where(
                EquityObservation.observation_kind == "news_coverage"
            )
        )
        assert coverage_id is not None
        return sorted(
            str(v.text_value)
            for v in session.scalars(
                sa.select(EquityObservationValue).where(
                    EquityObservationValue.observation_id == coverage_id,
                    EquityObservationValue.field_name == field,
                )
            )
        )


def test_identical_text_under_two_article_ids_reuses_the_recorded_exchange() -> None:
    factory = _factory()
    first = replace(_article("a1"), headline="Same wire story")
    second = replace(_article("a2"), headline="Same wire story")
    client = _FakeClient(varying=True)
    report = _run(factory, client, _batch(1, first, second))
    assert len(client.requests) == 1
    assert report.classified == 2
    assert report.failed_instruments == ()
    assert report.covered_instruments == (1,)
    assert _kinds(factory).count("news_event") == 2
    assert _texts(factory, "returned_article_identity") == ["a1", "a2"]
    with factory() as session:
        assert (
            session.scalar(sa.select(sa.func.count()).select_from(EquityModelInferenceRecord)) == 1
        )
    rerun = _FakeClient(varying=True)
    assert _run(factory, rerun, _batch(1, first, second)).covered_instruments == (1,)
    assert rerun.requests == []


def test_articles_available_after_cutoff_are_neither_classified_nor_listed() -> None:
    factory = _factory()
    client = _FakeClient()
    late = _article("late", available_at=_END + timedelta(seconds=1))
    report = _run(factory, client, _batch(1, _article("a"), late))
    assert len(client.requests) == 1
    assert report.classified == 1
    assert report.excluded_after_cutoff == 1
    assert report.covered_instruments == (1,)
    assert _texts(factory, "returned_article_identity") == ["a"]


def test_batch_window_must_end_at_cutoff() -> None:
    with pytest.raises(ValueError, match="cutoff"):
        replace(_batch(1), cutoff=_END - timedelta(hours=1))


def test_batch_cutoff_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(_batch(1), cutoff=_END.replace(tzinfo=None))
