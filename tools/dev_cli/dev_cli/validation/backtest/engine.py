"""Deterministic validation engine using production strategy and bar contracts."""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, ClassVar, NoReturn

from dev_cli.validation.evidence import canonical_json_bytes
from dev_cli.validation.position_sizing import (
    SIZING_EVIDENCE_SCHEMA,
    CoinbaseSizingRules,
    validate_sizing_boundary,
)
from lib_data.bars import Bar, ohlcv_invariant_error
from lib_data.consolidation import BarConsolidator
from lib_strategy.position_sizing import PositionSizingPolicy
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.signals.utils import ensure_utc

from .execution import (
    CostBreakdown,
    ExecutionCostModel,
    FillPolicy,
    execution_bar_times,
    normalize_execution_bar,
)
from .metrics import BacktestMetrics, compute_metrics
from .simulator import (
    BacktestSizingDecision,
    FillSimulator,
    ModelExecutionDivergence,
    TerminalPosition,
    Trade,
)


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)


def _invalid_type(message: str) -> NoReturn:
    raise TypeError(message)


def _aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    consolidation_minutes: int = 15
    initial_capital: float = 100_000.0
    size_pct: float = 0.95
    fee_bps: float = 10.0
    annualization_factor: float = 365.0
    execution_costs: ExecutionCostModel | None = None
    fill_policy: FillPolicy = field(default_factory=FillPolicy.reference_v2)
    min_bar_coverage: float = 0.0
    evaluation_start: datetime | None = None
    evaluation_end: datetime | None = None
    price_source: str | None = None
    asset_class: str | None = None
    requested_product: str | None = None
    canonical_product: str | None = None
    require_flat_model_boundary: bool = False
    sizing_policy: PositionSizingPolicy | None = None
    coinbase_sizing_rules: CoinbaseSizingRules | None = None

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            _invalid("symbol must not be blank")
        if isinstance(self.consolidation_minutes, bool) or not isinstance(
            self.consolidation_minutes, int
        ):
            _invalid_type("consolidation_minutes must be an integer")
        if self.consolidation_minutes < 0:
            _invalid("consolidation_minutes must be non-negative")
        if not math.isfinite(self.initial_capital) or self.initial_capital <= 0.0:
            _invalid("initial_capital must be finite and positive")
        if not math.isfinite(self.size_pct) or not 0.0 < self.size_pct <= 1.0:
            _invalid("size_pct must be finite and in (0, 1]")
        validate_sizing_boundary(self.sizing_policy, self.coinbase_sizing_rules)
        if not math.isfinite(self.fee_bps) or self.fee_bps < 0.0:
            _invalid("fee_bps must be finite and non-negative")
        if not math.isfinite(self.annualization_factor) or self.annualization_factor <= 0.0:
            _invalid("annualization_factor must be finite and positive")
        if not math.isfinite(self.min_bar_coverage) or not (0.0 <= self.min_bar_coverage <= 1.0):
            _invalid("min_bar_coverage must be finite and in [0, 1]")
        if self.evaluation_start is not None:
            _aware(self.evaluation_start, field_name="evaluation_start")
        if self.evaluation_end is not None:
            _aware(self.evaluation_end, field_name="evaluation_end")
        if (
            self.evaluation_start is not None
            and self.evaluation_end is not None
            and self.evaluation_start >= self.evaluation_end
        ):
            _invalid("evaluation_start must be earlier than evaluation_end")
        for name in ("price_source", "asset_class", "requested_product", "canonical_product"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                _invalid(f"{name} must not be blank when supplied")
        if not isinstance(self.require_flat_model_boundary, bool):
            _invalid_type("require_flat_model_boundary must be a bool")


@dataclass(frozen=True)
class RawSignalEvidence:
    """Immutable, deterministic evidence for one production-core emission.

    The ledger deliberately excludes runtime-only fields including the random
    ``Signal.signal_id``, ``source``, ``run_id``, and extensible metadata/features
    dictionaries.  Reconciliation uses the canonical external idempotency key,
    while ``canonical_json`` provides a stable, JSON-safe representation of only
    the execution/scoring fields required by validation campaigns.
    """

    external_signal_id: str
    strategy_id: str
    strategy_type: str
    symbol: str
    action: str
    generated_at: datetime
    valid_until: datetime | None
    entry_price: float | None
    stop_loss: float | None
    take_profit: float | None
    confidence: float
    expected_return: float | None
    predicted_risk: float | None
    horizon: str | None
    horizon_days: float | None
    canonical_json: str

    _CANONICAL_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "action",
            "confidence",
            "entry_price",
            "expected_return",
            "external_signal_id",
            "generated_at",
            "horizon",
            "horizon_days",
            "predicted_risk",
            "stop_loss",
            "strategy_id",
            "strategy_type",
            "symbol",
            "take_profit",
            "valid_until",
        }
    )
    _SERIALIZED_FIELDS: ClassVar[frozenset[str]] = _CANONICAL_FIELDS | {"canonical_json"}

    @classmethod
    def from_signal(cls, signal: Signal) -> RawSignalEvidence:
        """Snapshot an emitted signal without retaining its mutable payloads."""

        external_signal_id = signal.external_signal_id
        if external_signal_id is None or not external_signal_id.strip():
            msg = "backtest signal is missing its canonical external_signal_id"
            raise ValueError(msg)
        generated_at = ensure_utc(signal.timestamp)
        valid_until = ensure_utc(signal.expires_at) if signal.expires_at is not None else None
        confidence = _evidence_float(signal.confidence, field_name="confidence")
        entry_price = _optional_evidence_float(signal.entry_price, field_name="entry_price")
        stop_loss = _optional_evidence_float(signal.stop_loss, field_name="stop_loss")
        take_profit = _optional_evidence_float(signal.take_profit, field_name="take_profit")
        expected_return = _optional_evidence_float(
            signal.expected_return,
            field_name="expected_return",
        )
        predicted_risk = _optional_evidence_float(
            signal.predicted_risk,
            field_name="predicted_risk",
        )
        horizon_days = _optional_evidence_float(signal.horizon_days, field_name="horizon_days")
        payload: dict[str, object] = {
            "action": signal.action.value,
            "confidence": confidence,
            "entry_price": entry_price,
            "expected_return": expected_return,
            "external_signal_id": external_signal_id,
            "generated_at": generated_at.isoformat(),
            "horizon": signal.horizon,
            "horizon_days": horizon_days,
            "predicted_risk": predicted_risk,
            "stop_loss": stop_loss,
            "strategy_id": signal.strategy_id,
            "strategy_type": signal.strategy_type,
            "symbol": signal.symbol,
            "take_profit": take_profit,
            "valid_until": valid_until.isoformat() if valid_until is not None else None,
        }
        canonical_json = _canonical_evidence_json(payload)
        return cls(
            external_signal_id=external_signal_id,
            strategy_id=signal.strategy_id,
            strategy_type=signal.strategy_type,
            symbol=signal.symbol,
            action=signal.action.value,
            generated_at=generated_at,
            valid_until=valid_until,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=confidence,
            expected_return=expected_return,
            predicted_risk=predicted_risk,
            horizon=signal.horizon,
            horizon_days=horizon_days,
            canonical_json=canonical_json,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RawSignalEvidence:
        """Restore strictly canonical evidence from a persisted ledger row.

        Persistence is an evidence boundary, so this decoder does not accept
        schema evolution by omission, unknown fields, permissive timestamps,
        or merely equivalent JSON.  The outer row, embedded canonical JSON,
        reconstructed signal, and a fresh evidence snapshot must all agree.
        """

        if not isinstance(data, Mapping):
            _invalid_type("raw signal evidence must be a mapping")
        actual_fields = frozenset(data)
        if not all(isinstance(field, str) for field in actual_fields):
            _invalid("raw signal evidence field names must be strings")
        if actual_fields != cls._SERIALIZED_FIELDS:
            missing = sorted(cls._SERIALIZED_FIELDS - actual_fields)
            unexpected = sorted(actual_fields - cls._SERIALIZED_FIELDS)
            _invalid(
                "raw signal evidence fields do not match the canonical schema: "
                f"missing={missing}, unexpected={unexpected}"
            )

        canonical_json = data["canonical_json"]
        if not isinstance(canonical_json, str):
            _invalid_type("raw signal evidence canonical_json must be a string")
        try:
            canonical_payload = json.loads(canonical_json)
        except (TypeError, json.JSONDecodeError) as exc:
            msg = "raw signal evidence canonical_json is not valid JSON"
            raise ValueError(msg) from exc
        if not isinstance(canonical_payload, dict):
            _invalid("raw signal evidence canonical_json must encode an object")
        canonical_fields = frozenset(canonical_payload)
        if canonical_fields != cls._CANONICAL_FIELDS:
            missing = sorted(cls._CANONICAL_FIELDS - canonical_fields)
            unexpected = sorted(canonical_fields - cls._CANONICAL_FIELDS)
            _invalid(
                "raw signal evidence canonical_json fields do not match the canonical schema: "
                f"missing={missing}, unexpected={unexpected}"
            )

        encoded_payload = _canonical_evidence_json(canonical_payload)
        if encoded_payload != canonical_json:
            _invalid("raw signal evidence canonical_json is not canonical")
        outer_payload = {field: data[field] for field in cls._CANONICAL_FIELDS}
        if _canonical_evidence_json(outer_payload) != canonical_json:
            _invalid("raw signal evidence outer fields do not match canonical_json")

        evidence = cls(
            external_signal_id=_evidence_string(
                canonical_payload["external_signal_id"],
                field_name="external_signal_id",
            ),
            strategy_id=_evidence_string(
                canonical_payload["strategy_id"],
                field_name="strategy_id",
            ),
            strategy_type=_evidence_string(
                canonical_payload["strategy_type"],
                field_name="strategy_type",
            ),
            symbol=_evidence_string(canonical_payload["symbol"], field_name="symbol"),
            action=_evidence_string(canonical_payload["action"], field_name="action"),
            generated_at=_evidence_datetime(
                canonical_payload["generated_at"],
                field_name="generated_at",
            ),
            valid_until=_optional_evidence_datetime(
                canonical_payload["valid_until"],
                field_name="valid_until",
            ),
            entry_price=_persisted_optional_evidence_float(
                canonical_payload["entry_price"],
                field_name="entry_price",
            ),
            stop_loss=_persisted_optional_evidence_float(
                canonical_payload["stop_loss"],
                field_name="stop_loss",
            ),
            take_profit=_persisted_optional_evidence_float(
                canonical_payload["take_profit"],
                field_name="take_profit",
            ),
            confidence=_persisted_evidence_float(
                canonical_payload["confidence"],
                field_name="confidence",
            ),
            expected_return=_persisted_optional_evidence_float(
                canonical_payload["expected_return"],
                field_name="expected_return",
            ),
            predicted_risk=_persisted_optional_evidence_float(
                canonical_payload["predicted_risk"],
                field_name="predicted_risk",
            ),
            horizon=_optional_evidence_string(
                canonical_payload["horizon"],
                field_name="horizon",
            ),
            horizon_days=_persisted_optional_evidence_float(
                canonical_payload["horizon_days"],
                field_name="horizon_days",
            ),
            canonical_json=canonical_json,
        )
        evidence.to_signal()
        return evidence

    def to_dict(self) -> dict[str, object]:
        """Return a fresh JSON-safe payload plus its canonical representation."""

        payload = json.loads(self.canonical_json)
        if not isinstance(payload, dict):  # pragma: no cover - construction invariant
            msg = "raw signal evidence payload must be a JSON object"
            raise TypeError(msg)
        payload["canonical_json"] = self.canonical_json
        return payload

    def to_signal(self) -> Signal:
        """Reconstruct the canonical signal without random or wall-clock fields.

        The evidence ledger intentionally omits runtime-only signal fields.  This
        reconstruction is therefore limited to the fields covered by
        ``canonical_json`` and gives the random ``signal_id`` the canonical
        external identity.  ``source="backtest"`` labels this reconstruction's
        context; it does not recover or assert the original runtime source.  A
        round trip through :meth:`from_signal` is required so a mutated dataclass
        field or canonical payload fails closed before a validation replay can
        use it.
        """

        if not isinstance(self.external_signal_id, str) or not self.external_signal_id.strip():
            _invalid("raw signal evidence is missing its canonical external_signal_id")
        _aware(self.generated_at, field_name="raw signal evidence generated_at")
        if self.valid_until is not None:
            _aware(self.valid_until, field_name="raw signal evidence valid_until")
        try:
            action = SignalAction(self.action)
        except ValueError as exc:
            msg = f"raw signal evidence has an invalid action: {self.action!r}"
            raise ValueError(msg) from exc
        signal = Signal(
            signal_id=self.external_signal_id,
            strategy_id=self.strategy_id,
            strategy_type=self.strategy_type,
            symbol=self.symbol,
            action=action,
            confidence=self.confidence,
            timestamp=self.generated_at,
            horizon=self.horizon,
            expected_return=self.expected_return,
            predicted_risk=self.predicted_risk,
            horizon_days=self.horizon_days,
            entry_price=self.entry_price,
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            source="backtest",
            external_signal_id=self.external_signal_id,
            expires_at=self.valid_until,
        )
        rebuilt = type(self).from_signal(signal)
        if rebuilt.canonical_json != self.canonical_json:
            msg = (
                "raw signal evidence canonical mismatch for "
                f"external_signal_id={self.external_signal_id!r}"
            )
            raise ValueError(msg)
        return signal


def _evidence_float(value: float, *, field_name: str) -> float:
    normalized = float(value)
    if not math.isfinite(normalized):
        _invalid(f"signal {field_name} must be finite for evidence serialization")
    return normalized


def _optional_evidence_float(value: float | None, *, field_name: str) -> float | None:
    return None if value is None else _evidence_float(value, field_name=field_name)


def _canonical_evidence_json(payload: Mapping[str, object]) -> str:
    try:
        return canonical_json_bytes(payload).decode("utf-8")
    except (TypeError, ValueError) as exc:
        msg = "raw signal evidence contains a non-JSON-safe value"
        raise ValueError(msg) from exc


def _evidence_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid(f"raw signal evidence {field_name} must be a non-blank string")
    return value


def _optional_evidence_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _invalid_type(f"raw signal evidence {field_name} must be a string or null")
    return value


def _evidence_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        _invalid_type(f"raw signal evidence {field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"raw signal evidence {field_name} must be a valid ISO-8601 timestamp"
        raise ValueError(msg) from exc
    _aware(parsed, field_name=f"raw signal evidence {field_name}")
    return parsed


def _optional_evidence_datetime(value: object, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _evidence_datetime(value, field_name=field_name)


def _persisted_evidence_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid_type(f"raw signal evidence {field_name} must be numeric")
    return _evidence_float(value, field_name=field_name)


def _persisted_optional_evidence_float(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    return _persisted_evidence_float(value, field_name=field_name)


@dataclass(frozen=True)
class BacktestReport:
    """Full deterministic result of one backtest run."""

    config: BacktestConfig
    metrics: BacktestMetrics
    trades: list[Trade]
    equity_curve: list[tuple[datetime, float]]
    signals_emitted: int
    consolidated_bars: int
    unfilled_signals: int
    unmatched_signals: int = 0
    ignored_signals: int = 0
    cancelled_signals: int = 0
    rejected_entries: int = 0
    ambiguous_bars: int = 0
    protective_stop_exits: int = 0
    model_execution_divergences: tuple[ModelExecutionDivergence, ...] = ()
    buying_power_rejections: int = 0
    account_ruined: bool = False
    terminal_position: TerminalPosition | None = None
    gross_pnl: float = 0.0
    closed_gross_pnl: float = 0.0
    terminal_gross_pnl: float = 0.0
    cost_breakdown: CostBreakdown = field(default_factory=CostBreakdown)
    closed_cost_breakdown: CostBreakdown = field(default_factory=CostBreakdown)
    turnover_notional: float = 0.0
    closed_turnover_notional: float = 0.0
    holding_periods_bars: tuple[int, ...] = ()
    exit_reason_counts: dict[str, int] = field(default_factory=dict)
    execution_policy_id: str = "next_open_bracket_v2"
    cost_scenario: str = "configured_flat_fee"
    bootstrap_bars: int = 0
    flat_model_boundary_applied: bool = False
    raw_signal_ledger: tuple[RawSignalEvidence, ...] = ()
    sizing_policy: PositionSizingPolicy | None = None
    sizing_decision_ledger: tuple[BacktestSizingDecision, ...] = ()

    @property
    def selection_blockers(self) -> tuple[str, ...]:
        """Conditions that make this report ineligible for parameter selection."""

        blockers: list[str] = []
        if self.terminal_position is not None:
            blockers.append("unresolved_terminal_position")
        if any(
            item.end_ts is None and item.cause != "position_lineage_mismatch"
            for item in self.model_execution_divergences
        ):
            blockers.append("unresolved_model_execution_divergence")
        if any(
            item.cause == "position_lineage_mismatch" for item in self.model_execution_divergences
        ):
            blockers.append("model_execution_position_lineage_divergence")
        if self.unmatched_signals:
            blockers.append("unmatched_signals")
        if self.account_ruined:
            blockers.append("account_ruined")
        return tuple(blockers)

    @property
    def selection_eligible(self) -> bool:
        return not self.selection_blockers

    def require_selection_eligible(self) -> None:
        if self.selection_blockers:
            msg = "report is ineligible for selection: " + ", ".join(self.selection_blockers)
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        m = self.metrics
        return {
            "symbol": self.config.symbol,
            "timeframe": f"{self.config.consolidation_minutes}m",
            "initial_capital": m.initial_capital,
            "final_equity": m.final_equity,
            "peak_equity": m.peak_equity,
            "total_return_pct": m.total_return_pct,
            "cagr_pct": m.cagr_pct,
            "sharpe_ratio": m.sharpe_ratio,
            "sortino_ratio": m.sortino_ratio,
            "max_drawdown_pct": m.max_drawdown_pct,
            "win_rate_pct": m.win_rate_pct,
            "profit_factor": m.profit_factor,
            "annualization_factor": m.annualization_factor,
            "annualized_volatility_pct": m.annualized_volatility_pct,
            "calmar_ratio": m.calmar_ratio,
            "recovery_duration_days": m.recovery_duration_days,
            "expectancy": m.expectancy,
            "payoff_ratio": m.payoff_ratio,
            "exposure_pct": m.exposure_pct,
            "turnover_ratio": m.turnover_ratio,
            "average_holding_period_days": m.average_holding_period_days,
            "median_holding_period_days": m.median_holding_period_days,
            "value_at_risk_95_pct": m.value_at_risk_95_pct,
            "expected_shortfall_95_pct": m.expected_shortfall_95_pct,
            "break_even_cost_bps": m.break_even_cost_bps,
            "total_trades": m.total_trades,
            "winning_trades": m.winning_trades,
            "losing_trades": m.losing_trades,
            "signals_emitted": self.signals_emitted,
            "raw_signal_ledger": [item.to_dict() for item in self.raw_signal_ledger],
            "sizing_evidence_schema": SIZING_EVIDENCE_SCHEMA,
            "sizing_policy": (
                self.sizing_policy.to_dict() if self.sizing_policy is not None else None
            ),
            "coinbase_sizing_rules": (
                self.config.coinbase_sizing_rules.to_dict()
                if self.config.coinbase_sizing_rules is not None
                else None
            ),
            "sizing_decision_ledger": [item.to_dict() for item in self.sizing_decision_ledger],
            "consolidated_bars": self.consolidated_bars,
            "bootstrap_bars": self.bootstrap_bars,
            "flat_model_boundary_applied": self.flat_model_boundary_applied,
            "unfilled_signals": self.unfilled_signals,
            "unmatched_signals": self.unmatched_signals,
            "ignored_signals": self.ignored_signals,
            "cancelled_signals": self.cancelled_signals,
            "rejected_entries": self.rejected_entries,
            "ambiguous_bars": self.ambiguous_bars,
            "protective_stop_exits": self.protective_stop_exits,
            "model_execution_divergences": [
                divergence.to_dict() for divergence in self.model_execution_divergences
            ],
            "buying_power_rejections": self.buying_power_rejections,
            "account_ruined": self.account_ruined,
            "selection_eligible": self.selection_eligible,
            "selection_blockers": list(self.selection_blockers),
            "terminal_position": (
                self.terminal_position.to_dict() if self.terminal_position else None
            ),
            "gross_pnl": self.gross_pnl,
            "closed_gross_pnl": self.closed_gross_pnl,
            "terminal_gross_pnl": self.terminal_gross_pnl,
            "cost_breakdown": self.cost_breakdown.to_dict(),
            "closed_cost_breakdown": self.closed_cost_breakdown.to_dict(),
            "turnover_notional": self.turnover_notional,
            "closed_turnover_notional": self.closed_turnover_notional,
            "holding_periods_bars": list(self.holding_periods_bars),
            "exit_reason_counts": dict(self.exit_reason_counts),
            "execution_policy_id": self.execution_policy_id,
            "cost_scenario": self.cost_scenario,
        }


class BacktestEngine:
    """Run a ``PureSignalStrategy`` over validated fixed bars."""

    validation_capabilities: ClassVar[Mapping[str, bool]] = MappingProxyType(
        {
            "flat_model_boundary": True,
            "purged_event_overlap": True,
            "embargoed_selection": True,
        }
    )

    def consolidate(
        self,
        raw_bars: list[Bar],
        *,
        period_minutes: int,
        min_coverage: float = 0.0,
    ) -> list[Bar]:
        """Consolidate via production code and suppress under-covered periods."""
        self._validate_bars(raw_bars, min_coverage=0.0)
        if period_minutes < 0:
            _invalid("period_minutes must be non-negative")
        if not math.isfinite(min_coverage) or not 0.0 <= min_coverage <= 1.0:
            _invalid("min_coverage must be finite and in [0, 1]")
        if period_minutes == 0:
            self._validate_bars(raw_bars, min_coverage=min_coverage)
            return list(raw_bars)
        provenance: dict[str, Any] = {}
        for key in ("asset_class", "requested_product", "canonical_product"):
            value = next(
                (bar.metadata.get(key) for bar in raw_bars if bar.metadata.get(key) is not None),
                None,
            )
            if value is not None:
                provenance[key] = value
        consolidator = BarConsolidator(
            period_minutes=period_minutes,
            min_coverage=min_coverage,
        )
        out: list[Bar] = []
        for bar in raw_bars:
            emitted = consolidator.update(bar)
            if emitted is not None:
                emitted.metadata.update(provenance)
                emitted.metadata["timestamp_semantics"] = "period_close"
                times = execution_bar_times(emitted)
                emitted.metadata["open_timestamp"] = times.open_ts.isoformat()
                emitted.metadata["close_timestamp"] = times.close_ts.isoformat()
                out.append(emitted)
        if not out:
            _invalid("consolidation produced no complete bars")
        self._validate_bars(out, min_coverage=min_coverage)
        return out

    def run(
        self,
        strategy: PureSignalStrategy,
        raw_bars: list[Bar],
        config: BacktestConfig,
    ) -> BacktestReport:
        """Validate and consolidate raw bars, then execute the production core."""
        self._validate_bars(raw_bars, config=config, min_coverage=0.0)
        consolidated = self.consolidate(
            raw_bars,
            period_minutes=config.consolidation_minutes,
            min_coverage=config.min_bar_coverage,
        )
        return self.run_consolidated(strategy, consolidated, config)

    def run_consolidated(
        self,
        strategy: PureSignalStrategy,
        consolidated: list[Bar],
        config: BacktestConfig,
    ) -> BacktestReport:
        """Run over already-consolidated bars with optional non-trading pre-roll."""
        bootstrap_states, evaluation_bars, evaluation_states = self._evaluation_inputs(
            consolidated,
            config,
            required_warmup_bars=strategy.warmup_bars_needed,
        )

        if bootstrap_states:
            # Production bootstrap semantics are retained.  Entry delivery is
            # suppressed, while private indicator/model state is not rewritten
            # by the engine.  The executable portfolio below always starts flat.
            strategy.bootstrap_history(bootstrap_states)
        flat_model_boundary_applied = False
        if config.require_flat_model_boundary:
            symbols = tuple(sorted({state.symbol for state in evaluation_states}))
            try:
                strategy.apply_flat_model_evaluation_boundary(symbols)
            except (NotImplementedError, RuntimeError) as exc:
                msg = f"required flat model evaluation boundary failed: {exc}"
                raise ValueError(msg) from exc
            flat_model_boundary_applied = True
        signals = strategy.run(evaluation_states)
        raw_signal_ledger = tuple(RawSignalEvidence.from_signal(signal) for signal in signals)
        return self._simulate_report(
            evaluation_bars,
            signals,
            raw_signal_ledger,
            config,
            bootstrap_bars=len(bootstrap_states),
            flat_model_boundary_applied=flat_model_boundary_applied,
        )

    def replay_consolidated_signal_evidence(
        self,
        consolidated: list[Bar],
        evidence: tuple[RawSignalEvidence, ...],
        config: BacktestConfig,
        *,
        source_bootstrap_bars: int,
        source_flat_model_boundary_applied: bool,
    ) -> BacktestReport:
        """Replay a frozen canonical signal ledger without rerunning a strategy.

        This is the production-sizing comparison boundary.  Signal identity and
        decisions remain immutable; only the registered fill/sizing policy may
        differ.  The source report's warm-up and flat-boundary facts must match
        the bars/config supplied for replay.
        """

        if isinstance(source_bootstrap_bars, bool) or not isinstance(source_bootstrap_bars, int):
            _invalid_type("source_bootstrap_bars must be an integer")
        if source_bootstrap_bars < 0:
            _invalid("source_bootstrap_bars must be non-negative")
        if not isinstance(source_flat_model_boundary_applied, bool):
            _invalid_type("source_flat_model_boundary_applied must be a bool")
        if not isinstance(evidence, tuple) or not all(
            isinstance(item, RawSignalEvidence) for item in evidence
        ):
            _invalid_type("evidence must be a tuple of RawSignalEvidence")
        external_ids = tuple(item.external_signal_id for item in evidence)
        if len(external_ids) != len(set(external_ids)):
            _invalid("signal evidence external_signal_id values must be unique")
        if any(
            left.generated_at > right.generated_at for left, right in itertools.pairwise(evidence)
        ):
            _invalid("signal evidence must be chronological")

        bootstrap_states, evaluation_bars, _evaluation_states = self._evaluation_inputs(
            consolidated,
            config,
            required_warmup_bars=source_bootstrap_bars,
        )
        if len(bootstrap_states) != source_bootstrap_bars:
            _invalid("signal evidence source bootstrap count differs from replay bars")
        if config.require_flat_model_boundary and not source_flat_model_boundary_applied:
            _invalid("signal evidence source did not apply the required flat model boundary")
        signals = [item.to_signal() for item in evidence]
        report = self._simulate_report(
            evaluation_bars,
            signals,
            evidence,
            config,
            bootstrap_bars=source_bootstrap_bars,
            flat_model_boundary_applied=source_flat_model_boundary_applied,
        )
        if report.unmatched_signals:
            _invalid("signal evidence contains decisions outside the replay bar interval")
        return report

    def _evaluation_inputs(
        self,
        consolidated: list[Bar],
        config: BacktestConfig,
        *,
        required_warmup_bars: int,
    ) -> tuple[list[MarketState], list[Bar], list[MarketState]]:
        """Validate bars and resolve one exact bootstrap/evaluation boundary."""

        if isinstance(required_warmup_bars, bool) or not isinstance(required_warmup_bars, int):
            _invalid_type("required_warmup_bars must be an integer")
        if required_warmup_bars < 0:
            _invalid("required_warmup_bars must be non-negative")
        self._validate_bars(
            consolidated,
            config=config,
            min_coverage=config.min_bar_coverage,
        )
        execution_bars = [normalize_execution_bar(bar) for bar in consolidated]
        self._validate_bars(
            execution_bars,
            config=config,
            min_coverage=config.min_bar_coverage,
        )
        states = [self._market_state(bar, config) for bar in execution_bars]

        bootstrap_states: list[MarketState] = []
        evaluation_pairs = list(zip(execution_bars, states, strict=True))
        if config.evaluation_start is not None:
            bootstrap_states = [
                state for bar, state in evaluation_pairs if bar.timestamp < config.evaluation_start
            ]
            observed = len(bootstrap_states)
            required = required_warmup_bars
            if observed < required:
                msg = (
                    "insufficient non-trading warm-up: "
                    f"required {required} bars, observed {observed}"
                )
                raise ValueError(msg)
            evaluation_pairs = [
                pair for pair in evaluation_pairs if pair[0].timestamp >= config.evaluation_start
            ]
        if config.evaluation_end is not None:
            evaluation_pairs = [
                pair for pair in evaluation_pairs if pair[0].timestamp < config.evaluation_end
            ]
        if not evaluation_pairs:
            _invalid("evaluation window contains no bars")
        evaluation_bars = [pair[0] for pair in evaluation_pairs]
        evaluation_states = [pair[1] for pair in evaluation_pairs]
        return bootstrap_states, evaluation_bars, evaluation_states

    @staticmethod
    def _simulate_report(
        evaluation_bars: list[Bar],
        signals: list[Signal],
        raw_signal_ledger: tuple[RawSignalEvidence, ...],
        config: BacktestConfig,
        *,
        bootstrap_bars: int,
        flat_model_boundary_applied: bool,
    ) -> BacktestReport:
        """Execute one signal ledger and construct its complete report."""

        simulator = FillSimulator(
            initial_capital=config.initial_capital,
            size_pct=config.size_pct,
            fee_bps=config.fee_bps,
            cost_model=config.execution_costs,
            fill_policy=config.fill_policy,
            sizing_policy=config.sizing_policy,
            coinbase_sizing_rules=config.coinbase_sizing_rules,
        )
        result = simulator.run(evaluation_bars, signals)
        trades = [replace(trade, symbol=config.symbol) for trade in result.trades]
        metrics = compute_metrics(
            result.equity_curve,
            [trade.pnl for trade in trades],
            initial_capital=config.initial_capital,
            annualization_factor=config.annualization_factor,
            gross_trade_pnls=[trade.gross_pnl for trade in trades],
            turnover_notional=result.closed_turnover_notional,
            exposure_seconds=result.exposure_seconds,
            holding_periods_seconds=[
                (trade.exit_ts - trade.entry_ts).total_seconds() for trade in trades
            ],
        )
        return BacktestReport(
            config=config,
            metrics=metrics,
            trades=trades,
            equity_curve=result.equity_curve,
            signals_emitted=len(signals),
            consolidated_bars=len(evaluation_bars),
            unfilled_signals=result.unfilled_signals,
            unmatched_signals=result.unmatched_signals,
            ignored_signals=result.ignored_signals,
            cancelled_signals=result.cancelled_signals,
            rejected_entries=result.rejected_entries,
            ambiguous_bars=result.ambiguous_bars,
            protective_stop_exits=result.protective_stop_exits,
            model_execution_divergences=result.model_execution_divergences,
            buying_power_rejections=result.buying_power_rejections,
            account_ruined=result.account_ruined,
            terminal_position=result.terminal_position,
            gross_pnl=result.gross_pnl,
            closed_gross_pnl=result.closed_gross_pnl,
            terminal_gross_pnl=result.terminal_gross_pnl,
            cost_breakdown=result.cost_breakdown,
            closed_cost_breakdown=result.closed_cost_breakdown,
            turnover_notional=result.turnover_notional,
            closed_turnover_notional=result.closed_turnover_notional,
            holding_periods_bars=result.holding_periods_bars,
            exit_reason_counts=result.exit_reason_counts,
            execution_policy_id=result.execution_policy_id,
            cost_scenario=result.cost_scenario,
            bootstrap_bars=bootstrap_bars,
            flat_model_boundary_applied=flat_model_boundary_applied,
            raw_signal_ledger=raw_signal_ledger,
            sizing_policy=result.sizing_policy,
            sizing_decision_ledger=result.sizing_decisions,
        )

    @staticmethod
    def _market_state(bar: Bar, config: BacktestConfig) -> MarketState:
        metadata = dict(bar.metadata)
        source = config.price_source or bar.source or metadata.get("price_source")
        coverage = metadata.get("coverage", 1.0)
        metadata.update(
            {
                "timeframe": bar.timeframe,
                "source": source,
                "price_timeframe": bar.timeframe,
                "price_source": source,
                "coverage": coverage,
                "asset_class": config.asset_class or metadata.get("asset_class"),
                "requested_product": config.requested_product or metadata.get("requested_product"),
                "canonical_product": config.canonical_product or metadata.get("canonical_product"),
            }
        )
        return MarketState(
            symbol=config.symbol,
            timestamp=bar.timestamp,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            metadata=metadata,
        )

    @staticmethod
    def _validate_bars(  # noqa: PLR0912 - fail-closed invariants stay explicit
        bars: list[Bar],
        *,
        config: BacktestConfig | None = None,
        min_coverage: float,
    ) -> None:
        if not bars:
            _invalid("bars must not be empty")
        previous: datetime | None = None
        symbols: set[str] = set()
        sources: set[str | None] = set()
        timeframes: set[str] = set()
        lineage: dict[str, set[str]] = {
            "asset_class": set(),
            "requested_product": set(),
            "canonical_product": set(),
        }
        lineage_present = dict.fromkeys(lineage, 0)
        for index, bar in enumerate(bars):
            _aware(bar.timestamp, field_name=f"bars[{index}].timestamp")
            if previous is not None:
                if bar.timestamp == previous:
                    _invalid(f"duplicate bar timestamp at {bar.timestamp.isoformat()}")
                if bar.timestamp < previous:
                    _invalid("bars must be strictly chronological")
            previous = bar.timestamp
            symbols.add(bar.symbol)
            resolved_source = bar.source or bar.metadata.get("price_source")
            sources.add(str(resolved_source) if resolved_source is not None else None)
            timeframes.add(bar.timeframe)

            ohlcv_error = ohlcv_invariant_error(
                open_price=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            if ohlcv_error is not None:
                _invalid(f"bar {index} {ohlcv_error}")

            coverage = float(bar.metadata.get("coverage", 1.0))
            if not math.isfinite(coverage) or not 0.0 <= coverage <= 1.0:
                _invalid(f"bar {index} has invalid coverage {coverage!r}")
            if coverage < min_coverage:
                msg = (
                    f"bar {index} coverage {coverage:.4f} is below configured "
                    f"minimum {min_coverage:.4f}"
                )
                raise ValueError(msg)
            for key, lineage_values in lineage.items():
                value = bar.metadata.get(key)
                if value is not None:
                    lineage_values.add(str(value))
                    lineage_present[key] += 1

        if len(symbols) != 1:
            _invalid(f"bars contain inconsistent symbols: {sorted(symbols)}")
        if len(sources) != 1:
            _invalid("bars contain inconsistent sources")
        if len(timeframes) != 1:
            _invalid(f"bars contain inconsistent timeframes: {sorted(timeframes)}")
        for key, lineage_values in lineage.items():
            if len(lineage_values) > 1:
                _invalid(f"bars contain inconsistent {key} lineage")
            if 0 < lineage_present[key] < len(bars):
                _invalid(f"bars contain incomplete {key} lineage")

        if config is None:
            return
        if symbols != {config.symbol}:
            actual = next(iter(symbols))
            _invalid(f"configured symbol {config.symbol!r} does not match {actual!r}")
        if config.price_source is not None and sources != {config.price_source}:
            _invalid(f"configured source {config.price_source!r} does not match bar source")
        expected_lineage = {
            "asset_class": config.asset_class,
            "requested_product": config.requested_product,
            "canonical_product": config.canonical_product,
        }
        for key, expected in expected_lineage.items():
            observed = lineage[key]
            if expected is None:
                continue
            if lineage_present[key] != len(bars):
                _invalid(f"configured {key} requires complete bar lineage")
            if observed != {expected}:
                _invalid(f"configured {key} does not match bar lineage")
