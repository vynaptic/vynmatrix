"""Registered robustness inputs, regimes, and source attestations."""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from dev_cli.validation.backtest.execution import (
    normalize_execution_bar,
)
from dev_cli.validation.backtest.metrics import sharpe_ratio
from dev_cli.validation.campaign_composition import CampaignMixinContract
from dev_cli.validation.campaign_contracts import (
    _BENCHMARK_ARM_BY_PROTOCOL_NAME,
    _EQUITY_POINT_LENGTH,
    _SHA256_RE,
    _DerivedDiagnosticInputs,
)
from dev_cli.validation.cscv import TimestampedDailyReturn
from dev_cli.validation.economic_regimes import (
    EconomicRegimeCalibrationConfig,
    EconomicRegimeObservation,
    RegimeDecisionClose,
    RegimeDecisionCloseSeries,
    RegimeFoldIndexRegistry,
    RegimeFoldIndices,
    build_no_lookahead_economic_regimes,
)
from dev_cli.validation.evidence import parse_utc_datetime
from dev_cli.validation.performance_diagnostics import NamedReturn
from dev_cli.validation.persistence.backtest_manifest_store import (
    manifest_sha256,
)
from dev_cli.validation.persistence.backtest_selection_ledger import (
    validate_embedded_selection_ledger,
    validated_prior_diagnostic_attempt_count,
)
from dev_cli.validation.report_evidence import (
    StandardReportEvidence,
)
from dev_cli.validation.robustness_models import (
    AdaptiveFoldSelection,
    AdaptiveSelectorEvidence,
    RegisteredBenchmarkEvidence,
    RegisteredOneFactorChange,
    RobustnessEvidenceConfig,
    TrialSharpeDispersion,
)


