# Historical migration and publication record

This is a dated record of the independent vynmatrix migration and publication
review. It is not a current operating guide or proof of a present runtime.
Current setup, database, deployment, and paper-evidence contracts are in the
documents linked from [README.md](../README.md#documentation).

## Source boundary

The repository was exported from committed legacy-platform snapshot
a739b499c66dfea3895dd37966b120c9e81ed22d with fresh Git history. No source
history, branches, tags, author metadata, local Git configuration, runtime
state, credentials, ignored/untracked files, environments, caches, images, or
Git objects were copied.

| Area | Recorded result |
| --- | --- |
| Product identity | vynmatrix package, service, image, Compose, and tooling names established |
| Runtime contracts | Schema/provider namespaces and payload identities updated without changing recorded market values |
| Example material | Personal identities replaced with fictional/reserved examples |
| Build and Git tooling | vmdev retained; repository-local hooks and protected pull-request workflow configured |
| Documentation | Portable local setup, authority boundaries, and source-available publication context added |

The historical snapshot had no submodules, Git LFS pointers, binary assets,
archives, notebooks, or embedded documents. Generic VM names, vm database
roles, and vmdev were retained because they are functional contracts.

## Recorded review and validation

The original sanitization review covered retained files, filenames, hidden
metadata, configuration/SQL examples, documentation, public URLs,
credential-shaped strings, and built wheel metadata. It found no retained
personal names, contact addresses, home paths, private keys, or private account
records after sanitization. That is a bounded historical audit, not a legal
clearance or anonymity guarantee.

At the migration checkpoint, repository tooling, wheel builds, Compose parsing,
schema-drift checks, disposable PostgreSQL bootstrap, and selected service
imports were run against isolated local targets. Two inherited PostgreSQL
baseline failures were recorded before later targeted fixture corrections:
local-paper account identity and a market-catalogue SELECT expectation that
conflicted with migration 0086. No execution gate or privilege was weakened to
make those checks pass.

The 2026-09-05 single-owner follow-up recorded successful fresh/repeated
bootstrap, role/catalogue/owner operations, migration/restore, bounded
two-/three-container topology checks, and focused PostgreSQL and recorded-data
pipeline validation in an isolated test project. It did not establish a current
Swing economic-order witness, broker connectivity, deployment, capacity result,
or live authority. Re-run the current checks for any new claim.

A later isolated local-paper recorded-data exercise covered bootstrap, bounded
strategy configuration/binding, historical backfill, normal stale-entry
rejection, and a separately authorized paper replay. Explicit scoring inputs
were relaxed only after the blocked-entry result, then restored with the binding
revoked. A known harness timeframe defect excluded affected signals from
feedback, and the per-day replay cap bounded the exercise. It exposed the host
bootstrap import, protective-order cancellation, metrics-proxy credential, and
year-long Coinbase FX-backfill regressions addressed in the current source.
This was a functional pipeline exercise, not strategy-promotion or live-trading
evidence.

The event-driven delivery change (2026-09-06, `feat/event-driven-delivery-bounded-pools`)
was accepted on the same isolated stack: `vmdev db bootstrap` applied
`0105_retire_observational_topics` and recreated both groups on the rebuilt image; the
supervisor's `/metrics/scoring` reported `vm_scoring_outbox_relay_up 1`,
`vm_scoring_outbox_notify_listener_up 1`, a zero outbox backlog and
`vm_outbox_progress_ready 1`; `vm_indicator_login` held 4 connections against its
configured allowance of 5 for one strategy plus its supervisor; every row of the four
retired topics was already published, so the migration changed no rows; no errors were
logged. The twenty-worker PostgreSQL acceptance module measured commit-to-receipt delivery
latency of about 0.22 to 0.25 s median and 0.31 s maximum. During the preceding soak the
live canary journaled 72 bar decisions and emitted one natural `ETH-USDC` entry, which the
durable relay published 88 ms after enqueue and scoring persisted with exact `1m` price
provenance; no execution decision followed because binding authority had been revoked.
`vmdev test all` passed 3,653 tests with 122 skips and `vmdev audit --strict` passed with
the broad-exception baseline lowered from 43 to 42.

## Current design references

The follow-up added migrations 0099 through 0104 for owner designation, safe
catalogue registration, control-plane guards, commercial-tenancy removal, and
the guarded Saxo capability record. The current revision and all lifecycle
rules are canonical in [DATABASE.md](DATABASE.md). The current single-owner
decision is [SINGLE_OWNER.md](SINGLE_OWNER.md); it supersedes this record for
architecture.

## Publication and remaining provenance work

vynmatrix is published at
[vynaptic/vynmatrix](https://github.com/vynaptic/vynmatrix) under the
[Vynmatrix Personal Noncommercial Reciprocity License 1.1](../LICENSE). It is
publicly source-available for personal, noncommercial use and is not
OSI-approved open source. Publication does not transfer retained VisionMaverick
copyright or any account, deployment, strategy, or live-execution authority.

Two fixture-reuse records remain incomplete:

- config/universe/sp500_membership_full.csv has a source label but no exact
  source revision or reuse record.
- tests/fixtures/market_data contains Coinbase-related frozen fixtures with
  incomplete capture and redistribution records.

Resolve or deliberately replace/exclude those materials without hiding their
history or required attribution. [NOTICE](../NOTICE) keeps this limitation
visible. Credentialed broker/provider behavior and PowerShell execution were
not established by the historical review.

The [Code of Conduct](../CODE_OF_CONDUCT.md) has no private incident-reporting
channel. Designate one before soliciting community reports; do not invent a
contact address in documentation.
