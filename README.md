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
uv run python -m accounts_api.eod                                    # close yesterday (end-of-day batch)
```

## End-of-day batch

`python -m accounts_api.eod` closes each business day (UTC) that hasn't been closed yet,
oldest first: it checks the ledger balances, pays a day's interest on each active
customer account's balance at midnight, checks the ledger again and records the result
in the `eod_runs` table. It is safe to re-run, and a failed day is retried by the next run.
The API publishes the latest results as `bank_eod_*` and `bank_ledger_*` metrics, since
the job itself finishes too quickly to be scraped.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | *(required)* | Postgres URL (`postgresql://...` accepted) |
| `DAILY_TRANSFER_LIMIT_CENTS` | `1000000` | Max outgoing customer transfers per UTC day |
| `INTEREST_RATE_BPS` | `150` | Annual interest on customer balances in basis points (150 = 1.50%) |
| `DB_POOL_SIZE` | `5` | Connections per process (no overflow) |
| `DB_POOL_TIMEOUT_SECONDS` | `5` | Wait for a free connection before failing with `db_pool_exhausted` |
| `LOG_LEVEL` | `INFO` | |
| `APP_VERSION`, `GIT_SHA` | `dev`, `unknown` | Set by CI; exported as the `app_info` metric |
