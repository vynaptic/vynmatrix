"""Production-core and primary-group trial execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from dev_cli.validation.backtest.engine import BacktestConfig, BacktestEngine
from dev_cli.validation.backtest.execution import (
    ExecutionCostModel,
    FillPolicy,
    normalize_execution_bar,
)
from dev_cli.validation.backtest.folds import TimestampFoldWindow
from dev_cli.validation.backtest.walk_forward import (
    TerminalExitCostPolicy,
    build_terminal_liquidation_cost_policy,
    pool_fold_oos_returns,
    run_joint_asset_timestamp_anchored_walk_forward,
)
from dev_cli.validation.benchmarks import (
    BenchmarkDefinition,
    BenchmarkKind,
    VolatilityTargetConfig,
    calibrate_fixed_volatility_exposure,
    run_consolidated_benchmark,
)
from dev_cli.validation.campaign_composition import CampaignMixinContract
from dev_cli.validation.campaign_contracts import (
    _BENCHMARK_KIND_BY_PROTOCOL_NAME,
    _DERIVED_SYMBOL_MAX_LENGTH,
    _TERMINAL_STATUSES,
)
from dev_cli.validation.evidence import parse_utc_datetime
from dev_cli.validation.persistence.backtest_result_store import (
    DerivedBacktestEvidence,
    DerivedEvidenceMetrics,
)
from dev_cli.validation.persistence.backtest_trial_store import (
    BacktestTrialStatus,
)
from lib_data.dataset import timeframe_interval


class CampaignExecutionMixin(CampaignMixinContract):
    def _execute_trial(
        self,
        spec: Mapping[str, Any],
        *,
        manifest: Mapping[str, Any],
        runtime_config: Mapping[str, Any],
        core_class: type[Any],
    ) -> tuple[Any, dict[str, Any]]:
        runner_kind = str(spec.get("runner_kind"))
        if runner_kind in {
            "production_core_direct",
            "production_core_semantic_diagnostic",
        }:
            return self._execute_production_core_trial(
                spec,
                manifest=manifest,
                runtime_config=runtime_config,
                core_class=core_class,
            )
        if runner_kind == "executable_benchmark_component":
            return self._execute_benchmark_component(spec, manifest=manifest)
        message = f"phase-1 individual executor does not support runner_kind={runner_kind}"
        raise RuntimeError(message)

    def _execute_production_core_trial(
        self,
        spec: Mapping[str, Any],
        *,
        manifest: Mapping[str, Any],
        runtime_config: Mapping[str, Any],
        core_class: type[Any],
    ) -> tuple[Any, dict[str, Any]]:
        dataset, bars, audit = self._load_trial_dataset(spec, manifest=manifest)
        parameters = self._strategy_parameters(spec, runtime_config=runtime_config)
        strategy = self._build_strategy(
            parameters,
            manifest=manifest,
            core_class=core_class,
        )
        config = self._backtest_config(
            spec,
            dataset=dataset,
            manifest=manifest,
            fill_policy=self._fill_policy(spec, manifest=manifest),
        )
        report = BacktestEngine().run_consolidated(strategy, bars, config)
        return report, audit

    def _execute_benchmark_component(
        self,
        spec: Mapping[str, Any],
        *,
        manifest: Mapping[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        dataset, bars, dataset_audit = self._load_trial_dataset(spec, manifest=manifest)
        parameters = self._mapping(spec, "parameters")
        definition = self._benchmark_definition(
            parameters,
            bars=bars,
            annualization_factor=float(
                self._mapping(manifest, "inference")["annualization_factor"]
            ),
        )
        config = self._backtest_config(
            spec,
            dataset=dataset,
            manifest=manifest,
            fill_policy=self._fill_policy(spec, manifest=manifest),
        )
        benchmark_run = run_consolidated_benchmark(definition, bars, config)
        return benchmark_run.report, {
            "dataset": dataset_audit,
            "benchmark": benchmark_run.registration_metadata(),
        }

    def _load_trial_dataset(
        self,
        spec: Mapping[str, Any],
        *,
        manifest: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], list[Any], dict[str, Any]]:
        datasets = {
            str(item["dataset_id"]): item
            for item in self._list_of_mappings(self._mapping(manifest, "data"), "datasets")
        }
        dataset_id = str(spec["dataset_id"])
        dataset = datasets.get(dataset_id)
        if dataset is None:
            message = f"trial does not reference a component dataset: {dataset_id}"
            raise ValueError(message)
        product = str(dataset["requested_product"])
        start = parse_utc_datetime(dataset["start"], field="dataset.start")
        end = parse_utc_datetime(dataset["end_exclusive"], field="dataset.end_exclusive")
        bars, audit = self._historical_prices.load_validated_bars(
            int(dataset["instrument_id"]),
            start=start,
            end=end,
            timeframe=str(dataset["timeframe"]),
            source=str(dataset["source"]),
            symbol=product,
            minimum_coverage=float(dataset["minimum_coverage"]),
            metadata={
                "asset_class": self._dataset_asset_class(dataset, manifest=manifest),
                "canonical_product": dataset["canonical_product"],
                "requested_product": product,
            },
        )
        if audit.ohlcv_sha256 != self._mapping(dataset, "audit")["ohlcv_sha256"]:
            message = f"Dataset changed after manifest freeze: {dataset['dataset_id']}"
            raise ValueError(message)
        return dataset, bars, audit.to_dict()

    def _execution_cost_model(
        self,
        manifest: Mapping[str, Any],
        scenario: str,
        product: str,
        *,
        latency_bars: int | None = None,
    ) -> ExecutionCostModel:
        cost = self._product_cost(manifest, scenario, product)
        return ExecutionCostModel(
            scenario_name=scenario,
            version="coinbase-current-backward-v1",
            commission_bps=float(cost["commission_bps"]),
            half_spread_bps=float(cost["half_spread_bps"]),
            slippage_bps=float(cost["slippage_bps"]),
            impact_bps=float(cost["impact_bps"]),
            latency_bars=(int(cost["fill_delay_bars"]) if latency_bars is None else latency_bars),
            historical_basis="backward-applied-scenario",
            rejection_assumption="none",
            partial_fill_assumption="full_fill",
        )

    def _fill_policy(
        self,
        spec: Mapping[str, Any],
        *,
        manifest: Mapping[str, Any],
    ) -> FillPolicy:
        parameters = self._mapping(spec, "parameters")
        override = parameters.get("execution_policy_override")
        if override is not None:
            if override != "next_open_market_only_ignore_emitted_protective_stop":
                message = f"unsupported semantic execution-policy override: {override}"
                raise ValueError(message)
            return FillPolicy(
                policy_id=str(override),
                protective_orders=False,
                gap_through_at_open=False,
                ambiguity_policy="ignore",
                invalid_bracket_policy="ignore",
                timestamp_contract="explicit_interval",
            )
        policy = self._mapping(manifest, "execution_policy")
        return FillPolicy(
            policy_id=str(policy["policy_version"]),
            protective_orders=True,
            gap_through_at_open=True,
            ambiguity_policy="stop_first",
            invalid_bracket_policy="reject_entry",
            timestamp_contract="explicit_interval",
        )

    @staticmethod
    def _runtime_strategy_parameters(runtime_config: Mapping[str, Any]) -> dict[str, Any]:
        params = dict(runtime_config.get("parameters") or {})
        strategy_version = runtime_config.get("strategy_version")
        if not isinstance(strategy_version, str) or not strategy_version.strip():
            message = "runtime config requires a top-level strategy_version"
            raise ValueError(message)
        params["strategy_version"] = strategy_version.strip()
        return params

    @classmethod
    def _strategy_parameters(
        cls,
        spec: Mapping[str, Any],
        *,
        runtime_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        params = cls._runtime_strategy_parameters(runtime_config)
        raw_parameters = spec.get("parameters")
        if not isinstance(raw_parameters, Mapping):
            message = "trial parameters must be a JSON object"
            raise TypeError(message)
        params.update(dict(raw_parameters))
        params["signal_source"] = "backtest"
        return params

    def _build_strategy(
        self,
        parameters: Mapping[str, Any],
        *,
        manifest: Mapping[str, Any],
        core_class: type[Any],
    ) -> Any:
        strategy_info = self._mapping(manifest, "strategy")
        return core_class(
            strategy_id=str(strategy_info["strategy_id"]),
            strategy_type="indicator",
            config=dict(parameters),
        )

    def _backtest_config(
        self,
        spec: Mapping[str, Any],
        *,
        dataset: Mapping[str, Any],
        manifest: Mapping[str, Any],
        fill_policy: FillPolicy,
    ) -> BacktestConfig:
        product = str(dataset["requested_product"])
        timeframe = self._nonempty_string(dataset.get("timeframe"), field="dataset.timeframe")
        interval_seconds = timeframe_interval(timeframe).total_seconds()
        if interval_seconds % 60:
            message = f"dataset timeframe must resolve to whole minutes: {timeframe}"
            raise ValueError(message)
        return BacktestConfig(
            symbol=product,
            consolidation_minutes=int(interval_seconds // 60),
            initial_capital=float(
                self._mapping(manifest, "portfolio_assumptions")["initial_capital_usd"]
            ),
            size_pct=float(
                self._mapping(manifest, "portfolio_assumptions")["primary_fixed_exposure_pct"]
            ),
            annualization_factor=float(
                self._mapping(manifest, "inference")["annualization_factor"]
            ),
            execution_costs=self._execution_cost_model(
                manifest,
                str(spec["cost_scenario"]),
                product,
            ),
            fill_policy=fill_policy,
            min_bar_coverage=float(dataset["minimum_coverage"]),
            evaluation_start=parse_utc_datetime(
                spec["evaluation_start"], field="trial.evaluation_start"
            ),
            evaluation_end=parse_utc_datetime(
                spec["evaluation_end_exclusive"], field="trial.evaluation_end_exclusive"
            ),
            price_source=str(dataset["source"]),
            asset_class=self._dataset_asset_class(dataset, manifest=manifest),
            requested_product=product,
            canonical_product=str(dataset["canonical_product"]),
            require_flat_model_boundary=True,
        )

    def _benchmark_definition(
        self,
        parameters: Mapping[str, Any],
        *,
        bars: Sequence[Any],
        annualization_factor: float,
    ) -> BenchmarkDefinition:
        name = self._nonempty_string(
            parameters.get("protocol_benchmark_name"),
            field="protocol_benchmark_name",
        )
        kind = BenchmarkKind(
            self._nonempty_string(parameters.get("benchmark_kind"), field="benchmark_kind")
        )
        if kind.value != _BENCHMARK_KIND_BY_PROTOCOL_NAME.get(name):
            message = "registered benchmark name and kind do not match"
            raise ValueError(message)
        definition = self._mapping(parameters, "definition")
        if kind is BenchmarkKind.CASH:
            return BenchmarkDefinition.cash()
        if kind is BenchmarkKind.BUY_AND_HOLD:
            return BenchmarkDefinition.buy_and_hold(exposure=float(definition["exposure_pct"]))
        if kind is BenchmarkKind.SMA_LONG_CASH:
            return BenchmarkDefinition.sma_long_cash(
                period=int(definition["warmup_bars"]),
                exposure=float(definition["exposure_pct"]),
            )
        if kind is BenchmarkKind.MOMENTUM_LONG_CASH:
            return BenchmarkDefinition.momentum_long_cash(
                lookback_bars=int(definition["warmup_bars"]) - 1,
                exposure=float(definition["exposure_pct"]),
            )
        if kind is BenchmarkKind.VOL_TARGET_BUY_AND_HOLD:
            calibration_end = parse_utc_datetime(
                parameters.get("calibration_end_exclusive"),
                field="calibration_end_exclusive",
            )
            normalized = [normalize_execution_bar(bar) for bar in bars]
            eligible_training_bars = [bar for bar in normalized if bar.timestamp < calibration_end]
            calibration = calibrate_fixed_volatility_exposure(
                eligible_training_bars,
                VolatilityTargetConfig(
                    maximum_lookback_bars=int(definition["calibration_lookback_calendar_days"]),
                    minimum_observations=int(definition["minimum_calibration_returns"]),
                    target_annualized_volatility=float(definition["target_annualized_volatility"]),
                    exposure_floor=float(definition["minimum_exposure_pct"]),
                    exposure_cap=float(definition["maximum_exposure_pct"]),
                ),
                annualization_factor=annualization_factor,
            )
            if calibration.training_end >= calibration_end:
                message = "volatility calibration used evaluation data"
                raise RuntimeError(message)
            return BenchmarkDefinition.volatility_targeted_buy_and_hold(calibration)
        message = f"unsupported benchmark kind: {kind.value}"
        raise ValueError(message)

    def _execute_primary_group(
        self,
        primary_specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
        runtime_config: Mapping[str, Any],
        core_class: type[Any],
        report_cache: dict[int, Any],
    ) -> int:
        """Run all anchored folds once and checkpoint each registered result."""

        if not primary_specs:
            message = "primary selector registry is empty"
            raise RuntimeError(message)
        primary_ids = [trial_ids[int(spec["sequence"])] for spec in primary_specs]
        statuses = {trial_id: self._trial_status(trial_id) for trial_id in primary_ids}
        active_ids = [
            trial_id for trial_id, status in statuses.items() if status not in _TERMINAL_STATUSES
        ]
        if not active_ids:
            return 0
        non_resumable = {
            trial_id: status
            for trial_id, status in statuses.items()
            if status
            in {
                BacktestTrialStatus.FAILED.value,
                BacktestTrialStatus.INTERRUPTED.value,
                BacktestTrialStatus.ABANDONED.value,
            }
        }
        if non_resumable:
            message = (
                "primary selector group contains failed, interrupted, or abandoned "
                "evidence and cannot be retried"
            )
            for trial_id in active_ids:
                self._trial_store.mark_failed(
                    trial_id,
                    error_class="PartialPrimaryEvidenceError",
                    error_context=message,
                )
            return len(active_ids)

        failed_before = self._failed_trial_count(trial_ids.values())
        completed_before = {
            trial_id
            for trial_id in primary_ids
            if self._trial_status(trial_id) == BacktestTrialStatus.COMPLETED.value
        }
        try:
            dependency_reports = self._prepare_primary_dependencies(
                all_specs,
                trial_ids=trial_ids,
                manifest=manifest,
                runtime_config=runtime_config,
                core_class=core_class,
                report_cache=report_cache,
            )
            for trial_id in active_ids:
                self._trial_store.mark_running(trial_id)
            result, dataset_audits = self._run_primary_selector(
                primary_specs,
                dependency_reports=dependency_reports,
                manifest=manifest,
                runtime_config=runtime_config,
                core_class=core_class,
            )
            self._persist_primary_result(
                result,
                primary_specs=primary_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
                dataset_audits=dataset_audits,
            )
        except (KeyboardInterrupt, SystemExit):
            completed_after = {
                trial_id
                for trial_id in primary_ids
                if self._trial_status(trial_id) == BacktestTrialStatus.COMPLETED.value
            }
            if completed_after == completed_before:
                for trial_id in active_ids:
                    if self._trial_status(trial_id) == BacktestTrialStatus.RUNNING.value:
                        self._trial_store.mark_interrupted(
                            trial_id,
                            error_context="joint selector interrupted before any checkpoint",
                        )
            self._refresh_experiment_summary(experiment_id)
            raise
        except Exception as exc:
            completed_after = {
                trial_id
                for trial_id in primary_ids
                if self._trial_status(trial_id) == BacktestTrialStatus.COMPLETED.value
            }
            if completed_after != completed_before:
                # Completed evidence is immutable. Leave unfinished rows RUNNING
                # so a resume recomputes once, content-verifies every completed
                # checkpoint, then persists only the remainder.
                raise
            context = self._safe_error_context(exc)
            for trial_id in primary_ids:
                if self._trial_status(trial_id) in {
                    BacktestTrialStatus.REGISTERED.value,
                    BacktestTrialStatus.RUNNING.value,
                }:
                    self._trial_store.mark_failed(
                        trial_id,
                        error_class=type(exc).__name__,
                        error_context=context,
                    )
        failed_after = self._failed_trial_count(trial_ids.values())
        return failed_after - failed_before

    def _prepare_primary_dependencies(
        self,
        all_specs: Sequence[Mapping[str, Any]],
        *,
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
        runtime_config: Mapping[str, Any],
        core_class: type[Any],
        report_cache: dict[int, Any],
    ) -> list[tuple[Mapping[str, Any], Any]]:
        """Materialize exact expected-cost full-panel reports for event purging."""

        dependencies = self._primary_dependency_specs(all_specs)
        reports: list[tuple[Mapping[str, Any], Any]] = []
        for spec in dependencies:
            sequence = int(spec["sequence"])
            report = report_cache.get(sequence)
            if report is None:
                trial_id = trial_ids[sequence]
                status = self._trial_status(trial_id)
                if status in {
                    BacktestTrialStatus.FAILED.value,
                    BacktestTrialStatus.INTERRUPTED.value,
                    BacktestTrialStatus.ABANDONED.value,
                }:
                    message = (
                        "primary selector dependency is terminal without reusable "
                        f"evidence: sequence={sequence}, status={status}"
                    )
                    raise RuntimeError(message)
                should_persist = status != BacktestTrialStatus.COMPLETED.value
                try:
                    if should_persist:
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
                except Exception as exc:
                    if self._trial_status(trial_id) in {
                        BacktestTrialStatus.REGISTERED.value,
                        BacktestTrialStatus.RUNNING.value,
                    }:
                        self._trial_store.mark_failed(
                            trial_id,
                            error_class=type(exc).__name__,
                            error_context=self._safe_error_context(exc),
                        )
                    raise
                report_cache[sequence] = report
            reports.append((spec, report))
        return reports

    @staticmethod
    def _primary_dependency_specs(
        all_specs: Sequence[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        primary = next(
            (
                spec
                for spec in all_specs
                if spec.get("runner_kind") == "anchored_joint_asset_selector"
            ),
            None,
        )
        if primary is None:
            message = "primary selector registry has no fold component"
            raise RuntimeError(message)
        parameters = primary.get("parameters")
        if not isinstance(parameters, Mapping):
            message = "primary selector parameters must be a JSON object"
            raise TypeError(message)
        candidate_ids = parameters.get("candidate_arm_ids")
        components = parameters.get("component_datasets")
        if (
            isinstance(candidate_ids, (str, bytes))
            or not isinstance(candidate_ids, Sequence)
            or isinstance(components, (str, bytes))
            or not isinstance(components, Sequence)
        ):
            message = "primary selector dependencies are malformed"
            raise TypeError(message)
        dataset_ids = {
            str(component["dataset_id"])
            for component in components
            if isinstance(component, Mapping)
        }
        candidates = [str(arm_id) for arm_id in candidate_ids]
        by_identity = {
            (str(spec["arm_id"]), str(spec["dataset_id"])): spec
            for spec in all_specs
            if spec.get("runner_kind") == "production_core_direct"
            and spec.get("fold_id") == "full-panel"
            and spec.get("cost_scenario") == "expected"
            and str(spec.get("arm_id")) in candidates
            and str(spec.get("dataset_id")) in dataset_ids
        }
        expected = len(candidates) * len(dataset_ids)
        if len(by_identity) != expected:
            message = "primary selector direct dependencies do not exactly cover the registry"
            raise RuntimeError(message)
        return [
            by_identity[(arm_id, dataset_id)]
            for arm_id in candidates
            for dataset_id in sorted(dataset_ids)
        ]

    def _run_primary_selector(
        self,
        primary_specs: Sequence[Mapping[str, Any]],
        *,
        dependency_reports: Sequence[tuple[Mapping[str, Any], Any]],
        manifest: Mapping[str, Any],
        runtime_config: Mapping[str, Any],
        core_class: type[Any],
    ) -> tuple[Any, dict[str, dict[str, Any]]]:
        fold_specs = sorted(
            (
                spec
                for spec in primary_specs
                if spec.get("runner_kind") == "anchored_joint_asset_selector"
            ),
            key=lambda spec: parse_utc_datetime(
                spec["evaluation_start"], field="primary.evaluation_start"
            ),
        )
        pooled_specs = [
            spec
            for spec in primary_specs
            if spec.get("runner_kind") == "pooled_oos_primary_aggregate"
        ]
        if not fold_specs or len(pooled_specs) != 1:
            message = "primary selector must contain folds and one pooled aggregate"
            raise RuntimeError(message)
        selector_parameters = self._mapping(fold_specs[0], "parameters")

        bars_by_asset: dict[str, list[Any]] = {}
        configs_by_asset: dict[str, BacktestConfig] = {}
        dataset_audits: dict[str, dict[str, Any]] = {}
        dependency_by_dataset = {
            str(spec["dataset_id"]): spec for spec, _report in dependency_reports
        }
        for dataset_id in sorted(dependency_by_dataset):
            dependency_spec = dependency_by_dataset[dataset_id]
            dataset, bars, audit = self._load_trial_dataset(
                dependency_spec,
                manifest=manifest,
            )
            product = str(dataset["requested_product"])
            bars_by_asset[product] = bars
            configs_by_asset[product] = self._backtest_config(
                dependency_spec,
                dataset=dataset,
                manifest=manifest,
                fill_policy=self._fill_policy(dependency_spec, manifest=manifest),
            )
            dataset_audits[product] = audit

        candidates = [str(item) for item in selector_parameters["candidate_arm_ids"]]
        candidate_params: list[dict[str, Any]] = []
        for arm_id in candidates:
            variants = [
                dict(self._mapping(spec, "parameters"))
                for spec, _report in dependency_reports
                if spec["arm_id"] == arm_id
            ]
            if not variants or any(variant != variants[0] for variant in variants[1:]):
                message = f"candidate parameters differ across assets: {arm_id}"
                raise RuntimeError(message)
            candidate_params.append(variants[0])

        runtime_parameters = self._runtime_strategy_parameters(runtime_config)

        def strategy_factory(parameters: Mapping[str, Any]) -> Any:
            return self._build_strategy(
                {**runtime_parameters, **dict(parameters), "signal_source": "backtest"},
                manifest=manifest,
                core_class=core_class,
            )

        bootstrap_bars = max(
            int(strategy_factory(parameters).warmup_bars_needed) for parameters in candidate_params
        )
        training_start = parse_utc_datetime(
            selector_parameters["training_evaluation_start"],
            field="primary.training_evaluation_start",
        )
        windows = [
            TimestampFoldWindow(
                train_end_exclusive=parse_utc_datetime(
                    spec["training_end_exclusive"],
                    field="primary.training_end_exclusive",
                ),
                test_start=parse_utc_datetime(
                    spec["evaluation_start"], field="primary.evaluation_start"
                ),
                test_end_exclusive=parse_utc_datetime(
                    spec["evaluation_end_exclusive"],
                    field="primary.evaluation_end_exclusive",
                ),
            )
            for spec in fold_specs
        ]
        terminal_policy = self._primary_terminal_policy(
            manifest,
            products=tuple(sorted(bars_by_asset)),
            scenario=str(selector_parameters["training_terminal_cost_scenario"]),
        )
        baseline_arm_id = self._historical_disposition_policy(manifest).baseline_arm_id
        result = run_joint_asset_timestamp_anchored_walk_forward(
            strategy_factory,
            candidate_params,
            bars_by_asset,
            configs_by_asset,
            windows=windows,
            training_evaluation_start=training_start,
            bootstrap_bars=bootstrap_bars,
            training_terminal_exit_cost_policy=terminal_policy,
            oos_terminal_exit_cost_policy=terminal_policy,
            selected_arm_tie_tolerance=float(selector_parameters["selected_arm_tie_tolerance"]),
            baseline_registry_index=candidates.index(baseline_arm_id),
            pre_oos_purge=timedelta(days=int(selector_parameters["purge_calendar_days"])),
            embargo_bars=int(selector_parameters["embargo_bars"]),
            # Strategy-return selection has no fixed forward label horizon.  A
            # flat boundary plus the registered terminal mark/cost policy cuts
            # the training information set at the contiguous selection
            # boundary.  Removing individual price bars whose *natural* trade
            # exit later overlaps OOS would compress the state path and no
            # longer replay the production strategy.
            event_intervals=None,
        )
        return result, dataset_audits

    def _primary_terminal_policy(
        self,
        manifest: Mapping[str, Any],
        *,
        products: Sequence[str],
        scenario: str,
    ) -> TerminalExitCostPolicy:
        policies = {
            product: build_terminal_liquidation_cost_policy(
                self._execution_cost_model(
                    manifest,
                    scenario,
                    product,
                    latency_bars=0,
                ),
                policy_id=f"terminal-liquidation:{scenario}:{product}:final-mark-v1",
            )
            for product in products
        }

        def estimator(report: Any) -> float:
            policy = policies.get(str(report.config.symbol))
            if policy is None:
                message = f"no terminal-liquidation policy for {report.config.symbol}"
                raise ValueError(message)
            return float(policy.estimator(report))

        return TerminalExitCostPolicy(
            policy_id=f"terminal-liquidation:{scenario}:per-product-final-mark-v1",
            estimator=estimator,
        )

    def _persist_primary_result(
        self,
        result: Any,
        *,
        primary_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
        dataset_audits: Mapping[str, Mapping[str, Any]],
    ) -> None:
        fold_specs = {
            str(spec["fold_id"]): spec
            for spec in primary_specs
            if spec.get("runner_kind") == "anchored_joint_asset_selector"
        }
        if len(result.windows) != len(fold_specs):
            message = "joint selector result does not match registered fold count"
            raise RuntimeError(message)
        strategy = self._mapping(manifest, "strategy")
        symbol = "+".join(result.asset_order)
        if len(symbol) > _DERIVED_SYMBOL_MAX_LENGTH:
            message = "primary evidence symbol exceeds database limit"
            raise ValueError(message)

        evidence_records: list[tuple[Mapping[str, Any], DerivedBacktestEvidence]] = []
        for window in result.windows:
            fold_id = f"oos-{window.fold.test_start_ts.year}"
            spec = fold_specs.get(fold_id)
            if spec is None:
                message = f"joint selector returned unregistered fold {fold_id}"
                raise RuntimeError(message)
            expected_start = parse_utc_datetime(
                spec["evaluation_start"], field="primary.evaluation_start"
            )
            expected_end = parse_utc_datetime(
                spec["evaluation_end_exclusive"],
                field="primary.evaluation_end_exclusive",
            )
            if (
                window.fold.test_start_ts != expected_start
                or window.fold.test_end_exclusive_ts != expected_end
            ):
                message = f"joint selector boundaries differ from registered fold {fold_id}"
                raise RuntimeError(message)
            fold_summary = pool_fold_oos_returns(
                [window.equal_weight_oos_series],
                annualization_factor=float(
                    self._mapping(manifest, "inference")["annualization_factor"]
                ),
            )
            curve = tuple(window.equal_weight_oos_series.equity_curve)
            total_trades = sum(
                item.report.metrics.total_trades for item in window.oos_asset_reports
            )
            total_signals = sum(item.report.signals_emitted for item in window.oos_asset_reports)
            metrics = fold_summary.metrics
            evidence_records.append(
                (
                    spec,
                    DerivedBacktestEvidence(
                        strategy_id=str(strategy["strategy_id"]),
                        strategy_semver=str(strategy["strategy_version"]),
                        experiment_id=experiment_id,
                        evidence_kind="joint_asset_anchored_fold",
                        symbol=symbol,
                        asset_class=str(self._mapping(manifest, "habitat")["asset_class_db"]),
                        timeframe=self._common_dataset_value(manifest, "timeframe"),
                        start_date=curve[0][0].date(),
                        end_date=curve[-1][0].date(),
                        payload={
                            "dataset_audits": {
                                key: dict(value) for key, value in sorted(dataset_audits.items())
                            },
                            "evidence_role": spec["evidence_role"],
                            "fold": window.to_dict(),
                            "historical_selection_contaminated": True,
                            "protocol_id": manifest["protocol_id"],
                            "registered_fold_id": fold_id,
                            "runner_kind": spec["runner_kind"],
                        },
                        metrics=DerivedEvidenceMetrics(
                            initial_capital=window.equal_weight_oos_series.initial_capital,
                            total_return_pct=metrics.compounded_return_pct,
                            sharpe_ratio=metrics.sharpe_ratio,
                            max_drawdown_pct=metrics.max_drawdown_pct,
                            total_trades=total_trades,
                            total_signals=total_signals,
                            final_equity=curve[-1][1],
                            peak_equity=max(equity for _timestamp, equity in curve),
                        ),
                        equity_curve=curve,
                    ),
                )
            )

        pooled_specs = [
            spec
            for spec in primary_specs
            if spec.get("runner_kind") == "pooled_oos_primary_aggregate"
        ]
        if len(pooled_specs) != 1:
            message = "primary selector registry lacks one pooled result"
            raise RuntimeError(message)
        pooled_spec = pooled_specs[0]
        pooled_curve: list[tuple[datetime, float]] = []
        equity = 1.0
        for observation in result.pooled_oos.observations:
            equity *= 1.0 + observation.daily_return
            pooled_curve.append((observation.timestamp, equity))
        if not pooled_curve:
            message = "primary pooled OOS result contains no observations"
            raise RuntimeError(message)
        pooled_metrics = result.pooled_oos.metrics
        evidence_records.append(
            (
                pooled_spec,
                DerivedBacktestEvidence(
                    strategy_id=str(strategy["strategy_id"]),
                    strategy_semver=str(strategy["strategy_version"]),
                    experiment_id=experiment_id,
                    evidence_kind="joint_asset_pooled_oos",
                    symbol=symbol,
                    asset_class=str(self._mapping(manifest, "habitat")["asset_class_db"]),
                    timeframe=self._common_dataset_value(manifest, "timeframe"),
                    start_date=pooled_curve[0][0].date(),
                    end_date=pooled_curve[-1][0].date(),
                    payload={
                        "dataset_audits": {
                            key: dict(value) for key, value in sorted(dataset_audits.items())
                        },
                        "evidence_role": pooled_spec["evidence_role"],
                        "historical_selection_contaminated": True,
                        "pooled_oos": result.pooled_oos.to_dict(),
                        "protocol_id": manifest["protocol_id"],
                        "runner_kind": pooled_spec["runner_kind"],
                        "source_fold_ids": self._mapping(pooled_spec, "parameters")[
                            "source_fold_ids"
                        ],
                    },
                    metrics=DerivedEvidenceMetrics(
                        initial_capital=1.0,
                        total_return_pct=pooled_metrics.compounded_return_pct,
                        sharpe_ratio=pooled_metrics.sharpe_ratio,
                        max_drawdown_pct=pooled_metrics.max_drawdown_pct,
                        total_trades=sum(
                            item.report.metrics.total_trades
                            for window in result.windows
                            for item in window.oos_asset_reports
                        ),
                        total_signals=sum(
                            item.report.signals_emitted
                            for window in result.windows
                            for item in window.oos_asset_reports
                        ),
                        final_equity=pooled_curve[-1][1],
                        peak_equity=max(value for _timestamp, value in pooled_curve),
                    ),
                    equity_curve=tuple(pooled_curve),
                ),
            )
        )

        # Verify every immutable completed checkpoint before writing any
        # unfinished row. BacktestResultStore performs a canonical content-hash
        # comparison on completed derived evidence and refuses drift.
        for completed_only in (True, False):
            for spec, evidence in evidence_records:
                trial_id = trial_ids[int(spec["sequence"])]
                is_completed = self._trial_status(trial_id) == BacktestTrialStatus.COMPLETED.value
                if is_completed != completed_only:
                    continue
                self._result_store.save_derived_evidence(
                    evidence,
                    trial_id=trial_id,
                )
