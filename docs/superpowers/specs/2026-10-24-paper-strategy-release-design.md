# Paper strategy release: one maintenance command for one exact version

Status: accepted (2026-10-24). Scope: a `vmdev db` maintenance command that
releases one exact, already-registered strategy version so the owner can bind
it for paper trading, and a matching command that withdraws the release. It does
not change live authority, create a binding, enable a strategy in
`STRATEGY_LIST`, or replace the cloud-paper promotion manifest.

## 1. Goal and non-goals

Registration is fail-closed: `vmdev db catalogue --apply` creates every shipped
strategy as `strategies.is_active = false` and `strategy_versions.status =
'registered'`. The only code that ever sets those two columns is the dev canary
activation, and it is hard-limited to the Swing canary, which is permanently
excluded from paper promotion. So no other strategy can be bound: the bind gate
answers 409 and the owner UI says "Not released" with no way forward.

| Scenario | Owner wants | Today | After |
| --- | --- | --- | --- |
| A | Paper-trade a port I have reviewed | Impossible without hand-written SQL | Mark it ready in a reviewed commit, run one command, bind it in the UI |
| B | See what a release would do first | Nothing to preview | `--check` prints the decision and writes nothing |
| C | Take a strategy back out of the catalogue | Hand-written SQL or a migration | One command, refused while anything still trades it |
| D | Know who released what, and on what basis | Nothing to read | One audit row per change, with the commit and config hash relied on |

Non-goals, each deliberate:

- **Live authority.** Nothing here reads, writes or relaxes
  `EXECUTION_ENGINE_ALLOW_LIVE`, a broker environment, an account or a
  credential (section 6).
- **A UI release button.** Release stays a maintenance action under the
  maintenance database role; the backend role keeps no grant to release.
- **Runtime selection.** `enabled`, `environments` and `STRATEGY_LIST` remain
  separate, later gates.
- **Cloud-paper promotion.** The staging/production promotion manifest
  (`lib_common.paper_promotion`) is unchanged and still required there.
- **Swing.** It keeps `vmdev db activate-canary`; this command refuses it.
- **A schema change** (section 3, approach 4).

## 2. Evidence that shapes the design

