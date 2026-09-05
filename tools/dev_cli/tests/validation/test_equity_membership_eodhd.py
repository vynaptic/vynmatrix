"""Licensed EODHD effective-date membership materialization tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from dev_cli.validation.backtest.equity_membership_corrections import (
    CORRECTION_SOURCE_SPEC_SCHEMA,
    acquire_membership_correction_evidence,
)
from dev_cli.validation.backtest.equity_membership_eodhd import (
    EODHDMembershipMaterializationError,
    materialize_eodhd_index_membership,
)
from dev_cli.validation.backtest.equity_snapshot_identity import (
    ListingEvidenceStatus,
    MembershipSourceKind,
    SnapshotValidationError,
    ValidationProvider,
    read_historical_aliases,
    read_membership_intervals,
)
from dev_cli.validation.evidence import canonical_json_bytes, verified_content_path
from dev_cli.validation.providers.eodhd import (
    EODHDJsonEvidence,
    EODHDMarketDataError,
)

_CLOCK = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _evidence(endpoint: str, payload: Any) -> EODHDJsonEvidence:
    content = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return EODHDJsonEvidence(
        endpoint=endpoint,
        retrieved_at=_CLOCK,
        payload=payload,
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def _component(code: str, name: str, snapshot_date: str) -> dict[str, str]:
    return {
        "Code": code,
        "Date": snapshot_date,
        "Exchange": "NYSE",
        "Industry": "Industrial",
        "Name": name,
        "Sector": "Industrials",
    }


def _history(
    code: str,
    name: str,
    *,
    start: str | None,
    end: str | None,
    active: int,
    delisted: int,
) -> dict[str, object]:
    return {
        "Code": code,
        "Name": name,
        "StartDate": start,
        "EndDate": end,
        "IsActiveNow": active,
        "IsDelisted": delisted,
    }


def _mapping(code: str, **identifiers: str | None) -> EODHDJsonEvidence:
    data = [{"symbol": f"{code}.US", **identifiers}] if identifiers else []
    return _evidence(
        f"/api/id-mapping?filter[symbol]={code}.US&page[limit]=100&page[offset]=0&fmt=json",
        {
            "meta": {"limit": 100, "offset": 0, "total": len(data)},
            "data": data,
            "links": {"next": None},
        },
    )


def _general(
    code: str,
    name: str,
    *,
    ipo_date: str | None,
    is_delisted: bool | None,
    isin: str | None,
    cik: str | None,
    updated_at: str | None = "2026-08-01",
) -> EODHDJsonEvidence:
    return _evidence(
        f"/api/v1.1/fundamentals/{code}.US?filter=General&fmt=json",
        {
            "CIK": cik,
            "Code": code,
            "IPODate": ipo_date,
            "ISIN": isin,
            "IsDelisted": is_delisted,
            "Name": name,
            "UpdatedAt": updated_at,
        },
    )


class _MembershipProvider:
    def __init__(self) -> None:
        histories = {
            "0": _history(
                "AAA",
                "Alpha Inc",
                start=None,
                end=None,
                active=1,
                delisted=0,
            ),
            "1": _history(
                "BBB",
                "Beta Inc",
                start="2019-01-01",
                end="2020-01-03",
                active=0,
                delisted=1,
            ),
            "2": _history(
                "CCC",
                "Gamma Inc",
                start="2020-01-03",
                end=None,
                active=1,
                delisted=0,
            ),
        }
        snapshots = {
            "2019-12-20": {
                "0": _component("AAA", "Alpha Inc", "2019-12-20"),
                "1": _component("BBB", "Beta Inc", "2019-12-20"),
            },
            "2020-01-03": {
                "0": _component("AAA", "Alpha Inc", "2020-01-03"),
                "1": _component("CCC", "Gamma Inc", "2020-01-03"),
            },
        }
        self.ticker = _evidence(
            "/api/fundamentals/GSPC.INDX?filter=HistoricalTickerComponents&fmt=json",
            histories,
        )
        self.snapshots = _evidence(
            "/api/v1.1/fundamentals/GSPC.INDX?historical=1&from=2018-01-01&to=2020-01-10&fmt=json",
            {"HistoricalComponents": snapshots},
        )
        self.active = _evidence(
            "/api/exchange-symbol-list/US?type=common_stock&fmt=json",
            [
                {
                    "Code": "AAA",
                    "Name": "Alpha Inc",
                    "Exchange": "NYSE",
                    "Type": "Common Stock",
                    "Isin": "US0000000001",
                },
                {
                    "Code": "CCC",
                    "Name": "Gamma Inc",
                    "Exchange": "NYSE",
                    "Type": "Common Stock",
                    "Isin": "US0000000003",
                },
            ],
        )
        self.delisted = _evidence(
            "/api/exchange-symbol-list/US?type=common_stock&delisted=1&fmt=json",
            [
                {
                    "Code": "BBB",
                    "Name": "Beta Inc",
                    "Exchange": "NYSE",
                    "Type": "Common Stock",
                    "Isin": None,
                }
            ],
        )
        self.symbol_changes = _evidence(
            "/api/symbol-change-history?from=2018-01-01&to=2020-01-10&ex=US&fmt=json",
            [],
        )
        self.mappings = {
            "AAA": _mapping(
                "AAA",
                figi="BBG000000001",
                isin="US0000000001",
                cusip="000000001",
                cik="1",
            ),
            "BBB": _mapping("BBB", figi="BBG000000002", cik="2"),
            "CCC": _mapping(
                "CCC",
                isin="US0000000003",
                cusip="000000003",
                cik="3",
            ),
        }
        self.generals = {
            "AAA": _general(
                "AAA",
                "Alpha Inc",
                ipo_date="1980-01-01",
                is_delisted=False,
                isin="US0000000001",
                cik="1",
            ),
            "BBB": _general(
                "BBB",
                "Beta Inc",
                ipo_date="1990-01-01",
                is_delisted=True,
                isin=None,
                cik="2",
            ),
            "CCC": _general(
                "CCC",
                "Gamma Inc",
                ipo_date="2000-01-01",
                is_delisted=False,
                isin="US0000000003",
                cik="3",
            ),
        }
        self.mapping_calls: list[str] = []
        self.general_calls: list[str] = []

    def fetch_index_membership_history(
        self,
        *,
        index_symbol: str,
        start: date,
        end: date,
    ) -> tuple[EODHDJsonEvidence, EODHDJsonEvidence]:
        assert index_symbol == "GSPC.INDX"
        assert start == date(2018, 1, 1)
        assert end == date(2020, 1, 10)
        return self.ticker, self.snapshots

    def fetch_us_symbol_directory(self, *, delisted: bool) -> EODHDJsonEvidence:
        return self.delisted if delisted else self.active

    def fetch_us_symbol_change_history(
        self,
        *,
        start: date,
        end: date,
    ) -> EODHDJsonEvidence:
        assert start == date(2018, 1, 1)
        assert end == date(2020, 1, 10)
        return self.symbol_changes

    def fetch_id_mapping(self, *, provider_symbol: str) -> EODHDJsonEvidence:
        self.mapping_calls.append(provider_symbol)
        return self.mappings[provider_symbol]

    def fetch_security_fundamentals_general(
        self,
        *,
        provider_symbol: str,
    ) -> EODHDJsonEvidence:
        self.general_calls.append(provider_symbol)
        return self.generals[provider_symbol]


def _materialize(
    root: Path,
    provider: _MembershipProvider,
    *,
    authority_correction_manifest_path: Path | None = None,
):
    return materialize_eodhd_index_membership(
        root,
        provider=provider,
        index_symbol="GSPC.INDX",
        evidence_start=date(2018, 1, 1),
        requested_start=date(2020, 1, 1),
        requested_end=date(2020, 1, 10),
        dataset_version="eodhd-all-in-one-observed-2026-08-02",
        entitlement_scope="personal",
        entitlement_owner_user_id="owner-user",
        authority_correction_manifest_path=authority_correction_manifest_path,
        clock=lambda: _CLOCK,
    )


def _authority_correction_manifest(
    root: Path,
    *,
    expected_start: str | None,
    replacements: list[dict[str, object]],
    reviewed_listing_date: str | None = None,
    reviewed_identity_endpoint: bool = False,
    reviewed_identity_name: str = "Alpha Inc",
    reviewed_security_id: str = "figi:BBG000000001",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    source_url = "https://press.spglobal.com/alpha-index-correction"
    sec_url = "https://www.sec.gov/Archives/edgar/data/1/alpha-identity.htm"
    interval = {
        "code": "AAA",
        "end_exclusive": None,
        "is_active_now": True,
        "is_delisted": False,
        "name": "Alpha Inc",
        "start_inclusive": expected_start,
    }
    payload = {
        "corrections": [
            {
                "action": "replace_exact_interval",
                "bridge_kind": "index_membership_correction",
                "correction_id": "alpha-reviewed-primary-correction-v1",
                "effective_date": "2019-12-20",
                "expected_ticker_interval": interval,
                "new_identity": {
                    "cik": "1",
                    "code": "AAA",
                    "security_id": reviewed_security_id,
                },
                "old_identity": {
                    "cik": "1",
                    "code": "AAA",
                    "security_id": reviewed_security_id,
                },
                "rationale": "Reviewed index-provider notice replaces only the exact raw row.",
                "replacement_intervals": replacements,
                "reviewed_listing_date": reviewed_listing_date,
                "source_ids": ["sp-alpha-correction", "sec-alpha-identity"],
                "valid_from": "2019-12-20",
                "valid_to_exclusive": None,
            }
        ],
        "review": {
            "reviewed_at": "2026-08-02T12:00:00Z",
            "reviewed_by": "vynmatrix personal research",
        },
        "schema": CORRECTION_SOURCE_SPEC_SCHEMA,
        "sources": [
            {
                "document_date": "2019-12-19",
                "kind": "index_provider_notice",
                "source_id": "sp-alpha-correction",
                "url": source_url,
            },
            {
                "document_date": "2019-12-19",
                "kind": "sec_filing",
                "source_id": "sec-alpha-identity",
                "url": sec_url,
            },
        ],
    }
    if reviewed_identity_endpoint:
        payload["corrections"][0]["new_identity"].update(
            {
                "is_active_now": True,
                "is_delisted": False,
                "name": reviewed_identity_name,
                "provider_symbol": "AAA",
                "valid_from": "2019-12-20",
                "valid_to_exclusive": None,
            }
        )
    spec_path = root / "correction-source.json"
    spec_path.write_bytes(canonical_json_bytes(payload) + b"\n")

    def handler(request: httpx.Request) -> httpx.Response:
        content = b"<html>reviewed S&P correction notice</html>"
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": "text/html"},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        frozen = acquire_membership_correction_evidence(
            root,
            source_spec_path=spec_path,
            client=client,
            clock=lambda: _CLOCK,
        )
    return frozen.manifest_path


def _identity_bridge_manifest(root: Path, *, valid_from: str = "2019-01-01") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    sec_url = "https://www.sec.gov/Archives/edgar/data/1/alpha-successor.htm"
    payload = {
        "corrections": [
            {
                "action": "identity_bridge_only",
                "bridge_kind": "holding_company_successor",
                "correction_id": "alpha-reviewed-identity-split-v1",
                "edge_usage": "identity_relation_only",
                "effective_date": "2020-01-03",
                "expected_ticker_interval": {
                    "code": "AAA",
                    "end_exclusive": None,
                    "is_active_now": True,
                    "is_delisted": False,
                    "name": "Alpha Inc",
                    "start_inclusive": "2019-01-01",
                },
                "new_identity": {
                    "cik": "1",
                    "code": "AAA",
                    "is_active_now": True,
                    "is_delisted": False,
                    "name": "Alpha Inc",
                    "provider_symbol": "AAA",
                    "security_id": "figi:BBG000000001",
                    "valid_from": "2020-01-03",
                    "valid_to_exclusive": None,
                },
                "old_identity": {
                    "cik": "10",
                    "code": "OLD",
                    "is_active_now": False,
                    "is_delisted": True,
                    "name": "Old Alpha Inc",
                    "provider_symbol": "OLD",
                    "security_id": "isin:US0000000010",
                    "valid_from": "1980-01-01",
                    "valid_to_exclusive": "2020-01-03",
                },
                "rationale": "Primary evidence partitions a backcast successor ticker row.",
                "replacement_intervals": [],
                "source_ids": ["sec-alpha-successor"],
                "valid_from": valid_from,
                "valid_to_exclusive": None,
            }
        ],
        "review": {
            "reviewed_at": "2026-08-02T12:00:00Z",
            "reviewed_by": "vynmatrix personal research",
        },
        "schema": CORRECTION_SOURCE_SPEC_SCHEMA,
        "sources": [
            {
                "document_date": "2020-01-02",
                "kind": "sec_filing",
                "source_id": "sec-alpha-successor",
                "url": sec_url,
            }
        ],
    }
    spec_path = root / "identity-source.json"
    spec_path.write_bytes(canonical_json_bytes(payload) + b"\n")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>reviewed identity evidence</html>",
            headers={"Content-Type": "text/html"},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        frozen = acquire_membership_correction_evidence(
            root,
            source_spec_path=spec_path,
            client=client,
            clock=lambda: _CLOCK,
        )
    return frozen.manifest_path


def _configure_reused_apc(
    provider: _MembershipProvider,
    *,
    mapping_isin: str = "US0000000002",
) -> None:
    ticker_payload = json.loads(provider.ticker.content)
    ticker_payload["1"]["Code"] = "APC"
    ticker_payload["1"]["Name"] = "Anadarko Petroleum Corp"
    provider.ticker = _evidence(provider.ticker.endpoint, ticker_payload)

    snapshot_payload = json.loads(provider.snapshots.content)
    historical_row = snapshot_payload["HistoricalComponents"]["2019-12-20"]["1"]
    historical_row["Code"] = "APC"
    historical_row["Name"] = "Anadarko Petroleum Corp"
    provider.snapshots = _evidence(provider.snapshots.endpoint, snapshot_payload)

    active_payload = json.loads(provider.active.content)
    active_payload.append(
        {
            "Code": "APC",
            "Name": "Current APC Issuer",
            "Exchange": "NYSE",
            "Type": "Common Stock",
            "Isin": "US9999999999",
        }
    )
    provider.active = _evidence(provider.active.endpoint, active_payload)
    provider.delisted = _evidence(
        provider.delisted.endpoint,
        [
            {
                "Code": "APC_OLD",
                "Name": "  ANADARKO   PETROLEUM CORP ",
                "Exchange": "NYSE",
                "Type": "Common Stock",
                "Isin": "US0000000002",
            }
        ],
    )
    del provider.mappings["BBB"]
    provider.mappings.update(
        {
            "APC": _mapping(
                "APC",
                figi="BBG999999999",
                isin="US9999999999",
                cik="999",
            ),
            "APC_OLD": _mapping(
                "APC_OLD",
                figi="BBG000000002",
                isin=mapping_isin,
                cik="2",
            ),
        }
    )
    del provider.generals["BBB"]
    provider.generals["APC_OLD"] = _general(
        "APC_OLD",
        "Anadarko Petroleum Corp",
        ipo_date=None,
        is_delisted=True,
        isin=None,
        cik="2",
    )


def test_materializes_ticker_intervals_identity_and_raw_lineage(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    result = _materialize(tmp_path, provider)

    intervals = read_membership_intervals(result.membership_path)
    aliases = read_historical_aliases(
        result.aliases_path,
        provider=ValidationProvider.EODHD,
    )
    by_symbol = {interval.canonical_symbol: interval for interval in intervals}
    assert provider.mapping_calls == ["AAA", "BBB", "CCC"]
    assert provider.general_calls == ["AAA", "BBB", "CCC"]
    assert by_symbol["AAA"].effective_from == date(2020, 1, 1)
    assert by_symbol["AAA"].effective_to == date(2020, 1, 10)
    assert by_symbol["BBB"].effective_to == date(2020, 1, 2)
    assert by_symbol["CCC"].effective_from == date(2020, 1, 3)
    assert by_symbol["AAA"].security_id == "figi:BBG000000001"
    assert by_symbol["BBB"].security_id == "figi:BBG000000002"
    assert by_symbol["CCC"].security_id == "isin:US0000000003"
    alpha_listing = by_symbol["AAA"].listing_evidence
    assert alpha_listing is not None
    assert alpha_listing.status is ListingEvidenceStatus.BOUND
    assert alpha_listing.vendor_reported_ipo_date == date(1980, 1, 1)
    assert alpha_listing.provider_symbol == "AAA"
    assert alpha_listing.identity_binding == "exact_provider_symbol_code"
    assert alpha_listing.general_isin == "US0000000001"
    assert alpha_listing.general_cik == "0000000001"
    changed_listing = replace(alpha_listing, general_isin="US9999999999")
    assert changed_listing.evidence_id != alpha_listing.evidence_id
    assert (
        replace(by_symbol["AAA"], listing_evidence=changed_listing).interval_id
        != by_symbol["AAA"].interval_id
    )
    assert all(
        interval.source_authority is not None
        and interval.source_authority.kind is MembershipSourceKind.LICENSED_POINT_IN_TIME
        for interval in intervals
    )
    assert len(aliases) == 3
    gamma_alias = next(alias for alias in aliases if alias.canonical_symbol == "CCC")
    assert gamma_alias.effective_from == date(2020, 1, 1)
    assert gamma_alias.provider_symbol == "CCC"
    assert result.permanent_identity_complete is True
    assert result.lineage["historical_ticker_missing_start_count"] == 1
    assert result.lineage["maximum_change_event_gap_days"] == 14
    assert len(result.evidence_artifacts) == 14
    assert {
        "eodhd_active_symbol_directory_response",
        "eodhd_delisted_symbol_directory_response",
        "eodhd_id_mapping_response",
        "eodhd_index_historical_components_response",
        "eodhd_index_historical_ticker_components_response",
        "eodhd_membership_authority_crosscheck",
        "eodhd_membership_identity_resolutions",
        "eodhd_membership_materialization_manifest",
        "eodhd_security_fundamentals_general_response",
        "eodhd_us_symbol_change_history_response",
    } == {artifact.role for artifact in result.evidence_artifacts}
    for artifact in result.evidence_artifacts:
        assert verified_content_path(tmp_path, artifact).is_file()
        assert "api_token" not in str(artifact.context)
    assert result.lineage["fundamentals_general_response_count"] == 3
    assert result.lineage["fundamentals_general_ipo_date_count"] == 3
    assert result.lineage["identity_policy_version"] == ("sp500-eodhd-pit-membership-identity-v8")
    assert result.lineage["ticker_history_is_membership_authority"] is True
    assert result.lineage["ticker_history_start_date_semantics"] == "inclusive"
    assert result.lineage["ticker_history_end_date_semantics"] == "exclusive"
    assert result.membership_authority_complete is True
    assert "/api/v1.1/fundamentals/AAA.US?filter=General&fmt=json#sha256=" in (
        by_symbol["AAA"].source_ref
    )


def test_partial_typed_listing_evidence_columns_fail_closed(tmp_path: Path) -> None:
    result = _materialize(tmp_path / "source", _MembershipProvider())
    malformed = tmp_path / "membership.csv"
    malformed.write_text(
        result.membership_path.read_text(encoding="utf-8").replace(
            "listing_general_cik",
            "unregistered_listing_general_cik",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        SnapshotValidationError,
        match="membership listing-evidence fields are incomplete",
    ):
        read_membership_intervals(malformed)


def test_nullable_general_fields_retain_membership_without_lifetime_claim(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    provider.generals["BBB"] = _evidence(
        provider.generals["BBB"].endpoint,
        {
            "CIK": None,
            "Code": None,
            "IPODate": None,
            "ISIN": None,
            "IsDelisted": None,
            "Name": None,
            "UpdatedAt": None,
        },
    )

    result = _materialize(tmp_path, provider)

    assert result.permanent_identity_complete is True
    assert result.lineage["fundamentals_general_ipo_date_count"] == 2
    beta = next(
        interval
        for interval in read_membership_intervals(result.membership_path)
        if interval.canonical_symbol == "BBB"
    )
    assert "general_identity_binding=nullable_or_unbound_without_lifetime_claim" in beta.notes
    assert beta.listing_evidence is not None
    assert beta.listing_evidence.status is ListingEvidenceStatus.UNAVAILABLE
    assert beta.listing_evidence.vendor_reported_ipo_date is None
    artifact = next(
        item
        for item in result.evidence_artifacts
        if item.role == "eodhd_security_fundamentals_general_response"
        and item.context["requested_symbol"] == "BBB.US"
    )
    assert (
        verified_content_path(tmp_path, artifact).read_bytes() == provider.generals["BBB"].content
    )


@pytest.mark.parametrize(
    ("isin", "cik", "binding"),
    [
        ("US0000000001", None, "matching_permanent_isin"),
        (None, "1", "matching_sec_cik"),
    ],
)
def test_general_ipo_date_can_bind_through_matching_permanent_identifier(
    tmp_path: Path,
    isin: str | None,
    cik: str | None,
    binding: str,
) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.generals["AAA"].content)
    payload.update({"CIK": cik, "Code": None, "ISIN": isin})
    provider.generals["AAA"] = _evidence(provider.generals["AAA"].endpoint, payload)

    result = _materialize(tmp_path, provider)

    alpha = next(
        interval
        for interval in read_membership_intervals(result.membership_path)
        if interval.canonical_symbol == "AAA"
    )
    assert alpha.listing_evidence is not None
    assert alpha.listing_evidence.identity_binding == binding
    assert alpha.listing_evidence.status is ListingEvidenceStatus.BOUND


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("Code", [], "must be a string or null"),
        ("Name", 17, "must be a string or null"),
        ("IPODate", "2020/01/01", "must be an ISO-8601 date or null"),
        ("IsDelisted", "false", "must be a boolean or zero/one flag"),
        ("UpdatedAt", "yesterday", "must be an ISO-8601 date or null"),
        ("ISIN", "not-an-isin", "must be a 12-character ISIN or null"),
        ("CIK", "not-a-cik", "must contain at most 10 digits or null"),
    ],
)
def test_malformed_present_general_fields_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
    error: str,
) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.generals["AAA"].content)
    payload[field] = value
    provider.generals["AAA"] = _evidence(provider.generals["AAA"].endpoint, payload)

    with pytest.raises(EODHDMembershipMaterializationError, match=error):
        _materialize(tmp_path, provider)


def test_general_code_must_match_resolved_provider_symbol(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.generals["AAA"].content)
    payload["Code"] = "ZZZ"
    provider.generals["AAA"] = _evidence(provider.generals["AAA"].endpoint, payload)

    with pytest.raises(
        EODHDMembershipMaterializationError,
        match="Code ZZZ does not match requested provider symbol AAA",
    ):
        _materialize(tmp_path, provider)


@pytest.mark.parametrize(
    ("field", "value"),
    [("ISIN", "US9999999999"), ("CIK", "999")],
)
def test_general_identifier_conflict_fails_with_raw_evidence_reference(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.generals["AAA"].content)
    payload[field] = value
    provider.generals["AAA"] = _evidence(provider.generals["AAA"].endpoint, payload)

    with pytest.raises(
        EODHDMembershipMaterializationError,
        match="identity contradicts selected directory/ID mapping for AAA",
    ) as exc_info:
        _materialize(tmp_path, provider)

    assert provider.generals["AAA"].content_sha256 in str(exc_info.value)
    assert provider.generals["AAA"].endpoint in str(exc_info.value)


def test_general_isin_variant_present_in_mapping_does_not_override_directory(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    endpoint = provider.mappings["AAA"].endpoint
    provider.mappings["AAA"] = _evidence(
        endpoint,
        {
            "meta": {"limit": 100, "offset": 0, "total": 2},
            "data": [
                {
                    "symbol": "AAA.US",
                    "isin": "US0000000001",
                    "cik": "1",
                },
                {
                    "symbol": "AAA.US",
                    "isin": "US9999999999",
                    "cik": "1",
                },
            ],
            "links": {"next": None},
        },
    )

    result = _materialize(tmp_path, provider)
    alpha = next(
        interval
        for interval in read_membership_intervals(result.membership_path)
        if interval.canonical_symbol == "AAA"
    )

    assert alpha.security_id == "isin:US0000000001"


def test_unbound_general_ipo_date_fails_closed(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.generals["AAA"].content)
    payload.update({"CIK": None, "Code": None, "ISIN": None, "IPODate": "2024-01-01"})
    provider.generals["AAA"] = _evidence(provider.generals["AAA"].endpoint, payload)

    with pytest.raises(
        EODHDMembershipMaterializationError,
        match="IPODate is not positively bound",
    ):
        _materialize(tmp_path, provider)


def test_pre_ipo_membership_interval_fails_without_truncation(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.generals["AAA"].content)
    payload["IPODate"] = "2024-07-08"
    provider.generals["AAA"] = _evidence(provider.generals["AAA"].endpoint, payload)

    with pytest.raises(
        EODHDMembershipMaterializationError,
        match=r"membership interval begins before General\.IPODate",
    ) as exc_info:
        _materialize(tmp_path, provider)

    error = str(exc_info.value)
    assert "component=AAA" in error
    assert "effective_from=2020-01-01" in error
    assert "ipo_date=2024-07-08" in error
    assert provider.generals["AAA"].content_sha256 in error


def test_symbol_change_history_uses_successor_storage_with_shared_identity(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    provider.symbol_changes = _evidence(
        "/api/symbol-change-history?from=2018-01-01&to=2020-01-10&ex=US&fmt=json",
        [
            {
                "exchange": "US",
                "old_symbol": "BBB",
                "new_symbol": "CCC",
                "company_name": "Gamma Inc",
                "effective": "2020-01-03",
            }
        ],
    )

    result = _materialize(tmp_path, provider)
    intervals = read_membership_intervals(result.membership_path)
    aliases = read_historical_aliases(
        result.aliases_path,
        provider=ValidationProvider.EODHD,
    )

    assert provider.mapping_calls == ["AAA", "CCC"]
    assert provider.general_calls == ["AAA", "CCC"]
    assert {item.security_id for item in intervals if item.canonical_symbol in {"BBB", "CCC"}} == {
        "isin:US0000000003"
    }
    assert {
        alias.provider_symbol for alias in aliases if alias.canonical_symbol in {"BBB", "CCC"}
    } == {"CCC"}
    assert result.lineage["source_backed_symbol_change_alias_codes"] == ["BBB"]
    assert result.lineage["directory_alias_inference_codes"] == []


def test_dated_symbol_recurrence_replays_to_current_storage_symbol(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    provider.symbol_changes = _evidence(
        "/api/symbol-change-history?from=2018-01-01&to=2020-01-10&ex=US&fmt=json",
        [
            {
                "exchange": "US",
                "old_symbol": "BBB",
                "new_symbol": "CCC",
                "company_name": "Gamma Inc",
                "effective": "2020-01-03",
            },
            {
                "exchange": "US",
                "old_symbol": "CCC",
                "new_symbol": "BBB",
                "company_name": "Gamma Inc",
                "effective": "2020-01-06",
            },
        ],
    )

    result = _materialize(tmp_path, provider)
    aliases = read_historical_aliases(
        result.aliases_path,
        provider=ValidationProvider.EODHD,
    )

    assert provider.mapping_calls == ["AAA", "BBB"]
    assert {
        alias.provider_symbol for alias in aliases if alias.canonical_symbol in {"BBB", "CCC"}
    } == {"BBB"}


def test_irrelevant_reused_symbol_change_does_not_block_membership(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    provider.symbol_changes = _evidence(
        "/api/symbol-change-history?from=2018-01-01&to=2020-01-10&ex=US&fmt=json",
        [
            {
                "exchange": "US",
                "old_symbol": "CEAD",
                "new_symbol": "VAPE",
                "company_name": "CEA Industries Inc. Common Stock",
                "effective": "2025-06-13",
            },
            {
                "exchange": "US",
                "old_symbol": "CEAD",
                "new_symbol": "BNC",
                "company_name": "CEA Industries Inc. Common Stock",
                "effective": "2025-08-06",
            },
        ],
    )

    result = _materialize(tmp_path, provider)

    assert result.lineage["symbol_change_history_count"] == 2


def test_irrelevant_noncanonical_symbol_change_is_retained_but_not_used(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    provider.symbol_changes = _evidence(
        "/api/symbol-change-history?from=2018-01-01&to=2020-01-10&ex=US&fmt=json",
        [
            {
                "exchange": "US",
                "old_symbol": "COPP B",
                "new_symbol": "COPP",
                "company_name": "Sprott Copper Miners ETF",
                "effective": "2023-10-26",
            }
        ],
    )

    result = _materialize(tmp_path, provider)

    assert result.lineage["symbol_change_history_count"] == 1
    assert result.lineage["symbol_change_noncanonical_symbol_count"] == 1


def test_ambiguous_reused_symbol_change_for_member_fails_closed(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    provider.symbol_changes = _evidence(
        "/api/symbol-change-history?from=2018-01-01&to=2020-01-10&ex=US&fmt=json",
        [
            {
                "exchange": "US",
                "old_symbol": "BBB",
                "new_symbol": "CCC",
                "company_name": "Beta Inc",
                "effective": "2020-01-03",
            },
            {
                "exchange": "US",
                "old_symbol": "BBB",
                "new_symbol": "DDD",
                "company_name": "Beta Inc",
                "effective": "2020-01-06",
            },
        ],
    )

    with pytest.raises(
        EODHDMembershipMaterializationError,
        match="symbol-change history is ambiguous for membership component BBB",
    ):
        _materialize(tmp_path, provider)


def test_unresolved_identity_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    provider.mappings["BBB"] = _mapping("BBB")
    result = _materialize(tmp_path, provider)

    by_symbol = {
        interval.canonical_symbol: interval
        for interval in read_membership_intervals(result.membership_path)
    }
    assert by_symbol["BBB"].security_id.startswith("eodhd-unresolved:")
    assert result.permanent_identity_complete is False
    assert result.lineage["unresolved_identity_codes"] == ["BBB"]


def test_exact_directory_name_resolves_inactive_provider_symbol(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    directory_payload = json.loads(provider.delisted.content)
    directory_payload[0]["Code"] = "BBB_OLD"
    provider.delisted = _evidence(provider.delisted.endpoint, directory_payload)
    provider.mappings["BBB_OLD"] = _mapping(
        "BBB_OLD",
        figi="BBG000000002",
        cik="2",
    )
    del provider.mappings["BBB"]
    provider.generals["BBB_OLD"] = _general(
        "BBB_OLD",
        "Beta Inc",
        ipo_date=None,
        is_delisted=True,
        isin=None,
        cik="2",
    )
    del provider.generals["BBB"]

    result = _materialize(tmp_path, provider)
    aliases = read_historical_aliases(
        result.aliases_path,
        provider=ValidationProvider.EODHD,
    )
    beta_alias = next(alias for alias in aliases if alias.canonical_symbol == "BBB")

    assert beta_alias.provider_symbol == "BBB_OLD"
    assert provider.mapping_calls == ["AAA", "BBB_OLD", "CCC"]


def test_reused_active_code_uses_unique_name_matched_storage_symbol(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    _configure_reused_apc(provider)

    result = _materialize(tmp_path, provider)
    intervals = read_membership_intervals(result.membership_path)
    aliases = read_historical_aliases(
        result.aliases_path,
        provider=ValidationProvider.EODHD,
    )
    apc_interval = next(item for item in intervals if item.canonical_symbol == "APC")
    apc_alias = next(item for item in aliases if item.canonical_symbol == "APC")

    assert apc_alias.provider_symbol == "APC_OLD"
    assert apc_interval.security_id == "figi:BBG000000002"
    assert provider.mapping_calls == ["AAA", "APC_OLD", "CCC"]
    assert result.lineage["directory_alias_inference_codes"] == ["APC"]


def test_matching_exact_code_remains_preferred_over_name_alias(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    directory_payload = json.loads(provider.delisted.content)
    directory_payload.append(
        {
            "Code": "BBB_OLD",
            "Name": "Beta Inc",
            "Exchange": "NYSE",
            "Type": "Common Stock",
            "Isin": "US0000000099",
        }
    )
    provider.delisted = _evidence(provider.delisted.endpoint, directory_payload)
    provider.mappings["BBB_OLD"] = _mapping(
        "BBB_OLD",
        figi="BBG000000099",
        isin="US0000000099",
        cik="99",
    )

    result = _materialize(tmp_path, provider)
    aliases = read_historical_aliases(
        result.aliases_path,
        provider=ValidationProvider.EODHD,
    )
    beta_alias = next(item for item in aliases if item.canonical_symbol == "BBB")

    assert beta_alias.provider_symbol == "BBB"
    assert provider.mapping_calls == ["AAA", "BBB", "CCC"]


def test_ambiguous_name_matched_storage_symbols_fail_before_id_lookup(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    _configure_reused_apc(provider)
    directory_payload = json.loads(provider.delisted.content)
    directory_payload.append(
        {
            "Code": "APC_OLD2",
            "Name": "Anadarko Petroleum Corp",
            "Exchange": "NYSE",
            "Type": "Common Stock",
            "Isin": "US0000000022",
        }
    )
    provider.delisted = _evidence(provider.delisted.endpoint, directory_payload)

    with pytest.raises(
        EODHDMembershipMaterializationError,
        match="ambiguous exact component-name storage aliases for APC",
    ):
        _materialize(tmp_path, provider)
    assert provider.mapping_calls == ["AAA"]


def test_name_matched_storage_identity_conflict_remains_unresolved(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    _configure_reused_apc(provider, mapping_isin="US0000000099")

    result = _materialize(tmp_path, provider)
    intervals = read_membership_intervals(result.membership_path)
    apc_interval = next(item for item in intervals if item.canonical_symbol == "APC")

    assert apc_interval.security_id.startswith("eodhd-unresolved:")
    assert result.permanent_identity_complete is False
    assert result.lineage["unresolved_identity_codes"] == ["APC"]
    assert provider.mapping_calls == ["AAA", "APC_OLD", "CCC"]


def test_permanent_identity_stitches_prehistory_across_ticker_change(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    provider.ticker = _evidence(
        provider.ticker.endpoint,
        {
            "0": _history(
                "OLD",
                "Issuer Inc",
                start="2019-01-01",
                end="2020-01-03",
                active=0,
                delisted=1,
            ),
            "1": _history(
                "NEW",
                "Issuer Inc",
                start="2020-01-03",
                end=None,
                active=1,
                delisted=0,
            ),
        },
    )
    provider.snapshots = _evidence(
        provider.snapshots.endpoint,
        {
            "HistoricalComponents": {
                "2019-12-20": {"0": _component("OLD", "Issuer Inc", "2019-12-20")},
                "2020-01-03": {"0": _component("NEW", "Issuer Inc", "2020-01-03")},
            }
        },
    )
    provider.active = _evidence(
        provider.active.endpoint,
        [
            {
                "Code": "NEW",
                "Name": "Issuer Inc",
                "Exchange": "NYSE",
                "Type": "Common Stock",
                "Isin": "US0000000010",
            }
        ],
    )
    provider.delisted = _evidence(
        provider.delisted.endpoint,
        [
            {
                "Code": "OLD",
                "Name": "Issuer Inc",
                "Exchange": "NYSE",
                "Type": "Common Stock",
                "Isin": "US0000000010",
            }
        ],
    )
    provider.mappings = {
        "NEW": _mapping("NEW", figi="BBG000000010", isin="US0000000010"),
        "OLD": _mapping("OLD", figi="BBG000000010", isin="US0000000010"),
    }
    provider.generals = {
        "NEW": _general(
            "NEW",
            "Issuer Inc",
            ipo_date="1990-01-01",
            is_delisted=False,
            isin="US0000000010",
            cik=None,
        ),
        "OLD": _general(
            "OLD",
            "Issuer Inc",
            ipo_date="1990-01-01",
            is_delisted=True,
            isin="US0000000010",
            cik=None,
        ),
    }

    result = _materialize(tmp_path, provider)
    intervals = read_membership_intervals(result.membership_path)
    aliases = read_historical_aliases(
        result.aliases_path,
        provider=ValidationProvider.EODHD,
    )
    new_aliases = [alias for alias in aliases if alias.canonical_symbol == "NEW"]

    assert {interval.security_id for interval in intervals} == {"figi:BBG000000010"}
    assert [
        (alias.provider_symbol, alias.effective_from, alias.effective_to) for alias in new_aliases
    ] == [
        ("OLD", date(2020, 1, 1), date(2020, 1, 2)),
        ("NEW", date(2020, 1, 3), date(2020, 1, 10)),
    ]


def test_embedded_snapshot_date_mismatch_fails_before_identity_calls(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.snapshots.content)
    payload["HistoricalComponents"]["2020-01-03"]["0"]["Date"] = "2020-01-04"
    provider.snapshots = _evidence(provider.snapshots.endpoint, payload)

    with pytest.raises(EODHDMembershipMaterializationError, match="embedded Date"):
        _materialize(tmp_path, provider)
    assert provider.mapping_calls == []


def test_ticker_bounds_are_authority_and_snapshot_conflict_is_typed(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.ticker.content)
    payload["0"]["StartDate"] = "2020-01-04"
    provider.ticker = _evidence(provider.ticker.endpoint, payload)

    result = _materialize(tmp_path, provider)
    intervals = read_membership_intervals(result.membership_path)
    alpha = next(interval for interval in intervals if interval.canonical_symbol == "AAA")
    crosscheck = next(
        artifact
        for artifact in result.evidence_artifacts
        if artifact.role == "eodhd_membership_authority_crosscheck"
    )
    crosscheck_payload = json.loads(verified_content_path(tmp_path, crosscheck).read_bytes())

    assert alpha.effective_from == date(2020, 1, 4)
    assert result.lineage["ticker_history_is_membership_authority"] is True
    assert result.membership_authority_complete is False
    assert result.lineage["unresolved_snapshot_only_codes"] == ["AAA"]
    assert {
        row["disposition"] for row in crosscheck_payload["discrepancies"] if row["code"] == "AAA"
    } == {"snapshot_only_no_authoritative_ticker_interval"}
    assert provider.mapping_calls == ["AAA", "BBB", "CCC"]


def test_snapshot_component_missing_from_ticker_authority_is_not_backfilled(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.ticker.content)
    del payload["1"]
    provider.ticker = _evidence(provider.ticker.endpoint, payload)

    result = _materialize(tmp_path, provider)
    intervals = read_membership_intervals(result.membership_path)

    assert {interval.canonical_symbol for interval in intervals} == {"AAA", "CCC"}
    assert result.lineage["unresolved_snapshot_only_codes"] == ["BBB"]
    assert result.membership_authority_complete is False
    assert result.permanent_identity_complete is True
    assert provider.mapping_calls == ["AAA", "CCC"]


def test_one_matching_post_end_snapshot_is_typed_checkpoint_lag(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    ticker = json.loads(provider.ticker.content)
    ticker["1"]["EndDate"] = "2020-01-02"
    provider.ticker = _evidence(provider.ticker.endpoint, ticker)
    snapshots = json.loads(provider.snapshots.content)
    snapshots["HistoricalComponents"]["2020-01-02"] = {
        "0": _component("AAA", "Alpha Inc", "2020-01-02"),
        "1": _component("BBB", "Beta Inc", "2020-01-02"),
    }
    provider.snapshots = _evidence(provider.snapshots.endpoint, snapshots)

    result = _materialize(tmp_path, provider)

    assert result.membership_authority_complete is True
    assert result.lineage["membership_crosscheck_disposition_counts"] == {
        "snapshot_checkpoint_lag_after_ticker_end": 1
    }


def test_post_end_snapshot_name_reuse_is_not_treated_as_checkpoint_lag(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    ticker = json.loads(provider.ticker.content)
    ticker["1"]["EndDate"] = "2020-01-02"
    provider.ticker = _evidence(provider.ticker.endpoint, ticker)
    snapshots = json.loads(provider.snapshots.content)
    snapshots["HistoricalComponents"]["2020-01-02"] = {
        "0": _component("AAA", "Alpha Inc", "2020-01-02"),
        "1": _component("BBB", "Replacement Issuer", "2020-01-02"),
    }
    provider.snapshots = _evidence(provider.snapshots.endpoint, snapshots)

    result = _materialize(tmp_path, provider)

    assert result.membership_authority_complete is False
    assert result.lineage["unresolved_snapshot_only_codes"] == ["BBB"]


def test_overlapping_explicit_ticker_histories_fail_closed(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.ticker.content)
    payload["0"]["StartDate"] = "2019-01-01"
    payload["0"]["EndDate"] = "2021-01-01"
    payload["3"] = _history(
        "AAA",
        "Different Issuer",
        start="2019-06-01",
        end="2020-06-01",
        active=0,
        delisted=1,
    )
    provider.ticker = _evidence(provider.ticker.endpoint, payload)

    with pytest.raises(EODHDMembershipMaterializationError, match="conflicting authoritative"):
        _materialize(tmp_path, provider)
    assert provider.mapping_calls == []


def test_snapshot_name_reuse_never_relabels_ticker_authority(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.snapshots.content)
    payload["HistoricalComponents"]["2020-01-03"] = {
        "0": _component("CCC", "Gamma Inc", "2020-01-03")
    }
    payload["HistoricalComponents"]["2020-01-06"] = {
        "0": _component("AAA", "Replacement Inc", "2020-01-06"),
        "1": _component("CCC", "Gamma Inc", "2020-01-06"),
    }
    provider.snapshots = _evidence(provider.snapshots.endpoint, payload)

    result = _materialize(tmp_path, provider)
    intervals = read_membership_intervals(result.membership_path)
    alpha = next(interval for interval in intervals if interval.canonical_symbol == "AAA")

    assert "component_names=['Alpha Inc']" in alpha.notes
    assert "Replacement Inc" not in alpha.notes
    assert result.membership_authority_complete is False
    assert provider.mapping_calls == ["AAA", "BBB", "CCC"]


def test_one_day_ticker_row_proves_end_date_is_exclusive(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.ticker.content)
    payload["0"]["StartDate"] = "2020-01-04"
    payload["0"]["EndDate"] = "2020-01-05"
    provider.ticker = _evidence(provider.ticker.endpoint, payload)

    result = _materialize(tmp_path, provider)
    intervals = read_membership_intervals(result.membership_path)
    alpha = next(interval for interval in intervals if interval.canonical_symbol == "AAA")

    assert alpha.effective_from == date(2020, 1, 4)
    assert alpha.effective_to == date(2020, 1, 4)


def test_frozen_reviewed_correction_replaces_one_raw_row_with_many_intervals(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.ticker.content)
    payload["0"]["StartDate"] = "2020-01-04"
    provider.ticker = _evidence(provider.ticker.endpoint, payload)
    corrected_intervals = [
        {
            "code": "AAA",
            "end_exclusive": "2020-01-05",
            "is_active_now": False,
            "is_delisted": False,
            "name": "Alpha Inc",
            "start_inclusive": "2019-12-20",
        },
        {
            "code": "AAA",
            "end_exclusive": None,
            "is_active_now": True,
            "is_delisted": False,
            "name": "Alpha Inc",
            "start_inclusive": "2020-01-05",
        },
    ]
    manifest = _authority_correction_manifest(
        tmp_path,
        expected_start="2020-01-04",
        replacements=corrected_intervals,
        reviewed_security_id="isin:US0000000001",
    )

    result = _materialize(
        tmp_path,
        provider,
        authority_correction_manifest_path=manifest,
    )
    alpha = [
        interval
        for interval in read_membership_intervals(result.membership_path)
        if interval.canonical_symbol == "AAA"
    ]

    assert [(item.effective_from, item.effective_to) for item in alpha] == [
        (date(2020, 1, 1), date(2020, 1, 4)),
        (date(2020, 1, 5), date(2020, 1, 10)),
    ]
    assert result.membership_authority_complete is True
    assert result.lineage["reviewed_correction_count"] == 1
    assert len(result.lineage["reviewed_correction_artifact_sha256s"]) == 4
    assert all("alpha-reviewed-primary-correction-v1" in item.source_ref for item in alpha)
    assert {
        "eodhd_membership_correction_source_spec",
        "eodhd_membership_correction_evidence_manifest",
        "eodhd_membership_primary_source_document",
    } <= {artifact.role for artifact in result.evidence_artifacts}
    conflict_root = tmp_path / "conflict"
    conflicting_manifest = _authority_correction_manifest(
        conflict_root,
        expected_start="2020-01-04",
        replacements=corrected_intervals,
        reviewed_security_id="isin:US9999999999",
    )
    with pytest.raises(EODHDMembershipMaterializationError, match="permanent identity"):
        _materialize(
            conflict_root,
            provider,
            authority_correction_manifest_path=conflicting_manifest,
        )


def test_reviewed_primary_listing_date_overrides_conflicting_vendor_ipo_date(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    ticker_payload = json.loads(provider.ticker.content)
    ticker_payload["0"]["StartDate"] = "2020-01-04"
    provider.ticker = _evidence(provider.ticker.endpoint, ticker_payload)
    general_payload = json.loads(provider.generals["AAA"].content)
    general_payload["IPODate"] = "2024-07-08"
    provider.generals["AAA"] = _evidence(provider.generals["AAA"].endpoint, general_payload)
    corrected_interval = {
        "code": "AAA",
        "end_exclusive": None,
        "is_active_now": True,
        "is_delisted": False,
        "name": "Alpha Inc",
        "start_inclusive": "2019-12-20",
    }
    manifest = _authority_correction_manifest(
        tmp_path,
        expected_start="2020-01-04",
        replacements=[corrected_interval],
        reviewed_listing_date="2019-12-20",
        reviewed_identity_endpoint=True,
    )

    result = _materialize(
        tmp_path,
        provider,
        authority_correction_manifest_path=manifest,
    )
    alpha = next(
        interval
        for interval in read_membership_intervals(result.membership_path)
        if interval.canonical_symbol == "AAA"
    )

    assert alpha.listing_evidence is not None
    assert alpha.listing_evidence.vendor_reported_ipo_date == date(2024, 7, 8)
    assert alpha.listing_evidence.reviewed_listing_date == date(2019, 12, 20)
    assert alpha.listing_evidence.effective_listing_date == date(2019, 12, 20)
    assert alpha.listing_evidence.identity_binding == "reviewed_primary_class_identity"
    assert alpha.listing_evidence.reviewed_listing_correction_manifest_sha256 == manifest.stem


def test_reviewed_identity_endpoint_resolves_snapshot_name_contamination(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    ticker_payload = json.loads(provider.ticker.content)
    ticker_payload["0"]["StartDate"] = "2020-01-04"
    provider.ticker = _evidence(provider.ticker.endpoint, ticker_payload)
    active_payload = json.loads(provider.active.content)
    active_payload[0]["Name"] = "Reviewed Alpha Inc"
    provider.active = _evidence(provider.active.endpoint, active_payload)
    provider.generals["AAA"] = _general(
        "AAA",
        "Reviewed Alpha Inc",
        ipo_date="1980-01-01",
        is_delisted=False,
        isin="US0000000001",
        cik="1",
    )
    replacement = {
        "code": "AAA",
        "end_exclusive": None,
        "is_active_now": True,
        "is_delisted": False,
        "name": "Reviewed Alpha Inc",
        "start_inclusive": "2019-12-20",
    }
    manifest = _authority_correction_manifest(
        tmp_path,
        expected_start="2020-01-04",
        replacements=[replacement],
        reviewed_identity_endpoint=True,
        reviewed_identity_name="Reviewed Alpha Inc",
    )

    result = _materialize(
        tmp_path,
        provider,
        authority_correction_manifest_path=manifest,
    )

    assert result.membership_authority_complete is True
    assert result.lineage["unresolved_membership_crosscheck_codes"] == []
    assert result.lineage["membership_crosscheck_disposition_counts"] == {
        "snapshot_name_overridden_by_reviewed_identity": 2
    }


def test_reviewed_identity_edge_splits_backcast_identity_without_price_stitch(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    ticker_payload = json.loads(provider.ticker.content)
    ticker_payload["0"]["StartDate"] = "2019-01-01"
    provider.ticker = _evidence(provider.ticker.endpoint, ticker_payload)
    delisted_payload = json.loads(provider.delisted.content)
    delisted_payload.append(
        {
            "Code": "OLD",
            "Exchange": "NYSE",
            "Isin": "US0000000010",
            "Name": "Old Alpha Inc",
            "Type": "Common Stock",
        }
    )
    provider.delisted = _evidence(provider.delisted.endpoint, delisted_payload)
    provider.mappings["OLD"] = _mapping("OLD", isin="US0000000010", cik="10")
    provider.generals["OLD"] = _general(
        "OLD",
        "Old Alpha Inc",
        ipo_date="1980-01-01",
        is_delisted=True,
        isin="US0000000010",
        cik="10",
    )
    manifest = _identity_bridge_manifest(tmp_path)

    result = _materialize(tmp_path, provider, authority_correction_manifest_path=manifest)
    intervals = read_membership_intervals(result.membership_path)
    aliases = read_historical_aliases(result.aliases_path, provider=ValidationProvider.EODHD)
    old = next(interval for interval in intervals if interval.canonical_symbol == "OLD")
    new = next(interval for interval in intervals if interval.canonical_symbol == "AAA")

    assert (old.effective_from, old.effective_to, old.security_id) == (
        date(2020, 1, 1),
        date(2020, 1, 2),
        "isin:US0000000010",
    )
    assert (new.effective_from, new.effective_to, new.security_id) == (
        date(2020, 1, 3),
        date(2020, 1, 10),
        "figi:BBG000000001",
    )
    assert min(
        alias.effective_from for alias in aliases if alias.canonical_symbol == "AAA"
    ) == date(2020, 1, 3)
    assert all(
        alias.security_id != old.security_id for alias in aliases if alias.canonical_symbol == "AAA"
    )
    assert result.identity_edges_path is not None
    identity_payload = json.loads(result.identity_edges_path.read_bytes())
    assert identity_payload["edges"][0]["membership_coverage_changed"] is False
    assert identity_payload["edges"][0]["price_stitch_allowed"] is False
    assert result.membership_authority_complete is True
    assert result.lineage["unresolved_membership_crosscheck_codes"] == []
    assert (
        result.lineage["membership_crosscheck_disposition_counts"][
            "snapshot_successor_code_backcast_over_reviewed_identity_partition"
        ]
        == 1
    )
    assert (
        result.lineage["membership_crosscheck_disposition_counts"][
            "authoritative_ticker_interval_absent_from_snapshot_checkpoint"
        ]
        == 1
    )
    assert result.lineage["historical_membership_publication_availability_complete"] is False


def test_reviewed_identity_edge_rejects_request_before_reviewed_scope(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.ticker.content)
    payload["0"]["StartDate"] = "2019-01-01"
    provider.ticker = _evidence(provider.ticker.endpoint, payload)
    manifest = _identity_bridge_manifest(tmp_path, valid_from="2020-01-02")

    with pytest.raises(EODHDMembershipMaterializationError, match="reviewed validity"):
        _materialize(tmp_path, provider, authority_correction_manifest_path=manifest)


def test_reviewed_identity_backcast_does_not_waive_unrelated_snapshot_code(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    ticker_payload = json.loads(provider.ticker.content)
    ticker_payload["0"]["StartDate"] = "2019-01-01"
    provider.ticker = _evidence(provider.ticker.endpoint, ticker_payload)
    snapshot_payload = json.loads(provider.snapshots.content)
    snapshot_payload["HistoricalComponents"]["2019-12-20"]["9"] = _component(
        "ZZZ",
        "Alpha Inc",
        "2019-12-20",
    )
    provider.snapshots = _evidence(provider.snapshots.endpoint, snapshot_payload)
    delisted_payload = json.loads(provider.delisted.content)
    delisted_payload.append(
        {
            "Code": "OLD",
            "Exchange": "NYSE",
            "Isin": "US0000000010",
            "Name": "Old Alpha Inc",
            "Type": "Common Stock",
        }
    )
    provider.delisted = _evidence(provider.delisted.endpoint, delisted_payload)
    provider.mappings["OLD"] = _mapping("OLD", isin="US0000000010", cik="10")
    provider.generals["OLD"] = _general(
        "OLD",
        "Old Alpha Inc",
        ipo_date="1980-01-01",
        is_delisted=True,
        isin="US0000000010",
        cik="10",
    )

    result = _materialize(
        tmp_path,
        provider,
        authority_correction_manifest_path=_identity_bridge_manifest(tmp_path),
    )

    assert result.membership_authority_complete is False
    assert result.lineage["unresolved_membership_crosscheck_codes"] == ["ZZZ"]
    assert (
        result.lineage["membership_crosscheck_disposition_counts"][
            "snapshot_successor_code_backcast_over_reviewed_identity_partition"
        ]
        == 1
    )


def test_frozen_reviewed_correction_can_remove_exact_bad_row_without_backfill(
    tmp_path: Path,
) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.ticker.content)
    payload["0"]["StartDate"] = "2020-01-04"
    provider.ticker = _evidence(provider.ticker.endpoint, payload)
    manifest = _authority_correction_manifest(
        tmp_path,
        expected_start="2020-01-04",
        replacements=[],
    )

    result = _materialize(
        tmp_path,
        provider,
        authority_correction_manifest_path=manifest,
    )

    assert {
        interval.canonical_symbol for interval in read_membership_intervals(result.membership_path)
    } == {"BBB", "CCC"}
    assert result.membership_authority_complete is False
    assert result.lineage["unresolved_snapshot_only_codes"] == ["AAA"]


def test_reviewed_correction_raw_row_precondition_must_match_exactly(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.ticker.content)
    payload["0"]["StartDate"] = "2020-01-04"
    provider.ticker = _evidence(provider.ticker.endpoint, payload)
    manifest = _authority_correction_manifest(
        tmp_path,
        expected_start="2020-01-05",
        replacements=[],
    )

    with pytest.raises(
        EODHDMembershipMaterializationError,
        match="expected exactly one raw ticker-history row, observed 0",
    ):
        _materialize(
            tmp_path,
            provider,
            authority_correction_manifest_path=manifest,
        )
    assert provider.mapping_calls == []


def test_null_start_requires_matching_earliest_snapshot_baseline(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.snapshots.content)
    del payload["HistoricalComponents"]["2019-12-20"]["0"]
    provider.snapshots = _evidence(provider.snapshots.endpoint, payload)

    with pytest.raises(
        EODHDMembershipMaterializationError,
        match="null StartDate without exact-code earliest full-state baseline proof",
    ):
        _materialize(tmp_path, provider)
    assert provider.mapping_calls == []


def test_reviewed_correction_can_replace_exact_null_start_row(tmp_path: Path) -> None:
    provider = _MembershipProvider()
    payload = json.loads(provider.snapshots.content)
    del payload["HistoricalComponents"]["2019-12-20"]["0"]
    provider.snapshots = _evidence(provider.snapshots.endpoint, payload)
    general_payload = json.loads(provider.generals["AAA"].content)
    general_payload["IPODate"] = "2024-07-08"
    provider.generals["AAA"] = _evidence(provider.generals["AAA"].endpoint, general_payload)
    manifest = _authority_correction_manifest(
        tmp_path,
        expected_start=None,
        replacements=[
            {
                "code": "AAA",
                "end_exclusive": None,
                "is_active_now": True,
                "is_delisted": False,
                "name": "Alpha Inc",
                "start_inclusive": "2019-12-20",
            }
        ],
        reviewed_identity_endpoint=True,
    )

    result = _materialize(
        tmp_path,
        provider,
        authority_correction_manifest_path=manifest,
    )
    alpha = next(
        interval
        for interval in read_membership_intervals(result.membership_path)
        if interval.canonical_symbol == "AAA"
    )

    assert alpha.effective_from == date(2020, 1, 1)
    assert alpha.effective_to == date(2020, 1, 10)
    assert "alpha-reviewed-primary-correction-v1" in alpha.source_ref


def test_materialization_content_is_deterministic(tmp_path: Path) -> None:
    first = _materialize(tmp_path / "first", _MembershipProvider())
    second = _materialize(tmp_path / "second", _MembershipProvider())

    assert first.membership_path.read_bytes() == second.membership_path.read_bytes()
    assert first.aliases_path.read_bytes() == second.aliases_path.read_bytes()
    assert first.lineage == second.lineage


def test_evidence_hash_mismatch_is_rejected() -> None:
    valid = _evidence("/api/fundamentals/GSPC.INDX", {})
    with pytest.raises(EODHDMarketDataError, match="content SHA-256"):
        replace(valid, content_sha256="0" * 64)
