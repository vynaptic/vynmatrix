"""Tests for ``vmdev user`` CLI commands."""

from __future__ import annotations

import importlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

user_module = importlib.import_module("dev_cli.commands.user")

from lib_application.db.models import (
    Base,
    Broker,
    BrokerEnvironment,
    LinkedBrokerAccount,
    Organization,
    Plan,
    User,
    UserPlanSubscription,
    UserRole,
    UserTradingPolicy,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session() -> Session:
    """Build an in-memory SQLite session with the full ORM schema."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def seeded_session(db_session: Session) -> Session:
    """Pre-seed plans and brokers so the user-add path can resolve them."""
    db_session.add_all(
        [
            Plan(code="free", features={"max_strategies": 2}),
            Plan(code="starter", features={"max_strategies": 5}),
            Plan(code="pro", features={"max_strategies": 20}),
        ]
    )
    coinbase = Broker(code="coinbase", name="Coinbase", capabilities={})
    db_session.add(coinbase)
    db_session.flush()
    db_session.add(
        BrokerEnvironment(
            broker_id=coinbase.broker_id,
            environment="paper",
            region="US",
            base_urls={"rest": "https://example.test"},
        )
    )
    db_session.commit()
    return db_session


def _user_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "email": "alice@example.com",
        "full_name": "Alice Example",
        "timezone": "UTC",
        "base_currency": "USD",
        "plan": "free",
        "trading_policy": {
            "asset_class": "crypto",
            "horizon": "swing",
            "default_method": "SPOT",
        },
        "broker": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _load_config
# ---------------------------------------------------------------------------


def test_load_config_reads_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "users.yaml"
    config_path.write_text(
        "users:\n  - email: user@example.invalid\n    full_name: Example User\n    plan: free\n",
        encoding="utf-8",
    )
    config = user_module._load_config(str(config_path))
    assert config["users"][0]["email"] == "user@example.invalid"
    assert config["users"][0]["plan"] == "free"


def test_load_config_exits_on_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(SystemExit) as excinfo:
        user_module._load_config(str(missing))
    assert excinfo.value.code == 1


# ---------------------------------------------------------------------------
# _add_user_to_db — happy paths and idempotency
# ---------------------------------------------------------------------------


def test_add_user_to_db_creates_org_user_role_and_policy(
    seeded_session: Session,
) -> None:
    payload = _user_payload()
    user_id = user_module._add_user_to_db(payload, seeded_session)

    # ``user_id`` is the canonical UUID-form string the column was
    # designed to hold (it gained a ``generate_uuid`` default during
    # this PR's bugfix).
    assert isinstance(user_id, str)
    assert len(user_id) > 0

    user = seeded_session.get(User, user_id)
    assert user is not None
    assert user.email == "alice@example.com"
    assert user.full_name == "Alice Example"
    assert user.tz == "UTC"
    assert user.base_ccy == "USD"
    assert user.status == "active"

    # Default org auto-created.
    org = seeded_session.query(Organization).first()
    assert org is not None
    assert user.org_id == org.org_id

    # ``trader`` role assigned.
    role = seeded_session.query(UserRole).filter_by(user_id=user_id).first()
    assert role is not None
    assert role.role == "trader"

    # Plan subscription points at the seeded ``free`` plan.
    sub = seeded_session.query(UserPlanSubscription).filter_by(user_id=user_id).first()
    assert sub is not None
    assert sub.status == "active"

    # Trading policy mirrors the input payload.
    policy = seeded_session.query(UserTradingPolicy).filter_by(user_id=user_id).first()
    assert policy is not None
    assert policy.asset_class == "crypto"
    assert policy.horizon == "swing"
    assert policy.default_method == "SPOT"
    assert policy.methods_allowed == ["SPOT"]


def test_add_user_normalizes_asset_class_alias_at_write_boundary(
    seeded_session: Session,
) -> None:
    payload = _user_payload(
        email="fx@example.com",
        trading_policy={
            "asset_class": "forex",
            "horizon": "intraday",
            "default_method": "SPOT",
        },
    )

    user_id = user_module._add_user_to_db(payload, seeded_session)

    policy = seeded_session.query(UserTradingPolicy).filter_by(user_id=user_id).one()
    assert policy.asset_class == "fx"


def test_add_user_rejects_unknown_asset_class_before_commit(
    seeded_session: Session,
) -> None:
    payload = _user_payload(
        trading_policy={
            "asset_class": "stock",
            "horizon": "swing",
            "default_method": "SPOT",
        }
    )

    with pytest.raises(ValueError, match=r"Unsupported trading_policy\.asset_class"):
        user_module._add_user_to_db(payload, seeded_session)

    seeded_session.rollback()
    assert seeded_session.query(User).count() == 0


def test_add_user_to_db_is_idempotent_on_email(seeded_session: Session) -> None:
    """Duplicate email returns the existing ``user_id`` instead of creating again."""
    payload = _user_payload()

    first_id = user_module._add_user_to_db(payload, seeded_session)
    second_id = user_module._add_user_to_db(payload, seeded_session)

    assert first_id == second_id
    # Only one user row exists for that email.
    rows = seeded_session.query(User).filter_by(email=payload["email"]).all()
    assert len(rows) == 1
    # And only one role / policy / subscription row each.
    assert seeded_session.query(UserRole).filter_by(user_id=first_id).count() == 1
    assert seeded_session.query(UserTradingPolicy).filter_by(user_id=first_id).count() == 1
    assert seeded_session.query(UserPlanSubscription).filter_by(user_id=first_id).count() == 1


def test_add_user_to_db_attaches_broker_account_when_configured(
    seeded_session: Session,
) -> None:
    """Broker payload should create a ``LinkedBrokerAccount`` row."""
    payload = _user_payload(
        email="bob@example.com",
        full_name="Bob Example",
        broker={
            "broker": "coinbase",
            "display_name": "My Coinbase",
            "environment": "paper",
            "base_currency": "USD",
            "paper_initial_equity": "10000",
            "paper_initial_cash": "8000",
        },
    )
    user_id = user_module._add_user_to_db(payload, seeded_session)

    account = seeded_session.query(LinkedBrokerAccount).filter_by(user_id=user_id).first()
    assert account is not None
    assert account.display_name == "My Coinbase"
    # Status is ``connected`` because the schema's CHECK constraint only
    # allows ``connected``/``revoked``/``error`` — see the inline comment
    # in ``_add_user_to_db``.
    assert account.status == "connected"
    assert account.environment == "paper"
    assert account.base_ccy == "USD"
    assert account.paper_initial_equity == Decimal("10000")
    assert account.paper_initial_cash == Decimal("8000")


@pytest.mark.parametrize("base_currency", [None, "", "usd", "US", "USD-EUR"])
def test_add_user_requires_canonical_base_currency(
    seeded_session: Session,
    base_currency: str | None,
) -> None:
    with pytest.raises(ValueError, match="base_currency"):
        user_module._add_user_to_db(
            _user_payload(base_currency=base_currency),
            seeded_session,
        )

    assert seeded_session.query(User).count() == 0


def test_add_user_requires_explicit_broker_account_currency(
    seeded_session: Session,
) -> None:
    payload = _user_payload(
        broker={
            "broker": "coinbase",
            "display_name": "My Coinbase",
            "environment": "paper",
        }
    )

    with pytest.raises(ValueError, match=r"broker\.base_currency"):
        user_module._add_user_to_db(payload, seeded_session)

    assert seeded_session.query(User).count() == 0


def test_add_user_requires_explicit_valid_paper_capital(seeded_session: Session) -> None:
    broker = {
        "broker": "coinbase",
        "display_name": "My Coinbase",
        "environment": "paper",
        "base_currency": "USD",
    }
    with pytest.raises(ValueError, match="initial equity and cash"):
        user_module._add_user_to_db(_user_payload(broker=broker), seeded_session)

    with pytest.raises(ValueError, match="between zero and initial equity"):
        user_module._add_user_to_db(
            _user_payload(
                broker={
                    **broker,
                    "paper_initial_equity": "10000",
                    "paper_initial_cash": "11000",
                }
            ),
            seeded_session,
        )


def test_add_user_to_db_skips_unknown_plan_silently(seeded_session: Session) -> None:
    """An unknown plan code skips the subscription row but still creates the user."""
    payload = _user_payload(email="carol@example.com", plan="enterprise-mega")
    user_id = user_module._add_user_to_db(payload, seeded_session)

    assert isinstance(user_id, str)
    assert user_id
    assert seeded_session.query(UserPlanSubscription).filter_by(user_id=user_id).count() == 0


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_user_group_help_lists_all_subcommands() -> None:
    """``vmdev user --help`` must advertise every public command."""
    result = CliRunner().invoke(user_module.user, ["--help"])
    assert result.exit_code == 0
    for subcommand in ("add", "list", "show"):
        assert subcommand in result.output


def test_user_add_help_documents_config_flag() -> None:
    result = CliRunner().invoke(user_module.user, ["add", "--help"])
    assert result.exit_code == 0
    assert "--config" in result.output
    assert "YAML" in result.output or "yaml" in result.output
