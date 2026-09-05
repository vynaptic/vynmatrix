from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scoring_engine.api import create_app
from scoring_engine.engine import ScoreEngine
from scoring_engine.storage import InMemoryScoreStore


def _client() -> TestClient:
    engine = ScoreEngine(store=InMemoryScoreStore(), default_weight=1.0, half_life_bars=10)
    return TestClient(create_app(engine))


def test_production_auth_fails_closed_when_service_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("API_KEY", raising=False)

    response = _client().get("/bindings")

    assert response.status_code == 503
    assert response.json()["detail"] == "API key not configured"


def test_scoring_rejects_unauthorized_request_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY", "service-secret")

    response = _client().get("/bindings")

    assert response.status_code == 401


def test_operator_routes_require_a_distinct_admin_key_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY", "service-secret")

    response = _client().get("/bindings", headers={"X-API-Key": "service-secret"})

    assert response.status_code == 503
    assert response.json()["detail"] == "Admin API key not configured"


def test_operator_routes_accept_valid_service_and_admin_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY", "service-secret")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")

    response = _client().get(
        "/bindings",
        headers={
            "X-API-Key": "service-secret",
            "X-Admin-API-Key": "admin-secret",
        },
    )

    assert response.status_code == 200
    assert response.json() == []


def test_scoring_health_and_readiness_are_auth_exempt_but_readiness_is_durable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("API_KEY", raising=False)
    client = _client()

    assert client.get("/health").status_code == 200
    # 503 is the durability verdict for this in-memory test store, not an auth
    # rejection. Production startup cannot construct this topology.
    assert client.get("/ready").status_code == 503
