"""Shared immutable inputs and summaries for robustness diagnostics."""

from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass, field

from dev_cli.validation._diagnostic_common import (
    MIN_DSR_DISPERSION_VALUES,
    PERCENT_MAX,
    REPORT_SHA256_HEX_LENGTH,
    invalid,
    require_finite,
    require_nonempty,
)
from dev_cli.validation.cscv import TimestampedDailyReturn
from dev_cli.validation.performance_diagnostics import NamedReturn


@dataclass(frozen=True, slots=True)
class RegisteredOneFactorChange:
    """One pre-registered design factor represented by one arm.

    A factor is normally one parameter. Economically indivisible factors may
    use ordered mapping values when the protocol explicitly names the factor.
    """

    parameter_name: str
    baseline_value: object
    candidate_value: object

    def __post_init__(self) -> None:
        require_nonempty(self.parameter_name, field_name="parameter_name")
        if self.baseline_value == self.candidate_value:
            invalid("registered one-factor change must alter its parameter")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RobustnessEvidenceConfig:
    """Frozen identities, inference settings, and thresholds for one campaign."""

    assets: tuple[str, ...]
    fold_years: tuple[tuple[str, int], ...]
    baseline_arm_id: str
    neighbor_candidate_order: tuple[str, ...]
    benchmark_order: tuple[str, ...]
    economic_period_date_rule: str
    benchmark_comparator_id: str
    expected_cost_scenario: str
    stressed_cost_scenario: str
    deflated_sharpe_effective_trials: float
    deflated_sharpe_upstream_attempted_trials: int
    deflated_sharpe_upstream_finite_count: int
    deflated_sharpe_current_candidate_order: tuple[str, ...]
    annualization_factor: float
    bootstrap_block_bars: int
    bootstrap_samples: int
    bootstrap_seed: int
    confidence_level: float
    minimum_positive_selected_folds: int
    minimum_positive_pooled_neighbors: int
    minimum_positive_candidate_fold_cells_total: int
    minimum_training_selected_folds_supporting_baseline: int
    stressed_edge_reversal_limit_fraction: float
    maximum_positive_contribution_share: float
    maximum_expected_pooled_drawdown_pct: float
    maximum_expected_recovery_duration_days: float
    maximum_expected_daily_es95_pct: float
    maximum_stressed_pooled_drawdown_pct: float
    maximum_closed_or_terminal_contribution_loss_pct: float
    registered_redesign_changes: tuple[tuple[str, RegisteredOneFactorChange], ...] = ()


@dataclass(frozen=True, slots=True)
class TrialSharpeDispersion:
    """Attested upstream values plus every current redesign-eligible candidate."""

    upstream_attempted_trials: int
    upstream_finite_sharpes: tuple[float, ...]
    current_candidate_sharpes: tuple[tuple[str, float], ...]
    source: str


def _validated_trial_sharpe_dispersion(
    evidence: TrialSharpeDispersion,
    *,
    config: RobustnessEvidenceConfig,
) -> tuple[float, float]:
    if (
        isinstance(evidence.upstream_attempted_trials, bool)
        or not isinstance(evidence.upstream_attempted_trials, int)
        or evidence.upstream_attempted_trials != config.deflated_sharpe_upstream_attempted_trials
    ):
        invalid("DSR upstream attempted trial count differs from the frozen registry")
    if len(evidence.upstream_finite_sharpes) != config.deflated_sharpe_upstream_finite_count:
        invalid("DSR upstream finite Sharpe count differs from the attested ledger")
    if any(not math.isfinite(value) for value in evidence.upstream_finite_sharpes):
        invalid("DSR upstream Sharpe values must all be finite")
    candidate_ids = tuple(
        candidate_id for candidate_id, _value in evidence.current_candidate_sharpes
    )
    if candidate_ids != config.deflated_sharpe_current_candidate_order:
        invalid("DSR current Sharpe rows do not exactly cover the registered candidate order")
    current_values = tuple(value for _candidate_id, value in evidence.current_candidate_sharpes)
    if any(not math.isfinite(value) for value in current_values):
        invalid("DSR current candidate Sharpe values must all be finite")
    require_nonempty(evidence.source, field_name="trial_sharpe_dispersion.source")
    effective_trials = float(evidence.upstream_attempted_trials + len(current_values))
    if effective_trials != config.deflated_sharpe_effective_trials:
        invalid("DSR effective trial count differs from upstream attempts plus current candidates")
    values = (*evidence.upstream_finite_sharpes, *current_values)
    if len(values) < MIN_DSR_DISPERSION_VALUES:
        invalid("DSR Sharpe dispersion requires at least two finite values")
    average = math.fsum(values) / len(values)
    sample_variance = math.fsum((value - average) ** 2 for value in values) / (len(values) - 1)
    sample_standard_deviation = sample_variance**0.5
    if sample_standard_deviation <= 0.0:
        invalid("DSR combined cross-trial Sharpe dispersion must be positive")
    return effective_trials, sample_standard_deviation


