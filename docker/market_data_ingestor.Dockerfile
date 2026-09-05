# syntax=docker/dockerfile:1.7
# Market Data Ingestor image.

FROM vynmatrix/svc-base:latest AS wheel-builder

ENV PYTHONPATH=/opt/service/lib/python3.11/site-packages:/opt/runtime/lib/python3.11/site-packages

RUN /usr/local/bin/python -m venv /opt/service
COPY docker/requirements-market-data.txt /tmp/requirements-market-data.txt
COPY docker/constraints.txt /tmp/constraints.txt
RUN --mount=type=cache,id=vm-market-data-pip,target=/root/.cache/pip,sharing=locked \
    /opt/service/bin/python -m pip install \
        --no-compile \
        -c /tmp/constraints.txt \
        -r /tmp/requirements-market-data.txt

COPY build/wheels/lib_common-*.whl /tmp/wheels/
COPY build/wheels/lib_data-*.whl /tmp/wheels/
COPY build/wheels/lib_indicators-*.whl /tmp/wheels/
COPY build/wheels/lib_strategy-*.whl /tmp/wheels/
COPY build/wheels/lib_application-*.whl /tmp/wheels/
COPY build/wheels/lib_infrastructure-*.whl /tmp/wheels/
COPY build/wheels/vynmatrix_indicator-*.whl /tmp/wheels/
RUN /opt/service/bin/python -m pip install --no-compile --no-deps /tmp/wheels/*.whl \
    && /opt/service/bin/python -m pip check \
    && /opt/service/bin/python -m pip uninstall --yes pip setuptools

FROM vynmatrix/svc-base:latest AS runtime

ENV PATH=/opt/service/bin:/opt/runtime/bin:$PATH \
    PYTHONPATH=/opt/service/lib/python3.11/site-packages:/opt/runtime/lib/python3.11/site-packages:/app

COPY --from=wheel-builder /opt/service /opt/service
COPY --chown=vmuser:vmuser apps/market_data_ingestor /app/apps/market_data_ingestor

USER vmuser

EXPOSE 8003

ENV HOST=0.0.0.0
ENV PORT=8003
ENV ENV=dev
ENV DB_HOST=localhost
ENV DB_PORT=5432
ENV DB_NAME=vm_trading
# DATABASE_URL (preferred) or an explicit DB_USER and DB_PASSWORD must be
# injected at runtime; images never select a database identity.

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8003/ready')"]

CMD ["python", "-m", "apps.market_data_ingestor.market_data_ingestor.main", "ingest"]
