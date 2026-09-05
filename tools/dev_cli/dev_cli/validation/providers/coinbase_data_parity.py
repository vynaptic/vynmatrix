"""Hash-attested Coinbase minute-to-daily data-parity evidence.

The implementation delegates reconciliation to the validation-only daily
reconciliation module. This module owns Coinbase input normalization,
provenance, and fail-closed attestation.
"""

from __future__ import annotations

import inspect
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

import dev_cli.validation.providers.daily_reconciliation as consolidation_module
from dev_cli.validation.evidence import (
    atomic_create_bytes,
    canonical_json_bytes,
    evidence_sha256,
    file_sha256,
    load_json_object,
    nonblank_string,
    parse_utc_datetime,
    resolve_strategy_validation_artifact,
    utc_iso,
)
from dev_cli.validation.providers.coinbase_protocol import (
    MAX_PUBLIC_CANDLE_CHUNK_BARS,
    CoinbaseValidationClient,
    fetch_half_open_candles,
    registered_data_parity_inputs,
)
from dev_cli.validation.providers.daily_reconciliation import (
    audit_minute_daily_reconciliation,
)
from lib_data.bars import Bar

_ATTESTATION_SCHEMA = "vynmatrix.coinbase-daily-parity-attestation"
_ATTESTATION_VERSION = 1
_PUBLIC_CANDLE_ENDPOINT = "/api/v3/brokerage/market/products/{product_id}/candles"
_BASE_URL = "https://api.coinbase.com"
_MINUTE_GRANULARITY = "ONE_MINUTE"
_DAILY_GRANULARITY = "ONE_DAY"
_MINUTE_CHUNK_BARS = 300
_PROVIDER_LIMIT = 350
_SOURCE_PATH = "dev_cli/validation/providers/daily_reconciliation.py"
_DAYS_PER_STRATUM = 2


def _finite_decimal_string(
    value: object,
    *,
    field: str,
    positive: bool,
) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        message = f"{field} must be numeric"
        raise TypeError(message)
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        message = f"{field} is not a decimal"
        raise ValueError(message) from exc
    if not parsed.is_finite() or (parsed <= 0 if positive else parsed < 0):
        qualifier = "positive" if positive else "non-negative"
        message = f"{field} must be finite and {qualifier}"
        raise ValueError(message)
    return format(parsed, "f")


