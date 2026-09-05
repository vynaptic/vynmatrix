"""Owner-relative configuration rejects caller-selected tenant authority."""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event
from sqlalchemy.orm import Session

from apps.backend.tests.test_api import AUTH
from apps.backend.tests.test_api import env as _backend_env  # noqa: F401
from lib_application.db.models import (
    ApiAuditLog,
    Broker,
    BrokerCredential,
    ExecutionLog,
    LinkedBrokerAccount,
    ManagedSecret,
    OutboxEvent,
    User,
)
from lib_application.services.account_onboarding import (
    AccountOnboardingError,
    BrokerAccountIn,
    BrokerCredentialIn,
    onboard_account,
    patch_account,
    rotate_credentials,
)
from lib_infrastructure.brokers import DbSecretsProvider


@pytest.fixture
def env(request: pytest.FixtureRequest) -> dict[str, Any]:
    return request.getfixturevalue("_backend_env")


def test_owner_relative_bindings_and_removed_user_paths(env: dict[str, Any]) -> None:
    client = env["client"]
    assert client.get("/bindings", headers=AUTH).status_code == 200
    assert client.get("/users/demo_user/bindings", headers=AUTH).status_code == 404
    assert client.get("/bindings?user_id=demo_peer", headers=AUTH).status_code == 422


