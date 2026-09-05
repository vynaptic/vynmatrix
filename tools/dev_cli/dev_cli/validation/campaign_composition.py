"""Runtime-neutral typing contract for composed validation-campaign mixins."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import date, datetime
    from decimal import Decimal
    from pathlib import Path
    from typing import Any, ClassVar

    from sqlalchemy.engine import Engine

    from dev_cli.validation.backtest.engine import BacktestConfig
    from dev_cli.validation.backtest.execution import ExecutionCostModel, FillPolicy
    from dev_cli.validation.backtest.walk_forward import TerminalExitCostPolicy
    from dev_cli.validation.campaign_contracts import (
        DataParityAttestationVerifier,
        ExecutionCostMeasurementVerifier,
        ProductMetadataProvider,
        _DerivedDiagnosticInputs,
    )
    from dev_cli.validation.disposition import HistoricalDispositionPolicy
    from dev_cli.validation.persistence.backtest_manifest_store import (
        BacktestManifestStore,
    )
    from dev_cli.validation.persistence.backtest_result_store import (
        BacktestResultStore,
        DerivedBacktestEvidence,
        DerivedEvidenceMetrics,
    )
    from dev_cli.validation.persistence.backtest_trial_store import BacktestTrialStore
    from dev_cli.validation.persistence.historical_price_repository import (
        HistoricalPriceRepository,
    )
    from dev_cli.validation.report_evidence import StandardReportEvidence
    from lib_application.services.price_ingestion_service import PriceIngestionService

    _DerivedRecords = list[tuple[Mapping[str, Any], DerivedBacktestEvidence]]
    _DirectReports = dict[
        tuple[str, str],
        tuple[Mapping[str, Any], str, dict[str, Any]],
    ]
    _ReportIndex = dict[tuple[str, str, str, str], Mapping[str, Any]]


class CampaignMixinContract:
    """Declare the composed host surface without runtime fallback methods."""

    if TYPE_CHECKING:
        MANIFEST_SCHEMA_VERSION: ClassVar[str]

        _engine: Engine
        _repo_root: Path
        _artifact_root: Path
        _manifest_store: BacktestManifestStore
        _price_service: PriceIngestionService
        _historical_prices: HistoricalPriceRepository
        _trial_store: BacktestTrialStore
        _result_store: BacktestResultStore
        _product_metadata_provider: ProductMetadataProvider | None
        _execution_cost_measurement_verifier: ExecutionCostMeasurementVerifier
        _data_parity_attestation_verifier: DataParityAttestationVerifier

        _file_sha256: Callable[..., str]
        _load_json_object: Callable[..., dict[str, Any]]

        _mapping: Callable[..., dict[str, Any]]
        _require_mapping: Callable[..., dict[str, Any]]
        _list_of_mappings: Callable[..., list[dict[str, Any]]]
        _nonempty_string: Callable[..., str]
        _common_dataset_value: Callable[..., str]
        _decimal: Callable[..., Decimal]
        _safe_error_context: Callable[..., str]
        _require_within_repo: Callable[..., None]
        _relative: Callable[..., str]

        _dataset_asset_class: Callable[..., str]
        _fetch_and_verify_product_metadata: Callable[..., dict[str, Any]]
        _environment_manifest: Callable[..., dict[str, Any]]

        _trial_status: Callable[..., str]
        _failed_trial_count: Callable[..., int]
        _refresh_experiment_summary: Callable[..., dict[str, int]]
        _product_cost: Callable[..., dict[str, Any]]

        _benchmark_contracts: Callable[..., list[tuple[str, str, dict[str, Any]]]]
        _semantic_contracts: Callable[..., list[dict[str, Any]]]
        _power_arm_id: Callable[..., str]
        _historical_disposition_policy: Callable[..., HistoricalDispositionPolicy]
        _historical_disposition_policy_from_mapping: Callable[..., HistoricalDispositionPolicy]
        _registered_redesign_trial_contract: Callable[..., dict[str, str]]
        _registered_redesign_change_contracts: Callable[..., list[dict[str, Any]]]
        _build_trial_registry: Callable[..., list[dict[str, Any]]]
        _operational_binding_snapshot: Callable[..., dict[str, Any]]
        _derived_portfolio_dataset_id: Callable[..., str]

        _validate_protocol: Callable[..., None]
        _validate_correctness_attestation: Callable[..., dict[str, Any]]
        _validate_data_parity_attestation: Callable[..., None]
        _validate_execution_cost_measurement: Callable[..., None]
        _validated_regime_assignment: Callable[..., dict[str, Any]]

        _load_trial_dataset: Callable[..., tuple[Mapping[str, Any], list[Any], dict[str, Any]]]
        _execution_cost_model: Callable[..., ExecutionCostModel]
        _fill_policy: Callable[..., FillPolicy]
        _backtest_config: Callable[..., BacktestConfig]
        _execute_benchmark_component: Callable[..., tuple[Any, dict[str, Any]]]
        _primary_terminal_policy: Callable[..., TerminalExitCostPolicy]
        _save_report_result: Callable[..., None]

        _execute_checkpointed_derived_group: Callable[..., int]
        _evidence_dates: Callable[..., tuple[date, date]]
        _expected_daily_timestamps: Callable[..., tuple[datetime, ...]]
        _load_direct_report_evidence: Callable[..., _DirectReports]
        _direct_report_spec_index: Callable[..., _ReportIndex]
        _standard_report_from_spec: Callable[..., StandardReportEvidence]
        _terminal_exit_adjustment: Callable[..., tuple[float, str | None]]
        _registered_component_products: Callable[..., tuple[str, ...]]
        _report_derived_metrics: Callable[..., DerivedEvidenceMetrics]

        _build_power_evidence: Callable[..., _DerivedRecords]
        _build_scoring_ledger_evidence: Callable[..., _DerivedRecords]
        _build_robustness_evidence: Callable[..., _DerivedRecords]
        _build_redesign_evidence: Callable[..., _DerivedRecords]
        _build_historical_disposition_evidence: Callable[..., _DerivedRecords]
        _build_derived_diagnostic_inputs: Callable[..., _DerivedDiagnosticInputs]
        _robustness_source_reports: Callable[..., tuple[StandardReportEvidence, ...]]
        _redesign_source_reports: Callable[..., tuple[StandardReportEvidence, ...]]

        _replay_scoring_entries: Callable[..., list[dict[str, Any]]]
        _scoring_evidence_payload: Callable[..., dict[str, Any]]
        _normalized_source_attestation: Callable[..., dict[str, Any]]
        _merge_source_attestations: Callable[..., dict[str, Any]]
        _audit_source_attestation: Callable[..., None]
        _expected_derived_evidence_kind: Callable[..., str]
        _conditional_sizing_gate: Callable[..., tuple[bool, str]]
