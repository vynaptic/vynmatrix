"""Canonicalize asset classes and make cash indices reference-only.

The platform previously mixed ``forex``/``fx`` and
``commodity``/``commodities``, coerced ETFs and indices to equity during
catalogue bootstrap, and had different check constraints across scoring and
feedback tables. This revision establishes one persisted taxonomy, adds an
explicit instrument tradability boundary, and migrates the source-controlled
SPY/QQQ and NIFTY50/BANKNIFTY classifications without making cash indices
executable. Downgrade refuses to erase canonical ETF or cash-index semantics.

Revision ID: 0070_canonical_asset_taxonomy
Revises: 0069_broker_account_contracts
Create Date: 2026-07-25
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0070_canonical_asset_taxonomy"
down_revision = "0069_broker_account_contracts"
branch_labels = None
depends_on = None

_CANONICAL_ASSET_CLASSES = (
    "crypto",
    "equity",
    "etf",
    "index",
    "futures",
    "options",
    "fx",
    "commodities",
)
_ALIASES = {
    "commodity": "commodities",
    "forex": "fx",
    "indices": "index",
}
_ASSET_CLASS_SQL = (
    "asset_class IN (" + ", ".join(f"'{value}'" for value in _CANONICAL_ASSET_CLASSES) + ")"
)
_NULLABLE_ASSET_CLASS_SQL = (
    "asset_class IS NULL OR asset_class IN ("
    + ", ".join(f"'{value}'" for value in _CANONICAL_ASSET_CLASSES)
    + ")"
)

# Legacy constraints that may exist at revision 0069. The historical migration
# chain did not create every model-declared check, while some early databases
# were bootstrapped through ORM metadata and did. Drop only the checks actually
# present before lexical normalization; the upgrade then converges both lineages
# onto the same complete constraint set.
_LEGACY_CONSTRAINTS = (
    ("user_trading_policies", "ck_policy_asset_class"),
    ("market_scores", "ck_market_score_asset_class"),
    ("backtest_results", "ck_backtest_asset_class"),
)
_OPTIONAL_METADATA_CONSTRAINTS = (
    ("instruments", "ck_asset_class"),
    ("sectors", "ck_sector_asset_class"),
)
_CONSTRAINTS_TO_DROP = _LEGACY_CONSTRAINTS + _OPTIONAL_METADATA_CONSTRAINTS
_CURRENT_CONSTRAINTS = (
    ("instruments", "ck_asset_class", _ASSET_CLASS_SQL),
    ("user_trading_policies", "ck_policy_asset_class", _ASSET_CLASS_SQL),
    ("user_budget_buckets", "ck_budget_asset_class", _ASSET_CLASS_SQL),
    ("sectors", "ck_sector_asset_class", _ASSET_CLASS_SQL),
    ("market_scores", "ck_market_score_asset_class", _ASSET_CLASS_SQL),
    (
        "mode_performance",
        "ck_mode_performance_asset_class",
        _NULLABLE_ASSET_CLASS_SQL,
    ),
    ("scoring_rules", "ck_scoring_rule_asset_class", _NULLABLE_ASSET_CLASS_SQL),
    ("strategies", "ck_strategy_asset_class", _NULLABLE_ASSET_CLASS_SQL),
    (
        "execution_metrics",
        "ck_execution_metric_asset_class",
        _NULLABLE_ASSET_CLASS_SQL,
    ),
    ("backtest_results", "ck_backtest_asset_class", _ASSET_CLASS_SQL),
)
_ASSET_CLASS_TABLES = tuple(dict.fromkeys(table for table, *_rest in _CURRENT_CONSTRAINTS))
_LEGACY_ALLOWED_BY_TABLE = {
    "instruments": {"crypto", "equity", "futures", "options", "fx"},
    "user_trading_policies": {"crypto", "equity", "futures", "options", "fx"},
    "sectors": {"crypto", "equity", "futures", "options", "fx", "commodities"},
    "market_scores": {"crypto", "equity", "futures", "options", "fx", "commodities"},
    "backtest_results": {"crypto", "equity", "futures", "options", "fx"},
}

_INSTRUMENTS = sa.table(
    "instruments",
    sa.column("instr_id", sa.Integer()),
    sa.column("canonical", sa.String()),
    sa.column("asset_class", sa.String()),
    sa.column("is_tradable", sa.Boolean()),
)
_BINDINGS = sa.table(
    "user_strategy_bindings",
    sa.column("binding_id", sa.BigInteger()),
    sa.column("asset_classes_allowed", sa.JSON()),
)
_BROKERS = sa.table(
    "brokers",
    sa.column("broker_id", sa.Integer()),
    sa.column("code", sa.String()),
    sa.column("capabilities", sa.JSON()),
)

_REFERENCE_INDEXES = {"NIFTY50", "BANKNIFTY"}
_CANONICAL_ETFS = {"SPY", "QQQ"}
_ETF_BROKERS = {"ibkr", "saxo", "zerodha"}
_PAPER_ASSET_CLASSES = (
    "crypto",
    "equity",
    "etf",
    "futures",
    "options",
    "fx",
    "commodities",
)


def _canonicalize(value: object, *, context: str) -> str:
    normalized = str(value or "").strip().lower()
    canonical = _ALIASES.get(normalized, normalized)
    if canonical not in _CANONICAL_ASSET_CLASSES:
        msg = f"{context} contains unsupported asset class {value!r}"
        raise RuntimeError(msg)
    return canonical


def _require_normalizable_existing_data() -> None:
    bind = op.get_bind()
    for table_name in _ASSET_CLASS_TABLES:
        values = bind.execute(
            sa.text(f"SELECT DISTINCT asset_class FROM {table_name} WHERE asset_class IS NOT NULL")
        ).scalars()
        for value in values:
            _canonicalize(value, context=table_name)


def _drop_constraints(constraints: tuple[tuple[str, str], ...]) -> None:
    bind = op.get_bind()
    for table_name, constraint_name in constraints:
        existing = {
            constraint["name"]
            for constraint in sa.inspect(bind).get_check_constraints(table_name)
            if constraint.get("name")
        }
        if constraint_name not in existing:
            continue
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(constraint_name, type_="check")


def _normalize_asset_class_columns() -> None:
    for table_name in _ASSET_CLASS_TABLES:
        op.execute(
            sa.text(
                f"""
                UPDATE {table_name}
                SET asset_class = CASE lower(trim(asset_class))
                    WHEN 'commodity' THEN 'commodities'
                    WHEN 'forex' THEN 'fx'
                    WHEN 'indices' THEN 'index'
                    ELSE lower(trim(asset_class))
                END
                WHERE asset_class IS NOT NULL
                """
            )
        )


def _reclassify_source_controlled_instruments() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.update(_INSTRUMENTS)
        .where(sa.func.upper(sa.func.trim(_INSTRUMENTS.c.canonical)).in_(_CANONICAL_ETFS))
        .values(asset_class="etf", is_tradable=True)
    )
    bind.execute(
        sa.update(_INSTRUMENTS)
        .where(sa.func.upper(sa.func.trim(_INSTRUMENTS.c.canonical)).in_(_REFERENCE_INDEXES))
        .values(asset_class="index", is_tradable=False)
    )
    # No cash index is executable. Strategies must route an explicit futures or
    # options instrument with its own broker identity.
    bind.execute(
        sa.update(_INSTRUMENTS)
        .where(_INSTRUMENTS.c.asset_class == "index")
        .values(is_tradable=False)
    )


def _normalize_binding_filters() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.select(_BINDINGS.c.binding_id, _BINDINGS.c.asset_classes_allowed)).all()
    for binding_id, raw_values in rows:
        if not isinstance(raw_values, list):
            msg = (
                f"user_strategy_bindings binding_id={binding_id} has a non-list "
                "asset_classes_allowed value"
            )
            raise TypeError(msg)
        canonical_values = list(
            dict.fromkeys(
                _canonicalize(
                    value,
                    context=f"user_strategy_bindings binding_id={binding_id}",
                )
                for value in raw_values
            )
        )
        if canonical_values != raw_values:
            bind.execute(
                sa.update(_BINDINGS)
                .where(_BINDINGS.c.binding_id == binding_id)
                .values(asset_classes_allowed=canonical_values)
            )


def _converge_broker_capabilities() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_BROKERS.c.broker_id, _BROKERS.c.code, _BROKERS.c.capabilities)
    ).all()
    for broker_id, raw_code, raw_capabilities in rows:
        if not isinstance(raw_capabilities, dict):
            msg = f"brokers broker_id={broker_id} has non-object capabilities"
            raise TypeError(msg)
        capabilities: dict[str, Any] = dict(raw_capabilities)
        raw_asset_classes = capabilities.get("asset_classes")
        if raw_asset_classes is None:
            continue
        if not isinstance(raw_asset_classes, list):
            msg = f"brokers broker_id={broker_id} has non-list asset_classes"
            raise TypeError(msg)
        canonical_values = list(
            dict.fromkeys(
                _canonicalize(value, context=f"brokers broker_id={broker_id}")
                for value in raw_asset_classes
            )
        )
        broker_code = str(raw_code or "").strip().lower()
        if broker_code in _ETF_BROKERS and "etf" not in canonical_values:
            equity_index = (
                canonical_values.index("equity") + 1 if "equity" in canonical_values else 0
            )
            canonical_values.insert(equity_index, "etf")
        if broker_code == "paper":
            canonical_values = list(_PAPER_ASSET_CLASSES)
        if canonical_values != raw_asset_classes:
            capabilities["asset_classes"] = canonical_values
            bind.execute(
                sa.update(_BROKERS)
                .where(_BROKERS.c.broker_id == broker_id)
                .values(capabilities=capabilities)
            )


def _create_current_constraints() -> None:
    for table_name, constraint_name, condition in _CURRENT_CONSTRAINTS:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.create_check_constraint(constraint_name, condition)
    with op.batch_alter_table("instruments") as batch_op:
        batch_op.create_check_constraint(
            "ck_index_reference_only",
            "asset_class <> 'index' OR is_tradable = false",
        )


def upgrade() -> None:
    _require_normalizable_existing_data()
    _drop_constraints(_CONSTRAINTS_TO_DROP)
    with op.batch_alter_table("instruments") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_tradable",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
    _normalize_asset_class_columns()
    _reclassify_source_controlled_instruments()
    _normalize_binding_filters()
    _converge_broker_capabilities()
    _create_current_constraints()


def _assert_no_canonical_instrument_semantics_for_downgrade() -> None:
    bind = op.get_bind()
    canonical_instrument = bind.execute(
        sa.select(
            _INSTRUMENTS.c.instr_id,
            _INSTRUMENTS.c.canonical,
            _INSTRUMENTS.c.asset_class,
            _INSTRUMENTS.c.is_tradable,
        )
        .where(_INSTRUMENTS.c.asset_class.in_(("etf", "index")))
        .limit(1)
    ).first()
    if canonical_instrument is None:
        return
    msg = (
        "Cannot downgrade canonical asset taxonomy while instrument "
        f"{canonical_instrument.canonical!r} relies on "
        f"asset_class={canonical_instrument.asset_class!r} and explicit "
        f"is_tradable={canonical_instrument.is_tradable!r}; converting ETFs or "
        "cash indices back to tradable equities is unsafe."
    )
    raise RuntimeError(msg)


def _lock_downgrade_state() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE instruments, user_trading_policies, user_budget_buckets, "
        "sectors, market_scores, mode_performance, scoring_rules, strategies, "
        "execution_metrics, backtest_results IN ACCESS EXCLUSIVE MODE"
    )


def _require_legacy_compatible_data() -> None:
    bind = op.get_bind()
    for table_name, allowed in _LEGACY_ALLOWED_BY_TABLE.items():
        invalid = bind.execute(
            sa.text(
                f"SELECT asset_class FROM {table_name} "
                "WHERE asset_class IS NOT NULL "
                "AND asset_class NOT IN :allowed LIMIT 1"
            ).bindparams(sa.bindparam("allowed", expanding=True)),
            {"allowed": sorted(allowed)},
        ).scalar_one_or_none()
        if invalid is not None:
            msg = (
                f"Cannot downgrade asset taxonomy while {table_name} contains "
                f"asset_class={invalid!r}"
            )
            raise RuntimeError(msg)


def downgrade() -> None:
    _lock_downgrade_state()
    _assert_no_canonical_instrument_semantics_for_downgrade()
    _require_legacy_compatible_data()

    with op.batch_alter_table("instruments") as batch_op:
        batch_op.drop_constraint("ck_index_reference_only", type_="check")
    _drop_constraints(
        tuple(
            (table_name, constraint_name) for table_name, constraint_name, _ in _CURRENT_CONSTRAINTS
        )
    )
    with op.batch_alter_table("instruments") as batch_op:
        batch_op.drop_column("is_tradable")

    legacy_sql = {
        table_name: "asset_class IN (" + ", ".join(f"'{value}'" for value in sorted(values)) + ")"
        for table_name, values in _LEGACY_ALLOWED_BY_TABLE.items()
    }
    for table_name, constraint_name in _LEGACY_CONSTRAINTS:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.create_check_constraint(
                constraint_name,
                legacy_sql[table_name],
            )
