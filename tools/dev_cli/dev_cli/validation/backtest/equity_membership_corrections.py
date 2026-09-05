"""Reviewed primary-source corrections for licensed index membership evidence.

Acquisition freezes exact bytes from canonical HTTPS source specifications.
Application accepts only the resulting content-addressed manifest; it never
trusts an unfrozen URL, caller-provided hash, or a component snapshot as a
membership correction.
"""

from __future__ import annotations

import ipaddress
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import httpx

from dev_cli.validation.evidence import (
    ContentAddressedArtifact,
    canonical_json_bytes,
    content_manifest_artifacts,
    evidence_sha256,
    file_sha256,
    load_content_addressed_manifest,
    nonblank_string,
    parse_utc_datetime,
    store_content_object,
    verified_content_path,
    write_content_addressed_manifest,
)
from lib_common.http_exceptions import is_retryable_status
from lib_common.logging import get_logger
from lib_common.retries import retry_sync

logger = get_logger(__name__)

CORRECTION_SOURCE_SPEC_SCHEMA: Final = "vynmatrix.eodhd-membership-correction-source-spec.v1"
CORRECTION_EVIDENCE_MANIFEST_SCHEMA: Final = "vynmatrix.eodhd-membership-correction-evidence.v1"
_SPEC_ROLE: Final = "eodhd_membership_correction_source_spec"
_SOURCE_ROLE: Final = "eodhd_membership_primary_source_document"
_MANIFEST_ROLE: Final = "eodhd_membership_correction_evidence_manifest"
_MAX_SOURCE_BYTES: Final = 10 * 1024 * 1024
_MAX_CIK_DIGITS: Final = 10
_MAX_RETRIES: Final = 5
_MAX_TIMEOUT_SECONDS: Final = 120.0
_DEFAULT_USER_AGENT: Final = "vynmatrix/1.0 membership-correction-evidence"


class MembershipCorrectionError(RuntimeError):
    """A correction source, frozen manifest, or exact-row precondition is invalid."""


class _RetryableMembershipCorrectionError(MembershipCorrectionError):
    """Internal marker for bounded source transport retries."""


class MembershipCorrectionAction(StrEnum):
    """Whether reviewed evidence may change membership intervals."""

    REPLACE_EXACT_INTERVAL = "replace_exact_interval"
    IDENTITY_BRIDGE_ONLY = "identity_bridge_only"


class MembershipIdentityBridgeKind(StrEnum):
    """Reviewed legal-security relationship; none implies interval continuity."""

    INDEX_MEMBERSHIP_CORRECTION = "index_membership_correction"
    SAME_SECURITY_RENAME = "same_security_rename"
    SHARE_CLASS_CONVERGENCE = "share_class_convergence"
    HOLDING_COMPANY_SUCCESSOR = "holding_company_successor"
    MERGER_EXCHANGE = "merger_exchange"
    SPINOFF_CONTINUATION = "spinoff_continuation"


class MembershipIdentityEdgeUsage(StrEnum):
    """Whether a reviewed edge may resolve provider symbols for price acquisition."""

    PROVIDER_ALIAS_SAME_CLASS = "provider_alias_same_class"
    IDENTITY_RELATION_ONLY = "identity_relation_only"


class MembershipCorrectionSourceKind(StrEnum):
    """Permitted primary-source authority classes."""

    INDEX_PROVIDER_NOTICE = "index_provider_notice"
    SEC_FILING = "sec_filing"
    ISSUER_RELEASE = "issuer_release"


@dataclass(frozen=True, slots=True)
class MembershipCorrectionSource:
    source_id: str
    kind: MembershipCorrectionSourceKind
    url: str
    document_date: date


@dataclass(frozen=True, slots=True)
class MembershipCorrectionIdentity:
    code: str
    name: str | None
    provider_symbol: str | None
    cik: str
    security_id: str
    valid_from: date | None
    valid_to_exclusive: date | None
    is_active_now: bool | None
    is_delisted: bool | None


@dataclass(frozen=True, slots=True)
class MembershipCorrectionInterval:
    code: str
    name: str
    start_inclusive: date | None
    end_exclusive: date | None
    is_active_now: bool
    is_delisted: bool


@dataclass(frozen=True, slots=True)
class ReviewedMembershipCorrection:
    correction_id: str
    action: MembershipCorrectionAction
    bridge_kind: MembershipIdentityBridgeKind
    edge_usage: MembershipIdentityEdgeUsage | None
    effective_date: date
    reviewed_listing_date: date | None
    valid_from: date
    valid_to_exclusive: date | None
    old_identity: MembershipCorrectionIdentity
    new_identity: MembershipCorrectionIdentity
    expected_ticker_interval: MembershipCorrectionInterval | None
    replacement_intervals: tuple[MembershipCorrectionInterval, ...]
    source_ids: tuple[str, ...]
    rationale: str


@dataclass(frozen=True, slots=True)
class MembershipCorrectionSourceSpec:
    reviewed_by: str
    reviewed_at: datetime
    sources: tuple[MembershipCorrectionSource, ...]
    corrections: tuple[ReviewedMembershipCorrection, ...]
    content: bytes
    content_sha256: str


