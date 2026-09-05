"""In-memory scoring store used only as a unit-test double."""

from __future__ import annotations

import collections
import copy
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from lib_application.outbox import OutboxRecord, OutboxRedriveResult
from lib_common.internal_events import ModelRebalanceSubmissionEvent
from lib_strategy.signals.normalization import normalize_scoring_action

from .models import (
    AccountRebalancePlanDraft,
    InstrumentHierarchy,
    ModePerformance,
    ScoreRecord,
    ScoringUserBinding,
    SignalRecord,
)
from .storage_base import ScoreStore


class InMemoryScoreStore(ScoreStore):
    """Simple in-memory store for unit tests."""

    def __init__(self) -> None:
        self._signals: dict[str, collections.deque[SignalRecord]] = {}
        self._scores: dict[str, ScoreRecord] = {}
        self._bindings: list[ScoringUserBinding] = []
        self._mode_perf: dict[str, list[ModePerformance]] = {}
        self._instruments: dict[str, InstrumentHierarchy] = {}
        self._instrument_ids: dict[str, int] = {}
        self._binding_id_seq: int = 0
        self._outbox: dict[str, OutboxRecord] = {}
        self._model_rebalances: dict[str, ModelRebalanceSubmissionEvent] = {}
        self._account_rebalance_plans: dict[str, AccountRebalancePlanDraft] = {}
        self._rebalance_entitlement_owners: dict[str, str] = {}

    @contextmanager
    def unit_of_work(self):
        """Provide rollback semantics equivalent to the database test store."""

        snapshot = copy.deepcopy(
            (
                self._signals,
                self._scores,
                self._outbox,
                self._model_rebalances,
                self._account_rebalance_plans,
            )
        )
        completed = False
        try:
            yield None
            completed = True
        finally:
            if not completed:
                (
                    self._signals,
                    self._scores,
                    self._outbox,
                    self._model_rebalances,
                    self._account_rebalance_plans,
                ) = snapshot

    def classify_model_rebalance(
        self,
        event: ModelRebalanceSubmissionEvent,
    ) -> Literal["new", "replay"]:
        """Classify an exact unit-test replay without weakening production lineage."""

        existing = self._model_rebalances.get(event.rebalance_id)
        if existing is None:
            external_ids = {leg.signal.external_signal_id for leg in event.legs}
            if any(
                signal.external_signal_id in external_ids
                for bucket in self._signals.values()
                for signal in bucket
            ):
                message = "New model rebalance collides with an existing signal"
                raise ValueError(message)
            return "new"
        if existing.model_dump(mode="json") != event.model_dump(mode="json"):
            message = "Model rebalance replayed with different content"
            raise ValueError(message)
        return "replay"

    def seed_rebalance_entitlement_owner(
        self,
        *,
        provider_authority_sha256: str,
        user_id: str,
    ) -> None:
        """Bind personal evidence to one user in this non-production test double."""

        if not provider_authority_sha256 or not user_id:
            message = "Test entitlement owner identity must be non-empty"
            raise ValueError(message)
        self._rebalance_entitlement_owners[provider_authority_sha256] = user_id

    def resolve_model_rebalance_entitlement_owner(
        self,
        event: ModelRebalanceSubmissionEvent,
    ) -> str | None:
        """Return the test-double owner for the submitted authority digest."""

        return self._rebalance_entitlement_owners.get(event.provider_authority_sha256)

    def persist_model_rebalance(self, event: ModelRebalanceSubmissionEvent) -> None:
        """Persist one immutable model batch in the in-memory unit-test double."""

        if event.rebalance_id in self._model_rebalances:
            message = "Model rebalance already exists"
            raise ValueError(message)
        persisted_ids = {
            signal.external_signal_id for bucket in self._signals.values() for signal in bucket
        }
        missing = [
            leg.signal.external_signal_id
            for leg in event.legs
            if leg.signal.external_signal_id not in persisted_ids
        ]
        if missing:
            message = f"Canonical model signals are missing: {missing!r}"
            raise ValueError(message)
        self._model_rebalances[event.rebalance_id] = event.model_copy(deep=True)

    def list_account_rebalance_plan_ids(self, model_rebalance_id: str) -> list[str]:
        """List exact in-memory plans in deterministic tenant/account order."""

        drafts = [
            draft
            for draft in self._account_rebalance_plans.values()
            if draft.command.model_rebalance_id == model_rebalance_id
        ]
        drafts.sort(
            key=lambda draft: (
                draft.command.user_id,
                draft.command.broker_route.broker_account_id,
                draft.command.strategy_id,
                draft.command.binding_id,
            )
        )
        return [draft.command.account_plan_id for draft in drafts]

    def persist_account_rebalance_plan(self, draft: AccountRebalancePlanDraft) -> str:
        """Persist one immutable in-memory account plan."""

        command = draft.command
        existing = self._account_rebalance_plans.get(command.account_plan_id)
        if existing is not None:
            if existing != draft:
                message = "Account rebalance plan identity conflicts"
                raise ValueError(message)
            return command.account_plan_id
        self._account_rebalance_plans[command.account_plan_id] = copy.deepcopy(draft)
        return command.account_plan_id

    def resolve_instrument_id(self, symbol: str) -> int | None:
        """Deterministic in-memory instrument resolver for tests.

        Auto-assigns a stable positive id per symbol so the dispatch path can
        resolve an instrument without the production DB. The real AppScoreStore
        resolves from the instruments table; the dispatcher no longer fabricates
        a hashed id when resolution fails.
        """
        if symbol not in self._instrument_ids:
            self._instrument_ids[symbol] = len(self._instrument_ids) + 1
        return self._instrument_ids[symbol]

    def add_signal(self, signal: SignalRecord) -> int | None:
        signal.action = normalize_scoring_action(signal.action)
        bucket = self._signals.setdefault(signal.symbol, collections.deque(maxlen=500))
        bucket.appendleft(signal)
        return None

    def list_signals(self, symbol: str, limit: int = 200) -> list[SignalRecord]:
        if symbol not in self._signals:
            return []
        return list(list(self._signals[symbol])[:limit])

    def upsert_score(self, score: ScoreRecord) -> None:
        key = f"{score.scope}:{score.target}"
        self._scores[key] = score

    def get_latest_score(self, target: str, scope: str = "asset") -> ScoreRecord | None:
        return self._scores.get(f"{scope}:{target}")

    def seed_binding(self, binding: ScoringUserBinding) -> None:
        """Seed a binding in the in-memory test store.

        Production binding writes belong exclusively to the authenticated
        backend configuration API. Keeping this helper specific to the
        non-persistent store avoids a second production write implementation.
        """
        # Replace an existing binding only for the same user, strategy scope,
        # and linked broker account.
        existing_id: int | None = None
        new_bindings: list[ScoringUserBinding] = []
        for item in self._bindings:
            if (
                item.user_id == binding.user_id
                and item.strategy_id == binding.strategy_id
                and item.broker_account_id == binding.broker_account_id
            ):
                existing_id = item.binding_id
            else:
                new_bindings.append(item)
        self._bindings = new_bindings

        if binding.binding_id is None:
            if existing_id is not None:
                binding.binding_id = existing_id
            else:
                self._binding_id_seq += 1
                binding.binding_id = self._binding_id_seq
        self._bindings.append(binding)

    def list_bindings(self) -> list[ScoringUserBinding]:
        return list(self._bindings)

    def list_inactive_strategy_binding_ids(
        self,
        user_id: str,
        strategy_id: str,
        broker_account_id: int | None,
    ) -> list[int]:
        """The in-memory test store does not retain inactive binding records."""
        _ = (user_id, strategy_id, broker_account_id)
        return []

    def upsert_mode_performance(self, perf: ModePerformance) -> None:
        entries = self._mode_perf.setdefault(perf.asset, [])
        updated: list[ModePerformance] = [
            item
            for item in entries
            if not (
                item.execution_mode == perf.execution_mode
                and item.horizon == perf.horizon
                and item.account_id == perf.account_id
                and item.strategy_id == perf.strategy_id
            )
        ]
        updated.append(perf)
        self._mode_perf[perf.asset] = updated

    def list_mode_performance(
        self,
        asset: str | None = None,
        horizon: str | None = None,
        instrument_id: int | None = None,
        sector_id: int | None = None,
        asset_class: str | None = None,
        account_id: int | None = None,
        strategy_id: str | None = None,
    ) -> list[ModePerformance]:
        """List mode performance with scope isolation matching AppScoreStore."""
        results: list[ModePerformance] = []

        resolved_instr_id = instrument_id
        if asset and not instrument_id and asset in self._mode_perf:
            entries = self._mode_perf.get(asset, [])
            if entries and entries[0].instrument_id is not None:
                resolved_instr_id = entries[0].instrument_id

        all_entries: list[ModePerformance] = []
        for entries in self._mode_perf.values():
            all_entries.extend(entries)

        if resolved_instr_id is not None:
            results = [item for item in all_entries if item.instrument_id == resolved_instr_id]
        elif asset:
            results = list(self._mode_perf.get(asset, []))
        elif sector_id is not None:
            results = [
                item
                for item in all_entries
                if item.sector_id == sector_id and item.instrument_id is None
            ]
        elif asset_class:
            results = [
                item
                for item in all_entries
                if (
                    item.asset_class == asset_class
                    and item.instrument_id is None
                    and item.sector_id is None
                )
            ]

        if account_id is not None:
            results = [item for item in results if item.account_id == account_id]
        if strategy_id is not None:
            results = [item for item in results if item.strategy_id == strategy_id]
        if horizon:
            results = [item for item in results if item.horizon == horizon]

        return results

    def get_instrument(self, symbol: str) -> InstrumentHierarchy | None:
        return self._instruments.get(symbol)

    def list_signals_by_sector(self, sector: str, limit: int = 500) -> list[SignalRecord]:
        all_signals = self._list_signals_across_symbols(limit=limit)
        return [signal for signal in all_signals if signal.sector == sector][:limit]

    def list_signals_by_industry(self, industry: str, limit: int = 500) -> list[SignalRecord]:
        all_signals = self._list_signals_across_symbols(limit=limit)
        return [signal for signal in all_signals if signal.industry == industry][:limit]

    def list_signals_by_index(self, index_name: str, limit: int = 500) -> list[SignalRecord]:
        all_signals = self._list_signals_across_symbols(limit=limit)
        return [signal for signal in all_signals if signal.index == index_name][:limit]

    def list_all_signals(self, limit: int = 1000) -> list[SignalRecord]:
        return self._list_signals_across_symbols(limit=limit)[:limit]

    def _list_signals_across_symbols(self, *, limit: int) -> list[SignalRecord]:
        all_signals: list[SignalRecord] = []
        for symbol in list(self._signals.keys()):
            all_signals.extend(self.list_signals(symbol, limit=limit))
        return all_signals

    def enqueue_event(
        self,
        *,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        schema_version: str = "v1",
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        event_key: str | None = None,
        ordering_key: str | None = None,
        headers: dict[str, Any] | None = None,
        max_attempts: int = 10,
        available_at: datetime | None = None,
    ) -> str:
        for record in self._outbox.values():
            if event_key and record.event_key == event_key:
                return str(record.event_id)

        now = datetime.now(tz=UTC)
        event_id = f"mem-{len(self._outbox) + 1}"
        self._outbox[event_id] = OutboxRecord(
            event_id=event_id,
            topic=topic,
            event_type=event_type,
            schema_version=schema_version,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_key=event_key,
            ordering_key=ordering_key,
            payload=dict(payload),
            headers=dict(headers or {}),
            delivery_metadata={},
            status="pending",
            attempts=0,
            max_attempts=max_attempts,
            available_at=available_at or now,
            claimed_at=None,
            claim_owner=None,
            published_at=None,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        return event_id

    def claim_outbox_batch(
        self,
        *,
        topics: Iterable[str],
        consumer: str,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> list[OutboxRecord]:
        now = datetime.now(tz=UTC)
        stale_cutoff = now - timedelta(seconds=max(1, lease_seconds))
        topic_set = set(topics)
        claimed: list[OutboxRecord] = []
        for event_id, record in list(self._outbox.items()):
            if record.topic not in topic_set:
                continue
            if record.status not in {"pending", "failed"}:
                continue
            if record.available_at > now:
                continue
            if record.claimed_at is not None and record.claimed_at >= stale_cutoff:
                continue
            updated = OutboxRecord(
                event_id=record.event_id,
                topic=record.topic,
                event_type=record.event_type,
                schema_version=record.schema_version,
                aggregate_type=record.aggregate_type,
                aggregate_id=record.aggregate_id,
                event_key=record.event_key,
                ordering_key=record.ordering_key,
                payload=record.payload,
                headers=record.headers,
                delivery_metadata=record.delivery_metadata,
                status="in_progress",
                attempts=record.attempts + 1,
                max_attempts=record.max_attempts,
                available_at=record.available_at,
                claimed_at=now,
                claim_owner=consumer,
                published_at=record.published_at,
                last_error=record.last_error,
                created_at=record.created_at,
                updated_at=now,
                failure_class=record.failure_class,
                redrive_generation=record.redrive_generation,
                redrive_audit=record.redrive_audit,
            )
            self._outbox[event_id] = updated
            claimed.append(updated)
            if len(claimed) >= limit:
                break
        return claimed

    def outbox_backlog_counts(
        self,
        *,
        topics: Iterable[str] | None = None,
    ) -> dict[str, int]:
        topic_set = set(topics or [])
        counts: dict[str, int] = {}
        for record in self._outbox.values():
            if record.status == "published":
                continue
            if topic_set and record.topic not in topic_set:
                continue
            counts[record.status] = counts.get(record.status, 0) + 1
        return counts

    def oldest_outbox_undelivered_created_at(
        self,
        *,
        topics: Iterable[str] | None = None,
    ) -> datetime | None:
        topic_set = set(topics or [])
        candidates = [
            record.created_at
            for record in self._outbox.values()
            if record.status != "published" and (not topic_set or record.topic in topic_set)
        ]
        return min(candidates) if candidates else None

    def mark_outbox_published(
        self,
        event_id: str,
        *,
        expected_claim_owner: str,
        expected_attempts: int,
        delivery_metadata: dict[str, Any] | None = None,
    ) -> bool:
        record = self._outbox.get(event_id)
        if (
            record is None
            or record.status != "in_progress"
            or record.claim_owner != expected_claim_owner
            or record.attempts != expected_attempts
        ):
            return False
        now = datetime.now(tz=UTC)
        self._outbox[event_id] = OutboxRecord(
            event_id=record.event_id,
            topic=record.topic,
            event_type=record.event_type,
            schema_version=record.schema_version,
            aggregate_type=record.aggregate_type,
            aggregate_id=record.aggregate_id,
            event_key=record.event_key,
            ordering_key=record.ordering_key,
            payload=record.payload,
            headers=record.headers,
            delivery_metadata=dict(delivery_metadata or {}),
            status="published",
            attempts=record.attempts,
            max_attempts=record.max_attempts,
            available_at=record.available_at,
            claimed_at=None,
            claim_owner=None,
            published_at=now,
            last_error=None,
            created_at=record.created_at,
            updated_at=now,
            failure_class=None,
            redrive_generation=record.redrive_generation,
            redrive_audit=record.redrive_audit,
        )
        return True

    def mark_outbox_failed(
        self,
        event_id: str,
        *,
        expected_claim_owner: str,
        expected_attempts: int,
        error_message: str,
        retry_delay_seconds: int = 30,
        failure_class: str = "transient",
        terminal: bool = False,
    ) -> bool:
        record = self._outbox.get(event_id)
        if (
            record is None
            or record.status != "in_progress"
            or record.claim_owner != expected_claim_owner
            or record.attempts != expected_attempts
        ):
            return False
        now = datetime.now(tz=UTC)
        normalized_failure_class = str(failure_class).strip().lower()
        if normalized_failure_class not in {"transient", "permanent"}:
            msg = "failure_class must be transient or permanent"
            raise ValueError(msg)
        status = (
            "dead_letter"
            if terminal
            or normalized_failure_class == "permanent"
            or record.attempts >= record.max_attempts
            else "failed"
        )
        self._outbox[event_id] = OutboxRecord(
            event_id=record.event_id,
            topic=record.topic,
            event_type=record.event_type,
            schema_version=record.schema_version,
            aggregate_type=record.aggregate_type,
            aggregate_id=record.aggregate_id,
            event_key=record.event_key,
            ordering_key=record.ordering_key,
            payload=record.payload,
            headers=record.headers,
            delivery_metadata=record.delivery_metadata,
            status=status,
            attempts=record.attempts,
            max_attempts=record.max_attempts,
            available_at=now + timedelta(seconds=max(1, retry_delay_seconds)),
            claimed_at=None,
            claim_owner=None,
            published_at=record.published_at,
            last_error=error_message,
            created_at=record.created_at,
            updated_at=now,
            failure_class=normalized_failure_class,
            redrive_generation=record.redrive_generation,
            redrive_audit=record.redrive_audit,
        )
        return True

    def list_outbox_dead_letters(
        self,
        *,
        topics: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[OutboxRecord]:
        topic_set = set(topics or [])
        return [
            record
            for record in self._outbox.values()
            if record.status == "dead_letter" and (not topic_set or record.topic in topic_set)
        ][:limit]

    def redrive_outbox_dead_letter(
        self,
        event_id: str,
        *,
        actor: str,
        reason: str,
        expected_generation: int,
    ) -> OutboxRedriveResult:
        if not str(actor or "").strip() or not str(reason or "").strip():
            msg = "actor and reason are required for redrive"
            raise ValueError(msg)
        if isinstance(expected_generation, bool) or expected_generation < 0:
            msg = "expected_generation must be a non-negative integer"
            raise ValueError(msg)
        record = self._outbox.get(event_id)
        if record is None:
            msg = f"Outbox event {event_id} does not exist"
            raise LookupError(msg)
        if record.topic not in {"execution.commands", "execution.rebalance.commands"}:
            msg = "Only execution command dead letters are eligible for redrive"
            raise ValueError(msg)
        acquired = (
            record.status == "dead_letter" and record.redrive_generation == expected_generation
        )
        outcome = (
            "queued"
            if acquired
            else ("rejected_state" if record.status != "dead_letter" else "rejected_generation")
        )
        next_generation = record.redrive_generation + 1 if acquired else record.redrive_generation
        now = datetime.now(tz=UTC)
        audit = [
            *record.redrive_audit,
            {
                "actor": actor,
                "reason": reason,
                "source_event_id": event_id,
                "requested_generation": expected_generation,
                "generation": next_generation,
                "outcome": outcome,
                "recorded_at": now.isoformat(),
            },
        ]
        self._outbox[event_id] = OutboxRecord(
            event_id=record.event_id,
            topic=record.topic,
            event_type=record.event_type,
            schema_version=record.schema_version,
            aggregate_type=record.aggregate_type,
            aggregate_id=record.aggregate_id,
            event_key=record.event_key,
            ordering_key=record.ordering_key,
            payload=record.payload,
            headers=record.headers,
            delivery_metadata=record.delivery_metadata,
            status="pending" if acquired else record.status,
            attempts=0 if acquired else record.attempts,
            max_attempts=record.max_attempts,
            available_at=now if acquired else record.available_at,
            claimed_at=None if acquired else record.claimed_at,
            claim_owner=None if acquired else record.claim_owner,
            published_at=None if acquired else record.published_at,
            last_error=None if acquired else record.last_error,
            created_at=record.created_at,
            updated_at=now,
            failure_class=None if acquired else record.failure_class,
            redrive_generation=next_generation,
            redrive_audit=audit,
        )
        return OutboxRedriveResult(
            acquired=acquired,
            event_id=event_id,
            generation=next_generation,
            outcome=outcome,
        )
