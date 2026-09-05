"""Opt-in synchronized-panel capability for the durable indicator runtime."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from indicator_runner.runtime_journal import (
    BufferedSignalEmitter,
    PanelCorrectionError,
    StrategyRuntimeStore,
)
from lib_application.db.models import (
    StrategyPanelDecision,
    StrategyPanelInputRevision,
)
from lib_application.services.strategy_panel_sessions import (
    validate_strategy_panel_sessions,
)
from lib_common.hashing import canonical_json_hash
from lib_common.logging import get_logger
from lib_strategy.panels import (
    PanelEvaluationAudit,
    PanelNotReadyError,
    PanelReadyInput,
    SynchronizedPanelStrategy,
    evaluate_panel_readiness,
    panel_ready_input_from_payload,
)
from lib_strategy.signals.pure_strategy import (
    ModelStateContractError,
    PureSignalStrategy,
)

SessionFactory = Callable[[], Any]
FailureCallback = Callable[[BaseException, str], None]
logger = get_logger(__name__)


class SynchronizedPanelRuntime:
    """Prepare, evaluate, and recover complete portfolio panels exactly once."""

    def __init__(
        self,
        *,
        strategy: PureSignalStrategy,
        capability: SynchronizedPanelStrategy,
        session_factory: SessionFactory,
        signal_buffer: BufferedSignalEmitter,
        runtime_store: StrategyRuntimeStore,
        processing_lock: threading.RLock,
        relay_once: Callable[[], tuple[int, int]],
        mark_transition_failure: FailureCallback,
        clock: Callable[[], datetime],
    ) -> None:
        self._strategy = strategy
        self._capability = capability
        self._session_factory = session_factory
        self._signal_buffer = signal_buffer
        self._runtime_store = runtime_store
        self._processing_lock = processing_lock
        self._relay_once = relay_once
        self._mark_transition_failure = mark_transition_failure
        self._clock = clock
        self._started = False

    def start(self) -> int:
        """Restore the model journal and finish any crash-prepared panel."""

        if not self._strategy._is_initialized:
            self._strategy.initialize()
            self._strategy._is_initialized = True
        restored = self._runtime_store.restore(self._strategy)
        if not restored:
            with self._session_factory() as session:
                self._runtime_store.persist_initial_on_session(
                    session,
                    strategy=self._strategy,
                    source_bar=None,
                )
                session.commit()
        self._started = True
        return self.resume_prepared()

    def submit(self, panel_input: object) -> bool:
        """Persist and evaluate a new panel, or no-op an exact completed replay."""

        if not self._started:
            msg = "synchronized-panel runtime must be started before submission"
            raise RuntimeError(msg)
        panel = self._capability.panel_ready_input(panel_input)
        evaluate_panel_readiness(panel).require_complete()
        self._runtime_store.validate_panel_binding(panel, require_active=False)
        input_payload = self._capability.serialize_panel_input(panel_input)
        registered_input = self._load_registered_input(input_payload)
        registered_panel = self._capability.panel_ready_input(registered_input)
        if registered_panel.canonical_digest() != panel.canonical_digest():
            msg = "submitted panel differs from its registered immutable input"
            raise ModelStateContractError(msg)
        disposition = self._terminal_disposition(registered_panel)
        if disposition is not None:
            status, reason = disposition
            self._runtime_store.disposition_panel(
                self._strategy,
                panel=registered_panel,
                input_payload=input_payload,
                status=status,
                reason=reason,
                disposed_at=self._clock(),
            )
            return False
        preparation = self._runtime_store.prepare_panel(
            self._strategy,
            panel=registered_panel,
            input_payload=input_payload,
            now=self._clock(),
        )
        if preparation.status == "completed":
            return False
        if not preparation.should_evaluate:
            message = (
                f"Panel correction {preparation.decision_key!r} was rejected because "
                "the official session is already revision-pinned"
            )
            raise PanelCorrectionError(message)
        return self._process_prepared(preparation.decision_key)

    def resume_prepared(self) -> int:
        """Complete every prepared panel after durable state restoration."""

        completed = 0
        for decision_key in self._runtime_store.pending_panel_decision_keys():
            if self._process_prepared(decision_key):
                completed += 1
        return completed

    def poll_input_revisions(self, *, limit: int = 4) -> int:
        """Consume complete append-only data-plane inputs in cutoff order."""

        if not self._started:
            msg = "synchronized-panel runtime must be started before polling"
            raise RuntimeError(msg)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            msg = "panel runtime clock must be timezone-aware"
            raise ModelStateContractError(msg)
        binding = self._runtime_store.identity.require_panel_binding()
        with self._session_factory() as session:
            consumed = exists().where(
                StrategyPanelDecision.worker_id == self._runtime_store.identity.worker_id,
                StrategyPanelDecision.strategy_input_sha256
                == StrategyPanelInputRevision.input_sha256,
            )
            rows = list(
                session.scalars(
                    select(StrategyPanelInputRevision)
                    .where(
                        StrategyPanelInputRevision.strategy_id
                        == self._runtime_store.identity.strategy_id,
                        StrategyPanelInputRevision.strategy_version
                        == self._runtime_store.identity.strategy_version,
                        StrategyPanelInputRevision.data_use_scope == binding.data_use_scope,
                        StrategyPanelInputRevision.entitlement_owner_user_id
                        == binding.entitlement_owner_user_id,
                        StrategyPanelInputRevision.cutoff_at
                        <= now.astimezone(UTC).replace(tzinfo=None),
                        ~consumed,
                    )
                    .order_by(
                        StrategyPanelInputRevision.cutoff_at,
                        StrategyPanelInputRevision.created_at,
                        StrategyPanelInputRevision.input_sha256,
                    )
                    .limit(max(1, limit))
                )
            )
            payloads = [_registered_revision_snapshot(row) for row in rows]
        completed = 0
        for input_sha256, panel_payload, strategy_payload, header in payloads:
            panel_input = self._restore_source_input(
                input_sha256=input_sha256,
                panel_payload=panel_payload,
                strategy_payload=strategy_payload,
                header=header,
            )
            try:
                if self.submit(panel_input):
                    completed += 1
            except PanelCorrectionError:
                logger.warning(
                    "Rejected immutable synchronized-panel input correction",
                    input_sha256=input_sha256,
                    strategy_id=self._runtime_store.identity.strategy_id,
                )
        return completed

    def _process_prepared(self, decision_key: str) -> bool:
        with self._processing_lock:
            if self._signal_buffer.pending():
                msg = "panel transition began with uncommitted signals"
                raise RuntimeError(msg)
            state_mutated = False
            try:
                with self._session_factory() as session:
                    row = self._runtime_store.lock_prepared_panel_on_session(
                        session,
                        decision_key=decision_key,
                        strategy=self._strategy,
                    )
                    if row is None:
                        return False
                    registered = _require_registered_revision(
                        session,
                        str(row.strategy_input_sha256),
                        message="prepared panel has no immutable registered input proof",
                    )
                    (
                        registered_sha256,
                        registered_panel_payload,
                        registered_strategy_payload,
                        registered_header,
                    ) = _registered_revision_snapshot(registered)
                    self._restore_source_input(
                        input_sha256=registered_sha256,
                        panel_payload=registered_panel_payload,
                        strategy_payload=registered_strategy_payload,
                        header=registered_header,
                    )
                    panel, restored_input = self._restore_pinned_input(row)
                    disposition = self._terminal_disposition(panel)
                    if disposition is not None:
                        status, reason = disposition
                        _require_expired_disposition(status)
                        row.status = "expired"
                        row.rejection_reason = reason
                        row.completed_at = self._clock().astimezone(UTC).replace(tzinfo=None)
                        session.commit()
                        return False
                    validate_strategy_panel_sessions(
                        session,
                        panel=panel,
                        now=self._clock(),
                    )
                    signal_offset = self._signal_buffer.count()
                    state_mutated = True
                    audit = _require_evaluation_audit(
                        self._capability.evaluate_panel(restored_input)
                    )
                    self._strategy.flush()
                    emitted = self._signal_buffer.since(signal_offset)
                    self._runtime_store.persist_panel_transition_on_session(
                        session,
                        strategy=self._strategy,
                        panel_row=row,
                        panel=panel,
                        audit=audit,
                        signals=emitted,
                    )
                    session.commit()
            except (
                ModelStateContractError,
                OSError,
                RuntimeError,
                SQLAlchemyError,
                TypeError,
                ValueError,
            ) as exc:
                if state_mutated:
                    self._mark_transition_failure(exc, decision_key)
                raise
            self._signal_buffer.clear()
            self._relay_once()
            return True

    def _load_registered_input(self, input_payload: Mapping[str, Any]) -> object:
        input_sha256 = canonical_json_hash(input_payload)
        with self._session_factory() as session:
            row = _require_registered_revision(
                session,
                input_sha256,
                message=("panel input has not passed immutable strategy-specific registration"),
            )
            (
                registered_sha256,
                panel_payload,
                strategy_payload,
                header,
            ) = _registered_revision_snapshot(row)
        return self._restore_source_input(
            input_sha256=registered_sha256,
            panel_payload=panel_payload,
            strategy_payload=strategy_payload,
            header=header,
        )

    def _restore_pinned_input(self, row: Any) -> tuple[PanelReadyInput, object]:
        raw_panel = row.panel_payload
        if not isinstance(raw_panel, Mapping):
            msg = "persisted generic panel input is not an object"
            raise ModelStateContractError(msg)
        panel = panel_ready_input_from_payload(raw_panel)
        readiness = evaluate_panel_readiness(panel).require_complete()
        if readiness.panel_sha256 != str(row.panel_sha256):
            msg = "persisted generic panel digest changed"
            raise ModelStateContractError(msg)

        raw_strategy_input = row.strategy_input_payload
        if not isinstance(raw_strategy_input, Mapping):
            msg = "persisted strategy panel input is not an object"
            raise ModelStateContractError(msg)
        restored_input = self._capability.deserialize_panel_input(raw_strategy_input)
        restored_panel = self._capability.panel_ready_input(restored_input)
        if restored_panel.canonical_digest() != panel.canonical_digest():
            msg = "strategy codec changed the generic panel identity"
            raise ModelStateContractError(msg)
        round_trip = self._capability.serialize_panel_input(restored_input)
        if canonical_json_hash(round_trip) != str(row.strategy_input_sha256):
            msg = "strategy panel codec is not lossless"
            raise ModelStateContractError(msg)
        return panel, restored_input

    def _restore_source_input(
        self,
        *,
        input_sha256: str,
        panel_payload: Mapping[str, Any],
        strategy_payload: Mapping[str, Any],
        header: Mapping[str, Any],
    ) -> object:
        if canonical_json_hash(strategy_payload) != input_sha256:
            msg = "data-plane strategy input digest changed"
            raise ModelStateContractError(msg)
        panel = panel_ready_input_from_payload(panel_payload)
        readiness = evaluate_panel_readiness(panel).require_complete()
        expected = {
            "cutoff_at": panel.cutoff.astimezone(UTC),
            "official_session_date": panel.session.session_date,
            "execute_not_before": panel.execution_session.opens_at.astimezone(UTC),
            "data_use_scope": panel.data_use_scope.value,
            "entitlement_owner_user_id": (
                panel.provider_authority_policy.effective_entitlement_owner_user_id
            ),
            "provider_authority_sha256": panel.provider_authority_sha256,
            "membership_sha256": panel.membership_sha256,
            "factor_snapshot_sha256": panel.factor_snapshot_sha256,
            "panel_sha256": readiness.panel_sha256,
        }
        actual = {field_name: header.get(field_name) for field_name in expected}
        if actual != expected:
            msg = "data-plane panel header differs from its immutable payload"
            raise ModelStateContractError(msg)
        validator_id = header.get("strategy_validator_id")
        validator_version = header.get("strategy_validator_version")
        authority_sha256 = header.get("strategy_input_authority_sha256")
        authority_payload = header.get("strategy_input_authority_payload")
        if (
            not isinstance(validator_id, str)
            or not validator_id.strip()
            or not isinstance(validator_version, str)
            or not validator_version.strip()
            or not isinstance(authority_sha256, str)
            or not isinstance(authority_payload, Mapping)
            or canonical_json_hash(authority_payload) != authority_sha256
        ):
            msg = "data-plane strategy authority proof is missing or inconsistent"
            raise ModelStateContractError(msg)
        restored = self._capability.deserialize_panel_input(strategy_payload)
        restored_panel = self._capability.panel_ready_input(restored)
        if restored_panel.canonical_digest() != panel.canonical_digest():
            msg = "data-plane strategy payload embeds a different generic panel"
            raise ModelStateContractError(msg)
        return restored

    def _terminal_disposition(self, panel: PanelReadyInput) -> tuple[str, str] | None:
        """Classify an exact-owner input without evaluating stale evidence."""

        self._runtime_store.validate_panel_binding(panel, require_active=False)
        binding = self._runtime_store.identity.require_panel_binding()
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            msg = "panel runtime clock must be timezone-aware"
            raise ModelStateContractError(msg)
        current = now.astimezone(UTC)
        if panel.cutoff > current:
            msg = "synchronized-panel cutoff is in the future"
            raise PanelNotReadyError(msg)
        if panel.cutoff < binding.activation_cutoff:
            return ("skipped", "panel_cutoff_precedes_activation_watermark")
        if (
            binding.data_use_scope == "paper_forward"
            and current >= panel.execution_session.closes_at
        ):
            return ("expired", "declared_execution_window_elapsed")
        return None


def _require_evaluation_audit(value: object) -> PanelEvaluationAudit:
    if not isinstance(value, PanelEvaluationAudit):
        message = "a prepared panel must return a complete PanelEvaluationAudit"
        raise ModelStateContractError(message)
    return value


def _require_expired_disposition(status: str) -> None:
    if status != "expired":
        message = "A prepared panel cannot precede its activation watermark"
        raise ModelStateContractError(message)


def _registered_revision_snapshot(
    row: StrategyPanelInputRevision,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    return (
        str(row.input_sha256),
        dict(row.panel_payload),
        dict(row.strategy_input_payload),
        {
            "cutoff_at": _utc(row.cutoff_at),
            "official_session_date": row.official_session_date,
            "execute_not_before": _utc(row.execute_not_before),
            "data_use_scope": str(row.data_use_scope),
            "entitlement_owner_user_id": row.entitlement_owner_user_id,
            "provider_authority_sha256": str(row.provider_authority_sha256),
            "membership_sha256": str(row.membership_sha256),
            "factor_snapshot_sha256": str(row.factor_snapshot_sha256),
            "panel_sha256": str(row.panel_sha256),
            "strategy_validator_id": str(row.strategy_validator_id),
            "strategy_validator_version": str(row.strategy_validator_version),
            "strategy_input_authority_sha256": str(row.strategy_input_authority_sha256),
            "strategy_input_authority_payload": dict(row.strategy_input_authority_payload),
        },
    )


def _require_registered_revision(
    session: Session,
    input_sha256: str,
    *,
    message: str,
) -> StrategyPanelInputRevision:
    row = session.get(StrategyPanelInputRevision, input_sha256)
    if row is None:
        raise ModelStateContractError(message)
    return row


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["SynchronizedPanelRuntime"]
