# Production-equivalent paper pipeline verification

This is the acceptance procedure for a connected local paper run. It records
requirements, not a completed run, inherited certification, deployment, or live
authority. Use an isolated database and dedicated local-paper account; never
point it at a real-money account or a production database.

The canonical proof path is real market data, strategy runtime journal, signal
relay, scoring, transactional outbox, paper execution, fill/accounting, and
feedback. PostgreSQL rows are the primary evidence; redacted logs and metrics
support them.

## Safety boundary

- Use only declared Compose services and images.
- Keep EXECUTION_MODE=paper, RUN_MODE=paper,
  EXECUTION_ENGINE_ALLOW_LIVE=false, and
  EXECUTION_USE_LOCAL_PAPER_BROKER=true.
- Do not insert signals, prices, fills, or broker responses to force a pass.
- Do not put secrets in commands, logs, evidence, or source control.
- Preserve the database and bounded service logs until evidence review; do not
  use down -v as routine teardown.

The canary is SwingHighLowPMO version 1.1.0 for BTC-USDC, ETH-USDC, and
SOL-USDC from coinbase_live one-minute candles consolidated to 15-minute bars.
It is a development-only pipeline canary, permanently excluded from paper
promotion and live trading. A recorded historical 1.0.1 BTC-USDC witness is a
separate artifact; it does not prove that 1.1.0 recreated it.

USQualityCompounder is not part of this canary. Its synchronized portfolio route
needs a separately authorized forward provider, complete panel evidence,
catalogue/session coverage, account reconciliation, and its own E2E proof.

## Composite proof contract

The run has four deliberately separate branches:

| Branch | Required result |
| --- | --- |
| Current-time soak | Newly completed real bars traverse strategy and scoring gates. With require_explicit_scoring_inputs=true, a Swing price-ladder entry stops at that gate; otherwise record the actual result. |
| Historical safety | A real old canonical signal reaches normal execution freshness validation and produces no order or fill. |
| Authorized replay | Existing canonical signals and persisted real candles drive the production local-paper execution, ledger, accounting, reconciliation, and restart path. |
| Failure-mode suites | Isolated PostgreSQL/service tests demonstrate retry, duplicate delivery, DLQ/redrive, authority races, pending-order restart, and feedback concurrency when a naturally timed fault cannot be safely created. |

Never relax forecast, freshness, authority, or signal-age rules to make
historical evidence appear current. Unit and isolated PostgreSQL tests are
prerequisites, not the real-data Docker proof.

## 1. Prepare the runtime

Complete [SETUP.md](../SETUP.md), then use the scoped URLs, keys, paper defaults,
and selectors in [CONFIGURATION.md](CONFIGURATION.md). Use the split layout
unless an approved combined layout is required. Select market-data and FX work,
leave STRATEGY_LIST empty during bootstrap, and configure only the three canary
symbols.

The source config needs 500 complete 15-minute strategy bars. Confirm that at
least 7,500 aligned real one-minute candles are available for each selected
symbol; partial boundaries, gaps, future rows, or relabelled provider data do
not count. The FX worker records observations but does not prove a required
account conversion or a USD/USDC parity assumption.

Before connecting, build and validate the declared checkout:

~~~text
vmdev build libs
vmdev build strategies
vmdev build venvs
vmdev build docker --from-config --tag latest
vmdev audit --strict
vmdev test all
docker compose --env-file .env -f docker/docker-compose.stack.yml config --quiet
~~~

## 2. Bootstrap the owner and activate the narrow canary

