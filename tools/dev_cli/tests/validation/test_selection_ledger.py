"""Tests for immutable upstream strategy-selection ledger attestation."""

from __future__ import annotations

import csv
import hashlib
import io
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from dev_cli.validation.persistence.backtest_selection_ledger import (
    load_upstream_selection_ledger,
    validate_embedded_selection_ledger,
    validate_upstream_selection_ledger_contract,
)

_COLUMNS = [
    "python_file",
    "class_name",
    "symbol",
    "timeframe",
    "family",
    "prior_score",
    "was_in_shortlist",
    "readiness",
    "status",
    "error_type",
    "error",
    "bars",
    "total_return_pct",
    "annualized_pct",
    "sharpe_daily_ann",
    "max_drawdown_pct",
    "sqn",
    "trades_closed",
    "win_rate_pct",
    "pnl_net",
    "runtime_sec",
    "y2020",
    "y2021",
    "y2022",
    "y2023",
    "y2024",
    "y2025",
    "y2026",
]


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def _row(
    python_file: str,
    class_name: str,
    symbol: str,
    status: str,
    sharpe: str,
) -> dict[str, str]:
    return dict.fromkeys(_COLUMNS, "") | {
        "python_file": python_file,
        "class_name": class_name,
        "symbol": symbol,
        "timeframe": "1d",
        "family": "trend",
        "status": status,
        "sharpe_daily_ann": sharpe,
    }


def _contract(raw: bytes) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source_filename": "master_results.csv",
        "expected_sha256": hashlib.sha256(raw).hexdigest(),
        "required_columns": list(_COLUMNS),
        "identity_fields": ["python_file", "class_name", "symbol", "timeframe"],
        "status_field": "status",
        "expected_status_counts": {"ok": 2, "error": 1, "timeout": 1},
        "sharpe_field": "sharpe_daily_ann",
        "expected_row_count": 4,
        "expected_finite_sharpe_count": 2,
        "finite_sharpe_inclusion_policy": "all_nonblank_finite_sharpe_values",
        "non_ok_sharpe_must_be_blank": True,
        "sample_standard_deviation_ddof": 1,
        "summary_decimal_places": 15,
        "expected_finite_sharpe_sample_std": "1.414213562373095",
        "resume_source": "embedded_manifest_only",
    }


def _valid_raw() -> bytes:
    return _csv_bytes(
        [
            _row("a.py", "A", "BTC-USD", "ok", "-1.000"),
            _row("b.py", "B", "SPY", "ok", "1"),
            _row("c.py", "C", "BTC-USD", "error", ""),
            _row("d.py", "D", "SPY", "timeout", ""),
        ]
    )


def test_upstream_selection_ledger_is_hash_attested_and_normalized(tmp_path: Path) -> None:
    raw = _valid_raw()
    source = tmp_path / "master_results.csv"
    source.write_bytes(raw)
    contract = _contract(raw)

    payload = load_upstream_selection_ledger(source, contract)

    assert payload["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert payload["summary"] == {
        "row_count": 4,
        "status_counts": {"error": 1, "ok": 2, "timeout": 1},
        "finite_sharpe_count": 2,
        "finite_sharpe_mean": "0.000000000000000",
        "finite_sharpe_sample_std": "1.414213562373095",
    }
    assert [row["sequence"] for row in payload["trials"]] == [0, 1, 2, 3]
    assert [row["sharpe_daily_ann"] for row in payload["trials"]] == [
        "-1",
        "1",
        None,
        None,
    ]
    validate_embedded_selection_ledger(payload, contract)


def test_upstream_selection_ledger_rejects_hash_schema_and_identity_drift(
    tmp_path: Path,
) -> None:
    raw = _valid_raw()
    source = tmp_path / "master_results.csv"
    source.write_bytes(raw)

    wrong_hash = _contract(raw)
    wrong_hash["expected_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256 differs"):
        load_upstream_selection_ledger(source, wrong_hash)

    reordered = [*_COLUMNS]
    reordered[0], reordered[1] = reordered[1], reordered[0]
    wrong_schema = _contract(raw)
    wrong_schema["required_columns"] = reordered
    with pytest.raises(ValueError, match="columns or column order"):
        load_upstream_selection_ledger(source, wrong_schema)

    duplicate_raw = _csv_bytes(
        [
            _row("a.py", "A", "BTC-USD", "ok", "-1"),
            _row("a.py", "A", "BTC-USD", "ok", "1"),
            _row("c.py", "C", "BTC-USD", "error", ""),
            _row("d.py", "D", "SPY", "timeout", ""),
        ]
    )
    source.write_bytes(duplicate_raw)
    with pytest.raises(ValueError, match="duplicate identity"):
        load_upstream_selection_ledger(source, _contract(duplicate_raw))


def test_upstream_selection_ledger_excludes_missing_but_rejects_nonfinite_values(
    tmp_path: Path,
) -> None:
    nonfinite_raw = _csv_bytes(
        [
            _row("a.py", "A", "BTC-USD", "ok", "NaN"),
            _row("b.py", "B", "SPY", "ok", "1"),
            _row("c.py", "C", "BTC-USD", "error", ""),
            _row("d.py", "D", "SPY", "timeout", ""),
        ]
    )
    source = tmp_path / "master_results.csv"
    source.write_bytes(nonfinite_raw)

    with pytest.raises(ValueError, match="must be finite"):
        load_upstream_selection_ledger(source, _contract(nonfinite_raw))

    non_ok_sharpe_raw = _csv_bytes(
        [
            _row("a.py", "A", "BTC-USD", "ok", "-1"),
            _row("b.py", "B", "SPY", "ok", "1"),
            _row("c.py", "C", "BTC-USD", "error", "0"),
            _row("d.py", "D", "SPY", "timeout", ""),
        ]
    )
    source.write_bytes(non_ok_sharpe_raw)
    with pytest.raises(ValueError, match="non-ok upstream trials"):
        load_upstream_selection_ledger(source, _contract(non_ok_sharpe_raw))


def test_embedded_upstream_selection_ledger_is_recomputed_on_resume(tmp_path: Path) -> None:
    raw = _valid_raw()
    source = tmp_path / "master_results.csv"
    source.write_bytes(raw)
    contract = _contract(raw)
    payload = load_upstream_selection_ledger(source, contract)

    tampered = deepcopy(payload)
    tampered["trials"][0]["sharpe_daily_ann"] = "0"
    with pytest.raises(ValueError, match="differs from the frozen protocol"):
        validate_embedded_selection_ledger(tampered, contract)

    changed_contract = deepcopy(contract)
    changed_contract["sample_standard_deviation_ddof"] = 0
    with pytest.raises(ValueError, match="ddof=1"):
        validate_upstream_selection_ledger_contract(changed_contract)
