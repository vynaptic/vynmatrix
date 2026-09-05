# Operational Reference (Local Paper Scope)

This inherited operational reference describes code paths and evidence requirements.
It does not attest to a running deployment or supply broker certification. The
vynmatrix migration authorizes local paper verification only; do not deploy,
write live-authority markers, clear live halts, or change live gates. External
provider details below are dated references and must be rechecked before future use.

This runbook covers the Coinbase spot launch path:
`indicator_runner -> scoring_engine -> execution_engine -> feedback_loop_engine`, plus
the primary `market_data_ingestor` and independent `fx-rate-ingestor`.

## Launch Gates

- Review the [local Docker and release boundary](DEPLOYMENT.md). Image publication
  is manual and opt-in; a tag push is not a deployment.
- Keep local services on the intended development network. No cloud host is
  provisioned or approved by this migration.
- Confirm `API_KEY` is set for service-to-service requests in production.
- Confirm `ADMIN_API_KEY` is set before using admin endpoints.
- Confirm `/health`, `/ready`, `/live`, and `/metrics` are reachable for API services.
- Treat `/ready` as a progress gate, not a process check. Defaults are
  `SCORING_OUTBOX_MAX_AGE_SECONDS=300`,
  `INDICATOR_MAX_SIGNAL_BACKLOG_AGE_SECONDS=300`,
  `INDICATOR_MAX_STRATEGY_LAG_SECONDS=300`, and
  `EXECUTION_PAPER_ORDER_MAX_LAG_SECONDS=300`. Do not widen one during an
  incident without identifying the stalled partition.
- Confirm `fx-rate-ingestor` is supervised independently, and its internal
  `http://localhost:8004/ready` probe is green. This process must have fresh ECB
  and Coinbase USDC-EUR observations before cross-currency execution/NAV.
- Confirm the Coinbase sandbox smoke suite has passed with real sandbox credentials.
- Treat certification-marker checks as code contracts only; no live authority is supplied.
- The local `feedback-loop-engine` container repeats one-shot `evaluate`
  invocations. Inspect database heartbeats for progress; no external systemd
  timer or production feedback host is configured here.

## Broker Certification Order

Certify broker integrations in this fixed order:

1. Coinbase
2. Interactive Brokers
3. Deribit
4. Saxo Bank
5. Zerodha
6. Delta Exchange

