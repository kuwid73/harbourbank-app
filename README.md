# harbourbank-app

Core banking service (`accounts-api`) and its fraud screening (`fraud-check`) for **Harbour Bank**, a fictional bank used in an agentic
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

## Fraud check

`fraud-check` scores each customer transfer before the money moves. It runs from the same image:
`uvicorn fraud_check.main:create_app --factory --port 8000`. accounts-api sends it what the rules
need: the amount, whether this is the first payment to the payee, and the account's transfers in the
last hour. It holds no customer data. A transfer is blocked when its score reaches `FRAUD_BLOCK_SCORE`:

| Rule | Points | Setting (default) |
|---|---|---|
| Large first payment to a new payee | 70 | `FRAUD_LARGE_NEW_PAYEE_CENTS` (100000) |
| Too many transfers in an hour | 70 | `FRAUD_MAX_TRANSFERS_PER_HOUR` (10) |
| Large amount | 40 | `FRAUD_LARGE_AMOUNT_CENTS` (500000) |

**How accounts-api uses it:**
- The check runs only for customer transfers, not deposits, interest or reversals, and only when `FRAUD_CHECK_URL` is set.
- The call is made **before any row is locked**, with the database connection given back, so a slow fraud check holds no locks or connections.
- **The bank's own rules come first.** A transfer that fails on funds, limits or a frozen account is rejected for that reason.
- **A block** is rejected with reason `fraud_suspected`, and no money moves. Each transfer records `fraud_check` (`allow`, `block` or `unavailable`).
- **If fraud-check is down, slow (past `FRAUD_CHECK_TIMEOUT_SECONDS`) or answers badly:**
  - transfers up to `FRAUD_CHECK_FALLBACK_LIMIT_CENTS` go through, marked `unavailable` (unchecked, for review)
  - larger ones are rejected with reason `fraud_check_unavailable`
- **Metrics:** `bank_fraud_checks_total{outcome}` (allow, block, timeout, error) and `bank_fraud_check_duration_seconds`, plus fraud-check's own `fraud_check_decisions_total`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | *(required)* | Postgres URL (`postgresql://...` accepted) |
| `DAILY_TRANSFER_LIMIT_CENTS` | `1000000` | Max outgoing customer transfers per UTC day |
| `INTEREST_RATE_BPS` | `150` | Annual interest on customer balances in basis points (150 = 1.50%) |
| `DB_POOL_SIZE` | `5` | Connections per process (no overflow) |
| `DB_POOL_TIMEOUT_SECONDS` | `5` | Wait for a free connection before failing with `db_pool_exhausted` |
| `FRAUD_CHECK_URL` | *(unset: no fraud check)* | fraud-check's base URL, e.g. `http://fraud-check:8000` |
| `FRAUD_CHECK_TIMEOUT_SECONDS` | `0.3` | Give up on fraud-check after this; no retry |
| `FRAUD_CHECK_FALLBACK_LIMIT_CENTS` | `10000` | While fraud-check is unavailable, transfers up to this go through unchecked |
| `LOG_LEVEL` | `INFO` | |
| `APP_VERSION`, `GIT_SHA` | `dev`, `unknown` | Set by CI; exported as the `app_info` metric |
