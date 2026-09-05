"""Pure position-sizing policy shared by execution and validation.

The domain kernel ends at the base-unit quantity supplied to a broker. Venue
denomination, product increments, and post-quantization minimum enforcement
remain adapter responsibilities.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import NoReturn

_POLICY_VERSION = "position-sizing-v2"
_SMALL_PRICE_THRESHOLD = 100.0


def _invalid(message: str) -> NoReturn:
    raise ValueError(message)


def _finite_fraction(value: float, *, field_name: str) -> None:
    if not math.isfinite(value) or not 0.0 < value <= 1.0:
        _invalid(f"{field_name} must be finite and in (0, 1]")


class PositionSizingMethod(StrEnum):
    """Sizing methods implemented by the shared kernel."""

    FIXED_PCT = "fixed_pct"
    RISK_PCT = "risk_pct"


class MissingStopBehavior(StrEnum):
    """Fail-closed research behavior versus production fallback."""

    REJECT = "reject"
    FALLBACK_FIXED_PCT = "fallback_fixed_pct"


class SizingDecisionStatus(StrEnum):
    """Whether a sizing decision can proceed to broker checks."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class PreBrokerQuantityRounding(StrEnum):
    """Base-unit rounding applied before broker-specific translation."""

    NONE = "none"
    PRICE_TIERED_DECIMALS = "price_tiered_decimals"


def apply_pre_broker_quantity_rounding(
    quantity: float,
    price: float,
    policy: PreBrokerQuantityRounding,
) -> float:
    """Apply the registered pre-broker quantity policy deterministically."""

    if not math.isfinite(quantity) or quantity < 0.0:
        _invalid("quantity must be finite and non-negative")
    if not math.isfinite(price) or price <= 0.0:
        _invalid("price must be finite and positive")
    if not isinstance(policy, PreBrokerQuantityRounding):
        message = "policy must be a PreBrokerQuantityRounding"
        raise TypeError(message)
    if policy is PreBrokerQuantityRounding.NONE:
        return quantity
    if quantity < 1.0:
        return round(quantity, 8)
    if price < _SMALL_PRICE_THRESHOLD:
        return round(quantity, 4)
    return round(quantity, 2)


@dataclass(frozen=True, slots=True)
class PositionSizingRequest:
    """Account and fill-time inputs visible to one sizing decision."""

    price: float
    equity: float
    buying_power: float
    stop_loss: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.price) or self.price <= 0.0:
            _invalid("price must be finite and positive")
        if not math.isfinite(self.equity) or self.equity <= 0.0:
            _invalid("equity must be finite and positive")
        if not math.isfinite(self.buying_power) or self.buying_power < 0.0:
            _invalid("buying_power must be finite and non-negative")
        if self.stop_loss is not None and not math.isfinite(self.stop_loss):
            _invalid("stop_loss must be finite when supplied")


@dataclass(frozen=True, slots=True)
class PositionSizingDecision:
    """Sizing result before provider denomination and precision checks."""

    policy_id: str
    requested_method: PositionSizingMethod
    method_used: PositionSizingMethod
    status: SizingDecisionStatus
    rejection_reason: str | None
    fallback_reason: str | None
    price: float
    equity: float
    buying_power: float
    stop_loss: float | None
    risk_per_unit: float | None
    requested_notional_value: float
    sizing_intent_quantity: float
    sizing_intent_notional_value: float
    broker_input_quantity: float
    broker_input_notional_value: float
    effective_sizing_risk_amount: float
    effective_sizing_risk_pct: float
    capped_by_buying_power: bool
    capped_by_max_position: bool
    below_minimum: bool

    @property
    def accepted(self) -> bool:
        return self.status is SizingDecisionStatus.ACCEPTED

    @property
    def quantity(self) -> float:
        """Return the base-unit quantity supplied to the broker adapter."""

        return self.broker_input_quantity

    @property
    def notional_value(self) -> float:
        """Return reference-price notional before provider translation."""

        return self.broker_input_notional_value

    def to_dict(self) -> dict[str, object]:
        """Return a fresh JSON-safe decision ledger payload."""

        payload = asdict(self)
        payload["requested_method"] = self.requested_method.value
        payload["method_used"] = self.method_used.value
        payload["status"] = self.status.value
        return payload


