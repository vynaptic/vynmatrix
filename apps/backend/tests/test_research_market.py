"""Research HTTP imports are bounded, owner-resolved and fail closed."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lib_application.db.models import Base, Instrument, User

CSV = "ts,open,high,low,close,volume\n2026-01-01T00:00:00Z,100,102,99,101,4\n"
META = {
    "instrument_id": 1,
    "source": "archive",
    "provenance": "recorded archive",
    "currency": "USD",
    "timeframe": "1m",
    "timestamp_semantics": "bar_open",
    "adjustment_policy": "raw",
}
AUTH = {"X-Admin-Key": "test"}


@pytest.fixture
def client() -> Any:
    from backend.research_market import register_research_market

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        session.add(
            User(user_id="owner", email="owner@test", base_ccy="USD", is_deployment_owner=True)
        )
        session.add(
            Instrument(
                instr_id=1, asset_class="crypto", canonical="BTCUSD", settlement_currency="USD"
            )
        )
        session.commit()
    app = FastAPI()
    app.state.test_session_factory = factory

    def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
        if x_admin_key != "test":
            raise HTTPException(status_code=401)

    register_research_market(app, session_factory=factory, require_admin=require_admin)
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def test_upload_freeze_browse_export_and_auth(client: TestClient) -> None:
    assert client.get("/api/ui/research/market").status_code == 401
    response = client.post(
        "/api/ui/research/imports", headers=AUTH, json={"metadata": META, "csv": CSV}
    )
    assert response.status_code == 200, response.text
    receipt = response.json()["import"]
    frozen = client.post(
        "/api/ui/research/datasets",
        headers=AUTH,
        json={
            "import_id": receipt["import_id"],
            "expected_hash": receipt["content_hash"],
            "name": "Recorded",
        },
    )
    assert frozen.status_code == 200, frozen.text
    dataset = frozen.json()["dataset"]
    listing = client.get("/api/ui/research/datasets", headers=AUTH).json()
    assert listing["datasets"][0]["dataset_id"] == dataset["dataset_id"]
    detail = client.get(
        f"/api/ui/research/datasets/{dataset['dataset_id']}?include_bars=true", headers=AUTH
    ).json()
    assert detail["bars"][0]["close"] == "101"
    exported = client.get(
        f"/api/ui/research/datasets/{dataset['dataset_id']}/export", headers=AUTH
    ).json()
    assert exported["original_csv"] == CSV
    assert exported["as_of"]


def test_unknown_selectors_duplicate_fields_and_bad_content_are_rejected(
    client: TestClient,
) -> None:
    assert client.get("/api/ui/research/market?owner_id=other", headers=AUTH).status_code == 422
    assert client.get("/api/ui/research/market?limit=1&limit=2", headers=AUTH).status_code == 422
    assert (
        client.post(
            "/api/ui/research/imports",
            headers=AUTH,
            json={"metadata": {**META, "owner_id": "other"}, "csv": CSV},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/ui/research/imports",
            headers={**AUTH, "Content-Type": "application/json"},
            content='{"metadata":{},"metadata":{},"csv":""}',
        ).status_code
        == 422
    )


def test_stream_limit_applies_before_json_decode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.research_http as api

    monkeypatch.setattr(api, "MAX_REQUEST_BYTES", 32)
    response = client.post(
        "/api/ui/research/imports",
        headers={**AUTH, "Content-Type": "application/json"},
        content=b" " * 33,
    )
    assert response.status_code == 413


def test_lone_unicode_surrogate_is_rejected_without_an_internal_error(client: TestClient) -> None:
    response = client.post(
        "/api/ui/research/imports", headers=AUTH, content=b'{"metadata":{},"csv":"\\ud800"}'
    )
    assert response.status_code == 422


def test_import_transaction_runs_outside_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from lib_application.services import research_data

    original = research_data.import_dataset

    def guarded(*args: Any, **kwargs: Any) -> Any:
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return original(*args, **kwargs)

    monkeypatch.setattr(research_data, "import_dataset", guarded)
    response = client.post(
        "/api/ui/research/imports", headers=AUTH, json={"metadata": META, "csv": CSV}
    )
    assert response.status_code == 200


def test_invalid_snapshot_returns_a_committed_validation_receipt(client: TestClient) -> None:
    from datetime import datetime

    from lib_application.db.models import InstrumentPrice

    with client.app.state.test_session_factory() as store:
        store.add(
            InstrumentPrice(
                instr_id=1,
                ts=datetime(2026, 1, 1),
                source="archive",
                timeframe="1m",
                open=None,
                high=102,
                low=99,
                close=101,
                volume=4,
            )
        )
        store.commit()
    response = client.post(
        "/api/ui/research/datasets/snapshot",
        headers=AUTH,
        json={
            "name": "Invalid snapshot",
            "metadata": {**META, "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:01:00Z"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["dataset"] is None
    assert response.json()["import"]["valid"] is False
    assert client.get("/api/ui/research/datasets", headers=AUTH).json()["datasets"] == []
