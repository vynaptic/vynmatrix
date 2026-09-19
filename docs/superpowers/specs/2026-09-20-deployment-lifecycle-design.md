# Deployment lifecycle: one command, stamped artefacts, recoverable upgrades

Status: accepted for implementation (2026-09-20). Scope: how this repository is
installed, upgraded and identified. It does not change what the platform does
once running.

## 1. Goal

Three scenarios must work with the same tooling and the same mental model.

| Scenario | Who | What changes | What must be protected |
| --- | --- | --- | --- |
| A | The maintainer, iterating on a stack that holds real paper history | Own code, often uncommitted, sometimes schema | Existing data; a fast loop |
| B | The maintainer adopting someone else's merged work | Code, schema, configuration keys, catalogue entries | Existing data; knowing what will change before it does |
| C | A new user cloning the repository | Everything, from nothing | Nothing yet; the cost is confusion, not loss |

Today A and B share a path that is broken as documented, and C takes sixteen
steps with twenty-three hand-edited configuration lines. All three should be one
command that is safe to re-run.

Non-goals: publishing images to a registry (the existing workflow is owner-gated
and stays that way), multi-host or cloud deployment, changing the three-container
topology, and any change to trading authority. Releasing a strategy for trading
is deliberately left to the strategy-control project.

## 2. Evidence that shapes the design

| Fact | Source |
| --- | --- |
| Compose references `vynmatrix/platform:${VM_DEPLOY_IMAGE_TAG:-latest}` and the platform image builds `FROM vynmatrix/svc-base:latest` | [docker-compose.stack.yml](../../../docker/docker-compose.stack.yml), [platform_runtime.Dockerfile](../../../docker/platform_runtime.Dockerfile) |
| Nothing stamps a commit or version into the image, and no endpoint reports what is running | `.dockerignore` excludes `.git/`; image labels carry ownership only |
| `vmdev build docker` deletes the superseded image, while the runbook tells the operator to retain it for rollback | `docker_build` cleanup of owned dangling images; [DEPLOYMENT.md](../../DEPLOYMENT.md), [RUNBOOK.md](../../RUNBOOK.md) |
| Bootstrap commits in five independent stages; only the migration is transactional | `tools/dev_cli/dev_cli/core/bootstrap.py` |
| `vmdev db migrate` and `vmdev db catalogue` read connection strings from the process environment, while `.env` names the container host `postgres:5432` | `tools/dev_cli/dev_cli/commands/db.py` |
| `.env.example` carries 23 mandatory edit points holding 15 distinct secrets, and nothing in the repository generates any of them | `.env.example`; no `Fernet.generate_key` or `token_urlsafe` anywhere outside libraries |
| `vmdev build venvs` builds 803 MB of host virtualenvs that the image never reads, and now pulls TA-Lib, which no setup document lists | `config/build.yaml`; `platform_runtime.Dockerfile` copies only `build/wheels/*.whl` |
| A full `pg_dump` of the local stack takes 3 seconds and 40 MB | measured on the running stack, 2026-09-20 |
| A merged change can leave the catalogue stale, which hides shipped strategies from the UI | the ported-strategies incident, 2026-09-19 |

## 3. Approaches considered

1. **Orchestrate the existing pieces behind one command (chosen).** Keep the
   build, migration, role, catalogue and owner stages that already work and are
   well tested, and add the two things missing: a generator for configuration,
   and a supervisor that stamps, snapshots, verifies and can roll back.
2. **Make Compose the orchestrator**, with a migration service and health
   dependencies. More declarative, but it cannot generate secrets, cannot take a
   snapshot, and discards the host-side preflight that already catches bad
   configuration before anything starts.
3. **Full release-artefact model**, where a build produces a manifest and
   `deploy` applies it. The strongest reproducibility, and the right answer if
   images were ever published for others to run. Too much machinery for a
   single-owner local stack; its useful half, a record of what is deployed, is
   adopted below.

## 4. Commands

Four commands, of which scenarios A and B use only the last two.

- **`vmdev init`** writes `.env` and `owner.local.yaml`. It generates all fifteen
  secrets, using `secrets.token_urlsafe` for keys and `Fernet.generate_key` for
  the secrets ring, distinct per role, URL-encoded where they are embedded in a
  connection string. It prompts only for what a machine cannot know: owner email,
  base currency, timezone and starting paper equity, each available as a flag for
  non-interactive use. It never overwrites an existing file, and validates what it
  wrote before exiting.
- **`vmdev init --update`** adds configuration keys that appeared in
  `.env.example` since the file was written, generating values for new secrets and
  leaving every existing value untouched. This is scenario B's silent failure made
  visible.
- **`vmdev doctor`** validates configuration, prerequisites and the running state
  without changing anything: missing or extra `.env` keys, unreadable Docker,
  wrong Python, an unreachable database, a schema head the image does not match.
  It runs first inside `deploy`, so bad configuration fails in seconds rather than
  after the slowest build.
- **`vmdev deploy`** is one idempotent command for all three scenarios. It
  detects whether the database exists and chooses a fresh install or an upgrade.
  `--start-only` brings a stopped stack back up, which nothing does today.

  `--plan` prints what would happen and exits. This is how scenario B inspects
  someone else's merged work before applying it: the deployed record against the
  target commit, the Alembic revisions that will run, whether wheels and the image
  must rebuild, configuration keys that appeared or disappeared, and the snapshot
  that would be taken. It is also the dry run the acceptance tests assert on.

## 5. Immutability: a stamped artefact

