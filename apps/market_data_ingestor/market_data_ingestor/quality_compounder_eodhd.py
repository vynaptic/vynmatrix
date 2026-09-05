"""Network-only EODHD acquisition for the prospective quality-compounder universe."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import NoReturn, Protocol

from lib_infrastructure.market_data.eodhd_client import EODHDJsonEvidence

from .quality_compounder_universe import (
    QualityCompounderIdentityEvidence,
    QualityCompounderSecurityIdentity,
    QualityCompounderUniverseComponent,
    parse_quality_compounder_benchmark_identity,
    parse_quality_compounder_components,
    parse_quality_compounder_security_identity,
)

_INDEX_SYMBOL = "GSPC.INDX"
_MEMBERSHIP_LOOKBACK_DAYS = 2 * 366


class QualityCompounderEODHDAcquisitionError(RuntimeError):
    """EODHD acquisition crossed its window or returned incomplete evidence."""


def _invalid(message: str) -> NoReturn:
    raise QualityCompounderEODHDAcquisitionError(message)


class QualityCompounderEODHDClient(Protocol):
    """Existing client methods used by the isolated quarterly acquisition."""

    def fetch_current_index_components_evidence(
        self,
        *,
        index_symbol: str,
    ) -> EODHDJsonEvidence: ...

    def fetch_index_membership_history_evidence(
        self,
        *,
        index_symbol: str,
        start: date,
        end: date,
    ) -> tuple[EODHDJsonEvidence, EODHDJsonEvidence]: ...

    def fetch_id_mapping_evidence(self, *, provider_symbol: str) -> EODHDJsonEvidence: ...

    def fetch_security_general_evidence(
        self,
        *,
        provider_symbol: str,
    ) -> EODHDJsonEvidence: ...


@dataclass(frozen=True, slots=True)
class AcquiredQualityCompounderMembership:
    """Three exact membership artifacts acquired after one completed close."""

    decision_session: date
    decision_close: datetime
    current_evidence: EODHDJsonEvidence
    historical_evidence: EODHDJsonEvidence
    ticker_history_evidence: EODHDJsonEvidence
    components: tuple[QualityCompounderUniverseComponent, ...]


@dataclass(frozen=True, slots=True)
class AcquiredQualityCompounderUniverse:
    """Complete prospective membership and per-share-class identity graph."""

    membership: AcquiredQualityCompounderMembership
    identities: Mapping[str, QualityCompounderSecurityIdentity]
    acquired_at: datetime


def acquire_quality_compounder_benchmark_identity(
    *,
    client: QualityCompounderEODHDClient,
    decision_close: datetime,
    complete_before: datetime,
) -> QualityCompounderSecurityIdentity:
    """Acquire the exact SPY mapping and General evidence without database work."""

    close = _utc(decision_close, field_name="decision_close")
    deadline = _utc(complete_before, field_name="complete_before")
    mapping = client.fetch_id_mapping_evidence(provider_symbol="SPY")
    general = client.fetch_security_general_evidence(provider_symbol="SPY")
    _require_window((mapping, general), close=close, deadline=deadline)
    return parse_quality_compounder_benchmark_identity(
        evidence=QualityCompounderIdentityEvidence(mapping=mapping, general=general)
    )


def acquire_quality_compounder_membership(
    *,
    client: QualityCompounderEODHDClient,
    decision_session: date,
    decision_close: datetime,
    complete_before: datetime,
) -> AcquiredQualityCompounderMembership:
    """Acquire and reconcile the three provider membership views."""

    close = _utc(decision_close, field_name="decision_close")
    deadline = _utc(complete_before, field_name="complete_before")
    if close.date() != decision_session or deadline <= close:
        _invalid("membership acquisition window is inconsistent")
    current = client.fetch_current_index_components_evidence(index_symbol=_INDEX_SYMBOL)
    ticker_history, historical = client.fetch_index_membership_history_evidence(
        index_symbol=_INDEX_SYMBOL,
        start=decision_session - timedelta(days=_MEMBERSHIP_LOOKBACK_DAYS),
        end=decision_session,
    )
    _require_window((current, historical, ticker_history), close=close, deadline=deadline)
    components = parse_quality_compounder_components(
        current=current,
        historical=historical,
        ticker_history=ticker_history,
        decision_session=decision_session,
    )
    return AcquiredQualityCompounderMembership(
        decision_session=decision_session,
        decision_close=close,
        current_evidence=current,
        historical_evidence=historical,
        ticker_history_evidence=ticker_history,
        components=components,
    )


def acquire_quality_compounder_identities(
    *,
    client: QualityCompounderEODHDClient,
    membership: AcquiredQualityCompounderMembership,
    complete_before: datetime,
) -> AcquiredQualityCompounderUniverse:
    """Acquire exact ID mapping and General artifacts for every qualified member."""

    deadline = _utc(complete_before, field_name="complete_before")
    identities: dict[str, QualityCompounderSecurityIdentity] = {}
    acquired_at = max(
        membership.current_evidence.retrieved_at,
        membership.historical_evidence.retrieved_at,
        membership.ticker_history_evidence.retrieved_at,
    )
    for component in membership.components:
        mapping = client.fetch_id_mapping_evidence(provider_symbol=component.symbol)
        general = client.fetch_security_general_evidence(provider_symbol=component.symbol)
        _require_window(
            (mapping, general),
            close=membership.decision_close,
            deadline=deadline,
        )
        evidence = QualityCompounderIdentityEvidence(mapping=mapping, general=general)
        identities[component.symbol] = parse_quality_compounder_security_identity(
            component,
            evidence=evidence,
        )
        acquired_at = max(acquired_at, mapping.retrieved_at, general.retrieved_at)
    if set(identities) != {item.symbol for item in membership.components}:
        _invalid("identity acquisition does not exactly cover membership")
    return AcquiredQualityCompounderUniverse(
        membership=membership,
        identities=dict(sorted(identities.items())),
        acquired_at=acquired_at,
    )


def _require_window(
    evidence: tuple[EODHDJsonEvidence, ...],
    *,
    close: datetime,
    deadline: datetime,
) -> None:
    if not evidence or any(not close <= item.retrieved_at < deadline for item in evidence):
        _invalid("EODHD evidence was not retrieved inside the quarter-end window")


def _utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "AcquiredQualityCompounderMembership",
    "AcquiredQualityCompounderUniverse",
    "QualityCompounderEODHDAcquisitionError",
    "QualityCompounderEODHDClient",
    "acquire_quality_compounder_benchmark_identity",
    "acquire_quality_compounder_identities",
    "acquire_quality_compounder_membership",
]
