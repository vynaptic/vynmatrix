"""Parameter Optimizer - Generates optimization suggestions for strategies.

When a strategy makes consecutive wrong predictions, this component
generates parameter optimization suggestions that traders can review.
Promotion is deliberately outside this runtime and goes through the normal
source-control, validation, image-build, and deployment path.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lib_application.services.strategy_feedback import exact_strategy_version_is_active
from lib_common.logging import get_logger

from .models import (
    EvaluationHorizon,
    FeedbackStatus,
    OptimizationMethod,
    OptimizationSuggestion,
    StrategyPerformanceStats,
    TriggerReason,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = get_logger(__name__)

# Accuracy threshold below which to adjust thresholds (50%)
ACCURACY_ADJUSTMENT_THRESHOLD = 0.5

# Default parameter ranges for common strategy parameters
DEFAULT_PARAMETER_RANGES: dict[str, dict[str, float]] = {
    # Moving averages
    "fast_period": {"min": 5, "max": 50, "step": 5},
    "slow_period": {"min": 10, "max": 200, "step": 10},
    "ema_period": {"min": 5, "max": 100, "step": 5},
    "sma_period": {"min": 5, "max": 200, "step": 10},
    # Momentum indicators
    "rsi_period": {"min": 7, "max": 21, "step": 2},
    "oversold": {"min": 20, "max": 40, "step": 5},
    "overbought": {"min": 60, "max": 80, "step": 5},
    "rsi_oversold": {"min": 20, "max": 40, "step": 5},
    "rsi_overbought": {"min": 60, "max": 80, "step": 5},
    "macd_fast": {"min": 8, "max": 16, "step": 2},
    "macd_slow": {"min": 20, "max": 32, "step": 2},
    "macd_signal": {"min": 7, "max": 12, "step": 1},
    # Volatility
    "atr_period": {"min": 10, "max": 30, "step": 5},
    "atr_multiplier": {"min": 1.5, "max": 4.0, "step": 0.5},
    "bb_period": {"min": 15, "max": 30, "step": 5},
    "bb_std": {"min": 1.5, "max": 3.0, "step": 0.5},
    # Risk management
    "stop_loss_pct": {"min": 0.001, "max": 0.10, "step": 0.001},
    "take_profit_pct": {"min": 0.001, "max": 0.20, "step": 0.001},
    "trailing_stop_pct": {"min": 0.001, "max": 0.10, "step": 0.001},
}
ADJUSTABLE_PARAMETER_NAMES = frozenset(DEFAULT_PARAMETER_RANGES) | {"position_size_pct"}


class SuggestionGenerationError(ValueError):
    """Raised when an optimization suggestion cannot be generated safely."""


def _coerce_numeric(value: Any) -> Any:
    """Coerce a numeric string to ``int`` or finite ``float``."""
    if isinstance(value, (int, float, bool)):
        return value
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    try:
        # Prefer int when the string has no decimal point
        if "." not in stripped and "e" not in stripped.lower():
            return int(stripped)
        numeric = float(stripped)
        return numeric if math.isfinite(numeric) else value
    except ValueError:
        return value


def _validate_parameter_value(value: Any, *, path: str) -> None:
    """Reject values that cannot be represented safely in JSON."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            message = f"Non-finite parameter value at {path}"
            raise SuggestionGenerationError(message)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_parameter_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                message = f"Invalid nested parameter key at {path}"
                raise SuggestionGenerationError(message)
            _validate_parameter_value(item, path=f"{path}.{key}")
        return
    message = f"Unsupported parameter value at {path}: {type(value).__name__}"
    raise SuggestionGenerationError(message)


def _validate_parameter_mapping(params: Mapping[Any, Any]) -> None:
    """Validate one parameter mapping without coercing its values."""
    for key, value in params.items():
        if not isinstance(key, str) or not key.strip():
            message = "Suggested parameters require non-empty string keys"
            raise SuggestionGenerationError(message)
        _validate_parameter_value(value, path=key)