@dataclass(frozen=True, slots=True)
class RegisteredBenchmarkEvidence:
    """One immutable expected-cost pooled benchmark return ledger."""

    benchmark_id: str
    fold_id: str
    cost_scenario: str
    source_trial_id: str
    evidence_sha256: str
    execution_definition_id: str
    daily_returns: tuple[TimestampedDailyReturn, ...]
    average_exposure_pct: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("benchmark_id", self.benchmark_id),
            ("fold_id", self.fold_id),
            ("cost_scenario", self.cost_scenario),
            ("source_trial_id", self.source_trial_id),
            ("execution_definition_id", self.execution_definition_id),
        ):
            require_nonempty(value, field_name=field_name)
        if (
            len(self.evidence_sha256) != REPORT_SHA256_HEX_LENGTH
            or self.evidence_sha256 != self.evidence_sha256.lower()
            or any(character not in "0123456789abcdef" for character in self.evidence_sha256)
        ):
            invalid("benchmark evidence_sha256 must be a lowercase SHA-256 digest")
        if not self.daily_returns:
            invalid("benchmark daily return ledger must not be empty")
        if any(
            current.timestamp <= previous.timestamp
            for previous, current in itertools.pairwise(self.daily_returns)
        ):
            invalid("benchmark daily return timestamps must be strictly increasing")
        require_finite(self.average_exposure_pct, field_name="average_exposure_pct")
        if not 0.0 <= self.average_exposure_pct <= PERCENT_MAX:
            invalid("average_exposure_pct must be in [0, 100]")

    def source_dict(self) -> dict[str, object]:
        return {
            "benchmark_id": self.benchmark_id,
            "fold_id": self.fold_id,
            "cost_scenario": self.cost_scenario,
            "source_trial_id": self.source_trial_id,
            "evidence_sha256": self.evidence_sha256,
            "execution_definition_id": self.execution_definition_id,
        }


@dataclass(frozen=True, slots=True)
class AdaptiveFoldSelection:
    fold_id: str
    selected_arm_id: str
    baseline_supported_within_tolerance: bool

    def __post_init__(self) -> None:
        require_nonempty(self.fold_id, field_name="fold_id")
        require_nonempty(self.selected_arm_id, field_name="selected_arm_id")
        if not isinstance(self.baseline_supported_within_tolerance, bool):
            invalid("baseline_supported_within_tolerance must be a bool")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AdaptiveSelectorEvidence:
    """Optional training-selected evidence, barred from rescuing the fixed baseline."""

    selections: tuple[AdaptiveFoldSelection, ...]
    fold_returns: tuple[NamedReturn, ...]
    asset_returns: tuple[NamedReturn, ...]
    pooled_daily_returns: tuple[TimestampedDailyReturn, ...]


@dataclass(frozen=True, slots=True)
class BootstrapInferenceSummary:
    compounded_return_pct: dict[str, object]
    annualized_sharpe: dict[str, object]
    evidence_role: str = field(default="retrospective_diagnostic", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "compounded_return_pct": self.compounded_return_pct,
            "annualized_sharpe": self.annualized_sharpe,
            "evidence_role": self.evidence_role,
        }


@dataclass(frozen=True, slots=True)
class SharpeDiagnosticAvailability:
    available: bool
    unavailable_reason: str | None
    diagnostic: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
