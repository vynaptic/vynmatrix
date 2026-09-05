# Swing High/Low + PMO

SwingHighLowPMO is a long-only crypto signal strategy and the version 1.1.0
development pipeline canary. Its authority and evidence boundaries are in
[Strategy Readiness](../../../docs/STRATEGY_READINESS.md) and the
[paper verification guide](../../../docs/E2E_VERIFICATION_GUIDE.md). It has no
paper-promotion or live-trading eligibility.

## Strategy contract

The SignalWorker runs core.py as a PureSignalStrategy using persisted prices. It
emits canonical LONG and CLOSE signals only and never calls a broker order API.
It evaluates BTCUSDC, ETHUSDC, and SOLUSDC independently from complete Coinbase
one-minute history consolidated to 15-minute bars.

Long entry requires all of the following:

1. PMO crosses above its signal line.
2. The close is above the EMA trend filter.
3. The close breaks the latest swing high.
4. A latest swing low exists for stop placement.
5. The bar is within the configured trading window.

It exits a virtual long on a swing-low stop, configured risk/reward target, or
the end of the trading window. Negative internal position state is invalid.

## Source configuration

| Parameter | Value |
| --- | ---: |
| PMO first/second/signal lengths | 35 / 20 / 10 |
| EMA period | 50 |
| Swing length | 5 |
| Risk/reward ratio | 1.5 |
| Maximum swing age | 20 bars |
| Maximum stop distance | 10% |
| Trading hours | 07:00–23:00 UTC |
| Source/consolidation | coinbase_live one minute to 15 minutes |
| Warm-up | 500 completed 15-minute bars |

The full source configuration is [config.json](config.json). Binding, sizing,
broker route, and execution authority are downstream control-plane decisions,
not strategy-core settings.

## Signal and safety behavior

LONG carries entry, stop, target, and confidence. The current normalization
labels those inputs price_ladder rather than explicit expected return/predicted
risk. If the owner policy requires explicit scoring inputs, an entry is blocked
before an economic order. Otherwise record the actual policy outcome; do not
invent forecasts or claim a fill.

The strategy starts only when STRATEGY_LIST selects it, the exact registered
version is active under maintenance authority, the owner/account/binding route
is current, and real warm-up data is complete. Source shipping, a database
binding, or a selector alone is never enough.

## Diagnostics

If no signals appear, confirm:

- complete source bars exist for every configured symbol;
- enough history exists for PMO, EMA, and swing warm-up;
- timestamps fall inside the configured trading hours; and
- the current market does satisfy the crossover and breakout conditions.

Use the E2E guide for backfill, worker recreation, readiness, and recorded-data
evidence. Do not use historical catch-up signals as current trading authority.
