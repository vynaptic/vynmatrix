"""Provider-neutral contracts for revision-pinned cross-sectional panels."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from lib_common.hashing import canonical_json_hash

from .cross_sectional import CrossSectionalSnapshot
from .data_authority import DataUseScope, ProviderAuthorityPolicy

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FACTOR_VERSION_PAIR_LENGTH = 2


class PanelContractError(ValueError):
    """A panel input cannot prove its identity or point-in-time semantics."""


class PanelNotReadyError(PanelContractError):
    """A synchronized panel is incomplete or not yet actionable."""


class SessionAuthority(StrEnum):
    """Authorities accepted for a production market-session cutoff."""

    OFFICIAL_EXCHANGE = "official_exchange"
    AUTHENTICATED_BROKER = "authenticated_broker"
    RESEARCH_LIBRARY = "research_library"


class PanelIssueCode(StrEnum):
    """Stable readiness issue codes persisted by panel runtimes."""

    CUTOFF_MISMATCH = "cutoff_mismatch"
    DUPLICATE_EXCLUSION = "duplicate_exclusion"
    DUPLICATE_INSTRUMENT = "duplicate_instrument"
    DUPLICATE_MEMBER = "duplicate_member"
    DUPLICATE_OBSERVATION = "duplicate_observation"
    DUPLICATE_SYMBOL = "duplicate_symbol"
    EXTRA_EXCLUSION = "extra_exclusion"
    EXTRA_OBSERVATION = "extra_observation"
    LATE_OBSERVATION = "late_observation"
    MEMBER_BOTH_OBSERVED_AND_EXCLUDED = "member_both_observed_and_excluded"
    MISSING_MEMBER = "missing_member"


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        message = f"{field_name} must be a string"
        raise PanelContractError(message)
    normalized = value.strip()
    if not normalized:
        message = f"{field_name} must be non-empty"
        raise PanelContractError(message)
    return normalized


def _require_sha256(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        message = f"{field_name} must be a string"
        raise PanelContractError(message)
    normalized = value.strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        message = f"{field_name} must be a lowercase SHA-256 digest"
        raise PanelContractError(message)
    return normalized


def _require_aware(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        message = f"{field_name} must be a datetime"
        raise PanelContractError(message)
    if value.tzinfo is None or value.utcoffset() is None:
        message = f"{field_name} must be timezone-aware"
        raise PanelContractError(message)
    return value


@dataclass(frozen=True, slots=True)
class OfficialSessionCutoff:
    """One persisted official exchange or authenticated-broker session.

    ``closes_at`` is the completed market-event timestamp. It is deliberately
    distinct from a panel's later knowledge/actionability cutoff.
    """

    mic: str
    session_date: date
    opens_at: datetime
    closes_at: datetime
    authority: SessionAuthority
    source_identity: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "mic", _require_text(self.mic, field_name="mic").upper())
        object.__setattr__(
            self,
            "source_identity",
            _require_text(self.source_identity, field_name="source_identity"),
        )
        object.__setattr__(
            self,
            "content_sha256",
            _require_sha256(self.content_sha256, field_name="content_sha256"),
        )
        opens_at = _require_aware(self.opens_at, field_name="opens_at")
        closes_at = _require_aware(self.closes_at, field_name="closes_at")
        if not isinstance(self.authority, SessionAuthority):
            message = "authority must be an accepted SessionAuthority"
            raise PanelContractError(message)
        if (
            self.authority is SessionAuthority.RESEARCH_LIBRARY
            and not self.source_identity.startswith("research-calendar:")
        ):
            message = "research-library sessions require explicit research-calendar identity"
            raise PanelContractError(message)
        if closes_at <= opens_at:
            message = "closes_at must be later than opens_at"
            raise PanelContractError(message)


@dataclass(frozen=True, slots=True)
class EffectivePanelMember:
    """One permanent security identity effective in the cutoff universe."""

    security_id: str
    issuer_id: str
    instrument_id: int
    canonical_symbol: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "security_id",
            _require_text(self.security_id, field_name="security_id"),
        )
        object.__setattr__(
            self,
            "issuer_id",
            _require_text(self.issuer_id, field_name="issuer_id"),
        )
        _positive_int(self.instrument_id, field_name="instrument_id")
        symbol = _require_text(self.canonical_symbol, field_name="canonical_symbol").upper()
        if symbol != self.canonical_symbol:
            message = "canonical_symbol must already be uppercase and trimmed"
            raise PanelContractError(message)


@dataclass(frozen=True, slots=True)
class PanelObservationRef:
    """Exact immutable observation revision covering one effective member."""

    security_id: str
    observation_id: str
    observed_at: datetime
    available_at: datetime
    content_revision: int
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "security_id",
            _require_text(self.security_id, field_name="security_id"),
        )
        object.__setattr__(
            self,
            "observation_id",
            _require_text(self.observation_id, field_name="observation_id"),
        )
        object.__setattr__(
            self,
            "content_sha256",
            _require_sha256(self.content_sha256, field_name="content_sha256"),
        )
        observed_at = _require_aware(self.observed_at, field_name="observed_at")
        available_at = _require_aware(self.available_at, field_name="available_at")
        if available_at < observed_at:
            message = "available_at cannot precede observed_at"
            raise PanelContractError(message)
        if (
            isinstance(self.content_revision, bool)
            or not isinstance(self.content_revision, int)
            or self.content_revision < 1
        ):
            message = "content_revision must be a positive integer"
            raise PanelContractError(message)


@dataclass(frozen=True, slots=True)
class PanelExclusion:
    """Explicit, lineage-bearing disposition for one effective member."""

    security_id: str
    reason_code: str
    disposition_identity: str
    content_sha256: str

    def __post_init__(self) -> None:
        for field_name in ("security_id", "reason_code", "disposition_identity"):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "content_sha256",
            _require_sha256(self.content_sha256, field_name="content_sha256"),
        )


@dataclass(frozen=True, slots=True)
class PanelReadyInput:
    """Complete identity of one candidate cross-sectional evaluation.

    ``cutoff`` is the latest knowledge admitted to the decision. It may follow
    the completed decision-session close while data is published, but it must
    remain strictly before the next official session opens for execution.
    """

    cutoff: datetime
    session: OfficialSessionCutoff
    execution_session: OfficialSessionCutoff
    data_use_scope: DataUseScope
    provider_authority_policy: ProviderAuthorityPolicy
    provider_authority_sha256: str
    membership_sha256: str
    factor_snapshot_sha256: str
    members: tuple[EffectivePanelMember, ...]
    observations: tuple[PanelObservationRef, ...]
    exclusions: tuple[PanelExclusion, ...] = ()

    def __post_init__(self) -> None:
        cutoff = _require_aware(self.cutoff, field_name="cutoff")
        if not isinstance(self.data_use_scope, DataUseScope):
            message = "data_use_scope must be a DataUseScope"
            raise PanelContractError(message)
        if not isinstance(self.provider_authority_policy, ProviderAuthorityPolicy):
            message = "provider_authority_policy must be a ProviderAuthorityPolicy"
            raise PanelContractError(message)
        if self.provider_authority_policy.data_use_scope is not self.data_use_scope:
            message = "provider authority policy scope must match data_use_scope"
            raise PanelContractError(message)
        object.__setattr__(
            self,
            "provider_authority_sha256",
            _require_sha256(
                self.provider_authority_sha256,
                field_name="provider_authority_sha256",
            ),
        )
        if self.provider_authority_policy.digest != self.provider_authority_sha256:
            message = "provider_authority_sha256 must identify the complete policy"
            raise PanelContractError(message)
        object.__setattr__(
            self,
            "membership_sha256",
            _require_sha256(self.membership_sha256, field_name="membership_sha256"),
        )
        object.__setattr__(
            self,
            "factor_snapshot_sha256",
            _require_sha256(
                self.factor_snapshot_sha256,
                field_name="factor_snapshot_sha256",
            ),
        )
        if self.execution_session.mic != self.session.mic:
            message = "execution session MIC must match the decision session"
            raise PanelContractError(message)
        if self.execution_session.session_date <= self.session.session_date:
            message = "execution session must follow the decision session"
            raise PanelContractError(message)
        if cutoff < self.session.closes_at:
            message = "decision cutoff cannot precede the completed session close"
            raise PanelContractError(message)
        if cutoff >= self.execution_session.opens_at:
            message = "decision cutoff must precede the execution-session open"
            raise PanelContractError(message)
        research_session = self.session.authority is SessionAuthority.RESEARCH_LIBRARY
        research_execution = self.execution_session.authority is SessionAuthority.RESEARCH_LIBRARY
        if research_session != research_execution:
            message = "decision and execution sessions must use the same authority class"
            raise PanelContractError(message)
        if research_session and self.data_use_scope is not DataUseScope.HISTORICAL_VALIDATION:
            message = "research-library sessions are restricted to historical validation"
            raise PanelContractError(message)

    def canonical_digest(self) -> str:
        """Return an ordering-invariant digest for the readiness input."""

        payload = {
            "cutoff": self.cutoff.isoformat(),
            "session_sha256": self.session.content_sha256,
            "execution_session_sha256": self.execution_session.content_sha256,
            "data_use_scope": self.data_use_scope.value,
            "provider_authority_policy": self.provider_authority_policy.to_payload(),
            "provider_authority_sha256": self.provider_authority_sha256,
            "membership_sha256": self.membership_sha256,
            "factor_snapshot_sha256": self.factor_snapshot_sha256,
            "members": sorted(
                (
                    member.security_id,
                    member.issuer_id,
                    member.instrument_id,
                    member.canonical_symbol,
                )
                for member in self.members
            ),
            "observations": sorted(
                (
                    ref.security_id,
                    ref.observation_id,
                    ref.observed_at.isoformat(),
                    ref.available_at.isoformat(),
                    ref.content_revision,
                    ref.content_sha256,
                )
                for ref in self.observations
            ),
            "exclusions": sorted(
                (
                    exclusion.security_id,
                    exclusion.reason_code,
                    exclusion.disposition_identity,
                    exclusion.content_sha256,
                )
                for exclusion in self.exclusions
            ),
        }
        return canonical_json_hash(payload)


@dataclass(frozen=True, slots=True, order=True)
class PanelIssue:
    """One deterministic reason the candidate panel is not ready."""

    code: PanelIssueCode
    security_id: str | None = None


@dataclass(frozen=True, slots=True)
class PanelReadiness:
    """Deterministic completeness result for one candidate panel."""

    panel_sha256: str
    issues: tuple[PanelIssue, ...]
    observed_count: int
    excluded_count: int
    required_count: int

    @property
    def complete(self) -> bool:
        return not self.issues

    def require_complete(self) -> PanelReadiness:
        """Return this result or fail before a strategy can mutate state."""

        if self.issues:
            details = ", ".join(
                f"{issue.code.value}:{issue.security_id or '-'}" for issue in self.issues
            )
            message = f"synchronized panel is not ready: {details}"
            raise PanelNotReadyError(message)
        return self


@runtime_checkable
class SynchronizedPanelStrategy(Protocol):
    """Explicit provider-neutral capability consumed by the indicator runtime."""

    def panel_ready_input(self, panel_input: object) -> PanelReadyInput:
        """Return the generic point-in-time contract for a strategy input."""

    def serialize_panel_input(self, panel_input: object) -> Mapping[str, Any]:
        """Encode the complete strategy input for durable replay."""

    def deserialize_panel_input(self, payload: Mapping[str, Any]) -> object:
        """Restore a strategy input through the strategy's own invariants."""

    def evaluate_panel(self, panel_input: object) -> PanelEvaluationAudit | None:
        """Evaluate one complete synchronized input and mutate model state."""


