"""Strict SEC EDGAR submissions and Company Facts acquisition.

This adapter preserves source documents and filing-time lineage.  It deliberately
does not calculate investment factors or persist observations: consumers first
join each XBRL fact to its exact filing accession, then apply a decision cutoff.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from lib_common.http_exceptions import APIError, is_retryable_status, raise_for_status
from lib_common.logging import get_logger
from lib_common.retries import retry_sync

logger = get_logger(__name__)

_DATA_BASE_URL = "https://data.sec.gov"
_WWW_BASE_URL = "https://www.sec.gov"
_ARCHIVES_BASE_URL = "https://www.sec.gov/Archives/edgar/data"
_COMPANY_TICKERS_URL = f"{_WWW_BASE_URL}/files/company_tickers.json"
_CIK_WIDTH = 10
_ACCESSION_DIGITS = 18
_ACCESSION_PATTERN = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_SUBMISSIONS_ACCEPTANCE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.json$")
_CONTACT_PATTERN = re.compile(r"(?:[^@\s]+@[^@\s]+\.[^@\s]+|https?://\S+)")
_MAX_SEC_REQUESTS_PER_SECOND = 10.0
_DEFAULT_REQUESTS_PER_SECOND = 8.0
_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_RETRIES = 2
_DEFAULT_RETRY_BASE_DELAY_SECONDS = 0.5
_DEFAULT_RETRY_MAX_DELAY_SECONDS = 5.0
_DEFAULT_MAX_RETRY_AFTER_SECONDS = 60.0
_MAX_TIMEOUT_SECONDS = 120.0
_MAX_RETRIES = 5
_MAX_USER_AGENT_LENGTH = 256
_HEADER_SCAN_LIMIT = 1_000_000
_SEC_HEADER_TIMEZONE = ZoneInfo("America/New_York")
_ASCII_CONTROL_BOUNDARY = 32
_ASCII_DELETE = 127
_MIN_USER_AGENT_PARTS = 2
_SIC_WIDTH = 4


def _is_supported_sec_endpoint(url: str) -> bool:
    return url == _COMPANY_TICKERS_URL or url.startswith(
        (f"{_DATA_BASE_URL}/", f"{_ARCHIVES_BASE_URL}/")
    )


class SecEdgarError(RuntimeError):
    """Base error for SEC acquisition and parsing failures."""


class SecEdgarParseError(SecEdgarError):
    """SEC content does not satisfy the required point-in-time contract."""


class SecEdgarRequestError(SecEdgarError):
    """SEC content could not be acquired within the configured request policy."""


class _RetryableSecEdgarRequestError(SecEdgarRequestError):
    """Internal marker for transport and retryable HTTP failures."""


class _HttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> httpx.Response: ...

    def close(self) -> None: ...


FactValue = Decimal | str | bool


@dataclass(frozen=True, slots=True)
class SecEdgarClientConfig:
    """Validated SEC fair-access and transport policy."""

    user_agent: str
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    requests_per_second: float = _DEFAULT_REQUESTS_PER_SECOND
    retries: int = _DEFAULT_RETRIES
    retry_base_delay_seconds: float = _DEFAULT_RETRY_BASE_DELAY_SECONDS
    retry_max_delay_seconds: float = _DEFAULT_RETRY_MAX_DELAY_SECONDS
    max_retry_after_seconds: float = _DEFAULT_MAX_RETRY_AFTER_SECONDS

    def __post_init__(self) -> None:
        normalized_user_agent = validate_edgar_user_agent(self.user_agent)
        object.__setattr__(self, "user_agent", normalized_user_agent)
        _require_finite_range(
            self.timeout_seconds,
            name="SEC timeout_seconds",
            minimum=0.001,
            maximum=_MAX_TIMEOUT_SECONDS,
        )
        _require_finite_range(
            self.requests_per_second,
            name="SEC requests_per_second",
            minimum=0.001,
            maximum=_MAX_SEC_REQUESTS_PER_SECOND,
        )
        if not 0 <= self.retries <= _MAX_RETRIES:
            msg = f"SEC retries must be between 0 and {_MAX_RETRIES}"
            raise ValueError(msg)
        _require_finite_range(
            self.retry_base_delay_seconds,
            name="SEC retry_base_delay_seconds",
            minimum=0.0,
            maximum=_MAX_TIMEOUT_SECONDS,
        )
        _require_finite_range(
            self.retry_max_delay_seconds,
            name="SEC retry_max_delay_seconds",
            minimum=self.retry_base_delay_seconds,
            maximum=_MAX_TIMEOUT_SECONDS,
        )
        _require_finite_range(
            self.max_retry_after_seconds,
            name="SEC max_retry_after_seconds",
            minimum=0.0,
            maximum=_MAX_TIMEOUT_SECONDS,
        )


@dataclass(frozen=True, slots=True)
class SecSourceDocument:
    """Content-addressed SEC response used by parsed records."""

    endpoint: str
    retrieved_at: datetime
    content_sha256: str

    def __post_init__(self) -> None:
        if not _is_supported_sec_endpoint(self.endpoint):
            msg = f"Unsupported SEC endpoint: {self.endpoint!r}"
            raise ValueError(msg)
        object.__setattr__(self, "retrieved_at", _require_aware_utc(self.retrieved_at))
        if _SHA256_PATTERN.fullmatch(self.content_sha256) is None:
            msg = "SEC source content_sha256 must be a lowercase SHA-256 digest"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SecCompanyTicker:
    """One current SEC ticker identity from the exact published catalogue."""

    cik: str
    ticker: str
    title: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "cik", normalize_cik(self.cik))
        ticker = _required_text(self.ticker, name="SEC company ticker").upper()
        if any(character.isspace() for character in ticker):
            msg = "SEC company ticker must not contain whitespace"
            raise ValueError(msg)
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(
            self,
            "title",
            _required_text(self.title, name="SEC company title"),
        )


@dataclass(frozen=True, slots=True)
class SecCompanyTickerDataset:
    """Current ticker catalogue retaining its exact SEC source response."""

    entries: tuple[SecCompanyTicker, ...]
    source: SecSourceDocument

    def __post_init__(self) -> None:
        if not self.entries:
            msg = "SEC company ticker catalogue must not be empty"
            raise ValueError(msg)
        if any(not isinstance(entry, SecCompanyTicker) for entry in self.entries):
            msg = "SEC company ticker catalogue contains an invalid entry"
            raise TypeError(msg)
        identities = tuple((entry.cik, entry.ticker) for entry in self.entries)
        if len(identities) != len(set(identities)):
            msg = "SEC company ticker catalogue contains duplicate identities"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SecCompanyTickerResponse:
    """Parsed current ticker catalogue plus the exact SEC response bytes."""

    dataset: SecCompanyTickerDataset
    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, SecCompanyTickerDataset):
            msg = "SEC company ticker response dataset is invalid"
            raise TypeError(msg)
        if not isinstance(self.content, bytes):
            msg = "SEC company ticker response content must be bytes"
            raise TypeError(msg)
        if hashlib.sha256(self.content).hexdigest() != self.dataset.source.content_sha256:
            msg = "SEC company ticker response content differs from its source SHA-256"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SecFilingRecord:
    """One filing from the submissions API, retaining its exact source page."""

    cik: str
    accession: str
    acceptance_time_raw: str
    acceptance_time: datetime
    filing_date: date
    form: str
    report_date: date | None
    primary_document: str | None
    items: tuple[str, ...]
    is_xbrl: bool
    is_inline_xbrl: bool
    source: SecSourceDocument
    historical_sic: int | None = None
    historical_sic_source: SecSourceDocument | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cik", normalize_cik(self.cik))
        object.__setattr__(self, "accession", normalize_accession(self.accession))
        normalized_acceptance = _require_aware_utc(self.acceptance_time)
        if normalized_acceptance != parse_submissions_acceptance_time(self.acceptance_time_raw):
            msg = "SEC filing acceptance_time differs from its raw submissions value"
            raise ValueError(msg)
        object.__setattr__(self, "acceptance_time", normalized_acceptance)
        if (self.historical_sic is None) != (self.historical_sic_source is None):
            msg = "SEC historical SIC and filing-header source must be present together"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SecSubmissionDataset:
    """Complete paginated submission history for one CIK."""

    cik: str
    entity_name: str | None
    current_sic: int | None
    filings: tuple[SecFilingRecord, ...]
    sources: tuple[SecSourceDocument, ...]


@dataclass(frozen=True, slots=True)
class SecXbrlFact:
    """One Company Facts unit observation before filing-time attachment."""

    cik: str
    taxonomy: str
    tag: str
    label: str | None
    description: str | None
    unit: str
    value: FactValue
    start: date | None
    end: date
    accession: str
    fiscal_year: int | None
    fiscal_period: str | None
    form: str
    filed: date
    frame: str | None
    source: SecSourceDocument


@dataclass(frozen=True, slots=True)
class SecCompanyFactsDataset:
    """Company Facts payload retaining all accessions and units."""

    cik: str
    entity_name: str | None
    facts: tuple[SecXbrlFact, ...]
    source: SecSourceDocument


@dataclass(frozen=True, slots=True)
class SecAcceptedFact:
    """XBRL fact joined to the filing that made it public."""

    fact: SecXbrlFact
    acceptance_time_raw: str
    acceptance_time: datetime
    historical_sic: int | None
    filing_source: SecSourceDocument
    historical_sic_source: SecSourceDocument | None

    def __post_init__(self) -> None:
        normalized_acceptance = _require_aware_utc(self.acceptance_time)
        if normalized_acceptance != parse_submissions_acceptance_time(self.acceptance_time_raw):
            msg = "SEC accepted fact differs from its raw submissions acceptance value"
            raise ValueError(msg)
        object.__setattr__(self, "acceptance_time", normalized_acceptance)
        if (self.historical_sic is None) != (self.historical_sic_source is None):
            msg = "SEC accepted fact SIC and filing-header source must be present together"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SecFilerIdentity:
    """Filer identity as it appeared in one immutable filing header."""

    cik: str
    company_name: str | None
    sic: int | None


@dataclass(frozen=True, slots=True)
class SecFilingHeader:
    """Point-in-time filing metadata parsed from the complete submission header."""

    accession: str
    acceptance_time: datetime
    filing_date: date
    form: str
    report_date: date | None
    filers: tuple[SecFilerIdentity, ...]
    source: SecSourceDocument


@dataclass(frozen=True, slots=True)
class _FetchedDocument:
    content: bytes
    source: SecSourceDocument


def validate_edgar_user_agent(raw: str) -> str:
    """Require an organization identity and operator contact without header controls."""
    value = raw.strip()
    if not value:
        msg = "SEC EDGAR requires a descriptive User-Agent with operator contact"
        raise ValueError(msg)
    if len(value) > _MAX_USER_AGENT_LENGTH:
        msg = f"SEC User-Agent must be at most {_MAX_USER_AGENT_LENGTH} characters"
        raise ValueError(msg)
    if any(
        ord(character) < _ASCII_CONTROL_BOUNDARY or ord(character) == _ASCII_DELETE
        for character in value
    ):
        msg = "SEC User-Agent must not contain control characters"
        raise ValueError(msg)
    if len(value.split()) < _MIN_USER_AGENT_PARTS or _CONTACT_PATTERN.search(value) is None:
        msg = "SEC User-Agent must include an organization identity and email or URL contact"
        raise ValueError(msg)
    return value


def normalize_cik(raw: str | int) -> str:
    """Return the SEC ten-digit CIK form."""
    value = str(raw).strip()
    if not value.isdigit() or len(value) > _CIK_WIDTH:
        msg = f"Invalid SEC CIK: {raw!r}"
        raise ValueError(msg)
    return value.zfill(_CIK_WIDTH)


def normalize_accession(raw: str) -> str:
    """Return the canonical dashed SEC accession form."""
    value = raw.strip()
    digits = value.replace("-", "")
    if not digits.isdigit() or len(digits) != _ACCESSION_DIGITS:
        msg = f"Invalid SEC accession: {raw!r}"
        raise ValueError(msg)
    normalized = f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"
    if _ACCESSION_PATTERN.fullmatch(normalized) is None:
        msg = f"Invalid SEC accession: {raw!r}"
        raise ValueError(msg)
    return normalized


def parse_company_tickers_payload(
    payload: Mapping[str, Any],
    *,
    source: SecSourceDocument,
) -> SecCompanyTickerDataset:
    """Parse the SEC current ticker catalogue without inferring historical identity."""

    entries: list[SecCompanyTicker] = []
    for key in sorted(payload, key=lambda item: (not str(item).isdigit(), str(item))):
        raw_entry = _require_mapping(payload[key], name=f"company_tickers[{key!r}]")
        raw_cik = raw_entry.get("cik_str")
        if isinstance(raw_cik, bool) or not isinstance(raw_cik, (str, int)):
            msg = f"company_tickers[{key!r}].cik_str must be a string or integer"
            raise SecEdgarParseError(msg)
        entries.append(
            SecCompanyTicker(
                cik=normalize_cik(raw_cik),
                ticker=_required_text(
                    raw_entry.get("ticker"),
                    name=f"company_tickers[{key!r}].ticker",
                ),
                title=_required_text(
                    raw_entry.get("title"),
                    name=f"company_tickers[{key!r}].title",
                ),
            )
        )
    return SecCompanyTickerDataset(entries=tuple(entries), source=source)


def parse_submissions_payload(
    payload: Mapping[str, Any],
    *,
    cik: str | int,
    source: SecSourceDocument,
) -> SecSubmissionDataset:
    """Parse either a root submissions response or one historical page."""
    normalized_cik = normalize_cik(cik)
    payload_cik = payload.get("cik")
    if payload_cik is not None and normalize_cik(str(payload_cik)) != normalized_cik:
        msg = f"SEC submissions CIK does not match requested CIK {normalized_cik}"
        raise SecEdgarParseError(msg)

    filings_container = payload.get("filings")
    if filings_container is None:
        columns = payload
        entity_name = None
        current_sic = None
    else:
        filings_mapping = _require_mapping(filings_container, name="submissions.filings")
        columns = _require_mapping(filings_mapping.get("recent"), name="submissions.filings.recent")
        entity_name = _optional_text(payload.get("name"))
        current_sic = _optional_sic(payload.get("sic"), name="submissions.sic")

    filings = _parse_filing_columns(columns, cik=normalized_cik, source=source)
    return SecSubmissionDataset(
        cik=normalized_cik,
        entity_name=entity_name,
        current_sic=current_sic,
        filings=filings,
        sources=(source,),
    )


def parse_company_facts_payload(
    payload: Mapping[str, Any],
    *,
    cik: str | int,
    source: SecSourceDocument,
) -> SecCompanyFactsDataset:
    """Parse Company Facts without collapsing accessions, contexts, or units."""
    normalized_cik = normalize_cik(cik)
    payload_cik = payload.get("cik")
    if payload_cik is None or normalize_cik(str(payload_cik)) != normalized_cik:
        msg = f"SEC Company Facts CIK does not match requested CIK {normalized_cik}"
        raise SecEdgarParseError(msg)
    taxonomy_map = _require_mapping(payload.get("facts"), name="companyfacts.facts")
    facts: list[SecXbrlFact] = []
    for taxonomy in sorted(taxonomy_map):
        concepts = _require_mapping(taxonomy_map[taxonomy], name=f"facts.{taxonomy}")
        for tag in sorted(concepts):
            concept = _require_mapping(concepts[tag], name=f"facts.{taxonomy}.{tag}")
            units = _require_mapping(concept.get("units"), name=f"facts.{taxonomy}.{tag}.units")
            label = _optional_text(concept.get("label"))
            description = _optional_text(concept.get("description"))
            for unit in sorted(units):
                observations = _require_sequence(
                    units[unit],
                    name=f"facts.{taxonomy}.{tag}.units.{unit}",
                )
                for index, raw_observation in enumerate(observations):
                    observation = _require_mapping(
                        raw_observation,
                        name=f"facts.{taxonomy}.{tag}.units.{unit}[{index}]",
                    )
                    facts.append(
                        _parse_fact(
                            observation,
                            cik=normalized_cik,
                            taxonomy=taxonomy,
                            tag=tag,
                            label=label,
                            description=description,
                            unit=unit,
                            source=source,
                        )
                    )
    facts.sort(key=_fact_sort_key)
    return SecCompanyFactsDataset(
        cik=normalized_cik,
        entity_name=_optional_text(payload.get("entityName")),
        facts=tuple(facts),
        source=source,
    )


def attach_filing_lineage(
    company_facts: SecCompanyFactsDataset,
    submissions: SecSubmissionDataset,
) -> tuple[SecAcceptedFact, ...]:
    """Join every fact to its exact accession; incomplete availability fails closed."""
    if company_facts.cik != submissions.cik:
        msg = "SEC Company Facts and submissions CIKs differ"
        raise SecEdgarParseError(msg)
    filing_by_accession = {filing.accession: filing for filing in submissions.filings}
    accepted: list[SecAcceptedFact] = []
    for fact in company_facts.facts:
        filing = filing_by_accession.get(fact.accession)
        if filing is None:
            msg = f"SEC fact accession {fact.accession} has no filing acceptance lineage"
            raise SecEdgarParseError(msg)
        if fact.form != filing.form or fact.filed != filing.filing_date:
            msg = f"SEC fact filing metadata conflicts for accession {fact.accession}"
            raise SecEdgarParseError(msg)
        accepted.append(
            SecAcceptedFact(
                fact=fact,
                acceptance_time_raw=filing.acceptance_time_raw,
                acceptance_time=filing.acceptance_time,
                historical_sic=filing.historical_sic,
                filing_source=filing.source,
                historical_sic_source=filing.historical_sic_source,
            )
        )
    return tuple(accepted)


def parse_filing_header(
    content: str,
    *,
    source: SecSourceDocument,
) -> SecFilingHeader:
    """Parse the ASCII SEC header from a complete-submission text document."""
    header = _header_prefix(content)
    accession = normalize_accession(
        _required_header_value(
            header,
            angle_name="ACCESSION-NUMBER",
            label_name="ACCESSION NUMBER",
        )
    )
    acceptance_time = _parse_header_acceptance(
        _required_header_value(
            header,
            angle_name="ACCEPTANCE-DATETIME",
            label_name="ACCEPTANCE-DATETIME",
        )
    )
    filing_date = _parse_compact_or_iso_date(
        _required_header_value(
            header,
            angle_name="FILED-AS-OF-DATE",
            label_name="FILED AS OF DATE",
        ),
        name="filing header filing date",
    )
    form = _required_header_value(
        header,
        angle_name="CONFORMED-SUBMISSION-TYPE",
        label_name="CONFORMED SUBMISSION TYPE",
    )
    report_date_raw = _optional_header_value(
        header,
        angle_name="CONFORMED-PERIOD-OF-REPORT",
        label_name="CONFORMED PERIOD OF REPORT",
    )
    report_date = (
        _parse_compact_or_iso_date(report_date_raw, name="filing header report date")
        if report_date_raw
        else None
    )
    filers = _parse_filer_identities(header)
    if not filers:
        msg = f"SEC filing header {accession} has no filer CIK"
        raise SecEdgarParseError(msg)
    return SecFilingHeader(
        accession=accession,
        acceptance_time=acceptance_time,
        filing_date=filing_date,
        form=form,
        report_date=report_date,
        filers=filers,
        source=source,
    )


def apply_filing_header(
    filing: SecFilingRecord,
    header: SecFilingHeader,
) -> SecFilingRecord:
    """Attach historical SIC only after exact filing-header reconciliation."""
    if (
        filing.accession != header.accession
        or filing.acceptance_time != header.acceptance_time
        or filing.filing_date != header.filing_date
        or filing.form != header.form
        or (
            filing.report_date is not None
            and header.report_date is not None
            and filing.report_date != header.report_date
        )
    ):
        msg = f"SEC filing header conflicts with submissions accession {filing.accession}"
        raise SecEdgarParseError(msg)
    filer_by_cik = {filer.cik: filer for filer in header.filers}
    filer = filer_by_cik.get(filing.cik)
    if filer is None:
        msg = f"SEC filing header {header.accession} has no matching filer CIK {filing.cik}"
        raise SecEdgarParseError(msg)
    if filer.sic is None:
        msg = f"SEC filing header {header.accession} has no historical SIC for {filing.cik}"
        raise SecEdgarParseError(msg)
    return replace(
        filing,
        historical_sic=filer.sic,
        historical_sic_source=header.source,
    )


def parse_submissions_acceptance_time(raw: str) -> datetime:
    """Interpret the SEC submissions timestamp as an Eastern wall clock.

    EDGAR submissions JSON appends ``Z`` to ``acceptanceDateTime``, but the
    clock value matches the authoritative compact filing-header timestamp in
    ``America/New_York`` rather than UTC.  Treating the suffix as ISO-8601 UTC
    carries filings backward by four or five hours and creates look-ahead.
    The original string remains on :class:`SecFilingRecord`; filing-header
    reconciliation must agree with this normalized instant exactly.
    """

    if (
        not isinstance(raw, str)
        or raw != raw.strip()
        or _SUBMISSIONS_ACCEPTANCE_PATTERN.fullmatch(raw) is None
    ):
        msg = (
            "SEC submissions acceptance timestamp must be a canonical "
            "EDGAR wall-clock value ending in Z"
        )
        raise SecEdgarParseError(msg)
    try:
        wall_clock = datetime.fromisoformat(raw[:-1])
    except ValueError as exc:
        msg = f"Invalid SEC submissions acceptance timestamp: {raw!r}"
        raise SecEdgarParseError(msg) from exc
    return _localize_unambiguous_eastern(wall_clock, source="SEC submissions")


class SecEdgarClient:
    """Synchronous SEC client with bounded retries and fair-access throttling."""

    def __init__(
        self,
        config: SecEdgarClientConfig,
        *,
        client: _HttpClient | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._client: _HttpClient = client or httpx.Client()
        self._owns_client = client is None
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._utc_now = utc_now or (lambda: datetime.now(tz=UTC))
        self._headers = {
            "User-Agent": config.user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        self._request_interval = 1.0 / config.requests_per_second
        self._last_request_monotonic: float | None = None
        self._blocked_until_monotonic = 0.0
        self._rate_lock = threading.Lock()
        self._closed = False

    def __enter__(self) -> SecEdgarClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close only the internally owned HTTP client."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            self._client.close()

    def fetch_company_tickers(self) -> Mapping[str, Any]:
        """Fetch the SEC's current ticker-to-CIK catalogue through this client."""

        payload, _source = self._get_json_document(_COMPANY_TICKERS_URL)
        return payload

    def fetch_company_ticker_dataset(self) -> SecCompanyTickerDataset:
        """Fetch the current ticker catalogue with content and retrieval lineage."""

        return self.fetch_company_ticker_response().dataset

    def fetch_company_ticker_response(self) -> SecCompanyTickerResponse:
        """Fetch the parsed catalogue while retaining its exact response bytes."""

        document = self._get_document(_COMPANY_TICKERS_URL)
        payload = self._decode_json_document(
            document,
            url=_COMPANY_TICKERS_URL,
        )
        return SecCompanyTickerResponse(
            dataset=parse_company_tickers_payload(payload, source=document.source),
            content=document.content,
        )

    def fetch_submissions(
        self,
        cik: str | int,
        *,
        since: date | None = None,
    ) -> SecSubmissionDataset:
        """Fetch the root and every declared historical submissions page."""
        normalized_cik = normalize_cik(cik)
        root_url = f"{_DATA_BASE_URL}/submissions/CIK{normalized_cik}.json"
        root_payload, root_source = self._get_json_document(root_url)
        datasets = [parse_submissions_payload(root_payload, cik=normalized_cik, source=root_source)]
        for page_name in _submission_page_names(root_payload, since=since):
            page_url = f"{_DATA_BASE_URL}/submissions/{page_name}"
            page_payload, page_source = self._get_json_document(page_url)
            datasets.append(
                parse_submissions_payload(page_payload, cik=normalized_cik, source=page_source)
            )
        return _merge_submission_datasets(datasets)

    def fetch_company_facts(self, cik: str | int) -> SecCompanyFactsDataset:
        """Fetch all standardized Company Facts for one CIK."""
        normalized_cik = normalize_cik(cik)
        url = f"{_DATA_BASE_URL}/api/xbrl/companyfacts/CIK{normalized_cik}.json"
        payload, source = self._get_json_document(url)
        return parse_company_facts_payload(payload, cik=normalized_cik, source=source)

    def fetch_filing_header(self, filing: SecFilingRecord) -> SecFilingHeader:
        """Fetch the compact SEC index-header document for one filing."""
        accession_path = filing.accession.replace("-", "")
        cik_path = str(int(filing.cik))
        url = (
            f"{_ARCHIVES_BASE_URL}/{cik_path}/{accession_path}/"
            f"{filing.accession}-index-headers.html"
        )
        document = self._get_document(url)
        content = document.content.decode("latin-1")
        return parse_filing_header(content, source=document.source)

    def _get_json_document(
        self,
        url: str,
    ) -> tuple[Mapping[str, Any], SecSourceDocument]:
        document = self._get_document(url)
        return self._decode_json_document(document, url=url), document.source

    @staticmethod
    def _decode_json_document(
        document: _FetchedDocument,
        *,
        url: str,
    ) -> Mapping[str, Any]:
        try:
            payload = json.loads(
                document.content,
                parse_float=Decimal,
                parse_int=int,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = f"SEC returned invalid JSON for {url}"
            raise SecEdgarParseError(msg) from exc
        return _require_mapping(payload, name=f"SEC response {url}")

    def _get_document(self, url: str) -> _FetchedDocument:
        if self._closed:
            msg = "SEC EDGAR client is closed"
            raise SecEdgarRequestError(msg)
        if not _is_supported_sec_endpoint(url):
            msg = f"Unsupported SEC endpoint: {url!r}"
            raise SecEdgarRequestError(msg)

        def request_once() -> _FetchedDocument:
            self._throttle()
            try:
                response = self._client.get(
                    url,
                    headers=self._headers,
                    timeout=self._config.timeout_seconds,
                )
            except httpx.RequestError as exc:
                msg = f"SEC request transport failure for {url}: {type(exc).__name__}"
                raise _RetryableSecEdgarRequestError(msg) from exc
            if is_retryable_status(response.status_code):
                self._register_retry_after(response)
                msg = f"SEC request received retryable HTTP {response.status_code} for {url}"
                raise _RetryableSecEdgarRequestError(msg)
            if response.is_error:
                try:
                    raise_for_status(
                        response.status_code,
                        request_url=url,
                        response_body=response.text[:512],
                    )
                except APIError as exc:
                    msg = f"SEC request failed with HTTP {response.status_code} for {url}"
                    raise SecEdgarRequestError(msg) from exc
            retrieved_at = _require_aware_utc(self._utc_now())
            content = response.content
            source = SecSourceDocument(
                endpoint=url,
                retrieved_at=retrieved_at,
                content_sha256=hashlib.sha256(content).hexdigest(),
            )
            return _FetchedDocument(content=content, source=source)

        try:
            document = retry_sync(
                request_once,
                retries=self._config.retries,
                base_delay=self._config.retry_base_delay_seconds,
                max_delay=self._config.retry_max_delay_seconds,
                jitter=0.0,
                retry_on=(_RetryableSecEdgarRequestError,),
                operation_name=f"SEC GET {url}",
                log=logger,
            )
        except _RetryableSecEdgarRequestError as exc:
            msg = f"SEC request exhausted retries for {url}"
            raise SecEdgarRequestError(msg) from exc
        else:
            return document

    def _throttle(self) -> None:
        with self._rate_lock:
            now = self._monotonic()
            earliest = self._blocked_until_monotonic
            if self._last_request_monotonic is not None:
                earliest = max(earliest, self._last_request_monotonic + self._request_interval)
            if earliest > now:
                self._sleep(earliest - now)
            self._last_request_monotonic = self._monotonic()

    def _register_retry_after(self, response: httpx.Response) -> None:
        retry_after = _parse_retry_after_seconds(
            response.headers.get("Retry-After"),
            now=self._utc_now(),
        )
        if retry_after is None:
            return
        bounded_retry_after = min(retry_after, self._config.max_retry_after_seconds)
        with self._rate_lock:
            self._blocked_until_monotonic = max(
                self._blocked_until_monotonic,
                self._monotonic() + bounded_retry_after,
            )


def _parse_filing_columns(
    columns: Mapping[str, Any],
    *,
    cik: str,
    source: SecSourceDocument,
) -> tuple[SecFilingRecord, ...]:
    required_names = ("accessionNumber", "filingDate", "acceptanceDateTime", "form")
    required = {
        name: _require_sequence(columns.get(name), name=f"submissions.{name}")
        for name in required_names
    }
    count = len(required["accessionNumber"])
    if any(len(values) != count for values in required.values()):
        msg = "SEC submissions required columns have unequal lengths"
        raise SecEdgarParseError(msg)
    optional_names = (
        "reportDate",
        "primaryDocument",
        "items",
        "isXBRL",
        "isInlineXBRL",
    )
    optional: dict[str, Sequence[Any]] = {}
    for name in optional_names:
        raw_values = columns.get(name)
        if raw_values is None:
            optional[name] = (None,) * count
            continue
        values = _require_sequence(raw_values, name=f"submissions.{name}")
        if len(values) != count:
            msg = f"SEC submissions optional column {name} has the wrong length"
            raise SecEdgarParseError(msg)
        optional[name] = values

    filings: list[SecFilingRecord] = []
    for index in range(count):
        acceptance_raw = required["acceptanceDateTime"][index]
        if not isinstance(acceptance_raw, str):
            msg = "SEC submissions acceptanceDateTime must be a string"
            raise SecEdgarParseError(msg)
        report_date_raw = _optional_text(optional["reportDate"][index])
        primary_document = _optional_text(optional["primaryDocument"][index])
        items = tuple(
            item.strip() for item in str(optional["items"][index] or "").split(",") if item.strip()
        )
        filings.append(
            SecFilingRecord(
                cik=cik,
                accession=normalize_accession(str(required["accessionNumber"][index])),
                acceptance_time_raw=acceptance_raw,
                acceptance_time=parse_submissions_acceptance_time(acceptance_raw),
                filing_date=_parse_iso_date(
                    required["filingDate"][index],
                    name="submissions filingDate",
                ),
                form=_required_text(required["form"][index], name="submissions form"),
                report_date=(
                    _parse_iso_date(report_date_raw, name="submissions reportDate")
                    if report_date_raw
                    else None
                ),
                primary_document=primary_document,
                items=items,
                is_xbrl=_parse_bool_flag(optional["isXBRL"][index], default=False),
                is_inline_xbrl=_parse_bool_flag(
                    optional["isInlineXBRL"][index],
                    default=False,
                ),
                source=source,
            )
        )
    filings.sort(key=lambda filing: (filing.acceptance_time, filing.accession))
    return tuple(filings)


def _parse_fact(
    observation: Mapping[str, Any],
    *,
    cik: str,
    taxonomy: str,
    tag: str,
    label: str | None,
    description: str | None,
    unit: str,
    source: SecSourceDocument,
) -> SecXbrlFact:
    start_raw = _optional_text(observation.get("start"))
    fiscal_year_raw = observation.get("fy")
    fiscal_year = None
    if fiscal_year_raw not in (None, ""):
        if isinstance(fiscal_year_raw, bool):
            msg = f"SEC fact fiscal year is invalid for {taxonomy}:{tag}"
            raise SecEdgarParseError(msg)
        try:
            fiscal_year = int(str(fiscal_year_raw))
        except (TypeError, ValueError) as exc:
            msg = f"SEC fact fiscal year is invalid for {taxonomy}:{tag}"
            raise SecEdgarParseError(msg) from exc
    return SecXbrlFact(
        cik=cik,
        taxonomy=_required_text(taxonomy, name="SEC fact taxonomy"),
        tag=_required_text(tag, name="SEC fact tag"),
        label=label,
        description=description,
        unit=_required_text(unit, name="SEC fact unit"),
        value=_parse_fact_value(observation.get("val"), taxonomy=taxonomy, tag=tag),
        start=(
            _parse_iso_date(start_raw, name=f"SEC fact {taxonomy}:{tag} start")
            if start_raw
            else None
        ),
        end=_parse_iso_date(
            observation.get("end"),
            name=f"SEC fact {taxonomy}:{tag} end",
        ),
        accession=normalize_accession(
            _required_text(observation.get("accn"), name="SEC fact accession")
        ),
        fiscal_year=fiscal_year,
        fiscal_period=_optional_text(observation.get("fp")),
        form=_required_text(observation.get("form"), name="SEC fact form"),
        filed=_parse_iso_date(
            observation.get("filed"),
            name=f"SEC fact {taxonomy}:{tag} filed",
        ),
        frame=_optional_text(observation.get("frame")),
        source=source,
    )


def _fact_sort_key(fact: SecXbrlFact) -> tuple[str, str, str, date, date, str, str]:
    return (
        fact.taxonomy,
        fact.tag,
        fact.unit,
        fact.end,
        fact.start or date.min,
        fact.accession,
        fact.frame or "",
    )


def _parse_fact_value(raw: Any, *, taxonomy: str, tag: str) -> FactValue:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, Decimal):
        if not raw.is_finite():
            msg = f"SEC fact value is non-finite for {taxonomy}:{tag}"
            raise SecEdgarParseError(msg)
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        if not math.isfinite(raw):
            msg = f"SEC fact value is non-finite for {taxonomy}:{tag}"
            raise SecEdgarParseError(msg)
        return Decimal(str(raw))
    if isinstance(raw, str):
        return raw
    msg = f"SEC fact value has unsupported type for {taxonomy}:{tag}"
    raise SecEdgarParseError(msg)


