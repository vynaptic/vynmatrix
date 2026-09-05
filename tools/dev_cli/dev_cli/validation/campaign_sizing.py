"""Conditional position-sizing evidence over frozen signals."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from dev_cli.validation.backtest.engine import BacktestEngine, RawSignalEvidence
from dev_cli.validation.backtest.execution import (
    normalize_execution_bar,
)
from dev_cli.validation.benchmarks import (
    BenchmarkPortfolioComponent,
    BenchmarkPortfolioEvidence,
    BenchmarkPortfolioMode,
    VolatilityTargetConfig,
    aggregate_benchmark_portfolio,
    calibrate_fixed_volatility_exposure,
    pool_benchmark_portfolio_folds,
)
from dev_cli.validation.campaign_composition import CampaignMixinContract
from dev_cli.validation.campaign_contracts import (
    _SIZING_RAW_GATE_IDS,
    _TERMINAL_STATUSES,
)
from dev_cli.validation.evidence import parse_utc_datetime
from dev_cli.validation.persistence.backtest_manifest_store import (
    manifest_sha256,
)
from dev_cli.validation.persistence.backtest_result_store import (
    BacktestResultStore,
    DerivedBacktestEvidence,
    DerivedEvidenceMetrics,
)
from dev_cli.validation.persistence.backtest_trial_store import (
    BacktestTrialStatus,
)
from dev_cli.validation.position_sizing import SIZING_EVIDENCE_SCHEMA, CoinbaseSizingRules
from lib_strategy.position_sizing import (
    MissingStopBehavior,
    PositionSizingPolicy,
    PreBrokerQuantityRounding,
)


class CampaignSizingMixin(CampaignMixinContract):
    def _execute_conditional_sizing_group(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> int:
        """Replay baseline sizing only after immutable raw qualification."""

        active_ids = [
            trial_ids[int(spec["sequence"])]
            for spec in specs
            if self._trial_status(trial_ids[int(spec["sequence"])]) not in _TERMINAL_STATUSES
        ]
        if not active_ids:
            return 0
        failed_before = self._failed_trial_count(trial_ids.values())
        try:
            eligible, reason = self._conditional_sizing_gate(
                all_specs,
                trial_ids=trial_ids,
            )
        except Exception as exc:
            # A missing or corrupt gate is evidence failure, not an orchestration
            # exception which may prevent the final disposition checkpoint from
            # recording BLOCKED_INSUFFICIENT_EVIDENCE.  Preserve the exact failure
            # on every still-active conditional row and continue to the final audit.
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
        if not eligible:
            for spec in specs:
                trial_id = trial_ids[int(spec["sequence"])]
                if self._trial_status(trial_id) in {
                    BacktestTrialStatus.REGISTERED.value,
                    BacktestTrialStatus.RUNNING.value,
                }:
                    self._trial_store.mark_abandoned(trial_id, error_context=reason)
            return 0
        return self._execute_checkpointed_derived_group(
            specs,
            trial_ids=trial_ids,
            experiment_id=experiment_id,
            group_name="conditional baseline sizing group",
            build_evidence=lambda: self._build_conditional_sizing_evidence(
                specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            ),
        )

    def _conditional_sizing_gate(
        self,
        all_specs: Sequence[Mapping[str, Any]],
        *,
        trial_ids: Mapping[int, str],
    ) -> tuple[bool, str]:
        robustness_specs = [
            spec
            for spec in all_specs
            if spec.get("runner_kind") == "robustness_concentration_inference"
        ]
        if len(robustness_specs) != 1:
            message = "conditional sizing requires one registered robustness checkpoint"
            raise RuntimeError(message)
        spec = robustness_specs[0]
        payload = self._result_store.load_derived_payload(
            trial_ids[int(spec["sequence"])],
            expected_evidence_kind="robustness_concentration_inference",
        )
        blockers = payload.get("blocking_quantitative_gate_ids")
        if isinstance(blockers, (str, bytes)) or not isinstance(blockers, Sequence):
            message = "robustness checkpoint has no quantitative blocking-gate ledger"
            raise TypeError(message)
        normalized_blockers = [str(item) for item in blockers]
        raw_checks = payload.get("quantitative_gate_checks")
        if not isinstance(raw_checks, Mapping) or set(raw_checks) != set(_SIZING_RAW_GATE_IDS):
            message = "robustness checkpoint has no exact baseline quantitative gate map"
            raise TypeError(message)
        if any(not isinstance(raw_checks[gate_id], bool) for gate_id in _SIZING_RAW_GATE_IDS):
            message = "robustness quantitative gate checks must be explicit booleans"
            raise TypeError(message)
        expected_blockers = [
            gate_id for gate_id in _SIZING_RAW_GATE_IDS if raw_checks[gate_id] is False
        ]
        if normalized_blockers != expected_blockers:
            message = "robustness blocking gates differ from the exact false gate checks"
            raise RuntimeError(message)
        missing_evidence = payload.get("missing_evidence_ids")
        if isinstance(missing_evidence, (str, bytes)) or not isinstance(missing_evidence, Sequence):
            message = "robustness checkpoint has no completion-evidence ledger"
            raise TypeError(message)
        normalized_missing_evidence = [str(item) for item in missing_evidence]
        zero_trades_or_exposure = payload.get("raw_baseline_zero_trades_or_exposure")
        if not isinstance(zero_trades_or_exposure, bool):
            message = "robustness checkpoint has no explicit baseline zero-exposure state"
            raise TypeError(message)
        baseline_arm_id = self._nonempty_string(
            self._mapping(spec, "parameters").get("source_arm_id"),
            field="robustness.source_arm_id",
        )
        eligible = (
            payload.get("disposition_source_arm_id") == baseline_arm_id
            and payload.get("quantitative_robustness_gate_eligible") is True
            and all(raw_checks[gate_id] is True for gate_id in _SIZING_RAW_GATE_IDS)
            and not expected_blockers
            and payload.get("evidence_complete") is True
            and not normalized_missing_evidence
            and not zero_trades_or_exposure
        )
        if eligible:
            return True, "eligible"
        if zero_trades_or_exposure:
            return False, "raw_baseline_zero_trades_or_exposure"
        reason = "conditional_sizing_abandoned:baseline_quantitative_gate_ineligible:" + (
            ",".join(expected_blockers)
            if expected_blockers
            else (
                "evidence_incomplete:" + ",".join(normalized_missing_evidence)
                if normalized_missing_evidence
                else "qualification_false"
            )
        )
        return False, reason

    def _build_conditional_sizing_evidence(  # noqa: PLR0912, PLR0915
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> list[tuple[Mapping[str, Any], DerivedBacktestEvidence]]:
        component_specs = [
            spec for spec in specs if spec.get("runner_kind") == "conditional_sizing_oos_component"
        ]
        pooled_specs = [
            spec for spec in specs if spec.get("runner_kind") == "conditional_sizing_pooled_oos"
        ]
        arm_ids = sorted({str(spec["arm_id"]) for spec in specs})
        fold_ids = sorted(
            {str(spec["fold_id"]) for spec in component_specs},
            key=lambda value: parse_utc_datetime(
                next(
                    spec["evaluation_start"] for spec in component_specs if spec["fold_id"] == value
                ),
                field="sizing.evaluation_start",
            ),
        )
        datasets = {
            str(dataset["dataset_id"]): dataset
            for dataset in self._list_of_mappings(self._mapping(manifest, "data"), "datasets")
        }
        products = {str(dataset["requested_product"]) for dataset in datasets.values()}
        expected_components = len(arm_ids) * len(fold_ids) * len(products)
        if (
            not arm_ids
            or len(component_specs) != expected_components
            or len(pooled_specs) != len(arm_ids)
        ):
            message = "conditional sizing registry does not exactly cover arms/assets/folds"
            raise RuntimeError(message)
        baseline_arm_id = self._historical_disposition_policy(manifest).baseline_arm_id
        if any(
            self._mapping(spec, "parameters").get("source_arm_id") != baseline_arm_id
            for spec in specs
        ):
            message = "conditional sizing source differs from the registered baseline arm"
            raise RuntimeError(message)

        source = self._load_direct_report_evidence(
            all_specs,
            trial_ids=trial_ids,
            manifest=manifest,
            arm_id=baseline_arm_id,
            cost_scenario="expected",
            fold_ids=fold_ids,
        )
        by_identity = {
            (str(spec["arm_id"]), str(spec["fold_id"]), str(spec["dataset_id"])): spec
            for spec in component_specs
        }
        if len(by_identity) != len(component_specs):
            message = "conditional sizing component identities are duplicated"
            raise RuntimeError(message)

        strategy = self._mapping(manifest, "strategy")
        asset_class = str(self._mapping(manifest, "habitat")["asset_class_db"])
        reports: dict[tuple[str, str, str], Any] = {}
        component_payload_hashes: dict[tuple[str, str, str], str] = {}
        records: list[tuple[Mapping[str, Any], DerivedBacktestEvidence]] = []
        bars_cache: dict[str, list[Any]] = {}
        audit_cache: dict[str, dict[str, Any]] = {}
        for arm_id in arm_ids:
            for fold_id in fold_ids:
                for dataset_id in sorted(datasets):
                    dataset = datasets[dataset_id]
                    product = str(dataset["requested_product"])
                    spec = by_identity[(arm_id, fold_id, dataset_id)]
                    source_spec, source_trial_id, source_envelope = source[(fold_id, product)]
                    source_report = self._mapping(source_envelope, "report")
                    raw_ledger = source_report.get("raw_signal_ledger")
                    if isinstance(raw_ledger, (str, bytes)) or not isinstance(raw_ledger, Sequence):
                        message = (
                            f"conditional sizing source ledger is invalid: {fold_id}/{product}"
                        )
                        raise TypeError(message)
                    evidence = tuple(
                        RawSignalEvidence.from_dict(
                            self._require_mapping(
                                row,
                                field=f"baseline.{fold_id}.{product}.raw_signal_ledger[{index}]",
                            )
                        )
                        for index, row in enumerate(raw_ledger)
                    )
                    if int(source_report.get("signals_emitted", -1)) != len(evidence):
                        message = (
                            "conditional sizing source signal coverage differs: "
                            f"{fold_id}/{product}"
                        )
                        raise RuntimeError(message)
                    if dataset_id not in bars_cache:
                        loaded_dataset, bars, audit = self._load_trial_dataset(
                            spec,
                            manifest=manifest,
                        )
                        if str(loaded_dataset["dataset_id"]) != dataset_id:
                            message = "conditional sizing loaded a different frozen dataset"
                            raise RuntimeError(message)
                        bars_cache[dataset_id] = bars
                        audit_cache[dataset_id] = audit
                    bars = bars_cache[dataset_id]
                    policy, coinbase_rules, sizing_calibration = self._conditional_sizing_policy(
                        spec,
                        dataset=dataset,
                        bars=bars,
                        annualization_factor=float(
                            self._mapping(manifest, "inference")["annualization_factor"]
                        ),
                    )
                    config = replace(
                        self._backtest_config(
                            spec,
                            dataset=dataset,
                            manifest=manifest,
                            fill_policy=self._fill_policy(spec, manifest=manifest),
                        ),
                        sizing_policy=policy,
                        coinbase_sizing_rules=coinbase_rules,
                    )
                    report = BacktestEngine().replay_consolidated_signal_evidence(
                        bars,
                        evidence,
                        config,
                        source_bootstrap_bars=int(source_report["bootstrap_bars"]),
                        source_flat_model_boundary_applied=bool(
                            source_report["flat_model_boundary_applied"]
                        ),
                    )
                    if tuple(item.canonical_json for item in report.raw_signal_ledger) != tuple(
                        item.canonical_json for item in evidence
                    ):
                        message = "conditional sizing replay changed canonical signal evidence"
                        raise RuntimeError(message)
                    complete_trades = BacktestResultStore._report_trade_rows(report)
                    content = {
                        "dataset_audit": audit_cache[dataset_id],
                        "equity_curve": [
                            [timestamp.isoformat(), value]
                            for timestamp, value in report.equity_curve
                        ],
                        "report": report.to_dict(),
                        "trades": complete_trades,
                    }
                    content_hash = manifest_sha256(content)
                    identity = (arm_id, fold_id, product)
                    reports[identity] = report
                    component_payload_hashes[identity] = content_hash
                    start_date, end_date = self._evidence_dates(spec)
                    records.append(
                        (
                            spec,
                            DerivedBacktestEvidence(
                                strategy_id=str(strategy["strategy_id"]),
                                strategy_semver=str(strategy["strategy_version"]),
                                experiment_id=experiment_id,
                                evidence_kind="conditional_sizing_oos_component",
                                symbol=product,
                                asset_class=asset_class,
                                timeframe=self._common_dataset_value(manifest, "timeframe"),
                                start_date=start_date,
                                end_date=end_date,
                                payload={
                                    **content,
                                    "component_content_sha256": content_hash,
                                    "evidence_role": spec["evidence_role"],
                                    "historical_selection_contaminated": True,
                                    "protocol_id": manifest["protocol_id"],
                                    "runner_kind": spec["runner_kind"],
                                    "signal_source": {
                                        "arm_id": baseline_arm_id,
                                        "cost_scenario": "expected",
                                        "dataset_id": source_spec["dataset_id"],
                                        "fold_id": fold_id,
                                        "report_sha256": source_envelope["report_sha256"],
                                        "trial_id": source_trial_id,
                                    },
                                    "coinbase_sizing_rules": coinbase_rules.to_dict(),
                                    "sizing_calibration": sizing_calibration,
                                    "sizing_evidence_schema": SIZING_EVIDENCE_SCHEMA,
                                    "sizing_policy": policy.to_dict(),
                                },
                                metrics=self._report_derived_metrics(report),
                                equity_curve=tuple(report.equity_curve),
                            ),
                        )
                    )

        weights = self._mapping(
            self._mapping(manifest, "portfolio_assumptions"), "portfolio_asset_weights"
        )
        if set(weights) != products:
            message = "conditional sizing weights do not exactly cover products"
            raise RuntimeError(message)
        terminal_policy = self._primary_terminal_policy(
            manifest,
            products=tuple(sorted(products)),
            scenario="stressed",
        )
        pooled_by_arm = {str(spec["arm_id"]): spec for spec in pooled_specs}
        for arm_id in arm_ids:
            folds: list[BenchmarkPortfolioEvidence] = []
            source_components: list[dict[str, Any]] = []
            for fold_index, fold_id in enumerate(fold_ids):
                fold_spec = next(
                    spec
                    for spec in component_specs
                    if spec["arm_id"] == arm_id and spec["fold_id"] == fold_id
                )
                components = tuple(
                    BenchmarkPortfolioComponent(
                        component_id=product,
                        expected_symbol=product,
                        weight=float(weights[product]),
                        source=reports[(arm_id, fold_id, product)],
                    )
                    for product in sorted(products)
                )
                portfolio = aggregate_benchmark_portfolio(
                    f"{arm_id}:baseline-signal-ledger",
                    components,
                    expected_timestamps=self._expected_daily_timestamps(
                        fold_spec,
                        manifest=manifest,
                    ),
                    annualization_factor=float(
                        self._mapping(manifest, "inference")["annualization_factor"]
                    ),
                    mode=BenchmarkPortfolioMode.OOS_FOLD,
                    fold_index=fold_index,
                    terminal_exit_cost_policy=terminal_policy,
                )
                folds.append(portfolio)
                source_components.extend(
                    {
                        "component_content_sha256": component_payload_hashes[
                            (arm_id, fold_id, product)
                        ],
                        "fold_id": fold_id,
                        "product": product,
                    }
                    for product in sorted(products)
                )
            pooled = pool_benchmark_portfolio_folds(
                folds,
                annualization_factor=float(
                    self._mapping(manifest, "inference")["annualization_factor"]
                ),
            )
            pooled_curve: list[tuple[datetime, float]] = []
            pooled_equity = 1.0
            for observation in pooled.observations:
                pooled_equity *= 1.0 + observation.daily_return
                pooled_curve.append((observation.timestamp, pooled_equity))
            if not pooled_curve:
                message = "conditional sizing pooled OOS evidence is empty"
                raise RuntimeError(message)
            spec = pooled_by_arm[arm_id]
            start_date, end_date = self._evidence_dates(spec)
            metrics = pooled.metrics
            records.append(
                (
                    spec,
                    DerivedBacktestEvidence(
                        strategy_id=str(strategy["strategy_id"]),
                        strategy_semver=str(strategy["strategy_version"]),
                        experiment_id=experiment_id,
                        evidence_kind="conditional_sizing_pooled_oos",
                        symbol="+".join(sorted(products)),
                        asset_class=asset_class,
                        timeframe=self._common_dataset_value(manifest, "timeframe"),
                        start_date=start_date,
                        end_date=end_date,
                        payload={
                            "evidence_role": spec["evidence_role"],
                            "fold_portfolios": [fold.to_dict() for fold in folds],
                            "historical_selection_contaminated": True,
                            "pooled_oos": pooled.to_dict(),
                            "protocol_id": manifest["protocol_id"],
                            "runner_kind": spec["runner_kind"],
                            "signal_source_arm_id": baseline_arm_id,
                            "source_components": source_components,
                        },
                        metrics=DerivedEvidenceMetrics(
                            initial_capital=1.0,
                            total_return_pct=metrics.compounded_return_pct,
                            sharpe_ratio=metrics.sharpe_ratio,
                            sortino_ratio=metrics.sortino_ratio,
                            max_drawdown_pct=metrics.max_drawdown_pct,
                            total_trades=sum(
                                report.metrics.total_trades
                                for key, report in reports.items()
                                if key[0] == arm_id
                            ),
                            total_signals=sum(
                                report.signals_emitted
                                for key, report in reports.items()
                                if key[0] == arm_id
                            ),
                            final_equity=pooled_curve[-1][1],
                            peak_equity=max(value for _timestamp, value in pooled_curve),
                        ),
                        equity_curve=tuple(pooled_curve),
                    ),
                )
            )
        return records

    def _conditional_sizing_policy(
        self,
        spec: Mapping[str, Any],
        *,
        dataset: Mapping[str, Any],
        bars: Sequence[Any],
        annualization_factor: float,
    ) -> tuple[PositionSizingPolicy, CoinbaseSizingRules, dict[str, object] | None]:
        parameters = self._mapping(spec, "parameters")
        arm = self._mapping(parameters, "sizing_arm")
        if parameters.get("pre_broker_quantity_rounding") != (
            PreBrokerQuantityRounding.PRICE_TIERED_DECIMALS.value
        ):
            message = "conditional sizing must freeze price-tiered pre-broker rounding"
            raise ValueError(message)
        metadata = self._mapping(dataset, "metadata_snapshot")
        coinbase_rules = CoinbaseSizingRules(
            base_increment=str(metadata["base_increment"]),
            quote_increment=str(metadata["quote_increment"]),
            minimum_order_notional=float(parameters["minimum_order_notional_usd"]),
            source=f"{parameters['precision_source']}:{dataset['requested_product']}",
        )
        arm_id = str(spec["arm_id"])
        maximum_position_pct = float(arm["maximum_notional_pct"])
        minimum_position_notional = float(parameters["minimum_order_notional_usd"])
        if arm_id == "SIZE_CURRENT_BINDING_FIXED_EQUITY_1PCT":
            policy = PositionSizingPolicy.fixed_percent(
                policy_id=arm_id,
                fixed_pct=float(arm["fixed_pct"]),
                max_position_pct=maximum_position_pct,
                minimum_position_notional=minimum_position_notional,
                pre_broker_quantity_rounding=(PreBrokerQuantityRounding.PRICE_TIERED_DECIMALS),
            )
            return policy, coinbase_rules, None
        if arm_id == "SIZE_INITIAL_STOP_RISK_1PCT":
            policy = PositionSizingPolicy.initial_stop_risk(
                policy_id=arm_id,
                risk_pct=float(arm["risk_per_trade"]),
                fixed_pct_fallback=0.01,
                max_position_pct=maximum_position_pct,
                minimum_position_notional=minimum_position_notional,
                missing_stop_behavior=MissingStopBehavior.REJECT,
                pre_broker_quantity_rounding=(PreBrokerQuantityRounding.PRICE_TIERED_DECIMALS),
            )
            return policy, coinbase_rules, None
        if arm_id == "SIZE_TRAINING_VOL_TARGET_5PCT":
            calibration_end = parse_utc_datetime(
                spec["evaluation_start"], field="sizing.evaluation_start"
            )
            eligible_training = [
                normalize_execution_bar(bar)
                for bar in bars
                if normalize_execution_bar(bar).timestamp < calibration_end
            ]
            calibration = calibrate_fixed_volatility_exposure(
                eligible_training,
                VolatilityTargetConfig(
                    maximum_lookback_bars=int(arm["calibration_lookback_calendar_days"]),
                    minimum_observations=int(arm["minimum_calibration_returns"]),
                    target_annualized_volatility=float(arm["target_annualized_volatility"]),
                    exposure_floor=float(arm["minimum_notional_pct"]),
                    exposure_cap=float(arm["maximum_notional_pct"]),
                ),
                annualization_factor=annualization_factor,
            )
            policy = calibration.to_fixed_position_sizing_policy(
                policy_id=arm_id,
                maximum_position_pct=float(arm["maximum_notional_pct"]),
                minimum_position_notional=float(parameters["minimum_order_notional_usd"]),
            )
            return (
                replace(
                    policy,
                    pre_broker_quantity_rounding=(PreBrokerQuantityRounding.PRICE_TIERED_DECIMALS),
                ),
                coinbase_rules,
                calibration.to_manifest_dict(),
            )
        message = f"unsupported registered conditional sizing arm: {arm_id}"
        raise ValueError(message)
