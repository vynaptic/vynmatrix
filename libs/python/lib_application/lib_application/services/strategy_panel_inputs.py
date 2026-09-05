"""Append-only, DB-reconciled input boundary for synchronized strategies."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, NoReturn, Protocol, cast

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    EquityFactorEvidence,
    EquityFactorSnapshot,
    EquityObservation,
    EquitySecurityIdentity,
    EquitySourceLineage,
    IndexMembership,
    Instrument,
    StrategyPanelInputRevision,
    StrategyVersion,
)
from lib_application.services.equity_lineage import (
    equity_observation_semantic_sha256,
    validate_equity_observation_authority,
)
from lib_application.services.strategy_panel_sessions import (
    validate_strategy_panel_sessions,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.panels import (
    PanelReadyInput,
    evaluate_panel_readiness,
    panel_ready_input_to_payload,
)

_DIGEST_LENGTH = 64


class StrategyPanelInputPersistenceError(RuntimeError):
    """A data-plane panel submission is incomplete, divergent, or unauthorized."""


@dataclass(frozen=True, slots=True)
class StrategyPanelPayloadValidationRequest:
    """Exact generic and strategy-specific input presented to a trusted validator."""

    strategy_id: str
    strategy_version: str
    universe_code: str
    panel: PanelReadyInput
    panel_sha256: str
    strategy_input_payload: Mapping[str, Any]
    strategy_input_sha256: str


@dataclass(frozen=True, slots=True)
class StrategyPanelPayloadValidationResult:
    """Content-addressed proof produced by a strategy-owned evidence validator.

    The authority payload must identify every persisted observation, derived
    field semantic version, and manifest used to validate all behavior-changing
    strategy fields. Merely re-hashing caller JSON does not satisfy this contract.
    """

    validator_id: str
    validator_version: str
    validated_input_sha256: str
    authority_sha256: str
    authority_payload: Mapping[str, Any]


class StrategyPanelPayloadValidator(Protocol):
    """Strategy-specific fail-closed boundary for opaque durable panel inputs."""

    def validate_strategy_panel_payload(
        self,
        session: Session,
        *,
        request: StrategyPanelPayloadValidationRequest,
    ) -> StrategyPanelPayloadValidationResult:
        """Reconcile every behavior-changing field to persisted exact evidence."""


def persist_strategy_panel_input_revision(
    session: Session,
    *,
    strategy_id: str,
    strategy_version: str,
    universe_code: str,
    panel: PanelReadyInput,
    strategy_input_payload: Mapping[str, Any],
    now: datetime,
    strategy_payload_validator: StrategyPanelPayloadValidator,
) -> StrategyPanelInputRevision:
    """Validate and append one complete production poll input."""

    readiness = evaluate_panel_readiness(panel).require_complete()
    normalized_universe = str(universe_code).strip().upper()
    if not normalized_universe:
        _invalid("strategy panel universe_code must be non-empty")
    _require_strategy_version(
        session,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
    )
    validate_strategy_panel_sessions(session, panel=panel, now=now)
    _validate_effective_membership(
        session,
        universe_code=normalized_universe,
        panel=panel,
    )
    _validate_factor_panel(
        session,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        panel=panel,
    )

    panel_payload = panel_ready_input_to_payload(panel)
    canonical_input = _canonical_json_object(
        strategy_input_payload,
        field_name="strategy panel input",
    )
    embedded_panel = canonical_input.get("panel")
    if embedded_panel != panel_payload:
        _invalid("strategy input must embed the exact generic panel payload")
    input_sha256 = canonical_json_hash(canonical_input)
    if strategy_payload_validator is None:
        _invalid("strategy-specific panel payload validator is mandatory")
    validation = strategy_payload_validator.validate_strategy_panel_payload(
        session,
        request=StrategyPanelPayloadValidationRequest(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            universe_code=normalized_universe,
            panel=panel,
            panel_sha256=readiness.panel_sha256,
            strategy_input_payload=canonical_input,
            strategy_input_sha256=input_sha256,
        ),
    )
    authority_payload = _validate_strategy_payload_result(
        validation,
        expected_input_sha256=input_sha256,
    )
    values = {
        "input_sha256": input_sha256,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "universe_code": normalized_universe,
        "cutoff_at": _stored(panel.cutoff),
        "official_session_date": panel.session.session_date,
        "execute_not_before": _stored(panel.execution_session.opens_at),
        "data_use_scope": panel.data_use_scope.value,
        "entitlement_owner_user_id": (
            panel.provider_authority_policy.effective_entitlement_owner_user_id
        ),
        "provider_authority_sha256": panel.provider_authority_sha256,
        "membership_sha256": panel.membership_sha256,
        "factor_snapshot_sha256": panel.factor_snapshot_sha256,
        "panel_sha256": readiness.panel_sha256,
        "strategy_validator_id": validation.validator_id,
        "strategy_validator_version": validation.validator_version,
        "strategy_input_authority_sha256": validation.authority_sha256,
        "strategy_input_authority_payload": authority_payload,
        "panel_payload": panel_payload,
        "strategy_input_payload": canonical_input,
    }
    existing = session.get(StrategyPanelInputRevision, input_sha256)
    if existing is not None:
        _assert_exact_revision(existing, values)
        return existing
    session.add(StrategyPanelInputRevision(**values))
    session.flush()
    return cast(StrategyPanelInputRevision, session.get(StrategyPanelInputRevision, input_sha256))


def effective_membership_sha256(
    *,
    universe_code: str,
    panel: PanelReadyInput,
    membership_rows: Mapping[int, IndexMembership],
    observation_sha256_by_instrument: Mapping[int, str],
    security_identity_rows: Mapping[int, EquitySecurityIdentity],
    security_identity_observation_sha256_by_instrument: Mapping[int, str],
) -> str:
    """Hash exact point-in-time member intervals and source references."""

    return canonical_json_hash(
        {
            "schema": "point-in-time-index-membership-v2",
            "universe_code": universe_code,
            "effective_session": panel.session.session_date.isoformat(),
            "members": [
                {
                    "security_id": member.security_id,
                    "issuer_id": member.issuer_id,
                    "instrument_id": member.instrument_id,
                    "canonical_symbol": member.canonical_symbol,
                    "membership_observation_sha256": (
                        observation_sha256_by_instrument[member.instrument_id]
                    ),
                    "effective_from": membership_rows[
                        member.instrument_id
                    ].effective_from.isoformat(),
                    "effective_to": (
                        _date_iso_or_none(membership_rows[member.instrument_id].effective_to)
                    ),
                    "source_ref": str(membership_rows[member.instrument_id].source_ref),
                    "security_identity": {
                        "effective_from": security_identity_rows[
                            member.instrument_id
                        ].effective_from.isoformat(),
                        "effective_to": (
                            _date_iso_or_none(
                                security_identity_rows[member.instrument_id].effective_to
                            )
                        ),
                        "source_ref": str(security_identity_rows[member.instrument_id].source_ref),
                        "observation_sha256": (
                            security_identity_observation_sha256_by_instrument[member.instrument_id]
                        ),
                    },
                }
                for member in sorted(
                    panel.members,
                    key=lambda item: (item.security_id, item.instrument_id),
                )
            ],
        }
    )


def factor_panel_sha256(snapshots: list[EquityFactorSnapshot]) -> str:
    """Hash every per-instrument factor disposition in canonical order."""

    return canonical_json_hash(
        {
            "schema": "equity-factor-panel-v1",
            "snapshots": [
                {
                    "instrument_id": int(snapshot.instr_id),
                    "factor_snapshot_id": str(snapshot.factor_snapshot_id),
                    "completeness_status": str(snapshot.completeness_status),
                    "content_sha256": str(snapshot.content_sha256),
                }
                for snapshot in sorted(snapshots, key=lambda item: int(item.instr_id))
            ],
        }
    )


def _require_strategy_version(
    session: Session,
    *,
    strategy_id: str,
    strategy_version: str,
) -> StrategyVersion:
    version = session.scalar(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.semver == strategy_version,
        )
    )
    if version is None:
        _invalid("strategy panel input references an unknown strategy version")
    return version


def _validate_effective_membership(
    session: Session,
    *,
    universe_code: str,
    panel: PanelReadyInput,
) -> None:
    rows = list(
        session.scalars(
            select(IndexMembership)
            .where(
                IndexMembership.index_code == universe_code,
                IndexMembership.effective_from <= panel.session.session_date,
                or_(
                    IndexMembership.effective_to.is_(None),
                    IndexMembership.effective_to >= panel.session.session_date,
                ),
            )
            .order_by(IndexMembership.instr_id, IndexMembership.membership_id)
        )
    )
    membership_by_instrument: dict[int, IndexMembership] = {}
    for row in rows:
        instrument_id = int(row.instr_id)
        if instrument_id in membership_by_instrument:
            _invalid("effective universe contains overlapping membership intervals")
        membership_by_instrument[instrument_id] = row
    member_ids = {member.instrument_id for member in panel.members}
    if set(membership_by_instrument) != member_ids:
        _invalid("panel members do not equal the complete point-in-time universe")
    instruments = {
        int(item.instr_id): item
        for item in session.scalars(
            select(Instrument).where(Instrument.instr_id.in_(tuple(member_ids)))
        )
    }
    if any(
        str(instruments[member.instrument_id].asset_class) != "equity" for member in panel.members
    ):
        _invalid("panel member does not reference an equity catalogue instrument")
    identity_rows = list(
        session.scalars(
            select(EquitySecurityIdentity)
            .where(
                EquitySecurityIdentity.instr_id.in_(tuple(member_ids)),
                EquitySecurityIdentity.effective_from <= panel.session.session_date,
                or_(
                    EquitySecurityIdentity.effective_to.is_(None),
                    EquitySecurityIdentity.effective_to >= panel.session.session_date,
                ),
            )
            .order_by(EquitySecurityIdentity.instr_id, EquitySecurityIdentity.identity_id)
        )
    )
    identities_by_instrument: dict[int, EquitySecurityIdentity] = {}
    for identity in identity_rows:
        instrument_id = int(identity.instr_id)
        if instrument_id in identities_by_instrument:
            _invalid("effective security identity intervals overlap")
        identities_by_instrument[instrument_id] = identity
    if set(identities_by_instrument) != member_ids:
        _invalid("every effective member requires a persisted security identity")
    for member in panel.members:
        identity = identities_by_instrument[member.instrument_id]
        if (
            str(identity.security_id) != member.security_id
            or str(identity.issuer_id) != member.issuer_id
            or str(identity.canonical_symbol) != member.canonical_symbol
        ):
            _invalid(
                "panel security or issuer identity differs from persisted authority, "
                "including its dated symbol"
            )
    observation_sha256_by_instrument: dict[int, str] = {}
    identity_observation_sha256_by_instrument: dict[int, str] = {}
    for instrument_id, membership in membership_by_instrument.items():
        observation, lineage = validate_equity_observation_authority(
            session,
            observation_id=membership.observation_id,
            expected_kind="membership",
            cutoff=panel.cutoff,
            provider_authority_policy=panel.provider_authority_policy,
            expected_instrument_id=instrument_id,
        )
        if str(observation.source_record_identity) != str(membership.source_ref):
            _invalid("membership source_ref differs from immutable observation identity")
        observation_sha256_by_instrument[instrument_id] = equity_observation_semantic_sha256(
            observation, lineage
        )
        identity = identities_by_instrument[instrument_id]
        identity_observation, identity_lineage = validate_equity_observation_authority(
            session,
            observation_id=identity.observation_id,
            expected_kind="security_identity",
            cutoff=panel.cutoff,
            provider_authority_policy=panel.provider_authority_policy,
            expected_instrument_id=instrument_id,
        )
        if str(identity_observation.source_record_identity) != str(identity.source_ref):
            _invalid("security identity source_ref differs from immutable observation")
        identity_observation_sha256_by_instrument[instrument_id] = (
            equity_observation_semantic_sha256(identity_observation, identity_lineage)
        )
    expected_digest = effective_membership_sha256(
        universe_code=universe_code,
        panel=panel,
        membership_rows=membership_by_instrument,
        observation_sha256_by_instrument=observation_sha256_by_instrument,
        security_identity_rows=identities_by_instrument,
        security_identity_observation_sha256_by_instrument=(
            identity_observation_sha256_by_instrument
        ),
    )
    if panel.membership_sha256 != expected_digest:
        _invalid("panel membership digest does not match effective source intervals")


def _validate_factor_panel(
    session: Session,
    *,
    strategy_id: str,
    strategy_version: str,
    panel: PanelReadyInput,
) -> None:
    version = _require_strategy_version(
        session,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
    )
    member_ids = {member.instrument_id for member in panel.members}
    snapshots = list(
        session.scalars(
            select(EquityFactorSnapshot).where(
                EquityFactorSnapshot.strategy_id == strategy_id,
                EquityFactorSnapshot.strat_ver_id == int(version.strat_ver_id),
                EquityFactorSnapshot.effective_session == panel.session.session_date,
                EquityFactorSnapshot.cutoff_at == _stored(panel.cutoff),
                EquityFactorSnapshot.instr_id.in_(tuple(member_ids)),
            )
        )
    )
    if {int(snapshot.instr_id) for snapshot in snapshots} != member_ids:
        _invalid("factor panel does not disposition every effective universe member")
    if panel.factor_snapshot_sha256 != factor_panel_sha256(snapshots):
        _invalid("factor panel digest differs from persisted factor snapshots")
    by_instrument = {int(snapshot.instr_id): snapshot for snapshot in snapshots}
    observations = {item.security_id: item for item in panel.observations}
    exclusions = {item.security_id: item for item in panel.exclusions}
    member_by_security = {item.security_id: item for item in panel.members}
    snapshot_ids = tuple(str(snapshot.factor_snapshot_id) for snapshot in snapshots)
    evidence_rows = session.execute(
        select(EquityFactorEvidence, EquityObservation, EquitySourceLineage)
        .join(
            EquityObservation,
            EquityFactorEvidence.observation_id == EquityObservation.observation_id,
        )
        .join(
            EquitySourceLineage,
            EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
        )
        .where(EquityFactorEvidence.factor_snapshot_id.in_(snapshot_ids))
    ).all()
    evidence_by_snapshot: dict[str, dict[str, EquityObservation]] = {}
    for evidence, observation, lineage in evidence_rows:
        if observation.available_at is None or _utc(observation.available_at) > _utc(panel.cutoff):
            _invalid("factor panel contains evidence unavailable at the decision cutoff")
        try:
            panel.provider_authority_policy.require_authorized(
                provider=str(lineage.provider),
                entitlement_scope=str(lineage.entitlement_scope),
                entitlement_owner_user_id=(
                    str(lineage.entitlement_owner_user_id)
                    if lineage.entitlement_owner_user_id is not None
                    else None
                ),
            )
        except ValueError as exc:
            message = "factor panel evidence is outside provider authority"
            raise StrategyPanelInputPersistenceError(message) from exc
        evidence_by_snapshot.setdefault(str(evidence.factor_snapshot_id), {})[
            str(observation.observation_id)
        ] = observation
    for security_id, member in member_by_security.items():
        snapshot = by_instrument[member.instrument_id]
        if str(snapshot.completeness_status) == "complete":
            observation_ref = observations.get(security_id)
            if observation_ref is None:
                _invalid("complete factor snapshot has no panel observation reference")
            evidence_observation = evidence_by_snapshot.get(
                str(snapshot.factor_snapshot_id), {}
            ).get(observation_ref.observation_id)
            if evidence_observation is None:
                _invalid("panel observation is not evidence for its factor snapshot")
            if (
                _utc(evidence_observation.event_at) != _utc(observation_ref.observed_at)
                or _utc_required(evidence_observation.available_at)
                != _utc(observation_ref.available_at)
                or int(evidence_observation.revision) != observation_ref.content_revision
                or str(evidence_observation.content_sha256) != observation_ref.content_sha256
            ):
                _invalid("panel observation reference differs from immutable evidence")
        else:
            exclusion = exclusions.get(security_id)
            if exclusion is None:
                _invalid("incomplete factor snapshot has no explicit panel exclusion")
            if exclusion.disposition_identity != str(
                snapshot.factor_snapshot_id
            ) or exclusion.content_sha256 != str(snapshot.content_sha256):
                _invalid("panel exclusion differs from its factor snapshot disposition")


def _assert_exact_revision(
    row: StrategyPanelInputRevision,
    expected: Mapping[str, Any],
) -> None:
    for field_name, raw_expected_value in expected.items():
        actual = getattr(row, field_name)
        expected_value = raw_expected_value
        if field_name in {"cutoff_at", "execute_not_before"}:
            actual = _utc(actual)
            expected_value = _utc(expected_value)
        if actual != expected_value:
            _invalid("strategy panel input replay differs from immutable content")


def _validate_strategy_payload_result(
    result: StrategyPanelPayloadValidationResult,
    *,
    expected_input_sha256: str,
) -> dict[str, Any]:
    if not isinstance(result, StrategyPanelPayloadValidationResult):
        _invalid("strategy panel validator returned an incompatible proof")
    for field_name in ("validator_id", "validator_version"):
        value = getattr(result, field_name)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            _invalid(f"strategy panel {field_name} must be canonical non-empty text")
    for field_name in ("validated_input_sha256", "authority_sha256"):
        value = getattr(result, field_name)
        if (
            not isinstance(value, str)
            or len(value) != _DIGEST_LENGTH
            or any(character not in "0123456789abcdef" for character in value)
        ):
            _invalid(f"strategy panel {field_name} must be a lowercase SHA-256 digest")
    if result.validated_input_sha256 != expected_input_sha256:
        _invalid("strategy panel validator proof covers a different input")
    authority_payload = _canonical_json_object(
        result.authority_payload,
        field_name="strategy panel authority proof",
    )
    if canonical_json_hash(authority_payload) != result.authority_sha256:
        _invalid("strategy panel authority proof digest differs from its payload")
    return authority_payload


def _canonical_json_object(value: Mapping[str, Any], *, field_name: str) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        message = f"{field_name} is not canonical JSON"
        raise StrategyPanelInputPersistenceError(message) from exc
    if not isinstance(decoded, dict):
        _invalid(f"{field_name} must be an object")
    return cast(dict[str, Any], decoded)


def _stored(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


def _date_iso_or_none(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_required(value: datetime | None) -> datetime:
    if value is None:
        _invalid("observed factor evidence has no availability timestamp")
    return _utc(value)


def _invalid(message: str) -> NoReturn:
    raise StrategyPanelInputPersistenceError(message)


__all__ = [
    "StrategyPanelInputPersistenceError",
    "StrategyPanelPayloadValidationRequest",
    "StrategyPanelPayloadValidationResult",
    "StrategyPanelPayloadValidator",
    "effective_membership_sha256",
    "factor_panel_sha256",
    "persist_strategy_panel_input_revision",
]
