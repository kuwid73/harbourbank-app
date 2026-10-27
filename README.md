# harbourbank-app

Core banking service (`accounts-api`) for **Harbour Bank**, a fictional bank used in an agentic
DevOps lab. Synthetic data only.

CI builds the image, pushes it to GHCR and bumps the image tag in
[harbourbank-gitops](https://github.com/kuwid73/harbourbank-gitops); ArgoCD deploys it.

## Run locally

```bash
uv sync
uv run pytest                      # starts a throwaway Postgres in Docker

docker run -d --name hb-pg -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=bank -p 127.0.0.1:55432:5432 postgres:17-alpine
export DATABASE_URL=postgresql://postgres:devpass@127.0.0.1:55432/bank
uv run alembic upgrade head
uv run uvicorn accounts_api.main:create_app --factory --port 8000   # API docs: http://localhost:8000/docs
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | *(required)* | Postgres URL (`postgresql://...` accepted) |
| `DAILY_TRANSFER_LIMIT_CENTS` | `1000000` | Max outgoing customer transfers per UTC day |
| `DB_POOL_SIZE` | `5` | Connections per process (no overflow) |
| `DB_POOL_TIMEOUT_SECONDS` | `5` | Wait for a free connection before failing with `db_pool_exhausted` |
| `LOG_LEVEL` | `INFO` | |
| `APP_VERSION`, `GIT_SHA` | `dev`, `unknown` | Set by CI; exported as the `app_info` metric |
