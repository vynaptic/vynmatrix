"""Provider-neutral cross-sectional factor ranking domain kernel.

The kernel accepts already cutoff-safe observations. Acquisition, persistence,
asset taxonomy, and strategy eligibility remain outside this module. Missing
enabled evidence is recorded explicitly and never zero-filled or renormalized.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from statistics import median, pstdev
from typing import NoReturn

from lib_common.hashing import canonical_json_hash

_DIGEST_VERSION = "cross-sectional-rank-v1"
_MAD_TO_NORMAL_SCALE = 1.482602218505602
_MINIMUM_PEER_COUNT = 2
_WEIGHT_TOLERANCE = 1e-12

UNIVERSE_PEER_GROUP = "__universe__"


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)


def _identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _invalid(f"{field_name} must be a non-blank, trimmed string")
    return value


def _finite(value: float, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(f"{field_name} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        _invalid(f"{field_name} must be a finite number")
    return 0.0 if converted == 0.0 else converted


def _canonical_source_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        _invalid("source_observation_ids must be a tuple")
    canonical = tuple(sorted(values))
    if any(_identifier(value, field_name="source_observation_id") != value for value in canonical):
        _invalid("source_observation_ids contain an invalid identity")
    if len(set(canonical)) != len(canonical):
        _invalid("source_observation_ids must be unique")
    return canonical


def _utc_cutoff(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid("cutoff must be timezone-aware")
    return value.astimezone(UTC)


class FactorDirection(StrEnum):
    """Whether a larger or smaller raw observation is preferred."""

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class MissingFactorReason(StrEnum):
    """Fail-closed reasons why an enabled factor cannot contribute."""

    INSUFFICIENT_PEERS = "insufficient_peers"
    NOT_SUPPLIED = "not_supplied"
    SOURCE_MISSING = "source_missing"


class PeerFallbackPolicy(StrEnum):
    """Declared behavior after every explicit peer group is insufficient."""

    GLOBAL = "global"
    INELIGIBLE = "ineligible"


class PeerScaleMethod(StrEnum):
    """Dispersion estimator selected for one peer normalization."""

    CONSTANT = "constant"
    MEDIAN_ABSOLUTE_DEVIATION = "median_absolute_deviation"
    STANDARD_DEVIATION_FALLBACK = "standard_deviation_fallback"


@dataclass(frozen=True, slots=True)
class CrossSectionalEntity:
    """Canonical entity identity and ordered peer fallback hierarchy."""

    entity_id: str
    symbol: str
    peer_groups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.entity_id, field_name="entity_id")
        _identifier(self.symbol, field_name="symbol")
        if not isinstance(self.peer_groups, tuple):
            _invalid("peer_groups must be a tuple")
        for peer_group in self.peer_groups:
            _identifier(peer_group, field_name="peer_group")
            if peer_group == UNIVERSE_PEER_GROUP:
                _invalid(f"{UNIVERSE_PEER_GROUP} is reserved for the deterministic fallback")
        if len(set(self.peer_groups)) != len(self.peer_groups):
            _invalid("peer_groups must be unique and ordered from narrowest to broadest")


@dataclass(frozen=True, slots=True)
class FactorSpec:
    """Immutable scoring behavior for one factor in a composite."""

    name: str
    weight: float
    enabled: bool = True
    direction: FactorDirection = FactorDirection.HIGHER_IS_BETTER

    def __post_init__(self) -> None:
        _identifier(self.name, field_name="factor name")
        weight = _finite(self.weight, field_name=f"factor {self.name!r} weight")
        object.__setattr__(self, "weight", weight)
        if not isinstance(self.enabled, bool):
            _invalid(f"factor {self.name!r} enabled must be boolean")
        if not isinstance(self.direction, FactorDirection):
            _invalid(f"factor {self.name!r} direction must be a FactorDirection")
        if self.enabled and weight < 0.0:
            _invalid(f"enabled factor {self.name!r} weight must be non-negative")
        if not self.enabled and weight != 0.0:
            _invalid(f"disabled factor {self.name!r} weight must be zero")

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe factor configuration."""

        return {
            "direction": self.direction.value,
            "enabled": self.enabled,
            "name": self.name,
            "weight": self.weight,
        }


