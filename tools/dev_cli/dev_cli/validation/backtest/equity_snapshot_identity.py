"""Point-in-time membership and provider-alias identity for equity snapshots."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

from dev_cli.validation.evidence import (
    canonical_json_bytes,
    evidence_sha256,
    file_sha256,
    nonblank_string,
    parse_utc_datetime,
    sha256_digest,
)
from lib_data.sessions import (
    session_close_utc,
    session_open_utc,
    sessions_between,
)
from lib_strategy.panels import OfficialSessionCutoff, SessionAuthority

_MIN_HISTORY_AXIS_SESSIONS = 2
HISTORICAL_MARKET_SESSIONS_SCHEMA = "vynmatrix.historical-market-sessions.v1"


class SnapshotValidationError(RuntimeError):
    """Raised when acquisition inputs or immutable snapshot evidence are invalid."""


class ValidationProvider(StrEnum):
    """Historical providers permitted by this validation-only command surface."""

    EODHD = "eodhd"


@dataclass(frozen=True)
class StrategyIdentity:
    """Exact production-core and configuration identity used by validation."""

    strategy_id: str
    strategy_version: str
    algorithm_type_name: str
    relative_path: str
    config_sha256: str
    source_tree_sha256: str


def build_strategy_identity(strategy_dir: Path, *, repo_root: Path) -> StrategyIdentity:
    """Bind a source-controlled strategy core and config without copying either."""

    resolved_root = repo_root.resolve()
    allowed_root = (resolved_root / "strategies" / "indicator").resolve()
    resolved_strategy = strategy_dir.resolve()
    try:
        relative_strategy = resolved_strategy.relative_to(allowed_root)
    except ValueError as exc:
        message = f"strategy must be below {allowed_root}: {resolved_strategy}"
        raise SnapshotValidationError(message) from exc
    if len(relative_strategy.parts) != 1:
        message = "strategy must name one package directly below strategies/indicator"
        raise SnapshotValidationError(message)
    config_path = resolved_strategy / "config.json"
    core_path = resolved_strategy / "core.py"
    if not config_path.is_file() or not core_path.is_file():
        message = f"strategy requires config.json and core.py: {resolved_strategy}"
        raise SnapshotValidationError(message)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        message = f"invalid strategy config: {config_path}"
        raise SnapshotValidationError(message) from exc
    if not isinstance(config, Mapping):
        message = f"strategy config must be a JSON object: {config_path}"
        raise SnapshotValidationError(message)
    source_files = sorted(
        path
        for path in resolved_strategy.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix in {".py", ".json", ".toml"}
    )
    source_identity = [
        {
            "path": str(path.relative_to(resolved_strategy)),
            "sha256": file_sha256(path),
        }
        for path in source_files
    ]
    configured_type = config.get("algorithm-type-name")
    if configured_type is None:
        from lib_strategy.signals.loading import (  # noqa: PLC0415 - selected package only
            load_pure_strategy_core,
        )

        configured_type = load_pure_strategy_core(resolved_strategy).__name__
    return StrategyIdentity(
        strategy_id=nonblank_string(config.get("strategy_id"), field="strategy_id"),
        strategy_version=nonblank_string(
            config.get("strategy_version"),
            field="strategy_version",
        ),
        algorithm_type_name=nonblank_string(
            configured_type,
            field="algorithm-type-name",
        ),
        relative_path=str(resolved_strategy.relative_to(resolved_root)),
        config_sha256=file_sha256(config_path),
        source_tree_sha256=evidence_sha256(source_identity),
    )


class MembershipSourceKind(StrEnum):
    """Typed provenance classes for point-in-time membership history."""

    LICENSED_POINT_IN_TIME = "licensed_point_in_time"
    OFFICIAL_POINT_IN_TIME = "official_point_in_time"
    RECONSTRUCTED = "reconstructed"


class ListingEvidenceStatus(StrEnum):
    """Whether vendor-reported IPO evidence is identity-bound and usable."""

    BOUND = "bound"
    UNAVAILABLE = "unavailable"


class MarketSessionSourceKind(StrEnum):
    """Typed provenance classes for historical market-session schedules."""

    EXCHANGE = "exchange"
    BROKER = "broker"
    LIBRARY_GENERATED = "library_generated"


@dataclass(frozen=True, slots=True)
class HistoricalMarketSessions:
    """Content-pinned historical schedule using canonical official sessions."""

    venue: str
    source_kind: MarketSessionSourceKind
    provider: str
    dataset: str
    dataset_version: str
    entitlement_scope: str
    source_reference: str
    source_revision: str
    timestamp_semantics: str
    source_content_sha256: str
    retrieved_at: datetime | None
    coverage_from: date
    coverage_to: date
    coverage_complete: bool
    sessions: tuple[OfficialSessionCutoff, ...]
    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, MarketSessionSourceKind):
            message = "market-session source kind must be a MarketSessionSourceKind"
            raise SnapshotValidationError(message)
        for field_name in (
            "venue",
            "provider",
            "dataset",
            "dataset_version",
            "entitlement_scope",
            "source_reference",
            "source_revision",
            "timestamp_semantics",
        ):
            object.__setattr__(
                self,
                field_name,
                require_snapshot_text(
                    str(getattr(self, field_name)),
                    field=f"market-session {field_name}",
                ),
            )
        if self.coverage_to < self.coverage_from:
            message = "market-session coverage_to cannot precede coverage_from"
            raise SnapshotValidationError(message)
        if not isinstance(self.coverage_complete, bool):
            message = "market-session coverage_complete must be a boolean"
            raise SnapshotValidationError(message)
        try:
            content_sha256 = sha256_digest(
                self.source_content_sha256,
                field="market-session source_content_sha256",
            )
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(str(exc)) from exc
        if hashlib.sha256(self.content).hexdigest() != content_sha256:
            message = "market-session source content differs from its pinned SHA-256"
            raise SnapshotValidationError(message)
        object.__setattr__(self, "source_content_sha256", content_sha256)
        if self.retrieved_at is not None:
            object.__setattr__(
                self,
                "retrieved_at",
                parse_utc_datetime(
                    self.retrieved_at,
                    field="market-session retrieved_at",
                ),
            )
        if not self.sessions:
            message = "market-session schedule must contain at least one session"
            raise SnapshotValidationError(message)
        dates = tuple(session.session_date for session in self.sessions)
        if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
            message = "market-session rows must use unique ascending session dates"
            raise SnapshotValidationError(message)
        previous_close: datetime | None = None
        for session in self.sessions:
            if session.mic != self.venue:
                message = "market-session row venue differs from schedule venue"
                raise SnapshotValidationError(message)
            if not self.coverage_from <= session.session_date <= self.coverage_to:
                message = "market-session row lies outside the declared date coverage"
                raise SnapshotValidationError(message)
            if previous_close is not None and session.opens_at < previous_close:
                message = "market-session windows overlap"
                raise SnapshotValidationError(message)
            previous_close = session.closes_at

    @property
    def confirmatory_eligible(self) -> bool:
        """Return whether a caller supplied complete exchange/broker evidence."""

        return (
            self.source_kind in {MarketSessionSourceKind.EXCHANGE, MarketSessionSourceKind.BROKER}
            and self.coverage_complete
            and self.retrieved_at is not None
        )

    @property
    def session_dates(self) -> tuple[date, ...]:
        """Return the exact authoritative or diagnostic session axis."""

        return tuple(session.session_date for session in self.sessions)

    def authority_payload(self) -> dict[str, object]:
        """Return the manifest-safe provenance boundary."""

        return {
            "confirmatory_eligible": self.confirmatory_eligible,
            "dataset": self.dataset,
            "dataset_version": self.dataset_version,
            "entitlement_scope": self.entitlement_scope,
            "provider": self.provider,
            "retrieved_at": (
                self.retrieved_at.isoformat() if self.retrieved_at is not None else None
            ),
            "source_content_sha256": self.source_content_sha256,
            "source_kind": self.source_kind.value,
            "source_reference": self.source_reference,
            "source_revision": self.source_revision,
            "timestamp_semantics": self.timestamp_semantics,
        }


@dataclass(frozen=True, slots=True)
class MembershipSourceAuthority:
    """Revision-pinned authority for one membership source dataset."""

    kind: MembershipSourceKind
    provider: str
    dataset: str
    revision_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, MembershipSourceKind):
            message = "membership source kind must be a MembershipSourceKind"
            raise SnapshotValidationError(message)
        object.__setattr__(
            self,
            "provider",
            require_snapshot_text(self.provider, field="membership source provider"),
        )
        object.__setattr__(
            self,
            "dataset",
            require_snapshot_text(self.dataset, field="membership source dataset"),
        )
        try:
            revision = sha256_digest(
                self.revision_sha256,
                field="membership source revision_sha256",
            )
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(str(exc)) from exc
        object.__setattr__(self, "revision_sha256", revision)

    @property
    def authority_id(self) -> str:
        """Return the canonical authority identity."""

        return evidence_sha256(self.to_payload())

    @property
    def confirmatory_eligible(self) -> bool:
        """Return whether this authority is non-reconstructed point-in-time evidence."""

        return self.kind is not MembershipSourceKind.RECONSTRUCTED

    def to_payload(self) -> dict[str, str]:
        """Return the canonical manifest representation."""

        return {
            "dataset": self.dataset,
            "kind": self.kind.value,
            "provider": self.provider,
            "revision_sha256": self.revision_sha256,
        }


class SnapshotRequestBoundary(Protocol):
    """Request fields needed to resolve membership and provider history."""

    @property
    def provider(self) -> ValidationProvider: ...

    @property
    def start(self) -> date: ...

    @property
    def end(self) -> date: ...

    @property
    def calendar_mic(self) -> str: ...

    @property
    def security_history_sessions(self) -> int: ...


_BOUND_LISTING_IDENTITY_BINDINGS = frozenset(
    {
        "exact_provider_symbol_code",
        "matching_permanent_isin",
        "matching_sec_cik",
        "reviewed_primary_class_identity",
    }
)


def _validated_reviewed_listing_fields(
    *,
    listing_date: date | None,
    correction_id: str | None,
    correction_manifest_sha256: str | None,
) -> tuple[str | None, str | None]:
    values = (listing_date, correction_id, correction_manifest_sha256)
    if any(value is not None for value in values) and not all(
        value is not None for value in values
    ):
        message = "reviewed listing evidence fields must be configured together"
        raise SnapshotValidationError(message)
    if correction_id is None or correction_manifest_sha256 is None:
        return None, None
    normalized_id = require_snapshot_text(
        correction_id,
        field="reviewed listing correction_id",
    )
    try:
        normalized_sha256 = sha256_digest(
            correction_manifest_sha256,
            field="reviewed listing correction manifest SHA-256",
        )
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(str(exc)) from exc
    return normalized_id, normalized_sha256


def _validate_reviewed_listing_identity_binding(
    *, identity_binding: str, reviewed_listing_date: date | None
) -> None:
    if identity_binding == "reviewed_primary_class_identity" and reviewed_listing_date is None:
        message = "reviewed-primary listing binding requires reviewed listing evidence"
        raise SnapshotValidationError(message)


@dataclass(frozen=True)
class MembershipListingEvidence:
    """Typed listing evidence, retaining conflicting vendor and reviewed dates."""

    status: ListingEvidenceStatus
    vendor_reported_ipo_date: date | None
    provider: ValidationProvider
    provider_symbol: str
    artifact_role: str
    artifact_sha256: str
    endpoint: str
    retrieved_at: datetime
    identity_binding: str
    general_code: str | None
    general_isin: str | None
    general_cik: str | None
    reviewed_listing_date: date | None = None
    reviewed_listing_correction_id: str | None = None
    reviewed_listing_correction_manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ListingEvidenceStatus):
            message = "listing evidence status must be a ListingEvidenceStatus"
            raise SnapshotValidationError(message)
        if not isinstance(self.provider, ValidationProvider):
            message = "listing evidence provider must be a ValidationProvider"
            raise SnapshotValidationError(message)
        normalized_symbol = require_snapshot_text(
            self.provider_symbol,
            field="listing evidence provider_symbol",
        ).upper()
        if any(character.isspace() for character in normalized_symbol):
            message = "listing evidence provider_symbol must contain no whitespace"
            raise SnapshotValidationError(message)
        object.__setattr__(self, "provider_symbol", normalized_symbol.removesuffix(".US"))
        object.__setattr__(
            self,
            "artifact_role",
            require_snapshot_text(self.artifact_role, field="listing evidence artifact_role"),
        )
        try:
            artifact_sha256 = sha256_digest(
                self.artifact_sha256,
                field="listing evidence artifact_sha256",
            )
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(str(exc)) from exc
        object.__setattr__(self, "artifact_sha256", artifact_sha256)
        endpoint = require_snapshot_text(self.endpoint, field="listing evidence endpoint")
        if "api_token" in endpoint.casefold():
            message = "listing evidence endpoint must not contain credentials"
            raise SnapshotValidationError(message)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(
            self,
            "retrieved_at",
            parse_utc_datetime(self.retrieved_at, field="listing evidence retrieved_at"),
        )
        identity_binding = require_snapshot_text(
            self.identity_binding,
            field="listing evidence identity_binding",
        )
        object.__setattr__(self, "identity_binding", identity_binding)
        for field_name in ("general_code", "general_isin", "general_cik"):
            value = getattr(self, field_name)
            if value is not None:
                normalized = require_snapshot_text(
                    value,
                    field=f"listing evidence {field_name}",
                ).upper()
                object.__setattr__(self, field_name, normalized)
        reviewed_id, reviewed_manifest_sha256 = _validated_reviewed_listing_fields(
            listing_date=self.reviewed_listing_date,
            correction_id=self.reviewed_listing_correction_id,
            correction_manifest_sha256=self.reviewed_listing_correction_manifest_sha256,
        )
        object.__setattr__(self, "reviewed_listing_correction_id", reviewed_id)
        object.__setattr__(
            self,
            "reviewed_listing_correction_manifest_sha256",
            reviewed_manifest_sha256,
        )
        _validate_reviewed_listing_identity_binding(
            identity_binding=identity_binding,
            reviewed_listing_date=self.reviewed_listing_date,
        )
        if self.status is ListingEvidenceStatus.BOUND:
            if self.effective_listing_date is None:
                message = "bound listing evidence requires an effective listing date"
                raise SnapshotValidationError(message)
            if identity_binding not in _BOUND_LISTING_IDENTITY_BINDINGS:
                message = "bound listing evidence requires a permanent identity binding"
                raise SnapshotValidationError(message)
        elif self.vendor_reported_ipo_date is not None or self.reviewed_listing_date is not None:
            message = "unavailable listing evidence cannot carry a listing date"
            raise SnapshotValidationError(message)
        if identity_binding == "exact_provider_symbol_code":
            if self.general_code != self.provider_symbol:
                message = "exact listing-evidence Code differs from provider_symbol"
                raise SnapshotValidationError(message)
        elif identity_binding == "matching_permanent_isin" and self.general_isin is None:
            message = "ISIN-bound listing evidence requires general_isin"
            raise SnapshotValidationError(message)
        elif identity_binding == "matching_sec_cik" and self.general_cik is None:
            message = "CIK-bound listing evidence requires general_cik"
            raise SnapshotValidationError(message)

    @property
    def effective_listing_date(self) -> date | None:
        """Return reviewed primary evidence when present, else the vendor observation."""

        return self.reviewed_listing_date or self.vendor_reported_ipo_date

    @property
    def evidence_id(self) -> str:
        """Bind every normalized listing-evidence field into interval identity."""

        return evidence_sha256(
            {
                "artifact_role": self.artifact_role,
                "artifact_sha256": self.artifact_sha256,
                "endpoint": self.endpoint,
                "general_cik": self.general_cik,
                "general_code": self.general_code,
                "general_isin": self.general_isin,
                "identity_binding": self.identity_binding,
                "provider": self.provider.value,
                "provider_symbol": self.provider_symbol,
                "retrieved_at": self.retrieved_at.isoformat(),
                "reviewed_listing_correction_id": self.reviewed_listing_correction_id,
                "reviewed_listing_correction_manifest_sha256": (
                    self.reviewed_listing_correction_manifest_sha256
                ),
                "reviewed_listing_date": (
                    self.reviewed_listing_date.isoformat()
                    if self.reviewed_listing_date is not None
                    else None
                ),
                "status": self.status.value,
                "vendor_reported_ipo_date": (
                    self.vendor_reported_ipo_date.isoformat()
                    if self.vendor_reported_ipo_date is not None
                    else None
                ),
            }
        )


@dataclass(frozen=True)
class MembershipInterval:
    """One source-identified point-in-time membership interval."""

    security_id: str
    canonical_symbol: str
    effective_from: date
    effective_to: date | None
    source_ref: str
    notes: str
    source_authority: MembershipSourceAuthority | None = None
    listing_evidence: MembershipListingEvidence | None = None

    @property
    def interval_id(self) -> str:
        return evidence_sha256(
            {
                "canonical_symbol": self.canonical_symbol,
                "effective_from": self.effective_from.isoformat(),
                "effective_to": (self.effective_to.isoformat() if self.effective_to else None),
                "security_id": self.security_id,
                "listing_evidence_id": (
                    self.listing_evidence.evidence_id if self.listing_evidence else None
                ),
                "source_authority_id": (
                    self.source_authority.authority_id if self.source_authority else None
                ),
                "source_ref": self.source_ref,
            }
        )


@dataclass(frozen=True)
class HistoricalAlias:
    """One dated, sourced mapping from internal security to provider symbol."""

    security_id: str
    canonical_symbol: str
    provider: str
    provider_symbol: str
    effective_from: date
    effective_to: date | None
    source_ref: str

    @property
    def alias_id(self) -> str:
        return evidence_sha256(
            {
                "canonical_symbol": self.canonical_symbol,
                "effective_from": self.effective_from.isoformat(),
                "effective_to": (self.effective_to.isoformat() if self.effective_to else None),
                "provider": self.provider,
                "provider_symbol": self.provider_symbol,
                "security_id": self.security_id,
                "source_ref": self.source_ref,
            }
        )


@dataclass(frozen=True)
class ProviderSegment:
    """Maximal date segment resolved to one provider symbol."""

    start: date
    end: date
    provider_symbol: str
    alias_id: str | None
    alias_source_ref: str | None


def _market_session_text(
    payload: Mapping[str, Any],
    field: str,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        message = f"market-session artifact {field} must be a string"
        raise SnapshotValidationError(message)
    return require_snapshot_text(value, field=f"market-session artifact {field}")


def _market_session_date(
    payload: Mapping[str, Any],
    field: str,
) -> date:
    try:
        return date.fromisoformat(_market_session_text(payload, field))
    except ValueError as exc:
        message = f"market-session artifact {field} must be an ISO date"
        raise SnapshotValidationError(message) from exc


def _market_session_rows(
    payload: Mapping[str, Any],
    *,
    venue: str,
    source: str,
    source_revision: str,
    source_content_sha256: str,
) -> tuple[OfficialSessionCutoff, ...]:
    raw_sessions = payload.get("sessions")
    if not isinstance(raw_sessions, list) or not raw_sessions:
        message = "market-session artifact sessions must be a non-empty list"
        raise SnapshotValidationError(message)
    sessions: list[OfficialSessionCutoff] = []
    authority = (
        SessionAuthority.RESEARCH_LIBRARY
        if source.startswith("library_generated:")
        else (
            SessionAuthority.OFFICIAL_EXCHANGE
            if source.startswith("exchange:")
            else SessionAuthority.AUTHENTICATED_BROKER
        )
    )
    source_identity = (
        f"research-calendar:{source}:{source_revision}"
        if authority is SessionAuthority.RESEARCH_LIBRARY
        else f"{source}:{source_revision}"
    )
    for row_number, raw_row in enumerate(raw_sessions, start=1):
        if not isinstance(raw_row, Mapping):
            message = f"market-session artifact row {row_number} must be an object"
            raise SnapshotValidationError(message)
        session_date = _market_session_date(raw_row, "session_date")
        try:
            opens_at = parse_utc_datetime(
                raw_row.get("opens_at"),
                field=f"market-session row {row_number} opens_at",
                strict_utc=True,
            )
            closes_at = parse_utc_datetime(
                raw_row.get("closes_at"),
                field=f"market-session row {row_number} closes_at",
                strict_utc=True,
            )
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(str(exc)) from exc
        sessions.append(
            OfficialSessionCutoff(
                mic=venue,
                session_date=session_date,
                opens_at=opens_at,
                closes_at=closes_at,
                authority=authority,
                source_identity=source_identity,
                content_sha256=evidence_sha256(
                    {
                        "closes_at": closes_at.isoformat(),
                        "opens_at": opens_at.isoformat(),
                        "session_date": session_date.isoformat(),
                        "source_content_sha256": source_content_sha256,
                        "source_revision": source_revision,
                        "venue": venue,
                    }
                ),
            )
        )
    return tuple(sessions)


def read_historical_market_sessions(
    path: Path,
    *,
    expected_content_sha256: str,
    expected_venue: str,
    expected_from: date,
    expected_to: date,
    require_authoritative: bool = True,
) -> HistoricalMarketSessions:
    """Read one content-pinned historical exchange/broker or diagnostic schedule."""

    try:
        pinned_sha256 = sha256_digest(
            expected_content_sha256,
            field="official market-session content SHA-256",
        )
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(str(exc)) from exc
    observed_sha256 = file_sha256(path)
    if observed_sha256 != pinned_sha256:
        message = (
            "official market-session artifact differs from its caller-supplied content SHA-256"
        )
        raise SnapshotValidationError(message)
    content = path.read_bytes()
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        message = "official market-session artifact must be valid UTF-8 JSON"
        raise SnapshotValidationError(message) from exc
    if not isinstance(decoded, Mapping):
        message = "official market-session artifact root must be an object"
        raise SnapshotValidationError(message)
    if decoded.get("schema") != HISTORICAL_MARKET_SESSIONS_SCHEMA:
        message = "official market-session artifact schema is unsupported"
        raise SnapshotValidationError(message)
    venue = _market_session_text(decoded, "venue").upper()
    if venue != expected_venue.strip().upper():
        message = (
            f"official market-session venue {venue!r} differs from "
            f"requested venue {expected_venue!r}"
        )
        raise SnapshotValidationError(message)
    try:
        source_kind = MarketSessionSourceKind(_market_session_text(decoded, "source_kind").lower())
    except ValueError as exc:
        message = "official market-session source_kind must be exchange or broker"
        raise SnapshotValidationError(message) from exc
    if require_authoritative and source_kind is MarketSessionSourceKind.LIBRARY_GENERATED:
        message = "caller-supplied market-session authority cannot be library-generated"
        raise SnapshotValidationError(message)
    source_reference = _market_session_text(decoded, "source_reference")
    if not source_reference.startswith("https://"):
        message = "official market-session source_reference must use HTTPS"
        raise SnapshotValidationError(message)
    coverage_from = _market_session_date(decoded, "coverage_from")
    coverage_to = _market_session_date(decoded, "coverage_to")
    if (coverage_from, coverage_to) != (expected_from, expected_to):
        message = (
            "official market-session artifact coverage differs from the requested snapshot period"
        )
        raise SnapshotValidationError(message)
    if decoded.get("coverage_complete") is not True:
        message = "official market-session artifact must declare complete coverage"
        raise SnapshotValidationError(message)
    if source_kind is MarketSessionSourceKind.LIBRARY_GENERATED:
        retrieved_at = None
    else:
        try:
            retrieved_at = parse_utc_datetime(
                decoded.get("retrieved_at"),
                field="official market-session retrieved_at",
            )
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(str(exc)) from exc
    provider = _market_session_text(decoded, "provider")
    dataset = _market_session_text(decoded, "dataset")
    dataset_version = _market_session_text(decoded, "dataset_version")
    entitlement_scope = _market_session_text(decoded, "entitlement_scope")
    source_revision = _market_session_text(decoded, "source_revision")
    timestamp_semantics = _market_session_text(decoded, "timestamp_semantics")
    source = f"{source_kind.value}:{provider}:{source_reference}"
    sessions = _market_session_rows(
        decoded,
        venue=venue,
        source=source,
        source_revision=source_revision,
        source_content_sha256=pinned_sha256,
    )
    return HistoricalMarketSessions(
        venue=venue,
        source_kind=source_kind,
        provider=provider,
        dataset=dataset,
        dataset_version=dataset_version,
        entitlement_scope=entitlement_scope,
        source_reference=source_reference,
        source_revision=source_revision,
        timestamp_semantics=timestamp_semantics,
        source_content_sha256=pinned_sha256,
        retrieved_at=retrieved_at,
        coverage_from=coverage_from,
        coverage_to=coverage_to,
        coverage_complete=True,
        sessions=sessions,
        content=content,
    )


def build_diagnostic_market_sessions(
    *,
    venue: str,
    start: date,
    end: date,
    library_version: str,
) -> HistoricalMarketSessions:
    """Build explicitly diagnostic session timing from ``exchange_calendars``."""

    normalized_venue = require_snapshot_text(
        venue,
        field="diagnostic market-session venue",
    ).upper()
    source_revision = require_snapshot_text(
        library_version,
        field="diagnostic market-session library version",
    )
    rows = [
        {
            "closes_at": session_close_utc(normalized_venue, session)
            .replace(tzinfo=UTC)
            .isoformat(),
            "opens_at": session_open_utc(normalized_venue, session).replace(tzinfo=UTC).isoformat(),
            "session_date": session.isoformat(),
        }
        for session in sessions_between(normalized_venue, start, end)
    ]
    payload: dict[str, object] = {
        "coverage_complete": True,
        "coverage_from": start.isoformat(),
        "coverage_to": end.isoformat(),
        "dataset": "exchange_calendars generated schedule",
        "dataset_version": source_revision,
        "entitlement_scope": "public",
        "provider": "exchange_calendars",
        "retrieved_at": None,
        "schema": HISTORICAL_MARKET_SESSIONS_SCHEMA,
        "sessions": rows,
        "source_kind": MarketSessionSourceKind.LIBRARY_GENERATED.value,
        "source_reference": "https://pypi.org/project/exchange-calendars/",
        "source_revision": source_revision,
        "timestamp_semantics": "library-derived regular-session UTC open and close",
        "venue": normalized_venue,
    }
    content = canonical_json_bytes(payload)
    content_sha256 = hashlib.sha256(content).hexdigest()
    sessions = _market_session_rows(
        payload,
        venue=normalized_venue,
        source="library_generated:exchange_calendars",
        source_revision=source_revision,
        source_content_sha256=content_sha256,
    )
    return HistoricalMarketSessions(
        venue=normalized_venue,
        source_kind=MarketSessionSourceKind.LIBRARY_GENERATED,
        provider="exchange_calendars",
        dataset="exchange_calendars generated schedule",
        dataset_version=source_revision,
        entitlement_scope="public",
        source_reference="https://pypi.org/project/exchange-calendars/",
        source_revision=source_revision,
        timestamp_semantics="library-derived regular-session UTC open and close",
        source_content_sha256=content_sha256,
        retrieved_at=None,
        coverage_from=start,
        coverage_to=end,
        coverage_complete=True,
        sessions=sessions,
        content=content,
    )


def require_snapshot_text(value: str, *, field: str) -> str:
    """Return canonical nonblank snapshot input text."""

    normalized = value.strip()
    if not normalized:
        message = f"{field} must be non-blank"
        raise SnapshotValidationError(message)
    return normalized


def _parse_optional_date(value: str) -> date | None:
    normalized = value.strip()
    return date.fromisoformat(normalized) if normalized else None


def _optional_membership_text(value: str, *, field: str) -> str | None:
    normalized = value.strip()
    return require_snapshot_text(normalized, field=field) if normalized else None


def _normalized_symbol(value: str, *, field: str) -> str:
    normalized = require_snapshot_text(value, field=field).upper()
    if any(char.isspace() for char in normalized):
        message = f"{field} must not contain whitespace"
        raise SnapshotValidationError(message)
    return normalized


def read_membership_intervals(path: Path) -> tuple[MembershipInterval, ...]:
    """Read and interval-validate membership without inferring source identity."""

    required = {"ticker", "effective_from", "effective_to", "source_ref"}
    authority_fields = {
        "source_dataset",
        "source_kind",
        "source_provider",
        "source_revision_sha256",
    }
    listing_evidence_fields = {
        "listing_evidence_artifact_role",
        "listing_evidence_artifact_sha256",
        "listing_evidence_endpoint",
        "listing_evidence_provider",
        "listing_evidence_provider_symbol",
        "listing_evidence_retrieved_at",
        "listing_evidence_status",
        "listing_general_cik",
        "listing_general_code",
        "listing_general_isin",
        "listing_identity_binding",
        "reviewed_listing_correction_id",
        "reviewed_listing_correction_manifest_sha256",
        "reviewed_listing_date",
        "vendor_reported_ipo_date",
    }
    rows: list[MembershipInterval] = []
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            message = f"membership CSV must contain {sorted(required)}: {path}"
            raise SnapshotValidationError(message)
        present_authority_fields = authority_fields.intersection(reader.fieldnames)
        if present_authority_fields and present_authority_fields != authority_fields:
            missing = sorted(authority_fields - present_authority_fields)
            message = f"membership source authority fields are incomplete; missing={missing}"
            raise SnapshotValidationError(message)
        has_source_authority = present_authority_fields == authority_fields
        present_listing_fields = listing_evidence_fields.intersection(reader.fieldnames)
        if present_listing_fields and present_listing_fields != listing_evidence_fields:
            missing = sorted(listing_evidence_fields - present_listing_fields)
            message = f"membership listing-evidence fields are incomplete; missing={missing}"
            raise SnapshotValidationError(message)
        has_listing_evidence = present_listing_fields == listing_evidence_fields
        has_security_id = "security_id" in reader.fieldnames
        for row_number, row in enumerate(reader, start=2):
            try:
                symbol = _normalized_symbol(
                    row["ticker"],
                    field=f"membership row {row_number} ticker",
                )
                security_id = (
                    require_snapshot_text(
                        row.get("security_id", ""),
                        field=f"membership row {row_number} security_id",
                    )
                    if has_security_id
                    else symbol
                )
                effective_from = date.fromisoformat(row["effective_from"].strip())
                effective_to = _parse_optional_date(row["effective_to"])
                source_ref = require_snapshot_text(
                    row["source_ref"],
                    field=f"membership row {row_number} source_ref",
                )
                source_authority = (
                    MembershipSourceAuthority(
                        kind=MembershipSourceKind(
                            require_snapshot_text(
                                row["source_kind"],
                                field=f"membership row {row_number} source_kind",
                            ).lower()
                        ),
                        provider=require_snapshot_text(
                            row["source_provider"],
                            field=f"membership row {row_number} source_provider",
                        ),
                        dataset=require_snapshot_text(
                            row["source_dataset"],
                            field=f"membership row {row_number} source_dataset",
                        ),
                        revision_sha256=require_snapshot_text(
                            row["source_revision_sha256"],
                            field=(f"membership row {row_number} source_revision_sha256"),
                        ),
                    )
                    if has_source_authority
                    else None
                )
                listing_evidence = (
                    MembershipListingEvidence(
                        status=ListingEvidenceStatus(
                            require_snapshot_text(
                                row["listing_evidence_status"],
                                field=(f"membership row {row_number} listing_evidence_status"),
                            ).lower()
                        ),
                        vendor_reported_ipo_date=_parse_optional_date(
                            row["vendor_reported_ipo_date"]
                        ),
                        provider=ValidationProvider(
                            require_snapshot_text(
                                row["listing_evidence_provider"],
                                field=(f"membership row {row_number} listing_evidence_provider"),
                            ).lower()
                        ),
                        provider_symbol=require_snapshot_text(
                            row["listing_evidence_provider_symbol"],
                            field=(f"membership row {row_number} listing_evidence_provider_symbol"),
                        ),
                        artifact_role=require_snapshot_text(
                            row["listing_evidence_artifact_role"],
                            field=(f"membership row {row_number} listing_evidence_artifact_role"),
                        ),
                        artifact_sha256=require_snapshot_text(
                            row["listing_evidence_artifact_sha256"],
                            field=(f"membership row {row_number} listing_evidence_artifact_sha256"),
                        ),
                        endpoint=require_snapshot_text(
                            row["listing_evidence_endpoint"],
                            field=f"membership row {row_number} listing_evidence_endpoint",
                        ),
                        retrieved_at=parse_utc_datetime(
                            row["listing_evidence_retrieved_at"],
                            field=(f"membership row {row_number} listing_evidence_retrieved_at"),
                        ),
                        identity_binding=require_snapshot_text(
                            row["listing_identity_binding"],
                            field=f"membership row {row_number} listing_identity_binding",
                        ),
                        general_code=_optional_membership_text(
                            row["listing_general_code"],
                            field=f"membership row {row_number} listing_general_code",
                        ),
                        general_isin=_optional_membership_text(
                            row["listing_general_isin"],
                            field=f"membership row {row_number} listing_general_isin",
                        ),
                        general_cik=_optional_membership_text(
                            row["listing_general_cik"],
                            field=f"membership row {row_number} listing_general_cik",
                        ),
                        reviewed_listing_date=_parse_optional_date(row["reviewed_listing_date"]),
                        reviewed_listing_correction_id=_optional_membership_text(
                            row["reviewed_listing_correction_id"],
                            field=(f"membership row {row_number} reviewed_listing_correction_id"),
                        ),
                        reviewed_listing_correction_manifest_sha256=(
                            _optional_membership_text(
                                row["reviewed_listing_correction_manifest_sha256"],
                                field=(
                                    f"membership row {row_number} "
                                    "reviewed_listing_correction_manifest_sha256"
                                ),
                            )
                        ),
                    )
                    if has_listing_evidence
                    else None
                )
            except (KeyError, TypeError, ValueError) as exc:
                message = f"invalid membership row {row_number}: {path}"
                raise SnapshotValidationError(message) from exc
            if effective_to is not None and effective_to < effective_from:
                message = f"membership row {row_number} ends before it starts"
                raise SnapshotValidationError(message)
            rows.append(
                MembershipInterval(
                    security_id=security_id,
                    canonical_symbol=symbol,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    source_ref=source_ref,
                    notes=(row.get("notes") or "").strip(),
                    source_authority=source_authority,
                    listing_evidence=listing_evidence,
                )
            )
    if not rows:
        message = f"membership CSV has no intervals: {path}"
        raise SnapshotValidationError(message)

    by_security: dict[str, list[MembershipInterval]] = defaultdict(list)
    by_symbol: dict[str, list[MembershipInterval]] = defaultdict(list)
    for interval in rows:
        by_security[interval.security_id].append(interval)
        by_symbol[interval.canonical_symbol].append(interval)
    for security_id, intervals in by_security.items():
        ordered = sorted(intervals, key=lambda item: item.effective_from)
        for previous, current in pairwise(ordered):
            previous_end = previous.effective_to or date.max
            if current.effective_from <= previous_end:
                message = f"overlapping membership intervals for {security_id}"
                raise SnapshotValidationError(message)
    for canonical_symbol, intervals in by_symbol.items():
        ordered = sorted(intervals, key=lambda item: item.effective_from)
        for previous, current in pairwise(ordered):
            previous_end = previous.effective_to or date.max
            if current.effective_from <= previous_end:
                message = (
                    f"overlapping membership ticker assignments for {canonical_symbol}: "
                    f"{previous.security_id} and {current.security_id}"
                )
                raise SnapshotValidationError(message)
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                item.security_id,
                item.effective_from,
                item.effective_to or date.max,
            ),
        )
    )


def membership_source_authority_summary(
    intervals: Sequence[MembershipInterval],
) -> dict[str, object]:
    """Summarize typed membership authority without trusting free-form citations."""

    if not intervals:
        message = "membership source authority requires at least one interval"
        raise SnapshotValidationError(message)
    authorities = {
        interval.source_authority.authority_id: interval.source_authority
        for interval in intervals
        if interval.source_authority is not None
    }
    authority_rows = [
        {
            "authority_id": authority_id,
            **authority.to_payload(),
        }
        for authority_id, authority in sorted(authorities.items())
    ]
    untyped_interval_count = sum(interval.source_authority is None for interval in intervals)
    reconstructed_interval_count = sum(
        interval.source_authority is not None
        and not interval.source_authority.confirmatory_eligible
        for interval in intervals
    )
    return {
        "authorities": authority_rows,
        "authority_set_sha256": evidence_sha256(authority_rows),
        "confirmatory_eligible": (
            untyped_interval_count == 0 and reconstructed_interval_count == 0
        ),
        "reconstructed_interval_count": reconstructed_interval_count,
        "untyped_interval_count": untyped_interval_count,
    }


def read_historical_aliases(
    path: Path | None,
    *,
    provider: ValidationProvider,
) -> tuple[HistoricalAlias, ...]:
    """Read dated provider aliases; undocumented substitutions are never inferred."""

    if path is None:
        return ()
    required = {
        "security_id",
        "canonical_symbol",
        "provider",
        "provider_symbol",
        "effective_from",
        "effective_to",
        "source_ref",
    }
    aliases: list[HistoricalAlias] = []
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            message = f"alias CSV must contain {sorted(required)}: {path}"
            raise SnapshotValidationError(message)
        for row_number, row in enumerate(reader, start=2):
            try:
                row_provider = require_snapshot_text(
                    row["provider"],
                    field=f"alias row {row_number} provider",
                ).lower()
                if row_provider != provider.value:
                    continue
                effective_from = date.fromisoformat(row["effective_from"].strip())
                effective_to = _parse_optional_date(row["effective_to"])
                alias = HistoricalAlias(
                    security_id=require_snapshot_text(
                        row["security_id"],
                        field=f"alias row {row_number} security_id",
                    ),
                    canonical_symbol=_normalized_symbol(
                        row["canonical_symbol"],
                        field=f"alias row {row_number} canonical_symbol",
                    ),
                    provider=row_provider,
                    provider_symbol=_normalized_symbol(
                        row["provider_symbol"],
                        field=f"alias row {row_number} provider_symbol",
                    ),
                    effective_from=effective_from,
                    effective_to=effective_to,
                    source_ref=require_snapshot_text(
                        row["source_ref"],
                        field=f"alias row {row_number} source_ref",
                    ),
                )
            except (KeyError, ValueError) as exc:
                message = f"invalid alias row {row_number}: {path}"
                raise SnapshotValidationError(message) from exc
            if effective_to is not None and effective_to < effective_from:
                message = f"alias row {row_number} ends before it starts"
                raise SnapshotValidationError(message)
            aliases.append(alias)

    by_security_symbol: dict[tuple[str, str], list[HistoricalAlias]] = defaultdict(list)
    by_provider_symbol: dict[str, list[HistoricalAlias]] = defaultdict(list)
    for alias in aliases:
        by_security_symbol[(alias.security_id, alias.canonical_symbol)].append(alias)
        by_provider_symbol[alias.provider_symbol].append(alias)
    for (security_id, canonical_symbol), values in by_security_symbol.items():
        ordered = sorted(values, key=lambda item: item.effective_from)
        for previous, current in pairwise(ordered):
            previous_end = previous.effective_to or date.max
            if current.effective_from <= previous_end:
                message = (
                    f"overlapping {provider.value} aliases for {security_id}/{canonical_symbol}"
                )
                raise SnapshotValidationError(message)
    for provider_symbol, values in by_provider_symbol.items():
        ordered = sorted(values, key=lambda item: item.effective_from)
        for previous, current in pairwise(ordered):
            previous_end = previous.effective_to or date.max
            if (
                current.effective_from <= previous_end
                and current.security_id != previous.security_id
            ):
                message = (
                    f"overlapping {provider.value} provider-symbol assignments for "
                    f"{provider_symbol}: {previous.security_id} and {current.security_id}"
                )
                raise SnapshotValidationError(message)
    return tuple(
        sorted(
            aliases,
            key=lambda item: (
                item.security_id,
                item.effective_from,
                item.effective_to or date.max,
            ),
        )
    )


def interval_request_bounds(
    interval: MembershipInterval,
    request: SnapshotRequestBoundary,
) -> tuple[date, date] | None:
    """Return the membership-eligible overlap with the snapshot request."""

    interval_end = interval.effective_to or request.end
    overlap_start = max(request.start, interval.effective_from)
    overlap_end = min(request.end, interval_end)
    if overlap_start > overlap_end:
        return None
    return overlap_start, overlap_end


def interval_history_bounds(
    interval: MembershipInterval,
    request: SnapshotRequestBoundary,
    *,
    session_axis: Sequence[date] | None = None,
) -> tuple[date, date] | None:
    """Return pre-membership price-only sessions needed by registered factors."""

    if request.security_history_sessions < _MIN_HISTORY_AXIS_SESSIONS:
        return None
    eligibility_bounds = interval_request_bounds(interval, request)
    if eligibility_bounds is None:
        return None
    eligible_sessions = [
        session
        for session in (
            session_axis
            if session_axis is not None
            else sessions_between(
                request.calendar_mic,
                request.start,
                request.end,
            )
        )
        if eligibility_bounds[0] <= session <= eligibility_bounds[1]
    ]
    if not eligible_sessions:
        return None
    first_eligible = eligible_sessions[0]
    available_axis = [
        session
        for session in (
            session_axis
            if session_axis is not None
            else sessions_between(
                request.calendar_mic,
                request.start,
                first_eligible,
            )
        )
        if request.start <= session <= first_eligible
    ]
    if len(available_axis) < _MIN_HISTORY_AXIS_SESSIONS:
        return None
    first_index = max(0, len(available_axis) - request.security_history_sessions)
    history_start = available_axis[first_index]
    history_end = available_axis[-2]
    return (history_start, history_end) if history_start <= history_end else None


def resolve_provider_segments(
    *,
    security_id: str,
    canonical_symbol: str,
    start: date,
    end: date,
    aliases: Sequence[HistoricalAlias],
) -> tuple[ProviderSegment, ...]:
    """Resolve a finite request period into non-overlapping dated symbol segments."""

    applicable = [
        alias
        for alias in aliases
        if alias.security_id == security_id
        and alias.canonical_symbol == canonical_symbol
        and alias.effective_from <= end
        and (alias.effective_to is None or alias.effective_to >= start)
    ]
    boundaries = {start, end + timedelta(days=1)}
    for alias in applicable:
        boundaries.add(max(start, alias.effective_from))
        alias_end = min(end, alias.effective_to or end)
        boundaries.add(alias_end + timedelta(days=1))
    ordered = sorted(boundaries)
    segments: list[ProviderSegment] = []
    for left, right_exclusive in pairwise(ordered):
        segment_end = right_exclusive - timedelta(days=1)
        selected = [
            alias
            for alias in applicable
            if alias.effective_from <= left
            and (alias.effective_to is None or left <= alias.effective_to)
        ]
        if len(selected) > 1:
            message = f"ambiguous provider alias for {security_id} on {left}"
            raise SnapshotValidationError(message)
        resolved_alias = selected[0] if selected else None
        segment = ProviderSegment(
            start=left,
            end=segment_end,
            provider_symbol=(
                resolved_alias.provider_symbol if resolved_alias else canonical_symbol
            ),
            alias_id=resolved_alias.alias_id if resolved_alias else None,
            alias_source_ref=(resolved_alias.source_ref if resolved_alias else None),
        )
        if (
            segments
            and segments[-1].provider_symbol == segment.provider_symbol
            and segments[-1].alias_id == segment.alias_id
            and segments[-1].end + timedelta(days=1) == segment.start
        ):
            previous = segments[-1]
            segments[-1] = ProviderSegment(
                start=previous.start,
                end=segment.end,
                provider_symbol=previous.provider_symbol,
                alias_id=previous.alias_id,
                alias_source_ref=previous.alias_source_ref,
            )
        else:
            segments.append(segment)
    return tuple(segments)


def validate_resolved_provider_symbol_assignments(
    *,
    request: SnapshotRequestBoundary,
    intervals: Sequence[MembershipInterval],
    aliases: Sequence[HistoricalAlias],
) -> None:
    """Reject overlapping internal assignments to one provider symbol."""

    assignments: dict[
        str,
        list[tuple[date, date, MembershipInterval]],
    ] = defaultdict(list)
    for interval in intervals:
        bounds = interval_request_bounds(interval, request)
        if bounds is None:
            continue
        start, end = bounds
        for segment in resolve_provider_segments(
            security_id=interval.security_id,
            canonical_symbol=interval.canonical_symbol,
            start=start,
            end=end,
            aliases=aliases,
        ):
            assignments[segment.provider_symbol].append((segment.start, segment.end, interval))
    for provider_symbol, values in assignments.items():
        ordered = sorted(
            values,
            key=lambda item: (item[0], item[1], item[2].interval_id),
        )
        for previous, current in pairwise(ordered):
            if current[0] <= previous[1]:
                message = (
                    f"ambiguous resolved {request.provider.value} symbol "
                    f"{provider_symbol} between membership intervals "
                    f"{previous[2].interval_id} and {current[2].interval_id}"
                )
                raise SnapshotValidationError(message)


def validate_alias_membership_alignment(
    *,
    intervals: Sequence[MembershipInterval],
    aliases: Sequence[HistoricalAlias],
) -> None:
    """Reject aliases that conflict with point-in-time membership tickers."""

    intervals_by_security: dict[str, list[MembershipInterval]] = defaultdict(list)
    for interval in intervals:
        intervals_by_security[interval.security_id].append(interval)
    for alias in aliases:
        alias_end = alias.effective_to or date.max
        matching_intervals = tuple(
            interval
            for interval in intervals_by_security[alias.security_id]
            if interval.canonical_symbol == alias.canonical_symbol
        )
        if not matching_intervals:
            message = (
                f"{alias.provider} alias {alias.alias_id} has no matching "
                f"membership ticker for {alias.security_id}"
            )
            raise SnapshotValidationError(message)
        first_matching_start = min(interval.effective_from for interval in matching_intervals)
        for interval in intervals_by_security[alias.security_id]:
            interval_end = interval.effective_to or date.max
            overlaps = alias.effective_from <= interval_end and interval.effective_from <= alias_end
            if overlaps and alias.canonical_symbol != interval.canonical_symbol:
                if alias_end < first_matching_start:
                    continue
                message = (
                    f"{alias.provider} alias {alias.alias_id} for "
                    f"{alias.canonical_symbol} overlaps membership ticker "
                    f"{interval.canonical_symbol} for {alias.security_id}"
                )
                raise SnapshotValidationError(message)


__all__ = [
    "HISTORICAL_MARKET_SESSIONS_SCHEMA",
    "HistoricalAlias",
    "HistoricalMarketSessions",
    "ListingEvidenceStatus",
    "MarketSessionSourceKind",
    "MembershipInterval",
    "MembershipListingEvidence",
    "MembershipSourceAuthority",
    "MembershipSourceKind",
    "ProviderSegment",
    "SnapshotRequestBoundary",
    "SnapshotValidationError",
    "StrategyIdentity",
    "ValidationProvider",
    "build_diagnostic_market_sessions",
    "build_strategy_identity",
    "interval_history_bounds",
    "interval_request_bounds",
    "membership_source_authority_summary",
    "read_historical_aliases",
    "read_historical_market_sessions",
    "read_membership_intervals",
    "require_snapshot_text",
    "resolve_provider_segments",
    "validate_alias_membership_alignment",
    "validate_resolved_provider_symbol_assignments",
]
