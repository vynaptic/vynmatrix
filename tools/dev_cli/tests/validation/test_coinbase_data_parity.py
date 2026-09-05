from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from dev_cli.validation.evidence import evidence_sha256
from dev_cli.validation.providers.coinbase_data_parity import (
    build_coinbase_daily_parity_attestation,
    resolve_data_parity_output,
    verify_coinbase_daily_parity_attestation,
)
from dev_cli.validation.providers.coinbase_protocol import (
    CoinbaseProductMetadata,
    CoinbaseValidationClient,
    fetch_half_open_candles,
    registered_data_parity_inputs,
)

_START = datetime(2025, 1, 15, tzinfo=UTC)
_END = _START + timedelta(days=2)
_REPO = Path(__file__).resolve().parents[4]
_REAL_PROVIDER_FIXTURE = (
    _REPO / "tests/fixtures/market_data/coinbase_btcusdc_1m_integration_2026-06-10.json"
)
_REAL_PROVIDER_FIXTURE_SHA256 = "23083a31a795c6aba679b21adeb3271a9f9598b33b3830c1b2bd938fa7d123ec"


def test_data_parity_output_is_content_addressed_and_confined(tmp_path: Path) -> None:
    digest = "b" * 64
    expected_name = f"RegisteredCampaign-data-parity-attestation-{digest}.json"
    path = resolve_data_parity_output(tmp_path, "RegisteredCampaign", digest, None)

    assert path.name == expected_name
    with pytest.raises(ValueError, match="data parity output escapes"):
        resolve_data_parity_output(
            tmp_path,
            "RegisteredCampaign",
            digest,
            tmp_path / "outside.json",
        )
    with pytest.raises(ValueError, match="registered filename"):
        resolve_data_parity_output(
            tmp_path,
            "RegisteredCampaign",
            digest,
            path.with_name("renamed.json"),
        )


def _metadata() -> dict[str, object]:
    return {
        "requested_product_id": "BTC-USDC",
        "product_id": "BTC-USDC",
        "canonical_book": "BTC-USD",
        "alias": "BTC-USD",
        "alias_to": [],
        "base_currency_id": "BTC",
        "quote_currency_id": "USDC",
        "quote_display_symbol": "USD",
        "base_increment": "0.00000001",
        "quote_increment": "0.01",
        "price_increment": "0.01",
        "product_type": "SPOT",
        "status": "online",
        "is_disabled": False,
        "trading_disabled": False,
    }


def test_product_metadata_preserves_requested_alias_and_canonical_book() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/brokerage/market/products/BTC-USDC"
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "product_id": "BTC-USDC",
                "alias": "BTC-USD",
                "alias_to": [],
                "base_currency_id": "BTC",
                "quote_currency_id": "USDC",
                "quote_display_symbol": "USD",
                "base_increment": "0.00000001",
                "quote_increment": "0.01",
                "price_increment": "0.01",
                "product_type": "SPOT",
                "status": "online",
                "is_disabled": False,
                "trading_disabled": False,
            },
        )

    client = CoinbaseValidationClient(
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.coinbase.com",
        )
    )
    metadata = client.fetch_product_metadata("btc-usdc")

    metadata.require_canonical_book("BTC-USD")
    assert metadata.requested_product_id == "BTC-USDC"
    assert metadata.canonical_book == "BTC-USD"
    assert metadata.to_manifest_dict()["quote_currency_id"] == "USDC"
    assert metadata.to_manifest_dict()["quote_display_symbol"] == "USD"


def test_real_provider_fixture_preserves_alias_and_half_open_candle_contract() -> None:
    raw_fixture = _REAL_PROVIDER_FIXTURE.read_bytes()
    assert hashlib.sha256(raw_fixture).hexdigest() == _REAL_PROVIDER_FIXTURE_SHA256
    fixture = json.loads(raw_fixture)
    assert fixture["fixture_scope"] == "real_provider_integration"
    assert fixture["market_evidence"] is True
    assert fixture["provider"] == "Coinbase Advanced Trade public API"
    assert fixture["retrieved_at_utc"] == "2026-07-22T22:00:49Z"

    metadata = CoinbaseProductMetadata.from_payload(
        requested_product_id=str(fixture["requested_product_id"]),
        payload=fixture["product_metadata"],
    )
    metadata.require_canonical_book("BTC-USD")

    class FixtureClient:
        def fetch_candles(self, **request: object) -> list[dict[str, str]]:
            assert request == {
                "product_id": "BTC-USDC",
                "start_time": datetime(2026, 6, 10, tzinfo=UTC),
                "end_time": datetime(2026, 6, 10, 0, 10, tzinfo=UTC),
                "granularity": "ONE_MINUTE",
            }
            return fixture["candles"]

    candles = fetch_half_open_candles(
        FixtureClient(),
        product_id="BTC-USDC",
        start=datetime.fromisoformat(fixture["requested_start_utc"]),
        end=datetime.fromisoformat(fixture["validation_window_end_exclusive_utc"]),
        granularity="ONE_MINUTE",
        period=timedelta(minutes=1),
        chunk_bars=20,
        request_interval_seconds=0.0,
    )
    assert len(candles) == 11
    assert [int(candle["start"]) for candle in candles] == list(range(1781049600, 1781050260, 60))


