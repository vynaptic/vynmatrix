"""SEC EDGAR protocol, real-response fixture, and failure-path tests."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from html import escape
from typing import Any

import httpx
import pytest

from market_data_ingestor.equity_factors import (
    CanonicalFundamentalMetric,
    FundamentalCalculationConfig,
    FundamentalSleeve,
    IssuerFundamentalEvidence,
    calculate_fundamental_panel,
    canonicalize_sec_facts,
)
from market_data_ingestor.sec_edgar import (
    SecCompanyFactsDataset,
    SecEdgarClient,
    SecEdgarClientConfig,
    SecEdgarParseError,
    SecEdgarRequestError,
    SecSourceDocument,
    SecSubmissionDataset,
    apply_filing_header,
    attach_filing_lineage,
    parse_company_facts_payload,
    parse_company_tickers_payload,
    parse_filing_header,
    parse_submissions_acceptance_time,
    parse_submissions_payload,
)

_CIK = "0000000001"
_ORIGINAL_ACCESSION = "0000000001-24-000001"
_AMENDED_ACCESSION = "0000000001-24-000002"
_USER_AGENT = "vynmatrix engineering@example.invalid"
_RETRIEVED_AT = datetime(2025, 1, 2, 12, tzinfo=UTC)

# Minimal excerpts selected from the official SEC responses on 2026-07-28.
# Scalar values and response nesting are unchanged; unrelated observations were removed.
# SEC submissions acceptanceDateTime retains the published Eastern wall-clock
# value, including its misleading trailing Z. The authoritative filing header
# below supplies the clock-for-clock acceptance evidence.
_AXON_CIK = "0001069183"
_AXON_PRIOR_ACCESSION = "0001069183-24-000006"
_AXON_ORIGINAL_ACCESSION = "0001069183-25-000019"
_AXON_AMENDED_ACCESSION = "0001069183-25-000075"
_AXON_SUBMISSIONS_SHA256 = "0b55f57e6a8844669088946596522bdb7c59ec00bc67ca8d2c797123c7104c1a"
_AXON_COMPANY_FACTS_SHA256 = "570b937ecd2c61a8e08f23830c1a1d5f796aa7a7405a67210d087707d2045a94"
_AXON_SUBMISSIONS_EXCERPT: dict[str, Any] = {
    "cik": _AXON_CIK,
    "name": "AXON ENTERPRISE, INC.",
    "sic": "3480",
    "filings": {
        "recent": {
            "accessionNumber": [
                _AXON_AMENDED_ACCESSION,
                _AXON_ORIGINAL_ACCESSION,
                _AXON_PRIOR_ACCESSION,
            ],
            "filingDate": ["2025-05-07", "2025-02-28", "2024-02-27"],
            "acceptanceDateTime": [
                "2025-05-07T16:38:11.000Z",
                "2025-02-28T16:22:06.000Z",
                "2024-02-27T16:31:51.000Z",
            ],
            "reportDate": ["2024-12-31", "2024-12-31", "2023-12-31"],
            "form": ["10-K/A", "10-K", "10-K"],
            "primaryDocument": [
                "axon-20241231.htm",
                "axon-20241231.htm",
                "axon-20231231x10k.htm",
            ],
            "items": ["", "", ""],
            "isXBRL": [1, 1, 1],
            "isInlineXBRL": [1, 1, 1],
        }
    },
}
_AXON_ORIGINAL_HEADER_EXCERPT = """<SEC-DOCUMENT>0001069183-25-000019.txt
<SEC-HEADER>0001069183-25-000019.hdr.sgml
<ACCEPTANCE-DATETIME>20250228162206
ACCESSION NUMBER: 0001069183-25-000019
CONFORMED SUBMISSION TYPE: 10-K
CONFORMED PERIOD OF REPORT: 20241231
FILED AS OF DATE: 20250228

FILER:
    COMPANY DATA:
        COMPANY CONFORMED NAME: AXON ENTERPRISE, INC.
        CENTRAL INDEX KEY: 0001069183
        STANDARD INDUSTRIAL CLASSIFICATION: ORDNANCE & ACCESSORIES [3480]
