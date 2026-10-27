"""Frozen research evidence stays isolated, idempotent and independent of prices."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from lib_application.db.models import Base, Instrument, InstrumentPrice, User

CSV = b"ts,open,high,low,close,volume\n2026-01-01T00:00:00Z,100,102,99,101,4\n2026-01-01T00:01:00Z,101,103,100,102,5\n"
METADATA = {
    "instrument_id": 1,
    "source": "archive",
    "provenance": "recorded archive",
    "currency": "USD",
    "timeframe": "1m",
    "timestamp_semantics": "bar_open",
    "adjustment_policy": "raw",
}


@pytest.fixture
def session() -> Any:
    import lib_application.db.models.research_data  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as store:
        store.add_all(
            [
                User(
                    user_id="owner",
                    email="owner@test.local",
                    base_ccy="USD",
                    is_deployment_owner=True,
                ),
                User(user_id="other", email="other@test.local", base_ccy="USD"),
                Instrument(
                    instr_id=1, canonical="BTCUSD", asset_class="crypto", settlement_currency="USD"
                ),
                Instrument(
                    instr_id=2, canonical="ABC", asset_class="equity", settlement_currency="USD"
                ),
            ]
        )
        store.commit()
        yield store
    engine.dispose()


def test_receipt_freeze_retry_and_export_preserve_original(session: Session) -> None:
    from lib_application.services import research_data as service

    receipt = service.import_dataset(session, "owner", METADATA, CSV)
    repeated = service.import_dataset(session, "owner", METADATA, CSV)
    assert repeated["import_id"] == receipt["import_id"]
    dataset = service.freeze_dataset(
        session, "owner", receipt["import_id"], receipt["content_hash"], "Window"
    )
    same = service.freeze_dataset(
        session, "owner", receipt["import_id"], receipt["content_hash"], "Window"
    )
    assert same["dataset_id"] == dataset["dataset_id"]
    result = service.get_dataset(session, "owner", dataset["dataset_id"], include_bars=True)
    assert result["bars"][1]["close"] == "102"
    assert result["eligibility"]["training"] is True
    exported = service.export_dataset(session, "owner", dataset["dataset_id"])
    assert exported["original_csv"].encode() == CSV
    assert exported["manifest"]["content_hash"] == receipt["content_hash"]
    assert session.scalar(select(func.count()).select_from(InstrumentPrice)) == 0


def test_invalid_receipts_are_retained_but_cannot_be_frozen(session: Session) -> None:
    from lib_application.db.models.research_data import ResearchImport
    from lib_application.services import research_data as service

    receipt = service.import_dataset(
        session, "owner", METADATA, CSV.replace(b",102,99,101,4", b",NaN,99,101,4")
    )
    assert receipt["valid"] is False
    assert receipt["report"]["eligibility"]["training"] is False
    assert session.scalar(select(func.count()).select_from(ResearchImport)) == 1
    with pytest.raises(service.ResearchDataError, match="invalid"):
        service.freeze_dataset(session, "owner", receipt["import_id"], "x" * 64, "Bad")


def test_owner_hash_and_name_conflicts_fail_closed(session: Session) -> None:
    from lib_application.services import research_data as service

    receipt = service.import_dataset(session, "owner", METADATA, CSV)
    with pytest.raises(service.ResearchDataError, match="hash"):
        service.freeze_dataset(session, "owner", receipt["import_id"], "x" * 64, "Window")
    saved = service.freeze_dataset(
        session, "owner", receipt["import_id"], receipt["content_hash"], "Window"
    )
    with pytest.raises(service.ResearchDataError, match="not found"):
        service.get_dataset(session, "other", saved["dataset_id"])
    assert service.list_datasets(session, "other")["datasets"] == []
    with pytest.raises(service.ResearchDataError, match="already frozen"):
        service.freeze_dataset(
            session, "owner", receipt["import_id"], receipt["content_hash"], "Renamed"
        )


def test_catalogue_currency_and_missing_session_authority(session: Session) -> None:
    from lib_application.services import research_data as service

    with pytest.raises(service.ResearchDataError, match="currency"):
        service.import_dataset(session, "owner", {**METADATA, "currency": "EUR"}, CSV)
    receipt = service.import_dataset(session, "owner", {**METADATA, "instrument_id": 2}, CSV)
    assert receipt["valid"] is True
    assert receipt["report"]["eligibility"]["training"] is False
    assert "authoritative" in str(receipt["report"]["eligibility"]["refusals"])


def test_quota_is_checked_before_storing_receipt(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lib_application.services import research_data as service

    monkeypatch.setattr(service, "OWNER_BYTE_QUOTA", 1)
    with pytest.raises(service.ResearchDataError, match="quota"):
        service.import_dataset(session, "owner", METADATA, CSV)


def test_dataset_and_bar_updates_are_immutable(session: Session) -> None:
    from lib_application.db.models.research_data import ResearchDataset
    from lib_application.services import research_data as service

    receipt = service.import_dataset(session, "owner", METADATA, CSV)
    saved = service.freeze_dataset(
        session, "owner", receipt["import_id"], receipt["content_hash"], "Window"
    )
    row = session.get(ResearchDataset, saved["dataset_id"])
    assert row is not None
    row.name = "changed"
    with pytest.raises(ValueError, match="immutable"):
        session.flush()


def test_snapshot_pins_values_ids_and_revisions(session: Session) -> None:
    from lib_application.services import research_data as service

    price = InstrumentPrice(
        instr_id=1,
        ts=datetime(2026, 1, 1),
        source="archive",
        timeframe="1m",
        open=100,
        high=102,
        low=99,
        close=101,
        volume=4,
    )
    session.add(price)
    session.flush()
    snapshot = service.snapshot_dataset(
        session,
        "owner",
        {
            **METADATA,
            "name": "Stored",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T00:01:00Z",
        },
    )
    price.close = Decimal("100")
    price.content_revision = 2
    session.flush()
    frozen = service.get_dataset(session, "owner", snapshot["dataset_id"], include_bars=True)
    assert frozen["bars"][0]["close"] == "101"
    assert frozen["manifest"]["price_revisions"] == [
        {"price_id": price.price_id, "content_revision": 1}
    ]


def test_market_and_dataset_pagination_are_bounded(session: Session) -> None:
    from lib_application.services import research_data as service

    market = service.market_data(session, "owner", limit=1)
    assert len(market["instruments"]) == 1
    with pytest.raises(service.ResearchDataError, match="limit"):
        service.list_datasets(session, "owner", limit=1001)
    with pytest.raises(service.ResearchDataError, match="cursor"):
        service.list_datasets(session, "owner", after="invalid")


def test_daily_session_timing_comes_from_the_frozen_calendar(session: Session) -> None:
    from lib_application.db.models import MarketCalendar, MarketSession
    from lib_application.services import research_data as service

    calendar = MarketCalendar(
        code="XTEST",
        source_kind="exchange",
        provider="test",
        source_reference="recorded exchange archive",
        coverage_start=datetime(2026, 1, 1),
        coverage_end=datetime(2026, 1, 3),
        observed_at=datetime(2026, 1, 1),
    )
    session.add(calendar)
    session.flush()
    instrument = session.get(Instrument, 2)
    assert instrument is not None
    instrument.market_calendar_id = calendar.calendar_id
    session.add(
        MarketSession(
            calendar_id=calendar.calendar_id,
            opens_at=datetime(2026, 1, 1, 9, 30),
            closes_at=datetime(2026, 1, 1, 16),
        )
    )
    session.flush()
    content = b"ts,open,high,low,close,volume\n2026-01-01T09:30:00Z,100,102,99,101,4\n"
    receipt = service.import_dataset(
        session, "owner", {**METADATA, "instrument_id": 2, "timeframe": "1d"}, content
    )
    assert receipt["report"]["eligibility"]["training"] is True
    saved = service.freeze_dataset(
        session, "owner", receipt["import_id"], receipt["content_hash"], "Exchange session"
    )
    frozen = service.get_dataset(session, "owner", saved["dataset_id"], include_bars=True)
    assert frozen["bars"][0]["bar_close"] == "2026-01-01T16:00:00Z"
    assert frozen["metadata"]["timing_evidence"]["calendar"]["code"] == "XTEST"


def test_explicit_intraday_times_must_match_timeframe(session: Session) -> None:
    from lib_application.services import research_data as service

    content = b"ts,open,high,low,close,volume,bar_open,bar_close\n2026-01-01T00:00:00Z,100,102,99,101,4,2026-01-01T00:00:00Z,2026-01-01T00:02:00Z\n"
    receipt = service.import_dataset(session, "owner", METADATA, content)
    assert receipt["report"]["eligibility"]["training"] is False
    assert "timeframe" in str(receipt["report"]["eligibility"]["refusals"])


def test_list_summaries_do_not_repeat_large_snapshot_manifests(session: Session) -> None:
    from lib_application.services import research_data as service

    receipt = service.import_dataset(session, "owner", METADATA, CSV)
    service.freeze_dataset(
        session, "owner", receipt["import_id"], receipt["content_hash"], "Window"
    )
    summary = service.list_datasets(session, "owner")["datasets"][0]
    assert "timing_evidence" not in summary["metadata"]
    assert "metadata" not in summary["manifest"]
    assert summary["metadata"]["instrument_id"] == 1


def test_import_audit_contains_identity_and_fields_without_raw_content(session: Session) -> None:
    from lib_application.db.models import ApiAuditLog
    from lib_application.services import research_data as service

    receipt = service.import_dataset(session, "owner", METADATA, CSV)
    audit = session.scalars(
        select(ApiAuditLog).where(ApiAuditLog.action == "research.import")
    ).one()
    assert audit.resp["import_id"] == receipt["import_id"]
    assert audit.req == {"fields": sorted(METADATA)}
    assert "recorded archive" not in str(audit.req)
    assert "100,102,99,101" not in str(audit.resp)


def test_invalid_snapshot_retains_failed_receipt_without_a_dataset(session: Session) -> None:
    from lib_application.db.models.research_data import ResearchDataset, ResearchImport
    from lib_application.services import research_data as service

    session.add(
        InstrumentPrice(
            instr_id=1,
            ts=datetime(2026, 1, 1),
            source="archive",
            timeframe="1m",
            open=None,
            high=102,
            low=99,
            close=101,
            volume=4,
        )
    )
    session.flush()
    result = service.snapshot_dataset(
        session,
        "owner",
        {
            **METADATA,
            "name": "Incomplete",
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T00:01:00Z",
        },
    )
    session.commit()
    assert result["dataset"] is None
    assert result["import"]["valid"] is False
    assert session.scalar(select(func.count()).select_from(ResearchImport)) == 1
    assert session.scalar(select(func.count()).select_from(ResearchDataset)) == 0


@pytest.mark.parametrize(
    ("timestamp", "semantics"),
    [("9999-12-31T23:59:59Z", "bar_open"), ("0001-01-01T00:00:00Z", "bar_close")],
)
def test_derived_bar_overflow_preserves_a_failed_import(
    session: Session, timestamp: str, semantics: str
) -> None:
    from lib_application.db.models.research_data import ResearchImport
    from lib_application.services import research_data as service

    content = f"ts,open,high,low,close,volume\n{timestamp},100,102,99,101,4\n".encode()
    receipt = service.import_dataset(
        session, "owner", {**METADATA, "timestamp_semantics": semantics}, content
    )
    assert receipt["valid"] is False
    assert receipt["report"]["eligibility"]["training"] is False
    assert "timestamp range" in str(receipt["report"]["errors"])
    assert session.scalar(select(func.count()).select_from(ResearchImport)) == 1


def test_calendar_terms_and_sessions_share_one_observation(session: Session) -> None:
    from sqlalchemy import event

    from lib_application.db.models import MarketCalendar, MarketSession
    from lib_application.services import research_data as service

    calendar = MarketCalendar(
        code="ATOMIC",
        source_kind="exchange",
        provider="before",
        source_reference="recorded observation",
        coverage_start=datetime(2026, 1, 1),
        coverage_end=datetime(2026, 1, 3),
        observed_at=datetime(2026, 1, 1),
    )
    session.add(calendar)
    session.flush()
    instrument = session.get(Instrument, 2)
    assert instrument is not None
    instrument.market_calendar_id = calendar.calendar_id
    session.add(
        MarketSession(
            calendar_id=calendar.calendar_id,
            opens_at=datetime(2026, 1, 1, 9, 30),
            closes_at=datetime(2026, 1, 1, 16),
        )
    )
    session.flush()
    fired = False

    def replace_after_observation(
        connection: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: Any
    ) -> None:
        nonlocal fired
        if (
            not fired
            and statement.lstrip().startswith("SELECT")
            and "market_calendars" in statement
        ):
            fired = True
            raw = connection.connection.driver_connection.cursor()
            raw.execute("UPDATE market_calendars SET provider='after'")
            raw.execute("UPDATE market_sessions SET closes_at='2026-01-01 17:00:00.000000'")
            raw.close()

    connection = session.connection()
    event.listen(connection, "after_cursor_execute", replace_after_observation)
    try:
        receipt = service.import_dataset(
            session,
            "owner",
            {**METADATA, "instrument_id": 2, "timeframe": "1d"},
            b"ts,open,high,low,close,volume\n2026-01-01T09:30:00Z,100,102,99,101,4\n",
        )
    finally:
        event.remove(connection, "after_cursor_execute", replace_after_observation)
    evidence = receipt["metadata"]["timing_evidence"]
    assert (evidence["calendar"]["provider"], evidence["sessions"][0]["closes_at"]) in {
        ("before", "2026-01-01T16:00:00Z"),
        ("after", "2026-01-01T17:00:00Z"),
    }


@pytest.mark.parametrize("operation", ["import", "freeze", "snapshot"])
def test_maintenance_fences_each_public_data_mutation(session: Session, operation: str) -> None:
    from lib_application.services import research_data as service
    from lib_application.services import research_jobs
    from lib_application.services.research_admission import ResearchWorkflowError

    receipt = service.import_dataset(session, "owner", METADATA, CSV)
    generation = research_jobs.supervisor_status(session)["generation"]
    research_jobs.set_maintenance(session, enabled=True, expected_generation=generation)

    def mutate() -> None:
        if operation == "import":
            service.import_dataset(session, "owner", METADATA, CSV)
        elif operation == "freeze":
            service.freeze_dataset(
                session, "owner", receipt["import_id"], receipt["content_hash"], "Window"
            )
        else:
            service.snapshot_dataset(
                session,
                "owner",
                {
                    **METADATA,
                    "name": "Window",
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2026-01-01T00:01:00Z",
                },
            )

    with pytest.raises(ResearchWorkflowError, match="maintenance"):
        mutate()
