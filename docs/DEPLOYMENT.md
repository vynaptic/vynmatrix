# Local Docker and release boundary

vynmatrix supports a self-hosted local paper runtime. This document owns the
Compose topology and lifecycle envelope. It does not authorize cloud deployment,
broker execution, paper promotion, or live trading.

## One command

Installing and upgrading are the same command:

~~~text
vmdev deploy
~~~

It detects whether the database exists and chooses a fresh install or an
upgrade. The install sequence itself lives in [SETUP.md](../SETUP.md), which is
its single owner; configuration keys are in
[CONFIGURATION.md](CONFIGURATION.md). This document owns what happens to a
deployment that already holds data.

## The stamped artefact

Nothing about a running stack may be implicit:

- The build receives the git commit as a build argument and records it as image
  labels and `/app/BUILD_INFO.json`, alongside the build time and the exact base
  image. `.dockerignore` excludes `.git`, so the commit can only arrive this way.
- Images are tagged `vynmatrix/platform:sha-<commit12>`. A working tree with
  uncommitted changes is allowed — it is how the maintainer works — and is
  tagged `sha-<commit12>-dirty`, recorded as dirty and shown as such in the UI.
- `deploy` writes the exact tag into `.env` as `VM_DEPLOY_IMAGE_TAG`. Compose
  resolves no moving tag: it fails closed when that variable is empty. `latest`
  survives only as a local alias and is never what Compose names.
- `vynmatrix/svc-base` is built under the same immutable tag and passed to the
  platform build as `VM_SVC_BASE_REF`, so a base image cannot move underneath a
  build. A locally built image has no registry digest to pin, so the image id
  is what the deployment record keeps.
- The previous two generations are retained; older stamped generations are
  pruned. That is the rollback target the procedure below assumes.

## The deployment record

`vmdev deploy` writes one row per run to the `deployments` table under
migration authority: the image tag and digest, the source commit and whether it
was dirty, the schema head, the start and finish times, the outcome and the
snapshot it took. The row is opened with `outcome = failed` before the first
destructive stage, so an interrupted run is visibly incomplete rather than
absent.

It answers "which build is live?". `GET /api/ui/version` returns it behind the
admin key and the owner UI footer shows the commit, the schema head and a dirty
marker. The record is written by the deploy tool, so a deployment performed by
other means leaves it stale: `vmdev doctor` reports that disagreement rather
than trusting the row.

## Upgrade and rollback

Stage order, with the runtime stopped only for the part that needs it:

| # | Stage | Effect |
| ---: | --- | --- |
| 1 | preflight | `doctor` checks, including the configuration-key diff |
| 2 | build | wheels and image, when the source commit differs from the record |
| 3 | snapshot | `pg_dump` through the existing PostgreSQL container |
| 4 | record | open the row as `failed` |
| 5 | stop | stop the application and worker groups |
| 6 | provision | migrate transactionally, then roles, catalogue and owner |
| 7 | account | create the local paper account (fresh install only) |
| 8 | start | point Compose at the new tag and start |
| 9 | verify | services healthy and the UI answering, then close the record |

The snapshot is taken on every upgrade, not only schema ones: a full dump of a
local stack measures about three seconds and forty megabytes. The last five are
kept under `.artifacts/deployments/`. `--skip-snapshot` is an explicit, recorded
choice that also disables automatic rollback.