def _submission_page_names(
    payload: Mapping[str, Any],
    *,
    since: date | None = None,
) -> tuple[str, ...]:
    filings = _require_mapping(payload.get("filings"), name="submissions.filings")
    pages_raw = filings.get("files", ())
    pages = _require_sequence(pages_raw, name="submissions.filings.files")
    names: set[str] = set()
    for index, raw_page in enumerate(pages):
        page = _require_mapping(raw_page, name=f"submissions.filings.files[{index}]")
        name = _required_text(page.get("name"), name="submissions page name")
        if _PAGE_NAME_PATTERN.fullmatch(name) is None or "/" in name or "\\" in name:
            msg = f"Unsafe SEC submissions page name: {name!r}"
            raise SecEdgarParseError(msg)
        filing_to_raw = _optional_text(page.get("filingTo"))
        if (
            since is not None
            and filing_to_raw is not None
            and _parse_iso_date(
                filing_to_raw,
                name="submissions page filingTo",
            )
            < since
        ):
            continue
        names.add(name)
    return tuple(sorted(names))


def _merge_submission_datasets(
    datasets: Sequence[SecSubmissionDataset],
) -> SecSubmissionDataset:
    if not datasets:
        msg = "At least one SEC submissions dataset is required"
        raise ValueError(msg)
    root = datasets[0]
    filings_by_accession: dict[str, SecFilingRecord] = {}
    sources: list[SecSourceDocument] = []
    for dataset in datasets:
        if dataset.cik != root.cik:
            msg = "SEC submissions pages contain different CIKs"
            raise SecEdgarParseError(msg)
        sources.extend(dataset.sources)
        for filing in dataset.filings:
            existing = filings_by_accession.get(filing.accession)
            if existing is not None and _filing_content(existing) != _filing_content(filing):
                msg = f"SEC submissions pages conflict for accession {filing.accession}"
                raise SecEdgarParseError(msg)
            filings_by_accession.setdefault(filing.accession, filing)
    filings = tuple(
        sorted(
            filings_by_accession.values(),
            key=lambda filing: (filing.acceptance_time, filing.accession),
        )
    )
    return SecSubmissionDataset(
        cik=root.cik,
        entity_name=root.entity_name,
        current_sic=root.current_sic,
        filings=filings,
        sources=tuple(sources),
    )


