"""DB-encrypted secrets backend + provider-agnostic factory selection."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from lib_application.db.models import (
    Base,
    Broker,
    BrokerCredential,
    LinkedBrokerAccount,
    ManagedSecret,
    User,
)
from lib_infrastructure.brokers.secrets import (
    DbSecretsProvider,
    EnvSecretsProvider,
    create_secrets_provider,
)

USER_A_SECRET_REF = "users/user-a/broker-accounts/1"
USER_B_SECRET_REF = "users/user-b/broker-accounts/2"
GENERIC_SECRET_REF = "users/user-c/broker-accounts/3"
GENERIC_ACCOUNT_ID = 3


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, future=True)
    with factory() as session:
        session.add_all(
            [
                User(user_id="user-a", email="a@example.com", base_ccy="USD"),
                User(user_id="user-b", email="b@example.com", base_ccy="EUR"),
                User(user_id="user-c", email="c@example.com", base_ccy="USD"),
            ]
        )
        session.add(Broker(broker_id=1, code="coinbase", name="Coinbase"))
        session.add_all(
            [
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="user-a",
                    broker_id=1,
                    environment="live",
                    display_name="User A Coinbase",
                    base_ccy="USD",
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=2,
                    user_id="user-b",
                    broker_id=1,
                    environment="live",
                    display_name="User B Coinbase",
                    base_ccy="EUR",
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=GENERIC_ACCOUNT_ID,
                    user_id="user-c",
                    broker_id=1,
                    environment="live",
                    display_name="User C Coinbase",
                    base_ccy="USD",
                    status="connected",
                ),
            ]
        )
        session.add_all(
            [
                BrokerCredential(
                    account_id=1,
                    secret_ref=USER_A_SECRET_REF,
                    status="active",
                ),
                BrokerCredential(
                    account_id=GENERIC_ACCOUNT_ID,
                    secret_ref=GENERIC_SECRET_REF,
                    status="active",
                ),
                BrokerCredential(
                    account_id=2,
                    secret_ref=USER_B_SECRET_REF,
                    status="active",
                ),
            ]
        )
        session.commit()

    @contextmanager
    def _ctx() -> Iterator[Session]:
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return _ctx


def _key() -> str:
    return Fernet.generate_key().decode()


def test_db_provider_encrypts_at_rest_and_round_trips(session_factory) -> None:
    provider = DbSecretsProvider(session_factory=session_factory, master_keys=[_key()])
    plaintext = '{"api_key": "k-123", "api_secret": "s-xyz"}'
    provider.set_secret(USER_A_SECRET_REF, plaintext, account_id=1)

    # Stored value is ciphertext — the plaintext key/secret never hit the DB.
    with session_factory() as s:
        row = s.get(ManagedSecret, USER_A_SECRET_REF)
        assert row is not None
        assert "k-123" not in row.ciphertext
        assert "s-xyz" not in row.ciphertext
        credential = (
            s.query(BrokerCredential).filter(BrokerCredential.secret_ref == USER_A_SECRET_REF).one()
        )
        assert credential.last_rotated_at is not None

    # Read path decrypts + parses into BrokerCredentials.
    assert asyncio.run(provider.get_secret(USER_A_SECRET_REF)) == plaintext
    creds = asyncio.run(provider.get_broker_credentials(USER_A_SECRET_REF))
    assert creds is not None
    assert creds.api_key == "k-123"
    assert creds.api_secret == "s-xyz"


def test_db_provider_missing_ref_returns_none(session_factory) -> None:
    provider = DbSecretsProvider(session_factory=session_factory, master_keys=[_key()])
    assert asyncio.run(provider.get_secret("does-not-exist")) is None


def test_generic_parser_supports_session_broker_without_api_keys(session_factory) -> None:
    provider = DbSecretsProvider(session_factory=session_factory, master_keys=[_key()])
    provider.set_secret(
        GENERIC_SECRET_REF,
        '{"subaccount":"U1234567","gateway_url":"https://localhost:5000"}',
        account_id=GENERIC_ACCOUNT_ID,
    )

    credentials = asyncio.run(provider.get_broker_credentials(GENERIC_SECRET_REF))

    assert credentials is not None
    assert credentials.api_key == ""
    assert credentials.api_secret == ""
    assert credentials.subaccount == "U1234567"
    assert credentials.additional == {"gateway_url": "https://localhost:5000"}


@pytest.mark.parametrize(
    "payload",
    [
        '["not", "an", "object"]',
        '{"api_key": 123, "api_secret": "secret"}',
    ],
)
def test_generic_parser_rejects_non_string_credential_documents(
    session_factory,
    payload: str,
) -> None:
    provider = DbSecretsProvider(session_factory=session_factory, master_keys=[_key()])
    provider.set_secret(GENERIC_SECRET_REF, payload, account_id=GENERIC_ACCOUNT_ID)

    assert asyncio.run(provider.get_broker_credentials(GENERIC_SECRET_REF)) is None


def test_db_provider_wrong_master_key_fails_closed(session_factory) -> None:
    # A different master key must NOT decrypt — return None, never leak/guess.
    writer = DbSecretsProvider(session_factory=session_factory, master_keys=[_key()])
    writer.set_secret(
        GENERIC_SECRET_REF,
        '{"api_key": "a", "api_secret": "b"}',
        account_id=GENERIC_ACCOUNT_ID,
    )

    reader = DbSecretsProvider(session_factory=session_factory, master_keys=[_key()])
    assert asyncio.run(reader.get_secret(GENERIC_SECRET_REF)) is None


def test_set_secret_upserts_not_duplicates(session_factory) -> None:
    provider = DbSecretsProvider(session_factory=session_factory, master_keys=[_key()])
    provider.set_secret(
        GENERIC_SECRET_REF,
        '{"api_key": "a", "api_secret": "b"}',
        account_id=GENERIC_ACCOUNT_ID,
    )
    provider.set_secret(
        GENERIC_SECRET_REF,
        '{"api_key": "c", "api_secret": "d"}',
        account_id=GENERIC_ACCOUNT_ID,
    )  # rotate

    creds = asyncio.run(provider.get_broker_credentials(GENERIC_SECRET_REF))
    assert creds is not None
    assert creds.api_key == "c"
    with session_factory() as s:
        assert s.query(ManagedSecret).count() == 1  # updated in place


def test_multifernet_decrypts_old_and_encrypts_with_newest_key(session_factory) -> None:
    old_key = _key()
    new_key = _key()
    old_plaintext = '{"api_key": "old-a", "api_secret": "old-b"}'
    new_plaintext = '{"api_key": "new-a", "api_secret": "new-b"}'

    old_provider = DbSecretsProvider(
        session_factory=session_factory,
        master_keys=[old_key],
    )
    old_provider.set_secret(
        USER_A_SECRET_REF,
        old_plaintext,
        account_id=1,
    )

    rotating_provider = DbSecretsProvider(
        session_factory=session_factory,
        master_keys=[new_key, old_key],
    )
    assert asyncio.run(rotating_provider.get_secret(USER_A_SECRET_REF)) == old_plaintext

    rotating_provider.set_secret(
        GENERIC_SECRET_REF,
        new_plaintext,
        account_id=GENERIC_ACCOUNT_ID,
    )
    with session_factory() as session:
        row = session.get(ManagedSecret, GENERIC_SECRET_REF)
        assert row is not None
        ciphertext = row.ciphertext

    assert Fernet(new_key.encode()).decrypt(ciphertext.encode()).decode() == new_plaintext
    with pytest.raises(InvalidToken):
        Fernet(old_key.encode()).decrypt(ciphertext.encode())


def test_rotate_secret_is_atomic_and_updates_rotation_timestamp(session_factory) -> None:
    old_key = _key()
    new_key = _key()
    plaintext = '{"api_key": "a", "api_secret": "b"}'
    old_provider = DbSecretsProvider(
        session_factory=session_factory,
        master_keys=[old_key],
    )
    old_provider.set_secret(
        GENERIC_SECRET_REF,
        plaintext,
        account_id=GENERIC_ACCOUNT_ID,
    )
    with session_factory() as session:
        credential = (
            session.query(BrokerCredential)
            .filter(BrokerCredential.secret_ref == GENERIC_SECRET_REF)
            .one()
        )
        credential.last_rotated_at = datetime(2020, 1, 1)
        row = session.get(ManagedSecret, GENERIC_SECRET_REF)
        assert row is not None
        original_ciphertext = row.ciphertext
        session.commit()

    wrong_provider = DbSecretsProvider(
        session_factory=session_factory,
        master_keys=[new_key],
    )
    with pytest.raises(ValueError, match="cannot be decrypted"):
        wrong_provider.rotate_secret(GENERIC_SECRET_REF, account_id=GENERIC_ACCOUNT_ID)
    with session_factory() as session:
        credential = (
            session.query(BrokerCredential)
            .filter(BrokerCredential.secret_ref == GENERIC_SECRET_REF)
            .one()
        )
        row = session.get(ManagedSecret, GENERIC_SECRET_REF)
        assert row is not None
        assert row.ciphertext == original_ciphertext
        assert credential.last_rotated_at == datetime(2020, 1, 1)

    provider = DbSecretsProvider(
        session_factory=session_factory,
        master_keys=[new_key, old_key],
    )
    provider.rotate_secret(GENERIC_SECRET_REF, account_id=GENERIC_ACCOUNT_ID)

    with session_factory() as session:
        credential = (
            session.query(BrokerCredential)
            .filter(BrokerCredential.secret_ref == GENERIC_SECRET_REF)
            .one()
        )
        row = session.get(ManagedSecret, GENERIC_SECRET_REF)
        assert row is not None
        rotated_ciphertext = row.ciphertext
        assert credential.last_rotated_at > datetime(2020, 1, 1)

    assert rotated_ciphertext != original_ciphertext
    assert Fernet(new_key.encode()).decrypt(rotated_ciphertext.encode()).decode() == plaintext
    with pytest.raises(InvalidToken):
        Fernet(old_key.encode()).decrypt(rotated_ciphertext.encode())


def test_secret_management_is_account_scoped(session_factory) -> None:
    provider = DbSecretsProvider(session_factory=session_factory, master_keys=[_key()])
    plaintext = '{"api_key": "a", "api_secret": "b"}'
    secret_ref = USER_A_SECRET_REF
    provider.set_secret(secret_ref, plaintext, account_id=1)

    assert asyncio.run(provider.get_secret_for_account(secret_ref, account_id=1)) == plaintext
    assert asyncio.run(provider.get_secret_for_account(secret_ref, account_id=2)) is None
    with pytest.raises(ValueError, match="uniquely registered"):
        provider.set_secret(secret_ref, plaintext, account_id=2)
    with pytest.raises(ValueError, match="uniquely registered"):
        provider.rotate_secret(secret_ref, account_id=2)
    assert asyncio.run(provider.get_secret_for_account(secret_ref, account_id=1)) == plaintext


def test_db_provider_rejects_invalid_key_sets(session_factory) -> None:
    with pytest.raises(ValueError, match="At least one"):
        DbSecretsProvider(session_factory=session_factory, master_keys=[])
    with pytest.raises(ValueError, match="invalid Fernet key"):
        DbSecretsProvider(session_factory=session_factory, master_keys=["not-a-key"])
    duplicate = _key()
    with pytest.raises(ValueError, match="duplicate"):
        DbSecretsProvider(
            session_factory=session_factory,
            master_keys=[duplicate, duplicate],
        )


def test_factory_default_backend_is_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRETS_BACKEND", raising=False)
    assert isinstance(create_secrets_provider(), EnvSecretsProvider)


def test_factory_selects_db_backend(monkeypatch: pytest.MonkeyPatch, session_factory) -> None:
    monkeypatch.setenv("SECRETS_BACKEND", "db")
    monkeypatch.setenv("SECRETS_MASTER_KEYS", _key())
    monkeypatch.delenv("SECRETS_MASTER_KEY", raising=False)
    provider = create_secrets_provider(session_factory=session_factory)
    assert isinstance(provider, DbSecretsProvider)


def test_factory_db_requires_master_key(monkeypatch: pytest.MonkeyPatch, session_factory) -> None:
    monkeypatch.setenv("SECRETS_BACKEND", "db")
    monkeypatch.delenv("SECRETS_MASTER_KEYS", raising=False)
    monkeypatch.delenv("SECRETS_MASTER_KEY", raising=False)
    with pytest.raises(ValueError, match="SECRETS_MASTER_KEYS"):
        create_secrets_provider(session_factory=session_factory)


def test_factory_rejects_invalid_canonical_ring_without_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
    session_factory,
) -> None:
    monkeypatch.setenv("SECRETS_BACKEND", "db")
    monkeypatch.setenv("SECRETS_MASTER_KEYS", f"{_key()},")
    monkeypatch.setenv("SECRETS_MASTER_KEY", _key())

    with pytest.raises(ValueError, match="non-empty comma-separated"):
        create_secrets_provider(session_factory=session_factory)


def test_factory_rejects_removed_singular_key_configuration(
    monkeypatch: pytest.MonkeyPatch,
    session_factory,
) -> None:
    monkeypatch.setenv("SECRETS_BACKEND", "db")
    monkeypatch.delenv("SECRETS_MASTER_KEYS", raising=False)
    monkeypatch.setenv("SECRETS_MASTER_KEY", _key())

    with pytest.raises(ValueError, match="SECRETS_MASTER_KEYS"):
        create_secrets_provider(session_factory=session_factory)


def test_factory_unknown_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRETS_BACKEND", "nope")
    with pytest.raises(ValueError, match="Unknown SECRETS_BACKEND"):
        create_secrets_provider()
