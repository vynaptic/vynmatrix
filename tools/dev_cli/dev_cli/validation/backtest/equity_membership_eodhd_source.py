"""Licensed EODHD membership-source parsing and security identity resolution.

This internal module owns deterministic interpretation of immutable provider
responses and resolved-identity lifetime validation. Reviewed authority
corrections, materialization, and the public facade remain in
``equity_membership_eodhd``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any

from dev_cli.validation.backtest.equity_membership_corrections import (
    MembershipCorrectionIdentity,
    MembershipIdentityEdgeUsage,
)
from dev_cli.validation.evidence import (
    ContentAddressedArtifact,
    evidence_sha256,
    nonblank_string,
    store_content_object,
)
from dev_cli.validation.providers.eodhd import EODHDJsonEvidence

_CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
_GENERAL_FUNDAMENTALS_ARTIFACT_ROLE = "eodhd_security_fundamentals_general_response"
_JSON_MEDIA_TYPE = "application/json"
_PROVIDER = "eodhd"
_SOURCE_KIND = "licensed_point_in_time"
_POLICY_VERSION = "sp500-eodhd-pit-membership-identity-v8"
_ID_MAPPING_PAGE_LIMIT = 100
_IDENTITY_PARTITION_ENDPOINT_COUNT = 2
_ISIN_LENGTH = 12
_MAX_CIK_DIGITS = 10
_SYMBOL_CHANGE_HISTORY_START = date(2022, 7, 22)
_TICKER_COMPONENTS_DOCUMENTATION = (
    "https://eodhd.com/financial-apis/stock-etfs-fundamental-data-feeds"
)


class EODHDMembershipMaterializationError(RuntimeError):
    """Licensed membership evidence is malformed, contradictory, or incomplete."""


class _MembershipCrosscheckDisposition(StrEnum):
    """Typed presence disagreement between the two licensed membership views."""

    SNAPSHOT_ONLY_CHECKPOINT_LAG = "snapshot_checkpoint_lag_after_ticker_end"
    SNAPSHOT_ONLY_REVIEWED_SUCCESSOR_BACKCAST = (
        "snapshot_successor_code_backcast_over_reviewed_identity_partition"
    )
    SNAPSHOT_ONLY_UNRESOLVED = "snapshot_only_no_authoritative_ticker_interval"
    CODE_NAME_REVIEWED_IDENTITY_OVERRIDE = "snapshot_name_overridden_by_reviewed_identity"
    CODE_NAME_UNRESOLVED = "matching_code_snapshot_ticker_name_disagreement"
    TICKER_ONLY = "authoritative_ticker_interval_absent_from_snapshot_checkpoint"


@dataclass(frozen=True, slots=True)
class _Component:
    code: str
    name: str
    exchange: str | None


@dataclass(frozen=True, slots=True)
class _ComponentSnapshot:
    effective_date: date
    components: Mapping[str, _Component]


@dataclass(frozen=True, slots=True)
class _TickerHistory:
    code: str
    name: str | None
    start: date | None
    end: date | None
    is_active_now: bool
    is_delisted: bool
    authority_correction_id: str | None = None
    reviewed_listing_date: date | None = None
    reviewed_identity: MembershipCorrectionIdentity | None = None
    reviewed_edge_usage: MembershipIdentityEdgeUsage | None = None


@dataclass(frozen=True, slots=True)
class _MembershipCrosscheckDiscrepancy:
    effective_date: date
    code: str
    disposition: _MembershipCrosscheckDisposition
    snapshot_name: str | None
    ticker_names: tuple[str, ...]
    ticker_starts: tuple[date | None, ...]
    ticker_ends_exclusive: tuple[date | None, ...]


@dataclass(frozen=True, slots=True)
class _IntervalDraft:
    code: str
    names: tuple[str, ...]
    effective_from: date
    effective_to: date
    authority_correction_id: str | None = None
    reviewed_listing_date: date | None = None
    reviewed_identity: MembershipCorrectionIdentity | None = None
    reviewed_edge_usage: MembershipIdentityEdgeUsage | None = None


@dataclass(frozen=True, slots=True)
class _DirectoryEntry:
    code: str
    name: str | None
    exchange: str | None
    isin: str | None
    delisted: bool


@dataclass(frozen=True, slots=True)
class _SymbolChange:
    old_symbol: str
    new_symbol: str
    company_name: str
    effective: date


@dataclass(frozen=True, slots=True)
class _IdentifierMapping:
    symbol: str
    isin: str | None
    figi: str | None
    lei: str | None
    cusip: str | None
    cik: str | None


@dataclass(frozen=True, slots=True)
class _GeneralFundamentals:
    code: str | None
    name: str | None
    ipo_date: date | None
    is_delisted: bool | None
    updated_at: date | None
    isin: str | None
    cik: str | None


@dataclass(frozen=True, slots=True)
class _IdentityResolution:
    code: str
    provider_symbol: str
    security_id: str
    security_id_source: str
    security_id_permanent: bool
    directory_disposition: str
    directory_entry: _DirectoryEntry | None
    mapping: tuple[_IdentifierMapping, ...]
    mapping_artifact: ContentAddressedArtifact
    general: _GeneralFundamentals
    general_artifact: ContentAddressedArtifact
    general_identity_binding: str
    source_ref: str
    resolution_id: str


def _validate_issuer_lifetimes(
    intervals: Sequence[_IntervalDraft],
    resolutions: Mapping[str, _IdentityResolution],
    *,
    resolution_key: Callable[[_IntervalDraft], str],
) -> None:
    conflicts: list[str] = []
    for interval in intervals:
        resolution = resolutions[resolution_key(interval)]
        if interval.reviewed_identity is not None:
            continue
        ipo_date = resolution.general.ipo_date
        reviewed_listing_date = interval.reviewed_listing_date
        if reviewed_listing_date is not None:
            if interval.effective_from < reviewed_listing_date:
                message = (
                    "reviewed primary listing date follows the corrected membership "
                    f"start for {interval.code}"
                )
                conflicts.append(message)
            continue
        if ipo_date is None or interval.effective_from >= ipo_date:
            continue
        conflicts.append(
            "EODHD membership interval begins before General.IPODate for resolved "
            f"legal security: component={interval.code};"
            f"provider_symbol={resolution.provider_symbol};"
            f"security_id={resolution.security_id};"
            f"effective_from={interval.effective_from.isoformat()};"
            f"effective_to={interval.effective_to.isoformat()};"
            f"ipo_date={ipo_date.isoformat()};"
            f"evidence={resolution.general_artifact.context['endpoint']}"
            f"#sha256={resolution.general_artifact.sha256};"
            f"retrieved_at={resolution.general_artifact.context['retrieved_at']};"
            f"policy_version={_POLICY_VERSION}"
        )
    if conflicts:
        message = "\n".join(conflicts)
        raise EODHDMembershipMaterializationError(message)


def _required_text(value: object, *, field: str) -> str:
    try:
        return nonblank_string(value, field=field)
    except (TypeError, ValueError) as exc:
        raise EODHDMembershipMaterializationError(str(exc)) from exc


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        message = f"{field} must be a string or null"
        raise EODHDMembershipMaterializationError(message)
    return value.strip()


def _symbol(value: object, *, field: str) -> str:
    result = _required_text(value, field=field).upper()
    if any(character.isspace() for character in result):
        message = f"{field} must contain no whitespace"
        raise EODHDMembershipMaterializationError(message)
    return result.removesuffix(".US")


def _optional_date(value: object, *, field: str) -> date | None:
    text = _optional_text(value, field=field)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        message = f"{field} must be an ISO-8601 date or null"
        raise EODHDMembershipMaterializationError(message) from exc


def _flag(value: object, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value.strip() in {"0", "1"}:
        return value.strip() == "1"
    message = f"{field} must be a boolean or zero/one flag"
    raise EODHDMembershipMaterializationError(message)


def _optional_flag(value: object, *, field: str) -> bool | None:
    if value is None:
        return None
    return _flag(value, field=field)


def _optional_symbol(value: object, *, field: str) -> str | None:
    text = _optional_text(value, field=field)
    if text is None:
        return None
    return _symbol(text, field=field)


def _optional_isin(value: object, *, field: str) -> str | None:
    text = _optional_text(value, field=field)
    if text is None:
        return None
    normalized = text.upper()
    if (
        len(normalized) != _ISIN_LENGTH
        or not normalized.isascii()
        or not normalized.isalnum()
        or not normalized[:2].isalpha()
        or not normalized[-1].isdigit()
    ):
        message = f"{field} must be a 12-character ISIN or null"
        raise EODHDMembershipMaterializationError(message)
    return normalized


def _optional_cik(value: object, *, field: str) -> str | None:
    text = _optional_text(value, field=field)
    if text is None:
        return None
    if not text.isdigit() or len(text) > _MAX_CIK_DIGITS:
        message = f"{field} must contain at most {_MAX_CIK_DIGITS} digits or null"
        raise EODHDMembershipMaterializationError(message)
    return text.zfill(_MAX_CIK_DIGITS)


def _object_rows(value: object, *, field: str) -> tuple[Mapping[str, Any], ...]:
    raw_rows: Sequence[object]
    if isinstance(value, Mapping):
        raw_rows = tuple(value.values())
    elif isinstance(value, list):
        raw_rows = value
    else:
        message = f"{field} must be an object or array"
        raise EODHDMembershipMaterializationError(message)
    rows: list[Mapping[str, Any]] = []
    for row_number, raw_row in enumerate(raw_rows, start=1):
        if not isinstance(raw_row, Mapping):
            message = f"{field} row {row_number} must be an object"
            raise EODHDMembershipMaterializationError(message)
        rows.append(raw_row)
    return tuple(rows)


def _root_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        message = f"{field} root must be an object"
        raise EODHDMembershipMaterializationError(message)
    return value


def _parse_snapshots(
    evidence: EODHDJsonEvidence,
    *,
    evidence_start: date,
    requested_start: date,
    requested_end: date,
) -> tuple[_ComponentSnapshot, ...]:
    root = _root_mapping(evidence.payload, field="EODHD historical fundamentals")
    raw_snapshots = root.get("HistoricalComponents")
    if not isinstance(raw_snapshots, Mapping) or not raw_snapshots:
        message = "EODHD HistoricalComponents must be a non-empty dated object"
        raise EODHDMembershipMaterializationError(message)
    snapshots: list[_ComponentSnapshot] = []
    for raw_effective_date, raw_components in raw_snapshots.items():
        if not isinstance(raw_effective_date, str):
            message = "EODHD HistoricalComponents keys must be ISO-8601 dates"
            raise EODHDMembershipMaterializationError(message)
        try:
            effective_date = date.fromisoformat(raw_effective_date)
        except ValueError as exc:
            message = "EODHD HistoricalComponents keys must be ISO-8601 dates"
            raise EODHDMembershipMaterializationError(message) from exc
        if not evidence_start <= effective_date <= requested_end:
            message = (
                "EODHD HistoricalComponents returned a change event outside the "
                "requested evidence window"
            )
            raise EODHDMembershipMaterializationError(message)
        components: dict[str, _Component] = {}
        for row_number, raw_row in enumerate(
            _object_rows(
                raw_components,
                field=f"EODHD HistoricalComponents[{raw_effective_date}]",
            ),
            start=1,
        ):
            observed_date = _optional_date(
                raw_row.get("Date"),
                field=(f"EODHD HistoricalComponents[{raw_effective_date}] row {row_number} Date"),
            )
            if observed_date != effective_date:
                message = (
                    "EODHD HistoricalComponents embedded Date differs from its "
                    f"snapshot key on {raw_effective_date}"
                )
                raise EODHDMembershipMaterializationError(message)
            code = _symbol(
                raw_row.get("Code"),
                field=(f"EODHD HistoricalComponents[{raw_effective_date}] row {row_number} Code"),
            )
            if code in components:
                message = f"EODHD HistoricalComponents duplicates {code} on {effective_date}"
                raise EODHDMembershipMaterializationError(message)
            components[code] = _Component(
                code=code,
                name=_required_text(
                    raw_row.get("Name"),
                    field=(
                        f"EODHD HistoricalComponents[{raw_effective_date}] row {row_number} Name"
                    ),
                ),
                exchange=_optional_text(
                    raw_row.get("Exchange"),
                    field=(
                        f"EODHD HistoricalComponents[{raw_effective_date}] "
                        f"row {row_number} Exchange"
                    ),
                ),
            )
        if not components:
            message = f"EODHD HistoricalComponents snapshot {effective_date} is empty"
            raise EODHDMembershipMaterializationError(message)
        snapshots.append(
            _ComponentSnapshot(
                effective_date=effective_date,
                components=components,
            )
        )
    ordered = tuple(sorted(snapshots, key=lambda item: item.effective_date))
    if ordered[0].effective_date > requested_start:
        message = (
            "EODHD HistoricalComponents has no baseline snapshot on or before "
            f"{requested_start.isoformat()}"
        )
        raise EODHDMembershipMaterializationError(message)
    return ordered


def _parse_ticker_history(evidence: EODHDJsonEvidence) -> tuple[_TickerHistory, ...]:
    payload = evidence.payload
    if isinstance(payload, Mapping) and "HistoricalTickerComponents" in payload:
        payload = payload["HistoricalTickerComponents"]
    histories: list[_TickerHistory] = []
    for row_number, raw_row in enumerate(
        _object_rows(payload, field="EODHD HistoricalTickerComponents"),
        start=1,
    ):
        histories.append(
            _TickerHistory(
                code=_symbol(
                    raw_row.get("Code"),
                    field=f"EODHD HistoricalTickerComponents row {row_number} Code",
                ),
                name=_optional_text(
                    raw_row.get("Name"),
                    field=f"EODHD HistoricalTickerComponents row {row_number} Name",
                ),
                start=_optional_date(
                    raw_row.get("StartDate"),
                    field=f"EODHD HistoricalTickerComponents row {row_number} StartDate",
                ),
                end=_optional_date(
                    raw_row.get("EndDate"),
                    field=f"EODHD HistoricalTickerComponents row {row_number} EndDate",
                ),
                is_active_now=_flag(
                    raw_row.get("IsActiveNow"),
                    field=f"EODHD HistoricalTickerComponents row {row_number} IsActiveNow",
                ),
                is_delisted=_flag(
                    raw_row.get("IsDelisted"),
                    field=f"EODHD HistoricalTickerComponents row {row_number} IsDelisted",
                ),
            )
        )
        history = histories[-1]
        if history.start is not None and history.end is not None and history.end <= history.start:
            message = (
                f"EODHD ticker-history row {row_number} has an empty or negative half-open interval"
            )
            raise EODHDMembershipMaterializationError(message)
    if not histories:
        message = "EODHD HistoricalTickerComponents is empty"
        raise EODHDMembershipMaterializationError(message)
    return tuple(histories)


def _ticker_history_covers(history: _TickerHistory, observed_on: date) -> bool:
    """Return whether a half-open authoritative interval covers ``observed_on``."""

    return (history.start is None or history.start <= observed_on) and (
        history.end is None or observed_on < history.end
    )


def _validate_ticker_history_intervals(histories: Sequence[_TickerHistory]) -> None:
    by_code: dict[str, list[_TickerHistory]] = defaultdict(list)
    for history in histories:
        by_code[history.code].append(history)
    for code, values in by_code.items():
        ordered = sorted(values, key=lambda item: (item.start is not None, item.start or date.min))
        for previous, current in pairwise(ordered):
            if current.start is None or previous.end is None or current.start < previous.end:
                message = f"EODHD ticker history has conflicting authoritative intervals for {code}"
                raise EODHDMembershipMaterializationError(message)


def _is_reviewed_successor_backcast(
    histories: Sequence[_TickerHistory],
    *,
    snapshot_code: str,
    snapshot_name: str,
    observed_on: date,
) -> bool:
    successors = tuple(
        history
        for history in histories
        if history.code == snapshot_code
        and history.start is not None
        and observed_on < history.start
        and history.authority_correction_id is not None
        and history.reviewed_identity is not None
        and _normalized_name(history.name) == _normalized_name(snapshot_name)
    )
    if len(successors) != 1:
        return False
    successor = successors[0]
    correction_group = tuple(
        history
        for history in histories
        if history.authority_correction_id == successor.authority_correction_id
    )
    if len(correction_group) != _IDENTITY_PARTITION_ENDPOINT_COUNT:
        return False
    predecessors = tuple(
        history
        for history in correction_group
        if history.code != successor.code
        and history.reviewed_identity is not None
        and _ticker_history_covers(history, observed_on)
    )
    if len(predecessors) != 1:
        return False
    predecessor = predecessors[0]
    return (
        predecessor.end == successor.start
        and predecessor.reviewed_edge_usage == successor.reviewed_edge_usage
    )


def _is_reviewed_name_override(
    histories: Sequence[_TickerHistory],
    *,
    code: str,
    observed_on: date,
) -> bool:
    covering = tuple(
        history
        for history in histories
        if history.code == code and _ticker_history_covers(history, observed_on)
    )
    if len(covering) != 1:
        return False
    history = covering[0]
    if history.authority_correction_id is None or history.reviewed_identity is None:
        return False
    correction_group = tuple(
        candidate
        for candidate in histories
        if candidate.authority_correction_id == history.authority_correction_id
    )
    return (
        len(correction_group) == 1
        and history.reviewed_identity.code == history.code
        and history.reviewed_identity.name == history.name
    )


def _crosscheck_snapshots(
    snapshots: Sequence[_ComponentSnapshot],
    histories: Sequence[_TickerHistory],
) -> tuple[_MembershipCrosscheckDiscrepancy, ...]:
    """Preserve every code/date state disagreement without changing authority."""

    by_code: dict[str, list[_TickerHistory]] = defaultdict(list)
    for history in histories:
        by_code[history.code].append(history)
    discrepancies: list[_MembershipCrosscheckDiscrepancy] = []
    for snapshot_index, snapshot in enumerate(snapshots):
        ticker_codes = {
            history.code
            for history in histories
            if _ticker_history_covers(history, snapshot.effective_date)
        }
        snapshot_codes = set(snapshot.components)
        for code in sorted(snapshot_codes - ticker_codes):
            candidates = tuple(by_code.get(code, ()))
            reviewed_successor_backcast = _is_reviewed_successor_backcast(
                histories,
                snapshot_code=code,
                snapshot_name=snapshot.components[code].name,
                observed_on=snapshot.effective_date,
            )
            next_snapshot = (
                snapshots[snapshot_index + 1] if snapshot_index + 1 < len(snapshots) else None
            )
            ended_candidates = tuple(
                history
                for history in candidates
                if history.end is not None and history.end <= snapshot.effective_date
            )
            checkpoint_lag = (
                bool(ended_candidates)
                and any(
                    _normalized_name(history.name)
                    == _normalized_name(snapshot.components[code].name)
                    for history in ended_candidates
                )
                and next_snapshot is not None
                and code not in next_snapshot.components
                and all(
                    later.effective_date <= snapshot.effective_date or code not in later.components
                    for later in snapshots
                )
            )
            discrepancies.append(
                _MembershipCrosscheckDiscrepancy(
                    effective_date=snapshot.effective_date,
                    code=code,
                    disposition=(
                        _MembershipCrosscheckDisposition.SNAPSHOT_ONLY_REVIEWED_SUCCESSOR_BACKCAST
                        if reviewed_successor_backcast
                        else (
                            _MembershipCrosscheckDisposition.SNAPSHOT_ONLY_CHECKPOINT_LAG
                            if checkpoint_lag
                            else _MembershipCrosscheckDisposition.SNAPSHOT_ONLY_UNRESOLVED
                        )
                    ),
                    snapshot_name=snapshot.components[code].name,
                    ticker_names=tuple(
                        sorted({name for row in candidates if (name := row.name) is not None})
                    ),
                    ticker_starts=tuple(sorted({row.start for row in candidates}, key=str)),
                    ticker_ends_exclusive=tuple(sorted({row.end for row in candidates}, key=str)),
                )
            )
        for code in sorted(ticker_codes - snapshot_codes):
            covering = tuple(
                history
                for history in by_code[code]
                if _ticker_history_covers(history, snapshot.effective_date)
            )
            discrepancies.append(
                _MembershipCrosscheckDiscrepancy(
                    effective_date=snapshot.effective_date,
                    code=code,
                    disposition=_MembershipCrosscheckDisposition.TICKER_ONLY,
                    snapshot_name=None,
                    ticker_names=tuple(
                        sorted({name for row in covering if (name := row.name) is not None})
                    ),
                    ticker_starts=tuple(sorted({row.start for row in covering}, key=str)),
                    ticker_ends_exclusive=tuple(sorted({row.end for row in covering}, key=str)),
                )
            )
        for code in sorted(ticker_codes & snapshot_codes):
            covering = tuple(
                history
                for history in by_code[code]
                if _ticker_history_covers(history, snapshot.effective_date)
            )
            ticker_names = tuple(
                sorted({name for row in covering if (name := row.name) is not None})
            )
            if _normalized_name(snapshot.components[code].name) in {
                _normalized_name(name) for name in ticker_names
            }:
                continue
            reviewed_name_override = _is_reviewed_name_override(
                histories,
                code=code,
                observed_on=snapshot.effective_date,
            )
            discrepancies.append(
                _MembershipCrosscheckDiscrepancy(
                    effective_date=snapshot.effective_date,
                    code=code,
                    disposition=(
                        _MembershipCrosscheckDisposition.CODE_NAME_REVIEWED_IDENTITY_OVERRIDE
                        if reviewed_name_override
                        else _MembershipCrosscheckDisposition.CODE_NAME_UNRESOLVED
                    ),
                    snapshot_name=snapshot.components[code].name,
                    ticker_names=ticker_names,
                    ticker_starts=tuple(sorted({row.start for row in covering}, key=str)),
                    ticker_ends_exclusive=tuple(sorted({row.end for row in covering}, key=str)),
                )
            )
    return tuple(discrepancies)


def _build_intervals(
    histories: Sequence[_TickerHistory],
    snapshots: Sequence[_ComponentSnapshot],
    *,
    requested_start: date,
    requested_end: date,
) -> tuple[_IntervalDraft, ...]:
    """Build clipped inclusive output ranges from authoritative half-open rows."""

    earliest_snapshot = snapshots[0]
    intervals: list[_IntervalDraft] = []
    for row_number, history in enumerate(histories, start=1):
        authoritative_start = history.start
        effective_to = (
            requested_end
            if history.end is None
            else min(history.end - timedelta(days=1), requested_end)
        )
        if effective_to < requested_start:
            continue
        if authoritative_start is None:
            baseline = earliest_snapshot.components.get(history.code)
            if baseline is None:
                message = (
                    "EODHD ticker-history row has null StartDate without exact-code "
                    "earliest full-state baseline proof: "
                    f"row={row_number};code={history.code};"
                    f"baseline={earliest_snapshot.effective_date.isoformat()}"
                )
                raise EODHDMembershipMaterializationError(message)
            if history.name is None or _normalized_name(history.name) != _normalized_name(
                baseline.name
            ):
                message = (
                    "EODHD ticker-history null StartDate baseline identity differs: "
                    f"row={row_number};code={history.code};"
                    f"ticker_name={history.name!r};snapshot_name={baseline.name!r}"
                )
                raise EODHDMembershipMaterializationError(message)
            authoritative_start = earliest_snapshot.effective_date
        if authoritative_start > requested_end:
            continue
        if history.name is None:
            message = (
                "EODHD authoritative ticker-history interval requires Name: "
                f"row={row_number};code={history.code}"
            )
            raise EODHDMembershipMaterializationError(message)
        overlap_start = max(authoritative_start, requested_start)
        if overlap_start <= effective_to:
            intervals.append(
                _IntervalDraft(
                    code=history.code,
                    names=(history.name,),
                    effective_from=overlap_start,
                    effective_to=effective_to,
                    authority_correction_id=history.authority_correction_id,
                    reviewed_listing_date=history.reviewed_listing_date,
                    reviewed_identity=history.reviewed_identity,
                    reviewed_edge_usage=history.reviewed_edge_usage,
                )
            )
    if not intervals:
        message = "EODHD ticker history produces no membership intervals in the requested period"
        raise EODHDMembershipMaterializationError(message)
    ordered = tuple(
        sorted(intervals, key=lambda item: (item.code, item.effective_from, item.effective_to))
    )
    by_code: dict[str, list[_IntervalDraft]] = defaultdict(list)
    for interval in ordered:
        by_code[interval.code].append(interval)
    for code, values in by_code.items():
        for previous, current in pairwise(values):
            if current.effective_from <= previous.effective_to:
                message = (
                    "EODHD ticker history has conflicting authoritative intervals "
                    f"for {code}: {previous.effective_from}..{previous.effective_to} "
                    f"and {current.effective_from}..{current.effective_to}"
                )
                raise EODHDMembershipMaterializationError(message)
    return ordered


def _parse_directory(
    evidence: EODHDJsonEvidence,
    *,
    delisted: bool,
) -> tuple[_DirectoryEntry, ...]:
    rows: list[_DirectoryEntry] = []
    for row_number, raw_row in enumerate(
        _object_rows(evidence.payload, field="EODHD US common-stock directory"),
        start=1,
    ):
        security_type = _required_text(
            raw_row.get("Type"),
            field=f"EODHD symbol-directory row {row_number} Type",
        )
        if security_type.casefold() != "common stock":
            message = "EODHD common-stock directory returned a different security type"
            raise EODHDMembershipMaterializationError(message)
        rows.append(
            _DirectoryEntry(
                code=_symbol(
                    raw_row.get("Code"),
                    field=f"EODHD symbol-directory row {row_number} Code",
                ),
                name=_optional_text(
                    raw_row.get("Name"),
                    field=f"EODHD symbol-directory row {row_number} Name",
                ),
                exchange=_optional_text(
                    raw_row.get("Exchange"),
                    field=f"EODHD symbol-directory row {row_number} Exchange",
                ),
                isin=(
                    value.upper()
                    if (
                        value := _optional_text(
                            raw_row.get("Isin"),
                            field=f"EODHD symbol-directory row {row_number} Isin",
                        )
                    )
                    is not None
                    else None
                ),
                delisted=delisted,
            )
        )
    if not rows:
        kind = "delisted" if delisted else "active"
        message = f"EODHD {kind} US common-stock directory is empty"
        raise EODHDMembershipMaterializationError(message)
    return tuple(rows)


def _parse_symbol_changes(
    evidence: EODHDJsonEvidence,
) -> tuple[_SymbolChange, ...]:
    """Validate the provider's dated US ticker-continuity edges."""

    changes: set[_SymbolChange] = set()
    for row_number, raw_row in enumerate(
        _object_rows(evidence.payload, field="EODHD US symbol-change history"),
        start=1,
    ):
        exchange = _required_text(
            raw_row.get("exchange"),
            field=f"EODHD symbol-change row {row_number} exchange",
        ).upper()
        if exchange != "US":
            message = "EODHD US symbol-change history returned another exchange"
            raise EODHDMembershipMaterializationError(message)
        # Preserve provider rows whose symbol spelling is not canonical (for
        # example ``"COPP B"``). Such an old symbol cannot match a validated
        # index component and is therefore irrelevant to resolution, while an
        # invalid successor for a relevant component is rejected explicitly at
        # the point of use. The exact source bytes and count remain auditable.
        old_symbol = (
            _required_text(
                raw_row.get("old_symbol"),
                field=f"EODHD symbol-change row {row_number} old_symbol",
            )
            .upper()
            .removesuffix(".US")
        )
        new_symbol = (
            _required_text(
                raw_row.get("new_symbol"),
                field=f"EODHD symbol-change row {row_number} new_symbol",
            )
            .upper()
            .removesuffix(".US")
        )
        if old_symbol == new_symbol:
            message = f"EODHD symbol-change row {row_number} does not change the symbol"
            raise EODHDMembershipMaterializationError(message)
        effective = _optional_date(
            raw_row.get("effective"),
            field=f"EODHD symbol-change row {row_number} effective",
        )
        if effective is None:
            message = f"EODHD symbol-change row {row_number} requires an effective date"
            raise EODHDMembershipMaterializationError(message)
        change = _SymbolChange(
            old_symbol=old_symbol,
            new_symbol=new_symbol,
            company_name=_required_text(
                raw_row.get("company_name"),
                field=f"EODHD symbol-change row {row_number} company_name",
            ),
            effective=effective,
        )
        changes.add(change)
    return tuple(
        sorted(
            changes,
            key=lambda item: (item.effective, item.old_symbol, item.new_symbol),
        )
    )