def _filing_content(filing: SecFilingRecord) -> tuple[Any, ...]:
    return (
        filing.cik,
        filing.accession,
        filing.acceptance_time_raw,
        filing.acceptance_time,
        filing.filing_date,
        filing.form,
        filing.report_date,
        filing.primary_document,
        filing.items,
        filing.is_xbrl,
        filing.is_inline_xbrl,
        filing.historical_sic,
        filing.historical_sic_source,
    )


def _header_prefix(content: str) -> str:
    prefix = content[:_HEADER_SCAN_LIMIT]
    preamble = re.search(r"<pre\b[^>]*>", prefix, re.IGNORECASE)
    if preamble is not None:
        prefix = html.unescape(prefix[preamble.end() :])
    closing_index = prefix.find("</SEC-HEADER>")
    if closing_index >= 0:
        return prefix[:closing_index]
    document_index = prefix.find("<DOCUMENT>")
    if document_index >= 0:
        return prefix[:document_index]
    return prefix


def _required_header_value(header: str, *, angle_name: str, label_name: str) -> str:
    value = _optional_header_value(header, angle_name=angle_name, label_name=label_name)
    if value is None:
        msg = f"SEC filing header is missing {label_name}"
        raise SecEdgarParseError(msg)
    return value


def _optional_header_value(
    header: str,
    *,
    angle_name: str,
    label_name: str,
) -> str | None:
    angle = re.search(rf"<{re.escape(angle_name)}>\s*([^\r\n<]+)", header, re.IGNORECASE)
    if angle is not None:
        return angle.group(1).strip()
    labelled = re.search(
        rf"^\s*{re.escape(label_name)}:\s*(\S.*?)\s*$",
        header,
        re.IGNORECASE | re.MULTILINE,
    )
    return labelled.group(1).strip() if labelled is not None else None


