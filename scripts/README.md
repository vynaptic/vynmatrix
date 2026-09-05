# Script catalogue

Scripts support the declared local paper platform. Their presence does not
authorize deployment, broker activity, a database reset, or a change to the
live-execution gate. Use an explicit target and the document that owns the
workflow before running one.

## Build and environment helpers

| Script | Purpose | Reference |
| --- | --- | --- |
| build_strategies.sh / build_strategies.ps1 | Build declared wheels, environments, and platform image | [SETUP.md](../SETUP.md) |
| diagnose_environments.sh / diagnose_environments.ps1 | Report local Python, vmdev, venv, and wheel state | [SETUP.md](../SETUP.md) |
| setup_windows.ps1 | Windows counterpart to repository setup | [SETUP_WINDOWS.md](../SETUP_WINDOWS.md) |
| venv/create_dev_venv.sh / .ps1 | Create a development environment | [SETUP.md](../SETUP.md) |

## Runtime and database helpers

| Script | Purpose | Reference |
| --- | --- | --- |
| run_platform.py / platform_processes.py | Supervise declared child processes and run bounded jobs in an existing group | [DEPLOYMENT.md](../docs/DEPLOYMENT.md) |
| replay_canonical_signals.py | Replay already persisted signals and real prices through account-scoped local-paper execution | [E2E_VERIFICATION_GUIDE.md](../docs/E2E_VERIFICATION_GUIDE.md) |
| audit_table_counts.sql | Diagnose estimated PostgreSQL table counts | [DATABASE.md](../docs/DATABASE.md) |
| db/pre_migration_backup.sh | Legacy explicit pg_dump helper; supported backup/restore uses vmdev db | [DATABASE.md](../docs/DATABASE.md) |
| db/check_schema_drift.py | Verify an Alembic-built PostgreSQL schema against ORM metadata | [DATABASE.md](../docs/DATABASE.md) |
| db/production_seed_guard.sql | Guard against unsafe seed behavior | [DATABASE.md](../docs/DATABASE.md) |

The supported runtime jobs execute inside workers or the combined application
group. For example:

~~~text
python -m scripts.run_platform job backfill --timeout-seconds 3600
python -m scripts.run_platform job quality-compounder --timeout-seconds 3600
~~~

Invoke them with compose exec in the existing group, not compose run or another
container. The exact preconditions and evidence are documented in the E2E guide
and the relevant strategy reference.

## Credentials, evidence, and certification helpers

| Script | Purpose | Reference |
| --- | --- | --- |
| manage_broker_secret.py | Account-scoped encrypted secret checks and rotation | [BROKER_CREDENTIALS.md](../docs/BROKER_CREDENTIALS.md) |
| write_paper_promotion_manifest.py | Generate a fail-closed paper authority from reviewed matched evidence | [E2E_VERIFICATION_GUIDE.md](../docs/E2E_VERIFICATION_GUIDE.md) |
| check_soak_acceptance.py / verify_pipeline_soak.py | Evaluate recorded paper-soak evidence | [E2E_VERIFICATION_GUIDE.md](../docs/E2E_VERIFICATION_GUIDE.md) |
| write_sandbox_certification_marker.py | Source utility for a separately authorized sandbox-certification workflow | [RUNBOOK.md](../docs/RUNBOOK.md) |

The promotion writer neither creates evidence nor grants live authority. The
Swing canary is permanently excluded from promotion. Secrets, database URLs,
and evidence artifacts stay outside the repository.

## Maintenance rule

Document a new script here with its purpose and its canonical workflow owner.
Do not add a parallel setup, seed, scheduler, Docker topology, or verification
guide beside it.
