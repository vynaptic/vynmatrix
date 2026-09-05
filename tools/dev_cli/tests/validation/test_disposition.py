"""Fail-closed contracts for the pure historical disposition hierarchy."""

from __future__ import annotations

from dataclasses import replace

import pytest

from dev_cli.validation.correctness import (
    CorrectnessFindingSeverity,
    CorrectnessFindingStatus,
)
from dev_cli.validation.disposition import (
    CorrectnessDispositionSummary,
    FixedBaselineDispositionSummary,
    HistoricalCompletionState,
    HistoricalDispositionEvidence,
    HistoricalDispositionPolicy,
    HistoricalEconomicDisposition,
    HistoricalEvidenceCheck,
    HistoricalEvidenceKind,
    HistoricalEvidenceRequirement,
    HistoricalEvidenceSource,
    HistoricalEvidenceStatus,
    HistoricalGateCheck,
    ProspectivePowerDispositionSummary,
    RegisteredRedesignCandidateSummary,
    RegisteredRedesignDispositionSummary,
    SemanticCorrectnessFindingSummary,
    evaluate_historical_strategy_disposition,
)

_DIGEST = "a" * 64
_CHECK_SPECS = (
    ("trial-ledger", HistoricalEvidenceKind.TRIAL),
    ("market-source", HistoricalEvidenceKind.SOURCE),
    ("hash-reconciliation", HistoricalEvidenceKind.HASH_RECONCILIATION),
    ("robustness-ledger", HistoricalEvidenceKind.ROBUSTNESS),
    ("dsr-ledger", HistoricalEvidenceKind.DEFLATED_SHARPE),
    ("power-ledger", HistoricalEvidenceKind.POWER),
    ("scoring-ledger", HistoricalEvidenceKind.SCORING),
    ("correctness-attestation", HistoricalEvidenceKind.CORRECTNESS),
)


def _policy(
    *,
    promotion_blocker_finding_ids: tuple[str, ...] = (),
) -> HistoricalDispositionPolicy:
    return HistoricalDispositionPolicy(
        policy_id="generic-policy-v1",
        baseline_arm_id="B0",
        baseline_ledger_check_id="robustness-ledger",
        required_evidence=tuple(
            HistoricalEvidenceRequirement(
                check_id=check_id,
                kind=kind,
                required_source_ids=(f"{check_id}-source",),
                allows_verified_empty=check_id == "robustness-ledger",
            )
            for check_id, kind in _CHECK_SPECS
        ),
        baseline_quantitative_gate_ids=("economic-edge", "absolute-risk"),
        baseline_economic_gate_ids=("economic-edge",),
        prospective_primary_effect_id="effect-0p2",
        maximum_prospective_duration_years=5.0,
        registered_redesign_candidate_ids=("A1", "A2"),
        redesign_quantitative_gate_ids=("positive-edge", "cost-tolerance"),
        registered_semantic_candidate_ids=("S1", "S2", "S5"),
        correctable_semantic_candidate_ids=("S1", "S2"),
        registered_promotion_blocker_finding_ids=promotion_blocker_finding_ids,
    )


def _checks(
    *,
    statuses: dict[str, HistoricalEvidenceStatus] | None = None,
) -> tuple[HistoricalEvidenceCheck, ...]:
    resolved = statuses or {}
    return tuple(
        HistoricalEvidenceCheck(
            check_id=check_id,
            kind=kind,
            status=resolved.get(check_id, HistoricalEvidenceStatus.VERIFIED),
            sources=(
                HistoricalEvidenceSource(
                    source_id=f"{check_id}-source",
                    sha256=(
                        _DIGEST
                        if resolved.get(check_id, HistoricalEvidenceStatus.VERIFIED).complete
                        else None
                    ),
                ),
            ),
        )
        for check_id, kind in _CHECK_SPECS
    )


def _baseline(
    *,
    economic: bool = True,
    risk: bool = True,
    zero: bool = False,
) -> FixedBaselineDispositionSummary:
    # Deliberately reverse the registered order; result serialization must be canonical.
    return FixedBaselineDispositionSummary(
        arm_id="B0",
        gate_checks=(
            HistoricalGateCheck("absolute-risk", risk),
            HistoricalGateCheck("economic-edge", economic),
        ),
        zero_trades_or_exposure=zero,
    )


