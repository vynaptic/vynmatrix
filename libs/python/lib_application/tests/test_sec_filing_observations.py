"""SEC filing and filing-coverage persistence tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    EquityObservation,
    EquityObservationValue,
    EquitySourceLineage,
    Instrument,
)
from lib_application.services.equity_optional_factor_sources import (
    validate_optional_factor_evidence,
)
from lib_application.services.sec_filing_observations import (
    SEC_PUBLIC_ENTITLEMENT,
    SecFilingFact,
    SecFilingPersistenceError,
    SecIssuerFilingFetch,
    SecSubmissionPage,
    persist_sec_filing_fetch,
)
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.equity_optional_factors import (
    SEC_FILING_COVERAGE_GATE_CONTRACT,
    SEC_FILING_EVENT_GATE_CONTRACT,
)

_CIK = "0000320193"
_CUTOFF = datetime(2025, 3, 31, 20, tzinfo=UTC)
_RETRIEVED = _CUTOFF + timedelta(minutes=5)
_PAGE = SecSubmissionPage(
    url="https://data.sec.gov/submissions/CIK0000320193.json",
    content_sha256="a" * 64,
    retrieved_at=_RETRIEVED,
)
_OLDER_PAGE = SecSubmissionPage(
    url="https://data.sec.gov/submissions/CIK0000320193-submissions-001.json",
    content_sha256="b" * 64,
    retrieved_at=_RETRIEVED + timedelta(minutes=1),
)


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)

    @sa.event.listens_for(engine, "connect")
    def _fk(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _session() -> Session:
    session = Session(_engine())
    for instr_id, canonical in ((11, "AAA"), (12, "AAB")):
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


def _authority() -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="sec-filing-test-v1",
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        rules=(
            ProviderAuthorityRule(
                provider="sec",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=(SEC_PUBLIC_ENTITLEMENT,),
            ),
        ),
    )


def _filing(
    accession: str,
    *,
    form: str = "8-K",
    items: tuple[str, ...] = ("2.02", "9.01"),
    acceptance_time: datetime = datetime(2025, 2, 10, 21, 5, 12, tzinfo=UTC),
    page: SecSubmissionPage = _PAGE,
) -> SecFilingFact:
    return SecFilingFact(
        cik=_CIK,
        accession=accession,
        form=form,
        items=items,
        acceptance_time=acceptance_time,
        acceptance_time_raw=acceptance_time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        filing_date=acceptance_time.date(),
        page=page,
    )


def _fetch(
    *,
    filings: tuple[SecFilingFact, ...] | None = None,
    instrument_ids: tuple[int, ...] = (11, 12),
    fetch_cutoff: datetime = _CUTOFF,
    pages: tuple[SecSubmissionPage, ...] = (_PAGE, _OLDER_PAGE),
    oldest: date | None = date(2022, 6, 1),
    has_older_page: bool = False,
) -> SecIssuerFilingFetch:
    return SecIssuerFilingFetch(
        cik=_CIK,
        instrument_ids=instrument_ids,
        fetch_cutoff=fetch_cutoff,
        pages=pages,
        filings=(
            filings
            if filings is not None
            else (
                _filing("0000320193-25-000010"),
                _filing("0000320193-25-000011", form="NT 10-Q", items=()),
                _filing("0000320193-24-000090", page=_OLDER_PAGE),
            )
        ),
        oldest_listed_filing_date=oldest,
        has_older_page=has_older_page,
    )


def _observations(session: Session, kind: str) -> list[EquityObservation]:
    return list(
        session.scalars(
            sa.select(EquityObservation)
            .where(EquityObservation.observation_kind == kind)
            .order_by(EquityObservation.source_record_identity)
        )
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


def _persist(session: Session, fetch: SecIssuerFilingFetch | None = None) -> tuple[str, ...]:
    return persist_sec_filing_fetch(session, fetch or _fetch(), entitlement_owner_user_id=None)


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def test_writes_one_filing_row_per_class_and_one_coverage_per_class() -> None:
    with _session() as session:
        ids = _persist(session)
        filings = _observations(session, "filing")
        coverage = _observations(session, "filing_coverage")
    assert len(ids) == 8
    assert len(set(ids)) == 8
    assert len(filings) == 6
    assert len(coverage) == 2
    assert {row.instr_id for row in filings} == {11, 12}
    assert {row.instr_id for row in coverage} == {11, 12}


def test_filing_row_fields() -> None:
    with _session() as session:
        _persist(session)
        row = next(
            item
            for item in _observations(session, "filing")
            if item.accession_number == "0000320193-25-000010" and item.instr_id == 11
        )
        lineage = session.get(EquitySourceLineage, row.lineage_id)
        items = _values(session, row.observation_id, "item")
        acceptance = _values(session, row.observation_id, "acceptance_time")
        raw = _values(session, row.observation_id, "acceptance_time_raw")
        available = _values(session, row.observation_id, "source_available_at")
        filing_date = _values(session, row.observation_id, "filing_date")
        cik = _values(session, row.observation_id, "cik")
    assert row.filing_form == "8-K"
    assert row.source_record_identity == (f"0000320193-25-000010|11|{_CUTOFF.isoformat()}")
    assert _utc(row.event_at) == datetime(2025, 2, 10, 21, 5, 12, tzinfo=UTC)
    assert _utc(row.available_at) == _RETRIEVED
    assert [(item.ordinal, item.text_value) for item in items] == [(0, "2.02"), (1, "9.01")]
    assert _utc(acceptance[0].timestamp_value) == _utc(row.event_at)
    assert acceptance[0].ordinal == 0
    assert raw[0].text_value == "2025-02-10T21:05:12.000Z"
    assert _utc(available[0].timestamp_value) == _RETRIEVED
    assert filing_date[0].date_value == date(2025, 2, 10)
    assert cik[0].text_value == _CIK
    assert lineage is not None
    assert lineage.provider == "sec"
    assert lineage.product == "edgar-submissions"
    assert lineage.endpoint == _PAGE.url
    assert lineage.source_revision == _PAGE.content_sha256
    assert lineage.content_sha256 == _PAGE.content_sha256
    assert lineage.entitlement_scope == SEC_PUBLIC_ENTITLEMENT
    assert lineage.entitlement_owner_user_id is None
    assert lineage.missing_data_policy == "fail-closed"


def test_identity_includes_fetch_cutoff() -> None:
    later = _CUTOFF + timedelta(days=91)
    later_pages = tuple(
        replace(page, retrieved_at=page.retrieved_at + timedelta(days=91))
        for page in (_PAGE, _OLDER_PAGE)
    )
    with _session() as session:
        first = _persist(session)
        second = _persist(
            session,
            _fetch(
                fetch_cutoff=later,
                pages=later_pages,
                filings=(_filing("0000320193-25-000010", page=later_pages[0]),),
            ),
        )
        rows = _observations(session, "filing")
    assert not set(first) & set(second)
    assert len(rows) == 8
    assert {row.revision for row in rows} == {1}


def test_exact_replay_is_idempotent() -> None:
    with _session() as session:
        first = _persist(session)
        second = _persist(session)
        count = len(_observations(session, "filing"))
    assert first == second
    assert count == 6


def test_same_identity_different_content_rejected() -> None:
    changed = _fetch(
        filings=(_filing("0000320193-25-000010", items=("1.03",)),),
    )
    with _session() as session:
        _persist(session)
        with pytest.raises(SecFilingPersistenceError, match="different content"):
            _persist(session, changed)


def test_same_identity_different_page_rejected() -> None:
    page = replace(_PAGE, content_sha256="c" * 64)
    changed = _fetch(
        pages=(page, _OLDER_PAGE),
        filings=(_filing("0000320193-25-000010", page=page),),
    )
    with _session() as session:
        _persist(session)
        with pytest.raises(SecFilingPersistenceError, match="different content"):
            _persist(session, changed)


def test_filing_after_fetch_cutoff_rejected() -> None:
    late = _filing("0000320193-25-000099", acceptance_time=_CUTOFF + timedelta(seconds=1))
    with _session() as session, pytest.raises(SecFilingPersistenceError, match="after"):
        _persist(session, _fetch(filings=(late,)))


def test_filing_from_unlisted_page_rejected() -> None:
    stray = replace(_PAGE, content_sha256="e" * 64)
    with _session() as session, pytest.raises(SecFilingPersistenceError, match="page"):
        _persist(session, _fetch(filings=(_filing("0000320193-25-000010", page=stray),)))


def test_foreign_issuer_filing_rejected() -> None:
    foreign = replace(_filing("0000789019-25-000001"), cik="0000789019")
    with _session() as session, pytest.raises(SecFilingPersistenceError, match="CIK"):
        _persist(session, _fetch(filings=(foreign,)))


def test_entitlement_owner_rejected() -> None:
    with _session() as session, pytest.raises(SecFilingPersistenceError, match="public"):
        persist_sec_filing_fetch(session, _fetch(), entitlement_owner_user_id="user-1")


def test_malformed_items_still_stored() -> None:
    malformed = _filing("0000320193-25-000010", items=("2.02", " 9.01", "", "Item 5"))
    with _session() as session:
        _persist(session, _fetch(filings=(malformed,), instrument_ids=(11,)))
        row = _observations(session, "filing")[0]
        items = _values(session, row.observation_id, "item")
    assert [item.text_value for item in items] == ["2.02", " 9.01", "", "Item 5"]
    assert [item.ordinal for item in items] == [0, 1, 2, 3]


def test_coverage_lists_every_accession() -> None:
    with _session() as session:
        _persist(session)
        row = next(
            item for item in _observations(session, "filing_coverage") if item.instr_id == 12
        )
        listed = _values(session, row.observation_id, "listed_accession")
        count = _values(session, row.observation_id, "listed_accession_count")
        pages = _values(session, row.observation_id, "submission_page_sha256")
        start = _values(session, row.observation_id, "window_start")
        end = _values(session, row.observation_id, "window_end")
        oldest = _values(session, row.observation_id, "oldest_listed_filing_date")
        older = _values(session, row.observation_id, "has_older_page")
        available = _values(session, row.observation_id, "source_available_at")
        lineage = session.get(EquitySourceLineage, row.lineage_id)
    assert sorted(str(item.text_value) for item in listed) == [
        "0000320193-24-000090",
        "0000320193-25-000010",
        "0000320193-25-000011",
    ]
    assert [(row.ordinal, row.integer_value) for row in count] == [(0, 3)]
    assert [item.text_value for item in pages] == ["a" * 64, "b" * 64]
    assert _utc(start[0].timestamp_value) == _CUTOFF - timedelta(days=730)
    assert _utc(end[0].timestamp_value) == _CUTOFF
    assert _utc(row.event_at) == _CUTOFF
    assert _utc(row.available_at) == _OLDER_PAGE.retrieved_at
    assert _utc(available[0].timestamp_value) == _OLDER_PAGE.retrieved_at
    assert oldest[0].date_value == date(2022, 6, 1)
    assert older[0].boolean_value is False
    assert lineage is not None
    assert lineage.source_revision == _OLDER_PAGE.content_sha256


def test_coverage_omits_unknown_oldest_filing_date() -> None:
    with _session() as session:
        _persist(session, _fetch(oldest=None, has_older_page=True))
        row = _observations(session, "filing_coverage")[0]
        oldest = _values(session, row.observation_id, "oldest_listed_filing_date")
        older = _values(session, row.observation_id, "has_older_page")
    assert oldest == []
    assert older[0].boolean_value is True


def test_rows_pass_optional_gate_evidence_validation() -> None:
    with _session() as session:
        _persist(session, _fetch(instrument_ids=(11,)))
        session.commit()
        filing = _observations(session, "filing")[0]
        coverage = _observations(session, "filing_coverage")[0]
        validation_cutoff = _OLDER_PAGE.retrieved_at + timedelta(minutes=1)
        results = [
            validate_optional_factor_evidence(
                session,
                contract=contract,
                factor_version="sec-filing-rules-v1",
                observation_id=str(observation.observation_id),
                cutoff=validation_cutoff,
                provider_authority_policy=_authority(),
                expected_instrument_id=11,
            )
            for contract, observation in (
                (SEC_FILING_EVENT_GATE_CONTRACT, filing),
                (SEC_FILING_COVERAGE_GATE_CONTRACT, coverage),
            )
        ]
    assert [result.observation_id for result in results] == [
        filing.observation_id,
        coverage.observation_id,
    ]
    assert {result.entitlement_scope for result in results} == {SEC_PUBLIC_ENTITLEMENT}


def test_coverage_with_zero_filings_passes_gate_evidence_validation() -> None:
    with _session() as session:
        _persist(session, _fetch(filings=(), instrument_ids=(11,)))
        session.commit()
        assert _observations(session, "filing") == []
        coverage = _observations(session, "filing_coverage")[0]
        listed = _values(session, coverage.observation_id, "listed_accession")
        count = _values(session, coverage.observation_id, "listed_accession_count")
        result = validate_optional_factor_evidence(
            session,
            contract=SEC_FILING_COVERAGE_GATE_CONTRACT,
            factor_version="sec-filing-rules-v1",
            observation_id=str(coverage.observation_id),
            cutoff=_OLDER_PAGE.retrieved_at + timedelta(minutes=1),
            provider_authority_policy=_authority(),
            expected_instrument_id=11,
        )
    assert listed == []
    assert [(row.ordinal, row.integer_value) for row in count] == [(0, 0)]
    assert result.observation_id == coverage.observation_id