Create the private owner document described in
[DATABASE.md](DATABASE.md#installation-and-privilege-stages), then bootstrap:

~~~text
vmdev db bootstrap --owner-config owner.local.yaml
vmdev db status
~~~

Record the checked-out source revision and the migrated Alembic version. The
lifecycle must have completed migration, roles, inactive catalogue registration,
and owner initialization without creating a broker account, binding, or
execution selector.

The owner, local-paper account, exact strategy version, canonical instruments,
feed route, binding scope, account currency, session state, and all current
authority flags must be resolved before strategy activation. Run the narrow
maintenance action only with the actual private maintenance URL:

~~~text
ENVIRONMENT=dev ENV=dev EXECUTION_MODE=paper EXECUTION_ENGINE_ALLOW_LIVE=false vmdev db activate-canary --strategy-id swing_high_low_pmo_v1 --version 1.1.0
~~~

This operation activates only the exact source-matching strategy/version
release. It creates no account, binding, worker selection, promotion manifest,
or general strategy approval.

To claim the strict explicit-input result, use the authenticated backend's
strategy-configuration route to retain require_stop_loss=true and
require_explicit_scoring_inputs=true for the owner and strategy. Those are owner
policy values, not source-strategy settings. Without that policy, record the
actual current-time result instead of expecting a blocked entry.

## 3. Ingest real data and start the strategy

Run the existing bounded backfill inside the worker group:

~~~text
docker compose --env-file .env -f docker/docker-compose.stack.yml exec -T workers python -m scripts.run_platform job backfill --timeout-seconds 3600
~~~

For the combined layout, use application in place of workers. The job must exit
successfully, persist real Coinbase provenance and content revisions, and leave
the supervised market-data process healthy. Repeated job overlap, timeout,
incomplete candle coverage, or a stale tail is incomplete evidence.

Only then set STRATEGY_LIST=SwingHighLowPMO in the private environment and
recreate the existing group:

~~~text
docker compose --env-file .env -f docker/docker-compose.stack.yml up -d --no-deps workers
~~~

There is no indicator Compose profile or separate strategy container. In the
combined layout recreate application and record the wider restart scope.
Warm-up and correction rebuilds must persist state with emissions suppressed.
For each later completed bar, the indicator transaction must persist its state
and watermark, decision, any canonical signal envelope, and at most one
signals.submit outbox event.

## 4. Observe progress, not only health

Capture each group's supervisor status:

~~~text
docker compose --env-file .env -f docker/docker-compose.stack.yml exec -T application python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8090/status").read().decode())'
docker compose --env-file .env -f docker/docker-compose.stack.yml exec -T workers python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8090/status").read().decode())'
~~~

Health proves process liveness. Readiness must also show fresh selected data,
indicator state/lag, scoring outbox progress with no dead letter, execution
reconciliation with no submission_unknown order, pending-paper progress, and a
successful feedback heartbeat. Capture the current values of:

- vm_indicator_signal_backlog_oldest_seconds
- vm_indicator_strategy_lag_seconds
- vm_outbox_oldest_undelivered_age_seconds
- vm_database_pool_checked_out and vm_database_pool_overflow
- execution_pending_orders_total and execution_paper_order_processing_lag_seconds
- execution_reconciliation_initial_complete and
  execution_reconciliation_discovered_partitions

Any readiness regression, exhausted pool budget, dead letter, or unknown broker
submission stops the run. Backend health at its loopback endpoint is management
availability, not trading readiness.

## 5. Prove persistence, safety, and replay

~~~mermaid
flowchart LR
    P[Real price row] --> D[Strategy decision]
    D --> SO[signals.submit outbox event]
    SO --> S[Canonical signal and score]
    S --> EO[execution.commands outbox event]
    EO --> I[Order intent and order]
    I --> E[Execution fill]
    E --> A[Accounting and feedback projections]
~~~

For the current-time branch, retain the decision, accepted or blocked result,
outbox identity, and reason. With the strict explicit-input policy, a real Swing
entry stops at that gate and leaves the economic-order branch unexercised.
Otherwise, record the actual gate and whether that branch was reached.

For historical safety, deliver an eligible old persisted signal through scoring
and show normal execution freshness rejects it with no canonical order or fill.
For the separate replay branch, use the production execution path on one
connected paper account with an exact active binding, non-empty instrument
scope, current authority, no exchange credential row, bounded dates, persisted
real candles, and the already-published exact execution command. Use resolved
control-plane IDs:

~~~text
docker compose --env-file .env -f docker/docker-compose.stack.yml exec -T application sh -c 'DATABASE_URL="$EXECUTION_DATABASE_URL" exec python /app/scripts/replay_canonical_signals.py "$@"' replay --user-id <owner-id> --broker-account-id <paper-account-id> --strategy-id swing_high_low_pmo_v1 --symbols BTC-USDC --start-date 2026-07-10 --end-date 2026-07-16 --timeframe 15m --source coinbase_live --require-minute-data --no-enable-shorting
~~~

The replay refuses an ambiguous, foreign, credential-bearing, inactive,
unbounded, unpublished, or lineage-conflicting route. It retains frozen
historical scoring context and does not relax normal execution freshness
outside the replay path.

Across all exercised branches, retain source price identity/revision/time,
strategy/version, deterministic signal identity, account-scoped decision and
outbox identities, stable client order identity, ledger records, account/currency
provenance, and feedback lineage. There must be no duplicate canonical signal,
order, execution, or accounting projection under retry. Positions and metrics
remain projections; ledger fills are the restart source.

## 6. Prove durable orders and restart behavior

Local-paper stop, stop-limit, limit, protective, and OCO orders must persist
before acknowledgement, consume later committed real bars, retain source
watermarks, apply ohlc-conservative-v1 ordering, and resume with the same client
identity after restart. Missing, stale, revised-out-of-order, or unverifiable
bars do not produce fills.

Restart the existing application group before a later qualifying bar resolves a
replay-created protective order:

~~~text
docker compose --env-file .env -f docker/docker-compose.stack.yml restart application
~~~

Restart workers after a non-empty indicator state exists:

~~~text
docker compose --env-file .env -f docker/docker-compose.stack.yml restart workers
~~~

For a combined layout, restart application and record the larger fault domain.
After either restart, state and watermarks restore, unacknowledged signal
delivery keeps its identity, and no duplicate economics occur.

Do not create another container to imitate an isolated relay outage. Run the
existing bounded failure-mode suites from .venv-dev and label their fixture
evidence separately:

~~~text
python -m pytest apps/scoring_engine/tests/test_outbox_dispatch.py apps/scoring_engine/tests/test_outbox_relay_failure_modes.py tests/test_outbox_store.py tests/test_outbox_failure_modes.py apps/execution_engine/tests/test_execution_freshness.py
~~~

Allow feedback to complete an eligible real-data horizon, restart its existing
group, and prove one advisory-lock-serialized cycle per horizon, deterministic
work order, and no duplicate performance row, tracker movement, or suggestion.

## 7. Revoke authority, reconcile, and decide

Use only the authenticated backend to change authority:

| State | Expected behavior |
| --- | --- |
| Active binding, autopilot, entries, exits | New entry and close may proceed if every other gate passes. |
| Active binding, entries false, exits true | No new exposure; risk-reducing close may proceed. |
| Inactive/disconnected/disabled credential/mismatched route | No broker I/O. |

Execution must re-read the current owner, binding, account, credential,
environment, and route before broker I/O. On application restart, each
discovered account partition reconciles orders, executions, positions, cash, and
open paper orders before entry readiness. Never reset submission_unknown to
pending. DLQ inspection and generation-fenced redrive are exceptional
authenticated actions described in [RUNBOOK.md](RUNBOOK.md).

For every fill, reconcile quantity, price, fee, cash/equity, P&L, FIFO lots,
point-in-time FX path, positions, and metrics against the canonical ledger.
Missing FX provenance fails closed; USD/USDC parity is never assumed. Feedback
may evaluate non-executed signals, but must not label them as executed outcomes.

## 8. Evidence and teardown

Keep one immutable, redacted evidence set for the exact image, configuration,
account, binding, broker route, and run window. It must include current
authority, real-data coverage, scoring inputs, replay/restart results,
reconciliation, readiness/metrics, and any separately labeled fixture-suite
output. It must not contain broker or database secrets.

The canary only passes as platform evidence when build/audit/test/migration and
declared-Compose checks succeed; real-data evidence is attributable and
duplicate-free; all exercised restart and reconciliation obligations pass; no
dead letter, unresolved submission, authority conflict, or unauthorized broker
call remains; and P&L/FX reconcile. A branch that natural data did not exercise
must be recorded as unexercised, not inferred.

Swing remains permanently ineligible for a paper-promotion manifest. Any other
strategy needs independent readiness, evidence, and account/route approval.

First revoke the canary binding and record the end of the authority window. Then
stop the declared stack without deleting its evidence:

~~~text
vmdev db stop
~~~

Archive reviewed, redacted evidence outside the repository. Deleting a volume
is a separate explicit operator decision.