def test_data_parity_volume_dependency_is_protocol_driven() -> None:
    protocol = {
        "data": {
            "primary_products": [
                {
                    "requested_product": "GENERIC-USD",
                    "expected_canonical_book": "GENERIC-USD",
                    "price_increment": "0.01",
                }
            ],
            "minute_parity": {
                "required": True,
                "artifact_schema_id": "vynmatrix.coinbase-daily-parity-attestation",
                "artifact_schema_version": 1,
                "base_url": "https://api.coinbase.com",
                "public_candle_endpoint": (
                    "/api/v3/brokerage/market/products/{product_id}/candles"
                ),
                "minute_request_chunk_bars": 300,
                "provider_limit": 350,
                "incomplete_chunk_refetches_required": 2,
                "production_consolidation_minutes": 1_440,
                "minimum_constituent_coverage": 0.95,
                "stamp_transform": "provider_start_plus_one_day_equals_period_close",
                "volume_tolerance": {
                    "absolute_base_units": 0.00000001,
                    "relative_fraction": 0.000001,
                },
                "strategy_field_dependency": {
                    "price_and_timestamp_fields": "blocking",
                    "uses_volume": True,
                },
                "calendar_strata": [
                    {
                        "id": "generic_window",
                        "start": "2026-01-01T00:00:00Z",
                        "end_exclusive": "2026-01-02T00:00:00Z",
                    }
                ],
            },
        }
    }

    assert registered_data_parity_inputs(protocol)["strategy_uses_volume"] is True


def test_data_parity_fetch_uses_exact_bounded_half_open_chunks() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=301)

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[datetime, datetime]] = []

        def fetch_candles(
            self,
            *,
            product_id: str,
            start_time: datetime,
            end_time: datetime,
            granularity: str,
        ) -> list[dict[str, str]]:
            assert product_id == "BTC-USDC"
            assert granularity == "ONE_MINUTE"
            self.calls.append((start_time, end_time))
            count = int((end_time - start_time) / timedelta(minutes=1)) + 1
            return [
                {
                    "start": str(int((start_time + timedelta(minutes=index)).timestamp())),
                    "open": "1",
                    "high": "1",
                    "low": "1",
                    "close": "1",
                    "volume": "1",
                }
                for index in reversed(range(count))
            ]

    client = FakeClient()
    rows = fetch_half_open_candles(
        client,
        product_id="BTC-USDC",
        start=start,
        end=end,
        granularity="ONE_MINUTE",
        period=timedelta(minutes=1),
        chunk_bars=300,
        request_interval_seconds=0.0,
    )

    assert len(rows) == 301
    assert client.calls == [
        (start, start + timedelta(minutes=299)),
        (start + timedelta(minutes=300), start + timedelta(minutes=300)),
    ]


def test_data_parity_fetch_refetches_and_retains_a_stable_real_gap() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=3)

    class GappedClient:
        calls = 0

        def fetch_candles(self, **_kwargs: object) -> list[dict[str, str]]:
            self.calls += 1
            return [
                {
                    "start": str(int(start.timestamp())),
                    "open": "1",
                    "high": "1",
                    "low": "1",
                    "close": "1",
                    "volume": "1",
                },
                {
                    "start": str(int((start + timedelta(minutes=2)).timestamp())),
                    "open": "1",
                    "high": "1",
                    "low": "1",
                    "close": "1",
                    "volume": "1",
                },
            ]

    client = GappedClient()
    rows = fetch_half_open_candles(
        client,
        product_id="BTC-USDC",
        start=start,
        end=end,
        granularity="ONE_MINUTE",
        period=timedelta(minutes=1),
        chunk_bars=300,
        request_interval_seconds=0.0,
        require_complete=False,
    )

    assert client.calls == 2
    assert [row["start"] for row in rows] == [
        str(int(start.timestamp())),
        str(int((start + timedelta(minutes=2)).timestamp())),
    ]


