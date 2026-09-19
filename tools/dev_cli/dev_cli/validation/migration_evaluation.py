"""Frozen retrospective comparisons of native ports using the existing backtest engine.

Provider daily bars, fixed rule defaults, explicit long-only projection and current
cost assumptions applied backward are research evidence, never trading authority.
"""

from __future__ import annotations

import argparse
import importlib
import statistics
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

from dev_cli.validation.backtest.engine import BacktestConfig, BacktestEngine, BacktestReport
from dev_cli.validation.backtest.execution import ExecutionCostModel, FillPolicy
from dev_cli.validation.backtest.walk_forward import FoldOOSSeries, pool_fold_oos_returns
from dev_cli.validation.backtrader_reference import package_identity
from dev_cli.validation.benchmarks import BenchmarkDefinition, run_consolidated_benchmark
from dev_cli.validation.evidence import (
    evidence_sha256,
    file_sha256,
    load_json_object,
    parse_utc_datetime,
    require_descendant,
    write_content_addressed_manifest,
)
from dev_cli.validation.historical_dataset import DatasetAudit, audit_bars
from lib_common.runner_utils import build_strategy_core_parameters
from lib_data.bars import Bar
from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import PureSignalStrategy

_DERIVED_SOURCE = "coinbase_validation_v1"
_MIN_CORRELATION_SAMPLES = 2
_SELECTED_STRATEGY_COUNT = 10


def load_daily_bars(payload: dict[str, Any]) -> tuple[list[Bar], DatasetAudit]:
    """Validate original provider bytes and preserve the documented book alias."""
    request, product, rows = payload["request"], payload["product_metadata"], payload["bars"]
    symbol = request["requested_product"]
    canonical = product.get("alias") or product["product_id"]
    if (
        evidence_sha256(rows) != payload["bars_sha256"]
        or canonical != request["expected_canonical_book"]
        or product["requested_product_id"] != symbol
        or product["product_id"] != symbol
        or request["granularity"] != "ONE_DAY"
        or request["timestamp_semantics"] != "period_start"
    ):
        msg = "Frozen provider data hash, product identity or daily timestamp contract changed"
        raise ValueError(msg)
    metadata = {
        "timestamp_semantics": "period_start",
        "asset_class": "crypto",
        "requested_product": symbol,
        "canonical_product": canonical,
        "base_currency": product["base_currency_id"],
        "quote_currency": product["quote_currency_id"],
        "provider_source": payload["source"],
    }
    bars = [
        Bar(
            symbol=symbol,
            timestamp=datetime.fromtimestamp(int(row["start"]), UTC),
            **{key: float(row[key]) for key in ("open", "high", "low", "close", "volume")},
            timeframe="1d",
            source=_DERIVED_SOURCE,
            metadata=dict(metadata),
        )
        for row in rows
    ]
    audit = audit_bars(
        bars,
        symbol=symbol,
        source=_DERIVED_SOURCE,
        timeframe="1d",
        start=parse_utc_datetime(request["start"], field="data start", strict_utc=True),
        end=parse_utc_datetime(request["end_exclusive"], field="data end", strict_utc=True),
    )
    if not audit.is_well_formed or audit.coverage != 1.0:
        msg = "Frozen daily history is incomplete or malformed"
        raise ValueError(msg)
    return bars, audit


def evaluation_windows() -> list[dict[str, str]]:
    """Map provider calendar years to half-open completed-close intervals."""
    return [
        {
            "name": name,
            "role": role,
            "start": datetime(start, 1, 2, tzinfo=UTC).isoformat(),
            "end": datetime(end, 1, 2, tzinfo=UTC).isoformat(),
        }
        for name, role, start, end in [
            ("is_2020_2021", "descriptive_in_sample", 2020, 2022),
            *(
                (f"oos_{year}", "retrospective_out_of_sample", year, year + 1)
                for year in range(2022, 2026)
            ),
        ]
    ]


def aligned_correlation(
    left: list[tuple[datetime, float]],
    right: list[tuple[datetime, float]],
) -> float | None:
    """Require an identical grid; constant return series have no correlation."""
    if [row[0] for row in left] != [row[0] for row in right]:
        msg = "Return correlation requires an identical completed-close grid"
        raise ValueError(msg)
    first, second = [row[1] for row in left], [row[1] for row in right]
    if (
        len(first) < _MIN_CORRELATION_SAMPLES
        or len(set(first)) < _MIN_CORRELATION_SAMPLES
        or len(set(second)) < _MIN_CORRELATION_SAMPLES
    ):
        return None
    return statistics.correlation(first, second)


