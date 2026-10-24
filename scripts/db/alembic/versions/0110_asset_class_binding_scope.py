"""Bound a binding without an instrument list by its asset classes in the overlap rule.

Revision ID: 0110_asset_class_binding_scope
Revises: 0109_backend_ui_readiness

``vm_validate_strategy_binding`` (``0077``) refuses a second active strategy on
an account when the two binding scopes overlap. It read a NULL
``instruments_allowed`` as every instrument, so a strategy with a membership
universe -- the point-in-time S&P 500 has no fixed list -- overlapped every
other strategy and needed an account of its own.

A binding without a list is in fact bounded by ``asset_classes_allowed``:
scoring resolves it to the catalogued instruments of those classes, and
execution refuses an instrument of any other class. This revision makes the
trigger read it the same way, through ``vm_binding_scopes_overlap``:

* two lists overlap when they name a common instrument (unchanged);
* a list and a list-free scope overlap when the list names an instrument of
  one of the other scope's classes;
* two list-free scopes overlap when their classes meet.

Each instrument has exactly one catalogue ``asset_class``, so a list-free
``["equity"]`` binding and a binding listing only crypto instruments can never
authorize the same instrument. A class list that is empty, not an array, or
holds a value outside the canonical taxonomy bounds nothing and still reads as
every instrument. The Python rule (``binding_control.BindingScope``) is the
same.

Upgrade checks no rows: the new rule refuses a subset of what ``0077`` refused,
so every existing pair stays valid. Downgrade restores the ``0077`` function
exactly, and refuses while two active bindings on one account rely on
asset-class separation, because ``0077`` would read them as overlapping.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0110_asset_class_binding_scope"
down_revision = "0109_backend_ui_readiness"
branch_labels = None
depends_on = None

_ASSET_CLASSES_FUNCTION = """
        CREATE OR REPLACE FUNCTION vm_binding_asset_classes(classes JSON)
        RETURNS TEXT[]
        LANGUAGE plpgsql
        IMMUTABLE
        AS $$
        DECLARE
            asset_classes TEXT[];
        BEGIN
            -- NULL means the column bounds nothing: missing, not an array,
            -- empty, or holding a value outside the canonical taxonomy.
            IF classes IS NULL
               OR json_typeof(classes) <> 'array'
               OR json_array_length(classes) = 0
            THEN
                RETURN NULL;
            END IF;
            SELECT array_agg(DISTINCT item.value)
              INTO asset_classes
              FROM json_array_elements_text(classes) AS item(value);
            IF EXISTS (
                SELECT 1
                  FROM unnest(asset_classes) AS asset(value)
                 WHERE asset.value IS NULL
                    OR asset.value NOT IN (
                        'crypto', 'equity', 'etf', 'index', 'futures',
                        'options', 'fx', 'commodities'
                    )
            )
            THEN
                RETURN NULL;
            END IF;
            RETURN asset_classes;
        END;
        $$;
        """

_SCOPES_OVERLAP_FUNCTION = """
        CREATE OR REPLACE FUNCTION vm_binding_scopes_overlap(
            left_ids BIGINT[],
            left_classes TEXT[],
            right_ids BIGINT[],
            right_classes TEXT[]
        )
        RETURNS BOOLEAN
        LANGUAGE sql
        STABLE
        AS $$
            -- An id list is the whole scope. Without one, the scope is every
            -- instrument in its classes; NULL classes bound nothing.
            SELECT CASE
                WHEN left_ids IS NOT NULL AND right_ids IS NOT NULL THEN
                    left_ids && right_ids
                WHEN left_ids IS NULL AND right_ids IS NULL THEN
                    left_classes IS NULL
                    OR right_classes IS NULL
                    OR left_classes && right_classes
                WHEN left_ids IS NULL THEN
                    left_classes IS NULL
                    OR EXISTS (
                        SELECT 1
                          FROM public.instruments AS instrument
                         WHERE instrument.instr_id = ANY(right_ids)
                           AND instrument.asset_class = ANY(left_classes)
                    )
                ELSE
                    right_classes IS NULL
                    OR EXISTS (
                        SELECT 1
                          FROM public.instruments AS instrument
                         WHERE instrument.instr_id = ANY(left_ids)
                           AND instrument.asset_class = ANY(right_classes)
                    )
            END
        $$;
        """

#: ``0077``'s ``vm_validate_strategy_binding``, verbatim except for the overlap
#: predicate, which each direction of this revision fills in.
_VALIDATE_BINDING_TEMPLATE = """
        CREATE OR REPLACE FUNCTION vm_validate_strategy_binding()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        DECLARE
            new_scope BIGINT[];
            selected_broker TEXT;
        BEGIN
            -- The conflict query below cannot by itself prevent two
            -- READ COMMITTED transactions from both observing an empty scope.
            -- Serialize all authority changes for an account at the database
            -- boundary as well as in the backend API.  UPDATEs that move a row
            -- lock both account scopes in numeric order to avoid deadlocks.
            IF TG_OP = 'UPDATE'
               AND OLD.broker_account_id IS DISTINCT FROM NEW.broker_account_id
            THEN
                IF OLD.broker_account_id < NEW.broker_account_id THEN
                    PERFORM pg_advisory_xact_lock(
                        hashtext('vm_binding_account'),
                        hashtext(OLD.broker_account_id::TEXT)
                    );
                    PERFORM pg_advisory_xact_lock(
                        hashtext('vm_binding_account'),
                        hashtext(NEW.broker_account_id::TEXT)
                    );
                ELSE
                    PERFORM pg_advisory_xact_lock(
                        hashtext('vm_binding_account'),
                        hashtext(NEW.broker_account_id::TEXT)
                    );
                    PERFORM pg_advisory_xact_lock(
                        hashtext('vm_binding_account'),
                        hashtext(OLD.broker_account_id::TEXT)
                    );
                END IF;
            ELSE
                PERFORM pg_advisory_xact_lock(
                    hashtext('vm_binding_account'),
                    hashtext(NEW.broker_account_id::TEXT)
                );
            END IF;

            IF json_typeof(NEW.execution_modes_allowed) <> 'array'
               OR json_array_length(NEW.execution_modes_allowed) = 0
               OR EXISTS (
                    SELECT 1
                      FROM json_array_elements_text(NEW.execution_modes_allowed) AS mode(value)
                     WHERE mode.value NOT IN (
                        'spot', 'margin', 'perpetual', 'futures', 'bull_call',
                        'bear_put', 'bull_put', 'bear_call', 'iron_condor',
                        'straddle', 'strangle', 'options_single', 'notify_only', 'paper'
                     )
               )
            THEN
                RAISE EXCEPTION 'binding has invalid execution_modes_allowed';
            END IF;
            IF NEW.preferred_mode IS NOT NULL
               AND NOT (
                    NEW.execution_modes_allowed::jsonb
                    @> to_jsonb(ARRAY[NEW.preferred_mode]::TEXT[])
               )
            THEN
                RAISE EXCEPTION 'binding preferred_mode is not allowed';
            END IF;
            IF json_typeof(NEW.asset_classes_allowed) <> 'array'
               OR json_array_length(NEW.asset_classes_allowed) = 0
               OR EXISTS (
                    SELECT 1
                      FROM json_array_elements_text(NEW.asset_classes_allowed) AS asset(value)
                     WHERE asset.value NOT IN (
                        'crypto', 'equity', 'etf', 'index', 'futures',
                        'options', 'fx', 'commodities'
                     )
               )
            THEN
                RAISE EXCEPTION 'binding has invalid asset_classes_allowed';
            END IF;
            IF NEW.allowed_brokers IS NOT NULL THEN
                IF json_typeof(NEW.allowed_brokers) <> 'array'
                   OR json_array_length(NEW.allowed_brokers) = 0
                   OR EXISTS (
                        SELECT 1
                          FROM json_array_elements_text(NEW.allowed_brokers) AS allowed(value)
                          LEFT JOIN brokers ON brokers.code = allowed.value
                         WHERE brokers.broker_id IS NULL
                   )
                THEN
                    RAISE EXCEPTION 'binding has invalid allowed_brokers';
                END IF;
                SELECT broker.code
                  INTO selected_broker
                  FROM linked_broker_accounts AS account
                  JOIN brokers AS broker ON broker.broker_id = account.broker_id
                 WHERE account.account_id = NEW.broker_account_id;
                IF selected_broker IS NULL
                   OR NOT (
                        NEW.allowed_brokers::jsonb
                        @> to_jsonb(ARRAY[selected_broker]::TEXT[])
                   )
                THEN
                    RAISE EXCEPTION 'binding account broker is outside allowed_brokers';
                END IF;
            END IF;

            IF NOT NEW.is_active THEN
                RETURN NEW;
            END IF;
            IF NEW.strategy_id IS NULL OR length(trim(NEW.strategy_id)) = 0 THEN
                RAISE EXCEPTION 'active binding requires explicit strategy_id';
            END IF;
            new_scope := vm_binding_instrument_ids(NEW.instruments_allowed);
            IF EXISTS (
                SELECT 1
                  FROM user_strategy_bindings AS existing
                 WHERE existing.binding_id IS DISTINCT FROM NEW.binding_id
                   AND existing.broker_account_id = NEW.broker_account_id
                   AND existing.is_active
                   AND existing.strategy_id IS DISTINCT FROM NEW.strategy_id
__OVERLAP__            )
            THEN
                RAISE EXCEPTION
                    'another active strategy owns an overlapping account/instrument scope';
            END IF;
            RETURN NEW;
        END;
        $$;
