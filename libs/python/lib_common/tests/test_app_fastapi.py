from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lib_common.app import create_service_app


def test_production_startup_requires_service_api_key(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("API_KEY", raising=False)

    app = create_service_app(title="Test Service", version="0.1.0")

    with pytest.raises(RuntimeError, match="API_KEY is required in production"), TestClient(app):
        pass


def test_metrics_requires_service_key_in_production(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY", "service-secret")
    app = create_service_app(title="Test Service", version="0.1.0")

    with TestClient(app) as client:
        unauthorized = client.get("/metrics")
        authorized = client.get("/metrics", headers={"X-API-Key": "service-secret"})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert "text/plain" in authorized.headers["content-type"]


def test_custom_auth_app_can_disable_service_key_boundary(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("API_KEY", raising=False)
    app = create_service_app(
        title="Custom Auth Service",
        version="0.1.0",
        service_auth=False,
    )

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