def freeze_design(repo_root: Path, dataset_path: Path) -> tuple[dict[str, Any], list[Bar]]:
    """Bind defaults and assumptions before evaluating any outcomes."""
    dataset = load_json_object(dataset_path)
    bars, audit = load_daily_bars(dataset)
    if audit.start != datetime(2019, 10, 1, tzinfo=UTC) or audit.end != datetime(
        2026, 1, 1, tzinfo=UTC
    ):
        msg = "This retrospective design requires the frozen 2019-10-01 to 2026-01-01 interval"
        raise ValueError(msg)
    cost_path = (
        repo_root
        / "tests/fixtures/strategy_validation/registered_campaign/validation_protocol.json"
    )
    template = load_json_object(cost_path)
    symbol = audit.symbol
    scenarios = {}
    for name in ("gross", "expected", "stressed"):
        costs = template["cost_scenarios"][name]
        market = costs["products"][symbol]
        scenarios[name] = asdict(
            ExecutionCostModel(
                scenario_name=name,
                commission_bps=float(costs["commission_bps"]),
                half_spread_bps=float(market["half_spread_bps"]),
                impact_bps=float(market["impact_bps"]),
                slippage_bps=float(costs["slippage_bps"]),
                latency_bars=costs["fill_delay_bars"],
                historical_basis="configured-flat-fee"
                if name == "gross"
                else "backward-applied-scenario",
            )
        )
    strategies = []
    for config_path in sorted((repo_root / "strategies/indicator").glob("*/config.json")):
        config = load_json_object(config_path)
        if "source_file" not in config.get("metadata", {}):
            continue
        strategies.append(
            {
                "name": config_path.parent.name,
                "config_sha256": file_sha256(config_path),
                "core_sha256": file_sha256(config_path.parent / "core.py"),
                "strategy_id": config["strategy_id"],
                "strategy_version": config["strategy_version"],
                "parameters": config["parameters"],
                "source_sha256": config["metadata"]["source_sha256"],
            }
        )
    if len(strategies) != _SELECTED_STRATEGY_COUNT:
        msg = "The frozen migration comparison expects the ten selected source-bound ports"
        raise ValueError(msg)
    return {
        "schema": "vynmatrix.native-migration-retrospective.v1",
        "evaluation_code_sha256": file_sha256(Path(__file__)),
        "loaded_code": {
            name: package_identity(importlib.import_module(name))
            for name in ("lib_common", "lib_data", "lib_indicators", "lib_strategy", "dev_cli")
        },
        "dataset": {
            "path": str(dataset_path.resolve().relative_to(repo_root)),
            "sha256": file_sha256(dataset_path),
            "audit": audit.to_dict(),
            "product_metadata": dataset["product_metadata"],
        },
        "strategies": strategies,
        "windows": evaluation_windows(),
        "scenarios": scenarios,
        "cost_basis": {
            "source_file": str(cost_path.relative_to(repo_root)),
            "sha256": file_sha256(cost_path),
            "measurement_date": "2026-07-21",
            "historical_order_books_available": False,
            "interpretation": (
                "Frozen template values used as sensitivity assumptions; "
                "original fee/book artifact unavailable"
            ),
        },
        "portfolio": {
            "initial_capital": 100000.0,
            "size_pct": 0.95,
            "annualization_factor": 365.0,
            "sizing": "fractional notional reference; venue rounding/minimums not applied",
            "account_currency": None,
            "accounting_unit": "hypothetical canonical-book price units",
            "valuation_scope": (
                "Returns on the provider aliased BTC-USD price series; "
                "no historical USDC/USD valuation or conversion is modeled"
            ),
            "cash_interest": 0,
            "leverage": 1,
            "terminal_treatment": "mark_to_market_unliquidated",
        },
        "fill_policy": asdict(FillPolicy.reference_v2()),
        "economic_override": {"trade_direction_mode": "long_only"},
        "behavior_mode": "unchanged long_short defaults; signal diagnostics only",
        "selection": (
            "No parameter fitting or automatic strategy ranking; ineligible folds are not pooled"
        ),
        "data_limit": (
            "Provider daily volume does not qualify minute-consolidated production VWAP/OBV"
        ),
        "benchmarks": ["cash", "buy_and_hold"],
    }, bars


def _core(repo_root: Path, record: dict[str, Any], *, long_only: bool) -> PureSignalStrategy:
    path = repo_root / "strategies/indicator" / record["name"]
    if (
        file_sha256(path / "config.json") != record["config_sha256"]
        or file_sha256(path / "core.py") != record["core_sha256"]
    ):
        msg = "Native strategy changed after the evaluation design was frozen"
        raise ValueError(msg)
    parameters = build_strategy_core_parameters(load_json_object(path / "config.json"))
    if long_only:
        parameters["trade_direction_mode"] = "long_only"
    strategy = load_pure_strategy_core(path)(
        strategy_id=record["strategy_id"],
        strategy_type="indicator",
        config=parameters,
    )
    strategy.bootstrap_history([])
    if strategy.warmup_bars_needed <= 0:
        msg = "Native strategy has no initialized warm-up contract"
        raise ValueError(msg)
    return strategy


