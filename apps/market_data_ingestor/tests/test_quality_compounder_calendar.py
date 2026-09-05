"""Focused tests for the prospective shared official XNYS calendar boundary."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dev_cli.validation.backtest.nyse_official_sessions import (
    CompiledNYSEOfficialSessionArtifact,
)
from lib_application.db.models import (
    Base,
    EquityObservation,
    EquityObservationValue,
    EquitySourceLineage,
    Instrument,
    MarketCalendar,
    MarketSession,
)
from lib_common.hashing import canonical_json_bytes
from market_data_ingestor.quality_compounder_calendar import (
    QualityCompounderCalendarError,
    load_quality_compounder_calendar_artifact,
    parse_quality_compounder_calendar_artifact,
    persist_quality_compounder_calendar,
)

_RETRIEVED_AT = datetime(2023, 11, 10, 18, tzinfo=UTC)
_REVISION = "a" * 64
_ROLES = (
    "calendar_2018_2020",
    "calendar_2021_2023",
    "calendar_2022_2024_juneteenth_revision",
    "calendar_2024_2026",
    "bush_national_day_of_mourning_2018",
    "carter_national_day_of_mourning_2025",
)


def _compiled_artifact() -> CompiledNYSEOfficialSessionArtifact:
    source_content = b"%PDF-1.7 deterministic official-source fixture"
    source_sha256 = hashlib.sha256(source_content).hexdigest()
    payload = {
        "calendar_evidence": {
            "closure_dates": [],
            "early_close_dates": [],
            "regular_session_cross_check": {
                "library": "exchange_calendars",
                "library_version": "4.11.2",
                "mic": "XNYS",
            },
        },
        "coverage_complete": True,
        "coverage_from": "2024-01-01",
        "coverage_to": "2024-01-03",
        "dataset": "ICE/NYSE cash-equity holidays, early closes, and special closures",
        "dataset_version": f"official-source-set-{_REVISION[:16]}",
        "entitlement_scope": "public official exchange publications",
        "provider": "Intercontinental Exchange / NYSE Group",
        "retrieved_at": _RETRIEVED_AT.isoformat(),
        "schema": "vynmatrix.historical-market-sessions.v1",
        "sessions": [
            {
                "closes_at": "2024-01-02T21:00:00+00:00",
                "opens_at": "2024-01-02T14:30:00+00:00",
                "session_date": "2024-01-02",
            },
            {
                "closes_at": "2024-01-03T21:00:00+00:00",
                "opens_at": "2024-01-03T14:30:00+00:00",
                "session_date": "2024-01-03",
            },
        ],
        "source_documents": [
            {
                "content_base64": base64.b64encode(source_content).decode("ascii"),
                "content_sha256": source_sha256,
                "content_type": "application/pdf",
                "publisher": "Intercontinental Exchange / NYSE Group",
                "role": role,
                "url": f"https://s2.q4cdn.com/154085107/files/doc_news/{role}.pdf",
            }
            for role in _ROLES
        ],
        "source_kind": "exchange",
        "source_reference": "https://www.nyse.com/markets/hours-calendars",
        "source_revision": _REVISION,
        "timestamp_semantics": "official XNYS regular-session UTC conversion",
        "venue": "XNYS",
    }
    content = canonical_json_bytes(payload)
    return CompiledNYSEOfficialSessionArtifact(
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
        coverage_from=date(2024, 1, 1),
        coverage_to=date(2024, 1, 3),
        session_count=2,
        source_revision=_REVISION,
        retrieved_at=_RETRIEVED_AT,
    )


def _engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def test_persists_generic_calendar_observation_and_exactly_replays_shared_xnys() -> None:
    engine = _engine()
    artifact = parse_quality_compounder_calendar_artifact(_compiled_artifact())

    with Session(engine) as session, session.begin():
        first = persist_quality_compounder_calendar(session, artifact)
        calendar_id = int(first.calendar_id)
        observation_id = str(first.observation_id)

    with Session(engine) as session, session.begin():
        replay = persist_quality_compounder_calendar(session, artifact)
        assert int(replay.calendar_id) == calendar_id
        assert str(replay.observation_id) == observation_id

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(EquitySourceLineage)) == 1
        assert session.scalar(select(func.count()).select_from(EquityObservation)) == 1
        assert session.scalar(select(func.count()).select_from(EquityObservationValue)) == 6
        assert session.scalar(select(func.count()).select_from(MarketCalendar)) == 1
        assert session.scalar(select(func.count()).select_from(MarketSession)) == 2
        assert session.scalar(select(func.count()).select_from(Instrument)) == 0

        lineage = session.scalar(select(EquitySourceLineage))
        assert lineage is not None
        assert lineage.provider == "ice_nyse"
        assert lineage.entitlement_scope == "public-official-exchange-publications"
        assert lineage.entitlement_owner_user_id is None
        assert lineage.content_sha256 == artifact.content_sha256
        assert lineage.source_revision == artifact.source_revision
        assert _utc(lineage.retrieved_at) == artifact.retrieved_at

        observation = session.get(EquityObservation, observation_id)
        assert observation is not None
        assert observation.observation_kind == "calendar"
        assert observation.instr_id is None
        assert observation.source_record_identity == artifact.source_reference
        assert _utc(observation.available_at) == artifact.retrieved_at

        calendar = session.get(MarketCalendar, calendar_id)
        assert calendar is not None
        assert calendar.code == "XNYS"
        assert calendar.source_kind == "exchange"
        assert calendar.provider == "ice_nyse"
        assert calendar.observation_id == observation_id
        windows = tuple(
            session.execute(
                select(MarketSession.opens_at, MarketSession.closes_at)
                .where(MarketSession.calendar_id == calendar_id)
                .order_by(MarketSession.opens_at)
            ).all()
        )
        assert tuple((_utc(opens_at), _utc(closes_at)) for opens_at, closes_at in windows) == tuple(
            (item.opens_at, item.closes_at) for item in artifact.sessions
        )


def test_parser_fails_when_compiler_metadata_differs_from_canonical_content() -> None:
    compiled = replace(_compiled_artifact(), source_revision="b" * 64)

    with pytest.raises(QualityCompounderCalendarError, match="source revision differs"):
        parse_quality_compounder_calendar_artifact(compiled)


def test_loader_requires_exact_pinned_compiler_bytes() -> None:
    compiled = _compiled_artifact()

    loaded = load_quality_compounder_calendar_artifact(
        compiled.content,
        expected_sha256=compiled.content_sha256,
    )

    assert loaded.content_sha256 == compiled.content_sha256
    assert loaded.coverage_from == compiled.coverage_from
    with pytest.raises(QualityCompounderCalendarError, match="expected SHA-256"):
        load_quality_compounder_calendar_artifact(
            compiled.content,
            expected_sha256="b" * 64,
        )


def test_existing_incompatible_xnys_calendar_fails_closed() -> None:
    engine = _engine()
    artifact = parse_quality_compounder_calendar_artifact(_compiled_artifact())
    with Session(engine) as session, session.begin():
        session.add(
            MarketCalendar(
                code="XNYS",
                source_kind="broker",
                provider="ibkr",
                source_reference="https://api.ibkr.com/trading-schedule",
            )
        )

    with (
        Session(engine) as session,
        pytest.raises(QualityCompounderCalendarError, match="calendar differs"),
        session.begin(),
    ):
        persist_quality_compounder_calendar(session, artifact)

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(EquityObservation)) == 0
        assert session.scalar(select(func.count()).select_from(MarketSession)) == 0


def test_existing_incompatible_xnys_sessions_fail_closed() -> None:
    engine = _engine()
    artifact = parse_quality_compounder_calendar_artifact(_compiled_artifact())
    with Session(engine) as session, session.begin():
        calendar = persist_quality_compounder_calendar(session, artifact)
        calendar_id = int(calendar.calendar_id)

    with Session(engine) as session, session.begin():
        row = session.scalar(
            select(MarketSession)
            .where(MarketSession.calendar_id == calendar_id)
            .order_by(MarketSession.opens_at)
        )
        assert row is not None
        row.closes_at += timedelta(minutes=1)

    with (
        Session(engine) as session,
        pytest.raises(QualityCompounderCalendarError, match="sessions differ"),
        session.begin(),
    ):
        persist_quality_compounder_calendar(session, artifact)
