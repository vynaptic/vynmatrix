"""Bind validity-dated symbols to permanent equity security identities.

Revision ID: 0091_dated_security_symbols
Revises: 0090_optional_factor_contracts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0091_dated_security_symbols"
down_revision = "0090_optional_factor_contracts"
branch_labels = None
depends_on = None

_TABLE = "equity_security_identities"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _assert_existing_intervals_do_not_overlap() -> None:
    connection = op.get_bind()
    overlap = connection.scalar(
        sa.text(
            "SELECT count(*) FROM equity_security_identities lhs "
            "JOIN equity_security_identities rhs ON lhs.identity_id < rhs.identity_id "
            "AND (lhs.security_id = rhs.security_id OR lhs.instr_id = rhs.instr_id) "
            "AND lhs.effective_from <= COALESCE(rhs.effective_to, '9999-12-31') "
            "AND rhs.effective_from <= COALESCE(lhs.effective_to, '9999-12-31')"
        )
    )
    if int(overlap or 0):
        message = "Cannot add dated equity symbols while security identity intervals overlap"
        raise RuntimeError(message)


def _install_postgresql_overlap_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION public.vm_reject_overlapping_equity_security_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.equity_security_identities existing
                WHERE existing.identity_id <> COALESCE(NEW.identity_id, -1)
                  AND (
                    existing.security_id = NEW.security_id
                    OR existing.instr_id = NEW.instr_id
                  )
                  AND existing.effective_from
                        <= COALESCE(NEW.effective_to, 'infinity'::date)
                  AND NEW.effective_from
                        <= COALESCE(existing.effective_to, 'infinity'::date)
            ) THEN
                RAISE EXCEPTION 'equity security identity intervals overlap'
                    USING ERRCODE = '23505';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_equity_security_identity_no_overlap
        BEFORE INSERT OR UPDATE ON public.equity_security_identities
        FOR EACH ROW
        EXECUTE FUNCTION public.vm_reject_overlapping_equity_security_identity()
        """
    )


def upgrade() -> None:
    _assert_existing_intervals_do_not_overlap()
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column("canonical_symbol", sa.String(length=50), nullable=True))
    op.execute(
        "UPDATE equity_security_identities AS identity "
        "SET canonical_symbol = ("
        "SELECT instrument.canonical FROM instruments AS instrument "
        "WHERE instrument.instr_id = identity.instr_id) "
        "WHERE identity.canonical_symbol IS NULL"
    )
    missing = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM equity_security_identities "
            "WHERE canonical_symbol IS NULL "
            "OR length(trim(canonical_symbol)) = 0 "
            "OR canonical_symbol <> upper(trim(canonical_symbol))"
        )
    )
    if int(missing or 0):
        message = "Cannot backfill one canonical symbol for every equity security identity"
        raise RuntimeError(message)
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            "canonical_symbol",
            existing_type=sa.String(length=50),
            nullable=False,
        )
        batch_op.drop_constraint("ck_equity_security_identity_values", type_="check")
        batch_op.create_check_constraint(
            "ck_equity_security_identity_values",
            "length(trim(security_id)) > 0 AND length(trim(issuer_id)) > 0 "
            "AND canonical_symbol = upper(trim(canonical_symbol)) "
            "AND length(trim(canonical_symbol)) > 0 "
            "AND length(trim(source_ref)) > 0 AND length(observation_id) = 64",
        )
        batch_op.create_index(
            "ix_equity_security_identity_symbol_effective",
            ["canonical_symbol", "effective_from", "effective_to"],
        )
    if _is_postgresql():
        _install_postgresql_overlap_guard()


def downgrade() -> None:
    renamed = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM equity_security_identities identity "
            "JOIN instruments instrument ON instrument.instr_id = identity.instr_id "
            "WHERE identity.canonical_symbol <> instrument.canonical"
        )
    )
    if int(renamed or 0):
        message = "Cannot remove dated equity symbols after a ticker transition is persisted"
        raise RuntimeError(message)
    if _is_postgresql():
        op.execute(
            "DROP TRIGGER IF EXISTS trg_equity_security_identity_no_overlap "
            "ON public.equity_security_identities"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS public.vm_reject_overlapping_equity_security_identity()"
        )
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_index("ix_equity_security_identity_symbol_effective")
        batch_op.drop_constraint("ck_equity_security_identity_values", type_="check")
        batch_op.create_check_constraint(
            "ck_equity_security_identity_values",
            "length(trim(security_id)) > 0 AND length(trim(issuer_id)) > 0 "
            "AND length(trim(source_ref)) > 0 AND length(observation_id) = 64",
        )
        batch_op.drop_column("canonical_symbol")


__all__ = ["downgrade", "upgrade"]
