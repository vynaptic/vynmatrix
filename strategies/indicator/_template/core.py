"""Strategy template core — copy this directory to start a new strategy.

This scaffold is NON-DEPLOYABLE by design (``enabled: false`` in
``config.json`` and ``readiness: STATIC_REVIEW_ONLY``). It exists so a new
strategy author never copy-pastes a live strategy: the boilerplate that must
be identical everywhere (string-parameter coercion, deterministic signal
identity via ``timestamp=state.timestamp``, warmup declaration) lives here
once, reviewed.

Checklist for a real strategy (see CLAUDE.md "Adding a New Strategy"):
1. Copy ``strategies/indicator/_template`` to ``strategies/indicator/<Name>``.
2. Rename the class, set ``strategy_id``/``strategy_version`` in config.json,
   and fill in ``parameters`` + ``market_data`` for your feed.
3. Implement ``initialize()`` and ``on_data()`` — emit canonical signals via
   ``emit_long/emit_short/emit_close``; NEVER call broker order APIs.
4. Write rule-level tests beside the core (see ``tests/test_core.py``).
5. Follow the strategy add/remove lockstep checklist before any deployment.
"""

from __future__ import annotations

from typing import Any

from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy


class StrategyNameCore(PureSignalStrategy):
    """One-line description of the edge this strategy expresses."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            strategy_id=kwargs.pop("strategy_id", "strategy_name_v1"),
            strategy_type="indicator",
            config=kwargs.pop("config", {}),
            emitter=kwargs.pop("emitter", None),
        )

    def initialize(self) -> None:
        """Coerce string config parameters ONCE, with validation.

        ``config.json`` parameter values arrive as strings; coerce and
        validate them here so ``on_data`` stays pure rule logic.
        """
        cfg = self.config
        self.period = int(cfg.get("period", 14))
        self.stop_loss_pct = float(cfg.get("stop_loss_pct", 0.02))
        self._strategy_version = cfg.get("strategy_version")
        if self.period < 1:
            msg = "period must be >= 1"
            raise ValueError(msg)
        if not 0 < self.stop_loss_pct < 1:
            msg = "stop_loss_pct must be a fraction in (0, 1)"
            raise ValueError(msg)
        # Warmup must cover every indicator's longest lookback.
        self.warmup_bars_needed = self.period

    def on_data(self, state: MarketState) -> None:
        """Consume one consolidated bar for one symbol; emit canonical signals.

        ALWAYS pass ``timestamp=state.timestamp`` so the deterministic
        ``external_signal_id`` is derived from the bar's own time — this is
        what makes redelivery and replay idempotent end to end.
        """
        # Example shape (replace with real rule logic):
        # if self._should_enter(state):
        #     self.emit_long(
        #         symbol=state.symbol,
        #         entry_price=state.close,
        #         stop_loss=state.close * (1 - self.stop_loss_pct),
        #         timestamp=state.timestamp,
        #     )
        # elif self._should_exit(state):
        #     self.emit_close(
        #         symbol=state.symbol,
        #         exit_price=state.close,
        #         timestamp=state.timestamp,
        #     )
