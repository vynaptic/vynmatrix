from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from feedback_loop_engine.api import create_app


class _FeedbackEngineStub:
    pass


class _SuggestionReviewStub:
    def __init__(self) -> None:
        self.approvals: list[tuple[int, str]] = []
        self.rejections: list[tuple[int, str, str | None]] = []

    def list_pending(self, strategy_id: str | None = None) -> list[dict[str, object]]:
        del strategy_id
        return []

    def get_pending(self, feedback_id: int) -> dict[str, object] | None:
        del feedback_id
        return None

    def approve(self, *, feedback_id: int, reviewer_user_id: str) -> bool:
        self.approvals.append((feedback_id, reviewer_user_id))
        return True

    def reject(
        self,
        *,
        feedback_id: int,
        reviewer_user_id: str,
        reason: str | None = None,
    ) -> bool:
        self.rejections.append((feedback_id, reviewer_user_id, reason))
        return True


def _client(review_stub: _SuggestionReviewStub | None = None) -> TestClient:
    return TestClient(
        create_app(
            cast(Any, _FeedbackEngineStub()),
            cast(Any, review_stub or _SuggestionReviewStub()),
        )
    )


def test_production_auth_fails_closed_when_service_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("API_KEY", raising=False)

    response = _client().get("/suggestions")

    assert response.status_code == 503
    assert response.json()["detail"] == "API key not configured"


def test_feedback_rejects_unauthorized_request_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY", "service-secret")

    response = _client().get("/suggestions")

    assert response.status_code == 401


def test_feedback_accepts_valid_service_key_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY", "service-secret")

    response = _client().get("/suggestions", headers={"X-API-Key": "service-secret"})

    assert response.status_code == 200
    assert response.json() == []


def test_feedback_health_and_readiness_are_auth_exempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("API_KEY", raising=False)
    client = _client()

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200


def test_feedback_has_no_runtime_apply_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")

    response = _client().post(
        "/suggestions/1/apply",
        json={"reviewer_user_id": "reviewer"},
    )

    assert response.status_code == 404


def test_feedback_review_routes_use_focused_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    reviews = _SuggestionReviewStub()
    client = _client(reviews)

    approval = client.post(
        "/suggestions/7/approve",
        json={"reviewer_user_id": "reviewer"},
    )
    rejection = client.post(
        "/suggestions/8/reject",
        json={"reviewer_user_id": "reviewer", "reason": "unsafe"},
    )

    assert approval.json() == {"success": True, "message": "Suggestion approved"}
    assert rejection.json() == {"success": True, "message": "Suggestion rejected"}
    assert reviews.approvals == [(7, "reviewer")]
    assert reviews.rejections == [(8, "reviewer", "unsafe")]