"""

#: ``0077``: a NULL list on either side is every instrument.
_INSTRUMENT_ONLY_OVERLAP = """                   AND (
                        new_scope IS NULL
                        OR vm_binding_instrument_ids(existing.instruments_allowed) IS NULL
                        OR new_scope && vm_binding_instrument_ids(existing.instruments_allowed)
                   )
"""

#: This revision: a NULL list is every instrument of the binding's classes.
_ASSET_CLASS_OVERLAP = """                   AND vm_binding_scopes_overlap(
                        new_scope,
                        vm_binding_asset_classes(NEW.asset_classes_allowed),
                        vm_binding_instrument_ids(existing.instruments_allowed),
                        vm_binding_asset_classes(existing.asset_classes_allowed)
                   )
"""

#: Active pairs on one account that ``0077`` reads as overlapping.
_PAIRS_ONLY_CLASSES_SEPARATE = """
    SELECT COUNT(*)
      FROM user_strategy_bindings AS earlier
      JOIN user_strategy_bindings AS later
        ON later.broker_account_id = earlier.broker_account_id
       AND later.binding_id > earlier.binding_id
     WHERE earlier.is_active
       AND later.is_active
       AND earlier.strategy_id IS DISTINCT FROM later.strategy_id
       AND (
            vm_binding_instrument_ids(earlier.instruments_allowed) IS NULL
            OR vm_binding_instrument_ids(later.instruments_allowed) IS NULL
            OR vm_binding_instrument_ids(earlier.instruments_allowed)
               && vm_binding_instrument_ids(later.instruments_allowed)
       )
"""


def _validate_binding_function(overlap: str) -> str:
    """The trigger function with ``overlap`` as its conflict predicate."""
    return _VALIDATE_BINDING_TEMPLATE.replace("__OVERLAP__", overlap)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_ASSET_CLASSES_FUNCTION)
    op.execute(_SCOPES_OVERLAP_FUNCTION)
    op.execute(_validate_binding_function(_ASSET_CLASS_OVERLAP))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    pairs = int(bind.execute(sa.text(_PAIRS_ONLY_CLASSES_SEPARATE)).scalar_one())
    if pairs:
        msg = (
            "cannot restore the instrument-only binding overlap rule: "
            f"{pairs} pair(s) of active bindings share an account only because their "
            "asset classes differ; switch one strategy of each pair off first"
        )
        raise RuntimeError(msg)
    op.execute(_validate_binding_function(_INSTRUMENT_ONLY_OVERLAP))
    op.execute(
        "DROP FUNCTION IF EXISTS vm_binding_scopes_overlap(BIGINT[], TEXT[], BIGINT[], TEXT[])"
    )
    op.execute("DROP FUNCTION IF EXISTS vm_binding_asset_classes(JSON)")
