# syntax=docker/dockerfile:1.7
# One image for the API (which also serves the reviewer UI) and the worker.
#   API:    docker run IMAGE                    (default CMD)
#   Worker: docker run IMAGE medchron worker --once

# ---- UI build ---------------------------------------------------------------
FROM node:22.11-bookworm-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# ---- Python build -----------------------------------------------------------
FROM python:3.12.7-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.4.30 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv venv /opt/venv && VIRTUAL_ENV=/opt/venv uv pip install --no-cache ".[gcp]"

# ---- Runtime ----------------------------------------------------------------
FROM python:3.12.7-slim-bookworm AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 medchron \
 && mkdir -p /data/storage && chown medchron:medchron /data/storage
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=medchron:medchron alembic.ini ./
COPY --chown=medchron:medchron migrations ./migrations
COPY --chown=medchron:medchron data/synthetic ./data/synthetic
COPY --from=ui --chown=medchron:medchron /ui/dist ./ui/dist
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MEDCHRON_UI_DIST=/app/ui/dist \
    MEDCHRON_GOLD_DIR=/app/data/synthetic \
    MEDCHRON_RUNS_DIR=/tmp/runs \
    PORT=8080
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=2)"
CMD ["sh", "-c", "exec uvicorn medchron.api.app:create_app --factory --host 0.0.0.0 --port ${PORT} --proxy-headers"]