</SEC-HEADER>
<DOCUMENT>
"""
_AXON_COMPANY_FACTS_EXCERPT: dict[str, Any] = {
    "cik": 1069183,
    "entityName": "Axon Enterprise, Inc.",
    "facts": {
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "label": "Entity Common Stock, Shares Outstanding",
                "description": (
                    "Indicate number of shares or other units outstanding of each of "
                    "registrant's classes of capital or common stock or other ownership "
                    "interests, if and as stated on cover of related periodic report. "
                    "Where multiple classes or units exist define each class/interest by "
                    "adding class of stock items such as Common Class A [Member], Common "
                    "Class B [Member] or Partnership Interest [Member] onto the Instrument "
                    "[Domain] of the Entity Listings, Instrument."
                ),
                "units": {
                    "shares": [
                        {
                            "end": "2025-02-24",
                            "val": 76623266,
                            "accn": _AXON_ORIGINAL_ACCESSION,
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2025-02-28",
                        }
                    ]
                },
            }
        },
        "us-gaap": {
            "Assets": {
                "label": "Assets",
                "description": (
                    "Sum of the carrying amounts as of the balance sheet date of all "
                    "assets that are recognized. Assets are probable future economic "
                    "benefits obtained or controlled by an entity as a result of past "
                    "transactions or events."
                ),
                "units": {
                    "USD": [
                        {
                            "end": "2022-12-31",
                            "val": 2851894000,
                            "accn": _AXON_PRIOR_ACCESSION,
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-02-27",
                            "frame": "CY2022Q4I",
                        },
                        {
                            "end": "2023-12-31",
                            "val": 3436845000,
                            "accn": _AXON_PRIOR_ACCESSION,
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-02-27",
                        },
                        {
                            "end": "2023-12-31",
                            "val": 3409174000,
                            "accn": _AXON_ORIGINAL_ACCESSION,
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2025-02-28",
                        },
                        {
                            "end": "2023-12-31",
                            "val": 3409174000,
                            "accn": _AXON_AMENDED_ACCESSION,
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K/A",
                            "filed": "2025-05-07",
                            "frame": "CY2023Q4I",
                        },
                    ]
                },
            },
            "EarningsPerShareBasic": {
                "label": "Earnings Per Share, Basic",
                "description": (
                    "The amount of net income (loss) for the period per each share of "
                    "common stock or unit outstanding during the reporting period."
                ),
                "units": {
                    "USD/shares": [
                        {
                            "start": "2024-01-01",
                            "end": "2024-12-31",
                            "val": 4.98,
                            "accn": _AXON_ORIGINAL_ACCESSION,
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2025-02-28",
                        }
                    ]
                },
            },
            "GrossProfit": {
                "label": "Gross Profit",
                "description": (
                    "Aggregate revenue less cost of goods and services sold or operating "
                    "expenses directly attributable to the revenue generation activity."
                ),
                "units": {
                    "USD": [
                        {
                            "start": "2023-01-01",
                            "end": "2023-12-31",
                            "val": 955453000,
                            "accn": _AXON_ORIGINAL_ACCESSION,
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2025-02-28",
                        },
                        {
                            "start": "2023-10-01",
                            "end": "2023-12-31",
                            "val": 263992000,
                            "accn": _AXON_ORIGINAL_ACCESSION,
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2025-02-28",
                        },
                    ]
                },
            },
            "LiabilitiesCurrent": {
                "label": "Liabilities, Current",
                "description": (
                    "Total obligations incurred as part of normal operations that are "
                    "expected to be paid during the following twelve months or within "
                    "one business cycle, if longer."
                ),
                "units": {
                    "USD": [
                        {
                            "end": "2024-12-31",
                            "val": 997586000,
                            "accn": _AXON_ORIGINAL_ACCESSION,
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2025-02-28",
                        },
                        {
                            "end": "2024-12-31",
                            "val": 1677875000,
                            "accn": _AXON_AMENDED_ACCESSION,
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K/A",
                            "filed": "2025-05-07",
                        },
                    ]
                },
            },
        },
    },
}


def _source(endpoint: str, content: bytes = b"protocol") -> SecSourceDocument:
    return SecSourceDocument(
        endpoint=endpoint,
        retrieved_at=_RETRIEVED_AT,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def _fixture_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _real_axon_datasets() -> tuple[SecSubmissionDataset, SecCompanyFactsDataset]:
    submissions_content = _fixture_bytes(_AXON_SUBMISSIONS_EXCERPT)
    company_facts_content = _fixture_bytes(_AXON_COMPANY_FACTS_EXCERPT)
    assert hashlib.sha256(submissions_content).hexdigest() == _AXON_SUBMISSIONS_SHA256
    assert hashlib.sha256(company_facts_content).hexdigest() == _AXON_COMPANY_FACTS_SHA256
    submissions = parse_submissions_payload(
        _AXON_SUBMISSIONS_EXCERPT,
        cik=_AXON_CIK,
        source=_source(
            "https://data.sec.gov/submissions/CIK0001069183.json",
            submissions_content,
        ),
    )
    company_facts = parse_company_facts_payload(
        _AXON_COMPANY_FACTS_EXCERPT,
        cik=_AXON_CIK,
        source=_source(
            "https://data.sec.gov/api/xbrl/companyfacts/CIK0001069183.json",
            company_facts_content,
        ),
    )
    return submissions, company_facts


def test_company_ticker_catalogue_retains_exact_source_identity() -> None:
    source = _source(
        "https://www.sec.gov/files/company_tickers.json",
        b"ticker-catalogue",
    )
    dataset = parse_company_tickers_payload(
        {
            "1": {"cik_str": "2", "ticker": "brk-b", "title": "B"},
            "0": {"cik_str": 1, "ticker": "aaa", "title": "A"},
        },
        source=source,
    )

    assert dataset.source == source
    assert [(entry.cik, entry.ticker) for entry in dataset.entries] == [
        ("0000000001", "AAA"),
        ("0000000002", "BRK-B"),
    ]


def test_company_ticker_response_retains_exact_bytes() -> None:
    content = b'{"0":{"cik_str":1,"ticker":"AAA","title":"Issuer A"}}'
    client = _client(lambda _request: httpx.Response(200, content=content))

    response = client.fetch_company_ticker_response()

    assert response.content == content
    assert response.dataset.entries[0].cik == "0000000001"
    assert response.dataset.entries[0].ticker == "AAA"
    assert response.dataset.source.content_sha256 == hashlib.sha256(content).hexdigest()


def test_company_ticker_catalogue_rejects_implicit_boolean_cik() -> None:
    with pytest.raises(SecEdgarParseError, match="string or integer"):
        parse_company_tickers_payload(
            {"0": {"cik_str": True, "ticker": "AAA", "title": "A"}},
            source=_source("https://www.sec.gov/files/company_tickers.json"),
        )


def _filing_columns(
    *,
    accessions: list[str] | None = None,
    acceptances: list[str] | None = None,
    forms: list[str] | None = None,
    filing_dates: list[str] | None = None,
) -> dict[str, list[Any]]:
    return {
        "accessionNumber": accessions or [_ORIGINAL_ACCESSION, _AMENDED_ACCESSION],
        "acceptanceDateTime": acceptances
        or ["2024-02-01T16:00:00.000Z", "2024-03-01T17:00:00.000Z"],
        "filingDate": filing_dates or ["2024-02-01", "2024-03-01"],
        "form": forms or ["10-K", "10-K/A"],
        "reportDate": ["2023-12-31", "2023-12-31"],
        "primaryDocument": ["original.htm", "amended.htm"],
        "items": ["", ""],
        "isXBRL": [1, 1],
        "isInlineXBRL": [1, 1],
    }


def _root_submissions_payload() -> dict[str, Any]:
    return {
        "cik": 1,
        "name": "Protocol Fixture Corporation",
        "sic": "9999",
        "filings": {
            "recent": _filing_columns(),
            "files": [],
        },
    }


def _company_facts_payload() -> dict[str, Any]:
    return {
        "cik": 1,
        "entityName": "Protocol Fixture Corporation",
        "facts": {
            "us-gaap": {
                "ProtocolMetric": {
                    "label": "Protocol metric",
                    "description": "Non-market protocol value.",
                    "units": {
                        "USD": [
                            {
                                "start": "2023-01-01",
                                "end": "2023-12-31",
                                "val": 100,
                                "accn": _ORIGINAL_ACCESSION,
                                "fy": 2023,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2024-02-01",
                                "frame": "CY2023",
                            },
                            {
                                "start": "2023-01-01",
                                "end": "2023-12-31",
                                "val": 90,
                                "accn": _AMENDED_ACCESSION,
                                "fy": 2023,
                                "fp": "FY",
                                "form": "10-K/A",
                                "filed": "2024-03-01",
                                "frame": "CY2023",
                            },
                        ],
                    },
                },
            },
        },
    }


def _filing_header(*, accession: str, accepted: str, form: str, filed: str) -> str:
    return f"""<SEC-DOCUMENT>{accession}.txt