class PanelAuditDecision(StrEnum):
    """Persisted disposition for one effective-universe member."""

    SELECTED = "selected"
    HOLD = "hold"
    ELIGIBLE = "eligible"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class PanelEvaluationRow:
    """Persistence-neutral audit row for one effective panel member."""

    entity_id: str
    symbol: str
    instrument_id: int
    factor_snapshot_id: str | None
    rank_complete: bool
    strategy_eligible: bool
    decision: PanelAuditDecision
    incumbent: bool
    rank: float | None
    composite_score: float | None
    target_allocation_hint: float | None
    exclusion_reason: str | None

    def __post_init__(self) -> None:  # noqa: PLR0912 - explicit fail-closed row contract
        _require_text(self.entity_id, field_name="entity_id")
        symbol = _require_text(self.symbol, field_name="symbol")
        if symbol != symbol.upper():
            msg = "panel audit symbol must be canonical uppercase"
            raise PanelContractError(msg)
        if (
            isinstance(self.instrument_id, bool)
            or not isinstance(self.instrument_id, int)
            or self.instrument_id < 1
        ):
            msg = "panel audit instrument_id must be positive"
            raise PanelContractError(msg)
        if (
            not isinstance(self.rank_complete, bool)
            or not isinstance(self.strategy_eligible, bool)
            or not isinstance(self.incumbent, bool)
        ):
            msg = "panel audit eligibility flags must be boolean"
            raise PanelContractError(msg)
        if not isinstance(self.decision, PanelAuditDecision):
            msg = "panel audit decision is invalid"
            raise PanelContractError(msg)
        if self.rank_complete:
            if self.factor_snapshot_id is None:
                msg = "eligible panel audit rows require factor_snapshot_id"
                raise PanelContractError(msg)
            _require_sha256(self.factor_snapshot_id, field_name="factor_snapshot_id")
            if self.rank is None or self.composite_score is None:
                msg = "eligible panel audit rows require rank and score"
                raise PanelContractError(msg)
            _finite_number(self.rank, field_name="rank", minimum=1.0)
            _finite_number(self.composite_score, field_name="composite_score")
        elif any(
            value is not None
            for value in (
                self.factor_snapshot_id,
                self.rank,
                self.composite_score,
            )
        ):
            msg = "rank-incomplete audit rows cannot carry rank values"
            raise PanelContractError(msg)
        if self.strategy_eligible:
            if not self.rank_complete:
                msg = "strategy eligibility requires a complete rank"
                raise PanelContractError(msg)
            if self.decision is PanelAuditDecision.EXCLUDED:
                msg = "strategy-eligible rows cannot be excluded"
                raise PanelContractError(msg)
            if self.exclusion_reason is not None:
                msg = "strategy-eligible rows cannot carry exclusion_reason"
                raise PanelContractError(msg)
        else:
            _require_text_value(self.exclusion_reason, field_name="exclusion_reason")
            if self.decision is not PanelAuditDecision.EXCLUDED:
                msg = "strategy-ineligible rows must be excluded"
                raise PanelContractError(msg)
            if self.target_allocation_hint is not None:
                msg = "strategy-ineligible rows cannot carry allocation"
                raise PanelContractError(msg)
        if self.target_allocation_hint is not None:
            _finite_number(
                self.target_allocation_hint,
                field_name="target_allocation_hint",
                minimum=0.0,
                maximum=1.0,
            )


