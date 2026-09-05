# syntax=docker/dockerfile:1.7
# Scoring Engine image. The backend API and migration one-shot intentionally
# share this image because their dependencies are a subset of scoring's.

FROM vynmatrix/svc-base:latest AS wheel-builder

ENV PYTHONPATH=/opt/service/lib/python3.11/site-packages:/opt/runtime/lib/python3.11/site-packages

RUN /usr/local/bin/python -m venv /opt/service
COPY docker/requirements-scoring.txt /tmp/requirements-scoring.txt
COPY docker/constraints.txt /tmp/constraints.txt
RUN --mount=type=cache,id=vm-scoring-pip,target=/root/.cache/pip,sharing=locked \
    /opt/service/bin/python -m pip install \
        --no-compile \
        -c /tmp/constraints.txt \
        -r /tmp/requirements-scoring.txt

COPY build/wheels/lib_common-*.whl /tmp/wheels/
COPY build/wheels/lib_data-*.whl /tmp/wheels/
COPY build/wheels/lib_strategy-*.whl /tmp/wheels/
COPY build/wheels/lib_application-*.whl /tmp/wheels/
COPY build/wheels/lib_infrastructure-*.whl /tmp/wheels/
RUN /opt/service/bin/python -m pip install --no-compile --no-deps /tmp/wheels/*.whl \
    && /opt/service/bin/python -m pip check \
    && rm -rf /opt/service/lib/python3.11/site-packages/alembic/testing \
    && /opt/service/bin/python -m pip uninstall --yes pip setuptools

FROM vynmatrix/svc-base:latest AS runtime

ENV PATH=/opt/service/bin:/opt/runtime/bin:$PATH \
    PYTHONPATH=/opt/service/lib/python3.11/site-packages:/opt/runtime/lib/python3.11/site-packages:/app:/app/apps/scoring_engine:/app/apps/backend

# postgresql-client serves ONLY the db-migrate one-shot's seed step
# (migrate_and_seed.sh drives psql -v/-f semantics; porting the seed runner to
# psycopg2 would save ~15MB here but risks psql variable-substitution drift —
# deliberately deferred). Installed BEFORE the wheel COPY so the OS layer is
# independent of wheel content and stays cached across every lib change.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=wheel-builder /opt/service /opt/service

COPY --chown=vmuser:vmuser apps/scoring_engine /app/apps/scoring_engine
# Migration + seed assets for the db-migrate one-shot: alembic upgrade head
# (incl. the NOTIFY triggers create_all omits) + docker/seed/*.sql, driven by
# scripts/db/migrate_and_seed.sh. Bake these assets into the image so migration
# jobs do not depend on mounting a repository checkout.
COPY --chown=vmuser:vmuser scripts/db /app/scripts/db
COPY --chown=vmuser:vmuser docker/seed /app/docker/seed
COPY --chown=vmuser:vmuser docker/provision-runtime-roles.sh /app/docker/provision-runtime-roles.sh
# apps/backend (tenant config API) ships in THIS image and runs as its own
# container with a command override (python -m apps.backend.backend.main) —
# same pattern as the db-migrate one-shot. Rationale: DOCR Basic caps the
# registry at 5 repositories; a 6th standalone image would force the $20/mo
# Professional tier for a tiny FastAPI app whose dependency closure is a strict
# subset of this image's. Runtime isolation is untouched (own container, own
# env, vm_backend DB role, own limits). Re-split into its own image when either
# the DOCR tier is upgraded for other reasons or backend's dependencies
# diverge from scoring's.
COPY --chown=vmuser:vmuser apps/backend /app/apps/backend

USER vmuser

EXPOSE 8001

# Default environment variables (override in docker-compose or deploy)
ENV HOST=0.0.0.0
ENV PORT=8001

# Database configuration (PostgreSQL)
# DATABASE_URL=postgresql://user:pass@host:5432/dbname
# Or use individual env vars:
ENV ENV=dev
ENV DB_HOST=localhost
ENV DB_PORT=5432
ENV DB_NAME=vm_trading
# DATABASE_URL (preferred) or an explicit DB_USER and DB_PASSWORD must be
# injected at runtime; images never select a database identity.

# Scoring engine settings
ENV HALF_LIFE_BARS=20
ENV SCORE_WEIGHTS=

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"]

CMD ["python", "-m", "apps.scoring_engine.scoring_engine.main"]
