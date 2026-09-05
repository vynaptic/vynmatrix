"""Tests for the append-only normalized equity evidence writer."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from lib_application.db.models import Base, Instrument, User
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
    EquityObservationWriteError,
    persist_equity_observation,
)

_RETRIEVED = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _submission() -> EquityObservationSubmission:
    return EquityObservationSubmission(
        provider="eodhd",
        product="historical-eod",
        endpoint="/api/eod/AAA.US",
        dataset_version="prospective-v1",
        tool_version="vynmatrix-equity-evidence-v1",
        source_identity="eodhd:eod:AAA.US:2026-08-13",
        source_revision="1",
        retrieved_at=_RETRIEVED,
        timestamp_semantics={
            "event_at": "official session close",
            "available_at": "first successful local retrieval",
        },
        adjustment_policy="raw-price-split-adjusted-volume",
        entitlement_scope="eodhd-personal-use-paper-only",
        entitlement_owner_user_id="owner-1",
        missing_data_policy="fail-closed",
        artifact_content_sha256="a" * 64,
        instrument_id=1,
        observation_kind="price",
        source_record_identity="AAA.US:2026-08-13",
        event_at=datetime(2026, 8, 13, 20, 0, tzinfo=UTC),
        available_at=_RETRIEVED,
        disposition="observed",
        normalized_content_sha256="b" * 64,
        values=(
            EquityObservationValueInput(
                field_name="close",
                value_type="decimal",
                value=Decimal("101.25"),
                unit="USD",
            ),
            EquityObservationValueInput(
                field_name="session_date",
                value_type="date",
                value=date(2026, 8, 13),
            ),
        ),
    )


def _seed(session: Session) -> None:
    session.add(
        User(
            user_id="owner-1",
            email="owner@example.com",
            base_ccy="EUR",
            status="active",
        )
    )
    session.add(
        Instrument(
            instr_id=1,
            asset_class="equity",
            canonical="AAA",
            settlement_currency="USD",
            market_session_policy="scheduled",
        )
    )
    session.flush()


def test_writer_is_idempotent_and_persists_typed_values() -> None:
    with Session(_engine()) as session:
        _seed(session)
        first = persist_equity_observation(session, _submission())
        replay = persist_equity_observation(session, _submission())

        assert replay.observation_id == first.observation_id
        assert len(first.observation_id) == 64
        assert len(first.lineage_id) == 64
        assert len(first.content_sha256) == 64


def test_writer_rejects_divergent_content_at_the_same_source_revision() -> None:
    with Session(_engine()) as session:
        _seed(session)
        persist_equity_observation(session, _submission())

        with pytest.raises(
            EquityObservationWriteError,
            match="revision already contains different content",
        ):
            persist_equity_observation(
                session,
                replace(_submission(), normalized_content_sha256="c" * 64),
            )