@dataclass(frozen=True, slots=True)
class PositionSizingPolicy:
    """Immutable generic policy used by production and validation."""

    policy_id: str
    method: PositionSizingMethod
    fixed_pct: float
    risk_pct: float
    max_position_pct: float
    minimum_position_notional: float
    missing_stop_behavior: MissingStopBehavior = MissingStopBehavior.FALLBACK_FIXED_PCT
    pre_broker_quantity_rounding: PreBrokerQuantityRounding = PreBrokerQuantityRounding.NONE
    version: str = _POLICY_VERSION

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            _invalid("policy_id must not be blank")
        if self.version != _POLICY_VERSION:
            _invalid(f"position-sizing version must be {_POLICY_VERSION}")
        if not isinstance(self.method, PositionSizingMethod):
            message = "method must be a PositionSizingMethod"
            raise TypeError(message)
        if not isinstance(self.missing_stop_behavior, MissingStopBehavior):
            message = "missing_stop_behavior must be a MissingStopBehavior"
            raise TypeError(message)
        if not isinstance(self.pre_broker_quantity_rounding, PreBrokerQuantityRounding):
            message = "pre_broker_quantity_rounding must be a PreBrokerQuantityRounding"
            raise TypeError(message)
        _finite_fraction(self.fixed_pct, field_name="fixed_pct")
        _finite_fraction(self.risk_pct, field_name="risk_pct")
        _finite_fraction(self.max_position_pct, field_name="max_position_pct")
        if (
            not math.isfinite(self.minimum_position_notional)
            or self.minimum_position_notional < 0.0
        ):
            _invalid("minimum_position_notional must be finite and non-negative")

    @classmethod
    def fixed_percent(
        cls,
        *,
        policy_id: str,
        fixed_pct: float,
        max_position_pct: float,
        minimum_position_notional: float,
        pre_broker_quantity_rounding: PreBrokerQuantityRounding = (PreBrokerQuantityRounding.NONE),
    ) -> PositionSizingPolicy:
        """Build a current-equity fixed-percent policy."""

        return cls(
            policy_id=policy_id,
            method=PositionSizingMethod.FIXED_PCT,
            fixed_pct=fixed_pct,
            risk_pct=0.01,
            max_position_pct=max_position_pct,
            minimum_position_notional=minimum_position_notional,
            pre_broker_quantity_rounding=pre_broker_quantity_rounding,
        )

    @classmethod
    def initial_stop_risk(
        cls,
        *,
        policy_id: str,
        risk_pct: float,
        fixed_pct_fallback: float,
        max_position_pct: float,
        minimum_position_notional: float,
        pre_broker_quantity_rounding: PreBrokerQuantityRounding = (PreBrokerQuantityRounding.NONE),
        missing_stop_behavior: MissingStopBehavior = MissingStopBehavior.REJECT,
    ) -> PositionSizingPolicy:
        """Build an initial-stop risk policy with an explicit missing-stop rule."""

        return cls(
            policy_id=policy_id,
            method=PositionSizingMethod.RISK_PCT,
            fixed_pct=fixed_pct_fallback,
            risk_pct=risk_pct,
            max_position_pct=max_position_pct,
            minimum_position_notional=minimum_position_notional,
            missing_stop_behavior=missing_stop_behavior,
            pre_broker_quantity_rounding=pre_broker_quantity_rounding,
        )

    def calculate(self, request: PositionSizingRequest) -> PositionSizingDecision:
        """Execute the shared kernel without mutating policy or account inputs."""

        requested_method = self.method
        method_used = requested_method
        fallback_reason: str | None = None
        risk_per_unit: float | None = None

        if requested_method is PositionSizingMethod.RISK_PCT:
            if request.stop_loss is None:
                fallback_reason = "missing_stop_loss"
            else:
                risk_per_unit = abs(request.price - request.stop_loss)
                if risk_per_unit == 0.0:
                    fallback_reason = "zero_stop_distance"
            if fallback_reason is not None:
                if self.missing_stop_behavior is MissingStopBehavior.REJECT:
                    return _rejected_decision(
                        request,
                        self,
                        reason=fallback_reason,
                        risk_per_unit=risk_per_unit,
                    )
                method_used = PositionSizingMethod.FIXED_PCT

        if method_used is PositionSizingMethod.FIXED_PCT:
            requested_notional = request.equity * self.fixed_pct
            notional = min(requested_notional, request.buying_power)
            capped_by_buying_power = notional < requested_notional
            quantity = notional / request.price
        else:
            assert risk_per_unit is not None
            assert risk_per_unit > 0.0
            requested_risk_amount = request.equity * self.risk_pct
            quantity = requested_risk_amount / risk_per_unit
            requested_notional = quantity * request.price
            notional = requested_notional
            capped_by_buying_power = notional > request.buying_power
            if capped_by_buying_power:
                quantity = request.buying_power / request.price
                notional = quantity * request.price

        max_notional = request.equity * self.max_position_pct
        capped_by_max_position = notional > max_notional
        if capped_by_max_position:
            quantity = max_notional / request.price
            notional = max_notional
        sizing_intent_quantity = quantity
        sizing_intent_notional = notional
        below_minimum = notional < self.minimum_position_notional
        broker_input_quantity = (
            0.0
            if below_minimum
            else apply_pre_broker_quantity_rounding(
                sizing_intent_quantity,
                request.price,
                self.pre_broker_quantity_rounding,
            )
        )
        broker_input_notional = broker_input_quantity * request.price
        rejection_reason = "below_minimum_notional" if below_minimum else None

        if method_used is PositionSizingMethod.RISK_PCT and risk_per_unit is not None:
            effective_sizing_risk_amount = broker_input_quantity * risk_per_unit
        else:
            effective_sizing_risk_amount = broker_input_notional
        effective_sizing_risk_pct = effective_sizing_risk_amount / request.equity
        status = (
            SizingDecisionStatus.REJECTED
            if rejection_reason is not None
            else SizingDecisionStatus.ACCEPTED
        )
        return PositionSizingDecision(
            policy_id=self.policy_id,
            requested_method=requested_method,
            method_used=method_used,
            status=status,
            rejection_reason=rejection_reason,
            fallback_reason=fallback_reason,
            price=request.price,
            equity=request.equity,
            buying_power=request.buying_power,
            stop_loss=request.stop_loss,
            risk_per_unit=risk_per_unit,
            requested_notional_value=requested_notional,
            sizing_intent_quantity=sizing_intent_quantity,
            sizing_intent_notional_value=sizing_intent_notional,
            broker_input_quantity=broker_input_quantity,
            broker_input_notional_value=broker_input_notional,
            effective_sizing_risk_amount=effective_sizing_risk_amount,
            effective_sizing_risk_pct=effective_sizing_risk_pct,
            capped_by_buying_power=capped_by_buying_power,
            capped_by_max_position=capped_by_max_position,
            below_minimum=below_minimum,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the generic policy contract as JSON-safe evidence."""

        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "method": self.method.value,
            "fixed_pct": self.fixed_pct,
            "risk_pct": self.risk_pct,
            "max_position_pct": self.max_position_pct,
            "minimum_position_notional": self.minimum_position_notional,
            "missing_stop_behavior": self.missing_stop_behavior.value,
            "pre_broker_quantity_rounding": self.pre_broker_quantity_rounding.value,
            "exposure_semantics": "fraction_of_current_equity_at_each_entry",
        }


def _rejected_decision(
    request: PositionSizingRequest,
    policy: PositionSizingPolicy,
    *,
    reason: str,
    risk_per_unit: float | None,
) -> PositionSizingDecision:
    return PositionSizingDecision(
        policy_id=policy.policy_id,
        requested_method=policy.method,
        method_used=policy.method,
        status=SizingDecisionStatus.REJECTED,
        rejection_reason=reason,
        fallback_reason=None,
        price=request.price,
        equity=request.equity,
        buying_power=request.buying_power,
        stop_loss=request.stop_loss,
        risk_per_unit=risk_per_unit,
        requested_notional_value=0.0,
        sizing_intent_quantity=0.0,
        sizing_intent_notional_value=0.0,
        broker_input_quantity=0.0,
        broker_input_notional_value=0.0,
        effective_sizing_risk_amount=0.0,
        effective_sizing_risk_pct=0.0,
        capped_by_buying_power=False,
        capped_by_max_position=False,
        below_minimum=False,
    )


__all__ = [
    "MissingStopBehavior",
    "PositionSizingDecision",
    "PositionSizingMethod",
    "PositionSizingPolicy",
    "PositionSizingRequest",
    "PreBrokerQuantityRounding",
    "SizingDecisionStatus",
    "apply_pre_broker_quantity_rounding",
]
