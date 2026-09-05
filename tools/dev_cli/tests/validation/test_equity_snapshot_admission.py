"""Scope-aware historical-snapshot admission tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from dev_cli.validation.backtest.equity_snapshot_admission import (
    SnapshotFactorAdmissionError,
    SnapshotFactorAdmissionPolicy,
    SnapshotFactorClaimScope,
    evaluate_snapshot_factor_admission,
)


def _diagnostic_manifest() -> dict[str, Any]:
    roles = (
        "acquisition_evidence",
        "benchmark_price_bars",
        "benchmark_total_return_bars",
        "benchmark_total_return_dividends",
        "benchmark_total_return_splits",
        "benchmark_volatility_bars",
        "membership",
        "official_sessions",
        "security_bars",
        "security_dividends",
        "security_splits",
    )
    return {
        "adjustment_policy": {
            "split_adjustment_basis_complete": True,
            "split_coordinate_reconstruction_complete": True,
        },
        "artifacts": [{"role": role} for role in roles],
        "benchmarks": {
            "price": {"status": "verified"},
            "total_return": {
                "adjustment_anchor_complete": True,
                "status": "verified",
            },
            "volatility": {"status": "verified"},
        },
        "calendar": {
            "confirmatory_eligible": True,
            "coverage_complete": True,
        },
        "complete": False,
        "dispositions": [
            {
                "component_requests": [
                    {"component": "security_splits", "status": "verified"},
                    {"component": "security_dividends", "status": "verified"},
                ],
                "panel_materialization_eligible": True,
                "requested_from": "2020-01-01",
                "status": "resolved",
            }
        ],
        "membership": {
            "identity_scheme": "mixed_explicit_or_derived",
            "membership_authority_complete": False,
            "permanent_identity_complete": False,
            "source_authority": {"confirmatory_eligible": True},
        },
        "panel_materialization_eligible": False,
    }


def test_only_explicit_identity_and_membership_incompleteness_is_diagnostic() -> None:
    policy = evaluate_snapshot_factor_admission(_diagnostic_manifest())

    assert policy.maximum_claim_scope is SnapshotFactorClaimScope.DIAGNOSTIC
    assert policy.explicit_global_incompleteness == (
        "membership_authority_complete",
        "permanent_identity_complete",
    )
    assert policy.decision(SnapshotFactorClaimScope.DIAGNOSTIC)[
        "accepted_global_incompleteness"
    ] == ["membership_authority_complete", "permanent_identity_complete"]
    with pytest.raises(SnapshotFactorAdmissionError, match="not confirmatory-eligible"):
        policy.decision(SnapshotFactorClaimScope.CONFIRMATORY)


@pytest.mark.parametrize(
    "field",
    ["membership_authority_complete", "permanent_identity_complete"],
)
def test_unknown_identity_or_membership_authority_is_not_admitted(field: str) -> None:
    manifest = _diagnostic_manifest()
    manifest["membership"][field] = None

    policy = evaluate_snapshot_factor_admission(manifest)

    assert policy.diagnostic_eligible is False
    assert policy.confirmatory_eligible is False
    assert policy.gates["identity_authority_declarations_explicit"] is False
    with pytest.raises(
        SnapshotFactorAdmissionError,
        match="identity_authority_declarations_explicit",
    ):
        policy.decision(SnapshotFactorClaimScope.DIAGNOSTIC)


@pytest.mark.parametrize(
    ("path", "value", "failed_gate"),
    [
        (("calendar", "coverage_complete"), False, "calendar_coverage_complete"),
        (
            ("benchmarks", "volatility", "status"),
            "failed",
            "benchmark_components_complete",
        ),
        (
            ("dispositions", 0, "component_requests", 0, "status"),
            "failed",
            "per_security_action_components_complete",
        ),
        (
            ("dispositions", 0, "panel_materialization_eligible"),
            False,
            "per_security_panel_dispositions_complete",
        ),
        (
            ("adjustment_policy", "split_coordinate_reconstruction_complete"),
            False,
            "split_coordinate_reconstruction_complete",
        ),
    ],
)
def test_diagnostic_exception_does_not_weaken_strict_gates(
    path: tuple[str | int, ...],
    value: object,
    failed_gate: str,
) -> None:
    manifest = deepcopy(_diagnostic_manifest())
    target: Any = manifest
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = value

    policy = evaluate_snapshot_factor_admission(manifest)

    assert policy.diagnostic_eligible is False
    assert policy.gates[failed_gate] is False
    with pytest.raises(SnapshotFactorAdmissionError, match=failed_gate):
        policy.decision(SnapshotFactorClaimScope.DIAGNOSTIC)


@pytest.mark.parametrize(
    ("removed_role", "failed_gate"),
    [
        ("security_dividends", "required_artifact_roles_complete"),
        (
            "benchmark_total_return_splits",
            "benchmark_action_components_complete",
        ),
    ],
)
def test_diagnostic_exception_requires_action_artifacts(
    removed_role: str,
    failed_gate: str,
) -> None:
    manifest = deepcopy(_diagnostic_manifest())
    manifest["artifacts"] = [
        artifact for artifact in manifest["artifacts"] if artifact["role"] != removed_role
    ]

    policy = evaluate_snapshot_factor_admission(manifest)

    assert policy.diagnostic_eligible is False
    assert policy.gates[failed_gate] is False


def test_embedded_admission_policy_is_rehashed_and_content_bound() -> None:
    manifest = _diagnostic_manifest()
    declared = evaluate_snapshot_factor_admission(manifest).to_manifest()
    manifest["factor_materialization_admission"] = declared

    assert SnapshotFactorAdmissionPolicy.from_manifest(declared).to_manifest() == declared
    assert evaluate_snapshot_factor_admission(manifest).to_manifest() == declared

    tampered = deepcopy(declared)
    tampered["policy_sha256"] = "0" * 64
    with pytest.raises(SnapshotFactorAdmissionError, match="content or digest differs"):
        SnapshotFactorAdmissionPolicy.from_manifest(tampered)

    inconsistent = deepcopy(declared)
    inconsistent["diagnostic_eligible"] = False
    with pytest.raises(SnapshotFactorAdmissionError, match="eligibility differs"):
        SnapshotFactorAdmissionPolicy.from_manifest(inconsistent)
