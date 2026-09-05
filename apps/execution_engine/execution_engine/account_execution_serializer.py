"""Account-wide execution authority shared by every broker-writing path."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from lib_application.db.models import AccountExecutionGeneration

from ._services import SessionFactory

AccountWriterKind = Literal[
    "ordinary",
    "manual",
    "rebalance",
    "historical_replay",
    "paper_lifecycle",
    "reconciliation",
]
_SHA256_HEX_LENGTH = 64
_WRITER_KINDS: frozenset[str] = frozenset(
    {
        "ordinary",
        "manual",
        "rebalance",
        "historical_replay",
        "paper_lifecycle",
        "reconciliation",
    }
)


class AccountExecutionBusyError(RuntimeError):
    """Another process currently owns the account broker-writing boundary."""


class AccountExecutionFenceLostError(RuntimeError):
    """The monotonic account generation changed while the caller held authority."""


@dataclass(frozen=True)
class AccountExecutionLease:
    """One account-wide writer generation and its audit identity."""

    user_id: str
    broker_account_id: int
    owner: str
    writer_kind: AccountWriterKind
    generation: int
    account_plan_id: str | None
    acquired_at: datetime


class AccountExecutionSerializer:
    """Serialize account writers across coroutines and execution-engine replicas.

    PostgreSQL's session advisory lock is the exclusion authority and is held on
    one dedicated connection across broker I/O. The database row provides a
    monotonic, auditable generation; it is deliberately not used as a time-based
    lease, so a slow broker call cannot permit an overlapping writer. Closing a
    failed process connection releases the advisory lock automatically.
    """

    def __init__(self, session_factory: SessionFactory | None) -> None:
        self._session_factory = session_factory
        self._local_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._memory_generations: dict[tuple[str, int], int] = {}
        self._current: ContextVar[AccountExecutionLease | None] = ContextVar(
            f"account_execution_lease_{id(self)}",
            default=None,
        )

    @asynccontextmanager
    async def hold(  # noqa: PLR0912, PLR0915
        self,
        *,
        user_id: str,
        broker_account_id: int,
        writer_kind: AccountWriterKind,
        account_plan_id: str | None = None,
    ) -> AsyncIterator[AccountExecutionLease]:
        """Acquire one non-overlapping writer generation for an account."""
        normalized_user = str(user_id or "").strip()
        if not normalized_user:
            message = "Account execution serialization requires user_id"
            raise ValueError(message)
        if isinstance(broker_account_id, bool) or int(broker_account_id) <= 0:
            message = "Account execution serialization requires a positive account ID"
            raise ValueError(message)
        account_id = int(broker_account_id)
        if writer_kind not in _WRITER_KINDS:
            message = f"Unsupported account writer kind: {writer_kind}"
            raise ValueError(message)
        normalized_plan = str(account_plan_id or "").strip() or None
        if normalized_plan is not None and len(normalized_plan) != _SHA256_HEX_LENGTH:
            message = "Account execution account_plan_id must be a SHA-256 digest"
            raise ValueError(message)

        inherited = self._current.get()
        if inherited is not None:
            if inherited.user_id != normalized_user or inherited.broker_account_id != account_id:
                message = "Nested execution attempted to change the fenced account partition"
                raise AccountExecutionFenceLostError(message)
            yield inherited
            return

        partition = (normalized_user, account_id)
        local_lock = self._local_locks.setdefault(partition, asyncio.Lock())
        if local_lock.locked():
            message = f"Account execution partition {normalized_user}/{account_id} is busy"
            raise AccountExecutionBusyError(message)
        await local_lock.acquire()

        connection: Connection | None = None
        advisory_lock_acquired = False
        lease: AccountExecutionLease | None = None
        token = None
        try:
            engine = self._postgres_engine()
            if engine is not None:
                connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
                acquired = bool(
                    connection.execute(
                        text(
                            "SELECT pg_try_advisory_lock("
                            "hashtextextended(CAST(:partition AS text), 0))"
                        ),
                        {"partition": self._partition_key(normalized_user, account_id)},
                    ).scalar_one()
                )
                if not acquired:
                    message = f"Account execution partition {normalized_user}/{account_id} is busy"
                    raise AccountExecutionBusyError(message)
                advisory_lock_acquired = True

            owner = f"{writer_kind}:{uuid4()}"
            acquired_at = datetime.now(tz=UTC)
            generation = self._claim_generation(
                user_id=normalized_user,
                broker_account_id=account_id,
                owner=owner,
                writer_kind=writer_kind,
                account_plan_id=normalized_plan,
                acquired_at=acquired_at,
                persist=engine is not None,
            )
            lease = AccountExecutionLease(
                user_id=normalized_user,
                broker_account_id=account_id,
                owner=owner,
                writer_kind=writer_kind,
                generation=generation,
                account_plan_id=normalized_plan,
                acquired_at=acquired_at,
            )
            token = self._current.set(lease)
            yield lease
        finally:
            if token is not None:
                self._current.reset(token)
            try:
                if lease is not None:
                    self._release_generation(lease, persist=connection is not None)
            finally:
                try:
                    if connection is not None and advisory_lock_acquired:
                        try:
                            released = bool(
                                connection.execute(
                                    text(
                                        "SELECT pg_advisory_unlock("
                                        "hashtextextended(CAST(:partition AS text), 0))"
                                    ),
                                    {
                                        "partition": self._partition_key(
                                            normalized_user,
                                            account_id,
                                        )
                                    },
                                ).scalar_one()
                            )
                            if not released:
                                message = (
                                    "PostgreSQL account advisory lock was not owned at release"
                                )
                                raise AccountExecutionFenceLostError(message)
                        finally:
                            connection.close()
                    elif connection is not None:
                        connection.close()
                finally:
                    local_lock.release()

    def assert_current(self, lease: AccountExecutionLease) -> None:
        """Fail if terminal reconciliation is outside its original authority."""
        if self._current.get() != lease:
            message = "Account execution generation is not current in this execution context"
            raise AccountExecutionFenceLostError(message)

    def current_lease(self) -> AccountExecutionLease:
        """Return the trusted internal lease for nested execution components."""
        lease = self._current.get()
        if lease is None:
            message = "Account execution requires a current serialized generation"
            raise AccountExecutionFenceLostError(message)
        return lease

    def _postgres_engine(self) -> Engine | None:
        if self._session_factory is None:
            return None
        with self._session_factory() as session:
            bind = session.get_bind()
            if bind.dialect.name != "postgresql":
                return None
            if isinstance(bind, Engine):
                return bind
            return cast(Engine, bind.engine)

    def _claim_generation(
        self,
        *,
        user_id: str,
        broker_account_id: int,
        owner: str,
        writer_kind: AccountWriterKind,
        account_plan_id: str | None,
        acquired_at: datetime,
        persist: bool,
    ) -> int:
        partition = (user_id, broker_account_id)
        if not persist or self._session_factory is None:
            generation = self._memory_generations.get(partition, 0) + 1
            self._memory_generations[partition] = generation
            return generation
        with self._session_factory() as session:
            row = session.get(
                AccountExecutionGeneration,
                {"user_id": user_id, "broker_account_id": broker_account_id},
                with_for_update=True,
            )
            if row is None:
                row = AccountExecutionGeneration(
                    user_id=user_id,
                    broker_account_id=broker_account_id,
                    generation=1,
                    active_owner=owner,
                    active_writer_kind=writer_kind,
                    active_account_plan_id=account_plan_id,
                    acquired_at=acquired_at,
                    released_at=None,
                    updated_at=acquired_at,
                )
                session.add(row)
            else:
                row.generation = int(row.generation) + 1
                row.active_owner = owner
                row.active_writer_kind = writer_kind
                row.active_account_plan_id = account_plan_id
                row.acquired_at = acquired_at
                row.released_at = None
                row.updated_at = acquired_at
            session.commit()
            return int(row.generation)

    def _release_generation(self, lease: AccountExecutionLease, *, persist: bool) -> None:
        if not persist or self._session_factory is None:
            return
        released_at = datetime.now(tz=UTC)
        with self._session_factory() as session:
            row = session.get(
                AccountExecutionGeneration,
                {
                    "user_id": lease.user_id,
                    "broker_account_id": lease.broker_account_id,
                },
                with_for_update=True,
            )
            if (
                row is None
                or int(row.generation) != lease.generation
                or str(row.active_owner or "") != lease.owner
            ):
                message = "Account execution generation changed before release"
                raise AccountExecutionFenceLostError(message)
            row.active_owner = None
            row.active_writer_kind = None
            row.active_account_plan_id = None
            row.acquired_at = None
            row.released_at = released_at
            row.updated_at = released_at
            session.commit()

    @staticmethod
    def _partition_key(user_id: str, broker_account_id: int) -> str:
        return f"execution-account:{user_id}:{broker_account_id}"


__all__ = [
    "AccountExecutionBusyError",
    "AccountExecutionFenceLostError",
    "AccountExecutionLease",
    "AccountExecutionSerializer",
    "AccountWriterKind",
]