class CampaignRobustnessEvidenceMixin(CampaignMixinContract):
    def _build_derived_diagnostic_inputs(
        self,
        robustness_spec: Mapping[str, Any],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> _DerivedDiagnosticInputs:
        config = self._robustness_evidence_config(robustness_spec)
        benchmarks, benchmark_attestation = self._registered_benchmark_evidence(
            config,
            all_specs=all_specs,
            trial_ids=trial_ids,
            manifest=manifest,
        )
        regimes, regime_payload, regime_attestation = self._economic_regime_evidence(
            robustness_spec,
            config=config,
            all_specs=all_specs,
            trial_ids=trial_ids,
            manifest=manifest,
        )
        adaptive, adaptive_attestation = self._adaptive_selector_evidence(
            robustness_spec,
            config=config,
            all_specs=all_specs,
            trial_ids=trial_ids,
        )
        dispersion, dispersion_attestation = self._trial_sharpe_dispersion(
            config=config,
            all_specs=all_specs,
            trial_ids=trial_ids,
            manifest=manifest,
        )
        return _DerivedDiagnosticInputs(
            config=config,
            benchmarks=benchmarks,
            regimes=regimes,
            regime_calibration=regime_payload,
            adaptive_selector=adaptive,
            sharpe_dispersion=dispersion,
            source_attestation=self._merge_source_attestations(
                benchmark_attestation,
                regime_attestation,
                adaptive_attestation,
                dispersion_attestation,
            ),
        )

    def _robustness_evidence_config(
        self,
        spec: Mapping[str, Any],
    ) -> RobustnessEvidenceConfig:
        parameters = self._mapping(spec, "parameters")
        robustness = self._mapping(parameters, "robustness")
        neighbor = self._mapping(robustness, "neighbor_support")
        cost = self._mapping(robustness, "cost_tolerance")
        absolute = self._mapping(robustness, "absolute_risk_budget")
        gates = self._mapping(parameters, "historical_disposition_gates")
        comparator = self._mapping(gates, "benchmark_comparator")
        multiple_testing = self._mapping(parameters, "multiple_testing")
        combination = self._mapping(multiple_testing, "deflated_sharpe_combination")
        upstream_contract = self._mapping(
            multiple_testing,
            "upstream_selection_ledger_contract",
        )
        inference = self._mapping(parameters, "inference")
        raw_folds = parameters.get("source_fold_ids")
        raw_neighbors = parameters.get("neighbor_candidate_order")
        raw_current = parameters.get("current_economically_compared_candidate_order")
        raw_changes = parameters.get("registered_one_factor_changes")
        if any(
            isinstance(value, (str, bytes)) or not isinstance(value, Sequence)
            for value in (raw_folds, raw_neighbors, raw_current, raw_changes)
        ):
            message = "robustness fold, candidate, and redesign registries must be arrays"
            raise TypeError(message)
        fold_rows = cast(Sequence[Any], raw_folds)
        neighbor_rows = cast(Sequence[Any], raw_neighbors)
        current_rows = cast(Sequence[Any], raw_current)
        change_rows = cast(Sequence[Any], raw_changes)
        fold_ids = tuple(str(fold_id) for fold_id in fold_rows)
        fold_years = tuple((fold_id, self._fold_year(fold_id)) for fold_id in fold_ids)
        current_order = tuple(str(candidate) for candidate in current_rows)
        attempted = int(combination["upstream_attempted_trial_count"])
        effective = attempted + len(current_order)
        if int(combination["effective_trial_count"]) != effective:
            message = (
                "DSR effective trials must equal every upstream attempt plus the "
                "registered current candidate count"
            )
            raise RuntimeError(message)
        changes: list[tuple[str, RegisteredOneFactorChange]] = []
        for index, raw_change in enumerate(change_rows):
            change = self._require_mapping(
                raw_change,
                field=f"registered_one_factor_changes[{index}]",
            )
            changes.append(
                (
                    self._nonempty_string(
                        change.get("candidate_id"),
                        field="redesign.candidate_id",
                    ),
                    RegisteredOneFactorChange(
                        parameter_name=self._nonempty_string(
                            change.get("parameter_name"),
                            field="redesign.parameter_name",
                        ),
                        baseline_value=change.get("baseline_value"),
                        candidate_value=change.get("candidate_value"),
                    ),
                )
            )
        raw_benchmarks = comparator.get("registered_benchmark_order")
        if isinstance(raw_benchmarks, (str, bytes)) or not isinstance(raw_benchmarks, Sequence):
            message = "registered benchmark order must be a JSON array"
            raise TypeError(message)
        return RobustnessEvidenceConfig(
            assets=self._registered_component_products(parameters),
            fold_years=fold_years,
            baseline_arm_id=str(parameters["source_arm_id"]),
            neighbor_candidate_order=tuple(str(candidate) for candidate in neighbor_rows),
            benchmark_order=tuple(str(benchmark) for benchmark in raw_benchmarks),
            economic_period_date_rule=str(parameters["economic_period_date_rule"]),
            benchmark_comparator_id=str(comparator["comparator_id"]),
            expected_cost_scenario=str(cost["primary"]),
            stressed_cost_scenario="stressed",
            deflated_sharpe_effective_trials=float(effective),
            deflated_sharpe_upstream_attempted_trials=attempted,
            deflated_sharpe_upstream_finite_count=int(
                upstream_contract["expected_finite_sharpe_count"]
            ),
            deflated_sharpe_current_candidate_order=current_order,
            annualization_factor=float(inference["annualization_factor"]),
            bootstrap_block_bars=int(inference["daily_return_block_size_bars"]),
            bootstrap_samples=int(inference["block_bootstrap_samples"]),
            bootstrap_seed=int(inference["block_bootstrap_seed"]),
            confidence_level=float(inference["confidence_level"]),
            minimum_positive_selected_folds=int(
                neighbor["minimum_positive_oos_folds_for_selected_arm"]
            ),
            minimum_positive_pooled_neighbors=int(neighbor["minimum_positive_pooled_neighbors"]),
            minimum_positive_candidate_fold_cells_total=int(
                neighbor["minimum_positive_candidate_fold_cells_total"]
            ),
            minimum_training_selected_folds_supporting_baseline=int(
                neighbor["minimum_training_selected_folds_supporting_baseline"]
            ),
            stressed_edge_reversal_limit_fraction=float(
                cost["stressed_edge_reversal_limit_fraction"]
            ),
            maximum_positive_contribution_share=float(
                robustness["maximum_absolute_positive_contribution_share"]
            ),
            maximum_expected_pooled_drawdown_pct=float(
                absolute["maximum_expected_pooled_drawdown_pct"]
            ),
            maximum_expected_recovery_duration_days=float(
                absolute["maximum_expected_recovery_duration_days"]
            ),
            maximum_expected_daily_es95_pct=float(absolute["maximum_expected_daily_es95_pct"]),
            maximum_stressed_pooled_drawdown_pct=float(
                absolute["maximum_stressed_pooled_drawdown_pct"]
            ),
            maximum_closed_or_terminal_contribution_loss_pct=float(
                absolute["maximum_closed_or_terminal_contribution_loss_pct"]
            ),
            registered_redesign_changes=tuple(changes),
        )

    @staticmethod
    def _fold_year(fold_id: str) -> int:
        if not fold_id.startswith("oos-"):
            message = f"robustness fold identity is not an OOS year: {fold_id}"
            raise ValueError(message)
        try:
            return int(fold_id.removeprefix("oos-"))
        except ValueError as exc:
            message = f"robustness fold identity has no calendar year: {fold_id}"
            raise ValueError(message) from exc

    def _robustness_source_reports(
        self,
        config: RobustnessEvidenceConfig,
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> tuple[StandardReportEvidence, ...]:
        direct = self._direct_report_spec_index(all_specs, manifest=manifest)
        expected = tuple(
            self._required_standard_report(
                direct,
                arm_id=arm,
                product=asset,
                fold_id=fold_id,
                cost_scenario=config.expected_cost_scenario,
                trial_ids=trial_ids,
                manifest=manifest,
            )
            for arm in config.neighbor_candidate_order
            for fold_id, _year in config.fold_years
            for asset in config.assets
        )
        stressed = tuple(
            self._required_standard_report(
                direct,
                arm_id=config.baseline_arm_id,
                product=asset,
                fold_id=fold_id,
                cost_scenario=config.stressed_cost_scenario,
                trial_ids=trial_ids,
                manifest=manifest,
            )
            for fold_id, _year in config.fold_years
            for asset in config.assets
        )
        return (*expected, *stressed)

    def _redesign_source_reports(
        self,
        config: RobustnessEvidenceConfig,
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> tuple[StandardReportEvidence, ...]:
        direct = self._direct_report_spec_index(all_specs, manifest=manifest)
        candidate_ids = tuple(
            candidate_id for candidate_id, _change in config.registered_redesign_changes
        )
        return tuple(
            self._required_standard_report(
                direct,
                arm_id=candidate,
                product=asset,
                fold_id=fold_id,
                cost_scenario=cost_scenario,
                trial_ids=trial_ids,
                manifest=manifest,
            )
            for candidate in candidate_ids
            for cost_scenario in (
                config.expected_cost_scenario,
                config.stressed_cost_scenario,
            )
            for fold_id, _year in config.fold_years
            for asset in config.assets
        )

    def _required_standard_report(
        self,
        direct: Mapping[tuple[str, str, str, str], Mapping[str, Any]],
        *,
        arm_id: str,
        product: str,
        fold_id: str,
        cost_scenario: str,
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> StandardReportEvidence:
        identity = (arm_id, product, fold_id, cost_scenario)
        spec = direct.get(identity)
        if spec is None:
            message = "required report evidence is absent: " + "/".join(identity)
            raise RuntimeError(message)
        return self._standard_report_from_spec(
            spec,
            trial_ids=trial_ids,
            manifest=manifest,
        )

    def _registered_benchmark_evidence(
        self,
        config: RobustnessEvidenceConfig,
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> tuple[tuple[RegisteredBenchmarkEvidence, ...], dict[str, Any]]:
        arm_by_name = {
            name: _BENCHMARK_ARM_BY_PROTOCOL_NAME[name]
            for name in config.benchmark_order
            if name in _BENCHMARK_ARM_BY_PROTOCOL_NAME
        }
        if tuple(arm_by_name) != config.benchmark_order:
            message = "benchmark order contains an unsupported registered benchmark"
            raise ValueError(message)
        by_identity = {
            (str(spec["arm_id"]), str(spec["cost_scenario"])): spec
            for spec in all_specs
            if spec.get("runner_kind") == "pooled_oos_benchmark_aggregate"
        }
        if len(by_identity) != sum(
            spec.get("runner_kind") == "pooled_oos_benchmark_aggregate" for spec in all_specs
        ):
            message = "pooled benchmark source identities are not unique"
            raise RuntimeError(message)
        output: list[RegisteredBenchmarkEvidence] = []
        derived_sources: list[dict[str, str]] = []
        report_sources: list[dict[str, str]] = []
        for benchmark_id in config.benchmark_order:
            arm_id = arm_by_name[benchmark_id]
            spec = by_identity.get((arm_id, config.expected_cost_scenario))
            if spec is None:
                message = f"required expected-cost benchmark evidence is absent: {benchmark_id}"
                raise RuntimeError(message)
            trial_id = trial_ids[int(spec["sequence"])]
            payload = self._result_store.load_derived_payload(
                trial_id,
                expected_evidence_kind="pooled_oos_benchmark_aggregate",
            )
            if (
                payload.get("runner_kind") != "pooled_oos_benchmark_aggregate"
                or payload.get("protocol_id") != manifest["protocol_id"]
                or payload.get("component_cost_scenario") != config.expected_cost_scenario
            ):
                message = f"pooled benchmark provenance differs: {benchmark_id}"
                raise RuntimeError(message)
            pooled = self._mapping(payload, "pooled_oos")
            observations = self._timestamped_returns(
                pooled.get("observations"),
                field=f"benchmark.{benchmark_id}.pooled_oos.observations",
            )
            observations = self._economic_period_returns(
                observations,
                date_rule=config.economic_period_date_rule,
            )
            exposure, fold_sources, component_sources = (
                self._benchmark_source_exposure_and_attestation(
                    spec,
                    payload=payload,
                    all_specs=all_specs,
                    trial_ids=trial_ids,
                )
            )
            payload_sha256 = manifest_sha256({"payload": payload})
            derived_sources.append(
                {
                    "evidence_kind": "pooled_oos_benchmark_aggregate",
                    "evidence_payload_sha256": payload_sha256,
                    "trial_id": trial_id,
                }
            )
            derived_sources.extend(fold_sources)
            report_sources.extend(component_sources)
            definition_id = (
                f"registered-benchmark:{benchmark_id}:"
                f"{manifest_sha256(self._mapping(spec, 'parameters'))[:16]}"
            )
            output.append(
                RegisteredBenchmarkEvidence(
                    benchmark_id=benchmark_id,
                    fold_id="pooled-oos",
                    cost_scenario=config.expected_cost_scenario,
                    source_trial_id=trial_id,
                    evidence_sha256=payload_sha256,
                    execution_definition_id=definition_id,
                    daily_returns=observations,
                    average_exposure_pct=exposure,
                )
            )
        return tuple(output), self._normalized_source_attestation(
            report_sources=report_sources,
            derived_sources=derived_sources,
        )

    def _benchmark_source_exposure_and_attestation(
        self,
        pooled_spec: Mapping[str, Any],
        *,
        payload: Mapping[str, Any],
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
    ) -> tuple[float, list[dict[str, str]], list[dict[str, str]]]:
        parameters = self._mapping(pooled_spec, "parameters")
        raw_fold_ids = parameters.get("source_fold_ids")
        raw_sources = payload.get("source_folds")
        if (
            isinstance(raw_fold_ids, (str, bytes))
            or not isinstance(raw_fold_ids, Sequence)
            or isinstance(raw_sources, (str, bytes))
            or not isinstance(raw_sources, Sequence)
        ):
            message = "pooled benchmark fold registry is malformed"
            raise TypeError(message)
        fold_ids = tuple(str(fold_id) for fold_id in raw_fold_ids)
        source_rows = [
            self._require_mapping(row, field="benchmark.source_folds[]") for row in raw_sources
        ]
        if tuple(str(row.get("fold_id")) for row in source_rows) != fold_ids:
            message = "pooled benchmark source folds differ from the frozen registry"
            raise RuntimeError(message)
        fold_specs = {
            str(spec["fold_id"]): spec
            for spec in all_specs
            if spec.get("runner_kind") == "equal_weight_benchmark_portfolio"
            and spec.get("arm_id") == pooled_spec.get("arm_id")
            and spec.get("cost_scenario") == pooled_spec.get("cost_scenario")
            and spec.get("fold_id") != "full-panel"
        }
        if set(fold_specs) != set(fold_ids):
            message = "benchmark OOS fold evidence does not exactly cover its sources"
            raise RuntimeError(message)
        exposure_weighted = 0.0
        total_observations = 0
        derived_sources: list[dict[str, str]] = []
        report_sources: list[dict[str, str]] = []
        for source in source_rows:
            fold_id = str(source["fold_id"])
            fold_spec = fold_specs[fold_id]
            trial_id = trial_ids[int(fold_spec["sequence"])]
            if source.get("trial_id") != trial_id:
                message = f"benchmark fold trial identity differs: {fold_id}"
                raise RuntimeError(message)
            fold_payload = self._result_store.load_derived_payload(
                trial_id,
                expected_evidence_kind="equal_weight_benchmark_portfolio",
            )
            portfolio = self._mapping(fold_payload, "benchmark_portfolio")
            if source.get("portfolio_evidence_sha256") != manifest_sha256(
                {"benchmark_portfolio": portfolio}
            ):
                message = f"benchmark fold content hash differs: {fold_id}"
                raise RuntimeError(message)
            metrics = self._mapping(portfolio, "metrics")
            observations = int(metrics["observations"])
            components = self._list_of_mappings(portfolio, "components")
            fold_exposure = math.fsum(
                float(component["weight"])
                * float(self._mapping(component, "report")["exposure_pct"])
                for component in components
            )
            exposure_weighted += fold_exposure * observations
            total_observations += observations
            payload_sha256 = manifest_sha256({"payload": fold_payload})
            derived_sources.append(
                {
                    "evidence_kind": "equal_weight_benchmark_portfolio",
                    "evidence_payload_sha256": payload_sha256,
                    "trial_id": trial_id,
                }
            )
            for component_source in self._list_of_mappings(
                fold_payload,
                "source_components",
            ):
                component_trial_id = str(component_source["trial_id"])
                observed_sha256 = self._result_store.verified_report_sha256(component_trial_id)
                if component_source.get("aggregation_input_sha256") != observed_sha256:
                    message = f"benchmark component report hash differs: {component_trial_id}"
                    raise RuntimeError(message)
                report_sources.append(
                    {
                        "report_sha256": observed_sha256,
                        "trial_id": component_trial_id,
                    }
                )
        if total_observations <= 0:
            message = "benchmark pooled exposure has no observations"
            raise RuntimeError(message)
        return exposure_weighted / total_observations, derived_sources, report_sources

    def _timestamped_returns(
        self,
        raw: Any,
        *,
        field: str,
    ) -> tuple[TimestampedDailyReturn, ...]:
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            message = f"{field} must be a JSON array"
            raise TypeError(message)
        rows: list[TimestampedDailyReturn] = []
        for index, item in enumerate(raw):
            row = self._require_mapping(item, field=f"{field}[{index}]")
            rows.append(
                TimestampedDailyReturn(
                    timestamp=parse_utc_datetime(
                        row.get("timestamp"),
                        field=f"{field}[{index}].timestamp",
                    ),
                    daily_return=float(row["daily_return"]),
                )
            )
        if not rows:
            message = f"{field} must not be empty"
            raise ValueError(message)
        return tuple(rows)

    @staticmethod
    def _economic_period_returns(
        observations: Sequence[TimestampedDailyReturn],
        *,
        date_rule: str,
    ) -> tuple[TimestampedDailyReturn, ...]:
        """Map immutable decision-close evidence onto its registered return period."""

        if date_rule != "normalized_close_timestamp_minus_one_calendar_day":
            message = "unsupported economic-period date rule"
            raise ValueError(message)
        return tuple(
            TimestampedDailyReturn(
                timestamp=datetime.combine(
                    (observation.timestamp - timedelta(days=1)).date(),
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
                daily_return=observation.daily_return,
            )
            for observation in observations
        )

    @classmethod
    def _verified_economic_period_returns(
        cls,
        persisted: Sequence[TimestampedDailyReturn],
        reconstructed: Sequence[TimestampedDailyReturn],
        *,
        date_rule: str,
        field: str,
    ) -> tuple[TimestampedDailyReturn, ...]:
        """Verify immutable source evidence before adapting its date key."""

        if [row.to_dict() for row in persisted] != [row.to_dict() for row in reconstructed]:
            message = f"{field} differs from its complete fold sources"
            raise RuntimeError(message)
        return cls._economic_period_returns(reconstructed, date_rule=date_rule)

    def _validated_regime_assignment(
        self,
        robustness: Mapping[str, Any],
    ) -> dict[str, Any]:
        contract = self._mapping(robustness, "regime_assignment")
        expected = {
            "calibration_id": "registered_training_only_close_regime_v1",
            "role": "retrospective_descriptive_only",
            "volatility_lookback_bars": 30,
            "volatility_measure": ("lagged_30_bar_close_to_close_realized_volatility"),
            "oos_volatility_thresholds": (
                "per_asset_quartiles_fitted_on_each_folds_purged_training_returns_only"
            ),
            "trend_measure": "lagged_90_bar_close_return",
            "trend_lookback_bars": 90,
            "trend_buckets": "positive_nonpositive",
            "missing_history_bucket": "unclassified",
        }
        if contract != expected:
            message = "economic-regime assignment differs from the implemented contract"
            raise ValueError(message)
        return contract

    def _economic_regime_evidence(
        self,
        robustness_spec: Mapping[str, Any],
        *,
        config: RobustnessEvidenceConfig,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> tuple[
        tuple[EconomicRegimeObservation, ...],
        dict[str, Any],
        dict[str, Any],
    ]:
        parameters = self._mapping(robustness_spec, "parameters")
        regime_contract = self._validated_regime_assignment(self._mapping(parameters, "robustness"))
        fold_registry, fold_sources = self._regime_fold_registry(
            config,
            all_specs=all_specs,
            trial_ids=trial_ids,
        )
        direct = self._direct_report_spec_index(all_specs, manifest=manifest)
        close_series: list[RegimeDecisionCloseSeries] = []
        dataset_sources: list[dict[str, str]] = []
        reference_grid: tuple[datetime, ...] | None = None
        for asset in config.assets:
            source_spec = direct.get(
                (
                    config.baseline_arm_id,
                    asset,
                    "full-panel",
                    config.expected_cost_scenario,
                )
            )
            if source_spec is None:
                message = f"regime close source is absent for {asset}"
                raise RuntimeError(message)
            dataset, bars, audit = self._load_trial_dataset(
                source_spec,
                manifest=manifest,
            )
            component = self._mapping(source_spec, "parameters").get("component_dataset")
            if not isinstance(component, Mapping):
                # Production-core direct trials identify the dataset on the
                # trial itself; bind the immutable manifest audit explicitly.
                matching_components = [
                    item
                    for item in self._list_of_mappings(self._mapping(manifest, "data"), "datasets")
                    if item["dataset_id"] == source_spec["dataset_id"]
                ]
                if len(matching_components) != 1:
                    message = f"regime dataset manifest identity differs for {asset}"
                    raise RuntimeError(message)
                component = matching_components[0]
            normalized = tuple(normalize_execution_bar(bar) for bar in bars)
            timestamps = tuple(bar.timestamp for bar in normalized)
            if reference_grid is None:
                reference_grid = timestamps
            elif timestamps != reference_grid:
                message = "regime close sources do not share the exact decision grid"
                raise RuntimeError(message)
            source_sha256 = str(self._mapping(component, "audit")["ohlcv_sha256"])
            if audit.get("ohlcv_sha256") != source_sha256:
                message = f"regime dataset audit differs from the manifest: {asset}"
                raise RuntimeError(message)
            source_id = str(dataset["dataset_id"])
            close_series.append(
                RegimeDecisionCloseSeries(
                    asset=asset,
                    source_id=source_id,
                    source_sha256=source_sha256,
                    observations=tuple(
                        RegimeDecisionClose(
                            index=index,
                            timestamp=bar.timestamp,
                            close=float(bar.close),
                        )
                        for index, bar in enumerate(normalized)
                    ),
                )
            )
            dataset_sources.append(
                {
                    "dataset_id": source_id,
                    "ohlcv_sha256": source_sha256,
                }
            )
        if reference_grid is None:
            message = "regime close source registry is empty"
            raise RuntimeError(message)
        calibration = EconomicRegimeCalibrationConfig(
            calibration_id=self._nonempty_string(
                regime_contract.get("calibration_id"),
                field="robustness.regime_assignment.calibration_id",
            ),
            asset_order=config.assets,
            fold_order=tuple(fold_id for fold_id, _year in config.fold_years),
            volatility_lookback_bars=int(regime_contract["volatility_lookback_bars"]),
            trend_lookback_bars=int(regime_contract["trend_lookback_bars"]),
            annualization_factor=config.annualization_factor,
            economic_period_date_rule=config.economic_period_date_rule,
        )
        result = build_no_lookahead_economic_regimes(
            tuple(close_series),
            config=calibration,
            fold_registry=fold_registry,
        )
        regime_payload = result.to_dict()
        regime_payload["fold_source_attestation"] = fold_sources
        return (
            result.observations,
            regime_payload,
            self._normalized_source_attestation(
                derived_sources=fold_sources,
                dataset_sources=dataset_sources,
            ),
        )

    def _regime_fold_registry(
        self,
        config: RobustnessEvidenceConfig,
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
    ) -> tuple[RegimeFoldIndexRegistry, list[dict[str, str]]]:
        specs = {
            str(spec["fold_id"]): spec
            for spec in all_specs
            if spec.get("runner_kind") == "anchored_joint_asset_selector"
        }
        expected_folds = tuple(fold_id for fold_id, _year in config.fold_years)
        if set(specs) != set(expected_folds):
            message = "primary fold evidence does not exactly cover regime folds"
            raise RuntimeError(message)
        folds: list[RegimeFoldIndices] = []
        derived_sources: list[dict[str, str]] = []
        for fold_id in expected_folds:
            spec = specs[fold_id]
            trial_id = trial_ids[int(spec["sequence"])]
            payload = self._result_store.load_derived_payload(
                trial_id,
                expected_evidence_kind="joint_asset_anchored_fold",
            )
            if (
                payload.get("runner_kind") != "anchored_joint_asset_selector"
                or payload.get("registered_fold_id") != fold_id
            ):
                message = f"primary fold provenance differs for regime source: {fold_id}"
                raise RuntimeError(message)
            window = self._mapping(payload, "fold")
            fold = self._mapping(window, "fold")
            training = self._integer_index_tuple(
                fold.get("training_indices"),
                field=f"regime.{fold_id}.training_indices",
            )
            test = self._integer_index_tuple(
                fold.get("test_indices"),
                field=f"regime.{fold_id}.test_indices",
            )
            folds.append(
                RegimeFoldIndices(
                    fold_id=fold_id,
                    training_indices=training,
                    test_indices=test,
                )
            )
            derived_sources.append(
                {
                    "evidence_kind": "joint_asset_anchored_fold",
                    "evidence_payload_sha256": manifest_sha256({"payload": payload}),
                    "trial_id": trial_id,
                }
            )
        fold_source_payload = {
            "folds": [
                {
                    "fold_id": fold.fold_id,
                    "training_indices": list(fold.training_indices),
                    "test_indices": list(fold.test_indices),
                }
                for fold in folds
            ],
            "sources": derived_sources,
        }
        return (
            RegimeFoldIndexRegistry(
                source_id="registered-primary-fold-index-ledger-v1",
                source_sha256=manifest_sha256(fold_source_payload),
                folds=tuple(folds),
            ),
            derived_sources,
        )

    def _integer_index_tuple(self, raw: Any, *, field: str) -> tuple[int, ...]:
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            message = f"{field} must be a JSON array"
            raise TypeError(message)
        values = tuple(raw)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            message = f"{field} must contain only integer indices"
            raise TypeError(message)
        return cast(tuple[int, ...], values)

    def _adaptive_selector_evidence(
        self,
        robustness_spec: Mapping[str, Any],
        *,
        config: RobustnessEvidenceConfig,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
    ) -> tuple[AdaptiveSelectorEvidence, dict[str, Any]]:
        parameters = self._mapping(robustness_spec, "parameters")
        expected_arm = str(parameters["adaptive_selector_diagnostic_arm_id"])
        fold_specs = {
            str(spec["fold_id"]): spec
            for spec in all_specs
            if spec.get("runner_kind") == "anchored_joint_asset_selector"
            and spec.get("arm_id") == expected_arm
        }
        pooled_specs = [
            spec
            for spec in all_specs
            if spec.get("runner_kind") == "pooled_oos_primary_aggregate"
            and spec.get("arm_id") == expected_arm
        ]
        expected_folds = tuple(fold_id for fold_id, _year in config.fold_years)
        if set(fold_specs) != set(expected_folds) or len(pooled_specs) != 1:
            message = "adaptive selector evidence does not exactly cover registered folds"
            raise RuntimeError(message)
        selections: list[AdaptiveFoldSelection] = []
        fold_returns: list[NamedReturn] = []
        pooled_returns: list[TimestampedDailyReturn] = []
        asset_returns: dict[str, list[TimestampedDailyReturn]] = {
            asset: [] for asset in config.assets
        }
        derived_sources: list[dict[str, str]] = []
        for fold_id in expected_folds:
            spec = fold_specs[fold_id]
            spec_parameters = self._mapping(spec, "parameters")
            candidate_ids = tuple(str(item) for item in spec_parameters["candidate_arm_ids"])
            if candidate_ids != config.neighbor_candidate_order:
                message = f"adaptive selector candidates differ for {fold_id}"
                raise RuntimeError(message)
            trial_id = trial_ids[int(spec["sequence"])]
            payload = self._result_store.load_derived_payload(
                trial_id,
                expected_evidence_kind="joint_asset_anchored_fold",
            )
            window = self._mapping(payload, "fold")
            selected_index = int(window["selected_registry_index"])
            if not 0 <= selected_index < len(candidate_ids):
                message = f"adaptive selected index is outside the registry: {fold_id}"
                raise RuntimeError(message)
            score_rows = self._list_of_mappings(window, "training_score_ledger")
            scores = {
                int(row["registry_index"]): float(row["annualized_sharpe"]) for row in score_rows
            }
            if set(scores) != set(range(len(candidate_ids))):
                message = f"adaptive training score ledger is incomplete: {fold_id}"
                raise RuntimeError(message)
            baseline_index = candidate_ids.index(config.baseline_arm_id)
            tolerance = float(spec_parameters["selected_arm_tie_tolerance"])
            baseline_supported = (
                selected_index == baseline_index
                or abs(scores[selected_index] - scores[baseline_index]) <= tolerance
            )
            selections.append(
                AdaptiveFoldSelection(
                    fold_id=fold_id,
                    selected_arm_id=candidate_ids[selected_index],
                    baseline_supported_within_tolerance=baseline_supported,
                )
            )
            equal_weight = self._mapping(window, "equal_weight_oos_series")
            fold_daily = self._curve_daily_returns(
                equal_weight.get("equity_curve"),
                initial_capital=float(equal_weight["initial_capital"]),
                field=f"adaptive.{fold_id}.equal_weight_oos_series",
            )
            fold_returns.append(
                NamedReturn(
                    fold_id,
                    self._compounded_return_from_daily(fold_daily),
                )
            )
            pooled_returns.extend(fold_daily)
            terminal_costs = {
                str(item["asset"]): float(item["amount"])
                for item in self._list_of_mappings(window, "oos_terminal_exit_costs")
            }
            asset_rows = self._list_of_mappings(window, "oos_asset_reports")
            by_asset = {str(row["asset"]): row for row in asset_rows}
            if set(by_asset) != set(config.assets):
                message = f"adaptive OOS assets differ for {fold_id}"
                raise RuntimeError(message)
            for asset in config.assets:
                asset_row = by_asset[asset]
                report = self._mapping(asset_row, "report")
                asset_returns[asset].extend(
                    self._curve_daily_returns(
                        asset_row.get("equity_curve"),
                        initial_capital=float(report["initial_capital"]),
                        final_equity_adjustment=terminal_costs.get(asset, 0.0),
                        field=f"adaptive.{fold_id}.{asset}.equity_curve",
                    )
                )
            derived_sources.append(
                {
                    "evidence_kind": "joint_asset_anchored_fold",
                    "evidence_payload_sha256": manifest_sha256({"payload": payload}),
                    "trial_id": trial_id,
                }
            )
        pooled_spec = pooled_specs[0]
        pooled_trial_id = trial_ids[int(pooled_spec["sequence"])]
        pooled_payload = self._result_store.load_derived_payload(
            pooled_trial_id,
            expected_evidence_kind="joint_asset_pooled_oos",
        )
        persisted_pooled = self._timestamped_returns(
            self._mapping(pooled_payload, "pooled_oos").get("observations"),
            field="adaptive.pooled_oos.observations",
        )
        economic_pooled_returns = self._verified_economic_period_returns(
            persisted_pooled,
            pooled_returns,
            date_rule=config.economic_period_date_rule,
            field="adaptive pooled evidence",
        )
        derived_sources.append(
            {
                "evidence_kind": "joint_asset_pooled_oos",
                "evidence_payload_sha256": manifest_sha256({"payload": pooled_payload}),
                "trial_id": pooled_trial_id,
            }
        )
        return (
            AdaptiveSelectorEvidence(
                selections=tuple(selections),
                fold_returns=tuple(fold_returns),
                asset_returns=tuple(
                    NamedReturn(
                        asset,
                        self._compounded_return_from_daily(asset_returns[asset]),
                    )
                    for asset in config.assets
                ),
                pooled_daily_returns=economic_pooled_returns,
            ),
            self._normalized_source_attestation(derived_sources=derived_sources),
        )

    def _trial_sharpe_dispersion(
        self,
        *,
        config: RobustnessEvidenceConfig,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> tuple[TrialSharpeDispersion, dict[str, Any]]:
        """Bind DSR to every attested prior attempt and registered current arm."""

        multiple_testing = self._mapping(manifest, "multiple_testing")
        ledger_contract = self._mapping(
            multiple_testing,
            "upstream_selection_ledger_contract",
        )
        ledger = self._mapping(manifest, "upstream_selection_ledger")
        validate_embedded_selection_ledger(ledger, ledger_contract)
        normalization = self._mapping(ledger, "normalization")
        sharpe_field = self._nonempty_string(
            normalization.get("sharpe_field"),
            field="upstream_selection_ledger.normalization.sharpe_field",
        )
        upstream_rows = self._list_of_mappings(ledger, "trials")
        prior_diagnostic_attempts = validated_prior_diagnostic_attempt_count(
            manifest,
            repo_root=(
                self._repo_root
                if multiple_testing.get("prior_diagnostic_attempts") is not None
                else None
            ),
        )
        if (
            len(upstream_rows) + prior_diagnostic_attempts
            != config.deflated_sharpe_upstream_attempted_trials
        ):
            message = "embedded upstream attempt count differs from the DSR registry"
            raise RuntimeError(message)
        upstream_sharpes = tuple(
            float(row[sharpe_field]) for row in upstream_rows if row.get(sharpe_field) is not None
        )
        if len(upstream_sharpes) != config.deflated_sharpe_upstream_finite_count:
            message = "embedded upstream finite Sharpe count differs from the DSR registry"
            raise RuntimeError(message)
        if any(not math.isfinite(value) for value in upstream_sharpes):
            message = "embedded upstream Sharpe dispersion contains a non-finite value"
            raise RuntimeError(message)

        current_values: list[tuple[str, float]] = []
        report_sources: list[dict[str, str]] = []
        for candidate_id in config.deflated_sharpe_current_candidate_order:
            observations, sources = self._current_candidate_pooled_returns(
                candidate_id,
                config=config,
                all_specs=all_specs,
                trial_ids=trial_ids,
                manifest=manifest,
            )
            value = sharpe_ratio(
                [observation.daily_return for observation in observations],
                annualization_factor=config.annualization_factor,
            )
            # The shared metric deliberately returns 0.0 for a complete
            # zero-return/zero-variance ledger. This keeps verified zero
            # activity COMPLETE for DSR while the separate activity gate can
            # issue the registered retirement disposition.
            if not math.isfinite(value):
                message = f"current candidate Sharpe is non-finite: {candidate_id}"
                raise RuntimeError(message)
            current_values.append((candidate_id, value))
            report_sources.extend(sources)

        if len(current_values) != len(config.deflated_sharpe_current_candidate_order):
            message = "current DSR values do not exactly cover the registered arm order"
            raise RuntimeError(message)
        combined_finite_count = len(upstream_sharpes) + len(current_values)
        expected_combined_count = config.deflated_sharpe_upstream_finite_count + len(
            config.deflated_sharpe_current_candidate_order
        )
        if combined_finite_count != expected_combined_count:
            message = "combined DSR dispersion count does not reconcile"
            raise RuntimeError(message)

        source = self._mapping(ledger, "source")
        embedded_source = {
            "evidence_payload_sha256": manifest_sha256(ledger),
            "manifest_field": "upstream_selection_ledger",
            "source_sha256": self._nonempty_string(
                source.get("sha256"),
                field="upstream_selection_ledger.source.sha256",
            ),
        }
        evidence = TrialSharpeDispersion(
            upstream_attempted_trials=len(upstream_rows) + prior_diagnostic_attempts,
            upstream_finite_sharpes=upstream_sharpes,
            current_candidate_sharpes=tuple(current_values),
            source=(
                "embedded_manifest_upstream_selection_ledger_plus_registered_"
                + (
                    "count_only_prior_diagnostic_attempts_plus_"
                    if prior_diagnostic_attempts
                    else ""
                )
                + "current_equal_weight_pooled_expected_cost_oos_v1"
            ),
        )
        return evidence, self._normalized_source_attestation(
            report_sources=report_sources,
            embedded_sources=[embedded_source],
        )

    def _current_candidate_pooled_returns(
        self,
        candidate_id: str,
        *,
        config: RobustnessEvidenceConfig,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> tuple[tuple[TimestampedDailyReturn, ...], list[dict[str, str]]]:
        direct = self._direct_report_spec_index(all_specs, manifest=manifest)
        semantic_specs = {
            (
                str(spec["arm_id"]),
                str(spec["dataset_id"]),
                str(spec["fold_id"]),
                str(spec["cost_scenario"]),
            ): spec
            for spec in all_specs
            if spec.get("runner_kind") == "production_core_semantic_diagnostic"
        }
        semantic_rows = sum(
            spec.get("runner_kind") == "production_core_semantic_diagnostic" for spec in all_specs
        )
        if len(semantic_specs) != semantic_rows:
            message = "semantic report registry identities are not unique"
            raise RuntimeError(message)
        dataset_by_product = {
            str(dataset["requested_product"]): str(dataset["dataset_id"])
            for dataset in self._list_of_mappings(self._mapping(manifest, "data"), "datasets")
        }
        returns_by_fold_asset: dict[tuple[str, str], tuple[TimestampedDailyReturn, ...]] = {}
        report_sources: list[dict[str, str]] = []
        for fold_id, _year in config.fold_years:
            for asset in config.assets:
                direct_spec = direct.get(
                    (
                        candidate_id,
                        asset,
                        fold_id,
                        config.expected_cost_scenario,
                    )
                )
                if direct_spec is not None:
                    report = self._standard_report_from_spec(
                        direct_spec,
                        trial_ids=trial_ids,
                        manifest=manifest,
                    )
                    returns = self._curve_daily_returns(
                        report.adjusted_equity_curve,
                        initial_capital=report.initial_capital,
                        field=f"dsr.{candidate_id}.{fold_id}.{asset}.equity_curve",
                    )
                    report_sources.append(
                        {
                            "report_sha256": report.report_sha256,
                            "trial_id": report.trial_id,
                        }
                    )
                else:
                    dataset_id = dataset_by_product.get(asset)
                    if dataset_id is None:
                        message = f"current DSR asset has no frozen dataset: {asset}"
                        raise RuntimeError(message)
                    semantic_spec = semantic_specs.get(
                        (
                            candidate_id,
                            dataset_id,
                            fold_id,
                            config.expected_cost_scenario,
                        )
                    )
                    if semantic_spec is None:
                        message = (
                            "current DSR report evidence is absent: "
                            f"{candidate_id}/{asset}/{fold_id}"
                        )
                        raise RuntimeError(message)
                    returns, source = self._semantic_report_daily_returns(
                        semantic_spec,
                        expected_asset=asset,
                        trial_ids=trial_ids,
                        manifest=manifest,
                    )
                    report_sources.append(source)
                returns_by_fold_asset[(fold_id, asset)] = returns
        pooled = self._equal_weight_pooled_candidate_returns(
            returns_by_fold_asset,
            candidate_id=candidate_id,
            config=config,
        )
        return pooled, report_sources

    def _semantic_report_daily_returns(
        self,
        spec: Mapping[str, Any],
        *,
        expected_asset: str,
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> tuple[tuple[TimestampedDailyReturn, ...], dict[str, str]]:
        trial_id = trial_ids[int(spec["sequence"])]
        envelope = self._result_store.load_verified_report_evidence(trial_id)
        context = self._mapping(envelope, "meta_context")
        report = self._mapping(envelope, "report")
        expected_identity = (
            str(spec["arm_id"]),
            str(spec["fold_id"]),
            str(spec["cost_scenario"]),
        )
        observed_identity = (
            str(context.get("arm_id")),
            str(spec["fold_id"]),
            str(report.get("cost_scenario")),
        )
        if (
            envelope.get("report_evidence_schema_id") != "vynmatrix.backtest.report-evidence"
            or envelope.get("report_evidence_schema_version") != 1
            or envelope.get("trial_id") != trial_id
            or context.get("runner_kind") != "production_core_semantic_diagnostic"
            or context.get("protocol_id") != manifest["protocol_id"]
            or observed_identity != expected_identity
            or envelope.get("symbol") != expected_asset
            or report.get("symbol") != expected_asset
            or envelope.get("timeframe") != self._common_dataset_value(manifest, "timeframe")
            or report.get("timeframe") != self._common_dataset_value(manifest, "timeframe")
            or bool(report.get("account_ruined"))
        ):
            message = f"semantic report provenance differs: {trial_id}"
            raise RuntimeError(message)
        raw_blockers = report.get("selection_blockers", [])
        if (
            isinstance(raw_blockers, (str, bytes))
            or not isinstance(raw_blockers, Sequence)
            or any(not isinstance(blocker, str) or not blocker.strip() for blocker in raw_blockers)
            or set(raw_blockers) - {"unresolved_terminal_position"}
        ):
            message = f"semantic report has unsupported selection blockers: {trial_id}"
            raise RuntimeError(message)
        terminal_open = report.get("terminal_position") is not None
        if terminal_open != ("unresolved_terminal_position" in raw_blockers):
            message = f"semantic report terminal blocker differs: {trial_id}"
            raise RuntimeError(message)
        terminal_exit_cost, _policy_id = self._terminal_exit_adjustment(
            envelope,
            fold_id=str(spec["fold_id"]),
            manifest=manifest,
        )
        raw_curve = envelope.get("equity_curve")
        if (
            isinstance(raw_curve, (str, bytes))
            or not isinstance(raw_curve, Sequence)
            or not raw_curve
            or not isinstance(raw_curve[-1], Sequence)
            or len(raw_curve[-1]) != _EQUITY_POINT_LENGTH
            or not math.isclose(
                float(raw_curve[-1][1]),
                float(report["final_equity"]),
                rel_tol=1e-12,
                abs_tol=1e-9,
            )
        ):
            message = f"semantic report final equity differs from its curve: {trial_id}"
            raise RuntimeError(message)
        returns = self._curve_daily_returns(
            raw_curve,
            initial_capital=float(report["initial_capital"]),
            final_equity_adjustment=terminal_exit_cost,
            field=f"semantic.{spec['arm_id']}.{spec['fold_id']}.{expected_asset}",
        )
        report_sha256 = self._nonempty_string(
            envelope.get("report_sha256"),
            field="semantic.report_sha256",
        )
        if not _SHA256_RE.fullmatch(report_sha256):
            message = f"semantic report hash is malformed: {trial_id}"
            raise RuntimeError(message)
        return returns, {"report_sha256": report_sha256, "trial_id": trial_id}

    def _equal_weight_pooled_candidate_returns(
        self,
        returns_by_fold_asset: Mapping[tuple[str, str], Sequence[TimestampedDailyReturn]],
        *,
        candidate_id: str,
        config: RobustnessEvidenceConfig,
    ) -> tuple[TimestampedDailyReturn, ...]:
        expected_keys = {
            (fold_id, asset) for fold_id, _year in config.fold_years for asset in config.assets
        }
        if set(returns_by_fold_asset) != expected_keys:
            message = f"current DSR coverage is incomplete: {candidate_id}"
            raise RuntimeError(message)
        output: list[TimestampedDailyReturn] = []
        for fold_id, year in config.fold_years:
            rows = tuple(returns_by_fold_asset[(fold_id, asset)] for asset in config.assets)
            grids = tuple(tuple(observation.timestamp for observation in series) for series in rows)
            if not grids or any(grid != grids[0] for grid in grids[1:]):
                message = f"current DSR asset grids differ: {candidate_id}/{fold_id}"
                raise RuntimeError(message)
            if any((timestamp - timedelta(days=1)).year != year for timestamp in grids[0]):
                message = f"current DSR fold dates differ: {candidate_id}/{fold_id}"
                raise RuntimeError(message)
            for index, timestamp in enumerate(grids[0]):
                output.append(
                    TimestampedDailyReturn(
                        timestamp=timestamp,
                        daily_return=math.fsum(series[index].daily_return for series in rows)
                        / len(rows),
                    )
                )
        if not output or any(
            current.timestamp <= previous.timestamp
            for previous, current in itertools.pairwise(output)
        ):
            message = f"current DSR pooled grid is empty or non-monotonic: {candidate_id}"
            raise RuntimeError(message)
        return tuple(output)

    def _curve_daily_returns(
        self,
        raw_curve: Any,
        *,
        initial_capital: float,
        field: str,
        final_equity_adjustment: float = 0.0,
    ) -> tuple[TimestampedDailyReturn, ...]:
        if (
            isinstance(raw_curve, (str, bytes))
            or not isinstance(raw_curve, Sequence)
            or not raw_curve
        ):
            message = f"{field} must be a non-empty JSON array"
            raise TypeError(message)
        if not math.isfinite(initial_capital) or initial_capital <= 0.0:
            message = f"{field} initial capital must be finite and positive"
            raise ValueError(message)
        if not math.isfinite(final_equity_adjustment) or final_equity_adjustment < 0.0:
            message = f"{field} final adjustment must be finite and non-negative"
            raise ValueError(message)
        previous = initial_capital
        output: list[TimestampedDailyReturn] = []
        for index, raw_point in enumerate(raw_curve):
            if isinstance(raw_point, (str, bytes)) or not isinstance(raw_point, Sequence):
                message = f"{field}[{index}] must contain timestamp and equity"
                raise TypeError(message)
            if len(raw_point) != _EQUITY_POINT_LENGTH:
                message = f"{field}[{index}] must contain exactly two values"
                raise ValueError(message)
            raw_timestamp = raw_point[0]
            if isinstance(raw_timestamp, datetime):
                if raw_timestamp.tzinfo is None or raw_timestamp.utcoffset() is None:
                    message = f"{field}[{index}].timestamp must be timezone-aware"
                    raise ValueError(message)
                timestamp = raw_timestamp.astimezone(UTC)
            else:
                timestamp = parse_utc_datetime(
                    raw_timestamp,
                    field=f"{field}[{index}].timestamp",
                )
            equity = float(raw_point[1])
            if index == len(raw_curve) - 1:
                equity -= final_equity_adjustment
            if not math.isfinite(equity) or equity <= 0.0:
                message = f"{field}[{index}] equity is non-finite or ruined"
                raise ValueError(message)
            daily_return = equity / previous - 1.0
            output.append(TimestampedDailyReturn(timestamp, daily_return))
            previous = equity
        if any(
            current.timestamp <= previous_row.timestamp
            for previous_row, current in itertools.pairwise(output)
        ):
            message = f"{field} timestamps must be strictly increasing"
            raise ValueError(message)
        return tuple(output)

    @staticmethod
    def _compounded_return_from_daily(
        observations: Sequence[TimestampedDailyReturn],
    ) -> float:
        if not observations:
            message = "daily return evidence must not be empty"
            raise ValueError(message)
        return (
            float(math.prod(1.0 + observation.daily_return for observation in observations) - 1.0)
            * 100.0
        )

    @classmethod
    def _normalized_source_attestation(
        cls,
        *,
        report_sources: Sequence[Mapping[str, Any]] = (),
        derived_sources: Sequence[Mapping[str, Any]] = (),
        dataset_sources: Sequence[Mapping[str, Any]] = (),
        embedded_sources: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Canonicalize a complete source ledger and bind it to one digest."""

        def normalized_rows(
            rows: Sequence[Mapping[str, Any]],
            *,
            required_fields: tuple[str, ...],
            identity_fields: tuple[str, ...],
            sha_fields: tuple[str, ...],
            field: str,
        ) -> list[dict[str, str]]:
            indexed: dict[tuple[str, ...], dict[str, str]] = {}
            for index, raw in enumerate(rows):
                if not isinstance(raw, Mapping) or set(raw) != set(required_fields):
                    message = f"{field}[{index}] fields differ from the source schema"
                    raise ValueError(message)
                row = {name: str(raw[name]) for name in required_fields}
                if any(not row[name].strip() for name in required_fields):
                    message = f"{field}[{index}] contains a blank identity"
                    raise ValueError(message)
                for name in sha_fields:
                    if not _SHA256_RE.fullmatch(row[name]):
                        message = f"{field}[{index}].{name} is not a SHA-256 digest"
                        raise ValueError(message)
                identity = tuple(row[name] for name in identity_fields)
                existing = indexed.get(identity)
                if existing is not None and existing != row:
                    message = f"{field} contains conflicting source identities: {identity}"
                    raise RuntimeError(message)
                indexed[identity] = row
            return [indexed[key] for key in sorted(indexed)]

        body = {
            "schema_id": "vynmatrix.backtest.source-attestation",
            "schema_version": 1,
            "report_evidence": normalized_rows(
                report_sources,
                required_fields=("trial_id", "report_sha256"),
                identity_fields=("trial_id",),
                sha_fields=("report_sha256",),
                field="report_evidence",
            ),
            "derived_evidence": normalized_rows(
                derived_sources,
                required_fields=(
                    "trial_id",
                    "evidence_kind",
                    "evidence_payload_sha256",
                ),
                identity_fields=("trial_id", "evidence_kind"),
                sha_fields=("evidence_payload_sha256",),
                field="derived_evidence",
            ),
            "dataset_evidence": normalized_rows(
                dataset_sources,
                required_fields=("dataset_id", "ohlcv_sha256"),
                identity_fields=("dataset_id",),
                sha_fields=("ohlcv_sha256",),
                field="dataset_evidence",
            ),
            "embedded_evidence": normalized_rows(
                embedded_sources,
                required_fields=(
                    "manifest_field",
                    "evidence_payload_sha256",
                    "source_sha256",
                ),
                identity_fields=("manifest_field",),
                sha_fields=("evidence_payload_sha256", "source_sha256"),
                field="embedded_evidence",
            ),
        }
        if not any(
            body[field]
            for field in (
                "report_evidence",
                "derived_evidence",
                "dataset_evidence",
                "embedded_evidence",
            )
        ):
            message = "source attestation must bind at least one immutable source"
            raise ValueError(message)
        return {
            **body,
            "source_attestation_sha256": manifest_sha256(body),
        }

    @classmethod
    def _merge_source_attestations(
        cls,
        *attestations: Mapping[str, Any],
    ) -> dict[str, Any]:
        reports: list[Mapping[str, Any]] = []
        derived: list[Mapping[str, Any]] = []
        datasets: list[Mapping[str, Any]] = []
        embedded: list[Mapping[str, Any]] = []
        for index, attestation in enumerate(attestations):
            if not isinstance(attestation, Mapping):
                message = f"source_attestations[{index}] must be a JSON object"
                raise TypeError(message)
            rebuilt = cls._normalized_source_attestation(
                report_sources=cast(
                    Sequence[Mapping[str, Any]],
                    attestation.get("report_evidence", ()),
                ),
                derived_sources=cast(
                    Sequence[Mapping[str, Any]],
                    attestation.get("derived_evidence", ()),
                ),
                dataset_sources=cast(
                    Sequence[Mapping[str, Any]],
                    attestation.get("dataset_evidence", ()),
                ),
                embedded_sources=cast(
                    Sequence[Mapping[str, Any]],
                    attestation.get("embedded_evidence", ()),
                ),
            )
            if dict(attestation) != rebuilt:
                message = f"source_attestations[{index}] content hash differs"
                raise RuntimeError(message)
            reports.extend(rebuilt["report_evidence"])
            derived.extend(rebuilt["derived_evidence"])
            datasets.extend(rebuilt["dataset_evidence"])
            embedded.extend(rebuilt["embedded_evidence"])
        return cls._normalized_source_attestation(
            report_sources=reports,
            derived_sources=derived,
            dataset_sources=datasets,
            embedded_sources=embedded,
        )