def test_data_parity_fetch_accepts_only_stable_empty_incomplete_ranges() -> None:
    start = datetime(2025, 10, 25, 16, tzinfo=UTC)
    end = start + timedelta(hours=5)

    class EmptyClient:
        calls = 0

        def fetch_candles(self, **_kwargs: object) -> list[dict[str, str]]:
            self.calls += 1
            return []

    client = EmptyClient()
    assert (
        fetch_half_open_candles(
            client,
            product_id="BTC-USDC",
            start=start,
            end=end,
            granularity="ONE_MINUTE",
            period=timedelta(minutes=1),
            chunk_bars=300,
            request_interval_seconds=0.0,
            require_complete=False,
        )
        == []
    )
    assert client.calls == 2

    with pytest.raises(ValueError, match="response is incomplete"):
        fetch_half_open_candles(
            client,
            product_id="BTC-USDC",
            start=start,
            end=end,
            granularity="ONE_MINUTE",
            period=timedelta(minutes=1),
            chunk_bars=300,
            request_interval_seconds=0.0,
        )


def test_product_metadata_alias_drift_fails_closed() -> None:
    payload = {
        "product_id": "BTC-USDC",
        "alias": "BTC-USDT",
        "alias_to": [],
        "base_currency_id": "BTC",
        "quote_currency_id": "USDC",
        "base_increment": "0.00000001",
        "quote_increment": "0.01",
        "price_increment": "0.01",
        "is_disabled": False,
        "trading_disabled": False,
    }
    client = CoinbaseValidationClient(
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
            base_url="https://api.coinbase.com",
        )
    )
    metadata = client.fetch_product_metadata("BTC-USDC")

    with pytest.raises(ValueError, match="alias drift"):
        metadata.require_canonical_book("BTC-USD")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "product_id": "BTC-USDC",
            "base_currency_id": "BTC",
            "quote_currency_id": "USDC",
            "base_increment": "0",
            "quote_increment": "0.01",
            "price_increment": "0.01",
        },
    ],
)
def test_rejects_malformed_product_metadata(payload: object) -> None:
    client = CoinbaseValidationClient(
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
            base_url="https://api.coinbase.com",
        )
    )
    with pytest.raises(RuntimeError, match="Coinbase product metadata"):
        client.fetch_product_metadata("BTC-USDC")


def _source(*, second_day_daily_volume: str = "1440") -> dict[str, object]:
    minute: list[dict[str, str]] = []
    for offset in range(2 * 1440):
        day = offset // 1440
        price = str(100 + day)
        minute.append(
            {
                "start": str(int((_START + timedelta(minutes=offset)).timestamp())),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": "1",
            }
        )
    daily = [
        {
            "start": str(int(_START.timestamp())),
            "open": "100",
            "high": "100",
            "low": "100",
            "close": "100",
            "volume": "1440",
        },
        {
            "start": str(int((_START + timedelta(days=1)).timestamp())),
            "open": "101",
            "high": "101",
            "low": "101",
            "close": "101",
            "volume": second_day_daily_volume,
        },
    ]
    return {
        "BTC-USDC": {
            "2025_h1": {
                "ONE_MINUTE": minute,
                "ONE_DAY": daily,
            }
        }
    }


def _build(*, second_day_daily_volume: str = "1440", uses_volume: bool = False):
    return build_coinbase_daily_parity_attestation(
        _source(second_day_daily_volume=second_day_daily_volume),
        product_metadata={"BTC-USDC": _metadata()},
        expected_canonical_books={"BTC-USDC": "BTC-USD"},
        calendar_strata=[
            {
                "id": "2025_h1",
                "start": _START.isoformat(),
                "end_exclusive": _END.isoformat(),
            }
        ],
        price_ticks={"BTC-USDC": "0.01"},
        measured_at_utc="2026-07-22T08:00:00Z",
        strategy_uses_volume=uses_volume,
    )