@dataclass(frozen=True, slots=True)
class FrozenMembershipCorrectionEvidence:
    manifest_path: Path
    manifest_sha256: str
    spec: MembershipCorrectionSourceSpec
    artifacts: tuple[ContentAddressedArtifact, ...]


@dataclass(frozen=True, slots=True)
class MembershipCorrectionAcquisitionConfig:
    timeout_seconds: float = 30.0
    retries: int = 2
    retry_base_delay_seconds: float = 0.5
    retry_max_delay_seconds: float = 5.0
    user_agent: str = _DEFAULT_USER_AGENT

    def __post_init__(self) -> None:
        for field_name in (
            "timeout_seconds",
            "retry_base_delay_seconds",
            "retry_max_delay_seconds",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                message = f"membership correction {field_name} must be finite"
                raise ValueError(message)
        if not 0 < self.timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            message = (
                f"membership correction timeout_seconds must be in (0, {_MAX_TIMEOUT_SECONDS}]"
            )
            raise ValueError(message)
        if not 0 <= self.retries <= _MAX_RETRIES:
            message = f"membership correction retries must be between 0 and {_MAX_RETRIES}"
            raise ValueError(message)
        if self.retry_base_delay_seconds < 0 or (
            self.retry_max_delay_seconds < self.retry_base_delay_seconds
        ):
            message = "membership correction retry delays are invalid"
            raise ValueError(message)
        nonblank_string(self.user_agent, field="membership correction user_agent")


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        message = f"{field} must be an object"
        raise MembershipCorrectionError(message)
    return value


def _rows(value: object, *, field: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        message = f"{field} must be an array"
        raise MembershipCorrectionError(message)
    result: list[Mapping[str, Any]] = []
    for index, row in enumerate(value):
        result.append(_mapping(row, field=f"{field}[{index}]"))
    return tuple(result)


def _text(value: object, *, field: str) -> str:
    try:
        return nonblank_string(value, field=field)
    except (TypeError, ValueError) as exc:
        raise MembershipCorrectionError(str(exc)) from exc


def _symbol(value: object, *, field: str) -> str:
    symbol = _text(value, field=field).upper().removesuffix(".US")
    if any(character.isspace() for character in symbol):
        message = f"{field} must not contain whitespace"
        raise MembershipCorrectionError(message)
    return symbol


def _date(value: object, *, field: str, optional: bool = False) -> date | None:
    if value is None and optional:
        return None
    try:
        return date.fromisoformat(_text(value, field=field))
    except ValueError as exc:
        message = f"{field} must be an ISO-8601 date"
        raise MembershipCorrectionError(message) from exc


def _flag(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        message = f"{field} must be a boolean"
        raise MembershipCorrectionError(message)
    return value


def _identity(
    value: object,
    *,
    field: str,
    require_endpoint: bool,
) -> MembershipCorrectionIdentity:
    row = _mapping(value, field=field)
    cik = _text(row.get("cik"), field=f"{field}.cik")
    if not cik.isdigit() or len(cik) > _MAX_CIK_DIGITS:
        message = f"{field}.cik must contain at most {_MAX_CIK_DIGITS} digits"
        raise MembershipCorrectionError(message)
    normalized_cik = cik.zfill(_MAX_CIK_DIGITS)
    security_id = _text(row.get("security_id"), field=f"{field}.security_id")
    if security_id.startswith("cik:") and security_id != f"cik:{normalized_cik}":
        message = f"{field}.security_id contradicts its CIK"
        raise MembershipCorrectionError(message)
    endpoint_fields = {
        "provider_symbol",
        "name",
        "valid_from",
        "valid_to_exclusive",
        "is_active_now",
        "is_delisted",
    }
    declared_endpoint_fields = endpoint_fields.intersection(row)
    if declared_endpoint_fields and declared_endpoint_fields != endpoint_fields:
        missing = sorted(endpoint_fields - declared_endpoint_fields)
        message = f"{field} reviewed endpoint is partial; missing {missing}"
        raise MembershipCorrectionError(message)
    parse_endpoint = require_endpoint or bool(declared_endpoint_fields)
    provider_symbol = (
        _symbol(row.get("provider_symbol"), field=f"{field}.provider_symbol")
        if parse_endpoint
        else None
    )
    name = _text(row.get("name"), field=f"{field}.name") if parse_endpoint else None
    valid_from = (
        _date(row.get("valid_from"), field=f"{field}.valid_from") if parse_endpoint else None
    )
    valid_to = (
        _date(
            row.get("valid_to_exclusive"),
            field=f"{field}.valid_to_exclusive",
            optional=True,
        )
        if parse_endpoint
        else None
    )
    is_active_now = (
        _flag(row.get("is_active_now"), field=f"{field}.is_active_now") if parse_endpoint else None
    )
    is_delisted = (
        _flag(row.get("is_delisted"), field=f"{field}.is_delisted") if parse_endpoint else None
    )
    if parse_endpoint:
        if not security_id.startswith(("figi:", "isin:")) or not security_id.partition(":")[2]:
            message = f"{field}.security_id must be a class-qualified figi: or isin: identity"
            raise MembershipCorrectionError(message)
        assert valid_from is not None
        if valid_to is not None and valid_to <= valid_from:
            message = f"{field} validity must be a non-empty half-open interval"
            raise MembershipCorrectionError(message)
    return MembershipCorrectionIdentity(
        code=_symbol(row.get("code"), field=f"{field}.code"),
        name=name,
        provider_symbol=provider_symbol,
        cik=normalized_cik,
        security_id=security_id,
        valid_from=valid_from,
        valid_to_exclusive=valid_to,
        is_active_now=is_active_now,
        is_delisted=is_delisted,
    )


def _validate_replacement_identity_endpoints(
    *,
    replacements: tuple[MembershipCorrectionInterval, ...],
    old_identity: MembershipCorrectionIdentity,
    new_identity: MembershipCorrectionIdentity,
    field: str,
) -> None:
    complete_identities = tuple(
        identity
        for identity in (old_identity, new_identity)
        if identity.provider_symbol is not None
    )
    for replacement in replacements:
        candidates = {
            identity for identity in complete_identities if identity.code == replacement.code
        }
        if len(candidates) > 1:
            message = (
                f"{field} has ambiguous reviewed endpoints for replacement code {replacement.code}"
            )
            raise MembershipCorrectionError(message)
        if not candidates:
            continue
        identity = next(iter(candidates))
        assert identity.name is not None
        assert identity.valid_from is not None
        if replacement.start_inclusive is None:
            message = f"{field} reviewed replacement endpoint requires an explicit start"
            raise MembershipCorrectionError(message)
        if identity.name != replacement.name:
            message = f"{field} reviewed replacement endpoint name differs from its interval"
            raise MembershipCorrectionError(message)
        if (
            identity.is_active_now is not replacement.is_active_now
            or identity.is_delisted is not replacement.is_delisted
        ):
            message = f"{field} reviewed replacement endpoint status differs from its interval"
            raise MembershipCorrectionError(message)
        if replacement.start_inclusive < identity.valid_from or (
            identity.valid_to_exclusive is not None
            and (
                replacement.end_exclusive is None
                or replacement.end_exclusive > identity.valid_to_exclusive
            )
        ):
            message = f"{field} replacement interval exceeds its reviewed identity endpoint"
            raise MembershipCorrectionError(message)


def _interval(value: object, *, field: str) -> MembershipCorrectionInterval:
    row = _mapping(value, field=field)
    start = _date(row.get("start_inclusive"), field=f"{field}.start_inclusive", optional=True)
    end = _date(row.get("end_exclusive"), field=f"{field}.end_exclusive", optional=True)
    if start is not None and end is not None and end <= start:
        message = f"{field} must be a non-empty half-open interval"
        raise MembershipCorrectionError(message)
    return MembershipCorrectionInterval(
        code=_symbol(row.get("code"), field=f"{field}.code"),
        name=_text(row.get("name"), field=f"{field}.name"),
        start_inclusive=start,
        end_exclusive=end,
        is_active_now=_flag(row.get("is_active_now"), field=f"{field}.is_active_now"),
        is_delisted=_flag(row.get("is_delisted"), field=f"{field}.is_delisted"),
    )


def _source(value: Mapping[str, Any], *, index: int) -> MembershipCorrectionSource:
    field = f"sources[{index}]"
    try:
        kind = MembershipCorrectionSourceKind(_text(value.get("kind"), field=f"{field}.kind"))
    except ValueError as exc:
        message = f"{field}.kind is unsupported"
        raise MembershipCorrectionError(message) from exc
    url = _canonical_source_url(value.get("url"), kind=kind, field=f"{field}.url")
    document_date = _date(value.get("document_date"), field=f"{field}.document_date")
    assert document_date is not None
    return MembershipCorrectionSource(
        source_id=_text(value.get("source_id"), field=f"{field}.source_id"),
        kind=kind,
        url=url,
        document_date=document_date,
    )


def _canonical_source_url(
    value: object,
    *,
    kind: MembershipCorrectionSourceKind,
    field: str,
) -> str:
    text = _text(value, field=field)
    parsed = httpx.URL(text)
    host = (parsed.host or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or bool(parsed.username)
        or bool(parsed.password)
        or bool(parsed.fragment)
        or str(parsed) != text
    ):
        message = f"{field} must be one canonical credential-free HTTPS URL"
        raise MembershipCorrectionError(message)
    if (
        kind is MembershipCorrectionSourceKind.INDEX_PROVIDER_NOTICE
        and host != "press.spglobal.com"
    ):
        message = f"{field} index-provider notice must use press.spglobal.com"
        raise MembershipCorrectionError(message)
    if kind is MembershipCorrectionSourceKind.SEC_FILING and host != "www.sec.gov":
        message = f"{field} SEC filing must use www.sec.gov"
        raise MembershipCorrectionError(message)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        message = f"{field} must use a DNS host, not an IP literal"
        raise MembershipCorrectionError(message)
    if host == "localhost" or "." not in host:
        message = f"{field} must use a public fully-qualified host"
        raise MembershipCorrectionError(message)
    return text


def _edge_usage(
    value: object,
    *,
    action: MembershipCorrectionAction,
    field: str,
) -> MembershipIdentityEdgeUsage | None:
    if action is not MembershipCorrectionAction.IDENTITY_BRIDGE_ONLY:
        if value is not None:
            message = f"{field} is valid only for identity-only bridges"
            raise MembershipCorrectionError(message)
        return None
    try:
        return MembershipIdentityEdgeUsage(_text(value, field=field))
    except ValueError as exc:
        message = f"{field} is unsupported"
        raise MembershipCorrectionError(message) from exc


def _correction_source_ids(
    value: object,
    *,
    sources: Mapping[str, MembershipCorrectionSource],
    field: str,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        message = f"{field}.source_ids must be an array"
        raise MembershipCorrectionError(message)
    source_ids = tuple(_text(source_id, field=f"{field}.source_ids") for source_id in value)
    if (
        not source_ids
        or len(set(source_ids)) != len(source_ids)
        or any(source_id not in sources for source_id in source_ids)
    ):
        message = f"{field}.source_ids must be unique references to declared sources"
        raise MembershipCorrectionError(message)
    return source_ids


def _validate_correction_authority(
    *,
    action: MembershipCorrectionAction,
    bridge_kind: MembershipIdentityBridgeKind,
    expected: MembershipCorrectionInterval | None,
    replacements: tuple[MembershipCorrectionInterval, ...],
    source_ids: tuple[str, ...],
    sources: Mapping[str, MembershipCorrectionSource],
    field: str,
) -> None:
    referenced_source_kinds = {sources[source_id].kind for source_id in source_ids}
    if not referenced_source_kinds.intersection(
        {
            MembershipCorrectionSourceKind.SEC_FILING,
            MembershipCorrectionSourceKind.ISSUER_RELEASE,
        }
    ):
        message = f"{field} requires SEC or issuer primary identity evidence"
        raise MembershipCorrectionError(message)
    if action is MembershipCorrectionAction.REPLACE_EXACT_INTERVAL:
        if bridge_kind is not MembershipIdentityBridgeKind.INDEX_MEMBERSHIP_CORRECTION:
            message = f"{field} interval replacement requires index_membership_correction"
            raise MembershipCorrectionError(message)
        if expected is None:
            message = f"{field} interval replacement requires an exact raw-row precondition"
            raise MembershipCorrectionError(message)
        if not any(
            sources[source_id].kind is MembershipCorrectionSourceKind.INDEX_PROVIDER_NOTICE
            for source_id in source_ids
        ):
            message = f"{field} interval replacement requires an index-provider notice"
            raise MembershipCorrectionError(message)
    else:
        if bridge_kind is MembershipIdentityBridgeKind.INDEX_MEMBERSHIP_CORRECTION:
            message = f"{field} identity-only bridge cannot claim index membership authority"
            raise MembershipCorrectionError(message)
        if expected is None:
            message = f"{field} identity-only bridge requires an exact raw-row precondition"
            raise MembershipCorrectionError(message)
        if replacements:
            message = f"{field} identity-only bridge cannot replace membership coverage"
            raise MembershipCorrectionError(message)


def _validate_correction_validity(
    *,
    effective_date: date,
    valid_from: date,
    valid_to: date | None,
    replacements: tuple[MembershipCorrectionInterval, ...],
    field: str,
) -> None:
    if valid_to is not None and valid_to <= valid_from:
        message = f"{field} validity must be a non-empty half-open interval"
        raise MembershipCorrectionError(message)
    if effective_date < valid_from or (valid_to is not None and effective_date >= valid_to):
        message = f"{field}.effective_date must fall within its validity interval"
        raise MembershipCorrectionError(message)
    for replacement in replacements:
        if replacement.start_inclusive is None:
            message = f"{field} replacement interval requires an explicit inclusive start"
            raise MembershipCorrectionError(message)
        if replacement.start_inclusive < valid_from or (
            valid_to is not None
            and (replacement.end_exclusive is None or replacement.end_exclusive > valid_to)
        ):
            message = f"{field} replacement interval exceeds reviewed validity"
            raise MembershipCorrectionError(message)


def _validate_reviewed_listing_date(
    *,
    action: MembershipCorrectionAction,
    effective_date: date,
    reviewed_listing_date: date | None,
    new_identity: MembershipCorrectionIdentity,
    replacements: tuple[MembershipCorrectionInterval, ...],
    field: str,
) -> None:
    if reviewed_listing_date is None:
        return
    if action is not MembershipCorrectionAction.REPLACE_EXACT_INTERVAL:
        message = f"{field}.reviewed_listing_date requires an interval replacement"
        raise MembershipCorrectionError(message)
    if reviewed_listing_date != effective_date:
        message = f"{field}.reviewed_listing_date must equal effective_date"
        raise MembershipCorrectionError(message)
    if not any(
        replacement.code == new_identity.code
        and replacement.start_inclusive == reviewed_listing_date
        for replacement in replacements
    ):
        message = (
            f"{field}.reviewed_listing_date requires a replacement for the reviewed "
            "identity beginning on that date"
        )
        raise MembershipCorrectionError(message)


def _validate_identity_bridge(
    *,
    bridge_kind: MembershipIdentityBridgeKind,
    edge_usage: MembershipIdentityEdgeUsage,
    effective_date: date,
    expected: MembershipCorrectionInterval,
    old_identity: MembershipCorrectionIdentity,
    new_identity: MembershipCorrectionIdentity,
    field: str,
) -> None:
    assert old_identity.valid_from is not None
    assert new_identity.valid_from is not None
    if new_identity.valid_from != effective_date:
        message = f"{field}.new_identity.valid_from must equal effective_date"
        raise MembershipCorrectionError(message)
    if expected.code != new_identity.code:
        message = f"{field}.expected_ticker_interval.code must equal new_identity.code"
        raise MembershipCorrectionError(message)
    starts_after_boundary = (
        expected.start_inclusive is not None and expected.start_inclusive >= effective_date
    )
    ends_before_boundary = (
        expected.end_exclusive is not None and expected.end_exclusive <= effective_date
    )
    if starts_after_boundary or ends_before_boundary:
        message = f"{field} exact raw row must straddle effective_date"
        raise MembershipCorrectionError(message)
    if old_identity.valid_from >= effective_date or (
        old_identity.valid_to_exclusive is not None
        and old_identity.valid_to_exclusive < effective_date
    ):
        message = f"{field}.old_identity validity must reach the reviewed effective boundary"
        raise MembershipCorrectionError(message)
    if edge_usage is not MembershipIdentityEdgeUsage.PROVIDER_ALIAS_SAME_CLASS:
        return
    if bridge_kind is not MembershipIdentityBridgeKind.SAME_SECURITY_RENAME:
        message = f"{field} provider_alias_same_class requires same_security_rename"
        raise MembershipCorrectionError(message)
    if old_identity.valid_to_exclusive != effective_date:
        message = (
            f"{field}.old_identity.valid_to_exclusive must equal effective_date "
            "for a same-class provider alias"
        )
        raise MembershipCorrectionError(message)
    if old_identity.security_id != new_identity.security_id or old_identity.cik != new_identity.cik:
        message = f"{field} provider_alias_same_class requires identical class identity and CIK"
        raise MembershipCorrectionError(message)


def _correction(
    value: Mapping[str, Any],
    *,
    index: int,
    sources: Mapping[str, MembershipCorrectionSource],
) -> ReviewedMembershipCorrection:
    field = f"corrections[{index}]"
    try:
        action = MembershipCorrectionAction(_text(value.get("action"), field=f"{field}.action"))
        bridge_kind = MembershipIdentityBridgeKind(
            _text(value.get("bridge_kind"), field=f"{field}.bridge_kind")
        )
    except ValueError as exc:
        message = f"{field} action or bridge_kind is unsupported"
        raise MembershipCorrectionError(message) from exc
    edge_usage = _edge_usage(value.get("edge_usage"), action=action, field=f"{field}.edge_usage")
    expected_raw = value.get("expected_ticker_interval")
    expected = None if expected_raw is None else _interval(expected_raw, field=f"{field}.expected")
    replacements = tuple(
        _interval(row, field=f"{field}.replacements[{row_index}]")
        for row_index, row in enumerate(
            _rows(value.get("replacement_intervals"), field=f"{field}.replacements")
        )
    )
    source_ids = _correction_source_ids(value.get("source_ids"), sources=sources, field=field)
    _validate_correction_authority(
        action=action,
        bridge_kind=bridge_kind,
        expected=expected,
        replacements=replacements,
        source_ids=source_ids,
        sources=sources,
        field=field,
    )
    effective_date = _date(value.get("effective_date"), field=f"{field}.effective_date")
    valid_from = _date(value.get("valid_from"), field=f"{field}.valid_from")
    valid_to = _date(
        value.get("valid_to_exclusive"), field=f"{field}.valid_to_exclusive", optional=True
    )
    assert effective_date is not None
    assert valid_from is not None
    _validate_correction_validity(
        effective_date=effective_date,
        valid_from=valid_from,
        valid_to=valid_to,
        replacements=replacements,
        field=field,
    )
    require_endpoint = action is MembershipCorrectionAction.IDENTITY_BRIDGE_ONLY
    old_identity = _identity(
        value.get("old_identity"), field=f"{field}.old_identity", require_endpoint=require_endpoint
    )
    new_identity = _identity(
        value.get("new_identity"), field=f"{field}.new_identity", require_endpoint=require_endpoint
    )
    if action is MembershipCorrectionAction.REPLACE_EXACT_INTERVAL:
        _validate_replacement_identity_endpoints(
            replacements=replacements,
            old_identity=old_identity,
            new_identity=new_identity,
            field=field,
        )
    reviewed_listing_date = _date(
        value.get("reviewed_listing_date"),
        field=f"{field}.reviewed_listing_date",
        optional=True,
    )
    _validate_reviewed_listing_date(
        action=action,
        effective_date=effective_date,
        reviewed_listing_date=reviewed_listing_date,
        new_identity=new_identity,
        replacements=replacements,
        field=field,
    )
    if require_endpoint:
        assert edge_usage is not None
        assert expected is not None
        _validate_identity_bridge(
            bridge_kind=bridge_kind,
            edge_usage=edge_usage,
            effective_date=effective_date,
            expected=expected,
            old_identity=old_identity,
            new_identity=new_identity,
            field=field,
        )
    return ReviewedMembershipCorrection(
        correction_id=_text(value.get("correction_id"), field=f"{field}.correction_id"),
        action=action,
        bridge_kind=bridge_kind,
        edge_usage=edge_usage,
        effective_date=effective_date,
        reviewed_listing_date=reviewed_listing_date,
        valid_from=valid_from,
        valid_to_exclusive=valid_to,
        old_identity=old_identity,
        new_identity=new_identity,
        expected_ticker_interval=expected,
        replacement_intervals=replacements,
        source_ids=source_ids,
        rationale=_text(value.get("rationale"), field=f"{field}.rationale"),
    )


def _identity_endpoint_intervals_overlap(
    left: MembershipCorrectionIdentity,
    right: MembershipCorrectionIdentity,
) -> bool:
    assert left.valid_from is not None
    assert right.valid_from is not None
    left_end = left.valid_to_exclusive or date.max
    right_end = right.valid_to_exclusive or date.max
    return left.valid_from < right_end and right.valid_from < left_end


def _validate_identity_endpoint_collisions(
    corrections: tuple[ReviewedMembershipCorrection, ...],
) -> None:
    by_provider_symbol: dict[
        str,
        list[tuple[str, MembershipCorrectionIdentity]],
    ] = {}
    for correction in corrections:
        for identity in (correction.old_identity, correction.new_identity):
            if identity.provider_symbol is None:
                continue
            by_provider_symbol.setdefault(identity.provider_symbol, []).append(
                (correction.correction_id, identity)
            )
    for provider_symbol, assignments in by_provider_symbol.items():
        ordered = sorted(
            assignments,
            key=lambda value: (
                value[1].valid_from or date.min,
                value[1].valid_to_exclusive or date.max,
                value[0],
            ),
        )
        for index, (left_correction_id, left) in enumerate(ordered):
            for right_correction_id, right in ordered[index + 1 :]:
                if not _identity_endpoint_intervals_overlap(left, right):
                    continue
                if left.security_id == right.security_id:
                    continue
                message = (
                    "identity endpoint provider-symbol collision for "
                    f"{provider_symbol}: {left_correction_id}/{left.security_id} and "
                    f"{right_correction_id}/{right.security_id}"
                )
                raise MembershipCorrectionError(message)


def load_membership_correction_source_spec(path: Path) -> MembershipCorrectionSourceSpec:
    """Load one canonical, human-reviewed correction source specification."""

    try:
        content = path.read_bytes()
        decoded = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        message = f"membership correction source spec is unreadable: {path}"
        raise MembershipCorrectionError(message) from exc
    root = _mapping(decoded, field="membership correction source spec")
    if root.get("schema") != CORRECTION_SOURCE_SPEC_SCHEMA:
        message = "membership correction source spec schema is unsupported"
        raise MembershipCorrectionError(message)
    if canonical_json_bytes(root) + b"\n" != content:
        message = "membership correction source spec must use canonical JSON bytes"
        raise MembershipCorrectionError(message)
    review = _mapping(root.get("review"), field="review")
    try:
        reviewed_at = parse_utc_datetime(
            review.get("reviewed_at"), field="review.reviewed_at", strict_utc=True
        )
    except (TypeError, ValueError) as exc:
        raise MembershipCorrectionError(str(exc)) from exc
    source_values = _rows(root.get("sources"), field="sources")
    sources = tuple(_source(value, index=index) for index, value in enumerate(source_values))
    sources_by_id = {source.source_id: source for source in sources}
    if not sources or len(sources_by_id) != len(sources):
        message = "membership correction sources must have unique source_id values"
        raise MembershipCorrectionError(message)
    correction_values = _rows(root.get("corrections"), field="corrections")
    corrections = tuple(
        _correction(value, index=index, sources=sources_by_id)
        for index, value in enumerate(correction_values)
    )
    if not corrections or len({item.correction_id for item in corrections}) != len(corrections):
        message = "membership corrections must have unique correction_id values"
        raise MembershipCorrectionError(message)
    _validate_identity_endpoint_collisions(corrections)
    return MembershipCorrectionSourceSpec(
        reviewed_by=_text(review.get("reviewed_by"), field="review.reviewed_by"),
        reviewed_at=reviewed_at,
        sources=sources,
        corrections=corrections,
        content=content,
        content_sha256=evidence_sha256(root),
    )


def _source_suffix(content_type: str) -> str:
    if content_type == "application/pdf":
        return ".pdf"
    if content_type in {"text/html", "application/xhtml+xml"}:
        return ".html"
    if content_type == "application/json":
        return ".json"
    return ".bin"


def _fetch_source(
    client: httpx.Client,
    source: MembershipCorrectionSource,
    *,
    config: MembershipCorrectionAcquisitionConfig,
    clock: Callable[[], datetime],
) -> tuple[bytes, Mapping[str, object]]:
    def request_once() -> tuple[bytes, Mapping[str, object]]:
        try:
            response = client.get(
                source.url,
                headers={
                    "Accept": (
                        "application/pdf,text/html,application/xhtml+xml,"
                        "application/json,text/plain"
                    ),
                    "Accept-Encoding": "identity",
                    "User-Agent": config.user_agent,
                },
                timeout=config.timeout_seconds,
            )
        except httpx.RequestError as exc:
            message = f"membership correction source transport failure for {source.source_id}"
            raise _RetryableMembershipCorrectionError(message) from exc
        if is_retryable_status(response.status_code):
            message = (
                f"membership correction source {source.source_id} returned retryable HTTP "
                f"{response.status_code}"
            )
            raise _RetryableMembershipCorrectionError(message)
        if response.is_redirect:
            message = (
                f"membership correction source {source.source_id} returned redirect HTTP "
                f"{response.status_code}"
            )
            raise MembershipCorrectionError(message)
        if response.is_error:
            message = (
                f"membership correction source {source.source_id} returned HTTP "
                f"{response.status_code}"
            )
            raise MembershipCorrectionError(message)
        if response.url != httpx.URL(source.url):
            message = f"membership correction source {source.source_id} redirected"
            raise MembershipCorrectionError(message)
        content = response.content
        content_encoding = response.headers.get("Content-Encoding", "identity").lower()
        if content_encoding not in {"", "identity"}:
            message = f"membership correction source {source.source_id} ignored identity encoding"
            raise MembershipCorrectionError(message)
        if not content or len(content) > _MAX_SOURCE_BYTES:
            message = f"membership correction source {source.source_id} has invalid byte length"
            raise MembershipCorrectionError(message)
        declared_length = response.headers.get("Content-Length")
        if declared_length is not None:
            try:
                parsed_length = int(declared_length)
            except ValueError as exc:
                message = (
                    f"membership correction source {source.source_id} has invalid Content-Length"
                )
                raise MembershipCorrectionError(message) from exc
            if parsed_length != len(content):
                message = f"membership correction source {source.source_id} Content-Length differs"
                raise MembershipCorrectionError(message)
        content_type = response.headers.get("Content-Type", "").partition(";")[0].lower()
        if content_type not in {
            "application/json",
            "application/pdf",
            "application/xhtml+xml",
            "text/html",
            "text/plain",
        }:
            message = f"membership correction source {source.source_id} content type is unsupported"
            raise MembershipCorrectionError(message)
        retrieved_at = parse_utc_datetime(clock(), field="membership correction retrieval clock")
        return content, {
            "content_type": content_type,
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "retrieved_at": retrieved_at.isoformat(),
        }

    return retry_sync(
        request_once,
        retries=config.retries,
        base_delay=config.retry_base_delay_seconds,
        max_delay=config.retry_max_delay_seconds,
        jitter=0.0,
        retry_on=(_RetryableMembershipCorrectionError,),
        operation_name=f"membership correction source GET {source.source_id}",
        log=logger,
    )


def acquire_membership_correction_evidence(
    output_root: Path,
    *,
    source_spec_path: Path,
    config: MembershipCorrectionAcquisitionConfig | None = None,
    client: httpx.Client | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FrozenMembershipCorrectionEvidence:
    """Freeze exact primary-source bytes and a content-addressed evidence manifest."""

    spec = load_membership_correction_source_spec(source_spec_path)
    resolved_config = config or MembershipCorrectionAcquisitionConfig()
    spec_artifact = store_content_object(
        output_root,
        spec.content,
        suffix=".json",
        role=_SPEC_ROLE,
        media_type="application/json",
        row_count=len(spec.corrections),
        context={
            "reviewed_at": spec.reviewed_at.isoformat(),
            "reviewed_by": spec.reviewed_by,
            "schema": CORRECTION_SOURCE_SPEC_SCHEMA,
        },
    )
    owns_client = client is None
    http_client = client or httpx.Client(follow_redirects=False)
    source_artifacts: list[ContentAddressedArtifact] = []
    try:
        for source in spec.sources:
            content, response_context = _fetch_source(
                http_client,
                source,
                config=resolved_config,
                clock=clock,
            )
            content_type = str(response_context["content_type"])
            source_artifacts.append(
                store_content_object(
                    output_root,
                    content,
                    suffix=_source_suffix(content_type),
                    role=_SOURCE_ROLE,
                    media_type=content_type,
                    row_count=None,
                    context={
                        **response_context,
                        "document_date": source.document_date.isoformat(),
                        "source_id": source.source_id,
                        "source_kind": source.kind.value,
                        "url": source.url,
                    },
                )
            )
    finally:
        if owns_client:
            http_client.close()
    source_evidence_artifacts = (spec_artifact, *source_artifacts)
    payload = {
        "artifacts": [artifact.as_manifest() for artifact in source_evidence_artifacts],
        "correction_set_sha256": evidence_sha256(
            [correction.correction_id for correction in spec.corrections]
        ),
        "reviewed_at": spec.reviewed_at.isoformat(),
        "reviewed_by": spec.reviewed_by,
        "schema": CORRECTION_EVIDENCE_MANIFEST_SCHEMA,
        "source_spec_sha256": spec_artifact.sha256,
    }
    manifest_path, manifest_sha256 = write_content_addressed_manifest(output_root, payload)
    manifest_artifact = store_content_object(
        output_root,
        manifest_path.read_bytes(),
        suffix=".json",
        role=_MANIFEST_ROLE,
        media_type="application/json",
        row_count=len(spec.corrections),
        context={
            "manifest_sha256": manifest_sha256,
            "schema": CORRECTION_EVIDENCE_MANIFEST_SCHEMA,
        },
    )
    return FrozenMembershipCorrectionEvidence(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        spec=spec,
        artifacts=(manifest_artifact, *source_evidence_artifacts),
    )


def load_frozen_membership_correction_evidence(
    output_root: Path,
    manifest_path: Path,
) -> FrozenMembershipCorrectionEvidence:
    """Verify a frozen correction manifest and every exact primary-source byte."""

    try:
        manifest = load_content_addressed_manifest(
            output_root,
            manifest_path,
            schema=CORRECTION_EVIDENCE_MANIFEST_SCHEMA,
        )
        artifacts = content_manifest_artifacts(manifest)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise MembershipCorrectionError(str(exc)) from exc
    specs = tuple(artifact for artifact in artifacts if artifact.role == _SPEC_ROLE)
    sources = tuple(artifact for artifact in artifacts if artifact.role == _SOURCE_ROLE)
    if len(specs) != 1 or not sources or len(artifacts) != len(sources) + 1:
        message = "frozen membership correction evidence has invalid artifact roles"
        raise MembershipCorrectionError(message)
    try:
        spec_path = verified_content_path(output_root, specs[0])
    except ValueError as exc:
        raise MembershipCorrectionError(str(exc)) from exc
    spec = load_membership_correction_source_spec(spec_path)
    if manifest.get("source_spec_sha256") != specs[0].sha256:
        message = "frozen membership correction source-spec identity differs"
        raise MembershipCorrectionError(message)
    if (
        manifest.get("reviewed_by") != spec.reviewed_by
        or manifest.get("reviewed_at") != spec.reviewed_at.isoformat()
    ):
        message = "frozen membership correction review identity differs"
        raise MembershipCorrectionError(message)
    artifacts_by_source_id: dict[str, ContentAddressedArtifact] = {}
    for artifact in sources:
        source_id = artifact.context.get("source_id")
        if not isinstance(source_id, str) or source_id in artifacts_by_source_id:
            message = "frozen membership correction source artifact identity is invalid"
            raise MembershipCorrectionError(message)
        artifacts_by_source_id[source_id] = artifact
    if set(artifacts_by_source_id) != {source.source_id for source in spec.sources}:
        message = "frozen membership correction source artifact set differs from its spec"
        raise MembershipCorrectionError(message)
    for source in spec.sources:
        artifact = artifacts_by_source_id[source.source_id]
        if (
            artifact.context.get("url") != source.url
            or artifact.context.get("document_date") != source.document_date.isoformat()
            or artifact.context.get("source_kind") != source.kind.value
        ):
            message = f"frozen membership correction source context differs for {source.source_id}"
            raise MembershipCorrectionError(message)
    expected_correction_set = evidence_sha256(
        [correction.correction_id for correction in spec.corrections]
    )
    if manifest.get("correction_set_sha256") != expected_correction_set:
        message = "frozen membership correction set identity differs"
        raise MembershipCorrectionError(message)
    manifest_content_sha256 = file_sha256(manifest_path)
    manifest_artifact = ContentAddressedArtifact(
        role=_MANIFEST_ROLE,
        path=(f"objects/{manifest_content_sha256[:2]}/{manifest_content_sha256}.json"),
        sha256=manifest_content_sha256,
        media_type="application/json",
        row_count=len(spec.corrections),
        context={
            "manifest_sha256": manifest_path.stem,
            "schema": CORRECTION_EVIDENCE_MANIFEST_SCHEMA,
        },
    )
    try:
        verified_content_path(output_root, manifest_artifact)
    except ValueError as exc:
        message = "frozen membership correction manifest content object is missing or altered"
        raise MembershipCorrectionError(message) from exc
    return FrozenMembershipCorrectionEvidence(
        manifest_path=manifest_path,
        manifest_sha256=manifest_path.stem,
        spec=spec,
        artifacts=(manifest_artifact, *artifacts),
    )


__all__ = [
    "CORRECTION_EVIDENCE_MANIFEST_SCHEMA",
    "CORRECTION_SOURCE_SPEC_SCHEMA",
    "FrozenMembershipCorrectionEvidence",
    "MembershipCorrectionAcquisitionConfig",
    "MembershipCorrectionAction",
    "MembershipCorrectionError",
    "MembershipCorrectionIdentity",
    "MembershipCorrectionInterval",
    "MembershipCorrectionSource",
    "MembershipCorrectionSourceKind",
    "MembershipIdentityBridgeKind",
    "MembershipIdentityEdgeUsage",
    "ReviewedMembershipCorrection",
    "acquire_membership_correction_evidence",
    "load_frozen_membership_correction_evidence",
    "load_membership_correction_source_spec",
]
