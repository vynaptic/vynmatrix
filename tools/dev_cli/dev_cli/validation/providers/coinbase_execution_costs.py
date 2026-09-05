"""Hash-attested Coinbase spread and depth-walk cost measurements."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

from dev_cli.validation.evidence import (
    atomic_create_bytes,
    canonical_json_bytes,
    evidence_sha256,
    load_json_object,
    nonblank_string,
    parse_utc_datetime,
    resolve_strategy_validation_artifact,
    utc_iso,
)
from dev_cli.validation.providers.coinbase_protocol import (
    CoinbaseValidationClient,
    registered_cost_measurement_inputs,
)

_BPS = Decimal("10000")
_MEASUREMENT_SCHEMA = "vynmatrix.coinbase-execution-cost-measurement"
_MEASUREMENT_VERSION = 1


def _decimal(value: object, *, field: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        message = f"{field} must be a non-blank decimal string"
        raise ValueError(message)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        message = f"{field} is not a decimal"
        raise ValueError(message) from exc
    if not parsed.is_finite():
        message = f"{field} must be finite"
        raise ValueError(message)
    return parsed


def _positive_decimal(value: object, *, field: str) -> Decimal:
    parsed = _decimal(value, field=field)
    if parsed <= 0:
        message = f"{field} must be finite and positive"
        raise ValueError(message)
    return parsed


def _nonnegative_decimal(value: object, *, field: str) -> Decimal:
    parsed = _decimal(value, field=field)
    if parsed < 0:
        message = f"{field} must be finite and non-negative"
        raise ValueError(message)
    return parsed


def _positive_float(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        message = f"{field} must be a number"
        raise TypeError(message)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        message = f"{field} must be finite and positive"
        raise ValueError(message)
    return parsed


def _nonnegative_float(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        message = f"{field} must be a number"
        raise TypeError(message)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        message = f"{field} must be finite and non-negative"
        raise ValueError(message)
    return parsed


@dataclass(frozen=True, slots=True)
class _BookLevel:
    price: Decimal
    size: Decimal

    def to_dict(self) -> dict[str, str]:
        return {"price": str(self.price), "size": str(self.size)}


@dataclass(frozen=True, slots=True)
class _BookSnapshot:
    requested_product: str
    provider_product: str
    observed_at: str
    bids: tuple[_BookLevel, ...]
    asks: tuple[_BookLevel, ...]

    def raw_dict(self) -> dict[str, object]:
        return {
            "requested_product": self.requested_product,
            "provider_product": self.provider_product,
            "observed_at": self.observed_at,
            "bids": [item.to_dict() for item in self.bids],
            "asks": [item.to_dict() for item in self.asks],
        }


def _levels(value: object, *, field: str, descending: bool) -> tuple[_BookLevel, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        message = f"{field} must be a non-empty array"
        raise ValueError(message)
    levels: list[_BookLevel] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            message = f"{field}[{index}] must be an object"
            raise TypeError(message)
        levels.append(
            _BookLevel(
                price=_positive_decimal(raw.get("price"), field=f"{field}[{index}].price"),
                size=_positive_decimal(raw.get("size"), field=f"{field}[{index}].size"),
            )
        )
    prices = tuple(item.price for item in levels)
    ordered = tuple(sorted(prices, reverse=descending))
    if prices != ordered:
        direction = "descending" if descending else "ascending"
        message = f"{field} prices must be sorted {direction}"
        raise ValueError(message)
    if len(set(prices)) != len(prices):
        message = f"{field} contains duplicate price levels"
        raise ValueError(message)
    return tuple(levels)


def _normalize_snapshot(
    payload: Mapping[str, Any],
    *,
    requested_product: str,
    expected_provider_product: str,
    depth_limit: int,
) -> _BookSnapshot:
    raw_book = payload.get("pricebook")
    if not isinstance(raw_book, Mapping):
        message = f"{requested_product} product-book payload omitted pricebook"
        raise TypeError(message)
    provider_product = raw_book.get("product_id")
    if provider_product != expected_provider_product:
        message = (
            f"{requested_product} product-book provider identity differs: "
            f"{provider_product!r} != {expected_provider_product!r}"
        )
        raise ValueError(message)
    bids = _levels(raw_book.get("bids"), field=f"{requested_product}.bids", descending=True)
    asks = _levels(raw_book.get("asks"), field=f"{requested_product}.asks", descending=False)
    if len(bids) > depth_limit or len(asks) > depth_limit:
        message = f"{requested_product} product book exceeds requested depth limit"
        raise ValueError(message)
    if bids[0].price >= asks[0].price:
        message = f"{requested_product} product book is crossed or locked"
        raise ValueError(message)
    return _BookSnapshot(
        requested_product=requested_product,
        provider_product=expected_provider_product,
        observed_at=utc_iso(raw_book.get("time"), field=f"{requested_product}.time"),
        bids=bids,
        asks=asks,
    )


def _walk_buy(levels: Sequence[_BookLevel], quote_notional: Decimal) -> Decimal:
    remaining = quote_notional
    base_filled = Decimal(0)
    for level in levels:
        available_quote = level.price * level.size
        consumed_quote = min(remaining, available_quote)
        base_filled += consumed_quote / level.price
        remaining -= consumed_quote
        if remaining <= 0:
            break
    if remaining > 0 or base_filled <= 0:
        message = "ask depth cannot fill the registered quote notional"
        raise ValueError(message)
    return quote_notional / base_filled


def _walk_sell(levels: Sequence[_BookLevel], quote_notional: Decimal, mid: Decimal) -> Decimal:
    base_target = quote_notional / mid
    remaining = base_target
    quote_filled = Decimal(0)
    for level in levels:
        consumed_base = min(remaining, level.size)
        quote_filled += consumed_base * level.price
        remaining -= consumed_base
        if remaining <= 0:
            break
    if remaining > 0 or base_target <= 0:
        message = "bid depth cannot fill the registered quote notional"
        raise ValueError(message)
    return quote_filled / base_target


def _snapshot_costs(snapshot: _BookSnapshot, *, quote_notional: Decimal) -> dict[str, str]:
    best_bid = snapshot.bids[0].price
    best_ask = snapshot.asks[0].price
    mid = (best_bid + best_ask) / Decimal(2)
    half_spread = (best_ask - best_bid) / (Decimal(2) * mid) * _BPS
    buy_vwap = _walk_buy(snapshot.asks, quote_notional)
    sell_vwap = _walk_sell(snapshot.bids, quote_notional, mid)
    buy_adverse = (buy_vwap / mid - Decimal(1)) * _BPS
    sell_adverse = (Decimal(1) - sell_vwap / mid) * _BPS
    buy_impact = max(Decimal(0), buy_adverse - half_spread)
    sell_impact = max(Decimal(0), sell_adverse - half_spread)
    return {
        "quote_notional": str(quote_notional),
        "mid_price": str(mid),
        "half_spread_bps": str(half_spread),
        "buy_vwap": str(buy_vwap),
        "sell_vwap": str(sell_vwap),
        "buy_depth_impact_bps": str(buy_impact),
        "sell_depth_impact_bps": str(sell_impact),
        "worst_side_depth_impact_bps": str(max(buy_impact, sell_impact)),
    }


def _nearest_rank(values: Sequence[Decimal], quantile: float) -> Decimal:
    if not values:
        message = "quantile input must not be empty"
        raise ValueError(message)
    if not math.isfinite(quantile) or not 0 < quantile <= 1:
        message = "quantile must be finite and in (0, 1]"
        raise ValueError(message)
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def _validate_snapshot_timing(
    rows: Sequence[_BookSnapshot],
    *,
    product: str,
    measured_at: datetime,
    maximum_age: float,
    maximum_gap: float,
    maximum_clock_skew: float,
) -> None:
    observed = tuple(row.observed_at for row in rows)
    if len(set(observed)) != len(observed) or observed != tuple(sorted(observed)):
        message = f"{product} snapshot timestamps must be unique and ordered"
        raise ValueError(message)
    observed_datetimes = tuple(
        parse_utc_datetime(value, field=f"{product}.observed_at") for value in observed
    )
    for observed_at in observed_datetimes:
        age_seconds = (measured_at - observed_at).total_seconds()
        if age_seconds < -maximum_clock_skew or age_seconds > maximum_age:
            message = f"{product} snapshot timestamp is stale or implausibly future-dated"
            raise ValueError(message)
    if any(
        (later - earlier).total_seconds() > maximum_gap
        for earlier, later in pairwise(observed_datetimes)
    ):
        message = f"{product} snapshot timestamps exceed the registered sample gap"
        raise ValueError(message)


def build_coinbase_execution_cost_measurement(
    snapshot_payloads: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    expected_provider_products: Mapping[str, str],
    maker_fee_rate: str,
    taker_fee_rate: str,
    depth_limit: int,
    expected_quote_notional: str,
    stressed_quote_notional: str,
    measured_at_utc: str,
    maximum_snapshot_age_seconds: float,
    maximum_inter_sample_gap_seconds: float,
    maximum_provider_clock_skew_seconds: float = 5.0,
    book_endpoint: str = "/api/v3/brokerage/product_book",
    fee_endpoint: str = "/api/v3/brokerage/transaction_summary?product_type=SPOT",
    expected_quantile: float = 0.5,
    stressed_quantile: float = 0.95,
) -> dict[str, object]:
    """Validate raw snapshots and return a self-hashed immutable measurement.

    Impact is the worst of buy/sell VWAP movement beyond half-spread. Expected
    costs use the registered median at the expected notional; stressed costs
    use a nearest-rank 95th percentile at the stressed notional. Any missing,
    stale, malformed, crossed, or insufficient-depth sample fails the complete
    measurement instead of being silently omitted.
    """

    if isinstance(depth_limit, bool) or not isinstance(depth_limit, int):
        message = "depth_limit must be an integer"
        raise TypeError(message)
    if depth_limit < 1:
        message = "depth_limit must be positive"
        raise ValueError(message)
    products = tuple(sorted(snapshot_payloads))
    if not products or set(products) != set(expected_provider_products):
        message = "snapshot and expected-provider products must match exactly"
        raise ValueError(message)
    expected_notional = _positive_decimal(
        expected_quote_notional,
        field="expected_quote_notional",
    )
    stressed_notional = _positive_decimal(
        stressed_quote_notional,
        field="stressed_quote_notional",
    )
    if stressed_notional < expected_notional:
        message = "stressed quote notional must not be below expected notional"
        raise ValueError(message)
    measured_at = parse_utc_datetime(measured_at_utc, field="measured_at_utc")
    maximum_age = _positive_float(
        maximum_snapshot_age_seconds,
        field="maximum_snapshot_age_seconds",
    )
    maximum_gap = _positive_float(
        maximum_inter_sample_gap_seconds,
        field="maximum_inter_sample_gap_seconds",
    )
    maximum_clock_skew = _nonnegative_float(
        maximum_provider_clock_skew_seconds,
        field="maximum_provider_clock_skew_seconds",
    )
    normalized_book_endpoint = nonblank_string(book_endpoint, field="book_endpoint")
    normalized_fee_endpoint = nonblank_string(fee_endpoint, field="fee_endpoint")
    maker_rate = _nonnegative_decimal(maker_fee_rate, field="maker_fee_rate")
    taker_rate = _nonnegative_decimal(taker_fee_rate, field="taker_fee_rate")
    normalized_by_product: dict[str, tuple[_BookSnapshot, ...]] = {}
    sample_count: int | None = None
    for product in products:
        raw_rows = snapshot_payloads[product]
        if isinstance(raw_rows, (str, bytes)) or not isinstance(raw_rows, Sequence):
            message = f"{product} snapshots must be an array"
            raise TypeError(message)
        rows = tuple(
            _normalize_snapshot(
                payload,
                requested_product=product,
                expected_provider_product=expected_provider_products[product],
                depth_limit=depth_limit,
            )
            for payload in raw_rows
        )
        if not rows:
            message = f"{product} snapshots must not be empty"
            raise ValueError(message)
        _validate_snapshot_timing(
            rows,
            product=product,
            measured_at=measured_at,
            maximum_age=maximum_age,
            maximum_gap=maximum_gap,
            maximum_clock_skew=maximum_clock_skew,
        )
        if sample_count is None:
            sample_count = len(rows)
        elif len(rows) != sample_count:
            message = "every product must have the same complete sample count"
            raise ValueError(message)
        normalized_by_product[product] = rows
    assert sample_count is not None

    raw_snapshot_payload = {
        product: [row.raw_dict() for row in normalized_by_product[product]] for product in products
    }
    product_summaries: dict[str, object] = {}
    for product in products:
        rows = normalized_by_product[product]
        expected_rows = tuple(
            _snapshot_costs(row, quote_notional=expected_notional) for row in rows
        )
        stressed_rows = tuple(
            _snapshot_costs(row, quote_notional=stressed_notional) for row in rows
        )
        half_spreads = tuple(Decimal(row["half_spread_bps"]) for row in expected_rows)
        expected_impacts = tuple(
            Decimal(row["worst_side_depth_impact_bps"]) for row in expected_rows
        )
        stressed_impacts = tuple(
            Decimal(row["worst_side_depth_impact_bps"]) for row in stressed_rows
        )
        product_summaries[product] = {
            "provider_product": expected_provider_products[product],
            "samples_observed": sample_count,
            "sample_metrics": {
                "expected_notional": list(expected_rows),
                "stressed_notional": list(stressed_rows),
            },
            "optimistic": {
                "half_spread_bps": str(_nearest_rank(half_spreads, expected_quantile)),
                "impact_bps": "0",
            },
            "expected": {
                "half_spread_bps": str(_nearest_rank(half_spreads, expected_quantile)),
                "impact_bps": str(_nearest_rank(expected_impacts, expected_quantile)),
            },
            "stressed": {
                "half_spread_bps": str(_nearest_rank(half_spreads, stressed_quantile)),
                "impact_bps": str(_nearest_rank(stressed_impacts, stressed_quantile)),
            },
        }

    observed_times = tuple(
        row.observed_at for rows in normalized_by_product.values() for row in rows
    )
    payload: dict[str, object] = {
        "schema_id": _MEASUREMENT_SCHEMA,
        "schema_version": _MEASUREMENT_VERSION,
        "provider": "coinbase_advanced_trade",
        "book_endpoint": normalized_book_endpoint,
        "fee_endpoint": normalized_fee_endpoint,
        "fee_snapshot": {
            "maker_fee_rate": str(maker_rate),
            "maker_fee_bps": str(maker_rate * _BPS),
            "taker_fee_rate": str(taker_rate),
            "taker_fee_bps": str(taker_rate * _BPS),
            "private_account_fields_retained": False,
        },
        "sampling": {
            "products": list(products),
            "samples_required_per_product": sample_count,
            "samples_observed_per_product": sample_count,
            "missing_sample_policy": "fail_complete_measurement",
            "depth_limit_levels_per_side": depth_limit,
            "measured_at_utc": measured_at.isoformat().replace("+00:00", "Z"),
            "first_provider_timestamp": min(observed_times),
            "last_provider_timestamp": max(observed_times),
            "maximum_snapshot_age_seconds": maximum_age,
            "maximum_inter_sample_gap_seconds": maximum_gap,
            "maximum_provider_clock_skew_seconds": maximum_clock_skew,
        },
        "depth_walk": {
            "expected_quote_notional": str(expected_notional),
            "stressed_quote_notional": str(stressed_notional),
            "buy_vwap": "spend_exact_quote_notional_across_ascending_asks",
            "sell_vwap": "sell_quote_notional_divided_by_mid_base_quantity_across_descending_bids",
            "half_spread_bps": "(best_ask-best_bid)/(2*mid)*10000",
            "depth_impact_bps": "max(0,worst_side_adverse_mid_vwap_bps-half_spread_bps)",
            "side_aggregation": "maximum_of_buy_and_sell_depth_impact",
            "expected_aggregation": {
                "quantile": expected_quantile,
                "method": "nearest_rank_ceiling",
            },
            "stressed_aggregation": {
                "quantile": stressed_quantile,
                "method": "nearest_rank_ceiling",
            },
        },
        "raw_snapshot_sha256": evidence_sha256(raw_snapshot_payload),
        "raw_snapshots": raw_snapshot_payload,
        "product_summaries": product_summaries,
        "historical_application": "current_measured_applied_backward_with_sensitivity",
        "historical_order_books_available": False,
    }
    payload["measurement_sha256"] = evidence_sha256(payload)
    return payload


def _measure_registered_coinbase_execution_costs(
    protocol: Mapping[str, object],
    *,
    api_key: str,
    api_secret: str,
    samples: int,
    sample_interval_seconds: float,
    depth_levels: int,
    expected_notional_usd: str,
    stressed_notional_usd: str,
    measured_at_utc: datetime | None = None,
) -> dict[str, object]:
    """Acquire and attest the protocol-registered Coinbase cost evidence."""

    expected_products, cost_contract, sampling_contract, depth_contract = (
        registered_cost_measurement_inputs(
            protocol,
            samples=samples,
            depth_levels=depth_levels,
            expected_notional=expected_notional_usd,
            stressed_notional=stressed_notional_usd,
        )
    )
    expected_aggregation = depth_contract.get("expected_aggregation")
    stressed_aggregation = depth_contract.get("stressed_aggregation")
    if not isinstance(expected_aggregation, Mapping) or not isinstance(
        stressed_aggregation, Mapping
    ):
        message = "cost measurement quantile aggregations must be objects"
        raise TypeError(message)

    client = CoinbaseValidationClient(
        api_key=api_key,
        api_secret=api_secret,
    )
    try:
        fee_rates = client.fetch_spot_fee_rates()
        snapshot_payloads: dict[str, list[dict[str, Any]]] = {
            product: [] for product in sorted(expected_products)
        }
        for sample_index in range(samples):
            for product in sorted(expected_products):
                snapshot_payloads[product].append(
                    client.fetch_product_book(
                        expected_products[product],
                        limit=depth_levels,
                    )
                )
            if sample_index + 1 < samples:
                time.sleep(sample_interval_seconds)
    finally:
        client.close()
    return build_coinbase_execution_cost_measurement(
        snapshot_payloads,
        expected_provider_products=expected_products,
        maker_fee_rate=fee_rates["maker_fee_rate"],
        taker_fee_rate=fee_rates["taker_fee_rate"],
        depth_limit=depth_levels,
        expected_quote_notional=expected_notional_usd,
        stressed_quote_notional=stressed_notional_usd,
        measured_at_utc=(measured_at_utc or datetime.now(UTC)).isoformat(),
        maximum_snapshot_age_seconds=float(str(sampling_contract["maximum_snapshot_age_seconds"])),
        maximum_inter_sample_gap_seconds=float(
            str(sampling_contract["maximum_inter_sample_gap_seconds"])
        ),
        maximum_provider_clock_skew_seconds=float(
            str(sampling_contract["maximum_provider_clock_skew_seconds"])
        ),
        book_endpoint=str(cost_contract["book_endpoint"]),
        fee_endpoint=str(cost_contract["fee_endpoint"]),
        expected_quantile=float(str(expected_aggregation["quantile"])),
        stressed_quantile=float(str(stressed_aggregation["quantile"])),
    )


def create_registered_coinbase_execution_cost_measurement(
    protocol: Mapping[str, object],
    *,
    repo_root: Path,
    strategy_name: str,
    api_key: str,
    api_secret: str,
    samples: int,
    sample_interval_seconds: float,
    depth_levels: int,
    expected_notional_usd: str,
    stressed_notional_usd: str,
    output: Path | None,
    measured_at_utc: datetime | None = None,
) -> tuple[dict[str, object], Path]:
    """Acquire, publish, and reverify one registered cost measurement."""

    payload = _measure_registered_coinbase_execution_costs(
        protocol,
        api_key=api_key,
        api_secret=api_secret,
        samples=samples,
        sample_interval_seconds=sample_interval_seconds,
        depth_levels=depth_levels,
        expected_notional_usd=expected_notional_usd,
        stressed_notional_usd=stressed_notional_usd,
        measured_at_utc=measured_at_utc,
    )
    destination = resolve_cost_measurement_output(
        repo_root,
        strategy_name,
        str(payload["measurement_sha256"]),
        output,
    )
    atomic_create_bytes(destination, canonical_json_bytes(payload), mode=0o444)
    stored = load_json_object(destination)
    if stored != payload:
        message = "stored execution-cost measurement differs from generated payload"
        raise RuntimeError(message)
    verify_coinbase_execution_cost_measurement(stored)
    return payload, destination


def resolve_cost_measurement_output(
    repo_root: Path,
    strategy_name: str,
    measurement_hash: str,
    output: Path | None,
) -> Path:
    """Resolve one immutable Coinbase execution-cost measurement path."""

    if not re.fullmatch(r"[0-9a-f]{64}", measurement_hash):
        message = "measurement_hash must be a lowercase SHA-256 digest"
        raise ValueError(message)
    return resolve_strategy_validation_artifact(
        repo_root,
        expected_name=f"{strategy_name}-execution-cost-measurement-{measurement_hash}.json",
        output=output,
        field="cost measurement output",
    )


def _rebuild_measurement_from_raw(payload: Mapping[str, object]) -> dict[str, object]:
    raw = payload.get("raw_snapshots")
    if not isinstance(raw, Mapping):
        message = "execution-cost raw_snapshots must be an object"
        raise TypeError(message)
    sampling = payload.get("sampling")
    depth_walk = payload.get("depth_walk")
    fee_snapshot = payload.get("fee_snapshot")
    summaries = payload.get("product_summaries")
    if not isinstance(sampling, Mapping):
        message = "execution-cost sampling must be an object"
        raise TypeError(message)
    if not isinstance(depth_walk, Mapping):
        message = "execution-cost depth_walk must be an object"
        raise TypeError(message)
    if not isinstance(fee_snapshot, Mapping):
        message = "execution-cost fee_snapshot must be an object"
        raise TypeError(message)
    if not isinstance(summaries, Mapping):
        message = "execution-cost product_summaries must be an object"
        raise TypeError(message)
    expected_products: dict[str, str] = {}
    snapshots: dict[str, Sequence[Mapping[str, Any]]] = {}
    for product, raw_rows in raw.items():
        if not isinstance(product, str):
            message = "execution-cost product identity must be a string"
            raise TypeError(message)
        summary = summaries.get(product)
        if not isinstance(summary, Mapping):
            message = f"execution-cost summary missing for {product}"
            raise TypeError(message)
        expected_products[product] = nonblank_string(
            summary.get("provider_product"),
            field=f"product_summaries.{product}.provider_product",
        )
        if isinstance(raw_rows, (str, bytes)) or not isinstance(raw_rows, Sequence):
            message = f"execution-cost raw snapshots for {product} must be an array"
            raise TypeError(message)
        provider_payloads: list[Mapping[str, Any]] = []
        for index, row in enumerate(raw_rows):
            if not isinstance(row, Mapping):
                message = f"execution-cost raw snapshot {product}[{index}] must be an object"
                raise TypeError(message)
            provider_payloads.append(
                {
                    "pricebook": {
                        "product_id": row.get("provider_product"),
                        "time": row.get("observed_at"),
                        "bids": row.get("bids"),
                        "asks": row.get("asks"),
                    }
                }
            )
        snapshots[product] = provider_payloads
    expected_aggregation = depth_walk.get("expected_aggregation")
    stressed_aggregation = depth_walk.get("stressed_aggregation")
    if not isinstance(expected_aggregation, Mapping) or not isinstance(
        stressed_aggregation, Mapping
    ):
        message = "execution-cost quantile aggregations must be objects"
        raise TypeError(message)
    return build_coinbase_execution_cost_measurement(
        snapshots,
        expected_provider_products=expected_products,
        maker_fee_rate=nonblank_string(
            fee_snapshot.get("maker_fee_rate"), field="fee_snapshot.maker_fee_rate"
        ),
        taker_fee_rate=nonblank_string(
            fee_snapshot.get("taker_fee_rate"), field="fee_snapshot.taker_fee_rate"
        ),
        depth_limit=int(sampling["depth_limit_levels_per_side"]),
        expected_quote_notional=nonblank_string(
            depth_walk.get("expected_quote_notional"),
            field="depth_walk.expected_quote_notional",
        ),
        stressed_quote_notional=nonblank_string(
            depth_walk.get("stressed_quote_notional"),
            field="depth_walk.stressed_quote_notional",
        ),
        measured_at_utc=nonblank_string(
            sampling.get("measured_at_utc"), field="sampling.measured_at_utc"
        ),
        maximum_snapshot_age_seconds=float(sampling["maximum_snapshot_age_seconds"]),
        maximum_inter_sample_gap_seconds=float(sampling["maximum_inter_sample_gap_seconds"]),
        maximum_provider_clock_skew_seconds=float(sampling["maximum_provider_clock_skew_seconds"]),
        book_endpoint=nonblank_string(payload.get("book_endpoint"), field="book_endpoint"),
        fee_endpoint=nonblank_string(payload.get("fee_endpoint"), field="fee_endpoint"),
        expected_quantile=float(expected_aggregation["quantile"]),
        stressed_quantile=float(stressed_aggregation["quantile"]),
    )


def verify_coinbase_execution_cost_measurement(payload: Mapping[str, object]) -> None:
    """Recompute a stored measurement from its raw public-book evidence."""

    observed_measurement_hash = payload.get("measurement_sha256")
    if not isinstance(observed_measurement_hash, str):
        message = "execution-cost measurement omitted measurement_sha256"
        raise TypeError(message)
    unsigned = dict(payload)
    del unsigned["measurement_sha256"]
    if evidence_sha256(unsigned) != observed_measurement_hash:
        message = "execution-cost measurement content hash differs"
        raise ValueError(message)
    raw = payload.get("raw_snapshots")
    if evidence_sha256(raw) != payload.get("raw_snapshot_sha256"):
        message = "execution-cost raw snapshot hash differs"
        raise ValueError(message)
    rebuilt = _rebuild_measurement_from_raw(payload)
    if canonical_json_bytes(rebuilt) != canonical_json_bytes(payload):
        message = "execution-cost measurement differs from recomputed raw evidence"
        raise ValueError(message)
