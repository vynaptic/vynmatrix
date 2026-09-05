"""Unit tests for environment-backed application secrets."""

import pytest

from lib_common.app.secrets import SecretsManager
from lib_common.exceptions import ConfigurationError


class TestSecretsManager:
    """Test suite for SecretsManager."""

    def test_loads_requested_environment_secrets(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("API_KEY", "test-api-key")
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/testdb")

        manager = SecretsManager()
        secrets = manager.load_secrets(
            source="env_vars",
            secret_names=["API_KEY", "DATABASE_URL"],
        )

        assert secrets == {
            "API_KEY": "test-api-key",
            "DATABASE_URL": "postgresql://localhost/testdb",
        }

    def test_defaults_to_canonical_api_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("API_KEY", "test-api-key")

        assert SecretsManager().load_secrets() == {"api_key": "test-api-key"}

    def test_missing_environment_secret_is_explicitly_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("MISSING_SECRET", raising=False)

        secrets = SecretsManager().load_secrets(secret_names=["MISSING_SECRET"])

        assert secrets == {"MISSING_SECRET": ""}

    def test_loaded_secrets_are_available_through_cache(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("API_KEY", "test-api-key")
        manager = SecretsManager()

        manager.load_secrets(secret_names=["api_key"])

        assert manager.get("api_key") == "test-api-key"
        assert manager.get("not_loaded", "default-value") == "default-value"

    def test_clear_cache_removes_loaded_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("API_KEY", "test-api-key")
        manager = SecretsManager()
        manager.load_secrets(secret_names=["api_key"])

        manager.clear_cache()

        assert manager.get("api_key") == ""

    def test_unsupported_source_fails_closed(self) -> None:
        with pytest.raises(ConfigurationError, match="Unsupported application secrets source"):
            SecretsManager().load_secrets(source="unsupported")

    def test_empty_secret_names_loads_nothing(self) -> None:
        assert SecretsManager().load_secrets(secret_names=[]) == {}
