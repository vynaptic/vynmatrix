# Quick reference

Run these from the repository root with the prepared tooling environment active.
The command contracts and safety limits are owned by the linked documents.

## Build and validate

Complete [SETUP.md](../SETUP.md) first. Then run the checks or builds that
match the work:

~~~text
vmdev build libs
vmdev build strategies
vmdev build venvs
vmdev build docker --from-config --tag latest

vmdev test lib --name=lib_common
vmdev test team --team=<team>
vmdev test all
python -m pytest <existing-path-or-node-id>

vmdev format
vmdev audit --strict
~~~

vmdev test has only all, lib, and team. Use vmdev strategy for recorded-data
strategy campaigns after building the strategy-validation environment.
[SETUP.md](../SETUP.md) and [CONTRIBUTING.md](../CONTRIBUTING.md) describe when
to use each check.

## PostgreSQL lifecycle

~~~text
vmdev db bootstrap --owner-config owner.local.yaml
vmdev db status
vmdev db catalogue --check
vmdev db catalogue --apply
vmdev user show
vmdev db migrate
vmdev db backup backups/pre-upgrade.dump
vmdev db restore backups/pre-upgrade.dump
vmdev db start
vmdev db stop
~~~

Use explicit private environment variables for all database work; never put
credentials in a command line. Bootstrap, catalogue behavior, backup/restore,
and existing-database rules are defined in [DATABASE.md](DATABASE.md).

## Runtime inspection

~~~text
docker compose --env-file .env -f docker/docker-compose.stack.yml ps
docker compose --env-file .env -f docker/docker-compose.stack.yml logs --tail 100 application workers
docker compose --env-file .env -f docker/docker-compose.stack.yml exec -T application <command>
docker compose --env-file .env -f docker/docker-compose.stack.yml exec -T workers <command>
~~~

These examples use the split layout. In the combined layout, omit workers from
the logs command and run worker work with exec against application.

Use only declared Compose services. The normal split layout has PostgreSQL,
application, and workers; the combined layout has PostgreSQL and one application
group. [DEPLOYMENT.md](DEPLOYMENT.md) defines startup, shutdown, ports, and the
three-container limit.

## Git and pull requests

~~~text
git status
git switch main
git pull --ff-only origin main
git add <paths>
git commit -m "docs: concise subject"
git mr submit
~~~

`git mr submit` creates and pushes the branch from local `main`; it does not
accept an existing feature branch. Use a pull request for main, review the
staged paths, and let the required CI gate complete.
[CONTRIBUTING.md](../CONTRIBUTING.md) owns the branch and submission policy.

## Common locations

| Need | Location |
| --- | --- |
| Runtime environment template | [.env.example](../.env.example) |
| Build/image declarations | [config/build.yaml](../config/build.yaml) and [config/containers.yaml](../config/containers.yaml) |
| Compose topology | [docker/docker-compose.stack.yml](../docker/docker-compose.stack.yml) |
| Database models | [libs/python/lib_application/lib_application/db/models](../libs/python/lib_application/lib_application/db/models) |
| Signal contract | [libs/python/lib_strategy/lib_strategy/signals/signal.py](../libs/python/lib_strategy/lib_strategy/signals/signal.py) |
| Dev CLI | [tools/dev_cli](../tools/dev_cli) |
| Operational scripts | [scripts](../scripts) and [scripts/README.md](../scripts/README.md) |

For configuration variables, use [CONFIGURATION.md](CONFIGURATION.md) and
.env.example. Do not copy a generic DATABASE_URL, API key, or password example
from another document into a runtime group.
