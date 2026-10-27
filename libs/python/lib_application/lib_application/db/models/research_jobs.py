"""Durable research jobs, fenced attempts, registered artifacts and maintenance."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    insert,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONType, generate_uuid

STATUSES = "'queued','running','completed','failed','cancelled','interrupted'"


class ResearchJob(Base):
    __tablename__ = "research_jobs"
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    owner_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    inputs_json: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    lineage_json: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    inputs_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_id: Mapped[str | None] = mapped_column(String(36))
    dataset_id: Mapped[str | None] = mapped_column(String(36))
    model_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    current_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        UniqueConstraint("owner_id", "job_id", name="uq_research_job_owner"),
        ForeignKeyConstraint(
            ["owner_id", "revision_id"],
            ["research_draft_revisions.owner_id", "research_draft_revisions.revision_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "dataset_id"],
            ["research_datasets.owner_id", "research_datasets.dataset_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "model_id"],
            ["research_model_versions.owner_id", "research_model_versions.model_id"],
            name="fk_research_job_model",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({STATUSES})", name="ck_research_job_status"),
        CheckConstraint(
            "kind IN ('validate_strategy','train','predict','backtest')",
            name="ck_research_job_kind",
        ),
        Index("ix_research_job_queue", "status", "created_at", "job_id"),
        Index("ix_research_job_owner_created", "owner_id", "created_at", "job_id"),
    )


class ResearchJobAttempt(Base):
    __tablename__ = "research_job_attempts"
    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    owner_id: Mapped[str] = mapped_column(String(50), nullable=False)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    worker_id: Mapped[str | None] = mapped_column(String(200))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    generation: Mapped[str | None] = mapped_column(String(36))
    image_hash: Mapped[str | None] = mapped_column(String(255))
    bundle_hash: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    __table_args__ = (
        UniqueConstraint("owner_id", "attempt_id", name="uq_research_attempt_owner"),
        UniqueConstraint("owner_id", "request_id", name="uq_research_attempt_request"),
        UniqueConstraint("job_id", "attempt", name="uq_research_attempt_number"),
        ForeignKeyConstraint(
            ["owner_id", "job_id"],
            ["research_jobs.owner_id", "research_jobs.job_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({STATUSES})", name="ck_research_attempt_status"),
    )


class ResearchModelVersion(Base):
    __tablename__ = "research_model_versions"
    model_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    owner_id: Mapped[str] = mapped_column(String(50), nullable=False)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(36), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    training_cutoff: Mapped[str] = mapped_column(String(50), nullable=False)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    artifact_json: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        UniqueConstraint("owner_id", "model_id", name="uq_research_model_owner"),
        Index("ix_research_model_owner_created", "owner_id", "created_at", "model_id"),
        UniqueConstraint("owner_id", "request_id", name="uq_research_model_request"),
        UniqueConstraint("owner_id", "attempt_id", name="uq_research_model_attempt"),
        ForeignKeyConstraint(
            ["owner_id", "job_id"],
            ["research_jobs.owner_id", "research_jobs.job_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "attempt_id"],
            ["research_job_attempts.owner_id", "research_job_attempts.attempt_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "dataset_id"],
            ["research_datasets.owner_id", "research_datasets.dataset_id"],
            ondelete="RESTRICT",
        ),
    )


class ResearchControl(Base):
    __tablename__ = "research_control"
    control_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    maintenance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    generation: Mapped[str] = mapped_column(String(36), nullable=False, default=generate_uuid)
    worker_id: Mapped[str | None] = mapped_column(String(200))
    image_hash: Mapped[str | None] = mapped_column(String(255))
    bundle_hash: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "control_id = 1 AND length(generation) = 36", name="ck_research_control_singleton"
        ),
    )


def _immutable(_mapper: Any, _connection: Any, _target: Any) -> None:
    msg = "research artifact is immutable"
    raise ValueError(msg)


def _protect_job(_mapper: Any, _connection: Any, target: ResearchJob) -> None:
    immutable = (
        "job_id",
        "owner_id",
        "kind",
        "inputs_json",
        "lineage_json",
        "inputs_hash",
        "revision_id",
        "dataset_id",
        "model_id",
        "created_at",
    )
    if any(inspect(target).attrs[name].history.has_changes() for name in immutable):
        msg = "research job inputs are immutable"
        raise ValueError(msg)


def _protect_attempt(_mapper: Any, _connection: Any, target: ResearchJobAttempt) -> None:
    state = inspect(target)
    immutable = ("attempt_id", "owner_id", "job_id", "attempt", "request_id", "request_hash")
    if any(state.attrs[name].history.has_changes() for name in immutable):
        msg = "research attempt identity is immutable"
        raise ValueError(msg)
    previous = state.attrs.status.history.deleted
    old_status = previous[0] if previous else target.status
    if old_status in {"completed", "failed", "cancelled", "interrupted"}:
        msg = "finished research attempt is immutable"
        raise ValueError(msg)


def _seed_control(_target: Any, connection: Any, **_kwargs: Any) -> None:
    connection.execute(
        insert(ResearchControl).values(control_id=1, maintenance=False, generation=generate_uuid())
    )


event.listen(ResearchControl.__table__, "after_create", _seed_control)
event.listen(ResearchJob, "before_update", _protect_job)
event.listen(ResearchJobAttempt, "before_update", _protect_attempt)
for _model in (ResearchModelVersion, ResearchJob, ResearchJobAttempt):
    event.listen(_model, "before_delete", _immutable)
event.listen(ResearchModelVersion, "before_update", _immutable)
