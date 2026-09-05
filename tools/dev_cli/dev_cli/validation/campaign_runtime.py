"""Dataset revalidation and campaign persistence."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from dev_cli.validation.backtest.engine import BacktestEngine
from dev_cli.validation.campaign_composition import CampaignMixinContract
from dev_cli.validation.campaign_contracts import (
    _CONDITIONAL_SIZING_GROUP_RUNNERS,
    _HISTORICAL_DISPOSITION_ARM,
    _HISTORICAL_DISPOSITION_RUNNER,
    _PBO_ARM,
    _PHASE_ONE_GROUP_RUNNERS,
    _PHASE_ONE_INDIVIDUAL_RUNNERS,
    _PHASE_ONE_RUNNERS,
    _POWER_GROUP_RUNNERS,
    _REQUIRED_ENGINE_CAPABILITIES,
    _ROBUSTNESS_ARM,
    _SCORING_LEDGER_ARM,
    _SCORING_LEDGER_GROUP_RUNNERS,
    _SOURCE_ATTESTED_GROUP_RUNNERS,
    _TERMINAL_STATUSES,
)
from dev_cli.validation.campaign_environment import CampaignEnvironmentMixin
from dev_cli.validation.evidence import (
    file_sha256,
    load_json_object,
    parse_utc_datetime,
)
from dev_cli.validation.persistence.backtest_manifest_store import (
    canonical_manifest_bytes,
    manifest_sha256,
)
from dev_cli.validation.persistence.backtest_trial_store import (
    BacktestTrialRegistration,
    BacktestTrialStatus,
    stable_trial_id,
)
from lib_application.db.models import (
    BacktestExperiment,
    BacktestResult,
    BacktestTrial,
    StrategyVersion,
)


class CampaignRuntimeMixin(CampaignEnvironmentMixin, CampaignMixinContract):
    _file_sha256 = staticmethod(file_sha256)
    _load_json_object = staticmethod(load_json_object)

    def _dataset_asset_class(
        self,
        dataset: Mapping[str, Any],
        *,
        manifest: Mapping[str, Any],
    ) -> str:
        """Resolve pre-1.4 datasets through their frozen habitat contract."""

        value = dataset.get("asset_class")
        if value is None:
            value = self._mapping(manifest, "habitat").get("asset_class_db")
        return self._nonempty_string(value, field="dataset.asset_class")

    def _revalidate_datasets(self, manifest: Mapping[str, Any]) -> None:
        for dataset in self._list_of_mappings(self._mapping(manifest, "data"), "datasets"):
            start_value = dataset["start"]
            end_value = dataset["end_exclusive"]
            for field, value in (
                ("dataset.start", start_value),
                ("dataset.end_exclusive", end_value),
            ):
                if not isinstance(value, str):
                    message = f"{field} must be an ISO-8601 UTC timestamp"
                    raise TypeError(message)
                if not value.strip():
                    message = f"{field} must be an ISO-8601 UTC timestamp"
                    raise ValueError(message)
            _, audit = self._historical_prices.load_validated_bars(
                int(dataset["instrument_id"]),
                start=parse_utc_datetime(start_value, field="dataset.start"),
                end=parse_utc_datetime(
                    end_value,
                    field="dataset.end_exclusive",
                ),
                timeframe=str(dataset["timeframe"]),
                source=str(dataset["source"]),
                symbol=str(dataset["requested_product"]),
                minimum_coverage=float(dataset["minimum_coverage"]),
                metadata={
                    "asset_class": self._dataset_asset_class(dataset, manifest=manifest),
                    "canonical_product": dataset["canonical_product"],
                    "requested_product": dataset["requested_product"],
                },
            )
            if audit.to_dict() != self._mapping(dataset, "audit"):
                message = f"Dataset audit differs from frozen evidence: {dataset['dataset_id']}"
                raise ValueError(message)

    def _revalidate_product_metadata(
        self,
        manifest: Mapping[str, Any],
        protocol: Mapping[str, Any],
    ) -> None:
        protocol_products = {
            str(product["requested_product"]): product
            for product in self._list_of_mappings(
                self._mapping(protocol, "data"), "primary_products"
            )
        }
        for dataset in self._list_of_mappings(self._mapping(manifest, "data"), "datasets"):
            requested = str(dataset["requested_product"])
            product = protocol_products.get(requested)
            if product is None:
                message = f"frozen dataset references undeclared product {requested}"
                raise ValueError(message)
            observed = self._fetch_and_verify_product_metadata(product)
            if observed != self._mapping(dataset, "metadata_snapshot"):
                message = f"Coinbase product metadata changed after manifest freeze: {requested}"
                raise ValueError(message)

    def _fetch_and_verify_product_metadata(
        self,
        declared: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self._product_metadata_provider is None:
            message = "a live product-metadata provider is required to freeze or resume a campaign"
            raise RuntimeError(message)
        requested = str(declared["requested_product"])
        observed = dict(self._product_metadata_provider(requested))
        expected = {
            "requested_product_id": declared["requested_product"],
            "product_id": declared["provider_product_id"],
            "canonical_book": declared["expected_canonical_book"],
            "alias": declared["provider_alias"],
            "alias_to": declared["provider_alias_to"],
            "base_currency_id": declared["base_currency"],
            "quote_currency_id": declared["settlement_quote"],
            "quote_display_symbol": declared["quote_display_symbol"],
            "base_increment": declared["base_increment"],
            "quote_increment": declared["quote_increment"],
            "price_increment": declared["price_increment"],
            "product_type": declared["product_type"],
            "status": declared["status"],
            "is_disabled": declared["is_disabled"],
            "trading_disabled": declared["trading_disabled"],
        }
        if observed != expected:
            differing = sorted(
                key
                for key in set(expected) | set(observed)
                if expected.get(key) != observed.get(key)
            )
            message = f"Coinbase product metadata drift for {requested}: {', '.join(differing)}"
            raise ValueError(message)
        return observed

    def _existing_experiment_for_audit(
        self,
        manifest: Mapping[str, Any],
        *,
        manifest_hash: str,
    ) -> int:
        """Resolve exactly one existing experiment without creating or updating it."""

        strategy_id = str(self._mapping(manifest, "strategy")["strategy_id"])
        with Session(self._engine) as session:
            rows = session.execute(
                select(BacktestExperiment).where(BacktestExperiment.strategy_id == strategy_id)
            ).scalars()
            matches = [
                row for row in rows if (row.config or {}).get("manifest_hash") == manifest_hash
            ]
            if len(matches) != 1:
                message = (
                    "audit requires exactly one existing experiment for immutable manifest "
                    f"{manifest_hash}; found {len(matches)}"
                )
                raise RuntimeError(message)
            experiment = matches[0]
            config = experiment.config or {}
            expected_config = {
                "manifest_hash": manifest_hash,
                "protocol_id": manifest["protocol_id"],
                "selection_contaminated": True,
                "validation_design": manifest["validation_design"],
            }
            if config != expected_config:
                message = "stored experiment config differs from the frozen manifest"
                raise RuntimeError(message)
            return int(experiment.experiment_id)

    def _registered_trial_ids_for_audit(
        self,
        *,
        experiment_id: int,
        manifest_hash: str,
        manifest: Mapping[str, Any],
        trial_specs: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[int, str], dict[str, int]]:
        """Verify the exact immutable DB trial ledger without repairing it."""

        strategy = self._mapping(manifest, "strategy")
        strategy_id = str(strategy["strategy_id"])
        strategy_semver = str(strategy["strategy_version"])
        with Session(self._engine) as session:
            version = session.execute(
                select(StrategyVersion).where(
                    StrategyVersion.strategy_id == strategy_id,
                    StrategyVersion.semver == strategy_semver,
                )
            ).scalar_one_or_none()
            if version is None:
                message = (
                    "audit requires the historical strategy-version identity: "
                    f"{strategy_id}@{strategy_semver}"
                )
                raise RuntimeError(message)
            rows = list(
                session.execute(
                    select(BacktestTrial).where(BacktestTrial.experiment_id == experiment_id)
                ).scalars()
            )

        if len(rows) != len(trial_specs):
            message = (
                "stored trial count differs from frozen registry: "
                f"observed {len(rows)}, expected {len(trial_specs)}"
            )
            raise RuntimeError(message)
        by_sequence = {int(row.sequence): row for row in rows}
        if len(by_sequence) != len(rows):
            message = "stored trial registry contains duplicate sequences"
            raise RuntimeError(message)

        trial_ids: dict[int, str] = {}
        statuses: list[str] = []
        valid_statuses = {status.value for status in BacktestTrialStatus}
        for spec in trial_specs:
            sequence = int(spec["sequence"])
            row = by_sequence.get(sequence)
            if row is None:
                message = f"stored trial registry is missing sequence {sequence}"
                raise RuntimeError(message)
            registration = BacktestTrialRegistration(
                experiment_id=experiment_id,
                strategy_id=strategy_id,
                strategy_semver=strategy_semver,
                sequence=sequence,
                trial_family=str(spec["trial_family"]),
                fold_id=str(spec["fold_id"]),
                cost_scenario=str(spec["cost_scenario"]),
                parameters=dict(self._mapping(spec, "parameters")),
                manifest_hash=manifest_hash,
            )
            expected_trial_id = stable_trial_id(registration)
            expected_fields = {
                "trial_id": expected_trial_id,
                "strategy_id": strategy_id,
                "strat_ver_id": version.strat_ver_id,
                "trial_family": registration.trial_family,
                "fold_id": registration.fold_id,
                "cost_scenario": registration.cost_scenario,
                "parameters": registration.parameters,
                "manifest_hash": manifest_hash,
            }
            differing = [
                field
                for field, expected_value in expected_fields.items()
                if getattr(row, field) != expected_value
            ]
            if differing:
                message = (
                    f"stored trial sequence {sequence} differs from frozen identity: "
                    f"{', '.join(differing)}"
                )
                raise RuntimeError(message)
            status = str(row.status)
            if status not in valid_statuses:
                message = f"stored trial sequence {sequence} has invalid status {status!r}"
                raise RuntimeError(message)
            if status == BacktestTrialStatus.COMPLETED.value and row.result_id is None:
                message = f"completed trial sequence {sequence} has no result"
                raise RuntimeError(message)
            if status != BacktestTrialStatus.COMPLETED.value and row.result_id is not None:
                message = f"non-completed trial sequence {sequence} references a result"
                raise RuntimeError(message)
            if (
                spec.get("registration_status") == BacktestTrialStatus.ABANDONED.value
                and status != BacktestTrialStatus.ABANDONED.value
            ):
                message = f"predeclared abandoned trial sequence {sequence} is {status}"
                raise RuntimeError(message)
            trial_ids[sequence] = str(row.trial_id)
            statuses.append(status)

        return trial_ids, dict(Counter(statuses))

    def _get_or_create_experiment(self, manifest: Mapping[str, Any], manifest_hash: str) -> int:
        strategy_id = str(self._mapping(manifest, "strategy")["strategy_id"])
        with Session(self._engine) as session:
            rows = session.execute(
                select(BacktestExperiment).where(BacktestExperiment.strategy_id == strategy_id)
            ).scalars()
            matches = [
                row for row in rows if (row.config or {}).get("manifest_hash") == manifest_hash
            ]
            if len(matches) > 1:
                message = f"Multiple experiments reference immutable manifest {manifest_hash}"
                raise RuntimeError(message)
            if matches:
                return int(matches[0].experiment_id)
            experiment = BacktestExperiment(
                strategy_id=strategy_id,
                experiment_type="parameter_sweep",
                metric="pooled_expected_cost_risk_adjusted_return",
                status="running",
                config={
                    "manifest_hash": manifest_hash,
                    "protocol_id": manifest["protocol_id"],
                    "selection_contaminated": True,
                    "validation_design": manifest["validation_design"],
                },
                started_at=datetime.now(tz=UTC),
            )
            session.add(experiment)
            session.commit()
            return int(experiment.experiment_id)

    def _register_all_trials(
        self,
        *,
        experiment_id: int,
        manifest_hash: str,
        manifest: Mapping[str, Any],
        trial_specs: Sequence[Mapping[str, Any]],
    ) -> dict[int, str]:
        strategy = self._mapping(manifest, "strategy")
        registered: dict[int, str] = {}
        for spec in trial_specs:
            sequence = int(spec["sequence"])
            trial_id = self._trial_store.register(
                BacktestTrialRegistration(
                    experiment_id=experiment_id,
                    strategy_id=str(strategy["strategy_id"]),
                    strategy_semver=str(strategy["strategy_version"]),
                    sequence=sequence,
                    trial_family=str(spec["trial_family"]),
                    fold_id=str(spec["fold_id"]),
                    cost_scenario=str(spec["cost_scenario"]),
                    parameters=dict(self._mapping(spec, "parameters")),
                    manifest_hash=manifest_hash,
                )
            )
            registered[sequence] = trial_id
            if spec.get("registration_status") == BacktestTrialStatus.ABANDONED.value:
                self._trial_store.mark_abandoned(
                    trial_id,
                    error_context=str(spec["abandonment_reason"]),
                )
        return registered

    def _refresh_experiment_summary(self, experiment_id: int) -> dict[str, int]:
        with Session(self._engine) as session:
            experiment = session.get(BacktestExperiment, experiment_id)
            if experiment is None:
                message = f"Backtest experiment disappeared: {experiment_id}"
                raise RuntimeError(message)
            statuses = session.execute(
                select(BacktestTrial.status).where(BacktestTrial.experiment_id == experiment_id)
            ).scalars()
            counts = dict(Counter(str(status) for status in statuses))
            pending = counts.get(BacktestTrialStatus.REGISTERED.value, 0) + counts.get(
                BacktestTrialStatus.RUNNING.value, 0
            )
            failed = counts.get(BacktestTrialStatus.FAILED.value, 0) + counts.get(
                BacktestTrialStatus.INTERRUPTED.value, 0
            )
            experiment.status = "running" if pending else ("failed" if failed else "completed")
            experiment.summary = {
                "historical_selection_contaminated": True,
                "status_counts": dict(sorted(counts.items())),
            }
            experiment.completed_at = None if pending else datetime.now(tz=UTC)
            session.commit()
            return counts

    def _trial_status(self, trial_id: str) -> str:
        with Session(self._engine) as session:
            status = session.execute(
                select(BacktestTrial.status).where(BacktestTrial.trial_id == trial_id)
            ).scalar_one()
            return str(status)

    @staticmethod
    def _select_trials(
        specs: Sequence[dict[str, Any]],
        *,
        arm_ids: Sequence[str],
        fold_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        registered_arms = {str(spec["arm_id"]) for spec in specs}
        registered_folds = {str(spec["fold_id"]) for spec in specs}
        unknown_arms = set(arm_ids) - registered_arms
        unknown_folds = set(fold_ids) - registered_folds
        if unknown_arms:
            message = f"Unregistered parameter arm(s): {', '.join(sorted(unknown_arms))}"
            raise ValueError(message)
        if unknown_folds:
            message = f"Unregistered fold(s): {', '.join(sorted(unknown_folds))}"
            raise ValueError(message)
        return [
            spec
            for spec in specs
            if (not arm_ids or spec["arm_id"] in arm_ids)
            and (not fold_ids or spec["fold_id"] in fold_ids)
        ]

    def _prevalidate_filters(
        self,
        protocol: Mapping[str, Any],
        *,
        arm_ids: Sequence[str],
        fold_ids: Sequence[str],
    ) -> None:
        registered_arms = {
            str(arm["id"]) for arm in self._list_of_mappings(protocol, "signal_rule_arms")
        }
        primary = self._mapping(self._mapping(protocol, "selection"), "primary_trial_family")
        registered_arms.add(str(primary["arm_id"]))
        registered_arms.update(str(arm["id"]) for arm in self._semantic_contracts(protocol))
        registered_arms.update(
            arm_id for arm_id, _name, _parameters in self._benchmark_contracts(protocol)
        )
        registered_arms.update(
            {
                _ROBUSTNESS_ARM,
                _PBO_ARM,
                _SCORING_LEDGER_ARM,
                _HISTORICAL_DISPOSITION_ARM,
            }
        )
        registered_arms.add(self._registered_redesign_trial_contract(protocol)["arm_id"])
        registered_arms.update(
            str(arm["id"])
            for arm in self._list_of_mappings(
                self._mapping(protocol, "conditional_sizing_trials"), "arms"
            )
        )
        effects = self._mapping(protocol, "power").get("minimum_standardized_effect_sensitivities")
        if isinstance(effects, (str, bytes)) or not isinstance(effects, Sequence):
            message = "power sensitivities must be a JSON array"
            raise TypeError(message)
        registered_arms.update(self._power_arm_id(float(effect)) for effect in effects)

        registered_folds = {
            "full-panel",
            "pooled-oos",
            "asset-mismatch",
            "cscv",
            "prospective-design",
            "historical-disposition",
        }
        registered_folds.update(
            f"oos-{int(fold['oos_year'])}" for fold in self._list_of_mappings(protocol, "folds")
        )
        unknown_arms = set(arm_ids) - registered_arms
        unknown_folds = set(fold_ids) - registered_folds
        if unknown_arms:
            message = f"Unregistered parameter arm(s): {', '.join(sorted(unknown_arms))}"
            raise ValueError(message)
        if unknown_folds:
            message = f"Unregistered fold(s): {', '.join(sorted(unknown_folds))}"
            raise ValueError(message)

    @staticmethod
    def _require_engine_capabilities() -> None:
        declared = getattr(BacktestEngine, "validation_capabilities", ())
        if isinstance(declared, Mapping):
            capabilities = frozenset(key for key, enabled in declared.items() if enabled is True)
        else:
            capabilities = frozenset(declared)
        missing = _REQUIRED_ENGINE_CAPABILITIES - capabilities
        if missing:
            message = (
                "Backtest engine cannot represent the frozen primary validation contract; "
                f"missing capabilities: {', '.join(sorted(missing))}"
            )
            raise RuntimeError(message)

    @staticmethod
    def _require_phase_one_runner_capabilities(
        specs: Sequence[Mapping[str, Any]],
    ) -> None:
        observed = {str(spec.get("runner_kind")) for spec in specs}
        unsupported = sorted(observed - _PHASE_ONE_RUNNERS)
        if unsupported:
            message = (
                "phase-1 outcome dispatch does not implement runner kind(s): "
                f"{', '.join(unsupported)}"
            )
            raise RuntimeError(message)

    def _has_executable_work(
        self,
        specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
    ) -> bool:
        executable = _PHASE_ONE_INDIVIDUAL_RUNNERS | _PHASE_ONE_GROUP_RUNNERS
        return any(
            spec.get("runner_kind") in executable
            and self._trial_status(trial_ids[int(spec["sequence"])]) not in _TERMINAL_STATUSES
            for spec in specs
        )

    def _audit_completed_evidence(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
        permit_historical_runner_fallback: bool = False,
    ) -> None:
        """Content-verify every completed selected checkpoint on every resume."""

        completed_selected = False
        for spec in specs:
            trial_id = trial_ids[int(spec["sequence"])]
            if self._trial_status(trial_id) != BacktestTrialStatus.COMPLETED.value:
                continue
            completed_selected = True
            runner_kind = str(spec.get("runner_kind"))
            if runner_kind in _PHASE_ONE_INDIVIDUAL_RUNNERS:
                self._result_store.verified_report_sha256(trial_id)
            elif runner_kind == _HISTORICAL_DISPOSITION_RUNNER:
                if permit_historical_runner_fallback:
                    self._audit_retired_runner_result(
                        spec,
                        trial_id=trial_id,
                        manifest=manifest,
                    )
                else:
                    self._reaudit_completed_historical_disposition(
                        spec,
                        all_specs=all_specs,
                        trial_ids=trial_ids,
                        manifest=manifest,
                    )
            elif runner_kind in _PHASE_ONE_GROUP_RUNNERS:
                payload = self._result_store.load_derived_payload(
                    trial_id,
                    expected_evidence_kind=self._expected_derived_evidence_kind(runner_kind),
                )
                if payload.get("runner_kind") != runner_kind:
                    message = "completed derived result runner kind differs"
                    raise RuntimeError(message)
                if runner_kind in _SOURCE_ATTESTED_GROUP_RUNNERS:
                    self._audit_source_attestation(
                        payload,
                        manifest=manifest,
                        visited_derived=set(),
                    )
            elif permit_historical_runner_fallback:
                self._audit_retired_runner_result(
                    spec,
                    trial_id=trial_id,
                    manifest=manifest,
                )
            else:
                message = f"completed trial has no verifiable evidence contract: {runner_kind}"
                raise RuntimeError(message)

        source_fold_ids: set[str] = set()
        selected_runners = {str(spec.get("runner_kind")) for spec in specs}
        if completed_selected and selected_runners & _POWER_GROUP_RUNNERS:
            source_fold_ids.add("full-panel")
        if completed_selected and selected_runners & (
            _SCORING_LEDGER_GROUP_RUNNERS | _CONDITIONAL_SIZING_GROUP_RUNNERS
        ):
            source_fold_ids.update(
                str(spec["fold_id"])
                for spec in all_specs
                if spec.get("runner_kind") == "production_core_direct"
                and spec.get("fold_id") != "full-panel"
            )
        baseline_arm_id = self._historical_disposition_policy(manifest).baseline_arm_id
        for source_spec in all_specs:
            if (
                source_spec.get("runner_kind") == "production_core_direct"
                and source_spec.get("arm_id") == baseline_arm_id
                and source_spec.get("cost_scenario") == "expected"
                and str(source_spec.get("fold_id")) in source_fold_ids
            ):
                self._result_store.verified_report_sha256(trial_ids[int(source_spec["sequence"])])

    def _audit_retired_runner_result(
        self,
        spec: Mapping[str, Any],
        *,
        trial_id: str,
        manifest: Mapping[str, Any],
    ) -> None:
        """Verify a pruned runner's persisted envelope without executing its code."""

        with Session(self._engine) as session:
            trial = session.get(BacktestTrial, trial_id)
            if trial is None or trial.result_id is None:
                message = "completed retired-runner trial has no linked result"
                raise RuntimeError(message)
            result = session.get(BacktestResult, trial.result_id)
            if result is None or not isinstance(result.meta, dict):
                message = "completed retired-runner result is missing or malformed"
                raise RuntimeError(message)
            metadata = dict(result.meta)
        if "evidence_sha256" in metadata:
            payload = self._result_store.load_derived_payload(trial_id)
            payload_runner = payload.get("runner_kind")
            if payload_runner is not None and payload_runner != spec.get("runner_kind"):
                message = "retired-runner evidence payload identifies a different runner"
                raise RuntimeError(message)
            if isinstance(payload.get("source_attestation"), Mapping):
                self._audit_source_attestation(
                    payload,
                    manifest=manifest,
                    visited_derived=set(),
                )
            if spec.get("runner_kind") == _HISTORICAL_DISPOSITION_RUNNER:
                authority = self._mapping(payload, "authority")
                if any(
                    authority.get(field) is not False
                    for field in (
                        "paper_trading_authorized",
                        "live_trading_authorized",
                        "automatic_parameter_deployment_authorized",
                    )
                ):
                    message = "retired historical disposition attempts to authorize trading"
                    raise RuntimeError(message)
            return
        if "report_sha256" in metadata:
            self._result_store.verified_report_sha256(trial_id)
            return
        message = "completed retired-runner result has no verifiable evidence hash"
        raise RuntimeError(message)

    def _reaudit_completed_historical_disposition(
        self,
        spec: Mapping[str, Any],
        *,
        all_specs: Sequence[Mapping[str, Any]],
        trial_ids: Mapping[int, str],
        manifest: Mapping[str, Any],
    ) -> None:
        """Re-derive the hierarchy without re-running strategy trials."""

        trial_id = trial_ids[int(spec["sequence"])]
        stored = self._result_store.load_derived_payload(
            trial_id,
            expected_evidence_kind=_HISTORICAL_DISPOSITION_RUNNER,
        )
        with Session(self._engine) as session:
            trial = session.get(BacktestTrial, trial_id)
            if trial is None:
                message = "completed historical disposition trial disappeared"
                raise RuntimeError(message)
            experiment_id = int(trial.experiment_id)
        ordered = sorted(all_specs, key=lambda item: int(item["sequence"]))
        if not ordered or ordered[-1] != spec:
            message = "completed historical disposition is not the final trial"
            raise RuntimeError(message)
        records = self._build_historical_disposition_evidence(
            [spec],
            all_specs=ordered,
            trial_ids=trial_ids,
            experiment_id=experiment_id,
            manifest=manifest,
        )
        if len(records) != 1:
            message = "historical disposition re-derivation did not produce one record"
            raise RuntimeError(message)
        expected = records[0][1].payload
        if canonical_manifest_bytes(stored) != canonical_manifest_bytes(expected):
            message = "completed historical disposition differs from re-derived evidence"
            raise RuntimeError(message)
        authority = self._mapping(stored, "authority")
        if any(
            authority.get(field) is not False
            for field in (
                "paper_trading_authorized",
                "live_trading_authorized",
                "automatic_parameter_deployment_authorized",
            )
        ):
            message = "completed historical disposition attempts to authorize trading"
            raise RuntimeError(message)

    def _audit_source_attestation(
        self,
        payload: Mapping[str, Any],
        *,
        manifest: Mapping[str, Any],
        visited_derived: set[tuple[str, str]],
    ) -> None:
        """Recursively re-resolve every immutable source bound by a payload."""

        raw_attestation = payload.get("source_attestation")
        if not isinstance(raw_attestation, Mapping):
            message = "completed derived evidence has no source attestation"
            raise TypeError(message)
        attestation = self._normalized_source_attestation(
            report_sources=cast(
                Sequence[Mapping[str, Any]],
                raw_attestation.get("report_evidence", ()),
            ),
            derived_sources=cast(
                Sequence[Mapping[str, Any]],
                raw_attestation.get("derived_evidence", ()),
            ),
            dataset_sources=cast(
                Sequence[Mapping[str, Any]],
                raw_attestation.get("dataset_evidence", ()),
            ),
            embedded_sources=cast(
                Sequence[Mapping[str, Any]],
                raw_attestation.get("embedded_evidence", ()),
            ),
        )
        if dict(raw_attestation) != attestation:
            message = "completed derived evidence source attestation hash differs"
            raise RuntimeError(message)

        for source in attestation["report_evidence"]:
            observed = self._result_store.verified_report_sha256(source["trial_id"])
            if observed != source["report_sha256"]:
                message = f"completed source report hash differs: {source['trial_id']}"
                raise RuntimeError(message)

        for source in attestation["derived_evidence"]:
            identity = (source["trial_id"], source["evidence_kind"])
            if identity in visited_derived:
                continue
            visited_derived.add(identity)
            derived_payload = self._result_store.load_derived_payload(
                source["trial_id"],
                expected_evidence_kind=source["evidence_kind"],
            )
            observed = manifest_sha256({"payload": derived_payload})
            if observed != source["evidence_payload_sha256"]:
                message = f"completed source derived hash differs: {source['trial_id']}"
                raise RuntimeError(message)
            if isinstance(derived_payload.get("source_attestation"), Mapping):
                self._audit_source_attestation(
                    derived_payload,
                    manifest=manifest,
                    visited_derived=visited_derived,
                )

        manifest_datasets = {
            str(dataset["dataset_id"]): str(self._mapping(dataset, "audit")["ohlcv_sha256"])
            for dataset in self._list_of_mappings(self._mapping(manifest, "data"), "datasets")
        }
        for source in attestation["dataset_evidence"]:
            if manifest_datasets.get(source["dataset_id"]) != source["ohlcv_sha256"]:
                message = f"completed source dataset hash differs: {source['dataset_id']}"
                raise RuntimeError(message)

        for source in attestation["embedded_evidence"]:
            field = source["manifest_field"]
            if field != "upstream_selection_ledger":
                message = f"unsupported embedded source field: {field}"
                raise RuntimeError(message)
            embedded = self._mapping(manifest, field)
            if manifest_sha256(embedded) != source["evidence_payload_sha256"]:
                message = f"completed embedded source hash differs: {field}"
                raise RuntimeError(message)
            embedded_source = self._mapping(embedded, "source")
            if embedded_source.get("sha256") != source["source_sha256"]:
                message = f"completed embedded source origin differs: {field}"
                raise RuntimeError(message)

    def _product_cost(
        self,
        manifest: Mapping[str, Any],
        scenario_name: str,
        product: str,
    ) -> dict[str, Any]:
        scenarios = self._mapping(manifest, "cost_scenarios")
        scenario = self._require_mapping(
            scenarios.get(scenario_name), field=f"cost_scenarios.{scenario_name}"
        )
        product_cost = self._require_mapping(
            self._mapping(scenario, "products").get(product),
            field=f"cost_scenarios.{scenario_name}.products.{product}",
        )
        return {
            "commission_bps": scenario["commission_bps"],
            "slippage_bps": scenario["slippage_bps"],
            "fill_delay_bars": scenario["fill_delay_bars"],
            "half_spread_bps": product_cost["half_spread_bps"],
            "impact_bps": product_cost["impact_bps"],
        }

    def _require_within_repo(self, path: Path) -> None:
        try:
            path.relative_to(self._repo_root)
        except ValueError as exc:
            message = f"strategy path must be inside repository root: {path}"
            raise ValueError(message) from exc

    def _relative(self, path: Path) -> str:
        return str(path.resolve().relative_to(self._repo_root))

    @classmethod
    def _mapping(cls, parent: Mapping[str, Any], key: str) -> dict[str, Any]:
        return cls._require_mapping(parent.get(key), field=key)

    @staticmethod
    def _require_mapping(value: Any, *, field: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            message = f"{field} must be a JSON object"
            raise TypeError(message)
        return dict(value)

    @staticmethod
    def _list_of_mappings(parent: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
        value = parent.get(key)
        if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
            message = f"{key} must be a list of JSON objects"
            raise TypeError(message)
        return [dict(item) for item in value]

    @staticmethod
    def _nonempty_string(value: Any, *, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            message = f"{field} must be a non-empty string"
            raise ValueError(message)
        return value.strip()

    @classmethod
    def _common_dataset_value(cls, manifest: Mapping[str, Any], field: str) -> str:
        values = {
            cls._nonempty_string(dataset.get(field), field=f"dataset.{field}")
            for dataset in cls._list_of_mappings(cls._mapping(manifest, "data"), "datasets")
        }
        if len(values) != 1:
            message = f"campaign datasets must share one {field}"
            raise ValueError(message)
        return values.pop()

    @staticmethod
    def _decimal(value: Any, *, field: str) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            message = f"{field} must be a finite decimal"
            raise TypeError(message)
        try:
            parsed = Decimal(str(value))
        except InvalidOperation as exc:
            message = f"{field} must be a finite decimal"
            raise ValueError(message) from exc
        if not parsed.is_finite():
            message = f"{field} must be a finite decimal"
            raise ValueError(message)
        return parsed

    @staticmethod
    def _safe_error_context(exc: Exception) -> str:
        message = str(exc).replace("\n", " ").strip() or "trial failed without an error message"
        message = re.sub(r"([a-z][a-z0-9+.-]*://)[^@\s]+@", r"\1<redacted>@", message)
        return message[:4000]
