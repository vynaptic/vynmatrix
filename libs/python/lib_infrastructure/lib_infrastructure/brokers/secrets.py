"""Secrets provider for broker credentials.

This module provides an extensible interface for retrieving broker
credentials from various secret storage backends.

Implementations:
- DbSecretsProvider: Account-scoped DB ciphertext with MultiFernet rotation
- EnvSecretsProvider: Environment variables (local development)

Design:
- Abstract interface allows easy addition of new backends (AWS, Vault, etc.)
- Credentials are never logged or stored in memory longer than needed
- Support for JSON-formatted secrets with multiple fields
"""

import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from lib_common.logging import get_logger

logger = get_logger(__name__)

# A callable returning a context-manager DB session (sessionmaker-style).
SessionFactory = Callable[[], Any]

_shared_session_factory: SessionFactory | None = None


def _default_session_factory() -> SessionFactory:
    """Lazily build (and process-cache) the canonical DB session factory from env.

    Lets the ``db`` secrets backend be self-contained — it sources its own DB
    connection rather than having one threaded through the broker factory/bridge.
    Imports are local to keep this module light and avoid import-order coupling.
    """
    global _shared_session_factory  # noqa: PLW0603 - process-wide singleton
    if _shared_session_factory is None:
        from lib_application.db.session import (  # noqa: PLC0415
            create_engine_for_env,
            get_session_factory,
        )
        from lib_common.env_utils import build_database_url  # noqa: PLC0415

        engine = create_engine_for_env(db_url=build_database_url())
        _shared_session_factory = get_session_factory(engine=engine)
    return _shared_session_factory


@dataclass
class BrokerCredentials:
    """Broker API credentials.

    Attributes:
        api_key: API key for authentication when the broker uses one
        api_secret: API secret for signing requests when the broker uses one
        passphrase: Optional passphrase (required by Coinbase)
        subaccount: Optional subaccount name (for Deribit, etc.)
        additional: Additional broker-specific fields
    """

    api_key: str = ""
    api_secret: str = ""
    passphrase: str | None = None
    subaccount: str | None = None
    additional: dict[str, str] | None = None


def _parse_broker_credentials(raw: str) -> BrokerCredentials:
    data = json.loads(raw)
    if not isinstance(data, dict):
        msg = "broker credential secret must be a JSON object"
        raise TypeError(msg)
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in data.items()):
        msg = "broker credential fields must be strings"
        raise TypeError(msg)
    return BrokerCredentials(
        api_key=data.get("api_key", ""),
        api_secret=data.get("api_secret", ""),
        passphrase=data.get("passphrase"),
        subaccount=data.get("subaccount"),
        additional={
            key: value
            for key, value in data.items()
            if key not in ("api_key", "api_secret", "passphrase", "subaccount")
        },
    )


class ISecretsProvider(ABC):
    """Abstract interface for secrets retrieval.

    Implementations must be able to:
    1. Retrieve raw secret values by reference
    2. Parse JSON secrets into BrokerCredentials

    Thread Safety:
        Implementations should be thread-safe for concurrent access.

    Example:
        >>> provider = EnvSecretsProvider()
        >>> creds = await provider.get_broker_credentials("coinbase-prod-api")
        >>> print(creds.api_key)
        "abc123..."
    """

    @abstractmethod
    async def get_secret(self, secret_ref: str) -> str | None:
        """Get raw secret value by reference.

        Args:
            secret_ref: Secret reference/name/path

        Returns:
            Secret value as string, or None if not found
        """

    async def get_broker_credentials(self, secret_ref: str) -> BrokerCredentials | None:
        """Get broker credentials from secret.

        Expects a JSON object. ``api_key`` and ``api_secret`` are optional at
        this generic parsing boundary because session-based brokers (for
        example IBKR) authenticate with different fields. Each adapter owns the
        strict credential contract for its broker.

        Common fields:
        - api_key (optional)
        - api_secret (optional)
        - passphrase (optional)
        - subaccount (optional)
        - Any additional fields

        Args:
            secret_ref: Secret reference

        Returns:
            BrokerCredentials or None if not found
        """
        raw = await self.get_secret(secret_ref)
        if not raw:
            return None

        try:
            return _parse_broker_credentials(raw)
        except (json.JSONDecodeError, TypeError):
            logger.exception("Failed to parse broker credentials")
            return None