Each broker advances independently through adapter contract tests, authenticated
non-ordering connectivity, broker-provided sandbox/test execution when
available, reconciliation, and an account-scoped paper soak before live
authority is considered. Do not infer certification from the presence of an
adapter. Interactive Brokers, Saxo Bank, and Zerodha additionally require a
fresh official exchange/broker schedule before stock onboarding. Enable the
matching supervised Compose profile (`calendar-ibkr`, `calendar-saxo`, or
`calendar-zerodha`) after reviewing its canonical selector list and catalogue
mapping. It writes through the admin-authenticated backend
``PUT /market-calendars/{code}`` and exposes readiness on internal port 8005.
Scheduled instruments with missing, stale, future-dated, or out-of-coverage
data fail closed. Crypto is explicitly 24/7, and ``CLOSE`` remains eligible for
risk reduction. The platform never guesses weekdays, holidays, shortened
sessions, or timezone rules. See
[Database Reference](DATABASE.md#authoritative-market-sessions). The Saxo
adapter is present but remains
uncertified: certification requires the external OAuth writer to prove atomic
refresh-token rotation, account identity, UIC/AssetType catalogue coverage,
pre-trade disclaimer handling, SIM reconciliation, and exchange-session
enforcement. Never exercise a live order merely to verify deployment.
IBKR certification likewise requires an authenticated Client Portal brokerage
session with `/tickle` health, a pinned live gateway CA, exact account identity,
reviewed conid coverage, and paper-account reconciliation. Execution never
performs `/secdef/search` or selects the first search result.

Before any broker advances to live certification, its adapter must ingest the
broker/exchange fill list and persist the actual stable execution identifier in
``executions.trade_id``. Cumulative order status is reconciliation state only
and never creates ledger economics. Canonical persistence requires the complete
order-scoped fill set with the venue trade ID, actual fill timestamp, positive
quantity and price, signed fee amount (including maker rebates), and fee
currency. A missing or contradictory
field leaves the pending order recoverable and fails closed.

Current certification boundary:

| Broker | Exact-fill implementation | Live status |
|---|---|---|
| Coinbase | Advanced Trade order-filtered fill list; quote currency is the documented commission currency | Eligible only after the existing Coinbase sandbox smoke, reconciliation, and paper-soak marker passes |
| Deribit | Order-scoped user trades with trade ID, timestamp, fee, and fee currency | Blocked until a Deribit-specific authenticated certification workflow and evidence are implemented |
| Interactive Brokers | Trade records do not provide a safely attributable commission currency in the current integration | Blocked |
| Saxo Bank | Order activities/trade history do not provide complete per-fill fee economics in one proven boundary | Blocked |
| Zerodha | Order trades provide fill identity/time/quantity/price but not per-fill fee economics | Blocked |
| Delta Exchange | Fills expose economics, but the current official request contract cannot be bounded to one order | Blocked |

Do not infer a fee currency, default fees to zero, stamp retrieval time as fill
time, or place a live order merely to manufacture certification evidence.

## Broker Source-IP and Gateway Requirements

Reviewed against official broker documentation on **2026-07-26**. Re-check the
linked source during every broker certification because these controls can
change independently of adapter code.

| Broker | Current requirement | Platform consequence |
|---|---|---|
| Coinbase | CDP secret keys support an IPv4/IPv6 or CIDR allowlist; Coinbase describes it as a security restriction, not a mandatory prerequisite for every key. [Coinbase security guidance](https://docs.cdp.coinbase.com/get-started/authentication/security-best-practices) | Use a fixed source IP before allowlisting a live trading key. The local-paper canary and public Coinbase candles do not require an allowlisted trading key. |
| Interactive Brokers | Individual Client Portal API order calls go through the Client Portal Gateway at `https://localhost:5000`; IBKR says the gateway must run on the same machine that generates commands. IBKR does not document a broker-side source-IP allowlist for this individual gateway path. [IBKR Web API](https://ibkrcampus.com/campus/ibkr-api-page/cpapi-v1/), [IBKR market-data lesson](https://ibkrcampus.com/campus/trading-lessons/requesting-market-data/) | Co-locate the authenticated gateway and adapter on one controlled host, pin its CA, monitor `/tickle`, and plan for regional reset/reauthentication. Do not apply the separate Account Management SSO static-IP field to an individual Client Portal account. |
| Deribit | API-key IP whitelisting is optional and restricts the selected key when configured. [Deribit key guidance](https://docs.deribit.com/articles/creating-api-key), [key API](https://docs.deribit.com/api-reference/account-management/private-edit_api_key) | A fixed source IP is recommended for a future trading key and becomes mandatory for that key once its whitelist is non-empty. |
| Zerodha | From 1 April 2026, Kite rejects every API order request from an unregistered static IP; the check applies to order endpoints, not market data, positions, or WebSocket reads. The broker also documents a strict 10-orders/second retail limit. [Zerodha operational notice](https://kite.trade/forum/discussion/15912/preparing-to-comply-with-sebis-retail-algo-rules-static-ip-ratelimits-order-types) | Static IPv4/IPv6 registration and an egress preflight are mandatory before Zerodha order certification. Keep order routing disabled until the exact observed source address is registered. |
| Delta Exchange India | A key with Trading permission requires one or more whitelisted IPv4/IPv6 addresses; India production and demo keys use different hosts, and signatures have a five-second validity window. [Delta India API](https://docs.delta.exchange/) | Fixed egress, NTP health, exact India/testnet host selection, and an IP-whitelist preflight are mandatory before order certification. |

For a separately reviewed future DigitalOcean deployment, do not assume that assigning a Reserved IP
changes outbound source identity. DigitalOcean requires an explicit persistent
route change for Reserved IPv4 egress; verify the observed address before
registering it with a broker. An assigned Reserved IP is currently free, but an
unassigned Reserved IPv4 is billed. [DigitalOcean outbound routing](https://docs.digitalocean.com/products/networking/reserved-ips/how-to/outbound-traffic/),
[Reserved IP pricing](https://docs.digitalocean.com/products/networking/reserved-ips/details/pricing/).
This egress change is required before Zerodha or Delta India certification, but
is deliberately not applied to the Coinbase local-paper canary.

## Pre-Migration Database Backup

Before production migrations:

```bash
# Cloud-agnostic pg_dump (current self-hosted or a future approved PostgreSQL).
DATABASE_URL="$DATABASE_URL" \
scripts/db/pre_migration_backup.sh
```

Do not run production migrations if the backup command fails. Record the backup file path in the
deployment notes.

## Live Trading Halt

Check halt state:

```bash
curl -sf \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Admin-API-Key: ${ADMIN_API_KEY}" \
  "${EXECUTION_ENGINE_URL}/admin/trading-halt"
```

Enable global halt:

```bash
curl -sf -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Admin-API-Key: ${ADMIN_API_KEY}" \
  -H "X-Admin-User: ${OPERATOR}" \
  -d '{"enabled": true, "reason": "operator stop", "metadata": {}}' \
  "${EXECUTION_ENGINE_URL}/admin/trading-halt"
```

Clear halt only after the incident owner confirms broker state, pending orders, and reconciliation
are healthy:

```bash
curl -sf -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Admin-API-Key: ${ADMIN_API_KEY}" \
  -H "X-Admin-User: ${OPERATOR}" \
  -d '{"enabled": false, "reason": "incident resolved", "metadata": {}}' \
  "${EXECUTION_ENGINE_URL}/admin/trading-halt"
```

## Failed Portfolio Rebalance Resolution

An unresolved terminal failed account plan keeps execution readiness red. First
inspect the readiness partition and the plan's immutable signal, decision,
order, fill, transition, and prior-resolution lineage:

```bash
curl -sf \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Admin-API-Key: ${ADMIN_API_KEY}" \
  "${EXECUTION_ENGINE_URL}/admin/rebalance-readiness"

curl -sf \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Admin-API-Key: ${ADMIN_API_KEY}" \
  "${EXECUTION_ENGINE_URL}/admin/rebalance-plans/${ACCOUNT_PLAN_ID}"
```

Only after the incident owner verifies broker state, canonical fills,
positions, cash, and reconciliation may an identified operator append a
disposition. `evidence` must contain durable incident or artifact references,
never credentials or secrets:

```bash
curl -sf -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -H "X-Admin-API-Key: ${ADMIN_API_KEY}" \
  -H "X-Admin-User: ${OPERATOR}" \
  -d '{"resolution_type":"reconciled","reason":"broker and canonical ledger reconciled","evidence":{"incident":"INC-0000","artifact":"reconciliation.json"}}' \
  "${EXECUTION_ENGINE_URL}/admin/rebalance-plans/${ACCOUNT_PLAN_ID}/resolve-failure"
```

The first `acknowledged`, `reconciled`, or `remediated` disposition removes the
plan from the unresolved-failure readiness count. It does not alter or restart
the failed plan. Exact retries deduplicate; later remediation evidence appends
another immutable record. Confirm both the plan audit and `/ready` after the
write.

## Coinbase Paper-Soak Certification

First verify the acceptance signals programmatically (DEPLOYMENT.md → *Promotion
acceptance criteria*). This queries the live DB + `ALERT_*` env and exits non-zero
unless every signal is green, writing a JSON report the marker requires:

```bash
# Run inside the deployed money-moving image so the check sees the exact
# service-role database URL, dependency set, and alert configuration. Keep the
# output on the host; the execution container's certification mount is read-only.
docker compose --env-file .env -f docker-compose.droplet.yml \
  exec -T execution-engine \
  python /app/scripts/check_soak_acceptance.py --json \
  > artifacts/coinbase/soak-acceptance.json
```

Then write the marker — only after at least 14 paper-soak days, successful sandbox
smoke evidence, healthy reconciliation, and a **passing** acceptance report (the
marker refuses a `passed` status while any check is red):

```bash
python scripts/write_sandbox_certification_marker.py \
  --commit "$(git rev-parse HEAD)" \
  --operator "${OPERATOR}" \
  --symbols "BTC-USDC,ETH-USDC,SOL-USDC" \
  --paper-window-days 14 \
  --duplicate-submission-count 0 \
  --acceptance-report ".artifacts/coinbase/soak-acceptance.json" \
  --sandbox-smoke-evidence ".artifacts/coinbase/sandbox-smoke.json" \
  --paper-soak-evidence ".artifacts/coinbase/paper-soak-summary.json" \
  --reconciliation-summary ".artifacts/coinbase/reconciliation-summary.json"
```

## Execution Claim State

`execution_decision_logs.idempotency_key` is the durable duplicate-command
guard. The execution engine must acquire the row before broker resolution,
account-state reads, market-data reads, or broker submission. A v1 decision also
stores its exact `canonical_signal_id` and `broker_account_id`; run-wide
attribution is not accepted for feedback.

The key is
`exec:sha256(identity:user_id:broker_account_id:symbol:action)[:32]`, where
`identity` is the **stable** signal identity — the required canonical
`external_signal_id`. Identity-less or account-less signals are rejected before
scoring persistence, outbox dispatch, or broker resolution; the per-request
`signal_id` is trace context and is never an execution identity.
Both scoring and execution derive it from
`lib_strategy.signals.utils.compute_execution_dedup_key`. Keying on
`external_signal_id` rather than a request/run ID makes worker restart, outbox
redelivery, and re-ingestion resolve to the same decision.

State transitions:

- `pending -> executing`: worker owns the execution attempt.
- `executing -> executed`: broker-visible submission completed successfully.
- `executing -> failed`: execution attempt ended without a successful broker-visible result.
- fresh `executing`: redelivery exits as a no-op; do not submit another order.
- stale `executing`: automatic redelivery exits as a no-op. Never use a database
  status reset as a substitute for broker reconciliation.
- `executed` and `failed`: terminal for automatic retries.

The concrete order has a second, broker-facing identity boundary. Execution
persists the canonical intent and stable `client_order_id` before broker I/O.
A timeout or transport failure after dispatch becomes
`pending_orders.status=submission_unknown` and the canonical order retains the
same state; it is not a broker rejection. `/ready` remains false while any
unknown submission exists. Reconciliation must query the broker by that stable
client identity/order history and converge the existing order before any
resubmit. Never invent a new client ID, reset the row to pending, or call the
broker again merely because the HTTP response was lost.

At startup, execution discovers every applicable paper account from active
bindings, recoverable orders, and non-flat canonical positions. It remains
unready until each discovered account partition completes one reconciliation,
including an account with no new signal traffic.

## Execution-command dead letters

Permanent delivery failures dead-letter immediately; transient failures use
bounded retry/backoff and dead-letter after the event's maximum attempts. Any
dead-lettered `execution.commands` event makes scoring/relay readiness false.
Inspect through the authenticated scoring API:

```bash
curl -sf \
  -H "X-Admin-API-Key: ${ADMIN_API_KEY}" \
  "${SCORING_ENGINE_URL}/admin/outbox/dead-letters?limit=100"
```

Redrive only after fixing the cause and proving from the broker/order ledger
that preserving the original economic identity is safe. Use the returned
`redrive_generation`; a concurrent or stale operator loses the generation
fence:

```bash
curl -sf -X POST \
  -H "Content-Type: application/json" \
  -H "X-Admin-API-Key: ${ADMIN_API_KEY}" \
  -H "X-Operator-ID: ${OPERATOR}" \
  -d '{"reason":"downstream dependency recovered","expected_generation":0}' \
  "${SCORING_ENGINE_URL}/admin/outbox/dead-letters/${EVENT_ID}/redrive"
```

Only `execution.commands` is eligible. Redrive preserves the event ID,
idempotency identity, source event, and payload and appends actor, reason,
generation, timestamp, and outcome to `redrive_audit`.

## Durable local-paper order checks

The local-paper lifecycle consumes every committed provenance-bearing source
bar independently of new strategy commands. A working stop/limit order must
retain `client_order_id`, `trigger_policy_version=ohlc-conservative-v1`,
source/timeframe, last source timestamp/revision, purpose, reduce-only,
parent/OCO, and cumulative-fill state. When one OCO member terminally fills, its
eligible sibling is canceled in the same economic transition. A bar touching
both siblings chooses the adverse result; a gap-through stop uses the first
available real bar price. Do not edit a price or trigger timestamp to force a
paper fill.

Monitor `execution_pending_orders_total`,
`execution_pending_order_oldest_age_seconds`,
`execution_paper_order_processing_lag_seconds`,
`execution_reconciliation_initial_complete`, and
`execution_reconciliation_discovered_partitions`. The age metric is incident
context; the committed market-time lag and missing watermark are the paper
progress gate. Restart evidence must show the same stable order/trade identity
and no second cash, position, P&L/NAV, or execution-event projection.

## Incident Checks

- If duplicate execution is suspected, check `execution_decision_logs.idempotency_key` and status
  transitions, the canonical order/client ID, and broker order history before
  considering any operator action.
- If readiness reports an unknown submission, quarantine new automatic
  execution for that account and reconcile by stable client order identity.
- If scoring readiness reports a dead letter, inspect its failure class and
  audit generation; never bulk-reset outbox rows.
- If indicator readiness is false, inspect per-strategy feed lag and durable
  signal backlog before restarting. A process restart does not clear either
  durable condition.
- If broker account state or market data is unavailable in live mode, leave live execution blocked;
  do not bypass fail-closed gates.
- If risk guard blocks execution, inspect `risk_breaches` for the exact
  `user_id` and `broker_account_id` before adjusting limits.
- If pending orders are inconsistent with Coinbase, keep halt enabled until reconciliation is clean.
- If a conversion is unavailable or stale, inspect `fx-rate-ingestor` readiness
  and the `prices` rows for `EURUSD`, `EURGBP`, `EURINR`, and `USDCEUR`. Do not
  bypass the accounting guard or substitute USD/USDC parity.
- Feedback approval is an audit decision only. Promote an approved parameter
  suggestion through a reviewed source/config change, validation, image build,
  and deployment; the runtime feedback job must never write strategy files.
  If generation is suppressed, verify the signal's exact `strat_ver_id` is
  active and that its `strategy_versions.default_params` is a non-empty,
  valid snapshot containing at least one supported adjustable parameter.
