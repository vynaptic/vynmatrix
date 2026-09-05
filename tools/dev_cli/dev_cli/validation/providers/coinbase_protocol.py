"""Frozen Coinbase provider contracts and bounded candle acquisition."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from lib_infrastructure.brokers.adapters.coinbase_auth import build_coinbase_auth_headers
from lib_infrastructure.market_data.coinbase_client import (
    CoinbaseCandleClient,
    CoinbaseMarketDataError,
)

MAX_PUBLIC_CANDLE_CHUNK_BARS = 300
_PROVIDER_LIMIT = 350
_DAILY_MINUTES = 1_440
_MAX_PRODUCT_BOOK_LEVELS = 1_000


@dataclass(frozen=True)
class CoinbaseProductMetadata:
    """Validated Coinbase identity retained in a research manifest."""

    requested_product_id: str
    product_id: str
    alias: str | None
    alias_to: tuple[str, ...]
    base_currency_id: str
    quote_currency_id: str
    quote_display_symbol: str | None
    base_increment: str
    quote_increment: str
    price_increment: str
    product_type: str | None
    status: str | None
    is_disabled: bool
    trading_disabled: bool

    @property
    def canonical_book(self) -> str:
        return self.alias or self.product_id

    @classmethod
    def from_payload(
        cls,
        *,
        requested_product_id: str,
        payload: dict[str, Any],
    ) -> CoinbaseProductMetadata:
        required = (
            "product_id",
            "base_currency_id",
            "quote_currency_id",
            "base_increment",
            "quote_increment",
            "price_increment",
        )
        missing = [field for field in required if not str(payload.get(field) or "").strip()]
        if missing:
            message = f"Coinbase product metadata missing required fields: {', '.join(missing)}"
            raise ValueError(message)
        for field in ("base_increment", "quote_increment", "price_increment"):
            try:
                increment = Decimal(str(payload[field]))
            except (InvalidOperation, ValueError) as exc:
                message = f"Coinbase product metadata has invalid {field}"
                raise ValueError(message) from exc
            if not increment.is_finite() or increment <= 0:
                message = f"Coinbase product metadata has non-positive {field}"
                raise ValueError(message)
        for field in ("is_disabled", "trading_disabled"):
            if not isinstance(payload.get(field), bool):
                message = f"Coinbase product metadata has invalid or missing {field}"
                raise TypeError(message)
        raw_alias_to = payload.get("alias_to") or []
        if not isinstance(raw_alias_to, list) or not all(
            isinstance(value, str) and value.strip() for value in raw_alias_to
        ):
            message = "Coinbase product metadata alias_to must be a list of product IDs"
            raise ValueError(message)
        alias_value = payload.get("alias")
        if alias_value is not None and not isinstance(alias_value, str):
            message = "Coinbase product metadata alias must be a product ID or null"
            raise ValueError(message)
        alias = str(alias_value).strip() if alias_value else None
        return cls(
            requested_product_id=requested_product_id.strip().upper(),
            product_id=str(payload["product_id"]).strip().upper(),
            alias=alias.upper() if alias else None,
            alias_to=tuple(str(value).strip().upper() for value in raw_alias_to),
            base_currency_id=str(payload["base_currency_id"]).strip().upper(),
            quote_currency_id=str(payload["quote_currency_id"]).strip().upper(),
            quote_display_symbol=(
                str(payload["quote_display_symbol"]).strip()
                if payload.get("quote_display_symbol")
                else None
            ),
            base_increment=str(payload["base_increment"]),
            quote_increment=str(payload["quote_increment"]),
            price_increment=str(payload["price_increment"]),
            product_type=(str(payload["product_type"]) if payload.get("product_type") else None),
            status=str(payload["status"]) if payload.get("status") else None,
            is_disabled=payload["is_disabled"],
            trading_disabled=payload["trading_disabled"],
        )

    def require_canonical_book(self, expected: str) -> None:
        expected_book = expected.strip().upper()
        if self.canonical_book != expected_book:
            message = (
                "Coinbase product alias drift: "
                f"requested={self.requested_product_id} "
                f"expected={expected_book} observed={self.canonical_book}"
            )
            raise ValueError(message)

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "requested_product_id": self.requested_product_id,
            "product_id": self.product_id,
            "canonical_book": self.canonical_book,
            "alias": self.alias,
            "alias_to": list(self.alias_to),
            "base_currency_id": self.base_currency_id,
            "quote_currency_id": self.quote_currency_id,
            "quote_display_symbol": self.quote_display_symbol,
            "base_increment": self.base_increment,
            "quote_increment": self.quote_increment,
            "price_increment": self.price_increment,
            "product_type": self.product_type,
            "status": self.status,
            "is_disabled": self.is_disabled,
            "trading_disabled": self.trading_disabled,
        }


class CoinbaseValidationClient(CoinbaseCandleClient):
    """Coinbase research endpoints kept outside the production candle adapter."""

    def fetch_product_metadata(self, product_id: str) -> CoinbaseProductMetadata:
        requested = product_id.strip().upper()
        if not requested:
            message = "Coinbase product_id must not be empty"
            raise ValueError(message)
        path = f"/api/v3/brokerage/market/products/{requested}"
        response = self._client.get(
            path,
            headers={"Accept": "application/json", "Cache-Control": "no-cache"},
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            message = "Coinbase product metadata response was not valid JSON"
            raise CoinbaseMarketDataError(message) from exc
        if not isinstance(payload, dict):
            message = "Coinbase product metadata response must be an object"
            raise CoinbaseMarketDataError(message)
        try:
            return CoinbaseProductMetadata.from_payload(
                requested_product_id=requested,
                payload=payload,
            )
        except (TypeError, ValueError) as exc:
            raise CoinbaseMarketDataError(str(exc)) from exc

    def fetch_product_book(self, product_id: str, *, limit: int = 100) -> dict[str, Any]:
        requested = product_id.strip().upper()
        if not requested:
            message = "Coinbase product_id must not be empty"
            raise ValueError(message)
        if isinstance(limit, bool) or not isinstance(limit, int):
            message = "Coinbase product-book limit must be an integer"
            raise TypeError(message)
        if not 1 <= limit <= _MAX_PRODUCT_BOOK_LEVELS:
            message = "Coinbase product-book limit must be between 1 and 1000"
            raise ValueError(message)
        path = (
            "/api/v3/brokerage/product_book"
            if self._authenticated
            else "/api/v3/brokerage/market/product_book"
        )
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        }
        if self._authenticated:
            assert self._api_key is not None
            assert self._api_secret is not None
            headers.update(
                build_coinbase_auth_headers(
                    api_key=self._api_key,
                    api_secret=self._api_secret,
                    method="GET",
                    host=self._request_host,
                    path=path,
                )
            )
        response = self._client.get(
            path,
            params={"product_id": requested, "limit": str(limit)},
            headers=headers,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            message = "Coinbase product-book response was not valid JSON"
            raise CoinbaseMarketDataError(message) from exc
        if not isinstance(payload, dict):
            message = "Coinbase product-book response must be an object"
            raise CoinbaseMarketDataError(message)
        return dict(payload)

    def fetch_spot_fee_rates(self) -> dict[str, str]:
        if not self._authenticated:
            message = "Coinbase fee measurement requires authenticated credentials"
            raise RuntimeError(message)
        assert self._api_key is not None
        assert self._api_secret is not None
        path = "/api/v3/brokerage/transaction_summary"
        headers = build_coinbase_auth_headers(
            api_key=self._api_key,
            api_secret=self._api_secret,
            method="GET",
            host=self._request_host,
            path=path,
        )
        response = self._client.get(path, params={"product_type": "SPOT"}, headers=headers)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            message = "Coinbase transaction-summary response was not valid JSON"
            raise CoinbaseMarketDataError(message) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("fee_tier"), dict):
            message = "Coinbase transaction-summary response omitted fee_tier"
            raise CoinbaseMarketDataError(message)
        maker = payload["fee_tier"].get("maker_fee_rate")
        taker = payload["fee_tier"].get("taker_fee_rate")
        if not isinstance(maker, str) or not isinstance(taker, str):
            message = "Coinbase fee_tier omitted string maker/taker rates"
            raise CoinbaseMarketDataError(message)
        return {"maker_fee_rate": maker, "taker_fee_rate": taker}


def registered_data_parity_inputs(protocol: Mapping[str, object]) -> dict[str, Any]:
    """Read and validate the protocol's exact Coinbase parity contract."""

    data = protocol.get("data")
    if not isinstance(data, Mapping):
        message = "validation protocol data must be an object"
        raise TypeError(message)
    parity = data.get("minute_parity")
    if not isinstance(parity, Mapping):
        message = "validation protocol data.minute_parity must be an object"
        raise TypeError(message)
    expected_contract = {
        "artifact_schema_id": "vynmatrix.coinbase-daily-parity-attestation",
        "artifact_schema_version": 1,
        "base_url": "https://api.coinbase.com",
        "public_candle_endpoint": "/api/v3/brokerage/market/products/{product_id}/candles",
        "minute_request_chunk_bars": MAX_PUBLIC_CANDLE_CHUNK_BARS,
        "provider_limit": _PROVIDER_LIMIT,
        "incomplete_chunk_refetches_required": 2,
    }
    if any(parity.get(key) != value for key, value in expected_contract.items()):
        message = "minute-parity provider endpoint or request contract differs"
        raise ValueError(message)
    if (
        parity.get("required") is not True
        or parity.get("production_consolidation_minutes") != _DAILY_MINUTES
        or parity.get("stamp_transform") != "provider_start_plus_one_day_equals_period_close"
    ):
        message = "minute-parity production consolidation contract differs"
        raise ValueError(message)
    dependency = parity.get("strategy_field_dependency")
    if not isinstance(dependency, Mapping) or set(dependency) != {
        "price_and_timestamp_fields",
        "uses_volume",
    }:
        message = "minute-parity strategy field-dependency contract differs"
        raise ValueError(message)
    strategy_uses_volume = dependency.get("uses_volume")
    if dependency.get("price_and_timestamp_fields") != "blocking" or not isinstance(
        strategy_uses_volume, bool
    ):
        message = "minute-parity strategy field-dependency contract differs"
        raise ValueError(message)
    volume = parity.get("volume_tolerance")
    if not isinstance(volume, Mapping):
        message = "minute-parity volume_tolerance must be an object"
        raise TypeError(message)
    raw_products = data.get("primary_products")
    if isinstance(raw_products, (str, bytes)) or not isinstance(raw_products, Sequence):
        message = "validation protocol primary_products must be an array"
        raise TypeError(message)
    expected_books: dict[str, str] = {}
    ticks: dict[str, str] = {}
    for index, raw_product in enumerate(raw_products):
        if not isinstance(raw_product, Mapping):
            message = f"primary_products[{index}] must be an object"
            raise TypeError(message)
        requested = raw_product.get("requested_product")
        canonical = raw_product.get("expected_canonical_book")
        tick = raw_product.get("price_increment")
        if not all(isinstance(value, str) and value for value in (requested, canonical, tick)):
            message = f"primary_products[{index}] omits parity product identity/tick"
            raise ValueError(message)
        expected_books[str(requested)] = str(canonical)
        ticks[str(requested)] = str(tick)
    strata = parity.get("calendar_strata")
    if isinstance(strata, (str, bytes)) or not isinstance(strata, Sequence) or not strata:
        message = "minute-parity calendar_strata must be a non-empty array"
        raise TypeError(message)
    return {
        "expected_canonical_books": expected_books,
        "price_ticks": ticks,
        "calendar_strata": list(strata),
        "minimum_constituent_coverage": float(parity["minimum_constituent_coverage"]),
        "volume_absolute_tolerance": float(volume["absolute_base_units"]),
        "volume_relative_tolerance": float(volume["relative_fraction"]),
        "strategy_uses_volume": strategy_uses_volume,
    }


