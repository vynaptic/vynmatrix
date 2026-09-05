# Single-owner architecture decision

**Status:** current design record, reviewed 2026-09-05. It describes the
enduring single-owner boundary; it is not an implementation checklist or an
operating runbook. Current commands and verification belong to
[DATABASE.md](DATABASE.md), [DEPLOYMENT.md](DEPLOYMENT.md), and
[E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md). Historical evidence and
publication limits belong to [MIGRATION.md](MIGRATION.md).

## Decision

Each deployment has one explicitly designated owner. The platform retains
historical users and account IDs for provenance but removes product tenancy,
organization/plan entitlement, tenant routing, and user-selected owner
resolution from the operating model.

The three-container limit is an explicit small-deployment operating constraint:
it keeps PostgreSQL and all supervised application work in a compact, locally
manageable footprint. Changing it requires an owner decision, a declared
topology, and evidence that the new external dependency or process cannot run
inside an existing group.

### Terms

| Term | Meaning |
| --- | --- |
| Deployment owner | The one user designated under maintenance authority for this database. |
| Historical user | A retained user ID referenced by past records; not a selectable tenant. |
| Broker account | An exact owner-linked broker, environment, currency, and external-account identity. |
| Execution authority | The current binding, route, data, session, credential, and risk state required immediately before broker I/O. |

## Runtime topology

~~~mermaid
flowchart LR
    O[Deployment owner] --> B[Maintenance bootstrap]
    B --> P[(PostgreSQL)]
    P --> A[application group]
    P --> W[workers group]
    A -->|scoring decision + outbox row| P
    P -->|outbox relay over HTTP| A
    A -->|execution command| A
    W -->|signals and market data| P
~~~

The normal split layout runs PostgreSQL, application, and workers. The combined
layout runs PostgreSQL plus one all application group. A bootstrap job uses an
existing non-PostgreSQL slot only after runtime groups stop, then exits before
they restart. It is therefore not a fourth running service.

The shared vynmatrix/platform image supervises API and worker processes inside
the application groups. Image count and process count do not change the running
container budget. PostgreSQL data and owner-managed secrets persist outside
ephemeral processes; logs go to the configured Docker or host collection path.

## Authority model

Removing tenancy does not remove account or execution boundaries:

- Every executable decision remains tied to a concrete owner, broker account,
  broker environment, canonical instrument, currency/FX provenance, and market
  session.
- The scoring-to-execution handoff remains a durable transactional outbox with
  at-least-once delivery and an idempotent consumer.
- Orders, fills, cash, positions, NAV, and feedback derive from the canonical
  execution ledger. Projections never replace ledger replay.
- Cash indices remain reference-only. A tradable contract needs an explicit
  catalogue identity and broker mapping.
- EXECUTION_MODE=paper and EXECUTION_ENGINE_ALLOW_LIVE=false stay fixed for
  ordinary local work.

Routine CLI and API callers resolve the designated owner and cannot supply a
different owner identity. The single sanctioned exception is maintenance
onboarding of a known retained user through owner.local.yaml with
existing_user_id or vmdev user init --existing-user-id; it runs with
migration/onboarding authority and verifies that exact ID. It is not a runtime
lookup or general caller option.

## Bootstrap and configuration lifecycle

Source-controlled catalogues describe reviewed, non-secret strategies, brokers,
instruments, and reference data. They use stable source identities; the
database owns installed owner settings, account keys, credentials, audit state,
and all trading history.

vmdev db bootstrap performs fresh install stages in order: database
provisioning, Alembic schema, runtime roles, inactive reference registration,
then explicit owner initialization. A repeat validates the existing designation
and creates only missing references; it never overwrites owner values,
credentials, account authority, or strategy state. Strategies register inactive
and versions registered, so source presence remains non-executable.

Later vmdev db catalogue and vmdev user operations use expected-value updates,
stable keys, transactions, audit rows, and bounded retries. Conflicts fail
without partial writes; explicitly acknowledged repeats are no-ops.
[DATABASE.md](DATABASE.md) is the complete contract for roles, transactions,
existing databases, migration/rollback, and recovery.

## Future owner UI

A future local UI can use the existing backend administrative boundary for
owner profile, account, broker credentials, strategy configuration/bindings,
risk mandates, calendars, and execution visibility. It must authenticate to
the backend's existing owner boundary and preserve the same current-state
execution checks. Static UI assets can be served from the application group;
they do not require a fourth container or the return of multi-tenancy.

## Deferred decisions

- Broker-specific credentials, account mapping, and paper certification remain
  owner-supplied and per-account; none are fabricated by bootstrap.
- Cloud host, external gateway, public authentication, image registry, and
  live authority require separate decisions and evidence.
- Strategy performance, paper-promotion eligibility, and broker certification
  remain strategy- and account-specific. The current Swing canary is never
  general execution approval.

See [STRATEGY_READINESS.md](STRATEGY_READINESS.md),
[BROKER_CREDENTIALS.md](BROKER_CREDENTIALS.md), and
[RUNBOOK.md](RUNBOOK.md) for those boundaries.
