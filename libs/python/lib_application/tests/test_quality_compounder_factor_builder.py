"""Tests for the quality-compounder factor snapshot materializer."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from lib_application.services.equity_factor_snapshots import (
    EquityEvidenceRole,
    EquityFactorState,
)
from lib_application.services.quality_compounder_factor_builder import (
    QualityCompounderFactorMember,
    build_quality_compounder_factor_submissions,
)
from lib_strategy.cross_sectional import CrossSectionalEntity, FactorObservation
from lib_strategy.equity_quality_compounder import (
    QUALITY_COMPOUNDER_REQUIRED_FACTOR_VERSIONS,
)

_CUTOFF = datetime(2026, 6, 30, 22, 0, tzinfo=UTC)


def _members(count: int = 6) -> tuple[QualityCompounderFactorMember, ...]:
    return tuple(
        QualityCompounderFactorMember(
            entity=CrossSectionalEntity(
                entity_id=f"security-{index}",
                symbol=f"S{index}",
                peer_groups=("industry:shared", "sector:shared"),
            ),
            instrument_id=index + 1,
        )
        for index in range(count)
    )


def _observations(
    members: tuple[QualityCompounderFactorMember, ...],
    *,
    omit: tuple[str, str] | None = None,
) -> tuple[FactorObservation, ...]:
    return tuple(
        FactorObservation(
            entity_id=member.entity.entity_id,
            factor_name=factor_name,
            raw_value=float(index + 1),
            source_observation_ids=(f"{index * 10 + factor_index + 1:064x}",),
        )
        for index, member in enumerate(members)
        for factor_index, (factor_name, _version) in enumerate(
            QUALITY_COMPOUNDER_REQUIRED_FACTOR_VERSIONS
        )
        if omit != (member.entity.entity_id, factor_name)
    )


def test_builder_emits_exactly_four_complete_details_per_member() -> None:
    members = _members()
    result = build_quality_compounder_factor_submissions(
        strategy_id="us_quality_compounder_v1",
        strategy_version_id=7,
        effective_session=date(2026, 6, 30),
        cutoff_at=_CUTOFF,
        configuration_digest="a" * 64,
        members=members,
        observations=_observations(members),
    )

    assert len(result.submissions) == len(members)
    assert all(len(item.details) == 4 for item in result.submissions)
    assert all(
        tuple(detail.factor_name for detail in item.details)
        == tuple(name for name, _version in QUALITY_COMPOUNDER_REQUIRED_FACTOR_VERSIONS)
        for item in result.submissions
    )
    assert all(
        detail.state is EquityFactorState.COMPLETE
        for item in result.submissions
        for detail in item.details
    )
    assert all(
        detail.contribution == detail.weight * detail.normalized_value
        for item in result.submissions
        for detail in item.details
    )
    assert all(
        reference.role is EquityEvidenceRole.PRIMARY
        for item in result.submissions
        for detail in item.details
        for reference in detail.evidence
    )


def test_builder_multiplies_the_persisted_normalized_precision() -> None:
    members = _members()
    raw_values = (-2.0, -1.0, 0.0, 2e-16, 1.0, 2.0)
    observations = tuple(
        FactorObservation(
            entity_id=member.entity.entity_id,
            factor_name=factor_name,
            raw_value=raw_values[index],
            source_observation_ids=(f"{index * 10 + factor_index + 1:064x}",),
        )
        for index, member in enumerate(members)
        for factor_index, (factor_name, _version) in enumerate(
            QUALITY_COMPOUNDER_REQUIRED_FACTOR_VERSIONS
        )
    )

    result = build_quality_compounder_factor_submissions(
        strategy_id="us_quality_compounder_v1",
        strategy_version_id=7,
        effective_session=date(2026, 6, 30),
        cutoff_at=_CUTOFF,
        configuration_digest="a" * 64,
        members=members,
        observations=observations,
    )

    assert all(
        detail.contribution
        == (detail.weight * detail.normalized_value).quantize(Decimal("0.000000000000000001"))
        for submission in result.submissions
        for detail in submission.details
    )


def test_builder_persists_missing_factor_without_neutral_fill() -> None:
    members = _members()
    missing_key = (members[0].entity.entity_id, "quality")
    result = build_quality_compounder_factor_submissions(
        strategy_id="us_quality_compounder_v1",
        strategy_version_id=7,
        effective_session=date(2026, 6, 30),
        cutoff_at=_CUTOFF,
        configuration_digest="a" * 64,
        members=members,
        observations=_observations(members, omit=missing_key),
    )

    missing = next(
        detail for detail in result.submissions[0].details if detail.factor_name == "quality"
    )
    assert missing.state is EquityFactorState.MISSING
    assert missing.raw_value is None
    assert missing.normalized_value is None
    assert missing.contribution is None
    assert missing.missing_reason == "enabled factor observation was not supplied"
