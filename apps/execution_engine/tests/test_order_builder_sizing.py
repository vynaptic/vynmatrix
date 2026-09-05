"""Regression tests for per-binding position-cap propagation into the sizer (N7).

The binding's ``max_position_pct`` is carried in ``risk_caps`` (what the risk guard
enforces), while the position sizer reads the ``sizing`` block. ``_parse_user_config``
must reconcile the two so the sizer never sizes above the risk cap — otherwise a tenant
with a tighter cap is sized to the 0.10 default and then blocked by the risk guard.
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from execution_engine.config import AccountState, BrokerType
from execution_engine.models import ExecutionRequest, OrderCurrencyContext
from execution_engine.order_builder import OrderBuilder
from lib_strategy.signals.signal import Signal, SignalAction


def _req(
    *,
    sizing: dict | None = None,
    risk_caps: dict | None = None,
    profile: dict | None = None,
    currency_context: OrderCurrencyContext | None = None,
) -> ExecutionRequest:
    cfg: dict = {}
    if sizing is not None:
        cfg["sizing"] = sizing
    if risk_caps is not None:
        cfg["risk_caps"] = risk_caps
    return ExecutionRequest(
        user_id="demo_peer",
        profile=profile or {},
        user_strategy_config=cfg,
        signal=Signal(
            strategy_id="test_strategy_alpha_v1",
            strategy_type="indicator",
            symbol="ETH/USD",
            action=SignalAction.LONG,
            confidence=0.9,
        ),
        currency_context=currency_context,
    )


def _sizing_cap(req: ExecutionRequest) -> float:
    return OrderBuilder()._parse_user_config(req).sizing.max_position_pct


def test_binding_risk_cap_flows_into_sizer_when_sizing_unset() -> None:
    # N7: binding max_position_pct=0.05 lives in risk_caps; the sizer must inherit it
    # (not its 0.10 default) so the position is sized DOWN, not risk-guard-blocked.
    assert _sizing_cap(_req(risk_caps={"max_position_pct": 0.05})) == 0.05


def test_sizer_caps_to_min_when_sizing_pref_looser_than_risk_cap() -> None:
    # Explicit sizing pref looser than the risk cap -> the risk cap wins (never size above it).
    assert (
        _sizing_cap(_req(sizing={"max_position_pct": 0.20}, risk_caps={"max_position_pct": 0.05}))
        == 0.05
    )


def test_explicit_tighter_sizing_pref_wins() -> None:
    # Explicit sizing tighter than the risk cap -> the tighter explicit value is kept.
    assert (
        _sizing_cap(_req(sizing={"max_position_pct": 0.03}, risk_caps={"max_position_pct": 0.05}))
        == 0.03
    )


def test_default_cap_preserved_when_no_risk_cap() -> None:
    assert _sizing_cap(_req()) == 0.10


def test_profile_risk_caps_used_as_fallback() -> None:
    assert _sizing_cap(_req(profile={"risk_caps": {"max_position_pct": 0.07}})) == 0.07


def test_eur_account_notional_is_converted_to_usdc_before_quantity() -> None:
    observed_at = datetime(2026, 7, 15, 12, tzinfo=UTC)
    context = OrderCurrencyContext(
        account_currency="EUR",
        settlement_currency="USDC",
        account_to_settlement_rate=Decimal("1.2"),
        requested_at=observed_at,
        observed_at=observed_at,
        source="coinbase_live",
    )
    req = _req(
        sizing={
            "method": "fixed_pct",
            "fixed_pct": 0.02,
            "max_position_pct": 0.10,
            "min_position_size": 1.0,
        },
        currency_context=context,
    )
    account = AccountState(
        broker=BrokerType.PAPER,
        equity=100_000.0,
        available_cash=100_000.0,
        currency="EUR",
    )

    intents = OrderBuilder().build(
        req,
        account=account,
        market_data={"price": 50_000.0},
    )

    assert len(intents) == 1
    # 2% of EUR 100,000 = EUR 2,000; observed EUR/USDC 1.2 makes
    # the settlement notional USDC 2,400 and the BTC quantity 0.048.
    assert intents[0].quantity == 0.048
    assert intents[0].quantity * 50_000.0 == 2_400.0
    assert intents[0].currency_context is context


def test_protective_order_preserves_exact_source_row_provenance() -> None:
    source_ts = datetime(2026, 7, 25, 12, 14, tzinfo=UTC)
    req = _req()
    req.signal = replace(
        req.signal,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        metadata={
            "price_source": "coinbase_live",
            "price_timeframe": "1m",
            "source_price_id": 901,
            "source_content_revision": 4,
            "source_price_ts": source_ts.isoformat(),
        },
    )

    intents = OrderBuilder().build(
        req,
        account=AccountState(
            broker=BrokerType.PAPER,
            equity=100_000.0,
            available_cash=100_000.0,
        ),
        market_data={"price": 100.0},
    )

    assert len(intents) == 2
    assert intents[1].order_type == "bracket"
    assert intents[1].metadata == {
        "parent_signal_id": req.signal.signal_id,
        "reduce_only": True,
        "market_data_source": "coinbase_live",
        "market_data_timeframe": "1m",
        "source_price_id": 901,
        "source_content_revision": 4,
        "source_price_ts": source_ts.isoformat(),
        "purpose": "bracket",
    }


# ── LIVE-BLOCKER: a CLOSE must find the open position even when the broker reports
# it in a different symbol format (live Coinbase "ETH-USD", ledger "ETH/USD") than
# the signal ("ETHUSD"). An exact-string match silently no-ops and the position can
# NEVER be flattened — unbounded risk in live.


def _close_req(symbol: str) -> ExecutionRequest:
    return ExecutionRequest(
        user_id="demo_peer",
        profile={},
        user_strategy_config={},
        signal=Signal(
            strategy_id="test_strategy_alpha_v1",
            strategy_type="indicator",
            symbol=symbol,
            action=SignalAction.CLOSE,
            confidence=0.9,
        ),
    )


def _account(positions: list[dict]) -> AccountState:
    return AccountState(
        broker=BrokerType.PAPER,
        equity=100000.0,
        available_cash=100000.0,
        positions=positions,
    )


def test_close_finds_position_across_symbol_formats() -> None:
    intents = OrderBuilder().build(
        _close_req("ETHUSD"),
        account=_account([{"symbol": "ETH-USD", "side": "long", "quantity": 2.0}]),
        market_data={"price": 2_500.25},
    )
    assert intents, "CLOSE must find the position despite symbol-format difference"
    assert intents[0].side == "SELL"
    assert float(intents[0].quantity) == 2.0
    assert intents[0].metadata["reference_price"] == 2_500.25


def test_close_no_matching_position_is_safe_noop() -> None:
    intents = OrderBuilder().build(
        _close_req("ETHUSD"),
        account=_account([{"symbol": "BTC-USD", "side": "long", "quantity": 1.0}]),
    )
    assert intents == []


def test_close_with_unknown_position_side_fails_closed() -> None:
    intents = OrderBuilder().build(
        _close_req("ETHUSD"),
        account=_account([{"symbol": "ETH-USD", "side": "mystery", "quantity": 2.0}]),
    )
    assert intents == []


@pytest.mark.parametrize(
    ("execution_mode", "contract_type"),
    [("perpetual", "perpetual"), ("futures", "dated")],
)
def test_futures_close_preserves_exact_open_contract_terms(
    execution_mode: str,
    contract_type: str,
) -> None:
    req = _close_req("BTC-USD")
    req.user_strategy_config = {
        "execution_mode": execution_mode,
        "futures": {
            "contract_size": 5,
            "max_leverage": 10,
            "target_leverage": 2,
            "use_perpetual": execution_mode == "perpetual",
        },
    }
    intents = OrderBuilder().build(
        req,
        account=_account(
            [
                {
                    "symbol": "BTC-USD",
                    "side": "long",
                    "quantity": 2,
                    "quantity_unit": "contracts",
                    "contract_multiplier": 5,
                    "leverage": 2,
                    "contract_type": contract_type,
                }
            ]
        ),
    )
    assert len(intents) == 1
    metadata = intents[0].metadata
    assert metadata is not None
    assert metadata["contract_value_model"] == "linear"
    assert metadata["contract_multiplier"] == 5
    assert metadata["leverage"] == 2
    assert metadata["contract_type"] == contract_type
    assert metadata["reduce_only"] is True


def test_futures_close_without_persisted_leverage_fails_closed() -> None:
    req = _close_req("BTC-USD")
    req.user_strategy_config = {
        "execution_mode": "futures",
        "futures": {
            "contract_size": 5,
            "max_leverage": 10,
            "target_leverage": 2,
            "use_perpetual": False,
        },
    }
    intents = OrderBuilder().build(
        req,
        account=_account(
            [
                {
                    "symbol": "BTC-USD",
                    "side": "long",
                    "quantity": 2,
                    "quantity_unit": "contracts",
                    "contract_multiplier": 5,
                }
            ]
        ),
    )
    assert intents == []


def test_futures_close_with_mismatched_contract_type_fails_closed() -> None:
    req = _close_req("BTC-USD")
    req.user_strategy_config = {
        "execution_mode": "futures",
        "futures": {
            "contract_size": 5,
            "max_leverage": 10,
            "target_leverage": 2,
            "use_perpetual": False,
        },
    }
    intents = OrderBuilder().build(
        req,
        account=_account(
            [
                {
                    "symbol": "BTC-USD",
                    "side": "long",
                    "quantity": 2,
                    "quantity_unit": "contracts",
                    "contract_multiplier": 5,
                    "leverage": 2,
                    "contract_type": "perpetual",
                }
            ]
        ),
    )
    assert intents == []
