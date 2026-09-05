"""Tests for build_database_url() in lib_common.env_utils."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from lib_common.env_utils import build_database_url

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DB_ENV_KEYS = ("DATABASE_URL", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")


@pytest.fixture(autouse=True)
def _clean_env():
    """Remove all DB-related env vars before each test."""
    saved = {k: os.environ.pop(k, None) for k in _DB_ENV_KEYS}
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


# ---------------------------------------------------------------------------
# Scenario: Full DATABASE_URL override
# ---------------------------------------------------------------------------


def test_full_url_override_from_env():
    """WHEN DATABASE_URL is set, it is returned verbatim."""
    with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://x:y@host:1234/db"}):
        assert build_database_url() == "postgresql://x:y@host:1234/db"


def test_full_url_override_from_dotenv():
    """WHEN DATABASE_URL is in dotenv_values only, it is returned verbatim."""
    url = build_database_url(dotenv_values={"DATABASE_URL": "postgresql://a:b@h:5/d"})
    assert url == "postgresql://a:b@h:5/d"


def test_env_overrides_dotenv():
    """OS env takes precedence over dotenv for DATABASE_URL."""
    with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://env"}):
        url = build_database_url(dotenv_values={"DATABASE_URL": "postgresql://dotenv"})
        assert url == "postgresql://env"


# ---------------------------------------------------------------------------
# Scenario: Component assembly from environment variables
# ---------------------------------------------------------------------------


def test_component_assembly():
    """WHEN all components are set, the URL is assembled correctly."""
    env = {
        "DB_HOST": "myhost",
        "DB_PORT": "9999",
        "DB_NAME": "mydb",
        "DB_USER": "myuser",
        "DB_PASSWORD": "secret",
    }
    with mock.patch.dict(os.environ, env):
        url = build_database_url()
        assert url == "postgresql://myuser:secret@myhost:9999/mydb"


# ---------------------------------------------------------------------------
# Scenario: Default values for non-identity components
# ---------------------------------------------------------------------------


def test_default_component_values():
    """WHEN identity/secrets are set, non-identity component defaults are used."""
    with mock.patch.dict(os.environ, {"DB_USER": "service-role", "DB_PASSWORD": "pw"}):
        url = build_database_url()
        assert url == "postgresql://service-role:pw@localhost:5432/vm_trading"


def test_raises_without_database_user():
    """Component assembly never invents a database identity."""
    with (
        mock.patch.dict(os.environ, {"DB_PASSWORD": "pw"}),
        pytest.raises(ValueError, match="DB_USER is required"),
    ):
        build_database_url()


# ---------------------------------------------------------------------------
# Scenario: Dev environment allows default password
# ---------------------------------------------------------------------------


def test_dev_raises_without_password():
    """WHEN env is 'dev' and no DB_PASSWORD, ValueError is raised (no hardcoded default)."""
    with (
        mock.patch.dict(os.environ, {"DB_USER": "service-role"}),
        pytest.raises(ValueError, match="DB_PASSWORD is required"),
    ):
        build_database_url(env="dev")


# ---------------------------------------------------------------------------
# Scenario: Non-dev environment requires explicit password
# ---------------------------------------------------------------------------


def test_non_dev_raises_without_password():
    """WHEN env is not 'dev' and DB_PASSWORD is missing, ValueError is raised."""
    with (
        mock.patch.dict(os.environ, {"DB_USER": "service-role"}),
        pytest.raises(ValueError, match="DB_PASSWORD is required"),
    ):
        build_database_url(env="prod")


def test_staging_raises_without_password():
    """Staging also requires explicit password."""
    with (
        mock.patch.dict(os.environ, {"DB_USER": "service-role"}),
        pytest.raises(ValueError, match="DB_PASSWORD is required"),
    ):
        build_database_url(env="staging")


# ---------------------------------------------------------------------------
# Scenario: Dotenv fallback for component values
# ---------------------------------------------------------------------------


def test_dotenv_fallback_for_components():
    """WHEN dotenv_values provides components not in OS env, they are used."""
    dv = {
        "DB_HOST": "dotenv-host",
        "DB_PORT": "6543",
        "DB_NAME": "dotenv-db",
        "DB_USER": "dotenv-user",
        "DB_PASSWORD": "dotenv-pw",
    }
    url = build_database_url(dotenv_values=dv)
    assert url == "postgresql://dotenv-user:dotenv-pw@dotenv-host:6543/dotenv-db"


def test_os_env_takes_precedence_over_dotenv_components():
    """OS env vars take precedence over dotenv_values for components."""
    dv = {
        "DB_USER": "dotenv-user",
        "DB_PASSWORD": "dotenv-pw",
        "DB_HOST": "dotenv-host",
    }
    with mock.patch.dict(os.environ, {"DB_PASSWORD": "env-pw", "DB_HOST": "env-host"}):
        url = build_database_url(dotenv_values=dv)
        assert "env-pw" in url
        assert "env-host" in url
        assert "dotenv-pw" not in url
        assert "dotenv-host" not in url