| Fact | Source |
| --- | --- |
| Registration creates `is_active=false` and `status='registered'`; catalogue reconciliation cannot activate a release | `catalogue.py` `_create_strategy_release`; `DATABASE.md` "Routine configuration changes" |
| The only writer of `status='active'` / `is_active=true` refuses anything that is not an enabled dev-only `E2E_PIPELINE_CANARY_ONLY` source, and requires `ENVIRONMENT=dev` | `canary_activation.py:25-33`, `:46-48`, `:91-93` |
| The bind gate needs `strategies.is_active IS TRUE` **and** an active version; the UI "Released" flag and the deploy summary use the same condition | `binding_control.py:118-145`, `:164-189`; `ui_queries.py:683-695` |
| Scoring refuses to persist a signal, and execution refuses broker I/O, unless the signal's exact version is active and its strategy row is active. Execution applies this to **every** order, exits included | `_storage_ops.py:261`; `execution_routing.py:60-67`, `:547` |
| Release also makes the strategy eligible for parameter feedback | `strategy_feedback.py:51-68`; `optimizer.py:259-266` |
| A signal with no explicit version resolves to the newest active version, so two active versions of one strategy make attribution depend on release order | `_storage_base.py:205-225` |
| A validation campaign cannot enable paper by contract: `historical_results_can_enable_paper: False`, `human_approval_required: True`; its best disposition is `FREEZE_FOR_PROSPECTIVE_SHADOW` | `campaign_contracts.py:137-148` |
| The paper-promotion manifest names a binding ID and an account, and its evidence includes `binding_active: True` and a soak. It is produced *after* a binding trades, so it cannot be the precondition for the release a binding needs | `paper_promotion.py` `_AUTHORITY_SCOPE_KEYS`; `paper_promotion_evidence.py` `account_binding`, `soak_acceptance` |
| The manifest and the readiness label are runtime gates only in staging/production. In dev both are skipped | `scoring_engine/main.py:162-166`; `process_manager.py:332-333` |
| The owner's install runs `ENVIRONMENT=dev` (Compose default) | `docker-compose.stack.yml:12` |
| Paper-ready readiness labels already exist: `READY_FOR_PAPER_TRADING`, `LIVE_CANDIDATE`. Every shipped non-canary strategy is `STATIC_REVIEW_ONLY`; Swing is `READY_FOR_BACKTEST` / `E2E_PIPELINE_CANARY_ONLY` | `process_manager.py:61`; `indicator_strategy_config.schema.json:186-215`; each `config.json` |
| `metadata` is not part of `default_params`, so changing a readiness label does not change the immutable release payload or need a new semver | `runner_utils.build_strategy_core_parameters` |
| Retirement precedent: refuse while non-terminal work exists *or any position anywhere is non-zero*, then switch bindings off, deactivate `user_strategy_configs`, deactivate the strategy, deprecate the version, expire pending/approved feedback | `0098_retire_liquidity_leaders.py:44-152` |
| A protected position's resting stop-loss and take-profit rows sit in `pending_orders` with the strategy's ID and a non-terminal status (`working`) | `pending_orders.py:298`, `:321` |
| `published` is the only terminal outbox status; a dead letter stays until redriven | `dispatch.py:353` |
| `user_strategy_configs` has no runtime consumer outside the admin API, which already requires release to write one | `api.py:691-781` |
| Parameter feedback is approved for a separate source-controlled change; it never applies itself, and its service refuses an unreleased strategy | `feedback.py:131-136`; `strategy_feedback.py:51-68` |
| The Compose stack hard-codes `EXECUTION_MODE: paper` and `EXECUTION_ENGINE_ALLOW_LIVE: 'false'` for every service | `docker-compose.stack.yml:20-21` |
| The backend role holds only `SELECT` on `strategies`; a row-locking read would need `UPDATE` or a definer function | `0094_backend_control_plane.py:71`; `strategy_feedback.py:28-32` |
| `deployments` records each install/upgrade's `source_commit`, `source_dirty` and outcome | `models/deployments.py:24-52` |
| `api_audit_logs` is the audit table both the canary and the binding service write | `canary_activation.py:94-101`; `control_audit.py:24-53` |

Two conclusions carry the design.

**The evidence for a paper release is a reviewed human decision, made
checkable.** No machine attestation exists that a strategy could pass today and
that this repository treats as paper authority: the campaign says so in its own
contract, and the manifest depends on the release. The decision that does exist
is the catalogue's own readiness label, which lives in `config.json` and changes
only through a reviewed pull request. The command verifies that label, verifies
the database row is exactly the packaged release, and records both.

**No migration is required.** Release writes the two columns the canary already
writes under the maintenance role.

## 3. Approaches considered

1. **Reviewed readiness label plus exact identity (chosen).** The source must
   say `READY_FOR_PAPER_TRADING` (or `LIVE_CANDIDATE`) with a keep decision, in
   a committed `config.json`, and the database row must equal the packaged
   release. Cheap for the owner, reviewed, and the same vocabulary the
   staging/production runtime already enforces.
2. **Require a frozen `vmdev strategy validate` campaign.** Strongest evidence.
   Rejected as the default: the campaign contract itself says historical results
   cannot enable paper, it needs an isolated validation database and the TA-Lib
   venv, and no shipped strategy has a committed protocol today. It can be added
   later as an extra required input without changing the command's shape.
3. **An owner flag and reason only.** Simplest. Rejected: nothing reviewed stands
   behind it, so a typo releases the wrong strategy and the audit row records
   only that someone wanted it.
4. **A paper-scoped release in the database** (a new column checked at the
   execution boundary). Rejected: a migration plus a change at the broker-I/O
   gate, to restate what the unchanged live gate already guarantees.
5. **Generalize `activate-canary`.** Rejected: its rules are deliberately narrow
   and documented as not generalizing. Both commands share one core instead
   (section 4.5).

## 4. Design

### 4.1 Commands

```text
vmdev db release-strategy  --strategy-id ID --version SEMVER --check
vmdev db release-strategy  --strategy-id ID --version SEMVER --apply --reason TEXT
vmdev db withdraw-strategy --strategy-id ID --version SEMVER --check
vmdev db withdraw-strategy --strategy-id ID --version SEMVER --apply --reason TEXT
```

