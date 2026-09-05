"""Round-trip checks for the per-domain models split.

After the May 2026 split (PR D + PR E) the ORM classes live in twelve
submodules under ``lib_application.db.models.{identity,consents,...,
feedback}`` and are re-exported from ``models/__init__.py``. Alembic's
``target_metadata = Base.metadata`` must still see every class so
``alembic revision --autogenerate`` doesn't silently miss tables.

These tests fail loudly if a future submodule is added but not wired
into the ``__init__`` re-exports, or if a class moves out of the shared
``Base.metadata`` registry by accident.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError

from lib_application.db.models import (
    Base,
    Broker,
    CanonicalSignal,
    Instrument,
    LinkedBrokerAccount,
    Order,
    OrderIntent,
    PendingOrder,
    Strategy,
    User,
    UserStrategyBinding,
)

_ROOT = Path(__file__).resolve().parents[1]
_FX_MIGRATION_PATH = _ROOT / "scripts" / "db" / "alembic" / "versions" / "0054_observed_fx_rates.py"


def test_all_revision_ids_fit_alembic_version_column() -> None:
    config = Config()
    config.set_main_option("script_location", str(_ROOT / "scripts" / "db" / "alembic"))
    revisions = ScriptDirectory.from_config(config).walk_revisions()

    overlong = sorted(revision.revision for revision in revisions if len(revision.revision) > 32)
    assert not overlong, f"Alembic revision IDs exceed varchar(32): {overlong}"


# Tables we expect to be discoverable from ``Base.metadata`` after the split.
# A subset is fine — we just want to catch silent drops, not freeze the schema.
EXPECTED_TABLES_BY_SECTION = {
    "A_identity": {"users"},
    "B_consents": {
        "user_consents",
        "suitability_questionnaires",
        "user_suitability_responses",
    },
    "C_brokers": {
        "brokers",
        "broker_environments",
        "linked_broker_accounts",
        "broker_credentials",
    },
    "D_instruments": {
        "instruments",
        "instrument_broker_symbols",
        "instrument_aliases",
        "market_calendars",
        "market_sessions",
        "prices",
        "watermarks",
    },
    "E_strategies": {
        "strategies",
        "strategy_decisions",
        "strategy_runtime_states",
        "strategy_versions",
        "user_strategy_configs",
    },
    "G_control_plane": {
        "user_trading_policies",
        "user_budget_buckets",
    },
    "H_options_templates": {
        "option_strategy_templates",
        "option_strategy_template_legs",
        "user_option_presets",
    },
    "I_oms": {
        "order_intents",
        "orders",
        "child_orders",
        "option_order_legs",
        "executions",
        "positions",
        "daily_nav",
        "execution_logs",
        "execution_metrics",
    },
    "J_risk_audit": {
        "risk_mandates",
        "risk_breaches",
        "api_audit_logs",
        "user_notifications",
        "outbound_webhooks",
        "feature_flags",
        "user_feature_flags",
    },
    "K_scoring": {
        "sectors",
        "instrument_sectors",
        "canonical_signals",
        "asset_scores",
        "sector_scores",
        "market_scores",
        "sizing_profiles",
        "user_strategy_bindings",
        "mode_performance",
        "scoring_rules",
        "execution_decision_logs",
        "pending_orders",
        "outbox_events",
    },
    "L_feedback": {
        "signal_performance",
        "strategy_parameter_feedback",
        "backtest_experiments",
        "backtest_results",
        "backtest_trials",
        "strategy_consecutive_wrong_tracker",
    },
    "O_equity_evidence": {
        "equity_security_identities",
        "equity_factor_evidence",
        "equity_factor_snapshot_details",
        "equity_factor_snapshots",
        "equity_observation_values",
        "equity_observations",
        "equity_rank_snapshot_rows",
        "equity_rank_snapshots",
        "equity_source_lineages",
    },
    "P_strategy_panels": {
        "strategy_panel_decisions",
        "strategy_panel_input_revisions",
    },
    "Q_portfolio_rebalances": {
        "account_execution_generations",
        "account_rebalance_plan_legs",
        "account_rebalance_plan_resolutions",
        "account_rebalance_plans",
        "model_rebalance_legs",
        "model_rebalances",
    },
}

RETIRED_SCHEMA_TABLES = {
    "trade_signals",
    "decision_runs",
    "decision_run_members",
    "opportunities",
    "opportunity_methods",
    "opportunity_explanations",
    "user_opportunity_subscriptions",
    "opp_sub_execution_bindings",
    "strategy_coverage",
    "options_positions",
}


def test_metadata_contains_all_split_section_tables() -> None:
    """Every table from every submodule must be in ``Base.metadata.tables``.

    If this fails, a submodule was added without re-exporting from
    ``models/__init__.py`` (Alembic autogenerate would silently skip it).
    """
    metadata_table_names = set(Base.metadata.tables.keys())
    missing: dict[str, set[str]] = {}
    for section, expected in EXPECTED_TABLES_BY_SECTION.items():
        gaps = expected - metadata_table_names
        if gaps:
            missing[section] = gaps
    assert not missing, (
        "Tables missing from Base.metadata after the models split — "
        "did you forget to re-export from models/__init__.py?\n"
        f"{missing}"
    )


def test_control_plane_models_keep_public_export_and_metadata_identity() -> None:
    from lib_application.db import models as exported_models
    from lib_application.db.models.control_plane import (
        SizingProfile as ControlPlaneSizingProfile,
    )
    from lib_application.db.models.control_plane import (
        UserStrategyBinding as ControlPlaneUserStrategyBinding,
    )

    assert exported_models.SizingProfile is ControlPlaneSizingProfile
    assert exported_models.UserStrategyBinding is ControlPlaneUserStrategyBinding
    assert Base.metadata.tables["sizing_profiles"] is ControlPlaneSizingProfile.__table__
    assert (
        Base.metadata.tables["user_strategy_bindings"] is ControlPlaneUserStrategyBinding.__table__
    )


def test_metadata_excludes_retired_decision_and_opportunity_schema() -> None:
    metadata_table_names = set(Base.metadata.tables)

    assert RETIRED_SCHEMA_TABLES.isdisjoint(metadata_table_names)
    assert "opp_id" not in Base.metadata.tables["order_intents"].c
    assert "user_budget_buckets" in metadata_table_names
    assert {
        "quantity_unit",
        "contract_multiplier",
        "gross_notional",
        "notional_currency",
    } <= set(Base.metadata.tables["positions"].c.keys())


def test_metadata_create_all_succeeds_on_sqlite() -> None:
    """``create_all`` is the same code path Alembic uses to apply migrations."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    actual_tables = set(inspector.get_table_names())

    expected = set().union(*EXPECTED_TABLES_BY_SECTION.values())
    missing_after_create = expected - actual_tables
    assert not missing_after_create, (
        "Tables present in metadata but not created on the engine — "
        "this usually means a class missed inheriting Base.\n"
        f"missing: {missing_after_create}"
    )


