"""Golden and fail-closed tests for official NYSE session compilation."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

import dev_cli.validation.backtest.nyse_official_sessions as nyse_sessions
from dev_cli.validation.backtest.equity_snapshot_identity import (
    MarketSessionSourceKind,
    read_historical_market_sessions,
)
from dev_cli.validation.backtest.nyse_official_sessions import (
    CompiledNYSEOfficialSessionArtifact,
    NYSEOfficialSessionCompilerConfig,
    NYSEOfficialSessionError,
    compile_nyse_official_session_artifact,
)
from dev_cli.validation.evidence import canonical_json_bytes

_START = date(2018, 11, 27)
_END = date(2026, 1, 6)
_RETRIEVED_AT = datetime(2026, 8, 2, 10, 30, tzinfo=UTC)
_EXPECTED_SESSION_COUNT = 1_786
_EXTENDED_END = date(2026, 12, 31)


def _offline_source_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_override: Callable[[httpx.Request, bytes], httpx.Response] | None = None,
) -> tuple[httpx.Client, dict[str, bytes], dict[str, int]]:
    """Provide deterministic source bytes while retaining production hash gates."""

    bodies: dict[str, bytes] = {}
    specs = []
    for source_number, spec in enumerate(nyse_sessions._PINNED_SOURCE_SPECS, start=1):
        content = (
            b"%PDF-1.4\n"
            + f"% NYSE source-byte contract {source_number}: {spec.role}\n".encode()
            + b"%%EOF\n"
        )
        bodies[spec.url] = content
        specs.append(replace(spec, expected_sha256=hashlib.sha256(content).hexdigest()))
    monkeypatch.setattr(nyse_sessions, "_PINNED_SOURCE_SPECS", tuple(specs))
    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls[url] = calls.get(url, 0) + 1
        content = bodies[url]
        if response_override is not None:
            return response_override(request, content)
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Type": "application/pdf",
                "ETag": f'"{hashlib.md5(content, usedforsecurity=False).hexdigest()}"',
                "Last-Modified": "Mon, 28 Dec 2020 20:00:36 GMT",
            },
            content=content,
        )

    return httpx.Client(transport=httpx.MockTransport(handler)), bodies, calls


def _compile(client: httpx.Client) -> CompiledNYSEOfficialSessionArtifact:
    return compile_nyse_official_session_artifact(
        start=_START,
        end=_END,
        client=client,
        clock=lambda: _RETRIEVED_AT,
    )


def _write_content_addressed_artifact(root: Path, content: bytes) -> tuple[Path, str]:
    digest = hashlib.sha256(content).hexdigest()
    path = root / f"{digest}.json"
    path.write_bytes(content)
    return path, digest


def test_full_artifact_is_accepted_by_existing_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, bodies, calls = _offline_source_client(monkeypatch)
    with client:
        artifact = _compile(client)

    artifact_path = tmp_path / "xnys-official-sessions.json"
    artifact_path.write_bytes(artifact.content)
    schedule = read_historical_market_sessions(
        artifact_path,
        expected_content_sha256=artifact.content_sha256,
        expected_venue="XNYS",
        expected_from=_START,
        expected_to=_END,
    )

    assert artifact.session_count == _EXPECTED_SESSION_COUNT
    assert schedule.source_kind is MarketSessionSourceKind.EXCHANGE
    assert schedule.confirmatory_eligible is True
    assert len(schedule.sessions) == _EXPECTED_SESSION_COUNT
    assert date(2018, 12, 5) not in schedule.session_dates
    assert date(2025, 1, 9) not in schedule.session_dates

    by_date = {session.session_date: session for session in schedule.sessions}
    assert by_date[date(2024, 11, 29)].closes_at == datetime(2024, 11, 29, 18, tzinfo=UTC)
    assert by_date[date(2024, 7, 10)].opens_at == datetime(2024, 7, 10, 13, 30, tzinfo=UTC)
    assert by_date[date(2024, 7, 10)].closes_at == datetime(2024, 7, 10, 20, tzinfo=UTC)

    payload = json.loads(artifact.content)
    assert payload["source_kind"] == "exchange"
    assert len(payload["source_documents"]) == len(bodies)
    for source in payload["source_documents"]:
        assert base64.b64decode(source["content_base64"], validate=True) == bodies[source["url"]]
        assert calls[source["url"]] == 1


def test_frozen_source_artifact_extends_coverage_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, bodies, _calls = _offline_source_client(monkeypatch)
    with client:
        source = _compile(client)
    source_path, source_sha256 = _write_content_addressed_artifact(tmp_path, source.content)

    extended = compile_nyse_official_session_artifact(
        start=_START,
        end=_EXTENDED_END,
        source_artifact=source_path,
        source_artifact_sha256=source_sha256,
    )
    output_path, _output_sha256 = _write_content_addressed_artifact(tmp_path, extended.content)
    schedule = read_historical_market_sessions(
        output_path,
        expected_content_sha256=extended.content_sha256,
        expected_venue="XNYS",
        expected_from=_START,
        expected_to=_EXTENDED_END,
    )

    by_date = {session.session_date: session for session in schedule.sessions}
    assert by_date[date(2026, 7, 31)].opens_at == datetime(2026, 7, 31, 13, 30, tzinfo=UTC)
    assert by_date[date(2026, 7, 31)].closes_at == datetime(2026, 7, 31, 20, tzinfo=UTC)
    assert by_date[date(2026, 8, 3)].opens_at == datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
    assert by_date[date(2026, 8, 3)].closes_at == datetime(2026, 8, 3, 20, tzinfo=UTC)
    assert extended.retrieved_at == source.retrieved_at
    output_payload = json.loads(extended.content)
    assert output_payload["source_documents"] == json.loads(source.content)["source_documents"]
    for document in output_payload["source_documents"]:
        assert (
            base64.b64decode(document["content_base64"], validate=True) == bodies[document["url"]]
        )


def test_frozen_source_artifact_rejects_changed_authoritative_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, _bodies, _calls = _offline_source_client(monkeypatch)
    with client:
        source = _compile(client)
    payload = json.loads(source.content)
    payload["source_documents"][0]["url"] = "https://s2.q4cdn.com/not-the-pinned-source.pdf"
    changed_path, changed_sha256 = _write_content_addressed_artifact(
        tmp_path,
        canonical_json_bytes(payload),
    )

    with pytest.raises(NYSEOfficialSessionError, match="url does not match"):
        compile_nyse_official_session_artifact(
            start=_START,
            end=_EXTENDED_END,
            source_artifact=changed_path,
            source_artifact_sha256=changed_sha256,
        )


def test_frozen_source_artifact_requires_exact_content_address(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, _bodies, _calls = _offline_source_client(monkeypatch)
    with client:
        source = _compile(client)
    source_path, _source_sha256 = _write_content_addressed_artifact(tmp_path, source.content)

    with pytest.raises(NYSEOfficialSessionError, match="filename is not content-addressed"):
        compile_nyse_official_session_artifact(
            start=_START,
            end=_EXTENDED_END,
            source_artifact=source_path,
            source_artifact_sha256="0" * 64,
        )


def test_changed_official_source_bytes_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    first_url = nyse_sessions._PINNED_SOURCE_SPECS[0].url

    def changed_response(request: httpx.Request, content: bytes) -> httpx.Response:
        changed = content + b"changed"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=changed if str(request.url) == first_url else content,
        )

    client, _bodies, _calls = _offline_source_client(
        monkeypatch,
        response_override=changed_response,
    )
    with client, pytest.raises(NYSEOfficialSessionError, match="pinned SHA-256"):
        _compile(client)


def test_retryable_http_status_is_bounded_and_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    first_url = nyse_sessions._PINNED_SOURCE_SPECS[0].url
    attempts = 0

    def transient_response(request: httpx.Request, content: bytes) -> httpx.Response:
        nonlocal attempts
        if str(request.url) == first_url:
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=content,
        )

    client, _bodies, calls = _offline_source_client(
        monkeypatch,
        response_override=transient_response,
    )
    with client:
        artifact = compile_nyse_official_session_artifact(
            start=_START,
            end=_END,
            config=NYSEOfficialSessionCompilerConfig(
                retries=1,
                retry_base_delay_seconds=0.0,
                retry_max_delay_seconds=0.0,
            ),
            client=client,
            clock=lambda: _RETRIEVED_AT,
        )

    assert artifact.session_count == _EXPECTED_SESSION_COUNT
    assert calls[first_url] == 2


def test_exchange_calendar_disagreement_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _bodies, _calls = _offline_source_client(monkeypatch)
    original_sessions_between = nyse_sessions.sessions_between

    def incomplete_library_schedule(mic: str, start: date, end: date) -> list[date]:
        sessions = original_sessions_between(mic, start, end)
        return [session for session in sessions if session != date(2024, 7, 10)]

    monkeypatch.setattr(nyse_sessions, "sessions_between", incomplete_library_schedule)
    with client, pytest.raises(NYSEOfficialSessionError, match=r"library_missing=.*2024-07-10"):
        _compile(client)


def test_unsupported_source_coverage_fails_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        pytest.fail("coverage validation must run before source acquisition")

    client = httpx.Client(transport=httpx.MockTransport(unexpected_request))
    with client, pytest.raises(NYSEOfficialSessionError, match="cover only"):
        compile_nyse_official_session_artifact(
            start=date(2017, 12, 29),
            end=_END,
            client=client,
            clock=lambda: _RETRIEVED_AT,
        )
