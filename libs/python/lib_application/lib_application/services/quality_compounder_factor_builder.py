"""Build exact US Quality Compounder factor snapshot submissions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import NoReturn

from lib_application.services.equity_factor_snapshots import (
    EquityEvidenceReference,
    EquityEvidenceRole,
    EquityFactorDetailInput,
    EquityFactorSnapshotSubmission,
    EquityFactorState,
)
from lib_strategy.cross_sectional import (
    CrossSectionalEntity,
    CrossSectionalRanker,
    CrossSectionalSnapshot,
    FactorContribution,
    FactorDirection,
    FactorObservation,
    MissingFactorDecision,
    MissingFactorReason,
)
from lib_strategy.equity_optional_factors import optional_factor_source_registry_sha256
from lib_strategy.equity_quality_compounder import (
    QUALITY_COMPOUNDER_CALCULATION_VERSION,
    QUALITY_COMPOUNDER_FACTOR_SPECS,
    QUALITY_COMPOUNDER_MINIMUM_PEER_COUNT,
    QUALITY_COMPOUNDER_PEER_TAXONOMY_VERSION,
    QUALITY_COMPOUNDER_REQUIRED_FACTOR_VERSIONS,
    QUALITY_COMPOUNDER_WINSORIZE_LIMIT,
)

_DETAIL_VALUE_QUANTUM = Decimal("0.000000000000000001")
_DETAIL_WEIGHT_QUANTUM = Decimal("0.000000000001")


class QualityCompounderFactorBuildError(ValueError):
    """A raw factor panel cannot satisfy the registered strategy contract."""


def _invalid(message: str) -> NoReturn:
    raise QualityCompounderFactorBuildError(message)


@dataclass(frozen=True, slots=True)
class QualityCompounderFactorMember:
    """Permanent strategy identity mapped to one catalogue instrument."""

    entity: CrossSectionalEntity
    instrument_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.entity, CrossSectionalEntity):
            _invalid("factor member entity must be a CrossSectionalEntity")
        if (
            isinstance(self.instrument_id, bool)
            or not isinstance(self.instrument_id, int)
            or self.instrument_id < 1
        ):
            _invalid("factor member instrument_id must be a positive integer")
        if not self.entity.peer_groups:
            _invalid("factor member requires point-in-time peer groups")


@dataclass(frozen=True, slots=True)
class QualityCompounderFactorBuild:
    """Rank arithmetic plus one immutable submission for every member."""

    snapshot: CrossSectionalSnapshot
    submissions: tuple[EquityFactorSnapshotSubmission, ...]


def build_quality_compounder_factor_submissions(
    *,
    strategy_id: str,
    strategy_version_id: int,
    effective_session: date,
    cutoff_at: datetime,
    configuration_digest: str,
    members: Sequence[QualityCompounderFactorMember],
    observations: Sequence[FactorObservation],
) -> QualityCompounderFactorBuild:
    """Build exactly four auditable factor details for every effective member."""

    canonical_members = tuple(
        sorted(members, key=lambda item: (item.entity.symbol, item.entity.entity_id))
    )
    if not canonical_members:
        _invalid("factor build requires at least one member")
    entity_ids = tuple(item.entity.entity_id for item in canonical_members)
    instrument_ids = tuple(item.instrument_id for item in canonical_members)
    if len(set(entity_ids)) != len(entity_ids) or len(set(instrument_ids)) != len(instrument_ids):
        _invalid("factor member identities and instrument ids must be unique")

    snapshot = CrossSectionalRanker(
        minimum_peer_count=QUALITY_COMPOUNDER_MINIMUM_PEER_COUNT,
        winsorize_limit=QUALITY_COMPOUNDER_WINSORIZE_LIMIT,
        calculation_version=QUALITY_COMPOUNDER_CALCULATION_VERSION,
    ).rank(
        cutoff=cutoff_at,
        entities=tuple(item.entity for item in canonical_members),
        factor_specs=QUALITY_COMPOUNDER_FACTOR_SPECS,
        observations=observations,
    )
    contribution_by_key = {
        (item.entity_id, item.factor_name): item for item in snapshot.contributions
    }
    missing_by_key = {
        (item.entity_id, item.factor_name): item for item in snapshot.missing_decisions
    }
    observation_by_key = {(item.entity_id, item.factor_name): item for item in observations}
    factor_versions = dict(QUALITY_COMPOUNDER_REQUIRED_FACTOR_VERSIONS)
    submissions = tuple(
        EquityFactorSnapshotSubmission(
            strategy_id=strategy_id,
            strategy_version_id=strategy_version_id,
            instrument_id=member.instrument_id,
            effective_session=effective_session,
            cutoff_at=cutoff_at,
            calculation_version=QUALITY_COMPOUNDER_CALCULATION_VERSION,
            configuration_digest=configuration_digest,
            source_contract_registry_sha256=optional_factor_source_registry_sha256(),
            peer_taxonomy_version=QUALITY_COMPOUNDER_PEER_TAXONOMY_VERSION,
            peer_group=member.entity.peer_groups[0],
            details=tuple(
                _detail(
                    entity_id=member.entity.entity_id,
                    factor_name=spec.name,
                    factor_version=factor_versions[spec.name],
                    weight=Decimal(str(spec.weight)),
                    direction=spec.direction,
                    contribution=contribution_by_key.get((member.entity.entity_id, spec.name)),
                    missing=missing_by_key.get((member.entity.entity_id, spec.name)),
                    observation=observation_by_key.get((member.entity.entity_id, spec.name)),
                )
                for spec in sorted(QUALITY_COMPOUNDER_FACTOR_SPECS, key=lambda item: item.name)
            ),
        )
        for member in canonical_members
    )
    return QualityCompounderFactorBuild(snapshot=snapshot, submissions=submissions)


def _detail(
    *,
    entity_id: str,
    factor_name: str,
    factor_version: str,
    weight: Decimal,
    direction: FactorDirection,
    contribution: FactorContribution | None,
    missing: MissingFactorDecision | None,
    observation: FactorObservation | None,
) -> EquityFactorDetailInput:
    if not isinstance(direction, FactorDirection):
        _invalid("registered factor direction is invalid")
    if isinstance(contribution, FactorContribution):
        persisted_weight = weight.quantize(_DETAIL_WEIGHT_QUANTUM)
        normalized = Decimal(str(contribution.normalized_value)).quantize(_DETAIL_VALUE_QUANTUM)
        return EquityFactorDetailInput(
            factor_name=factor_name,
            sleeve_name=factor_name,
            factor_version=factor_version,
            direction=direction,
            enabled=True,
            state=EquityFactorState.COMPLETE,
            weight=persisted_weight,
            evidence=_evidence(contribution.source_observation_ids),
            raw_value=Decimal(str(contribution.raw_value)),
            peer_group=contribution.peer_group,
            peer_count=contribution.peer_count,
            peer_center=Decimal(str(contribution.peer_center)),
            peer_scale=Decimal(str(contribution.peer_scale)),
            peer_scale_method=contribution.peer_scale_method,
            unbounded_normalized_value=Decimal(str(contribution.unbounded_normalized_value)),
            normalized_value=normalized,
            factor_rank=Decimal(str(contribution.factor_rank)),
            contribution=(persisted_weight * normalized).quantize(_DETAIL_VALUE_QUANTUM),
        )
    if not isinstance(missing, MissingFactorDecision):
        _invalid(f"factor disposition missing for {entity_id}.{factor_name}")
    state = (
        EquityFactorState.INSUFFICIENT_PEERS
        if missing.reason is MissingFactorReason.INSUFFICIENT_PEERS
        else EquityFactorState.MISSING
    )
    return EquityFactorDetailInput(
        factor_name=factor_name,
        sleeve_name=factor_name,
        factor_version=factor_version,
        direction=direction,
        enabled=True,
        state=state,
        weight=weight,
        evidence=_evidence(missing.source_observation_ids),
        raw_value=(
            Decimal(str(observation.raw_value))
            if observation is not None and observation.raw_value is not None
            else None
        ),
        missing_reason=missing.detail,
    )


def _evidence(observation_ids: tuple[str, ...]) -> tuple[EquityEvidenceReference, ...]:
    return tuple(
        EquityEvidenceReference(
            observation_id=observation_id,
            role=EquityEvidenceRole.PRIMARY,
        )
        for observation_id in sorted(observation_ids)
    )


__all__ = [
    "QualityCompounderFactorBuild",
    "QualityCompounderFactorBuildError",
    "QualityCompounderFactorMember",
    "build_quality_compounder_factor_submissions",
]
