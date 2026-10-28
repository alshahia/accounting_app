# syntax=docker/dockerfile:1
# Al-Mahmoud ERP production image. The build has two stages: the frontend bundle is
# compiled once, then copied next to the API so one container serves the whole
# product from a single origin.
#
#   docker build -f deploy/Dockerfile -t almahmoud-erp-api:prod .
#
# Build-time network is needed only for the base images and the dependency
# download. The resulting image is self-contained: it runs from /srv/.venv and
# never downloads a package at start-up (T-31, AC-14..AC-17).

FROM node:22-alpine AS web-build

RUN corepack enable && corepack prepare pnpm@10.18.0 --activate
WORKDIR /web
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web ./
RUN pnpm run build

FROM python:3.14-slim AS api

# Arabic-capable fonts so PDF reports embed real glyphs in the Linux container
# as well as on a Windows host. `fonts-dejavu-core` covers the Arabic block and
# `fonts-noto-core` supplies Noto Naskh Arabic; the renderer falls back to
# Helvetica only when neither is present.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7.18 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/srv/.venv/bin:/srv/.venv/Scripts:/usr/local/bin:/usr/bin:/bin"

WORKDIR /srv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY --from=web-build /web/dist ./web/dist

# The slim base has no useradd; an explicit numeric uid keeps the container
# unprivileged without adding a package-install layer.
RUN chown -R 10001:10001 /srv
ENV HOME=/srv
USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=6 \
    CMD ["/srv/.venv/bin/python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2).status == 200 else 1)"]

CMD ["/srv/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