class ParameterOptimizer:
    """Generates parameter optimization suggestions for underperforming strategies."""

    def __init__(
        self,
        engine: Engine,
        parameter_ranges: dict[str, dict[str, float]] | None = None,
    ) -> None:
        """Initialize the parameter optimizer.

        Args:
            engine: SQLAlchemy engine for database access
            parameter_ranges: Custom parameter ranges for optimization
        """
        self._engine = engine
        self.parameter_ranges: dict[str, dict[str, float]] = (
            parameter_ranges or DEFAULT_PARAMETER_RANGES
        )

    def generate_suggestion(
        self,
        strategy_id: str,
        strategy_code: str,
        strat_ver_id: int,
        instr_id: int | None,
        symbol: str | None,
        trigger_reason: TriggerReason,
        horizon: EvaluationHorizon = EvaluationHorizon.D1,
        consecutive_wrong: int = 0,
        accuracy_window_days: int | None = None,
        accuracy_pct: float | None = None,
        performance_stats: StrategyPerformanceStats | None = None,
    ) -> OptimizationSuggestion:
        """Generate parameter optimization suggestion for a strategy.

        Args:
            strategy_id: Database ID of the strategy
            strategy_code: Strategy code name
            strat_ver_id: Strategy version ID
            instr_id: Instrument ID (optional, for instrument-specific optimization)
            symbol: Symbol name
            trigger_reason: Why optimization was triggered
            horizon: Evaluation horizon that triggered the suggestion
            consecutive_wrong: Number of consecutive wrong predictions
            accuracy_window_days: Days of accuracy window for stats
            accuracy_pct: Current accuracy percentage
            performance_stats: Full performance statistics

        Returns:
            OptimizationSuggestion with suggested parameter changes
        """
        current_params = self._load_current_params(
            strategy_id=strategy_id,
            strat_ver_id=strat_ver_id,
        )

        # Generate suggested parameters based on performance
        suggested_params = self._generate_suggested_params(
            current_params=current_params,
            trigger_reason=trigger_reason,
            _consecutive_wrong=consecutive_wrong,
            accuracy_pct=accuracy_pct,
            _performance_stats=performance_stats,
        )
        if suggested_params == current_params:
            message = (
                "No supported parameter can be adjusted for "
                f"strategy/version {strategy_id}/{strat_ver_id}"
            )
            raise SuggestionGenerationError(message)
        changed_parameters = sorted(
            key
            for key in set(current_params) | set(suggested_params)
            if current_params.get(key) != suggested_params.get(key)
        )

        # Generate explanation
        explanation = self._generate_explanation(
            strategy_code=strategy_code,
            trigger_reason=trigger_reason,
            consecutive_wrong=consecutive_wrong,
            accuracy_pct=accuracy_pct,
            current_params=current_params,
            suggested_params=suggested_params,
        )

        # Build supporting data
        supporting_data: dict[str, Any] = {
            "trigger_timestamp": datetime.now(tz=UTC).isoformat(),
            "optimization_method": OptimizationMethod.BOUNDED_HEURISTIC.value,
            "changed_parameters": changed_parameters,
            "evaluation_horizon": horizon.value,
        }
        if performance_stats:
            supporting_data["performance_stats"] = {
                "total_signals": performance_stats.total_signals,
                "accuracy_pct": performance_stats.accuracy_pct,
                "recent_accuracy_pct": performance_stats.recent_accuracy_pct,
                "max_consecutive_wrong": performance_stats.max_consecutive_wrong,
            }

        return OptimizationSuggestion(
            strategy_id=strategy_id,
            strategy_code=strategy_code,
            strat_ver_id=strat_ver_id,
            instr_id=instr_id,
            symbol=symbol,
            horizon=horizon,
            trigger_reason=trigger_reason,
            consecutive_wrong_signals=consecutive_wrong,
            accuracy_window_days=accuracy_window_days,
            accuracy_pct=accuracy_pct,
            current_params=current_params,
            suggested_params=suggested_params,
            optimization_method=OptimizationMethod.BOUNDED_HEURISTIC,
            explanation=explanation,
            supporting_data=supporting_data,
        )

    def _load_current_params(
        self,
        *,
        strategy_id: str,
        strat_ver_id: int,
    ) -> dict[str, Any]:
        """Load the exact active version's canonical parameter snapshot."""
        from lib_application.db.models import Strategy, StrategyVersion  # noqa: PLC0415

        if not isinstance(strat_ver_id, int) or isinstance(strat_ver_id, bool) or strat_ver_id <= 0:
            message = f"Invalid strategy version ID: {strat_ver_id}"
            raise SuggestionGenerationError(message)

        with Session(self._engine) as session:
            params = session.execute(
                select(StrategyVersion.default_params)
                .join(Strategy, Strategy.strategy_id == StrategyVersion.strategy_id)
                .where(
                    StrategyVersion.strat_ver_id == strat_ver_id,
                    StrategyVersion.strategy_id == strategy_id,
                    StrategyVersion.status == "active",
                    Strategy.is_active.is_(True),
                )
            ).scalar_one_or_none()

        return self._normalize_current_params(
            params,
            strategy_id=strategy_id,
            strat_ver_id=strat_ver_id,
        )

    def _normalize_current_params(
        self,
        params: Any,
        *,
        strategy_id: str,
        strat_ver_id: int,
    ) -> dict[str, Any]:
        """Validate and normalize one persisted version-parameter snapshot."""
        if not isinstance(params, Mapping) or not params:
            message = (
                "Missing or invalid default_params for active strategy/version "
                f"{strategy_id}/{strat_ver_id}"
            )
            raise SuggestionGenerationError(message)

        current_params: dict[str, Any] = {}
        for key, value in params.items():
            if not isinstance(key, str) or not key.strip():
                message = (
                    "Strategy default_params must use non-empty string keys for "
                    f"{strategy_id}/{strat_ver_id}"
                )
                raise SuggestionGenerationError(message)
            coerced = _coerce_numeric(value)
            _validate_parameter_value(coerced, path=key)
            if key in ADJUSTABLE_PARAMETER_NAMES and (
                not isinstance(coerced, (int, float)) or isinstance(coerced, bool)
            ):
                message = (
                    f"Parameter {key!r} must be numeric for "
                    f"strategy/version {strategy_id}/{strat_ver_id}"
                )
                raise SuggestionGenerationError(message)
            current_params[key] = coerced
        return current_params

    def _generate_suggested_params(
        self,
        current_params: dict[str, Any],
        trigger_reason: TriggerReason,
        _consecutive_wrong: int,
        accuracy_pct: float | None,
        _performance_stats: StrategyPerformanceStats | None,
    ) -> dict[str, Any]:
        """Generate one bounded, review-only parameter adjustment."""
        suggested = dict(current_params)

        # Adjust parameters based on trigger reason
        if trigger_reason == TriggerReason.CONSECUTIVE_WRONG:
            # Strategy is making wrong predictions - try to reduce sensitivity
            self._adjust_sensitivity_params(suggested, direction="decrease")
        elif trigger_reason == TriggerReason.LOW_ACCURACY:
            # Low overall accuracy - try different parameter values
            self._adjust_for_accuracy(suggested, accuracy_pct or 0.5)
        elif trigger_reason == TriggerReason.HIGH_DRAWDOWN:
            # High drawdown - tighten risk parameters
            self._adjust_risk_params(suggested, direction="tighten")

        return suggested

    def _adjust_sensitivity_params(self, params: dict[str, Any], direction: str) -> None:
        """Adjust indicator sensitivity parameters."""
        # Moving average periods - increase to reduce noise
        period_adjustments = {
            "fast_period": 5 if direction == "decrease" else -5,
            "slow_period": 10 if direction == "decrease" else -10,
            "ema_period": 5 if direction == "decrease" else -5,
            "sma_period": 10 if direction == "decrease" else -10,
            "rsi_period": 2 if direction == "decrease" else -2,
            "atr_period": 5 if direction == "decrease" else -5,
        }

        for param, adjustment in period_adjustments.items():
            if param in params:
                current = _coerce_numeric(params[param])
                if isinstance(current, (int, float)):
                    new_value = current + adjustment
                    ranges = self.parameter_ranges.get(param, {})
                    min_val = ranges.get("min", 1)
                    max_val = ranges.get("max", 1000)
                    params[param] = type(current)(max(min_val, min(max_val, new_value)))

    def _adjust_for_accuracy(self, params: dict[str, Any], current_accuracy: float) -> None:
        """Adjust parameters to improve accuracy."""
        # If accuracy is below threshold, try reversing thresholds
        if current_accuracy < ACCURACY_ADJUSTMENT_THRESHOLD:
            for oversold_key, overbought_key in (
                ("oversold", "overbought"),
                ("rsi_oversold", "rsi_overbought"),
            ):
                if oversold_key not in params or overbought_key not in params:
                    continue
                oversold = _coerce_numeric(params[oversold_key])
                overbought = _coerce_numeric(params[overbought_key])
                if (
                    isinstance(oversold, (int, float))
                    and not isinstance(oversold, bool)
                    and isinstance(overbought, (int, float))
                    and not isinstance(overbought, bool)
                ):
                    params[oversold_key] = max(20, oversold - 5)
                    params[overbought_key] = min(80, overbought + 5)

        # Increase period lengths for smoother signals
        self._adjust_sensitivity_params(params, "decrease")

    def _adjust_risk_params(self, params: dict[str, Any], direction: str) -> None:
        """Adjust risk management parameters."""
        if direction == "tighten":
            for param, (floor, factor) in {
                "stop_loss_pct": (0.001, 0.8),
                "trailing_stop_pct": (0.001, 0.8),
                "position_size_pct": (0.005, 0.8),
            }.items():
                if param in params:
                    current = _coerce_numeric(params[param])
                    if isinstance(current, (int, float)):
                        params[param] = round(max(floor, current * factor), 6)

    def _generate_explanation(
        self,
        strategy_code: str,
        trigger_reason: TriggerReason,
        consecutive_wrong: int,
        accuracy_pct: float | None,
        current_params: dict[str, Any],
        suggested_params: dict[str, Any],
    ) -> str:
        """Generate human-readable explanation for the suggestion."""
        lines = [
            f"Parameter Optimization Suggestion for {strategy_code}",
            "=" * 60,
            "",
            f"Trigger: {trigger_reason.value}",
        ]

        if trigger_reason == TriggerReason.CONSECUTIVE_WRONG:
            lines.append(f"The strategy made {consecutive_wrong} consecutive wrong predictions.")
        elif trigger_reason == TriggerReason.LOW_ACCURACY:
            lines.append(f"Current accuracy: {accuracy_pct:.1%}" if accuracy_pct else "")

        lines.extend(
            [
                "",
                "Parameter Changes:",
                "-" * 40,
            ]
        )

        # Show changed parameters
        for key in sorted(set(current_params) | set(suggested_params)):
            old_val = current_params.get(key)
            new_val = suggested_params.get(key)
            if old_val != new_val:
                lines.append(f"  {key}: {old_val} -> {new_val}")

        lines.extend(
            [
                "",
                "Recommendation:",
                "Review the suggested parameters. If accepted, promote them through",
                "a source-controlled config change, validation, image build, and",
                "the normal deployment process.",
            ]
        )

        return "\n".join(lines)

    def persist_suggestion(self, suggestion: OptimizationSuggestion) -> int | None:
        """Persist optimization suggestion to database.

        Args:
            suggestion: The optimization suggestion

        Returns:
            Database ID of the persisted record, or ``None`` when the exact
            strategy/version is not currently eligible for optimization.
        """
        from lib_application.db.models import (  # noqa: PLC0415
            StrategyParameterFeedback,
            StrategyVersion,
        )

        if (
            not isinstance(suggestion.strat_ver_id, int)
            or isinstance(suggestion.strat_ver_id, bool)
            or suggestion.strat_ver_id <= 0
        ):
            logger.warning(
                "Suppressing parameter suggestion without a valid strategy version: %s",
                suggestion.strategy_id,
            )
            return None
        if (
            not isinstance(suggestion.current_params, Mapping)
            or not isinstance(suggestion.suggested_params, Mapping)
            or not suggestion.current_params
            or not suggestion.suggested_params
            or suggestion.current_params == suggestion.suggested_params
            or not isinstance(suggestion.explanation, str)
            or not suggestion.explanation.strip()
        ):
            logger.error(
                "Suppressing empty or non-actionable parameter suggestion for %s/%s",
                suggestion.strategy_id,
                suggestion.strat_ver_id,
            )
            return None

        with Session(self._engine) as session:
            if not exact_strategy_version_is_active(
                session,
                strategy_id=suggestion.strategy_id,
                strat_ver_id=suggestion.strat_ver_id,
            ):
                logger.warning(
                    "Suppressing parameter suggestion for inactive or mismatched strategy/version: "
                    "%s/%s",
                    suggestion.strategy_id,
                    suggestion.strat_ver_id,
                )
                return None
            persisted_params = session.execute(
                select(StrategyVersion.default_params).where(
                    StrategyVersion.strat_ver_id == suggestion.strat_ver_id,
                    StrategyVersion.strategy_id == suggestion.strategy_id,
                )
            ).scalar_one()
            try:
                canonical_params = self._normalize_current_params(
                    persisted_params,
                    strategy_id=suggestion.strategy_id,
                    strat_ver_id=suggestion.strat_ver_id,
                )
                _validate_parameter_mapping(suggestion.suggested_params)
            except SuggestionGenerationError:
                logger.exception(
                    "Suppressing invalid parameter snapshot for %s/%s",
                    suggestion.strategy_id,
                    suggestion.strat_ver_id,
                )
                return None
            if canonical_params != suggestion.current_params:
                logger.error(
                    "Suppressing stale parameter suggestion for %s/%s",
                    suggestion.strategy_id,
                    suggestion.strat_ver_id,
                )
                return None

            record = StrategyParameterFeedback(
                strategy_id=suggestion.strategy_id,
                strat_ver_id=suggestion.strat_ver_id,
                instr_id=suggestion.instr_id,
                horizon=suggestion.horizon.value,
                trigger_reason=suggestion.trigger_reason.value,
                consecutive_wrong_signals=suggestion.consecutive_wrong_signals,
                accuracy_window_days=suggestion.accuracy_window_days,
                accuracy_pct=suggestion.accuracy_pct,
                current_params=suggestion.current_params,
                suggested_params=suggestion.suggested_params,
                optimization_method=suggestion.optimization_method.value,
                explanation=suggestion.explanation,
                supporting_data=suggestion.supporting_data,
                status=FeedbackStatus.PENDING.value,
                created_at=datetime.now(tz=UTC),
            )
            session.add(record)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing_feedback_id = session.execute(
                    select(StrategyParameterFeedback.feedback_id).where(
                        StrategyParameterFeedback.strategy_id == suggestion.strategy_id,
                        StrategyParameterFeedback.strat_ver_id == suggestion.strat_ver_id,
                        StrategyParameterFeedback.instr_id == suggestion.instr_id,
                        StrategyParameterFeedback.horizon == suggestion.horizon.value,
                        StrategyParameterFeedback.status == FeedbackStatus.PENDING.value,
                    )
                ).scalar_one_or_none()
                if existing_feedback_id is None:
                    raise
                logger.info(
                    "Using concurrent pending suggestion %s for %s/%s/%s",
                    existing_feedback_id,
                    suggestion.strategy_id,
                    suggestion.instr_id,
                    suggestion.horizon.value,
                )
                feedback_id_result = int(existing_feedback_id)
            else:
                feedback_id_result = int(record.feedback_id)
            return feedback_id_result