@dataclass(frozen=True, slots=True)
class FactorObservation:
    """One raw factor observation or explicit source-level missing result."""

    entity_id: str
    factor_name: str
    raw_value: float | None
    missing_reason: str | None = None
    source_observation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.entity_id, field_name="entity_id")
        _identifier(self.factor_name, field_name="factor_name")
        canonical_sources = _canonical_source_ids(self.source_observation_ids)
        object.__setattr__(self, "source_observation_ids", canonical_sources)
        if self.raw_value is None:
            if self.missing_reason is None:
                _invalid("missing_reason is required when raw_value is absent")
            _identifier(self.missing_reason, field_name="missing_reason")
            return
        value = _finite(
            self.raw_value,
            field_name=f"{self.entity_id}.{self.factor_name}.raw_value",
        )
        if self.missing_reason is not None:
            _invalid("missing_reason must be absent when raw_value is present")
        object.__setattr__(self, "raw_value", value)


@dataclass(frozen=True, slots=True)
class MissingFactorDecision:
    """Auditable reason an enabled factor made one entity incomplete."""

    entity_id: str
    symbol: str
    factor_name: str
    reason: MissingFactorReason
    detail: str
    source_observation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.entity_id, field_name="entity_id")
        _identifier(self.symbol, field_name="symbol")
        _identifier(self.factor_name, field_name="factor_name")
        if not isinstance(self.reason, MissingFactorReason):
            _invalid("reason must be a MissingFactorReason")
        _identifier(self.detail, field_name="detail")
        object.__setattr__(
            self,
            "source_observation_ids",
            _canonical_source_ids(self.source_observation_ids),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic missing-evidence ledger entry."""

        return {
            "detail": self.detail,
            "entity_id": self.entity_id,
            "factor_name": self.factor_name,
            "reason": self.reason.value,
            "source_observation_ids": list(self.source_observation_ids),
            "symbol": self.symbol,
        }


@dataclass(frozen=True, slots=True)
class FactorContribution:
    """Complete arithmetic and peer context for one factor contribution."""

    entity_id: str
    symbol: str
    factor_name: str
    raw_value: float
    peer_group: str
    peer_count: int
    peer_center: float
    peer_scale: float
    peer_scale_method: PeerScaleMethod
    unbounded_normalized_value: float
    normalized_value: float
    factor_rank: float
    weight: float
    contribution: float
    source_observation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.entity_id, field_name="entity_id")
        _identifier(self.symbol, field_name="symbol")
        _identifier(self.factor_name, field_name="factor_name")
        _identifier(self.peer_group, field_name="peer_group")
        if isinstance(self.peer_count, bool) or not isinstance(self.peer_count, int):
            _invalid("peer_count must be an integer")
        if self.peer_count < 1:
            _invalid("peer_count must be positive")
        if not isinstance(self.peer_scale_method, PeerScaleMethod):
            _invalid("peer_scale_method must be a PeerScaleMethod")
        numeric_fields = {
            "raw_value": self.raw_value,
            "peer_center": self.peer_center,
            "peer_scale": self.peer_scale,
            "unbounded_normalized_value": self.unbounded_normalized_value,
            "normalized_value": self.normalized_value,
            "factor_rank": self.factor_rank,
            "weight": self.weight,
            "contribution": self.contribution,
        }
        for field_name, value in numeric_fields.items():
            object.__setattr__(self, field_name, _finite(value, field_name=field_name))
        if self.peer_scale < 0.0:
            _invalid("peer_scale must be non-negative")
        if self.factor_rank < 1.0:
            _invalid("factor_rank must be at least one")
        if self.weight < 0.0:
            _invalid("contribution weight must be non-negative")
        expected = self.weight * self.normalized_value
        if not math.isclose(
            self.contribution,
            expected,
            rel_tol=_WEIGHT_TOLERANCE,
            abs_tol=_WEIGHT_TOLERANCE,
        ):
            _invalid("contribution must equal weight * normalized_value")
        object.__setattr__(
            self,
            "source_observation_ids",
            _canonical_source_ids(self.source_observation_ids),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic contribution ledger entry."""

        return {
            "contribution": self.contribution,
            "entity_id": self.entity_id,
            "factor_name": self.factor_name,
            "factor_rank": self.factor_rank,
            "normalized_value": self.normalized_value,
            "peer_center": self.peer_center,
            "peer_count": self.peer_count,
            "peer_group": self.peer_group,
            "peer_scale": self.peer_scale,
            "peer_scale_method": self.peer_scale_method.value,
            "raw_value": self.raw_value,
            "source_observation_ids": list(self.source_observation_ids),
            "symbol": self.symbol,
            "unbounded_normalized_value": self.unbounded_normalized_value,
            "weight": self.weight,
        }


@dataclass(frozen=True, slots=True)
class InstrumentRank:
    """One complete entity's composite score and average-rank position."""

    entity_id: str
    symbol: str
    composite_score: float
    rank: float

    def __post_init__(self) -> None:
        _identifier(self.entity_id, field_name="entity_id")
        _identifier(self.symbol, field_name="symbol")
        object.__setattr__(
            self,
            "composite_score",
            _finite(self.composite_score, field_name="composite_score"),
        )
        object.__setattr__(self, "rank", _finite(self.rank, field_name="rank"))
        if self.rank < 1.0:
            _invalid("rank must be at least one")

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic rank ledger entry."""

        return {
            "composite_score": self.composite_score,
            "entity_id": self.entity_id,
            "rank": self.rank,
            "symbol": self.symbol,
        }


def _validate_factor_specs(specs: Sequence[FactorSpec]) -> tuple[FactorSpec, ...]:
    canonical = tuple(sorted(specs, key=lambda spec: spec.name))
    names = [spec.name for spec in canonical]
    if not canonical:
        _invalid("at least one factor specification is required")
    if len(set(names)) != len(names):
        _invalid("factor specification names must be unique")
    enabled = [spec for spec in canonical if spec.enabled]
    if not enabled:
        _invalid("at least one factor must be enabled")
    total_weight = math.fsum(spec.weight for spec in enabled)
    if not math.isclose(
        total_weight,
        1.0,
        rel_tol=0.0,
        abs_tol=_WEIGHT_TOLERANCE,
    ):
        _invalid("enabled factor weights must sum to one")
    return canonical


@dataclass(frozen=True, slots=True)
class CrossSectionalSnapshot:
    """Immutable, canonical rank result with an order-invariant digest."""

    cutoff: datetime
    calculation_version: str
    minimum_peer_count: int
    winsorize_limit: float
    fallback_policy: PeerFallbackPolicy
    factor_specs: tuple[FactorSpec, ...]
    contributions: tuple[FactorContribution, ...]
    ranks: tuple[InstrumentRank, ...]
    missing_decisions: tuple[MissingFactorDecision, ...]
    schema_version: str = field(default=_DIGEST_VERSION, init=False)
    content_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cutoff", _utc_cutoff(self.cutoff))
        _identifier(self.calculation_version, field_name="calculation_version")
        if (
            isinstance(self.minimum_peer_count, bool)
            or not isinstance(self.minimum_peer_count, int)
            or self.minimum_peer_count < _MINIMUM_PEER_COUNT
        ):
            _invalid("minimum_peer_count must be an integer of at least two")
        object.__setattr__(
            self,
            "winsorize_limit",
            _finite(self.winsorize_limit, field_name="winsorize_limit"),
        )
        if self.winsorize_limit <= 0.0:
            _invalid("winsorize_limit must be positive")
        if not isinstance(self.fallback_policy, PeerFallbackPolicy):
            _invalid("fallback_policy must be a PeerFallbackPolicy")
        object.__setattr__(self, "factor_specs", _validate_factor_specs(self.factor_specs))
        object.__setattr__(
            self,
            "contributions",
            tuple(
                sorted(
                    self.contributions,
                    key=lambda item: (item.symbol, item.entity_id, item.factor_name),
                )
            ),
        )
        object.__setattr__(
            self,
            "ranks",
            tuple(sorted(self.ranks, key=lambda item: (item.rank, item.symbol, item.entity_id))),
        )
        object.__setattr__(
            self,
            "missing_decisions",
            tuple(
                sorted(
                    self.missing_decisions,
                    key=lambda item: (item.symbol, item.entity_id, item.factor_name),
                )
            ),
        )
        _validate_snapshot_links(self)
        object.__setattr__(self, "content_digest", canonical_json_hash(self._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "calculation_version": self.calculation_version,
            "contributions": [item.to_dict() for item in self.contributions],
            "cutoff": self.cutoff.isoformat(),
            "factor_specs": [item.to_dict() for item in self.factor_specs],
            "fallback_policy": self.fallback_policy.value,
            "minimum_peer_count": self.minimum_peer_count,
            "missing_decisions": [item.to_dict() for item in self.missing_decisions],
            "ranks": [item.to_dict() for item in self.ranks],
            "schema_version": self.schema_version,
            "winsorize_limit": self.winsorize_limit,
        }

    def to_dict(self) -> dict[str, object]:
        """Return a new JSON-safe snapshot payload including its digest."""

        payload = self._payload()
        payload["content_digest"] = self.content_digest
        return payload


def _validate_snapshot_links(snapshot: CrossSectionalSnapshot) -> None:
    enabled_names = {spec.name for spec in snapshot.factor_specs if spec.enabled}
    contribution_keys: set[tuple[str, str]] = set()
    contributions_by_entity: dict[str, list[FactorContribution]] = {}
    symbols_by_entity: dict[str, str] = {}
    for factor_contribution in snapshot.contributions:
        key = (factor_contribution.entity_id, factor_contribution.factor_name)
        if key in contribution_keys:
            _invalid("snapshot contains a duplicate factor contribution")
        if factor_contribution.factor_name not in enabled_names:
            _invalid("snapshot contribution references a disabled or unknown factor")
        contribution_keys.add(key)
        contributions_by_entity.setdefault(factor_contribution.entity_id, []).append(
            factor_contribution
        )
        _record_symbol(
            symbols_by_entity,
            factor_contribution.entity_id,
            factor_contribution.symbol,
        )

    missing_keys: set[tuple[str, str]] = set()
    missing_entities: set[str] = set()
    for missing_decision in snapshot.missing_decisions:
        key = (missing_decision.entity_id, missing_decision.factor_name)
        if key in missing_keys or key in contribution_keys:
            _invalid("snapshot factor result must be exactly one of contribution or missing")
        if missing_decision.factor_name not in enabled_names:
            _invalid("snapshot missing decision references a disabled or unknown factor")
        missing_keys.add(key)
        missing_entities.add(missing_decision.entity_id)
        _record_symbol(
            symbols_by_entity,
            missing_decision.entity_id,
            missing_decision.symbol,
        )

    rank_entities: set[str] = set()
    for instrument_rank in snapshot.ranks:
        if instrument_rank.entity_id in rank_entities:
            _invalid("snapshot contains a duplicate instrument rank")
        if instrument_rank.entity_id in missing_entities:
            _invalid("an instrument with missing enabled evidence cannot be ranked")
        rank_entities.add(instrument_rank.entity_id)
        _record_symbol(
            symbols_by_entity,
            instrument_rank.entity_id,
            instrument_rank.symbol,
        )
        entity_contributions = contributions_by_entity.get(instrument_rank.entity_id, [])
        factor_names = {contribution.factor_name for contribution in entity_contributions}
        if factor_names != enabled_names:
            _invalid("ranked instrument must have one contribution from every enabled factor")
        expected_score = math.fsum(
            contribution.contribution
            for contribution in sorted(entity_contributions, key=lambda value: value.factor_name)
        )
        if not math.isclose(
            instrument_rank.composite_score,
            expected_score,
            rel_tol=_WEIGHT_TOLERANCE,
            abs_tol=_WEIGHT_TOLERANCE,
        ):
            _invalid("composite score must equal the recorded factor contributions")

    unresolved_entities = set(contributions_by_entity) - rank_entities - missing_entities
    if unresolved_entities:
        _invalid("partial factor contributions require a missing decision for the entity")


def _record_symbol(symbols_by_entity: dict[str, str], entity_id: str, symbol: str) -> None:
    previous = symbols_by_entity.setdefault(entity_id, symbol)
    if previous != symbol:
        _invalid("one entity_id cannot reference multiple canonical symbols")


def winsorize(value: float, limit: float) -> float:
    """Clamp a finite scalar to ``+-limit``; a non-positive limit disables clamping."""

    finite_value = _finite(value, field_name="value")
    finite_limit = _finite(limit, field_name="limit")
    if finite_limit <= 0.0:
        return finite_value
    bounded = max(-finite_limit, min(finite_limit, finite_value))
    return 0.0 if bounded == 0.0 else bounded


def average_ranks(values: Mapping[str, float], *, descending: bool = True) -> dict[str, float]:
    """Return deterministic one-based ranks, assigning tied values their average rank."""

    if not isinstance(descending, bool):
        _invalid("descending must be boolean")
    validated = {
        _identifier(key, field_name="rank key"): _finite(value, field_name=f"rank value {key!r}")
        for key, value in values.items()
    }
    ordered = sorted(
        validated.items(),
        key=lambda item: ((-item[1] if descending else item[1]), item[0]),
    )
    result: dict[str, float] = {}
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        average_rank = ((start + 1) + end) / 2.0
        for key, _value in ordered[start:end]:
            result[key] = average_rank
        start = end
    return result


@dataclass(frozen=True, slots=True)
class CrossSectionalRanker:
    """Deterministic peer-normalized composite ranker."""

    minimum_peer_count: int = 5
    winsorize_limit: float = 3.0
    fallback_policy: PeerFallbackPolicy = PeerFallbackPolicy.GLOBAL
    calculation_version: str = _DIGEST_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.minimum_peer_count, bool) or not isinstance(
            self.minimum_peer_count, int
        ):
            _invalid("minimum_peer_count must be an integer")
        if self.minimum_peer_count < _MINIMUM_PEER_COUNT:
            _invalid("minimum_peer_count must be at least two")
        object.__setattr__(
            self,
            "winsorize_limit",
            _finite(self.winsorize_limit, field_name="winsorize_limit"),
        )
        if self.winsorize_limit <= 0.0:
            _invalid("winsorize_limit must be positive")
        if not isinstance(self.fallback_policy, PeerFallbackPolicy):
            _invalid("fallback_policy must be a PeerFallbackPolicy")
        _identifier(self.calculation_version, field_name="calculation_version")

    def rank(
        self,
        *,
        cutoff: datetime,
        entities: Sequence[CrossSectionalEntity],
        factor_specs: Sequence[FactorSpec],
        observations: Sequence[FactorObservation],
    ) -> CrossSectionalSnapshot:
        """Build one immutable rank snapshot from a complete declared universe."""

        canonical_entities = _entity_index(entities)
        canonical_specs = _validate_factor_specs(factor_specs)
        observation_index = _observation_index(
            observations,
            entities=canonical_entities,
            factor_specs=canonical_specs,
        )
        contributions, missing = _evaluate_factors(
            entities=canonical_entities,
            factor_specs=canonical_specs,
            observations=observation_index,
            minimum_peer_count=self.minimum_peer_count,
            winsorize_limit=self.winsorize_limit,
            fallback_policy=self.fallback_policy,
        )
        ranks = _rank_composites(
            entities=canonical_entities,
            factor_specs=canonical_specs,
            contributions=contributions,
            missing=missing,
        )
        return CrossSectionalSnapshot(
            cutoff=cutoff,
            calculation_version=self.calculation_version,
            minimum_peer_count=self.minimum_peer_count,
            winsorize_limit=self.winsorize_limit,
            fallback_policy=self.fallback_policy,
            factor_specs=canonical_specs,
            contributions=tuple(contributions),
            ranks=tuple(ranks),
            missing_decisions=tuple(missing),
        )


def _entity_index(
    entities: Sequence[CrossSectionalEntity],
) -> dict[str, CrossSectionalEntity]:
    result: dict[str, CrossSectionalEntity] = {}
    symbols: set[str] = set()
    for entity in sorted(entities, key=lambda item: (item.symbol, item.entity_id)):
        if entity.entity_id in result:
            _invalid(f"duplicate entity_id {entity.entity_id!r}")
        if entity.symbol in symbols:
            _invalid(f"duplicate canonical symbol {entity.symbol!r}")
        result[entity.entity_id] = entity
        symbols.add(entity.symbol)
    if not result:
        _invalid("at least one cross-sectional entity is required")
    return result


def _observation_index(
    observations: Sequence[FactorObservation],
    *,
    entities: Mapping[str, CrossSectionalEntity],
    factor_specs: Sequence[FactorSpec],
) -> dict[tuple[str, str], FactorObservation]:
    configured_factors = {spec.name: spec for spec in factor_specs}
    result: dict[tuple[str, str], FactorObservation] = {}
    for observation in observations:
        if observation.entity_id not in entities:
            _invalid(f"factor observation references unknown entity {observation.entity_id!r}")
        factor_spec = configured_factors.get(observation.factor_name)
        if factor_spec is None:
            _invalid(f"factor observation references unknown factor {observation.factor_name!r}")
        if not factor_spec.enabled:
            _invalid(f"disabled factor {observation.factor_name!r} must not supply observations")
        key = (observation.entity_id, observation.factor_name)
        if key in result:
            _invalid(f"duplicate factor observation for {key!r}")
        result[key] = observation
    return result


def _evaluate_factors(
    *,
    entities: Mapping[str, CrossSectionalEntity],
    factor_specs: Sequence[FactorSpec],
    observations: Mapping[tuple[str, str], FactorObservation],
    minimum_peer_count: int,
    winsorize_limit: float,
    fallback_policy: PeerFallbackPolicy,
) -> tuple[list[FactorContribution], list[MissingFactorDecision]]:
    contributions: list[FactorContribution] = []
    missing: list[MissingFactorDecision] = []
    for spec in factor_specs:
        if not spec.enabled:
            continue
        available = {
            entity_id: observation.raw_value
            for entity_id in entities
            if (observation := observations.get((entity_id, spec.name))) is not None
            and observation.raw_value is not None
        }
        for entity in entities.values():
            observation = observations.get((entity.entity_id, spec.name))
            missing_decision = _source_missing_decision(entity, spec, observation)
            if missing_decision is not None:
                missing.append(missing_decision)
                continue
            assert observation is not None
            assert observation.raw_value is not None
            peer_selection = _select_peer_values(
                entity,
                entities=entities,
                available=available,
                minimum_peer_count=minimum_peer_count,
                fallback_policy=fallback_policy,
            )
            if peer_selection is None:
                attempted = entity.peer_groups
                if fallback_policy is PeerFallbackPolicy.GLOBAL:
                    attempted = (*attempted, UNIVERSE_PEER_GROUP)
                missing.append(
                    MissingFactorDecision(
                        entity_id=entity.entity_id,
                        symbol=entity.symbol,
                        factor_name=spec.name,
                        reason=MissingFactorReason.INSUFFICIENT_PEERS,
                        detail=(
                            f"no peer fallback reached minimum count {minimum_peer_count}; "
                            f"attempted={attempted!r}"
                        ),
                        source_observation_ids=observation.source_observation_ids,
                    )
                )
                continue
            peer_group, peer_values = peer_selection
            contributions.append(
                _build_contribution(
                    entity=entity,
                    spec=spec,
                    observation=observation,
                    peer_group=peer_group,
                    peer_values=peer_values,
                    winsorize_limit=winsorize_limit,
                )
            )
    return contributions, missing


def _source_missing_decision(
    entity: CrossSectionalEntity,
    spec: FactorSpec,
    observation: FactorObservation | None,
) -> MissingFactorDecision | None:
    if observation is None:
        return MissingFactorDecision(
            entity_id=entity.entity_id,
            symbol=entity.symbol,
            factor_name=spec.name,
            reason=MissingFactorReason.NOT_SUPPLIED,
            detail="enabled factor observation was not supplied",
        )
    if observation.raw_value is None:
        assert observation.missing_reason is not None
        return MissingFactorDecision(
            entity_id=entity.entity_id,
            symbol=entity.symbol,
            factor_name=spec.name,
            reason=MissingFactorReason.SOURCE_MISSING,
            detail=observation.missing_reason,
            source_observation_ids=observation.source_observation_ids,
        )
    return None


def _select_peer_values(
    entity: CrossSectionalEntity,
    *,
    entities: Mapping[str, CrossSectionalEntity],
    available: Mapping[str, float | None],
    minimum_peer_count: int,
    fallback_policy: PeerFallbackPolicy,
) -> tuple[str, dict[str, float]] | None:
    peer_groups = entity.peer_groups
    if fallback_policy is PeerFallbackPolicy.GLOBAL:
        peer_groups = (*peer_groups, UNIVERSE_PEER_GROUP)
    for peer_group in peer_groups:
        if peer_group == UNIVERSE_PEER_GROUP:
            peer_values = {
                entity_id: value for entity_id, value in available.items() if value is not None
            }
        else:
            peer_values = {
                entity_id: value
                for entity_id, value in available.items()
                if peer_group in entities[entity_id].peer_groups and value is not None
            }
        if len(peer_values) >= minimum_peer_count:
            return peer_group, peer_values
    return None


def _robust_location_scale(values: Sequence[float]) -> tuple[float, float, PeerScaleMethod]:
    center = float(median(values))
    absolute_deviations = [abs(value - center) for value in values]
    median_absolute_deviation = float(median(absolute_deviations))
    robust_scale = median_absolute_deviation * _MAD_TO_NORMAL_SCALE
    if robust_scale > 0.0:
        return center, robust_scale, PeerScaleMethod.MEDIAN_ABSOLUTE_DEVIATION
    standard_deviation = float(pstdev(values))
    if standard_deviation > 0.0:
        return center, standard_deviation, PeerScaleMethod.STANDARD_DEVIATION_FALLBACK
    return center, 0.0, PeerScaleMethod.CONSTANT


def _build_contribution(
    *,
    entity: CrossSectionalEntity,
    spec: FactorSpec,
    observation: FactorObservation,
    peer_group: str,
    peer_values: Mapping[str, float],
    winsorize_limit: float,
) -> FactorContribution:
    raw_value = observation.raw_value
    assert raw_value is not None
    center, scale, scale_method = _robust_location_scale(tuple(peer_values.values()))
    direction = 1.0 if spec.direction is FactorDirection.HIGHER_IS_BETTER else -1.0
    unbounded = 0.0 if scale == 0.0 else direction * (raw_value - center) / scale
    unbounded = _finite(unbounded, field_name=f"{entity.entity_id}.{spec.name}.normalized_value")
    normalized = winsorize(unbounded, winsorize_limit)
    oriented_peer_values = {
        entity_id: direction * peer_value for entity_id, peer_value in peer_values.items()
    }
    factor_rank = average_ranks(oriented_peer_values)[entity.entity_id]
    contribution = spec.weight * normalized
    return FactorContribution(
        entity_id=entity.entity_id,
        symbol=entity.symbol,
        factor_name=spec.name,
        raw_value=raw_value,
        peer_group=peer_group,
        peer_count=len(peer_values),
        peer_center=center,
        peer_scale=scale,
        peer_scale_method=scale_method,
        unbounded_normalized_value=unbounded,
        normalized_value=normalized,
        factor_rank=factor_rank,
        weight=spec.weight,
        contribution=contribution,
        source_observation_ids=observation.source_observation_ids,
    )


def _rank_composites(
    *,
    entities: Mapping[str, CrossSectionalEntity],
    factor_specs: Sequence[FactorSpec],
    contributions: Sequence[FactorContribution],
    missing: Sequence[MissingFactorDecision],
) -> list[InstrumentRank]:
    enabled_names = {spec.name for spec in factor_specs if spec.enabled}
    missing_entities = {decision.entity_id for decision in missing}
    contributions_by_entity: dict[str, list[FactorContribution]] = {}
    for contribution in contributions:
        contributions_by_entity.setdefault(contribution.entity_id, []).append(contribution)
    composite_scores: dict[str, float] = {}
    for entity_id in entities:
        if entity_id in missing_entities:
            continue
        entity_contributions = contributions_by_entity.get(entity_id, [])
        if {item.factor_name for item in entity_contributions} != enabled_names:
            _invalid("complete entity is missing an enabled factor contribution")
        composite_scores[entity_id] = math.fsum(
            item.contribution
            for item in sorted(entity_contributions, key=lambda value: value.factor_name)
        )
    ranks = average_ranks(composite_scores)
    return [
        InstrumentRank(
            entity_id=entity_id,
            symbol=entities[entity_id].symbol,
            composite_score=composite_scores[entity_id],
            rank=ranks[entity_id],
        )
        for entity_id in sorted(
            composite_scores,
            key=lambda value: (ranks[value], entities[value].symbol, value),
        )
    ]


__all__ = [
    "UNIVERSE_PEER_GROUP",
    "CrossSectionalEntity",
    "CrossSectionalRanker",
    "CrossSectionalSnapshot",
    "FactorContribution",
    "FactorDirection",
    "FactorObservation",
    "FactorSpec",
    "InstrumentRank",
    "MissingFactorDecision",
    "MissingFactorReason",
    "PeerFallbackPolicy",
    "PeerScaleMethod",
    "average_ranks",
    "winsorize",
]
