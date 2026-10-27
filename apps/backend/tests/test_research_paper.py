"""Paper adaptations preserve established owner, release and accounting gates."""

from __future__ import annotations

from backend.api import create_app
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.backend.tests import test_ui_api
from lib_application.db.models import ApiAuditLog, BrokerEnvironment, LinkedBrokerAccount

factory = test_ui_api.factory

AUTH = {"X-Admin-Key": "paper-ui-test"}
PREFIX = "/api/ui/research/paper"


def client(factory):
    return TestClient(
        create_app(session_factory=factory, admin_api_key=AUTH["X-Admin-Key"], allow_anon=False)
    )


def test_paper_view_preserves_owner_scope_and_real_fill_currency(factory):
    response = client(factory).get(PREFIX, headers=AUTH)
    assert response.status_code == 200, response.text
    value = response.json()
    assert [row["account_id"] for row in value["control"]["accounts"]] == [1]
    assert [row["account_id"] for row in value["pnl"]["accounts"]] == [1]
    assert all(row["account_id"] == 1 for row in value["fills"]["fills"])
    assert all(row["currency"] == "USDC" for row in value["fills"]["fills"])


def test_paper_account_creation_retries_do_not_duplicate_or_replace_terms(factory):
    with factory() as session:
        session.add(
            BrokerEnvironment(broker_id=6, environment="paper", region="global", base_urls={})
        )
        session.commit()
    browser = client(factory)
    body = {
        "config_key": "ui-component",
        "display_name": "UI component",
        "base_ccy": "EUR",
        "paper_initial_equity": "1000",
        "paper_initial_cash": "1000",
    }
    first = browser.post(PREFIX + "/accounts", json=body, headers=AUTH)
    assert first.status_code == 200, first.text
    second = browser.post(PREFIX + "/accounts", json=body, headers=AUTH)
    assert second.status_code == 200, second.text
    assert first.json()["account"]["account_id"] == second.json()["account"]["account_id"]
    changed = browser.post(
        PREFIX + "/accounts", json={**body, "paper_initial_equity": "2000"}, headers=AUTH
    )
    assert changed.status_code == 409
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(LinkedBrokerAccount)
                .where(LinkedBrokerAccount.config_key == "ui-component")
            )
            == 1
        )


def test_paper_cannot_change_live_or_other_owner_accounts(factory):
    with factory() as session:
        session.add(
            LinkedBrokerAccount(
                account_id=3,
                user_id="owner",
                broker_id=6,
                environment="live",
                display_name="Excluded live boundary",
                base_ccy="EUR",
            )
        )
        session.commit()
    browser = client(factory)
    for account_id in (2, 3):
        response = browser.post(
            PREFIX + "/bindings",
            headers=AUTH,
            json={"strategy_id": "swing_v1", "broker_account_id": account_id, "mode": "trading"},
        )
        assert response.status_code == 404, response.text
    assert [
        row["account_id"] for row in browser.get(PREFIX, headers=AUTH).json()["control"]["accounts"]
    ] == [1]


def test_refused_paper_binding_keeps_the_existing_error_audit_contract(factory):
    response = client(factory).post(
        PREFIX + "/bindings",
        headers=AUTH,
        json={"strategy_id": "ported_v1", "broker_account_id": 1, "mode": "trading"},
    )
    assert response.status_code == 409, response.text
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ApiAuditLog)
                .where(ApiAuditLog.action == "binding.create", ApiAuditLog.status == "error")
            )
            == 1
        )
