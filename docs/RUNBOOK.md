# Operational Reference (Local Paper Scope)

This inherited operational reference describes code paths and evidence requirements.
It does not attest to a running deployment or supply broker certification. The
vynmatrix migration authorizes local paper verification only; do not deploy,
write live-authority markers, clear live halts, or change live gates. External
provider details below are dated references and must be rechecked before future use.

This runbook covers the single-owner paper path: selected indicator processes →
scoring with inline outbox relay → execution → feedback, plus selected feed,
FX and official-calendar processes. [DEPLOYMENT.md](DEPLOYMENT.md) defines the
three-container split and two-container combined layouts.

## Launch Gates

- Complete the supported bootstrap/owner procedure in [DATABASE.md](DATABASE.md).
  The lifecycle stops application and workers before a maintenance job, waits for
  it to exit, then starts only the configured runtime groups. Do not use wildcard
  profiles, pgAdmin, or additional one-shot containers alongside running groups.
- Keep `EXECUTION_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false`. Configure
  exact role URLs, separate service/admin keys and the backend/execution key ring
  listed in [CONFIGURATION.md](CONFIGURATION.md).
- Inspect supervisor port `8090`: `/health` means required processes/listeners
  are alive; `/ready` requires progress from every selected component. Management
  can remain healthy while owner setup or feeds are unready. `/status` provides
  component results and `/metrics/<component>` exposes its metrics.
- Scoring outbox, indicator backlog/lag and durable paper-order lag default to
  300 seconds. Investigate the stalled partition before changing a bound.
- When `fx` is selected in `PLATFORM_WORKERS`, inspect its internal `8004/ready`
  result for fresh ECB/Coinbase observations. Calendar ports are `8005` (IBKR),
  `8006` (Saxo) and `8008` (NSE); primary/equity feeds use `8003`/`8007`.
- Feedback is a real supervised daemon on `8002`; readiness requires a recent
  successful database heartbeat. Its default interval is 300 seconds. There is
  no repeating shell, external timer or fourth feedback container.
- Use recorded real data for paper acceptance. A running image or green unit
  test is not PostgreSQL, restore, paper-soak or broker certification evidence.

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml logs --tail 100 application workers
docker compose --env-file .env -f docker/docker-compose.stack.yml exec -T application \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8090/status').read().decode())"
```

Only backend and PostgreSQL bind host loopback. For a private remote host, use
an owner-controlled SSH tunnel to backend. An IBKR gateway remains an explicitly
reviewed external dependency; a gateway container requires the combined `all`
layout so it occupies the third slot. No gateway is provisioned here.

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
matching supervised `PLATFORM_WORKERS` selection (`calendar-ibkr`, `calendar-saxo`, or
`calendar-zerodha`) after reviewing its canonical selector list and catalogue
mapping. It writes through the admin-authenticated backend
``PUT /market-calendars/{code}`` and exposes readiness on its unique internal port (8005/8006/8008 respectively).
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

## Backup and graceful upgrades

Use the scoped backup/restore and migration procedures in [DATABASE.md](DATABASE.md).
Record and verify a backup before migration. Keep the previous image and the
separately recoverable encryption ring; ciphertext alone is not a restorable
credential store. PostgreSQL persists in `postgres-data`; `.artifacts` is
read-only to runtime and `Data` is mounted at `/data`.

Stop runtime through the supported lifecycle before maintenance. Compose allows
60 seconds for application/workers to stop, while the supervisor bounds its
whole-group cleanup to 55 seconds and forwards signals to descendants. A failed
stage leaves runtime stopped. Review errors and the migration's rollback/data
limits before retrying; never remove volumes to obtain a clean startup.

## Internal administrative API contracts

These APIs run inside `application`: execution on `8000`, scoring on `8001`.
They are not published host ports. An authorized client on that private boundary
must send the corresponding `EXECUTION_API_KEY` or `SCORING_API_KEY` as
`X-API-Key`, and `EXECUTION_ADMIN_API_KEY` or `SCORING_ADMIN_API_KEY` as
`X-Admin-API-Key`. Backend uses its separate `X-Admin-Key` boundary. Never place
actual keys in command arguments, shared reports or logs.

### Trading halt

`GET /admin/trading-halt` reads execution halt state. An identified operator can
request a halt with `POST /admin/trading-halt`, `X-Admin-User`, and
`{"enabled":true,"reason":"operator stop","metadata":{}}`. Preserve incident
attribution. Clearing a halt requires explicit incident resolution and separate
authority; it is not a bootstrap or paper-verification step.

### Failed portfolio rebalance resolution

An unresolved terminal failed account plan keeps execution readiness red.
Inspect `GET /admin/rebalance-readiness` and
`GET /admin/rebalance-plans/{account_plan_id}` for immutable signal, decision,
order, fill, transition and prior-resolution lineage. After verifying broker
state and the canonical ledger, an identified operator may append a disposition
through `POST /admin/rebalance-plans/{account_plan_id}/resolve-failure` using
`X-Admin-User` and a `resolution_type`, reason and durable evidence references.

The first acknowledged, reconciled or remediated disposition removes that plan
from unresolved-failure readiness. It neither changes nor restarts the failed
plan. Exact retries deduplicate; later evidence adds an immutable record. Check
the audit and `/ready` afterward. No credential belongs in evidence.

## Coinbase Paper-Soak Certification

Follow [E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md) for the recorded-data
campaign, acceptance output and certification boundaries. The platform image
includes `scripts/check_soak_acceptance.py`; execute it inside the existing
`application` container with the execution child's scoped environment. Do not
start a new execution or verification container. For an already authorized
paper acceptance run, keep output on the host:

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml exec -T application \
  python -c 'import os,subprocess,sys; from scripts.platform_processes import build_processes; env=next(p.environment for p in build_processes("application",os.environ) if p.name=="execution"); sys.exit(subprocess.run([sys.executable,"/app/scripts/check_soak_acceptance.py","--json"],env=env,check=False).returncode)' \
  > .artifacts/coinbase/soak-acceptance.json
```

A passing marker requires the actual bounded paper window, recorded sandbox
smoke, reconciliation and passing acceptance evidence. No completed soak,
PostgreSQL integration or new-image build result is asserted by this document,
and no live-authority marker is authorized here.

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
Inspect `GET /admin/outbox/dead-letters?limit=100` through the authenticated
internal scoring API. After fixing the cause and checking broker/order history,
redrive through `POST /admin/outbox/dead-letters/{event_id}/redrive` with
`X-Operator-ID`, a reason, and the returned `expected_generation`. A stale or
concurrent operator loses the generation fence. The scoped scoring service and
admin keys above are both required.

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