@dataclass(frozen=True, slots=True)
class PanelEvaluationAudit:
    """Complete derived-rank audit returned by any synchronized strategy.

    ``strategy_result`` is deliberately opaque to the shared panel contract;
    downstream adapters may consume a strategy-specific immutable result while
    rank persistence relies only on the normalized fields above.
    """

    rank_snapshot: CrossSectionalSnapshot
    rows: tuple[PanelEvaluationRow, ...]
    configuration_sha256: str
    peer_taxonomy_version: str
    replayed: bool
    intentional_cash_slots: int
    required_factor_versions: tuple[tuple[str, str | None], ...]
    strategy_result: object | None = None

    def __post_init__(self) -> None:  # noqa: PLR0912 - explicit invariant ledger
        if not isinstance(self.rank_snapshot, CrossSectionalSnapshot):
            msg = "rank_snapshot must be a CrossSectionalSnapshot"
            raise PanelContractError(msg)
        if not isinstance(self.rows, tuple) or not self.rows:
            msg = "panel audit rows must be a non-empty immutable tuple"
            raise PanelContractError(msg)
        if not all(isinstance(row, PanelEvaluationRow) for row in self.rows):
            msg = "panel audit contains an invalid row"
            raise PanelContractError(msg)
        canonical = tuple(sorted(self.rows, key=lambda row: (row.symbol, row.entity_id)))
        if self.rows != canonical:
            msg = "panel audit rows must use canonical symbol ordering"
            raise PanelContractError(msg)
        entity_ids = tuple(row.entity_id for row in self.rows)
        instrument_ids = tuple(row.instrument_id for row in self.rows)
        if len(entity_ids) != len(set(entity_ids)):
            msg = "panel audit entity identities must be unique"
            raise PanelContractError(msg)
        if len(instrument_ids) != len(set(instrument_ids)):
            msg = "panel audit instrument identities must be unique"
            raise PanelContractError(msg)
        ranked_entities = {row.entity_id for row in self.rows if row.rank_complete}
        if ranked_entities != {rank.entity_id for rank in self.rank_snapshot.ranks}:
            msg = "panel audit eligible rows must match ranked entities"
            raise PanelContractError(msg)
        _require_sha256(self.configuration_sha256, field_name="configuration_sha256")
        _require_text(self.peer_taxonomy_version, field_name="peer_taxonomy_version")
        if not isinstance(self.replayed, bool):
            msg = "panel audit replayed must be boolean"
            raise PanelContractError(msg)
        if (
            isinstance(self.intentional_cash_slots, bool)
            or not isinstance(self.intentional_cash_slots, int)
            or self.intentional_cash_slots < 0
        ):
            msg = "panel audit intentional_cash_slots must be a nonnegative integer"
            raise PanelContractError(msg)
        if not isinstance(self.required_factor_versions, tuple):
            msg = "required_factor_versions must be an immutable tuple"
            raise PanelContractError(msg)
        required_names: list[str] = []
        for item in self.required_factor_versions:
            if not isinstance(item, tuple) or len(item) != _FACTOR_VERSION_PAIR_LENGTH:
                msg = "required_factor_versions contains an invalid entry"
                raise PanelContractError(msg)
            factor_name, factor_version = item
            required_names.append(_require_text(factor_name, field_name="factor_name"))
            if factor_version is not None:
                _require_text(factor_version, field_name="factor_version")
        if required_names != sorted(required_names) or len(required_names) != len(
            set(required_names)
        ):
            msg = "required_factor_versions must be unique and canonically ordered"
            raise PanelContractError(msg)
        configured = {spec.name: spec.enabled for spec in self.rank_snapshot.factor_specs}
        if set(required_names) != set(configured):
            msg = "required_factor_versions must cover every configured factor"
            raise PanelContractError(msg)
        for factor_name, factor_version in self.required_factor_versions:
            if configured[factor_name] != (factor_version is not None):
                msg = "factor version presence must match enabled factor configuration"
                raise PanelContractError(msg)


