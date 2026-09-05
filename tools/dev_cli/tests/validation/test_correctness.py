"""Contracts for generic strategy source and correctness attestations."""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
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
    verify_registered_strategy_correctness_attestation,
    verify_strategy_correctness_attestation,
)
from dev_cli.validation.persistence.backtest_manifest_store import manifest_sha256


def _inputs(
    tmp_path: Path,
) -> tuple[
    tuple[CorrectnessFileBinding, ...],
    CorrectnessSemanticFinding,
    CorrectnessReviewer,
]:
    current = tmp_path / "core.py"
    package = tmp_path / "reference.py"
    fixture = tmp_path / "fixture.json"
    current.write_text("current-v1", encoding="utf-8")
    package.write_text("package-v1", encoding="utf-8")
    fixture.write_text('{"fixture":1}', encoding="utf-8")
    files = (
        CorrectnessFileBinding(
            "strategy.current",
            CorrectnessFileClass.STRATEGY_SOURCE,
            CorrectnessContentClassification.NON_SENSITIVE_SOURCE,
            current,
            "strategies/current/core.py",
        ),
        CorrectnessFileBinding(
            "package.reference",
            CorrectnessFileClass.PACKAGE_SOURCE,
            CorrectnessContentClassification.NON_SENSITIVE_SOURCE,
            package,
            "package/reference.py",
        ),
        CorrectnessFileBinding(
            "fixture.semantic",
            CorrectnessFileClass.FIXTURE,
            CorrectnessContentClassification.NON_SENSITIVE_FIXTURE,
            fixture,
            "tests/fixtures/semantic.json",
        ),
    )
    finding = CorrectnessSemanticFinding(
        finding_id="semantic.crossover",
        authority="reviewed reference source and package implementation",
        current_behavior="The current implementation uses the immediate prior value.",
        authoritative_behavior="The reference implementation uses the prior non-zero value.",
        severity=CorrectnessFindingSeverity.HIGH,
        status=CorrectnessFindingStatus.REMEDIATION_REQUIRED,
        finding_class=CorrectnessFindingClass.STRATEGY_SEMANTIC,
        remediation="Resolve through the registered semantic redesign hierarchy.",
        evidence_file_ids=(
            "strategy.current",
            "package.reference",
            "fixture.semantic",
        ),
    )
    reviewer = CorrectnessReviewer(
        identity="independent-model-validator",
        role="independent quantitative validation",
        independent=True,
        reviewed_at_utc=datetime(2026, 7, 22, 8, 30, tzinfo=UTC),
    )
    return files, finding, reviewer


def _payload(tmp_path: Path) -> tuple[dict[str, object], dict[str, Path]]:
    files, finding, reviewer = _inputs(tmp_path)
    payload = build_strategy_correctness_attestation(
        strategy_id="generic_strategy_v1",
        strategy_version="1.0.0",
        files=files,
        findings=(finding,),
        reviewer=reviewer,
    )
    paths = {binding.binding_id: binding.path for binding in files}
    return payload, paths


def _rehash(payload: dict[str, object]) -> None:
    unsigned = dict(payload)
    del unsigned["attestation_sha256"]
    payload["attestation_sha256"] = manifest_sha256(unsigned)


def _contract(payload: dict[str, object]) -> dict[str, object]:
    file_rows = payload["files"]
    assert isinstance(file_rows, list)
    return {
        "required": True,
        "schema_id": "vynmatrix.strategy-correctness-contract",
        "schema_version": 1,
        "attestation_schema_id": "vynmatrix.strategy-correctness-attestation",
        "attestation_schema_version": 1,
        "subject": deepcopy(payload["subject"]),
        "files": [
            {key: value for key, value in row.items() if key != "content_base64"}
            | {"path_scope": "repository_relative"}
            for row in file_rows
        ],
        "findings": deepcopy(payload["findings"]),
        "reviewer": deepcopy(payload["reviewer"]),
        "exact_attestation": {
            "status": "frozen_pre_outcome_source_reverification",
            "attestation_sha256": payload["attestation_sha256"],
            "summary": summarize_strategy_correctness_attestation(payload),
        },
    }


def test_correctness_attestation_is_canonical_self_hashed_and_file_bound(
    tmp_path: Path,
) -> None:
    payload, paths = _payload(tmp_path)

    verify_strategy_correctness_attestation(payload)
    verify_strategy_correctness_attestation(payload, files=paths)
    unsigned = dict(payload)
    observed_hash = unsigned.pop("attestation_sha256")
    assert observed_hash == manifest_sha256(unsigned)
    assert payload["schema_id"] == "vynmatrix.strategy-correctness-attestation"
    assert payload["attestation_status"] == "verified_no_unresolved_evidence_integrity"
    file_rows = payload["files"]
    assert isinstance(file_rows, list)
    assert [row["id"] for row in file_rows] == [
        "fixture.semantic",
        "package.reference",
        "strategy.current",
    ]
    assert payload["reviewer"] == {
        "identity": "independent-model-validator",
        "role": "independent quantitative validation",
        "independent": True,
        "reviewed_at_utc": "2026-07-22T08:30:00Z",
    }
    for row in file_rows:
        embedded = base64.b64decode(row["content_base64"], validate=True)
        assert len(embedded) == row["byte_count"]
    assert summarize_strategy_correctness_attestation(payload) == {
        "file_count": 3,
        "finding_count": 1,
        "strategy_semantic_finding_count": 1,
        "evidence_integrity_finding_count": 0,
        "remediation_required_finding_ids": ["semantic.crossover"],
        "accepted_deviation_finding_ids": [],
        "resolved_evidence_integrity_finding_ids": [],
        "unresolved_evidence_integrity_finding_count": 0,
    }


