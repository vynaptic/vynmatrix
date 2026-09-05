"""Licensed EODHD effective-date membership and security identity acquisition.

This validation-only boundary treats ``HistoricalTickerComponents`` intervals
as the membership authority documented by EODHD.  ``EndDate`` is the exclusive
removal boundary, so authoritative intervals are half open.  Dated
``HistoricalComponents`` full-state snapshots are retained as an independent
consistency witness; they never silently create, extend, or relabel a legal
security's membership interval.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import combinations, pairwise
from pathlib import Path

from dev_cli.validation.backtest.equity_membership_bundle import (
    EODHDMembershipMaterializationResult,
    EODHDMembershipProvider,
    freeze_eodhd_membership_materialization,
    load_frozen_eodhd_membership_materialization,
    load_optional_membership_correction_evidence,
    validate_eodhd_membership_materialization_request,
)
from dev_cli.validation.backtest.equity_membership_corrections import (
    FrozenMembershipCorrectionEvidence,
    MembershipCorrectionAction,
    MembershipCorrectionIdentity,
    MembershipCorrectionInterval,
    MembershipIdentityEdgeUsage,
    ReviewedMembershipCorrection,
)
from dev_cli.validation.backtest.equity_membership_eodhd_source import (
    _CSV_MEDIA_TYPE,
    _GENERAL_FUNDAMENTALS_ARTIFACT_ROLE,
    _JSON_MEDIA_TYPE,
    _POLICY_VERSION,
    _PROVIDER,
    _SOURCE_KIND,
    _SYMBOL_CHANGE_HISTORY_START,
    _TICKER_COMPONENTS_DOCUMENTATION,
    EODHDMembershipMaterializationError,
    _build_intervals,
    _ComponentSnapshot,
    _crosscheck_snapshots,
    _deduplicated_directory,
    _DirectoryEntry,
    _general_fundamentals_payload,
    _GeneralFundamentals,
    _IdentifierMapping,
    _IdentityResolution,
    _IntervalDraft,
    _MembershipCrosscheckDiscrepancy,
    _MembershipCrosscheckDisposition,
    _normalized_name,
    _parse_directory,
    _parse_general_fundamentals,
    _parse_mapping,
    _parse_snapshots,
    _parse_symbol_changes,
    _parse_ticker_history,
    _provider_symbol_for_component,
    _resolve_identity,
    _store_evidence,
    _SymbolChange,
    _TickerHistory,
    _unique_identifier,
    _validate_issuer_lifetimes,
    _validate_ticker_history_intervals,
)
from dev_cli.validation.evidence import (
    ContentAddressedArtifact,
    canonical_json_bytes,
    evidence_sha256,
    store_content_object,
)
from dev_cli.validation.providers.eodhd import EODHDJsonEvidence


@dataclass(frozen=True, slots=True)
class _ResolutionTarget:
    key: str
    code: str
    names: tuple[str, ...]
    correction_id: str | None
    reviewed_identity: MembershipCorrectionIdentity | None


@dataclass(frozen=True, slots=True)
class _AcquiredMembershipInputs:
    ticker_evidence: EODHDJsonEvidence
    snapshot_evidence: EODHDJsonEvidence
    active_evidence: EODHDJsonEvidence
    delisted_evidence: EODHDJsonEvidence
    symbol_change_evidence: EODHDJsonEvidence
    snapshots: tuple[_ComponentSnapshot, ...]
    histories: tuple[_TickerHistory, ...]
    intervals: tuple[_IntervalDraft, ...]
    active_rows: tuple[_DirectoryEntry, ...]
    delisted_rows: tuple[_DirectoryEntry, ...]
    symbol_changes: tuple[_SymbolChange, ...]
    crosscheck_discrepancies: tuple[_MembershipCrosscheckDiscrepancy, ...]
    correction_evidence: FrozenMembershipCorrectionEvidence | None


def _validate_component_code_recurrence(intervals: Sequence[_IntervalDraft]) -> None:
    """Reject ticker reuse that current identifier endpoints cannot date-resolve."""

    by_code: dict[str, list[_IntervalDraft]] = defaultdict(list)
    for interval in intervals:
        by_code[interval.code].append(interval)
    for code, values in by_code.items():
        ordered = sorted(values, key=lambda item: (item.effective_from, item.effective_to))
        unreviewed = [item for item in ordered if item.reviewed_identity is None]
        for previous, current in combinations(unreviewed, 2):
            previous_names = {
                normalized
                for name in previous.names
                if (normalized := _normalized_name(name)) is not None
            }
            current_names = {
                normalized
                for name in current.names
                if (normalized := _normalized_name(name)) is not None
            }
            if previous_names.isdisjoint(current_names):
                message = (
                    "EODHD HistoricalTickerComponents appears to reuse component code "
                    f"{code} for different names across non-contiguous membership "
                    "intervals; current ID mapping cannot date-resolve the issuer"
                )
                raise EODHDMembershipMaterializationError(message)
        for previous, current in pairwise(ordered):
            previous_names = {
                normalized
                for name in previous.names
                if (normalized := _normalized_name(name)) is not None
            }
            current_names = {
                normalized
                for name in current.names
                if (normalized := _normalized_name(name)) is not None
            }
            if previous_names.isdisjoint(current_names):
                # Reviewed primary-source endpoints receive their own dated
                # resolution key. One reviewed side therefore partitions the
                # provider code from the current endpoint; all-unreviewed
                # recurrence pairs were rejected above.
                if previous.reviewed_identity is not None or current.reviewed_identity is not None:
                    continue
                message = (
                    "EODHD HistoricalTickerComponents appears to reuse component code "
                    f"{code} for different names across non-contiguous membership "
                    "intervals; current ID mapping cannot date-resolve the issuer"
                )
                raise EODHDMembershipMaterializationError(message)


def _reviewed_identity_payload(identity: MembershipCorrectionIdentity) -> dict[str, object]:
    return {
        "cik": identity.cik,
        "code": identity.code,
        "is_active_now": identity.is_active_now,
        "is_delisted": identity.is_delisted,
        "name": identity.name,
        "provider_symbol": identity.provider_symbol,
        "security_id": identity.security_id,
        "valid_from": identity.valid_from.isoformat() if identity.valid_from else None,
        "valid_to_exclusive": (
            identity.valid_to_exclusive.isoformat() if identity.valid_to_exclusive else None
        ),
    }


def _reviewed_resolution_key(
    correction_id: str,
    identity: MembershipCorrectionIdentity,
) -> str:
    return "reviewed:" + evidence_sha256(
        {
            "correction_id": correction_id,
            "identity": _reviewed_identity_payload(identity),
        }
    )


def _interval_resolution_key(interval: _IntervalDraft) -> str:
    if interval.reviewed_identity is None:
        return f"provider:{interval.code}"
    if interval.authority_correction_id is None:
        message = "reviewed membership identity lacks correction provenance"
        raise EODHDMembershipMaterializationError(message)
    return _reviewed_resolution_key(
        interval.authority_correction_id,
        interval.reviewed_identity,
    )


def _resolution_targets(
    intervals: Sequence[_IntervalDraft],
) -> dict[str, _ResolutionTarget]:
    names_by_key: dict[str, set[str]] = defaultdict(set)
    targets: dict[str, _ResolutionTarget] = {}
    for interval in intervals:
        key = _interval_resolution_key(interval)
        names_by_key[key].update(interval.names)
        target = _ResolutionTarget(
            key=key,
            code=interval.code,
            names=(),
            correction_id=interval.authority_correction_id,
            reviewed_identity=interval.reviewed_identity,
        )
        previous = targets.setdefault(key, target)
        if (
            previous.code != target.code
            or previous.correction_id != target.correction_id
            or previous.reviewed_identity != target.reviewed_identity
        ):
            message = f"membership resolution target collision for {key}"
            raise EODHDMembershipMaterializationError(message)
    return {
        key: _ResolutionTarget(
            key=target.key,
            code=target.code,
            names=tuple(sorted(names_by_key[key])),
            correction_id=target.correction_id,
            reviewed_identity=target.reviewed_identity,
        )
        for key, target in targets.items()
    }


def _resolve_reviewed_identity(
    *,
    target: _ResolutionTarget,
    mappings: Sequence[_IdentifierMapping],
    mapping_artifact: ContentAddressedArtifact,
    general: _GeneralFundamentals,
    general_artifact: ContentAddressedArtifact,
    directory_rows: Sequence[_DirectoryEntry],
    correction_evidence: FrozenMembershipCorrectionEvidence,
) -> _IdentityResolution:
    identity = target.reviewed_identity
    if identity is None or target.correction_id is None or identity.provider_symbol is None:
        message = "reviewed membership resolution target is incomplete"
        raise EODHDMembershipMaterializationError(message)
    exact_directory_rows = tuple(
        row
        for row in _deduplicated_directory(directory_rows)
        if row.code == identity.provider_symbol
        and _normalized_name(row.name) == _normalized_name(identity.name)
    )
    directory_entry = exact_directory_rows[0] if len(exact_directory_rows) == 1 else None
    correction_artifact_hashes = sorted(
        artifact.sha256 for artifact in correction_evidence.artifacts
    )
    source_ref = (
        f"reviewed_identity_correction={target.correction_id};"
        f"reviewed_correction_manifest={correction_evidence.manifest_sha256};"
        f"reviewed_correction_artifacts={','.join(correction_artifact_hashes)};"
        f"provider_symbol={identity.provider_symbol};"
        f"{mapping_artifact.context['endpoint']}#sha256={mapping_artifact.sha256};"
        f"{general_artifact.context['endpoint']}#sha256={general_artifact.sha256};"
        "price_stitch_allowed=false"
    )
    resolution_payload = {
        "correction_artifact_sha256s": correction_artifact_hashes,
        "correction_evidence_manifest_sha256": correction_evidence.manifest_sha256,
        "correction_id": target.correction_id,
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
        "fundamentals_general_sha256": general_artifact.sha256,
        "mapping_sha256": mapping_artifact.sha256,
        "reviewed_identity": _reviewed_identity_payload(identity),
    }
    return _IdentityResolution(
        code=identity.code,
        provider_symbol=identity.provider_symbol,
        security_id=identity.security_id,
        security_id_source=f"reviewed_primary_identity_edge:{target.correction_id}",
        security_id_permanent=True,
        directory_disposition="reviewed_primary_identity_edge",
        directory_entry=directory_entry,
        mapping=tuple(mappings),
        mapping_artifact=mapping_artifact,
        general=general,
        general_artifact=general_artifact,
        general_identity_binding="unbound_current_endpoint_observation_reviewed_identity",
        source_ref=source_ref,
        resolution_id=evidence_sha256(resolution_payload),
    )


def _csv_bytes(header: Sequence[str], rows: Sequence[Sequence[object]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _validate_security_timelines(
    intervals: Sequence[_IntervalDraft],
    resolutions: Mapping[str, _IdentityResolution],
) -> None:
    by_security: dict[str, list[_IntervalDraft]] = defaultdict(list)
    for interval in intervals:
        by_security[resolutions[_interval_resolution_key(interval)].security_id].append(interval)
    for security_id, values in by_security.items():
        ordered = sorted(values, key=lambda item: (item.effective_from, item.effective_to))
        for previous, current in pairwise(ordered):
            if current.effective_from <= previous.effective_to:
                message = (
                    "EODHD permanent identity maps overlapping membership tickers "
                    f"for {security_id}: {previous.code}, {current.code}"
                )
                raise EODHDMembershipMaterializationError(message)
    by_provider_symbol: dict[str, list[_IntervalDraft]] = defaultdict(list)
    for interval in intervals:
        resolution = resolutions[_interval_resolution_key(interval)]
        by_provider_symbol[resolution.provider_symbol].append(interval)
    for provider_symbol, values in by_provider_symbol.items():
        ordered = sorted(values, key=lambda item: (item.effective_from, item.effective_to))
        for previous, current in pairwise(ordered):
            if current.effective_from <= previous.effective_to and previous.code != current.code:
                message = (
                    "EODHD provider symbol maps overlapping membership securities "
                    f"for {provider_symbol}: {previous.code}, {current.code}"
                )
                raise EODHDMembershipMaterializationError(message)


def _validate_reviewed_correction_identities(
    acquired: _AcquiredMembershipInputs,
    resolutions: Mapping[str, _IdentityResolution],
) -> None:
    if acquired.correction_evidence is None:
        return
    for correction in acquired.correction_evidence.spec.corrections:
        if correction.action is MembershipCorrectionAction.IDENTITY_BRIDGE_ONLY:
            continue
        for identity in (correction.old_identity, correction.new_identity):
            resolution = resolutions.get(f"provider:{identity.code}")
            if resolution is None:
                continue
            observed_ciks = {
                value
                for value in (
                    resolution.general.cik,
                    *(mapping.cik for mapping in resolution.mapping),
                )
                if value is not None
            }
            if observed_ciks and identity.cik not in observed_ciks:
                message = (
                    f"membership correction {correction.correction_id} CIK contradicts "
                    f"resolved provider identity for {identity.code}"
                )
                raise EODHDMembershipMaterializationError(message)
            identifier_kind, separator, identifier_value = identity.security_id.partition(":")
            observed_identifiers: set[str] = set()
            if separator and identifier_kind == "figi":
                observed_identifiers.update(
                    mapping.figi for mapping in resolution.mapping if mapping.figi is not None
                )
            elif separator and identifier_kind == "isin":
                observed_identifiers.update(
                    mapping.isin for mapping in resolution.mapping if mapping.isin is not None
                )
                if resolution.general.isin is not None:
                    observed_identifiers.add(resolution.general.isin)
                if (
                    resolution.directory_entry is not None
                    and resolution.directory_entry.isin is not None
                ):
                    observed_identifiers.add(resolution.directory_entry.isin)
            elif separator and identifier_kind == "cusip":
                observed_identifiers.update(
                    mapping.cusip for mapping in resolution.mapping if mapping.cusip is not None
                )
            if observed_identifiers and identifier_value not in observed_identifiers:
                message = (
                    f"membership correction {correction.correction_id} permanent identity "
                    f"contradicts resolved provider identity for {identity.code}"
                )
                raise EODHDMembershipMaterializationError(message)


def _alias_rows(
    intervals: Sequence[_IntervalDraft],
    resolutions: Mapping[str, _IdentityResolution],
    *,
    history_start: date,
) -> list[tuple[object, ...]]:
    by_security: dict[str, list[_IntervalDraft]] = defaultdict(list)
    targets: dict[tuple[str, str], list[_IntervalDraft]] = defaultdict(list)
    for interval in intervals:
        security_id = resolutions[_interval_resolution_key(interval)].security_id
        by_security[security_id].append(interval)
        targets[(security_id, interval.code)].append(interval)
    rows: list[tuple[object, ...]] = []
    for (security_id, canonical_symbol), target_intervals in sorted(targets.items()):
        first_target_start = min(interval.effective_from for interval in target_intervals)
        coverage_end = max(interval.effective_to for interval in target_intervals)
        identity_relation_only = any(
            interval.reviewed_edge_usage is MembershipIdentityEdgeUsage.IDENTITY_RELATION_ONLY
            for interval in target_intervals
        )
        target_resolution_keys = {
            _interval_resolution_key(interval) for interval in target_intervals
        }
        alias_history_start = first_target_start if identity_relation_only else history_start
        reviewed_identity_starts = {
            interval.reviewed_identity.valid_from
            for interval in target_intervals
            if interval.reviewed_identity is not None
            and interval.reviewed_identity.valid_from is not None
        }
        if len(reviewed_identity_starts) > 1:
            message = (
                "reviewed alias target has conflicting identity validity starts for "
                f"{security_id}/{canonical_symbol}"
            )
            raise EODHDMembershipMaterializationError(message)
        if reviewed_identity_starts:
            alias_history_start = max(alias_history_start, reviewed_identity_starts.pop())
        interposed = tuple(
            interval
            for interval in by_security[security_id]
            if interval.code != canonical_symbol
            and interval.effective_from <= coverage_end
            and interval.effective_to >= first_target_start
        )
        if interposed:
            symbols = sorted({interval.code for interval in interposed})
            message = (
                "cannot source prehistory across a non-contiguous membership ticker "
                f"recurrence for {security_id}/{canonical_symbol}: {symbols}"
            )
            raise EODHDMembershipMaterializationError(message)
        timeline = tuple(
            sorted(
                (
                    interval
                    for interval in by_security[security_id]
                    if interval.effective_from <= coverage_end
                    and interval.effective_to >= alias_history_start
                    and (
                        not identity_relation_only
                        or _interval_resolution_key(interval) in target_resolution_keys
                    )
                ),
                key=lambda item: (item.effective_from, item.effective_to, item.code),
            )
        )
        boundaries = {
            alias_history_start,
            coverage_end + timedelta(days=1),
            first_target_start,
        }
        for interval in timeline:
            if alias_history_start < interval.effective_from <= coverage_end:
                boundaries.add(interval.effective_from)
            if alias_history_start <= interval.effective_to < coverage_end:
                boundaries.add(interval.effective_to + timedelta(days=1))
        for segment_start, next_start in pairwise(sorted(boundaries)):
            containing = tuple(
                interval
                for interval in timeline
                if interval.effective_from <= segment_start <= interval.effective_to
            )
            if len(containing) > 1:
                message = f"ambiguous security identity timeline on {segment_start}"
                raise EODHDMembershipMaterializationError(message)
            if containing:
                selected = containing[0]
            else:
                prior = tuple(
                    interval for interval in timeline if interval.effective_to < segment_start
                )
                selected = (
                    max(prior, key=lambda item: item.effective_to)
                    if prior
                    else min(timeline, key=lambda item: item.effective_from)
                )
            resolution = resolutions[_interval_resolution_key(selected)]
            row = (
                security_id,
                canonical_symbol,
                _PROVIDER,
                resolution.provider_symbol,
                segment_start.isoformat(),
                (next_start - timedelta(days=1)).isoformat(),
                resolution.source_ref,
            )
            if (
                rows
                and rows[-1][:4] == row[:4]
                and rows[-1][6] == row[6]
                and date.fromisoformat(str(rows[-1][5])) + timedelta(days=1) == segment_start
            ):
                rows[-1] = (*rows[-1][:5], row[5], row[6])
            else:
                rows.append(row)
    return rows


def _ticker_history_matches_correction(
    history: _TickerHistory,
    expected: MembershipCorrectionInterval,
) -> bool:
    return (
        history.code == expected.code
        and history.name == expected.name
        and history.start == expected.start_inclusive
        and history.end == expected.end_exclusive
        and history.is_active_now is expected.is_active_now
        and history.is_delisted is expected.is_delisted
        and history.authority_correction_id is None
    )


def _reviewed_replacement_identity(
    correction: ReviewedMembershipCorrection,
    replacement: MembershipCorrectionInterval,
) -> MembershipCorrectionIdentity | None:
    for identity in (correction.new_identity, correction.old_identity):
        if replacement.code == identity.code and identity.provider_symbol is not None:
            return identity
    return None


def _apply_reviewed_authority_corrections(
    histories: Sequence[_TickerHistory],
    correction_evidence: FrozenMembershipCorrectionEvidence | None,
    *,
    requested_start: date,
    requested_end: date,
) -> tuple[_TickerHistory, ...]:
    if correction_evidence is None:
        return tuple(histories)
    corrected = list(histories)
    for correction in correction_evidence.spec.corrections:
        expected = correction.expected_ticker_interval
        if expected is None:
            message = f"membership correction {correction.correction_id} lacks raw precondition"
            raise EODHDMembershipMaterializationError(message)
        matches = [
            index
            for index, history in enumerate(corrected)
            if _ticker_history_matches_correction(history, expected)
        ]
        if len(matches) != 1:
            message = (
                f"membership correction {correction.correction_id} expected exactly one "
                f"raw ticker-history row, observed {len(matches)}"
            )
            raise EODHDMembershipMaterializationError(message)
        if correction.action is MembershipCorrectionAction.IDENTITY_BRIDGE_ONLY:
            old_identity = correction.old_identity
            new_identity = correction.new_identity
            if (
                old_identity.name is None
                or new_identity.name is None
                or old_identity.is_active_now is None
                or new_identity.is_active_now is None
                or old_identity.is_delisted is None
                or new_identity.is_delisted is None
                or correction.edge_usage is None
            ):
                message = (
                    f"membership correction {correction.correction_id} lacks a complete "
                    "reviewed identity endpoint"
                )
                raise EODHDMembershipMaterializationError(message)
            raw_start = expected.start_inclusive or date.min
            raw_end = expected.end_exclusive or date.max
            requested_end_exclusive = requested_end + timedelta(days=1)
            before_reviewed_scope = max(requested_start, raw_start) < min(
                requested_end_exclusive,
                correction.valid_from,
                raw_end,
            )
            after_reviewed_scope = correction.valid_to_exclusive is not None and max(
                requested_start,
                raw_start,
                correction.valid_to_exclusive,
            ) < min(requested_end_exclusive, raw_end)
            if before_reviewed_scope or after_reviewed_scope:
                message = (
                    f"membership correction {correction.correction_id} cannot partition "
                    "the requested range outside its reviewed validity"
                )
                raise EODHDMembershipMaterializationError(message)
            reviewed_end_candidates = tuple(
                value
                for value in (
                    expected.end_exclusive,
                    correction.valid_to_exclusive,
                    new_identity.valid_to_exclusive,
                )
                if value is not None
            )
            corrected[matches[0] : matches[0] + 1] = [
                _TickerHistory(
                    code=old_identity.code,
                    name=old_identity.name,
                    start=max(
                        raw_start,
                        correction.valid_from,
                        old_identity.valid_from or date.min,
                    ),
                    end=correction.effective_date,
                    is_active_now=old_identity.is_active_now,
                    is_delisted=old_identity.is_delisted,
                    authority_correction_id=correction.correction_id,
                    reviewed_identity=old_identity,
                    reviewed_edge_usage=correction.edge_usage,
                ),
                _TickerHistory(
                    code=new_identity.code,
                    name=new_identity.name,
                    start=correction.effective_date,
                    end=(min(reviewed_end_candidates) if reviewed_end_candidates else None),
                    is_active_now=new_identity.is_active_now,
                    is_delisted=new_identity.is_delisted,
                    authority_correction_id=correction.correction_id,
                    reviewed_identity=new_identity,
                    reviewed_edge_usage=correction.edge_usage,
                ),
            ]
            continue
        permitted_codes = {correction.old_identity.code, correction.new_identity.code}
        replacements: list[_TickerHistory] = []
        for replacement in correction.replacement_intervals:
            if replacement.start_inclusive is None:
                message = (
                    f"membership correction {correction.correction_id} replacement "
                    "requires an explicit inclusive start"
                )
                raise EODHDMembershipMaterializationError(message)
            if replacement.code not in permitted_codes:
                message = (
                    f"membership correction {correction.correction_id} replacement code "
                    "is absent from its reviewed old/new identities"
                )
                raise EODHDMembershipMaterializationError(message)
            replacements.append(
                _TickerHistory(
                    code=replacement.code,
                    name=replacement.name,
                    start=replacement.start_inclusive,
                    end=replacement.end_exclusive,
                    is_active_now=replacement.is_active_now,
                    is_delisted=replacement.is_delisted,
                    authority_correction_id=correction.correction_id,
                    reviewed_listing_date=(
                        correction.reviewed_listing_date
                        if replacement.code == correction.new_identity.code
                        and replacement.start_inclusive == correction.reviewed_listing_date
                        else None
                    ),
                    reviewed_identity=_reviewed_replacement_identity(
                        correction,
                        replacement,
                    ),
                )
            )
        corrected[matches[0] : matches[0] + 1] = replacements
    return tuple(corrected)


def _acquire_membership_inputs(
    provider: EODHDMembershipProvider,
    *,
    index_symbol: str,
    evidence_start: date,
    requested_start: date,
    requested_end: date,
    correction_evidence: FrozenMembershipCorrectionEvidence | None,
) -> _AcquiredMembershipInputs:
    ticker_evidence, snapshot_evidence = provider.fetch_index_membership_history(
        index_symbol=index_symbol,
        start=evidence_start,
        end=requested_end,
    )
    snapshots = _parse_snapshots(
        snapshot_evidence,
        evidence_start=evidence_start,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    histories = _apply_reviewed_authority_corrections(
        _parse_ticker_history(ticker_evidence),
        correction_evidence,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    _validate_ticker_history_intervals(histories)
    intervals = _build_intervals(
        histories,
        snapshots,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    active_evidence = provider.fetch_us_symbol_directory(delisted=False)
    delisted_evidence = provider.fetch_us_symbol_directory(delisted=True)
    symbol_change_evidence = provider.fetch_us_symbol_change_history(
        start=evidence_start,
        end=requested_end,
    )
    return _AcquiredMembershipInputs(
        ticker_evidence=ticker_evidence,
        snapshot_evidence=snapshot_evidence,
        active_evidence=active_evidence,
        delisted_evidence=delisted_evidence,
        symbol_change_evidence=symbol_change_evidence,
        snapshots=snapshots,
        histories=histories,
        intervals=intervals,
        active_rows=_parse_directory(active_evidence, delisted=False),
        delisted_rows=_parse_directory(delisted_evidence, delisted=True),
        symbol_changes=_parse_symbol_changes(symbol_change_evidence),
        crosscheck_discrepancies=_crosscheck_snapshots(snapshots, histories),
        correction_evidence=correction_evidence,
    )


def _store_acquired_source_evidence(
    output_root: Path,
    acquired: _AcquiredMembershipInputs,
    *,
    index_symbol: str,
    dataset_version: str,
    entitlement_scope: str,
) -> tuple[ContentAddressedArtifact, ...]:
    specifications = (
        (
            acquired.ticker_evidence,
            "eodhd_index_historical_ticker_components_response",
            len(acquired.histories),
            index_symbol,
        ),
        (
            acquired.snapshot_evidence,
            "eodhd_index_historical_components_response",
            len(acquired.snapshots),
            index_symbol,
        ),
        (
            acquired.active_evidence,
            "eodhd_active_symbol_directory_response",
            len(acquired.active_rows),
            None,
        ),
        (
            acquired.delisted_evidence,
            "eodhd_delisted_symbol_directory_response",
            len(acquired.delisted_rows),
            None,
        ),
        (
            acquired.symbol_change_evidence,
            "eodhd_us_symbol_change_history_response",
            len(acquired.symbol_changes),
            None,
        ),
    )
    return tuple(
        _store_evidence(
            output_root,
            evidence,
            role=role,
            dataset_version=dataset_version,
            entitlement_scope=entitlement_scope,
            row_count=row_count,
            requested_symbol=requested_symbol,
        )
        for evidence, role, row_count, requested_symbol in specifications
    )


def _crosscheck_discrepancy_payload(
    discrepancy: _MembershipCrosscheckDiscrepancy,
) -> dict[str, object]:
    return {
        "code": discrepancy.code,
        "disposition": discrepancy.disposition.value,
        "effective_date": discrepancy.effective_date.isoformat(),
        "snapshot_name": discrepancy.snapshot_name,
        "ticker_ends_exclusive": [
            value.isoformat() if value is not None else None
            for value in discrepancy.ticker_ends_exclusive
        ],
        "ticker_names": list(discrepancy.ticker_names),
        "ticker_starts_inclusive": [
            value.isoformat() if value is not None else None for value in discrepancy.ticker_starts
        ],
    }


def _store_membership_authority_crosscheck(
    output_root: Path,
    acquired: _AcquiredMembershipInputs,
    *,
    ticker_artifact: ContentAddressedArtifact,
    snapshot_artifact: ContentAddressedArtifact,
) -> ContentAddressedArtifact:
    unresolved_dispositions = {
        _MembershipCrosscheckDisposition.SNAPSHOT_ONLY_UNRESOLVED,
        _MembershipCrosscheckDisposition.CODE_NAME_UNRESOLVED,
    }
    unresolved_codes = sorted(
        {
            discrepancy.code
            for discrepancy in acquired.crosscheck_discrepancies
            if discrepancy.disposition in unresolved_dispositions
        }
    )
    correction_evidence = acquired.correction_evidence
    correction_evidence_artifact_sha256s = (
        sorted(artifact.sha256 for artifact in correction_evidence.artifacts)
        if correction_evidence is not None
        else []
    )
    payload = {
        "authority": "HistoricalTickerComponents",
        "correction_evidence_manifest_sha256": (
            correction_evidence.manifest_sha256 if correction_evidence is not None else None
        ),
        "correction_ids": (
            [item.correction_id for item in correction_evidence.spec.corrections]
            if correction_evidence is not None
            else []
        ),
        "correction_evidence_artifact_sha256s": correction_evidence_artifact_sha256s,
        "discrepancies": [
            _crosscheck_discrepancy_payload(discrepancy)
            for discrepancy in acquired.crosscheck_discrepancies
        ],
        "end_date_semantics": "exclusive",
        "historical_components_role": "full_state_checkpoint_crosscheck_only",
        "policy_version": _POLICY_VERSION,
        "schema": "vynmatrix.eodhd-membership-authority-crosscheck.v1",
        "source_documentation": _TICKER_COMPONENTS_DOCUMENTATION,
        "start_date_semantics": "inclusive",
        "unresolved_membership_crosscheck_codes": unresolved_codes,
    }
    return store_content_object(
        output_root,
        canonical_json_bytes(payload) + b"\n",
        suffix=".json",
        role="eodhd_membership_authority_crosscheck",
        media_type=_JSON_MEDIA_TYPE,
        row_count=len(acquired.crosscheck_discrepancies),
        context={
            "end_date_semantics": "exclusive",
            "correction_evidence_manifest_sha256": (
                correction_evidence.manifest_sha256 if correction_evidence is not None else None
            ),
            "correction_evidence_artifact_sha256s": (correction_evidence_artifact_sha256s),
            "historical_components_sha256": snapshot_artifact.sha256,
            "historical_ticker_components_sha256": ticker_artifact.sha256,
            "membership_authority_complete": not unresolved_codes,
            "policy_version": _POLICY_VERSION,
            "source_documentation": _TICKER_COMPONENTS_DOCUMENTATION,
            "start_date_semantics": "inclusive",
            "unresolved_membership_crosscheck_codes": unresolved_codes,
        },
    )


def _membership_crosscheck_summary(
    discrepancies: Sequence[_MembershipCrosscheckDiscrepancy],
) -> tuple[dict[str, int], list[str], list[str]]:
    disposition_counts: dict[str, int] = defaultdict(int)
    for discrepancy in discrepancies:
        disposition_counts[discrepancy.disposition.value] += 1
    unresolved_snapshot_only = sorted(
        {
            discrepancy.code
            for discrepancy in discrepancies
            if discrepancy.disposition is _MembershipCrosscheckDisposition.SNAPSHOT_ONLY_UNRESOLVED
        }
    )
    unresolved_all = sorted(
        {
            discrepancy.code
            for discrepancy in discrepancies
            if discrepancy.disposition
            in {
                _MembershipCrosscheckDisposition.SNAPSHOT_ONLY_UNRESOLVED,
                _MembershipCrosscheckDisposition.CODE_NAME_UNRESOLVED,
            }
        }
    )
    return dict(sorted(disposition_counts.items())), unresolved_snapshot_only, unresolved_all


def _membership_finalization_summary(
    *,
    clock: Callable[[], datetime],
    snapshots: Sequence[_ComponentSnapshot],
    discrepancies: Sequence[_MembershipCrosscheckDiscrepancy],
) -> tuple[str, int, dict[str, int], list[str], list[str]]:
    finalized_at = clock()
    if finalized_at.tzinfo is None or finalized_at.utcoffset() is None:
        message = "membership materialization clock must return an aware datetime"
        raise EODHDMembershipMaterializationError(message)
    change_event_gaps = [
        (current.effective_date - previous.effective_date).days
        for previous, current in pairwise(snapshots)
    ]
    disposition_counts, unresolved_snapshot_only, unresolved_all = _membership_crosscheck_summary(
        discrepancies
    )
    return (
        finalized_at.astimezone(UTC).isoformat(),
        max(change_event_gaps, default=0),
        disposition_counts,
        unresolved_snapshot_only,
        unresolved_all,
    )


def _provider_alias_classifications(
    resolutions: Mapping[str, _IdentityResolution],
) -> tuple[list[str], list[str], list[str]]:
    provider_alias_codes = sorted(
        {
            resolution.code
            for resolution in resolutions.values()
            if resolution.provider_symbol != resolution.code
        }
    )
    source_backed = sorted(
        {
            resolution.code
            for resolution in resolutions.values()
            if resolution.directory_disposition.startswith(
                "eodhd_symbol_change_history_successor_storage:"
            )
        }
    )
    inferred = sorted(set(provider_alias_codes) - set(source_backed))
    return provider_alias_codes, source_backed, inferred


def _correction_materialization_state(
    acquired: _AcquiredMembershipInputs,
) -> tuple[str | None, tuple[ContentAddressedArtifact, ...]]:
    if acquired.correction_evidence is None:
        return None, ()
    return acquired.correction_evidence.manifest_sha256, acquired.correction_evidence.artifacts


def _store_reviewed_identity_edges(
    output_root: Path,
    correction_evidence: FrozenMembershipCorrectionEvidence | None,
) -> ContentAddressedArtifact | None:
    if correction_evidence is None:
        return None
    identity_corrections = tuple(
        correction
        for correction in correction_evidence.spec.corrections
        if correction.action is MembershipCorrectionAction.IDENTITY_BRIDGE_ONLY
    )
    if not identity_corrections:
        return None
    artifacts_by_source_id = {
        str(artifact.context["source_id"]): artifact
        for artifact in correction_evidence.artifacts
        if isinstance(artifact.context.get("source_id"), str)
    }
    sources_by_id = {source.source_id: source for source in correction_evidence.spec.sources}
    rows: list[dict[str, object]] = []
    for correction in identity_corrections:
        expected = correction.expected_ticker_interval
        if expected is None or correction.edge_usage is None:
            message = f"reviewed identity edge {correction.correction_id} is incomplete"
            raise EODHDMembershipMaterializationError(message)
        source_rows: list[dict[str, object]] = []
        for source_id in correction.source_ids:
            source = sources_by_id[source_id]
            artifact = artifacts_by_source_id[source_id]
            source_rows.append(
                {
                    "artifact_sha256": artifact.sha256,
                    "document_date": source.document_date.isoformat(),
                    "kind": source.kind.value,
                    "retrieved_at": artifact.context["retrieved_at"],
                    "source_id": source_id,
                    "url": source.url,
                }
            )
        rows.append(
            {
                "bridge_kind": correction.bridge_kind.value,
                "correction_id": correction.correction_id,
                "edge_usage": correction.edge_usage.value,
                "effective_date": correction.effective_date.isoformat(),
                "expected_raw_ticker_interval": {
                    "code": expected.code,
                    "end_exclusive": (
                        expected.end_exclusive.isoformat()
                        if expected.end_exclusive is not None
                        else None
                    ),
                    "is_active_now": expected.is_active_now,
                    "is_delisted": expected.is_delisted,
                    "name": expected.name,
                    "start_inclusive": (
                        expected.start_inclusive.isoformat()
                        if expected.start_inclusive is not None
                        else None
                    ),
                },
                "membership_coverage_changed": False,
                "new_identity": _reviewed_identity_payload(correction.new_identity),
                "old_identity": _reviewed_identity_payload(correction.old_identity),
                "price_stitch_allowed": False,
                "rationale": correction.rationale,
                "sources": source_rows,
            }
        )
    payload = {
        "correction_evidence_manifest_sha256": correction_evidence.manifest_sha256,
        "edges": rows,
        "historical_publication_availability_complete": False,
        "schema": "vynmatrix.eodhd-membership-reviewed-identity-edges.v1",
    }
    return store_content_object(
        output_root,
        canonical_json_bytes(payload) + b"\n",
        suffix=".json",
        role="eodhd_membership_reviewed_identity_edges",
        media_type=_JSON_MEDIA_TYPE,
        row_count=len(rows),
        context={
            "correction_evidence_manifest_sha256": correction_evidence.manifest_sha256,
            "historical_publication_availability_complete": False,
            "membership_coverage_changed": False,
            "price_stitch_allowed": False,
        },
    )


def _cached_general_fundamentals(
    output_root: Path,
    *,
    provider: EODHDMembershipProvider,
    provider_symbol: str,
    dataset_version: str,
    entitlement_scope: str,
    cache: dict[str, tuple[_GeneralFundamentals, ContentAddressedArtifact]],
    artifacts: list[ContentAddressedArtifact],
) -> tuple[_GeneralFundamentals, ContentAddressedArtifact]:
    cached = cache.get(provider_symbol)
    if cached is not None:
        return cached
    evidence = provider.fetch_security_fundamentals_general(provider_symbol=provider_symbol)
    general = _parse_general_fundamentals(evidence, provider_symbol=provider_symbol)
    artifact = _store_evidence(
        output_root,
        evidence,
        role=_GENERAL_FUNDAMENTALS_ARTIFACT_ROLE,
        dataset_version=dataset_version,
        entitlement_scope=entitlement_scope,
        row_count=(0 if evidence.payload in ({}, None) else 1),
        requested_symbol=f"{provider_symbol}.US",
    )
    result = (general, artifact)
    cache[provider_symbol] = result
    artifacts.append(artifact)
    return result


def _membership_resolution_inputs(
    acquired: _AcquiredMembershipInputs,
) -> tuple[
    dict[str, list[_TickerHistory]],
    dict[str, list[_DirectoryEntry]],
    dict[str, set[str]],
    frozenset[str],
    tuple[_DirectoryEntry, ...],
]:
    history_by_code: dict[str, list[_TickerHistory]] = defaultdict(list)
    for history in acquired.histories:
        history_by_code[history.code].append(history)
    directory_by_code: dict[str, list[_DirectoryEntry]] = defaultdict(list)
    for entry in (*acquired.active_rows, *acquired.delisted_rows):
        directory_by_code[entry.code].append(entry)
    names_by_code: dict[str, set[str]] = defaultdict(set)
    for interval in acquired.intervals:
        names_by_code[interval.code].update(interval.names)
    return (
        history_by_code,
        directory_by_code,
        names_by_code,
        frozenset(names_by_code),
        (*acquired.active_rows, *acquired.delisted_rows),
    )


def _membership_rows(
    *,
    acquired: _AcquiredMembershipInputs,
    resolutions: Mapping[str, _IdentityResolution],
    history_by_code: Mapping[str, Sequence[_TickerHistory]],
    membership_source_ref: str,
    disagreement_codes: set[str],
    membership_revision: str,
    index_symbol: str,
) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    correction_manifest_sha256 = (
        acquired.correction_evidence.manifest_sha256
        if acquired.correction_evidence is not None
        else None
    )
    for interval in acquired.intervals:
        if interval.reviewed_listing_date is not None and correction_manifest_sha256 is None:
            message = "reviewed listing date lacks frozen correction-manifest provenance"
            raise EODHDMembershipMaterializationError(message)
        resolution = resolutions[_interval_resolution_key(interval)]
        histories_for_code = history_by_code[interval.code]
        reviewed_identity = interval.reviewed_identity
        is_active_now = (
            reviewed_identity.is_active_now
            if reviewed_identity is not None
            else any(history.is_active_now for history in histories_for_code)
        )
        is_delisted = (
            reviewed_identity.is_delisted
            if reviewed_identity is not None
            else all(history.is_delisted for history in histories_for_code)
        )
        assert is_active_now is not None
        assert is_delisted is not None
        listing_identity_binding = (
            "reviewed_primary_class_identity"
            if interval.reviewed_listing_date is not None and reviewed_identity is not None
            else resolution.general_identity_binding
        )
        reviewed_listing_date_text = (
            interval.reviewed_listing_date.isoformat()
            if interval.reviewed_listing_date is not None
            else "null"
        )
        figi: str | None
        isin: str | None
        cusip: str | None
        cik: str | None
        if reviewed_identity is not None:
            figi = (
                reviewed_identity.security_id.removeprefix("figi:")
                if reviewed_identity.security_id.startswith("figi:")
                else None
            )
            isin = (
                reviewed_identity.security_id.removeprefix("isin:")
                if reviewed_identity.security_id.startswith("isin:")
                else None
            )
            cusip = None
            cik = reviewed_identity.cik
            listing_ipo_date = (
                resolution.general.ipo_date if interval.reviewed_listing_date is not None else None
            )
        else:
            figi = _unique_identifier(resolution.mapping, "figi")
            isin = (
                resolution.directory_entry.isin
                if resolution.directory_entry is not None
                and resolution.directory_entry.isin is not None
                else _unique_identifier(resolution.mapping, "isin")
            )
            cusip = _unique_identifier(resolution.mapping, "cusip")
            cik = _unique_identifier(resolution.mapping, "cik")
            listing_ipo_date = resolution.general.ipo_date
        notes = (
            f"component_names={list(interval.names)!r};"
            f"is_active_now={is_active_now};is_delisted={is_delisted};"
            f"directory_disposition={resolution.directory_disposition};"
            f"identity_source={resolution.security_id_source};"
            "snapshot_checkpoint_disagrees="
            f"{str(interval.code in disagreement_codes).lower()};"
            f"authority_correction_id={interval.authority_correction_id or 'none'};"
            f"general_identity_binding={resolution.general_identity_binding};"
            "general_ipo_date="
            f"{listing_ipo_date.isoformat() if listing_ipo_date else 'null'};"
            f"reviewed_listing_date={reviewed_listing_date_text};"
            "reviewed_identity_split="
            f"{str(reviewed_identity is not None).lower()};"
            "price_stitch_allowed=false"
        )
        interval_source_ref = (
            f"{membership_source_ref};identity_resolution={resolution.resolution_id};"
            f"authority_correction_id={interval.authority_correction_id or 'none'};"
            f"{resolution.general_artifact.context['endpoint']}"
            f"#sha256={resolution.general_artifact.sha256}"
        )
        rows.append(
            (
                resolution.security_id,
                interval.code,
                interval.effective_from.isoformat(),
                interval.effective_to.isoformat(),
                interval_source_ref,
                notes,
                _SOURCE_KIND,
                _PROVIDER,
                f"{index_symbol} HistoricalTickerComponents",
                membership_revision,
                resolution.provider_symbol,
                "; ".join(interval.names),
                str(is_active_now).lower(),
                str(is_delisted).lower(),
                resolution.security_id_source,
                str(resolution.security_id_permanent).lower(),
                figi or "",
                isin or "",
                cusip or "",
                cik or "",
                listing_ipo_date.isoformat() if listing_ipo_date else "",
                (
                    "bound"
                    if listing_ipo_date is not None or interval.reviewed_listing_date is not None
                    else "unavailable"
                ),
                _PROVIDER,
                resolution.provider_symbol,
                _GENERAL_FUNDAMENTALS_ARTIFACT_ROLE,
                resolution.general_artifact.sha256,
                resolution.general_artifact.context["endpoint"],
                resolution.general_artifact.context["retrieved_at"],
                listing_identity_binding,
                resolution.general.code or "",
                resolution.general.isin or "",
                resolution.general.cik or "",
                (
                    interval.reviewed_listing_date.isoformat()
                    if interval.reviewed_listing_date
                    else ""
                ),
                interval.authority_correction_id if interval.reviewed_listing_date else "",
                correction_manifest_sha256 if interval.reviewed_listing_date else "",
            )
        )
    return rows


def materialize_eodhd_index_membership(  # noqa: PLR0915 - atomic evidence materialization
    output_root: Path,
    *,
    provider: EODHDMembershipProvider,
    index_symbol: str,
    evidence_start: date,
    requested_start: date,
    requested_end: date,
    dataset_version: str,
    entitlement_scope: str,
    entitlement_owner_user_id: str,
    authority_correction_manifest_path: Path | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EODHDMembershipMaterializationResult:
    """Acquire licensed snapshots and bind every in-period member to source evidence."""

    (
        normalized_index,
        normalized_dataset_version,
        normalized_entitlement,
        normalized_owner,
    ) = validate_eodhd_membership_materialization_request(
        index_symbol=index_symbol,
        evidence_start=evidence_start,
        requested_start=requested_start,
        requested_end=requested_end,
        dataset_version=dataset_version,
        entitlement_scope=entitlement_scope,
        entitlement_owner_user_id=entitlement_owner_user_id,
    )

    correction_evidence = load_optional_membership_correction_evidence(
        output_root,
        authority_correction_manifest_path,
    )
    acquired = _acquire_membership_inputs(
        provider,
        index_symbol=normalized_index,
        evidence_start=evidence_start,
        requested_start=requested_start,
        requested_end=requested_end,
        correction_evidence=correction_evidence,
    )
    _validate_component_code_recurrence(acquired.intervals)
    (
        history_by_code,
        directory_by_code,
        _names_by_code,
        membership_codes,
        all_directory_rows,
    ) = _membership_resolution_inputs(acquired)
    resolution_targets = _resolution_targets(acquired.intervals)

    (
        ticker_artifact,
        snapshot_artifact,
        active_artifact,
        delisted_artifact,
        symbol_change_artifact,
    ) = _store_acquired_source_evidence(
        output_root,
        acquired,
        index_symbol=normalized_index,
        dataset_version=normalized_dataset_version,
        entitlement_scope=normalized_entitlement,
    )
    crosscheck_artifact = _store_membership_authority_crosscheck(
        output_root,
        acquired,
        ticker_artifact=ticker_artifact,
        snapshot_artifact=snapshot_artifact,
    )
    membership_authority_complete = bool(
        crosscheck_artifact.context["membership_authority_complete"]
    )
    correction_manifest_sha256, correction_artifacts = _correction_materialization_state(acquired)
    identity_edges_artifact = _store_reviewed_identity_edges(
        output_root,
        acquired.correction_evidence,
    )

    mapping_artifacts: list[ContentAddressedArtifact] = []
    general_artifacts: list[ContentAddressedArtifact] = []
    mapping_cache: dict[
        str,
        tuple[tuple[_IdentifierMapping, ...], ContentAddressedArtifact],
    ] = {}
    general_cache: dict[
        str,
        tuple[_GeneralFundamentals, ContentAddressedArtifact],
    ] = {}
    resolutions: dict[str, _IdentityResolution] = {}
    for resolution_key, target in sorted(resolution_targets.items()):
        reviewed_identity = target.reviewed_identity
        if reviewed_identity is None:
            provider_symbol, provider_symbol_disposition = _provider_symbol_for_component(
                code=target.code,
                names=target.names,
                directory_rows=all_directory_rows,
                membership_codes=membership_codes,
                symbol_changes=acquired.symbol_changes,
            )
        else:
            if reviewed_identity.provider_symbol is None:
                message = f"reviewed identity target {resolution_key} lacks provider symbol"
                raise EODHDMembershipMaterializationError(message)
            provider_symbol = reviewed_identity.provider_symbol
            provider_symbol_disposition = "reviewed_primary_identity_edge"
        cached_mapping = mapping_cache.get(provider_symbol)
        if cached_mapping is None:
            mapping_evidence = provider.fetch_id_mapping(provider_symbol=provider_symbol)
            mappings = _parse_mapping(mapping_evidence, code=provider_symbol)
            mapping_artifact = _store_evidence(
                output_root,
                mapping_evidence,
                role="eodhd_id_mapping_response",
                dataset_version=normalized_dataset_version,
                entitlement_scope=normalized_entitlement,
                row_count=len(mappings),
                requested_symbol=f"{provider_symbol}.US",
            )
            mapping_cache[provider_symbol] = (mappings, mapping_artifact)
            mapping_artifacts.append(mapping_artifact)
        else:
            mappings, mapping_artifact = cached_mapping
        general, general_artifact = _cached_general_fundamentals(
            output_root,
            provider=provider,
            provider_symbol=provider_symbol,
            dataset_version=normalized_dataset_version,
            entitlement_scope=normalized_entitlement,
            cache=general_cache,
            artifacts=general_artifacts,
        )
        if reviewed_identity is not None:
            if acquired.correction_evidence is None:
                message = "reviewed identity target lacks frozen correction evidence"
                raise EODHDMembershipMaterializationError(message)
            resolutions[resolution_key] = _resolve_reviewed_identity(
                target=target,
                mappings=mappings,
                mapping_artifact=mapping_artifact,
                general=general,
                general_artifact=general_artifact,
                directory_rows=all_directory_rows,
                correction_evidence=acquired.correction_evidence,
            )
        else:
            provider_symbol_source_ref = (
                f"{acquired.symbol_change_evidence.endpoint}#sha256={symbol_change_artifact.sha256}"
                if provider_symbol_disposition.startswith(
                    "eodhd_symbol_change_history_successor_storage:"
                )
                else None
            )
            resolutions[resolution_key] = _resolve_identity(
                code=target.code,
                provider_symbol=provider_symbol,
                provider_symbol_disposition=provider_symbol_disposition,
                names=target.names,
                directory_rows=directory_by_code.get(provider_symbol, ()),
                histories=history_by_code.get(target.code, ()),
                mappings=mappings,
                mapping_artifact=mapping_artifact,
                general=general,
                general_artifact=general_artifact,
                active_artifact=active_artifact,
                delisted_artifact=delisted_artifact,
                provider_symbol_source_ref=provider_symbol_source_ref,
            )
    _validate_issuer_lifetimes(
        acquired.intervals,
        resolutions,
        resolution_key=_interval_resolution_key,
    )
    _validate_reviewed_correction_identities(acquired, resolutions)
    _validate_security_timelines(acquired.intervals, resolutions)

    general_evidence_by_provider_symbol = {
        provider_symbol: artifact.sha256
        for provider_symbol, (_, artifact) in sorted(general_cache.items())
    }
    general_evidence_set_sha256 = evidence_sha256(general_evidence_by_provider_symbol)
    membership_revision = evidence_sha256(
        {
            "fundamentals_general_by_provider_symbol": general_evidence_by_provider_symbol,
            "historical_components": snapshot_artifact.sha256,
            "historical_ticker_components": ticker_artifact.sha256,
            "index_symbol": normalized_index,
            "membership_authority_crosscheck": crosscheck_artifact.sha256,
            "reviewed_correction_evidence_manifest": correction_manifest_sha256,
            "policy_version": _POLICY_VERSION,
            "symbol_change_history": symbol_change_artifact.sha256,
            "timestamp_semantics": (
                "HistoricalTickerComponents StartDate inclusive and EndDate exclusive; "
                "inclusive CSV effective_to is EndDate minus one calendar day and "
                "downstream official-session intersection selects the last session"
            ),
        }
    )
    membership_source_ref = (
        f"{acquired.ticker_evidence.endpoint}#sha256={ticker_artifact.sha256};"
        f"checkpoint_crosscheck={acquired.snapshot_evidence.endpoint}"
        f"#sha256={snapshot_artifact.sha256};"
        f"authority_crosscheck#sha256={crosscheck_artifact.sha256};"
        f"reviewed_correction_manifest={correction_manifest_sha256 or 'none'}"
    )
    disagreement_codes = {discrepancy.code for discrepancy in acquired.crosscheck_discrepancies}
    membership_rows = _membership_rows(
        acquired=acquired,
        resolutions=resolutions,
        history_by_code=history_by_code,
        membership_source_ref=membership_source_ref,
        disagreement_codes=disagreement_codes,
        membership_revision=membership_revision,
        index_symbol=normalized_index,
    )
    alias_rows = _alias_rows(
        acquired.intervals,
        resolutions,
        history_start=requested_start,
    )
    membership_content = _csv_bytes(
        (
            "security_id",
            "ticker",
            "effective_from",
            "effective_to",
            "source_ref",
            "notes",
            "source_kind",
            "source_provider",
            "source_dataset",
            "source_revision_sha256",
            "provider_symbol",
            "component_name",
            "is_active_now",
            "is_delisted",
            "security_id_source",
            "security_id_permanent",
            "figi",
            "isin",
            "cusip",
            "cik",
            "vendor_reported_ipo_date",
            "listing_evidence_status",
            "listing_evidence_provider",
            "listing_evidence_provider_symbol",
            "listing_evidence_artifact_role",
            "listing_evidence_artifact_sha256",
            "listing_evidence_endpoint",
            "listing_evidence_retrieved_at",
            "listing_identity_binding",
            "listing_general_code",
            "listing_general_isin",
            "listing_general_cik",
            "reviewed_listing_date",
            "reviewed_listing_correction_id",
            "reviewed_listing_correction_manifest_sha256",
        ),
        membership_rows,
    )
    aliases_content = _csv_bytes(
        (
            "security_id",
            "canonical_symbol",
            "provider",
            "provider_symbol",
            "effective_from",
            "effective_to",
            "source_ref",
        ),
        alias_rows,
    )
    permanent_identity_complete = all(
        resolution.security_id_permanent for resolution in resolutions.values()
    )
    unresolved_codes = sorted(
        {
            resolution.code
            for resolution in resolutions.values()
            if not resolution.security_id_permanent
        }
    )
    (
        provider_alias_codes,
        source_backed_symbol_change_alias_codes,
        directory_alias_inference_codes,
    ) = _provider_alias_classifications(
        resolutions,
    )
    resolution_rows = [
        {
            "code": resolution.code,
            "directory_disposition": resolution.directory_disposition,
            "directory_entry": (
                {
                    "delisted": resolution.directory_entry.delisted,
                    "exchange": resolution.directory_entry.exchange,
                    "isin": resolution.directory_entry.isin,
                    "name": resolution.directory_entry.name,
                }
                if resolution.directory_entry is not None
                else None
            ),
            "mapping_identifiers": [
                {
                    "cik": mapping.cik,
                    "cusip": mapping.cusip,
                    "figi": mapping.figi,
                    "isin": mapping.isin,
                    "lei": mapping.lei,
                    "symbol": mapping.symbol,
                }
                for mapping in resolution.mapping
            ],
            "fundamentals_general": _general_fundamentals_payload(resolution.general),
            "fundamentals_general_endpoint": resolution.general_artifact.context["endpoint"],
            "fundamentals_general_identity_binding": resolution.general_identity_binding,
            "fundamentals_general_retrieved_at": resolution.general_artifact.context[
                "retrieved_at"
            ],
            "fundamentals_general_sha256": resolution.general_artifact.sha256,
            "mapping_sha256": resolution.mapping_artifact.sha256,
            "provider_symbol": resolution.provider_symbol,
            "resolution_id": resolution.resolution_id,
            "security_id": resolution.security_id,
            "security_id_permanent": resolution.security_id_permanent,
            "security_id_source": resolution.security_id_source,
            "source_ref": resolution.source_ref,
            "resolution_key": resolution_key,
        }
        for resolution_key, resolution in sorted(resolutions.items())
    ]
    resolution_artifact = store_content_object(
        output_root,
        canonical_json_bytes(
            {
                "policy_version": _POLICY_VERSION,
                "resolutions": resolution_rows,
                "schema": "vynmatrix.eodhd-index-membership-identities.v1",
            }
        )
        + b"\n",
        suffix=".json",
        role="eodhd_membership_identity_resolutions",
        media_type=_JSON_MEDIA_TYPE,
        row_count=len(resolution_rows),
        context={
            "current_id_mapping_has_historical_validity": False,
            "fundamentals_general_evidence_set_sha256": general_evidence_set_sha256,
            "permanent_identity_complete": permanent_identity_complete,
            "policy_version": _POLICY_VERSION,
            "unresolved_codes": unresolved_codes,
        },
    )
    membership_artifact = store_content_object(
        output_root,
        membership_content,
        suffix=".csv",
        role="licensed_point_in_time_membership",
        media_type=_CSV_MEDIA_TYPE,
        row_count=len(membership_rows),
        context={
            "fundamentals_general_evidence_set_sha256": general_evidence_set_sha256,
            "membership_authority_complete": membership_authority_complete,
            "membership_authority_crosscheck_sha256": crosscheck_artifact.sha256,
            "reviewed_correction_evidence_manifest_sha256": correction_manifest_sha256,
            "reviewed_identity_edges_sha256": (
                identity_edges_artifact.sha256 if identity_edges_artifact else None
            ),
            "membership_revision_sha256": membership_revision,
            "permanent_identity_complete": permanent_identity_complete,
            "policy_version": _POLICY_VERSION,
            "snapshot_sha256": snapshot_artifact.sha256,
            "symbol_change_history_sha256": symbol_change_artifact.sha256,
            "ticker_history_is_membership_authority": True,
            "ticker_history_sha256": ticker_artifact.sha256,
        },
    )
    aliases_artifact = store_content_object(
        output_root,
        aliases_content,
        suffix=".csv",
        role="licensed_historical_aliases",
        media_type=_CSV_MEDIA_TYPE,
        row_count=len(alias_rows),
        context={
            "date_semantics": (
                "membership-period symbols use half-open ticker-history intervals; required "
                "pre-membership factor history uses the nearest source-resolved "
                "security timeline and is not vendor-published alias validity"
            ),
            "membership_authority_complete": membership_authority_complete,
            "permanent_identity_complete": permanent_identity_complete,
            "provider": _PROVIDER,
            "fundamentals_general_evidence_set_sha256": general_evidence_set_sha256,
            "resolution_sha256": resolution_artifact.sha256,
            "reviewed_identity_edges_sha256": (
                identity_edges_artifact.sha256 if identity_edges_artifact else None
            ),
            "symbol_change_history_sha256": symbol_change_artifact.sha256,
        },
    )
    (
        finalized_at,
        maximum_change_event_gap_days,
        discrepancy_disposition_counts,
        unresolved_snapshot_only_codes,
        unresolved_membership_crosscheck_codes,
    ) = _membership_finalization_summary(
        clock=clock,
        snapshots=acquired.snapshots,
        discrepancies=acquired.crosscheck_discrepancies,
    )
    lineage: dict[str, object] = {
        "membership_authority_complete": membership_authority_complete,
        "membership_authority_crosscheck_sha256": crosscheck_artifact.sha256,
        "reviewed_correction_evidence_manifest_sha256": correction_manifest_sha256,
        "reviewed_correction_count": (
            len(acquired.correction_evidence.spec.corrections)
            if acquired.correction_evidence is not None
            else 0
        ),
        "reviewed_identity_edges_sha256": (
            identity_edges_artifact.sha256 if identity_edges_artifact else None
        ),
        "historical_membership_publication_availability_complete": False,
        "reviewed_correction_artifact_sha256s": sorted(
            artifact.sha256 for artifact in correction_artifacts
        ),
        "membership_crosscheck_disposition_counts": discrepancy_disposition_counts,
        "membership_crosscheck_observation_count": len(acquired.crosscheck_discrepancies),
        "ticker_history_is_membership_authority": True,
        "ticker_history_end_date_semantics": "exclusive",
        "ticker_history_source_documentation": _TICKER_COMPONENTS_DOCUMENTATION,
        "ticker_history_start_date_semantics": "inclusive",
        "unresolved_membership_crosscheck_codes": (unresolved_membership_crosscheck_codes),
        "unresolved_snapshot_only_codes": unresolved_snapshot_only_codes,
        "credential_persisted": False,
        "dataset_version": normalized_dataset_version,
        "entitlement_scope": normalized_entitlement,
        "entitlement_owner_user_id": normalized_owner,
        "evidence_from": evidence_start.isoformat(),
        "finalized_at": finalized_at,
        "historical_components_are_change_event_snapshots": True,
        "membership_period_provider_symbols_directly_observed": not provider_alias_codes,
        "pre_membership_alias_validity_published_by_vendor": False,
        "historical_ticker_missing_start_count": sum(
            history.start is None for history in acquired.histories
        ),
        "identity_policy_version": _POLICY_VERSION,
        "index_symbol": normalized_index,
        "interval_count": len(acquired.intervals),
        "fundamentals_general_evidence_by_provider_symbol": (general_evidence_by_provider_symbol),
        "fundamentals_general_evidence_set_sha256": general_evidence_set_sha256,
        "fundamentals_general_ipo_date_count": sum(
            general.ipo_date is not None for general, _artifact in general_cache.values()
        ),
        "fundamentals_general_response_count": len(general_artifacts),
        "mapping_response_count": len(mapping_artifacts),
        "maximum_change_event_gap_days": maximum_change_event_gap_days,
        "membership_revision_sha256": membership_revision,
        "permanent_identity_complete": permanent_identity_complete,
        "directory_alias_inference_codes": directory_alias_inference_codes,
        "requested_from": requested_start.isoformat(),
        "requested_to": requested_end.isoformat(),
        "security_count": len(resolutions),
        "snapshot_count": len(acquired.snapshots),
        "snapshot_first_effective_date": acquired.snapshots[0].effective_date.isoformat(),
        "snapshot_last_effective_date": acquired.snapshots[-1].effective_date.isoformat(),
        "source_backed_symbol_change_alias_codes": source_backed_symbol_change_alias_codes,
        "symbol_change_history_count": len(acquired.symbol_changes),
        "symbol_change_noncanonical_symbol_count": sum(
            any(character.isspace() for character in change.old_symbol)
            or any(character.isspace() for character in change.new_symbol)
            for change in acquired.symbol_changes
        ),
        "symbol_change_history_documented_coverage_from": (
            _SYMBOL_CHANGE_HISTORY_START.isoformat()
        ),
        "symbol_change_history_sha256": symbol_change_artifact.sha256,
        "unresolved_identity_codes": unresolved_codes,
    }
    evidence_artifacts = (
        ticker_artifact,
        snapshot_artifact,
        crosscheck_artifact,
        *correction_artifacts,
        *([identity_edges_artifact] if identity_edges_artifact is not None else []),
        active_artifact,
        delisted_artifact,
        symbol_change_artifact,
        resolution_artifact,
        *mapping_artifacts,
        *general_artifacts,
    )
    return freeze_eodhd_membership_materialization(
        output_root,
        membership_artifact=membership_artifact,
        aliases_artifact=aliases_artifact,
        identity_edges_artifact=identity_edges_artifact,
        evidence_artifacts=tuple(evidence_artifacts),
        lineage=lineage,
        dataset_version=normalized_dataset_version,
        entitlement_scope=normalized_entitlement,
        entitlement_owner_user_id=normalized_owner,
        index_symbol=normalized_index,
        evidence_start=evidence_start,
        requested_start=requested_start,
        requested_end=requested_end,
        interval_count=len(acquired.intervals),
        security_count=len(resolutions),
        permanent_identity_complete=permanent_identity_complete,
        membership_authority_complete=membership_authority_complete,
    )


__all__ = [
    "EODHDMembershipMaterializationError",
    "EODHDMembershipMaterializationResult",
    "EODHDMembershipProvider",
    "load_frozen_eodhd_membership_materialization",
    "materialize_eodhd_index_membership",
]
