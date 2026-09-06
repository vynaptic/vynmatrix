"""The backend hands its one session factory to the db secrets backend."""

from __future__ import annotations

from typing import Any

import pytest
from backend import main as backend_main


def test_build_app_passes_the_session_factory_to_the_secrets_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = object()
    provider = object()
    seen: dict[str, Any] = {}

    def _create_secrets_provider(*_args: Any, **kwargs: Any) -> object:
        seen["secrets_kwargs"] = kwargs
        return provider

    def _create_app(**kwargs: Any) -> object:
        seen["app_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(backend_main, "get_session_factory", lambda: factory)
    monkeypatch.setattr(backend_main, "create_secrets_provider", _create_secrets_provider)
    monkeypatch.setattr(backend_main, "create_app", _create_app)

    backend_main.build_app()

    # Without this the db backend builds a second bounded engine per process.
    assert seen["secrets_kwargs"]["session_factory"] is factory
    assert seen["app_kwargs"] == {"session_factory": factory, "secrets_provider": provider}
