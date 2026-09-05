"""Historical evidence reconciliation and disposition campaign contracts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from campaign_test_support import (
    _REGISTERED_CAMPAIGN,
    _REPO,
    _campaign,
    _complete_minimal_scoring_trial,
    _engine,
    _minimal_historical_audit_registry,
    _protocol,
    _trial_ids_by_sequence,
    _unit_upstream_selection_ledger,
)
from sqlalchemy.orm import Session

from dev_cli.validation import campaign_contracts as campaign_module
from dev_cli.validation import campaign_environment as campaign_environment_module
from dev_cli.validation.campaign import StrategyValidationCampaign
from dev_cli.validation.campaign_environment import load_installed_pure_strategy_core
from dev_cli.validation.cscv import TimestampedDailyReturn
from dev_cli.validation.disposition import (
    CorrectnessDispositionSummary,
    FixedBaselineDispositionSummary,
    HistoricalEvidenceStatus,
    HistoricalGateCheck,
    ProspectivePowerDispositionSummary,
    RegisteredRedesignCandidateSummary,
    RegisteredRedesignDispositionSummary,
)
from dev_cli.validation.persistence.backtest_manifest_store import (
    manifest_sha256,
)
from dev_cli.validation.persistence.backtest_result_store import (
    DerivedBacktestEvidence,
    DerivedEvidenceMetrics,
)
from dev_cli.validation.providers.coinbase_execution_costs import (
    verify_coinbase_execution_cost_measurement,
)
from lib_application.db.models import (
    BacktestResult,
    BacktestTrial,
)


def test_historical_disposition_filter_requires_the_complete_registry() -> None:
    selected = [
        {
            "sequence": 1,
            "runner_kind": "historical_strategy_disposition",
        }
    ]
    all_specs = [
        {"sequence": 0, "runner_kind": "production_core_direct"},
        *selected,
    ]

    with pytest.raises(RuntimeError, match="complete frozen trial registry"):
        StrategyValidationCampaign._require_complete_group_selection(
            selected,
            all_specs=all_specs,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "baseline_quantitative_gate",
        "economic_gate_omitted",
        "risk_gate_included_as_economic",
        "redesign_gate",
        "redesign_candidate",
        "semantic_candidate",
        "correctable_semantic_candidate",
        "evidence_source_swap",
        "allowed_disposition",
        "promotion_authority",
        "semantic_high_trigger",
        "semantic_carried_remediation",
        "primary_power_effect",
        "maximum_power_duration",
    ],
)
def test_historical_disposition_cross_contract_rejects_producer_drift(
    mutation: str,
) -> None:
    protocol = _protocol()
    policy = protocol["historical_disposition_policy"]
    if mutation == "baseline_quantitative_gate":
        policy["baseline_quantitative_gate_ids"].pop()
    elif mutation == "economic_gate_omitted":
        policy["baseline_economic_gate_ids"].pop()
    elif mutation == "risk_gate_included_as_economic":
        policy["baseline_economic_gate_ids"].append("expected_pooled_max_drawdown_within_limit")
    elif mutation == "redesign_gate":
        policy["redesign_quantitative_gate_ids"].pop()
    elif mutation == "redesign_candidate":
        policy["registered_redesign_candidate_ids"].pop()
    elif mutation == "semantic_candidate":
        policy["registered_semantic_candidate_ids"].append("UNREGISTERED_SEMANTIC")
    elif mutation == "correctable_semantic_candidate":
        policy["correctable_semantic_candidate_ids"].append("UNREGISTERED_SEMANTIC")
    elif mutation == "evidence_source_swap":
        by_id = {row["check_id"]: row for row in policy["required_evidence"]}
        by_id["trial-ledger"]["required_source_ids"] = ["scoring_binding_ledger_pooled"]
        by_id["scoring-ledger"]["required_source_ids"] = ["trial_registry"]
    elif mutation == "allowed_disposition":
        protocol["allowed_dispositions"].reverse()
    elif mutation == "promotion_authority":
        protocol["promotion"]["historical_results_can_enable_paper"] = True
    elif mutation == "semantic_high_trigger":
        protocol["historical_disposition_gates"]["semantic_redesign_gate"][
            "high_severity_trigger_candidate_set"
        ].append("UNREGISTERED_SEMANTIC")
    elif mutation == "semantic_carried_remediation":
        protocol["historical_disposition_gates"]["semantic_redesign_gate"][
            "carried_remediation_candidate_set"
        ].append("UNREGISTERED_SEMANTIC")
    elif mutation == "primary_power_effect":
        policy["prospective_primary_effect_id"] = "POWER_EFFECT_0p3"
    elif mutation == "maximum_power_duration":
        policy["maximum_prospective_duration_years"] = 6.0
    else:  # pragma: no cover - the parameter list is closed above
        raise AssertionError(mutation)

    validator = object.__new__(StrategyValidationCampaign)
    with pytest.raises(ValueError, match=r"historical|semantic"):
        validator._validate_historical_disposition_cross_contract(protocol)


def test_historical_disposition_cross_contract_requires_exact_promotion_blockers() -> None:
    protocol = _protocol()
    blocker = deepcopy(protocol["correctness_attestation"]["findings"][0])
    blocker.update(
        {
            "id": "S9.durable_fill_state",
            "finding_class": "strategy_semantic",
            "severity": "critical",
            "status": "remediation_required",
        }
    )
    protocol["correctness_attestation"]["findings"].append(blocker)
    policy = protocol["historical_disposition_policy"]
    policy["registered_promotion_blocker_finding_ids"] = [blocker["id"]]
    validator = object.__new__(StrategyValidationCampaign)

    validator._validate_historical_disposition_cross_contract(protocol)

    policy["registered_promotion_blocker_finding_ids"] = []
    with pytest.raises(ValueError, match="promotion-blocker IDs differ"):
        validator._validate_historical_disposition_cross_contract(protocol)


def test_historical_disposition_cross_contract_rejects_mapped_promotion_blocker() -> None:
    protocol = _protocol()
    blocker = deepcopy(protocol["correctness_attestation"]["findings"][0])
    blocker.update(
        {
            "id": "S9.mapped_state_fix",
            "finding_class": "strategy_semantic",
            "severity": "critical",
            "status": "remediation_required",
        }
    )
    protocol["correctness_attestation"]["findings"].append(blocker)
    protocol["semantic_diagnostic_arms"] = [
        {
            "id": "SEMANTIC_STATE_FIX",
            "correctness_finding_id": blocker["id"],
            "overrides": {},
            "execution": "expected_cost_full_panel_and_direct_oos_diagnostic",
        }
    ]
    policy = protocol["historical_disposition_policy"]
    policy["registered_semantic_candidate_ids"] = ["SEMANTIC_STATE_FIX"]
    policy["correctable_semantic_candidate_ids"] = ["SEMANTIC_STATE_FIX"]
    policy["registered_promotion_blocker_finding_ids"] = [blocker["id"]]
    protocol["historical_disposition_gates"]["semantic_redesign_gate"][
        "carried_remediation_candidate_set"
    ] = ["SEMANTIC_STATE_FIX"]
    validator = object.__new__(StrategyValidationCampaign)

    with pytest.raises(ValueError, match="promotion-blocker IDs differ"):
        validator._validate_historical_disposition_cross_contract(protocol)


def test_historical_registry_audit_distinguishes_missing_and_verified_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _manifest_fixture = _campaign(tmp_path, monkeypatch)
    experiment_id, specs, trial_ids, manifest = _minimal_historical_audit_registry(campaign)

    missing = campaign._audit_historical_registry(
        specs[:-1],
        final_spec=specs[-1],
        all_specs=specs,
        trial_ids=trial_ids,
        experiment_id=experiment_id,
        manifest=manifest,
    )
    assert missing.status is HistoricalEvidenceStatus.MISSING
    assert missing.rows[0]["verification"] == "incomplete:registered"

    _complete_minimal_scoring_trial(
        campaign,
        experiment_id=experiment_id,
        trial_id=trial_ids[0],
    )
    complete = campaign._audit_historical_registry(
        specs[:-1],
        final_spec=specs[-1],
        all_specs=specs,
        trial_ids=trial_ids,
        experiment_id=experiment_id,
        manifest=manifest,
    )
    assert complete.status is HistoricalEvidenceStatus.VERIFIED
    assert complete.rows[0]["verification"] == "verified"
    assert len(complete.derived_sources) == 1


def test_historical_registry_audit_turns_result_hash_drift_into_corrupt_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _manifest_fixture = _campaign(tmp_path, monkeypatch)
    experiment_id, specs, trial_ids, manifest = _minimal_historical_audit_registry(campaign)
    _complete_minimal_scoring_trial(
        campaign,
        experiment_id=experiment_id,
        trial_id=trial_ids[0],
    )
    with Session(campaign._engine) as session:
        trial = session.get(BacktestTrial, trial_ids[0])
        assert trial is not None
        assert trial.result_id is not None
        result = session.get(BacktestResult, trial.result_id)
        assert result is not None
        assert isinstance(result.meta, dict)
        changed = deepcopy(result.meta)
        changed["evidence_payload"]["coverage"]["source_signals"] = 1
        result.meta = changed
        session.commit()

    audit = campaign._audit_historical_registry(
        specs[:-1],
        final_spec=specs[-1],
        all_specs=specs,
        trial_ids=trial_ids,
        experiment_id=experiment_id,
        manifest=manifest,
    )

    assert audit.status is HistoricalEvidenceStatus.CORRUPT
    assert audit.rows[0]["verification"].startswith("corrupt:")


def test_historical_registry_requires_source_attestation_for_attested_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _manifest_fixture = _campaign(tmp_path, monkeypatch)
    experiment_id, specs, trial_ids, manifest = _minimal_historical_audit_registry(
        campaign,
        prior_runner_kind="registered_cscv_pbo",
    )
    campaign._trial_store.mark_running(trial_ids[0])
    campaign._result_store.save_derived_evidence(
        DerivedBacktestEvidence(
            strategy_id="registered_campaign_v1",
            strategy_semver="1.0.0",
            experiment_id=experiment_id,
            evidence_kind="registered_cscv_pbo",
            symbol="BTC-USDC",
            asset_class="crypto",
            timeframe="1440m",
            start_date=datetime(2022, 1, 1, tzinfo=UTC).date(),
            end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
            payload={"runner_kind": "registered_cscv_pbo"},
            metrics=DerivedEvidenceMetrics(),
        ),
        trial_id=trial_ids[0],
    )

    audit = campaign._audit_historical_registry(
        specs[:-1],
        final_spec=specs[-1],
        all_specs=specs,
        trial_ids=trial_ids,
        experiment_id=experiment_id,
        manifest=manifest,
    )

    assert audit.status is HistoricalEvidenceStatus.CORRUPT
    assert audit.rows[0]["verification"] == "corrupt:RuntimeError"


def test_historical_registry_pins_persisted_derived_evidence_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _manifest_fixture = _campaign(tmp_path, monkeypatch)
    experiment_id, specs, trial_ids, manifest = _minimal_historical_audit_registry(campaign)
    campaign._trial_store.mark_running(trial_ids[0])
    campaign._result_store.save_derived_evidence(
        DerivedBacktestEvidence(
            strategy_id="registered_campaign_v1",
            strategy_semver="1.0.0",
            experiment_id=experiment_id,
            evidence_kind="wrong_evidence_kind",
            symbol="BTC-USDC",
            asset_class="crypto",
            timeframe="1440m",
            start_date=datetime(2022, 1, 1, tzinfo=UTC).date(),
            end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
            payload={
                "runner_kind": "scoring_binding_ledger_pooled",
                "coverage": {
                    "source_signals": 0,
                    "replayed_signals": 0,
                    "missing_signals": 0,
                    "duplicate_signals": 0,
                },
            },
            metrics=DerivedEvidenceMetrics(),
        ),
        trial_id=trial_ids[0],
    )

    audit = campaign._audit_historical_registry(
        specs[:-1],
        final_spec=specs[-1],
        all_specs=specs,
        trial_ids=trial_ids,
        experiment_id=experiment_id,
        manifest=manifest,
    )

    assert audit.status is HistoricalEvidenceStatus.CORRUPT
    assert audit.rows[0]["verification"] == "corrupt:ValueError"


@pytest.mark.parametrize(
    ("evidence_kind", "payload_runner_kind", "error_match"),
    [
        (
            "wrong_evidence_kind",
            "scoring_binding_ledger_pooled",
            "evidence_kind differs",
        ),
        (
            "scoring_binding_ledger_pooled",
            "prospective_power_study",
            "runner kind differs",
        ),
    ],
)
def test_completed_resume_pins_group_evidence_and_payload_runner_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence_kind: str,
    payload_runner_kind: str,
    error_match: str,
) -> None:
    campaign, _manifest_fixture = _campaign(tmp_path, monkeypatch)
    experiment_id, specs, trial_ids, manifest = _minimal_historical_audit_registry(campaign)
    campaign._trial_store.mark_running(trial_ids[0])
    campaign._result_store.save_derived_evidence(
        DerivedBacktestEvidence(
            strategy_id="registered_campaign_v1",
            strategy_semver="1.0.0",
            experiment_id=experiment_id,
            evidence_kind=evidence_kind,
            symbol="BTC-USDC",
            asset_class="crypto",
            timeframe="1440m",
            start_date=datetime(2022, 1, 1, tzinfo=UTC).date(),
            end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
            payload={"runner_kind": payload_runner_kind},
            metrics=DerivedEvidenceMetrics(),
        ),
        trial_id=trial_ids[0],
    )

    with pytest.raises((RuntimeError, ValueError), match=error_match):
        campaign._audit_completed_evidence(
            specs[:1],
            all_specs=specs,
            trial_ids=trial_ids,
            manifest=manifest,
        )


def test_historical_registry_rejects_unregistered_abandonment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _manifest_fixture = _campaign(tmp_path, monkeypatch)
    experiment_id, specs, trial_ids, manifest = _minimal_historical_audit_registry(campaign)
    campaign._trial_store.mark_abandoned(
        trial_ids[0],
        error_context="operator_abandoned_without_registered_policy",
    )

    audit = campaign._audit_historical_registry(
        specs[:-1],
        final_spec=specs[-1],
        all_specs=specs,
        trial_ids=trial_ids,
        experiment_id=experiment_id,
        manifest=manifest,
    )

    assert audit.status is HistoricalEvidenceStatus.CORRUPT
    assert audit.rows[0]["verification"] == "corrupt_unregistered_abandonment"


def test_conditional_sizing_gate_failure_is_persisted_without_preventing_final_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    specs = manifest["trial_registry"]
    trial_ids = _trial_ids_by_sequence(campaign)
    sizing_specs = [
        spec
        for spec in specs
        if spec["runner_kind"]
        in {"conditional_sizing_oos_component", "conditional_sizing_pooled_oos"}
    ]
    final_specs = [
        spec for spec in specs if spec["runner_kind"] == "historical_strategy_disposition"
    ]
    monkeypatch.setattr(
        campaign,
        "_conditional_sizing_gate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("gate corrupt")),
    )

    sizing_failures = campaign._execute_conditional_sizing_group(
        sizing_specs,
        all_specs=specs,
        trial_ids=trial_ids,
        experiment_id=frozen.experiment_id,
        manifest=manifest,
    )
    final_failures = campaign._execute_historical_disposition_group(
        final_specs,
        all_specs=specs,
        trial_ids=trial_ids,
        experiment_id=frozen.experiment_id,
        manifest=manifest,
    )

    assert sizing_failures == len(sizing_specs)
    assert final_failures == 0
    payload = campaign._result_store.load_derived_payload(
        trial_ids[int(final_specs[0]["sequence"])],
        expected_evidence_kind="historical_strategy_disposition",
    )
    assert payload["completion_state"] == "BLOCKED_INSUFFICIENT_EVIDENCE"
    assert payload["economic_disposition"] is None
    assert payload["authority"]["paper_trading_authorized"] is False


@pytest.mark.parametrize(
    (
        "zero_activity",
        "malformed_baseline",
        "contradictory_power",
        "expected_completion",
        "expected_disposition",
    ),
    [
        (False, False, False, "COMPLETE", "FREEZE_FOR_PROSPECTIVE_SHADOW"),
        (True, False, False, "COMPLETE", "RETIRE"),
        (False, True, False, "BLOCKED_INSUFFICIENT_EVIDENCE", None),
        (True, False, True, "BLOCKED_INSUFFICIENT_EVIDENCE", None),
    ],
)
def test_historical_disposition_builder_handles_complete_zero_and_malformed_evidence(
    monkeypatch: pytest.MonkeyPatch,
    zero_activity: bool,
    malformed_baseline: bool,
    contradictory_power: bool,
    expected_completion: str,
    expected_disposition: str | None,
) -> None:
    validator = object.__new__(StrategyValidationCampaign)
    policy_payload = _protocol()["historical_disposition_policy"]
    policy = validator._historical_disposition_policy_from_mapping(policy_payload)
    spec = {
        "sequence": 0,
        "runner_kind": "historical_strategy_disposition",
        "evidence_role": "historical_research_disposition_only",
        "evaluation_start": "2022-01-02T00:00:00+00:00",
        "evaluation_end_exclusive": "2026-01-02T00:00:00+00:00",
        "parameters": {
            "policy": policy_payload,
            "expected_prior_trial_count": 0,
            "historical_authority_only": True,
            "all_registered_trials_must_be_audited": True,
            "recursive_source_hash_reconciliation_required": True,
            "paper_trading_authorized": False,
            "live_trading_authorized": False,
            "automatic_parameter_deployment_authorized": False,
            "semantic_finding_candidate_map": {},
            "predeclared_abandonments": [],
            "conditional_abandonment_runner_kinds": sorted(
                campaign_module._CONDITIONAL_SIZING_GROUP_RUNNERS
            ),
        },
    }
    audit = campaign_module._HistoricalRegistryAudit(
        status=HistoricalEvidenceStatus.VERIFIED,
        rows=(),
        report_sources=(),
        derived_sources=(),
        derived_payloads={},
    )
    digest = "a" * 64
    observations = dict.fromkeys(
        campaign_module._HISTORICAL_SOURCE_IDS,
        (HistoricalEvidenceStatus.VERIFIED, digest),
    )
    baseline = FixedBaselineDispositionSummary(
        arm_id=policy.baseline_arm_id,
        gate_checks=tuple(
            HistoricalGateCheck(gate_id, not zero_activity)
            for gate_id in policy.baseline_quantitative_gate_ids
        ),
        zero_trades_or_exposure=zero_activity,
    )
    redesign = RegisteredRedesignDispositionSummary(
        candidates=tuple(
            RegisteredRedesignCandidateSummary(
                candidate_id=candidate_id,
                gate_checks=tuple(
                    HistoricalGateCheck(gate_id, False)
                    for gate_id in policy.redesign_quantitative_gate_ids
                ),
                zero_trades_or_exposure=True,
            )
            for candidate_id in policy.registered_redesign_candidate_ids
        )
    )
    power_zero_activity = not zero_activity if contradictory_power else zero_activity
    power = ProspectivePowerDispositionSummary(
        primary_effect_id=policy.prospective_primary_effect_id,
        maximum_duration_years=policy.maximum_prospective_duration_years,
        powered_within_maximum_duration=not power_zero_activity,
        operationally_impractical=power_zero_activity,
        not_applicable_due_to_zero_trades=power_zero_activity,
    )
    monkeypatch.setattr(validator, "_audit_historical_registry", lambda *_args, **_kwargs: audit)
    monkeypatch.setattr(
        validator,
        "_historical_source_observations",
        lambda *_args, **_kwargs: observations,
    )
    monkeypatch.setattr(
        validator,
        "_historical_scoring_source_observation",
        lambda *_args, **_kwargs: (HistoricalEvidenceStatus.VERIFIED, digest),
    )
    monkeypatch.setattr(
        validator,
        "_historical_power_source_observation",
        lambda *_args, **_kwargs: (HistoricalEvidenceStatus.VERIFIED, digest),
    )
    monkeypatch.setattr(
        validator,
        "_historical_robustness_source_observations",
        lambda *_args, **_kwargs: (
            (HistoricalEvidenceStatus.VERIFIED, digest),
            (HistoricalEvidenceStatus.VERIFIED, digest),
        ),
    )
    monkeypatch.setattr(
        validator,
        "_historical_redesign_source_status",
        lambda *_args, **_kwargs: HistoricalEvidenceStatus.VERIFIED,
    )
    monkeypatch.setattr(
        validator,
        "_historical_baseline_summary",
        lambda *_args, **_kwargs: None if malformed_baseline else baseline,
    )
    monkeypatch.setattr(
        validator,
        "_historical_redesign_summary",
        lambda *_args, **_kwargs: redesign,
    )
    monkeypatch.setattr(
        validator,
        "_historical_power_summary",
        lambda *_args, **_kwargs: power,
    )
    monkeypatch.setattr(
        validator,
        "_historical_correctness_summary",
        lambda *_args, **_kwargs: CorrectnessDispositionSummary(()),
    )
    manifest = {
        "protocol_id": "historical-disposition-test-v1",
        "strategy": {
            "strategy_id": "registered_campaign_v1",
            "strategy_version": "1.0.0",
        },
        "habitat": {"asset_class_db": "crypto"},
        "data": {
            "datasets": [
                {"requested_product": "BTC-USDC", "timeframe": "1d"},
                {"requested_product": "ETH-USDC", "timeframe": "1d"},
            ]
        },
    }

    records = validator._build_historical_disposition_evidence(
        [spec],
        all_specs=[spec],
        trial_ids={0: "final-trial"},
        experiment_id=1,
        manifest=manifest,
    )
    payload = records[0][1].payload

    assert payload["completion_state"] == expected_completion
    assert payload["economic_disposition"] == expected_disposition
    assert payload["paper_trading_authorized"] is False
    assert payload["authority"]["paper_trading_authorized"] is False


def test_completed_historical_disposition_resume_rederives_and_compares_full_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, _manifest_fixture = _campaign(tmp_path, monkeypatch)
    experiment_id, specs, trial_ids, manifest = _minimal_historical_audit_registry(campaign)
    final_spec = specs[-1]
    final_trial_id = trial_ids[int(final_spec["sequence"])]
    stored_payload = {
        "completion_state": "BLOCKED_INSUFFICIENT_EVIDENCE",
        "economic_disposition": None,
        "frozen_arm_id": None,
        "blocker_codes": ["evidence:trial_registry:missing"],
        "reason_codes": [],
        "source_hash_catalog": {"trial_registry": {"status": "verified", "sha256": "a" * 64}},
        "authority": {
            "paper_trading_authorized": False,
            "live_trading_authorized": False,
            "automatic_parameter_deployment_authorized": False,
        },
    }
    campaign._trial_store.mark_running(final_trial_id)
    stored_evidence = DerivedBacktestEvidence(
        strategy_id="registered_campaign_v1",
        strategy_semver="1.0.0",
        experiment_id=experiment_id,
        evidence_kind="historical_strategy_disposition",
        symbol="BTC-USDC",
        asset_class="crypto",
        timeframe="1440m",
        start_date=datetime(2022, 1, 1, tzinfo=UTC).date(),
        end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
        payload=stored_payload,
        metrics=DerivedEvidenceMetrics(),
    )
    campaign._result_store.save_derived_evidence(stored_evidence, trial_id=final_trial_id)
    campaign._result_store.save_derived_evidence(stored_evidence, trial_id=final_trial_id)
    with Session(campaign._engine) as session:
        assert session.query(BacktestResult).count() == 1
    summary = campaign._historical_disposition_run_summary(specs, trial_ids=trial_ids)
    assert summary["completion_state"] == "BLOCKED_INSUFFICIENT_EVIDENCE"
    assert summary["economic_disposition"] is None
    assert summary["authority"]["paper_trading_authorized"] is False

    def rederived(payload: dict[str, Any]) -> list[tuple[dict[str, Any], DerivedBacktestEvidence]]:
        return [
            (
                final_spec,
                DerivedBacktestEvidence(
                    strategy_id="registered_campaign_v1",
                    strategy_semver="1.0.0",
                    experiment_id=experiment_id,
                    evidence_kind="historical_strategy_disposition",
                    symbol="BTC-USDC",
                    asset_class="crypto",
                    timeframe="1440m",
                    start_date=datetime(2022, 1, 1, tzinfo=UTC).date(),
                    end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
                    payload=payload,
                    metrics=DerivedEvidenceMetrics(),
                ),
            )
        ]

    monkeypatch.setattr(
        campaign,
        "_build_historical_disposition_evidence",
        lambda *_args, **_kwargs: rederived(deepcopy(stored_payload)),
    )
    campaign._reaudit_completed_historical_disposition(
        final_spec,
        all_specs=specs,
        trial_ids=trial_ids,
        manifest=manifest,
    )

    changed = deepcopy(stored_payload)
    changed["source_hash_catalog"]["trial_registry"]["sha256"] = "b" * 64
    monkeypatch.setattr(
        campaign,
        "_build_historical_disposition_evidence",
        lambda *_args, **_kwargs: rederived(changed),
    )
    with pytest.raises(RuntimeError, match="differs from re-derived evidence"):
        campaign._reaudit_completed_historical_disposition(
            final_spec,
            all_specs=specs,
            trial_ids=trial_ids,
            manifest=manifest,
        )


def test_freeze_only_resume_audits_completed_verdict_under_frozen_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign, manifest = _campaign(tmp_path, monkeypatch)
    frozen = campaign.run(strategy_path=_REGISTERED_CAMPAIGN, freeze_only=True)
    specs = manifest["trial_registry"]
    trial_ids = _trial_ids_by_sequence(campaign)
    final_spec = next(
        spec for spec in specs if spec["runner_kind"] == "historical_strategy_disposition"
    )
    final_trial_id = trial_ids[int(final_spec["sequence"])]
    campaign._trial_store.mark_running(final_trial_id)
    campaign._result_store.save_derived_evidence(
        DerivedBacktestEvidence(
            strategy_id="registered_campaign_v1",
            strategy_semver="1.0.0",
            experiment_id=frozen.experiment_id,
            evidence_kind="historical_strategy_disposition",
            symbol="BTC-USDC+ETH-USDC",
            asset_class="crypto",
            timeframe="1440m",
            start_date=datetime(2022, 1, 1, tzinfo=UTC).date(),
            end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
            payload={
                "completion_state": "BLOCKED_INSUFFICIENT_EVIDENCE",
                "economic_disposition": None,
                "frozen_arm_id": None,
                "blocker_codes": ["missing_required_evidence:trial-ledger"],
                "reason_codes": ["no_economic_disposition_when_evidence_is_incomplete"],
                "authority": {
                    "paper_trading_authorized": False,
                    "live_trading_authorized": False,
                    "automatic_parameter_deployment_authorized": False,
                },
            },
            metrics=DerivedEvidenceMetrics(),
        ),
        trial_id=final_trial_id,
    )
    runtime_calls: list[str] = []
    audit_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        campaign,
        "_require_frozen_runtime_environment",
        lambda *_args: runtime_calls.append("runtime") or {},
    )
    monkeypatch.setattr(
        campaign,
        "_audit_completed_evidence",
        lambda completed, **_kwargs: audit_calls.append(
            tuple(str(spec["runner_kind"]) for spec in completed)
        ),
    )
    monkeypatch.setattr(
        campaign,
        "_execute_phase_one",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("freeze-only resume must not execute trials")
        ),
    )

    resumed = campaign.run(
        strategy_path=_REGISTERED_CAMPAIGN,
        manifest_hash=frozen.manifest_hash,
        freeze_only=True,
    )

    assert runtime_calls == ["runtime"]
    assert audit_calls == [("historical_strategy_disposition",)]
    assert resumed.historical_disposition["completion_state"] == ("BLOCKED_INSUFFICIENT_EVIDENCE")


def test_malformed_historical_gate_and_candidate_sets_are_not_typed_as_complete() -> None:
    validator = object.__new__(StrategyValidationCampaign)
    policy = validator._historical_disposition_policy_from_mapping(
        _protocol()["historical_disposition_policy"]
    )
    incomplete_baseline = {
        "disposition_source_arm_id": policy.baseline_arm_id,
        "quantitative_gate_checks": {
            policy.baseline_quantitative_gate_ids[0]: True,
        },
        "raw_baseline_zero_trades_or_exposure": False,
    }
    malformed_redesign = {
        "redesign_candidates": {
            "registered_candidate_order": list(policy.registered_redesign_candidate_ids),
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "gate_checks": {policy.redesign_quantitative_gate_ids[0]: True},
                    "raw_zero_trades_or_exposure": False,
                }
                for candidate_id in policy.registered_redesign_candidate_ids
            ],
        }
    }
    audit = campaign_module._HistoricalRegistryAudit(
        status=HistoricalEvidenceStatus.VERIFIED,
        rows=(),
        report_sources=(),
        derived_sources=(),
        derived_payloads={
            "robustness_concentration_inference": (({}, incomplete_baseline),),
            "registered_redesign_candidate_inference": (({}, malformed_redesign),),
        },
    )

    assert validator._historical_baseline_summary(audit, policy=policy) is None
    assert validator._historical_redesign_summary(audit, policy=policy) is None

    invalid_identifier_redesign = {
        "redesign_candidates": {
            "registered_candidate_order": list(policy.registered_redesign_candidate_ids),
            "candidates": [
                {
                    "candidate_id": "" if index == 0 else candidate_id,
                    "gate_checks": dict.fromkeys(
                        policy.redesign_quantitative_gate_ids,
                        True,
                    ),
                    "raw_zero_trades_or_exposure": False,
                }
                for index, candidate_id in enumerate(policy.registered_redesign_candidate_ids)
            ],
        }
    }
    invalid_audit = campaign_module._HistoricalRegistryAudit(
        status=HistoricalEvidenceStatus.VERIFIED,
        rows=(),
        report_sources=(),
        derived_sources=(),
        derived_payloads={
            "registered_redesign_candidate_inference": (({}, invalid_identifier_redesign),),
        },
    )
    assert validator._historical_redesign_summary(invalid_audit, policy=policy) is None


def test_historical_scoring_observation_rejects_hash_valid_but_incomplete_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = object.__new__(StrategyValidationCampaign)
    spec = {
        "sequence": 0,
        "runner_kind": "scoring_binding_ledger_pooled",
    }
    weak_payload = {
        "runner_kind": "scoring_binding_ledger_pooled",
        "coverage": {
            "source_signals": 0,
            "replayed_signals": 0,
            "missing_signals": 0,
            "duplicate_signals": 0,
        },
    }
    canonical_payload = {
        **weak_payload,
        "binding_snapshot": {"bindings": [{"binding_id": "1"}]},
        "historical_selection_contaminated": True,
        "same_raw_signal_ledger_no_second_strategy_run": True,
        "source_fold_ids": ["oos-2022"],
        "source_trial_ids": ["source-trial"],
        "scoring_binding_ledger": [],
        "thresholds_evaluated": False,
        "execution_evaluated": False,
    }
    audit = campaign_module._HistoricalRegistryAudit(
        status=HistoricalEvidenceStatus.VERIFIED,
        rows=(),
        report_sources=(),
        derived_sources=(),
        derived_payloads={
            "scoring_binding_ledger_pooled": ((spec, weak_payload),),
        },
    )
    monkeypatch.setattr(
        validator,
        "_build_scoring_ledger_evidence",
        lambda *_args, **_kwargs: [
            (
                spec,
                DerivedBacktestEvidence(
                    strategy_id="registered_campaign_v1",
                    strategy_semver="1.0.0",
                    experiment_id=1,
                    evidence_kind="scoring_binding_ledger_pooled",
                    symbol="BTC-USDC",
                    asset_class="crypto",
                    timeframe="1440m",
                    start_date=datetime(2022, 1, 1, tzinfo=UTC).date(),
                    end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
                    payload=canonical_payload,
                    metrics=DerivedEvidenceMetrics(),
                ),
            )
        ],
    )

    status, digest = validator._historical_scoring_source_observation(
        audit,
        all_specs=[spec],
        trial_ids={0: "scoring-trial"},
        experiment_id=1,
        manifest={},
    )

    assert status is HistoricalEvidenceStatus.CORRUPT
    assert digest is None


def test_historical_power_summary_rejects_wrong_primary_effect_payload() -> None:
    validator = object.__new__(StrategyValidationCampaign)
    policy = validator._historical_disposition_policy_from_mapping(
        _protocol()["historical_disposition_policy"]
    )
    spec = {
        "arm_id": policy.prospective_primary_effect_id,
        "evidence_role": "prospective_design",
        "parameters": {
            "minimum_standardized_effect": 0.2,
            "minimum_standardized_effect_primary": 0.2,
            "maximum_duration_years": 5.0,
            "cadence_source_arm_id": policy.baseline_arm_id,
            "cadence_source_fold_id": "full-panel",
        },
    }
    audit = campaign_module._HistoricalRegistryAudit(
        status=HistoricalEvidenceStatus.VERIFIED,
        rows=(),
        report_sources=(),
        derived_sources=(),
        derived_payloads={
            "prospective_power_study": (
                (
                    spec,
                    {
                        "minimum_standardized_effect": 0.3,
                        "evidence_complete": True,
                        "historical_selection_contaminated": True,
                        "evidence_role": "prospective_design",
                    },
                ),
            )
        },
    )

    assert (
        validator._historical_power_summary(
            audit,
            policy=policy,
            all_specs=[],
            trial_ids={},
            manifest={},
        )
        is None
    )


def test_historical_power_observation_rejects_tampered_design_or_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = object.__new__(StrategyValidationCampaign)
    policy = validator._historical_disposition_policy_from_mapping(
        _protocol()["historical_disposition_policy"]
    )
    spec = {
        "sequence": 0,
        "arm_id": policy.prospective_primary_effect_id,
        "runner_kind": "prospective_power_study",
    }
    canonical_payload = {
        "runner_kind": "prospective_power_study",
        "completed_trade_outcomes": [{"trade_id": "verified-trade"}],
        "power_design": {"minimum_duration_years": 4.5},
    }
    tampered_payload = deepcopy(canonical_payload)
    tampered_payload["power_design"]["minimum_duration_years"] = 1.0
    audit = campaign_module._HistoricalRegistryAudit(
        status=HistoricalEvidenceStatus.VERIFIED,
        rows=(),
        report_sources=(),
        derived_sources=(),
        derived_payloads={"prospective_power_study": ((spec, tampered_payload),)},
    )
    monkeypatch.setattr(
        validator,
        "_build_power_evidence",
        lambda *_args, **_kwargs: [
            (
                spec,
                DerivedBacktestEvidence(
                    strategy_id="registered_campaign_v1",
                    strategy_semver="1.0.0",
                    experiment_id=1,
                    evidence_kind="prospective_power_study",
                    symbol="BTC-USDC",
                    asset_class="crypto",
                    timeframe="1440m",
                    start_date=datetime(2022, 1, 1, tzinfo=UTC).date(),
                    end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
                    payload=canonical_payload,
                    metrics=DerivedEvidenceMetrics(),
                ),
            )
        ],
    )

    status, digest = validator._historical_power_source_observation(
        audit,
        policy=policy,
        all_specs=[spec],
        trial_ids={0: "power-trial"},
        experiment_id=1,
        manifest={},
    )

    assert status is HistoricalEvidenceStatus.CORRUPT
    assert digest is None


def test_historical_robustness_observation_rejects_tampered_gate_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = object.__new__(StrategyValidationCampaign)
    spec = {
        "sequence": 0,
        "runner_kind": "robustness_concentration_inference",
    }
    canonical_payload = {
        "runner_kind": "robustness_concentration_inference",
        "quantitative_gate_checks": {"minimum_trade_count": False},
        "sharpe_diagnostics": {"available": True},
    }
    tampered_payload = deepcopy(canonical_payload)
    tampered_payload["quantitative_gate_checks"]["minimum_trade_count"] = True
    audit = campaign_module._HistoricalRegistryAudit(
        status=HistoricalEvidenceStatus.VERIFIED,
        rows=(),
        report_sources=(),
        derived_sources=(),
        derived_payloads={
            "robustness_concentration_inference": ((spec, tampered_payload),),
        },
    )
    monkeypatch.setattr(
        validator,
        "_build_robustness_evidence",
        lambda *_args, **_kwargs: [
            (
                spec,
                DerivedBacktestEvidence(
                    strategy_id="registered_campaign_v1",
                    strategy_semver="1.0.0",
                    experiment_id=1,
                    evidence_kind="robustness_concentration_inference",
                    symbol="BTC-USDC",
                    asset_class="crypto",
                    timeframe="1440m",
                    start_date=datetime(2022, 1, 1, tzinfo=UTC).date(),
                    end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
                    payload=canonical_payload,
                    metrics=DerivedEvidenceMetrics(),
                ),
            )
        ],
    )

    robustness, sharpe = validator._historical_robustness_source_observations(
        audit,
        all_specs=[spec],
        trial_ids={0: "robustness-trial"},
        experiment_id=1,
        manifest={},
    )

    assert robustness == (HistoricalEvidenceStatus.CORRUPT, None)
    assert sharpe == (HistoricalEvidenceStatus.CORRUPT, None)


def test_historical_redesign_observation_rejects_tampered_gate_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = object.__new__(StrategyValidationCampaign)
    spec = {
        "sequence": 0,
        "runner_kind": "registered_redesign_candidate_inference",
    }
    canonical_payload = {
        "runner_kind": "registered_redesign_candidate_inference",
        "redesign_candidates": {
            "candidates": [
                {
                    "candidate_id": "R1",
                    "gate_checks": {"minimum_trade_count": False},
                }
            ]
        },
    }
    tampered_payload = deepcopy(canonical_payload)
    tampered_payload["redesign_candidates"]["candidates"][0]["gate_checks"][
        "minimum_trade_count"
    ] = True
    audit = campaign_module._HistoricalRegistryAudit(
        status=HistoricalEvidenceStatus.VERIFIED,
        rows=(),
        report_sources=(),
        derived_sources=(),
        derived_payloads={
            "registered_redesign_candidate_inference": ((spec, tampered_payload),),
        },
    )
    monkeypatch.setattr(
        validator,
        "_build_redesign_evidence",
        lambda *_args, **_kwargs: [
            (
                spec,
                DerivedBacktestEvidence(
                    strategy_id="registered_campaign_v1",
                    strategy_semver="1.0.0",
                    experiment_id=1,
                    evidence_kind="registered_redesign_candidate_inference",
                    symbol="BTC-USDC",
                    asset_class="crypto",
                    timeframe="1440m",
                    start_date=datetime(2022, 1, 1, tzinfo=UTC).date(),
                    end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
                    payload=canonical_payload,
                    metrics=DerivedEvidenceMetrics(),
                ),
            )
        ],
    )

    status = validator._historical_redesign_source_status(
        audit,
        all_specs=[spec],
        trial_ids={0: "redesign-trial"},
        experiment_id=1,
        manifest={},
    )

    assert status is HistoricalEvidenceStatus.CORRUPT


def test_historical_typed_observations_preserve_missing_source_provenance() -> None:
    validator = object.__new__(StrategyValidationCampaign)
    observations = dict.fromkeys(
        campaign_module._HISTORICAL_SOURCE_IDS,
        (HistoricalEvidenceStatus.MISSING, None),
    )

    normalized = validator._historical_typed_source_observations(
        observations,
        baseline=None,
        redesign=None,
        power=None,
        correctness=None,
    )

    assert normalized["robustness_concentration_inference"] == (
        HistoricalEvidenceStatus.MISSING,
        None,
    )
    assert normalized["prospective_power_primary_effect"] == (
        HistoricalEvidenceStatus.MISSING,
        None,
    )
    assert normalized["recursive_result_hash_reconciliation"] == (
        HistoricalEvidenceStatus.MISSING,
        None,
    )


def test_trial_sharpe_dispersion_keeps_complete_zero_activity_finite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _protocol()
    candidate_order = tuple(
        [arm["id"] for arm in protocol["signal_rule_arms"]]
        + [
            arm["id"]
            for arm in protocol["semantic_diagnostic_arms"]
            if arm["execution"] == "expected_cost_full_panel_and_direct_oos_diagnostic"
        ]
    )
    config = SimpleNamespace(
        deflated_sharpe_upstream_attempted_trials=4,
        deflated_sharpe_upstream_finite_count=2,
        deflated_sharpe_current_candidate_order=candidate_order,
        annualization_factor=365.0,
    )
    manifest = {
        "multiple_testing": protocol["multiple_testing"],
        "upstream_selection_ledger": _unit_upstream_selection_ledger(protocol),
    }
    validator = object.__new__(StrategyValidationCampaign)

    def complete_zero_returns(
        candidate_id: str,
        **_kwargs: Any,
    ) -> tuple[tuple[TimestampedDailyReturn, ...], list[dict[str, str]]]:
        return (
            (
                TimestampedDailyReturn(datetime(2022, 1, 2, tzinfo=UTC), 0.0),
                TimestampedDailyReturn(datetime(2022, 1, 3, tzinfo=UTC), 0.0),
            ),
            [
                {
                    "trial_id": f"trial-{candidate_id}",
                    "report_sha256": manifest_sha256({"candidate_id": candidate_id}),
                }
            ],
        )

    monkeypatch.setattr(
        validator,
        "_current_candidate_pooled_returns",
        complete_zero_returns,
    )

    evidence, attestation = validator._trial_sharpe_dispersion(
        config=config,
        all_specs=(),
        trial_ids={},
        manifest=manifest,
    )

    assert evidence.upstream_attempted_trials == 4
    assert len(evidence.upstream_finite_sharpes) == 2
    assert evidence.current_candidate_sharpes == tuple(
        (candidate_id, 0.0) for candidate_id in candidate_order
    )
    assert len(evidence.upstream_finite_sharpes) + len(
        evidence.current_candidate_sharpes
    ) == 2 + len(candidate_order)
    assert len(attestation["report_evidence"]) == len(candidate_order)
    assert attestation["embedded_evidence"][0]["manifest_field"] == ("upstream_selection_ledger")


def test_trial_sharpe_dispersion_counts_prior_diagnostics_without_adding_sharpes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    candidate_order = tuple(arm["id"] for arm in protocol["signal_rule_arms"])
    fixture = protocol["correctness_attestation"]["files"][0]
    artifact = {"arms": ["PRIOR_BASELINE", "PRIOR_NEIGHBOR"]}
    internal_sha256 = manifest_sha256(artifact)
    artifact["screen_sha256"] = internal_sha256
    artifact_path = tmp_path / f"prior-screen-{internal_sha256}.json"
    artifact_path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
    fixture["location"] = artifact_path.name
    fixture["sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    protocol["multiple_testing"]["prior_diagnostic_attempts"] = {
        "arm_ids": ["PRIOR_BASELINE", "PRIOR_NEIGHBOR"],
        "attempt_count": 2,
        "correctness_fixture_id": fixture["id"],
        "raw_artifact_sha256": fixture["sha256"],
        "content_addressed_artifact_sha256": internal_sha256,
        "content_sha256_field": "screen_sha256",
        "selection_contaminated": True,
        "authoritative": False,
        "sharpe_dispersion_policy": "count_only_no_finite_sharpe_dispersion",
    }
    config = SimpleNamespace(
        deflated_sharpe_upstream_attempted_trials=6,
        deflated_sharpe_upstream_finite_count=2,
        deflated_sharpe_current_candidate_order=candidate_order,
        annualization_factor=365.0,
    )
    manifest = {
        "correctness_attestation_contract": protocol["correctness_attestation"],
        "multiple_testing": protocol["multiple_testing"],
        "upstream_selection_ledger": _unit_upstream_selection_ledger(protocol),
    }
    validator = object.__new__(StrategyValidationCampaign)
    validator._repo_root = tmp_path

    def complete_returns(
        candidate_id: str,
        **_kwargs: Any,
    ) -> tuple[tuple[TimestampedDailyReturn, ...], list[dict[str, str]]]:
        return (
            (
                TimestampedDailyReturn(datetime(2022, 1, 2, tzinfo=UTC), 0.01),
                TimestampedDailyReturn(datetime(2022, 1, 3, tzinfo=UTC), -0.005),
            ),
            [
                {
                    "trial_id": f"trial-{candidate_id}",
                    "report_sha256": manifest_sha256({"candidate_id": candidate_id}),
                }
            ],
        )

    monkeypatch.setattr(
        validator,
        "_current_candidate_pooled_returns",
        complete_returns,
    )

    evidence, _attestation = validator._trial_sharpe_dispersion(
        config=config,
        all_specs=(),
        trial_ids={},
        manifest=manifest,
    )

    assert evidence.upstream_attempted_trials == 6
    assert len(evidence.upstream_finite_sharpes) == 2
    assert len(evidence.current_candidate_sharpes) == len(candidate_order)
    assert "count_only_prior_diagnostic_attempts" in evidence.source


def test_trial_sharpe_dispersion_blocks_missing_current_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _protocol()
    candidate_order = tuple(
        [arm["id"] for arm in protocol["signal_rule_arms"]]
        + [
            arm["id"]
            for arm in protocol["semantic_diagnostic_arms"]
            if arm["execution"] == "expected_cost_full_panel_and_direct_oos_diagnostic"
        ]
    )
    config = SimpleNamespace(
        deflated_sharpe_upstream_attempted_trials=4,
        deflated_sharpe_upstream_finite_count=2,
        deflated_sharpe_current_candidate_order=candidate_order,
        annualization_factor=365.0,
    )
    manifest = {
        "multiple_testing": protocol["multiple_testing"],
        "upstream_selection_ledger": _unit_upstream_selection_ledger(protocol),
    }
    validator = object.__new__(StrategyValidationCampaign)

    def missing_current(candidate_id: str, **_kwargs: Any) -> Any:
        if candidate_id == candidate_order[-1]:
            raise RuntimeError("current DSR report evidence is absent")
        return (
            (
                TimestampedDailyReturn(datetime(2022, 1, 2, tzinfo=UTC), 0.0),
                TimestampedDailyReturn(datetime(2022, 1, 3, tzinfo=UTC), 0.0),
            ),
            [
                {
                    "trial_id": f"trial-{candidate_id}",
                    "report_sha256": manifest_sha256({"candidate_id": candidate_id}),
                }
            ],
        )

    monkeypatch.setattr(
        validator,
        "_current_candidate_pooled_returns",
        missing_current,
    )

    with pytest.raises(RuntimeError, match="current DSR report evidence is absent"):
        validator._trial_sharpe_dispersion(
            config=config,
            all_specs=(),
            trial_ids={},
            manifest=manifest,
        )


def test_source_attestation_recursively_revalidates_every_hash() -> None:
    validator = object.__new__(StrategyValidationCampaign)
    report_hash = "a" * 64
    nested_attestation = validator._normalized_source_attestation(
        report_sources=[{"trial_id": "report-1", "report_sha256": report_hash}]
    )
    nested_payload = {"source_attestation": nested_attestation, "value": 1}
    nested_payload_hash = manifest_sha256({"payload": nested_payload})
    ledger = {
        "source": {"sha256": "b" * 64},
        "trials": [],
    }
    outer_attestation = validator._normalized_source_attestation(
        derived_sources=[
            {
                "trial_id": "derived-1",
                "evidence_kind": "nested-kind",
                "evidence_payload_sha256": nested_payload_hash,
            }
        ],
        dataset_sources=[{"dataset_id": "dataset-1", "ohlcv_sha256": "c" * 64}],
        embedded_sources=[
            {
                "manifest_field": "upstream_selection_ledger",
                "evidence_payload_sha256": manifest_sha256(ledger),
                "source_sha256": "b" * 64,
            }
        ],
    )

    class ResultStore:
        report_drift = False

        def verified_report_sha256(self, trial_id: str) -> str:
            assert trial_id == "report-1"
            return "d" * 64 if self.report_drift else report_hash

        def load_derived_payload(
            self,
            trial_id: str,
            *,
            expected_evidence_kind: str,
        ) -> dict[str, Any]:
            assert (trial_id, expected_evidence_kind) == (
                "derived-1",
                "nested-kind",
            )
            return deepcopy(nested_payload)

    store = ResultStore()
    validator._result_store = store
    manifest = {
        "data": {"datasets": [{"dataset_id": "dataset-1", "audit": {"ohlcv_sha256": "c" * 64}}]},
        "upstream_selection_ledger": ledger,
    }
    payload = {"source_attestation": outer_attestation}

    validator._audit_source_attestation(
        payload,
        manifest=manifest,
        visited_derived=set(),
    )
    store.report_drift = True
    with pytest.raises(RuntimeError, match="completed source report hash differs"):
        validator._audit_source_attestation(
            payload,
            manifest=manifest,
            visited_derived=set(),
        )


def test_installed_core_loader_requires_active_prefix_and_matching_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "RegisteredCampaignInstalled"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    core_bytes = (_REGISTERED_CAMPAIGN / "core.py").read_bytes()
    (package / "core.py").write_bytes(core_bytes)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(campaign_environment_module.sys, "prefix", str(tmp_path))

    loaded = load_installed_pure_strategy_core(
        "RegisteredCampaignInstalled.core",
        expected_class_name="RegisteredCampaignCore",
        expected_source_sha256=StrategyValidationCampaign._file_sha256(package / "core.py"),
    )

    assert loaded.__name__ == "RegisteredCampaignCore"
    with pytest.raises(RuntimeError, match="differs from frozen source"):
        load_installed_pure_strategy_core(
            "RegisteredCampaignInstalled.core",
            expected_class_name="RegisteredCampaignCore",
            expected_source_sha256="0" * 64,
        )


def test_campaign_product_metadata_match_is_exact_and_drift_fails_closed(
    tmp_path: Path,
) -> None:
    declared = _protocol()["data"]["primary_products"][0]
    observed = {
        "requested_product_id": declared["requested_product"],
        "product_id": declared["provider_product_id"],
        "canonical_book": declared["expected_canonical_book"],
        "alias": declared["provider_alias"],
        "alias_to": declared["provider_alias_to"],
        "base_currency_id": declared["base_currency"],
        "quote_currency_id": declared["settlement_quote"],
        "quote_display_symbol": declared["quote_display_symbol"],
        "base_increment": declared["base_increment"],
        "quote_increment": declared["quote_increment"],
        "price_increment": declared["price_increment"],
        "product_type": declared["product_type"],
        "status": declared["status"],
        "is_disabled": declared["is_disabled"],
        "trading_disabled": declared["trading_disabled"],
    }
    campaign = StrategyValidationCampaign(
        _engine(),
        repo_root=_REPO,
        artifact_root=tmp_path,
        product_metadata_provider=lambda _product: observed,
        execution_cost_measurement_verifier=verify_coinbase_execution_cost_measurement,
        data_parity_attestation_verifier=lambda _payload: None,
    )

    assert campaign._fetch_and_verify_product_metadata(declared) == observed
    observed["trading_disabled"] = True
    with pytest.raises(ValueError, match="trading_disabled"):
        campaign._fetch_and_verify_product_metadata(declared)
