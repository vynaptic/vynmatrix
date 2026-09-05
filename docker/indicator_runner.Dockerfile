# syntax=docker/dockerfile:1.7
# Indicator Runner image (signal_worker).

FROM vynmatrix/svc-base:latest AS wheel-builder

ENV PYTHONPATH=/opt/service/lib/python3.11/site-packages:/opt/runtime/lib/python3.11/site-packages:/opt/strategies/indicator

RUN /usr/local/bin/python -m venv /opt/service
COPY docker/requirements-indicator-runner.txt /tmp/requirements-indicator-runner.txt
COPY docker/constraints.txt /tmp/constraints.txt
RUN --mount=type=cache,id=vm-indicator-pip,target=/root/.cache/pip,sharing=locked \
    /opt/service/bin/python -m pip install \
        --no-compile \
        -c /tmp/constraints.txt \
        -r /tmp/requirements-indicator-runner.txt

COPY build/wheels/lib_common-*.whl /tmp/wheels/
COPY build/wheels/lib_data-*.whl /tmp/wheels/
COPY build/wheels/lib_indicators-*.whl /tmp/wheels/
COPY build/wheels/lib_strategy-*.whl /tmp/wheels/
COPY build/wheels/lib_application-*.whl /tmp/wheels/
COPY build/wheels/lib_infrastructure-*.whl /tmp/wheels/
COPY build/wheels/vynmatrix_indicator-*.whl /tmp/strategy-wheel/
RUN /opt/service/bin/python -m pip install --no-compile --no-deps /tmp/wheels/*.whl \
    && /opt/service/bin/python -m pip install \
        --no-compile \
        --no-deps \
        --target /opt/strategies/indicator \
        /tmp/strategy-wheel/vynmatrix_indicator-*.whl \
    && /opt/service/bin/python -m pip check \
    && find /opt/service/lib/python3.11/site-packages \
        -type d \( -name test -o -name tests \) -prune -exec rm -rf '{}' + \
    && find /opt/service/lib/python3.11/site-packages \
        -type f \( -name 'test_*.py' -o -name '*_test.py' \) -delete \
    && /opt/service/bin/python -m pip uninstall --yes pip setuptools

FROM vynmatrix/svc-base:latest AS runtime

ENV PATH=/opt/service/bin:/opt/runtime/bin:$PATH \
    PYTHONPATH=/opt/service/lib/python3.11/site-packages:/opt/runtime/lib/python3.11/site-packages:/app:/app/apps/indicator_runner:/app/strategies/indicator

COPY --from=wheel-builder /opt/service /opt/service
COPY --from=wheel-builder --chown=vmuser:vmuser /opt/strategies/indicator /app/strategies/indicator
COPY --chown=vmuser:vmuser apps/indicator_runner /app/apps/indicator_runner
COPY --chown=vmuser:vmuser config /app/config
COPY --chown=vmuser:vmuser \
    scripts/write_paper_promotion_manifest.py \
    /app/scripts/write_paper_promotion_manifest.py

RUN install -d -o vmuser -g vmuser /tmp/vynmatrix-prometheus

USER vmuser

ENV HEALTH_CHECK_PORT=8080 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/vynmatrix-prometheus

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]

CMD ["python", "-m", "indicator_runner.main"]
