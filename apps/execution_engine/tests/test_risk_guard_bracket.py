"""RiskGuard must treat the OCO `bracket` exit as reduce-only (not new exposure).

Regression for the Gap-N follow-on: introducing order_type/purpose="bracket" without
adding it to the reduce-only set made _estimate_notional double-count the position
(entry + bracket) and trip the max-position cap at 2x.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from execution_engine.models import OrderIntent
from execution_engine.risk_guard import RiskGuard

PRICE = 81.0
QTY = 0.07


def _entry() -> OrderIntent:
    return OrderIntent("coinbase", "SOLUSD", "BUY", QTY, "market", metadata={})


def _bracket() -> OrderIntent:
    return OrderIntent(
        "coinbase",
        "SOLUSD",
        "SELL",
        QTY,
        "bracket",
        stop_price=80.0,
        limit_price=90.0,
        metadata={"purpose": "bracket"},
    )


def test_bracket_exit_excluded_from_exposure() -> None:
    guard = RiskGuard()
    signal = SimpleNamespace(entry_price=PRICE)
    notional = guard._estimate_notional([_entry(), _bracket()], signal, {"price": PRICE})
    # only the entry counts; the bracket is a reduce-only exit
    assert notional == pytest.approx(QTY * PRICE)


def test_entry_only_matches_entry_plus_bracket() -> None:
    guard = RiskGuard()
    signal = SimpleNamespace(entry_price=PRICE)
    entry_only = guard._estimate_notional([_entry()], signal, {"price": PRICE})
    with_bracket = guard._estimate_notional([_entry(), _bracket()], signal, {"price": PRICE})
    assert entry_only == pytest.approx(with_bracket)