def panel_evaluation_audit_to_payload(
    audit: PanelEvaluationAudit,
) -> dict[str, object]:
    """Encode the complete derived rank and final eligibility audit."""

    return {
        "schema": "panel-evaluation-audit-v1",
        "configuration_sha256": audit.configuration_sha256,
        "peer_taxonomy_version": audit.peer_taxonomy_version,
        "replayed": audit.replayed,
        "intentional_cash_slots": audit.intentional_cash_slots,
        "required_factor_versions": [
            {"factor_name": factor_name, "factor_version": factor_version}
            for factor_name, factor_version in audit.required_factor_versions
        ],
        "rank_snapshot": audit.rank_snapshot.to_dict(),
        "rows": [
            {
                "entity_id": row.entity_id,
                "symbol": row.symbol,
                "instrument_id": row.instrument_id,
                "factor_snapshot_id": row.factor_snapshot_id,
                "rank_complete": row.rank_complete,
                "strategy_eligible": row.strategy_eligible,
                "decision": row.decision.value,
                "incumbent": row.incumbent,
                "rank": row.rank,
                "composite_score": row.composite_score,
                "target_allocation_hint": row.target_allocation_hint,
                "exclusion_reason": row.exclusion_reason,
            }
            for row in audit.rows
        ],
    }