If any stage from 5 onward fails, `deploy` provisions the runtime roles (a
dump's grants cannot resolve without them), restores the snapshot, re-points
`VM_DEPLOY_IMAGE_TAG` at the previous successful record, starts that image, and
closes the record as `rolled_back` with the stage that failed.

It refuses to restore automatically when the snapshot is missing or is not a
PostgreSQL custom archive. It stops instead, with the database untouched and an
explicit message, because a bad restore is worse than a stopped stack. Inspect
the installation and use the explicit [DATABASE.md](DATABASE.md) restore path.

## Supported topology

| Layout | Running containers | Use |
| --- | ---: | --- |
| Split | PostgreSQL, application, workers | Default local runtime |
| Combined | PostgreSQL, one all application group | Compact local runtime |
| Bootstrap maintenance | PostgreSQL plus the declared bootstrap job while application groups are stopped | Fresh install or controlled bootstrap repeat |

The limit is three running containers including PostgreSQL. The bootstrap job
uses a vacated application slot and exits before runtime groups restart. Bounded
jobs, diagnostics, and administrative actions run with compose exec in an
existing group; they do not justify another container.

An authenticated IBKR gateway is an external dependency. If an owner approves a
gateway container, it must use the combined application layout and consumes the
third slot. PostgreSQL, pgAdmin, a scheduler, an outbox relay, or feedback
cannot be hidden as additional services; their responsibilities are already
inside the declared groups.

## Lifecycle and supervision

Use the deployment and database lifecycles rather than arbitrary Compose
profiles:

~~~text
vmdev deploy --start-only
vmdev doctor
vmdev db status
vmdev db stop
~~~

`vmdev db bootstrap --owner-config owner.local.yaml` remains the explicit
single-stage path for a controlled bootstrap repeat; `vmdev deploy` orchestrates
it together with the build, snapshot, record and verification stages. Both stop
application groups, verify the maintenance window, run the declared job, remove
it, and start only selected groups. The full database, role, migration, repeat,
backup, and restore contract is in [DATABASE.md](DATABASE.md).

The application and workers groups each run a supervisor that starts and stops
their child processes together. Startup waits for PostgreSQL and selected child
configuration; readiness requires actual component progress, while health only
proves process liveness. Inspect declared status, health, and logs through the
existing groups:

~~~text
docker compose --env-file .env -f docker/docker-compose.stack.yml ps
docker compose --env-file .env -f docker/docker-compose.stack.yml logs --tail 100 application workers
docker compose --env-file .env -f docker/docker-compose.stack.yml exec -T application <command>
docker compose --env-file .env -f docker/docker-compose.stack.yml exec -T workers <command>
~~~

These examples use the split layout. In the combined layout, omit workers from
the logs command and run worker work with exec against application.

The supervisor forwards termination and bounds group shutdown. A failed
maintenance stage leaves runtime stopped for inspection rather than starting an
incomplete platform.

## Persistence, networking, and secrets

PostgreSQL data is persisted in the declared volume. Keep database archives and
the separate encryption-key ring under owner control; a database dump cannot
recreate encrypted credentials. Runtime logs use the configured Docker or host
collection mechanism. Do not commit .env, owner configuration, credentials, or
evidence artifacts.

Only the declared loopback PostgreSQL and backend listeners are published.
Internal service, worker, and supervisor listeners remain in their Compose
network. Use an owner-controlled SSH tunnel for a private remote backend rather
than publishing a control-plane or database port. See
[CONFIGURATION.md](CONFIGURATION.md) for scoped database URLs, child
environments, secret-key rings, ports, and readiness settings.

## Recovery boundary

`vmdev deploy` takes and verifies the snapshot, retains the prior image and
leaves the encryption-key ring alone — a database dump cannot recreate encrypted
credentials, so keep `SECRETS_MASTER_KEYS` under separate owner control. Never
remove volumes to resolve a startup failure. A manual restore leaves the runtime
stopped so the owner can verify the intended database and grants before
restarting. [DATABASE.md](DATABASE.md) is the source for the manual steps;
[RUNBOOK.md](RUNBOOK.md) contains incident actions.

## Paper verification and future release

Keep EXECUTION_MODE=paper and EXECUTION_ENGINE_ALLOW_LIVE=false. A running
stack, image build, or green unit test is not pipeline, broker, strategy, or
release evidence. The recorded-data acceptance procedure is
[E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md); account and credential
boundaries are in [BROKER_CREDENTIALS.md](BROKER_CREDENTIALS.md).

Any future cloud host, registry, public endpoint, external scheduler, gateway,
or live authority is a separate owner decision. It must declare its external
infrastructure, preserve the container budget or explicitly change it, and
produce new matched evidence.
