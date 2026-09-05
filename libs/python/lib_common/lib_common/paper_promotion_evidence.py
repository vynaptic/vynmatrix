"""Evidence profiles shared by single-instrument and portfolio paper promotion."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any

PAPER_PROMOTION_EVIDENCE_SCHEMA_VERSION = "2"
PAPER_PROMOTION_EVIDENCE_NAMES = frozenset(
    {
        "account_binding",
        "current_authorization",
        "durable_model_restart",
        "paper_order_restart",
        "real_market_data",
        "reconciliation",
        "scoring_inputs",
        "service_transport_restart",
        "soak_acceptance",
    }
)

_DOCUMENT_KEYS = frozenset(
    {"schema_version", "evidence_type", "status", "run_id", "observed_at", "scope", "outcomes"}
)
_LITERAL_OUTCOMES: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "account_binding": {
            "user_active": True,
            "account_connected": True,
            "binding_active": True,
            "autopilot_enabled": True,
            "entries_enabled": True,
            "exits_enabled": True,
            "scope_matches_persistence": True,
            "dedicated_account_confirmed": True,
            "matching_active_route_count": 1,
            "conflicting_active_route_count": 0,
        },
        "current_authorization": {
            "user_current": True,
            "account_current": True,
            "binding_current": True,
            "route_current": True,
            "paper_environment_confirmed": True,
            "pre_broker_io_revalidation_passed": True,
            "revoked_entry_rejected": True,
            "close_only_entry_rejected": True,
            "close_only_exit_allowed": True,
            "live_broker_call_count": 0,
            "unknown_submission_count": 0,
        },
        "durable_model_restart": {
            "mid_position_restart_passed": True,
            "state_contract_match": True,
            "incompatible_state_rejected": True,
            "watermark_monotonic": True,
            "post_restart_decision_match": True,
            "divergent_user_outcomes_isolated": True,
            "duplicate_signal_count": 0,
            "orphan_signal_count": 0,
        },
        "paper_order_restart": {},
        "real_market_data": {
            "real_market_data": True,
            "simulated_market_data": False,
            "market_evidence": True,
            "source_timestamps_complete": True,
            "persisted_price_lineage_complete": True,
        },
        "reconciliation": {
            "initial_reconciliation_complete": True,
            "reconciliation_healthy": True,
            "orders_match": True,
            "fills_match": True,
            "positions_match": True,
            "cash_match": True,
            "pnl_match": True,
            "drift_count": 0,
            "unknown_submission_count": 0,
            "orphan_order_count": 0,
            "orphan_fill_count": 0,
        },
        "scoring_inputs": {
            "entry_scoring_input_source": "explicit",
            "expected_return_present": True,
            "predicted_risk_present": True,
            "out_of_sample_calibration": True,
            "leakage_check_passed": True,
            "calibration_current": True,
            "uncalibrated_entry_rejected": True,
            "heuristic_entry_signal_count": 0,
        },
        "service_transport_restart": {
            "production_compose_topology": True,
            "current_time_signal_path_passed": True,
            "historical_stale_rejection_passed": True,
            "retry_injected": True,
            "restart_injected": True,
            "stable_identity_preserved": True,
            "exact_feedback_lineage": True,
            "duplicate_signal_count": 0,
            "duplicate_order_count": 0,
            "duplicate_fill_count": 0,
            "dead_letter_count": 0,
            "orphan_record_count": 0,
        },
        "soak_acceptance": {
            "report_passed": True,
            "feedback_liveness": True,
            "market_data_freshness": True,
            "signal_activity": True,
            "outbox_backlog": True,
            "execution_fills": True,
            "duplicate_submissions": True,
            "positions_consistency": True,
            "nav_recorded": True,
            "alert_sink": True,
        },
    }
)
_MINIMUM_INTEGER_OUTCOMES: Mapping[str, Mapping[str, int]] = MappingProxyType(
    {
        "account_binding": {},
        "current_authorization": {"authorization_check_count": 1},
        "durable_model_restart": {"restart_count": 1},
        "paper_order_restart": {},
        "real_market_data": {
            "price_row_count": 1,
            "complete_price_row_count": 1,
            "provenance_price_row_count": 1,
        },
        "reconciliation": {"reconciliation_run_count": 1},
        "scoring_inputs": {
            "explicit_entry_signal_count": 1,
            "uncalibrated_guard_rejection_count": 1,
        },
        "service_transport_restart": {
            "canonical_signal_count": 1,
            "scoring_decision_count": 1,
            "execution_command_count": 1,
            "entry_fill_count": 1,
            "close_fill_count": 1,
            "feedback_evaluation_count": 1,
        },
        "soak_acceptance": {"duration_days": 14},
    }
)


def parse_promotion_timestamp(value: Any, *, field: str) -> tuple[datetime | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, f"{field} must be a non-empty RFC3339 timestamp"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, f"{field} must be a valid RFC3339 timestamp"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, f"{field} must include a UTC offset"
    return parsed, None


def _same_typed_value(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if isinstance(expected, int):
        return isinstance(actual, int) and not isinstance(actual, bool) and actual == expected
    return isinstance(actual, type(expected)) and actual == expected


def _outcome_contract(
    name: str,
    *,
    expected_scope: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, int], frozenset[str], frozenset[str]]:
    literals = _LITERAL_OUTCOMES[name]
    minimums = _MINIMUM_INTEGER_OUTCOMES[name]
    strings = frozenset({"calibration_version"}) if name == "scoring_inputs" else frozenset()
    timestamps = (
        frozenset({"coverage_start", "coverage_end"})
        if name == "real_market_data"
        else (
            frozenset({"calibration_valid_from", "calibration_valid_until"})
            if name == "scoring_inputs"
            else frozenset()
        )
    )
    if name == "current_authorization":
        credential_state = (
            "not_required" if expected_scope.get("broker_code") == "paper" else "current"
        )
        literals = {**literals, "credential_state": credential_state}
    elif name == "paper_order_restart":
        common = {
            "partial_fill_case_passed": True,
            "exact_source_provenance": True,
            "exact_fee_provenance": True,
            "duplicate_fill_count": 0,
            "orphan_fill_count": 0,
        }
        if expected_scope.get("order_evidence_profile") == "bracket_oco":
            literals = {
                **common,
                "stop_case_passed": True,
                "target_case_passed": True,
                "same_bar_adverse_case_passed": True,
                "gap_case_passed": True,
                "oco_atomicity_passed": True,
                "pre_trigger_restart_passed": True,
                "post_fill_restart_passed": True,
            }
            minimums = {"restart_count": 2, "canonical_fill_count": 2}
        else:
            literals = {
                **common,
                "target_batch_passed": True,
                "reduce_case_passed": True,
                "exit_case_passed": True,
                "pre_submission_restart_passed": True,
                "post_partial_fill_restart_passed": True,
            }
            minimums = {"restart_count": 2, "canonical_fill_count": 1}
    elif name == "scoring_inputs" and expected_scope.get("scoring_semantics") == "rank_model":
        literals = {
            "entry_scoring_input_source": "model_rank",
            "require_explicit_scoring_inputs": False,
            "rank_snapshot_present": True,
            "configuration_identity_present": True,
            "calibration_required": False,
            "synthetic_expected_return_count": 0,
            "synthetic_predicted_risk_count": 0,
        }
        minimums = {"ranked_entry_signal_count": 1}
        strings = frozenset()
        timestamps = frozenset()
    elif name == "scoring_inputs":
        literals = {**literals, "require_explicit_scoring_inputs": True}
    return literals, minimums, strings, timestamps


def validate_evidence_document(  # noqa: PLR0912 - exact evidence mismatch ledger
    name: str,
    payload: Mapping[str, Any],
    *,
    expected_scope: Mapping[str, Any],
) -> list[str]:
    prefix = f"evidence.{name}"
    errors: list[str] = []
    if set(payload) != _DOCUMENT_KEYS:
        errors.append(
            f"{prefix} fields do not match evidence schema "
            f"{PAPER_PROMOTION_EVIDENCE_SCHEMA_VERSION}"
        )
    expected_header = {
        "schema_version": PAPER_PROMOTION_EVIDENCE_SCHEMA_VERSION,
        "evidence_type": name,
        "status": "passed",
    }
    header_mismatches = sorted(
        field
        for field, expected in expected_header.items()
        if not _same_typed_value(payload.get(field), expected)
    )
    if header_mismatches:
        errors.append(f"{prefix} header mismatch: {header_mismatches}")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        errors.append(f"{prefix}.run_id must be non-empty")
    _, observed_error = parse_promotion_timestamp(
        payload.get("observed_at"), field=f"{prefix}.observed_at"
    )
    if observed_error is not None:
        errors.append(observed_error)

    scope = payload.get("scope")
    if not isinstance(scope, Mapping):
        errors.append(f"{prefix}.scope must be an object")
    else:
        if set(scope) != set(expected_scope):
            errors.append(f"{prefix}.scope fields do not match the exact promotion scope")
        mismatches = sorted(
            field
            for field, expected in expected_scope.items()
            if not _same_typed_value(scope.get(field), expected)
        )
        if mismatches:
            errors.append(f"{prefix}.scope mismatch: {mismatches}")

    outcomes = payload.get("outcomes")
    if not isinstance(outcomes, Mapping):
        errors.append(f"{prefix}.outcomes must be an object")
        return errors
    literals, minimums, strings, timestamps = _outcome_contract(name, expected_scope=expected_scope)
    if set(outcomes) != set(literals) | set(minimums) | set(strings) | set(timestamps):
        errors.append(f"{prefix}.outcomes fields do not match the required outcome contract")
    failed = sorted(
        field
        for field, expected in literals.items()
        if not _same_typed_value(outcomes.get(field), expected)
    )
    if failed:
        errors.append(f"{prefix}.outcomes failed: {failed}")
    for field, minimum in minimums.items():
        value = outcomes.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            errors.append(f"{prefix}.outcomes.{field} must be at least {minimum}")
    for field in strings:
        value = outcomes.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{prefix}.outcomes.{field} must be non-empty")

    parsed: dict[str, datetime] = {}
    for field in timestamps:
        timestamp, error = parse_promotion_timestamp(
            outcomes.get(field), field=f"{prefix}.outcomes.{field}"
        )
        if error is not None:
            errors.append(error)
        elif timestamp is not None:
            parsed[field] = timestamp
    if name == "real_market_data":
        price_count = outcomes.get("price_row_count")
        for field in ("complete_price_row_count", "provenance_price_row_count"):
            if outcomes.get(field) != price_count:
                errors.append(f"{prefix}.outcomes.{field} must equal price_row_count")
        if (
            parsed.get("coverage_end")
            and parsed.get("coverage_start")
            and parsed["coverage_end"] <= parsed["coverage_start"]
        ):
            errors.append(f"{prefix}.outcomes coverage_end must be after coverage_start")
    if (
        name == "scoring_inputs"
        and parsed.get("calibration_valid_until")
        and parsed.get("calibration_valid_from")
        and parsed["calibration_valid_until"] <= parsed["calibration_valid_from"]
    ):
        errors.append(
            f"{prefix}.outcomes calibration_valid_until must be after calibration_valid_from"
        )
    return errors


def validate_evidence_documents_for_build(
    documents: Mapping[str, Mapping[str, Any]],
    *,
    expected_scope: Mapping[str, Any],
) -> str:
    errors: list[str] = []
    run_ids: set[str] = set()
    for name in sorted(PAPER_PROMOTION_EVIDENCE_NAMES):
        document = documents[name]
        errors.extend(validate_evidence_document(name, document, expected_scope=expected_scope))
        run_id = document.get("run_id")
        if isinstance(run_id, str) and run_id.strip():
            run_ids.add(run_id)
    if errors:
        msg = "paper promotion evidence validation failed: " + "; ".join(errors)
        raise ValueError(msg)
    if len(run_ids) != 1:
        msg = "paper promotion evidence must share one non-empty run_id"
        raise ValueError(msg)
    return next(iter(run_ids))


__all__ = [
    "PAPER_PROMOTION_EVIDENCE_NAMES",
    "PAPER_PROMOTION_EVIDENCE_SCHEMA_VERSION",
    "parse_promotion_timestamp",
    "validate_evidence_document",
    "validate_evidence_documents_for_build",
]
