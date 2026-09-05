"""Compile content-pinned official NYSE regular-session evidence.

The ICE releases are the authority for holiday and early-close dates.  The
platform's pinned ``exchange_calendars`` adapter supplies the UTC conversion
and acts as an independent schedule cross-check.  Any source-byte or calendar
disagreement fails closed before an authoritative artifact can be emitted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Final, Protocol
from zoneinfo import ZoneInfo

import httpx

from dev_cli.validation.backtest.equity_snapshot_identity import (
    HISTORICAL_MARKET_SESSIONS_SCHEMA,
)
from dev_cli.validation.evidence import canonical_json_bytes, evidence_sha256, sha256_digest
from lib_common.http_exceptions import is_retryable_status
from lib_common.logging import get_logger
from lib_common.retries import retry_sync
from lib_data.sessions import (
    is_half_day,
    session_close_utc,
    session_open_utc,
    sessions_between,
)

logger = get_logger(__name__)

__all__ = [
    "CompiledNYSEOfficialSessionArtifact",
    "NYSEOfficialSessionCompilerConfig",
    "NYSEOfficialSessionError",
    "compile_nyse_official_session_artifact",
]

_XNYS: Final = "XNYS"
_EASTERN: Final = ZoneInfo("America/New_York")
_SOURCE_COVERAGE_FROM: Final = date(2018, 1, 1)
_SOURCE_COVERAGE_TO: Final = date(2026, 12, 31)
_MAX_SOURCE_BYTES: Final = 5 * 1024 * 1024
_MAX_FROZEN_ARTIFACT_BYTES: Final = 64 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS: Final = 30.0
_DEFAULT_RETRIES: Final = 2
_DEFAULT_RETRY_BASE_DELAY_SECONDS: Final = 0.5
_DEFAULT_RETRY_MAX_DELAY_SECONDS: Final = 5.0
_MAX_TIMEOUT_SECONDS: Final = 120.0
_MAX_RETRIES: Final = 5
_WEEKDAY_COUNT: Final = 5
_USER_AGENT: Final = "vynmatrix/1.0 official-NYSE-session-compiler"
_SOURCE_REFERENCE: Final = "https://www.nyse.com/markets/hours-calendars"

_CALENDAR_2018_2020 = "calendar_2018_2020"
_CALENDAR_2021_2023 = "calendar_2021_2023"
_CALENDAR_2022_2024_REVISION = "calendar_2022_2024_juneteenth_revision"
_CALENDAR_2024_2026 = "calendar_2024_2026"
_BUSH_CLOSURE = "bush_national_day_of_mourning_2018"
_CARTER_CLOSURE = "carter_national_day_of_mourning_2025"


class NYSEOfficialSessionError(RuntimeError):
    """Official source or derived XNYS session evidence is invalid."""


class _RetryableNYSESourceError(NYSEOfficialSessionError):
    """Internal marker for bounded transport retries."""


class _HttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> httpx.Response: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class NYSEOfficialSessionCompilerConfig:
    """Bounded transport policy for immutable ICE source documents."""

    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    retries: int = _DEFAULT_RETRIES
    retry_base_delay_seconds: float = _DEFAULT_RETRY_BASE_DELAY_SECONDS
    retry_max_delay_seconds: float = _DEFAULT_RETRY_MAX_DELAY_SECONDS

    def __post_init__(self) -> None:
        for field_name, field_value in (
            ("timeout_seconds", self.timeout_seconds),
            ("retry_base_delay_seconds", self.retry_base_delay_seconds),
            ("retry_max_delay_seconds", self.retry_max_delay_seconds),
        ):
            if not math.isfinite(field_value) or field_value < 0:
                message = f"NYSE source {field_name} must be finite and non-negative"
                raise ValueError(message)
        if not 0 < self.timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            message = f"NYSE source timeout_seconds must be in (0, {_MAX_TIMEOUT_SECONDS}]"
            raise ValueError(message)
        if not 0 <= self.retries <= _MAX_RETRIES:
            message = f"NYSE source retries must be between 0 and {_MAX_RETRIES}"
            raise ValueError(message)
        if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            message = "NYSE source retry_max_delay_seconds cannot be below the base delay"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class CompiledNYSEOfficialSessionArtifact:
    """Canonical JSON bytes and immutable identity returned by the compiler."""

    content: bytes
    content_sha256: str
    coverage_from: date
    coverage_to: date
    session_count: int
    source_revision: str
    retrieved_at: datetime

    def __post_init__(self) -> None:
        if hashlib.sha256(self.content).hexdigest() != self.content_sha256:
            message = "compiled NYSE artifact content differs from its SHA-256"
            raise NYSEOfficialSessionError(message)
        if self.coverage_to < self.coverage_from or self.session_count <= 0:
            message = "compiled NYSE artifact has invalid coverage"
            raise NYSEOfficialSessionError(message)
        object.__setattr__(self, "retrieved_at", _aware_utc(self.retrieved_at))


@dataclass(frozen=True, slots=True)
class _OfficialSourceSpec:
    role: str
    url: str
    released_on: date
    expected_sha256: str

    def __post_init__(self) -> None:
        if not self.role or self.role != self.role.strip():
            message = "official NYSE source role must be non-blank and canonical"
            raise NYSEOfficialSessionError(message)
        parsed_url = httpx.URL(self.url)
        if parsed_url.scheme != "https" or parsed_url.host != "s2.q4cdn.com":
            message = f"official NYSE source {self.role} must use the pinned ICE CDN"
            raise NYSEOfficialSessionError(message)
        if len(self.expected_sha256) != hashlib.sha256().digest_size * 2 or any(
            character not in "0123456789abcdef" for character in self.expected_sha256
        ):
            message = f"official NYSE source {self.role} SHA-256 is invalid"
            raise NYSEOfficialSessionError(message)


@dataclass(frozen=True, slots=True)
class _OfficialSourceDocument:
    spec: _OfficialSourceSpec
    retrieved_at: datetime
    content_type: str
    etag: str | None
    last_modified: str | None
    content: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "retrieved_at", _aware_utc(self.retrieved_at))
        if self.content_type != "application/pdf" or not self.content.startswith(b"%PDF-"):
            message = f"official NYSE source {self.spec.role} is not a PDF"
            raise NYSEOfficialSessionError(message)
        if len(self.content) > _MAX_SOURCE_BYTES:
            message = f"official NYSE source {self.spec.role} exceeds the byte limit"
            raise NYSEOfficialSessionError(message)
        if hashlib.sha256(self.content).hexdigest() != self.spec.expected_sha256:
            message = f"official NYSE source {self.spec.role} differs from its pinned SHA-256"
            raise NYSEOfficialSessionError(message)


@dataclass(frozen=True, slots=True)
class _DatedEvidence:
    session_date: date
    source_role: str


_PINNED_SOURCE_SPECS: tuple[_OfficialSourceSpec, ...] = (
    _OfficialSourceSpec(
        role=_CALENDAR_2018_2020,
        url=(
            "https://s2.q4cdn.com/154085107/files/doc_news/archive/"
            "27aaa58b-ebcb-4f53-b505-aee992f54968.pdf"
        ),
        released_on=date(2017, 11, 27),
        expected_sha256="b13c0181091b10164319992cb537e948e2d4ee8ceff71f61fdfe7989ccd89812",
    ),
    _OfficialSourceSpec(
        role=_CALENDAR_2021_2023,
        url=(
            "https://s2.q4cdn.com/154085107/files/doc_news/"
            "NYSE-Group-Announces-2021-2022-and-2023-Holiday-and-Early-Closings-"
            "Calendar-2020.pdf"
        ),
        released_on=date(2020, 12, 28),
        expected_sha256="4419cb783586369dc58003e630d81095a23991eec1cb31308c7b2e6099ceeb49",
    ),
    _OfficialSourceSpec(
        role=_CALENDAR_2022_2024_REVISION,
        url=(
            "https://s2.q4cdn.com/154085107/files/doc_news/"
            "NYSE-Group-Announces-2022-2023-and-2024-Holiday-and-Early-Closings-"
            "Calendar-2021.pdf"
        ),
        released_on=date(2021, 12, 27),
        expected_sha256="b8537e458aa0f7123c014056da9260d08f3e874b32f7f617d99451f36e049e90",
    ),
    _OfficialSourceSpec(
        role=_CALENDAR_2024_2026,
        url=(
            "https://s2.q4cdn.com/154085107/files/doc_news/"
            "NYSE-Group-Announces-2024-2025-and-2026-Holiday-and-Early-Closings-"
            "Calendar-2023.pdf"
        ),
        released_on=date(2023, 11, 10),
        expected_sha256="a379433b12a84c1b898075be0c04343ffaef45b6d47a2fd4283747cdcd2bd64f",
    ),
    _OfficialSourceSpec(
        role=_BUSH_CLOSURE,
        url=(
            "https://s2.q4cdn.com/154085107/files/doc_news/archive/"
            "266fafb2-a85c-433a-8350-0760d895b49c.pdf"
        ),
        released_on=date(2018, 12, 1),
        expected_sha256="46e28f7864ed20dac04b3acfc51a4cbea6937f57ba01d427226e6837c8785b7c",
    ),
    _OfficialSourceSpec(
        role=_CARTER_CLOSURE,
        url=(
            "https://s2.q4cdn.com/154085107/files/doc_news/"
            "The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-"
            "the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of--IGCRS.pdf"
        ),
        released_on=date(2024, 12, 30),
        expected_sha256="b83882b2f29554a969c30f5a48c37eff3b6baab449273bfdb3d8c3df62244c69",
    ),
)

_YEAR_SOURCE_ROLE: Mapping[int, str] = {
    2018: _CALENDAR_2018_2020,
    2019: _CALENDAR_2018_2020,
    2020: _CALENDAR_2018_2020,
    2021: _CALENDAR_2021_2023,
    2022: _CALENDAR_2022_2024_REVISION,
    2023: _CALENDAR_2022_2024_REVISION,
    2024: _CALENDAR_2024_2026,
    2025: _CALENDAR_2024_2026,
    2026: _CALENDAR_2024_2026,
}

_STANDARD_CLOSURE_DATES: Mapping[int, tuple[tuple[int, int], ...]] = {
    2018: ((1, 1), (1, 15), (2, 19), (3, 30), (5, 28), (7, 4), (9, 3), (11, 22), (12, 25)),
    2019: ((1, 1), (1, 21), (2, 18), (4, 19), (5, 27), (7, 4), (9, 2), (11, 28), (12, 25)),
    2020: ((1, 1), (1, 20), (2, 17), (4, 10), (5, 25), (7, 3), (9, 7), (11, 26), (12, 25)),
    2021: ((1, 1), (1, 18), (2, 15), (4, 2), (5, 31), (7, 5), (9, 6), (11, 25), (12, 24)),
    2022: ((1, 17), (2, 21), (4, 15), (5, 30), (6, 20), (7, 4), (9, 5), (11, 24), (12, 26)),
    2023: ((1, 2), (1, 16), (2, 20), (4, 7), (5, 29), (6, 19), (7, 4), (9, 4), (11, 23), (12, 25)),
    2024: ((1, 1), (1, 15), (2, 19), (3, 29), (5, 27), (6, 19), (7, 4), (9, 2), (11, 28), (12, 25)),
    2025: ((1, 1), (1, 20), (2, 17), (4, 18), (5, 26), (6, 19), (7, 4), (9, 1), (11, 27), (12, 25)),
    2026: ((1, 1), (1, 19), (2, 16), (4, 3), (5, 25), (6, 19), (7, 3), (9, 7), (11, 26), (12, 25)),
}

_EARLY_CLOSE_DATES: Mapping[int, tuple[tuple[int, int], ...]] = {
    2018: ((7, 3), (11, 23), (12, 24)),
    2019: ((7, 3), (11, 29), (12, 24)),
    2020: ((11, 27), (12, 24)),
    2021: ((11, 26),),
    2022: ((11, 25),),
    2023: ((7, 3), (11, 24)),
    2024: ((7, 3), (11, 29), (12, 24)),
    2025: ((7, 3), (11, 28), (12, 24)),
    2026: ((11, 27), (12, 24)),
}


def _dated_evidence() -> tuple[tuple[_DatedEvidence, ...], tuple[_DatedEvidence, ...]]:
    closures = [
        _DatedEvidence(date(year, month, day), _YEAR_SOURCE_ROLE[year])
        for year, values in _STANDARD_CLOSURE_DATES.items()
        for month, day in values
    ]
    closures.extend(
        (
            _DatedEvidence(date(2018, 12, 5), _BUSH_CLOSURE),
            _DatedEvidence(date(2025, 1, 9), _CARTER_CLOSURE),
        )
    )
    early_closes = [
        _DatedEvidence(date(year, month, day), _YEAR_SOURCE_ROLE[year])
        for year, values in _EARLY_CLOSE_DATES.items()
        for month, day in values
    ]
    return tuple(sorted(closures, key=lambda item: item.session_date)), tuple(
        sorted(early_closes, key=lambda item: item.session_date)
    )


_CLOSURES, _EARLY_CLOSES = _dated_evidence()


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        message = "NYSE source retrieval clock must return a timezone-aware datetime"
        raise NYSEOfficialSessionError(message)
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _validate_coverage(start: date, end: date) -> None:
    if not isinstance(start, date) or not isinstance(end, date):
        message = "NYSE session coverage requires date values"
        raise TypeError(message)
    if isinstance(start, datetime) or isinstance(end, datetime):
        message = "NYSE session coverage requires date values, not datetimes"
        raise TypeError(message)
    if end < start:
        message = "NYSE session coverage end cannot precede start"
        raise ValueError(message)
    if start < _SOURCE_COVERAGE_FROM or end > _SOURCE_COVERAGE_TO:
        message = (
            "NYSE official sources cover only "
            f"{_SOURCE_COVERAGE_FROM.isoformat()} through {_SOURCE_COVERAGE_TO.isoformat()}"
        )
        raise NYSEOfficialSessionError(message)


def _fetch_source(
    client: _HttpClient,
    spec: _OfficialSourceSpec,
    *,
    config: NYSEOfficialSessionCompilerConfig,
    clock: Callable[[], datetime],
) -> _OfficialSourceDocument:
    headers = {"Accept": "application/pdf", "User-Agent": _USER_AGENT}

    def request_once() -> _OfficialSourceDocument:
        try:
            response = client.get(
                spec.url,
                headers=headers,
                timeout=config.timeout_seconds,
            )
        except httpx.RequestError as exc:
            message = f"official NYSE source transport failure for {spec.role}"
            raise _RetryableNYSESourceError(message) from exc
        if is_retryable_status(response.status_code):
            message = f"official NYSE source {spec.role} returned HTTP {response.status_code}"
            raise _RetryableNYSESourceError(message)
        if response.is_error:
            message = f"official NYSE source {spec.role} returned HTTP {response.status_code}"
            raise NYSEOfficialSessionError(message)
        if response.url != httpx.URL(spec.url):
            message = f"official NYSE source {spec.role} redirected outside its pinned URL"
            raise NYSEOfficialSessionError(message)
        declared_length = response.headers.get("Content-Length")
        content = response.content
        if declared_length is not None:
            try:
                parsed_length = int(declared_length)
            except ValueError as exc:
                message = f"official NYSE source {spec.role} has invalid Content-Length"
                raise NYSEOfficialSessionError(message) from exc
            if parsed_length < 0 or parsed_length > _MAX_SOURCE_BYTES:
                message = f"official NYSE source {spec.role} exceeds the byte limit"
                raise NYSEOfficialSessionError(message)
            if parsed_length != len(content):
                message = f"official NYSE source {spec.role} Content-Length differs"
                raise NYSEOfficialSessionError(message)
        content_type = response.headers.get("Content-Type", "").partition(";")[0].lower()
        return _OfficialSourceDocument(
            spec=spec,
            retrieved_at=clock(),
            content_type=content_type,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            content=content,
        )

    try:
        return retry_sync(
            request_once,
            retries=config.retries,
            base_delay=config.retry_base_delay_seconds,
            max_delay=config.retry_max_delay_seconds,
            jitter=0.0,
            retry_on=(_RetryableNYSESourceError,),
            operation_name=f"ICE NYSE source GET {spec.role}",
            log=logger,
        )
    except _RetryableNYSESourceError as exc:
        message = f"official NYSE source {spec.role} exhausted retries"
        raise NYSEOfficialSessionError(message) from exc


def _official_session_dates(start: date, end: date) -> tuple[date, ...]:
    closures = {item.session_date for item in _CLOSURES}
    cursor = start
    result: list[date] = []
    while cursor <= end:
        if cursor.weekday() < _WEEKDAY_COUNT and cursor not in closures:
            result.append(cursor)
        cursor += timedelta(days=1)
    return tuple(result)


def _calendar_rows(start: date, end: date) -> tuple[dict[str, str], ...]:
    official_dates = _official_session_dates(start, end)
    try:
        library_dates = tuple(sessions_between(_XNYS, start, end))
    except ImportError as exc:
        message = "NYSE compilation requires the pinned lib-data[sessions] runtime"
        raise NYSEOfficialSessionError(message) from exc
    if official_dates != library_dates:
        missing = sorted(set(official_dates) - set(library_dates))
        extra = sorted(set(library_dates) - set(official_dates))
        message = (
            "official ICE dates disagree with the pinned XNYS calendar: "
            f"library_missing={[item.isoformat() for item in missing]}, "
            f"library_extra={[item.isoformat() for item in extra]}"
        )
        raise NYSEOfficialSessionError(message)

    expected_early_closes = {item.session_date for item in _EARLY_CLOSES}
    rows: list[dict[str, str]] = []
    for session_date in official_dates:
        expected_early = session_date in expected_early_closes
        if is_half_day(_XNYS, session_date) is not expected_early:
            message = (
                "official ICE early-close evidence disagrees with the pinned XNYS calendar "
                f"for {session_date.isoformat()}"
            )
            raise NYSEOfficialSessionError(message)
        opens_at = session_open_utc(_XNYS, session_date).replace(tzinfo=UTC)
        closes_at = session_close_utc(_XNYS, session_date).replace(tzinfo=UTC)
        local_open = opens_at.astimezone(_EASTERN)
        local_close = closes_at.astimezone(_EASTERN)
        expected_close_hour = 13 if expected_early else 16
        if (
            (local_open.hour, local_open.minute, local_open.second, local_open.microsecond)
            != (9, 30, 0, 0)
            or (local_close.hour, local_close.minute, local_close.second, local_close.microsecond)
            != (expected_close_hour, 0, 0, 0)
            or local_open.date() != session_date
            or local_close.date() != session_date
        ):
            message = (
                f"pinned XNYS regular-session timing is invalid for {session_date.isoformat()}"
            )
            raise NYSEOfficialSessionError(message)
        rows.append(
            {
                "closes_at": closes_at.isoformat(),
                "opens_at": opens_at.isoformat(),
                "session_date": session_date.isoformat(),
            }
        )
    if not rows:
        message = "NYSE official coverage contains no trading sessions"
        raise NYSEOfficialSessionError(message)
    return tuple(rows)


def _exchange_calendars_version() -> str:
    try:
        return version("exchange-calendars")
    except PackageNotFoundError as exc:  # pragma: no cover - invalid runtime image
        message = "exchange-calendars package metadata is unavailable"
        raise NYSEOfficialSessionError(message) from exc


def _source_revision(documents: Sequence[_OfficialSourceDocument]) -> str:
    return evidence_sha256(
        [
            {
                "content_sha256": document.spec.expected_sha256,
                "released_on": document.spec.released_on.isoformat(),
                "role": document.spec.role,
                "url": document.spec.url,
            }
            for document in documents
        ]
    )


def _source_payload(document: _OfficialSourceDocument) -> dict[str, object]:
    return {
        "content_base64": base64.b64encode(document.content).decode("ascii"),
        "content_sha256": document.spec.expected_sha256,
        "content_type": document.content_type,
        "etag": document.etag,
        "last_modified": document.last_modified,
        "publisher": "Intercontinental Exchange / NYSE Group",
        "released_on": document.spec.released_on.isoformat(),
        "retrieved_at": document.retrieved_at.isoformat(),
        "role": document.spec.role,
        "url": document.spec.url,
    }


def _frozen_artifact_date(payload: Mapping[str, object], field: str) -> date:
    value = payload.get(field)
    if not isinstance(value, str):
        message = f"frozen NYSE source artifact {field} must be an ISO date"
        raise NYSEOfficialSessionError(message)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        message = f"frozen NYSE source artifact {field} must be an ISO date"
        raise NYSEOfficialSessionError(message) from exc


def _frozen_source_header(
    payload: Mapping[str, object],
    field: str,
    *,
    role: str,
) -> str | None:
    value = payload.get(field)
    if value is not None and not isinstance(value, str):
        message = f"frozen NYSE source {role} {field} must be a string or null"
        raise NYSEOfficialSessionError(message)
    return value


def _frozen_source_document(
    payload: Mapping[str, object],
    spec: _OfficialSourceSpec,
) -> _OfficialSourceDocument:
    expected_fields: Mapping[str, object] = {
        "content_sha256": spec.expected_sha256,
        "content_type": "application/pdf",
        "publisher": "Intercontinental Exchange / NYSE Group",
        "released_on": spec.released_on.isoformat(),
        "role": spec.role,
        "url": spec.url,
    }
    for field, expected in expected_fields.items():
        if payload.get(field) != expected:
            message = (
                f"frozen NYSE source {spec.role} {field} does not match "
                "the pinned authoritative source contract"
            )
            raise NYSEOfficialSessionError(message)
    encoded = payload.get("content_base64")
    if not isinstance(encoded, str):
        message = f"frozen NYSE source {spec.role} content_base64 must be a string"
        raise NYSEOfficialSessionError(message)
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        message = f"frozen NYSE source {spec.role} content_base64 is invalid"
        raise NYSEOfficialSessionError(message) from exc
    retrieved_at_raw = payload.get("retrieved_at")
    if not isinstance(retrieved_at_raw, str):
        message = f"frozen NYSE source {spec.role} retrieved_at must be an ISO timestamp"
        raise NYSEOfficialSessionError(message)
    try:
        retrieved_at = datetime.fromisoformat(retrieved_at_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        message = f"frozen NYSE source {spec.role} retrieved_at must be an ISO timestamp"
        raise NYSEOfficialSessionError(message) from exc
    return _OfficialSourceDocument(
        spec=spec,
        retrieved_at=retrieved_at,
        content_type="application/pdf",
        etag=_frozen_source_header(payload, "etag", role=spec.role),
        last_modified=_frozen_source_header(payload, "last_modified", role=spec.role),
        content=content,
    )


def _documents_from_frozen_artifact(
    path: Path,
    *,
    expected_content_sha256: str,
) -> tuple[_OfficialSourceDocument, ...]:
    try:
        pinned_sha256 = sha256_digest(
            expected_content_sha256,
            field="frozen NYSE source artifact SHA-256",
        )
    except (TypeError, ValueError) as exc:
        raise NYSEOfficialSessionError(str(exc)) from exc
    if path.name != f"{pinned_sha256}.json":
        message = "frozen NYSE source artifact filename is not content-addressed"
        raise NYSEOfficialSessionError(message)
    try:
        content = path.read_bytes()
    except OSError as exc:
        message = f"cannot read frozen NYSE source artifact: {path}"
        raise NYSEOfficialSessionError(message) from exc
    if not content or len(content) > _MAX_FROZEN_ARTIFACT_BYTES:
        message = "frozen NYSE source artifact is empty or exceeds the byte limit"
        raise NYSEOfficialSessionError(message)
    if hashlib.sha256(content).hexdigest() != pinned_sha256:
        message = "frozen NYSE source artifact differs from its pinned SHA-256"
        raise NYSEOfficialSessionError(message)
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        message = "frozen NYSE source artifact must be valid UTF-8 JSON"
        raise NYSEOfficialSessionError(message) from exc
    if not isinstance(decoded, Mapping):
        message = "frozen NYSE source artifact root must be an object"
        raise NYSEOfficialSessionError(message)
    coverage_from = _frozen_artifact_date(decoded, "coverage_from")
    coverage_to = _frozen_artifact_date(decoded, "coverage_to")
    _validate_coverage(coverage_from, coverage_to)
    raw_documents = decoded.get("source_documents")
    if not isinstance(raw_documents, list) or len(raw_documents) != len(_PINNED_SOURCE_SPECS):
        message = "frozen NYSE source artifact has an incomplete source-document set"
        raise NYSEOfficialSessionError(message)
    documents: list[_OfficialSourceDocument] = []
    for raw_document, spec in zip(raw_documents, _PINNED_SOURCE_SPECS, strict=True):
        if not isinstance(raw_document, Mapping):
            message = "frozen NYSE source artifact contains a non-object source document"
            raise NYSEOfficialSessionError(message)
        documents.append(_frozen_source_document(raw_document, spec))
    frozen_documents = tuple(documents)
    reconstructed = _compile_from_documents(
        frozen_documents,
        start=coverage_from,
        end=coverage_to,
    )
    if reconstructed.content != content:
        message = "frozen NYSE source artifact is not an exact canonical compiler output"
        raise NYSEOfficialSessionError(message)
    return frozen_documents


def _dated_evidence_payload(
    evidence: Sequence[_DatedEvidence],
    *,
    start: date,
    end: date,
) -> list[dict[str, str]]:
    return [
        {"session_date": item.session_date.isoformat(), "source_role": item.source_role}
        for item in evidence
        if start <= item.session_date <= end
    ]


def _compile_from_documents(
    documents: Sequence[_OfficialSourceDocument],
    *,
    start: date,
    end: date,
) -> CompiledNYSEOfficialSessionArtifact:
    observed_specs = tuple(document.spec for document in documents)
    if observed_specs != _PINNED_SOURCE_SPECS:
        message = "official NYSE source set is incomplete or out of canonical order"
        raise NYSEOfficialSessionError(message)
    observed_roles = tuple(document.spec.role for document in documents)
    evidence_roles = {item.source_role for item in (*_CLOSURES, *_EARLY_CLOSES)}
    if not evidence_roles.issubset(set(observed_roles)):
        message = "official NYSE date evidence references an unavailable source"
        raise NYSEOfficialSessionError(message)

    rows = _calendar_rows(start, end)
    retrieved_at = max(document.retrieved_at for document in documents)
    source_revision = _source_revision(documents)
    calendar_version = _exchange_calendars_version()
    payload: dict[str, object] = {
        "calendar_evidence": {
            "closure_dates": _dated_evidence_payload(_CLOSURES, start=start, end=end),
            "early_close_dates": _dated_evidence_payload(_EARLY_CLOSES, start=start, end=end),
            "regular_session_cross_check": {
                "library": "exchange_calendars",
                "library_version": calendar_version,
                "mic": _XNYS,
            },
        },
        "coverage_complete": True,
        "coverage_from": start.isoformat(),
        "coverage_to": end.isoformat(),
        "dataset": "ICE/NYSE cash-equity holidays, early closes, and special closures",
        "dataset_version": f"official-source-set-{source_revision[:16]}",
        "entitlement_scope": "public official exchange publications",
        "provider": "Intercontinental Exchange / NYSE Group",
        "retrieved_at": retrieved_at.isoformat(),
        "schema": HISTORICAL_MARKET_SESSIONS_SCHEMA,
        "sessions": list(rows),
        "source_documents": [_source_payload(document) for document in documents],
        "source_kind": "exchange",
        "source_reference": _SOURCE_REFERENCE,
        "source_revision": source_revision,
        "timestamp_semantics": (
            "official NYSE cash-equity session dates and 13:00 Eastern early closes; "
            "pinned exchange_calendars XNYS regular-session UTC conversion"
        ),
        "venue": _XNYS,
    }
    content = canonical_json_bytes(payload)
    return CompiledNYSEOfficialSessionArtifact(
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
        coverage_from=start,
        coverage_to=end,
        session_count=len(rows),
        source_revision=source_revision,
        retrieved_at=retrieved_at,
    )


def compile_nyse_official_session_artifact(
    *,
    start: date,
    end: date,
    config: NYSEOfficialSessionCompilerConfig | None = None,
    client: _HttpClient | None = None,
    clock: Callable[[], datetime] = _utc_now,
    source_artifact: Path | None = None,
    source_artifact_sha256: str | None = None,
) -> CompiledNYSEOfficialSessionArtifact:
    """Verify official source bytes and compile authoritative XNYS session JSON.

    The returned ``content_sha256`` is the exact value callers must persist
    beside the artifact and pass to ``read_historical_market_sessions``.
    Source bytes are either fetched from every pinned ICE URL or re-verified
    from one exact prior compiler output. A caller-supplied HTTP client remains
    caller-owned.
    """

    _validate_coverage(start, end)
    if source_artifact is not None:
        if source_artifact_sha256 is None:
            message = "source_artifact_sha256 is required with source_artifact"
            raise NYSEOfficialSessionError(message)
        if config is not None or client is not None:
            message = "frozen NYSE source reuse cannot be combined with transport settings"
            raise NYSEOfficialSessionError(message)
        documents = _documents_from_frozen_artifact(
            source_artifact,
            expected_content_sha256=source_artifact_sha256,
        )
        return _compile_from_documents(documents, start=start, end=end)
    if source_artifact_sha256 is not None:
        message = "source_artifact is required with source_artifact_sha256"
        raise NYSEOfficialSessionError(message)
    transport_policy = config or NYSEOfficialSessionCompilerConfig()
    source_client: _HttpClient = client or httpx.Client(follow_redirects=True)
    owns_client = client is None
    try:
        documents = tuple(
            _fetch_source(
                source_client,
                spec,
                config=transport_policy,
                clock=clock,
            )
            for spec in _PINNED_SOURCE_SPECS
        )
        return _compile_from_documents(documents, start=start, end=end)
    finally:
        if owns_client:
            source_client.close()
