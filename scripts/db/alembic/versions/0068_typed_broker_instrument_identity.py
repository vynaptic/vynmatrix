"""Add typed, broker-scoped instrument catalogue identity.

Broker symbols are not sufficient for venues whose APIs require an opaque
numeric identifier, and Saxo additionally requires an AssetType discriminator.
The new fields remain nullable for text-symbol venues and historical mappings;
each adapter declares which facts it requires and fails closed when absent.
Downgrade never deletes typed identities implicitly.

Revision ID: 0068_typed_broker_instrument
Revises: 0067_market_sessions
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0068_typed_broker_instrument"
down_revision = "0067_market_sessions"
branch_labels = None
depends_on = None

_BROKERS = sa.table(
    "brokers",
    sa.column("broker_id", sa.Integer()),
    sa.column("code", sa.String()),
)
_INSTRUMENTS = sa.table(
    "instruments",
    sa.column("instr_id", sa.Integer()),
    sa.column("asset_class", sa.String()),
    sa.column("canonical", sa.String()),
)
_MAPPINGS = sa.table(
    "instrument_broker_symbols",
    sa.column("instr_id", sa.Integer()),
    sa.column("broker_id", sa.Integer()),
    sa.column("broker_symbol", sa.String()),
    sa.column("broker_instrument_id", sa.String()),
    sa.column("broker_instrument_type", sa.String()),
)
_SAXO_EURUSD_ID = "21"
_SAXO_EURUSD_TYPE = "FxSpot"
_SAXO_EURUSD_SYMBOL = "EURUSD"


def _lock_downgrade_state() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE instrument_broker_symbols IN ACCESS EXCLUSIVE MODE")


def upgrade() -> None:
    with op.batch_alter_table("instrument_broker_symbols") as batch_op:
        batch_op.add_column(sa.Column("broker_instrument_id", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("broker_instrument_type", sa.String(length=100), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_instrument_broker_id_nonblank",
            "broker_instrument_id IS NULL OR length(trim(broker_instrument_id)) > 0",
        )
        batch_op.create_check_constraint(
            "ck_instrument_broker_type_nonblank",
            "broker_instrument_type IS NULL OR length(trim(broker_instrument_type)) > 0",
        )
        batch_op.create_unique_constraint(
            "uq_instrument_broker_venue_identity",
            ["broker_id", "broker_instrument_id", "broker_instrument_type"],
        )
    untyped_identity = sa.text(
        "broker_instrument_id IS NOT NULL AND broker_instrument_type IS NULL"
    )
    op.create_index(
        "uq_instrument_broker_untyped_venue_id",
        "instrument_broker_symbols",
        ["broker_id", "broker_instrument_id"],
        unique=True,
        postgresql_where=untyped_identity,
        sqlite_where=untyped_identity,
    )

    bind = op.get_bind()
    identity = bind.execute(
        sa.select(_INSTRUMENTS.c.instr_id, _BROKERS.c.broker_id)
        .select_from(_INSTRUMENTS)
        .join(_BROKERS, _BROKERS.c.code == "saxo")
        .where(
            _INSTRUMENTS.c.asset_class == "fx",
            _INSTRUMENTS.c.canonical == "EUR/USD",
        )
    ).one_or_none()
    if identity is None:
        msg = "Typed broker identity requires the canonical Saxo and EUR/USD catalogue rows"
        raise RuntimeError(msg)
    instrument_id, broker_id = (int(identity[0]), int(identity[1]))
    existing = bind.execute(
        sa.select(
            _MAPPINGS.c.broker_symbol,
            _MAPPINGS.c.broker_instrument_id,
            _MAPPINGS.c.broker_instrument_type,
        ).where(
            _MAPPINGS.c.instr_id == instrument_id,
            _MAPPINGS.c.broker_id == broker_id,
        )
    ).one_or_none()
    expected = (
        _SAXO_EURUSD_SYMBOL,
        _SAXO_EURUSD_ID,
        _SAXO_EURUSD_TYPE,
    )
    if existing is None:
        bind.execute(
            sa.insert(_MAPPINGS).values(
                instr_id=instrument_id,
                broker_id=broker_id,
                broker_symbol=_SAXO_EURUSD_SYMBOL,
                broker_instrument_id=_SAXO_EURUSD_ID,
                broker_instrument_type=_SAXO_EURUSD_TYPE,
            )
        )
    elif tuple(existing) != expected:
        msg = (
            "Existing Saxo EUR/USD mapping conflicts with the official "
            "UIC=21, AssetType=FxSpot catalogue identity"
        )
        raise RuntimeError(msg)


def downgrade() -> None:
    _lock_downgrade_state()
    bind = op.get_bind()
    typed_identities = int(
        bind.execute(
            sa.select(sa.func.count())
            .select_from(_MAPPINGS)
            .where(
                sa.or_(
                    _MAPPINGS.c.broker_instrument_id.is_not(None),
                    _MAPPINGS.c.broker_instrument_type.is_not(None),
                )
            )
        ).scalar_one()
    )
    if typed_identities:
        msg = (
            "Cannot downgrade typed broker instrument identity while "
            f"{typed_identities} typed mapping(s) exist; deleting opaque venue "
            "identity would make broker routing ambiguous. Retire those mappings "
            "explicitly before downgrading."
        )
        raise RuntimeError(msg)

    op.drop_index(
        "uq_instrument_broker_untyped_venue_id",
        table_name="instrument_broker_symbols",
    )
    with op.batch_alter_table("instrument_broker_symbols") as batch_op:
        batch_op.drop_constraint(
            "uq_instrument_broker_venue_identity",
            type_="unique",
        )
        batch_op.drop_constraint(
            "ck_instrument_broker_type_nonblank",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_instrument_broker_id_nonblank",
            type_="check",
        )
        batch_op.drop_column("broker_instrument_type")
        batch_op.drop_column("broker_instrument_id")
