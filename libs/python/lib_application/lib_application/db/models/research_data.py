"""Owner-scoped append-only research imports, datasets and normalized bars."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONType, generate_uuid


class ResearchImport(Base):
    """Exact original bytes, validation receipt and normalized valid content."""

    __tablename__ = "research_imports"
    import_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    owner_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False
    )
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    original_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    original_csv: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    canonical_bars: Mapped[list[dict[str, str]]] = mapped_column(JSONType, nullable=False)
    stored_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    __table_args__ = (
        UniqueConstraint("owner_id", "import_id", name="uq_research_import_owner"),
        UniqueConstraint("owner_id", "request_hash", name="uq_research_import_request"),
        CheckConstraint("stored_bytes >= 0", name="ck_research_import_bytes"),
        CheckConstraint(
            "(valid AND content_hash IS NOT NULL) OR (NOT valid AND content_hash IS NULL)",
            name="ck_research_import_valid_hash",
        ),
        Index("ix_research_import_owner_created", "owner_id", "created_at", "import_id"),
    )


class ResearchDataset(Base):
    """A named immutable freeze of one successful validation receipt."""

    __tablename__ = "research_datasets"
    dataset_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    owner_id: Mapped[str] = mapped_column(String(50), nullable=False)
    import_id: Mapped[str] = mapped_column(String(36), nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    original_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    freeze_xid: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    bar_count: Mapped[int] = mapped_column(Integer, nullable=False)
    stored_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    __table_args__ = (
        UniqueConstraint("owner_id", "dataset_id", name="uq_research_dataset_owner"),
        UniqueConstraint("owner_id", "import_id", name="uq_research_dataset_import"),
        ForeignKeyConstraint(
            ["owner_id", "import_id"],
            ["research_imports.owner_id", "research_imports.import_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "supersedes_id"],
            ["research_datasets.owner_id", "research_datasets.dataset_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "stored_bytes >= 0 AND bar_count BETWEEN 1 AND 100000",
            name="ck_research_dataset_bounds",
        ),
        Index("ix_research_dataset_owner_created", "owner_id", "created_at", "dataset_id"),
    )


class ResearchBar(Base):
    """Lossless ordered dataset content, separate from runtime prices."""

    __tablename__ = "research_bars"
    owner_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bar_json: Mapped[dict[str, str]] = mapped_column(JSONType, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "dataset_id"],
            ["research_datasets.owner_id", "research_datasets.dataset_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("owner_id", "dataset_id", "ts", name="uq_research_bar_timestamp"),
        CheckConstraint("ordinal BETWEEN 0 AND 99999", name="ck_research_bar_ordinal"),
    )


def _reject_mutation(_mapper: Any, _connection: Any, _target: Any) -> None:
    msg = "Research evidence is immutable; create another import or dataset"
    raise ValueError(msg)


for _model in (ResearchImport, ResearchDataset, ResearchBar):
    event.listen(_model, "before_update", _reject_mutation)
    event.listen(_model, "before_delete", _reject_mutation)


__all__ = ["ResearchBar", "ResearchDataset", "ResearchImport"]
