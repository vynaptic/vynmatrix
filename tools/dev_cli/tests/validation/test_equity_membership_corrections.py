"""Reviewed membership correction acquisition and frozen-manifest tests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from dev_cli.validation.backtest.equity_membership_corrections import (
    CORRECTION_SOURCE_SPEC_SCHEMA,
    MembershipCorrectionAction,
    MembershipCorrectionError,
    acquire_membership_correction_evidence,
    load_frozen_membership_correction_evidence,
    load_membership_correction_source_spec,
)
from dev_cli.validation.evidence import canonical_json_bytes, verified_content_path

_CLOCK = datetime(2026, 8, 2, 14, 0, tzinfo=UTC)
_SP_URL = "https://press.spglobal.com/2019-02-21-Wabtec-Set-to-Join-S-P-500"
_SEC_URL = "https://www.sec.gov/Archives/edgar/data/943452/source.htm"


def _interval(
    *,
    start: str | None,
    end: str | None,
    active: bool,
) -> dict[str, object]:
    return {
        "code": "WAB",
        "end_exclusive": end,
        "is_active_now": active,
        "is_delisted": False,
        "name": "Westinghouse Air Brake Technologies Corp",
        "start_inclusive": start,
    }


def _spec_payload() -> dict[str, Any]:
    return {
        "corrections": [
            {
                "action": "replace_exact_interval",
                "bridge_kind": "index_membership_correction",
                "correction_id": "wab-2019-primary-index-correction-v1",
                "effective_date": "2019-02-27",
                "expected_ticker_interval": _interval(
                    start="2019-02-26",
                    end="2019-02-27",
                    active=False,
                ),
                "new_identity": {
                    "cik": "943452",
                    "code": "WAB",
                    "security_id": "cik:0000943452",
                },
                "old_identity": {
                    "cik": "943452",
                    "code": "WAB",
                    "security_id": "cik:0000943452",
                },
                "rationale": "S&P notice fixes the exact effective membership interval.",
                "replacement_intervals": [
                    _interval(start="2019-02-27", end="2020-01-01", active=False),
                    _interval(start="2020-01-01", end=None, active=True),
                ],
                "source_ids": ["sp-wab-2019", "sec-wab-2019"],
                "valid_from": "2019-02-27",
                "valid_to_exclusive": None,
            },
            {
                "action": "identity_bridge_only",
                "bridge_kind": "same_security_rename",
                "correction_id": "wab-identity-only-v1",
                "edge_usage": "provider_alias_same_class",
                "effective_date": "2019-02-27",
                "expected_ticker_interval": _interval(
                    start="2019-02-26",
                    end=None,
                    active=True,
                ),
                "new_identity": {
                    "cik": "943452",
                    "code": "WAB",
                    "is_active_now": True,
                    "is_delisted": False,
                    "name": "Westinghouse Air Brake Technologies Corp",
                    "provider_symbol": "WAB",
                    "security_id": "figi:BBG000D8RBN6",
                    "valid_from": "2019-02-27",
                    "valid_to_exclusive": None,
                },
                "old_identity": {
                    "cik": "943452",
                    "code": "WAB",
                    "is_active_now": False,
                    "is_delisted": True,
                    "name": "Westinghouse Air Brake Technologies Corp",
                    "provider_symbol": "WABP",
                    "security_id": "figi:BBG000D8RBN6",
                    "valid_from": "1995-01-01",
                    "valid_to_exclusive": "2019-02-27",
                },
                "rationale": "Identity-only evidence cannot create membership dates.",
                "replacement_intervals": [],
                "source_ids": ["sec-wab-2019"],
                "valid_from": "2019-02-27",
                "valid_to_exclusive": None,
            },
        ],
        "review": {
            "reviewed_at": "2026-08-02T14:00:00Z",
            "reviewed_by": "vynmatrix personal research",
        },
        "schema": CORRECTION_SOURCE_SPEC_SCHEMA,
        "sources": [
            {
                "document_date": "2019-02-21",
                "kind": "index_provider_notice",
                "source_id": "sp-wab-2019",
                "url": _SP_URL,
            },
            {
                "document_date": "2019-02-25",
                "kind": "sec_filing",
                "source_id": "sec-wab-2019",
                "url": _SEC_URL,
            },
        ],
    }


def _write_spec(path: Path, payload: dict[str, Any] | None = None) -> None:
    path.write_bytes(canonical_json_bytes(payload or _spec_payload()) + b"\n")


def test_parses_zero_to_many_replacements_and_identity_only_edge(tmp_path: Path) -> None:
    path = tmp_path / "corrections.json"
    _write_spec(path)

    spec = load_membership_correction_source_spec(path)

    replacement, identity_only = spec.corrections
    assert replacement.action is MembershipCorrectionAction.REPLACE_EXACT_INTERVAL
    assert replacement.reviewed_listing_date is None
    assert len(replacement.replacement_intervals) == 2
    assert replacement.old_identity.cik == "0000943452"
    assert identity_only.action is MembershipCorrectionAction.IDENTITY_BRIDGE_ONLY
    assert identity_only.replacement_intervals == ()


def test_reviewed_listing_date_is_bound_to_exact_replacement(tmp_path: Path) -> None:
    payload = _spec_payload()
    payload["corrections"][0]["reviewed_listing_date"] = "2019-02-27"
    path = tmp_path / "corrections.json"
    _write_spec(path, payload)

    spec = load_membership_correction_source_spec(path)

    assert spec.corrections[0].reviewed_listing_date == date(2019, 2, 27)


def test_reviewed_listing_date_cannot_be_added_to_identity_only_edge(tmp_path: Path) -> None:
    payload = _spec_payload()
    payload["corrections"][1]["reviewed_listing_date"] = "2019-02-27"
    path = tmp_path / "corrections.json"
    _write_spec(path, payload)

    with pytest.raises(MembershipCorrectionError, match="requires an interval replacement"):
        load_membership_correction_source_spec(path)


def test_noncanonical_spec_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "corrections.json"
    path.write_text(json.dumps(_spec_payload(), indent=2), encoding="utf-8")

    with pytest.raises(MembershipCorrectionError, match="canonical JSON"):
        load_membership_correction_source_spec(path)


def test_identity_only_edge_cannot_supply_replacement_interval(tmp_path: Path) -> None:
    payload = _spec_payload()
    payload["corrections"][1]["replacement_intervals"] = [
        _interval(start="2019-02-27", end=None, active=True)
    ]
    path = tmp_path / "corrections.json"
    _write_spec(path, payload)

    with pytest.raises(MembershipCorrectionError, match="identity-only bridge"):
        load_membership_correction_source_spec(path)


def test_identity_edge_requires_class_qualified_security_id(tmp_path: Path) -> None:
    payload = _spec_payload()
    payload["corrections"][1]["old_identity"]["security_id"] = "cik:0000943452"
    path = tmp_path / "corrections.json"
    _write_spec(path, payload)

    with pytest.raises(MembershipCorrectionError, match="class-qualified"):
        load_membership_correction_source_spec(path)


def test_interval_replacement_rejects_partial_reviewed_identity_endpoint(
    tmp_path: Path,
) -> None:
    payload = _spec_payload()
    payload["corrections"][0]["new_identity"]["provider_symbol"] = "WAB"
    path = tmp_path / "corrections.json"
    _write_spec(path, payload)

    with pytest.raises(MembershipCorrectionError, match="reviewed endpoint is partial"):
        load_membership_correction_source_spec(path)


def test_overlapping_provider_symbol_collision_fails_closed(tmp_path: Path) -> None:
    payload = _spec_payload()
    collision = json.loads(json.dumps(payload["corrections"][1]))
    collision["correction_id"] = "provider-symbol-collision-v1"
    collision["old_identity"]["security_id"] = "isin:US0000000001"
    collision["old_identity"]["cik"] = "1"
    collision["new_identity"]["security_id"] = "isin:US0000000001"
    collision["new_identity"]["cik"] = "1"
    collision["new_identity"]["provider_symbol"] = "WABP"
    payload["corrections"].append(collision)
    path = tmp_path / "corrections.json"
    _write_spec(path, payload)

    with pytest.raises(MembershipCorrectionError, match="provider-symbol collision"):
        load_membership_correction_source_spec(path)


def test_interval_correction_requires_primary_identity_evidence(tmp_path: Path) -> None:
    payload = _spec_payload()
    payload["corrections"][0]["source_ids"] = ["sp-wab-2019"]
    path = tmp_path / "corrections.json"
    _write_spec(path, payload)

    with pytest.raises(MembershipCorrectionError, match="SEC or issuer"):
        load_membership_correction_source_spec(path)


def test_cik_security_identity_contradiction_fails_closed(tmp_path: Path) -> None:
    payload = _spec_payload()
    payload["corrections"][0]["new_identity"]["security_id"] = "cik:0000000001"
    path = tmp_path / "corrections.json"
    _write_spec(path, payload)

    with pytest.raises(MembershipCorrectionError, match="contradicts its CIK"):
        load_membership_correction_source_spec(path)


def test_acquires_exact_sources_and_reloads_frozen_manifest(tmp_path: Path) -> None:
    path = tmp_path / "corrections.json"
    _write_spec(path)
    source_bytes = {
        _SP_URL: b"<html>S&P primary notice</html>",
        _SEC_URL: b"<html>SEC filing</html>",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        content = source_bytes[str(request.url)]
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Length": str(len(content)), "Content-Type": "text/html"},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        acquired = acquire_membership_correction_evidence(
            tmp_path,
            source_spec_path=path,
            client=client,
            clock=lambda: _CLOCK,
        )
    loaded = load_frozen_membership_correction_evidence(
        tmp_path,
        acquired.manifest_path,
    )

    assert loaded.manifest_sha256 == acquired.manifest_sha256
    assert loaded.spec.content_sha256 == acquired.spec.content_sha256
    assert len(loaded.artifacts) == 4
    assert loaded.artifacts[0].role == "eodhd_membership_correction_evidence_manifest"
    for artifact in loaded.artifacts:
        assert verified_content_path(tmp_path, artifact).is_file()
    source_artifacts = [
        artifact
        for artifact in loaded.artifacts
        if artifact.role == "eodhd_membership_primary_source_document"
    ]
    assert {artifact.context["url"] for artifact in source_artifacts} == set(source_bytes)
    assert all(
        artifact.context["retrieved_at"] == _CLOCK.isoformat() for artifact in source_artifacts
    )


def test_frozen_source_context_tamper_fails_manifest_hash(tmp_path: Path) -> None:
    path = tmp_path / "corrections.json"
    _write_spec(path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"primary",
            headers={"Content-Type": "text/html"},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        acquired = acquire_membership_correction_evidence(
            tmp_path,
            source_spec_path=path,
            client=client,
            clock=lambda: _CLOCK,
        )
    envelope = json.loads(acquired.manifest_path.read_bytes())
    envelope["manifest"]["artifacts"][1]["context"]["url"] = "https://press.spglobal.com/tampered"
    acquired.manifest_path.write_bytes(canonical_json_bytes(envelope) + b"\n")

    with pytest.raises(MembershipCorrectionError, match="manifest content address differs"):
        load_frozen_membership_correction_evidence(tmp_path, acquired.manifest_path)


def test_redirect_is_not_accepted_as_primary_source_bytes(tmp_path: Path) -> None:
    path = tmp_path / "corrections.json"
    _write_spec(path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://example.com/not-authority"},
            request=request,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client,
        pytest.raises(MembershipCorrectionError, match="HTTP 302"),
    ):
        acquire_membership_correction_evidence(
            tmp_path,
            source_spec_path=path,
            client=client,
            clock=lambda: _CLOCK,
        )


def test_unapproved_index_notice_host_fails_before_network(tmp_path: Path) -> None:
    payload = _spec_payload()
    payload["sources"][0]["url"] = "https://example.com/index-notice"
    path = tmp_path / "corrections.json"
    _write_spec(path, payload)

    with pytest.raises(MembershipCorrectionError, match=r"press\.spglobal\.com"):
        load_membership_correction_source_spec(path)
