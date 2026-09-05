"""Make historical price corrections rebuild indicator state deterministically.

The prices table is an upserted observation store. Before this revision, a
provider correction or a backfilled gap at/before an indicator watermark
silently changed persisted history while the live strategy retained state
computed from the old bars.

This revision adds:

* a per-price content revision, incremented only for a real OHLCV change;
* source/instrument identity on every consumer watermark;
* a durable earliest rebuild boundary plus generation fence; and
* the narrow market-data role grant required to mark affected consumers in the
  same transaction as a price mutation.

Revision ID: 0062_historical_price_rebuild
Revises: 0061_account_scoped_daily_nav
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0062_historical_price_rebuild"
down_revision = "0061_account_scoped_daily_nav"
branch_labels = None
depends_on = None


def _normalized_symbol(value: object) -> str:
    return str(value or "").replace("/", "").replace("-", "").replace("_", "").strip().upper()


def _instrument_candidates(bind: Any) -> dict[str, set[int]]:
    candidates: dict[str, set[int]] = defaultdict(set)
    for instr_id, symbol in bind.execute(
        sa.text("SELECT instr_id, canonical FROM instruments")
    ).all():
        candidates[_normalized_symbol(symbol)].add(int(instr_id))
    for instr_id, symbol in bind.execute(
        sa.text("SELECT instr_id, alias FROM instrument_aliases")
    ).all():
        candidates[_normalized_symbol(symbol)].add(int(instr_id))
    for instr_id, symbol in bind.execute(
        sa.text("SELECT instr_id, broker_symbol FROM instrument_broker_symbols")
    ).all():
        candidates[_normalized_symbol(symbol)].add(int(instr_id))
    return candidates


def _backfill_watermark_identity() -> None:
    bind = op.get_bind()
    candidates = _instrument_candidates(bind)
    unresolved: list[str] = []
    rows = bind.execute(
        sa.text("SELECT worker_id, symbol, timeframe, last_ts FROM watermarks")
    ).mappings()
    for row in rows:
        key = _normalized_symbol(row["symbol"])
        matches = candidates.get(key, set())
        if len(matches) != 1:
            unresolved.append(
                f"{row['worker_id']}:{row['symbol']}:{row['timeframe']} -> {sorted(matches)}"
            )
            continue
        instr_id = next(iter(matches))
        sources = {
            str(source)
            for source in bind.execute(
                sa.text(
                    """
                    SELECT DISTINCT source
                    FROM prices
                    WHERE instr_id = :instr_id
                      AND timeframe = :timeframe
                      AND ts <= :last_ts
                    """
                ),
                {
                    "instr_id": instr_id,
                    "timeframe": row["timeframe"],
                    "last_ts": row["last_ts"],
                },
            ).scalars()
        }
        if len(sources) != 1:
            unresolved.append(
                f"{row['worker_id']}:{row['symbol']}:{row['timeframe']} "
                f"-> instr_id={instr_id}, sources={sorted(sources)}"
            )
            continue
        bind.execute(
            sa.text(
                """
                UPDATE watermarks
                SET instr_id = :instr_id, source = :source
                WHERE worker_id = :worker_id
                  AND symbol = :symbol
                  AND timeframe = :timeframe
                """
            ),
            {
                "instr_id": instr_id,
                "source": next(iter(sources)),
                "worker_id": row["worker_id"],
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
            },
        )
    if unresolved:
        sample = "; ".join(unresolved[:10])
        msg = (
            "Historical-price rebuild migration cannot attribute every existing "
            f"watermark to one instrument/source feed; reconcile these rows first: {sample}"
        )
        raise RuntimeError(msg)


def upgrade() -> None:
    op.add_column(
        "prices",
        sa.Column(
            "content_revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column("watermarks", sa.Column("instr_id", sa.Integer(), nullable=True))
    op.add_column(
        "watermarks",
        sa.Column(
            "source",
            sa.String(length=50),
            nullable=True,
        ),
    )
    op.add_column("watermarks", sa.Column("rebuild_from_ts", sa.DateTime(), nullable=True))
    op.add_column(
        "watermarks",
        sa.Column(
            "rebuild_generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    _backfill_watermark_identity()

    with op.batch_alter_table("watermarks") as batch:
        batch.alter_column("instr_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column(
            "source",
            existing_type=sa.String(length=50),
            nullable=False,
            server_default=None,
        )
        batch.create_foreign_key(
            "fk_watermarks_instr_id_instruments",
            "instruments",
            ["instr_id"],
            ["instr_id"],
            ondelete="CASCADE",
        )
        batch.create_check_constraint(
            "ck_watermarks_rebuild_generation_nonnegative",
            "rebuild_generation >= 0",
        )

    op.create_index(
        "ix_watermarks_feed_rebuild",
        "watermarks",
        ["instr_id", "source", "timeframe", "rebuild_from_ts"],
    )

    if op.get_bind().dialect.name == "postgresql":
        # Price ingestion alone owns historical-rebuild requests. Indicator
        # workers retain their existing SELECT/INSERT/UPDATE watermark grants.
        op.execute("GRANT SELECT, UPDATE ON TABLE public.watermarks TO vm_market_data")


def downgrade() -> None:
    bind = op.get_bind()
    pending = int(
        bind.execute(
            sa.text("SELECT count(*) FROM watermarks WHERE rebuild_from_ts IS NOT NULL")
        ).scalar_one()
    )
    if pending:
        msg = (
            "Cannot remove historical-price rebuild metadata while indicator "
            f"rebuilds are pending ({pending} watermark row(s))"
        )
        raise RuntimeError(msg)

    op.drop_index("ix_watermarks_feed_rebuild", table_name="watermarks")
    with op.batch_alter_table("watermarks") as batch:
        batch.drop_constraint(
            "ck_watermarks_rebuild_generation_nonnegative",
            type_="check",
        )
        batch.drop_constraint(
            "fk_watermarks_instr_id_instruments",
            type_="foreignkey",
        )
        batch.drop_column("rebuild_generation")
        batch.drop_column("rebuild_from_ts")
        batch.drop_column("source")
        batch.drop_column("instr_id")
    op.drop_column("prices", "content_revision")
