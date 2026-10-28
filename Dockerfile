# syntax=docker/dockerfile:1
# One image, three uses:
#   default command               -> the accounts-api web server
#   alembic upgrade head          -> the database migration Job (ArgoCD Sync hook)
#   python -m accounts_api.eod    -> the end-of-day batch CronJob

# --- build: resolve and install dependencies with uv ----------------------------------
FROM ghcr.io/astral-sh/uv:0.11-python3.13-trixie-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Dependencies first: this layer is cached until uv.lock changes
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- runtime: slim image, no build tools, non-root ---------------------------------------
FROM python:3.13-slim-trixie
# pip (and its vendored urllib3/msgpack) is unused at runtime - uv installed everything in the
# build stage - so remove it rather than carry its CVEs. No package manager also means less to abuse.
RUN python -m pip uninstall --yes --no-cache-dir pip \
 && rm -rf /usr/local/lib/python3.13/ensurepip
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app

COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Set by CI; declared last so a new commit doesn't invalidate the cached layers above.
# The app reports them at /info and in the app_info metric, so every pod can be traced to its commit.
ARG APP_VERSION=dev
ARG GIT_SHA=unknown
ENV APP_VERSION=$APP_VERSION GIT_SHA=$GIT_SHA
LABEL org.opencontainers.image.source="https://github.com/kuwid73/harbourbank-app" \
      org.opencontainers.image.title="Harbour Bank accounts-api" \
      org.opencontainers.image.version=$APP_VERSION \
      org.opencontainers.image.revision=$GIT_SHA
# Numeric UID so Kubernetes can verify runAsNonRoot
USER 10001
EXPOSE 8000
CMD ["uvicorn", "accounts_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
