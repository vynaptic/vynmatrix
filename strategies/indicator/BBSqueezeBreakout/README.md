# BBSqueezeBreakout

Prior low bandwidth percentile followed by a band breakout, optionally confirmed by OBV; close at the middle band.

Defaults and the original source hash are recorded in [config.json](config.json).
Source: `BBSqueezeBreakout.py`, strategy manual section 36. Selection, shared market
applicability, and evidence status: [strategy readiness](../../../docs/STRATEGY_READINESS.md#backtrader-migration).

Consumes complete, chronological OHLC bars; the default configuration consolidates
venue minutes to daily bars. Requires comparable venue-traded volume; OTC FX tick volume is not a substitute.

PercentRank fractions are compared with squeeze_threshold_pct / 100. The source compares fractions directly with 10, disabling its intended squeeze filter. Native OBV replaces TA-Lib plumbing. This strategy has no source-defined protective stop; policies requiring one must reject it.

Trailing rules produce model CLOSE intent at the decision bar close. They do not
claim a broker fill at a historical stop price. Execution, funding/borrow, contract
terms, account sizing, and permission remain downstream responsibilities.
