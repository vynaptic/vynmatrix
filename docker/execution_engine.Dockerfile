# syntax=docker/dockerfile:1.7
# Execution Engine image.

FROM vynmatrix/svc-base:latest AS wheel-builder

ENV PYTHONPATH=/opt/service/lib/python3.11/site-packages:/opt/runtime/lib/python3.11/site-packages

RUN /usr/local/bin/python -m venv /opt/service
COPY docker/requirements-execution.txt /tmp/requirements-execution.txt
COPY docker/constraints.txt /tmp/constraints.txt
RUN --mount=type=cache,id=vm-execution-pip,target=/root/.cache/pip,sharing=locked \
    /opt/service/bin/python -m pip install \
        --no-compile \
        -c /tmp/constraints.txt \
        -r /tmp/requirements-execution.txt

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
    PYTHONPATH=/opt/service/lib/python3.11/site-packages:/opt/runtime/lib/python3.11/site-packages:/app:/app/apps/execution_engine

COPY --from=wheel-builder /opt/service /opt/service
COPY --chown=vmuser:vmuser apps/execution_engine /app/apps/execution_engine
# The production soak gate runs inside this image so it uses the exact deployed
# dependency set, service-role URL, and alert environment. It is an operator
# one-shot, not another service or image.
COPY --chown=vmuser:vmuser \
    scripts/check_soak_acceptance.py \
    scripts/replay_canonical_signals.py \
    /app/scripts/

USER vmuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]

CMD ["python", "-m", "apps.execution_engine.main"]
