"""Section M (provenance) — immutable decision-context snapshots.

Table: ``decision_contexts``.

A point-in-time, append-only snapshot of the context behind every scoring
decision (market regime, volatility, liquidity, news, model/version, per-factor
contributions, stale/missing-input flags, and the abstain outcome). Persisted in
the SAME unit of work as the signal, its scores, and the execution command so a
trade can always be replayed and audited against exactly the inputs that
produced it — provenance that cannot be reconstructed after the fact.

Registers against the shared ``Base`` and carries no ORM relationships to the
scoring tables (it references ``instruments``/``strategies`` by id only), so the
split is purely file-level. Rows are never updated after insert.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from ._base import Base, JSONType, SQLiteBigInteger


class DecisionContext(Base):
    """Immutable point-in-time snapshot of one scoring decision's context."""

    __tablename__ = "decision_contexts"

    context_id: Mapped[int] = mapped_column(SQLiteBigInteger, primary_key=True, autoincrement=True)

    # Decision identity / tracing. ``signal_id`` is the domain Signal UUID (a
    # string), NOT the CanonicalSignal autoincrement PK — same convention as
    # ExecutionDecisionLog.signal_id; no FK on purpose.
    signal_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    instr_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("instruments.instr_id"))
    strategy_id: Mapped[str | None] = mapped_column(
        String(50), ForeignKey("strategies.strategy_id")
    )
    symbol: Mapped[str | None] = mapped_column(String(50))
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64))

    # Versioning of the decision machinery so a replay knows which logic ran.
    pipeline_version: Mapped[str | None] = mapped_column(String(20))
    model_version: Mapped[str | None] = mapped_column(String(50))

    # Score breakdown / per-factor contributions (Layer-3 ensemble math).
    alpha_core: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    alpha_raw: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    score_standardized: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    p_agg: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    score_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))

    # Market / decision context snapshot (the inputs to the decision).
    regime: Mapped[str | None] = mapped_column(String(30))
    vix_level: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    iv_rank: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    realized_vol: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    liquidity_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    news_sentiment: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))

    # Outcome.
    action: Mapped[str | None] = mapped_column(String(20))
    abstained: Mapped[bool | None] = mapped_column(Boolean)
    abstain_reason: Mapped[str | None] = mapped_column(String(100))

    # Full snapshots for replay. ``feature_snapshot`` is the score payload; the
    # others capture per-factor contributions and which inputs were defaulted /
    # missing at decision time so a stale-data decision is auditable.
    feature_snapshot: Mapped[Any | None] = mapped_column(JSONType)
    factor_contributions: Mapped[Any | None] = mapped_column(JSONType)
    stale_inputs: Mapped[Any | None] = mapped_column(JSONType)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=UTC),
        index=True,
    )

    __table_args__ = (Index("ix_decision_context_instr_created", "instr_id", "created_at"),)


__all__ = [
    "DecisionContext",
]
