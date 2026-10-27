"""Causal editor callbacks followed by replay through the canonical engine."""

from __future__ import annotations

import json
import math
import statistics
from bisect import bisect_right
from dataclasses import asdict
from datetime import datetime
from typing import Any

from dev_cli.validation.backtest.engine import BacktestConfig, BacktestEngine, RawSignalEvidence
from dev_cli.validation.backtest.execution import ExecutionCostModel
from dev_cli.validation.backtest.metrics import daily_returns
from lib_common.asset_classes import SESSION_BASED_ASSET_CLASSES, TRADABLE_ASSET_CLASSES
from lib_data.bars import Bar
from lib_strategy.research.features import WARMUP_BARS, parse_bars, utc_timestamp
from lib_strategy.research.linear_model import validate_model
from lib_strategy.signals.signal import SignalAction

from .sandbox import validate_signal_message

_PRODUCTS = {"crypto": "fully_funded_spot", "equity": "cash_equity", "etf": "cash_etf"}
SOURCE_BYTES = 256 * 1024
MAX_WARMUP = 10000
MIN_RATIO_RETURNS = 2
_ASSUMPTIONS = {
    "initial_capital",
    "currency",
    "size_pct",
    "fee_bps",
    "slippage_bps",
    "annualization_factor",
}


def _eligible(dataset: dict[str, Any], purpose: str) -> None:
    if dataset.get("eligibility", {}).get(purpose) is not True:
        msg = f"Frozen dataset is not eligible for {purpose}"
        raise ValueError(msg)


def model_rows(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    """Features become available at the authoritative close, never the CSV stamp."""
    _eligible(dataset, "training")
    try:
        rows = [
            {
                "ts": row["bar_close"],
                **{key: row[key] for key in ("open", "high", "low", "close", "volume")},
            }
            for row in dataset["bars"]
        ]
    except KeyError as exc:
        msg = "Frozen bars require authoritative close timing and complete OHLCV"
        raise ValueError(msg) from exc
    parse_bars(rows)
    return rows


def compatible_model(
    model: dict[str, Any], dataset: dict[str, Any], evaluation_start: str | None
) -> tuple[dict[str, Any], datetime]:
    frozen = validate_model(model)
    metadata = dataset["metadata"]
    if any(
        frozen["identity"][key] != metadata[key] for key in ("instrument_id", "source", "timeframe")
    ):
        msg = "Model and frozen dataset instrument/source/timeframe identity differ"
        raise ValueError(msg)
    cutoff = utc_timestamp(frozen["training_cutoff"])
    start = cutoff if evaluation_start is None else utc_timestamp(evaluation_start)
    if start < cutoff:
        msg = "Evaluation precedes model training cutoff and embargo"
        raise ValueError(msg)
    rows = model_rows(dataset)
    first = next(
        (index for index, row in enumerate(rows) if utc_timestamp(row["ts"]) >= start), None
    )
    if first is None or first < WARMUP_BARS:
        msg = "Model evaluation requires preceding warmup and observations after cutoff"
        raise ValueError(msg)
    return frozen, start


def execution_bars(dataset: dict[str, Any]) -> list[Bar]:
    _eligible(dataset, "backtest")
    metadata = dataset["metadata"]
    terms = metadata["instrument_terms"]
    asset = terms["asset_class"]
    if (
        asset not in TRADABLE_ASSET_CLASSES
        or asset not in _PRODUCTS
        or terms.get("is_tradable") is not True
    ):
        msg = "Research execution supports long-only spot and cash equity/ETF products"
        raise ValueError(msg)
    if metadata["currency"] != terms.get("settlement_currency"):
        msg = "Research execution requires the instrument's recorded quote currency"
        raise ValueError(msg)
    timing = metadata.get("timing_evidence", {})
    if asset in SESSION_BASED_ASSET_CLASSES and (
        timing.get("kind") != "authoritative_calendar"
        or not timing.get("calendar")
        or not timing.get("sessions")
    ):
        msg = "Cash execution requires authoritative session evidence"
        raise ValueError(msg)
    if asset == "crypto" and terms.get("market_session_policy") != "continuous":
        msg = "Spot execution requires explicit continuous-market authority"
        raise ValueError(msg)
    # Reuse canonical OHLCV/chronology validation before any editor code is sent.
    normalized = model_rows(dataset)
    sessions = [
        (utc_timestamp(item["opens_at"]), utc_timestamp(item["closes_at"]))
        for item in timing.get("sessions", [])
    ]
    starts = [opened for opened, _closed in sessions]
    if any(
        opened >= closed or (index and opened < sessions[index - 1][1])
        for index, (opened, closed) in enumerate(sessions)
    ):
        msg = "Execution session evidence is ambiguous or out of order"
        raise ValueError(msg)
    result = []
    previous_close: datetime | None = None
    for row, numeric in zip(dataset["bars"], normalized, strict=True):
        opened, closed = utc_timestamp(row["bar_open"]), utc_timestamp(row["bar_close"])
        if opened >= closed or (previous_close is not None and opened < previous_close):
            msg = "Frozen execution intervals overlap or are invalid"
            raise ValueError(msg)
        previous_close = closed
        if asset in SESSION_BASED_ASSET_CLASSES:
            index = bisect_right(starts, opened) - 1
            if index < 0 or closed > sessions[index][1]:
                msg = "Execution bar is outside authoritative sessions"
                raise ValueError(msg)
        result.append(
            Bar(
                symbol=terms["canonical"],
                timestamp=closed,
                **{key: float(numeric[key]) for key in ("open", "high", "low", "close", "volume")},
                timeframe=metadata["timeframe"],
                source=metadata["source"],
                metadata={
                    "asset_class": asset,
                    "timestamp_semantics": "period_close",
                    "open_timestamp": opened.isoformat(),
                    "close_timestamp": closed.isoformat(),
                    "requested_product": terms["canonical"],
                    "canonical_product": terms["canonical"],
                },
            )
        )
    return result


def strategy_source(
    wire: Any, revision: dict[str, Any], *, mode: str, model: dict[str, Any] | None = None
) -> None:
    source = revision["source"]
    if not isinstance(source, str) or len(source.encode()) > SOURCE_BYTES:
        msg = "Research strategy source exceeds 256 KiB"
        raise ValueError(msg)
    config = json.loads(json.dumps(revision["config"], allow_nan=False))
    if model is None and "research_model" in config["parameters"]:
        msg = "The research_model parameter requires a selected registered model artifact"
        raise ValueError(msg)
    if model is not None:
        config["parameters"]["research_model"] = model
    wire.send({"kind": "init", "mode": mode, "config": config})
    # UTF-8 plus JSON escaping fits the fixed message bound even for Unicode source.
    for start in range(0, len(source), 4096):
        wire.send({"kind": "source", "text": source[start : start + 4096]})
    wire.send({"kind": "start"})


def _finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_finite(item) for item in value]
    return value


