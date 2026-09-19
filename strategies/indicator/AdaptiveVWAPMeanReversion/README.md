# AdaptiveVWAPMeanReversion

Rolling close-volume VWAP, rolling squared VWAP residuals, RSI/ADX entry gates, and tightening ATR protection.

Defaults and the original source hash are recorded in [config.json](config.json).
Source: `AdaptiveVWAPMeanReversionStrategy.py`, strategy manual section 12. Selection, shared market
applicability, and evidence status: [strategy readiness](../../../docs/STRATEGY_READINESS.md#backtrader-migration).

Consumes complete, chronological OHLC bars; the default configuration consolidates
venue minutes to daily bars. Requires comparable venue-traded volume; OTC FX tick volume is not a substitute.

Executable defaults override the stale docstring: 1-sigma entries, ADX 30/35, RSI 45/55. Nested VWAP residual warm-up is 39 bars. The intended initial 2.5-ATR stop is installed immediately; its source branch is unreachable. Zero aggregate volume is unavailable, not replaced by close; existing protective
stops remain active while VWAP is unavailable.

Trailing rules produce model CLOSE intent at the decision bar close. They do not
claim a broker fill at a historical stop price. Execution, funding/borrow, contract
terms, account sizing, and permission remain downstream responsibilities.
