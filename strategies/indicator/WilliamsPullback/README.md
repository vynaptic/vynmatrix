# WilliamsPullback

Williams %R recovery aligned with the EMA trend and MACD histogram; close-based ATR trail.

Defaults and the original source hash are recorded in [config.json](config.json).
Source: `WilliamsPullbackStrategy.py`, strategy manual section 242. Selection, shared market
applicability, and evidence status: [strategy readiness](../../../docs/STRATEGY_READINESS.md#backtrader-migration).

Consumes complete, chronological OHLC bars; the default configuration consolidates
venue minutes to daily bars.

The source reads `MACDHistogram[0]`, which is the MACD line, despite manual section
242 specifying the histogram (`MACD - signal`). The port implements the documented
histogram gate. Recorded Coinbase bar 50 demonstrates the difference: the source
MACD is negative while its histogram is positive, so the port rejects that short.

Undefined zero-range Williams values produce no crossover. Protection starts at the signal close; current high/low ratchets are used only for close-based decisions.

Trailing rules produce model CLOSE intent at the decision bar close. They do not
claim a broker fill at a historical stop price. Execution, funding/borrow, contract
terms, account sizing, and permission remain downstream responsibilities.
