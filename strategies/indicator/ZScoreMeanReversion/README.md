# ZScoreMeanReversion

Current-window price z-score extremes under an ADX range gate; mean or percentage-trail exit.

Defaults and the original source hash are recorded in [config.json](config.json).
Source: `ZScoreMeanReversionStrategy.py`, strategy manual section 245. Selection, shared market
applicability, and evidence status: [strategy readiness](../../../docs/STRATEGY_READINESS.md#backtrader-migration).

Consumes complete, chronological OHLC bars; the default configuration consolidates
venue minutes to daily bars.

Retains the executable 2% default, not the contradictory 3% comment. A completed exit cannot create another opposing stop. Flat variance creates no entry; safe RSI/ADX division limits are documented in shared indicators.

Trailing rules produce model CLOSE intent at the decision bar close. They do not
claim a broker fill at a historical stop price. Execution, funding/borrow, contract
terms, account sizing, and permission remain downstream responsibilities.
