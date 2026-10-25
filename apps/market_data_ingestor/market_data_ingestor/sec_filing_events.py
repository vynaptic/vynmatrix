"""Acquire SEC filing events from EDGAR submissions for the quality compounder gate.

Each issuer's submissions root and the history pages that can reach the fetch
window are retrieved once at one fetch cutoff. Gate-relevant filings (8-K and NT
late-filing notices) accepted inside the evidence lookback are kept with their
exact source page; the pure rule function, not this module, interprets them.
Persistence is ``persist_sec_filing_fetch``'s responsibility.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from lib_application.services.sec_filing_observations import (
    SecFilingFact,
    SecIssuerFilingFetch,
    SecSubmissionPage,
)
from lib_common.logging import get_logger
from lib_strategy.sec_filing_rules import (
    GATE_RELEVANT_FORMS,
    SEC_FILING_EVIDENCE_SCOPE,
    SEC_FILING_FETCH_LOOKBACK,
)
from market_data_ingestor.sec_edgar import (
    SecEdgarClient,
    SecEdgarError,
    SecSourceDocument,
    SecSubmissionDataset,
    normalize_cik,
)

logger = get_logger(__name__)

# Error name recorded for issuers left unfetched once the caller's stop time passed.
SEC_FILING_GATE_TIME_BUDGET_EXCEEDED = "SecFilingGateTimeBudgetExceeded"


@dataclass(frozen=True, slots=True)
class SecFilingAcquisitionFailure:
    """One issuer whose submissions could not be acquired at this cutoff."""

    cik: str
    error: str


def acquire_sec_filing_events(
    client: SecEdgarClient,
    *,
    issuers: Mapping[str, tuple[int, ...]],
    fetch_cutoff: datetime,
    stop_at: datetime | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> tuple[tuple[SecIssuerFilingFetch, ...], tuple[SecFilingAcquisitionFailure, ...]]:
    """Fetch every issuer's gate-relevant filings at ``fetch_cutoff``.

    ``issuers`` maps CIK to its listed instrument IDs. An SEC failure for one
    issuer is recorded as a failure and the remaining issuers continue; any
    other error propagates. When ``stop_at`` is given, ``clock`` is read before
    each issuer; once it reaches ``stop_at`` no further request is issued and
    every remaining issuer is recorded as a time-budget failure. A request
    already in flight is bounded only by the client's own timeouts.
    """
    if fetch_cutoff.tzinfo is None or fetch_cutoff.utcoffset() is None:
        msg = "fetch_cutoff must be timezone-aware"
        raise ValueError(msg)
    if stop_at is not None and (stop_at.tzinfo is None or stop_at.utcoffset() is None):
        msg = "stop_at must be timezone-aware"
        raise ValueError(msg)
    cutoff = fetch_cutoff.astimezone(UTC)
    normalized: dict[str, tuple[int, ...]] = {}
    for raw_cik, instrument_ids in issuers.items():
        cik = normalize_cik(raw_cik)
        if cik in normalized:
            msg = f"SEC CIK {cik} is listed more than once"
            raise ValueError(msg)
        normalized[cik] = tuple(instrument_ids)

    since = (cutoff - SEC_FILING_FETCH_LOOKBACK).date()
    fetches: list[SecIssuerFilingFetch] = []
    failures: list[SecFilingAcquisitionFailure] = []
    budget_error: str | None = None
    for cik, instrument_ids in normalized.items():
        if budget_error is None and stop_at is not None and clock() >= stop_at:
            budget_error = (
                f"{SEC_FILING_GATE_TIME_BUDGET_EXCEEDED}: stop time "
                f"{stop_at.astimezone(UTC).isoformat()} reached before this issuer"
            )
        if budget_error is not None:
            failures.append(SecFilingAcquisitionFailure(cik=cik, error=budget_error))
            continue
        try:
            dataset = client.fetch_submissions(cik, since=since)
        except SecEdgarError as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "sec_filing_events_issuer_failed",
                cik=cik,
                fetch_cutoff=cutoff.isoformat(),
                error=error,
            )
            failures.append(SecFilingAcquisitionFailure(cik=cik, error=error))
            continue
        fetch = _issuer_fetch(dataset, instrument_ids=instrument_ids, cutoff=cutoff)
        logger.info(
            "sec_filing_events_issuer_fetched",
            cik=cik,
            fetch_cutoff=cutoff.isoformat(),
            pages=len(fetch.pages),
            filings=len(fetch.filings),
            has_older_page=fetch.has_older_page,
        )
        fetches.append(fetch)
    return tuple(fetches), tuple(failures)


def _issuer_fetch(
    dataset: SecSubmissionDataset, *, instrument_ids: tuple[int, ...], cutoff: datetime
) -> SecIssuerFilingFetch:
    pages: dict[SecSourceDocument, SecSubmissionPage] = {}
    for source in dataset.sources:
        pages.setdefault(source, _page(source))
    window_start = cutoff - SEC_FILING_EVIDENCE_SCOPE
    filings = tuple(
        SecFilingFact(
            cik=dataset.cik,
            accession=record.accession,
            form=record.form,
            items=record.items,
            acceptance_time=record.acceptance_time,
            acceptance_time_raw=record.acceptance_time_raw,
            filing_date=record.filing_date,
            page=pages[record.source],
        )
        for record in dataset.filings
        if record.form in GATE_RELEVANT_FORMS and window_start < record.acceptance_time <= cutoff
    )
    return SecIssuerFilingFetch(
        cik=dataset.cik,
        instrument_ids=instrument_ids,
        fetch_cutoff=cutoff,
        pages=tuple(pages.values()),
        filings=filings,
        oldest_listed_filing_date=min(
            (record.filing_date for record in dataset.filings), default=None
        ),
        has_older_page=bool(dataset.skipped_page_names),
    )


def _page(source: SecSourceDocument) -> SecSubmissionPage:
    return SecSubmissionPage(
        url=source.endpoint,
        content_sha256=source.content_sha256,
        retrieved_at=source.retrieved_at,
    )
