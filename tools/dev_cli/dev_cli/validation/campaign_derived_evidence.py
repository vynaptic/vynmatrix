"""Power, CSCV, robustness, and redesign evidence construction."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from dev_cli.validation.backtest.engine import RawSignalEvidence
from dev_cli.validation.backtest.execution import (
    adverse_execution_price,
    execution_fill_costs,
)
from dev_cli.validation.campaign_composition import CampaignMixinContract
from dev_cli.validation.campaign_contracts import (
    _DERIVED_SYMBOL_MAX_LENGTH,
    _POWER_RUNNER,
    _REDESIGN_RUNNER,
)
from dev_cli.validation.evidence import parse_utc_datetime
from dev_cli.validation.fixed_robustness import build_fixed_baseline_robustness_evidence
from dev_cli.validation.persistence.backtest_manifest_store import (
    manifest_sha256,
)
from dev_cli.validation.persistence.backtest_result_store import (
    DerivedBacktestEvidence,
    DerivedEvidenceMetrics,
)
from dev_cli.validation.prospective_power import (
    ClosedTradeOutcome,
    observe_prospective_design_inputs,
    search_prospective_duration_from_observed_trades,
)
from dev_cli.validation.redesign_diagnostics import (
    build_registered_redesign_candidate_evidence,
    build_registered_report_cscv_pbo,
)
from dev_cli.validation.report_evidence import (
    StandardReportEvidence,
    standard_report_evidence_from_mapping,
)


class CampaignDerivedEvidenceMixin(CampaignMixinContract):
    def _execute_power_group(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> int:
        """Checkpoint the three registered power sensitivities as one design."""

        return self._execute_checkpointed_derived_group(
            specs,
            trial_ids=trial_ids,
            experiment_id=experiment_id,
            group_name="prospective power design group",
            build_evidence=lambda: self._build_power_evidence(
                specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            ),
        )

    def _build_power_evidence(  # noqa: PLR0912, PLR0915 - one atomic evidence contract
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> list[tuple[Mapping[str, Any], DerivedBacktestEvidence]]:
        if not specs or {str(spec.get("runner_kind")) for spec in specs} != {_POWER_RUNNER}:
            message = "prospective power registry must contain registered effects"
            raise RuntimeError(message)
        parameter_payloads = [self._mapping(spec, "parameters") for spec in specs]
        baseline_arm_id = self._historical_disposition_policy(manifest).baseline_arm_id
        if any(
            parameters.get("cadence_source_arm_id") != baseline_arm_id
            for parameters in parameter_payloads
        ):
            message = "prospective power source differs from the registered baseline arm"
            raise RuntimeError(message)
        if any(
            parameters.get("economic_period_date_transform")
            != "normalized_close_timestamp_minus_one_calendar_day"
            for parameters in parameter_payloads
        ):
            message = "prospective cadence economic-period transform is not frozen"
            raise RuntimeError(message)
        source = self._load_direct_report_evidence(
            all_specs,
            trial_ids=trial_ids,
            manifest=manifest,
            arm_id=baseline_arm_id,
            cost_scenario="expected",
            fold_ids=("full-panel",),
        )
        products = {
            str(dataset["requested_product"])
            for dataset in self._list_of_mappings(self._mapping(manifest, "data"), "datasets")
        }
        if set(source) != {("full-panel", product) for product in products}:
            message = "power cadence source does not exactly cover baseline full-panel assets"
            raise RuntimeError(message)

        outcomes: list[ClosedTradeOutcome] = []
        source_trials: list[dict[str, Any]] = []
        terminal_positions_right_censored = 0
        for (_fold_id, product), (source_spec, trial_id, envelope) in sorted(source.items()):
            if envelope.get("symbol") != product:
                message = f"power source symbol differs from frozen product: {product}"
                raise RuntimeError(message)
            report = self._mapping(envelope, "report")
            terminal = report.get("terminal_position")
            if terminal is not None:
                if not isinstance(terminal, Mapping):
                    message = f"power source terminal position is malformed: {product}"
                    raise TypeError(message)
                terminal_positions_right_censored += 1
            raw_trades = envelope.get("trades")
            if isinstance(raw_trades, (str, bytes)) or not isinstance(raw_trades, Sequence):
                message = f"power source complete trade ledger is malformed: {product}"
                raise TypeError(message)
            if int(report.get("total_trades", -1)) != len(raw_trades):
                message = f"power source complete trade coverage differs: {product}"
                raise RuntimeError(message)
            for index, raw_trade in enumerate(raw_trades):
                trade = self._require_mapping(raw_trade, field=f"{product}.trades[{index}]")
                quantity = float(trade["quantity"])
                entry_fill_price = float(trade["entry_price"])
                denominator = abs(quantity * entry_fill_price)
                if denominator <= 0.0:
                    message = f"power source trade has non-positive entry notional: {product}"
                    raise RuntimeError(message)
                identity = {
                    "source_trial_id": trial_id,
                    "ledger_index": index,
                    "asset": product,
                    "entry_ts": trade["entry_ts"],
                    "exit_ts": trade["exit_ts"],
                    "quantity": quantity,
                    "entry_fill_price": entry_fill_price,
                }
                trade_id = manifest_sha256(identity)
                outcomes.append(
                    ClosedTradeOutcome(
                        trade_id=trade_id,
                        asset=product,
                        entry_ts=parse_utc_datetime(
                            trade["entry_ts"], field=f"{product}.trades[{index}].entry_ts"
                        ),
                        exit_ts=parse_utc_datetime(
                            trade["exit_ts"], field=f"{product}.trades[{index}].exit_ts"
                        ),
                        net_return=float(trade["pnl"]) / denominator,
                    )
                )
            source_trials.append(
                {
                    "dataset_id": source_spec["dataset_id"],
                    "fold_id": source_spec["fold_id"],
                    "product": product,
                    "report_sha256": envelope["report_sha256"],
                    "trial_id": trial_id,
                }
            )

        ordered_outcomes = sorted(
            outcomes,
            key=lambda item: (item.exit_ts, item.entry_ts, item.asset, item.trade_id),
        )
        first_spec = specs[0]
        observation_start = parse_utc_datetime(
            first_spec["evaluation_start"], field="power.evaluation_start"
        )
        observation_end = parse_utc_datetime(
            first_spec["evaluation_end_exclusive"],
            field="power.evaluation_end_exclusive",
        )
        inference = self._mapping(manifest, "inference")
        power_contract = parameter_payloads[0]
        observed = (
            observe_prospective_design_inputs(
                ordered_outcomes,
                observation_start=observation_start,
                observation_end_exclusive=observation_end,
                effective_sample_max_lag=int(inference["effective_sample_size_max_lag_bars"]),
                right_censored_entries=terminal_positions_right_censored,
                cadence_lower_confidence=float(power_contract["input_uncertainty_confidence"]),
                independence_bootstrap_samples=int(
                    power_contract["input_uncertainty_bootstrap_samples"]
                ),
                independence_bootstrap_seed=int(power_contract["input_uncertainty_bootstrap_seed"]),
                independence_bootstrap_block_size=int(
                    power_contract["input_uncertainty_trade_block_size"]
                ),
                independence_lower_quantile=float(power_contract["independence_lower_quantile"]),
            )
            if ordered_outcomes
            else None
        )

        strategy = self._mapping(manifest, "strategy")
        asset_class = str(self._mapping(manifest, "habitat")["asset_class_db"])
        symbol = "+".join(sorted(products))
        if len(symbol) > _DERIVED_SYMBOL_MAX_LENGTH:
            message = "power evidence symbol exceeds database limit"
            raise ValueError(message)
        records: list[tuple[Mapping[str, Any], DerivedBacktestEvidence]] = []
        for spec in sorted(specs, key=lambda item: int(item["sequence"])):
            parameters = self._mapping(spec, "parameters")
            effect = float(parameters["minimum_standardized_effect"])
            design = None
            not_applicable_reason = None
            if observed is None:
                not_applicable_reason = "zero_closed_trades_in_baseline_expected_cost_full_panel"
            else:
                design = search_prospective_duration_from_observed_trades(
                    observed,
                    minimum_standardized_effect=effect,
                    effective_trials=int(parameters["prospective_strategy_family_trials"]),
                    familywise_alpha=float(parameters["familywise_alpha"]),
                    familywise_error_allocation=float(parameters["familywise_error_allocation"]),
                    target_power=float(parameters["minimum_power"]),
                    simulations=int(parameters["simulations"]),
                    seed=int(parameters["seed"]),
                    monte_carlo_confidence=float(parameters["monte_carlo_confidence"]),
                    duration_step_years=float(parameters["duration_step_years"]),
                    maximum_duration_years=float(parameters["maximum_duration_years"]),
                    required_consecutive_passes=int(
                        parameters["minimum_consecutive_powered_durations"]
                    ),
                )
            operationally_impractical = (
                design is None or design.search.minimum_duration_years is None
            )
            start_date, end_date = self._evidence_dates(spec)
            payload = {
                "cadence_source": {
                    "arm_id": baseline_arm_id,
                    "cadence_gate_population": "matured_closed_trade_outcomes",
                    "cost_scenario": "expected",
                    "fold_id": "full-panel",
                    "net_return_formula": "pnl/abs(quantity*entry_fill_price)",
                    "economic_period_date_transform": (
                        "normalized_close_timestamp_minus_one_calendar_day"
                    ),
                    "source_trials": source_trials,
                    "terminal_position_treatment": (
                        "filled_entry_reported_but_right_censored_outcome_excluded_from_"
                        "matured_cadence_and_power"
                    ),
                    "terminal_positions_right_censored": terminal_positions_right_censored,
                },
                "completed_trade_outcomes": [
                    {
                        **item.to_dict(),
                        "economic_entry_period_date": (item.entry_ts - timedelta(days=1))
                        .date()
                        .isoformat(),
                        "economic_exit_period_date": (item.exit_ts - timedelta(days=1))
                        .date()
                        .isoformat(),
                    }
                    for item in ordered_outcomes
                ],
                "evidence_role": spec["evidence_role"],
                "evidence_complete": True,
                "historical_hierarchy_implication": ("RETIRE" if observed is None else None),
                "historical_selection_contaminated": True,
                "minimum_standardized_effect": effect,
                "observed_inputs": observed.to_dict() if observed is not None else None,
                "operationally_impractical": operationally_impractical,
                "primary_duration_operationally_impractical": (
                    operationally_impractical
                    if effect == float(parameters["minimum_standardized_effect_primary"])
                    else None
                ),
                "power_design": design.to_dict() if design is not None else None,
                "protocol_id": manifest["protocol_id"],
                "runner_kind": spec["runner_kind"],
                "status": ("not_applicable_due_to_zero_trades" if design is None else "complete"),
                "not_applicable_reason": not_applicable_reason,
            }
            records.append(
                (
                    spec,
                    DerivedBacktestEvidence(
                        strategy_id=str(strategy["strategy_id"]),
                        strategy_semver=str(strategy["strategy_version"]),
                        experiment_id=experiment_id,
                        evidence_kind="prospective_power_study",
                        symbol=symbol,
                        asset_class=asset_class,
                        timeframe=self._common_dataset_value(manifest, "timeframe"),
                        start_date=start_date,
                        end_date=end_date,
                        payload=payload,
                        metrics=DerivedEvidenceMetrics(total_trades=len(ordered_outcomes)),
                    ),
                )
            )
        return records

    def _build_scoring_ledger_evidence(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> list[tuple[Mapping[str, Any], DerivedBacktestEvidence]]:
        component_specs = sorted(
            (
                spec
                for spec in specs
                if spec.get("runner_kind") == "scoring_binding_ledger_component"
            ),
            key=lambda item: str(item["dataset_id"]),
        )
        pooled_specs = [
            spec for spec in specs if spec.get("runner_kind") == "scoring_binding_ledger_pooled"
        ]
        dataset_count = len(self._list_of_mappings(self._mapping(manifest, "data"), "datasets"))
        if len(component_specs) != dataset_count or len(pooled_specs) != 1:
            message = "scoring ledger registry does not exactly cover all components and pooled"
            raise RuntimeError(message)
        baseline_arm_id = self._historical_disposition_policy(manifest).baseline_arm_id
        if any(
            self._mapping(spec, "parameters").get("source_arm_id") != baseline_arm_id
            for spec in specs
        ):
            message = "scoring ledger source differs from the registered baseline arm"
            raise RuntimeError(message)

        binding_snapshot = self._mapping(manifest, "operational_binding_snapshot")
        binding_rows = self._list_of_mappings(binding_snapshot, "bindings")
        if not binding_rows:
            message = "scoring replay requires at least one frozen exact binding"
            raise RuntimeError(message)
        expected_products = {
            str(
                self._mapping(self._mapping(spec, "parameters"), "component_dataset")[
                    "requested_product"
                ]
            )
            for spec in component_specs
        }
        source_fold_ids = self._scoring_source_fold_ids(specs)
        entries_by_product, source_trial_ids = self._load_scoring_source_ledgers(
            all_specs,
            trial_ids=trial_ids,
            source_fold_ids=source_fold_ids,
            expected_products=expected_products,
            manifest=manifest,
            source_arm_id=baseline_arm_id,
        )
        strategy_section = self._mapping(manifest, "strategy")
        asset_class = str(self._mapping(manifest, "habitat")["asset_class_db"])
        evidence_records: list[tuple[Mapping[str, Any], DerivedBacktestEvidence]] = []
        component_replays: dict[str, list[dict[str, Any]]] = {}
        for spec in component_specs:
            product = str(
                self._mapping(self._mapping(spec, "parameters"), "component_dataset")[
                    "requested_product"
                ]
            )
            entries = entries_by_product[product]
            replay_rows = self._replay_scoring_entries(entries, binding_rows=binding_rows)
            component_replays[product] = replay_rows
            counts = Counter(entry.action for _fold_id, entry in entries)
            start_date, end_date = self._evidence_dates(spec)
            evidence_records.append(
                (
                    spec,
                    DerivedBacktestEvidence(
                        strategy_id=str(strategy_section["strategy_id"]),
                        strategy_semver=str(strategy_section["strategy_version"]),
                        experiment_id=experiment_id,
                        evidence_kind="scoring_binding_ledger_component",
                        symbol=product,
                        asset_class=asset_class,
                        timeframe=self._common_dataset_value(manifest, "timeframe"),
                        start_date=start_date,
                        end_date=end_date,
                        payload=self._scoring_evidence_payload(
                            spec,
                            manifest=manifest,
                            binding_snapshot=binding_snapshot,
                            replay_rows=replay_rows,
                            source_fold_ids=source_fold_ids,
                            source_trial_ids=source_trial_ids,
                        ),
                        metrics=DerivedEvidenceMetrics(
                            total_signals=len(entries),
                            long_signals=counts["long"],
                            short_signals=counts["short"],
                        ),
                    ),
                )
            )

        pooled_entries = sorted(
            (
                (fold_id, evidence)
                for product in sorted(entries_by_product)
                for fold_id, evidence in entries_by_product[product]
            ),
            key=lambda item: (
                item[1].generated_at,
                item[1].symbol,
                item[1].external_signal_id,
            ),
        )
        pooled_ids = [entry.external_signal_id for _fold_id, entry in pooled_entries]
        if len(pooled_ids) != len(set(pooled_ids)):
            message = "pooled scoring source contains duplicate canonical external signal IDs"
            raise RuntimeError(message)
        pooled_spec = pooled_specs[0]
        pooled_replay = self._replay_scoring_entries(
            pooled_entries,
            binding_rows=binding_rows,
        )
        counts = Counter(entry.action for _fold_id, entry in pooled_entries)
        symbol = "+".join(sorted(component_replays))
        if len(symbol) > _DERIVED_SYMBOL_MAX_LENGTH:
            message = "pooled scoring evidence symbol exceeds database limit"
            raise ValueError(message)
        start_date, end_date = self._evidence_dates(pooled_spec)
        evidence_records.append(
            (
                pooled_spec,
                DerivedBacktestEvidence(
                    strategy_id=str(strategy_section["strategy_id"]),
                    strategy_semver=str(strategy_section["strategy_version"]),
                    experiment_id=experiment_id,
                    evidence_kind="scoring_binding_ledger_pooled",
                    symbol=symbol,
                    asset_class=asset_class,
                    timeframe=self._common_dataset_value(manifest, "timeframe"),
                    start_date=start_date,
                    end_date=end_date,
                    payload=self._scoring_evidence_payload(
                        pooled_spec,
                        manifest=manifest,
                        binding_snapshot=binding_snapshot,
                        replay_rows=pooled_replay,
                        source_fold_ids=source_fold_ids,
                        source_trial_ids=source_trial_ids,
                    ),
                    metrics=DerivedEvidenceMetrics(
                        total_signals=len(pooled_entries),
                        long_signals=counts["long"],
                        short_signals=counts["short"],
                    ),
                ),
            )
        )
        return evidence_records

    def _scoring_source_fold_ids(
        self,
        specs: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        observed: list[list[str]] = []
        for spec in specs:
            parameters = self._mapping(spec, "parameters")
            raw_fold_ids = parameters.get("source_fold_ids")
            if isinstance(raw_fold_ids, (str, bytes)) or not isinstance(raw_fold_ids, Sequence):
                message = "scoring source_fold_ids must be a JSON array"
                raise TypeError(message)
            normalized = [str(fold_id) for fold_id in raw_fold_ids]
            if not normalized or len(normalized) != len(set(normalized)):
                message = "scoring source_fold_ids must be non-empty and unique"
                raise ValueError(message)
            observed.append(normalized)
        first = observed[0]
        if any(item != first for item in observed[1:]):
            message = "scoring group components disagree on source folds"
            raise RuntimeError(message)
        return first

    def _load_scoring_source_ledgers(
        self,
        all_specs: Sequence[Mapping[str, Any]],
        *,
        trial_ids: Mapping[int, str],
        source_fold_ids: Sequence[str],
        expected_products: set[str],
        manifest: Mapping[str, Any],
        source_arm_id: str,
    ) -> tuple[dict[str, list[tuple[str, RawSignalEvidence]]], list[str]]:
        source = self._load_direct_report_evidence(
            all_specs,
            trial_ids=trial_ids,
            manifest=manifest,
            arm_id=source_arm_id,
            cost_scenario="expected",
            fold_ids=source_fold_ids,
        )
        if {product for _fold_id, product in source} != expected_products:
            message = "scoring source does not exactly cover baseline expected-cost OOS reports"
            raise RuntimeError(message)
        entries_by_product: dict[str, list[tuple[str, RawSignalEvidence]]] = {
            product: [] for product in sorted(expected_products)
        }
        source_trial_ids: list[str] = []
        seen_ids: set[str] = set()
        for fold_id in source_fold_ids:
            for product in sorted(expected_products):
                _spec, trial_id, envelope = source[(fold_id, product)]
                source_trial_ids.append(trial_id)
                report = self._mapping(envelope, "report")
                raw_ledger = report.get("raw_signal_ledger")
                if isinstance(raw_ledger, (str, bytes)) or not isinstance(raw_ledger, Sequence):
                    message = f"baseline raw signal ledger is invalid for {fold_id}/{product}"
                    raise TypeError(message)
                if int(report.get("signals_emitted", -1)) != len(raw_ledger):
                    message = f"baseline raw signal coverage differs for {fold_id}/{product}"
                    raise RuntimeError(message)
                for index, raw_entry in enumerate(raw_ledger):
                    evidence = RawSignalEvidence.from_dict(
                        self._require_mapping(
                            raw_entry,
                            field=f"baseline.{fold_id}.{product}.raw_signal_ledger[{index}]",
                        )
                    )
                    if evidence.symbol != product:
                        message = f"baseline raw signal symbol differs for {fold_id}/{product}"
                        raise RuntimeError(message)
                    if evidence.external_signal_id in seen_ids:
                        message = (
                            "duplicate canonical external signal ID across baseline source "
                            f"ledgers: {evidence.external_signal_id}"
                        )
                        raise RuntimeError(message)
                    seen_ids.add(evidence.external_signal_id)
                    entries_by_product[product].append((fold_id, evidence))
        return entries_by_product, source_trial_ids

    def _load_direct_report_evidence(
        self,
        all_specs: Sequence[Mapping[str, Any]],
        *,
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
        arm_id: str,
        cost_scenario: str,
        fold_ids: Sequence[str],
    ) -> dict[tuple[str, str], tuple[Mapping[str, Any], str, dict[str, Any]]]:
        """Load an exact direct-report panel through its immutable hash boundary."""

        normalized_folds = tuple(str(fold_id) for fold_id in fold_ids)
        if not normalized_folds or len(normalized_folds) != len(set(normalized_folds)):
            message = "direct report source folds must be non-empty and unique"
            raise ValueError(message)
        datasets = {
            str(dataset["dataset_id"]): str(dataset["requested_product"])
            for dataset in self._list_of_mappings(self._mapping(manifest, "data"), "datasets")
        }
        specs = [
            spec
            for spec in all_specs
            if spec.get("runner_kind") == "production_core_direct"
            and spec.get("arm_id") == arm_id
            and spec.get("cost_scenario") == cost_scenario
            and str(spec.get("fold_id")) in set(normalized_folds)
            and str(spec.get("dataset_id")) in datasets
        ]
        by_key = {(str(spec["fold_id"]), datasets[str(spec["dataset_id"])]): spec for spec in specs}
        expected_keys = {
            (fold_id, product) for fold_id in normalized_folds for product in datasets.values()
        }
        if len(by_key) != len(specs) or set(by_key) != expected_keys:
            message = (
                f"direct report source does not exactly cover {arm_id}/{cost_scenario}/"
                f"{','.join(normalized_folds)}"
            )
            raise RuntimeError(message)

        loaded: dict[tuple[str, str], tuple[Mapping[str, Any], str, dict[str, Any]]] = {}
        for key in sorted(by_key):
            spec = by_key[key]
            trial_id = trial_ids[int(spec["sequence"])]
            envelope = self._result_store.load_verified_report_evidence(trial_id)
            context = self._mapping(envelope, "meta_context")
            report = self._mapping(envelope, "report")
            if (
                context.get("arm_id") != arm_id
                or context.get("runner_kind") != "production_core_direct"
                or envelope.get("trial_id") != trial_id
                or envelope.get("symbol") != key[1]
                or report.get("cost_scenario") != cost_scenario
                or report.get("symbol") != key[1]
            ):
                message = (
                    "direct report source provenance differs from its registered identity: "
                    f"{key[0]}/{key[1]}"
                )
                raise RuntimeError(message)
            loaded[key] = (spec, trial_id, envelope)
        return loaded

    def _execute_cscv_group(
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
            group_name="registered CSCV/PBO diagnostic",
            build_evidence=lambda: self._build_cscv_evidence(
                specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            ),
        )

    def _build_cscv_evidence(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> list[tuple[Mapping[str, Any], DerivedBacktestEvidence]]:
        if len(specs) != 1 or specs[0].get("runner_kind") != "registered_cscv_pbo":
            message = "registered CSCV/PBO requires exactly one trial"
            raise RuntimeError(message)
        spec = specs[0]
        parameters = self._mapping(spec, "parameters")
        pbo = self._mapping(parameters, "pbo")
        raw_candidates = pbo.get("registered_candidate_order")
        raw_years = parameters.get("calendar_year_groups")
        if (
            isinstance(raw_candidates, (str, bytes))
            or not isinstance(raw_candidates, Sequence)
            or isinstance(raw_years, (str, bytes))
            or not isinstance(raw_years, Sequence)
        ):
            message = "registered CSCV candidates and years must be JSON arrays"
            raise TypeError(message)
        candidates = tuple(str(candidate) for candidate in raw_candidates)
        years = tuple(int(year) for year in raw_years)
        products = self._registered_component_products(parameters)
        direct = self._direct_report_spec_index(all_specs, manifest=manifest)
        reports = tuple(
            self._standard_report_from_spec(
                direct[(candidate, product, "full-panel", "expected")],
                trial_ids=trial_ids,
                manifest=manifest,
            )
            for candidate in candidates
            for product in products
        )
        result = build_registered_report_cscv_pbo(
            reports,
            registered_candidate_order=candidates,
            registered_asset_order=products,
            calendar_years=years,
            registered_economic_dates=tuple(
                (timestamp - timedelta(days=1)).date()
                for timestamp in self._expected_daily_timestamps(
                    spec,
                    manifest=manifest,
                )
            ),
            annualization_factor=float(
                self._mapping(manifest, "inference")["annualization_factor"]
            ),
            expected_cost_scenario="expected",
            economic_period_date_rule=str(parameters["economic_period_date_rule"]),
        )
        strategy = self._mapping(manifest, "strategy")
        symbol = self._derived_symbol(products)
        start_date, end_date = self._evidence_dates(spec)
        return [
            (
                spec,
                DerivedBacktestEvidence(
                    strategy_id=str(strategy["strategy_id"]),
                    strategy_semver=str(strategy["strategy_version"]),
                    experiment_id=experiment_id,
                    evidence_kind="registered_cscv_pbo",
                    symbol=symbol,
                    asset_class=str(self._mapping(manifest, "habitat")["asset_class_db"]),
                    timeframe=self._common_dataset_value(manifest, "timeframe"),
                    start_date=start_date,
                    end_date=end_date,
                    payload={
                        "cscv_pbo": result.to_dict(),
                        "evidence_role": spec["evidence_role"],
                        "historical_selection_contaminated": True,
                        "protocol_id": manifest["protocol_id"],
                        "runner_kind": spec["runner_kind"],
                        "source_attestation": self._report_source_attestation(reports),
                    },
                    metrics=DerivedEvidenceMetrics(),
                ),
            )
        ]

    def _direct_report_spec_index(
        self,
        all_specs: Sequence[Mapping[str, Any]],
        *,
        manifest: Mapping[str, Any],
    ) -> dict[tuple[str, str, str, str], Mapping[str, Any]]:
        products_by_dataset = {
            str(dataset["dataset_id"]): str(dataset["requested_product"])
            for dataset in self._list_of_mappings(self._mapping(manifest, "data"), "datasets")
        }
        rows = [
            spec
            for spec in all_specs
            if spec.get("runner_kind") == "production_core_direct"
            and str(spec.get("dataset_id")) in products_by_dataset
        ]
        indexed = {
            (
                str(spec["arm_id"]),
                products_by_dataset[str(spec["dataset_id"])],
                str(spec["fold_id"]),
                str(spec["cost_scenario"]),
            ): spec
            for spec in rows
        }
        if len(indexed) != len(rows):
            message = "direct report registry identities are not unique"
            raise RuntimeError(message)
        return indexed

    def _standard_report_from_spec(
        self,
        spec: Mapping[str, Any],
        *,
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> StandardReportEvidence:
        if spec.get("runner_kind") != "production_core_direct":
            message = "standard report diagnostics require production_core_direct evidence"
            raise RuntimeError(message)
        trial_id = trial_ids[int(spec["sequence"])]
        envelope = self._result_store.load_verified_report_evidence(trial_id)
        terminal_exit_cost, terminal_policy_id = self._terminal_exit_adjustment(
            envelope,
            fold_id=str(spec["fold_id"]),
            manifest=manifest,
        )
        return standard_report_evidence_from_mapping(
            envelope,
            arm_id=str(spec["arm_id"]),
            fold_id=str(spec["fold_id"]),
            cost_scenario=str(spec["cost_scenario"]),
            terminal_exit_cost=terminal_exit_cost,
            terminal_exit_policy_id=terminal_policy_id,
        )

    def _terminal_exit_adjustment(
        self,
        envelope: Mapping[str, Any],
        *,
        fold_id: str,
        manifest: Mapping[str, Any],
    ) -> tuple[float, str | None]:
        """Calculate the frozen conservative OOS liquidation adjustment."""

        report = self._mapping(envelope, "report")
        if report.get("terminal_position") is None or fold_id == "full-panel":
            return 0.0, None
        terminal = self._mapping(report, "terminal_position")
        product = self._nonempty_string(envelope.get("symbol"), field="report.symbol")
        side = self._nonempty_string(terminal.get("side"), field="terminal.side")
        if side not in {"long", "short"}:
            message = "terminal position side must be long or short"
            raise ValueError(message)
        quantity = float(terminal["quantity"])
        mark_price = float(terminal["mark_price"])
        transaction_side = -1 if side == "long" else 1
        cost_model = self._execution_cost_model(
            manifest,
            "stressed",
            product,
            latency_bars=0,
        )
        execution_price = adverse_execution_price(
            mark_price,
            transaction_side=transaction_side,
            cost_model=cost_model,
        )
        terminal_exit_cost = execution_fill_costs(
            reference_price=mark_price,
            execution_price=execution_price,
            quantity=quantity,
            cost_model=cost_model,
        ).total
        if not math.isfinite(terminal_exit_cost) or terminal_exit_cost <= 0.0:
            message = "registered OOS terminal liquidation cost must be finite and positive"
            raise RuntimeError(message)
        return (
            terminal_exit_cost,
            f"terminal-liquidation:stressed:{product}:final-mark-v1",
        )

    def _registered_component_products(
        self,
        parameters: Mapping[str, Any],
    ) -> tuple[str, ...]:
        components = self._list_of_mappings(parameters, "component_datasets")
        products = tuple(
            self._nonempty_string(
                component.get("requested_product"),
                field="component_datasets.requested_product",
            )
            for component in components
        )
        if not products or len(products) != len(set(products)):
            message = "component product identities must be non-empty and unique"
            raise ValueError(message)
        return products

    @classmethod
    def _report_source_attestation(
        cls,
        reports: Sequence[StandardReportEvidence],
    ) -> dict[str, Any]:
        return cls._normalized_source_attestation(
            report_sources=[
                {
                    "report_sha256": report.report_sha256,
                    "trial_id": report.trial_id,
                }
                for report in reports
            ]
        )

    @staticmethod
    def _derived_symbol(products: Sequence[str]) -> str:
        symbol = "+".join(products)
        if not symbol or len(symbol) > _DERIVED_SYMBOL_MAX_LENGTH:
            message = "derived evidence symbol is blank or exceeds database limit"
            raise ValueError(message)
        return symbol

    def _execute_robustness_group(
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
            group_name="fixed-baseline robustness diagnostic",
            build_evidence=lambda: self._build_robustness_evidence(
                specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            ),
        )

    def _build_robustness_evidence(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> list[tuple[Mapping[str, Any], DerivedBacktestEvidence]]:
        if len(specs) != 1 or specs[0].get("runner_kind") != "robustness_concentration_inference":
            message = "fixed-baseline robustness requires exactly one registered trial"
            raise RuntimeError(message)
        spec = specs[0]
        inputs = self._build_derived_diagnostic_inputs(
            spec,
            all_specs=all_specs,
            trial_ids=trial_ids,
            manifest=manifest,
        )
        reports = self._robustness_source_reports(
            inputs.config,
            all_specs=all_specs,
            trial_ids=trial_ids,
            manifest=manifest,
        )
        result = build_fixed_baseline_robustness_evidence(
            reports,
            config=inputs.config,
            expected_cost_benchmarks=inputs.benchmarks,
            economic_regimes=inputs.regimes,
            sharpe_dispersion=inputs.sharpe_dispersion,
            adaptive_selector=inputs.adaptive_selector,
        )
        strategy = self._mapping(manifest, "strategy")
        start_date, end_date = self._evidence_dates(spec)
        source_attestation = self._merge_source_attestations(
            inputs.source_attestation,
            self._report_source_attestation(reports),
        )
        return [
            (
                spec,
                DerivedBacktestEvidence(
                    strategy_id=str(strategy["strategy_id"]),
                    strategy_semver=str(strategy["strategy_version"]),
                    experiment_id=experiment_id,
                    evidence_kind="robustness_concentration_inference",
                    symbol=self._derived_symbol(inputs.config.assets),
                    asset_class=str(self._mapping(manifest, "habitat")["asset_class_db"]),
                    timeframe=self._common_dataset_value(manifest, "timeframe"),
                    start_date=start_date,
                    end_date=end_date,
                    payload={
                        **result.to_dict(),
                        "evidence_role": spec["evidence_role"],
                        "historical_selection_contaminated": True,
                        "protocol_id": manifest["protocol_id"],
                        "regime_calibration": inputs.regime_calibration,
                        "runner_kind": spec["runner_kind"],
                        "source_attestation": source_attestation,
                        "trial_sharpe_dispersion": {
                            "upstream_attempted_trials": (
                                inputs.sharpe_dispersion.upstream_attempted_trials
                            ),
                            "upstream_finite_count": len(
                                inputs.sharpe_dispersion.upstream_finite_sharpes
                            ),
                            "current_candidate_sharpes": [
                                [candidate, value]
                                for candidate, value in (
                                    inputs.sharpe_dispersion.current_candidate_sharpes
                                )
                            ],
                            "source": inputs.sharpe_dispersion.source,
                        },
                    },
                    metrics=DerivedEvidenceMetrics(
                        total_return_pct=result.expected_total_return_pct,
                        sharpe_ratio=result.expected_sharpe_ratio,
                        sortino_ratio=result.expected_sortino_ratio,
                        max_drawdown_pct=result.expected_max_drawdown_pct,
                    ),
                ),
            )
        ]

    def _execute_redesign_group(
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
            group_name="registered redesign candidate diagnostic",
            build_evidence=lambda: self._build_redesign_evidence(
                specs,
                all_specs=all_specs,
                trial_ids=trial_ids,
                experiment_id=experiment_id,
                manifest=manifest,
            ),
        )

    def _build_redesign_evidence(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        experiment_id: int,
        manifest: Mapping[str, Any],
    ) -> list[tuple[Mapping[str, Any], DerivedBacktestEvidence]]:
        if len(specs) != 1 or specs[0].get("runner_kind") != _REDESIGN_RUNNER:
            message = "registered redesign evidence requires exactly one trial"
            raise RuntimeError(message)
        spec = specs[0]
        robustness_specs = [
            candidate
            for candidate in all_specs
            if candidate.get("runner_kind") == "robustness_concentration_inference"
        ]
        if len(robustness_specs) != 1:
            message = "redesign evidence requires one frozen robustness contract"
            raise RuntimeError(message)
        inputs = self._build_derived_diagnostic_inputs(
            robustness_specs[0],
            all_specs=all_specs,
            trial_ids=trial_ids,
            manifest=manifest,
        )
        reports = self._redesign_source_reports(
            inputs.config,
            all_specs=all_specs,
            trial_ids=trial_ids,
            manifest=manifest,
        )
        result = build_registered_redesign_candidate_evidence(
            reports,
            config=inputs.config,
            expected_cost_benchmarks=inputs.benchmarks,
            economic_regimes=inputs.regimes,
        )
        strategy = self._mapping(manifest, "strategy")
        start_date, end_date = self._evidence_dates(spec)
        source_attestation = self._merge_source_attestations(
            inputs.source_attestation,
            self._report_source_attestation(reports),
        )
        return [
            (
                spec,
                DerivedBacktestEvidence(
                    strategy_id=str(strategy["strategy_id"]),
                    strategy_semver=str(strategy["strategy_version"]),
                    experiment_id=experiment_id,
                    evidence_kind=_REDESIGN_RUNNER,
                    symbol=self._derived_symbol(inputs.config.assets),
                    asset_class=str(self._mapping(manifest, "habitat")["asset_class_db"]),
                    timeframe=self._common_dataset_value(manifest, "timeframe"),
                    start_date=start_date,
                    end_date=end_date,
                    payload={
                        "evidence_role": spec["evidence_role"],
                        "historical_selection_contaminated": True,
                        "protocol_id": manifest["protocol_id"],
                        "redesign_candidates": result.to_dict(),
                        "regime_calibration": inputs.regime_calibration,
                        "runner_kind": spec["runner_kind"],
                        "source_attestation": source_attestation,
                    },
                    metrics=DerivedEvidenceMetrics(),
                ),
            )
        ]
