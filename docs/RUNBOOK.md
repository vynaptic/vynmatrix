# Operational runbook: local paper scope

This is the incident and recovery reference for the declared local paper
runtime. It does not authorize a deployment, broker certification, or live
trading. Bootstrap is in [DATABASE.md](DATABASE.md), configuration is in
[CONFIGURATION.md](CONFIGURATION.md), and recorded-data acceptance is in
[E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md).

## Before an operational action

- Keep EXECUTION_MODE=paper and EXECUTION_ENGINE_ALLOW_LIVE=false.
- Confirm the explicit owner, scoped role URLs, selected workers, and strategy
  readiness before using the runtime.
- Use only the declared PostgreSQL/application/workers or combined layout.
  Bounded work runs with compose exec inside an existing group.
- Preserve database records and redacted logs until the incident or evidence
  review is complete.

Inspect the existing runtime first:

~~~text
vmdev db status
docker compose --env-file .env -f docker/docker-compose.stack.yml logs --tail 100 application workers
docker compose --env-file .env -f docker/docker-compose.stack.yml exec -T application <status-or-recovery-command>
~~~

The log example is for the split layout; omit workers in the combined layout.

Supervisor health proves liveness. Supervisor readiness needs selected-component
progress. A red readiness result is evidence to investigate, not a reason to
weaken a threshold, bypass a gate, or restart a database volume.

## Broker certification state

Each broker needs independent adapter tests, authenticated non-ordering
connectivity, account-scoped reconciliation, and a bounded paper soak before
any future live-certification discussion. Do not infer certification from an
adapter, a credential, or a current market-data connection.

| Broker | Current boundary |
| --- | --- |
| Coinbase | Exact-fill persistence is implemented; still requires the documented sandbox, reconciliation, and paper-soak evidence for the exact account/route. |
| Deribit | Exact trade reader exists; its authenticated certification workflow remains outstanding. |
| Interactive Brokers | Blocked until a route supplies complete exact-fill economics, including fee currency, and passes gateway/account/session evidence. |
| Saxo, Zerodha, Delta | Blocked until the implemented official route can prove complete account-scoped exact-fill economics and its own certification workflow. |

Before an order-capable certification, re-check the broker's current
documentation for source-IP, egress, rate, gateway, credential, and session
requirements. The platform must persist a stable venue trade ID, actual fill
time, quantity, price, fee amount, and fee currency. Cumulative order status,
assumed fee currency, retrieval timestamps, and fabricated fills do not satisfy
the ledger contract.

IBKR Client Portal requires an owner-operated authenticated gateway and exact
catalogued account/conid identity; never select the first account or symbol
search result. A separately approved gateway container consumes the third
container slot in the combined layout. See
[BROKER_CREDENTIALS.md](BROKER_CREDENTIALS.md) for account-specific credential
and gateway fields.

## Backup and controlled maintenance

Use the explicit database operations:

~~~text
vmdev db backup backups/pre-upgrade.dump
vmdev db migrate
vmdev db restore backups/pre-upgrade.dump
~~~

Validate a backup before migration, retain the prior image and encryption-key
ring, and leave runtime stopped after restore until the target, roles, and
schema state are checked. Never remove volumes to clear an error.

## Administrative recovery surfaces

Administrative APIs are private to the application group. Each route below
requires its matching X-Admin-API-Key; do not publish it or put it in a command
line, log, or evidence file.

### Trading halt

GET /admin/trading-halt reads execution halt state. An identified operator may
request a halt with POST /admin/trading-halt, X-Admin-User or X-User-ID, and an
explicit reason. Clearing a halt needs incident resolution and separate
authority; it is not part of bootstrap or paper verification.

### Failed rebalance plan

An unresolved terminal account-rebalance plan keeps execution readiness red.
Inspect GET /admin/rebalance-readiness and
GET /admin/rebalance-plans/{account_plan_id}, verify broker state and the
canonical ledger, then append a disposition through
POST /admin/rebalance-plans/{account_plan_id}/resolve-failure with the identified
operator header X-Admin-User or X-User-ID, and a body containing resolution_type,
reason, and optional durable evidence references.

An acknowledged or remediated disposition does not change or restart the
terminal plan. Exact retries deduplicate; later evidence appends an immutable
record. Recheck readiness and audit after the action.

### Outbox dead letter

If an execution.commands event is dead-lettered, scoring readiness is false.
Inspect GET /admin/outbox/dead-letters through the scoped scoring admin boundary.
After fixing the cause and reconciling broker/order history, redrive with
POST /admin/outbox/dead-letters/{event_id}/redrive, X-Operator-ID, and a body
containing reason and the current expected_generation. Only execution-command
topics (execution.commands and execution.rebalance.commands) are eligible.
Redrive keeps the original business identity and appends generation-fenced audit;
it cannot bypass current execution authority.

## Incident checks

| Symptom | First action |
| --- | --- |
| Suspected duplicate execution | Inspect execution-decision idempotency state, canonical client order ID, and broker history before any action. |
| Unknown submission | Keep automatic execution quarantined for the account; reconcile by stable client order identity. Never reset to pending. |
| Indicator unready | Inspect strategy feed lag, source watermark, and durable backlog before restart. |
| Scoring dead letter | Inspect failure class and redrive audit. Never bulk-reset outbox rows. |
| Risk guard block | Inspect the exact user/account risk breach before changing limits. |
| Stale/missing FX | Inspect the selected FX worker and persisted price observations. Do not substitute parity. |
| Inconsistent local-paper order | Check stable order/trade identity, source watermark, trigger policy, and ledger/projection reconciliation. |

Pending local-paper orders retain trigger, purpose, time-in-force, reduce-only,
parent/OCO, cumulative quantity, and exact source watermark. They consume only
later committed real bars under the configured conservative trigger policy.
After restart, the same identity and economics must converge once.