def _config(design: dict[str, Any], window: dict[str, str], scenario: str) -> BacktestConfig:
    data = design["dataset"]["audit"]
    product = design["dataset"]["product_metadata"]
    return BacktestConfig(
        symbol=data["symbol"],
        consolidation_minutes=1440,
        initial_capital=design["portfolio"]["initial_capital"],
        size_pct=design["portfolio"]["size_pct"],
        annualization_factor=365,
        execution_costs=ExecutionCostModel(**design["scenarios"][scenario]),
        fill_policy=FillPolicy.reference_v2(),
        evaluation_start=datetime.fromisoformat(window["start"]),
        evaluation_end=datetime.fromisoformat(window["end"]),
        price_source=data["source"],
        asset_class="crypto",
        requested_product=product["requested_product_id"],
        canonical_product=product.get("alias") or product["product_id"],
        require_flat_model_boundary=True,
    )


def _require_long_only(report: BacktestReport) -> None:
    if (
        any(row.action == "SHORT" for row in report.raw_signal_ledger)
        or any(trade.side != "long" for trade in report.trades)
        or (report.terminal_position is not None and report.terminal_position.side != "long")
    ):
        msg = "Economic spot report contains a short exposure"
        raise ValueError(msg)


def _report_payload(report: BacktestReport) -> dict[str, Any]:
    trades = []
    for trade in report.trades:
        row = asdict(trade)
        row["entry_ts"], row["exit_ts"] = trade.entry_ts.isoformat(), trade.exit_ts.isoformat()
        trades.append(row)
    return {
        **report.to_dict(),
        "trades": trades,
        "equity_curve": [[ts.isoformat(), equity] for ts, equity in report.equity_curve],
        "metric_scopes": {
            "closed_trades_only": [
                "total_trades",
                "winning_trades",
                "losing_trades",
                "win_rate_pct",
                "profit_factor",
                "payoff_ratio",
                "expectancy",
                "turnover_ratio",
                "average_holding_period_days",
                "median_holding_period_days",
                "holding_periods_bars",
                "break_even_cost_bps",
            ],
            "includes_terminal_position": [
                "equity_curve",
                "final_equity",
                "total_return_pct",
                "turnover_notional",
                "cost_breakdown",
                "gross_pnl",
            ],
        },
    }


def _overlap(
    expected: dict[str, list[BacktestReport]], windows: list[dict[str, str]]
) -> dict[str, Any]:
    pooled: dict[str, list[tuple[datetime, float]]] = {}
    entries: dict[str, set[datetime]] = {}
    executed: dict[str, set[datetime]] = {}
    summaries: dict[str, Any] = {}
    for name, reports in expected.items():
        entries[name] = {
            row.generated_at
            for report in reports
            for row in report.raw_signal_ledger
            if row.action == "LONG"
        }
        executed[name] = {trade.entry_ts for report in reports for trade in report.trades}
        executed[name].update(
            report.terminal_position.entry_ts
            for report in reports
            if report.terminal_position is not None
        )
        blockers = {
            window["name"]: list(report.selection_blockers)
            for window, report in zip(windows, reports, strict=True)
            if report.selection_blockers
        }
        if blockers:
            summaries[name] = {"pooled_metrics": None, "blockers": blockers}
            continue
        folds = []
        for index, (window, report) in enumerate(zip(windows, reports, strict=True)):
            start, end = (
                datetime.fromisoformat(window["start"]),
                datetime.fromisoformat(window["end"]),
            )
            grid = tuple(start + timedelta(days=i) for i in range((end - start).days))
            folds.append(
                FoldOOSSeries(
                    index, grid, tuple(report.equity_curve), report.config.initial_capital
                )
            )
        result = pool_fold_oos_returns(folds, annualization_factor=365)
        summaries[name] = {"pooled_metrics": result.metrics.to_dict(), "blockers": {}}
        pooled[name] = [(row.timestamp, row.daily_return) for row in result.observations]
    pairs = []
    for left, right in combinations(sorted(expected), 2):
        values: dict[str, Any] = {
            "left": left,
            "right": right,
            "expected_oos_return_correlation": aligned_correlation(pooled[left], pooled[right])
            if left in pooled and right in pooled
            else None,
            "return_correlation_status": "eligible_exact_grid"
            if left in pooled and right in pooled
            else "unavailable_ineligible_folds",
        }
        for label, mapping in (("raw_long_entry", entries), ("executed_long_entry", executed)):
            union = mapping[left] | mapping[right]
            values[label + "_jaccard"] = (
                len(mapping[left] & mapping[right]) / len(union) if union else None
            )
        pairs.append(values)
    return {"expected_pooled_oos": summaries, "pairs": pairs}