def test_registered_correctness_contract_returns_a_typed_deterministic_decision(
    tmp_path: Path,
) -> None:
    payload, _paths = _payload(tmp_path)

    decision = verify_registered_strategy_correctness_attestation(payload, _contract(payload))

    assert decision.strategy_id == "generic_strategy_v1"
    assert decision.attestation_sha256 == payload["attestation_sha256"]
    assert decision.file_ids == (
        "fixture.semantic",
        "package.reference",
        "strategy.current",
    )
    assert decision.remediation_required_finding_ids == ("semantic.crossover",)
    assert decision.reviewer_independent is True
    assert decision.to_dict()["unresolved_evidence_integrity_finding_count"] == 0


def test_registered_correctness_contract_rejects_every_mismatched_ledger(
    tmp_path: Path,
) -> None:
    payload, _paths = _payload(tmp_path)

    extra_field = _contract(payload)
    extra_field["unexpected"] = True
    with pytest.raises(ValueError, match="correctness contract fields differ"):
        verify_registered_strategy_correctness_attestation(payload, extra_field)

    file_mismatch = _contract(payload)
    files = file_mismatch["files"]
    assert isinstance(files, list)
    files[0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match=r"file fixture\.semantic differs"):
        verify_registered_strategy_correctness_attestation(payload, file_mismatch)

    finding_mismatch = _contract(payload)
    findings = finding_mismatch["findings"]
    assert isinstance(findings, list)
    findings[0]["remediation"] = "A different registered remediation."
    with pytest.raises(ValueError, match="findings differ"):
        verify_registered_strategy_correctness_attestation(payload, finding_mismatch)

    reviewer_mismatch = _contract(payload)
    reviewer = reviewer_mismatch["reviewer"]
    assert isinstance(reviewer, dict)
    reviewer["identity"] = "different-reviewer"
    with pytest.raises(ValueError, match="reviewer differs"):
        verify_registered_strategy_correctness_attestation(payload, reviewer_mismatch)

    hash_mismatch = _contract(payload)
    exact = hash_mismatch["exact_attestation"]
    assert isinstance(exact, dict)
    exact["attestation_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash differs"):
        verify_registered_strategy_correctness_attestation(payload, hash_mismatch)

    summary_mismatch = _contract(payload)
    exact = summary_mismatch["exact_attestation"]
    assert isinstance(exact, dict)
    summary = exact["summary"]
    assert isinstance(summary, dict)
    summary["finding_count"] = 99
    with pytest.raises(ValueError, match="summary differs"):
        verify_registered_strategy_correctness_attestation(payload, summary_mismatch)


def test_correctness_attestation_detects_outer_and_file_tampering(tmp_path: Path) -> None:
    payload, paths = _payload(tmp_path)
    tampered = deepcopy(payload)
    tampered["attestation_status"] = "changed"
    with pytest.raises(ValueError, match="content hash differs"):
        verify_strategy_correctness_attestation(tampered)

    rebound = deepcopy(payload)
    rows = rebound["files"]
    assert isinstance(rows, list)
    rows[0]["sha256"] = "0" * 64
    _rehash(rebound)
    with pytest.raises(ValueError, match="embedded digest differs"):
        verify_strategy_correctness_attestation(rebound, files=paths)

    paths["strategy.current"].write_text("current-v2", encoding="utf-8")
    with pytest.raises(ValueError, match="file digest differs"):
        verify_strategy_correctness_attestation(payload, files=paths)


def test_correctness_attestation_rejects_embedded_tampering_and_sensitive_classes(
    tmp_path: Path,
) -> None:
    payload, _paths = _payload(tmp_path)
    rows = payload["files"]
    assert isinstance(rows, list)
    rows[0]["content_base64"] = base64.b64encode(b"different").decode("ascii")
    _rehash(payload)
    with pytest.raises(ValueError, match="embedded byte count differs"):
        verify_strategy_correctness_attestation(payload)

    payload, _paths = _payload(tmp_path)
    rows = payload["files"]
    assert isinstance(rows, list)
    rows[0]["content_classification"] = "sensitive"
    _rehash(payload)
    with pytest.raises(ValueError, match="content_classification"):
        verify_strategy_correctness_attestation(payload)


