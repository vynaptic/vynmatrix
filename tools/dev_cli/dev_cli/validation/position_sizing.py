"""Validation-only composition of domain sizing and Coinbase order rules."""

from __future__ import annotations

import math
from dataclasses import dataclass

from lib_infrastructure.brokers.adapters.coinbase_order_sizing import (
    BrokerMarketOrderSize,
    MarketOrderPrecision,
    MarketOrderSide,
    translate_coinbase_market_order_size,
)
from lib_strategy.position_sizing import (
    PositionSizingDecision,
    PositionSizingMethod,
    PositionSizingPolicy,
)

SIZING_EVIDENCE_SCHEMA = "vynmatrix.position-sizing-evidence-v3"


@dataclass(frozen=True, slots=True)
class CoinbaseSizingRules:
    """Frozen provider rules applied after the shared sizing kernel."""

    base_increment: str
    quote_increment: str
    minimum_order_notional: float
    source: str

    def __post_init__(self) -> None:
        precision = self.market_order_precision
        object.__setattr__(self, "base_increment", precision.base_increment)
        object.__setattr__(self, "quote_increment", precision.quote_increment)
        if not math.isfinite(self.minimum_order_notional) or self.minimum_order_notional <= 0.0:
            message = "minimum_order_notional must be finite and positive"
            raise ValueError(message)

    @property
    def market_order_precision(self) -> MarketOrderPrecision:
        return MarketOrderPrecision(
            base_increment=self.base_increment,
            quote_increment=self.quote_increment,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "base_increment": self.base_increment,
            "quote_increment": self.quote_increment,
            "minimum_order_notional": self.minimum_order_notional,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class CoinbaseSizingTranslation:
    """Provider translation joined to, but separate from, a domain decision."""

    market_order_size: BrokerMarketOrderSize | None
    executable_quantity_at_reference: float
    executable_notional_at_reference: float
    effective_sizing_risk_amount: float
    effective_sizing_risk_pct: float
    rejection_reason: str | None

    @property
    def accepted(self) -> bool:
        return self.rejection_reason is None

    def to_dict(self) -> dict[str, object]:
        return {
            "market_order_size": (
                self.market_order_size.to_dict() if self.market_order_size is not None else None
            ),
            "executable_quantity_at_reference": self.executable_quantity_at_reference,
            "executable_notional_at_reference": self.executable_notional_at_reference,
            "effective_sizing_risk_amount": self.effective_sizing_risk_amount,
            "effective_sizing_risk_pct": self.effective_sizing_risk_pct,
            "rejection_reason": self.rejection_reason,
        }


def validate_sizing_boundary(
    policy: PositionSizingPolicy | None,
    coinbase_rules: CoinbaseSizingRules | None,
) -> None:
    """Validate the generic-policy/provider-rules composition once."""

    if policy is not None and not isinstance(policy, PositionSizingPolicy):
        message = "sizing_policy must be a PositionSizingPolicy"
        raise TypeError(message)
    if coinbase_rules is not None and not isinstance(coinbase_rules, CoinbaseSizingRules):
        message = "coinbase_sizing_rules must be a CoinbaseSizingRules"
        raise TypeError(message)
    if coinbase_rules is not None and policy is None:
        message = "coinbase_sizing_rules requires a sizing_policy"
        raise ValueError(message)
    if (
        coinbase_rules is not None
        and policy is not None
        and coinbase_rules.minimum_order_notional != policy.minimum_position_notional
    ):
        message = "Coinbase and domain minimum order notionals must match"
        raise ValueError(message)


def apply_coinbase_sizing_rules(
    decision: PositionSizingDecision,
    *,
    side: MarketOrderSide,
    rules: CoinbaseSizingRules,
) -> CoinbaseSizingTranslation:
    """Translate one accepted domain quantity and enforce frozen provider rules."""

    if not decision.accepted:
        return CoinbaseSizingTranslation(
            market_order_size=None,
            executable_quantity_at_reference=0.0,
            executable_notional_at_reference=0.0,
            effective_sizing_risk_amount=0.0,
            effective_sizing_risk_pct=0.0,
            rejection_reason=decision.rejection_reason,
        )
    market_size = translate_coinbase_market_order_size(
        base_quantity=decision.broker_input_quantity,
        side=side,
        precision=rules.market_order_precision,
        reference_price=decision.price,
    )
    quantity = market_size.base_quantity_at_fill(decision.price)
    notional = market_size.quote_notional_at_fill(decision.price)
    rejection_reason: str | None = None
    if quantity <= 0.0:
        rejection_reason = "quantized_to_zero"
    elif notional < rules.minimum_order_notional:
        rejection_reason = "below_minimum_after_precision"
    if rejection_reason is not None:
        quantity = 0.0
        notional = 0.0
    if decision.method_used is PositionSizingMethod.RISK_PCT and decision.risk_per_unit is not None:
        effective_risk = quantity * decision.risk_per_unit
    else:
        effective_risk = notional
    return CoinbaseSizingTranslation(
        market_order_size=market_size,
        executable_quantity_at_reference=quantity,
        executable_notional_at_reference=notional,
        effective_sizing_risk_amount=effective_risk,
        effective_sizing_risk_pct=effective_risk / decision.equity,
        rejection_reason=rejection_reason,
    )


__all__ = [
    "SIZING_EVIDENCE_SCHEMA",
    "CoinbaseSizingRules",
    "CoinbaseSizingTranslation",
    "apply_coinbase_sizing_rules",
    "validate_sizing_boundary",
]
