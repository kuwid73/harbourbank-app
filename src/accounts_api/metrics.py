"""Prometheus metrics.

RED metrics (rate, errors, duration) per route, business metrics for transfers,
and app_info so the agent can correlate a regression with the version that shipped it.
"""
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import Engine

from .config import Settings

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
TRANSFERS = Counter(
    "bank_transfers_total", "Money movements by outcome", ["kind", "status", "reason"]
)
APP_INFO = Gauge("app_info", "Running application version (always 1)", ["version", "git_sha"])
DB_POOL_SIZE = Gauge("db_pool_size", "Configured database connection pool size")
DB_POOL_IN_USE = Gauge("db_pool_connections_in_use", "Database connections currently checked out")


def init(settings: Settings, engine: Engine) -> None:
    APP_INFO.labels(version=settings.app_version, git_sha=settings.git_sha).set(1)
    DB_POOL_SIZE.set(settings.db_pool_size)
    DB_POOL_IN_USE.set_function(engine.pool.checkedout)


def record_transfer(transfer) -> None:
    if transfer.replayed:   # an idempotent retry is not a new money movement
        return
    reason = transfer.reject_reason.value if transfer.reject_reason else "none"
    TRANSFERS.labels(kind=transfer.kind.value, status=transfer.status.value, reason=reason).inc()