def _counts(values: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def evaluate_panel_readiness(  # noqa: PLR0912 - explicit completeness matrix
    candidate: PanelReadyInput,
) -> PanelReadiness:
    """Prove that every effective member has one cutoff-safe disposition."""

    issues: set[PanelIssue] = set()
    if not (candidate.session.closes_at <= candidate.cutoff < candidate.execution_session.opens_at):
        issues.add(PanelIssue(PanelIssueCode.CUTOFF_MISMATCH))

    member_counts = _counts(tuple(member.security_id for member in candidate.members))
    instrument_counts = _counts(tuple(str(member.instrument_id) for member in candidate.members))
    symbol_counts = _counts(tuple(member.canonical_symbol for member in candidate.members))
    observation_counts = _counts(tuple(ref.security_id for ref in candidate.observations))
    exclusion_counts = _counts(tuple(item.security_id for item in candidate.exclusions))

    for security_id, count in member_counts.items():
        if count > 1:
            issues.add(PanelIssue(PanelIssueCode.DUPLICATE_MEMBER, security_id))
    for member in candidate.members:
        if instrument_counts[str(member.instrument_id)] > 1:
            issues.add(PanelIssue(PanelIssueCode.DUPLICATE_INSTRUMENT, member.security_id))
        if symbol_counts[member.canonical_symbol] > 1:
            issues.add(PanelIssue(PanelIssueCode.DUPLICATE_SYMBOL, member.security_id))
    for security_id, count in observation_counts.items():
        if count > 1:
            issues.add(PanelIssue(PanelIssueCode.DUPLICATE_OBSERVATION, security_id))
    for security_id, count in exclusion_counts.items():
        if count > 1:
            issues.add(PanelIssue(PanelIssueCode.DUPLICATE_EXCLUSION, security_id))

    member_ids = set(member_counts)
    observation_ids = set(observation_counts)
    exclusion_ids = set(exclusion_counts)
    for security_id in sorted(observation_ids - member_ids):
        issues.add(PanelIssue(PanelIssueCode.EXTRA_OBSERVATION, security_id))
    for security_id in sorted(exclusion_ids - member_ids):
        issues.add(PanelIssue(PanelIssueCode.EXTRA_EXCLUSION, security_id))
    for security_id in sorted(observation_ids & exclusion_ids):
        issues.add(PanelIssue(PanelIssueCode.MEMBER_BOTH_OBSERVED_AND_EXCLUDED, security_id))
    for security_id in sorted(member_ids - observation_ids - exclusion_ids):
        issues.add(PanelIssue(PanelIssueCode.MISSING_MEMBER, security_id))
    for ref in candidate.observations:
        if ref.available_at > candidate.cutoff:
            issues.add(PanelIssue(PanelIssueCode.LATE_OBSERVATION, ref.security_id))

    return PanelReadiness(
        panel_sha256=candidate.canonical_digest(),
        issues=tuple(sorted(issues)),
        observed_count=len(observation_ids & member_ids),
        excluded_count=len(exclusion_ids & member_ids),
        required_count=len(member_ids),
    )


def panel_ready_input_to_payload(candidate: PanelReadyInput) -> dict[str, object]:
    """Encode the full generic panel contract in canonical collection order."""

    return {
        "schema": "panel-ready-input-v2",
        "cutoff": candidate.cutoff.isoformat(),
        "session": {
            "mic": candidate.session.mic,
            "session_date": candidate.session.session_date.isoformat(),
            "opens_at": candidate.session.opens_at.isoformat(),
            "closes_at": candidate.session.closes_at.isoformat(),
            "authority": candidate.session.authority.value,
            "source_identity": candidate.session.source_identity,
            "content_sha256": candidate.session.content_sha256,
        },
        "execution_session": {
            "mic": candidate.execution_session.mic,
            "session_date": candidate.execution_session.session_date.isoformat(),
            "opens_at": candidate.execution_session.opens_at.isoformat(),
            "closes_at": candidate.execution_session.closes_at.isoformat(),
            "authority": candidate.execution_session.authority.value,
            "source_identity": candidate.execution_session.source_identity,
            "content_sha256": candidate.execution_session.content_sha256,
        },
        "data_use_scope": candidate.data_use_scope.value,
        "provider_authority_policy": candidate.provider_authority_policy.to_payload(),
        "provider_authority_sha256": candidate.provider_authority_sha256,
        "membership_sha256": candidate.membership_sha256,
        "factor_snapshot_sha256": candidate.factor_snapshot_sha256,
        "members": [
            {
                "security_id": member.security_id,
                "issuer_id": member.issuer_id,
                "instrument_id": member.instrument_id,
                "canonical_symbol": member.canonical_symbol,
            }
            for member in sorted(candidate.members, key=lambda item: item.security_id)
        ],
        "observations": [
            {
                "security_id": observation.security_id,
                "observation_id": observation.observation_id,
                "observed_at": observation.observed_at.isoformat(),
                "available_at": observation.available_at.isoformat(),
                "content_revision": observation.content_revision,
                "content_sha256": observation.content_sha256,
            }
            for observation in sorted(
                candidate.observations,
                key=lambda item: (item.security_id, item.observation_id),
            )
        ],
        "exclusions": [
            {
                "security_id": exclusion.security_id,
                "reason_code": exclusion.reason_code,
                "disposition_identity": exclusion.disposition_identity,
                "content_sha256": exclusion.content_sha256,
            }
            for exclusion in sorted(candidate.exclusions, key=lambda item: item.security_id)
        ],
    }


def panel_ready_input_from_payload(payload: Mapping[str, Any]) -> PanelReadyInput:
    """Restore a generic panel payload and re-run every domain invariant."""

    values = _mapping(payload, field_name="panel payload")
    _require_keys(
        values,
        expected={
            "schema",
            "cutoff",
            "session",
            "execution_session",
            "data_use_scope",
            "provider_authority_policy",
            "provider_authority_sha256",
            "membership_sha256",
            "factor_snapshot_sha256",
            "members",
            "observations",
            "exclusions",
        },
        field_name="panel payload",
    )
    if values.get("schema") != "panel-ready-input-v2":
        msg = "panel payload schema is incompatible"
        raise PanelContractError(msg)
    session_values = _mapping(values.get("session"), field_name="panel session")
    _require_keys(
        session_values,
        expected={
            "mic",
            "session_date",
            "opens_at",
            "closes_at",
            "authority",
            "source_identity",
            "content_sha256",
        },
        field_name="panel session",
    )
    execution_session_values = _mapping(
        values.get("execution_session"), field_name="panel execution session"
    )
    _require_keys(
        execution_session_values,
        expected={
            "mic",
            "session_date",
            "opens_at",
            "closes_at",
            "authority",
            "source_identity",
            "content_sha256",
        },
        field_name="panel execution session",
    )
    members = _object_array(values.get("members"), field_name="panel members")
    observations = _object_array(values.get("observations"), field_name="panel observations")
    exclusions = _object_array(values.get("exclusions"), field_name="panel exclusions")
    for item in members:
        _require_keys(
            item,
            expected={"security_id", "issuer_id", "instrument_id", "canonical_symbol"},
            field_name="panel member",
        )
    for item in observations:
        _require_keys(
            item,
            expected={
                "security_id",
                "observation_id",
                "observed_at",
                "available_at",
                "content_revision",
                "content_sha256",
            },
            field_name="panel observation",
        )
    for item in exclusions:
        _require_keys(
            item,
            expected={
                "security_id",
                "reason_code",
                "disposition_identity",
                "content_sha256",
            },
            field_name="panel exclusion",
        )
    try:
        data_use_scope = DataUseScope(
            _require_text_value(values.get("data_use_scope"), field_name="data_use_scope")
        )
        authority = SessionAuthority(
            _require_text_value(session_values.get("authority"), field_name="authority")
        )
        execution_authority = SessionAuthority(
            _require_text_value(
                execution_session_values.get("authority"),
                field_name="execution authority",
            )
        )
        provider_authority_policy = ProviderAuthorityPolicy.from_payload(
            _mapping(
                values.get("provider_authority_policy"),
                field_name="provider_authority_policy",
            )
        )
        return PanelReadyInput(
            cutoff=_parse_datetime(values.get("cutoff"), field_name="cutoff"),
            session=OfficialSessionCutoff(
                mic=_require_text_value(session_values.get("mic"), field_name="mic"),
                session_date=_parse_date(
                    session_values.get("session_date"), field_name="session_date"
                ),
                opens_at=_parse_datetime(session_values.get("opens_at"), field_name="opens_at"),
                closes_at=_parse_datetime(session_values.get("closes_at"), field_name="closes_at"),
                authority=authority,
                source_identity=_require_text_value(
                    session_values.get("source_identity"), field_name="source_identity"
                ),
                content_sha256=_require_text_value(
                    session_values.get("content_sha256"), field_name="content_sha256"
                ),
            ),
            execution_session=OfficialSessionCutoff(
                mic=_require_text_value(
                    execution_session_values.get("mic"), field_name="execution mic"
                ),
                session_date=_parse_date(
                    execution_session_values.get("session_date"),
                    field_name="execution session_date",
                ),
                opens_at=_parse_datetime(
                    execution_session_values.get("opens_at"),
                    field_name="execution opens_at",
                ),
                closes_at=_parse_datetime(
                    execution_session_values.get("closes_at"),
                    field_name="execution closes_at",
                ),
                authority=execution_authority,
                source_identity=_require_text_value(
                    execution_session_values.get("source_identity"),
                    field_name="execution source_identity",
                ),
                content_sha256=_require_text_value(
                    execution_session_values.get("content_sha256"),
                    field_name="execution content_sha256",
                ),
            ),
            data_use_scope=data_use_scope,
            provider_authority_policy=provider_authority_policy,
            provider_authority_sha256=_require_text_value(
                values.get("provider_authority_sha256"),
                field_name="provider_authority_sha256",
            ),
            membership_sha256=_require_text_value(
                values.get("membership_sha256"), field_name="membership_sha256"
            ),
            factor_snapshot_sha256=_require_text_value(
                values.get("factor_snapshot_sha256"),
                field_name="factor_snapshot_sha256",
            ),
            members=tuple(
                EffectivePanelMember(
                    security_id=_require_text_value(
                        item.get("security_id"), field_name="member security_id"
                    ),
                    issuer_id=_require_text_value(
                        item.get("issuer_id"), field_name="member issuer_id"
                    ),
                    instrument_id=_positive_int(
                        item.get("instrument_id"), field_name="member instrument_id"
                    ),
                    canonical_symbol=_require_text_value(
                        item.get("canonical_symbol"), field_name="canonical_symbol"
                    ),
                )
                for item in members
            ),
            observations=tuple(
                PanelObservationRef(
                    security_id=_require_text_value(
                        item.get("security_id"), field_name="observation security_id"
                    ),
                    observation_id=_require_text_value(
                        item.get("observation_id"), field_name="observation_id"
                    ),
                    observed_at=_parse_datetime(item.get("observed_at"), field_name="observed_at"),
                    available_at=_parse_datetime(
                        item.get("available_at"), field_name="available_at"
                    ),
                    content_revision=_positive_int(
                        item.get("content_revision"), field_name="content_revision"
                    ),
                    content_sha256=_require_text_value(
                        item.get("content_sha256"), field_name="content_sha256"
                    ),
                )
                for item in observations
            ),
            exclusions=tuple(
                PanelExclusion(
                    security_id=_require_text_value(
                        item.get("security_id"), field_name="exclusion security_id"
                    ),
                    reason_code=_require_text_value(
                        item.get("reason_code"), field_name="reason_code"
                    ),
                    disposition_identity=_require_text_value(
                        item.get("disposition_identity"),
                        field_name="disposition_identity",
                    ),
                    content_sha256=_require_text_value(
                        item.get("content_sha256"), field_name="content_sha256"
                    ),
                )
                for item in exclusions
            ),
        )
    except ValueError as exc:
        if isinstance(exc, PanelContractError):
            raise
        message = "panel payload contains an invalid enum or timestamp"
        raise PanelContractError(message) from exc


def _mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        message = f"{field_name} must be an object with string keys"
        raise PanelContractError(message)
    return value


def _require_keys(value: Mapping[str, Any], *, expected: set[str], field_name: str) -> None:
    if set(value) != expected:
        message = f"{field_name} has an incompatible field set"
        raise PanelContractError(message)


def _object_array(value: object, *, field_name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        message = f"{field_name} must be an array"
        raise PanelContractError(message)
    return tuple(_mapping(item, field_name=field_name) for item in value)


def _require_text_value(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        message = f"{field_name} must be a string"
        raise PanelContractError(message)
    return _require_text(value, field_name=field_name)


def _parse_datetime(value: object, *, field_name: str) -> datetime:
    text = _require_text_value(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        message = f"{field_name} must be an ISO-8601 datetime"
        raise PanelContractError(message) from exc
    return _require_aware(parsed, field_name=field_name)


def _parse_date(value: object, *, field_name: str) -> date:
    text = _require_text_value(value, field_name=field_name)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        message = f"{field_name} must be an ISO-8601 date"
        raise PanelContractError(message) from exc


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        message = f"{field_name} must be a positive integer"
        raise PanelContractError(message)
    return value


def _finite_number(
    value: object,
    *,
    field_name: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        message = f"{field_name} must be a finite number"
        raise PanelContractError(message)
    converted = float(value)
    if not math.isfinite(converted):
        message = f"{field_name} must be a finite number"
        raise PanelContractError(message)
    if minimum is not None and converted < minimum:
        message = f"{field_name} is below its minimum"
        raise PanelContractError(message)
    if maximum is not None and converted > maximum:
        message = f"{field_name} exceeds its maximum"
        raise PanelContractError(message)
    return converted


__all__ = [
    "EffectivePanelMember",
    "OfficialSessionCutoff",
    "PanelAuditDecision",
    "PanelContractError",
    "PanelEvaluationAudit",
    "PanelEvaluationRow",
    "PanelExclusion",
    "PanelIssue",
    "PanelIssueCode",
    "PanelNotReadyError",
    "PanelObservationRef",
    "PanelReadiness",
    "PanelReadyInput",
    "SessionAuthority",
    "SynchronizedPanelStrategy",
    "evaluate_panel_readiness",
    "panel_evaluation_audit_to_payload",
    "panel_ready_input_from_payload",
    "panel_ready_input_to_payload",
]
