"""Environment-backed application secret loading."""

from __future__ import annotations

import os

from lib_common.exceptions import ConfigurationError
from lib_common.logging import get_logger

logger = get_logger(__name__)


class SecretsManager:
    """Load application-level secrets from the process environment."""

    def __init__(self) -> None:
        self._secrets_cache: dict[str, str] = {}

    def load_secrets(
        self, source: str = "env_vars", secret_names: list[str] | None = None
    ) -> dict[str, str]:
        """
        Load secrets from specified source.

        Args:
            source: Secret source. Only ``env_vars`` is supported.
            secret_names: List of secret names to load. If None, uses defaults.

        Returns:
            Dictionary mapping secret names to values

        Raises:
            ConfigurationError: If secrets loading fails
        """
        if source != "env_vars":
            msg = f"Unsupported application secrets source: {source!r}"
            raise ConfigurationError(msg)
        return self._load_from_env(secret_names)

    def _load_from_env(self, secret_names: list[str] | None = None) -> dict[str, str]:
        """
        Load secrets from environment variables.

        Args:
            secret_names: List of secret names. If None, uses defaults.

        Returns:
            Dictionary mapping secret names to values
        """
        logger.info("Loading secrets from environment variables")

        if secret_names is None:
            # Canonical inter-service key. Broker credentials load per account
            # through ``BROKER_CREDS_<ref>``.
            secret_names = ["api_key"]

        secrets: dict[str, str] = {}
        for name in secret_names:
            # Try with uppercase and original case
            env_name = name.upper()
            value = os.environ.get(env_name, os.environ.get(name, ""))

            if not value:
                logger.warning("Secret '%s' not found in environment", name)

            secrets[name] = value

        self._secrets_cache.update(secrets)
        loaded_count = sum(1 for v in secrets.values() if v)
        logger.info("Loaded %s/%s secrets from environment", loaded_count, len(secret_names))

        return secrets

    def get(self, key: str, default: str = "") -> str:
        """
        Get secret value by key.

        Args:
            key: Secret key name
            default: Default value if key not found

        Returns:
            Secret value or default
        """
        return self._secrets_cache.get(key, default)

    def clear_cache(self) -> None:
        """Clear secrets cache."""
        self._secrets_cache.clear()
        logger.debug("Secrets cache cleared")
