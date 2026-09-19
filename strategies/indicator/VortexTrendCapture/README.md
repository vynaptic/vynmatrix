# VortexTrendCapture

Vortex crossover aligned with SMA trend and an ATR/price ceiling; close-based high/low ATR ratchet.

Defaults and the original source hash are recorded in [config.json](config.json).
Source: `VortexTrendCaptureStrategy.py`, strategy manual section 233. Selection, shared market
applicability, and evidence status: [strategy readiness](../../../docs/STRATEGY_READINESS.md#backtrader-migration).

Consumes complete, chronological OHLC bars; the default configuration consolidates
venue minutes to daily bars.

Uses the canonical Vortex, SMA, and ATR implementations. Crossover retains the previous nonzero difference through ties. A zero-range window clears stale Vortex values. Protection starts at the decision
close rather than an assumed fill.

Trailing rules produce model CLOSE intent at the decision bar close. They do not
claim a broker fill at a historical stop price. Execution, funding/borrow, contract
terms, account sizing, and permission remain downstream responsibilities.
