"""Registered correctness-contract and immutable-artifact behavior."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dev_cli.validation.correctness import (
    CorrectnessContentClassification,
    CorrectnessFileBinding,
    CorrectnessFileClass,
    CorrectnessFindingClass,
    CorrectnessFindingSeverity,
    CorrectnessFindingStatus,
    CorrectnessReviewer,
    CorrectnessSemanticFinding,
    build_strategy_correctness_attestation,
    summarize_strategy_correctness_attestation,
    verify_strategy_correctness_attestation,
)
from dev_cli.validation.correctness_registration import (
    build_registered_correctness_attestation,
    create_registered_correctness_attestation,
    resolve_correctness_attestation_output,
)
from dev_cli.validation.evidence import canonical_json_bytes


def _write_generic_correctness_contract(
    tmp_path: Path,
) -> tuple[Path, str, tuple[str, ...], dict[str, object]]:
    repo_root = tmp_path / "repo"
    strategy_name = "GenericMomentum"
    strategy_dir = repo_root / "strategies" / "indicator" / strategy_name
    strategy_dir.mkdir(parents=True)
    fixture = repo_root / "tests" / "fixtures" / "generic.json"
    fixture.parent.mkdir(parents=True)
    package_source = tmp_path / "authoritative_package.py"
    strategy_source = strategy_dir / "core.py"
    strategy_source.write_text("STRATEGY = 'generic'\n", encoding="utf-8")
    package_source.write_text("REFERENCE = 'generic'\n", encoding="utf-8")
    fixture.write_text('{"close":[1,2,3]}\n', encoding="utf-8")

    file_specs = (
        (
            "strategy.current",
            CorrectnessFileClass.STRATEGY_SOURCE,
            CorrectnessContentClassification.NON_SENSITIVE_SOURCE,
            strategy_source,
            "strategies/indicator/GenericMomentum/core.py",
            "repository_relative",
        ),
        (
            "package.reference",
            CorrectnessFileClass.PACKAGE_SOURCE,
            CorrectnessContentClassification.NON_SENSITIVE_SOURCE,
            package_source,
            "packages/reference.py",
            "explicit_absolute_external",
        ),
        (
            "fixture.reference",
            CorrectnessFileClass.FIXTURE,
            CorrectnessContentClassification.NON_SENSITIVE_FIXTURE,
            fixture,
            "tests/fixtures/generic.json",
            "repository_relative",
        ),
    )
    bindings = tuple(
        CorrectnessFileBinding(
            binding_id=binding_id,
            file_class=file_class,
            content_classification=classification,
            path=path,
            location=location,
        )
        for binding_id, file_class, classification, path, location, _scope in file_specs
    )
    finding = CorrectnessSemanticFinding(
        finding_id="S1.reference_behavior",
        authority="Exact reviewed reference bytes.",
        current_behavior="The current implementation uses the reviewed behavior.",
        authoritative_behavior="The reference implementation defines the behavior.",
        severity=CorrectnessFindingSeverity.HIGH,
        status=CorrectnessFindingStatus.VERIFIED_EQUIVALENT,
        finding_class=CorrectnessFindingClass.STRATEGY_SEMANTIC,
        remediation="Repeat the review after any bound byte changes.",
        evidence_file_ids=tuple(spec[0] for spec in file_specs),
    )
    reviewer = CorrectnessReviewer(
        identity="owner review plus source reverification",
        role="Automated pre-outcome review; not independent human validation.",
        independent=False,
        reviewed_at_utc=datetime(2026, 7, 22, 9, 0, tzinfo=UTC),
    )
    payload = build_strategy_correctness_attestation(
        strategy_id="generic_momentum_v1",
        strategy_version="1.0.0",
        files=bindings,
        findings=(finding,),
        reviewer=reviewer,
    )
    protocol_files = sorted(
        [
            {
                "id": binding_id,
                "class": file_class.value,
                "content_classification": classification.value,
                "location": location,
                "path_scope": scope,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "byte_count": path.stat().st_size,
            }
            for binding_id, file_class, classification, path, location, scope in file_specs
        ],
        key=lambda row: str(row["id"]),
    )
    protocol_finding = {
        "id": finding.finding_id,
        "authority": finding.authority,
        "current_behavior": finding.current_behavior,
        "authoritative_behavior": finding.authoritative_behavior,
        "severity": finding.severity.value,
        "status": finding.status.value,
        "finding_class": finding.finding_class.value,
        "remediation": finding.remediation,
        "evidence_file_ids": sorted(finding.evidence_file_ids),
    }
    protocol = {
        "correctness_attestation": {
            "required": True,
            "schema_id": "vynmatrix.strategy-correctness-contract",
            "schema_version": 1,
            "attestation_schema_id": "vynmatrix.strategy-correctness-attestation",
            "attestation_schema_version": 1,
            "subject": {
                "strategy_id": "generic_momentum_v1",
                "strategy_version": "1.0.0",
            },
            "files": protocol_files,
            "findings": [protocol_finding],
            "reviewer": {
                "identity": reviewer.identity,
                "role": reviewer.role,
                "independent": reviewer.independent,
                "reviewed_at_utc": "2026-07-22T09:00:00Z",
            },
            "exact_attestation": {
                "status": "frozen_pre_outcome_source_reverification",
                "attestation_sha256": payload["attestation_sha256"],
                "summary": summarize_strategy_correctness_attestation(payload),
            },
        }
    }
    (strategy_dir / "validation_protocol.json").write_text(
        json.dumps(protocol),
        encoding="utf-8",
    )
    file_values = tuple(
        f"{binding_id}={path if scope == 'explicit_absolute_external' else location}"
        for binding_id, _file_class, _classification, path, location, scope in file_specs
    )
    return repo_root, strategy_name, file_values, payload


def test_registered_correctness_artifact_is_immutable_and_reverified(tmp_path: Path) -> None:
    repo_root, strategy_name, file_values, expected = _write_generic_correctness_contract(tmp_path)

    first, artifact = create_registered_correctness_attestation(
        repo_root=repo_root,
        strategy_name=strategy_name,
        file_values=file_values,
        output=None,
    )
    second, repeated_artifact = create_registered_correctness_attestation(
        repo_root=repo_root,
        strategy_name=strategy_name,
        file_values=file_values,
        output=None,
    )

    assert first == second == expected
    assert repeated_artifact == artifact
    assert artifact.read_bytes() == canonical_json_bytes(expected)
    assert artifact.stat().st_mode & 0o222 == 0
    stored = json.loads(artifact.read_text(encoding="utf-8"))
    verify_strategy_correctness_attestation(stored)
    assert all(row["content_base64"] for row in stored["files"])

    artifact.chmod(0o644)
    artifact.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="immutable artifact already exists"):
        create_registered_correctness_attestation(
            repo_root=repo_root,
            strategy_name=strategy_name,
            file_values=file_values,
            output=None,
        )


def test_registered_correctness_rejects_missing_extra_and_escaped_bindings(
    tmp_path: Path,
) -> None:
    repo_root, _strategy_name, file_values, _expected = _write_generic_correctness_contract(
        tmp_path
    )
    protocol_path = (
        repo_root / "strategies" / "indicator" / "GenericMomentum" / "validation_protocol.json"
    )
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="missing correctness file binding ID"):
        build_registered_correctness_attestation(
            protocol,
            file_values=file_values[:-1],
            repo_root=repo_root,
        )
    with pytest.raises(ValueError, match="unknown correctness file binding IDs"):
        build_registered_correctness_attestation(
            protocol,
            file_values=(*file_values, f"unknown={tmp_path / 'authoritative_package.py'}"),
            repo_root=repo_root,
        )

    outside = tmp_path / "escaped.py"
    outside.write_text("ESCAPED = True\n", encoding="utf-8")
    escaped_values = tuple(
        (f"strategy.current={outside}" if value.startswith("strategy.current=") else value)
        for value in file_values
    )
    with pytest.raises(ValueError, match="escapes its required root"):
        build_registered_correctness_attestation(
            protocol,
            file_values=escaped_values,
            repo_root=repo_root,
        )


def test_correctness_output_requires_content_addressed_name_and_artifact_root(
    tmp_path: Path,
) -> None:
    digest = "c" * 64
    expected_name = f"RegisteredCampaign-correctness-attestation-{digest}.json"
    expected = tmp_path / ".artifacts" / "research" / "strategy-validation" / expected_name

    assert (
        resolve_correctness_attestation_output(tmp_path, "RegisteredCampaign", digest, None)
        == expected
    )
    with pytest.raises(ValueError, match="registered filename"):
        resolve_correctness_attestation_output(
            tmp_path,
            "RegisteredCampaign",
            digest,
            expected.with_name("incorrect.json"),
        )
    with pytest.raises(ValueError, match="correctness attestation output escapes"):
        resolve_correctness_attestation_output(
            tmp_path,
            "RegisteredCampaign",
            digest,
            tmp_path / "outside" / expected_name,
        )
