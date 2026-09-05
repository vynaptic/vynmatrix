"""Account-ownership boundaries for persisted drawdown history."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from execution_engine.metrics.drawdown_service import (
    DrawdownLedgerUnavailableError,
    DrawdownService,
)
from lib_application.db.models import (
    Base,
    Broker,
    DailyNav,
    LinkedBrokerAccount,
    User,
)


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine)
    with session_factory() as session:
        session.add_all(
            [
                User(user_id="u1", email="u1@example.com", base_ccy="EUR"),
                User(user_id="u2", email="u2@example.com", base_ccy="USD"),
                Broker(broker_id=1, code="paper", name="Paper", capabilities={}),
            ]
        )
        session.flush()
        session.add_all(
            [
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="u1",
                    broker_id=1,
                    environment="paper",
                    display_name="Primary EUR",
                    base_ccy="EUR",
                    status="connected",
                    paper_initial_equity=Decimal("100"),
                    paper_initial_cash=Decimal("100"),
                ),
                LinkedBrokerAccount(
                    account_id=2,
                    user_id="u1",
                    broker_id=1,
                    environment="paper",
                    display_name="Secondary EUR",
                    base_ccy="EUR",
                    status="connected",
                    paper_initial_equity=Decimal("1000"),
                    paper_initial_cash=Decimal("1000"),
                ),
                LinkedBrokerAccount(
                    account_id=3,
                    user_id="u2",
                    broker_id=1,
                    environment="paper",
                    display_name="Other tenant",
                    base_ccy="USD",
                    status="connected",
                    paper_initial_equity=Decimal("100"),
                    paper_initial_cash=Decimal("100"),
                ),
            ]
        )
        session.commit()
    return session_factory


def test_drawdown_history_isolated_by_broker_account() -> None:
    session_factory = _session_factory()
    prior_day = datetime.now(tz=UTC).date() - timedelta(days=1)
    with session_factory() as session:
        session.add_all(
            [
                DailyNav(
                    user_id="u1",
                    account_id=1,
                    date=prior_day,
                    nav_ccy="EUR",
                    nav_value=Decimal("100"),
                ),
                DailyNav(
                    user_id="u1",
                    account_id=2,
                    date=prior_day,
                    nav_ccy="EUR",
                    nav_value=Decimal("1000"),
                ),
            ]
        )
        session.commit()

    service = DrawdownService(session_factory=session_factory)
    snapshot = asyncio.run(
        service.get_drawdown_snapshot(
            user_id="u1",
            account_id=1,
            current_equity=Decimal("90"),
        )
    )

    assert snapshot.account_id == 1
    assert snapshot.peak_equity == Decimal("100")
    assert snapshot.current_drawdown_pct == pytest.approx(0.10)


def test_drawdown_reads_and_writes_validate_account_ownership_and_currency() -> None:
    session_factory = _session_factory()
    service = DrawdownService(session_factory=session_factory)

    with pytest.raises(DrawdownLedgerUnavailableError, match="unavailable for user u1"):
        asyncio.run(
            service.get_drawdown_snapshot(
                user_id="u1",
                account_id=3,
                current_equity=Decimal("100"),
            )
        )

    asyncio.run(
        service.update_drawdown(
            user_id="u1",
            account_id=1,
            equity=Decimal("95"),
            nav_ccy="EUR",
        )
    )
    with session_factory() as session:
        row = session.query(DailyNav).one()
        assert row.user_id == "u1"
        assert row.account_id == 1
        assert row.nav_ccy == "EUR"
        assert row.nav_value == Decimal("95")

    with pytest.raises(DrawdownLedgerUnavailableError, match="does not match broker account"):
        asyncio.run(
            service.update_drawdown(
                user_id="u1",
                account_id=1,
                equity=Decimal("90"),
                nav_ccy="USD",
            )
        )
