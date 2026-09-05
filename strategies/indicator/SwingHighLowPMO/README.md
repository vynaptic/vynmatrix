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

Default universe: `BTCUSDC`. Other pairs are opt-in and require complete,
quality-gated Coinbase minute history before use.

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
  "universe": "BTCUSDC",
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

The strategy runs inside the `indicator-runner` service (`runner_kind:
signal_worker`), fed from the `prices` table. Populate `prices` with the
`market_data_ingestor`, then run the worker in paper mode via the local stack:

```bash
# Warm real history in a separate, deficit-aware process, then start 60s polling.
INGESTOR_SYMBOLS=BTC-USDC INGESTOR_BACKFILL_DAYS=150 \
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  --profile backfill run --rm --no-deps market-data-backfill
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  up -d market-data-ingestor

# Run only this opt-in indicator strategy
STRATEGY_LIST=SwingHighLowPMO RUN_MODE=paper \
docker compose --env-file .env -f docker/docker-compose.stack.yml --profile indicator up -d \
  indicator-runner
```

The `SignalWorker` LISTENs on `new_market_data`, consolidates 1-minute bars to
15-minute bars, calls `core.on_data()`, and emits signals to the scoring engine.

## Signal Emission

The strategy emits canonical signals to the scoring API (the worker's target is
set via `SIGNAL_API_URL`):

- `LONG`: open a long position intent.
- `CLOSE`: flatten the current long position.

It never emits `SHORT`; inherited short emission is explicitly rejected by the strategy core.

## Validation

Focused tests:

```bash
pytest tests/test_swing_high_low_pmo_public_replay.py -q
pytest tests/test_http_signal_emitter_capture.py -q
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

Run the profile-gated `market-data-backfill` one-shot if a configured symbol
lacks history; use the live `market_data_ingestor` only for the short startup
window and continuous polling.
