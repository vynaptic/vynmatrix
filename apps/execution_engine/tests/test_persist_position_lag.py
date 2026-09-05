"""Post-fill position persistence must survive the broker settlement lag.

Coinbase reflects a fill in the account book only after a few seconds, while persist
runs immediately post-trade. Without recording the just-opened position from the fill
result (and suppressing the empty-snapshot prune), the ledger stays empty →
reconciliation false-phantoms every live position → breaker, and max-open-positions
(which counts the ledger) reads 0.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from execution_engine.execution_persistence import ExecutionPersistence

_persist = ExecutionPersistence._positions_to_persist


def _signal(action: str, symbol: str = "SOLUSD") -> SimpleNamespace:
    return SimpleNamespace(action=SimpleNamespace(value=action), symbol=symbol)


def _result(
    *,
    orders_filled: int,
    qty: float,
    price: float = 80.49,
    execution_mode: str = "spot",
    settlement_currency: str | None = "USD",
) -> SimpleNamespace:
    return SimpleNamespace(
        orders_filled=orders_filled,
        total_quantity=qty,
        average_price=price,
        execution_mode=execution_mode,
        settlement_currency=settlement_currency,
    )


def test_open_fill_with_lagging_empty_snapshot_records_position_and_blocks_prune() -> None:
    positions, allow_prune = _persist([], _signal("LONG"), _result(orders_filled=1, qty=0.069))
    assert allow_prune is False  # never prune a just-opened position
    assert len(positions) == 1
    assert positions[0]["symbol"] == "SOLUSD"
    assert positions[0]["side"] == "long"
    assert positions[0]["quantity"] == Decimal("0.069")
    assert positions[0]["entry_price"] == Decimal("80.49")
    assert positions[0]["quantity_unit"] == "asset"
    assert positions[0]["contract_multiplier"] is None
    assert positions[0]["gross_notional"] == Decimal("5.55381")
    assert positions[0]["notional_currency"] == "USD"


def test_open_fill_when_snapshot_already_has_it_does_not_duplicate() -> None:
    snap = [{"symbol": "SOLUSD", "quantity": 0.069}]
    positions, allow_prune = _persist(snap, _signal("LONG"), _result(orders_filled=1, qty=0.069))
    assert allow_prune is False
    assert len(positions) == 1  # no duplicate row


def test_close_fill_with_empty_snapshot_allows_prune() -> None:
    positions, allow_prune = _persist([], _signal("CLOSE"), _result(orders_filled=1, qty=0.069))
    assert allow_prune is True  # empty book after a close legitimately means flat
    assert positions == []


def test_unfilled_open_does_not_record_and_allows_prune() -> None:
    positions, allow_prune = _persist([], _signal("LONG"), _result(orders_filled=0, qty=0.0))
    assert allow_prune is True
    assert positions == []


def test_derivative_fill_without_snapshot_is_not_valued_by_guessing() -> None:
    positions, allow_prune = _persist(
        [],
        _signal("LONG"),
        _result(
            orders_filled=1,
            qty=2,
            price=500.0,
            execution_mode="futures",
        ),
    )

    assert allow_prune is False
    assert positions == []


def test_asset_fill_requires_settlement_currency() -> None:
    with pytest.raises(ValueError, match="settlement currency"):
        _persist(
            [],
            _signal("LONG"),
            _result(
                orders_filled=1,
                qty=0.069,
                settlement_currency=None,
            ),
        )
