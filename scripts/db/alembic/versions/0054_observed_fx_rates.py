"""Add observed FX source instruments and unify currency defaults.

Revision ID: 0054_observed_fx_rates
Revises: 0053_feedback_suggestion_only
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054_observed_fx_rates"
down_revision = "0053_feedback_suggestion_only"
branch_labels = None
depends_on = None

_FX_INSTRUMENTS = (
    {
        "instr_id": 10,
        "canonical": "EUR/USD",
        "exchange": "ecb",
        "settlement_currency": "USD",
        "tick_size": "0.00001",
    },
    {
        "instr_id": 14,
        "canonical": "EUR/INR",
        "exchange": "ecb",
        "settlement_currency": "INR",
        "tick_size": "0.0001",
    },
    {
        "instr_id": 15,
        "canonical": "EUR/GBP",
        "exchange": "ecb",
        "settlement_currency": "GBP",
        "tick_size": "0.00001",
    },
    {
        "instr_id": 16,
        "canonical": "USDC/EUR",
        "exchange": "coinbase",
        "settlement_currency": "EUR",
        "tick_size": "0.000001",
    },
)
_ALIASES = {
    "EUR/USD": "EURUSD",
    "EUR/INR": "EURINR",
    "EUR/GBP": "EURGBP",
    "USDC/EUR": "USDCEUR",
}
_BASE_CURRENCY_CONSTRAINTS = {
    "users": "ck_user_base_ccy",
    "linked_broker_accounts": "ck_account_base_ccy",
}


def _normalized_symbol(value: str) -> str:
    return value.strip().upper().replace("/", "").replace("-", "").replace("_", "")


def _ensure_fx_catalogue() -> None:
    bind = op.get_bind()
    existing_rows = bind.execute(
        sa.text(
            """
            SELECT instr_id, asset_class, canonical
            FROM instruments
            """
        )
    ).mappings()
    by_normalized: dict[str, list[dict[str, object]]] = {}
    for row in existing_rows:
        by_normalized.setdefault(_normalized_symbol(str(row["canonical"])), []).append(
            {
                "instr_id": int(row["instr_id"]),
                "asset_class": str(row["asset_class"]),
                "canonical": str(row["canonical"]),
            }
        )

    for specification in _FX_INSTRUMENTS:
        normalized = _normalized_symbol(str(specification["canonical"]))
        matches = by_normalized.get(normalized, [])
        if len(matches) > 1:
            msg = (
                f"Multiple instruments normalize to FX symbol "
                f"{specification['canonical']}: {matches}"
            )
            raise RuntimeError(msg)
        if matches and matches[0]["asset_class"] != "fx":
            msg = (
                f"Instrument {matches[0]['canonical']} conflicts with required FX symbol "
                f"{specification['canonical']} but has asset_class={matches[0]['asset_class']}"
            )
            raise RuntimeError(msg)
        if not matches:
            preferred_id_owner = bind.execute(
                sa.text(
                    """
                    SELECT canonical
                    FROM instruments
                    WHERE instr_id = :instr_id
                    """
                ),
                {"instr_id": specification["instr_id"]},
            ).scalar_one_or_none()
            if preferred_id_owner is None:
                bind.execute(
                    sa.text(
                        """
                        INSERT INTO instruments (
                            instr_id,
                            asset_class,
                            canonical,
                            exchange,
                            settlement_currency,
                            tick_size,
                            lot_size
                        )
                        VALUES (
                            :instr_id,
                            'fx',
                            :canonical,
                            :exchange,
                            :settlement_currency,
                            :tick_size,
                            1
                        )
                        """
                    ),
                    specification,
                )
            else:
                bind.execute(
                    sa.text(
                        """
                        INSERT INTO instruments (
                            asset_class,
                            canonical,
                            exchange,
                            settlement_currency,
                            tick_size,
                            lot_size
                        )
                        VALUES (
                            'fx',
                            :canonical,
                            :exchange,
                            :settlement_currency,
                            :tick_size,
                            1
                        )
                        """
                    ),
                    specification,
                )
        else:
            existing = matches[0]
            bind.execute(
                sa.text(
                    """
                    UPDATE instruments
                    SET canonical = :canonical,
                        exchange = :exchange,
                        settlement_currency = :settlement_currency,
                        tick_size = :tick_size,
                        lot_size = 1
                    WHERE instr_id = :instr_id
                    """
                ),
                {**specification, "instr_id": existing["instr_id"]},
            )

    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                """
                SELECT setval(
                    pg_get_serial_sequence('instruments', 'instr_id'),
                    (SELECT max(instr_id) FROM instruments),
                    true
                )
                """
            )
        )

    rows = bind.execute(
        sa.text(
            """
            SELECT instr_id, canonical
            FROM instruments
            WHERE asset_class = 'fx'
            """
        )
    ).mappings()
    instrument_ids = {
        _normalized_symbol(str(row["canonical"])): int(row["instr_id"]) for row in rows
    }
    for canonical, alias in _ALIASES.items():
        instr_id = instrument_ids[_normalized_symbol(canonical)]
        existing_alias = bind.execute(
            sa.text(
                """
                SELECT instr_id
                FROM instrument_aliases
                WHERE upper(alias) = :alias
                """
            ),
            {"alias": alias},
        ).scalar_one_or_none()
        if existing_alias is not None and int(existing_alias) != instr_id:
            msg = f"FX alias {alias} already points to a different instrument"
            raise RuntimeError(msg)
        if existing_alias is None:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO instrument_aliases (instr_id, alias, source)
                    VALUES (:instr_id, :alias, 'fx_reference')
                    """
                ),
                {"instr_id": instr_id, "alias": alias},
            )

    coinbase_id = bind.execute(
        sa.text("SELECT broker_id FROM brokers WHERE lower(code) = 'coinbase'")
    ).scalar_one_or_none()
    if coinbase_id is not None:
        usdc_eur_id = instrument_ids["USDCEUR"]
        existing_mapping = bind.execute(
            sa.text(
                """
                SELECT broker_symbol
                FROM instrument_broker_symbols
                WHERE instr_id = :instr_id AND broker_id = :broker_id
                """
            ),
            {"instr_id": usdc_eur_id, "broker_id": coinbase_id},
        ).scalar_one_or_none()
        if existing_mapping is None:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO instrument_broker_symbols (
                        instr_id,
                        broker_id,
                        broker_symbol
                    )
                    VALUES (:instr_id, :broker_id, 'USDC-EUR')
                    """
                ),
                {"instr_id": usdc_eur_id, "broker_id": coinbase_id},
            )
        elif str(existing_mapping).upper() != "USDC-EUR":
            msg = "USDC/EUR already has a conflicting Coinbase broker symbol"
            raise RuntimeError(msg)


def _upgrade_base_currency_columns() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table, constraint_name in _BASE_CURRENCY_CONSTRAINTS.items():
            with op.batch_alter_table(table) as batch:
                batch.alter_column(
                    "base_ccy",
                    existing_type=sa.String(length=10),
                    nullable=False,
                    server_default=None,
                )
                batch.create_check_constraint(
                    constraint_name,
                    ("base_ccy = upper(trim(base_ccy)) AND length(base_ccy) BETWEEN 3 AND 10"),
                )
        return
    for table, constraint_name in _BASE_CURRENCY_CONSTRAINTS.items():
        op.alter_column(
            table,
            "base_ccy",
            existing_type=sa.String(length=10),
            nullable=False,
            server_default=None,
        )
        op.create_check_constraint(
            constraint_name,
            table,
            "base_ccy = upper(trim(base_ccy)) AND length(base_ccy) BETWEEN 3 AND 10",
        )


def _downgrade_base_currency_columns() -> None:
    dialect = op.get_bind().dialect.name
    previous_shapes = {
        "users": {"nullable": True, "server_default": None},
        "linked_broker_accounts": {"nullable": True, "server_default": "USD"},
    }
    if dialect == "sqlite":
        for table, previous_shape in previous_shapes.items():
            with op.batch_alter_table(table) as batch:
                batch.drop_constraint(
                    _BASE_CURRENCY_CONSTRAINTS[table],
                    type_="check",
                )
                batch.alter_column(
                    "base_ccy",
                    existing_type=sa.String(length=10),
                    **previous_shape,
                )
        return
    for table, previous_shape in previous_shapes.items():
        op.drop_constraint(
            _BASE_CURRENCY_CONSTRAINTS[table],
            table,
            type_="check",
        )
        op.alter_column(
            table,
            "base_ccy",
            existing_type=sa.String(length=10),
            **previous_shape,
        )


def upgrade() -> None:
    bind = op.get_bind()
    missing_users = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM users
            WHERE base_ccy IS NULL OR trim(base_ccy) = ''
            """
        )
    ).scalar_one()
    missing_accounts = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM linked_broker_accounts
            WHERE base_ccy IS NULL OR trim(base_ccy) = ''
            """
        )
    ).scalar_one()
    if missing_users or missing_accounts:
        message = (
            "base currency migration requires explicit values for every user and "
            "linked broker account; resolve "
            f"{missing_users} user row(s) and {missing_accounts} account row(s)"
        )
        raise RuntimeError(message)

    op.execute(
        """
        UPDATE users
        SET base_ccy = upper(trim(base_ccy))
        """
    )
    op.execute(
        """
        UPDATE linked_broker_accounts
        SET base_ccy = upper(trim(base_ccy))
        """
    )
    _upgrade_base_currency_columns()
    _ensure_fx_catalogue()


def downgrade() -> None:
    # Preserve catalogue rows: by the time rollback is needed they can own
    # production price history, so deleting them would either fail on foreign
    # keys or destroy observed market data. Revision 0053 tolerates extra
    # instruments and mappings.
    _downgrade_base_currency_columns()
