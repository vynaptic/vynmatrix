"""Cutoff-safe loader for separate point-in-time portfolio risk evidence.

The application service reads the existing append-only equity-observation
store.  It does not calculate alpha, infer exposures from strategy factors, or
contain a provider client.  Ingestion adapters must normalize an entitled,
version-pinned external risk model into the registered scalar contract.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnElement

from lib_application.db.models import (
    EquityObservation,
    EquityObservationValue,
    EquitySourceLineage,
)
from lib_application.services.equity_lineage import (
    EquityObservationAuthorityError,
    equity_observation_with_values_sha256,
    validate_equity_observation_authority,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.data_authority import DataUseScope, ProviderAuthorityPolicy
from lib_strategy.equity_factor_risk import (
    CANONICAL_STYLE_RISK_FACTORS,
    FACTOR_RISK_EXPOSURE_CONTRACT,
    FACTOR_RISK_OBSERVATION_KIND,
    FACTOR_RISK_TIMESTAMP_SEMANTICS_SCHEMA,
    EquityFactorRiskExposure,
    EquityFactorRiskPanel,
    PortfolioFactorRiskPolicy,
    StyleRiskExposure,
)
from lib_strategy.equity_factor_risk_model import (
    INTERNAL_FACTOR_RISK_ADJUSTMENT_POLICY,
    INTERNAL_FACTOR_RISK_DATASET_VERSION,
    INTERNAL_FACTOR_RISK_ENDPOINT,
    INTERNAL_FACTOR_RISK_ENTITLEMENT_SCOPE,
    INTERNAL_FACTOR_RISK_MISSING_DATA_POLICY,
    INTERNAL_FACTOR_RISK_MODEL,
    INTERNAL_FACTOR_RISK_PRODUCT,
    INTERNAL_FACTOR_RISK_PROVIDER,
    INTERNAL_FACTOR_RISK_TOOL_VERSION,
    CalculatedFactorRiskExposure,
    CalculatedFactorRiskPanel,
    factor_risk_input_manifest_payload,
)
from lib_strategy.panels import EffectivePanelMember

FACTOR_RISK_ADJUSTMENT_POLICY = INTERNAL_FACTOR_RISK_ADJUSTMENT_POLICY
FACTOR_RISK_TIMESTAMP_SEMANTICS = {
    "schema": FACTOR_RISK_TIMESTAMP_SEMANTICS_SCHEMA,
    "event_at": "verified official XNYS decision close",
    "available_at": "maximum availability of every cited atomic input",
    "retrieved_at": "actual derived-exposure materialization time",
    "corrections": "append_only_supersession",
    "semantics": "descriptive style-exposure control; not covariance or return forecast",
}

_UNPINNED_IDENTITIES = frozenset({"current", "latest", "unknown", "unversioned"})
_BASE_FIELDS = frozenset(
    {
        "factor_risk_contract",
        "security_id",
        "symbol",
        "model_provider",
        "model_id",
        "model_version",
        "model_definition_sha256",
        "benchmark_security_id",
        "input_manifest_sha256",
        "market_input_sha256",
        "fundamental_input_sha256",
        "membership_sha256",
        "provider_authority_sha256",
        "source_observation_set_sha256",
        "source_observation_count",
        "calculation_sha256",
        "exposure_observed_at",
        "source_available_at",
        "market_beta",
    }
)
_STYLE_FIELDS = frozenset(
    f"style_exposure_{factor_name}" for factor_name in CANONICAL_STYLE_RISK_FACTORS
)
_RAW_FIELDS = frozenset(
    f"raw_descriptor_{factor_name}" for factor_name in CANONICAL_STYLE_RISK_FACTORS
)
_REQUIRED_FIELDS = _BASE_FIELDS | _STYLE_FIELDS | _RAW_FIELDS
_QUERY_BATCH = 400


@dataclass(frozen=True, slots=True)
class PersistedFactorRiskPanel:
    """Exact append/replay outcome for one calculated cross-section."""

    panel: EquityFactorRiskPanel
    created_count: int


class EquityFactorRiskEvidenceError(RuntimeError):
    """Persisted risk evidence is incomplete, ambiguous, or not authoritative."""


def materialize_calculated_equity_factor_risk_panel(
    *,
    calculated: CalculatedFactorRiskPanel,
    policy: PortfolioFactorRiskPolicy,
    provider_authority_policy: ProviderAuthorityPolicy,
    entitlement_owner_user_id: str,
    retrieved_at: datetime,
) -> EquityFactorRiskPanel:
    """Adapt one pure calculation to the same immutable evidence contract."""

    if policy.model != INTERNAL_FACTOR_RISK_MODEL or calculated.model != policy.model:
        _invalid("factor-risk calculation differs from the frozen strategy model")
    owner = _pinned(entitlement_owner_user_id, field_name="entitlement owner")
    if provider_authority_policy.effective_entitlement_owner_user_id != owner:
        _invalid("factor-risk authority does not bind the exact personal owner")
    try:
        provider_authority_policy.require_authorized(
            provider=INTERNAL_FACTOR_RISK_PROVIDER,
            entitlement_scope=INTERNAL_FACTOR_RISK_ENTITLEMENT_SCOPE,
            entitlement_owner_user_id=owner,
        )
    except ValueError as exc:
        message = "factor-risk derived scope is outside provider authority"
        raise EquityFactorRiskEvidenceError(message) from exc
    materialized_at = _aware(retrieved_at, field_name="factor-risk retrieved_at")
    if materialized_at < calculated.available_at:
        _invalid("factor-risk materialization cannot precede atomic input availability")
    lineage_payload = _calculated_lineage_payload(
        calculated,
        owner=owner,
        retrieved_at=materialized_at,
    )
    lineage_id = canonical_json_hash({"schema": "equity-source-lineage-v1", **lineage_payload})
    manifest = factor_risk_input_manifest_payload(calculated)
    benchmark = manifest.get("benchmark")
    if not isinstance(benchmark, Mapping):
        _invalid("factor-risk input manifest benchmark is unavailable")
    benchmark_security_id = _pinned(
        benchmark.get("security_id"),
        field_name="benchmark security_id",
    )
    exposures: list[EquityFactorRiskExposure] = []
    for exposure in calculated.exposures:
        values = _calculated_exposure_values(calculated, exposure, manifest=manifest)
        source_record_identity = (
            f"{calculated.model.model_id}:{exposure.security_id}:"
            f"{calculated.effective_session.isoformat()}"
        )
        content_sha256 = _normalized_record_sha256(
            instrument_id=exposure.instrument_id,
            source_record_identity=source_record_identity,
            event_at=calculated.observed_at,
            values=values,
        )
        observation_id = canonical_json_hash(
            {
                "available_at": calculated.available_at.isoformat(),
                "content_sha256": content_sha256,
                "lineage_id": lineage_id,
                "revision": 1,
                "schema": "equity-observation-identity-v1",
                "source_record_identity": source_record_identity,
            }
        )
        authority_sha256 = canonical_json_hash(
            {
                "schema": "calculated-factor-risk-output-authority-v1",
                "lineage": lineage_payload,
                "observation_id": observation_id,
                "observation_content_sha256": content_sha256,
                "values": [
                    _normalized_value_payload(
                        field_name,
                        value_type,
                        value,
                        unit=unit,
                        context_identity=context_identity,
                    )
                    for field_name, value_type, value, unit, context_identity in values
                ],
            }
        )
        exposures.append(
            EquityFactorRiskExposure(
                instrument_id=exposure.instrument_id,
                security_id=exposure.security_id,
                symbol=exposure.symbol,
                model=calculated.model,
                benchmark_security_id=benchmark_security_id,
                input_manifest_sha256=calculated.input_manifest_sha256,
                market_input_sha256=calculated.market_input_sha256,
                fundamental_input_sha256=calculated.fundamental_input_sha256,
                membership_sha256=calculated.membership_sha256,
                provider_authority_sha256=calculated.provider_authority_sha256,
                source_observation_set_sha256=exposure.source_observation_set_sha256,
                source_observation_count=len(exposure.source_references),
                calculation_sha256=calculated.calculation_sha256,
                observed_at=calculated.observed_at,
                available_at=calculated.available_at,
                retrieved_at=materialized_at,
                revision=1,
                observation_id=observation_id,
                observation_content_sha256=content_sha256,
                observation_authority_sha256=authority_sha256,
                lineage_id=lineage_id,
                source_content_sha256=calculated.input_manifest_sha256,
                source_product=INTERNAL_FACTOR_RISK_PRODUCT,
                dataset_version=INTERNAL_FACTOR_RISK_DATASET_VERSION,
                tool_version=INTERNAL_FACTOR_RISK_TOOL_VERSION,
                source_revision=calculated.calculation_sha256,
                timestamp_semantics_sha256=canonical_json_hash(FACTOR_RISK_TIMESTAMP_SEMANTICS),
                adjustment_policy=INTERNAL_FACTOR_RISK_ADJUSTMENT_POLICY,
                missing_data_policy=INTERNAL_FACTOR_RISK_MISSING_DATA_POLICY,
                entitlement_scope=INTERNAL_FACTOR_RISK_ENTITLEMENT_SCOPE,
                entitlement_owner_user_id=owner,
                market_beta=float(exposure.market_beta),
                raw_descriptors=tuple(
                    (name, float(value)) for name, value in exposure.raw_descriptors
                ),
                style_exposures=tuple(
                    StyleRiskExposure(
                        factor_name=name,
                        standardized_exposure=float(value),
                    )
                    for name, value in exposure.style_exposures
                ),
            )
        )
    return EquityFactorRiskPanel(
        cutoff=calculated.cutoff,
        model=calculated.model,
        exposures=tuple(exposures),
    )


def _calculated_lineage_payload(
    calculated: CalculatedFactorRiskPanel,
    *,
    owner: str,
    retrieved_at: datetime,
) -> dict[str, object]:
    return {
        "adjustment_policy": FACTOR_RISK_ADJUSTMENT_POLICY,
        "content_sha256": calculated.input_manifest_sha256,
        "dataset_version": INTERNAL_FACTOR_RISK_DATASET_VERSION,
        "endpoint": INTERNAL_FACTOR_RISK_ENDPOINT,
        "entitlement_owner_user_id": owner,
        "entitlement_scope": INTERNAL_FACTOR_RISK_ENTITLEMENT_SCOPE,
        "missing_data_policy": INTERNAL_FACTOR_RISK_MISSING_DATA_POLICY,
        "product": INTERNAL_FACTOR_RISK_PRODUCT,
        "provider": INTERNAL_FACTOR_RISK_PROVIDER,
        "retrieved_at": retrieved_at.isoformat(),
        "source_identity": (
            f"SP500/{calculated.effective_session.isoformat()}/"
            f"{calculated.model.model_definition_sha256}"
        ),
        "source_revision": calculated.calculation_sha256,
        "timestamp_semantics": FACTOR_RISK_TIMESTAMP_SEMANTICS,
        "tool_version": INTERNAL_FACTOR_RISK_TOOL_VERSION,
    }


def persist_calculated_equity_factor_risk_panel(
    session: Session,
    *,
    calculated: CalculatedFactorRiskPanel,
    members: Sequence[EffectivePanelMember],
    policy: PortfolioFactorRiskPolicy,
    provider_authority_policy: ProviderAuthorityPolicy,
    entitlement_owner_user_id: str,
    retrieved_at: datetime,
) -> PersistedFactorRiskPanel:
    """Append or replay one owner-scoped internally derived exposure panel."""

    if not isinstance(calculated, CalculatedFactorRiskPanel):
        _invalid("factor-risk persistence requires a calculated panel")
    if policy.model != INTERNAL_FACTOR_RISK_MODEL or calculated.model != policy.model:
        _invalid("factor-risk calculation differs from the frozen strategy model")
    if provider_authority_policy.data_use_scope is DataUseScope.LIVE_FORWARD:
        _invalid("personal-research factor-risk evidence cannot authorize live use")
    owner = _pinned(entitlement_owner_user_id, field_name="entitlement owner")
    if provider_authority_policy.effective_entitlement_owner_user_id != owner:
        _invalid("factor-risk persistence authority does not bind the exact owner")
    try:
        provider_authority_policy.require_authorized(
            provider=INTERNAL_FACTOR_RISK_PROVIDER,
            entitlement_scope=INTERNAL_FACTOR_RISK_ENTITLEMENT_SCOPE,
            entitlement_owner_user_id=owner,
        )
    except ValueError as exc:
        message = "factor-risk derived scope is outside provider authority"
        raise EquityFactorRiskEvidenceError(message) from exc
    canonical_members = tuple(sorted(members, key=lambda item: item.security_id))
    member_by_security = {item.security_id: item for item in canonical_members}
    if set(member_by_security) != {item.security_id for item in calculated.exposures}:
        _invalid("factor-risk persistence members differ from the calculated cross-section")
    for exposure in calculated.exposures:
        member = member_by_security[exposure.security_id]
        if (
            member.instrument_id != exposure.instrument_id
            or member.canonical_symbol != exposure.symbol
        ):
            _invalid("factor-risk calculated and persisted permanent identities differ")
    _validate_atomic_sources(
        session,
        calculated=calculated,
        provider_authority_policy=provider_authority_policy,
    )
    materialized_at = _aware(retrieved_at, field_name="factor-risk retrieved_at")
    if materialized_at < calculated.available_at:
        _invalid("factor-risk materialization cannot precede atomic input availability")
    if (
        provider_authority_policy.data_use_scope is DataUseScope.PAPER_FORWARD
        and materialized_at > calculated.cutoff
    ):
        _invalid("paper factor-risk materialization occurred after its knowledge cutoff")
    lineage_payload = _calculated_lineage_payload(
        calculated,
        owner=owner,
        retrieved_at=materialized_at,
    )
    source_identity = str(lineage_payload["source_identity"])
    lineage_id = canonical_json_hash({"schema": "equity-source-lineage-v1", **lineage_payload})
    lineage = session.get(EquitySourceLineage, lineage_id)
    if lineage is None:
        lineage = EquitySourceLineage(
            lineage_id=lineage_id,
            provider=INTERNAL_FACTOR_RISK_PROVIDER,
            product=INTERNAL_FACTOR_RISK_PRODUCT,
            endpoint=INTERNAL_FACTOR_RISK_ENDPOINT,
            dataset_version=INTERNAL_FACTOR_RISK_DATASET_VERSION,
            tool_version=INTERNAL_FACTOR_RISK_TOOL_VERSION,
            source_identity=source_identity,
            source_revision=calculated.calculation_sha256,
            retrieved_at=materialized_at,
            timestamp_semantics=dict(FACTOR_RISK_TIMESTAMP_SEMANTICS),
            adjustment_policy=FACTOR_RISK_ADJUSTMENT_POLICY,
            entitlement_scope=INTERNAL_FACTOR_RISK_ENTITLEMENT_SCOPE,
            entitlement_owner_user_id=owner,
            missing_data_policy=INTERNAL_FACTOR_RISK_MISSING_DATA_POLICY,
            content_sha256=calculated.input_manifest_sha256,
        )
        session.add(lineage)
        session.flush()
    created_count = 0
    for exposure in calculated.exposures:
        created_count += int(
            _persist_calculated_exposure(
                session,
                calculated=calculated,
                exposure=exposure,
                lineage=lineage,
            )
        )
    loaded = load_equity_factor_risk_panel(
        session,
        members=canonical_members,
        cutoff=calculated.cutoff,
        policy=policy,
        provider_authority_policy=provider_authority_policy,
    )
    if loaded is None or {item.security_id for item in loaded.exposures} != set(member_by_security):
        _invalid("persisted factor-risk panel did not round-trip completely")
    return PersistedFactorRiskPanel(panel=loaded, created_count=created_count)


def _validate_atomic_sources(
    session: Session,
    *,
    calculated: CalculatedFactorRiskPanel,
    provider_authority_policy: ProviderAuthorityPolicy,
) -> None:
    manifest = factor_risk_input_manifest_payload(calculated)
    if manifest.get("source_references") != [
        item.to_payload() for item in calculated.source_references
    ]:
        _invalid("factor-risk input manifest source set differs from the calculation")
    ids = tuple(item.observation_id for item in calculated.source_references)
    rows: list[tuple[EquityObservation, EquitySourceLineage]] = []
    for index in range(0, len(ids), _QUERY_BATCH):
        batch = session.execute(
            select(EquityObservation, EquitySourceLineage)
            .join(
                EquitySourceLineage,
                EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
            )
            .where(EquityObservation.observation_id.in_(ids[index : index + _QUERY_BATCH]))
        ).all()
        rows.extend((row[0], row[1]) for row in batch)
    if len(rows) != len(ids):
        _invalid("factor-risk atomic source graph is incomplete")
    values = _normalized_values_unrestricted(session, observation_ids=ids)
    reference_by_id = {item.observation_id: item for item in calculated.source_references}
    for observation, lineage in rows:
        observation_id = str(observation.observation_id)
        reference = reference_by_id[observation_id]
        if str(observation.observation_kind) == FACTOR_RISK_OBSERVATION_KIND:
            _invalid("factor-risk calculation cannot recursively consume risk exposure")
        if (
            observation.available_at is None
            or _utc(observation.available_at) != reference.available_at
        ):
            _invalid("factor-risk atomic source availability differs from its manifest")
        if reference.available_at > calculated.cutoff:
            _invalid("factor-risk atomic source is future-dated")
        try:
            provider_authority_policy.require_authorized(
                provider=str(lineage.provider),
                entitlement_scope=str(lineage.entitlement_scope),
                entitlement_owner_user_id=(
                    str(lineage.entitlement_owner_user_id)
                    if lineage.entitlement_owner_user_id is not None
                    else None
                ),
            )
        except ValueError as exc:
            message = "factor-risk atomic source is outside provider authority"
            raise EquityFactorRiskEvidenceError(message) from exc
        authority_sha256 = equity_observation_with_values_sha256(
            observation,
            lineage,
            values[observation_id],
        )
        if authority_sha256 != reference.authority_sha256:
            _invalid("factor-risk atomic source authority digest differs")


def _persist_calculated_exposure(
    session: Session,
    *,
    calculated: CalculatedFactorRiskPanel,
    exposure: CalculatedFactorRiskExposure,
    lineage: EquitySourceLineage,
) -> bool:
    manifest = factor_risk_input_manifest_payload(calculated)
    values = _calculated_exposure_values(calculated, exposure, manifest=manifest)
    source_record_identity = (
        f"{calculated.model.model_id}:{exposure.security_id}:"
        f"{calculated.effective_session.isoformat()}"
    )
    content_sha256 = _normalized_record_sha256(
        instrument_id=exposure.instrument_id,
        source_record_identity=source_record_identity,
        event_at=calculated.observed_at,
        values=values,
    )
    rows = list(
        session.scalars(
            select(EquityObservation)
            .join(
                EquitySourceLineage,
                EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
            )
            .where(
                EquityObservation.instr_id == exposure.instrument_id,
                EquityObservation.observation_kind == FACTOR_RISK_OBSERVATION_KIND,
                EquityObservation.source_record_identity == source_record_identity,
                EquitySourceLineage.provider == INTERNAL_FACTOR_RISK_PROVIDER,
                EquitySourceLineage.product == INTERNAL_FACTOR_RISK_PRODUCT,
                EquitySourceLineage.endpoint == INTERNAL_FACTOR_RISK_ENDPOINT,
                EquitySourceLineage.source_identity == lineage.source_identity,
                EquitySourceLineage.entitlement_owner_user_id == lineage.entitlement_owner_user_id,
            )
            .order_by(EquityObservation.revision.desc())
            .with_for_update()
        )
    )
    latest = rows[0] if rows else None
    if latest is not None and str(latest.content_sha256) == content_sha256:
        return False
    revision = int(latest.revision) + 1 if latest is not None else 1
    observation_id = canonical_json_hash(
        {
            "available_at": calculated.available_at.isoformat(),
            "content_sha256": content_sha256,
            "lineage_id": str(lineage.lineage_id),
            "revision": revision,
            "schema": "equity-observation-identity-v1",
            "source_record_identity": source_record_identity,
        }
    )
    observation = EquityObservation(
        observation_id=observation_id,
        lineage_id=str(lineage.lineage_id),
        instr_id=exposure.instrument_id,
        observation_kind=FACTOR_RISK_OBSERVATION_KIND,
        source_record_identity=source_record_identity,
        event_at=calculated.observed_at,
        available_at=calculated.available_at,
        revision=revision,
        supersedes_observation_id=(str(latest.observation_id) if latest is not None else None),
        disposition="observed",
        content_sha256=content_sha256,
    )
    session.add(observation)
    session.flush()
    for field_name, value_type, value, unit, context_identity in values:
        kwargs: dict[str, object] = {
            "decimal_value": None,
            "integer_value": None,
            "text_value": None,
            "boolean_value": None,
            "date_value": None,
            "timestamp_value": None,
        }
        kwargs[f"{value_type}_value"] = value
        value_id = canonical_json_hash(
            {
                "observation_id": observation_id,
                "ordinal": 0,
                "schema": "equity-observation-value-v1",
                **_normalized_value_payload(
                    field_name,
                    value_type,
                    value,
                    unit=unit,
                    context_identity=context_identity,
                ),
            }
        )
        session.add(
            EquityObservationValue(
                value_id=value_id,
                observation_id=observation_id,
                field_name=field_name,
                ordinal=0,
                value_type=value_type,
                unit=unit,
                context_identity=context_identity,
                **kwargs,
            )
        )
    session.flush()
    return True


_StoredRiskValue = tuple[str, str, object, str | None, str | None]


def _calculated_exposure_values(
    calculated: CalculatedFactorRiskPanel,
    exposure: CalculatedFactorRiskExposure,
    *,
    manifest: Mapping[str, object],
) -> tuple[_StoredRiskValue, ...]:
    benchmark = manifest.get("benchmark")
    if not isinstance(benchmark, Mapping):
        _invalid("factor-risk input manifest benchmark is unavailable")
    benchmark_security_id = _pinned(
        benchmark.get("security_id"),
        field_name="benchmark security_id",
    )
    context = calculated.model.model_definition_sha256
    values: list[_StoredRiskValue] = [
        ("factor_risk_contract", "text", FACTOR_RISK_EXPOSURE_CONTRACT, None, None),
        ("security_id", "text", exposure.security_id, None, None),
        ("symbol", "text", exposure.symbol, None, None),
        ("model_provider", "text", calculated.model.provider, None, None),
        ("model_id", "text", calculated.model.model_id, None, None),
        ("model_version", "text", calculated.model.model_version, None, None),
        (
            "model_definition_sha256",
            "text",
            calculated.model.model_definition_sha256,
            None,
            None,
        ),
        ("benchmark_security_id", "text", benchmark_security_id, None, None),
        (
            "input_manifest_sha256",
            "text",
            calculated.input_manifest_sha256,
            None,
            None,
        ),
        ("market_input_sha256", "text", calculated.market_input_sha256, None, None),
        (
            "fundamental_input_sha256",
            "text",
            calculated.fundamental_input_sha256,
            None,
            None,
        ),
        ("membership_sha256", "text", calculated.membership_sha256, None, None),
        (
            "provider_authority_sha256",
            "text",
            calculated.provider_authority_sha256,
            None,
            None,
        ),
        (
            "source_observation_set_sha256",
            "text",
            exposure.source_observation_set_sha256,
            None,
            None,
        ),
        (
            "source_observation_count",
            "integer",
            len(exposure.source_references),
            None,
            None,
        ),
        ("calculation_sha256", "text", calculated.calculation_sha256, None, None),
        ("exposure_observed_at", "timestamp", calculated.observed_at, None, None),
        ("source_available_at", "timestamp", calculated.available_at, None, None),
        ("market_beta", "decimal", exposure.market_beta, "beta", context),
    ]
    values.extend(
        (
            f"raw_descriptor_{factor_name}",
            "decimal",
            value,
            "raw_descriptor",
            context,
        )
        for factor_name, value in exposure.raw_descriptors
    )
    values.extend(
        (
            f"style_exposure_{factor_name}",
            "decimal",
            value,
            "standard_deviation",
            context,
        )
        for factor_name, value in exposure.style_exposures
    )
    return tuple(sorted(values, key=lambda item: item[0]))


def _normalized_record_sha256(
    *,
    instrument_id: int,
    source_record_identity: str,
    event_at: datetime,
    values: Sequence[_StoredRiskValue],
) -> str:
    return canonical_json_hash(
        {
            "accession_number": None,
            "event_at": _aware(event_at, field_name="factor-risk event_at").isoformat(),
            "filing_form": None,
            "instrument_id": instrument_id,
            "observation_kind": FACTOR_RISK_OBSERVATION_KIND,
            "schema": "normalized-equity-source-record-v1",
            "sic_code": None,
            "source_record_identity": source_record_identity,
            "values": [
                _normalized_value_payload(
                    field_name,
                    value_type,
                    value,
                    unit=unit,
                    context_identity=context_identity,
                )
                for field_name, value_type, value, unit, context_identity in values
            ],
        }
    )


def _normalized_value_payload(
    field_name: str,
    value_type: str,
    value: object,
    *,
    unit: str | None,
    context_identity: str | None,
) -> dict[str, object]:
    normalized = value
    if isinstance(value, Decimal):
        normalized = format(value.normalize(), "f")
    elif isinstance(value, datetime):
        normalized = _aware(value, field_name=field_name).isoformat()
    return {
        "context_identity": context_identity,
        "field_name": field_name,
        "fiscal_period": None,
        "fiscal_year": None,
        "period_end": None,
        "period_start": None,
        "unit": unit,
        "value": normalized,
        "value_type": value_type,
    }


def load_equity_factor_risk_panel(
    session: Session,
    *,
    members: Sequence[EffectivePanelMember],
    cutoff: datetime,
    policy: PortfolioFactorRiskPolicy,
    provider_authority_policy: ProviderAuthorityPolicy,
) -> EquityFactorRiskPanel | None:
    """Load the latest exact-model exposure for each covered panel member.

    Coverage may be partial at this storage boundary because the portfolio
    selector knows which names are economically selected.  A configured model
    with no evidence returns ``None``; selected missing coverage and all
    paper/live model gaps fail closed in the strategy domain.
    """

    if not isinstance(policy, PortfolioFactorRiskPolicy):
        _invalid("factor-risk policy must be typed")
    if not isinstance(provider_authority_policy, ProviderAuthorityPolicy):
        _invalid("factor-risk provider authority must be typed")
    decision_cutoff = _aware(cutoff, field_name="factor-risk cutoff")
    if policy.model is None:
        return None
    canonical_members = tuple(sorted(members, key=lambda item: item.security_id))
    if not canonical_members or not all(
        isinstance(item, EffectivePanelMember) for item in canonical_members
    ):
        _invalid("factor-risk lookup requires typed effective panel members")
    if len({item.security_id for item in canonical_members}) != len(canonical_members):
        _invalid("factor-risk lookup member security identities are not unique")
    if len({item.instrument_id for item in canonical_members}) != len(canonical_members):
        _invalid("factor-risk lookup member instrument identities are not unique")
    member_by_instrument = {item.instrument_id: item for item in canonical_members}

    candidates = _matching_candidates(
        session,
        instrument_ids=tuple(sorted(member_by_instrument)),
        cutoff=decision_cutoff,
        policy=policy,
    )
    values_by_observation = _normalized_values(
        session,
        observation_ids=tuple(str(item[0].observation_id) for item in candidates),
    )
    selected_rows = _latest_unambiguous_rows(candidates)
    exposure_rows: list[EquityFactorRiskExposure] = []
    for observation, lineage in selected_rows:
        instrument_id = observation.instr_id
        if instrument_id is None:
            _invalid("factor-risk observation lacks an instrument identity")
        exposure_rows.append(
            _build_exposure(
                session,
                observation=observation,
                lineage=lineage,
                values=values_by_observation[str(observation.observation_id)],
                member=member_by_instrument[instrument_id],
                cutoff=decision_cutoff,
                policy=policy,
                provider_authority_policy=provider_authority_policy,
            )
        )
    exposures = tuple(sorted(exposure_rows, key=lambda item: item.security_id))
    if not exposures:
        return None
    return EquityFactorRiskPanel(
        cutoff=decision_cutoff,
        model=policy.model,
        exposures=exposures,
    )


def _matching_candidates(
    session: Session,
    *,
    instrument_ids: tuple[int, ...],
    cutoff: datetime,
    policy: PortfolioFactorRiskPolicy,
) -> tuple[tuple[EquityObservation, EquitySourceLineage], ...]:
    assert policy.model is not None
    contract_value = aliased(EquityObservationValue)
    provider_value = aliased(EquityObservationValue)
    model_id_value = aliased(EquityObservationValue)
    version_value = aliased(EquityObservationValue)
    definition_value = aliased(EquityObservationValue)

    def text_join(alias: Any, field_name: str) -> ColumnElement[bool]:
        return and_(
            alias.observation_id == EquityObservation.observation_id,
            alias.field_name == field_name,
            alias.ordinal == 0,
            alias.value_type == "text",
        )

    oldest = cutoff - timedelta(days=policy.maximum_age_days)
    rows: list[tuple[EquityObservation, EquitySourceLineage]] = []
    for index in range(0, len(instrument_ids), _QUERY_BATCH):
        instrument_batch = instrument_ids[index : index + _QUERY_BATCH]
        batch = session.execute(
            select(EquityObservation, EquitySourceLineage)
            .join(
                EquitySourceLineage,
                EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
            )
            .join(contract_value, text_join(contract_value, "factor_risk_contract"))
            .join(provider_value, text_join(provider_value, "model_provider"))
            .join(model_id_value, text_join(model_id_value, "model_id"))
            .join(version_value, text_join(version_value, "model_version"))
            .join(
                definition_value,
                text_join(definition_value, "model_definition_sha256"),
            )
            .where(
                EquityObservation.instr_id.in_(instrument_batch),
                EquityObservation.observation_kind == FACTOR_RISK_OBSERVATION_KIND,
                EquityObservation.disposition == "observed",
                EquityObservation.available_at.is_not(None),
                EquityObservation.event_at >= _stored(session, oldest),
                EquityObservation.event_at <= _stored(session, cutoff),
                EquityObservation.available_at <= _stored(session, cutoff),
                contract_value.text_value == FACTOR_RISK_EXPOSURE_CONTRACT,
                provider_value.text_value == policy.model.provider,
                model_id_value.text_value == policy.model.model_id,
                version_value.text_value == policy.model.model_version,
                definition_value.text_value == policy.model.model_definition_sha256,
            )
        ).all()
        rows.extend((row[0], row[1]) for row in batch)
    return tuple((observation, lineage) for observation, lineage in rows)


def _normalized_values(
    session: Session,
    *,
    observation_ids: tuple[str, ...],
) -> dict[str, dict[str, EquityObservationValue]]:
    if not observation_ids:
        return {}
    rows: list[EquityObservationValue] = []
    for index in range(0, len(observation_ids), _QUERY_BATCH):
        observation_batch = observation_ids[index : index + _QUERY_BATCH]
        rows.extend(
            session.scalars(
                select(EquityObservationValue)
                .where(EquityObservationValue.observation_id.in_(observation_batch))
                .order_by(
                    EquityObservationValue.observation_id,
                    EquityObservationValue.field_name,
                    EquityObservationValue.ordinal,
                )
            )
        )
    result: dict[str, dict[str, EquityObservationValue]] = defaultdict(dict)
    for row in rows:
        observation_id = str(row.observation_id)
        field_name = str(row.field_name)
        if field_name in result[observation_id]:
            _invalid("factor-risk normalized fields cannot repeat")
        result[observation_id][field_name] = row
    for observation_id in observation_ids:
        values = result.get(observation_id, {})
        if set(values) != _REQUIRED_FIELDS:
            _invalid("factor-risk observation has an incompatible normalized field set")
        if any(int(value.ordinal) != 0 for value in values.values()):
            _invalid("factor-risk normalized values must use ordinal zero")
    return dict(result)


def _normalized_values_unrestricted(
    session: Session,
    *,
    observation_ids: tuple[str, ...],
) -> dict[str, dict[str, EquityObservationValue]]:
    """Load complete scalar values without imposing a risk-output field set."""

    if not observation_ids:
        return {}
    rows: list[EquityObservationValue] = []
    for index in range(0, len(observation_ids), _QUERY_BATCH):
        rows.extend(
            session.scalars(
                select(EquityObservationValue)
                .where(
                    EquityObservationValue.observation_id.in_(
                        observation_ids[index : index + _QUERY_BATCH]
                    )
                )
                .order_by(
                    EquityObservationValue.observation_id,
                    EquityObservationValue.field_name,
                    EquityObservationValue.ordinal,
                )
            )
        )
    result: dict[str, dict[str, EquityObservationValue]] = defaultdict(dict)
    for row in rows:
        observation_id = str(row.observation_id)
        field_name = str(row.field_name)
        if field_name in result[observation_id] or int(row.ordinal) != 0:
            _invalid("factor-risk atomic sources require unique scalar normalized fields")
        result[observation_id][field_name] = row
    missing = set(observation_ids) - set(result)
    if missing:
        _invalid("factor-risk atomic source normalized values are incomplete")
    return dict(result)


def _latest_unambiguous_rows(
    rows: Sequence[tuple[EquityObservation, EquitySourceLineage]],
) -> tuple[tuple[EquityObservation, EquitySourceLineage], ...]:
    grouped: dict[int, list[tuple[EquityObservation, EquitySourceLineage]]] = defaultdict(list)
    for observation, lineage in rows:
        if observation.instr_id is None:
            _invalid("factor-risk observation lacks an instrument identity")
        grouped[int(observation.instr_id)].append((observation, lineage))
    selected: list[tuple[EquityObservation, EquitySourceLineage]] = []
    for instrument_id in sorted(grouped):
        candidates = grouped[instrument_id]
        newest_event = max(_utc(item[0].event_at) for item in candidates)
        event_frontier = [item for item in candidates if _utc(item[0].event_at) == newest_event]
        newest_availability = max(_utc(item[0].available_at) for item in event_frontier)
        frontier = [
            item for item in event_frontier if _utc(item[0].available_at) == newest_availability
        ]
        if len({str(item[0].source_record_identity) for item in frontier}) != 1:
            _invalid("factor-risk latest exposure is ambiguous across source records")
        newest_revision = max(int(item[0].revision) for item in frontier)
        revision_frontier = [item for item in frontier if int(item[0].revision) == newest_revision]
        if len(revision_frontier) != 1:
            _invalid("factor-risk latest exposure revision is ambiguous")
        selected.append(revision_frontier[0])
    return tuple(selected)


def _build_exposure(  # noqa: PLR0912
    session: Session,
    *,
    observation: EquityObservation,
    lineage: EquitySourceLineage,
    values: dict[str, EquityObservationValue],
    member: EffectivePanelMember,
    cutoff: datetime,
    policy: PortfolioFactorRiskPolicy,
    provider_authority_policy: ProviderAuthorityPolicy,
) -> EquityFactorRiskExposure:
    assert policy.model is not None
    try:
        authoritative_observation, authoritative_lineage = validate_equity_observation_authority(
            session,
            observation_id=str(observation.observation_id),
            expected_kind=FACTOR_RISK_OBSERVATION_KIND,
            cutoff=cutoff,
            provider_authority_policy=provider_authority_policy,
            expected_instrument_id=member.instrument_id,
        )
    except EquityObservationAuthorityError as exc:
        raise EquityFactorRiskEvidenceError(str(exc)) from exc
    if str(authoritative_observation.observation_id) != str(observation.observation_id):
        _invalid("factor-risk authority returned a different observation identity")
    if str(authoritative_lineage.lineage_id) != str(lineage.lineage_id):
        _invalid("factor-risk authority returned a different lineage identity")

    expected_text = {
        "factor_risk_contract": FACTOR_RISK_EXPOSURE_CONTRACT,
        "security_id": member.security_id,
        "symbol": member.canonical_symbol,
        "model_provider": policy.model.provider,
        "model_id": policy.model.model_id,
        "model_version": policy.model.model_version,
        "model_definition_sha256": policy.model.model_definition_sha256,
    }
    for field_name, expected in expected_text.items():
        if _text_value(values, field_name) != expected:
            _invalid(f"factor-risk value {field_name!r} differs from frozen identity")
    observed_at = _timestamp_value(values, "exposure_observed_at")
    available_at = _timestamp_value(values, "source_available_at")
    if observed_at != _utc(observation.event_at):
        _invalid("factor-risk normalized observation timestamp differs from event_at")
    if observation.available_at is None or available_at != _utc(observation.available_at):
        _invalid("factor-risk normalized availability differs from available_at")

    timestamp_semantics = lineage.timestamp_semantics
    if not isinstance(timestamp_semantics, Mapping) or dict(timestamp_semantics) != (
        FACTOR_RISK_TIMESTAMP_SEMANTICS
    ):
        _invalid("factor-risk lineage timestamp semantics are incompatible")
    if str(lineage.provider) != policy.model.provider:
        _invalid("factor-risk lineage provider differs from model provider")
    if policy.model == INTERNAL_FACTOR_RISK_MODEL:
        if str(lineage.product) != INTERNAL_FACTOR_RISK_PRODUCT:
            _invalid("factor-risk lineage product is incompatible")
        if str(lineage.endpoint) != INTERNAL_FACTOR_RISK_ENDPOINT:
            _invalid("factor-risk lineage endpoint is incompatible")
        if str(lineage.dataset_version) != INTERNAL_FACTOR_RISK_DATASET_VERSION:
            _invalid("factor-risk lineage dataset version is incompatible")
        if str(lineage.tool_version) != INTERNAL_FACTOR_RISK_TOOL_VERSION:
            _invalid("factor-risk lineage tool version is incompatible")
    if str(lineage.adjustment_policy) != FACTOR_RISK_ADJUSTMENT_POLICY:
        _invalid("factor-risk lineage adjustment policy is incompatible")
    if str(lineage.missing_data_policy) != INTERNAL_FACTOR_RISK_MISSING_DATA_POLICY:
        _invalid("factor-risk lineage must use fail-closed missing-data semantics")
    if (
        policy.model == INTERNAL_FACTOR_RISK_MODEL
        and str(lineage.entitlement_scope) != INTERNAL_FACTOR_RISK_ENTITLEMENT_SCOPE
    ):
        _invalid("factor-risk lineage entitlement scope is incompatible")

    input_manifest_sha256 = _text_value(values, "input_manifest_sha256")
    calculation_sha256 = _text_value(values, "calculation_sha256")
    if str(lineage.content_sha256) != input_manifest_sha256:
        _invalid("factor-risk lineage content digest differs from its input manifest")
    if str(lineage.source_revision) != calculation_sha256:
        _invalid("factor-risk lineage revision differs from the calculation digest")
    source_observation_count = _integer_value(values, "source_observation_count")
    if source_observation_count < 1:
        _invalid("factor-risk source observation count must be positive")

    beta = _decimal_value(
        values,
        "market_beta",
        unit="beta",
        context_identity=policy.model.model_definition_sha256,
    )
    styles = tuple(
        StyleRiskExposure(
            factor_name=factor_name,
            standardized_exposure=_decimal_value(
                values,
                f"style_exposure_{factor_name}",
                unit="standard_deviation",
                context_identity=policy.model.model_definition_sha256,
            ),
        )
        for factor_name in CANONICAL_STYLE_RISK_FACTORS
    )
    raw_descriptors = tuple(
        (
            factor_name,
            _decimal_value(
                values,
                f"raw_descriptor_{factor_name}",
                unit="raw_descriptor",
                context_identity=policy.model.model_definition_sha256,
            ),
        )
        for factor_name in CANONICAL_STYLE_RISK_FACTORS
    )
    return EquityFactorRiskExposure(
        instrument_id=member.instrument_id,
        security_id=member.security_id,
        symbol=member.canonical_symbol,
        model=policy.model,
        benchmark_security_id=_text_value(values, "benchmark_security_id"),
        input_manifest_sha256=input_manifest_sha256,
        market_input_sha256=_text_value(values, "market_input_sha256"),
        fundamental_input_sha256=_text_value(values, "fundamental_input_sha256"),
        membership_sha256=_text_value(values, "membership_sha256"),
        provider_authority_sha256=_text_value(values, "provider_authority_sha256"),
        source_observation_set_sha256=_text_value(
            values,
            "source_observation_set_sha256",
        ),
        source_observation_count=source_observation_count,
        calculation_sha256=calculation_sha256,
        observed_at=observed_at,
        available_at=available_at,
        retrieved_at=_utc(lineage.retrieved_at),
        revision=int(observation.revision),
        observation_id=str(observation.observation_id),
        observation_content_sha256=str(observation.content_sha256),
        observation_authority_sha256=equity_observation_with_values_sha256(
            observation,
            lineage,
            values,
        ),
        lineage_id=str(lineage.lineage_id),
        source_content_sha256=str(lineage.content_sha256),
        source_product=_pinned(lineage.product, field_name="source product"),
        dataset_version=_pinned(lineage.dataset_version, field_name="dataset version"),
        tool_version=_pinned(lineage.tool_version, field_name="tool version"),
        source_revision=_pinned(lineage.source_revision, field_name="source revision"),
        timestamp_semantics_sha256=canonical_json_hash(timestamp_semantics),
        adjustment_policy=str(lineage.adjustment_policy),
        missing_data_policy=str(lineage.missing_data_policy),
        entitlement_scope=str(lineage.entitlement_scope),
        entitlement_owner_user_id=(
            str(lineage.entitlement_owner_user_id)
            if lineage.entitlement_owner_user_id is not None
            else None
        ),
        market_beta=beta,
        raw_descriptors=raw_descriptors,
        style_exposures=styles,
    )


def _scalar_row(
    values: dict[str, EquityObservationValue],
    field_name: str,
    value_type: str,
) -> EquityObservationValue:
    row = values[field_name]
    if (
        str(row.value_type) != value_type
        or int(row.ordinal) != 0
        or row.fiscal_year is not None
        or row.fiscal_period is not None
        or row.period_start is not None
        or row.period_end is not None
    ):
        _invalid(f"factor-risk value {field_name!r} has incompatible scalar semantics")
    return row


def _text_value(values: dict[str, EquityObservationValue], field_name: str) -> str:
    row = _scalar_row(values, field_name, "text")
    if row.unit is not None or row.context_identity is not None:
        _invalid(f"factor-risk text value {field_name!r} cannot carry unit context")
    return _pinned(row.text_value, field_name=field_name)


def _timestamp_value(
    values: dict[str, EquityObservationValue],
    field_name: str,
) -> datetime:
    row = _scalar_row(values, field_name, "timestamp")
    if row.timestamp_value is None or row.unit is not None or row.context_identity is not None:
        _invalid(f"factor-risk timestamp value {field_name!r} is invalid")
    return _utc(row.timestamp_value)


def _integer_value(values: dict[str, EquityObservationValue], field_name: str) -> int:
    row = _scalar_row(values, field_name, "integer")
    if row.integer_value is None or row.unit is not None or row.context_identity is not None:
        _invalid(f"factor-risk integer value {field_name!r} is invalid")
    return int(row.integer_value)


def _decimal_value(
    values: dict[str, EquityObservationValue],
    field_name: str,
    *,
    unit: str,
    context_identity: str,
) -> float:
    row = _scalar_row(values, field_name, "decimal")
    value = row.decimal_value
    if (
        value is None
        or not isinstance(value, Decimal)
        or not value.is_finite()
        or str(row.unit) != unit
        or str(row.context_identity) != context_identity
    ):
        _invalid(f"factor-risk decimal value {field_name!r} is invalid")
    result = float(value)
    if not math.isfinite(result):
        _invalid(f"factor-risk decimal value {field_name!r} exceeds finite precision")
    return 0.0 if result == 0.0 else result


def _pinned(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"factor-risk {field_name} must be canonical text")
    if value.casefold() in _UNPINNED_IDENTITIES:
        _invalid(f"factor-risk {field_name} must be a pinned identity")
    return value


def _aware(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _utc(value: datetime | None) -> datetime:
    if value is None:
        _invalid("factor-risk persisted timestamp is missing")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _stored(session: Session, value: datetime) -> datetime:
    utc_value = _aware(value, field_name="factor-risk cutoff")
    return (
        utc_value.replace(tzinfo=None) if session.get_bind().dialect.name == "sqlite" else utc_value
    )


def _invalid(message: str) -> NoReturn:
    raise EquityFactorRiskEvidenceError(message)


__all__ = [
    "FACTOR_RISK_ADJUSTMENT_POLICY",
    "FACTOR_RISK_TIMESTAMP_SEMANTICS",
    "EquityFactorRiskEvidenceError",
    "PersistedFactorRiskPanel",
    "load_equity_factor_risk_panel",
    "materialize_calculated_equity_factor_risk_panel",
    "persist_calculated_equity_factor_risk_panel",
]