def _parse_filer_identities(header: str) -> tuple[SecFilerIdentity, ...]:
    identities: list[SecFilerIdentity] = []
    pending_name: str | None = None
    current_cik: str | None = None
    current_name: str | None = None
    current_sic: int | None = None

    def flush() -> None:
        nonlocal current_cik, current_name, current_sic
        if current_cik is not None:
            identities.append(
                SecFilerIdentity(
                    cik=current_cik,
                    company_name=current_name,
                    sic=current_sic,
                )
            )
        current_cik = None
        current_name = None
        current_sic = None

    for raw_line in header.splitlines():
        line = raw_line.strip()
        name_match = re.match(
            r"(?:<COMPANY-CONFORMED-NAME>|COMPANY CONFORMED NAME:)\s*(.+)",
            line,
            re.IGNORECASE,
        )
        if name_match is not None:
            pending_name = name_match.group(1).strip()
            continue
        cik_match = re.match(
            r"(?:<CENTRAL-INDEX-KEY>|CENTRAL INDEX KEY:)\s*(\d+)",
            line,
            re.IGNORECASE,
        )
        if cik_match is not None:
            flush()
            current_cik = normalize_cik(cik_match.group(1))
            current_name = pending_name
            pending_name = None
            continue
        sic_match = re.match(
            r"(?:<ASSIGNED-SIC>|STANDARD INDUSTRIAL CLASSIFICATION:)\s*(.+)",
            line,
            re.IGNORECASE,
        )
        if sic_match is not None and current_cik is not None:
            current_sic = _sic_from_header_value(sic_match.group(1))
    flush()
    by_cik: dict[str, SecFilerIdentity] = {}
    for identity in identities:
        existing = by_cik.get(identity.cik)
        if existing is not None and existing != identity:
            msg = f"SEC filing header has conflicting filer identity for CIK {identity.cik}"
            raise SecEdgarParseError(msg)
        by_cik[identity.cik] = identity
    return tuple(by_cik[cik] for cik in sorted(by_cik))