def test_exact_public_candles_build_a_self_verifying_attestation() -> None:
    payload = _build()

    verify_coinbase_daily_parity_attestation(payload)

    assert payload["summary"] == {
        "minute_candles_expected": 2880,
        "minute_candles_observed": 2880,
        "minimum_daily_constituent_coverage_observed": 1.0,
        "sampled_daily_bars": 2,
        "exact_price_and_timestamp_matches": 2,
        "full_ohlcv_matches": 2,
        "volume_only_mismatches": 0,
        "blocking_mismatches": 0,
        "status": "exact_full_ohlcv_match",
        "campaign_disposition": "eligible_for_volume_independent_price_rules",
    }
    assert len(payload["daily_audit_ledger"]) == 2
    assert payload["products"]["BTC-USDC"]["canonical_book"] == "BTC-USD"
    assert payload["reconciliation"]["source_sha256"]
    assert payload["source_input_sha256"]


def test_exact_volume_dependent_parity_has_an_explicit_eligible_disposition() -> None:
    payload = _build(uses_volume=True)

    verify_coinbase_daily_parity_attestation(payload)

    assert payload["strategy_field_dependency"]["volume"] == "consumed"
    assert payload["summary"]["campaign_disposition"] == "eligible_for_volume_dependent_ohlcv_rules"


def test_volume_only_provider_difference_is_explicit_and_strategy_scoped() -> None:
    payload = _build(second_day_daily_volume="1439")

    verify_coinbase_daily_parity_attestation(payload)

    assert payload["summary"]["full_ohlcv_matches"] == 1
    assert payload["summary"]["volume_only_mismatches"] == 1
    assert payload["daily_audit_ledger"][1]["disposition"] == ("non_blocking_volume_only_mismatch")
    with pytest.raises(ValueError, match="parity is blocking"):
        _build(second_day_daily_volume="1439", uses_volume=True)


def test_price_mismatch_or_incomplete_minute_window_fails_closed() -> None:
    price_mismatch = _source()
    price_mismatch["BTC-USDC"]["2025_h1"]["ONE_DAY"][0]["close"] = "99"
    price_mismatch["BTC-USDC"]["2025_h1"]["ONE_DAY"][0]["low"] = "99"
    with pytest.raises(ValueError, match="parity is blocking"):
        build_coinbase_daily_parity_attestation(
            price_mismatch,
            product_metadata={"BTC-USDC": _metadata()},
            expected_canonical_books={"BTC-USDC": "BTC-USD"},
            calendar_strata=[
                {"id": "2025_h1", "start": _START.isoformat(), "end_exclusive": _END.isoformat()}
            ],
            price_ticks={"BTC-USDC": "0.01"},
            measured_at_utc="2026-07-22T08:00:00Z",
        )

    incomplete = _source()
    del incomplete["BTC-USDC"]["2025_h1"]["ONE_MINUTE"][:200]
    with pytest.raises(ValueError, match="parity is blocking"):
        build_coinbase_daily_parity_attestation(
            incomplete,
            product_metadata={"BTC-USDC": _metadata()},
            expected_canonical_books={"BTC-USDC": "BTC-USD"},
            calendar_strata=[
                {"id": "2025_h1", "start": _START.isoformat(), "end_exclusive": _END.isoformat()}
            ],
            price_ticks={"BTC-USDC": "0.01"},
            measured_at_utc="2026-07-22T08:00:00Z",
        )


def test_hash_rewrite_cannot_hide_ledger_or_source_tampering() -> None:
    ledger_tamper = deepcopy(_build())
    ledger_tamper["daily_audit_ledger"][0]["disposition"] = "exact_ohlcv_match_tampered"
    unsigned = dict(ledger_tamper)
    unsigned.pop("attestation_sha256")
    ledger_tamper["attestation_sha256"] = evidence_sha256(unsigned)
    with pytest.raises(ValueError, match="recomputed source evidence"):
        verify_coinbase_daily_parity_attestation(ledger_tamper)

    source_tamper = deepcopy(_build())
    source_tamper["source_inputs"]["BTC-USDC"]["2025_h1"]["ONE_MINUTE"][0]["volume"] = "2"
    source_tamper["source_input_sha256"] = evidence_sha256(source_tamper["source_inputs"])
    unsigned = dict(source_tamper)
    unsigned.pop("attestation_sha256")
    source_tamper["attestation_sha256"] = evidence_sha256(unsigned)
    with pytest.raises(ValueError, match="recomputed source evidence"):
        verify_coinbase_daily_parity_attestation(source_tamper)
