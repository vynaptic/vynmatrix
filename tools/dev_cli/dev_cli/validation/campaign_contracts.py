"""Shared immutable contracts for strategy-validation campaign services."""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from dev_cli.validation.benchmarks import (
    BenchmarkKind,
)
from dev_cli.validation.disposition import (
    HistoricalEvidenceStatus,
)
from dev_cli.validation.economic_regimes import (
    EconomicRegimeObservation,
)
from dev_cli.validation.persistence.backtest_trial_store import (
    BacktestTrialStatus,
)
from dev_cli.validation.robustness_models import (
    AdaptiveSelectorEvidence,
    RegisteredBenchmarkEvidence,
    RobustnessEvidenceConfig,
    TrialSharpeDispersion,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]*$")
_DISTRIBUTION_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_CANONICAL_DISTRIBUTION_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_REQUIRED_ENGINE_CAPABILITIES = frozenset(
    {"flat_model_boundary", "purged_event_overlap", "embargoed_selection"}
)
_TERMINAL_STATUSES = frozenset(
    {
        BacktestTrialStatus.COMPLETED.value,
        BacktestTrialStatus.FAILED.value,
        BacktestTrialStatus.INTERRUPTED.value,
        BacktestTrialStatus.ABANDONED.value,
    }
)
ProductMetadataProvider = Callable[[str], Mapping[str, Any]]
ExecutionCostMeasurementVerifier = Callable[[Mapping[str, object]], None]
DataParityAttestationVerifier = Callable[[Mapping[str, object]], None]

