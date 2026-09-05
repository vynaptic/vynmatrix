"""Checkpointed benchmark and scoring-ledger evidence."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dev_cli.validation.benchmarks import (
    BenchmarkPortfolioComponent,
    BenchmarkPortfolioEvidence,
    BenchmarkPortfolioMode,
    aggregate_benchmark_portfolio,
    pool_benchmark_portfolio_folds,
)
from dev_cli.validation.campaign_composition import CampaignMixinContract
from dev_cli.validation.campaign_contracts import (
    _DERIVED_SYMBOL_MAX_LENGTH,
    _TERMINAL_STATUSES,
)
from dev_cli.validation.evidence import parse_utc_datetime
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
from lib_application.db.models import (
    BacktestResult,
    BacktestTrial,
)


class CampaignBenchmarkEvidenceMixin(CampaignMixinContract):
    def _execute_checkpointed_derived_group(  # noqa: PLR0912
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        trial_ids: Mapping[int, str],
        experiment_id: int,
        group_name: str,
        build_evidence: Callable[[], Sequence[tuple[Mapping[str, Any], DerivedBacktestEvidence]]],
    ) -> int:
        """Compute a deterministic group once and content-check every checkpoint."""

        if not specs:
            message = f"{group_name} registry is empty"
            raise RuntimeError(message)
        group_trial_ids = [trial_ids[int(spec["sequence"])] for spec in specs]
        statuses = {trial_id: self._trial_status(trial_id) for trial_id in group_trial_ids}
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
                f"{group_name} contains failed, interrupted, or abandoned evidence "
                "and cannot be retried"
            )
            for trial_id in active_ids:
                self._trial_store.mark_failed(
                    trial_id,
                    error_class="PartialDerivedEvidenceError",
                    error_context=message,
                )
            return len(active_ids)

        failed_before = self._failed_trial_count(trial_ids.values())
        completed_before = {
            trial_id
            for trial_id in group_trial_ids
            if self._trial_status(trial_id) == BacktestTrialStatus.COMPLETED.value
        }
        try:
            for trial_id in active_ids:
                self._trial_store.mark_running(trial_id)
            evidence_records = list(build_evidence())
            self._require_complete_evidence_records(
                specs,
                evidence_records,
                group_name=group_name,
            )

            # Completed rows are verified before any unfinished checkpoint is
            # written. A resumed computation therefore cannot silently change
            # evidence that survived an earlier interruption.
            for completed_only in (True, False):
                for spec, evidence in evidence_records:
                    trial_id = trial_ids[int(spec["sequence"])]
                    is_completed = (
                        self._trial_status(trial_id) == BacktestTrialStatus.COMPLETED.value
                    )
                    if is_completed != completed_only:
                        continue
                    self._result_store.save_derived_evidence(
                        evidence,
                        trial_id=trial_id,
                    )
        except (KeyboardInterrupt, SystemExit):
            completed_after = {
                trial_id
                for trial_id in group_trial_ids
                if self._trial_status(trial_id) == BacktestTrialStatus.COMPLETED.value
            }
            if not completed_before and completed_after == completed_before:
                for trial_id in active_ids:
                    if self._trial_status(trial_id) == BacktestTrialStatus.RUNNING.value:
                        self._trial_store.mark_interrupted(
                            trial_id,
                            error_context=f"{group_name} interrupted before any checkpoint",
                        )
            self._refresh_experiment_summary(experiment_id)
            raise
        except Exception as exc:
            completed_after = {
                trial_id
                for trial_id in group_trial_ids
                if self._trial_status(trial_id) == BacktestTrialStatus.COMPLETED.value
            }
            if completed_after != completed_before or completed_before:
                raise
            context = self._safe_error_context(exc)
            for trial_id in active_ids:
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

    @staticmethod
    def _require_complete_evidence_records(
        specs: Sequence[Mapping[str, Any]],
        records: Sequence[tuple[Mapping[str, Any], DerivedBacktestEvidence]],
        *,
        group_name: str,
    ) -> None:
        observed_sequences = [int(spec["sequence"]) for spec, _ in records]
        expected_sequences = [int(spec["sequence"]) for spec in specs]
        if sorted(observed_sequences) != sorted(expected_sequences) or len(
            observed_sequences
        ) != len(set(observed_sequences)):
            message = f"{group_name} did not produce exactly one record per trial"
            raise RuntimeError(message)

    def _execute_benchmark_portfolio_group(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
        report_cache: dict[int, Any],
        report_audit_cache: dict[int, dict[str, Any]],
    ) -> int:
        """Checkpoint one benchmark arm/cost portfolio family atomically."""

        arm_ids = {str(spec["arm_id"]) for spec in specs}
        cost_scenarios = {str(spec["cost_scenario"]) for spec in specs}
        if len(arm_ids) != 1 or len(cost_scenarios) != 1:
            message = "benchmark portfolio group must identify one arm and cost scenario"
            raise RuntimeError(message)
        arm_id = next(iter(arm_ids))
        cost_scenario = next(iter(cost_scenarios))
        return self._execute_checkpointed_derived_group(
            specs,
            trial_ids=trial_ids,
            experiment_id=experiment_id,
            group_name=f"benchmark portfolio group {arm_id}/{cost_scenario}",
            build_evidence=lambda: self._build_benchmark_portfolio_evidence(
                specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
                report_cache=report_cache,
                report_audit_cache=report_audit_cache,
            ),
        )

    def _build_benchmark_portfolio_evidence(  # noqa: PLR0915
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
        report_cache: dict[int, Any],
        report_audit_cache: dict[int, dict[str, Any]],
    ) -> list[tuple[Mapping[str, Any], DerivedBacktestEvidence]]:
        """Rebuild, verify, aggregate, and pool one frozen benchmark family."""

        portfolio_specs, pooled_spec, source_fold_ids = self._benchmark_group_contract(specs)
        strategy = self._mapping(manifest, "strategy")
        asset_class = str(self._mapping(manifest, "habitat")["asset_class_db"])
        arm_id = str(pooled_spec["arm_id"])
        cost_scenario = str(pooled_spec["cost_scenario"])
        portfolio_id = f"benchmark-portfolio:{arm_id}:{cost_scenario}:equal-weight-v1"
        fold_index_by_id = {
            fold_id: fold_index for fold_index, fold_id in enumerate(source_fold_ids)
        }
        portfolio_runs: dict[str, BenchmarkPortfolioEvidence] = {}
        evidence_records: list[tuple[Mapping[str, Any], DerivedBacktestEvidence]] = []

        for spec in portfolio_specs:
            fold_id = str(spec["fold_id"])
            dependency_specs = self._benchmark_component_dependency_specs(
                spec,
                all_specs=all_specs,
            )
            source_rows: list[dict[str, Any]] = []
            component_reports: dict[str, Any] = {}
            for dependency in dependency_specs:
                report, audit, source_hash = self._materialize_benchmark_component(
                    dependency,
                    trial_ids=trial_ids,
                    manifest=manifest,
                    report_cache=report_cache,
                    report_audit_cache=report_audit_cache,
                )
                parameters = self._mapping(dependency, "parameters")
                component = self._mapping(parameters, "component_dataset")
                product = str(component["requested_product"])
                if product in component_reports:
                    message = f"duplicate benchmark portfolio product in {fold_id}: {product}"
                    raise RuntimeError(message)
                component_reports[product] = report
                source_rows.append(
                    {
                        "aggregation_input_sha256": source_hash,
                        "dataset_id": dependency["dataset_id"],
                        "requested_product": product,
                        "source_audit": dict(audit),
                        "trial_id": trial_ids[int(dependency["sequence"])],
                    }
                )

            parameters = self._mapping(spec, "parameters")
            aggregation = self._validated_benchmark_aggregation(
                parameters,
                products=set(component_reports),
            )
            weights = self._mapping(aggregation, "asset_weights")
            components = tuple(
                BenchmarkPortfolioComponent(
                    component_id=product,
                    expected_symbol=product,
                    weight=float(weights[product]),
                    source=component_reports[product],
                )
                for product in sorted(component_reports)
            )
            expected_timestamps = self._expected_daily_timestamps(
                spec,
                manifest=manifest,
            )
            if fold_id == "full-panel":
                mode = BenchmarkPortfolioMode.FULL_PANEL
                fold_index = None
                terminal_policy = None
            else:
                if fold_id not in fold_index_by_id:
                    message = f"benchmark portfolio has an unregistered OOS fold: {fold_id}"
                    raise RuntimeError(message)
                mode = BenchmarkPortfolioMode.OOS_FOLD
                fold_index = fold_index_by_id[fold_id]
                terminal_policy = self._primary_terminal_policy(
                    manifest,
                    products=tuple(sorted(component_reports)),
                    scenario="stressed",
                )
            portfolio = aggregate_benchmark_portfolio(
                portfolio_id,
                components,
                expected_timestamps=expected_timestamps,
                annualization_factor=float(
                    self._mapping(manifest, "inference")["annualization_factor"]
                ),
                mode=mode,
                fold_index=fold_index,
                terminal_exit_cost_policy=terminal_policy,
            )
            portfolio_runs[fold_id] = portfolio
            metrics = portfolio.metrics
            symbol = "+".join(sorted(component_reports))
            if len(symbol) > _DERIVED_SYMBOL_MAX_LENGTH:
                message = "benchmark portfolio evidence symbol exceeds database limit"
                raise ValueError(message)
            evidence_records.append(
                (
                    spec,
                    DerivedBacktestEvidence(
                        strategy_id=str(strategy["strategy_id"]),
                        strategy_semver=str(strategy["strategy_version"]),
                        experiment_id=experiment_id,
                        evidence_kind="equal_weight_benchmark_portfolio",
                        symbol=symbol,
                        asset_class=asset_class,
                        timeframe=self._common_dataset_value(manifest, "timeframe"),
                        start_date=portfolio.equity_curve[0][0].date(),
                        end_date=portfolio.equity_curve[-1][0].date(),
                        payload={
                            "benchmark_portfolio": portfolio.to_dict(),
                            "component_cost_scenario": cost_scenario,
                            "evidence_role": spec["evidence_role"],
                            "historical_selection_contaminated": True,
                            "oos_terminal_cost_scenario": (
                                "not_applicable_unliquidated_full_panel"
                                if mode is BenchmarkPortfolioMode.FULL_PANEL
                                else "stressed"
                            ),
                            "protocol_id": manifest["protocol_id"],
                            "runner_kind": spec["runner_kind"],
                            "source_components": sorted(
                                source_rows,
                                key=lambda row: str(row["dataset_id"]),
                            ),
                        },
                        metrics=DerivedEvidenceMetrics(
                            initial_capital=metrics.initial_capital,
                            total_return_pct=metrics.total_return_pct,
                            sharpe_ratio=metrics.sharpe_ratio,
                            sortino_ratio=metrics.sortino_ratio,
                            max_drawdown_pct=metrics.max_drawdown_pct,
                            total_trades=metrics.total_component_trades,
                            total_signals=metrics.total_component_signals,
                            final_equity=metrics.final_equity,
                            peak_equity=metrics.peak_equity,
                        ),
                        equity_curve=portfolio.equity_curve,
                    ),
                )
            )

        oos_folds = [portfolio_runs[fold_id] for fold_id in source_fold_ids]
        pooled = pool_benchmark_portfolio_folds(
            oos_folds,
            annualization_factor=float(
                self._mapping(manifest, "inference")["annualization_factor"]
            ),
        )
        pooled_curve: list[tuple[datetime, float]] = []
        equity = 1.0
        for observation in pooled.observations:
            equity *= 1.0 + observation.daily_return
            pooled_curve.append((observation.timestamp, equity))
        if not pooled_curve:
            message = "pooled benchmark portfolio contains no OOS observations"
            raise RuntimeError(message)
        pooled_metrics = pooled.metrics
        symbol = "+".join(component.expected_symbol for component in oos_folds[0].components)
        if len(symbol) > _DERIVED_SYMBOL_MAX_LENGTH:
            message = "pooled benchmark portfolio evidence symbol exceeds database limit"
            raise ValueError(message)
        source_fold_rows = [
            {
                "fold_id": fold_id,
                "portfolio_evidence_sha256": manifest_sha256(
                    {"benchmark_portfolio": portfolio_runs[fold_id].to_dict()}
                ),
                "trial_id": trial_ids[
                    int(
                        next(
                            spec["sequence"]
                            for spec in portfolio_specs
                            if spec["fold_id"] == fold_id
                        )
                    )
                ],
            }
            for fold_id in source_fold_ids
        ]
        evidence_records.append(
            (
                pooled_spec,
                DerivedBacktestEvidence(
                    strategy_id=str(strategy["strategy_id"]),
                    strategy_semver=str(strategy["strategy_version"]),
                    experiment_id=experiment_id,
                    evidence_kind="pooled_oos_benchmark_aggregate",
                    symbol=symbol,
                    asset_class=asset_class,
                    timeframe=self._common_dataset_value(manifest, "timeframe"),
                    start_date=pooled_curve[0][0].date(),
                    end_date=pooled_curve[-1][0].date(),
                    payload={
                        "component_cost_scenario": cost_scenario,
                        "evidence_role": pooled_spec["evidence_role"],
                        "historical_selection_contaminated": True,
                        "oos_terminal_cost_scenario": "stressed",
                        "pooled_oos": pooled.to_dict(),
                        "pooling_rule": self._mapping(pooled_spec, "parameters")["pooling_rule"],
                        "protocol_id": manifest["protocol_id"],
                        "runner_kind": pooled_spec["runner_kind"],
                        "source_folds": source_fold_rows,
                    },
                    metrics=DerivedEvidenceMetrics(
                        initial_capital=1.0,
                        total_return_pct=pooled_metrics.compounded_return_pct,
                        sharpe_ratio=pooled_metrics.sharpe_ratio,
                        sortino_ratio=pooled_metrics.sortino_ratio,
                        max_drawdown_pct=pooled_metrics.max_drawdown_pct,
                        total_trades=sum(fold.metrics.total_component_trades for fold in oos_folds),
                        total_signals=sum(
                            fold.metrics.total_component_signals for fold in oos_folds
                        ),
                        final_equity=pooled_curve[-1][1],
                        peak_equity=max(value for _timestamp, value in pooled_curve),
                    ),
                    equity_curve=tuple(pooled_curve),
                ),
            )
        )
        return evidence_records

    def _benchmark_group_contract(
        self,
        specs: Sequence[Mapping[str, Any]],
    ) -> tuple[list[Mapping[str, Any]], Mapping[str, Any], list[str]]:
        portfolio_specs = [
            spec for spec in specs if spec.get("runner_kind") == "equal_weight_benchmark_portfolio"
        ]
        pooled_specs = [
            spec for spec in specs if spec.get("runner_kind") == "pooled_oos_benchmark_aggregate"
        ]
        if len(pooled_specs) != 1:
            message = "benchmark portfolio group requires exactly one pooled OOS trial"
            raise RuntimeError(message)
        pooled_spec = pooled_specs[0]
        raw_source_fold_ids = self._mapping(pooled_spec, "parameters").get("source_fold_ids")
        if isinstance(raw_source_fold_ids, (str, bytes)) or not isinstance(
            raw_source_fold_ids, Sequence
        ):
            message = "benchmark pooled source_fold_ids must be a JSON array"
            raise TypeError(message)
        source_fold_ids = [str(fold_id) for fold_id in raw_source_fold_ids]
        if not source_fold_ids or len(source_fold_ids) != len(set(source_fold_ids)):
            message = "benchmark pooled source_fold_ids must be non-empty and unique"
            raise ValueError(message)
        by_fold = {str(spec["fold_id"]): spec for spec in portfolio_specs}
        expected_fold_ids = {"full-panel", *source_fold_ids}
        if len(by_fold) != len(portfolio_specs) or set(by_fold) != expected_fold_ids:
            message = (
                "benchmark portfolio group must exactly cover full-panel and every "
                "registered OOS fold"
            )
            raise RuntimeError(message)
        arm_ids = {str(spec["arm_id"]) for spec in specs}
        costs = {str(spec["cost_scenario"]) for spec in specs}
        if len(arm_ids) != 1 or len(costs) != 1:
            message = "benchmark portfolio group mixes arm or cost identities"
            raise RuntimeError(message)
        ordered = [by_fold["full-panel"], *(by_fold[fold_id] for fold_id in source_fold_ids)]
        return ordered, pooled_spec, source_fold_ids

    def _benchmark_component_dependency_specs(
        self,
        portfolio_spec: Mapping[str, Any],
        *,
        all_specs: Sequence[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        parameters = self._mapping(portfolio_spec, "parameters")
        raw_components = parameters.get("component_datasets")
        if isinstance(raw_components, (str, bytes)) or not isinstance(raw_components, Sequence):
            message = "benchmark portfolio component_datasets must be a JSON array"
            raise TypeError(message)
        components = [
            self._require_mapping(component, field="benchmark.component_datasets[]")
            for component in raw_components
        ]
        expected_by_dataset = {str(component["dataset_id"]): component for component in components}
        if not expected_by_dataset or len(expected_by_dataset) != len(components):
            message = "benchmark portfolio component datasets must be non-empty and unique"
            raise ValueError(message)
        candidates = [
            spec
            for spec in all_specs
            if spec.get("runner_kind") == "executable_benchmark_component"
            and spec.get("arm_id") == portfolio_spec.get("arm_id")
            and spec.get("cost_scenario") == portfolio_spec.get("cost_scenario")
            and spec.get("fold_id") == portfolio_spec.get("fold_id")
            and str(spec.get("dataset_id")) in expected_by_dataset
        ]
        by_dataset = {str(spec["dataset_id"]): spec for spec in candidates}
        if len(by_dataset) != len(candidates) or set(by_dataset) != set(expected_by_dataset):
            message = "benchmark component dependencies do not exactly cover the portfolio"
            raise RuntimeError(message)
        common_keys = (
            "protocol_benchmark_name",
            "benchmark_kind",
            "definition",
            "calibration_end_exclusive",
        )
        for dataset_id, dependency in by_dataset.items():
            dependency_parameters = self._mapping(dependency, "parameters")
            if any(
                canonical_manifest_bytes({key: dependency_parameters.get(key)})
                != canonical_manifest_bytes({key: parameters.get(key)})
                for key in common_keys
            ):
                message = f"benchmark component semantics differ for {dataset_id}"
                raise RuntimeError(message)
            if canonical_manifest_bytes(
                self._mapping(dependency_parameters, "component_dataset")
            ) != canonical_manifest_bytes(expected_by_dataset[dataset_id]):
                message = f"benchmark component dataset identity differs for {dataset_id}"
                raise RuntimeError(message)
            for boundary in ("evaluation_start", "evaluation_end_exclusive"):
                if dependency.get(boundary) != portfolio_spec.get(boundary):
                    message = f"benchmark component {boundary} differs for {dataset_id}"
                    raise RuntimeError(message)
        return [by_dataset[dataset_id] for dataset_id in sorted(by_dataset)]

    def _materialize_benchmark_component(
        self,
        spec: Mapping[str, Any],
        *,
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
        report_cache: dict[int, Any],
        report_audit_cache: dict[int, dict[str, Any]],
    ) -> tuple[Any, dict[str, Any], str]:
        sequence = int(spec["sequence"])
        trial_id = trial_ids[sequence]
        status = self._trial_status(trial_id)
        if status in {
            BacktestTrialStatus.FAILED.value,
            BacktestTrialStatus.INTERRUPTED.value,
            BacktestTrialStatus.ABANDONED.value,
        }:
            message = (
                "benchmark portfolio dependency is terminal without reusable evidence: "
                f"sequence={sequence}, status={status}"
            )
            raise RuntimeError(message)

        report = report_cache.get(sequence)
        audit = report_audit_cache.get(sequence)
        should_persist = status != BacktestTrialStatus.COMPLETED.value
        try:
            if report is None or audit is None:
                if should_persist:
                    self._trial_store.mark_running(trial_id)
                report, raw_audit = self._execute_benchmark_component(
                    spec,
                    manifest=manifest,
                )
                audit = dict(raw_audit)
                self._save_report_result(
                    report,
                    audit=audit,
                    spec=spec,
                    trial_id=trial_id,
                    manifest=manifest,
                )
                report_cache[sequence] = report
                report_audit_cache[sequence] = audit
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
        if audit is None:  # pragma: no cover - cache/materialization invariant
            message = "benchmark component audit was not materialized"
            raise RuntimeError(message)
        source_hash = self._verify_benchmark_component_checkpoint(
            spec,
            trial_id=trial_id,
            report=report,
            audit=audit,
            manifest=manifest,
        )
        return report, audit, source_hash

    def _verify_benchmark_component_checkpoint(
        self,
        spec: Mapping[str, Any],
        *,
        trial_id: str,
        report: Any,
        audit: Mapping[str, Any],
        manifest: Mapping[str, Any],
    ) -> str:
        fresh = self._benchmark_component_snapshot(
            spec,
            trial_id=trial_id,
            report_payload=report.to_dict(),
            equity_curve=[
                [timestamp.isoformat(), equity] for timestamp, equity in report.equity_curve
            ],
            audit=audit,
            manifest=manifest,
        )
        with Session(self._engine) as session:
            trial = session.execute(
                select(BacktestTrial).where(BacktestTrial.trial_id == trial_id)
            ).scalar_one_or_none()
            if (
                trial is None
                or trial.status != BacktestTrialStatus.COMPLETED.value
                or trial.result_id is None
            ):
                message = "benchmark component checkpoint is not completed and linked"
                raise RuntimeError(message)
            result = session.get(BacktestResult, trial.result_id)
            if result is None or not isinstance(result.meta, Mapping):
                message = "benchmark component checkpoint result is missing or malformed"
                raise RuntimeError(message)
            expected_meta = {
                "arm_id": spec["arm_id"],
                "dataset_audit": dict(audit),
                "evidence_role": spec["evidence_role"],
                "manifest_hash": manifest_sha256(manifest),
                "protocol_id": manifest["protocol_id"],
                "runner_kind": spec["runner_kind"],
                "trial_id": trial_id,
                "validation_design": manifest["validation_design"],
            }
            if any(result.meta.get(key) != value for key, value in expected_meta.items()):
                message = (
                    "benchmark component checkpoint provenance differs from its frozen "
                    f"trial: {trial_id}"
                )
                raise RuntimeError(message)
            stored = self._benchmark_component_snapshot(
                spec,
                trial_id=trial_id,
                report_payload=result.meta.get("report"),
                equity_curve=result.equity_curve,
                audit=result.meta.get("dataset_audit"),
                manifest=manifest,
            )
        fresh_hash = str(manifest_sha256(fresh))
        stored_hash = str(manifest_sha256(stored))
        if fresh_hash != stored_hash:
            message = (
                "benchmark component checkpoint content differs from its deterministic rerun: "
                f"{trial_id}"
            )
            raise RuntimeError(message)
        self._result_store.load_verified_report_payload(trial_id)
        return self._result_store.verified_report_sha256(trial_id)

    @staticmethod
    def _benchmark_component_snapshot(
        spec: Mapping[str, Any],
        *,
        trial_id: str,
        report_payload: Any,
        equity_curve: Any,
        audit: Any,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(report_payload, Mapping):
            message = "benchmark component checkpoint report payload is malformed"
            raise TypeError(message)
        if isinstance(equity_curve, (str, bytes)) or not isinstance(equity_curve, Sequence):
            message = "benchmark component checkpoint equity curve is malformed"
            raise TypeError(message)
        if not isinstance(audit, Mapping):
            message = "benchmark component checkpoint audit payload is malformed"
            raise TypeError(message)
        return {
            "audit": dict(audit),
            "equity_curve": list(equity_curve),
            "identity": {
                "arm_id": spec["arm_id"],
                "cost_scenario": spec["cost_scenario"],
                "dataset_id": spec["dataset_id"],
                "evidence_role": spec["evidence_role"],
                "fold_id": spec["fold_id"],
                "manifest_hash": manifest_sha256(manifest),
                "protocol_id": manifest["protocol_id"],
                "runner_kind": spec["runner_kind"],
                "trial_id": trial_id,
            },
            "report": dict(report_payload),
        }

    def _validated_benchmark_aggregation(
        self,
        parameters: Mapping[str, Any],
        *,
        products: set[str],
    ) -> dict[str, Any]:
        aggregation = self._mapping(
            parameters,
            "portfolio_aggregation",
        )
        expected = {
            "daily_return_rule": ("arithmetic_mean_of_exact_timestamp_aligned_asset_net_returns"),
            "full_panel_terminal_treatment": ("mark_to_market_without_counting_a_closed_trade"),
            "missing_or_unaligned_asset_observation": "trial_failure",
            "oos_fold_terminal_treatment": (
                "last_oos_close_mark_plus_stressed_hypothetical_liquidation_cost_"
                "before_fold_pooling"
            ),
        }
        if any(aggregation.get(key) != value for key, value in expected.items()):
            message = "benchmark portfolio aggregation semantics are unsupported"
            raise ValueError(message)
        weights = self._mapping(aggregation, "asset_weights")
        if set(weights) != products:
            message = "benchmark portfolio weights do not exactly cover component products"
            raise ValueError(message)
        return aggregation

    def _expected_daily_timestamps(
        self,
        spec: Mapping[str, Any],
        *,
        manifest: Mapping[str, Any],
    ) -> tuple[datetime, ...]:
        start = parse_utc_datetime(spec["evaluation_start"], field="trial.evaluation_start")
        end = parse_utc_datetime(
            spec["evaluation_end_exclusive"],
            field="trial.evaluation_end_exclusive",
        )
        parity = self._mapping(manifest, "data_parity_attestation")
        calendar = parity.get("common_calendar")
        if calendar is None:
            timestamps: list[datetime] = []
            current = start
            while current < end:
                timestamps.append(current)
                current += timedelta(days=1)
        else:
            common_calendar = self._require_mapping(
                calendar,
                field="data_parity_attestation.common_calendar",
            )
            raw_ledger = common_calendar.get("ledger")
            if isinstance(raw_ledger, (str, bytes)) or not isinstance(
                raw_ledger,
                Sequence,
            ):
                message = "attested common-calendar ledger must be a JSON array"
                raise TypeError(message)
            rows = [
                self._require_mapping(
                    row,
                    field=f"data_parity_attestation.common_calendar.ledger[{index}]",
                )
                for index, row in enumerate(raw_ledger)
            ]
            if len(rows) != int(common_calendar.get("candidate_days", -1)):
                message = "attested common-calendar ledger length is inconsistent"
                raise ValueError(message)
            source_timestamps: list[datetime] = []
            included_count = 0
            for index, row in enumerate(rows):
                included = row.get("included")
                if not isinstance(included, bool):
                    message = (
                        f"attested common-calendar included flag must be a bool: index={index}"
                    )
                    raise TypeError(message)
                source_timestamp = parse_utc_datetime(
                    row.get("period_start"),
                    field=(f"data_parity_attestation.common_calendar.ledger[{index}].period_start"),
                )
                source_timestamps.append(source_timestamp)
                if included:
                    included_count += 1
            if any(
                current != previous + timedelta(days=1)
                for previous, current in itertools.pairwise(source_timestamps)
            ):
                message = "attested common-calendar source timestamps are not daily"
                raise ValueError(message)
            if included_count != int(common_calendar.get("included_day_count", -1)):
                message = "attested common-calendar included count is inconsistent"
                raise ValueError(message)
            if len(rows) - included_count != int(common_calendar.get("excluded_day_count", -1)):
                message = "attested common-calendar excluded count is inconsistent"
                raise ValueError(message)
            timestamps = [
                source_timestamp + timedelta(days=1)
                for source_timestamp, row in zip(source_timestamps, rows, strict=True)
                if row["included"] and start <= source_timestamp + timedelta(days=1) < end
            ]
        if not timestamps:
            message = "benchmark portfolio evaluation interval contains no daily timestamps"
            raise ValueError(message)
        return tuple(timestamps)

    def _evidence_dates(self, spec: Mapping[str, Any]) -> tuple[date, date]:
        start = parse_utc_datetime(spec["evaluation_start"], field="trial.evaluation_start")
        end = parse_utc_datetime(
            spec["evaluation_end_exclusive"],
            field="trial.evaluation_end_exclusive",
        )
        if start >= end:
            message = "derived evidence boundaries must be increasing"
            raise ValueError(message)
        return start.date(), (end - timedelta(microseconds=1)).date()

    def _execute_scoring_ledger_group(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> int:
        return self._execute_checkpointed_derived_group(
            specs,
            trial_ids=trial_ids,
            experiment_id=experiment_id,
            group_name="scoring/binding ledger group",
            build_evidence=lambda: self._build_scoring_ledger_evidence(
                specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            ),
        )
