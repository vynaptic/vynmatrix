#!/usr/bin/env bash
# One-shot database migrate + seed for a cloud (or local) deployment.
#
# Runs the canonical sequence the cloud bootstrap needs and that nothing else in
# the deploy pipeline currently does (G2):
#   1) alembic upgrade head   — builds the full schema INCLUDING the outbox/
#      market-data NOTIFY triggers that the app's create_all() does NOT install.
#   2) apply docker/seed/*.sql — instruments, aliases, strategies, sizing
#      profiles, and the demo paper tenants (demo_user + demo_peer). Idempotent.
#
# Designed to run as a db-migrate one-shot job before request/worker services start, and
# locally for a fresh database. Cloud-agnostic: only needs DATABASE_URL, alembic,
# and psql on PATH.
#
# Usage:
#   DATABASE_URL=postgresql://user:pass@host:5432/vm_trading scripts/db/migrate_and_seed.sh
#
# Env:
#   DATABASE_URL         (required) target Postgres URL
#   SEED                 (optional) "false" to skip the seed step (migrate only)
#   SEED_PROFILE         (optional) development|production; production requires
#                        an immutable image tag and runs the fail-closed
#                        convergence guard.
#   VM_DEPLOY_IMAGE_TAG  (required for production) immutable SemVer image tag
#                        recorded for newly inserted strategy-version rows.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "[migrate-seed] ERROR: DATABASE_URL is required" >&2
    exit 1
fi

echo "[migrate-seed] 1/2 alembic upgrade head"
alembic -c scripts/db/alembic.ini upgrade head

if [[ "${SEED:-true}" == "false" ]]; then
    echo "[migrate-seed] SEED=false — skipping seed step"
    exit 0
fi

seed_profile="${SEED_PROFILE:-development}"
case "${seed_profile}" in
    development)
        image_tag="${VM_DEPLOY_IMAGE_TAG:-latest}"
        ;;
    production)
        image_tag="${VM_DEPLOY_IMAGE_TAG:-}"
        if [[ ! "${image_tag}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            echo "[migrate-seed] ERROR: production VM_DEPLOY_IMAGE_TAG must be SemVer X.Y.Z" >&2
            exit 1
        fi
        ;;
    *)
        echo "[migrate-seed] ERROR: SEED_PROFILE must be development or production" >&2
        exit 1
        ;;
esac

echo "[migrate-seed] 2/2 applying ${seed_profile} seed files (idempotent)"
for f in docker/seed/*.sql; do
    echo "[migrate-seed] applying $f"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q \
        -v image_tag="${image_tag}" \
        -f "$f"
done

if [[ "${seed_profile}" == "production" ]]; then
    echo "[migrate-seed] applying production fail-closed guard"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q \
        -v image_tag="${image_tag}" \
        -f scripts/db/production_seed_guard.sql
fi

echo "[migrate-seed] complete"