def _positive_float(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        message = f"{field} must be numeric"
        raise TypeError(message)
    try:
        parsed = float(value)
    except ValueError as exc:
        message = f"{field} must be numeric"
        raise ValueError(message) from exc
    if not math.isfinite(parsed) or parsed <= 0:
        message = f"{field} must be finite and positive"
        raise ValueError(message)
    return parsed


def _consolidation_source_sha256() -> str:
    source = inspect.getsourcefile(consolidation_module)
    if source is None:
        message = "daily reconciliation has no inspectable source file"
        raise RuntimeError(message)
    path = Path(source)
    if not path.is_file():
        message = "daily reconciliation source file is unavailable"
        raise RuntimeError(message)
    return file_sha256(path)


def _normalized_candles(
    rows: object,
    *,
    product: str,
    stratum_id: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    require_complete: bool,
) -> tuple[list[dict[str, object]], list[Bar]]:
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        message = f"{product}.{stratum_id}.{timeframe} must be an array"
        raise TypeError(message)
    step = timedelta(minutes=1) if timeframe == "1m" else timedelta(days=1)
    expected_count = int((end - start) / step)
    normalized_by_start: dict[int, dict[str, object]] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            message = f"{product}.{stratum_id}.{timeframe}[{index}] must be an object"
            raise TypeError(message)
        raw_start = raw.get("start")
        if isinstance(raw_start, bool):
            message = f"{product}.{stratum_id}.{timeframe}[{index}].start must be an epoch"
            raise TypeError(message)
        try:
            timestamp = int(str(raw_start))
        except (TypeError, ValueError) as exc:
            message = f"{product}.{stratum_id}.{timeframe}[{index}].start must be an epoch"
            raise ValueError(message) from exc
        if str(timestamp) != str(raw_start):
            message = f"{product}.{stratum_id}.{timeframe}[{index}].start is not canonical"
            raise ValueError(message)
        if timestamp in normalized_by_start:
            message = f"{product}.{stratum_id}.{timeframe} contains duplicate timestamps"
            raise ValueError(message)
        candle = {
            "start": timestamp,
            "open": _finite_decimal_string(
                raw.get("open"), field=f"{product}.{timeframe}[{index}].open", positive=True
            ),
            "high": _finite_decimal_string(
                raw.get("high"), field=f"{product}.{timeframe}[{index}].high", positive=True
            ),
            "low": _finite_decimal_string(
                raw.get("low"), field=f"{product}.{timeframe}[{index}].low", positive=True
            ),
            "close": _finite_decimal_string(
                raw.get("close"), field=f"{product}.{timeframe}[{index}].close", positive=True
            ),
            "volume": _finite_decimal_string(
                raw.get("volume"),
                field=f"{product}.{timeframe}[{index}].volume",
                positive=False,
            ),
        }
        open_price = Decimal(str(candle["open"]))
        high = Decimal(str(candle["high"]))
        low = Decimal(str(candle["low"]))
        close = Decimal(str(candle["close"]))
        if high < max(open_price, close) or low > min(open_price, close) or low > high:
            message = f"{product}.{stratum_id}.{timeframe}[{index}] has invalid OHLC bounds"
            raise ValueError(message)
        normalized_by_start[timestamp] = candle

    expected_starts = tuple(
        int((start + step * offset).timestamp()) for offset in range(expected_count)
    )
    observed_starts = tuple(sorted(normalized_by_start))
    if not observed_starts or not set(observed_starts).issubset(expected_starts):
        message = (
            f"{product}.{stratum_id}.{timeframe} contains timestamps outside the "
            "registered half-open grid"
        )
        raise ValueError(message)
    if require_complete and observed_starts != expected_starts:
        message = (
            f"{product}.{stratum_id}.{timeframe} does not exactly cover the registered "
            f"half-open interval: expected={expected_count} observed={len(normalized_by_start)}"
        )
        raise ValueError(message)
    normalized = [normalized_by_start[timestamp] for timestamp in observed_starts]
    bars = [
        Bar(
            symbol=product,
            timestamp=datetime.fromtimestamp(int(str(row["start"])), tz=UTC),
            open=float(str(row["open"])),
            high=float(str(row["high"])),
            low=float(str(row["low"])),
            close=float(str(row["close"])),
            volume=float(str(row["volume"])),
            timeframe=timeframe,
            source="coinbase_public_advanced_trade_parity_v1",
        )
        for row in normalized
    ]
    return normalized, bars


def _metadata_snapshot(
    value: object,
    *,
    requested_product: str,
    expected_canonical_book: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        message = f"product metadata for {requested_product} must be an object"
        raise TypeError(message)
    fields = (
        "requested_product_id",
        "product_id",
        "canonical_book",
        "alias",
        "alias_to",
        "base_currency_id",
        "quote_currency_id",
        "quote_display_symbol",
        "base_increment",
        "quote_increment",
        "price_increment",
        "product_type",
        "status",
        "is_disabled",
        "trading_disabled",
    )
    missing = [field for field in fields if field not in value]
    if missing:
        message = f"product metadata for {requested_product} omits {', '.join(missing)}"
        raise ValueError(message)
    snapshot = {field: value[field] for field in fields}
    if (
        snapshot["requested_product_id"] != requested_product
        or snapshot["product_id"] != requested_product
        or snapshot["canonical_book"] != expected_canonical_book
        or snapshot["alias"] != expected_canonical_book
        or snapshot["is_disabled"] is not False
        or snapshot["trading_disabled"] is not False
    ):
        message = f"Coinbase product identity or tradability differs for {requested_product}"
        raise ValueError(message)
    alias_to = snapshot["alias_to"]
    if isinstance(alias_to, (str, bytes)) or not isinstance(alias_to, Sequence):
        message = f"Coinbase alias_to must be an array for {requested_product}"
        raise TypeError(message)
    snapshot["alias_to"] = list(alias_to)
    return snapshot


def _bar_disposition(audit_bar: Mapping[str, object]) -> str:
    if audit_bar.get("status") == "matched":
        return "exact_ohlcv_match"
    if audit_bar.get("status") != "field_mismatch":
        return "blocking_mismatch"
    raw_deltas = audit_bar.get("field_deltas")
    if isinstance(raw_deltas, (str, bytes)) or not isinstance(raw_deltas, Sequence):
        return "blocking_mismatch"
    mismatched_fields = {
        str(delta.get("field"))
        for delta in raw_deltas
        if isinstance(delta, Mapping) and delta.get("within_tolerance") is False
    }
    return (
        "non_blocking_volume_only_mismatch"
        if mismatched_fields == {"volume"}
        else "blocking_mismatch"
    )


def _normalized_calendar_strata(
    calendar_strata: Sequence[Mapping[str, object]],
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    stratum_ids: set[str] = set()
    for index, raw_stratum in enumerate(calendar_strata):
        if not isinstance(raw_stratum, Mapping):
            message = f"calendar_strata[{index}] must be an object"
            raise TypeError(message)
        stratum_id = nonblank_string(raw_stratum.get("id"), field=f"strata[{index}].id")
        if stratum_id in stratum_ids:
            message = f"duplicate calendar stratum: {stratum_id}"
            raise ValueError(message)
        stratum_ids.add(stratum_id)
        start = parse_utc_datetime(raw_stratum.get("start"), field=f"strata[{index}].start")
        end = parse_utc_datetime(
            raw_stratum.get("end_exclusive"), field=f"strata[{index}].end_exclusive"
        )
        if end - start != timedelta(days=_DAYS_PER_STRATUM) or start.time() != datetime.min.time():
            message = f"calendar stratum {stratum_id} must be exactly two UTC days"
            raise ValueError(message)
        normalized.append(
            {
                "id": stratum_id,
                "start": start.isoformat().replace("+00:00", "Z"),
                "end_exclusive": end.isoformat().replace("+00:00", "Z"),
            }
        )
    if not normalized:
        message = "calendar_strata must not be empty"
        raise ValueError(message)
    return normalized


def build_coinbase_daily_parity_attestation(
    source_candles: Mapping[str, Mapping[str, Mapping[str, object]]],
    *,
    product_metadata: Mapping[str, Mapping[str, object]],
    expected_canonical_books: Mapping[str, str],
    calendar_strata: Sequence[Mapping[str, object]],
    price_ticks: Mapping[str, object],
    measured_at_utc: str,
    minimum_constituent_coverage: float = 0.95,
    volume_absolute_tolerance: float = 1e-8,
    volume_relative_tolerance: float = 1e-6,
    strategy_uses_volume: bool = False,
) -> dict[str, object]:
    """Build a self-verifying attestation from complete public candle evidence."""

    products = tuple(sorted(source_candles))
    if (
        not products
        or set(products) != set(product_metadata)
        or set(products) != set(expected_canonical_books)
        or set(products) != set(price_ticks)
    ):
        message = "source, metadata, canonical-book, and price-tick products must match exactly"
        raise ValueError(message)
    if isinstance(strategy_uses_volume, bool) is False:
        message = "strategy_uses_volume must be a bool"
        raise TypeError(message)
    measured_at = utc_iso(measured_at_utc, field="measured_at_utc")
    normalized_strata = _normalized_calendar_strata(calendar_strata)
    stratum_ids = {row["id"] for row in normalized_strata}

    normalized_inputs: dict[str, object] = {}
    product_rows: dict[str, object] = {}
    ledger: list[dict[str, object]] = []
    for product in products:
        metadata = _metadata_snapshot(
            product_metadata[product],
            requested_product=product,
            expected_canonical_book=expected_canonical_books[product],
        )
        tick = _positive_float(price_ticks[product], field=f"price_ticks.{product}")
        raw_product_strata = source_candles[product]
        if set(raw_product_strata) != stratum_ids:
            message = f"source strata for {product} do not match the registered strata"
            raise ValueError(message)
        product_inputs: dict[str, object] = {}
        product_audits: list[dict[str, object]] = []
        for stratum in normalized_strata:
            stratum_id = stratum["id"]
            raw_pair = raw_product_strata[stratum_id]
            if not isinstance(raw_pair, Mapping) or set(raw_pair) != {
                _MINUTE_GRANULARITY,
                _DAILY_GRANULARITY,
            }:
                message = f"{product}.{stratum_id} must contain exactly ONE_MINUTE and ONE_DAY"
                raise ValueError(message)
            start = parse_utc_datetime(stratum["start"], field=f"{stratum_id}.start")
            end = parse_utc_datetime(stratum["end_exclusive"], field=f"{stratum_id}.end_exclusive")
            minute_rows, minute_bars = _normalized_candles(
                raw_pair[_MINUTE_GRANULARITY],
                product=product,
                stratum_id=stratum_id,
                timeframe="1m",
                start=start,
                end=end,
                require_complete=False,
            )
            daily_rows, daily_bars = _normalized_candles(
                raw_pair[_DAILY_GRANULARITY],
                product=product,
                stratum_id=stratum_id,
                timeframe="1d",
                start=start,
                end=end,
                require_complete=True,
            )
            inputs = {
                _MINUTE_GRANULARITY: minute_rows,
                _DAILY_GRANULARITY: daily_rows,
            }
            product_inputs[stratum_id] = inputs
            audit = audit_minute_daily_reconciliation(
                minute_bars,
                daily_bars,
                price_tick=tick,
                minimum_constituent_coverage=minimum_constituent_coverage,
                volume_absolute_tolerance=volume_absolute_tolerance,
                volume_relative_tolerance=volume_relative_tolerance,
            ).to_dict()
            audit_bars = audit.get("bars")
            if not isinstance(audit_bars, list) or len(audit_bars) != _DAYS_PER_STRATUM:
                message = f"{product}.{stratum_id} did not produce exactly two daily audits"
                raise ValueError(message)
            for raw_audit_bar in audit_bars:
                if not isinstance(raw_audit_bar, Mapping):
                    message = f"{product}.{stratum_id} produced malformed audit evidence"
                    raise TypeError(message)
                audit_bar = dict(raw_audit_bar)
                disposition = _bar_disposition(audit_bar)
                if disposition == "blocking_mismatch" or (
                    disposition == "non_blocking_volume_only_mismatch" and strategy_uses_volume
                ):
                    message = (
                        f"{product}.{stratum_id} parity is blocking: "
                        f"close={audit_bar.get('close_timestamp')} disposition={disposition}"
                    )
                    raise ValueError(message)
                ledger.append(
                    {
                        "product": product,
                        "canonical_book": expected_canonical_books[product],
                        "stratum_id": stratum_id,
                        "disposition": disposition,
                        **audit_bar,
                    }
                )
            product_audits.append(
                {
                    "stratum_id": stratum_id,
                    "input_source_sha256": evidence_sha256(inputs),
                }
            )
        normalized_inputs[product] = product_inputs
        product_rows[product] = {
            "canonical_book": expected_canonical_books[product],
            "price_tick": str(Decimal(str(tick))),
            "metadata_snapshot": metadata,
            "strata": product_audits,
        }

    full_matches = sum(row["disposition"] == "exact_ohlcv_match" for row in ledger)
    volume_mismatches = sum(
        row["disposition"] == "non_blocking_volume_only_mismatch" for row in ledger
    )
    summary = {
        "minute_candles_expected": len(ledger) * 1_440,
        "minute_candles_observed": sum(int(str(row["constituent_bars"])) for row in ledger),
        "minimum_daily_constituent_coverage_observed": min(
            float(str(row["coverage"])) for row in ledger
        ),
        "sampled_daily_bars": len(ledger),
        "exact_price_and_timestamp_matches": len(ledger),
        "full_ohlcv_matches": full_matches,
        "volume_only_mismatches": volume_mismatches,
        "blocking_mismatches": 0,
        "status": (
            "exact_full_ohlcv_match"
            if volume_mismatches == 0
            else "provider_cross_granularity_volume_exception"
        ),
        "campaign_disposition": (
            "blocking_for_volume_dependent_strategy"
            if strategy_uses_volume and volume_mismatches
            else (
                "eligible_for_volume_dependent_ohlcv_rules"
                if strategy_uses_volume
                else "eligible_for_volume_independent_price_rules"
            )
        ),
    }
    payload: dict[str, object] = {
        "schema_id": _ATTESTATION_SCHEMA,
        "schema_version": _ATTESTATION_VERSION,
        "provider": "coinbase_advanced_trade",
        "base_url": _BASE_URL,
        "candle_endpoint": _PUBLIC_CANDLE_ENDPOINT,
        "endpoint_kind": "public_advanced_trade",
        "fetch_contract": {
            "minute_granularity": _MINUTE_GRANULARITY,
            "daily_granularity": _DAILY_GRANULARITY,
            "minute_request_chunk_bars": _MINUTE_CHUNK_BARS,
            "provider_limit": _PROVIDER_LIMIT,
            "incomplete_chunk_refetches_required": 2,
            "range_semantics": "exact_half_open_after_inclusive_provider_filter",
            "minute_gap_policy": "retain_never_fill_and_enforce_registered_daily_coverage",
        },
        "measured_at_utc": measured_at,
        "calendar_strata": normalized_strata,
        "strategy_field_dependency": {
            "price_and_timestamp_fields": "blocking",
            "volume": "consumed" if strategy_uses_volume else "not_consumed",
        },
        "reconciliation": {
            "module": "dev_cli.validation.providers.daily_reconciliation",
            "callable": "audit_minute_daily_reconciliation",
            "source_path": _SOURCE_PATH,
            "source_sha256": _consolidation_source_sha256(),
            "production_consolidation_minutes": 1440,
            "minimum_constituent_coverage": minimum_constituent_coverage,
            "volume_absolute_tolerance": volume_absolute_tolerance,
            "volume_relative_tolerance": volume_relative_tolerance,
        },
        "source_input_sha256": evidence_sha256(normalized_inputs),
        "source_inputs": normalized_inputs,
        "products": product_rows,
        "daily_audit_ledger": ledger,
        "summary": summary,
    }
    payload["attestation_sha256"] = evidence_sha256(payload)
    return payload


def _measure_registered_coinbase_data_parity(
    protocol: Mapping[str, object],
    *,
    request_interval_seconds: float,
    measured_at_utc: datetime | None = None,
) -> dict[str, object]:
    """Acquire and attest the protocol-registered Coinbase parity windows."""

    parity_inputs = registered_data_parity_inputs(protocol)
    client = CoinbaseValidationClient()
    try:
        source_candles: dict[str, dict[str, dict[str, object]]] = {}
        metadata: dict[str, Mapping[str, object]] = {}
        for product in sorted(parity_inputs["expected_canonical_books"]):
            metadata[product] = client.fetch_product_metadata(product).to_manifest_dict()
            source_candles[product] = {}
            for stratum in parity_inputs["calendar_strata"]:
                start = parse_utc_datetime(stratum["start"], field=f"{stratum['id']}.start")
                end = parse_utc_datetime(
                    stratum["end_exclusive"],
                    field=f"{stratum['id']}.end_exclusive",
                )
                source_candles[product][str(stratum["id"])] = {
                    "ONE_MINUTE": fetch_half_open_candles(
                        client,
                        product_id=product,
                        start=start,
                        end=end,
                        granularity="ONE_MINUTE",
                        period=timedelta(minutes=1),
                        chunk_bars=MAX_PUBLIC_CANDLE_CHUNK_BARS,
                        request_interval_seconds=request_interval_seconds,
                        require_complete=False,
                    ),
                    "ONE_DAY": fetch_half_open_candles(
                        client,
                        product_id=product,
                        start=start,
                        end=end,
                        granularity="ONE_DAY",
                        period=timedelta(days=1),
                        chunk_bars=MAX_PUBLIC_CANDLE_CHUNK_BARS,
                        request_interval_seconds=request_interval_seconds,
                        require_complete=True,
                    ),
                }
    finally:
        client.close()
    return build_coinbase_daily_parity_attestation(
        source_candles,
        product_metadata=metadata,
        expected_canonical_books=parity_inputs["expected_canonical_books"],
        calendar_strata=parity_inputs["calendar_strata"],
        price_ticks=parity_inputs["price_ticks"],
        measured_at_utc=(measured_at_utc or datetime.now(UTC)).isoformat(),
        minimum_constituent_coverage=parity_inputs["minimum_constituent_coverage"],
        volume_absolute_tolerance=parity_inputs["volume_absolute_tolerance"],
        volume_relative_tolerance=parity_inputs["volume_relative_tolerance"],
        strategy_uses_volume=parity_inputs["strategy_uses_volume"],
    )


def create_registered_coinbase_data_parity_attestation(
    protocol: Mapping[str, object],
    *,
    repo_root: Path,
    strategy_name: str,
    request_interval_seconds: float,
    output: Path | None,
    measured_at_utc: datetime | None = None,
) -> tuple[dict[str, object], Path]:
    """Acquire, publish, and reverify one registered parity attestation."""

    payload = _measure_registered_coinbase_data_parity(
        protocol,
        request_interval_seconds=request_interval_seconds,
        measured_at_utc=measured_at_utc,
    )
    destination = resolve_data_parity_output(
        repo_root,
        strategy_name,
        str(payload["attestation_sha256"]),
        output,
    )
    atomic_create_bytes(destination, canonical_json_bytes(payload), mode=0o444)
    stored = load_json_object(destination)
    if stored != payload:
        message = "stored data-parity attestation differs from generated payload"
        raise RuntimeError(message)
    verify_coinbase_daily_parity_attestation(stored)
    return payload, destination


def resolve_data_parity_output(
    repo_root: Path,
    strategy_name: str,
    attestation_hash: str,
    output: Path | None,
) -> Path:
    """Resolve one immutable Coinbase parity attestation path."""

    if not re.fullmatch(r"[0-9a-f]{64}", attestation_hash):
        message = "attestation_hash must be a lowercase SHA-256 digest"
        raise ValueError(message)
    return resolve_strategy_validation_artifact(
        repo_root,
        expected_name=f"{strategy_name}-data-parity-attestation-{attestation_hash}.json",
        output=output,
        field="data parity output",
    )


def _rebuild_attestation(payload: Mapping[str, object]) -> dict[str, object]:
    raw_inputs = payload.get("source_inputs")
    products = payload.get("products")
    strata = payload.get("calendar_strata")
    reconciliation = payload.get("reconciliation")
    dependency = payload.get("strategy_field_dependency")
    if not isinstance(raw_inputs, Mapping) or not isinstance(products, Mapping):
        message = "data-parity source_inputs and products must be objects"
        raise TypeError(message)
    if isinstance(strata, (str, bytes)) or not isinstance(strata, Sequence):
        message = "data-parity calendar_strata must be an array"
        raise TypeError(message)
    if not isinstance(reconciliation, Mapping) or not isinstance(dependency, Mapping):
        message = "data-parity reconciliation/dependency must be objects"
        raise TypeError(message)
    metadata: dict[str, Mapping[str, object]] = {}
    expected_books: dict[str, str] = {}
    ticks: dict[str, object] = {}
    for product, raw_product in products.items():
        if not isinstance(product, str) or not isinstance(raw_product, Mapping):
            message = "data-parity product evidence is malformed"
            raise TypeError(message)
        raw_metadata = raw_product.get("metadata_snapshot")
        if not isinstance(raw_metadata, Mapping):
            message = f"data-parity metadata is missing for {product}"
            raise TypeError(message)
        metadata[product] = raw_metadata
        expected_books[product] = nonblank_string(
            raw_product.get("canonical_book"), field=f"products.{product}.canonical_book"
        )
        ticks[product] = raw_product.get("price_tick")
    return build_coinbase_daily_parity_attestation(
        cast(Mapping[str, Mapping[str, Mapping[str, object]]], raw_inputs),
        product_metadata=metadata,
        expected_canonical_books=expected_books,
        calendar_strata=cast(Sequence[Mapping[str, object]], strata),
        price_ticks=ticks,
        measured_at_utc=nonblank_string(payload.get("measured_at_utc"), field="measured_at_utc"),
        minimum_constituent_coverage=float(reconciliation["minimum_constituent_coverage"]),
        volume_absolute_tolerance=float(reconciliation["volume_absolute_tolerance"]),
        volume_relative_tolerance=float(reconciliation["volume_relative_tolerance"]),
        strategy_uses_volume=dependency.get("volume") == "consumed",
    )


def verify_coinbase_daily_parity_attestation(payload: Mapping[str, object]) -> None:
    """Recompute the complete attestation from its embedded public candles."""

    observed_hash = payload.get("attestation_sha256")
    if not isinstance(observed_hash, str):
        message = "data-parity attestation omitted attestation_sha256"
        raise TypeError(message)
    unsigned = dict(payload)
    del unsigned["attestation_sha256"]
    if evidence_sha256(unsigned) != observed_hash:
        message = "data-parity attestation content hash differs"
        raise ValueError(message)
    if evidence_sha256(payload.get("source_inputs")) != payload.get("source_input_sha256"):
        message = "data-parity source input hash differs"
        raise ValueError(message)
    rebuilt = _rebuild_attestation(payload)
    if canonical_json_bytes(rebuilt) != canonical_json_bytes(payload):
        message = "data-parity attestation differs from recomputed source evidence"
        raise ValueError(message)


__all__ = [
    "build_coinbase_daily_parity_attestation",
    "create_registered_coinbase_data_parity_attestation",
    "resolve_data_parity_output",
    "verify_coinbase_daily_parity_attestation",
]
