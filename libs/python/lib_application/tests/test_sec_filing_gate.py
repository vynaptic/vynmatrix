"""SEC filing gate resolver tests over rows written by the Task 4 writer."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from lib_application.db.models import Base, EquityObservation, EquityObservationValue, Instrument
from lib_application.services.sec_filing_gate import resolve_quality_compounder_sec_filing_gate
from lib_application.services.sec_filing_observations import (
    SEC_PUBLIC_ENTITLEMENT,
    SecFilingFact,
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
from lib_strategy.sec_filing_rules import SecFilingGateReason, SecFilingGateResult

_CIK = "0000320193"
_PANEL_CUTOFF = datetime(2025, 3, 31, 20, tzinfo=UTC)
_FETCH_CUTOFF = _PANEL_CUTOFF - timedelta(hours=1)


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


def _authority(provider: str = "sec") -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="sec-filing-test-v1",
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        rules=(
            ProviderAuthorityRule(
                provider=provider,
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=(SEC_PUBLIC_ENTITLEMENT,),
            ),
        ),
    )


def _page(fetch_cutoff: datetime) -> SecSubmissionPage:
    digest = format(int(fetch_cutoff.timestamp()), "x").rjust(64, "0")
    return SecSubmissionPage(
        url=f"https://data.sec.gov/submissions/CIK{_CIK}.json",
        content_sha256=digest,
        retrieved_at=fetch_cutoff + timedelta(minutes=5),
    )


def _filing(
    accession: str,
    *,
    page: SecSubmissionPage,
    form: str = "8-K",
    items: tuple[str, ...] = ("2.02", "9.01"),
    acceptance_time: datetime = datetime(2025, 2, 10, 21, 5, 12, tzinfo=UTC),
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


def _persist(
    session: Session,
    *,
    filings: tuple[tuple[str, str, tuple[str, ...], datetime], ...] = (),
    fetch_cutoff: datetime = _FETCH_CUTOFF,
    instrument_ids: tuple[int, ...] = (11,),
    has_older_page: bool = False,
) -> tuple[str, ...]:
    page = _page(fetch_cutoff)
    fetch = SecIssuerFilingFetch(
        cik=_CIK,
        instrument_ids=instrument_ids,
        fetch_cutoff=fetch_cutoff,
        pages=(page,),
        filings=tuple(
            _filing(accession, page=page, form=form, items=items, acceptance_time=accepted)
            for accession, form, items, accepted in filings
        ),
        oldest_listed_filing_date=date(2022, 6, 1),
        has_older_page=has_older_page,
    )
    ids = persist_sec_filing_fetch(session, fetch, entitlement_owner_user_id=None)
    session.commit()
    return ids


def _resolve(
    session: Session,
    *,
    instrument_ids: tuple[int, ...] = (11,),
    cutoff: datetime = _PANEL_CUTOFF,
    authority: ProviderAuthorityPolicy | None = None,
) -> dict[int, SecFilingGateResult]:
    return dict(
        resolve_quality_compounder_sec_filing_gate(
            session,
            cutoff=cutoff,
            instrument_ids=instrument_ids,
            provider_authority_policy=authority or _authority(),
        )
    )


_ROUTINE = ("0000320193-25-000010", "8-K", ("2.02", "9.01"), _PANEL_CUTOFF - timedelta(days=40))


def _coverage_id(session: Session, instrument_id: int = 11) -> str:
    return str(
        session.scalar(
            sa.select(EquityObservation.observation_id).where(
                EquityObservation.observation_kind == "filing_coverage",
                EquityObservation.instr_id == instrument_id,
            )
        )
    )


def _tamper(session: Session, observation_id: str, field: str, **values: object) -> None:
    # Stored values are immutable through the ORM; a direct update simulates a corrupt row.
    session.execute(
        sa.update(EquityObservationValue)
        .where(
            EquityObservationValue.observation_id == observation_id,
            EquityObservationValue.field_name == field,
        )
        .values(**values)
    )
    session.commit()


def test_clear_member() -> None:
    with _session() as session:
        _persist(session, filings=(_ROUTINE,))
        result = _resolve(session)[11]
    assert result.reason is None
    assert result.fired == ()
    assert result.accessions == ()


def test_non_reliance_member() -> None:
    accepted = _PANEL_CUTOFF - timedelta(days=200)
    with _session() as session:
        _persist(
            session, filings=(_ROUTINE, ("0000320193-24-000077", "8-K/A", ("4.02",), accepted))
        )
        result = _resolve(session)[11]
    assert result.reason is SecFilingGateReason.NON_RELIANCE
    assert result.accessions == ("0000320193-24-000077",)


def test_multi_class_same_reason() -> None:
    accepted = _PANEL_CUTOFF - timedelta(days=500)
    with _session() as session:
        _persist(
            session,
            filings=(("0000320193-23-000050", "8-K", ("1.03",), accepted),),
            instrument_ids=(11, 12),
        )
        results = _resolve(session, instrument_ids=(12, 11))
    assert set(results) == {11, 12}
    for result in results.values():
        assert result.reason is SecFilingGateReason.BANKRUPTCY_OR_RECEIVERSHIP
        assert result.accessions == ("0000320193-23-000050",)
    assert set(results[11].observation_ids).isdisjoint(results[12].observation_ids)


def test_older_fetch_ignored() -> None:
    previous = _PANEL_CUTOFF - timedelta(days=91)
    impairment = ("0000320193-24-000099", "8-K", ("2.06",), previous - timedelta(days=1))
    with _session() as session:
        _persist(session, filings=(impairment,), fetch_cutoff=previous)
        _persist(session)
        result = _resolve(session)[11]
        stale_only = _resolve(session, cutoff=previous + timedelta(hours=1))[11]
    assert result.reason is None
    assert stale_only.reason is SecFilingGateReason.MATERIAL_IMPAIRMENT


def test_latest_coverage_within_tolerance_wins() -> None:
    earlier = _PANEL_CUTOFF - timedelta(hours=5)
    impairment = ("0000320193-25-000020", "8-K", ("2.06",), _PANEL_CUTOFF - timedelta(days=30))
    with _session() as session:
        _persist(session, filings=(impairment,), fetch_cutoff=earlier)
        _persist(session, filings=(impairment,))
        ids = set(_persist(session, filings=(impairment,)))
        result = _resolve(session)[11]
    assert result.reason is SecFilingGateReason.MATERIAL_IMPAIRMENT
    assert set(result.observation_ids) == ids


def test_no_coverage_fails_closed() -> None:
    with _session() as session:
        result = _resolve(session)[11]
    assert result.reason is SecFilingGateReason.COVERAGE_UNAVAILABLE
    assert result.observation_ids == ()


def test_stale_coverage_fails_closed() -> None:
    with _session() as session:
        _persist(session, filings=(_ROUTINE,), fetch_cutoff=_PANEL_CUTOFF - timedelta(hours=7))
        result = _resolve(session)[11]
    assert result.reason is SecFilingGateReason.COVERAGE_UNAVAILABLE


def test_coverage_available_after_cutoff_is_not_selected() -> None:
    with _session() as session:
        _persist(session, filings=(_ROUTINE,), fetch_cutoff=_PANEL_CUTOFF - timedelta(minutes=2))
        result = _resolve(session)[11]
    assert result.reason is SecFilingGateReason.COVERAGE_UNAVAILABLE


def test_unauthorized_provider_is_invalid() -> None:
    with _session() as session:
        _persist(session, filings=(_ROUTINE,))
        result = _resolve(session, authority=_authority("other-vendor"))[11]
    assert result.reason is SecFilingGateReason.EVIDENCE_INVALID


def test_observation_ids_include_coverage_and_events() -> None:
    nt = ("0000320193-25-000011", "NT 10-Q", (), _PANEL_CUTOFF - timedelta(days=10))
    with _session() as session:
        ids = _persist(session, filings=(_ROUTINE, nt))
        result = _resolve(session)[11]
    assert result.reason is SecFilingGateReason.LATE_PERIODIC_REPORT
    assert result.observation_ids == tuple(sorted(ids))
    assert len(ids) == 3


def test_malformed_items_reach_rule_function() -> None:
    malformed = ("0000320193-25-000012", "8-K", ("Item 4.02",), _PANEL_CUTOFF - timedelta(days=5))
    with _session() as session:
        _persist(session, filings=(malformed,))
        result = _resolve(session)[11]
    assert result.reason is SecFilingGateReason.EVIDENCE_INVALID
    assert result.accessions == ("0000320193-25-000012",)


def test_listed_accession_count_mismatch_is_invalid() -> None:
    with _session() as session:
        _persist(session, filings=(_ROUTINE,))
        _tamper(session, _coverage_id(session), "listed_accession_count", integer_value=2)
        result = _resolve(session)[11]
    assert result.reason is SecFilingGateReason.EVIDENCE_INVALID


def test_non_boolean_has_older_page_is_invalid() -> None:
    with _session() as session:
        _persist(session, filings=(_ROUTINE,))
        _tamper(
            session,
            _coverage_id(session),
            "has_older_page",
            value_type="integer",
            boolean_value=None,
            integer_value=0,
        )
        result = _resolve(session)[11]
    assert result.reason is SecFilingGateReason.EVIDENCE_INVALID


def test_non_text_item_is_invalid() -> None:
    with _session() as session:
        ids = _persist(session, filings=(_ROUTINE,))
        _tamper(session, ids[0], "item", value_type="integer", text_value=None, integer_value=2)
        result = _resolve(session)[11]
    assert result.reason is SecFilingGateReason.EVIDENCE_INVALID


def test_naive_cutoff_raises() -> None:
    with _session() as session, pytest.raises(ValueError, match="timezone-aware"):
        _resolve(session, cutoff=_PANEL_CUTOFF.replace(tzinfo=None))


def test_non_integer_instrument_raises() -> None:
    with _session() as session, pytest.raises(ValueError, match="integers"):
        resolve_quality_compounder_sec_filing_gate(
            session,
            cutoff=_PANEL_CUTOFF,
            instrument_ids=(True,),
            provider_authority_policy=_authority(),
        )