def _result(report: Any, dataset: dict[str, Any], assumptions: dict[str, Any]) -> dict[str, Any]:
    value = _finite(
        {
            **report.to_dict(),
            "timeframe": dataset["metadata"]["timeframe"],
            "currency": assumptions["currency"],
            "trades": [asdict(item) for item in report.trades],
            "equity_curve": [
                {"at": at.isoformat(), "value": amount} for at, amount in report.equity_curve
            ],
            "execution_contract": {
                "product": _PRODUCTS[dataset["metadata"]["instrument_terms"]["asset_class"]],
                "long_only": True,
                "margin": False,
                "currency_conversion": False,
                "cost_basis": "owner_supplied_research_assumptions",
                "assumptions": assumptions,
                "instrument_terms": dataset["metadata"]["instrument_terms"],
                "timing_evidence": dataset["metadata"]["timing_evidence"],
            },
        }
    )
    returns = daily_returns(report.equity_curve)
    if len(returns) < MIN_RATIO_RETURNS or statistics.pstdev(returns) == 0:
        value["sharpe_ratio"] = None
    if len(returns) < MIN_RATIO_RETURNS or not any(item < 0 for item in returns):
        value["sortino_ratio"] = None
    if not report.trades:
        for field in (
            "win_rate_pct",
            "expectancy",
            "payoff_ratio",
            "average_holding_period_days",
            "median_holding_period_days",
        ):
            value[field] = None
    if not any(trade.pnl < 0 for trade in report.trades):
        value["profit_factor"] = value["payoff_ratio"] = None
    if report.metrics.max_drawdown_pct == 0:
        value["calmar_ratio"] = None
    if not returns:
        for field in (
            "annualized_volatility_pct",
            "value_at_risk_95_pct",
            "expected_shortfall_95_pct",
        ):
            value[field] = None
    if report.turnover_notional == 0:
        value["break_even_cost_bps"] = None
    return dict(value)


