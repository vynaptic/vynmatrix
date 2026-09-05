"""Atomic persistence and entitlement-safe replay of equity rank snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    EquityFactorEvidence,
    EquityFactorSnapshot,
    EquityFactorSnapshotDetail,
    EquityObservation,
    EquityRankSnapshot,
    EquityRankSnapshotRow,
    EquitySourceLineage,
    StrategyVersion,
)
from lib_strategy.cross_sectional import FactorContribution
from lib_strategy.panels import PanelEvaluationAudit, PanelReadyInput

_NUMERIC_TOLERANCE = Decimal("0.000000000001")


class EquityRankSnapshotPersistenceError(RuntimeError):
    """An immutable rank snapshot failed lineage or replay validation."""


@dataclass(frozen=True, slots=True)
class PersistedEquityRankSnapshot:
    """Rank identity returned for a new insert or exact replay."""

    rank_snapshot_id: str
    strategy_version_id: int
    created: bool


def persist_equity_rank_snapshot(
    session: Session,
    *,
    strategy_id: str,
    strategy_version: str,
    panel: PanelReadyInput,
    audit: PanelEvaluationAudit,
) -> PersistedEquityRankSnapshot:
    """Persist a complete rank result inside the caller's panel transaction."""

    _validate_audit_panel(panel, audit)
    version = _lock_strategy_version(
        session,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
    )
    factor_snapshots = _load_and_validate_factor_snapshots(
        session,
        strategy_id=strategy_id,
        strategy_version_id=int(version.strat_ver_id),
        panel=panel,
        audit=audit,
    )
    _validate_contribution_ledgers(session, audit=audit, snapshots=factor_snapshots)
    _validate_factor_entitlements(
        session,
        panel=panel,
        factor_snapshot_ids=tuple(sorted(factor_snapshots)),
    )

    header = _header_values(
        strategy_id=strategy_id,
        strategy_version_id=int(version.strat_ver_id),
        panel=panel,
        audit=audit,
    )
    rows = _row_values(audit)
    existing = session.get(EquityRankSnapshot, audit.rank_snapshot.content_digest)
    if existing is not None:
        _assert_exact_header(existing, header)
        _assert_exact_rows(session, audit.rank_snapshot.content_digest, rows)
        return PersistedEquityRankSnapshot(
            audit.rank_snapshot.content_digest,
            int(version.strat_ver_id),
            created=False,
        )

    divergent = session.scalar(
        select(EquityRankSnapshot)
        .where(
            EquityRankSnapshot.strategy_id == strategy_id,
            EquityRankSnapshot.strat_ver_id == int(version.strat_ver_id),
            EquityRankSnapshot.effective_session == panel.session.session_date,
            EquityRankSnapshot.cutoff_at == _stored_datetime(panel.cutoff),
            EquityRankSnapshot.configuration_digest == audit.configuration_sha256,
            EquityRankSnapshot.factor_content_digest == panel.factor_snapshot_sha256,
            EquityRankSnapshot.data_use_scope == panel.data_use_scope.value,
            EquityRankSnapshot.provider_authority_digest == panel.provider_authority_sha256,
        )
        .with_for_update()
    )
    if divergent is not None:
        _invalid("completed equity rank cutoff cannot be corrected or replaced")

    session.add(EquityRankSnapshot(**header))
    session.flush()
    session.add_all(EquityRankSnapshotRow(**row) for row in rows)
    session.flush()
    return PersistedEquityRankSnapshot(
        audit.rank_snapshot.content_digest,
        int(version.strat_ver_id),
        created=True,
    )