def fetch_half_open_candles(
    client: Any,
    *,
    product_id: str,
    start: datetime,
    end: datetime,
    granularity: str,
    period: timedelta,
    chunk_bars: int,
    request_interval_seconds: float,
    require_complete: bool = True,
) -> list[dict[str, Any]]:
    """Fetch an exact half-open interval through bounded public API windows."""

    if start.tzinfo is None or end.tzinfo is None or start >= end:
        message = "Coinbase parity fetch requires an aware positive UTC range"
        raise ValueError(message)
    if period <= timedelta(0) or (end - start) % period != timedelta(0):
        message = "Coinbase parity range must align exactly to its granularity"
        raise ValueError(message)
    if chunk_bars < 1 or chunk_bars > MAX_PUBLIC_CANDLE_CHUNK_BARS:
        message = "Coinbase parity chunks must contain between 1 and 300 bars"
        raise ValueError(message)
    rows_by_start: dict[int, dict[str, Any]] = {}
    cursor = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    while cursor < end_utc:
        chunk_end = min(end_utc, cursor + period * chunk_bars)
        chunk_rows = _indexed_candle_chunk(
            client.fetch_candles(
                product_id=product_id,
                start_time=cursor,
                end_time=chunk_end - period,
                granularity=granularity,
            ),
            product_id=product_id,
            start=cursor,
            end=chunk_end,
        )
        expected_chunk = {
            int((cursor + period * index).timestamp())
            for index in range(int((chunk_end - cursor) / period))
        }
        if not require_complete and set(chunk_rows) != expected_chunk:
            if request_interval_seconds:
                time.sleep(request_interval_seconds)
            refetched_rows = _indexed_candle_chunk(
                client.fetch_candles(
                    product_id=product_id,
                    start_time=cursor,
                    end_time=chunk_end - period,
                    granularity=granularity,
                ),
                product_id=product_id,
                start=cursor,
                end=chunk_end,
            )
            if chunk_rows != refetched_rows:
                message = (
                    f"Coinbase {product_id} {granularity} incomplete chunk was not "
                    "stable across the required independent refetch"
                )
                raise ValueError(message)
        duplicates = set(rows_by_start) & set(chunk_rows)
        if duplicates:
            message = f"Coinbase {product_id} returned duplicate candle timestamps"
            raise ValueError(message)
        rows_by_start.update(chunk_rows)
        cursor = chunk_end
        if cursor < end_utc and request_interval_seconds:
            time.sleep(request_interval_seconds)
    expected = tuple(
        int((start.astimezone(UTC) + period * index).timestamp())
        for index in range(int((end_utc - start.astimezone(UTC)) / period))
    )
    observed = tuple(sorted(rows_by_start))
    if not set(observed).issubset(expected):
        message = f"Coinbase {product_id} {granularity} returned off-grid candles"
        raise ValueError(message)
    if require_complete and observed != expected:
        message = (
            f"Coinbase {product_id} {granularity} response is incomplete: "
            f"expected={len(expected)} observed={len(rows_by_start)}"
        )
        raise ValueError(message)
    return [rows_by_start[timestamp] for timestamp in observed]


