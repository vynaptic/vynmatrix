"""Controlled, resumable historical validation of production strategy cores."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

from sqlalchemy.engine import Engine

from dev_cli.validation.campaign_benchmark_evidence import CampaignBenchmarkEvidenceMixin
from dev_cli.validation.campaign_contracts import (
    _BENCHMARK_PORTFOLIO_GROUP_RUNNERS,
    _CONDITIONAL_SIZING_GROUP_RUNNERS,
    _CSCV_GROUP_RUNNERS,
    _HISTORICAL_DISPOSITION_GROUP_RUNNERS,
    _HISTORICAL_DISPOSITION_RUNNER,
    _PHASE_ONE_INDIVIDUAL_RUNNERS,
    _POWER_GROUP_RUNNERS,
    _PRIMARY_GROUP_RUNNERS,
    _REDESIGN_GROUP_RUNNERS,
    _ROBUSTNESS_GROUP_RUNNERS,
    _SCORING_LEDGER_GROUP_RUNNERS,
    _SHA256_RE,
    _TERMINAL_STATUSES,
    CampaignRunSummary,
    DataParityAttestationVerifier,
    ExecutionCostMeasurementVerifier,
    ProductMetadataProvider,
)
from dev_cli.validation.campaign_derived_evidence import CampaignDerivedEvidenceMixin
from dev_cli.validation.campaign_disposition_evidence import CampaignDispositionEvidenceMixin
from dev_cli.validation.campaign_execution import CampaignExecutionMixin
from dev_cli.validation.campaign_protocol_validation import CampaignProtocolValidationMixin
from dev_cli.validation.campaign_registry import CampaignRegistryMixin
from dev_cli.validation.campaign_robustness_evidence import CampaignRobustnessEvidenceMixin
from dev_cli.validation.campaign_runtime import CampaignRuntimeMixin
from dev_cli.validation.campaign_sizing import CampaignSizingMixin
from dev_cli.validation.persistence.backtest_manifest_store import (
    BacktestManifestStore,
    ManifestReference,
)
from dev_cli.validation.persistence.backtest_result_store import (
    BacktestResultStore,
)
from dev_cli.validation.persistence.backtest_trial_store import (
    BacktestTrialStatus,
    BacktestTrialStore,
)
from dev_cli.validation.persistence.historical_price_repository import (
    HistoricalPriceRepository,
)
from lib_application.db.session import get_session_factory
from lib_application.services.price_ingestion_service import PriceIngestionService


class StrategyValidationCampaign(
    CampaignRegistryMixin,
    CampaignExecutionMixin,
    CampaignBenchmarkEvidenceMixin,
    CampaignDerivedEvidenceMixin,
    CampaignRobustnessEvidenceMixin,
    CampaignSizingMixin,
    CampaignDispositionEvidenceMixin,
    CampaignProtocolValidationMixin,
    CampaignRuntimeMixin,
):
    """Freeze and execute a pre-registered production-core campaign.

    The service contains orchestration only. Strategy decisions come from the
    same file-backed ``PureSignalStrategy`` core loaded by the indicator worker,
    and fill semantics come from the versioned validation backtest engine.
    """

    MANIFEST_SCHEMA_VERSION: ClassVar[str] = "1.4"

    def __init__(
        self,
        engine: Engine,
        *,
        repo_root: Path,
        artifact_root: Path,
        product_metadata_provider: ProductMetadataProvider | None = None,
        execution_cost_measurement_verifier: ExecutionCostMeasurementVerifier,
        data_parity_attestation_verifier: DataParityAttestationVerifier,
    ) -> None:
        self._engine = engine
        self._repo_root = repo_root.resolve()
        self._artifact_root = artifact_root.resolve()
        self._manifest_store = BacktestManifestStore(self._artifact_root)
        self._session_factory = get_session_factory(engine=engine)
        self._price_service = PriceIngestionService(self._session_factory)
        self._historical_prices = HistoricalPriceRepository(self._session_factory)
        self._trial_store = BacktestTrialStore(engine)
        self._result_store = BacktestResultStore(engine)
        self._product_metadata_provider = product_metadata_provider
        self._execution_cost_measurement_verifier = execution_cost_measurement_verifier
        self._data_parity_attestation_verifier = data_parity_attestation_verifier

    def _store_frozen_manifest(
        self,
        protocol: Mapping[str, Any],
        manifest: Mapping[str, Any],
    ) -> ManifestReference:
        """Publish the protocol before the manifest that references it."""

        self._store_content_addressed_protocol(protocol)
        return self._manifest_store.store(manifest)

    def audit(
        self,
        *,
        manifest_hash: str,
        expected_strategy_name: str | None = None,
    ) -> CampaignRunSummary:
        """Re-audit immutable historical evidence after strategy retirement.

        This path is deliberately read-only and cannot execute or register a
        trial.  It resolves the protocol through its independently
        content-addressed artifact, then verifies the frozen manifest,
        self-contained source attestations, database datasets, exact trial
        identities, and every completed result without loading the retired
        production core, config, or checked-in protocol.
        """

        if not _SHA256_RE.fullmatch(manifest_hash):
            message = "manifest_hash must be a lowercase SHA-256 digest"
            raise ValueError(message)
        manifest = self._manifest_store.read(manifest_hash)
        protocol = self._load_content_addressed_protocol(manifest)
        self._validate_frozen_manifest_content(
            manifest,
            protocol=protocol,
            expected_hash=manifest_hash,
            current_data_parity_source_required=False,
        )
        if expected_strategy_name is not None:
            strategy_directory = Path(
                str(self._mapping(manifest, "strategy")["core_path"])
            ).parent.name
            if strategy_directory != expected_strategy_name:
                message = (
                    "requested strategy name differs from frozen manifest: "
                    f"expected {strategy_directory}, found {expected_strategy_name}"
                )
                raise ValueError(message)

        self._revalidate_datasets(manifest)
        trial_specs = self._validated_frozen_trial_specs_for_audit(manifest)
        experiment_id = self._existing_experiment_for_audit(
            manifest,
            manifest_hash=manifest_hash,
        )
        trial_ids, counts = self._registered_trial_ids_for_audit(
            experiment_id=experiment_id,
            manifest_hash=manifest_hash,
            manifest=manifest,
            trial_specs=trial_specs,
        )
        completed_specs = [
            spec
            for spec in trial_specs
            if self._trial_status(trial_ids[int(spec["sequence"])])
            == BacktestTrialStatus.COMPLETED.value
        ]
        if completed_specs:
            self._audit_completed_evidence(
                completed_specs,
                all_specs=trial_specs,
                trial_ids=trial_ids,
                manifest=manifest,
                permit_historical_runner_fallback=True,
            )
        historical_disposition = self._historical_disposition_run_summary(
            trial_specs,
            trial_ids=trial_ids,
        )
        if not self._is_completed_retirement(historical_disposition):
            # Only an immutable, completed retirement can outlive the mutable
            # reconciliation implementation that originally produced its
            # attestation.  Every incomplete or non-retired audit retains the
            # normal exact-current-source re-derivation contract.
            self._validate_manifest_data_parity_provenance(
                manifest,
                protocol=protocol,
            )
        return CampaignRunSummary(
            manifest_hash=manifest_hash,
            manifest_path=self._manifest_store.path_for(manifest_hash),
            experiment_id=experiment_id,
            total_trials=len(trial_specs),
            selected_trials=0,
            status_counts=counts,
            failures_this_run=0,
            historical_disposition=historical_disposition,
        )

    @staticmethod
    def _is_completed_retirement(disposition: Mapping[str, Any]) -> bool:
        """Return whether a verified stored verdict is an immutable retirement."""

        authority = disposition.get("authority")
        return (
            disposition.get("status") == BacktestTrialStatus.COMPLETED.value
            and disposition.get("completion_state") == "COMPLETE"
            and disposition.get("economic_disposition") == "RETIRE"
            and disposition.get("frozen_arm_id") is None
            and isinstance(authority, Mapping)
            and authority.get("prospective_shadow_research_eligible") is False
            and authority.get("paper_trading_authorized") is False
            and authority.get("live_trading_authorized") is False
            and authority.get("automatic_parameter_deployment_authorized") is False
        )

    def run(
        self,
        *,
        strategy_path: Path,
        manifest_hash: str | None = None,
        arm_ids: Sequence[str] = (),
        fold_ids: Sequence[str] = (),
        freeze_only: bool = False,
        execution_environment: Mapping[str, Any] | None = None,
        upstream_selection_ledger: Path | None = None,
        execution_cost_measurement: Path | None = None,
        data_parity_attestation: Path | None = None,
        correctness_attestation: Path | None = None,
    ) -> CampaignRunSummary:
        """Freeze a new manifest or resume an exact one, then checkpoint trials."""

        resolved_strategy_path = strategy_path.resolve()
        self._require_within_repo(resolved_strategy_path)
        protocol = self._load_json_object(resolved_strategy_path / "validation_protocol.json")
        runtime_config = self._load_json_object(resolved_strategy_path / "config.json")
        self._prevalidate_filters(protocol, arm_ids=arm_ids, fold_ids=fold_ids)
        self._load_and_validate_core(
            resolved_strategy_path,
            protocol=protocol,
            runtime_config=runtime_config,
        )

        if manifest_hash is None:
            manifest = self._build_manifest(
                resolved_strategy_path,
                protocol=protocol,
                runtime_config=runtime_config,
                execution_environment=execution_environment,
                upstream_selection_ledger=upstream_selection_ledger,
                execution_cost_measurement=execution_cost_measurement,
                data_parity_attestation=data_parity_attestation,
                correctness_attestation=correctness_attestation,
            )
            reference = self._store_frozen_manifest(protocol, manifest)
        else:
            if execution_environment is not None:
                message = "execution_environment cannot amend an existing frozen manifest"
                raise ValueError(message)
            if upstream_selection_ledger is not None:
                message = (
                    "upstream_selection_ledger cannot amend an existing frozen manifest; "
                    "resume uses the embedded attested ledger"
                )
                raise ValueError(message)
            if execution_cost_measurement is not None:
                message = (
                    "execution_cost_measurement cannot amend an existing frozen "
                    "manifest; resume uses the embedded attested artifact"
                )
                raise ValueError(message)
            if data_parity_attestation is not None:
                message = (
                    "data_parity_attestation cannot amend an existing frozen "
                    "manifest; resume uses the embedded attested artifact"
                )
                raise ValueError(message)
            if correctness_attestation is not None:
                message = (
                    "correctness_attestation cannot amend an existing frozen "
                    "manifest; resume uses the embedded attested artifact"
                )
                raise ValueError(message)
            if not _SHA256_RE.fullmatch(manifest_hash):
                message = "manifest_hash must be a lowercase SHA-256 digest"
                raise ValueError(message)
            manifest = self._manifest_store.read(manifest_hash)
            reference = self._manifest_store.store(manifest)

        self._validate_frozen_manifest(
            manifest,
            protocol=protocol,
            strategy_path=resolved_strategy_path,
            runtime_config=runtime_config,
            expected_hash=reference.sha256,
        )
        runtime_verified = False
        environment = self._mapping(manifest, "environment")
        if environment.get("execution_authorized") is True:
            # An executable manifest must be self-consistent even on the initial
            # freeze or an outcome-free resume.  Do not defer this check until
            # the first trial is about to run.
            self._require_frozen_runtime_environment(manifest)
            runtime_verified = True
        if manifest_hash is not None:
            self._revalidate_product_metadata(manifest, protocol)
        self._revalidate_datasets(manifest)
        trial_specs = self._validated_trial_specs(manifest, protocol)
        selected = self._select_trials(trial_specs, arm_ids=arm_ids, fold_ids=fold_ids)
        experiment_id = self._get_or_create_experiment(manifest, reference.sha256)
        trial_ids = self._register_all_trials(
            experiment_id=experiment_id,
            manifest_hash=reference.sha256,
            manifest=manifest,
            trial_specs=trial_specs,
        )

        failures = 0
        completed_specs = [
            spec
            for spec in trial_specs
            if self._trial_status(trial_ids[int(spec["sequence"])])
            == BacktestTrialStatus.COMPLETED.value
        ]
        if completed_specs:
            # A read-only resume still executes current application policy code.
            # Bind that re-derivation to the frozen interpreter/distribution and
            # installed wheel payloads, but do not require an unpruned local image.
            if not runtime_verified:
                self._require_frozen_runtime_environment(manifest)
            self._audit_completed_evidence(
                completed_specs,
                all_specs=trial_specs,
                trial_ids=trial_ids,
                manifest=manifest,
            )
        if not freeze_only and selected:
            self._require_phase_one_runner_capabilities(selected)
            self._require_complete_group_selection(
                selected,
                all_specs=trial_specs,
            )
            if self._has_executable_work(selected, trial_ids):
                self._require_execution_environment(manifest)
                self._require_engine_capabilities()
                core_class = self._load_installed_core(manifest)
                failures = self._execute_phase_one(
                    selected,
                    all_specs=trial_specs,
                    trial_ids=trial_ids,
                    experiment_id=experiment_id,
                    manifest=manifest,
                    runtime_config=runtime_config,
                    core_class=core_class,
                )

        counts = self._refresh_experiment_summary(experiment_id)
        historical_disposition = self._historical_disposition_run_summary(
            trial_specs,
            trial_ids=trial_ids,
        )
        return CampaignRunSummary(
            manifest_hash=reference.sha256,
            manifest_path=reference.path,
            experiment_id=experiment_id,
            total_trials=len(trial_specs),
            selected_trials=len(selected),
            status_counts=counts,
            failures_this_run=failures,
            historical_disposition=historical_disposition,
        )

    def _historical_disposition_run_summary(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        trial_ids: Mapping[int, str],
    ) -> dict[str, Any]:
        """Expose the persisted final verdict without recomputing the hierarchy."""

        final_specs = [
            spec for spec in specs if spec.get("runner_kind") == _HISTORICAL_DISPOSITION_RUNNER
        ]
        if len(final_specs) != 1:
            message = "campaign registry must contain exactly one historical disposition"
            raise RuntimeError(message)
        spec = final_specs[0]
        trial_id = trial_ids[int(spec["sequence"])]
        status = self._trial_status(trial_id)
        authority = {
            "paper_trading_authorized": False,
            "live_trading_authorized": False,
            "automatic_parameter_deployment_authorized": False,
        }
        if status != BacktestTrialStatus.COMPLETED.value:
            return {
                "status": status,
                "trial_id": trial_id,
                "completion_state": None,
                "economic_disposition": None,
                "frozen_arm_id": None,
                "blocker_codes": [],
                "reason_codes": [],
                "authority": authority,
            }
        payload = self._result_store.load_derived_payload(
            trial_id,
            expected_evidence_kind=_HISTORICAL_DISPOSITION_RUNNER,
        )
        payload_authority = self._mapping(payload, "authority")
        if any(payload_authority.get(key) is not False for key in authority):
            message = "persisted historical disposition attempts to authorize trading"
            raise RuntimeError(message)
        return {
            "status": status,
            "trial_id": trial_id,
            "completion_state": payload.get("completion_state"),
            "economic_disposition": payload.get("economic_disposition"),
            "frozen_arm_id": payload.get("frozen_arm_id"),
            "blocker_codes": list(payload.get("blocker_codes", ())),
            "reason_codes": list(payload.get("reason_codes", ())),
            "authority": dict(payload_authority),
        }

    def _execute_phase_one(
        self,
        selected: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
        runtime_config: Mapping[str, Any],
        core_class: type[Any],
    ) -> int:
        """Execute authorized component trials and the primary selector once."""

        failures = 0
        report_cache: dict[int, Any] = {}
        report_audit_cache: dict[int, dict[str, Any]] = {}
        for spec in selected:
            if spec.get("runner_kind") not in _PHASE_ONE_INDIVIDUAL_RUNNERS:
                continue
            sequence = int(spec["sequence"])
            trial_id = trial_ids[sequence]
            if self._trial_status(trial_id) in _TERMINAL_STATUSES:
                continue
            try:
                self._trial_store.mark_running(trial_id)
                report, audit = self._execute_trial(
                    spec,
                    manifest=manifest,
                    runtime_config=runtime_config,
                    core_class=core_class,
                )
                self._save_report_result(
                    report,
                    audit=audit,
                    spec=spec,
                    trial_id=trial_id,
                    manifest=manifest,
                )
                report_cache[sequence] = report
                report_audit_cache[sequence] = dict(audit)
            except (KeyboardInterrupt, SystemExit):
                self._trial_store.mark_interrupted(
                    trial_id,
                    error_context="validation runner interrupted after trial start",
                )
                self._refresh_experiment_summary(experiment_id)
                raise
            except Exception as exc:
                self._trial_store.mark_failed(
                    trial_id,
                    error_class=type(exc).__name__,
                    error_context=self._safe_error_context(exc),
                )
                failures += 1

        if any(spec.get("runner_kind") in _PRIMARY_GROUP_RUNNERS for spec in selected):
            primary_specs = [
                spec for spec in all_specs if spec.get("runner_kind") in _PRIMARY_GROUP_RUNNERS
            ]
            failures += self._execute_primary_group(
                primary_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
                runtime_config=runtime_config,
                core_class=core_class,
                report_cache=report_cache,
            )
        if any(spec.get("runner_kind") in _SCORING_LEDGER_GROUP_RUNNERS for spec in selected):
            scoring_specs = [
                spec
                for spec in all_specs
                if spec.get("runner_kind") in _SCORING_LEDGER_GROUP_RUNNERS
            ]
            failures += self._execute_scoring_ledger_group(
                scoring_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            )
        if any(spec.get("runner_kind") in _POWER_GROUP_RUNNERS for spec in selected):
            power_specs = [
                spec for spec in all_specs if spec.get("runner_kind") in _POWER_GROUP_RUNNERS
            ]
            failures += self._execute_power_group(
                power_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            )
        benchmark_group_keys = sorted(
            {
                (str(spec["arm_id"]), str(spec["cost_scenario"]))
                for spec in selected
                if spec.get("runner_kind") in _BENCHMARK_PORTFOLIO_GROUP_RUNNERS
            }
        )
        for arm_id, cost_scenario in benchmark_group_keys:
            benchmark_specs = [
                spec
                for spec in all_specs
                if spec.get("runner_kind") in _BENCHMARK_PORTFOLIO_GROUP_RUNNERS
                and spec.get("arm_id") == arm_id
                and spec.get("cost_scenario") == cost_scenario
            ]
            failures += self._execute_benchmark_portfolio_group(
                benchmark_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
                report_cache=report_cache,
                report_audit_cache=report_audit_cache,
            )
        if any(spec.get("runner_kind") in _CSCV_GROUP_RUNNERS for spec in selected):
            cscv_specs = [
                spec for spec in all_specs if spec.get("runner_kind") in _CSCV_GROUP_RUNNERS
            ]
            failures += self._execute_cscv_group(
                cscv_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            )
        if any(spec.get("runner_kind") in _ROBUSTNESS_GROUP_RUNNERS for spec in selected):
            robustness_specs = [
                spec for spec in all_specs if spec.get("runner_kind") in _ROBUSTNESS_GROUP_RUNNERS
            ]
            failures += self._execute_robustness_group(
                robustness_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            )
        if any(spec.get("runner_kind") in _REDESIGN_GROUP_RUNNERS for spec in selected):
            redesign_specs = [
                spec for spec in all_specs if spec.get("runner_kind") in _REDESIGN_GROUP_RUNNERS
            ]
            failures += self._execute_redesign_group(
                redesign_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            )
        # Conditional sizing is intentionally last. Its raw-baseline gate is
        # persisted by the robustness stage and it must never pre-empt CSCV,
        # DSR/regime, robustness, redesign, or benchmark evidence.
        if any(spec.get("runner_kind") in _CONDITIONAL_SIZING_GROUP_RUNNERS for spec in selected):
            sizing_specs = [
                spec
                for spec in all_specs
                if spec.get("runner_kind") in _CONDITIONAL_SIZING_GROUP_RUNNERS
            ]
            failures += self._execute_conditional_sizing_group(
                sizing_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            )
        # The historical conclusion is always the final checkpoint. It audits
        # every earlier registry row and recursively verifies persisted evidence;
        # it never executes a strategy or authorizes paper/live trading.
        if any(
            spec.get("runner_kind") in _HISTORICAL_DISPOSITION_GROUP_RUNNERS for spec in selected
        ):
            disposition_specs = [
                spec
                for spec in all_specs
                if spec.get("runner_kind") in _HISTORICAL_DISPOSITION_GROUP_RUNNERS
            ]
            failures += self._execute_historical_disposition_group(
                disposition_specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            )
        return failures

    @staticmethod
    def _require_complete_group_selection(
        selected: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
    ) -> None:
        StrategyValidationCampaign._require_complete_benchmark_group_selection(
            selected,
            all_specs=all_specs,
        )
        groups = (
            (
                _PRIMARY_GROUP_RUNNERS,
                "TRAINING_SELECTED is one checkpointed group and requires all anchored "
                "folds and its pooled aggregate",
            ),
            (
                _SCORING_LEDGER_GROUP_RUNNERS,
                "SCORING_BINDING_LEDGER requires both components and its pooled aggregate",
            ),
            (
                _POWER_GROUP_RUNNERS,
                "prospective power is one checkpointed three-effect design",
            ),
            (
                _CSCV_GROUP_RUNNERS,
                "registered CSCV/PBO is one checkpointed retrospective diagnostic",
            ),
            (
                _ROBUSTNESS_GROUP_RUNNERS,
                "robustness, concentration, DSR, and regime evidence is one checkpoint",
            ),
            (
                _REDESIGN_GROUP_RUNNERS,
                "registered redesign evidence is one complete candidate-set checkpoint",
            ),
            (
                _CONDITIONAL_SIZING_GROUP_RUNNERS,
                "conditional sizing is one checkpointed three-policy OOS comparison",
            ),
            (
                _HISTORICAL_DISPOSITION_GROUP_RUNNERS,
                "historical disposition is one final outcome-blind checkpoint",
            ),
        )
        for runner_kinds, explanation in groups:
            selected_sequences = {
                int(spec["sequence"])
                for spec in selected
                if spec.get("runner_kind") in runner_kinds
            }
            if not selected_sequences:
                continue
            registered_sequences = {
                int(spec["sequence"])
                for spec in all_specs
                if spec.get("runner_kind") in runner_kinds
            }
            if selected_sequences != registered_sequences:
                message = f"{explanation}; partial group filters are prohibited"
                raise RuntimeError(message)
        disposition_selected = any(
            spec.get("runner_kind") == _HISTORICAL_DISPOSITION_RUNNER for spec in selected
        )
        if disposition_selected and {int(spec["sequence"]) for spec in selected} != {
            int(spec["sequence"]) for spec in all_specs
        }:
            message = (
                "historical disposition requires selection of the complete frozen "
                "trial registry; a partial campaign cannot permanently checkpoint a verdict"
            )
            raise RuntimeError(message)

    @staticmethod
    def _require_complete_benchmark_group_selection(
        selected: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
    ) -> None:
        """Require one complete benchmark/cost checkpoint family per selection."""

        selected_groups = {
            (str(spec["arm_id"]), str(spec["cost_scenario"]))
            for spec in selected
            if spec.get("runner_kind") in _BENCHMARK_PORTFOLIO_GROUP_RUNNERS
        }
        for arm_id, cost_scenario in selected_groups:
            selected_sequences = {
                int(spec["sequence"])
                for spec in selected
                if spec.get("runner_kind") in _BENCHMARK_PORTFOLIO_GROUP_RUNNERS
                and spec.get("arm_id") == arm_id
                and spec.get("cost_scenario") == cost_scenario
            }
            registered_sequences = {
                int(spec["sequence"])
                for spec in all_specs
                if spec.get("runner_kind") in _BENCHMARK_PORTFOLIO_GROUP_RUNNERS
                and spec.get("arm_id") == arm_id
                and spec.get("cost_scenario") == cost_scenario
            }
            if selected_sequences != registered_sequences:
                message = (
                    "benchmark portfolio evidence is one checkpointed arm/cost group and "
                    "requires full-panel, every OOS fold, and pooled OOS; partial group "
                    "filters are prohibited"
                )
                raise RuntimeError(message)

    def _save_report_result(
        self,
        report: Any,
        *,
        audit: Mapping[str, Any],
        spec: Mapping[str, Any],
        trial_id: str,
        manifest: Mapping[str, Any],
    ) -> None:
        self._result_store.save(
            report,
            trial_id=trial_id,
            asset_class=str(self._mapping(manifest, "habitat")["asset_class_db"]),
            meta={
                "arm_id": spec["arm_id"],
                "dataset_audit": dict(audit),
                "evidence_role": spec["evidence_role"],
                "historical_selection_contaminated": True,
                "protocol_id": manifest["protocol_id"],
                "report": report.to_dict(),
                "runner_kind": spec["runner_kind"],
                "validation_design": manifest["validation_design"],
            },
        )


__all__ = ["CampaignRunSummary", "StrategyValidationCampaign"]