def test_owner_profile_expected_value_patch_preserves_omitted_fields(env: dict[str, Any]) -> None:
    client = env["client"]
    response = client.get("/owner", headers=AUTH)
    assert response.status_code == 200
    before = response.json()
    changed = client.patch(
        "/owner",
        headers=AUTH,
        json={
            "expected": {"full_name": before["full_name"]},
            "changes": {"full_name": "Personal owner"},
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["full_name"] == "Personal owner"
    assert changed.json()["base_ccy"] == before["base_ccy"]
    stale = client.patch(
        "/owner",
        headers=AUTH,
        json={
            "expected": {"full_name": before["full_name"]},
            "changes": {"full_name": "Stale overwrite"},
        },
    )
    assert stale.status_code == 409


def test_account_key_retry_never_recreates_or_rotates_credentials(env: dict[str, Any]) -> None:
    payload = {
        "config_key": "primary",
        "broker_code": "paper",
        "environment": "paper",
        "base_ccy": "EUR",
        "paper_initial_equity": "10000",
        "paper_initial_cash": "10000",
    }
    client = env["client"]
    first = client.post("/broker-accounts", headers=AUTH, json=payload)
    assert first.status_code == 200, first.text
    repeated = client.post("/broker-accounts", headers=AUTH, json=payload)
    assert repeated.status_code == 200, repeated.text
    assert first.json()["account_id"] == repeated.json()["account_id"]
    assert first.json()["secret_ref"] == repeated.json()["secret_ref"] == ""
    assert repeated.json()["config_key"] == "primary"
    assert env["secrets"].store == {}
    with env["factory"]() as session:
        assert session.query(BrokerCredential).count() == 0
        assert session.query(ManagedSecret).count() == 0


def test_local_paper_onboarding_needs_no_secret_writer(env: dict[str, Any]) -> None:
    with env["factory"]() as session:
        result = onboard_account(session, BrokerAccountIn.model_validate(_paper_payload()), None)
        session.commit()
    assert result.secret_ref == ""
    with env["factory"]() as session:
        assert session.get(LinkedBrokerAccount, result.account_id).status == "connected"
        assert session.query(BrokerCredential).count() == 0
        assert session.query(ManagedSecret).count() == 0
        assert session.query(ApiAuditLog).one().action == "broker_account.create"


@pytest.mark.parametrize("secret", ["not-a-paper-credential", " "])
def test_local_paper_rejects_secret_fields_before_any_account_write(
    env: dict[str, Any], secret: str
) -> None:
    response = env["client"].post(
        "/broker-accounts",
        headers=AUTH,
        json=_paper_payload() | {"credentials": {"api_key": secret}},
    )
    assert response.status_code == 422
    with env["factory"]() as session:
        assert session.query(LinkedBrokerAccount).filter_by(config_key="primary").count() == 0
        assert session.query(BrokerCredential).count() == 0
        assert session.query(ApiAuditLog).count() == 0
    assert env["secrets"].store == {}


def test_legacy_local_paper_credentials_are_preserved_but_cannot_rotate(
    env: dict[str, Any],
) -> None:
    client = env["client"]
    account_id = 1
    provider = DbSecretsProvider(
        session_factory=env["factory"], master_keys=[Fernet.generate_key().decode()]
    )
    with env["factory"]() as session:
        account = session.get(LinkedBrokerAccount, account_id)
        account.broker_id = session.query(Broker).filter_by(code="paper").one().broker_id
        account.config_key = "primary"
        account.paper_initial_cash = account.paper_initial_equity = 10000
        session.add(
            BrokerCredential(account_id=account_id, secret_ref="legacy-paper", status="active")
        )
        session.flush()
        provider.set_secret("legacy-paper", "{}", account_id=account_id, session=session)
        session.commit()
        original_ciphertext = session.query(ManagedSecret).one().ciphertext
    retried = client.post("/broker-accounts", headers=AUTH, json=_paper_payload())
    assert retried.status_code == 200
    assert retried.json()["secret_ref"] == "legacy-paper"
    for credentials in ({}, {"api_key": "not-a-paper-credential"}):
        response = client.put(
            f"/broker-accounts/{account_id}/credentials", headers=AUTH, json=credentials
        )
        assert response.status_code == 409
    with env["factory"]() as session:
        assert session.query(BrokerCredential).one().secret_ref == "legacy-paper"
        assert session.query(ManagedSecret).one().ciphertext == original_ciphertext
        assert session.query(ApiAuditLog).count() == 0


@pytest.mark.parametrize("owner_status", ["missing", "suspended"])
def test_owner_routes_fail_closed_without_active_designation(
    env: dict[str, Any], owner_status: str
) -> None:
    with env["factory"]() as session:
        owner = session.get(User, "demo_user")
        if owner_status == "missing":
            owner.is_deployment_owner = False
        else:
            owner.status = owner_status
        session.commit()
    assert env["client"].get("/broker-accounts", headers=AUTH).status_code == 503
    assert env["client"].get("/owner", headers=AUTH).status_code == 503


@pytest.mark.parametrize("header", ["user_id", "user-id", "x-user-id"])
def test_caller_cannot_select_owner_in_headers(env: dict[str, Any], header: str) -> None:
    response = env["client"].get("/bindings", headers=AUTH | {header: "demo_peer"})
    assert response.status_code == 422


def test_body_owner_and_missing_stable_key_are_rejected(env: dict[str, Any]) -> None:
    client = env["client"]
    payload = _paper_payload()
    assert (
        client.post(
            "/broker-accounts",
            headers=AUTH,
            json=payload
            | {
                "user_id": "demo_peer",
            },
        ).status_code
        == 422
    )
    del payload["config_key"]
    assert client.post("/broker-accounts", headers=AUTH, json=payload).status_code == 422
    assert (
        client.post(
            "/bindings",
            headers=AUTH,
            json={
                "user_id": "demo_peer",
                "broker_account_id": 1,
            },
        ).status_code
        == 422
    )


def _paper_payload() -> dict[str, str]:
    return {
        "config_key": "primary",
        "broker_code": "paper",
        "environment": "paper",
        "base_ccy": "EUR",
        "paper_initial_equity": "10000",
        "paper_initial_cash": "10000",
    }


def test_key_retry_preserves_revocation_and_secret_snapshot(env: dict[str, Any]) -> None:
    client = env["client"]
    payload = _paper_payload()
    first = client.post("/broker-accounts", headers=AUTH, json=payload).json()
    with env["factory"]() as session:
        session.get(LinkedBrokerAccount, first["account_id"]).status = "revoked"
        session.commit()
    before = dict(env["secrets"].store)
    repeated = client.post(
        "/broker-accounts",
        headers=AUTH,
        json=payload
        | {
            "credentials": {"api_key": "must-not-rotate"},
        },
    )
    assert repeated.status_code == 422
    repeated = client.post("/broker-accounts", headers=AUTH, json=payload)
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "revoked"
    assert env["secrets"].store == before
    conflicting = client.post("/broker-accounts", headers=AUTH, json=payload | {"base_ccy": "USD"})
    assert conflicting.status_code == 409
    with env["factory"]() as session:
        assert session.query(LinkedBrokerAccount).count() == 2
        assert session.query(BrokerCredential).count() == 0
        assert session.query(ApiAuditLog).filter_by(action="broker_account.create").count() == 1


def test_explicit_adoption_preserves_identity_and_rejects_other_owner(env: dict[str, Any]) -> None:
    client = env["client"]
    with env["factory"]() as session:
        session.add(
            LinkedBrokerAccount(
                account_id=2,
                user_id="demo_peer",
                broker_id=1,
                environment="live",
                display_name="Historical peer",
                external_ref="legacy-account",
                base_ccy="INR",
            )
        )
        session.commit()
    adopted = client.post("/broker-accounts/1/adopt", headers=AUTH, json={"config_key": "legacy"})
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["account_id"] == 1
    assert adopted.json()["paper_initial_cash"] == "100000.00000000"
    repeated = client.post("/broker-accounts/1/adopt", headers=AUTH, json={"config_key": "legacy"})
    assert repeated.status_code == 200
    assert (
        client.post(
            "/broker-accounts/1/adopt",
            headers=AUTH,
            json={
                "config_key": "renamed",
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/broker-accounts/2/adopt",
            headers=AUTH,
            json={
                "config_key": "peer",
            },
        ).status_code
        == 404
    )
    assert (
        client.patch(
            "/broker-accounts/2",
            headers=AUTH,
            json={
                "expected": {"display_name": "Historical peer"},
                "changes": {"display_name": "Moved"},
            },
        ).status_code
        == 404
    )
    assert (
        client.put(
            "/broker-accounts/2/credentials",
            headers=AUTH,
            json={
                "api_key": "not-owner",
                "api_secret": "not-owner",
            },
        ).status_code
        == 404
    )
    assert [row["account_id"] for row in client.get("/broker-accounts", headers=AUTH).json()] == [1]
    with env["factory"]() as session:
        assert session.query(ApiAuditLog).filter_by(action="broker_account.adopt").count() == 1
        assert session.get(LinkedBrokerAccount, 2).config_key is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", "demo_peer"),
        ("broker_id", 2),
        ("environment", "live"),
        ("account_id", 2),
        ("config_key", "renamed"),
    ],
)
def test_account_patch_forbids_identity_reassignment(
    env: dict[str, Any], field: str, value: Any
) -> None:
    response = env["client"].patch(
        "/broker-accounts/1",
        headers=AUTH,
        json={
            "expected": {field: None},
            "changes": {field: value},
        },
    )
    assert response.status_code == 422


def test_account_patch_requires_expected_values_and_preserves_omitted_fields(
    env: dict[str, Any],
) -> None:
    client = env["client"]
    path = "/broker-accounts/1"
    assert (
        client.patch(
            path,
            headers=AUTH,
            json={
                "expected": {},
                "changes": {"display_name": "Reviewed"},
            },
        ).status_code
        == 422
    )
    assert (
        client.patch(
            path,
            headers=AUTH,
            json={
                "expected": {"display_name": "stale"},
                "changes": {"display_name": "Reviewed"},
            },
        ).status_code
        == 409
    )
    response = client.patch(
        path,
        headers=AUTH,
        json={
            "expected": {"display_name": "Demo User paper"},
            "changes": {"display_name": "Reviewed"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["base_ccy"] == "EUR"
    assert response.json()["config_key"] is None
    assert response.json()["paper_initial_cash"] == "100000.00000000"


@pytest.mark.parametrize("activity", ["ledger", "queued", "dead_letter", "ambiguous"])
def test_account_finance_is_immutable_after_activity_but_name_remains_editable(
    env: dict[str, Any], activity: str
) -> None:
    with env["factory"]() as session:
        if activity == "ledger":
            session.add(
                ExecutionLog(
                    log_id="recorded-outcome",
                    user_id="demo_user",
                    account_id=1,
                    strategy_id="swing_high_low_pmo_v1",
                    signal_type="flat",
                    execution_mode="paper",
                    status="success",
                )
            )
        else:
            session.add(
                OutboxEvent(
                    topic="execution.commands",
                    event_type="ExecutionCommand",
                    status="dead_letter" if activity == "dead_letter" else "pending",
                    payload={
                        "user_id": "demo_user",
                        "broker_route": {} if activity == "ambiguous" else {"broker_account_id": 1},
                    },
                )
            )
        session.commit()
    client = env["client"]
    response = client.patch(
        "/broker-accounts/1",
        headers=AUTH,
        json={
            "expected": {"base_ccy": "EUR"},
            "changes": {"base_ccy": "USD"},
        },
    )
    assert response.status_code == 409
    display = client.patch(
        "/broker-accounts/1",
        headers=AUTH,
        json={
            "expected": {"display_name": "Demo User paper"},
            "changes": {"display_name": "Reviewed"},
        },
    )
    assert display.status_code == 200
    assert display.json()["base_ccy"] == "EUR"


def test_unused_account_finance_patch_normalizes_decimal_expected_values(
    env: dict[str, Any],
) -> None:
    response = env["client"].patch(
        "/broker-accounts/1",
        headers=AUTH,
        json={
            "expected": {"paper_initial_cash": "100000"},
            "changes": {"paper_initial_cash": "90000"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["paper_initial_cash"] == "90000"
    assert response.json()["paper_initial_equity"] == "100000.00000000"


def test_display_patch_preserves_legacy_external_identity(env: dict[str, Any]) -> None:
    with env["factory"]() as session:
        session.get(LinkedBrokerAccount, 1).external_ref = "historical-provider-reference"
        session.commit()
    response = env["client"].patch(
        "/broker-accounts/1",
        headers=AUTH,
        json={
            "expected": {"display_name": "Demo User paper"},
            "changes": {"display_name": "Reviewed"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["external_ref"] == "historical-provider-reference"


def test_repeated_successful_account_patch_is_a_noop(env: dict[str, Any]) -> None:
    payload = {
        "expected": {"display_name": "Demo User paper"},
        "changes": {"display_name": "Reviewed"},
    }
    first = env["client"].patch("/broker-accounts/1", headers=AUTH, json=payload)
    assert first.status_code == 200
    second = env["client"].patch("/broker-accounts/1", headers=AUTH, json=payload)
    assert second.status_code == 200
    assert second.json() == first.json()
    with env["factory"]() as session:
        assert session.query(ApiAuditLog).filter_by(action="broker_account.patch").count() == 1


def test_patch_reloads_locked_account_before_checking_expected_values(env: dict[str, Any]) -> None:
    with env["factory"]() as session:
        cached = session.get(LinkedBrokerAccount, 1)
        session.execute(
            LinkedBrokerAccount.__table__.update()
            .where(
                LinkedBrokerAccount.account_id == 1,
            )
            .values(display_name="Concurrent update")
        )
        session.commit()
        assert cached.display_name == "Demo User paper"
        with pytest.raises(AccountOnboardingError, match="expected"):
            patch_account(
                session, 1, {"display_name": "Demo User paper"}, {"display_name": "Overwrite"}
            )


def test_account_ciphertext_pointer_and_audit_commit_or_rollback_together(
    env: dict[str, Any],
) -> None:
    provider = DbSecretsProvider(
        session_factory=env["factory"], master_keys=[Fernet.generate_key().decode()]
    )
    payload = BrokerAccountIn(
        config_key="broker-backed",
        broker_code="coinbase",
        environment="live",
        base_ccy="EUR",
        credentials=BrokerCredentialIn(api_key="fixture-key", api_secret="fixture-secret"),
    )
    with env["factory"]() as session:
        result = onboard_account(session, payload, provider)
        assert session.query(ManagedSecret).count() == 1
        session.rollback()
    with env["factory"]() as session:
        assert session.query(ManagedSecret).count() == 0
        assert session.query(BrokerCredential).count() == 0
        assert session.query(ApiAuditLog).count() == 0
        assert session.get(LinkedBrokerAccount, result.account_id) is None
        committed = onboard_account(session, payload, provider)
        session.commit()
    with env["factory"]() as session:
        assert session.query(ManagedSecret).count() == 1
        assert session.query(BrokerCredential).one().secret_ref == committed.secret_ref
        assert session.query(ApiAuditLog).one().action == "broker_account.create"


def test_failed_audit_rolls_back_written_ciphertext_and_credential_rotation(
    env: dict[str, Any],
) -> None:
    provider = DbSecretsProvider(
        session_factory=env["factory"], master_keys=[Fernet.generate_key().decode()]
    )
    with env["factory"]() as session:
        created = onboard_account(
            session,
            BrokerAccountIn(
                config_key="live-account",
                broker_code="coinbase",
                environment="live",
                base_ccy="EUR",
                credentials=BrokerCredentialIn(api_key="old-key", api_secret="old-secret"),
            ),
            provider,
        )
        session.commit()
    with env["factory"]() as session:
        original = session.query(ManagedSecret).one().ciphertext
        original_rotation = session.query(BrokerCredential).one().last_rotated_at

    def fail_audit(session: Session, _context: Any, _instances: Any) -> None:
        if any(isinstance(row, ApiAuditLog) for row in session.new):
            raise RuntimeError("audit persistence unavailable")

    with env["factory"]() as session:
        event.listen(session, "before_flush", fail_audit)
        with pytest.raises(RuntimeError, match="audit persistence unavailable"):
            rotate_credentials(
                session,
                created.account_id,
                BrokerCredentialIn(
                    api_key="new-key",
                    api_secret="new-secret",
                ),
                provider,
            )
        session.rollback()
    with env["factory"]() as session:
        assert session.query(ManagedSecret).one().ciphertext == original
        assert session.query(BrokerCredential).one().last_rotated_at == original_rotation
        assert session.query(ApiAuditLog).count() == 1