def _validate_audit_panel(panel: PanelReadyInput, audit: PanelEvaluationAudit) -> None:
    if audit.replayed:
        _invalid("a prepared panel completion cannot persist a replay marker")
    if audit.rank_snapshot.cutoff != panel.cutoff.astimezone(UTC):
        _invalid("rank audit cutoff does not match the pinned panel")
    member_by_id = {member.security_id: member for member in panel.members}
    row_by_id = {row.entity_id: row for row in audit.rows}
    if set(member_by_id) != set(row_by_id):
        _invalid("rank audit must disposition every effective panel member")
    for entity_id, member in member_by_id.items():
        if (
            row_by_id[entity_id].symbol != member.canonical_symbol
            or row_by_id[entity_id].instrument_id != member.instrument_id
        ):
            _invalid("rank audit member identity does not match the pinned panel")
    excluded = {item.security_id for item in panel.exclusions}
    if any(row_by_id[entity_id].rank_complete for entity_id in excluded):
        _invalid("an explicitly excluded panel member cannot be rank eligible")


def _lock_strategy_version(
    session: Session,
    *,
    strategy_id: str,
    strategy_version: str,
) -> StrategyVersion:
    version = session.scalar(
        select(StrategyVersion)
        .where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.semver == strategy_version,
        )
        .with_for_update()
    )
    if version is None:
        _invalid("rank snapshot references an unknown strategy version")
    return version


def _load_and_validate_factor_snapshots(
    session: Session,
    *,
    strategy_id: str,
    strategy_version_id: int,
    panel: PanelReadyInput,
    audit: PanelEvaluationAudit,
) -> dict[str, EquityFactorSnapshot]:
    expected = {
        row.factor_snapshot_id: row for row in audit.rows if row.factor_snapshot_id is not None
    }
    if len(expected) != sum(row.rank_complete for row in audit.rows):
        _invalid("eligible rank rows must reference distinct factor snapshots")
    snapshots = session.scalars(
        select(EquityFactorSnapshot).where(
            EquityFactorSnapshot.factor_snapshot_id.in_(tuple(expected))
        )
    ).all()
    by_id = {snapshot.factor_snapshot_id: snapshot for snapshot in snapshots}
    missing = sorted(set(expected) - set(by_id))
    if missing:
        _invalid(f"rank factor snapshots are missing: {missing!r}")
    for factor_snapshot_id, row in expected.items():
        snapshot = by_id[factor_snapshot_id]
        if (
            snapshot.strategy_id != strategy_id
            or int(snapshot.strat_ver_id) != strategy_version_id
            or int(snapshot.instr_id) != row.instrument_id
            or snapshot.effective_session != panel.session.session_date
            or _utc(snapshot.cutoff_at) != panel.cutoff.astimezone(UTC)
            or snapshot.calculation_version != audit.rank_snapshot.calculation_version
            or snapshot.configuration_digest != audit.configuration_sha256
            or snapshot.peer_taxonomy_version != audit.peer_taxonomy_version
            or snapshot.completeness_status != "complete"
        ):
            _invalid("rank row references an incompatible factor snapshot")
    return by_id