class EnvSecretsProvider(ISecretsProvider):
    """Environment variable secrets provider for local development.

    Looks up secrets from environment variables with naming convention:
        {PREFIX}_{SECRET_REF_UPPERCASE}

    Example:
        Secret ref: "coinbase-live-main"
        Env var: "BROKER_CREDS_COINBASE_LIVE_MAIN"

    For JSON credentials, the env var should contain the full JSON string.

    Example:
        >>> os.environ["BROKER_CREDS_COINBASE_PAPER"] = '{"api_key": "xxx", "api_secret": "yyy"}'
        >>> provider = EnvSecretsProvider()
        >>> creds = await provider.get_broker_credentials("coinbase-paper")
    """

    def __init__(self, prefix: str = "BROKER_CREDS") -> None:
        """Initialize Env Secrets Provider.

        Args:
            prefix: Environment variable prefix
        """
        self._prefix = prefix

    def _ref_to_env_name(self, secret_ref: str) -> str:
        """Convert secret reference to environment variable name."""
        # coinbase-live-main -> BROKER_CREDS_COINBASE_LIVE_MAIN
        normalized = secret_ref.upper().replace("-", "_").replace(".", "_")
        return f"{self._prefix}_{normalized}"

    async def get_secret(self, secret_ref: str) -> str | None:
        """Get secret from environment variable.

        Args:
            secret_ref: Secret reference

        Returns:
            Environment variable value or None if not set
        """
        env_name = self._ref_to_env_name(secret_ref)
        value = os.environ.get(env_name)

        if value is None:
            logger.debug("Environment variable %s not set", env_name)
            return None

        return value