def _candidate(
    candidate_id: str,
    *,
    eligible: bool = False,
    zero: bool = False,
) -> RegisteredRedesignCandidateSummary:
    return RegisteredRedesignCandidateSummary(
        candidate_id=candidate_id,
        gate_checks=(
            HistoricalGateCheck("cost-tolerance", eligible),
            HistoricalGateCheck("positive-edge", eligible),
        ),
        zero_trades_or_exposure=zero,
    )


def _redesign(
    *,
    eligible: tuple[str, ...] = (),
) -> RegisteredRedesignDispositionSummary:
    # Deliberately reverse the registry to exercise canonical candidate ordering.
    return RegisteredRedesignDispositionSummary(
        candidates=tuple(
            _candidate(candidate_id, eligible=candidate_id in eligible)
            for candidate_id in ("A2", "A1")
        )
    )


def _power(
    *,
    powered: bool = True,
    zero: bool = False,
) -> ProspectivePowerDispositionSummary:
    return ProspectivePowerDispositionSummary(
        primary_effect_id="effect-0p2",
        maximum_duration_years=5.0,
        powered_within_maximum_duration=powered,
        operationally_impractical=not powered,
        not_applicable_due_to_zero_trades=zero,
    )


def _finding(
    finding_id: str,
    *,
    severity: CorrectnessFindingSeverity = CorrectnessFindingSeverity.HIGH,
    semantic_candidate_id: str | None = "S1",
    status: CorrectnessFindingStatus = CorrectnessFindingStatus.REMEDIATION_REQUIRED,
) -> SemanticCorrectnessFindingSummary:
    return SemanticCorrectnessFindingSummary(
        finding_id=finding_id,
        severity=severity,
        status=status,
        semantic_candidate_id=semantic_candidate_id,
        evidence_source_ids=("strategy-source",),
    )


def _evidence(
    *,
    checks: tuple[HistoricalEvidenceCheck, ...] | None = None,
    baseline: FixedBaselineDispositionSummary | None = None,
    redesign: RegisteredRedesignDispositionSummary | None = None,
    power: ProspectivePowerDispositionSummary | None = None,
    correctness: CorrectnessDispositionSummary | None = None,
) -> HistoricalDispositionEvidence:
    return HistoricalDispositionEvidence(
        checks=_checks() if checks is None else checks,
        baseline=_baseline() if baseline is None else baseline,
        redesign=_redesign() if redesign is None else redesign,
        power=_power() if power is None else power,
        correctness=(CorrectnessDispositionSummary(()) if correctness is None else correctness),
    )


def test_complete_exact_baseline_freezes_for_prospective_research_only() -> None:
    result = evaluate_historical_strategy_disposition(_policy(), _evidence())

    assert result.completion_state is HistoricalCompletionState.COMPLETE
    assert (
        result.economic_disposition is HistoricalEconomicDisposition.FREEZE_FOR_PROSPECTIVE_SHADOW
    )
    assert result.frozen_arm_id == "B0"
    assert result.blocker_codes == ()
    payload = result.to_dict()
    assert payload["selected_redesign_candidate_id"] is None
    assert payload["baseline"]["gate_checks"] == [  # type: ignore[index]
        {"gate_id": "economic-edge", "passed": True},
        {"gate_id": "absolute-risk", "passed": True},
    ]
    assert payload["authority"] == {
        "decision_scope": "historical_research_ranking_rejection_only",
        "prospective_shadow_research_eligible": True,
        "paper_trading_authorized": False,
        "live_trading_authorized": False,
        "automatic_parameter_deployment_authorized": False,
        "future_promotion_requires_prospective_evidence": True,
    }


@pytest.mark.parametrize(
    ("check_id", "status"),
    [
        (check_id, status)
        for check_id, _kind in _CHECK_SPECS
        for status in (
            HistoricalEvidenceStatus.MISSING,
            HistoricalEvidenceStatus.CORRUPT,
        )
    ],
)
def test_any_missing_or_corrupt_evidence_blocks_without_disposition(
    check_id: str,
    status: HistoricalEvidenceStatus,
) -> None:
    checks = _checks(statuses={check_id: status})
    evidence = _evidence(
        checks=checks,
        power=None if check_id == "power-ledger" else _power(),
    )

    result = evaluate_historical_strategy_disposition(_policy(), evidence)

    assert result.completion_state is HistoricalCompletionState.BLOCKED_INSUFFICIENT_EVIDENCE
    assert result.economic_disposition is None
    assert result.frozen_arm_id is None
    assert result.blocker_codes == (f"{status.value}_required_evidence:{check_id}",)
    assert result.to_dict()["authority"]["paper_trading_authorized"] is False  # type: ignore[index]