def test_metadata_drop_all_round_trips_cleanly() -> None:
    """``create_all`` + ``drop_all`` must leave no stragglers behind."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Base.metadata.drop_all(engine)
    inspector = inspect(engine)
    assert inspector.get_table_names() == []


def test_cross_section_relationships_resolve() -> None:
    """``relationship('OtherClass')`` strings must resolve via ``Base.registry``.

    The split spreads ``OptionStrategyTemplate`` (Section H) and
    ``OptionStrategyTemplateLeg`` (Section H) across files; SQLAlchemy
    only resolves the ``relationship`` strings lazily on first use. This
    test forces resolution by configuring all mappers.
    """
    from sqlalchemy.orm import configure_mappers

    # Will raise ``InvalidRequestError`` if any string-named mapper target
    # is not in the shared registry.
    configure_mappers()


def test_account_owned_rows_reject_another_users_broker_account() -> None:
    """The database, not only API validation, enforces broker-account ownership."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            User.__table__.insert(),
            [
                {"user_id": "owner", "email": "owner@example.invalid", "base_ccy": "USD"},
                {"user_id": "other", "email": "other@example.invalid", "base_ccy": "USD"},
            ],
        )
        broker_id = connection.execute(
            Broker.__table__.insert()
            .values(code="paper", name="Local paper", capabilities={})
            .returning(Broker.broker_id)
        ).scalar_one()
        account_id = connection.execute(
            LinkedBrokerAccount.__table__.insert()
            .values(
                user_id="owner",
                broker_id=broker_id,
                environment="paper",
                display_name="Owner paper account",
                base_ccy="USD",
                paper_initial_equity=Decimal("100000"),
                paper_initial_cash=Decimal("100000"),
            )
            .returning(LinkedBrokerAccount.account_id)
        ).scalar_one()

        with pytest.raises(IntegrityError):
            connection.execute(
                OrderIntent.__table__.insert().values(
                    user_id="other",
                    account_id=account_id,
                    method="SPOT",
                    payload={},
                    status="created",
                )
            )