def _validate_contribution_ledgers(
    session: Session,
    *,
    audit: PanelEvaluationAudit,
    snapshots: dict[str, EquityFactorSnapshot],
) -> None:
    if not snapshots:
        return
    details = session.scalars(
        select(EquityFactorSnapshotDetail).where(
            EquityFactorSnapshotDetail.factor_snapshot_id.in_(tuple(snapshots))
        )
    ).all()
    evidence = session.scalars(
        select(EquityFactorEvidence).where(
            EquityFactorEvidence.factor_snapshot_id.in_(tuple(snapshots))
        )
    ).all()
    details_by_key = {(detail.factor_snapshot_id, detail.factor_name): detail for detail in details}
    required_versions = dict(audit.required_factor_versions)
    for factor_snapshot_id in snapshots:
        snapshot_details = {
            detail.factor_name: detail
            for detail in details
            if detail.factor_snapshot_id == factor_snapshot_id
        }
        if set(snapshot_details) != set(required_versions):
            _invalid("factor snapshot does not cover the fixed factor-version contract")
        for factor_name, required_version in required_versions.items():
            detail = snapshot_details[factor_name]
            if required_version is None:
                if bool(detail.enabled) or detail.state != "disabled":
                    _invalid("disabled factor does not match the fixed factor contract")
            elif (
                not bool(detail.enabled)
                or detail.factor_version != required_version
                or detail.state != "complete"
            ):
                _invalid("enabled factor version does not match the fixed factor contract")
    evidence_by_key: dict[tuple[str, str], set[str]] = {}
    for item in evidence:
        evidence_by_key.setdefault((item.factor_snapshot_id, item.factor_name), set()).add(
            item.observation_id
        )
    factor_id_by_entity = {
        row.entity_id: row.factor_snapshot_id
        for row in audit.rows
        if row.factor_snapshot_id is not None
    }
    contributions = {
        (item.entity_id, item.factor_name): item for item in audit.rank_snapshot.contributions
    }
    enabled_names = {spec.name for spec in audit.rank_snapshot.factor_specs if spec.enabled}
    for entity_id, factor_snapshot_id in factor_id_by_entity.items():
        for factor_name in enabled_names:
            contribution = contributions.get((entity_id, factor_name))
            contribution_detail = details_by_key.get((factor_snapshot_id, factor_name))
            if (
                contribution is None
                or contribution_detail is None
                or contribution_detail.state != "complete"
            ):
                _invalid("eligible rank contribution is absent from factor evidence")
            _assert_contribution(contribution_detail, contribution)
            persisted_evidence = evidence_by_key.get((factor_snapshot_id, factor_name), set())
            if persisted_evidence != set(contribution.source_observation_ids):
                _invalid("rank contribution source evidence does not match factor snapshot")


def _assert_contribution(
    detail: EquityFactorSnapshotDetail,
    contribution: FactorContribution,
) -> None:
    expected_text = {
        "peer_group": contribution.peer_group,
        "peer_scale_method": contribution.peer_scale_method.value,
    }
    if any(getattr(detail, key) != value for key, value in expected_text.items()):
        _invalid("rank contribution peer context does not match factor snapshot")
    if int(detail.peer_count or 0) != contribution.peer_count:
        _invalid("rank contribution peer count does not match factor snapshot")
    expected_numeric = {
        "raw_value": contribution.raw_value,
        "peer_center": contribution.peer_center,
        "peer_scale": contribution.peer_scale,
        "unbounded_normalized_value": contribution.unbounded_normalized_value,
        "normalized_value": contribution.normalized_value,
        "factor_rank": contribution.factor_rank,
        "weight": contribution.weight,
        "contribution": contribution.contribution,
    }
    for field_name, expected in expected_numeric.items():
        stored = getattr(detail, field_name)
        if stored is None or abs(Decimal(stored) - Decimal(str(expected))) > _NUMERIC_TOLERANCE:
            _invalid(f"rank contribution {field_name} does not match factor snapshot")


def _validate_factor_entitlements(
    session: Session,
    *,
    panel: PanelReadyInput,
    factor_snapshot_ids: tuple[str, ...],
) -> None:
    if not factor_snapshot_ids:
        return
    rows = session.execute(
        select(EquityFactorEvidence, EquityObservation, EquitySourceLineage)
        .join(
            EquityObservation,
            EquityFactorEvidence.observation_id == EquityObservation.observation_id,
        )
        .join(
            EquitySourceLineage,
            EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
        )
        .where(EquityFactorEvidence.factor_snapshot_id.in_(factor_snapshot_ids))
    ).all()
    if not rows:
        _invalid("rank factor snapshots have no immutable source evidence")
    for _evidence, observation, lineage in rows:
        if observation.available_at is None or _utc(observation.available_at) > panel.cutoff:
            _invalid("rank evidence was unavailable at the panel cutoff")
        try:
            panel.provider_authority_policy.require_authorized(
                provider=lineage.provider,
                entitlement_scope=lineage.entitlement_scope,
                entitlement_owner_user_id=lineage.entitlement_owner_user_id,
            )
        except ValueError as exc:
            message = "rank factor evidence is outside the pinned provider authority"
            raise EquityRankSnapshotPersistenceError(message) from exc