def _sic_from_header_value(raw: str) -> int:
    bracketed = re.search(r"\[(\d{4})\]\s*$", raw)
    digits = bracketed.group(1) if bracketed is not None else raw.strip()
    return _optional_sic(digits, name="filing header SIC") or 0


def _parse_explicit_timezone_acceptance(raw: str) -> datetime:
    value = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"Invalid SEC filing-header acceptance timestamp: {raw!r}"
        raise SecEdgarParseError(msg) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = "SEC filing-header acceptance timestamp lacks an explicit timezone"
        raise SecEdgarParseError(msg)
    return parsed.astimezone(UTC)


def _parse_header_acceptance(raw: str) -> datetime:
    value = raw.strip()
    if re.fullmatch(r"\d{14}", value) is not None:
        parsed = datetime.strptime(  # noqa: DTZ007 - EDGAR publishes an Eastern wall clock.
            value,
            "%Y%m%d%H%M%S",
        )
        return _localize_unambiguous_eastern(parsed, source="SEC filing header")
    return _parse_explicit_timezone_acceptance(value)


def _localize_unambiguous_eastern(wall_clock: datetime, *, source: str) -> datetime:
    if wall_clock.tzinfo is not None or wall_clock.utcoffset() is not None:
        msg = f"{source} wall clock must not carry timezone metadata"
        raise SecEdgarParseError(msg)
    candidates: set[datetime] = set()
    for fold in (0, 1):
        localized = wall_clock.replace(tzinfo=_SEC_HEADER_TIMEZONE, fold=fold)
        candidate = localized.astimezone(UTC)
        round_trip = candidate.astimezone(_SEC_HEADER_TIMEZONE).replace(tzinfo=None)
        if round_trip == wall_clock:
            candidates.add(candidate)
    if len(candidates) != 1:
        msg = f"{source} wall clock is ambiguous or nonexistent in America/New_York"
        raise SecEdgarParseError(msg)
    return candidates.pop()