<SEC-HEADER>{accession}.hdr.sgml
<ACCEPTANCE-DATETIME>{accepted}
ACCESSION NUMBER: {accession}
CONFORMED SUBMISSION TYPE: {form}
CONFORMED PERIOD OF REPORT: 20231231
FILED AS OF DATE: {filed}

FILER:
    COMPANY DATA:
        COMPANY CONFORMED NAME: Protocol Fixture Corporation
        CENTRAL INDEX KEY: 0000000001
        STANDARD INDUSTRIAL CLASSIFICATION: PROTOCOL SERVICES [9999]
</SEC-HEADER>
<DOCUMENT>
"""


def _client(
    handler: Any,
    *,
    sleep: Any = lambda _seconds: None,
    monotonic: Any = lambda: 0.0,
    retries: int = 2,
) -> SecEdgarClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return SecEdgarClient(
        SecEdgarClientConfig(
            user_agent=_USER_AGENT,
            retries=retries,
            retry_base_delay_seconds=0.0,
            retry_max_delay_seconds=0.0,
        ),
        client=http_client,
        sleep=sleep,
        monotonic=monotonic,
        utc_now=lambda: _RETRIEVED_AT,
    )


@pytest.mark.parametrize(
    "user_agent",
    [
        "",
        "anonymous",
        "vynmatrix no-contact",
        "vynmatrix engineering@example.invalid\r\nInjected: true",
    ],
)
def test_client_configuration_rejects_undeclared_automation(user_agent: str) -> None:
    with pytest.raises(ValueError, match="User-Agent"):
        SecEdgarClientConfig(user_agent=user_agent)


def test_client_configuration_enforces_sec_rate_and_timeout_bounds() -> None:
    with pytest.raises(ValueError, match="requests_per_second"):
        SecEdgarClientConfig(user_agent=_USER_AGENT, requests_per_second=10.01)
    with pytest.raises(ValueError, match="timeout_seconds"):
        SecEdgarClientConfig(user_agent=_USER_AGENT, timeout_seconds=0.0)


def test_real_sec_excerpts_preserve_content_hash_cik_accessions_and_units() -> None:
    bodies = {
        "/submissions/CIK0001069183.json": _fixture_bytes(_AXON_SUBMISSIONS_EXCERPT),
        "/api/xbrl/companyfacts/CIK0001069183.json": _fixture_bytes(_AXON_COMPANY_FACTS_EXCERPT),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bodies[request.url.path])

    client = _client(handler)
    submissions = client.fetch_submissions(_AXON_CIK)
    company_facts = client.fetch_company_facts(_AXON_CIK)

    assert submissions.cik == company_facts.cik == _AXON_CIK
    assert submissions.sources[0].content_sha256 == _AXON_SUBMISSIONS_SHA256
    assert company_facts.source.content_sha256 == _AXON_COMPANY_FACTS_SHA256
    assert {filing.accession for filing in submissions.filings} == {
        _AXON_PRIOR_ACCESSION,
        _AXON_ORIGINAL_ACCESSION,
        _AXON_AMENDED_ACCESSION,
    }
    assert {fact.unit for fact in company_facts.facts} == {
        "USD",
        "USD/shares",
        "shares",
    }
    assert all(
        fact.cik == _AXON_CIK and fact.source == company_facts.source
        for fact in company_facts.facts
    )


def test_real_submissions_wall_clock_reconciles_to_authoritative_header() -> None:
    submissions, _company_facts = _real_axon_datasets()
    filing = next(
        item for item in submissions.filings if item.accession == _AXON_ORIGINAL_ACCESSION
    )
    header_endpoint = (
        "https://www.sec.gov/Archives/edgar/data/1069183/000106918325000019/"
        "0001069183-25-000019-index-headers.html"
    )
    header = parse_filing_header(
        _AXON_ORIGINAL_HEADER_EXCERPT,
        source=_source(header_endpoint, _AXON_ORIGINAL_HEADER_EXCERPT.encode("latin-1")),
    )

    enriched = apply_filing_header(filing, header)

    assert filing.acceptance_time_raw == "2025-02-28T16:22:06.000Z"
    assert filing.acceptance_time == datetime(2025, 2, 28, 21, 22, 6, tzinfo=UTC)
    assert header.acceptance_time == filing.acceptance_time
    assert enriched.historical_sic == 3480
    assert enriched.historical_sic_source == header.source


def test_utc_shifted_submissions_clock_conflicts_with_authoritative_header() -> None:
    payload = deepcopy(_AXON_SUBMISSIONS_EXCERPT)
    payload["filings"]["recent"]["acceptanceDateTime"][1] = "2025-02-28T21:22:06.000Z"
    submissions = parse_submissions_payload(
        payload,
        cik=_AXON_CIK,
        source=_source("https://data.sec.gov/submissions/CIK0001069183.json"),
    )
    filing = next(
        item for item in submissions.filings if item.accession == _AXON_ORIGINAL_ACCESSION
    )
    header = parse_filing_header(
        _AXON_ORIGINAL_HEADER_EXCERPT,
        source=_source(
            "https://www.sec.gov/Archives/edgar/data/1069183/000106918325000019/"
            "0001069183-25-000019-index-headers.html",
            _AXON_ORIGINAL_HEADER_EXCERPT.encode("latin-1"),
        ),
    )

    with pytest.raises(SecEdgarParseError, match="conflicts with submissions"):
        apply_filing_header(filing, header)


def test_real_sec_revisions_are_visible_only_after_exact_acceptance_time() -> None:
    submissions, company_facts = _real_axon_datasets()
    accepted = attach_filing_lineage(company_facts, submissions)
    amended_at = datetime(2025, 5, 7, 20, 38, 11, tzinfo=UTC)

    liabilities = tuple(item for item in accepted if item.fact.tag == "LiabilitiesCurrent")
    before_amendment = max(
        (item for item in liabilities if item.acceptance_time < amended_at),
        key=lambda item: item.acceptance_time,
    )
    after_amendment = max(
        (item for item in liabilities if item.acceptance_time <= amended_at),
        key=lambda item: item.acceptance_time,
    )

    assert (
        before_amendment.fact.accession,
        before_amendment.fact.value,
        before_amendment.acceptance_time,
    ) == (
        _AXON_ORIGINAL_ACCESSION,
        Decimal("997586000"),
        datetime(2025, 2, 28, 21, 22, 6, tzinfo=UTC),
    )
    assert (
        after_amendment.fact.accession,
        after_amendment.fact.value,
        after_amendment.acceptance_time,
    ) == (
        _AXON_AMENDED_ACCESSION,
        Decimal("1677875000"),
        amended_at,
    )

    before_later_filing = canonicalize_sec_facts(
        "AXON",
        accepted,
        cutoff=datetime(2024, 12, 31, 23, 59, tzinfo=UTC),
    )
    after_later_filing = canonicalize_sec_facts(
        "AXON",
        accepted,
        cutoff=datetime(2025, 4, 1, tzinfo=UTC),
    )
    early_assets = tuple(
        fact
        for fact in before_later_filing
        if fact.metric is CanonicalFundamentalMetric.ASSETS
        and fact.period_end.isoformat() == "2023-12-31"
    )
    later_assets = tuple(
        fact
        for fact in after_later_filing
        if fact.metric is CanonicalFundamentalMetric.ASSETS
        and fact.period_end.isoformat() == "2023-12-31"
    )

    assert [(fact.accession, fact.value) for fact in early_assets] == [
        (_AXON_PRIOR_ACCESSION, Decimal("3436845000"))
    ]
    assert max(later_assets, key=lambda fact: fact.acceptance_time).value == Decimal("3409174000")
    assert all(fact.accession != _AXON_AMENDED_ACCESSION for fact in after_later_filing)


def test_real_sec_quality_reports_required_operating_profitability() -> None:
    submissions, company_facts = _real_axon_datasets()
    cutoff = datetime(2025, 4, 1, tzinfo=UTC)
    facts = canonicalize_sec_facts(
        "AXON",
        attach_filing_lineage(company_facts, submissions),
        cutoff=cutoff,
    )
    evidence = IssuerFundamentalEvidence(
        symbol="AXON",
        peer_group="industrials",
        cutoff=cutoff,
        historical_sic=3480,
        classification_available_at=datetime(2025, 2, 28, 21, 22, 6, tzinfo=UTC),
        classification_source_id=_AXON_ORIGINAL_ACCESSION,
        facts=facts,
        market_cap=None,
    )
    result = calculate_fundamental_panel(
        (evidence,),
        FundamentalCalculationConfig(
            max_fundamental_age_days=800,
            minimum_peer_count=2,
            winsorize_limit=3.0,
        ),
    )

    operating_profitability = next(
        component
        for component in result.calculations[0].components
        if component.name == "operating_profitability"
    )
    quality = next(
        observation
        for observation in result.sleeve_observations
        if observation.factor_name == FundamentalSleeve.QUALITY.value
    )

    assert operating_profitability.value is None
    assert (
        operating_profitability.missing_reason
        == "operating_income is missing for annual period 2023-12-31"
    )
    assert all(
        component.name != "gross_profitability" for component in result.calculations[0].components
    )
    assert quality.raw_value is None
    assert quality.missing_reason is not None
    assert (
        "operating_profitability: operating_income is missing for annual period 2023-12-31"
        in quality.missing_reason
    )


def test_real_sec_excerpt_completeness_failures_are_rejected() -> None:
    incomplete_submissions = deepcopy(_AXON_SUBMISSIONS_EXCERPT)
    incomplete_submissions["filings"]["recent"]["acceptanceDateTime"].pop()
    with pytest.raises(SecEdgarParseError, match="required columns have unequal lengths"):
        parse_submissions_payload(
            incomplete_submissions,
            cik=_AXON_CIK,
            source=_source("https://data.sec.gov/submissions/CIK0001069183.json"),
        )

    incomplete_facts = deepcopy(_AXON_COMPANY_FACTS_EXCERPT)
    del incomplete_facts["facts"]["us-gaap"]["Assets"]["units"]["USD"][0]["accn"]
    with pytest.raises(SecEdgarParseError, match="SEC fact accession"):
        parse_company_facts_payload(
            incomplete_facts,
            cik=_AXON_CIK,
            source=_source("https://data.sec.gov/api/xbrl/companyfacts/CIK0001069183.json"),
        )


def test_facts_keep_original_and_amended_accessions_with_acceptance_lineage() -> None:
    submissions = parse_submissions_payload(
        _root_submissions_payload(),
        cik=_CIK,
        source=_source("https://data.sec.gov/submissions/CIK0000000001.json"),
    )
    facts = parse_company_facts_payload(
        _company_facts_payload(),
        cik=_CIK,
        source=_source("https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json"),
    )

    accepted = attach_filing_lineage(facts, submissions)

    assert [item.fact.accession for item in accepted] == [
        _ORIGINAL_ACCESSION,
        _AMENDED_ACCESSION,
    ]
    assert [item.fact.value for item in accepted] == [Decimal(100), Decimal(90)]
    assert [item.acceptance_time for item in accepted] == [
        datetime(2024, 2, 1, 21, tzinfo=UTC),
        datetime(2024, 3, 1, 22, tzinfo=UTC),
    ]
    assert [item.acceptance_time_raw for item in accepted] == [
        "2024-02-01T16:00:00.000Z",
        "2024-03-01T17:00:00.000Z",
    ]
    assert all(item.filing_source == submissions.sources[0] for item in accepted)
    assert all(item.historical_sic is None for item in accepted)
    assert submissions.current_sic == 9999


def test_current_sic_is_not_historically_attached_without_filing_header() -> None:
    submissions = parse_submissions_payload(
        _root_submissions_payload(),
        cik=_CIK,
        source=_source("https://data.sec.gov/submissions/CIK0000000001.json"),
    )
    header_source = _source(
        "https://www.sec.gov/Archives/edgar/data/1/000000000124000001/"
        "0000000001-24-000001-index-headers.html"
    )
    header = parse_filing_header(
        _filing_header(
            accession=_ORIGINAL_ACCESSION,
            accepted="20240201160000",
            form="10-K",
            filed="20240201",
        ),
        source=header_source,
    )

    enriched = apply_filing_header(submissions.filings[0], header)

    assert enriched.historical_sic == 9999
    assert enriched.historical_sic_source == header_source
    assert header.acceptance_time == datetime(2024, 2, 1, 21, tzinfo=UTC)


def test_client_fetches_compact_index_header_for_exact_accession() -> None:
    submissions = parse_submissions_payload(
        _root_submissions_payload(),
        cik=_CIK,
        source=_source("https://data.sec.gov/submissions/CIK0000000001.json"),
    )
    expected_path = (
        "/Archives/edgar/data/1/000000000124000001/0000000001-24-000001-index-headers.html"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == expected_path
        return httpx.Response(
            200,
            text=_filing_header(
                accession=_ORIGINAL_ACCESSION,
                accepted="20240201160000",
                form="10-K",
                filed="20240201",
            ),
        )

    header = _client(handler).fetch_filing_header(submissions.filings[0])

    assert header.accession == _ORIGINAL_ACCESSION
    assert header.filers[0].sic == 9999


def test_parser_uses_full_escaped_header_inside_real_index_html_shape() -> None:
    compact_comment = """<!--
