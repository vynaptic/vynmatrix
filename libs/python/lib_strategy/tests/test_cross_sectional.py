"""Pure tests for the provider-neutral cross-sectional ranking kernel."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from lib_strategy.cross_sectional import (
    UNIVERSE_PEER_GROUP,
    CrossSectionalEntity,
    CrossSectionalRanker,
    FactorContribution,
    FactorDirection,
    FactorObservation,
    FactorSpec,
    MissingFactorReason,
    PeerFallbackPolicy,
    PeerScaleMethod,
    average_ranks,
    winsorize,
)

_CUTOFF = datetime(2025, 1, 31, 21, 0, tzinfo=UTC)


def _entities() -> tuple[CrossSectionalEntity, ...]:
    return (
        CrossSectionalEntity(
            entity_id="instrument-aapl",
            symbol="AAPL",
            peer_groups=("industry:hardware", "sector:technology"),
        ),
        CrossSectionalEntity(
            entity_id="instrument-msft",
            symbol="MSFT",
            peer_groups=("industry:software", "sector:technology"),
        ),
        CrossSectionalEntity(
            entity_id="instrument-nvda",
            symbol="NVDA",
            peer_groups=("industry:semiconductors", "sector:technology"),
        ),
        CrossSectionalEntity(
            entity_id="instrument-jpm",
            symbol="JPM",
            peer_groups=("industry:banks", "sector:financials"),
        ),
    )


def _specs() -> tuple[FactorSpec, ...]:
    return (
        FactorSpec(name="momentum", weight=0.6),
        FactorSpec(
            name="downside_risk",
            weight=0.4,
            direction=FactorDirection.LOWER_IS_BETTER,
        ),
        FactorSpec(name="licensed_news", weight=0.0, enabled=False),
    )


def _observations() -> tuple[FactorObservation, ...]:
    values = {
        "instrument-aapl": (0.20, 0.15),
        "instrument-msft": (0.15, 0.10),
        "instrument-nvda": (0.30, 0.25),
        "instrument-jpm": (0.10, 0.08),
    }
    return tuple(
        observation
        for entity_id, (momentum, downside_risk) in values.items()
        for observation in (
            FactorObservation(
                entity_id=entity_id,
                factor_name="momentum",
                raw_value=momentum,
                source_observation_ids=(f"price:{entity_id}",),
            ),
            FactorObservation(
                entity_id=entity_id,
                factor_name="downside_risk",
                raw_value=downside_risk,
                source_observation_ids=(f"risk:{entity_id}",),
            ),
        )
    )


def test_winsorize_promoted_scalar_behavior_is_finite_and_deterministic() -> None:
    assert winsorize(5.0, 3.0) == 3.0
    assert winsorize(-5.0, 3.0) == -3.0
    assert winsorize(1.5, 3.0) == 1.5
    assert winsorize(9.0, 0.0) == 9.0
    with pytest.raises(ValueError, match="finite"):
        winsorize(float("nan"), 3.0)


def test_average_ranks_assign_ties_their_average_independent_of_input_order() -> None:
    forward = {"AAPL": 2.0, "MSFT": 2.0, "NVDA": 4.0, "JPM": 1.0}
    reverse = dict(reversed(tuple(forward.items())))

    assert (
        average_ranks(forward)
        == average_ranks(reverse)
        == {
            "NVDA": 1.0,
            "AAPL": 2.5,
            "MSFT": 2.5,
            "JPM": 4.0,
        }
    )
    assert average_ranks(forward, descending=False) == {
        "JPM": 1.0,
        "AAPL": 2.5,
        "MSFT": 2.5,
        "NVDA": 4.0,
    }


@pytest.mark.parametrize("invalid_value", [float("nan"), float("inf"), -float("inf")])
def test_factor_observations_reject_non_finite_values(invalid_value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        FactorObservation(
            entity_id="instrument-aapl", factor_name="momentum", raw_value=invalid_value
        )


def test_factor_observation_requires_an_explicit_missing_reason() -> None:
    with pytest.raises(ValueError, match="missing_reason"):
        FactorObservation(entity_id="instrument-aapl", factor_name="quality", raw_value=None)
    with pytest.raises(ValueError, match="must be absent"):
        FactorObservation(
            entity_id="instrument-aapl",
            factor_name="quality",
            raw_value=1.0,
            missing_reason="not reported",
        )


def test_rank_snapshot_is_invariant_to_entity_factor_and_observation_order() -> None:
    ranker = CrossSectionalRanker(minimum_peer_count=3)
    forward = ranker.rank(
        cutoff=_CUTOFF,
        entities=_entities(),
        factor_specs=_specs(),
        observations=_observations(),
    )
    reverse = ranker.rank(
        cutoff=_CUTOFF,
        entities=tuple(reversed(_entities())),
        factor_specs=tuple(reversed(_specs())),
        observations=tuple(reversed(_observations())),
    )

    assert forward.content_digest == reverse.content_digest
    assert forward.to_dict() == reverse.to_dict()
    assert json.dumps(forward.to_dict(), allow_nan=False, sort_keys=True)


def test_peer_normalization_uses_ordered_fallback_and_records_it() -> None:
    snapshot = CrossSectionalRanker(minimum_peer_count=3).rank(
        cutoff=_CUTOFF,
        entities=_entities(),
        factor_specs=_specs(),
        observations=_observations(),
    )
    contributions = {(item.symbol, item.factor_name): item for item in snapshot.contributions}

    assert contributions[("AAPL", "momentum")].peer_group == "sector:technology"
    assert contributions[("AAPL", "momentum")].peer_count == 3
    assert contributions[("JPM", "momentum")].peer_group == UNIVERSE_PEER_GROUP
    assert contributions[("JPM", "momentum")].peer_count == 4


def test_lower_is_better_factor_direction_reverses_factor_rank() -> None:
    snapshot = CrossSectionalRanker(minimum_peer_count=3).rank(
        cutoff=_CUTOFF,
        entities=_entities(),
        factor_specs=_specs(),
        observations=_observations(),
    )
    risk = {
        item.symbol: item for item in snapshot.contributions if item.factor_name == "downside_risk"
    }

    assert risk["MSFT"].factor_rank < risk["AAPL"].factor_rank
    assert risk["MSFT"].normalized_value > risk["AAPL"].normalized_value


def test_missing_enabled_factor_excludes_entity_without_weight_renormalization() -> None:
    observations = [
        observation
        for observation in _observations()
        if not (
            observation.entity_id == "instrument-aapl"
            and observation.factor_name == "downside_risk"
        )
    ]
    observations.append(
        FactorObservation(
            entity_id="instrument-aapl",
            factor_name="downside_risk",
            raw_value=None,
            missing_reason="required filing fact unavailable at cutoff",
            source_observation_ids=("filing:aapl:2024q4",),
        )
    )

    snapshot = CrossSectionalRanker(minimum_peer_count=2).rank(
        cutoff=_CUTOFF,
        entities=_entities(),
        factor_specs=_specs(),
        observations=observations,
    )

    assert "AAPL" not in {item.symbol for item in snapshot.ranks}
    missing = next(
        item
        for item in snapshot.missing_decisions
        if item.symbol == "AAPL" and item.factor_name == "downside_risk"
    )
    assert missing.reason is MissingFactorReason.SOURCE_MISSING
    assert missing.source_observation_ids == ("filing:aapl:2024q4",)
    momentum = next(
        item
        for item in snapshot.contributions
        if item.symbol == "AAPL" and item.factor_name == "momentum"
    )
    assert momentum.weight == 0.6
    assert momentum.contribution == pytest.approx(momentum.normalized_value * 0.6)


def test_absent_enabled_factor_is_distinct_from_source_reported_missing() -> None:
    observations = [
        observation
        for observation in _observations()
        if not (
            observation.entity_id == "instrument-jpm" and observation.factor_name == "downside_risk"
        )
    ]
    snapshot = CrossSectionalRanker(minimum_peer_count=2).rank(
        cutoff=_CUTOFF,
        entities=_entities(),
        factor_specs=_specs(),
        observations=observations,
    )

    missing = next(
        item
        for item in snapshot.missing_decisions
        if item.symbol == "JPM" and item.factor_name == "downside_risk"
    )
    assert missing.reason is MissingFactorReason.NOT_SUPPLIED


def test_insufficient_peer_evidence_fails_closed() -> None:
    entities = _entities()[:2]
    snapshot = CrossSectionalRanker(minimum_peer_count=2).rank(
        cutoff=_CUTOFF,
        entities=entities,
        factor_specs=(FactorSpec(name="quality", weight=1.0),),
        observations=(
            FactorObservation(
                entity_id="instrument-aapl",
                factor_name="quality",
                raw_value=1.0,
            ),
        ),
    )

    assert snapshot.ranks == ()
    decisions = {(item.symbol, item.reason) for item in snapshot.missing_decisions}
    assert decisions == {
        ("AAPL", MissingFactorReason.INSUFFICIENT_PEERS),
        ("MSFT", MissingFactorReason.NOT_SUPPLIED),
    }


def test_disabled_factor_is_recorded_but_cannot_contribute_or_gate() -> None:
    snapshot = CrossSectionalRanker(minimum_peer_count=3).rank(
        cutoff=_CUTOFF,
        entities=_entities(),
        factor_specs=_specs(),
        observations=_observations(),
    )

    disabled = next(spec for spec in snapshot.factor_specs if spec.name == "licensed_news")
    assert disabled.enabled is False
    assert disabled.weight == 0.0
    assert all(item.factor_name != "licensed_news" for item in snapshot.contributions)
    assert all(item.factor_name != "licensed_news" for item in snapshot.missing_decisions)
    assert len(snapshot.ranks) == len(_entities())


def test_disabled_factor_rejects_supplied_observations() -> None:
    observations = (
        *_observations(),
        FactorObservation(
            entity_id="instrument-aapl",
            factor_name="licensed_news",
            raw_value=0.9,
        ),
    )

    with pytest.raises(ValueError, match="disabled factor"):
        CrossSectionalRanker(minimum_peer_count=3).rank(
            cutoff=_CUTOFF,
            entities=_entities(),
            factor_specs=_specs(),
            observations=observations,
        )


def test_enabled_zero_weight_factor_can_gate_but_cannot_change_composite() -> None:
    specs = (
        FactorSpec(name="momentum", weight=1.0),
        FactorSpec(name="quality_gate", weight=0.0),
    )
    observations = tuple(
        observation
        for entity, momentum in zip(_entities(), (0.2, 0.1, 0.3, 0.0), strict=True)
        for observation in (
            FactorObservation(
                entity_id=entity.entity_id,
                factor_name="momentum",
                raw_value=momentum,
            ),
            FactorObservation(
                entity_id=entity.entity_id,
                factor_name="quality_gate",
                raw_value=1.0,
            ),
        )
    )
    snapshot = CrossSectionalRanker(minimum_peer_count=4).rank(
        cutoff=_CUTOFF,
        entities=_entities(),
        factor_specs=specs,
        observations=observations,
    )

    for rank in snapshot.ranks:
        contributions = {
            item.factor_name: item
            for item in snapshot.contributions
            if item.entity_id == rank.entity_id
        }
        assert contributions["quality_gate"].contribution == 0.0
        assert rank.composite_score == contributions["momentum"].contribution


def test_global_peer_fallback_is_explicit_and_can_fail_closed() -> None:
    entities = (
        CrossSectionalEntity("instrument-aapl", "AAPL", ("industry:hardware",)),
        CrossSectionalEntity("instrument-msft", "MSFT", ("industry:software",)),
    )
    observations = (
        FactorObservation("instrument-aapl", "momentum", 0.2),
        FactorObservation("instrument-msft", "momentum", 0.1),
    )
    specs = (FactorSpec(name="momentum", weight=1.0),)
    global_snapshot = CrossSectionalRanker(
        minimum_peer_count=2,
        fallback_policy=PeerFallbackPolicy.GLOBAL,
    ).rank(
        cutoff=_CUTOFF,
        entities=entities,
        factor_specs=specs,
        observations=observations,
    )
    ineligible_snapshot = CrossSectionalRanker(
        minimum_peer_count=2,
        fallback_policy=PeerFallbackPolicy.INELIGIBLE,
    ).rank(
        cutoff=_CUTOFF,
        entities=entities,
        factor_specs=specs,
        observations=observations,
    )

    assert len(global_snapshot.ranks) == 2
    assert ineligible_snapshot.ranks == ()
    assert {item.reason for item in ineligible_snapshot.missing_decisions} == {
        MissingFactorReason.INSUFFICIENT_PEERS
    }
    assert global_snapshot.content_digest != ineligible_snapshot.content_digest


@pytest.mark.parametrize(
    "factor_specs",
    [
        (FactorSpec(name="momentum", weight=0.9),),
        (
            FactorSpec(name="momentum", weight=0.5),
            FactorSpec(name="quality", weight=0.5),
            FactorSpec(name="quality", weight=0.0, enabled=False),
        ),
    ],
)
def test_ranker_rejects_invalid_composite_weight_contract(
    factor_specs: tuple[FactorSpec, ...],
) -> None:
    with pytest.raises(ValueError, match=r"sum to one|unique"):
        CrossSectionalRanker(minimum_peer_count=2).rank(
            cutoff=_CUTOFF,
            entities=_entities(),
            factor_specs=factor_specs,
            observations=(),
        )


def test_disabled_factor_rejects_nonzero_weight() -> None:
    with pytest.raises(ValueError, match="must be zero"):
        FactorSpec(name="licensed_news", weight=0.1, enabled=False)


def test_contribution_contract_rejects_inconsistent_arithmetic() -> None:
    with pytest.raises(ValueError, match=r"weight \* normalized_value"):
        FactorContribution(
            entity_id="instrument-aapl",
            symbol="AAPL",
            factor_name="momentum",
            raw_value=0.1,
            peer_group=UNIVERSE_PEER_GROUP,
            peer_count=3,
            peer_center=0.0,
            peer_scale=1.0,
            peer_scale_method=PeerScaleMethod.MEDIAN_ABSOLUTE_DEVIATION,
            unbounded_normalized_value=1.0,
            normalized_value=1.0,
            factor_rank=1.0,
            weight=0.4,
            contribution=0.5,
        )


def test_composite_score_equals_recorded_contributions() -> None:
    snapshot = CrossSectionalRanker(minimum_peer_count=3).rank(
        cutoff=_CUTOFF,
        entities=_entities(),
        factor_specs=_specs(),
        observations=_observations(),
    )
    for rank in snapshot.ranks:
        expected = sum(
            item.contribution for item in snapshot.contributions if item.entity_id == rank.entity_id
        )
        assert rank.composite_score == pytest.approx(expected)


def test_equal_composites_keep_average_rank_and_symbol_only_orders_output() -> None:
    entities = _entities()
    observations = tuple(
        FactorObservation(entity_id=entity.entity_id, factor_name="quality", raw_value=1.0)
        for entity in entities
    )
    snapshot = CrossSectionalRanker(minimum_peer_count=2).rank(
        cutoff=_CUTOFF,
        entities=entities,
        factor_specs=(FactorSpec(name="quality", weight=1.0),),
        observations=observations,
    )

    assert [item.symbol for item in snapshot.ranks] == ["AAPL", "JPM", "MSFT", "NVDA"]
    assert {item.rank for item in snapshot.ranks} == {2.5}
    assert {item.composite_score for item in snapshot.ranks} == {0.0}
    assert {item.peer_scale_method for item in snapshot.contributions} == {PeerScaleMethod.CONSTANT}


def test_zero_mad_uses_recorded_standard_deviation_fallback() -> None:
    entities = _entities()
    values = (1.0, 1.0, 1.0, 2.0)
    snapshot = CrossSectionalRanker(minimum_peer_count=4).rank(
        cutoff=_CUTOFF,
        entities=entities,
        factor_specs=(FactorSpec(name="quality", weight=1.0),),
        observations=tuple(
            FactorObservation(
                entity_id=entity.entity_id,
                factor_name="quality",
                raw_value=value,
            )
            for entity, value in zip(entities, values, strict=True)
        ),
    )

    assert {item.peer_scale_method for item in snapshot.contributions} == {
        PeerScaleMethod.STANDARD_DEVIATION_FALLBACK
    }


def test_robust_normalization_winsorizes_outlier_before_contribution() -> None:
    entities = _entities()
    values = (0.0, 1.0, 2.0, 100.0)
    snapshot = CrossSectionalRanker(minimum_peer_count=2, winsorize_limit=2.5).rank(
        cutoff=_CUTOFF,
        entities=entities,
        factor_specs=(FactorSpec(name="momentum", weight=1.0),),
        observations=tuple(
            FactorObservation(
                entity_id=entity.entity_id,
                factor_name="momentum",
                raw_value=value,
            )
            for entity, value in zip(entities, values, strict=True)
        ),
    )
    outlier = next(item for item in snapshot.contributions if item.raw_value == 100.0)

    assert outlier.unbounded_normalized_value > 2.5
    assert outlier.normalized_value == 2.5
    assert outlier.contribution == 2.5