def run_backtest(
    wire: Any,
    dataset: dict[str, Any],
    revision: dict[str, Any],
    assumptions: dict[str, Any],
    *,
    model: dict[str, Any] | None = None,
    evaluation_start: str | None = None,
) -> dict[str, Any]:
    """Editor decides current signals; only this trusted path computes performance."""
    bars = execution_bars(dataset)
    if (
        set(assumptions) != _ASSUMPTIONS
        or assumptions["currency"] != dataset["metadata"]["currency"]
    ):
        msg = "Explicit capital/currency/sizing/costs/annualization assumptions required"
        raise ValueError(msg)
    costs = ExecutionCostModel(
        scenario_name="owner_research_assumptions",
        commission_bps=float(assumptions["fee_bps"]),
        slippage_bps=float(assumptions["slippage_bps"]),
    )
    terms = dataset["metadata"]["instrument_terms"]
    configured = {
        "symbol": terms["canonical"],
        "consolidation_minutes": 0,
        "initial_capital": float(assumptions["initial_capital"]),
        "size_pct": float(assumptions["size_pct"]),
        "fee_bps": float(assumptions["fee_bps"]),
        "annualization_factor": float(assumptions["annualization_factor"]),
        "execution_costs": costs,
        "price_source": dataset["metadata"]["source"],
        "asset_class": terms["asset_class"],
        "requested_product": terms["canonical"],
        "canonical_product": terms["canonical"],
    }
    BacktestConfig(**configured)  # All execution contracts are checked before source delivery.
    start = utc_timestamp(evaluation_start) if evaluation_start is not None else None
    frozen_model = None
    if model is not None:
        frozen_model, start = compatible_model(model, dataset, evaluation_start)
    strategy_source(wire, revision, mode="strategy", model=frozen_model)
    ready = wire.receive()
    if (
        set(ready) != {"kind", "warmup_bars"}
        or ready["kind"] != "ready"
        or type(ready["warmup_bars"]) is not int
        or not 0 <= ready["warmup_bars"] <= MAX_WARMUP
    ):
        msg = "Strategy warmup response schema invalid"
        raise ValueError(msg)
    warmup = ready["warmup_bars"]
    if start is None:
        if warmup >= len(bars):
            msg = "No evaluation bars remain after strategy warmup"
            raise ValueError(msg)
        start = bars[warmup].timestamp
    bootstrap = sum(bar.timestamp < start for bar in bars)
    if bootstrap < max(warmup, WARMUP_BARS if model is not None else 0) or bootstrap >= len(bars):
        msg = "Evaluation requires the declared preceding warmup and at least one bar"
        raise ValueError(msg)
    ledger: list[RawSignalEvidence] = []
    for sequence, bar in enumerate(bars):
        is_warmup = bar.timestamp < start
        wire.send(
            {
                "kind": "bar",
                "sequence": sequence,
                "warmup": is_warmup,
                "bar": {
                    "symbol": bar.symbol,
                    "ts": bar.timestamp.isoformat(),
                    **{
                        key: getattr(bar, key) for key in ("open", "high", "low", "close", "volume")
                    },
                    "metadata": bar.metadata,
                },
            }
        )
        signals = validate_signal_message(
            wire.receive(),
            sequence=sequence,
            symbol=bar.symbol,
            timestamp=bar.timestamp,
            strategy_id=revision["config"]["strategy_id"],
            asset_class=terms["asset_class"],
            strategy_version=revision["revision_id"],
        )
        if not is_warmup:
            if any(item.action == SignalAction.SHORT for item in signals):
                msg = "Research execution is fully funded and long-only"
                raise ValueError(msg)
            ledger.extend(RawSignalEvidence.from_signal(item) for item in signals)
    wire.send({"kind": "end"})
    wire.finish()
    report = BacktestEngine().replay_consolidated_signal_evidence(
        bars,
        tuple(ledger),
        BacktestConfig(**configured, evaluation_start=start),
        source_bootstrap_bars=bootstrap,
        source_flat_model_boundary_applied=False,
    )
    return _result(report, dataset, assumptions)
