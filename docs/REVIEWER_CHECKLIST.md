# Pull-request reviewer checklist

Use this for judgment calls that automation cannot make. The source of truth for
mechanical checks is
[tools/dev_cli/dev_cli/commands/audit.py](../tools/dev_cli/dev_cli/commands/audit.py);
CI and pre-commit run vmdev audit. Do not duplicate its thresholds or silence a
failure by raising a baseline without a stated reason.

## Architecture and contracts

- [ ] New behavior uses the established domain, application, infrastructure, or
  app boundary. No reverse imports or new duplicate canonical types.
- [ ] Strategies remain signal-only and emit the canonical Signal contract.
- [ ] A signal, scoring, outbox, execution, or feedback change traces its real
  producers, consumers, persistence, and idempotency behavior.
- [ ] Order-intent boundary types remain explicitly converted rather than merged
  by matching names.
- [ ] Explicit owner, broker account, environment, instrument, currency/FX,
  market-session, and ledger authority remain present and fail closed.

## Data and migrations

- [ ] ORM, Alembic, database-role, transaction, retry, rollback, and
  downstream-consumer implications are covered by the change.
- [ ] A migration preserves historical attribution and does not delete populated
  commercial or trading data merely to simplify a schema change.
- [ ] New reference or account updates have stable identities, expected-value
  conflicts, idempotent repeats, and audit behavior where appropriate.
- [ ] Positions and metrics remain projections; ledger replay remains the
  accounting source.

## Tests and evidence

- [ ] The narrowest meaningful tests ran, then broader checks where the blast
  radius warrants them.
- [ ] Default tests do not call live external services or a personal runtime.
- [ ] A paper-pipeline claim distinguishes real-data current-time behavior,
  stale historical rejection, authorized historical replay, and fixture-based
  failure-mode evidence.
- [ ] A skipped integration test, a running container, or synthetic market data
  is not presented as acceptance proof.

## Runtime and operations

- [ ] Paper mode and the live-execution gate remain unchanged.
- [ ] The declared runtime stays within three running containers including
  PostgreSQL; bounded jobs use an existing group.
- [ ] Runtime children receive scoped database and API credentials, never
  maintenance credentials.
- [ ] Readiness remains distinct from management health, and shutdown/recovery
  behavior remains bounded.
- [ ] New scripts are listed in [scripts/README.md](../scripts/README.md) and
  use a documented canonical workflow.

## Public surface and documentation

- [ ] No compatibility alias, stale export, or parallel API is retained without
  a known consumer and approval.
- [ ] The document that owns changed behavior is updated, links resolve, and
  root README remains the canonical document index.
- [ ] AGENTS.md and CLAUDE.md remain synchronized if either changed.
- [ ] The PR title uses a conventional prefix, the description explains the
  concrete behavior, and the test plan states what actually ran.
