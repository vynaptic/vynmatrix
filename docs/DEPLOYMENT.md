# Local Docker and release boundary

vynmatrix supports a self-hosted local paper runtime. This document owns the
Compose topology and lifecycle envelope. It does not authorize cloud deployment,
broker execution, paper promotion, or live trading.

## Build the declared image

Build only images declared by the repository configuration:

~~~text
vmdev build docker --from-config --tag latest
~~~

The shared vynmatrix/platform image contains the application and worker
programs. Image count is not container count. Build prerequisites are in
[SETUP.md](../SETUP.md); configuration keys are in
[CONFIGURATION.md](CONFIGURATION.md).

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

Use the database lifecycle rather than arbitrary Compose profiles:

~~~text
vmdev db bootstrap --owner-config owner.local.yaml
vmdev db status
vmdev db start
vmdev db stop
~~~

Bootstrap stops application groups, verifies the maintenance window, runs the
declared job, removes it, and starts only selected groups. The full database,
role, migration, repeat, backup, and restore contract is in
[DATABASE.md](DATABASE.md).

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

## Upgrade and recovery boundary

Before an existing-data upgrade, take and validate a protected backup, retain
the prior image and encryption-key ring, then use the explicit migration path.
Do not remove volumes to resolve a startup failure. Restore leaves runtime
stopped so the owner can verify the intended database and grants before restart.
[DATABASE.md](DATABASE.md) is the source for these steps; [RUNBOOK.md](RUNBOOK.md)
contains incident actions.

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