<SEC-HEADER>0000000001-24-000001.hdr.sgml
<FILING-DATE>20240201
<TYPE>10-K
</SEC-HEADER>
-->"""
    full_header = _filing_header(
        accession=_ORIGINAL_ACCESSION,
        accepted="20240201160000",
        form="10-K",
        filed="20240201",
    )
    wrapped = f"<HTML><HEAD>{compact_comment}</HEAD><BODY><PRE>{escape(full_header)}</PRE>"

    parsed = parse_filing_header(
        wrapped,
        source=_source(
            "https://www.sec.gov/Archives/edgar/data/1/000000000124000001/"
            "0000000001-24-000001-index-headers.html",
            wrapped.encode("latin-1"),
        ),
    )

    assert parsed.accession == _ORIGINAL_ACCESSION
    assert parsed.filing_date == date(2024, 2, 1)
    assert parsed.filers[0].sic == 9999


def test_submissions_acceptance_without_sec_z_marker_fails_closed() -> None:
    payload = _root_submissions_payload()
    payload["filings"]["recent"] = _filing_columns(
        accessions=[_ORIGINAL_ACCESSION],
        acceptances=["2024-02-01T16:00:00"],
        forms=["10-K"],
        filing_dates=["2024-02-01"],
    )
    payload["filings"]["recent"]["reportDate"] = ["2023-12-31"]
    payload["filings"]["recent"]["primaryDocument"] = ["original.htm"]
    payload["filings"]["recent"]["items"] = [""]
    payload["filings"]["recent"]["isXBRL"] = [1]
    payload["filings"]["recent"]["isInlineXBRL"] = [1]

    with pytest.raises(SecEdgarParseError, match="ending in Z"):
        parse_submissions_payload(
            payload,
            cik=_CIK,
            source=_source("https://data.sec.gov/submissions/CIK0000000001.json"),
        )


@pytest.mark.parametrize(
    "raw",
    [
        "2024-03-10T02:30:00.000Z",
        "2024-11-03T01:30:00.000Z",
    ],
)
def test_submissions_acceptance_rejects_dst_gap_and_fold(raw: str) -> None:
    with pytest.raises(SecEdgarParseError, match="ambiguous or nonexistent"):
        parse_submissions_acceptance_time(raw)


def test_fact_without_submission_accession_fails_closed() -> None:
    submissions_payload = _root_submissions_payload()
    submissions_payload["filings"]["recent"] = _filing_columns(
        accessions=[_ORIGINAL_ACCESSION],
        acceptances=["2024-02-01T16:00:00.000Z"],
        forms=["10-K"],
        filing_dates=["2024-02-01"],
    )
    for name in (
        "reportDate",
        "primaryDocument",
        "items",
        "isXBRL",
        "isInlineXBRL",
    ):
        submissions_payload["filings"]["recent"][name] = submissions_payload["filings"]["recent"][
            name
        ][:1]
    submissions = parse_submissions_payload(
        submissions_payload,
        cik=_CIK,
        source=_source("https://data.sec.gov/submissions/CIK0000000001.json"),
    )
    facts = parse_company_facts_payload(
        _company_facts_payload(),
        cik=_CIK,
        source=_source("https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json"),
    )

    with pytest.raises(SecEdgarParseError, match=_AMENDED_ACCESSION):
        attach_filing_lineage(facts, submissions)


def test_client_paginates_and_hashes_exact_sec_response_bytes() -> None:
    root_payload = _root_submissions_payload()
    root_payload["filings"]["files"] = [{"name": "CIK0000000001-submissions-001.json"}]
    page_payload = _filing_columns(
        accessions=["0000000001-23-000003"],
        acceptances=["2023-02-01T16:00:00.000Z"],
        forms=["10-K"],
        filing_dates=["2023-02-01"],
    )
    page_payload["reportDate"] = ["2022-12-31"]
    page_payload["primaryDocument"] = ["older.htm"]
    page_payload["items"] = [""]
    page_payload["isXBRL"] = [1]
    page_payload["isInlineXBRL"] = [1]
    bodies = {
        "/submissions/CIK0000000001.json": httpx.Response(200, json=root_payload),
        "/submissions/CIK0000000001-submissions-001.json": httpx.Response(
            200,
            json=page_payload,
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == _USER_AGENT
        assert request.extensions["timeout"]["read"] == default_test_timeout
        return bodies[request.url.path]

    default_test_timeout = 30.0
    client = _client(handler)
    dataset = client.fetch_submissions(_CIK)

    assert len(dataset.filings) == 3
    assert len(dataset.sources) == 2
    assert (
        dataset.sources[0].content_sha256
        == hashlib.sha256(bodies["/submissions/CIK0000000001.json"].content).hexdigest()
    )


def test_rate_limit_retry_after_is_bounded_and_retried() -> None:
    requests = 0
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                httpx.codes.TOO_MANY_REQUESTS,
                headers={"Retry-After": "600"},
            )
        return httpx.Response(200, json=_root_submissions_payload())

    client = _client(handler, sleep=delays.append)
    dataset = client.fetch_submissions(_CIK)

    assert dataset.cik == _CIK
    assert requests == 2
    assert max(delays) == 60.0


def test_transport_timeout_retries_then_succeeds() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ReadTimeout("protocol timeout", request=request)
        return httpx.Response(200, json=_root_submissions_payload())

    dataset = _client(handler).fetch_submissions(_CIK)

    assert dataset.cik == _CIK
    assert requests == 2


def test_permanent_http_error_is_not_retried() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(httpx.codes.NOT_FOUND)

    with pytest.raises(SecEdgarRequestError, match="HTTP 404"):
        _client(handler).fetch_submissions(_CIK)
    assert requests == 1


def test_unsafe_historical_page_name_is_rejected_before_second_request() -> None:
    payload = _root_submissions_payload()
    payload["filings"]["files"] = [{"name": "../outside.json"}]
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=payload)

    with pytest.raises(SecEdgarParseError, match="Unsafe"):
        _client(handler).fetch_submissions(_CIK)
    assert requests == 1