def _parse_compact_or_iso_date(raw: str, *, name: str) -> date:
    value = raw.strip()
    if re.fullmatch(r"\d{8}", value) is not None:
        try:
            return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")
        except ValueError as exc:
            msg = f"Invalid {name}: {raw!r}"
            raise SecEdgarParseError(msg) from exc
    return _parse_iso_date(value, name=name)


def _parse_iso_date(raw: Any, *, name: str) -> date:
    value = _required_text(raw, name=name)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        msg = f"Invalid {name}: {value!r}"
        raise SecEdgarParseError(msg) from exc


def _parse_bool_flag(raw: Any, *, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    if raw in (True, 1, "1"):
        return True
    if raw in (False, 0, "0"):
        return False
    msg = f"Invalid SEC boolean flag: {raw!r}"
    raise SecEdgarParseError(msg)


def _parse_retry_after_seconds(raw: str | None, *, now: datetime) -> float | None:
    if raw is None:
        return None
    value = raw.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at.astimezone(UTC) - _require_aware_utc(now)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "SEC timestamp must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC)


def _require_mapping(raw: Any, *, name: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        msg = f"{name} must be an object"
        raise SecEdgarParseError(msg)
    return raw


def _require_sequence(raw: Any, *, name: str) -> Sequence[Any]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        msg = f"{name} must be an array"
        raise SecEdgarParseError(msg)
    return raw


def _required_text(raw: Any, *, name: str) -> str:
    value = str(raw or "").strip()
    if not value:
        msg = f"{name} must be non-empty"
        raise SecEdgarParseError(msg)
    return value


def _optional_text(raw: Any) -> str | None:
    value = str(raw or "").strip()
    return value or None


def _optional_sic(raw: Any, *, name: str) -> int | None:
    if raw in (None, ""):
        return None
    value = str(raw).strip()
    if not value.isdigit() or len(value) != _SIC_WIDTH:
        msg = f"{name} must be a four-digit SIC"
        raise SecEdgarParseError(msg)
    return int(value)


def _require_finite_range(
    value: float,
    *,
    name: str,
    minimum: float,
    maximum: float,
) -> None:
    if not math.isfinite(value) or not minimum <= value <= maximum:
        msg = f"{name} must be between {minimum} and {maximum}"
        raise ValueError(msg)


__all__ = [
    "SecAcceptedFact",
    "SecCompanyFactsDataset",
    "SecCompanyTicker",
    "SecCompanyTickerDataset",
    "SecCompanyTickerResponse",
    "SecEdgarClient",
    "SecEdgarClientConfig",
    "SecEdgarError",
    "SecEdgarParseError",
    "SecEdgarRequestError",
    "SecFilerIdentity",
    "SecFilingHeader",
    "SecFilingRecord",
    "SecSourceDocument",
    "SecSubmissionDataset",
    "SecXbrlFact",
    "apply_filing_header",
    "attach_filing_lineage",
    "normalize_accession",
    "normalize_cik",
    "parse_company_facts_payload",
    "parse_company_tickers_payload",
    "parse_filing_header",
    "parse_submissions_acceptance_time",
    "parse_submissions_payload",
    "validate_edgar_user_agent",
]
