"""Adapter utilities to derive scoring inputs from canonical Signals."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from lib_strategy.signals.normalization import normalize_signal_action
from lib_strategy.signals.signal import Signal, SignalAction


@dataclass(frozen=True)
class EntrySignalRequirementDecision:
    """Pure result for entry-only stop and scoring-provenance requirements."""

    applicable: bool
    allowed: bool
    scoring_input_source: str
    rule_code: str | None
    message: str | None
    context: Mapping[str, object]


@dataclass(frozen=True)
class ScoringSignalView:
    """Scoring-focused view derived from a canonical Signal."""

    signal_id: str
    strategy_id: str
    strategy_type: str
    asset: str
    direction: int
    expected_return: float
    predicted_risk: float
    horizon_days: float
    confidence: float
    signal_timestamp: datetime
    metadata: dict[str, Any]

    @property
    def sharpe_raw(self) -> float:
        if self.predicted_risk <= 0:
            return 0.0
        return self.direction * (self.expected_return / self.predicted_risk)


_HORIZON_MAP = {
    "1H": 1 / 24,
    "4H": 4 / 24,
    "1D": 1.0,
    "1W": 5.0,
    "2W": 10.0,
    "1M": 21.0,
    "INTRADAY": 0.5,
    "SWING": 5.0,
    "POSITION": 21.0,
}


def _direction_from_action(action: SignalAction) -> int:
    if action == SignalAction.LONG:
        return 1
    if action == SignalAction.SHORT:
        return -1
    return 0


def _horizon_days(signal: Signal) -> float:
    if signal.horizon_days:
        return float(signal.horizon_days)
    if signal.horizon:
        key = signal.horizon.strip().upper()
        if key in _HORIZON_MAP:
            return _HORIZON_MAP[key]
        # Strategy configs commonly use compact explicit durations such as
        # ``5d``. Parse them instead of silently falling back to one day and
        # contradicting the configured feedback horizon.
        if len(key) > 1:
            try:
                amount = float(key[:-1])
            except ValueError:
                amount = 0.0
            if amount > 0:
                unit_days = {"H": 1 / 24, "D": 1.0, "W": 5.0, "M": 21.0}
                if key[-1] in unit_days:
                    return amount * unit_days[key[-1]]
    return 1.0


def _resolve_base_volatility(signal: Signal, default: float = 0.02) -> float:
    meta = signal.metadata or {}
    value = meta.get("base_volatility", default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_risk_reward(signal: Signal, default: float = 1.5) -> float:
    """Risk/reward multiplier a strategy uses to size targets off its stop."""
    meta = signal.metadata or {}
    value = meta.get("risk_reward", default)
    try:
        rr = float(value)
    except (TypeError, ValueError):
        return default
    return rr if rr > 0 else default


def _estimate_mu_sigma(
    signal: Signal,
    horizon_days: float,
    *,
    base_volatility: float,
) -> tuple[float, float, str]:
    expected_return = signal.expected_return
    predicted_risk = signal.predicted_risk
    expected_is_explicit = expected_return is not None
    risk_is_explicit = predicted_risk is not None
    if expected_is_explicit and risk_is_explicit:
        estimate_source = "explicit"
    elif expected_is_explicit or risk_is_explicit:
        estimate_source = "mixed_explicit_and_heuristic"
    else:
        estimate_source = "base_volatility_heuristic"

    # Derive mu/sigma from the price ladder only when the strategy set neither
    # explicitly. A full entry+stop+target ladder yields sigma from the stop
    # distance and mu from the target distance; an entry+stop-only ladder (most
    # deployable cores) yields mu from the stop scaled by the strategy's
    # risk/reward. Anything still missing falls through to synthesis so a
    # directional signal never scores a hard zero.
    if (
        expected_return is None
        and predicted_risk is None
        and signal.entry_price
        and signal.entry_price > 0
    ):
        entry = signal.entry_price
        if signal.stop_loss:
            predicted_risk = abs(signal.stop_loss - entry) / entry
        if signal.take_profit:
            expected_return = (abs(signal.take_profit - entry) / entry) * signal.confidence
            estimate_source = "price_ladder"
        elif predicted_risk is not None:
            expected_return = predicted_risk * _resolve_risk_reward(signal) * signal.confidence
            estimate_source = "stop_risk_reward_heuristic"

    if expected_return is None:
        expected_return = signal.confidence * base_volatility * 0.5
        estimate_source = (
            "mixed_explicit_and_heuristic" if risk_is_explicit else "base_volatility_heuristic"
        )
    if predicted_risk is None:
        predicted_risk = base_volatility * (horizon_days**0.5)
        if expected_is_explicit:
            estimate_source = "mixed_explicit_and_heuristic"

    predicted_risk = max(float(predicted_risk), 0.001)
    expected_return = float(expected_return)
    return expected_return, predicted_risk, estimate_source


def build_scoring_view(
    signal: Signal,
    *,
    scoring_action_override: SignalAction | None = None,
) -> ScoringSignalView:
    """Create a scoring view without changing the canonical signal action.

    ``scoring_action_override`` is reserved for coordinated portfolio semantics,
    such as scoring an incumbent ``HOLD`` as continued long exposure while the
    canonical audit action remains ``HOLD``. Only directional overrides are
    accepted and the caller must record their provenance.
    """
    if scoring_action_override not in {None, SignalAction.LONG, SignalAction.SHORT}:
        message = "scoring_action_override must be LONG, SHORT, or None"
        raise ValueError(message)
    action = scoring_action_override or normalize_signal_action(signal.action)
    direction = _direction_from_action(action)
    horizon_days = _horizon_days(signal)
    base_vol = _resolve_base_volatility(signal)
    expected_return, predicted_risk, estimate_source = _estimate_mu_sigma(
        signal,
        horizon_days,
        base_volatility=base_vol,
    )
    timestamp = signal.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)

    metadata = dict(signal.metadata or {})
    metadata["scoring_input_source"] = estimate_source
    if scoring_action_override is not None:
        metadata["scoring_action_override"] = scoring_action_override.value
    return ScoringSignalView(
        signal_id=signal.signal_id,
        strategy_id=signal.strategy_id,
        strategy_type=signal.strategy_type,
        asset=signal.symbol,
        direction=direction,
        expected_return=expected_return,
        predicted_risk=predicted_risk,
        horizon_days=horizon_days,
        confidence=signal.confidence,
        signal_timestamp=timestamp,
        metadata=metadata,
    )


def evaluate_entry_signal_requirements(
    signal: Signal,
    *,
    require_stop_loss: bool,
    require_explicit_scoring_inputs: bool,
    scoring_input_source: str | None = None,
) -> EntrySignalRequirementDecision:
    """Evaluate production entry admissibility without execution side effects.

    This is the single pure source for the two entry-only requirements shared
    by historical scoring replay and :class:`execution_engine.risk_guard.RiskGuard`.
    It intentionally does not evaluate user binding thresholds, account limits,
    broker capabilities, or whether a position should be opened.
    """

    resolved_source = str(
        scoring_input_source or (signal.metadata or {}).get("scoring_input_source") or "missing"
    )
    context: Mapping[str, object]
    if signal.action not in {SignalAction.LONG, SignalAction.SHORT}:
        return EntrySignalRequirementDecision(
            applicable=False,
            allowed=True,
            scoring_input_source=resolved_source,
            rule_code=None,
            message=None,
            context=MappingProxyType({}),
        )
    if require_stop_loss and signal.stop_loss is None:
        context = MappingProxyType({"signal_id": signal.signal_id, "symbol": signal.symbol})
        return EntrySignalRequirementDecision(
            applicable=True,
            allowed=False,
            scoring_input_source=resolved_source,
            rule_code="stop_loss_required",
            message="Entry signals must include stop_loss",
            context=context,
        )
    if require_explicit_scoring_inputs and resolved_source != "explicit":
        context = MappingProxyType(
            {
                "signal_id": signal.signal_id,
                "symbol": signal.symbol,
                "scoring_input_source": resolved_source,
            }
        )
        return EntrySignalRequirementDecision(
            applicable=True,
            allowed=False,
            scoring_input_source=resolved_source,
            rule_code="explicit_scoring_inputs_required",
            message=(
                "Entry signal uses uncalibrated scoring inputs "
                f"({resolved_source}); explicit forecasts are required"
            ),
            context=context,
        )
    return EntrySignalRequirementDecision(
        applicable=True,
        allowed=True,
        scoring_input_source=resolved_source,
        rule_code=None,
        message=None,
        context=MappingProxyType({}),
    )
