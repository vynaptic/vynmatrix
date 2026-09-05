"""Deterministic contracts for hash-attested Coinbase cost measurements."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from dev_cli.validation.evidence import evidence_sha256
from dev_cli.validation.providers import coinbase_protocol
from dev_cli.validation.providers.coinbase_execution_costs import (
    build_coinbase_execution_cost_measurement,
    create_registered_coinbase_execution_cost_measurement,
    resolve_cost_measurement_output,
    verify_coinbase_execution_cost_measurement,
)
from dev_cli.validation.providers.coinbase_protocol import CoinbaseValidationClient


def test_cost_output_is_content_addressed_and_confined(tmp_path: Path) -> None:
    digest = "a" * 64
    expected_name = f"RegisteredCampaign-execution-cost-measurement-{digest}.json"
    path = resolve_cost_measurement_output(tmp_path, "RegisteredCampaign", digest, None)

    assert path.name == expected_name
    with pytest.raises(ValueError, match="cost measurement output escapes"):
        resolve_cost_measurement_output(
            tmp_path,
            "RegisteredCampaign",
            digest,
            tmp_path / "outside.json",
        )
    with pytest.raises(ValueError, match="registered filename"):
        resolve_cost_measurement_output(
            tmp_path,
            "RegisteredCampaign",
            digest,
            path.with_name("renamed.json"),
        )


def test_registered_measurement_reads_canonical_venue_books(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_to_canonical = {"BTC-USDC": "BTC-USD", "ETH-USDC": "ETH-USD"}
    fetched_products: list[str] = []

    class FakeCoinbaseClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def fetch_spot_fee_rates(self) -> dict[str, str]:
            return {"maker_fee_rate": "0.006", "taker_fee_rate": "0.012"}

        def fetch_product_book(self, product: str, *, limit: int) -> dict[str, object]:
            fetched_products.append(product)
            return {"product_id": product, "limit": limit}

        def close(self) -> None:
            pass

    from dev_cli.validation.providers import coinbase_execution_costs

    monkeypatch.setattr(
        coinbase_execution_costs,
        "registered_cost_measurement_inputs",
        lambda *_args, **_kwargs: (
            requested_to_canonical,
            {"book_endpoint": "/book", "fee_endpoint": "/fees"},
            {
                "maximum_snapshot_age_seconds": 120,
                "maximum_inter_sample_gap_seconds": 10,
                "maximum_provider_clock_skew_seconds": 5,
            },
            {
                "expected_aggregation": {"quantile": 0.5},
                "stressed_aggregation": {"quantile": 0.95},
            },
        ),
    )
    monkeypatch.setattr(coinbase_execution_costs, "CoinbaseValidationClient", FakeCoinbaseClient)
    monkeypatch.setattr(coinbase_execution_costs.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        coinbase_execution_costs,
        "build_coinbase_execution_cost_measurement",
        lambda snapshots, **_kwargs: {
            "measurement_sha256": "a" * 64,
            "snapshot_keys": sorted(snapshots),
        },
    )
    monkeypatch.setattr(
        coinbase_execution_costs,
        "verify_coinbase_execution_cost_measurement",
        lambda _payload: None,
    )

    payload, destination = create_registered_coinbase_execution_cost_measurement(
        {},
        repo_root=tmp_path,
        strategy_name="RegisteredCampaign",
        api_key="test-key",
        api_secret="test-secret",
        samples=2,
        sample_interval_seconds=1.0,
        depth_levels=100,
        expected_notional_usd="10000",
        stressed_notional_usd="100000",
        output=None,
    )

    assert fetched_products == ["BTC-USD", "ETH-USD", "BTC-USD", "ETH-USD"]
    assert payload["snapshot_keys"] == ["BTC-USDC", "ETH-USDC"]
    assert destination.read_bytes()


def test_public_product_book_requests_exact_depth_without_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/brokerage/market/product_book"
        assert dict(request.url.params) == {"product_id": "BTC-USDC", "limit": "100"}
        assert request.headers["Cache-Control"] == "no-cache"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"pricebook": {"product_id": "BTC-USD"}})

    client = CoinbaseValidationClient(
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.coinbase.com",
        )
    )

    assert client.fetch_product_book("btc-usdc", limit=100) == {
        "pricebook": {"product_id": "BTC-USD"}
    }


def test_authenticated_fee_snapshot_discards_private_account_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coinbase_protocol,
        "build_coinbase_auth_headers",
        lambda **_kwargs: {"Authorization": "Bearer test"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/brokerage/transaction_summary"
        assert dict(request.url.params) == {"product_type": "SPOT"}
        assert request.headers["Authorization"] == "Bearer test"
        return httpx.Response(
            200,
            json={
                "fee_tier": {
                    "maker_fee_rate": "0.006",
                    "taker_fee_rate": "0.012",
                },
                "total_balance": "private-not-retained",
                "advanced_trade_only_volume": 12345,
            },
        )

    client = CoinbaseValidationClient(
        api_key="key",
        api_secret="secret",
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.coinbase.com",
        ),
    )

    assert client.fetch_spot_fee_rates() == {
        "maker_fee_rate": "0.006",
        "taker_fee_rate": "0.012",
    }


def _snapshot(
    timestamp: str,
    *,
    provider_product: str = "BTC-USD",
    best_bid: str = "99",
    best_ask: str = "101",
) -> dict[str, object]:
    return {
        "pricebook": {
            "product_id": provider_product,
            "time": timestamp,
            "bids": [
                {"price": best_bid, "size": "100"},
                {"price": "98", "size": "100"},
            ],
            "asks": [
                {"price": best_ask, "size": "100"},
                {"price": "102", "size": "100"},
            ],
        }
    }


def _measurement(
    *, maker_fee_rate: str = "0.006", taker_fee_rate: str = "0.012"
) -> dict[str, object]:
    return build_coinbase_execution_cost_measurement(
        {
            "BTC-USDC": (
                _snapshot("2026-07-21T10:00:00Z"),
                _snapshot(
                    "2026-07-21T10:00:02Z",
                    best_bid="99.5",
                    best_ask="100.5",
                ),
            )
        },
        expected_provider_products={"BTC-USDC": "BTC-USD"},
        maker_fee_rate=maker_fee_rate,
        taker_fee_rate=taker_fee_rate,
        depth_limit=100,
        expected_quote_notional="1000",
        stressed_quote_notional="15000",
        measured_at_utc="2026-07-21T10:00:03Z",
        maximum_snapshot_age_seconds=10,
        maximum_inter_sample_gap_seconds=5,
    )


def test_cost_measurement_is_complete_self_hashed_and_private_data_free() -> None:
    payload = _measurement()

    verify_coinbase_execution_cost_measurement(payload)
    assert payload["fee_snapshot"] == {
        "maker_fee_rate": "0.006",
        "maker_fee_bps": "60.000",
        "taker_fee_rate": "0.012",
        "taker_fee_bps": "120.000",
        "private_account_fields_retained": False,
    }
    sampling = payload["sampling"]
    assert isinstance(sampling, dict)
    assert sampling["samples_required_per_product"] == 2
    assert sampling["samples_observed_per_product"] == 2
    summaries = payload["product_summaries"]
    assert isinstance(summaries, dict)
    btc = summaries["BTC-USDC"]
    assert isinstance(btc, dict)
    assert btc["expected"]["impact_bps"] == "0"
    assert float(btc["stressed"]["impact_bps"]) > 0.0


def test_cost_measurement_accepts_zero_fees_and_rejects_negative_fees() -> None:
    payload = _measurement(maker_fee_rate="0", taker_fee_rate="0")
    assert payload["fee_snapshot"] == {
        "maker_fee_rate": "0",
        "maker_fee_bps": "0",
        "taker_fee_rate": "0",
        "taker_fee_bps": "0",
        "private_account_fields_retained": False,
    }
    verify_coinbase_execution_cost_measurement(payload)

    with pytest.raises(ValueError, match="maker_fee_rate must be finite and non-negative"):
        _measurement(maker_fee_rate="-0.001")
    with pytest.raises(ValueError, match="taker_fee_rate must be finite and non-negative"):
        _measurement(taker_fee_rate="-0.001")


def test_cost_measurement_hash_detects_summary_and_raw_book_mutation() -> None:
    payload = _measurement()
    mutated_summary = deepcopy(payload)
    mutated_summary["historical_application"] = "changed"
    with pytest.raises(ValueError, match="content hash differs"):
        verify_coinbase_execution_cost_measurement(mutated_summary)

    mutated_raw = deepcopy(payload)
    raw = mutated_raw["raw_snapshots"]
    assert isinstance(raw, dict)
    rows = raw["BTC-USDC"]
    assert isinstance(rows, list)
    rows[0]["bids"][0]["size"] = "999"
    unsigned = dict(mutated_raw)
    del unsigned["measurement_sha256"]
    # Preserve the outer content hash to isolate the independent raw digest.
    mutated_raw["measurement_sha256"] = evidence_sha256(unsigned)
    with pytest.raises(ValueError, match="raw snapshot hash differs"):
        verify_coinbase_execution_cost_measurement(mutated_raw)

    internally_rehashed_summary = deepcopy(payload)
    summaries = internally_rehashed_summary["product_summaries"]
    assert isinstance(summaries, dict)
    btc = summaries["BTC-USDC"]
    assert isinstance(btc, dict)
    expected = btc["expected"]
    assert isinstance(expected, dict)
    expected["impact_bps"] = "999"
    unsigned = dict(internally_rehashed_summary)
    del unsigned["measurement_sha256"]
    internally_rehashed_summary["measurement_sha256"] = evidence_sha256(unsigned)
    with pytest.raises(ValueError, match="differs from recomputed raw evidence"):
        verify_coinbase_execution_cost_measurement(internally_rehashed_summary)


def test_cost_measurement_fails_closed_on_missing_or_cached_samples() -> None:
    with pytest.raises(ValueError, match="same complete sample count"):
        build_coinbase_execution_cost_measurement(
            {
                "BTC-USDC": (_snapshot("2026-07-21T10:00:00Z"),),
                "ETH-USDC": (
                    _snapshot(
                        "2026-07-21T10:00:00Z",
                        provider_product="ETH-USD",
                    ),
                    _snapshot(
                        "2026-07-21T10:00:02Z",
                        provider_product="ETH-USD",
                    ),
                ),
            },
            expected_provider_products={
                "BTC-USDC": "BTC-USD",
                "ETH-USDC": "ETH-USD",
            },
            maker_fee_rate="0.006",
            taker_fee_rate="0.012",
            depth_limit=100,
            expected_quote_notional="1000",
            stressed_quote_notional="5000",
            measured_at_utc="2026-07-21T10:00:03Z",
            maximum_snapshot_age_seconds=10,
            maximum_inter_sample_gap_seconds=5,
        )

    duplicate = _snapshot("2026-07-21T10:00:00Z")
    with pytest.raises(ValueError, match="unique and ordered"):
        build_coinbase_execution_cost_measurement(
            {"BTC-USDC": (duplicate, duplicate)},
            expected_provider_products={"BTC-USDC": "BTC-USD"},
            maker_fee_rate="0.006",
            taker_fee_rate="0.012",
            depth_limit=100,
            expected_quote_notional="1000",
            stressed_quote_notional="5000",
            measured_at_utc="2026-07-21T10:00:03Z",
            maximum_snapshot_age_seconds=10,
            maximum_inter_sample_gap_seconds=5,
        )


def test_cost_measurement_rejects_alias_or_insufficient_depth_drift() -> None:
    with pytest.raises(ValueError, match="provider identity differs"):
        build_coinbase_execution_cost_measurement(
            {"BTC-USDC": (_snapshot("2026-07-21T10:00:00Z", provider_product="BTC-USDC"),)},
            expected_provider_products={"BTC-USDC": "BTC-USD"},
            maker_fee_rate="0.006",
            taker_fee_rate="0.012",
            depth_limit=100,
            expected_quote_notional="1000",
            stressed_quote_notional="5000",
            measured_at_utc="2026-07-21T10:00:03Z",
            maximum_snapshot_age_seconds=10,
            maximum_inter_sample_gap_seconds=5,
        )

    with pytest.raises(ValueError, match="depth cannot fill"):
        build_coinbase_execution_cost_measurement(
            {"BTC-USDC": (_snapshot("2026-07-21T10:00:00Z"),)},
            expected_provider_products={"BTC-USDC": "BTC-USD"},
            maker_fee_rate="0.006",
            taker_fee_rate="0.012",
            depth_limit=100,
            expected_quote_notional="1000",
            stressed_quote_notional="100000000",
            measured_at_utc="2026-07-21T10:00:03Z",
            maximum_snapshot_age_seconds=10,
            maximum_inter_sample_gap_seconds=5,
        )


def test_cost_measurement_fails_closed_on_stale_future_or_gapped_samples() -> None:
    common = {
        "expected_provider_products": {"BTC-USDC": "BTC-USD"},
        "maker_fee_rate": "0.006",
        "taker_fee_rate": "0.012",
        "depth_limit": 100,
        "expected_quote_notional": "1000",
        "stressed_quote_notional": "5000",
        "measured_at_utc": "2026-07-21T10:01:00Z",
        "maximum_snapshot_age_seconds": 10,
        "maximum_inter_sample_gap_seconds": 5,
    }
    with pytest.raises(ValueError, match="stale or implausibly future-dated"):
        build_coinbase_execution_cost_measurement(
            {"BTC-USDC": (_snapshot("2026-07-21T10:00:00Z"),)},
            **common,
        )
    with pytest.raises(ValueError, match="stale or implausibly future-dated"):
        build_coinbase_execution_cost_measurement(
            {"BTC-USDC": (_snapshot("2026-07-21T10:01:06Z"),)},
            **common,
        )

    gap_contract = dict(common)
    gap_contract["maximum_snapshot_age_seconds"] = 120
    with pytest.raises(ValueError, match="registered sample gap"):
        build_coinbase_execution_cost_measurement(
            {
                "BTC-USDC": (
                    _snapshot("2026-07-21T10:00:00Z"),
                    _snapshot("2026-07-21T10:00:06Z"),
                )
            },
            **gap_contract,
        )