Exactly one of `--check` or `--apply` is required, as with `vmdev db
catalogue`. `--check` runs every precondition, prints the result and writes
nothing, not even an audit row. `--apply` requires a non-blank `--reason` (at
most 500 characters) and commits the change and its audit row in one
transaction. Both run under the maintenance stage (`MIGRATION_DATABASE_URL`,
`require_maintenance_database_role`). Output is one JSON object, as the canary
prints: `strategy_id`, `version`, `mode`, `changed` (whether this run wrote),
and the resulting `status` and `strategy_is_active`. `--check` reports the
current state, which it leaves unchanged, and adds `would_change`.

### 4.2 Release preconditions

Every check must pass. A failure writes nothing, exits non-zero and names the
check that failed.

Environment, resolved the way the database URL already is — the project `.env`
overlaid by the process environment — so the command checks the configuration
the stack actually runs with:

- `EXECUTION_MODE` is present and exactly `paper`, and
  `EXECUTION_ENGINE_ALLOW_LIVE` is present and exactly `false`. A missing value
  is a refusal, not a default.
- `ENV` and `ENVIRONMENT` are present and agree. Any of `dev`, `staging` or
  `production` is accepted; staging and production still need the promotion
  manifest at runtime.

Source, read from the checkout:

- Exactly one packaged release has this `strategy_id`, and its semver equals
  `--version` (`load_strategy_releases`, as the canary uses).
- `metadata.readiness` is in the paper-ready set (`READY_FOR_PAPER_TRADING`,
  `LIVE_CANDIDATE`). That set moves from `process_manager.py` into `lib_common`
  so the runtime gate and this command share one definition.
- `metadata.decision` is `KEEP_HIGH_PRIORITY` or `IMPROVE_THEN_VALIDATE`. This is
  an allowlist, so `INSUFFICIENT_EVIDENCE`, every `DISCARD_*`,
  `BENCHMARK_OR_COMPONENT_ONLY` and `E2E_PIPELINE_CANARY_ONLY` (Swing) are
  refused, and so is any value added to the schema later.
- The strategy's `config.json` is tracked by Git and identical to `HEAD`, so the
  label relied on is a committed one. `HEAD` is resolved once and the label,
  the file's SHA-256 and the strategy directory's tree are all read from that
  commit and recorded.
- The newest row in `deployments` finished (`succeeded` or `rolled_back`, not
  an in-flight or interrupted `failed`), the latest `succeeded` row has
  `source_dirty = false`, and the strategy directory's Git tree at its
  `source_commit` equals the tree at `HEAD`. This proves the running image ships
  the exact source and label being released; the parameter identity check alone
  cannot see a changed `core.py` at the same semver. `vmdev deploy` marks a
  deployment dirty when the checkout has any uncommitted or untracked file, so
  a release needs a deployment made from a clean checkout. The deployment ID is
  recorded.
- `VM_DEPLOY_IMAGE_TAG`, which Compose runs, equals that deployment's
  `image_tag`, so an image started by other means cannot pass on a stale
  record.

The check holds at release time. A later deployment that changes the strategy's
source at the same semver does not revoke the release; changed source ships as
a new semver, and the recorded `strategy_tree` shows what was released.

Database, under the catalogue advisory lock with the strategy and version rows
locked `FOR UPDATE`:

- The maintenance role is in use and the deployment owner resolves.
- The strategy and the exact version row exist, and `strategy_name`,
  `asset_class`, `default_params` and `param_schema` equal the packaged release.
- The version is `registered`. If it is already `active` with the strategy
  active, the command is an idempotent no-op (`changed: false`, no audit row).
  `deprecated` and `pulled` are refused: ship a new semver.
- No other version of this strategy is `active`. Upgrading from 1.0.0 to 1.1.0
  means withdrawing 1.0.0 first, so signal attribution never depends on release
  order.

Effect: `strategy_versions.status = 'active'`, `strategies.is_active = true`,
one `api_audit_logs` row with action `strategy.release`.

