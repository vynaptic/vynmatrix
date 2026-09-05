"""Storage protocol for scoring engine persistence implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from lib_application.outbox import OutboxRecord, OutboxRedriveResult
from lib_common.internal_events import ModelRebalanceSubmissionEvent

from .models import (
    AccountRebalancePlanDraft,
    InstrumentHierarchy,
    ModePerformance,
    ScoreRecord,
    ScoringUserBinding,
    SignalRecord,
)

# How many newest-N rows the generic latest-per-strategy default scans (per
# requested row) before de-duplicating by strategy. A wider scan reduces the
# chance a chatty strategy hides slower strategies in the in-Python fallback;
# AppScoreStore avoids the scan entirely with a windowed DB query.
_LATEST_PER_STRATEGY_SCAN = 8


class ScoreStore(ABC):
    """Abstract storage for signals, scores, bindings, and mode performance."""

    @abstractmethod
    def add_signal(self, signal: SignalRecord) -> int | None:
        """Store a signal and return its canonical database identifier, if any."""
        raise NotImplementedError

    @abstractmethod
    def list_signals(self, symbol: str, limit: int = 200) -> list[SignalRecord]:
        """List recent signals for a symbol."""
        raise NotImplementedError

    @abstractmethod
    def list_all_signals(self, limit: int = 1000) -> list[SignalRecord]:
        """List recent signals across all symbols for market-level aggregation."""
        raise NotImplementedError

    @abstractmethod
    def upsert_score(self, score: ScoreRecord) -> None:
        """Insert or update a score."""
        raise NotImplementedError

    @abstractmethod
    def get_latest_score(self, target: str, scope: str = "asset") -> ScoreRecord | None:
        """Fetch latest score."""
        raise NotImplementedError

    @abstractmethod
    def list_bindings(self) -> list[ScoringUserBinding]:
        """List bindings."""
        raise NotImplementedError

    @abstractmethod
    def list_inactive_strategy_binding_ids(
        self,
        user_id: str,
        strategy_id: str,
        broker_account_id: int | None,
    ) -> list[int]:
        """Return inactive bindings for one user, account, and strategy."""
        raise NotImplementedError

    @abstractmethod
    def upsert_mode_performance(self, perf: ModePerformance) -> None:
        """Insert mode performance metric."""
        raise NotImplementedError

    @abstractmethod
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
        """List mode performance by instrument, sector, or asset class scope."""
        raise NotImplementedError

    @abstractmethod
    def get_instrument(self, symbol: str) -> InstrumentHierarchy | None:
        """Fetch classification for a symbol if present."""
        raise NotImplementedError

    def resolve_instrument_id(self, _symbol: str) -> int | None:
        """Resolve symbol to instrument_id when the store supports DB lookup."""
        return None

    def resolve_instrument_asset_class(self, _symbol: str) -> str | None:
        """Resolve symbol to its catalogued asset class when the store supports DB lookup."""
        return None

    def recent_asset_alpha_history(self, _window: int = 100) -> dict[str, list[float]]:
        """Per-asset chronological ``alpha_raw`` history (oldest→newest) used to
        warm-start Layer-3 rolling standardization after a restart.

        Default: empty. DB-backed stores override this to read the persisted
        score history so the standardized asset score is reproducible across
        process restarts instead of resetting to the cold-start default.
        """
        return {}

    def list_latest_signal_per_strategy(self, symbol: str, limit: int = 64) -> list[SignalRecord]:
        """Latest signal per strategy for ``symbol`` (newest-first, one per strategy).

        Used by cross-strategy ensemble gating so one chatty strategy cannot crowd
        slower strategies off a flat newest-N page. This generic default reads a
        wider page via :meth:`list_signals` and de-duplicates by strategy in Python;
        ``AppScoreStore`` overrides it with a single windowed DB query that returns
        the latest row per strategy directly.
        """
        seen: set[str] = set()
        latest: list[SignalRecord] = []
        for record in self.list_signals(symbol, limit=max(limit, 1) * _LATEST_PER_STRATEGY_SCAN):
            sid = (record.strategy_id or "").strip().lower()
            if sid in seen:
                continue
            seen.add(sid)
            latest.append(record)
            if len(latest) >= limit:
                break
        return latest

    def get_session(self) -> Session | None:
        """Return a new DB session if the store is database-backed, else None."""
        return None

    @contextmanager
    def unit_of_work(self) -> Iterator[Session | None]:
        """Group writes into one transaction so they persist all-or-nothing.

        Default: a no-op for stores without a real transaction (e.g. the
        in-memory test double). ``AppScoreStore`` overrides this with a real
        database transaction so a signal, its scores, and its outbox events
        commit together (transactional outbox).
        """
        yield None

    def classify_model_rebalance(
        self,
        _event: ModelRebalanceSubmissionEvent,
    ) -> Literal["new", "replay"]:
        """Validate immutable batch lineage and classify exact replay identity."""

        raise NotImplementedError

    def resolve_model_rebalance_entitlement_owner(
        self,
        _event: ModelRebalanceSubmissionEvent,
    ) -> str | None:
        """Return the exact owner of personal evidence, or ``None`` when shared.

        Database-backed stores must derive this from the immutable rank policy.
        The default supports test doubles whose events represent shared evidence.
        """

        return None

    def persist_model_rebalance(self, _event: ModelRebalanceSubmissionEvent) -> None:
        """Persist a complete model header after all canonical signals exist."""

        raise NotImplementedError

    def list_account_rebalance_plan_ids(self, _model_rebalance_id: str) -> list[str]:
        """List exact tenant/account plan identities for a model batch."""

        raise NotImplementedError

    def persist_account_rebalance_plan(self, _draft: AccountRebalancePlanDraft) -> str:
        """Persist one frozen tenant/account plan and all target dispositions."""

        raise NotImplementedError

    def persist_decision_context(
        self,
        *,
        signal_id: str,
        symbol: str | None = None,
        strategy_id: str | None = None,
        run_id: str | None = None,
        correlation_id: str | None = None,
        action: str | None = None,
        score_value: float | None = None,
        feature_snapshot: dict[str, Any] | None = None,
        signal_metadata: dict[str, Any] | None = None,
        instr_id: int | None = None,
    ) -> int | None:
        """Persist an immutable decision-context snapshot for replay/audit.

        Default: a no-op for stores without a real schema (the in-memory test
        double), so provenance persistence is a drop-in. ``AppScoreStore``
        overrides this to write a ``decision_contexts`` row on the active unit
        of work (atomic with the signal + scores + execution command).
        """
        # Default no-op: the kwargs define the contract that AppScoreStore
        # fulfils; the in-memory double intentionally ignores them.
        _ = (
            signal_id,
            symbol,
            strategy_id,
            run_id,
            correlation_id,
            action,
            score_value,
            feature_snapshot,
            signal_metadata,
            instr_id,
        )
        return None

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
        """Enqueue an outbox event."""
        raise NotImplementedError

    def claim_outbox_batch(
        self,
        *,
        topics: Iterable[str],
        consumer: str,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> list[OutboxRecord]:
        """Claim a batch of pending outbox events."""
        raise NotImplementedError

    def outbox_backlog_counts(self, *, topics: Iterable[str] | None = None) -> dict[str, int]:
        """Count undelivered outbox events by status (for the backlog gauge).

        Defaults to empty so non-DB stores (in-memory test double) need no
        implementation; the DB-backed store overrides it.
        """
        _ = topics  # interface signature; the no-op default ignores the filter
        return {}

    def oldest_outbox_undelivered_created_at(
        self,
        *,
        topics: Iterable[str] | None = None,
    ) -> datetime | None:
        """Return the oldest undelivered event timestamp for progress checks."""
        _ = topics
        return None

    def mark_outbox_published(
        self,
        event_id: str,
        *,
        expected_claim_owner: str,
        expected_attempts: int,
        delivery_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Mark an outbox event as published."""
        raise NotImplementedError

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
        """Mark an outbox event as failed."""
        raise NotImplementedError

    def list_outbox_dead_letters(
        self,
        *,
        topics: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[OutboxRecord]:
        """List dead-lettered events for authenticated operator inspection."""
        raise NotImplementedError

    def redrive_outbox_dead_letter(
        self,
        event_id: str,
        *,
        actor: str,
        reason: str,
        expected_generation: int,
    ) -> OutboxRedriveResult:
        """Acquire a fenced redrive generation for one dead letter."""
        raise NotImplementedError
