# syntax=docker/dockerfile:1.7
# Feedback Loop Engine image.

FROM vynmatrix/svc-base:latest AS wheel-builder

ENV PYTHONPATH=/opt/service/lib/python3.11/site-packages:/opt/runtime/lib/python3.11/site-packages

RUN /usr/local/bin/python -m venv /opt/service
COPY docker/requirements-feedback-loop.txt /tmp/requirements-feedback-loop.txt
COPY docker/constraints.txt /tmp/constraints.txt
RUN --mount=type=cache,id=vm-feedback-pip,target=/root/.cache/pip,sharing=locked \
    /opt/service/bin/python -m pip install \
        --no-compile \
        -c /tmp/constraints.txt \
        -r /tmp/requirements-feedback-loop.txt

COPY build/wheels/lib_common-*.whl /tmp/wheels/
COPY build/wheels/lib_data-*.whl /tmp/wheels/
COPY build/wheels/lib_strategy-*.whl /tmp/wheels/
COPY build/wheels/lib_application-*.whl /tmp/wheels/
COPY build/wheels/lib_infrastructure-*.whl /tmp/wheels/
RUN /opt/service/bin/python -m pip install --no-compile --no-deps /tmp/wheels/*.whl \
    && /opt/service/bin/python -m pip check \
    && /opt/service/bin/python -m pip uninstall --yes pip setuptools

FROM vynmatrix/svc-base:latest AS runtime

ENV PATH=/opt/service/bin:/opt/runtime/bin:$PATH \
    PYTHONPATH=/opt/service/lib/python3.11/site-packages:/opt/runtime/lib/python3.11/site-packages:/app:/app/apps/feedback_loop_engine

COPY --from=wheel-builder /opt/service /opt/service
COPY --chown=vmuser:vmuser apps/feedback_loop_engine /app/apps/feedback_loop_engine

USER vmuser

EXPOSE 8002

# Default environment variables (override in docker-compose or deploy)
ENV HOST=0.0.0.0
ENV PORT=8002
ENV ENV=dev
ENV DB_HOST=localhost
ENV DB_PORT=5432
ENV DB_NAME=vm_trading
# DATABASE_URL (preferred) or an explicit DB_USER and DB_PASSWORD must be
# injected at runtime; images never select a database identity.

# Feedback loop settings
ENV WRONG_THRESHOLD=2
ENV EVALUATION_INTERVAL=3600

# Mode-aware: in scheduled mode (the Compose default) the service is a
# one-shot `evaluate` on a timer with NO HTTP server, so curling :8002 would
# report a false 'unhealthy' forever — liveness in that mode is the
# service_heartbeats row the soak acceptance gate reads. Only daemon mode (which
# runs the :8002 health server) is HTTP-probed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; os.getenv('FEEDBACK_RUN_MODE') == 'scheduled' or urllib.request.urlopen('http://localhost:8002/health')"]

CMD ["python", "-m", "apps.feedback_loop_engine.feedback_loop_engine.main", "daemon"]