def _mapping_integer(payload: Mapping[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        message = f"EODHD ID-mapping meta.{field} must be a non-negative integer"
        raise EODHDMembershipMaterializationError(message)
    return value


def _parse_mapping(
    evidence: EODHDJsonEvidence,
    *,
    code: str,
) -> tuple[_IdentifierMapping, ...]:
    root = _root_mapping(evidence.payload, field=f"EODHD ID mapping for {code}")
    meta = root.get("meta")
    data = root.get("data")
    links = root.get("links")
    if (
        not isinstance(meta, Mapping)
        or not isinstance(data, list)
        or not isinstance(links, Mapping)
    ):
        message = f"EODHD ID mapping for {code} has an invalid envelope"
        raise EODHDMembershipMaterializationError(message)
    total = _mapping_integer(meta, "total")
    limit = _mapping_integer(meta, "limit")
    offset = _mapping_integer(meta, "offset")
    if limit != _ID_MAPPING_PAGE_LIMIT or offset != 0:
        message = f"EODHD ID mapping pagination metadata differs for {code}"
        raise EODHDMembershipMaterializationError(message)
    if total != len(data) or links.get("next") not in {None, ""}:
        message = f"EODHD exact ID mapping for {code} requires unsupported pagination"
        raise EODHDMembershipMaterializationError(message)
    expected_symbol = f"{code}.US"
    mappings: list[_IdentifierMapping] = []
    for row_number, raw_row in enumerate(data, start=1):
        if not isinstance(raw_row, Mapping):
            message = f"EODHD ID-mapping row {row_number} for {code} must be an object"
            raise EODHDMembershipMaterializationError(message)
        symbol = _required_text(
            raw_row.get("symbol"),
            field=f"EODHD ID-mapping row {row_number} symbol",
        ).upper()
        if symbol != expected_symbol:
            message = f"EODHD exact ID mapping for {code} returned {symbol}"
            raise EODHDMembershipMaterializationError(message)
        identifiers = {
            field: (
                value.upper()
                if (value := _optional_text(raw_row.get(field), field=f"ID mapping {field}"))
                is not None
                else None
            )
            for field in ("isin", "figi", "lei", "cusip", "cik")
        }
        cik = identifiers["cik"]
        if cik is not None and (not cik.isdigit() or len(cik) > _MAX_CIK_DIGITS):
            message = f"EODHD ID-mapping CIK for {code} is invalid"
            raise EODHDMembershipMaterializationError(message)
        mappings.append(
            _IdentifierMapping(
                symbol=symbol,
                isin=identifiers["isin"],
                figi=identifiers["figi"],
                lei=identifiers["lei"],
                cusip=identifiers["cusip"],
                cik=cik.zfill(10) if cik is not None else None,
            )
        )
    return tuple(mappings)


def _parse_general_fundamentals(
    evidence: EODHDJsonEvidence,
    *,
    provider_symbol: str,
) -> _GeneralFundamentals:
    payload = evidence.payload
    if payload is None:
        root: Mapping[str, Any] = {}
    elif isinstance(payload, Mapping):
        root = payload
    else:
        message = f"EODHD Fundamentals General for {provider_symbol} must be an object or null"
        raise EODHDMembershipMaterializationError(message)

    code = _optional_symbol(
        root.get("Code"),
        field=f"EODHD Fundamentals General Code for {provider_symbol}",
    )
    if code is not None and code != provider_symbol:
        message = (
            f"EODHD Fundamentals General Code {code} does not match requested "
            f"provider symbol {provider_symbol}"
        )
        raise EODHDMembershipMaterializationError(message)
    return _GeneralFundamentals(
        code=code,
        name=_optional_text(
            root.get("Name"),
            field=f"EODHD Fundamentals General Name for {provider_symbol}",
        ),
        ipo_date=_optional_date(
            root.get("IPODate"),
            field=f"EODHD Fundamentals General IPODate for {provider_symbol}",
        ),
        is_delisted=_optional_flag(
            root.get("IsDelisted"),
            field=f"EODHD Fundamentals General IsDelisted for {provider_symbol}",
        ),
        updated_at=_optional_date(
            root.get("UpdatedAt"),
            field=f"EODHD Fundamentals General UpdatedAt for {provider_symbol}",
        ),
        isin=_optional_isin(
            root.get("ISIN"),
            field=f"EODHD Fundamentals General ISIN for {provider_symbol}",
        ),
        cik=_optional_cik(
            root.get("CIK"),
            field=f"EODHD Fundamentals General CIK for {provider_symbol}",
        ),
    )


def _normalized_name(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.casefold().split())


def _deduplicated_directory(
    rows: Sequence[_DirectoryEntry],
) -> tuple[_DirectoryEntry, ...]:
    unique = {(row.code, row.name, row.exchange, row.isin, row.delisted): row for row in rows}
    return tuple(
        sorted(
            unique.values(),
            key=lambda row: (row.delisted, row.isin or "", row.exchange or "", row.name or ""),
        )
    )


def _select_directory_entry(
    candidates: Sequence[_DirectoryEntry],
    *,
    names: Sequence[str],
    mappings: Sequence[_IdentifierMapping],
    histories: Sequence[_TickerHistory],
) -> tuple[_DirectoryEntry | None, str]:
    rows = _deduplicated_directory(candidates)
    if not rows:
        return None, "absent_from_active_and_delisted_directories"
    selected: _DirectoryEntry | None = None
    disposition = "ambiguous_exact_code_directory_rows"
    normalized_names = {_normalized_name(name) for name in names}
    name_matches = tuple(row for row in rows if _normalized_name(row.name) in normalized_names)
    if len(name_matches) == 1:
        selected = name_matches[0]
        disposition = "exact_snapshot_name"
    mapping_isins = {mapping.isin for mapping in mappings if mapping.isin is not None}
    if selected is None and mapping_isins:
        matched = tuple(row for row in rows if row.isin in mapping_isins)
        if len(matched) == 1:
            selected = matched[0]
            disposition = "exact_mapping_isin"
    if selected is None and histories and all(history.is_delisted for history in histories):
        delisted = tuple(row for row in rows if row.delisted)
        if len(delisted) == 1:
            selected = delisted[0]
            disposition = "historical_delisted_flag"
    if selected is None and any(history.is_active_now for history in histories):
        active = tuple(row for row in rows if not row.delisted)
        if len(active) == 1:
            selected = active[0]
            disposition = "historical_active_flag"
    if selected is None and len(rows) == 1:
        selected = rows[0]
        disposition = "unique_exact_code"
    return selected, disposition


def _unique_identifier(
    mappings: Sequence[_IdentifierMapping],
    field: str,
) -> str | None:
    values = {value for mapping in mappings if (value := getattr(mapping, field)) is not None}
    return next(iter(values)) if len(values) == 1 else None


def _provider_symbol_for_component(
    *,
    code: str,
    names: Sequence[str],
    directory_rows: Sequence[_DirectoryEntry],
    membership_codes: frozenset[str],
    symbol_changes: Sequence[_SymbolChange],
) -> tuple[str, str]:
    changes_by_old: defaultdict[str, list[_SymbolChange]] = defaultdict(list)
    for change in symbol_changes:
        changes_by_old[change.old_symbol].append(change)
    normalized_names = {
        normalized for name in names if (normalized := _normalized_name(name)) is not None
    }
    chain: list[_SymbolChange] = []
    successor = code
    previous_effective: date | None = None
    while successor in changes_by_old:
        candidates = [
            candidate
            for candidate in changes_by_old[successor]
            if previous_effective is None or candidate.effective > previous_effective
        ]
        if not candidates:
            # A ticker may legitimately recur (for example FISV -> FI -> FISV).
            # Replay only later dated edges; reaching an already-consumed symbol
            # ends the finite historical chain at its current storage ticker.
            break
        if len(candidates) == 1:
            change = candidates[0]
        else:
            change_name_matches = [
                candidate
                for candidate in candidates
                if _normalized_name(candidate.company_name) in normalized_names
            ]
            if len(change_name_matches) > 1:
                edges = sorted(
                    f"{item.old_symbol}->{item.new_symbol}@{item.effective.isoformat()}"
                    for item in change_name_matches
                )
                message = (
                    "EODHD symbol-change history is ambiguous for membership component "
                    f"{code}: {edges}"
                )
                raise EODHDMembershipMaterializationError(message)
            if not change_name_matches:
                break
            change = change_name_matches[0]
        if any(character.isspace() for character in change.new_symbol):
            message = (
                "EODHD symbol-change history has a non-canonical successor for "
                f"membership component {code}: {change.new_symbol!r}"
            )
            raise EODHDMembershipMaterializationError(message)
        chain.append(change)
        previous_effective = change.effective
        successor = change.new_symbol
    if chain and all(change.new_symbol in membership_codes for change in chain):
        lineage = ",".join(
            f"{change.old_symbol}->{change.new_symbol}@{change.effective.isoformat()}"
            for change in chain
        )
        return successor, f"eodhd_symbol_change_history_successor_storage:{lineage}"
    rows = _deduplicated_directory(directory_rows)
    exact_code_rows = tuple(row for row in rows if row.code == code)
    exact_code_name_matches = tuple(
        row for row in exact_code_rows if _normalized_name(row.name) in normalized_names
    )
    if exact_code_name_matches:
        if len(exact_code_rows) != 1:
            message = (
                "EODHD symbol directories contain ambiguous exact-code identities "
                f"for component {code}"
            )
            raise EODHDMembershipMaterializationError(message)
        return code, "historical_component_exact_directory_code_name_match"

    name_matches = tuple(row for row in rows if _normalized_name(row.name) in normalized_names)
    if len(name_matches) == 1:
        return name_matches[0].code, "exact_component_name_directory_storage_alias"
    if len(name_matches) > 1:
        candidate_codes = sorted({row.code for row in name_matches})
        message = (
            "EODHD symbol directories contain ambiguous exact component-name "
            f"storage aliases for {code}: {candidate_codes}"
        )
        raise EODHDMembershipMaterializationError(message)
    if exact_code_rows:
        message = (
            f"EODHD exact directory code {code} does not match its historical "
            "component name and no unique storage alias exists"
        )
        raise EODHDMembershipMaterializationError(message)
    return code, "component_code_absent_from_symbol_directories"


def _store_evidence(
    output_root: Path,
    evidence: EODHDJsonEvidence,
    *,
    role: str,
    dataset_version: str,
    entitlement_scope: str,
    row_count: int | None,
    requested_symbol: str | None = None,
) -> ContentAddressedArtifact:
    return store_content_object(
        output_root,
        evidence.content,
        suffix=".json",
        role=role,
        media_type=_JSON_MEDIA_TYPE,
        row_count=row_count,
        context={
            "content_sha256": evidence.content_sha256,
            "dataset_version": dataset_version,
            "endpoint": evidence.endpoint,
            "entitlement_scope": entitlement_scope,
            "provider": _PROVIDER,
            "requested_symbol": requested_symbol,
            "retrieved_at": evidence.retrieved_at.isoformat(),
            "scope": "personal_historical_validation_only",
        },
    )


def _general_fundamentals_payload(
    general: _GeneralFundamentals,
) -> dict[str, object]:
    return {
        "cik": general.cik,
        "code": general.code,
        "ipo_date": general.ipo_date.isoformat() if general.ipo_date is not None else None,
        "is_delisted": general.is_delisted,
        "isin": general.isin,
        "name": general.name,
        "updated_at": (general.updated_at.isoformat() if general.updated_at is not None else None),
    }


def _validate_general_identifiers(
    *,
    provider_symbol: str,
    general: _GeneralFundamentals,
    general_artifact: ContentAddressedArtifact,
    directory_entry: _DirectoryEntry | None,
    mappings: Sequence[_IdentifierMapping],
) -> str:
    observed_isins = {
        value
        for value in (
            directory_entry.isin if directory_entry is not None else None,
            *(mapping.isin for mapping in mappings),
        )
        if value is not None
    }
    observed_ciks = {mapping.cik for mapping in mappings if mapping.cik is not None}
    contradictions: dict[str, object] = {}
    if general.isin is not None and observed_isins and general.isin not in observed_isins:
        contradictions["isin"] = {
            "fundamentals_general": general.isin,
            "selected_directory_or_mapping": sorted(observed_isins),
        }
    if general.cik is not None and any(value != general.cik for value in observed_ciks):
        contradictions["cik"] = {
            "fundamentals_general": general.cik,
            "id_mapping": sorted(observed_ciks),
        }
    if contradictions:
        message = (
            "EODHD Fundamentals General identity contradicts selected directory/ID "
            f"mapping for {provider_symbol}: {contradictions}; "
            f"evidence={general_artifact.context['endpoint']}"
            f"#sha256={general_artifact.sha256};"
            f"retrieved_at={general_artifact.context['retrieved_at']}"
        )
        raise EODHDMembershipMaterializationError(message)
    if general.code == provider_symbol:
        return "exact_provider_symbol_code"
    if general.isin is not None and general.isin in observed_isins:
        return "matching_permanent_isin"
    if general.cik is not None and general.cik in observed_ciks:
        return "matching_sec_cik"
    if general.ipo_date is not None:
        message = (
            "EODHD Fundamentals General IPODate is not positively bound to the "
            f"resolved legal security for {provider_symbol}; require exact Code or "
            "a matching permanent ISIN/CIK; "
            f"evidence={general_artifact.context['endpoint']}"
            f"#sha256={general_artifact.sha256};"
            f"retrieved_at={general_artifact.context['retrieved_at']}"
        )
        raise EODHDMembershipMaterializationError(message)
    return "nullable_or_unbound_without_lifetime_claim"


def _resolve_identity(
    *,
    code: str,
    provider_symbol: str,
    provider_symbol_disposition: str,
    names: Sequence[str],
    directory_rows: Sequence[_DirectoryEntry],
    histories: Sequence[_TickerHistory],
    mappings: Sequence[_IdentifierMapping],
    mapping_artifact: ContentAddressedArtifact,
    general: _GeneralFundamentals,
    general_artifact: ContentAddressedArtifact,
    active_artifact: ContentAddressedArtifact,
    delisted_artifact: ContentAddressedArtifact,
    provider_symbol_source_ref: str | None,
) -> _IdentityResolution:
    directory_entry, directory_disposition = _select_directory_entry(
        directory_rows,
        names=names,
        mappings=mappings,
        histories=histories,
    )
    general_identity_binding = _validate_general_identifiers(
        provider_symbol=provider_symbol,
        general=general,
        general_artifact=general_artifact,
        directory_entry=directory_entry,
        mappings=mappings,
    )
    directory_disposition = f"{provider_symbol_disposition}:{directory_disposition}"
    mapping_isins = {mapping.isin for mapping in mappings if mapping.isin is not None}
    directory_conflict = (
        directory_entry is not None
        and directory_entry.isin is not None
        and bool(mapping_isins)
        and directory_entry.isin not in mapping_isins
    )
    figi = _unique_identifier(mappings, "figi")
    isin = _unique_identifier(mappings, "isin")
    cusip = _unique_identifier(mappings, "cusip")
    if directory_conflict:
        security_id = ""
        security_id_source = "directory_mapping_isin_conflict"
    elif figi is not None:
        security_id = f"figi:{figi}"
        security_id_source = "unique_figi"
    elif directory_entry is not None and directory_entry.isin is not None:
        security_id = f"isin:{directory_entry.isin}"
        security_id_source = "exact_directory_isin"
    elif isin is not None:
        security_id = f"isin:{isin}"
        security_id_source = "unique_isin"
    elif cusip is not None:
        security_id = f"cusip:{cusip}"
        security_id_source = "unique_cusip"
    else:
        security_id = ""
        security_id_source = "no_unique_permanent_identifier"
    security_id_permanent = bool(security_id)
    if not security_id:
        security_id = "eodhd-unresolved:" + evidence_sha256(
            {
                "active_directory_sha256": active_artifact.sha256,
                "code": code,
                "delisted_directory_sha256": delisted_artifact.sha256,
                "directory_disposition": directory_disposition,
                "fundamentals_general_sha256": general_artifact.sha256,
                "mapping_sha256": mapping_artifact.sha256,
                "names": list(names),
                "policy_version": _POLICY_VERSION,
            }
        )
    source_ref = (
        f"membership_component={code};provider_symbol={provider_symbol};"
        f"provider_symbol_resolution={provider_symbol_disposition};"
        f"{mapping_artifact.context['endpoint']}#sha256={mapping_artifact.sha256};"
        f"{general_artifact.context['endpoint']}#sha256={general_artifact.sha256};"
        f"fundamentals_general_identity_binding={general_identity_binding};"
        f"active_directory#sha256={active_artifact.sha256};"
        f"delisted_directory#sha256={delisted_artifact.sha256};"
        f"provider_symbol_source={provider_symbol_source_ref or 'directory_only'}"
    )
    resolution_payload = {
        "code": code,
        "directory_disposition": directory_disposition,
        "directory_entry": (
            {
                "code": directory_entry.code,
                "delisted": directory_entry.delisted,
                "exchange": directory_entry.exchange,
                "isin": directory_entry.isin,
                "name": directory_entry.name,
            }
            if directory_entry is not None
            else None
        ),
        "fundamentals_general": _general_fundamentals_payload(general),
        "fundamentals_general_identity_binding": general_identity_binding,
        "fundamentals_general_sha256": general_artifact.sha256,
        "mapping_sha256": mapping_artifact.sha256,
        "names": list(names),
        "provider_symbol": provider_symbol,
        "provider_symbol_disposition": provider_symbol_disposition,
        "provider_symbol_source_ref": provider_symbol_source_ref,
        "security_id": security_id,
        "security_id_permanent": security_id_permanent,
        "security_id_source": security_id_source,
    }
    return _IdentityResolution(
        code=code,
        provider_symbol=provider_symbol,
        security_id=security_id,
        security_id_source=security_id_source,
        security_id_permanent=security_id_permanent,
        directory_disposition=directory_disposition,
        directory_entry=directory_entry,
        mapping=tuple(mappings),
        mapping_artifact=mapping_artifact,
        general=general,
        general_artifact=general_artifact,
        general_identity_binding=general_identity_binding,
        source_ref=source_ref,
        resolution_id=evidence_sha256(resolution_payload),
    )
