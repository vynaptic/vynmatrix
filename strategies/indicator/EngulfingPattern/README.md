# EngulfingPattern

Strict real-body engulfing confirmed by RSI extremes; percentage trailing protection.

Defaults and the original source hash are recorded in [config.json](config.json).
Source: `EngulfingPatternStrategy.py`, strategy manual section 64. Selection, shared market
applicability, and evidence status: [strategy readiness](../../../docs/STRATEGY_READINESS.md#backtrader-migration).

Consumes complete, chronological OHLC bars; the default configuration consolidates
venue minutes to daily bars.

Equal body boundaries do not engulf. Native RSI defines monotone/flat limits instead of raising division errors. The
source creates an opposite trailing stop after every completed order, including
protective exits; those stops can reopen exposure without a new pattern. Native
protection closes model exposure and waits for a subsequent valid entry. Recorded
daily witnesses and restart coverage are linked from strategy readiness.

Trailing rules produce model CLOSE intent at the decision bar close. They do not
claim a broker fill at a historical stop price. Execution, funding/borrow, contract
terms, account sizing, and permission remain downstream responsibilities.
