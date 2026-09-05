"""Fail-closed protocol and frozen-manifest validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from dev_cli.validation.campaign_composition import CampaignMixinContract
from dev_cli.validation.campaign_contracts import (
    _ABSOLUTE_RISK_GATE_IDS,
    _HISTORICAL_ALLOWED_DISPOSITIONS,
    _HISTORICAL_DISPOSITION_RUNNER,
    _HISTORICAL_EVIDENCE_REQUIREMENT_VOCABULARY,
    _HISTORICAL_PROMOTION_CONTRACT,
    _PARITY_DAYS_PER_STRATUM,
    _REDESIGN_QUANTITATIVE_GATE_IDS,
    _SHA256_RE,
    _SIZING_RAW_GATE_IDS,
)
from dev_cli.validation.correctness import (
    CorrectnessFindingClass,
    CorrectnessFindingSeverity,
    CorrectnessFindingStatus,
    verify_registered_strategy_correctness_attestation,
)
from dev_cli.validation.disposition import (
    HistoricalDispositionPolicy,
)
from dev_cli.validation.evidence import (
    atomic_create_bytes,
    parse_utc_datetime,
)
from dev_cli.validation.persistence.backtest_manifest_store import (
    canonical_manifest_bytes,
    manifest_sha256,
)
from dev_cli.validation.persistence.backtest_selection_ledger import (
    validate_embedded_selection_ledger,
    validate_upstream_selection_ledger_contract,
    validated_prior_diagnostic_attempt_count,
)
from dev_cli.validation.persistence.backtest_trial_store import (
    BacktestTrialStatus,
)

_CURRENT_ZERO_ACTIVITY_SHARPE_CONTRACT = {
    "metric_callable": "dev_cli.validation.backtest.metrics.sharpe_ratio",
    "complete_aligned_zero_return_zero_variance_annualized_sharpe": 0.0,
    "missing_corrupt_or_misaligned": "BLOCKED_INSUFFICIENT_EVIDENCE",
    "baseline_zero_activity_disposition_gate": "separate_RETIRE_gate",
}
_LEGACY_ZERO_ACTIVITY_SHARPE_CONTRACT = {
    "metric_callable": "lib_strategy.backtest.metrics.sharpe_ratio",
    "complete_aligned_zero_return_zero_variance_annualized_sharpe": 0.0,
    "missing_corrupt_or_misaligned": "BLOCKED_INSUFFICIENT_EVIDENCE",
    "b0_zero_activity_disposition_gate": "separate_RETIRE_gate",
}


class CampaignProtocolValidationMixin(CampaignMixinContract):
    def _validate_protocol(self, protocol: Mapping[str, Any]) -> None:
        status = self._nonempty_string(protocol.get("status"), field="status")
        if not status.startswith("frozen"):
            message = f"Validation protocol is not frozen: {status}"
            raise ValueError(message)
        self._validate_design_contract(protocol)
        self._validate_operational_state_contract(protocol)
        evidence = self._mapping(protocol, "evidence_boundary")
        if not evidence.get("selection_contaminated_through"):
            message = "historical selection contamination boundary is required"
            raise ValueError(message)
        self._validate_upstream_selection_contract(protocol)
        self._validated_regime_assignment(self._mapping(protocol, "robustness"))
        self._mapping(protocol, "portfolio_assumptions")
        self._validate_data_timestamp_contract(protocol)
        products = self._validate_product_contract(protocol)
        self._validate_data_parity_contract(protocol, products=products)
        self._validate_cost_measurement_contract(protocol, products=products)
        self._validate_cost_contract(protocol, products=products)
        arms = self._validate_selection_contract(protocol)
        self._validate_fold_contract(protocol)
        self._validate_historical_disposition_cross_contract(protocol)
        if not arms:
            message = "at least one registered signal-rule arm is required"
            raise ValueError(message)

    def _validate_operational_state_contract(self, protocol: Mapping[str, Any]) -> None:
        strategy = self._mapping(protocol, "strategy")
        version = strategy.get("operational_state_contract_version")
        if version is None:
            return
        if isinstance(version, bool) or version != 1:
            message = "strategy.operational_state_contract_version must equal 1"
            raise ValueError(message)
        for field in (
            "paper_binding_expected_active",
            "paper_binding_expected_autopilot",
            "paper_strategy_config_expected_active",
            "require_stop_loss",
            "require_explicit_scoring_inputs",
        ):
            if not isinstance(strategy.get(field), bool):
                message = f"strategy.{field} must be an explicit bool"
                raise TypeError(message)

    def _validate_historical_disposition_cross_contract(
        self,
        protocol: Mapping[str, Any],
    ) -> None:
        """Fail before freeze when disposition policy drifts from its producers."""

        policy = self._historical_disposition_policy(protocol)
        self._validate_historical_evidence_authority_contract(protocol, policy=policy)
        self._validate_historical_quantitative_gate_contract(protocol, policy=policy)
        self._validate_historical_redesign_candidate_contract(protocol, policy=policy)
        self._validate_historical_semantic_contract(protocol, policy=policy)
        self._validate_historical_power_contract(protocol, policy=policy)

    @staticmethod
    def _validate_historical_evidence_authority_contract(
        protocol: Mapping[str, Any],
        *,
        policy: HistoricalDispositionPolicy,
    ) -> None:
        observed_requirements = tuple(
            (
                requirement.check_id,
                requirement.kind.value,
                requirement.required_source_ids,
                requirement.allows_verified_empty,
            )
            for requirement in policy.required_evidence
        )
        if observed_requirements != _HISTORICAL_EVIDENCE_REQUIREMENT_VOCABULARY:
            message = (
                "historical evidence requirements differ from the exact ordered "
                "check/kind/source/empty vocabulary"
            )
            raise ValueError(message)
        allowed_dispositions = protocol.get("allowed_dispositions")
        if (
            not isinstance(allowed_dispositions, list)
            or tuple(allowed_dispositions) != _HISTORICAL_ALLOWED_DISPOSITIONS
        ):
            message = "historical allowed dispositions differ from the executable hierarchy"
            raise ValueError(message)
        if protocol.get("promotion") != _HISTORICAL_PROMOTION_CONTRACT:
            message = "historical promotion contract does not fail closed"
            raise ValueError(message)

    def _validate_historical_quantitative_gate_contract(
        self,
        protocol: Mapping[str, Any],
        *,
        policy: HistoricalDispositionPolicy,
    ) -> None:
        design = self._mapping(protocol, "validation_design")
        if policy.baseline_arm_id != design.get("shipped_version_shadow_freeze_arm"):
            message = "historical disposition baseline arm differs from the shipped freeze arm"
            raise ValueError(message)
        if policy.baseline_quantitative_gate_ids != _SIZING_RAW_GATE_IDS:
            message = "historical baseline gates differ from the robustness producer vocabulary"
            raise ValueError(message)
        expected_economic_gate_ids = tuple(
            gate_id for gate_id in _SIZING_RAW_GATE_IDS if gate_id not in _ABSOLUTE_RISK_GATE_IDS
        )
        if policy.baseline_economic_gate_ids != expected_economic_gate_ids:
            message = (
                "historical baseline economic gates must be the exact ordered "
                "non-risk complement of the robustness producer vocabulary"
            )
            raise ValueError(message)
        if policy.redesign_quantitative_gate_ids != _REDESIGN_QUANTITATIVE_GATE_IDS:
            message = "historical redesign gates differ from the diagnostics producer vocabulary"
            raise ValueError(message)
        for field, gates in (
            ("baseline", policy.baseline_quantitative_gate_ids),
            ("redesign", policy.redesign_quantitative_gate_ids),
        ):
            if not set(_ABSOLUTE_RISK_GATE_IDS).issubset(gates):
                message = f"historical {field} policy omits an absolute-risk gate"
                raise ValueError(message)

    def _validate_historical_redesign_candidate_contract(
        self,
        protocol: Mapping[str, Any],
        *,
        policy: HistoricalDispositionPolicy,
    ) -> None:
        redesign_ids = tuple(
            str(item["candidate_id"])
            for item in self._registered_redesign_change_contracts(protocol)
        )
        if policy.registered_redesign_candidate_ids != redesign_ids:
            message = "historical redesign candidate IDs differ from registered one-factor arms"
            raise ValueError(message)

    def _validate_historical_semantic_contract(
        self,
        protocol: Mapping[str, Any],
        *,
        policy: HistoricalDispositionPolicy,
    ) -> None:
        semantic_contracts = self._semantic_contracts(protocol)
        semantic_ids = tuple(str(contract["id"]) for contract in semantic_contracts)
        if policy.registered_semantic_candidate_ids != semantic_ids:
            message = "historical semantic IDs differ from registered semantic contracts"
            raise ValueError(message)
        finding_contracts = {
            str(finding["id"]): finding
            for finding in self._list_of_mappings(
                self._mapping(protocol, "correctness_attestation"),
                "findings",
            )
        }
        expected_correctable: list[str] = []
        for contract in semantic_contracts:
            finding_id = str(contract["correctness_finding_id"])
            finding = finding_contracts.get(finding_id)
            if finding is None or finding.get("finding_class") != (
                CorrectnessFindingClass.STRATEGY_SEMANTIC.value
            ):
                message = f"semantic contract has no exact strategy finding: {finding_id}"
                raise ValueError(message)
            if finding.get("status") == CorrectnessFindingStatus.REMEDIATION_REQUIRED.value:
                expected_correctable.append(str(contract["id"]))
        if policy.correctable_semantic_candidate_ids != tuple(expected_correctable):
            message = (
                "historical correctable semantic IDs differ from remediation-required "
                "correctness mappings"
            )
            raise ValueError(message)
        semantic_finding_ids = {
            str(contract["correctness_finding_id"]) for contract in semantic_contracts
        }
        open_statuses = {
            CorrectnessFindingStatus.REMEDIATION_REQUIRED.value,
            CorrectnessFindingStatus.UNRESOLVED.value,
        }
        high_severities = {
            CorrectnessFindingSeverity.HIGH.value,
            CorrectnessFindingSeverity.CRITICAL.value,
        }
        expected_promotion_blockers = tuple(
            finding_id
            for finding_id, finding in finding_contracts.items()
            if (
                finding.get("finding_class") == CorrectnessFindingClass.STRATEGY_SEMANTIC.value
                and finding.get("status") in open_statuses
                and finding.get("severity") in high_severities
                and finding_id not in semantic_finding_ids
            )
        )
        if policy.registered_promotion_blocker_finding_ids != expected_promotion_blockers:
            message = (
                "historical promotion-blocker IDs differ from open high/critical "
                "correctness findings without semantic candidate mappings"
            )
            raise ValueError(message)
        expected_high_triggers = tuple(
            str(contract["id"])
            for contract in semantic_contracts
            if (
                finding_contracts[str(contract["correctness_finding_id"])].get("status")
                == CorrectnessFindingStatus.REMEDIATION_REQUIRED.value
                and finding_contracts[str(contract["correctness_finding_id"])].get("severity")
                == CorrectnessFindingSeverity.HIGH.value
                and str(contract["id"]) in policy.correctable_semantic_candidate_ids
            )
        )
        semantic_gate = self._mapping(
            self._mapping(protocol, "historical_disposition_gates"),
            "semantic_redesign_gate",
        )
        for field, expected in (
            ("high_severity_trigger_candidate_set", expected_high_triggers),
            ("carried_remediation_candidate_set", tuple(expected_correctable)),
        ):
            raw = semantic_gate.get(field)
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                message = f"historical semantic gate {field} must be an array"
                raise TypeError(message)
            if tuple(raw) != expected:
                message = f"historical semantic gate {field} differs from correctness mappings"
                raise ValueError(message)

    def _validate_historical_power_contract(
        self,
        protocol: Mapping[str, Any],
        *,
        policy: HistoricalDispositionPolicy,
    ) -> None:
        power = self._mapping(protocol, "power")
        primary_effect = float(power["minimum_standardized_effect_primary"])
        if policy.prospective_primary_effect_id != self._power_arm_id(
            primary_effect
        ) or policy.maximum_prospective_duration_years != float(power["maximum_duration_years"]):
            message = "historical disposition power contract differs from the producer"
            raise ValueError(message)

    def _validate_upstream_selection_contract(self, protocol: Mapping[str, Any]) -> None:
        upstream = self._mapping(
            self._mapping(protocol, "evidence_boundary"),
            "upstream_selection",
        )
        multiple_testing = self._mapping(protocol, "multiple_testing")
        ledger_contract = self._mapping(
            multiple_testing,
            "upstream_selection_ledger_contract",
        )
        validate_upstream_selection_ledger_contract(ledger_contract)
        expected_rows = int(ledger_contract["expected_row_count"])
        expected_sha256 = str(ledger_contract["expected_sha256"])
        prior_diagnostic_attempts = validated_prior_diagnostic_attempt_count(
            protocol,
            repo_root=(
                self._repo_root
                if multiple_testing.get("prior_diagnostic_attempts") is not None
                else None
            ),
        )
        arms = self._list_of_mappings(protocol, "signal_rule_arms")
        semantic_contracts = self._semantic_contracts(protocol)
        executable_semantic_ids = tuple(
            str(arm["id"])
            for arm in semantic_contracts
            if arm.get("execution") == "expected_cost_full_panel_and_direct_oos_diagnostic"
        )
        current_compared = len(arms) + len(executable_semantic_ids)
        if (
            upstream.get("catalogue_campaign_runs") != expected_rows
            or upstream.get("authoritative_ledger_sha256") != expected_sha256
            or upstream.get("ledger_required_before_manifest_freeze") is not True
            or upstream.get("resume_uses_embedded_manifest_ledger") is not True
        ):
            message = "evidence-boundary upstream selection accounting is inconsistent"
            raise ValueError(message)
        if (
            multiple_testing.get("catalogue_campaign_runs_observed") != expected_rows
            or multiple_testing.get("registered_economically_compared_arms") != current_compared
            or multiple_testing.get("registered_signal_rule_arms") != len(arms)
            or multiple_testing.get("registered_executable_semantic_arms")
            != len(executable_semantic_ids)
        ):
            message = "multiple-testing counts differ from the attested trial families"
            raise ValueError(message)
        zero_activity_contract = self._mapping(
            multiple_testing,
            "zero_activity_current_arm_sharpe",
        )
        # Content-addressed protocols frozen before validation moved to vmdev
        # retain the original callable and B0 key.  Accept only that exact
        # legacy contract or the exact current contract; neither is rewritten.
        if zero_activity_contract not in (
            _CURRENT_ZERO_ACTIVITY_SHARPE_CONTRACT,
            _LEGACY_ZERO_ACTIVITY_SHARPE_CONTRACT,
        ):
            message = "zero-activity current-arm Sharpe contract differs"
            raise ValueError(message)
        combination = self._mapping(multiple_testing, "deflated_sharpe_combination")
        upstream_attempted_trials = expected_rows + prior_diagnostic_attempts
        effective_trials = upstream_attempted_trials + current_compared
        if (
            combination.get("upstream_attempted_trial_count") != upstream_attempted_trials
            or combination.get("current_registered_signal_rule_arm_count") != len(arms)
            or combination.get("current_registered_executable_semantic_arm_count")
            != len(executable_semantic_ids)
            or combination.get("current_economically_compared_arm_count") != current_compared
            or combination.get("effective_trial_count") != effective_trials
            or combination.get("historical_selection_contaminated") is not True
            or combination.get("missing_current_arm_value")
            != "BLOCKED_INSUFFICIENT_EVIDENCE_never_drop_or_impute"
        ):
            message = "deflated-Sharpe trial combination is arithmetically inconsistent"
            raise ValueError(message)

    def _validate_design_contract(self, protocol: Mapping[str, Any]) -> None:
        expected_design = {
            "historical_evidence_role": "selection_contaminated_research_only",
            "primary_decision_basis": (
                "pooled_purged_oos_for_ranking_rejection_and_redesign_diagnosis_only"
            ),
            "production_parameter_semantics": (
                "one_fixed_registered_arm_per_frozen_strategy_version"
            ),
            "adaptive_fold_selector_is_not_a_deployable_strategy": True,
            "shipped_version_shadow_freeze_arm": self._nonempty_string(
                self._mapping(protocol, "historical_disposition_policy").get("baseline_arm_id"),
                field="historical_disposition_policy.baseline_arm_id",
            ),
            "non_baseline_winner_disposition": (
                "REDESIGN_as_new_version_and_new_manifest_before_any_prospective_clock"
            ),
            "initial_purge_calendar_days": 60,
            "embargo_bars": 30,
            "calendar_purge_interval": ("[test_start_minus_60_calendar_days,test_start)"),
            "embargo_semantics": (
                "exclude_final_30_otherwise_eligible_training_bars_immediately_before_"
                "calendar_purge"
            ),
            "bootstrap_semantics": (
                "purged_and_embargoed_bars_may_be_used_only_as_non_trading_indicator_preroll"
            ),
            "stateful_training_input_topology": (
                "contiguous_prefix_no_interior_market_bar_deletion"
            ),
            "fold_boundaries": "exact_half_open_timestamps_never_inferred_from_bar_counts",
            "fold_boundary_position": ("flat_after_indicator_bootstrap_before_first_oos_bar"),
        }
        design = self._mapping(protocol, "validation_design")
        if any(design.get(key) != value for key, value in expected_design.items()):
            message = "validation_design does not match the required selection-safe contract"
            raise ValueError(message)

    def _validate_data_timestamp_contract(self, protocol: Mapping[str, Any]) -> None:
        data = self._mapping(protocol, "data")
        self._nonempty_string(data.get("timeframe"), field="data.timeframe")
        warmup_start = parse_utc_datetime(data.get("warmup_start"), field="data.warmup_start")
        source_start = parse_utc_datetime(
            data.get("source_candle_evaluation_start"),
            field="data.source_candle_evaluation_start",
        )
        source_end = parse_utc_datetime(
            data.get("source_candle_evaluation_end_exclusive"),
            field="data.source_candle_evaluation_end_exclusive",
        )
        decision_start = parse_utc_datetime(
            data.get("evaluation_start"), field="data.evaluation_start"
        )
        decision_end = parse_utc_datetime(
            data.get("evaluation_end_exclusive"),
            field="data.evaluation_end_exclusive",
        )
        if not warmup_start < source_start < source_end:
            message = "source-candle evaluation must follow warm-up and have positive duration"
            raise ValueError(message)
        if (
            decision_start != source_start + timedelta(days=1)
            or decision_end != source_end + timedelta(days=1)
            or data.get("evaluation_boundary_basis") != "normalized_completed_bar_close_timestamp"
            or data.get("source_to_decision_transform") != "one_day_period_start_plus_one_day_close"
        ):
            message = "daily source-candle and decision-close boundaries are inconsistent"
            raise ValueError(message)

    def _validate_product_contract(self, protocol: Mapping[str, Any]) -> set[str]:
        product_metadata_fields = {
            "provider_product_id",
            "expected_canonical_book",
            "provider_alias",
            "provider_alias_to",
            "base_currency",
            "settlement_quote",
            "quote_display_symbol",
            "product_type",
            "status",
            "is_disabled",
            "trading_disabled",
            "price_increment",
            "base_increment",
            "quote_increment",
            "dataset_rows",
            "expected_dataset_rows",
            "dataset_sha256",
        }
        for product in self._list_of_mappings(self._mapping(protocol, "data"), "primary_products"):
            missing = product_metadata_fields - set(product)
            if missing:
                message = (
                    f"product metadata snapshot is incomplete for "
                    f"{product.get('requested_product')}: {', '.join(sorted(missing))}"
                )
                raise ValueError(message)
            if product["provider_alias"] != product["expected_canonical_book"]:
                message = f"product alias does not match expected canonical book: {product}"
                raise ValueError(message)
            if product["is_disabled"] is not False or product["trading_disabled"] is not False:
                message = f"frozen product is not tradable: {product['requested_product']}"
                raise ValueError(message)
            if not _SHA256_RE.fullmatch(str(product["dataset_sha256"])):
                message = f"invalid declared dataset digest: {product['requested_product']}"
                raise ValueError(message)
        return {
            self._nonempty_string(item.get("requested_product"), field="requested_product")
            for item in self._list_of_mappings(self._mapping(protocol, "data"), "primary_products")
        }

    def _validate_data_parity_contract(
        self,
        protocol: Mapping[str, Any],
        *,
        products: set[str],
    ) -> None:
        data = self._mapping(protocol, "data")
        contract = self._mapping(data, "minute_parity")
        expected_identity = {
            "required": True,
            "artifact_schema_id": "vynmatrix.coinbase-daily-parity-attestation",
            "artifact_schema_version": 1,
            "base_url": "https://api.coinbase.com",
            "public_candle_endpoint": ("/api/v3/brokerage/market/products/{product_id}/candles"),
            "minute_request_chunk_bars": 300,
            "provider_limit": 350,
            "incomplete_chunk_refetches_required": 2,
            "production_consolidation_minutes": 1_440,
            "stamp_transform": "provider_start_plus_one_day_equals_period_close",
            "ohlc_tolerance": "product_tick",
        }
        if any(contract.get(key) != value for key, value in expected_identity.items()):
            message = "minute-parity provider, endpoint, or consolidation contract differs"
            raise ValueError(message)
        dependency = self._mapping(contract, "strategy_field_dependency")
        if (
            set(dependency) != {"price_and_timestamp_fields", "uses_volume"}
            or dependency.get("price_and_timestamp_fields") != "blocking"
            or not isinstance(dependency.get("uses_volume"), bool)
        ):
            message = "minute-parity strategy field-dependency contract differs"
            raise ValueError(message)
        strategy_uses_volume = bool(dependency["uses_volume"])
        minimum_coverage = float(contract.get("minimum_constituent_coverage", 0.0))
        volume_tolerance = self._mapping(contract, "volume_tolerance")
        if (
            not 0 < minimum_coverage <= 1
            or float(volume_tolerance.get("absolute_base_units", 0.0)) <= 0
            or float(volume_tolerance.get("relative_fraction", 0.0)) <= 0
        ):
            message = "minute-parity coverage or volume tolerance is invalid"
            raise ValueError(message)
        strata = self._list_of_mappings(contract, "calendar_strata")
        if not strata:
            message = "minute-parity contract must contain at least one registered stratum"
            raise ValueError(message)
        stratum_ids: set[str] = set()
        for stratum in strata:
            stratum_id = self._nonempty_string(stratum.get("id"), field="calendar_strata.id")
            start = parse_utc_datetime(stratum.get("start"), field=f"{stratum_id}.start")
            end = parse_utc_datetime(
                stratum.get("end_exclusive"), field=f"{stratum_id}.end_exclusive"
            )
            if stratum_id in stratum_ids or end - start != timedelta(days=_PARITY_DAYS_PER_STRATUM):
                message = "minute-parity strata must be unique exact two-day intervals"
                raise ValueError(message)
            stratum_ids.add(stratum_id)
        exact = self._mapping(contract, "exact_attestation")
        if not str(exact.get("status", "")).startswith("frozen"):
            message = "minute-parity exact attestation is not frozen"
            raise ValueError(message)
        for key in (
            "attestation_sha256",
            "source_input_sha256",
            "consolidation_source_sha256",
        ):
            if not _SHA256_RE.fullmatch(str(exact.get(key, ""))):
                message = f"minute-parity exact_attestation.{key} is not frozen"
                raise ValueError(message)
        parse_utc_datetime(exact.get("measured_at_utc"), field="minute_parity.measured_at_utc")
        summary = self._mapping(exact, "summary")
        expected_bars = len(products) * len(strata) * _PARITY_DAYS_PER_STRATUM
        volume_mismatches = int(summary.get("volume_only_mismatches", -1))
        expected_status = (
            "exact_full_ohlcv_match"
            if volume_mismatches == 0
            else "provider_cross_granularity_volume_exception"
        )
        expected_disposition = (
            "eligible_for_volume_dependent_ohlcv_rules"
            if strategy_uses_volume
            else "eligible_for_volume_independent_price_rules"
        )
        if (
            summary.get("sampled_daily_bars") != expected_bars
            or summary.get("exact_price_and_timestamp_matches") != expected_bars
            or int(summary.get("full_ohlcv_matches", -1)) + volume_mismatches != expected_bars
            or summary.get("blocking_mismatches") != 0
            or (strategy_uses_volume and volume_mismatches != 0)
            or summary.get("status") != expected_status
            or summary.get("campaign_disposition") != expected_disposition
        ):
            message = "minute-parity exact summary is incomplete or blocking"
            raise ValueError(message)
        observed = self._mapping(contract, "observed_reconciliation")
        for field in (
            "sampled_daily_bars",
            "exact_price_and_timestamp_matches",
            "full_ohlcv_matches",
            "volume_only_mismatches",
            "status",
        ):
            if observed.get(field) != summary[field]:
                message = f"minute-parity observed and exact {field} summaries contradict"
                raise ValueError(message)

    def _validate_data_parity_attestation(
        self,
        payload: Mapping[str, Any],
        *,
        protocol: Mapping[str, Any],
        current_source_required: bool = True,
    ) -> None:
        """Verify and reconcile complete public candle evidence to the protocol."""

        contract = self._mapping(self._mapping(protocol, "data"), "minute_parity")
        self._validate_frozen_data_parity_integrity(payload)
        if current_source_required:
            self._data_parity_attestation_verifier(payload)
        data = self._mapping(protocol, "data")
        exact = self._mapping(contract, "exact_attestation")
        identity = {
            "schema_id": "artifact_schema_id",
            "schema_version": "artifact_schema_version",
            "base_url": "base_url",
            "candle_endpoint": "public_candle_endpoint",
        }
        if any(
            payload.get(actual) != contract[registered] for actual, registered in identity.items()
        ):
            message = "data-parity artifact identity or endpoint differs from the protocol"
            raise ValueError(message)
        if (
            payload.get("provider") != "coinbase_advanced_trade"
            or payload.get("endpoint_kind") != "public_advanced_trade"
        ):
            message = "data-parity artifact provider identity differs"
            raise ValueError(message)
        fetch_contract = self._mapping(payload, "fetch_contract")
        if fetch_contract != {
            "minute_granularity": "ONE_MINUTE",
            "daily_granularity": "ONE_DAY",
            "minute_request_chunk_bars": contract["minute_request_chunk_bars"],
            "provider_limit": contract["provider_limit"],
            "incomplete_chunk_refetches_required": contract["incomplete_chunk_refetches_required"],
            "range_semantics": "exact_half_open_after_inclusive_provider_filter",
            "minute_gap_policy": ("retain_never_fill_and_enforce_registered_daily_coverage"),
        }:
            message = "data-parity artifact fetch chunk contract differs"
            raise ValueError(message)
        reconciliation = self._mapping(payload, "reconciliation")
        if (
            reconciliation.get("callable") != "audit_minute_daily_reconciliation"
            or (
                current_source_required
                and (
                    reconciliation.get("module")
                    != "dev_cli.validation.providers.daily_reconciliation"
                    or reconciliation.get("source_path")
                    != "dev_cli/validation/providers/daily_reconciliation.py"
                )
            )
            or payload.get("attestation_sha256") != exact["attestation_sha256"]
            or payload.get("source_input_sha256") != exact["source_input_sha256"]
            or payload.get("measured_at_utc") != exact["measured_at_utc"]
            or reconciliation.get("source_sha256") != exact["consolidation_source_sha256"]
            or payload.get("summary") != exact["summary"]
        ):
            message = "data-parity artifact hashes, timestamp, or summary differ"
            raise ValueError(message)
        if (
            reconciliation.get("production_consolidation_minutes")
            != contract["production_consolidation_minutes"]
            or reconciliation.get("minimum_constituent_coverage")
            != contract["minimum_constituent_coverage"]
            or reconciliation.get("volume_absolute_tolerance")
            != self._mapping(contract, "volume_tolerance")["absolute_base_units"]
            or reconciliation.get("volume_relative_tolerance")
            != self._mapping(contract, "volume_tolerance")["relative_fraction"]
        ):
            message = "data-parity artifact tolerance or consolidation settings differ"
            raise ValueError(message)
        if payload.get("calendar_strata") != contract["calendar_strata"]:
            message = "data-parity artifact strata differ from the protocol"
            raise ValueError(message)
        artifact_dependency = self._mapping(payload, "strategy_field_dependency")
        registered_dependency = self._mapping(
            contract,
            "strategy_field_dependency",
        )
        expected_artifact_dependency = {
            "price_and_timestamp_fields": registered_dependency["price_and_timestamp_fields"],
            "volume": ("consumed" if registered_dependency["uses_volume"] else "not_consumed"),
        }
        if artifact_dependency != expected_artifact_dependency:
            message = "data-parity artifact differs from the strategy field dependency"
            raise ValueError(message)
        artifact_products = self._mapping(payload, "products")
        declared_products = {
            str(product["requested_product"]): product
            for product in self._list_of_mappings(data, "primary_products")
        }
        if set(artifact_products) != set(declared_products):
            message = "data-parity artifact products differ from the primary panel"
            raise ValueError(message)
        for product, declared in declared_products.items():
            artifact_product = self._mapping(artifact_products, product)
            expected_metadata = {
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
            if (
                artifact_product.get("canonical_book") != declared["expected_canonical_book"]
                or artifact_product.get("price_tick") != declared["price_increment"]
                or artifact_product.get("metadata_snapshot") != expected_metadata
            ):
                message = f"data-parity artifact product lineage differs for {product}"
                raise ValueError(message)

    def _validate_frozen_data_parity_integrity(
        self,
        payload: Mapping[str, Any],
    ) -> None:
        """Validate immutable parity evidence without consulting mutable source bytes."""

        observed_hash = payload.get("attestation_sha256")
        if not isinstance(observed_hash, str) or not _SHA256_RE.fullmatch(observed_hash):
            message = "data-parity attestation_sha256 is not a lowercase SHA-256 digest"
            raise ValueError(message)
        unsigned = dict(payload)
        del unsigned["attestation_sha256"]
        if manifest_sha256(unsigned) != observed_hash:
            message = "data-parity attestation content hash differs"
            raise ValueError(message)

        source_inputs = self._mapping(payload, "source_inputs")
        source_input_sha256 = payload.get("source_input_sha256")
        if (
            not isinstance(source_input_sha256, str)
            or not _SHA256_RE.fullmatch(source_input_sha256)
            or manifest_sha256(source_inputs) != source_input_sha256
        ):
            message = "data-parity source input hash differs"
            raise ValueError(message)

        reconciliation = self._mapping(payload, "reconciliation")
        source_sha256 = reconciliation.get("source_sha256")
        if not isinstance(source_sha256, str) or not _SHA256_RE.fullmatch(source_sha256):
            message = "data-parity reconciliation source_sha256 is invalid"
            raise ValueError(message)

        products = self._mapping(payload, "products")
        if set(products) != set(source_inputs):
            message = "data-parity product and source-input identities differ"
            raise ValueError(message)
        calendar_strata = self._list_of_mappings(payload, "calendar_strata")
        expected_strata = {
            self._nonempty_string(row.get("id"), field="calendar_strata.id")
            for row in calendar_strata
        }
        if len(expected_strata) != len(calendar_strata):
            message = "data-parity calendar strata contain duplicate identities"
            raise ValueError(message)
        for product in sorted(products):
            product_inputs = self._mapping(source_inputs, product)
            product_evidence = self._mapping(products, product)
            raw_strata = self._list_of_mappings(product_evidence, "strata")
            strata_by_id: dict[str, dict[str, Any]] = {}
            for row in raw_strata:
                stratum_id = self._nonempty_string(
                    row.get("stratum_id"),
                    field=f"products.{product}.strata.stratum_id",
                )
                if stratum_id in strata_by_id:
                    message = f"data-parity product {product} contains duplicate strata"
                    raise ValueError(message)
                strata_by_id[stratum_id] = row
            if set(product_inputs) != expected_strata or set(strata_by_id) != expected_strata:
                message = f"data-parity product {product} strata differ from source inputs"
                raise ValueError(message)
            for stratum_id in sorted(expected_strata):
                stratum_inputs = self._mapping(product_inputs, stratum_id)
                input_sha256 = strata_by_id[stratum_id].get("input_source_sha256")
                if (
                    not isinstance(input_sha256, str)
                    or not _SHA256_RE.fullmatch(input_sha256)
                    or manifest_sha256(stratum_inputs) != input_sha256
                ):
                    message = (
                        f"data-parity product-stratum input hash differs: {product}.{stratum_id}"
                    )
                    raise ValueError(message)

    def _validate_cost_contract(
        self,
        protocol: Mapping[str, Any],
        *,
        products: set[str],
    ) -> None:
        costs = self._mapping(protocol, "cost_scenarios")
        required_scenarios = {
            "gross",
            "optimistic",
            "expected",
            "expected_latency_1bar",
            "stressed",
        }
        if set(costs) != required_scenarios:
            message = "cost_scenarios must exactly match the registered cost and latency panel"
            raise ValueError(message)
        for scenario_name, raw_scenario in costs.items():
            scenario = self._require_mapping(raw_scenario, field=f"cost_scenarios.{scenario_name}")
            if not str(scenario.get("status", "")).startswith("frozen"):
                message = f"cost scenario {scenario_name} is not frozen"
                raise ValueError(message)
            product_costs = self._mapping(scenario, "products")
            if set(product_costs) != products:
                message = f"cost scenario {scenario_name} does not cover the primary products"
                raise ValueError(message)

    def _validate_cost_measurement_contract(
        self,
        protocol: Mapping[str, Any],
        *,
        products: set[str],
    ) -> None:
        contract = self._mapping(protocol, "cost_measurement")
        status = self._nonempty_string(contract.get("status"), field="cost_measurement.status")
        if not status.startswith("frozen"):
            message = f"cost measurement contract is not frozen: {status}"
            raise ValueError(message)
        expected_identity = {
            "schema_id": "vynmatrix.coinbase-execution-cost-measurement",
            "schema_version": 1,
            "provider": "coinbase_advanced_trade",
            "book_endpoint": "/api/v3/brokerage/product_book",
            "fee_endpoint": "/api/v3/brokerage/transaction_summary?product_type=SPOT",
            "market_fills_use_taker_fee": True,
            "historical_order_books_available": False,
            "historical_application": ("current_measured_applied_backward_with_sensitivity"),
        }
        if any(contract.get(key) != value for key, value in expected_identity.items()):
            message = "cost measurement provider, endpoint, or historical-use contract differs"
            raise ValueError(message)
        sampling = self._mapping(contract, "sampling")
        expected_sampling = {
            "samples_required_per_product": 20,
            "missing_sample_policy": "fail_complete_measurement",
            "depth_limit_levels_per_side": 100,
            "maximum_snapshot_age_seconds": 120.0,
            "maximum_inter_sample_gap_seconds": 10.0,
            "maximum_provider_clock_skew_seconds": 5.0,
        }
        if sampling != expected_sampling:
            message = "cost measurement sample completeness/freshness contract differs"
            raise ValueError(message)
        depth = self._mapping(contract, "depth_walk")
        expected_depth = {
            "expected_quote_notional": "10000",
            "stressed_quote_notional": "100000",
            "side_aggregation": "maximum_of_buy_and_sell_depth_impact",
            "expected_aggregation": {
                "quantile": 0.5,
                "method": "nearest_rank_ceiling",
            },
            "stressed_aggregation": {
                "quantile": 0.95,
                "method": "nearest_rank_ceiling",
            },
        }
        if depth != expected_depth:
            message = "cost measurement notional or nearest-rank quantile contract differs"
            raise ValueError(message)
        attestation = self._mapping(contract, "exact_attestation")
        for key in ("measurement_sha256", "raw_snapshot_sha256"):
            if not _SHA256_RE.fullmatch(str(attestation.get(key, ""))):
                message = f"cost measurement exact_attestation.{key} is not frozen"
                raise ValueError(message)
        for key in (
            "measured_at_utc",
            "first_provider_timestamp",
            "last_provider_timestamp",
        ):
            parse_utc_datetime(attestation.get(key), field=f"cost_measurement.{key}")
        fee_summary = self._mapping(contract, "exact_fee_summary")
        if set(fee_summary) != {"maker_fee_bps", "taker_fee_bps"}:
            message = "cost measurement exact fee summary is incomplete"
            raise ValueError(message)
        for key, value in fee_summary.items():
            if self._decimal(value, field=f"cost_measurement.exact_fee_summary.{key}") < 0:
                message = f"cost measurement {key} must be non-negative"
                raise ValueError(message)
        declared_products = {
            self._nonempty_string(item.get("requested_product"), field="requested_product")
            for item in self._list_of_mappings(self._mapping(protocol, "data"), "primary_products")
        }
        if declared_products != products:
            message = "cost measurement products differ from the data contract"
            raise ValueError(message)

    def _validate_execution_cost_measurement(
        self,
        payload: Mapping[str, Any],
        *,
        protocol: Mapping[str, Any],
    ) -> None:
        """Verify and reconcile one full public-book artifact to the protocol."""

        self._execution_cost_measurement_verifier(payload)
        summaries, expected_products, taker_bps = self._validate_cost_artifact_contract(
            payload,
            protocol=protocol,
        )
        self._validate_cost_artifact_scenarios(
            protocol,
            summaries=summaries,
            expected_products=expected_products,
            taker_bps=taker_bps,
        )

    def _validate_cost_artifact_contract(
        self,
        payload: Mapping[str, Any],
        *,
        protocol: Mapping[str, Any],
    ) -> tuple[dict[str, Any], set[str], Decimal]:
        contract = self._mapping(protocol, "cost_measurement")
        attestation = self._mapping(contract, "exact_attestation")
        sampling_contract = self._mapping(contract, "sampling")
        depth_contract = self._mapping(contract, "depth_walk")
        for key in ("schema_id", "schema_version", "provider", "book_endpoint", "fee_endpoint"):
            if payload.get(key) != contract.get(key):
                message = f"execution-cost artifact {key} differs from the protocol"
                raise ValueError(message)
        if (
            payload.get("measurement_sha256") != attestation["measurement_sha256"]
            or payload.get("raw_snapshot_sha256") != attestation["raw_snapshot_sha256"]
            or payload.get("historical_order_books_available")
            != contract["historical_order_books_available"]
            or payload.get("historical_application") != contract["historical_application"]
        ):
            message = "execution-cost artifact hashes or historical-use label differ"
            raise ValueError(message)
        sampling = self._mapping(payload, "sampling")
        exact_sampling = {
            "measured_at_utc": attestation["measured_at_utc"],
            "first_provider_timestamp": attestation["first_provider_timestamp"],
            "last_provider_timestamp": attestation["last_provider_timestamp"],
        }
        if any(sampling.get(key) != value for key, value in exact_sampling.items()):
            message = "execution-cost artifact timestamps differ from the protocol"
            raise ValueError(message)
        for key in (
            "samples_required_per_product",
            "missing_sample_policy",
            "depth_limit_levels_per_side",
            "maximum_snapshot_age_seconds",
            "maximum_inter_sample_gap_seconds",
            "maximum_provider_clock_skew_seconds",
        ):
            if sampling.get(key) != sampling_contract[key]:
                message = f"execution-cost artifact sampling.{key} differs from the protocol"
                raise ValueError(message)
        if (
            sampling.get("samples_observed_per_product")
            != sampling_contract["samples_required_per_product"]
        ):
            message = "execution-cost artifact did not observe the complete registered sample"
            raise ValueError(message)
        depth = self._mapping(payload, "depth_walk")
        for key in (
            "expected_quote_notional",
            "stressed_quote_notional",
            "side_aggregation",
            "expected_aggregation",
            "stressed_aggregation",
        ):
            if depth.get(key) != depth_contract[key]:
                message = f"execution-cost artifact depth_walk.{key} differs from the protocol"
                raise ValueError(message)

        product_contracts = {
            str(item["requested_product"]): str(item["expected_canonical_book"])
            for item in self._list_of_mappings(self._mapping(protocol, "data"), "primary_products")
        }
        expected_products = set(product_contracts)
        if set(sampling.get("products", ())) != expected_products:
            message = "execution-cost artifact products differ from the data contract"
            raise ValueError(message)
        raw_snapshots = self._mapping(payload, "raw_snapshots")
        summaries = self._mapping(payload, "product_summaries")
        if set(raw_snapshots) != expected_products or set(summaries) != expected_products:
            message = "execution-cost raw books and summaries must cover every product exactly"
            raise ValueError(message)
        sample_count = int(sampling_contract["samples_required_per_product"])
        for product in sorted(expected_products):
            rows = raw_snapshots[product]
            if not isinstance(rows, list) or len(rows) != sample_count:
                message = f"execution-cost raw sample count differs for {product}"
                raise ValueError(message)
            summary = self._require_mapping(
                summaries[product], field=f"product_summaries.{product}"
            )
            if (
                summary.get("provider_product") != product_contracts[product]
                or summary.get("samples_observed") != sample_count
            ):
                message = f"execution-cost provider identity/sample count differs for {product}"
                raise ValueError(message)

        taker_bps = self._validated_cost_fee(payload, contract=contract)
        return summaries, expected_products, taker_bps

    def _validated_cost_fee(
        self,
        payload: Mapping[str, Any],
        *,
        contract: Mapping[str, Any],
    ) -> Decimal:
        fee = self._mapping(payload, "fee_snapshot")
        if fee.get("private_account_fields_retained") is not False:
            message = "execution-cost artifact retained private account fields"
            raise ValueError(message)
        fee_summary = self._mapping(contract, "exact_fee_summary")
        maker_bps = self._decimal(fee.get("maker_fee_bps"), field="maker_fee_bps")
        taker_bps = self._decimal(fee.get("taker_fee_bps"), field="taker_fee_bps")
        if maker_bps != self._decimal(
            fee_summary["maker_fee_bps"], field="maker_fee_bps"
        ) or taker_bps != self._decimal(fee_summary["taker_fee_bps"], field="taker_fee_bps"):
            message = "execution-cost fee summary differs from the protocol"
            raise ValueError(message)
        return taker_bps

    def _validate_cost_artifact_scenarios(
        self,
        protocol: Mapping[str, Any],
        *,
        summaries: Mapping[str, Any],
        expected_products: set[str],
        taker_bps: Decimal,
    ) -> None:
        scenarios = self._mapping(protocol, "cost_scenarios")
        scenario_contract = {
            "gross": (Decimal(0), 0, None),
            "optimistic": (taker_bps, 0, "optimistic"),
            "expected": (taker_bps, 0, "expected"),
            "expected_latency_1bar": (taker_bps, 1, "expected"),
            "stressed": (taker_bps * Decimal("1.5"), 1, "stressed"),
        }
        for scenario_name, (commission, delay, artifact_summary_name) in scenario_contract.items():
            scenario = self._mapping(scenarios, scenario_name)
            if (
                self._decimal(
                    scenario.get("commission_bps"),
                    field=f"cost_scenarios.{scenario_name}.commission_bps",
                )
                != commission
                or self._decimal(
                    scenario.get("slippage_bps"),
                    field=f"cost_scenarios.{scenario_name}.slippage_bps",
                )
                != 0
                or scenario.get("fill_delay_bars") != delay
            ):
                message = f"cost scenario {scenario_name} fee/slippage/delay differs"
                raise ValueError(message)
            if scenario_name == "stressed" and self._decimal(
                scenario.get("commission_sensitivity_multiple"),
                field="cost_scenarios.stressed.commission_sensitivity_multiple",
            ) != Decimal("1.5"):
                message = "stressed commission sensitivity must be exactly 1.5"
                raise ValueError(message)
            product_costs = self._mapping(scenario, "products")
            for product in sorted(expected_products):
                observed_cost = self._require_mapping(
                    product_costs[product],
                    field=f"cost_scenarios.{scenario_name}.products.{product}",
                )
                if artifact_summary_name is None:
                    expected_half_spread = Decimal(0)
                    expected_impact = Decimal(0)
                else:
                    product_summary = self._require_mapping(
                        summaries[product], field=f"product_summaries.{product}"
                    )
                    artifact_cost = self._mapping(product_summary, artifact_summary_name)
                    expected_half_spread = self._decimal(
                        artifact_cost.get("half_spread_bps"), field="half_spread_bps"
                    )
                    expected_impact = self._decimal(
                        artifact_cost.get("impact_bps"), field="impact_bps"
                    )
                if (
                    self._decimal(observed_cost.get("half_spread_bps"), field="half_spread_bps")
                    != expected_half_spread
                    or self._decimal(observed_cost.get("impact_bps"), field="impact_bps")
                    != expected_impact
                ):
                    message = f"cost scenario {scenario_name} product summary differs for {product}"
                    raise ValueError(message)

    def _validate_selection_contract(self, protocol: Mapping[str, Any]) -> list[str]:
        arms = [
            self._nonempty_string(arm.get("id"), field="signal_rule_arms.id")
            for arm in self._list_of_mappings(protocol, "signal_rule_arms")
        ]
        if len(arms) != len(set(arms)):
            message = "signal_rule_arms IDs must be unique"
            raise ValueError(message)
        primary = self._mapping(self._mapping(protocol, "selection"), "primary_trial_family")
        raw_exclusions = primary.get("diagnostic_arm_ids_excluded_from_selection")
        if isinstance(raw_exclusions, (str, bytes)) or not isinstance(raw_exclusions, Sequence):
            message = "diagnostic arm exclusions must be an array"
            raise TypeError(message)
        exclusions = [
            self._nonempty_string(value, field="diagnostic_arm_ids_excluded_from_selection")
            for value in raw_exclusions
        ]
        if len(exclusions) != len(set(exclusions)) or not set(exclusions).issubset(arms):
            message = "diagnostic arm exclusions must be unique registered arms"
            raise ValueError(message)
        selectable_arms = [arm for arm in arms if arm not in set(exclusions)]
        expected_primary = {
            "arm_id": "TRAINING_SELECTED",
            "family": "training_selected_pooled_oos",
            "candidate_arm_ids": selectable_arms,
            "diagnostic_arm_ids_excluded_from_selection": exclusions,
            "cost_scenario": "expected",
            "selection_scope": "equal_weighted_primary_assets",
            "selector": "anchored_exact_calendar_v1",
            "training_objective": ("annualized_sharpe_of_equal_weighted_daily_net_returns"),
            "tie_break": "baseline_first_then_registered_arm_order",
            "oos_parameter_source": "training_data_only",
            "direct_candidate_oos_role": "retrospective_diagnostic",
            "selection_terminal_treatment": {
                "policy": (
                    "last_eligible_training_close_mark_plus_stressed_hypothetical_liquidation_cost"
                ),
                "counts_as_closed_trade": False,
                "must_be_reported_separately": True,
                "unregistered_or_unreconciled_terminal_exposure": "trial_failure",
            },
            "oos_fold_terminal_treatment": {
                "policy": (
                    "last_oos_close_mark_plus_stressed_hypothetical_liquidation_cost_"
                    "before_fold_pooling"
                ),
                "raw_report_remains_open": True,
                "counts_as_closed_trade": False,
                "must_be_reported_separately": True,
                "unadjusted_terminal_exposure": "primary_trial_failure",
            },
        }
        if primary != expected_primary:
            message = "primary_trial_family does not match the registered selection contract"
            raise ValueError(message)
        return arms

    def _validate_fold_contract(self, protocol: Mapping[str, Any]) -> None:
        fold_ids: set[str] = set()
        for fold in self._list_of_mappings(protocol, "folds"):
            year = int(fold["oos_year"])
            fold_id = f"oos-{year}"
            if fold_id in fold_ids:
                message = f"duplicate fold ID: {fold_id}"
                raise ValueError(message)
            fold_ids.add(fold_id)
            train_end = parse_utc_datetime(
                fold.get("train_end_exclusive"), field=f"folds.{fold_id}.train_end_exclusive"
            )
            test_start = parse_utc_datetime(
                fold.get("test_start"), field=f"folds.{fold_id}.test_start"
            )
            test_end = parse_utc_datetime(
                fold.get("test_end_exclusive"),
                field=f"folds.{fold_id}.test_end_exclusive",
            )
            if train_end > test_start or test_start >= test_end:
                message = f"invalid exact half-open boundaries for {fold_id}"
                raise ValueError(message)
            expected_start = datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=1)
            expected_end = datetime(year + 1, 1, 1, tzinfo=UTC) + timedelta(days=1)
            if (
                train_end != expected_start
                or test_start != expected_start
                or test_end != expected_end
            ):
                message = f"{fold_id} does not own its exact source-candle calendar year"
                raise ValueError(message)

    def _validate_frozen_manifest(
        self,
        manifest: Mapping[str, Any],
        *,
        protocol: Mapping[str, Any],
        strategy_path: Path,
        runtime_config: Mapping[str, Any],
        expected_hash: str,
    ) -> None:
        self._validate_frozen_manifest_content(
            manifest,
            protocol=protocol,
            expected_hash=expected_hash,
        )
        strategy = self._mapping(manifest, "strategy")
        if runtime_config.get("strategy_id") != strategy["strategy_id"]:
            message = "runtime config strategy_id differs from the frozen manifest"
            raise ValueError(message)
        if runtime_config.get("strategy_version") != strategy["strategy_version"]:
            message = "runtime config strategy_version differs from the frozen manifest"
            raise ValueError(message)
        for path_key, hash_key, expected_path in (
            ("core_path", "core_sha256", strategy_path / "core.py"),
            ("runtime_config_path", "runtime_config_sha256", strategy_path / "config.json"),
        ):
            if strategy.get(path_key) != self._relative(expected_path):
                message = f"frozen {path_key} does not identify the production strategy"
                raise ValueError(message)
            if strategy.get(hash_key) != self._file_sha256(expected_path):
                message = f"production {path_key} changed after manifest freeze"
                raise ValueError(message)
        current_binding_snapshot = self._operational_binding_snapshot(
            self._mapping(protocol, "strategy")
        )
        if manifest.get("operational_binding_snapshot") != current_binding_snapshot:
            message = "operational binding or entry-guard state changed after manifest freeze"
            raise ValueError(message)

    def _validate_frozen_manifest_content(
        self,
        manifest: Mapping[str, Any],
        *,
        protocol: Mapping[str, Any],
        expected_hash: str,
        current_data_parity_source_required: bool = True,
    ) -> None:
        """Verify only immutable manifest/protocol content, never mutable runtime files."""

        self._validate_protocol(protocol)
        if manifest_sha256(manifest) != expected_hash:
            message = "frozen manifest content hash does not match its address"
            raise ValueError(message)
        if manifest.get("status") != "frozen":
            message = "campaign manifest status must be frozen"
            raise ValueError(message)
        self._validate_manifest_cost_provenance(manifest, protocol=protocol)
        self._validate_manifest_data_parity_provenance(
            manifest,
            protocol=protocol,
            current_source_required=current_data_parity_source_required,
        )
        self._validate_manifest_correctness_provenance(manifest, protocol=protocol)
        if manifest.get("protocol_sha256") != manifest_sha256(protocol):
            message = "checked-in validation protocol differs from the frozen manifest"
            raise ValueError(message)
        if manifest.get("validation_design") != protocol["validation_design"]:
            message = "frozen manifest validation design differs from the protocol"
            raise ValueError(message)
        strategy = self._mapping(manifest, "strategy")
        protocol_strategy = self._mapping(protocol, "strategy")
        for key in ("strategy_id", "strategy_version", "core_class"):
            if strategy.get(key) != protocol_strategy.get(key):
                message = f"frozen strategy {key} differs from the protocol"
                raise ValueError(message)
        for path_key in ("core_path", "runtime_config_path"):
            protocol_path = protocol_strategy.get(path_key)
            if protocol_path is not None and strategy.get(path_key) != protocol_path:
                message = f"frozen {path_key} differs from the protocol"
                raise ValueError(message)
        for hash_key in ("core_sha256", "runtime_config_sha256"):
            digest = strategy.get(hash_key)
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                message = f"frozen strategy {hash_key} is not a SHA-256 digest"
                raise ValueError(message)
        if manifest.get("evidence_boundary") != protocol["evidence_boundary"]:
            message = "frozen evidence boundary differs from the protocol"
            raise ValueError(message)
        self._validate_manifest_statistical_contract(manifest, protocol)
        for field in (
            "historical_disposition_gates",
            "historical_disposition_policy",
            "allowed_dispositions",
            "promotion",
        ):
            if manifest.get(field) != protocol[field]:
                message = f"frozen {field} differs from the verdict protocol"
                raise ValueError(message)

    def _load_content_addressed_protocol(
        self,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resolve an immutable protocol whose canonical digest the manifest pins."""

        expected = manifest.get("protocol_sha256")
        if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
            message = "frozen protocol_sha256 is not a lowercase SHA-256 digest"
            raise ValueError(message)
        protocol_root = self._artifact_root / "research" / "protocols" / "sha256"
        matches: list[tuple[Path, dict[str, Any]]] = []
        if protocol_root.is_dir():
            for path in sorted(protocol_root.glob("*/*.json")):
                try:
                    payload = path.read_bytes()
                    decoded = json.loads(payload)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(decoded, dict) or manifest_sha256(decoded) != expected:
                    continue
                raw_digest = hashlib.sha256(payload).hexdigest()
                if path.stem != raw_digest or path.parent.name != raw_digest[:2]:
                    message = f"protocol artifact content address is invalid: {path}"
                    raise ValueError(message)
                matches.append((path, decoded))
        if not matches:
            message = f"content-addressed protocol artifact not found for {expected}"
            raise FileNotFoundError(message)
        if len(matches) > 1:
            paths = ", ".join(str(path) for path, _payload in matches)
            message = f"multiple protocol artifacts resolve frozen digest {expected}: {paths}"
            raise ValueError(message)
        return matches[0][1]

    def _store_content_addressed_protocol(self, protocol: Mapping[str, Any]) -> None:
        """Publish the canonical protocol needed to audit a retired strategy."""

        payload = canonical_manifest_bytes(protocol)
        digest = hashlib.sha256(payload).hexdigest()
        destination = (
            self._artifact_root
            / "research"
            / "protocols"
            / "sha256"
            / digest[:2]
            / f"{digest}.json"
        )
        atomic_create_bytes(destination, payload, mode=0o444)

    def _validate_manifest_cost_provenance(
        self,
        manifest: Mapping[str, Any],
        *,
        protocol: Mapping[str, Any],
    ) -> None:
        if manifest.get("manifest_schema_version") != self.MANIFEST_SCHEMA_VERSION:
            message = "campaign manifest schema version differs from the executable runner"
            raise ValueError(message)
        if manifest.get("cost_measurement") != protocol["cost_measurement"]:
            message = "frozen cost measurement contract differs from the protocol"
            raise ValueError(message)
        self._validate_execution_cost_measurement(
            self._mapping(manifest, "execution_cost_measurement"),
            protocol=protocol,
        )

    def _validate_manifest_data_parity_provenance(
        self,
        manifest: Mapping[str, Any],
        *,
        protocol: Mapping[str, Any],
        current_source_required: bool = True,
    ) -> None:
        contract = self._mapping(self._mapping(protocol, "data"), "minute_parity")
        if manifest.get("data_parity_contract") != contract:
            message = "frozen data-parity contract differs from the protocol"
            raise ValueError(message)
        self._validate_data_parity_attestation(
            self._mapping(manifest, "data_parity_attestation"),
            protocol=protocol,
            current_source_required=current_source_required,
        )

    def _validate_manifest_correctness_provenance(
        self,
        manifest: Mapping[str, Any],
        *,
        protocol: Mapping[str, Any],
    ) -> None:
        contract = self._mapping(protocol, "correctness_attestation")
        if manifest.get("correctness_attestation_contract") != contract:
            message = "frozen correctness-attestation contract differs from the protocol"
            raise ValueError(message)
        decision = self._validate_correctness_attestation(
            self._mapping(manifest, "correctness_attestation"),
            protocol=protocol,
        )
        if manifest.get("correctness_attestation_decision") != decision:
            message = "frozen correctness-attestation decision differs from re-verification"
            raise ValueError(message)

    def _validate_correctness_attestation(
        self,
        payload: Mapping[str, Any],
        *,
        protocol: Mapping[str, Any],
    ) -> dict[str, object]:
        """Verify an embedded source review against the exact protocol contract."""

        decision = verify_registered_strategy_correctness_attestation(
            payload,
            self._mapping(protocol, "correctness_attestation"),
        )
        strategy = self._mapping(protocol, "strategy")
        if decision.strategy_id != strategy.get(
            "strategy_id"
        ) or decision.strategy_version != strategy.get("strategy_version"):
            message = "correctness-attestation subject differs from the registered strategy"
            raise ValueError(message)
        return decision.to_dict()

    def _validate_manifest_statistical_contract(
        self,
        manifest: Mapping[str, Any],
        protocol: Mapping[str, Any],
    ) -> None:
        for field in ("multiple_testing", "inference"):
            if manifest.get(field) != protocol[field]:
                message = f"frozen {field} differs from the statistical protocol"
                raise ValueError(message)
        validate_embedded_selection_ledger(
            self._mapping(manifest, "upstream_selection_ledger"),
            self._mapping(
                self._mapping(protocol, "multiple_testing"),
                "upstream_selection_ledger_contract",
            ),
        )

    def _validated_frozen_trial_specs_for_audit(
        self,
        manifest: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Validate a retired manifest's registry without rebuilding executable code.

        The manifest content hash authenticates the exact historical rows.  This
        validator therefore enforces the frozen registry schema, ordering,
        dataset lineage, and uniqueness while deliberately accepting a
        historical runner kind that no longer exists after retirement.  Exact
        trial/result identity is checked separately against the append-only DB.
        """

        specs = [dict(item) for item in self._list_of_mappings(manifest, "trial_registry")]
        if not specs:
            message = "frozen trial_registry must not be empty"
            raise ValueError(message)
        datasets = {
            str(dataset["dataset_id"]): dataset
            for dataset in self._list_of_mappings(self._mapping(manifest, "data"), "datasets")
        }
        if not datasets:
            message = "frozen audit manifest has no datasets"
            raise ValueError(message)

        row_identities: set[bytes] = set()
        final_sequences: list[int] = []
        for sequence, spec in enumerate(specs):
            self._validate_frozen_trial_spec_schema(spec, sequence=sequence)
            self._validate_frozen_trial_window(spec, sequence=sequence)
            self._validate_frozen_trial_dataset(
                spec,
                datasets=datasets,
                sequence=sequence,
            )

            identity = canonical_manifest_bytes(
                {key: value for key, value in spec.items() if key != "sequence"}
            )
            if identity in row_identities:
                message = "frozen trial_registry contains a duplicate non-sequence identity"
                raise ValueError(message)
            row_identities.add(identity)
            if spec["runner_kind"] == _HISTORICAL_DISPOSITION_RUNNER:
                final_sequences.append(sequence)

        if final_sequences != [len(specs) - 1]:
            message = "frozen trial_registry must end with one historical disposition trial"
            raise ValueError(message)
        return specs

    def _validate_frozen_trial_spec_schema(
        self,
        spec: Mapping[str, Any],
        *,
        sequence: int,
    ) -> None:
        required_keys = {
            "arm_id",
            "cost_scenario",
            "dataset_id",
            "evidence_role",
            "fold_id",
            "historical_selection_contaminated",
            "parameters",
            "runner_kind",
            "sequence",
            "trial_family",
        }
        optional_keys = {
            "abandonment_reason",
            "component_datasets",
            "evaluation_end_exclusive",
            "evaluation_start",
            "registration_status",
            "selection_group_id",
            "training_end_exclusive",
        }
        missing = sorted(required_keys - set(spec))
        unsupported = sorted(set(spec) - required_keys - optional_keys)
        if missing or unsupported:
            details = []
            if missing:
                details.append(f"missing {', '.join(missing)}")
            if unsupported:
                details.append(f"unsupported {', '.join(unsupported)}")
            message = (
                f"frozen trial_registry sequence {sequence} has invalid fields: "
                f"{'; '.join(details)}"
            )
            raise ValueError(message)
        observed_sequence = spec.get("sequence")
        if isinstance(observed_sequence, bool) or observed_sequence != sequence:
            message = "trial_registry sequences must be contiguous integers from zero"
            raise ValueError(message)
        for field in (
            "arm_id",
            "cost_scenario",
            "dataset_id",
            "evidence_role",
            "fold_id",
            "runner_kind",
            "trial_family",
        ):
            self._nonempty_string(spec.get(field), field=f"trial_registry[{sequence}].{field}")
        if spec.get("historical_selection_contaminated") is not True:
            message = (
                f"frozen trial_registry sequence {sequence} is not labelled "
                "historical-selection-contaminated"
            )
            raise ValueError(message)
        self._require_mapping(
            spec.get("parameters"),
            field=f"trial_registry[{sequence}].parameters",
        )
        canonical_manifest_bytes({"parameters": spec["parameters"]})
        self._validate_frozen_trial_registration(spec, sequence=sequence)

    def _validate_frozen_trial_registration(
        self,
        spec: Mapping[str, Any],
        *,
        sequence: int,
    ) -> None:
        registration_status = spec.get("registration_status")
        abandonment_reason = spec.get("abandonment_reason")
        if registration_status is None:
            if abandonment_reason is not None:
                message = (
                    f"frozen trial_registry sequence {sequence} has an abandonment "
                    "reason without abandoned status"
                )
                raise ValueError(message)
            return
        if registration_status != BacktestTrialStatus.ABANDONED.value:
            message = (
                f"frozen trial_registry sequence {sequence} has unsupported "
                f"registration_status {registration_status!r}"
            )
            raise ValueError(message)
        self._nonempty_string(
            abandonment_reason,
            field=f"trial_registry[{sequence}].abandonment_reason",
        )

    def _validate_frozen_trial_window(
        self,
        spec: Mapping[str, Any],
        *,
        sequence: int,
    ) -> None:
        evaluation_start = spec.get("evaluation_start")
        evaluation_end = spec.get("evaluation_end_exclusive")
        if (evaluation_start is None) != (evaluation_end is None):
            message = f"frozen trial_registry sequence {sequence} has an incomplete interval"
            raise ValueError(message)
        if evaluation_start is None:
            if spec.get("registration_status") != BacktestTrialStatus.ABANDONED.value:
                message = f"frozen executable trial sequence {sequence} has no interval"
                raise ValueError(message)
        else:
            start = parse_utc_datetime(
                evaluation_start,
                field=f"trial_registry[{sequence}].evaluation_start",
            )
            end = parse_utc_datetime(
                evaluation_end,
                field=f"trial_registry[{sequence}].evaluation_end_exclusive",
            )
            if start >= end:
                message = f"frozen trial_registry sequence {sequence} has an empty interval"
                raise ValueError(message)
        if "training_end_exclusive" in spec:
            parse_utc_datetime(
                spec["training_end_exclusive"],
                field=f"trial_registry[{sequence}].training_end_exclusive",
            )
        if "selection_group_id" in spec:
            self._nonempty_string(
                spec["selection_group_id"],
                field=f"trial_registry[{sequence}].selection_group_id",
            )

    def _validate_frozen_trial_dataset(
        self,
        spec: Mapping[str, Any],
        *,
        datasets: Mapping[str, Mapping[str, Any]],
        sequence: int,
    ) -> None:
        dataset_id = str(spec["dataset_id"])
        raw_components = spec.get("component_datasets")
        if raw_components is None:
            if dataset_id not in datasets:
                message = (
                    f"frozen trial_registry sequence {sequence} references unknown "
                    f"dataset {dataset_id}"
                )
                raise ValueError(message)
            return
        components = self._validate_frozen_component_datasets(
            raw_components,
            datasets=datasets,
            sequence=sequence,
        )
        current_id = self._derived_portfolio_dataset_id(components)
        legacy_id = current_id.replace(
            "derived:equal-weight-portfolio:",
            "synthetic:equal-weight-portfolio:",
            1,
        )
        if dataset_id not in {current_id, legacy_id}:
            message = (
                f"frozen trial_registry sequence {sequence} derived dataset identity "
                "differs from its components"
            )
            raise ValueError(message)

    def _validate_frozen_component_datasets(
        self,
        raw_components: Any,
        *,
        datasets: Mapping[str, Mapping[str, Any]],
        sequence: int,
    ) -> list[dict[str, str]]:
        if isinstance(raw_components, (str, bytes)) or not isinstance(raw_components, Sequence):
            message = f"trial_registry[{sequence}].component_datasets must be an array"
            raise TypeError(message)
        components: list[dict[str, str]] = []
        observed_ids: set[str] = set()
        for index, raw_component in enumerate(raw_components):
            component = self._require_mapping(
                raw_component,
                field=f"trial_registry[{sequence}].component_datasets[{index}]",
            )
            required = {
                "canonical_product",
                "dataset_id",
                "ohlcv_sha256",
                "requested_product",
            }
            if set(component) != required:
                message = (
                    f"trial_registry[{sequence}].component_datasets[{index}] fields "
                    "are not canonical"
                )
                raise ValueError(message)
            dataset_id = self._nonempty_string(
                component.get("dataset_id"),
                field=f"trial_registry[{sequence}].component_datasets[{index}].dataset_id",
            )
            dataset = datasets.get(dataset_id)
            if dataset is None:
                message = (
                    f"trial_registry[{sequence}].component_datasets[{index}] references "
                    f"unknown dataset {dataset_id}"
                )
                raise ValueError(message)
            audit = self._mapping(dataset, "audit")
            expected = {
                "canonical_product": str(dataset["canonical_product"]),
                "dataset_id": dataset_id,
                "ohlcv_sha256": str(audit["ohlcv_sha256"]),
                "requested_product": str(dataset["requested_product"]),
            }
            if component != expected:
                message = (
                    f"trial_registry[{sequence}].component_datasets[{index}] differs "
                    "from frozen dataset lineage"
                )
                raise ValueError(message)
            if dataset_id in observed_ids:
                message = f"trial_registry[{sequence}] contains a duplicate component dataset"
                raise ValueError(message)
            observed_ids.add(dataset_id)
            components.append(expected)
        if not components:
            message = f"trial_registry[{sequence}].component_datasets must not be empty"
            raise ValueError(message)
        return components

    def _validated_trial_specs(
        self,
        manifest: Mapping[str, Any],
        protocol: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        specs = [dict(item) for item in self._list_of_mappings(manifest, "trial_registry")]
        if not specs:
            message = "frozen trial_registry must not be empty"
            raise ValueError(message)
        expected = self._build_trial_registry(
            protocol,
            self._list_of_mappings(self._mapping(manifest, "data"), "datasets"),
        )
        if len(specs) != len(expected):
            message = (
                "trial_registry family coverage differs from the frozen protocol: "
                f"observed {len(specs)}, expected {len(expected)}"
            )
            raise ValueError(message)
        for sequence, (observed, registered) in enumerate(zip(specs, expected, strict=True)):
            if observed.get("sequence") != sequence:
                message = "trial_registry sequences must be contiguous from zero"
                raise ValueError(message)
            if canonical_manifest_bytes(observed) != canonical_manifest_bytes(registered):
                differing = sorted(
                    key
                    for key in set(observed) | set(registered)
                    if observed.get(key) != registered.get(key)
                )
                message = (
                    f"trial_registry sequence {sequence} differs from its exact frozen spec: "
                    f"{', '.join(differing)}"
                )
                raise ValueError(message)
        return specs
