"""Atomic persistence and cutoff-safe replay for equity factor snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import NoReturn, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    EquityFactorEvidence,
    EquityFactorSnapshot,
    EquityFactorSnapshotDetail,
    EquityObservation,
    EquitySourceLineage,
    StrategyVersion,
)
from lib_application.services.equity_lineage import EquityObservationAuthorityError
from lib_application.services.equity_optional_factor_sources import (
    OptionalFactorEvidenceValidationError,
    validate_optional_factor_evidence,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.cross_sectional import FactorDirection, PeerScaleMethod
from lib_strategy.data_authority import ProviderAuthorityPolicy
from lib_strategy.equity_optional_factors import (
    OptionalFactorApplication,
    OptionalFactorContractError,
    optional_factor_source_contract,
    optional_factor_source_registry_sha256,
)

_FACTOR_SNAPSHOT_SCHEMA = "equity-factor-snapshot-v1"
_DIGEST_LENGTH = 64


class EquityFactorSnapshotPersistenceError(RuntimeError):
    """Raised when an immutable factor snapshot cannot be persisted safely."""


class EquityEvidenceRole(StrEnum):
    """Relationship between a factor result and one source observation."""

    PRIMARY = "primary"
    CONTEXT = "context"
    PEER = "peer"
    BENCHMARK = "benchmark"
    CALENDAR = "calendar"
    MEMBERSHIP = "membership"


class EquityFactorState(StrEnum):
    """Persisted completeness state for one configured factor."""

    COMPLETE = "complete"
    DISABLED = "disabled"
    MISSING = "missing"
    STALE = "stale"
    AMBIGUOUS = "ambiguous"
    INELIGIBLE = "ineligible"
    INSUFFICIENT_PEERS = "insufficient_peers"


@dataclass(frozen=True, slots=True)
class EquityEvidenceReference:
    """One exact immutable source observation used by a factor."""

    observation_id: str
    role: EquityEvidenceRole = EquityEvidenceRole.PRIMARY

    def __post_init__(self) -> None:
        _require_digest(self.observation_id, field_name="observation_id")
        if not isinstance(self.role, EquityEvidenceRole):
            _invalid("evidence role must be an EquityEvidenceRole")


@dataclass(frozen=True, slots=True)
class EquityFactorDetailInput:
    """Normalized factor arithmetic and evidence ready for persistence."""

    factor_name: str
    sleeve_name: str
    factor_version: str
    direction: FactorDirection
    enabled: bool
    state: EquityFactorState
    weight: Decimal
    evidence: tuple[EquityEvidenceReference, ...] = ()
    raw_value: Decimal | None = None
    peer_group: str | None = None
    peer_count: int | None = None
    peer_center: Decimal | None = None
    peer_scale: Decimal | None = None
    peer_scale_method: PeerScaleMethod | None = None
    unbounded_normalized_value: Decimal | None = None
    normalized_value: Decimal | None = None
    factor_rank: Decimal | None = None
    contribution: Decimal | None = None
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("factor_name", "sleeve_name", "factor_version"):
            _require_text(getattr(self, field_name), field_name=field_name)
        if not isinstance(self.direction, FactorDirection):
            _invalid("direction must be a FactorDirection")
        if not isinstance(self.enabled, bool):
            _invalid("enabled must be boolean")
        if not isinstance(self.state, EquityFactorState):
            _invalid("state must be an EquityFactorState")
        object.__setattr__(self, "weight", _decimal(self.weight, scale=12, field_name="weight"))
        for field_name, scale in (
            ("raw_value", 18),
            ("peer_center", 18),
            ("peer_scale", 18),
            ("unbounded_normalized_value", 18),
            ("normalized_value", 18),
            ("factor_rank", 10),
            ("contribution", 18),
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _decimal(value, scale=scale, field_name=field_name),
                )
        if self.weight < 0 or self.weight > 1:
            _invalid("weight must be in [0, 1]")
        if not isinstance(self.evidence, tuple):
            _invalid("evidence must be an immutable tuple")
        observation_ids = tuple(item.observation_id for item in self.evidence)
        canonical_evidence = tuple(
            sorted(self.evidence, key=lambda item: (item.observation_id, item.role.value))
        )
        if self.evidence != canonical_evidence or len(observation_ids) != len(set(observation_ids)):
            _invalid("factor evidence must be unique and canonically ordered")
        self._validate_state()

    def _validate_state(self) -> None:
        numeric_values = (
            self.raw_value,
            self.peer_center,
            self.peer_scale,
            self.unbounded_normalized_value,
            self.normalized_value,
            self.factor_rank,
            self.contribution,
        )
        if self.state is EquityFactorState.DISABLED:
            if (
                self.enabled
                or self.weight != 0
                or any(value is not None for value in numeric_values)
                or self.peer_group is not None
                or self.peer_count is not None
                or self.peer_scale_method is not None
                or self.missing_reason is not None
                or self.evidence
            ):
                _invalid("disabled factors must carry only zero weight")
            return
        if not self.enabled:
            _invalid("non-disabled factor states must be enabled")
        if self.state is not EquityFactorState.COMPLETE:
            _require_text(self.missing_reason, field_name="missing_reason")
            return
        if self.missing_reason is not None:
            _invalid("complete factors cannot carry missing_reason")
        if (
            any(value is None for value in numeric_values)
            or self.peer_group is None
            or self.peer_count is None
            or self.peer_scale_method is None
            or not self.evidence
        ):
            _invalid("complete factors require arithmetic, peer context, and evidence")
        _require_text(self.peer_group, field_name="peer_group")
        if self.peer_count < 1:
            _invalid("complete factor peer_count must be positive")
        peer_scale = self.peer_scale
        factor_rank = self.factor_rank
        normalized_value = self.normalized_value
        if peer_scale is None or peer_scale < 0:
            _invalid("complete factor peer_scale must be non-negative")
        if factor_rank is None or factor_rank < 1:
            _invalid("complete factor rank must be at least one")
        if normalized_value is None:
            _invalid("complete factor normalized_value is required")
        expected = _decimal(
            self.weight * normalized_value,
            scale=18,
            field_name="contribution",
        )
        if self.contribution != expected:
            _invalid("contribution must equal weight * normalized_value")


@dataclass(frozen=True, slots=True)
class EquityFactorSnapshotSubmission:
    """One complete per-instrument snapshot at a decision cutoff."""

    strategy_id: str
    strategy_version_id: int
    instrument_id: int
    effective_session: date
    cutoff_at: datetime
    calculation_version: str
    configuration_digest: str
    source_contract_registry_sha256: str
    peer_taxonomy_version: str
    peer_group: str
    details: tuple[EquityFactorDetailInput, ...]

    def __post_init__(self) -> None:
        _require_text(self.strategy_id, field_name="strategy_id")
        _require_positive_int(self.strategy_version_id, field_name="strategy_version_id")
        _require_positive_int(self.instrument_id, field_name="instrument_id")
        cutoff = _utc(self.cutoff_at, field_name="cutoff_at")
        object.__setattr__(self, "cutoff_at", cutoff)
        if self.effective_session > cutoff.date():
            _invalid("effective_session cannot follow cutoff_at")
        _require_text(self.calculation_version, field_name="calculation_version")
        _require_digest(self.configuration_digest, field_name="configuration_digest")
        _require_digest(
            self.source_contract_registry_sha256,
            field_name="source_contract_registry_sha256",
        )
        if self.source_contract_registry_sha256 != optional_factor_source_registry_sha256():
            _invalid("source-contract registry differs from the registered optional factors")
        _require_text(self.peer_taxonomy_version, field_name="peer_taxonomy_version")
        _require_text(self.peer_group, field_name="peer_group")
        if not isinstance(self.details, tuple) or not self.details:
            _invalid("details must be a non-empty immutable tuple")
        names = tuple(detail.factor_name for detail in self.details)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            _invalid("factor details must be unique and canonically ordered")
        if not any(detail.enabled for detail in self.details):
            _invalid("factor snapshot requires at least one enabled factor")


@dataclass(frozen=True, slots=True)
class PersistedEquityFactorSnapshot:
    """Identity returned for a newly inserted or exact replayed snapshot."""

    factor_snapshot_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class _PersistenceMaterial:
    factor_snapshot_id: str
    snapshot: dict[str, object]
    details: tuple[dict[str, object], ...]
    detail_payloads: tuple[dict[str, object], ...]
    evidence: tuple[dict[str, object], ...]


def equity_factor_snapshot_identity(
    submission: EquityFactorSnapshotSubmission,
) -> str:
    """Return the exact content identity without reading or mutating the DB."""

    if not isinstance(submission, EquityFactorSnapshotSubmission):
        _invalid("submission must be an EquityFactorSnapshotSubmission")
    return _build_material(submission).factor_snapshot_id


def persist_equity_factor_snapshot(
    session: Session,
    submission: EquityFactorSnapshotSubmission,
    *,
    provider_authority_policy: ProviderAuthorityPolicy | None = None,
) -> PersistedEquityFactorSnapshot:
    """Atomically insert one snapshot or accept only its exact immutable replay.

    The caller owns the transaction. Locking the strategy-version row and the
    database uniqueness constraint serialize competing completions for the same
    instrument cutoff. Evidence is checked before any snapshot row is staged.
    """

    if not isinstance(submission, EquityFactorSnapshotSubmission):
        _invalid("submission must be an EquityFactorSnapshotSubmission")
    _lock_strategy_version(session, submission)
    material = _build_material(submission)
    _validate_optional_factor_contracts(
        session,
        submission,
        provider_authority_policy=provider_authority_policy,
    )
    _validate_evidence_cutoff(
        session,
        submission,
        material.evidence,
        provider_authority_policy=provider_authority_policy,
    )
    existing = session.execute(
        select(EquityFactorSnapshot)
        .where(
            EquityFactorSnapshot.strategy_id == submission.strategy_id,
            EquityFactorSnapshot.strat_ver_id == submission.strategy_version_id,
            EquityFactorSnapshot.instr_id == submission.instrument_id,
            EquityFactorSnapshot.effective_session == submission.effective_session,
            EquityFactorSnapshot.cutoff_at == submission.cutoff_at,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        if existing.factor_snapshot_id != material.factor_snapshot_id:
            message = "completed equity factor cutoff cannot be corrected or replaced"
            raise EquityFactorSnapshotPersistenceError(message)
        _assert_exact_replay(session, material)
        return PersistedEquityFactorSnapshot(material.factor_snapshot_id, created=False)

    session.add(EquityFactorSnapshot(**material.snapshot))
    session.flush()
    session.add_all(EquityFactorSnapshotDetail(**values) for values in material.details)
    session.flush()
    session.add_all(EquityFactorEvidence(**values) for values in material.evidence)
    session.flush()
    return PersistedEquityFactorSnapshot(material.factor_snapshot_id, created=True)


def _lock_strategy_version(
    session: Session,
    submission: EquityFactorSnapshotSubmission,
) -> None:
    strategy_version = session.execute(
        select(StrategyVersion)
        .where(
            StrategyVersion.strat_ver_id == submission.strategy_version_id,
            StrategyVersion.strategy_id == submission.strategy_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if strategy_version is None:
        message = "factor snapshot references an unknown strategy version"
        raise EquityFactorSnapshotPersistenceError(message)


def _build_material(submission: EquityFactorSnapshotSubmission) -> _PersistenceMaterial:
    details = tuple(_detail_values(detail) for detail in submission.details)
    expected_count = sum(detail.enabled for detail in submission.details)
    available_count = sum(
        detail.enabled and detail.state is EquityFactorState.COMPLETE
        for detail in submission.details
    )
    completeness = "complete" if expected_count == available_count else "ineligible"
    payload = {
        "schema": _FACTOR_SNAPSHOT_SCHEMA,
        "strategy_id": submission.strategy_id,
        "strategy_version_id": submission.strategy_version_id,
        "instrument_id": submission.instrument_id,
        "effective_session": submission.effective_session.isoformat(),
        "cutoff_at": submission.cutoff_at.isoformat(),
        "calculation_version": submission.calculation_version,
        "configuration_digest": submission.configuration_digest,
        "source_contract_registry_sha256": submission.source_contract_registry_sha256,
        "peer_taxonomy_version": submission.peer_taxonomy_version,
        "peer_group": submission.peer_group,
        "completeness_status": completeness,
        "expected_factor_count": expected_count,
        "available_factor_count": available_count,
        "details": [_detail_content(values) for values in details],
    }
    factor_snapshot_id = canonical_json_hash(payload)
    snapshot = {
        "factor_snapshot_id": factor_snapshot_id,
        "strategy_id": submission.strategy_id,
        "strat_ver_id": submission.strategy_version_id,
        "instr_id": submission.instrument_id,
        "effective_session": submission.effective_session,
        "cutoff_at": submission.cutoff_at,
        "calculation_version": submission.calculation_version,
        "configuration_digest": submission.configuration_digest,
        "source_contract_registry_sha256": submission.source_contract_registry_sha256,
        "peer_taxonomy_version": submission.peer_taxonomy_version,
        "peer_group": submission.peer_group,
        "completeness_status": completeness,
        "expected_factor_count": expected_count,
        "available_factor_count": available_count,
        "content_sha256": factor_snapshot_id,
    }
    persisted_details = tuple(
        {
            "factor_snapshot_id": factor_snapshot_id,
            **{key: value for key, value in values.items() if key != "evidence"},
        }
        for values in details
    )
    evidence: tuple[dict[str, object], ...] = tuple(
        {
            "factor_snapshot_id": factor_snapshot_id,
            "factor_name": detail.factor_name,
            "observation_id": reference.observation_id,
            "evidence_role": reference.role.value,
        }
        for detail in submission.details
        for reference in detail.evidence
    )
    return _PersistenceMaterial(
        factor_snapshot_id,
        snapshot,
        persisted_details,
        tuple(_detail_content(values) for values in details),
        evidence,
    )


def _detail_values(detail: EquityFactorDetailInput) -> dict[str, object]:
    return {
        "factor_name": detail.factor_name,
        "sleeve_name": detail.sleeve_name,
        "factor_version": detail.factor_version,
        "direction": detail.direction.value,
        "enabled": detail.enabled,
        "state": detail.state.value,
        "raw_value": detail.raw_value,
        "peer_group": detail.peer_group,
        "peer_count": detail.peer_count,
        "peer_center": detail.peer_center,
        "peer_scale": detail.peer_scale,
        "peer_scale_method": (
            detail.peer_scale_method.value if detail.peer_scale_method is not None else None
        ),
        "unbounded_normalized_value": detail.unbounded_normalized_value,
        "normalized_value": detail.normalized_value,
        "factor_rank": detail.factor_rank,
        "weight": detail.weight,
        "contribution": detail.contribution,
        "missing_reason": detail.missing_reason,
        "evidence": tuple(
            (reference.observation_id, reference.role.value) for reference in detail.evidence
        ),
    }


def _detail_content(values: dict[str, object]) -> dict[str, object]:
    payload = dict(values)
    evidence = cast(tuple[tuple[str, str], ...], values["evidence"])
    payload["evidence"] = [
        {"observation_id": observation_id, "role": role} for observation_id, role in evidence
    ]
    for field_name, scale in (
        ("raw_value", 18),
        ("peer_center", 18),
        ("peer_scale", 18),
        ("unbounded_normalized_value", 18),
        ("normalized_value", 18),
        ("factor_rank", 10),
        ("weight", 12),
        ("contribution", 18),
    ):
        value = values[field_name]
        payload[field_name] = None if value is None else _decimal_token(value, scale=scale)
    return payload


def _validate_evidence_cutoff(
    session: Session,
    submission: EquityFactorSnapshotSubmission,
    evidence_values: tuple[dict[str, object], ...],
    *,
    provider_authority_policy: ProviderAuthorityPolicy | None,
) -> None:
    if not evidence_values:
        return
    observation_ids = {str(values["observation_id"]) for values in evidence_values}
    rows = session.execute(
        select(EquityObservation, EquitySourceLineage)
        .join(EquitySourceLineage, EquityObservation.lineage_id == EquitySourceLineage.lineage_id)
        .where(EquityObservation.observation_id.in_(tuple(observation_ids)))
    ).all()
    observations = {observation.observation_id: observation for observation, _lineage in rows}
    lineages = {observation.observation_id: lineage for observation, lineage in rows}
    missing = sorted(observation_ids - set(observations))
    if missing:
        message = f"factor evidence observations are missing: {missing!r}"
        raise EquityFactorSnapshotPersistenceError(message)
    for values in evidence_values:
        observation_id = str(values["observation_id"])
        role = str(values["evidence_role"])
        observation = observations[observation_id]
        lineage = lineages[observation_id]
        if lineage.entitlement_owner_user_id is not None:
            if provider_authority_policy is None:
                message = f"factor evidence {observation_id} requires owner-scoped authority"
                raise EquityFactorSnapshotPersistenceError(message)
            try:
                provider_authority_policy.require_authorized(
                    provider=str(lineage.provider),
                    entitlement_scope=str(lineage.entitlement_scope),
                    entitlement_owner_user_id=str(lineage.entitlement_owner_user_id),
                )
            except ValueError as exc:
                message = f"factor evidence {observation_id} is outside owner-scoped authority"
                raise EquityFactorSnapshotPersistenceError(message) from exc
        if observation.disposition != "observed":
            message = f"factor evidence {observation_id} is not an observed record"
            raise EquityFactorSnapshotPersistenceError(message)
        if (
            observation.available_at is None
            or _stored_utc(
                observation.available_at,
            )
            > submission.cutoff_at
        ):
            message = f"factor evidence {observation_id} was unavailable at the decision cutoff"
            raise EquityFactorSnapshotPersistenceError(message)
        if _stored_utc(observation.event_at) > submission.cutoff_at:
            message = f"factor evidence {observation_id} has a future event timestamp"
            raise EquityFactorSnapshotPersistenceError(message)
        if role == EquityEvidenceRole.PRIMARY.value and observation.instr_id not in (
            None,
            submission.instrument_id,
        ):
            message = f"primary factor evidence {observation_id} belongs to another instrument"
            raise EquityFactorSnapshotPersistenceError(message)


def _validate_optional_factor_contracts(
    session: Session,
    submission: EquityFactorSnapshotSubmission,
    *,
    provider_authority_policy: ProviderAuthorityPolicy | None,
) -> None:
    for detail in submission.details:
        try:
            contract = optional_factor_source_contract(detail.factor_name)
        except OptionalFactorContractError:
            continue
        if not detail.enabled:
            continue
        if contract.activation.application is not OptionalFactorApplication.CROSS_SECTIONAL_RANK:
            message = f"optional factor {detail.factor_name!r} is not a cross-sectional rank sleeve"
            raise EquityFactorSnapshotPersistenceError(message)
        if detail.direction is not contract.activation.direction:
            message = f"optional factor {detail.factor_name!r} direction differs from its contract"
            raise EquityFactorSnapshotPersistenceError(message)
        if len(detail.evidence) < contract.activation.minimum_evidence_count:
            message = (
                f"optional factor {detail.factor_name!r} requires at least "
                f"{contract.activation.minimum_evidence_count} source observations"
            )
            raise EquityFactorSnapshotPersistenceError(message)
        if provider_authority_policy is None:
            message = f"optional factor {detail.factor_name!r} requires provider authority"
            raise EquityFactorSnapshotPersistenceError(message)
        expected_instrument_id = submission.instrument_id if contract.requires_instrument else None
        for reference in detail.evidence:
            try:
                validated = validate_optional_factor_evidence(
                    session,
                    sleeve_name=detail.factor_name,
                    factor_version=detail.factor_version,
                    observation_id=reference.observation_id,
                    cutoff=submission.cutoff_at,
                    provider_authority_policy=provider_authority_policy,
                    expected_instrument_id=expected_instrument_id,
                )
            except (
                EquityObservationAuthorityError,
                OptionalFactorEvidenceValidationError,
            ) as exc:
                message = f"optional factor {detail.factor_name!r} evidence is invalid"
                raise EquityFactorSnapshotPersistenceError(message) from exc
            if validated.source_contract_sha256 != contract.digest:
                message = f"optional factor {detail.factor_name!r} contract identity differs"
                raise EquityFactorSnapshotPersistenceError(message)


def _assert_exact_replay(session: Session, material: _PersistenceMaterial) -> None:
    stored_details = session.execute(
        select(EquityFactorSnapshotDetail)
        .where(EquityFactorSnapshotDetail.factor_snapshot_id == material.factor_snapshot_id)
        .order_by(EquityFactorSnapshotDetail.factor_name)
    ).scalars()
    stored_evidence = session.execute(
        select(EquityFactorEvidence)
        .where(EquityFactorEvidence.factor_snapshot_id == material.factor_snapshot_id)
        .order_by(EquityFactorEvidence.factor_name, EquityFactorEvidence.observation_id)
    ).scalars()
    evidence_by_factor: dict[str, list[tuple[str, str]]] = {}
    for row in stored_evidence:
        evidence_by_factor.setdefault(row.factor_name, []).append(
            (row.observation_id, row.evidence_role)
        )
    stored_payloads = tuple(
        _detail_content(
            {
                "factor_name": row.factor_name,
                "sleeve_name": row.sleeve_name,
                "factor_version": row.factor_version,
                "direction": row.direction,
                "enabled": row.enabled,
                "state": row.state,
                "raw_value": row.raw_value,
                "peer_group": row.peer_group,
                "peer_count": row.peer_count,
                "peer_center": row.peer_center,
                "peer_scale": row.peer_scale,
                "peer_scale_method": row.peer_scale_method,
                "unbounded_normalized_value": row.unbounded_normalized_value,
                "normalized_value": row.normalized_value,
                "factor_rank": row.factor_rank,
                "weight": row.weight,
                "contribution": row.contribution,
                "missing_reason": row.missing_reason,
                "evidence": tuple(evidence_by_factor.get(row.factor_name, ())),
            }
        )
        for row in stored_details
    )
    if stored_payloads != material.detail_payloads:
        message = "persisted factor details do not match the immutable replay"
        raise EquityFactorSnapshotPersistenceError(message)


def _utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    """Restore UTC convention for SQLite's timezone-naive datetime adapter."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal(value: object, *, scale: int, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        _invalid(f"{field_name} must be a finite Decimal")
    try:
        return value.quantize(Decimal(1).scaleb(-scale))
    except InvalidOperation:
        _invalid(f"{field_name} exceeds supported precision")


def _decimal_token(value: object, *, scale: int) -> str:
    return format(_decimal(value, scale=scale, field_name="numeric content"), "f")


def _require_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field_name} must be a non-blank canonical string")
    return value


def _require_digest(value: object, *, field_name: str) -> str:
    digest = _require_text(value, field_name=field_name)
    if len(digest) != _DIGEST_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        _invalid(f"{field_name} must be a lowercase SHA-256 digest")
    return digest


def _require_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _invalid(f"{field_name} must be a positive integer")
    return value


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)


__all__ = [
    "EquityEvidenceReference",
    "EquityEvidenceRole",
    "EquityFactorDetailInput",
    "EquityFactorSnapshotPersistenceError",
    "EquityFactorSnapshotSubmission",
    "EquityFactorState",
    "PersistedEquityFactorSnapshot",
    "equity_factor_snapshot_identity",
    "persist_equity_factor_snapshot",
]
