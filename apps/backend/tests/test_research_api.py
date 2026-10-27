"""Research HTTP admission is owner-scoped and never executes editor source."""

from __future__ import annotations

from uuid import uuid4

import pytest
from backend.api import create_app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lib_application.db.models import User
from lib_application.db.models.research_authoring import ResearchDraft
from lib_application.db.models.research_data import ResearchDataset
from lib_application.db.models.research_jobs import ResearchJob

KEY = "research-test-admin"
AUTH = {"X-Admin-Key": KEY}
PREFIX = "/api/ui/research"


@pytest.fixture
def setup_research():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ResearchDataset.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        session.add_all(
            [
                User(
                    user_id="owner",
                    email="owner@example.test",
                    base_ccy="EUR",
                    is_deployment_owner=True,
                ),
                User(user_id="other", email="other@example.test", base_ccy="USD"),
            ]
        )
        session.commit()
    client = TestClient(create_app(session_factory=factory, admin_api_key=KEY, allow_anon=False))
    yield client, factory
    engine.dispose()


def draft_body():
    return {
        "request_id": str(uuid4()),
        "name": "Source is data",
        "source": "raise RuntimeError('source must not execute on save')",
        "config": {"class_name": "UserStrategy", "strategy_id": "draft_v1", "parameters": {}},
    }


def test_research_workspace_requires_admin_and_reports_real_empty_state(setup_research):
    client, _ = setup_research
    assert client.get(PREFIX + "/workspace").status_code == 401
    response = client.get(PREFIX + "/workspace", headers=AUTH)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["drafts"] == []
    assert data["jobs"] == []
    assert data["models"] == []
    assert data["supervisor"]["available"] is False


