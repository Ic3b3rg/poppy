# python:3.14.7-slim-trixie, pinned to its multi-platform manifest.
ARG PYTHON_IMAGE=python:3.14.7-slim-trixie@sha256:83ff1d245a3d57d04152252d3ef9cb361494d0b3395abd65a5ebe91c401c8e83

FROM ${PYTHON_IMAGE} AS builder

ARG UV_VERSION=0.10.10
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM ${PYTHON_IMAGE} AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN groupadd --gid 10001 poppy \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin poppy \
    && mkdir -p /var/lib/poppy /var/empty/poppy \
    && chmod 700 /var/lib/poppy /var/empty/poppy \
    && chown -R poppy:poppy /var/lib/poppy /var/empty/poppy

COPY --from=builder --chown=poppy:poppy /app/.venv /app/.venv

USER poppy
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).read()"]

ENTRYPOINT ["poppy"]
CMD ["serve"]
