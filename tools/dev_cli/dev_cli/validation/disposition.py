"""Fail-closed historical strategy completion and disposition policy.

This module is deliberately independent of any campaign runner.  It consumes
small, typed summaries of already-persisted evidence and applies a registered
hierarchy without reading files, running strategies, or authorising trading.
Historical evidence may reject a strategy or freeze an exact baseline for a
future prospective shadow study; it can never authorise paper or live trading.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

from dev_cli.validation.correctness import (
    CorrectnessFindingSeverity,
    CorrectnessFindingStatus,
)
from dev_cli.validation.correctness import safe_identifier as _identifier

_SCHEMA_ID: Final = "vynmatrix.historical-strategy-disposition"
_SCHEMA_VERSION: Final = 1
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_OPEN_FINDING_STATUSES: Final = frozenset(
    {
        CorrectnessFindingStatus.REMEDIATION_REQUIRED,
        CorrectnessFindingStatus.UNRESOLVED,
    }
)
_HIGH_FINDING_SEVERITIES: Final = frozenset(
    {
        CorrectnessFindingSeverity.HIGH,
        CorrectnessFindingSeverity.CRITICAL,
    }
)


class HistoricalCompletionState(str, Enum):
    """Whether all registered historical evidence is decision-usable."""

    COMPLETE = "COMPLETE"
    BLOCKED_INSUFFICIENT_EVIDENCE = "BLOCKED_INSUFFICIENT_EVIDENCE"


class HistoricalEconomicDisposition(str, Enum):
    """Historical research disposition, never a trading authorization."""

    FREEZE_FOR_PROSPECTIVE_SHADOW = "FREEZE_FOR_PROSPECTIVE_SHADOW"
    REDESIGN = "REDESIGN"
    RETIRE = "RETIRE"


class HistoricalEvidenceKind(str, Enum):
    """Required evidence classes which must all be registered by policy."""

    TRIAL = "trial"
    SOURCE = "source"
    HASH_RECONCILIATION = "hash_reconciliation"
    ROBUSTNESS = "robustness"
    DEFLATED_SHARPE = "deflated_sharpe"
    POWER = "power"
    SCORING = "scoring"
    CORRECTNESS = "correctness"


class HistoricalEvidenceStatus(str, Enum):
    """Integrity state of one registered evidence requirement."""

    VERIFIED = "verified"
    VERIFIED_EMPTY = "verified_empty"
    MISSING = "missing"
    CORRUPT = "corrupt"

    @property
    def complete(self) -> bool:
        """Return whether the status represents observed, trustworthy evidence."""

        return self in {self.VERIFIED, self.VERIFIED_EMPTY}


def _unique_identifiers(values: tuple[str, ...], *, field: str, nonempty: bool = True) -> None:
    if nonempty and not values:
        message = f"{field} must not be empty"
        raise ValueError(message)
    normalized = tuple(_identifier(value, field=f"{field}[]") for value in values)
    if len(set(normalized)) != len(normalized):
        message = f"{field} must contain unique identifiers"
        raise ValueError(message)


def _require_bool(value: object, *, field: str) -> None:
    if not isinstance(value, bool):
        message = f"{field} must be a bool"
        raise TypeError(message)


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceRequirement:
    """One pre-registered evidence check and its exact source identities."""

    check_id: str
    kind: HistoricalEvidenceKind
    required_source_ids: tuple[str, ...]
    allows_verified_empty: bool = False

    def __post_init__(self) -> None:
        _identifier(self.check_id, field="check_id")
        if not isinstance(self.kind, HistoricalEvidenceKind):
            message = "kind must be a HistoricalEvidenceKind"
            raise TypeError(message)
        _unique_identifiers(self.required_source_ids, field="required_source_ids")
        _require_bool(self.allows_verified_empty, field="allows_verified_empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "kind": self.kind.value,
            "required_source_ids": list(self.required_source_ids),
            "allows_verified_empty": self.allows_verified_empty,
        }


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceSource:
    """Observed digest for one registered source; ``None`` means unavailable."""

    source_id: str
    sha256: str | None

    def __post_init__(self) -> None:
        _identifier(self.source_id, field="source_id")
        if self.sha256 is not None and not _SHA256_RE.fullmatch(self.sha256):
            message = "sha256 must be a lowercase 64-character SHA-256 digest or None"
            raise ValueError(message)

    def to_dict(self) -> dict[str, object]:
        return {"source_id": self.source_id, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceCheck:
    """Observed status and provenance for one registered evidence requirement."""

    check_id: str
    kind: HistoricalEvidenceKind
    status: HistoricalEvidenceStatus
    sources: tuple[HistoricalEvidenceSource, ...]

    def __post_init__(self) -> None:
        _identifier(self.check_id, field="check_id")
        if not isinstance(self.kind, HistoricalEvidenceKind):
            message = "kind must be a HistoricalEvidenceKind"
            raise TypeError(message)
        if not isinstance(self.status, HistoricalEvidenceStatus):
            message = "status must be a HistoricalEvidenceStatus"
            raise TypeError(message)
        if any(not isinstance(source, HistoricalEvidenceSource) for source in self.sources):
            message = "sources must contain HistoricalEvidenceSource values"
            raise TypeError(message)
        source_ids = tuple(source.source_id for source in self.sources)
        _unique_identifiers(source_ids, field="sources[].source_id", nonempty=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "complete": self.status.complete,
            "sources": [source.to_dict() for source in self.sources],
        }


@dataclass(frozen=True, slots=True)
class HistoricalGateCheck:
    """One exact pre-registered pass/fail gate."""

    gate_id: str
    passed: bool

    def __post_init__(self) -> None:
        _identifier(self.gate_id, field="gate_id")
        _require_bool(self.passed, field="passed")

    def to_dict(self) -> dict[str, object]:
        return {"gate_id": self.gate_id, "passed": self.passed}


@dataclass(frozen=True, slots=True)
class FixedBaselineDispositionSummary:
    """Decision fields extracted from the fixed-baseline robustness payload."""

    arm_id: str
    gate_checks: tuple[HistoricalGateCheck, ...]
    zero_trades_or_exposure: bool

    def __post_init__(self) -> None:
        _identifier(self.arm_id, field="arm_id")
        if any(not isinstance(check, HistoricalGateCheck) for check in self.gate_checks):
            message = "gate_checks must contain HistoricalGateCheck values"
            raise TypeError(message)
        gate_ids = tuple(check.gate_id for check in self.gate_checks)
        _unique_identifiers(gate_ids, field="gate_checks[].gate_id")
        _require_bool(self.zero_trades_or_exposure, field="zero_trades_or_exposure")

    def to_dict(self) -> dict[str, object]:
        return {
            "arm_id": self.arm_id,
            "gate_checks": [check.to_dict() for check in self.gate_checks],
            "zero_trades_or_exposure": self.zero_trades_or_exposure,
        }


@dataclass(frozen=True, slots=True)
class RegisteredRedesignCandidateSummary:
    """Decision fields for one exact registered quantitative redesign arm."""

    candidate_id: str
    gate_checks: tuple[HistoricalGateCheck, ...]
    zero_trades_or_exposure: bool

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, field="candidate_id")
        if any(not isinstance(check, HistoricalGateCheck) for check in self.gate_checks):
            message = "gate_checks must contain HistoricalGateCheck values"
            raise TypeError(message)
        _require_bool(self.zero_trades_or_exposure, field="zero_trades_or_exposure")

    @property
    def eligible(self) -> bool:
        return not self.zero_trades_or_exposure and all(check.passed for check in self.gate_checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "gate_checks": [check.to_dict() for check in self.gate_checks],
            "zero_trades_or_exposure": self.zero_trades_or_exposure,
            "quantitative_candidate_gate_eligible": self.eligible,
        }


@dataclass(frozen=True, slots=True)
class RegisteredRedesignDispositionSummary:
    """Complete evidence for all quantitative redesign arms; selects no winner."""

    candidates: tuple[RegisteredRedesignCandidateSummary, ...]

    def __post_init__(self) -> None:
        if any(
            not isinstance(candidate, RegisteredRedesignCandidateSummary)
            for candidate in self.candidates
        ):
            message = "candidates must contain RegisteredRedesignCandidateSummary values"
            raise TypeError(message)
        candidate_ids = tuple(candidate.candidate_id for candidate in self.candidates)
        _unique_identifiers(candidate_ids, field="candidates[].candidate_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "no_selection_performed": True,
        }


@dataclass(frozen=True, slots=True)
class ProspectivePowerDispositionSummary:
    """Decision fields from the observed-cadence prospective power design."""

    primary_effect_id: str
    maximum_duration_years: float
    powered_within_maximum_duration: bool
    operationally_impractical: bool
    not_applicable_due_to_zero_trades: bool

    def __post_init__(self) -> None:
        _identifier(self.primary_effect_id, field="primary_effect_id")
        if (
            isinstance(self.maximum_duration_years, bool)
            or not isinstance(self.maximum_duration_years, (int, float))
            or not math.isfinite(float(self.maximum_duration_years))
            or self.maximum_duration_years <= 0.0
        ):
            message = "maximum_duration_years must be finite and positive"
            raise ValueError(message)
        _require_bool(
            self.powered_within_maximum_duration,
            field="powered_within_maximum_duration",
        )
        _require_bool(self.operationally_impractical, field="operationally_impractical")
        _require_bool(
            self.not_applicable_due_to_zero_trades,
            field="not_applicable_due_to_zero_trades",
        )
        if self.operationally_impractical == self.powered_within_maximum_duration:
            message = (
                "operationally_impractical must be the inverse of powered_within_maximum_duration"
            )
            raise ValueError(message)
        if self.not_applicable_due_to_zero_trades and self.powered_within_maximum_duration:
            message = "zero-trade power evidence cannot be adequately powered"
            raise ValueError(message)

    def to_dict(self) -> dict[str, object]:
        return {
            "primary_effect_id": self.primary_effect_id,
            "maximum_duration_years": float(self.maximum_duration_years),
            "powered_within_maximum_duration": self.powered_within_maximum_duration,
            "operationally_impractical": self.operationally_impractical,
            "not_applicable_due_to_zero_trades": (self.not_applicable_due_to_zero_trades),
        }


@dataclass(frozen=True, slots=True)
class SemanticCorrectnessFindingSummary:
    """Decision-relevant semantic finding from a verified correctness attestation."""

    finding_id: str
    severity: CorrectnessFindingSeverity
    status: CorrectnessFindingStatus
    semantic_candidate_id: str | None
    evidence_source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.finding_id, field="finding_id")
        if not isinstance(self.severity, CorrectnessFindingSeverity):
            message = "severity must be a CorrectnessFindingSeverity"
            raise TypeError(message)
        if not isinstance(self.status, CorrectnessFindingStatus):
            message = "status must be a CorrectnessFindingStatus"
            raise TypeError(message)
        if self.semantic_candidate_id is not None:
            _identifier(self.semantic_candidate_id, field="semantic_candidate_id")
        _unique_identifiers(self.evidence_source_ids, field="evidence_source_ids")

    @property
    def unresolved_high_severity(self) -> bool:
        return self.severity in _HIGH_FINDING_SEVERITIES and self.status in (_OPEN_FINDING_STATUSES)

    def to_dict(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity.value,
            "status": self.status.value,
            "semantic_candidate_id": self.semantic_candidate_id,
            "evidence_source_ids": list(self.evidence_source_ids),
            "unresolved_high_severity": self.unresolved_high_severity,
        }


@dataclass(frozen=True, slots=True)
class CorrectnessDispositionSummary:
    """Semantic findings extracted from verified, hash-bound correctness evidence."""

    findings: tuple[SemanticCorrectnessFindingSummary, ...]

    def __post_init__(self) -> None:
        if any(
            not isinstance(finding, SemanticCorrectnessFindingSummary) for finding in self.findings
        ):
            message = "findings must contain SemanticCorrectnessFindingSummary values"
            raise TypeError(message)
        finding_ids = tuple(finding.finding_id for finding in self.findings)
        _unique_identifiers(finding_ids, field="findings[].finding_id", nonempty=False)

    def to_dict(self) -> dict[str, object]:
        return {"findings": [finding.to_dict() for finding in self.findings]}


@dataclass(frozen=True, slots=True)
class HistoricalDispositionPolicy:
    """Frozen identities and gate registries for one historical campaign."""

    policy_id: str
    baseline_arm_id: str
    baseline_ledger_check_id: str
    required_evidence: tuple[HistoricalEvidenceRequirement, ...]
    baseline_quantitative_gate_ids: tuple[str, ...]
    baseline_economic_gate_ids: tuple[str, ...]
    prospective_primary_effect_id: str
    maximum_prospective_duration_years: float
    registered_redesign_candidate_ids: tuple[str, ...]
    redesign_quantitative_gate_ids: tuple[str, ...]
    registered_semantic_candidate_ids: tuple[str, ...]
    correctable_semantic_candidate_ids: tuple[str, ...]
    registered_promotion_blocker_finding_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.policy_id, field="policy_id")
        _identifier(self.baseline_arm_id, field="baseline_arm_id")
        _identifier(self.baseline_ledger_check_id, field="baseline_ledger_check_id")
        _identifier(
            self.prospective_primary_effect_id,
            field="prospective_primary_effect_id",
        )
        if (
            isinstance(self.maximum_prospective_duration_years, bool)
            or not isinstance(self.maximum_prospective_duration_years, (int, float))
            or not math.isfinite(float(self.maximum_prospective_duration_years))
            or self.maximum_prospective_duration_years <= 0.0
        ):
            message = "maximum_prospective_duration_years must be finite and positive"
            raise ValueError(message)
        if any(
            not isinstance(requirement, HistoricalEvidenceRequirement)
            for requirement in self.required_evidence
        ):
            message = "required_evidence must contain HistoricalEvidenceRequirement values"
            raise TypeError(message)
        check_ids = tuple(requirement.check_id for requirement in self.required_evidence)
        _unique_identifiers(check_ids, field="required_evidence[].check_id")
        observed_kinds = {requirement.kind for requirement in self.required_evidence}
        missing_kinds = set(HistoricalEvidenceKind) - observed_kinds
        if missing_kinds:
            missing = sorted(kind.value for kind in missing_kinds)
            message = f"required_evidence omits mandatory evidence kinds: {missing}"
            raise ValueError(message)
        requirement_by_id = {
            requirement.check_id: requirement for requirement in self.required_evidence
        }
        ledger_requirement = requirement_by_id.get(self.baseline_ledger_check_id)
        if ledger_requirement is None:
            message = "baseline_ledger_check_id is not a registered evidence check"
            raise ValueError(message)
        if ledger_requirement.kind is not HistoricalEvidenceKind.ROBUSTNESS:
            message = "baseline_ledger_check_id must identify a robustness check"
            raise ValueError(message)
        if not ledger_requirement.allows_verified_empty:
            message = "baseline ledger evidence must allow a verified-empty observation"
            raise ValueError(message)

        _unique_identifiers(
            self.baseline_quantitative_gate_ids,
            field="baseline_quantitative_gate_ids",
        )
        _unique_identifiers(
            self.baseline_economic_gate_ids,
            field="baseline_economic_gate_ids",
        )
        unknown_economic = set(self.baseline_economic_gate_ids) - set(
            self.baseline_quantitative_gate_ids
        )
        if unknown_economic:
            message = (
                "baseline_economic_gate_ids are not registered quantitative gates: "
                f"{sorted(unknown_economic)}"
            )
            raise ValueError(message)
        _unique_identifiers(
            self.registered_redesign_candidate_ids,
            field="registered_redesign_candidate_ids",
        )
        _unique_identifiers(
            self.redesign_quantitative_gate_ids,
            field="redesign_quantitative_gate_ids",
        )
        _unique_identifiers(
            self.registered_semantic_candidate_ids,
            field="registered_semantic_candidate_ids",
            nonempty=False,
        )
        _unique_identifiers(
            self.correctable_semantic_candidate_ids,
            field="correctable_semantic_candidate_ids",
            nonempty=False,
        )
        unknown_correctable = set(self.correctable_semantic_candidate_ids) - set(
            self.registered_semantic_candidate_ids
        )
        if unknown_correctable:
            message = (
                f"correctable semantic candidates are not registered: {sorted(unknown_correctable)}"
            )
            raise ValueError(message)
        _unique_identifiers(
            self.registered_promotion_blocker_finding_ids,
            field="registered_promotion_blocker_finding_ids",
            nonempty=False,
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "policy_id": self.policy_id,
            "baseline_arm_id": self.baseline_arm_id,
            "baseline_ledger_check_id": self.baseline_ledger_check_id,
            "required_evidence": [item.to_dict() for item in self.required_evidence],
            "baseline_quantitative_gate_ids": list(self.baseline_quantitative_gate_ids),
            "baseline_economic_gate_ids": list(self.baseline_economic_gate_ids),
            "prospective_primary_effect_id": self.prospective_primary_effect_id,
            "maximum_prospective_duration_years": float(self.maximum_prospective_duration_years),
            "registered_redesign_candidate_ids": list(self.registered_redesign_candidate_ids),
            "redesign_quantitative_gate_ids": list(self.redesign_quantitative_gate_ids),
            "registered_semantic_candidate_ids": list(self.registered_semantic_candidate_ids),
            "correctable_semantic_candidate_ids": list(self.correctable_semantic_candidate_ids),
        }
        if self.registered_promotion_blocker_finding_ids:
            payload["registered_promotion_blocker_finding_ids"] = list(
                self.registered_promotion_blocker_finding_ids
            )
        return payload


@dataclass(frozen=True, slots=True)
class HistoricalDispositionEvidence:
    """Typed, side-effect-free inputs to the historical hierarchy."""

    checks: tuple[HistoricalEvidenceCheck, ...]
    baseline: FixedBaselineDispositionSummary | None
    redesign: RegisteredRedesignDispositionSummary | None
    power: ProspectivePowerDispositionSummary | None
    correctness: CorrectnessDispositionSummary | None

    def __post_init__(self) -> None:
        if not isinstance(self.checks, tuple):
            message = "checks must be a tuple"
            raise TypeError(message)
        if any(not isinstance(check, HistoricalEvidenceCheck) for check in self.checks):
            message = "checks must contain HistoricalEvidenceCheck values"
            raise TypeError(message)
        optional_types = (
            ("baseline", self.baseline, FixedBaselineDispositionSummary),
            ("redesign", self.redesign, RegisteredRedesignDispositionSummary),
            ("power", self.power, ProspectivePowerDispositionSummary),
            ("correctness", self.correctness, CorrectnessDispositionSummary),
        )
        for field_name, value, expected_type in optional_types:
            if value is not None and not isinstance(value, expected_type):
                message = f"{field_name} must be {expected_type.__name__} or None"
                raise TypeError(message)


@dataclass(frozen=True, slots=True)
class HistoricalDispositionResult:
    """Canonical historical conclusion with explicit non-authorization limits."""

    policy_id: str
    baseline_arm_id: str
    completion_state: HistoricalCompletionState
    economic_disposition: HistoricalEconomicDisposition | None
    frozen_arm_id: str | None
    evidence_checks: tuple[HistoricalEvidenceCheck, ...]
    baseline: FixedBaselineDispositionSummary | None
    redesign: RegisteredRedesignDispositionSummary | None
    power: ProspectivePowerDispositionSummary | None
    correctness: CorrectnessDispositionSummary | None
    eligible_quantitative_redesign_candidate_ids: tuple[str, ...]
    eligible_semantic_redesign_candidate_ids: tuple[str, ...]
    blocker_codes: tuple[str, ...]
    reason_codes: tuple[str, ...]

    @property
    def prospective_shadow_research_eligible(self) -> bool:
        return (
            self.economic_disposition is HistoricalEconomicDisposition.FREEZE_FOR_PROSPECTIVE_SHADOW
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_id": _SCHEMA_ID,
            "schema_version": _SCHEMA_VERSION,
            "policy_id": self.policy_id,
            "baseline_arm_id": self.baseline_arm_id,
            "completion_state": self.completion_state.value,
            "economic_disposition": (
                self.economic_disposition.value if self.economic_disposition is not None else None
            ),
            "frozen_arm_id": self.frozen_arm_id,
            "evidence_checks": [check.to_dict() for check in self.evidence_checks],
            "baseline": self.baseline.to_dict() if self.baseline is not None else None,
            "redesign": self.redesign.to_dict() if self.redesign is not None else None,
            "power": self.power.to_dict() if self.power is not None else None,
            "correctness": (self.correctness.to_dict() if self.correctness is not None else None),
            "eligible_quantitative_redesign_candidate_ids": list(
                self.eligible_quantitative_redesign_candidate_ids
            ),
            "eligible_semantic_redesign_candidate_ids": list(
                self.eligible_semantic_redesign_candidate_ids
            ),
            "selected_redesign_candidate_id": None,
            "blocker_codes": list(self.blocker_codes),
            "reason_codes": list(self.reason_codes),
            "authority": {
                "decision_scope": "historical_research_ranking_rejection_only",
                "prospective_shadow_research_eligible": (self.prospective_shadow_research_eligible),
                "paper_trading_authorized": False,
                "live_trading_authorized": False,
                "automatic_parameter_deployment_authorized": False,
                "future_promotion_requires_prospective_evidence": True,
            },
        }


def _normalize_evidence_checks(
    policy: HistoricalDispositionPolicy,
    checks: tuple[HistoricalEvidenceCheck, ...],
) -> tuple[HistoricalEvidenceCheck, ...]:
    if any(not isinstance(check, HistoricalEvidenceCheck) for check in checks):
        message = "checks must contain HistoricalEvidenceCheck values"
        raise TypeError(message)
    observed_ids = tuple(check.check_id for check in checks)
    _unique_identifiers(observed_ids, field="checks[].check_id", nonempty=False)
    required_by_id = {item.check_id: item for item in policy.required_evidence}
    observed_by_id = {item.check_id: item for item in checks}
    missing = sorted(set(required_by_id) - set(observed_by_id))
    unknown = sorted(set(observed_by_id) - set(required_by_id))
    if missing or unknown:
        message = f"evidence check IDs differ (missing={missing}, unknown={unknown})"
        raise ValueError(message)

    normalized: list[HistoricalEvidenceCheck] = []
    observed_source_hashes: dict[str, str] = {}
    for requirement in policy.required_evidence:
        check = observed_by_id[requirement.check_id]
        if check.kind is not requirement.kind:
            message = f"evidence check {check.check_id} has the wrong evidence kind"
            raise ValueError(message)
        if (
            check.status is HistoricalEvidenceStatus.VERIFIED_EMPTY
            and not requirement.allows_verified_empty
        ):
            message = f"evidence check {check.check_id} cannot be verified empty"
            raise ValueError(message)
        supplied_by_id = {source.source_id: source for source in check.sources}
        unknown_sources = sorted(set(supplied_by_id) - set(requirement.required_source_ids))
        if unknown_sources:
            message = (
                f"evidence check {check.check_id} references unknown sources: {unknown_sources}"
            )
            raise ValueError(message)
        canonical_sources: list[HistoricalEvidenceSource] = []
        for source_id in requirement.required_source_ids:
            source = supplied_by_id.get(source_id)
            if source is None:
                source = HistoricalEvidenceSource(source_id=source_id, sha256=None)
            if check.status.complete and source.sha256 is None:
                message = (
                    f"complete evidence check {check.check_id} lacks a verified digest for "
                    f"source {source_id}"
                )
                raise ValueError(message)
            if source.sha256 is not None:
                prior = observed_source_hashes.setdefault(source_id, source.sha256)
                if prior != source.sha256:
                    message = f"source {source_id} is bound to contradictory SHA-256 digests"
                    raise ValueError(message)
            canonical_sources.append(source)
        normalized.append(
            HistoricalEvidenceCheck(
                check_id=check.check_id,
                kind=check.kind,
                status=check.status,
                sources=tuple(canonical_sources),
            )
        )
    return tuple(normalized)


def _normalize_gate_checks(
    checks: tuple[HistoricalGateCheck, ...],
    required_gate_ids: tuple[str, ...],
    *,
    field: str,
) -> tuple[HistoricalGateCheck, ...]:
    if any(not isinstance(check, HistoricalGateCheck) for check in checks):
        message = f"{field} must contain HistoricalGateCheck values"
        raise TypeError(message)
    observed_ids = tuple(check.gate_id for check in checks)
    _unique_identifiers(observed_ids, field=f"{field}[].gate_id", nonempty=False)
    by_id = {check.gate_id: check for check in checks}
    missing = sorted(set(required_gate_ids) - set(by_id))
    unknown = sorted(set(by_id) - set(required_gate_ids))
    if missing or unknown:
        message = f"{field} IDs differ (missing={missing}, unknown={unknown})"
        raise ValueError(message)
    return tuple(by_id[gate_id] for gate_id in required_gate_ids)


def _validate_registered_promotion_blockers(
    policy: HistoricalDispositionPolicy,
    correctness: CorrectnessDispositionSummary,
) -> None:
    finding_by_id = {finding.finding_id: finding for finding in correctness.findings}
    missing = sorted(set(policy.registered_promotion_blocker_finding_ids) - set(finding_by_id))
    if missing:
        message = (
            "registered promotion blocker findings are missing from correctness evidence: "
            f"{missing}"
        )
        raise ValueError(message)
    for finding_id in policy.registered_promotion_blocker_finding_ids:
        finding = finding_by_id[finding_id]
        if finding.status not in _OPEN_FINDING_STATUSES:
            message = (
                f"registered promotion blocker finding {finding_id} is not open for remediation"
            )
            raise ValueError(message)
        if finding.severity not in _HIGH_FINDING_SEVERITIES:
            message = f"registered promotion blocker finding {finding_id} is not high or critical"
            raise ValueError(message)
        if finding.semantic_candidate_id is not None:
            message = (
                f"registered promotion blocker finding {finding_id} must not identify "
                "a semantic candidate"
            )
            raise ValueError(message)


def _normalize_summaries(
    policy: HistoricalDispositionPolicy,
    evidence: HistoricalDispositionEvidence,
    *,
    require_all: bool,
) -> tuple[
    FixedBaselineDispositionSummary | None,
    RegisteredRedesignDispositionSummary | None,
    ProspectivePowerDispositionSummary | None,
    CorrectnessDispositionSummary | None,
]:
    baseline = evidence.baseline
    redesign = evidence.redesign
    power = evidence.power
    correctness = evidence.correctness
    if require_all:
        missing = [
            name
            for name, value in (
                ("baseline", baseline),
                ("redesign", redesign),
                ("power", power),
                ("correctness", correctness),
            )
            if value is None
        ]
        if missing:
            message = f"verified evidence is missing typed summaries: {missing}"
            raise ValueError(message)

    if baseline is not None:
        if not isinstance(baseline, FixedBaselineDispositionSummary):
            message = "baseline must be a FixedBaselineDispositionSummary or None"
            raise TypeError(message)
        if baseline.arm_id != policy.baseline_arm_id:
            message = "baseline summary does not identify the exact registered baseline arm"
            raise ValueError(message)
        baseline = FixedBaselineDispositionSummary(
            arm_id=baseline.arm_id,
            gate_checks=_normalize_gate_checks(
                baseline.gate_checks,
                policy.baseline_quantitative_gate_ids,
                field="baseline.gate_checks",
            ),
            zero_trades_or_exposure=baseline.zero_trades_or_exposure,
        )

    if redesign is not None:
        if not isinstance(redesign, RegisteredRedesignDispositionSummary):
            message = "redesign must be a RegisteredRedesignDispositionSummary or None"
            raise TypeError(message)
        candidate_by_id = {candidate.candidate_id: candidate for candidate in redesign.candidates}
        missing = sorted(set(policy.registered_redesign_candidate_ids) - set(candidate_by_id))
        unknown = sorted(set(candidate_by_id) - set(policy.registered_redesign_candidate_ids))
        if missing or unknown:
            message = f"redesign candidate IDs differ (missing={missing}, unknown={unknown})"
            raise ValueError(message)
        redesign = RegisteredRedesignDispositionSummary(
            candidates=tuple(
                RegisteredRedesignCandidateSummary(
                    candidate_id=candidate_id,
                    gate_checks=_normalize_gate_checks(
                        candidate_by_id[candidate_id].gate_checks,
                        policy.redesign_quantitative_gate_ids,
                        field=f"redesign[{candidate_id}].gate_checks",
                    ),
                    zero_trades_or_exposure=(candidate_by_id[candidate_id].zero_trades_or_exposure),
                )
                for candidate_id in policy.registered_redesign_candidate_ids
            )
        )

    if power is not None:
        if not isinstance(power, ProspectivePowerDispositionSummary):
            message = "power must be a ProspectivePowerDispositionSummary or None"
            raise TypeError(message)
        if power.primary_effect_id != policy.prospective_primary_effect_id:
            message = "power summary does not identify the registered primary effect"
            raise ValueError(message)
        if float(power.maximum_duration_years) != float(policy.maximum_prospective_duration_years):
            message = "power summary does not use the registered maximum duration"
            raise ValueError(message)

    if correctness is not None:
        if not isinstance(correctness, CorrectnessDispositionSummary):
            message = "correctness must be a CorrectnessDispositionSummary or None"
            raise TypeError(message)
        known_semantics = set(policy.registered_semantic_candidate_ids)
        unknown_semantics = sorted(
            {
                finding.semantic_candidate_id
                for finding in correctness.findings
                if finding.semantic_candidate_id is not None
                and finding.semantic_candidate_id not in known_semantics
            }
        )
        if unknown_semantics:
            message = f"correctness findings reference unknown semantic IDs: {unknown_semantics}"
            raise ValueError(message)
        correctness = CorrectnessDispositionSummary(
            findings=tuple(sorted(correctness.findings, key=lambda item: item.finding_id))
        )
        _validate_registered_promotion_blockers(policy, correctness)

    return baseline, redesign, power, correctness


def _classify_correctness_findings(
    policy: HistoricalDispositionPolicy,
    correctness: CorrectnessDispositionSummary,
) -> tuple[
    tuple[SemanticCorrectnessFindingSummary, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[SemanticCorrectnessFindingSummary, ...],
    tuple[SemanticCorrectnessFindingSummary, ...],
]:
    """Separate safety blockers, redesign triggers, and carried remediations."""

    open_findings = tuple(
        finding for finding in correctness.findings if finding.status in _OPEN_FINDING_STATUSES
    )
    unresolved_high = tuple(
        finding for finding in open_findings if finding.unresolved_high_severity
    )
    correctable = set(policy.correctable_semantic_candidate_ids)
    carried_observed = {
        finding.semantic_candidate_id
        for finding in open_findings
        if finding.semantic_candidate_id in correctable
    }
    high_observed = {
        finding.semantic_candidate_id
        for finding in unresolved_high
        if finding.severity is CorrectnessFindingSeverity.HIGH
        and finding.semantic_candidate_id in correctable
    }
    carried_ids = tuple(
        candidate_id
        for candidate_id in policy.correctable_semantic_candidate_ids
        if candidate_id in carried_observed
    )
    high_trigger_ids = tuple(
        candidate_id
        for candidate_id in policy.correctable_semantic_candidate_ids
        if candidate_id in high_observed
    )
    unresolved_high_by_id = {finding.finding_id: finding for finding in unresolved_high}
    promotion_blockers = tuple(
        unresolved_high_by_id[finding_id]
        for finding_id in policy.registered_promotion_blocker_finding_ids
    )
    registered_promotion_blockers = set(policy.registered_promotion_blocker_finding_ids)
    uncorrectable = tuple(
        finding
        for finding in unresolved_high
        if not (
            finding.severity is CorrectnessFindingSeverity.HIGH
            and finding.semantic_candidate_id in correctable
        )
        and finding.finding_id not in registered_promotion_blockers
    )
    return unresolved_high, carried_ids, high_trigger_ids, promotion_blockers, uncorrectable


def _retire_redesign_failure_reason(
    *,
    otherwise_eligible: bool,
    baseline_zero: bool,
    primary_effect_powered: bool,
    uncorrectable_safety_defect: bool,
) -> str:
    if otherwise_eligible and baseline_zero:
        return "otherwise_eligible_redesign_vetoed_by_zero_baseline_activity"
    if otherwise_eligible and not primary_effect_powered:
        return "otherwise_eligible_redesign_vetoed_by_unpowered_primary_effect"
    if otherwise_eligible and uncorrectable_safety_defect:
        return "otherwise_eligible_redesign_vetoed_by_uncorrectable_safety_defect"
    return "no_registered_redesign_path_satisfied_its_requirements"


def evaluate_historical_strategy_disposition(
    policy: HistoricalDispositionPolicy,
    evidence: HistoricalDispositionEvidence,
) -> HistoricalDispositionResult:
    """Apply the registered completion and freeze-redesign-retire hierarchy."""

    if not isinstance(policy, HistoricalDispositionPolicy):
        message = "policy must be a HistoricalDispositionPolicy"
        raise TypeError(message)
    if not isinstance(evidence, HistoricalDispositionEvidence):
        message = "evidence must be HistoricalDispositionEvidence"
        raise TypeError(message)
    checks = _normalize_evidence_checks(policy, evidence.checks)
    incomplete_checks = tuple(check for check in checks if not check.status.complete)
    baseline, redesign, power, correctness = _normalize_summaries(
        policy,
        evidence,
        require_all=not incomplete_checks,
    )

    if baseline is not None:
        ledger_check = next(
            check for check in checks if check.check_id == policy.baseline_ledger_check_id
        )
        if ledger_check.status.complete:
            expected_status = (
                HistoricalEvidenceStatus.VERIFIED_EMPTY
                if baseline.zero_trades_or_exposure
                else HistoricalEvidenceStatus.VERIFIED
            )
            if ledger_check.status is not expected_status:
                message = (
                    "baseline zero-trade/exposure state contradicts its registered "
                    "ledger evidence status"
                )
                raise ValueError(message)
    if (
        baseline is not None
        and power is not None
        and baseline.zero_trades_or_exposure != power.not_applicable_due_to_zero_trades
    ):
        message = "baseline and prospective-power zero-trade states contradict"
        raise ValueError(message)

    if incomplete_checks:
        incomplete_blockers = tuple(
            f"{check.status.value}_required_evidence:{check.check_id}"
            for check in incomplete_checks
        )
        return HistoricalDispositionResult(
            policy_id=policy.policy_id,
            baseline_arm_id=policy.baseline_arm_id,
            completion_state=HistoricalCompletionState.BLOCKED_INSUFFICIENT_EVIDENCE,
            economic_disposition=None,
            frozen_arm_id=None,
            evidence_checks=checks,
            baseline=baseline,
            redesign=redesign,
            power=power,
            correctness=correctness,
            eligible_quantitative_redesign_candidate_ids=(),
            eligible_semantic_redesign_candidate_ids=(),
            blocker_codes=incomplete_blockers,
            reason_codes=("no_economic_disposition_when_evidence_is_incomplete",),
        )

    # ``require_all=True`` above proves these values non-None to runtime, while
    # the explicit assertions keep the decision logic and type narrowing clear.
    assert baseline is not None
    assert redesign is not None
    assert power is not None
    assert correctness is not None

    baseline_by_gate = {check.gate_id: check.passed for check in baseline.gate_checks}
    all_baseline_quantitative_pass = all(baseline_by_gate.values())
    all_baseline_economic_pass = all(
        baseline_by_gate[gate_id] for gate_id in policy.baseline_economic_gate_ids
    )
    (
        unresolved_high,
        carried_correctable_semantic_ids,
        correctable_open_high_semantic_ids,
        promotion_blocker_findings,
        uncorrectable_open_findings,
    ) = _classify_correctness_findings(policy, correctness)
    eligible_quantitative = tuple(
        candidate.candidate_id for candidate in redesign.candidates if candidate.eligible
    )

    blockers: list[str] = []
    if baseline.zero_trades_or_exposure:
        blockers.append("baseline_zero_completed_trades_or_exposure")
    blockers.extend(
        f"baseline_quantitative_gate_failed:{check.gate_id}"
        for check in baseline.gate_checks
        if not check.passed
    )
    if not power.powered_within_maximum_duration:
        blockers.append("primary_effect_not_powered_within_maximum_duration")
    blockers.extend(
        f"unresolved_high_severity_correctness_finding:{finding.finding_id}"
        for finding in unresolved_high
    )

    freeze = (
        not baseline.zero_trades_or_exposure
        and all_baseline_quantitative_pass
        and power.powered_within_maximum_duration
        and not unresolved_high
    )
    semantic_redesign = bool(
        not baseline.zero_trades_or_exposure
        and all_baseline_economic_pass
        and (not promotion_blocker_findings or all_baseline_quantitative_pass)
        and power.powered_within_maximum_duration
        and correctable_open_high_semantic_ids
        and not uncorrectable_open_findings
    )
    quantitative_redesign = bool(
        not baseline.zero_trades_or_exposure
        and (not promotion_blocker_findings or all_baseline_quantitative_pass)
        and power.powered_within_maximum_duration
        and eligible_quantitative
        and not uncorrectable_open_findings
    )
    promotion_blocker_redesign = bool(
        not baseline.zero_trades_or_exposure
        and all_baseline_quantitative_pass
        and power.powered_within_maximum_duration
        and promotion_blocker_findings
        and not uncorrectable_open_findings
    )
    eligible_semantic_ids = (
        carried_correctable_semantic_ids
        if semantic_redesign or quantitative_redesign or promotion_blocker_redesign
        else ()
    )
    if freeze:
        disposition = HistoricalEconomicDisposition.FREEZE_FOR_PROSPECTIVE_SHADOW
        frozen_arm_id = policy.baseline_arm_id
        reason_codes: tuple[str, ...] = (
            "exact_baseline_passed_every_registered_quantitative_gate",
            "primary_effect_powered_within_maximum_duration",
            "zero_unresolved_high_severity_correctness_defects",
            "exact_baseline_frozen_for_prospective_shadow_research_only",
        )
    elif quantitative_redesign or semantic_redesign or promotion_blocker_redesign:
        disposition = HistoricalEconomicDisposition.REDESIGN
        frozen_arm_id = None
        reason_codes = (
            *(
                f"registered_quantitative_redesign_candidate_eligible:{candidate_id}"
                for candidate_id in eligible_quantitative
            ),
            *(
                (
                    "registered_correctable_high_semantic_defect:"
                    if candidate_id in correctable_open_high_semantic_ids
                    else "registered_correctable_semantic_remediation:"
                )
                + candidate_id
                for candidate_id in eligible_semantic_ids
            ),
            *(
                f"registered_correctness_only_promotion_blocker_requires_redesign:"
                f"{finding.finding_id}"
                for finding in promotion_blocker_findings
            ),
            "no_redesign_candidate_selected_for_deployment",
            "new_version_protocol_manifest_and_complete_rerun_required",
            *(
                (
                    "promotion_blocker_redesign_requires_new_attestation_protocol_manifest_"
                    "and_full_rerun",
                )
                if promotion_blocker_redesign
                else ()
            ),
        )
    else:
        disposition = HistoricalEconomicDisposition.RETIRE
        frozen_arm_id = None
        otherwise_eligible_redesign = bool(
            eligible_quantitative
            or (
                all_baseline_economic_pass
                and correctable_open_high_semantic_ids
                and not uncorrectable_open_findings
            )
            or bool(promotion_blocker_findings)
        )
        if (
            promotion_blocker_findings
            and not baseline.zero_trades_or_exposure
            and power.powered_within_maximum_duration
            and not all_baseline_quantitative_pass
        ):
            redesign_failure_reason = (
                "registered_promotion_blocker_redesign_vetoed_by_baseline_quantitative_failure"
            )
        else:
            redesign_failure_reason = _retire_redesign_failure_reason(
                otherwise_eligible=otherwise_eligible_redesign,
                baseline_zero=baseline.zero_trades_or_exposure,
                primary_effect_powered=power.powered_within_maximum_duration,
                uncorrectable_safety_defect=bool(uncorrectable_open_findings),
            )
        reason_codes = (
            "baseline_did_not_satisfy_freeze_requirements",
            redesign_failure_reason,
            *(
                "unresolved_critical_or_uncorrectable_high_correctness_finding:"
                f"{finding.finding_id}"
                for finding in uncorrectable_open_findings
            ),
            "historical_research_rejection_does_not_authorize_future_trading",
        )

    return HistoricalDispositionResult(
        policy_id=policy.policy_id,
        baseline_arm_id=policy.baseline_arm_id,
        completion_state=HistoricalCompletionState.COMPLETE,
        economic_disposition=disposition,
        frozen_arm_id=frozen_arm_id,
        evidence_checks=checks,
        baseline=baseline,
        redesign=redesign,
        power=power,
        correctness=correctness,
        eligible_quantitative_redesign_candidate_ids=eligible_quantitative,
        eligible_semantic_redesign_candidate_ids=eligible_semantic_ids,
        blocker_codes=tuple(blockers),
        reason_codes=reason_codes,
    )


__all__ = [
    "CorrectnessDispositionSummary",
    "FixedBaselineDispositionSummary",
    "HistoricalCompletionState",
    "HistoricalDispositionEvidence",
    "HistoricalDispositionPolicy",
    "HistoricalDispositionResult",
    "HistoricalEconomicDisposition",
    "HistoricalEvidenceCheck",
    "HistoricalEvidenceKind",
    "HistoricalEvidenceRequirement",
    "HistoricalEvidenceSource",
    "HistoricalEvidenceStatus",
    "HistoricalGateCheck",
    "ProspectivePowerDispositionSummary",
    "RegisteredRedesignCandidateSummary",
    "RegisteredRedesignDispositionSummary",
    "SemanticCorrectnessFindingSummary",
    "evaluate_historical_strategy_disposition",
]
