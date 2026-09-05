"""Test-owned equal-weight equity basket core — exercises the portfolio simulator.

This is a TEST FIXTURE, not a strategy. It is deliberately not packaged, not
listed in any catalogue, and never selectable by ``STRATEGY_LIST``.

It exists because the equity portfolio simulator's rule-level suite
(``tools/dev_cli/tests/validation/test_equity_portfolio.py``) needs a
deterministic multi-symbol core that opens an equal-weight position in every
fed symbol and re-opens names that return after a quarter-scale feed gap. That
coverage is independent of any particular strategy's investment thesis, so it
outlives the strategies that happen to share the shape.

Adopted from the retired ``LiquidityLeadersBasket`` core when that strategy was
discarded (2026-08-13) after a 26-year point-in-time test found it lost to SPY
total return. The same pattern was used when the scalpers were retired and their
pipeline coverage moved to ``PipelineExerciser``: keep the exerciser, drop the
strategy.

Division of labour, unchanged: the feeder owns universe membership; this core
opens equal-weight positions in whatever it is fed. Weight is equal-at-entry and
drifts until exit; there is no intra-quarter rebalancing.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy

_HORIZON_DAYS = 63.0
_MIN_REENTRY_GAP_SESSIONS = 5


class EquityBasketExerciserCore(PureSignalStrategy):
    """Equal-weight long book over whatever universe the feeder supplies."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            strategy_id=kwargs.pop("strategy_id", "equity_basket_exerciser_v1"),
            strategy_type="indicator",
            config=kwargs.pop("config", {}),
            emitter=kwargs.pop("emitter", None),
        )
        self._last_session: dict[str, date] = {}

    def initialize(self) -> None:
        cfg = self.config
        self.basket_size = int(cfg.get("basket_size", 25))
        self.reentry_gap_sessions = int(cfg.get("reentry_gap_sessions", 30))
        self._strategy_version = self.config.get("strategy_version")
        if self.basket_size < 1:
            msg = "basket_size must be >= 1"
            raise ValueError(msg)
        if self.reentry_gap_sessions < _MIN_REENTRY_GAP_SESSIONS:
            msg = "reentry_gap_sessions must be >= 5 (quarter-scale gaps, not holidays)"
            raise ValueError(msg)
        self._last_session = {}
        self.warmup_bars_needed = 1

    def on_data(self, state: MarketState) -> None:
        if self._bootstrapping:
            # Emission is suppressed during history preloads, so recording a
            # position here would create a phantom hold that permanently
            # blocks the real entry on the first live bar.
            return
        symbol = state.symbol
        session = state.timestamp.date()
        sym_state = self.state_for(symbol)
        previous = self._last_session.get(symbol)
        self._last_session[symbol] = session

        if sym_state.position == 1:
            # A quarter-scale feed gap means the harness rotated us out and
            # the name has re-entered the universe: reset and re-open below.
            if previous is not None and (session - previous).days > self.reentry_gap_sessions:
                sym_state.position = 0
                sym_state.entry_price = None
            else:
                return

        self.emit_long(
            symbol=symbol,
            entry_price=state.close,
            confidence=1.0,
            timestamp=state.timestamp,
            horizon="position",
            horizon_days=_HORIZON_DAYS,
            size_hint=1.0 / self.basket_size,
            strategy_version=self._strategy_version,
        )
        sym_state.position = 1
        sym_state.entry_price = state.close
        sym_state.entry_time = state.timestamp
