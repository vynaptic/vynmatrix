# VolatilityClusteringReversion

Fade net movement after an exact high-volatility streak when aligned with the longer SMA trend.

Defaults and the original source hash are recorded in [config.json](config.json).
Source: `VolatilityClusteringReversion.py`, strategy manual section 211. Selection, shared market
applicability, and evidence status: [strategy readiness](../../../docs/STRATEGY_READINESS.md#backtrader-migration).

Consumes complete, chronological OHLC bars; the default configuration consolidates
venue minutes to daily bars.

Uses prior protection before a bar-close ratchet, correcting same-bar high/low look-ahead. Model entry uses decision close, not the source’s current-open assumption. Days parameters count bars; annualization must match the chosen calendar (its common scale cancels from the cluster comparison).

Trailing rules produce model CLOSE intent at the decision bar close. They do not
claim a broker fill at a historical stop price. Execution, funding/borrow, contract
terms, account sizing, and permission remain downstream responsibilities.
