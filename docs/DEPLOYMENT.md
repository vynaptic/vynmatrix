# Local Docker and Release Boundary

vynmatrix is a self-hosted independent migration. No cloud infrastructure,
registry, production environment, or broker certification is provisioned by this
source tree. The source is available under [LICENSE](../LICENSE) for personal,
noncommercial use; see [NOTICE](../NOTICE) for retained attribution and
third-party-material boundaries. Source publication does not authorize deployment,
release, or live trading.

## What this repository contains

| Surface | Source of truth |
|---|---|
| Local component and wheel inventory | [config/build.yaml](../config/build.yaml) |
| Service image inventory | [config/containers.yaml](../config/containers.yaml) |
| Dependency lock | [docker/constraints.txt](../docker/constraints.txt) |
| Local runtime topology | [docker/docker-compose.stack.yml](../docker/docker-compose.stack.yml) |
| Runtime configuration contract | [CONFIGURATION.md](CONFIGURATION.md) |
| CI and optional image publication | [.github/workflows/](../.github/workflows/) |

Earlier documentation described external infrastructure. That repository and its
runtime authority are not included in vynmatrix. The canonical source repository
is [`vynaptic/vynmatrix`](https://github.com/vynaptic/vynmatrix), but no cloud
budget, deployment host, or disaster-recovery result should be inferred from the
inherited technical history.

## Local image build

Run from the prepared `.venv-dev` environment:

```bash
vmdev build libs
vmdev build strategies
vmdev build docker --from-config --tag latest
```

The Docker command checks the explicit inventory, Dockerfiles, declared wheels,
and wheel freshness before building `vynmatrix/platform`. Its intermediate
`vynmatrix/svc-base` holds pinned shared dependencies; the platform profile adds
its declared extras, six library wheels and the indicator strategy wheel, then
runs `pip check`. The platform image contains backend, scoring, execution,
feedback, market-data and indicator entrypoints plus the maintenance CLI.
Presence in the image does not activate a strategy.

## Local Compose topology

The same single-owner topology can run locally or on one separately approved
private host. Prepare `.env` from [`.env.example`](../.env.example), then follow
[the database lifecycle](DATABASE.md) for bootstrap and startup.

| Layout | Configuration | Running containers |
|---|---|---|
| Recommended split | `COMPOSE_PROFILES=workers`, `PLATFORM_APPLICATION_GROUP=application` | PostgreSQL + `application` + `workers` |
| Small combined installation | Empty `COMPOSE_PROFILES`, `PLATFORM_APPLICATION_GROUP=all` | PostgreSQL + `application` running all selected processes |

The application group supervises backend, scoring with its inline transactional
outbox relay, and execution. Workers always include the feedback daemon.
`PLATFORM_WORKERS` explicitly selects feeds, FX and calendars; `STRATEGY_LIST`
explicitly selects indicator strategies. Empty selections start no optional
producer. All children retain separate interpreters and role-specific credentials.
There is no Redis, standalone relay, pgAdmin or additional scheduler container.

Only backend `127.0.0.1:8081` and PostgreSQL `127.0.0.1:5432` are published by
default; their host ports are configurable. A private cloud baseline uses an
SSH tunnel to backend loopback, without an added load balancer or TLS service.
A future UI should serve static assets through backend in the same image.

An IBKR Client Portal Gateway is an explicit external dependency, including
interactive authentication and gateway/TLS requirements in
[BROKER_CREDENTIALS.md](BROKER_CREDENTIALS.md). A separately approved gateway
container consumes the third slot and therefore requires the combined layout.
An owner-operated external gateway must be configured explicitly; this stack
neither supplies nor provisions one.

Read-only inspection uses the existing service identities:

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml ps
docker compose --env-file .env -f docker/docker-compose.stack.yml logs --tail 100 application workers
```

## Database bootstrap and operational bounds

Use the supported `vmdev db` lifecycle described in [DATABASE.md](DATABASE.md).
Bootstrap stops both runtime groups, verifies they have exited, starts PostgreSQL,
runs the declared maintenance `bootstrap` service with `--rm --no-deps`, and waits
for success before restarting the selected groups. Administrative and migration
credentials belong only to maintenance. Safe reference catalogue reconciliation
and explicit owner designation replace broad demo/commercial seeding.

The three-container limit covers supported lifecycle operations and explicit jobs
inside existing containers. Raw `docker compose --profile '*' up`, parallel
maintenance jobs, extra services or manual `run` commands bypass that contract.
Do not use them as an alternative startup path. Build tools create images, not
extra running services. A failed maintenance stage leaves runtime stopped for
review and a bounded retry.

The supervisor exposes `/health`, `/ready`, `/status` and `/metrics` internally
on `8090`; component metrics are available through `/metrics/<component>`.
Management can stay healthy while owner setup or a selected feed is unready.
Trading readiness separately checks source progress, outbox age/dead letters,
durable paper orders, reconciliation and feedback heartbeats. See
[CONFIGURATION.md](CONFIGURATION.md) for the complete internal port and key map.

The explicit jobs `python -m scripts.run_platform job backfill` and
`python -m scripts.run_platform job quality-compounder` run with `compose exec`
in `workers` (or `application` in the combined layout). They require their
reviewed source/owner settings, accept `--timeout-seconds` (default 3600,
maximum 86400), and prevent same-job overlap within that container. There is
no automatic scheduler. File locks assume the single prescribed worker group;
multiple independent worker containers are outside this topology.

PostgreSQL persists in `postgres-data`; `Data` is mounted at `/data`, and
`.artifacts` is mounted read-only at `/app/.artifacts`. Retain encrypted-secret
key-ring recovery material separately from database backups. Take and verify a
backup before upgrade; keep the previous image and apply only reviewed migration
rollback procedures. Recreating empty retired tables cannot recover disposed data.

Logs rotate at 10 MB with three files per container by default. Pools default
to at most five connections per process. Compose grants 60 seconds for shutdown;
the supervisor forwards termination, reaps process trees and bounds group cleanup
to 55 seconds. Readiness, restore and restart evidence must be checked after an
upgrade. Unit/contract checks do not establish that PostgreSQL acceptance or the
new image build has passed.

## Optional future release workflow

[build-and-push.yml](../.github/workflows/build-and-push.yml) is a manual
`workflow_dispatch` image build/publication path. Publication is
opt-in (`publish=false` by default); pushing a tag is not an automatic registry
publication instruction. A configured destination must explicitly provide
`DO_REGISTRY_NAME` and the registry credential. No registry destination is
configured by this repository.

The retained release checks require a strict SemVer tag, a matching dated
changelog section, the current `origin/main` commit, and successful CI for that
exact commit. These technical gates do not resolve licensing, authorize a push,
or deploy a service. Review rights, credentials, destination ownership, and
workflow permissions before any separately authorized publication. Deployment
would also need independently reviewed manifests, secret provisioning, backups,
restore evidence, and rollback instructions.

## Paper acceptance is separate from live authority

The [E2E guide](E2E_VERIFICATION_GUIDE.md) describes recorded-data checks for
bootstrap suppression, fresh signals, stale-signal rejection, explicitly
bounded paper replay, durable orders/fills, restart accounting, and feedback.
A green unit suite or running container cannot substitute for that evidence.

Swing is an `E2E_PIPELINE_CANARY_ONLY` development fixture, permanently excluded
from paper promotion and live trading. The narrow maintenance canary activation
in [DATABASE.md](DATABASE.md) only enables its exact registered dev-only release;
it creates no account/binding authority and does not grant deployment eligibility.

Paper promotion manifests bind the exact image/config/evidence, strategy version,
user, binding, broker account, data scope, and instrument set. Portfolio
manifests also bind the model configuration. They record `live_authority=false`.
The image repository is `vynmatrix/platform`; the logical `indicator-runner`
attestation role does not imply a separate image or container. Retired-image
evidence is rejected and cannot be relabeled for the consolidated image.
Inherited marker-writing utilities and broker certification descriptions are
technical references only; no certificate or live authority is supplied here.
Keep `EXECUTION_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false` throughout
this migration and its verification.
