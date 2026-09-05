"""Historical evidence reconciliation and final disposition."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dev_cli.validation.backtest.engine import RawSignalEvidence
from dev_cli.validation.campaign_composition import CampaignMixinContract
from dev_cli.validation.campaign_contracts import (
    _CONDITIONAL_SIZING_GROUP_RUNNERS,
    _DERIVED_EVIDENCE_KIND_BY_RUNNER,
    _DERIVED_SYMBOL_MAX_LENGTH,
    _HISTORICAL_DISPOSITION_RUNNER,
    _HISTORICAL_SOURCE_IDS,
    _PHASE_ONE_GROUP_RUNNERS,
    _PHASE_ONE_INDIVIDUAL_RUNNERS,
    _POWER_RUNNER,
    _REDESIGN_RUNNER,
    _SCORING_LEDGER_GROUP_RUNNERS,
    _SOURCE_ATTESTED_GROUP_RUNNERS,
    _HistoricalRegistryAudit,
)
from dev_cli.validation.correctness import (
    CorrectnessFindingClass,
    CorrectnessFindingSeverity,
    CorrectnessFindingStatus,
)
from dev_cli.validation.disposition import (
    CorrectnessDispositionSummary,
    FixedBaselineDispositionSummary,
    HistoricalDispositionEvidence,
    HistoricalDispositionPolicy,
    HistoricalEvidenceCheck,
    HistoricalEvidenceSource,
    HistoricalEvidenceStatus,
    HistoricalGateCheck,
    ProspectivePowerDispositionSummary,
    RegisteredRedesignCandidateSummary,
    RegisteredRedesignDispositionSummary,
    SemanticCorrectnessFindingSummary,
    evaluate_historical_strategy_disposition,
)
from dev_cli.validation.persistence.backtest_manifest_store import (
    canonical_manifest_bytes,
    manifest_sha256,
)
from dev_cli.validation.persistence.backtest_result_store import (
    DerivedBacktestEvidence,
    DerivedEvidenceMetrics,
)
from dev_cli.validation.persistence.backtest_trial_store import (
    BacktestTrialStatus,
)
from dev_cli.validation.scoring_replay import (
    ScoringReplayBindingState,
    replay_scoring_raw_signal_ledger,
)
from lib_application.db.models import (
    BacktestResult,
    BacktestTrial,
)


class CampaignDispositionEvidenceMixin(CampaignMixinContract):
    def _execute_historical_disposition_group(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> int:
        """Persist one final historical-only conclusion after every other stage."""

        return self._execute_checkpointed_derived_group(
            specs,
            trial_ids=trial_ids,
            experiment_id=experiment_id,
            group_name="historical strategy disposition",
            build_evidence=lambda: self._build_historical_disposition_evidence(
                specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            ),
        )

    def _build_historical_disposition_evidence(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> list[tuple[Mapping[str, Any], DerivedBacktestEvidence]]:
        """Build a fail-closed conclusion from hash-verified persisted evidence only."""

        if len(specs) != 1 or specs[0].get("runner_kind") != _HISTORICAL_DISPOSITION_RUNNER:
            message = "historical disposition requires exactly one registered final trial"
            raise RuntimeError(message)
        spec = specs[0]
        ordered_specs = sorted(all_specs, key=lambda item: int(item["sequence"]))
        final_sequence = int(spec["sequence"])
        if (
            not ordered_specs
            or ordered_specs[-1] != spec
            or final_sequence != len(ordered_specs) - 1
        ):
            message = "historical disposition must be the final registered trial"
            raise RuntimeError(message)
        parameters = self._mapping(spec, "parameters")
        if int(parameters.get("expected_prior_trial_count", -1)) != final_sequence:
            message = "historical disposition prior-trial count differs from the registry"
            raise RuntimeError(message)
        if any(
            parameters.get(field) is not False
            for field in (
                "paper_trading_authorized",
                "live_trading_authorized",
                "automatic_parameter_deployment_authorized",
            )
        ):
            message = "historical disposition cannot authorize trading or deployment"
            raise RuntimeError(message)
        if (
            parameters.get("historical_authority_only") is not True
            or parameters.get("all_registered_trials_must_be_audited") is not True
            or parameters.get("recursive_source_hash_reconciliation_required") is not True
        ):
            message = "historical disposition safety contract is incomplete"
            raise RuntimeError(message)

        policy = self._historical_disposition_policy_from_mapping(
            self._mapping(parameters, "policy")
        )
        prior_specs = ordered_specs[:-1]
        expected_predeclared_abandonments = [
            {
                "sequence": int(candidate["sequence"]),
                "runner_kind": str(candidate["runner_kind"]),
                "exact_reason": str(candidate["abandonment_reason"]),
            }
            for candidate in prior_specs
            if candidate.get("registration_status") == BacktestTrialStatus.ABANDONED.value
        ]
        if parameters.get("predeclared_abandonments") != (
            expected_predeclared_abandonments
        ) or parameters.get("conditional_abandonment_runner_kinds") != sorted(
            _CONDITIONAL_SIZING_GROUP_RUNNERS
        ):
            message = "historical disposition abandonment contract differs from the registry"
            raise RuntimeError(message)
        audit = self._audit_historical_registry(
            prior_specs,
            final_spec=spec,
            all_specs=ordered_specs,
            trial_ids=trial_ids,
            experiment_id=experiment_id,
            manifest=manifest,
        )
        observations = self._historical_source_observations(
            audit,
            policy=policy,
            manifest=manifest,
        )
        observations["scoring_binding_ledger_pooled"] = self._historical_scoring_source_observation(
            audit,
            all_specs=prior_specs,
            trial_ids=trial_ids,
            experiment_id=experiment_id,
            manifest=manifest,
        )
        observations["prospective_power_primary_effect"] = (
            self._historical_power_source_observation(
                audit,
                policy=policy,
                all_specs=prior_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            )
        )
        observations = self._historical_rederived_source_observations(
            observations,
            audit=audit,
            all_specs=prior_specs,
            trial_ids=trial_ids,
            experiment_id=experiment_id,
            manifest=manifest,
        )
        baseline = self._historical_baseline_summary(audit, policy=policy)
        redesign = self._historical_redesign_summary(audit, policy=policy)
        power = self._historical_power_summary(
            audit,
            policy=policy,
            all_specs=prior_specs,
            trial_ids=trial_ids,
            manifest=manifest,
        )
        correctness = self._historical_correctness_summary(
            manifest,
            parameters=parameters,
        )
        observations, baseline, power, reconciliation_errors = (
            self._reconcile_historical_summary_consistency(
                observations,
                baseline=baseline,
                power=power,
            )
        )
        observations = self._historical_typed_source_observations(
            observations,
            baseline=baseline,
            redesign=redesign,
            power=power,
            correctness=correctness,
        )
        checks = self._historical_evidence_checks(
            policy,
            observations=observations,
            baseline=baseline,
        )
        evidence = HistoricalDispositionEvidence(
            checks=checks,
            baseline=baseline,
            redesign=redesign,
            power=power,
            correctness=correctness,
        )
        try:
            result = evaluate_historical_strategy_disposition(policy, evidence)
        except (TypeError, ValueError) as exc:
            # Policy shape is validated before manifest freeze. Any remaining
            # typed-policy rejection therefore represents malformed or
            # contradictory persisted evidence and must yield a durable BLOCKED
            # conclusion rather than failing the final checkpoint itself.
            reconciliation_errors.append(
                {
                    "error_class": type(exc).__name__,
                    "error_context": self._safe_error_context(exc),
                }
            )
            for source_id in (
                "recursive_result_hash_reconciliation",
                "robustness_concentration_inference",
                "prospective_power_primary_effect",
                "correctness_attestation",
            ):
                observations[source_id] = (HistoricalEvidenceStatus.CORRUPT, None)
            baseline = None
            redesign = None
            power = None
            correctness = None
            checks = self._historical_evidence_checks(
                policy,
                observations=observations,
                baseline=None,
            )
            result = evaluate_historical_strategy_disposition(
                policy,
                HistoricalDispositionEvidence(
                    checks=checks,
                    baseline=None,
                    redesign=None,
                    power=None,
                    correctness=None,
                ),
            )
        payload = {
            **result.to_dict(),
            "evidence_role": spec["evidence_role"],
            "historical_selection_contaminated": True,
            "protocol_id": manifest["protocol_id"],
            "runner_kind": spec["runner_kind"],
            "registry_audit": {
                "status": audit.status.value,
                "all_prior_trials_audited": True,
                "expected_prior_trial_count": final_sequence,
                "observed_prior_trial_count": len(audit.rows),
                "trials": list(audit.rows),
            },
            "source_hash_catalog": {
                source_id: {
                    "status": status.value,
                    "sha256": digest,
                }
                for source_id, (status, digest) in sorted(observations.items())
            },
            "typed_evidence_reconciliation_errors": reconciliation_errors,
            "historical_authority_only": True,
            "paper_trading_authorized": False,
            "live_trading_authorized": False,
            "automatic_parameter_deployment_authorized": False,
        }
        strategy = self._mapping(manifest, "strategy")
        start_date, end_date = self._evidence_dates(spec)
        symbol = "+".join(
            sorted(
                str(dataset["requested_product"])
                for dataset in self._list_of_mappings(self._mapping(manifest, "data"), "datasets")
            )
        )
        if len(symbol) > _DERIVED_SYMBOL_MAX_LENGTH:
            message = "historical disposition symbol exceeds database limit"
            raise ValueError(message)
        return [
            (
                spec,
                DerivedBacktestEvidence(
                    strategy_id=str(strategy["strategy_id"]),
                    strategy_semver=str(strategy["strategy_version"]),
                    experiment_id=experiment_id,
                    evidence_kind=_HISTORICAL_DISPOSITION_RUNNER,
                    symbol=symbol,
                    asset_class=str(self._mapping(manifest, "habitat")["asset_class_db"]),
                    timeframe=self._common_dataset_value(manifest, "timeframe"),
                    start_date=start_date,
                    end_date=end_date,
                    payload=payload,
                    metrics=DerivedEvidenceMetrics(),
                ),
            )
        ]

    @staticmethod
    def _reconcile_historical_summary_consistency(
        observations: Mapping[str, tuple[HistoricalEvidenceStatus, str | None]],
        *,
        baseline: FixedBaselineDispositionSummary | None,
        power: ProspectivePowerDispositionSummary | None,
    ) -> tuple[
        dict[str, tuple[HistoricalEvidenceStatus, str | None]],
        FixedBaselineDispositionSummary | None,
        ProspectivePowerDispositionSummary | None,
        list[dict[str, str]],
    ]:
        """Convert cross-summary contradictions into explicit corrupt evidence."""

        normalized = dict(observations)
        conflict = (
            baseline is not None
            and power is not None
            and baseline.zero_trades_or_exposure != power.not_applicable_due_to_zero_trades
        )
        if not conflict:
            return normalized, baseline, power, []
        normalized["robustness_concentration_inference"] = (
            HistoricalEvidenceStatus.CORRUPT,
            None,
        )
        normalized["prospective_power_primary_effect"] = (
            HistoricalEvidenceStatus.CORRUPT,
            None,
        )
        return (
            normalized,
            None,
            None,
            [
                {
                    "error_class": "HistoricalEvidenceContradiction",
                    "error_context": "baseline and power zero-trade states contradict",
                }
            ],
        )

    @staticmethod
    def _historical_typed_source_observations(
        observations: Mapping[str, tuple[HistoricalEvidenceStatus, str | None]],
        *,
        baseline: FixedBaselineDispositionSummary | None,
        redesign: RegisteredRedesignDispositionSummary | None,
        power: ProspectivePowerDispositionSummary | None,
        correctness: CorrectnessDispositionSummary | None,
    ) -> dict[str, tuple[HistoricalEvidenceStatus, str | None]]:
        """Mark hash-valid but malformed decision payloads as corrupt evidence."""

        normalized = dict(observations)
        malformed_sources = (
            (baseline is None, "robustness_concentration_inference"),
            (redesign is None, "recursive_result_hash_reconciliation"),
            (power is None, "prospective_power_primary_effect"),
            (correctness is None, "correctness_attestation"),
        )
        for malformed, source_id in malformed_sources:
            if malformed and normalized[source_id][0].complete:
                normalized[source_id] = (HistoricalEvidenceStatus.CORRUPT, None)
        return normalized

    def _audit_historical_registry(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        final_spec: Mapping[str, Any],
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> _HistoricalRegistryAudit:
        """Audit every prior lifecycle row and every completed result recursively."""

        expected_ids = {trial_ids[int(spec["sequence"])] for spec in (*specs, final_spec)}
        with Session(self._engine) as session:
            db_rows = list(
                session.execute(
                    select(BacktestTrial)
                    .where(BacktestTrial.experiment_id == experiment_id)
                    .order_by(BacktestTrial.sequence)
                ).scalars()
            )
        row_by_id = {str(row.trial_id): row for row in db_rows}
        issue_status: HistoricalEvidenceStatus | None = None
        if set(row_by_id) != expected_ids:
            issue_status = HistoricalEvidenceStatus.CORRUPT

        report_sources: list[dict[str, str]] = []
        derived_sources: list[dict[str, str]] = []
        payloads: dict[str, list[tuple[Mapping[str, Any], dict[str, Any]]]] = {}
        audit_rows: list[dict[str, Any]] = []

        def record_issue(status: HistoricalEvidenceStatus) -> None:
            nonlocal issue_status
            if status is HistoricalEvidenceStatus.CORRUPT or issue_status is None:
                issue_status = status

        for spec in specs:
            sequence = int(spec["sequence"])
            trial_id = trial_ids.get(sequence)
            row = row_by_id.get(str(trial_id)) if trial_id is not None else None
            audit_row: dict[str, Any] = {
                "sequence": sequence,
                "trial_id": trial_id,
                "runner_kind": str(spec["runner_kind"]),
                "status": None if row is None else str(row.status),
                "result_sha256": None,
                "verification": None,
            }
            if row is None:
                audit_row["verification"] = "missing_registered_trial"
                record_issue(HistoricalEvidenceStatus.MISSING)
                audit_rows.append(audit_row)
                continue
            status = str(row.status)
            if status == BacktestTrialStatus.COMPLETED.value:
                try:
                    runner_kind = str(spec["runner_kind"])
                    digest, evidence_kind, payload = self._verified_historical_trial_result(
                        runner_kind=runner_kind,
                        trial_id=str(row.trial_id),
                        manifest=manifest,
                    )
                    if evidence_kind is None:
                        report_sources.append(
                            {"trial_id": str(row.trial_id), "report_sha256": digest}
                        )
                    else:
                        assert payload is not None
                        derived_sources.append(
                            {
                                "trial_id": str(row.trial_id),
                                "evidence_kind": evidence_kind,
                                "evidence_payload_sha256": digest,
                            }
                        )
                        payloads.setdefault(runner_kind, []).append((spec, payload))
                    audit_row["result_sha256"] = digest
                    audit_row["verification"] = "verified"
                except Exception as exc:  # final ledger records, rather than hides, corruption
                    audit_row["verification"] = f"corrupt:{type(exc).__name__}"
                    record_issue(HistoricalEvidenceStatus.CORRUPT)
            elif status == BacktestTrialStatus.ABANDONED.value:
                audit_row["verification"] = "pending_abandonment_reconciliation"
            elif status in {
                BacktestTrialStatus.REGISTERED.value,
                BacktestTrialStatus.RUNNING.value,
            }:
                audit_row["verification"] = f"incomplete:{status}"
                record_issue(HistoricalEvidenceStatus.MISSING)
            else:
                audit_row["verification"] = f"invalid_terminal_status:{status}"
                record_issue(HistoricalEvidenceStatus.CORRUPT)
            audit_rows.append(audit_row)

        self._reconcile_historical_abandonments(
            specs,
            audit_rows=audit_rows,
            row_by_id=row_by_id,
            trial_ids=trial_ids,
            all_specs=all_specs,
            record_issue=record_issue,
        )
        status = issue_status or HistoricalEvidenceStatus.VERIFIED
        return _HistoricalRegistryAudit(
            status=status,
            rows=tuple(audit_rows),
            report_sources=tuple(sorted(report_sources, key=lambda item: item["trial_id"])),
            derived_sources=tuple(
                sorted(
                    derived_sources,
                    key=lambda item: (item["trial_id"], item["evidence_kind"]),
                )
            ),
            derived_payloads={
                key: tuple(sorted(value, key=lambda item: int(item[0]["sequence"])))
                for key, value in payloads.items()
            },
        )

    def _verified_historical_trial_result(
        self,
        *,
        runner_kind: str,
        trial_id: str,
        manifest: Mapping[str, Any],
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        if runner_kind in _PHASE_ONE_INDIVIDUAL_RUNNERS:
            return self._result_store.verified_report_sha256(trial_id), None, None
        if runner_kind not in _PHASE_ONE_GROUP_RUNNERS - {_HISTORICAL_DISPOSITION_RUNNER}:
            message = f"unsupported completed runner kind: {runner_kind}"
            raise RuntimeError(message)
        payload = self._result_store.load_derived_payload(
            trial_id,
            expected_evidence_kind=self._expected_derived_evidence_kind(runner_kind),
        )
        if payload.get("runner_kind") != runner_kind:
            message = "completed derived result runner kind differs"
            raise RuntimeError(message)
        source_attestation = payload.get("source_attestation")
        if runner_kind in _SOURCE_ATTESTED_GROUP_RUNNERS and not isinstance(
            source_attestation, Mapping
        ):
            message = "completed source-attested result has no source attestation"
            raise RuntimeError(message)
        if isinstance(source_attestation, Mapping):
            self._audit_source_attestation(
                payload,
                manifest=manifest,
                visited_derived=set(),
            )
        return (
            manifest_sha256({"payload": payload}),
            self._completed_derived_evidence_kind(trial_id),
            payload,
        )

    @staticmethod
    def _expected_derived_evidence_kind(runner_kind: str) -> str:
        try:
            return _DERIVED_EVIDENCE_KIND_BY_RUNNER[runner_kind]
        except KeyError as exc:
            message = f"runner has no frozen derived evidence-kind contract: {runner_kind}"
            raise ValueError(message) from exc

    def _completed_derived_evidence_kind(self, trial_id: str) -> str:
        with Session(self._engine) as session:
            trial = session.get(BacktestTrial, trial_id)
            result = (
                session.get(BacktestResult, trial.result_id)
                if trial is not None and trial.result_id is not None
                else None
            )
            if result is None or not isinstance(result.meta, Mapping):
                message = "completed derived result has no persisted evidence identity"
                raise RuntimeError(message)
            return self._nonempty_string(result.meta.get("evidence_kind"), field="evidence_kind")

    def _reconcile_historical_abandonments(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        audit_rows: list[dict[str, Any]],
        row_by_id: Mapping[str, BacktestTrial],
        trial_ids: Mapping[int, str],
        all_specs: Sequence[Mapping[str, Any]],
        record_issue: Callable[[HistoricalEvidenceStatus], None],
    ) -> None:
        """Accept only exact registered or conditionally-derived abandonments."""

        spec_by_sequence = {int(spec["sequence"]): spec for spec in specs}
        audit_by_sequence = {int(row["sequence"]): row for row in audit_rows}
        conditional_specs = [
            spec for spec in specs if spec.get("runner_kind") in _CONDITIONAL_SIZING_GROUP_RUNNERS
        ]
        conditional_abandoned = any(
            audit_by_sequence[int(spec["sequence"])]["status"]
            == BacktestTrialStatus.ABANDONED.value
            for spec in conditional_specs
        )
        conditional_reason: str | None = None
        conditional_valid = True
        if conditional_abandoned:
            try:
                eligible, conditional_reason = self._conditional_sizing_gate(
                    all_specs,
                    trial_ids=trial_ids,
                )
                conditional_valid = not eligible and all(
                    audit_by_sequence[int(spec["sequence"])]["status"]
                    == BacktestTrialStatus.ABANDONED.value
                    for spec in conditional_specs
                )
            except Exception:
                conditional_valid = False

        for sequence, spec in sorted(spec_by_sequence.items()):
            audit_row = audit_by_sequence[sequence]
            if audit_row["status"] != BacktestTrialStatus.ABANDONED.value:
                continue
            trial_id = trial_ids[sequence]
            row = row_by_id[trial_id]
            exact_reason = spec.get("abandonment_reason")
            predeclared = (
                spec.get("registration_status") == BacktestTrialStatus.ABANDONED.value
                and isinstance(exact_reason, str)
                and row.error_class == "Abandoned"
                and row.error_context == exact_reason
                and row.result_id is None
            )
            conditional = (
                spec.get("runner_kind") in _CONDITIONAL_SIZING_GROUP_RUNNERS
                and conditional_valid
                and conditional_reason is not None
                and row.error_class == "Abandoned"
                and row.error_context == conditional_reason
                and row.result_id is None
            )
            if predeclared:
                audit_row["verification"] = "verified_predeclared_abandonment"
            elif conditional:
                audit_row["verification"] = "verified_conditional_abandonment"
            else:
                audit_row["verification"] = "corrupt_unregistered_abandonment"
                record_issue(HistoricalEvidenceStatus.CORRUPT)

    def _historical_source_observations(
        self,
        audit: _HistoricalRegistryAudit,
        *,
        policy: HistoricalDispositionPolicy,
        manifest: Mapping[str, Any],
    ) -> dict[str, tuple[HistoricalEvidenceStatus, str | None]]:
        """Resolve every stable policy source alias to one observed digest."""

        observations: dict[str, tuple[HistoricalEvidenceStatus, str | None]] = {}
        audit_payload = {
            "rows": list(audit.rows),
            "report_sources": list(audit.report_sources),
            "derived_sources": list(audit.derived_sources),
        }
        audit_digest = manifest_sha256(audit_payload) if audit.complete else None
        observations["trial_registry"] = (audit.status, audit_digest)
        observations["recursive_result_hash_reconciliation"] = (
            audit.status,
            audit_digest,
        )

        for source_id, field, wrapper in (
            ("frozen_market_data_datasets", "data", "datasets"),
            ("data_parity_attestation", "data_parity_attestation", None),
            ("execution_cost_measurement", "execution_cost_measurement", None),
            ("correctness_attestation", "correctness_attestation", None),
        ):
            try:
                value: Any = self._mapping(manifest, field)
                if wrapper is not None:
                    value = self._list_of_mappings(value, wrapper)
                observations[source_id] = (
                    HistoricalEvidenceStatus.VERIFIED,
                    manifest_sha256({source_id: value}),
                )
            except Exception:
                observations[source_id] = (HistoricalEvidenceStatus.CORRUPT, None)

        robustness = self._single_historical_payload(
            audit,
            runner_kind="robustness_concentration_inference",
        )
        if robustness is None:
            state = (
                audit.status
                if audit.status is not HistoricalEvidenceStatus.VERIFIED
                else HistoricalEvidenceStatus.MISSING
            )
            observations["robustness_concentration_inference"] = (state, None)
            observations["deflated_sharpe_diagnostics"] = (state, None)
        else:
            digest = manifest_sha256({"payload": robustness})
            observations["robustness_concentration_inference"] = (
                HistoricalEvidenceStatus.VERIFIED,
                digest,
            )
            sharpe = robustness.get("sharpe_diagnostics")
            dsr_status = (
                HistoricalEvidenceStatus.VERIFIED
                if isinstance(sharpe, Mapping) and sharpe.get("available") is True
                else HistoricalEvidenceStatus.MISSING
            )
            observations["deflated_sharpe_diagnostics"] = (
                dsr_status,
                digest if dsr_status.complete else None,
            )

        primary_power = self._primary_power_payload(audit, policy=policy)
        observations["prospective_power_primary_effect"] = (
            (HistoricalEvidenceStatus.VERIFIED, manifest_sha256({"payload": primary_power}))
            if primary_power is not None
            else (HistoricalEvidenceStatus.MISSING, None)
        )
        scoring = self._single_historical_payload(
            audit,
            runner_kind="scoring_binding_ledger_pooled",
        )
        scoring_status = HistoricalEvidenceStatus.MISSING
        if scoring is not None:
            coverage = scoring.get("coverage")
            scoring_status = (
                HistoricalEvidenceStatus.VERIFIED
                if isinstance(coverage, Mapping)
                and coverage.get("missing_signals") == 0
                and coverage.get("duplicate_signals") == 0
                and coverage.get("source_signals") == coverage.get("replayed_signals")
                else HistoricalEvidenceStatus.CORRUPT
            )
        observations["scoring_binding_ledger_pooled"] = (
            scoring_status,
            manifest_sha256({"payload": scoring}) if scoring_status.complete else None,
        )
        if set(observations) != _HISTORICAL_SOURCE_IDS:
            message = "historical source observation coverage differs from the policy vocabulary"
            raise RuntimeError(message)
        return observations

    def _historical_rederived_source_observations(
        self,
        observations: Mapping[str, tuple[HistoricalEvidenceStatus, str | None]],
        *,
        audit: _HistoricalRegistryAudit,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> dict[str, tuple[HistoricalEvidenceStatus, str | None]]:
        """Replace trusted summaries with exact deterministic rebuild observations."""

        normalized = dict(observations)
        robustness, sharpe = self._historical_robustness_source_observations(
            audit,
            all_specs=all_specs,
            trial_ids=trial_ids,
            experiment_id=experiment_id,
            manifest=manifest,
        )
        normalized["robustness_concentration_inference"] = robustness
        normalized["deflated_sharpe_diagnostics"] = sharpe
        redesign_status = self._historical_redesign_source_status(
            audit,
            all_specs=all_specs,
            trial_ids=trial_ids,
            experiment_id=experiment_id,
            manifest=manifest,
        )
        if redesign_status is not HistoricalEvidenceStatus.VERIFIED:
            normalized["recursive_result_hash_reconciliation"] = (
                redesign_status,
                None,
            )
        return normalized

    def _historical_scoring_source_observation(
        self,
        audit: _HistoricalRegistryAudit,
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> tuple[HistoricalEvidenceStatus, str | None]:
        """Rebuild the scoring ledger from verified baseline reports and compare bytes."""

        persisted = self._single_historical_payload(
            audit,
            runner_kind="scoring_binding_ledger_pooled",
        )
        if persisted is None:
            status = (
                audit.status
                if audit.status is not HistoricalEvidenceStatus.VERIFIED
                else HistoricalEvidenceStatus.MISSING
            )
            return status, None
        scoring_specs = [
            spec for spec in all_specs if spec.get("runner_kind") in _SCORING_LEDGER_GROUP_RUNNERS
        ]
        try:
            rebuilt = self._build_scoring_ledger_evidence(
                scoring_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            )
            pooled = [
                evidence.payload
                for spec, evidence in rebuilt
                if spec.get("runner_kind") == "scoring_binding_ledger_pooled"
            ]
            if len(pooled) != 1 or canonical_manifest_bytes(pooled[0]) != (
                canonical_manifest_bytes(persisted)
            ):
                return HistoricalEvidenceStatus.CORRUPT, None
        except (KeyError, RuntimeError, TypeError, ValueError):
            return HistoricalEvidenceStatus.CORRUPT, None
        return (
            HistoricalEvidenceStatus.VERIFIED,
            manifest_sha256({"payload": persisted}),
        )

    def _historical_power_source_observation(
        self,
        audit: _HistoricalRegistryAudit,
        *,
        policy: HistoricalDispositionPolicy,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> tuple[HistoricalEvidenceStatus, str | None]:
        """Rebuild all power rows from verified baseline reports and compare exact bytes."""

        persisted_rows = audit.derived_payloads.get(_POWER_RUNNER, ())
        if not persisted_rows:
            status = (
                audit.status
                if audit.status is not HistoricalEvidenceStatus.VERIFIED
                else HistoricalEvidenceStatus.MISSING
            )
            return status, None
        power_specs = [spec for spec in all_specs if spec.get("runner_kind") == _POWER_RUNNER]
        try:
            rebuilt = self._build_power_evidence(
                power_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            )
            persisted_by_sequence = {
                int(spec["sequence"]): payload for spec, payload in persisted_rows
            }
            rebuilt_by_sequence = {
                int(spec["sequence"]): evidence.payload for spec, evidence in rebuilt
            }
            if set(persisted_by_sequence) != set(rebuilt_by_sequence) or any(
                canonical_manifest_bytes(persisted_by_sequence[sequence])
                != canonical_manifest_bytes(rebuilt_by_sequence[sequence])
                for sequence in persisted_by_sequence
            ):
                return HistoricalEvidenceStatus.CORRUPT, None
            primary = [
                payload
                for spec, payload in persisted_rows
                if spec.get("arm_id") == policy.prospective_primary_effect_id
            ]
            if len(primary) != 1:
                return HistoricalEvidenceStatus.CORRUPT, None
        except (KeyError, RuntimeError, TypeError, ValueError):
            return HistoricalEvidenceStatus.CORRUPT, None
        return (
            HistoricalEvidenceStatus.VERIFIED,
            manifest_sha256({"payload": primary[0]}),
        )

    def _historical_robustness_source_observations(
        self,
        audit: _HistoricalRegistryAudit,
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> tuple[
        tuple[HistoricalEvidenceStatus, str | None],
        tuple[HistoricalEvidenceStatus, str | None],
    ]:
        """Rebuild fixed-baseline robustness and preserve diagnostic provenance."""

        persisted = self._single_historical_payload(
            audit,
            runner_kind="robustness_concentration_inference",
        )
        if persisted is None:
            status = (
                audit.status
                if audit.status is not HistoricalEvidenceStatus.VERIFIED
                else HistoricalEvidenceStatus.MISSING
            )
            return (status, None), (status, None)
        robustness_specs = [
            spec
            for spec in all_specs
            if spec.get("runner_kind") == "robustness_concentration_inference"
        ]
        try:
            rebuilt = self._build_robustness_evidence(
                robustness_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            )
            payloads = [evidence.payload for _spec, evidence in rebuilt]
            if len(payloads) != 1 or canonical_manifest_bytes(payloads[0]) != (
                canonical_manifest_bytes(persisted)
            ):
                corrupt = (HistoricalEvidenceStatus.CORRUPT, None)
                return corrupt, corrupt
        except (KeyError, RuntimeError, TypeError, ValueError):
            corrupt = (HistoricalEvidenceStatus.CORRUPT, None)
            return corrupt, corrupt

        digest = manifest_sha256({"payload": persisted})
        robustness = (HistoricalEvidenceStatus.VERIFIED, digest)
        sharpe = persisted.get("sharpe_diagnostics")
        if isinstance(sharpe, Mapping) and sharpe.get("available") is True:
            return robustness, (HistoricalEvidenceStatus.VERIFIED, digest)
        return robustness, (HistoricalEvidenceStatus.MISSING, None)

    def _historical_redesign_source_status(
        self,
        audit: _HistoricalRegistryAudit,
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> HistoricalEvidenceStatus:
        """Rebuild registered redesign evidence before trusting its gate results."""

        persisted = self._single_historical_payload(audit, runner_kind=_REDESIGN_RUNNER)
        if persisted is None:
            return (
                audit.status
                if audit.status is not HistoricalEvidenceStatus.VERIFIED
                else HistoricalEvidenceStatus.MISSING
            )
        redesign_specs = [spec for spec in all_specs if spec.get("runner_kind") == _REDESIGN_RUNNER]
        try:
            rebuilt = self._build_redesign_evidence(
                redesign_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            )
            payloads = [evidence.payload for _spec, evidence in rebuilt]
            if len(payloads) != 1 or canonical_manifest_bytes(payloads[0]) != (
                canonical_manifest_bytes(persisted)
            ):
                return HistoricalEvidenceStatus.CORRUPT
        except (KeyError, RuntimeError, TypeError, ValueError):
            return HistoricalEvidenceStatus.CORRUPT
        return HistoricalEvidenceStatus.VERIFIED

    @staticmethod
    def _single_historical_payload(
        audit: _HistoricalRegistryAudit,
        *,
        runner_kind: str,
    ) -> dict[str, Any] | None:
        rows = audit.derived_payloads.get(runner_kind, ())
        return rows[0][1] if len(rows) == 1 else None

    def _primary_power_payload(
        self,
        audit: _HistoricalRegistryAudit,
        *,
        policy: HistoricalDispositionPolicy,
    ) -> dict[str, Any] | None:
        matches = [
            payload
            for spec, payload in audit.derived_payloads.get(_POWER_RUNNER, ())
            if spec.get("arm_id") == policy.prospective_primary_effect_id
        ]
        return matches[0] if len(matches) == 1 else None

    def _historical_evidence_checks(
        self,
        policy: HistoricalDispositionPolicy,
        *,
        observations: Mapping[str, tuple[HistoricalEvidenceStatus, str | None]],
        baseline: FixedBaselineDispositionSummary | None,
    ) -> tuple[HistoricalEvidenceCheck, ...]:
        checks: list[HistoricalEvidenceCheck] = []
        for requirement in policy.required_evidence:
            source_rows = [
                HistoricalEvidenceSource(
                    source_id=source_id,
                    sha256=observations[source_id][1],
                )
                for source_id in requirement.required_source_ids
            ]
            statuses = [observations[source_id][0] for source_id in requirement.required_source_ids]
            if HistoricalEvidenceStatus.CORRUPT in statuses:
                status = HistoricalEvidenceStatus.CORRUPT
            elif HistoricalEvidenceStatus.MISSING in statuses:
                status = HistoricalEvidenceStatus.MISSING
            else:
                status = HistoricalEvidenceStatus.VERIFIED
            if (
                status is HistoricalEvidenceStatus.VERIFIED
                and requirement.check_id == policy.baseline_ledger_check_id
                and baseline is not None
                and baseline.zero_trades_or_exposure
            ):
                status = HistoricalEvidenceStatus.VERIFIED_EMPTY
            checks.append(
                HistoricalEvidenceCheck(
                    check_id=requirement.check_id,
                    kind=requirement.kind,
                    status=status,
                    sources=tuple(source_rows),
                )
            )
        return tuple(checks)

    def _historical_baseline_summary(
        self,
        audit: _HistoricalRegistryAudit,
        *,
        policy: HistoricalDispositionPolicy,
    ) -> FixedBaselineDispositionSummary | None:
        payload = self._single_historical_payload(
            audit,
            runner_kind="robustness_concentration_inference",
        )
        if payload is None or payload.get("disposition_source_arm_id") != policy.baseline_arm_id:
            return None
        raw_checks = payload.get("quantitative_gate_checks")
        zero = payload.get("raw_baseline_zero_trades_or_exposure")
        if not isinstance(raw_checks, Mapping) or not isinstance(zero, bool):
            return None
        if set(raw_checks) != set(policy.baseline_quantitative_gate_ids) or any(
            not isinstance(value, bool) for value in raw_checks.values()
        ):
            return None
        return FixedBaselineDispositionSummary(
            arm_id=policy.baseline_arm_id,
            gate_checks=tuple(
                HistoricalGateCheck(gate_id=str(gate_id), passed=bool(raw_checks[gate_id]))
                for gate_id in policy.baseline_quantitative_gate_ids
                if gate_id in raw_checks
            ),
            zero_trades_or_exposure=zero,
        )

    def _historical_redesign_summary(  # noqa: PLR0911 - fail-closed schema parser
        self,
        audit: _HistoricalRegistryAudit,
        *,
        policy: HistoricalDispositionPolicy,
    ) -> RegisteredRedesignDispositionSummary | None:
        payload = self._single_historical_payload(audit, runner_kind=_REDESIGN_RUNNER)
        raw_set = payload.get("redesign_candidates") if payload is not None else None
        if not isinstance(raw_set, Mapping):
            return None
        if raw_set.get("registered_candidate_order") != list(
            policy.registered_redesign_candidate_ids
        ):
            return None
        raw_candidates = raw_set.get("candidates")
        if isinstance(raw_candidates, (str, bytes)) or not isinstance(raw_candidates, Sequence):
            return None
        normalized_candidates: list[tuple[str, Mapping[str, Any], bool]] = []
        for raw in raw_candidates:
            if not isinstance(raw, Mapping):
                return None
            candidate_id = raw.get("candidate_id")
            checks = raw.get("gate_checks")
            zero = raw.get("raw_zero_trades_or_exposure")
            if (
                not isinstance(candidate_id, str)
                or not isinstance(checks, Mapping)
                or not isinstance(zero, bool)
            ):
                return None
            if any(not isinstance(value, bool) for value in checks.values()):
                return None
            if set(checks) != set(policy.redesign_quantitative_gate_ids):
                return None
            normalized_candidates.append((candidate_id, checks, zero))
        if tuple(candidate_id for candidate_id, _checks, _zero in normalized_candidates) != (
            policy.registered_redesign_candidate_ids
        ):
            return None
        try:
            candidates = tuple(
                RegisteredRedesignCandidateSummary(
                    candidate_id=candidate_id,
                    gate_checks=tuple(
                        HistoricalGateCheck(gate_id=gate_id, passed=checks[gate_id])
                        for gate_id in policy.redesign_quantitative_gate_ids
                    ),
                    zero_trades_or_exposure=zero,
                )
                for candidate_id, checks, zero in normalized_candidates
            )
            return RegisteredRedesignDispositionSummary(candidates=candidates)
        except (TypeError, ValueError):
            return None

    def _historical_power_summary(
        self,
        audit: _HistoricalRegistryAudit,
        *,
        policy: HistoricalDispositionPolicy,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> ProspectivePowerDispositionSummary | None:
        record = self._historical_primary_power_record(audit, policy=policy)
        if record is None:
            return None
        spec, payload = record
        if not self._historical_power_identity_matches(spec, payload, policy=policy):
            return None
        if not self._historical_power_sources_match(
            payload,
            audit=audit,
            policy=policy,
            all_specs=all_specs,
            trial_ids=trial_ids,
            manifest=manifest,
        ):
            return None
        state = self._historical_power_payload_state(payload)
        if state is None:
            return None
        zero, operationally_impractical, minimum_duration = state
        powered = bool(
            minimum_duration is not None
            and minimum_duration <= policy.maximum_prospective_duration_years
            and not operationally_impractical
        )
        if zero:
            powered = False
            operationally_impractical = True
        if operationally_impractical == powered:
            return None
        return ProspectivePowerDispositionSummary(
            primary_effect_id=policy.prospective_primary_effect_id,
            maximum_duration_years=policy.maximum_prospective_duration_years,
            powered_within_maximum_duration=powered,
            operationally_impractical=operationally_impractical,
            not_applicable_due_to_zero_trades=zero,
        )

    @staticmethod
    def _historical_primary_power_record(
        audit: _HistoricalRegistryAudit,
        *,
        policy: HistoricalDispositionPolicy,
    ) -> tuple[Mapping[str, Any], dict[str, Any]] | None:
        matches = [
            (spec, payload)
            for spec, payload in audit.derived_payloads.get(_POWER_RUNNER, ())
            if spec.get("arm_id") == policy.prospective_primary_effect_id
        ]
        return matches[0] if len(matches) == 1 else None

    def _historical_power_identity_matches(
        self,
        spec: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        policy: HistoricalDispositionPolicy,
    ) -> bool:
        try:
            parameters = self._mapping(spec, "parameters")
            effect = float(parameters["minimum_standardized_effect"])
            registered_primary = float(parameters["minimum_standardized_effect_primary"])
            registered_maximum = float(parameters["maximum_duration_years"])
            payload_effect = float(payload["minimum_standardized_effect"])
        except (KeyError, TypeError, ValueError):
            return False
        identity_matches = not (
            not all(
                math.isfinite(value)
                for value in (effect, registered_primary, registered_maximum, payload_effect)
            )
            or effect != registered_primary
            or effect != payload_effect
            or self._power_arm_id(effect) != policy.prospective_primary_effect_id
            or registered_maximum != policy.maximum_prospective_duration_years
            or parameters.get("cadence_source_arm_id") != policy.baseline_arm_id
            or parameters.get("cadence_source_fold_id") != "full-panel"
            or payload.get("evidence_complete") is not True
            or payload.get("historical_selection_contaminated") is not True
            or payload.get("evidence_role") != spec.get("evidence_role")
        )
        cadence = payload.get("cadence_source")
        cadence_matches = isinstance(cadence, Mapping) and not any(
            cadence.get(field) != value
            for field, value in (
                ("arm_id", policy.baseline_arm_id),
                ("cadence_gate_population", "matured_closed_trade_outcomes"),
                ("cost_scenario", "expected"),
                ("fold_id", "full-panel"),
                ("net_return_formula", "pnl/abs(quantity*entry_fill_price)"),
                (
                    "economic_period_date_transform",
                    "normalized_close_timestamp_minus_one_calendar_day",
                ),
                (
                    "terminal_position_treatment",
                    "filled_entry_reported_but_right_censored_outcome_excluded_from_"
                    "matured_cadence_and_power",
                ),
            )
        )
        return identity_matches and cadence_matches

    def _historical_power_sources_match(
        self,
        payload: Mapping[str, Any],
        *,
        audit: _HistoricalRegistryAudit,
        policy: HistoricalDispositionPolicy,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> bool:
        cadence = payload.get("cadence_source")
        if not isinstance(cadence, Mapping):
            return False
        report_hash_by_trial = {
            row["trial_id"]: row["report_sha256"] for row in audit.report_sources
        }
        product_by_dataset = {
            str(dataset["dataset_id"]): str(dataset["requested_product"])
            for dataset in self._list_of_mappings(self._mapping(manifest, "data"), "datasets")
        }
        source_specs = sorted(
            (
                source_spec
                for source_spec in all_specs
                if source_spec.get("runner_kind") == "production_core_direct"
                and source_spec.get("arm_id") == policy.baseline_arm_id
                and source_spec.get("cost_scenario") == "expected"
                and source_spec.get("fold_id") == "full-panel"
            ),
            key=lambda item: product_by_dataset.get(str(item.get("dataset_id")), ""),
        )
        expected_source_trials: list[dict[str, Any]] = []
        for source_spec in source_specs:
            dataset_id = str(source_spec.get("dataset_id"))
            product = product_by_dataset.get(dataset_id)
            source_trial_id = trial_ids.get(int(source_spec["sequence"]))
            report_hash = report_hash_by_trial.get(str(source_trial_id))
            if product is None or source_trial_id is None or report_hash is None:
                return False
            expected_source_trials.append(
                {
                    "dataset_id": dataset_id,
                    "fold_id": "full-panel",
                    "product": product,
                    "report_sha256": report_hash,
                    "trial_id": source_trial_id,
                }
            )
        return not (
            len(expected_source_trials) != len(product_by_dataset)
            or cadence.get("source_trials") != expected_source_trials
        )

    @staticmethod
    def _historical_power_payload_state(
        payload: Mapping[str, Any],
    ) -> tuple[bool, bool, float | None] | None:
        status = payload.get("status")
        zero = status == "not_applicable_due_to_zero_trades"
        operationally_impractical = payload.get("operationally_impractical")
        design = payload.get("power_design")
        outcomes = payload.get("completed_trade_outcomes")
        basic_valid = (
            status in {"complete", "not_applicable_due_to_zero_trades"}
            and isinstance(operationally_impractical, bool)
            and payload.get("primary_duration_operationally_impractical")
            is operationally_impractical
            and not isinstance(outcomes, (str, bytes))
            and isinstance(outcomes, Sequence)
        )
        zero_valid = bool(
            zero
            and not outcomes
            and payload.get("observed_inputs") is None
            and design is None
            and payload.get("not_applicable_reason")
            == "zero_closed_trades_in_baseline_expected_cost_full_panel"
            and payload.get("historical_hierarchy_implication") == "RETIRE"
        )
        complete_valid = bool(
            not zero
            and outcomes
            and isinstance(payload.get("observed_inputs"), Mapping)
            and isinstance(design, Mapping)
            and payload.get("not_applicable_reason") is None
            and payload.get("historical_hierarchy_implication") is None
        )
        minimum_duration = (
            design.get("minimum_duration_years") if isinstance(design, Mapping) else None
        )
        normalized_duration: float | None = None
        if isinstance(minimum_duration, (int, float)) and not isinstance(minimum_duration, bool):
            try:
                candidate_duration = float(minimum_duration)
            except OverflowError:
                candidate_duration = math.inf
            if math.isfinite(candidate_duration) and candidate_duration > 0.0:
                normalized_duration = candidate_duration
        duration_valid = bool(
            zero
            or (
                isinstance(operationally_impractical, bool)
                and operationally_impractical != (normalized_duration is not None)
            )
        )
        if not (
            basic_valid
            and (zero_valid or complete_valid)
            and duration_valid
            and isinstance(operationally_impractical, bool)
        ):
            return None
        return zero, operationally_impractical, normalized_duration

    def _historical_correctness_summary(
        self,
        manifest: Mapping[str, Any],
        *,
        parameters: Mapping[str, Any],
    ) -> CorrectnessDispositionSummary | None:
        try:
            attestation = self._mapping(manifest, "correctness_attestation")
            findings = self._list_of_mappings(attestation, "findings")
            finding_map = self._mapping(parameters, "semantic_finding_candidate_map")
        except (KeyError, TypeError, ValueError):
            return None
        output: list[SemanticCorrectnessFindingSummary] = []
        for finding in findings:
            if finding.get("finding_class") != CorrectnessFindingClass.STRATEGY_SEMANTIC.value:
                continue
            finding_id = finding.get("id")
            evidence_ids = finding.get("evidence_file_ids")
            if (
                not isinstance(finding_id, str)
                or isinstance(evidence_ids, (str, bytes))
                or not isinstance(evidence_ids, Sequence)
                or any(not isinstance(item, str) for item in evidence_ids)
            ):
                return None
            candidate_id = finding_map.get(finding_id)
            if candidate_id is not None and not isinstance(candidate_id, str):
                return None
            try:
                output.append(
                    SemanticCorrectnessFindingSummary(
                        finding_id=finding_id,
                        severity=CorrectnessFindingSeverity(str(finding["severity"])),
                        status=CorrectnessFindingStatus(str(finding["status"])),
                        semantic_candidate_id=candidate_id,
                        evidence_source_ids=tuple(str(item) for item in evidence_ids),
                    )
                )
            except (KeyError, TypeError, ValueError):
                return None
        return CorrectnessDispositionSummary(findings=tuple(output))

    @staticmethod
    def _report_derived_metrics(report: Any) -> DerivedEvidenceMetrics:
        metrics = report.metrics
        return DerivedEvidenceMetrics(
            initial_capital=metrics.initial_capital,
            total_return_pct=metrics.total_return_pct,
            cagr_pct=metrics.cagr_pct,
            sharpe_ratio=metrics.sharpe_ratio,
            sortino_ratio=metrics.sortino_ratio,
            max_drawdown_pct=metrics.max_drawdown_pct,
            win_rate_pct=metrics.win_rate_pct,
            profit_factor=metrics.profit_factor,
            total_trades=metrics.total_trades,
            winning_trades=metrics.winning_trades,
            losing_trades=metrics.losing_trades,
            total_signals=report.signals_emitted,
            final_equity=metrics.final_equity,
            peak_equity=metrics.peak_equity,
        )

    def _replay_scoring_entries(
        self,
        entries: Sequence[tuple[str, RawSignalEvidence]],
        *,
        binding_rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        evidence_rows = [evidence for _fold_id, evidence in entries]
        binding_replays: list[tuple[Mapping[str, Any], dict[str, dict[str, object]]]] = []
        for row in binding_rows:
            binding = ScoringReplayBindingState(
                binding_id=str(row["binding_id"]),
                active=bool(row["active"]),
                autopilot=bool(row["autopilot"]),
                execution_mode=str(row["execution_mode"]),
            )
            replay = replay_scoring_raw_signal_ledger(
                evidence_rows,
                require_stop_loss=bool(row["require_stop_loss"]),
                require_explicit_scoring_inputs=bool(row["require_explicit_scoring_inputs"]),
                binding=binding,
            )
            binding_replays.append((row, replay))

        output: list[dict[str, Any]] = []
        for source_ordinal, (fold_id, evidence) in enumerate(entries):
            common: dict[str, Any] | None = None
            per_binding: list[dict[str, Any]] = []
            for binding_row, replay in binding_replays:
                row = dict(replay[evidence.external_signal_id])
                binding_payload = row.pop("binding")
                entry_requirements = row.pop("entry_requirements")
                stop_status = row.pop("stop_status")
                if common is None:
                    common = row
                elif canonical_manifest_bytes(common) != canonical_manifest_bytes(row):
                    message = (
                        "scoring replay produced binding-dependent signal fields for "
                        f"{evidence.external_signal_id}"
                    )
                    raise RuntimeError(message)
                per_binding.append(
                    {
                        "binding": binding_payload,
                        "entry_requirements": entry_requirements,
                        "stop_status": stop_status,
                        "strategy_config_active": bool(binding_row["strategy_config_active"]),
                        "require_stop_loss": bool(binding_row["require_stop_loss"]),
                        "require_explicit_scoring_inputs": bool(
                            binding_row["require_explicit_scoring_inputs"]
                        ),
                        "thresholds_evaluated": False,
                        "execution_evaluated": False,
                    }
                )
            if common is None:
                message = "scoring replay produced no binding evidence"
                raise RuntimeError(message)
            output.append(
                {
                    **common,
                    "source_fold_id": fold_id,
                    "source_ordinal": source_ordinal,
                    "binding_replays": per_binding,
                }
            )
        return output

    def _scoring_evidence_payload(
        self,
        spec: Mapping[str, Any],
        *,
        manifest: Mapping[str, Any],
        binding_snapshot: Mapping[str, Any],
        replay_rows: Sequence[Mapping[str, Any]],
        source_fold_ids: Sequence[str],
        source_trial_ids: Sequence[str],
    ) -> dict[str, Any]:
        external_ids = [str(row["external_signal_id"]) for row in replay_rows]
        if len(external_ids) != len(set(external_ids)):
            message = "scoring replay output contains duplicate canonical external signal IDs"
            raise RuntimeError(message)
        return {
            "binding_snapshot": dict(binding_snapshot),
            "coverage": {
                "source_signals": len(replay_rows),
                "replayed_signals": len(replay_rows),
                "missing_signals": 0,
                "duplicate_signals": 0,
            },
            "evidence_role": spec["evidence_role"],
            "execution_evaluated": False,
            "historical_selection_contaminated": True,
            "join_key": "canonical_external_signal_id",
            "protocol_id": manifest["protocol_id"],
            "random_signal_id_used_as_join_key": False,
            "runner_kind": spec["runner_kind"],
            "same_raw_signal_ledger_no_second_strategy_run": True,
            "scoring_binding_ledger": [dict(row) for row in replay_rows],
            "source_fold_ids": list(source_fold_ids),
            "source_trial_ids": list(source_trial_ids),
            "thresholds_evaluated": False,
        }

    def _failed_trial_count(self, trial_ids: Iterable[str]) -> int:
        identities = tuple(trial_ids)
        if not identities:
            return 0
        with Session(self._engine) as session:
            return len(
                tuple(
                    session.execute(
                        select(BacktestTrial.trial_id).where(
                            BacktestTrial.trial_id.in_(identities),
                            BacktestTrial.status == BacktestTrialStatus.FAILED.value,
                        )
                    ).scalars()
                )
            )