def _header_values(
    *,
    strategy_id: str,
    strategy_version_id: int,
    panel: PanelReadyInput,
    audit: PanelEvaluationAudit,
) -> dict[str, Any]:
    included = sum(row.rank_complete for row in audit.rows)
    return {
        "rank_snapshot_id": audit.rank_snapshot.content_digest,
        "strategy_id": strategy_id,
        "strat_ver_id": strategy_version_id,
        "effective_session": panel.session.session_date,
        "cutoff_at": _stored_datetime(panel.cutoff),
        "configuration_digest": audit.configuration_sha256,
        "panel_revision_digest": panel.canonical_digest(),
        "factor_content_digest": panel.factor_snapshot_sha256,
        "data_use_scope": panel.data_use_scope.value,
        "provider_authority_digest": panel.provider_authority_sha256,
        "provider_authority_policy": panel.provider_authority_policy.to_payload(),
        "peer_taxonomy_version": audit.peer_taxonomy_version,
        "completeness_status": "complete",
        "expected_instrument_count": len(audit.rows),
        "included_instrument_count": included,
        "excluded_instrument_count": len(audit.rows) - included,
        "content_sha256": audit.rank_snapshot.content_digest,
    }


def _row_values(audit: PanelEvaluationAudit) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "rank_snapshot_id": audit.rank_snapshot.content_digest,
            "instr_id": row.instrument_id,
            "factor_snapshot_id": row.factor_snapshot_id,
            "eligible": row.rank_complete,
            "strategy_eligible": row.strategy_eligible,
            "row_ordinal": ordinal,
            "rank_position": _decimal(row.rank),
            "composite_score": _decimal(row.composite_score),
            "target_allocation_hint": _decimal(row.target_allocation_hint),
            "decision": row.decision.value,
            "incumbent": row.incumbent,
            "exclusion_reason": row.exclusion_reason,
        }
        for ordinal, row in enumerate(audit.rows)
    )


def _assert_exact_header(
    stored: EquityRankSnapshot,
    expected: dict[str, Any],
) -> None:
    for field_name, expected_value in expected.items():
        stored_value = getattr(stored, field_name)
        compared_expected = expected_value
        if field_name == "cutoff_at":
            stored_value = _utc(stored_value)
            compared_expected = _utc(expected_value)
        if stored_value != compared_expected:
            _invalid("persisted rank header does not match immutable replay")


def _assert_exact_rows(
    session: Session,
    rank_snapshot_id: str,
    expected: tuple[dict[str, Any], ...],
) -> None:
    rows = session.scalars(
        select(EquityRankSnapshotRow)
        .where(EquityRankSnapshotRow.rank_snapshot_id == rank_snapshot_id)
        .order_by(EquityRankSnapshotRow.row_ordinal)
    ).all()
    actual = tuple(
        {
            "rank_snapshot_id": row.rank_snapshot_id,
            "instr_id": row.instr_id,
            "factor_snapshot_id": row.factor_snapshot_id,
            "eligible": row.eligible,
            "strategy_eligible": row.strategy_eligible,
            "row_ordinal": row.row_ordinal,
            "rank_position": row.rank_position,
            "composite_score": row.composite_score,
            "target_allocation_hint": row.target_allocation_hint,
            "decision": row.decision,
            "incumbent": row.incumbent,
            "exclusion_reason": row.exclusion_reason,
        }
        for row in rows
    )
    if actual != expected:
        _invalid("persisted rank rows do not match immutable replay")


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _stored_datetime(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _invalid(message: str) -> NoReturn:
    raise EquityRankSnapshotPersistenceError(message)


__all__ = [
    "EquityRankSnapshotPersistenceError",
    "PersistedEquityRankSnapshot",
    "persist_equity_rank_snapshot",
]