- The build receives the git commit as a build argument and records it as an
  image label and a file inside the image, alongside the build time.
- Images are tagged `vynmatrix/platform:sha-<commit12>`, matching the convention
  the publish workflow already uses. The repository carries no SemVer version
  today: `CHANGELOG.md` uses date headings, and `vmdev release tag` requires a
  `## [X.Y.Z]` entry that does not exist. A release build additionally applies the
  `X.Y.Z` tag when one is cut. `latest` remains only as a local convenience alias
  and is never referenced by Compose.
- `deploy` writes the exact tag it built or selected into `.env` as
  `VM_DEPLOY_IMAGE_TAG`, so Compose always names one immutable tag.
- `vynmatrix/svc-base` is pinned by digest in `platform_runtime.Dockerfile`,
  removing the last moving base reference.
- A dirty working tree is allowed, because scenario A deploys from one
  constantly. The image is then tagged `sha-<commit12>-dirty`, the record
  says so, and the UI shows it. Only a tagged release build refuses a dirty tree.
- The build stops deleting the superseded image and retains the previous two,
  because that is the rollback target the runbook already assumes.

## 6. The deployment record

A new table, `deployments`, written by `deploy` under migration authority and
read by the backend:

| Column | Meaning |
| --- | --- |
| `deployment_id` | surrogate key |
| `image_tag`, `image_digest` | exactly what Compose was pointed at |
| `source_commit`, `source_dirty` | what the image was built from |
| `alembic_head` | the schema the code expected |
| `started_at`, `finished_at`, `outcome` | `succeeded`, `rolled_back` or `failed` |
| `snapshot_path` | the pre-upgrade dump, or null with a recorded reason |

It answers "which build is live?", which is unanswerable today. `vmdev db status`
prints the latest row, and `GET /api/ui/version` returns it behind the existing
admin key so the UI footer can show the commit, the schema head and a dirty
marker. Migration `0108` creates the table and grants the backend role
column-level `SELECT`, following the pattern established by `0107`.

Drift detection falls out of it: the running container's image digest, the latest
record and the live Alembic head can be compared in one place, and `doctor`
reports any disagreement.

## 7. The upgrade, and how it rolls back

Stage order, with the runtime stopped only for the part that needs it:

1. `doctor` preflight, including the configuration-key diff.
2. Build wheels and the image if the source commit differs from the record.
3. Snapshot with `pg_dump` through the existing PostgreSQL container, so the
   three-container limit holds. Measured at 3 seconds and 40 MB on the current
   stack, so it is taken on every upgrade, not only schema ones. Retention keeps
   the last five, and `--skip-snapshot` is an explicit, recorded choice.
4. Open the record with `outcome = failed`, so an interrupted run is visibly
   incomplete rather than absent.
5. Stop the runtime groups.
6. Migrate, transactionally, as today.
7. Provision roles, reconcile the catalogue, reconcile the owner.
8. Point Compose at the new tag and start the runtime.
9. Verify health, then close the record as `succeeded`.

If any stage from 5 onward fails, `deploy` restores the snapshot, re-points
`VM_DEPLOY_IMAGE_TAG` at the previous successful record, starts that image,
closes the record as `rolled_back`, and reports the failing stage and its error.
It refuses to auto-restore when the snapshot is missing or fails verification,
stopping instead with the database untouched and an explicit message, because a
bad restore is worse than a stopped stack.

## 8. Fresh install and first-run state

With no database, `deploy` creates it, migrates from zero, provisions roles,
registers the catalogue, creates the owner from `owner.local.yaml`, and creates a
local paper broker account with its starting equity through the existing
onboarding service, never by raw SQL. No snapshot is taken and the record says
why. It finishes by printing the URL and where to find the admin key.

It stops there. No strategy is released or bound, so installing grants no trading
authority; the Strategies page shows the catalogue as registered and not
released, which is the honest state.

## 9. Friction removed

- `vmdev build venvs` leaves the deployment path entirely. It is a contributor
  step, and the setup guide says so, along with its TA-Lib prerequisite.
- `deploy` resolves host versus container addressing itself, so no user ever
  hand-exports a rewritten `MIGRATION_DATABASE_URL`.
- The install sequence is duplicated across five documents. `SETUP.md` becomes
  its single owner; the others link to it. `DEPLOYMENT.md` owns the upgrade and
  rollback contract.

## 10. Testing and acceptance

- **Unit:** secret generation shape and distinctness, URL encoding inside
  connection strings, `.env` rendering and the key diff, record open and close,
  and the rollback decision table against a faked container and database,
  including the refusal when a snapshot is missing.
- **PostgreSQL integration:** migrate-from-zero on a scratch database; an upgrade
  with a forced failure after the migration that must leave the schema head, the
  data and the image tag as they were, with the record marked `rolled_back`.
- **Contract:** Compose references no moving tag; Dockerfiles pin bases by
  digest; the deploy path never invokes the venv build; `0108` grants read-only
  access following `0107`'s column-scoped pattern.
- **Acceptance:** on this machine, a fresh install into a scratch database
  reaching a live UI, and an upgrade of the real stack that preserves its history.
  `vmdev audit --strict`, the affected suites and CI.

## 11. Risks

Restoring a dump into a cluster whose roles are missing fails on grants, so the
restore path provisions roles before restoring. Snapshot retention grows with
deploy frequency, which the five-file limit bounds. The record is written by the
deploy tool rather than by the application, so a deployment performed by other
means leaves it stale; `doctor` reports that rather than trusting it.
