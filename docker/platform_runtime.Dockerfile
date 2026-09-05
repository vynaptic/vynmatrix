# syntax=docker/dockerfile:1.7
# One release artifact for the application/workers/all process groups and bootstrap.

FROM vynmatrix/svc-base:latest AS wheel-builder

ENV PYTHONPATH=/opt/service/lib/python3.11/site-packages:/opt/runtime/lib/python3.11/site-packages:/opt/strategies/indicator

RUN /usr/local/bin/python -m venv /opt/service
COPY docker/requirements-platform.txt /tmp/requirements-platform.txt
COPY docker/constraints.txt /tmp/constraints.txt
RUN --mount=type=cache,id=vm-platform-pip,target=/root/.cache/pip,sharing=locked \
    /opt/service/bin/python -m pip install \
        --no-compile \
        -c /tmp/constraints.txt \
        -r /tmp/requirements-platform.txt

COPY build/wheels/lib_common-*.whl /tmp/wheels/
COPY build/wheels/lib_data-*.whl /tmp/wheels/
COPY build/wheels/lib_indicators-*.whl /tmp/wheels/
COPY build/wheels/lib_strategy-*.whl /tmp/wheels/
COPY build/wheels/lib_application-*.whl /tmp/wheels/
COPY build/wheels/lib_infrastructure-*.whl /tmp/wheels/
COPY build/wheels/vynmatrix_indicator-*.whl /tmp/strategy-wheel/
RUN /opt/service/bin/python -m pip install --no-compile --no-deps /tmp/wheels/*.whl \
    && /opt/service/bin/python -m pip install \
        --no-compile --no-deps --target /opt/strategies/indicator \
        /tmp/strategy-wheel/vynmatrix_indicator-*.whl \
    && /opt/service/bin/python -m pip check \
    && rm -rf /opt/service/lib/python3.11/site-packages/alembic/testing \
    && find /opt/service/lib/python3.11/site-packages \
        -type d \( -name test -o -name tests \) -prune -exec rm -rf '{}' + \
    && find /opt/service/lib/python3.11/site-packages \
        -type f \( -name 'test_*.py' -o -name '*_test.py' \) -delete \
    && /opt/service/bin/python -m pip uninstall --yes pip setuptools

FROM vynmatrix/svc-base:latest AS runtime

ENV PATH=/opt/service/bin:/opt/runtime/bin:$PATH \
    PYTHONPATH=/opt/service/lib/python3.11/site-packages:/opt/runtime/lib/python3.11/site-packages:/app:/app/tools/dev_cli:/app/apps/backend:/app/apps/scoring_engine:/app/apps/execution_engine:/app/apps/feedback_loop_engine:/app/apps/indicator_runner:/app/apps/market_data_ingestor:/app/strategies/indicator

# psql is used only by explicit migration/role administration inside bootstrap.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=wheel-builder /opt/service /opt/service
COPY --from=wheel-builder --chown=vmuser:vmuser /opt/strategies/indicator /app/strategies/indicator
COPY --chown=vmuser:vmuser apps/backend /app/apps/backend
COPY --chown=vmuser:vmuser apps/scoring_engine /app/apps/scoring_engine
COPY --chown=vmuser:vmuser apps/execution_engine /app/apps/execution_engine
COPY --chown=vmuser:vmuser apps/feedback_loop_engine /app/apps/feedback_loop_engine
COPY --chown=vmuser:vmuser apps/indicator_runner /app/apps/indicator_runner
COPY --chown=vmuser:vmuser apps/market_data_ingestor /app/apps/market_data_ingestor
COPY --chown=vmuser:vmuser tools/dev_cli/dev_cli /app/tools/dev_cli/dev_cli
COPY --chown=vmuser:vmuser config /app/config
COPY --chown=vmuser:vmuser scripts/db /app/scripts/db
COPY --chown=vmuser:vmuser docker/provision-runtime-roles.sh /app/docker/provision-runtime-roles.sh
COPY --chown=vmuser:vmuser \
    scripts/run_platform.py \
    scripts/platform_processes.py \
    scripts/check_soak_acceptance.py \
    scripts/replay_canonical_signals.py \
    scripts/write_paper_promotion_manifest.py \
    /app/scripts/

RUN install -d -o vmuser -g vmuser /tmp/vynmatrix-prometheus /tmp/vynmatrix-jobs

USER vmuser

ENV EXECUTION_MODE=paper EXECUTION_ENGINE_ALLOW_LIVE=false RUN_MODE=paper
EXPOSE 8081 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8090/health')"]

CMD ["python", "-m", "scripts.run_platform", "application"]
