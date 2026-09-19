# HurstRegime

Hurst-gated MACD trend cross or slow-stochastic reversion cross; close-based ATR protection.

Defaults and the original source hash are recorded in [config.json](config.json).
Source: `HurstRegimeStrategy.py`, strategy manual section 90. Selection, shared market
applicability, and evidence status: [strategy readiness](../../../docs/STRATEGY_READINESS.md#backtrader-migration).

Consumes complete, chronological OHLC bars; the default configuration consolidates
venue minutes to daily bars.

Hurst exactly follows Backtrader 1.9.78.123 lags 2..period//2-1 and twice the log-log slope of sqrt(population stddev). Degenerate Hurst is unavailable. Stochastic uses both smoothing stages; default combined crossover warm-up is 35 bars. The source pending-order no-op has no native counterpart.

Protection starts at the signal close and retains its bar extreme. The source
initializes protection from the later fill bar instead; this explains the first
recorded exit difference (native bar 554, source bar 559).

Trailing rules produce model CLOSE intent at the decision bar close. They do not
claim a broker fill at a historical stop price. Execution, funding/borrow, contract
terms, account sizing, and permission remain downstream responsibilities.
