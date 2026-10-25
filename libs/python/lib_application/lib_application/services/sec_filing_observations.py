"""Persist SEC submissions evidence for the quarter-end filing event gate.

One issuer fetch at one cutoff becomes one immutable ``filing`` observation per
(gate-relevant filing, listed instrument) and one ``filing_coverage``
observation per listed instrument. Item codes are stored exactly as EDGAR
listed them; the pure rule function, not this writer, decides their validity.
Every write goes through ``persist_equity_observation``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import EquityObservation, EquitySourceLineage
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
    EquityObservationWriteError,
    persist_equity_observation,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.equity_optional_factors import (
    OPTIONAL_FACTOR_TIMESTAMP_SCHEMA,
    SEC_FILING_COVERAGE_GATE_CONTRACT,
    SEC_FILING_EVENT_GATE_CONTRACT,
    OptionalFactorSourceContract,
)
from lib_strategy.sec_filing_rules import SEC_FILING_EVIDENCE_SCOPE

SEC_PUBLIC_ENTITLEMENT: Final = "public-sec-edgar"

_PROVIDER: Final = "sec"
_PRODUCT: Final = "edgar-submissions"
_DATASET_VERSION: Final = "prospective-edgar-submissions-v1"
_TOOL_VERSION: Final = "sec-filing-observations-v1"
_ADJUSTMENT_POLICY: Final = "not-applicable"
_MISSING_DATA_POLICY: Final = "fail-closed"
_DISPOSITION: Final = "observed"
_FILING_KIND: Final = "filing"
_COVERAGE_KIND: Final = "filing_coverage"
_SHA256_CHARACTERS: Final = frozenset("0123456789abcdef")
_SHA256_LENGTH: Final = 64


class SecFilingPersistenceError(RuntimeError):
    """SEC filing gate evidence cannot be written or replayed safely."""


def _invalid(message: str) -> NoReturn:
    raise SecFilingPersistenceError(message)


@dataclass(frozen=True, slots=True)
class SecSubmissionPage:
    """One retrieved EDGAR submissions page and its exact content digest."""

    url: str
    content_sha256: str
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class SecFilingFact:
    """One gate-relevant filing row as listed on a submissions page."""

    cik: str
    accession: str
    form: str
    items: tuple[str, ...]
    acceptance_time: datetime
    acceptance_time_raw: str
    filing_date: date
    page: SecSubmissionPage


@dataclass(frozen=True, slots=True)
class SecIssuerFilingFetch:
    """Every gate-relevant filing of one issuer fetched at one cutoff."""

    cik: str
    instrument_ids: tuple[int, ...]
    fetch_cutoff: datetime
    pages: tuple[SecSubmissionPage, ...]
    filings: tuple[SecFilingFact, ...]
    oldest_listed_filing_date: date | None
    has_older_page: bool


def _text_field(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field} must be non-blank canonical text")
    return value


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _check_page(page: object) -> SecSubmissionPage:
    if not isinstance(page, SecSubmissionPage):
        _invalid("submission page must be a SecSubmissionPage")
    _text_field(page.url, "submission page url")
    digest = page.content_sha256
    if (
        not isinstance(digest, str)
        or len(digest) != _SHA256_LENGTH
        or not set(digest) <= _SHA256_CHARACTERS
    ):
        _invalid("submission page content_sha256 must be a lowercase SHA-256 digest")
    _aware_utc(page.retrieved_at, "submission page retrieved_at")
    return page


def _check_fetch(fetch: SecIssuerFilingFetch) -> datetime:
    """Validate one fetch and return its cutoff in UTC."""
    if not isinstance(fetch, SecIssuerFilingFetch):
        _invalid("fetch must be a SecIssuerFilingFetch")
    cik = _text_field(fetch.cik, "cik")
    cutoff = _aware_utc(fetch.fetch_cutoff, "fetch_cutoff")
    ids = fetch.instrument_ids
    if not ids or any(isinstance(item, bool) or not isinstance(item, int) for item in ids):
        _invalid("instrument_ids must be a non-empty tuple of integers")
    if len(set(ids)) != len(ids):
        _invalid("instrument_ids must be unique")
    if not fetch.pages:
        _invalid("a filing fetch requires at least one submissions page")
    pages = [_check_page(page) for page in fetch.pages]
    if len({page.content_sha256 for page in pages}) != len(pages):
        _invalid("submission pages must be unique")
    if not isinstance(fetch.has_older_page, bool):
        _invalid("has_older_page must be a boolean")
    oldest = fetch.oldest_listed_filing_date
    if oldest is not None and (isinstance(oldest, datetime) or not isinstance(oldest, date)):
        _invalid("oldest_listed_filing_date must be a date")
    accessions = [
        _check_filing(filing, cik=cik, cutoff=cutoff, pages=pages) for filing in fetch.filings
    ]
    if len(set(accessions)) != len(accessions):
        _invalid("a filing is listed twice in one fetch")
    return cutoff


def _check_filing(
    filing: object, *, cik: str, cutoff: datetime, pages: Sequence[SecSubmissionPage]
) -> str:
    """Validate one filing against its fetch and return its accession."""
    if not isinstance(filing, SecFilingFact):
        _invalid("filings must be SecFilingFact records")
    accession = _text_field(filing.accession, "accession")
    if filing.cik != cik:
        _invalid(f"filing {accession!r} belongs to another CIK")
    _text_field(filing.form, "form")
    _text_field(filing.acceptance_time_raw, "acceptance_time_raw")
    if not isinstance(filing.items, tuple) or not all(
        isinstance(item, str) for item in filing.items
    ):
        _invalid("filing items must be a tuple of raw strings")
    if isinstance(filing.filing_date, datetime) or not isinstance(filing.filing_date, date):
        _invalid("filing_date must be a date")
    if filing.page not in pages:
        _invalid(f"filing {accession!r} cites a page outside this fetch")
    accepted = _aware_utc(filing.acceptance_time, "acceptance_time")
    if accepted > cutoff:
        _invalid(f"filing {accession!r} was accepted after the fetch cutoff")
    if accepted <= cutoff - SEC_FILING_EVIDENCE_SCOPE:
        _invalid(f"filing {accession!r} precedes the lookback window")
    if _aware_utc(filing.page.retrieved_at, "retrieved_at") < accepted:
        _invalid(f"filing {accession!r} was retrieved before its acceptance")
    return accession


def _timestamp_semantics(
    contract: OptionalFactorSourceContract, *, event_source_field: str, revision_source_field: str
) -> dict[str, object]:
    # Availability is the first local retrieval of a prospectively captured page,
    # which is this deployment's first delivery; it is not a backfill retrieval.
    return {
        "availability_is_retrieval_time": False,
        "availability_source_field": "retrieved_at",
        "available_at": contract.available_at_semantic.value,
        "correction_policy": contract.correction_policy.value,
        "event_at": contract.event_at_semantic.value,
        "event_source_field": event_source_field,
        "revision_source_field": revision_source_field,
        "schema": OPTIONAL_FACTOR_TIMESTAMP_SCHEMA,
        "source_timezone": "UTC",
    }


_FILING_SEMANTICS: Final[Mapping[str, object]] = _timestamp_semantics(
    SEC_FILING_EVENT_GATE_CONTRACT,
    event_source_field="acceptanceDateTime",
    revision_source_field="accessionNumber",
)
_COVERAGE_SEMANTICS: Final[Mapping[str, object]] = _timestamp_semantics(
    SEC_FILING_COVERAGE_GATE_CONTRACT,
    event_source_field="fetch_cutoff",
    revision_source_field="content_sha256",
)


def _text(field: str, value: str, ordinal: int = 0) -> EquityObservationValueInput:
    return EquityObservationValueInput(field, "text", value, ordinal)


def _timestamp(field: str, value: datetime) -> EquityObservationValueInput:
    return EquityObservationValueInput(field, "timestamp", value)


def _date(field: str, value: date) -> EquityObservationValueInput:
    return EquityObservationValueInput(field, "date", value)


def _submission(
    *,
    cik: str,
    page: SecSubmissionPage,
    semantics: Mapping[str, object],
    instrument_id: int,
    observation_kind: str,
    source_record_identity: str,
    event_at: datetime,
    available_at: datetime,
    values: Sequence[EquityObservationValueInput],
    accession_number: str | None = None,
    filing_form: str | None = None,
) -> EquityObservationSubmission:
    ordered = tuple(sorted(values, key=lambda item: (item.field_name, item.ordinal)))
    return EquityObservationSubmission(
        provider=_PROVIDER,
        product=_PRODUCT,
        endpoint=page.url,
        dataset_version=_DATASET_VERSION,
        tool_version=_TOOL_VERSION,
        source_identity=f"sec:CIK{cik}:submissions",
        source_revision=page.content_sha256,
        retrieved_at=page.retrieved_at,
        timestamp_semantics=semantics,
        adjustment_policy=_ADJUSTMENT_POLICY,
        entitlement_scope=SEC_PUBLIC_ENTITLEMENT,
        entitlement_owner_user_id=None,
        missing_data_policy=_MISSING_DATA_POLICY,
        artifact_content_sha256=page.content_sha256,
        instrument_id=instrument_id,
        observation_kind=observation_kind,
        source_record_identity=source_record_identity,
        event_at=event_at,
        available_at=available_at,
        disposition=_DISPOSITION,
        normalized_content_sha256=canonical_json_hash([item.payload() for item in ordered]),
        values=ordered,
        accession_number=accession_number,
        filing_form=filing_form,
    )


def _reject_divergent_identity(session: Session, submission: EquityObservationSubmission) -> None:
    """Fail when this identity already exists under any other SEC submissions lineage.

    The shared writer only detects conflicts within one lineage; a changed page
    digest yields a new lineage, so the immutability of an EDGAR identity is
    enforced here across every ``sec``/``edgar-submissions`` lineage.
    """
    rows = session.execute(
        select(EquityObservation.content_sha256, EquitySourceLineage.source_revision)
        .join(EquitySourceLineage, EquitySourceLineage.lineage_id == EquityObservation.lineage_id)
        .where(
            EquitySourceLineage.provider == _PROVIDER,
            EquitySourceLineage.product == _PRODUCT,
            EquityObservation.observation_kind == submission.observation_kind,
            EquityObservation.instr_id == submission.instrument_id,
            EquityObservation.source_record_identity == submission.source_record_identity,
        )
    ).all()
    for content_sha256, source_revision in rows:
        if (
            str(content_sha256) != submission.normalized_content_sha256
            or str(source_revision) != submission.source_revision
        ):
            _invalid(
                f"{submission.observation_kind} identity "
                f"{submission.source_record_identity!r} already holds different content"
            )


def _write(session: Session, submission: EquityObservationSubmission) -> str:
    _reject_divergent_identity(session, submission)
    try:
        observation = persist_equity_observation(session, submission)
    except EquityObservationWriteError as exc:
        raise SecFilingPersistenceError(str(exc)) from exc
    return str(observation.observation_id)


def _filing_submission(
    filing: SecFilingFact, *, instrument_id: int, cutoff: datetime
) -> EquityObservationSubmission:
    accepted = _aware_utc(filing.acceptance_time, "acceptance_time")
    available = _aware_utc(filing.page.retrieved_at, "submission page retrieved_at")
    values = [
        _timestamp("acceptance_time", accepted),
        _text("acceptance_time_raw", filing.acceptance_time_raw),
        _text("cik", filing.cik),
        _date("filing_date", filing.filing_date),
        _timestamp("source_available_at", available),
        *(_text("item", item, ordinal) for ordinal, item in enumerate(filing.items)),
    ]
    return _submission(
        cik=filing.cik,
        page=filing.page,
        semantics=_FILING_SEMANTICS,
        instrument_id=instrument_id,
        observation_kind=_FILING_KIND,
        source_record_identity=f"{filing.accession}|{instrument_id}|{cutoff.isoformat()}",
        event_at=accepted,
        available_at=available,
        values=values,
        accession_number=filing.accession,
        filing_form=filing.form,
    )


def _coverage_submission(
    fetch: SecIssuerFilingFetch, *, instrument_id: int, cutoff: datetime
) -> EquityObservationSubmission:
    latest = max(fetch.pages, key=lambda page: _aware_utc(page.retrieved_at, "retrieved_at"))
    available = _aware_utc(latest.retrieved_at, "submission page retrieved_at")
    accessions = sorted(filing.accession for filing in fetch.filings)
    values = [
        EquityObservationValueInput("has_older_page", "boolean", fetch.has_older_page),
        EquityObservationValueInput("listed_accession_count", "integer", len(accessions)),
        _timestamp("source_available_at", available),
        _timestamp("window_end", cutoff),
        _timestamp("window_start", cutoff - SEC_FILING_EVIDENCE_SCOPE),
        *(
            _text("submission_page_sha256", page.content_sha256, ordinal)
            for ordinal, page in enumerate(fetch.pages)
        ),
        *(
            _text("submission_page_url", page.url, ordinal)
            for ordinal, page in enumerate(fetch.pages)
        ),
        *(
            _text("listed_accession", accession, ordinal)
            for ordinal, accession in enumerate(accessions)
        ),
    ]
    if fetch.oldest_listed_filing_date is not None:
        values.append(_date("oldest_listed_filing_date", fetch.oldest_listed_filing_date))
    return _submission(
        cik=fetch.cik,
        page=latest,
        semantics=_COVERAGE_SEMANTICS,
        instrument_id=instrument_id,
        observation_kind=_COVERAGE_KIND,
        source_record_identity=f"{fetch.cik}|{instrument_id}|{cutoff.isoformat()}",
        event_at=cutoff,
        available_at=available,
        values=values,
    )


def persist_sec_filing_fetch(
    session: Session,
    fetch: SecIssuerFilingFetch,
    *,
    entitlement_owner_user_id: str | None,
) -> tuple[str, ...]:
    """Write one issuer fetch; return the filing then coverage observation ids.

    An exact replay returns the same ids. A second write of any identity with
    different content fails: EDGAR filings are immutable and an amendment is a
    new accession.
    """
    if entitlement_owner_user_id is not None:
        _invalid("SEC EDGAR evidence is public and cannot carry an entitlement owner")
    cutoff = _check_fetch(fetch)
    submissions = [
        _filing_submission(filing, instrument_id=instrument_id, cutoff=cutoff)
        for instrument_id in fetch.instrument_ids
        for filing in fetch.filings
    ]
    submissions.extend(
        _coverage_submission(fetch, instrument_id=instrument_id, cutoff=cutoff)
        for instrument_id in fetch.instrument_ids
    )
    return tuple(_write(session, submission) for submission in submissions)


__all__ = [
    "SEC_PUBLIC_ENTITLEMENT",
    "SecFilingFact",
    "SecFilingPersistenceError",
    "SecIssuerFilingFetch",
    "SecSubmissionPage",
    "persist_sec_filing_fetch",
]
