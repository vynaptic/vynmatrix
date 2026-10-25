"""Tests for SEC filing event acquisition for the quality compounder gate."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from lib_application.services.sec_filing_observations import SecIssuerFilingFetch
from market_data_ingestor.sec_edgar import SecEdgarClient, SecEdgarClientConfig
from market_data_ingestor.sec_filing_events import (
    SecFilingAcquisitionFailure,
    acquire_sec_filing_events,
)

_CIK = "0000000001"
_OTHER_CIK = "0000000002"
_USER_AGENT = "vynmatrix engineering@example.invalid"
# 17:00 New York (EDT) on the quarter end.
_CUTOFF = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)
_RETRIEVED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_HISTORY_PAGE = "CIK0000000001-submissions-001.json"
_OLDER_PAGE = "CIK0000000001-submissions-002.json"


def _columns(rows: list[tuple[str, str, str, str, str]]) -> dict[str, list[str]]:
    """EDGAR column layout from (accession, filingDate, acceptanceDateTime, form, items) rows."""
    return {
        "accessionNumber": [row[0] for row in rows],
        "filingDate": [row[1] for row in rows],
        "acceptanceDateTime": [row[2] for row in rows],
        "form": [row[3] for row in rows],
        "items": [row[4] for row in rows],
    }


def _root(
    cik: str,
    rows: list[tuple[str, str, str, str, str]],
    files: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "cik": cik,
        "name": "EXAMPLE ISSUER INC",
        "sic": "3571",
        "filings": {"recent": _columns(rows), "files": files or []},
    }


# Acceptance values keep EDGAR's Eastern wall clock with a trailing Z.
_ROOT_ROWS = [
    ("0000000001-26-000010", "2026-02-20", "2026-02-20T16:10:00.000Z", "10-K", ""),
    ("0000000001-26-000011", "2026-05-01", "2026-05-01T18:00:00.000Z", "4", ""),
    ("0000000001-26-000012", "2026-07-30", "2026-07-30T16:05:12.000Z", "8-K", "2.02,9.01"),
    ("0000000001-26-000013", "2026-08-15", "2026-08-15T17:20:00.000Z", "NT 10-Q", ""),
    # 16:30 EDT = 20:30 UTC: inside the cutoff.
    ("0000000001-26-000014", "2026-09-30", "2026-09-30T16:30:00.000Z", "8-K", "4.02"),
    # 17:30 EDT = 21:30 UTC: after the cutoff.
    ("0000000001-26-000015", "2026-09-30", "2026-09-30T17:30:00.000Z", "8-K", "1.03"),
]
_HISTORY_ROWS = [
    ("0000000001-25-000020", "2025-03-10", "2025-03-10T08:00:00.000Z", "8-K", "5.02"),
    ("0000000001-24-000021", "2024-11-05", "2024-11-05T16:00:00.000Z", "10-Q", ""),
    # Inside the 760-day fetch window but at or before the 730-day evidence window.
    ("0000000001-24-000022", "2024-09-15", "2024-09-15T16:00:00.000Z", "8-K", "2.06"),
]
_FILES = [
    {"name": _HISTORY_PAGE, "filingFrom": "2024-01-02", "filingTo": "2025-12-31"},
    {"name": _OLDER_PAGE, "filingFrom": "2015-01-02", "filingTo": "2023-12-29"},
]


def _responses(
    *,
    files: list[dict[str, str]] | None = None,
    other: httpx.Response | None = None,
) -> dict[str, httpx.Response]:
    return {
        f"/submissions/CIK{_CIK}.json": httpx.Response(
            200, json=_root(_CIK, _ROOT_ROWS, _FILES if files is None else files)
        ),
        f"/submissions/{_HISTORY_PAGE}": httpx.Response(200, json=_columns(_HISTORY_ROWS)),
        f"/submissions/CIK{_OTHER_CIK}.json": other
        or httpx.Response(
            200,
            json=_root(
                _OTHER_CIK,
                [("0000000002-26-000001", "2026-06-01", "2026-06-01T09:00:00.000Z", "8-K", "8.01")],
            ),
        ),
    }


def _client(responses: dict[str, httpx.Response], requested: list[str]) -> SecEdgarClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return responses[request.url.path]

    return SecEdgarClient(
        SecEdgarClientConfig(
            user_agent=_USER_AGENT,
            retries=0,
            retry_base_delay_seconds=0.0,
            retry_max_delay_seconds=0.0,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
        utc_now=lambda: _RETRIEVED_AT,
    )


def _acquire(
    responses: dict[str, httpx.Response] | None = None,
    *,
    issuers: dict[str, tuple[int, ...]] | None = None,
    requested: list[str] | None = None,
) -> tuple[tuple[SecIssuerFilingFetch, ...], tuple[SecFilingAcquisitionFailure, ...]]:
    return acquire_sec_filing_events(
        _client(responses or _responses(), [] if requested is None else requested),
        issuers=issuers or {_CIK: (11, 12)},
        fetch_cutoff=_CUTOFF,
    )


def _single_fetch() -> SecIssuerFilingFetch:
    fetches, failures = _acquire()
    assert failures == ()
    assert len(fetches) == 1
    return fetches[0]


def test_selects_gate_forms_only() -> None:
    fetch = _single_fetch()

    assert {filing.form for filing in fetch.filings} == {"8-K", "NT 10-Q"}
    assert [filing.accession for filing in fetch.filings] == [
        "0000000001-25-000020",
        "0000000001-26-000012",
        "0000000001-26-000013",
        "0000000001-26-000014",
    ]
    by_accession = {filing.accession: filing for filing in fetch.filings}
    assert by_accession["0000000001-26-000012"].items == ("2.02", "9.01")
    assert by_accession["0000000001-26-000013"].items == ()
    assert fetch.cik == _CIK
    assert fetch.instrument_ids == (11, 12)
    assert fetch.fetch_cutoff == _CUTOFF


def test_drops_filings_after_fetch_cutoff() -> None:
    fetch = _single_fetch()

    accessions = {filing.accession for filing in fetch.filings}
    assert "0000000001-26-000014" in accessions
    assert "0000000001-26-000015" not in accessions
    assert all(filing.acceptance_time <= _CUTOFF for filing in fetch.filings)
    kept = next(f for f in fetch.filings if f.accession == "0000000001-26-000014")
    assert kept.acceptance_time == datetime(2026, 9, 30, 20, 30, tzinfo=UTC)
    assert kept.acceptance_time_raw == "2026-09-30T16:30:00.000Z"


def test_drops_filings_before_evidence_lookback() -> None:
    fetch = _single_fetch()

    assert "0000000001-24-000022" not in {filing.accession for filing in fetch.filings}


def test_heavy_filer_fetches_history_page() -> None:
    requested: list[str] = []
    fetches, failures = _acquire(requested=requested)

    assert failures == ()
    assert requested == [f"/submissions/CIK{_CIK}.json", f"/submissions/{_HISTORY_PAGE}"]
    fetch = fetches[0]
    assert [page.url for page in fetch.pages] == [
        f"https://data.sec.gov/submissions/CIK{_CIK}.json",
        f"https://data.sec.gov/submissions/{_HISTORY_PAGE}",
    ]
    # The oldest row of any form on any fetched page, outside the evidence window.
    assert fetch.oldest_listed_filing_date == date(2024, 9, 15)
    history = next(f for f in fetch.filings if f.accession == "0000000001-25-000020")
    assert history.page == fetch.pages[1]


def test_skipped_page_sets_has_older_page() -> None:
    assert _single_fetch().has_older_page is True

    fetches, _failures = _acquire(_responses(files=[_FILES[0]]))
    assert fetches[0].has_older_page is False

    fetches, _failures = _acquire(_responses(files=[]))
    assert fetches[0].has_older_page is False
    assert fetches[0].oldest_listed_filing_date == date(2026, 2, 20)


def test_one_issuer_failure_isolated() -> None:
    responses = _responses(other=httpx.Response(500, text="unavailable"))

    fetches, failures = _acquire(responses, issuers={_CIK: (11,), _OTHER_CIK: (21,)})

    assert [fetch.cik for fetch in fetches] == [_CIK]
    assert len(failures) == 1
    assert failures[0].cik == _OTHER_CIK
    assert failures[0].error


def test_parse_failure_isolated() -> None:
    responses = _responses(other=httpx.Response(200, text="not json"))

    fetches, failures = _acquire(responses, issuers={_OTHER_CIK: (21,), _CIK: (11,)})

    assert [fetch.cik for fetch in fetches] == [_CIK]
    assert [failure.cik for failure in failures] == [_OTHER_CIK]


def test_page_source_retained() -> None:
    responses = _responses()
    fetch = _single_fetch()

    digests = {page.url: page.content_sha256 for page in fetch.pages}
    assert digests == {
        f"https://data.sec.gov/submissions/CIK{_CIK}.json": hashlib.sha256(
            responses[f"/submissions/CIK{_CIK}.json"].content
        ).hexdigest(),
        f"https://data.sec.gov/submissions/{_HISTORY_PAGE}": hashlib.sha256(
            responses[f"/submissions/{_HISTORY_PAGE}"].content
        ).hexdigest(),
    }
    for filing in fetch.filings:
        assert filing.page in fetch.pages
        assert filing.page.content_sha256 == digests[filing.page.url]
        assert filing.page.retrieved_at == _RETRIEVED_AT


def test_rejects_naive_cutoff() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        acquire_sec_filing_events(
            _client(_responses(), []),
            issuers={_CIK: (11,)},
            fetch_cutoff=_CUTOFF.replace(tzinfo=None),
        )


def test_rejects_duplicate_normalized_cik() -> None:
    with pytest.raises(ValueError, match="more than once"):
        _acquire(issuers={_CIK: (11,), "1": (12,)})
