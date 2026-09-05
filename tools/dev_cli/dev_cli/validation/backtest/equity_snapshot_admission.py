"""Scope-aware admission for immutable equity snapshots.

The historical snapshot's global ``complete`` flag remains the unregistered
and confirmatory portfolio-run gate. Factor research and a preregistered
diagnostic portfolio trial have a narrower exception: they may defer explicit
membership-authority, permanent-identity, or EODHD split-basis authority while
every price, split-coordinate reconstruction, action, session, benchmark, and
per-security disposition gate remains closed. This module owns that exception
so factor materialization, campaign verification, and diagnostic execution
cannot interpret it differently.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from dev_cli.validation.evidence import evidence_sha256, sha256_digest

SNAPSHOT_FACTOR_ADMISSION_SCHEMA: Final = "vynmatrix.equity-snapshot-factor-admission.v2"
SNAPSHOT_FACTOR_ADMISSION_DECISION_SCHEMA: Final = (
    "vynmatrix.equity-snapshot-factor-admission-decision.v2"
)
SNAPSHOT_FACTOR_ADMISSION_POLICY_VERSION: Final = (
    "strict-diagnostic-membership-identity-and-split-basis-v2"
)

_ACCEPTED_SECURITY_STATUSES: Final = frozenset({"aliased", "excluded", "resolved"})
# Owner-registered vendor-gap classes. Each is a real, documented absence in
# the provider's history; the panel never ranks such a security, so excluding
# it cannot change any factor value.
_REGISTERED_PANEL_EXCLUSION_REASONS: Final = frozenset(
    {
        "corporate_actions_incomplete",
        "dividend_adjustment_anchor_missing",
        "provider_returned_no_history",
        "registered_price_history_incomplete",
        "session_coverage_below_policy",
    }
)
_STRUCTURAL_WARMUP_OWNER: Final = "lib_strategy.equity_market_factors.StructuralBreadthExclusion"
# A structural warm-up prefix is deferred to the panel builder, which proves
# the exact bounded window before excluding it.
_STRUCTURAL_WARMUP_DEFERRAL_REASONS: Final = {
    "structural_identity_partition_warmup_candidate": (
        "registered_price_history_structural_identity_partition_warmup"
    ),
    "structural_listing_warmup_candidate": ("registered_price_history_structural_listing_warmup"),
}
_REQUIRED_ARTIFACT_CARDINALITIES: Final = {
    "acquisition_evidence": 1,
    "benchmark_price_bars": 1,
    "benchmark_total_return_bars": 1,
    "benchmark_total_return_dividends": 1,
    "benchmark_total_return_splits": 1,
    "benchmark_volatility_bars": 1,
    "membership": 1,
    "official_sessions": 1,
}
_REQUIRED_SECURITY_ARTIFACT_ROLES: Final = (
    "security_bars",
    "security_dividends",
    "security_splits",
)
_ALLOWED_GLOBAL_INCOMPLETENESS: Final = (
    "membership_authority_complete",
    "permanent_identity_complete",
    "split_adjustment_basis_complete",
)
_CONFIRMATORY_IDENTITY_SCHEMES: Final = frozenset(
    {"explicit_security_id", "source_backed_permanent_security_id"}
)


class SnapshotFactorClaimScope(StrEnum):
    """Claim scopes admitted by a historical snapshot."""

    DIAGNOSTIC = "diagnostic"
    CONFIRMATORY = "confirmatory"


class SnapshotFactorAdmissionError(ValueError):
    """A snapshot cannot satisfy the requested factor claim scope."""


@dataclass(frozen=True, slots=True)
class SnapshotFactorAdmissionPolicy:
    """Recomputed admission policy bound to exact snapshot manifest content."""

    snapshot_complete: bool
    declared_panel_materialization_eligible: bool
    explicit_global_incompleteness: tuple[str, ...]
    gates: Mapping[str, bool]
    diagnostic_eligible: bool
    confirmatory_eligible: bool

    @property
    def policy_sha256(self) -> str:
        """Return the digest of the canonical policy body."""

        return evidence_sha256(self._body())

    @property
    def maximum_claim_scope(self) -> SnapshotFactorClaimScope:
        """Return the strongest claim scope this snapshot can support."""

        if self.confirmatory_eligible:
            return SnapshotFactorClaimScope.CONFIRMATORY
        if self.diagnostic_eligible:
            return SnapshotFactorClaimScope.DIAGNOSTIC
        message = "historical snapshot is not eligible for factor materialization"
        raise SnapshotFactorAdmissionError(message)

    def _body(self) -> dict[str, object]:
        return {
            "accepted_diagnostic_global_incompleteness": list(_ALLOWED_GLOBAL_INCOMPLETENESS),
            "confirmatory_eligible": self.confirmatory_eligible,
            "declared_panel_materialization_eligible": (
                self.declared_panel_materialization_eligible
            ),
            "diagnostic_eligible": self.diagnostic_eligible,
            "explicit_global_incompleteness": list(self.explicit_global_incompleteness),
            "gates": dict(sorted(self.gates.items())),
            "policy_version": SNAPSHOT_FACTOR_ADMISSION_POLICY_VERSION,
            "schema": SNAPSHOT_FACTOR_ADMISSION_SCHEMA,
            "snapshot_complete": self.snapshot_complete,
        }

    def to_manifest(self) -> dict[str, object]:
        """Serialize the canonical policy and its independently checked digest."""

        return {**self._body(), "policy_sha256": self.policy_sha256}

    @classmethod
    def from_manifest(cls, value: object) -> SnapshotFactorAdmissionPolicy:
        """Load and re-hash one canonical admission policy."""

        payload = _mapping(value)
        expected_keys = frozenset(
            {
                "accepted_diagnostic_global_incompleteness",
                "confirmatory_eligible",
                "declared_panel_materialization_eligible",
                "diagnostic_eligible",
                "explicit_global_incompleteness",
                "gates",
                "policy_sha256",
                "policy_version",
                "schema",
                "snapshot_complete",
            }
        )
        if payload is None or set(payload) != expected_keys:
            message = "snapshot factor admission policy keys differ"
            raise SnapshotFactorAdmissionError(message)
        raw_gates = _mapping(payload.get("gates"))
        if not raw_gates or any(
            not isinstance(key, str) or not isinstance(passed, bool)
            for key, passed in raw_gates.items()
        ):
            message = "snapshot factor admission gates are invalid"
            raise SnapshotFactorAdmissionError(message)
        accepted = payload.get("accepted_diagnostic_global_incompleteness")
        explicit = payload.get("explicit_global_incompleteness")
        if accepted != list(_ALLOWED_GLOBAL_INCOMPLETENESS) or not isinstance(explicit, list):
            message = "snapshot factor admission global-incompleteness policy differs"
            raise SnapshotFactorAdmissionError(message)
        if any(item not in _ALLOWED_GLOBAL_INCOMPLETENESS for item in explicit):
            message = "snapshot factor admission names unsupported global incompleteness"
            raise SnapshotFactorAdmissionError(message)
        canonical_explicit = [item for item in _ALLOWED_GLOBAL_INCOMPLETENESS if item in explicit]
        if explicit != canonical_explicit:
            message = "snapshot factor admission global incompleteness is noncanonical"
            raise SnapshotFactorAdmissionError(message)
        boolean_fields = (
            "confirmatory_eligible",
            "declared_panel_materialization_eligible",
            "diagnostic_eligible",
            "snapshot_complete",
        )
        if any(not isinstance(payload.get(field), bool) for field in boolean_fields):
            message = "snapshot factor admission eligibility fields must be boolean"
            raise SnapshotFactorAdmissionError(message)
        policy = cls(
            snapshot_complete=bool(payload["snapshot_complete"]),
            declared_panel_materialization_eligible=bool(
                payload["declared_panel_materialization_eligible"]
            ),
            explicit_global_incompleteness=tuple(str(item) for item in explicit),
            gates={str(key): bool(passed) for key, passed in raw_gates.items()},
            diagnostic_eligible=bool(payload["diagnostic_eligible"]),
            confirmatory_eligible=bool(payload["confirmatory_eligible"]),
        )
        if policy.diagnostic_eligible != all(policy.gates.values()):
            message = "snapshot factor diagnostic eligibility differs from its gates"
            raise SnapshotFactorAdmissionError(message)
        if policy.confirmatory_eligible and (
            not policy.diagnostic_eligible
            or not policy.snapshot_complete
            or bool(policy.explicit_global_incompleteness)
        ):
            message = "snapshot factor confirmatory eligibility is internally inconsistent"
            raise SnapshotFactorAdmissionError(message)
        if (
            payload.get("schema") != SNAPSHOT_FACTOR_ADMISSION_SCHEMA
            or payload.get("policy_version") != SNAPSHOT_FACTOR_ADMISSION_POLICY_VERSION
            or payload != policy.to_manifest()
        ):
            message = "snapshot factor admission policy content or digest differs"
            raise SnapshotFactorAdmissionError(message)
        return policy

    def decision(self, scope: SnapshotFactorClaimScope) -> dict[str, object]:
        """Return a scope-specific lineage decision or fail closed."""

        eligible = (
            self.diagnostic_eligible
            if scope is SnapshotFactorClaimScope.DIAGNOSTIC
            else self.confirmatory_eligible
        )
        if not eligible:
            failed = sorted(name for name, passed in self.gates.items() if not passed)
            message = (
                f"historical snapshot is not {scope.value}-eligible for factor "
                f"materialization: failed_gates={failed}, "
                f"explicit_global_incompleteness={list(self.explicit_global_incompleteness)}"
            )
            raise SnapshotFactorAdmissionError(message)
        return {
            "accepted_global_incompleteness": (
                list(self.explicit_global_incompleteness)
                if scope is SnapshotFactorClaimScope.DIAGNOSTIC
                else []
            ),
            "claim_scope": scope.value,
            "eligible": True,
            "policy_sha256": self.policy_sha256,
            "schema": SNAPSHOT_FACTOR_ADMISSION_DECISION_SCHEMA,
            "snapshot_complete": self.snapshot_complete,
        }


@dataclass(frozen=True, slots=True)
class SnapshotFactorAdmissionLineage:
    """Exact snapshot policy and scope decision carried by a factor manifest."""

    historical_snapshot_manifest_sha256: str
    policy: SnapshotFactorAdmissionPolicy
    claim_scope: SnapshotFactorClaimScope

    def to_manifest(self) -> dict[str, object]:
        """Serialize one self-verifying snapshot admission lineage binding."""

        return {
            "decision": self.policy.decision(self.claim_scope),
            "historical_snapshot_manifest_sha256": (self.historical_snapshot_manifest_sha256),
            "policy": self.policy.to_manifest(),
        }

    @classmethod
    def from_manifest(
        cls,
        value: object,
        *,
        expected_claim_scope: SnapshotFactorClaimScope,
    ) -> SnapshotFactorAdmissionLineage:
        """Load and verify one factor-to-snapshot admission binding."""

        payload = _mapping(value)
        if payload is None or set(payload) != {
            "decision",
            "historical_snapshot_manifest_sha256",
            "policy",
        }:
            message = "factor snapshot-admission lineage keys differ"
            raise SnapshotFactorAdmissionError(message)
        try:
            snapshot_sha256 = sha256_digest(
                payload.get("historical_snapshot_manifest_sha256"),
                field="historical_snapshot_manifest_sha256",
            )
        except (TypeError, ValueError) as exc:
            raise SnapshotFactorAdmissionError(str(exc)) from exc
        policy = SnapshotFactorAdmissionPolicy.from_manifest(payload.get("policy"))
        lineage = cls(
            historical_snapshot_manifest_sha256=snapshot_sha256,
            policy=policy,
            claim_scope=expected_claim_scope,
        )
        if payload != lineage.to_manifest():
            message = "factor snapshot-admission decision differs from its policy"
            raise SnapshotFactorAdmissionError(message)
        return lineage


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _artifact_role_counts(manifest: Mapping[str, object]) -> Counter[str]:
    raw = manifest.get("artifacts")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return Counter()
    roles: list[str] = []
    for value in raw:
        artifact = _mapping(value)
        role = artifact.get("role") if artifact is not None else None
        if isinstance(role, str):
            roles.append(role)
    return Counter(roles)


def _component_requests_actions_complete(disposition: Mapping[str, object]) -> bool:
    if disposition.get("requested_from") is None:
        return disposition.get("status") == "excluded"
    raw = disposition.get("component_requests")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return False
    requests = [item for value in raw if (item := _mapping(value)) is not None]
    actions = [
        item
        for item in requests
        if isinstance(item.get("component"), str)
        and str(item["component"]).endswith(("_splits", "_dividends"))
    ]
    components = {str(item["component"]) for item in actions}
    return {"security_splits", "security_dividends"}.issubset(components) and all(
        item.get("status") == "verified" for item in actions
    )


def disposition_registered_panel_exclusion(disposition: Mapping[str, object]) -> bool:
    """True when a security is a documented, owner-registered panel exclusion.

    These securities carry real vendor evidence gaps that the owner accepted
    for diagnostic research: the panel never ranks them, so their absence
    cannot influence any factor value.
    """

    return (
        disposition.get("status") in {"partial", "unavailable"}
        and disposition.get("reason") in _REGISTERED_PANEL_EXCLUSION_REASONS
    )


def _disposition_structural_warmup_deferral(disposition: Mapping[str, object]) -> bool:
    """True when the panel builder owns proving this security's warm-up prefix.

    The snapshot deliberately marks a deferred security ineligible: it is the
    panel builder that verifies the exact rolling window, the immutable
    post-identity prices, and any reviewed identity edge before excluding the
    prefix. Admitting the deferral here records that ownership transfer.
    """

    deferred = _mapping(disposition.get("deferred_panel_validation"))
    if deferred is None or disposition.get("status") != "partial":
        return False
    expected_reason = _STRUCTURAL_WARMUP_DEFERRAL_REASONS.get(str(deferred.get("kind")))
    return (
        expected_reason is not None
        and disposition.get("reason") == expected_reason
        and deferred.get("validation_owner") == _STRUCTURAL_WARMUP_OWNER
    )


def _disposition_panel_eligible(disposition: Mapping[str, object]) -> bool:
    if disposition_registered_panel_exclusion(disposition):
        return True
    if _disposition_structural_warmup_deferral(disposition):
        return True
    if disposition.get("panel_materialization_eligible") is not True:
        return False
    return disposition.get("status") in _ACCEPTED_SECURITY_STATUSES


def _required_bool_or_none(value: object, *, field: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    message = f"historical snapshot {field} must be boolean or null"
    raise SnapshotFactorAdmissionError(message)


def evaluate_snapshot_factor_admission(
    manifest: Mapping[str, object],
) -> SnapshotFactorAdmissionPolicy:
    """Recompute strict diagnostic and confirmatory admission from one manifest."""

    complete = manifest.get("complete")
    panel_eligible = manifest.get("panel_materialization_eligible")
    if not isinstance(complete, bool) or not isinstance(panel_eligible, bool):
        message = "historical snapshot completeness declarations must be boolean"
        raise SnapshotFactorAdmissionError(message)
    membership = _mapping(manifest.get("membership"))
    calendar = _mapping(manifest.get("calendar"))
    benchmarks = _mapping(manifest.get("benchmarks"))
    adjustment_policy = _mapping(manifest.get("adjustment_policy"))
    if membership is None or calendar is None or benchmarks is None or adjustment_policy is None:
        message = "historical snapshot admission context is missing"
        raise SnapshotFactorAdmissionError(message)
    split_adjustment_basis_complete = adjustment_policy.get("split_adjustment_basis_complete")
    if not isinstance(split_adjustment_basis_complete, bool):
        message = (
            "historical snapshot adjustment_policy.split_adjustment_basis_complete must be boolean"
        )
        raise SnapshotFactorAdmissionError(message)
    split_coordinate_reconstruction_complete = adjustment_policy.get(
        "split_coordinate_reconstruction_complete"
    )
    if not isinstance(split_coordinate_reconstruction_complete, bool):
        message = (
            "historical snapshot adjustment_policy."
            "split_coordinate_reconstruction_complete must be boolean"
        )
        raise SnapshotFactorAdmissionError(message)
    permanent_identity = _required_bool_or_none(
        membership.get("permanent_identity_complete"),
        field="membership.permanent_identity_complete",
    )
    membership_authority = _required_bool_or_none(
        membership.get("membership_authority_complete"),
        field="membership.membership_authority_complete",
    )
    explicit_global_incompleteness = tuple(
        field
        for field, value in (
            ("membership_authority_complete", membership_authority),
            ("permanent_identity_complete", permanent_identity),
            ("split_adjustment_basis_complete", split_adjustment_basis_complete),
        )
        if value is False
    )

    raw_dispositions = manifest.get("dispositions")
    dispositions = (
        [item for value in raw_dispositions if (item := _mapping(value)) is not None]
        if isinstance(raw_dispositions, Sequence) and not isinstance(raw_dispositions, (str, bytes))
        else []
    )
    dispositions_well_formed = bool(dispositions) and len(dispositions) == len(
        raw_dispositions if isinstance(raw_dispositions, Sequence) else ()
    )
    securities_complete = dispositions_well_formed and all(
        item.get("status") in _ACCEPTED_SECURITY_STATUSES for item in dispositions
    )
    panel_dispositions_complete = dispositions_well_formed and all(
        _disposition_panel_eligible(item) for item in dispositions
    )
    security_actions_complete = dispositions_well_formed and all(
        _component_requests_actions_complete(item) for item in dispositions
    )

    benchmark_statuses = tuple(
        (_mapping(benchmarks.get(role)) or {}).get("status")
        for role in ("price", "total_return", "volatility")
    )
    benchmark_components_complete = benchmark_statuses == (
        "verified",
        "verified",
        "verified",
    )
    total_return = _mapping(benchmarks.get("total_return"))
    benchmark_adjustment_anchors_complete = (
        total_return is not None and total_return.get("adjustment_anchor_complete") is True
    )
    role_counts = _artifact_role_counts(manifest)
    benchmark_actions_complete = all(
        role_counts[role] == 1
        for role in (
            "benchmark_total_return_dividends",
            "benchmark_total_return_splits",
        )
    )
    required_artifacts_complete = all(
        role_counts[role] == expected for role, expected in _REQUIRED_ARTIFACT_CARDINALITIES.items()
    ) and all(role_counts[role] >= 1 for role in _REQUIRED_SECURITY_ARTIFACT_ROLES)
    calendar_coverage_complete = calendar.get("coverage_complete") is True

    identity_authority_declarations_explicit = isinstance(permanent_identity, bool) and isinstance(
        membership_authority, bool
    )
    identity_authority_complete = permanent_identity is True and membership_authority is True
    expected_complete = (
        securities_complete
        and benchmark_components_complete
        and benchmark_actions_complete
        and benchmark_adjustment_anchors_complete
        and split_adjustment_basis_complete
        and identity_authority_complete
    )
    expected_legacy_panel_eligibility = (
        panel_dispositions_complete
        and benchmark_components_complete
        and benchmark_actions_complete
        and benchmark_adjustment_anchors_complete
        and split_coordinate_reconstruction_complete
        and identity_authority_complete
    )
    complete_declaration_consistent = complete is expected_complete
    panel_declaration_consistent = panel_eligible is expected_legacy_panel_eligibility

    gates = {
        "benchmark_adjustment_anchors_complete": benchmark_adjustment_anchors_complete,
        "benchmark_action_components_complete": benchmark_actions_complete,
        "benchmark_components_complete": benchmark_components_complete,
        "calendar_coverage_complete": calendar_coverage_complete,
        "complete_declaration_consistent": complete_declaration_consistent,
        "identity_authority_declarations_explicit": (identity_authority_declarations_explicit),
        "panel_declaration_consistent": panel_declaration_consistent,
        "per_security_action_components_complete": security_actions_complete,
        "per_security_panel_dispositions_complete": panel_dispositions_complete,
        "required_artifact_roles_complete": required_artifacts_complete,
        "split_coordinate_reconstruction_complete": (split_coordinate_reconstruction_complete),
    }
    diagnostic_eligible = all(gates.values())
    source_authority = _mapping(membership.get("source_authority"))
    confirmatory_eligible = (
        diagnostic_eligible
        and complete
        and identity_authority_complete
        and not explicit_global_incompleteness
        and securities_complete
        and calendar.get("confirmatory_eligible") is True
        and source_authority is not None
        and source_authority.get("confirmatory_eligible") is True
        and membership.get("identity_scheme") in _CONFIRMATORY_IDENTITY_SCHEMES
    )
    policy = SnapshotFactorAdmissionPolicy(
        snapshot_complete=complete,
        declared_panel_materialization_eligible=panel_eligible,
        explicit_global_incompleteness=explicit_global_incompleteness,
        gates=gates,
        diagnostic_eligible=diagnostic_eligible,
        confirmatory_eligible=confirmatory_eligible,
    )
    declared_policy = manifest.get("factor_materialization_admission")
    if declared_policy is not None and declared_policy != policy.to_manifest():
        message = "historical snapshot factor admission policy differs from manifest content"
        raise SnapshotFactorAdmissionError(message)
    return policy


__all__ = [
    "SNAPSHOT_FACTOR_ADMISSION_DECISION_SCHEMA",
    "SNAPSHOT_FACTOR_ADMISSION_POLICY_VERSION",
    "SNAPSHOT_FACTOR_ADMISSION_SCHEMA",
    "SnapshotFactorAdmissionError",
    "SnapshotFactorAdmissionLineage",
    "SnapshotFactorAdmissionPolicy",
    "SnapshotFactorClaimScope",
    "evaluate_snapshot_factor_admission",
]
