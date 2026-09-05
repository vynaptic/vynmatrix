# Swing High/Low + PMO Strategy

This strategy documentation is inherited design/research context. vynmatrix has
no transferred paper approval, live certification, account, or deployment. See
[the source readiness inventory](../../../docs/STRATEGY_READINESS.md).

Long-only crypto momentum strategy combining Price Momentum Oscillator, EMA trend filtering,
and swing high/low breakout confirmation.

## Overview

`SwingHighLowPMO` is a `PureSignalStrategy` (`core.py`) run by the `SignalWorker`
runtime, fed from the `prices` table via Postgres LISTEN/NOTIFY. It emits canonical
`LONG` and `CLOSE` signals only; it does not place broker orders directly and it does
not emit `SHORT` signals.

The current `1.1.0` universe is `BTCUSDC,ETHUSDC,SOLUSDC`. Every selected pair
requires complete, quality-gated Coinbase minute history before use.

## Trading Logic

Indicators:

- PMO: momentum crossover indicator using `pmo_first_length`, `pmo_second_length`, and
  `pmo_signal_length`.
- EMA: trend filter using `ema_period`.
- Swing High/Low: pivot-based support/resistance using `swing_length`.

Long entry conditions:

1. PMO crosses above its signal line.
2. Close price is above EMA.
3. Close price breaks above the latest swing high.
4. A latest swing low exists for stop placement.
5. Bar timestamp is inside the configured trading window.

Long exit conditions:

1. Stop loss: close price is at or below the entry swing-low stop.
2. Take profit: close price reaches `entry + risk * risk_reward_ratio`.
3. Time exit: an open long is closed outside the configured trading window.

The strategy keeps per-symbol virtual position state so each configured crypto symbol can
be evaluated independently. Any negative internal position state is rejected at runtime as
invalid for this strategy.

## Configuration

Core parameters live under `parameters` in `config.json` (`runner_kind` is
`signal_worker`):

```json
{
  "universe": "BTCUSDC,ETHUSDC,SOLUSDC",
  "pmo_first_length": "35",
  "pmo_second_length": "20",
  "pmo_signal_length": "10",
  "ema_period": "50",
  "swing_length": "5",
  "risk_reward_ratio": "1.5",
  "max_swing_age_bars": "20",
  "max_stop_distance_pct": "0.10",
  "trading_start_hour": "7",
  "trading_end_hour": "23"
}
```

Bar consolidation is configured once under `market_data.consolidation_minutes`.
Final position sizing and broker routing are controlled by user bindings and the
downstream execution service, not by this signal core.

## Local Run (paper mode)

The indicator child runs inside `workers` in the three-container layout, or
`application` in the two-container `all` layout, using the declared
`vynmatrix/platform` image. Follow the canonical
[paper verification procedure](../../../docs/E2E_VERIFICATION_GUIDE.md) for
bootstrap, explicit owner/account onboarding, exact development-canary activation,
and bounded binding authority. Keep `EXECUTION_MODE=paper`,
`EXECUTION_ENGINE_ALLOW_LIVE=false`, and both stop-loss and explicit-scoring-input
requirements enabled. Presence in the image or a binding row grants no release
eligibility. This development-only canary is excluded from paper promotion and
live trading.

Begin with an empty `STRATEGY_LIST`, `PLATFORM_WORKERS=market-data,fx`, and all
three configured Coinbase pairs. Set an explicit bounded recent-history window
in the private `.env`; verify 500 complete `15m` bootstrap bars formed from 7,500
aligned real `1m` candles per pair. Run the existing backfill job inside the
running worker group:

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  exec -T workers python -m scripts.run_platform job backfill --timeout-seconds 3600
```

After verifying history and the authority prerequisites, set
`STRATEGY_LIST=SwingHighLowPMO` in `.env` and recreate the existing worker group:

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  up -d --no-deps workers
```

In the combined layout, target `application` for both commands. Recreating it
also restarts the APIs. No step starts another service or container.

The `SignalWorker` LISTENs on `new_market_data`, consolidates 1-minute bars to
15-minute bars, calls `core.on_data()`, and emits signals to the scoring engine.

## Signal Emission

The strategy emits canonical signals to the scoring API (the worker's target is
set via `SIGNAL_API_URL`):

- `LONG`: open a long position intent.
- `CLOSE`: flatten the current long position.

It never emits `SHORT`; inherited short emission is explicitly rejected by the strategy core.
Entries carry entry/stop/target prices and confidence, without explicit expected
return or predicted risk. Normalization labels these inputs `price_ladder`;
`require_explicit_scoring_inputs=true` therefore blocks their execution. Preserve
that gate and report the blocked branch accurately; do not invent forecasts or
claim an economic order without observed evidence.

## Validation

Focused tests:

```bash
python -m pytest tests/test_swing_high_low_pmo_public_replay.py -q
python -m pytest tests/test_http_signal_emitter_capture.py -q
```

Build checks:

```bash
make build-wheels
make build-venvs
make build-docker
```

## Troubleshooting

If no signals are emitted:

- Confirm enough bars exist for PMO, EMA, and swing warmup.
- Confirm bars are inside `trading_start_hour` and `trading_end_hour`.
- Confirm the `prices` table has data for every configured symbol.
- Confirm market conditions produce both PMO crossovers and swing-high breakouts.

If the worker has no bars to consolidate, confirm the `prices` table is being
populated:

```sql
SELECT i.canonical, count(*), max(p.ts)
FROM prices AS p
JOIN instruments AS i USING (instr_id)
GROUP BY i.canonical;
```

Run the bounded `scripts.run_platform job backfill` invocation above if a
configured symbol lacks history. The supervised market-data child supplies its
short startup window and continuous polling. Historical replay uses a separately
bounded recorded window and does not authorize historical catch-up emissions.
