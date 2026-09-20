"""Section N (deployment record) -- which build this installation is running.

Table: ``deployments``.

One row per ``vmdev deploy`` run, opened before anything changes and closed
with its outcome. It describes the installation rather than a user, so it
carries no tenant column and no row security; the backend role holds
column-level ``SELECT`` on the subset the owner UI displays (migration
``0108_deployment_record``). The deploy tool writes it under migration
authority, so a deployment performed by other means leaves it stale --
``vmdev doctor`` reports that rather than trusting the row.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, SQLiteBigInteger


class Deployment(Base):
    """An install or upgrade of this deployment, and how it ended."""

    __tablename__ = "deployments"

    deployment_id: Mapped[int] = mapped_column(
        SQLiteBigInteger, primary_key=True, autoincrement=True
    )

    # ``install`` builds the database from nothing; ``upgrade`` preserves history.
    mode: Mapped[str] = mapped_column(String(20), nullable=False)

    # Exactly what Compose was pointed at, and what that image was built from.
    image_tag: Mapped[str] = mapped_column(String(255), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    source_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    source_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False)
    alembic_head: Mapped[str] = mapped_column(String(64), nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ``failed`` while the run is in flight, so an interrupted deploy is visible.
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    failure_stage: Mapped[str | None] = mapped_column(String(50))

    # The pre-upgrade dump, or the recorded reason there is none.
    snapshot_path: Mapped[str | None] = mapped_column(Text)
    snapshot_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('succeeded', 'rolled_back', 'failed')",
            name="ck_deployment_outcome",
        ),
        CheckConstraint("mode IN ('install', 'upgrade')", name="ck_deployment_mode"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_deployment_finished_after_start",
        ),
        CheckConstraint(
            "snapshot_path IS NOT NULL OR snapshot_reason IS NOT NULL",
            name="ck_deployment_snapshot_accounted",
        ),
        Index("ix_deployments_started_at", "started_at"),
    )


__all__ = ["Deployment"]