_BENCHMARK_KIND_BY_PROTOCOL_NAME = {
    "cash": BenchmarkKind.CASH.value,
    "buy_and_hold": BenchmarkKind.BUY_AND_HOLD.value,
    "training_calibrated_volatility_targeted_buy_and_hold": (
        BenchmarkKind.VOL_TARGET_BUY_AND_HOLD.value
    ),
    "sma_30_long_cash": BenchmarkKind.SMA_LONG_CASH.value,
    "momentum_90d_long_cash": BenchmarkKind.MOMENTUM_LONG_CASH.value,
}
_BENCHMARK_ARM_BY_PROTOCOL_NAME = {
    "cash": "BENCHMARK_CASH",
    "buy_and_hold": "BENCHMARK_BUY_AND_HOLD",
    "training_calibrated_volatility_targeted_buy_and_hold": "BENCHMARK_VOL_TARGET_BUY_HOLD",
    "sma_30_long_cash": "BENCHMARK_SMA_30_LONG_CASH",
    "momentum_90d_long_cash": "BENCHMARK_MOMENTUM_90D_LONG_CASH",
}
_ROBUSTNESS_ARM = "ROBUSTNESS_CONCENTRATION_INFERENCE"
_PBO_ARM = "CSCV_PBO"
_REDESIGN_RUNNER = "registered_redesign_candidate_inference"
_SCORING_LEDGER_ARM = "SCORING_BINDING_LEDGER"
_POWER_RUNNER = "prospective_power_study"
_HISTORICAL_DISPOSITION_ARM = "HISTORICAL_DISPOSITION"
_HISTORICAL_DISPOSITION_RUNNER = "historical_strategy_disposition"
_HISTORICAL_SOURCE_IDS = frozenset(
    {
        "trial_registry",
        "frozen_market_data_datasets",
        "data_parity_attestation",
        "execution_cost_measurement",
        "recursive_result_hash_reconciliation",
        "robustness_concentration_inference",
        "deflated_sharpe_diagnostics",
        "prospective_power_primary_effect",
        "scoring_binding_ledger_pooled",
        "correctness_attestation",
    }
)
_HISTORICAL_EVIDENCE_REQUIREMENT_VOCABULARY = (
    ("trial-ledger", "trial", ("trial_registry",), False),
    (
        "market-source-ledger",
        "source",
        (
            "frozen_market_data_datasets",
            "data_parity_attestation",
            "execution_cost_measurement",
        ),
        False,
    ),
    (
        "recursive-hash-reconciliation",
        "hash_reconciliation",
        ("recursive_result_hash_reconciliation",),
        False,
    ),
    (
        "robustness-ledger",
        "robustness",
        ("robustness_concentration_inference",),
        True,
    ),
    (
        "deflated-sharpe-ledger",
        "deflated_sharpe",
        ("deflated_sharpe_diagnostics",),
        False,
    ),
    (
        "power-ledger",
        "power",
        ("prospective_power_primary_effect",),
        False,
    ),
    (
        "scoring-ledger",
        "scoring",
        ("scoring_binding_ledger_pooled",),
        False,
    ),
    (
        "correctness-attestation",
        "correctness",
        ("correctness_attestation",),
        False,
    ),
)
_HISTORICAL_ALLOWED_DISPOSITIONS = (
    "RETIRE",
    "REDESIGN",
    "FREEZE_FOR_PROSPECTIVE_SHADOW",
)
_HISTORICAL_PROMOTION_CONTRACT = {
    "automatic_parameter_deployment": False,
    "historical_results_can_enable_live": False,
    "historical_results_can_enable_paper": False,
    "human_approval_required": True,
    "independent_validation_required": True,
}
_SMA_BENCHMARK_WARMUP = 30
_MOMENTUM_BENCHMARK_WARMUP = 91
_PARITY_DAYS_PER_STRATUM = 2
_EQUITY_POINT_LENGTH = 2
_SIZING_RAW_GATE_IDS = (
    "pooled_expected_cost_oos_return_positive",
    "median_oos_fold_return_positive",
    "all_assets_positive",
    "contribution_ex_best_closed_trade_positive",
    "contribution_ex_best_calendar_year_positive",
    "contribution_share_requirement_met",
    "neighbor_support_requirements_met",
    "cost_tolerance_requirements_met",
    "benchmark_pareto_not_dominated",
    "training_selector_supports_baseline",
    "expected_pooled_max_drawdown_within_limit",
    "expected_pooled_recovery_duration_within_limit",
    "expected_daily_es95_within_limit",
    "stressed_pooled_max_drawdown_within_limit",
    "worst_closed_or_terminal_contribution_loss_within_limit",
)
_ABSOLUTE_RISK_GATE_IDS = (
    "expected_pooled_max_drawdown_within_limit",
    "expected_pooled_recovery_duration_within_limit",
    "expected_daily_es95_within_limit",
    "stressed_pooled_max_drawdown_within_limit",
    "worst_closed_or_terminal_contribution_loss_within_limit",
)
_REDESIGN_QUANTITATIVE_GATE_IDS = (
    "pooled_expected_cost_oos_return_positive",
    "all_registered_assets_positive",
    "minimum_positive_expected_cost_oos_folds_met",
    "contribution_ex_best_closed_trade_positive",
    "contribution_ex_best_calendar_year_positive",
    "contribution_share_requirement_met",
    "cost_tolerance_requirements_met",
    "benchmark_pareto_not_dominated",
    *_ABSOLUTE_RISK_GATE_IDS,
    "single_registered_economically_defensible_change_confirmed",
)
_DERIVED_SYMBOL_MAX_LENGTH = 50
_EXECUTION_HELPERS = {
    "event_interval_builder": "build_report_event_intervals",
    "terminal_liquidation_estimator": "estimate_terminal_liquidation",
    "terminal_liquidation_policy_builder": "build_terminal_liquidation_cost_policy",
}
_PHASE_ONE_INDIVIDUAL_RUNNERS = frozenset(
    {
        "production_core_direct",
        "production_core_semantic_diagnostic",
        "executable_benchmark_component",
    }
)
_PRIMARY_GROUP_RUNNERS = frozenset(
    {"anchored_joint_asset_selector", "pooled_oos_primary_aggregate"}
)
_SCORING_LEDGER_GROUP_RUNNERS = frozenset(
    {"scoring_binding_ledger_component", "scoring_binding_ledger_pooled"}
)
_POWER_GROUP_RUNNERS = frozenset({_POWER_RUNNER})
_CONDITIONAL_SIZING_GROUP_RUNNERS = frozenset(
    {"conditional_sizing_oos_component", "conditional_sizing_pooled_oos"}
)
_BENCHMARK_PORTFOLIO_GROUP_RUNNERS = frozenset(
    {"equal_weight_benchmark_portfolio", "pooled_oos_benchmark_aggregate"}
)
_CSCV_GROUP_RUNNERS = frozenset({"registered_cscv_pbo"})
_ROBUSTNESS_GROUP_RUNNERS = frozenset({"robustness_concentration_inference"})
_REDESIGN_GROUP_RUNNERS = frozenset({_REDESIGN_RUNNER})
_HISTORICAL_DISPOSITION_GROUP_RUNNERS = frozenset({_HISTORICAL_DISPOSITION_RUNNER})
_DERIVED_EVIDENCE_KIND_BY_RUNNER = {
    "anchored_joint_asset_selector": "joint_asset_anchored_fold",
    "pooled_oos_primary_aggregate": "joint_asset_pooled_oos",
    "scoring_binding_ledger_component": "scoring_binding_ledger_component",
    "scoring_binding_ledger_pooled": "scoring_binding_ledger_pooled",
    "conditional_sizing_oos_component": "conditional_sizing_oos_component",
    "conditional_sizing_pooled_oos": "conditional_sizing_pooled_oos",
    "equal_weight_benchmark_portfolio": "equal_weight_benchmark_portfolio",
    "pooled_oos_benchmark_aggregate": "pooled_oos_benchmark_aggregate",
    _POWER_RUNNER: _POWER_RUNNER,
    "registered_cscv_pbo": "registered_cscv_pbo",
    "robustness_concentration_inference": "robustness_concentration_inference",
    _REDESIGN_RUNNER: _REDESIGN_RUNNER,
    _HISTORICAL_DISPOSITION_RUNNER: _HISTORICAL_DISPOSITION_RUNNER,
}
_SOURCE_ATTESTED_GROUP_RUNNERS = (
    _CSCV_GROUP_RUNNERS | _ROBUSTNESS_GROUP_RUNNERS | _REDESIGN_GROUP_RUNNERS
)
_PHASE_ONE_GROUP_RUNNERS = (
    _PRIMARY_GROUP_RUNNERS
    | _SCORING_LEDGER_GROUP_RUNNERS
    | _BENCHMARK_PORTFOLIO_GROUP_RUNNERS
    | _POWER_GROUP_RUNNERS
    | _CSCV_GROUP_RUNNERS
    | _ROBUSTNESS_GROUP_RUNNERS
    | _REDESIGN_GROUP_RUNNERS
    | _CONDITIONAL_SIZING_GROUP_RUNNERS
    | _HISTORICAL_DISPOSITION_GROUP_RUNNERS
)
_PHASE_ONE_REGISTRATION_ONLY_RUNNERS = frozenset({"abandoned_asset_mismatch"})
_PHASE_ONE_RUNNERS = (
    _PHASE_ONE_INDIVIDUAL_RUNNERS | _PHASE_ONE_GROUP_RUNNERS | _PHASE_ONE_REGISTRATION_ONLY_RUNNERS
)


