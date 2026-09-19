# ATRBreakout

Prior close-channel breakout gated by ATR expansion; close-based ATR trail or a holding-bar limit.

Defaults and the original source hash are recorded in [config.json](config.json).
Source: `ATRBreakoutStrategy.py`, strategy manual section 30. Selection, shared market
applicability, and evidence status: [strategy readiness](../../../docs/STRATEGY_READINESS.md#backtrader-migration).

Consumes complete, chronological OHLC bars; the default configuration consolidates
venue minutes to daily bars.

The source name max_hold_days counts bars. Entry/model protection is anchored to the decision close; downstream fills remain separate. The prior ATR mean requires 45 default bars.

Trailing rules produce model CLOSE intent at the decision bar close. They do not
claim a broker fill at a historical stop price. Execution, funding/borrow, contract
terms, account sizing, and permission remain downstream responsibilities.