class DbSecretsProvider(ISecretsProvider):
    """DB-backed secrets provider: per-account secrets encrypted at rest.

    Stores each secret as a Fernet-encrypted ciphertext in the ``managed_secrets``
    table, addressed by an account-owned ``secret_ref``. The ordered key ring uses
    MultiFernet semantics: the first key encrypts new values, while every configured
    key may decrypt existing values. This permits a no-downtime master-key rollout.

    Example:
        >>> provider = DbSecretsProvider(session_factory=sf, master_keys=[new_key, old_key])
        >>> provider.set_secret(
        ...     "users/u-a/broker-accounts/42",
        ...     '{"api_key": "..", "api_secret": ".."}',
        ...     account_id=42,
        ... )
        >>> creds = await provider.get_broker_credentials("users/u-a/broker-accounts/42")
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        master_keys: Sequence[str],
    ) -> None:
        """Initialize DB secrets provider.

        Args:
            session_factory: Callable returning a context-manager DB session.
            master_keys: Fernet keys ordered newest first. New ciphertext uses the
                first key; decryption and rotation accept every key.
        """
        from cryptography.fernet import Fernet, MultiFernet  # noqa: PLC0415

        normalized = tuple(str(key).strip() for key in master_keys)
        if not normalized or any(not key for key in normalized):
            msg = "At least one non-empty Fernet key is required"
            raise ValueError(msg)
        if len(set(normalized)) != len(normalized):
            msg = "SECRETS_MASTER_KEYS must not contain duplicate keys"
            raise ValueError(msg)
        try:
            fernets = [Fernet(key.encode("ascii")) for key in normalized]
        except (UnicodeEncodeError, ValueError) as exc:
            msg = "SECRETS_MASTER_KEYS contains an invalid Fernet key"
            raise ValueError(msg) from exc
        self._session_factory = session_factory
        self._fernet = MultiFernet(fernets)

    @staticmethod
    def _registered_credential(
        session: Any,
        *,
        secret_ref: str,
        account_id: int,
    ) -> Any:
        """Return the unique credential registration for one account/ref pair."""
        from lib_application.db.models import BrokerCredential  # noqa: PLC0415

        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            msg = "A positive broker account_id is required"
            raise ValueError(msg)
        normalized_ref = str(secret_ref or "").strip()
        if not normalized_ref:
            msg = "A non-empty secret_ref is required"
            raise ValueError(msg)
        registrations = session.scalars(
            select(BrokerCredential)
            .where(BrokerCredential.secret_ref == normalized_ref)
            .with_for_update()
        ).all()
        if len(registrations) != 1 or int(registrations[0].account_id) != account_id:
            msg = "secret_ref is not uniquely registered to the linked broker account"
            raise ValueError(msg)
        return registrations[0]

    def _load_ciphertext(
        self,
        secret_ref: str,
        *,
        account_id: int | None = None,
    ) -> str | None:
        """Load ciphertext only when its account registration is unambiguous."""
        from lib_application.db.models import (  # noqa: PLC0415
            BrokerCredential,
            ManagedSecret,
        )

        normalized_ref = str(secret_ref or "").strip()
        if not normalized_ref:
            return None

        with self._session_factory() as session:
            row = session.get(ManagedSecret, normalized_ref)
            registrations = session.execute(
                select(BrokerCredential.account_id).where(
                    BrokerCredential.secret_ref == normalized_ref
                )
            ).all()
            if (
                row is None
                or len(registrations) != 1
                or int(registrations[0][0]) != int(row.account_id)
                or (account_id is not None and int(row.account_id) != account_id)
            ):
                return None
            return str(row.ciphertext) if row.ciphertext else None

    async def get_secret(self, secret_ref: str) -> str | None:
        """Fetch and decrypt a uniquely account-registered secret, or None."""
        return self._decrypt_ciphertext(self._load_ciphertext(secret_ref))

    async def get_secret_for_account(
        self,
        secret_ref: str,
        *,
        account_id: int,
    ) -> str | None:
        """Fetch a secret only when ``secret_ref`` belongs to ``account_id``."""
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            return None
        return self._decrypt_ciphertext(self._load_ciphertext(secret_ref, account_id=account_id))

    def _decrypt_ciphertext(self, ciphertext: str | None) -> str | None:
        """Decrypt ciphertext with the configured key ring without logging values."""
        if not ciphertext:
            logger.debug("No managed secret stored for ref")
            return None
        from cryptography.fernet import InvalidToken  # noqa: PLC0415

        try:
            plaintext = self._fernet.decrypt(ciphertext.encode("utf-8"))
            return str(plaintext.decode("utf-8"))
        except InvalidToken:
            logger.exception("Failed to decrypt managed secret with configured key ring")
            return None

    def set_secret(
        self,
        secret_ref: str,
        plaintext: str,
        *,
        account_id: int,
        session: Any | None = None,
    ) -> None:
        """Encrypt + upsert a secret. Used by tenant onboarding / credential rotation.

        The plaintext (JSON credential blob) is never persisted — only the Fernet
        ciphertext is written. Passing the caller's session makes account,
        credential-reference, and ciphertext onboarding atomic.
        """
        normalized_ref = str(secret_ref or "").strip()

        def _set(current_session: Any) -> None:
            from lib_application.db.models import ManagedSecret  # noqa: PLC0415

            credential = self._registered_credential(
                current_session,
                secret_ref=normalized_ref,
                account_id=account_id,
            )
            record = current_session.scalars(
                select(ManagedSecret)
                .where(ManagedSecret.secret_ref == normalized_ref)
                .with_for_update()
            ).one_or_none()
            if record is not None and int(record.account_id) != account_id:
                msg = "secret_ref ciphertext belongs to a different broker account"
                raise ValueError(msg)
            token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
            if record is None:
                current_session.add(
                    ManagedSecret(
                        secret_ref=normalized_ref,
                        account_id=account_id,
                        ciphertext=token,
                    )
                )
            else:
                record.ciphertext = token
            credential.last_rotated_at = datetime.now(tz=UTC).replace(tzinfo=None)
            current_session.flush()

        if session is not None:
            _set(session)
            return
        with self._session_factory() as owned_session:
            _set(owned_session)
            owned_session.commit()

    def rotate_secret(
        self,
        secret_ref: str,
        *,
        account_id: int,
        session: Any | None = None,
    ) -> None:
        """Atomically re-encrypt one account secret with the newest master key.

        MultiFernet decrypts with any configured key and encrypts the replacement
        token with the first key. Only ciphertext and ``last_rotated_at`` are
        written; plaintext is never returned by this operation.
        """
        from cryptography.fernet import InvalidToken  # noqa: PLC0415

        normalized_ref = str(secret_ref or "").strip()

        def _rotate(current_session: Any) -> None:
            from lib_application.db.models import ManagedSecret  # noqa: PLC0415

            credential = self._registered_credential(
                current_session,
                secret_ref=normalized_ref,
                account_id=account_id,
            )
            record = current_session.scalars(
                select(ManagedSecret)
                .where(
                    ManagedSecret.secret_ref == normalized_ref,
                    ManagedSecret.account_id == account_id,
                )
                .with_for_update()
            ).one_or_none()
            if record is None or not record.ciphertext:
                msg = "No managed secret is registered to the linked broker account"
                raise ValueError(msg)
            try:
                rotated = self._fernet.rotate(record.ciphertext.encode("utf-8"))
            except InvalidToken as exc:
                msg = "Managed secret cannot be decrypted with the configured key ring"
                raise ValueError(msg) from exc
            record.ciphertext = rotated.decode("utf-8")
            credential.last_rotated_at = datetime.now(tz=UTC).replace(tzinfo=None)
            current_session.flush()

        if session is not None:
            _rotate(session)
            return
        with self._session_factory() as owned_session:
            _rotate(owned_session)
            owned_session.commit()


class CompositeSecretsProvider(ISecretsProvider):
    """Composite provider that tries multiple backends in order.

    Useful for DB-first lookup with an environment fallback during development.

    Example:
        >>> provider = CompositeSecretsProvider([
        ...     DbSecretsProvider(session_factory=session_factory, master_keys=keys),
        ...     EnvSecretsProvider(),
        ... ])
        >>> # Will try the database first, then environment variables
        >>> creds = await provider.get_broker_credentials("coinbase-paper")
    """

    def __init__(self, providers: list[ISecretsProvider]) -> None:
        """Initialize composite provider.

        Args:
            providers: List of providers to try in order
        """
        self._providers = providers

    async def get_secret(self, secret_ref: str) -> str | None:
        """Try each provider until one succeeds.

        Args:
            secret_ref: Secret reference

        Returns:
            Secret value from first successful provider, or None
        """
        for provider in self._providers:
            try:
                value = await provider.get_secret(secret_ref)
                if value is not None:
                    return value
            except Exception as e:
                logger.debug(
                    "Provider %s failed: %s",
                    provider.__class__.__name__,
                    e,
                    secret_ref=secret_ref,
                )
                continue

        return None


def _parse_master_keys(raw_keys: str) -> tuple[str, ...]:
    """Parse a newest-first comma-separated Fernet key ring."""
    keys = tuple(key.strip() for key in raw_keys.split(","))
    if not keys or any(not key for key in keys):
        msg = "SECRETS_MASTER_KEYS must be a non-empty comma-separated key list"
        raise ValueError(msg)
    return keys


def _configured_master_keys(
    explicit_keys: Sequence[str] | str | None,
) -> tuple[str, ...] | None:
    """Resolve the canonical newest-first key ring."""
    if explicit_keys is not None:
        if isinstance(explicit_keys, str):
            return _parse_master_keys(explicit_keys)
        return tuple(explicit_keys)

    configured = os.environ.get("SECRETS_MASTER_KEYS")
    if configured is not None:
        return _parse_master_keys(configured)
    return None


def create_secrets_provider(
    backend: str | None = None,
    *,
    session_factory: SessionFactory | None = None,
    master_keys: Sequence[str] | str | None = None,
) -> ISecretsProvider:
    """Create the configured, provider-agnostic secrets backend.

    The backend is selected by the ``backend`` arg or the ``SECRETS_BACKEND`` env
    var (default ``"env"`` for local development).
    The DigitalOcean deployment uses ``SECRETS_BACKEND=db`` with a newest-first
    ``SECRETS_MASTER_KEYS`` key ring; the ``ISecretsProvider`` seam remains
    independent of deployment topology.

    Backends:
        - ``env``: ``EnvSecretsProvider`` — ``BROKER_CREDS_{REF}`` env vars (dev/local).
        - ``db``: ``DbSecretsProvider`` — per-user secrets encrypted at rest in
          Postgres with ``SECRETS_MASTER_KEYS``; scales per-user with no redeploy.
        - ``composite``: DB (if a master key ring is set) then env — dev fallback.

    Args:
        backend: Override for ``SECRETS_BACKEND``.
        session_factory: DB session factory for the ``db`` backend (defaults to the
            canonical env-derived factory).
        master_keys: Fernet keys ordered newest first, or the equivalent comma-
            separated string (defaults to ``SECRETS_MASTER_KEYS``).

    Returns:
        Configured secrets provider.
    """
    backend = (backend or os.environ.get("SECRETS_BACKEND") or "env").strip().lower()

    if backend == "env":
        return EnvSecretsProvider()

    if backend == "db":
        keys = _configured_master_keys(master_keys)
        if keys is None:
            msg = "SECRETS_MASTER_KEYS is required for the 'db' secrets backend"
            raise ValueError(msg)
        return DbSecretsProvider(
            session_factory=session_factory or _default_session_factory(),
            master_keys=keys,
        )

    if backend == "composite":
        providers: list[ISecretsProvider] = []
        keys = _configured_master_keys(master_keys)
        if keys is not None:
            providers.append(
                DbSecretsProvider(
                    session_factory=session_factory or _default_session_factory(),
                    master_keys=keys,
                )
            )
        providers.append(EnvSecretsProvider())
        return CompositeSecretsProvider(providers)

    msg = f"Unknown SECRETS_BACKEND: {backend!r} (expected env|db|composite)"
    raise ValueError(msg)
