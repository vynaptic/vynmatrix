# TimeDecayAdaptiveEMA

Price crossover of a volatility-adaptive exponential filter; trailing distance fixed from entry ATR.

Defaults and the original source hash are recorded in [config.json](config.json).
Source: `TimeDecayAdaptiveEMAStrategy.py`, strategy manual section 190. Selection, shared market
applicability, and evidence status: [strategy readiness](../../../docs/STRATEGY_READINESS.md#backtrader-migration).

Consumes complete, chronological OHLC bars; the default configuration consolidates
venue minutes to daily bars.

The executable source uses current volatility EWMA, despite its lagged comment. The port preserves that causal choice, updates the filter while positioned, requires a full return window, and removes synthetic fallback volatility and broker-account sizing.

The source never clears its entry-order handle: after the first completed entry,
`if self.order` blocks all later decisions and the intended protective stop. The
port maintains protection and permits subsequent entries, consistent with manual
section 190. Source decision counts are therefore not an equality target.

Trailing rules produce model CLOSE intent at the decision bar close. They do not
claim a broker fill at a historical stop price. Execution, funding/borrow, contract
terms, account sizing, and permission remain downstream responsibilities.
