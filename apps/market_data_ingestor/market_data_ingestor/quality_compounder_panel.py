"""US Quality Compounder synchronized panel production and validation."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session
from USQualityCompounder.panel import (
    USQualityCompounderPanelInput,
    panel_input_from_payload,
    panel_input_to_payload,
)

from lib_application.db.models import (
    EquityObservation,
    EquityObservationValue,
    EquitySourceLineage,
)
from lib_application.services.equity_lineage import (
    equity_observation_semantic_sha256,
    validate_equity_observation_authority,
)
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
    persist_equity_observation,
)
from lib_application.services.strategy_panel_inputs import (
    StrategyPanelInputPersistenceError,
    StrategyPanelPayloadValidationRequest,
    StrategyPanelPayloadValidationResult,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.cross_sectional import CrossSectionalEntity, FactorObservation
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.equity_market_factors import (
    EquityMarketFactorPolicy,
    EquityMarketFactorSnapshot,
)
from lib_strategy.equity_quality_compounder import (
    QUALITY_COMPOUNDER_PANEL_DERIVATION_VERSION,
    QualityCompounderEvidencePolicy,
    QualityCompounderGroupLevel,
    QualityCompounderGroupMember,
    QualityCompounderPolicy,
    QualityCompounderSecurity,
    calculate_quality_compounder_group_scores,
    quality_compounder_configuration_sha256,
    quality_compounder_entries_allowed,
    quality_compounder_market_cap_bucket,
    quality_compounder_market_ineligibility_reason,
)
from lib_strategy.panels import PanelReadyInput, panel_ready_input_to_payload

from .equity_factors import FundamentalPanelSnapshot, MarketCapitalizationEvidence

_FUNDAMENTAL_FACTORS = frozenset({"fundamental_growth", "quality", "valuation"})
_VALIDATOR_ID = "us-quality-compounder-panel-validator"
_VALIDATOR_VERSION = "2.0.0"
_MANIFEST_PRODUCT = "us-quality-compounder-derived-panel"
_MANIFEST_SCOPE = "vynmatrix-owner-derived-paper-only"
_MANIFEST_TOOL_VERSION = "vynmatrix-quality-compounder-panel-v2"
_SHA256_LENGTH = 64
_AUTHORITY_POLICY_VERSION = "us-quality-compounder-paper-v1"


class QualityCompounderPanelError(StrategyPanelInputPersistenceError):
    """The strategy panel cannot be derived from synchronized evidence."""


def _invalid(message: str) -> NoReturn:
    raise QualityCompounderPanelError(message)


def quality_compounder_provider_authority_policy(
    entitlement_owner_user_id: str,
) -> ProviderAuthorityPolicy:
    """Return the exact default-deny authority for prospective paper evidence."""

    owner = str(entitlement_owner_user_id).strip()
    if not owner or owner != entitlement_owner_user_id:
        _invalid("quality-compounder entitlement owner must be canonical non-empty text")
    return ProviderAuthorityPolicy(
        policy_version=_AUTHORITY_POLICY_VERSION,
        data_use_scope=DataUseScope.PAPER_FORWARD,
        rules=(
            ProviderAuthorityRule(
                provider="eodhd",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("eodhd-personal-use-paper-only",),
                entitlement_owner_user_id=owner,
            ),
            ProviderAuthorityRule(
                provider="ice_nyse",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("public-official-exchange-publications",),
            ),
            ProviderAuthorityRule(
                provider="sec",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("public-sec-edgar",),
            ),
            ProviderAuthorityRule(
                provider="vynmatrix",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("vynmatrix-owner-derived-paper-only",),
                entitlement_owner_user_id=owner,
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class QualityCompounderPanelResolution:
    """Recomputed panel plus the immutable authority used to derive it."""

    panel_input: USQualityCompounderPanelInput
    authority_payload: Mapping[str, Any]


class QualityCompounderPanelResolver(Protocol):
    """Database resolver implemented by the ingestion composition root."""

    def resolve_quality_compounder_panel(
        self,
        *,
        request: StrategyPanelPayloadValidationRequest,
    ) -> QualityCompounderPanelResolution:
        """Rebuild one exact strategy panel from persisted immutable evidence."""


ResolverFactory = Callable[[Session], QualityCompounderPanelResolver]


class DatabaseQualityCompounderPanelResolver:
    """Resolve the trusted derived manifest and all of its upstream evidence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def resolve_quality_compounder_panel(
        self,
        *,
        request: StrategyPanelPayloadValidationRequest,
    ) -> QualityCompounderPanelResolution:
        if request.strategy_id != "us_quality_compounder_v1":
            _invalid("database resolver received an incompatible strategy")
        if request.universe_code != "SP500":
            _invalid("database resolver received an incompatible universe")
        source_record_identity = _manifest_record_identity(
            strategy_id=request.strategy_id,
            strategy_version=request.strategy_version,
            panel=request.panel,
        )
        rows = list(
            self._session.execute(
                select(EquityObservation, EquitySourceLineage)
                .join(
                    EquitySourceLineage,
                    EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
                )
                .where(
                    EquityObservation.observation_kind == "benchmark",
                    EquityObservation.source_record_identity == source_record_identity,
                    EquitySourceLineage.provider == "vynmatrix",
                    EquitySourceLineage.product == _MANIFEST_PRODUCT,
                )
            ).all()
        )
        if len(rows) != 1:
            _invalid("exactly one quality-compounder derived manifest is required")
        observation, lineage = rows[0]
        validated, validated_lineage = validate_equity_observation_authority(
            self._session,
            observation_id=str(observation.observation_id),
            expected_kind="benchmark",
            cutoff=request.panel.cutoff,
            provider_authority_policy=request.panel.provider_authority_policy,
            expected_instrument_id=None,
        )
        if (
            str(lineage.lineage_id) != str(validated_lineage.lineage_id)
            or str(lineage.entitlement_scope) != _MANIFEST_SCOPE
            or _db_utc(validated.event_at) != _utc(request.panel.cutoff)
        ):
            _invalid("derived manifest authority differs from the submitted cutoff")
        values = list(
            self._session.scalars(
                select(EquityObservationValue)
                .where(EquityObservationValue.observation_id == observation.observation_id)
                .order_by(EquityObservationValue.field_name, EquityObservationValue.ordinal)
            )
        )
        by_name: dict[str, list[EquityObservationValue]] = {}
        for value in values:
            by_name.setdefault(str(value.field_name), []).append(value)
        required_single = {
            "configuration_sha256",
            "fundamental_snapshot_sha256",
            "market_policy_json",
            "market_policy_sha256",
            "market_snapshot_sha256",
            "panel_payload_json",
            "strategy_input_sha256",
        }
        if set(by_name) != required_single | {"source_observation_id"} or any(
            len(by_name[name]) != 1 for name in required_single
        ):
            _invalid("derived manifest values are incomplete or incompatible")
        payload_json = _text_value(by_name, "panel_payload_json")
        try:
            decoded = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            message = "derived manifest panel payload is invalid JSON"
            raise QualityCompounderPanelError(message) from exc
        if not isinstance(decoded, dict):
            _invalid("derived manifest panel payload must be an object")
        panel_input = panel_input_from_payload(decoded)
        if panel_ready_input_to_payload(panel_input.panel) != panel_ready_input_to_payload(
            request.panel
        ):
            _invalid("derived manifest embeds a different generic panel")
        input_sha256 = canonical_json_hash(decoded)
        if _text_value(by_name, "strategy_input_sha256") != input_sha256:
            _invalid("derived manifest strategy digest differs from its payload")
        if _text_value(by_name, "configuration_sha256") != (
            quality_compounder_configuration_sha256(request.strategy_version)
        ):
            _invalid("derived manifest configuration digest is incompatible")
        market_policy_json = _text_value(by_name, "market_policy_json")
        try:
            market_policy_payload = json.loads(market_policy_json)
        except json.JSONDecodeError as exc:
            message = "derived manifest market policy is invalid JSON"
            raise QualityCompounderPanelError(message) from exc
        if (
            not isinstance(market_policy_payload, dict)
            or json.dumps(
                market_policy_payload,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            != market_policy_json
            or canonical_json_hash(market_policy_payload)
            != _text_value(by_name, "market_policy_sha256")
        ):
            _invalid("derived manifest market policy identity is incompatible")
        source_ids = tuple(
            str(value.text_value) for value in by_name.get("source_observation_id", ())
        )
        if not source_ids or len(source_ids) != len(set(source_ids)):
            _invalid("derived manifest source observation ledger is incomplete")
        source_authority = tuple(
            _validate_manifest_source(
                self._session,
                observation_id=observation_id,
                panel=request.panel,
            )
            for observation_id in source_ids
        )
        authority_payload = {
            "manifest_observation_id": str(observation.observation_id),
            "manifest_semantic_sha256": equity_observation_semantic_sha256(
                validated,
                validated_lineage,
            ),
            "market_snapshot_sha256": _text_value(by_name, "market_snapshot_sha256"),
            "market_policy": market_policy_payload,
            "market_policy_sha256": _text_value(by_name, "market_policy_sha256"),
            "fundamental_snapshot_sha256": _text_value(
                by_name,
                "fundamental_snapshot_sha256",
            ),
            "configuration_sha256": _text_value(by_name, "configuration_sha256"),
            "source_observations": list(source_authority),
        }
        return QualityCompounderPanelResolution(
            panel_input=panel_input,
            authority_payload=authority_payload,
        )


class QualityCompounderPanelPayloadValidator:
    """Mandatory persistence hook that accepts only a DB-recomputed payload."""

    def __init__(self, *, resolver_factory: ResolverFactory) -> None:
        if not callable(resolver_factory):
            _invalid("quality-compounder resolver_factory must be callable")
        self._resolver_factory = resolver_factory

    def validate_strategy_panel_payload(
        self,
        session: Session,
        *,
        request: StrategyPanelPayloadValidationRequest,
    ) -> StrategyPanelPayloadValidationResult:
        resolver = self._resolver_factory(session)
        resolution = resolver.resolve_quality_compounder_panel(request=request)
        if not isinstance(resolution, QualityCompounderPanelResolution):
            _invalid("quality-compounder resolver returned an incompatible result")
        expected_payload = panel_input_to_payload(resolution.panel_input)
        if expected_payload != dict(request.strategy_input_payload):
            _invalid("strategy payload differs from recomputed immutable evidence")
        if canonical_json_hash(expected_payload) != request.strategy_input_sha256:
            _invalid("strategy payload digest differs after evidence reconstruction")
        authority = {
            "schema": "us-quality-compounder-panel-authority-v1",
            "derivation_version": QUALITY_COMPOUNDER_PANEL_DERIVATION_VERSION,
            "generic_panel_sha256": request.panel_sha256,
            "strategy_input_sha256": request.strategy_input_sha256,
            "evidence": dict(resolution.authority_payload),
        }
        return StrategyPanelPayloadValidationResult(
            validator_id=_VALIDATOR_ID,
            validator_version=_VALIDATOR_VERSION,
            validated_input_sha256=request.strategy_input_sha256,
            authority_sha256=canonical_json_hash(authority),
            authority_payload=authority,
        )


def persist_quality_compounder_panel_manifest(
    session: Session,
    *,
    panel_input: USQualityCompounderPanelInput,
    strategy_version: str,
    entitlement_owner_user_id: str,
    market_policy: EquityMarketFactorPolicy,
    market_snapshot_sha256: str,
    fundamental_snapshot_sha256: str,
    source_observation_ids: Sequence[str],
) -> EquityObservation:
    """Persist the deterministic derived panel and its complete upstream ledger."""

    payload = panel_input_to_payload(panel_input)
    payload_json = json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
    input_sha256 = canonical_json_hash(payload)
    configuration_sha256 = quality_compounder_configuration_sha256(strategy_version)
    if not isinstance(market_policy, EquityMarketFactorPolicy):
        _invalid("derived manifest requires an EquityMarketFactorPolicy")
    market_policy_payload = market_policy.to_payload()
    market_policy_json = json.dumps(
        market_policy_payload,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    sources = tuple(sorted(source_observation_ids))
    if not sources or len(sources) != len(set(sources)):
        _invalid("derived manifest requires unique upstream observation identities")
    for source_id in sources:
        _digest(source_id, field_name="source_observation_id")
    for name, digest in (
        ("market_snapshot_sha256", market_snapshot_sha256),
        ("fundamental_snapshot_sha256", fundamental_snapshot_sha256),
    ):
        _digest(digest, field_name=name)
    values = tuple(
        sorted(
            (
                EquityObservationValueInput(
                    field_name="configuration_sha256",
                    value_type="text",
                    value=configuration_sha256,
                ),
                EquityObservationValueInput(
                    field_name="fundamental_snapshot_sha256",
                    value_type="text",
                    value=fundamental_snapshot_sha256,
                ),
                EquityObservationValueInput(
                    field_name="market_policy_json",
                    value_type="text",
                    value=market_policy_json,
                ),
                EquityObservationValueInput(
                    field_name="market_policy_sha256",
                    value_type="text",
                    value=market_policy.configuration_sha256,
                ),
                EquityObservationValueInput(
                    field_name="market_snapshot_sha256",
                    value_type="text",
                    value=market_snapshot_sha256,
                ),
                EquityObservationValueInput(
                    field_name="panel_payload_json",
                    value_type="text",
                    value=payload_json,
                ),
                EquityObservationValueInput(
                    field_name="strategy_input_sha256",
                    value_type="text",
                    value=input_sha256,
                ),
                *(
                    EquityObservationValueInput(
                        field_name="source_observation_id",
                        ordinal=ordinal,
                        value_type="text",
                        value=source_id,
                    )
                    for ordinal, source_id in enumerate(sources)
                ),
            ),
            key=lambda item: (item.field_name, item.ordinal),
        )
    )
    normalized_sha256 = canonical_json_hash(
        {
            "schema": "us-quality-compounder-derived-manifest-v1",
            "values": [item.payload() for item in values],
        }
    )
    panel = panel_input.panel
    return persist_equity_observation(
        session,
        EquityObservationSubmission(
            provider="vynmatrix",
            product=_MANIFEST_PRODUCT,
            endpoint="internal:quality-compounder-panel-builder",
            dataset_version="prospective-derived-panel-v2",
            tool_version=_MANIFEST_TOOL_VERSION,
            source_identity=(
                f"{panel_input.panel.data_use_scope.value}:us_quality_compounder_v1:"
                f"{strategy_version}"
            ),
            source_revision=input_sha256,
            retrieved_at=panel.cutoff,
            timestamp_semantics={
                "event_at": "strategy panel knowledge cutoff",
                "available_at": "atomic derived-manifest persistence time",
                "derivation_version": QUALITY_COMPOUNDER_PANEL_DERIVATION_VERSION,
            },
            adjustment_policy="deterministic-provider-neutral-derivation",
            entitlement_scope=_MANIFEST_SCOPE,
            entitlement_owner_user_id=entitlement_owner_user_id,
            missing_data_policy="all-required-inputs-fail-closed",
            artifact_content_sha256=normalized_sha256,
            instrument_id=None,
            observation_kind="benchmark",
            source_record_identity=_manifest_record_identity(
                strategy_id="us_quality_compounder_v1",
                strategy_version=strategy_version,
                panel=panel,
            ),
            event_at=panel.cutoff,
            available_at=panel.cutoff,
            disposition="observed",
            normalized_content_sha256=normalized_sha256,
            values=values,
        ),
    )


def build_quality_compounder_panel_input(
    *,
    panel: PanelReadyInput,
    market: EquityMarketFactorSnapshot,
    fundamentals: FundamentalPanelSnapshot,
    market_cap_by_symbol: Mapping[str, MarketCapitalizationEvidence],
    factor_snapshot_id_by_security: Mapping[str, str],
    selection_policy: QualityCompounderPolicy | None = None,
    evidence_policy: QualityCompounderEvidencePolicy | None = None,
) -> USQualityCompounderPanelInput:
    """Derive every behavior-changing strategy field from one synchronized panel."""

    selection = selection_policy or QualityCompounderPolicy()
    evidence = evidence_policy or QualityCompounderEvidencePolicy()
    if market.cutoff != _utc(panel.cutoff) or fundamentals.cutoff != _utc(panel.cutoff):
        _invalid("market, fundamental, and generic panels must share the exact cutoff")
    if market.effective_session != panel.session.session_date:
        _invalid("market panel effective session differs from the generic panel")
    observed_security_ids = {item.security_id for item in panel.observations}
    if set(factor_snapshot_id_by_security) != observed_security_ids:
        _invalid("factor snapshot identities must exactly cover observed panel members")
    market_by_security = {item.security.security_id: item for item in market.instruments}
    if len(market_by_security) != len(market.instruments):
        _invalid("market panel contains duplicate security identities")
    if not observed_security_ids <= set(market_by_security):
        _invalid("observed panel member lacks complete market factor evidence")

    fundamental_by_key = _fundamental_by_key(fundamentals.sleeve_observations)
    group_members = tuple(
        QualityCompounderGroupMember(
            entity_id=item.security.security_id,
            sector=f"sector:{item.security.sector}",
            industry=f"industry:{item.security.industry}",
            price_momentum=item.price_momentum,
            trend_return=item.trend_return,
            fundamental_growth=_required_fundamental_raw(
                fundamental_by_key,
                symbol=item.security.symbol,
                factor_name="fundamental_growth",
            ),
        )
        for item in market.instruments
        if _complete_fundamental(
            fundamental_by_key.get((item.security.symbol, "fundamental_growth"))
        )
    )
    sector_scores = calculate_quality_compounder_group_scores(
        group_members,
        level=QualityCompounderGroupLevel.SECTOR,
        policy=evidence,
    )
    industry_scores = calculate_quality_compounder_group_scores(
        group_members,
        level=QualityCompounderGroupLevel.INDUSTRY,
        policy=evidence,
    )
    entries_allowed = quality_compounder_entries_allowed(
        benchmark_trend_score=market.regime.benchmark_trend_score,
        breadth_score=market.regime.breadth_score,
        breadth_coverage_ratio=market.regime.breadth_coverage_ratio,
        realized_volatility=market.regime.realized_volatility,
        policy=evidence,
    )

    entities: list[CrossSectionalEntity] = []
    observations: list[FactorObservation] = []
    securities: list[QualityCompounderSecurity] = []
    for security_id in sorted(observed_security_ids):
        market_item = market_by_security[security_id]
        security = market_item.security
        sector = f"sector:{security.sector}"
        industry = f"industry:{security.industry}"
        cap = market_cap_by_symbol.get(security.symbol)
        if cap is None or cap.available_at > panel.cutoff:
            _invalid("observed factor-complete member lacks cutoff-safe market capitalization")
        market_cap_usd = float(cap.value)
        reason = quality_compounder_market_ineligibility_reason(
            quote_currency=security.quote_currency,
            tradable=security.tradable,
            market_cap_usd=market_cap_usd,
            reference_price=market_item.reference_price,
            median_dollar_volume=market_item.median_dollar_volume,
            expected_round_trip_cost_bps=market_item.expected_round_trip_cost_bps,
            worst_gap_return=market_item.worst_gap_return,
            downside_volatility=market_item.downside_volatility,
            corporate_action_clear=market_item.corporate_action_clear,
            data_quality_passed=market_item.data_quality_passed,
            evidence_policy=evidence,
            selection_policy=selection,
        )
        sector_score = sector_scores.get(sector)
        industry_score = industry_scores.get(industry)
        if reason is None and sector_score is None:
            reason = "sector_score_unavailable"
        if reason is None and industry_score is None:
            reason = "industry_score_unavailable"
        bucket = (
            quality_compounder_market_cap_bucket(market_cap_usd, policy=evidence)
            if market_cap_usd >= evidence.minimum_market_cap_usd
            else None
        )
        entities.append(
            CrossSectionalEntity(
                entity_id=security_id,
                symbol=security.symbol,
                peer_groups=(industry, sector),
            )
        )
        observations.append(
            FactorObservation(
                entity_id=security_id,
                factor_name="momentum",
                raw_value=market_item.price_momentum,
                source_observation_ids=market_item.source_observation_ids,
            )
        )
        observations.extend(
            _fundamental_observations(
                security_id=security_id,
                symbol=security.symbol,
                fundamental_by_key=fundamental_by_key,
            )
        )
        securities.append(
            QualityCompounderSecurity(
                entity_id=security_id,
                instrument_id=security.instrument_id,
                symbol=security.symbol,
                factor_snapshot_id=factor_snapshot_id_by_security[security_id],
                sector=sector,
                industry=industry,
                market_cap_bucket=bucket,
                sector_score=sector_score,
                industry_score=industry_score,
                reference_price=market_item.reference_price,
                expected_round_trip_cost_bps=market_item.expected_round_trip_cost_bps,
                market_eligible=reason is None,
                market_ineligibility_reason=reason,
            )
        )
    return USQualityCompounderPanelInput(
        panel=panel,
        entries_allowed=entries_allowed,
        entities=tuple(entities),
        factor_observations=tuple(observations),
        securities=tuple(securities),
    )


def _fundamental_by_key(
    observations: Sequence[FactorObservation],
) -> dict[tuple[str, str], FactorObservation]:
    result: dict[tuple[str, str], FactorObservation] = {}
    for observation in observations:
        if observation.factor_name not in _FUNDAMENTAL_FACTORS:
            _invalid("fundamental panel contains an unsupported sleeve")
        key = (observation.entity_id, observation.factor_name)
        if key in result:
            _invalid("fundamental panel contains a duplicate sleeve observation")
        result[key] = observation
    return result


def _fundamental_observations(
    *,
    security_id: str,
    symbol: str,
    fundamental_by_key: Mapping[tuple[str, str], FactorObservation],
) -> tuple[FactorObservation, ...]:
    return tuple(
        FactorObservation(
            entity_id=security_id,
            factor_name=factor_name,
            raw_value=(
                source := _required_fundamental(
                    fundamental_by_key,
                    symbol=symbol,
                    factor_name=factor_name,
                )
            ).raw_value,
            missing_reason=source.missing_reason,
            source_observation_ids=source.source_observation_ids,
        )
        for factor_name in sorted(_FUNDAMENTAL_FACTORS)
    )


def _required_fundamental(
    values: Mapping[tuple[str, str], FactorObservation],
    *,
    symbol: str,
    factor_name: str,
) -> FactorObservation:
    value = values.get((symbol, factor_name))
    if value is None:
        _invalid(f"fundamental panel lacks {symbol}.{factor_name}")
    return value


def _required_fundamental_raw(
    values: Mapping[tuple[str, str], FactorObservation],
    *,
    symbol: str,
    factor_name: str,
) -> float:
    value = _required_fundamental(values, symbol=symbol, factor_name=factor_name)
    if value.raw_value is None:
        _invalid(f"fundamental panel has incomplete {symbol}.{factor_name}")
    return value.raw_value


def _complete_fundamental(value: FactorObservation | None) -> bool:
    return value is not None and value.raw_value is not None


def _manifest_record_identity(
    *,
    strategy_id: str,
    strategy_version: str,
    panel: PanelReadyInput,
) -> str:
    return ":".join(
        (
            strategy_id,
            strategy_version,
            panel.session.session_date.isoformat(),
            panel.cutoff.isoformat(),
        )
    )


def _text_value(
    values: Mapping[str, list[EquityObservationValue]],
    field_name: str,
) -> str:
    value = values[field_name][0].text_value
    if not isinstance(value, str) or not value:
        _invalid(f"derived manifest {field_name} must be non-empty text")
    return value


def _validate_manifest_source(
    session: Session,
    *,
    observation_id: str,
    panel: PanelReadyInput,
) -> dict[str, str]:
    _digest(observation_id, field_name="source_observation_id")
    row = session.execute(
        select(EquityObservation, EquitySourceLineage)
        .join(
            EquitySourceLineage,
            EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
        )
        .where(EquityObservation.observation_id == observation_id)
    ).one_or_none()
    if row is None:
        _invalid("derived manifest references an unavailable source observation")
    observation, lineage = row
    if (
        str(observation.disposition) != "observed"
        or observation.available_at is None
        or _db_utc(observation.available_at) > _utc(panel.cutoff)
        or _db_utc(observation.event_at) > _utc(panel.cutoff)
    ):
        _invalid("derived manifest source was not usable at the panel cutoff")
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
        message = "derived manifest source is outside provider authority"
        raise QualityCompounderPanelError(message) from exc
    return {
        "observation_id": observation_id,
        "semantic_sha256": equity_observation_semantic_sha256(observation, lineage),
    }


def _digest(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _invalid(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid("quality-compounder panel cutoff must be timezone-aware")
    return value.astimezone(UTC)


def _db_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "DatabaseQualityCompounderPanelResolver",
    "QualityCompounderPanelError",
    "QualityCompounderPanelPayloadValidator",
    "QualityCompounderPanelResolution",
    "QualityCompounderPanelResolver",
    "build_quality_compounder_panel_input",
    "persist_quality_compounder_panel_manifest",
    "quality_compounder_provider_authority_policy",
]