def test_verified_empty_zero_trade_ledger_is_complete_and_retires() -> None:
    evidence = _evidence(
        checks=_checks(
            statuses={
                "robustness-ledger": HistoricalEvidenceStatus.VERIFIED_EMPTY,
            }
        ),
        baseline=_baseline(economic=False, risk=False, zero=True),
        power=_power(powered=False, zero=True),
    )

    result = evaluate_historical_strategy_disposition(_policy(), evidence)

    assert result.completion_state is HistoricalCompletionState.COMPLETE
    assert result.economic_disposition is HistoricalEconomicDisposition.RETIRE
    assert "baseline_zero_completed_trades_or_exposure" in result.blocker_codes
    robustness_check = next(
        check
        for check in result.to_dict()["evidence_checks"]  # type: ignore[union-attr]
        if check["check_id"] == "robustness-ledger"
    )
    assert robustness_check["complete"] is True
    assert robustness_check["status"] == "verified_empty"


def test_complete_but_not_powered_within_five_years_is_retire_input_not_missing() -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(),
        _evidence(power=_power(powered=False)),
    )

    assert result.completion_state is HistoricalCompletionState.COMPLETE
    assert result.economic_disposition is HistoricalEconomicDisposition.RETIRE
    assert result.power is not None
    assert result.power.operationally_impractical is True
    assert "primary_effect_not_powered_within_maximum_duration" in result.blocker_codes


