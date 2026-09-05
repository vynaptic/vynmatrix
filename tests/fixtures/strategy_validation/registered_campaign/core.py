"""Non-deployable signal core for validation-campaign contract tests."""

from __future__ import annotations

from typing import Any

from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy


class RegisteredCampaignCore(PureSignalStrategy):
    """No-signal core used only to exercise shared campaign orchestration."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("strategy_type", "test_fixture")
        super().__init__(**kwargs)

    def initialize(self) -> None:
        """The contract fixture has no mutable indicator state."""

    def on_data(self, state: MarketState) -> None:
        """Consume a bar without expressing a deployable market hypothesis."""

        del state
