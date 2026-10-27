"""Owner-authored strategy drafts and append-only source revisions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONType, generate_uuid


class ResearchDraft(Base):
    __tablename__ = "research_drafts"

    draft_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    owner_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_revision_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        UniqueConstraint("owner_id", "draft_id", name="uq_research_draft_owner"),
        Index("ix_research_draft_owner_created", "owner_id", "created_at", "draft_id"),
        UniqueConstraint("owner_id", "request_id", name="uq_research_draft_request"),
        ForeignKeyConstraint(
            ["owner_id", "latest_revision_id"],
            ["research_draft_revisions.owner_id", "research_draft_revisions.revision_id"],
            name="fk_research_draft_latest",
            use_alter=True,
            ondelete="RESTRICT",
        ),
    )


class ResearchDraftRevision(Base):
    __tablename__ = "research_draft_revisions"

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    owner_id: Mapped[str] = mapped_column(String(50), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        UniqueConstraint("owner_id", "revision_id", name="uq_research_revision_owner"),
        UniqueConstraint("owner_id", "draft_id", "revision", name="uq_research_draft_revision"),
        ForeignKeyConstraint(
            ["owner_id", "draft_id"],
            ["research_drafts.owner_id", "research_drafts.draft_id"],
            ondelete="RESTRICT",
        ),
    )


def _immutable(_mapper: Any, _connection: Any, _target: Any) -> None:
    msg = "research revision is immutable"
    raise ValueError(msg)


def _protect_draft(_mapper: Any, _connection: Any, target: ResearchDraft) -> None:
    if any(
        inspect(target).attrs[name].history.has_changes()
        for name in ("draft_id", "owner_id", "request_id", "request_hash", "created_at")
    ):
        msg = "research draft identity is immutable"
        raise ValueError(msg)


event.listen(ResearchDraftRevision, "before_update", _immutable)
event.listen(ResearchDraftRevision, "before_delete", _immutable)
event.listen(ResearchDraft, "before_update", _protect_draft)
event.listen(ResearchDraft, "before_delete", _immutable)