def test_saving_source_and_queuing_validation_never_imports_it(setup_research):
    client, factory = setup_research
    body = draft_body()
    saved = client.post(PREFIX + "/drafts", json=body, headers=AUTH)
    assert saved.status_code == 200, saved.text
    draft = saved.json()["draft"]
    response = client.post(
        PREFIX + "/jobs",
        json={
            "request_id": str(uuid4()),
            "kind": "validate_strategy",
            "inputs": {"revision_id": draft["revision_id"]},
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json()["job"]["status"] == "queued"
    with factory() as session:
        assert session.scalar(select(ResearchDraft)).owner_id == "owner"
        assert session.scalar(select(ResearchJob)).owner_id == "owner"


def test_draft_retry_conflict_and_revision_export(setup_research):
    client, _ = setup_research
    body = draft_body()
    first = client.post(PREFIX + "/drafts", json=body, headers=AUTH).json()["draft"]
    assert (
        client.post(PREFIX + "/drafts", json=body, headers=AUTH).json()["draft"]["draft_id"]
        == first["draft_id"]
    )
    changed = {
        "expected_revision": first["revision"],
        "name": "Revised",
        "source": "# different source",
        "config": body["config"],
    }
    response = client.post(
        f"{PREFIX}/drafts/{first['draft_id']}/revisions", json=changed, headers=AUTH
    )
    assert response.status_code == 200, response.text
    assert (
        client.post(
            f"{PREFIX}/drafts/{first['draft_id']}/revisions", json=changed, headers=AUTH
        ).status_code
        == 409
    )
    exported = client.get(
        f"{PREFIX}/drafts/{first['draft_id']}/export",
        params={"revision_id": first["revision_id"]},
        headers=AUTH,
    )
    assert exported.status_code == 200
    assert exported.json()["source"] == body["source"]


def test_owner_selectors_and_extra_authority_are_rejected(setup_research):
    client, _ = setup_research
    assert client.get(PREFIX + "/workspace?user_id=other", headers=AUTH).status_code == 422
    assert (
        client.post(
            PREFIX + "/drafts", json={**draft_body(), "owner_id": "other"}, headers=AUTH
        ).status_code
        == 422
    )
    assert (
        client.post(
            PREFIX + "/paper/accounts",
            headers=AUTH,
            json={
                "config_key": "paper-ui",
                "base_ccy": "EUR",
                "paper_initial_equity": "1000",
                "paper_initial_cash": "1000",
                "environment": "live",
            },
        ).status_code
        == 422
    )


def test_cancelled_job_retains_attempt_without_leaking_fencing_token(setup_research):
    client, _ = setup_research
    draft = client.post(PREFIX + "/drafts", json=draft_body(), headers=AUTH).json()["draft"]
    job = client.post(
        PREFIX + "/jobs",
        json={
            "request_id": str(uuid4()),
            "kind": "validate_strategy",
            "inputs": {"revision_id": draft["revision_id"]},
        },
        headers=AUTH,
    ).json()["job"]
    cancelled = client.post(f"{PREFIX}/jobs/{job['job_id']}/cancel", headers=AUTH)
    assert cancelled.status_code == 200
    assert cancelled.json()["job"]["status"] == "cancelled"
    assert "claim_token" not in cancelled.text


def test_history_api_accepts_its_opaque_cursor_and_returns_newest_first(setup_research):
    client, _ = setup_research
    first = client.post(PREFIX + "/drafts", json=draft_body(), headers=AUTH).json()["draft"]
    latest = client.post(PREFIX + "/drafts", json=draft_body(), headers=AUTH).json()["draft"]
    page = client.get(PREFIX + "/drafts?limit=1", headers=AUTH).json()
    assert page["drafts"][0]["draft_id"] == latest["draft_id"]
    following = client.get(
        PREFIX + "/drafts", params={"limit": 1, "after": page["next_after"]}, headers=AUTH
    )
    assert following.status_code == 200, following.text
    assert following.json()["drafts"][0]["draft_id"] == first["draft_id"]


def test_templates_are_real_canonical_source_without_market_samples(setup_research):
    client, _ = setup_research
    assert client.get(PREFIX + "/templates").status_code == 401
    response = client.get(PREFIX + "/templates", headers=AUTH)
    assert response.status_code == 200
    templates = response.json()["templates"]
    assert {row["template_id"] for row in templates} == {"moving_average", "return_model"}
    assert all(isinstance(row["source"], str) and row["source"] for row in templates)
    assert all(row["config"]["class_name"] == "UserStrategy" for row in templates)


def test_paper_setup_rejects_inconsistent_capital_without_a_server_error(setup_research):
    client, _ = setup_research
    response = client.post(
        PREFIX + "/paper/accounts",
        headers=AUTH,
        json={
            "config_key": "paper-ui",
            "base_ccy": "EUR",
            "paper_initial_equity": "1000",
            "paper_initial_cash": "1001",
        },
    )
    assert response.status_code == 422, response.text


def test_maintenance_fences_paper_authority_changes_before_account_lookup(setup_research):
    from lib_application.db.models.research_jobs import ResearchControl

    client, factory = setup_research
    with factory() as session:
        session.get(ResearchControl, 1).maintenance = True
        session.commit()
    response = client.post(
        PREFIX + "/paper/bindings",
        headers=AUTH,
        json={"strategy_id": "missing", "broker_account_id": 9, "mode": "trading"},
    )
    assert response.status_code == 503, response.text


def test_saving_draft_keeps_workspace_reads_responsive(setup_research):
    import asyncio
    import time

    import httpx
    from sqlalchemy import event

    client, factory = setup_research
    completed = []

    def delayed_storage(session, _context, _instances):
        if any(isinstance(item, ResearchDraft) for item in session.new):
            time.sleep(0.2)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://test"
        ) as browser:

            async def save():
                response = await browser.post(PREFIX + "/drafts", headers=AUTH, json=draft_body())
                assert response.status_code == 200, response.text
                completed.append("save")

            async def read():
                await asyncio.sleep(0.02)
                response = await browser.get(PREFIX + "/workspace", headers=AUTH)
                assert response.status_code == 200, response.text
                completed.append("read")

            await asyncio.gather(save(), read())

    event.listen(factory, "before_flush", delayed_storage)
    try:
        asyncio.run(scenario())
    finally:
        event.remove(factory, "before_flush", delayed_storage)
    assert completed == ["read", "save"]


def test_authoring_api_rejects_reserved_model_injection_on_create_and_revision(setup_research):
    client, _ = setup_research
    body = draft_body()
    injected = {
        **body,
        "config": {
            **body["config"],
            "parameters": {"research_model": {"artifact": "unregistered"}},
        },
    }
    response = client.post(PREFIX + "/drafts", json=injected, headers=AUTH)
    assert response.status_code == 422, response.text
    assert "research_model" in response.text
    saved = client.post(PREFIX + "/drafts", json=body, headers=AUTH).json()["draft"]
    response = client.post(
        f"{PREFIX}/drafts/{saved['draft_id']}/revisions",
        json={
            "expected_revision": 1,
            "name": "Injected",
            "source": body["source"],
            "config": injected["config"],
        },
        headers=AUTH,
    )
    assert response.status_code == 422, response.text
    assert "research_model" in response.text