def test_all_eligible_quantitative_candidates_are_recorded_without_selection() -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(),
        _evidence(
            baseline=_baseline(economic=False),
            redesign=_redesign(eligible=("A1", "A2")),
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.REDESIGN
    assert result.eligible_quantitative_redesign_candidate_ids == ("A1", "A2")
    assert [
        row["candidate_id"]
        for row in result.to_dict()["redesign"]["candidates"]  # type: ignore[index]
    ] == ["A1", "A2"]
    assert result.to_dict()["selected_redesign_candidate_id"] is None


def test_correctable_high_semantic_defect_authorizes_redesign_not_deployment() -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(),
        _evidence(
            baseline=_baseline(risk=False),
            correctness=CorrectnessDispositionSummary((_finding("trail-seed"),)),
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.REDESIGN
    assert result.eligible_semantic_redesign_candidate_ids == ("S1",)
    assert result.frozen_arm_id is None
    assert result.to_dict()["authority"]["paper_trading_authorized"] is False  # type: ignore[index]


def test_registered_correctness_only_promotion_blocker_requires_redesign() -> None:
    for severity in (
        CorrectnessFindingSeverity.HIGH,
        CorrectnessFindingSeverity.CRITICAL,
    ):
        policy = _policy(promotion_blocker_finding_ids=("nondurable-position-state",))
        assert policy.to_dict()["registered_promotion_blocker_finding_ids"] == [
            "nondurable-position-state"
        ]
        result = evaluate_historical_strategy_disposition(
            policy,
            _evidence(
                correctness=CorrectnessDispositionSummary(
                    (
                        _finding(
                            "nondurable-position-state",
                            severity=severity,
                            semantic_candidate_id=None,
                        ),
                    )
                )
            ),
        )

        assert result.economic_disposition is HistoricalEconomicDisposition.REDESIGN
        assert result.frozen_arm_id is None
        assert result.eligible_quantitative_redesign_candidate_ids == ()
        assert result.eligible_semantic_redesign_candidate_ids == ()
        assert result.blocker_codes == (
            "unresolved_high_severity_correctness_finding:nondurable-position-state",
        )
        assert (
            "registered_correctness_only_promotion_blocker_requires_redesign:"
            "nondurable-position-state"
        ) in result.reason_codes
        assert (
            "promotion_blocker_redesign_requires_new_attestation_protocol_manifest_and_full_rerun"
        ) in result.reason_codes
        payload = result.to_dict()
        assert payload["selected_redesign_candidate_id"] is None
        assert (
            payload["authority"]["prospective_shadow_research_eligible"] is False  # type: ignore[index]
        )
        assert payload["authority"]["paper_trading_authorized"] is False  # type: ignore[index]


def test_registered_promotion_blocker_reasons_follow_policy_order() -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(promotion_blocker_finding_ids=("z-blocker", "a-blocker")),
        _evidence(
            correctness=CorrectnessDispositionSummary(
                (
                    _finding("a-blocker", semantic_candidate_id=None),
                    _finding("z-blocker", semantic_candidate_id=None),
                )
            )
        ),
    )

    assert tuple(
        reason
        for reason in result.reason_codes
        if reason.startswith("registered_correctness_only_promotion_blocker_requires_redesign:")
    ) == (
        "registered_correctness_only_promotion_blocker_requires_redesign:z-blocker",
        "registered_correctness_only_promotion_blocker_requires_redesign:a-blocker",
    )


@pytest.mark.parametrize(
    ("baseline", "power", "checks", "expected_reason"),
    [
        (
            _baseline(economic=False),
            _power(),
            _checks(),
            "registered_promotion_blocker_redesign_vetoed_by_baseline_quantitative_failure",
        ),
        (
            _baseline(risk=False),
            _power(),
            _checks(),
            "registered_promotion_blocker_redesign_vetoed_by_baseline_quantitative_failure",
        ),
        (
            _baseline(),
            _power(powered=False),
            _checks(),
            "otherwise_eligible_redesign_vetoed_by_unpowered_primary_effect",
        ),
        (
            _baseline(economic=False, risk=False, zero=True),
            _power(powered=False, zero=True),
            _checks(statuses={"robustness-ledger": HistoricalEvidenceStatus.VERIFIED_EMPTY}),
            "otherwise_eligible_redesign_vetoed_by_zero_baseline_activity",
        ),
    ],
)
def test_registered_promotion_blocker_does_not_rescue_failed_viability(
    baseline: FixedBaselineDispositionSummary,
    power: ProspectivePowerDispositionSummary,
    checks: tuple[HistoricalEvidenceCheck, ...],
    expected_reason: str,
) -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(promotion_blocker_finding_ids=("execution-blocker",)),
        _evidence(
            checks=checks,
            baseline=baseline,
            redesign=_redesign(eligible=("A1",)),
            power=power,
            correctness=CorrectnessDispositionSummary(
                (_finding("execution-blocker", semantic_candidate_id=None),)
            ),
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.RETIRE
    assert result.frozen_arm_id is None
    assert expected_reason in result.reason_codes
    assert (
        "registered_correctness_only_promotion_blocker_requires_redesign:execution-blocker"
        not in result.reason_codes
    )


def test_registered_promotion_blocker_cannot_override_another_uncorrectable_finding() -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(promotion_blocker_finding_ids=("execution-blocker",)),
        _evidence(
            correctness=CorrectnessDispositionSummary(
                (
                    _finding("execution-blocker", semantic_candidate_id=None),
                    _finding("unregistered-critical", semantic_candidate_id=None),
                )
            )
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.RETIRE
    assert (
        "otherwise_eligible_redesign_vetoed_by_uncorrectable_safety_defect" in result.reason_codes
    )
    assert (
        "unresolved_critical_or_uncorrectable_high_correctness_finding:unregistered-critical"
    ) in result.reason_codes


@pytest.mark.parametrize(
    ("finding", "match"),
    [
        (None, "missing from correctness evidence"),
        (
            _finding(
                "execution-blocker",
                semantic_candidate_id=None,
                status=CorrectnessFindingStatus.RESOLVED,
            ),
            "is not open for remediation",
        ),
        (
            _finding(
                "execution-blocker",
                severity=CorrectnessFindingSeverity.MEDIUM,
                semantic_candidate_id=None,
            ),
            "is not high or critical",
        ),
        (
            _finding("execution-blocker", semantic_candidate_id="S1"),
            "must not identify a semantic candidate",
        ),
    ],
)
def test_registered_promotion_blocker_must_match_qualifying_correctness_evidence(
    finding: SemanticCorrectnessFindingSummary | None,
    match: str,
) -> None:
    correctness = CorrectnessDispositionSummary(() if finding is None else (finding,))

    with pytest.raises(ValueError, match=match):
        evaluate_historical_strategy_disposition(
            _policy(promotion_blocker_finding_ids=("execution-blocker",)),
            _evidence(correctness=correctness),
        )


def test_promotion_blocker_registry_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="must contain unique identifiers"):
        _policy(
            promotion_blocker_finding_ids=(
                "execution-blocker",
                "execution-blocker",
            )
        )


def test_empty_promotion_blocker_registry_preserves_prior_uncorrectable_behavior() -> None:
    policy = _policy()
    assert policy.registered_promotion_blocker_finding_ids == ()
    assert "registered_promotion_blocker_finding_ids" not in policy.to_dict()

    result = evaluate_historical_strategy_disposition(
        policy,
        _evidence(
            correctness=CorrectnessDispositionSummary(
                (_finding("execution-blocker", semantic_candidate_id=None),)
            )
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.RETIRE
    assert result.reason_codes == (
        "baseline_did_not_satisfy_freeze_requirements",
        "no_registered_redesign_path_satisfied_its_requirements",
        "unresolved_critical_or_uncorrectable_high_correctness_finding:execution-blocker",
        "historical_research_rejection_does_not_authorize_future_trading",
    )


@pytest.mark.parametrize(
    ("severity", "semantic_candidate_id"),
    [
        (CorrectnessFindingSeverity.CRITICAL, "S1"),
        (CorrectnessFindingSeverity.HIGH, "S5"),
        (CorrectnessFindingSeverity.HIGH, None),
    ],
)
def test_critical_or_noncorrectable_high_defect_blocks_freeze_without_redesign(
    severity: CorrectnessFindingSeverity,
    semantic_candidate_id: str | None,
) -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(),
        _evidence(
            correctness=CorrectnessDispositionSummary(
                (
                    _finding(
                        "unsafe-semantic",
                        severity=severity,
                        semantic_candidate_id=semantic_candidate_id,
                    ),
                )
            )
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.RETIRE
    assert result.eligible_semantic_redesign_candidate_ids == ()
    assert result.blocker_codes == ("unresolved_high_severity_correctness_finding:unsafe-semantic",)


@pytest.mark.parametrize(
    ("severity", "semantic_candidate_id"),
    [
        (CorrectnessFindingSeverity.CRITICAL, "S1"),
        (CorrectnessFindingSeverity.HIGH, "S5"),
        (CorrectnessFindingSeverity.HIGH, None),
    ],
)
def test_quantitative_candidate_cannot_override_an_uncorrectable_safety_defect(
    severity: CorrectnessFindingSeverity,
    semantic_candidate_id: str | None,
) -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(),
        _evidence(
            baseline=_baseline(economic=False),
            redesign=_redesign(eligible=("A1",)),
            correctness=CorrectnessDispositionSummary(
                (
                    _finding(
                        "unsafe-semantic",
                        severity=severity,
                        semantic_candidate_id=semantic_candidate_id,
                    ),
                )
            ),
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.RETIRE
    assert result.eligible_quantitative_redesign_candidate_ids == ("A1",)
    assert result.eligible_semantic_redesign_candidate_ids == ()
    assert (
        "otherwise_eligible_redesign_vetoed_by_uncorrectable_safety_defect" in result.reason_codes
    )
    assert "no_registered_redesign_path_satisfied_its_requirements" not in result.reason_codes
    assert any(
        code.endswith(":unsafe-semantic")
        for code in result.reason_codes
        if code.startswith("unresolved_critical_or_uncorrectable_high_correctness_finding:")
    )


def test_quantitative_redesign_carries_every_registered_correctable_high_repair() -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(),
        _evidence(
            baseline=_baseline(economic=False),
            redesign=_redesign(eligible=("A1",)),
            correctness=CorrectnessDispositionSummary(
                (_finding("correctable-semantic", semantic_candidate_id="S1"),)
            ),
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.REDESIGN
    assert result.eligible_quantitative_redesign_candidate_ids == ("A1",)
    assert result.eligible_semantic_redesign_candidate_ids == ("S1",)
    assert "registered_correctable_high_semantic_defect:S1" in result.reason_codes


def test_redesign_carries_registered_medium_remediation_without_using_it_as_trigger() -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(),
        _evidence(
            baseline=_baseline(economic=False),
            redesign=_redesign(eligible=("A1",)),
            correctness=CorrectnessDispositionSummary(
                (
                    _finding("high-repair", semantic_candidate_id="S1"),
                    _finding(
                        "medium-repair",
                        severity=CorrectnessFindingSeverity.MEDIUM,
                        semantic_candidate_id="S2",
                    ),
                )
            ),
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.REDESIGN
    assert result.eligible_semantic_redesign_candidate_ids == ("S1", "S2")
    assert "registered_correctable_high_semantic_defect:S1" in result.reason_codes
    assert "registered_correctable_semantic_remediation:S2" in result.reason_codes


def test_mapped_medium_semantic_alone_cannot_trigger_redesign() -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(),
        _evidence(
            baseline=_baseline(economic=True, risk=False),
            correctness=CorrectnessDispositionSummary(
                (
                    _finding(
                        "medium-only-repair",
                        severity=CorrectnessFindingSeverity.MEDIUM,
                        semantic_candidate_id="S2",
                    ),
                )
            ),
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.RETIRE
    assert result.eligible_quantitative_redesign_candidate_ids == ()
    assert result.eligible_semantic_redesign_candidate_ids == ()
    assert "no_registered_redesign_path_satisfied_its_requirements" in result.reason_codes


@pytest.mark.parametrize(
    ("zero", "powered", "expected_reason"),
    [
        (
            True,
            False,
            "otherwise_eligible_redesign_vetoed_by_zero_baseline_activity",
        ),
        (
            False,
            False,
            "otherwise_eligible_redesign_vetoed_by_unpowered_primary_effect",
        ),
    ],
)
def test_semantic_redesign_path_cannot_override_viability_veto(
    zero: bool,
    powered: bool,
    expected_reason: str,
) -> None:
    checks = (
        _checks(statuses={"robustness-ledger": HistoricalEvidenceStatus.VERIFIED_EMPTY})
        if zero
        else _checks()
    )
    result = evaluate_historical_strategy_disposition(
        _policy(),
        _evidence(
            checks=checks,
            baseline=_baseline(economic=True, risk=False, zero=zero),
            power=_power(powered=powered, zero=zero),
            correctness=CorrectnessDispositionSummary(
                (_finding("high-semantic-repair", semantic_candidate_id="S1"),)
            ),
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.RETIRE
    assert result.eligible_quantitative_redesign_candidate_ids == ()
    assert result.eligible_semantic_redesign_candidate_ids == ()
    assert expected_reason in result.reason_codes
    assert "no_registered_redesign_path_satisfied_its_requirements" not in result.reason_codes


@pytest.mark.parametrize(
    ("zero", "powered", "expected_blocker"),
    [
        (True, False, "baseline_zero_completed_trades_or_exposure"),
        (False, False, "primary_effect_not_powered_within_maximum_duration"),
    ],
)
def test_quantitative_candidate_cannot_override_baseline_retirement_conditions(
    zero: bool,
    powered: bool,
    expected_blocker: str,
) -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(),
        _evidence(
            checks=(
                _checks(
                    statuses={
                        "robustness-ledger": HistoricalEvidenceStatus.VERIFIED_EMPTY,
                    }
                )
                if zero
                else _checks()
            ),
            baseline=_baseline(economic=False, risk=False, zero=zero),
            redesign=_redesign(eligible=("A1",)),
            power=_power(powered=powered, zero=zero),
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.RETIRE
    assert result.eligible_quantitative_redesign_candidate_ids == ("A1",)
    assert expected_blocker in result.blocker_codes
    expected_reason = (
        "otherwise_eligible_redesign_vetoed_by_zero_baseline_activity"
        if zero
        else "otherwise_eligible_redesign_vetoed_by_unpowered_primary_effect"
    )
    assert expected_reason in result.reason_codes
    assert "no_registered_redesign_path_satisfied_its_requirements" not in result.reason_codes


def test_mapped_correctable_repairs_follow_policy_order_not_finding_order() -> None:
    result = evaluate_historical_strategy_disposition(
        _policy(),
        _evidence(
            baseline=_baseline(economic=False),
            redesign=_redesign(eligible=("A1",)),
            correctness=CorrectnessDispositionSummary(
                (
                    _finding("a-s2-finding", semantic_candidate_id="S2"),
                    _finding("z-s1-finding", semantic_candidate_id="S1"),
                )
            ),
        ),
    )

    assert result.economic_disposition is HistoricalEconomicDisposition.REDESIGN
    assert result.eligible_semantic_redesign_candidate_ids == ("S1", "S2")
    assert result.reason_codes[:3] == (
        "registered_quantitative_redesign_candidate_eligible:A1",
        "registered_correctable_high_semantic_defect:S1",
        "registered_correctable_high_semantic_defect:S2",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate-check",
        "unknown-check",
        "wrong-baseline",
        "unknown-baseline-gate",
        "duplicate-candidate-gate",
        "unknown-candidate",
        "unknown-semantic",
        "zero-state-conflict",
        "wrong-power-effect",
        "wrong-power-duration",
        "verified-empty-not-allowed",
        "verified-source-without-hash",
    ],
)
def test_unknown_duplicate_and_impossible_input_combinations_are_rejected(
    mutation: str,
) -> None:
    policy = _policy()
    evidence = _evidence()
    checks = evidence.checks
    baseline = evidence.baseline
    redesign = evidence.redesign
    power = evidence.power
    correctness = evidence.correctness

    if mutation == "duplicate-check":
        checks = (*checks, checks[0])
    elif mutation == "unknown-check":
        checks = (
            *checks[:-1],
            replace(checks[-1], check_id="unknown-check"),
        )
    elif mutation == "wrong-baseline":
        assert baseline is not None
        baseline = replace(baseline, arm_id="P9")
    elif mutation == "unknown-baseline-gate":
        assert baseline is not None
        baseline = replace(
            baseline,
            gate_checks=(
                HistoricalGateCheck("economic-edge", True),
                HistoricalGateCheck("unknown-gate", True),
            ),
        )
    elif mutation == "duplicate-candidate-gate":
        redesign = RegisteredRedesignDispositionSummary(
            (
                RegisteredRedesignCandidateSummary(
                    "A1",
                    (
                        HistoricalGateCheck("positive-edge", True),
                        HistoricalGateCheck("positive-edge", True),
                    ),
                    False,
                ),
                _candidate("A2"),
            )
        )
    elif mutation == "unknown-candidate":
        redesign = RegisteredRedesignDispositionSummary((_candidate("A1"), _candidate("P9")))
    elif mutation == "unknown-semantic":
        correctness = CorrectnessDispositionSummary(
            (_finding("unknown-semantic", semantic_candidate_id="S99"),)
        )
    elif mutation == "zero-state-conflict":
        baseline = _baseline(zero=True)
    elif mutation == "wrong-power-effect":
        assert power is not None
        power = replace(power, primary_effect_id="effect-0p3")
    elif mutation == "wrong-power-duration":
        assert power is not None
        power = replace(power, maximum_duration_years=6.0)
    elif mutation == "verified-empty-not-allowed":
        checks = _checks(statuses={"power-ledger": HistoricalEvidenceStatus.VERIFIED_EMPTY})
    elif mutation == "verified-source-without-hash":
        checks = (
            replace(
                checks[0],
                sources=(
                    HistoricalEvidenceSource(
                        source_id="trial-ledger-source",
                        sha256=None,
                    ),
                ),
            ),
            *checks[1:],
        )
    else:  # pragma: no cover - exhaustive parameter registry above
        raise AssertionError(mutation)

    with pytest.raises(ValueError, match=r".+"):
        evaluate_historical_strategy_disposition(
            policy,
            HistoricalDispositionEvidence(
                checks=checks,
                baseline=baseline,
                redesign=redesign,
                power=power,
                correctness=correctness,
            ),
        )


def test_verified_checks_without_required_typed_power_summary_are_rejected() -> None:
    with pytest.raises(ValueError, match="missing typed summaries"):
        evaluate_historical_strategy_disposition(
            _policy(),
            HistoricalDispositionEvidence(
                checks=_checks(),
                baseline=_baseline(),
                redesign=_redesign(),
                power=None,
                correctness=CorrectnessDispositionSummary(()),
            ),
        )


def test_evidence_container_enforces_runtime_types() -> None:
    with pytest.raises(TypeError, match="checks must be a tuple"):
        HistoricalDispositionEvidence(  # type: ignore[arg-type]
            checks=[],
            baseline=None,
            redesign=None,
            power=None,
            correctness=None,
        )
    with pytest.raises(TypeError, match="baseline must be"):
        HistoricalDispositionEvidence(  # type: ignore[arg-type]
            checks=(),
            baseline="B0",
            redesign=None,
            power=None,
            correctness=None,
        )


def test_power_summary_rejects_impossible_status_combinations() -> None:
    with pytest.raises(ValueError, match="inverse"):
        ProspectivePowerDispositionSummary(
            primary_effect_id="effect-0p2",
            maximum_duration_years=5.0,
            powered_within_maximum_duration=False,
            operationally_impractical=False,
            not_applicable_due_to_zero_trades=False,
        )