def _indexed_candle_chunk(
    raw_rows: object,
    *,
    product_id: str,
    start: datetime,
    end: datetime,
) -> dict[int, dict[str, Any]]:
    if isinstance(raw_rows, (str, bytes)) or not isinstance(raw_rows, Sequence):
        message = f"Coinbase {product_id} candle response must be an array"
        raise TypeError(message)
    indexed: dict[int, dict[str, Any]] = {}
    for row in raw_rows:
        if not isinstance(row, Mapping):
            message = f"Coinbase {product_id} returned a non-object candle"
            raise TypeError(message)
        try:
            timestamp = int(str(row.get("start")))
        except (TypeError, ValueError) as exc:
            message = f"Coinbase {product_id} returned a malformed candle timestamp"
            raise ValueError(message) from exc
        observed = datetime.fromtimestamp(timestamp, tz=UTC)
        if not start <= observed < end:
            message = f"Coinbase {product_id} returned a candle outside its request chunk"
            raise ValueError(message)
        if timestamp in indexed:
            message = f"Coinbase {product_id} returned a duplicate candle timestamp"
            raise ValueError(message)
        indexed[timestamp] = dict(row)
    return indexed


def registered_cost_measurement_inputs(
    protocol: Mapping[str, object],
    *,
    samples: int,
    depth_levels: int,
    expected_notional: str,
    stressed_notional: str,
) -> tuple[dict[str, str], Mapping[str, object], Mapping[str, object], Mapping[str, object]]:
    """Read and validate the exact registered cost-measurement inputs."""

    data = protocol.get("data")
    if not isinstance(data, Mapping):
        message = "validation protocol data must be an object"
        raise TypeError(message)
    products = data.get("primary_products")
    if isinstance(products, (str, bytes)) or not isinstance(products, list) or not products:
        message = "validation protocol primary_products must be a non-empty array"
        raise TypeError(message)
    expected_products: dict[str, str] = {}
    for index, raw_product in enumerate(products):
        if not isinstance(raw_product, Mapping):
            message = f"primary_products[{index}] must be an object"
            raise TypeError(message)
        requested = raw_product.get("requested_product")
        canonical = raw_product.get("expected_canonical_book")
        if not isinstance(requested, str) or not isinstance(canonical, str):
            message = f"primary_products[{index}] omits requested/canonical product"
            raise TypeError(message)
        expected_products[requested] = canonical

    cost_contract = protocol.get("cost_measurement")
    if not isinstance(cost_contract, Mapping):
        message = "validation protocol cost_measurement must be an object"
        raise TypeError(message)
    sampling_contract = cost_contract.get("sampling")
    depth_contract = cost_contract.get("depth_walk")
    if not isinstance(sampling_contract, Mapping) or not isinstance(depth_contract, Mapping):
        message = "validation protocol cost measurement sampling/depth must be objects"
        raise TypeError(message)
    registered_inputs = {
        "samples": int(sampling_contract["samples_required_per_product"]),
        "depth_levels": int(sampling_contract["depth_limit_levels_per_side"]),
        "expected_notional": str(depth_contract["expected_quote_notional"]),
        "stressed_notional": str(depth_contract["stressed_quote_notional"]),
    }
    observed_inputs = {
        "samples": samples,
        "depth_levels": depth_levels,
        "expected_notional": expected_notional,
        "stressed_notional": stressed_notional,
    }
    if observed_inputs != registered_inputs:
        message = (
            "cost measurement inputs must exactly match validation_protocol.json: "
            f"registered={registered_inputs}, observed={observed_inputs}"
        )
        raise ValueError(message)
    return expected_products, cost_contract, sampling_contract, depth_contract
