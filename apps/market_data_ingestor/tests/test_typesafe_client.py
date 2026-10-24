"""TypeSafe client tests over httpx.MockTransport (no network)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx
import pytest

from lib_strategy.adverse_news_questions import TYPESAFE_ENDPOINT
from market_data_ingestor.typesafe_client import TypeSafeClient, TypeSafeRequestError

_KEY = "sk-test-secret-key-123"
_NOW = datetime(2025, 1, 21, 9, tzinfo=UTC)
_REQUEST = {"model": "jev-1.14.0", "state": {"name": "Zoë"}, "questions": {}}


def _client(handler: httpx.MockTransport | object, **kwargs: object) -> TypeSafeClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return TypeSafeClient(
        api_key=_KEY,
        http_client=httpx.Client(transport=transport),
        clock=lambda: _NOW,
        retry_base_delay_seconds=0.0,
        retry_max_delay_seconds=0.0,
        **kwargs,  # type: ignore[arg-type]
    )


def test_ask_sends_bearer_and_exact_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b'{"model":"jev-1.14.0"}')

    _client(handler).ask(_REQUEST)
    request = seen[0]
    assert str(request.url) == TYPESAFE_ENDPOINT
    assert request.headers["Authorization"] == f"Bearer {_KEY}"
    assert request.headers["Content-Type"] == "application/json"
    assert (
        request.content == json.dumps(_REQUEST, separators=(",", ":"), ensure_ascii=False).encode()
    )
    assert json.loads(request.content) == _REQUEST


def test_ask_returns_exact_bytes() -> None:
    body = b'{ "model": "jev-1.14.0",  "x": 1 }'
    exchange = _client(lambda _r: httpx.Response(200, content=body)).ask(_REQUEST)
    assert exchange.response_bytes == body
    assert exchange.response_json == {"model": "jev-1.14.0", "x": 1}
    assert exchange.request_json == _REQUEST
    assert json.loads(exchange.request_bytes) == _REQUEST
    assert exchange.received_at == _NOW


def test_ask_retries_429_then_succeeds() -> None:
    calls = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    exchange = _client(handler).ask(_REQUEST)
    assert len(calls) == 2
    assert exchange.response_json == {"ok": True}


def test_ask_raises_on_400_without_retry() -> None:
    calls = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad")

    with pytest.raises(TypeSafeRequestError, match="400"):
        _client(handler).ask(_REQUEST)
    assert len(calls) == 1


def test_timeout_raises_request_error_after_retries() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(TypeSafeRequestError, match="timed out"):
        _client(handler, retries=1).ask(_REQUEST)


def test_non_json_response_raises() -> None:
    with pytest.raises(TypeSafeRequestError, match="not valid JSON"):
        _client(lambda _r: httpx.Response(200, content=b"<html>")).ask(_REQUEST)


def test_api_key_absent_from_error_messages_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=request.headers["Authorization"])

    client = _client(handler, retries=1)
    with caplog.at_level(logging.DEBUG), pytest.raises(TypeSafeRequestError) as info:
        client.ask(_REQUEST)
    assert _KEY not in str(info.value)
    assert _KEY not in repr(info.value)
    assert _KEY not in repr(client)
    assert _KEY not in caplog.text
    assert all(_KEY not in str(record.__dict__) for record in caplog.records)


def test_redirect_raises_without_retry() -> None:
    calls = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(302, headers={"Location": "https://elsewhere.invalid"})

    with pytest.raises(TypeSafeRequestError, match="302"):
        _client(handler).ask(_REQUEST)
    assert len(calls) == 1


def test_close_closes_only_an_owned_http_client() -> None:
    supplied = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    TypeSafeClient(api_key=_KEY, http_client=supplied).close()
    assert not supplied.is_closed
    owned = TypeSafeClient(api_key=_KEY)
    owned.close()
    assert owned._http.is_closed


def test_context_manager_closes_owned_client() -> None:
    with TypeSafeClient(api_key=_KEY) as client:
        assert not client._http.is_closed
    assert client._http.is_closed
