"""Research imports reject ambiguous or lossy data before persistence."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

METADATA = {
    "instrument_id": 1,
    "source": "recorded-provider",
    "provenance": "provider archive 2026-01",
    "currency": "USD",
    "timeframe": "1m",
    "timestamp_semantics": "bar_open",
    "adjustment_policy": "raw",
}
CSV = b"ts,open,high,low,close,volume\n2026-01-01T00:00:00Z,1.000000000000000001,2,1,1.5,0\n2026-01-01T00:01:00Z,1.5,2,1,1.75,2\n"


def parse(content: bytes = CSV, **metadata: Any) -> dict[str, Any]:
    from lib_data.research_dataset import parse_research_csv

    return parse_research_csv(content, {**METADATA, **metadata}, asset_class="crypto")


def test_precision_and_original_hash_survive_a_valid_import() -> None:
    result = parse()
    assert result["valid"] is True
    assert result["original_hash"] == hashlib.sha256(CSV).hexdigest()
    assert result["bars"][0]["open"] == "1.000000000000000001"
    assert result["bars"][0]["ts"] == "2026-01-01T00:00:00Z"
    assert result["report"]["bar_count"] == 2


def test_canonical_identity_ignores_numeric_spelling_but_not_provenance() -> None:
    left = parse()
    right = parse(CSV.replace(b",2,1,1.5,0", b",2.0,1.0,1.50,0.00"))
    assert left["content_hash"] == right["content_hash"]
    assert left["original_hash"] != right["original_hash"]
    assert left["content_hash"] != parse(provenance="another archive")["content_hash"]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (CSV.replace(b"volume\n", b"volume,volume\n"), "header"),
        (CSV.replace(b"volume\n", b"volume,secret\n"), "header"),
        (CSV.replace(b"00:00:00Z", b"00:00:00"), "timezone"),
        (CSV.replace(b",2,1,1.5,0", b",NaN,1,1.5,0"), "finite"),
        (CSV.replace(b",2,1,1.5,0", b",1,1,1.5,0"), "OHLC"),
        (CSV.replace(b"00:01:00Z", b"00:00:00Z"), "increasing"),
        (CSV.replace(b"00:01:00Z", b"00:03:00Z"), "cadence"),
        (CSV.replace(b",2,1,1.5,0", b",2,1,1.5,-1"), "negative"),
        (b"\xff", "UTF-8"),
    ],
)
def test_malformed_data_returns_a_failed_receipt(content: bytes, reason: str) -> None:
    result = parse(content)
    assert result["valid"] is False
    assert result["content_hash"] is None
    assert result["bars"] == []
    assert reason in str(result["report"]["errors"])


def test_unknown_metadata_cannot_smuggle_an_owner_or_path() -> None:
    with pytest.raises(ValueError, match="Unknown metadata"):
        parse(owner_id="someone-else")


@pytest.mark.parametrize("field", ["timestamp_semantics", "adjustment_policy"])
def test_unhashable_metadata_is_a_validation_refusal(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        parse(**{field: []})


@pytest.mark.parametrize("value", ["provider\x00archive", "bad\ud800unicode"])
def test_metadata_requires_postgresql_safe_utf8(value: str) -> None:
    with pytest.raises(ValueError, match="source"):
        parse(source=value)


def test_python_decimal_extensions_are_not_csv_numbers() -> None:
    result = parse(CSV.replace(b",2,1,1.5,0", b",2,1,1_0,0"))
    assert result["valid"] is False
    assert "decimal" in str(result["report"]["errors"])


@pytest.mark.parametrize("timestamp", [b"0001-01-01T00:00:00+01:00", b"9999-12-31T23:59:59-01:00"])
def test_timezone_overflow_is_a_bounded_validation_receipt(timestamp: bytes) -> None:
    result = parse(CSV.replace(b"2026-01-01T00:00:00Z", timestamp))
    assert result["valid"] is False
    assert result["content_hash"] is None
    assert "timestamp" in str(result["report"]["errors"])


def test_session_data_requires_explicit_timing_for_eligibility() -> None:
    result = parse()
    assert result["bars"][0].get("bar_close") is None
    # The parser preserves structural validity. Catalogue/calendar authority is
    # resolved by the application, never invented in this pure library.
    assert "eligibility" not in result


def test_size_row_and_error_sampling_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import lib_data.research_dataset as module

    monkeypatch.setattr(module, "MAX_IMPORT_BYTES", len(CSV) - 1)
    with pytest.raises(ValueError, match="byte limit"):
        parse()
    monkeypatch.setattr(module, "MAX_IMPORT_BYTES", 10_000)
    monkeypatch.setattr(module, "MAX_IMPORT_BARS", 1)
    assert parse()["valid"] is False
    monkeypatch.setattr(module, "MAX_IMPORT_BARS", 100)
    bad = b"ts,open,high,low,close,volume\n" + b"bad,0,0,0,0,-1\n" * 30
    result = parse(bad)
    assert result["report"]["error_count"] == 30
    assert len(result["report"]["errors"]) == 20