def _runtime_distribution_snapshot() -> tuple[tuple[str, ...], str]:
    """Return a canonical, duplicate-free lock for the active Python environment."""

    rows_by_name: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        raw_name = distribution.metadata["Name"]
        version = distribution.version
        if not isinstance(raw_name, str) or not _DISTRIBUTION_NAME_RE.fullmatch(raw_name):
            message = "installed distribution has a missing or invalid metadata Name"
            raise ValueError(message)
        if (
            not isinstance(version, str)
            or not version
            or version.strip() != version
            or any(character.isspace() for character in version)
            or "==" in version
        ):
            message = f"installed distribution {raw_name} has an invalid version"
            raise ValueError(message)
        canonical_name = re.sub(r"[-_.]+", "-", raw_name).lower()
        if not _CANONICAL_DISTRIBUTION_NAME_RE.fullmatch(canonical_name):
            message = f"installed distribution name cannot be canonicalized: {raw_name}"
            raise ValueError(message)
        if canonical_name in rows_by_name:
            message = f"active environment contains duplicate distribution: {canonical_name}"
            raise ValueError(message)
        rows_by_name[canonical_name] = f"{canonical_name}=={version}"

    if not rows_by_name:
        message = "active environment contains no installed distributions"
        raise ValueError(message)
    rows = tuple(rows_by_name[name] for name in sorted(rows_by_name))
    digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
    return rows, digest


