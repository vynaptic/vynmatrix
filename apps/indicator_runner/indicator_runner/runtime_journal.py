"""Durable shared-model journal and signal relay for indicator workers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    StrategyDecision,
    StrategyPanelDecision,
    StrategyPanelInputRevision,
    StrategyRuntimeState,
)
from lib_application.outbox import OutboxBacklogSnapshot, OutboxStore, backlog_age_seconds
from lib_application.services.equity_rank_snapshots import persist_equity_rank_snapshot
from lib_application.services.price_ingestion_service import PriceIngestionService
from lib_application.services.strategy_panel_sessions import (
    validate_strategy_panel_sessions,
)
from lib_common.hashing import canonical_json_hash
from lib_common.internal_events import ModelRebalanceSubmissionEvent
from lib_common.logging import get_logger
from lib_common.metrics import gauge
from lib_common.time_utils import now_utc
from lib_data.bars import Bar
from lib_data.watermark import Watermark
from lib_strategy.panels import (
    PanelEvaluationAudit,
    PanelReadyInput,
    evaluate_panel_readiness,
    panel_evaluation_audit_to_payload,
    panel_ready_input_to_payload,
)
from lib_strategy.signals.emitter import SignalEmitter
from lib_strategy.signals.pure_strategy import ModelStateContractError, PureSignalStrategy
from lib_strategy.signals.signal import Signal, SignalAction

from .model_rebalance_projection import build_model_rebalance_event
from .panel_binding import (
    PanelCorrectionError,
    PanelPreparation,
)
from .runtime_params import DEFAULT_MAX_PANEL_AGE_DAYS, StrategyRuntimeIdentity

logger = get_logger(__name__)

SIGNAL_SUBMISSION_TOPIC = "signals.submit"
MODEL_REBALANCE_SUBMISSION_TOPIC = "signals.rebalances.submit"
SIGNAL_OUTBOX_TOPICS = (
    SIGNAL_SUBMISSION_TOPIC,
    MODEL_REBALANCE_SUBMISSION_TOPIC,
)
_BACKLOG_GAUGE = "vm_indicator_signal_backlog_events"
_BACKLOG_AGE_GAUGE = "vm_indicator_signal_backlog_oldest_seconds"
_STRATEGY_LAG_GAUGE = "vm_indicator_strategy_lag_seconds"
_OPERATIONAL_READY_GAUGE = "vm_indicator_worker_operational_ready"
_DEFAULT_MAX_PANEL_AGE_SECONDS = DEFAULT_MAX_PANEL_AGE_DAYS * 24 * 60 * 60

SessionFactory = Callable[[], Any]


@dataclass(frozen=True)
class StrategyBarDecision:
    """One deterministic strategy-bar evaluation and its emitted envelopes."""

    symbol: str
    strategy_bar_ts: datetime
    signals: tuple[Signal, ...]


@dataclass(frozen=True)
class SignalRelayReadiness:
    """Observable durable signal-delivery backlog for one worker partition."""

    ready: bool
    counts: dict[str, int]
    oldest_age_seconds: float


@dataclass(frozen=True)
class StrategyFeedLag:
    """Latest source-price to durable strategy-watermark lag for one feed."""

    symbol: str
    latest_price_ts: datetime | None
    watermark_ts: datetime | None
    lag_seconds: float | None
    ready: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe health-detail representation."""
        return {
            "symbol": self.symbol,
            "latest_price_ts": _iso_or_none(self.latest_price_ts),
            "watermark_ts": _iso_or_none(self.watermark_ts),
            "lag_seconds": self.lag_seconds,
            "ready": self.ready,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class StrategyPanelProgress:
    """Latest durable production-input disposition for a panel worker."""

    ready: bool
    reason: str
    latest_input_sha256: str | None
    latest_input_cutoff: datetime | None
    latest_decision_key: str | None
    latest_decision_status: str | None
    latest_completed_cutoff: datetime | None
    age_seconds: float | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe panel readiness detail."""

        return {
            "ready": self.ready,
            "reason": self.reason,
            "latest_input_sha256": self.latest_input_sha256,
            "latest_input_cutoff": _iso_or_none(self.latest_input_cutoff),
            "latest_decision_key": self.latest_decision_key,
            "latest_decision_status": self.latest_decision_status,
            "latest_completed_cutoff": _iso_or_none(self.latest_completed_cutoff),
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True)
class StrategyOperationalStatus:
    """Parent-visible operational readiness for one selected worker."""

    worker_id: str
    strategy_id: str
    ready: bool
    outbox_counts: dict[str, int]
    outbox_oldest_age_seconds: float
    feed_lags: tuple[StrategyFeedLag, ...]
    error: str | None = None
    panel_progress: StrategyPanelProgress | None = None

    @classmethod
    def unavailable(
        cls,
        *,
        worker_id: str,
        strategy_id: str,
        error: str,
    ) -> StrategyOperationalStatus:
        """Create a fail-closed result when the operational query failed."""
        status = cls(
            worker_id=worker_id,
            strategy_id=strategy_id,
            ready=False,
            outbox_counts={},
            outbox_oldest_age_seconds=0.0,
            feed_lags=(),
            error=error,
        )
        _record_operational_metrics(status)
        return status

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe health-detail representation."""
        return {
            "worker_id": self.worker_id,
            "strategy_id": self.strategy_id,
            "ready": self.ready,
            "outbox": {
                "counts": dict(self.outbox_counts),
                "oldest_age_seconds": self.outbox_oldest_age_seconds,
            },
            "feeds": [feed.to_dict() for feed in self.feed_lags],
            "panel": self.panel_progress.to_dict() if self.panel_progress else None,
            "error": self.error,
        }


class BufferedSignalEmitter(SignalEmitter):
    """Capture canonical signals until the surrounding DB transaction commits."""

    def __init__(self) -> None:
        self._pending: list[Signal] = []

    def emit(self, signal: Signal) -> None:
        # Existing per-symbol HOLD remains a no-op. A synchronized portfolio
        # HOLD is an explicit target leg and must remain in the atomic batch.
        is_model_leg = bool(
            str(
                signal.metadata.get("strategy_rebalance_id")
                or signal.metadata.get("model_rebalance_id")
                or ""
            ).strip()
        )
        if signal.action != SignalAction.HOLD or is_model_leg:
            self._pending.append(signal)

    def flush(self) -> None:
        """No-op; persistence is controlled by the worker transaction."""

    def pending(self) -> tuple[Signal, ...]:
        """Return the uncommitted signals without transferring ownership."""
        return tuple(self._pending)

    def count(self) -> int:
        """Return the current transaction-local signal count."""
        return len(self._pending)

    def since(self, offset: int) -> tuple[Signal, ...]:
        """Return signals appended after a transaction-local checkpoint."""
        if offset < 0 or offset > len(self._pending):
            msg = f"Invalid durable signal buffer offset: {offset}"
            raise ValueError(msg)
        return tuple(self._pending[offset:])

    def clear(self) -> None:
        """Forget signals only after their journal transaction commits."""
        self._pending.clear()


class StrategyRuntimeStore:
    """Persistence operations for one strategy worker's durable state machine."""

    def __init__(
        self,
        session_factory: SessionFactory,
        identity: StrategyRuntimeIdentity,
    ) -> None:
        self._session_factory = session_factory
        self.identity = identity
        self._outbox = OutboxStore(session_factory)

    def restore(
        self,
        strategy: PureSignalStrategy,
        *,
        checkpoint_ts_by_symbol: dict[str, datetime] | None = None,
    ) -> bool:
        """Restore the latest compatible state; return false when none exists."""
        with self._session_factory() as session:
            row = session.get(StrategyRuntimeState, self.identity.worker_id)
            if row is None:
                return False
            self._validate_row(row, strategy=strategy)
            if checkpoint_ts_by_symbol is not None:
                self._validate_checkpoint(row, checkpoint_ts_by_symbol)
            payload = dict(row.state_payload or {})
        strategy.restore_model_state(payload)
        return True

    def prepare_panel(
        self,
        strategy: PureSignalStrategy,
        *,
        panel: PanelReadyInput,
        input_payload: Mapping[str, Any],
        now: datetime,
    ) -> PanelPreparation:
        """Revision-pin one complete panel before cross-sectional evaluation."""

        self.validate_panel_binding(panel, require_active=True)
        readiness = evaluate_panel_readiness(panel).require_complete()
        panel_payload = panel_ready_input_to_payload(panel)
        canonical_input = _canonical_json_object(
            input_payload,
            field_name="strategy panel input",
        )
        strategy_input_sha256 = canonical_json_hash(canonical_input)
        decision_key = _panel_decision_key(
            identity=self.identity,
            cutoff=panel.cutoff,
            strategy_input_sha256=strategy_input_sha256,
        )
        cutoff = _as_db_timestamp(panel.cutoff)

        with self._session_factory() as session:
            # Keep the worker row first in the global lock order. Completion
            # already locks it before rank persistence locks instruments; doing
            # the inverse here can deadlock a concurrent prepare against that
            # completion transaction on PostgreSQL.
            runtime = self._lock_runtime_on_session(session, strategy=strategy)
            validate_strategy_panel_sessions(
                session,
                panel=panel,
                now=now,
            )
            prepared_state = _canonical_json_object(
                runtime.state_payload,
                field_name="persisted strategy state",
            )
            prepared_state_sha256 = canonical_json_hash(prepared_state)
            existing = session.get(StrategyPanelDecision, decision_key)
            if existing is not None:
                self._validate_panel_row(
                    existing,
                    panel=panel,
                    panel_payload=panel_payload,
                    strategy_input_payload=canonical_input,
                    panel_sha256=readiness.panel_sha256,
                    strategy_input_sha256=strategy_input_sha256,
                    state_schema_version=strategy.model_state_schema_version,
                )
                return PanelPreparation(
                    decision_key=decision_key,
                    status=str(existing.status),
                )

            latest_completed = session.scalar(
                select(StrategyPanelDecision)
                .where(
                    StrategyPanelDecision.worker_id == self.identity.worker_id,
                    StrategyPanelDecision.status == "completed",
                )
                .order_by(StrategyPanelDecision.cutoff_at.desc())
                .limit(1)
            )
            if latest_completed is not None and _as_utc(cutoff) < _as_utc(
                latest_completed.cutoff_at
            ):
                row = self._new_panel_row(
                    strategy=strategy,
                    panel=panel,
                    panel_payload=panel_payload,
                    strategy_input_payload=canonical_input,
                    panel_sha256=readiness.panel_sha256,
                    strategy_input_sha256=strategy_input_sha256,
                    decision_key=decision_key,
                    prepared_state_generation=int(runtime.generation),
                    prepared_state_sha256=prepared_state_sha256,
                    status="rejected_correction",
                    correction_of_decision_key=str(latest_completed.decision_key),
                    rejection_reason="panel cutoff precedes the latest completed cutoff",
                    completed_at=now_utc(),
                )
                session.add(row)
                session.commit()
                return PanelPreparation(
                    decision_key=decision_key,
                    status="rejected_correction",
                )

            # The runtime row is locked before this lookup. Every production
            # prepare/completion path takes that same worker lock first, so
            # concurrent PostgreSQL writers serialize before deciding which
            # input pins an official session. Keep the invariant here instead
            # of a unique index: rejected corrections are intentionally retained
            # as multiple auditable rows for the same session.
            pinned = session.scalar(
                select(StrategyPanelDecision)
                .where(
                    StrategyPanelDecision.worker_id == self.identity.worker_id,
                    StrategyPanelDecision.official_session_date == panel.session.session_date,
                    StrategyPanelDecision.status.in_(("prepared", "completed")),
                )
                .order_by(
                    StrategyPanelDecision.created_at,
                    StrategyPanelDecision.decision_key,
                )
                .limit(1)
            )
            if pinned is not None:
                row = self._new_panel_row(
                    strategy=strategy,
                    panel=panel,
                    panel_payload=panel_payload,
                    strategy_input_payload=canonical_input,
                    panel_sha256=readiness.panel_sha256,
                    strategy_input_sha256=strategy_input_sha256,
                    decision_key=decision_key,
                    prepared_state_generation=int(runtime.generation),
                    prepared_state_sha256=prepared_state_sha256,
                    status="rejected_correction",
                    correction_of_decision_key=str(pinned.decision_key),
                    rejection_reason=(
                        "immutable panel content already pinned for this worker official session"
                    ),
                    completed_at=now_utc(),
                )
                session.add(row)
                session.commit()
                return PanelPreparation(
                    decision_key=decision_key,
                    status="rejected_correction",
                )

            outstanding = session.scalar(
                select(StrategyPanelDecision.decision_key)
                .where(
                    StrategyPanelDecision.worker_id == self.identity.worker_id,
                    StrategyPanelDecision.status == "prepared",
                )
                .limit(1)
            )
            if outstanding is not None:
                message = (
                    f"Worker {self.identity.worker_id!r} already has prepared panel {outstanding!r}"
                )
                raise ModelStateContractError(message)

            session.add(
                self._new_panel_row(
                    strategy=strategy,
                    panel=panel,
                    panel_payload=panel_payload,
                    strategy_input_payload=canonical_input,
                    panel_sha256=readiness.panel_sha256,
                    strategy_input_sha256=strategy_input_sha256,
                    decision_key=decision_key,
                    prepared_state_generation=int(runtime.generation),
                    prepared_state_sha256=prepared_state_sha256,
                    status="prepared",
                    correction_of_decision_key=None,
                    rejection_reason=None,
                    completed_at=None,
                )
            )
            session.commit()
        return PanelPreparation(decision_key=decision_key, status="prepared")

    def validate_panel_binding(
        self,
        panel: PanelReadyInput,
        *,
        require_active: bool,
    ) -> None:
        """Prove a panel belongs to this worker's exact owner/scope fence."""

        binding = self.identity.require_panel_binding()
        panel_owner = panel.provider_authority_policy.effective_entitlement_owner_user_id
        if panel.data_use_scope.value != binding.data_use_scope:
            message = (
                f"Panel scope {panel.data_use_scope.value!r} is not authorized for "
                f"worker scope {binding.data_use_scope!r}"
            )
            raise ModelStateContractError(message)
        if panel_owner != binding.entitlement_owner_user_id:
            message = "Panel entitlement owner differs from worker owner"
            raise ModelStateContractError(message)
        if require_active and _as_utc(panel.cutoff) < binding.activation_cutoff:
            message = "Panel cutoff precedes the worker activation watermark"
            raise ModelStateContractError(message)

    def disposition_panel(
        self,
        strategy: PureSignalStrategy,
        *,
        panel: PanelReadyInput,
        input_payload: Mapping[str, Any],
        status: str,
        reason: str,
        disposed_at: datetime,
    ) -> PanelPreparation:
        """Persist an immutable skipped/expired input without model evaluation."""

        if status not in {"skipped", "expired"}:
            message = "Panel disposition must be skipped or expired"
            raise ModelStateContractError(message)
        normalized_reason = reason.strip()
        if not normalized_reason:
            message = "Panel terminal disposition requires a reason"
            raise ModelStateContractError(message)
        self.validate_panel_binding(panel, require_active=False)
        readiness = evaluate_panel_readiness(panel).require_complete()
        panel_payload = panel_ready_input_to_payload(panel)
        canonical_input = _canonical_json_object(
            input_payload,
            field_name="strategy panel input",
        )
        strategy_input_sha256 = canonical_json_hash(canonical_input)
        decision_key = _panel_decision_key(
            identity=self.identity,
            cutoff=panel.cutoff,
            strategy_input_sha256=strategy_input_sha256,
        )
        with self._session_factory() as session:
            runtime = self._lock_runtime_on_session(session, strategy=strategy)
            existing = session.get(StrategyPanelDecision, decision_key, with_for_update=True)
            if existing is not None:
                self._validate_panel_row(
                    existing,
                    panel=panel,
                    panel_payload=panel_payload,
                    strategy_input_payload=canonical_input,
                    panel_sha256=readiness.panel_sha256,
                    strategy_input_sha256=strategy_input_sha256,
                    state_schema_version=strategy.model_state_schema_version,
                )
                if str(existing.status) in {"skipped", "expired"}:
                    return PanelPreparation(decision_key=decision_key, status=str(existing.status))
                if str(existing.status) != "prepared" or status != "expired":
                    message = (
                        f"Panel {decision_key!r} cannot transition from "
                        f"{existing.status!r} to {status!r}"
                    )
                    raise ModelStateContractError(message)
                existing.status = "expired"
                existing.rejection_reason = normalized_reason
                existing.completed_at = _as_db_timestamp(disposed_at)
                session.commit()
                return PanelPreparation(decision_key=decision_key, status="expired")

            prepared_state = _canonical_json_object(
                runtime.state_payload,
                field_name="persisted strategy state",
            )
            session.add(
                self._new_panel_row(
                    strategy=strategy,
                    panel=panel,
                    panel_payload=panel_payload,
                    strategy_input_payload=canonical_input,
                    panel_sha256=readiness.panel_sha256,
                    strategy_input_sha256=strategy_input_sha256,
                    decision_key=decision_key,
                    prepared_state_generation=int(runtime.generation),
                    prepared_state_sha256=canonical_json_hash(prepared_state),
                    status=status,
                    correction_of_decision_key=None,
                    rejection_reason=normalized_reason,
                    completed_at=_as_db_timestamp(disposed_at),
                )
            )
            session.commit()
        return PanelPreparation(decision_key=decision_key, status=status)

    def pending_panel_decision_keys(self) -> tuple[str, ...]:
        """Return prepared panels in deterministic cutoff order."""

        with self._session_factory() as session:
            rows = session.scalars(
                select(StrategyPanelDecision.decision_key)
                .where(
                    StrategyPanelDecision.worker_id == self.identity.worker_id,
                    StrategyPanelDecision.status == "prepared",
                )
                .order_by(
                    StrategyPanelDecision.cutoff_at,
                    StrategyPanelDecision.created_at,
                    StrategyPanelDecision.decision_key,
                )
            ).all()
        return tuple(str(item) for item in rows)

    def lock_prepared_panel_on_session(
        self,
        session: Session,
        *,
        decision_key: str,
        strategy: PureSignalStrategy,
    ) -> StrategyPanelDecision | None:
        """Lock a prepared panel and prove its pre-state is still current."""

        runtime = self._lock_runtime_on_session(session, strategy=strategy)
        row = session.get(StrategyPanelDecision, decision_key, with_for_update=True)
        if row is None or str(row.worker_id) != self.identity.worker_id:
            message = f"Unknown synchronized-panel decision {decision_key!r}"
            raise ModelStateContractError(message)
        if str(row.status) == "completed":
            return None
        if str(row.status) != "prepared":
            message = (
                f"Synchronized-panel decision {decision_key!r} cannot run from "
                f"status {row.status!r}"
            )
            raise PanelCorrectionError(message)
        if int(row.state_schema_version) != strategy.model_state_schema_version:
            message = f"Panel {decision_key!r} state schema changed after preparation"
            raise ModelStateContractError(message)
        persisted_state = _canonical_json_object(
            runtime.state_payload,
            field_name="persisted strategy state",
        )
        active_state = _canonical_json_object(
            strategy.serialize_model_state(),
            field_name="active strategy state",
        )
        persisted_state_sha256 = canonical_json_hash(persisted_state)
        if persisted_state != active_state:
            message = (
                f"Active strategy state for {self.identity.worker_id!r} is stale "
                "relative to the durable panel journal"
            )
            raise ModelStateContractError(message)
        if (
            int(row.prepared_state_generation) != int(runtime.generation)
            or str(row.prepared_state_sha256) != persisted_state_sha256
        ):
            message = f"Panel {decision_key!r} prepared model state is no longer current"
            raise ModelStateContractError(message)
        payload = _canonical_json_object(
            row.strategy_input_payload,
            field_name="persisted strategy panel input",
        )
        if canonical_json_hash(payload) != str(row.strategy_input_sha256):
            message = f"Panel {decision_key!r} strategy payload digest changed"
            raise ModelStateContractError(message)
        return row

    def persist_panel_transition_on_session(
        self,
        session: Session,
        *,
        strategy: PureSignalStrategy,
        panel_row: StrategyPanelDecision,
        panel: PanelReadyInput,
        audit: PanelEvaluationAudit,
        signals: Sequence[Signal],
    ) -> StrategyRuntimeState:
        """Atomically commit panel state, signal batch, and outbox envelopes."""

        if str(panel_row.status) != "prepared":
            message = f"Panel {panel_row.decision_key!r} is not prepared"
            raise ModelStateContractError(message)
        for signal in signals:
            external_signal_id = str(signal.external_signal_id or "").strip()
            if not external_signal_id:
                message = (
                    f"Panel {panel_row.decision_key!r} emitted a signal without stable identity"
                )
                raise ModelStateContractError(message)

        runtime = self._persist_state_on_session(
            session,
            strategy=strategy,
            source_bar=None,
        )
        persisted_rank = persist_equity_rank_snapshot(
            session,
            strategy_id=self.identity.strategy_id,
            strategy_version=self.identity.strategy_version,
            panel=panel,
            audit=audit,
        )
        state_payload = _canonical_json_object(
            runtime.state_payload,
            field_name="completed strategy state",
        )
        audit_payload = panel_evaluation_audit_to_payload(audit)
        panel_row.status = "completed"
        panel_row.state_generation = int(runtime.generation)
        panel_row.state_payload = state_payload
        panel_row.state_sha256 = canonical_json_hash(state_payload)
        panel_row.rank_snapshot_id = persisted_rank.rank_snapshot_id
        panel_row.evaluation_audit_payload = audit_payload
        panel_row.evaluation_audit_sha256 = canonical_json_hash(audit_payload)
        panel_row.completed_at = now_utc()
        event = build_model_rebalance_event(
            identity=self.identity,
            panel_row=panel_row,
            panel=panel,
            audit=audit,
            signals=signals,
            rank_snapshot_id=str(persisted_rank.rank_snapshot_id),
        )
        panel_row.signal_envelopes = [leg.signal.model_dump(mode="json") for leg in event.legs]
        self._outbox.enqueue_on_session(
            session,
            topic=MODEL_REBALANCE_SUBMISSION_TOPIC,
            event_type=event.event_type,
            schema_version=event.schema_version,
            aggregate_type="model_rebalance",
            aggregate_id=event.rebalance_id,
            event_key=f"model-rebalance:{event.rebalance_id}",
            ordering_key=self.identity.ordering_key,
            payload=event.model_dump(mode="json"),
            headers={
                "rebalance_id": event.rebalance_id,
                "worker_id": self.identity.worker_id,
                "expected_leg_count": event.expected_leg_count,
            },
        )
        return runtime

    def _lock_runtime_on_session(
        self,
        session: Session,
        *,
        strategy: PureSignalStrategy,
    ) -> StrategyRuntimeState:
        runtime = session.scalar(
            select(StrategyRuntimeState)
            .where(StrategyRuntimeState.worker_id == self.identity.worker_id)
            .with_for_update()
        )
        if runtime is None:
            message = (
                "Cannot prepare a panel before durable runtime initialization "
                f"for {self.identity.worker_id!r}"
            )
            raise ModelStateContractError(message)
        self._validate_row(runtime, strategy=strategy)
        return runtime

    def _new_panel_row(
        self,
        *,
        strategy: PureSignalStrategy,
        panel: PanelReadyInput,
        panel_payload: Mapping[str, Any],
        strategy_input_payload: Mapping[str, Any],
        panel_sha256: str,
        strategy_input_sha256: str,
        decision_key: str,
        prepared_state_generation: int,
        prepared_state_sha256: str,
        status: str,
        correction_of_decision_key: str | None,
        rejection_reason: str | None,
        completed_at: datetime | None,
    ) -> StrategyPanelDecision:
        binding = self.identity.require_panel_binding()
        maximum_revision = max(
            (item.content_revision for item in panel.observations),
            default=None,
        )
        return StrategyPanelDecision(
            decision_key=decision_key,
            worker_id=self.identity.worker_id,
            strategy_id=self.identity.strategy_id,
            strategy_version=self.identity.strategy_version,
            cutoff_at=_as_db_timestamp(panel.cutoff),
            official_session_date=panel.session.session_date,
            official_session_sha256=panel.session.content_sha256,
            execute_not_before=_as_db_timestamp(panel.execution_session.opens_at),
            execution_session_sha256=panel.execution_session.content_sha256,
            data_use_scope=panel.data_use_scope.value,
            entitlement_owner_user_id=binding.entitlement_owner_user_id,
            runtime_environment=binding.environment,
            activation_cutoff=_as_db_timestamp(binding.activation_cutoff),
            provider_authority_policy=panel.provider_authority_policy.to_payload(),
            provider_authority_sha256=panel.provider_authority_sha256,
            membership_sha256=panel.membership_sha256,
            factor_snapshot_sha256=panel.factor_snapshot_sha256,
            panel_sha256=panel_sha256,
            strategy_input_sha256=strategy_input_sha256,
            member_count=len(panel.members),
            observation_count=len(panel.observations),
            exclusion_count=len(panel.exclusions),
            maximum_content_revision=maximum_revision,
            panel_payload=dict(panel_payload),
            strategy_input_payload=dict(strategy_input_payload),
            status=status,
            correction_of_decision_key=correction_of_decision_key,
            rejection_reason=rejection_reason,
            state_schema_version=strategy.model_state_schema_version,
            prepared_state_generation=prepared_state_generation,
            prepared_state_sha256=prepared_state_sha256,
            signal_envelopes=[],
            completed_at=completed_at,
        )

    def _validate_panel_row(
        self,
        row: StrategyPanelDecision,
        *,
        panel: PanelReadyInput,
        panel_payload: Mapping[str, Any],
        strategy_input_payload: Mapping[str, Any],
        panel_sha256: str,
        strategy_input_sha256: str,
        state_schema_version: int,
    ) -> None:
        actual = {
            "worker_id": str(row.worker_id),
            "strategy_id": str(row.strategy_id),
            "strategy_version": str(row.strategy_version),
            "cutoff_at": _as_utc(row.cutoff_at),
            "official_session_date": row.official_session_date,
            "official_session_sha256": str(row.official_session_sha256),
            "execute_not_before": _as_utc(row.execute_not_before),
            "execution_session_sha256": str(row.execution_session_sha256),
            "data_use_scope": str(row.data_use_scope),
            "entitlement_owner_user_id": (
                str(row.entitlement_owner_user_id)
                if row.entitlement_owner_user_id is not None
                else None
            ),
            "runtime_environment": str(row.runtime_environment),
            "activation_cutoff": _as_utc(row.activation_cutoff),
            "provider_authority_policy": _canonical_json_object(
                row.provider_authority_policy,
                field_name="persisted provider authority policy",
            ),
            "provider_authority_sha256": str(row.provider_authority_sha256),
            "membership_sha256": str(row.membership_sha256),
            "factor_snapshot_sha256": str(row.factor_snapshot_sha256),
            "panel_sha256": str(row.panel_sha256),
            "strategy_input_sha256": str(row.strategy_input_sha256),
            "state_schema_version": int(row.state_schema_version),
            "panel_payload": _canonical_json_object(
                row.panel_payload,
                field_name="persisted generic panel",
            ),
            "strategy_input_payload": _canonical_json_object(
                row.strategy_input_payload,
                field_name="persisted strategy panel",
            ),
        }
        expected = {
            "worker_id": self.identity.worker_id,
            "strategy_id": self.identity.strategy_id,
            "strategy_version": self.identity.strategy_version,
            "cutoff_at": _as_utc(panel.cutoff),
            "official_session_date": panel.session.session_date,
            "official_session_sha256": panel.session.content_sha256,
            "execute_not_before": _as_utc(panel.execution_session.opens_at),
            "execution_session_sha256": panel.execution_session.content_sha256,
            "data_use_scope": panel.data_use_scope.value,
            "entitlement_owner_user_id": (
                self.identity.require_panel_binding().entitlement_owner_user_id
            ),
            "runtime_environment": self.identity.require_panel_binding().environment,
            "activation_cutoff": self.identity.require_panel_binding().activation_cutoff,
            "provider_authority_policy": panel.provider_authority_policy.to_payload(),
            "provider_authority_sha256": panel.provider_authority_sha256,
            "membership_sha256": panel.membership_sha256,
            "factor_snapshot_sha256": panel.factor_snapshot_sha256,
            "panel_sha256": panel_sha256,
            "strategy_input_sha256": strategy_input_sha256,
            "state_schema_version": state_schema_version,
            "panel_payload": dict(panel_payload),
            "strategy_input_payload": dict(strategy_input_payload),
        }
        if actual != expected:
            message = f"Synchronized panel {row.decision_key!r} replay changed content"
            raise ModelStateContractError(message)

    def persist_initial_on_session(
        self,
        session: Session,
        *,
        strategy: PureSignalStrategy,
        source_bar: Bar | None = None,
    ) -> StrategyRuntimeState:
        """Create or validate an initial snapshot in the caller's transaction."""
        return self._persist_state_on_session(
            session,
            strategy=strategy,
            source_bar=source_bar,
        )

    def persist_rebuild_on_session(
        self,
        session: Session,
        *,
        strategy: PureSignalStrategy,
        source_bar: Bar | None,
    ) -> StrategyRuntimeState:
        """Persist a rebuilt state snapshot without creating signal envelopes."""
        return self._persist_state_on_session(
            session,
            strategy=strategy,
            source_bar=source_bar,
        )

    def persist_transition_on_session(
        self,
        session: Session,
        *,
        strategy: PureSignalStrategy,
        source_bar: Bar,
        decisions: Sequence[StrategyBarDecision],
    ) -> StrategyRuntimeState:
        """Persist state, decision journal, and signal envelopes atomically."""
        row = self._persist_state_on_session(
            session,
            strategy=strategy,
            source_bar=source_bar,
        )
        price_id, content_revision = _source_identity(source_bar)
        for decision in decisions:
            decision_key = _decision_key(
                identity=self.identity,
                decision=decision,
                source_price_id=price_id,
                source_content_revision=content_revision,
            )
            existing = session.get(StrategyDecision, decision_key)
            envelopes = [signal.to_dict() for signal in decision.signals]
            if existing is None:
                session.add(
                    StrategyDecision(
                        decision_key=decision_key,
                        worker_id=self.identity.worker_id,
                        strategy_id=self.identity.strategy_id,
                        strategy_version=self.identity.strategy_version,
                        symbol=decision.symbol,
                        source=self.identity.source,
                        timeframe=self.identity.timeframe,
                        consolidation_minutes=self.identity.consolidation_minutes,
                        strategy_bar_ts=_as_db_timestamp(decision.strategy_bar_ts),
                        source_price_id=price_id,
                        source_content_revision=content_revision,
                        state_schema_version=strategy.model_state_schema_version,
                        signal_envelopes=envelopes,
                    )
                )
            elif list(existing.signal_envelopes or []) != envelopes:
                msg = f"Strategy decision {decision_key} replayed with different envelopes"
                raise ModelStateContractError(msg)

            for signal in decision.signals:
                self._enqueue_strategy_signal(
                    session,
                    decision_key=decision_key,
                    signal=signal,
                    source_identity={
                        "source_price_id": price_id,
                        "source_content_revision": content_revision,
                    },
                )
        return row

    def _enqueue_strategy_signal(
        self,
        session: Session,
        *,
        decision_key: str,
        signal: Signal,
        source_identity: Mapping[str, Any],
    ) -> None:
        external_signal_id = str(signal.external_signal_id or "").strip()
        if not external_signal_id:
            message = f"Strategy decision {decision_key} emitted a signal without stable identity"
            raise ModelStateContractError(message)
        self._outbox.enqueue_on_session(
            session,
            topic=SIGNAL_SUBMISSION_TOPIC,
            event_type="StrategySignalSubmitted",
            schema_version="v1",
            aggregate_type="strategy",
            aggregate_id=self.identity.strategy_id,
            event_key=f"strategy-signal:{external_signal_id}",
            ordering_key=self.identity.ordering_key,
            payload={
                "decision_key": decision_key,
                "signal": signal.to_dict(),
                **dict(source_identity),
            },
            headers={
                "external_signal_id": external_signal_id,
                "worker_id": self.identity.worker_id,
            },
        )

    def _persist_state_on_session(
        self,
        session: Session,
        *,
        strategy: PureSignalStrategy,
        source_bar: Bar | None,
    ) -> StrategyRuntimeState:
        snapshot = strategy.serialize_model_state()
        row = cast(
            StrategyRuntimeState | None,
            session.get(StrategyRuntimeState, self.identity.worker_id),
        )
        if row is None:
            binding = self.identity.panel_binding
            row = StrategyRuntimeState(
                worker_id=self.identity.worker_id,
                strategy_id=self.identity.strategy_id,
                strategy_version=self.identity.strategy_version,
                symbols=list(self.identity.symbols),
                source=self.identity.source,
                timeframe=self.identity.timeframe,
                consolidation_minutes=self.identity.consolidation_minutes,
                config_fingerprint=self.identity.config_fingerprint,
                panel_runtime_environment=(binding.environment if binding else None),
                panel_data_use_scope=(binding.data_use_scope if binding else None),
                panel_entitlement_owner_user_id=(
                    binding.entitlement_owner_user_id if binding else None
                ),
                panel_activation_cutoff=(
                    _as_db_timestamp(binding.activation_cutoff) if binding else None
                ),
                state_schema_version=strategy.model_state_schema_version,
                state_payload=snapshot,
                generation=1,
            )
            session.add(row)
        else:
            self._validate_row(row, strategy=strategy)
            row.state_payload = snapshot
            row.generation = int(row.generation) + 1
            row.updated_at = now_utc()

        if source_bar is not None:
            price_id, content_revision = _source_identity(source_bar)
            row.last_source_symbol = source_bar.symbol
            row.last_source_price_id = price_id
            row.last_source_ts = _as_db_timestamp(source_bar.timestamp)
            row.last_source_content_revision = content_revision
        return row

    def _validate_row(
        self,
        row: StrategyRuntimeState,
        *,
        strategy: PureSignalStrategy,
    ) -> None:
        actual = {
            "strategy_id": str(row.strategy_id),
            "strategy_version": str(row.strategy_version),
            "symbols": tuple(sorted(str(symbol) for symbol in (row.symbols or []))),
            "source": str(row.source),
            "timeframe": str(row.timeframe),
            "consolidation_minutes": int(row.consolidation_minutes),
            "config_fingerprint": str(row.config_fingerprint),
            "panel_runtime_environment": (
                str(row.panel_runtime_environment)
                if row.panel_runtime_environment is not None
                else None
            ),
            "panel_data_use_scope": (
                str(row.panel_data_use_scope) if row.panel_data_use_scope is not None else None
            ),
            "panel_entitlement_owner_user_id": (
                str(row.panel_entitlement_owner_user_id)
                if row.panel_entitlement_owner_user_id is not None
                else None
            ),
            "panel_activation_cutoff": (
                _as_utc(row.panel_activation_cutoff)
                if row.panel_activation_cutoff is not None
                else None
            ),
            "state_schema_version": int(row.state_schema_version),
        }
        binding = self.identity.panel_binding
        expected = {
            "strategy_id": self.identity.strategy_id,
            "strategy_version": self.identity.strategy_version,
            "symbols": self.identity.symbols,
            "source": self.identity.source,
            "timeframe": self.identity.timeframe,
            "consolidation_minutes": self.identity.consolidation_minutes,
            "config_fingerprint": self.identity.config_fingerprint,
            "panel_runtime_environment": binding.environment if binding else None,
            "panel_data_use_scope": binding.data_use_scope if binding else None,
            "panel_entitlement_owner_user_id": (
                binding.entitlement_owner_user_id if binding else None
            ),
            "panel_activation_cutoff": binding.activation_cutoff if binding else None,
            "state_schema_version": strategy.model_state_schema_version,
        }
        if actual != expected:
            msg = (
                f"Incompatible durable runtime state for {self.identity.worker_id!r}: "
                f"expected={expected!r} actual={actual!r}"
            )
            raise ModelStateContractError(msg)

    def _validate_checkpoint(
        self,
        row: StrategyRuntimeState,
        checkpoint_ts_by_symbol: dict[str, datetime],
    ) -> None:
        symbol = str(row.last_source_symbol or "").strip().upper()
        if not symbol:
            msg = f"Durable runtime state for {self.identity.worker_id!r} has no source symbol"
            raise ModelStateContractError(msg)
        checkpoint = checkpoint_ts_by_symbol.get(symbol)
        if checkpoint is None or row.last_source_ts is None:
            msg = (
                f"Durable runtime state for {self.identity.worker_id!r} has no "
                f"matching checkpoint for {symbol!r}"
            )
            raise ModelStateContractError(msg)
        if _as_utc(row.last_source_ts) != _as_utc(checkpoint):
            msg = (
                f"Durable runtime state/checkpoint mismatch for {self.identity.worker_id!r} "
                f"{symbol!r}: state={row.last_source_ts!r} checkpoint={checkpoint!r}"
            )
            raise ModelStateContractError(msg)


class StrategyOperationalStatusReader:
    """Read bounded DB-derived readiness for a selected strategy worker.

    The supervisor and worker are separate processes. Reading the durable
    outbox and watermarks here makes `/ready` independent of Prometheus
    multiprocess configuration and proves economic progress, not merely that a
    child PID exists.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._outbox = OutboxStore(session_factory)
        self._prices = PriceIngestionService(session_factory)
        self._session_factory = session_factory
        self._clock = clock or now_utc

    def read(
        self,
        *,
        worker_id: str,
        strategy_id: str,
        symbols: Sequence[str],
        source: str,
        timeframe: str,
        asset_class: str,
        max_outbox_age_seconds: float,
        max_strategy_lag_seconds: float,
        ordering_key: str | None = None,
        panel_capable: bool = False,
        strategy_version: str | None = None,
        max_panel_age_seconds: float = _DEFAULT_MAX_PANEL_AGE_SECONDS,
    ) -> StrategyOperationalStatus:
        """Return one point-in-time operational snapshot."""
        if (
            max_outbox_age_seconds <= 0
            or max_strategy_lag_seconds <= 0
            or max_panel_age_seconds <= 0
        ):
            msg = "Operational readiness SLOs must be positive"
            raise ValueError(msg)

        outbox_ordering_key = ordering_key or worker_id
        counts = self._outbox.backlog_counts(
            topics=SIGNAL_OUTBOX_TOPICS,
            ordering_keys=(outbox_ordering_key,),
        )
        oldest = self._outbox.list_pending(
            topics=SIGNAL_OUTBOX_TOPICS,
            ordering_keys=(outbox_ordering_key,),
            limit=1,
            include_dead_letter=True,
        )
        outbox_snapshot = OutboxBacklogSnapshot(
            counts=counts,
            oldest_age_seconds=backlog_age_seconds(
                oldest[0].created_at if oldest else None,
                now=_as_utc(self._clock()),
            ),
        )
        oldest_age = outbox_snapshot.oldest_age_seconds

        if panel_capable:
            if strategy_version is None or not strategy_version.strip():
                msg = "Panel operational readiness requires strategy_version"
                raise ValueError(msg)
            return self._read_panel_status(
                worker_id=worker_id,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                outbox_snapshot=outbox_snapshot,
                counts=counts,
                oldest_age=oldest_age,
                max_outbox_age_seconds=max_outbox_age_seconds,
                max_strategy_lag_seconds=max_strategy_lag_seconds,
                max_panel_age_seconds=max_panel_age_seconds,
            )

        normalized_symbols = tuple(
            sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        )
        if not normalized_symbols:
            msg = f"Operational target {worker_id!r} has no symbols"
            raise ValueError(msg)

        watermark = Watermark(self._session_factory, worker_id, source=source)
        feed_lags: list[StrategyFeedLag] = []
        for symbol in normalized_symbols:
            instr_id = self._prices.resolve_instrument(
                symbol,
                asset_class=asset_class,
            )
            if instr_id is None:
                feed_lags.append(
                    StrategyFeedLag(
                        symbol=symbol,
                        latest_price_ts=None,
                        watermark_ts=None,
                        lag_seconds=None,
                        ready=False,
                        reason="instrument_missing",
                    )
                )
                continue

            state = watermark.get_state(symbol, timeframe)
            latest = self._prices.latest_candle_ts(
                instr_id,
                source=source,
                timeframe=timeframe,
            )
            if latest is None:
                feed_lags.append(
                    StrategyFeedLag(
                        symbol=symbol,
                        latest_price_ts=None,
                        watermark_ts=(None if state.instr_id is None else _as_utc(state.last_ts)),
                        lag_seconds=None,
                        ready=False,
                        reason="market_data_missing",
                    )
                )
                continue
            if state.instr_id is None:
                feed_lags.append(
                    StrategyFeedLag(
                        symbol=symbol,
                        latest_price_ts=_as_utc(latest),
                        watermark_ts=None,
                        lag_seconds=None,
                        ready=False,
                        reason="watermark_missing",
                    )
                )
                continue

            watermark.validate_state(state, instr_id=instr_id)
            latest_utc = _as_utc(latest)
            watermark_utc = _as_utc(state.last_ts)
            if state.rebuild_pending:
                feed_lags.append(
                    StrategyFeedLag(
                        symbol=symbol,
                        latest_price_ts=latest_utc,
                        watermark_ts=watermark_utc,
                        lag_seconds=max(
                            0.0,
                            (latest_utc - watermark_utc).total_seconds(),
                        ),
                        ready=False,
                        reason="historical_rebuild_pending",
                    )
                )
                continue
            lag_seconds = (latest_utc - watermark_utc).total_seconds()
            if lag_seconds < 0:
                reason = "watermark_ahead_of_market_data"
                feed_ready = False
            elif lag_seconds > max_strategy_lag_seconds:
                reason = "strategy_lag_exceeded"
                feed_ready = False
            else:
                reason = "within_slo"
                feed_ready = True
            feed_lags.append(
                StrategyFeedLag(
                    symbol=symbol,
                    latest_price_ts=latest_utc,
                    watermark_ts=watermark_utc,
                    lag_seconds=max(0.0, lag_seconds),
                    ready=feed_ready,
                    reason=reason,
                )
            )

        ready = outbox_snapshot.is_current(max_outbox_age_seconds) and all(
            feed.ready for feed in feed_lags
        )
        status = StrategyOperationalStatus(
            worker_id=worker_id,
            strategy_id=strategy_id,
            ready=ready,
            outbox_counts=counts,
            outbox_oldest_age_seconds=oldest_age,
            feed_lags=tuple(feed_lags),
        )
        _record_operational_metrics(status)
        return status

    def _read_panel_status(
        self,
        *,
        worker_id: str,
        strategy_id: str,
        strategy_version: str,
        outbox_snapshot: OutboxBacklogSnapshot,
        counts: dict[str, int],
        oldest_age: float,
        max_outbox_age_seconds: float,
        max_strategy_lag_seconds: float,
        max_panel_age_seconds: float,
    ) -> StrategyOperationalStatus:
        now = _as_utc(self._clock())
        with self._session_factory() as session:
            runtime = session.get(StrategyRuntimeState, worker_id)
            if (
                runtime is None
                or runtime.panel_data_use_scope is None
                or runtime.panel_entitlement_owner_user_id is None
                or runtime.panel_activation_cutoff is None
            ):
                latest_input = None
            else:
                latest_input = session.scalar(
                    select(StrategyPanelInputRevision)
                    .where(
                        StrategyPanelInputRevision.strategy_id == strategy_id,
                        StrategyPanelInputRevision.strategy_version == strategy_version,
                        StrategyPanelInputRevision.data_use_scope == runtime.panel_data_use_scope,
                        StrategyPanelInputRevision.entitlement_owner_user_id
                        == runtime.panel_entitlement_owner_user_id,
                        StrategyPanelInputRevision.cutoff_at >= runtime.panel_activation_cutoff,
                        StrategyPanelInputRevision.cutoff_at <= _as_db_timestamp(now),
                    )
                    .order_by(
                        StrategyPanelInputRevision.cutoff_at.desc(),
                        StrategyPanelInputRevision.created_at.desc(),
                        StrategyPanelInputRevision.input_sha256.desc(),
                    )
                    .limit(1)
                )
            latest_decision = session.scalar(
                select(StrategyPanelDecision)
                .where(StrategyPanelDecision.worker_id == worker_id)
                .order_by(
                    StrategyPanelDecision.cutoff_at.desc(),
                    StrategyPanelDecision.created_at.desc(),
                )
                .limit(1)
            )
            latest_completed = session.scalar(
                select(StrategyPanelDecision)
                .where(
                    StrategyPanelDecision.worker_id == worker_id,
                    StrategyPanelDecision.status == "completed",
                )
                .order_by(StrategyPanelDecision.cutoff_at.desc())
                .limit(1)
            )
        input_cutoff = _as_utc(latest_input.cutoff_at) if latest_input else None
        completed_cutoff = _as_utc(latest_completed.cutoff_at) if latest_completed else None
        age_seconds = (
            max(0.0, (now - input_cutoff).total_seconds()) if input_cutoff is not None else None
        )
        reason = "within_panel_cadence"
        panel_ready = True
        if latest_input is None:
            reason, panel_ready = "panel_input_missing", False
        elif latest_decision is None:
            reason, panel_ready = "panel_input_unprocessed", False
        elif str(latest_decision.strategy_input_sha256) != str(latest_input.input_sha256):
            reason, panel_ready = "latest_panel_input_unprocessed", False
        elif str(latest_decision.status) == "prepared":
            prepared_age = max(
                0.0,
                (now - _as_utc(latest_decision.created_at)).total_seconds(),
            )
            reason = (
                "panel_prepared_stale"
                if prepared_age > max_strategy_lag_seconds
                else "panel_prepared"
            )
            panel_ready = False
        elif str(latest_decision.status) == "rejected_correction":
            reason, panel_ready = "panel_input_rejected_correction", False
        elif str(latest_decision.status) in {"skipped", "expired"}:
            reason, panel_ready = f"panel_input_{latest_decision.status}", False
        elif str(latest_decision.status) != "completed":
            reason, panel_ready = "panel_terminal_failure", False
        elif age_seconds is None or age_seconds > max_panel_age_seconds:
            reason, panel_ready = "panel_input_stale", False
        progress = StrategyPanelProgress(
            ready=panel_ready,
            reason=reason,
            latest_input_sha256=(str(latest_input.input_sha256) if latest_input else None),
            latest_input_cutoff=input_cutoff,
            latest_decision_key=(str(latest_decision.decision_key) if latest_decision else None),
            latest_decision_status=(str(latest_decision.status) if latest_decision else None),
            latest_completed_cutoff=completed_cutoff,
            age_seconds=age_seconds,
        )
        ready = panel_ready and outbox_snapshot.is_current(max_outbox_age_seconds)
        status = StrategyOperationalStatus(
            worker_id=worker_id,
            strategy_id=strategy_id,
            ready=ready,
            outbox_counts=counts,
            outbox_oldest_age_seconds=oldest_age,
            feed_lags=(),
            panel_progress=progress,
        )
        _record_operational_metrics(status)
        return status


class DurableSignalRelay:
    """Deliver one worker partition from the existing transactional outbox."""

    def __init__(
        self,
        *,
        outbox: OutboxStore,
        emitter: SignalEmitter,
        worker_id: str,
        strategy_id: str,
        ordering_key: str | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
    ) -> None:
        self._outbox = outbox
        self._emitter = emitter
        self._worker_id = worker_id
        self._ordering_key = ordering_key or worker_id
        self._strategy_id = strategy_id
        self._clock = clock or now_utc
        self._lease_seconds = max(1, lease_seconds)
        self._retry_base_seconds = max(1, retry_base_seconds)
        self._retry_max_seconds = max(self._retry_base_seconds, retry_max_seconds)
        self._consumer = f"indicator-signal-relay:{worker_id}"

    def run_once(self, *, limit: int = 50) -> tuple[int, int]:
        """Claim, deliver, and fence one bounded batch outside business locks."""
        records = self._outbox.claim_batch(
            topics=SIGNAL_OUTBOX_TOPICS,
            consumer=self._consumer,
            limit=limit,
            lease_seconds=self._lease_seconds,
            ordering_keys=(self._ordering_key,),
            preserve_ordering=True,
        )
        delivered = 0
        failed = 0
        for record in records:
            try:
                if record.topic == MODEL_REBALANCE_SUBMISSION_TOPIC:
                    self._emitter.emit_model_rebalance(_model_rebalance_from_record(record))
                else:
                    self._emitter.emit(_signal_from_record(record))
            except (
                httpx.HTTPError,
                ImportError,
                KeyError,
                NotImplementedError,
                TypeError,
                ValueError,
            ) as exc:
                retry_delay = min(
                    self._retry_max_seconds,
                    self._retry_base_seconds * (2 ** max(0, record.attempts - 1)),
                )
                fenced = self._outbox.mark_failed(
                    record.event_id,
                    expected_claim_owner=self._consumer,
                    expected_attempts=record.attempts,
                    error_message=str(exc),
                    retry_delay_seconds=retry_delay,
                )
                logger.warning(
                    "Durable strategy signal delivery failed",
                    event_id=record.event_id,
                    worker_id=self._worker_id,
                    external_signal_id=record.headers.get("external_signal_id"),
                    attempts=record.attempts,
                    claim_fenced=fenced,
                    error=str(exc),
                )
                failed += 1
                continue

            fenced = self._outbox.mark_published(
                record.event_id,
                expected_claim_owner=self._consumer,
                expected_attempts=record.attempts,
                delivery_metadata={"transport": "http", "worker_id": self._worker_id},
            )
            if not fenced:
                logger.warning(
                    "Strategy signal delivered after relay claim was lost",
                    event_id=record.event_id,
                    worker_id=self._worker_id,
                    attempts=record.attempts,
                )
                failed += 1
                continue
            delivered += 1
        self.readiness()
        return delivered, failed

    def readiness(self, *, max_oldest_age_seconds: float = 300.0) -> SignalRelayReadiness:
        """Return and publish backlog readiness for this worker partition."""
        counts = self._outbox.backlog_counts(
            topics=SIGNAL_OUTBOX_TOPICS,
            ordering_keys=(self._ordering_key,),
        )
        pending = self._outbox.list_pending(
            topics=SIGNAL_OUTBOX_TOPICS,
            ordering_keys=(self._ordering_key,),
            limit=1,
            include_dead_letter=True,
        )
        snapshot = OutboxBacklogSnapshot(
            counts=counts,
            oldest_age_seconds=backlog_age_seconds(
                pending[0].created_at if pending else None,
                now=self._clock(),
            ),
        )
        oldest_age = snapshot.oldest_age_seconds
        ready = snapshot.is_current(max_oldest_age_seconds)
        _record_backlog_metrics(
            strategy_id=self._strategy_id,
            worker_id=self._worker_id,
            counts=counts,
            oldest_age_seconds=oldest_age,
        )
        return SignalRelayReadiness(
            ready=ready,
            counts=counts,
            oldest_age_seconds=oldest_age,
        )


def _panel_decision_key(
    *,
    identity: StrategyRuntimeIdentity,
    cutoff: datetime,
    strategy_input_sha256: str,
) -> str:
    binding = identity.require_panel_binding()
    return canonical_json_hash(
        {
            "schema": "strategy-panel-decision-v2",
            "worker_id": identity.worker_id,
            "strategy_id": identity.strategy_id,
            "strategy_version": identity.strategy_version,
            "config_fingerprint": identity.config_fingerprint,
            "runtime_environment": binding.environment,
            "data_use_scope": binding.data_use_scope,
            "entitlement_owner_user_id": binding.entitlement_owner_user_id,
            "activation_cutoff": binding.activation_cutoff.isoformat(),
            "cutoff": _as_utc(cutoff).isoformat(),
            "strategy_input_sha256": strategy_input_sha256,
        }
    )


def _canonical_json_object(
    value: object,
    *,
    field_name: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        message = f"{field_name} must be an object with string keys"
        raise ModelStateContractError(message)
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        message = f"{field_name} is not canonical JSON"
        raise ModelStateContractError(message) from exc
    if not isinstance(decoded, dict):
        message = f"{field_name} must decode to an object"
        raise ModelStateContractError(message)
    return cast(dict[str, Any], decoded)


def _decision_key(
    *,
    identity: StrategyRuntimeIdentity,
    decision: StrategyBarDecision,
    source_price_id: int,
    source_content_revision: int,
) -> str:
    material = "|".join(
        (
            identity.worker_id,
            identity.strategy_id,
            identity.strategy_version,
            decision.symbol,
            _as_utc(decision.strategy_bar_ts).isoformat(),
            str(source_price_id),
            str(source_content_revision),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _signal_from_record(record: Any) -> Signal:
    raw_signal = record.payload["signal"]
    if not isinstance(raw_signal, dict):
        msg = f"Outbox event {record.event_id} signal payload is not an object"
        raise TypeError(msg)
    signal = Signal.from_dict(raw_signal)
    external_signal_id = str(signal.external_signal_id or "").strip()
    if record.event_key != f"strategy-signal:{external_signal_id}":
        msg = f"Outbox event {record.event_id} has inconsistent signal identity"
        raise ValueError(msg)
    return signal


def _model_rebalance_from_record(record: Any) -> ModelRebalanceSubmissionEvent:
    if record.event_type != "ModelRebalanceSubmission":
        msg = f"Outbox event {record.event_id} has an invalid rebalance event type"
        raise ValueError(msg)
    event = ModelRebalanceSubmissionEvent.model_validate(record.payload)
    if record.event_key != f"model-rebalance:{event.rebalance_id}":
        msg = f"Outbox event {record.event_id} has inconsistent rebalance identity"
        raise ValueError(msg)
    return event


def _source_identity(bar: Bar) -> tuple[int, int]:
    price_id = int(bar.metadata.get("price_id") or 0)
    content_revision = int(bar.metadata.get("content_revision") or 0)
    if price_id < 1 or content_revision < 1:
        msg = f"Source bar {bar.symbol}/{bar.timestamp} has no durable identity"
        raise ModelStateContractError(msg)
    return price_id, content_revision


def _as_utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _as_db_timestamp(timestamp: datetime) -> datetime:
    return _as_utc(timestamp).replace(tzinfo=None)


def _iso_or_none(timestamp: datetime | None) -> str | None:
    return _as_utc(timestamp).isoformat() if timestamp is not None else None


def _record_backlog_metrics(
    *,
    strategy_id: str,
    worker_id: str,
    counts: dict[str, int],
    oldest_age_seconds: float,
) -> None:
    try:
        backlog = gauge(
            _BACKLOG_GAUGE,
            "Undelivered durable strategy signal envelopes by state",
            ("strategy_id", "worker_id", "status"),
        )
        if backlog is not None:
            for status in ("pending", "failed", "in_progress", "dead_letter"):
                backlog.labels(
                    strategy_id=strategy_id,
                    worker_id=worker_id,
                    status=status,
                ).set(counts.get(status, 0))
        age = gauge(
            _BACKLOG_AGE_GAUGE,
            "Age in seconds of the oldest undelivered durable strategy signal",
            ("strategy_id", "worker_id"),
        )
        if age is not None:
            age.labels(
                strategy_id=strategy_id,
                worker_id=worker_id,
            ).set(oldest_age_seconds)
    except (OSError, ValueError):
        logger.warning("Failed to record strategy signal backlog metrics", exc_info=True)


def _record_operational_metrics(status: StrategyOperationalStatus) -> None:
    """Publish the supervisor-visible readiness snapshot."""
    if status.error is None:
        _record_backlog_metrics(
            strategy_id=status.strategy_id,
            worker_id=status.worker_id,
            counts=status.outbox_counts,
            oldest_age_seconds=status.outbox_oldest_age_seconds,
        )
    try:
        ready = gauge(
            _OPERATIONAL_READY_GAUGE,
            "Whether one selected indicator worker is within every operational SLO.",
            ("strategy_id", "worker_id"),
        )
        if ready is not None:
            ready.labels(
                strategy_id=status.strategy_id,
                worker_id=status.worker_id,
            ).set(1 if status.ready else 0)
        lag = gauge(
            _STRATEGY_LAG_GAUGE,
            "Latest persisted source-price timestamp minus indicator watermark seconds.",
            ("strategy_id", "worker_id", "symbol"),
        )
        if lag is not None:
            for feed in status.feed_lags:
                lag.labels(
                    strategy_id=status.strategy_id,
                    worker_id=status.worker_id,
                    symbol=feed.symbol,
                ).set(feed.lag_seconds if feed.lag_seconds is not None else -1)
    except (OSError, ValueError):
        logger.warning("Failed to record strategy operational metrics", exc_info=True)


__all__ = [
    "MODEL_REBALANCE_SUBMISSION_TOPIC",
    "SIGNAL_OUTBOX_TOPICS",
    "SIGNAL_SUBMISSION_TOPIC",
    "BufferedSignalEmitter",
    "DurableSignalRelay",
    "PanelCorrectionError",
    "PanelPreparation",
    "SignalRelayReadiness",
    "StrategyBarDecision",
    "StrategyFeedLag",
    "StrategyOperationalStatus",
    "StrategyOperationalStatusReader",
    "StrategyPanelProgress",
    "StrategyRuntimeIdentity",
    "StrategyRuntimeStore",
]