def evaluate(design: dict[str, Any], bars: list[Bar], repo_root: Path) -> dict[str, Any]:
    engine = BacktestEngine()
    records, diagnostics, benchmarks = [], [], []
    expected: dict[str, list[BacktestReport]] = {}
    for candidate in design["strategies"]:
        name = candidate["name"]
        expected[name] = []
        for window in design["windows"]:
            gross_config = _config(design, window, "gross")
            report = engine.run_consolidated(
                _core(repo_root, candidate, long_only=True), bars, gross_config
            )
            _require_long_only(report)
            assert gross_config.evaluation_start is not None
            warmup = sum(
                bar.timestamp + timedelta(days=1) < gross_config.evaluation_start for bar in bars
            )
            if report.bootstrap_bars != warmup:
                msg = "Historical warm-up did not cover exactly the prior completed bars"
                raise ValueError(msg)
            for scenario in design["scenarios"]:
                current = (
                    report
                    if scenario == "gross"
                    else engine.replay_consolidated_signal_evidence(
                        bars,
                        report.raw_signal_ledger,
                        _config(design, window, scenario),
                        source_bootstrap_bars=report.bootstrap_bars,
                        source_flat_model_boundary_applied=report.flat_model_boundary_applied,
                    )
                )
                _require_long_only(current)
                if current.raw_signal_ledger != report.raw_signal_ledger:
                    msg = "Execution costs changed the frozen strategy decisions"
                    raise ValueError(msg)
                records.append(
                    {
                        "strategy": name,
                        "window": window["name"],
                        "scenario": scenario,
                        "report": _report_payload(current),
                    }
                )
                if scenario == "expected" and window["role"] == "retrospective_out_of_sample":
                    expected[name].append(current)
            behavior = engine.run_consolidated(
                _core(repo_root, candidate, long_only=False), bars, gross_config
            )
            # Re-run a prefix to prove later evaluation inputs cannot change earlier decisions.
            end = datetime.fromisoformat(window["end"])
            cutoff = gross_config.evaluation_start + (end - gross_config.evaluation_start) / 2
            prefix_bars = [bar for bar in bars if bar.timestamp + timedelta(days=1) < cutoff]
            prefix = engine.run_consolidated(
                _core(repo_root, candidate, long_only=False),
                prefix_bars,
                replace(gross_config, evaluation_end=cutoff),
            )
            if prefix.raw_signal_ledger != tuple(
                row for row in behavior.raw_signal_ledger if row.generated_at < cutoff
            ):
                msg = f"Future input changed native decisions: {name}/{window['name']}"
                raise ValueError(msg)
            diagnostics.append(
                {
                    "strategy": name,
                    "window": window["name"],
                    "prefix_causality_passed": True,
                    "raw_signal_ledger": [row.to_dict() for row in behavior.raw_signal_ledger],
                }
            )
        print(f"Evaluated {name}", flush=True)
    for window in design["windows"]:
        for scenario in design["scenarios"]:
            for definition in (BenchmarkDefinition.cash(), BenchmarkDefinition.buy_and_hold()):
                result = run_consolidated_benchmark(
                    definition, bars, _config(design, window, scenario)
                )
                benchmarks.append(
                    {
                        "benchmark": definition.kind.value,
                        "window": window["name"],
                        "scenario": scenario,
                        "report": _report_payload(result.report),
                    }
                )
    oos_windows = [
        window for window in design["windows"] if window["role"] == "retrospective_out_of_sample"
    ]
    return {
        "schema": "vynmatrix.native-migration-results.v1",
        "design_sha256": evidence_sha256(design),
        "economic_projection": design["economic_override"],
        "reports": records,
        "behavior_only": diagnostics,
        "benchmarks": benchmarks,
        "overlap": _overlap(expected, oos_windows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    output = require_descendant(
        root / ".artifacts/research/strategy-validation", root, field="research artifacts"
    )
    design, bars = freeze_design(root, args.dataset)
    design_path, digest = write_content_addressed_manifest(
        output, design, manifest_directory="migration-designs"
    )
    print(f"Frozen design {design_path} sha256={digest}", flush=True)
    results = evaluate(design, bars, root)
    path, digest = write_content_addressed_manifest(
        output, results, manifest_directory="migration-results"
    )
    print(f"Results {path} sha256={digest}", flush=True)


if __name__ == "__main__":
    main()
