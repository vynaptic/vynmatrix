"""Frozen EODHD membership materialization bundle and resume validation.

The licensed membership acquisition is quota-consuming.  This module freezes
its complete output graph once and reconstructs the exact snapshot inputs only
after re-verifying every content object and every scope-defining field.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, Protocol

from dev_cli.validation.backtest.equity_membership_corrections import (
    FrozenMembershipCorrectionEvidence,
    MembershipCorrectionError,
    load_frozen_membership_correction_evidence,
)
from dev_cli.validation.backtest.equity_membership_eodhd_source import (
    EODHDMembershipMaterializationError,
)
from dev_cli.validation.evidence import (
    ContentAddressedArtifact,
    content_manifest_artifacts,
    evidence_sha256,
    file_sha256,
    load_content_addressed_manifest,
    nonblank_string,
    parse_utc_datetime,
    sha256_digest,
    store_content_object,
    verified_content_path,
    write_content_addressed_manifest,
)
from dev_cli.validation.providers.eodhd import EODHDJsonEvidence

EODHD_MEMBERSHIP_MATERIALIZATION_MANIFEST_SCHEMA: Final = (
    "vynmatrix.eodhd-membership-materialization.v1"
)

_PROVIDER: Final = "eodhd"
_SOURCE_KIND: Final = "licensed_point_in_time"
_MEMBERSHIP_ROLE: Final = "licensed_point_in_time_membership"
_ALIASES_ROLE: Final = "licensed_historical_aliases"
_IDENTITY_EDGES_ROLE: Final = "eodhd_membership_reviewed_identity_edges"
_MANIFEST_EVIDENCE_ROLE: Final = "eodhd_membership_materialization_manifest"
_CORRECTION_MANIFEST_ROLE: Final = "eodhd_membership_correction_evidence_manifest"
_CORRECTION_SPEC_ROLE: Final = "eodhd_membership_correction_source_spec"
_CORRECTION_SOURCE_ROLE: Final = "eodhd_membership_primary_source_document"
_MAPPING_ROLE: Final = "eodhd_id_mapping_response"
_GENERAL_ROLE: Final = "eodhd_security_fundamentals_general_response"
_RESOLUTION_ROLE: Final = "eodhd_membership_identity_resolutions"
_CROSSCHECK_ROLE: Final = "eodhd_membership_authority_crosscheck"

_REQUIRED_SINGLETON_EVIDENCE_ROLES: Final = frozenset(
    {
        "eodhd_index_historical_ticker_components_response",
        "eodhd_index_historical_components_response",
        "eodhd_active_symbol_directory_response",
        "eodhd_delisted_symbol_directory_response",
        "eodhd_us_symbol_change_history_response",
        _CROSSCHECK_ROLE,
        _RESOLUTION_ROLE,
    }
)
_REPEATABLE_EVIDENCE_ROLES: Final = frozenset({_MAPPING_ROLE, _GENERAL_ROLE})
_OPTIONAL_EVIDENCE_ROLES: Final = frozenset(
    {
        _IDENTITY_EDGES_ROLE,
        _CORRECTION_MANIFEST_ROLE,
        _CORRECTION_SPEC_ROLE,
        _CORRECTION_SOURCE_ROLE,
    }
)
_ALLOWED_EVIDENCE_ROLES: Final = (
    _REQUIRED_SINGLETON_EVIDENCE_ROLES | _REPEATABLE_EVIDENCE_ROLES | _OPTIONAL_EVIDENCE_ROLES
)
_DATASET_BOUND_EVIDENCE_ROLES: Final = frozenset(
    {
        "eodhd_index_historical_ticker_components_response",
        "eodhd_index_historical_components_response",
        "eodhd_active_symbol_directory_response",
        "eodhd_delisted_symbol_directory_response",
        "eodhd_us_symbol_change_history_response",
        _MAPPING_ROLE,
        _GENERAL_ROLE,
    }
)
_MANIFEST_KEYS: Final = frozenset(
    {
        "artifacts",
        "bindings",
        "completion",
        "counts",
        "dataset_version",
        "entitlement_owner_user_id",
        "entitlement_scope",
        "index_symbol",
        "lineage",
        "provider",
        "schema",
        "source_kind",
        "window",
    }
)


class EODHDMembershipProvider(Protocol):
    """Validation-provider methods required by membership materialization."""

    def fetch_index_membership_history(
        self,
        *,
        index_symbol: str,
        start: date,
        end: date,
    ) -> tuple[EODHDJsonEvidence, EODHDJsonEvidence]: ...

    def fetch_us_symbol_directory(self, *, delisted: bool) -> EODHDJsonEvidence: ...

    def fetch_us_symbol_change_history(
        self,
        *,
        start: date,
        end: date,
    ) -> EODHDJsonEvidence: ...

    def fetch_id_mapping(self, *, provider_symbol: str) -> EODHDJsonEvidence: ...

    def fetch_security_fundamentals_general(
        self,
        *,
        provider_symbol: str,
    ) -> EODHDJsonEvidence: ...


@dataclass(frozen=True, slots=True)
class EODHDMembershipMaterializationResult:
    """Snapshot-ready membership graph and its immutable bundle identity."""

    membership_path: Path
    aliases_path: Path
    identity_edges_path: Path | None
    evidence_artifacts: tuple[ContentAddressedArtifact, ...]
    lineage: Mapping[str, object]
    interval_count: int
    security_count: int
    permanent_identity_complete: bool
    membership_authority_complete: bool
    manifest_path: Path
    manifest_sha256: str


def validate_eodhd_membership_materialization_request(
    *,
    index_symbol: str,
    evidence_start: date,
    requested_start: date,
    requested_end: date,
    dataset_version: str,
    entitlement_scope: str,
    entitlement_owner_user_id: str,
) -> tuple[str, str, str, str]:
    """Normalize the immutable dataset identity shared by fetch and resume."""

    if requested_end < requested_start:
        message = "membership requested_end cannot precede requested_start"
        raise EODHDMembershipMaterializationError(message)
    if evidence_start > requested_start:
        message = "membership evidence_start must be on or before requested_start"
        raise EODHDMembershipMaterializationError(message)
    try:
        normalized_index = nonblank_string(index_symbol, field="index_symbol").upper()
        normalized_dataset = nonblank_string(dataset_version, field="dataset_version")
        normalized_entitlement = nonblank_string(
            entitlement_scope,
            field="entitlement_scope",
        )
        normalized_owner = nonblank_string(
            entitlement_owner_user_id,
            field="entitlement_owner_user_id",
        )
    except (TypeError, ValueError) as exc:
        raise EODHDMembershipMaterializationError(str(exc)) from exc
    if not normalized_index.endswith(".INDX") or any(
        character.isspace() for character in normalized_index
    ):
        message = "membership index_symbol must be a .INDX EODHD symbol"
        raise EODHDMembershipMaterializationError(message)
    return normalized_index, normalized_dataset, normalized_entitlement, normalized_owner


def load_optional_membership_correction_evidence(
    output_root: Path,
    manifest_path: Path | None,
) -> FrozenMembershipCorrectionEvidence | None:
    """Load an optional correction graph through its existing strict owner."""

    if manifest_path is None:
        return None
    try:
        return load_frozen_membership_correction_evidence(output_root, manifest_path)
    except MembershipCorrectionError as exc:
        message = f"membership authority correction evidence is invalid: {exc}"
        raise EODHDMembershipMaterializationError(message) from exc


def _exact_mapping(value: object, *, field: str, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        message = f"{field} must be an object"
        raise TypeError(message)
    observed = set(value)
    if observed != keys:
        message = f"{field} keys differ: expected {sorted(keys)}, observed {sorted(observed)}"
        raise ValueError(message)
    return value


def _required_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        message = f"{field} must be a boolean"
        raise TypeError(message)
    return value


def _required_count(value: object, *, field: str, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if positive else "non-negative"
        message = f"{field} must be a {qualifier} integer"
        raise ValueError(message)
    return value


def _required_date(value: object, *, field: str) -> date:
    if not isinstance(value, str):
        message = f"{field} must be an ISO date"
        raise TypeError(message)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        message = f"{field} must be an ISO date"
        raise ValueError(message) from exc
    if parsed.isoformat() != value:
        message = f"{field} must use canonical ISO date notation"
        raise ValueError(message)
    return parsed


def _artifact_identity(artifact: ContentAddressedArtifact) -> str:
    return evidence_sha256(artifact.as_manifest())


def _artifact_binding(artifact: ContentAddressedArtifact) -> dict[str, str]:
    return {
        "descriptor_sha256": _artifact_identity(artifact),
        "path": artifact.path,
        "role": artifact.role,
        "sha256": artifact.sha256,
    }


def _store_manifest_evidence(
    output_root: Path,
    manifest_path: Path,
    *,
    manifest_sha256: str,
    interval_count: int,
) -> ContentAddressedArtifact:
    return store_content_object(
        output_root,
        manifest_path.read_bytes(),
        suffix=".json",
        role=_MANIFEST_EVIDENCE_ROLE,
        media_type="application/json",
        row_count=interval_count,
        context={
            "manifest_sha256": manifest_sha256,
            "schema": EODHD_MEMBERSHIP_MATERIALIZATION_MANIFEST_SCHEMA,
        },
    )


def _load_manifest_evidence(
    output_root: Path,
    manifest_path: Path,
    *,
    interval_count: int,
) -> ContentAddressedArtifact:
    content_sha256 = file_sha256(manifest_path)
    artifact = ContentAddressedArtifact(
        role=_MANIFEST_EVIDENCE_ROLE,
        path=f"objects/{content_sha256[:2]}/{content_sha256}.json",
        sha256=content_sha256,
        media_type="application/json",
        row_count=interval_count,
        context={
            "manifest_sha256": manifest_path.stem,
            "schema": EODHD_MEMBERSHIP_MATERIALIZATION_MANIFEST_SCHEMA,
        },
    )
    try:
        verified_content_path(output_root, artifact)
    except ValueError as exc:
        message = "membership materialization manifest content object is missing or altered"
        raise EODHDMembershipMaterializationError(message) from exc
    return artifact


def _artifact_from_binding(
    value: object,
    *,
    artifacts: Sequence[ContentAddressedArtifact],
    field: str,
) -> ContentAddressedArtifact:
    binding = _exact_mapping(
        value,
        field=field,
        keys=frozenset({"descriptor_sha256", "path", "role", "sha256"}),
    )
    try:
        descriptor_sha256 = sha256_digest(
            binding.get("descriptor_sha256"),
            field=f"{field}.descriptor_sha256",
        )
        role = nonblank_string(binding.get("role"), field=f"{field}.role")
        path = nonblank_string(binding.get("path"), field=f"{field}.path")
        content_sha256 = sha256_digest(binding.get("sha256"), field=f"{field}.sha256")
    except (TypeError, ValueError) as exc:
        raise EODHDMembershipMaterializationError(str(exc)) from exc
    matches = tuple(
        artifact
        for artifact in artifacts
        if _artifact_identity(artifact) == descriptor_sha256
        and artifact.role == role
        and artifact.path == path
        and artifact.sha256 == content_sha256
    )
    if len(matches) != 1:
        message = f"{field} must identify exactly one manifest artifact"
        raise EODHDMembershipMaterializationError(message)
    return matches[0]


def _required_lineage_value(
    lineage: Mapping[str, object],
    field: str,
    expected: object,
) -> None:
    if lineage.get(field) != expected:
        message = f"membership materialization lineage {field} differs"
        raise EODHDMembershipMaterializationError(message)


def _validate_correction_roles(
    *,
    role_counts: Counter[str],
    evidence_artifacts: Sequence[ContentAddressedArtifact],
    lineage: Mapping[str, object],
) -> None:
    correction_count = _required_count(
        lineage.get("reviewed_correction_count"),
        field="lineage.reviewed_correction_count",
    )
    correction_digest = lineage.get("reviewed_correction_evidence_manifest_sha256")
    if correction_count == 0:
        if correction_digest is not None or any(
            role_counts[role]
            for role in (
                _CORRECTION_MANIFEST_ROLE,
                _CORRECTION_SPEC_ROLE,
                _CORRECTION_SOURCE_ROLE,
            )
        ):
            message = "membership materialization has unexpected correction evidence"
            raise EODHDMembershipMaterializationError(message)
        return
    try:
        expected_digest = sha256_digest(
            correction_digest,
            field="lineage.reviewed_correction_evidence_manifest_sha256",
        )
    except (TypeError, ValueError) as exc:
        raise EODHDMembershipMaterializationError(str(exc)) from exc
    if (
        role_counts[_CORRECTION_MANIFEST_ROLE] != 1
        or role_counts[_CORRECTION_SPEC_ROLE] != 1
        or role_counts[_CORRECTION_SOURCE_ROLE] < 1
    ):
        message = "membership materialization correction artifact roles are incomplete"
        raise EODHDMembershipMaterializationError(message)
    manifest_artifact = next(
        artifact for artifact in evidence_artifacts if artifact.role == _CORRECTION_MANIFEST_ROLE
    )
    if manifest_artifact.context.get("manifest_sha256") != expected_digest:
        message = "membership correction manifest identity differs from lineage"
        raise EODHDMembershipMaterializationError(message)


def _validate_artifact_contract(  # noqa: PLR0912, PLR0915 - explicit strict boundary
    *,
    membership_artifact: ContentAddressedArtifact,
    aliases_artifact: ContentAddressedArtifact,
    identity_edges_artifact: ContentAddressedArtifact | None,
    evidence_artifacts: Sequence[ContentAddressedArtifact],
    lineage: Mapping[str, object],
    dataset_version: str,
    entitlement_scope: str,
    entitlement_owner_user_id: str,
    index_symbol: str,
    evidence_start: date,
    requested_start: date,
    requested_end: date,
    interval_count: int,
    security_count: int,
    permanent_identity_complete: bool,
    membership_authority_complete: bool,
) -> dict[str, int]:
    if membership_artifact.role != _MEMBERSHIP_ROLE:
        message = "membership materialization has an invalid membership artifact role"
        raise EODHDMembershipMaterializationError(message)
    if aliases_artifact.role != _ALIASES_ROLE:
        message = "membership materialization has an invalid aliases artifact role"
        raise EODHDMembershipMaterializationError(message)
    all_artifacts = (membership_artifact, aliases_artifact, *evidence_artifacts)
    identities = [_artifact_identity(artifact) for artifact in all_artifacts]
    if len(set(identities)) != len(identities):
        message = "membership materialization contains duplicate artifact identities"
        raise EODHDMembershipMaterializationError(message)

    role_counts = Counter(artifact.role for artifact in evidence_artifacts)
    unknown_roles = sorted(set(role_counts) - _ALLOWED_EVIDENCE_ROLES)
    if unknown_roles:
        message = f"membership materialization has unsupported evidence roles: {unknown_roles}"
        raise EODHDMembershipMaterializationError(message)
    for role in _REQUIRED_SINGLETON_EVIDENCE_ROLES:
        if role_counts[role] != 1:
            message = f"membership materialization requires exactly one {role!r} artifact"
            raise EODHDMembershipMaterializationError(message)
    for role in _REPEATABLE_EVIDENCE_ROLES:
        if role_counts[role] < 1:
            message = f"membership materialization requires at least one {role!r} artifact"
            raise EODHDMembershipMaterializationError(message)

    identity_matches = tuple(
        artifact for artifact in evidence_artifacts if artifact.role == _IDENTITY_EDGES_ROLE
    )
    if identity_edges_artifact is None:
        if identity_matches:
            message = "membership materialization identity-edge binding is missing"
            raise EODHDMembershipMaterializationError(message)
    elif identity_matches != (identity_edges_artifact,):
        message = "membership materialization identity-edge artifact differs"
        raise EODHDMembershipMaterializationError(message)

    for artifact in evidence_artifacts:
        if artifact.role in _DATASET_BOUND_EVIDENCE_ROLES and (
            artifact.context.get("dataset_version") != dataset_version
            or artifact.context.get("entitlement_scope") != entitlement_scope
        ):
            message = f"membership evidence scope differs for role {artifact.role!r}"
            raise EODHDMembershipMaterializationError(message)

    interval_count = _required_count(interval_count, field="interval_count", positive=True)
    security_count = _required_count(security_count, field="security_count", positive=True)
    membership_rows = _required_count(
        membership_artifact.row_count,
        field="membership_artifact.row_count",
        positive=True,
    )
    alias_rows = _required_count(
        aliases_artifact.row_count,
        field="aliases_artifact.row_count",
    )
    identity_edge_count = (
        0
        if identity_edges_artifact is None
        else _required_count(
            identity_edges_artifact.row_count,
            field="identity_edges_artifact.row_count",
            positive=True,
        )
    )
    resolution_artifact = next(
        artifact for artifact in evidence_artifacts if artifact.role == _RESOLUTION_ROLE
    )
    if resolution_artifact.row_count != security_count:
        message = "membership identity-resolution count differs from security_count"
        raise EODHDMembershipMaterializationError(message)

    for field, expected in (
        ("dataset_version", dataset_version),
        ("entitlement_scope", entitlement_scope),
        ("entitlement_owner_user_id", entitlement_owner_user_id),
        ("index_symbol", index_symbol),
        ("evidence_from", evidence_start.isoformat()),
        ("requested_from", requested_start.isoformat()),
        ("requested_to", requested_end.isoformat()),
        ("interval_count", interval_count),
        ("security_count", security_count),
        ("permanent_identity_complete", permanent_identity_complete),
        ("membership_authority_complete", membership_authority_complete),
    ):
        _required_lineage_value(lineage, field, expected)
    publication_complete = _required_bool(
        lineage.get("historical_membership_publication_availability_complete"),
        field="lineage.historical_membership_publication_availability_complete",
    )
    try:
        parse_utc_datetime(
            lineage.get("finalized_at"),
            field="lineage.finalized_at",
            strict_utc=True,
        )
    except (TypeError, ValueError) as exc:
        raise EODHDMembershipMaterializationError(str(exc)) from exc
    if membership_artifact.context.get("permanent_identity_complete") is not (
        permanent_identity_complete
    ) or membership_artifact.context.get("membership_authority_complete") is not (
        membership_authority_complete
    ):
        message = "membership artifact completion differs from the bundle"
        raise EODHDMembershipMaterializationError(message)
    if aliases_artifact.context.get("permanent_identity_complete") is not (
        permanent_identity_complete
    ) or aliases_artifact.context.get("membership_authority_complete") is not (
        membership_authority_complete
    ):
        message = "aliases artifact completion differs from the bundle"
        raise EODHDMembershipMaterializationError(message)
    crosscheck = next(
        artifact for artifact in evidence_artifacts if artifact.role == _CROSSCHECK_ROLE
    )
    if crosscheck.context.get("membership_authority_complete") is not (
        membership_authority_complete
    ):
        message = "membership crosscheck completion differs from the bundle"
        raise EODHDMembershipMaterializationError(message)
    expected_identity_digest = (
        identity_edges_artifact.sha256 if identity_edges_artifact is not None else None
    )
    _required_lineage_value(lineage, "reviewed_identity_edges_sha256", expected_identity_digest)
    if membership_artifact.context.get("reviewed_identity_edges_sha256") != (
        expected_identity_digest
    ) or aliases_artifact.context.get("reviewed_identity_edges_sha256") != (
        expected_identity_digest
    ):
        message = "membership identity-edge digest differs across primary artifacts"
        raise EODHDMembershipMaterializationError(message)
    _validate_correction_roles(
        role_counts=role_counts,
        evidence_artifacts=evidence_artifacts,
        lineage=lineage,
    )
    if role_counts[_MAPPING_ROLE] != lineage.get("mapping_response_count"):
        message = "membership mapping-response count differs from lineage"
        raise EODHDMembershipMaterializationError(message)
    if role_counts[_GENERAL_ROLE] != lineage.get("fundamentals_general_response_count"):
        message = "membership fundamentals-response count differs from lineage"
        raise EODHDMembershipMaterializationError(message)
    return {
        "alias_row_count": alias_rows,
        "evidence_artifact_count": len(evidence_artifacts),
        "identity_edge_count": identity_edge_count,
        "interval_count": interval_count,
        "membership_row_count": membership_rows,
        "security_count": security_count,
        "historical_membership_publication_availability_complete": int(publication_complete),
    }


def freeze_eodhd_membership_materialization(
    output_root: Path,
    *,
    membership_artifact: ContentAddressedArtifact,
    aliases_artifact: ContentAddressedArtifact,
    identity_edges_artifact: ContentAddressedArtifact | None,
    evidence_artifacts: tuple[ContentAddressedArtifact, ...],
    lineage: Mapping[str, object],
    dataset_version: str,
    entitlement_scope: str,
    entitlement_owner_user_id: str,
    index_symbol: str,
    evidence_start: date,
    requested_start: date,
    requested_end: date,
    interval_count: int,
    security_count: int,
    permanent_identity_complete: bool,
    membership_authority_complete: bool,
) -> EODHDMembershipMaterializationResult:
    """Freeze one successful membership acquisition as a resumable graph."""

    normalized_index, normalized_dataset, normalized_entitlement, normalized_owner = (
        validate_eodhd_membership_materialization_request(
            index_symbol=index_symbol,
            evidence_start=evidence_start,
            requested_start=requested_start,
            requested_end=requested_end,
            dataset_version=dataset_version,
            entitlement_scope=entitlement_scope,
            entitlement_owner_user_id=entitlement_owner_user_id,
        )
    )
    counts = _validate_artifact_contract(
        membership_artifact=membership_artifact,
        aliases_artifact=aliases_artifact,
        identity_edges_artifact=identity_edges_artifact,
        evidence_artifacts=evidence_artifacts,
        lineage=lineage,
        dataset_version=normalized_dataset,
        entitlement_scope=normalized_entitlement,
        entitlement_owner_user_id=normalized_owner,
        index_symbol=normalized_index,
        evidence_start=evidence_start,
        requested_start=requested_start,
        requested_end=requested_end,
        interval_count=interval_count,
        security_count=security_count,
        permanent_identity_complete=permanent_identity_complete,
        membership_authority_complete=membership_authority_complete,
    )
    publication_complete = bool(
        counts.pop("historical_membership_publication_availability_complete")
    )
    artifacts = (membership_artifact, aliases_artifact, *evidence_artifacts)
    payload = {
        "artifacts": [artifact.as_manifest() for artifact in artifacts],
        "bindings": {
            "aliases": _artifact_binding(aliases_artifact),
            "evidence": [_artifact_binding(artifact) for artifact in evidence_artifacts],
            "identity_edges": (
                _artifact_binding(identity_edges_artifact)
                if identity_edges_artifact is not None
                else None
            ),
            "membership": _artifact_binding(membership_artifact),
        },
        "completion": {
            "historical_membership_publication_availability_complete": (publication_complete),
            "membership_authority_complete": membership_authority_complete,
            "permanent_identity_complete": permanent_identity_complete,
        },
        "counts": counts,
        "dataset_version": normalized_dataset,
        "entitlement_owner_user_id": normalized_owner,
        "entitlement_scope": normalized_entitlement,
        "index_symbol": normalized_index,
        "lineage": dict(lineage),
        "provider": _PROVIDER,
        "schema": EODHD_MEMBERSHIP_MATERIALIZATION_MANIFEST_SCHEMA,
        "source_kind": _SOURCE_KIND,
        "window": {
            "evidence_start": evidence_start.isoformat(),
            "requested_end": requested_end.isoformat(),
            "requested_start": requested_start.isoformat(),
        },
    }
    manifest_path, manifest_sha256 = write_content_addressed_manifest(output_root, payload)
    _store_manifest_evidence(
        output_root,
        manifest_path,
        manifest_sha256=manifest_sha256,
        interval_count=interval_count,
    )
    return load_frozen_eodhd_membership_materialization(
        output_root,
        manifest_path,
        expected_dataset_version=normalized_dataset,
        expected_entitlement_scope=normalized_entitlement,
        expected_entitlement_owner_user_id=normalized_owner,
        expected_index_symbol=normalized_index,
        expected_evidence_start=evidence_start,
        expected_requested_start=requested_start,
        expected_requested_end=requested_end,
    )


@contextmanager
def _materialization_error_boundary() -> Iterator[None]:
    try:
        yield
    except EODHDMembershipMaterializationError:
        raise
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise EODHDMembershipMaterializationError(str(exc)) from exc


def load_frozen_eodhd_membership_materialization(
    output_root: Path,
    manifest_path: Path,
    *,
    expected_dataset_version: str,
    expected_entitlement_scope: str,
    expected_entitlement_owner_user_id: str,
    expected_index_symbol: str,
    expected_evidence_start: date,
    expected_requested_start: date,
    expected_requested_end: date,
) -> EODHDMembershipMaterializationResult:
    """Verify and load one exact frozen membership graph without provider I/O."""

    normalized_index, normalized_dataset, normalized_entitlement, normalized_owner = (
        validate_eodhd_membership_materialization_request(
            index_symbol=expected_index_symbol,
            evidence_start=expected_evidence_start,
            requested_start=expected_requested_start,
            requested_end=expected_requested_end,
            dataset_version=expected_dataset_version,
            entitlement_scope=expected_entitlement_scope,
            entitlement_owner_user_id=expected_entitlement_owner_user_id,
        )
    )
    with _materialization_error_boundary():
        manifest = load_content_addressed_manifest(
            output_root,
            manifest_path,
            schema=EODHD_MEMBERSHIP_MATERIALIZATION_MANIFEST_SCHEMA,
        )
        if set(manifest) != _MANIFEST_KEYS:
            message = "membership materialization manifest keys differ"
            raise ValueError(message)
        if manifest.get("provider") != _PROVIDER or manifest.get("source_kind") != _SOURCE_KIND:
            message = "membership materialization provider identity differs"
            raise ValueError(message)
        expected_identity = (
            ("dataset_version", normalized_dataset),
            ("entitlement_scope", normalized_entitlement),
            ("entitlement_owner_user_id", normalized_owner),
            ("index_symbol", normalized_index),
        )
        for field, expected in expected_identity:
            if manifest.get(field) != expected:
                message = f"membership materialization {field} differs"
                raise ValueError(message)
        window = _exact_mapping(
            manifest.get("window"),
            field="window",
            keys=frozenset({"evidence_start", "requested_end", "requested_start"}),
        )
        observed_window = (
            _required_date(window.get("evidence_start"), field="window.evidence_start"),
            _required_date(window.get("requested_start"), field="window.requested_start"),
            _required_date(window.get("requested_end"), field="window.requested_end"),
        )
        expected_window = (
            expected_evidence_start,
            expected_requested_start,
            expected_requested_end,
        )
        if observed_window != expected_window:
            message = "membership materialization requested window differs"
            raise ValueError(message)

        artifacts = content_manifest_artifacts(manifest)
        bindings = _exact_mapping(
            manifest.get("bindings"),
            field="bindings",
            keys=frozenset({"aliases", "evidence", "identity_edges", "membership"}),
        )
        membership_artifact = _artifact_from_binding(
            bindings.get("membership"),
            artifacts=artifacts,
            field="bindings.membership",
        )
        aliases_artifact = _artifact_from_binding(
            bindings.get("aliases"),
            artifacts=artifacts,
            field="bindings.aliases",
        )
        raw_evidence = bindings.get("evidence")
        if not isinstance(raw_evidence, list):
            message = "bindings.evidence must be an array"
            raise TypeError(message)
        evidence_artifacts = tuple(
            _artifact_from_binding(
                value,
                artifacts=artifacts,
                field=f"bindings.evidence[{index}]",
            )
            for index, value in enumerate(raw_evidence)
        )
        raw_identity_edges = bindings.get("identity_edges")
        identity_edges_artifact = (
            None
            if raw_identity_edges is None
            else _artifact_from_binding(
                raw_identity_edges,
                artifacts=artifacts,
                field="bindings.identity_edges",
            )
        )
        if artifacts != (membership_artifact, aliases_artifact, *evidence_artifacts):
            message = "membership materialization bindings do not cover artifacts exactly"
            raise ValueError(message)

        completion = _exact_mapping(
            manifest.get("completion"),
            field="completion",
            keys=frozenset(
                {
                    "historical_membership_publication_availability_complete",
                    "membership_authority_complete",
                    "permanent_identity_complete",
                }
            ),
        )
        publication_complete = _required_bool(
            completion.get("historical_membership_publication_availability_complete"),
            field="completion.historical_membership_publication_availability_complete",
        )
        membership_authority_complete = _required_bool(
            completion.get("membership_authority_complete"),
            field="completion.membership_authority_complete",
        )
        permanent_identity_complete = _required_bool(
            completion.get("permanent_identity_complete"),
            field="completion.permanent_identity_complete",
        )
        counts = _exact_mapping(
            manifest.get("counts"),
            field="counts",
            keys=frozenset(
                {
                    "alias_row_count",
                    "evidence_artifact_count",
                    "identity_edge_count",
                    "interval_count",
                    "membership_row_count",
                    "security_count",
                }
            ),
        )
        interval_count = _required_count(
            counts.get("interval_count"),
            field="counts.interval_count",
            positive=True,
        )
        security_count = _required_count(
            counts.get("security_count"),
            field="counts.security_count",
            positive=True,
        )
        expected_counts = {
            "alias_row_count": aliases_artifact.row_count,
            "evidence_artifact_count": len(evidence_artifacts),
            "identity_edge_count": (
                identity_edges_artifact.row_count if identity_edges_artifact else 0
            ),
            "membership_row_count": membership_artifact.row_count,
        }
        for field, expected_count in expected_counts.items():
            if _required_count(counts.get(field), field=f"counts.{field}") != expected_count:
                message = f"membership materialization {field} differs"
                raise ValueError(message)
        lineage_value = manifest.get("lineage")
        if not isinstance(lineage_value, Mapping):
            message = "membership materialization lineage must be an object"
            raise TypeError(message)
        lineage = dict(lineage_value)
        validated_counts = _validate_artifact_contract(
            membership_artifact=membership_artifact,
            aliases_artifact=aliases_artifact,
            identity_edges_artifact=identity_edges_artifact,
            evidence_artifacts=evidence_artifacts,
            lineage=lineage,
            dataset_version=normalized_dataset,
            entitlement_scope=normalized_entitlement,
            entitlement_owner_user_id=normalized_owner,
            index_symbol=normalized_index,
            evidence_start=expected_evidence_start,
            requested_start=expected_requested_start,
            requested_end=expected_requested_end,
            interval_count=interval_count,
            security_count=security_count,
            permanent_identity_complete=permanent_identity_complete,
            membership_authority_complete=membership_authority_complete,
        )
        observed_publication_complete = bool(
            validated_counts.pop("historical_membership_publication_availability_complete")
        )
        if observed_publication_complete is not publication_complete:
            message = "membership publication-availability completion differs"
            raise ValueError(message)
        if dict(counts) != validated_counts:
            message = "membership materialization counts differ from artifact graph"
            raise ValueError(message)
        resolved_manifest = manifest_path.resolve()
        manifest_artifact = _load_manifest_evidence(
            output_root,
            resolved_manifest,
            interval_count=interval_count,
        )
        return EODHDMembershipMaterializationResult(
            membership_path=verified_content_path(output_root, membership_artifact),
            aliases_path=verified_content_path(output_root, aliases_artifact),
            identity_edges_path=(
                verified_content_path(output_root, identity_edges_artifact)
                if identity_edges_artifact is not None
                else None
            ),
            evidence_artifacts=(manifest_artifact, *evidence_artifacts),
            lineage=lineage,
            interval_count=interval_count,
            security_count=security_count,
            permanent_identity_complete=permanent_identity_complete,
            membership_authority_complete=membership_authority_complete,
            manifest_path=resolved_manifest,
            manifest_sha256=resolved_manifest.stem,
        )


__all__ = [
    "EODHD_MEMBERSHIP_MATERIALIZATION_MANIFEST_SCHEMA",
    "EODHDMembershipMaterializationResult",
    "EODHDMembershipProvider",
    "freeze_eodhd_membership_materialization",
    "load_frozen_eodhd_membership_materialization",
    "load_optional_membership_correction_evidence",
    "validate_eodhd_membership_materialization_request",
]