### 4.3 Withdraw

Withdrawal is the undo. Its job is to stop a version producing new work without
stranding work in flight, because execution refuses exits of an unreleased
version too.

Inputs are the database rows only. Withdraw does not load the packaged source,
so 1.0.0 can still be withdrawn after the checkout has moved to 1.1.0, or after
its readiness label has been lowered.

Preconditions, all under the catalogue lock with the strategy and version rows
locked:

- The same environment and maintenance checks as release.
- The version is `active`.
- No owner binding naming the strategy has `is_active`, `entries_enabled`,
  `exits_enabled` or `autopilot` set. The owner turns each one **Off** in the UI
  first.
- No non-terminal work for the strategy remains in `pending_orders`,
  `execution_logs` or `execution_decision_logs`, nor `pending`, `in_progress`
  or `failed` events in `outbox_events` (matched on the top-level or the
  signal's `strategy_id`), using the statuses `0098` lists, nor a `pending`,
  `running` or `blocked` row in `account_rebalance_plans`.
- The strategy holds no position: for every account and instrument, its signed
  canonical fills (`executions` through `orders` to `order_intents`, `BUY`
  positive) net to zero.

Two deliberate departures from `0098`:

- **Dead letters do not block.** One dead letter would otherwise block withdraw
  until someone redrives it. Redriving it after withdrawal fails at the release
  gate, so it cannot trade. The output reports how many remain.
- **Only this strategy's positions block.** `0098` refuses on any non-zero
  `positions` row in the installation because that projection carries no
  strategy attribution; copied here, one open position in any strategy would
  make every withdraw impossible. The canonical fill ledger does carry it, so
  withdraw nets this strategy's own fills instead. Withdrawing with a position
  open would strand it, because execution refuses exits of an unreleased
  version. The owner's path is **Close only**, wait until flat, **Off**, then
  withdraw.

Effect: `strategy_versions.status = 'registered'`, `strategies.is_active =
false`, one audit row with action `strategy.withdraw`. Because the version
returns to `registered`, it can be released again later through the same gates.
Permanent retirement (`deprecated`) stays a migration, as `0098` does it.

Withdraw leaves `user_strategy_configs` and parameter feedback untouched.
Neither has a runtime effect on its own: configs have no consumer outside the
admin API, which requires release to write one, and feedback never applies
itself and is refused for an unreleased strategy.

### 4.4 Audit

One `api_audit_logs` row per changed state, in the same transaction, owned by the
deployment owner:

- Release `req`: `strategy_id`, `version`, `reason`, `source_commit`,
  `config_sha256`, `strategy_tree`, `readiness`, `decision`, `deployment_id`.
- Withdraw `req`: `strategy_id`, `version`, `reason`.
- Both `resp`: `environment`, `execution_mode`, `allow_live: "false"`,
  `previous_status`, `status`, `previous_strategy_is_active`,
  `strategy_is_active`; withdraw adds `dead_letter_count`.

A structured log line carries the same fields. Refusals are not audited: they
change nothing, and `--check` exists to show them.

### 4.5 Where the code goes

- `lib_application/services/strategy_release.py`: `release_strategy_version` and
  `withdraw_strategy_version`, taking a validated source object and the three
  environment values; the caller commits. The exact-row lookup and identity
  comparison move here from `canary_activation.py`, which then calls them, so
  there is one implementation of "the row is exactly the packaged release".
- `dev_cli/core/strategy_release.py`: loads and validates the packaged source,
  the readiness/decision labels, and the Git facts.
- `dev_cli/commands/db_release.py`: the two Click commands, registered under
  `vmdev db` next to `activate-canary`.

### 4.6 A bind racing a withdraw

A bind that reads the strategy just before a withdraw commits can leave an
active binding on a withdrawn strategy. That binding is inert — scoring refuses
its signals and execution refuses its orders — and the readiness checklist shows
it as not released, so the race fails closed. It is accepted rather than fixed:
a `FOR SHARE` read in `require_release` would need an `UPDATE` grant or a
definer function for the backend role, which is a migration, and a shared
advisory lock would add a cross-process lock to every bind for a race between
two actions only the owner can take. `binding_control.py` is not changed.

## 5. The owner's path, after this change

1. In a pull request, set the strategy's `metadata.readiness` to
   `READY_FOR_PAPER_TRADING` and its `decision` to a keep value, and update its
   row in `STRATEGY_READINESS.md`. To run it, also set `enabled` and its
   `environments`. Merge.
2. `vmdev deploy`. Registration happens in bootstrap; on an existing database,
   `vmdev db catalogue --apply`.
3. `vmdev db release-strategy … --check`, then `… --apply --reason "…"`.
4. The UI shows the strategy as Released. Bind it to a paper account and pick a
   mode.
5. Name it in `STRATEGY_LIST` so a signal worker runs it.

## 6. Why this cannot affect live authority

- It writes only `strategies.is_active`, `strategy_versions.status` and one
  audit row — the columns the canary already writes. It never writes the live
  gate, a broker environment, an account, a binding or a credential; withdraw
  only reads bindings, work queues and the fill ledger to refuse.
- It refuses to run unless the resolved configuration says
  `EXECUTION_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false`. Independently,
  the Compose stack hard-codes both values for every service.
- A release is necessary for any trading, never sufficient. Live execution
  additionally needs `EXECUTION_ENGINE_ALLOW_LIVE=true` at the execution engine,
  a `LIVE_CANDIDATE` label outside dev, and a live broker account. None of these
  is touched, and this migration grants none of them.
- The database release is not itself scoped to a mode. That is acceptable only
  because the live gate stays closed. Arming it is a separate owner
  authorization this change neither makes nor eases.

## 7. Tests

- Service tests (SQLite, as `tests/test_canary_activation.py`): release and its
  audit row; idempotent repeat; each refusal in section 4.2; withdraw and its
  audit row; each withdraw refusal; release again after withdraw; the canary
  still activates Swing and still refuses everything else.
- Source-loader tests: readiness, decision, a modified or untracked
  `config.json`, an ambiguous or mismatched version, Swing refused.
- CLI tests: maintenance stage used and committed; `--check` writes nothing;
  `--apply` without `--reason` refused; `ENV`/`ENVIRONMENT` disagreement and each
  live/paper gate refused.
- Gate tests: after release, `require_release` accepts the strategy and
  `_released_strategy_ids` includes it; after withdraw, neither does.

## 8. Documentation

- `STRATEGY_READINESS.md`, step 3 of "From a shipped file to a visible, tradable
  strategy": the release command, its evidence and its undo.
- `DATABASE.md`: the registration/activation paragraph names the new command
  next to `activate-canary`.
- `QUICK_REFERENCE.md`: the two commands.
- `CHANGELOG.md`: an `Unreleased` entry.

## 9. Left to the builder

Message wording, JSON key order, how Git facts are read, and the exact
non-terminal status lists (copied from `0098`, not invented).

## 10. Decisions

The owner decided (2026-10-24):

1. **A reviewed readiness label is enough evidence**: `READY_FOR_PAPER_TRADING`
   (or `LIVE_CANDIDATE`) with a keep decision, in a committed `config.json`,
   plus the exact identity check. A frozen `vmdev strategy validate` campaign is
   not required.
2. **A release must match the deployed image** (section 4.2): it is possible
   only on an installation deployed with `vmdev deploy` from a clean checkout,
   after the labelled commit is deployed.

Calls made in this design:

- Withdraw returns the version to `registered`, so it is reversible.
- One active version per strategy; an upgrade withdraws the old version first.
- The keep decisions are `KEEP_HIGH_PRIORITY` and `IMPROVE_THEN_VALIDATE`.
- The paper-ready set is shared with the runtime gate, so `LIVE_CANDIDATE` is
  accepted; it arms nothing.
- `dev`, `staging` and `production` are all accepted with paper mode and the
  live gate false.
- Dead letters do not block withdraw; this strategy's net positions, resting
  orders and unfinished rebalance plans do. Positions in other strategies do
  not.
- Withdraw leaves `user_strategy_configs` and feedback untouched.
- The bind/withdraw race is accepted as fail-closed; no change to
  `binding_control.py`.
- `--check` and refusals write no audit row.
