"""Validated projection of panel decisions into account-neutral rebalance events."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from lib_application.db.models import StrategyPanelDecision
from lib_common.internal_events import (
    CanonicalSignalSnapshot,
    ModelRebalanceLegSnapshot,
    ModelRebalanceSubmissionEvent,
    compute_model_rebalance_content_sha256,
    compute_model_rebalance_id,
)
from lib_strategy.panels import PanelEvaluationAudit, PanelReadyInput
from lib_strategy.signals.pure_strategy import ModelStateContractError
from lib_strategy.signals.signal import Signal


class ModelRebalanceRuntimeIdentity(Protocol):
    """Runtime identity fields required to project a rebalance batch."""

    @property
    def strategy_id(self) -> str:
        """Return the immutable strategy identifier."""

    @property
    def strategy_version(self) -> str:
        """Return the immutable strategy version."""


def build_model_rebalance_event(
    *,
    identity: ModelRebalanceRuntimeIdentity,
    panel_row: StrategyPanelDecision,
    panel: PanelReadyInput,
    audit: PanelEvaluationAudit,
    signals: Sequence[Signal],
    rank_snapshot_id: str,
) -> ModelRebalanceSubmissionEvent:
    """Project one completed panel into the typed account-neutral batch."""

    if rank_snapshot_id != audit.rank_snapshot.content_digest:
        message = "persisted rank snapshot identity differs from the evaluation audit"
        raise ModelStateContractError(message)
    ordered_signals = sorted(signals, key=_model_rebalance_sequence)
    sequences = [_model_rebalance_sequence(signal) for signal in ordered_signals]
    if sequences != list(range(len(ordered_signals))):
        message = "panel signal model-rebalance sequences must be contiguous"
        raise ModelStateContractError(message)

    audit_by_instrument = {row.instrument_id: row for row in audit.rows}
    legs: list[ModelRebalanceLegSnapshot] = []
    for signal in ordered_signals:
        _validate_model_signal_lineage(
            identity=identity,
            panel_row=panel_row,
            panel=panel,
            audit=audit,
            signal=signal,
            signal_count=len(ordered_signals),
            rank_snapshot_id=rank_snapshot_id,
        )
        metadata = signal.metadata
        factor_snapshot_id = _required_metadata_text(signal, "factor_snapshot_id")
        current_factor_value = metadata.get("current_rank_factor_snapshot_id")
        current_factor = (
            str(current_factor_value).strip() if current_factor_value is not None else None
        )
        membership_value = metadata.get("membership_change_reason")
        membership_reason = str(membership_value).strip() if membership_value is not None else None
        instrument_id = _positive_signal_instrument_id(signal)
        audit_row = audit_by_instrument.get(instrument_id)
        current_rank_row_present = metadata.get("current_rank_row_present")
        if not isinstance(current_rank_row_present, bool):
            message = (
                f"Panel signal {signal.external_signal_id!r} has invalid "
                "current_rank_row_present lineage"
            )
            raise ModelStateContractError(message)
        if current_rank_row_present != (audit_row is not None):
            message = (
                f"Panel signal {signal.external_signal_id!r} disagrees with the "
                "persisted current rank-row disposition"
            )
            raise ModelStateContractError(message)
        if audit_row is not None:
            if audit_row.rank_complete:
                if current_factor != audit_row.factor_snapshot_id:
                    message = (
                        f"Panel signal {signal.external_signal_id!r} does not identify "
                        "its exact current rank factor snapshot"
                    )
                    raise ModelStateContractError(message)
            elif current_factor is not None:
                message = (
                    f"Rank-incomplete panel signal {signal.external_signal_id!r} "
                    "cannot claim current rank evidence"
                )
                raise ModelStateContractError(message)
        elif current_factor is not None:
            message = (
                f"Out-of-universe panel signal {signal.external_signal_id!r} cannot "
                "claim current rank evidence"
            )
            raise ModelStateContractError(message)

        rank_value = metadata.get("rank")
        rank = float(rank_value) if rank_value is not None else None
        snapshot = _canonical_signal_snapshot(signal)
        legs.append(
            ModelRebalanceLegSnapshot(
                leg_id=str(signal.external_signal_id),
                sequence=_model_rebalance_sequence(signal),
                phase=cast(Any, _required_metadata_text(signal, "model_rebalance_phase")),
                signal=snapshot,
                rank=rank,
                allocation_hint=float(signal.size_hint or 0.0),
                factor_snapshot_id=factor_snapshot_id,
                rank_snapshot_id=rank_snapshot_id,
                current_rank_row_present=current_rank_row_present,
                current_rank_factor_snapshot_id=current_factor,
                membership_change_reason=cast(Any, membership_reason),
            )
        )

    content_sha256 = compute_model_rebalance_content_sha256(
        strategy_id=identity.strategy_id,
        strategy_version=identity.strategy_version,
        effective_session=panel.session.session_date,
        decision_cutoff=panel.cutoff,
        execute_not_before=panel.execution_session.opens_at,
        execution_session_sha256=panel.execution_session.content_sha256,
        data_use_scope=cast(Any, panel.data_use_scope.value),
        provider_authority_sha256=panel.provider_authority_sha256,
        configuration_sha256=audit.configuration_sha256,
        input_snapshot_sha256=str(panel_row.strategy_input_sha256),
        rank_snapshot_sha256=rank_snapshot_id,
        intentional_cash_slots=audit.intentional_cash_slots,
        legs=legs,
    )
    rebalance_id = compute_model_rebalance_id(
        strategy_id=identity.strategy_id,
        strategy_version=identity.strategy_version,
        effective_session=panel.session.session_date,
        execute_not_before=panel.execution_session.opens_at,
        execution_session_sha256=panel.execution_session.content_sha256,
        data_use_scope=cast(Any, panel.data_use_scope.value),
        provider_authority_sha256=panel.provider_authority_sha256,
        configuration_sha256=audit.configuration_sha256,
        input_snapshot_sha256=str(panel_row.strategy_input_sha256),
        content_sha256=content_sha256,
    )
    return ModelRebalanceSubmissionEvent(
        event_id=rebalance_id,
        occurred_at=panel.cutoff,
        correlation_id=str(panel_row.decision_key),
        producer="indicator_runner",
        rebalance_id=rebalance_id,
        strategy_id=identity.strategy_id,
        strategy_version=identity.strategy_version,
        effective_session=panel.session.session_date,
        decision_cutoff=panel.cutoff,
        execute_not_before=panel.execution_session.opens_at,
        execution_session_sha256=panel.execution_session.content_sha256,
        data_use_scope=cast(Any, panel.data_use_scope.value),
        provider_authority_sha256=panel.provider_authority_sha256,
        configuration_sha256=audit.configuration_sha256,
        input_snapshot_sha256=str(panel_row.strategy_input_sha256),
        rank_snapshot_sha256=rank_snapshot_id,
        content_sha256=content_sha256,
        expected_leg_count=len(legs),
        intentional_cash_slots=audit.intentional_cash_slots,
        legs=legs,
    )


def _validate_model_signal_lineage(
    *,
    identity: ModelRebalanceRuntimeIdentity,
    panel_row: StrategyPanelDecision,
    panel: PanelReadyInput,
    audit: PanelEvaluationAudit,
    signal: Signal,
    signal_count: int,
    rank_snapshot_id: str,
) -> None:
    external_signal_id = str(signal.external_signal_id or "").strip()
    if not external_signal_id:
        message = "model rebalance signal lacks an external_signal_id"
        raise ModelStateContractError(message)
    if signal.strategy_id != identity.strategy_id:
        message = f"Panel signal {external_signal_id!r} has a different strategy_id"
        raise ModelStateContractError(message)
    if signal.strategy_version != identity.strategy_version:
        message = f"Panel signal {external_signal_id!r} has a different strategy_version"
        raise ModelStateContractError(message)
    if _as_utc(signal.timestamp) != _as_utc(panel.cutoff):
        message = f"Panel signal {external_signal_id!r} has a different decision cutoff"
        raise ModelStateContractError(message)
    expected = {
        "configuration_sha256": audit.configuration_sha256,
        "strategy_input_sha256": str(panel_row.strategy_input_sha256),
        "rank_snapshot_sha256": rank_snapshot_id,
        "provider_authority_sha256": panel.provider_authority_sha256,
        "data_use_scope": panel.data_use_scope.value,
        "execution_session_sha256": panel.execution_session.content_sha256,
        "execute_not_before": panel.execution_session.opens_at.isoformat(),
    }
    for field_name, expected_value in expected.items():
        if _required_metadata_text(signal, field_name) != expected_value:
            message = f"Panel signal {external_signal_id!r} has inconsistent {field_name}"
            raise ModelStateContractError(message)
    count_value = signal.metadata.get("model_rebalance_expected_leg_count")
    if isinstance(count_value, bool) or not isinstance(count_value, int):
        message = f"Panel signal {external_signal_id!r} has invalid expected leg count"
        raise ModelStateContractError(message)
    if count_value != signal_count:
        message = f"Panel signal {external_signal_id!r} has a partial batch count"
        raise ModelStateContractError(message)
    cash_value = signal.metadata.get("intentional_cash_slots")
    if cash_value != audit.intentional_cash_slots:
        message = f"Panel signal {external_signal_id!r} has inconsistent cash slots"
        raise ModelStateContractError(message)


def _model_rebalance_sequence(signal: Signal) -> int:
    value = signal.metadata.get("model_rebalance_sequence")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        message = f"Panel signal {signal.external_signal_id!r} has an invalid model sequence"
        raise ModelStateContractError(message)
    return value


def _required_metadata_text(signal: Signal, field_name: str) -> str:
    value = signal.metadata.get(field_name)
    if not isinstance(value, str) or not value.strip():
        message = f"Panel signal {signal.external_signal_id!r} requires metadata {field_name!r}"
        raise ModelStateContractError(message)
    return value.strip()


def _positive_signal_instrument_id(signal: Signal) -> int:
    value = signal.instrument_id
    if isinstance(value, bool):
        value = None
    try:
        instrument_id = int(cast(Any, value))
    except (TypeError, ValueError) as exc:
        message = f"Panel signal {signal.external_signal_id!r} has no instrument_id"
        raise ModelStateContractError(message) from exc
    if instrument_id < 1:
        message = f"Panel signal {signal.external_signal_id!r} has no instrument_id"
        raise ModelStateContractError(message)
    return instrument_id


def _canonical_signal_snapshot(signal: Signal) -> CanonicalSignalSnapshot:
    payload = signal.to_dict()
    payload = {
        key: value for key, value in payload.items() if key in CanonicalSignalSnapshot.model_fields
    }
    external_signal_id = str(signal.external_signal_id or "").strip()
    payload["action"] = str(payload["action"]).lower()
    payload["signal_id"] = external_signal_id
    payload["instrument_id"] = str(_positive_signal_instrument_id(signal))
    payload["external_signal_id"] = external_signal_id
    payload["metadata"] = dict(signal.metadata)
    return CanonicalSignalSnapshot.model_validate(payload)


def _as_utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


__all__ = ["build_model_rebalance_event"]
