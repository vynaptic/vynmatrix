# Local Docker and Release Boundary

vynmatrix is a local independent migration. No cloud infrastructure, registry,
production environment, or broker certification is provisioned by this source
tree. The project is not yet open-source: [LICENSE](../LICENSE) is unchanged and
a license/rights decision is pending. Publishing, pushing, releasing, and
deploying require a separate owner decision; this document authorizes none of them.

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
runtime authority are not included in vynmatrix. No public repository owner,
cloud budget, deployment host, or disaster-recovery result should be inferred
from the inherited technical history.

## Local image build

Run from the prepared `.venv-dev` environment:

```bash
vmdev build libs
vmdev build strategies
vmdev build docker --from-config --tag latest
```

The Docker command checks the inventory, Dockerfiles, declared wheels, and wheel
freshness before contacting the daemon. The five service images are:

- `vynmatrix/scoring-engine`
- `vynmatrix/execution-engine`
- `vynmatrix/feedback-loop-engine`
- `vynmatrix/market-data-ingestor`
- `vynmatrix/indicator-runner`

They derive from the local multi-stage `vynmatrix/svc-base`, containing the
shared pinned third-party dependencies. Service builders add only declared local
wheels and extras, run `pip check`, and copy the result into clean runtime
stages. The indicator image includes `vynmatrix_indicator`; it does not select
strategies automatically. Buildx receives the just-built base as a named image
context so it does not need to pull a private intermediate image.

The backend, database/bootstrap one-shots, and optional outbox relay reuse the
scoring image. FX, calendar writers, and historical/equity one-shots reuse the
market-data image. They are separate processes with their own configuration,
health, and ownership boundaries, not additional registry artifacts.

## Local Compose topology

Prepare `.env` using the [OS setup guides](../README.md#new-developer-onboarding).
Keep paper execution and the disabled live gate explicit.

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml up -d

# Optional exact development canary selection (Bash syntax)
STRATEGY_LIST=SwingHighLowPMO \
docker compose --env-file .env -f docker/docker-compose.stack.yml --profile indicator up -d

docker compose --env-file .env -f docker/docker-compose.stack.yml ps
```

The default outbox relay runs inline in scoring; feedback runs repeated one-shot
`evaluate` invocations. The optional `standalone-relay` profile plus
`SCORING_OUTBOX_RELAY_INLINE=false` and `FEEDBACK_RUN_MODE=daemon` exercises an
alternative topology. Neither configuration implies a deployed cloud service.

The independent FX process uses internal port `8004`. Optional official calendar
writers use `8005` and require exact selectors, catalogue mappings, credentials,
and backend admin authority. Only one calendar writer may own each instrument.
Leave optional profiles disabled until their prerequisites are reviewed.

Use `docker compose ... exec <service>` for inspection; container names are
assigned by the Compose project. `down` stops this local stack; adding `--volumes`
would delete database state and is not part of ordinary teardown.

## Database bootstrap and operational bounds

The declared Compose bootstrap chain runs migrations, seed convergence, and
runtime-role provisioning before dependent services start. The shared
[scripts/db/migrate_and_seed.sh](../scripts/db/migrate_and_seed.sh) uses Alembic,
including triggers that ORM `create_all` does not install. Use a separate
disposable database for tests, and do not copy a personal or live database into
this migration.

Readiness depends on progress as well as process health. Scoring checks aged or
dead-lettered execution commands; indicator workers check signal backlog and
source lag; execution checks ambiguous submissions, paper-order lag, and initial
account reconciliation. The corresponding age bounds default to 300 seconds.
Database pools default to `3 + 2 <= 5` connections per process. Compose uses
bounded log rotation and a 60-second application stop grace period. Changes to
these limits require measured capacity and failure evidence.

## Optional future release workflow

[build-and-push.yml](../.github/workflows/build-and-push.yml) is a manual
`workflow_dispatch` image build/publication path. Publication is
opt-in (`publish=false` by default); pushing a tag is not an automatic registry
publication instruction. A configured destination must explicitly provide
`DO_REGISTRY_NAME` and the registry credential. No registry or GitHub owner is
assumed by the local migration.

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

Paper promotion manifests bind the exact image/config/evidence, strategy version,
user, binding, broker account, data scope, and instrument set. Portfolio
manifests also bind the model configuration. They record `live_authority=false`.
Inherited marker-writing utilities and broker certification descriptions are
technical references only; no certificate or live authority is supplied here.
Keep `EXECUTION_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false` throughout
this migration and its verification.
