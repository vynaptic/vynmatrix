"""Immutable panel input for the US Quality Compounder strategy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from lib_strategy.cross_sectional import CrossSectionalEntity, FactorObservation
from lib_strategy.equity_quality_compounder import (
    MarketCapBucket,
    QualityCompounderSecurity,
)
from lib_strategy.panels import (
    PanelReadyInput,
    panel_ready_input_from_payload,
    panel_ready_input_to_payload,
)

_SCHEMA = "us-quality-compounder-panel-v2"
_FACTOR_SCHEMA = "us-quality-compounder-factor-panel-v2"


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)


@dataclass(frozen=True, slots=True)
class USQualityCompounderPanelInput:
    """One complete, cutoff-bound quarterly strategy input."""

    panel: PanelReadyInput
    entries_allowed: bool
    entities: tuple[CrossSectionalEntity, ...]
    factor_observations: tuple[FactorObservation, ...]
    securities: tuple[QualityCompounderSecurity, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entries_allowed, bool):
            _invalid("entries_allowed must be boolean")
        canonical_entities = tuple(
            sorted(self.entities, key=lambda item: (item.symbol, item.entity_id))
        )
        canonical_observations = tuple(
            sorted(
                self.factor_observations,
                key=lambda item: (item.entity_id, item.factor_name),
            )
        )
        canonical_securities = tuple(
            sorted(self.securities, key=lambda item: (item.symbol, item.entity_id))
        )
        object.__setattr__(self, "entities", canonical_entities)
        object.__setattr__(self, "factor_observations", canonical_observations)
        object.__setattr__(self, "securities", canonical_securities)
        _validate_input(self)


def panel_input_to_payload(candidate: USQualityCompounderPanelInput) -> dict[str, object]:
    """Serialize one input in canonical order for durable replay."""

    return {
        "schema": _SCHEMA,
        "panel": panel_ready_input_to_payload(candidate.panel),
        **_factor_payload(
            entries_allowed=candidate.entries_allowed,
            entities=candidate.entities,
            factor_observations=candidate.factor_observations,
            securities=candidate.securities,
        ),
    }


def panel_input_from_payload(payload: Mapping[str, Any]) -> USQualityCompounderPanelInput:
    """Restore one input and re-run every strategy-specific invariant."""

    values = _mapping(payload, field_name="strategy panel")
    expected = {
        "schema",
        "factor_schema",
        "panel",
        "entries_allowed",
        "entities",
        "factor_observations",
        "securities",
    }
    if set(values) != expected or values.get("schema") != _SCHEMA:
        _invalid("strategy panel has an incompatible schema or field set")
    if values.get("factor_schema") != _FACTOR_SCHEMA:
        _invalid("strategy factor panel schema is incompatible")

    entity_payloads = _object_array(values.get("entities"), field_name="entities")
    observation_payloads = _object_array(
        values.get("factor_observations"),
        field_name="factor_observations",
    )
    security_payloads = _object_array(values.get("securities"), field_name="securities")
    return USQualityCompounderPanelInput(
        panel=panel_ready_input_from_payload(
            _mapping(values.get("panel"), field_name="generic panel")
        ),
        entries_allowed=_boolean(values.get("entries_allowed"), field_name="entries_allowed"),
        entities=tuple(_entity_from_payload(item) for item in entity_payloads),
        factor_observations=tuple(
            _factor_observation_from_payload(item) for item in observation_payloads
        ),
        securities=tuple(_security_from_payload(item) for item in security_payloads),
    )


def _validate_input(candidate: USQualityCompounderPanelInput) -> None:
    entity_by_id = {item.entity_id: item for item in candidate.entities}
    security_by_id = {item.entity_id: item for item in candidate.securities}
    if len(entity_by_id) != len(candidate.entities) or len(security_by_id) != len(
        candidate.securities
    ):
        _invalid("strategy panel entity identities must be unique")
    if len({item.factor_snapshot_id for item in candidate.securities}) != len(candidate.securities):
        _invalid("strategy panel factor snapshot identities must be unique")
    observed_by_id = {item.security_id: item for item in candidate.panel.observations}
    if set(entity_by_id) != set(security_by_id) or set(entity_by_id) != set(observed_by_id):
        _invalid("strategy evidence must exactly cover observed panel members")
    member_by_id = {item.security_id: item for item in candidate.panel.members}
    for entity_id, security in security_by_id.items():
        entity = entity_by_id[entity_id]
        member = member_by_id.get(entity_id)
        if member is None:
            _invalid("strategy evidence references a non-member security")
        if (
            entity.symbol != security.symbol
            or member.canonical_symbol != security.symbol
            or member.instrument_id != security.instrument_id
        ):
            _invalid("panel, factor, and instrument identities disagree")
        if entity.peer_groups != (security.industry, security.sector):
            _invalid("peer groups must be ordered industry then sector")
        entity_observations = tuple(
            item for item in candidate.factor_observations if item.entity_id == entity_id
        )
        if not entity_observations or any(
            not item.source_observation_ids for item in entity_observations
        ):
            _invalid("factor observations require immutable source identities")
        # ``factor_snapshot_id`` and the generic factor-panel digest identify
        # persisted application evidence. Their reconciliation to DB rows and
        # the strategy-specific fields above belongs to the mandatory panel
        # payload validator at registration, not to this provider-neutral codec.
    if any(item.entity_id not in entity_by_id for item in candidate.factor_observations):
        _invalid("factor observation references an unknown entity")


def _factor_payload(
    *,
    entries_allowed: bool,
    entities: Sequence[CrossSectionalEntity],
    factor_observations: Sequence[FactorObservation],
    securities: Sequence[QualityCompounderSecurity],
) -> dict[str, object]:
    return {
        "factor_schema": _FACTOR_SCHEMA,
        "entries_allowed": entries_allowed,
        "entities": [
            {
                "entity_id": item.entity_id,
                "symbol": item.symbol,
                "peer_groups": list(item.peer_groups),
            }
            for item in sorted(entities, key=lambda value: (value.symbol, value.entity_id))
        ],
        "factor_observations": [
            _factor_observation_payload(item)
            for item in sorted(
                factor_observations,
                key=lambda value: (value.entity_id, value.factor_name),
            )
        ],
        "securities": [
            _security_payload(item)
            for item in sorted(securities, key=lambda value: (value.symbol, value.entity_id))
        ],
    }


def _factor_observation_payload(item: FactorObservation) -> dict[str, object]:
    return {
        "entity_id": item.entity_id,
        "factor_name": item.factor_name,
        "raw_value": item.raw_value,
        "missing_reason": item.missing_reason,
        "source_observation_ids": list(item.source_observation_ids),
    }


def _security_payload(
    item: QualityCompounderSecurity,
) -> dict[str, object]:
    return {
        "entity_id": item.entity_id,
        "instrument_id": item.instrument_id,
        "symbol": item.symbol,
        "factor_snapshot_id": item.factor_snapshot_id,
        "sector": item.sector,
        "industry": item.industry,
        "market_cap_bucket": (
            item.market_cap_bucket.value if item.market_cap_bucket is not None else None
        ),
        "sector_score": item.sector_score,
        "industry_score": item.industry_score,
        "reference_price": item.reference_price,
        "expected_round_trip_cost_bps": item.expected_round_trip_cost_bps,
        "market_eligible": item.market_eligible,
        "market_ineligibility_reason": item.market_ineligibility_reason,
    }


def _entity_from_payload(payload: Mapping[str, Any]) -> CrossSectionalEntity:
    expected = {"entity_id", "symbol", "peer_groups"}
    if set(payload) != expected:
        _invalid("entity payload has an incompatible field set")
    peer_groups = payload.get("peer_groups")
    if not isinstance(peer_groups, list) or not all(isinstance(item, str) for item in peer_groups):
        _invalid("entity peer_groups must be an array of strings")
    return CrossSectionalEntity(
        entity_id=_string(payload.get("entity_id"), field_name="entity_id"),
        symbol=_string(payload.get("symbol"), field_name="symbol"),
        peer_groups=tuple(peer_groups),
    )


def _factor_observation_from_payload(payload: Mapping[str, Any]) -> FactorObservation:
    expected = {
        "entity_id",
        "factor_name",
        "raw_value",
        "missing_reason",
        "source_observation_ids",
    }
    if set(payload) != expected:
        _invalid("factor observation payload has an incompatible field set")
    source_ids = payload.get("source_observation_ids")
    if not isinstance(source_ids, list) or not all(isinstance(item, str) for item in source_ids):
        _invalid("source_observation_ids must be an array of strings")
    raw_value = payload.get("raw_value")
    missing_reason = payload.get("missing_reason")
    if isinstance(raw_value, bool) or (
        raw_value is not None and not isinstance(raw_value, (int, float))
    ):
        _invalid("factor observation raw_value must be a number or null")
    if missing_reason is not None and not isinstance(missing_reason, str):
        _invalid("factor observation missing_reason must be a string or null")
    return FactorObservation(
        entity_id=_string(payload.get("entity_id"), field_name="entity_id"),
        factor_name=_string(payload.get("factor_name"), field_name="factor_name"),
        raw_value=(float(raw_value) if raw_value is not None else None),
        missing_reason=missing_reason,
        source_observation_ids=tuple(source_ids),
    )


def _security_from_payload(payload: Mapping[str, Any]) -> QualityCompounderSecurity:
    expected = {
        "entity_id",
        "instrument_id",
        "symbol",
        "factor_snapshot_id",
        "sector",
        "industry",
        "market_cap_bucket",
        "sector_score",
        "industry_score",
        "reference_price",
        "expected_round_trip_cost_bps",
        "market_eligible",
        "market_ineligibility_reason",
    }
    if set(payload) != expected:
        _invalid("security payload has an incompatible field set")
    raw_bucket = payload.get("market_cap_bucket")
    try:
        bucket = (
            MarketCapBucket(_string(raw_bucket, field_name="market_cap_bucket"))
            if raw_bucket is not None
            else None
        )
    except ValueError as exc:
        _invalid(f"invalid market_cap_bucket: {exc}")
    return QualityCompounderSecurity(
        entity_id=_string(payload.get("entity_id"), field_name="entity_id"),
        instrument_id=_positive_int(payload.get("instrument_id"), field_name="instrument_id"),
        symbol=_string(payload.get("symbol"), field_name="symbol"),
        factor_snapshot_id=_string(
            payload.get("factor_snapshot_id"), field_name="factor_snapshot_id"
        ),
        sector=_string(payload.get("sector"), field_name="sector"),
        industry=_string(payload.get("industry"), field_name="industry"),
        market_cap_bucket=bucket,
        sector_score=_optional_number(payload.get("sector_score"), field_name="sector_score"),
        industry_score=_optional_number(payload.get("industry_score"), field_name="industry_score"),
        reference_price=_number(payload.get("reference_price"), field_name="reference_price"),
        expected_round_trip_cost_bps=_optional_number(
            payload.get("expected_round_trip_cost_bps"),
            field_name="expected_round_trip_cost_bps",
        ),
        market_eligible=_boolean(payload.get("market_eligible"), field_name="market_eligible"),
        market_ineligibility_reason=_optional_string(
            payload.get("market_ineligibility_reason"),
            field_name="market_ineligibility_reason",
        ),
    )


def _mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        _invalid(f"{field_name} must be an object with string keys")
    return value


def _object_array(value: object, *, field_name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        _invalid(f"{field_name} must be an array")
    return tuple(_mapping(item, field_name=field_name) for item in value)


def _string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _invalid(f"{field_name} must be a non-blank canonical string")
    return value


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _string(value, field_name=field_name)


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _invalid(f"{field_name} must be a positive integer")
    return value


def _number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(f"{field_name} must be a number")
    return float(value)


def _optional_number(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    return _number(value, field_name=field_name)


def _boolean(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        _invalid(f"{field_name} must be boolean")
    return value


__all__ = [
    "USQualityCompounderPanelInput",
    "panel_input_from_payload",
    "panel_input_to_payload",
]