def _validated_frozen_runtime_distribution_lock(
    environment: Mapping[str, Any],
) -> tuple[tuple[str, ...], str]:
    """Validate the canonical rows and digest embedded in a frozen manifest."""

    raw_rows = environment.get("runtime_distributions")
    if isinstance(raw_rows, (str, bytes)) or not isinstance(raw_rows, Sequence):
        message = "frozen runtime_distributions must be a JSON array"
        raise TypeError(message)
    rows = tuple(raw_rows)
    if not rows or any(not isinstance(row, str) for row in rows):
        message = "frozen runtime_distributions must contain non-empty string rows"
        raise TypeError(message)
    rows_by_name: dict[str, str] = {}
    for row in rows:
        name, separator, version = row.partition("==")
        if (
            not separator
            or not _CANONICAL_DISTRIBUTION_NAME_RE.fullmatch(name)
            or re.sub(r"[-_.]+", "-", name).lower() != name
            or not version
            or version.strip() != version
            or any(character.isspace() for character in version)
            or "==" in version
        ):
            message = f"frozen runtime distribution row is not canonical: {row}"
            raise ValueError(message)
        if name in rows_by_name:
            message = "frozen runtime_distributions must be sorted and duplicate-free"
            raise ValueError(message)
        rows_by_name[name] = row
    expected_rows = tuple(rows_by_name[name] for name in sorted(rows_by_name))
    if rows != expected_rows:
        message = "frozen runtime_distributions must be sorted and duplicate-free"
        raise ValueError(message)
    digest = environment.get("runtime_distribution_lock_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        message = "frozen runtime distribution lock digest is invalid"
        raise ValueError(message)
    observed = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
    if observed != digest:
        message = "frozen runtime distribution rows differ from their lock digest"
        raise ValueError(message)
    return cast(tuple[str, ...], rows), digest


@dataclass(frozen=True, slots=True)
class _DerivedDiagnosticInputs:
    """Verified shared inputs for robustness and redesign evidence builders."""

    config: RobustnessEvidenceConfig
    benchmarks: tuple[RegisteredBenchmarkEvidence, ...]
    regimes: tuple[EconomicRegimeObservation, ...]
    regime_calibration: dict[str, Any]
    adaptive_selector: AdaptiveSelectorEvidence
    sharpe_dispersion: TrialSharpeDispersion
    source_attestation: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _HistoricalRegistryAudit:
    """Detached integrity result for every trial preceding final disposition."""

    status: HistoricalEvidenceStatus
    rows: tuple[dict[str, Any], ...]
    report_sources: tuple[dict[str, str], ...]
    derived_sources: tuple[dict[str, str], ...]
    derived_payloads: Mapping[str, tuple[tuple[Mapping[str, Any], dict[str, Any]], ...]]

    @property
    def complete(self) -> bool:
        return self.status is HistoricalEvidenceStatus.VERIFIED


@dataclass(frozen=True, slots=True)
class CampaignRunSummary:
    """Machine-readable result of one create, resume, or freeze-only call."""

    manifest_hash: str
    manifest_path: Path
    experiment_id: int
    total_trials: int
    selected_trials: int
    status_counts: Mapping[str, int]
    failures_this_run: int
    historical_disposition: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_hash": self.manifest_hash,
            "manifest_path": str(self.manifest_path),
            "experiment_id": self.experiment_id,
            "total_trials": self.total_trials,
            "selected_trials": self.selected_trials,
            "status_counts": dict(sorted(self.status_counts.items())),
            "failures_this_run": self.failures_this_run,
            "historical_disposition": dict(self.historical_disposition),
        }
