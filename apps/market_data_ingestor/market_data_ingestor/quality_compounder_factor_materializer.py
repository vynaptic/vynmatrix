"""Cutoff-safe DB materializer for US Quality Compounder factors.

The adapter reconstructs retained market and fundamental calculator inputs from
immutable observations, persists the four exact factor snapshots, and exposes
their result to the panel registrar. The caller owns the transaction; no network
work is performed here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import NoReturn

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    EquityObservation,
    EquityObservationValue,
    EquitySecurityIdentity,
    EquitySourceLineage,
    Instrument,
    MarketCalendar,
    MarketSession,
    StrategyVersion,
)
from lib_application.services.equity_factor_snapshots import (
    PersistedEquityFactorSnapshot,
    persist_equity_factor_snapshot,
)
from lib_application.services.equity_lineage import (
    equity_observation_with_values_sha256,
    validate_equity_observation_authority,
)
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
    persist_equity_observation,
)
from lib_application.services.quality_compounder_factor_builder import (
    QualityCompounderFactorBuild,
    QualityCompounderFactorMember,
    build_quality_compounder_factor_submissions,
)
from lib_application.services.strategy_panel_sessions import (
    market_session_content_sha256,
    market_session_source_identity,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.cross_sectional import CrossSectionalEntity, FactorObservation
from lib_strategy.data_authority import ProviderAuthorityPolicy
from lib_strategy.equity_market_factors import (
    STRUCTURAL_BREADTH_EXCLUSION_REASON,
    DailyEquityMarketObservation,
    EquityMarketFactorInput,
    EquityMarketFactorPolicy,
    EquityMarketFactorSnapshot,
    PointInTimeEquitySecurity,
    StructuralBreadthExclusion,
    calculate_equity_market_factors,
)
from lib_strategy.equity_quality_compounder import (
    QUALITY_COMPOUNDER_MINIMUM_PEER_COUNT,
    QUALITY_COMPOUNDER_WINSORIZE_LIMIT,
    QualityCompounderEvidencePolicy,
    quality_compounder_configuration_sha256,
)
from lib_strategy.panels import (
    EffectivePanelMember,
    OfficialSessionCutoff,
    PanelReadyInput,
    SessionAuthority,
)

from .equity_factors import (
    CanonicalFundamentalFact,
    CanonicalFundamentalMetric,
    FundamentalCalculationConfig,
    FundamentalPanelSnapshot,
    IssuerFundamentalEvidence,
    MarketCapitalizationEvidence,
    calculate_fundamental_panel,
    canonical_sec_tag_mapping,
    cutoff_safe_shares_outstanding,
    sic_peer_group,
)
from .sec_edgar import parse_submissions_acceptance_time

__all__ = [
    "PreparedQualityCompounderFactorEvidence",
    "QualityCompounderDatabaseFactorResolver",
    "QualityCompounderFactorMaterializationError",
    "QualityCompounderFactorMaterializationResult",
]

_SEC_FACT_FIELDS = frozenset(
    {
        "acceptance_time_raw",
        "cik",
        "end",
        "filed",
        "form",
        "historical_sic",
        "start",
        "tag",
        "taxonomy",
        "unit",
        "value",
    }
)
_ANNUAL_FORMS = frozenset({"10-K", "10-K/A"})
_SHARE_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
_DIGEST_LENGTH = 64
_CIK_LENGTH = 10
_STRATEGY_ID = "us_quality_compounder_v1"
_BENCHMARK_SYMBOL = "SPY"
_DERIVED_MARKET_PRODUCT = "derived-daily-market-with-corporate-actions"
_MARKET_CAP_SCOPE = "vynmatrix-owner-derived-paper-only"
_MARKET_CAP_TOOL_VERSION = "vynmatrix-quality-compounder-factor-v1"
_MAXIMUM_STRUCTURAL_EXCLUSION_FRACTION = 0.01


class QualityCompounderFactorMaterializationError(RuntimeError):
    """Persisted evidence cannot satisfy the retained factor calculator."""


def _invalid(message: str) -> NoReturn:
    raise QualityCompounderFactorMaterializationError(message)


@dataclass(frozen=True, slots=True)
class PreparedQualityCompounderFactorEvidence:
    """Exact fundamental result and market caps selected at one cutoff."""

    fundamental_evidence: tuple[IssuerFundamentalEvidence, ...]
    fundamental_snapshot: FundamentalPanelSnapshot
    market_cap_by_symbol: Mapping[str, MarketCapitalizationEvidence]
    maximum_available_at: datetime


@dataclass(frozen=True, slots=True)
class _IdentityMaterial:
    observation: EquityObservation
    lineage: EquitySourceLineage
    values: Mapping[str, EquityObservationValue]
    authority_sha256: str


@dataclass(frozen=True, slots=True)
class QualityCompounderFactorMaterializationResult:
    """Calculated panels and durable snapshot identities for a later registrar."""

    market_snapshot: EquityMarketFactorSnapshot
    fundamental_snapshot: FundamentalPanelSnapshot
    market_cap_by_symbol: Mapping[str, MarketCapitalizationEvidence]
    build: QualityCompounderFactorBuild
    persisted: tuple[PersistedEquityFactorSnapshot, ...]

    @property
    def factor_snapshot_id_by_security(self) -> Mapping[str, str]:
        security_by_instrument = {
            item.security.instrument_id: item.security.security_id
            for item in self.market_snapshot.instruments
        }
        security_by_instrument.update(
            {
                item.security.instrument_id: item.security.security_id
                for item in self.market_snapshot.regime.structural_breadth_exclusions
            }
        )
        return dict(
            sorted(
                (
                    security_by_instrument[submission.instrument_id],
                    persisted.factor_snapshot_id,
                )
                for submission, persisted in zip(
                    self.build.submissions,
                    self.persisted,
                    strict=True,
                )
            )
        )


class QualityCompounderDatabaseFactorResolver:
    """Reconstruct retained fundamental inputs from append-only DB evidence."""

    def __init__(
        self,
        session: Session,
        *,
        provider_authority_policy: ProviderAuthorityPolicy,
    ) -> None:
        if not isinstance(provider_authority_policy, ProviderAuthorityPolicy):
            _invalid("factor resolver requires a ProviderAuthorityPolicy")
        self._session = session
        self._provider_authority_policy = provider_authority_policy

    def materialize(
        self,
        *,
        panel: PanelReadyInput,
        strategy_version: str,
        strategy_version_id: int,
        market_policy: EquityMarketFactorPolicy,
    ) -> QualityCompounderFactorMaterializationResult:
        """Calculate and atomically stage exactly four factors for every member."""

        registered_version = self._session.get(StrategyVersion, strategy_version_id)
        if (
            registered_version is None
            or str(registered_version.strategy_id) != _STRATEGY_ID
            or str(registered_version.semver) != strategy_version
            or str(registered_version.status) != "active"
        ):
            _invalid("factor materialization strategy version authority is incompatible")
        configured = QualityCompounderEvidencePolicy()
        market_input = self.resolve_market_input(panel=panel, market_policy=market_policy)
        market_snapshot = calculate_equity_market_factors(market_input, market_policy)
        price_by_symbol = {item.security.symbol: item for item in market_snapshot.instruments}
        exclusion_by_symbol = {
            item.security.symbol: item
            for item in market_snapshot.regime.structural_breadth_exclusions
        }
        prepared = self._prepare_with_derived_evidence(
            panel=panel,
            config=FundamentalCalculationConfig(
                max_fundamental_age_days=configured.max_fundamental_age_days,
                minimum_peer_count=QUALITY_COMPOUNDER_MINIMUM_PEER_COUNT,
                winsorize_limit=QUALITY_COMPOUNDER_WINSORIZE_LIMIT,
            ),
            market_input=market_input,
            max_shares_age_days=configured.max_shares_age_days,
        )
        member_by_security = {item.security_id: item for item in panel.members}
        security_by_id = {item.security_id: item for item in market_input.members}
        security_by_id.update(
            {
                item.security.security_id: item.security
                for item in market_input.structural_breadth_exclusions
            }
        )
        members = tuple(
            QualityCompounderFactorMember(
                entity=CrossSectionalEntity(
                    entity_id=security_id,
                    symbol=security_by_id[security_id].symbol,
                    peer_groups=security_by_id[security_id].peer_groups,
                ),
                instrument_id=member_by_security[security_id].instrument_id,
            )
            for security_id in sorted(member_by_security)
        )
        fundamental_by_symbol = {
            (item.entity_id, item.factor_name): item
            for item in prepared.fundamental_snapshot.sleeve_observations
        }
        observations: list[FactorObservation] = []
        for member in members:
            market_item = price_by_symbol.get(member.entity.symbol)
            exclusion = exclusion_by_symbol.get(member.entity.symbol)
            if market_item is not None:
                observations.append(
                    FactorObservation(
                        entity_id=member.entity.entity_id,
                        factor_name="momentum",
                        raw_value=market_item.price_momentum,
                        source_observation_ids=market_item.source_observation_ids,
                    )
                )
            elif exclusion is not None:
                observations.append(
                    FactorObservation(
                        entity_id=member.entity.entity_id,
                        factor_name="momentum",
                        raw_value=None,
                        missing_reason=STRUCTURAL_BREADTH_EXCLUSION_REASON,
                        source_observation_ids=exclusion.source_observation_ids,
                    )
                )
            else:
                _invalid("factor member lacks market calculation or structural exclusion")
            for factor_name in ("fundamental_growth", "quality", "valuation"):
                source = fundamental_by_symbol[(member.entity.symbol, factor_name)]
                observations.append(
                    FactorObservation(
                        entity_id=member.entity.entity_id,
                        factor_name=factor_name,
                        raw_value=source.raw_value,
                        missing_reason=source.missing_reason,
                        source_observation_ids=source.source_observation_ids,
                    )
                )
        build = build_quality_compounder_factor_submissions(
            strategy_id=_STRATEGY_ID,
            strategy_version_id=strategy_version_id,
            effective_session=panel.session.session_date,
            cutoff_at=panel.cutoff,
            configuration_digest=quality_compounder_configuration_sha256(
                strategy_version,
                evidence_policy=configured,
            ),
            members=members,
            observations=tuple(observations),
        )
        persisted = tuple(
            persist_equity_factor_snapshot(
                self._session,
                submission,
                provider_authority_policy=self._provider_authority_policy,
            )
            for submission in build.submissions
        )
        return QualityCompounderFactorMaterializationResult(
            market_snapshot=market_snapshot,
            fundamental_snapshot=prepared.fundamental_snapshot,
            market_cap_by_symbol=prepared.market_cap_by_symbol,
            build=build,
            persisted=persisted,
        )

    def resolve_market_input(
        self,
        *,
        panel: PanelReadyInput,
        market_policy: EquityMarketFactorPolicy,
    ) -> EquityMarketFactorInput:
        """Resolve exact identities, official sessions, SPY, and derived daily bars."""

        if not isinstance(panel, PanelReadyInput):
            _invalid("market resolver requires a PanelReadyInput")
        if panel.provider_authority_policy.digest != self._provider_authority_policy.digest:
            _invalid("market resolver authority differs from the admitted panel")
        if not isinstance(market_policy, EquityMarketFactorPolicy):
            _invalid("market resolver requires an EquityMarketFactorPolicy")
        benchmark_instrument = self._session.scalar(
            select(Instrument).where(
                Instrument.asset_class == "etf",
                Instrument.canonical == _BENCHMARK_SYMBOL,
            )
        )
        if benchmark_instrument is None:
            _invalid("SPY benchmark is absent from the exact ETF catalogue")
        member_instrument_ids = tuple(item.instrument_id for item in panel.members)
        instruments = list(
            self._session.scalars(
                select(Instrument).where(
                    Instrument.instr_id.in_(
                        (*member_instrument_ids, int(benchmark_instrument.instr_id))
                    )
                )
            )
        )
        if len(instruments) != len(member_instrument_ids) + 1:
            _invalid("factor market input references an unknown catalogue instrument")
        member_ids = set(member_instrument_ids)
        for instrument in instruments:
            expected_asset_class = "equity" if int(instrument.instr_id) in member_ids else "etf"
            if (
                str(instrument.asset_class) != expected_asset_class
                or str(instrument.settlement_currency) != "USD"
                or not bool(instrument.is_tradable)
                or str(instrument.market_session_policy) != "scheduled"
            ):
                _invalid("member or SPY catalogue authority is incompatible")
        calendar_ids = {item.market_calendar_id for item in instruments}
        if None in calendar_ids or len(calendar_ids) != 1:
            _invalid("members and SPY must share one authoritative market calendar")
        raw_calendar_id = next(iter(calendar_ids))
        if raw_calendar_id is None:
            _invalid("factor market calendar identity is unavailable")
        calendar_id = int(raw_calendar_id)
        calendar = self._session.get(MarketCalendar, calendar_id)
        if calendar is None or calendar.observation_id is None:
            _invalid("factor market calendar lacks immutable authority")
        _calendar_observation, calendar_lineage = validate_equity_observation_authority(
            self._session,
            observation_id=calendar.observation_id,
            expected_kind="calendar",
            cutoff=panel.cutoff,
            provider_authority_policy=self._provider_authority_policy,
            expected_instrument_id=None,
        )
        sessions = list(
            self._session.scalars(
                select(MarketSession)
                .where(
                    MarketSession.calendar_id == calendar_id,
                    MarketSession.closes_at <= _stored(panel.session.closes_at),
                )
                .order_by(MarketSession.opens_at.desc())
                .limit(market_policy.required_history_sessions)
            )
        )
        sessions.reverse()
        if len(sessions) != market_policy.required_history_sessions:
            _invalid("official calendar lacks the exact market-factor lookback")
        authority = (
            SessionAuthority.OFFICIAL_EXCHANGE
            if str(calendar.source_kind) == "exchange"
            else SessionAuthority.AUTHENTICATED_BROKER
        )
        official_sessions = tuple(
            OfficialSessionCutoff(
                mic=str(calendar.code),
                session_date=_utc(item.opens_at).date(),
                opens_at=_utc(item.opens_at),
                closes_at=_utc(item.closes_at),
                authority=authority,
                source_identity=market_session_source_identity(calendar),
                content_sha256=market_session_content_sha256(
                    calendar,
                    item,
                    calendar_lineage,
                ),
            )
            for item in sessions
        )
        if official_sessions[-1] != panel.session:
            _invalid("factor market session differs from the admitted panel")
        identities, identity_material = self._market_identities(
            panel=panel,
            instrument_ids=(*member_instrument_ids, int(benchmark_instrument.instr_id)),
        )
        member_by_instrument = {item.instrument_id: item for item in panel.members}
        for instrument_id, member in member_by_instrument.items():
            identity = identities[instrument_id]
            if (
                identity.security_id != member.security_id
                or identity.issuer_id != member.issuer_id
                or identity.symbol != member.canonical_symbol
            ):
                _invalid("effective market identity differs from the admitted panel")
        prices = self._market_prices(
            panel=panel,
            identities=identities,
            sessions=official_sessions,
            required_adjustment_policy=market_policy.required_adjustment_policy,
            cost_context_sha256=market_policy.cost_context_sha256,
        )
        complete_ids, structural_exclusions = _partition_listing_warmups(
            panel=panel,
            sessions=official_sessions,
            benchmark_instrument_id=int(benchmark_instrument.instr_id),
            identities=identities,
            identity_material=identity_material,
            prices=prices,
        )
        return EquityMarketFactorInput(
            effective_session=panel.session.session_date,
            cutoff=panel.cutoff,
            official_sessions=official_sessions,
            members=tuple(
                identities[item.instrument_id]
                for item in panel.members
                if item.instrument_id in complete_ids
            ),
            benchmark=identities[int(benchmark_instrument.instr_id)],
            prices=prices,
            structural_breadth_exclusions=structural_exclusions,
        )

    def _market_identities(
        self,
        *,
        panel: PanelReadyInput,
        instrument_ids: tuple[int, ...],
    ) -> tuple[dict[int, PointInTimeEquitySecurity], dict[int, _IdentityMaterial]]:
        rows = list(
            self._session.scalars(
                select(EquitySecurityIdentity).where(
                    EquitySecurityIdentity.instr_id.in_(instrument_ids),
                    EquitySecurityIdentity.effective_from <= panel.session.session_date,
                    or_(
                        EquitySecurityIdentity.effective_to.is_(None),
                        EquitySecurityIdentity.effective_to >= panel.session.session_date,
                    ),
                )
            )
        )
        if len(rows) != len(instrument_ids):
            _invalid("every member and SPY require one effective security identity")
        result: dict[int, PointInTimeEquitySecurity] = {}
        material: dict[int, _IdentityMaterial] = {}
        for identity in rows:
            instrument_id = int(identity.instr_id)
            if instrument_id in result:
                _invalid("effective security identity intervals overlap")
            observation, lineage = validate_equity_observation_authority(
                self._session,
                observation_id=str(identity.observation_id),
                expected_kind="security_identity",
                cutoff=panel.cutoff,
                provider_authority_policy=self._provider_authority_policy,
                expected_instrument_id=instrument_id,
            )
            values = self._values_by_observation((str(observation.observation_id),))[
                str(observation.observation_id)
            ]
            required = {
                "industry",
                "issuer_id",
                "quote_currency",
                "sector",
                "security_id",
                "symbol",
                "tradable",
            }
            if not required <= set(values):
                _invalid("effective security identity values are incomplete")
            security = PointInTimeEquitySecurity(
                instrument_id=instrument_id,
                security_id=_text_value(values, "security_id"),
                issuer_id=_text_value(values, "issuer_id"),
                symbol=_text_value(values, "symbol"),
                sector=_text_value(values, "sector"),
                industry=_text_value(values, "industry"),
                quote_currency=_text_value(values, "quote_currency"),
                tradable=_boolean_value(values, "tradable"),
                observation_id=str(observation.observation_id),
                observation_sha256=equity_observation_with_values_sha256(
                    observation,
                    lineage,
                    dict(values),
                ),
            )
            if (
                security.security_id != str(identity.security_id)
                or security.issuer_id != str(identity.issuer_id)
                or security.symbol != str(identity.canonical_symbol)
            ):
                _invalid("security identity row differs from immutable observation")
            result[instrument_id] = security
            material[instrument_id] = _IdentityMaterial(
                observation=observation,
                lineage=lineage,
                values=values,
                authority_sha256=security.observation_sha256,
            )
        return result, material

    def _market_prices(
        self,
        *,
        panel: PanelReadyInput,
        identities: Mapping[int, PointInTimeEquitySecurity],
        sessions: tuple[OfficialSessionCutoff, ...],
        required_adjustment_policy: str,
        cost_context_sha256: str,
    ) -> tuple[DailyEquityMarketObservation, ...]:
        instrument_ids = tuple(identities)
        start = sessions[0].closes_at
        end = sessions[-1].closes_at
        rows = list(
            self._session.execute(
                select(EquityObservation, EquitySourceLineage)
                .join(
                    EquitySourceLineage,
                    EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
                )
                .where(
                    EquityObservation.instr_id.in_(instrument_ids),
                    EquityObservation.observation_kind == "price",
                    EquityObservation.event_at >= _stored(start),
                    EquityObservation.event_at <= _stored(end),
                    EquityObservation.available_at.is_not(None),
                    EquityObservation.available_at <= _stored(panel.cutoff),
                    EquitySourceLineage.product == _DERIVED_MARKET_PRODUCT,
                    EquitySourceLineage.adjustment_policy == required_adjustment_policy,
                )
                .order_by(
                    EquityObservation.instr_id,
                    EquityObservation.event_at,
                    EquityObservation.revision.desc(),
                )
            ).all()
        )
        latest: dict[tuple[int, datetime], tuple[EquityObservation, EquitySourceLineage]] = {}
        for observation, lineage in rows:
            if observation.instr_id is None:
                _invalid("derived market observation lacks an instrument")
            key = (int(observation.instr_id), _utc(observation.event_at))
            current = latest.get(key)
            if current is None or int(observation.revision) > int(current[0].revision):
                latest[key] = (observation, lineage)
        allowed = {
            (instrument_id, item.closes_at) for instrument_id in instrument_ids for item in sessions
        }
        if not set(latest) <= allowed:
            _invalid("member or SPY derived market history is off-calendar")
        values_by_id = self._values_by_observation(
            tuple(str(item[0].observation_id) for item in latest.values())
        )
        result: list[DailyEquityMarketObservation] = []
        session_by_close = {item.closes_at: item for item in sessions}
        for (instrument_id, closes_at), (observation, lineage) in sorted(latest.items()):
            if str(observation.disposition) != "observed":
                _invalid("derived market evidence must have observed disposition")
            validated, validated_lineage = validate_equity_observation_authority(
                self._session,
                observation_id=str(observation.observation_id),
                expected_kind="price",
                cutoff=panel.cutoff,
                provider_authority_policy=self._provider_authority_policy,
                expected_instrument_id=instrument_id,
            )
            if str(validated_lineage.lineage_id) != str(lineage.lineage_id):
                _invalid("derived market lineage changed during validation")
            values = values_by_id[str(observation.observation_id)]
            required = {
                "corporate_action_clear",
                "cost_context_sha256",
                "one_way_nonspread_cost_bps",
                "raw_close",
                "round_trip_spread_bps",
                "session_date",
                "split_adjusted_close",
                "split_adjusted_open",
                "split_adjusted_volume",
                "split_adjustment_factor",
                "total_return_close",
            }
            if set(values) != required:
                _invalid("derived market values differ from the registered factor contract")
            if _date_value(values, "session_date") != session_by_close[closes_at].session_date:
                _invalid("derived market session date differs from official calendar")
            stored_context = _text_value(values, "cost_context_sha256")
            if stored_context != cost_context_sha256:
                _invalid("derived transaction-cost context differs from market policy")
            result.append(
                DailyEquityMarketObservation(
                    instrument_id=instrument_id,
                    symbol=identities[instrument_id].symbol,
                    session_date=session_by_close[closes_at].session_date,
                    observed_at=_utc(validated.event_at),
                    available_at=_required_available(validated),
                    observation_id=str(validated.observation_id),
                    observation_sha256=equity_observation_with_values_sha256(
                        validated,
                        validated_lineage,
                        dict(values),
                    ),
                    provider=str(validated_lineage.provider),
                    timeframe="1d",
                    entitlement_scope=str(validated_lineage.entitlement_scope),
                    entitlement_owner_user_id=(
                        str(validated_lineage.entitlement_owner_user_id)
                        if validated_lineage.entitlement_owner_user_id is not None
                        else None
                    ),
                    total_return_close=float(_decimal_value(values, "total_return_close")),
                    split_adjusted_open=float(_decimal_value(values, "split_adjusted_open")),
                    split_adjusted_close=float(_decimal_value(values, "split_adjusted_close")),
                    split_adjusted_volume=float(_decimal_value(values, "split_adjusted_volume")),
                    split_adjustment_factor=float(
                        _decimal_value(values, "split_adjustment_factor")
                    ),
                    raw_close=float(_decimal_value(values, "raw_close")),
                    round_trip_spread_bps=float(_decimal_value(values, "round_trip_spread_bps")),
                    one_way_nonspread_cost_bps=float(
                        _decimal_value(values, "one_way_nonspread_cost_bps")
                    ),
                    cost_context_sha256=stored_context,
                    corporate_action_clear=_boolean_value(
                        values,
                        "corporate_action_clear",
                    ),
                )
            )
        return tuple(result)

    def prepare(
        self,
        *,
        panel: PanelReadyInput,
        config: FundamentalCalculationConfig,
    ) -> PreparedQualityCompounderFactorEvidence:
        """Select and calculate the three fundamental sleeves without writes."""

        if not isinstance(panel, PanelReadyInput):
            _invalid("factor resolver requires a PanelReadyInput")
        if panel.provider_authority_policy.digest != self._provider_authority_policy.digest:
            _invalid("factor resolver authority differs from the admitted panel")
        if not isinstance(config, FundamentalCalculationConfig):
            _invalid("factor resolver requires a FundamentalCalculationConfig")
        rows = self._selected_observations(panel)
        values = self._values_by_observation(
            tuple(str(row.observation_id) for row, _lineage in rows)
        )
        by_instrument: dict[int, list[tuple[EquityObservation, EquitySourceLineage]]] = defaultdict(
            list
        )
        issuer_scoped: dict[str, list[tuple[EquityObservation, EquitySourceLineage]]] = defaultdict(
            list
        )
        for row, lineage in rows:
            row_values = values[str(row.observation_id)]
            if row.instr_id is None:
                cik = _text_value(row_values, "cik")
                issuer_scoped[f"cik:{cik}"].append((row, lineage))
            else:
                by_instrument[int(row.instr_id)].append((row, lineage))

        evidence: list[IssuerFundamentalEvidence] = []
        used_availability: list[datetime] = []
        market_caps: dict[str, MarketCapitalizationEvidence] = {}
        for member in sorted(panel.members, key=lambda item: item.security_id):
            member_rows = (
                *by_instrument.get(member.instrument_id, ()),
                *issuer_scoped.get(member.issuer_id, ()),
            )
            issuer, available = self._issuer_evidence(
                member,
                member_rows,
                values=values,
                cutoff=panel.cutoff,
                decision_cutoff=panel.session.closes_at,
            )
            evidence.append(issuer)
            used_availability.extend(available)
            if issuer.market_cap is not None:
                market_caps[issuer.symbol] = issuer.market_cap
        if not used_availability:
            _invalid("factor resolver selected no cutoff-safe fundamental evidence")
        canonical_evidence = tuple(sorted(evidence, key=lambda item: item.symbol))
        snapshot = calculate_fundamental_panel(canonical_evidence, config)
        return PreparedQualityCompounderFactorEvidence(
            fundamental_evidence=canonical_evidence,
            fundamental_snapshot=snapshot,
            market_cap_by_symbol=dict(sorted(market_caps.items())),
            maximum_available_at=max(used_availability),
        )

    def _prepare_with_derived_evidence(
        self,
        *,
        panel: PanelReadyInput,
        config: FundamentalCalculationConfig,
        market_input: EquityMarketFactorInput,
        max_shares_age_days: int,
    ) -> PreparedQualityCompounderFactorEvidence:
        preliminary = self.prepare(panel=panel, config=config)
        prices_by_instrument: dict[int, dict[date, DailyEquityMarketObservation]] = defaultdict(
            dict
        )
        for price in market_input.prices:
            prices_by_instrument[price.instrument_id][price.session_date] = price
        instrument_by_symbol = {item.symbol: item.instrument_id for item in market_input.members}
        observation_instrument = {
            str(observation_id): instrument_id
            for observation_id, instrument_id in self._session.execute(
                select(EquityObservation.observation_id, EquityObservation.instr_id).where(
                    EquityObservation.observation_id.in_(
                        tuple(
                            fact.observation_id
                            for issuer in preliminary.fundamental_evidence
                            for fact in issuer.facts
                        )
                    )
                )
            ).all()
        }
        for issuer in preliminary.fundamental_evidence:
            instrument_id = instrument_by_symbol.get(issuer.symbol)
            if instrument_id is None:
                continue
            class_facts = tuple(
                fact
                for fact in issuer.facts
                if observation_instrument.get(fact.observation_id) == instrument_id
            )
            shares = cutoff_safe_shares_outstanding(
                class_facts,
                cutoff=panel.session.closes_at,
                maximum_age_days=max_shares_age_days,
            )
            if shares is None:
                continue
            decision_price = prices_by_instrument[instrument_id].get(panel.session.session_date)
            if decision_price is None:
                _invalid("market-cap derivation lacks the exact decision-close price")
            decision_price_values = self._values_by_observation((decision_price.observation_id,))[
                decision_price.observation_id
            ]
            self._persist_market_cap(
                panel=panel,
                instrument_id=instrument_id,
                symbol=issuer.symbol,
                shares=shares.value,
                share_observation_ids=shares.source_observation_ids,
                decision_price=decision_price,
                decision_raw_close=_decimal_value(decision_price_values, "raw_close"),
            )
        return self.prepare(panel=panel, config=config)

    def _persist_market_cap(
        self,
        *,
        panel: PanelReadyInput,
        instrument_id: int,
        symbol: str,
        shares: Decimal,
        share_observation_ids: tuple[str, ...],
        decision_price: DailyEquityMarketObservation,
        decision_raw_close: Decimal,
    ) -> EquityObservation:
        owner = self._provider_authority_policy.effective_entitlement_owner_user_id
        if owner is None:
            _invalid("derived market capitalization requires one entitlement owner")
        source_ids = tuple(sorted((*share_observation_ids, decision_price.observation_id)))
        source_rows = {
            str(row.observation_id): row
            for row in self._session.scalars(
                select(EquityObservation).where(EquityObservation.observation_id.in_(source_ids))
            )
        }
        if set(source_rows) != set(source_ids) or any(
            _required_available(row) > _utc(panel.cutoff) or str(row.disposition) != "observed"
            for row in source_rows.values()
        ):
            _invalid("market-cap source graph is unavailable at the factor cutoff")
        source_graph_sha256 = canonical_json_hash(
            {
                "schema": "quality-compounder-market-cap-source-v1",
                "source_observation_ids": list(source_ids),
                "shares": str(shares),
                "raw_close_usd": str(decision_raw_close),
            }
        )
        market_cap = shares * decision_raw_close
        values = (
            EquityObservationValueInput(
                field_name="market_cap",
                value_type="decimal",
                value=market_cap,
                unit="USD",
            ),
        )
        record_identity = f"{symbol}:{panel.session.session_date}:derived-market-cap"
        normalized_sha256 = canonical_json_hash(
            {
                "schema": "quality-compounder-derived-market-cap-v1",
                "source_record_identity": record_identity,
                "source_graph_sha256": source_graph_sha256,
                "values": [item.payload() for item in values],
            }
        )
        return persist_equity_observation(
            self._session,
            EquityObservationSubmission(
                provider="vynmatrix",
                product="quality-compounder-derived-market-cap",
                endpoint="internal:cutoff-safe-shares-times-decision-close",
                dataset_version="prospective-derived-market-cap-v1",
                tool_version=_MARKET_CAP_TOOL_VERSION,
                source_identity=f"vynmatrix:{symbol}:derived-market-cap",
                source_revision=source_graph_sha256,
                retrieved_at=panel.cutoff,
                timestamp_semantics={
                    "event_at": "completed decision-session close",
                    "available_at": "factor materialization cutoff",
                    "source_observation_ids": list(source_ids),
                    "source_graph_sha256": source_graph_sha256,
                },
                adjustment_policy="cutoff-safe-shares-times-raw-close-usd",
                entitlement_scope=_MARKET_CAP_SCOPE,
                entitlement_owner_user_id=owner,
                missing_data_policy="ambiguous-or-stale-share-count-fails-closed",
                artifact_content_sha256=source_graph_sha256,
                instrument_id=instrument_id,
                observation_kind="market_cap",
                source_record_identity=record_identity,
                event_at=panel.session.closes_at,
                available_at=panel.cutoff,
                disposition="observed",
                normalized_content_sha256=normalized_sha256,
                values=values,
            ),
        )

    def _selected_observations(
        self,
        panel: PanelReadyInput,
    ) -> tuple[tuple[EquityObservation, EquitySourceLineage], ...]:
        instrument_ids = tuple(member.instrument_id for member in panel.members)
        issuer_ciks = tuple(
            member.issuer_id.removeprefix("cik:")
            for member in panel.members
            if member.issuer_id.startswith("cik:")
        )
        rows = list(
            self._session.execute(
                select(EquityObservation, EquitySourceLineage)
                .join(
                    EquitySourceLineage,
                    EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
                )
                .where(
                    EquityObservation.observation_kind.in_(("xbrl_fact", "market_cap")),
                    EquityObservation.available_at.is_not(None),
                    EquityObservation.available_at <= _stored(panel.cutoff),
                    or_(
                        EquityObservation.instr_id.in_(instrument_ids),
                        EquityObservation.instr_id.is_(None),
                    ),
                )
                .order_by(
                    EquityObservation.source_record_identity,
                    EquityObservation.revision.desc(),
                    EquityObservation.observation_id,
                )
            ).all()
        )
        latest: dict[
            tuple[int | None, str, str, str, str, str],
            tuple[EquityObservation, EquitySourceLineage],
        ] = {}
        for row, lineage in rows:
            if row.instr_id is None and str(row.observation_kind) != "xbrl_fact":
                continue
            key = (
                int(row.instr_id) if row.instr_id is not None else None,
                str(row.observation_kind),
                str(lineage.provider),
                str(lineage.product),
                str(lineage.source_identity),
                str(row.source_record_identity),
            )
            current = latest.get(key)
            if current is None or int(row.revision) > int(current[0].revision):
                latest[key] = (row, lineage)
        selected: list[tuple[EquityObservation, EquitySourceLineage]] = []
        for row, lineage in latest.values():
            if str(row.disposition) != "observed":
                continue
            if row.instr_id is None:
                cik = _cik_from_source_identity(str(row.source_record_identity))
                if cik not in issuer_ciks:
                    continue
            validate_equity_observation_authority(
                self._session,
                observation_id=str(row.observation_id),
                expected_kind=str(row.observation_kind),
                cutoff=panel.cutoff,
                provider_authority_policy=self._provider_authority_policy,
                expected_instrument_id=(int(row.instr_id) if row.instr_id is not None else None),
            )
            selected.append((row, lineage))
        return tuple(
            sorted(
                selected,
                key=lambda item: (
                    item[0].instr_id or 0,
                    str(item[0].observation_kind),
                    _utc(item[0].event_at),
                    str(item[0].observation_id),
                ),
            )
        )

    def _issuer_evidence(
        self,
        member: EffectivePanelMember,
        rows: Sequence[tuple[EquityObservation, EquitySourceLineage]],
        *,
        values: Mapping[str, Mapping[str, EquityObservationValue]],
        cutoff: datetime,
        decision_cutoff: datetime,
    ) -> tuple[IssuerFundamentalEvidence, tuple[datetime, ...]]:
        facts: list[CanonicalFundamentalFact] = []
        classification_candidates: list[tuple[datetime, int, str, str]] = []
        market_cap_candidates: list[MarketCapitalizationEvidence] = []
        used_available: list[datetime] = []
        for row, lineage in rows:
            row_values = values[str(row.observation_id)]
            available_at = _required_available(row)
            if str(row.observation_kind) == "market_cap":
                market_cap = _market_cap(member.canonical_symbol, row, row_values)
                if market_cap is not None:
                    market_cap_candidates.append(market_cap)
                continue
            historical_sic = _historical_sic(row, row_values)
            semantics = _semantics(lineage)
            classification_sha256 = _digest_value(
                semantics.get("historical_sic_source_sha256"),
                field_name="historical_sic_source_sha256",
            )
            acceptance_raw = _text_value(row_values, "acceptance_time_raw")
            acceptance = parse_submissions_acceptance_time(acceptance_raw)
            if acceptance != _utc(row.event_at):
                _invalid("SEC acceptance time differs from persisted event authority")
            if acceptance > _utc(decision_cutoff):
                continue
            classification_candidates.append(
                (acceptance, historical_sic, classification_sha256, str(row.observation_id))
            )
            fact = _canonical_fact(
                symbol=member.canonical_symbol,
                row=row,
                lineage=lineage,
                values=row_values,
                historical_sic=historical_sic,
                classification_sha256=classification_sha256,
                cutoff=cutoff,
            )
            if fact is not None:
                facts.append(fact)
                used_available.append(available_at)
        if not classification_candidates:
            _invalid(f"{member.canonical_symbol} lacks cutoff-safe historical SIC filing evidence")
        latest_acceptance = max(item[0] for item in classification_candidates)
        latest = tuple(item for item in classification_candidates if item[0] == latest_acceptance)
        classifications = {(item[1], item[2]) for item in latest}
        if len(classifications) != 1:
            _invalid(f"{member.canonical_symbol} latest historical SIC evidence is ambiguous")
        historical_sic, _classification_sha256 = next(iter(classifications))
        classification_anchor = min(item[3] for item in latest)
        classification_available = min(
            _required_available(row)
            for row, _lineage in rows
            if str(row.observation_id) == classification_anchor
        )
        used_available.append(classification_available)
        market_cap = _latest_market_cap(member.canonical_symbol, market_cap_candidates)
        if market_cap is not None:
            used_available.append(market_cap.available_at)
        return (
            IssuerFundamentalEvidence(
                symbol=member.canonical_symbol,
                peer_group=sic_peer_group(historical_sic),
                cutoff=cutoff,
                historical_sic=historical_sic,
                classification_available_at=classification_available,
                classification_source_id=classification_anchor,
                facts=tuple(
                    sorted(
                        facts,
                        key=lambda item: (
                            item.period_end,
                            item.metric.value,
                            item.acceptance_time,
                            item.mapping_priority,
                            item.observation_id,
                        ),
                    )
                ),
                market_cap=market_cap,
                decision_cutoff=decision_cutoff,
            ),
            tuple(used_available),
        )

    def _values_by_observation(
        self,
        observation_ids: tuple[str, ...],
    ) -> dict[str, dict[str, EquityObservationValue]]:
        if not observation_ids:
            return {}
        rows = list(
            self._session.scalars(
                select(EquityObservationValue).where(
                    EquityObservationValue.observation_id.in_(observation_ids)
                )
            )
        )
        result: dict[str, dict[str, EquityObservationValue]] = defaultdict(dict)
        for row in rows:
            observation_id = str(row.observation_id)
            field_name = str(row.field_name)
            if int(row.ordinal) != 0 or field_name in result[observation_id]:
                _invalid("factor source fields must be unique scalar values")
            result[observation_id][field_name] = row
        if set(result) != set(observation_ids):
            _invalid("factor source observation values are incomplete")
        return dict(result)


def _canonical_fact(
    *,
    symbol: str,
    row: EquityObservation,
    lineage: EquitySourceLineage,
    values: Mapping[str, EquityObservationValue],
    historical_sic: int,
    classification_sha256: str,
    cutoff: datetime,
) -> CanonicalFundamentalFact | None:
    if not set(values) <= _SEC_FACT_FIELDS or "value" not in values:
        _invalid("persisted SEC fact contains an incompatible field contract")
    taxonomy = _text_value(values, "taxonomy")
    raw_tag = _text_value(values, "tag")
    mapping = canonical_sec_tag_mapping(taxonomy, raw_tag)
    if mapping is None:
        return None
    metric, mapping_priority = mapping
    value = values["value"]
    form = _text_value(values, "form")
    unit = _text_value(values, "unit")
    if form != str(row.filing_form) or not isinstance(row.accession_number, str):
        _invalid("persisted SEC fact differs from filing metadata")
    if value.value_type != "decimal" or value.decimal_value is None or value.period_end is None:
        return None
    if metric is CanonicalFundamentalMetric.SHARES_OUTSTANDING:
        if form not in _SHARE_FORMS or unit != "shares" or value.period_start is not None:
            return None
    elif form not in _ANNUAL_FORMS or value.fiscal_period != "FY" or unit != "USD":
        return None
    acceptance_raw = _text_value(values, "acceptance_time_raw")
    acceptance = parse_submissions_acceptance_time(acceptance_raw)
    if acceptance > _utc(cutoff) or value.period_end > _utc(cutoff).date():
        return None
    semantics = _semantics(lineage)
    return CanonicalFundamentalFact(
        symbol=symbol,
        metric=metric,
        value=Decimal(value.decimal_value),
        period_start=value.period_start,
        period_end=value.period_end,
        acceptance_time_raw=acceptance_raw,
        acceptance_time=acceptance,
        accession=row.accession_number,
        historical_sic=historical_sic,
        taxonomy=taxonomy,
        raw_tag=raw_tag,
        mapping_priority=mapping_priority,
        source_sha256=_digest_value(lineage.source_revision, field_name="SEC source_revision"),
        availability_source_sha256=_digest_value(
            semantics.get("filing_source_sha256"),
            field_name="filing_source_sha256",
        ),
        classification_source_sha256=classification_sha256,
        observation_id=str(row.observation_id),
    )


def _partition_listing_warmups(
    *,
    panel: PanelReadyInput,
    sessions: tuple[OfficialSessionCutoff, ...],
    benchmark_instrument_id: int,
    identities: Mapping[int, PointInTimeEquitySecurity],
    identity_material: Mapping[int, _IdentityMaterial],
    prices: Sequence[DailyEquityMarketObservation],
) -> tuple[set[int], tuple[StructuralBreadthExclusion, ...]]:
    required_dates = tuple(item.session_date for item in sessions)
    required_set = set(required_dates)
    dates_by_instrument: dict[int, set[date]] = defaultdict(set)
    price_by_key: dict[tuple[int, date], DailyEquityMarketObservation] = {}
    for price in prices:
        dates_by_instrument[price.instrument_id].add(price.session_date)
        price_by_key[(price.instrument_id, price.session_date)] = price
    if dates_by_instrument[benchmark_instrument_id] != required_set:
        _invalid("SPY derived market history must cover every official session")
    complete: set[int] = set()
    exclusions: list[StructuralBreadthExclusion] = []
    for member in sorted(panel.members, key=lambda item: item.security_id):
        instrument_id = member.instrument_id
        observed_dates = tuple(sorted(dates_by_instrument[instrument_id]))
        if set(observed_dates) == required_set:
            complete.add(instrument_id)
            continue
        material = identity_material[instrument_id]
        listing_value = material.values.get("listing_date")
        if (
            listing_value is None
            or listing_value.value_type != "date"
            or listing_value.date_value is None
        ):
            _invalid(f"{member.canonical_symbol} incomplete history lacks listing evidence")
        listing_date = listing_value.date_value
        listing_session = next((item for item in required_dates if item >= listing_date), None)
        if listing_session is None:
            _invalid("listing evidence begins after the panel decision session")
        expected_missing = tuple(item for item in required_dates if item < listing_session)
        expected_observed = tuple(item for item in required_dates if item >= listing_session)
        if not expected_missing or observed_dates != expected_observed:
            _invalid(f"{member.canonical_symbol} has an unexplained post-listing price gap")
        observed_prices = tuple(
            price_by_key[(instrument_id, session_date)] for session_date in observed_dates
        )
        source_graph = material.values.get("source_graph_sha256")
        evidence_sha256 = (
            str(source_graph.text_value)
            if source_graph is not None
            and source_graph.value_type == "text"
            and source_graph.text_value is not None
            else material.authority_sha256
        )
        _digest_value(evidence_sha256, field_name="listing evidence_sha256")
        security = identities[instrument_id]
        exclusions.append(
            StructuralBreadthExclusion(
                security=security,
                reason_code=STRUCTURAL_BREADTH_EXCLUSION_REASON,
                listing_date=listing_date,
                listing_session=listing_session,
                observed_history_sessions=len(observed_dates),
                required_history_sessions=len(required_dates),
                missing_session_dates=expected_missing,
                observed_session_dates=observed_dates,
                source_observation_ids=tuple(item.observation_id for item in observed_prices),
                source_observation_sha256s=tuple(
                    item.observation_sha256 for item in observed_prices
                ),
                membership_interval_id=canonical_json_hash(
                    {
                        "schema": "quality-compounder-membership-interval-v1",
                        "membership_sha256": panel.membership_sha256,
                        "security_id": member.security_id,
                        "effective_session": panel.session.session_date.isoformat(),
                    }
                ),
                evidence_id=str(material.observation.observation_id),
                evidence_provider=str(material.lineage.provider),
                evidence_provider_symbol=f"{security.symbol}.US",
                evidence_artifact_role="security-identity-general-listing-date",
                evidence_source_ref=str(material.observation.source_record_identity),
                evidence_retrieved_at=_utc(material.lineage.retrieved_at),
                evidence_sha256=evidence_sha256,
                identity_binding=canonical_json_hash(
                    {
                        "schema": "quality-compounder-listing-identity-binding-v1",
                        "security_id": security.security_id,
                        "identity_observation_id": security.observation_id,
                        "identity_observation_sha256": security.observation_sha256,
                    }
                ),
            )
        )
    if not complete:
        _invalid("no quality-compounder member has complete market history")
    if len(exclusions) / len(panel.members) > _MAXIMUM_STRUCTURAL_EXCLUSION_FRACTION:
        _invalid("structural breadth exclusion fraction exceeds the frozen one-percent bound")
    return complete, tuple(sorted(exclusions, key=lambda item: item.security.security_id))


def _historical_sic(
    row: EquityObservation,
    values: Mapping[str, EquityObservationValue],
) -> int:
    value = values.get("historical_sic")
    if (
        value is None
        or value.value_type != "integer"
        or value.integer_value is None
        or row.sic_code is None
        or str(int(value.integer_value)) != str(row.sic_code)
    ):
        _invalid("persisted SEC fact lacks exact historical SIC evidence")
    return int(value.integer_value)


def _market_cap(
    symbol: str,
    row: EquityObservation,
    values: Mapping[str, EquityObservationValue],
) -> MarketCapitalizationEvidence | None:
    value = values.get("market_cap")
    if (
        set(values) != {"market_cap"}
        or value is None
        or value.value_type != "decimal"
        or value.decimal_value is None
        or value.unit != "USD"
        or value.decimal_value <= 0
    ):
        return None
    return MarketCapitalizationEvidence(
        symbol=symbol,
        value=Decimal(value.decimal_value),
        observed_at=_utc(row.event_at),
        available_at=_required_available(row),
        source_observation_id=str(row.observation_id),
    )


def _latest_market_cap(
    symbol: str,
    candidates: Sequence[MarketCapitalizationEvidence],
) -> MarketCapitalizationEvidence | None:
    if not candidates:
        return None
    latest_at = max(item.available_at for item in candidates)
    latest = tuple(item for item in candidates if item.available_at == latest_at)
    if len({(item.value, item.source_observation_id) for item in latest}) != 1:
        _invalid(f"{symbol} latest market-cap evidence is ambiguous")
    return latest[0]


def _semantics(lineage: EquitySourceLineage) -> Mapping[str, object]:
    value = lineage.timestamp_semantics
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        _invalid("SEC timestamp semantics must be an object with string keys")
    return value


def _text_value(values: Mapping[str, EquityObservationValue], field_name: str) -> str:
    value = values.get(field_name)
    if value is None or value.value_type != "text" or value.text_value is None:
        _invalid(f"factor source {field_name} must be text")
    result = str(value.text_value)
    if not result or result != result.strip():
        _invalid(f"factor source {field_name} must be canonical text")
    return result


def _boolean_value(values: Mapping[str, EquityObservationValue], field_name: str) -> bool:
    value = values.get(field_name)
    if value is None or value.value_type != "boolean" or value.boolean_value is None:
        _invalid(f"factor source {field_name} must be boolean")
    return bool(value.boolean_value)


def _decimal_value(
    values: Mapping[str, EquityObservationValue],
    field_name: str,
) -> Decimal:
    value = values.get(field_name)
    if value is None or value.value_type != "decimal" or value.decimal_value is None:
        _invalid(f"factor source {field_name} must be decimal")
    return Decimal(value.decimal_value)


def _date_value(values: Mapping[str, EquityObservationValue], field_name: str) -> date:
    value = values.get(field_name)
    if value is None or value.value_type != "date" or value.date_value is None:
        _invalid(f"factor source {field_name} must be a date")
    return value.date_value


def _digest_value(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _DIGEST_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _invalid(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _cik_from_source_identity(value: str) -> str:
    cik, separator, _rest = value.partition(":")
    if separator != ":" or len(cik) != _CIK_LENGTH or not cik.isdecimal():
        _invalid("issuer-scoped SEC fact lacks a canonical CIK identity")
    return cik


def _required_available(observation: EquityObservation) -> datetime:
    if observation.available_at is None:
        _invalid("selected factor evidence lacks available_at")
    return _utc(observation.available_at)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _stored(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)
