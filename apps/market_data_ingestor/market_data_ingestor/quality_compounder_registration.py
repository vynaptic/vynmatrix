"""Atomic registration of one calculated US Quality Compounder panel."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import NoReturn

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from USQualityCompounder.panel import panel_input_to_payload

from lib_application.db.models import (
    EquityFactorEvidence,
    EquityFactorSnapshot,
    EquityObservation,
    EquityObservationValue,
    EquitySecurityIdentity,
    IndexMembership,
    MarketCalendar,
    MarketSession,
    StrategyPanelInputRevision,
    StrategyVersion,
)
from lib_application.services.equity_lineage import (
    equity_observation_semantic_sha256,
    validate_equity_observation_authority,
)
from lib_application.services.strategy_panel_inputs import (
    effective_membership_sha256,
    factor_panel_sha256,
    persist_strategy_panel_input_revision,
)
from lib_application.services.strategy_panel_sessions import (
    market_session_content_sha256,
    market_session_source_identity,
)
from lib_strategy.data_authority import DataUseScope, ProviderAuthorityPolicy
from lib_strategy.equity_market_factors import EquityMarketFactorPolicy, EquityMarketFactorSnapshot
from lib_strategy.equity_quality_compounder import QUALITY_COMPOUNDER_STRATEGY_VERSION
from lib_strategy.panels import (
    EffectivePanelMember,
    OfficialSessionCutoff,
    PanelExclusion,
    PanelObservationRef,
    PanelReadyInput,
    SessionAuthority,
)

from .equity_factors import FundamentalPanelSnapshot, MarketCapitalizationEvidence
from .quality_compounder_panel import (
    DatabaseQualityCompounderPanelResolver,
    QualityCompounderPanelPayloadValidator,
    build_quality_compounder_panel_input,
    persist_quality_compounder_panel_manifest,
)
from .quality_compounder_quarterly import QualityCompounderQuarterlyWindow

_STRATEGY_ID = "us_quality_compounder_v1"
_STRATEGY_VERSION = QUALITY_COMPOUNDER_STRATEGY_VERSION
_UNIVERSE = "SP500"
_DIGEST_PLACEHOLDER = "0" * 64
_MINIMUM_OVERALL_COVERAGE_PERCENT = 80
_MINIMUM_MATERIAL_SECTOR_COVERAGE_PERCENT = 70
_MATERIAL_SECTOR_MINIMUM_MEMBERS = 10


class QualityCompounderRegistrationError(RuntimeError):
    """Calculated evidence cannot become one durable strategy revision."""


def _invalid(message: str) -> NoReturn:
    raise QualityCompounderRegistrationError(message)


def persist_quality_compounder_panel_revision(
    session: Session,
    *,
    window: QualityCompounderQuarterlyWindow,
    cutoff: datetime,
    now: datetime,
    market: EquityMarketFactorSnapshot,
    market_policy: EquityMarketFactorPolicy,
    fundamentals: FundamentalPanelSnapshot,
    market_cap_by_symbol: Mapping[str, MarketCapitalizationEvidence],
    provider_authority_policy: ProviderAuthorityPolicy,
    entitlement_owner_user_id: str,
) -> StrategyPanelInputRevision:
    """Persist factor-panel dispositions, manifest, and validated input atomically.

    Factor snapshots and all source observations must already be staged in the
    caller-owned transaction. No network operation is performed here.
    """

    cutoff_at = _aware(cutoff, field_name="panel cutoff")
    current = _aware(now, field_name="registration clock")
    owner = str(entitlement_owner_user_id).strip()
    if not owner or owner != entitlement_owner_user_id:
        _invalid("registration requires one canonical entitlement owner")
    if (
        provider_authority_policy.data_use_scope is not DataUseScope.PAPER_FORWARD
        or provider_authority_policy.effective_entitlement_owner_user_id != owner
    ):
        _invalid("registration authority must bind the exact paper entitlement owner")
    if current < cutoff_at:
        _invalid("registration clock cannot precede the panel cutoff")
    if cutoff_at >= window.execution_opens_at or current >= window.execution_opens_at:
        _invalid("registration must complete before the execution-session open")
    if (
        not isinstance(market_policy, EquityMarketFactorPolicy)
        or market.policy_sha256 != market_policy.configuration_sha256
    ):
        _invalid("market snapshot and registered market policy identities differ")
    version = _strategy_version(session)
    decision, execution, calendar_observation_id = _session_cutoffs(
        session,
        window=window,
        cutoff=cutoff_at,
        authority=provider_authority_policy,
    )
    (
        members,
        memberships,
        identities,
        membership_digests,
        identity_digests,
    ) = _effective_membership(
        session,
        decision=decision,
        cutoff=cutoff_at,
        authority=provider_authority_policy,
    )
    snapshots = _factor_snapshots(
        session,
        version=version,
        members=members,
        decision=decision,
        cutoff=cutoff_at,
    )
    require_quality_compounder_factor_coverage(
        sector_by_instrument=_member_sectors(session, identities=identities),
        snapshots=snapshots,
    )
    observations, exclusions, complete_snapshot_ids = _panel_dispositions(
        session,
        members=members,
        snapshots=snapshots,
        decision=decision,
        cutoff=cutoff_at,
        authority=provider_authority_policy,
    )
    if not observations:
        _invalid("registration requires at least one factor-complete member")

    provisional = PanelReadyInput(
        cutoff=cutoff_at,
        session=decision,
        execution_session=execution,
        data_use_scope=DataUseScope.PAPER_FORWARD,
        provider_authority_policy=provider_authority_policy,
        provider_authority_sha256=provider_authority_policy.digest,
        membership_sha256=_DIGEST_PLACEHOLDER,
        factor_snapshot_sha256=factor_panel_sha256(list(snapshots.values())),
        members=tuple(sorted(members.values(), key=lambda item: item.security_id)),
        observations=observations,
        exclusions=exclusions,
    )
    panel = replace(
        provisional,
        membership_sha256=effective_membership_sha256(
            universe_code=_UNIVERSE,
            panel=provisional,
            membership_rows=memberships,
            observation_sha256_by_instrument=membership_digests,
            security_identity_rows=identities,
            security_identity_observation_sha256_by_instrument=identity_digests,
        ),
    )
    strategy_input = build_quality_compounder_panel_input(
        panel=panel,
        market=market,
        fundamentals=fundamentals,
        market_cap_by_symbol=market_cap_by_symbol,
        factor_snapshot_id_by_security=complete_snapshot_ids,
    )
    source_ids = _manifest_sources(
        session,
        snapshots=snapshots,
        memberships=memberships,
        identities=identities,
        calendar_observation_id=calendar_observation_id,
        market=market,
        fundamentals=fundamentals,
        market_caps=market_cap_by_symbol,
    )
    persist_quality_compounder_panel_manifest(
        session,
        panel_input=strategy_input,
        strategy_version=_STRATEGY_VERSION,
        entitlement_owner_user_id=owner,
        market_policy=market_policy,
        market_snapshot_sha256=market.content_sha256,
        fundamental_snapshot_sha256=fundamentals.content_digest,
        source_observation_ids=source_ids,
    )
    return persist_strategy_panel_input_revision(
        session,
        strategy_id=_STRATEGY_ID,
        strategy_version=_STRATEGY_VERSION,
        universe_code=_UNIVERSE,
        panel=panel,
        strategy_input_payload=panel_input_to_payload(strategy_input),
        now=current,
        strategy_payload_validator=QualityCompounderPanelPayloadValidator(
            resolver_factory=DatabaseQualityCompounderPanelResolver
        ),
    )


def build_quality_compounder_materialization_panel(
    session: Session,
    *,
    window: QualityCompounderQuarterlyWindow,
    cutoff: datetime,
    provider_authority_policy: ProviderAuthorityPolicy,
) -> PanelReadyInput:
    """Build the exact source panel used before factor snapshots exist.

    The returned factor digest honestly identifies an empty pre-materialization
    ledger. It is transaction-local input to the factor resolver and is never
    persisted as a completed strategy panel revision.
    """

    cutoff_at = _aware(cutoff, field_name="materialization cutoff")
    if cutoff_at >= window.execution_opens_at:
        _invalid("materialization cutoff must precede the execution-session open")
    decision, execution, _calendar_observation_id = _session_cutoffs(
        session,
        window=window,
        cutoff=cutoff_at,
        authority=provider_authority_policy,
    )
    members, memberships, identities, membership_digests, identity_digests = _effective_membership(
        session,
        decision=decision,
        cutoff=cutoff_at,
        authority=provider_authority_policy,
    )
    provisional = PanelReadyInput(
        cutoff=cutoff_at,
        session=decision,
        execution_session=execution,
        data_use_scope=DataUseScope.PAPER_FORWARD,
        provider_authority_policy=provider_authority_policy,
        provider_authority_sha256=provider_authority_policy.digest,
        membership_sha256=_DIGEST_PLACEHOLDER,
        factor_snapshot_sha256=factor_panel_sha256([]),
        members=tuple(sorted(members.values(), key=lambda item: item.security_id)),
        observations=(),
        exclusions=(),
    )
    return replace(
        provisional,
        membership_sha256=effective_membership_sha256(
            universe_code=_UNIVERSE,
            panel=provisional,
            membership_rows=memberships,
            observation_sha256_by_instrument=membership_digests,
            security_identity_rows=identities,
            security_identity_observation_sha256_by_instrument=identity_digests,
        ),
    )


def _strategy_version(session: Session) -> StrategyVersion:
    version = session.scalar(
        select(StrategyVersion)
        .where(
            StrategyVersion.strategy_id == _STRATEGY_ID,
            StrategyVersion.semver == _STRATEGY_VERSION,
        )
        .with_for_update()
    )
    if version is None or str(version.status) != "active":
        _invalid("registered quality-compounder version is unavailable or inactive")
    return version


def _session_cutoffs(
    session: Session,
    *,
    window: QualityCompounderQuarterlyWindow,
    cutoff: datetime,
    authority: ProviderAuthorityPolicy,
) -> tuple[OfficialSessionCutoff, OfficialSessionCutoff, str]:
    calendar = session.get(MarketCalendar, window.calendar_id)
    decision = session.get(MarketSession, window.decision_session_id)
    execution = session.get(MarketSession, window.execution_session_id)
    if (
        calendar is None
        or decision is None
        or execution is None
        or int(decision.calendar_id) != window.calendar_id
        or int(execution.calendar_id) != window.calendar_id
        or _utc(decision.opens_at) != window.decision_opens_at
        or _utc(decision.closes_at) != window.decision_closes_at
        or _utc(execution.opens_at) != window.execution_opens_at
        or _utc(execution.closes_at) != window.execution_closes_at
    ):
        _invalid("quarterly window differs from persisted official sessions")
    observation, lineage = validate_equity_observation_authority(
        session,
        observation_id=calendar.observation_id,
        expected_kind="calendar",
        cutoff=cutoff,
        provider_authority_policy=authority,
        expected_instrument_id=None,
    )
    if (
        str(calendar.source_kind) != "exchange"
        or str(lineage.provider) != str(calendar.provider)
        or str(observation.source_record_identity) != str(calendar.source_reference)
    ):
        _invalid("calendar is not exact official-exchange authority")

    def build(source: MarketSession) -> OfficialSessionCutoff:
        return OfficialSessionCutoff(
            mic=str(calendar.code),
            session_date=_utc(source.opens_at).date(),
            opens_at=_utc(source.opens_at),
            closes_at=_utc(source.closes_at),
            authority=SessionAuthority.OFFICIAL_EXCHANGE,
            source_identity=market_session_source_identity(calendar),
            content_sha256=market_session_content_sha256(calendar, source, lineage),
        )

    return build(decision), build(execution), str(observation.observation_id)


def _effective_membership(
    session: Session,
    *,
    decision: OfficialSessionCutoff,
    cutoff: datetime,
    authority: ProviderAuthorityPolicy,
) -> tuple[
    dict[int, EffectivePanelMember],
    dict[int, IndexMembership],
    dict[int, EquitySecurityIdentity],
    dict[int, str],
    dict[int, str],
]:
    rows = tuple(
        session.scalars(
            select(IndexMembership).where(
                IndexMembership.index_code == _UNIVERSE,
                IndexMembership.effective_from <= decision.session_date,
                or_(
                    IndexMembership.effective_to.is_(None),
                    IndexMembership.effective_to >= decision.session_date,
                ),
            )
        )
    )
    memberships = {int(item.instr_id): item for item in rows}
    if not rows or len(memberships) != len(rows):
        _invalid("effective S&P 500 membership is empty or overlapping")
    identity_rows = tuple(
        session.scalars(
            select(EquitySecurityIdentity).where(
                EquitySecurityIdentity.instr_id.in_(tuple(memberships)),
                EquitySecurityIdentity.effective_from <= decision.session_date,
                or_(
                    EquitySecurityIdentity.effective_to.is_(None),
                    EquitySecurityIdentity.effective_to >= decision.session_date,
                ),
            )
        )
    )
    identities = {int(item.instr_id): item for item in identity_rows}
    if len(identities) != len(identity_rows) or set(identities) != set(memberships):
        _invalid("effective membership lacks one exact security identity per member")

    members: dict[int, EffectivePanelMember] = {}
    membership_digests: dict[int, str] = {}
    identity_digests: dict[int, str] = {}
    for instrument_id in sorted(memberships):
        membership = memberships[instrument_id]
        identity = identities[instrument_id]
        membership_observation, membership_lineage = validate_equity_observation_authority(
            session,
            observation_id=membership.observation_id,
            expected_kind="membership",
            cutoff=cutoff,
            provider_authority_policy=authority,
            expected_instrument_id=instrument_id,
        )
        identity_observation, identity_lineage = validate_equity_observation_authority(
            session,
            observation_id=identity.observation_id,
            expected_kind="security_identity",
            cutoff=cutoff,
            provider_authority_policy=authority,
            expected_instrument_id=instrument_id,
        )
        if str(membership_observation.source_record_identity) != str(membership.source_ref) or str(
            identity_observation.source_record_identity
        ) != str(identity.source_ref):
            _invalid("membership identity differs from immutable source evidence")
        members[instrument_id] = EffectivePanelMember(
            security_id=str(identity.security_id),
            issuer_id=str(identity.issuer_id),
            instrument_id=instrument_id,
            canonical_symbol=str(identity.canonical_symbol),
        )
        membership_digests[instrument_id] = equity_observation_semantic_sha256(
            membership_observation,
            membership_lineage,
        )
        identity_digests[instrument_id] = equity_observation_semantic_sha256(
            identity_observation,
            identity_lineage,
        )
    return members, memberships, identities, membership_digests, identity_digests


def _factor_snapshots(
    session: Session,
    *,
    version: StrategyVersion,
    members: Mapping[int, EffectivePanelMember],
    decision: OfficialSessionCutoff,
    cutoff: datetime,
) -> dict[int, EquityFactorSnapshot]:
    rows = tuple(
        session.scalars(
            select(EquityFactorSnapshot).where(
                EquityFactorSnapshot.strategy_id == _STRATEGY_ID,
                EquityFactorSnapshot.strat_ver_id == int(version.strat_ver_id),
                EquityFactorSnapshot.effective_session == decision.session_date,
                EquityFactorSnapshot.cutoff_at == _stored(cutoff),
                EquityFactorSnapshot.instr_id.in_(tuple(members)),
            )
        )
    )
    snapshots = {int(item.instr_id): item for item in rows}
    if len(snapshots) != len(rows) or set(snapshots) != set(members):
        _invalid("factor snapshots do not disposition every effective member")
    return snapshots


def _member_sectors(
    session: Session,
    *,
    identities: Mapping[int, EquitySecurityIdentity],
) -> dict[int, str]:
    observation_ids = tuple(str(item.observation_id) for item in identities.values())
    rows = tuple(
        session.scalars(
            select(EquityObservationValue).where(
                EquityObservationValue.observation_id.in_(observation_ids),
                EquityObservationValue.field_name == "sector",
                EquityObservationValue.ordinal == 0,
            )
        )
    )
    by_observation: dict[str, str] = {}
    for row in rows:
        observation_id = str(row.observation_id)
        sector = str(row.text_value or "")
        if (
            observation_id in by_observation
            or str(row.value_type) != "text"
            or not sector
            or sector != sector.strip()
        ):
            _invalid("effective member sector evidence is missing or ambiguous")
        by_observation[observation_id] = sector
    sectors = {
        instrument_id: by_observation.get(str(identity.observation_id), "")
        for instrument_id, identity in identities.items()
    }
    if not sectors or any(not sector for sector in sectors.values()):
        _invalid("effective membership lacks one exact sector per member")
    return dict(sorted(sectors.items()))


def require_quality_compounder_factor_coverage(
    *,
    sector_by_instrument: Mapping[int, str],
    snapshots: Mapping[int, EquityFactorSnapshot],
) -> None:
    """Enforce the pre-registered forward factor-completeness floors."""

    if not snapshots or set(sector_by_instrument) != set(snapshots):
        _invalid("factor coverage requires one sector and snapshot per effective member")
    sector_counts: dict[str, list[int]] = {}
    complete_count = 0
    for instrument_id in sorted(snapshots):
        sector = str(sector_by_instrument[instrument_id])
        if not sector or sector != sector.strip():
            _invalid("factor coverage sector values must be canonical non-blank text")
        status = str(snapshots[instrument_id].completeness_status)
        if status not in {"complete", "incomplete", "ineligible"}:
            _invalid("factor coverage received an unsupported completeness status")
        counts = sector_counts.setdefault(sector, [0, 0])
        counts[1] += 1
        if status == "complete":
            complete_count += 1
            counts[0] += 1

    total_count = len(snapshots)
    overall_passes = complete_count * 100 >= total_count * _MINIMUM_OVERALL_COVERAGE_PERCENT
    required_sector_percent = _MINIMUM_MATERIAL_SECTOR_COVERAGE_PERCENT
    material_failures = tuple(
        (sector, counts[0], counts[1])
        for sector, counts in sorted(sector_counts.items())
        if counts[1] >= _MATERIAL_SECTOR_MINIMUM_MEMBERS
        and counts[0] * 100 < counts[1] * required_sector_percent
    )
    if overall_passes and not material_failures:
        return

    overall = _coverage_text(
        complete_count,
        total_count,
        required_percent=_MINIMUM_OVERALL_COVERAGE_PERCENT,
    )
    sector_diagnostics = ", ".join(
        f"{sector}={_coverage_text(complete, total, required_percent=required_sector_percent)}"
        for sector, complete, total in material_failures
    )
    _invalid(
        "factor-complete coverage gate failed: "
        f"overall={overall}; material_sector_failures={sector_diagnostics or 'none'}"
    )


def _coverage_text(complete: int, total: int, *, required_percent: int) -> str:
    observed = complete * 100 / total
    return f"{complete}/{total} ({observed:.2f}%, required>={required_percent}.00%)"


def _panel_dispositions(
    session: Session,
    *,
    members: Mapping[int, EffectivePanelMember],
    snapshots: Mapping[int, EquityFactorSnapshot],
    decision: OfficialSessionCutoff,
    cutoff: datetime,
    authority: ProviderAuthorityPolicy,
) -> tuple[
    tuple[PanelObservationRef, ...],
    tuple[PanelExclusion, ...],
    dict[str, str],
]:
    complete = {
        instrument_id: snapshot
        for instrument_id, snapshot in snapshots.items()
        if str(snapshot.completeness_status) == "complete"
    }
    rows = session.execute(
        select(EquityFactorEvidence, EquityObservation)
        .join(
            EquityObservation,
            EquityFactorEvidence.observation_id == EquityObservation.observation_id,
        )
        .where(
            EquityFactorEvidence.factor_snapshot_id.in_(
                tuple(str(item.factor_snapshot_id) for item in complete.values())
            ),
            EquityFactorEvidence.factor_name == "momentum",
            EquityObservation.observation_kind == "price",
            EquityObservation.event_at == _stored(decision.closes_at),
        )
    ).all()
    latest: dict[str, EquityObservation] = {}
    for evidence, observation in rows:
        snapshot_id = str(evidence.factor_snapshot_id)
        if snapshot_id in latest:
            _invalid("complete factor snapshot cites ambiguous decision-close prices")
        latest[snapshot_id] = observation

    observations: list[PanelObservationRef] = []
    exclusions: list[PanelExclusion] = []
    complete_ids: dict[str, str] = {}
    for instrument_id in sorted(members):
        member = members[instrument_id]
        snapshot = snapshots[instrument_id]
        snapshot_id = str(snapshot.factor_snapshot_id)
        if str(snapshot.completeness_status) != "complete":
            exclusions.append(
                PanelExclusion(
                    security_id=member.security_id,
                    reason_code=f"factor_snapshot_{snapshot.completeness_status}",
                    disposition_identity=snapshot_id,
                    content_sha256=str(snapshot.content_sha256),
                )
            )
            continue
        source = latest.get(snapshot_id)
        if source is None:
            _invalid("complete factor snapshot lacks its decision-close price")
        validated, _lineage = validate_equity_observation_authority(
            session,
            observation_id=str(source.observation_id),
            expected_kind="price",
            cutoff=cutoff,
            provider_authority_policy=authority,
            expected_instrument_id=instrument_id,
        )
        if _utc(validated.event_at) != decision.closes_at or validated.available_at is None:
            _invalid("complete factor snapshot price differs from the official close")
        observations.append(
            PanelObservationRef(
                security_id=member.security_id,
                observation_id=str(validated.observation_id),
                observed_at=_utc(validated.event_at),
                available_at=_utc(validated.available_at),
                content_revision=int(validated.revision),
                content_sha256=str(validated.content_sha256),
            )
        )
        complete_ids[member.security_id] = snapshot_id
    return (
        tuple(sorted(observations, key=lambda item: item.security_id)),
        tuple(sorted(exclusions, key=lambda item: item.security_id)),
        dict(sorted(complete_ids.items())),
    )


def _manifest_sources(
    session: Session,
    *,
    snapshots: Mapping[int, EquityFactorSnapshot],
    memberships: Mapping[int, IndexMembership],
    identities: Mapping[int, EquitySecurityIdentity],
    calendar_observation_id: str,
    market: EquityMarketFactorSnapshot,
    fundamentals: FundamentalPanelSnapshot,
    market_caps: Mapping[str, MarketCapitalizationEvidence],
) -> tuple[str, ...]:
    snapshot_ids = tuple(str(item.factor_snapshot_id) for item in snapshots.values())
    factor_source_ids = tuple(
        session.scalars(
            select(EquityFactorEvidence.observation_id).where(
                EquityFactorEvidence.factor_snapshot_id.in_(snapshot_ids)
            )
        )
    )
    source_ids = {
        calendar_observation_id,
        *(str(item.observation_id) for item in memberships.values()),
        *(str(item.observation_id) for item in identities.values()),
        *(str(item) for item in factor_source_ids),
        *market.regime.source_observation_ids,
        *(source for item in market.instruments for source in item.source_observation_ids),
        *(
            source
            for observation in fundamentals.sleeve_observations
            for source in observation.source_observation_ids
        ),
        *(item.source_observation_id for item in market_caps.values()),
    }
    if not source_ids:
        _invalid("derived panel source ledger is empty")
    return tuple(sorted(source_ids))


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _stored(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


__all__ = [
    "QualityCompounderRegistrationError",
    "build_quality_compounder_materialization_panel",
    "persist_quality_compounder_panel_revision",
    "require_quality_compounder_factor_coverage",
]
