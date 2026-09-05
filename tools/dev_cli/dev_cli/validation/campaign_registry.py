"""Manifest construction and immutable trial registration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dev_cli.validation.benchmarks import (
    BenchmarkKind,
)
from dev_cli.validation.campaign_composition import CampaignMixinContract
from dev_cli.validation.campaign_contracts import (
    _BENCHMARK_ARM_BY_PROTOCOL_NAME,
    _BENCHMARK_KIND_BY_PROTOCOL_NAME,
    _CONDITIONAL_SIZING_GROUP_RUNNERS,
    _EXECUTION_HELPERS,
    _HISTORICAL_DISPOSITION_ARM,
    _HISTORICAL_DISPOSITION_RUNNER,
    _HISTORICAL_SOURCE_IDS,
    _MOMENTUM_BENCHMARK_WARMUP,
    _PBO_ARM,
    _REDESIGN_RUNNER,
    _ROBUSTNESS_ARM,
    _SCORING_LEDGER_ARM,
    _SHA256_RE,
    _SMA_BENCHMARK_WARMUP,
)
from dev_cli.validation.correctness import (
    CorrectnessFindingClass,
)
from dev_cli.validation.disposition import (
    HistoricalDispositionPolicy,
    HistoricalEvidenceKind,
    HistoricalEvidenceRequirement,
)
from dev_cli.validation.evidence import parse_utc_datetime
from dev_cli.validation.persistence.backtest_manifest_store import (
    canonical_manifest_bytes,
    manifest_sha256,
)
from dev_cli.validation.persistence.backtest_selection_ledger import (
    load_upstream_selection_ledger,
)
from dev_cli.validation.persistence.backtest_trial_store import (
    BacktestTrialStatus,
)
from lib_application.db.models import (
    UserStrategyBinding,
    UserStrategyConfig,
)


class CampaignRegistryMixin(CampaignMixinContract):
    def _build_manifest(
        self,
        strategy_path: Path,
        *,
        protocol: dict[str, Any],
        runtime_config: dict[str, Any],
        execution_environment: Mapping[str, Any] | None,
        upstream_selection_ledger: Path | None = None,
        execution_cost_measurement: Path | None = None,
        data_parity_attestation: Path | None = None,
        correctness_attestation: Path | None = None,
    ) -> dict[str, Any]:
        self._validate_protocol(protocol)
        strategy_section = self._mapping(protocol, "strategy")
        operational_binding_snapshot = self._operational_binding_snapshot(strategy_section)
        missing_inputs = []
        if upstream_selection_ledger is None:
            missing_inputs.append("upstream_selection_ledger")
        if execution_cost_measurement is None:
            missing_inputs.append("execution_cost_measurement")
        if data_parity_attestation is None:
            missing_inputs.append("data_parity_attestation")
        if correctness_attestation is None:
            missing_inputs.append("correctness_attestation")
        if missing_inputs:
            message = "; ".join(
                f"{field} is required before freezing a new manifest" for field in missing_inputs
            )
            raise ValueError(message)
        assert upstream_selection_ledger is not None
        assert execution_cost_measurement is not None
        assert data_parity_attestation is not None
        assert correctness_attestation is not None
        multiple_testing = self._mapping(protocol, "multiple_testing")
        upstream_ledger_payload = load_upstream_selection_ledger(
            upstream_selection_ledger,
            self._mapping(multiple_testing, "upstream_selection_ledger_contract"),
        )
        cost_measurement_payload = self._load_json_object(execution_cost_measurement)
        self._validate_execution_cost_measurement(
            cost_measurement_payload,
            protocol=protocol,
        )
        data_parity_payload = self._load_json_object(data_parity_attestation)
        self._validate_data_parity_attestation(
            data_parity_payload,
            protocol=protocol,
        )
        correctness_attestation_payload = self._load_json_object(correctness_attestation)
        correctness_attestation_decision = self._validate_correctness_attestation(
            correctness_attestation_payload,
            protocol=protocol,
        )
        data = self._mapping(protocol, "data")
        start = parse_utc_datetime(data.get("warmup_start"), field="data.warmup_start")
        end = parse_utc_datetime(
            data.get("source_candle_evaluation_end_exclusive"),
            field="data.source_candle_evaluation_end_exclusive",
        )
        source = self._nonempty_string(data.get("source"), field="data.source")
        timeframe = self._nonempty_string(data.get("timeframe"), field="data.timeframe")
        runtime_parameters = self._mapping(runtime_config, "parameters")
        asset_class = self._nonempty_string(
            runtime_parameters.get("asset_class"),
            field="parameters.asset_class",
        )
        minimum_coverage = float(data.get("minimum_daily_coverage", 1.0))
        datasets: list[dict[str, Any]] = []
        for product in self._list_of_mappings(data, "primary_products"):
            requested = self._nonempty_string(
                product.get("requested_product"), field="requested_product"
            )
            canonical = self._nonempty_string(
                product.get("expected_canonical_book"), field="expected_canonical_book"
            )
            metadata_snapshot = self._fetch_and_verify_product_metadata(product)
            instr_id = self._price_service.resolve_instrument(
                requested,
                asset_class=asset_class,
            )
            if instr_id is None:
                message = f"No registered {asset_class} instrument resolves {requested}"
                raise ValueError(message)
            _, audit = self._historical_prices.load_validated_bars(
                instr_id,
                start=start,
                end=end,
                timeframe=timeframe,
                source=source,
                symbol=requested,
                minimum_coverage=minimum_coverage,
                metadata={
                    "asset_class": asset_class,
                    "canonical_product": canonical,
                    "requested_product": requested,
                },
            )
            if int(product["dataset_rows"]) != audit.observed_rows:
                message = f"Declared dataset row count differs from database for {requested}"
                raise ValueError(message)
            if str(product["dataset_sha256"]) != audit.ohlcv_sha256:
                message = f"Declared dataset digest differs from database for {requested}"
                raise ValueError(message)
            datasets.append(
                {
                    "dataset_id": (f"{requested}:{source}:{timeframe}:{start.date()}:{end.date()}"),
                    "instrument_id": instr_id,
                    "requested_product": requested,
                    "canonical_product": canonical,
                    "metadata_snapshot": metadata_snapshot,
                    "source": source,
                    "timeframe": timeframe,
                    "asset_class": asset_class,
                    "start": start.isoformat(),
                    "end_exclusive": end.isoformat(),
                    "minimum_coverage": minimum_coverage,
                    "audit": audit.to_dict(),
                }
            )

        trial_registry = self._build_trial_registry(protocol, datasets)
        core_path = strategy_path / "core.py"
        config_path = strategy_path / "config.json"
        protocol_path = strategy_path / "validation_protocol.json"
        return {
            "manifest_schema_version": self.MANIFEST_SCHEMA_VERSION,
            "status": "frozen",
            "protocol_id": self._nonempty_string(protocol.get("protocol_id"), field="protocol_id"),
            "protocol_sha256": manifest_sha256(protocol),
            "strategy": {
                "strategy_id": strategy_section["strategy_id"],
                "strategy_version": strategy_section["strategy_version"],
                "core_class": strategy_section["core_class"],
                "core_path": self._relative(core_path),
                "core_sha256": self._file_sha256(core_path),
                "runtime_config_path": self._relative(config_path),
                "runtime_config_sha256": self._file_sha256(config_path),
                "protocol_path": self._relative(protocol_path),
            },
            "data": {"datasets": datasets},
            "data_parity_contract": data["minute_parity"],
            "data_parity_attestation": data_parity_payload,
            "correctness_attestation_contract": protocol["correctness_attestation"],
            "correctness_attestation": correctness_attestation_payload,
            "correctness_attestation_decision": correctness_attestation_decision,
            "cost_measurement": protocol["cost_measurement"],
            "execution_cost_measurement": cost_measurement_payload,
            "cost_scenarios": protocol["cost_scenarios"],
            "execution_policy": protocol["execution_policy"],
            "validation_design": protocol["validation_design"],
            "evidence_boundary": protocol["evidence_boundary"],
            "multiple_testing": protocol["multiple_testing"],
            "inference": protocol["inference"],
            "upstream_selection_ledger": upstream_ledger_payload,
            "historical_disposition_gates": protocol["historical_disposition_gates"],
            "historical_disposition_policy": protocol["historical_disposition_policy"],
            "allowed_dispositions": protocol["allowed_dispositions"],
            "promotion": protocol["promotion"],
            "portfolio_assumptions": protocol["portfolio_assumptions"],
            "habitat": {
                **protocol["habitat"],
                "asset_class_db": asset_class,
            },
            "environment": self._environment_manifest(
                strategy_path,
                execution_environment=execution_environment,
            ),
            "operational_binding_snapshot": operational_binding_snapshot,
            "trial_registry": trial_registry,
        }

    def _operational_binding_snapshot(
        self,
        strategy: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Freeze exact non-secret binding and entry-guard provenance.

        Scoring thresholds are intentionally excluded because this campaign does
        not evaluate them.  The two entry-safety flags are the only parameter
        values retained from ``UserStrategyConfig``; arbitrary user parameters
        and credentials never cross the manifest boundary.
        """

        strategy_id = self._nonempty_string(
            strategy.get("strategy_id"),
            field="strategy.strategy_id",
        )
        expectation_fields = {
            "paper_binding_expected_active": "active",
            "paper_binding_expected_autopilot": "autopilot",
            "paper_strategy_config_expected_active": "strategy_config_active",
            "require_stop_loss": "require_stop_loss",
            "require_explicit_scoring_inputs": "require_explicit_scoring_inputs",
        }
        expectations: dict[str, bool] = {}
        if strategy.get("operational_state_contract_version") == 1:
            for protocol_field, snapshot_field in expectation_fields.items():
                value = strategy[protocol_field]
                expectations[snapshot_field] = value

        with Session(self._engine) as session:
            bindings = list(
                session.execute(
                    select(UserStrategyBinding)
                    .where(UserStrategyBinding.strategy_id == strategy_id)
                    .order_by(UserStrategyBinding.binding_id)
                ).scalars()
            )
            configs = list(
                session.execute(
                    select(UserStrategyConfig)
                    .where(UserStrategyConfig.strategy_id == strategy_id)
                    .order_by(UserStrategyConfig.user_id, UserStrategyConfig.config_id)
                ).scalars()
            )
        if not bindings:
            message = f"no exact operational binding exists for {strategy_id}"
            raise ValueError(message)
        configs_by_user = {config.user_id: config for config in configs}
        if len(configs_by_user) != len(configs):
            message = f"duplicate exact user strategy configs exist for {strategy_id}"
            raise ValueError(message)
        if expectations.get("strategy_config_active") is False and any(
            bool(config.is_active) for config in configs
        ):
            message = (
                "operational binding state differs from the protocol expectation: "
                "an exact user strategy config remains active "
                "field=strategy_config_active expected=False"
            )
            raise ValueError(message)

        rows: list[dict[str, Any]] = []
        for binding in bindings:
            config = configs_by_user.get(binding.user_id)
            if config is None:
                message = (
                    "operational binding has no exact user strategy config: "
                    f"binding_id={binding.binding_id}"
                )
                raise ValueError(message)
            parameters = config.parameters
            if not isinstance(parameters, Mapping):
                message = (
                    "operational user strategy config parameters must be a JSON object: "
                    f"config_id={config.config_id}"
                )
                raise TypeError(message)
            guards: dict[str, bool] = {}
            for flag in ("require_stop_loss", "require_explicit_scoring_inputs"):
                value = parameters.get(flag)
                if not isinstance(value, bool):
                    message = (
                        f"operational user strategy config {flag} must be an explicit bool: "
                        f"config_id={config.config_id}"
                    )
                    raise TypeError(message)
                guards[flag] = value
            raw_modes = binding.execution_modes_allowed
            if (
                isinstance(raw_modes, (str, bytes))
                or not isinstance(raw_modes, Sequence)
                or any(not isinstance(mode, str) or not mode.strip() for mode in raw_modes)
            ):
                message = (
                    "operational binding execution_modes_allowed must be a list of strings: "
                    f"binding_id={binding.binding_id}"
                )
                raise TypeError(message)
            row = {
                "binding_id": str(binding.binding_id),
                "strategy_config_id": str(config.config_id),
                "strategy_id": strategy_id,
                "active": bool(binding.is_active),
                "autopilot": bool(binding.autopilot),
                "approval_mode": ("autopilot" if bool(binding.autopilot) else "manual_approval"),
                "execution_modes_allowed": sorted(set(raw_modes)),
                "preferred_mode": binding.preferred_mode,
                "strategy_config_active": bool(config.is_active),
                "execution_mode": config.execution_mode,
                **guards,
            }
            for field, expected in expectations.items():
                if row[field] != expected:
                    message = (
                        "operational binding state differs from the protocol expectation: "
                        f"binding_id={binding.binding_id} field={field} "
                        f"expected={expected} observed={row[field]}"
                    )
                    raise ValueError(message)
            rows.append(row)
        return {
            "schema_version": "1.0",
            "thresholds_included": False,
            "thresholds_evaluated": False,
            "bindings": rows,
        }

    def _build_trial_registry(
        self,
        protocol: Mapping[str, Any],
        datasets: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        baseline = dict(self._mapping(protocol, "baseline_parameters"))
        baseline_arm_id = self._historical_disposition_policy(protocol).baseline_arm_id
        arms = self._list_of_mappings(protocol, "signal_rule_arms")
        scenarios = sorted(self._mapping(protocol, "cost_scenarios"))
        windows = self._registry_windows(protocol)
        oos_windows = [window for window in windows if window["fold_id"] != "full-panel"]
        ordered_datasets = sorted(datasets, key=lambda item: str(item["dataset_id"]))
        components = self._registry_dataset_components(ordered_datasets)
        expected_products = {
            self._nonempty_string(product.get("requested_product"), field="requested_product")
            for product in self._list_of_mappings(
                self._mapping(protocol, "data"), "primary_products"
            )
        }
        if {component["requested_product"] for component in components} != expected_products:
            message = "trial registry datasets must exactly cover the primary products"
            raise ValueError(message)
        portfolio_dataset_id = self._derived_portfolio_dataset_id(components)
        portfolio_fields = {
            "dataset_id": portfolio_dataset_id,
            "component_datasets": components,
        }
        trials: list[dict[str, Any]] = []

        # Direct production-core panel: every registered arm across the complete
        # product, cost, and chronological-window design.
        for arm in arms:
            arm_id = self._nonempty_string(arm.get("id"), field="signal_rule_arms.id")
            overrides = dict(self._mapping(arm, "overrides"))
            parameters = {**baseline, **overrides}
            for dataset in ordered_datasets:
                for scenario in scenarios:
                    for window in windows:
                        self._append_registered_trial(
                            trials,
                            {
                                "arm_id": arm_id,
                                "trial_family": (
                                    "baseline" if arm_id == baseline_arm_id else "one_factor"
                                ),
                                "runner_kind": "production_core_direct",
                                "fold_id": window["fold_id"],
                                "cost_scenario": scenario,
                                "dataset_id": dataset["dataset_id"],
                                "parameters": parameters,
                                "evaluation_start": window["evaluation_start"],
                                "evaluation_end_exclusive": window["evaluation_end_exclusive"],
                                "evidence_role": window["evidence_role"],
                            },
                        )

        # The selector is one joint portfolio trial per fold, never duplicated per asset.
        primary = self._mapping(self._mapping(protocol, "selection"), "primary_trial_family")
        primary_arm = self._nonempty_string(primary.get("arm_id"), field="primary.arm_id")
        primary_family = self._nonempty_string(primary.get("family"), field="primary.family")
        primary_cost = self._nonempty_string(
            primary.get("cost_scenario"), field="primary.cost_scenario"
        )
        selector_parameters = {
            **self._primary_selector_parameters(protocol, primary),
            "component_datasets": components,
        }
        folds = self._list_of_mappings(protocol, "folds")
        for fold in folds:
            fold_id = f"oos-{int(fold['oos_year'])}"
            self._append_registered_trial(
                trials,
                {
                    "arm_id": primary_arm,
                    "trial_family": primary_family,
                    "runner_kind": "anchored_joint_asset_selector",
                    "selection_group_id": f"{primary_arm}:{fold_id}",
                    "fold_id": fold_id,
                    "cost_scenario": primary_cost,
                    **portfolio_fields,
                    "parameters": selector_parameters,
                    "training_end_exclusive": fold["train_end_exclusive"],
                    "evaluation_start": fold["test_start"],
                    "evaluation_end_exclusive": fold["test_end_exclusive"],
                    "evidence_role": "primary_joint_asset_purged_oos",
                },
            )
        self._append_registered_trial(
            trials,
            {
                "arm_id": primary_arm,
                "trial_family": primary_family,
                "runner_kind": "pooled_oos_primary_aggregate",
                "selection_group_id": f"{primary_arm}:pooled-oos",
                "fold_id": "pooled-oos",
                "cost_scenario": primary_cost,
                **portfolio_fields,
                "parameters": {
                    **selector_parameters,
                    "source_fold_ids": [window["fold_id"] for window in oos_windows],
                    "pooling_rule": "chronological_non_overlapping_equal_weight_daily_returns",
                },
                "evaluation_start": oos_windows[0]["evaluation_start"],
                "evaluation_end_exclusive": oos_windows[-1]["evaluation_end_exclusive"],
                "evidence_role": "primary_pooled_purged_oos_aggregate",
            },
        )

        self._append_benchmark_trials(
            trials,
            protocol=protocol,
            datasets=ordered_datasets,
            components=components,
            portfolio_dataset_id=portfolio_dataset_id,
            windows=windows,
            scenarios=scenarios,
        )
        self._append_semantic_trials(
            trials,
            protocol=protocol,
            baseline=baseline,
            datasets=ordered_datasets,
            windows=windows,
        )
        self._append_derived_trials(
            trials,
            protocol=protocol,
            components=components,
            portfolio_dataset_id=portfolio_dataset_id,
            windows=windows,
        )
        self._append_scoring_and_sizing_trials(
            trials,
            protocol=protocol,
            datasets=ordered_datasets,
            components=components,
            portfolio_dataset_id=portfolio_dataset_id,
            oos_windows=oos_windows,
        )
        self._append_historical_disposition_trial(
            trials,
            protocol=protocol,
            components=components,
            portfolio_dataset_id=portfolio_dataset_id,
            oos_windows=oos_windows,
        )
        return trials

    def _registry_windows(self, protocol: Mapping[str, Any]) -> list[dict[str, str]]:
        data = self._mapping(protocol, "data")
        windows = [
            {
                "fold_id": "full-panel",
                "evaluation_start": parse_utc_datetime(
                    data.get("evaluation_start"), field="data.evaluation_start"
                ).isoformat(),
                "evaluation_end_exclusive": parse_utc_datetime(
                    data.get("evaluation_end_exclusive"),
                    field="data.evaluation_end_exclusive",
                ).isoformat(),
                "evidence_role": "retrospective_descriptive",
            }
        ]
        for fold in self._list_of_mappings(protocol, "folds"):
            fold_id = f"oos-{int(fold['oos_year'])}"
            windows.append(
                {
                    "fold_id": fold_id,
                    "evaluation_start": parse_utc_datetime(
                        fold["test_start"], field=f"folds.{fold_id}.test_start"
                    ).isoformat(),
                    "evaluation_end_exclusive": parse_utc_datetime(
                        fold["test_end_exclusive"],
                        field=f"folds.{fold_id}.test_end_exclusive",
                    ).isoformat(),
                    "evidence_role": "retrospective_diagnostic",
                }
            )
        return windows

    def _registry_dataset_components(
        self,
        datasets: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, str]]:
        if not datasets:
            message = "trial registry requires at least one frozen dataset"
            raise ValueError(message)
        components: list[dict[str, str]] = []
        for dataset in datasets:
            dataset_id = self._nonempty_string(dataset.get("dataset_id"), field="dataset_id")
            digest = self._nonempty_string(
                self._mapping(dataset, "audit").get("ohlcv_sha256"),
                field=f"datasets.{dataset_id}.audit.ohlcv_sha256",
            )
            if not _SHA256_RE.fullmatch(digest):
                message = f"dataset {dataset_id} has an invalid OHLCV digest"
                raise ValueError(message)
            components.append(
                {
                    "dataset_id": dataset_id,
                    "ohlcv_sha256": digest,
                    "requested_product": self._nonempty_string(
                        dataset.get("requested_product"), field="requested_product"
                    ),
                    "canonical_product": self._nonempty_string(
                        dataset.get("canonical_product"), field="canonical_product"
                    ),
                }
            )
        if len({item["dataset_id"] for item in components}) != len(components):
            message = "frozen datasets contain duplicate dataset IDs"
            raise ValueError(message)
        return sorted(components, key=lambda item: item["dataset_id"])

    @staticmethod
    def _derived_portfolio_dataset_id(components: Sequence[Mapping[str, str]]) -> str:
        encoded = "||".join(
            f"{item['dataset_id']}@sha256={item['ohlcv_sha256']}" for item in components
        )
        return f"derived:equal-weight-portfolio:{encoded}"

    @staticmethod
    def _append_registered_trial(
        trials: list[dict[str, Any]],
        spec: Mapping[str, Any],
    ) -> None:
        trials.append(
            {
                "sequence": len(trials),
                **dict(spec),
                "historical_selection_contaminated": True,
            }
        )

    def _benchmark_contracts(
        self,
        protocol: Mapping[str, Any],
    ) -> list[tuple[str, str, dict[str, Any]]]:
        raw_names = protocol.get("benchmarks")
        if isinstance(raw_names, (str, bytes)) or not isinstance(raw_names, Sequence):
            message = "benchmarks must be a JSON array"
            raise TypeError(message)
        names = [self._nonempty_string(name, field="benchmarks[]") for name in raw_names]
        supported = set(_BENCHMARK_KIND_BY_PROTOCOL_NAME)
        if not names or len(names) != len(set(names)) or not set(names) <= supported:
            message = "benchmarks must be unique executable benchmark implementations"
            raise ValueError(message)
        definitions = self._mapping(protocol, "benchmark_definitions")
        if set(definitions) != {"portfolio_aggregation", *names}:
            message = "benchmark_definitions must exactly cover the registered benchmarks"
            raise ValueError(message)

        fixed_exposure = float(
            self._mapping(protocol, "portfolio_assumptions")["primary_fixed_exposure_pct"]
        )

        contracts: list[tuple[str, str, dict[str, Any]]] = []
        for name in names:
            kind = _BENCHMARK_KIND_BY_PROTOCOL_NAME[name]
            BenchmarkKind(kind)
            definition = dict(self._mapping(definitions, name))
            self._validate_benchmark_definition(
                name,
                definition,
                fixed_exposure=fixed_exposure,
            )
            contracts.append(
                (
                    _BENCHMARK_ARM_BY_PROTOCOL_NAME[name],
                    name,
                    {
                        "protocol_benchmark_name": name,
                        "benchmark_kind": kind,
                        "definition": definition,
                        "execution_helpers": dict(_EXECUTION_HELPERS),
                    },
                )
            )
        return contracts

    @staticmethod
    def _validate_benchmark_definition(
        name: str,
        definition: Mapping[str, Any],
        *,
        fixed_exposure: float,
    ) -> None:
        if name == "cash" and definition.get("execution") != "zero_return_no_orders":
            message = "cash benchmark is unsupported unless it remains flat with no orders"
            raise ValueError(message)
        if name == "buy_and_hold" and float(definition.get("exposure_pct", -1.0)) != fixed_exposure:
            message = "buy_and_hold exposure must match the registered fixed-exposure contract"
            raise ValueError(message)
        if name == "sma_30_long_cash" and (
            int(definition.get("warmup_bars", -1)) != _SMA_BENCHMARK_WARMUP
            or float(definition.get("exposure_pct", -1.0)) != fixed_exposure
        ):
            message = "sma_30_long_cash benchmark definition is not executable as named"
            raise ValueError(message)
        if name == "momentum_90d_long_cash" and (
            int(definition.get("warmup_bars", -1)) != _MOMENTUM_BENCHMARK_WARMUP
            or float(definition.get("exposure_pct", -1.0)) != fixed_exposure
        ):
            message = "momentum_90d_long_cash benchmark definition is not executable as named"
            raise ValueError(message)
        if name == "training_calibrated_volatility_targeted_buy_and_hold":
            expected = {
                "calibration_lookback_calendar_days": 365,
                "minimum_calibration_returns": 60,
                "target_annualized_volatility": 0.05,
                "minimum_exposure_pct": 0.01,
                "maximum_exposure_pct": 0.1,
                "leverage_allowed": False,
            }
            if any(definition.get(key) != value for key, value in expected.items()):
                message = "volatility-target benchmark definition is not executable as registered"
                raise ValueError(message)

    def _append_benchmark_trials(
        self,
        trials: list[dict[str, Any]],
        *,
        protocol: Mapping[str, Any],
        datasets: Sequence[Mapping[str, Any]],
        components: list[dict[str, str]],
        portfolio_dataset_id: str,
        windows: Sequence[Mapping[str, str]],
        scenarios: Sequence[str],
    ) -> None:
        aggregation = dict(
            self._mapping(self._mapping(protocol, "benchmark_definitions"), "portfolio_aggregation")
        )
        oos_fold_ids = [
            str(window["fold_id"]) for window in windows if window["fold_id"] != "full-panel"
        ]
        for arm_id, _benchmark_name, base_parameters in self._benchmark_contracts(protocol):
            for scenario in scenarios:
                for window in windows:
                    for dataset, component in zip(datasets, components, strict=True):
                        self._append_registered_trial(
                            trials,
                            {
                                "arm_id": arm_id,
                                "trial_family": "executable_benchmark",
                                "runner_kind": "executable_benchmark_component",
                                "fold_id": window["fold_id"],
                                "cost_scenario": scenario,
                                "dataset_id": dataset["dataset_id"],
                                "parameters": {
                                    **base_parameters,
                                    "component_dataset": component,
                                    "calibration_end_exclusive": window["evaluation_start"],
                                },
                                "evaluation_start": window["evaluation_start"],
                                "evaluation_end_exclusive": window["evaluation_end_exclusive"],
                                "evidence_role": window["evidence_role"],
                            },
                        )
                    self._append_registered_trial(
                        trials,
                        {
                            "arm_id": arm_id,
                            "trial_family": "executable_benchmark",
                            "runner_kind": "equal_weight_benchmark_portfolio",
                            "fold_id": window["fold_id"],
                            "cost_scenario": scenario,
                            "dataset_id": portfolio_dataset_id,
                            "component_datasets": components,
                            "parameters": {
                                **base_parameters,
                                "component_datasets": components,
                                "portfolio_aggregation": aggregation,
                                "calibration_end_exclusive": window["evaluation_start"],
                            },
                            "evaluation_start": window["evaluation_start"],
                            "evaluation_end_exclusive": window["evaluation_end_exclusive"],
                            "evidence_role": (
                                "retrospective_portfolio_descriptive"
                                if window["fold_id"] == "full-panel"
                                else "retrospective_portfolio_oos_diagnostic"
                            ),
                        },
                    )
                self._append_registered_trial(
                    trials,
                    {
                        "arm_id": arm_id,
                        "trial_family": "executable_benchmark",
                        "runner_kind": "pooled_oos_benchmark_aggregate",
                        "fold_id": "pooled-oos",
                        "cost_scenario": scenario,
                        "dataset_id": portfolio_dataset_id,
                        "component_datasets": components,
                        "parameters": {
                            **base_parameters,
                            "component_datasets": components,
                            "portfolio_aggregation": aggregation,
                            "source_fold_ids": oos_fold_ids,
                            "pooling_rule": (
                                "chronological_non_overlapping_equal_weight_daily_returns"
                            ),
                        },
                        "evaluation_start": windows[1]["evaluation_start"],
                        "evaluation_end_exclusive": windows[-1]["evaluation_end_exclusive"],
                        "evidence_role": "retrospective_pooled_oos_benchmark",
                    },
                )

    def _semantic_contracts(
        self,
        protocol: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Validate protocol-declared semantic diagnostics without strategy coupling."""

        contracts = self._list_of_mappings(protocol, "semantic_diagnostic_arms")
        supported_execution_modes = {
            "expected_cost_full_panel_and_direct_oos_diagnostic",
            "registered_abandoned_asset_mismatch_no_spot_short_pnl",
        }
        supported_policy_overrides = {
            "next_open_market_only_ignore_emitted_protective_stop",
        }
        findings = self._list_of_mappings(
            self._mapping(protocol, "correctness_attestation"),
            "findings",
        )
        findings_by_id: dict[str, Mapping[str, Any]] = {}
        for finding in findings:
            finding_id = self._nonempty_string(
                finding.get("id"),
                field="correctness_attestation.findings.id",
            )
            if finding_id in findings_by_id:
                message = "correctness-finding identities must be unique"
                raise ValueError(message)
            findings_by_id[finding_id] = finding

        observed_ids: set[str] = set()
        observed_findings: set[str] = set()
        observed_signatures: set[tuple[bytes, str, str | None]] = set()
        for contract in contracts:
            arm_id = self._nonempty_string(contract.get("id"), field="semantic.id")
            finding_id = self._nonempty_string(
                contract.get("correctness_finding_id"),
                field="semantic.correctness_finding_id",
            )
            overrides = dict(self._mapping(contract, "overrides"))
            overrides_bytes = canonical_manifest_bytes({"overrides": overrides})
            execution = self._nonempty_string(
                contract.get("execution"),
                field="semantic.execution",
            )
            if execution not in supported_execution_modes:
                message = f"unsupported semantic execution mode: {execution}"
                raise ValueError(message)
            policy_raw = contract.get("execution_policy_override")
            policy: str | None = None
            if policy_raw is not None:
                policy = self._nonempty_string(
                    policy_raw,
                    field="semantic.execution_policy_override",
                )
                if policy not in supported_policy_overrides:
                    message = f"unsupported semantic execution-policy override: {policy}"
                    raise ValueError(message)
                if execution != "expected_cost_full_panel_and_direct_oos_diagnostic":
                    message = "semantic execution-policy override requires executable diagnostics"
                    raise ValueError(message)

            matched_finding = findings_by_id.get(finding_id)
            if matched_finding is None or matched_finding.get("finding_class") != (
                CorrectnessFindingClass.STRATEGY_SEMANTIC.value
            ):
                message = f"semantic contract has no exact strategy finding: {finding_id}"
                raise ValueError(message)
            signature = (overrides_bytes, execution, policy)
            if (
                arm_id in observed_ids
                or finding_id in observed_findings
                or signature in observed_signatures
            ):
                message = (
                    "semantic arm, correctness-finding, and execution signatures must be unique"
                )
                raise ValueError(message)
            observed_ids.add(arm_id)
            observed_findings.add(finding_id)
            observed_signatures.add(signature)
        return contracts

    def _append_semantic_trials(
        self,
        trials: list[dict[str, Any]],
        *,
        protocol: Mapping[str, Any],
        baseline: Mapping[str, Any],
        datasets: Sequence[Mapping[str, Any]],
        windows: Sequence[Mapping[str, str]],
    ) -> None:
        components = self._registry_dataset_components(datasets)
        for contract in self._semantic_contracts(protocol):
            arm_id = str(contract["id"])
            parameters = {
                **dict(baseline),
                **dict(self._mapping(contract, "overrides")),
                "semantic_arm_id": arm_id,
                "execution_contract": contract["execution"],
            }
            if "execution_policy_override" in contract:
                parameters["execution_policy_override"] = contract["execution_policy_override"]
            if contract["execution"] == "registered_abandoned_asset_mismatch_no_spot_short_pnl":
                for dataset, component in zip(datasets, components, strict=True):
                    self._append_registered_trial(
                        trials,
                        {
                            "arm_id": arm_id,
                            "trial_family": "semantic_diagnostic",
                            "runner_kind": "abandoned_asset_mismatch",
                            "fold_id": "asset-mismatch",
                            "cost_scenario": "not_applicable",
                            "dataset_id": dataset["dataset_id"],
                            "parameters": {**parameters, "component_dataset": component},
                            "evidence_role": "asset_mismatch_no_spot_short_pnl",
                            "registration_status": BacktestTrialStatus.ABANDONED.value,
                            "abandonment_reason": (
                                "spot products cannot reproduce the original short leg"
                            ),
                        },
                    )
                continue
            for dataset, component in zip(datasets, components, strict=True):
                for window in windows:
                    self._append_registered_trial(
                        trials,
                        {
                            "arm_id": arm_id,
                            "trial_family": "semantic_diagnostic",
                            "runner_kind": "production_core_semantic_diagnostic",
                            "fold_id": window["fold_id"],
                            "cost_scenario": "expected",
                            "dataset_id": dataset["dataset_id"],
                            "parameters": {**parameters, "component_dataset": component},
                            "evaluation_start": window["evaluation_start"],
                            "evaluation_end_exclusive": window["evaluation_end_exclusive"],
                            "evidence_role": "retrospective_semantic_diagnostic",
                        },
                    )

    def _append_derived_trials(
        self,
        trials: list[dict[str, Any]],
        *,
        protocol: Mapping[str, Any],
        components: list[dict[str, str]],
        portfolio_dataset_id: str,
        windows: Sequence[Mapping[str, str]],
    ) -> None:
        baseline_arm_id = self._historical_disposition_policy(protocol).baseline_arm_id
        full_panel = windows[0]
        oos_windows = list(windows[1:])
        inference = dict(self._mapping(protocol, "inference"))
        robustness = dict(self._mapping(protocol, "robustness"))
        pbo = dict(self._mapping(robustness, "pbo"))
        economic_period_date_rule = self._nonempty_string(
            self._mapping(protocol, "power").get("economic_period_date_transform"),
            field="power.economic_period_date_transform",
        )
        robustness_without_pbo = {key: value for key, value in robustness.items() if key != "pbo"}
        pooled_common = {
            "dataset_id": portfolio_dataset_id,
            "component_datasets": components,
            "evaluation_start": oos_windows[0]["evaluation_start"],
            "evaluation_end_exclusive": oos_windows[-1]["evaluation_end_exclusive"],
        }
        self._append_registered_trial(
            trials,
            {
                "arm_id": _ROBUSTNESS_ARM,
                "trial_family": "derived_diagnostic",
                "runner_kind": "robustness_concentration_inference",
                "fold_id": "pooled-oos",
                "cost_scenario": "expected",
                **pooled_common,
                "parameters": {
                    "component_datasets": components,
                    "source_arm_id": baseline_arm_id,
                    "adaptive_selector_diagnostic_arm_id": self._nonempty_string(
                        self._mapping(protocol, "robustness").get(
                            "adaptive_selector_diagnostic_arm_id"
                        ),
                        field="robustness.adaptive_selector_diagnostic_arm_id",
                    ),
                    "source_fold_ids": [window["fold_id"] for window in oos_windows],
                    "neighbor_candidate_order": list(
                        self._mapping(
                            self._mapping(protocol, "selection"),
                            "primary_trial_family",
                        )["candidate_arm_ids"]
                    ),
                    "current_economically_compared_candidate_order": (
                        self._current_compared_arm_order(protocol)
                    ),
                    "registered_one_factor_changes": (
                        self._registered_redesign_change_contracts(protocol)
                    ),
                    "robustness": robustness_without_pbo,
                    "inference": inference,
                    "multiple_testing": dict(self._mapping(protocol, "multiple_testing")),
                    "historical_disposition_gates": dict(
                        self._mapping(protocol, "historical_disposition_gates")
                    ),
                    "allowed_dispositions": list(protocol["allowed_dispositions"]),
                    "promotion": dict(self._mapping(protocol, "promotion")),
                    "economic_period_date_rule": economic_period_date_rule,
                    "execution_helpers": dict(_EXECUTION_HELPERS),
                },
                "evidence_role": "retrospective_robustness_inference_diagnostic",
            },
        )
        self._append_registered_trial(
            trials,
            {
                "arm_id": _PBO_ARM,
                "trial_family": "derived_diagnostic",
                "runner_kind": "registered_cscv_pbo",
                "fold_id": "cscv",
                "cost_scenario": "expected",
                "dataset_id": portfolio_dataset_id,
                "component_datasets": components,
                "evaluation_start": full_panel["evaluation_start"],
                "evaluation_end_exclusive": full_panel["evaluation_end_exclusive"],
                "parameters": {
                    "component_datasets": components,
                    "pbo": pbo,
                    "candidate_source_family": "production_core_direct",
                    "execution_helpers": dict(_EXECUTION_HELPERS),
                    "calendar_year_groups": list(
                        self._mapping(protocol, "robustness")["year_groups"]
                    ),
                    "economic_period_date_rule": economic_period_date_rule,
                },
                "evidence_role": "retrospective_calendar_block_cscv_pbo_diagnostic",
            },
        )
        redesign = self._registered_redesign_trial_contract(protocol)
        self._append_registered_trial(
            trials,
            {
                "arm_id": redesign["arm_id"],
                "trial_family": redesign["trial_family"],
                "runner_kind": redesign["runner_kind"],
                "fold_id": redesign["fold_id"],
                "cost_scenario": redesign["cost_scenario"],
                **pooled_common,
                "parameters": {
                    "component_datasets": components,
                    "source_fold_ids": [window["fold_id"] for window in oos_windows],
                    "expected_cost_scenario": "expected",
                    "stressed_cost_scenario": "stressed",
                    "registered_one_factor_changes": (
                        self._registered_redesign_change_contracts(protocol)
                    ),
                    "economic_period_date_rule": economic_period_date_rule,
                    "historical_disposition_gates": dict(
                        self._mapping(protocol, "historical_disposition_gates")
                    ),
                    "execution_helpers": dict(_EXECUTION_HELPERS),
                },
                "evidence_role": redesign["evidence_role"],
            },
        )

        power = dict(self._mapping(protocol, "power"))
        effects = power.get("minimum_standardized_effect_sensitivities")
        if isinstance(effects, (str, bytes)) or not isinstance(effects, Sequence):
            message = "power sensitivities must be a JSON array"
            raise TypeError(message)
        normalized_effects = [float(effect) for effect in effects]
        if len(normalized_effects) != len(set(normalized_effects)) or any(
            not 0.0 < effect < 1.0 for effect in normalized_effects
        ):
            message = "power sensitivities must be unique values in (0, 1)"
            raise ValueError(message)
        for effect in normalized_effects:
            self._append_registered_trial(
                trials,
                {
                    "arm_id": self._power_arm_id(effect),
                    "trial_family": "prospective_power_design",
                    "runner_kind": "prospective_power_study",
                    "fold_id": "prospective-design",
                    "cost_scenario": "not_applicable",
                    "dataset_id": portfolio_dataset_id,
                    "component_datasets": components,
                    "evaluation_start": full_panel["evaluation_start"],
                    "evaluation_end_exclusive": full_panel["evaluation_end_exclusive"],
                    "parameters": {
                        **power,
                        "component_datasets": components,
                        "minimum_standardized_effect": effect,
                        "cadence_source_arm_id": baseline_arm_id,
                        "cadence_source_fold_id": "full-panel",
                        "closed_trades_only": True,
                        "terminal_position_treatment": "right_censored_excluded",
                    },
                    "evidence_role": "prospective_design",
                },
            )

    def _append_scoring_and_sizing_trials(
        self,
        trials: list[dict[str, Any]],
        *,
        protocol: Mapping[str, Any],
        datasets: Sequence[Mapping[str, Any]],
        components: list[dict[str, str]],
        portfolio_dataset_id: str,
        oos_windows: Sequence[Mapping[str, str]],
    ) -> None:
        baseline_arm_id = self._historical_disposition_policy(protocol).baseline_arm_id
        source_fold_ids = [str(window["fold_id"]) for window in oos_windows]
        pipeline = dict(self._mapping(protocol, "pipeline_evidence_layers"))
        common_boundaries = {
            "evaluation_start": oos_windows[0]["evaluation_start"],
            "evaluation_end_exclusive": oos_windows[-1]["evaluation_end_exclusive"],
        }
        for dataset, component in zip(datasets, components, strict=True):
            self._append_registered_trial(
                trials,
                {
                    "arm_id": _SCORING_LEDGER_ARM,
                    "trial_family": "pipeline_reconciliation",
                    "runner_kind": "scoring_binding_ledger_component",
                    "fold_id": "pooled-oos",
                    "cost_scenario": "expected",
                    "dataset_id": dataset["dataset_id"],
                    "parameters": {
                        "pipeline_evidence_layers": pipeline,
                        "component_dataset": component,
                        "source_arm_id": baseline_arm_id,
                        "source_fold_ids": source_fold_ids,
                        "second_strategy_run_prohibited": True,
                    },
                    **common_boundaries,
                    "evidence_role": "pipeline_scoring_binding_reconciliation",
                },
            )
        self._append_registered_trial(
            trials,
            {
                "arm_id": _SCORING_LEDGER_ARM,
                "trial_family": "pipeline_reconciliation",
                "runner_kind": "scoring_binding_ledger_pooled",
                "fold_id": "pooled-oos",
                "cost_scenario": "expected",
                "dataset_id": portfolio_dataset_id,
                "component_datasets": components,
                "parameters": {
                    "pipeline_evidence_layers": pipeline,
                    "component_datasets": components,
                    "source_arm_id": baseline_arm_id,
                    "source_fold_ids": source_fold_ids,
                    "join_key": pipeline["join_key"],
                    "second_strategy_run_prohibited": True,
                },
                **common_boundaries,
                "evidence_role": "pipeline_scoring_binding_pooled_reconciliation",
            },
        )

        sizing = self._mapping(protocol, "conditional_sizing_trials")
        sizing_arms = self._list_of_mappings(sizing, "arms")
        sizing_ids = [
            self._nonempty_string(arm.get("id"), field="sizing.id") for arm in sizing_arms
        ]
        if not sizing_ids or len(sizing_ids) != len(set(sizing_ids)):
            message = "conditional sizing must contain unique registered arms"
            raise ValueError(message)
        sizing_common = {
            "execution_gate": sizing["execution_gate"],
            "registration": sizing["registration"],
            "minimum_order_notional_usd": sizing["minimum_order_notional_usd"],
            "precision_source": sizing["precision_source"],
            "pre_broker_quantity_rounding": sizing["pre_broker_quantity_rounding"],
            "research_only_parallel_sizer_prohibited": sizing[
                "research_only_parallel_sizer_prohibited"
            ],
        }
        for arm in sizing_arms:
            arm_id = str(arm["id"])
            for window in oos_windows:
                for dataset, component in zip(datasets, components, strict=True):
                    self._append_registered_trial(
                        trials,
                        {
                            "arm_id": arm_id,
                            "trial_family": "conditional_sizing",
                            "runner_kind": "conditional_sizing_oos_component",
                            "fold_id": window["fold_id"],
                            "cost_scenario": "expected",
                            "dataset_id": dataset["dataset_id"],
                            "parameters": {
                                **sizing_common,
                                "sizing_arm": dict(arm),
                                "component_dataset": component,
                                "source_arm_id": baseline_arm_id,
                                "source_fold_id": window["fold_id"],
                                "execution_helpers": dict(_EXECUTION_HELPERS),
                            },
                            "evaluation_start": window["evaluation_start"],
                            "evaluation_end_exclusive": window["evaluation_end_exclusive"],
                            "evidence_role": "conditional_sizing_oos_diagnostic",
                        },
                    )
            self._append_registered_trial(
                trials,
                {
                    "arm_id": arm_id,
                    "trial_family": "conditional_sizing",
                    "runner_kind": "conditional_sizing_pooled_oos",
                    "fold_id": "pooled-oos",
                    "cost_scenario": "expected",
                    "dataset_id": portfolio_dataset_id,
                    "component_datasets": components,
                    "parameters": {
                        **sizing_common,
                        "sizing_arm": dict(arm),
                        "component_datasets": components,
                        "source_arm_id": baseline_arm_id,
                        "source_fold_ids": source_fold_ids,
                        "execution_helpers": dict(_EXECUTION_HELPERS),
                    },
                    **common_boundaries,
                    "evidence_role": "conditional_sizing_pooled_oos_diagnostic",
                },
            )

    @staticmethod
    def _power_arm_id(effect: float) -> str:
        token = format(effect, ".12g").replace("-", "neg").replace(".", "p")
        return f"POWER_EFFECT_{token}"

    def _append_historical_disposition_trial(
        self,
        trials: list[dict[str, Any]],
        *,
        protocol: Mapping[str, Any],
        components: list[dict[str, str]],
        portfolio_dataset_id: str,
        oos_windows: Sequence[Mapping[str, str]],
    ) -> None:
        """Register the sole outcome-blind historical conclusion checkpoint last."""

        policy = self._historical_disposition_policy(protocol)
        prior_sequences = [int(spec["sequence"]) for spec in trials]
        if prior_sequences != list(range(len(trials))):
            message = "historical disposition requires a contiguous prior trial registry"
            raise ValueError(message)
        predeclared_abandonments = [
            {
                "sequence": int(spec["sequence"]),
                "runner_kind": str(spec["runner_kind"]),
                "exact_reason": str(spec["abandonment_reason"]),
            }
            for spec in trials
            if spec.get("registration_status") == BacktestTrialStatus.ABANDONED.value
        ]
        semantic_finding_candidate_map = {
            self._nonempty_string(
                contract.get("correctness_finding_id"),
                field="semantic_diagnostic_arms.correctness_finding_id",
            ): self._nonempty_string(contract.get("id"), field="semantic_diagnostic_arms.id")
            for contract in self._semantic_contracts(protocol)
        }
        self._append_registered_trial(
            trials,
            {
                "arm_id": _HISTORICAL_DISPOSITION_ARM,
                "trial_family": "historical_disposition",
                "runner_kind": _HISTORICAL_DISPOSITION_RUNNER,
                "fold_id": "historical-disposition",
                "cost_scenario": "not_applicable",
                "dataset_id": portfolio_dataset_id,
                "component_datasets": components,
                "parameters": {
                    "policy": policy.to_dict(),
                    "expected_prior_trial_count": len(trials),
                    "predeclared_abandonments": predeclared_abandonments,
                    "conditional_abandonment_runner_kinds": sorted(
                        _CONDITIONAL_SIZING_GROUP_RUNNERS
                    ),
                    "semantic_finding_candidate_map": (semantic_finding_candidate_map),
                    "all_registered_trials_must_be_audited": True,
                    "recursive_source_hash_reconciliation_required": True,
                    "historical_authority_only": True,
                    "paper_trading_authorized": False,
                    "live_trading_authorized": False,
                    "automatic_parameter_deployment_authorized": False,
                },
                "evaluation_start": oos_windows[0]["evaluation_start"],
                "evaluation_end_exclusive": oos_windows[-1]["evaluation_end_exclusive"],
                "evidence_role": "historical_research_disposition_only",
            },
        )

    def _historical_disposition_policy(
        self,
        protocol: Mapping[str, Any],
    ) -> HistoricalDispositionPolicy:
        """Parse the exact generic disposition policy frozen in the protocol."""

        raw = self._mapping(protocol, "historical_disposition_policy")
        return self._historical_disposition_policy_from_mapping(raw)

    def _historical_disposition_policy_from_mapping(
        self,
        raw: Mapping[str, Any],
    ) -> HistoricalDispositionPolicy:
        """Parse an embedded policy copy without consulting mutable protocol state."""

        expected_keys = {
            "policy_id",
            "baseline_arm_id",
            "baseline_ledger_check_id",
            "required_evidence",
            "baseline_quantitative_gate_ids",
            "baseline_economic_gate_ids",
            "prospective_primary_effect_id",
            "maximum_prospective_duration_years",
            "registered_redesign_candidate_ids",
            "redesign_quantitative_gate_ids",
            "registered_semantic_candidate_ids",
            "correctable_semantic_candidate_ids",
        }
        optional_keys = {"registered_promotion_blocker_finding_ids"}
        if set(raw) not in (expected_keys, expected_keys | optional_keys):
            message = "historical_disposition_policy fields differ from the executable schema"
            raise ValueError(message)

        def identifiers(field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
            value = raw.get(field)
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                message = f"historical_disposition_policy.{field} must be an array"
                raise TypeError(message)
            normalized = tuple(
                self._nonempty_string(
                    item,
                    field=f"historical_disposition_policy.{field}[]",
                )
                for item in value
            )
            if not allow_empty and not normalized:
                message = f"historical_disposition_policy.{field} must not be empty"
                raise ValueError(message)
            return normalized

        requirements: list[HistoricalEvidenceRequirement] = []
        for index, item in enumerate(self._list_of_mappings(raw, "required_evidence")):
            if set(item) != {
                "check_id",
                "kind",
                "required_source_ids",
                "allows_verified_empty",
            }:
                message = (
                    "historical_disposition_policy.required_evidence fields differ "
                    f"at index {index}"
                )
                raise ValueError(message)
            source_ids_raw = item.get("required_source_ids")
            if isinstance(source_ids_raw, (str, bytes)) or not isinstance(source_ids_raw, Sequence):
                message = "historical disposition required_source_ids must be an array"
                raise TypeError(message)
            source_ids = tuple(
                self._nonempty_string(
                    source_id,
                    field="historical_disposition_policy.required_source_ids[]",
                )
                for source_id in source_ids_raw
            )
            unknown_sources = sorted(set(source_ids) - _HISTORICAL_SOURCE_IDS)
            if unknown_sources:
                message = f"historical disposition source IDs are unsupported: {unknown_sources}"
                raise ValueError(message)
            allows_empty = item.get("allows_verified_empty")
            if not isinstance(allows_empty, bool):
                message = "historical disposition allows_verified_empty must be a bool"
                raise TypeError(message)
            requirements.append(
                HistoricalEvidenceRequirement(
                    check_id=self._nonempty_string(
                        item.get("check_id"),
                        field="historical_disposition_policy.required_evidence.check_id",
                    ),
                    kind=HistoricalEvidenceKind(
                        self._nonempty_string(
                            item.get("kind"),
                            field="historical_disposition_policy.required_evidence.kind",
                        )
                    ),
                    required_source_ids=source_ids,
                    allows_verified_empty=allows_empty,
                )
            )
        observed_source_ids = {
            source_id
            for requirement in requirements
            for source_id in requirement.required_source_ids
        }
        if observed_source_ids != _HISTORICAL_SOURCE_IDS:
            missing = sorted(_HISTORICAL_SOURCE_IDS - observed_source_ids)
            extra = sorted(observed_source_ids - _HISTORICAL_SOURCE_IDS)
            message = (
                "historical disposition required source coverage differs "
                f"(missing={missing}, extra={extra})"
            )
            raise ValueError(message)

        return HistoricalDispositionPolicy(
            policy_id=self._nonempty_string(
                raw.get("policy_id"), field="historical_disposition_policy.policy_id"
            ),
            baseline_arm_id=self._nonempty_string(
                raw.get("baseline_arm_id"),
                field="historical_disposition_policy.baseline_arm_id",
            ),
            baseline_ledger_check_id=self._nonempty_string(
                raw.get("baseline_ledger_check_id"),
                field="historical_disposition_policy.baseline_ledger_check_id",
            ),
            required_evidence=tuple(requirements),
            baseline_quantitative_gate_ids=identifiers("baseline_quantitative_gate_ids"),
            baseline_economic_gate_ids=identifiers("baseline_economic_gate_ids"),
            prospective_primary_effect_id=self._nonempty_string(
                raw.get("prospective_primary_effect_id"),
                field="historical_disposition_policy.prospective_primary_effect_id",
            ),
            maximum_prospective_duration_years=float(raw["maximum_prospective_duration_years"]),
            registered_redesign_candidate_ids=identifiers("registered_redesign_candidate_ids"),
            redesign_quantitative_gate_ids=identifiers("redesign_quantitative_gate_ids"),
            registered_semantic_candidate_ids=identifiers(
                "registered_semantic_candidate_ids",
                allow_empty=True,
            ),
            correctable_semantic_candidate_ids=identifiers(
                "correctable_semantic_candidate_ids",
                allow_empty=True,
            ),
            registered_promotion_blocker_finding_ids=(
                identifiers(
                    "registered_promotion_blocker_finding_ids",
                    allow_empty=True,
                )
                if "registered_promotion_blocker_finding_ids" in raw
                else ()
            ),
        )

    def _registered_redesign_trial_contract(
        self,
        protocol: Mapping[str, Any],
    ) -> dict[str, str]:
        gates = self._mapping(protocol, "historical_disposition_gates")
        candidate_gate = self._mapping(gates, "redesign_candidate_gate")
        registered = self._mapping(candidate_gate, "derived_evidence_trial")
        required = {
            "arm_id",
            "trial_family",
            "runner_kind",
            "fold_id",
            "cost_scenario",
            "evidence_role",
        }
        if set(registered) != required:
            message = "registered redesign derived-evidence trial contract is incomplete"
            raise ValueError(message)
        normalized = {
            key: self._nonempty_string(registered.get(key), field=f"redesign.{key}")
            for key in sorted(required)
        }
        if (
            normalized["runner_kind"] != _REDESIGN_RUNNER
            or normalized["fold_id"] != "pooled-oos"
            or normalized["cost_scenario"] != "expected"
        ):
            message = "registered redesign derived-evidence execution contract is unsupported"
            raise ValueError(message)
        return normalized

    def _registered_redesign_change_contracts(
        self,
        protocol: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        baseline = self._mapping(protocol, "baseline_parameters")
        arms = {str(arm["id"]): arm for arm in self._list_of_mappings(protocol, "signal_rule_arms")}
        candidate_gate = self._mapping(
            self._mapping(protocol, "historical_disposition_gates"),
            "redesign_candidate_gate",
        )
        raw_candidate_ids = candidate_gate.get("candidate_set")
        if isinstance(raw_candidate_ids, (str, bytes)) or not isinstance(
            raw_candidate_ids, Sequence
        ):
            message = "redesign candidate_set must be a JSON array"
            raise TypeError(message)
        candidate_ids = [
            self._nonempty_string(candidate_id, field="redesign.candidate_set[]")
            for candidate_id in raw_candidate_ids
        ]
        if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
            message = "redesign candidate_set must be non-empty and unique"
            raise ValueError(message)
        changes: list[dict[str, Any]] = []
        for candidate_id in candidate_ids:
            arm = arms.get(candidate_id)
            if arm is None:
                message = f"redesign candidate is absent from signal-rule registry: {candidate_id}"
                raise ValueError(message)
            overrides = self._mapping(arm, "overrides")
            if not overrides:
                message = f"redesign candidate must change one registered factor: {candidate_id}"
                raise ValueError(message)
            unknown_fields = sorted(set(overrides) - set(baseline))
            if unknown_fields:
                message = (
                    "redesign candidate changes unknown baseline field(s): "
                    f"{candidate_id} {unknown_fields}"
                )
                raise ValueError(message)
            unchanged_fields = sorted(
                field
                for field, candidate_value in overrides.items()
                if baseline[field] == candidate_value
            )
            if unchanged_fields:
                message = (
                    "redesign candidate contains unchanged override field(s): "
                    f"{candidate_id} {unchanged_fields}"
                )
                raise ValueError(message)
            if len(overrides) == 1:
                parameter_name, candidate_value = next(iter(overrides.items()))
                baseline_value: Any = baseline[parameter_name]
            else:
                parameter_name = self._nonempty_string(
                    arm.get("factor_name"),
                    field=f"signal_rule_arms.{candidate_id}.factor_name",
                )
                ordered_fields = sorted(overrides)
                baseline_value = {field: baseline[field] for field in ordered_fields}
                candidate_value = {field: overrides[field] for field in ordered_fields}
            changes.append(
                {
                    "candidate_id": candidate_id,
                    "parameter_name": parameter_name,
                    "baseline_value": baseline_value,
                    "candidate_value": candidate_value,
                }
            )
        return changes

    def _current_compared_arm_order(
        self,
        protocol: Mapping[str, Any],
    ) -> list[str]:
        signal_ids = [
            self._nonempty_string(arm.get("id"), field="signal_rule_arms.id")
            for arm in self._list_of_mappings(protocol, "signal_rule_arms")
        ]
        semantic_ids = [
            self._nonempty_string(arm.get("id"), field="semantic_diagnostic_arms.id")
            for arm in self._list_of_mappings(protocol, "semantic_diagnostic_arms")
            if arm.get("execution") == "expected_cost_full_panel_and_direct_oos_diagnostic"
        ]
        compared = [*signal_ids, *semantic_ids]
        if len(compared) != len(set(compared)):
            message = "economically compared candidate identities must be unique"
            raise ValueError(message)
        return compared

    def _primary_selector_parameters(
        self,
        protocol: Mapping[str, Any],
        primary: Mapping[str, Any],
    ) -> dict[str, Any]:
        design = self._mapping(protocol, "validation_design")
        selection = self._mapping(protocol, "selection")
        data = self._mapping(protocol, "data")
        return {
            "selector": primary["selector"],
            "candidate_arm_ids": list(primary["candidate_arm_ids"]),
            "diagnostic_arm_ids_excluded_from_selection": list(
                primary["diagnostic_arm_ids_excluded_from_selection"]
            ),
            "selection_scope": primary["selection_scope"],
            "training_objective": primary["training_objective"],
            "tie_break": primary["tie_break"],
            "oos_parameter_source": primary["oos_parameter_source"],
            "selection_terminal_treatment": dict(
                self._mapping(primary, "selection_terminal_treatment")
            ),
            "oos_fold_terminal_treatment": dict(
                self._mapping(primary, "oos_fold_terminal_treatment")
            ),
            "purge_calendar_days": design["initial_purge_calendar_days"],
            "embargo_bars": design["embargo_bars"],
            "fold_boundary_position": design["fold_boundary_position"],
            "selected_arm_tie_tolerance": selection["selected_arm_tie_tolerance"],
            "training_evaluation_start": data["evaluation_start"],
            "training_terminal_cost_scenario": selection["training_terminal_cost_scenario"],
            "bootstrap_rule": "maximum_registered_candidate_core_warmup",
            "execution_helpers": dict(_EXECUTION_HELPERS),
        }