def test_correctness_attestation_rejects_oversized_embedded_files(tmp_path: Path) -> None:
    files, finding, reviewer = _inputs(tmp_path)
    files[0].path.write_bytes(b"x" * 1_048_577)

    with pytest.raises(ValueError, match="embedding limit"):
        build_strategy_correctness_attestation(
            strategy_id="generic_strategy_v1",
            strategy_version="1.0.0",
            files=files,
            findings=(finding,),
            reviewer=reviewer,
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("severity", "severe"),
        ("status", "pending_review"),
        ("finding_class", "operational"),
    ],
)
def test_correctness_attestation_rejects_unknown_finding_types(
    tmp_path: Path,
    field: str,
    invalid_value: str,
) -> None:
    payload, _paths = _payload(tmp_path)
    findings = payload["findings"]
    assert isinstance(findings, list)
    findings[0][field] = invalid_value
    _rehash(payload)

    with pytest.raises(ValueError, match=field):
        verify_strategy_correctness_attestation(payload)


def test_correctness_attestation_rejects_unresolved_evidence_integrity(
    tmp_path: Path,
) -> None:
    files, finding, reviewer = _inputs(tmp_path)
    unresolved = replace(
        finding,
        finding_id="integrity.source-identity",
        finding_class=CorrectnessFindingClass.EVIDENCE_INTEGRITY,
        severity=CorrectnessFindingSeverity.CRITICAL,
        status=CorrectnessFindingStatus.UNRESOLVED,
        remediation="Obtain and bind the authoritative source file.",
    )
    with pytest.raises(ValueError, match=r"evidence-integrity finding.*is unresolved"):
        build_strategy_correctness_attestation(
            strategy_id="generic_strategy_v1",
            strategy_version="1.0.0",
            files=files,
            findings=(unresolved,),
            reviewer=reviewer,
        )

    payload, _paths = _payload(tmp_path)
    findings = payload["findings"]
    assert isinstance(findings, list)
    findings[0]["finding_class"] = "evidence_integrity"
    findings[0]["status"] = "unresolved"
    _rehash(payload)
    with pytest.raises(ValueError, match=r"evidence-integrity finding.*is unresolved"):
        verify_strategy_correctness_attestation(payload)


def test_correctness_attestation_rejects_duplicate_or_incomplete_bindings(
    tmp_path: Path,
) -> None:
    files, finding, reviewer = _inputs(tmp_path)
    duplicate = replace(files[1], binding_id=files[0].binding_id)
    with pytest.raises(ValueError, match="file IDs must be unique"):
        build_strategy_correctness_attestation(
            strategy_id="generic_strategy_v1",
            strategy_version="1.0.0",
            files=(files[0], duplicate, files[2]),
            findings=(finding,),
            reviewer=reviewer,
        )

    with pytest.raises(ValueError, match="must bind strategy source, package source, and fixture"):
        build_strategy_correctness_attestation(
            strategy_id="generic_strategy_v1",
            strategy_version="1.0.0",
            files=files[:2],
            findings=(replace(finding, evidence_file_ids=("strategy.current",)),),
            reviewer=reviewer,
        )


def test_correctness_attestation_rejects_duplicate_findings_and_empty_fields(
    tmp_path: Path,
) -> None:
    files, finding, reviewer = _inputs(tmp_path)
    with pytest.raises(ValueError, match="finding IDs must be unique"):
        build_strategy_correctness_attestation(
            strategy_id="generic_strategy_v1",
            strategy_version="1.0.0",
            files=files,
            findings=(finding, finding),
            reviewer=reviewer,
        )

    with pytest.raises(ValueError, match="remediation must be a non-blank string"):
        build_strategy_correctness_attestation(
            strategy_id="generic_strategy_v1",
            strategy_version="1.0.0",
            files=files,
            findings=(replace(finding, remediation="  "),),
            reviewer=reviewer,
        )


def test_correctness_attestation_rejects_malformed_hashes_and_binding_sets(
    tmp_path: Path,
) -> None:
    payload, paths = _payload(tmp_path)
    payload["attestation_sha256"] = "not-a-digest"
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        verify_strategy_correctness_attestation(payload)

    payload, paths = _payload(tmp_path)
    rows = payload["files"]
    assert isinstance(rows, list)
    rows[0]["sha256"] = "not-a-digest"
    _rehash(payload)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        verify_strategy_correctness_attestation(payload)

    payload, paths = _payload(tmp_path)
    del paths["fixture.semantic"]
    with pytest.raises(ValueError, match="file binding IDs differ"):
        verify_strategy_correctness_attestation(payload, files=paths)


def test_correctness_attestation_requires_a_canonical_utc_review_timestamp(
    tmp_path: Path,
) -> None:
    files, finding, reviewer = _inputs(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_strategy_correctness_attestation(
            strategy_id="generic_strategy_v1",
            strategy_version="1.0.0",
            files=files,
            findings=(finding,),
            reviewer=replace(reviewer, reviewed_at_utc=datetime(2026, 7, 22, 8, 30)),
        )
    with pytest.raises(ValueError, match="must use UTC"):
        build_strategy_correctness_attestation(
            strategy_id="generic_strategy_v1",
            strategy_version="1.0.0",
            files=files,
            findings=(finding,),
            reviewer=replace(
                reviewer,
                reviewed_at_utc=datetime(
                    2026,
                    7,
                    22,
                    8,
                    30,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
            ),
        )