def test_execution_chain_and_active_binding_are_account_consistent() -> None:
    """Composite FKs and the partial unique index enforce account identity."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            User.__table__.insert(),
            [
                {"user_id": "owner", "email": "owner@example.invalid", "base_ccy": "USD"},
                {"user_id": "other", "email": "other@example.invalid", "base_ccy": "USD"},
            ],
        )
        connection.execute(
            Strategy.__table__.insert().values(
                strategy_id="strategy-1",
                strategy_name="Strategy One",
            )
        )
        connection.execute(
            Instrument.__table__.insert().values(
                instr_id=1,
                asset_class="crypto",
                canonical="BTC-USD",
                settlement_currency="USD",
            )
        )
        connection.execute(
            CanonicalSignal.__table__.insert().values(
                signal_id=1,
                strategy_id="strategy-1",
                instr_id=1,
                action="long",
                external_signal_id="round-trip-signal-1",
                ts=datetime(2026, 7, 25, tzinfo=UTC),
            )
        )
        broker_id = connection.execute(
            Broker.__table__.insert()
            .values(code="paper", name="Local paper", capabilities={})
            .returning(Broker.broker_id)
        ).scalar_one()
        owner_account_id = connection.execute(
            LinkedBrokerAccount.__table__.insert()
            .values(
                user_id="owner",
                broker_id=broker_id,
                environment="paper",
                display_name="Owner primary",
                base_ccy="USD",
                paper_initial_equity=Decimal("100000"),
                paper_initial_cash=Decimal("100000"),
            )
            .returning(LinkedBrokerAccount.account_id)
        ).scalar_one()
        owner_second_account_id = connection.execute(
            LinkedBrokerAccount.__table__.insert()
            .values(
                user_id="owner",
                broker_id=broker_id,
                environment="paper",
                display_name="Owner secondary",
                base_ccy="USD",
                paper_initial_equity=Decimal("100000"),
                paper_initial_cash=Decimal("100000"),
            )
            .returning(LinkedBrokerAccount.account_id)
        ).scalar_one()
        other_account_id = connection.execute(
            LinkedBrokerAccount.__table__.insert()
            .values(
                user_id="other",
                broker_id=broker_id,
                environment="paper",
                display_name="Other account",
                base_ccy="USD",
                paper_initial_equity=Decimal("100000"),
                paper_initial_cash=Decimal("100000"),
            )
            .returning(LinkedBrokerAccount.account_id)
        ).scalar_one()
        intent_id = connection.execute(
            OrderIntent.__table__.insert()
            .values(
                user_id="owner",
                account_id=owner_account_id,
                strategy_id="strategy-1",
                canonical_signal_id=1,
                side="BUY",
                execution_mode="spot",
                broker_environment="paper",
                method="SPOT",
                payload={"symbol": "BTC-USD"},
                status="created",
            )
            .returning(OrderIntent.intent_id)
        ).scalar_one()

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            Order.__table__.insert().values(
                intent_id=intent_id,
                broker_id=broker_id,
                account_id=other_account_id,
                settlement_currency="USD",
                state="new",
            )
        )

    with engine.begin() as connection:
        canonical_order_id = connection.execute(
            Order.__table__.insert()
            .values(
                intent_id=intent_id,
                broker_id=broker_id,
                account_id=owner_account_id,
                settlement_currency="USD",
                state="new",
            )
            .returning(Order.order_id)
        ).scalar_one()

    pending_values = {
        "order_id": "pending-other",
        "user_id": "other",
        "signal_id": "signal-1",
        "broker_account_id": other_account_id,
        "canonical_order_id": canonical_order_id,
        "symbol": "BTC-USD",
        "settlement_currency": "USD",
        "side": "BUY",
        "order_type": "market",
        "quantity": Decimal("1"),
        "execution_mode": "spot",
        "broker": "paper",
        "status": "pending",
    }
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(PendingOrder.__table__.insert().values(**pending_values))

    binding_values = {
        "user_id": "owner",
        "strategy_id": "strategy-1",
        "broker_account_id": owner_account_id,
        "asset_score_threshold": Decimal("0.6"),
        "execution_modes_allowed": ["spot"],
        "mode_selection_policy": "fixed",
        "asset_classes_allowed": ["crypto"],
        "max_position_pct": Decimal("0.1"),
        "max_daily_loss_pct": Decimal("0.05"),
        "max_open_positions": 10,
        "is_active": True,
        "autopilot": True,
    }
    with engine.begin() as connection:
        connection.execute(UserStrategyBinding.__table__.insert().values(**binding_values))
        connection.execute(
            UserStrategyBinding.__table__.insert().values(
                **{**binding_values, "broker_account_id": owner_second_account_id}
            )
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(UserStrategyBinding.__table__.insert().values(**binding_values))


def _load_fx_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("observed_fx_rates_migration", _FX_MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_pre_fx_schema(engine: sa.Engine) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    users = sa.Table(
        "users",
        metadata,
        sa.Column("user_id", sa.String(50), primary_key=True),
        sa.Column("base_ccy", sa.String(10), nullable=True),
    )
    brokers = sa.Table(
        "brokers",
        metadata,
        sa.Column("broker_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(50), nullable=False, unique=True),
    )
    accounts = sa.Table(
        "linked_broker_accounts",
        metadata,
        sa.Column("account_id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(50), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("base_ccy", sa.String(10), nullable=True, server_default="USD"),
    )
    instruments = sa.Table(
        "instruments",
        metadata,
        sa.Column("instr_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("asset_class", sa.String(20), nullable=False),
        sa.Column("canonical", sa.String(50), nullable=False),
        sa.Column("exchange", sa.String(50)),
        sa.Column("settlement_currency", sa.String(10), nullable=False),
        sa.Column("tick_size", sa.Numeric(20, 8)),
        sa.Column("lot_size", sa.Numeric(20, 8)),
    )
    aliases = sa.Table(
        "instrument_aliases",
        metadata,
        sa.Column("alias_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "instr_id",
            sa.Integer,
            sa.ForeignKey("instruments.instr_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(100), nullable=False, unique=True),
        sa.Column("source", sa.String(50)),
    )
    mappings = sa.Table(
        "instrument_broker_symbols",
        metadata,
        sa.Column(
            "instr_id",
            sa.Integer,
            sa.ForeignKey("instruments.instr_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "broker_id",
            sa.Integer,
            sa.ForeignKey("brokers.broker_id"),
            primary_key=True,
        ),
        sa.Column("broker_symbol", sa.String(100), nullable=False),
    )
    metadata.create_all(engine)
    return {
        "users": users,
        "brokers": brokers,
        "accounts": accounts,
        "instruments": instruments,
        "aliases": aliases,
        "mappings": mappings,
    }


def _run_fx_revision(engine: sa.Engine, operation: str) -> None:
    migration = _load_fx_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def test_observed_fx_migration_round_trips_data_and_constraints() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    tables = _create_pre_fx_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            tables["users"].insert(),
            [
                {"user_id": "eur-user", "base_ccy": " eur "},
                {"user_id": "default-user", "base_ccy": ""},
            ],
        )
        connection.execute(
            tables["accounts"].insert(),
            [
                {"account_id": 1, "user_id": "eur-user", "base_ccy": " inr "},
                {"account_id": 2, "user_id": "default-user", "base_ccy": None},
            ],
        )
        connection.execute(tables["brokers"].insert().values(broker_id=1, code="coinbase"))
        connection.execute(
            tables["instruments"]
            .insert()
            .values(
                instr_id=10,
                asset_class="fx",
                canonical="eurusd",
                exchange="legacy",
                settlement_currency="usd",
                tick_size=Decimal("0.1"),
                lot_size=Decimal("10"),
            )
        )

    with pytest.raises(RuntimeError, match="requires explicit values"):
        _run_fx_revision(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(
            tables["users"]
            .update()
            .where(tables["users"].c.user_id == "default-user")
            .values(base_ccy=" usd ")
        )
        connection.execute(
            tables["accounts"]
            .update()
            .where(tables["accounts"].c.account_id == 2)
            .values(base_ccy=" usd ")
        )

    _run_fx_revision(engine, "upgrade")

    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT user_id, base_ccy FROM users ORDER BY user_id")
        ).all() == [("default-user", "USD"), ("eur-user", "EUR")]
        assert connection.execute(
            sa.text("SELECT account_id, base_ccy FROM linked_broker_accounts ORDER BY account_id")
        ).all() == [(1, "INR"), (2, "USD")]
        assert connection.execute(
            sa.text(
                """
                SELECT instr_id, canonical, exchange, settlement_currency
                FROM instruments
                WHERE asset_class = 'fx'
                ORDER BY canonical
                """
            )
        ).all() == [
            (15, "EUR/GBP", "ecb", "GBP"),
            (14, "EUR/INR", "ecb", "INR"),
            (10, "EUR/USD", "ecb", "USD"),
            (16, "USDC/EUR", "coinbase", "EUR"),
        ]
        assert connection.execute(
            sa.text("SELECT alias FROM instrument_aliases ORDER BY alias")
        ).scalars().all() == ["EURGBP", "EURINR", "EURUSD", "USDCEUR"]
        assert connection.execute(
            sa.text(
                """
                SELECT instruments.canonical, instrument_broker_symbols.broker_symbol
                FROM instrument_broker_symbols
                JOIN instruments USING (instr_id)
                """
            )
        ).one() == ("USDC/EUR", "USDC-EUR")

    upgraded_inspector = inspect(engine)
    for table in ("users", "linked_broker_accounts"):
        base_ccy = next(
            column
            for column in upgraded_inspector.get_columns(table)
            if column["name"] == "base_ccy"
        )
        assert base_ccy["nullable"] is False
        assert base_ccy["default"] is None
    assert {item["name"] for item in upgraded_inspector.get_check_constraints("users")} >= {
        "ck_user_base_ccy"
    }
    assert {
        item["name"] for item in upgraded_inspector.get_check_constraints("linked_broker_accounts")
    } >= {"ck_account_base_ccy"}

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(tables["users"].insert().values(user_id="lowercase", base_ccy="usd"))

    _run_fx_revision(engine, "downgrade")

    downgraded_inspector = inspect(engine)
    user_base_ccy = next(
        column
        for column in downgraded_inspector.get_columns("users")
        if column["name"] == "base_ccy"
    )
    account_base_ccy = next(
        column
        for column in downgraded_inspector.get_columns("linked_broker_accounts")
        if column["name"] == "base_ccy"
    )
    assert user_base_ccy["nullable"] is True
    assert user_base_ccy["default"] is None
    assert account_base_ccy["nullable"] is True
    assert "USD" in str(account_base_ccy["default"])
    with engine.begin() as connection:
        connection.execute(tables["users"].insert().values(user_id="nullable", base_ccy=None))
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM instruments WHERE asset_class = 'fx'")
            ).scalar_one()
            == 4
        )

    with pytest.raises(RuntimeError, match="1 user row"):
        _run_fx_revision(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(
            tables["users"]
            .update()
            .where(tables["users"].c.user_id == "nullable")
            .values(base_ccy="USD")
        )

    _run_fx_revision(engine, "upgrade")
    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM instruments WHERE asset_class = 'fx'")
            ).scalar_one()
            == 4
        )
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM instrument_aliases WHERE source = 'fx_reference'")
            ).scalar_one()
            == 4
        )


def test_observed_fx_migration_rejects_cross_asset_symbol_collision() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    tables = _create_pre_fx_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            tables["instruments"]
            .insert()
            .values(
                asset_class="crypto",
                canonical="EURUSD",
                exchange="conflicting",
                settlement_currency="USD",
                tick_size=Decimal("0.01"),
                lot_size=Decimal("1"),
            )
        )

    migration = _load_fx_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="conflicts with required FX symbol"):
            migration._ensure_fx_catalogue()
