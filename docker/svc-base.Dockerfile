# syntax=docker/dockerfile:1.7
# Shared base for the composed platform runtime. Platform-specific additions
# are installed in the platform builder stage.

ARG PYTHON_IMAGE=python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

FROM ${PYTHON_IMAGE} AS dependency-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/runtime

COPY docker/requirements-svc-base.txt /tmp/requirements-svc-base.txt
COPY docker/constraints.txt /tmp/constraints.txt
RUN --mount=type=cache,id=vm-svc-base-pip,target=/root/.cache/pip,sharing=locked \
    /opt/runtime/bin/python -m pip install \
        --no-compile \
        -c /tmp/constraints.txt \
        -r /tmp/requirements-svc-base.txt \
    && /opt/runtime/bin/python -m pip check \
    && find /opt/runtime/lib/python3.11/site-packages \
        -type d \( -name test -o -name tests \) -prune -exec rm -rf '{}' + \
    && find /opt/runtime/lib/python3.11/site-packages \
        -type f \( -name 'test_*.py' -o -name '*_test.py' \) -delete \
    && /opt/runtime/bin/python -m pip uninstall --yes pip setuptools

FROM ${PYTHON_IMAGE} AS runtime

ENV PATH=/opt/runtime/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY --from=dependency-builder /opt/runtime /opt/runtime

RUN /usr/local/bin/python -m pip uninstall --yes pip setuptools \
    && useradd --create-home --shell /usr/sbin/nologin vmuser
