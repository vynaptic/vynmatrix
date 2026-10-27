"""Durable research fencing and exact sandbox drain for database maintenance."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from dev_cli.core.deployment.environment import resolve_host_env
from lib_application.db.models.research_jobs import ResearchJob, ResearchJobAttempt
from lib_application.services import research_jobs
from lib_application.services.database_authority import require_maintenance_database_role
from lib_application.services.research_admission import RESEARCH_CONTROL_LOCK, RESEARCH_LAUNCH_LOCK

from .process import DockerBackend, exact_identity
from .sandbox import LABEL_GENERATION, LABEL_JOB, SandboxSpec

DRAIN_SECONDS = 5.0


class MaintenanceStore:
    """Hold launch/control session locks through dump, DDL, restore and re-fencing."""

    def __init__(self, environment: Mapping[str, str]) -> None:
        value = resolve_host_env(environment).get("MIGRATION_DATABASE_URL", "")
        try:
            url = make_url(value)
        except SQLAlchemyError as exc:
            msg = "Research maintenance requires an explicit migration database URL"
            raise ValueError(msg) from exc
        if (
            url.get_backend_name() != "postgresql"
            or not all((url.host, url.database, url.username, url.password))
            or (url.username or "").startswith("vm_")
            or url.query
        ):
            msg = "Research maintenance requires the explicit PostgreSQL maintenance owner"
            raise ValueError(msg)
        self.url = url.render_as_string(hide_password=False)
        self.engine: Any = None
        self.connection: Any = None
        self.locked: list[str] = []

    def _open(self) -> bool:
        if self.connection is not None:
            return True
        from lib_application.db.session import (  # noqa: PLC0415
            create_engine_for_env,
            dispose_engine,
        )

        self.engine = create_engine_for_env(env="dev", db_url=self.url)
        try:
            self.connection = self.engine.connect()
        except SQLAlchemyError as exc:
            dispose_engine(self.engine)
            self.engine = None
            original = getattr(exc, "orig", None)
            if (
                getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            ) == "3D000":
                return False
            msg = "Research maintenance target is unreachable; no maintenance action is authorized"
            raise ValueError(msg) from exc
        self.connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        # A verified claim can exist before Docker creates its container. Block
        # launch first, then fence admission; scans alone cannot close this gap.
        for key in (RESEARCH_LAUNCH_LOCK, RESEARCH_CONTROL_LOCK):
            self.connection.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:key,0))"), {"key": key}
            )
            self.locked.append(key)
        self.connection.commit()
        return True

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with Session(bind=self.connection) as session, session.begin():
            yield session

    @staticmethod
    def _exists(session: Session) -> bool:
        return (
            session.scalar(text("SELECT to_regclass('public.research_control') IS NOT NULL"))
            is True
        )

    def begin(self) -> dict[str, Any] | None:
        if not self._open():
            return None
        with self._session() as session:
            if not self._exists(session):
                return None
            require_maintenance_database_role(session)
            previous = research_jobs.supervisor_status(session)
            research_jobs.set_maintenance(
                session, enabled=True, expected_generation=previous["generation"]
            )
            return previous

    def lookup(self, job_id: str, generation: str, image: str) -> dict[str, Any] | None:
        with self._session() as session:
            row = (
                session.execute(
                    select(
                        ResearchJobAttempt.job_id,
                        ResearchJobAttempt.generation,
                        ResearchJobAttempt.image_hash,
                        ResearchJobAttempt.bundle_hash,
                    )
                    .where(
                        ResearchJobAttempt.job_id == job_id,
                        ResearchJobAttempt.generation == generation,
                        ResearchJobAttempt.image_hash == image,
                    )
                    .order_by(ResearchJobAttempt.attempt.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            return dict(row) if row else None

    def interrupt_running(self) -> None:
        with self._session() as session:
            if not self._exists(session):
                return
            research_jobs.reconcile_expired_attempts(session)
            rows = list(
                session.execute(
                    select(
                        ResearchJobAttempt.owner_id,
                        ResearchJobAttempt.job_id,
                        ResearchJobAttempt.attempt_id,
                        ResearchJobAttempt.claim_token,
                        ResearchJobAttempt.generation,
                    )
                    .join(
                        ResearchJob,
                        (ResearchJob.owner_id == ResearchJobAttempt.owner_id)
                        & (ResearchJob.job_id == ResearchJobAttempt.job_id)
                        & (ResearchJob.current_attempt == ResearchJobAttempt.attempt),
                    )
                    .where(ResearchJob.status == "running", ResearchJobAttempt.status == "running")
                ).mappings()
            )
            for row in rows:
                research_jobs.interrupt_job(
                    session,
                    row["owner_id"],
                    row["job_id"],
                    attempt_id=row["attempt_id"],
                    token=row["claim_token"],
                    generation=row["generation"],
                    detail="Research sandbox stopped for database maintenance",
                )

    def refence(self) -> None:
        if not self._open():
            return
        with self._session() as session:
            if self._exists(session):
                require_maintenance_database_role(session)
                state = research_jobs.supervisor_status(session)
                research_jobs.set_maintenance(
                    session, enabled=True, expected_generation=state["generation"]
                )
        self.interrupt_running()

    def complete(self) -> None:
        if not self._open():
            return
        with self._session() as session:
            if self._exists(session):
                require_maintenance_database_role(session)
                state = research_jobs.supervisor_status(session)
                research_jobs.set_maintenance(
                    session, enabled=False, expected_generation=state["generation"]
                )

    def close(self) -> None:
        from lib_application.db.session import dispose_engine  # noqa: PLC0415

        connection, self.connection = self.connection, None
        engine, self.engine = self.engine, None
        locked, self.locked = self.locked, []
        try:
            if connection is not None:
                try:
                    if connection.in_transaction():
                        connection.rollback()
                    for key in reversed(locked):
                        connection.execute(
                            text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"),
                            {"key": key},
                        )
                    connection.commit()
                finally:
                    connection.close()
        finally:
            if engine is not None:
                dispose_engine(engine)


class ResearchMaintenance:
    """Fence before effects, prove exact cleanup, then retain interrupted attempts."""

    def __init__(
        self,
        *,
        store: Any,
        docker: DockerBackend | None = None,
        drain_seconds: float = DRAIN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.docker = docker if docker is not None else DockerBackend()
        self.drain_seconds, self.clock, self.sleep = drain_seconds, clock, sleep
        self.started = False
        self.previous: dict[str, Any] | None = None
        self.stack_was_active = False

    def _research(self) -> list[dict[str, Any]]:
        rows = self.docker.running()
        self.stack_was_active |= any(
            row.get("Config", {}).get("Labels", {}).get("com.docker.compose.project") == "vynmatrix"
            and row.get("Config", {}).get("Labels", {}).get("com.docker.compose.service")
            == "application"
            for row in rows
        )
        return [row for row in rows if LABEL_JOB in (row.get("Config", {}).get("Labels") or {})]

    def _spec(self, row: dict[str, Any]) -> SandboxSpec:
        labels = row.get("Config", {}).get("Labels") or {}
        attempt = self.store.lookup(
            labels[LABEL_JOB], labels.get(LABEL_GENERATION), row.get("Image")
        )
        if attempt is None:
            msg = "Unrecognized labelled research sandbox blocks maintenance"
            raise ValueError(msg)
        return SandboxSpec(
            attempt["job_id"], attempt["generation"], attempt["image_hash"], attempt["bundle_hash"]
        )

    def begin(self) -> None:
        succeeded = False
        try:
            self.previous = self.store.begin()
            self.started = True
            if self.previous is not None:
                deadline = self.clock() + self.drain_seconds
                rows = self._research()
                for row in rows:
                    self._spec(row)
                while rows and self.clock() < deadline:
                    self.sleep(min(0.2, max(0, deadline - self.clock())))
                    rows = self._research()
                for row in rows:
                    self.docker.remove(exact_identity(row, self._spec(row)))
                if self._research():
                    msg = "Research sandbox drain could not be proved"
                    raise ValueError(msg)
                self.store.interrupt_running()
            succeeded = True
        except SQLAlchemyError as exc:
            msg = "Research maintenance database proof failed; research remains fenced"
            raise ValueError(msg) from exc
        finally:
            if not succeeded:
                self.abort()

    def after_restore(self) -> None:
        try:
            self.store.refence()
            if self.previous is not None and self._research():
                msg = "Research sandbox appeared during fenced maintenance"
                raise ValueError(msg)
        except SQLAlchemyError as exc:
            msg = "Restored research control could not be fenced; runtime must remain stopped"
            raise ValueError(msg) from exc

    def complete(self) -> None:
        try:
            self.store.refence()
            self.store.complete()
        except SQLAlchemyError as exc:
            msg = "Research maintenance could not safely resume; research remains fenced"
            raise ValueError(msg) from exc
        finally:
            self.abort()

    def abort(self) -> None:
        self.store.close()
        self.started = False
