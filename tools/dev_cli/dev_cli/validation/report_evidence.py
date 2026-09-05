"""Validated extraction of immutable standard backtest-report evidence."""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from dev_cli.validation._diagnostic_common import (
    PERCENT_MAX,
    REPORT_EQUITY_POINT_LENGTH,
    REPORT_SHA256_HEX_LENGTH,
    invalid,
    require_finite,
    require_nonempty,
    require_utc,
)
from dev_cli.validation.evidence import parse_utc_datetime


def _evidence_mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        invalid(f"{field_name} must be a mapping")
    return value


def _evidence_sequence(value: object, *, field_name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        invalid(f"{field_name} must be an array")
    return value


def _evidence_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        invalid(f"{field_name} must be numeric")
    result = float(value)
    require_finite(result, field_name=field_name)
    return result


def _evidence_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        invalid(f"{field_name} must be an ISO-8601 string")
    return parse_utc_datetime(value, field=field_name, strict_utc=True)


@dataclass(frozen=True, slots=True)
class PersistedTradeContribution:
    """Minimal immutable trade row extracted from a verified report envelope."""

    index: int
    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    pnl: float
    exit_reason: str = "model_close"

    def __post_init__(self) -> None:
        if self.index < 0:
            invalid("trade index must be non-negative")
        require_nonempty(self.symbol, field_name="trade.symbol")
        require_utc(self.entry_ts, field_name="trade.entry_ts")
        require_utc(self.exit_ts, field_name="trade.exit_ts")
        if self.exit_ts <= self.entry_ts:
            invalid("trade exit_ts must be later than entry_ts")
        require_finite(self.pnl, field_name="trade.pnl")
        require_nonempty(self.exit_reason, field_name="trade.exit_reason")


@dataclass(frozen=True, slots=True)
class StandardReportEvidence:
    """Detached immutable fields from application-verified report evidence."""

    arm_id: str
    asset: str
    fold_id: str
    cost_scenario: str
    trial_id: str
    report_sha256: str
    timeframe: str
    initial_capital: float
    equity_curve: tuple[tuple[datetime, float], ...]
    trades: tuple[PersistedTradeContribution, ...]
    total_return_pct: float
    max_drawdown_pct: float
    sortino_ratio: float
    exposure_pct: float
    gross_pnl: float
    aggregate_execution_cost: float
    turnover_notional: float
    terminal_position_open: bool
    terminal_entry_ts: datetime | None
    terminal_exit_cost: float
    terminal_exit_reference_notional: float
    terminal_exit_policy_id: str | None

    @property
    def adjusted_equity_curve(self) -> tuple[tuple[datetime, float], ...]:
        if not self.terminal_position_open:
            return self.equity_curve
        timestamp, equity = self.equity_curve[-1]
        adjusted = equity - self.terminal_exit_cost
        if not math.isfinite(adjusted) or adjusted <= 0.0:
            invalid(f"terminal exit cost ruins report evidence {self.trial_id}")
        return (*self.equity_curve[:-1], (timestamp, adjusted))

    def source_dict(self) -> dict[str, object]:
        return {
            "arm_id": self.arm_id,
            "asset": self.asset,
            "fold_id": self.fold_id,
            "cost_scenario": self.cost_scenario,
            "trial_id": self.trial_id,
            "report_sha256": self.report_sha256,
            "terminal_entry_ts": (
                self.terminal_entry_ts.isoformat() if self.terminal_entry_ts is not None else None
            ),
            "terminal_exit_cost": self.terminal_exit_cost,
            "terminal_exit_reference_notional": self.terminal_exit_reference_notional,
            "terminal_exit_policy_id": self.terminal_exit_policy_id,
        }


def standard_report_evidence_from_mapping(  # noqa: PLR0912, PLR0915
    evidence: Mapping[str, Any],
    *,
    arm_id: str,
    fold_id: str,
    cost_scenario: str,
    terminal_exit_cost: float = 0.0,
    terminal_exit_policy_id: str | None = None,
) -> StandardReportEvidence:
    """Extract and cross-check one application-verified report envelope."""

    for field_name, value in (
        ("arm_id", arm_id),
        ("fold_id", fold_id),
        ("cost_scenario", cost_scenario),
    ):
        require_nonempty(value, field_name=field_name)
    if evidence.get("report_evidence_schema_id") != "vynmatrix.backtest.report-evidence":
        invalid("report evidence schema id is unsupported")
    if evidence.get("report_evidence_schema_version") != 1:
        invalid("report evidence schema version is unsupported")
    trial_id = evidence.get("trial_id")
    report_sha256 = evidence.get("report_sha256")
    if not isinstance(trial_id, str) or not trial_id.strip():
        invalid("report evidence trial_id must not be blank")
    if (
        not isinstance(report_sha256, str)
        or len(report_sha256) != REPORT_SHA256_HEX_LENGTH
        or report_sha256 != report_sha256.lower()
        or any(character not in "0123456789abcdef" for character in report_sha256)
    ):
        invalid("report evidence report_sha256 must be a lowercase SHA-256 digest")

    report = _evidence_mapping(evidence.get("report"), field_name="report")
    context = _evidence_mapping(evidence.get("meta_context"), field_name="meta_context")
    if context.get("arm_id") != arm_id:
        invalid("report evidence arm_id differs from the registered identity")
    if context.get("runner_kind") != "production_core_direct":
        invalid("report evidence must come from production_core_direct")
    asset = evidence.get("symbol")
    timeframe = evidence.get("timeframe")
    if not isinstance(asset, str) or not asset.strip():
        invalid("report evidence symbol must not be blank")
    if not isinstance(timeframe, str) or not timeframe.strip():
        invalid("report evidence timeframe must not be blank")
    if report.get("symbol") != asset or report.get("timeframe") != timeframe:
        invalid("report summary identity differs from the evidence envelope")
    if report.get("cost_scenario") != cost_scenario:
        invalid("report cost scenario differs from the registered identity")
    if bool(report.get("account_ruined")):
        invalid("ruined report evidence is not eligible for diagnostics")
    raw_blockers = _evidence_sequence(
        report.get("selection_blockers", []), field_name="report.selection_blockers"
    )
    if any(not isinstance(item, str) or not item.strip() for item in raw_blockers):
        invalid("report selection blockers must be non-empty strings")
    blockers = tuple(raw_blockers)
    if len(set(blockers)) != len(blockers):
        invalid("report selection blockers must be unique")
    unsupported_blockers = set(blockers) - {"unresolved_terminal_position"}
    if unsupported_blockers:
        invalid("report evidence has unresolved non-terminal selection blockers")

    initial_capital = _evidence_float(
        report.get("initial_capital"), field_name="report.initial_capital"
    )
    if initial_capital <= 0.0:
        invalid("report initial capital must be positive")
    raw_curve = _evidence_sequence(evidence.get("equity_curve"), field_name="equity_curve")
    if not raw_curve:
        invalid("report equity curve must not be empty")
    curve: list[tuple[datetime, float]] = []
    for index, raw_point in enumerate(raw_curve):
        point = _evidence_sequence(raw_point, field_name=f"equity_curve[{index}]")
        if len(point) != REPORT_EQUITY_POINT_LENGTH:
            invalid(f"equity_curve[{index}] must contain timestamp and equity")
        timestamp = _evidence_datetime(point[0], field_name=f"equity_curve[{index}].timestamp")
        equity = _evidence_float(point[1], field_name=f"equity_curve[{index}].equity")
        if equity <= 0.0:
            invalid("report equity values must be positive")
        curve.append((timestamp, equity))
    if any(current[0] <= previous[0] for previous, current in itertools.pairwise(curve)):
        invalid("report equity timestamps must be strictly increasing")
    if len({timestamp.date() for timestamp, _equity in curve}) != len(curve):
        invalid("report evidence must contain exactly one equity observation per UTC day")
    final_equity = _evidence_float(report.get("final_equity"), field_name="report.final_equity")
    if not math.isclose(curve[-1][1], final_equity, rel_tol=1e-12, abs_tol=1e-9):
        invalid("report final equity differs from the complete equity curve")

    raw_trades = _evidence_sequence(evidence.get("trades"), field_name="trades")
    trades: list[PersistedTradeContribution] = []
    for index, raw_trade in enumerate(raw_trades):
        trade = _evidence_mapping(raw_trade, field_name=f"trades[{index}]")
        symbol = trade.get("symbol")
        if symbol != asset:
            invalid(f"trades[{index}] symbol differs from report asset")
        exit_reason = trade.get("exit_reason")
        if not isinstance(exit_reason, str) or not exit_reason.strip():
            invalid(f"trades[{index}] exit_reason must be a non-empty string")
        trades.append(
            PersistedTradeContribution(
                index=index,
                symbol=asset,
                entry_ts=_evidence_datetime(
                    trade.get("entry_ts"), field_name=f"trades[{index}].entry_ts"
                ),
                exit_ts=_evidence_datetime(
                    trade.get("exit_ts"), field_name=f"trades[{index}].exit_ts"
                ),
                pnl=_evidence_float(trade.get("pnl"), field_name=f"trades[{index}].pnl"),
                exit_reason=exit_reason,
            )
        )
    total_trades = report.get("total_trades")
    if isinstance(total_trades, bool) or not isinstance(total_trades, int):
        invalid("report total_trades must be an integer")
    if total_trades != len(trades):
        invalid("report total_trades differs from the complete trade ledger")

    raw_terminal = report.get("terminal_position")
    terminal_open = raw_terminal is not None
    terminal_blocked = "unresolved_terminal_position" in blockers
    if terminal_open != terminal_blocked:
        invalid("terminal position and unresolved-terminal selection blocker disagree")
    normalized_exit_cost = _evidence_float(terminal_exit_cost, field_name="terminal_exit_cost")
    if normalized_exit_cost < 0.0:
        invalid("terminal_exit_cost must be non-negative")
    if terminal_exit_policy_id is not None and (
        not isinstance(terminal_exit_policy_id, str)
        or not terminal_exit_policy_id.strip()
        or terminal_exit_policy_id != terminal_exit_policy_id.strip()
    ):
        invalid("terminal_exit_policy_id must be a normalized non-empty string or None")
    if terminal_open and fold_id != "full-panel" and normalized_exit_cost <= 0.0:
        invalid("OOS terminal exposure requires a registered positive exit cost")
    if (
        terminal_open
        and fold_id != "full-panel"
        and (terminal_exit_policy_id is None or not terminal_exit_policy_id.strip())
    ):
        invalid("OOS terminal exposure requires a registered exit policy ID")
    if not terminal_open and normalized_exit_cost != 0.0:
        invalid("terminal_exit_cost must be zero when no terminal position exists")
    if not terminal_open and terminal_exit_policy_id is not None:
        invalid("terminal_exit_policy_id must be None when no terminal position exists")
    if (
        terminal_open
        and fold_id == "full-panel"
        and (normalized_exit_cost != 0.0 or terminal_exit_policy_id is not None)
    ):
        invalid("full-panel terminal evidence must remain marked without an exit adjustment")
    terminal_entry_ts: datetime | None = None
    terminal_reference_notional = 0.0
    if terminal_open:
        terminal = _evidence_mapping(raw_terminal, field_name="report.terminal_position")
        if terminal.get("symbol") != asset:
            invalid("terminal position symbol differs from report asset")
        if terminal.get("side") not in {"long", "short"}:
            invalid("terminal position side must be long or short")
        terminal_quantity = _evidence_float(
            terminal.get("quantity"), field_name="report.terminal_position.quantity"
        )
        terminal_mark = _evidence_float(
            terminal.get("mark_price"), field_name="report.terminal_position.mark_price"
        )
        terminal_entry_ts = _evidence_datetime(
            terminal.get("entry_ts"), field_name="report.terminal_position.entry_ts"
        )
        if terminal_quantity <= 0.0 or terminal_mark <= 0.0:
            invalid("terminal quantity and mark price must be positive")
        final_economic_timestamp = curve[-1][0] - timedelta(days=1)
        if terminal_entry_ts > final_economic_timestamp:
            invalid("terminal entry timestamp is after the final economic period")
        terminal_reference_notional = terminal_quantity * terminal_mark

    cost_breakdown = _evidence_mapping(
        report.get("cost_breakdown"), field_name="report.cost_breakdown"
    )
    cost_components = tuple(
        _evidence_float(cost_breakdown.get(name), field_name=f"report.cost_breakdown.{name}")
        for name in ("commission", "half_spread", "slippage", "impact")
    )
    aggregate_execution_cost = _evidence_float(
        cost_breakdown.get("total"), field_name="report.cost_breakdown.total"
    )
    if any(value < 0.0 for value in cost_components) or aggregate_execution_cost < 0.0:
        invalid("report execution costs must be non-negative")
    if not math.isclose(
        aggregate_execution_cost,
        math.fsum(cost_components),
        rel_tol=1e-12,
        abs_tol=1e-9,
    ):
        invalid("report aggregate execution cost differs from its complete components")

    exposure_pct = _evidence_float(report.get("exposure_pct"), field_name="report.exposure_pct")
    if not 0.0 <= exposure_pct <= PERCENT_MAX:
        invalid("report exposure_pct must be in [0, 100]")
    turnover_notional = _evidence_float(
        report.get("turnover_notional"), field_name="report.turnover_notional"
    )
    if turnover_notional < 0.0:
        invalid("report turnover_notional must be non-negative")

    return StandardReportEvidence(
        arm_id=arm_id,
        asset=asset,
        fold_id=fold_id,
        cost_scenario=cost_scenario,
        trial_id=trial_id,
        report_sha256=report_sha256,
        timeframe=timeframe,
        initial_capital=initial_capital,
        equity_curve=tuple(curve),
        trades=tuple(trades),
        total_return_pct=_evidence_float(
            report.get("total_return_pct"), field_name="report.total_return_pct"
        ),
        max_drawdown_pct=_evidence_float(
            report.get("max_drawdown_pct"), field_name="report.max_drawdown_pct"
        ),
        sortino_ratio=_evidence_float(
            report.get("sortino_ratio"), field_name="report.sortino_ratio"
        ),
        exposure_pct=exposure_pct,
        gross_pnl=_evidence_float(report.get("gross_pnl"), field_name="report.gross_pnl"),
        aggregate_execution_cost=aggregate_execution_cost,
        turnover_notional=turnover_notional,
        terminal_position_open=terminal_open,
        terminal_entry_ts=terminal_entry_ts,
        terminal_exit_cost=normalized_exit_cost,
        terminal_exit_reference_notional=terminal_reference_notional,
        terminal_exit_policy_id=terminal_exit_policy_id,
    )
